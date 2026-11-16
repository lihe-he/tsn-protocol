"""版本适配层.

核心思想：所有跟「哪个 App 版本用哪套字段」相关的逻辑，全部收敛到 adapter 里。
core 和 engine 完全不知道协议长什么样，所以 App 升级时只改一个 adapter。

一个 adapter 要负责四件事：
    1. 认证怎么发
    2. 请求怎么签/怎么加密
    3. 服务端下发的原始 dict 怎么翻译成领域模型
    4. 领域模型怎么变成提交字段
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from tsn.core.models import (
    Account,
    AppVersionInfo,
    DeviceProfile,
    ExerciseSetting,
    RunDraft,
    RunResult,
    School,
)
from tsn.core.transport import Transport


@dataclass(slots=True)
class AdapterContext:
    """adapter 运行时上下文 —— 由 client 组装后注入."""

    school: School
    device: DeviceProfile
    transport: Transport
    token: str = ""
    uid: str = ""
    app_version: str = ""
    cloud_base: str = ""


@dataclass(slots=True)
class LoginResult:
    ok: bool
    uid: str = ""
    token: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class ProtocolAdapter(abc.ABC):
    """一套协议版本的完整实现."""

    #: 人类可读名字
    name: str = "abstract"
    #: 支持的 App 版本闭区间，(lo, hi)
    version_range: tuple[str, str] = ("0.0.0", "0.0.0")

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx

    # ── 版本能力 ──

    @classmethod
    def supports(cls, app_version: str) -> bool:
        lo, hi = (parse_version(v) for v in cls.version_range)
        v = parse_version(app_version or "0.0.0")
        return lo <= v <= hi

    @property
    def school(self) -> School:
        return self.ctx.school

    # ── 认证 ──

    @abc.abstractmethod
    async def login(self, username: str, password: str) -> LoginResult:
        """账号密码登录，返回 token."""

    @abc.abstractmethod
    async def fetch_app_version(self) -> AppVersionInfo:
        """查服务端要求的 App 版本，用于自动选 adapter."""

    # ── 跑步规则 ──

    @abc.abstractmethod
    async def get_setting(self, run_type: int, lng: float, lat: float) -> ExerciseSetting:
        """拉跑步规则（距离/时间/步数/围栏/人脸策略）."""

    @abc.abstractmethod
    async def get_start_time(self, identify: str) -> int:
        """拿服务端认可的起跑时间戳（毫秒）."""

    @abc.abstractmethod
    async def get_summary(self) -> dict[str, Any]:
        """查各类跑步的可用状态."""

    # ── 记录 ──

    @abc.abstractmethod
    async def submit_record(self, draft: RunDraft) -> tuple[str | None, dict[str, Any]]:
        """提交一条跑步记录，返回 (记录 id, 原始响应)."""

    @abc.abstractmethod
    async def get_record(self, record_id: str) -> RunResult:
        """查记录判定结果 —— sportStatus != 1 就是被拒."""

    @abc.abstractmethod
    async def list_records(self) -> list[dict[str, Any]]:
        """历史记录，路线源要用."""

    # ── 人脸 ──

    @abc.abstractmethod
    async def upload_face(self, image: bytes, coords: str, identify: str, face_type: int) -> None:
        """上传一个验证点的人脸."""

    @abc.abstractmethod
    async def list_user_faces(self) -> list[dict[str, Any]]:
        """该账号在服务端的人脸素材."""

    # ── 辅助 ──

    def new_account(self, username: str, login: LoginResult) -> Account:
        return Account(
            username=username,
            school=self.school,
            uid=login.uid,
            token=login.token,
            device=self.ctx.device,
        )


# ─────────────────────────  版本号工具  ─────────────────────────


def parse_version(text: str) -> tuple[int, ...]:
    """把 '2.0.23' 变成 (2, 0, 23, 0)，容忍脏数据.

    固定返回 4 元组，这样比较时不会因为长度不同而出现
    (2, 0, 23) < (2, 0, 23, 0) 这种反直觉结果。
    """
    parts: list[int] = []
    for chunk in str(text).replace("-", ".").replace("_", ".").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def version_tuple_to_str(v: tuple[int, ...]) -> str:
    return ".".join(str(x) for x in v)
