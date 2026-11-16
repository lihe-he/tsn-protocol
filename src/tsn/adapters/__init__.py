"""adapters —— 按 App 版本隔离的协议实现.

导入本模块即完成所有 adapter 的注册。
"""

from tsn.adapters import v2023  # noqa: F401  (import 触发注册)
from tsn.adapters.base import (
    AdapterContext,
    LoginResult,
    ProtocolAdapter,
    parse_version,
)
from tsn.adapters.registry import (
    available,
    create,
    iter_adapters,
    latest_version_range,
    pick,
    register,
)

__all__ = [
    "AdapterContext",
    "LoginResult",
    "ProtocolAdapter",
    "available",
    "create",
    "iter_adapters",
    "latest_version_range",
    "parse_version",
    "pick",
    "register",
    "v2023",
]
