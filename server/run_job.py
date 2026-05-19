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
from .settings import RUNS_DIR, MOSAICS_DIR, TRACKS_DIR


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


def _run_mosaic_tracks(job: dict, prog: _Throttle) -> dict:
    from garmin_core import areas

    p = job["params"]
    mosaic_dir = MOSAICS_DIR / job["id"]
    out = mosaic_dir / "mosaic.tif"

    clip = None
    if p.get("clip_polygon_path"):
        clip = areas.first_polygon(p["clip_polygon_path"])
        selected = areas.tracks_intersecting_polygon(p["tracks_geojson"], clip)
        rsd_paths = [t["rsd_file"] for t in selected]
    else:
        rsd_paths = [Path(x) for x in p["rsd_paths"]]

    def cb(stage, n, total):
        prog(stage, n, total)

    result = areas.mosaic_tracks(
        rsd_paths, out, clip_polygon=clip,
        raster_name=p.get("raster_name", "intensity.tif"), progress_cb=cb,
    )
    if result.get("ok"):
        result["cog"] = str(to_cog(out, mosaic_dir / "mosaic_cog.tif"))
    return result


_DISPATCH = {
    "mosaic": _run_mosaic,
    "tracks": _run_tracks,
    "mosaic_tracks": _run_mosaic_tracks,
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
