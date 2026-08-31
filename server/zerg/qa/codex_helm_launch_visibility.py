#!/usr/bin/env python3
"""Live producer for Codex Helm launch provenance and factory isolation.

This producer deliberately drives the installed ``longhouse codex`` facade in
an owned PTY.  Direct bridge startup or a synthetic registration would miss
the product boundary that regressed: the facade is responsible for attaching
typed launch provenance to both fresh and resumed interactive sessions.
"""

from __future__ import annotations

import argparse
import copy
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa import codex_provider_release_canary as bridge_canary
from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import sha256_file
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.provider_launch_oracles import ASSERTION_ID
from zerg.qa.provider_launch_oracles import SCENARIO_ID
from zerg.qa.provider_launch_oracles import helm_launch_assertions
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import PROVIDER_RELEASE_SUBJECT
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.qa.runtime_host_canary_isolation import hide_and_verify_canary_isolation
from zerg.qa.runtime_host_canary_isolation import runtime_host_request
from zerg.services.internal_sessions import PROVIDER_FACTORY_MACHINE_ID

_EXECUTION_VARIANT = execution_variant_key(
    provider="codex",
    assertion_id=ASSERTION_ID,
    scenario_id=SCENARIO_ID,
    variant=None,
)

REGISTRATION = ProducerRegistration(
    producer_id="codex.helm_launch_visibility.v1",
    producer_revision=9,
    scenario_id=SCENARIO_ID,
    scenario_revision=6,
    assertion_cells=((ASSERTION_ID, None),),
    providers=("codex",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "fresh_interactive_facade_registration",
        "resumed_interactive_facade_registration",
        "canonical_control_head",
        "open_working_set_with_factory_isolation",
        "automation_hidden",
        "provenance_free_rejected",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "launch_registration_receipts",
        "canonical_visibility_receipts",
        "canary_isolation_receipt",
    ),
    required_cleanup=("runtime_host_canary_isolated", "owned_processes_dead"),
    implementation="server/zerg/qa/codex_helm_launch_visibility.py",
    oracle_source="server/zerg/qa/provider_launch_oracles.py",
    oracle_entrypoint="helm_launch_assertions",
    executable_module="zerg.qa.codex_helm_launch_visibility",
    provider_artifact_required=True,
    subject_kind=PROVIDER_RELEASE_SUBJECT,
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _cleanup_evidence(cleanups: list[dict[str, Any]]) -> tuple[str, dict[str, bool]]:
    status = "pass" if all(item.get("status") == "pass" for item in cleanups) else "fail"
    owned_processes_dead = all(isinstance(item.get("axes"), dict) and item["axes"].get("owned_processes_dead") is True for item in cleanups)
    return status, {
        "runtime_host_canary_isolated": status == "pass",
        "owned_processes_dead": owned_processes_dead,
    }


def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class RuntimeHostRecordingProxy:
    """Transparent loopback proxy retaining only safe registration fields."""

    def __init__(self, target: str) -> None:
        self.target = target.rstrip("/")
        self._condition = threading.Condition()
        self._registrations: list[dict[str, Any]] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                self._forward()

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                self._forward()

            def do_PATCH(self) -> None:  # noqa: N802 - stdlib callback name
                self._forward()

            def do_PUT(self) -> None:  # noqa: N802 - stdlib callback name
                self._forward()

            def do_DELETE(self) -> None:  # noqa: N802 - stdlib callback name
                self._forward()

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _forward(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "connection", "content-length", "accept-encoding"}
                }
                request = urllib.request.Request(
                    f"{owner.target}{self.path}",
                    data=body if self.command not in {"GET", "HEAD"} else None,
                    headers=headers,
                    method=self.command,
                )
                try:
                    response = urllib.request.urlopen(request, timeout=30)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    response_body = response.read()
                    status = int(response.status)
                    response_headers = dict(response.headers.items())
                if self.command == "POST" and self.path.split("?", 1)[0].endswith("/api/sessions/managed-local/this-device"):
                    # The upstream registration is authoritative even if the
                    # TUI client disconnects before the proxy relays it.
                    owner._capture_registration(body, response_body, status)
                self.send_response(status)
                for key, value in response_headers.items():
                    if key.lower() not in {"connection", "transfer-encoding", "content-length", "content-encoding"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                try:
                    self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="runtime-host-recording-proxy")

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _capture_registration(self, request_body: bytes, response_body: bytes, status: int) -> None:
        try:
            request_payload = json.loads(request_body)
        except (TypeError, json.JSONDecodeError):
            request_payload = {}
        try:
            response_payload = json.loads(response_body)
        except (TypeError, json.JSONDecodeError):
            response_payload = {}
        if not isinstance(request_payload, dict):
            request_payload = {}
        if not isinstance(response_payload, dict):
            response_payload = {}
        # Registration responses mint live coordination credentials. Retain
        # identity only; the proxy forwards the full response in memory but
        # never writes an authority token into evidence.
        safe_response = {
            key: response_payload.get(key)
            for key in ("session_id", "run_id", "thread_id", "created")
            if response_payload.get(key) is not None
        }
        record = {
            "received_at": _now(),
            "received_monotonic": time.monotonic(),
            "http_status": status,
            "request": request_payload,
            "response": safe_response,
        }
        with self._condition:
            self._registrations.append(record)
            self._condition.notify_all()

    def wait_registration(
        self,
        *,
        after: int,
        timeout: float,
        process: subprocess.Popen[bytes] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self._registrations) <= after:
                if process is not None and process.poll() is not None:
                    raise RuntimeError(f"managed launch wrapper exited before registration (exit={process.returncode})")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("timed out waiting for managed launch registration")
                self._condition.wait(timeout=min(remaining, 0.1 if process is not None else remaining))
            return copy.deepcopy(self._registrations[after])


