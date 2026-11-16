"""提交字段构造测试.

字段错了服务端会静默拒绝，所以每个字段的取值都要锁住。
"""

from __future__ import annotations

from tsn.adapters.v2023 import payload as p
from tsn.core.models import (
    ExerciseSetting,
    GeoPoint,
    RunDraft,
    RunType,
    School,
    StepBucket,
    Track,
)


def _draft(setting: ExerciseSetting) -> RunDraft:
    points = []
    t = 1_700_000_000_000.0
    cum = 0.0
    for i in range(1200):
        t += 1000
        cum += 1.67
        points.append(GeoPoint(t_ms=t, lng=104.0 + i * 0.00001, lat=30.0, distance_m=1.67, cumulative_m=cum, speed_mps=1.67))
    track = Track(
        points=points,
        target_distance_m=cum,
        base_speed_mps=1.67,
        plan_use_time_s=1200,
        start_ts_ms=points[0].t_ms,
    )
    return RunDraft(
        school=School(school_id=1, name="测试大学", code="test"),
        run_type=RunType.SUN,
        setting=setting,
        track=track,
        steps=StepBucket(buckets=[100] * 20, total=2000),
        start_ts_ms=points[0].t_ms,
        end_ts_ms=points[-1].t_ms,
        identify="id-123",
    )


class TestRunTypeCode:
    def test_mapping(self) -> None:
        assert p.run_type_code(1) == 2   # RunType.MORNING → 协议 2
        assert p.run_type_code(2) == 1   # RunType.SUN → 协议 1
        assert p.run_type_code(3) == 0   # RunType.FREEDOM → 协议 0

    def test_unknown_defaults_to_zero(self) -> None:
        assert p.run_type_code(99) == 0


class TestRiskFlags:
    def test_clean_points_all_zero(self) -> None:
        flags = p.risk_flags([{"f": False, "m": False, "h": False}] * 5)
        assert flags == {"d": 0, "f": 0, "m": 0, "h": 0}

    def test_counts_flagged(self) -> None:
        flags = p.risk_flags([{"f": True}, {"f": True}, {"m": 1}, {"h": "true"}])
        assert flags["f"] == 2
        assert flags["m"] == 1
        assert flags["h"] == 1

    def test_develop_mode_sets_d(self) -> None:
        assert p.risk_flags([], develop=True)["d"] == 1

    def test_offline_adds_to_d(self) -> None:
        assert p.risk_flags([], offline=3)["d"] == 3


class TestOkPoints:
    def test_empty(self) -> None:
        assert p.build_ok_points([], 0, 1000) == []

    def test_times_increase(self) -> None:
        checkpoints = [
            {"latitude": 30.0, "longitude": 104.0, "content": "A", "id": 1},
            {"latitude": 30.001, "longitude": 104.001, "content": "B", "id": 2},
        ]
        out = p.build_ok_points(checkpoints, 1_700_000_000_000, 1_700_000_600_000)
        assert len(out) == 2
        assert out[0]["time"] < out[1]["time"]

    def test_coords_preserved(self) -> None:
        checkpoints = [{"latitude": 30.5, "longitude": 104.5, "content": "x", "id": 7}]
        out = p.build_ok_points(checkpoints, 0, 60_000)
        assert out[0]["latitude"] == 30.5
        assert out[0]["id"] == 7


class TestBuildRecordParams:
    def test_core_fields(self, setting) -> None:
        draft = _draft(setting)
        params = p.build_record_params(
            draft,
            version_code=20230,
            version_name="2.0.23",
            device_fields={"brand": "Xiaomi", "model": "X", "signature_md5": "AA", "os_version": "15", "unique_code": "abc"},
            environment='{"deviceId":"abc"}',
        )
        assert params["sportType"] == 1
        assert params["identify"] == "id-123"
        assert params["appType"] == "Android"
        assert params["versionCode"] == 20230
        assert params["d"] == 0 and params["f"] == 0
        assert params["isSequencePoint"] == "0"

    def test_sport_range_is_km_string(self, setting) -> None:
        draft = _draft(setting)
        params = p.build_record_params(
            draft, version_code=1, version_name="x",
            device_fields={}, environment="{}",
        )
        # 2 公里左右，两位小数字符串
        assert float(params["sportRange"]) == round(draft.track.total_distance_m / 1000, 2)
        assert params["sportRange"].count(".") == 1

    def test_is_valid_computed(self, setting) -> None:
        draft = _draft(setting)
        params = p.build_record_params(
            draft, version_code=1, version_name="x", device_fields={}, environment="{}",
        )
        assert params["isValid"] in (0, 1)

    def test_track_json_parsable(self, setting) -> None:
        import json

        draft = _draft(setting)
        params = p.build_record_params(
            draft, version_code=1, version_name="x", device_fields={}, environment="{}",
        )
        rows = json.loads(params["gitudeLatitude"])
        assert len(rows) == len(draft.track.points)
        assert {"t", "latitude", "longitude"} <= set(rows[0])
