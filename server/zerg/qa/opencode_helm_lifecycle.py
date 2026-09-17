#!/usr/bin/env python3
"""Live OpenCode Helm lifecycle proof through the Runtime Host control path.

The provider is launched by the stock ``longhouse opencode`` facade, and every
control assertion is dispatched the way a user's client dispatches it:
``POST /api/agents/sessions/{id}/input`` (auto and steer intent),
``interrupt-live`` and ``terminate-live`` on the Runtime Host, relayed over the
Machine Agent's control channel to the engine's OpenCode server bridge. The
provider's own message store, read from its localhost server, is the
independent observation source.

Oracles are written against the shapes a wrong implementation would produce:

- ``opencode_helm_steer_active`` requires the steer to change the course of the
  *original* turn: the session never goes idle between the task and the steer
  answer, no assistant message of the original prompt finishes with ``stop``,
  the task's completion marker and its later tool calls never appear, and the
  steer answer is parented to the steer message. A steer delivered as a queued
  follow-up completes the original turn first and fails this oracle.
- ``opencode_helm_abort_native`` requires the active turn to stop without
  completing, the session to return idle, and a following send on the same
  owner to complete a new turn. An abort that silently does nothing lets the
  task finish and fails.

``--negative-control`` runs the scenario against an engine built with the
``qa-fault-injection`` feature. The engine substitutes the named wrong behavior
at its real dispatch boundary and writes a receipt; the producer passes only if
the fault fired, the healthy preconditions held, and the target assertion
failed with its typed failure code.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import uuid
from pathlib import Path
from typing import Any
from urllib.request import Request
from urllib.request import urlopen

from zerg.qa import live_session_toolkit
from zerg.qa import provider_console_lifecycle as console_lifecycle
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "opencode_helm_lifecycle"
ASSERTIONS = (
    "opencode_helm_launch_registration",
    "opencode_helm_send_idle",
    "opencode_helm_steer_active",
    "opencode_helm_abort_native",
    "opencode_helm_terminate_owned",
)
_VARIANTS = tuple(
    execution_variant_key(provider="opencode", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS
)
NEGATIVE_CONTROLS = {
    # engine fault name -> (target assertion, typed failure code it must produce)
    "opencode_steer_as_queued_follow_up": ("opencode_helm_steer_active", "steer_delivered_as_queued_follow_up"),
    "opencode_abort_noop": ("opencode_helm_abort_native", "abort_did_not_stop_active_turn"),
}
_TASK_STEPS = 5
_STEP_SLEEP_SECS = 4

SPEC = live_session_toolkit.ProviderSpec(
    provider="opencode",
    producer_id="opencode.helm_lifecycle.v1",
    executable_module="zerg.qa.opencode_helm_lifecycle",
    binary_flag="--opencode-bin",
    resume_flag="--resume-session",
    credential_binding_id="opencode_provider_token",
    state_patterns=(
        ".claude/managed-local/opencode-server/*.json",
        "managed-local/opencode-server/*.json",
        ".longhouse/managed-local/opencode/bridge/sessions/*.json",
        "managed-local/opencode/bridge/sessions/*.json",
    ),
)

REGISTRATION = ProducerRegistration(
    producer_id="opencode.helm_lifecycle.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),  # type: ignore[arg-type]
    providers=("opencode",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "stock_longhouse_opencode_helm_launch",
        "runtime_host_input_auto_delivered",
        "runtime_host_input_steer_changed_active_turn",
        "runtime_host_interrupt_stopped_active_turn",
        "post_abort_turn_completed",
        "runtime_host_terminate_owned_processes_dead",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("opencode_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "helm_registration_receipt",
        "control_command_receipts",
        "native_message_receipts",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "provider_process_dead",
        "process_group_dead",
        "no_orphan_provider_processes",
        "canary_session_hidden",
        "served_run_retired",
    ),
    implementation="server/zerg/qa/opencode_helm_lifecycle.py",
    oracle_source="server/zerg/qa/opencode_helm_lifecycle.py",
    oracle_entrypoint="opencode_helm_lifecycle_assertions",
    executable_module="zerg.qa.opencode_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)

_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"


def _kill_session_processes(identities: list[str]) -> list[dict[str, Any]]:
    needles = [value for value in identities if len(value) >= 8]
    if not needles:
        return []
    listing = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=10, check=False).stdout
    orphans = []
    for line in listing.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if not pid_text.isdigit() or int(pid_text) == os.getpid() or not any(needle in command for needle in needles):
            continue
        try:
            os.kill(int(pid_text), signal.SIGKILL)
        except ProcessLookupError:
            continue
        orphans.append({"pid": int(pid_text), "command": command[:300]})
    return orphans


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _marker(label: str) -> str:
    return f"LH_OPENCODE_{label}_{uuid.uuid4().hex[:10].upper()}"


def _wait(predicate, *, timeout: float, description: str, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {description}")


# --- Runtime Host (the product control path) --------------------------------


def _runtime_post(url: str, token: str, path: str, payload: dict[str, Any] | None, *, timeout: float = 60) -> dict[str, Any]:
    request = Request(
        f"{url.rstrip('/')}/api/agents/sessions/{path}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Agents-Token": token, "User-Agent": _RUNTIME_HOST_USER_AGENT},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
            return {"http_status": response.status, "body": body, "accepted": True, "elapsed_s": round(time.monotonic() - started, 2)}
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        return {"http_status": exc.code, "detail": detail[:1000], "accepted": False, "elapsed_s": round(time.monotonic() - started, 2)}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {
            "http_status": None,
            "detail": f"{type(exc).__name__}: {exc}",
            "accepted": False,
            "elapsed_s": round(time.monotonic() - started, 2),
        }


def _runtime_input(url: str, token: str, session_id: str, text: str, *, intent: str, timeout: float = 90) -> dict[str, Any]:
    """Deliver input, retrying only while live control is not yet attached."""

    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        receipt = _runtime_post(url, token, f"{session_id}/input", {"text": text, "intent": intent, "client_request_id": uuid.uuid4().hex})
        receipt["attempts"] = attempts
        receipt["intent"] = intent
        # Never retry a steer: a rejected-but-delivered steer retried would land
        # twice, which is its own wrong shape rather than the one under test.
        if receipt["accepted"] or intent == "steer" or receipt["http_status"] not in {409, 503} or time.monotonic() >= deadline:
            return receipt
        time.sleep(1.0)


# --- OpenCode's own message store (the independent observation) -------------


def _opencode_get(state: dict[str, Any], path: str, *, directory: bool = True) -> Any:
    url = f"{str(state['server_url']).rstrip('/')}{path}"
    cwd = str(state.get("cwd") or "")
    if directory and cwd:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode({'directory': cwd})}"
    credentials = base64.b64encode(f"{state.get('username') or 'opencode'}:{state.get('password') or ''}".encode()).decode()
    request = Request(url, headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8") or "null")


def _session_busy(state: dict[str, Any]) -> bool:
    statuses = _opencode_get(state, "/session/status")
    entry = statuses.get(state["provider_session_id"]) if isinstance(statuses, dict) else None
    return isinstance(entry, dict) and entry.get("type") not in {None, "idle"}


def _messages(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = _opencode_get(state, f"/session/{state['provider_session_id']}/message")
    return value if isinstance(value, list) else []


def _text(message: dict[str, Any]) -> str:
    return "\n".join(str(part.get("text") or "") for part in message.get("parts") or [] if part.get("type") == "text")


def _tool_commands(message: dict[str, Any]) -> list[str]:
    commands = []
    for part in message.get("parts") or []:
        if part.get("type") != "tool":
            continue
        tool_state = part.get("state") if isinstance(part.get("state"), dict) else {}
        tool_input = tool_state.get("input") if isinstance(tool_state.get("input"), dict) else {}
        commands.append(str(tool_input.get("command") or tool_input.get("filePath") or json.dumps(tool_input)[:200]))
    return commands


def _summarize(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for message in messages:
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        error = info.get("error") if isinstance(info.get("error"), dict) else None
        rows.append(
            {
                "id": info.get("id"),
                "role": info.get("role"),
                "parent_id": info.get("parentID"),
                "finish": info.get("finish"),
                "error": error.get("name") if error else None,
                "created": (info.get("time") or {}).get("created"),
                "completed": (info.get("time") or {}).get("completed"),
                "text": _text(message)[:400],
                "tool_commands": _tool_commands(message),
            }
        )
    return rows


def _user_message_id(rows: list[dict[str, Any]], text: str) -> str | None:
    for row in rows:
        if row["role"] == "user" and text in row["text"]:
            return str(row["id"])
    return None


def _chain(rows: list[dict[str, Any]], parent_id: str | None) -> list[dict[str, Any]]:
    return [row for row in rows if row["role"] == "assistant" and parent_id and row["parent_id"] == parent_id]


def _task_prompt(completion_marker: str, prefix: str) -> str:
    steps = ", then ".join(f"`sleep {_STEP_SLEEP_SECS}; cat {prefix}{index}.txt`" for index in range(1, _TASK_STEPS + 1))
    return (
        "Run these shell commands strictly one at a time, each as its own separate bash tool call, never batched "
        f"or parallel: {steps}. After all {_TASK_STEPS} have run, reply with exactly {completion_marker}."
    )


class _BusySampler:
    """Sample provider busy/idle on a thread, so a control call that blocks
    (a queued-follow-up fault holds the steer until the turn ends) cannot hide
    the idle gap it creates."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="opencode-busy-sampler", daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.samples.append({"t": round(time.time(), 2), "busy": _session_busy(self.state)})
            except (OSError, ValueError) as exc:
                self.samples.append({"t": round(time.time(), 2), "error": f"{type(exc).__name__}: {exc}"})
            self._stop.wait(0.4)

    def __enter__(self) -> "_BusySampler":
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _watch_until_idle(state: dict[str, Any], *, timeout: float) -> list[dict[str, Any]]:
    """Sample provider busy/idle until it settles; the samples are evidence."""

    samples = []
    deadline = time.monotonic() + timeout
    idle_streak = 0
    while time.monotonic() < deadline:
        busy = _session_busy(state)
        samples.append({"t": round(time.time(), 2), "busy": busy})
        idle_streak = 0 if busy else idle_streak + 1
        if idle_streak >= 4:
            return samples
        time.sleep(0.5)
    samples.append({"t": round(time.time(), 2), "timeout": True})
    return samples


