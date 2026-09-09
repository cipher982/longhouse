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
import urllib.error
from pathlib import Path
from typing import Any
from urllib.request import Request
from urllib.request import urlopen

from zerg.qa.console_served_state_core import assistant_marker_events
from zerg.qa.console_served_state_core import event_text
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.pi_native import pi_native_shadow_taxonomy
from zerg.qa.pi_native import pi_transcript_rows
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "pi_helm_lifecycle"
_RUNTIME_HOST_USER_AGENT = "LonghouseProviderFactory/1.0"
_PROPAGATING_HTTP_STATUSES = frozenset({404, 429, 500, 502, 503, 504})
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
    producer_revision=2,
    scenario_id=SCENARIO_ID,
    scenario_revision=2,
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
    observation_scope="scenario",
)


class _RuntimeHostHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        self.last_runtime_observation: dict[str, Any] | None = None
        super().__init__(f"Runtime Host HTTP {status}: {detail}")


class _RuntimeConvergenceError(RuntimeError):
    def __init__(self, marker: str, cause: BaseException, last_runtime_observation: dict[str, Any] | None) -> None:
        self.marker = marker
        self.cause_type = type(cause).__name__
        self.http_status = getattr(cause, "status", None)
        self.detail = str(cause)
        self.last_runtime_observation = last_runtime_observation
        super().__init__(f"Runtime Host convergence for Pi marker {marker} failed: {cause}")


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
        headers={
            "X-Agents-Token": token,
            "Accept": "application/json",
            "User-Agent": _RUNTIME_HOST_USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
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


def _runtime_observation_summary(
    snapshot: dict[str, Any],
    *,
    session_id: str,
    provider_session_id: str,
    marker: str,
) -> dict[str, Any]:
    events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), dict) else {}
    events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
    matches = assistant_marker_events(events, marker, default_origin="durable")
    binding = _runtime_binding(snapshot, session_id=session_id, provider_session_id=provider_session_id)
    diagnostic = snapshot.get("diagnostic") if isinstance(snapshot.get("diagnostic"), dict) else {}
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    return {
        "session_id": session_id,
        "provider_session_id": provider_session_id,
        "marker": marker,
        "event_count": len(events),
        "assistant_marker_count": len(matches),
        "assistant_marker_occurrences": sum(event_text(event).count(marker) for event in matches),
        "binding": binding,
        "served_controls": _served_controls(snapshot),
        "served_path": diagnostic.get("served_path"),
        "shadow_mode": shadow.get("mode"),
    }


def _runtime_receipt(
    runtime: dict[str, Any],
    *,
    session_id: str,
    provider_session_id: str,
    marker: str,
) -> dict[str, Any]:
    snapshot = runtime.get("snapshot") if isinstance(runtime.get("snapshot"), dict) else {}
    return _runtime_observation_summary(
        snapshot,
        session_id=session_id,
        provider_session_id=provider_session_id,
        marker=marker,
    )


def _state_identity(state: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "session_id",
        "provider",
        "provider_session_id",
        "run_id",
        "connection_id",
        "lease_generation",
        "session_file",
        "session_dir",
        "status",
        "ready",
        "phase",
        "terminal_reason",
        "launcher_pid",
        "launcher_process_start_time",
        "provider_pid",
        "provider_process_start_time",
        "socket_path",
    )
    return {key: state[key] for key in keys if key in state}


