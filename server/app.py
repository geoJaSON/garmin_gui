"""FastAPI app: auth, job submission/status, SSE progress, COG tiles, static UI.

Wiring only — the real work lives in garmin_core (pipeline) and server.jobs
(serial worker). TiTiler is mounted in-process so tiles share this app/port.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from garmin_core.config import MosaicConfig
from garmin_core.locking import locked
from . import db, jobs
from .auth import AuthDep, is_authed, login, logout, password_ok
from .settings import (
    MOSAICS_DIR,
    POLYGONS_DIR,
    RSD_DIR,
    RUNS_DIR,
    SECRET_FROM_ENV,
    SECRET_KEY,
    SHARED_PASSWORD,
    TRACKS_DIR,
)

app = FastAPI(title="Garmin Sidescan GUI")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)


@app.middleware("http")
async def _no_store_api(request: Request, call_next):
    """Stop the browser caching API responses.

    The SPA calls GET /api/me before and after login with an identical
    URL+method; without this the browser replays the cached pre-login
    {"authed":false} and the user is stuck on the password screen even
    though login succeeded. Tiles are intentionally left cacheable.
    """
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    elif path.startswith("/tiles"):
        pass  # COG tiles are immutable per run — let them cache
    else:
        # SPA assets: always revalidate so a deploy can't leave a client
        # running stale JS (etag makes this a cheap 304 when unchanged).
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    _migrate_legacy_layer_file()
    jobs.start_worker()
    if not SHARED_PASSWORD:
        print("!! GARMIN_GUI_PASSWORD is unset — the app is OPEN. Set it in prod.")
    if SHARED_PASSWORD and not SECRET_FROM_ENV:
        print("!! GARMIN_GUI_SECRET is unset — session cookies rotate on every "
              "restart, logging everyone out. Set it in prod.")


def _migrate_legacy_layer_file() -> None:
    """One-shot: if the areas table is empty and the Phase 5 layer file
    exists on disk, ingest its features so prior uploads aren't lost."""
    if db.list_areas():
        return
    legacy = POLYGONS_DIR / "layer_areas.geojson"
    if not legacy.exists():
        return
    try:
        payload = json.loads(legacy.read_text())
    except Exception:
        return
    n = 0
    for feat in payload.get("features", []):
        props = feat.get("properties") or {}
        on = props.get("Our_Name")
        no = props.get("TPWD_App_No")
        geom = feat.get("geometry")
        if not (on and no and geom):
            continue
        db.upsert_area(str(on), str(no), props, geom)
        n += 1
    if n:
        print(f"migrated {n} area(s) from legacy layer_areas.geojson")


@app.on_event("shutdown")
def _shutdown() -> None:
    jobs.stop_worker()


# ---- auth ---------------------------------------------------------------
@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    if not password_ok(body.get("password", "")):
        raise HTTPException(401, "Wrong password")
    login(request)
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(request: Request):
    logout(request)
    return {"ok": True}


@app.get("/api/me")
async def api_me(request: Request):
    return {"authed": is_authed(request)}


# ---- RSD files ----------------------------------------------------------
@app.get("/api/rsd", dependencies=[AuthDep])
async def api_rsd_list():
    """RSD files available on the server (uploaded into the data volume)."""
    out = []
    for p in sorted(RSD_DIR.glob("*")):
        if p.is_file() and p.suffix.lower() == ".rsd":
            out.append({"name": p.name, "path": str(p), "size": p.stat().st_size})
    return out


@app.post("/api/rsd", dependencies=[AuthDep])
async def api_rsd_upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".rsd"):
        raise HTTPException(400, "expected a .RSD file")
    # Keep the original name; reject path traversal.
    name = Path(file.filename).name
    dest = RSD_DIR / name
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            fh.write(chunk)
    return {"name": name, "path": str(dest), "size": dest.stat().st_size}


# ---- MosaicConfig schema (drives the tuning form) -----------------------
_CFG_TYPES = {bool: "bool", int: "int", float: "float", str: "str"}