def _wait_first_tool_completed(state: dict[str, Any], user_text: str, *, timeout: float) -> list[dict[str, Any]]:
    def observe():
        rows = _summarize(_messages(state))
        user_id = _user_message_id(rows, user_text)
        chain = _chain(rows, user_id)
        return rows if any(row["tool_commands"] for row in chain) and _session_busy(state) else None

    return _wait(observe, timeout=timeout, description="the task's first tool call while the turn is busy")


def _wait_marker_answer(state: dict[str, Any], user_text: str, marker: str, *, timeout: float) -> list[dict[str, Any]]:
    def observe():
        rows = _summarize(_messages(state))
        chain = _chain(rows, _user_message_id(rows, user_text))
        return rows if any(marker in row["text"] and row["finish"] == "stop" for row in chain) and not _session_busy(state) else None

    return _wait(observe, timeout=timeout, description=f"assistant answer {marker}")


# --- oracle ------------------------------------------------------------------


def steer_observation(
    rows: list[dict[str, Any]],
    *,
    task_text: str,
    steer_text: str,
    completion_marker: str,
    steer_marker: str,
    samples: list[dict[str, Any]],
    steer_posted_at: float,
) -> dict[str, Any]:
    task_id = _user_message_id(rows, task_text)
    steer_id = _user_message_id(rows, steer_text)
    task_chain = _chain(rows, task_id)
    steer_chain = _chain(rows, steer_id)
    answer = next((row for row in steer_chain if steer_marker in row["text"] and row["finish"] == "stop"), None)
    answer_completed_s = (answer["completed"] or 0) / 1000 if answer else None
    # Every sample from the steer post until the steer answer completed must be
    # busy: an idle sample in that window means the original turn ended and the
    # steer ran as a new turn.
    window = [
        sample
        for sample in samples
        if "busy" in sample and sample["t"] >= steer_posted_at and (answer_completed_s is None or sample["t"] < answer_completed_s - 0.5)
    ]
    idle_gap = any(sample["busy"] is False for sample in window)
    commands = [command for row in task_chain + steer_chain for command in row["tool_commands"]]
    original_turn_completed = any(row["finish"] == "stop" for row in task_chain)
    completion_marker_seen = any(completion_marker in row["text"] for row in rows if row["role"] == "assistant")
    return {
        "task_message_id": task_id,
        "steer_message_id": steer_id,
        "task_assistant_messages": len(task_chain),
        "steer_assistant_messages": len(steer_chain),
        "tool_commands": commands,
        "samples_before_answer": window,
        "session_busy_until_steer_answer": bool(window) and not idle_gap and answer is not None,
        "idle_gap_before_steer_answer": idle_gap,
        "original_turn_completed_independently": original_turn_completed,
        "completion_marker_seen": completion_marker_seen,
        "final_task_step_ran": any(f"{_TASK_STEPS}.txt" in command for command in commands),
        "steer_answered_in_steer_chain": answer is not None,
        "queued_follow_up_shape": original_turn_completed or completion_marker_seen or idle_gap,
    }


