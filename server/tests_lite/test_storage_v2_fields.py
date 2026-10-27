from __future__ import annotations

import pytest

from zerg.storage_v2.fields import parse_session_facts


def _session_facts(**overrides) -> dict:
    return {
        "environment": "local",
        "project": "longhouse",
        "cwd": "/workspace/longhouse",
        "git_repo": "cipher982/longhouse",
        "git_branch": "main",
        "started_at": "2026-07-12T11:00:00Z",
        "last_activity_at": "2026-07-12T12:00:00+00:00",
        "ended_at": None,
        "origin_kind": "shadow",
        "hidden_from_default_timeline": False,
        "launch_actor": None,
        "launch_surface": None,
        **overrides,
    }


def test_reported_paths_keep_their_normalization_but_identity_must_be_nfc():
    parsed = parse_session_facts(_session_facts(cwd="/Users/me/Café", git_branch="café"))

    assert parsed["cwd"] == "/Users/me/Café"
    assert parsed["git_branch"] == "café"
    with pytest.raises(ValueError, match="NFC"):
        parse_session_facts(_session_facts(provider_session_id="café"))


@pytest.mark.parametrize("value", ["", "x" * 256, "café"])
def test_edge_refuses_lineage_ids_catalogd_would_refuse(value):
    # The edge once accepted any string here while catalogd required a short
    # canonical id, so the host refused envelopes its own edge had admitted.
    with pytest.raises(ValueError, match="parent_tool_call_id"):
        parse_session_facts(_session_facts(is_subagent=True, parent_tool_call_id=value))


def test_absent_optional_fields_mean_what_an_older_engine_meant():
    parsed = parse_session_facts(_session_facts())

    assert parsed["is_subagent"] is False
    assert parsed["provider_session_id"] is None
    assert parsed["parent_tool_call_id"] is None
    assert parsed["started_at"] == "2026-07-12T11:00:00+00:00"
