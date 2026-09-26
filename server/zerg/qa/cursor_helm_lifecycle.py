#!/usr/bin/env python3
"""Live Cursor Helm lifecycle proof through the stock Longhouse facade.

Launches stock ``cursor-agent`` through ``longhouse cursor`` and drives every
control through the Runtime Host, the same path a user's client takes:
launch, idle send, active-turn steer, abort, and terminate. The oracles live
in :mod:`zerg.qa.cursor_helm_product_e2e` and judge Cursor's own hook
records, not the keystrokes Longhouse sent.

``--negative-control`` runs the same scenario against a Machine Agent built with
the ``qa-fault-injection`` feature, with one control per corrupted step:
``cursor_steer_queue_only`` queues the steer as a follow-up instead of steering,
and ``cursor_abort_noop`` acknowledges the abort without sending ^C, so the
generation runs on and answers. The assertion that owns that step must then
fail, a receipt must show the fault fired, and launch and send must still hold;
anything else is inconclusive or a broken oracle.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from zerg.qa import cursor_helm_product_e2e
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import bound_terminal_recordings
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import qualification_secrets
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import secret_scan
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import wait_pid_dead
from zerg.qa.live_session_toolkit import wait_process_group_dead
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.provider_release_identity import artifact_manifest
from zerg.qa.provider_release_identity import now
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.resume_assurance import ProducerRegistration

_DEFAULT_CURSOR_MODEL = "gpt-5.3-codex-low"
NEGATIVE_CONTROLS = ("cursor_steer_queue_only", "cursor_abort_noop")
# A steer that arrived as the next turn, or never changed the original one.
_STEER_FAULT_FAILURE_CODES = frozenset({"steer_delivered_as_followup", "steer_did_not_change_course"})
# Which lifecycle step each fault corrupts, and therefore which assertion has
# to catch it. A fault that fires against one step proves nothing about another.
_FAULT_TARGETS = {
    "cursor_steer_queue_only": ("steer_active", "cursor_helm_steer_active"),
    "cursor_abort_noop": ("abort_native", "cursor_helm_abort_native"),
}

ASSERTIONS = (
    "cursor_helm_launch_registration",
    "cursor_helm_send_idle",
    "cursor_helm_steer_active",
    "cursor_helm_abort_native",
    "cursor_helm_terminate_owned",
)

REGISTRATION = ProducerRegistration(
    producer_id="cursor.helm_lifecycle.v1",
    producer_revision=1,
    scenario_id="cursor_helm_lifecycle",
    scenario_revision=2,
    assertion_cells=tuple((item, None) for item in ASSERTIONS),
    providers=("cursor",),
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "stock_longhouse_cursor_helm_launch",
        "cursor_native_binding_claimed",
        "remote_send_reply_archived",
        "steer_landed_in_target_generation",
        "aborted_generation_followed_by_completed_turn",
        "remote_terminate_provider_dead",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=("cursor_provider_token", "runtime_host_control"),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "transcript_shipper_receipt",
        "product_e2e_report",
        "cleanup_receipt",
    ),
    required_cleanup=("session_stopped", "no_orphan_provider_processes"),
    implementation="server/zerg/qa/cursor_helm_lifecycle.py",
    oracle_source="server/zerg/qa/cursor_helm_product_e2e.py",
    oracle_entrypoint="steer_landed_in_generation",
    executable_module="zerg.qa.cursor_helm_lifecycle",
    required_executables=("longhouse", "longhouse-engine"),
    observation_scope="scenario",
)


def failed_run_report(artifact_root: Path, exc: BaseException) -> dict[str, Any]:
    """The failed e2e run's own report, so its proven steps survive the failure.

    run_product_e2e mutates its report in place and always writes it in its own
    finally, so a run that dies at, say, the steer still leaves the launch and
    send evidence on disk. Substituting a bare status/error dict discarded it:
    `lifecycle` went missing, so every assertion -- including
    launch_registration and send_idle, which had already passed -- read false,
    and `cursor_pid` went missing, so session_stopped could never be proven and
    the cell died as "lacks required cleanup" before any oracle ran. That is why
    all five cursor_helm cells read never_proven while the evidence for two of
    them sat in product-e2e.json (2026-09-19).

    The status stays `failed`: this recovers evidence, it never upgrades a
    verdict. Each assertion is still judged only by its own oracle.
    """

    failure = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    try:
        persisted = json.loads((artifact_root / "product-e2e.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return failure
    if not isinstance(persisted, dict):
        return failure
    return {**persisted, **failure}


def lifecycle_assertions(report: dict[str, Any], *, cleanup_ok: bool) -> dict[str, bool]:
    lifecycle = report.get("lifecycle") if isinstance(report.get("lifecycle"), dict) else {}
    passed = report.get("status") == "passed"

    def held(key: str) -> bool:
        value = lifecycle.get(key)
        if not isinstance(value, dict):
            return False
        if "passed" in value:
            return value.get("passed") is True
        return all(item is True for item in value.values())

    return {
        "cursor_helm_launch_registration": held("launch_registration"),
        "cursor_helm_send_idle": held("send_idle"),
        "cursor_helm_steer_active": passed and held("steer_active"),
        "cursor_helm_abort_native": passed and held("abort_native"),
        "cursor_helm_terminate_owned": passed and held("terminate_owned") and cleanup_ok,
    }


def negative_control_verdict(report: dict[str, Any], *, fault: str) -> dict[str, Any]:
    """Pass only when the injected fault fired and the target oracle caught it."""

    step_name, target_assertion = _FAULT_TARGETS[fault]
    lifecycle = report.get("lifecycle") if isinstance(report.get("lifecycle"), dict) else {}
    step = lifecycle.get(step_name) if isinstance(lifecycle.get(step_name), dict) else {}
    receipt = step.get("qa_fault_receipt")
    preconditions = lifecycle_assertions(report, cleanup_ok=True)
    fault_fired = isinstance(receipt, dict) and receipt.get("fault") == fault
    preconditions_held = preconditions["cursor_helm_launch_registration"] and preconditions["cursor_helm_send_idle"]
    if fault == "cursor_abort_noop":
        # An unsent ^C leaves the generation to finish on its own: it never
        # stops as aborted, and it answers with the reply the oracle forbids.
        # Requiring that shape keeps an unrelated abort failure inconclusive.
        oracle_rejected = step.get("passed") is False and (
            step.get("generation_stopped_aborted") is False or step.get("forbidden_response_produced") is True
        )
        target_detail = {
            "generation_stopped_aborted": step.get("generation_stopped_aborted"),
            "forbidden_response_produced": step.get("forbidden_response_produced"),
            # provider_factory/negative_controls.py's independent normalize_verdict
            # falls back to a generic branch that requires a typed
            # target_failure_code string to confirm the target genuinely failed
            # in the fault's shape. Without this, its independent recompute
            # always disagreed with this producer's own "pass" claim and
            # recorded the control as "fail" no matter how correctly the abort
            # oracle judged cursor_abort_noop (factory.sqlite3 negative_control_verdicts,
            # 2026-09-22 and 2026-09-25: producer_claim=pass, independent verdict=fail).
            "target_failure_code": step.get("failure_code"),
        }
    else:
        oracle_rejected = step.get("passed") is False and step.get("failure_code") in _STEER_FAULT_FAILURE_CODES
        target_detail = {"target_failure_code": step.get("failure_code")}
    if report.get("status") != "negative_control_observed" or not fault_fired or not preconditions_held:
        status = "inconclusive"
    elif oracle_rejected:
        status = "pass"
    elif step.get("passed") is True:
        # The oracle accepted a run whose control was deliberately broken.
        status = "fail"
    else:
        # It rejected the run, but not in the shape this fault produces, so the
        # run says nothing about whether the oracle catches this fault.
        status = "inconclusive"
    return {
        "status": status,
        "fault": fault,
        "fault_fired": fault_fired,
        "preconditions_held": preconditions_held,
        "target_assertion": target_assertion,
        **target_detail,
    }


def run_lifecycle(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    write_json(root / "provider-binary-receipt.json", provider_receipt)

    home = isolated_provider_home()
    shipper: TranscriptShipper | None = None
    report: dict[str, Any] | None = None
    saved = {name: os.environ.get(name) for name in ("LONGHOUSE_CURSOR_BIN", "LONGHOUSE_HOME", "LH_QA_FAULT", "AGENT_CLI_CREDENTIAL_STORE")}
    artifact_kind = "negative_control_result" if args.negative_control else "direct_helm_lifecycle_result"
    try:
        # The Machine Agent computes its control grants when its channel starts,
        # so the staged binary must be bound before the shipper starts.
        os.environ["LONGHOUSE_CURSOR_BIN"] = str(args.provider_bin)
        os.environ["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
        os.environ["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
        os.environ["LONGHOUSE_LAUNCH_SURFACE"] = "test"
        # Cursor's default macOS credential store is the desktop Keychain; with
        # a relocated HOME it raises a "Keychain Not Found" dialog.
        os.environ["AGENT_CLI_CREDENTIAL_STORE"] = "file"
        if args.negative_control:
            os.environ["LH_QA_FAULT"] = args.negative_control
        else:
            os.environ.pop("LH_QA_FAULT", None)

        shipper = start_transcript_shipper("cursor", args, home=home, environment=os.environ, evidence_root=root)
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        e2e_args = argparse.Namespace(
            workspace=home / "canaries" / "provider-live" / "cursor" / "helm-lifecycle" / "workspace",
            artifact_root=root / "product-e2e",
            timeout=args.timeout_secs,
            max_archive_lag=args.max_archive_lag_secs,
            model=(args.model or os.environ.get("CURSOR_MODEL", "").strip() or _DEFAULT_CURSOR_MODEL),
            longhouse_bin=str(args.longhouse_cli),
            engine_bin=str(args.engine),
            api_url=args.api_url,
            agents_token=args.agents_token,
            lifecycle_only=True,
            permission_mode="auto_approve",
            skip_machine_agent_restart=True,
        )
        try:
            report = cursor_helm_product_e2e.run_product_e2e(e2e_args)
        except Exception as exc:  # noqa: BLE001 - the report carries partial observations
            # run_product_e2e mutates its report in place and always writes it
            # in its own finally, so the failed run's proven steps survive on
            # disk. Replacing it with a bare status/error dict discarded them:
            # lifecycle went missing, so every assertion -- including
            # launch_registration and send_idle, which had already passed --
            # read false, and cursor_pid went missing, so session_stopped could
            # never be proven and the cell died as "lacks required cleanup"
            # before any oracle ran. That is why all five cursor_helm cells were
            # never_proven while the evidence for two of them sat in
            # product-e2e.json (2026-09-19). Read it back; the bare dict is only
            # the fallback when even that is unavailable.
            report = failed_run_report(e2e_args.artifact_root, exc)
        finally:
            if isinstance(report, dict):
                write_json(root / "product-e2e-report.json", report)

        raw_pid = report.get("cursor_pid")
        cursor_pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 1 else None
        provider_dead = cursor_pid is None or wait_pid_dead(cursor_pid)
        group_dead = cursor_pid is None or wait_process_group_dead(cursor_pid)
        required_cleanup = {
            # A passing run proves teardown through served state; any other run
            # was stopped by the canary's own finally, so the provider must be gone.
            "session_stopped": report.get("run_lifecycle_after_teardown") == "ended"
            if report.get("status") == "passed"
            else provider_dead and cursor_pid is not None,
            "no_orphan_provider_processes": provider_dead and group_dead,
        }
        cleanup_ok = all(required_cleanup.values())
        write_json(
            root / "cleanup-receipt.json",
            {
                "schema_version": 1,
                "artifact_kind": "cursor_helm_lifecycle_cleanup_receipt",
                "session_id": report.get("session_id"),
                "status": "pass" if cleanup_ok else "fail",
                "orphan_count": 0 if required_cleanup["no_orphan_provider_processes"] else None,
                "required_cleanup": required_cleanup,
                "diagnostics": {"cursor_pid": cursor_pid, "provider_process_dead": provider_dead, "process_group_dead": group_dead},
            },
        )
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None
        bound_terminal_recordings(
            root / "product-e2e", provider="cursor-helm-lifecycle", states=[], recording_names=("terminal.raw",), checkpoint_name=None
        )
        redacted = secret_scan(root, list(qualification_secrets(dict(os.environ), args.agents_token)))
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": artifact_kind,
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "observation_scope": "scenario",
            "generated_at": now(),
            "session_id": report.get("session_id"),
            "provider_binary": provider_receipt,
            "observation": {"lifecycle": report.get("lifecycle"), "error": report.get("error"), **required_cleanup},
            "redacted_secret_files": redacted,
        }
        if args.negative_control:
            verdict = negative_control_verdict(report, fault=args.negative_control)
            result.update({"status": verdict["status"], "negative_control": verdict})
        else:
            assertions = lifecycle_assertions(report, cleanup_ok=cleanup_ok)
            result.update({"status": "pass" if all(assertions.values()) else "fail", "assertions": assertions})
            if result["status"] == "fail":
                result["failure_code"] = "cursor_helm_lifecycle_failed"
        result["artifact_manifest"] = artifact_manifest(root)
        write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = {
            "schema_version": 1,
            "artifact_kind": artifact_kind,
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "observation_scope": "scenario",
            "generated_at": now(),
            "status": "inconclusive" if args.negative_control else "fail",
            "failure_code": "cursor_helm_lifecycle_harness_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": secret_scan(root, list(qualification_secrets(dict(os.environ), args.agents_token))),
            "artifact_manifest": artifact_manifest(root),
        }
        write_json(root / "result.json", failure)
        return failure
    finally:
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - best-effort final stop
                pass
        for name, previous in saved.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--longhouse-cli", required=True, type=Path)
    parser.add_argument("--provider-bin", required=True, type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout-secs", type=float, default=150.0)
    parser.add_argument("--max-archive-lag-secs", type=float, default=20.0)
    parser.add_argument("--negative-control", choices=NEGATIVE_CONTROLS, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    require_disposable_runtime(args.api_url)
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    for path, code in (
        (args.engine, "longhouse_engine_missing"),
        (args.longhouse_cli, "longhouse_cli_missing"),
        (args.provider_bin, "cursor_binary_missing"),
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            print(json.dumps({"status": "fail", "failure_code": code}))
            return 2
    result = run_lifecycle(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
