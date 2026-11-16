"""领域模型.

刻意用 pydantic 而不是裸 dict：协议字段名全是缩写（d/f/m/h、sid、gitudeLatitude），
裸 dict 传下去三个月后没人知道哪个是哪个。
"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ─────────────────────────  枚举  ─────────────────────────


class RunType(IntEnum):
    """跑步类型.

    注意：公版和私版的编码不一样，转换放在 adapter 里做，别在业务层判断。
    """

    SUN = 2       # 阳光跑
    MORNING = 1   # 晨跑
    FREEDOM = 0   # 自由跑


class FaceType(IntEnum):
    START = 1
    MIDDLE = 2
    END = 3


class SysType(IntEnum):
    PRIVATE = 1   # 自建服务
    PUBLIC = 2    # 平台托管


# ─────────────────────────  实体  ─────────────────────────


class School(BaseModel):
    model_config = ConfigDict(extra="ignore")

    school_id: int
    name: str
    code: str = ""
    url: str = ""
    lan_url: str | None = None
    open_id: str = ""
    sys_type: SysType = SysType.PUBLIC
    is_open_keep: bool = False
    is_open_live: bool = False
    is_open_encry: bool = False
    province: str | None = None

    @property
    def is_public(self) -> bool:
        return self.sys_type is SysType.PUBLIC

    @property
    def cloud_base(self) -> str:
        """租户接口的 base url，自建域名优先."""
        return self.lan_url or self.url or ""


class DeviceProfile(BaseModel):
    """上报给服务端的设备画像.

    这些值会被服务端拿去跟其它信号交叉比对（型号 vs GPU vs 传感器），
    所以不要随便编一个型号却忘了同步其它字段。
    """

    model_config = ConfigDict(extra="allow")

    brand: str = "Xiaomi"
    model: str = "25042PN24C"
    os_version: str = "15"
    abis: list[str] = Field(default_factory=lambda: ["arm64-v8a"])
    unique_code: str = ""          # md5 后的设备号
    signature_md5: str = "7F:C0:22:E6:7C:7D:2A:CC:C3:C8:77:0A:46:13:8D:C3"

    @property
    def model_label(self) -> str:
        return f"{self.brand}-{self.model}"


class Account(BaseModel):
    """账号. 密码不出现在日志里，用 SecretStr-like 的 str 但调用方负责脱敏."""

    model_config = ConfigDict(extra="ignore")

    username: str
    password: str = ""
    school: School
    uid: str = ""
    token: str = ""
    device: DeviceProfile = Field(default_factory=DeviceProfile)

    def masked(self) -> str:
        return f"{self.username[:3]}***@{self.school.name}"


# ─────────────────────────  跑步规则  ─────────────────────────


class ExerciseSetting(BaseModel):
    """服务端下发的跑步规则 —— 我们的目标就是满足它.

    所有距离单位统一成米，时间统一成秒，避免在业务层到处 ×1000。
    """

    model_config = ConfigDict(extra="allow")

    identify: str = ""
    total_range_m: float = 0.0
    start_qualified_s: float = 0.0
    end_qualified_s: float = -1.0
    step_required: int = 0
    step_recursive: bool = False
    geofence: list[dict[str, Any]] = Field(default_factory=list)
    checkpoints: list[dict[str, Any]] = Field(default_factory=list)
    limit_speed: float = 0.0
    limit_stride: float = 0.0
    limit_step_frequency: float = 0.0
    is_start_face: bool = False
    is_midway_face: bool = False
    is_end_face: bool = False
    middle_face_positions: list[float] = Field(default_factory=list)
    user_open_develop: bool = False
    remark_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    def required_steps_for(self, distance_m: float) -> int:
        """按距离放大的步数要求；协议用 isStepRecursion 控制是否递归."""
        if self.step_recursive and self.total_range_m > 0 and distance_m > self.total_range_m:
            return int(-(-self.step_required * distance_m // self.total_range_m))
        return self.step_required

    def end_qualified_for(self, distance_m: float) -> float:
        if self.end_qualified_s != -1 and self.total_range_m > 0 and distance_m > self.total_range_m:
            return self.end_qualified_s * distance_m / self.total_range_m
        return self.end_qualified_s

    def is_qualified(self, elapsed_s: float, distance_m: float, steps: int) -> bool:
        ok_time = elapsed_s >= self.start_qualified_s
        end = self.end_qualified_for(distance_m)
        if not ok_time and end != -1:
            ok_time = elapsed_s <= end
        return bool(
            ok_time
            and distance_m >= self.total_range_m
            and steps >= self.required_steps_for(distance_m)
        )


# ─────────────────────────  轨迹  ─────────────────────────


class GeoPoint(BaseModel):
    """一个轨迹采样点."""

    model_config = ConfigDict(extra="ignore")

    t_ms: float
    lng: float
    lat: float
    distance_m: float = 0.0      # 与本点上一点的距离
    cumulative_m: float = 0.0    # 累计距离
    speed_mps: float = 0.0
    paused: bool = False

    def geo(self) -> tuple[float, float]:
        return (self.lng, self.lat)


class StepBucket(BaseModel):
    """60 秒步数桶."""

    buckets: list[int] = Field(default_factory=list)
    total: int = 0
    bucket_seconds: float = 60.0

    def as_json(self) -> str:
        import json

        return json.dumps(self.buckets, separators=(",", ":"))


class Track(BaseModel):
    """重采样后的完整轨迹."""

    points: list[GeoPoint] = Field(default_factory=list)
    target_distance_m: float = 0.0
    base_speed_mps: float = 0.0
    plan_use_time_s: float = 0.0
    start_ts_ms: float = 0.0

    @property
    def total_distance_m(self) -> float:
        return self.points[-1].cumulative_m if self.points else 0.0

    @property
    def elapsed_s(self) -> float:
        if not self.points:
            return 0.0
        return (self.points[-1].t_ms - self.points[0].t_ms) / 1000.0

    @property
    def end_ts_ms(self) -> float:
        return self.points[-1].t_ms if self.points else 0.0

    @property
    def avg_speed_mps(self) -> float:
        elapsed = self.elapsed_s
        return self.total_distance_m / elapsed if elapsed > 0 else 0.0

    def pace_label(self) -> str:
        """配速，格式 5'12" —— 服务端原样存这个字符串."""
        if self.total_distance_m <= 0:
            return "0'0\""
        sec_per_km = self.elapsed_s / (self.total_distance_m / 1000.0)
        return f"{int(sec_per_km // 60)}'{int(sec_per_km % 60)}\""