@app.get("/api/config/mosaic", dependencies=[AuthDep])
async def api_mosaic_config():
    """Scalar MosaicConfig fields + defaults so the UI can build a form.

    List-valued fields (payload mode tables) are intentionally omitted —
    they're advanced and not form-friendly; omitting them means the run
    uses their faithful defaults.
    """
    defaults = MosaicConfig()
    fields = []
    for f in dataclasses.fields(MosaicConfig):
        val = getattr(defaults, f.name)
        if isinstance(val, list) or isinstance(val, set):
            continue
        # Optional[...] fields default to None; treat as free text.
        if val is None:
            kind = "optional"
        elif isinstance(val, bool):
            kind = "bool"
        else:
            kind = _CFG_TYPES.get(type(val), "str")
        fields.append({"name": f.name, "type": kind, "default": val})
    return {"fields": fields}


# ---- job submission -----------------------------------------------------
@app.post("/api/jobs/mosaic", dependencies=[AuthDep])
async def submit_mosaic(request: Request):
    body = await request.json()
    if not body.get("rsd_path"):
        raise HTTPException(400, "rsd_path required")
    # Client-supplied path that the worker will stage and process — accept
    # only uploaded RSDs, not arbitrary server paths.
    rsd = Path(str(body["rsd_path"])).expanduser().resolve()
    if not (rsd.suffix.lower() == ".rsd" and rsd.is_file()
            and rsd.is_relative_to(RSD_DIR)):
        raise HTTPException(400, "rsd_path must be an uploaded .RSD file")
    job_id = jobs.enqueue("mosaic", {
        "rsd_path": str(rsd),
        "config": body.get("config") or {},
    })
    return {"job_id": job_id}


@app.post("/api/jobs/tracks", dependencies=[AuthDep])
async def submit_tracks(request: Request):
    body = await request.json()
    if not body.get("input_folder"):
        raise HTTPException(400, "input_folder required")
    job_id = jobs.enqueue("tracks", {
        "input_folder": body["input_folder"],
        "output_path": body.get("output_path") or str(TRACKS_DIR / "rsd_tracks.geojson"),
        "config": body.get("config") or {},
    })
    return {"job_id": job_id}


@app.post("/api/jobs/combine", dependencies=[AuthDep])
async def submit_combine(request: Request):
    """W2/W3: combine existing run COGs into one mosaic.

    body: {run_ids:[...]}                     -> merge those runs (W2)
          {polygon: <GeoJSON geometry/FC>}    -> merge+clip runs whose track
                                                 intersects the polygon (W3)
    """
    body = await request.json()
    run_ids = body.get("run_ids")
    polygon = body.get("polygon")
    area = body.get("area")  # {"Our_Name":..,"TPWD_App_No":..} -> deliverable
    if not run_ids and not polygon and not area:
        raise HTTPException(400, "need run_ids, polygon, or area")
    job_id = jobs.enqueue(
        "combine", {"run_ids": run_ids, "polygon": polygon, "area": area,
                    "blend": body.get("blend")}
    )
    return {"job_id": job_id}


# ---- areas (Phase 6: data-mgmt) -----------------------------------------
DEFAULT_BUFFER_M = 30.0


def _area_summary(a: dict) -> dict:
    """Compact row for the table UI."""
    cog = None
    if a.get("mosaic_job_id"):
        j = db.get_job(a["mosaic_job_id"])
        if j and (j.get("result") or {}).get("cog"):
            cog = j["result"]["cog"] if Path(j["result"]["cog"]).exists() else None
    return {
        "id": a["id"],
        "our_name": a["our_name"],
        "tpwd_app_no": a["tpwd_app_no"],
        "notes": a["notes"],
        "mosaic_job_id": a["mosaic_job_id"] if cog else None,
        "has_mosaic": bool(cog),
        "updated_at": a["updated_at"],
    }


def _ci_get(d: dict, name: str):
    """Case-insensitive property lookup."""
    if not isinstance(d, dict):
        return None
    target = name.lower()
    for k, v in d.items():
        if str(k).lower() == target:
            return v
    return None


def _norm(v):
    """Treat None/empty/whitespace as missing; everything else stringifies."""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _track_is_ignored(feature: dict) -> bool:
    return bool((feature.get("properties") or {}).get("mosaic_ignore"))


