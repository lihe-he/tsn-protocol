"""v2023 协议：提交字段构造.

这一层只做「领域模型 → 协议字段」的翻译，不发请求、不碰 IO。
好处是可以单独写测试，用固定输入验证字段，不用起网络。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from tsn.adapters.base import version_tuple_to_str
from tsn.core.models import ExerciseSetting, RunDraft

# 协议里 sportType 的编码（公版）
RUN_TYPE_CODE = {"freedom": 0, "morning": 1, "sun": 2}
_RUN_TYPE_BY_VALUE = {1: 2, 2: 1, 3: 0}


def run_type_code(value: int) -> int:
    """把 RunType 的枚举值（1/2/3）翻译成协议值（2/1/0）."""
    return _RUN_TYPE_BY_VALUE.get(int(value), 0)


def risk_flags(points: Iterable[Mapping[str, Any]], *, develop: bool = False, offline: int = 0) -> dict[str, int]:
    """统计轨迹点上的风险标记.

    d = 开发者模式 + 离线次数
    f / m / h = 各风险点计数

    协议把这些作为「可疑度」上报，正常记录应该全 0。
    """
    f = m = h = 0
    for p in points:
        if _truthy(p.get("f")):
            f += 1
        if _truthy(p.get("m")):
            m += 1
        if _truthy(p.get("h")):
            h += 1
    return {"d": (1 if develop else 0) + int(offline), "f": f, "m": m, "h": h}


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def build_ok_points(
    checkpoints: list[dict[str, Any]],
    start_ms: float,
    end_ms: float,
) -> list[dict[str, Any]]:
    """把打卡点按时间均摊到整条轨迹上.

    服务端要的是「第 N 个打卡点在什么时间被经过」，时间必须严格递增且落在
    [start, end] 区间内，否则会被判轨迹异常。
    """
    if not checkpoints:
        return []

    span = end_ms - start_ms
    step = span / (len(checkpoints) + 4)
    cursor = start_ms + step
    out: list[dict[str, Any]] = []
    for cp in checkpoints:
        from datetime import datetime

        when = datetime.fromtimestamp(cursor / 1000).strftime("%Y-%m-%d %H:%M:%S")
        item: dict[str, Any] = {
            "latitude": float(cp["latitude"]),
            "longitude": float(cp["longitude"]),
            "content": cp.get("content", ""),
            "id": cp.get("id"),
            "time": when,
        }
        if "sort" in cp:
            item["sort"] = cp["sort"]
        out.append(item)
        cursor += step
    return out


def build_track_payload(draft: RunDraft) -> list[dict[str, Any]]:
    """轨迹点.

    协议要的是 1 秒一个点，字段 t/latitude/longitude。
    """
    from datetime import datetime

    rows: list[dict[str, Any]] = []
    for p in draft.track.points:
        rows.append(
            {
                "t": int(p.t_ms),
                "time": datetime.fromtimestamp(p.t_ms / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                "latitude": round(p.lat, 7),
                "longitude": round(p.lng, 7),
            }
        )
    return rows


def build_record_params(
    draft: RunDraft,
    *,
    version_code: int,
    version_name: str,
    device_fields: Mapping[str, Any],
    environment: str,
) -> dict[str, Any]:
    """组装 addExerciseRecord 的完整参数."""
    setting = draft.setting
    track_rows = build_track_payload(draft)
    flags = risk_flags(track_rows, develop=setting.user_open_develop)
    distance_m = draft.track.total_distance_m
    elapsed_s = draft.elapsed_s

    params: dict[str, Any] = {
        "sportType": run_type_code(int(draft.run_type)),
        "startTime": int(draft.start_ts_ms),
        "endTime": int(draft.end_ts_ms),
        "sportTime": int(elapsed_s),
        "sportRange": f"{distance_m / 1000:.2f}",
        "speed": draft.track.pace_label(),
        "avgSpeed": f"{distance_m / elapsed_s * 3.6:.2f}" if elapsed_s > 0 else "0.00",
        "gitudeLatitude": json.dumps(track_rows, separators=(",", ":")),
        "stepNumbers": draft.steps.as_json(),
        "isSequencePoint": "0",
        "pointList": json.dumps(setting.checkpoints, ensure_ascii=False),
        "okPointList": json.dumps(
            build_ok_points(setting.checkpoints, draft.start_ts_ms, draft.end_ts_ms),
            ensure_ascii=False,
        ),
        "isFaceStatus": str(draft.face_uploaded),
        "uploadType": 0,
        "identify": draft.identify,
        "geofence": json.dumps(setting.geofence, ensure_ascii=False),
        "limitSpeed": f"{setting.limit_speed:.1f}",
        "limitStride": f"{setting.limit_stride:.1f}",
        "limitStepFrequency": str(int(setting.limit_step_frequency)),
        "gpsDistance": f"{distance_m / 1000:.2f}",
        "d": flags["d"],
        "f": flags["f"],
        "m": flags["m"],
        "h": flags["h"],
        "isValid": 1 if setting.is_qualified(elapsed_s, distance_m, draft.steps.total) else 0,
        "environment": environment,
        "appType": "Android",
        "versionCode": version_code,
        "versionName": version_name,
        "signatureMD5": device_fields.get("signature_md5", ""),
        "brand": device_fields.get("brand", ""),
        "model": device_fields.get("model", ""),
        "system": "Android",
        "version": device_fields.get("os_version", ""),
        "uniqueCode": device_fields.get("unique_code", ""),
    }

    if setting.remark_id:
        params["remarkId"] = setting.remark_id
    return params


def version_defaults() -> tuple[int, str]:
    """v2023 协议默认上报的版本号."""
    return (20230, "2.0.23")


__all__ = [
    "build_ok_points",
    "build_record_params",
    "build_track_payload",
    "risk_flags",
    "run_type_code",
    "version_defaults",
    "version_tuple_to_str",
]
