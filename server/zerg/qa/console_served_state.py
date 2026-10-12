#!/usr/bin/env python3
"""Factory producer: prove Console truth on the surface a viewer actually reads.

`provider_console_lifecycle` drives a real Console turn and asserts on the
machine: archived events and the local turn-claim file. `product_console_lifecycle`
asserts on the served state contract but writes `state: completed` itself, with
no provider process. Between them sits the gap the 2026-08-23 wedge fell through:
the archive held the reply, the claim read `terminal`, and the served surface
rendered "Working" for ten hours while every machine-side signal stayed green.

This producer closes it -- a real provider turn, judged by what the browser and
iOS are served:

  live delivery  frames reach the workspace SSE stream during the turn
  settlement     once the reply is served, the state axis stops saying work

The predicates come from `console_served_state_core`, which the shipped
`scripts/qa/console-served-state-e2e.py` also uses, so the factory and the
hand-run harness cannot drift into asserting different things.

Settlement is judged on the structured contract -- run identity, lifecycle,
activity, presentation key, working_set -- never on `display_phase`. Six live
fault-injected drops served "Using shell" and "Thinking"; not one served
"Working", so a label allowlist would have passed the incident it was written
for.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.live_kit import group_dead as _process_group_dead
from zerg.qa.live_kit import pid_dead as _process_dead
from zerg.qa.live_kit import poll
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.producer_shell import write_json
from zerg.qa.provider_artifacts import sha256_file as _sha256_file
from zerg.qa.provider_console_lifecycle import _force_cleanup
from zerg.qa.provider_console_lifecycle import _terminate_live_qualification_session
from zerg.qa.provider_console_lifecycle import turn_identity_mismatches
from zerg.qa.resume_assurance import ProducerRegistration

SCENARIO_ID = "console_served_state"
ASSERTION_LIVE = "live_frames_reach_the_viewer_during_the_turn"
ASSERTION_SETTLED = "served_state_settles_once_the_reply_is_served"
ASSERTION_CAPABILITY = "browser_console_composer_is_ready"

REGISTRATION = ProducerRegistration(
    producer_id="longhouse.console_served_state.v1",
    producer_revision=6,
    scenario_id=SCENARIO_ID,
    # 5: a failure after the vehicle was bound carries the binary receipt.
    scenario_revision=5,
    assertion_cells=((ASSERTION_LIVE, None), (ASSERTION_SETTLED, None), (ASSERTION_CAPABILITY, None)),
    # The subject under test is Longhouse, not a provider release, so this
    # declares no provider subject. Stock Codex is an exact auxiliary vehicle:
    # the factory pins and mounts its release while the proof remains about the
    # Longhouse product.
    providers=(),
    vehicle_provider="codex",
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    # A real provider runs and a real Runtime Host serves the result. Nothing
    # here is reconstructed from fixtures; that is the whole point.
    evidence_classes=("live_token",),
    observed_activity=(
        "live_frame_after_dispatch",
        "reply_served_to_the_viewer",
        "state_axis_settled",
        "browser_console_composer_ready",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("codex_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "provider_auth_receipt",
        "machine_shipper_receipt",
        "vehicle_dispatch_receipt",
        "console_served_state_observation",
        "cleanup_receipt",
    ),
    required_cleanup=("no_orphan_provider_processes", "canary_session_hidden"),
    implementation="server/zerg/qa/console_served_state.py",
    oracle_source="server/zerg/qa/console_served_state_core.py",
    oracle_entrypoint="settlement_state",
    executable_module="zerg.qa.console_served_state",
    provider_artifact_required=False,
    subject_kind="longhouse_product",
)


def _wait_vehicle_dead(claim: dict[str, object] | None, *, timeout: float = 10.0) -> bool:
    if not isinstance(claim, dict):
        return False
    return poll(
        lambda: _process_dead(claim.get("pid")) and _process_group_dead(claim.get("process_group_id")), timeout=timeout, interval=0.1
    )


def _vehicle_dispatch_receipt(
    claim: dict[str, object],
    *,
    provider_bin: Path,
    model: str,
    session_id: str,
    thread_id: str,
    run_id: str,
) -> dict[str, object]:
    result = claim.get("result") if isinstance(claim.get("result"), dict) else {}
    argv = result.get("argv") if isinstance(result, dict) else None
    binary_bound = False
    model_bound = False
    if isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv):
        try:
            binary_bound = Path(argv[0]).resolve(strict=True) == provider_bin.resolve(strict=True)
        except OSError:
            binary_bound = False
        encoded_model = model.replace("\\", "\\\\").replace('"', '\\"')
        model_bound = f'model="{encoded_model}"' in argv
    identity_mismatches = turn_identity_mismatches(claim, provider="codex", session_id=session_id, thread_id=thread_id, run_id=run_id)
    identity_bound = (
        bool(thread_id)
        and not identity_mismatches
        and claim.get("state") == "terminal"
        and isinstance(result, dict)
        and result.get("terminal_state") == "run_completed"
    )
    return {
        "status": "pass" if binary_bound and model_bound and identity_bound else "fail",
        "provider": "codex",
        "session_id": session_id,
        "thread_id": thread_id,
        "run_id": run_id,
        "provider_binary_sha256": _sha256_file(provider_bin),
        "qualification_model": model,
        "binary_bound": binary_bound,
        "model_bound": model_bound,
        "identity_mismatches": identity_mismatches,
        "identity_bound": identity_bound,
        "claim_state": claim.get("state"),
        "terminal_state": result.get("terminal_state") if isinstance(result, dict) else None,
        "pid": claim.get("pid"),
        "process_group_id": claim.get("process_group_id"),
    }


def assertions_from_report(report: dict) -> dict[str, bool]:
    """Map one harness report onto this producer's assertion cells.

    Kept separate from the run so the mapping is testable without a provider,
    and so a report shape change fails here rather than silently reporting pass.
    """
    live = report.get("first_live_frame_s") is not None and int(report.get("frame_count") or 0) > 0 and not report.get("stream_error")
    # Legacy marker_served artifacts admitted prompt echoes. Only the strict
    # post-settlement assistant evidence can qualify reply content.
    evidence = report.get("assistant_marker_after_settlement") or {}
    settled = (
        report.get("marker_served") is True
        and report.get("assistant_reply_complete") is True
        and report.get("duplicate_assistant_marker_seen") is False
        and evidence.get("exactly_once") is True
        and evidence.get("event_count") == 1
        and evidence.get("marker_count") == 1
        and report.get("settle_latency_s") is not None
    )
    capability = report.get("workspace_capability") or {}
    observed = capability.get("observed") if isinstance(capability, dict) else None
    composer_ready = (
        capability.get("ready") is True
        and isinstance(observed, dict)
        and observed.get("input_mode") == "console"
        and observed.get("can_start_turn") is True
        and observed.get("composer_enabled") is True
        and observed.get("composer_disabled_reason") is None
    )
    return {ASSERTION_LIVE: live, ASSERTION_SETTLED: settled, ASSERTION_CAPABILITY: composer_ready}


def observe_console_served_state(
    run: Run,
    *,
    provider: str,
    device_id: str,
    cwd: str,
    model: str | None = None,
    on_session_created=None,
) -> dict[str, object]:
    """Drive one Console turn through the vehicle and put the served-state report and its verdict on ``run``."""

    from zerg.qa import console_served_state_core as core

    arguments = argparse.Namespace(
        provider=provider,
        device_id=device_id,
        cwd=cwd,
        api_url=None,
        turn_timeout=180.0,
        settle_budget=30.0,
        watch_session=None,
        drop_terminal=False,
        model=model,
    )
    report = core.run(arguments, on_session_created=on_session_created)
    run.write("console-served-state-observation.json", report)
    run.observation = report
    run.assertions = assertions_from_report(report)
    return report


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    cleanup: dict[str, object] = {
        "schema_version": 1,
        "artifact_kind": "longhouse_console_served_state_cleanup_receipt",
        "status": "fail",
        "orphan_count": 0,
        "requirements": {
            "no_orphan_provider_processes": False,
            "served_run_retired": False,
            "canary_session_hidden": False,
            "served_session_absent": False,
        },
    }
    provider = "codex"
    # The subject is Longhouse, so the result's identity carries no provider:
    # the factory compares it with a contract that pins ``provider: null``.
    # The vehicle that drove the turn is named beside the identity.
    run.fields.update(vehicle_provider=provider, vehicle_qualification_model=str(args.model))
    device_id: str | None = None
    session_id: str | None = None
    shipper: TranscriptShipper | None = None
    vehicle_claim: dict[str, object] | None = None
    reached = False
    failure: Exception | None = None
    api_url = str(os.environ.get(RUNTIME_API_URL_ENV) or "").strip().rstrip("/")
    require_disposable_runtime(api_url)
    token = str(os.environ.get(RUNTIME_AGENTS_TOKEN_ENV) or "").strip()
    environment: dict[str, str] = {}
    try:
        if not api_url or not token:
            raise RuntimeError(f"{RUNTIME_API_URL_ENV} and {RUNTIME_AGENTS_TOKEN_ENV} are required")
        home = isolated_provider_home()
        workspace = home / "c" / "w"
        engine_evidence = home / "c" / "e"
        longhouse_home = home / "c" / "lh"
        workspace.mkdir(mode=0o700, parents=True)
        engine_evidence.mkdir(mode=0o700, parents=True)
        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(home),
                "CODEX_HOME": str(home / ".codex"),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_DATA_HOME": str(home / ".local" / "share"),
                "XDG_CACHE_HOME": str(home / ".cache"),
                "LONGHOUSE_CODEX_BIN": str(args.provider_bin),
                "LONGHOUSE_ENGINE_BIN": str(args.engine),
                "LONGHOUSE_ORIGIN_KIND": "test_or_canary",
                "LONGHOUSE_LAUNCH_ACTOR": "automation",
                "LONGHOUSE_LAUNCH_SURFACE": "product-e2e",
                "CODEX_MODEL": str(args.model),
            }
        )
        version = subprocess.run(
            [str(args.provider_bin), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        expected_version = f"codex-cli {args.provider_version}"
        if version.returncode != 0 or version.stdout.strip() != expected_version:
            raise RuntimeError("staged Codex vehicle version does not match the retained plan")
        provider_receipt = {
            "provider": provider,
            "path": str(args.provider_bin),
            "sha256": _sha256_file(args.provider_bin),
            "version": str(args.provider_version),
            "raw_version_output": version.stdout.strip(),
        }
        write_json(root / "provider-binary-receipt.json", provider_receipt)
        auth_receipt = login_with_api_key(
            args.provider_bin,
            api_key=str(environment.get("CODEX_API_KEY") or ""),
            environment=environment,
            cwd=workspace,
        )
        environment.pop("CODEX_API_KEY", None)
        environment.pop("OPENAI_API_KEY", None)
        write_json(root / "provider-auth-receipt.json", auth_receipt)
        args.api_url = api_url
        args.agents_token = token
        shipper = start_transcript_shipper(
            provider,
            args,
            home=home,
            environment=environment,
            evidence_root=engine_evidence,
            longhouse_home=longhouse_home,
        )
        device_id = str(shipper.receipt["machine_name"])

        def remember_session(created_session_id: str) -> None:
            nonlocal session_id
            session_id = created_session_id

        report = observe_console_served_state(
            run,
            provider=provider,
            device_id=device_id,
            cwd=str(workspace),
            model=str(args.model),
            on_session_created=remember_session,
        )
        reached = True
        run_id = str(report.get("run_id") or "")
        if not run_id:
            raise RuntimeError("served-state oracle returned no vehicle run identity")
        claim_path = Path(environment["LONGHOUSE_HOME"]) / "agent" / "turn-claims" / f"{run_id}.json"
        vehicle_claim = json.loads(claim_path.read_text(encoding="utf-8"))
        if not isinstance(vehicle_claim, dict):
            raise RuntimeError("Console vehicle claim is not an object")
        dispatch = _vehicle_dispatch_receipt(
            vehicle_claim,
            provider_bin=args.provider_bin,
            model=str(args.model),
            session_id=str(session_id or ""),
            thread_id=str(report.get("thread_id") or ""),
            run_id=run_id,
        )
        write_json(root / "vehicle-dispatch-receipt.json", dispatch)
        if dispatch["status"] != "pass":
            raise RuntimeError("Console vehicle did not bind the exact staged binary, model, and run")
        run.fields["provider_binary"] = provider_receipt
        run.observation = {**report, "vehicle_binary_bound": True, "vehicle_model_bound": True}
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = exc
    finally:
        if vehicle_claim is None and environment.get("LONGHOUSE_HOME"):
            claim_root = Path(environment["LONGHOUSE_HOME"]) / "agent" / "turn-claims"
            for claim_path in sorted(claim_root.glob("*.json")):
                try:
                    candidate = json.loads(claim_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(candidate, dict) and candidate.get("session_id") == session_id:
                    vehicle_claim = candidate
        claims = [vehicle_claim] if vehicle_claim is not None else []
        if failure is not None and session_id:
            cleanup["termination_dispatch"] = _terminate_live_qualification_session(api_url, token, session_id)
        try:
            _force_cleanup(claims)
        except Exception as exc:  # noqa: BLE001 - continue retirement even if local termination fails
            failure = failure or exc
        if shipper is not None:
            try:
                shipper_receipt = shipper.stop()
            except Exception as exc:  # noqa: BLE001 - cleanup failure must dominate the result
                failure = failure or exc
                shipper_receipt = {"stopped": False, "process_dead": False, "process_group_dead": False}
            write_json(root / "machine-shipper-receipt.json", shipper_receipt)
        else:
            shipper_receipt = {"stopped": True, "process_dead": True, "process_group_dead": True}
        retirement: dict[str, object] | None = None
        candidate = run.observation.get("cleanup_receipt") if run.observation is not None else None
        if isinstance(candidate, dict):
            retirement = candidate
        elif session_id:
            try:
                from zerg.qa.live_session_toolkit import retire_qualification_session

                retirement = retire_qualification_session(
                    api_url,
                    token,
                    session_id,
                    provider=provider,
                    project="console-served-state-e2e",
                )
            except Exception as exc:  # noqa: BLE001 - cleanup failure must remain visible
                retirement = {
                    "status": "fail",
                    "session_id": session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        hidden = isinstance(retirement, dict) and retirement.get("hidden") is True
        archived = isinstance(retirement, dict) and retirement.get("archived") is True
        served_session_absent = isinstance(retirement, dict) and retirement.get("present_in_served_inventory") is False
        served_run_retired = isinstance(retirement, dict) and retirement.get("served_run_retired") is True
        # A missing claim is not evidence that a vehicle died. The only
        # lifecycle state that proves it never started is failure before the
        # disposable session was created; every later missing-claim case fails
        # closed.
        vehicle_never_started = session_id is None and not reached
        provider_dead = _wait_vehicle_dead(vehicle_claim) if vehicle_claim is not None else vehicle_never_started
        machine_dead = all(shipper_receipt.get(field) is True for field in ("stopped", "process_dead", "process_group_dead"))
        cleanup_ok = provider_dead and machine_dead and served_run_retired and hidden and archived and served_session_absent
        cleanup.update(
            {
                "status": "pass" if cleanup_ok else "fail",
                "orphan_count": 0 if cleanup_ok else 1,
                "session_id": session_id,
                "session_retirement": retirement,
                "requirements": {
                    "no_orphan_provider_processes": provider_dead and machine_dead,
                    "served_run_retired": served_run_retired,
                    "canary_session_hidden": hidden,
                    "served_session_absent": served_session_absent and archived,
                },
            }
        )
        if cleanup["status"] != "pass" and failure is None:
            failure = RuntimeError("Console vehicle cleanup did not satisfy its required contract")
        if failure is not None:
            cleanup["error_type"] = type(failure).__name__
        write_json(root / "cleanup-receipt.json", cleanup)

    if failure is not None:
        run.fields.update(vehicle_device_id=device_id, failure_context={"vehicle_device_id": device_id, "session_id": session_id})
        raise failure


def _refuse(args: argparse.Namespace) -> str | None:
    vehicle = (args.engine, args.provider_bin, args.provider_version, args.repo_root, args.model)
    return "vehicle_arguments_missing" if any(value in (None, "") for value in vehicle) else None


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="longhouse_console_served_state_result",
    failure_code="console_served_state_failed",
    variant_required=False,
    refuse=_refuse,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(lambda args: PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args)), argv)


if __name__ == "__main__":
    raise SystemExit(main())