@app.post("/api/areas/upload", dependencies=[AuthDep])
async def api_areas_upload(file: UploadFile = File(...)):
    """Upsert polygons from a GeoJSON FeatureCollection.

    Features matched case-insensitively on (Our_Name, TPWD_App_No). Notes
    and the linked mosaic_job_id are preserved across re-uploads.
    """
    raw = await file.read()
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "not valid JSON")
    if payload.get("type") != "FeatureCollection":
        raise HTTPException(400, "expected a GeoJSON FeatureCollection")
    added = updated = skipped = 0
    seen_ids = set()
    skip_reasons: list[str] = []
    sample_keys: set[str] = set()
    for i, feat in enumerate(payload.get("features", [])):
        props = feat.get("properties") or {}
        if i < 5:
            sample_keys.update(map(str, props.keys()))
        on = _norm(_ci_get(props, "Our_Name"))
        # Real data uses TPWD_App_No; accept the old typo TPDW_App_No too.
        no = _norm(_ci_get(props, "TPWD_App_No")
                   or _ci_get(props, "TPDW_App_No"))
        geom = feat.get("geometry")
        miss = []
        if not on:
            miss.append("Our_Name")
        if not no:
            miss.append("TPWD_App_No")
        if not geom:
            miss.append("geometry")
        if miss:
            skipped += 1
            if len(skip_reasons) < 5:
                skip_reasons.append(f"feature #{i}: missing " + ", ".join(miss))
            continue
        existing = db.get_area_by_key(on, no)
        area_id = db.upsert_area(on, no, props, geom)
        seen_ids.add(area_id)
        if existing:
            updated += 1
        else:
            added += 1
    return {
        "ok": True, "added": added, "updated": updated,
        "skipped": skipped, "total": len(seen_ids),
        "skipped_reasons": skip_reasons,
        "sample_property_keys": sorted(sample_keys),
    }


@app.get("/api/areas", dependencies=[AuthDep])
async def api_areas_list():
    return [_area_summary(a) for a in db.list_areas()]


@app.get("/api/areas.geojson", dependencies=[AuthDep])
async def api_areas_geojson():
    """FeatureCollection for the map. Each feature carries the row id."""
    attention_terms = ("redo", "re-do", "rework", "rescan", "re-scan", "revisit")
    feats = []
    for a in db.list_areas():
        summary = _area_summary(a)
        notes = a.get("notes") or ""
        needs_attention = any(term in notes.lower() for term in attention_terms)
        feats.append({
            "type": "Feature",
            "properties": {
                "id": a["id"],
                "Our_Name": a["our_name"],
                "TPWD_App_No": a["tpwd_app_no"],
                "mosaic_job_id": summary["mosaic_job_id"],
                "has_mosaic": summary["has_mosaic"],
                "notes": notes,
                "needs_attention": needs_attention,
            },
            "geometry": a["geometry"],
        })
    return {"type": "FeatureCollection", "features": feats}


@app.get("/api/areas-buffered.geojson", dependencies=[AuthDep])
async def api_areas_buffered_geojson(buffer_ft: float = 200.0):
    """Each area buffered by `buffer_ft` (default 200) for a visual hint
    of the clip boundary the deliverable will use. Buffer is computed in
    projected meters (same path as the actual deliverable clip)."""
    from shapely.geometry import mapping
    from .geo import buffer_wgs84

    buffer_m = float(buffer_ft) * 0.3048
    feats = []
    for a in db.list_areas():
        if not a.get("geometry"):
            continue
        try:
            buffered = buffer_wgs84(a["geometry"], buffer_m)
        except Exception:
            continue
        feats.append({
            "type": "Feature",
            "properties": {
                "id": a["id"],
                "Our_Name": a["our_name"],
                "buffer_ft": buffer_ft,
            },
            "geometry": mapping(buffered),
        })
    return {"type": "FeatureCollection", "features": feats}


@app.get("/api/areas/{area_id}", dependencies=[AuthDep])
async def api_area_get(area_id: str):
    a = db.get_area(area_id)
    if not a:
        raise HTTPException(404, "no such area")
    return a


@app.patch("/api/areas/{area_id}", dependencies=[AuthDep])
async def api_area_patch(area_id: str, request: Request):
    body = await request.json()
    if "notes" in body:
        if not db.update_area_notes(area_id, str(body["notes"])):
            raise HTTPException(404, "no such area")
    return db.get_area(area_id)


@app.delete("/api/areas/{area_id}", dependencies=[AuthDep])
async def api_area_delete(area_id: str):
    if not db.delete_area(area_id):
        raise HTTPException(404, "no such area")
    return {"ok": True}


