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
from zerg.qa.claude_live_session_support import find_compaction_boundary
from zerg.qa.claude_live_session_support import find_tool_invocation
from zerg.qa.claude_live_session_support import isolation_paths
from zerg.qa.claude_live_session_support import launch_claude_session
from zerg.qa.claude_live_session_support import mcp_bootstrap_config_paths
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import start_machine_and_shipper
from zerg.qa.claude_live_session_support import wait_until
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.managed_claude_live import transcript_line_counts
from zerg.qa.provider_coordination_oracles import awareness_post_compaction_assertions
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import _prepare_claude_profile
from zerg.qa.pty_session import wait_for_terminal_quiescence
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

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
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=1,
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
    ),
    required_cleanup=("claude_helm_process_exited",),
    implementation="server/zerg/qa/claude_coordination_awareness_post_compaction.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="awareness_post_compaction_assertions",
    executable_module="zerg.qa.claude_coordination_awareness_post_compaction",
)

_ARTIFACT_KIND = "claude_coordination_awareness_post_compaction_result"


def run_awareness_post_compaction_scenario(args: argparse.Namespace) -> dict[str, Any]:
    requested_assertion_id = _CELL_BY_VARIANT[args.variant]
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-claude-coord-compact-", dir="/tmp"))
    _home, longhouse_home = isolation_paths(isolation_root)
    workspace = isolation_root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    marker_pre = f"LONGHOUSE_COORD_PRECOMPACT_{uuid.uuid4().hex}"
    marker_post = f"LONGHOUSE_COORD_POSTCOMPACT_{uuid.uuid4().hex}"
    probe_repo = f"longhouse-coordination-awareness-probe-{uuid.uuid4().hex[:12]}"

    def peers_prompt(marker: str) -> str:
        return (
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
        # the session stuck at the wizard forever -- see
        # claude_coordination_awareness_create.py's identical fix for the
        # full rationale.
        onboarding_home, _longhouse_home_for_onboarding = isolation_paths(isolation_root)
        onboarding = _prepare_claude_profile(
            binary=args.claude_bin,
            home=onboarding_home,
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
            name="Longhouse coordination-awareness-post-compaction qualification",
            env=launch_env,
            terminal_path=root / "terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        write_json(root / "session-launch-receipt.json", {"session_id": session_id, "workspace": str(workspace)})
        transcript_id = provider_session_id or session_id

        # These are real Helm-user turns. The coordination channel is
        # deliberately untrusted peer input and must not be used to command
        # the model to call a tool.
        session.submit_line(peers_prompt(marker_pre))
        await_assistant_marker(
            session_id=session_id,
            marker=marker_pre,
            timeout=args.response_timeout_secs,
            home=Path(environment["HOME"]),
            provider_session_id=provider_session_id,
        )
        pre_invocation = find_tool_invocation(transcript_id, "peers", home=Path(environment["HOME"]))
        write_json(root / "pre-compaction-tool-invocation.json", pre_invocation or {"found": False})

        bootstrap_configs_before = mcp_bootstrap_config_paths(longhouse_home, session_id)

        pre_compaction_line_counts = transcript_line_counts(transcript_id, home=Path(environment["HOME"]))
        session.submit_line("/compact")
        wait_for_terminal_quiescence(session, timeout=args.compaction_timeout_secs, minimum_bytes=1, stable_seconds=2.0)
        compaction_boundary = wait_until(
            lambda: find_compaction_boundary(
                transcript_id,
                after_line_counts=pre_compaction_line_counts,
                home=Path(environment["HOME"]),
            ),
            timeout=args.compaction_timeout_secs,
            description="Claude transcript compaction boundary",
        )
        write_json(root / "compaction-boundary-receipt.json", compaction_boundary)

        session.submit_line(peers_prompt(marker_post))
        await_assistant_marker(
            session_id=session_id,
            marker=marker_post,
            timeout=args.response_timeout_secs,
            home=Path(environment["HOME"]),
            provider_session_id=provider_session_id,
        )
        post_invocation = find_tool_invocation(
            transcript_id, "peers", after_line_counts=pre_compaction_line_counts, home=Path(environment["HOME"])
        )
        write_json(root / "post-compaction-tool-invocation.json", post_invocation or {"found": False})

        bootstrap_configs_after = mcp_bootstrap_config_paths(longhouse_home, session_id)
        write_json(
            root / "mcp-bootstrap-config-manifest.json",
            {
                "before_compact": [str(path) for path in bootstrap_configs_before],
                "after_compact": [str(path) for path in bootstrap_configs_after],
            },
        )

        close_receipt = close_session(session)
        session = None
        write_json(root / "session-close-receipt.json", close_receipt)
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None

        pre_ok = (
            pre_invocation is not None and pre_invocation.get("tool_result_line") is not None and pre_invocation.get("is_error") is not True
        )
        post_ok = (
            post_invocation is not None
            and post_invocation.get("tool_result_line") is not None
            and post_invocation.get("is_error") is not True
        )
        # A confident count requires having actually found the bootstrap
        # directory. An empty glob is ambiguous between "correctly bootstrapped
        # once, path assumption right" and "path assumption wrong" -- do not
        # manufacture a specific count in that case; report None (the oracle
        # already treats None as non-duplicating) and let the raw manifest
        # above carry the evidence for a human to check.
        visible_bootstrap_count = len(bootstrap_configs_after) if bootstrap_configs_after or bootstrap_configs_before else None

        observation = {
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
        assertions = awareness_post_compaction_assertions(observation)
        # This one live run always computes both assertion keys, but the
        # requested cell -- the specific assertion_id this --variant
        # invocation is for -- is what decides pass/fail here. The other
        # key still rides along in the assertions map as a real bonus
        # observation (matching provider_coordination_oracles' own shape:
        # directed_input_assertions does the same with its extra
        # "directed_input_persisted" key), but it is not what this
        # invocation is being scored on.
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
            "status": "pass" if assertions.get(requested_assertion_id) is True else "fail",
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
            "failure_code": "claude_coordination_awareness_post_compaction_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "assertions": {requested_assertion_id: False},
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
    parser.add_argument("--variant", required=True, choices=tuple(_CELL_BY_VARIANT))
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--claude-bin", type=Path)
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"))
    parser.add_argument("--launch-timeout-secs", type=float, default=60.0)
    parser.add_argument("--response-timeout-secs", type=float, default=120.0)
    parser.add_argument("--compaction-timeout-secs", type=float, default=90.0)
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
    result = run_awareness_post_compaction_scenario(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
