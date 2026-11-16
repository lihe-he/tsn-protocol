"""统一异常体系.

设计原则：异常要能区分「是我的锅」和「是服务端的锅」，
因为两者的处理策略完全不同（重试 vs 放弃）。
"""

from __future__ import annotations


class TSNError(Exception):
    """所有项目异常的基类."""

    def __init__(self, message: str, *, code: int | str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def __str__(self) -> str:
        if self.code is None:
            return self.message
        return f"[{self.code}] {self.message}"


class ConfigError(TSNError):
    """配置缺失或非法，重试无用."""


class ProtocolError(TSNError):
    """协议层错误：签名失败、解密失败、字段不符合预期.

    出现这类错误通常意味着服务端协议发生了变化，需要更新 adapter。
    """


class UnsupportedVersion(ProtocolError):
    """没有匹配当前 App 版本的 ProtocolAdapter."""


class TransportError(TSNError):
    """网络层错误，可重试."""


class RemoteRejected(TSNError):
    """服务端明确拒绝（业务失败），重试无用.

    remark 是服务端给出的原因，是排查校验规则的主要信息源。
    """

    def __init__(self, message: str, *, remark: str | None = None, code: int | str | None = None) -> None:
        super().__init__(message, code=code)
        self.remark = remark


class AuthError(RemoteRejected):
    """认证相关失败."""


class EngineError(TSNError):
    """轨迹生成/校验阶段失败，未发出请求."""
