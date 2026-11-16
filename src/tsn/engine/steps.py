"""60 秒步数分桶.

服务端按分钟粒度核对步数，所以不能只给一个总数 —— 必须给一个
「第 1 分钟 N1 步、第 2 分钟 N2 步…」的数组，且总和精确等于要求步数。

分配用最大余数法，保证：
    - 总和 == 目标值（一分不差）
    - 各桶之间按时间比例分布（不会出现某分钟骤增）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

DEFAULT_BUCKET_SECONDS = 60.0


@dataclass(slots=True)
class StepInterval:
    """一段时间内的步数."""

    start_offset_s: float
    duration_s: float
    steps: float

    def __post_init__(self) -> None:
        for name, value in (
            ("start_offset_s", self.start_offset_s),
            ("duration_s", self.duration_s),
            ("steps", self.steps),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} 必须是有限数值")
            if value < 0:
                raise ValueError(f"{name} 不能为负")


@dataclass(slots=True)
class BucketAllocation:
    buckets: list[int] = field(default_factory=list)
    total: int = 0
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS

    def as_json(self) -> str:
        import json

        return json.dumps(self.buckets, separators=(",", ":"))

    @property
    def count(self) -> int:
        return len(self.buckets)

    def summary(self) -> str:
        return f"{self.count} 桶 / 合计 {self.total} 步 / 最小 {min(self.buckets or [0])} 最大 {max(self.buckets or [0])}"


def allocate(
    intervals: list[StepInterval],
    *,
    bucket_seconds: float = DEFAULT_BUCKET_SECONDS,
    total_steps: int | None = None,
) -> BucketAllocation:
    """把各段步数摊到 60 秒桶里."""
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds 必须为正")
    if not intervals:
        return BucketAllocation(buckets=[], total=0, bucket_seconds=bucket_seconds)

    n_buckets = 1
    for iv in intervals:
        end = iv.start_offset_s + iv.duration_s
        touched = (
            max(1, int(math.ceil(end / bucket_seconds)))
            if iv.duration_s > 0
            else int(iv.start_offset_s // bucket_seconds) + 1
        )
        n_buckets = max(n_buckets, touched)

    acc = [0.0] * n_buckets
    for iv in intervals:
        if iv.duration_s <= 0 or iv.steps <= 0:
            if iv.steps > 0:
                idx = min(n_buckets - 1, int(iv.start_offset_s // bucket_seconds))
                acc[idx] += iv.steps
            continue

        t = max(0.0, iv.start_offset_s)
        left = iv.duration_s
        guard = 0
        while left > 1e-9 and guard < 10_000:
            guard += 1
            idx = min(n_buckets - 1, int(t // bucket_seconds))
            boundary = (idx + 1) * bucket_seconds
            seg = min(left, boundary - t)
            if seg <= 0:
                seg = left
            acc[idx] += iv.steps * (seg / iv.duration_s)
            t += seg
            left -= seg

    expected = int(round(sum(iv.steps for iv in intervals)))
    if total_steps is not None:
        target = int(total_steps)
        # 允许调用方为了满足服务端步数要求而做小幅补齐；
        # 但如果差得离谱，说明上游逻辑有 bug，宁可报错也不要静默改数据。
        if expected > 0 and abs(target - expected) > max(2, int(expected * 0.2)):
            raise ValueError(
                f"total_steps({target}) 与各段合计({expected}) 差异超过 20%；"
                "请先在上游按比例缩放区间步数"
            )
        expected = target

    buckets = _largest_remainder(acc, expected)
    return BucketAllocation(buckets=buckets, total=sum(buckets), bucket_seconds=bucket_seconds)


def _largest_remainder(values: list[float], target: int) -> list[int]:
    """最大余数法取整：先floor，再把差额按小数部分从大到小补."""
    if not values:
        return []
    floors = [int(v) for v in values]
    remainder = target - sum(floors)

    if remainder == 0:
        return floors

    if remainder > 0:
        order = sorted(range(len(values)), key=lambda i: values[i] - floors[i], reverse=True)
        for i in order[:remainder]:
            floors[i] += 1
        # 余数大于桶数时继续轮转
        i = 0
        while sum(floors) < target and i < len(floors) * 10:
            floors[i % len(floors)] += 1
            i += 1
        return floors

    need = -remainder
    order = sorted(range(len(values)), key=lambda i: values[i] - floors[i])
    for i in order:
        if need <= 0:
            break
        if floors[i] > 0:
            floors[i] -= 1
            need -= 1
    return floors


def intervals_from_samples(samples: list[tuple[float, float, float]]) -> list[StepInterval]:
    """(start_s, duration_s, steps) 三元组列表 → StepInterval 列表."""
    return [StepInterval(start_offset_s=s, duration_s=d, steps=n) for s, d, n in samples]
