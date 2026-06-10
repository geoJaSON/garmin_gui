"""Runtime settings and the on-disk data layout.

Everything the server persists lives under DATA_DIR so the VPS's 400 GB disk
is the single thing to monitor/retain. Paths are created on import.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

# --- core config (env-driven; safe local defaults) -----------------------
DATA_DIR = Path(os.environ.get("GARMIN_GUI_DATA_DIR", "./data")).expanduser().resolve()

# Shared password for the whole app. If unset, a random one is generated and
# printed once at startup (dev convenience; set it explicitly in production).
SHARED_PASSWORD = os.environ.get("GARMIN_GUI_PASSWORD") or ""

# Session cookie signing key. Set in production so logins survive restarts.
SECRET_FROM_ENV = bool(os.environ.get("GARMIN_GUI_SECRET"))
SECRET_KEY = os.environ.get("GARMIN_GUI_SECRET") or secrets.token_urlsafe(32)

# Python used to launch isolated job subprocesses (defaults to this venv).
JOB_PYTHON = os.environ.get("GARMIN_GUI_PYTHON", "")

# --- disk layout ---------------------------------------------------------
DB_PATH = DATA_DIR / "garmin_gui.sqlite"
RSD_DIR = DATA_DIR / "rsd"            # registered/uploaded .RSD files
RUNS_DIR = DATA_DIR / "runs"          # runs/<run_id>/processed + cog
TRACKS_DIR = DATA_DIR / "tracks"      # rsd_tracks.geojson inventory
POLYGONS_DIR = DATA_DIR / "polygons"  # uploaded selection polygons
MOSAICS_DIR = DATA_DIR / "mosaics"    # mosaics/<mosaic_id>/out.tif + cog

_ALL_DIRS = [DATA_DIR, RSD_DIR, RUNS_DIR, TRACKS_DIR, POLYGONS_DIR, MOSAICS_DIR]


def ensure_layout() -> None:
    for d in _ALL_DIRS:
        d.mkdir(parents=True, exist_ok=True)


ensure_layout()
