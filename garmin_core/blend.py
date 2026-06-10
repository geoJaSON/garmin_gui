"""
garmin_core.blend — seam-aware merging of run COGs.

Replaces rasterio.merge(method="first") for the combine workflows with two
quality passes the plain merge can't do:

  1. Histogram matching (color balance): each source's valid-pixel histogram
     is quantile-mapped onto a reference source (the median-brightness one),
     so independently percentile-stretched runs stop looking like a
     patchwork. `match_strength` blends the LUT with identity (1.0 = full
     match, 0.0 = off).

  2. Narrow-band seam feathering: sources are composited in priority order
     ("over" operator). A source keeps full weight in its interior and only
     ramps to transparent over `feather_m` meters at its own data edge, so
     seams soften WITHOUT averaging whole overlap zones — averaging would
     ghost every target that appears in both passes, since consumer GPS
     misregisters adjacent passes by a couple of meters.

With match_strength=0 and feather_m=0 the output is pixel-identical to
rasterio.merge(method="first") on the same warped grid.

Inputs are the app's uint8 COGs (nodata=0, values 1..255).
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from scipy.ndimage import distance_transform_edt

from .areas import get_target_resolution, get_utm_crs_from_lon_lat, transform_geometry

# Grid cap: the compositor holds three float32 accumulators at mosaic size
# (~12 bytes/px). 500 Mpx ≈ 6 GB — past that, raise so the caller can fall
# back to the plain streaming merge instead of OOMing the worker.
MAX_GRID_PIXELS = 500_000_000

_LEVELS = np.arange(256, dtype=np.float64)


def _hist256(path: Path) -> np.ndarray:
    """Valid-pixel (value>0) histogram of a uint8 raster, decimated to ~4 Mpx.

    Decimated reads pull from COG overviews when present; histograms are
    insensitive to nearest-neighbor decimation.
    """
    with rasterio.open(path) as src:
        scale = max(1.0, np.sqrt((src.width * src.height) / 4e6))
        out_shape = (max(1, int(src.height / scale)), max(1, int(src.width / scale)))
        data = src.read(1, out_shape=out_shape)
    h = np.bincount(data.ravel(), minlength=256).astype(np.float64)
    h[0] = 0.0  # nodata never participates
    return h


def _cdf(hist: np.ndarray) -> np.ndarray:
    total = hist.sum()
    if total <= 0:
        return np.linspace(0.0, 1.0, 256)
    return np.cumsum(hist) / total


def _median_level(hist: np.ndarray) -> float:
    return float(np.searchsorted(_cdf(hist), 0.5))


def build_match_luts(raster_paths, match_strength: float) -> list[np.ndarray]:
    """Per-source uint8 LUTs quantile-mapping each histogram onto a reference.

    The reference is the source with the median of the median gray levels —
    matching everyone to the most middle-of-the-road run moves the least
    data. LUTs keep 0 -> 0 (nodata) and valid output in 1..255.
    """
    hists = [_hist256(Path(p)) for p in raster_paths]
    strength = float(np.clip(match_strength, 0.0, 1.0))
    if strength == 0.0 or len(hists) < 2:
        return [_LEVELS.astype(np.uint8)] * len(hists)

    medians = [_median_level(h) for h in hists]
    ref_idx = int(np.argsort(medians)[len(medians) // 2])
    ref_cdf = _cdf(hists[ref_idx])

    luts = []
    for i, h in enumerate(hists):
        if i == ref_idx:
            luts.append(_LEVELS.astype(np.uint8))
            continue
        src_cdf = _cdf(h)
        mapped = np.interp(src_cdf, ref_cdf, _LEVELS)
        lut = strength * mapped + (1.0 - strength) * _LEVELS
        lut = np.clip(np.rint(lut), 1, 255).astype(np.uint8)
        lut[0] = 0
        luts.append(lut)
    return luts


def merge_rasters_blended(raster_paths, output_path, *, clip_polygon=None,
                          match_strength: float = 1.0,
                          feather_m: float = 1.5) -> bool:
    """Merge run COGs with histogram matching + feathered seams.

    clip_polygon: optional shapely geometry in WGS84 — output is masked to
    it and cropped to its bbox (same contract as merge_and_clip_rasters).
    Raises ValueError when the target grid exceeds MAX_GRID_PIXELS; callers
    should fall back to the plain merge.
    """
    raster_paths = [Path(p) for p in raster_paths]
    output_path = Path(output_path)
    if not raster_paths:
        return False

    with ExitStack() as stack:
        srcs = [stack.enter_context(rasterio.open(p)) for p in raster_paths]
        src0 = srcs[0]

        # Target CRS: polygon centroid when clipping (deliverable contract),
        # else the first raster's center — mirrors areas.py.
        if clip_polygon is not None:
            c = clip_polygon.centroid
            target_crs = get_utm_crs_from_lon_lat(c.x, c.y)
        else:
            cx = (src0.bounds.left + src0.bounds.right) / 2.0
            cy = (src0.bounds.bottom + src0.bounds.top) / 2.0
            from pyproj import Transformer
            lon, lat = Transformer.from_crs(src0.crs, "EPSG:4326",
                                            always_xy=True).transform(cx, cy)
            target_crs = get_utm_crs_from_lon_lat(lon, lat)

        res_x, res_y = get_target_resolution(raster_paths)

        # Grid bounds: clip polygon bbox, else union of warped source bounds.
        probe_vrts = [stack.enter_context(WarpedVRT(s, crs=target_crs,
                                                    resampling=Resampling.nearest,
                                                    nodata=0))
                      for s in srcs]
        if clip_polygon is not None:
            polygon_target = transform_geometry(clip_polygon, "EPSG:4326", target_crs)
            min_x, min_y, max_x, max_y = polygon_target.bounds
        else:
            polygon_target = None
            min_x = min(v.bounds.left for v in probe_vrts)
            min_y = min(v.bounds.bottom for v in probe_vrts)
            max_x = max(v.bounds.right for v in probe_vrts)
            max_y = max(v.bounds.top for v in probe_vrts)

        width = max(1, int(np.ceil((max_x - min_x) / res_x)))
        height = max(1, int(np.ceil((max_y - min_y) / res_y)))
        if width * height > MAX_GRID_PIXELS:
            raise ValueError(
                f"blended merge grid {width}x{height} exceeds "
                f"{MAX_GRID_PIXELS} px — use the plain merge")
        transform = from_origin(min_x, max_y, res_x, res_y)

        luts = build_match_luts(raster_paths, match_strength)
        feather_px = float(feather_m) / float(res_x)

        num = np.zeros((height, width), dtype=np.float32)
        den = np.zeros((height, width), dtype=np.float32)
        remaining = np.ones((height, width), dtype=np.float32)

        for src, lut in zip(srcs, luts):
            grid_vrt = WarpedVRT(src, crs=target_crs, transform=transform,
                                 width=width, height=height,
                                 resampling=Resampling.nearest, nodata=0)
            with grid_vrt:
                v = grid_vrt.read(1)
            valid = v != 0
            if not valid.any():
                continue
            if feather_px > 0:
                # Distance (px) from each pixel to this source's own data
                # edge; alpha ramps 0 -> 1 over the feather band.
                dist = distance_transform_edt(valid)
                alpha = np.minimum(dist / feather_px, 1.0).astype(np.float32)
            else:
                alpha = valid.astype(np.float32)
            w = alpha * remaining
            num += lut[v].astype(np.float32) * w
            den += w
            remaining *= (1.0 - alpha)

        has = den > 1e-6
        if not has.any():
            return False
        mosaic = np.zeros((height, width), dtype=np.uint8)
        mosaic[has] = np.clip(np.rint(num[has] / den[has]), 1, 255).astype(np.uint8)
        del num, den, remaining

        if polygon_target is not None:
            mask = geometry_mask(
                [polygon_target.__geo_interface__],
                out_shape=(height, width),
                transform=transform,
                invert=True,
                all_touched=False,
            )
            mosaic[~mask] = 0
            if not np.any(mosaic):
                return False
            rows, cols = np.where(mask)
            r0, r1 = rows.min(), rows.max()
            c0, c1 = cols.min(), cols.max()
            mosaic = mosaic[r0:r1 + 1, c0:c1 + 1]
            transform = rasterio.Affine(
                transform.a, transform.b, transform.c + c0 * transform.a,
                transform.d, transform.e, transform.f + r0 * transform.e,
            )
        elif not np.any(mosaic):
            return False

        output_path.parent.mkdir(parents=True, exist_ok=True)
        profile = src0.profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[0],
            width=mosaic.shape[1],
            count=1,
            dtype=rasterio.uint8,
            crs=target_crs,
            transform=transform,
            nodata=0,
            compress="lzw",
        )
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(mosaic, 1)
    return True
