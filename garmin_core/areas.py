"""
garmin_core.areas — generic polygon / track-set mosaicking.

Generalized from legacy/{run_,merge_clip_}application_area_mosaics.py. The
legacy "application area" / "col" framing is dropped: this module thinks in
tracks + polygons + a mosaic selection. It supports the three target
workflows:

  W1  (caller-side) inspect one track's outputs/metadata.
  W2  mosaic an arbitrary set of tracks -> one TIF      (mosaic_tracks, no polygon)
  W3  mosaic tracks intersecting a polygon, clipped     (mosaic_tracks + clip_polygon)

The raster merge/clip math (sanitize_name, get_output_paths,
get_utm_crs_from_lon_lat, transform_geometry, get_target_resolution,
merge_and_clip_rasters) is copied VERBATIM from
legacy/merge_clip_application_area_mosaics.py so W3 is byte-faithful to the
original. _merge_rasters (W2) is a deliberate new sibling, clearly marked.
"""

from __future__ import annotations

import json
import re
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.vrt import WarpedVRT
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform
from shapely.prepared import prep


def _load_feature_collection(path: Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise ValueError(f"Expected a GeoJSON FeatureCollection: {path}")
    return payload


# ===================================================================
# VERBATIM from legacy/merge_clip_application_area_mosaics.py — do not edit.
# (Keeps the W3 merge+clip result byte-identical to the original script.)
# ===================================================================
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
# ===================================================================
# end verbatim block
# ===================================================================


def _merge_rasters(raster_paths, output_path: Path):
    """W2: merge rasters with no polygon clip.

    Deliberate sibling of merge_and_clip_rasters: identical reprojection and
    merge (UTM from data centroid, nearest, method="first", lzw, nodata=0),
    minus the geometry mask + bbox crop. NOT claimed verbatim.
    """
    with ExitStack() as stack:
        srcs = [stack.enter_context(rasterio.open(p)) for p in raster_paths]
        if not srcs:
            return False
        src0 = srcs[0]
        # UTM zone from the first raster's center, expressed in lon/lat.
        cx = (src0.bounds.left + src0.bounds.right) / 2.0
        cy = (src0.bounds.bottom + src0.bounds.top) / 2.0
        to_wgs = Transformer.from_crs(src0.crs, "EPSG:4326", always_xy=True)
        lon, lat = to_wgs.transform(cx, cy)
        target_crs = get_utm_crs_from_lon_lat(lon, lat)
        resolution = get_target_resolution(raster_paths)

        vrt_sources = [
            stack.enter_context(
                WarpedVRT(s, crs=target_crs, resampling=Resampling.nearest, nodata=0)
            )
            for s in srcs
        ]
        mosaic, transform = merge(
            vrt_sources, res=resolution, nodata=0, method="first"
        )
        if mosaic.size == 0 or not np.any(mosaic):
            return False

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


# ----- generic selection / API -------------------------------------------

def _as_payload(tracks):
    """Accept a FeatureCollection dict or a path to one."""
    if isinstance(tracks, (str, Path)):
        return _load_feature_collection(Path(tracks))
    return tracks


def first_polygon(polygon_source) -> "shape":
    """First non-empty polygon from a geojson path/FeatureCollection/geometry."""
    if isinstance(polygon_source, (str, Path)):
        payload = _load_feature_collection(Path(polygon_source))
        for feat in payload.get("features", []):
            geom = feat.get("geometry")
            if not geom:
                continue
            poly = shape(geom)
            if not poly.is_empty:
                return poly
        raise ValueError(f"No usable polygon in {polygon_source}")
    poly = shape(polygon_source)
    if poly.is_empty:
        raise ValueError("Empty polygon geometry")
    return poly


def tracks_intersecting_polygon(tracks, polygon) -> list[dict]:
    """W3 selection (generalized from legacy select_matching_tracks).

    Returns [{rsd_file: Path, file_name: str}] for tracks whose geometry
    intersects `polygon` (a shapely geom in WGS84). Track RSD path is read
    from the `file_path` property (same convention as legacy).
    """
    payload = _as_payload(tracks)
    prepared = prep(polygon)
    out = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry")
        if not geometry:
            continue
        track_geom = shape(geometry)
        if track_geom.is_empty:
            continue
        if not prepared.intersects(track_geom):
            continue
        props = feature.get("properties", {})
        rsd_path = props.get("file_path")
        if not rsd_path:
            continue
        out.append(
            {
                "rsd_file": Path(rsd_path).expanduser().resolve(),
                "file_name": props.get("file_name") or Path(rsd_path).name,
            }
        )
    return out


def all_tracks(tracks) -> list[dict]:
    """Every track in the inventory, as [{rsd_file, file_name}]."""
    payload = _as_payload(tracks)
    out = []
    for feature in payload.get("features", []):
        props = feature.get("properties", {})
        rsd_path = props.get("file_path")
        if not rsd_path:
            continue
        out.append(
            {
                "rsd_file": Path(rsd_path).expanduser().resolve(),
                "file_name": props.get("file_name") or Path(rsd_path).name,
            }
        )
    return out


def resolve_track_rasters(rsd_paths, raster_name: str = "intensity.tif") -> list[Path]:
    """Map RSD paths -> their existing processed/<raster_name>, deduped."""
    paths: list[Path] = []
    seen = set()
    for rsd in rsd_paths:
        rsd = Path(rsd)
        _, processed_dir = get_output_paths(rsd)
        raster_path = (processed_dir / raster_name).resolve()
        if not raster_path.exists():
            continue
        key = str(raster_path).lower()
        if key in seen:
            continue
        seen.add(key)
        paths.append(raster_path)
    return paths


def mosaic_tracks(
    rsd_paths,
    output_path,
    *,
    clip_polygon=None,
    raster_name: str = "intensity.tif",
    progress_cb=None,
) -> dict:
    """Mosaic a set of tracks into one GeoTIFF.

    W2: clip_polygon=None -> merge only.
    W3: clip_polygon given (shapely WGS84) -> merge then clip to it
        (uses the verbatim legacy core).

    Returns a summary dict; raises ValueError if no rasters resolve.
    """
    output_path = Path(output_path)
    rsd_paths = [Path(p) for p in rsd_paths]
    if progress_cb is not None:
        try:
            progress_cb("resolve", 0, len(rsd_paths))
        except Exception:
            pass

    raster_paths = resolve_track_rasters(rsd_paths, raster_name)
    if not raster_paths:
        raise ValueError(
            f"No '{raster_name}' outputs found for the selected tracks "
            f"(have they been processed?)"
        )

    if clip_polygon is not None:
        ok = merge_and_clip_rasters(raster_paths, clip_polygon, output_path)
        mode = "merge+clip"
    else:
        ok = _merge_rasters(raster_paths, output_path)
        mode = "merge"

    if progress_cb is not None:
        try:
            progress_cb("done", len(raster_paths), len(raster_paths))
        except Exception:
            pass

    return {
        "ok": bool(ok),
        "mode": mode,
        "rasters": len(raster_paths),
        "output_path": str(output_path) if ok else None,
    }