def _runtime_request(args: argparse.Namespace, path: str, method: str, body: dict[str, Any] | None) -> dict[str, Any]:
    return runtime_host_request(args.api_url, args.agents_token, path, method, body)


def _set_facade_machine_label(isolation_root: Path, *, label: str, api_url: str) -> None:
    """Keep factory routing identity while avoiding its test-label classifier.

    The already-running Machine Agent remains authenticated as the factory
    device.  The facade's ``machine_name`` is display/provenance input, and a
    neutral label is required here because the legacy
    ``provider-factory-resume`` label intentionally forces every launch into
    the hidden test origin before typed human provenance can be evaluated.
    """

    write_json(
        isolation_root / "longhouse" / "machine" / "state.json",
        {"runtime_url": api_url, "machine_name": label},
    )


def _session_visible(args: argparse.Namespace, *, session_id: str, project: str, device_id: str) -> bool:
    query = urllib.parse.urlencode(
        {
            "project": project,
            "provider": "codex",
            "device_id": device_id,
            "hide_autonomous": "true",
            "limit": 100,
        }
    )
    payload = _runtime_request(args, f"sessions?{query}", "GET", None)
    return any(isinstance(row, dict) and str(row.get("id") or "") == session_id for row in payload.get("sessions") or [])


def _wait_canonical_launch(
    args: argparse.Namespace,
    *,
    registration: dict[str, Any],
    project: str,
    device_id: str,
    expected_actor: str,
    expected_surface: str,
    expect_visible: bool,
    expect_open: bool,
    launched_at: float,
) -> dict[str, Any]:
    request_payload = registration["request"]
    response_payload = registration["response"]
    session_id = str(response_payload.get("session_id") or request_payload.get("session_id") or "")
    run_id = str(response_payload.get("run_id") or "")
    if not session_id or not run_id:
        raise RuntimeError("managed launch registration did not return session and run identities")
    deadline = time.monotonic() + args.wait_ready_secs
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            diagnostic = _runtime_request(args, f"sessions/{session_id}/state-diagnostics", "GET", None)
            visible = _session_visible(args, session_id=session_id, project=project, device_id=device_id)
        except RuntimeError:
            time.sleep(0.25)
            continue
        shadow = diagnostic.get("shadow") if isinstance(diagnostic.get("shadow"), dict) else {}
        explain = diagnostic.get("explain") if isinstance(diagnostic.get("explain"), dict) else {}
        sources = explain.get("fact_sources") if isinstance(explain.get("fact_sources"), dict) else {}
        last = {
            "session_id": session_id,
            "mode": shadow.get("mode"),
            "working_set": explain.get("working_set"),
            "launch_actor": explain.get("launch_actor"),
            "launch_surface": explain.get("launch_surface"),
            "origin_kind": explain.get("origin_kind"),
            "control_head_current": bool(shadow.get("control")) and isinstance(sources.get("control"), dict),
            "control_run_id": shadow.get("control_run_id"),
            "default_timeline_visible": visible,
            "observed_within_seconds": round(time.monotonic() - launched_at, 3),
            "catalog_commit_seq": diagnostic.get("catalog_commit_seq"),
            "control_source": sources.get("control"),
        }
        ready = (
            last["mode"] == "helm"
            and last["launch_actor"] == expected_actor
            and last["launch_surface"] == expected_surface
            and last["control_head_current"] is True
            and last["control_run_id"] == run_id
            and visible is expect_visible
            and (not expect_open or last["working_set"] == "open")
        )
        if ready:
            return last
        time.sleep(0.25)
    raise RuntimeError(f"Codex launch did not reach canonical visibility: {json.dumps(last, sort_keys=True, default=str)}")


