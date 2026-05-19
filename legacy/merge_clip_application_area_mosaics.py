"""
Merge and clip completed Garmin mosaic outputs by application area polygon.

For each polygon in COL_Application_areas.geojson, this script finds matching
tracks from rsd_tracks_application_area_matches.geojson, collects the produced
GeoTIFFs for those tracks, merges them by raster type, and clips the merged
result to the polygon boundary.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.transform import array_bounds
from rasterio.vrt import WarpedVRT
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform


# --- Edit these before running ---
INPUT_FOLDER = Path(r"C:\Users\jason\Documents\RSD_FILES")
MATCHED_TRACKS_PATH = None  # None -> <INPUT_FOLDER>/rsd_tracks_application_area_matches.geojson
APPLICATION_AREAS_PATH = Path(__file__).with_name("col_buffered.geojson")
OUTPUT_DIR = None  # None -> <INPUT_FOLDER>/application_area_mosaics
RASTER_TYPES = (
    "intensity.tif",
)
SKIP_EXISTING_OUTPUTS = True


def resolve_matched_tracks_path():
    if MATCHED_TRACKS_PATH:
        return Path(MATCHED_TRACKS_PATH).expanduser().resolve()
    return (INPUT_FOLDER / "rsd_tracks_application_area_matches.geojson").resolve()


def resolve_output_dir():
    if OUTPUT_DIR:
        return Path(OUTPUT_DIR).expanduser().resolve()
    return (INPUT_FOLDER / "application_area_mosaics").resolve()


def load_feature_collection(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise SystemExit(f"Expected a GeoJSON FeatureCollection: {path}")
    return payload


def sanitize_name(value: str):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return safe.strip("._") or "unnamed"


def get_output_paths(rsd_file: Path):
    output_base_dir = rsd_file.parent / f"garmin_output_{rsd_file.stem}"
    processed_dir = output_base_dir / "processed"
    return output_base_dir, processed_dir


def get_utm_crs_from_lon_lat(lon: float, lat: float):
    zone = int((lon + 180.0) / 6.0) + 1
    zone = min(max(zone, 1), 60)
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return CRS.from_epsg(epsg)


def transform_geometry(geom, src_crs, dst_crs):
    transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    return shapely_transform(transformer.transform, geom)


def load_application_areas(path: Path):
    payload = load_feature_collection(path)
    areas = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        polygon = shape(geometry)
        if polygon.is_empty:
            continue
        props = feature.get("properties", {})
        legacy_name = f"feature_{feature.get('id', len(areas) + 1)}"
        area_name = props.get("Our_Name") or props.get("Name") or legacy_name
        areas.append(
            {
                "name": area_name,
                "legacy_name": legacy_name,
                "safe_name": sanitize_name(area_name),
                "object_id": props.get("OBJECTID"),
                "geometry_wgs84": polygon,
            }
        )
    return areas


def build_area_track_map(matched_tracks_payload):
    area_to_tracks = defaultdict(list)
    for feature in matched_tracks_payload.get("features", []):
        props = feature.get("properties", {})
        rsd_path = props.get("file_path")
        if not rsd_path:
            continue
        track_info = {
            "rsd_file": Path(rsd_path).expanduser().resolve(),
            "file_name": props.get("file_name"),
        }
        for area_name in props.get("application_area_names") or []:
            area_to_tracks[area_name].append(track_info)
    return area_to_tracks


def collect_rasters_for_area(track_infos):
    rasters_by_type = defaultdict(list)
    seen_paths = set()

    for track in track_infos:
        _, processed_dir = get_output_paths(track["rsd_file"])
        for raster_name in RASTER_TYPES:
            raster_path = (processed_dir / raster_name).resolve()
            if not raster_path.exists():
                continue
            dedupe_key = (raster_name, str(raster_path).lower())
            if dedupe_key in seen_paths:
                continue
            seen_paths.add(dedupe_key)
            rasters_by_type[raster_name].append(raster_path)

    return rasters_by_type


def get_target_resolution(raster_paths):
    resolutions = []
    for raster_path in raster_paths:
        with rasterio.open(raster_path) as src:
            resolutions.append((abs(src.res[0]), abs(src.res[1])))
    return min(resolutions, key=lambda pair: pair[0] * pair[1])


def merge_and_clip_rasters(raster_paths, polygon_wgs84, output_path: Path):
    centroid = polygon_wgs84.centroid
    target_crs = get_utm_crs_from_lon_lat(centroid.x, centroid.y)
    polygon_target = transform_geometry(polygon_wgs84, "EPSG:4326", target_crs)
    bounds = polygon_target.bounds
    resolution = get_target_resolution(raster_paths)

    with ExitStack() as stack:
        vrt_sources = []
        src0 = None
        for raster_path in raster_paths:
            src = stack.enter_context(rasterio.open(raster_path))
            if src0 is None:
                src0 = src
            vrt = stack.enter_context(
                WarpedVRT(
                    src,
                    crs=target_crs,
                    resampling=Resampling.nearest,
                    nodata=0,
                )
            )
            vrt_sources.append(vrt)

        if src0 is None:
            return False

        mosaic, transform = merge(
            vrt_sources,
            bounds=bounds,
            res=resolution,
            nodata=0,
            method="first",
        )

        if mosaic.size == 0:
            return False

        mask = geometry_mask(
            [polygon_target.__geo_interface__],
            out_shape=(mosaic.shape[1], mosaic.shape[2]),
            transform=transform,
            invert=True,
            all_touched=False,
        )
        mosaic[:, ~mask] = 0

        if not np.any(mosaic):
            return False

        min_y, min_x, max_y, max_x = np.where(mask)[0].min(), np.where(mask)[1].min(), np.where(mask)[0].max(), np.where(mask)[1].max()
        mosaic = mosaic[:, min_y:max_y + 1, min_x:max_x + 1]
        transform = rasterio.Affine(
            transform.a,
            transform.b,
            transform.c + (min_x * transform.a),
            transform.d,
            transform.e,
            transform.f + (min_y * transform.e),
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        profile = src0.profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            count=mosaic.shape[0],
            crs=target_crs,
            transform=transform,
            nodata=0,
            compress="lzw",
        )

        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(mosaic)

    return True


def main():
    matched_tracks_path = resolve_matched_tracks_path()
    application_areas_path = APPLICATION_AREAS_PATH.expanduser().resolve()
    output_dir = resolve_output_dir()

    if not matched_tracks_path.exists():
        raise SystemExit(f"Matched tracks file not found: {matched_tracks_path}")
    if not application_areas_path.exists():
        raise SystemExit(f"Application areas file not found: {application_areas_path}")

    matched_tracks_payload = load_feature_collection(matched_tracks_path)
    areas = load_application_areas(application_areas_path)
    area_track_map = build_area_track_map(matched_tracks_payload)

    print(f"Loaded {len(areas)} application area(s)")
    print(f"Loaded {len(matched_tracks_payload.get('features', []))} matched track(s)")

    wrote = 0
    skipped = 0

    for area in areas:
        track_infos = area_track_map.get(area["name"], [])
        if not track_infos:
            track_infos = area_track_map.get(area["legacy_name"], [])
        if not track_infos:
            continue

        rasters_by_type = collect_rasters_for_area(track_infos)
        if not rasters_by_type:
            continue

        area_output_dir = output_dir / area["safe_name"]
        print(f"Processing {area['name']} with {len(track_infos)} track(s)")

        for raster_name, raster_paths in rasters_by_type.items():
            raster_stem = Path(raster_name).stem
            output_path = area_output_dir / f"{area['safe_name']}_{raster_stem}_clipped.tif"

            if SKIP_EXISTING_OUTPUTS and output_path.exists():
                print(f"  Skipping existing {output_path.name}")
                skipped += 1
                continue

            print(f"  Merging {len(raster_paths)} raster(s) for {raster_name}")
            if merge_and_clip_rasters(raster_paths, area["geometry_wgs84"], output_path):
                wrote += 1
                print(f"    Wrote {output_path}")
            else:
                print(f"    No clipped pixels written for {output_path.name}")

    print(f"Wrote clipped mosaics: {wrote}")
    print(f"Skipped existing outputs: {skipped}")


if __name__ == "__main__":
    main()
