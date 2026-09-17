#!/usr/bin/env python3
"""Live Claude Helm lifecycle proof through the stock Longhouse facade.

Launches stock ``claude`` through ``longhouse claude`` and drives every control
through the Runtime Host, the path a user's client takes: idle send, active-turn
steer, abort, and terminate. The oracles below judge Claude's own transcript
turn boundaries (``system/turn_duration``), not the requests Longhouse sent.

``--negative-control claude_steer_after_turn`` or ``claude_interrupt_noop`` runs
the same scenario against a Machine Agent built with the ``qa-fault-injection``
feature. The steer fault delivers the steer only after the active turn has
ended, the shape of a queued follow-up; the interrupt fault reports success
without stopping anything. The targeted oracle must then fail with a typed code
and a receipt must show the fault fired; anything else is inconclusive or a
broken oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from zerg.qa.claude_live_session_support import ScenarioError
from zerg.qa.claude_live_session_support import api_json
from zerg.qa.claude_live_session_support import api_json_tolerant
from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import claude_launch_environment
from zerg.qa.claude_live_session_support import close_session
from zerg.qa.claude_live_session_support import failed_session_close_receipt
from zerg.qa.claude_live_session_support import isolation_paths
from zerg.qa.claude_live_session_support import launch_claude_session
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import scanner_visible_claude_binary
from zerg.qa.claude_live_session_support import start_machine_and_shipper
from zerg.qa.claude_live_session_support import wait_until
from zerg.qa.claude_live_session_support import write_claude_cleanup_aggregate
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import prepare_claude_profile
from zerg.qa.live_session_toolkit import qualification_secrets
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import secret_scan
from zerg.qa.live_session_toolkit import wait_pid_dead
from zerg.qa.managed_claude_live import transcript_lookup_id
from zerg.qa.managed_claude_live import transcript_paths
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SCENARIO_ID = "claude_helm_lifecycle"
_ARTIFACT_KIND = "claude_helm_lifecycle_result"
NEGATIVE_CONTROLS = {
    # fault -> the assertion it must break, and the failure codes that count as caught
    "claude_steer_after_turn": ("claude_helm_steer_active", frozenset({"steer_delivered_as_followup", "steer_did_not_change_course"})),
    "claude_interrupt_noop": ("claude_helm_abort_native", frozenset({"abort_did_not_stop_turn"})),
}

ASSERTIONS = (
    "claude_helm_launch_registration",
    "claude_helm_send_idle",
    "claude_helm_steer_active",
    "claude_helm_abort_native",
    "claude_helm_terminate_owned",
)
_VARIANTS = tuple(
    execution_variant_key(provider="claude", assertion_id=item, scenario_id=_SCENARIO_ID, variant=None) for item in ASSERTIONS
)

REGISTRATION = ProducerRegistration(
    producer_id="claude.helm_lifecycle.v1",
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=2,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("claude",),
    # Claude on macOS keeps credentials in the desktop Keychain; a relocated
    # HOME there raises Keychain dialogs. The factory qualifies Linux.
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "stock_longhouse_claude_helm_launch",
        "remote_send_reply_archived",
        "steer_landed_in_target_turn",
        "aborted_turn_followed_by_completed_turn",
        "remote_terminate_provider_dead",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "session_launch_receipt",
        "lifecycle_report",
        "session_close_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_helm_lifecycle.py",
    oracle_source="server/zerg/qa/claude_helm_lifecycle.py",
    oracle_entrypoint="steer_landed_in_turn",
    executable_module="zerg.qa.claude_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)


# --- Oracles over Claude's native transcript rows ---------------------------


def _assistant_texts(rows: list[dict[str, Any]]) -> list[str]:
    texts: list[str] = []
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if row.get("type") != "assistant" or not isinstance(message.get("content"), list):
            continue
        texts.extend(
            str(block.get("text") or "") for block in message["content"] if isinstance(block, dict) and block.get("type") == "text"
        )
    return texts


def _bash_commands(rows: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    for row in rows:
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if row.get("type") != "assistant" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                commands.append(str(block["input"].get("command") or ""))
    return commands


def _user_prompt_contains(row: dict[str, Any], marker: str) -> bool:
    if row.get("type") == "user":
        content = (row.get("message") or {}).get("content") if isinstance(row.get("message"), dict) else None
        return isinstance(content, str) and marker in content
    attachment = row.get("attachment") if isinstance(row.get("attachment"), dict) else {}
    return row.get("type") == "attachment" and attachment.get("type") == "queued_command" and marker in str(attachment.get("prompt") or "")


def _turn_end(row: dict[str, Any]) -> bool:
    return row.get("type") == "system" and row.get("subtype") == "turn_duration"


def _turn_bounds(rows: list[dict[str, Any]], prompt_marker: str) -> tuple[int, int | None] | None:
    start = next((index for index, row in enumerate(rows) if _user_prompt_contains(row, prompt_marker)), None)
    if start is None:
        return None
    end = next((index for index in range(start + 1, len(rows)) if _turn_end(rows[index])), None)
    return start, end


def steer_landed_in_turn(
    rows: list[dict[str, Any]],
    *,
    prompt_marker: str,
    steer_marker: str,
    steered_marker: str,
    done_marker: str,
    later_step_command: str,
) -> dict[str, Any]:
    """Did a steer change the course of the turn it was aimed at?

    A queued follow-up is the shape this must reject: Claude finishes the
    original turn as asked, then answers the steer text in a new turn. Seeing
    the steered marker somewhere later proves nothing.
    """

    bounds = _turn_bounds(rows, prompt_marker)
    if bounds is None:
        return {"passed": False, "failure_code": "steer_target_prompt_missing"}
    start, end = bounds
    if end is None:
        return {"passed": False, "failure_code": "steer_target_turn_never_completed"}
    turn, after = rows[start : end + 1], rows[end + 1 :]
    steer_in_turn = any(_user_prompt_contains(row, steer_marker) for row in turn[1:])
    steered_here = any(steered_marker in text for text in _assistant_texts(turn))
    later_step_ran = any(later_step_command in command for command in _bash_commands(turn))
    finished_original = any(done_marker in text for text in _assistant_texts(turn))
    steered_elsewhere = any(_user_prompt_contains(row, steer_marker) for row in after) or any(
        steered_marker in text for text in _assistant_texts(after)
    )
    if steer_in_turn and steered_here and not later_step_ran and not finished_original:
        failure = None
    elif steered_elsewhere and not steer_in_turn:
        failure = "steer_delivered_as_followup"
    elif later_step_ran or finished_original:
        failure = "steer_did_not_change_course"
    else:
        failure = "steer_marker_missing"
    return {
        "passed": failure is None,
        "failure_code": failure,
        "steer_delivered_in_target_turn": steer_in_turn,
        "steered_in_target_turn": steered_here,
        "steered_in_later_turn": steered_elsewhere,
        "later_step_ran_in_target_turn": later_step_ran,
        "original_task_finished": finished_original,
    }


def abort_stopped_turn(
    rows: list[dict[str, Any]],
    *,
    prompt_marker: str,
    forbidden_marker: str,
    interrupted_at: float,
    tool_seconds: float,
    recovery_marker: str | None = None,
) -> dict[str, Any]:
    """Did the interrupt end the active turn early, without its promised reply?

    The turn's long tool runs for ``tool_seconds``. A no-op interrupt lets it
    finish and the model then produces ``forbidden_marker``; a real one ends the
    turn well before the tool could have completed.

    With ``recovery_marker`` the verdict also requires the session to survive
    the interrupt: a later turn prompted with that marker must complete and
    answer with it. An interrupt that stops the turn by breaking the session
    is not an abort.
    """

    bounds = _turn_bounds(rows, prompt_marker)
    if bounds is None:
        return {"passed": False, "failure_code": "abort_target_prompt_missing"}
    start, end = bounds
    if end is None:
        return {"passed": False, "failure_code": "abort_did_not_stop_turn", "turn_completed": False}
    turn = rows[start : end + 1]
    forbidden = any(forbidden_marker in text for text in _assistant_texts(turn))
    ended_at = _timestamp(rows[end])
    stop_latency = None if ended_at is None else round(ended_at - interrupted_at, 3)
    early = stop_latency is not None and stop_latency < tool_seconds / 2
    # A killed tool alone is not a stop: the model can read the failure and
    # carry on with more tools. Nothing may execute after the interrupt.
    tools_after = sum(
        1 for row in turn if (stamp := _timestamp(row)) is not None and stamp > interrupted_at and _successful_tool_result(row)
    )
    failure = None if (not forbidden and early and tools_after == 0) else "abort_did_not_stop_turn"
    following_completed = None
    if recovery_marker is not None:
        later = rows[end + 1 :]
        recovery = _turn_bounds(later, recovery_marker)
        following_completed = bool(
            recovery is not None
            and recovery[1] is not None
            and any(recovery_marker in text for text in _assistant_texts(later[recovery[0] : recovery[1] + 1]))
        )
        if failure is None and not following_completed:
            failure = "abort_following_turn_missing"
    return {
        "passed": failure is None,
        "failure_code": failure,
        "forbidden_reply_produced": forbidden,
        "turn_stop_latency_seconds": stop_latency,
        "stopped_before_tool_could_finish": early,
        "tools_executed_after_interrupt": tools_after,
        "following_turn_completed": following_completed,
    }


def _successful_tool_result(row: dict[str, Any]) -> bool:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    content = message.get("content")
    return (
        row.get("type") == "user"
        and isinstance(content, list)
        and any(isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error") is not True for block in content)
    )


def _timestamp(row: dict[str, Any]) -> float | None:
    raw = row.get("timestamp")
    if not isinstance(raw, str):
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def lifecycle_assertions(lifecycle: dict[str, Any], *, completed: bool, cleanup_ok: bool) -> dict[str, bool]:
    def held(key: str) -> bool:
        value = lifecycle.get(key)
        return isinstance(value, dict) and value.get("passed") is True

    return {
        "claude_helm_launch_registration": held("launch_registration"),
        "claude_helm_send_idle": held("send_idle"),
        "claude_helm_steer_active": completed and held("steer_active"),
        "claude_helm_abort_native": completed and held("abort_native"),
        "claude_helm_terminate_owned": completed and held("terminate_owned") and cleanup_ok,
    }


def negative_control_verdict(lifecycle: dict[str, Any], *, fault: str, fault_receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Pass only when the injected fault fired and its target oracle caught it."""

    target, caught_codes = NEGATIVE_CONTROLS[fault]
    key = "steer_active" if target == "claude_helm_steer_active" else "abort_native"
    observed = lifecycle.get(key) if isinstance(lifecycle.get(key), dict) else {}
    fault_fired = isinstance(fault_receipt, dict) and fault_receipt.get("fault") == fault
    preconditions = ["launch_registration", "send_idle"] + (["steer_active"] if key == "abort_native" else [])
    preconditions_held = all(isinstance(lifecycle.get(item), dict) and lifecycle[item].get("passed") is True for item in preconditions)
    if not fault_fired or not preconditions_held or not observed:
        status = "inconclusive"
    elif observed.get("passed") is False and observed.get("failure_code") in caught_codes:
        status = "pass"
    else:
        status = "fail"
    return {
        "status": status,
        "fault": fault,
        "fault_fired": fault_fired,
        "preconditions_held": preconditions_held,
        "target_assertion": target,
        "target_failure_code": observed.get("failure_code"),
    }


