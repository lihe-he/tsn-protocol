"""加密与签名测试.

这些测试是协议实现的回归保护：一旦签名逻辑改错，
服务端会统一返回「签名错误」，排查起来非常痛苦，所以必须锁死。
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from tsn.core import crypto


class TestHash:
    def test_md5_hex_str(self) -> None:
        assert crypto.md5_hex("abc") == "900150983cd24fb0d6963f7d28e17f72"

    def test_md5_hex_bytes(self) -> None:
        assert crypto.md5_hex(b"abc") == crypto.md5_hex("abc")

    def test_colon_hex(self) -> None:
        assert crypto.colon_hex(bytes([0x7F, 0xC0, 0x22])) == "7F:C0:22"


class TestTokenDerivation:
    def test_key_iv_split(self) -> None:
        token = "abc123"
        digest = hashlib.md5(token.encode()).hexdigest()
        key, iv = crypto.token_key_iv(token)
        assert key == digest[0:16].encode()
        assert iv == digest[16:32].encode()

    def test_session_key_changes_with_timestamp(self) -> None:
        token = "abc123"
        a = crypto.derive_session_key(token, "1000")
        b = crypto.derive_session_key(token, "1001")
        assert a != b
        assert len(a) == 16

    def test_session_key_stable(self) -> None:
        assert crypto.derive_session_key("t", "123") == crypto.derive_session_key("t", "123")


class TestAES:
    def test_cbc_roundtrip(self) -> None:
        key, iv = crypto.token_key_iv("tok")
        text = "hello 世界 123"
        enc = crypto.aes_cbc_encrypt(text, key, iv)
        assert crypto.aes_cbc_decrypt(enc, key, iv) == text

    def test_ecb_roundtrip(self) -> None:
        key = b"0123456789abcdef"
        text = '{"a":1,"b":"中文"}'
        enc = crypto.aes_ecb_encrypt(text, key)
        assert crypto.aes_ecb_decrypt(enc, key) == text

    def test_zero_padding_differs_from_pkcs7(self) -> None:
        key = crypto.PASSWORD_KEY
        text = "123456"
        zero = crypto.aes_cbc_encrypt_zero(text, key, key)
        pkcs = crypto.aes_cbc_encrypt(text, key, key)
        assert zero != pkcs
        assert crypto.aes_cbc_decrypt_zero(zero, key, key) == text

    def test_zero_padding_full_block(self) -> None:
        """正好 16 字节时 zero padding 会补满一整块 16 个 0x00."""
        key = crypto.PASSWORD_KEY
        text = "1234567890abcdef"  # 16 字节
        enc = crypto.aes_cbc_encrypt_zero(text, key, key)
        raw = base64.b64decode(enc)
        assert len(raw) == 32

    def test_encrypt_password_uses_zero_padding(self) -> None:
        pw = "mypassword"
        expected = crypto.aes_cbc_encrypt_zero(pw, crypto.PASSWORD_KEY, crypto.PASSWORD_KEY)
        assert crypto.encrypt_password(pw) == expected

    def test_bad_key_length(self) -> None:
        with pytest.raises(crypto.ProtocolError):
            crypto.aes_cbc_encrypt("x", b"short", b"0" * 16)


class TestRSASigning:
    def test_rsa_encrypt_produces_base64(self) -> None:
        out = crypto.rsa_encrypt(b"0123456789abcdef")
        raw = base64.b64decode(out)
        assert len(raw) == 256  # 2048-bit

    def test_app_sign_hash_shape(self) -> None:
        cert = b"\x30\x82\x01\x00fake-cert-body"
        h = crypto.app_sign_hash(cert)
        assert len(base64.b64decode(h)) == 32  # sha256

    def test_app_sign_hash_rejects_empty(self) -> None:
        with pytest.raises(crypto.ProtocolError):
            crypto.app_sign_hash(b"")


class TestPublicSign:
    def test_deterministic(self) -> None:
        params = {"schoolId": 1, "appSign": "XX"}
        assert crypto.public_sign(params) == crypto.public_sign(params)

    def test_order_independent(self) -> None:
        a = crypto.public_sign({"b": 2, "a": 1})
        b = crypto.public_sign({"a": 1, "b": 2})
        assert a == b

    def test_value_sensitive(self) -> None:
        assert crypto.public_sign({"a": 1}) != crypto.public_sign({"a": 2})

    def test_ignores_sign_field(self) -> None:
        a = crypto.public_sign({"a": 1})
        b = crypto.public_sign({"a": 1, "sign": "whatever"})
        assert a == b

    def test_build_auth_info_format(self) -> None:
        info = crypto.build_auth_info({"b": 2, "a": "x", "c": None, "d": True})
        assert info == "a=x&b=2&c=null&d=true"


class TestEncParams:
    def test_has_key_and_param(self) -> None:
        out = crypto.enc_params({"runType": 2}, "token", "1700000000000")
        assert set(out) == {"key", "param"}

    def test_param_is_decryptable_with_session_key(self) -> None:
        token, ts = "tok", "1700000000000"
        out = crypto.enc_params({"runType": 2}, token, ts)
        import urllib.parse

        session_key = crypto.derive_session_key(token, ts)
        plain = crypto.enc_param_decrypt(urllib.parse.unquote(out["param"]), session_key)
        assert '"runType":2' in plain
        assert '"v"' in plain

    def test_key_differs_per_timestamp(self) -> None:
        a = crypto.enc_params({}, "tok", "1")
        b = crypto.enc_params({}, "tok", "2")
        import urllib.parse

        assert urllib.parse.unquote(a["param"]) != urllib.parse.unquote(b["param"])
