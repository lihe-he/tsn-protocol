"""适配器注册表.

新增一个 App 版本支持的标准流程：

    1. 复制 adapters/v2023 → adapters/v2026
    2. 改 version_range 和变化的字段
    3. 在 adapters/__init__ 里 import 一下就自动注册

不需要改 core / engine / client 的任何一行。
"""

from __future__ import annotations

import logging
from typing import Iterator

from tsn.adapters.base import AdapterContext, ProtocolAdapter, parse_version
from tsn.core.errors import UnsupportedVersion

log = logging.getLogger("tsn.adapters")

_REGISTRY: dict[str, type[ProtocolAdapter]] = {}


def register(cls: type[ProtocolAdapter]) -> type[ProtocolAdapter]:
    """类装饰器：注册一个 adapter."""
    if not issubclass(cls, ProtocolAdapter):
        raise TypeError(f"{cls!r} 不是 ProtocolAdapter 子类")
    if cls.name in _REGISTRY and _REGISTRY[cls.name] is not cls:
        raise ValueError(f"adapter 名字重复: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def iter_adapters() -> Iterator[type[ProtocolAdapter]]:
    return iter(_REGISTRY.values())


def available() -> list[dict[str, str]]:
    return [
        {"name": c.name, "range": f"{c.version_range[0]} ~ {c.version_range[1]}"}
        for c in sorted(_REGISTRY.values(), key=lambda x: parse_version(x.version_range[1]))
    ]


def pick(app_version: str) -> type[ProtocolAdapter]:
    """按 App 版本挑适配器.

    策略：优先选「版本区间包含当前版本」中上界最高的那个，
    这样装了 2.0.30 而只有 v2023(→2.0.99) 时也能用最近的适配器。
    """
    candidates = [c for c in _REGISTRY.values() if c.supports(app_version)]
    if not candidates:
        hint = ", ".join(f"{c.name}[{c.version_range[0]}~{c.version_range[1]}]" for c in _REGISTRY.values())
        raise UnsupportedVersion(f"没有适配 App {app_version} 的协议实现；已注册: {hint or '无'}")
    candidates.sort(key=lambda c: parse_version(c.version_range[1]), reverse=True)
    return candidates[0]


def create(app_version: str, ctx: AdapterContext) -> ProtocolAdapter:
    cls = pick(app_version)
    log.debug("选用 adapter=%s (app_version=%s)", cls.name, app_version)
    return cls(ctx)


def latest_version_range() -> tuple[str, str]:
    """所有已注册适配器里覆盖的最高版本 —— 用于给服务端上报版本号."""
    best = ("0.0.0", "0.0.0")
    for cls in _REGISTRY.values():
        if parse_version(cls.version_range[1]) > parse_version(best[1]):
            best = cls.version_range
    return best
