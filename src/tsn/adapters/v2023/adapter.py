"""v2023 协议适配器.

覆盖 App 2.0.0 ~ 2.0.99。这是目前实测在跑的一版。

签名三段式（租户接口）：
    appId     = md5( AES-CBC(appId_tenant + token + ts) )
    appSecret = md5( AES-CBC(token + ts) )
    sign      = md5( AES-CBC(按 key 排序的 k=v&... 拼接串) )

看起来像在混淆，其实是为了让每个请求的 sign 都依赖 token 和毫秒时间戳，
旧请求重放会因为 token 变了而失效。
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any
from urllib.parse import unquote

from tsn.adapters.base import AdapterContext, LoginResult, ProtocolAdapter
from tsn.adapters.registry import register
from tsn.adapters.v2023 import env as env_mod
from tsn.adapters.v2023 import payload as payload_mod
from tsn.core import crypto, routes
from tsn.core.errors import AuthError, ProtocolError, RemoteRejected
from tsn.core.models import (
    AppVersionInfo,
    ExerciseSetting,
    RunDraft,
    RunResult,
    now_ms,
)
from tsn.core.transport import unwrap_data

UA = "okhttp/4.9.0"


@register
class V2023Adapter(ProtocolAdapter):
    name = "v2023"
    version_range = ("2.0.0", "2.0.99")

    VERSION_CODE = 20230
    VERSION_NAME = "2.0.23"

    def __init__(self, ctx: AdapterContext) -> None:
        super().__init__(ctx)
        raw_id = ctx.device.unique_code or str(uuid.uuid4())
        # 协议里上报的是 md5 后的设备号
        self._device_id = crypto.md5_hex(raw_id)
        self.ctx.device.unique_code = self._device_id

    # ─────────────────────  请求头  ─────────────────────

    @property
    def _cloud(self) -> str:
        return self.ctx.cloud_base or self.school.cloud_base or routes.DEFAULT_CLOUD_BASE

    def _public_headers(self) -> dict[str, str]:
        return {
            "Host": "m.boxkj.com",
            "channel": "Android",
            "version": str(self.VERSION_CODE),
            "type": "0",
            "Content-Type": "application/x-www-form-urlencoded",
            "accept-encoding": "gzip",
            "user-agent": UA,
        }

    def _tenant_headers(self) -> dict[str, str]:
        return {
            "model": self.ctx.device.model_label,
            "uniqueCode": self._device_id,
            "school": self.school.code,
            "Cookie": f"host={self.school.code}",
            "accept-encoding": "gzip",
            "user-agent": UA,
        }

    # ─────────────────────  签名  ─────────────────────

    def _cbc(self, text: str) -> str:
        """token 派生的 CBC 加密，签名的骨架."""
        key, iv = crypto.token_key_iv(self.ctx.token)
        return crypto.aes_cbc_encrypt(text, key, iv)

    def _appid_mix(self, ts: str) -> str:
        return crypto.md5_hex(self._cbc(crypto.APP_ID_TENANT + self.ctx.token + ts))

    def _appsecret_mix(self, ts: str) -> str:
        return crypto.md5_hex(self._cbc(self.ctx.token + ts))

    def _sign(self, params: dict[str, Any], ts: str) -> str:
        merged = {k: v for k, v in params.items() if k not in ("sign", "timestamp")}
        merged["appId"] = self._appid_mix(ts)
        merged["appSecret"] = self._appsecret_mix(ts)
        auth_info = unquote(crypto.build_auth_info(merged))
        return crypto.md5_hex(self._cbc(auth_info))

    def _signed_headers(self, params: dict[str, Any], ts: str, *, bearer: bool = True) -> dict[str, str]:
        headers = self._tenant_headers()
        headers["sign"] = self._sign(params, ts)
        headers["timestamp"] = ts
        if bearer:
            headers["Authorization"] = f"Bearer {self.ctx.token}"
        return headers

    # ─────────────────────  底层调用  ─────────────────────

    def _url(self, name: str, **kw: Any) -> str:
        return routes.get(name).render(self._cloud, **kw)

    async def _get(self, name: str, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        ts = str(now_ms())
        url = self._url(name)
        body = await self.ctx.transport.get(url, params, headers=self._signed_headers(params, ts))
        return unwrap_data(body)

    async def _post_encrypted(self, name: str, params: dict[str, Any]) -> tuple[Any, str | None]:
        """加密 POST：参数走 RSA+AES 打包."""
        ts = str(now_ms())
        url = self._url(name)
        enc = crypto.enc_params(params, self.ctx.token, ts)
        headers = self._signed_headers(params, ts)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = await self.ctx.transport.post_form(url, enc, headers=headers)
        record_id = body.get("exerciseRecordId")
        return unwrap_data(body), str(record_id) if record_id is not None else None

    async def _get_encrypted(self, name: str, params: dict[str, Any]) -> Any:
        """加密 GET：参数加密，响应也加密，需要用会话 key 解回来."""
        ts = str(now_ms())
        url = self._url(name)
        enc = crypto.enc_params(params, self.ctx.token, ts)
        headers = self._signed_headers(params, ts)
        body = await self.ctx.transport.get(url, enc, headers=headers)
        data = unwrap_data(body)
        if isinstance(data, str):
            session_key = crypto.derive_session_key(self.ctx.token, ts)
            plain = crypto.aes_ecb_decrypt(data, session_key.encode())
            try:
                return json.loads(plain)
            except ValueError as exc:
                raise ProtocolError(f"解密后的响应不是 JSON: {plain[:200]}") from exc
        return data

    # ─────────────────────  认证  ─────────────────────

    def _basic_auth(self) -> str:
        """HTTP Basic: base64(schoolCode + ':pig')."""
        raw = f"{self.school.code}:pig"
        return "Basic " + base64.b64encode(raw.encode()).decode()

    async def login(self, username: str, password: str) -> LoginResult:
        ts = str(now_ms())
        params = {
            "username": username,
            "password": crypto.encrypt_password(password),
            "grant_type": "password",
            "type": "app",
            "appType": "stuApp",
        }
        headers = self._tenant_headers()
        headers["Authorization"] = self._basic_auth()
        url = self._url("auth.token")

        try:
            body = await self.ctx.transport.post_form(url, params=params, headers=headers)
        except RemoteRejected as exc:
            msg = str(exc.remark or exc.message)
            if "Bad credentials" in msg:
                raise AuthError("用户名不存在", remark=msg) from exc
            if "Wrong password" in msg:
                raise AuthError("密码错误", remark=msg) from exc
            raise AuthError(f"登录失败: {msg}", remark=msg) from exc

        token = body.get("access_token") or ""
        if not token:
            raise AuthError(f"登录响应里没有 access_token: {body}")
        self.ctx.token = token
        return LoginResult(
            ok=True,
            uid=str(body.get("user_id", "")),
            token=token,
            raw=body,
        )

    async def refresh(self, refresh_token: str) -> LoginResult:
        params = {
            "refresh_token": refresh_token,
            "scope": "server",
            "grant_type": "refresh_token",
            "type": "app",
            "appType": "stuApp",
        }
        headers = self._tenant_headers()
        headers["Authorization"] = self._basic_auth()
        body = await self.ctx.transport.post_form(self._url("auth.token"), params=params, headers=headers)
        token = body.get("access_token", "")
        self.ctx.token = token
        return LoginResult(ok=bool(token), token=token, raw=body)

    async def fetch_app_version(self) -> AppVersionInfo:
        params = {
            "schoolId": self.school.school_id,
            "appSign": self.ctx.device.signature_md5,
        }
        ts = str(now_ms())
        data = dict(params)
        data["timestamp"] = ts
        data["sign"] = crypto.public_sign(data)
        body = await self.ctx.transport.post_form(
            self._url("app.version"), data, headers=self._public_headers()
        )
        payload = unwrap_data(body) or {}
        if not isinstance(payload, dict):
            return AppVersionInfo()
        return AppVersionInfo(
            min_version=str(payload.get("minVersion") or payload.get("versionName") or ""),
            latest_version=str(payload.get("versionName") or payload.get("newVersion") or ""),
            force_update=str(payload.get("isForce") or payload.get("forceUpdate") or "0") in ("1", "true"),
            download_url=str(payload.get("downloadUrl") or payload.get("url") or ""),
        )

    # ─────────────────────  跑步规则  ─────────────────────

    async def get_summary(self) -> dict[str, Any]:
        data = await self._get("run.sum")
        return data if isinstance(data, dict) else {}

    async def get_setting(self, run_type: int, lng: float, lat: float) -> ExerciseSetting:
        params: dict[str, Any] = {
            "runType": payload_mod.run_type_code(run_type),
            "longitude": round(lng, 6),
            "latitude": round(lat, 6),
            "appType": "Android",
            "versionCode": self.VERSION_CODE,
            "versionName": self.VERSION_NAME,
            "signatureMD5": self.ctx.device.signature_md5,
            "brand": self.ctx.device.brand,
            "model": self.ctx.device.model,
            "system": "Android",
            "version": self.ctx.device.os_version,
            "uniqueCode": self._device_id,
        }
        raw = await self._get_encrypted("run.setting", params)
        raw = _ensure_dict(raw)
        return _parse_setting(raw)

    async def get_start_time(self, identify: str) -> int:
        data = await self._get("run.start_time", {"identify": identify})
        payload = _ensure_dict(data)
        value = payload.get("startTime")
        if value is None:
            raise ProtocolError(f"起跑时间响应缺少 startTime: {payload}")
        return int(float(value))

    async def get_explanation(self) -> dict[str, Any]:
        return _ensure_dict(await self._get("run.explanation"))

    # ─────────────────────  记录  ─────────────────────

    async def submit_record(self, draft: RunDraft) -> tuple[str | None, dict[str, Any]]:
        environment = json.dumps(
            env_mod.build_env_payload(self._device_id, abis=self.ctx.device.abis),
            separators=(",", ":"),
            ensure_ascii=False,
        )
        params = payload_mod.build_record_params(
            draft,
            version_code=self.VERSION_CODE,
            version_name=self.VERSION_NAME,
            device_fields=self.ctx.device.model_dump(),
            environment=environment,
        )
        data, record_id = await self._post_encrypted("run.add_record", params)
        return record_id, _ensure_dict(data)

    async def get_record(self, record_id: str) -> RunResult:
        data = await self._get("run.get_record", {"exerciseRecordId": record_id})
        payload = _ensure_dict(data)
        return RunResult(
            record_id=str(record_id),
            sport_status=str(payload.get("sportStatus", "")),
            remark=str(payload.get("remark", "")),
            raw=payload,
        )

    async def list_records(self, page: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        data = await self._get(
            "run.list_record",
            {"status": 1, "date": "", "datePageIndex": page},
        )
        if isinstance(data, list):
            return data
        payload = _ensure_dict(data)
        for key in ("list", "records", "rows", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []

    async def statistics(self) -> dict[str, Any]:
        return _ensure_dict(await self._get("run.statistics"))

    # ─────────────────────  人脸  ─────────────────────

    async def upload_face(self, image: bytes, coords: str, identify: str, face_type: int) -> None:
        ts = str(now_ms())
        params: dict[str, Any] = {
            "identify": identify,
            "type": str(face_type),
            "runType": str(payload_mod.run_type_code(1)),
            "coordinates": coords,
            "timeStamp": ts,
            "exception": "0",
            "environment": json.dumps(
                env_mod.build_env_payload(self._device_id, abis=self.ctx.device.abis),
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
        enc = crypto.enc_params(params, self.ctx.token, ts)
        sign = self._sign({"key": enc["key"], "param": enc["param"]}, ts)

        headers = self._tenant_headers()
        headers["sign"] = sign
        headers["timestamp"] = ts
        headers["Authorization"] = f"Bearer {self.ctx.token}"

        files = {
            "key": (None, enc["key"].encode()),
            "param": (None, enc["param"].encode()),
            "file": ("file.jpg", image, "image/png"),
        }
        await self.ctx.transport.post_multipart(
            self._url("run.face"), data={}, files=files, headers=headers
        )

    async def list_user_faces(self) -> list[dict[str, Any]]:
        data = await self._get("user.list_face", {"basUserId": self.ctx.uid})
        if isinstance(data, list):
            return data
        payload = _ensure_dict(data)
        if isinstance(payload.get("list"), list):
            return payload["list"]
        return []

    async def get_user_info(self) -> dict[str, Any]:
        return _ensure_dict(await self._get("user.info"))


# ─────────────────────  解析辅助  ─────────────────────


def _ensure_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_setting(raw: dict[str, Any]) -> ExerciseSetting:
    """把服务端原始 setting 翻译成领域模型，单位统一成米/秒."""
    geofence = raw.get("geofence") or []
    if isinstance(geofence, str):
        try:
            geofence = json.loads(geofence)
        except ValueError:
            geofence = []

    checkpoints = raw.get("list") or []
    if isinstance(checkpoints, str):
        try:
            checkpoints = json.loads(checkpoints) if checkpoints.strip() else []
        except ValueError:
            checkpoints = []

    # campusList 情况下按校区取打卡点
    campus_list = raw.get("campusList")
    if isinstance(campus_list, list) and campus_list:
        # 具体用哪个校区需要 userInfo.campusId，这里留第一个作为兜底
        first = campus_list[0]
        if isinstance(first, dict) and isinstance(first.get("point"), list):
            checkpoints = first["point"]

    middle = raw.get("middleFaces") or []
    if not isinstance(middle, list):
        middle = []

    return ExerciseSetting(
        identify=str(raw.get("identify") or ""),
        total_range_m=_as_float(raw.get("totalRange")) * 1000.0,
        start_qualified_s=_as_float(raw.get("startQualifiedTime")) * 60.0,
        end_qualified_s=(
            _as_float(raw.get("endQualifiedTime")) * 60.0
            if raw.get("endQualifiedTime") not in (None, "", -1, "-1")
            else -1.0
        ),
        step_required=_as_int(raw.get("step")),
        step_recursive=bool(_as_int(raw.get("isStepRecursion"))),
        geofence=geofence if isinstance(geofence, list) else [],
        checkpoints=[c for c in checkpoints if isinstance(c, dict)],
        limit_speed=_as_float(raw.get("limitSpeed")),
        limit_stride=_as_float(raw.get("endStride")),
        limit_step_frequency=_as_float(raw.get("endLimitStepFrequency")),
        is_start_face=_as_bool(raw.get("isStartFace")),
        is_midway_face=_as_bool(raw.get("isMidwayFace")),
        is_end_face=_as_bool(raw.get("isEndFace")),
        middle_face_positions=[float(x) for x in middle if isinstance(x, (int, float, str))],
        user_open_develop=_as_bool(raw.get("userOpenDevelop")),
        remark_id=str(raw["remarkId"]) if raw.get("remarkId") not in (None, "", 0, "0") else None,
        raw=raw,
    )
