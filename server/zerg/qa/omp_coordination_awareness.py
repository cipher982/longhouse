#!/usr/bin/env python3
"""Producer for omp/coordination.awareness.create.

Proves ``coordination_instructions_model_visible`` for OMP Helm the way the
Claude and Codex producers do: launch a real managed OMP Helm session
(``longhouse omp``) and have the model call the Longhouse ``peers`` tool,
rather than asking it to describe what it can see.

OMP mounts extension tools as ``xd://`` devices by default (setting
``tools.xdevDocs``: built-in tools stay top-level, extension and MCP tools are
fetched on demand), so the model invokes ``peers`` as ``write xd://peers`` after
optionally reading ``xd://peers`` for its schema. With devices disabled the
same tool is a top-level ``peers`` call. Either, with a non-error tool result,
is direct evidence the Longhouse extension's tool is visible to and callable by
the model. Reading the device's docs alone is not an invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.live_session_toolkit import new_qualification_isolation_root
from zerg.qa.live_session_toolkit import redact_state_for_evidence
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.omp_helm_lifecycle import _exact_session_retirement
from zerg.qa.omp_helm_lifecycle import _launch_argv
from zerg.qa.omp_helm_lifecycle import _native_rows
from zerg.qa.omp_helm_lifecycle import _process_record
from zerg.qa.omp_helm_lifecycle import _remove_isolation_after_source_retention
from zerg.qa.omp_helm_lifecycle import _wait_cleanup_receipt
from zerg.qa.omp_helm_lifecycle import _wait_native_marker
from zerg.qa.omp_helm_lifecycle import _wait_state
from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "omp_coordination_awareness_create"
ASSERTION_ID = "coordination_instructions_model_visible"
_EXECUTION_VARIANT = execution_variant_key(provider="omp", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)
_ARTIFACT_KIND = "omp_coordination_awareness_create_result"

REGISTRATION = ProducerRegistration(
    producer_id="omp.coordination_awareness_create.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=("omp",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=("coordination_tool_invoked",),
    acquisition_methods=("staged_release",),
    credential_binding_ids=("omp_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "session_launch_receipt",
        "tool_invocation_evidence",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "canary_session_hidden", "isolation_removed"),
    implementation="server/zerg/qa/omp_coordination_awareness.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="awareness_create_assertions",
    executable_module="zerg.qa.omp_coordination_awareness",
    required_executables=("longhouse", "longhouse-engine"),
)


def _tool_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(part.get("text") or "") for part in content if isinstance(part, Mapping))
    return ""


def _is_peers_invocation(call: Mapping[str, Any]) -> bool:
    name = str(call.get("name") or "")
    if name == "peers":
        return True
    arguments = call.get("arguments")
    path = str(arguments.get("path") or "") if isinstance(arguments, Mapping) else ""
    return name == "write" and path.split("?", 1)[0].rstrip("/") == "xd://peers"


def peers_invocation_evidence(rows: list[Mapping[str, Any]]) -> dict[str, Any] | None:
    """A ``peers`` invocation in an OMP session and its tool result.

    A result counts only when OMP reports no error and the body is not a
    Longhouse error object (the extension answers a refused call with
    ``{"error": ...}``). The first successful invocation wins, so a refusal
    followed by a good retry still proves the tool works; with none, the
    first refusal is returned as the evidence.
    """

    calls: dict[str, Mapping[str, Any]] = {}
    first_refusal: dict[str, Any] | None = None
    for row in rows:
        message = row.get("message") if row.get("type") == "message" else None
        if not isinstance(message, Mapping):
            continue
        if message.get("role") == "assistant" and isinstance(message.get("content"), list):
            for part in message["content"]:
                if isinstance(part, Mapping) and part.get("type") == "toolCall" and part.get("id") and _is_peers_invocation(part):
                    calls[str(part["id"])] = part
        elif message.get("role") == "toolResult" and str(message.get("toolCallId") or "") in calls:
            call = calls[str(message["toolCallId"])]
            text = _tool_text(message)
            try:
                body = json.loads(text)
            except ValueError:
                body = None
            refused = isinstance(body, Mapping) and "error" in body
            evidence = {
                "tool_name": call.get("name"),
                "arguments": call.get("arguments"),
                "is_error": message.get("isError") is True or refused,
                "result_excerpt": text[:400],
            }
            if evidence["is_error"] is not True:
                return evidence
            first_refusal = first_refusal or evidence
    return first_refusal


def run_scenario(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.environ.get("LONGHOUSE_OMP_LIVE") not in {"1", "true", "yes", "on"}:
        raise RuntimeError("OMP coordination qualification requires explicit LONGHOUSE_OMP_LIVE opt-in")
    if not args.api_url or not args.agents_token:
        raise RuntimeError("OMP coordination qualification requires Runtime Host URL and token")
    require_disposable_runtime(args.api_url)
    isolation = new_qualification_isolation_root("omp-coordination")
    provider_home = isolation / "home"
    longhouse_home = provider_home / ".longhouse"
    workspace = isolation / "workspace"
    try:
        provider_home.mkdir(mode=0o700, parents=True)
        workspace.mkdir(mode=0o700, parents=True)
    except BaseException:
        shutil.rmtree(isolation, ignore_errors=True)
        raise
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
    marker = f"LONGHOUSE_OMP_COORD_{uuid.uuid4().hex[:12]}"
    probe_repo = f"longhouse-coordination-awareness-probe-{uuid.uuid4().hex[:12]}"
    prompt = (
        f'Call your Longhouse peers tool now with repo="{probe_repo}" and active_only=false. '
        f"After the tool call returns, reply with exactly {marker} and nothing else."
    )
    session: ProviderPtySession | None = None
    shipper = None
    state: dict[str, Any] = {}
    owner_records: list[dict[str, Any]] = []
    invocation: dict[str, Any] | None = None
    failure: str | None = None
    try:
        shipper = start_transcript_shipper(
            "omp", args, home=provider_home, environment=env, evidence_root=root / "shipper", longhouse_home=longhouse_home
        )
        lifecycle.write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        session = ProviderPtySession.start(
            argv=_launch_argv(args, workspace=workspace, prompt=prompt),
            cwd=workspace,
            env=env,
            terminal_path=root / "omp-terminal.raw",
            thread_name="omp-coordination-terminal-drain",
        )
        state = _wait_state(longhouse_home)
        for label, pid_key, start_key in (
            ("launcher", "launcher_pid", "launcher_process_start_time"),
            ("provider", "provider_pid", "provider_process_start_time"),
        ):
            owner_records.append(_process_record(state.get(pid_key), state.get(start_key), label, owner="initial"))
        lifecycle.write_json(root / "session-launch-receipt.json", redact_state_for_evidence(state))
        session_file = Path(str(state["session_file"]))
        _wait_native_marker(session_file, marker, timeout=float(args.response_timeout_secs))
        invocation = peers_invocation_evidence(_native_rows(session_file))
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        session_id = str(state.get("session_id") or "")
        if session_id:
            lifecycle._terminate_live_qualification_session(str(args.api_url), str(args.agents_token), session_id)
        if session is not None:
            if session.alive():
                try:
                    os.killpg(session.process.pid, signal.SIGTERM)
                    session.process.wait(timeout=10)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(session.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            session.close()
        cleanup = (
            _wait_cleanup_receipt(owner_records)
            if owner_records
            else {"status": "fail", "provider_process_dead": False, "process_group_dead": False}
        )
        if session_id:
            retirement = retire_qualification_session(str(args.api_url), str(args.agents_token), session_id, provider="omp")
            cleanup["canary_session_hidden"] = _exact_session_retirement(retirement, session_id)
        else:
            cleanup["canary_session_hidden"] = False
        shipper_stop: dict[str, Any] = {}
        if shipper is not None:
            try:
                shipper_stop = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup evidence must remain visible
                shipper_stop = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        lifecycle.write_json(root / "transcript-shipper-receipt.json", shipper_stop or {"status": "fail", "stopped": False})
        processes_stopped = cleanup.get("provider_process_dead") is True and cleanup.get("process_group_dead") is True
        # This scenario retains no provider source, so the isolation may go
        # once the owners are dead and the session is retired.
        cleanup["isolation_removed"] = _remove_isolation_after_source_retention(
            isolation,
            source_retention_verified=True,
            runtime_cleanup_verified=processes_stopped and cleanup["canary_session_hidden"] is True,
            cleanup=cleanup,
        )
        required = ("provider_process_dead", "process_group_dead", "canary_session_hidden", "isolation_removed")
        cleanup["status"] = "pass" if all(cleanup.get(key) is True for key in required) else "fail"
        cleanup = redact_state_for_evidence(cleanup)
        lifecycle.write_json(root / "cleanup-receipt.json", cleanup)
    lifecycle.write_json(root / "tool-invocation-evidence.json", invocation or {"found": False})
    visible = invocation is not None and invocation.get("is_error") is not True
    observation = {
        "session_id": str(state.get("session_id") or ""),
        "marker": marker,
        "probe_repo": probe_repo,
        "coordination_instructions_model_visible": visible,
        "tool_invocation": invocation,
        "cleanup": cleanup,
    }
    assertions = awareness_create_assertions(observation)
    result = {
        "schema_version": 1,
        "artifact_kind": _ARTIFACT_KIND,
        "producer": REGISTRATION.to_dict(),
        "provider": "omp",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        # Status follows the assertion map, as the factory validator requires;
        # cleanup is judged from the cleanup receipt (REGISTRATION.required_cleanup).
        "status": "pass" if failure is None and assertions[ASSERTION_ID] else "fail",
        "observation": observation,
        "assertions": assertions,
        **({"failure_code": "omp_coordination_awareness_failed", "error": failure} if failure else {}),
        "artifact_manifest": artifact_manifest(root),
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=(_EXECUTION_VARIANT,))
    parser.add_argument("--model", default=os.environ.get("LONGHOUSE_OMP_QUALIFICATION_MODEL", ""))
    parser.add_argument("--api-url", default=os.environ.get("LONGHOUSE_RUNTIME_API_URL"))
    parser.add_argument("--agents-token", default=os.environ.get("LONGHOUSE_RUNTIME_AGENTS_TOKEN"))
    parser.add_argument("--response-timeout-secs", type=float, default=180.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    if args.longhouse_cli is None:
        args.longhouse_cli = Path(os.environ.get("LONGHOUSE_CLI_BIN") or "longhouse")
    try:
        result = run_scenario(args)
    except Exception as exc:  # noqa: BLE001 - a precondition failure still writes a typed result
        result = {
            "schema_version": 1,
            "artifact_kind": _ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "omp",
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "omp_coordination_awareness_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "observation": {},
            "assertions": awareness_create_assertions({}),
        }
        if args.evidence_root is not None:
            args.evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            result["artifact_manifest"] = artifact_manifest(args.evidence_root)
            lifecycle.write_json(args.evidence_root / "result.json", result)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
