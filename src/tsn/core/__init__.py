"""core —— 协议内核，无业务语义，无 IO 副作用之外的逻辑.

分层约定：
    core      不 import adapters / engine / client
    adapters  只 import core
    engine    只 import core（与协议版本无关）
    client    组合 adapters + engine
"""

from tsn.core import crypto, errors, models, routes, transport

__all__ = ["crypto", "errors", "models", "routes", "transport"]
