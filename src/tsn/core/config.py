"""运行配置.

优先级：显式参数 > 环境变量 > 默认值。
配置文件里不放凭据，凭据一律走环境变量或命令行交互输入。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from tsn.core.models import DeviceProfile


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


@dataclass(slots=True)
class TransportConfig:
    timeout: float = 20.0
    retries: int = 3
    backoff: float = 0.8
    user_agent: str = "okhttp/4.9.0"
    debug: bool = False

    @classmethod
    def from_env(cls) -> TransportConfig:
        return cls(
            timeout=_env_float("TSN_TIMEOUT", 20.0),
            retries=_env_int("TSN_RETRIES", 3),
            backoff=_env_float("TSN_BACKOFF", 0.8),
            user_agent=os.getenv("TSN_UA", "okhttp/4.9.0"),
            debug=_env_bool("TSN_DEBUG", False),
        )


@dataclass(slots=True)
class EngineConfig:
    """轨迹引擎参数. 这些默认值来自真实跑者数据的经验区间."""

    sample_interval_s: float = 1.0
    gps_sigma_m: float = 3.5          # 手机 GPS 典型误差
    gps_ar_alpha: float = 0.85        # AR(1) 相关系数，越大越平滑
    speed_jitter: float = 0.07
    speed_min_factor: float = 0.80
    speed_max_factor: float = 1.22
    pace_sec_per_km: tuple[float, float] = (300.0, 390.0)  # 5'00" ~ 6'30"
    pace_relax_every_s: tuple[float, float] = (45.0, 120.0)
    pace_relax_ratio: float = 0.35    # 放松段速度降幅
    pace_relax_len_s: tuple[float, float] = (4.0, 15.0)
    stride_min_m: float = 0.55
    stride_max_m: float = 1.75
    loop_mode: str = "pingpong"
    max_samples: int = 200_000
    tail_pause_points: int = 2

    @classmethod
    def from_env(cls) -> EngineConfig:
        return cls(
            sample_interval_s=_env_float("TSN_SAMPLE_INTERVAL", 1.0),
            gps_sigma_m=_env_float("TSN_GPS_SIGMA", 3.5),
            gps_ar_alpha=_env_float("TSN_GPS_AR", 0.85),
            speed_jitter=_env_float("TSN_SPEED_JITTER", 0.07),
            loop_mode=os.getenv("TSN_LOOP_MODE", "pingpong"),
        )


@dataclass(slots=True)
class AppConfig:
    transport: TransportConfig = field(default_factory=TransportConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    device: DeviceProfile = field(default_factory=DeviceProfile)
    data_dir: Path = field(default_factory=lambda: Path("data"))
    face_dir: Path = field(default_factory=lambda: Path("data/faces"))
    state_file: Path = field(default_factory=lambda: Path("data/state.json"))

    @classmethod
    def from_env(cls) -> AppConfig:
        data_dir = Path(os.getenv("TSN_DATA_DIR", "data"))
        return cls(
            transport=TransportConfig.from_env(),
            engine=EngineConfig.from_env(),
            device=DeviceProfile(
                brand=os.getenv("TSN_DEVICE_BRAND", "Xiaomi"),
                model=os.getenv("TSN_DEVICE_MODEL", "25042PN24C"),
                os_version=os.getenv("TSN_DEVICE_OS", "15"),
            ),
            data_dir=data_dir,
            face_dir=data_dir / "faces",
            state_file=data_dir / "state.json",
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.face_dir.mkdir(parents=True, exist_ok=True)
