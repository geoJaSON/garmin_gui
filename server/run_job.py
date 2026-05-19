"""Isolated job runner: `python -m server.run_job <job_id>`.

Runs in its own process so a native crash (GDAL/pingverter) can't take down
the API. Dispatches a DB job to garmin_core, streams throttled progress back
to the DB, COG-converts raster output, then marks the job done/error.
"""

from __future__ import annotations

import shutil
import sys
import time
import traceback
from pathlib import Path

from . import db
from .cog import to_cog
from .settings import RUNS_DIR, MOSAICS_DIR, TRACKS_DIR, POLYGONS_DIR


class _Throttle:
    """Coalesce frequent progress_cb calls into <=1 DB write / interval."""

    def __init__(self, job_id: str, interval: float = 1.0):
        self.job_id = job_id
        self.interval = interval
        self._last = 0.0

    def __call__(self, desc, n, total):
        now = time.time()
        if now - self._last < self.interval:
            return
        self._last = now
        pct = round(100.0 * n / total, 1) if total else None
        db.set_progress(self.job_id, {"desc": desc, "n": n, "total": total, "pct": pct})


def _run_mosaic(job: dict, prog: _Throttle) -> dict:
    from garmin_core.config import MosaicConfig
    from garmin_core.mosaic import run_mosaic

    p = job["params"]
    run_id = job["id"]
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Stage the RSD inside the run dir so outputs land under runs/<id>/.
    src_rsd = Path(p["rsd_path"]).expanduser().resolve()
    staged = run_dir / src_rsd.name
    if not staged.exists():
        try:
            staged.hardlink_to(src_rsd)
        except OSError:
            shutil.copy2(src_rsd, staged)

    cfg = MosaicConfig.from_dict(p.get("config") or {})
    out_dir = Path(run_mosaic(staged, cfg, progress_cb=prog))
    intensity = out_dir / "intensity.tif"
    cog = run_dir / "intensity_cog.tif"
    if intensity.exists():
        to_cog(intensity, cog)

    # Append this RSD's track to the shared inventory so the just-run
    # mosaic actually shows up on the (track-driven) map without a
    # separate tracks job. A failure here must not fail the mosaic.
    track_added = False
    try:
        from garmin_core.config import TracksConfig
        from garmin_core.tracks import build_track_inventory

        build_track_inventory(
            run_dir, TRACKS_DIR / "rsd_tracks.geojson", TracksConfig()
        )
        track_added = True
    except Exception as e:
        print(f"  track append skipped: {e}")

    return {
        "run_id": run_id,
        "processed_dir": str(out_dir),
        "intensity": str(intensity) if intensity.exists() else None,
        "cog": str(cog) if cog.exists() else None,
        "track_added": track_added,
    }


def _run_tracks(job: dict, prog: _Throttle) -> dict:
    from garmin_core.config import TracksConfig
    from garmin_core.tracks import build_track_inventory

    p = job["params"]

    def cb(done, total, name):
        prog(name, done, total)

    return build_track_inventory(
        p["input_folder"],
        p.get("output_path"),
        TracksConfig.from_dict(p.get("config") or {}),
        progress_cb=cb,
    )


