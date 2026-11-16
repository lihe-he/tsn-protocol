"""HTTP 传输层.

只做三件事：发请求、重试、把响应体翻译成 dict 或异常。
不认识任何业务字段——那是 adapter 的活。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

import httpx

from tsn.core.errors import RemoteRejected, TransportError

log = logging.getLogger("tsn.transport")

# 这些状态码值得重试；4xx 说明请求本身有问题，重试只会浪费配额
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

DEFAULT_UA = "okhttp/4.9.0"


class Transport:
    """一个可复用的异步 HTTP 客户端."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 0.8,
        user_agent: str = DEFAULT_UA,
        debug: bool = False,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.debug = debug
        self._client: httpx.AsyncClient | None = None
        self._ua = user_agent

    async def __aenter__(self) -> Transport:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"user-agent": self._ua, "accept-encoding": "gzip"},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── 内部 ──

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Transport 必须在 async with 中使用")
        return self._client

    def _log(self, direction: str, url: str, payload: Any) -> None:
        if not self.debug:
            return
        text = repr(payload)
        if len(text) > 800:
            text = text[:800] + f"... (共 {len(text)} 字符)"
        log.debug("%s %s %s", direction, url, text)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        files: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        self._log(f"→ {method}", url, data or params)
        last: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                resp = await self.client.request(
                    method,
                    url,
                    data=data,
                    params=params,
                    files=files,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
                if attempt >= self.retries:
                    break
                wait = self.backoff * (2**attempt)
                log.warning("网络错误 %s，%.1fs 后重试 (%d/%d)", exc, wait, attempt + 1, self.retries)
                await asyncio.sleep(wait)
                continue

            if resp.status_code in _RETRYABLE_STATUS and attempt < self.retries:
                wait = self.backoff * (2**attempt)
                log.warning("HTTP %d，%.1fs 后重试", resp.status_code, wait)
                await asyncio.sleep(wait)
                continue

            if resp.status_code >= 400:
                raise TransportError(f"HTTP {resp.status_code}: {resp.text[:200]}", code=resp.status_code)

            return resp

        raise TransportError(f"请求失败（已重试 {self.retries} 次）: {last}")

    @staticmethod
    def _as_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            body = resp.json()
        except ValueError as exc:
            raise TransportError(f"响应不是 JSON: {resp.text[:200]}") from exc
        if not isinstance(body, dict):
            raise TransportError(f"响应顶层不是对象: {type(body).__name__}")
        return body

    @staticmethod
    def _check(body: dict[str, Any]) -> dict[str, Any]:
        """把两家服务端各自的成功码统一成「没抛异常就是成功」.

        公共接口用 returnCode=='200'，租户接口用 code==0。
        """
        if "returnCode" in body and str(body["returnCode"]) != "200":
            raise RemoteRejected(
                str(body.get("returnMsg") or body.get("returnCode")),
                remark=body.get("returnMsg"),
                code=body.get("returnCode"),
            )
        if "code" in body and str(body["code"]) != "0":
            raise RemoteRejected(
                str(body.get("msg") or body.get("code")),
                remark=body.get("msg"),
                code=body.get("code"),
            )
        return body

    # ── 对外 ──

    async def post_form(
        self,
        url: str,
        data: Mapping[str, Any] | None = None,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """表单 POST.

        params 走 query string —— 登录接口就是这么发的，别改成 body。
        """
        resp = await self._send("POST", url, data=data, params=params, headers=headers)
        body = self._as_json(resp)
        self._log("← 200", url, body)
        return self._check(body)

    async def get(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resp = await self._send("GET", url, params=params, headers=headers)
        body = self._as_json(resp)
        self._log("← 200", url, body)
        return self._check(body)

    async def post_multipart(
        self,
        url: str,
        *,
        data: Mapping[str, Any],
        files: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resp = await self._send("POST", url, data=data, files=files, headers=headers)
        body = self._as_json(resp)
        self._log("← 200", url, body)
        return self._check(body)

    async def get_bytes(self, url: str) -> bytes:
        resp = await self._send("GET", url)
        return resp.content


def unwrap_data(body: Mapping[str, Any]) -> Any:
    """取业务负载，兼容 data / returnData / 顶层."""
    for key in ("data", "returnData", "returnObject", "result"):
        if key in body:
            return body[key]
    return body
