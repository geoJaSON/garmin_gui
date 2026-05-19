"""FastAPI app: auth, job submission/status, SSE progress, COG tiles, static UI.

Wiring only — the real work lives in garmin_core (pipeline) and server.jobs
(serial worker). TiTiler is mounted in-process so tiles share this app/port.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from garmin_core.config import MosaicConfig
from . import db, jobs
from .auth import AuthDep, is_authed, login, logout, password_ok
from .settings import (
    MOSAICS_DIR,
    RSD_DIR,
    RUNS_DIR,
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
    jobs.start_worker()
    if not SHARED_PASSWORD:
        print("!! GARMIN_GUI_PASSWORD is unset — the app is OPEN. Set it in prod.")


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
    job_id = jobs.enqueue("mosaic", {
        "rsd_path": body["rsd_path"],
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


@app.post("/api/jobs/mosaic_tracks", dependencies=[AuthDep])
async def submit_mosaic_tracks(request: Request):
    body = await request.json()
    params = {
        "rsd_paths": body.get("rsd_paths"),
        "tracks_geojson": body.get("tracks_geojson"),
        "clip_polygon_path": body.get("clip_polygon_path"),
        "raster_name": body.get("raster_name", "intensity.tif"),
    }
    if not params["rsd_paths"] and not (params["tracks_geojson"] and params["clip_polygon_path"]):
        raise HTTPException(400, "need rsd_paths, or tracks_geojson + clip_polygon_path")
    job_id = jobs.enqueue("mosaic_tracks", params)
    return {"job_id": job_id}


# ---- job status ---------------------------------------------------------
@app.get("/api/jobs", dependencies=[AuthDep])
async def api_jobs():
    return jobs and db.list_jobs()


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


# ---- result discovery ---------------------------------------------------
@app.get("/api/tracks", dependencies=[AuthDep])
async def api_tracks():
    """The track inventory GeoJSON (empty FeatureCollection if none yet)."""
    gj = TRACKS_DIR / "rsd_tracks.geojson"
    if not gj.exists():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(gj.read_text())


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
    (TRACKS_DIR / "rsd_tracks.geojson").write_bytes(raw)
    return {"ok": True, "features": len(payload.get("features", []))}


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
    import shutil

    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(404, "no such run")
    rd = RUNS_DIR / job_id
    if rd.exists():
        shutil.rmtree(rd, ignore_errors=True)
    with db.connect() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"ok": True}


@app.delete("/api/rsd/{name}", dependencies=[AuthDep])
async def api_delete_rsd(name: str):
    target = (RSD_DIR / Path(name).name)
    if not target.exists():
        raise HTTPException(404, "no such RSD")
    target.unlink()
    return {"ok": True}


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


# ---- COG tiles (TiTiler mounted in-process) -----------------------------
def _mount_titiler() -> None:
    try:
        from titiler.core.factory import TilerFactory
    except Exception as e:  # keep app importable without titiler installed
        print(f"!! TiTiler not available, /tiles disabled: {e}")
        return
    tiler = TilerFactory()
    app.include_router(tiler.router, prefix="/tiles", tags=["tiles"])


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
