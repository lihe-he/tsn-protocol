"""加解密与签名.

协议里同时存在四套凭据，别混：

  1. 公共接口签名   md5(排序后的 k=v&... + appId + appSecret)
  2. 租户接口加密   AES-ECB(json) + RSA(AES key)，每次请求派生新 key
  3. 登录密码       AES-CBC 固定 key/iv = "thanks,pig4cloud"
  4. App 身份       appSignHash = b64(sha256(hex(sha256(apk_cert))))

第 4 套不是账号凭据，是「这个 APK 是官方装的」的证明，服务端用它挡掉
自己重打包的客户端。这也是为什么换一个 App 版本就要更新适配器。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any, Mapping

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from tsn.core.errors import ProtocolError

# —— 协议常量（不是账号凭据；服务端公开可见于请求体）——
APP_ID_PUBLIC = "move"
APP_SECRET_PUBLIC = "e8167ef026cbc5e456ab837d9d6d9254"
APP_ID_TENANT = "c9292ee89d2f49492f983f5931af0d09"
PASSWORD_KEY = b"thanks,pig4cloud"

# 体适能 Android 客户端签名证书的 appSignHash；换包/换签名时需更新
KNOWN_APP_SIGN_HASH = "d2tnIyqximO/L8Y4MzfRELa1hSAtSRxzmvXlcOCzRyk="

RSA_PUBLIC_KEY_B64 = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAgzQ7BYqBZ5LTjoOb9aHO8fI0hbww9YRW2lnq"
    "IdDyIjBwmhthTR+EmiKNm4yFKg6Vz2GW5ix3IdUQdaAq3ZZ7se/dCOTpu3dk15ZgkO6ZImUE7gqzSXXJ0"
    "NaACudk4yJwk3Q69kB4m3xIKxiOlG2HtbEed01LrUmLag9VOP96BuSao2sP4Als5hA/8C6KqdihTOcZF1"
    "RT+lqrT3Qvja7q+qI5QZw9d7NrFFycQs8jk8O49f9mkvLZRZCCWEbwzuCPTlMy/ZNAsMeU/gNSRKUnquO"
    "iPboc2KUhsvY4cK0GeuS9vuIrMGE01L/BCc+rUrautq3n3WiIVJwnwWiJtgk33QIDAQAB"
)

_SUPPORTED_PLATFORMS = ("android",)
_IV_LEN = 16


# ─────────────────────────────  基础摘要  ─────────────────────────────


def md5_hex(text: str | bytes) -> str:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return hashlib.md5(data).hexdigest()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def colon_hex(digest: bytes) -> str:
    """字节串转 'AA:BB:CC' 形式，偶数长度、大写."""
    return ":".join(f"{b:02X}" for b in digest)


# ─────────────────────────────  AES  ─────────────────────────────


def _aes_cbc(key: bytes, iv: bytes):
    if len(key) not in (16, 24, 32):
        raise ProtocolError(f"AES key 长度非法: {len(key)}")
    if len(iv) != _IV_LEN:
        raise ProtocolError(f"AES iv 长度非法: {len(iv)}")
    return AES.new(key, AES.MODE_CBC, iv)


def aes_cbc_encrypt(plaintext: str | bytes, key: bytes, iv: bytes) -> str:
    raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    return base64.b64encode(_aes_cbc(key, iv).encrypt(pad(raw, 16))).decode()


def aes_cbc_encrypt_zero(plaintext: str | bytes, key: bytes, iv: bytes) -> str:
    """zero padding 版本 —— 登录密码走这个.

    协议里密码用的是 '\\x00' 填充而不是 PKCS7，两者密文不通用，
    用错了服务端直接判密码错误。
    """
    raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    pad_len = 16 - (len(raw) % 16)
    padded = raw + b"\x00" * pad_len
    return base64.b64encode(_aes_cbc(key, iv).encrypt(padded)).decode()


def aes_cbc_decrypt_zero(ciphertext_b64: str, key: bytes, iv: bytes) -> str:
    raw = _aes_cbc(key, iv).decrypt(base64.b64decode(ciphertext_b64))
    return raw.rstrip(b"\x00").decode("utf-8")


def aes_cbc_decrypt(ciphertext_b64: str, key: bytes, iv: bytes) -> str:
    try:
        raw = _aes_cbc(key, iv).decrypt(base64.b64decode(ciphertext_b64))
        return unpad(raw, 16).decode("utf-8")
    except (ValueError, KeyError) as exc:
        raise ProtocolError(f"AES-CBC 解密失败: {exc}") from exc


def aes_ecb_encrypt(plaintext: str | bytes, key: bytes) -> str:
    raw = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
    return base64.b64encode(AES.new(key, AES.MODE_ECB).encrypt(pad(raw, 16))).decode()


def aes_ecb_decrypt(ciphertext_b64: str, key: bytes) -> str:
    try:
        raw = AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(ciphertext_b64))
        return unpad(raw, 16).decode("utf-8")
    except (ValueError, KeyError) as exc:
        raise ProtocolError(f"AES-ECB 解密失败: {exc}") from exc


# ─────────────────────  从 token 派生 key / iv  ─────────────────────


def token_key_iv(token: str) -> tuple[bytes, bytes]:
    """CBC 模式：md5(token) 前半做 key，后半做 iv."""
    digest = md5_hex(token)
    return digest[0:16].encode(), digest[16:32].encode()


def derive_session_key(token: str, timestamp: str | int) -> str:
    """每次请求的 AES-128 key：md5(token + timestamp) 前 16 位.

    timestamp 是毫秒字符串，所以同一 token 每次请求 key 都不同，
    重放旧请求体在服务端解不开。
    """
    return md5_hex(f"{token}{timestamp}")[0:16]


# ─────────────────────────────  RSA  ─────────────────────────────

_rsa_key: RSA.RsaKey | None = None


def rsa_encrypt(data: bytes) -> str:
    """PKCS#1 v1.5 加密后 base64。用于把会话 AES key 传给服务端."""
    global _rsa_key
    if _rsa_key is None:
        try:
            _rsa_key = RSA.import_key(base64.b64decode(RSA_PUBLIC_KEY_B64))
        except (ValueError, IndexError) as exc:
            raise ProtocolError(f"RSA 公钥解析失败: {exc}") from exc
    return base64.b64encode(PKCS1_v1_5.new(_rsa_key).encrypt(data)).decode()


