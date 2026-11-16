"""GPS 噪声模型.

这是最能提升「真实度」的一块。

真实手机 GPS 的定位误差不是独立同分布的高斯白噪声，而是**时间相关**的：
上一秒偏左 3 米，下一秒大概率还在左边 2 米，而不是突然跳到右边。
用 AR(1) 建模，轨迹会自然出现「沿着路走但偶尔偏出去一点」的样子。

上一代实现直接用路线上的精确坐标当轨迹点，等于宣称自己有一个误差为零的 GPS，
这在服务端看就是异常。
"""

from __future__ import annotations

import numpy as np

DEFAULT_SIGMA_M = 3.5     # 手机 GPS 在开阔地典型误差
DEFAULT_ALPHA = 0.85      # 相邻采样点的相关系数，越大越平滑
MAX_OFFSET_M = 12.0       # 偶尔的跳点上限

_M_PER_DEG_LAT = 111_320.0


def ar1_noise(
    n: int,
    *,
    sigma: float = DEFAULT_SIGMA_M,
    alpha: float = DEFAULT_ALPHA,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """生成 AR(1) 噪声序列.

        e[t] = alpha * e[t-1] + (1 - alpha) * eps[t],  eps ~ N(0, sigma^2)

    稳态标准差约为 sigma * sqrt((1-alpha)/(1+alpha))，所以这里对 sigma 做了补偿，
    让最终偏移幅度符合预期。
    """
    rng = rng or np.random.default_rng()
    if n <= 0:
        return np.empty(0)

    # 补偿系数：让稳态方差等于 sigma^2
    innov_sigma = sigma * np.sqrt(max(1e-9, (1 - alpha) / (1 + alpha))) if alpha < 1 else sigma
    eps = rng.normal(0.0, innov_sigma, n)

    out = np.empty(n, dtype=np.float64)
    prev = 0.0
    for i in range(n):
        prev = alpha * prev + eps[i]
        out[i] = prev

    np.clip(out, -MAX_OFFSET_M, MAX_OFFSET_M, out=out)
    return out


def apply_gps_noise(
    lngs: np.ndarray,
    lats: np.ndarray,
    *,
    sigma: float = DEFAULT_SIGMA_M,
    alpha: float = DEFAULT_ALPHA,
    rng: np.random.Generator | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """给整条轨迹加时间相关的定位误差（米制 → 度）."""
    rng = rng or np.random.default_rng()
    n = len(lngs)
    if n == 0:
        return lngs, lats

    east = ar1_noise(n, sigma=sigma, alpha=alpha, rng=rng)
    north = ar1_noise(n, sigma=sigma * 0.9, alpha=alpha, rng=rng)

    mean_lat = float(np.mean(lats)) if n else 0.0
    m_per_deg_lng = max(1.0, _M_PER_DEG_LAT * np.cos(np.radians(mean_lat)))

    return lngs + east / m_per_deg_lng, lats + north / _M_PER_DEG_LAT


def residual_stats(
    lngs_clean: np.ndarray,
    lats_clean: np.ndarray,
    lngs_noisy: np.ndarray,
    lats_noisy: np.ndarray,
) -> dict[str, float]:
    """诊断用：噪声后的位移统计，验证幅度是否合理."""
    from tsn.engine.geo import haversine

    if len(lngs_clean) == 0:
        return {"mean_m": 0.0, "max_m": 0.0}

    dists = [
        haversine(lats_clean[i], lngs_clean[i], lats_noisy[i], lngs_noisy[i])
        for i in range(len(lngs_clean))
    ]
    arr = np.asarray(dists, dtype=np.float64)
    return {"mean_m": float(arr.mean()), "max_m": float(arr.max()), "p95_m": float(np.percentile(arr, 95))}
