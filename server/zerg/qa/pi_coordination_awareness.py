#!/usr/bin/env python3
"""Producer for pi/coordination.awareness.create.

Proves ``coordination_instructions_model_visible`` for Pi Helm the way the
OMP producer does: launch a real managed Pi Helm session (``longhouse pi``)
and have the model call the Longhouse ``peers`` tool, rather than asking it to
describe what it can see. A ``peers`` invocation with a non-error result is
direct evidence that the Longhouse extension's tool is visible to and
callable by the model. Pi's session file shares OMP's format, so the
invocation evidence and the retirement record come from the OMP producer.
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
from pathlib import Path
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.live_session_toolkit import new_qualification_isolation_root
from zerg.qa.live_session_toolkit import redact_state_for_evidence
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.omp_coordination_awareness import peers_invocation_evidence
from zerg.qa.omp_coordination_awareness import session_retirement_cleanup
from zerg.qa.omp_helm_lifecycle import _remove_isolation_after_source_retention
from zerg.qa.openrouter_routing import prepare_pi_openrouter_routing
from zerg.qa.pi_family_turn_oracle import read_session_entries
from zerg.qa.pi_helm_lifecycle import _cleanup_receipt
from zerg.qa.pi_helm_lifecycle import _process_record
from zerg.qa.pi_helm_lifecycle import _wait
from zerg.qa.pi_helm_lifecycle import _wait_native_marker
from zerg.qa.pi_helm_lifecycle import _wait_state
from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_factory_invocation import add_factory_provider_arguments
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.pty_session import ProviderPtySession
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

SCENARIO_ID = "pi_coordination_awareness_create"
ASSERTION_ID = "coordination_instructions_model_visible"
_EXECUTION_VARIANT = execution_variant_key(provider="pi", assertion_id=ASSERTION_ID, scenario_id=SCENARIO_ID, variant=None)
_ARTIFACT_KIND = "pi_coordination_awareness_create_result"

REGISTRATION = ProducerRegistration(
    producer_id="pi.coordination_awareness_create.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, None),),
    providers=("pi",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=("coordination_tool_invoked",),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("pi_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "session_launch_receipt",
        "tool_invocation_evidence",
        "cleanup_receipt",
    ),
    required_cleanup=("provider_process_dead", "process_group_dead", "canary_session_hidden", "isolation_removed"),
    implementation="server/zerg/qa/pi_coordination_awareness.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="awareness_create_assertions",
    executable_module="zerg.qa.pi_coordination_awareness",
    required_executables=("longhouse", "longhouse-engine"),
)


def run_scenario(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.environ.get("LONGHOUSE_PI_LIVE") not in {"1", "true", "yes", "on"}:
        raise RuntimeError("Pi coordination qualification requires explicit LONGHOUSE_PI_LIVE opt-in")
    if not args.api_url or not args.agents_token:
        raise RuntimeError("Pi coordination qualification requires Runtime Host URL and token")
    if not str(args.model or "").strip():
        raise RuntimeError("Pi coordination qualification requires an explicit --model (LONGHOUSE_PI_QUALIFICATION_MODEL)")
    require_disposable_runtime(args.api_url)
    isolation = new_qualification_isolation_root("pi-coordination")
    provider_home = isolation / "provider-home"
    home = isolation / "longhouse"
    workspace = isolation / "workspace"
    try:
        workspace.mkdir(mode=0o700, parents=True)
        (provider_home / ".pi" / "agent" / "sessions").mkdir(mode=0o700, parents=True)
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(provider_home),
                "LONGHOUSE_HOME": str(home),
                "PI_CODING_AGENT_DIR": str(provider_home / ".pi"),
                "LONGHOUSE_ENGINE_BIN": str(args.engine),
                "LONGHOUSE_PI_BIN": str(args.provider_bin),
                "LONGHOUSE_PI_HELM_URL": str(args.api_url),
                "LONGHOUSE_PI_HELM_TOKEN": str(args.agents_token),
                "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
                "LONGHOUSE_LAUNCH_ACTOR": "automation",
                "LONGHOUSE_LAUNCH_SURFACE": "qa",
            }
        )
        lifecycle.write_json(
            root / "openrouter-routing-receipt.json",
            prepare_pi_openrouter_routing(provider_home / ".pi", args.model),
        )
    except BaseException:
        shutil.rmtree(isolation, ignore_errors=True)
        raise
    marker = f"LONGHOUSE_PI_COORD_{uuid.uuid4().hex[:12]}"
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
            "pi", args, home=provider_home, environment=env, evidence_root=root / "shipper", longhouse_home=home
        )
        lifecycle.write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        session = ProviderPtySession.start(
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
                prompt,
            ],
            cwd=workspace,
            env=env,
            terminal_path=root / "pi-terminal.raw",
            thread_name="pi-coordination-terminal-drain",
        )
        state = _wait_state(home, predicate=lambda item: item.get("ready") is True, timeout=45)
        for label in ("launcher", "provider"):
            owner_records.append(_process_record(state.get(f"{label}_pid"), state.get(f"{label}_process_start_time"), label))
        lifecycle.write_json(root / "session-launch-receipt.json", redact_state_for_evidence(state))
        session_file = Path(str(state.get("session_file") or ""))
        if not session_file.is_file():
            session_dir = Path(str(state.get("session_dir") or ""))
            session_file = _wait(
                lambda: next(
                    (path for path in session_dir.rglob("*.jsonl") if path.is_file() and not path.is_symlink()),
                    None,
                ),
                timeout=45,
                description="Pi native session file",
            )
        _wait_native_marker(session_file, marker, timeout=float(args.response_timeout_secs))
        invocation = peers_invocation_evidence(read_session_entries(session_file))
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
            _cleanup_receipt(owner_records)
            if owner_records
            else {"status": "fail", "provider_process_dead": False, "process_group_dead": False}
        )
        if session_id:
            retirement = retire_qualification_session(str(args.api_url), str(args.agents_token), session_id, provider="pi")
            cleanup.update(session_retirement_cleanup(retirement, session_id))
        else:
            cleanup.update(session_retirement_cleanup(None, ""))
        shipper_stop: dict[str, Any] = {}
        if shipper is not None:
            try:
                shipper_stop = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup evidence must remain visible
                shipper_stop = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        lifecycle.write_json(root / "transcript-shipper-receipt.json", shipper_stop or {"status": "fail", "stopped": False})
        processes_stopped = cleanup.get("provider_process_dead") is True and cleanup.get("process_group_dead") is True
        # No provider source is retained here, so the isolation may go once the
        # owners are dead and the session is retired.
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
        "provider": "pi",
        "variant": None,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_token",
        "generated_at": now(),
        "status": "pass" if failure is None and assertions[ASSERTION_ID] else "fail",
        "observation": observation,
        "assertions": assertions,
        **({"failure_code": "pi_coordination_awareness_failed", "error": failure} if failure else {}),
        "artifact_manifest": artifact_manifest(root),
    }
    lifecycle.write_json(root / "result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_factory_provider_arguments(parser, variants=(_EXECUTION_VARIANT,))
    # No default: the factory names the qualification model, and an Anthropic
    # model must never be routed through OpenRouter.
    parser.add_argument("--model", default=os.environ.get("LONGHOUSE_PI_QUALIFICATION_MODEL", ""))
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
            "provider": "pi",
            "variant": None,
            "scenario_id": SCENARIO_ID,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now(),
            "status": "fail",
            "failure_code": "pi_coordination_awareness_failed",
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
