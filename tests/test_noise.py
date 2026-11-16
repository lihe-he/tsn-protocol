"""GPS 噪声模型测试.

要验证的不只是「有噪声」，而是「噪声在时间上是相关的」——
这正是真实 GPS 和劣质伪造的区别。
"""

from __future__ import annotations

import numpy as np
import pytest

from tsn.engine.noise import gps


class TestAR1:
    def test_length(self) -> None:
        rng = np.random.default_rng(1)
        out = gps.ar1_noise(100, rng=rng)
        assert len(out) == 100

    def test_empty(self) -> None:
        assert len(gps.ar1_noise(0)) == 0

    def test_bounded(self) -> None:
        rng = np.random.default_rng(2)
        out = gps.ar1_noise(5000, sigma=3.5, alpha=0.85, rng=rng)
        assert np.max(np.abs(out)) <= gps.MAX_OFFSET_M + 1e-9

    def test_temporal_correlation(self) -> None:
        """lag-1 自相关应接近 alpha，而不是 0."""
        rng = np.random.default_rng(3)
        out = gps.ar1_noise(20000, sigma=3.5, alpha=0.85, rng=rng)
        corr = np.corrcoef(out[:-1], out[1:])[0, 1]
        assert corr > 0.7, f"相邻点相关性太低: {corr:.3f}"

    def test_high_alpha_is_smoother(self) -> None:
        rng1 = np.random.default_rng(4)
        rng2 = np.random.default_rng(4)
        rough = gps.ar1_noise(5000, alpha=0.2, rng=rng1)
        smooth = gps.ar1_noise(5000, alpha=0.95, rng=rng2)
        d_rough = np.mean(np.abs(np.diff(rough)))
        d_smooth = np.mean(np.abs(np.diff(smooth)))
        assert d_smooth < d_rough

    def test_magnitude_reasonable(self) -> None:
        rng = np.random.default_rng(5)
        out = gps.ar1_noise(20000, sigma=3.5, alpha=0.85, rng=rng)
        assert 1.0 < np.std(out) < 6.0


class TestApply:
    def test_shapes_preserved(self) -> None:
        rng = np.random.default_rng(6)
        lngs = np.linspace(104.0, 104.001, 50)
        lats = np.linspace(30.0, 30.001, 50)
        nlng, nlat = gps.apply_gps_noise(lngs, lats, rng=rng)
        assert len(nlng) == 50 and len(nlat) == 50

    def test_offset_in_meters(self) -> None:
        rng = np.random.default_rng(7)
        lngs = np.full(2000, 104.0)
        lats = np.full(2000, 30.0)
        nlng, nlat = gps.apply_gps_noise(lngs, lats, sigma=3.5, rng=rng)
        stats = gps.residual_stats(lngs, lats, nlng, nlat)
        assert 0.5 < stats["mean_m"] < 8.0
        assert stats["max_m"] < 25.0

    def test_empty_input(self) -> None:
        rng = np.random.default_rng(8)
        out = gps.apply_gps_noise(np.array([]), np.array([]), rng=rng)
        assert len(out[0]) == 0
