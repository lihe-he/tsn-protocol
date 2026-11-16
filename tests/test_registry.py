"""适配器注册表测试.

重点验证「App 版本 → adapter」的匹配逻辑：这是升级时最容易出错的地方。
"""

from __future__ import annotations

import pytest

from tsn.adapters import base, registry
from tsn.adapters.base import ProtocolAdapter, parse_version
from tsn.core.errors import UnsupportedVersion


class TestParseVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("2.0.23", (2, 0, 23, 0)),
            ("2.0", (2, 0, 0, 0)),
            ("2", (2, 0, 0, 0)),
            ("v2.0.26", (2, 0, 26, 0)),
            ("2.0.23-beta", (2, 0, 23, 0)),
            ("", (0, 0, 0, 0)),
            ("abc", (0, 0, 0, 0)),
        ],
    )
    def test_parse(self, text: str, expected: tuple[int, ...]) -> None:
        assert parse_version(text) == expected


class TestSupports:
    def test_inside_range(self) -> None:
        from tsn.adapters.v2023 import V2023Adapter

        assert V2023Adapter.supports("2.0.23")
        assert V2023Adapter.supports("2.0.0")
        assert V2023Adapter.supports("2.0.99")

    def test_outside_range(self) -> None:
        from tsn.adapters.v2023 import V2023Adapter

        assert not V2023Adapter.supports("1.9.9")
        assert not V2023Adapter.supports("2.1.0")

    def test_empty_version(self) -> None:
        from tsn.adapters.v2023 import V2023Adapter

        assert not V2023Adapter.supports("")


class TestPick:
    def test_registered(self) -> None:
        rows = registry.available()
        assert any(r["name"] == "v2023" for r in rows)

    def test_pick_known_version(self) -> None:
        cls = registry.pick("2.0.23")
        assert cls.name == "v2023"

    def test_pick_unknown_raises(self) -> None:
        with pytest.raises(UnsupportedVersion):
            registry.pick("9.9.9")

    def test_latest_range(self) -> None:
        lo, hi = registry.latest_version_range()
        assert parse_version(hi) >= parse_version("2.0.23")


class TestRegisterValidation:
    def test_rejects_non_adapter(self) -> None:
        with pytest.raises(TypeError):

            @registry.register
            class NotAnAdapter:  # noqa: D401
                name = "bogus"

    def test_duplicate_name_rejected(self) -> None:
        class Dup(ProtocolAdapter):
            name = "v2023"
            version_range = ("0.0.0", "0.0.0")

            async def login(self, username, password): ...
            async def fetch_app_version(self): ...
            async def get_setting(self, run_type, lng, lat): ...
            async def get_start_time(self, identify): ...
            async def get_summary(self): ...
            async def submit_record(self, draft): ...
            async def get_record(self, record_id): ...
            async def list_records(self): ...
            async def upload_face(self, image, coords, identify, face_type): ...
            async def list_user_faces(self): ...

        with pytest.raises(ValueError, match="重复"):
            registry.register(Dup)


class TestSnapshot:
    def test_all_adapters_have_range(self) -> None:
        for cls in registry.iter_adapters():
            lo, hi = cls.version_range
            assert parse_version(lo) <= parse_version(hi)
            assert cls.name
