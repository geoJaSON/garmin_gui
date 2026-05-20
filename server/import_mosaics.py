"""Bulk-import already-clipped per-area mosaics (legacy merge_clip output).

  docker compose exec app python -m server.import_mosaics <SOURCE_DIR>
                                  [--dry-run] [--force]

Layout expected (matches legacy/merge_clip_application_area_mosaics.py):

  <SOURCE_DIR>/<safe_name>/<safe_name>_intensity_clipped.tif

For each tif: COG-convert into /data/mosaics/<id>/mosaic_cog.tif, copy
the original as mosaic.tif (the downloadable deliverable), register a
completed 'combine' job, and link the matching area row to it so the
Data table shows GeoTIFF + Metadata downloads immediately.

Matching: file stem -> strip '_intensity_clipped' suffix -> compare
against sanitize_name(area.our_name) for each area in the DB. Idempotent:
areas that already have a mosaic_job_id are skipped unless --force.

The imported deliverables won't have a contributing-run list or buffer
recorded (we don't know which RSDs went in or what buffer was used).
Their metadata.txt will be sparse; clicking Generate later will rebuild
a fully-detailed one from the current runs + weather.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from pathlib import Path

from garmin_core.areas import sanitize_name
from . import db
from .cog import to_cog
from .settings import MOSAICS_DIR


_SUFFIX = "_intensity_clipped"


def _index_areas_by_safe_name() -> dict:
    """Build {sanitize_name(our_name): area_row} for fast lookup."""
    out: dict = {}
    for a in db.list_areas():
        key = sanitize_name(a["our_name"])
        # If two areas sanitize to the same name (unlikely), record both so
        # we can warn about the collision.
        out.setdefault(key, []).append(a)
    return out


def _candidate_key(tif: Path) -> str:
    stem = tif.stem
    if stem.endswith(_SUFFIX):
        stem = stem[: -len(_SUFFIX)]
    return stem


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-link even if the area already has a mosaic")
    args = ap.parse_args(argv[1:])

    src = Path(args.source).expanduser().resolve()
    if not src.is_dir():
        print(f"source not a directory: {src}", file=sys.stderr)
        return 2

    db.init_db()
    by_safe = _index_areas_by_safe_name()
    if not by_safe:
        print("no areas in the DB — upload your areas.geojson first "
              "(Layers panel or POST /api/areas/upload)", file=sys.stderr)
        return 1

    tifs = sorted(src.glob("**/*_intensity_clipped.tif"))
    if not tifs:
        # Fall back to any tif under <safe_name>/<safe_name>.tif layouts.
        tifs = sorted(p for p in src.glob("**/*.tif")
                      if p.is_file())
    if not tifs:
        print(f"no .tif files under {src}", file=sys.stderr)
        return 1

    added = skipped = unmatched = collisions = 0
    for tif in tifs:
        key = _candidate_key(tif)
        cands = by_safe.get(key) or []
        if not cands:
            # try parent directory name as a fallback key
            cands = by_safe.get(tif.parent.name) or []
        if not cands:
            print(f"  no area match: {tif.name}")
            unmatched += 1
            continue
        if len(cands) > 1:
            print(f"  ! {tif.name}: name collides for {len(cands)} areas; "
                  f"skipping (use --map later if needed)")
            collisions += 1
            continue
        area = cands[0]

        if area.get("mosaic_job_id") and not args.force:
            print(f"  skip {area['our_name']} (already linked)")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  would import {tif.name} -> area "
                  f"'{area['our_name']}' ({area['tpwd_app_no']})")
            added += 1
            continue

        jid = uuid.uuid4().hex
        mdir = MOSAICS_DIR / jid
        mdir.mkdir(parents=True, exist_ok=True)
        # Copy the original tif as the downloadable deliverable; COG-convert
        # a sibling for tiling. Same shape as a live combine result.
        deliverable = mdir / "mosaic.tif"
        shutil.copy2(tif, deliverable)
        cog = to_cog(tif, mdir / "mosaic_cog.tif")
        db.create_done_job(
            "combine",
            {"imported": True, "area_id": area["id"],
             "source_tif": str(tif)},
            {
                "ok": True, "mode": "merge+clip", "rasters": None,
                "sources": None, "area_name": sanitize_name(area["our_name"]),
                "area_id": area["id"],
                "cog": str(cog), "deliverable": str(deliverable),
                "imported": True,
            },
            job_id=jid,
        )
        db.set_area_mosaic_job(area["id"], jid)
        print(f"  imported {area['our_name']}  "
              f"({tif.stat().st_size // (1 << 20)} MB)")
        added += 1

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}done: "
          f"{added} imported, {skipped} skipped, "
          f"{unmatched} unmatched"
          + (f", {collisions} name collisions" if collisions else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
