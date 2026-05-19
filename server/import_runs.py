"""Bulk-import already-processed mosaics (no pipeline re-run).

  python -m server.import_runs <SOURCE_DIR> [--tracks rsd_tracks.geojson] [--dry-run]

SOURCE_DIR is an rsync'd tree containing legacy outputs, i.e. any number of
  <something>/processed/intensity.tif
(typically garmin_output_<NAME>/processed/intensity.tif).

For each tif: COG-convert into /data/runs/<id>/intensity_cog.tif and register
a completed 'mosaic' job keyed to "<NAME>.RSD" so the map links the existing
track to it. RSDs are NOT needed. Idempotent: an RSD already having a done
mosaic run is skipped. Only the COG is stored (the source tif is read, not
copied) — keeps disk small.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from . import db
from .cog import to_cog
from .settings import RUNS_DIR


def _rsd_stem(tif: Path) -> str:
    # .../<parent>/processed/intensity.tif  -> parent name, minus garmin_output_
    parent = tif.parent.parent.name
    pref = "garmin_output_"
    return parent[len(pref):] if parent.startswith(pref) else parent


def _track_stems(geojson: Path) -> set[str]:
    data = json.loads(geojson.read_text())
    out = set()
    for f in data.get("features", []):
        fn = (f.get("properties") or {}).get("file_name")
        if fn:
            out.add(Path(fn).stem)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--tracks", help="rsd_tracks.geojson to validate matches against")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv[1:])

    src = Path(args.source).expanduser().resolve()
    if not src.is_dir():
        print(f"source not a directory: {src}", file=sys.stderr)
        return 2

    tifs = sorted(src.glob("**/processed/intensity.tif"))
    if not tifs:
        print(f"no */processed/intensity.tif under {src}", file=sys.stderr)
        return 1

    track_stems = _track_stems(Path(args.tracks)) if args.tracks else None
    db.init_db()

    imported = skipped = unmatched = 0
    for tif in tifs:
        stem = _rsd_stem(tif)
        rsd_name = f"{stem}.RSD"

        if track_stems is not None and stem not in track_stems:
            print(f"  WARN no track for {rsd_name} (will be invisible on map)")
            unmatched += 1

        if db.find_done_mosaic_by_rsd(rsd_name):
            print(f"  skip {rsd_name} (already imported)")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  would import {rsd_name}  <- {tif}")
            imported += 1
            continue

        job_id = uuid.uuid4().hex
        run_dir = RUNS_DIR / job_id
        cog = to_cog(tif, run_dir / "intensity_cog.tif")
        db.create_done_job(
            "mosaic",
            {"rsd_path": rsd_name, "imported": True},
            {"run_id": job_id, "cog": str(cog), "rsd_name": rsd_name,
             "imported": True},
            job_id=job_id,
        )
        print(f"  imported {rsd_name}  ({cog.stat().st_size // (1<<20)} MB COG)")
        imported += 1

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}done: "
          f"{imported} imported, {skipped} skipped"
          + (f", {unmatched} with no matching track" if track_stems is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
