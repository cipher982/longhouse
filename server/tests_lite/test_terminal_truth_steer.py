"""The steer cell's ``freed_diagnosis`` records what the host believed about the turn, not just transcript activity."""

from __future__ import annotations

import json

from zerg.qa import terminal_truth_steer as steer
from zerg.routers.session_chat import QueuedInputSummary
from zerg.services.session_state_contract import SessionStateFacts
from zerg.services.session_views import SessionResponse

SID = "00000000-0000-4000-8000-00000000abcd"


class FakeApi:
    """Serves the browser detail route the way the server does; the machine route carries no runtime state."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def get(self, path: str, *, browser: bool = False, timeout: float = 10) -> tuple[int, str]:
        self.calls.append((path, browser))
        if path == f"/api/timeline/sessions/{SID}" and browser:
            return 200, json.dumps(
                {
                    "id": SID,
                    "status": "working",
                    "runtime_phase": "running",
                    "display_phase": "Running tool",
                    "last_activity_at": "2026-11-28T00:00:00Z",
                    "timeline_card": {"tone": "busy"},
                    "session_state": {
                        "turn": {"active": True, "active_call_count": 1},
                        "run": {"state": "active"},
                        "presentation": {"primary": "running_tool"},
                        "transcript": {"event_count": 5},
                    },
                }
            )
        if path == f"/api/agents/sessions/{SID}":
            return 200, json.dumps({"id": SID, "last_activity_at": "2026-11-28T00:00:00Z"})
        if path == f"/api/sessions/{SID}/inputs" and browser:
            return 200, json.dumps(
                [
                    {
                        "text": "after the steer",
                        "intent": "auto",
                        "status": "queued",
                        "disposition": "accepted",
                        "delivery_status": "queued",
                        "turn": None,
                        "attachments": [],
                    },
                    {
                        "text": "the steer",
                        "intent": "steer",
                        "status": "delivered",
                        "disposition": "accepted",
                        "delivery_status": "delivered",
                        "turn": {"turn_id": "t1", "state": "active"},
                    },
                ]
            )
        return 404, "{}"


def test_freed_diagnosis_carries_runtime_phase_and_turn_state() -> None:
    api = FakeApi()
    view = steer.host_view(api, SID)  # type: ignore[arg-type]

    assert (f"/api/timeline/sessions/{SID}", True) in api.calls
    assert view["session"]["runtime_phase"] == "running"
    assert view["session"]["display_phase"] == "Running tool"
    assert view["session"]["status"] == "working"
    assert view["session_state"]["turn"] == {"active": True, "active_call_count": 1}
    assert view["session_state"]["presentation"] == {"primary": "running_tool"}
    assert "transcript" not in view["session_state"]
    assert "timeline_card" not in view["session"]

    queued, delivered = view["inputs"]
    assert queued["disposition"] == "accepted" and queued["turn"] is None
    assert delivered["turn"] == {"turn_id": "t1", "state": "active"}


def test_diagnosis_keys_exist_on_the_served_shapes() -> None:
    """A renamed field fails here instead of silently dumping null on the next CI flake."""

    assert set(steer.HOST_VIEW_SESSION_KEYS) <= set(SessionResponse.model_fields)
    assert set(steer.HOST_VIEW_STATE_KEYS) <= set(SessionStateFacts.model_fields)
    assert set(steer.HOST_VIEW_INPUT_KEYS) <= set(QueuedInputSummary.model_fields)
