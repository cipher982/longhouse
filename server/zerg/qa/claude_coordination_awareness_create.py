#!/usr/bin/env python3
"""Continuous producer for claude/coordination.awareness.create.

Proves ``coordination_instructions_model_visible`` behaviorally: launches a
real managed Claude Helm session and has the model actually call the
Longhouse coordination MCP tool (``peers``), rather than merely asking it to
describe what it can see. ``longhouse claude`` wires that server under the
fixed name ``longhouse-coordination`` once a coordination token is issued for
the session (``engine/src/longhouse.rs::write_claude_mcp_config``,
``CLAUDE_COORDINATION_SERVER_NAME`` in
``zerg/services/claude_channel_bridge.py``), so a successful, non-error tool
call is direct evidence the instructions/tool surface is genuinely visible to
the model -- not just present in some config file it never reads.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import await_assistant_marker
from zerg.qa.claude_live_session_support import claude_launch_environment
from zerg.qa.claude_live_session_support import close_session
from zerg.qa.claude_live_session_support import find_tool_invocation
from zerg.qa.claude_live_session_support import isolation_paths
from zerg.qa.claude_live_session_support import launch_claude_session
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import start_machine_and_shipper
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.provider_coordination_oracles import awareness_create_assertions
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import _prepare_claude_profile
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SCENARIO_ID = "claude_coordination_awareness_create"
_ASSERTION_ID = "coordination_instructions_model_visible"
# No `variant:` key is authored for this assertion in
# schemas/managed_providers.yml, so the compiler always passes this generated
# `cell:<provider>:<assertion_id>:<scenario_id>` execution_variant as
# --variant -- see resume_assurance.execution_variant_key and
# codex_turn_boundary_native.py / codex_coordination_native.py, which
# established this exact pattern for the same shape of cell.
_EXECUTION_VARIANT = execution_variant_key(
    provider="claude",
    assertion_id=_ASSERTION_ID,
    scenario_id=_SCENARIO_ID,
    variant=None,
)

REGISTRATION = ProducerRegistration(
    producer_id="claude.coordination_awareness_create.v1",
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((_ASSERTION_ID, None),),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=("coordination_mcp_tool_invoked",),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "session_launch_receipt",
        "tool_invocation_evidence",
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_coordination_awareness_create.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="awareness_create_assertions",
    executable_module="zerg.qa.claude_coordination_awareness_create",
)

_ARTIFACT_KIND = "claude_coordination_awareness_create_result"


def run_awareness_create_scenario(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-claude-coord-create-", dir="/tmp"))
    workspace = isolation_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    marker = f"LONGHOUSE_COORD_CREATE_{uuid.uuid4().hex}"
    probe_repo = f"longhouse-coordination-awareness-probe-{uuid.uuid4().hex[:12]}"
    prompt = (
        f'Call your peers tool now with repo="{probe_repo}" and active_only=false. '
        f"After the tool call returns, reply with exactly {marker} and nothing else."
    )

    shipper = None
    session = None
    try:
        shipper, environment = start_machine_and_shipper(args, isolation_root=isolation_root, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        # Claude persists first-run onboarding (theme/API-key/trust) outside
        # the Longhouse channel state. Launching the managed facade against
        # this fresh disposable profile without completing it first leaves
        # the session stuck at the wizard forever -- no channel readiness,
        # no MCP tool surface, indistinguishable from a hang. Every proven
        # Resume producer gets this for free through provider_native_resume's
        # shared launch path; this producer launches Claude directly, so it
        # needs the same priming call explicitly.
        home, longhouse_home = isolation_paths(isolation_root)
        onboarding = _prepare_claude_profile(
            binary=args.claude_bin,
            home=home,
            workspace=workspace,
            environment=environment,
            recording=root / "claude-onboarding.tty",
        )
        write_json(root / "claude-onboarding-receipt.json", onboarding)

        launch_env = claude_launch_environment(
            environment,
            claude_bin=args.claude_bin,
            engine=args.engine,
            model=args.model,
            longhouse_home=longhouse_home,
        )
        session, session_id, provider_session_id = launch_claude_session(
            workspace=workspace,
            project=args.project,
            name="Longhouse coordination-awareness-create qualification",
            env=launch_env,
            terminal_path=root / "terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        write_json(root / "session-launch-receipt.json", {"session_id": session_id, "workspace": str(workspace)})

        # Coordination-channel messages are attributed, untrusted peer input;
        # asking Claude to obey one as a command correctly triggers its safety
        # posture. Enter this behavioral probe as the Helm user instead.
        session.submit_line(prompt)
        await_assistant_marker(
            session_id=session_id,
            marker=marker,
            timeout=args.response_timeout_secs,
            home=Path(environment["HOME"]),
            provider_session_id=provider_session_id,
        )

        invocation = find_tool_invocation(provider_session_id or session_id, "peers", home=Path(environment["HOME"]))
        write_json(root / "tool-invocation-evidence.json", invocation or {"found": False})

        close_receipt = close_session(session)
        session = None
        write_json(root / "session-close-receipt.json", close_receipt)
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None

        tool_called_successfully = (
            invocation is not None and invocation.get("tool_result_line") is not None and invocation.get("is_error") is not True
        )
        observation = {
            "session_id": session_id,
            "marker": marker,
            "probe_repo": probe_repo,
            "coordination_instructions_model_visible": tool_called_successfully,
            "tool_invocation": invocation,
        }
        assertions = awareness_create_assertions(observation)
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": _ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "claude",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now_iso(),
            "status": "pass" if assertions.get("coordination_instructions_model_visible") else "fail",
            "observation": observation,
            "assertions": assertions,
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        if session is not None:
            try:
                close_session(session)
            except Exception:  # noqa: BLE001 - never let cleanup mask the causal error
                pass
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None
        failure = {
            "schema_version": 1,
            "artifact_kind": _ARTIFACT_KIND,
            "producer": REGISTRATION.to_dict(),
            "provider": "claude",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": now_iso(),
            "status": "fail",
            "failure_code": "claude_coordination_awareness_create_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", failure)
        return failure
    finally:
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
        shutil.rmtree(isolation_root, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True, choices=(_EXECUTION_VARIANT,))
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--claude-bin", type=Path)
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"))
    parser.add_argument("--launch-timeout-secs", type=float, default=60.0)
    parser.add_argument("--response-timeout-secs", type=float, default=120.0)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    for required in ("evidence_root", "repo_root", "engine", "claude_bin"):
        if getattr(args, required) is None:
            print(json.dumps({"status": "fail", "failure_code": f"missing_required_argument:--{required.replace('_', '-')}"}))
            return 2
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    if not args.engine.is_file() or not os.access(args.engine, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_engine_missing"}))
        return 2
    if not args.claude_bin.is_file() or not os.access(args.claude_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "claude_binary_missing"}))
        return 2
    result = run_awareness_create_scenario(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