@app.get("/api/areas/{area_id}/coverage", dependencies=[AuthDep])
async def api_area_coverage(area_id: str, buffer_m: float = DEFAULT_BUFFER_M):
    """Tracks intersecting this area's polygon buffered by buffer_m meters."""
    from .geo import buffer_wgs84
    from shapely.geometry import shape
    from shapely.prepared import prep

    a = db.get_area(area_id)
    if not a or not a.get("geometry"):
        raise HTTPException(404, "no such area")
    clip = prep(buffer_wgs84(a["geometry"], buffer_m))
    inv_fc = _load_inventory()
    tracks, with_mosaic = [], []
    for f in inv_fc.get("features", []):
        if _track_is_ignored(f):
            continue
        g = f.get("geometry")
        if not g:
            continue
        geom = shape(g)
        if geom.is_empty or not clip.intersects(geom):
            continue
        fn = (f.get("properties") or {}).get("file_name")
        if not fn:
            continue
        tracks.append(fn)
        if db.find_done_mosaic_by_rsd(fn):
            with_mosaic.append(fn)
    return {"tracks": tracks, "with_mosaic": with_mosaic,
            "total": len(tracks), "buffer_m": buffer_m}


@app.post("/api/areas/{area_id}/mosaic", dependencies=[AuthDep])
async def api_area_mosaic(area_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    buffer_m = float(body.get("buffer_m") or DEFAULT_BUFFER_M)
    if not db.get_area(area_id):
        raise HTTPException(404, "no such area")
    job_id = jobs.enqueue("combine",
                           {"area_id": area_id, "buffer_m": buffer_m,
                            "blend": body.get("blend")})
    return {"job_id": job_id, "buffer_m": buffer_m}


@app.get("/api/deliverable/{job_id}", dependencies=[AuthDep])
async def api_deliverable(job_id: str):
    """Download the clipped per-area GeoTIFF deliverable for a combine job."""
    from fastapi.responses import FileResponse

    job = db.get_job(job_id)
    if not job or not job.get("result"):
        raise HTTPException(404, "no such combine job")
    res = job["result"]
    tif = res.get("deliverable")
    if not tif or not Path(tif).exists():
        raise HTTPException(404, "no deliverable file")
    label = res.get("area_name") or "mosaic"
    return FileResponse(
        tif, media_type="image/tiff",
        filename=f"{label}_intensity_clipped.tif",
    )


@app.get("/api/deliverable/{job_id}/metadata.txt", dependencies=[AuthDep])
async def api_deliverable_metadata(job_id: str):
    """Download the per-area survey metadata .txt that accompanies the COG."""
    from fastapi.responses import FileResponse

    job = db.get_job(job_id)
    if not job or not job.get("result"):
        raise HTTPException(404, "no such combine job")
    txt = (job["result"] or {}).get("metadata_txt")
    if not txt or not Path(txt).exists():
        raise HTTPException(404, "no metadata.txt for this deliverable")
    label = (job["result"] or {}).get("area_name") or "mosaic"
    return FileResponse(
        txt, media_type="text/plain",
        filename=f"{label}_metadata.txt",
    )


# ---- job status ---------------------------------------------------------
@app.get("/api/jobs", dependencies=[AuthDep])
async def api_jobs():
    return db.list_jobs()


@app.get("/api/jobs/{job_id}", dependencies=[AuthDep])
async def api_job(job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job


@app.get("/api/jobs/{job_id}/events", dependencies=[AuthDep])
async def api_job_events(job_id: str):
    """SSE: emit job state ~1/s until it reaches a terminal status."""
    if db.get_job(job_id) is None:
        raise HTTPException(404, "no such job")

    async def gen():
        while True:
            job = db.get_job(job_id)
            yield f"data: {json.dumps(job)}\n\n"
            if job is None or job["status"] in ("done", "error", "cancelled"):
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---- track inventory helpers ---------------------------------------------
# The inventory is one shared geojson mutated by several handlers AND by job
# subprocesses (post-mosaic track append). Every read-modify-write cycle must
# run inside `with locked(_INV)`, and all writes go through _save_inventory:
# temp-file + os.replace so a crash can't truncate the file mid-write.
_INV = TRACKS_DIR / "rsd_tracks.geojson"


def _load_inventory() -> dict:
    if not _INV.exists():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(_INV.read_text())


def _save_inventory(fc: dict) -> None:
    tmp = _INV.with_suffix(_INV.suffix + ".tmp")
    tmp.write_text(json.dumps(fc))
    tmp.replace(_INV)


# ---- result discovery ---------------------------------------------------
@app.get("/api/tracks/{file_name}/metadata", dependencies=[AuthDep])
async def api_track_metadata(file_name: str):
    """Per-RSD survey metadata + weather for the track click panel.

    Priority:
      1. The mosaic job's stored result (has meta+weather+survey_datetime
         after import_runs+backfill, even when no CSVs are on disk).
      2. The on-disk pingverter meta dir (run dir, then RSD folder) —
         a fallback for runs that pre-date Phase 7 and haven't been
         backfilled.
    """
    from .track_metadata import summarize_meta_dir

    stem = Path(file_name).stem
    run = db.find_done_mosaic_by_rsd(file_name)
    if run:
        res = run.get("result") or {}
        job_meta = res.get("meta")
        weather = res.get("weather")
        survey_dt = res.get("survey_datetime")
        if job_meta or weather:
            out = dict(job_meta or {})
            if weather:
                out["weather"] = weather
            if survey_dt:
                out["survey_datetime"] = survey_dt
            out["source"] = "job"
            return out

    sources = []
    if run:
        sources.append(
            (RUNS_DIR / run["id"] / f"garmin_output_{stem}" / "meta", "run")
        )
    sources.append((RSD_DIR / f"garmin_output_{stem}" / "meta", "tracks"))
    for meta_dir, kind in sources:
        s = summarize_meta_dir(meta_dir)
        if s:
            s["source"] = kind
            return s
    raise HTTPException(404, "no metadata available for this RSD")


@app.get("/api/tracks/{file_name}/footprint", dependencies=[AuthDep])
async def api_track_footprint(file_name: str,
                              port_m: float = Query(None, gt=0, le=500),
                              star_m: float = Query(None, gt=0, le=500)):
    """Predicted swath footprint: port/starboard polygons around the track.

    A geometric preview (GPS line buffered by the per-side range) for lane
    planning before re-running a mosaic — NOT true ensonified coverage:
    the real mosaic loses a depth-dependent strip at nadir, and slant
    correction shortens the far edge over deep water.

    When a side's range isn't supplied, falls back to the run's recorded
    mean range from the metadata snapshot, then to 15 m. Each feature's
    properties say which source was used.
    """
    from shapely.geometry import mapping
    from .geo import swath_wgs84

    feature = next(
        (f for f in _load_inventory().get("features", [])
         if (f.get("properties") or {}).get("file_name") == file_name
         and f.get("geometry")),
        None,
    )
    if feature is None:
        raise HTTPException(404, "no track in inventory for this RSD")

    src = {"port": "requested", "star": "requested"}
    if port_m is None or star_m is None:
        run = db.find_done_mosaic_by_rsd(file_name)
        rng = (((run or {}).get("result") or {}).get("meta") or {}).get("range_m") or {}
        try:
            recorded = float(rng["mean"]) if rng.get("mean") is not None else None
        except (TypeError, ValueError):
            recorded = None
        if port_m is None:
            port_m, src["port"] = (recorded, "recorded") if recorded else (15.0, "default")
        if star_m is None:
            star_m, src["star"] = (recorded, "recorded") if recorded else (15.0, "default")

    port_geom, star_geom = swath_wgs84(feature["geometry"],
                                       float(port_m), float(star_m))
    feats = []
    for side, geom, rng_m in (("port", port_geom, port_m),
                              ("star", star_geom, star_m)):
        if geom is None or geom.is_empty:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"file_name": file_name, "side": side,
                           "range_m": round(float(rng_m), 2),
                           "range_source": src[side]},
            "geometry": mapping(geom),
        })
    return {"type": "FeatureCollection", "features": feats}


