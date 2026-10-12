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
import uuid
from typing import Any

from zerg.qa.claude_live_session_support import RuntimeHostCoordinationAuthorityUnavailable
from zerg.qa.claude_live_session_support import await_assistant_marker
from zerg.qa.claude_live_session_support import claude_qualification
from zerg.qa.claude_live_session_support import find_tool_invocation
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_coordination_oracles import awareness_create_assertions
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
    producer_revision=5,
    scenario_id=_SCENARIO_ID,
    scenario_revision=6,
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
        "cleanup_receipt",
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_coordination_awareness_create.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="awareness_create_assertions",
    executable_module="zerg.qa.claude_coordination_awareness_create",
)

_ARTIFACT_KIND = "claude_coordination_awareness_create_result"


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    marker = f"LONGHOUSE_COORD_CREATE_{uuid.uuid4().hex[:12]}"
    probe_repo = f"longhouse-coordination-awareness-probe-{uuid.uuid4().hex[:12]}"
    prompt = (
        f'Call your peers tool now with repo="{probe_repo}" and active_only=false. '
        f"After the tool call returns, reply with exactly {marker} and nothing else."
    )

    with claude_qualification(
        run,
        args,
        registration=REGISTRATION,
        prefix="lhx-claude-coord-create-",
        name="Longhouse coordination-awareness-create qualification",
        coordination_authority=True,
    ) as live:
        session, session_id, provider_session_id, home = live.session, live.session_id, live.provider_session_id, live.home

        # Coordination-channel messages are attributed, untrusted peer input;
        # asking Claude to obey one as a command correctly triggers its safety
        # posture. Enter this behavioral probe as the Helm user instead.
        session.submit_line(prompt)
        await_assistant_marker(
            session_id=session_id,
            marker=marker,
            timeout=args.response_timeout_secs,
            home=home,
            provider_session_id=provider_session_id,
        )

        invocation = find_tool_invocation(provider_session_id or session_id, "peers", home=home)
        write_json(root / "tool-invocation-evidence.json", invocation or {"found": False})

    tool_called_successfully = (
        invocation is not None and invocation.get("tool_result_line") is not None and invocation.get("is_error") is not True
    )
    run.observation = {
        "session_id": session_id,
        "marker": marker,
        "probe_repo": probe_repo,
        "coordination_instructions_model_visible": tool_called_successfully,
        "tool_invocation": invocation,
    }
    run.assertions = awareness_create_assertions(run.observation)


def run_awareness_create_scenario(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--launch-timeout-secs", type=float, default=60.0)
    parser.add_argument("--response-timeout-secs", type=float, default=120.0)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind=_ARTIFACT_KIND,
    # A launch without coordination authority, or one that never came up, is
    # typed so the factory reports infrastructure rather than a model finding.
    failure_code=lambda exc: (
        "runtime_host_coordination_authority_unavailable"
        if isinstance(exc, RuntimeHostCoordinationAuthorityUnavailable)
        else "claude_coordination_awareness_create_failed"
    ),
    needs=("evidence_root", "repo_root", "engine", "provider_bin"),
    binaries=(("engine", "longhouse_engine_missing"), ("provider_bin", "claude_binary_missing")),
    runtime_host=True,
    variants=(_EXECUTION_VARIANT,),
    provider_bin_aliases=("--claude-bin",),
    model_env="ANTHROPIC_MODEL",
    add_arguments=_add_arguments,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_awareness_create_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())
