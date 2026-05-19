#!/usr/bin/env python3
"""
CLI for generic track/polygon mosaicking (garmin_core.areas).

  # W2: mosaic every track in the inventory into one TIF
  python scripts/mosaic_tracks.py TRACKS.geojson OUT.tif

  # W3: mosaic tracks intersecting a polygon, clipped to it
  python scripts/mosaic_tracks.py TRACKS.geojson OUT.tif --polygon AREA.geojson

Replaces the application-area-specific legacy scripts with generic polygon /
track-set selection. The merge+clip math is the verbatim legacy core.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from garmin_core import areas


def main() -> int:
    ap = argparse.ArgumentParser(description="Mosaic tracks into one GeoTIFF.")
    ap.add_argument("tracks", help="track inventory GeoJSON (from rsd_tracks)")
    ap.add_argument("output", help="output .tif path")
    ap.add_argument("--polygon", help="polygon GeoJSON; clip + select to it (W3)")
    ap.add_argument("--raster-name", default="intensity.tif")
    args = ap.parse_args()

    clip = None
    if args.polygon:
        clip = areas.first_polygon(args.polygon)
        selected = areas.tracks_intersecting_polygon(args.tracks, clip)
    else:
        selected = areas.all_tracks(args.tracks)

    if not selected:
        print("No tracks selected.")
        return 1

    rsd_paths = [t["rsd_file"] for t in selected]
    print(f"Selected {len(rsd_paths)} track(s); mosaicking...")
    try:
        result = areas.mosaic_tracks(
            rsd_paths, args.output,
            clip_polygon=clip, raster_name=args.raster_name,
        )
    except ValueError as e:
        print(e)
        return 1

    if not result["ok"]:
        print("Mosaic produced no data (no overlap / empty result).")
        return 1
    print(f"Wrote {result['mode']} mosaic of {result['rasters']} raster(s) "
          f"to {result['output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
