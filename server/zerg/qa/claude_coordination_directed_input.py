#!/usr/bin/env python3
"""Continuous producer for claude/coordination.directed_input.{send,receive}.

Proves ``provider_input_receipt_linked`` and ``attributed_input_visible`` for
the ``claude_coordination_directed_input`` scenario against the real
``/api/agents/directed-inputs`` machinery two real managed Claude Helm
sessions actually use: launches session A (sender) and session B (receiver),
reads each session's real per-session coordination token straight out of the
bootstrap config Longhouse wrote for it (the same token value the
``send``/``inbox`` MCP tools use -- see ``read_coordination_token`` in
``claude_live_session_support.py``), then calls ``POST /directed-inputs`` as A
and ``GET /directed-inputs`` as B directly.

This exercises the create -> persist -> live-deliver -> receipt-link -> list
code path exactly as the real ``send``/``inbox`` MCP tools do
(``zerg/mcp_server/server.py``), without needing the model to actually invoke
them -- source_session_id != target_session_id is required server-side
(``CatalogStore.create_directed_input`` rejects ``same_session``), so this
producer needs two real sessions, not one self-targeting session.
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

from zerg.qa.claude_live_session_support import RuntimeHostHTTPError
from zerg.qa.claude_live_session_support import api_json
from zerg.qa.claude_live_session_support import artifact_manifest
from zerg.qa.claude_live_session_support import claude_launch_environment
from zerg.qa.claude_live_session_support import close_session
from zerg.qa.claude_live_session_support import isolation_paths
from zerg.qa.claude_live_session_support import launch_claude_session
from zerg.qa.claude_live_session_support import now_iso
from zerg.qa.claude_live_session_support import read_coordination_token
from zerg.qa.claude_live_session_support import start_machine_and_shipper
from zerg.qa.claude_live_session_support import wait_until
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.provider_coordination_oracles import directed_input_assertions
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key

_SCENARIO_ID = "claude_coordination_directed_input"
_ASSERTION_SEND = "provider_input_receipt_linked"
_ASSERTION_RECEIVE = "attributed_input_visible"
# Neither assertion carries a `variant:` key in schemas/managed_providers.yml,
# so each gets its own generated `cell:<provider>:<assertion_id>:<scenario_id>`
# execution_variant (see resume_assurance.execution_variant_key) -- the same
# two-cells-one-scenario shape codex_coordination_native.py's directed-input
# cells use. Compute the exact strings the real compiler will pass as
# --variant instead of guessing at a parallel format.
_CELL_BY_VARIANT: dict[str, str] = {
    execution_variant_key(provider="claude", assertion_id=assertion_id, scenario_id=_SCENARIO_ID, variant=None): assertion_id
    for assertion_id in (_ASSERTION_SEND, _ASSERTION_RECEIVE)
}

REGISTRATION = ProducerRegistration(
    producer_id="claude.coordination_directed_input.v1",
    producer_revision=1,
    scenario_id=_SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=(
        (_ASSERTION_SEND, None),
        (_ASSERTION_RECEIVE, None),
    ),
    providers=("claude",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "sender_session_launched",
        "receiver_session_launched",
        "directed_input_created",
        "directed_input_receipt_linked",
        "directed_input_visible_to_receiver",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("claude_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "transcript_shipper_receipt",
        "sender_session_launch_receipt",
        "receiver_session_launch_receipt",
        "directed_input_create_receipt",
        "directed_input_inbox_receipt",
    ),
    required_cleanup=("claude_helm_processes_exited",),
    implementation="server/zerg/qa/claude_coordination_directed_input.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="directed_input_assertions",
    executable_module="zerg.qa.claude_coordination_directed_input",
)

_ARTIFACT_KIND = "claude_coordination_directed_input_result"
_SESSION_HEADER = "X-Longhouse-Session-Id"


def run_directed_input_scenario(args: argparse.Namespace) -> dict[str, Any]:
    requested_assertion_id = _CELL_BY_VARIANT[args.variant]
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-claude-coord-directed-", dir="/tmp"))
    _home, longhouse_home = isolation_paths(isolation_root)
    marker = f"LONGHOUSE_DIRECTED_INPUT_{uuid.uuid4().hex}"

    shipper = None
    sender = None
    receiver = None
    try:
        shipper, environment = start_machine_and_shipper(args, isolation_root=isolation_root, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)
        launch_env = claude_launch_environment(environment, claude_bin=args.claude_bin, engine=args.engine, model=args.model)

        sender_workspace = isolation_root / "workspace-sender"
        sender_workspace.mkdir(parents=True, exist_ok=True)
        sender, sender_session_id = launch_claude_session(
            workspace=sender_workspace,
            project=args.project,
            name="Longhouse directed-input qualification (sender)",
            env=launch_env,
            terminal_path=root / "sender-terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        write_json(root / "sender-session-launch-receipt.json", {"session_id": sender_session_id})

        receiver_workspace = isolation_root / "workspace-receiver"
        receiver_workspace.mkdir(parents=True, exist_ok=True)
        receiver, receiver_session_id = launch_claude_session(
            workspace=receiver_workspace,
            project=args.project,
            name="Longhouse directed-input qualification (receiver)",
            env=launch_env,
            terminal_path=root / "receiver-terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        write_json(root / "receiver-session-launch-receipt.json", {"session_id": receiver_session_id})

        sender_token = wait_until(
            lambda: read_coordination_token(longhouse_home, sender_session_id),
            timeout=30,
            description="sender coordination token bootstrap",
        )
        receiver_token = wait_until(
            lambda: read_coordination_token(longhouse_home, receiver_session_id),
            timeout=30,
            description="receiver coordination token bootstrap",
        )

        created = api_json(
            args.api_url,
            sender_token,
            "directed-inputs",
            method="POST",
            json_body={"target_session_id": receiver_session_id, "text": marker},
            extra_headers={_SESSION_HEADER: sender_session_id},
        )
        write_json(root / "directed-input-create-receipt.json", created)

        directed_input_id = created.get("id")

        def receiver_sees_it() -> dict[str, Any] | None:
            payload = api_json(
                args.api_url,
                receiver_token,
                "directed-inputs?direction=inbound&limit=200",
                extra_headers={_SESSION_HEADER: receiver_session_id},
            )
            for item in payload.get("directed_inputs", []):
                if item.get("id") == directed_input_id:
                    return item
            return None

        # provider_input_receipt_linked can race live delivery; re-list the
        # sender's own outbound inputs (there is no single-item GET route --
        # only POST /directed-inputs and GET /directed-inputs exist) until the
        # same created record shows a linked receipt, so a delivery that lands
        # a moment after creation still counts.
        def receipt_linked() -> dict[str, Any] | None:
            payload = api_json(
                args.api_url,
                sender_token,
                "directed-inputs?direction=outbound&limit=200",
                extra_headers={_SESSION_HEADER: sender_session_id},
            )
            for item in payload.get("directed_inputs", []):
                if item.get("id") == directed_input_id and item.get("input_receipt") is not None:
                    return item
            return None

        receipt_record = None
        try:
            receipt_record = wait_until(receipt_linked, timeout=args.receipt_timeout_secs, description="directed input receipt link")
        except Exception:
            receipt_record = None
        write_json(root / "directed-input-receipt-check.json", receipt_record or {"linked": False})

        inbox_record = None
        try:
            inbox_record = wait_until(
                receiver_sees_it, timeout=args.inbox_timeout_secs, description="directed input visible in receiver inbox"
            )
        except Exception:
            inbox_record = None
        write_json(root / "directed-input-inbox-receipt.json", inbox_record or {"visible": False})

        sender_close = close_session(sender)
        sender = None
        receiver_close = close_session(receiver)
        receiver = None
        write_json(root / "session-close-receipts.json", {"sender": sender_close, "receiver": receiver_close})

        observation = {
            "sender_session_id": sender_session_id,
            "receiver_session_id": receiver_session_id,
            "directed_input_id": directed_input_id,
            "marker": marker,
            "input_persisted": directed_input_id is not None,
            "input_receipt_linked": bool((receipt_record or {}).get("input_receipt") or created.get("input_receipt") is not None),
            "input_visible": inbox_record is not None,
            "source_session_id": (inbox_record or {}).get("source_session_id"),
        }
        assertions = directed_input_assertions(observation)
        # One live run always computes both assertion keys; pass/fail here is
        # scored on the specific assertion_id this --variant invocation is
        # for (see claude_coordination_awareness_post_compaction.py for the
        # identical reasoning).
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
        for session in (sender, receiver):
            if session is not None:
                try:
                    close_session(session)
                except Exception:  # noqa: BLE001 - never let cleanup mask the causal error
                    pass
        error_detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, RuntimeHostHTTPError):
            error_detail = f"RuntimeHostHTTPError[{exc.status}]: {exc.detail}"
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
            "failure_code": "claude_coordination_directed_input_failed",
            "error": error_detail,
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
    parser.add_argument("--receipt-timeout-secs", type=float, default=30.0)
    parser.add_argument("--inbox-timeout-secs", type=float, default=30.0)
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
    result = run_directed_input_scenario(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
