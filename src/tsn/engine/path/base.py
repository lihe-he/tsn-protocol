"""路线来源的统一接口.

上一代实现硬绑定「历史记录」这一种来源，导致新账号完全没法用。
抽成接口后可以随时加来源（历史、OSM、手工坐标、别人的路线）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from tsn.core.models import School
from tsn.engine import geo


@dataclass(slots=True)
class RoutePlan:
    """一条可用于重采样的路线."""

    points: list[geo.Coord]
    source: str = "unknown"
    name: str = ""
    length_m: float = 0.0
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.points and not self.length_m:
            self.length_m = geo.polyline_length(self.points)

    def __len__(self) -> int:
        return len(self.points)


class PathSource(Protocol):
    """路线来源."""

    name: str

    async def get_routes(
        self,
        school: School,
        *,
        target_m: float,
        limit: int = 10,
    ) -> list[RoutePlan]:
        """返回若干候选路线，按与目标距离的接近程度排序."""
        ...


def rank_by_distance(routes: Sequence[RoutePlan], target_m: float, limit: int) -> list[RoutePlan]:
    """按与目标距离的差值排序，取前 limit 条."""
    if not routes:
        return []
    ordered = sorted(routes, key=lambda r: abs(r.length_m - target_m))
    return list(ordered[:limit])
