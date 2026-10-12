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
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from zerg.qa.claude_live_session_support import RuntimeHostCoordinationAuthorityUnavailable
from zerg.qa.claude_live_session_support import RuntimeHostHTTPError
from zerg.qa.claude_live_session_support import ScenarioError
from zerg.qa.claude_live_session_support import api_json
from zerg.qa.claude_live_session_support import claude_launch_environment
from zerg.qa.claude_live_session_support import close_session
from zerg.qa.claude_live_session_support import failed_session_close_receipt
from zerg.qa.claude_live_session_support import isolation_paths
from zerg.qa.claude_live_session_support import launch_claude_session
from zerg.qa.claude_live_session_support import local_managed_control_fact
from zerg.qa.claude_live_session_support import local_managed_control_snapshot
from zerg.qa.claude_live_session_support import read_coordination_token
from zerg.qa.claude_live_session_support import start_machine_and_shipper
from zerg.qa.claude_live_session_support import wait_until
from zerg.qa.claude_live_session_support import write_claude_cleanup_aggregate
from zerg.qa.claude_live_session_support import write_json
from zerg.qa.live_session_toolkit import prepare_claude_profile
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_coordination_oracles import directed_input_assertions
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.qa.resume_assurance import execution_variant_key
from zerg.qa.wait_kind import LONGHOUSE_ENGINE

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
    producer_revision=5,
    scenario_id=_SCENARIO_ID,
    # 7: the status is the assertion map's, not the requested cell's.
    scenario_revision=7,
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
        "receiver_live_control_receipt",
        "directed_input_create_receipt",
        "directed_input_inbox_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("claude_helm_processes_exited",),
    implementation="server/zerg/qa/claude_coordination_directed_input.py",
    oracle_source="server/zerg/qa/provider_coordination_oracles.py",
    oracle_entrypoint="directed_input_assertions",
    executable_module="zerg.qa.claude_coordination_directed_input",
)