# ─────────────────────────  密码 / App 身份  ─────────────────────────


def encrypt_password(password: str) -> str:
    """登录密码：固定 key/iv + zero padding（协议如此，不是我们选的）."""
    return aes_cbc_encrypt_zero(password, PASSWORD_KEY, PASSWORD_KEY)


def app_sign_hash(apk_cert_der: bytes) -> str:
    """APK 签名证书 → appSignHash.

    sha256(cert) → ':' 分隔大写十六进制 → sha256 → base64
    """
    if not apk_cert_der:
        raise ProtocolError("cert_der 不能为空")
    inner = colon_hex(sha256_digest(apk_cert_der))
    return base64.b64encode(sha256_digest(inner.encode())).decode()


def sha256_digest(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ─────────────────────  公共接口签名（md5 排序）  ─────────────────────


def _kv(key: str, value: Any) -> str:
    if value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    return f"{key}={text}"


def build_auth_info(params: Mapping[str, Any]) -> str:
    """按 key 排序拼成 k=v&k=v，签名和 CBC 签名都用这个串."""
    return "&".join(_kv(k, params[k]) for k in sorted(params))


def public_sign(params: Mapping[str, Any], app_secret: str = APP_SECRET_PUBLIC) -> str:
    """公共接口签名：把 appId / appSecret 混入后整体 md5."""
    merged = {k: v for k, v in params.items() if k not in ("sign", "appId", "appSecret")}
    merged["appId"] = APP_ID_PUBLIC
    merged["appSecret"] = app_secret
    return md5_hex(build_auth_info(merged))


def tenant_sign(auth_info: str, token: str) -> str:
    """租户接口的签名：AES-CBC(authInfo) → base64 → md5."""
    key, iv = token_key_iv(token)
    cipher_b64 = aes_cbc_encrypt(auth_info, key, iv)
    return md5_hex(cipher_b64)


# ─────────────────────  租户接口参数加密（核心）  ─────────────────────


def enc_params(
    params: Mapping[str, Any],
    token: str,
    timestamp: str,
    *,
    app_sign: str = KNOWN_APP_SIGN_HASH,
    url_encode: bool = True,
) -> dict[str, str]:
    """把参数打包成 {key, param}.

    key   = RSA(AES key)，让服务端能解出这次会话的 AES key
    param = AES-ECB(排序后的紧凑 JSON)

    'v' 字段塞了 appSign 的密文，服务端解出来后比对，确认是官方客户端。
    """
    from urllib.parse import quote

    session_key = derive_session_key(token, timestamp)
    key_bytes = session_key.encode()

    payload: dict[str, Any] = dict(params)
    payload["v"] = aes_ecb_encrypt(app_sign, key_bytes)

    ordered = {k: payload[k] for k in sorted(payload)}
    body = json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)
    enc_body = aes_ecb_encrypt(body, key_bytes)
    enc_key = rsa_encrypt(key_bytes)

    if url_encode:
        return {"key": quote(enc_key), "param": quote(enc_body)}
    # 表单直传模式：'+' 会被当成空格，协议里用空格代替
    return {"key": enc_key.replace("+", " "), "param": enc_body.replace("+", " ")}


def enc_param_decrypt(param_b64: str, session_key: str) -> str:
    """调试用：用已知会话 key 解出明文参数."""
    return aes_ecb_decrypt(param_b64, session_key.encode())


def b64_json(data: Any) -> str:
    return base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