@app.get("/api/tracks", dependencies=[AuthDep])
async def api_tracks():
    """The track inventory GeoJSON (empty FeatureCollection if none yet)."""
    return _load_inventory()


@app.post("/api/tracks", dependencies=[AuthDep])
async def api_tracks_upload(file: UploadFile = File(...)):
    """Replace the server track inventory with an uploaded rsd_tracks.geojson."""
    raw = await file.read()
    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(400, "not valid JSON")
    if payload.get("type") != "FeatureCollection":
        raise HTTPException(400, "expected a GeoJSON FeatureCollection")
    tmp = _INV.with_suffix(_INV.suffix + ".tmp")
    with locked(_INV):
        tmp.write_bytes(raw)
        tmp.replace(_INV)
    return {"ok": True, "features": len(payload.get("features", []))}


@app.post("/api/tracks/mosaic_ignore", dependencies=[AuthDep])
async def api_tracks_bulk_ignore(request: Request):
    """Set mosaic_ignore on many tracks in one write (batch ignore/include).

    Body: {"file_names": [...], "mosaic_ignore": true|false}. Lets the UI
    flip a whole selection at once instead of one PATCH per track (each of
    which would rewrite the entire inventory file)."""
    body = await request.json()
    names = body.get("file_names")
    if not isinstance(names, list) or not names:
        raise HTTPException(400, "file_names must be a non-empty list")
    ignore = bool(body.get("mosaic_ignore"))
    name_set = {str(n) for n in names}
    if not _INV.exists():
        raise HTTPException(404, "no inventory on disk")
    changed = 0
    matched: set[str] = set()
    with locked(_INV):
        fc = _load_inventory()
        for f in fc.get("features", []):
            props = f.setdefault("properties", {})
            if props.get("file_name") in name_set:
                props["mosaic_ignore"] = ignore
                changed += 1
                matched.add(props.get("file_name"))
        if not changed:
            raise HTTPException(404, "no matching tracks in inventory")
        _save_inventory(fc)
    return {"ok": True, "updated": changed,
            "files": sorted(matched), "mosaic_ignore": ignore}