def abort_observation(
    rows: list[dict[str, Any]], *, task_text: str, completion_marker: str, follow_text: str, follow_marker: str, idle_after_abort: bool
) -> dict[str, Any]:
    task_id = _user_message_id(rows, task_text)
    task_chain = _chain(rows, task_id)
    follow_chain = _chain(rows, _user_message_id(rows, follow_text))
    commands = [command for row in task_chain for command in row["tool_commands"]]
    return {
        "task_message_id": task_id,
        "task_assistant_messages": len(task_chain),
        "tool_commands": commands,
        "last_task_message_error": task_chain[-1]["error"] if task_chain else None,
        "task_completed": any(row["finish"] == "stop" for row in task_chain),
        "completion_marker_seen": any(completion_marker in row["text"] for row in rows if row["role"] == "assistant"),
        "final_task_step_ran": any(f"{_TASK_STEPS}.txt" in command for command in commands),
        "idle_after_abort": idle_after_abort,
        "follow_up_answered": any(follow_marker in row["text"] and row["finish"] == "stop" for row in follow_chain),
    }


def opencode_helm_lifecycle_assertions(observation: dict[str, Any]) -> dict[str, bool]:
    launch = observation.get("launch") or {}
    send = observation.get("send") or {}
    steer = observation.get("steer") or {}
    abort = observation.get("abort") or {}
    terminate = observation.get("terminate") or {}
    return {
        "opencode_helm_launch_registration": all(
            launch.get(key) for key in ("session_id", "provider_session_id", "run_id", "connection_id")
        )
        and launch.get("tui_ready") is True
        and launch.get("runtime_input_accepted") is True,
        "opencode_helm_send_idle": send.get("dispatch_accepted") is True
        and send.get("idle_before_send") is True
        and send.get("answered") is True,
        "opencode_helm_steer_active": steer.get("dispatch_accepted") is True
        and steer.get("session_busy_until_steer_answer") is True
        and steer.get("steer_answered_in_steer_chain") is True
        and steer.get("queued_follow_up_shape") is False
        and steer.get("final_task_step_ran") is False,
        "opencode_helm_abort_native": abort.get("dispatch_accepted") is True
        and abort.get("task_completed") is False
        and abort.get("completion_marker_seen") is False
        and abort.get("final_task_step_ran") is False
        and abort.get("idle_after_abort") is True
        and abort.get("follow_up_answered") is True,
        "opencode_helm_terminate_owned": terminate.get("dispatch_accepted") is True
        and terminate.get("launcher_exited") is True
        and terminate.get("cleanup_verified") is True
        and terminate.get("forced_cleanup") is False,
    }


