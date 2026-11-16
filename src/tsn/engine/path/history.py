"""历史记录路线源.

这是「最真实」的来源：用账号自己跑过的路线重采样，服务端见过的轨迹形态
和这次提交的形态是同一类，不会触发「路线不像校园」的判断。

代价是需要账号有历史记录。没有的话用 OSMPathSource。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tsn.core.models import School
from tsn.engine import geo
from tsn.engine.path.base import RoutePlan, rank_by_distance

log = logging.getLogger("tsn.engine.path.history")

RecordFetcher = Callable[[], Awaitable[list[dict[str, Any]]]]


def parse_track_points(record: dict[str, Any]) -> list[geo.Coord]:
    """从一条记录里抽出轨迹点.

    服务端存轨迹的字段在不同版本里叫法不同：
        gitudeLatitude / trackPoints / pointList
    内容可能是 JSON 字符串或已解析的 list，点可能是对象或 [lng,lat]。
    """
    raw = record.get("gitudeLatitude") or record.get("trackPoints") or record.get("pointList")
    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except ValueError:
            return []

    if not isinstance(raw, list):
        return []

    out: list[geo.Coord] = []
    for item in raw:
        if isinstance(item, dict):
            lng = item.get("longitude", item.get("lng"))
            lat = item.get("latitude", item.get("lat"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            lng, lat = item[0], item[1]
        else:
            continue
        try:
            out.append((float(lng), float(lat)))
        except (TypeError, ValueError):
            continue
    return geo.normalize_points(out)


@dataclass(slots=True)
class HistoryPathSource:
    """从账号历史记录里取路线."""

    fetch: RecordFetcher
    name: str = "history"

    async def get_routes(
        self,
        school: School,
        *,
        target_m: float,
        limit: int = 10,
    ) -> list[RoutePlan]:
        try:
            records = await self.fetch()
        except Exception as exc:  # noqa: BLE001  历史记录拉取失败不应中断主流程
            log.warning("拉取历史记录失败: %s", exc)
            return []

        if not records:
            log.info("账号没有历史记录，无法从历史取路线")
            return []

        routes: list[RoutePlan] = []
        for idx, rec in enumerate(records):
            points = parse_track_points(rec)
            if len(points) < 2:
                continue
            distance = rec.get("sportRange") or rec.get("distance")
            try:
                length_hint = float(distance) * 1000.0 if distance else 0.0
            except (TypeError, ValueError):
                length_hint = 0.0
            routes.append(
                RoutePlan(
                    points=points,
                    source="history",
                    name=f"历史记录 #{idx + 1}",
                    length_m=length_hint,
                )
            )

        log.info("从历史记录解析出 %d 条候选路线", len(routes))
        return rank_by_distance(routes, target_m, limit)