@app.patch("/api/tracks/{file_name}", dependencies=[AuthDep])
async def api_track_patch(file_name: str, request: Request):
    """Update per-track inventory flags without touching runs or RSD files."""
    body = await request.json()
    if not _INV.exists():
        raise HTTPException(404, "no inventory on disk")
    changed = 0
    with locked(_INV):
        fc = _load_inventory()
        for f in fc.get("features", []):
            props = f.setdefault("properties", {})
            if props.get("file_name") != file_name:
                continue
            if "mosaic_ignore" in body:
                props["mosaic_ignore"] = bool(body["mosaic_ignore"])
                changed += 1
        if not changed:
            raise HTTPException(404, "no such track in inventory")
        _save_inventory(fc)
    return {"ok": True, "updated": changed,
            "mosaic_ignore": bool(body.get("mosaic_ignore"))}


# ---- storage / data management ------------------------------------------
def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


@app.get("/api/storage", dependencies=[AuthDep])
async def api_storage():
    import shutil

    du = shutil.disk_usage(RUNS_DIR)
    rsds = [
        {"name": p.name, "size": p.stat().st_size}
        for p in sorted(RSD_DIR.glob("*"))
        if p.is_file() and p.suffix.lower() == ".rsd"
    ]
    runs = []
    for j in db.list_jobs(100000):
        if j["kind"] == "mosaic" and j["status"] == "done" and j.get("result"):
            rd = RUNS_DIR / j["id"]
            runs.append({
                "job_id": j["id"],
                "rsd_name": j["result"].get("rsd_name"),
                "imported": bool(j["result"].get("imported")),
                "size": _dir_size(rd) if rd.exists() else 0,
            })
    return {
        "disk": {"total": du.total, "used": du.used, "free": du.free},
        "rsd": {"count": len(rsds), "bytes": sum(r["size"] for r in rsds), "items": rsds},
        "runs": {"count": len(runs), "bytes": sum(r["size"] for r in runs), "items": runs},
    }


@app.delete("/api/runs/{job_id}", dependencies=[AuthDep])
async def api_delete_run(job_id: str):
    """Delete any job + on-disk artifacts: mosaic run dir AND combine
    mosaic dir (depending on kind). Used by the Queue panel as a single
    'cancel/remove this job' affordance for any kind."""
    import shutil

    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such run")
    for d in (RUNS_DIR / job_id, MOSAICS_DIR / job_id):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    with db.connect() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


