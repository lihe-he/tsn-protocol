"""配速模型.

不要用「均匀速度 + 高斯扰动」——真实跑者的配速有结构：
    基础配速 + 高频微抖 + 低频间歇（过路口、喝水、看手机）

均匀速度的轨迹在速度曲线上是一条平线，看一眼就知道是机器生成的。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class PaceProfile:
    """一次跑步的配速画像."""

    base_sec_per_km: float = 330.0     # 5'30"
    jitter: float = 0.07               # 高频微抖，标准差
    relax_every_s: tuple[float, float] = (50.0, 140.0)
    relax_ratio: float = 0.35          # 放松段速度降到 65%
    relax_len_s: tuple[float, float] = (4.0, 15.0)
    warmup_s: float = 20.0             # 起步加速段
    cooldown_s: float = 15.0           # 收尾减速段

    @property
    def base_speed_mps(self) -> float:
        if self.base_sec_per_km <= 0:
            return 2.8
        return 1000.0 / self.base_sec_per_km

    @classmethod
    def from_pace_range(cls, lo_sec_per_km: float, hi_sec_per_km: float, rng: np.random.Generator) -> PaceProfile:
        """在给定配速区间里随机取一个，用作一次跑步的基线."""
        base = float(rng.uniform(min(lo_sec_per_km, hi_sec_per_km), max(lo_sec_per_km, hi_sec_per_km)))
        return cls(base_sec_per_km=base)


def speed_factors(
    n: int,
    interval_s: float,
    profile: PaceProfile,
    rng: np.random.Generator,
) -> np.ndarray:
    """生成 n 个采样点的速度系数（1.0 = 基准速度）.

    合成三个分量：
        1. 高频抖动     —— 每步的落地差异
        2. 低频间歇     —— 随机插入的明显降速段
        3. 首尾斜坡     —— 起步加速、收尾减速
    """
    if n <= 0:
        return np.empty(0)

    total_s = n * interval_s
    factors = rng.normal(1.0, profile.jitter, n)

    # 低频间歇
    cursor = float(rng.uniform(0, profile.relax_every_s[1]))
    while cursor < total_s:
        length = float(rng.uniform(*profile.relax_len_s))
        start = int(cursor / interval_s)
        end = min(n, start + max(1, int(length / interval_s)))
        if start < n:
            factors[start:end] *= 1.0 - profile.relax_ratio
        cursor += float(rng.uniform(*profile.relax_every_s))

    # 首尾斜坡
    warm = int(profile.warmup_s / interval_s)
    for i in range(min(warm, n)):
        factors[i] *= 0.80 + 0.20 * (i / max(1, warm))

    cool = int(profile.cooldown_s / interval_s)
    for i in range(min(cool, n)):
        idx = n - 1 - i
        if idx >= 0:
            factors[idx] *= 0.80 + 0.20 * (i / max(1, cool))

    # 限幅，避免出现 3 分配速这种明显不合理的值
    np.clip(factors, 0.55, 1.45, out=factors)
    return factors


def realistic_pace_label(sec_per_km: float) -> str:
    """把秒/公里转成 5'30" 这种格式."""
    sec = max(0.0, sec_per_km)
    return f"{int(sec // 60)}'{int(sec % 60):02d}\""
