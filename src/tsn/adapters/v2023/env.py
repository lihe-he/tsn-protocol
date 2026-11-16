"""设备环境上报.

服务端会拿这份「自检结果」判断客户端是否处于可疑状态（root / hook / 模拟器）。
它是一个带 checksum 的 JSON 字符串，塞在请求体的 environment 字段里。

设计上只提供「构造一份清白上报」的能力，不做主动探测——
在服务端协议里，探测结果由客户端自己声明，我们没有理由声明坏结果。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

# 需要附带 detail 的字段（协议里只有这几个字段带 detail 数组）
_DETAIL_FIELDS = ("selinux", "frida", "hook", "breakpoint")
_FIELDS = (
    "root",
    "selinux",
    "xposed",
    "proxy",
    "vpn",
    "separation",
    "emulator",
    "ptrace",
    "frida",
    "hook",
    "breakpoint",
)
CLEAN_SELINUX_DETAIL = ["Enforcing\n"]


def _checksum(data: dict[str, Any]) -> str:
    """checksum 自身不参与计算，否则永远对不上."""
    temp = {k: v for k, v in data.items() if k != "checksum"}
    raw = json.dumps(temp, separators=(",", ":"), ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _b(value: bool) -> str:
    return "true" if value else "false"


def build_env_payload(
    device_id: str,
    *,
    abis: list[str] | None = None,
    nonce: str | None = None,
    timestamp_ms: int | None = None,
    signed: bool = True,
    debug: bool = False,
) -> dict[str, str]:
    """构造 environment 字段（一个 JSON 字符串）.

    返回结构：
        {"deviceId": ..., "oppo": "<json>", "safe": "<json>"}
    """
    oppo: dict[str, Any] = {"auth": "success"}
    for name in _FIELDS:
        entry: dict[str, Any] = {"result": "false"}
        if name in _DETAIL_FIELDS:
            entry["detail"] = list(CLEAN_SELINUX_DETAIL) if name == "selinux" else []
        oppo[name] = entry

    oppo["nonce"] = nonce or str(uuid.uuid4())
    oppo["timestamp"] = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    oppo["checksum"] = _checksum(oppo)

    safe = {
        "sign": _b(signed),
        "root": "false",
        "emulator": "false",
        "hook": "false",
        "debug": _b(debug),
        "breakpoint": "false",
        "supported_abis": list(abis or ["arm64-v8a"]),
    }

    return {
        "deviceId": device_id,
        "oppo": json.dumps(oppo, separators=(",", ":"), ensure_ascii=False),
        "safe": json.dumps(safe, separators=(",", ":"), ensure_ascii=False),
    }
