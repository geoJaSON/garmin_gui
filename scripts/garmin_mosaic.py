#!/usr/bin/env python3
"""
CLI wrapper — reproduces the exact behavior of the original
legacy/garmin_mosaic.py __main__ block, now backed by garmin_core.

  GARMIN_RSD_FILE=/path/to/file.RSD python scripts/garmin_mosaic.py

With a default MosaicConfig this is behavior-identical to the original script:
same default RSD path, same env var, same existence check, same banner,
same outputs in <rsd_parent>/garmin_output_<name>/processed/.
"""
import os
import sys

# Make the repo root importable when run as a loose script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from garmin_core.config import MosaicConfig
from garmin_core.mosaic import run_mosaic

# Same default and env var as the original (legacy/garmin_mosaic.py line 36).
RSD_FILE = os.environ.get(
    "GARMIN_RSD_FILE", r"C:\Users\jason\Downloads\cols2\24APR26-1329-01.RSD"
)


def main() -> int:
    print("=" * 60)
    print("Garmin Sidescan Mosaic Generator")
    print("=" * 60)

    if not os.path.exists(RSD_FILE):
        print(f"Input RSD file does not exist: {RSD_FILE}")
        print("Set GARMIN_RSD_FILE environment variable or pass a path.")
        return 1

    run_mosaic(RSD_FILE, MosaicConfig())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