def _safe_registration(record: dict[str, Any]) -> dict[str, Any]:
    payload = dict(record["request"])
    payload.update(
        {
            "session_id": record["response"].get("session_id") or payload.get("session_id"),
            "run_id": record["response"].get("run_id"),
            "http_status": record.get("http_status"),
            "received_at": record.get("received_at"),
        }
    )
    return payload


def _launch_command(
    args: argparse.Namespace,
    *,
    proxy_url: str,
    workspace: Path,
    project: str,
    resume_session: str | None = None,
) -> list[str]:
    command = [
        str(args.longhouse_cli),
        "codex",
        "--cwd",
        str(workspace),
        "--project",
        project,
        "--url",
        proxy_url,
        "--token",
        args.agents_token,
        "--codex-bin",
        str(args.codex_bin),
    ]
    if args.model:
        command.extend(("--model", args.model))
    if resume_session:
        command.extend(("--resume-session", resume_session))
    return command


def _start_tui(
    args: argparse.Namespace,
    *,
    proxy_url: str,
    workspace: Path,
    project: str,
    environment: dict[str, str],
    terminal_path: Path,
    resume_session: str | None = None,
) -> ProviderPtySession:
    return ProviderPtySession.start(
        argv=_launch_command(
            args,
            proxy_url=proxy_url,
            workspace=workspace,
            project=project,
            resume_session=resume_session,
        ),
        cwd=workspace,
        env=environment,
        terminal_path=terminal_path,
        thread_name="codex-helm-launch-terminal-drain",
    )


def _stop_launch(
    args: argparse.Namespace,
    *,
    tui: ProviderPtySession,
    session_id: str,
    isolation_root: Path,
) -> dict[str, Any]:
    bridge = bridge_canary._stop_bridge(args, session_id, isolation_root)
    tui.close()
    return {
        "wrapper_pid": tui.process.pid,
        "wrapper_exit_code": tui.process.returncode,
        "wrapper_dead": _pid_dead(tui.process.pid),
        "bridge": bridge,
    }


