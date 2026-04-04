from __future__ import annotations

from zerg.services.managed_session_env import build_managed_session_env_exports
from zerg.services.managed_session_env import get_managed_session_id


def test_get_managed_session_id_prefers_internal_env_name():
    assert get_managed_session_id({"LONGHOUSE_MANAGED_SESSION_ID": "managed-123"}) == "managed-123"


def test_build_managed_session_env_exports_emits_internal_name():
    exports = build_managed_session_env_exports("managed-123")

    assert exports == ["export LONGHOUSE_MANAGED_SESSION_ID=managed-123"]
