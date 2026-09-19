"""Turn-correlated steer and abort verdicts for Pi-family session transcripts.

Pi and OMP write one JSONL session tree: every entry has an ``id`` and a
``parentId``, and message entries carry ``message.role`` and, for assistant
messages, ``message.stopReason``. A turn that is still executing tools ends each
assistant message with ``stopReason == "toolUse"``; a finished turn ends with
``stop``.

That shape is what separates a real steer from a queued follow-up. A steer is
delivered between a tool result and the next model call, so every assistant
message between the task prompt and the steer text is still ``toolUse``. A
follow-up is delivered only after the task turn finished, so a ``stop``
assistant message sits between them. Matching a marker anywhere later in the
file, which the earlier oracles did, cannot tell the two apart.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from typing import Iterable
from typing import Mapping

_TERMINAL_STOP_REASONS = frozenset({"stop", "end_turn", "length", "error"})
_ABORT_STOP_REASONS = frozenset({"aborted", "cancelled", "canceled"})


def read_session_entries(session_file: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in session_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _message(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    message = entry.get("message") if entry.get("type") == "message" else None
    return message if isinstance(message, Mapping) else {}


def _role(entry: Mapping[str, Any]) -> str:
    return str(_message(entry).get("role") or "")


def _stop_reason(entry: Mapping[str, Any]) -> str:
    return str(_message(entry).get("stopReason") or "").strip().lower()


def _text(entry: Mapping[str, Any]) -> str:
    content = _message(entry).get("content")
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
    return "\n".join(parts)


def _tool_call_texts(entry: Mapping[str, Any]) -> list[str]:
    content = _message(entry).get("content")
    calls: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "toolCall":
                calls.append(json.dumps(item.get("arguments"), sort_keys=True, default=str))
    return calls


def _index(entries: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(entry["id"]): entry for entry in entries if isinstance(entry.get("id"), str)}


def _ancestors(entry: Mapping[str, Any], by_id: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Parent chain from the nearest parent back to the root."""

    chain: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    parent = entry.get("parentId")
    while isinstance(parent, str) and parent in by_id and parent not in seen:
        seen.add(parent)
        current = by_id[parent]
        chain.append(current)
        parent = current.get("parentId")
    return chain


