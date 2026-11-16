"""轨迹生成测试.

这是整个项目最关键的测试：生成的轨迹必须自洽。
如果这里过了但服务端拒了，说明规则理解有偏差；如果这里没过，
提交上去必然被拒，所以先在这一层拦住。
"""

from __future__ import annotations

import numpy as np
import pytest

from tsn.core.errors import EngineError
from tsn.engine import geo, track as track_mod, validate


class TestPathWalker:
    def test_current_on_segment(self, square_route) -> None:
        w = track_mod.PathWalker(square_route, "pingpong")
        p = w.current()
        assert p == pytest.approx(square_route[0])

    def test_advance_moves_point(self, square_route) -> None:
        w = track_mod.PathWalker(square_route, "pingpong")
        start = w.current()
        w.advance(50)
        assert w.current() != start

    def test_pingpong_reverses(self, square_route) -> None:
        w = track_mod.PathWalker(square_route, "pingpong")
        w.advance(geo.polyline_length(square_route) + 10)
        assert w.laps >= 1

    def test_repeat_closes_loop(self, square_route) -> None:
        w = track_mod.PathWalker(square_route[:-1], "repeat")
        assert w.points[0] == w.points[-1]

    def test_requires_two_points(self) -> None:
        with pytest.raises(EngineError):
            track_mod.PathWalker([(104.0, 30.0)], "pingpong")


class TestGenerate:
    def test_distance_is_target(self, square_route, track_options, rng) -> None:
        trk, _ = track_mod.generate(
            square_route,
            target_distance_m=2000,
            start_ts_ms=1_700_000_000_000,
            plan_use_time_s=1200,
            options=track_options,
            rng=rng,
        )
        assert trk.total_distance_m == pytest.approx(2000, abs=2.0)

    def test_timestamps_increase(self, square_route, track_options, rng) -> None:
        trk, _ = track_mod.generate(
            square_route, target_distance_m=1000, start_ts_ms=0, plan_use_time_s=600,
            options=track_options, rng=rng,
        )
        ts = [p.t_ms for p in trk.points]
        assert all(b > a for a, b in zip(ts, ts[1:]))

    def test_cumulative_monotonic(self, square_route, track_options, rng) -> None:
        trk, _ = track_mod.generate(
            square_route, target_distance_m=1500, start_ts_ms=0, plan_use_time_s=900,
            options=track_options, rng=rng,
        )
        cum = [p.cumulative_m for p in trk.points]
        assert all(b >= a for a, b in zip(cum, cum[1:]))

    def test_elapsed_matches_plan(self, square_route, track_options, rng) -> None:
        trk, _ = track_mod.generate(
            square_route, target_distance_m=2000, start_ts_ms=0, plan_use_time_s=1200,
            options=track_options, rng=rng,
        )
        assert trk.elapsed_s == pytest.approx(1200, rel=0.05)

    def test_pace_label_format(self, square_route, track_options, rng) -> None:
        trk, _ = track_mod.generate(
            square_route, target_distance_m=2000, start_ts_ms=0, plan_use_time_s=1200,
            options=track_options, rng=rng,
        )
        label = trk.pace_label()
        assert "'" in label and '"' in label

    def test_noise_changes_coords(self, square_route, track_options, rng) -> None:
        opts_clean = track_mod.TrackOptions.from_config(__import__("tsn.core.config", fromlist=["EngineConfig"]).EngineConfig())
        opts_clean.apply_noise = False
        opts_noisy = track_mod.TrackOptions.from_config(__import__("tsn.core.config", fromlist=["EngineConfig"]).EngineConfig())
        opts_noisy.apply_noise = True

        clean, _ = track_mod.generate(
            square_route, target_distance_m=500, start_ts_ms=0, plan_use_time_s=300,
            options=opts_clean, rng=np.random.default_rng(1),
        )
        noisy, _ = track_mod.generate(
            square_route, target_distance_m=500, start_ts_ms=0, plan_use_time_s=300,
            options=opts_noisy, rng=np.random.default_rng(1),
        )
        assert clean.points[0].lng != noisy.points[0].lng

    def test_distance_unaffected_by_noise(self, square_route, track_options, rng) -> None:
        """噪声加在坐标上，但累计距离必须仍等于目标（距离用无噪声坐标算）."""
        trk, _ = track_mod.generate(
            square_route, target_distance_m=1000, start_ts_ms=0, plan_use_time_s=600,
            options=track_options, rng=rng,
        )
        assert trk.total_distance_m == pytest.approx(1000, abs=2.0)

    def test_rejects_bad_distance(self, square_route, track_options, rng) -> None:
        with pytest.raises(EngineError):
            track_mod.generate(square_route, target_distance_m=0, start_ts_ms=0, plan_use_time_s=100, rng=rng)

    def test_rejects_bad_duration(self, square_route, track_options, rng) -> None:
        with pytest.raises(EngineError):
            track_mod.generate(square_route, target_distance_m=100, start_ts_ms=0, plan_use_time_s=0, rng=rng)

    def test_rejects_short_route(self, track_options, rng) -> None:
        with pytest.raises(EngineError):
            track_mod.generate([(104.0, 30.0)], target_distance_m=100, start_ts_ms=0, plan_use_time_s=60, rng=rng)


class TestGeneratedTrackValidates:
    def test_passes_own_validator(self, square_route, track_options, rng, setting) -> None:
        """自产自检：生成的轨迹（补齐步数后）必须能过自己的校验器."""
        trk, bucket = track_mod.generate(
            square_route,
            target_distance_m=setting.total_range_m,
            start_ts_ms=1_700_000_000_000,
            plan_use_time_s=1200,
            options=track_options,
            rng=rng,
        )
        # 学校要求 1250 步/km，比自然步频高，按 client.prepare 的做法补齐
        need = setting.required_steps_for(trk.total_distance_m)
        if bucket.total < need:
            bucket = track_mod.build_steps(trk, force_total=need)

        report = validate.validate_track(trk, bucket=bucket, setting=setting)
        assert report.ok, report.summary()

    def test_steps_reach_requirement(self, square_route, track_options, rng, setting) -> None:
        trk, _ = track_mod.generate(
            square_route, target_distance_m=2000, start_ts_ms=0, plan_use_time_s=1200,
            options=track_options, rng=rng,
        )
        need = setting.required_steps_for(trk.total_distance_m)
        bucket = track_mod.build_steps(trk, force_total=need)
        assert bucket.total >= need
