#!/usr/bin/env python3
"""One Helm lifecycle for the two providers of the Pi family, OMP and Pi.

Both run the same extension-channel launcher in the engine and write the same
session tree, so one engine drives both through launch, send, follow-up,
steer, abort, session replacement, terminate and cold resume, and records
what it saw in one shape: an owner state per execution owner, one record per
control (``controls``), one per served convergence, and the cleanup. A
provider's module holds its registration, its ``Family`` (what differs) and
its cells, which read that record and nothing else.

What differs is of two kinds, and ``Family`` keeps them apart. Some is the
provider: only OMP's extension reports the end of a turn on the channel, and
only Pi is known to reload its extension. The rest is how each lifecycle
happens to run today (which transport carries a control, which waits end the
run and which are left to a cell): coverage one provider has and the other
lacks, to be closed one verdict change at a time.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import scripted_model_profiles
from zerg.qa import scripted_model_server
from zerg.qa.answer_markers import marker_answered
from zerg.qa.console_served_state_core import MarkerAnswerCountError
from zerg.qa.console_served_state_core import StandingServedAnswerFault
from zerg.qa.console_served_state_core import assistant_marker_events
from zerg.qa.console_served_state_core import assistant_marker_mentions
from zerg.qa.console_served_state_core import event_text
from zerg.qa.console_served_state_core import served_answer_correspondence
from zerg.qa.console_served_state_core import served_answer_fault
from zerg.qa.live_kit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_kit import RUNTIME_API_URL_ENV
from zerg.qa.live_kit import RuntimeHostError
from zerg.qa.live_kit import group_dead as _pgid_dead
from zerg.qa.live_kit import pid_alive as _pid_alive
from zerg.qa.live_kit import pid_dead as _pid_dead
from zerg.qa.live_kit import poll
from zerg.qa.live_kit import runtime_host_json
from zerg.qa.live_session_toolkit import new_qualification_isolation_root
from zerg.qa.live_session_toolkit import redact_state_for_evidence
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.model_compliance import attach
from zerg.qa.pi_family_turn_oracle import abort_then_send_verdict
from zerg.qa.pi_family_turn_oracle import control_made_its_observation
from zerg.qa.pi_family_turn_oracle import control_run_assertions
from zerg.qa.pi_family_turn_oracle import fault_name
from zerg.qa.pi_family_turn_oracle import is_aborted_assistant_message
from zerg.qa.pi_family_turn_oracle import negative_control_verdict
from zerg.qa.pi_family_turn_oracle import pi_family_steer_noncompliance
from zerg.qa.pi_family_turn_oracle import provider_holds_prompt
from zerg.qa.pi_family_turn_oracle import read_fault_receipts
from zerg.qa.pi_family_turn_oracle import read_session_entries
from zerg.qa.pi_family_turn_oracle import send_turn_verdict
from zerg.qa.pi_family_turn_oracle import steer_turn_verdict
from zerg.qa.pi_family_turn_oracle import step_task_prompt
from zerg.qa.pi_family_turn_oracle import task_tool_boundary
from zerg.qa.pi_family_turn_oracle import task_tool_call_boundary
from zerg.qa.pi_native import pi_native_shadow_taxonomy
from zerg.qa.pi_native import pi_transcript_rows
from zerg.qa.producer_shell import Run
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.wait_kind import LONGHOUSE_ENGINE
from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import PROVIDER_PROCESS
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import RUNTIME_HOST
from zerg.qa.wait_kind import expired
from zerg.services.provider_interaction_semantics import omp_agent_end_is_terminal

# Hosted ends a run only when it receives a terminal_signal runtime event, and
# that event ships through the machine's outbox rather than straight from the
# launcher. Measured from the local stop, retirement landed ~181s later: a 30s
# deadline gave up on a condition that was still going to become true, and 180s
# missed the same run by two seconds. The latency itself is the outbox drain and
# is tracked separately; this budget just has to exceed it.
RETIREMENT_EVIDENCE_TIMEOUT_SEC = 300.0


@dataclass(frozen=True)
class Family:
    """What differs between the providers of the family; everything else in this module is one engine for both."""

    provider: str  # "omp" or "pi": the launcher's state directory, the served provider, the prefix of the cells
    name: str
    marker_prefix: str
    live_env: str
    # The launcher's name for the provider's own session id. Every state read here calls it ``native_session_id``.
    id_key: str
    launch_argv: Callable[..., list[str]]
    environment: Callable[[Path, argparse.Namespace], dict[str, str]]
    prompts: Mapping[str, str]
    # The cells, from the finished observation; and for a failed run, which records prove a cell's step was reached.
    cells: Callable[[Mapping[str, Any]], dict[str, bool]]
    reached: Mapping[str, tuple[str, ...]]
    # What a served page has to show, beside the answers, for a convergence wait to be over.
    converged: Callable[[Mapping[str, Any]], bool]
    # What the served diagnostic has to show for control to be bound to an owner's lease.
    control_bound: Callable[..., bool]
    is_idle: Callable[[Mapping[str, Any]], bool]
    timeouts: Mapping[str, float]
    # Inside the run's isolation: the provider's HOME, and the Longhouse home the launcher and Machine Agent share.
    homes: tuple[str, str]
    prepare: Callable[[Any], None] = lambda _lifecycle: None

    # The provider itself.
    channel_terminal: bool = False  # the extension reports the end of a turn on the channel (agent_end)
    # The step whose turn calls a tool and has finished it: the session's tool calls and results are read once that
    # step is over ("launch" where the first prompt asks for a tool, "follow_up" where the busy turn runs one).
    tool_turn: str = "launch"

    # How the lifecycle runs today: coverage one provider has and the other lacks.
    # Send, steer, abort and terminate go through the Runtime Host. Where this is set they are proven once more
    # through the engine's local command, in steps of their own (``local=True``) and with the last terminate.
    # Dropping that path is deleting this flag and those steps.
    local_controls: bool = False
    terminate_proves_owners_dead: bool = False  # a terminate is over when the two recorded processes stopped, not when the state says so
    rebind_command: str = "send"  # how the first input after `/new` is sent: as an input, or as "send_live"
    binds_resume_prompt: bool = False  # the resume prompt is found as one row of the restored session
    retirement_claims: bool = False  # each run's retirement is claimed and waited for as it ends
    local_health: bool = False  # the engine's local health is snapshotted after launch

    # Which unmet conditions end the run (a failure of the run) and which are recorded for a cell to judge.
    settles_between_turns: bool = False  # use provider-correlated turn boundaries before another scripted input
    records_misses: bool = False  # an unseen busy state, an unanswered stale frame: recorded, not raised
    flush_must_complete: bool = False  # a flush that did not ship cleanly ends the run where it happens
    refused_command_ends_run: bool = False  # a host input or a final terminate that is not accepted ends the run
    strict_resume_state: bool = False  # the resumed owner must name the same session and file before the turn is read
    evidence_must_be_final: bool = False  # the result says whether the evidence tree was still changing; the provider's module judges it
    control_outlives_failure: bool = False  # a negative control's verdict is reported even when the run failed


SCRIPTED_CONTEXT_PHRASE = "sable-harbor-729"


def evidence_class(args: argparse.Namespace) -> str:
    selected = getattr(args, "evidence_class", None) or "live_token"
    selected = getattr(selected, "value", selected)
    if selected not in ("live_token", "scripted_model"):
        raise ValueError(f"unsupported Helm evidence class: {selected!r}")
    return str(selected)


def _scripted_model_fixture(family: Family, proof_file: Path | None) -> tuple[list[dict[str, Any]], frozenset[str]]:
    """One deterministic fixture for the complete provider-specific Helm lifecycle."""

    prefix = family.marker_prefix
    provider = family.provider
    turns: list[dict[str, Any]] = []
    expected: set[str] = set()

    def add(
        name: str,
        match: str,
        *,
        reply: str | None = None,
        tool_call: dict[str, Any] | None = None,
        reply_from_history: dict[str, Any] | None = None,
        expected_request: bool = True,
    ) -> None:
        turn: dict[str, Any] = {"id": f"{provider}-{name}", "match": match}
        if reply is not None:
            turn["reply"] = reply
        if tool_call is not None:
            turn["tool_call"] = tool_call
        if reply_from_history is not None:
            turn["reply_from_history"] = reply_from_history
        turns.append(turn)
        if expected_request:
            expected.add(turn["id"])

    if proof_file is None:
        add("launch", f"{prefix}_LAUNCH_", reply="{{input_marker}}")
    else:
        add(
            "launch-tool",
            "Use the read tool to read",
            tool_call={"name": "read", "arguments": {"path": str(proof_file)}},
        )
        add("launch-answer", proof_file.read_text(encoding="utf-8").strip(), reply="{{input_marker}}")

    if provider == "omp":
        # OMP's TUI makes a separate title request. It is not a Helm turn, but
        # the scripted endpoint must answer it rather than echoing its prompt.
        add("title", "<title-request>", reply="<title/>", expected_request=False)

    if provider == "pi":
        add("local-send", f"{prefix}_LOCAL_SEND_", reply="{{input_marker}}")
    add(
        "send",
        f"{prefix}_SEND_",
        reply="{{input_marker}}",
    )
    add(
        "busy-tool",
        f"{prefix}_ACTIVE_",
        tool_call={"name": "bash", "command": "sleep 8 && echo scripted-helm-busy-result"},
    )
    add("busy-answer", "scripted-helm-busy-result", reply="{{input_marker}}")
    add("follow-up", f"{prefix}_FOLLOW_UP_", reply="{{input_marker}}")

    for local in (False, True) if provider == "pi" else (False,):
        label = "local-steer" if local else "steer"
        owner_prefix = f"{prefix}_LOCAL_" if local else f"{prefix}_"
        add(
            f"{label}-tool",
            f"Task {owner_prefix}STEER_TASK_",
            tool_call={"name": "bash", "command": f"sleep 30 && echo scripted-{label}-result"},
        )
        add(f"{label}-answer", f"{prefix}_{'LOCAL_' if local else ''}STEER_", reply="{{input_marker}}")
        add(
            f"{label}-settled",
            f"scripted-{label}-result",
            reply=f"scripted-{label}-settled",
            expected_request=False,
        )

    for local in (False, True) if provider == "pi" else (False,):
        label = "local-abort" if local else "abort"
        owner_prefix = f"{prefix}_LOCAL_" if local else f"{prefix}_"
        add(
            f"{label}-tool",
            f"Task {owner_prefix}ABORT_TASK_",
            tool_call={"name": "bash", "command": f"sleep 300 && echo scripted-{label}-result"},
        )
        add(f"{label}-after", f"{prefix}_{'LOCAL_' if local else ''}ABORT_AFTER_", reply="{{input_marker}}")

    if provider == "pi":
        add("reload", f"{prefix}_RELOAD_", reply="{{input_marker}}")

    add(
        "replacement",
        "Remember this context phrase for later:",
        reply=f"{SCRIPTED_CONTEXT_PHRASE} {{{{input_marker}}}}",
    )
    add(
        "stale-owner-refused",
        "A stale owner must be refused:",
        reply="unexpected-stale-owner-model-call",
        expected_request=False,
    )
    add(
        "cold-resume",
        "It is in the conversation history above.",
        reply="{{history}} {{input_marker}}",
        reply_from_history={"pattern": SCRIPTED_CONTEXT_PHRASE},
    )
    return turns, frozenset(expected)


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class RuntimeConvergenceTimeout(RuntimeError):
    """The Runtime Host never served the reply inside the deadline.

    ``observation`` is the last complete read, so a reader sees what was served
    rather than a bare ``None``.
    """

    def __init__(self, message: str, *, observation: dict[str, Any]) -> None:
        self.observation = observation
        super().__init__(message)


# A wait here is over when its reading is anything at all: a falsy reading is still one.
_wait = functools.partial(poll, until=lambda value: value is not None)


_RUNTIME_HOST_RETRY_STATUSES = frozenset({404, 429, 500, 502, 503, 504})

_RUNTIME_EVENTS_PAGE_LIMIT = 500
_RUNTIME_EVENTS_MAX_PAGES = 32


def _runtime_get(api_url: str, token: str, path: str) -> dict[str, Any]:
    """One read of the agents API. A refusal raises `RuntimeHostError`, which a wait outlasts when it is transient."""

    return runtime_host_json(api_url, token, path.removeprefix("/api/agents/"))


def _runtime_events_snapshot(api_url: str, token: str, session_id: str) -> dict[str, Any]:
    """The session's served events: every page, under one generation."""

    events: list[dict[str, Any]] = []
    first_page: dict[str, Any] | None = None
    generation_id: str | None = None
    branch_mode: str | None = None
    cursor: str | None = None

    for pages_read in range(1, _RUNTIME_EVENTS_MAX_PAGES + 1):
        query = f"/api/agents/sessions/{session_id}/events?anchor=start&branch_mode=head&limit={_RUNTIME_EVENTS_PAGE_LIMIT}"
        if cursor is not None:
            query += f"&cursor={quote(cursor, safe='')}"
        page = _runtime_get(api_url, token, query)
        if first_page is None:
            first_page = dict(page)
        page_session_id = page.get("session_id")
        if page_session_id is not None and page_session_id != session_id:
            raise RuntimeError("Runtime Host returned events for the wrong session")
        page_generation = page.get("generation_id")
        if not isinstance(page_generation, str) or not page_generation:
            raise RuntimeError("Runtime Host returned events without a generation identity")
        if generation_id is None:
            generation_id = page_generation
            branch_mode = page.get("branch_mode")
        elif page_generation != generation_id or page.get("branch_mode") != branch_mode:
            raise RuntimeError("Runtime Host changed the events generation during pagination")
        raw_events = page.get("events")
        if not isinstance(raw_events, list) or any(not isinstance(event, Mapping) for event in raw_events):
            raise RuntimeError("Runtime Host returned an invalid events page")
        events.extend(dict(event) for event in raw_events)
        has_more = page.get("has_more")
        next_cursor = page.get("next_cursor")
        if has_more is False and next_cursor is None:
            result = dict(first_page or {})
            result.update(
                {
                    "events": events,
                    "generation_id": generation_id,
                    "branch_mode": branch_mode,
                    "has_more": False,
                    "next_cursor": None,
                    "pagination": {
                        "pages_read": pages_read,
                        "exhausted": True,
                        "generation_id": generation_id,
                    },
                }
            )
            return result
        if has_more is not True or not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError("Runtime Host returned a non-exhaustible events page")
        cursor = next_cursor
    raise RuntimeError(f"Runtime Host events pagination exceeded {_RUNTIME_EVENTS_MAX_PAGES} pages")


def _runtime_snapshot(api_url: str, token: str, session_id: str) -> dict[str, Any]:
    """The canonical served surfaces of a session, not a synthetic turn route."""

    return {
        "detail": _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}"),
        "thread": _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}/thread"),
        "events": _runtime_events_snapshot(api_url, token, session_id),
        "diagnostic": _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}/state-diagnostics"),
    }


_SERVED_TURN_KEYS = frozenset({"thinking", "executing"})


def _wait_served_turn(api_url: str, token: str, session_id: str, *, timeout: float = 20.0, interval: float = 0.1) -> dict[str, Any]:
    """Wait until the Runtime Host serves the session as thinking or executing, the states a steer is accepted in.

    The host refuses a steer until its own served state shows a running turn
    (409 ``steer_requires_active_turn``), and that state trails the Machine
    Agent's local phase by the time it takes the phase to be posted and
    reduced (about half a second, measured on the bench). A scripted model
    reaches its first tool call inside that gap, so a steer sent the moment the
    tool call is in the session file lands before the host has seen the turn.
    The wait is on the served state the host will judge the steer by, never a
    delay. It records what it saw and returns at its deadline rather than
    raising: a turn the host never serves as running is the steer's own 409.
    """

    deadline = time.monotonic() + timeout
    started = time.monotonic()
    key: str | None = None
    while True:
        try:
            diagnostic = _runtime_get(api_url, token, f"/api/agents/sessions/{session_id}/state-diagnostics")
            key = ((diagnostic.get("explain") or {}).get("presentation_keys") or {}).get("primary")
        except RuntimeError as exc:
            if not _is_transient_runtime_read_error(exc):
                raise
        if key in _SERVED_TURN_KEYS or time.monotonic() >= deadline:
            return {
                "served_primary_key": key,
                "served_as_running": key in _SERVED_TURN_KEYS,
                "waited_s": round(time.monotonic() - started, 3),
            }
        time.sleep(interval)


def _nested_values(value: object, keys: set[str]) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child is not None and str(child).strip():
                values.append(str(child))
            values.extend(_nested_values(child, keys))
    elif isinstance(value, list):
        for child in value:
            values.extend(_nested_values(child, keys))
    return values


