"""Codex adapter and evidence normalization for the universal provider harness."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import Mapping

from zerg.qa.repo_root import default_repo_root
from zerg.qa.universal_agent_harness import STATUS_BLOCKED
from zerg.qa.universal_agent_harness import STATUS_FAIL
from zerg.qa.universal_agent_harness import STATUS_PASS
from zerg.qa.universal_agent_harness import STATUS_UNSUPPORTED_GAP
from zerg.qa.universal_agent_harness import EvidencePackage
from zerg.qa.universal_agent_harness import UniversalProviderAdapter
from zerg.qa.universal_agent_harness import _clean_optional_str
from zerg.qa.universal_agent_harness import _project_managed_transport
from zerg.qa.universal_agent_harness import _seed_managed_kernel_rows
from zerg.qa.universal_agent_harness import _uniform_operation_evidence
from zerg.qa.universal_agent_harness import register_adapter
from zerg.services.managed_provider_contracts import contract_for_provider


def _first_codex_thread_id(artifact: Mapping[str, Any]) -> str | None:
    canaries = dict(artifact.get("canaries") or {})
    for name in ("managed_tui_attach", "managed_live_send", "managed_live_interrupt"):
        canary = canaries.get(name)
        if isinstance(canary, Mapping):
            thread_id = _clean_optional_str(canary.get("thread_id"))
            if thread_id:
                return thread_id
    return None


def codex_provider_release_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    managed_tui = dict(canaries.get("managed_tui_attach") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-managed-session-e2e"
    rows: list[dict[str, Any]] = []
    if managed_tui:
        rows.append(
            {
                "type": "session_start",
                "role": "system",
                "text": "Codex managed TUI bridge attached to a provider thread.",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_tui_attach",
                "thread_id": managed_tui.get("thread_id"),
                "status": managed_tui.get("status"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    if not rows:
        rows.append(
            {
                "type": "system",
                "role": "system",
                "text": "Codex managed-session e2e canary produced no runnable managed bridge rows.",
                "provider_session_id": provider_session_id,
                "source_canary": "codex_provider_release_canary",
                "status": artifact.get("verdict"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def codex_interrupt_cancel_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    interrupt = dict(canaries.get("managed_live_interrupt") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-interrupt-cancel"
    rows: list[dict[str, Any]] = []
    marker = interrupt.get("marker")
    if interrupt:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": f"Codex managed live-interrupt canary turn started: {marker or 'marker-unavailable'}",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_interrupt",
                "thread_id": interrupt.get("thread_id"),
                "state_file": interrupt.get("state_file"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
        rows.append(
            {
                "type": "interrupt",
                "role": "system",
                "text": f"Codex interrupt result: {interrupt.get('last_turn_status')}",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_interrupt",
                "last_turn_status": interrupt.get("last_turn_status"),
                "status": interrupt.get("status"),
                "failure_code": interrupt.get("failure_code"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def codex_live_token_streaming_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    live_send = dict(canaries.get("managed_live_send") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-live-token-streaming"
    marker = str(live_send.get("marker") or "marker-unavailable")
    rows: list[dict[str, Any]] = []
    if live_send:
        rows.append(
            {
                "type": "user",
                "role": "user",
                "text": f"Reply exactly {marker} and nothing else.",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_send",
                "thread_id": live_send.get("thread_id"),
                "state_file": live_send.get("state_file"),
                "thread_path": live_send.get("thread_path"),
                "send_summary": live_send.get("send_summary"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": marker if live_send.get("status") == STATUS_PASS else "",
                "provider_session_id": provider_session_id,
                "source_canary": "managed_live_send",
                "thread_id": live_send.get("thread_id"),
                "thread_path": live_send.get("thread_path"),
                "marker_found": live_send.get("status") == STATUS_PASS,
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def codex_tool_call_result_raw_events(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    canaries = dict(artifact.get("canaries") or {})
    tool = dict(canaries.get("codex_real_tool_result_shape") or {})
    provider_session_id = _first_codex_thread_id(artifact) or "codex-tool-call-result"
    command_event = tool.get("matching_command_event")
    command_event = command_event if isinstance(command_event, Mapping) else {}
    done_event = tool.get("done_text_event")
    done_event = done_event if isinstance(done_event, Mapping) else {}
    tool_call_id = str(command_event.get("id") or "codex-real-tool-result-shape")
    command = command_event.get("command") or tool.get("command")
    output = command_event.get("aggregated_output")
    if output is None and tool.get("output_exact_match"):
        output = f"{tool.get('marker', 'marker-unavailable')}\n"
    rows: list[dict[str, Any]] = []
    if tool:
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": "Codex requested a shell command through the real-tool canary.",
                "provider_session_id": provider_session_id,
                "source_canary": "codex_real_tool_result_shape",
                "tool_name": "shell",
                "tool_input_json": {"command": command},
                "tool_call_id": tool_call_id,
                "command_status": tool.get("command_status") or command_event.get("status"),
                "command_exit_code": tool.get("command_exit_code") or command_event.get("exit_code"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
        rows.append(
            {
                "type": "tool",
                "role": "tool",
                "text": str(output or ""),
                "provider_session_id": provider_session_id,
                "source_canary": "codex_real_tool_result_shape",
                "tool_name": "shell",
                "tool_output_text": str(output or ""),
                "tool_call_id": tool_call_id,
                "command_exact_match": tool.get("command_exact_match"),
                "output_exact_match": tool.get("output_exact_match"),
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    if done_event or tool.get("status") == "pass":
        rows.append(
            {
                "type": "assistant",
                "role": "assistant",
                "text": str(done_event.get("text") or "DONE"),
                "provider_session_id": provider_session_id,
                "source_canary": "codex_real_tool_result_shape",
                "evidence_origin": "codex_provider_release_canary",
            }
        )
    return rows


def codex_live_token_streaming_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    live_send = dict(dict(artifact.get("canaries") or {}).get("managed_live_send") or {})
    return _uniform_operation_evidence(
        passed=live_send.get("status") == "pass",
        level="live_token",
        canary="managed_live_send",
        default_failure_code="codex_live_token_streaming_failed",
        operations=("send_input", "live_token_behavior"),
        raw_failure_code=live_send.get("failure_code"),
        seed=artifact.get("operation_evidence"),
    )


def codex_tool_call_result_operation_evidence(artifact: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    tool = dict(dict(artifact.get("canaries") or {}).get("codex_real_tool_result_shape") or {})
    return _uniform_operation_evidence(
        passed=tool.get("status") == "pass",
        level="live_token",
        canary="codex_real_tool_result_shape",
        default_failure_code="codex_tool_call_result_failed",
        operations=("tool_call_result",),
        raw_failure_code=tool.get("failure_code"),
        seed=artifact.get("operation_evidence"),
    )


def _codex_canary_credentials_gap(artifact: Mapping[str, Any], canary_names: tuple[str, ...]) -> list[str]:
    canaries = dict(artifact.get("canaries") or {})
    missing: set[str] = set()
    for name in canary_names:
        canary = canaries.get(name)
        if not isinstance(canary, Mapping):
            continue
        if canary.get("failure_code") != "managed_bridge_credentials_missing":
            continue
        values = canary.get("missing")
        if isinstance(values, list):
            missing.update(str(value) for value in values if str(value))
    return sorted(missing)


def _codex_managed_bridge_credentials_gap(artifact: Mapping[str, Any]) -> list[str]:
    return _codex_canary_credentials_gap(artifact, ("managed_tui_attach",))


@register_adapter("codex")
class CodexOpenAIHarnessAdapter(UniversalProviderAdapter):
    """Codex/OpenAI concrete adapter for the universal Longhouse action contract.

    Phase 3 of docs/specs/provider-factory-coherence.md ("split the
    adapter"): fourth and final provider extraction slice (after
    Antigravity, OpenCode, Claude), per Hatch Sol design review
    2026-07-28. Deliberately done last -- largest and most
    launch-critical provider path.
    """

    def conversation_reset(self, package: EvidencePackage) -> dict[str, Any]:
        from zerg.qa.conversation_reset import consume_live_reset_artifact

        return consume_live_reset_artifact(self, package, provider="codex") or super().conversation_reset(package)

    def permission_prompt(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "permission_prompt")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-permission-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_args: dict[str, Any] = {
            "codex_bin": str(binary),
            "artifact": canary_artifact_path,
            "evidence_root": canary_evidence_root,
            "repo_root": default_repo_root(),
            "source_review_status": "pass",
            "skip_static_contract": True,
        }
        engine = os.environ.get("LONGHOUSE_ENGINE_BIN")
        if engine:
            canary_args.update(engine=engine, run_fake_app_server_binary=True)
        else:
            canary_args["run_fake_app_server"] = True
        canary_artifact = run_codex_provider_release_canary(canary_args)
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        operation_evidence = self._operation_evidence_map(canary_artifact.get("operation_evidence"))
        permission = dict(operation_evidence.get("permission_prompt") or {})
        verdict = str(canary_artifact.get("verdict") or "red")
        passed = verdict == "green" and permission.get("status") == STATUS_PASS
        payload = {
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "permission_prompt",
            "provider_version": canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": {
                "permission_prompt": permission
                or {
                    "status": STATUS_FAIL,
                    "level": "none",
                    "canary": "codex_fake_app_server_permission_approval",
                    "failure_code": "codex_permission_prompt_evidence_missing",
                }
            },
            "proof_scope": "codex_fake_app_server_permission_approval",
            "next": "Promote with a live held-permission Codex provider canary.",
        }
        if not passed:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_permission_prompt_failed"
            payload["message"] = "Codex fake app-server permission prompt canary did not pass."
        package.write_json("assertions/permission_prompt.json", payload)
        return payload

    def steer_active_turn(self, package: EvidencePackage) -> dict[str, Any]:
        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession
        from zerg.services import managed_local_control as control
        from zerg.session_execution_home import ManagedSessionTransport

        db_path = package.path("longhouse", "codex-steer-dispatch.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        steer_text = "Longhouse universal Codex steer transport proof."
        attachment_id = "11111111-1111-1111-1111-111111111111"
        blob_url = f"/api/agents/sessions/codex-steer/inputs/1/attachments/{attachment_id}/blob"
        attachments = [
            {
                "id": attachment_id,
                "mime_type": "image/png",
                "sha256": "a" * 64,
                "blob_url": blob_url,
            }
        ]
        calls: list[dict[str, Any]] = []
        codex_transport = ManagedSessionTransport.CODEX_APP_SERVER.value

        async def fake_dispatch(**kwargs: Any) -> SimpleNamespace:
            calls.append(
                {
                    "owner_id": kwargs.get("owner_id"),
                    "timeout_secs": kwargs.get("timeout_secs"),
                    "command_type": kwargs.get("command_type"),
                    "payload": kwargs.get("payload"),
                    "request_id": kwargs.get("request_id"),
                    "run_id": kwargs.get("run_id"),
                    "provider": getattr(kwargs.get("session"), "provider", None),
                    "managed_transport": _project_managed_transport(kwargs.get("db"), kwargs.get("session")),
                }
            )
            return SimpleNamespace(
                ok=True,
                transport="engine_channel",
                data={"exit_code": 0, "stdout": "", "stderr": ""},
                error=None,
            )

        original_dispatch = control.dispatch_managed_control_command
        original_transport_error = control._managed_control_transport_error
        control.dispatch_managed_control_command = fake_dispatch
        control._managed_control_transport_error = lambda *_args, **_kwargs: None
        try:
            with session_factory() as db:
                session = AgentSession(
                    provider="codex",
                    environment="test",
                    project="universal-agent-harness",
                    device_id="universal-harness",
                    cwd=str(package.path("workspace")),
                    started_at=now - timedelta(minutes=5),
                    last_activity_at=now,
                    user_messages=1,
                    assistant_messages=1,
                )
                db.add(session)
                db.flush()
                contract = contract_for_provider("codex")
                if contract is not None:
                    _seed_managed_kernel_rows(db, session, control_plane=contract.control_plane)
                result = asyncio.run(
                    control.steer_text_to_managed_local_session(
                        db=db,
                        owner_id=1,
                        session=session,
                        text=steer_text,
                        request_id="universal-codex-steer",
                        attachments=attachments,
                    )
                )
        finally:
            control.dispatch_managed_control_command = original_dispatch
            control._managed_control_transport_error = original_transport_error

        request = calls[0] if calls else {}
        expected_payload = {"text": steer_text, "intent": "steer", "attachments": attachments}
        assertions = {
            "command_dispatched": bool(calls),
            "command_type_matches": request.get("command_type") == "session.steer_text",
            "payload_matches": request.get("payload") == expected_payload,
            "provider_is_codex": request.get("provider") == "codex",
            "transport_is_codex_app_server": request.get("managed_transport") == codex_transport,
            "result_ok": result.ok is True,
            "exit_code_zero": result.exit_code == 0,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/codex-steer-dispatch.json",
            {
                "db_path": str(db_path),
                "calls": calls,
                "result": {
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "error": result.error,
                },
                "assertions": assertions,
            },
        )
        operations = {
            "steer_active_turn": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "codex_managed_local_steer_dispatch",
                "failure_code": None if passed else "codex_steer_dispatch_failed",
                "source": "zerg.services.managed_local_control.steer_text_to_managed_local_session",
            }
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "user",
                    "role": "user",
                    "text": steer_text,
                    "provider_session_id": "codex-steer-transport-session",
                    "source_canary": "codex_managed_local_steer_dispatch",
                    "intent": "steer",
                    "evidence_origin": "managed_local_control_transport_proof",
                },
            ),
            operations=operations,
            provider_session_id="codex-steer-transport-session",
        )
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "steer_active_turn",
                "assertions": assertions,
                "raw_steer_dispatch_path": str(raw_path),
                "proof_scope": "codex_managed_local_steer_dispatch",
                "synthetic": False,
            }
        )
        if not passed:
            payload["failure_code"] = "codex_steer_dispatch_failed"
            payload["message"] = "Codex managed-local steer dispatch did not pass."
        package.write_json("assertions/steer_active_turn.json", payload)
        return payload

    def interrupt_cancel(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "interrupt_cancel")
        if binary_error is not None:
            return binary_error

        from zerg.qa import codex_helm_interrupt
        from zerg.qa.codex_provider_release_canary import CODEX_AGENTS_TOKEN_ENV
        from zerg.qa.codex_provider_release_canary import CODEX_API_URL_ENV
        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-interrupt-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")

        # Stage 1: bridge credentials, checked BEFORE touching the process
        # environment -- mirrors codex_provider_release_canary's own
        # _managed_bridge_credentials_gap predicate (a preflight check, not
        # the post-execution artifact parse _codex_canary_credentials_gap
        # does). Missing either falls back to the existing hermetic dispatch
        # proof, exactly as before Phase 2, "closing the observation gap"
        # (docs/specs/provider-factory-coherence.md) -- just decided earlier,
        # so the canary (and any environment isolation) is never invoked at
        # all on this path, per Hatch Sol review.
        bridge_credentials_gap = [
            flag
            for flag, env_name in (("--api-url", CODEX_API_URL_ENV), ("--agents-token", CODEX_AGENTS_TOKEN_ENV))
            if not os.environ.get(env_name)
        ]
        if bridge_credentials_gap:
            return self._run_codex_interrupt_dispatch_proof(
                package,
                credentials_gap=bridge_credentials_gap,
                canary_artifact_path=canary_artifact_path,
                canary_evidence_root=canary_evidence_root,
                source_artifact_kind=None,
            )

        # Stage 2: the strict-lane inputs the release lane itself requires
        # (codex_helm_interrupt._required_environment()) before it will ever
        # replace the environment or start a live interrupt. Missing here is
        # BLOCKED, not the hermetic fallback -- bridge credentials exist, so
        # this is a genuine gap in the isolated-run inputs, not "no live
        # credentials at all."
        strict_values, strict_missing = codex_helm_interrupt._required_environment()  # noqa: SLF001
        if strict_missing:
            payload = {
                "status": STATUS_BLOCKED,
                "scenario": "interrupt_cancel",
                "failure_code": "codex_helm_strict_environment_missing",
                "message": "Strict Codex helm-interrupt isolation inputs are missing.",
                "missing": sorted(strict_missing),
            }
            package.write_json("assertions/interrupt_cancel.json", payload)
            return payload

        engine = Path(strict_values[codex_helm_interrupt.ENGINE_ENV])
        try:
            engine_identity = codex_helm_interrupt._file_identity(  # noqa: SLF001
                engine, label=codex_helm_interrupt.ENGINE_ENV, executable=True
            )
            _package_root_path, package_identity, _package_members = codex_helm_interrupt._package_identity(  # noqa: SLF001
                strict_values[codex_helm_interrupt.PACKAGE_ROOT_ENV], binary
            )
        except codex_helm_interrupt.identity_bridge.RequestError as exc:
            payload = {
                "status": STATUS_BLOCKED,
                "scenario": "interrupt_cancel",
                "failure_code": "codex_helm_strict_identity_invalid",
                "message": str(exc),
            }
            package.write_json("assertions/interrupt_cancel.json", payload)
            return payload

        def _operation(canary_root: Path, provider_bin: Path) -> dict[str, Any]:
            return run_codex_provider_release_canary(
                {
                    "codex_bin": str(provider_bin),
                    "engine": str(engine),
                    "artifact": canary_artifact_path,
                    "evidence_root": canary_root,
                    "repo_root": default_repo_root(),
                    "source_review_status": "pass",
                    "skip_static_contract": True,
                    "run_managed_live_interrupt": True,
                }
            )

        canary_artifact, canary_error, stop, mcp_bootstrap = codex_helm_interrupt.run_isolated_codex_operation(
            binary,
            engine=engine,
            package_root=strict_values[codex_helm_interrupt.PACKAGE_ROOT_ENV],
            api_url=strict_values[codex_helm_interrupt.API_URL_ENV],
            agents_token=strict_values[codex_helm_interrupt.AGENTS_TOKEN_ENV],
            provider_token=strict_values[codex_helm_interrupt.PROVIDER_TOKEN_ENV],
            output_root=canary_evidence_root,
            operation=_operation,
        )
        canary_artifact = canary_artifact or {}
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)

        strict_outcomes = codex_helm_interrupt.codex_helm_interrupt_oracle(
            canary_artifact.get("canaries", {}).get("managed_live_interrupt") or {},
            stop,
            canary_error=canary_error,
        )

        operation_evidence = self._operation_evidence_map(canary_artifact.get("operation_evidence"))
        raw_events = codex_interrupt_cancel_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        interrupt_status = str((operation_evidence.get("interrupt") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and interrupt_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "interrupt_cancel",
            "provider_version": canary_artifact.get("codex_version") or canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
            "engine_identity": engine_identity,
            "package_identity": package_identity,
            "strict_oracle": {key: value.value for key, value in strict_outcomes.items()},
            "mcp_bootstrap": mcp_bootstrap,
        }
        if verdict != "green" or interrupt_status != STATUS_PASS:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_interrupt_cancel_failed"
            payload["message"] = "Codex managed live interrupt canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "interrupt_cancel_db_ingest_failed"
            payload["message"] = "Codex interrupt evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def tool_call_result(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "tool_call_result")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-real-tool-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "skip_static_contract": True,
                "run_real_tool": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)

        operation_evidence = codex_tool_call_result_operation_evidence(canary_artifact)
        raw_events = codex_tool_call_result_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        tool_status = str((operation_evidence.get("tool_call_result") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = verdict == "green" and tool_status == STATUS_PASS and db_status == STATUS_PASS
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "tool_call_result",
            "provider_version": canary_artifact.get("codex_version") or canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or tool_status != STATUS_PASS:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_tool_call_result_failed"
            payload["message"] = "Codex real-tool call/result canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "tool_call_result_db_ingest_failed"
            payload["message"] = "Codex real-tool call/result evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/tool_call_result.json", payload)
        return payload

    def live_token_streaming(self, package: EvidencePackage) -> dict[str, Any]:
        binary, binary_error = self._require_binary(package, "live_token_streaming")
        if binary_error is not None:
            return binary_error

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-live-token-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "skip_static_contract": True,
                "run_managed_live_send": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        credentials_gap = _codex_canary_credentials_gap(canary_artifact, ("managed_live_send",))
        if credentials_gap:
            payload = {
                "status": STATUS_UNSUPPORTED_GAP,
                "scenario": "live_token_streaming",
                "failure_code": "codex_managed_bridge_credentials_missing",
                "message": "Codex live_token_streaming requires Runtime Host credentials.",
                "missing": credentials_gap,
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "source_artifact_kind": canary_artifact.get("artifact_kind"),
                "synthetic": False,
                "operation_evidence": {
                    "send_input": {
                        "status": STATUS_UNSUPPORTED_GAP,
                        "level": "live_token_required",
                        "canary": "managed_live_send",
                        "failure_code": "codex_managed_bridge_credentials_missing",
                    },
                    "live_token_behavior": {
                        "status": STATUS_UNSUPPORTED_GAP,
                        "level": "live_token_required",
                        "canary": "managed_live_send",
                        "failure_code": "codex_managed_bridge_credentials_missing",
                    },
                },
            }
            package.write_json("assertions/live_token_streaming.json", payload)
            return payload

        operation_evidence = codex_live_token_streaming_operation_evidence(canary_artifact)
        raw_events = codex_live_token_streaming_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        live_status = str((operation_evidence.get("live_token_behavior") or {}).get("status") or STATUS_FAIL)
        send_status = str((operation_evidence.get("send_input") or {}).get("status") or STATUS_FAIL)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        passed = all(
            (
                verdict == "green",
                live_status == STATUS_PASS,
                send_status == STATUS_PASS,
                db_status == STATUS_PASS,
            )
        )
        payload = {
            **projection,
            "status": STATUS_PASS if passed else STATUS_FAIL,
            "scenario": "live_token_streaming",
            "provider_version": canary_artifact.get("codex_version") or canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if verdict != "green" or live_status != STATUS_PASS or send_status != STATUS_PASS:
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_live_token_streaming_failed"
            payload["message"] = "Codex managed live-send canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or "live_token_streaming_db_ingest_failed"
            payload["message"] = "Codex live-token evidence did not pass Longhouse DB ingest assertions."
        package.write_json("assertions/live_token_streaming.json", payload)
        return payload

    def managed_session_e2e(self, package: EvidencePackage) -> dict[str, Any]:
        if not self.config.real_managed_session_e2e:
            payload = self._unsupported_payload(
                "managed_session_e2e",
                "managed_session_e2e_not_migrated",
                "No real no-token managed-session e2e adapter is implemented for this provider yet.",
            )
            package.write_json("assertions/managed_session_e2e.json", payload)
            return payload
        return self._run_codex_managed_session_canary_projection(
            package,
            scenario="managed_session_e2e",
            assertion_name="managed_session_e2e",
        )

    def resume_reattach(self, package: EvidencePackage) -> dict[str, Any]:
        return self._run_codex_managed_session_canary_projection(
            package,
            scenario="resume_reattach",
            assertion_name="resume_reattach",
            require_operation="reattach",
        )

    def cold_resume(self, package: EvidencePackage) -> dict[str, Any]:
        payload = self._run_codex_managed_session_canary_projection(
            package,
            scenario="helm_cold_resume_native",
            assertion_name="helm_cold_resume_native",
            require_operation="reattach",
        )
        if payload.get("status") == STATUS_UNSUPPORTED_GAP and payload.get("failure_code") == "codex_managed_bridge_credentials_missing":
            payload["failure_code"] = "codex_cold_resume_canary_missing"
            payload["message"] = "Codex cold Resume needs a managed-live canary with Runtime Host credentials."
            package.write_json("assertions/helm_cold_resume_native.json", payload)
        return payload

    def _run_codex_interrupt_dispatch_proof(
        self,
        package: EvidencePackage,
        *,
        credentials_gap: list[str],
        canary_artifact_path: Path,
        canary_evidence_root: Path,
        source_artifact_kind: object,
    ) -> dict[str, Any]:
        os.environ.setdefault("TESTING", "1")
        os.environ.setdefault("DATABASE_URL", f"sqlite:///{package.path('longhouse', 'settings-bootstrap.sqlite')}")

        from zerg.database import initialize_database
        from zerg.database import make_engine
        from zerg.database import make_sessionmaker
        from zerg.models.agents import AgentSession
        from zerg.services import managed_local_control as control
        from zerg.session_execution_home import ManagedSessionTransport

        db_path = package.path("longhouse", "codex-interrupt-dispatch.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(f"sqlite:///{db_path}")
        initialize_database(engine)
        session_factory = make_sessionmaker(engine)
        now = datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
        calls: list[dict[str, Any]] = []
        codex_transport = ManagedSessionTransport.CODEX_APP_SERVER.value

        async def fake_dispatch(**kwargs: Any) -> SimpleNamespace:
            calls.append(
                {
                    "owner_id": kwargs.get("owner_id"),
                    "timeout_secs": kwargs.get("timeout_secs"),
                    "command_type": kwargs.get("command_type"),
                    "payload": kwargs.get("payload"),
                    "request_id": kwargs.get("request_id"),
                    "run_id": kwargs.get("run_id"),
                    "provider": getattr(kwargs.get("session"), "provider", None),
                    "managed_transport": _project_managed_transport(kwargs.get("db"), kwargs.get("session")),
                }
            )
            return SimpleNamespace(
                ok=True,
                transport="engine_channel",
                data={"exit_code": 0, "stdout": "interrupted", "stderr": ""},
                error=None,
            )

        original_dispatch = control.dispatch_managed_control_command
        original_transport_error = control._managed_control_transport_error
        control.dispatch_managed_control_command = fake_dispatch
        control._managed_control_transport_error = lambda *_args, **_kwargs: None
        try:
            with session_factory() as db:
                session = AgentSession(
                    provider="codex",
                    environment="test",
                    project="universal-agent-harness",
                    device_id="universal-harness",
                    cwd=str(package.path("workspace")),
                    started_at=now - timedelta(minutes=5),
                    last_activity_at=now,
                    user_messages=1,
                    assistant_messages=1,
                )
                db.add(session)
                db.flush()
                contract = contract_for_provider("codex")
                if contract is not None:
                    _seed_managed_kernel_rows(db, session, control_plane=contract.control_plane)
                result = asyncio.run(
                    control.interrupt_managed_local_session(
                        db=db,
                        owner_id=1,
                        session=session,
                        request_id="universal-codex-interrupt",
                    )
                )
        finally:
            control.dispatch_managed_control_command = original_dispatch
            control._managed_control_transport_error = original_transport_error

        request = calls[0] if calls else {}
        assertions = {
            "command_dispatched": bool(calls),
            "command_type_matches": request.get("command_type") == "session.interrupt",
            "payload_empty": request.get("payload") == {},
            "provider_is_codex": request.get("provider") == "codex",
            "transport_is_codex_app_server": request.get("managed_transport") == codex_transport,
            "result_ok": result.ok is True,
            "exit_code_zero": result.exit_code == 0,
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/codex-interrupt-dispatch.json",
            {
                "db_path": str(db_path),
                "credentials_gap": credentials_gap,
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "calls": calls,
                "result": {
                    "ok": result.ok,
                    "exit_code": result.exit_code,
                    "error": result.error,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
                "assertions": assertions,
            },
        )
        operations = {
            "interrupt": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "codex_managed_local_interrupt_dispatch",
                "failure_code": None if passed else "codex_interrupt_dispatch_failed",
                "source": "zerg.services.managed_local_control.interrupt_managed_local_session",
            },
            "live_interrupt_canary": {
                "status": STATUS_BLOCKED,
                "level": "live_token_required",
                "canary": "managed_live_interrupt",
                "failure_code": "codex_managed_bridge_credentials_missing",
            },
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "system",
                    "role": "system",
                    "text": "Codex managed-local interrupt dispatch command completed.",
                    "provider_session_id": "codex-interrupt-transport-session",
                    "source_canary": "codex_managed_local_interrupt_dispatch",
                    "evidence_origin": "managed_local_control_transport_proof",
                },
            ),
            operations=operations,
            provider_session_id="codex-interrupt-transport-session",
        )
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "interrupt_cancel",
                "assertions": assertions,
                "raw_interrupt_dispatch_path": str(raw_path),
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "source_artifact_kind": source_artifact_kind,
                "missing_live_credentials": credentials_gap,
                "proof_scope": "codex_managed_local_interrupt_dispatch",
                "synthetic": False,
                "operation_evidence": operations,
                "next": "Promote with managed-live Codex interrupt canary when Runtime Host credentials are present.",
            }
        )
        if not passed:
            payload["failure_code"] = "codex_interrupt_dispatch_failed"
            payload["message"] = "Codex interrupt dispatch proof did not pass."
        package.write_json("assertions/interrupt_cancel.json", payload)
        return payload

    def _run_codex_managed_session_canary_projection(
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
                "message": f"codex binary was not found for {scenario}",
                "binary_source": source,
            }
            package.write_json(f"assertions/{assertion_name}.json", payload)
            return payload

        from zerg.qa.codex_provider_release_canary import run_codex_provider_release_canary

        canary_evidence_root = package.path("raw", "codex-release-canary-evidence")
        canary_artifact_path = package.path("raw", "codex-provider-release-canary.json")
        canary_artifact = run_codex_provider_release_canary(
            {
                "codex_bin": str(binary),
                "artifact": canary_artifact_path,
                "evidence_root": canary_evidence_root,
                "repo_root": default_repo_root(),
                "source_review_status": "pass",
                "run_managed_tui_attach": True,
            }
        )
        if not canary_artifact_path.is_file():
            package.write_json("raw/codex-provider-release-canary.json", canary_artifact)
        package.write_json("raw/codex-provider-release-canary-inline.json", canary_artifact)
        operation_evidence = self._operation_evidence_map(canary_artifact.get("operation_evidence"))
        raw_events = codex_provider_release_raw_events(canary_artifact)
        provider_session_id = _first_codex_thread_id(canary_artifact) or self._session_id(package)
        projection, operation_evidence, db_ingest = self._project_ingest_and_merge(
            package,
            operation_evidence=operation_evidence,
            raw_events=raw_events,
            provider_session_id=provider_session_id,
        )

        verdict = str(canary_artifact.get("verdict") or "red")
        credentials_gap = _codex_managed_bridge_credentials_gap(canary_artifact)
        db_status = str(db_ingest.get("status") or STATUS_FAIL)
        payload = {
            **projection,
            "status": STATUS_PASS if verdict == "green" and db_status == STATUS_PASS else STATUS_FAIL,
            "scenario": scenario,
            "provider_version": canary_artifact.get("provider_version"),
            "codex_canary_artifact_path": str(canary_artifact_path),
            "codex_canary_evidence_root": str(canary_evidence_root),
            "codex_canary_verdict": verdict,
            "source_artifact_kind": canary_artifact.get("artifact_kind"),
            "synthetic": False,
            "operation_evidence": operation_evidence,
            "longhouse_ingest": self._longhouse_ingest_block(db_ingest),
        }
        if credentials_gap:
            if scenario == "resume_reattach":
                return self._run_codex_resume_attach_command_proof(
                    package,
                    credentials_gap=credentials_gap,
                    canary_artifact_path=canary_artifact_path,
                    canary_evidence_root=canary_evidence_root,
                    source_artifact_kind=canary_artifact.get("artifact_kind"),
                )
            payload["status"] = STATUS_UNSUPPORTED_GAP
            payload["failure_code"] = "codex_managed_bridge_credentials_missing"
            payload["message"] = f"Codex {scenario} requires Runtime Host credentials."
            payload["missing"] = credentials_gap
        elif verdict != "green":
            payload["failure_code"] = canary_artifact.get("failure_code") or "codex_provider_release_canary_failed"
            payload["message"] = "Codex provider release canary did not pass."
        elif db_status != STATUS_PASS:
            payload["failure_code"] = db_ingest.get("failure_code") or f"{scenario}_db_ingest_failed"
            payload["message"] = "Codex canary evidence did not pass Longhouse DB ingest assertions."
        if require_operation and not credentials_gap and verdict == "green" and db_status == STATUS_PASS:
            operation_status = str((operation_evidence.get(require_operation) or {}).get("status") or STATUS_FAIL)
            if operation_status != STATUS_PASS:
                payload["status"] = STATUS_FAIL
                payload["failure_code"] = f"codex_{require_operation}_evidence_missing"
                payload["message"] = f"Codex canary did not produce passing {require_operation} evidence."
        package.write_json(f"assertions/{assertion_name}.json", payload)
        return payload

    def _run_codex_resume_attach_command_proof(
        self,
        package: EvidencePackage,
        *,
        credentials_gap: list[str],
        canary_artifact_path: Path,
        canary_evidence_root: Path,
        source_artifact_kind: object,
    ) -> dict[str, Any]:
        from zerg.services.managed_local_transport import build_managed_local_attach_command
        from zerg.session_execution_home import ManagedSessionTransport

        longhouse_session_id = "33333333-3333-4333-8333-333333333333"
        session = SimpleNamespace(
            id=longhouse_session_id,
            managed_transport=ManagedSessionTransport.CODEX_APP_SERVER.value,
        )
        command = build_managed_local_attach_command(session=session)
        assertions = {
            "command_built": command is not None,
            "uses_engine_bridge_attach": "codex-bridge attach" in str(command or ""),
            "uses_longhouse_session_id": f"--session-id {longhouse_session_id}" in str(command or ""),
            "requires_longhouse_engine": "command -v longhouse-engine" in str(command or ""),
            "requires_codex": "command -v codex" in str(command or ""),
            "execs_engine": 'exec "$engine" codex-bridge attach' in str(command or ""),
            "uses_zsh_shell": str(command or "").startswith("zsh -lc "),
        }
        passed = all(assertions.values())
        raw_path = package.write_json(
            "raw/codex-reattach-command.json",
            {
                "command": command,
                "longhouse_session_id": longhouse_session_id,
                "credentials_gap": credentials_gap,
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "assertions": assertions,
            },
        )
        operations = {
            "reattach": {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "level": "hermetic",
                "canary": "codex_managed_local_attach_command_shape",
                "failure_code": None if passed else "codex_reattach_command_shape_failed",
                "source": "zerg.services.managed_local_transport.build_managed_local_attach_command",
            },
            "live_reattach_canary": {
                "status": STATUS_BLOCKED,
                "level": "live_no_token",
                "canary": "managed_tui_attach",
                "failure_code": "codex_managed_bridge_credentials_missing",
            },
        }
        payload = self._write_session_projection(
            package,
            raw_events=(
                {
                    "type": "system",
                    "role": "system",
                    "text": "Codex managed-local reattach command shape was built.",
                    "provider_session_id": longhouse_session_id,
                    "source_canary": "codex_managed_local_attach_command_shape",
                    "evidence_origin": "managed_local_transport_command_shape",
                },
            ),
            operations=operations,
            provider_session_id=longhouse_session_id,
        )
        payload.update(
            {
                "status": STATUS_PASS if passed else STATUS_FAIL,
                "scenario": "resume_reattach",
                "assertions": assertions,
                "raw_reattach_command_path": str(raw_path),
                "codex_canary_artifact_path": str(canary_artifact_path),
                "codex_canary_evidence_root": str(canary_evidence_root),
                "source_artifact_kind": source_artifact_kind,
                "missing_live_credentials": credentials_gap,
                "proof_scope": "codex_managed_local_attach_command_shape",
                "synthetic": False,
                "operation_evidence": operations,
                "next": "Promote with managed Codex process restart and same-thread reattach proof.",
            }
        )
        if not passed:
            payload["failure_code"] = "codex_reattach_command_shape_failed"
            payload["message"] = "Codex reattach command shape proof did not pass."
        package.write_json("assertions/resume_reattach.json", payload)
        return payload
