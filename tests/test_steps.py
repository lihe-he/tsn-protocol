"""60 秒步数分桶测试.

核心不变量：**各桶之和必须精确等于目标步数**。
服务端按分钟核对，差一步都可能被判异常。
"""

from __future__ import annotations

import pytest

from tsn.engine import steps


class TestAllocate:
    def test_exact_total(self) -> None:
        intervals = [
            steps.StepInterval(0, 60, 80.0),
            steps.StepInterval(60, 60, 82.0),
            steps.StepInterval(120, 60, 78.5),
        ]
        alloc = steps.allocate(intervals)
        assert sum(alloc.buckets) == alloc.total
        assert alloc.total in (240, 241)  # round(240.5)

    def test_forced_total_is_exact(self) -> None:
        intervals = [steps.StepInterval(0, 600, 2400.0)]
        alloc = steps.allocate(intervals, total_steps=2500)
        assert sum(alloc.buckets) == 2500
        assert alloc.total == 2500

    def test_bucket_count_matches_duration(self) -> None:
        intervals = [steps.StepInterval(0, 600, 2400.0)]
        alloc = steps.allocate(intervals)
        assert len(alloc.buckets) == 10  # 600s / 60s

    def test_distribution_is_proportional(self) -> None:
        """前 5 分钟慢、后 5 分钟快的段，后 5 桶应该步数更多."""
        intervals = [
            steps.StepInterval(0, 300, 900.0),
            steps.StepInterval(300, 300, 1500.0),
        ]
        alloc = steps.allocate(intervals)
        first_half = sum(alloc.buckets[:5])
        second_half = sum(alloc.buckets[5:])
        assert second_half > first_half

    def test_no_bucket_is_absurdly_large(self) -> None:
        intervals = [steps.StepInterval(0, 1800, 7500.0)]
        alloc = steps.allocate(intervals)
        assert max(alloc.buckets) < alloc.total * 0.2

    def test_empty_intervals(self) -> None:
        alloc = steps.allocate([])
        assert alloc.buckets == []
        assert alloc.total == 0

    def test_zero_duration_interval(self) -> None:
        intervals = [steps.StepInterval(30, 0, 10.0)]
        alloc = steps.allocate(intervals)
        assert sum(alloc.buckets) == 10

    def test_total_mismatch_raises(self) -> None:
        """差异过大说明上游没先缩放，必须报错而不是静默改数据."""
        intervals = [steps.StepInterval(0, 60, 100.0)]
        with pytest.raises(ValueError, match="差异超过"):
            steps.allocate(intervals, total_steps=500)

    def test_negative_interval_rejected(self) -> None:
        with pytest.raises(ValueError):
            steps.StepInterval(-1, 60, 10.0)

    def test_non_finite_rejected(self) -> None:
        with pytest.raises(ValueError):
            steps.StepInterval(0, 60, float("nan"))


class TestLargestRemainder:
    def test_sum_matches_target(self) -> None:
        out = steps._largest_remainder([1.4, 2.4, 3.2], 10)
        assert sum(out) == 10

    def test_floor_when_exact(self) -> None:
        out = steps._largest_remainder([1.0, 2.0, 3.0], 6)
        assert out == [1, 2, 3]

    def test_large_remainder_rotates(self) -> None:
        out = steps._largest_remainder([0.1, 0.1], 10)
        assert sum(out) == 10