def _runtime_binding(snapshot: Mapping[str, Any], *, session_id: str, native_session_id: str) -> dict[str, Any]:
    """Whether what is served is one session and one thread, bound to the provider's own session."""

    detail = snapshot.get("detail") if isinstance(snapshot.get("detail"), dict) else {}
    thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), dict) else {}
    events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), dict) else {}
    events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
    served_session_id = str(detail.get("session_id") or detail.get("id") or "")
    # This machine endpoint exposes the canonical continuation chain, not the
    # internal kernel thread UUID. Keep those identities distinct.
    root_session_id = str(thread.get("root_session_id") or "")
    head_session_id = str(thread.get("head_session_id") or "")
    members = thread.get("sessions") if isinstance(thread.get("sessions"), list) else []
    member_ids = {str(member.get("id")) for member in members if isinstance(member, dict)}
    event_session_ids = {str(event.get("session_id")) for event in events if isinstance(event, dict) and event.get("session_id")}
    provider_ids = set(
        _nested_values({"detail": detail, "thread": thread, "events": events}, {"provider_session_id", "provider_thread_id", "alias_value"})
    )
    return {
        "session_id": served_session_id,
        "thread_root_session_id": root_session_id,
        "thread_head_session_id": head_session_id,
        "provider": detail.get("provider"),
        "provider_session_id": native_session_id,
        "provider_session_bound": native_session_id in provider_ids,
        "one_session": served_session_id == session_id and (not event_session_ids or event_session_ids == {session_id}),
        "one_thread": root_session_id == head_session_id == session_id and member_ids == {session_id},
        "event_count": len(events),
    }


def _served_controls(diagnostic: Mapping[str, Any]) -> bool:
    """Whether the served session offers a send and names a terminate."""

    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), dict) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), dict) else {}
    return (
        diagnostic.get("served_path") == "canonical_session_detail"
        and isinstance(actions.get("send_input"), dict)
        and actions["send_input"].get("state") == "available"
        and isinstance(actions.get("terminate"), dict)
    )


def _flush_receipt_complete(receipt: Mapping[str, Any]) -> bool:
    events_shipped = receipt.get("events_shipped")
    return (
        receipt.get("status") == "pass"
        and receipt.get("exit_code") == 0
        and receipt.get("daemon_paused") is True
        and receipt.get("daemon_restarted") is True
        and isinstance(events_shipped, int)
        and not isinstance(events_shipped, bool)
        and events_shipped >= 0
    )


def _events_page_metadata(payload: Mapping[str, Any], *, requested_limit: int = _RUNTIME_EVENTS_PAGE_LIMIT) -> dict[str, Any]:
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    has_more = payload.get("has_more")
    next_cursor = payload.get("next_cursor")
    total = payload.get("total")
    generation_id = payload.get("generation_id")
    pagination = payload.get("pagination") if isinstance(payload.get("pagination"), Mapping) else {}
    pages_read = pagination.get("pages_read", 1)
    exhausted = pagination.get("exhausted") is True or (has_more is False and next_cursor is None)
    total_valid = isinstance(total, int) and not isinstance(total, bool) and total >= len(events)
    return {
        "requested_anchor": "start",
        "requested_branch_mode": "head",
        "requested_limit": requested_limit,
        "requested_cursor": None,
        "returned_count": len(events),
        "total": total,
        "generation_id": generation_id,
        "branch_mode": payload.get("branch_mode"),
        "abandoned_events": payload.get("abandoned_events"),
        "next_cursor": next_cursor,
        "has_more": has_more,
        "pages_read": pages_read,
        "pagination_exhausted": exhausted,
        "complete": (
            payload.get("branch_mode") == "head" and total_valid and isinstance(generation_id, str) and bool(generation_id) and exhausted
        ),
    }


def _stable_event_id(value: object) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool) and bool(str(value))


def _served_projection_evidence(
    snapshot: Mapping[str, Any],
    *,
    session_id: str,
    native_session_id: str,
    marker: str,
    alongside: tuple[str, ...] = (),
    recalled: bool = False,
    requested_limit: int = _RUNTIME_EVENTS_PAGE_LIMIT,
) -> dict[str, Any]:
    """Keep only structural fields from the served Runtime Host responses."""

    detail = snapshot.get("detail") if isinstance(snapshot.get("detail"), Mapping) else {}
    thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), Mapping) else {}
    events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), Mapping) else {}
    events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
    events_page = _events_page_metadata(events_payload, requested_limit=requested_limit)
    members = thread.get("sessions") if isinstance(thread.get("sessions"), list) else []
    marker_matches = assistant_marker_events(events, marker, default_origin="durable", alongside=alongside, recalled=recalled)
    marker_mentions = assistant_marker_mentions(events, marker, default_origin="durable", alongside=alongside, recalled=recalled)
    marker_event_ids = [event.get("id") for event in marker_matches]
    # The one id when the turn was answered once; a turn answered more than
    # once has no single id and is read from `marker_event_ids`.
    marker_event_id = marker_event_ids[0] if len(marker_event_ids) == 1 else None
    diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), Mapping) else {}
    return {
        "requested_session_id": session_id,
        "detail": {
            "id": detail.get("id"),
            "provider": detail.get("provider"),
            "provider_session_id": detail.get("provider_session_id"),
        },
        "thread": {
            "root_session_id": thread.get("root_session_id"),
            "head_session_id": thread.get("head_session_id"),
            "session_ids": [member.get("id") for member in members if isinstance(member, Mapping) and member.get("id") is not None],
        },
        "events": [
            {
                "id": event.get("id"),
                "role": event.get("role"),
                "event_origin": event.get("event_origin", "durable"),
                "tool_name_present": bool(event.get("tool_name")),
                "marker_occurrences": event_text(event).count(marker),
            }
            for event in events
            if isinstance(event, Mapping)
        ],
        # Retain the marker-bearing served records themselves. Counts and
        # identities are useful indexes, not independent proof of content.
        # `marker_events` answered with the marker; `marker_mention_events`
        # name it without answering (narration ahead of a tool call) and are
        # never counted as replies.
        "marker_events": [dict(event) for event in marker_matches],
        "marker_mention_events": [dict(event) for event in marker_mentions],
        "marker_event_id": marker_event_id,
        "marker_event_ids": marker_event_ids,
        "marker_event_durable": bool(marker_matches) and all(event.get("event_origin", "durable") == "durable" for event in marker_matches),
        "marker_event_identity_bound": bool(marker_event_ids) and all(_stable_event_id(event_id) for event_id in marker_event_ids),
        "events_page": events_page,
        "diagnostic": {
            "session_id": diagnostic.get("session_id"),
            "served_path": diagnostic.get("served_path"),
        },
    }


def _runtime_convergence(
    api_url: str,
    token: str,
    *,
    family: Family,
    session_id: str,
    native_session_id: str,
    marker: str,
    flush: Mapping[str, Any],
    native_source_path: str,
    alongside: tuple[str, ...] = (),
    recalled: bool = False,
    native_reply_id: object = None,
    timeout: float = 90,
) -> dict[str, Any]:
    """Wait until the Runtime Host serves what the provider answered ``marker`` with.

    The served events that answer must be the rows of the provider's own
    transcript (``native_source_path``) that answer, row for row
    (`served_answer_correspondence`). How many there are is the provider's
    business: OMP ends a reply while a background job runs and wakes the agent
    when the job finishes, and the agent may answer again. ``alongside`` names
    the other markers the reply was asked to carry; ``recalled`` is for a
    phrase the model brings back from restored history, which it may put in a
    sentence. ``native_reply_id`` is the row the native wait returned; it
    decides nothing about convergence, which is judged against every answering
    row of the transcript. It is recorded beside the observation
    (``native_reply_served``), and having it is what lets an expired wait say
    it was waiting on Longhouse and not on the provider's turn. A served page
    read to its end with an answer the transcript does not account for fails
    at once with a typed reason (`served_answer_fault`); a page that has not
    reached an answering row yet, or has only its first block, is the lag this
    waits out. A wait that gives up with a fault still on the page ends with
    that fault's reason, not as a timeout.

    What else has to be served before the wait is over is the family's
    (``Family.converged``): OMP holds the served identity and the durable,
    identified events; Pi holds one session, one thread and the served controls.
    """

    last: dict[str, Any] = {}
    refusal = StandingServedAnswerFault()
    record = {
        "provider": family.provider,
        "session_id": session_id,
        "native_session_id": native_session_id,
        "marker": marker,
        "flush": dict(flush),
        "native_source_path": native_source_path,
    }

    def observe() -> dict[str, Any] | None:
        try:
            snapshot = _runtime_snapshot(api_url, token, session_id)
        except RuntimeHostError as exc:
            if exc.status in _RUNTIME_HOST_RETRY_STATUSES:
                last["runtime_read_error"] = str(exc)
                return None
            exc.last_runtime_observation = dict(last) or None
            raise
        events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), Mapping) else {}
        events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
        matches = assistant_marker_events(events, marker, default_origin="durable", alongside=alongside, recalled=recalled)
        served_projection = _served_projection_evidence(
            snapshot,
            session_id=session_id,
            native_session_id=native_session_id,
            marker=marker,
            alongside=alongside,
            recalled=recalled,
        )
        events_page = served_projection["events_page"]
        if events_page.get("complete") is not True:
            return {
                **record,
                "status": "unproven",
                "reason": "runtime_events_page_incomplete",
                "events_page": dict(events_page),
                "served_projection": served_projection,
            }
        # Read after the page, so nothing served is newer than what is read here.
        native_answers, native_state = lifecycle._native_answer_rows(
            family.provider, Path(native_source_path), marker, alongside=alongside, recalled=recalled
        )
        native_readable = native_answers is not None
        native_answers = native_answers or {}
        # Without the transcript read to the end there is nothing to hold the
        # page to: no verdict either way. And a refusal has to stand
        # (`StandingServedAnswerFault`), so a row the provider appended between
        # the engine shipping it and this read is seen on the next one.
        found = (
            served_answer_fault(events, marker, native_answers, default_origin="durable", alongside=alongside, recalled=recalled)
            if native_readable
            else None
        )
        correspondence = served_answer_correspondence(
            events, marker, native_answers, default_origin="durable", alongside=alongside, recalled=recalled
        )
        correspondence["native_transcript_readable"] = native_readable
        correspondence["native_transcript_state"] = native_state
        binding = _runtime_binding(snapshot, session_id=session_id, native_session_id=native_session_id)
        diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), Mapping) else {}
        served = {
            "answer_correspondence": correspondence,
            "served_projection": served_projection,
            "binding": binding,
            "served_controls": _served_controls(diagnostic),
        }
        if found is not None:
            found.observation = {**record, "native_reply_id": native_reply_id, **served}
        fault = refusal.standing(found)
        if fault is not None:
            raise fault
        detail = served_projection.get("detail") if isinstance(served_projection.get("detail"), Mapping) else {}
        thread = served_projection.get("thread") if isinstance(served_projection.get("thread"), Mapping) else {}
        served_events = served_projection.get("events") if isinstance(served_projection.get("events"), list) else []
        served_event_ids = [event.get("id") for event in served_events if isinstance(event, Mapping)]
        # What the deadline reports when this never converges: structure only.
        last.clear()
        last.update(
            {
                "answer_event_count": len(matches),
                "answers_converged": correspondence["converged"] is True,
                "native_transcript_readable": native_readable,
                "native_transcript_state": native_state,
                "native_answer_rows": correspondence["native_answer_rows"],
                "unserved_native_row_ids": correspondence["unserved_native_row_ids"],
                "mention_event_count": len(served_projection["marker_mention_events"]),
                "served_event_count": len(served_event_ids),
                "native_reply_served": native_reply_id is not None and native_reply_id in served_event_ids,
                "exact_served_identity": (
                    detail.get("id") == session_id
                    and detail.get("provider") == family.provider
                    and detail.get("provider_session_id") == native_session_id
                    and thread.get("root_session_id") == session_id
                    and thread.get("head_session_id") == session_id
                    and session_id in (thread.get("session_ids") if isinstance(thread.get("session_ids"), list) else [])
                ),
                "marker_bound_durable_event": (
                    correspondence["converged"] is True
                    and bool(matches)
                    and all(event.get("id") in served_event_ids for event in matches)
                    and served_projection.get("marker_event_durable") is True
                    and served_projection.get("marker_event_identity_bound") is True
                ),
                "served_path": diagnostic.get("served_path"),
                **served,
            }
        )
        if not family.converged(last):
            return None
        return {**record, "status": "pass", **served}

    def timed_out(prefix: str) -> RuntimeError:
        # A fault still on the page is why this never converged: say that.
        fault = refusal.at_deadline()
        if fault is not None:
            return fault
        summary = {key: value for key, value in last.items() if key != "served_projection"}
        return RuntimeConvergenceTimeout(f"{prefix}: {summary}", observation=dict(last))

    def stopped_on() -> str | None:
        # Without the native row, or without the transcript to hold the page
        # to, this wait cannot say which side stopped.
        if native_reply_id is None or last.get("native_transcript_readable") is False:
            return None
        # The last read failed, or the answers are served and what the family
        # holds beside them is not: the Runtime Host. Otherwise the provider's
        # reply has yet to be served, which is ingest.
        return RUNTIME_HOST if "runtime_read_error" in last or last.get("answers_converged") else LONGHOUSE_INGEST

    return _wait(
        observe,
        timeout=timeout,
        description=f"Runtime Host convergence for {family.name} marker {marker}",
        on_timeout=timed_out,
        # The caller found the reply in the provider's own transcript before
        # asking, so the provider's turn is not what is awaited.
        waited_on=stopped_on,
    )


def _served_control_identity(
    diagnostic: dict[str, Any],
    *,
    expected_subject_key: str,
) -> bool:
    """Require the exact managed channel before exercising control."""
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    fact_sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), dict) else {}
    control_source = fact_sources.get("control") if isinstance(fact_sources.get("control"), dict) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), dict) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), dict) else {}
    return (
        diagnostic.get("served_path") == "canonical_session_detail"
        and control_source.get("subject_key") == expected_subject_key
        and isinstance(actions.get("send_input"), dict)
        and actions["send_input"].get("state") == "available"
        and isinstance(actions.get("interrupt"), dict)
        and actions["interrupt"].get("state") == "available"
        and isinstance(actions.get("terminate"), dict)
        and actions["terminate"].get("state") == "available"
    )


def _served_send_identity(diagnostic: dict[str, Any], *, expected_subject_key: str) -> bool:
    """The managed channel, held to a send it offers and a terminate it names (what Pi's lifecycle holds today)."""

    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    fact_sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), dict) else {}
    control_source = fact_sources.get("control") if isinstance(fact_sources.get("control"), dict) else {}
    return control_source.get("subject_key") == expected_subject_key and _served_controls(diagnostic)


def _control_identity_receipt(identity: Mapping[str, Any]) -> dict[str, Any]:
    diagnostic = identity.get("diagnostic") if isinstance(identity.get("diagnostic"), Mapping) else {}
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), Mapping) else {}
    fact_sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), Mapping) else {}
    control_source = fact_sources.get("control") if isinstance(fact_sources.get("control"), Mapping) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), Mapping) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), Mapping) else {}
    local_state = identity.get("local_state")
    owner_identity = list(_state_owner_identity(local_state)) if isinstance(local_state, Mapping) else None
    return {
        "session_id": identity.get("session_id"),
        "expected_subject_key": identity.get("expected_subject_key"),
        "served_path": diagnostic.get("served_path"),
        "shadow_mode": shadow.get("mode"),
        "control_subject_key": control_source.get("subject_key"),
        "owner_identity": owner_identity,
        "actions": {
            name: (value.get("state") if isinstance(value, Mapping) else None)
            for name, value in actions.items()
            if name in {"send_input", "interrupt", "terminate"}
        },
        "transient_errors": list(identity.get("transient_errors") or []),
    }


def _owner_identity_is_complete(owner_identity: Any) -> bool:
    if not isinstance(owner_identity, list) or len(owner_identity) != len(_OWNER_IDENTITY_FIELDS):
        return False
    session_id, native_session_id, session_file, launcher_pid, launcher_start, provider_pid, provider_start = owner_identity
    return (
        all(isinstance(value, str) and value.strip() for value in (session_id, native_session_id, session_file))
        and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in (launcher_pid, provider_pid))
        and all(isinstance(value, str) and value.strip() for value in (launcher_start, provider_start))
    )


