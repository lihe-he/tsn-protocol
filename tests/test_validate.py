"""校验器测试 —— 它要能真的抓到问题，而不是永远返回通过."""

from __future__ import annotations

from tsn.core.models import GeoPoint, StepBucket, Track
from tsn.engine import validate


def _make_track(specs: list[tuple[float, float, float, float, float]]) -> Track:
    """(dt, lng, lat, distance_m, speed) → Track."""
    points = []
    cumulative = 0.0
    t = 1_700_000_000_000.0
    for dt, lng, lat, distance, speed in specs:
        t += dt * 1000
        cumulative += distance
        points.append(
            GeoPoint(t_ms=t, lng=lng, lat=lat, distance_m=distance, cumulative_m=cumulative, speed_mps=speed)
        )
    return Track(points=points, target_distance_m=cumulative, plan_use_time_s=(t - 1_700_000_000_000.0) / 1000)


class TestDetectsProblems:
    def test_empty_track(self) -> None:
        report = validate.validate_track(Track())
        assert not report.ok
        assert report.errors[0].code == "empty_track"

    def test_single_point(self) -> None:
        track = _make_track([(0, 104.0, 30.0, 0, 0)])
        assert not validate.validate_track(track).ok

    def test_timestamp_backwards(self) -> None:
        track = _make_track([(0, 104.0, 30.0, 0, 2.0), (1, 104.0, 30.0001, 2.0, 2.0)])
        track.points[1].t_ms = track.points[0].t_ms - 1000
        report = validate.validate_track(track)
        assert any(i.code == "timestamp_not_increasing" for i in report.errors)

    def test_speed_distance_mismatch(self) -> None:
        # 声称 2 m/s 走 1 秒，却记了 100 米
        track = _make_track([(0, 104.0, 30.0, 0, 2.0), (1, 104.0, 30.0001, 100.0, 2.0)])
        report = validate.validate_track(track)
        assert any(i.code == "speed_distance_mismatch" for i in report.errors)

    def test_distance_below_requirement(self, setting) -> None:
        track = _make_track([(0, 104.0, 30.0, 0, 2.0), (1, 104.0, 30.0001, 2.0, 2.0)])
        report = validate.validate_track(track, setting=setting)
        assert any(i.code == "distance_below_requirement" for i in report.errors)

    def test_bucket_sum_mismatch(self) -> None:
        track = _make_track([(0, 104.0, 30.0, 0, 2.0), (1, 104.0, 30.0001, 2.0, 2.0)])
        bad = StepBucket(buckets=[10, 20], total=999)
        report = validate.validate_track(track, bucket=bad)
        assert any(i.code == "bucket_sum_mismatch" for i in report.errors)


class TestAcceptsValid:
    def test_consistent_small_track(self) -> None:
        specs = [(0, 104.0, 30.0, 0.0, 2.0)]
        for i in range(1, 60):
            specs.append((1, 104.0 + i * 0.00002, 30.0, 2.0, 2.0))
        track = _make_track(specs)
        report = validate.validate_track(track)
        assert report.ok, report.summary()

    def test_warnings_do_not_fail(self) -> None:
        """弦长偏差只是 warning，不应导致整体失败."""
        track = _make_track(
            [(0, 104.0, 30.0, 0, 2.0), (1, 104.0, 30.0001, 2.0, 2.0), (1, 104.0, 30.0002, 2.0, 2.0)]
        )
        report = validate.validate_track(track)
        assert report.ok
