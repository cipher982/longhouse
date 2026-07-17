from __future__ import annotations

import json

from zerg.qa.cursor_helm_product_e2e import _hook_rows
from zerg.qa.cursor_helm_product_e2e import _pending_pause
from zerg.qa.cursor_helm_product_e2e import _state_ids
from zerg.qa.cursor_helm_product_e2e import _visible_texts


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
    assert _visible_texts(
        {"events": [{"role": "system", "content_text": "hidden"}, {"role": "assistant", "content_text": "ready"}]}
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
