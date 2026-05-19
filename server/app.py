"""FastAPI app: auth, job submission/status, SSE progress, COG tiles, static UI.

Wiring only — the real work lives in garmin_core (pipeline) and server.jobs
(serial worker). TiTiler is mounted in-process so tiles share this app/port.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db, jobs
from .auth import AuthDep, is_authed, login, logout, password_ok
from .settings import (
    MOSAICS_DIR,
    RUNS_DIR,
    SECRET_KEY,
    SHARED_PASSWORD,
    TRACKS_DIR,
)

app = FastAPI(title="Garmin Sidescan GUI")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, https_only=False)


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
