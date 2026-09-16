#!/usr/bin/env python3
"""Stock-Codex Helm lifecycle producer: send, steer, abort, terminate.

One detached Codex Helm bridge (the same ``longhouse-engine codex-bridge``
dispatch functions the Machine Agent control channel calls) is driven through
four control operations against the real provider binary with a live token.
Every verdict is read from the provider's own rollout, correlated by the
app-server ``turn_id``, not from command exit codes:

- ``codex_helm_send_idle`` -- an idle session accepts a message; the turn id
  ``send`` returned carries that message and completes with the marker reply.
- ``codex_helm_steer_active`` -- a message delivered while a multi-tool turn
  is active is recorded inside that *same* turn, before its ``task_complete``;
  that turn's final answer is the steer marker; no other turn starts; and the
  later tool calls never run. A queued follow-up (new turn after completion)
  fails.
- ``codex_helm_abort_native`` -- ``turn/interrupt`` ends the active turn as
  interrupted, and a following send on the same thread completes a new turn.
- ``codex_helm_terminate_owned`` -- stop leaves a terminal bridge state, no
  control socket, and every recorded bridge/app-server pid dead.

``--negative-control`` runs one target phase against an engine built with the
``qa-fault-injection`` feature (``engine/src/qa_fault.rs``). The run passes
only when the fault receipt proves the fault fired and the target assertion
failed; anything else is ``inconclusive`` or ``undetected``.
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

from zerg.qa import codex_provider_release_canary as bridge_canary
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import redact_state_for_evidence
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "codex_helm_lifecycle"
SEND_IDLE = "codex_helm_send_idle"
STEER_ACTIVE = "codex_helm_steer_active"
ABORT_NATIVE = "codex_helm_abort_native"
TERMINATE_OWNED = "codex_helm_terminate_owned"
ASSERTIONS = (SEND_IDLE, STEER_ACTIVE, ABORT_NATIVE, TERMINATE_OWNED)
_VARIANTS = tuple(execution_variant_key(provider="codex", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS)

# Negative control name -> (engine fault, target assertion).
NEGATIVE_CONTROLS = {
    "send": ("codex_send_noop", SEND_IDLE),
    "steer": ("codex_steer_as_follow_up", STEER_ACTIVE),
    "abort": ("codex_interrupt_noop", ABORT_NATIVE),
}
_STEP_COUNT = 6
_STEER_STEP = "LH_STEER_STEP_"
_ABORT_STEP = "LH_ABORT_STEP_"

REGISTRATION = ProducerRegistration(
    producer_id="codex.helm_lifecycle.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("codex",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "idle_send_turn_completed_with_marker",
        "steer_recorded_inside_active_turn",
        "steered_turn_final_answer_is_steer_marker",
        "later_tool_calls_absent_after_steer",
        "interrupt_ended_active_turn",
        "follow_up_turn_completed_after_interrupt",
        "owned_bridge_processes_dead",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "control_command_receipts",
        "provider_rollout",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "final_bridge_stopped",
        "final_socket_absent",
        "no_orphan_provider_processes",
    ),
    implementation="server/zerg/qa/codex_helm_lifecycle.py",
    oracle_source="server/zerg/qa/codex_helm_lifecycle.py",
    oracle_entrypoint="codex_helm_lifecycle_assertions",
    executable_module="zerg.qa.codex_helm_lifecycle",
    observation_scope="scenario",
)


# --- rollout reading -------------------------------------------------------


def _rollout(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else {}


def _item_text(item: Any) -> str:
    return json.dumps(item, sort_keys=True) if item is not None else ""


def _user_message_turns(records: list[dict[str, Any]], marker: str) -> list[tuple[int, str]]:
    """(index, turn_id) of every turn-correlated user message carrying marker."""

    found = []
    for index, record in enumerate(records):
        payload = _payload(record)
        item = payload.get("item")
        if payload.get("type") == "item_completed" and isinstance(item, dict) and item.get("type") == "UserMessage":
            if marker in _item_text(item.get("content")):
                found.append((index, str(payload.get("turn_id") or "")))
    return found


def _task_events(records: list[dict[str, Any]], event_type: str) -> list[tuple[int, dict[str, Any]]]:
    return [(index, _payload(record)) for index, record in enumerate(records) if _payload(record).get("type") == event_type]


def _task_complete(records: list[dict[str, Any]], turn_id: str) -> tuple[int, dict[str, Any]] | None:
    for index, payload in _task_events(records, "task_complete"):
        if str(payload.get("turn_id") or "") == turn_id:
            return index, payload
    return None


def _turn_records(records: list[dict[str, Any]], turn_id: str) -> list[dict[str, Any]]:
    """Records from this turn's task_started up to the next turn's task_started."""

    starts = [index for index, _payload_ in _task_events(records, "task_started")]
    for index, payload in _task_events(records, "task_started"):
        if turn_id and str(payload.get("turn_id") or "") == turn_id:
            later = [start for start in starts if start > index]
            return records[index : later[0] if later else len(records)]
    return []


def _tool_call_started(records: list[dict[str, Any]], needle: str) -> bool:
    for record in records:
        payload = _payload(record)
        if payload.get("type") in {"function_call", "custom_tool_call"} and needle in _item_text(
            payload.get("arguments") or payload.get("input")
        ):
            return True
    return False


# --- oracles ---------------------------------------------------------------


def send_idle_holds(obs: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    turn_id = str(obs.get("turn_id") or "")
    if not turn_id or not obs.get("quiescent_before_send"):
        return False
    carriers = {turn for _index, turn in _user_message_turns(records, obs["marker"])}
    complete = _task_complete(records, turn_id)
    return (
        carriers == {turn_id}
        and complete is not None
        and obs["marker"] in str(complete[1].get("last_agent_message") or "")
        and obs.get("final_turn_status") == "completed"
    )


def steer_active_holds(obs: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    """The steer must land inside the active turn, not queue behind it."""

    turn_id = str(obs.get("steered_turn_id") or "")
    if not turn_id or obs.get("steer_returncode") != 0 or obs.get("send_turn_id") != turn_id:
        return False
    carriers = _user_message_turns(records, obs["marker"])
    complete = _task_complete(records, turn_id)
    if len(carriers) != 1 or complete is None:
        return False
    steer_index, steer_turn = carriers[0]
    complete_index, complete_payload = complete
    other_turn_started = any(
        str(payload.get("turn_id") or "") != turn_id
        for index, payload in _task_events(records, "task_started")
        if steer_index < index < complete_index
    )
    return (
        steer_turn == turn_id
        and steer_index < complete_index
        and obs["marker"] in str(complete_payload.get("last_agent_message") or "")
        and not other_turn_started
        and not _tool_call_started(_turn_records(records, turn_id), f"{_STEER_STEP}{_STEP_COUNT}")
    )


def abort_native_holds(obs: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    aborted = str(obs.get("aborted_turn_id") or "")
    follow_up = str(obs.get("follow_up_turn_id") or "")
    if not aborted or obs.get("interrupt_returncode") != 0:
        return False
    if obs.get("aborted_terminal_status") not in {"interrupted", "cancelled"}:
        return False
    if _tool_call_started(_turn_records(records, aborted), f"{_ABORT_STEP}{_STEP_COUNT}"):
        return False
    complete = _task_complete(records, follow_up) if follow_up else None
    return (
        bool(follow_up)
        and follow_up != aborted
        and complete is not None
        and obs["follow_up_marker"] in str(complete[1].get("last_agent_message") or "")
        and obs.get("follow_up_terminal_status") == "completed"
    )


def terminate_owned_holds(obs: dict[str, Any]) -> bool:
    verification = obs.get("stop_verification") or {}
    return bool(
        verification.get("verified")
        and verification.get("socket_absent")
        and verification.get("owned_processes_dead")
        and obs.get("recorded_pids")
        and not obs.get("recorded_pids_alive")
    )


def codex_helm_lifecycle_assertions(observations: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, bool]:
    return {
        SEND_IDLE: "send" in observations and send_idle_holds(observations["send"], records),
        STEER_ACTIVE: "steer" in observations and steer_active_holds(observations["steer"], records),
        ABORT_NATIVE: "abort" in observations and abort_native_holds(observations["abort"], records),
        TERMINATE_OWNED: "terminate" in observations and terminate_owned_holds(observations["terminate"]),
    }


# --- driver ----------------------------------------------------------------


class _Session:
    def __init__(self, args: argparse.Namespace, root: Path) -> None:
        self.args = args
        self.root = root
        self.isolation_root: Path | None = None
        self.session_id = ""
        self.state_file = Path()
        self.receipts: list[dict[str, Any]] = []

    def engine(self, verb: str, *extra: str, timeout: int = 120) -> Any:
        argv = [
            str(self.args.engine),
            "codex-bridge",
            verb,
            "--session-id",
            self.session_id,
            *extra,
            "--state-root",
            str(bridge_canary._bridge_state_root(self.isolation_root)),
        ]
        completed = bridge_canary._run(argv, cwd=self.args.repo_root, timeout=timeout)
        self.receipts.append(
            {
                "verb": verb,
                "returncode": completed.returncode,
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
                "at": now(),
            }
        )
        return completed

    def state(self) -> dict[str, Any]:
        try:
            return bridge_canary._read_json(self.state_file)
        except (OSError, json.JSONDecodeError):
            return {}

    def records(self) -> list[dict[str, Any]]:
        return _rollout(Path(str(self.state().get("thread_path") or "")))

    def wait(self, predicate, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.25)
        return predicate()

    def send(self, text: str) -> tuple[int, str]:
        completed = self.engine("send", "--text", text, "--json")
        try:
            turn_id = str(json.loads(completed.stdout).get("turn_id") or "")
        except (json.JSONDecodeError, AttributeError):
            turn_id = ""
        return completed.returncode, turn_id

    def terminal(self) -> bool:
        return bridge_canary._terminal_turn_state(self.state())


def _steps_prompt(prefix: str, seconds: int) -> str:
    commands = ", ".join(f"`sleep {seconds}; echo {prefix}{n}`" for n in range(1, _STEP_COUNT + 1))
    return (
        "Run these shell commands one at a time, each as its own separate tool call, waiting for each to finish "
        f"before starting the next: {commands}. After all of them, reply DONE."
    )


def _phase_send(session: _Session, timeout: float) -> dict[str, Any]:
    marker = f"LH_CODEX_SEND_{uuid.uuid4().hex}"
    obs: dict[str, Any] = {"marker": marker, "quiescent_before_send": not session.state().get("active_turn_id")}
    returncode, turn_id = session.send(f"Reply exactly {marker} and nothing else. Do not use tools.")
    obs.update({"send_returncode": returncode, "turn_id": turn_id})
    session.wait(lambda: session.terminal() and _task_complete(session.records(), turn_id) is not None, timeout)
    obs["final_turn_status"] = session.state().get("last_turn_status")
    return obs


def _phase_steer(session: _Session, timeout: float) -> dict[str, Any]:
    marker = f"LH_CODEX_STEER_{uuid.uuid4().hex}"
    obs: dict[str, Any] = {"marker": marker}
    returncode, turn_id = session.send(_steps_prompt(_STEER_STEP, 4))
    obs.update({"send_returncode": returncode, "send_turn_id": turn_id})
    # Steer only once the turn is demonstrably mid-flight: its first tool call
    # has started and the turn has not completed.
    mid_turn = session.wait(
        lambda: (
            session.state().get("active_turn_id") == turn_id
            and _tool_call_started(_turn_records(session.records(), turn_id), f"{_STEER_STEP}1")
        ),
        timeout,
    )
    obs["mid_turn_observed"] = mid_turn
    obs["steered_turn_id"] = str(session.state().get("active_turn_id") or "")
    completed = session.engine(
        "steer",
        "--text",
        f"STOP. Do not run any more commands. Reply exactly {marker} and nothing else.",
        timeout=int(timeout) + 60,
    )
    obs["steer_returncode"] = completed.returncode
    session.wait(
        lambda: (
            session.terminal() and any(_task_complete(session.records(), t) for _i, t in _user_message_turns(session.records(), marker))
        ),
        timeout,
    )
    # A turn that ignored the steer keeps running its steps; let it settle so
    # the abort phase starts from an idle session.
    session.wait(session.terminal, timeout)
    obs["final_turn_status"] = session.state().get("last_turn_status")
    return obs


def _phase_abort(session: _Session, timeout: float) -> dict[str, Any]:
    follow_up_marker = f"LH_CODEX_AFTER_ABORT_{uuid.uuid4().hex}"
    obs: dict[str, Any] = {"follow_up_marker": follow_up_marker}
    returncode, turn_id = session.send(_steps_prompt(_ABORT_STEP, 8))
    obs.update({"send_returncode": returncode, "aborted_turn_id": turn_id})
    obs["mid_turn_observed"] = session.wait(
        lambda: (
            session.state().get("active_turn_id") == turn_id
            and _tool_call_started(_turn_records(session.records(), turn_id), f"{_ABORT_STEP}1")
        ),
        timeout,
    )
    obs["interrupt_returncode"] = session.engine("interrupt", timeout=60).returncode
    session.wait(session.terminal, timeout)
    obs["aborted_terminal_status"] = session.state().get("last_turn_status")
    returncode, follow_up = session.send(f"Reply exactly {follow_up_marker} and nothing else. Do not use tools.")
    obs.update({"follow_up_send_returncode": returncode, "follow_up_turn_id": follow_up})
    session.wait(lambda: session.terminal() and _task_complete(session.records(), follow_up) is not None, timeout)
    obs["follow_up_terminal_status"] = session.state().get("last_turn_status")
    return obs


def _phase_terminate(session: _Session) -> dict[str, Any]:
    state = session.state()
    recorded = sorted({int(value) for value in (state.get("pid"), state.get("app_server_pid")) if isinstance(value, int) and value > 1})
    stop = bridge_canary._stop_bridge(session.args, session.session_id, session.isolation_root)
    return {
        "recorded_pids": recorded,
        "recorded_app_server_pgid": state.get("app_server_pgid"),
        "recorded_pids_alive": [pid for pid in recorded if bridge_canary._pid_alive(pid)],
        "stop_verification": {key: value for key, value in (stop.get("verification") or {}).items() if key != "state"},
        "stop_evidence": stop.get("evidence"),
    }


def _fault_receipts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_codex_helm_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "provider": "codex",
        "path": str(args.codex_bin),
        "sha256": sha256_file(args.codex_bin),
        "version": bridge_canary._run([str(args.codex_bin), "--version"], timeout=30).stdout.strip(),
    }
    write_json(root / "provider-binary-receipt.json", provider_receipt)
    negative = getattr(args, "negative_control", None)
    fault_receipt_path = root / "qa-fault-receipt.jsonl"
    if negative:
        fault, _target = NEGATIVE_CONTROLS[negative]
        os.environ["LONGHOUSE_QA_FAULT"] = fault
        os.environ["LONGHOUSE_QA_FAULT_RECEIPT"] = str(fault_receipt_path)

    session = _Session(args, root)
    observations: dict[str, Any] = {}
    failure: BaseException | None = None
    timeout = float(args.live_send_timeout_secs)
    try:
        # Keep the socket path under Linux's SUN_LEN inside the sandbox.
        session.isolation_root = Path(tempfile.mkdtemp(prefix="lch-", dir="/tmp"))
        summary, _result, session.isolation_root = bridge_canary._start_bridge(
            args,
            evidence_root=root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
            isolation_root=session.isolation_root,
        )
        session.session_id = str(summary.get("session_id") or "")
        session.state_file = Path(str(summary.get("state_file") or ""))
        if not session.session_id or not session.state_file.is_file() or not summary.get("thread_id"):
            raise RuntimeError("Codex Helm bridge did not start a provider thread")
        phases = {"send": _phase_send, "steer": _phase_steer, "abort": _phase_abort}
        selected = (negative,) if negative else ("send", "steer", "abort")
        for name in selected:
            observations[name] = phases[name](session, timeout)
    except Exception as exc:  # noqa: BLE001 - retained as a typed failure
        failure = exc
    finally:
        records = session.records() if session.session_id else []
        if records:
            write_json(root / "provider-rollout.json", records)
        if session.session_id and session.isolation_root:
            write_json(root / "final-bridge-state.json", redact_state_for_evidence(session.state()))
            observations["terminate"] = _phase_terminate(session)
        cleanup = observations.get("terminate", {}).get("stop_verification", {})
        write_json(
            root / "cleanup-receipt.json",
            {
                "schema_version": 1,
                "artifact_kind": "codex_helm_lifecycle_cleanup_receipt",
                "status": "pass" if cleanup.get("verified") else "fail",
                "orphan_count": len(observations.get("terminate", {}).get("recorded_pids_alive") or [])
                + (0 if cleanup.get("owned_processes_dead") else 1),
                "required_cleanup": {
                    "final_bridge_stopped": bool(cleanup.get("verified")),
                    "final_socket_absent": bool(cleanup.get("socket_absent")),
                    "no_orphan_provider_processes": bool(cleanup.get("owned_processes_dead")),
                },
            },
        )
        write_json(root / "control-command-receipts.json", session.receipts)
        if session.isolation_root:
            shutil.rmtree(session.isolation_root, ignore_errors=True)
        if negative:
            os.environ.pop("LONGHOUSE_QA_FAULT", None)
            os.environ.pop("LONGHOUSE_QA_FAULT_RECEIPT", None)

    assertions = codex_helm_lifecycle_assertions(observations, records)
    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "codex_helm_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "codex",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "observation_scope": "scenario",
        "generated_at": now(),
        "assertions": assertions,
        "observation": observations,
        "provider_binary": provider_receipt,
    }
    if negative:
        fault, target = NEGATIVE_CONTROLS[negative]
        fired = [receipt for receipt in _fault_receipts(fault_receipt_path) if receipt.get("session_id") == session.session_id]
        if failure is not None or not fired:
            verdict = "inconclusive"
        elif assertions[target]:
            verdict = "undetected"
        else:
            verdict = "rejected"
        result["negative_control"] = {
            "fault": fault,
            "target_assertion": target,
            "fault_fired": bool(fired),
            "target_outcome": "semantic_fail" if not assertions[target] else "pass",
            "verdict": verdict,
        }
        result["status"] = "pass" if verdict == "rejected" and assertions[TERMINATE_OWNED] else "fail"
    else:
        result["status"] = "pass" if failure is None and all(assertions.values()) else "fail"
    if failure is not None:
        result["failure_code"] = "codex_helm_lifecycle_failed"
        result["error"] = f"{type(failure).__name__}: {failure}"
    result["artifact_manifest"] = artifact_manifest(root)
    write_json(root / "result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=_VARIANTS)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("CODEX_MODEL"))
    parser.add_argument("--bridge-start-timeout-secs", type=int, default=60)
    parser.add_argument("--live-send-timeout-secs", type=int, default=180)
    parser.add_argument("--negative-control", choices=tuple(NEGATIVE_CONTROLS))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    if not args.negative_control and not args.variant:
        print(json.dumps({"status": "fail", "failure_code": "missing_required_argument:--variant"}))
        return 2
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    require_disposable_runtime(args.api_url)
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    args.script_bin = None
    args.timeout_bin = None
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    for path, code in ((args.engine, "longhouse_engine_missing"), (args.codex_bin, "codex_binary_missing")):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": code}))
            return 2
    result = run_codex_helm_lifecycle(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
