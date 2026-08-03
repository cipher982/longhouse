"""Claude Code adapter and evidence normalization for the universal provider harness."""

from __future__ import annotations

import json
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Mapping
from uuid import NAMESPACE_URL
from uuid import uuid5

from zerg.qa.universal_agent_harness import STATUS_BLOCKED
from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import _clean_optional_str
from zerg.qa.universal_agent_harness import _subprocess_runtime_env
from zerg.qa.universal_agent_harness import _uniform_operation_evidence
from zerg.qa.universal_agent_harness import register_adapter
from zerg.qa.universal_agent_harness import run_provider_control_e2e_canary


def _claude_control_canary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    canary = dict(dict(artifact.get("canaries") or {}).get("claude") or {})
    return canary


def _first_claude_control_session_id(canary: Mapping[str, Any]) -> str | None:
    cleaned = _clean_optional_str(canary.get("session_id"))
    if cleaned:
        return cleaned
    session_ids = canary.get("session_ids")
    if isinstance(session_ids, list):
        for session_id in session_ids:
            cleaned = _clean_optional_str(session_id)
            if cleaned:
                return cleaned
    result_event = canary.get("result_event")
    if isinstance(result_event, Mapping):
        return _clean_optional_str(result_event.get("session_id"))
    return None


def claude_provider_live_raw_events(artifact: Mapping[str, Any], *, provider_session_id: str) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    rows: list[dict[str, Any]] = []
    binary_identity = dict(canaries.get("binary_identity") or {})
    command_shape = dict(canaries.get("command_shape") or {})
    channels_shape = dict(canaries.get("channels_shape") or {})
    detached_pty = dict(canaries.get("detached_pty_shape") or {})
    provider_version = artifact.get("provider_version") or binary_identity.get("version")
    if binary_identity:
        rows.append(
            {
                "type": "session_start",
                "role": "system",
                "text": f"Claude binary identity captured: {provider_version}",
                "provider_session_id": provider_session_id,
                "source_canary": "binary_identity",
                "status": binary_identity.get("status"),
                "provider_version": provider_version,
                "evidence_origin": "provider_live_canary",
            }
        )
    if command_shape:
        rows.append(
            {
                "type": "launch_contract",
                "role": "system",
                "text": "Claude launch/session command contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "command_shape",
                "status": command_shape.get("status"),
                "missing": command_shape.get("missing"),
                "failure_code": command_shape.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if channels_shape:
        rows.append(
            {
                "type": "external_event_channel",
                "role": "system",
                "text": "Claude development channel contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "channels_shape",
                "status": channels_shape.get("status"),
                "missing": channels_shape.get("missing"),
                "reason": channels_shape.get("reason"),
                "failure_code": channels_shape.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    if detached_pty:
        rows.append(
            {
                "type": "runtime_phase",
                "role": "system",
                "text": "Claude detached PTY wrapper contract checked.",
                "provider_session_id": provider_session_id,
                "source_canary": "detached_pty_shape",
                "status": detached_pty.get("status"),
                "platform": detached_pty.get("platform"),
                "script_path": detached_pty.get("script_path"),
                "failure_code": detached_pty.get("failure_code"),
                "evidence_origin": "provider_live_canary",
            }
        )
    return rows


def claude_provider_live_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    operation_evidence = {
        str(operation): dict(evidence)
        for operation, evidence in dict(artifact.get("operation_evidence") or {}).items()
        if isinstance(evidence, Mapping)
    }
    canaries = dict(artifact.get("canaries") or {})
    channels_shape = dict(canaries.get("channels_shape") or {})
    pty_shape = dict(canaries.get("detached_pty_shape") or {})
    channel_status = STATUS_PASS if channels_shape.get("status") == "pass" else STATUS_FAIL
    pty_status = STATUS_PASS if pty_shape.get("status") == "pass" else STATUS_FAIL
    if channels_shape.get("status") == "warn":
        channel_status = STATUS_BLOCKED
    operation_evidence.setdefault(
        "external_event_channel",
        {
            "status": channel_status,
            "level": "live_no_token" if channel_status == STATUS_PASS else "none",
            "canary": "claude_development_channels_contract",
            "failure_code": channels_shape.get("failure_code") or channels_shape.get("reason"),
        },
    )
    operation_evidence.setdefault(
        "runtime_phase",
        {
            "status": pty_status,
            "level": "live_no_token" if pty_status == STATUS_PASS else "none",
            "canary": "claude_detached_pty_shape",
            "failure_code": pty_shape.get("failure_code"),
        },
    )
    for operation in ("send_input", "steer_active_turn"):
        operation_evidence.setdefault(
            operation,
            {
                "status": STATUS_BLOCKED,
                "level": "live_token_required",
                "canary": "claude_live_token_contract",
                "failure_code": "claude_live_token_contract_not_run",
                "next": "Run the explicit Claude live-token provider-live contract before gating live send/steer.",
            },
        )
    return operation_evidence


def claude_channel_control_raw_events(canary: Mapping[str, Any]) -> list[dict[str, Any]]:
    session_id = _first_claude_control_session_id(canary) or "claude-channel-control"
    rows: list[dict[str, Any]] = []
    if canary:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": "hello from provider control canary",
                "provider_session_id": session_id,
                "source_canary": "claude_channel_control",
                "meta": canary.get("send_meta"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": "steer from provider control canary",
                "provider_session_id": session_id,
                "source_canary": "claude_channel_control",
                "intent": "steer",
                "meta": canary.get("steer_meta"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "system",
                "role": "system",
                "text": "Claude channel interrupt delivered SIGINT to the owned fake provider process.",
                "provider_session_id": session_id,
                "source_canary": "claude_channel_control",
                "interrupt_marker": canary.get("interrupt_marker"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def claude_real_print_raw_events(canary: Mapping[str, Any]) -> list[dict[str, Any]]:
    provider_session_id = _first_claude_control_session_id(canary) or "claude-real-print"
    marker = str(canary.get("marker") or "marker-unavailable")
    prompt = f"Reply with exactly {marker} and nothing else."
    result_event = canary.get("result_event")
    result_event = result_event if isinstance(result_event, Mapping) else {}
    exact_match = bool(result_event.get("result_exact_match"))
    rows: list[dict[str, Any]] = []
    if canary:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": prompt,
                "provider_session_id": provider_session_id,
                "source_canary": "claude_real_print",
                "marker": marker,
                "prompt_sha256": canary.get("prompt_sha256"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": marker if exact_match else "",
                "provider_session_id": provider_session_id,
                "source_canary": "claude_real_print",
                "result_exact_match": exact_match,
                "session_id_present": result_event.get("session_id_present"),
                "evidence_origin": "provider_control_e2e_canary",
            }
        )
    return rows


def claude_channel_control_operation_evidence(canary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return _uniform_operation_evidence(
        passed=canary.get("status") == "pass",
        level="live_no_token",
        canary="claude_channel_control",
        default_failure_code="claude_channel_control_failed",
        operations=("send_input", "steer_active_turn", "interrupt"),
        raw_failure_code=canary.get("failure_code"),
    )


def claude_real_print_operation_evidence(canary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return _uniform_operation_evidence(
        passed=canary.get("status") == "pass",
        level="live_token",
        canary="claude_real_print",
        default_failure_code="claude_real_print_failed",
        operations=("run_once", "live_token_behavior"),
        raw_failure_code=canary.get("failure_code"),
        seed=canary.get("operation_evidence"),
    )


@register_adapter("claude")
class ClaudeCodeHarnessAdapter(UniversalProviderAdapter):
    """Claude Code concrete adapter for the universal Longhouse action contract.

    Phase 3 of docs/specs/provider-factory-coherence.md ("split the
    adapter"): third extraction slice (after Antigravity, OpenCode), per
    Hatch Sol design review 2026-07-28.
    """

    def conversation_reset(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.qa.conversation_reset import consume_live_reset_artifact

        return consume_live_reset_artifact(self, package, provider="claude") or super().conversation_reset(package)

    def launch_managed_session(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_claude_provider_live_projection(
            package,
            scenario="launch_managed_session",
            assertion_name="launch_managed_session",
            require_operation="launch_local",
        )

    def managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        if not self.config.real_managed_session_e2e:
            payload = self._unsupported_payload(
                "managed_session_e2e",
                "managed_session_e2e_not_migrated",
                "No real no-token managed-session e2e adapter is implemented for this provider yet.",
            )
            package.write_json("assertions/managed_session_e2e.json", payload)
            return payload
        return self._run_claude_provider_live_projection(
            package,
            scenario="managed_session_e2e",
            assertion_name="managed_session_e2e",
        )

    def external_event_channel(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_claude_provider_live_projection(
            package,
            scenario="external_event_channel",
            assertion_name="external_event_channel",
            require_operation="external_event_channel",
        )

    def permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        """Prove the real Claude PreToolUse permission-gate loop end to end.

        Unlike a hand-faked contract, this drives Longhouse's actual code: the
        real ``permission_gate.py`` hook subprocess registers a held request and
        polls, served by the real ``upsert_pause_request`` store on a throwaway
        SQLite, and a background thread answers via the real pull-mode resolve
        (``resolve_pause_request`` writing permissionDecision). The hook must then
        emit ``permissionDecision: allow``.
        """
        import http.server as _http_server
        import subprocess as _subprocess
        import sys as _sys
        import threading as _threading
        import time as _time

        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession

        # Materialize the canonical installed hook (the exact bytes install_hooks
        # writes) and drive that, so the proof matches production.
        from zerg.services.shipper.hooks import PERMISSION_GATE_SCRIPT

        hook_script = package.path("hooks", "longhouse-permission-gate.py")
        hook_script.parent.mkdir(parents=True, exist_ok=True)
        hook_script.write_text(PERMISSION_GATE_SCRIPT)
        hook_script.chmod(0o755)

        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        session_uuid = uuid5(NAMESPACE_URL, f"claude-permission-prompt:{package.root}")
        session_id = str(session_uuid)
        tool_use_id = "toolu_universal_claude_perm"
        db_path = package.path("longhouse", "claude-permission-gate.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)

        with session_factory() as db:
            db.add(
                AgentSession(
                    id=session_uuid,
                    provider="claude",
                    environment="test",
                    project="universal-agent-harness",
                    device_id="universal-harness",
                    cwd=str(package.path("workspace")),
                    started_at=now - timedelta(minutes=1),
                )
            )
            db.commit()

        captured: dict[str, Any] = {"register_seen": False, "polls": 0, "ack": None}

        # The stub socket exists only because the hook is a real subprocess that
        # needs a URL. Its handlers delegate to the REAL endpoint coroutines +
        # the REAL pause-response route, authenticated by a real session-scoped
        # ManagedSessionToken — so the genuine auth-scope, _is_permission_gate_row
        # filter, explicit-allow decision logic, and answer route all execute.
        import asyncio as _asyncio

        from zerg.auth.managed_session_tokens import MANAGED_SESSION_SCOPE_HOOK
        from zerg.auth.managed_session_tokens import ManagedSessionToken
        from zerg.routers.permission_gate import PermissionRequestIn
        from zerg.routers.permission_gate import get_permission_decision
        from zerg.routers.permission_gate import register_permission_request

        scoped_token = ManagedSessionToken(
            session_id=session_id,
            owner_id=1,
            device_id="universal-harness",
            scope=MANAGED_SESSION_SCOPE_HOOK,
        )

        def _register(body: dict[str, Any]) -> dict[str, Any]:
            with session_factory() as db:
                ack = _asyncio.run(
                    register_permission_request(
                        payload=PermissionRequestIn(
                            session_id=session_id,
                            tool_use_id=str(body.get("tool_use_id") or tool_use_id),
                            tool_name=str(body.get("tool_name") or ""),
                            tool_input=body.get("tool_input") or {},
                        ),
                        db=db,
                        _token=scoped_token,
                    )
                )
                db.commit()
                return {"pause_request_id": ack.pause_request_id, "request_key": ack.request_key, "status": ack.status}

        def _decision(query: dict[str, str]) -> dict[str, Any]:
            with session_factory() as db:
                out = _asyncio.run(
                    get_permission_decision(
                        session_id=session_id,
                        tool_use_id=query.get("tool_use_id", tool_use_id),
                        pause_request_id=query.get("pause_request_id"),
                        db=db,
                        _token=scoped_token,
                    )
                )
                return {"decision": out.decision, "reason": out.reason, "resolved": out.resolved}

        class Handler(_http_server.BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return

            def _send(self, code: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                if self.path == "/api/agents/permission-requests":
                    length = int(self.headers.get("Content-Length") or "0")
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
                    ack = _register(body)
                    captured["register_seen"] = True
                    captured["ack"] = ack
                    self._send(200, ack)
                else:
                    self._send(404, {})

            def do_GET(self) -> None:
                if self.path.startswith("/api/agents/permission-decision"):
                    captured["polls"] += 1
                    from urllib.parse import parse_qs
                    from urllib.parse import urlparse

                    q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                    self._send(200, _decision(q))
                else:
                    self._send(404, {})

        server = _http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = _threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        # Background answerer: once the hook has registered the request, answer it
        # through the REAL pause-response route (_respond_to_pause_request), which
        # runs the genuine transport dispatch -> pull resolve-in-place. The polling
        # hook then reads the decision.
        stop = _threading.Event()
        answer_result: dict[str, Any] = {"answered": False, "status": None}

        def _answer_when_registered() -> None:
            import asyncio as _aio

            from zerg.models.agents import SessionPauseRequest
            from zerg.routers.session_chat import PauseRequestResponseRequest
            from zerg.routers.session_chat import _respond_to_pause_request
            from zerg.services.session_pause_requests import PENDING_STATUS as _PENDING

            for _ in range(200):
                if stop.is_set():
                    return
                with session_factory() as db:
                    row = (
                        db.query(SessionPauseRequest)
                        .filter(
                            SessionPauseRequest.session_id == session_uuid,
                            SessionPauseRequest.kind == "permission_prompt",
                        )
                        .first()
                    )
                    if row is not None and row.status == _PENDING:
                        source_session = db.query(AgentSession).filter(AgentSession.id == session_uuid).first()
                        try:
                            resp = _aio.run(
                                _respond_to_pause_request(
                                    source_session=source_session,
                                    owner_id=1,
                                    pause_request_id=str(row.id),
                                    body=PauseRequestResponseRequest(decision="answer"),
                                    db=db,
                                )
                            )
                            answer_result["answered"] = True
                            answer_result["status"] = getattr(resp, "status", None)
                        except Exception as exc:  # pragma: no cover - defensive
                            answer_result["error"] = f"{type(exc).__name__}: {exc}"
                        db.commit()
                        return
                _time.sleep(0.05)

        answerer = _threading.Thread(target=_answer_when_registered, daemon=True)
        answerer.start()

        hook_env = {
            **_subprocess_runtime_env(),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LONGHOUSE_PERMISSION_HOOK_ENABLED": "1",
            "LONGHOUSE_HOOK_URL": base_url,
            "LONGHOUSE_HOOK_TOKEN": "zst_universal_harness",
            "LONGHOUSE_MANAGED_SESSION_ID": session_id,
            "LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S": "10",
        }
        hook_input = json.dumps(
            {
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            }
        )
        hook_error: str | None = None
        hook_stdout = ""
        try:
            completed = _subprocess.run(
                [_sys.executable, str(hook_script)],
                input=hook_input,
                capture_output=True,
                text=True,
                timeout=20,
                env=hook_env,
            )
            hook_stdout = completed.stdout or ""
        except Exception as exc:  # pragma: no cover - defensive
            hook_error = f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            answerer.join(timeout=5)

        emitted_decision: str | None = None
        stdout_clean = hook_stdout.strip()
        if stdout_clean:
            try:
                emitted_decision = json.loads(stdout_clean)["hookSpecificOutput"]["permissionDecision"]
            except (json.JSONDecodeError, KeyError, TypeError):
                emitted_decision = None

        assertions = {
            "request_registered_via_real_endpoint": bool(captured["register_seen"]),
            "hook_polled_for_decision": captured["polls"] >= 1,
            "answered_via_real_pause_route": bool(answer_result.get("answered")),
            "hook_emitted_allow": emitted_decision == "allow",
            "hook_no_error": hook_error is None,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/claude-permission-gate.json",
            {
                "base_url": base_url,
                "session_id": session_id,
                "tool_use_id": tool_use_id,
                "answer_result": answer_result,
                "hook_stdout": hook_stdout,
                "emitted_decision": emitted_decision,
                "hook_error": hook_error,
                "polls": captured["polls"],
            },
        )
        operation_evidence = {
            "permission_prompt": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "claude_permission_gate_reply",
                "failure_code": None if passed else "claude_permission_gate_failed",
            }
        }
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "permission_prompt",
            "assertions": assertions,
            "raw_permission_gate_path": str(raw_path),
            "operation_evidence": operation_evidence,
            "proof_scope": "claude_pretooluse_permission_gate",
        }
        if not passed:
            payload["failure_code"] = "claude_permission_gate_failed"
            payload["message"] = "Claude PreToolUse permission-gate loop did not pass."
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="claude",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        claude = _claude_control_canary(control_artifact)
        operation_evidence = claude_channel_control_operation_evidence(claude)
        raw_events = claude_channel_control_raw_events(claude)
        provider_session_id = _first_claude_control_session_id(claude) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        interrupt_status = str((operation_evidence.get("interrupt") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and interrupt_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "interrupt_cancel",
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or interrupt_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or claude.get("failure_code")
            payload["failure_code"] = failure_code or "claude_interrupt_cancel_failed"
            payload["message"] = "Claude channel interrupt canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "interrupt_cancel_db_ingest_failed"
            payload["message"] = "Claude interrupt evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def steer_active_turn(self, package: EvidencePackage) -> dict[str, Any]:
        payload = dict(self.interrupt_cancel(package))
        operation_evidence = {
            str(operation): dict(evidence)
            for operation, evidence in dict(payload.get("operation_evidence") or {}).items()
            if isinstance(evidence, Mapping)
        }
        steer_status = str((operation_evidence.get("steer_active_turn") or {}).get("status") or STATUS_FAIL)
        db_status = str(((payload.get("longhouse_ingest") or {}).get("status")) or STATUS_FAIL)
        verdict = str(payload.get("provider_control_verdict") or "red")
        passed = verdict == "green" and steer_status == STATUS_PASS and db_status == STATUS_PASS
        payload["status"] = STATUS_PASS if passed else STATUS_FAIL
        payload["scenario"] = "steer_active_turn"
        if passed:
            payload.pop("failure_code", None)
            payload.pop("message", None)
        elif verdict != "green" or steer_status != STATUS_PASS:
            payload["failure_code"] = payload.get("failure_code") or "claude_steer_active_turn_failed"
            payload["message"] = "Claude channel steer canary did not pass."
        else:
            payload["failure_code"] = payload.get("failure_code") or "steer_active_turn_db_ingest_failed"
            payload["message"] = "Claude steer evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/steer_active_turn.json", payload)
        return payload

    def resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        return self._claude_resume_command_proof(
            package,
            scenario="resume_reattach",
            operation="reattach",
            proof_scope="claude_channel_resume_command_shape",
        )

    def cold_resume(self, package: EvidencePackage) -> dict[str, Any]:
        return self._claude_resume_command_proof(
            package,
            scenario="helm_cold_resume_native",
            operation="resume",
            proof_scope="claude_native_cold_resume_command",
        )

    def _claude_resume_command_proof(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        operation: str,
        proof_scope: str,
    ) -> dict[str, Any]:
        from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_DEVELOPMENT_FLAG
        from zerg.services.claude_channel_bridge import CLAUDE_CHANNEL_SERVER_NAME
        from zerg.services.claude_channel_bridge import build_claude_channel_exec_command

        binary, failure = self._require_binary(package, scenario)
        if failure is not None:
            return failure
        assert binary is not None
        provider_session_id = "11111111-1111-1111-1111-111111111111"
        longhouse_session_id = "22222222-2222-4222-8222-222222222222"
        cwd = str(package.path("workspace"))
        command = build_claude_channel_exec_command(
            provider_session_id=provider_session_id,
            longhouse_session_id=longhouse_session_id,
            cwd=cwd,
            resume=True,
            claude_command=str(binary),
        )
        assertions = {
            "uses_resume_flag": f"--resume {provider_session_id}" in command,
            "does_not_use_session_id_flag": f"--session-id {provider_session_id}" not in command,
            "exports_longhouse_session_id": f"LONGHOUSE_CHANNEL_SESSION_ID={longhouse_session_id}" in command,
            "exports_provider_session_id": f"LONGHOUSE_PROVIDER_SESSION_ID={provider_session_id}" in command,
            "loads_development_channel": CLAUDE_CHANNEL_DEVELOPMENT_FLAG in command,
            "loads_longhouse_channel_server": f"server:{CLAUDE_CHANNEL_SERVER_NAME}" in command,
            "changes_to_workspace": cwd in command,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/claude-resume-command.json",
            {
                "command": command,
                "provider_session_id": provider_session_id,
                "longhouse_session_id": longhouse_session_id,
                "cwd": cwd,
                "assertions": assertions,
            },
        )
        operations = {
            operation: {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": (
                    "claude_native_cold_resume_command_shape"
                    if scenario == "helm_cold_resume_native"
                    else "claude_channel_resume_command_shape"
                ),
                "failure_code": None if passed else "claude_resume_command_shape_failed",
                "source": "zerg.services.claude_channel_bridge.build_claude_channel_exec_command",
            }
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "system",
                    "role": "system",
                    "text": "Claude channel resume command shape was built for an existing provider session.",
                    "provider_session_id": provider_session_id,
                    "source_canary": "claude_channel_resume_command_shape",
                    "evidence_origin": "claude_channel_bridge_command_shape",
                },
            ),
            operations=operations,
            provider_session_id=provider_session_id,
        )
        next_gate = "Promote with launch, process restart, reattach, and send against the same provider session id."
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": scenario,
                "assertions": assertions,
                "raw_resume_command_path": str(raw_path),
                "proof_scope": proof_scope,
                "synthetic": False,
                "next": next_gate,
            }
        )
        if not passed:
            payload["failure_code"] = "claude_resume_command_shape_failed"
            payload["message"] = "Claude resume command shape did not pass."
        package.write_json(f"assertions/{scenario}.json", payload)
        return payload

    def live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "live_token_streaming")
        if binary_error is not None:
            return binary_error

        control_evidence_root = package.path("raw", "provider-control-e2e-evidence")
        control_artifact_path = package.path("raw", "provider-control-e2e.json")
        control_artifact = run_provider_control_e2e_canary(
            provider="claude",
            artifact_path=control_artifact_path,
            evidence_root=control_evidence_root,
            extra_args=["--claude-run-real-print"],
            extra_env={"LONGHOUSE_CLAUDE_BIN": str(binary)},
        )
        if not control_artifact_path.is_file():
            package.write_json("raw/provider-control-e2e.json", control_artifact)
        package.write_json("raw/provider-control-e2e-inline.json", control_artifact)

        claude = _claude_control_canary(control_artifact)
        operation_evidence = claude_real_print_operation_evidence(claude)
        raw_events = claude_real_print_raw_events(claude)
        provider_session_id = _first_claude_control_session_id(claude) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(control_artifact.get("verdict") or "red")
        live_status = str((operation_evidence.get("live_token_behavior") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and live_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "live_token_streaming",
            "provider_version": claude.get("provider_version"),
            "provider_control_artifact_path": str(control_artifact_path),
            "provider_control_evidence_root": str(control_evidence_root),
            "provider_control_verdict": verdict,
            "source_artifact_kind": "provider_control_e2e_canary",
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or live_status != STATUS_PASS:
            failure_code = control_artifact.get("failure_code") or claude.get("failure_code")
            payload["failure_code"] = failure_code or "claude_live_token_streaming_failed"
            payload["message"] = "Claude real-print live-token canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "Claude live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def _run_claude_provider_live_projection(
        self,
        package: EvidencePackage,
        *,
        scenario: str,
        assertion_name: str,
        require_operation: str | None = None,
    ) -> dict[str, Any]:
        binary, source = self._resolve_binary()
        if binary is None:
            payload = {
                "status": STATUS_FAIL,
                "failure_code": "provider_binary_not_found",
                "message": f"claude binary was not found for {scenario}",
                "binary_source": source,
            }
            package.write_json(f"assertions/{assertion_name}.json", payload)
            return payload

        from zerg.qa.provider_live_canary import run_provider_live_canary

        live_evidence_root = package.path("raw", "provider-live-evidence")
        live_artifact_path = package.path("raw", "provider-live-canary.json")
        live_artifact = run_provider_live_canary(
            {
                "provider": "claude",
                "provider_bin": str(binary),
                "artifact": live_artifact_path,
                "evidence_root": live_evidence_root,
                "wait_ready_secs": 15.0,
                "json": False,
            }
        )
        package.write_json("raw/provider-live-canary-inline.json", live_artifact)
        operation_evidence = claude_provider_live_operation_evidence(live_artifact)
        provider_session_id = str(live_artifact.get("provider_session_id") or self._session_id(package))
        raw_events = claude_provider_live_raw_events(live_artifact, provider_session_id=provider_session_id)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        live_verdict = str(live_artifact.get("verdict") or "red")
        db_verdict = str(db_ingest.get("status") or STATUS_FAIL)
        status = STATUS_PASS if live_verdict == "green" and db_verdict == STATUS_PASS else STATUS_FAIL
        if live_verdict == "yellow" and db_verdict == STATUS_PASS:
            status = STATUS_BLOCKED
        payload = {
            **projection,
            "status": status,
            "scenario": scenario,
            "provider_version": live_artifact.get("provider_version"),
            "provider_live_artifact_path": str(live_artifact_path),
            "provider_live_evidence_root": str(live_evidence_root),
            "provider_live_verdict": live_verdict,
            "source_artifact_kind": live_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if live_verdict == "red":
            payload["failure_code"] = live_artifact.get("failure_code") or "provider_live_canary_failed"
            payload["message"] = "Claude provider-live no-token canary did not pass."
        elif live_verdict == "yellow":
            payload["failure_code"] = live_artifact.get("failure_code") or "claude_provider_live_unconfirmed"
            payload["message"] = "Claude provider-live no-token canary is recognized but not fully confirmed."
        elif db_verdict != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or f"{scenario}_db_ingest_failed"
            payload["message"] = "Claude provider-live evidence did not pass Longhouse DB ingest assertions."
        if require_operation and status == STATUS_PASS:
            operation_status = str((operation_evidence.get(require_operation) or {}).get("status") or STATUS_FAIL)
            if operation_status != STATUS_PASS:
                payload["status"] = STATUS_FAIL
                payload["failure_code"] = f"claude_{require_operation}_evidence_missing"
                message = f"Claude provider-live canary did not produce passing {require_operation} evidence."
                payload["message"] = message
        package.write_json(f"assertions/{assertion_name}.json", payload)
        return payload