def _native_file_identity(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError as exc:
        return {"path": str(path), "present": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "path": str(path.resolve()),
        "present": True,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "size_bytes": stat.st_size,
    }


def _native_receipt(
    native: dict[str, Any],
    *,
    session_id: str,
    session_file: Path,
    marker: str | None = None,
) -> dict[str, Any]:
    metadata = native.get("metadata") if isinstance(native.get("metadata"), dict) else {}
    taxonomy = native.get("taxonomy") if isinstance(native.get("taxonomy"), dict) else {}
    invocation_rows = native.get("invocation_rows") if isinstance(native.get("invocation_rows"), list) else []
    receipt = {
        "session_id": session_id,
        "provider": "pi",
        "provider_session_id": metadata.get("provider_session_id"),
        "native_header_provider_session_id": metadata.get("provider_session_id"),
        "native_file": _native_file_identity(session_file),
        "row_count": native.get("rows"),
        "invocation_row_count": len(invocation_rows),
        "assistant_marker_rows": native.get("assistant_marker_rows"),
        "taxonomy_source": taxonomy.get("source"),
    }
    if marker is not None:
        receipt["marker"] = marker
    return receipt


def _diagnostic_receipt(diagnostic: dict[str, Any]) -> dict[str, Any]:
    shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
    fact_sources = shadow.get("fact_sources") if isinstance(shadow.get("fact_sources"), dict) else {}
    control = fact_sources.get("control") if isinstance(fact_sources.get("control"), dict) else {}
    return {
        "served_path": diagnostic.get("served_path"),
        "shadow_mode": shadow.get("mode"),
        "control_subject_key": control.get("subject_key"),
    }


def _control_receipt(
    operation: str,
    state: dict[str, Any],
    *,
    command: object,
    accepted: bool,
    result: object,
    native: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "operation": operation,
        "session": _state_identity(state),
        "accepted": accepted,
        "command": command,
        "result": result,
    }
    if native is not None:
        receipt["native"] = native
    if runtime is not None:
        receipt["runtime"] = runtime
    return receipt


def _write_scenario_receipts(root: Path, args: argparse.Namespace, observations: dict[str, Any], secrets: list[str]) -> None:
    provider_binary = {
        "schema_version": 1,
        "artifact_kind": "pi_helm_provider_binary_receipt",
        "provider": "pi",
        "version": args.provider_version,
        "path": str(args.provider_bin),
        "sha256": observations.get("provider_binary_sha256"),
    }
    control_receipts = observations["control_receipts"]
    control_complete = all(receipt.get("accepted") is True for receipt in control_receipts.values())
    receipts: dict[str, dict[str, Any]] = {
        "provider-binary-receipt.json": provider_binary,
        "helm-registration-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_registration_receipt",
            **observations["helm_registration_receipt"],
        },
        "native-session-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_native_session_receipt",
            "status": "pass" if observations["native_session_receipt"].get("native_file", {}).get("present") else "fail",
            **observations["native_session_receipt"],
        },
        "native-taxonomy-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_native_taxonomy_receipt",
            **observations["native_taxonomy_receipt"],
        },
        "control-command-receipts.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_control_command_receipts",
            "status": "pass" if control_complete else "fail",
            "provider": "pi",
            "session_id": observations.get("session_id"),
            "commands": control_receipts,
        },
        "reload-rebind-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_reload_rebind_receipt",
            **observations["reload_rebind_receipt"],
        },
        "session-replacement-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_session_replacement_receipt",
            **observations["session_replacement_receipt"],
        },
        "cold-resume-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_cold_resume_receipt",
            **observations["cold_resume_receipt"],
        },
        "stale-owner-receipt.json": {
            "schema_version": 1,
            "artifact_kind": "pi_helm_stale_owner_receipt",
            **observations["stale_owner_receipt"],
        },
    }
    for name, receipt in receipts.items():
        _write_json(root / name, _redact_value(receipt, secrets))


def _runtime_failure_diagnostic(error: BaseException) -> dict[str, Any] | None:
    if not isinstance(error, (_RuntimeHostHTTPError, _RuntimeConvergenceError)):
        return None
    diagnostic: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error": str(error),
        "http_status": getattr(error, "status", getattr(error, "http_status", None)),
        "last_runtime_observation": getattr(error, "last_runtime_observation", None),
    }
    if isinstance(error, _RuntimeConvergenceError):
        diagnostic.update({"marker": error.marker, "cause_type": error.cause_type, "cause_detail": error.detail})
    return diagnostic