def failure_codes(assertions: dict[str, bool], observation: dict[str, Any]) -> dict[str, str]:
    """Typed reasons, so a negative control can prove *why* an assertion failed."""

    codes: dict[str, str] = {}
    steer = observation.get("steer") or {}
    if not assertions.get("opencode_helm_steer_active"):
        if steer.get("queued_follow_up_shape") is True:
            codes["opencode_helm_steer_active"] = "steer_delivered_as_queued_follow_up"
        elif not steer:
            codes["opencode_helm_steer_active"] = "not_reached"
        else:
            codes["opencode_helm_steer_active"] = "steer_did_not_change_active_turn"
    abort = observation.get("abort") or {}
    if not assertions.get("opencode_helm_abort_native"):
        if abort.get("task_completed") is True or abort.get("final_task_step_ran") is True:
            codes["opencode_helm_abort_native"] = "abort_did_not_stop_active_turn"
        elif not abort:
            codes["opencode_helm_abort_native"] = "not_reached"
        else:
            codes["opencode_helm_abort_native"] = "post_abort_turn_not_completed"
    for assertion in ASSERTIONS:
        if not assertions.get(assertion) and assertion not in codes:
            codes[assertion] = "assertion_failed"
    return codes


# --- scenario ------------------------------------------------------------------


