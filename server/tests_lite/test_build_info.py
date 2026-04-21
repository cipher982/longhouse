"""Tests for zerg.build_info — build identity loader.

Two modes, no fallback. Cover both and their failure surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zerg import build_info
from zerg.build_info import BuildIdentity
from zerg.build_info import BuildIdentityMissing


VALID_PAYLOAD = {
    "version": "0.2.0",
    "commit": "b672fccae990c020de56139d38dcd9990bae7aa0",
    "commit_short": "b672fcca",
    "dirty": False,
    "built_at": "2026-04-21T18:03:12Z",
    "channel": "release",
}


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _reset_cache():
    build_info.reset_cache()
    yield
    build_info.reset_cache()


class TestQualifiedVersion:
    def test_release(self) -> None:
        identity = BuildIdentity(**VALID_PAYLOAD)
        assert identity.qualified_version == "0.2.0 (b672fcca)"

    def test_dev_clean(self) -> None:
        identity = BuildIdentity(**{**VALID_PAYLOAD, "channel": "dev"})
        assert identity.qualified_version == "0.2.0-dev+b672fcca"

    def test_dev_dirty(self) -> None:
        identity = BuildIdentity(**{**VALID_PAYLOAD, "channel": "dev", "dirty": True})
        assert identity.qualified_version == "0.2.0-dev+b672fcca.dirty"


class TestEnvMode:
    def test_loads_from_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        identity_file = _write(tmp_path / "build-identity.json", VALID_PAYLOAD)
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(identity_file))

        identity = build_info.load()
        assert identity.commit_short == "b672fcca"
        assert identity.channel == "release"

    def test_missing_env_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(tmp_path / "nope.json"))
        with pytest.raises(BuildIdentityMissing, match="no file exists"):
            build_info.load()

    def test_invalid_json_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(bad))
        with pytest.raises(BuildIdentityMissing, match="not valid JSON"):
            build_info.load()

    def test_missing_key_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        partial = {**VALID_PAYLOAD}
        del partial["commit_short"]
        identity_file = _write(tmp_path / "partial.json", partial)
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(identity_file))
        with pytest.raises(BuildIdentityMissing, match="commit_short"):
            build_info.load()

    def test_empty_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", "   ")
        with pytest.raises(BuildIdentityMissing, match="set but empty"):
            build_info.load()

    def test_invalid_channel_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = _write(tmp_path / "bad.json", {**VALID_PAYLOAD, "channel": "rc"})
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(bad))
        with pytest.raises(BuildIdentityMissing, match="invalid channel"):
            build_info.load()

    def test_non_bool_dirty_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = _write(tmp_path / "bad.json", {**VALID_PAYLOAD, "dirty": "yes"})
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(bad))
        with pytest.raises(BuildIdentityMissing, match="non-bool dirty"):
            build_info.load()

    def test_empty_string_field_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bad = _write(tmp_path / "bad.json", {**VALID_PAYLOAD, "commit": ""})
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(bad))
        with pytest.raises(BuildIdentityMissing, match="invalid commit"):
            build_info.load()


class TestResourceMode:
    def test_missing_resource_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When LONGHOUSE_BUILD_IDENTITY_PATH is unset and the wheel resource
        is missing, load() must raise BuildIdentityMissing.
        """
        monkeypatch.delenv("LONGHOUSE_BUILD_IDENTITY_PATH", raising=False)

        class _MissingRef:
            def is_file(self) -> bool:
                return False

            def __truediv__(self, other: str) -> "_MissingRef":
                return self

        monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _MissingRef())
        with pytest.raises(BuildIdentityMissing, match="missing"):
            build_info.load()

    def test_reads_bundled_resource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When the env var is unset, load() reads the packaged resource."""
        monkeypatch.delenv("LONGHOUSE_BUILD_IDENTITY_PATH", raising=False)
        raw = json.dumps(VALID_PAYLOAD)

        class _Ref:
            def is_file(self) -> bool:
                return True

            def read_text(self, encoding: str = "utf-8") -> str:
                return raw

            def __truediv__(self, other: str) -> "_Ref":
                return self

        monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _Ref())

        identity = build_info.load()
        assert identity.commit_short == "b672fcca"


class TestCaching:
    def test_subsequent_calls_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        identity_file = _write(tmp_path / "build-identity.json", VALID_PAYLOAD)
        monkeypatch.setenv("LONGHOUSE_BUILD_IDENTITY_PATH", str(identity_file))

        first = build_info.load()
        # Overwrite the file — cached load() should still return the first value.
        second_payload = {**VALID_PAYLOAD, "commit_short": "abcdef12"}
        identity_file.write_text(json.dumps(second_payload))
        second = build_info.load()
        assert first is second
        assert second.commit_short == "b672fcca"

        build_info.reset_cache()
        third = build_info.load()
        assert third.commit_short == "abcdef12"
