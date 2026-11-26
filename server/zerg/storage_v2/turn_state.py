"""The current turn's live state, folded from render objects as they are sealed.

Clients used to scrape two facts out of whatever transcript page they had
loaded: when the current turn started (the newest user message) and which tool
calls are running now. A page that had not loaded the turn's start, or loaded
it late, made the web and the phone disagree. The server owns both now.

Each sealed render object carries a small ``turn_tail``: what its records say
about the turn in progress. The catalog folds tails in order onto the session
row (``sessions.turn_state``), so a read costs one JSON column the snapshot
already loads, never a transcript read.

A call is running when it is in the current turn, has no result yet, and the
model has not spoken since it was made (a thought or a message after a call
means the call is history, the same boundary the web transcript draws). A user
turn or a provider turn end clears the running set.

``open`` is ``None`` when a folded object predates tails, so the running set
is unknown rather than empty; the next user turn makes it known again.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

from zerg.provider_contract.provider_interaction_semantics import INTERACTION_PROVIDER_NOTIFICATION
from zerg.provider_contract.provider_interaction_semantics import semantic_event_included
from zerg.services.tool_presentation import resolve_tool_intent

# A turn with more running calls than this is still reported as this many; the
# count is a status line, not an inventory.
MAX_OPEN_CALLS = 64
_ID_CHARS = 255
_TOOL_CHARS = 255
_INTENT_CHARS = 160
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# A session with nothing served yet: no turn, and positively no running call.
EMPTY_TURN_STATE: Mapping[str, Any] = {"turn_at_us": None, "open": []}


def _is_user_turn(provider: str, record: Any) -> bool:
    return (
        record.role == "user"
        and not record.tool_call_id
        and record.interaction_kind != INTERACTION_PROVIDER_NOTIFICATION
        and semantic_event_included(
            provider,
            role=record.role,
            content_text=record.content_text,
            interaction_kind=record.interaction_kind,
        )
    )


def _model_spoke(record: Any) -> bool:
    return record.role == "assistant" and record.tool_name is None


def _is_turn_end(record: Any) -> bool:
    return isinstance(record.item, Mapping) and record.item.get("kind") == "turn_end"


def turn_tail(provider: str, records: Iterable[Any]) -> dict[str, Any]:
    """What one render object's records say about the turn in progress.

    ``turn_at_us`` is the newest user turn in the object; ``cleared`` says the
    calls running before this object are no longer running; ``open`` lists the
    calls still running at its end, oldest first; ``closed`` lists results in
    the object for calls made in an earlier object.
    """

    turn_at_us: int | None = None
    cleared = False
    open_calls: dict[str, dict[str, Any]] = {}
    closed: list[str] = []
    for record in records:
        if record.branch_kind == "abandoned":
            continue
        if _is_user_turn(provider, record):
            turn_at_us = int(record.order_time_us)
            cleared = True
            open_calls.clear()
            closed.clear()
        elif _is_turn_end(record) or _model_spoke(record):
            cleared = True
            open_calls.clear()
            closed.clear()
        elif record.role == "tool" and record.tool_call_id:
            call_id = str(record.tool_call_id)[:_ID_CHARS]
            if open_calls.pop(call_id, None) is None and call_id not in closed and len(closed) < MAX_OPEN_CALLS * 4:
                closed.append(call_id)
        elif record.tool_name is not None and record.role != "tool":
            call_id = str(record.tool_call_id or record.event_id)[:_ID_CHARS]
            intent, _ = resolve_tool_intent(
                record.tool_name,
                record.tool_input_json,
                provider_intents=((record.item or {}).get("intent"),),
            )
            open_calls[call_id] = {
                "id": call_id,
                "tool": str(record.tool_name)[:_TOOL_CHARS],
                "at_us": int(record.order_time_us),
                "intent": intent[:_INTENT_CHARS] if intent else None,
            }
    return {
        "turn_at_us": turn_at_us,
        "cleared": cleared,
        "open": list(open_calls.values())[-MAX_OPEN_CALLS:],
        "closed": closed,
    }


def fold_turn_state(state: Mapping[str, Any] | None, tail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Apply one object's tail to the session's turn state, in transcript order."""

    previous_turn = state.get("turn_at_us") if state else None
    previous_open = state.get("open") if state else None
    if not isinstance(tail, Mapping):
        # An object sealed before tails existed: the turn start stands, the
        # running set is unknown until a user turn clears it.
        return {"turn_at_us": previous_turn, "open": None}
    turn_at_us = tail.get("turn_at_us") if tail.get("turn_at_us") is not None else previous_turn
    tail_open = [dict(call) for call in tail.get("open") or ()]
    if tail.get("cleared"):
        open_calls: list[dict[str, Any]] | None = tail_open
    elif previous_open is None:
        open_calls = None
    else:
        closed = set(tail.get("closed") or ())
        open_calls = [dict(call) for call in previous_open if call.get("id") not in closed] + tail_open
    if open_calls is not None:
        open_calls = open_calls[-MAX_OPEN_CALLS:]
    return {"turn_at_us": turn_at_us, "open": open_calls}


