"""步幅 / 步频模型.

真实跑者的步幅与速度近似线性相关，且步频集中在 150~185 spm。
用一个经验回归 + 合理限幅就够，不需要生物力学模型。
"""

from __future__ import annotations

STRIDE_MIN = 0.55   # 米，快走下限
STRIDE_MAX = 1.75   # 米，冲刺上限
STRIDE_INTERCEPT = 0.65
STRIDE_SLOPE = 0.24  # 每 m/s 增加的步幅


def stride_for_speed(speed_mps: float) -> float:
    """速度 → 步幅（米）.

    经验关系：慢走步幅 ~0.6m，5'00" 配速（3.3m/s）约 1.45m。
    """
    if speed_mps <= 0:
        return STRIDE_MIN
    stride = STRIDE_INTERCEPT + STRIDE_SLOPE * speed_mps
    return max(STRIDE_MIN, min(STRIDE_MAX, stride))


def cadence_for_speed(speed_mps: float) -> float:
    """速度 → 步频（步/分钟）."""
    stride = stride_for_speed(speed_mps)
    if stride <= 0:
        return 0.0
    return speed_mps / stride * 60.0


def steps_for_distance(distance_m: float, speed_mps: float) -> float:
    """一段距离在给定速度下对应多少步."""
    stride = stride_for_speed(speed_mps)
    if stride <= 0:
        return 0.0
    return distance_m / stride


def is_plausible(stride_m: float, cadence_spm: float) -> bool:
    """一组步幅/步频是否落在人类合理范围内."""
    return STRIDE_MIN * 0.8 <= stride_m <= STRIDE_MAX * 1.1 and 110 <= cadence_spm <= 210