@app.delete("/api/rsd/{name}", dependencies=[AuthDep])
async def api_delete_rsd(name: str):
    """Cascade delete: RSD file + track inventory entry + every mosaic run
    (and its run dir) keyed to this RSD. 404 only if nothing matched."""
    import shutil

    name = Path(name).name   # path-traversal guard
    removed = {"rsd_file": False, "track_features": 0, "mosaic_runs": 0}

    rsd_file = RSD_DIR / name
    if rsd_file.exists():
        rsd_file.unlink()
        removed["rsd_file"] = True

    if _INV.exists():
        with locked(_INV):
            fc = _load_inventory()
            before = len(fc.get("features", []))
            fc["features"] = [
                f for f in fc.get("features", [])
                if (f.get("properties") or {}).get("file_name") != name
            ]
            removed["track_features"] = before - len(fc["features"])
            _save_inventory(fc)

    for j in db.find_mosaic_jobs_by_rsd(name):
        rd = RUNS_DIR / j["id"]
        if rd.exists():
            shutil.rmtree(rd, ignore_errors=True)
        with db.connect() as c:
            c.execute("DELETE FROM jobs WHERE id=?", (j["id"],))
        removed["mosaic_runs"] += 1

    if not (removed["rsd_file"] or removed["track_features"]
            or removed["mosaic_runs"]):
        raise HTTPException(404, "nothing to delete for that name")
    return {"ok": True, **removed}


@app.delete("/api/tracks/{file_name}", dependencies=[AuthDep])
async def api_delete_track(file_name: str):
    """Remove ALL inventory entries for this file_name (no run/RSD touch)."""
    if not _INV.exists():
        raise HTTPException(404, "no inventory on disk")
    with locked(_INV):
        fc = _load_inventory()
        before = len(fc.get("features", []))
        fc["features"] = [
            f for f in fc.get("features", [])
            if (f.get("properties") or {}).get("file_name") != file_name
        ]
        removed = before - len(fc["features"])
        if not removed:
            raise HTTPException(404, "no such track in inventory")
        _save_inventory(fc)
    return {"ok": True, "removed": removed}


@app.delete("/api/tracks/{file_name}/{index}", dependencies=[AuthDep])
async def api_delete_track_at(file_name: str, index: int):
    """Remove ONE inventory entry by 0-based occurrence index among
    features with this file_name. Use this to delete a single duplicate."""
    if not _INV.exists():
        raise HTTPException(404, "no inventory on disk")
    with locked(_INV):
        fc = _load_inventory()
        feats = fc.get("features", [])
        occurrence = -1
        target = -1
        for i, f in enumerate(feats):
            if (f.get("properties") or {}).get("file_name") == file_name:
                occurrence += 1
                if occurrence == index:
                    target = i
                    break
        if target < 0:
            raise HTTPException(404, "no such occurrence")
        feats.pop(target)
        fc["features"] = feats
        _save_inventory(fc)
    return {"ok": True, "removed": 1, "remaining": occurrence}


@app.delete("/api/rsd/{name}/file", dependencies=[AuthDep])
async def api_delete_rsd_file_only(name: str):
    """Delete just the uploaded RSD file. Inventory and runs are untouched
    (useful for reclaiming disk while keeping mosaic outputs)."""
    target = RSD_DIR / Path(name).name
    if not target.exists():
        raise HTTPException(404, "no such RSD file")
    size = target.stat().st_size
    target.unlink()
    return {"ok": True, "freed_bytes": size}


