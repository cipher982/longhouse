#!/usr/bin/env python3
"""Direct stock-Codex Helm Resume producer used by the trusted factory plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa import codex_provider_release_canary as bridge_canary
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.provider_resume_oracles import native_resume_assertions
from zerg.qa.resume_assurance import ProducerRegistration

_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"

REGISTRATION = ProducerRegistration(
    producer_id="codex.native_resume.v1",
    producer_revision=1,
    scenario_id="helm_cold_resume",
    scenario_revision=4,
    assertion_cells=(
        ("native_provider_resume_proven", "clean_exit"),
        ("native_provider_resume_proven", "process_loss"),
    ),
    providers=("codex",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "provider_neutral_resume_intent",
        "native_resume_command",
        "post_resume_provider_activity",
        "stale_input_rejected",
        "concurrent_resume_refused",
        "artifact_secret_scan_passed",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "transcript_shipper_receipt",
        "resume_intent_receipt",
        "initial_bridge_state",
        "initial_transcript",
        "native_resume_terminal_recording",
        "resumed_bridge_state",
        "resumed_transcript",
        "process_transition_receipt",
        "stale_input_receipt",
        "concurrent_resume_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "old_owner_dead",
        "final_bridge_stopped",
        "final_socket_absent",
        "no_orphan_provider_processes",
    ),
    implementation="server/zerg/qa/codex_native_resume.py",
    oracle_source="server/zerg/qa/provider_resume_oracles.py",
    oracle_entrypoint="native_resume_assertions",
    executable_module="zerg.qa.codex_native_resume",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


class _RuntimeHostHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"Runtime Host HTTP {status}: {detail}")


def _api_json(api_url: str, token: str, path: str, *, method: str = "GET") -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/api/agents/{path.lstrip('/')}",
        headers={
            "X-Agents-Token": token,
            "Accept": "application/json",
            "User-Agent": _RUNTIME_HOST_USER_AGENT,
        },
        data=b"" if method == "POST" else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            raw_detail = exc.read(4096).decode("utf-8", "replace")
        except OSError:
            raw_detail = ""
        try:
            parsed_detail = json.loads(raw_detail)
        except json.JSONDecodeError:
            parsed_detail = raw_detail[:1000]
        if isinstance(parsed_detail, dict):
            detail = str(parsed_detail.get("detail") or parsed_detail)[:1000]
        else:
            detail = str(parsed_detail)[:1000]
        raise _RuntimeHostHTTPError(exc.code, detail) from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime Host returned a non-object")
    return payload


def _wait_resume_intent(args: argparse.Namespace, session_id: str, *, timeout: float = 45) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_reason = "resume intent was not projected"
    while time.monotonic() < deadline:
        try:
            intent = _api_json(
                args.api_url,
                args.agents_token,
                f"sessions/{session_id}/resume-intent",
                method="POST",
            )
        except _RuntimeHostHTTPError as exc:
            if exc.status in {401, 403}:
                raise
            last_reason = str(exc)
            time.sleep(0.5)
            continue
        except (OSError, urllib.error.URLError) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
            continue
        if intent.get("available") is True:
            return intent
        last_reason = str(intent.get("reason") or "resume intent unavailable")
        time.sleep(0.5)
    raise RuntimeError(f"provider-neutral Resume intent remained unavailable: {last_reason}")


def _validate_resume_intent(args: argparse.Namespace, session_id: str, intent: dict[str, Any]) -> dict[str, Any]:
    expected_argv = ["longhouse", "codex", "--cwd", str(args.repo_root), "--resume-session", session_id]
    identity_valid = (
        intent.get("available") is True
        and intent.get("session_id") == session_id
        and intent.get("provider") == "codex"
        and intent.get("cwd") == str(args.repo_root)
        and intent.get("handoff") == "terminal_command"
        and intent.get("argv") == expected_argv
    )
    if not identity_valid:
        raise RuntimeError("provider-neutral Resume intent did not match the exact Codex session, cwd, and native selector")
    return {
        "requested_at": _now(),
        "intent": intent,
        "identity_valid": identity_valid,
    }


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()
        if len(fields) > 2 and fields[2] == "Z":
            return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_command(pid: int) -> str:
    try:
        return (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        return ""


def _process_start_time(pid: int) -> str:
    result = bridge_canary._run(["ps", "-p", str(pid), "-o", "lstart="], timeout=5)
    return result.stdout.strip() if result.returncode == 0 else ""


def _wait_dead(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _pid_alive(pid)


def _force_process_loss(state: dict[str, Any], codex_bin: Path) -> dict[str, Any]:
    bridge_pid = int(state.get("pid") or 0)
    app_server_pid = int(state.get("app_server_pid") or 0)
    bridge_command = _proc_command(bridge_pid)
    app_server_command = _proc_command(app_server_pid)
    bridge_start_time = _process_start_time(bridge_pid)
    app_server_start_time = _process_start_time(app_server_pid)
    if not state.get("bridge_process_start_time") or not state.get("app_server_process_start_time"):
        raise RuntimeError("recorded process start identity is unavailable")
    if not _pid_alive(bridge_pid) or "longhouse-engine" not in bridge_command:
        raise RuntimeError("recorded bridge process identity is not live")
    if not _pid_alive(app_server_pid) or codex_bin.name not in app_server_command:
        raise RuntimeError("recorded Codex app-server process identity is not live")
    if state["bridge_process_start_time"] != bridge_start_time:
        raise RuntimeError("recorded bridge process start identity no longer matches")
    if state["app_server_process_start_time"] != app_server_start_time:
        raise RuntimeError("recorded Codex app-server process start identity no longer matches")
    os.kill(bridge_pid, signal.SIGKILL)
    if _pid_alive(app_server_pid):
        os.kill(app_server_pid, signal.SIGKILL)
    bridge_dead = _wait_dead(bridge_pid)
    app_server_dead = _wait_dead(app_server_pid)
    if not app_server_dead or not bridge_dead:
        raise RuntimeError("process-loss injection did not terminate the exact recorded owners")
    return {
        "method": "sigkill_exact_recorded_processes",
        "bridge": {
            "pid": bridge_pid,
            "recorded_start_time": state.get("bridge_process_start_time"),
            "observed_start_time": bridge_start_time,
            "command": bridge_command,
            "dead": bridge_dead,
        },
        "app_server": {
            "pid": app_server_pid,
            "recorded_start_time": state.get("app_server_process_start_time"),
            "observed_start_time": app_server_start_time,
            "command": app_server_command,
            "dead": app_server_dead,
        },
    }


def _send_marker(args: argparse.Namespace, isolation_root: Path, session_id: str, marker: str) -> dict[str, Any]:
    result = bridge_canary._run(
        [
            str(args.engine),
            "codex-bridge",
            "send",
            "--session-id",
            session_id,
            "--text",
            f"Reply exactly {marker} and nothing else.",
            "--state-root",
            str(bridge_canary._bridge_state_root(isolation_root)),
            "--json",
        ],
        cwd=args.repo_root,
        timeout=args.live_send_timeout_secs + 20,
    )
    if result.returncode:
        raise RuntimeError(f"codex-bridge send failed with status {result.returncode}: {result.stderr[-1000:]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("codex-bridge send returned invalid JSON") from exc


def _attempt_stale_input(args: argparse.Namespace, isolation_root: Path, session_id: str) -> dict[str, Any]:
    marker = f"LONGHOUSE_CODEX_STALE_INPUT_{uuid.uuid4().hex}"
    result = bridge_canary._run(
        [
            str(args.engine),
            "codex-bridge",
            "send",
            "--session-id",
            session_id,
            "--text",
            f"Reply exactly {marker} and nothing else.",
            "--state-root",
            str(bridge_canary._bridge_state_root(isolation_root)),
            "--json",
        ],
        cwd=args.repo_root,
        timeout=30,
    )
    return {
        "marker": marker,
        "rejected": result.returncode != 0,
        "evidence": bridge_canary._command_evidence(result, secrets=[args.agents_token]),
    }


def _attempt_concurrent_resume(
    args: argparse.Namespace,
    *,
    root: Path,
    isolation_root: Path,
    session_id: str,
    thread_id: str,
    thread_path: Path,
    active_state_file: Path,
    active_run_id: str,
    active_bridge_pid: int,
) -> dict[str, Any]:
    attempt_root = root / "concurrent-resume-attempt"
    attempt_root.mkdir()
    try:
        summary, _, _ = bridge_canary._start_bridge(
            args,
            evidence_root=attempt_root,
            codex_bin=str(args.codex_bin),
            launch_mode="tui",
            session_id=session_id,
            isolation_root=isolation_root,
            resume_thread_id=thread_id,
            resume_thread_path=str(thread_path),
        )
    except RuntimeError as exc:
        try:
            active_state = bridge_canary._read_json(active_state_file)
        except (OSError, json.JSONDecodeError):
            active_state = {}
        owner_preserved = (
            active_state.get("run_id") == active_run_id and active_state.get("pid") == active_bridge_pid and _pid_alive(active_bridge_pid)
        )
        return {"rejected": owner_preserved, "owner_preserved": owner_preserved, "error": str(exc)}
    cleanup = bridge_canary._stop_bridge(args, session_id, isolation_root)
    return {"rejected": False, "unexpected_start": summary, "cleanup": cleanup}


def _redact_retained_secrets(root: Path, secrets: list[str]) -> list[str]:
    encoded = [secret.encode() for secret in secrets if secret]
    redacted: list[str] = []
    if not encoded:
        return redacted
    for path in root.rglob("*"):
        if not path.is_file() or path.name == "result.json":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        replaced = data
        for secret in encoded:
            replaced = replaced.replace(secret, b"<redacted>")
        if replaced != data:
            path.write_bytes(replaced)
            redacted.append(path.relative_to(root).as_posix())
    return redacted


def _wait_for_marker(state_file: Path, marker: str, *, timeout: int) -> tuple[dict[str, Any], Path]:
    deadline = time.monotonic() + timeout
    state = bridge_canary._read_json(state_file)
    while not bridge_canary._terminal_turn_state(state) and time.monotonic() < deadline:
        time.sleep(0.25)
        state = bridge_canary._read_json(state_file)
    thread_path = Path(str(state.get("thread_path") or ""))
    if (
        state.get("last_turn_status") != "completed"
        or not thread_path.is_file()
        or not bridge_canary._assistant_transcript_contains(thread_path, marker)
    ):
        raise RuntimeError("Codex turn did not retain the expected assistant marker")
    return state, thread_path


def _native_resume_tui_environment(isolation_root: Path, session_id: str) -> dict[str, str]:
    environment = bridge_canary._provider_runtime_environment(os.environ, isolation_root)
    environment["LONGHOUSE_MANAGED_SESSION_ID"] = session_id
    return environment


def run_native_resume(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.codex_bin),
        "sha256": _sha256(args.codex_bin),
        "version": bridge_canary._run([str(args.codex_bin), "--version"], timeout=30).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    isolation_root: Path | None = None
    session_id = ""
    old_pids: set[int] = set()
    final_cleanup: dict[str, Any] = {"verified": False}
    shipper: TranscriptShipper | None = None
    try:
        initial_summary, _, isolation_root = bridge_canary._start_bridge(
            args,
            evidence_root=root,
            codex_bin=str(args.codex_bin),
            launch_mode="detached_ui",
        )
        session_id = str(initial_summary.get("session_id") or "")
        initial_state_file = Path(str(initial_summary.get("state_file") or ""))
        initial_state = bridge_canary._read_json(initial_state_file)
        thread_id = str(initial_state.get("thread_id") or "")
        if not session_id or not thread_id:
            raise RuntimeError("initial bridge did not create a resumable provider thread")
        shipper_environment = bridge_canary._provider_runtime_environment(os.environ, isolation_root)
        shipper = _start_transcript_shipper(
            "codex",
            args,
            home=Path(shipper_environment["HOME"]),
            environment=shipper_environment,
            evidence_root=root,
        )
        _write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        seed_marker = f"LONGHOUSE_CODEX_RESUME_SEED_{uuid.uuid4().hex}"
        _send_marker(args, isolation_root, session_id, seed_marker)
        initial_state, initial_thread_path = _wait_for_marker(initial_state_file, seed_marker, timeout=args.live_send_timeout_secs)
        _write_json(root / "initial-bridge-state.json", initial_state)
        shutil.copy2(initial_thread_path, root / "initial-transcript.jsonl")
        old_pids = {int(value) for value in (initial_state.get("pid"), initial_state.get("app_server_pid")) if value}

        if args.variant == "clean_exit":
            process_transition = bridge_canary._stop_bridge(args, session_id, isolation_root)
            if not (process_transition.get("verification") or {}).get("verified"):
                raise RuntimeError("clean-exit transition did not stop the initial bridge")
        else:
            process_transition = _force_process_loss(initial_state, args.codex_bin)
        _write_json(root / "process-transition-receipt.json", process_transition)
        stale_input_receipt = _attempt_stale_input(args, isolation_root, session_id)
        _write_json(root / "stale-input-receipt.json", stale_input_receipt)
        if stale_input_receipt["rejected"] is not True:
            raise RuntimeError("input addressed to the terminated Resume generation was accepted")

        resume_intent = _wait_resume_intent(args, session_id)
        resume_intent_receipt = _validate_resume_intent(args, session_id, resume_intent)

        resumed_summary, _, _ = bridge_canary._start_bridge(
            args,
            evidence_root=root,
            codex_bin=str(args.codex_bin),
            launch_mode="tui",
            session_id=session_id,
            isolation_root=isolation_root,
            resume_thread_id=thread_id,
            resume_thread_path=str(initial_thread_path),
        )
        resumed_state_file = Path(str(resumed_summary.get("state_file") or ""))
        ws_url = str(resumed_summary.get("ws_url") or "")
        recording = root / "native-resume.tty"
        command = [
            str(args.codex_bin),
            "resume",
            thread_id,
            "-c",
            "check_for_update_on_startup=false",
            "--enable",
            "tui_app_server",
            "--remote",
            ws_url,
            "--no-alt-screen",
        ]
        native_resume_command = command[:3] == [str(args.codex_bin), "resume", thread_id]
        resume_intent_receipt.update(
            {
                "provider_thread_id": thread_id,
                "native_argv": command,
                "session_to_provider_thread_mapping_valid": bool(session_id and thread_id),
                "native_selector_valid": native_resume_command,
            }
        )
        _write_json(root / "resume-intent-receipt.json", resume_intent_receipt)
        subscribed_state: dict[str, Any] | None = None

        def subscribed() -> bool:
            nonlocal subscribed_state
            try:
                candidate = bridge_canary._read_json(resumed_state_file)
            except (OSError, json.JSONDecodeError):
                return False
            if candidate.get("thread_subscription_status") == "subscribed" and candidate.get("thread_id") == thread_id:
                subscribed_state = candidate
                return True
            return False

        tui_env = _native_resume_tui_environment(isolation_root, session_id)
        tui_result = bridge_canary._record_pty_session(args, command, recording, env=tui_env, ready=subscribed)
        if tui_result.returncode not in {0, 124} or subscribed_state is None:
            raise RuntimeError("stock Codex native resume command did not subscribe to the retained thread")
        resumed_before_activity = subscribed_state
        post_marker = f"LONGHOUSE_CODEX_RESUME_POST_{uuid.uuid4().hex}"
        send_summary = _send_marker(args, isolation_root, session_id, post_marker)
        resumed_state, resumed_thread_path = _wait_for_marker(resumed_state_file, post_marker, timeout=args.live_send_timeout_secs)
        stale_generation_dispatched = bridge_canary._assistant_transcript_contains(
            resumed_thread_path,
            str(stale_input_receipt["marker"]),
        )
        stale_input_receipt["assistant_marker_observed_after_resume"] = stale_generation_dispatched
        _write_json(root / "stale-input-receipt.json", stale_input_receipt)
        _write_json(root / "resumed-bridge-state.json", resumed_state)
        _write_json(root / "post-resume-send.json", send_summary)
        shutil.copy2(resumed_thread_path, root / "resumed-transcript.jsonl")
        concurrent_resume_receipt = _attempt_concurrent_resume(
            args,
            root=root,
            isolation_root=isolation_root,
            session_id=session_id,
            thread_id=thread_id,
            thread_path=resumed_thread_path,
            active_state_file=resumed_state_file,
            active_run_id=str(resumed_state.get("run_id") or ""),
            active_bridge_pid=int(resumed_state.get("pid") or 0),
        )
        _write_json(root / "concurrent-resume-receipt.json", concurrent_resume_receipt)

        final_cleanup = bridge_canary._stop_bridge(args, session_id, isolation_root)
        verification = final_cleanup.get("verification") or {}
        remaining_old = sorted(pid for pid in old_pids if _pid_alive(pid))
        resumed_pids = {int(value) for value in (resumed_state.get("pid"), resumed_state.get("app_server_pid")) if value}
        for pid in resumed_pids:
            _wait_dead(pid)
        remaining_resumed = sorted(pid for pid in resumed_pids if _pid_alive(pid))
        orphan_count = len(set(remaining_old + remaining_resumed))
        final_cleanup.update(
            {
                "old_processes_still_alive": remaining_old,
                "resumed_processes_still_alive": remaining_resumed,
                "orphan_count": orphan_count,
            }
        )
        _write_json(root / "cleanup-receipt.json", final_cleanup)
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        redacted_secret_files = _redact_retained_secrets(
            root,
            [args.agents_token, os.environ.get("CODEX_API_KEY", "")],
        )

        observation = {
            "variant": args.variant,
            "same_longhouse_session": resumed_state.get("session_id") == session_id,
            "same_provider_thread": resumed_state.get("thread_id") == thread_id,
            "new_run": resumed_state.get("run_id") != initial_state.get("run_id"),
            "new_connection": resumed_state.get("connection_id") != initial_state.get("connection_id"),
            "new_app_server_process": (
                resumed_state.get("app_server_pid"),
                resumed_state.get("app_server_process_start_time"),
            )
            != (
                initial_state.get("app_server_pid"),
                initial_state.get("app_server_process_start_time"),
            ),
            "provider_neutral_resume_intent": (
                resume_intent_receipt["identity_valid"] is True
                and resume_intent_receipt["session_to_provider_thread_mapping_valid"] is True
            ),
            "native_resume_command": native_resume_command,
            "bridge_subscribed": resumed_before_activity.get("thread_subscription_status") == "subscribed",
            "post_resume_provider_activity": resumed_state.get("last_turn_status") == "completed",
            "post_resume_marker_in_assistant_transcript": bridge_canary._assistant_transcript_contains(resumed_thread_path, post_marker),
            "stale_input_rejected": stale_input_receipt["rejected"] is True,
            "stale_generation_dispatched": stale_generation_dispatched,
            "concurrent_resume_refused": concurrent_resume_receipt["rejected"] is True,
            "artifact_secret_scan_passed": not redacted_secret_files,
            "clean_stop_verified": args.variant == "clean_exit" and bool((process_transition.get("verification") or {}).get("verified")),
            "old_bridge_process_dead": args.variant == "process_loss" and bool((process_transition.get("bridge") or {}).get("dead")),
            "old_app_server_process_dead": args.variant == "process_loss"
            and bool((process_transition.get("app_server") or {}).get("dead")),
            "replacement_started_after_process_loss": args.variant == "process_loss"
            and resumed_state.get("run_id") != initial_state.get("run_id"),
            "final_cleanup_verified": bool(verification.get("verified")),
            "final_socket_absent": verification.get("socket_absent") is True,
            "orphan_count": orphan_count,
        }
        assertions = native_resume_assertions(args.variant, observation)
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "direct_native_resume_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": args.variant,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "pass" if assertions["native_provider_resume_proven"] else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": session_id,
            "provider_thread_id": thread_id,
            "provider_binary": provider_receipt,
            "seed_marker": seed_marker,
            "post_resume_marker": post_marker,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        redacted_secret_files = _redact_retained_secrets(
            root,
            [args.agents_token, os.environ.get("CODEX_API_KEY", "")],
        )
        failure = {
            "schema_version": 1,
            "artifact_kind": "direct_native_resume_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": args.variant,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "direct_native_resume_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure
    finally:
        if shipper is not None:
            _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        if session_id and isolation_root and not (final_cleanup.get("verification") or {}).get("verified"):
            cleanup = bridge_canary._stop_bridge(args, session_id, isolation_root)
            if not (root / "cleanup-receipt.json").exists():
                _write_json(root / "cleanup-receipt.json", cleanup)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=("clean_exit", "process_loss"))
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True, type=Path)
    parser.add_argument("--model", default=os.environ.get("CODEX_MODEL"))
    parser.add_argument("--bridge-start-timeout-secs", type=int, default=30)
    parser.add_argument("--live-send-timeout-secs", type=int, default=120)
    parser.add_argument("--tui-record-secs", type=int, default=30)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    args.api_url = os.environ.get("CODEX_API_URL", "")
    args.agents_token = os.environ.get("CODEX_AGENTS_TOKEN", "")
    args.script_bin = None
    args.timeout_bin = None
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    if not args.engine.is_file() or not os.access(args.engine, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_engine_missing"}))
        return 2
    if not args.codex_bin.is_file() or not os.access(args.codex_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "codex_binary_missing"}))
        return 2
    result = run_native_resume(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