def _control_identity_receipt_is_bound(receipt: Any) -> bool:
    if not isinstance(receipt, Mapping):
        return False
    expected_subject_key = receipt.get("expected_subject_key")
    actions = receipt.get("actions")
    owner_identity = receipt.get("owner_identity")
    if not isinstance(expected_subject_key, str) or not expected_subject_key:
        return False
    if (
        receipt.get("session_id") is None
        or receipt.get("control_subject_key") != expected_subject_key
        or not _owner_identity_is_complete(owner_identity)
    ):
        return False
    if receipt.get("served_path") != "canonical_session_detail" or not isinstance(actions, Mapping):
        return False
    return all(isinstance(actions.get(name), str) and actions.get(name) == "available" for name in ("send_input", "interrupt", "terminate"))


def _runtime_control_identity_is_complete(identity: Any) -> bool:
    if not isinstance(identity, Mapping):
        return False
    labels = ("initial", "replacement", "cold_resume", "final")
    receipts = [identity.get(label) for label in labels]
    if not all(_control_identity_receipt_is_bound(receipt) for receipt in receipts):
        return False
    session_ids = {str(receipt["session_id"]) for receipt in receipts if isinstance(receipt, Mapping)}
    subject_keys = [str(receipt["expected_subject_key"]) for receipt in receipts if isinstance(receipt, Mapping)]
    owner_identities = [
        tuple(receipt["owner_identity"])
        for receipt in receipts
        if isinstance(receipt, Mapping) and isinstance(receipt.get("owner_identity"), list)
    ]
    return (
        len(session_ids) == 1
        and len(subject_keys) == len(labels)
        and len(set(subject_keys[:3])) == 3
        and len(owner_identities) == len(labels)
        and owner_identities[2] == owner_identities[3]
    )


_OWNER_IDENTITY_FIELDS = (
    "session_id",
    "native_session_id",
    "session_file",
    "launcher_pid",
    "launcher_process_start_time",
    "provider_pid",
    "provider_process_start_time",
)