def _seed_codex_rollout(
    tui: ProviderPtySession,
    *,
    codex_home: Path,
    marker: str,
    timeout: float,
) -> dict[str, Any]:
    """Create the provider history that a cold Resume is required to retain."""

    tui.submit_line(f"Reply with exactly {marker}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not tui.alive():
            raise RuntimeError(f"Codex TUI exited before seeding its rollout ({tui.process.returncode})")
        for path in codex_home.glob("sessions/**/*.jsonl"):
            if bridge_canary._assistant_transcript_contains(path, marker):
                return {
                    "status": "pass",
                    "rollout_path": str(path),
                    "rollout_size": path.stat().st_size,
                    "assistant_marker_observed": True,
                }
        time.sleep(0.25)
    raise RuntimeError("Codex TUI did not persist the seed turn before cold Resume")


def _human_launch_sequence(
    args: argparse.Namespace,
    *,
    root: Path,
    proxy: RuntimeHostRecordingProxy,
    registration_offset: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], int]:
    isolation_root = Path(tempfile.mkdtemp(prefix="lch-", dir="/tmp"))
    workspace = isolation_root / "workspace"
    workspace.mkdir(mode=0o700)
    project = f"provider-factory-codex-launch-{uuid.uuid4().hex}"
    environment = bridge_canary._provider_runtime_environment(os.environ, isolation_root)
    for key in ("LONGHOUSE_ORIGIN_KIND", "LONGHOUSE_LAUNCH_ACTOR", "LONGHOUSE_LAUNCH_SURFACE"):
        environment.pop(key, None)
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    login_receipt = login_with_api_key(
        args.codex_bin,
        api_key=os.environ["CODEX_API_KEY"],
        environment=environment,
        cwd=workspace,
    )
    write_json(root / "human-auth-receipt.json", login_receipt)
    shipper = _start_transcript_shipper(
        "codex",
        args,
        home=Path(environment["HOME"]),
        environment=environment,
        evidence_root=root / "human-shipper",
        longhouse_home=isolation_root / "longhouse",
    )
    canonical_device_id = str(shipper.receipt["machine_name"])
    facade_device_id = f"codex-launch-proof-{uuid.uuid4().hex}"
    _set_facade_machine_label(
        isolation_root,
        label=facade_device_id,
        api_url=args.api_url,
    )
    pids = [shipper.process.pid]
    launched_tuis: list[ProviderPtySession] = []
    created_session_ids: list[str] = []
    stop_receipts: list[dict[str, Any]] = []
    try:
        launched_at = time.monotonic()
        fresh_tui = _start_tui(
            args,
            proxy_url=proxy.url,
            workspace=workspace,
            project=project,
            environment=environment,
            terminal_path=root / "fresh-terminal.log",
        )
        launched_tuis.append(fresh_tui)
        pids.append(fresh_tui.process.pid)
        fresh_record = proxy.wait_registration(
            after=registration_offset,
            timeout=args.wait_ready_secs,
            process=fresh_tui.process,
        )
        fresh_registration = _safe_registration(fresh_record)
        # Record the session before asserting anything about it. The bridge is
        # already running by the time registration returns, and only
        # ``created_session_ids`` tells the finally block to stop it. Waiting for
        # canonical visibility first meant every failed launch leaked its
        # codex-bridge and app-server for the life of the container -- 96
        # orphaned processes in 21 hours on clifford. The automation lane below
        # already reads the id in this order.
        session_id = str(fresh_registration["session_id"])
        created_session_ids.append(session_id)
        fresh_canonical = _wait_canonical_launch(
            args,
            registration=fresh_record,
            project=project,
            device_id=canonical_device_id,
            expected_actor="human_shell",
            expected_surface="terminal",
            # The factory token is deliberately bound to the
            # provider-factory-resume machine. Product policy hides every
            # session from that machine even when the launch itself has
            # genuine human Helm provenance. Prove the independent Open
            # working-set fact here and the mandatory factory isolation at
            # the default-list boundary; a factory canary cannot honestly
            # impersonate a neutral user machine.
            expect_visible=False,
            expect_open=True,
            launched_at=launched_at,
        )
        fresh_canonical["factory_machine_identity"] = canonical_device_id
        fresh_canonical["factory_policy_hidden"] = canonical_device_id == PROVIDER_FACTORY_MACHINE_ID
        seed_receipt = _seed_codex_rollout(
            fresh_tui,
            codex_home=Path(environment["CODEX_HOME"]),
            marker=f"_RESUME_SEED_LONGHOUSE_CODEX_HELM_{uuid.uuid4().hex}",
            timeout=args.wait_ready_secs,
        )
        write_json(root / "human-seed-receipt.json", seed_receipt)
        stop_receipts.append(_stop_launch(args, tui=fresh_tui, session_id=session_id, isolation_root=isolation_root))

        resumed_at = time.monotonic()
        resumed_tui = _start_tui(
            args,
            proxy_url=proxy.url,
            workspace=workspace,
            project=project,
            environment=environment,
            terminal_path=root / "resumed-terminal.log",
            resume_session=session_id,
        )
        launched_tuis.append(resumed_tui)
        pids.append(resumed_tui.process.pid)
        resumed_record = proxy.wait_registration(
            after=registration_offset + 1,
            timeout=args.wait_ready_secs,
            process=resumed_tui.process,
        )
        resumed_registration = _safe_registration(resumed_record)
        resumed_canonical = _wait_canonical_launch(
            args,
            registration=resumed_record,
            project=project,
            device_id=canonical_device_id,
            expected_actor="human_shell",
            expected_surface="terminal",
            expect_visible=False,
            expect_open=True,
            launched_at=resumed_at,
        )
        resumed_canonical["factory_machine_identity"] = canonical_device_id
        resumed_canonical["factory_policy_hidden"] = canonical_device_id == PROVIDER_FACTORY_MACHINE_ID
        stop_receipts.append(_stop_launch(args, tui=resumed_tui, session_id=session_id, isolation_root=isolation_root))
        write_json(root / "human-stop-receipts.json", {"stops": stop_receipts})
        write_json(root / "human-transcript-shipper-receipt.json", shipper.stop())
        cleanup = hide_and_verify_canary_isolation(
            lambda path, method, body: _runtime_request(args, path, method, body),
            session_id=session_id,
            provider="codex",
            project=project,
            device_id=canonical_device_id,
            cwd=str(workspace),
            owned_processes_dead=lambda: all(_pid_dead(pid) for pid in pids),
            timeout_seconds=args.cleanup_timeout_secs,
        )
        return (
            {"registration": fresh_registration, "canonical": fresh_canonical},
            {"registration": resumed_registration, "canonical": resumed_canonical},
            cleanup,
            registration_offset + 2,
        )
    finally:
        for session_id in dict.fromkeys(created_session_ids):
            try:
                bridge_canary._stop_bridge(args, session_id, isolation_root)
            except Exception:
                pass
        for tui in launched_tuis:
            try:
                tui.close()
            except Exception:
                pass
        shipper.stop()
        for session_id in dict.fromkeys(created_session_ids):
            try:
                _runtime_request(
                    args,
                    f"sessions/{session_id}/timeline-visibility",
                    "PATCH",
                    {"hidden": True},
                )
            except Exception:
                pass
        shutil.rmtree(isolation_root, ignore_errors=True)


