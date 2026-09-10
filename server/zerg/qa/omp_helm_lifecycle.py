#!/usr/bin/env python3
"""Bounded OMP Helm qualification using the native extension channel."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa import provider_release_identity as identity
from zerg.qa import provider_semantic_qualification as semantic
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.services.provider_capability_proof import AssertionOutcome
from zerg.services.provider_capability_proof import EvidenceClass

SCENARIO_ID = "omp_helm_lifecycle"
ASSERTIONS = (
    "omp_helm_launch_registration",
    "omp_helm_send_idle",
    "omp_helm_steer_active",
    "omp_helm_abort_native",
    "omp_helm_terminate_owned",
    "omp_helm_cold_resume_exact_file",
    "omp_helm_stale_owner_refused",
    "omp_helm_native_replacement_bound",
)
_VARIANTS = tuple(
    execution_variant_key(
        provider="omp",
        assertion_id=assertion,
        scenario_id=SCENARIO_ID,
        variant=None,
    )
    for assertion in ASSERTIONS
)

REGISTRATION = ProducerRegistration(
    producer_id="omp.helm_lifecycle.v1",
    producer_revision=3,
    scenario_id=SCENARIO_ID,
    scenario_revision=3,
    assertion_cells=tuple((assertion, None) for assertion in ASSERTIONS),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "omp_native_extension_channel_bound",
        "omp_agent_end_settlement_observed",
        "omp_native_archive_bound",
        "omp_owned_processes_dead",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "omp_helm_receipt",
        "omp_native_settlement_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/omp_helm_lifecycle.py",
    oracle_source="server/zerg/qa/omp_helm_lifecycle.py",
    oracle_entrypoint="omp_helm_lifecycle_assertions",
    executable_module="zerg.qa.omp_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)
PROFILE = "omp_helm_v1"
_PROFILE = identity.IdentityProfile(
    provider="omp",
    profile=PROFILE,
    scenario_id=SCENARIO_ID,
    version_line=identity.semver_version_line(),
    oracle_source=Path(__file__),
)


def omp_helm_lifecycle_assertions(observation: Mapping[str, object]) -> dict[str, bool]:
    settlement = observation.get("settlement")
    settlement = settlement if isinstance(settlement, Mapping) else {}
    cleanup = observation.get("cleanup")
    cleanup = cleanup if isinstance(cleanup, Mapping) else {}
    cleanup_ok = (
        cleanup.get("status") == "pass"
        and cleanup.get("provider_process_dead") is True
        and cleanup.get("process_group_dead") is True
        and cleanup.get("orphan_count") == 0
    )
    return {
        "omp_helm_launch_registration": observation.get("omp_native_extension_channel_bound") is True,
        "omp_helm_send_idle": observation.get("send_idle") is True,
        "omp_helm_steer_active": observation.get("steer_active") is True,
        "omp_helm_abort_native": observation.get("abort_native") is True and settlement.get("agent_end_terminal") is True,
        "omp_helm_terminate_owned": observation.get("terminate_owned") is True and cleanup_ok,
        "omp_helm_cold_resume_exact_file": observation.get("cold_resume_exact_file") is True
        and settlement.get("native_archive_bound") is True,
        "omp_helm_stale_owner_refused": observation.get("stale_owner_refused") is True,
        "omp_helm_native_replacement_bound": observation.get("native_replacement_bound") is True,
    }


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _wait(
    observe: Any,
    *,
    timeout: float,
    description: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        last = observe()
        if last is not None:
            return last
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}: {last!r}")


def _wait_state(
    home: Path,
    *,
    session_id: str | None = None,
    timeout: float = 30,
    predicate: Any = None,
) -> dict[str, Any]:
    state_dir = home / "managed-local" / "omp-helm"

    def observe() -> dict[str, Any] | None:
        for path in sorted(state_dir.glob("*.json")):
            state = _read_state(path)
            if not state or state.get("ready") is not True:
                continue
            if session_id is not None and state.get("session_id") != session_id:
                continue
            if predicate is not None and not predicate(state):
                continue
            return state
        return None

    return _wait(observe, timeout=timeout, description="OMP Helm state")


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
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        for row in _native_rows(session_file, minimum_offset):
            if row.get("type") != "message":
                continue
            message = row.get("message")
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue
            if marker in json.dumps(message, sort_keys=True):
                return row
        return None

    return _wait(observe, timeout=timeout, description=f"OMP native marker {marker}")


def _wait_native_terminal(
    session_file: Path,
    *,
    minimum_offset: int = 0,
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        for row in _native_rows(session_file, minimum_offset):
            if row.get("type") != "agent_end":
                continue
            if row.get("isTerminal") is True or row.get("willContinue") is False:
                return row
        return None

    return _wait(observe, timeout=timeout, description="OMP native terminal agent_end")


def _native_settlement(session_file: Path) -> dict[str, object]:
    rows = _native_rows(session_file)
    agent_end_terminal = any(
        row.get("type") == "agent_end" and (row.get("isTerminal") is True or row.get("willContinue") is False) for row in rows
    )
    native_archive_bound = any(row.get("type") == "session" and row.get("id") for row in rows)
    return {
        "status": "pass" if agent_end_terminal and native_archive_bound else "fail",
        "agent_end_terminal": agent_end_terminal,
        "native_archive_bound": native_archive_bound,
        "agent_settled_is_not_completion_contract": True,
        "session_file": str(session_file),
    }


def _stale_frame(old_state: Mapping[str, Any], *, text: str) -> dict[str, Any]:
    socket_path = str(old_state.get("socket_path") or "")
    if not socket_path:
        raise RuntimeError("old OMP Helm state has no socket path")
    request = {
        "kind": "send",
        "session_id": old_state.get("session_id"),
        "native_session_id": old_state.get("native_session_id"),
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


def _run_engine(engine: Path, command: str, session_id: str, env: Mapping[str, str], *, text: str | None = None) -> dict[str, object]:
    argv = [str(engine), "omp-helm", command, "--session-id", session_id]
    if text is not None:
        argv.extend(("--text", text))
    completed = subprocess.run(argv, env=dict(env), capture_output=True, text=True, timeout=20, check=False)
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "accepted": completed.returncode == 0 and isinstance(payload, Mapping) and payload.get("ok") is True,
        "payload": payload,
    }


def _launch_argv(
    args: argparse.Namespace,
    *,
    workspace: Path,
    prompt: str,
    resume_session: str | None = None,
) -> list[str]:
    argv = [
        str(args.longhouse_cli),
        "omp",
        "--cwd",
        str(workspace),
        "--omp-bin",
        str(args.provider_bin),
        "--url",
        str(args.api_url),
        "--token",
        str(args.agents_token),
        "--prompt",
        prompt,
    ]
    if getattr(args, "model", None):
        argv.extend(("--model", str(args.model)))
    if resume_session is not None:
        argv.extend(("--resume-session", resume_session))
    return argv


def _wait_stopped(
    longhouse_home: Path,
    session_id: str,
    *,
    timeout: float = 30,
) -> dict[str, Any]:
    state_path = longhouse_home / "managed-local" / "omp-helm" / f"{session_id}.json"

    def observe() -> dict[str, Any] | None:
        state = _read_state(state_path)
        if state and state.get("status") == "stopped":
            return state
        return None

    return _wait(observe, timeout=timeout, description="OMP Helm remote termination")


def _read_source_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _wait_native_agent_end(
    session_file: Path,
    *,
    minimum_offset: int,
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        for row in _native_rows(session_file, minimum_offset):
            if row.get("type") == "agent_end":
                return row
        return None

    return _wait(observe, timeout=timeout, description="OMP native agent_end")


def _process_record(pid: object, expected_birth: object, label: str) -> dict[str, Any]:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return {"label": label, "pid": pid, "birth_matches": False, "pgid": None, "alive": False}
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
        "label": label,
        "pid": pid,
        "pgid": pgid,
        "birth": birth,
        "expected_birth": expected_birth,
        "birth_matches": bool(birth and expected_birth and birth == str(expected_birth)),
        "alive": _pid_alive(pid),
    }


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_dead(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _pgid_dead(pgid: object) -> bool:
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _cleanup_receipt(records: list[dict[str, Any]]) -> dict[str, Any]:
    unique = {(item.get("label"), item.get("pid"), item.get("pgid"), item.get("expected_birth")): item for item in records}
    records = list(unique.values())
    provider_records = [item for item in records if item.get("label") == "provider"]
    provider_dead = bool(provider_records) and all(_pid_dead(item.get("pid")) for item in provider_records)
    groups_dead = bool(records) and all(_pgid_dead(item.get("pgid")) for item in records)
    birth_verified = bool(records) and all(item.get("birth_matches") is True for item in records)
    orphan_count = sum(not (_pid_dead(item.get("pid")) and _pgid_dead(item.get("pgid"))) for item in records)
    return {
        "status": "pass" if provider_dead and groups_dead and birth_verified and orphan_count == 0 else "fail",
        "provider_process_dead": provider_dead,
        "process_group_dead": groups_dead,
        "no_orphan_provider_processes": orphan_count == 0 and birth_verified,
        "birth_identities_verified": birth_verified,
        "orphan_count": orphan_count,
        "owned_processes": records,
    }


def run_omp_helm(args: argparse.Namespace) -> dict[str, object]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.environ.get("LONGHOUSE_OMP_LIVE") not in {"1", "true", "yes", "on"}:
        raise RuntimeError("OMP Helm qualification requires explicit LONGHOUSE_OMP_LIVE opt-in")
    if not args.api_url or not args.agents_token:
        raise RuntimeError("OMP Helm qualification requires Runtime Host URL and token")
    isolation = root / "isolation"
    provider_home = isolation / "home"
    longhouse_home = provider_home / ".longhouse"
    workspace = isolation / "workspace"
    provider_home.mkdir(mode=0o700, parents=True)
    workspace.mkdir(mode=0o700, parents=True)
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(provider_home),
            "LONGHOUSE_HOME": str(longhouse_home),
            "LONGHOUSE_OMP_BIN": str(args.provider_bin),
            "LONGHOUSE_OMP_HELM_URL": str(args.api_url),
            "LONGHOUSE_OMP_HELM_TOKEN": str(args.agents_token),
            "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
            "LONGHOUSE_LAUNCH_ACTOR": "automation",
            "LONGHOUSE_LAUNCH_SURFACE": "qa",
            "XDG_DATA_HOME": str(provider_home / ".local" / "share"),
            "LONGHOUSE_OMP_DATA_DIR": str(provider_home / ".local" / "share" / "omp"),
            "LONGHOUSE_OMP_SESSION_DIR": str(provider_home / ".local" / "share" / "omp" / "sessions"),
        }
    )

    sessions: list[ProviderPtySession] = []
    owner_records: list[dict[str, Any]] = []
    controls: dict[str, object] = {}
    observation: dict[str, object] = {
        "omp_native_extension_channel_bound": False,
        "omp_agent_end_settlement_observed": False,
        "omp_native_archive_bound": False,
        "send_idle": False,
        "steer_active": False,
        "abort_native": False,
        "terminate_owned": False,
        "cold_resume_exact_file": False,
        "stale_owner_refused": False,
        "settlement": {},
        "cleanup": {},
    }
    current_session_id: str | None = None
    current_session_file: Path | None = None
    current_state: dict[str, Any] = {}

    try:
        initial_marker = f"OMP_HELM_INITIAL_{os.urandom(8).hex()}"
        first = ProviderPtySession.start(
            argv=_launch_argv(
                args,
                workspace=workspace,
                prompt=f"Reply with exactly {initial_marker}.",
            ),
            cwd=workspace,
            env=env,
            terminal_path=root / "omp-helm-terminal.raw",
            thread_name="omp-helm-qualification-terminal-drain",
        )
        sessions.append(first)
        current_state = _wait_state(longhouse_home)
        current_session_id = str(current_state["session_id"])
        current_session_file = Path(str(current_state["session_file"]))
        initial_row = _wait_native_marker(current_session_file, initial_marker)
        _wait_native_terminal(current_session_file, minimum_offset=int(initial_row["_source_offset"]))
        owner_records.append(
            _process_record(
                current_state.get("launcher_pid"),
                current_state.get("launcher_process_start_time"),
                "launcher",
            )
        )
        owner_records.append(
            _process_record(
                current_state.get("provider_pid"),
                current_state.get("provider_process_start_time"),
                "provider",
            )
        )
        old_state = dict(current_state)
        old_native_id = str(current_state.get("native_session_id") or "")
        observation["omp_native_extension_channel_bound"] = bool(
            current_state.get("ready") is True
            and current_state.get("connection_id")
            and current_state.get("lease_generation")
            and current_state.get("native_session_id")
            and current_state.get("session_file")
        )

        send_marker = f"OMP_HELM_SEND_{os.urandom(8).hex()}"
        send_offset = _read_source_size(current_session_file)
        send = _run_engine(args.engine, "send", current_session_id, env, text=f"Reply with exactly {send_marker}.")
        send_row = _wait_native_marker(current_session_file, send_marker, minimum_offset=send_offset)
        controls["send"] = {"command": send, "marker_row": send_row}
        observation["send_idle"] = send.get("accepted") is True

        active_marker = f"OMP_HELM_STEER_{os.urandom(8).hex()}"
        active_offset = _read_source_size(current_session_file)
        active = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Run the shell command `sleep 8`, then reply with exactly {active_marker}.",
        )
        _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: value.get("phase") in {"running", "thinking"},
            timeout=30,
        )
        steer = _run_engine(args.engine, "steer", current_session_id, env, text=f"Reply with exactly {active_marker}.")
        steer_row = _wait_native_marker(current_session_file, active_marker, minimum_offset=active_offset)
        controls["steer"] = {"active_command": active, "command": steer, "marker_row": steer_row}
        observation["steer_active"] = active.get("accepted") is True and steer.get("accepted") is True

        abort_marker = f"OMP_HELM_ABORT_{os.urandom(8).hex()}"
        active_for_abort = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Run the shell command `sleep 15`, then reply with exactly {abort_marker}.",
        )
        _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: value.get("phase") in {"running", "thinking"},
            timeout=30,
        )
        abort_offset = _read_source_size(current_session_file)
        abort = _run_engine(args.engine, "abort", current_session_id, env)
        abort_end = _wait_native_agent_end(current_session_file, minimum_offset=abort_offset)
        controls["abort"] = {
            "active_command": active_for_abort,
            "command": abort,
            "agent_end": abort_end,
        }
        observation["abort_native"] = abort.get("accepted") is True and abort_end.get("type") == "agent_end"

        first.submit_line("/new")
        replaced_state = _wait_state(
            longhouse_home,
            session_id=current_session_id,
            predicate=lambda value: bool(value.get("native_session_id"))
            and value.get("native_session_id") != old_native_id
            and value.get("ready") is True,
            timeout=60,
        )
        stale = _stale_frame(old_state, text="stale OMP owner must be refused")
        controls["stale_owner"] = {"response": stale, "old_state": old_state, "new_state": replaced_state}
        observation["stale_owner_refused"] = stale.get("ok") is False and (stale.get("error") or {}).get("code") == "stale_channel"
        current_state = replaced_state
        current_session_file = Path(str(replaced_state["session_file"]))
        current_native_id = str(replaced_state["native_session_id"])
        replacement_marker = f"OMP_HELM_REPLACEMENT_{os.urandom(8).hex()}"
        replacement_offset = _read_source_size(current_session_file)
        replacement = _run_engine(
            args.engine,
            "send",
            current_session_id,
            env,
            text=f"Reply with exactly {replacement_marker}.",
        )
        replacement_row = _wait_native_marker(
            current_session_file,
            replacement_marker,
            minimum_offset=replacement_offset,
        )
        controls["replacement"] = {"command": replacement, "marker_row": replacement_row}
        observation["native_replacement_bound"] = (
            replacement.get("accepted") is True
            and replaced_state.get("native_session_id") == current_native_id
            and replacement_row.get("_source_offset", -1) >= replacement_offset
        )

        owner_records.append(
            _process_record(
                current_state.get("launcher_pid"),
                current_state.get("launcher_process_start_time"),
                "launcher",
            )
        )
        owner_records.append(
            _process_record(
                current_state.get("provider_pid"),
                current_state.get("provider_process_start_time"),
                "provider",
            )
        )
        terminate = _run_engine(args.engine, "terminate", current_session_id, env)
        stopped = _wait_stopped(longhouse_home, current_session_id)
        first.process.wait(timeout=15)
        controls["terminate"] = {"command": terminate, "stopped_state": stopped}
        observation["terminate_owned"] = terminate.get("accepted") is True and stopped.get("terminal_reason") == "remote_terminate"

        resume_marker = f"OMP_HELM_RESUME_{os.urandom(8).hex()}"
        resumed = ProviderPtySession.start(
            argv=_launch_argv(
                args,
                workspace=workspace,
                prompt=f"Reply with exactly {resume_marker}.",
                resume_session=current_session_id,
            ),
            cwd=workspace,
            env=env,
            terminal_path=root / "omp-helm-resume-terminal.raw",
            thread_name="omp-helm-resume-qualification-terminal-drain",
        )
        sessions.append(resumed)
        resume_state = _wait_state(longhouse_home, session_id=current_session_id)
        resume_file = Path(str(resume_state["session_file"]))
        resume_row = _wait_native_marker(resume_file, resume_marker)
        resume_terminal = _wait_native_terminal(
            resume_file,
            minimum_offset=int(resume_row["_source_offset"]),
        )
        resume_owner_records = [
            _process_record(
                resume_state.get("launcher_pid"),
                resume_state.get("launcher_process_start_time"),
                "launcher",
            ),
            _process_record(
                resume_state.get("provider_pid"),
                resume_state.get("provider_process_start_time"),
                "provider",
            ),
        ]
        owner_records.extend(resume_owner_records)
        observation["cold_resume_exact_file"] = (
            resume_state.get("native_session_id") == current_native_id
            and resume_state.get("session_file") == str(current_session_file)
            and resume_file == current_session_file
            and resume_row.get("_source_offset", -1) >= 0
            and resume_terminal.get("type") == "agent_end"
        )
        controls["cold_resume"] = {
            "state": resume_state,
            "marker_row": resume_row,
            "terminal": resume_terminal,
        }
        observation["settlement"] = _native_settlement(resume_file)
        final_terminate = _run_engine(args.engine, "terminate", current_session_id, env)
        final_stopped = _wait_stopped(longhouse_home, current_session_id)
        resumed.process.wait(timeout=15)
        controls["final_terminate"] = {"command": final_terminate, "stopped_state": final_stopped}
    finally:
        for provider_session in sessions:
            if provider_session.alive():
                try:
                    os.killpg(provider_session.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    provider_session.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(provider_session.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            provider_session.close()
        cleanup = (
            _cleanup_receipt(owner_records)
            if owner_records
            else {
                "status": "fail",
                "provider_process_dead": False,
                "process_group_dead": False,
                "orphan_count": 1,
            }
        )
        observation["cleanup"] = cleanup

    observation["omp_owned_processes_dead"] = cleanup.get("provider_process_dead") is True and cleanup.get("process_group_dead") is True
    binary_receipt = {
        "provider": "omp",
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": args.provider_version,
    }
    lifecycle.write_json(root / "provider-binary-receipt.json", binary_receipt)
    lifecycle.write_json(root / "omp-native-settlement-receipt.json", observation["settlement"])
    lifecycle.write_json(
        root / "omp-helm-receipt.json",
        {
            "managed_transport": "omp_helm_channel",
            "state": current_state,
            "old_state": old_state if "old_state" in locals() else {},
            "controls": controls,
            "observation": observation,
        },
    )
    lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
    assertions = omp_helm_lifecycle_assertions(observation)
    result = {
        "schema_version": 1,
        "artifact_kind": "omp_helm_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "omp",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": "pass" if all(assertions.values()) else "fail",
        "assertions": assertions,
        "provider_binary": binary_receipt,
        "observation": observation,
        "artifact_manifest": artifact_manifest(root),
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def _request_args(request_path: Path, output_root: Path) -> argparse.Namespace:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    return argparse.Namespace(
        evidence_root=output_root,
        provider_bin=Path(str(request["provider_bin"])),
        provider_version=str(request["expected_provider_version"]),
        engine=Path(os.environ.get("LONGHOUSE_ENGINE_BIN") or ""),
        longhouse_cli=Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse"),
        api_url=os.environ.get("LONGHOUSE_RUNTIME_API_URL"),
        agents_token=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"),
        model=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""),
    )


def run(request_path: Path, output_root: Path) -> dict[str, object]:
    identity.load_request(
        request_path,
        provider="omp",
        profile=PROFILE,
        version_grammar=_PROFILE.version_grammar,
    )

    def execute(binary: Path, evidence_root: Path):
        args = _request_args(request_path, evidence_root)
        args.provider_bin = binary
        result = run_omp_helm(args)
        observation = dict(result.get("observation") or {})
        assertions = omp_helm_lifecycle_assertions(observation)
        semantic_assertions = tuple(
            semantic.SemanticAssertion(
                assertion,
                AssertionOutcome.PASS if passed else AssertionOutcome.SEMANTIC_FAIL,
                EvidenceClass.LIVE_TOKEN,
            )
            for assertion, passed in assertions.items()
        )
        return (
            observation,
            semantic_assertions,
            tuple(value for name in ("OPENROUTER_API_KEY",) if (value := str(os.environ.get(name) or "").strip())),
        )

    return semantic.run_semantic_profile(
        request_path,
        output_root,
        profile=_PROFILE,
        assertion_ids=ASSERTIONS,
        executor=execute,
        oracle_source=Path(__file__),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS)
    parser.add_argument("--model", required=False)
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_RUNTIME_API_URL"))
    parser.add_argument("--agents-token", default=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    try:
        result = run_omp_helm(args)
    except Exception as exc:  # noqa: BLE001 - preserve a typed harness failure
        result = {
            "schema_version": 1,
            "artifact_kind": "omp_helm_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "omp",
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "omp_helm_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
