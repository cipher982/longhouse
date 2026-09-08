#!/usr/bin/env python3
"""Live Pi Helm lifecycle proof through the stock Longhouse facade.

This producer never calls Pi directly.  The provider is launched by the stock
``longhouse pi`` facade, and every control assertion is exercised through the
paired ``longhouse-engine pi-helm`` command surface.  Native Pi JSONL and the
Runtime Host state diagnostic are the two independent observation sources.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.request import Request
from urllib.request import urlopen

from zerg.qa.console_served_state_core import assistant_marker_events
from zerg.qa.console_served_state_core import event_text
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.provider_adapters.pi import pi_native_shadow_taxonomy
from zerg.qa.provider_adapters.pi import pi_transcript_rows
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "pi_helm_lifecycle"
ASSERTIONS = (
    "pi_helm_launch_registration",
    "pi_helm_native_tool_taxonomy",
    "pi_helm_send_idle",
    "pi_helm_steer_active",
    "pi_helm_follow_up_native",
    "pi_helm_abort_native",
    "pi_helm_terminate_owned",
    "pi_helm_reload_rebind",
    "pi_helm_session_replacement_rebind",
    "pi_helm_cold_resume_exact_file",
    "pi_helm_stale_owner_refused",
)
_VARIANTS = tuple(execution_variant_key(provider="pi", assertion_id=item, scenario_id=SCENARIO_ID, variant=None) for item in ASSERTIONS)

REGISTRATION = ProducerRegistration(
    producer_id="pi.helm_lifecycle.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("pi",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "stock_longhouse_pi_helm_launch",
        "pi_native_session_jsonl_bound",
        "pi_helm_channel_command_accepted",
        "pi_helm_reload_and_session_replacement_observed",
        "pi_helm_exact_cold_resume",
        "pi_helm_stale_owner_rejected",
        "owned_processes_dead",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("pi_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "helm_registration_receipt",
        "native_session_receipt",
        "native_taxonomy_receipt",
        "control_command_receipts",
        "reload_rebind_receipt",
        "session_replacement_receipt",
        "cold_resume_receipt",
        "stale_owner_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/pi_helm_lifecycle.py",
    oracle_source="server/zerg/qa/pi_helm_lifecycle.py",
    oracle_entrypoint="pi_helm_lifecycle_assertions",
    executable_module="zerg.qa.pi_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object at {path}")
    return value


def _wait(predicate, *, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {description}: {last}")


def _state_paths(home: Path) -> list[Path]:
    return sorted((home / "managed-local" / "pi-helm").glob("*.json"))


def _wait_state(home: Path, *, session_id: str | None = None, predicate=None, timeout: float = 30.0) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        for path in _state_paths(home):
            try:
                state = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if session_id is not None and state.get("session_id") != session_id:
                continue
            if predicate is None or predicate(state):
                return state
        return None

    return _wait(observe, timeout=timeout, description="Pi Helm state")


def _run_engine(engine: Path, kind: str, session_id: str, env: dict[str, str], *, text: str | None = None) -> dict[str, Any]:
    command = [str(engine), "pi-helm", kind, "--session-id", session_id]
    if text is not None:
        command.extend(("--text", text))
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20, check=False)
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-2000:],
        "stderr": completed.stderr[-2000:],
        "payload": payload,
        "accepted": completed.returncode == 0 and payload.get("ok") is True,
    }


def _runtime_get(url: str, token: str, path: str) -> dict[str, Any]:
    request = Request(
        f"{url.rstrip('/')}{path}",
        headers={"X-Agents-Token": token, "Accept": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    return payload if isinstance(payload, dict) else {}


def _runtime_diagnostic(url: str, token: str, session_id: str) -> dict[str, Any]:
    return _runtime_get(url, token, f"/api/agents/sessions/{session_id}/state-diagnostics")


def _runtime_snapshot(url: str, token: str, session_id: str) -> dict[str, Any]:
    """Read the canonical served session surfaces, not a synthetic turn route."""

    return {
        "detail": _runtime_get(url, token, f"/api/agents/sessions/{session_id}"),
        "thread": _runtime_get(url, token, f"/api/agents/sessions/{session_id}/thread"),
        "events": _runtime_get(url, token, f"/api/agents/sessions/{session_id}/events?limit=200"),
        "diagnostic": _runtime_diagnostic(url, token, session_id),
    }


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


def _runtime_binding(snapshot: dict[str, Any], *, session_id: str, provider_session_id: str) -> dict[str, Any]:
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
        _nested_values(
            {"detail": detail, "thread": thread, "events": events},
            {"provider_session_id", "provider_thread_id", "alias_value"},
        )
    )
    return {
        "session_id": served_session_id,
        "thread_root_session_id": root_session_id,
        "thread_head_session_id": head_session_id,
        "provider": detail.get("provider"),
        "provider_session_id": provider_session_id,
        "provider_session_bound": provider_session_id in provider_ids,
        "one_session": served_session_id == session_id and (not event_session_ids or event_session_ids == {session_id}),
        "one_thread": root_session_id == head_session_id == session_id and member_ids == {session_id},
        "event_count": len(events),
    }


def _served_controls(snapshot: dict[str, Any]) -> bool:
    diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), dict) else {}
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    control = shadow.get("control") if isinstance(shadow.get("control"), dict) else {}
    actions = control.get("actions") if isinstance(control.get("actions"), dict) else {}
    return (
        diagnostic.get("served_path") == "canonical_session_detail"
        and isinstance(actions.get("send_input"), dict)
        and actions["send_input"].get("state") == "available"
        and isinstance(actions.get("terminate"), dict)
    )


def _wait_runtime_convergence(
    url: str,
    token: str,
    session_id: str,
    provider_session_id: str,
    marker: str,
    *,
    timeout: float = 90,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        try:
            snapshot = _runtime_snapshot(url, token, session_id)
        except Exception:
            return None
        events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), dict) else {}
        events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
        matches = assistant_marker_events(events, marker, default_origin="durable")
        binding = _runtime_binding(snapshot, session_id=session_id, provider_session_id=provider_session_id)
        if (
            len(matches) == 1
            and event_text(matches[0]).count(marker) == 1
            and binding["one_session"]
            and binding["one_thread"]
            and binding["provider"] == "pi"
            and binding["provider_session_bound"]
            and _served_controls(snapshot)
        ):
            return {"snapshot": snapshot, "binding": binding, "assistant_events": matches}
        return None

    return _wait(observe, timeout=timeout, description=f"Runtime Host convergence for Pi marker {marker}")


def _native_snapshot(session_file: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    rows, provider_session_id, metadata = pi_transcript_rows(session_file)
    taxonomy = pi_native_shadow_taxonomy(rows, metadata)
    taxonomy["provider_session_id"] = provider_session_id
    return rows, metadata, taxonomy


def _wait_native_marker(
    session_file: Path,
    marker: str,
    *,
    minimum_source_offset: int = 0,
    timeout: float = 90.0,
) -> dict[str, Any]:
    def observe() -> dict[str, Any] | None:
        if not session_file.is_file():
            return None
        try:
            rows, metadata, taxonomy = _native_snapshot(session_file)
        except OSError:
            return None
        invocation_rows = [row for row in rows if int(row.get("source_offset") or 0) >= minimum_source_offset]
        assistant = [row for row in invocation_rows if row.get("role") == "assistant" and marker in str(row.get("text") or "")]
        return (
            {
                "rows": len(rows),
                "invocation_rows": invocation_rows,
                "metadata": metadata,
                "taxonomy": taxonomy,
                "assistant_marker_rows": len(assistant),
            }
            if assistant
            else None
        )

    return _wait(observe, timeout=timeout, description=f"native Pi marker {marker}")


def _stale_frame(state: dict[str, Any]) -> dict[str, Any]:
    frame = {
        "kind": "send",
        "auth_token": state.get("channel_token"),
        "session_id": state.get("session_id"),
        "provider_session_id": state.get("provider_session_id"),
        "connection_id": state.get("connection_id"),
        "lease_generation": state.get("lease_generation"),
        "text": "stale-channel-proof",
    }
    socket_path = str(state.get("socket_path") or "")
    if not socket_path:
        return {"accepted": False, "error_code": "socket_missing"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(5)
            channel.connect(socket_path)
            channel.sendall((json.dumps(frame) + "\n").encode())
            response = json.loads(channel.recv(8192).decode())
    except (OSError, json.JSONDecodeError) as exc:
        return {"accepted": False, "error_code": type(exc).__name__, "error": str(exc)}
    error = response.get("error") if isinstance(response, dict) else {}
    return {
        "accepted": response.get("ok") is True if isinstance(response, dict) else False,
        "error_code": error.get("code") if isinstance(error, dict) else None,
        "response": response,
    }


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
    pid_dead = bool(provider_records) and all(_pid_dead(item.get("pid")) for item in provider_records)
    groups_dead = bool(records) and all(_pgid_dead(item.get("pgid")) for item in records)
    birth_verified = bool(records) and all(item.get("birth_matches") is True for item in records)
    orphan_count = sum(not (_pid_dead(item.get("pid")) and _pgid_dead(item.get("pgid"))) for item in records)
    return {
        "status": "pass" if pid_dead and groups_dead and birth_verified and orphan_count == 0 else "fail",
        "provider_process_dead": pid_dead,
        "process_group_dead": groups_dead,
        "no_orphan_provider_processes": orphan_count == 0 and birth_verified,
        "birth_identities_verified": birth_verified,
        "orphan_count": orphan_count,
        "owned_processes": records,
    }


def _retain_source(root: Path, source: Path, name: str, secrets: list[str]) -> dict[str, Any]:
    target = root / "source-artifacts" / name
    try:
        content = source.read_bytes()
    except OSError as exc:
        return {"source": str(source), "retained": False, "error": f"{type(exc).__name__}: {exc}"}
    for secret in secrets:
        if secret:
            content = content.replace(secret.encode(), b"<redacted>")
    max_bytes = 16 * 1024 * 1024
    truncated = len(content) > max_bytes
    if truncated:
        content = content[:max_bytes] + b"\n[truncated by QA evidence bound]\n"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_bytes(content)
    return {
        "source": str(source),
        "path": str(target),
        "retained": True,
        "truncated": truncated,
        "bytes": len(content),
    }


def _redact_value(value: object, secrets: list[str]) -> object:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<redacted>")
        return value
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    return value


def pi_helm_lifecycle_assertions(observation: dict[str, Any]) -> dict[str, bool]:
    return {
        "pi_helm_launch_registration": observation.get("launch_registration") is True,
        "pi_helm_native_tool_taxonomy": observation.get("native_tool_taxonomy") is True,
        "pi_helm_send_idle": observation.get("send_idle") is True,
        "pi_helm_steer_active": observation.get("steer_active") is True,
        "pi_helm_follow_up_native": observation.get("follow_up_native") is True,
        "pi_helm_abort_native": observation.get("abort_native") is True,
        "pi_helm_terminate_owned": observation.get("terminate_owned") is True,
        "pi_helm_reload_rebind": observation.get("reload_rebind") is True,
        "pi_helm_session_replacement_rebind": (
            observation.get("session_replacement_rebind") is True
            and (observation.get("replacement_binding") or {}).get("native_file_present") is True
            and (observation.get("replacement_binding") or {}).get("provider_session_id")
            == (observation.get("replacement_binding") or {}).get("native_header_id")
            and (observation.get("replacement_runtime") or {}).get("binding", {}).get("one_session") is True
            and (observation.get("replacement_runtime") or {}).get("binding", {}).get("one_thread") is True
        ),
        "pi_helm_cold_resume_exact_file": observation.get("cold_resume_exact_file") is True,
        "pi_helm_stale_owner_refused": observation.get("stale_owner_refused") is True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=_VARIANTS)
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_RUNTIME_API_URL"))
    parser.add_argument("--agents-token", default=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"))
    parser.add_argument("--model", default=os.environ.get("LONGHOUSE_PI_QUALIFICATION_MODEL", "deepseek/deepseek-v4-flash-0731"))
    return parser


def run_pi_helm_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    isolation = Path(tempfile.mkdtemp(prefix="longhouse-pi-helm-", dir="/tmp"))
    provider_home = isolation / "provider-home"
    home = isolation / "longhouse"
    workspace = isolation / "workspace"
    workspace.mkdir(mode=0o700)
    provider_home.mkdir(mode=0o700, parents=True)
    (provider_home / ".pi" / "agent" / "sessions").mkdir(mode=0o700, parents=True)
    proof_file = workspace / "pi-tool-proof.txt"
    proof_marker = f"PI_HELM_TOOL_{os.urandom(8).hex()}"
    proof_file.write_text(proof_marker + "\n", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(provider_home),
            "LONGHOUSE_HOME": str(home),
            "PI_CODING_AGENT_DIR": str(provider_home / ".pi"),
            "LONGHOUSE_ENGINE_BIN": str(args.engine),
            "LONGHOUSE_PI_HELM_URL": str(args.api_url or os.environ.get("LONGHOUSE_RUNTIME_API_URL") or ""),
            "LONGHOUSE_PI_HELM_TOKEN": str(args.agents_token or os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN") or ""),
            "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
            "LONGHOUSE_LAUNCH_ACTOR": "automation",
            "LONGHOUSE_LAUNCH_SURFACE": "qa",
        }
    )
    observations: dict[str, Any] = {"proof_marker": proof_marker, "provider": "pi"}
    sessions: list[ProviderPtySession] = []
    owned_processes: list[dict[str, Any]] = []
    retained_sources: list[dict[str, Any]] = []
    shipper = None
    session_id = ""
    current_state: dict[str, Any] = {}
    failure: Exception | None = None
    try:
        if not env["LONGHOUSE_PI_HELM_URL"] or not env["LONGHOUSE_PI_HELM_TOKEN"]:
            raise RuntimeError("Runtime Host URL and token are required for Pi Helm proof")
        if args.repo_root is None:
            raise RuntimeError("Pi Helm proof requires the repository root for the real Machine Agent")
        shipper = start_transcript_shipper(
            "pi",
            args,
            home=provider_home,
            environment=env,
            evidence_root=root / "shipper",
            longhouse_home=home,
        )
        observations["machine_registration"] = {
            "status": shipper.receipt.get("status"),
            "machine_name": shipper.receipt.get("machine_name"),
            "authenticated": bool(shipper.receipt.get("machine_name")),
            "receipt": shipper.receipt,
        }
        marker = f"PI_HELM_LAUNCH_{os.urandom(8).hex()}"
        launch = ProviderPtySession.start(
            argv=[
                str(args.longhouse_cli),
                "pi",
                "--cwd",
                str(workspace),
                "--pi-bin",
                str(args.provider_bin),
                "--provider",
                "openrouter",
                "--model",
                args.model,
                "--prompt",
                f"Use the read tool to read {proof_file}, then reply with exactly {marker}.",
            ],
            cwd=workspace,
            env=env,
            terminal_path=root / "helm-terminal.raw",
            thread_name="pi-helm-qualification-terminal-drain",
        )
        sessions.append(launch)
        current_state = _wait_state(home, predicate=lambda item: item.get("ready") is True, timeout=45)
        session_id = str(current_state.get("session_id") or "")
        if not session_id:
            raise RuntimeError("Pi Helm state did not contain a session id")
        observations["session_id"] = session_id
        observations["provider_binary"] = str(args.provider_bin)
        observations["provider_version"] = args.provider_version
        observations["provider_binary_sha256"] = sha256_file(args.provider_bin)
        observations["provider_session_id"] = current_state.get("provider_session_id")
        for label in ("launcher", "provider"):
            owned_processes.append(
                _process_record(
                    current_state.get(f"{label}_pid"),
                    current_state.get(f"{label}_process_start_time"),
                    label,
                )
            )
        session_file = Path(str(current_state.get("session_file") or ""))
        if not session_file.is_file():
            session_dir = Path(str(current_state.get("session_dir") or ""))
            session_file = _wait(
                lambda: next(
                    (path for path in session_dir.rglob("*.jsonl") if path.is_file() and not path.is_symlink()),
                    None,
                ),
                timeout=45,
                description="Pi native session file",
            )
        native = _wait_native_marker(session_file, marker)
        observations["native_taxonomy"] = native["taxonomy"]
        observations["native_tool_taxonomy"] = bool(native["taxonomy"].get("tool_pairs")) and not bool(
            native["taxonomy"].get("tool_calls_without_results")
        )
        observations["session_file"] = str(session_file)
        launch_flush = shipper.flush("pi-helm-launch")
        runtime_launch = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(current_state.get("provider_session_id") or ""),
            marker,
        )
        observations["runtime_launch"] = {
            "flush": launch_flush,
            "binding": runtime_launch["binding"],
            "served_controls": _served_controls(runtime_launch["snapshot"]),
        }
        observations["launch_registration"] = (
            observations["machine_registration"]["authenticated"] is True
            and current_state.get("provider") == "pi"
            and current_state.get("ready") is True
            and current_state.get("status") in {"ready", "running"}
            and bool(str(current_state.get("session_dir") or ""))
            and bool(str(current_state.get("provider_session_id") or ""))
            and launch_flush.get("status") == "pass"
            and runtime_launch["binding"]["one_session"]
            and runtime_launch["binding"]["one_thread"]
            and runtime_launch["binding"]["provider_session_bound"]
            and _served_controls(runtime_launch["snapshot"])
        )
        if args.api_url and args.agents_token:
            diagnostic = _runtime_diagnostic(args.api_url, args.agents_token, session_id)
            observations["runtime_diagnostic"] = {
                "mode": (diagnostic.get("shadow") or {}).get("mode") if isinstance(diagnostic.get("shadow"), dict) else None,
                "launch_actor": (diagnostic.get("explain") or {}).get("launch_actor")
                if isinstance(diagnostic.get("explain"), dict)
                else None,
                "launch_surface": (diagnostic.get("explain") or {}).get("launch_surface")
                if isinstance(diagnostic.get("explain"), dict)
                else None,
            }
        send_marker = f"PI_HELM_SEND_{os.urandom(8).hex()}"
        send_offset = session_file.stat().st_size
        send = _run_engine(args.engine, "send", session_id, env, text=f"Reply with exactly {send_marker}.")
        send_native = _wait_native_marker(session_file, send_marker, minimum_source_offset=send_offset)
        send_flush = shipper.flush("pi-helm-send")
        runtime_send = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(current_state.get("provider_session_id") or ""),
            send_marker,
        )
        observations["send_idle"] = (
            send["accepted"]
            and bool(send_native["invocation_rows"])
            and send_native["assistant_marker_rows"] == 1
            and send_flush.get("status") == "pass"
            and runtime_send["binding"]["one_session"]
            and runtime_send["binding"]["one_thread"]
        )

        active_marker = f"PI_HELM_ACTIVE_{os.urandom(8).hex()}"
        active = _run_engine(
            args.engine,
            "send",
            session_id,
            env,
            text=f"Use the read tool repeatedly on {proof_file}, then reply with {active_marker}.",
        )
        try:
            active_state = _wait_state(
                home,
                session_id=session_id,
                predicate=lambda item: item.get("phase") in {"running", "thinking"},
                timeout=10,
            )
        except RuntimeError:
            active_state = {}
        steer_marker = f"PI_HELM_STEER_{os.urandom(8).hex()}"
        steer_offset = session_file.stat().st_size
        steer = _run_engine(args.engine, "steer", session_id, env, text=f"Change direction and finish with {steer_marker}.")
        steer_native = _wait_native_marker(session_file, steer_marker, minimum_source_offset=steer_offset)
        follow_marker = f"PI_HELM_FOLLOW_{os.urandom(8).hex()}"
        follow_offset = session_file.stat().st_size
        follow = _run_engine(args.engine, "follow-up", session_id, env, text=f"After this turn, reply with {follow_marker}.")
        follow_native = _wait_native_marker(session_file, follow_marker, minimum_source_offset=follow_offset)
        observations["steer_active"] = (
            active["accepted"]
            and bool(active_state)
            and steer["accepted"]
            and bool(steer_native["invocation_rows"])
            and steer_native["assistant_marker_rows"] == 1
        )
        observations["follow_up_native"] = (
            active["accepted"]
            and follow["accepted"]
            and bool(follow_native["invocation_rows"])
            and follow_native["assistant_marker_rows"] == 1
        )
        observations["control_receipts"] = {"send": send, "active": active, "steer": steer, "follow_up": follow}

        old_connection = dict(current_state)
        launch.submit_line("/reload")
        reloaded = _wait_state(
            home,
            session_id=session_id,
            predicate=lambda item: item.get("connection_id") and item.get("connection_id") != old_connection.get("connection_id"),
            timeout=30,
        )
        for label in ("launcher", "provider"):
            owned_processes.append(
                _process_record(
                    reloaded.get(f"{label}_pid"),
                    reloaded.get(f"{label}_process_start_time"),
                    label,
                )
            )
        observations["reload_rebind"] = reloaded.get("connection_id") != old_connection.get("connection_id")
        stale = _stale_frame(old_connection)
        observations["stale_frame"] = stale
        observations["stale_owner_refused"] = stale.get("accepted") is False and stale.get("error_code") in {
            "stale_channel",
            "stale_generation",
            "session_not_attached",
        }
        reload_snapshot = _runtime_snapshot(args.api_url, args.agents_token, session_id)
        reload_binding = _runtime_binding(
            reload_snapshot,
            session_id=session_id,
            provider_session_id=str(reloaded.get("provider_session_id") or ""),
        )
        observations["reload_runtime"] = {
            "binding": reload_binding,
            "served_controls": _served_controls(reload_snapshot),
        }
        observations["reload_rebind"] = (
            observations["reload_rebind"] is True
            and reload_binding["one_session"]
            and reload_binding["one_thread"]
            and reload_binding["provider"] == "pi"
            and _served_controls(reload_snapshot)
        )

        replacement_before = str(reloaded.get("provider_session_id") or "")
        launch.submit_line("/new")
        replaced = _wait_state(
            home,
            session_id=session_id,
            predicate=lambda item: item.get("provider_session_id") and item.get("provider_session_id") != replacement_before,
            timeout=30,
        )
        observations["session_replacement_rebind"] = (
            bool(replaced.get("provider_session_id")) and replaced.get("provider_session_id") != replacement_before
        )
        current_state = replaced
        session_file = Path(str(replaced.get("session_file") or session_file))
        for label in ("launcher", "provider"):
            owned_processes.append(
                _process_record(
                    replaced.get(f"{label}_pid"),
                    replaced.get(f"{label}_process_start_time"),
                    label,
                )
            )
        replacement_native = _native_snapshot(session_file)
        observations["replacement_binding"] = {
            "provider_session_id": replaced.get("provider_session_id"),
            "native_header_id": replacement_native[1].get("provider_session_id"),
            "native_file_present": session_file.is_file(),
        }
        replacement_marker = f"PI_HELM_REPLACED_{os.urandom(8).hex()}"
        replacement_offset = session_file.stat().st_size
        replacement_send = _run_engine(args.engine, "send", session_id, env, text=f"Reply with exactly {replacement_marker}.")
        replacement_native = _wait_native_marker(
            session_file,
            replacement_marker,
            minimum_source_offset=replacement_offset,
        )
        replacement_flush = shipper.flush("pi-helm-replacement")
        runtime_replacement = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(replaced.get("provider_session_id") or ""),
            replacement_marker,
        )
        observations["replacement_runtime"] = {
            "send": replacement_send,
            "native": replacement_native,
            "flush": replacement_flush,
            "binding": runtime_replacement["binding"],
        }
        observations["replacement_binding"].update(
            {
                "native_header_id": replacement_native["metadata"].get("provider_session_id"),
                "current_invocation_rows": len(replacement_native["invocation_rows"]),
            }
        )

        abort_started = _run_engine(
            args.engine,
            "send",
            session_id,
            env,
            text=f"Use the read tool repeatedly on {proof_file} before replying with PI_HELM_ABORT_{os.urandom(8).hex()}.",
        )
        try:
            abort_active_state = _wait_state(
                home,
                session_id=session_id,
                predicate=lambda item: item.get("phase") in {"running", "thinking"},
                timeout=10,
            )
        except RuntimeError:
            abort_active_state = {}
        abort = _run_engine(args.engine, "abort", session_id, env)
        try:
            aborted_state = _wait_state(
                home,
                session_id=session_id,
                predicate=lambda item: item.get("phase") not in {"running", "thinking"},
                timeout=20,
            )
        except RuntimeError:
            aborted_state = {}
        observations["abort_native"] = (
            abort_started["accepted"]
            and bool(abort_active_state)
            and abort["accepted"]
            and bool(aborted_state)
            and aborted_state.get("phase") not in {"running", "thinking"}
        )
        terminate = _run_engine(args.engine, "terminate", session_id, env)
        launch.close()
        observations["terminate_owned"] = terminate["accepted"] and not launch.alive()

        resume_marker = f"PI_HELM_RESUME_{os.urandom(8).hex()}"
        resume_offset = session_file.stat().st_size
        resume = ProviderPtySession.start(
            argv=[
                str(args.longhouse_cli),
                "pi",
                "--cwd",
                str(workspace),
                "--pi-bin",
                str(args.provider_bin),
                "--provider",
                "openrouter",
                "--model",
                args.model,
                "--resume-session",
                session_id,
                "--prompt",
                f"Use the read tool to read {proof_file}, then reply with exactly {resume_marker}.",
            ],
            cwd=workspace,
            env=env,
            terminal_path=root / "cold-resume-terminal.raw",
            thread_name="pi-helm-cold-resume-terminal-drain",
        )
        sessions.append(resume)
        resumed_state = _wait_state(home, session_id=session_id, predicate=lambda item: item.get("ready") is True, timeout=45)
        for label in ("launcher", "provider"):
            owned_processes.append(
                _process_record(
                    resumed_state.get(f"{label}_pid"),
                    resumed_state.get(f"{label}_process_start_time"),
                    label,
                )
            )
        resumed_file = Path(str(resumed_state.get("session_file") or session_file))
        resumed_native = _wait_native_marker(resumed_file, resume_marker, minimum_source_offset=resume_offset)
        resume_flush = shipper.flush("pi-helm-cold-resume")
        runtime_resume = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(resumed_state.get("provider_session_id") or ""),
            resume_marker,
        )
        observations["cold_resume_exact_file"] = (
            resumed_state.get("run_id") != old_connection.get("run_id")
            and resumed_state.get("provider_session_id") == current_state.get("provider_session_id")
            and resumed_file.resolve() == session_file.resolve()
            and resumed_native["metadata"].get("provider_session_id") == resumed_state.get("provider_session_id")
            and bool(resumed_native["invocation_rows"])
            and resumed_native["assistant_marker_rows"] > 0
            and resume_flush.get("status") == "pass"
            and runtime_resume["binding"]["one_session"]
            and runtime_resume["binding"]["one_thread"]
            and runtime_resume["binding"]["provider_session_bound"]
        )
        resume_terminate = _run_engine(args.engine, "terminate", session_id, env)
        resume.close()
        observations["resume_terminate"] = resume_terminate
        observations["cleanup"] = _cleanup_receipt(owned_processes)
        observations["terminate_owned"] = (
            terminate["accepted"]
            and not launch.alive()
            and not resume.alive()
            and observations["cleanup"]["provider_process_dead"] is True
            and observations["cleanup"]["process_group_dead"] is True
        )
        observations["status"] = "pass" if observations["terminate_owned"] else "fail"
    except Exception as exc:  # noqa: BLE001 - retain the real failure and cleanup evidence
        failure = exc
        observations["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for process in sessions:
            try:
                process.close()
            except Exception as exc:  # noqa: BLE001 - cleanup evidence records the failure below
                observations.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
        cleanup_deadline = time.monotonic() + 10
        cleanup = _cleanup_receipt(owned_processes)
        while cleanup["birth_identities_verified"] and cleanup["status"] != "pass" and time.monotonic() < cleanup_deadline:
            time.sleep(0.1)
            cleanup = _cleanup_receipt(owned_processes)
        if shipper is not None and owned_processes:
            try:
                # Let the real Machine Agent observe final owner exit before
                # stopping its last path to the Runtime Host.
                observations["cleanup_flush"] = shipper.flush("pi-helm-cleanup")
            except Exception as exc:
                observations.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
        if shipper is not None:
            try:
                observations["shipper_stop"] = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup failure must remain visible
                observations.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
        source_secrets = [value for name, value in env.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
        source_paths = {
            "terminal": root / "helm-terminal.raw",
            "resume_terminal": root / "cold-resume-terminal.raw",
        }
        native_root = Path(env["PI_CODING_AGENT_DIR"]) / "sessions"
        source_paths.update((f"native-{path.stem}", path) for path in native_root.rglob("*.jsonl"))
        for name, source in source_paths.items():
            if source.is_file():
                retained_sources.append(_retain_source(root, source, f"{name}.raw", source_secrets))
        observations["retained_source_artifacts"] = retained_sources
        observations["cleanup"] = _cleanup_receipt(owned_processes)
        observations["process_provider_dead"] = observations["cleanup"]["provider_process_dead"]
        observations["process_group_dead"] = observations["cleanup"]["process_group_dead"]
        observations["no_orphan_provider_processes"] = observations["cleanup"]["no_orphan_provider_processes"]
        _write_json(root / "cleanup-receipt.json", observations["cleanup"])
        if failure is not None:
            _write_json(
                root / "failure-envelope.json",
                {
                    "status": "fail",
                    "failure_code": "pi_helm_lifecycle_failed",
                    "error": observations.get("error"),
                    "cleanup": observations["cleanup"],
                    "retained_source_artifacts": retained_sources,
                },
            )
        try:
            shutil.rmtree(isolation)
        except OSError as exc:
            observations.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
    source_secrets = [value for name, value in env.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
    observations = _redact_value(observations, source_secrets)
    assertions = pi_helm_lifecycle_assertions(observations)
    result = {
        "schema_version": 1,
        "artifact_kind": "pi_helm_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "pi",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": "pass" if failure is None and all(assertions.values()) else "fail",
        "assertions": assertions,
        "observation": observations,
        "provider_binary": {"path": str(args.provider_bin), "sha256": sha256_file(args.provider_bin)},
        "artifact_manifest": artifact_manifest(root),
    }
    if failure is not None:
        result["failure_code"] = "pi_helm_lifecycle_failed"
        result["error"] = f"{type(failure).__name__}: {failure}"
    _write_json(root / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    required = (
        "evidence_root",
        "repo_root",
        "engine",
        "longhouse_cli",
        "provider_bin",
        "provider_version",
        "api_url",
        "agents_token",
    )
    missing = [name for name in required if not getattr(args, name)]
    if missing:
        print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{missing[0].replace('_', '-')}"}))
        return 2
    try:
        result = run_pi_helm_lifecycle(args)
    except Exception as exc:  # noqa: BLE001 - retain a typed producer failure
        result = {
            "schema_version": 1,
            "artifact_kind": "pi_helm_lifecycle_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "pi",
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "pi_helm_lifecycle_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        if args.evidence_root:
            args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