def _descendants(entry: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Entries whose parent chain passes through ``entry``, in file order."""

    by_id = _index(entries)
    root = entry.get("id")
    return [candidate for candidate in entries if any(item.get("id") == root for item in _ancestors(candidate, by_id))]


def _single_user(entries: list[Mapping[str, Any]], marker: str) -> tuple[Mapping[str, Any] | None, int]:
    matches = [entry for entry in entries if _role(entry) == "user" and marker in _text(entry)]
    return (matches[0] if len(matches) == 1 else None), len(matches)


def step_task_prompt(task_marker: str, done_marker: str) -> str:
    """A turn that stays busy across several tool boundaries."""

    steps = " ".join(f"`sleep 4 && echo {task_marker}-step{index}`" for index in range(1, 7))
    return (
        f"Task {task_marker}: run each of these bash commands as its own separate tool call, one at a time, "
        f"waiting for each to finish: {steps}. After all six, reply with {done_marker}."
    )


def task_tool_boundary(entries: list[Mapping[str, Any]], task_marker: str) -> str | None:
    """Id of the first tool result inside the task turn, once one exists."""

    task, _count = _single_user(entries, task_marker)
    if task is None:
        return None
    by_id = _index(entries)
    for entry in entries:
        if _role(entry) == "toolResult" and any(item.get("id") == task.get("id") for item in _ancestors(entry, by_id)):
            return str(entry.get("id"))
    return None


def steer_turn_verdict(
    entries: list[Mapping[str, Any]],
    *,
    task_marker: str,
    steer_marker: str,
    task_done_marker: str,
) -> dict[str, Any]:
    """Did the steer land inside the task turn and change its course?"""

    by_id = _index(entries)
    task, task_count = _single_user(entries, task_marker)
    steer, steer_count = _single_user(entries, steer_marker)
    verdict: dict[str, Any] = {"task_user_rows": task_count, "steer_user_rows": steer_count}
    if task is None:
        return {**verdict, "passed": False, "code": "task_prompt_not_unique"}
    if steer is None:
        return {**verdict, "passed": False, "code": "steer_prompt_not_unique"}
    chain = _ancestors(steer, by_id)
    task_position = next((index for index, item in enumerate(chain) if item.get("id") == task.get("id")), None)
    if task_position is None:
        return {**verdict, "passed": False, "code": "steer_not_in_task_chain"}
    between = chain[:task_position]
    assistant_stops = [_stop_reason(item) for item in between if _role(item) == "assistant"]
    tool_results = sum(_role(item) == "toolResult" for item in between)
    interposed_users = sum(_role(item) == "user" for item in between)
    verdict.update(
        {
            "assistant_stop_reasons_before_steer": assistant_stops,
            "tool_results_before_steer": tool_results,
            "interposed_user_rows": interposed_users,
        }
    )
    if interposed_users:
        return {**verdict, "passed": False, "code": "steer_not_adjacent_to_task_turn"}
    if any(reason != "tooluse" for reason in assistant_stops):
        return {**verdict, "passed": False, "code": "steer_delivered_as_queued_follow_up"}
    if tool_results == 0:
        return {**verdict, "passed": False, "code": "steer_before_any_tool_boundary"}
    # The steered turn ends at the first terminal assistant message after the
    # steer. That message must answer the steer, not the original task. A later
    # turn -- OMP re-prompts when a job it backgrounded for the steer finishes --
    # is recorded but is a separate turn, not the steered one.
    after = _descendants(steer, list(entries))
    terminal = next(
        (item for item in after if _role(item) == "assistant" and _stop_reason(item) in _TERMINAL_STOP_REASONS),
        None,
    )
    later_task_done = [item for item in entries if _role(item) == "assistant" and task_done_marker in _text(item)]
    verdict.update(
        {
            "steered_turn_terminal_id": terminal.get("id") if terminal else None,
            "task_done_rows": len(later_task_done),
        }
    )
    if terminal is not None and task_done_marker in _text(terminal):
        return {**verdict, "passed": False, "code": "original_task_completed_after_steer"}
    if terminal is None or steer_marker not in _text(terminal):
        return {**verdict, "passed": False, "code": "steer_marker_not_answered_in_turn"}
    return {**verdict, "passed": True, "code": "steer_changed_active_turn"}


def abort_then_send_verdict(
    entries: list[Mapping[str, Any]],
    *,
    task_marker: str,
    task_done_marker: str,
    after_marker: str,
) -> dict[str, Any]:
    """Did the abort stop the task turn, and did the next turn complete?"""

    task, task_count = _single_user(entries, task_marker)
    verdict: dict[str, Any] = {"task_user_rows": task_count}
    if task is None:
        return {**verdict, "passed": False, "code": "task_prompt_not_unique"}
    task_turn = _descendants(task, list(entries))
    aborted = [item for item in task_turn if _role(item) == "assistant" and _stop_reason(item) in _ABORT_STOP_REASONS]
    completed = [item for item in entries if _role(item) == "assistant" and task_done_marker in _text(item)]
    verdict.update({"aborted_rows": len(aborted), "task_done_rows": len(completed)})
    if completed or not aborted:
        return {**verdict, "passed": False, "code": "abort_did_not_stop_active_turn"}
    by_id = _index(entries)
    after_user, after_count = _single_user(entries, after_marker)
    verdict["after_user_rows"] = after_count
    if after_user is None or not any(item.get("id") == aborted[0].get("id") for item in _ancestors(after_user, by_id)):
        return {**verdict, "passed": False, "code": "turn_after_abort_missing"}
    answered = [
        item
        for item in _descendants(after_user, list(entries))
        if _role(item) == "assistant" and after_marker in _text(item) and _stop_reason(item) in _TERMINAL_STOP_REASONS
    ]
    verdict["after_answer_rows"] = len(answered)
    if not answered:
        return {**verdict, "passed": False, "code": "turn_after_abort_missing"}
    return {**verdict, "passed": True, "code": "abort_stopped_turn_and_session_continued"}


def step_tool_calls(entries: list[Mapping[str, Any]], step_token: str) -> int:
    return sum(step_token in call for entry in entries if _role(entry) == "assistant" for call in _tool_call_texts(entry))


# Negative control -> (fault suffix, target assertion suffix, verdict key, expected code).
# The engine fault is `<provider>_<fault suffix>` and only exists in a build with
# the `qa-fault-injection` feature (engine/src/qa_fault.rs).
NEGATIVE_CONTROLS = {
    "steer": ("steer_as_follow_up", "helm_steer_active", "steer_turn_verdict", "steer_delivered_as_queued_follow_up"),
    "abort": ("abort_noop", "helm_abort_native", "abort_turn_verdict", "abort_did_not_stop_active_turn"),
    "send": ("send_noop", "helm_send_idle", "send_turn_verdict", "send_accepted_without_a_turn"),
    "terminate": ("terminate_noop", "helm_terminate_owned", "terminate_verdict", "terminate_left_owners_alive"),
}
# A control whose target IS one of the healthy preconditions cannot also
# require that precondition to hold; `send` drops itself from the list.
_CONTROL_PRECONDITION_SUFFIXES = {
    "send": ("helm_launch_registration",),
    # A precondition must also be SATISFIABLE under the fault, or the control is
    # inconclusive by construction -- the same defect as requiring a control's
    # own target. OMP's helm_launch_registration is not a launch check: it ANDs
    # settlement and a four-phase control identity that needs the cold_resume
    # and final receipts, and a no-op terminate keeps the session alive so that
    # phase can never run. The healthy prefix is proven instead by the
    # pre-terminate steps, which already require channel binding and native
    # evidence and are common to both Pi and OMP (2026-09-19).
    "terminate": ("helm_send_idle", "helm_follow_up_native", "helm_steer_active", "helm_abort_native"),
}
_DEFAULT_PRECONDITION_SUFFIXES = ("helm_launch_registration", "helm_send_idle")


def fault_name(provider: str, control: str) -> str:
    return f"{provider}_{NEGATIVE_CONTROLS[control][0]}"


def read_fault_receipts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    receipts: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            receipts.append(item)
    return receipts


def negative_control_verdict(
    control: str,
    *,
    provider: str,
    assertions: Mapping[str, bool],
    observation: Mapping[str, Any],
    fault_receipts: list[dict[str, Any]],
    session_id: str,
) -> dict[str, Any]:
    """A negative control passes only when the fault fired on this session, the
    healthy preconditions held, and the target assertion failed for the expected
    reason. Anything else is inconclusive, never a pass."""

    _suffix, target_suffix, verdict_key, expected_code = NEGATIVE_CONTROLS[control]
    fault = fault_name(provider, control)
    target = f"{provider}_{target_suffix}"
    fired = [item for item in fault_receipts if item.get("fault") == fault and item.get("session_id") == session_id]
    verdict = observation.get(verdict_key) if isinstance(observation.get(verdict_key), Mapping) else {}
    preconditions = all(
        assertions.get(f"{provider}_{suffix}") is True
        for suffix in _CONTROL_PRECONDITION_SUFFIXES.get(control, _DEFAULT_PRECONDITION_SUFFIXES)
    )
    rejected = assertions.get(target) is False and verdict.get("code") == expected_code
    return {
        "control": control,
        "fault": fault,
        "target_assertion": target,
        "fault_fired": bool(fired),
        "preconditions_held": preconditions,
        "target_rejected": rejected,
        "observed_code": verdict.get("code"),
        "expected_code": expected_code,
        "status": "pass" if fired and preconditions and rejected else "inconclusive",
    }