def run_opencode_helm_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    live_session_toolkit.require_disposable_runtime(args.api_url)
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    timeout = float(args.live_send_timeout_secs)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("LONGHOUSE_MANAGED_") and key != "LONGHOUSE_SESSION_ID"
    }
    home = live_session_toolkit.isolated_provider_home()
    workspace = home / "workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    for prefix in ("steer", "abort"):
        for index in range(1, _TASK_STEPS + 1):
            (workspace / f"{prefix}{index}.txt").write_text(f"{prefix}-content-{index}\n", encoding="utf-8")
    environment.update(
        {
            "HOME": str(home),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "LONGHOUSE_ENGINE_BIN": str(args.engine),
            # The Machine Agent advertises opencode.* control only when it can
            # resolve the provider binary at channel start. The staged binary is
            # not on PATH as `opencode`, so bind it before the shipper starts.
            "LONGHOUSE_OPENCODE_BIN": str(args.provider_bin),
            "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
            "LONGHOUSE_LAUNCH_ACTOR": "automation",
            "LONGHOUSE_LAUNCH_SURFACE": "test",
        }
    )
    fault_receipt_path = root / "qa-fault-receipt.jsonl"
    if args.negative_control:
        environment["LONGHOUSE_QA_FAULT"] = args.negative_control
        environment["LONGHOUSE_QA_FAULT_RECEIPT"] = str(fault_receipt_path)
    from zerg.qa.opencode_qualification_profile import prepare_opencode_qualification_profile

    model_profile = prepare_opencode_qualification_profile(home, environment)
    # The lifecycle drives bash tool calls; a permission prompt would stall
    # the turn this scenario exists to steer and abort.
    config_path = home / ".config" / "opencode" / "opencode.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["permission"] = {"*": "allow"}
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.environ["LONGHOUSE_OPENCODE_MODEL"] = environment.get("LONGHOUSE_OPENCODE_MODEL", "")
    _write_json(root / "opencode-model-profile-receipt.json", model_profile)

    observation: dict[str, Any] = {"provider": "opencode", "negative_control": args.negative_control}
    launch: live_session_toolkit.PtyProcess | None = None
    shipper: live_session_toolkit.TranscriptShipper | None = None
    states: list[dict[str, Any]] = []
    state: dict[str, Any] = {}
    failure: BaseException | None = None
    control_receipts: dict[str, Any] = {}
    try:
        shipper = live_session_toolkit.start_transcript_shipper(
            "opencode", args, home=home, environment=environment, evidence_root=root / "shipper"
        )
        launch = live_session_toolkit.PtyProcess(
            live_session_toolkit.launch_command(SPEC, args, None, use_credential_files=True, cwd=workspace),
            cwd=workspace,
            env=environment,
            recording=root / "helm-terminal.tty",
        )
        state = live_session_toolkit.wait_state(SPEC, home, process=launch)
        states.append(state)
        live_session_toolkit.wait_opencode_tui_ready(launch, root / "helm-terminal.tty")
        session_id = str(state["session_id"])
        observation["launch"] = {
            "session_id": session_id,
            "provider_session_id": state.get("provider_session_id"),
            "run_id": state.get("run_id"),
            "connection_id": state.get("connection_id"),
            "tui_ready": True,
        }
        _write_json(root / "helm-registration-receipt.json", live_session_toolkit.redact_state_for_evidence(state))

        # send_idle: a message to an idle session must start and finish a turn.
        send_marker = _marker("SEND")
        send_text = f"Reply with exactly {send_marker} and nothing else."
        idle_before_send = not _session_busy(state)
        control_receipts["send"] = _runtime_input(args.api_url, args.agents_token, session_id, send_text, intent="auto", timeout=timeout)
        observation["launch"]["runtime_input_accepted"] = control_receipts["send"]["accepted"]
        send_rows: list[dict[str, Any]] = []
        answered = False
        if control_receipts["send"]["accepted"]:
            try:
                send_rows = _wait_marker_answer(state, send_text, send_marker, timeout=timeout)
                answered = True
            except TimeoutError:
                send_rows = _summarize(_messages(state))
        observation["send"] = {
            "dispatch_accepted": control_receipts["send"]["accepted"],
            "idle_before_send": idle_before_send,
            "answered": answered,
            "marker": send_marker,
        }
        _write_json(root / "native-messages-send.json", send_rows)

        # steer_active
        steer_done = _marker("STEER_TASK_DONE")
        steer_marker = _marker("STEER")
        task_text = _task_prompt(steer_done, "steer")
        steer_text = f"STOP. Change of plan: do not run any more commands. Reply with only {steer_marker}."
        control_receipts["steer_task"] = _runtime_input(
            args.api_url, args.agents_token, session_id, task_text, intent="auto", timeout=timeout
        )
        _wait_first_tool_completed(state, task_text, timeout=timeout)
        steer_posted_at = time.time()
        with _BusySampler(state) as sampler:
            control_receipts["steer"] = _runtime_input(
                args.api_url, args.agents_token, session_id, steer_text, intent="steer", timeout=timeout
            )
            _watch_until_idle(state, timeout=timeout)
        samples = sampler.samples
        steer_rows = _summarize(_messages(state))
        observation["steer"] = {
            "dispatch_accepted": control_receipts["steer"]["accepted"],
            **steer_observation(
                steer_rows,
                task_text=task_text,
                steer_text=steer_text,
                completion_marker=steer_done,
                steer_marker=steer_marker,
                samples=samples,
                steer_posted_at=steer_posted_at,
            ),
        }
        _write_json(root / "native-messages-steer.json", steer_rows)

        # abort_native
        abort_done = _marker("ABORT_TASK_DONE")
        abort_task = _task_prompt(abort_done, "abort")
        control_receipts["abort_task"] = _runtime_input(
            args.api_url, args.agents_token, session_id, abort_task, intent="auto", timeout=timeout
        )
        _wait_first_tool_completed(state, abort_task, timeout=timeout)
        control_receipts["abort"] = _runtime_post(args.api_url, args.agents_token, f"{session_id}/interrupt-live", None)
        abort_samples = _watch_until_idle(state, timeout=timeout)
        idle_after_abort = not abort_samples[-1].get("timeout")
        follow_marker = _marker("AFTER_ABORT")
        follow_text = f"Reply with exactly {follow_marker} and nothing else."
        control_receipts["after_abort_send"] = _runtime_input(
            args.api_url, args.agents_token, session_id, follow_text, intent="auto", timeout=timeout
        )
        try:
            abort_rows = _wait_marker_answer(state, follow_text, follow_marker, timeout=timeout)
        except TimeoutError:
            abort_rows = _summarize(_messages(state))
        observation["abort"] = {
            "dispatch_accepted": control_receipts["abort"]["accepted"],
            "samples": abort_samples,
            **abort_observation(
                abort_rows,
                task_text=abort_task,
                completion_marker=abort_done,
                follow_text=follow_text,
                follow_marker=follow_marker,
                idle_after_abort=idle_after_abort,
            ),
        }
        _write_json(root / "native-messages-abort.json", abort_rows)

        # terminate_owned
        control_receipts["terminate"] = _runtime_post(args.api_url, args.agents_token, f"{session_id}/terminate-live", None)
        exit_code = launch.wait(30)
        # Give the provider server the same bound before the helper is allowed
        # to force anything; a terminate that needed SIGKILL did not terminate.
        provider_pid = live_session_toolkit.provider_process_pid(SPEC, state)
        live_session_toolkit.wait_pid_dead(provider_pid, timeout=30)
        cleanup = live_session_toolkit.cleanup_processes(SPEC, (launch,), states)
        observation["terminate"] = {
            "dispatch_accepted": control_receipts["terminate"]["accepted"],
            "launcher_exited": exit_code is not None,
            "launcher_exit_code": exit_code,
            "cleanup_verified": cleanup.get("verified") is True,
            "forced_cleanup": bool(
                cleanup.get("forced_cleanup_pids")
                or cleanup.get("forced_provider_cleanup_pids")
                or cleanup.get("forced_attach_cleanup_pids")
            ),
            "cleanup": cleanup,
        }
    except Exception as exc:  # noqa: BLE001 - keep the causal failure beside cleanup evidence
        failure = exc
        observation["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        session_id = str(state.get("session_id") or "")
        if failure is not None and session_id:
            control_receipts["failure_terminate"] = _runtime_post(
                args.api_url, args.agents_token, f"{session_id}/terminate-live", None, timeout=20
            )
        if launch is not None and launch.process.poll() is None:
            launch.kill_group(signal.SIGTERM)
            if launch.wait(5) is None:
                launch.kill_group(signal.SIGKILL)
                launch.wait(5)
        final_cleanup = (
            live_session_toolkit.cleanup_processes(SPEC, (launch,), states) if launch is not None else {"verified": True, "orphan_count": 0}
        )
        # The attach wrapper and its TUI run in their own process group, so the
        # launcher group kill cannot reach them. Anything still naming this
        # session is a leak: kill it and fail the cleanup gate.
        session_orphans = _kill_session_processes(
            [str(state.get(key) or "") for key in ("session_id", "provider_session_id", "server_url")]
        )
        final_cleanup["session_orphans"] = session_orphans
        if shipper is not None:
            try:
                observation["shipper_flush"] = shipper.flush("opencode-helm-cleanup")
            except Exception as exc:  # noqa: BLE001
                observation.setdefault("cleanup_errors", []).append(f"flush: {type(exc).__name__}: {exc}")
            try:
                observation["shipper_stop"] = shipper.stop()
            except Exception as exc:  # noqa: BLE001
                observation.setdefault("cleanup_errors", []).append(f"shipper stop: {type(exc).__name__}: {exc}")
        run_id = str(state.get("run_id") or "")
        served_runs = (
            console_lifecycle._wait_served_run_retirement(
                str(args.api_url), str(args.agents_token), session_id, [{"session_id": session_id, "run_id": run_id, "state": "terminal"}]
            )
            if session_id and run_id
            else {"retired": False, "error": "session_or_run_identity_unavailable"}
        )
        retirement = (
            live_session_toolkit.retire_qualification_session(args.api_url, args.agents_token, session_id, provider="opencode")
            if session_id
            else {"status": "fail", "error": "no session"}
        )
        shipper_stop = observation.get("shipper_stop") if isinstance(observation.get("shipper_stop"), dict) else {}
        cleanup_receipt = {
            **final_cleanup,
            "provider_process_dead": final_cleanup.get("verified") is True,
            "process_group_dead": final_cleanup.get("verified") is True,
            "no_orphan_provider_processes": final_cleanup.get("orphan_count") == 0 and not session_orphans,
            "served_run_inventory": served_runs,
            "served_run_retired": served_runs.get("retired") is True and served_runs.get("active_run_count") == 0,
            "session_retirement": retirement,
            "canary_session_hidden": retirement.get("status") == "pass"
            and retirement.get("hidden") is True
            and retirement.get("archived") is True
            and retirement.get("present_in_served_inventory") is False,
            "shipper_stopped": shipper_stop.get("stopped") is True and shipper_stop.get("process_dead") is True,
            "cleanup_errors": observation.get("cleanup_errors", []),
        }
        cleanup_receipt["status"] = (
            "pass"
            if all(
                cleanup_receipt[key] is True
                for key in (
                    "provider_process_dead",
                    "no_orphan_provider_processes",
                    "served_run_retired",
                    "canary_session_hidden",
                    "shipper_stopped",
                )
            )
            and not cleanup_receipt["cleanup_errors"]
            else "fail"
        )
        cleanup_receipt["required_cleanup"] = {key: cleanup_receipt[key] is True for key in REGISTRATION.required_cleanup}
        observation["cleanup"] = cleanup_receipt
        _write_json(root / "cleanup-receipt.json", cleanup_receipt)
        _write_json(root / "control-command-receipts.json", control_receipts)
        _write_json(root / "native-message-receipts.json", {key: observation.get(key) for key in ("send", "steer", "abort")})

    assertions = opencode_helm_lifecycle_assertions(observation)
    codes = failure_codes(assertions, observation)
    fired = []
    if fault_receipt_path.is_file():
        for line in fault_receipt_path.read_text(encoding="utf-8").splitlines():
            try:
                fired.append(json.loads(line))
            except json.JSONDecodeError:
                fired.append({"unparseable_receipt_line": line[:300]})
    status = "pass" if failure is None and all(assertions.values()) and observation["cleanup"]["status"] == "pass" else "fail"
    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "opencode_helm_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "opencode",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "observation_scope": "scenario",
        "generated_at": now(),
        "assertions": assertions,
        "failure_codes": codes,
        "observation": observation,
        "provider_binary": provider_receipt,
    }
    if args.negative_control:
        target, expected_code = NEGATIVE_CONTROLS[args.negative_control]
        preconditions = [
            assertion for assertion in ("opencode_helm_launch_registration", "opencode_helm_send_idle") if not assertions[assertion]
        ]
        verdict = {
            "fault": args.negative_control,
            "fault_fired": any(entry.get("fault") == args.negative_control for entry in fired),
            "fault_receipts": fired,
            "healthy_preconditions_failed": preconditions,
            "target_assertion": target,
            "target_assertion_failed": assertions[target] is False,
            "target_failure_code": codes.get(target),
            "expected_failure_code": expected_code,
            "cleanup_passed": observation["cleanup"]["status"] == "pass",
        }
        verdict["status"] = (
            "pass"
            if verdict["fault_fired"]
            and not preconditions
            and verdict["target_assertion_failed"]
            and verdict["target_failure_code"] == expected_code
            and verdict["cleanup_passed"]
            else "inconclusive"
            if failure is not None and not verdict["fault_fired"]
            else "fail"
        )
        result["artifact_kind"] = "opencode_helm_lifecycle_negative_control"
        result["negative_control"] = verdict
        status = verdict["status"]
    result["status"] = status
    if failure is not None:
        result["failure_code"] = "opencode_helm_lifecycle_failed"
        result["error"] = f"{type(failure).__name__}: {failure}"
    secrets = list(live_session_toolkit.qualification_secrets(environment, str(args.agents_token)))
    result["redacted_secret_files"] = live_session_toolkit.secret_scan(root, secrets)
    result["artifact_manifest"] = artifact_manifest(root)
    _write_json(root / "result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS)
    parser.add_argument("--api-url", default=os.environ.get(live_session_toolkit.RUNTIME_API_URL_ENV))
    parser.add_argument("--agents-token", default=os.environ.get(live_session_toolkit.RUNTIME_AGENTS_TOKEN_ENV))
    parser.add_argument("--live-send-timeout-secs", type=int, default=180)
    parser.add_argument("--negative-control", choices=tuple(NEGATIVE_CONTROLS))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    missing = [
        name
        for name in ("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "api_url", "agents_token")
        if not getattr(args, name)
    ]
    if missing:
        print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{missing[0].replace('_', '-')}"}))
        return 2
    try:
        result = run_opencode_helm_lifecycle(args)
    except Exception as exc:  # noqa: BLE001 - a typed producer failure, never a traceback-only exit
        result = {
            "schema_version": 1,
            "artifact_kind": "opencode_helm_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "opencode",
            "scenario_id": SCENARIO_ID,
            "status": "fail",
            "failure_code": "opencode_helm_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "generated_at": now(),
        }
    print(
        json.dumps(
            {key: result.get(key) for key in ("status", "failure_code", "error", "assertions", "failure_codes", "negative_control")},
            sort_keys=True,
            default=str,
        )
    )
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ASSERTIONS", "REGISTRATION", "abort_observation", "opencode_helm_lifecycle_assertions", "steer_observation"]
