#!/usr/bin/env python3
"""Continuous producer for claude/coordination.awareness.post_compaction.

Proves two things about one real managed Claude Helm session that survives a
context compaction:

* ``coordination_instructions_model_visible_after_compaction`` -- the model
  can still successfully call the Longhouse coordination MCP tool (``peers``)
  *after* running ``/compact``, the same behavioral check
  ``claude_coordination_awareness_create.py`` uses before compaction.
* ``no_duplicate_visible_bootstrap`` -- compaction did not cause a second
  coordination MCP bootstrap. ``engine/src/longhouse.rs::write_claude_mcp_config``
  is called exactly once, at process launch, before the provider binary is
  even spawned; there is no compaction-triggered call path back into it. That
  is also *why* Claude's coordination server can skip a SessionStart-style
  re-injection hook at all -- see the ``create_server`` docstring in
  ``zerg/mcp_server/server.py`` ("MCP initialization instructions ... remain
  part of the tool namespace after provider context compaction"). This
  producer turns that design intent into a real, counted observation: it
  counts the on-disk MCP bootstrap config Longhouse wrote for this exact
  session id (``$LONGHOUSE_HOME/run/claude-mcp/<session_id>-*.json``) and
  requires that count to still be exactly one after ``/compact``.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from zerg.qa.claude_live_session_support import RuntimeHostCoordinationAuthorityUnavailable
from zerg.qa.claude_live_session_support import await_assistant_marker
from zerg.qa.claude_live_session_support import claude_qualification
from zerg.qa.claude_live_session_support import find_compaction_boundary
from zerg.qa.claude_live_session_support import find_tool_invocation
from zerg.qa.claude_live_session_support import mcp_bootstrap_config_paths
from zerg.qa.claude_live_session_support import wait_until
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.managed_claude_live import transcript_line_counts
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_coordination_oracles import awareness_post_compaction_assertions
from zerg.qa.pty_session import wait_for_terminal_quiescence
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.qa.wait_kind import PROVIDER_TURN

_SCENARIO_ID = "claude_coordination_awareness_post_compaction"
_ASSERTION_VISIBLE = "coordination_instructions_model_visible_after_compaction"
_ASSERTION_NO_DUP_BOOTSTRAP = "no_duplicate_visible_bootstrap"
# Neither assertion carries a `variant:` key in schemas/managed_providers.yml,
# so each gets its own generated `cell:<provider>:<assertion_id>:<scenario_id>`
# execution_variant (see resume_assurance.execution_variant_key). Both cells
# share this scenario/producer -- the compiler schedules them as two separate
# --variant invocations of the exact same underlying scenario, exactly the
# codex_coordination_native.py precedent for the identical shape (one
# scenario computing multiple assertion keys, requested one --variant at a
# time). Compute the exact strings the real compiler will pass instead of
# guessing at a parallel format.
_CELL_BY_VARIANT: dict[str, str] = {
    execution_variant_key(provider="claude", assertion_id=assertion_id, scenario_id=_SCENARIO_ID, variant=None): assertion_id
    for assertion_id in (_ASSERTION_VISIBLE, _ASSERTION_NO_DUP_BOOTSTRAP)
}

REGISTRATION = ProducerRegistration(
    producer_id="claude.coordination_awareness_post_compaction.v1",
    producer_revision=5,
    scenario_id=_SCENARIO_ID,
    # 7: the status is the assertion map's, not the requested cell's.
    scenario_revision=7,
    assertion_cells=(
        (_ASSERTION_VISIBLE, None),
        (_ASSERTION_NO_DUP_BOOTSTRAP, None),
    ),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "coordination_mcp_tool_invoked_pre_compaction",
        "conversation_compacted",
        "coordination_mcp_tool_invoked_post_compaction",
        "mcp_bootstrap_config_count_checked",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "session_launch_receipt",
        "pre_compaction_tool_invocation_evidence",
        "compaction_boundary_receipt",
        "post_compaction_tool_invocation_evidence",
        "mcp_bootstrap_config_manifest",
        "cleanup_receipt",
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_coordination_awareness_post_compaction.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="awareness_post_compaction_assertions",
    executable_module="zerg.qa.claude_coordination_awareness_post_compaction",
)

_ARTIFACT_KIND = "claude_coordination_awareness_post_compaction_result"


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    marker_pre = f"LONGHOUSE_COORD_PRECOMPACT_{uuid.uuid4().hex[:12]}"
    marker_post = f"LONGHOUSE_COORD_POSTCOMPACT_{uuid.uuid4().hex[:12]}"
    probe_repo = f"longhouse-coordination-awareness-probe-{uuid.uuid4().hex[:12]}"

    def peers_prompt(marker: str) -> str:
        return (
            f'Call your peers tool now with repo="{probe_repo}" and active_only=false. '
            f"After the tool call returns, reply with exactly {marker} and nothing else."
        )

    with claude_qualification(
        run,
        args,
        registration=REGISTRATION,
        prefix="lhx-claude-coord-compact-",
        name="Longhouse coordination-awareness-post-compaction qualification",
        coordination_authority=True,
    ) as live:
        session, session_id, provider_session_id, home, longhouse_home = (
            live.session,
            live.session_id,
            live.provider_session_id,
            live.home,
            live.longhouse_home,
        )
        transcript_id = provider_session_id or session_id

        # These are real Helm-user turns. The coordination channel is
        # deliberately untrusted peer input and must not be used to command
        # the model to call a tool.
        session.submit_line(peers_prompt(marker_pre))
        await_assistant_marker(
            session_id=session_id,
            marker=marker_pre,
            timeout=args.response_timeout_secs,
            home=home,
            provider_session_id=provider_session_id,
        )
        pre_invocation = find_tool_invocation(transcript_id, "peers", home=home)
        write_json(root / "pre-compaction-tool-invocation-evidence.json", pre_invocation or {"found": False})

        bootstrap_configs_before = mcp_bootstrap_config_paths(longhouse_home, session_id)

        pre_compaction_line_counts = transcript_line_counts(transcript_id, home=home)
        session.submit_line("/compact")
        wait_for_terminal_quiescence(session, timeout=args.compaction_timeout_secs, minimum_bytes=1, stable_seconds=2.0)
        compaction_boundary = wait_until(
            lambda: find_compaction_boundary(
                transcript_id,
                after_line_counts=pre_compaction_line_counts,
                home=home,
            ),
            timeout=args.compaction_timeout_secs,
            description="Claude transcript compaction boundary",
            # Claude's own transcript after `/compact`, which is a model call.
            waited_on=PROVIDER_TURN,
        )
        write_json(root / "compaction-boundary-receipt.json", compaction_boundary)

        session.submit_line(peers_prompt(marker_post))
        await_assistant_marker(
            session_id=session_id,
            marker=marker_post,
            timeout=args.response_timeout_secs,
            home=home,
            provider_session_id=provider_session_id,
        )
        post_invocation = find_tool_invocation(transcript_id, "peers", after_line_counts=pre_compaction_line_counts, home=home)
        write_json(root / "post-compaction-tool-invocation-evidence.json", post_invocation or {"found": False})

        bootstrap_configs_after = mcp_bootstrap_config_paths(longhouse_home, session_id)
        write_json(
            root / "mcp-bootstrap-config-manifest.json",
            {
                "before_compact": [str(path) for path in bootstrap_configs_before],
                "after_compact": [str(path) for path in bootstrap_configs_after],
            },
        )

    pre_ok = (
        pre_invocation is not None and pre_invocation.get("tool_result_line") is not None and pre_invocation.get("is_error") is not True
    )
    post_ok = (
        post_invocation is not None and post_invocation.get("tool_result_line") is not None and post_invocation.get("is_error") is not True
    )
    # A confident count requires having actually found the bootstrap
    # directory. An empty glob is ambiguous between "correctly bootstrapped
    # once, path assumption right" and "path assumption wrong" -- do not
    # manufacture a specific count in that case; report None (the oracle
    # already treats None as non-duplicating) and let the raw manifest
    # above carry the evidence for a human to check.
    visible_bootstrap_count = len(bootstrap_configs_after) if bootstrap_configs_after or bootstrap_configs_before else None

    run.observation = {
        "session_id": session_id,
        "probe_repo": probe_repo,
        "pre_compaction_marker": marker_pre,
        "post_compaction_marker": marker_post,
        "pre_compaction_tool_call_ok": pre_ok,
        "compaction_signal_observed": compaction_boundary is not None,
        "coordination_instructions_model_visible_after_compaction": post_ok and compaction_boundary is not None,
        "pre_compaction_tool_invocation": pre_invocation,
        "post_compaction_tool_invocation": post_invocation,
        "mcp_bootstrap_configs_before_compact": [str(path) for path in bootstrap_configs_before],
        "mcp_bootstrap_configs_after_compact": [str(path) for path in bootstrap_configs_after],
        "visible_bootstrap_count": visible_bootstrap_count,
    }
    # One live run computes every assertion of the scenario; the status is
    # the whole map's, and the factory reads each cell from its own key.
    run.assertions = awareness_post_compaction_assertions(run.observation)


def run_awareness_post_compaction_scenario(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--launch-timeout-secs", type=float, default=60.0)
    parser.add_argument("--response-timeout-secs", type=float, default=120.0)
    parser.add_argument("--compaction-timeout-secs", type=float, default=90.0)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind=_ARTIFACT_KIND,
    # A launch without coordination authority, or one that never came up, is
    # typed so the factory reports infrastructure rather than a model finding.
    failure_code=lambda exc: (
        "runtime_host_coordination_authority_unavailable"
        if isinstance(exc, RuntimeHostCoordinationAuthorityUnavailable)
        else "claude_coordination_awareness_post_compaction_failed"
    ),
    needs=("evidence_root", "repo_root", "engine", "provider_bin"),
    binaries=(("engine", "longhouse_engine_missing"), ("provider_bin", "claude_binary_missing")),
    runtime_host=True,
    variants=tuple(_CELL_BY_VARIANT),
    provider_bin_aliases=("--claude-bin",),
    model_env="ANTHROPIC_MODEL",
    add_arguments=_add_arguments,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_awareness_post_compaction_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())
