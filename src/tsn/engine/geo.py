"""地理计算.

只用球面近似 + 局部平面投影，够用且不依赖 GIS 库。
要更高精度再换 pyproj，但 GPS 本身误差就有几米，没必要。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

EARTH_R = 6371008.8  # 平均半径，米

Coord = tuple[float, float]  # (lng, lat)


def haversine(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """两点球面距离（米）."""
    lat1, lat2 = math.radians(a_lat), math.radians(b_lat)
    dlat = lat2 - lat1
    dlng = math.radians(b_lng - a_lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def bearing(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    """方位角（度，正北为 0）."""
    lat1, lat2 = math.radians(a_lat), math.radians(b_lat)
    dlng = math.radians(b_lng - a_lng)
    y = math.sin(dlng) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


class LocalProjection:
    """以某点为原点的局部平面投影，用于线段插值.

    小范围内（几公里）误差可以忽略，比反复算球面快得多。
    """

    __slots__ = ("lng0", "lat0", "_m_per_deg_lat", "_m_per_deg_lng")

    def __init__(self, lng0: float, lat0: float) -> None:
        self.lng0 = lng0
        self.lat0 = lat0
        self._m_per_deg_lat = math.pi * EARTH_R / 180.0
        self._m_per_deg_lng = self._m_per_deg_lat * math.cos(math.radians(lat0))

    def to_xy(self, lng: float, lat: float) -> tuple[float, float]:
        return ((lng - self.lng0) * self._m_per_deg_lng, (lat - self.lat0) * self._m_per_deg_lat)

    def to_lnglat(self, x: float, y: float) -> Coord:
        return (self.lng0 + x / self._m_per_deg_lng, self.lat0 + y / self._m_per_deg_lat)

    def interpolate(self, a: Sequence[float], b: Sequence[float], ratio: float) -> Coord:
        ax, ay = self.to_xy(a[0], a[1])
        bx, by = self.to_xy(b[0], b[1])
        r = min(1.0, max(0.0, ratio))
        return self.to_lnglat(ax + (bx - ax) * r, ay + (by - ay) * r)


def normalize_points(points: Iterable[Sequence[float]]) -> list[Coord]:
    """统一成 (lng, lat) 并去掉非法点和连续重复点."""
    out: list[Coord] = []
    for p in points:
        if len(p) < 2:
            continue
        try:
            lng, lat = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lng) and math.isfinite(lat)):
            continue
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            continue
        if out and abs(out[-1][0] - lng) < 1e-9 and abs(out[-1][1] - lat) < 1e-9:
            continue
        out.append((lng, lat))
    return out


def polyline_length(points: Sequence[Coord]) -> float:
    total = 0.0
    for i in range(1, len(points)):
        total += haversine(points[i - 1][1], points[i - 1][0], points[i][1], points[i][0])
    return total


def point_in_polygon(lng: float, lat: float, polygon: Sequence[Sequence[float]]) -> bool:
    """射线法判断点是否在多边形内.

    polygon 是 [[lng, lat], ...] 或 [{'longitude':..,'latitude':..}, ...]
    """
    verts = _polygon_verts(polygon)
    if len(verts) < 3:
        return True  # 围栏无效时不做限制

    inside = False
    n = len(verts)
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_cross = (x2 - x1) * (lat - y1) / (y2 - y1 + 1e-12) + x1
            if lng < x_cross:
                inside = not inside
    return inside


def _polygon_verts(polygon: Sequence[Sequence[float]] | Sequence[dict]) -> list[tuple[float, float]]:
    verts: list[tuple[float, float]] = []
    for p in polygon:
        if isinstance(p, dict):
            lng = p.get("longitude", p.get("lng"))
            lat = p.get("latitude", p.get("lat"))
        else:
            lng, lat = (p[0], p[1]) if len(p) >= 2 else (None, None)
        if lng is None or lat is None:
            continue
        try:
            verts.append((float(lng), float(lat)))
        except (TypeError, ValueError):
            continue
    return verts


def polygon_center(polygon: Sequence[Sequence[float]] | Sequence[dict]) -> Coord | None:
    verts = _polygon_verts(polygon)
    if not verts:
        return None
    return (sum(v[0] for v in verts) / len(verts), sum(v[1] for v in verts) / len(verts))


def polygon_radius(polygon: Sequence[Sequence[float]] | Sequence[dict]) -> float:
    """多边形外接圆半径的近似（到中心的最大距离）."""
    verts = _polygon_verts(polygon)
    if len(verts) < 2:
        return 0.0
    cx, cy = sum(v[0] for v in verts) / len(verts), sum(v[1] for v in verts) / len(verts)
    return max(haversine(cy, cx, v[1], v[0]) for v in verts)