def _state_owner_identity(state: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the execution-owner fields that must not change during a wait."""

    return tuple(state.get(field) for field in _OWNER_IDENTITY_FIELDS)


def _is_transient_runtime_read_error(error: BaseException) -> bool:
    return isinstance(error, RuntimeHostError) and error.status in _RUNTIME_HOST_RETRY_STATUSES


def _states(home: Path, family: Family, session_id: str | None = None) -> list[dict[str, Any]]:
    """The provider's launcher state files, each naming the provider's own session id ``native_session_id``."""

    states = []
    for path in sorted((home / "managed-local" / f"{family.provider}-helm").glob("*.json")):
        state = _read_state(path)
        if state and (session_id is None or state.get("session_id") == session_id):
            state.setdefault("native_session_id", state.get(family.id_key))
            states.append(state)
    return states


def _current_ready_state(home: Path, family: Family, session_id: str) -> dict[str, Any] | None:
    candidates = [
        state
        for state in _states(home, family, session_id)
        if state.get("ready") is True and state.get("connection_id") and state.get("lease_generation")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda value: str(value.get("updated_at") or ""))


def _control_observation_diagnostic(diagnostic: Mapping[str, Any]) -> dict[str, Any]:
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), Mapping) else {}
    sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), Mapping) else {}
    control_source = sources.get("control") if isinstance(sources.get("control"), Mapping) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), Mapping) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), Mapping) else {}
    return {
        "served_path": diagnostic.get("served_path"),
        "control_subject_key": control_source.get("subject_key"),
        "connection": control.get("connection"),
        "ownership": control.get("ownership"),
        "actions": {
            name: value.get("state") if isinstance(value, Mapping) else None
            for name, value in actions.items()
            if name in {"send_input", "interrupt", "terminate"}
        },
    }


# Why a pass of the control-identity wait returned nothing, and what that pass
# was waiting on. Without the engine's own state file the engine is awaited;
# a request the Runtime Host failed is the Runtime Host. A lease that rotated
# during the read, and a diagnostic served without the identity, do not say
# which side has yet to act (the engine registering the lease, or the Runtime
# Host binding it), so they declare nothing.
_CONTROL_IDENTITY_WAITED_ON = {
    "local_state_unavailable": LONGHOUSE_ENGINE,
    "local_state_unavailable_during_read": LONGHOUSE_ENGINE,
    "transient_runtime_read": RUNTIME_HOST,
    "lease_rotated_during_read": None,
    "projection_not_bound": None,
}


def _wait_runtime_control_identity(
    url: str,
    token: str,
    *,
    family: Family,
    home: Path | None = None,
    session_id: str,
    state: dict[str, Any],
    timeout: float = 90.0,
    bound: Callable[..., bool] = _served_control_identity,
) -> dict[str, Any]:
    """Wait until the Runtime Host serves control of the session under the owner's current lease.

    With ``home`` the owner is re-read around each request, so a lease that
    rotates or an owner that changes during the wait is seen; without it the
    given ``state`` is the owner. ``bound`` is what the served diagnostic has
    to show for that lease.
    """

    baseline_owner = _state_owner_identity(state)
    transient_errors: list[str] = []
    last_observation: dict[str, Any] = {}

    def observe() -> dict[str, Any] | None:
        local_before = _current_ready_state(home, family, session_id) if home is not None else state
        if local_before is None:
            last_observation.clear()
            last_observation.update({"reason": "local_state_unavailable"})
            return None
        if _state_owner_identity(local_before) != baseline_owner:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "execution_owner_changed",
                    "local_state": _redacted_state_snapshot(local_before),
                    "baseline_state": _redacted_state_snapshot(state),
                }
            )
            raise RuntimeError(f"{family.name} Helm execution owner changed while waiting for runtime control identity")
        connection_id = str(local_before.get("connection_id") or "").strip()
        lease_generation = str(local_before.get("lease_generation") or "").strip()
        expected_subject_key = f"connection:{connection_id}:{lease_generation}"
        try:
            diagnostic = _runtime_get(
                url,
                token,
                f"/api/agents/sessions/{session_id}/state-diagnostics",
            )
        except RuntimeError as exc:
            if not _is_transient_runtime_read_error(exc):
                raise
            transient_errors.append(str(exc))
            del transient_errors[:-8]
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "transient_runtime_read",
                    "expected_subject_key": expected_subject_key,
                    "error": str(exc),
                }
            )
            return None
        local_after = _current_ready_state(home, family, session_id) if home is not None else local_before
        if local_after is None:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "local_state_unavailable_during_read",
                    "expected_subject_key": expected_subject_key,
                    "local_before": _redacted_state_snapshot(local_before),
                }
            )
            return None
        if _state_owner_identity(local_after) != baseline_owner:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "execution_owner_changed_during_read",
                    "expected_subject_key": expected_subject_key,
                    "local_before": _redacted_state_snapshot(local_before),
                    "local_after": _redacted_state_snapshot(local_after),
                }
            )
            raise RuntimeError(f"{family.name} Helm execution owner changed during runtime control identity read")
        after_connection_id = str(local_after.get("connection_id") or "").strip()
        after_lease_generation = str(local_after.get("lease_generation") or "").strip()
        after_subject_key = f"connection:{after_connection_id}:{after_lease_generation}"
        if after_subject_key != expected_subject_key:
            last_observation.clear()
            last_observation.update(
                {
                    "reason": "lease_rotated_during_read",
                    "expected_subject_key": expected_subject_key,
                    "current_subject_key": after_subject_key,
                }
            )
            return None
        last_observation.clear()
        last_observation.update(
            {
                "reason": "projection_not_bound",
                "expected_subject_key": expected_subject_key,
                "local_state": _redacted_state_snapshot(local_after),
                "runtime": _control_observation_diagnostic(diagnostic),
            }
        )
        if not bound(diagnostic, expected_subject_key=expected_subject_key):
            return None
        return {
            "session_id": session_id,
            "expected_subject_key": expected_subject_key,
            "diagnostic": diagnostic,
            "local_state": _redacted_state_snapshot(local_after),
            "transient_errors": list(transient_errors),
        }

    try:
        return _wait(
            observe,
            timeout=timeout,
            description=f"Runtime Host managed control identity for {session_id}",
            waited_on=lambda: _CONTROL_IDENTITY_WAITED_ON.get(str(last_observation.get("reason") or "")),
        )
    except RuntimeError as exc:
        evidence = json.dumps(last_observation, sort_keys=True, separators=(",", ":"))
        raise RuntimeError(f"{exc}; last_observation={evidence}") from exc


def _wait_state(
    home: Path,
    family: Family,
    *,
    session_id: str | None = None,
    timeout: float = 30,
    predicate: Any = None,
) -> dict[str, Any]:
    """Wait for a ready launcher state, of ``session_id`` when given, that ``predicate`` holds for."""

    last_state: dict[str, Any] | None = None
    last_predicate_satisfied: bool | None = None

    def observe() -> dict[str, Any] | None:
        nonlocal last_state, last_predicate_satisfied
        for state in _states(home, family, session_id):
            last_state = dict(state)
            if state.get("ready") is not True or (predicate is not None and not predicate(state)):
                last_predicate_satisfied = False
                continue
            last_predicate_satisfied = True
            return state
        return None

    try:
        return _wait(observe, timeout=timeout, description=f"{family.name} Helm state")
    except RuntimeError as exc:
        diagnostic = {
            "last_state": _redacted_state_snapshot(last_state) if last_state else None,
            "predicate_satisfied": last_predicate_satisfied,
        }
        raise RuntimeError(f"{exc}; diagnostic={json.dumps(diagnostic, sort_keys=True, separators=(',', ':'))}") from exc


def _resume_state_is_settled(
    state: Mapping[str, Any],
    *,
    native_session_id: str,
    session_file: Path,
) -> bool:
    return (
        state.get("ready") is True
        and state.get("phase") == "idle"
        and state.get("agent_end_observed") is True
        and state.get("agent_end_is_terminal") is True
        and state.get("native_session_id") == native_session_id
        and state.get("session_file") == str(session_file)
    )


def _native_rows(session_file: Path, minimum_offset: int = 0) -> list[dict[str, Any]]:
    try:
        content = session_file.read_bytes()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    for raw in content.splitlines(keepends=True):
        end = offset + len(raw)
        if end <= minimum_offset:
            offset = end
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            offset = end
            continue
        if isinstance(value, dict):
            value = dict(value)
            value["_source_offset"] = offset
            rows.append(value)
        offset = end
    return rows


def _wait_native_marker(
    session_file: Path,
    marker: str,
    *,
    minimum_offset: int = 0,
    role: str = "assistant",
    alongside: tuple[str, ...] = (),
    recalled: bool = False,
    timeout: float = 90,
) -> dict[str, Any]:
    """Wait for the native row that carries ``marker``.

    An assistant row is the reply only when it answers with the marker
    (``zerg.qa.answer_markers``). A turn that works through tools narrates the
    marker first ("both jobs must finish before I may reply with ..."), and
    returning that row ended the wait before the turn did and bound the
    evidence to the wrong message. A user row is the prompt, which names the
    marker inside a sentence, so it is matched by presence; so is a phrase the
    model brings back from restored history (``recalled``), which no prompt of
    the turn contains and the model may put in a sentence.

    The wait cannot end early on a turn that stopped without answering: OMP
    ends a reply while a background job runs and is woken when it finishes, so
    a ``stop`` row does not prove the model is done. The timeout says what the
    transcript held instead.
    """

    last: dict[str, Any] = {}

    def observe() -> dict[str, Any] | None:
        candidates = 0
        mentions = 0
        stop_reason: object = None
        for row in _native_rows(session_file, minimum_offset):
            if row.get("type") != "message":
                continue
            message = row.get("message")
            if not isinstance(message, Mapping) or message.get("role") != role:
                continue
            text = lifecycle._native_message_text(message)
            if role != "assistant" or recalled:
                if marker in text:
                    return row
                continue
            if marker_answered(text, marker, alongside=alongside):
                return row
            candidates += 1
            mentions += marker in text
            stop_reason = message.get("stopReason")
        last.update(
            {
                f"{role}_rows": candidates,
                "rows_mentioning_marker": mentions,
                "last_stop_reason": stop_reason,
            }
        )
        return None

    return _wait(
        observe,
        timeout=timeout,
        description=f"native marker {marker}",
        on_timeout=lambda prefix: RuntimeError(f"{prefix}: {last or None}"),
        # An assistant row is the model's own reply in OMP's own transcript,
        # and the wait was on the model only if OMP holds the prompt it would
        # answer: a prompt Longhouse never delivered leaves the same silence.
        # A user row is the prompt arriving there, and a recalled phrase is
        # read from a turn that has already replied; neither is a stalled turn.
        waited_on=(
            (lambda: PROVIDER_TURN if provider_holds_prompt(session_file, marker) else None)
            if role == "assistant" and not recalled
            else None
        ),
    )


def _native_file_identity(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        return {"path": str(path), "present": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"path": str(path.resolve()), "present": True, "device": stat.st_dev, "inode": stat.st_ino, "size_bytes": stat.st_size}


def _native_marker_evidence(
    row: Mapping[str, Any],
    session_file: Path,
    *,
    marker: str,
    minimum_offset: int,
    native_session_id: str,
    role: str = "assistant",
    alongside: tuple[str, ...] = (),
    recalled: bool = False,
) -> dict[str, Any]:
    """What the provider's own transcript holds of ``marker`` from ``minimum_offset`` on, and the row a wait returned for it.

    One reading for both providers. The row's own facts bind the reply to a
    place in the file; the counts say how often the window names the marker
    and in which rows: a prompt delivered twice is two user rows, and a model
    that answered twice is two answering rows.
    """

    message = row.get("message")
    text = lifecycle._native_message_text(message) if isinstance(message, Mapping) else ""
    offset = row.get("_source_offset")
    window = [
        item
        for item in _native_rows(session_file, minimum_offset)
        if item.get("type") == "message" and isinstance(item.get("message"), Mapping)
    ]
    texts = [(item["message"].get("role"), lifecycle._native_message_text(item["message"])) for item in window]
    naming = [body for speaker, body in texts if speaker == "assistant" and marker in body]
    prompts = [body for speaker, body in texts if speaker == "user" and marker in body]
    headers = [item.get("id") for item in _native_rows(session_file) if item.get("type") == "session"]
    models = [
        item["message"].get("model") for item in window if item["message"].get("role") == "assistant" and item["message"].get("model")
    ]
    try:
        source_end_offset = session_file.stat().st_size
    except OSError:
        source_end_offset = None
    return {
        "native_source_bound": (
            session_file.is_file()
            and row.get("type") == "message"
            and isinstance(message, Mapping)
            and message.get("role") == role
            and isinstance(row.get("id"), str)
            and isinstance(offset, int)
            and offset >= minimum_offset
            and bool(native_session_id)
        ),
        "source_path": str(session_file),
        "source_offset": offset,
        "minimum_source_offset": minimum_offset,
        "source_end_offset": source_end_offset,
        "native_session_id": native_session_id,
        "native_header_session_id": headers[0] if len(headers) == 1 else None,
        "native_file": _native_file_identity(session_file),
        "native_model": models[-1] if models else None,
        "message_role": role,
        "event_id": row.get("id"),
        "marker": marker,
        # Occurrences inside the one row the wait returned: the reply must say
        # the marker once. Which row is the reply is decided by the wait.
        "marker_count": text.count(marker),
        "window_row_count": len(window),
        "assistant_marker_rows": len(naming),
        "assistant_answer_rows": len(naming) if recalled else sum(marker_answered(body, marker, alongside=alongside) for body in naming),
        "user_marker_rows": len(prompts),
        "user_marker_occurrences": sum(body.count(marker) for body in prompts),
    }


def _wait_native_abort(session_file: Path, *, minimum_offset: int, timeout: float = 30) -> dict[str, Any]:
    """Wait for the row in which the provider records a turn it aborted."""

    def observe() -> dict[str, Any] | None:
        for row in _native_rows(session_file, minimum_offset):
            if is_aborted_assistant_message(row):
                return row
        return None

    return _wait(observe, timeout=timeout, description="native aborted message")


def _channel_terminal_evidence(
    event: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    native_session_id: str,
    session_file: Path,
) -> dict[str, Any]:
    terminal = event.get("type") == "agent_end" and omp_agent_end_is_terminal(event)
    evidence = {
        "channel_source_bound": (
            terminal
            and event.get("source") == "omp_helm_extension_channel"
            and state.get("status") == "ready"
            and state.get("native_session_id") == native_session_id
            and state.get("session_file") == str(session_file)
            and bool(state.get("connection_id"))
            and bool(state.get("lease_generation"))
        ),
        "source": "omp_helm_extension_channel",
        "event_type": event.get("type"),
        "native_session_id": native_session_id,
        "session_file": str(session_file),
        "connection_id": state.get("connection_id"),
        "lease_generation": state.get("lease_generation"),
        "terminal": terminal,
    }
    if "isTerminal" in event:
        evidence["is_terminal"] = event["isTerminal"]
    if "willContinue" in event:
        evidence["will_continue"] = event["willContinue"]
    return evidence


def _channel_binding_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    session_file = Path(str(state.get("session_file") or ""))
    return {
        "ready": state.get("ready") is True,
        "session_id_present": bool(state.get("session_id")),
        "native_session_id_present": bool(state.get("native_session_id")),
        "connection_id_present": bool(state.get("connection_id")),
        "lease_generation_present": bool(state.get("lease_generation")),
        "session_file_present": session_file.is_file(),
    }


def _redacted_state_snapshot(state: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_state_for_evidence(dict(state))
    return redacted if isinstance(redacted, dict) else {}


def _channel_command_evidence(command: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    payload = command.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    request = command.get("request")
    request = request if isinstance(request, Mapping) else {}
    request_payload = request.get("payload")
    request_payload = request_payload if isinstance(request_payload, Mapping) else {}
    expected_path = f"/api/agents/sessions/{state.get('session_id')}/input"
    response_client_request_id = payload.get("client_request_id")
    request_client_request_id = request_payload.get("client_request_id")
    native_session_id = state.get("native_session_id")
    accepted = command.get("accepted") is True
    request_bound = (
        request.get("method") == "POST"
        and request.get("path") == expected_path
        and isinstance(request_payload.get("text"), str)
        and bool(request_payload.get("text"))
        and request_payload.get("intent") in {"auto", "steer"}
        and isinstance(request_client_request_id, str)
        and bool(request_client_request_id)
        and response_client_request_id == request_client_request_id
    )
    return {
        "accepted": accepted,
        "native_session_id": native_session_id,
        "status": state.get("phase"),
        "request_path": request.get("path"),
        "request_client_request_id": request_client_request_id,
        "response_client_request_id": response_client_request_id,
        "channel_ack_bound": (accepted and request_bound and state.get("ready") is True and bool(native_session_id)),
    }


def _exact_session_retirement(receipt: Mapping[str, Any] | None, session_id: str | None) -> bool:
    return (
        isinstance(receipt, Mapping)
        and bool(session_id)
        and receipt.get("status") == "pass"
        and receipt.get("session_id") == session_id
        and receipt.get("hidden") is True
        and receipt.get("archived") is True
        and receipt.get("present_in_served_inventory") is False
    )


def _append_retirement_claim(
    claims: list[dict[str, Any]],
    *,
    session_id: str | None,
    state: Mapping[str, Any],
) -> None:
    run_id = str(state.get("run_id") or "").strip()
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id or not run_id:
        return
    claim = next(
        (item for item in claims if item.get("session_id") == normalized_session_id and item.get("run_id") == run_id),
        None,
    )
    if claim is None:
        claim = {"session_id": normalized_session_id, "run_id": run_id}
        claims.append(claim)
    # A replacement may retain its managed run identity. Acquiring that head
    # invalidates the previous head's proof even when the run ID is unchanged.
    claim["state"] = "acquired"
    claim.pop("terminal_evidence", None)


def _retirement_claim_is_proven(claim: Mapping[str, Any]) -> bool:
    evidence = claim.get("terminal_evidence")
    return (
        claim.get("state") == "terminal"
        and isinstance(evidence, Mapping)
        and evidence.get("retired") is True
        and evidence.get("session_id") == claim.get("session_id")
        and evidence.get("expected_run_id") == claim.get("run_id")
    )


def _record_retirement_claim_terminal(
    api_url: str,
    token: str,
    claims: list[dict[str, Any]],
    *,
    session_id: str,
    run_id: str,
    timeout: float = RETIREMENT_EVIDENCE_TIMEOUT_SEC,
) -> dict[str, Any]:
    claim = next((item for item in claims if item.get("session_id") == session_id and item.get("run_id") == run_id), None)
    if claim is None:
        raise RuntimeError(f"retirement claim was not acquired for run {run_id}")
    claim["state"] = "acquired"
    claim.pop("terminal_evidence", None)

    deadline = time.monotonic() + timeout
    evidence: dict[str, object] = {}
    while time.monotonic() < deadline:
        evidence = lifecycle._served_run_terminal_evidence(api_url, token, session_id, run_id)
        if evidence.get("retired") is True:
            claim["terminal_evidence"] = dict(evidence)
            claim["state"] = "terminal"
            return dict(evidence)
        time.sleep(0.2)
    raise expired(RuntimeError(f"run {run_id} did not reach canonical terminal evidence: {evidence}"), RUNTIME_HOST)


def _wait_served_run_retirement(
    api_url: str,
    token: str,
    session_id: str,
    claims: list[dict[str, Any]],
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = dict(lifecycle._served_run_inventory_evidence(api_url, token, session_id, claims))
        all_claims_proven = bool(claims) and all(_retirement_claim_is_proven(claim) for claim in claims)
        last["all_acquired_runs_terminal"] = all_claims_proven
        last["terminal_evidence_by_run"] = [
            {
                "session_id": claim.get("session_id"),
                "run_id": claim.get("run_id"),
                "state": claim.get("state"),
                "terminal_evidence": dict(claim["terminal_evidence"]) if isinstance(claim.get("terminal_evidence"), Mapping) else None,
            }
            for claim in claims
        ]
        if last.get("retired") is True and all_claims_proven:
            last["retirement_wait_status"] = "pass"
            return last
        last["retired"] = False
        last["active_run_count"] = None
        time.sleep(0.2)
    last["retirement_wait_status"] = "timeout"
    return last


def _turn_sequence(state: Mapping[str, Any]) -> int:
    value = state.get("live_turn_seq")
    return value if type(value) is int and value >= 0 else 0


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _wait_channel_terminal(
    home: Path,
    family: Family,
    *,
    session_id: str,
    native_session_id: str,
    session_file: Path,
    minimum_turn_seq: int = 0,
    exact_turn_seq: int | None = None,
    timeout: float = 90,
) -> tuple[dict[str, Any], dict[str, Any]]:
    def observe() -> tuple[dict[str, Any], dict[str, Any]] | None:
        for state in _states(home, family, session_id):
            if (
                state.get("ready") is True
                and state.get("native_session_id") == native_session_id
                and state.get("session_file") == str(session_file)
                and (_turn_sequence(state) == exact_turn_seq if exact_turn_seq is not None else _turn_sequence(state) >= minimum_turn_seq)
                and state.get("agent_end_observed") is True
                and state.get("agent_end_is_terminal") is True
            ):
                return (
                    {
                        "type": "agent_end",
                        "source": "omp_helm_extension_channel",
                        **({"isTerminal": state["agent_end_is_terminal"]} if state.get("agent_end_is_terminal_present") is True else {}),
                        **(
                            {"willContinue": state["agent_end_will_continue"]}
                            if state.get("agent_end_will_continue_present") is True
                            else {}
                        ),
                    },
                    state,
                )
        return None

    return _wait(observe, timeout=timeout, description=f"{family.name} Helm extension-channel agent_end")


def _native_settlement(
    session_file: Path,
    *,
    channel_state: Mapping[str, Any],
    native_session_id: str,
) -> dict[str, object]:
    try:
        payload = session_file.read_bytes()
    except OSError as exc:
        return {
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "agent_end_terminal": False,
            "agent_end_evidence_shape": False,
            "native_archive_bound": False,
            "native_terminal_after_assistant": False,
            "malformed_source": True,
            "session_file": str(session_file),
        }
    rows: list[dict[str, Any]] = []
    malformed_source = False
    offset = 0
    for raw in payload.splitlines(keepends=True):
        end = offset + len(raw)
        if not raw.strip():
            offset = end
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            malformed_source = True
            offset = end
            continue
        if isinstance(value, dict):
            row = dict(value)
            row["_source_offset"] = offset
            rows.append(row)
        else:
            malformed_source = True
        offset = end
    headers = [row for row in rows if row.get("type") == "session" and row.get("id") == native_session_id]
    assistant_rows = [
        row
        for row in rows
        if row.get("type") in {"message", "message_end"}
        and isinstance(row.get("message"), Mapping)
        and row["message"].get("role") == "assistant"
    ]
    native_archive_bound = len(headers) == 1
    native_terminal_after_assistant = False
    channel_bound = (
        channel_state.get("status") == "ready"
        and channel_state.get("phase") == "idle"
        and channel_state.get("native_session_id") == native_session_id
        and channel_state.get("session_file") == str(session_file)
        and isinstance(channel_state.get("updated_at"), str)
        and bool(channel_state.get("updated_at"))
    )
    channel_agent_end_observed = channel_state.get("agent_end_observed") is True and isinstance(
        channel_state.get("agent_end_is_terminal"), bool
    )
    agent_end_terminal = channel_agent_end_observed and channel_state.get("agent_end_is_terminal") is True
    return {
        "status": (
            "pass"
            if agent_end_terminal and native_archive_bound and bool(assistant_rows) and channel_bound and not malformed_source
            else "fail"
        ),
        "agent_end_terminal": agent_end_terminal,
        "agent_end_evidence_shape": channel_bound and channel_agent_end_observed,
        "agent_end_evidence_source": "omp_helm_extension_channel" if channel_bound and channel_agent_end_observed else None,
        "native_archive_bound": native_archive_bound,
        "native_session_header_count": len(headers),
        "native_terminal_after_assistant": native_terminal_after_assistant,
        "malformed_source": malformed_source,
        "agent_settled_is_not_completion_contract": True,
        "channel_phase": channel_state.get("phase"),
        "channel_status": channel_state.get("status"),
        "channel_native_session_id": channel_state.get("native_session_id"),
        "channel_session_file": channel_state.get("session_file"),
        "channel_updated_at": channel_state.get("updated_at"),
        "session_file": str(session_file),
    }


def _stale_frame(old_state: Mapping[str, Any], family: Family, *, text: str) -> dict[str, Any]:
    """Send a command frame as an owner that is no longer current, and return the launcher's answer."""

    socket_path = str(old_state.get("socket_path") or "")
    if not socket_path:
        raise RuntimeError(f"old {family.name} Helm state has no socket path")
    request = {
        "kind": "send",
        "session_id": old_state.get("session_id"),
        family.id_key: old_state.get("native_session_id"),
        "connection_id": old_state.get("connection_id"),
        "lease_generation": old_state.get("lease_generation"),
        "auth_token": old_state.get("channel_token"),
        "text": text,
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode())
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            payload += chunk
    try:
        value = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


_MANAGED_CONTROL_UNAVAILABLE = "Managed control channel is not connected or does not advertise this capability"


def _control_channel_reconnecting(status: int, detail: str) -> bool:
    """True only when the Runtime Host refused before dispatching anything.

    Input answers 409 while no live channel exists. Interrupt/terminate answer
    502 with the managed-control-unavailable message, which the dispatcher
    returns only when transport selection or grant preparation refused: the
    command never reached the Machine Agent. A dispatched failure carries a
    different message and is never retried.
    """

    if status == 409:
        return "live longhouse control channel" in detail.lower()
    if status != 502:
        return False
    try:
        body = json.loads(detail)
    except json.JSONDecodeError:
        return False
    error = body.get("detail") if isinstance(body, dict) else None
    return (
        isinstance(error, dict)
        and (error.get("error_code") or error.get("code")) in {"interrupt_failed", "terminate_failed"}
        and error.get("message") == _MANAGED_CONTROL_UNAVAILABLE
    )


def _runtime_post(
    api_url: str, token: str, path: str, payload: Mapping[str, Any] | None = None, retried: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """One control request. Each refusal it retried on (the control channel reconnecting) is appended to ``retried``."""

    body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
    headers = {
        "X-Agents-Token": token,
        "Accept": "application/json",
        "User-Agent": "LonghouseProviderFactory/1.0",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(60):
        request = urllib.request.Request(
            f"{api_url.rstrip('/')}{path}",
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            # TranscriptShipper.flush restarts the real Machine Agent after
            # the storage proof. The control websocket can take a few seconds
            # to re-register; do not mistake that bounded reconnect window for
            # a failed provider control path.
            if _control_channel_reconnecting(exc.code, detail) and attempt < 59:
                if retried is not None:
                    retried.append({"http_status": exc.code, "detail": detail[:300]})
                time.sleep(0.5)
                continue
            raise RuntimeHostError(exc.code, detail[:500]) from exc
        if not isinstance(value, dict):
            raise RuntimeError("Runtime Host returned a non-object control response")
        return value
    raise RuntimeError("Runtime Host control request exhausted reconnect attempts")


def _host_control(api_url: str, token: str, family: Family, command: str, session_id: str, *, text: str | None = None) -> dict[str, object]:
    """Dispatch a control through the Runtime Host's agents API, as every client does.

    The Runtime Host is the authority for remote control, and the disposable
    Machine Agent is the only process that talks to the provider. A send made
    mid-turn is a durable receipt the turn-boundary drain delivers: the
    Runtime Host answers "queued", where "sent" would mean the provider took
    it mid-turn. ``send_live`` is the route that delivers to an idle session
    at once.
    """

    session = f"/api/agents/sessions/{session_id}"
    if command in {"send", "steer"}:
        if not text:
            raise RuntimeError(f"{family.name} Helm {command} requires text")
        path = f"{session}/input"
        payload: dict[str, object] | None = {
            "text": text,
            "intent": "steer" if command == "steer" else "auto",
            "client_request_id": f"{family.provider}-helm-{command}-{os.urandom(8).hex()}",
        }
    elif command == "send_live":
        path, payload = f"{session}/send-live", {"message": text}
    elif command == "abort":
        path, payload = f"{session}/interrupt-live", None
    elif command == "terminate":
        path, payload = f"{session}/terminate-live", None
    else:
        raise RuntimeError(f"unsupported {family.name} Helm control command: {command}")

    retried: list[dict[str, Any]] = []
    response = _runtime_post(api_url, token, path, payload, retried)
    accepted = {
        "send": response.get("outcome") in {"sent", "queued"},
        "steer": response.get("outcome") in {"sent", "queued"},
        "send_live": response.get("accepted") is True,
        "abort": response.get("interrupt_dispatched") is True,
        "terminate": response.get("terminate_dispatched") is True,
    }[command]
    return {
        "returncode": 0,
        "accepted": accepted,
        "payload": response,
        "transport": "runtime_host_agents_api",
        "path": path,
        "request": {"method": "POST", "path": path, "payload": dict(payload) if isinstance(payload, Mapping) else None},
        # A command the channel took only on a later try is not the same evidence as one it took at once.
        "retries": {"count": len(retried), "refused_with": retried[-1] if retried else None},
    }


def _local_control(
    engine: Path, family: Family, command: str, session_id: str, env: Mapping[str, str], *, text: str | None = None
) -> dict[str, object]:
    """Dispatch a control through the engine's own command on this machine, which speaks to the launcher directly."""

    argv = [str(engine), f"{family.provider}-helm", command, "--session-id", session_id]
    if text is not None:
        argv.extend(("--text", text))
    completed = subprocess.run(argv, env=dict(env), capture_output=True, text=True, timeout=20, check=False)
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "command": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
        "payload": payload,
        "accepted": completed.returncode == 0 and payload.get("ok") is True,
        "transport": "local_engine_command",
    }


def _wait_stopped(home: Path, family: Family, session_id: str, *, timeout: float = 30) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        return next((state for state in _states(home, family, session_id) if state.get("status") == "stopped"), None)

    return _wait(observe, timeout=timeout, description=f"{family.name} Helm remote termination", waited_on=LONGHOUSE_ENGINE)


def _recorded_owner_status(record: Mapping[str, Any]) -> str:
    pid = record.get("pid")
    expected_birth = record.get("expected_birth")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not expected_birth:
        return "invalid"
    if not record["alive"]:
        return "dead"
    if record.get("birth") == str(expected_birth):
        return "alive"
    return "reused" if record.get("birth") else "unknown"


def _wait_recorded_owners_dead(
    home: Path, family: Family, session_id: str, expected_state: Mapping[str, Any], *, owner: str, timeout: float = 20.0
) -> dict[str, Any]:
    """Wait until the launcher records a remote termination and the two processes it recorded have stopped.

    The recorded launcher is not the process this producer started on the
    terminal: `longhouse <provider>` runs the launcher as its child. So the
    recorded launcher is read by its own pid and birth here, and the
    terminal's process is collected separately by whoever started it.
    """

    expected = {
        label: (expected_state.get(f"{label}_pid"), expected_state.get(f"{label}_process_start_time")) for label in ("launcher", "provider")
    }
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = next(iter(_states(home, family, session_id)), None)
        owners = [_process_record(pid, birth, label, owner=owner) for label, (pid, birth) in expected.items()]
        owner_status = {record["label"]: _recorded_owner_status(record) for record in owners}
        state_identity_matches = bool(state) and all(
            state.get(f"{label}_pid") == pid and state.get(f"{label}_process_start_time") == birth
            for label, (pid, birth) in expected.items()
        )
        last = {
            "state_status": state.get("status") if state else None,
            "terminal_reason": state.get("terminal_reason") if state else None,
            "state_identity_matches": state_identity_matches,
            "owner_status": owner_status,
            "owners": owners,
        }
        if (
            state
            and state.get("status") == "stopped"
            and state.get("terminal_reason") == "remote_terminate"
            and state_identity_matches
            and all(status in {"dead", "reused"} for status in owner_status.values())
        ):
            return {"status": "pass", **last}
        time.sleep(0.2)
    raise expired(RuntimeError(f"timed out waiting for terminated {family.name} Helm execution owners: {last}"), LONGHOUSE_ENGINE)


def _read_source_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _register_native_source(
    claims: list[dict[str, Any]],
    *,
    provider: str,
    label: str,
    source_path: object,
    session_id: object,
    run_id: object,
    native_session_id: object,
) -> None:
    """Claim a native source before later lifecycle work can fail."""

    if not isinstance(source_path, str) or not source_path:
        return
    if any(item.get("source_path") == source_path and item.get("run_id") == run_id for item in claims):
        return
    claims.append(
        {
            "label": label,
            "provider": provider,
            "run_id": run_id,
            "source_path": source_path,
            "session_id": session_id,
            "native_session_id": native_session_id,
        }
    )


def _remove_isolation_after_source_retention(
    isolation: Path,
    *,
    source_retention_verified: bool,
    runtime_cleanup_verified: bool,
    cleanup: dict[str, Any],
) -> bool:
    if not source_retention_verified or not runtime_cleanup_verified:
        cleanup["isolation_retained"] = True
        cleanup["authoritative_source_evidence_retained"] = isolation.exists()
        cleanup["isolation_retention_reason"] = (
            "source retention is incomplete" if not source_retention_verified else "owned runtime cleanup is incomplete"
        )
        return False
    try:
        shutil.rmtree(isolation)
    except FileNotFoundError:
        return True
    except OSError as exc:
        cleanup["isolation_remove_error"] = f"{type(exc).__name__}: {exc}"
        return False
    return not isolation.exists()


def _process_record(pid: object, expected_birth: object, label: str, *, owner: str) -> dict[str, Any]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {
            "owner": owner,
            "label": label,
            "pid": pid,
            "process_group_id": None,
            "pgid": None,
            "birth_matches": False,
            "pid_positive": False,
            "process_group_positive": False,
            "pid_dead": False,
            "process_group_dead": False,
            "alive": False,
        }
    try:
        completed = subprocess.run(
            ["ps", "-o", "pid=", "-o", "pgid=", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        fields = completed.stdout.strip().split(None, 2)
        pgid = int(fields[1]) if len(fields) >= 2 else None
        birth = fields[2].strip() if len(fields) >= 3 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pgid = None
        birth = None
    return {
        "owner": owner,
        "label": label,
        "pid": pid,
        "process_group_id": pgid,
        "pgid": pgid,
        "birth": birth,
        "expected_birth": expected_birth,
        "birth_matches": bool(birth and expected_birth and birth == str(expected_birth)),
        "pid_positive": True,
        "process_group_positive": isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 0,
        "pid_dead": _pid_dead(pid),
        "process_group_dead": _pgid_dead(pgid),
        "alive": _pid_alive(pid),
    }


def _cleanup_receipt(records: list[dict[str, Any]]) -> dict[str, Any]:
    unique = {
        (
            item.get("owner"),
            item.get("label"),
            item.get("pid"),
            item.get("process_group_id", item.get("pgid")),
            item.get("expected_birth"),
        ): dict(item)
        for item in records
    }
    records = list(unique.values())
    for item in records:
        process_group_id = item.get("process_group_id", item.get("pgid"))
        item["process_group_id"] = process_group_id
        item["pgid"] = process_group_id
        item["pid_positive"] = isinstance(item.get("pid"), int) and not isinstance(item.get("pid"), bool) and item["pid"] > 0
        item["process_group_positive"] = (
            isinstance(process_group_id, int) and not isinstance(process_group_id, bool) and process_group_id > 0
        )
        item["pid_dead"] = _pid_dead(item.get("pid"))
        item["process_group_dead"] = _pgid_dead(process_group_id)
        item["alive"] = not item["pid_dead"]
    provider_records = [item for item in records if item.get("label") == "provider"]
    provider_dead = bool(provider_records) and all(item.get("pid_dead") is True for item in provider_records)
    groups_dead = bool(records) and all(item.get("process_group_dead") is True for item in records)
    birth_verified = bool(records) and all(item.get("birth_matches") is True for item in records)
    orphan_count = sum(not (item.get("pid_dead") is True and item.get("process_group_dead") is True) for item in records)
    return {
        "status": "pass" if provider_dead and groups_dead and birth_verified and orphan_count == 0 else "fail",
        "provider_process_dead": provider_dead,
        "process_group_dead": groups_dead,
        "no_orphan_provider_processes": orphan_count == 0 and birth_verified,
        "birth_identities_verified": birth_verified,
        "orphan_count": orphan_count,
        "owned_process_count": len(records),
        "owned_processes": records,
    }


def _wait_cleanup_receipt(records: list[dict[str, Any]], timeout: float = 10.0) -> dict[str, Any]:
    """Allow asynchronously exiting provider children to reach a settled receipt."""

    cleanup = _cleanup_receipt(records)
    deadline = time.monotonic() + timeout
    while cleanup.get("birth_identities_verified") is True and cleanup.get("status") != "pass" and time.monotonic() < deadline:
        time.sleep(0.1)
        cleanup = _cleanup_receipt(records)
    return cleanup


def _manifest_is_stable(root: Path, manifest: list[dict[str, Any]]) -> bool:
    """Require the final evidence tree to remain unchanged before publishing."""

    return bool(manifest) and manifest == artifact_manifest(root)


def _helm_cleanup_ready(cleanup: Mapping[str, Any]) -> bool:
    return (
        cleanup.get("status") == "pass"
        and cleanup.get("provider_process_dead") is True
        and cleanup.get("process_group_dead") is True
        and cleanup.get("orphan_count") == 0
        and cleanup.get("shipper_stop_verified") is True
        and cleanup.get("canary_session_hidden") is True
        and cleanup.get("served_run_retired") is True
        and cleanup.get("source_retention_verified") is True
        and cleanup.get("isolation_removed") is True
    )


def _wait_task_tool_boundary(session_file: Path, task_marker: str, *, timeout: float = 90) -> dict[str, Any]:
    """Wait until the task turn has returned at least one tool result."""

    def observe() -> dict[str, Any] | None:
        if not session_file.is_file():
            return None
        boundary = task_tool_boundary(read_session_entries(session_file), task_marker)
        return {"tool_result_id": boundary} if boundary else None

    return _wait(
        observe,
        timeout=timeout,
        description=f"tool boundary in task {task_marker}",
        # The model's first tool call in a turn the provider holds the prompt for.
        waited_on=lambda: PROVIDER_TURN if provider_holds_prompt(session_file, task_marker) else None,
    )


def _wait_task_tool_call_boundary(session_file: Path, task_marker: str, *, timeout: float = 90) -> dict[str, Any]:
    """For the local fixture, stop the run at the issued tool call before its long shell command ends."""

    def observe() -> dict[str, Any] | None:
        if not session_file.is_file():
            return None
        boundary = task_tool_call_boundary(read_session_entries(session_file), task_marker)
        return {"tool_call_id": boundary} if boundary else None

    return _wait(
        observe,
        timeout=timeout,
        description=f"tool call in task {task_marker}",
        waited_on=lambda: PROVIDER_TURN if provider_holds_prompt(session_file, task_marker) else None,
    )


# How long the first turn may take to answer. A plain launch turn is one model
# call. The background turn is a whole job: about five model calls in the
# parent, a child agent with calls of its own, and two 2 s commands. The parent
# spends most of it inside one `wait` tool call until the child reports.
#
# Measured on the factory, prompt to answer, over the 56 background turns that
# answered between 2026-10-01 and 2026-10-11: median 19.7 s, 95th percentile
# 58.5 s, slowest 82.6 s; under the current prompt, fourteen turns, slowest
# 59.8 s. The slow turns have the same four to six parent messages as the fast
# ones, and in the two slowest the parent never waited more than 4.9 s for its
# own next message: the time is the child (49.5 s inside `wait` in the 59.8 s
# turn, about 7 s in a typical one). The retained evidence cannot say whether
# that child was slow or its stream stalled and recovered. Either way the turn
# completed and did everything the scenario judges, and the 90 s every other
# wait here uses left it 7.4 s.
#
# 150 s is 1.8 times the slowest completed turn and what the steer and abort
# turns below already get. A stream that stalls for good is silent longer than
# any margin worth waiting (54 s and 75 s in the parent on 2026-10-11): the
# factory's same-tick retry answers that, not this number.
_FIRST_TURN_TIMEOUT_SECS = 90.0
_BACKGROUND_FIRST_TURN_TIMEOUT_SECS = 150.0


def _first_turn_timeout(*, background: bool) -> float:
    return _BACKGROUND_FIRST_TURN_TIMEOUT_SECS if background else _FIRST_TURN_TIMEOUT_SECS


def _redacted(value: Any, secrets: list[str]) -> Any:
    """``value`` without the channel's authority, and with no qualification secret left in a string."""

    def clean(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "<redacted>")
            return item
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        return item

    return clean(redact_state_for_evidence(value))


def _context_prompt(phrase: str, marker: str) -> str:
    # Pi's wording. A two-part "Then\n\nNew machine-check request" frame read as
    # a prompt injection to the qualification model, which refused it or went
    # exploring the workspace instead of replying.
    return (
        f"Remember this context phrase for later: {phrase}. Do not read any file for it and do not repeat it now. "
        f"Reply with exactly {marker} and no other text."
    )


def _resume_prompt(family: Family, marker: str) -> str:
    # Name where the phrase is and what it starts with, never the phrase
    # itself: only restored history can supply it, and an unambiguous ask
    # leaves a cheap model no reason to decline.
    return (
        f"Earlier in this conversation I gave you a context phrase that starts with {family.marker_prefix}_CONTEXT_. "
        f"It is in the conversation history above. Reply with that exact phrase, then {marker}, and nothing else."
    )


class Lifecycle:
    """One provider's Helm lifecycle in progress: what it has launched, and what it has recorded of each step.

    A step records and does not judge: the provider's cells read the finished
    observation (``Family.cells``). The steps are run in the order the
    provider's module gives them.
    """

    def __init__(self, family: Family, run: Run, args: argparse.Namespace) -> None:
        self.f, self.run, self.args, self.root = family, run, args, run.root
        self.scripted_model = evidence_class(args) == "scripted_model"
        self.scripted_model_server: scripted_model_server.ThreadedServer | None = None
        self.scripted_model_thread: threading.Thread | None = None
        self.scripted_model_script: Path | None = None
        self.scripted_model_expected_turns: frozenset[str] = frozenset()
        # The opt-in guards against spending a real model by accident; a scripted
        # run talks only to its own loopback fixture, so it needs none.
        if not self.scripted_model and os.environ.get(family.live_env) not in {"1", "true", "yes", "on"}:
            raise RuntimeError(f"{family.name} Helm qualification requires explicit {family.live_env} opt-in")
        self.api_url = str(args.api_url or os.environ.get(RUNTIME_API_URL_ENV) or "")
        self.token = str(args.agents_token or os.environ.get(RUNTIME_AGENTS_TOKEN_ENV) or "")
        if not self.api_url or not self.token:
            raise RuntimeError(f"{family.name} Helm qualification requires Runtime Host URL and token")
        require_disposable_runtime(self.api_url)
        # Keep a failed isolation quarantine outside the sealed evidence tree. It
        # may contain the disposable machine token until owned processes are dead;
        # the evidence scanner must never have to choose between preserving it and
        # accepting a secret-bearing artifact.
        self.isolation = new_qualification_isolation_root(f"{family.provider}-helm")
        self.provider_home = self.isolation / family.homes[0]
        self.home = self.isolation / family.homes[1]
        self.workspace = self.isolation / "workspace"
        self.negative_control = getattr(args, "negative_control", None)
        self.background = bool(getattr(args, "background_prompt", None))
        try:
            self.provider_home.mkdir(mode=0o700, parents=True)
            self.workspace.mkdir(mode=0o700, parents=True)
            upper = family.provider.upper()
            self.env = dict(os.environ)
            if self.scripted_model:
                self.env = scripted_model_profiles.credential_free_environment(reject_ambient_credentials=False)
            self.env.update(
                {
                    "HOME": str(self.provider_home),
                    "LONGHOUSE_HOME": str(self.home),
                    f"LONGHOUSE_{upper}_BIN": str(args.provider_bin),
                    f"LONGHOUSE_{upper}_HELM_URL": self.api_url,
                    f"LONGHOUSE_{upper}_HELM_TOKEN": self.token,
                    "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
                    "LONGHOUSE_LAUNCH_ACTOR": "automation",
                    "LONGHOUSE_LAUNCH_SURFACE": "qa",
                    **family.environment(self.provider_home, args),
                }
            )
            if self.negative_control:
                self.env["LONGHOUSE_QA_FAULT"] = fault_name(family.provider, self.negative_control)
                self.env["LONGHOUSE_QA_FAULT_RECEIPT"] = str(self.root / "qa-fault-receipt.jsonl")
            family.prepare(self)
            if self.scripted_model:
                self._start_scripted_model()
        except BaseException:
            self._close_scripted_model_server()
            shutil.rmtree(self.isolation)
            raise
        self.terminals: list[ProviderPtySession] = []
        self.shipper: Any = None
        self.state: dict[str, Any] = {}
        self.session_id = ""
        self.session_file = Path()
        self.context_phrase = ""
        self.second_launcher_evidence: dict[str, Any] | None = None
        self.owner_records: list[dict[str, Any]] = []
        self.source_claims: list[dict[str, Any]] = []
        self.retirement_claims: list[dict[str, Any]] = []
        self.served_run_inventory: dict[str, Any] = {}
        self.failure: Exception | None = None
        self.queued_sends: list[dict[str, Any]] = []
        self._last_begin: tuple[str, dict[str, Any]] | None = None
        self.controls: dict[str, dict[str, Any]] = {}
        self.observation: dict[str, Any] = {
            "observation_scope": "scenario",
            "provider": family.provider,
            "owners": {},
            "controls": self.controls,
            "queued_sends": self.queued_sends,
            "convergence": {},
            "control_identity": {},
            "flush": {},
            "sources": [],
            "settlement": {},
            "cleanup": {},
        }

    def _start_scripted_model(self) -> None:
        script = self.root / "scripted-model-fixture.json"
        turns, expected = _scripted_model_fixture(self.f, getattr(self, "proof_file", None))
        script.write_text(json.dumps({"turns": turns}, sort_keys=True) + "\n", encoding="utf-8")
        script.chmod(0o600)
        turns, script_sha256 = scripted_model_server.load_script(script)
        server = scripted_model_server.ThreadedServer(
            ("127.0.0.1", 0),
            scripted_model_server.MockHandler,
            script_turns=turns,
            script_sha256=script_sha256,
            receipt_file=self.root / "scripted-model-server-receipt.json",
        )
        thread = threading.Thread(target=server.serve_forever, name=f"{self.f.provider}-helm-scripted-model", daemon=True)
        self.scripted_model_server, self.scripted_model_thread = server, thread
        thread.start()
        deadline = time.monotonic() + 5.0
        while True:
            try:
                with urllib.request.urlopen(f"{server.endpoint}/healthz", timeout=0.5):
                    break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("scripted model server did not become ready on loopback")
                time.sleep(0.01)
        self.scripted_model_expected_turns = expected
        self.scripted_model_script = script
        self.env[scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV] = server.endpoint
        self.env[scripted_model_profiles.SCRIPTED_MODEL_SCRIPT_ENV] = str(script)
        scripted_model_profiles.configure_scripted_model_profile(self.f.provider, self.provider_home, self.env)
        if self.f.provider == "omp":
            self.args.model = f"{scripted_model_profiles.SCRIPTED_MODEL_PROVIDER}/{scripted_model_profiles.SCRIPTED_MODEL_NAME}"
        else:
            self.args.model_provider = scripted_model_profiles.SCRIPTED_MODEL_PROVIDER
            self.args.model = scripted_model_profiles.SCRIPTED_MODEL_NAME

    def _close_scripted_model_server(self) -> None:
        server = self.scripted_model_server
        if server is None:
            return
        thread = self.scripted_model_thread
        if thread is not None and thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5.0)
        self.scripted_model_server = None
        self.scripted_model_thread = None

    def _record_scripted_model_receipt(self) -> None:
        if not self.scripted_model:
            return
        try:
            receipt = scripted_model_profiles.scripted_model_receipt(
                self.env,
                requests_before=0,
                require_served_request=True,
            )
            missing = sorted(self.scripted_model_expected_turns - set(receipt["matched_turns"]))
            if missing:
                raise RuntimeError("scripted model did not serve required Helm turns: " + ", ".join(missing))
            receipt["status"] = "pass"
        except Exception as exc:  # noqa: BLE001 - a missing or failed fixture request is scripted-lane evidence
            receipt = {
                "status": "fail",
                "endpoint": self.env.get(scripted_model_profiles.SCRIPTED_MODEL_BASE_URL_ENV),
                "script_sha256": sha256_file(self.scripted_model_script) if self.scripted_model_script is not None else None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        self.observation["scripted_model"] = receipt
        lifecycle.write_json(self.root / "scripted-model-receipt.json", receipt)
        self._close_scripted_model_server()

    # -- what the steps share

    @property
    def scripted_turn_barrier(self) -> bool:
        """Use fresh, provider-correlated boundaries only in the deterministic lane."""

        return self.scripted_model and self.f.settles_between_turns

    def wait_for_turn_completion(
        self,
        before: Mapping[str, Any],
        completion_row: Mapping[str, Any],
        *,
        marker: str,
        timeout: float = 30,
    ) -> dict[str, Any]:
        """Wait for this turn's terminal evidence before permitting another input."""

        message = completion_row.get("message")
        if (
            completion_row.get("type") != "message"
            or not isinstance(message, Mapping)
            or message.get("role") != "assistant"
            or not marker_answered(lifecycle._native_message_text(message), marker)
        ):
            raise RuntimeError(f"{self.f.name} Helm {marker} is not bound to its native assistant answer")

        if self.f.provider == "omp":
            _event, state = _wait_channel_terminal(
                self.home,
                self.f,
                session_id=self.session_id,
                native_session_id=str(self.state.get("native_session_id") or ""),
                session_file=self.session_file,
                exact_turn_seq=_turn_sequence(before) + 1,
                timeout=timeout,
            )
            return state

        if message.get("stopReason") not in {"stop", "length"}:
            raise RuntimeError(f"Pi Helm {marker} did not finish in its native assistant completion row")
        completed_at = _parse_timestamp(completion_row.get("timestamp"))
        if completed_at is None:
            raise RuntimeError(f"Pi Helm {marker} native completion row has no valid timestamp")

        def idle_after_completion(state: Mapping[str, Any]) -> bool:
            updated_at = _parse_timestamp(state.get("updated_at"))
            return self.f.is_idle(state) and updated_at is not None and updated_at > completed_at

        return self.wait_state(idle_after_completion, timeout=timeout)

    def marker(self, name: str) -> str:
        return f"{self.f.marker_prefix}_{name}_{os.urandom(8).hex()}"

    def prompt(self, kind: str, marker: str) -> str:
        return self.f.prompts[kind].format(marker=marker, proof_file=getattr(self, "proof_file", ""))

    def wait_state(self, predicate: Any = None, *, timeout: float = 30) -> dict[str, Any]:
        return _wait_state(self.home, self.f, session_id=self.session_id or None, predicate=predicate, timeout=timeout)

    def or_missing(self, wait: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """What a wait saw. Where the family records a miss, a wait that expired saw nothing, and a cell judges that."""

        if not self.f.records_misses:
            return wait()
        try:
            return wait()
        except RuntimeError:
            return {}

    def busy_state(
        self,
        *,
        after: Mapping[str, Any] | None = None,
        sent: Mapping[str, Any] | None = None,
        what: str | None = None,
    ) -> dict[str, Any]:
        """The state of a turn in progress, and a later one than ``after`` when that is given."""

        last_begin = getattr(self, "_last_begin", None)
        if sent is None and last_begin is not None:
            last_what, sent = last_begin
            if what is None:
                what = last_what
        self._last_begin = None
        payload = sent.get("payload") if isinstance(sent, Mapping) else None
        queued = bool(isinstance(sent, Mapping) and sent.get("queued")) or (
            isinstance(payload, Mapping) and payload.get("outcome") == "queued"
        )
        timeout = self.f.timeouts["busy"]

        def busy(state: Mapping[str, Any]) -> bool:
            if state.get("phase") not in {"running", "thinking"}:
                return False
            if after is None or self.f.provider != "omp":
                return True
            return _turn_sequence(state) > _turn_sequence(after)

        if queued:
            try:
                return self.wait_state(busy, timeout=timeout)
            except RuntimeError as exc:
                label = what or "busy turn's send"
                raise RuntimeError(f"{self.f.name} Helm {label} stayed queued and started no turn within {timeout:g}s: {payload}") from exc

        return self.or_missing(lambda: self.wait_state(busy, timeout=timeout))

    def settle(self) -> dict[str, Any]:
        """Wait for the turn to be over, without making that a condition: a turn that never settles shows in what follows."""

        try:
            return self.wait_state(self.f.is_idle, timeout=120)
        except RuntimeError:
            return {}

    def control(self, command: str, text: str | None = None, *, local: bool = False) -> dict[str, Any]:
        """Dispatch a control through the Runtime Host, or when ``local`` through the engine's own command on this machine."""

        if local:
            return _local_control(self.args.engine, self.f, command, self.session_id, self.env, text=text)
        return _host_control(self.api_url, self.token, self.f, command, self.session_id, text=text)

    def begin(self, what: str, prompt: str, *, local: bool = False) -> dict[str, Any]:
        """Send a prompt that starts a turn, or is queued while the host's turn-end view catches up.

        Where a refused command ends the run, it ends it here and says which
        send it was, instead of at whatever later step first misses the turn.
        """

        sent = dict(self.control("send", prompt, local=local))
        payload = sent.get("payload")
        if self.f.refused_command_ends_run and sent.get("accepted") is not True:
            detail = payload or sent.get("stderr") or sent.get("error")
            raise RuntimeError(f"{self.f.name} Helm {what} was not accepted, so it started no turn: {detail}")
        queued = isinstance(payload, Mapping) and payload.get("outcome") == "queued"
        sent["queued"] = queued
        if queued:
            queued_sends = getattr(self, "queued_sends", None)
            if queued_sends is None:
                queued_sends = self.queued_sends = []
            queued_sends.append({"what": what, "payload": dict(payload)})
        self._last_begin = (what, sent)
        return sent

    def host(self, command: str, text: str) -> dict[str, Any]:
        """Dispatch a control through the Runtime Host, whatever the lifecycle's own transport."""

        sent = _host_control(self.api_url, self.token, self.f, command, self.session_id, text=text)
        if self.f.refused_command_ends_run and sent["accepted"] is not True:
            raise RuntimeError(f"Runtime Host did not accept {self.f.name} input: {sent['payload']}")
        return sent

    def start(self, prompt: str, terminal: str, *, resume: bool = False) -> tuple[ProviderPtySession, list[str]]:
        argv = self.f.launch_argv(self.args, workspace=self.workspace, prompt=prompt, resume_session=self.session_id if resume else None)
        session = ProviderPtySession.start(
            argv=argv,
            cwd=self.workspace,
            env=self.env,
            terminal_path=self.root / terminal,
            thread_name=f"{self.f.provider}-helm-{Path(terminal).stem}-drain",
        )
        self.terminals.append(session)
        return session, argv

    def own(self, label: str, state: Mapping[str, Any], *, marker: str | None = None, controls: tuple[str, ...] = ()) -> None:
        """Record an execution owner: its two processes, its state, and the session file it writes.

        The recorded launcher is the engine's, not the process this producer
        started on the terminal: `longhouse <provider>` runs the launcher as
        its child. Each is followed by its own identity; neither stands for
        the other.
        """

        if self.f.retirement_claims:
            _append_retirement_claim(self.retirement_claims, session_id=self.session_id, state=state)
        for part in ("launcher", "provider"):
            self.owner_records.append(_process_record(state.get(f"{part}_pid"), state.get(f"{part}_process_start_time"), part, owner=label))
        self.observation["owners"][label] = _redacted_state_snapshot(state)
        _register_native_source(
            self.source_claims,
            provider=self.f.provider,
            label=label,
            source_path=str(self.session_file),
            session_id=self.session_id,
            run_id=state.get("run_id"),
            native_session_id=state.get("native_session_id"),
        )
        if marker is not None:
            self.observation["sources"].append(
                {
                    "label": label,
                    "native_session_id": state.get("native_session_id"),
                    "source_path": str(self.session_file),
                    "marker": marker,
                    "controls": list(controls),
                }
            )

    def reply(
        self,
        marker: str,
        offset: int,
        *,
        role: str = "assistant",
        alongside: tuple[str, ...] = (),
        recalled: bool = False,
        timeout: float = 90,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Wait for the row of the current session file that carries ``marker``, and read what the file holds of it."""

        row = _wait_native_marker(
            self.session_file, marker, minimum_offset=offset, role=role, alongside=alongside, recalled=recalled, timeout=timeout
        )
        evidence = _native_marker_evidence(
            row,
            self.session_file,
            marker=marker,
            minimum_offset=offset,
            native_session_id=str(self.state.get("native_session_id") or ""),
            role=role,
            alongside=alongside,
            recalled=recalled,
        )
        return row, evidence

    def flush(self, label: str) -> dict[str, Any]:
        receipt = self.shipper.flush(f"{self.f.provider}-helm-{label}")
        self.observation["flush"][label] = receipt
        return receipt

    def converge(
        self,
        turn: str,
        marker: str,
        row: Mapping[str, Any],
        *,
        flush: Mapping[str, Any] | None = None,
        alongside: tuple[str, ...] = (),
        recalled: bool = False,
    ) -> str:
        """Wait until the turn's reply is served, keep what was served for it, and return the name it is kept under."""

        if flush is not None and self.f.flush_must_complete and not _flush_receipt_complete(flush):
            raise RuntimeError(f"{self.f.name} Helm transcript flush did not complete a bounded ship")
        served = _runtime_convergence(
            self.api_url,
            self.token,
            family=self.f,
            session_id=self.session_id,
            native_session_id=str(self.state.get("native_session_id") or ""),
            marker=marker,
            flush=flush or {},
            native_source_path=str(self.session_file),
            alongside=alongside,
            recalled=recalled,
            native_reply_id=row.get("id"),
        )
        self.observation["convergence"][turn] = served
        return turn

    def taxonomy(self, step: str) -> None:
        """After the step whose turn ran a tool: what the provider's own file holds of tool calls and their results.

        The record says how far into the file it was read, so the same bytes can be read again from what is retained.
        """

        if self.f.tool_turn != step:
            return
        rows, _native_id, metadata = pi_transcript_rows(self.session_file)
        self.observation["native_taxonomy"] = {
            **pi_native_shadow_taxonomy(rows, metadata),
            "after": step,
            "source_path": str(self.session_file),
            "source_end_offset": metadata["source_end_offset"],
        }

    def identity(self, label: str, state: dict[str, Any]) -> dict[str, Any]:
        """Wait until the Runtime Host serves control of the session under this owner's lease."""

        served = _wait_runtime_control_identity(
            self.api_url,
            self.token,
            family=self.f,
            home=self.home,
            session_id=self.session_id,
            state=state,
            timeout=self.f.timeouts["identity"],
            bound=self.f.control_bound,
        )
        receipt = _control_identity_receipt(served)
        self.observation["control_identity"][label] = receipt
        return receipt

    # -- the steps

    def launch(self) -> None:
        f, args = self.f, self.args
        self.shipper = start_transcript_shipper(
            f.provider, args, home=self.provider_home, environment=self.env, evidence_root=self.root / "shipper", longhouse_home=self.home
        )
        self.observation["shipper"] = {"started": self.shipper.receipt.get("ready") is True, **self.shipper.receipt}
        lifecycle.write_json(self.root / "transcript-shipper-receipt.json", self.shipper.receipt)
        marker = self.marker("LAUNCH")
        prompt = self.prompt("launch", marker)
        if self.background:
            prompt = f"{args.background_prompt}\nAfter the background job reaches terminal state: {prompt}"
        _terminal, argv = self.start(prompt, "helm-terminal.raw")
        self.state = self.wait_state(timeout=f.timeouts["launch"])
        self.session_id = str(self.state["session_id"])
        self.observation["session_id"] = self.session_id
        self.session_file = Path(str(self.state.get("session_file") or ""))
        if not self.session_file.is_file():
            # The launcher may name the session's directory before the provider has written its file.
            directory = Path(str(self.state.get("session_dir") or ""))
            self.session_file = _wait(
                lambda: next((path for path in directory.rglob("*.jsonl") if path.is_file() and not path.is_symlink()), None),
                timeout=45,
                description=f"{f.name} native session file",
                waited_on=PROVIDER_PROCESS,
            )
        self.own("initial", self.state, marker=marker, controls=() if self.background else ("send", "follow_up", "steer", "abort"))
        row, evidence = self.reply(marker, 0, timeout=_first_turn_timeout(background=self.background))
        if self.scripted_turn_barrier and not f.channel_terminal:
            self.wait_for_turn_completion(self.state, row, marker=marker, timeout=f.timeouts["launch"])
        launch = self.controls["launch"] = {
            "action_label": "launch",
            "prompt": prompt,
            "command": argv,
            "state": dict(self.state),
            "marker_row": row,
            "evidence": evidence,
        }
        self.taxonomy("launch")
        settled = self.state
        if f.channel_terminal:
            settled = self.wait_state(
                lambda value: value.get("agent_end_observed") is True and _turn_sequence(value) >= _turn_sequence(self.state), timeout=90
            )
            _wait_channel_terminal(
                self.home,
                f,
                session_id=self.session_id,
                native_session_id=str(self.state.get("native_session_id") or ""),
                session_file=self.session_file,
                minimum_turn_seq=_turn_sequence(settled),
            )
        launch["served"] = self.converge("launch", marker, row, flush=self.flush("launch"))
        launch["control_identity"] = self.identity("initial", self.state)
        # Bound before any later owner exists: it reads only the first owner's state.
        self.observation["channel_binding"] = _channel_binding_evidence(self.state)
        if f.local_health:
            health = subprocess.run(
                [str(args.longhouse_cli), "local-health", "--json", "--state-root", self.env["LONGHOUSE_HOME"]],
                cwd=self.workspace,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            (self.root / "local-health.json").write_text(health.stdout, encoding="utf-8")
        if self.background:
            # The background-jobs scenario proves what the first turn did: native async
            # frames, the served terminal statuses captured here, the archive and the
            # cleanup. The control steps prove other cells; running them only made the
            # background cells fail whenever an unrelated live step flaked. Settle the
            # first session file and go straight to teardown, which runs for every run.
            self.observation["scenario_scope"] = "background"
            lifecycle.write_json(
                self.root / "omp-background-served-receipt.json",
                {
                    "managed_transport": f"{f.provider}_helm_channel",
                    "state": redact_state_for_evidence(settled),
                    "control_identity": launch.get("control_identity"),
                    "detail": _runtime_get(self.api_url, self.token, f"/api/agents/sessions/{self.session_id}"),
                    "workspace": _runtime_get(self.api_url, self.token, f"/api/agents/sessions/{self.session_id}/workspace?limit=1"),
                },
            )
            # The settled first turn, not the launch state from before it.
            self.observation["settlement"] = _native_settlement(
                self.session_file, channel_state=settled, native_session_id=str(settled.get("native_session_id") or "")
            )

    def second_launcher(self) -> None:
        """Start a second launcher against the live owner: one session has one execution owner."""

        if self.terminals[0].alive() is not True:
            raise RuntimeError(f"{self.f.name} original owner was not live for stale-launcher proof")
        stale, _argv = self.start(
            "This launcher must be refused while the original owner remains live.", "helm-stale-launcher.raw", resume=True
        )
        try:
            returncode = stale.process.wait(timeout=15)
        except subprocess.TimeoutExpired as exc:
            stale.close()
            raise RuntimeError(f"{self.f.name} stale launcher did not refuse a live owner") from exc
        stale.close()
        output = stale.terminal_path.read_text(encoding="utf-8", errors="replace")
        self.second_launcher_evidence = {
            "returncode": returncode,
            "terminal_path": str(stale.terminal_path),
            "original_owner_live": True,
            "owner_refusal_observed": returncode != 0 and ("execution owner" in output or "already attached" in output),
        }

    def send_idle(self, *, local: bool = False) -> None:
        name = "local_send" if local else "send"
        marker = self.marker(name.upper())
        before_state = self.wait_state(self.f.is_idle, timeout=30) if self.scripted_turn_barrier else dict(self.state)
        offset = _read_source_size(self.session_file)
        prompt = self.prompt("send", marker)
        command = self.control("send", prompt, local=local)
        # Recorded before the reply is waited for: a send that was taken and never answered is a verdict on the send.
        record = self.controls[name] = {
            "action_label": "send",
            "prompt": prompt,
            "state": dict(self.state),
            "command": command,
            "marker_wait": {"status": "pending", "marker": marker, "source_offset": offset},
        }
        try:
            if self.f.refused_command_ends_run and command.get("accepted") is not True:
                # Nothing was sent, so there is no reply to wait for: the record says so and the run ends here.
                record["marker_wait"]["status"] = "refused"
                raise RuntimeError(f"{self.f.name} Helm {name} was not accepted, so it started no turn: {command.get('payload')}")
            try:
                row, evidence = self.reply(marker, offset)
            except BaseException as exc:
                record["marker_wait"].update(
                    status="failed", source_size_after=_read_source_size(self.session_file), error=f"{type(exc).__name__}: {exc}"
                )
                raise
            record["marker_wait"]["status"] = "answered"
        finally:
            # Whatever the wait came to. A send that was taken and whose prompt the provider never held started no turn.
            self.observation[f"{name}_turn_verdict"] = send_turn_verdict(
                self.session_file,
                marker=marker,
                accepted=command.get("accepted") is True,
                answered=record["marker_wait"]["status"] == "answered",
            )
        record.update(marker_row=row, evidence=evidence)
        if self.scripted_turn_barrier:
            self.wait_for_turn_completion(before_state, row, marker=marker)
        # Serving a reply does not depend on how the prompt was sent: the local command's turn is not shown served again.
        if not local:
            record["served"] = self.converge("send", marker, row, flush=self.flush("send"))

    def follow_up(self) -> None:
        """Start a busy turn and send into it: a send made mid-turn is queued and delivered when the turn is over."""

        active_marker, marker = self.marker("ACTIVE"), self.marker("FOLLOW_UP")
        before_state = self.wait_state(self.f.is_idle, timeout=30) if self.scripted_turn_barrier else dict(self.state)
        offset = _read_source_size(self.session_file)
        active = self.begin("busy turn's send", self.prompt("busy", active_marker))
        active_state = self.busy_state(after=before_state if self.f.provider == "omp" else None)
        prompt = self.prompt("follow_up", marker)
        command = self.host("send", prompt)
        row, evidence = self.reply(marker, offset)
        if self.scripted_turn_barrier:
            self.wait_for_turn_completion(active_state, row, marker=marker)
        payload = command.get("payload")
        record = self.controls["follow_up"] = {
            "action_label": "follow_up",
            "prompt": prompt,
            "state": dict(self.state),
            "active_action_label": "active_turn_setup",
            "active_command": active,
            "active_state": dict(active_state),
            "command": command,
            "queued_not_sent": isinstance(payload, Mapping) and payload.get("outcome") == "queued",
            "marker_row": row,
            "evidence": evidence,
        }
        record["served"] = self.converge("follow_up", marker, row)
        self.taxonomy("follow_up")

    def steer(self, *, local: bool = False) -> None:
        f = self.f
        name = "local_steer" if local else "steer"
        before_state = self.wait_state(f.is_idle, timeout=30) if self.scripted_turn_barrier else None
        offset = _read_source_size(self.session_file)
        task, done, marker = (self.marker(f"{name.upper()}{part}") for part in ("_TASK", "_TASK_DONE", ""))
        active = self.begin(f"{name} turn's send", step_task_prompt(task, done), local=local)
        active_state = self.busy_state(after=before_state if f.provider == "omp" else None)
        boundary_wait = _wait_task_tool_call_boundary if self.scripted_model else _wait_task_tool_boundary
        self.observation[f"{name}_tool_boundary"] = boundary_wait(self.session_file, task)
        prompt = self.prompt("steer", marker)
        if not local:
            # A remote steer is judged by the host's served state, which trails the local phase: steer once it shows the turn.
            self.observation[f"{name}_served_turn"] = _wait_served_turn(self.api_url, self.token, self.session_id)
        command = self.control("steer", prompt, local=local)
        row, evidence = self.reply(marker, offset, timeout=150)
        settled = self.settle()
        verdict = steer_turn_verdict(read_session_entries(self.session_file), task_marker=task, steer_marker=marker, task_done_marker=done)
        self.observation[f"{name}_turn_verdict"] = verdict
        noncompliance = pi_family_steer_noncompliance(verdict)
        if noncompliance is not None:
            attach(self.observation, f"{f.provider}_helm_steer_active", noncompliance)
        record = self.controls[name] = {
            "action_label": "steer",
            "prompt": prompt,
            "state": dict(self.state),
            "active_action_label": "active_turn_setup",
            "active_command": active,
            "active_state": dict(active_state),
            "settled_state": dict(settled),
            "command": command,
            "marker_row": row,
            "evidence": evidence,
        }
        if not local:
            record["served"] = self.converge("steer", marker, row)

    def abort(self, *, local: bool = False) -> None:
        f = self.f
        name = "local_abort" if local else "abort"
        before_state = self.wait_state(f.is_idle, timeout=30) if self.scripted_turn_barrier else None
        task, done, after = (self.marker(f"{name.upper()}{part}") for part in ("_TASK", "_TASK_DONE", "_AFTER"))
        active = self.begin(f"{name} turn's send", step_task_prompt(task, done), local=local)
        active_state = self.busy_state(after=before_state if f.provider == "omp" else None)
        offset = _read_source_size(self.session_file)
        boundary_wait = _wait_task_tool_call_boundary if self.scripted_model else _wait_task_tool_boundary
        self.observation[f"{name}_tool_boundary"] = boundary_wait(self.session_file, task)
        command = self.control("abort", local=local)
        record: dict[str, Any] = {
            "action_label": "abort",
            "state": dict(self.state),
            "active_action_label": "abort_turn_setup",
            "active_command": active,
            "active_state": dict(active_state),
            "command": command,
        }
        native_session_id = str(self.state.get("native_session_id") or "")
        if f.channel_terminal:
            agent_end, channel_state = _wait_channel_terminal(
                self.home,
                f,
                session_id=self.session_id,
                native_session_id=native_session_id,
                session_file=self.session_file,
                minimum_turn_seq=_turn_sequence(active_state),
            )
            record["agent_end"] = agent_end
            record["evidence"] = _channel_terminal_evidence(
                agent_end, channel_state, native_session_id=native_session_id, session_file=self.session_file
            )
        else:
            # No channel event says the turn is over: the launcher's state leaving the turn does.
            record["settled_state"] = dict(
                self.or_missing(lambda: self.wait_state(lambda value: value.get("phase") not in {"running", "thinking"}, timeout=20))
            )
        # The provider records the turn it aborted as a row of its own file, whatever else says the turn ended.
        try:
            aborted: dict[str, Any] | None = _wait_native_abort(self.session_file, minimum_offset=offset)
        except RuntimeError as exc:
            aborted = None
            self.observation[f"{name}_native_wait_error"] = f"{type(exc).__name__}: {exc}"
        record["native_abort"] = {
            "observed": aborted is not None,
            "source_path": str(self.session_file),
            "minimum_source_offset": offset,
            "native_session_id": native_session_id,
            "native_file": _native_file_identity(self.session_file),
            "aborted_row_id": aborted.get("id") if aborted else None,
            "aborted_row_source_offset": aborted.get("_source_offset") if aborted else None,
            "stop_reason": (aborted.get("message") or {}).get("stopReason") if aborted else None,
        }
        # An abort proves nothing if the session cannot take the next turn.
        self.settle()
        try:
            after_command = self.control("send", self.prompt("after_abort", after), local=local)
        except RuntimeError as exc:
            after_command = {"accepted": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            _wait_native_marker(self.session_file, after, minimum_offset=offset, timeout=150)
        except RuntimeError as exc:
            self.observation[f"after_{name}_wait_error"] = f"{type(exc).__name__}: {exc}"
        self.settle()
        self.observation[f"{name}_turn_verdict"] = {
            **abort_then_send_verdict(read_session_entries(self.session_file), task_marker=task, task_done_marker=done, after_marker=after),
            "after_send_accepted": after_command.get("accepted") is True,
        }
        record["after_command"] = after_command
        self.controls[name] = record

    def stale_frame(self, old: Mapping[str, Any], current: Mapping[str, Any]) -> None:
        """Send a command as the owner that was current before a rebind, and record what the launcher answered."""

        old_file = Path(str(old.get("session_file") or ""))
        before = _read_source_size(old_file)
        # Text of this run's own, so that a frame the launcher dispatched after all can be found in what the provider wrote.
        text = f"A stale owner must be refused: {self.marker('STALE')}"
        try:
            response = _stale_frame(old, self.f, text=text)
        except (OSError, RuntimeError) as exc:
            if not self.f.records_misses:
                raise
            response = {"error": {"code": type(exc).__name__, "message": str(exc)}}
        after = _read_source_size(old_file)
        error = response.get("error") if isinstance(response.get("error"), Mapping) else {}
        self.controls["stale_owner"] = {
            "second_launcher": self.second_launcher_evidence,
            "old_state": dict(old),
            "new_state": dict(current),
            "frame_text": text,
            "response": response,
            "accepted": response.get("ok") is True,
            "error_code": error.get("code"),
            # Whether the stale frame reached the provider: the session file it would have written to.
            "native_dispatch": {"observed": after != before, "source_offset_before": before, "source_offset_after": after},
        }

    def reload(self) -> None:
        """Reload the extension (`/reload`): the session keeps its identity under a new connection and lease."""

        old = dict(self.state)
        self.terminals[0].submit_line("/reload")
        reloaded = self.wait_state(
            lambda value: value.get("connection_id") and value.get("connection_id") != old.get("connection_id"), timeout=30
        )
        self.own("reloaded", reloaded)
        self.stale_frame(old, reloaded)
        self.state = reloaded
        identity = self.identity("reloaded", reloaded)
        marker = self.marker("RELOAD")
        offset = _read_source_size(self.session_file)
        prompt = self.prompt("send", marker)
        command = self.host("send_live", prompt)
        row, evidence = self.reply(marker, offset)
        self.controls["reload"] = {
            "action_label": "reload",
            "command": {"surface": "terminal", "text": "/reload", "submitted": True},
            "before": old,
            "state": dict(reloaded),
            "prompt": prompt,
            "send": command,
            "marker_row": row,
            "evidence": evidence,
            "control_identity": identity,
            "served": self.converge("reload", marker, row, flush=self.flush("reload")),
        }

    def replace(self, *, stale_frame: bool) -> None:
        """Start a new native session under the same Longhouse session (`/new`), and seed it with a phrase to recall."""

        f = self.f
        old = dict(self.state)
        old_native = str(old.get("native_session_id") or "")
        file_before = _native_file_identity(self.session_file)
        self.terminals[0].submit_line("/new")
        replaced = self.wait_state(
            lambda value: bool(value.get("native_session_id"))
            and value.get("native_session_id") != old_native
            and bool(value.get("session_file")),
            timeout=f.timeouts["replace"],
        )
        if stale_frame:
            self.stale_frame(old, replaced)
        self.state = replaced
        self.session_file = Path(str(replaced["session_file"]))
        marker = self.marker("REPLACEMENT")
        phrase = SCRIPTED_CONTEXT_PHRASE if self.scripted_model else self.marker("CONTEXT")
        self.context_phrase = phrase
        self.own("replacement", replaced, marker=marker, controls=("replacement", "cold_resume"))
        identity = self.identity("replacement", replaced)
        # The provider may reserve the path at `/new` and write the file with the first turn.
        offset = _read_source_size(self.session_file)
        prompt = _context_prompt(phrase, marker)
        alongside = (phrase,) if self.scripted_model else ()
        command = self.host(f.rebind_command, prompt)
        row, evidence = self.reply(marker, offset, alongside=alongside)
        seed_row, seed = self.reply(phrase, offset, role="user")
        record = self.controls["replacement"] = {
            "action_label": "replacement_send",
            "command_surface": {"surface": "terminal", "text": "/new", "submitted": True},
            "prompt": prompt,
            "before": old,
            "native_file_before": file_before,
            "state": dict(replaced),
            "command": command,
            "marker_row": row,
            "evidence": evidence,
            "context_seed_row": seed_row,
            "context_evidence": seed,
            "control_identity": identity,
        }
        record["served"] = self.converge("replacement", marker, row, flush=self.flush("replacement"), alongside=alongside)

    def _stopped(self, state: Mapping[str, Any], terminal: ProviderPtySession, *, owner: str, tolerate: bool = False) -> dict[str, Any]:
        """What proves the owner stopped after a terminate, as this lifecycle proves it today.

        ``tolerate`` is the terminate control: its fault keeps the owners alive
        on purpose, and the wait that expires is the observation the control
        exists to make.
        """

        if not self.f.terminate_proves_owners_dead:
            try:
                stopped = _wait_stopped(self.home, self.f, self.session_id)
            except RuntimeError as exc:
                if not tolerate:
                    raise
                # The launcher has outlived its deadline: there is no exit of the terminal's process left to wait for.
                return {"terminal_reason": None, "status": "owners_alive_at_deadline", "detail": str(exc)[:500]}
            # A launcher that outlives an accepted terminate is the failure this exists to find.
            with contextlib.suppress(subprocess.TimeoutExpired) if tolerate else contextlib.nullcontext():
                terminal.process.wait(timeout=15)
            return stopped
        try:
            stopped = _wait_recorded_owners_dead(self.home, self.f, self.session_id, state, owner=owner)
        except RuntimeError as exc:
            if not tolerate:
                raise
            stopped = {"status": "owners_alive_at_deadline", "terminal_reason": None, "detail": str(exc)[:500]}
        finally:
            # Judged before the terminal's own process is collected: that one is ours to close either way.
            owners_dead = all(_pid_dead(state.get(f"{part}_pid")) for part in ("launcher", "provider"))
            terminal.close()
        return {**stopped, "owners_dead": stopped.get("status") == "pass" and owners_dead}

    def terminate(self) -> None:
        f = self.f
        state = dict(self.state)
        command = self.control("terminate")
        control = self.negative_control == "terminate"
        stopped = self._stopped(state, self.terminals[0], owner="replacement", tolerate=control)
        owned = command.get("accepted") is True and (
            stopped.get("owners_dead") is True if f.terminate_proves_owners_dead else stopped.get("terminal_reason") == "remote_terminate"
        )
        self.observation["terminate_verdict"] = {
            "passed": owned,
            "accepted": command.get("accepted") is True,
            "terminal_reason": stopped.get("terminal_reason"),
            "owners_dead": stopped.get("owners_dead"),
            "code": (
                None
                if owned
                # Accepted while the owners never reached a remote-terminate end: they outlived the command.
                else "terminate_left_owners_alive"
                if command.get("accepted") is True
                else "terminate_not_accepted"
            ),
        }
        self.controls["terminate"] = {"action_label": "terminate", "state": state, "command": command, "stopped_state": stopped}
        if f.refused_command_ends_run and not owned and not control:
            raise RuntimeError(f"{f.name} Helm terminate was not proven after the recorded execution owners exited")
        # Retirement is the cleanup proof, not the terminate assertion, so it is
        # recorded after the verdict: under the terminate control the run stays
        # `running` by design and the claim never reaches retired.
        if f.retirement_claims:
            _record_retirement_claim_terminal(
                self.api_url, self.token, self.retirement_claims, session_id=self.session_id, run_id=str(state.get("run_id") or "")
            )
        # Every turn is shown served, so the terminated session is shipped before it is resumed.
        if self.flush("before-resume").get("status") != "pass":
            raise RuntimeError(f"{f.name} Helm termination was not flushed before cold resume")

    def cold_resume(self) -> None:
        """Launch again into the terminated session: the same native session and file, with what it was told still there."""

        f = self.f
        terminated = dict(self.state)
        phrase = self.context_phrase
        marker = self.marker("RESUME")
        offset = _read_source_size(self.session_file)
        file_before = _native_file_identity(self.session_file)
        # What the file held when the old owner stopped, so the boundary of the resume is bytes and not an offset alone.
        (self.root / "pre-resume-native-source.raw").write_bytes(self.session_file.read_bytes())
        prompt = _resume_prompt(f, marker)
        terminal, argv = self.start(prompt, "helm-resume-terminal.raw", resume=True)
        before_file = self.session_file.resolve()

        def fresh(value: Mapping[str, Any]) -> bool:
            if not value.get("run_id") or value.get("run_id") == terminated.get("run_id"):
                return False
            if not f.strict_resume_state:
                return True
            try:
                same_file = bool(value.get("session_file")) and Path(str(value["session_file"])).resolve() == before_file
            except OSError:
                same_file = False
            return (
                value.get("status") in {"ready", "running"}
                and value.get("native_session_id") == terminated.get("native_session_id")
                and same_file
            )

        state = self.wait_state(fresh, timeout=f.timeouts["resume"])
        self.state = dict(state)
        self.session_file = Path(str(state.get("session_file") or self.session_file))
        self.own("cold_resume", state, marker=marker, controls=("cold_resume",))
        identity = self.identity("cold_resume", self.state)
        # The resume prompt asks for the recalled phrase and then the marker.
        # The marker is the answer and may share its line with the phrase; the
        # phrase is a recall, which the model may also put in a sentence, so
        # its row is matched by presence.
        row, evidence = self.reply(marker, offset, alongside=(phrase,), timeout=f.timeouts["resume_reply"])
        record: dict[str, Any] = {
            "action_label": "cold_resume",
            "prompt": prompt,
            "command": argv,
            "before": terminated,
            "native_file_before": file_before,
            "state": dict(state),
            "context_phrase": phrase,
            "pre_resume_source_path": "pre-resume-native-source.raw",
            "pre_resume_source_offset": offset,
            "marker_row": row,
            "evidence": evidence,
            "control_identity": identity,
        }
        if f.binds_resume_prompt:
            _prompt_row, record["prompt_evidence"] = self.reply(prompt, offset, role="user", timeout=300)
        record["context_row"], record["context_evidence"] = self.reply(phrase, offset, recalled=True, timeout=f.timeouts["resume_context"])
        settled: dict[str, Any] = {}
        if f.channel_terminal:
            native_session_id = str(state.get("native_session_id") or "")
            agent_end, settled = _wait_channel_terminal(
                self.home,
                f,
                session_id=self.session_id,
                native_session_id=native_session_id,
                session_file=self.session_file,
                minimum_turn_seq=_turn_sequence(state),
            )
            record["agent_end"] = agent_end
            record["terminal_evidence"] = _channel_terminal_evidence(
                agent_end, settled, native_session_id=native_session_id, session_file=self.session_file
            )
        # Reached: what follows is serving and settlement, which other cells judge.
        self.controls["cold_resume"] = record
        if f.channel_terminal and not _resume_state_is_settled(
            settled, native_session_id=str(state.get("native_session_id") or ""), session_file=self.session_file
        ):
            settled = self.wait_state(
                lambda value: _resume_state_is_settled(
                    value, native_session_id=str(state.get("native_session_id") or ""), session_file=self.session_file
                )
            )
        record["served"] = self.converge("resume", marker, row, flush=self.flush("resume"), alongside=(phrase,))
        record["context_served"] = self.converge("resume_context", phrase, record["context_row"], recalled=True)
        final = self.wait_state(
            lambda value: (
                f.is_idle(value)
                and value.get("native_session_id") == state.get("native_session_id")
                and value.get("session_file") == str(self.session_file)
            )
        )
        self.identity("final", final)
        self.state = dict(final)
        if f.channel_terminal:
            self.observation["settlement"] = _native_settlement(
                self.session_file, channel_state=settled, native_session_id=str(state.get("native_session_id") or "")
            )
        command = self.control("terminate", local=f.local_controls)
        if f.refused_command_ends_run and command.get("accepted") is not True:
            raise RuntimeError(f"{f.name} cold-resume termination was not accepted")
        stopped = self._stopped(self.state, terminal, owner="cold_resume")
        self.controls["final_terminate"] = {
            "action_label": "final_terminate",
            "state": dict(self.state),
            "command": command,
            "stopped_state": stopped,
            "terminals_exited": all(not session.alive() for session in self.terminals),
        }
        if f.retirement_claims:
            _record_retirement_claim_terminal(
                self.api_url, self.token, self.retirement_claims, session_id=self.session_id, run_id=str(self.state.get("run_id") or "")
            )
            try:
                self.flush("terminal")
            except Exception as exc:  # noqa: BLE001 - preserve terminal-delivery evidence
                self.observation["flush"]["terminal"] = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
            self.served_run_inventory = _wait_served_run_retirement(self.api_url, self.token, self.session_id, self.retirement_claims)

    # -- the end of every run

    def fail(self, exc: Exception) -> None:
        """Keep what the run had recorded when it failed, before anything is torn down."""

        if isinstance(exc, MarkerAnswerCountError):
            self.observation["runtime_convergence_failure"] = exc.evidence()
        elif isinstance(exc, RuntimeConvergenceTimeout):
            self.observation["runtime_convergence_failure"] = {"reason": "runtime_convergence_timed_out", "observation": exc.observation}
        elif isinstance(exc, RuntimeHostError):
            self.observation["runtime_failure"] = {
                "http_status": exc.status,
                "error": str(exc),
                "last_runtime_observation": exc.last_runtime_observation,
            }
        self.write_partial(exc)
        if control_made_its_observation(self.negative_control, self.observation):
            # A no-op send or terminate was injected, so the turn never comes or
            # the owners stay alive by design, and every later step runs against
            # that. Once the typed verdict is recorded the control has made its
            # whole observation: the failure is kept as evidence and the verdict
            # is still ruled on. Aborting here left the factory with no result to
            # judge. A failure before the verdict, or under another control, is
            # still the run's failure.
            self.observation["failure_after_control_verdict"] = f"{type(exc).__name__}: {exc}"
            return
        self.failure = exc

    def write_partial(self, exc: BaseException) -> None:
        """Leave what the run had recorded when it failed as a file of its own, whatever the result goes on to keep."""

        partial = {
            "schema_version": 1,
            "status": "fail",
            "error": f"{type(exc).__name__}: {exc}",
            "observation": {**self._redacted(self.observation), "current_state": redact_state_for_evidence(self.state)},
        }
        with contextlib.suppress(Exception):
            lifecycle.write_json(self.root / "partial-observation.json", partial)

    def _redacted(self, value: Any) -> Any:
        secrets = [secret for name, secret in self.env.items() if secret and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
        return _redacted(value, secrets)

    def teardown(self) -> None:
        """Stop what is still running, prove what stopped, retain the provider's files, and remove the isolation."""

        f, observation = self.f, self.observation
        dispatch: dict[str, Any] = {"status": "pass", "dispatched": False, "skipped": True}
        # A lifecycle that claims each run's retirement terminates whatever it did not see retired; another, a run that failed.
        if self.session_id and (not self.served_run_inventory if f.retirement_claims else self.failure is not None):
            try:
                sent = self.control("terminate", local=f.local_controls)
                dispatch = {
                    "status": "pass" if sent.get("accepted") else "fail",
                    "dispatched": sent.get("accepted") is True,
                    "response": sent,
                }
            except Exception as exc:  # noqa: BLE001 - cleanup must continue and report failure
                dispatch = {"status": "fail", "dispatched": False, "error": f"{type(exc).__name__}: {exc}"}
        cleanup_errors: list[str] = []
        for terminal in self.terminals:
            try:
                terminal.close()
            except Exception as exc:  # noqa: BLE001 - cleanup evidence records the failure below
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
        cleanup = (
            _wait_cleanup_receipt(self.owner_records)
            if self.owner_records
            else {"status": "fail", "provider_process_dead": False, "process_group_dead": False, "orphan_count": 1}
        )
        self._record_scripted_model_receipt()
        run_id = str(self.state.get("run_id") or "")
        if f.retirement_claims:
            if not self.retirement_claims:
                _append_retirement_claim(self.retirement_claims, session_id=self.session_id, state=self.state)
            if self.session_id and run_id:
                try:
                    _record_retirement_claim_terminal(
                        self.api_url, self.token, self.retirement_claims, session_id=self.session_id, run_id=run_id
                    )
                except Exception as exc:  # noqa: BLE001 - cleanup evidence must fail closed
                    cleanup["terminal_evidence_error"] = f"{type(exc).__name__}: {exc}"
        inventory = self.served_run_inventory
        if not inventory:
            if not (self.session_id and run_id):
                inventory = {"retired": False, "active_run_count": None, "error": "session_or_run_identity_unavailable"}
            elif f.retirement_claims:
                inventory = _wait_served_run_retirement(self.api_url, self.token, self.session_id, self.retirement_claims)
            else:
                claim = {"session_id": self.session_id, "run_id": run_id, "state": "terminal"}
                inventory = lifecycle._wait_served_run_retirement(self.api_url, self.token, self.session_id, [claim])
        retirement = retire_qualification_session(self.api_url, self.token, self.session_id, provider=f.provider)
        shipper_stop: dict[str, Any] = {}
        if self.shipper is not None:
            try:
                self.flush("cleanup")
            except Exception as exc:  # noqa: BLE001 - cleanup evidence must remain visible
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
            try:
                shipper_stop = self.shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup evidence must remain visible
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
                shipper_stop = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        lifecycle.write_json(self.root / "transcript-shipper-receipt.json", shipper_stop or {"status": "fail", "stopped": False})
        lifecycle.write_json(self.root / "transcript-flush-receipt.json", observation["flush"])
        owners_stopped = cleanup.get("provider_process_dead") is True and cleanup.get("process_group_dead") is True
        cleanup.update(
            {
                "termination_dispatch": dispatch,
                "session_retirement": retirement,
                "served_run_inventory": inventory,
                "served_run_retired": (
                    inventory.get("retired") is True
                    and inventory.get("session_id") == self.session_id
                    and inventory.get("active_run_count") == 0
                ),
                "dispatch_session_id": self.session_id,
                "dispatch_run_id": run_id,
                "process_stop": {"verified": owners_stopped and cleanup.get("orphan_count") == 0},
                "shipper_stop": shipper_stop,
                "shipper_stop_verified": (
                    shipper_stop.get("stopped") is True
                    and shipper_stop.get("process_dead") is True
                    and shipper_stop.get("process_group_dead") is True
                ),
                "cleanup_errors": cleanup_errors,
                "isolation_path": str(self.isolation),
                "canary_session_hidden": _exact_session_retirement(retirement, self.session_id),
            }
        )
        if owners_stopped:
            try:
                retained = lifecycle._retain_claim_sources(self.root, self.source_claims, self.env, complete=True)
            except Exception as exc:  # noqa: BLE001 - preserve cleanup evidence on producer failure
                retained = [
                    {
                        "source": claim.get("source_path"),
                        "kind": "source_path",
                        "retained": False,
                        "complete": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    for claim in self.source_claims
                ]
                lifecycle.write_json(self.root / "provider-source-retention.json", {"sources": retained})
        else:
            reason = "provider owners did not stop; authoritative source remains in isolation"
            retained = [
                {"source": claim.get("source_path"), "kind": "source_path", "retained": False, "complete": False, "error": reason}
                for claim in self.source_claims
            ]
            lifecycle.write_json(self.root / "provider-source-retention.json", {"sources": retained})
        cleanup["source_retention_verified"] = bool(retained) and all(
            item.get("retained") is True
            and item.get("complete") is True
            and bool(item.get("original_sha256"))
            and bool(item.get("retained_sha256"))
            and isinstance(item.get("path"), str)
            and bool(item["path"])
            and bool(item.get("identities"))
            and all(
                all(identity.get(name) for name in ("provider", "session_id", "run_id", "native_session_id"))
                for identity in item["identities"]
            )
            for item in retained
        )
        cleanup["source_retention"] = {
            "verified": cleanup["source_retention_verified"],
            "source_count": len(retained),
            "retained_source_count": sum(item.get("retained") is True for item in retained),
        }
        runtime_cleanup_verified = (
            cleanup["process_stop"]["verified"] is True
            and cleanup["shipper_stop_verified"] is True
            and cleanup["canary_session_hidden"] is True
            and cleanup["served_run_retired"] is True
        )
        cleanup["isolation_removed"] = _remove_isolation_after_source_retention(
            self.isolation,
            source_retention_verified=cleanup["source_retention_verified"],
            runtime_cleanup_verified=runtime_cleanup_verified,
            cleanup=cleanup,
        )
        if not (
            cleanup["status"] == "pass"
            and runtime_cleanup_verified
            and not cleanup_errors
            and cleanup["source_retention_verified"]
            and cleanup["isolation_removed"]
        ):
            cleanup["status"] = "fail"
        cleanup = self._redacted(cleanup)
        observation["cleanup"] = cleanup
        lifecycle.write_json(self.root / "cleanup-receipt.json", cleanup)
        # Every record that names a place in a session file also names where that file is retained.
        retained_by_source = {
            str(item["source"]): str(item["path"])
            for item in retained
            if item.get("retained") is True and isinstance(item.get("source"), str) and isinstance(item.get("path"), str)
        }

        def name_retained(value: Any) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("source_path"), str):
                    value["retained_source_path"] = retained_by_source.get(value["source_path"])
                for child in value.values():
                    name_retained(child)
            elif isinstance(value, list):
                for child in value:
                    name_retained(child)

        for records in (self.controls, observation["sources"], observation.get("native_taxonomy")):
            name_retained(records)
        settlement = observation["settlement"]
        if settlement:
            native_source = str(settlement.get("session_file") or "")
            settlement.update(
                retained_source_path=retained_by_source.get(native_source),
                retained_source=native_source,
                retained_sources=retained_by_source,
            )
        observation["provider_source_retention"] = retained

    def result(self) -> None:
        """Put the finished observation and its verdicts on the run, or raise what failed it."""

        f, args, root, run = self.f, self.args, self.root, self.run
        cleanup = self.observation["cleanup"]
        binary = {
            "provider": f.provider,
            "path": str(args.provider_bin),
            "sha256": sha256_file(args.provider_bin),
            "version": args.provider_version,
        }
        observation = self._redacted(self.observation)
        lifecycle.write_json(root / "provider-binary-receipt.json", binary)
        lifecycle.write_json(root / "runtime-convergence-receipt.json", observation["convergence"])
        if observation["settlement"]:
            lifecycle.write_json(root / "native-settlement-receipt.json", observation["settlement"])
        # The channel this lifecycle drove and the owner it ended with; what it recorded on the way is the observation.
        lifecycle.write_json(
            root / "helm-receipt.json",
            {
                "managed_transport": f"{f.provider}_helm_channel",
                "session_id": self.session_id,
                "state": redact_state_for_evidence(self.state),
            },
        )
        lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
        assertions = f.cells(observation)
        manifest = artifact_manifest(root)
        run.fields["provider_binary"] = binary
        if f.evidence_must_be_final:
            run.fields["final_evidence"] = {
                "cleanup_ready": _helm_cleanup_ready(cleanup),
                "source_retention_verified": cleanup.get("source_retention_verified") is True,
                "isolation_removed": cleanup.get("isolation_removed") is True,
                "cleanup_receipt_written": True,
                "manifest_stable": _manifest_is_stable(root, manifest),
                "manifest_entry_count": len(manifest),
            }
        failure = self.failure
        if failure is None and self.scripted_model and observation.get("scripted_model", {}).get("status") != "pass":
            failure = RuntimeError("scripted model receipt did not prove every required Helm turn")
        if failure is None and not f.evidence_must_be_final and cleanup.get("status") != "pass":
            failure = RuntimeError(
                f"{f.name} Helm cleanup did not prove run retirement, source retention, shipper stop, and scratch removal"
            )
            # A run that failed only in its cleanup had recorded everything: keep it, as a failed step does.
            self.write_partial(failure)
        if self.negative_control and (failure is None or f.control_outlives_failure):
            verdict = negative_control_verdict(
                self.negative_control,
                provider=f.provider,
                assertions=assertions,
                observation=observation,
                fault_receipts=read_fault_receipts(root / "qa-fault-receipt.jsonl"),
                session_id=self.session_id,
            )
            run.observation, run.assertions = observation, control_run_assertions(self.negative_control, f.provider, assertions)
            cleaned_up = f.evidence_must_be_final or cleanup.get("status") == "pass"
            run.control(verdict, status="pass" if verdict["status"] == "pass" and cleaned_up else "fail")
            return
        if failure is not None:
            # A run that failed vouches for nothing, model compliance included.
            observation.pop("model_noncompliance", None)
            run.observation, run.assertions = observation, assertions
            raise failure
        run.observation, run.assertions = observation, assertions


def reach(family: Family, steps: Callable[[Lifecycle], None], run: Run, args: argparse.Namespace) -> None:
    """Run ``steps`` as one provider's lifecycle, tear it down whatever happened, and leave the result on ``run``."""

    life = Lifecycle(family, run, args)
    try:
        steps(life)
        # Set only by a lifecycle that ran to its end: a cell that needs the whole run is not reached before it.
        life.observation["completed"] = True
    except Exception as exc:  # noqa: BLE001 - what the run had reached is evidence; the failure is raised with the result
        life.fail(exc)
    finally:
        life.teardown()
    life.result()