def _wait_runtime_control_identity(url: str, token: str, session_id: str, state: dict[str, Any]) -> dict[str, Any]:
    expected = f"connection:{state['connection_id']}:{state['lease_generation']}"

    def observe() -> dict[str, Any] | None:
        try:
            diagnostic = _runtime_diagnostic(url, token, session_id)
        except _RuntimeHostHTTPError as exc:
            if exc.status in _PROPAGATING_HTTP_STATUSES:
                return None
            raise
        shadow = diagnostic.get("shadow") or {}
        chosen = (shadow.get("fact_sources") or {}).get("control") or {}
        if chosen.get("subject_key") == expected and _served_controls({"diagnostic": diagnostic}):
            return diagnostic
        return None

    return _wait(observe, timeout=30, description=f"served Pi control identity {expected}")


def _wait_runtime_convergence(
    url: str,
    token: str,
    session_id: str,
    provider_session_id: str,
    marker: str,
    *,
    timeout: float = 90,
) -> dict[str, Any]:
    last_runtime_observation: dict[str, Any] | None = None

    def observe() -> dict[str, Any] | None:
        nonlocal last_runtime_observation
        try:
            snapshot = _runtime_snapshot(url, token, session_id)
        except _RuntimeHostHTTPError as exc:
            if exc.status in _PROPAGATING_HTTP_STATUSES:
                last_runtime_observation = {"http_status": exc.status, "error": str(exc)}
                return None
            exc.last_runtime_observation = last_runtime_observation
            raise
        events_payload = snapshot.get("events") if isinstance(snapshot.get("events"), dict) else {}
        events = events_payload.get("events") if isinstance(events_payload.get("events"), list) else []
        matches = assistant_marker_events(events, marker, default_origin="durable")
        binding = _runtime_binding(snapshot, session_id=session_id, provider_session_id=provider_session_id)
        last_runtime_observation = _runtime_observation_summary(
            snapshot,
            session_id=session_id,
            provider_session_id=provider_session_id,
            marker=marker,
        )
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

    try:
        return _wait(observe, timeout=timeout, description=f"Runtime Host convergence for Pi marker {marker}")
    except _RuntimeHostHTTPError:
        raise
    except Exception as exc:
        raise _RuntimeConvergenceError(marker, exc, last_runtime_observation) from exc


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
        return {"frame": frame, "accepted": False, "error_code": "socket_missing"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(5)
            channel.connect(socket_path)
            channel.sendall((json.dumps(frame) + "\n").encode())
            response = json.loads(channel.recv(8192).decode())
    except (OSError, json.JSONDecodeError) as exc:
        return {"frame": frame, "accepted": False, "error_code": type(exc).__name__, "error": str(exc)}
    error = response.get("error") if isinstance(response, dict) else {}
    return {
        "frame": frame,
        "accepted": response.get("ok") is True if isinstance(response, dict) else False,
        "error_code": error.get("code") if isinstance(error, dict) else None,
        "response": response,
    }


def _send_live(url: str, token: str, session_id: str, text: str, *, timeout: float = 30) -> dict[str, Any]:
    request = Request(
        f"{url.rstrip('/')}/api/agents/sessions/{session_id}/send-live",
        data=json.dumps({"message": text}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Agents-Token": token,
            "User-Agent": _RUNTIME_HOST_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
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
    if not isinstance(payload, dict) or payload.get("accepted") is not True:
        raise RuntimeError(f"Runtime Host did not accept Pi follow-up: {payload}")
    return payload


def _wait_native_abort(
    session_file: Path,
    *,
    minimum_source_offset: int,
    timeout: float = 30,
) -> dict[str, Any] | None:
    def observe() -> dict[str, Any] | None:
        if not session_file.is_file():
            return None
        rows, metadata, taxonomy = _native_snapshot(session_file)
        for row in rows:
            if int(row.get("source_offset") or 0) < minimum_source_offset:
                continue
            message = row.get("message") if isinstance(row.get("message"), dict) else {}
            stop_reason = str(message.get("stop_reason") or "").strip().lower()
            if row.get("type") == "assistant" and stop_reason in {"aborted", "cancelled", "canceled"}:
                return {"row": row, "metadata": metadata, "taxonomy": taxonomy}
        return None

    return _wait(observe, timeout=timeout, description="native Pi aborted message")


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


def _recorded_owner_status(record: dict[str, Any]) -> str:
    pid = record.get("pid")
    expected_birth = record.get("expected_birth")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not expected_birth:
        return "invalid"
    if not record["alive"]:
        return "dead"
    if record.get("birth") == str(expected_birth):
        return "alive"
    if record.get("birth"):
        return "reused"
    return "unknown"


def _wait_recorded_execution_owners_dead(
    home: Path,
    session_id: str,
    expected_state: dict[str, Any],
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    expected_identities = {
        label: {
            "pid": expected_state.get(f"{label}_pid"),
            "birth": expected_state.get(f"{label}_process_start_time"),
        }
        for label in ("launcher", "provider")
    }
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = None
        for path in _state_paths(home):
            try:
                candidate = _read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if candidate.get("session_id") == session_id:
                state = candidate
                break
        owner_records = [_process_record(identity["pid"], identity["birth"], label) for label, identity in expected_identities.items()]
        owner_status = {record["label"]: _recorded_owner_status(record) for record in owner_records}
        state_identity_matches = bool(state) and all(
            state.get(f"{label}_pid") == identity["pid"] and state.get(f"{label}_process_start_time") == identity["birth"]
            for label, identity in expected_identities.items()
        )
        last = {
            "state_status": state.get("status") if state else None,
            "terminal_reason": state.get("terminal_reason") if state else None,
            "state_identity_matches": state_identity_matches,
            "owner_status": owner_status,
            "owners": owner_records,
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
    raise RuntimeError(f"timed out waiting for terminated Pi Helm execution owners: {last}")


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
        return {key: _redact_value(item, secrets) for key, item in value.items() if key not in {"channel_token", "auth_token"}}
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
            "LONGHOUSE_PI_BIN": str(args.provider_bin),
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
        launch_command = [
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
        ]
        launch = ProviderPtySession.start(
            argv=launch_command,
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
        observations["initial_owner_state"] = _state_identity(current_state)
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
        observations["native_session_receipt"] = _native_receipt(
            native,
            session_id=session_id,
            session_file=session_file,
        )
        observations["native_taxonomy_receipt"] = {
            "session_id": session_id,
            "provider": "pi",
            "provider_session_id": native["metadata"].get("provider_session_id"),
            "native_file": _native_file_identity(session_file),
            "taxonomy": native["taxonomy"],
            "status": "pass" if observations["native_tool_taxonomy"] else "fail",
        }
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
            "receipt": _runtime_receipt(
                runtime_launch,
                session_id=session_id,
                provider_session_id=str(current_state.get("provider_session_id") or ""),
                marker=marker,
            ),
        }
        local_health = subprocess.run(
            [str(args.longhouse_cli), "local-health", "--json", "--state-root", env["LONGHOUSE_HOME"]],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        (root / "local-health.json").write_text(local_health.stdout, encoding="utf-8")
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
        observations["helm_registration_receipt"] = {
            "status": "pass" if observations["launch_registration"] else "fail",
            "provider": "pi",
            "command": launch_command,
            "accepted": current_state.get("ready") is True,
            "session": _state_identity(current_state),
            "owner_state": _state_identity(current_state),
            "machine_registration": observations["machine_registration"],
            "runtime": observations["runtime_launch"]["receipt"],
        }
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
        observations["runtime_send"] = _runtime_receipt(
            runtime_send,
            session_id=session_id,
            provider_session_id=str(current_state.get("provider_session_id") or ""),
            marker=send_marker,
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
        follow = _send_live(
            str(args.api_url),
            str(args.agents_token),
            session_id,
            f"After this turn, reply with {follow_marker}.",
        )
        follow_native = _wait_native_marker(session_file, follow_marker, minimum_source_offset=follow_offset)
        steer_runtime = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(current_state.get("provider_session_id") or ""),
            steer_marker,
        )
        follow_runtime = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(current_state.get("provider_session_id") or ""),
            follow_marker,
        )
        observations["runtime_steer"] = _runtime_receipt(
            steer_runtime,
            session_id=session_id,
            provider_session_id=str(current_state.get("provider_session_id") or ""),
            marker=steer_marker,
        )
        observations["runtime_follow_up"] = _runtime_receipt(
            follow_runtime,
            session_id=session_id,
            provider_session_id=str(current_state.get("provider_session_id") or ""),
            marker=follow_marker,
        )
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
        observations["control_receipts"] = {
            "send": _control_receipt(
                "send",
                current_state,
                command=send["command"],
                accepted=send["accepted"],
                result={key: send[key] for key in ("returncode", "payload", "stdout", "stderr")},
                native=_native_receipt(send_native, session_id=session_id, session_file=session_file, marker=send_marker),
                runtime=observations["runtime_send"],
            ),
            "active": _control_receipt(
                "active",
                current_state,
                command=active["command"],
                accepted=active["accepted"],
                result={key: active[key] for key in ("returncode", "payload", "stdout", "stderr")},
                runtime={"state": _state_identity(active_state), "observed": bool(active_state)},
            ),
            "steer": _control_receipt(
                "steer",
                current_state,
                command=steer["command"],
                accepted=steer["accepted"],
                result={key: steer[key] for key in ("returncode", "payload", "stdout", "stderr")},
                native=_native_receipt(steer_native, session_id=session_id, session_file=session_file, marker=steer_marker),
                runtime=observations["runtime_steer"],
            ),
            "follow_up": _control_receipt(
                "follow_up",
                current_state,
                command={
                    "method": "POST",
                    "path": f"/api/agents/sessions/{session_id}/send-live",
                    "text": f"After this turn, reply with {follow_marker}.",
                },
                accepted=follow.get("accepted") is True,
                result=follow,
                native=_native_receipt(follow_native, session_id=session_id, session_file=session_file, marker=follow_marker),
                runtime=observations["runtime_follow_up"],
            ),
        }

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
        observations["reloaded_owner_state"] = _state_identity(reloaded)
        observations["reload_rebind"] = reloaded.get("connection_id") != old_connection.get("connection_id")
        stale_native_offset = session_file.stat().st_size
        stale = _stale_frame(old_connection)
        stale_native_after_offset = session_file.stat().st_size
        observations["stale_native_dispatch"] = stale_native_after_offset != stale_native_offset
        observations["stale_frame"] = stale
        observations["stale_owner_refused"] = (
            stale.get("accepted") is False
            and stale.get("error_code")
            in {
                "stale_channel",
                "stale_generation",
                "session_not_attached",
            }
            and observations["stale_native_dispatch"] is False
        )
        observations["stale_owner_receipt"] = {
            "status": "pass" if observations["stale_owner_refused"] else "fail",
            "provider": "pi",
            "session_id": session_id,
            "old_owner": _state_identity(old_connection),
            "current_owner": _state_identity(reloaded),
            "attempted_stale_frame": stale.get("frame"),
            "rejection": {
                "accepted": stale.get("accepted") is True,
                "error_code": stale.get("error_code"),
                "response": stale.get("response"),
                "error": stale.get("error"),
            },
            "native_dispatch": {
                "observed": observations["stale_native_dispatch"],
                "source_offset_before": stale_native_offset,
                "source_offset_after": stale_native_after_offset,
            },
        }
        reload_control_identity = _wait_runtime_control_identity(args.api_url, args.agents_token, session_id, reloaded)
        reload_marker = f"PI_HELM_RELOADED_{os.urandom(8).hex()}"
        reload_offset = session_file.stat().st_size
        reload_send = _send_live(args.api_url, args.agents_token, session_id, f"Reply with exactly {reload_marker}.")
        reload_native = _wait_native_marker(session_file, reload_marker, minimum_source_offset=reload_offset)
        reload_flush = shipper.flush("pi-helm-reload")
        reload_snapshot = _wait_runtime_convergence(
            args.api_url,
            args.agents_token,
            session_id,
            str(reloaded.get("provider_session_id") or ""),
            reload_marker,
        )["snapshot"]
        reload_binding = _runtime_binding(
            reload_snapshot,
            session_id=session_id,
            provider_session_id=str(reloaded.get("provider_session_id") or ""),
        )
        observations["reload_runtime"] = {
            "send": reload_send,
            "native": reload_native,
            "flush": reload_flush,
            "binding": reload_binding,
            "served_controls": _served_controls(reload_snapshot),
            "control_identity": _diagnostic_receipt(reload_control_identity),
            "receipt": _runtime_receipt(
                {
                    "snapshot": reload_snapshot,
                },
                session_id=session_id,
                provider_session_id=str(reloaded.get("provider_session_id") or ""),
                marker=reload_marker,
            ),
        }
        observations["reload_rebind"] = (
            observations["reload_rebind"] is True
            and reload_send["accepted"]
            and reload_native["assistant_marker_rows"] == 1
            and reload_binding["one_session"]
            and reload_binding["one_thread"]
            and reload_binding["provider"] == "pi"
            and _served_controls(reload_snapshot)
        )
        observations["reload_rebind_receipt"] = {
            "status": "pass" if observations["reload_rebind"] else "fail",
            "provider": "pi",
            "command": {"surface": "terminal", "text": "/reload", "submitted": True},
            "session_id": session_id,
            "before": _state_identity(old_connection),
            "after": _state_identity(reloaded),
            "accepted": reload_send.get("accepted") is True,
            "native": _native_receipt(
                reload_native,
                session_id=session_id,
                session_file=session_file,
                marker=reload_marker,
            ),
            "runtime": observations["reload_runtime"],
        }

        replacement_before = str(reloaded.get("provider_session_id") or "")
        launch.submit_line("/new")
        replaced = _wait_state(
            home,
            session_id=session_id,
            predicate=lambda item: item.get("session_file") and item.get("provider_session_id") != replacement_before,
            timeout=30,
        )
        observations["session_replacement_rebind"] = (
            bool(replaced.get("provider_session_id")) and replaced.get("provider_session_id") != replacement_before
        )
        current_state = replaced
        session_file = Path(str(replaced["session_file"]))
        for label in ("launcher", "provider"):
            owned_processes.append(
                _process_record(
                    replaced.get(f"{label}_pid"),
                    replaced.get(f"{label}_process_start_time"),
                    label,
                )
            )
        observations["replacement_owner_state"] = _state_identity(replaced)
        replacement_marker = f"PI_HELM_REPLACED_{os.urandom(8).hex()}"
        # Pi reserves the path at /new and materializes it on the first turn.
        replacement_offset = session_file.stat().st_size if session_file.is_file() else 0
        replacement_control_identity = _wait_runtime_control_identity(args.api_url, args.agents_token, session_id, replaced)
        replacement_send = _send_live(args.api_url, args.agents_token, session_id, f"Reply with exactly {replacement_marker}.")
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
            "control_identity": _diagnostic_receipt(replacement_control_identity),
            "receipt": _runtime_receipt(
                runtime_replacement,
                session_id=session_id,
                provider_session_id=str(replaced.get("provider_session_id") or ""),
                marker=replacement_marker,
            ),
        }
        observations["replacement_binding"] = {
            "provider_session_id": replaced.get("provider_session_id"),
            "native_header_id": replacement_native["metadata"].get("provider_session_id"),
            "native_file_present": session_file.is_file(),
            "current_invocation_rows": len(replacement_native["invocation_rows"]),
        }
        observations["session_replacement_receipt"] = {
            "status": "pass" if observations["session_replacement_rebind"] else "fail",
            "provider": "pi",
            "command": {"surface": "terminal", "text": "/new", "submitted": True},
            "session_id": session_id,
            "before": _state_identity(reloaded),
            "after": _state_identity(replaced),
            "accepted": replacement_send.get("accepted") is True,
            "native": _native_receipt(
                replacement_native,
                session_id=session_id,
                session_file=session_file,
                marker=replacement_marker,
            ),
            "runtime": observations["replacement_runtime"],
            "binding": observations["replacement_binding"],
        }

        terminated_native_file = _native_file_identity(session_file)
        terminated_native_header_id = replacement_native["metadata"].get("provider_session_id")
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
        abort_offset = session_file.stat().st_size
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
        try:
            abort_native = _wait_native_abort(session_file, minimum_source_offset=abort_offset)
        except RuntimeError as exc:
            abort_native = None
            observations["abort_native_wait_error"] = f"{type(exc).__name__}: {exc}"
        observations["abort_native_evidence"] = abort_native
        observations["abort_native"] = (
            abort_started["accepted"]
            and bool(abort_active_state)
            and abort["accepted"]
            and bool(aborted_state)
            and aborted_state.get("phase") not in {"running", "thinking"}
            and abort_native is not None
        )
        observations["control_receipts"]["abort"] = _control_receipt(
            "abort",
            current_state,
            command=abort["command"],
            accepted=abort["accepted"],
            result={key: abort[key] for key in ("returncode", "payload", "stdout", "stderr")},
            native=(
                {
                    "session_id": session_id,
                    "provider": "pi",
                    "provider_session_id": abort_native["metadata"].get("provider_session_id"),
                    "native_file": _native_file_identity(session_file),
                    "stop_reason": (abort_native.get("row") or {}).get("message", {}).get("stop_reason")
                    if isinstance((abort_native.get("row") or {}).get("message"), dict)
                    else None,
                    "observed": abort_native is not None,
                }
                if abort_native is not None
                else {"session_id": session_id, "observed": False}
            ),
            runtime={"started_state": _state_identity(abort_active_state), "settled_state": _state_identity(aborted_state)},
        )
        terminate_state = dict(current_state)
        terminate = _run_engine(args.engine, "terminate", session_id, env)
        observations["terminate_owned"] = False
        try:
            termination_wait = _wait_recorded_execution_owners_dead(
                home,
                session_id,
                terminate_state,
            )
            observations["terminate_owner_wait"] = termination_wait
        finally:
            # The facade is only a terminal wrapper. The recorded launcher and
            # provider owners must be dead before this close can precede resume.
            launch.close()
        observations["terminate_owned"] = terminate["accepted"] and not launch.alive()
        if not observations["terminate_owned"]:
            raise RuntimeError("Pi Helm terminate was not proven after the recorded execution owners exited")
        observations["control_receipts"]["terminate"] = _control_receipt(
            "terminate",
            terminate_state,
            command=terminate["command"],
            accepted=terminate["accepted"],
            result={key: terminate[key] for key in ("returncode", "payload", "stdout", "stderr")},
            runtime={"owner_cleanup": observations["terminate_owner_wait"]},
        )

        resume_marker = f"PI_HELM_RESUME_{os.urandom(8).hex()}"
        resume_offset = session_file.stat().st_size
        terminated_run_id = terminate_state.get("run_id")
        resumed_provider_session_id = terminate_state.get("provider_session_id")
        resumed_file = session_file.resolve()

        def is_fresh_resume_state(item: dict[str, Any]) -> bool:
            state_file = item.get("session_file")
            try:
                same_file = bool(state_file) and Path(str(state_file)).resolve() == resumed_file
            except OSError:
                same_file = False
            return (
                item.get("ready") is True
                and item.get("status") in {"ready", "running"}
                and item.get("run_id")
                and item.get("run_id") != terminated_run_id
                and item.get("provider_session_id") == resumed_provider_session_id
                and same_file
            )

        resume_command = [
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
        ]
        resume = ProviderPtySession.start(
            argv=resume_command,
            cwd=workspace,
            env=env,
            terminal_path=root / "cold-resume-terminal.raw",
            thread_name="pi-helm-cold-resume-terminal-drain",
        )
        sessions.append(resume)
        resumed_state = _wait_state(
            home,
            session_id=session_id,
            predicate=is_fresh_resume_state,
            timeout=45,
        )
        for label in ("launcher", "provider"):
            owned_processes.append(
                _process_record(
                    resumed_state.get(f"{label}_pid"),
                    resumed_state.get(f"{label}_process_start_time"),
                    label,
                )
            )
        observations["resumed_owner_state"] = _state_identity(resumed_state)
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
        observations["runtime_resume"] = _runtime_receipt(
            runtime_resume,
            session_id=session_id,
            provider_session_id=str(resumed_state.get("provider_session_id") or ""),
            marker=resume_marker,
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
        try:
            if not resume_terminate["accepted"]:
                raise RuntimeError("Pi cold-resume termination was not accepted")
            observations["resume_terminate_owner_wait"] = _wait_recorded_execution_owners_dead(
                home,
                session_id,
                resumed_state,
            )
        finally:
            resume.close()
        observations["resume_terminate"] = resume_terminate
        observations["cold_resume_receipt"] = {
            "status": "pass" if observations["cold_resume_exact_file"] else "fail",
            "provider": "pi",
            "command": resume_command,
            "accepted": resumed_state.get("ready") is True,
            "session_id": session_id,
            "initial_owner_state": _state_identity(terminate_state),
            "resumed_owner_state": _state_identity(resumed_state),
            "old_owner_cleanup": observations["terminate_owner_wait"],
            "resumed_owner_cleanup": observations["resume_terminate_owner_wait"],
            "native_file_identity_before_resume": terminated_native_file,
            "native_file_identity_after_resume": _native_file_identity(resumed_file),
            "native_header_provider_session_id_before_resume": terminated_native_header_id,
            "native_header_provider_session_id_after_resume": resumed_native["metadata"].get("provider_session_id"),
            "same_native_file": resumed_file.resolve() == session_file.resolve(),
            "same_native_header": resumed_native["metadata"].get("provider_session_id") == terminated_native_header_id,
            "post_resume_activity": {
                "marker": resume_marker,
                "native": _native_receipt(
                    resumed_native,
                    session_id=session_id,
                    session_file=resumed_file,
                    marker=resume_marker,
                ),
                "runtime": observations["runtime_resume"],
                "flush": resume_flush,
                "resume_offset": resume_offset,
            },
        }
        observations["cleanup"] = _cleanup_receipt(owned_processes)
        observations["terminate_owned"] = (
            terminate["accepted"]
            and resume_terminate["accepted"]
            and observations["resume_terminate_owner_wait"]["status"] == "pass"
            and not launch.alive()
            and not resume.alive()
            and observations["cleanup"]["provider_process_dead"] is True
            and observations["cleanup"]["process_group_dead"] is True
        )
        observations["status"] = "pass" if observations["terminate_owned"] else "fail"
        source_secrets = [value for name, value in env.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
        _write_scenario_receipts(root, args, observations, source_secrets)
    except Exception as exc:  # noqa: BLE001 - retain the real failure and cleanup evidence
        failure = exc
        observations["error"] = f"{type(exc).__name__}: {exc}"
        runtime_failure = _runtime_failure_diagnostic(exc)
        if runtime_failure is not None:
            observations["runtime_failure"] = runtime_failure
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
                _redact_value(
                    {
                        "status": "fail",
                        "failure_code": "pi_helm_lifecycle_failed",
                        "error": observations.get("error"),
                        "runtime_failure": observations.get("runtime_failure"),
                        "cleanup": observations["cleanup"],
                        "retained_source_artifacts": retained_sources,
                    },
                    source_secrets,
                ),
            )
        try:
            shutil.rmtree(isolation)
        except OSError as exc:
            observations.setdefault("cleanup_errors", []).append(f"{type(exc).__name__}: {exc}")
    source_secrets = [value for name, value in env.items() if value and (name.endswith("_KEY") or name.endswith("_TOKEN"))]
    observations = _redact_value(observations, source_secrets)
    assertions = pi_helm_lifecycle_assertions(observations)
    provider_binary = {
        "provider": "pi",
        "version": args.provider_version,
        "path": str(args.provider_bin),
        "sha256": observations.get("provider_binary_sha256"),
    }
    result = {
        "schema_version": 1,
        "artifact_kind": "pi_helm_lifecycle_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "pi",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "observation_scope": "scenario",
        "generated_at": now(),
        "status": "pass" if failure is None and all(assertions.values()) else "fail",
        "assertions": assertions,
        "observation": observations,
        "provider_binary": provider_binary,
        "artifact_manifest": artifact_manifest(root),
    }
    if failure is not None:
        result["failure_code"] = "pi_helm_lifecycle_failed"
        result["error"] = f"{type(failure).__name__}: {failure}"
        result["diagnostic_observation"] = result.pop("observation")
        result["diagnostic_assertions"] = result.pop("assertions")
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
            "observation_scope": "scenario",
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