# --- Live scenario ------------------------------------------------------------


def _transcript_rows(lookup_id: str, home: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in transcript_paths(lookup_id, home=home):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _channel_state(home: Path, session_id: str) -> dict[str, Any]:
    path = home / ".claude" / "channels" / "longhouse" / "sessions" / f"{session_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fault_receipt(home: Path, session_id: str) -> dict[str, Any] | None:
    path = home / ".claude" / "channels" / "longhouse" / "sessions" / f"{session_id}.qa-fault.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _served_state(api_url: str, token: str, session_id: str) -> dict[str, Any]:
    payload = api_json_tolerant(api_url, token, f"sessions/{session_id}/state-diagnostics")
    shadow = payload.get("shadow") if isinstance(payload, dict) and payload.get("served_path") == "canonical_session_detail" else None
    return shadow if isinstance(shadow, dict) else {}


def _action_available(state: dict[str, Any], action: str) -> bool:
    actions = (state.get("control") or {}).get("actions") if isinstance(state.get("control"), dict) else None
    entry = actions.get(action) if isinstance(actions, dict) else None
    return isinstance(entry, dict) and entry.get("state") == "available"


def _hosted_assistant_texts(api_url: str, token: str, session_id: str) -> list[str]:
    payload = api_json_tolerant(api_url, token, f"sessions/{session_id}/events?context_mode=forensic&branch_mode=head&limit=100")
    events = payload.get("events") if isinstance(payload, dict) else None
    return [str(row.get("content_text") or "") for row in events or [] if isinstance(row, dict) and row.get("role") == "assistant"]


def _post(api_url: str, token: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 20
    while True:
        try:
            return api_json(api_url, token, path, method="POST", json_body=body or {})
        except Exception as exc:  # noqa: BLE001 - retry only the documented session lock
            if "session_locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def _slow_echo(seconds: int, marker: str) -> str:
    return f"python3 -c \"import select; select.select([], [], [], {seconds}); print('{marker}')\""


def _drive_lifecycle(
    args: argparse.Namespace,
    *,
    session: Any,
    session_id: str,
    lookup_id: str,
    home: Path,
    fault: str | None,
    lifecycle: dict[str, Any],
) -> None:
    api, token = args.api_url, args.agents_token

    def rows() -> list[dict[str, Any]]:
        return _transcript_rows(lookup_id, home)

    def wait_turn_end(prompt_marker: str, description: str, timeout: float) -> None:
        wait_until(
            lambda: (bounds := _turn_bounds(rows(), prompt_marker)) is not None and bounds[1] is not None,
            timeout=timeout,
            description=description,
        )

    def wait_can_send() -> None:
        wait_until(
            lambda: _action_available(_served_state(api, token, session_id), "send_input"),
            timeout=args.response_timeout_secs,
            description="Claude live-control lease on Runtime Host",
        )

    served = api_json_tolerant(api, token, f"sessions/{session_id}") or {}
    state = _channel_state(home, session_id)
    wait_can_send()
    lifecycle["launch_registration"] = {
        "passed": bool(state.get("ready")) and bool(state.get("provider_session_id")) and bool(served),
        "channel_ready": bool(state.get("ready")),
        "native_binding_claimed": bool(state.get("provider_session_id")),
        "served_session_registered": bool(served),
        "send_input_available": True,
    }

    token_hex = uuid.uuid4().hex[:10]
    reply = f"LONGHOUSE_CLAUDE_SEND_{token_hex}"
    _post(api, token, f"sessions/{session_id}/send-live", {"message": f"Reply with exactly {reply}"})
    wait_until(
        lambda: any(reply in text for text in _hosted_assistant_texts(api, token, session_id)),
        timeout=args.response_timeout_secs,
        description="remote Claude reply in hosted archive",
    )
    wait_turn_end(f"Reply with exactly {reply}", "idle send turn completing", args.response_timeout_secs)
    lifecycle["send_idle"] = {"passed": True, "remote_reply_archived": True}

    # Steer: aim at a turn mid-way through three slow sequential Bash steps.
    step = f"lh_claude_step_{token_hex}"
    steered = f"LONGHOUSE_CLAUDE_STEERED_{token_hex}"
    done = f"LONGHOUSE_CLAUDE_UNSTEERED_{token_hex}"
    steer_text = f"Stop the remaining steps now. Do not run any more commands. Reply with exactly {steered}"
    wait_can_send()
    _post(
        api,
        token,
        f"sessions/{session_id}/send-live",
        {
            # Claude's own Bash guidance refuses idle `sleep`, so each step is
            # a bounded wait that reads as work: an 8-second select() then echo.
            "message": "Run these three Bash commands one at a time, each as its own separate foreground Bash tool call, "
            f"waiting for each to finish: `{_slow_echo(8, step + '_1')}`, then `{_slow_echo(8, step + '_2')}`, then "
            f"`{_slow_echo(8, step + '_3')}`. After all three, reply with exactly {done}"
        },
    )
    wait_until(
        lambda: any(f"{step}_1" in command for command in _bash_commands(rows())),
        timeout=args.response_timeout_secs,
        description="first slow Claude Bash step",
    )
    time.sleep(1.0)
    _post(api, token, f"sessions/{session_id}/input", {"text": steer_text, "intent": "steer", "client_request_id": uuid.uuid4().hex})
    wait_turn_end(f"{step}_1", "steered Claude turn completing", args.response_timeout_secs)
    if fault == "claude_steer_after_turn":
        # Let the delayed follow-up land and be answered before judging.
        delay = float(os.environ.get("LH_QA_FAULT_DELAY_SECS") or 75)
        wait_until(
            lambda: any(steered in text for text in _assistant_texts(rows())),
            timeout=delay + args.response_timeout_secs,
            description="delayed Claude follow-up answer",
        )
    else:
        time.sleep(5.0)
    steer_verdict = steer_landed_in_turn(
        rows(),
        prompt_marker=f"{step}_1",
        steer_marker=steered,
        steered_marker=steered,
        done_marker=done,
        later_step_command=f"{step}_3",
    )
    steer_verdict["qa_fault_receipt"] = _fault_receipt(home, session_id) if fault == "claude_steer_after_turn" else None
    lifecycle["steer_active"] = steer_verdict
    if fault == "claude_steer_after_turn":
        return
    if not steer_verdict["passed"]:
        raise ScenarioError(f"Claude steer oracle failed: {steer_verdict}")

    # Abort: stop an active turn whose foreground tool runs for tool_seconds,
    # then prove the surviving session completes a following turn.
    tool_seconds = 45.0
    forbidden = f"LONGHOUSE_CLAUDE_FORBIDDEN_{token_hex}"
    abort_prompt = f"lh_claude_progress_{token_hex}"
    wait_can_send()
    _post(
        api,
        token,
        f"sessions/{session_id}/send-live",
        {
            "message": "This is a Longhouse interrupt qualification. Use one foreground Bash tool call (not background) "
            f"to run exactly: `{_slow_echo(int(tool_seconds), abort_prompt)}`. "
            f"When it finishes, reply with exactly {forbidden}"
        },
    )
    wait_until(
        lambda: any(abort_prompt in command for command in _bash_commands(rows())),
        timeout=args.response_timeout_secs,
        description="active Claude tool turn to abort",
    )
    time.sleep(3.0)
    interrupted_at = time.time()
    _post(api, token, f"sessions/{session_id}/interrupt-live")
    wait_turn_end(abort_prompt, "interrupted Claude turn ending", tool_seconds + args.response_timeout_secs)
    time.sleep(2.0)
    abort_verdict = abort_stopped_turn(
        rows(),
        prompt_marker=abort_prompt,
        forbidden_marker=forbidden,
        interrupted_at=interrupted_at,
        tool_seconds=tool_seconds,
    )
    abort_verdict["session_alive_after_abort"] = session.alive()
    abort_verdict["qa_fault_receipt"] = _fault_receipt(home, session_id) if fault == "claude_interrupt_noop" else None
    lifecycle["abort_native"] = abort_verdict
    if fault == "claude_interrupt_noop":
        return
    if not abort_verdict["passed"] or not session.alive():
        abort_verdict["passed"] = False
        raise ScenarioError(f"Claude abort oracle failed: {abort_verdict}")
    recovery = f"LONGHOUSE_CLAUDE_RECOVERED_{token_hex}"
    # The named abort verdict is not a pass until the surviving session has
    # completed a following turn; record it failed until that is observed.
    abort_verdict["passed"] = False
    abort_verdict["failure_code"] = "abort_following_turn_missing"
    wait_can_send()
    _post(api, token, f"sessions/{session_id}/send-live", {"message": f"Reply with exactly {recovery}"})
    wait_until(
        lambda: any(recovery in text for text in _hosted_assistant_texts(api, token, session_id)),
        timeout=args.response_timeout_secs,
        description="post-abort Claude turn in hosted archive",
    )
    wait_turn_end(f"Reply with exactly {recovery}", "recovery turn completing", args.response_timeout_secs)
    final_abort = abort_stopped_turn(
        rows(),
        prompt_marker=abort_prompt,
        forbidden_marker=forbidden,
        interrupted_at=interrupted_at,
        tool_seconds=tool_seconds,
        recovery_marker=recovery,
    )
    abort_verdict.update(final_abort)
    if not final_abort["passed"]:
        raise ScenarioError(f"Claude abort oracle failed after recovery: {abort_verdict}")

    # Terminate through the Runtime Host, then prove the owned provider process
    # is gone and the served run ended.
    claude_pid = int(_channel_state(home, session_id).get("claude_pid") or 0)
    _post(api, token, f"sessions/{session_id}/terminate-live")
    provider_dead = claude_pid > 1 and wait_pid_dead(claude_pid, timeout=20)
    launcher_exited = wait_until(lambda: not session.alive() or None, timeout=20, description="longhouse claude launcher exit") is not None
    ended = wait_until(
        lambda: ((_served_state(api, token, session_id).get("run") or {}).get("lifecycle") == "ended") or None,
        timeout=args.response_timeout_secs,
        description="Claude run reaching ended after remote terminate",
    )
    lifecycle["terminate_owned"] = {
        "passed": bool(provider_dead and launcher_exited and ended),
        "provider_process_dead": provider_dead,
        "launcher_exited": launcher_exited,
        "run_lifecycle": "ended" if ended else None,
    }


def run_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-claude-helm-", dir="/tmp"))
    workspace = isolation_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    fault = args.negative_control
    saved = {name: os.environ.get(name) for name in ("LONGHOUSE_CLAUDE_BIN", "LH_QA_FAULT", "PATH")}
    shipper = None
    session = None
    session_id: str | None = None
    lifecycle: dict[str, Any] = {}
    error: str | None = None
    close_receipt: dict[str, Any] = {"not_started": True, "alive_after_close": False}
    artifact_kind = "negative_control_result" if fault else _ARTIFACT_KIND
    try:
        home, longhouse_home = isolation_paths(isolation_root)
        # The Machine Agent grants control from the provider binary it can see
        # when its channel starts, and executes the faulted control commands.
        os.environ["LONGHOUSE_CLAUDE_BIN"] = str(scanner_visible_claude_binary(args.provider_bin, longhouse_home=longhouse_home))
        if args.longhouse_cli is not None:
            os.environ["PATH"] = f"{args.longhouse_cli.resolve().parent}{os.pathsep}{os.environ.get('PATH', '')}"
        if fault:
            os.environ["LH_QA_FAULT"] = fault
        else:
            os.environ.pop("LH_QA_FAULT", None)
        shipper, environment = start_machine_and_shipper(args, isolation_root=isolation_root, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        onboarding = prepare_claude_profile(
            binary=args.provider_bin,
            home=home,
            workspace=workspace,
            environment=environment,
            recording=root / "claude-onboarding.tty",
        )
        write_json(root / "claude-onboarding-receipt.json", onboarding)
        launch_env = claude_launch_environment(
            environment,
            claude_bin=args.provider_bin,
            engine=args.engine,
            model=args.model,
            longhouse_home=longhouse_home,
        )
        session, session_id, provider_session_id = launch_claude_session(
            workspace=workspace,
            project=args.project,
            name="Longhouse Helm lifecycle qualification",
            env=launch_env,
            terminal_path=root / "terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        write_json(root / "session-launch-receipt.json", {"session_id": session_id, "workspace": str(workspace)})
        try:
            _drive_lifecycle(
                args,
                session=session,
                session_id=session_id,
                lookup_id=transcript_lookup_id(session_id, provider_session_id),
                home=home,
                fault=fault,
                lifecycle=lifecycle,
            )
        except Exception as exc:  # noqa: BLE001 - the report keeps partial observations
            error = f"{type(exc).__name__}: {exc}"
        finally:
            write_json(root / "lifecycle-report.json", {"lifecycle": lifecycle, "error": error, "negative_control": fault})
            rows_path = root / "claude-transcript-rows.json"
            write_json(rows_path, _transcript_rows(transcript_lookup_id(session_id, provider_session_id), home))
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        error = error or f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None:
            try:
                close_receipt = close_session(session)
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error
                close_receipt = failed_session_close_receipt(session, cleanup_exc)
        write_json(root / "session-close-receipt.json", close_receipt)
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error
                close_receipt["shipper_stop_error"] = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    cleanup_ok = close_receipt.get("alive_after_close") is False and "shipper_stop_error" not in close_receipt
    write_claude_cleanup_aggregate(
        root,
        producer_id=REGISTRATION.producer_id,
        required_cleanup=REGISTRATION.required_cleanup,
        outcomes={"claude_helm_process_exited": cleanup_ok},
        diagnostics={"session": close_receipt},
    )
    redacted = secret_scan(root, list(qualification_secrets(dict(os.environ), args.agents_token)))
    shutil.rmtree(isolation_root, ignore_errors=True)
    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": artifact_kind,
        "producer": REGISTRATION.to_dict(),
        "provider": "claude",
        "variant": None,
        "scenario_id": REGISTRATION.scenario_id,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now_iso(),
        "session_id": session_id,
        "observation": {"lifecycle": lifecycle, "error": error, "claude_helm_process_exited": cleanup_ok},
        "redacted_secret_files": redacted,
    }
    if fault:
        target, _codes = NEGATIVE_CONTROLS[fault]
        observed = lifecycle.get("steer_active" if target == "claude_helm_steer_active" else "abort_native") or {}
        verdict = negative_control_verdict(lifecycle, fault=fault, fault_receipt=observed.get("qa_fault_receipt"))
        result.update({"status": verdict["status"], "negative_control": verdict})
    else:
        assertions = lifecycle_assertions(lifecycle, completed=error is None, cleanup_ok=cleanup_ok)
        result.update({"status": "pass" if all(assertions.values()) else "fail", "assertions": assertions})
        if result["status"] == "fail":
            result["failure_code"] = "claude_helm_lifecycle_failed"
    result["artifact_manifest"] = artifact_manifest(root)
    write_json(root / "result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS, provider_bin_aliases=("--claude-bin",))
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"))
    parser.add_argument("--launch-timeout-secs", type=float, default=90.0)
    parser.add_argument("--response-timeout-secs", type=float, default=150.0)
    parser.add_argument("--negative-control", choices=tuple(NEGATIVE_CONTROLS), default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    for required in ("evidence_root", "repo_root", "engine", "provider_bin"):
        if getattr(args, required) is None:
            print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{required.replace('_', '-')}"}))
            return 2
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    require_disposable_runtime(args.api_url)
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    for path, code in ((args.engine, "longhouse_engine_missing"), (args.provider_bin, "claude_binary_missing")):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": code}))
            return 2
    result = run_lifecycle(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