_ARTIFACT_KIND = "claude_coordination_directed_input_result"
_SESSION_HEADER = "X-Longhouse-Session-Id"


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    isolation_root = Path(tempfile.mkdtemp(prefix="lhx-claude-coord-directed-", dir="/tmp"))
    _home, longhouse_home = isolation_paths(isolation_root)
    marker = f"LONGHOUSE_DIRECTED_INPUT_{uuid.uuid4().hex}"

    shipper = None
    sender = None
    receiver = None
    close_receipts: dict[str, dict[str, Any]] = {
        "sender": {"not_started": True, "alive_after_close": False},
        "receiver": {"not_started": True, "alive_after_close": False},
    }
    try:
        shipper, environment = start_machine_and_shipper(args, isolation_root=isolation_root, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        # Claude persists first-run onboarding (theme/API-key/trust) outside
        # the Longhouse channel state. Launching the managed facade against
        # this fresh disposable profile without completing it first leaves
        # the session stuck at the wizard forever -- see
        # claude_coordination_awareness_create.py's identical fix for the
        # full rationale. Sender and receiver share this isolation_root's one
        # CLAUDE_CONFIG_DIR, so priming it once covers both launches below.
        onboarding_home, _longhouse_home_for_onboarding = isolation_paths(isolation_root)
        onboarding = prepare_claude_profile(
            binary=args.provider_bin,
            home=onboarding_home,
            workspace=isolation_root,
            environment=environment,
            recording=root / "claude-onboarding.tty",
        )
        write_json(root / "claude-onboarding-receipt.json", onboarding)

        launch_env = claude_launch_environment(
            environment,
            claude_bin=args.provider_bin,
            engine=args.engine,
            model=args.model,
            longhouse_home=longhouse_home,
        )

        sender_workspace = isolation_root / "workspace-sender"
        sender_workspace.mkdir(parents=True, exist_ok=True)
        sender, sender_session_id, _sender_provider_session_id = launch_claude_session(
            workspace=sender_workspace,
            project=args.project,
            name="Longhouse directed-input qualification (sender)",
            env=launch_env,
            terminal_path=root / "sender-terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        sender_token = read_coordination_token(longhouse_home, sender_session_id)
        write_json(
            root / "sender-session-launch-receipt.json",
            {
                "session_id": sender_session_id,
                "coordination_authority_available": sender_token is not None,
            },
        )
        if sender_token is None:
            raise RuntimeHostCoordinationAuthorityUnavailable("managed Claude sender launched without Runtime Host coordination authority")

        receiver_workspace = isolation_root / "workspace-receiver"
        receiver_workspace.mkdir(parents=True, exist_ok=True)
        receiver, receiver_session_id, _receiver_provider_session_id = launch_claude_session(
            workspace=receiver_workspace,
            project=args.project,
            name="Longhouse directed-input qualification (receiver)",
            env=launch_env,
            terminal_path=root / "receiver-terminal.log",
            launch_timeout_secs=args.launch_timeout_secs,
        )
        receiver_token = read_coordination_token(longhouse_home, receiver_session_id)
        write_json(
            root / "receiver-session-launch-receipt.json",
            {
                "session_id": receiver_session_id,
                "coordination_authority_available": receiver_token is not None,
            },
        )
        if receiver_token is None:
            raise RuntimeHostCoordinationAuthorityUnavailable(
                "managed Claude receiver launched without Runtime Host coordination authority"
            )

        receiver_control = wait_until(
            lambda: local_managed_control_fact(longhouse_home, receiver_session_id),
            timeout=args.receipt_timeout_secs,
            description="receiver attached local live-control fact",
            # A fact the engine writes on the machine once its channel is attached.
            waited_on=LONGHOUSE_ENGINE,
        )
        write_json(root / "receiver-live-control-receipt.json", receiver_control)

        # A local control fact is written just before the corresponding
        # heartbeat POST. Retry the idempotent create with one stable request
        # id until the Runtime Host has consumed that heartbeat and links the
        # provider input receipt. Reusing the id is essential: a fresh id on
        # each attempt would deliver duplicate peer messages.
        client_request_id = str(uuid.uuid4())
        create_attempts: list[dict[str, Any]] = []
        create_errors: list[str] = []

        def create_with_receipt() -> dict[str, Any] | None:
            try:
                response = api_json(
                    args.api_url,
                    sender_token,
                    "directed-inputs",
                    method="POST",
                    json_body={
                        "target_session_id": receiver_session_id,
                        "text": marker,
                        "client_request_id": client_request_id,
                    },
                    extra_headers={_SESSION_HEADER: sender_session_id},
                )
            except RuntimeHostHTTPError as exc:
                # A delayed HTTP answer is not evidence that the durable create
                # failed. Keep observing the outbound row by its stable caller
                # id; retrying POST with a new id could duplicate delivery.
                create_errors.append(str(exc))
                return None
            create_attempts.append(response)
            return response if response.get("input_receipt") is not None else None

        try:
            created = wait_until(
                create_with_receipt,
                timeout=args.receipt_timeout_secs,
                description="directed input receipt link",
            )
        except ScenarioError:
            created = (
                create_attempts[-1]
                if create_attempts
                else {
                    "client_request_id": client_request_id,
                    "create_errors": create_errors,
                }
            )
        write_json(root / "directed-input-create-receipt.json", created)

        directed_input_id = created.get("id")

        def _matches(item: dict[str, Any]) -> bool:
            return item.get("id") == directed_input_id or (directed_input_id is None and item.get("client_request_id") == client_request_id)

        def _receipt_is_nonterminal(item: dict[str, Any] | None) -> bool:
            receipt = (item or {}).get("input_receipt")
            if not isinstance(receipt, dict):
                return False
            return receipt.get("status") not in {"failed", "cancelled"}

        def receiver_sees_it() -> dict[str, Any] | None:
            nonlocal directed_input_id
            payload = api_json(
                args.api_url,
                receiver_token,
                "directed-inputs?direction=inbound&limit=200",
                extra_headers={_SESSION_HEADER: receiver_session_id},
            )
            for item in payload.get("directed_inputs", []):
                if isinstance(item, dict) and _matches(item):
                    directed_input_id = item.get("id")
                    return item
            return None

        # provider_input_receipt_linked can race live delivery; re-list the
        # sender's own outbound inputs by the stable caller id. The create POST
        # may have timed out after persistence, so an HTTP response is not the
        # observer's source of truth.
        def receipt_linked() -> dict[str, Any] | None:
            nonlocal directed_input_id
            payload = api_json(
                args.api_url,
                sender_token,
                "directed-inputs?direction=outbound&limit=200",
                extra_headers={_SESSION_HEADER: sender_session_id},
            )
            for item in payload.get("directed_inputs", []):
                if isinstance(item, dict) and _matches(item) and item.get("input_receipt") is not None:
                    directed_input_id = item.get("id")
                    return item
            return None

        receipt_record = created if _receipt_is_nonterminal(created) else None
        if receipt_record is None:
            try:
                receipt_record = wait_until(
                    receipt_linked,
                    timeout=args.receipt_timeout_secs,
                    description="directed input receipt link",
                )
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
        close_receipts["sender"] = sender_close
        sender = None
        receiver_close = close_session(receiver)
        close_receipts["receiver"] = receiver_close
        receiver = None
        write_json(root / "session-close-receipts.json", {"sender": sender_close, "receiver": receiver_close})
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None
        write_claude_cleanup_aggregate(
            root,
            producer_id=REGISTRATION.producer_id,
            required_cleanup=REGISTRATION.required_cleanup,
            outcomes={
                "claude_helm_processes_exited": all(receipt.get("alive_after_close") is False for receipt in close_receipts.values())
            },
            diagnostics={"sessions": close_receipts},
        )

        input_receipt = (receipt_record or {}).get("input_receipt")
        run.observation = {
            "sender_session_id": sender_session_id,
            "receiver_session_id": receiver_session_id,
            "directed_input_id": directed_input_id,
            "client_request_id": client_request_id,
            "marker": marker,
            "input_persisted": directed_input_id is not None,
            "input_receipt_linked": _receipt_is_nonterminal(receipt_record),
            "input_receipt_status": input_receipt.get("status") if isinstance(input_receipt, dict) else None,
            "input_visible": inbox_record is not None,
            "source_session_id": (inbox_record or {}).get("source_session_id"),
        }
        # One live run computes every assertion of the scenario; the status is
        # the whole map's, and the factory reads each cell from its own key.
        run.assertions = directed_input_assertions(run.observation)
    except Exception as exc:  # noqa: BLE001 - close what the run owned, then let the shell type the failure
        if "receiver_session_id" in locals():
            diagnostic = local_managed_control_snapshot(longhouse_home, receiver_session_id)
            if diagnostic is not None:
                write_json(root / "receiver-live-control-diagnostic.json", diagnostic)
        for role, session in (("sender", sender), ("receiver", receiver)):
            if session is not None:
                try:
                    close_receipts[role] = close_session(session)
                except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
                    close_receipts[role] = failed_session_close_receipt(session, cleanup_exc)
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
                close_receipts["shipper"] = {"error": f"{type(cleanup_exc).__name__}: {cleanup_exc}"}
            shipper = None
        cleanup_recording_error = None
        try:
            write_claude_cleanup_aggregate(
                root,
                producer_id=REGISTRATION.producer_id,
                required_cleanup=REGISTRATION.required_cleanup,
                outcomes={
                    "claude_helm_processes_exited": all(
                        close_receipts[role].get("alive_after_close") is False for role in ("sender", "receiver")
                    )
                },
                diagnostics={"sessions": close_receipts},
            )
        except Exception as cleanup_exc:  # noqa: BLE001 - preserve the causal error below
            cleanup_recording_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        if isinstance(exc, RuntimeHostHTTPError):
            run.fields["error"] = f"RuntimeHostHTTPError[{exc.status}]: {exc.detail}"
        if cleanup_recording_error:
            run.fields["cleanup_recording_error"] = cleanup_recording_error
        raise
    finally:
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - result already retains the causal failure
                pass
        shutil.rmtree(isolation_root, ignore_errors=True)


def run_directed_input_scenario(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="zerg")
    parser.add_argument("--launch-timeout-secs", type=float, default=60.0)
    parser.add_argument("--receipt-timeout-secs", type=float, default=30.0)
    parser.add_argument("--inbox-timeout-secs", type=float, default=30.0)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind=_ARTIFACT_KIND,
    # A launch without coordination authority, or one that never came up, is
    # typed so the factory reports infrastructure rather than a model finding.
    failure_code=lambda exc: (
        "runtime_host_coordination_authority_unavailable"
        if isinstance(exc, RuntimeHostCoordinationAuthorityUnavailable)
        else "claude_coordination_directed_input_failed"
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
    return PRODUCER.main(run_directed_input_scenario, argv)


if __name__ == "__main__":
    raise SystemExit(main())
