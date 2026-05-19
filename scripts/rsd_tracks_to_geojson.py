#!/usr/bin/env python3
"""
CLI wrapper for the RSD track-inventory builder, backed by garmin_core.tracks.

Usage:
  python scripts/rsd_tracks_to_geojson.py <RSD_FOLDER> [OUTPUT.geojson]

The legacy script hardcoded INPUT_FOLDER; this accepts it as an argument
(matching the README's documented usage) or the GARMIN_RSD_FOLDER env var.
Behavior for a given folder is faithful to the original.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from garmin_core.config import TracksConfig
from garmin_core.tracks import build_track_inventory


def main() -> int:
    folder = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GARMIN_RSD_FOLDER")
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if not folder:
        print("Usage: rsd_tracks_to_geojson.py <RSD_FOLDER> [OUTPUT.geojson]")
        print("   or set GARMIN_RSD_FOLDER")
        return 2
    try:
        build_track_inventory(folder, out, TracksConfig())
    except ValueError as e:
        print(e)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
