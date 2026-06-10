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


def swath_wgs84(geometry: Any, port_m: float, star_m: float):
    """Port/starboard swath polygons for a WGS84 track line.

    Buffers each side of the line separately in the centroid's UTM zone.
    Shapely's single-sided buffer puts a positive distance on the LEFT of
    the line direction — the port side when coordinates are ordered by
    time of travel, which the inventory tracks are.

    Returns (port_geom, star_geom) as WGS84 shapely geometries; a side
    with range <= 0 comes back as None.
    """
    geom = geometry if hasattr(geometry, "geom_type") else _shape(geometry)
    c = geom.centroid
    epsg = _utm_epsg(c.x, c.y)
    to_utm = Transformer.from_crs("EPSG:4326", CRS.from_epsg(epsg),
                                  always_xy=True).transform
    to_wgs = Transformer.from_crs(CRS.from_epsg(epsg), "EPSG:4326",
                                  always_xy=True).transform
    line = _transform(to_utm, geom)
    sides = []
    for dist in (abs(port_m), -abs(star_m)):
        if dist == 0:
            sides.append(None)
            continue
        poly = line.buffer(dist, single_sided=True)
        if not poly.is_valid:  # tight turns can self-intersect
            poly = poly.buffer(0)
        sides.append(_transform(to_wgs, poly))
    return tuple(sides)


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
