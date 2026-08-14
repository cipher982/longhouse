from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import httpx

from zerg.qa.cursor_helm_product_e2e import _accept_workspace_trust_if_prompted
from zerg.qa.cursor_helm_product_e2e import _assistant_texts
from zerg.qa.cursor_helm_product_e2e import _can_send_canonical
from zerg.qa.cursor_helm_product_e2e import _can_send_live
from zerg.qa.cursor_helm_product_e2e import _canonical_state_from_diagnostics
from zerg.qa.cursor_helm_product_e2e import _hook_rows
from zerg.qa.cursor_helm_product_e2e import _pending_pause
from zerg.qa.cursor_helm_product_e2e import _response_observed_at
from zerg.qa.cursor_helm_product_e2e import _session_locked
from zerg.qa.cursor_helm_product_e2e import _state_ids
from zerg.qa.cursor_helm_product_e2e import build_arg_parser


def test_machine_agent_restart_uses_reconnected_status_after_kickstart_timeout(tmp_path, monkeypatch) -> None:
    from zerg.qa import cursor_helm_product_e2e as product_e2e

    status_path = tmp_path / "engine-status.json"
    status_path.write_text(json.dumps({"daemon_pid": 101, "control_channel": {"status": "connected"}}))

    def timed_out_kickstart(*_args, **_kwargs):
        status_path.write_text(json.dumps({"daemon_pid": 202, "control_channel": {"status": "connected"}}))
        raise subprocess.TimeoutExpired(cmd="launchctl kickstart", timeout=15)

    monkeypatch.setattr(product_e2e.sys, "platform", "darwin")
    monkeypatch.setattr(product_e2e.subprocess, "run", timed_out_kickstart)

    assert product_e2e._restart_machine_agent(status_path, timeout=0.1) == {
        "old_pid": 101,
        "new_pid": 202,
        "control_status": "connected",
        "kickstart_timed_out": True,
    }


def test_workspace_trust_prompt_is_explicitly_accepted(tmp_path) -> None:
    terminal_path = tmp_path / "terminal.raw"
    terminal_path.write_text(
        "Workspace Trust Required\nDo you trust the contents of this directory?\n[a] Trust this workspace",
        encoding="utf-8",
    )
    sent: list[str] = []
    session = SimpleNamespace(
        terminal_path=terminal_path,
        process=SimpleNamespace(poll=lambda: None),
        send=sent.append,
    )

    assert _accept_workspace_trust_if_prompted(session, timeout=0.1) is True
    assert sent == ["a"]


def test_turn_boundary_only_is_explicit_and_disabled_for_the_release_canary() -> None:
    parser = build_arg_parser()

    assert parser.parse_args([]).turn_boundary_only is False
    assert parser.parse_args(["--turn-boundary-only"]).turn_boundary_only is True


def test_canonical_state_requires_a_matched_diagnostic_snapshot() -> None:
    shadow = {"activity": {"state": "quiescent"}, "run": {"lifecycle": "running", "id": "run-1"}}
    payload = {
        "served_path": "canonical_session_detail",
        "comparison": {"status": "matched"},
        "shadow": shadow,
    }

    assert _canonical_state_from_diagnostics(payload) == shadow
    # The comparison is against the legacy projection. A difference is why
    # this canary must consume the canonical `shadow` axes, not reject them.
    assert _canonical_state_from_diagnostics({**payload, "comparison": {"status": "different"}}) == shadow


def test_product_e2e_helpers_parse_managed_state_hooks_and_visible_events(tmp_path) -> None:
    (tmp_path / "one.json").write_text(json.dumps({"session_id": "session-1", "socket_path": "/tmp/socket"}))
    (tmp_path / "session-1.phase.json").write_text(json.dumps({"session_id": "session-1", "phase": "idle"}))
    hooks = tmp_path / "hook-events"
    hooks.mkdir()
    (hooks / "session-1.ndjson").write_text(
        json.dumps(
            {
                "event": "afterAgentResponse",
                "observed_at": "2026-07-17T00:00:00Z",
                "payload": {"generation_id": "generation-1", "text": "ready"},
            }
        )
        + "\n"
    )

    assert _state_ids(tmp_path) == {"session-1"}
    assert _hook_rows(tmp_path, "session-1") == [
        {
            "event": "afterAgentResponse",
            "generation_id": "generation-1",
            "observed_at": "2026-07-17T00:00:00Z",
            "text": "ready",
        }
    ]
    assert _assistant_texts(
        {
            "events": [
                {"role": "system", "content_text": "hidden"},
                {"role": "user", "content_text": "marker must not prove a reply"},
                {"role": "assistant", "content_text": "ready"},
            ]
        }
    ) == ["ready"]
    assert _pending_pause(
        {
            "requests": [
                {"id": "done", "status": "resolved", "can_respond": True},
                {"id": "blocked", "status": "pending", "can_respond": False},
                {"id": "ready", "status": "pending", "can_respond": True},
            ]
        }
    ) == {"id": "ready", "status": "pending", "can_respond": True}
    assert (
        _response_observed_at(
            [
                {"event": "afterAgentResponse", "text": "old", "observed_at": "2026-07-17T00:00:00+00:00"},
                {"event": "afterAgentResponse", "text": "marker", "observed_at": "2026-07-17T00:00:01+00:00"},
            ],
            "marker",
        ).isoformat()
        == "2026-07-17T00:00:01+00:00"
    )
    assert _can_send_live({"capabilities": {"can_send_input": True}}) is True
    assert _can_send_live({"capabilities": {"can_send_input": False}}) is False
    assert _can_send_live({}) is False
    assert _can_send_canonical({"control": {"actions": {"send_input": {"state": "available"}}}}) is True
    assert _can_send_canonical({"control": {"actions": {"send_input": {"state": "unavailable"}}}}) is False
    assert (
        _session_locked(
            httpx.Response(
                409,
                json={"detail": {"code": "SESSION_LOCKED"}},
                request=httpx.Request("POST", "https://example.invalid"),
            )
        )
        is True
    )
    assert (
        _session_locked(
            httpx.Response(
                409,
                json={"detail": "not attached"},
                request=httpx.Request("POST", "https://example.invalid"),
            )
        )
        is False
    )
