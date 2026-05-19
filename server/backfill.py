"""Backfill survey metadata + weather into existing mosaic job results.

Runs inside the container:

  docker compose exec app python -m server.backfill            # fill gaps
  docker compose exec app python -m server.backfill --force    # refetch all
  docker compose exec app python -m server.backfill --dry-run  # show only

For each completed mosaic job that's missing `meta` and/or `weather`:
  - meta:    from the on-disk pingverter CSVs (run dir, then RSD-folder)
  - weather: parse the date from the RSD filename + take the matching
             track's centroid from the inventory geojson; Open-Meteo
             cached fetch (so re-runs/scale are cheap)

Imported historical mosaics have no CSVs, so they get weather but not
meta — which is the most you can do without the original RSDs.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import db
from .settings import RSD_DIR, RUNS_DIR, TRACKS_DIR
from .track_metadata import summarize_meta_dir
from .weather import fetch_daily, parse_rsd_datetime


def _load_inventory() -> dict:
    p = TRACKS_DIR / "rsd_tracks.geojson"
    if not p.exists():
        return {"features": []}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {"features": []}


def _track_centroid(inv: dict, file_name: str):
    for f in inv.get("features", []):
        if (f.get("properties") or {}).get("file_name") == file_name:
            g = f.get("geometry") or {}
            coords = g.get("coordinates") or []
            if g.get("type") == "LineString" and coords:
                xs = [c[0] for c in coords]
                ys = [c[1] for c in coords]
                return sum(ys) / len(ys), sum(xs) / len(xs)
            if g.get("type") == "MultiLineString" and coords:
                pts = [p for line in coords for p in line]
                if pts:
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    return sum(ys) / len(ys), sum(xs) / len(xs)
            return None
    return None


def _find_meta_dir(job_id: str, stem: str):
    for p in (
        RUNS_DIR / job_id / f"garmin_output_{stem}" / "meta",
        RSD_DIR / f"garmin_output_{stem}" / "meta",
    ):
        if p.is_dir():
            return p
    return None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="refetch even if meta/weather already present")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv[1:])

    db.init_db()
    inv = _load_inventory()
    no_inv = not inv.get("features")
    if no_inv:
        print("  ! no track inventory yet — weather backfill needs lat/lon "
              "from a track. Upload your rsd_tracks.geojson first "
              "(POST /api/tracks) or run a tracks job over /data/rsd.")

    jobs = [j for j in db.list_jobs(100000)
            if j["kind"] == "mosaic" and j["status"] == "done"
            and j.get("result")]
    print(f"Considering {len(jobs)} completed mosaic job(s)")

    meta_n = wx_n = skip_n = err_n = 0
    for j in jobs:
        res = j.get("result") or {}
        params = j.get("params") or {}
        rsd = (res.get("rsd_name")
               or Path(params.get("rsd_path", "")).name)
        if not rsd:
            skip_n += 1
            continue
        stem = Path(rsd).stem
        patch = {}

        # --- metadata ---
        if args.force or not res.get("meta"):
            md = _find_meta_dir(j["id"], stem)
            if md:
                try:
                    m = summarize_meta_dir(md)
                    if m:
                        patch["meta"] = m
                except Exception as e:
                    print(f"  ! {rsd}: meta read failed: {e}")
                    err_n += 1

        # --- weather + survey_datetime ---
        if args.force or not res.get("weather"):
            dt = parse_rsd_datetime(rsd)
            if dt is not None:
                patch.setdefault("survey_datetime",
                                 dt.isoformat(timespec="minutes"))
                cen = _track_centroid(inv, rsd) if inv else None
                if cen is not None:
                    lat, lon = cen
                    try:
                        w = fetch_daily(lat, lon, dt.date())
                        if w:
                            patch["weather"] = w
                            # be polite to Open-Meteo (cached calls are free)
                            time.sleep(0.05)
                    except Exception as e:
                        print(f"  ! {rsd}: weather fetch failed: {e}")
                        err_n += 1

        if not patch:
            skip_n += 1
            continue

        labels = []
        if "meta" in patch:
            labels.append("meta"); meta_n += 1
        if "weather" in patch:
            labels.append("weather"); wx_n += 1
        if "survey_datetime" in patch and "weather" not in patch:
            labels.append("date")
        verb = "would update" if args.dry_run else "updated"
        print(f"  + {rsd}: {verb} {', '.join(labels)}")
        if not args.dry_run:
            db.update_job_result(j["id"], patch)

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}done: "
          f"meta+{meta_n}, weather+{wx_n}, "
          f"unchanged {skip_n}" + (f", errors {err_n}" if err_n else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