def fold_turn_tails(tails: Iterable[Mapping[str, Any] | None]) -> dict[str, Any]:
    state: dict[str, Any] = dict(EMPTY_TURN_STATE)
    for tail in tails:
        state = fold_turn_state(state, tail)
    return state


# The catalog snapshot cuts text columns at a byte limit (store._CATALOG_TEXT_LIMITS
# "turn_state"); a cut JSON value would erase the whole axis, so encoding stays
# well under it by dropping intents, then the oldest calls.
MAX_TURN_JSON_BYTES = 32_768


def encode_turn_json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) <= MAX_TURN_JSON_BYTES or not isinstance(value.get("open"), list):
        return encoded
    calls = [{**call, "intent": None} if index else dict(call) for index, call in enumerate(value["open"])]
    while True:
        encoded = json.dumps({**value, "open": calls}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) <= MAX_TURN_JSON_BYTES or not calls:
            return encoded
        calls = calls[1:] if len(calls) > 1 else []


def decode_turn_json(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def turn_time(order_time_us: object) -> datetime | None:
    if type(order_time_us) is not int:
        return None
    try:
        return _EPOCH + timedelta(microseconds=order_time_us)
    except OverflowError:
        return None


def validate_turn_tail(value: object) -> str | None:
    """A manifest's tail as canonical JSON, or None when it is not one.

    Like the turn list, the tail is derived from the immutable object, so a
    malformed one is dropped (the running set reads unknown) rather than
    failing the commit that carries it.
    """

    if not isinstance(value, Mapping) or set(value) != {"turn_at_us", "cleared", "open", "closed"}:
        return None
    if value["turn_at_us"] is not None and type(value["turn_at_us"]) is not int:
        return None
    if type(value["cleared"]) is not bool:
        return None
    open_calls = value["open"]
    closed = value["closed"]
    if not isinstance(open_calls, list) or len(open_calls) > MAX_OPEN_CALLS:
        return None
    if not isinstance(closed, list) or len(closed) > MAX_OPEN_CALLS * 4:
        return None
    for call in open_calls:
        if not isinstance(call, Mapping) or set(call) != {"id", "tool", "at_us", "intent"}:
            return None
        if not isinstance(call["id"], str) or not call["id"] or len(call["id"]) > _ID_CHARS:
            return None
        if not isinstance(call["tool"], str) or not call["tool"] or len(call["tool"]) > _TOOL_CHARS:
            return None
        if type(call["at_us"]) is not int:
            return None
        if call["intent"] is not None and (not isinstance(call["intent"], str) or len(call["intent"]) > _INTENT_CHARS):
            return None
    if not all(isinstance(call_id, str) and call_id and len(call_id) <= _ID_CHARS for call_id in closed):
        return None
    return encode_turn_json(value)
