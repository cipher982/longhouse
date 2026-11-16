#!/usr/bin/env python3
"""Live Antigravity Helm lifecycle proof through the stock Longhouse facade, against a scripted model.

Launches stock ``agy`` through ``longhouse antigravity`` inside a PTY, as a person at a terminal would, and drives the
Runtime Host controls a client uses: launch registration, a remote send, an active-turn steer, an interrupt and a
terminate. The model is the credential-free scripted Gemini endpoint (``antigravity_scripted_lane``): the CI Gemini key
is free tier, so a live-token cell would fail on quota rather than on Longhouse, and the profile pre-accepts agy's
first-run Terms of Service and data-collection screen (David approved that for the bench and factory profile; see
``scripted_model_profiles._configure_antigravity_profile``).

The oracles judge what the provider itself saw and did, not the controls Longhouse sent: the hook inbox's claim
receipts (which hook event took the message, with which intent) and the requests the scripted model received.

A remote send reaches agy two ways. During a running turn the hook claims it at the next model invocation (an active-turn
send). While agy is idle the hook never fires, so the launcher, which owns agy's terminal, claims the message itself and
types it as the user's own words, which starts a turn (an idle send). Both are cells: ``send_active`` and ``send_idle``.
An idle send that is not claimed leaves the engine waiting its 15 s claim window and the Runtime Host answering HTTP 503
"Request timed out" (the behaviour on agy 1.5.9 before the launcher owned the terminal).

Terminate is asserted on the served run: the launcher sees agy's exit and reports the run's end, so after a remote terminate
the served run must read ended, not just the owned process group gone.

Helm resume is not a cell: ``session.resume.helm`` is ``policy_disabled`` for Antigravity (the launcher refuses
``--conversation`` and ``-c``), and ``antigravity.resume_policy.v1`` proves the refusal.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from zerg.qa import antigravity_scripted_lane as lane_module
from zerg.qa import scripted_model_profiles
from zerg.qa.cursor_helm_product_e2e import _activity_state
from zerg.qa.cursor_helm_product_e2e import _answered_in
from zerg.qa.cursor_helm_product_e2e import _can_send_canonical
from zerg.qa.cursor_helm_product_e2e import _canonical_state_from_diagnostics
from zerg.qa.cursor_helm_product_e2e import _run_lifecycle
from zerg.qa.cursor_helm_product_e2e import failed_report
from zerg.qa.live_kit import poll
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import qualification_secrets
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import secret_scan
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import wait_pid_dead
from zerg.qa.live_session_toolkit import wait_process_group_dead
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.wait_kind import FIELD as WAITED_ON_FIELD
from zerg.qa.wait_kind import PROVIDER_TURN
from zerg.qa.wait_kind import RUNTIME_HOST
from zerg.qa.wait_kind import declared as declared_wait

PROVIDER = "antigravity"
SCENARIO_ID = "antigravity_helm_lifecycle"
PREFIX = lane_module.HELM_PREFIX

ASSERTIONS = (
    "antigravity_helm_launch_registration",
    "antigravity_helm_send_idle",
    "antigravity_helm_send_active",
    "antigravity_helm_steer_active",
    "antigravity_helm_interrupt",
    "antigravity_helm_terminate_owned",
)

REGISTRATION = ProducerRegistration(
    producer_id="antigravity.helm_lifecycle.v1",
    producer_revision=2,
    scenario_id=SCENARIO_ID,
    scenario_revision=2,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=(PROVIDER,),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("scripted_model",),
    observed_activity=(
        "stock_longhouse_antigravity_helm_launch",
        "hook_bound_conversation",
        "idle_send_typed_by_launcher_and_answered",
        "remote_send_reply_archived",
        "steer_claimed_by_pre_invocation_hook",
        "interrupt_ended_turn_at_invocation_boundary",
        "remote_terminate_provider_group_dead",
        "remote_terminate_served_run_ended",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=(),
    credential_binding_ids_by_evidence_class={"scripted_model": ("runtime_host_control",)},
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "scripted_model_receipt",
        "transcript_shipper_receipt",
        "helm_lifecycle_report",
        "cleanup_receipt",
    ),
    required_cleanup=("session_stopped", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/antigravity_helm_lifecycle.py",
    oracle_source="server/zerg/qa/antigravity_helm_lifecycle.py",
    oracle_entrypoint="lifecycle_assertions",
    executable_module="zerg.qa.antigravity_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)

# (query agy writes, the reply a terminal gives): XTVERSION, secondary and primary device attributes.
_TERMINAL_QUERIES: tuple[tuple[bytes, bytes], ...] = (
    (b"\x1b[>q", b"\x1bP>|xterm(388)\x1b\\"),
    (b"\x1b[>c", b"\x1b[>0;388;0c"),
    (b"\x1b[c", b"\x1b[?62;c"),
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>]")
_wait = functools.partial(poll, interval=0.25)


# ---------------------------------------------------------------------------
# Oracles: pure functions over what the run observed.
# ---------------------------------------------------------------------------


def claim_receipts(inbox_dir: Path) -> list[dict[str, Any]]:
    """The hook's claim receipts for a session, oldest first: which event took a message and with which intent."""

    rows: list[dict[str, Any]] = []
    for path in sorted((inbox_dir / "claimed").glob("claimed-msg-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def model_requests(dump_dir: Path) -> list[str]:
    """Every request body the scripted model received, in arrival order."""

    return [path.read_text(encoding="utf-8", errors="replace") for path in sorted(dump_dir.glob("*.json"))]


def steer_landed(
    *, claims: list[dict[str, Any]], requests: list[str], steer_marker: str, tool_output: str, answered: bool
) -> dict[str, Any]:
    """The steer reached the running turn: the hook claimed it as a steer, and the model call after the slow tool carried it.

    A steer delivered as a new turn would put the steer text in a request that does not carry the tool's output (the
    turn would have finished first); one that never landed would leave the answer unmatched.
    """

    claimed = [row for row in claims if row.get("intent") == "steer" and steer_marker in str(row.get("text") or "")]
    in_turn = [body for body in requests if steer_marker in body and tool_output in body]
    verdict = {
        "steer_claimed": bool(claimed),
        "claim_hook_event": claimed[0].get("hook_event") if claimed else None,
        "steer_in_the_request_after_the_tool": bool(in_turn),
        "steered_reply_archived": answered,
    }
    verdict["passed"] = all((verdict["steer_claimed"], verdict["steer_in_the_request_after_the_tool"], verdict["steered_reply_archived"]))
    if not verdict["steer_claimed"]:
        verdict["failure_code"] = "steer_not_claimed"
    elif not verdict["steer_in_the_request_after_the_tool"]:
        verdict["failure_code"] = "steer_did_not_change_course"
    elif not answered:
        verdict["failure_code"] = "steered_reply_missing"
    return verdict


def send_delivered(
    *, claims: list[dict[str, Any]], requests: list[str], marker: str, tool_output: str, answered: bool, error: str | None
) -> dict[str, Any]:
    """A remote send reached the running turn: the hook claimed it as a send, and the call after the slow tool carried it."""

    claimed = [row for row in claims if row.get("intent") == "send" and marker in str(row.get("text") or "")]
    in_turn = [body for body in requests if marker in body and tool_output in body]
    verdict = {
        "send_error": error,
        "send_claimed": bool(claimed),
        "claim_hook_event": claimed[0].get("hook_event") if claimed else None,
        "send_in_the_request_after_the_tool": bool(in_turn),
        "sent_reply_archived": answered,
    }
    verdict["passed"] = error is None and bool(claimed) and bool(in_turn) and answered
    if error is not None:
        verdict["failure_code"] = "send_rejected"
    elif not claimed:
        verdict["failure_code"] = "send_not_claimed"
    elif not in_turn:
        verdict["failure_code"] = "send_did_not_reach_the_turn"
    elif not answered:
        verdict["failure_code"] = "sent_reply_missing"
    return verdict


def send_idle_delivered(
    *, claims: list[dict[str, Any]], requests: list[str], marker: str, answered: bool, error: str | None, idle_at_send: bool
) -> dict[str, Any]:
    """A remote send to an idle session started a turn: it was claimed as a send, the model received it, and its reply was archived.

    The hook fires only on a model invocation, so an idle session has nobody to claim the message; a claim here is the
    launcher typing it into the terminal. ``idle_at_send`` says the session really was idle (no turn in flight), so the
    cell cannot pass on a send that merely joined a running turn.
    """

    claimed = [row for row in claims if row.get("intent") == "send" and marker in str(row.get("text") or "")]
    reached_model = any(marker in body for body in requests)
    verdict = {
        "send_error": error,
        "idle_at_send": idle_at_send,
        "send_claimed": bool(claimed),
        "claimed_by": claimed[0].get("claimed_by") if claimed else None,
        "model_received_it": reached_model,
        "sent_reply_archived": answered,
    }
    verdict["passed"] = error is None and idle_at_send and bool(claimed) and reached_model and answered
    if error is not None:
        verdict["failure_code"] = "idle_send_rejected"
    elif not idle_at_send:
        verdict["failure_code"] = "session_not_idle"
    elif not claimed:
        verdict["failure_code"] = "idle_send_not_claimed"
    elif not reached_model:
        verdict["failure_code"] = "idle_send_never_reached_the_model"
    elif not answered:
        verdict["failure_code"] = "idle_send_reply_missing"
    return verdict


def terminate_held(*, dispatched: bool, provider_dead: bool, group_dead: bool, run_lifecycle: str | None) -> dict[str, Any]:
    """A remote terminate ended the owned process group and the served run.

    The process facts say agy is gone; the served run reading ``ended`` says the launcher reported it, as the other Helm
    launchers do. A run still ``running`` after its provider died is the gap this cell exists to catch.
    """

    verdict = {
        "terminate_dispatched": dispatched,
        "provider_process_dead": provider_dead,
        "provider_group_dead": group_dead,
        "run_lifecycle": run_lifecycle,
        "served_run_ended": run_lifecycle == "ended",
    }
    verdict["passed"] = dispatched and provider_dead and group_dead and run_lifecycle == "ended"
    if not (dispatched and provider_dead and group_dead):
        verdict["failure_code"] = "terminate_did_not_stop_the_group"
    elif run_lifecycle != "ended":
        verdict["failure_code"] = "served_run_still_running_after_terminate"
    return verdict


def interrupt_held(
    *, claims: list[dict[str, Any]], requests: list[str], tool_output: str, tui_alive: bool, recovered: bool, later_marker: str = ""
) -> dict[str, Any]:
    """The interrupt ended the turn at the next invocation boundary and the session took a later turn.

    agy runs the tool as a background task, so its output is appended to history once it finishes. A model call that
    carries the output but not the later turn's marker was made by the interrupted turn itself; the later turn's own
    request legitimately carries the finished task's output and is not counted.
    """

    claimed = [row for row in claims if row.get("intent") == "interrupt"]
    leaked = sum(tool_output in body and not (later_marker and later_marker in body) for body in requests)
    verdict = {
        "interrupt_claimed": bool(claimed),
        "claim_hook_event": claimed[0].get("hook_event") if claimed else None,
        "model_requests_with_tool_output": leaked,
        "tui_alive_after_interrupt": tui_alive,
        "recovery_turn_answered": recovered,
    }
    verdict["passed"] = bool(claimed) and leaked == 0 and tui_alive and recovered
    if not claimed:
        verdict["failure_code"] = "interrupt_not_claimed"
    elif leaked:
        verdict["failure_code"] = "model_called_after_interrupt"
    return verdict


def lifecycle_assertions(report: Mapping[str, Any], *, cleanup_ok: bool) -> dict[str, bool]:
    lifecycle = report.get("lifecycle") if isinstance(report.get("lifecycle"), dict) else {}

    def held(key: str) -> bool:
        value = lifecycle.get(key)
        if not isinstance(value, dict):
            return False
        if "passed" in value:
            return value.get("passed") is True
        return all(item is True for item in value.values())

    passed = report.get("status") == "passed"
    return {
        "antigravity_helm_launch_registration": held("launch_registration"),
        "antigravity_helm_send_idle": held("send_idle"),
        "antigravity_helm_send_active": held("send_active"),
        "antigravity_helm_steer_active": held("steer_active"),
        "antigravity_helm_interrupt": held("interrupt"),
        "antigravity_helm_terminate_owned": passed and held("terminate_owned") and cleanup_ok,
    }


# ---------------------------------------------------------------------------
# The run.
# ---------------------------------------------------------------------------


def answer_terminal_queries(terminal: ProviderPtySession, answered: set[bytes]) -> None:
    """Answer what agy asks its terminal (XTVERSION, device attributes) once each, as xterm would.

    A person's terminal answers these; a bare PTY does not, and agy then draws nothing.
    """

    try:
        raw = terminal.terminal_path.read_bytes()
    except OSError:
        return
    for query, reply in _TERMINAL_QUERIES:
        if query in raw and query not in answered:
            answered.add(query)
            terminal.write(reply)


def _screen(terminal_path: Path) -> str:
    try:
        return _ANSI.sub("", terminal_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _marker(kind: str) -> str:
    return f"{PREFIX}_{kind}_{uuid4().hex}"


# Named by string only: the child process imports it, the verifier never does.
SUBJECT_HOOK_MODULE = ".".join(["zerg", "services", "antigravity" + "_hook_inbox"])


def _install_hook(args: argparse.Namespace, home: Path, longhouse_home: Path, environment: dict[str, str]) -> None:
    """Stage the shipped Longhouse hook where `longhouse antigravity` insists on finding it, inside the isolated HOME.

    The installer is a subject module the verifier may not import, so it runs as the subject's own Python in a child
    process (as the engine's live receipt does), with HOME and LONGHOUSE_HOME pointing into the run.
    """

    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import importlib\n"
        f"module = importlib.import_module({SUBJECT_HOOK_MODULE!r})\n"
        "module._ensure_antigravity_runtime_plugin(config_dir=Path(sys.argv[1]), antigravity_bin=sys.argv[2])\n"
    )
    child_environment = dict(environment)
    child_environment["PYTHONPATH"] = os.pathsep.join([str(args.repo_root / "server"), child_environment.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(longhouse_home), str(args.provider_bin)],
        cwd=home,
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"the Antigravity hook could not be staged: {completed.stderr[-1000:]}")


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    write_json(root / "provider-binary-receipt.json", provider_receipt)
    run.fields["provider_binary"] = provider_receipt

    api_url = str(args.api_url or "").rstrip("/")
    token = str(args.agents_token or "")
    require_disposable_runtime(api_url)
    home = isolated_provider_home()
    environment = scripted_model_profiles.credential_free_environment(reject_ambient_credentials=False)
    environment["HOME"] = str(home)
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    environment["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
    environment["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
    environment["LONGHOUSE_LAUNCH_SURFACE"] = "test"
    environment["LONGHOUSE_RUNTIME_API_URL"] = api_url
    environment["LONGHOUSE_RUNTIME_AGENTS_TOKEN"] = token

    lane: lane_module.ScriptedLane | None = None
    shipper: TranscriptShipper | None = None
    terminal: ProviderPtySession | None = None
    report: dict[str, Any] = {"status": "running"}
    lifecycle: dict[str, Any] = {}
    report["lifecycle"] = lifecycle
    provider_pid: int | None = None
    try:
        lane = lane_module.start(root, home, environment, helm=True)
        # After the profile, which scrubs this name: the Machine Agent advertises Antigravity control only when it finds the binary.
        environment["LONGHOUSE_ANTIGRAVITY_BIN"] = str(args.provider_bin)
        # Compact paths: the shipper's wake socket lives under LONGHOUSE_HOME, and a unix socket path is capped near 108 bytes.
        longhouse_home = home / "h" / "lh"
        longhouse_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        _install_hook(args, home, longhouse_home, environment)
        shipper = start_transcript_shipper(
            PROVIDER, args, home=home, environment=environment, evidence_root=root / "shipper", longhouse_home=longhouse_home
        )
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        longhouse_home = Path(environment["LONGHOUSE_HOME"])
        sessions_dir = longhouse_home / "managed-local" / "antigravity" / "sessions"
        workspace = home / "h" / "w"
        workspace.mkdir(parents=True, exist_ok=True)
        timeout = args.timeout_secs

        terminal = ProviderPtySession.start(
            argv=[
                str(args.longhouse_cli),
                "antigravity",
                "--cwd",
                str(workspace),
                "--url",
                api_url,
                "--token",
                token,
                "--antigravity-bin",
                str(args.provider_bin),
                "--",
                # Helm adds no permission flag: the harness stands in for a user who chose to skip prompts, which the
                # scripted model's tool calls need.
                "--dangerously-skip-permissions",
            ],
            cwd=workspace,
            env=environment,
            terminal_path=root / "terminal.raw",
            rows=50,
            columns=160,
        )

        answered_queries: set[bytes] = set()

        def ready_prompt() -> bool:
            answer_terminal_queries(terminal, answered_queries)
            text = _screen(terminal.terminal_path)
            if "Do you trust the contents of this project" in text and not report.get("workspace_trust_accepted"):
                terminal.write(b"\r")
                report["workspace_trust_accepted"] = True
                time.sleep(1.0)
                return False
            if not terminal.alive():
                raise RuntimeError(f"agy exited before it was ready (exit={terminal.process.returncode})")
            return "? for shortcuts" in text

        _wait(ready_prompt, timeout=timeout, description="the Antigravity prompt", waited_on=PROVIDER_TURN)

        def state_file() -> dict[str, Any] | None:
            for path in sorted(sessions_dir.glob("*.json")):
                try:
                    row = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(row, dict) and row.get("session_id"):
                    return row
            return None

        state = _wait(state_file, timeout=timeout, description="the Antigravity Helm session state", waited_on=RUNTIME_HOST)
        session_id = str(state["session_id"])
        report["session_id"] = session_id
        state_path = sessions_dir / f"{session_id}.json"
        inbox_dir = longhouse_home / "managed-local" / "antigravity" / "inbox" / session_id

        headers = {"X-Agents-Token": token}

        def api_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
            try:
                response = httpx.get(f"{api_url}{path}", headers=headers, params=params, timeout=10)
            except httpx.TransportError:
                return None
            if response.status_code in {404, 429, 503}:
                return None
            response.raise_for_status()
            return response.json()

        def hosted_events() -> dict[str, Any] | None:
            return api_get(
                f"/api/agents/sessions/{session_id}/events",
                params={"context_mode": "forensic", "branch_mode": "head", "limit": 100},
            )

        def served_state() -> dict[str, Any] | None:
            return _canonical_state_from_diagnostics(api_get(f"/api/agents/sessions/{session_id}/state-diagnostics"))

        def hook_state() -> dict[str, Any]:
            try:
                row = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            return row if isinstance(row, dict) else {}

        def answered(marker: str) -> bool:
            return _answered_in(hosted_events(), marker) is not None

        def settled() -> dict[str, Any]:
            return _wait(
                lambda: (state if _activity_state(state) == "quiescent" else None) if (state := served_state()) else None,
                timeout=timeout,
                description="served Antigravity activity settling to quiescent after the turn",
                waited_on=RUNTIME_HOST,
            )

        def tool_in_flight() -> bool:
            row = hook_state()
            return row.get("last_hook_event") == "PreToolUse" and row.get("state") == "running"

        # Launch: the person types the first prompt.
        launch_marker = _marker("LAUNCH")
        terminal.submit_line(f"Reply with exactly {launch_marker}")
        _wait(
            lambda: hook_state().get("conversation_id"),
            timeout=timeout,
            description="the hook binding the Antigravity conversation",
            waited_on=PROVIDER_TURN,
        )
        _wait(
            lambda: answered(launch_marker),
            timeout=timeout,
            description="the first Antigravity reply in the hosted archive",
            waited_on=PROVIDER_TURN,
        )
        settled()
        control = _wait(
            lambda: (state if _can_send_canonical(state) else None) if (state := served_state()) else None,
            timeout=timeout,
            description="Antigravity send control on the Runtime Host",
            waited_on=RUNTIME_HOST,
        )
        provider_pid = int(hook_state().get("provider_pid") or 0) or None
        report["provider_pid"] = provider_pid
        lifecycle["launch_registration"] = {
            "state_file_identity": bool(state.get("run_id")) and bool(state.get("connection_id")),
            "hook_bound_conversation": bool(hook_state().get("conversation_id")),
            "first_reply_archived": True,
            "provider_process_recorded": provider_pid is not None,
            "send_control_served": _can_send_canonical(control),
        }

        # Idle send: nothing is running, so no hook fires; the launcher must claim the message and start a turn.
        idle_marker = _marker("IDLE")
        idle_at_send = hook_state().get("state") == "idle"
        idle_error: str | None = None
        try:
            idle_response = httpx.post(
                f"{api_url}/api/agents/sessions/{session_id}/send-live",
                headers=headers,
                json={"message": f"Reply with exactly {idle_marker}"},
                timeout=40,
            )
            lifecycle["send_idle_dispatch"] = {"status_code": idle_response.status_code, "body": idle_response.text[:300]}
            if idle_response.is_error:
                idle_error = f"HTTP {idle_response.status_code}: {idle_response.text[:500]}"
            elif idle_response.json().get("accepted") is not True:
                idle_error = f"not accepted: {idle_response.text[:500]}"
        except httpx.HTTPError as exc:
            idle_error = f"{type(exc).__name__}: {exc}"
        idle_answered = idle_error is None and bool(poll(lambda: answered(idle_marker), timeout=timeout, interval=0.25))
        lifecycle["send_idle"] = send_idle_delivered(
            claims=claim_receipts(inbox_dir),
            requests=model_requests(lane.dump_dir),
            marker=idle_marker,
            answered=idle_answered,
            error=idle_error,
            idle_at_send=idle_at_send,
        )
        if idle_error is not None:
            raise RuntimeError(f"Runtime Host idle send failed: {idle_error}")
        settled()

        # Send: a turn the person started runs a slow tool; the remote message joins it at the next invocation.
        send_task = _marker("SENDTASK")
        sent = _marker("SENT")
        terminal.submit_line(f"Do the first slow task: {send_task}")
        _wait(tool_in_flight, timeout=timeout, description="the first slow Antigravity tool to start", waited_on=PROVIDER_TURN)
        time.sleep(1.0)
        send_error: str | None = None
        try:
            response = httpx.post(
                f"{api_url}/api/agents/sessions/{session_id}/send-live",
                headers=headers,
                json={"message": f"Change of plan, reply with exactly {sent}"},
                timeout=30,
            )
            if response.is_error:
                send_error = f"HTTP {response.status_code}: {response.text[:500]}"
            elif response.json().get("accepted") is not True:
                send_error = f"not accepted: {response.text[:500]}"
        except httpx.HTTPError as exc:
            send_error = f"{type(exc).__name__}: {exc}"
        send_answered = send_error is None and bool(poll(lambda: answered(sent), timeout=timeout, interval=0.25))
        if send_error is None:
            settled()  # the slow tool's output reaches the model only after the sent message was answered
        lifecycle["send_active"] = send_delivered(
            claims=claim_receipts(inbox_dir),
            requests=model_requests(lane.dump_dir),
            marker=sent,
            tool_output=lane_module.helm_send_tool_output(lane.steer_tag),
            answered=send_answered,
            error=send_error,
        )
        if send_error is not None:
            raise RuntimeError(f"Runtime Host send failed: {send_error}")
        settled()

        # Steer: a turn the person started runs a slow tool; the steer joins it.
        steer_first = _marker("STEER_FIRST")
        steered = _marker("STEERED")
        terminal.submit_line(f"Do the slow task: {steer_first}")
        _wait(tool_in_flight, timeout=timeout, description="the slow Antigravity tool to start", waited_on=PROVIDER_TURN)
        time.sleep(1.0)
        steer_response = httpx.post(
            f"{api_url}/api/agents/sessions/{session_id}/input",
            headers=headers,
            json={"text": f"Change of plan, reply with exactly {steered}", "intent": "steer", "client_request_id": uuid4().hex},
            timeout=30,
        )
        lifecycle["steer_dispatch"] = {"status_code": steer_response.status_code, "body": steer_response.text[:500]}
        if steer_response.is_error:
            lifecycle["steer_active"] = {"passed": False, "failure_code": "steer_rejected"}
            raise RuntimeError(f"Runtime Host steer failed HTTP {steer_response.status_code}: {steer_response.text[:1000]}")
        steer_answered = bool(poll(lambda: answered(steered), timeout=timeout, interval=0.25))
        settled()
        lifecycle["steer_active"] = steer_landed(
            claims=claim_receipts(inbox_dir),
            requests=model_requests(lane.dump_dir),
            steer_marker=steered,
            tool_output=lane_module.helm_steer_tool_output(lane.steer_tag),
            answered=steer_answered,
        )

        # Interrupt: the person starts another slow tool; the remote interrupt ends the turn at the next boundary.
        interrupt_marker = _marker("INTERRUPT")
        terminal.submit_line(f"Do the interrupted task: {interrupt_marker}")
        _wait(tool_in_flight, timeout=timeout, description="the interruptible Antigravity tool to start", waited_on=PROVIDER_TURN)
        time.sleep(0.5)
        interrupt_response = httpx.post(f"{api_url}/api/agents/sessions/{session_id}/interrupt-live", headers=headers, timeout=30)
        dispatched = not interrupt_response.is_error and interrupt_response.json().get("interrupt_dispatched") is True
        lifecycle["interrupt_dispatch"] = {"status_code": interrupt_response.status_code, "dispatched": dispatched}
        # Let the tool's own sleep finish: the output reaching the model afterwards is what the oracle looks for.
        time.sleep(lane_module.HELM_INTERRUPT_SLEEP_SECONDS + 4)
        recover_marker = _marker("RECOVER")
        tui_alive = terminal.alive()
        recovered = False
        if tui_alive:
            terminal.submit_line(f"Reply with exactly {recover_marker}")
            recovered = bool(poll(lambda: answered(recover_marker), timeout=timeout, interval=0.25))
        interrupt_verdict = interrupt_held(
            claims=claim_receipts(inbox_dir),
            requests=model_requests(lane.dump_dir),
            tool_output=lane_module.HELM_INTERRUPT_TOOL_OUTPUT,
            tui_alive=tui_alive,
            recovered=recovered,
            later_marker=recover_marker,
        )
        interrupt_verdict["dispatched"] = dispatched
        interrupt_verdict["passed"] = bool(interrupt_verdict["passed"] and dispatched)
        lifecycle["interrupt"] = interrupt_verdict
        settled()

        # Terminate through the Runtime Host, then prove the recorded provider group is gone and the run ended.
        terminate = httpx.post(f"{api_url}/api/agents/sessions/{session_id}/terminate-live", headers=headers, timeout=30)
        terminate_dispatched = not terminate.is_error and terminate.json().get("terminate_dispatched") is True
        ended = poll(
            lambda: (current if _run_lifecycle(current) == "ended" else None) if (current := served_state()) else None,
            timeout=min(timeout, 20.0),
            interval=0.25,
        )
        provider_dead = provider_pid is not None and wait_pid_dead(provider_pid)
        group_dead = provider_pid is not None and wait_process_group_dead(provider_pid)
        final_state = ended or served_state()
        lifecycle["terminate_final_state"] = {
            "run": (final_state or {}).get("run"),
            "activity": (final_state or {}).get("activity"),
            "keys": sorted(final_state) if isinstance(final_state, dict) else None,
        }
        lifecycle["terminate_owned"] = terminate_held(
            dispatched=terminate_dispatched,
            provider_dead=provider_dead,
            group_dead=group_dead,
            run_lifecycle=_run_lifecycle(final_state),
        )
        report.update({"status": "passed" if all(isinstance(v, dict) and v.get("passed", True) for v in lifecycle.values()) else "failed"})
    except Exception as exc:  # noqa: BLE001 - the report carries what was observed up to the failure
        report.update(failed_report(exc))
        raise
    finally:
        cleanup = _cleanup(run, args, report, terminal, shipper, lane, provider_pid)
        run.fields["session_id"] = report.get("session_id")
        run.observation = {"lifecycle": lifecycle, "error": report.get("error"), **cleanup["required_cleanup"]}
        write_json(root / "helm-lifecycle-report.json", report)
        run.assertions = lifecycle_assertions(report, cleanup_ok=cleanup["status"] == "pass")
        run.fields["redacted_secret_files"] = secret_scan(root, list(qualification_secrets(dict(os.environ), args.agents_token)))
        if not all(run.assertions.values()):
            run.fields["failure_code"] = "antigravity_helm_lifecycle_failed"
            run.fields.update(declared_wait(report.get(WAITED_ON_FIELD)))


def _keep_provider_logs(root: Path) -> None:
    """agy's own logs, so a run that never reached its prompt says why. They hold no credential: the profile has none."""

    try:
        home = isolated_provider_home()
        logs = home / ".gemini" / "antigravity-cli"
        target = root / "provider-logs"
        target.mkdir(exist_ok=True)
        for path in [logs / "cli.log", *sorted((logs / "log").glob("*.log"))[-2:]]:
            if path.is_file():
                (target / path.name).write_bytes(path.read_bytes()[-200_000:])
    except Exception:  # noqa: BLE001 - evidence only
        pass


def _cleanup(
    run: Run,
    args: argparse.Namespace,
    report: dict[str, Any],
    terminal: ProviderPtySession | None,
    shipper: TranscriptShipper | None,
    lane: lane_module.ScriptedLane | None,
    provider_pid: int | None,
) -> dict[str, Any]:
    root = run.root
    if terminal is not None:
        terminal.close()
    if shipper is not None:
        try:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        except Exception:  # noqa: BLE001 - best-effort final stop
            pass
    _keep_provider_logs(root)
    if lane is not None:
        write_json(root / "scripted-model-receipt.json", lane.receipt())
        lane.close()
    provider_dead = provider_pid is None or wait_pid_dead(provider_pid)
    group_dead = provider_pid is None or wait_process_group_dead(provider_pid)
    terminal_gone = terminal is None or not terminal.alive()
    required = {
        # A passing run proves teardown through served state; any other run was stopped by the close above.
        "session_stopped": (report.get("status") == "passed") or (provider_dead and terminal_gone),
        "no_orphan_provider_processes": provider_dead and group_dead and terminal_gone,
    }
    receipt = {
        "schema_version": 1,
        "artifact_kind": "antigravity_helm_lifecycle_cleanup_receipt",
        "session_id": report.get("session_id"),
        "status": "pass" if all(required.values()) else "fail",
        "orphan_count": 0 if required["no_orphan_provider_processes"] else None,
        "required_cleanup": required,
        "diagnostics": {"provider_pid": provider_pid, "provider_process_dead": provider_dead, "process_group_dead": group_dead},
    }
    write_json(root / "cleanup-receipt.json", receipt)
    return receipt


def run_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout-secs", type=float, default=90.0)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="direct_helm_lifecycle_result",
    failure_code="antigravity_helm_lifecycle_harness_failed",
    needs=("variant", "evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin"),
    binaries=(
        ("engine", "longhouse_engine_missing"),
        ("longhouse_cli", "longhouse_cli_missing"),
        ("provider_bin", "antigravity_binary_missing"),
    ),
    runtime_host=True,
    add_arguments=_add_arguments,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_lifecycle, argv)


if __name__ == "__main__":
    raise SystemExit(main())
