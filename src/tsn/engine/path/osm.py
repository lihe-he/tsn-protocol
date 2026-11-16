"""OSM 路线源 —— 没有历史记录时也能生成合规路线.

思路：
    1. 用学校围栏（或学校坐标）做中心，拉取周边步行路网
    2. 在路网上找环（校园跑道、环路天然就是闭环）
    3. 没有合适环就用随机游走 + 回起点，凑出接近目标长度的路径
    4. 所有点必须落在围栏内

依赖 osmnx 是可选的：没装时 osm_available() 返回 False，
上层会退回到历史记录或手工路线。
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any

from tsn.core.errors import EngineError
from tsn.core.models import School
from tsn.engine import geo
from tsn.engine.path.base import RoutePlan

log = logging.getLogger("tsn.engine.path.osm")


def osm_available() -> bool:
    try:
        import osmnx  # noqa: F401
    except Exception:  # noqa: BLE001  osmnx 依赖 geopandas，失败原因很多
        return False
    return True


@dataclass(slots=True)
class OSMPathSource:
    """基于 OpenStreetMap 的路线合成."""

    radius_m: float = 900.0
    network_type: str = "walk"
    center: tuple[float, float] | None = None  # (lng, lat)
    name: str = "osm"

    async def get_routes(
        self,
        school: School,
        *,
        target_m: float,
        limit: int = 10,
    ) -> list[RoutePlan]:
        if not osm_available():
            raise EngineError(
                "未安装 osmnx，无法从路网生成路线；"
                "安装方式：pip install 'tsn-protocol[osm]'"
            )

        center = self.center or self._resolve_center(school)
        if center is None:
            raise EngineError("无法确定中心点：学校没有坐标也没有围栏")

        import asyncio

        loop = asyncio.get_running_loop()
        graph = await loop.run_in_executor(None, self._fetch_graph, center)
        if graph is None:
            return []

        loops = self._extract_loops(graph, center, target_m, limit)
        return loops

    # ── 内部 ──

    def _resolve_center(self, school: School) -> tuple[float, float] | None:
        """围栏中心优先，其次看 school 是否带坐标."""
        lat = getattr(school, "latitude", None)
        lng = getattr(school, "longitude", None)
        if lat and lng:
            return (float(lng), float(lat))
        return None

    def _fetch_graph(self, center: tuple[float, float]) -> Any:
        try:
            import osmnx as ox
        except Exception as exc:  # noqa: BLE001
            raise EngineError(f"import osmnx 失败: {exc}") from exc

        lng, lat = center
        log.info("拉取路网: center=(%.6f, %.6f) r=%.0fm", lng, lat, self.radius_m)
        try:
            graph = ox.graph_from_point(
                (lat, lng),
                dist=self.radius_m,
                network_type=self.network_type,
                simplify=True,
                retain_all=False,
            )
        except Exception as exc:  # noqa: BLE001  网络/数据源问题
            log.warning("拉取路网失败: %s", exc)
            return None
        return graph

    def _extract_loops(
        self,
        graph: Any,
        center: tuple[float, float],
        target_m: float,
        limit: int,
    ) -> list[RoutePlan]:
        """从图里找接近目标长度的闭环."""
        import networkx as nx

        routes: list[RoutePlan] = []

        # 1. 先把环路都找出来（校园道路通常有不少环）
        try:
            cycles = list(nx.simple_cycles(graph.to_directed(), length_bound=200))[:200]
        except Exception:  # noqa: BLE001  simple_cycles 对大规模图可能很慢
            cycles = []

        for cycle in cycles:
            pts = self._cycle_to_points(graph, cycle)
            if len(pts) < 3:
                continue
            if not self._inside(pts, center, self.radius_m * 1.2):
                continue
            length = geo.polyline_length(pts)
            if length < 120:
                continue
            routes.append(RoutePlan(points=pts, source="osm-cycle", name=f"OSM 环路 {length:.0f}m", length_m=length))

        # 2. 没有合适环路就随机游走凑一条
        if not routes:
            walk = self._random_walk(graph, center, target_m)
            if walk:
                routes.append(RoutePlan(points=walk, source="osm-walk", name="OSM 游走路线"))

        routes.sort(key=lambda r: abs(r.length_m - target_m))
        log.info("OSM 生成 %d 条候选路线", len(routes))
        return routes[:limit]

    @staticmethod
    def _cycle_to_points(graph: Any, cycle: list[Any]) -> list[geo.Coord]:
        pts: list[geo.Coord] = []
        for node in cycle:
            data = graph.nodes.get(node) or {}
            lng, lat = data.get("x"), data.get("y")
            if lng is None or lat is None:
                continue
            pts.append((float(lng), float(lat)))
        if len(pts) >= 3:
            pts.append(pts[0])
        return pts

    @staticmethod
    def _inside(points: list[geo.Coord], center: tuple[float, float], radius: float) -> bool:
        clng, clat = center
        return all(geo.haversine(clat, clng, p[1], p[0]) <= radius for p in points)

    def _random_walk(self, graph: Any, center: tuple[float, float], target_m: float) -> list[geo.Coord]:
        """随机游走并尝试回到起点，长度接近目标."""
        rng = random.Random(42)
        clng, clat = center

        start = min(
            graph.nodes,
            key=lambda n: geo.haversine(clat, clng, graph.nodes[n]["y"], graph.nodes[n]["x"]),
        )
        path = [start]
        total = 0.0
        current = start
        guard = 0

        while total < target_m * 1.05 and guard < 5000:
            guard += 1
            neighbors = list(graph.neighbors(current))
            if not neighbors:
                break
            nxt = rng.choice(neighbors)
            a, b = graph.nodes[current], graph.nodes[nxt]
            total += geo.haversine(a["y"], a["x"], b["y"], b["x"])
            path.append(nxt)
            current = nxt

        coords = [(float(graph.nodes[n]["x"]), float(graph.nodes[n]["y"])) for n in path]
        if len(coords) < 2:
            return []
        return geo.normalize_points(coords)