def _automation_launch(
    args: argparse.Namespace,
    *,
    root: Path,
    proxy: RuntimeHostRecordingProxy,
    registration_offset: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    isolation_root = Path(tempfile.mkdtemp(prefix="lca-", dir="/tmp"))
    workspace = isolation_root / "workspace"
    workspace.mkdir(mode=0o700)
    project = f"provider-factory-codex-automation-{uuid.uuid4().hex}"
    environment = bridge_canary._provider_runtime_environment(os.environ, isolation_root)
    environment["LONGHOUSE_ENGINE_BIN"] = str(args.engine)
    login_receipt = login_with_api_key(
        args.codex_bin,
        api_key=os.environ["CODEX_API_KEY"],
        environment=environment,
        cwd=workspace,
    )
    write_json(root / "automation-auth-receipt.json", login_receipt)
    shipper = _start_transcript_shipper(
        "codex",
        args,
        home=Path(environment["HOME"]),
        environment=environment,
        evidence_root=root / "automation-shipper",
        longhouse_home=isolation_root / "longhouse",
    )
    canonical_device_id = str(shipper.receipt["machine_name"])
    facade_device_id = f"codex-automation-proof-{uuid.uuid4().hex}"
    _set_facade_machine_label(
        isolation_root,
        label=facade_device_id,
        api_url=args.api_url,
    )
    pids = [shipper.process.pid]
    tui: ProviderPtySession | None = None
    session_id = ""
    try:
        launched_at = time.monotonic()
        tui = _start_tui(
            args,
            proxy_url=proxy.url,
            workspace=workspace,
            project=project,
            environment=environment,
            terminal_path=root / "automation-terminal.log",
        )
        pids.append(tui.process.pid)
        record = proxy.wait_registration(
            after=registration_offset,
            timeout=args.wait_ready_secs,
            process=tui.process,
        )
        registration = _safe_registration(record)
        session_id = str(registration["session_id"])
        canonical = _wait_canonical_launch(
            args,
            registration=record,
            project=project,
            device_id=canonical_device_id,
            expected_actor="automation",
            expected_surface="test",
            expect_visible=False,
            expect_open=False,
            launched_at=launched_at,
        )
        stop = _stop_launch(
            args,
            tui=tui,
            session_id=session_id,
            isolation_root=isolation_root,
        )
        write_json(root / "automation-stop-receipt.json", stop)
        write_json(root / "automation-transcript-shipper-receipt.json", shipper.stop())
        cleanup = hide_and_verify_canary_isolation(
            lambda path, method, body: _runtime_request(args, path, method, body),
            session_id=session_id,
            provider="codex",
            project=project,
            device_id=canonical_device_id,
            cwd=str(workspace),
            owned_processes_dead=lambda: all(_pid_dead(pid) for pid in pids),
            timeout_seconds=args.cleanup_timeout_secs,
        )
        return {"registration": registration, "canonical": canonical}, cleanup
    finally:
        if session_id:
            try:
                bridge_canary._stop_bridge(args, session_id, isolation_root)
            except Exception:
                pass
        if tui is not None:
            try:
                tui.close()
            except Exception:
                pass
        shipper.stop()
        if session_id:
            try:
                _runtime_request(
                    args,
                    f"sessions/{session_id}/timeline-visibility",
                    "PATCH",
                    {"hidden": True},
                )
            except Exception:
                pass
        shutil.rmtree(isolation_root, ignore_errors=True)


def run_scenario(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.codex_bin),
        "sha256": sha256_file(args.codex_bin),
        "version": bridge_canary._run([str(args.codex_bin), "--version"], timeout=30).stdout.strip(),
    }
    write_json(root / "provider-binary-receipt.json", provider_receipt)
    proxy = RuntimeHostRecordingProxy(args.api_url)
    proxy.start()
    try:
        fresh, resumed, human_cleanup, offset = _human_launch_sequence(
            args,
            root=root,
            proxy=proxy,
            registration_offset=0,
        )
        automation, automation_cleanup = _automation_launch(
            args,
            root=root,
            proxy=proxy,
            registration_offset=offset,
        )
        observation: dict[str, Any] = {
            "fresh": fresh,
            "resumed": resumed,
            "automation": automation,
            "same_session_resumed": fresh["registration"]["session_id"] == resumed["registration"]["session_id"],
            "new_run_on_resume": fresh["registration"]["run_id"] != resumed["registration"]["run_id"],
            "cleanup": [human_cleanup, automation_cleanup],
        }
        negative = copy.deepcopy(observation)
        negative["fresh"]["registration"].pop("launch_actor", None)
        negative["fresh"]["registration"].pop("launch_surface", None)
        negative["provenance_free_observation_rejected"] = True
        provenance_free_rejected = helm_launch_assertions(negative)[ASSERTION_ID] is False
        observation["provenance_free_observation_rejected"] = provenance_free_rejected
        assertions = helm_launch_assertions(observation)
        write_json(
            root / "launch-registration-receipts.json",
            {
                "fresh": fresh["registration"],
                "resumed": resumed["registration"],
                "automation": automation["registration"],
            },
        )
        write_json(
            root / "canonical-visibility-receipts.json",
            {
                "fresh": fresh["canonical"],
                "resumed": resumed["canonical"],
                "automation": automation["canonical"],
            },
        )
        cleanup_status, cleanup_requirements = _cleanup_evidence(observation["cleanup"])
        write_json(
            root / "canary-isolation-receipt.json",
            {
                "status": cleanup_status,
                "sessions": observation["cleanup"],
                "requirements": cleanup_requirements,
                "provenance_free_observation_rejected": provenance_free_rejected,
            },
        )
        write_json(
            root / "cleanup-receipt.json",
            {
                "status": cleanup_status,
                "sessions": observation["cleanup"],
                "requirements": cleanup_requirements,
            },
        )
        status = "pass" if assertions.get(ASSERTION_ID) is True else "fail"
        result = {
            "schema_version": 1,
            "artifact_kind": "codex_helm_launch_visibility_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": status,
            "observation": observation,
            "assertions": assertions,
            "provider_binary": provider_receipt,
            "session_id": fresh["registration"]["session_id"],
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = {
            "schema_version": 1,
            "artifact_kind": "codex_helm_launch_visibility_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "codex",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "codex_helm_launch_visibility_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", failure)
        return failure
    finally:
        proxy.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=(_EXECUTION_VARIANT,))
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True, type=Path)
    parser.add_argument(
        "--longhouse-cli",
        type=Path,
        default=os.environ.get("LONGHOUSE_CLI_BIN") or shutil.which("longhouse"),
    )
    parser.add_argument("--model", default=os.environ.get("CODEX_MODEL"))
    parser.add_argument("--wait-ready-secs", type=float, default=45.0)
    parser.add_argument("--cleanup-timeout-secs", type=float, default=30.0)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    args.script_bin = None
    args.timeout_bin = None
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    if not os.environ.get("CODEX_API_KEY"):
        print(json.dumps({"status": "fail", "failure_code": "codex_provider_token_missing"}))
        return 2
    for name in ("engine", "codex_bin", "longhouse_cli"):
        path = getattr(args, name)
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": f"{name}_missing"}))
            return 2
    result = run_scenario(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