# ---- unified "Files" view (for the manage page) -------------------------
def _dir_size_safe(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


@app.get("/api/files", dependencies=[AuthDep])
async def api_files():
    """Everything keyed by RSD name: file presence + tracks + mosaic runs.

    One row per unique name. Useful for spotting duplicates and surgically
    cleaning them up via the granular delete endpoints.
    """
    # 1) on-disk RSDs
    files = {}
    for p in RSD_DIR.glob("*"):
        if p.is_file() and p.suffix.lower() == ".rsd":
            files[p.name] = {"present": True, "size": p.stat().st_size}

    # 2) inventory tracks (keep insertion order so 'index' is meaningful)
    inv_fc = _load_inventory()
    track_groups: dict[str, list] = {}
    for f in inv_fc.get("features", []):
        pr = f.get("properties") or {}
        name = pr.get("file_name")
        if not name:
            continue
        grp = track_groups.setdefault(name, [])
        grp.append({
            "index": len(grp),
            "point_count": pr.get("track_points") or pr.get("point_count"),
            "metadata_source": (pr.get("metadata_source")
                                or pr.get("source_meta")),
        })

    # 3) mosaic runs grouped by RSD
    run_groups: dict[str, list] = {}
    for j in db.list_jobs(100000):
        if j["kind"] != "mosaic":
            continue
        res = j.get("result") or {}
        params = j.get("params") or {}
        name = (res.get("rsd_name")
                or (params.get("rsd_path") or "").rsplit("/", 1)[-1])
        if not name:
            continue
        rd = RUNS_DIR / j["id"]
        run_groups.setdefault(name, []).append({
            "job_id": j["id"],
            "status": j["status"],
            "finished_at": j.get("finished_at"),
            "imported": bool(res.get("imported")),
            "has_cog": bool(res.get("cog")
                            and Path(res["cog"]).exists()),
            "disk_size": _dir_size_safe(rd),
        })

    # Union of names across the three sources
    names = sorted(set(files) | set(track_groups) | set(run_groups))
    rows = []
    for n in names:
        tracks = track_groups.get(n) or []
        runs = run_groups.get(n) or []
        rows.append({
            "file_name": n,
            "rsd_file": files.get(n) or {"present": False, "size": 0},
            "tracks": tracks,
            "track_count": len(tracks),
            "runs": runs,
            "run_count": len(runs),
            "duplicate": len(tracks) > 1 or len(runs) > 1,
            "disk_bytes": (files.get(n, {}).get("size", 0)
                           + sum(r["disk_size"] for r in runs)),
        })
    return rows


@app.get("/api/runs", dependencies=[AuthDep])
async def api_runs():
    """Completed mosaic runs that produced a COG, keyed for the map browser.

    rsd_name lets the frontend link a track feature to its mosaic.
    """
    out = []
    for j in db.list_jobs(500):
        if j["kind"] == "mosaic" and j["status"] == "done" and j.get("result"):
            cog = j["result"].get("cog")
            if cog and Path(cog).exists():
                rsd = (j.get("params") or {}).get("rsd_path", "")
                out.append({
                    "job_id": j["id"],
                    "cog": cog,
                    "rsd_name": Path(rsd).name if rsd else None,
                    "finished_at": j["finished_at"],
                })
    return out


@app.get("/api/mosaics", dependencies=[AuthDep])
async def api_mosaics():
    """Completed combine (W2/W3) mosaics, for the map browser."""
    out = []
    for j in db.list_jobs(500):
        if j["kind"] == "combine" and j["status"] == "done" and j.get("result"):
            cog = j["result"].get("cog")
            if cog and Path(cog).exists():
                out.append({
                    "job_id": j["id"],
                    "cog": cog,
                    "mode": j["result"].get("mode"),
                    "rasters": j["result"].get("rasters"),
                    "sources": j["result"].get("sources"),
                    "finished_at": j["finished_at"],
                })
    return out


# ---- COG tiles (TiTiler mounted in-process) -----------------------------
def _validated_dataset_path(
    url: str = Query(..., description="COG path under the server data dir"),
) -> str:
    """Restrict TiTiler to local rasters inside our data layout.

    The stock factory passes url= straight to GDAL, which happily opens
    any file on disk or remote /vsicurl/ URLs — a local-file-read/SSRF
    primitive. Only run/mosaic outputs are ever tiled by this app.
    """
    try:
        p = Path(url).expanduser().resolve()
    except (OSError, ValueError):
        raise HTTPException(400, "invalid dataset path")
    for root in (RUNS_DIR, MOSAICS_DIR):
        if p.is_file() and p.is_relative_to(root):
            return str(p)
    raise HTTPException(400, "dataset must be a raster under the data directory")


def _mount_titiler() -> None:
    try:
        from titiler.core.factory import TilerFactory
    except Exception as e:  # keep app importable without titiler installed
        print(f"!! TiTiler not available, /tiles disabled: {e}")
        return
    # router_prefix MUST match the include_router prefix so TiTiler's
    # url_for() builds tilejson tile URLs that actually resolve. Without
    # it, tilejson advertised /tiles/{tms}/{z}/{x}/{y} while the real
    # route was /tiles/tiles/{tms}/{z}/{x}/{y} -> every tile 404'd.
    tiler = TilerFactory(router_prefix="/tiles",
                         path_dependency=_validated_dataset_path)
    app.include_router(tiler.router, prefix="/tiles", tags=["tiles"],
                       dependencies=[AuthDep])


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})


_mount_titiler()

# ---- static frontend ----------------------------------------------------
# MUST be last: a mount at "/" is a catch-all, so all /api, /tiles and
# /healthz routes have to be registered before it.
_WEB = Path(__file__).resolve().parents[1] / "web"
if (_WEB / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_WEB), html=True), name="web")
