"""Small geo helpers shared between the API and the worker."""

from __future__ import annotations

from typing import Any

from pyproj import CRS, Transformer
from shapely.geometry import shape as _shape
from shapely.ops import transform as _transform


def _utm_epsg(lon: float, lat: float) -> int:
    zone = int((lon + 180.0) / 6.0) + 1
    zone = min(max(zone, 1), 60)
    return (32600 + zone) if lat >= 0 else (32700 + zone)


def buffer_wgs84(geometry: Any, buffer_m: float):
    """Buffer a WGS84 geometry by `buffer_m` METERS.

    Projects to the UTM zone for the geometry centroid, buffers in meters,
    projects back to WGS84. Accepts a GeoJSON-style geometry dict or a
    shapely geometry. Returns a shapely geometry (WGS84).
    """
    if hasattr(geometry, "geom_type"):
        geom = geometry
    else:
        geom = _shape(geometry)
    if buffer_m <= 0:
        return geom
    c = geom.centroid
    epsg = _utm_epsg(c.x, c.y)
    to_utm = Transformer.from_crs("EPSG:4326", CRS.from_epsg(epsg),
                                  always_xy=True).transform
    to_wgs = Transformer.from_crs(CRS.from_epsg(epsg), "EPSG:4326",
                                  always_xy=True).transform
    return _transform(to_wgs, _transform(to_utm, geom).buffer(buffer_m))
