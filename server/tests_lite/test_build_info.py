"""Tests for zerg.build_info — build identity loader.

Single path: importlib.resources. Tests monkeypatch resources.files to
simulate bundled vs missing identity. No env-var fallback.
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


class _FakeResource:
    def __init__(self, raw: str | None) -> None:
        self._raw = raw

    def is_file(self) -> bool:
        return self._raw is not None

    def read_text(self, encoding: str = "utf-8") -> str:
        assert self._raw is not None
        return self._raw

    def __truediv__(self, _other: str) -> "_FakeResource":
        return self


def _install_resource(monkeypatch: pytest.MonkeyPatch, payload: dict | str | None) -> None:
    if payload is None:
        raw: str | None = None
    elif isinstance(payload, str):
        raw = payload
    else:
        raw = json.dumps(payload)
    monkeypatch.setattr(build_info.resources, "files", lambda _pkg: _FakeResource(raw))


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


class TestResourceMode:
    def test_loads_bundled_resource(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, VALID_PAYLOAD)
        identity = build_info.load()
        assert identity.commit_short == "b672fcca"
        assert identity.channel == "release"

    def test_missing_resource_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, None)
        with pytest.raises(BuildIdentityMissing, match="missing"):
            build_info.load()

    def test_invalid_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, "{not json")
        with pytest.raises(BuildIdentityMissing, match="not valid JSON"):
            build_info.load()

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        partial = {**VALID_PAYLOAD}
        del partial["commit_short"]
        _install_resource(monkeypatch, partial)
        with pytest.raises(BuildIdentityMissing, match="commit_short"):
            build_info.load()

    def test_invalid_channel_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, {**VALID_PAYLOAD, "channel": "rc"})
        with pytest.raises(BuildIdentityMissing, match="invalid channel"):
            build_info.load()

    def test_non_bool_dirty_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, {**VALID_PAYLOAD, "dirty": "yes"})
        with pytest.raises(BuildIdentityMissing, match="non-bool dirty"):
            build_info.load()

    def test_empty_string_field_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, {**VALID_PAYLOAD, "commit": ""})
        with pytest.raises(BuildIdentityMissing, match="invalid commit"):
            build_info.load()


class TestCaching:
    def test_subsequent_calls_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_resource(monkeypatch, VALID_PAYLOAD)
        first = build_info.load()
        # Swap the fake resource to a different payload — cached load() should
        # still return the first identity until reset_cache().
        _install_resource(monkeypatch, {**VALID_PAYLOAD, "commit_short": "abcdef12"})
        second = build_info.load()
        assert first is second
        assert second.commit_short == "b672fcca"

        build_info.reset_cache()
        third = build_info.load()
        assert third.commit_short == "abcdef12"


# Silence unused-path parameter if any pytest collector insists.
_ = Path