# ─────────────────────────  一次跑步  ─────────────────────────


class RunDraft(BaseModel):
    """待提交的跑步记录（业务层产物，还没变成协议字段）."""

    model_config = ConfigDict(extra="ignore")

    school: School
    run_type: RunType
    setting: ExerciseSetting
    track: Track
    steps: StepBucket
    start_ts_ms: float
    end_ts_ms: float
    identify: str = ""
    face_uploaded: int = 0
    risk: dict[str, int] = Field(default_factory=dict)

    @property
    def distance_m(self) -> float:
        return self.track.total_distance_m

    @property
    def elapsed_s(self) -> float:
        return (self.end_ts_ms - self.start_ts_ms) / 1000.0

    def summary(self) -> str:
        km = self.distance_m / 1000
        mm, ss = divmod(int(self.elapsed_s), 60)
        return (
            f"{km:.2f} km / {mm}'{ss:02d}\" / {self.steps.total} 步"
            f" / 配速 {self.track.pace_label()}"
        )


class RunResult(BaseModel):
    """提交结果."""

    record_id: str | None = None
    sport_status: str = ""
    remark: str = ""
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def accepted(self) -> bool:
        return str(self.sport_status) == "1"

    def explain(self) -> str:
        if self.accepted:
            return f"记录有效 (id={self.record_id})"
        return f"被拒: status={self.sport_status} remark={self.remark or '无'}"


class AppVersionInfo(BaseModel):
    """服务端下发的版本策略，适配器选择依赖它."""

    model_config = ConfigDict(extra="allow")

    min_version: str = ""
    latest_version: str = ""
    force_update: bool = False
    download_url: str = ""


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


def fmt_ts(ts_ms: float) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