def _run_combine(job: dict, prog: _Throttle) -> dict:
    """Combine existing run COGs into one mosaic.

    Operates on registered run COGs (the deployed layout: /data/runs/<id>/
    intensity_cog.tif), NOT the legacy garmin_output tree — so imported
    historical runs combine too.

    params:
      run_ids: list of mosaic job ids (W2: merge their COGs)
      polygon: GeoJSON geometry/Feature/FC (W3: tracks intersecting it)
      area:    {"Our_Name":..,"TPDW_App_No":..} -> clip by the matching
               feature in the buffered layer; the result is the
               downloadable per-area deliverable (Phase 5).
    """
    import json
    from garmin_core import areas as areas_mod
    from shapely.geometry import shape
    from shapely.prepared import prep

    p = job["params"]
    mosaic_dir = MOSAICS_DIR / job["id"]
    out = mosaic_dir / "mosaic.tif"

    def cog_for_run(run_id: str):
        j = db.get_job(run_id)
        if not j or not j.get("result"):
            return None
        c = j["result"].get("cog")
        return c if c and Path(c).exists() else None

    def cogs_intersecting(clip_geom):
        """Inventory tracks intersecting clip_geom -> their run COGs."""
        inv = TRACKS_DIR / "rsd_tracks.geojson"
        fc = json.loads(inv.read_text()) if inv.exists() else {"features": []}
        pc = prep(clip_geom)
        cg, nm = [], []
        for feat in fc.get("features", []):
            g = feat.get("geometry")
            if not g:
                continue
            geom = shape(g)
            if geom.is_empty or not pc.intersects(geom):
                continue
            fn = (feat.get("properties") or {}).get("file_name")
            if not fn:
                continue
            run = db.find_done_mosaic_by_rsd(fn)
            c = cog_for_run(run["id"]) if run else None
            if c:
                cg.append(c)
                nm.append(fn)
        return cg, nm

    clip = None
    area_name = None

    if p.get("area"):
        # Phase 5 deliverable: clip by the matching BUFFERED feature.
        key = p["area"]
        buf = POLYGONS_DIR / "layer_buffered.geojson"
        if not buf.exists():
            return {"ok": False, "reason": "no buffered layer uploaded",
                    "rasters": 0}
        fc = json.loads(buf.read_text())
        match = None
        for feat in fc.get("features", []):
            pr = feat.get("properties") or {}
            if (str(pr.get("Our_Name")) == str(key.get("Our_Name"))
                    and str(pr.get("TPDW_App_No")) == str(key.get("TPDW_App_No"))):
                match = feat
                break
        if match is None or not match.get("geometry"):
            return {"ok": False, "reason": "no matching buffered polygon",
                    "rasters": 0}
        clip = shape(match["geometry"])
        area_name = areas_mod.sanitize_name(
            str(key.get("Our_Name") or key.get("TPDW_App_No") or "area")
        )
        cogs, names = cogs_intersecting(clip)

    elif p.get("polygon"):
        poly = p["polygon"]
        t = (poly or {}).get("type")
        if t == "FeatureCollection":
            fcj = poly
        elif t == "Feature":
            fcj = {"type": "FeatureCollection", "features": [poly]}
        else:
            fcj = {"type": "FeatureCollection",
                   "features": [{"type": "Feature", "properties": {},
                                 "geometry": poly}]}
        poly_path = POLYGONS_DIR / f"{job['id']}.geojson"
        poly_path.parent.mkdir(parents=True, exist_ok=True)
        poly_path.write_text(json.dumps(fcj))
        clip = areas_mod.first_polygon(str(poly_path))
        cogs, names = cogs_intersecting(clip)

    else:
        run_ids = p.get("run_ids") or []
        cogs = [c for c in (cog_for_run(r) for r in run_ids) if c]
        names = run_ids

    if not cogs:
        return {"ok": False, "reason": "no source COGs resolved", "rasters": 0}

    prog("combine", 0, len(cogs))
    mosaic_dir.mkdir(parents=True, exist_ok=True)
    if clip is not None:
        ok = areas_mod.merge_and_clip_rasters([Path(c) for c in cogs], clip, out)
        mode = "merge+clip"
    else:
        ok = areas_mod._merge_rasters([Path(c) for c in cogs], out)
        mode = "merge"
    prog("combine", len(cogs), len(cogs))

    res = {"ok": bool(ok), "mode": mode, "rasters": len(cogs),
           "sources": names, "area_name": area_name}
    if ok:
        res["cog"] = str(to_cog(out, mosaic_dir / "mosaic_cog.tif"))
        # The plain clipped GeoTIFF is the downloadable deliverable.
        res["deliverable"] = str(out)
    return res


_DISPATCH = {
    "mosaic": _run_mosaic,
    "tracks": _run_tracks,
    "combine": _run_combine,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m server.run_job <job_id>", file=sys.stderr)
        return 2
    job_id = argv[1]
    job = db.get_job(job_id)
    if job is None:
        print(f"no such job: {job_id}", file=sys.stderr)
        return 2

    prog = _Throttle(job_id)
    try:
        result = _DISPATCH[job["kind"]](job, prog)
        db.finish_job(job_id, result=result)
        return 0
    except Exception as e:
        db.finish_job(job_id, error=f"{e}\n{traceback.format_exc()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
