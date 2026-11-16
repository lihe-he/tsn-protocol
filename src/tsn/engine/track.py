"""轨迹生成主流程.

输入：一条真实路线 + 目标距离 + 起跑时间
输出：一条自洽的轨迹（点、时间戳、速度、累计距离）+ 60 秒步数桶

设计要点：
    · 所有中间量都显式记录在 GeoPoint 里，方便 validate 交叉核对
    · 速度系数来自 pace 模型，不是白噪声
    · GPS 噪声最后一步才加，避免污染距离计算
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from tsn.core.config import EngineConfig
from tsn.core.errors import EngineError
from tsn.core.models import GeoPoint, StepBucket, Track
from tsn.engine import geo, steps
from tsn.engine.noise import gps as gps_noise
from tsn.engine.physics import pace as pace_mod
from tsn.engine.physics import stride as stride_mod
from tsn.engine.steps import BucketAllocation

log = logging.getLogger("tsn.engine.track")


@dataclass(slots=True)
class TrackOptions:
    """一次生成的参数."""

    sample_interval_s: float = 1.0
    gps_sigma_m: float = 3.5
    gps_alpha: float = 0.85
    apply_noise: bool = True
    loop_mode: str = "pingpong"     # pingpong=折返, repeat=回到起点循环
    max_samples: int = 200_000
    tail_pause_points: int = 2
    pace: pace_mod.PaceProfile = field(default_factory=pace_mod.PaceProfile)

    @classmethod
    def from_config(cls, cfg: EngineConfig) -> TrackOptions:
        return cls(
            sample_interval_s=cfg.sample_interval_s,
            gps_sigma_m=cfg.gps_sigma_m,
            gps_alpha=cfg.gps_ar_alpha,
            loop_mode=cfg.loop_mode,
            max_samples=cfg.max_samples,
            tail_pause_points=cfg.tail_pause_points,
            pace=pace_mod.PaceProfile(
                jitter=cfg.speed_jitter,
                relax_len_s=cfg.pace_relax_len_s,
            ),
        )


class PathWalker:
    """沿折线前进，走完按 loop_mode 处理，支持任意里程."""

    __slots__ = ("points", "loop_mode", "seg_lengths", "seg_index", "offset", "laps")

    def __init__(self, points: list[geo.Coord], loop_mode: str) -> None:
        if len(points) < 2:
            raise EngineError("路线至少需要两个点")
        self.points = list(points)
        self.loop_mode = loop_mode
        self.seg_index = 0
        self.offset = 0.0
        self.laps = 0
        self.seg_lengths: list[float] = []

        if loop_mode == "repeat":
            closing = geo.haversine(
                self.points[-1][1], self.points[-1][0], self.points[0][1], self.points[0][0]
            )
            if closing > 1e-9:
                self.points.append(self.points[0])
        self._rebuild()

    def _rebuild(self) -> None:
        self.seg_lengths = [
            geo.haversine(self.points[i][1], self.points[i][0], self.points[i + 1][1], self.points[i + 1][0])
            for i in range(len(self.points) - 1)
        ]

    def current(self) -> geo.Coord:
        if self.seg_index >= len(self.seg_lengths):
            return self.points[-1]
        a, b = self.points[self.seg_index], self.points[self.seg_index + 1]
        seg = self.seg_lengths[self.seg_index]
        if seg <= 0:
            return a
        ratio = min(1.0, max(0.0, self.offset / seg))
        proj = geo.LocalProjection(a[0], a[1])
        return proj.interpolate(a, b, ratio)

    def advance(self, meters: float) -> None:
        remaining = meters
        guard = 0
        while remaining > 1e-9 and guard < 100_000:
            guard += 1
            if self.seg_index >= len(self.seg_lengths):
                self._wrap()
                continue
            seg = self.seg_lengths[self.seg_index]
            if seg <= 0:
                self.seg_index += 1
                self.offset = 0.0
                continue
            available = seg - self.offset
            if available > remaining:
                self.offset += remaining
                remaining = 0.0
            else:
                remaining -= available
                self.seg_index += 1
                self.offset = 0.0

    def _wrap(self) -> None:
        if self.loop_mode == "repeat":
            self.seg_index = 0
            self.offset = 0.0
            self.laps += 1
            return
        if self.loop_mode == "pingpong":
            self.points.reverse()
            self._rebuild()
            self.seg_index = 0
            self.offset = 0.0
            self.laps += 1
            return
        raise EngineError(f"未知 loop_mode: {self.loop_mode}")


def generate(
    route: list[geo.Coord],
    *,
    target_distance_m: float,
    start_ts_ms: float,
    plan_use_time_s: float,
    options: TrackOptions | None = None,
    rng: np.random.Generator | None = None,
) -> tuple[Track, BucketAllocation]:
    """生成轨迹 + 步数桶."""
    if target_distance_m <= 0:
        raise EngineError(f"目标距离必须为正: {target_distance_m}")
    if plan_use_time_s <= 0:
        raise EngineError(f"计划用时必须为正: {plan_use_time_s}")

    opts = options or TrackOptions()
    rng = rng or np.random.default_rng()

    cleaned = geo.normalize_points(route)
    if len(cleaned) < 2:
        raise EngineError("路线清洗后不足两个点")

    base_speed = target_distance_m / plan_use_time_s
    if base_speed <= 0 or not math.isfinite(base_speed):
        raise EngineError(f"基准速度非法: {base_speed}")

    est_samples = max(2, min(opts.max_samples, int(plan_use_time_s / opts.sample_interval_s) + 8))
    factors = pace_mod.speed_factors(est_samples, opts.sample_interval_s, opts.pace, rng)

    walker = PathWalker(cleaned, opts.loop_mode)
    points: list[GeoPoint] = []
    traveled = 0.0
    elapsed = 0.0
    i = 0

    while traveled < target_distance_m - 1e-9:
        if len(points) >= opts.max_samples:
            raise EngineError(f"采样点超过上限 {opts.max_samples}")

        factor = float(factors[i % len(factors)])
        i += 1
        speed = base_speed * factor
        if speed <= 0:
            raise EngineError("采样速度非正")

        dt = opts.sample_interval_s
        nominal = speed * dt
        remaining = target_distance_m - traveled
        if nominal >= remaining:
            dt = remaining / speed
            nominal = remaining

        walker.advance(nominal)
        elapsed += dt
        traveled += nominal
        lng, lat = walker.current()

        points.append(
            GeoPoint(
                t_ms=start_ts_ms + elapsed * 1000.0,
                lng=lng,
                lat=lat,
                distance_m=nominal,
                cumulative_m=traveled,
                speed_mps=speed,
            )
        )

    # 尾部几秒标为「已停」，真实跑完会站着喘气
    for p in points[-opts.tail_pause_points :] if opts.tail_pause_points else []:
        p.paused = True

    # GPS 噪声最后加：距离用的是无噪声坐标，避免累计距离被噪声污染
    if opts.apply_noise and len(points) > 1:
        lngs = np.array([p.lng for p in points], dtype=np.float64)
        lats = np.array([p.lat for p in points], dtype=np.float64)
        nlng, nlat = gps_noise.apply_gps_noise(
            lngs, lats, sigma=opts.gps_sigma_m, alpha=opts.gps_alpha, rng=rng
        )
        for p, lng, lat in zip(points, nlng, nlat, strict=True):
            p.lng, p.lat = float(lng), float(lat)

    track = Track(
        points=points,
        target_distance_m=target_distance_m,
        base_speed_mps=base_speed,
        plan_use_time_s=plan_use_time_s,
        start_ts_ms=start_ts_ms,
    )

    bucket = build_steps(track)
    log.info(
        "生成轨迹：%.0f m / %.0f s / %d 点 / 均速 %.2f m/s / 配速 %s",
        track.total_distance_m,
        track.elapsed_s,
        len(points),
        track.avg_speed_mps,
        track.pace_label(),
    )
    return track, bucket


def build_steps(track: Track, *, force_total: int | None = None) -> BucketAllocation:
    """按轨迹算 60 秒步数桶.

    force_total：服务端要求的最低步数。自然步数不够时按比例放大，
    而不是直接改桶——放大后各分钟仍然是按时间分布，不会出现某分钟暴增。
    """
    intervals: list[steps.StepInterval] = []
    prev_offset = 0.0
    for p in track.points:
        offset = (p.t_ms - track.start_ts_ms) / 1000.0
        duration = max(0.0, offset - prev_offset)
        natural = stride_mod.steps_for_distance(p.distance_m, p.speed_mps)
        intervals.append(steps.StepInterval(start_offset_s=prev_offset, duration_s=duration, steps=natural))
        prev_offset = offset

    natural_total = int(round(sum(iv.steps for iv in intervals)))

    if force_total is not None and force_total > natural_total > 0:
        scale = force_total / natural_total
        intervals = [
            steps.StepInterval(
                start_offset_s=iv.start_offset_s,
                duration_s=iv.duration_s,
                steps=iv.steps * scale,
            )
            for iv in intervals
        ]

    return steps.allocate(intervals, total_steps=force_total)


def plan_duration(distance_m: float, sec_per_km: float) -> float:
    """按配速算计划用时（秒）."""
    return distance_m / 1000.0 * sec_per_km
