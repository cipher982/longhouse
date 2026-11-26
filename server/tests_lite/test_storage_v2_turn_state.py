"""The current turn's start and running calls, folded from sealed render objects."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from zerg.services.session_state_facts_projector import project_turn_facts
from zerg.storage_v2.render_objects import RenderRecord
from zerg.storage_v2.turn_state import EMPTY_TURN_STATE
from zerg.storage_v2.turn_state import encode_turn_json
from zerg.storage_v2.turn_state import fold_turn_state
from zerg.storage_v2.turn_state import fold_turn_tails
from zerg.storage_v2.turn_state import turn_tail
from zerg.storage_v2.turn_state import validate_turn_tail

T0 = 1_700_000_000_000_000
_position = iter(range(1_000_000))


def _record(role: str, *, at: int, **fields) -> RenderRecord:
    position = next(_position)
    return RenderRecord(
        event_id=f"e{position}",
        order_time_us=T0 + at,
        source_position=position,
        event_subordinal=0,
        role=role,
        **fields,
    )


def _user(at: int, text: str = "Fix the build") -> RenderRecord:
    return _record("user", at=at, content_text=text, interaction_kind="durable_user_message")


def _call(at: int, call_id: str, tool: str = "Bash", tool_input: object | None = None) -> RenderRecord:
    return _record("assistant", at=at, tool_name=tool, tool_call_id=call_id, tool_input_json=tool_input)


def _result(at: int, call_id: str) -> RenderRecord:
    return _record("tool", at=at, tool_call_id=call_id, tool_output_text="ok")


def _say(at: int, text: str = "Looking at the failure") -> RenderRecord:
    return _record("assistant", at=at, content_text=text)


def _served(*objects: tuple[RenderRecord, ...]):
    state = fold_turn_tails(turn_tail("claude", records) for records in objects)
    return project_turn_facts(encode_turn_json(state))


def test_parallel_calls_count_until_their_results_arrive_across_objects():
    turn = _served(
        (_user(0), _say(1), _call(2, "a", tool_input={"command": "make test", "description": "Run the tests"}), _call(3, "b", "Read")),
        (_result(4, "a"),),
    )
    assert turn.started_at == datetime.fromtimestamp(T0 / 1_000_000, tz=UTC)
    assert turn.active_call_count == 1
    assert turn.lead_call is not None
    assert (turn.lead_call.tool_call_id, turn.lead_call.status) == ("b", "Reading files")

    both = _served((_user(0), _call(2, "a", tool_input={"command": "make test", "description": "Run the tests"}), _call(3, "b", "Read")))
    assert both.active_call_count == 2
    assert both.lead_call.headline == "Run the tests"
    assert both.lead_call.status == "Running a command"


def test_a_call_the_model_moved_past_is_history():
    """A call with no result is not running once the model speaks after it."""
    turn = _served((_user(0), _call(1, "lost"), _say(2, "That hung, trying another way"), _call(3, "next")))
    assert turn.active_call_count == 1
    assert turn.lead_call.tool_call_id == "next"


def test_a_new_user_turn_restarts_the_clock_and_clears_running_calls():
    turn = _served((_user(0), _call(1, "a")), (_user(10, "Stop, do this instead"),))
    assert turn.started_at == datetime.fromtimestamp((T0 + 10) / 1_000_000, tz=UTC)
    assert turn.active_call_count == 0
    assert turn.lead_call is None


def test_a_turn_end_clears_running_calls_but_keeps_the_turn_start():
    turn = _served((_user(0), _call(1, "a")), (_record("assistant", at=2, item={"kind": "turn_end", "end_reason": "interrupted"}),))
    assert turn.active_call_count == 0
    assert turn.started_at is not None


def test_notifications_and_abandoned_prompts_are_not_turns():
    turn = _served(
        (
            _user(0),
            _call(1, "a"),
            _record("user", at=2, content_text="Background task finished", interaction_kind="provider_notification"),
            _record("user", at=3, content_text="an edited-away prompt", branch_kind="abandoned"),
        )
    )
    assert turn.started_at == datetime.fromtimestamp(T0 / 1_000_000, tz=UTC)
    assert turn.active_call_count == 1


def test_history_sealed_before_tails_reads_unknown_until_the_next_prompt():
    unknown = fold_turn_state({"turn_at_us": T0, "open": []}, None)
    assert unknown == {"turn_at_us": T0, "open": None}
    served = project_turn_facts(encode_turn_json(unknown))
    assert served.active_call_count is None
    # A result for an unknown call cannot make the running set known...
    still = fold_turn_state(unknown, turn_tail("claude", (_result(5, "x"),)))
    assert still["open"] is None
    # ...a prompt can.
    known = fold_turn_state(still, turn_tail("claude", (_user(6), _call(7, "y"))))
    assert [call["id"] for call in known["open"]] == ["y"]


def test_a_fresh_session_starts_from_a_known_empty_turn():
    state = fold_turn_state(EMPTY_TURN_STATE, turn_tail("claude", (_say(1),)))
    assert state == {"turn_at_us": None, "open": []}


def test_the_catalog_accepts_only_the_tail_shape_it_folds():
    tail = turn_tail("claude", (_user(0), _call(1, "a")))
    assert validate_turn_tail(tail) is not None
    assert validate_turn_tail({**tail, "extra": 1}) is None
    assert validate_turn_tail({**tail, "open": [{"id": "a"}]}) is None
    assert validate_turn_tail(None) is None
