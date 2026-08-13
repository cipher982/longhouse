#!/usr/bin/env python3
"""Direct stock-Cursor Helm turn-boundary quiescence producer.

Continuously re-verifies ``session.activity.turn_boundary`` /
``activity_returns_to_quiescent_at_turn_boundary`` for the Cursor provider by
running the real product end-to-end canary
(:mod:`zerg.qa.cursor_helm_product_e2e`) inside the provider-factory
qualification sandbox.

That canary is the file the schema names as ``oracle_source`` for this
assertion (``schemas/managed_providers.yml``:
``session.activity.turn_boundary`` -> ``cursor_turn_boundary_quiescent``).  It
predates the factory's continuous producer registry: it was written to be run
by hand against a normal, already-``longhouse auth``'d dogfood machine (see
its default ``--artifact-root`` under ``~/.longhouse/canaries/...`` and its
use of ``get_zerg_url()``/``load_token()``, which read *local file* config,
not the factory's ``LONGHOUSE_RUNTIME_API_URL`` / ``LONGHOUSE_RUNTIME_AGENTS_TOKEN``
environment variables).  This module is the adapter: it provisions the exact
disposable machine identity (``machine/state.json`` + ``machine/device-token``
under an isolated ``LONGHOUSE_HOME``) and a real transcript shipper the same
way the existing native-resume producers do, points ``run_product_e2e`` at
that identity, and translates its report into the factory's admissible
``result.json`` contract.  It calls ``run_product_e2e`` unmodified and
verbatim -- it does not re-derive or approximate its quiescence check.

``run_product_e2e`` internally checks, at three separate live turn
boundaries (first reply, remote-sent reply, post-interrupt recovery reply),
that served activity returns to ``quiescent`` -- see its own ``settled()``
helper and its docstring ("Served activity must return to quiescent once a
turn is done."). A clean, non-raising, ``status == "passed"`` report is proof
the assertion held at every boundary it exercised; any failure (including a
turn boundary that never settles) raises and is surfaced here as a typed
"fail" result.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa import cursor_helm_product_e2e
from zerg.qa.provider_native_resume import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.provider_native_resume import RUNTIME_API_URL_ENV
from zerg.qa.provider_native_resume import TranscriptShipper
from zerg.qa.provider_native_resume import _artifact_manifest
from zerg.qa.provider_native_resume import _isolated_provider_home
from zerg.qa.provider_native_resume import _qualification_secrets
from zerg.qa.provider_native_resume import _secret_scan
from zerg.qa.provider_native_resume import _sha256
from zerg.qa.provider_native_resume import _start_transcript_shipper
from zerg.qa.provider_native_resume import _write_json
from zerg.qa.resume_assurance import ProducerRegistration

# The default Cursor model baked into cursor_helm_product_e2e.py itself.
# Prefer the factory's configured CURSOR_MODEL (matching how
# _assurance_child_environment forwards it) and only fall back to this when
# unset, rather than inventing a different default of our own.
_DEFAULT_CURSOR_MODEL = "gpt-5.3-codex-low"

REGISTRATION = ProducerRegistration(
    producer_id="cursor.turn_boundary.v1",
    producer_revision=1,
    scenario_id="cursor_turn_boundary_quiescent",
    scenario_revision=1,
    # This assertion has no authored `variant:` in schemas/managed_providers.yml,
    # so its cell's variant is None -- not an empty string -- matching how
    # provider_capability_schema.py parses an absent `variant:` field and how
    # provider_generic_resume.py declares its own variant-less cells.
    assertion_cells=(("activity_returns_to_quiescent_at_turn_boundary", None),),
    providers=("cursor",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_token",),
    observed_activity=(
        "provider_turn_completed",
        "served_activity_settled_to_quiescent",
        "run_lifecycle_ended_after_teardown",
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
    implementation="server/zerg/qa/cursor_turn_boundary_producer.py",
    oracle_source="server/zerg/qa/cursor_helm_product_e2e.py",
    # cursor_helm_product_e2e.py has no factored-out pure assertion function
    # (unlike provider_resume_oracles.py / provider_coordination_oracles.py):
    # the scenario driver and its postcondition (settled() returning to
    # "quiescent") are the same function. run_product_e2e is the closest
    # addressable symbol and the one this producer actually calls.
    oracle_entrypoint="run_product_e2e",
    executable_module="zerg.qa.cursor_turn_boundary_producer",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def run_turn_boundary(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": _sha256(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    home = _isolated_provider_home()
    shipper: TranscriptShipper | None = None
    report: dict[str, Any] | None = None
    try:
        # Passing os.environ itself (not a copy) so the machine-identity
        # LONGHOUSE_HOME binding _start_transcript_shipper writes lands on
        # this process's real environment -- run_product_e2e's own
        # get_zerg_url()/load_token() calls read local file config resolved
        # from that same env var, and its launched `longhouse cursor`
        # subprocess inherits it too (it passes no explicit env=).
        shipper = _start_transcript_shipper(
            "cursor",
            args,
            home=home,
            environment=os.environ,
            evidence_root=root,
        )
        _write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        # run_product_e2e's own launch argv carries no --cursor-bin selector;
        # it resolves the provider binary ambiently (LONGHOUSE_CURSOR_BIN or
        # PATH), matching schemas/managed_providers.yml's
        # provider_cli_env: LONGHOUSE_CURSOR_BIN for cursor. Pin it to the
        # exact discovered/staged binary this execution was given.
        os.environ["LONGHOUSE_CURSOR_BIN"] = str(args.provider_bin)

        e2e_args = argparse.Namespace(
            workspace=home / "canaries" / "provider-live" / "cursor" / "turn-boundary" / "workspace",
            artifact_root=root / "product-e2e",
            timeout=args.timeout_secs,
            max_archive_lag=args.max_archive_lag_secs,
            model=(args.model or os.environ.get("CURSOR_MODEL", "").strip() or _DEFAULT_CURSOR_MODEL),
            longhouse_bin=str(args.longhouse_cli),
            engine_bin=str(args.engine),
            # The Machine Agent restart leg is launchctl/macOS-only
            # (_restart_machine_agent raises outright on Linux). The
            # qualification sandbox is Linux; this assertion is about
            # turn-boundary quiescence, not restart recovery, so skip it
            # rather than fail every run on an unrelated precondition.
            skip_machine_agent_restart=True,
        )
        try:
            report = cursor_helm_product_e2e.run_product_e2e(e2e_args)
        except Exception as exc:  # noqa: BLE001 - report carries partial state even on raise
            report = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            raise
        finally:
            if isinstance(report, dict):
                _write_json(root / "product-e2e-report.json", report)

        native_ok = report.get("status") == "passed"
        observation = {
            "product_e2e_status": report.get("status"),
            "run_lifecycle_after_teardown": report.get("run_lifecycle_after_teardown"),
            "activity_after_teardown": report.get("activity_after_teardown"),
            "run_id_stable_across_session": report.get("run_id_stable_across_session"),
            "process_alive_after_cancel": report.get("process_alive_after_cancel"),
            "archive_lag_seconds": report.get("archive_lag_seconds"),
            "machine_agent_restart": report.get("machine_agent_restart"),
            # run_product_e2e's own settled() is the postcondition: it raises
            # before returning if served activity fails to reach "quiescent"
            # at any of the first/remote/post-interrupt-recovery turn
            # boundaries it exercises. A clean, non-raising "passed" report
            # is proof that held at every boundary checked.
            "quiescent_at_every_observed_turn_boundary": native_ok,
        }
        assertions = {"activity_returns_to_quiescent_at_turn_boundary": native_ok}
        _write_json(
            root / "cleanup-receipt.json",
            {
                "schema_version": 1,
                "artifact_kind": "cursor_turn_boundary_cleanup_receipt",
                # run_product_e2e's own finally block stops the session
                # (`cursor-helm stop`) and closes the PTY, which SIGTERM/
                # SIGKILLs the launcher's process group -- see its _PtyProcess
                # .close(). This producer does not duplicate that teardown.
                "delegated_to": "cursor_helm_product_e2e.run_product_e2e finally block",
                "session_id": report.get("session_id"),
            },
        )
        redacted_secret_files = _secret_scan(root, list(_qualification_secrets(dict(os.environ), args.agents_token)))
        result: dict[str, Any] = {
            "schema_version": 1,
            "artifact_kind": "direct_turn_boundary_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            # Must equal the compiled command's raw authored `variant`, which
            # is None for this cell (no `variant:` field authored in the
            # schema) -- NOT the opaque execution_variant key received via
            # --variant, which only exists to disambiguate scheduling/retry
            # state. Echoing args.variant here would fail
            # _validate_execution_result's `result.variant == command.variant`
            # check.
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "pass" if native_ok else "fail",
            "observation": observation,
            "assertions": assertions,
            "session_id": report.get("session_id"),
            "provider_binary": provider_receipt,
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        if shipper is not None:
            try:
                _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - preserve the causal failure
                pass
        redacted_secret_files = _secret_scan(root, list(_qualification_secrets(dict(os.environ), args.agents_token)))
        failure = {
            "schema_version": 1,
            "artifact_kind": "direct_turn_boundary_result",
            "producer": REGISTRATION.to_dict(),
            "provider": "cursor",
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "scenario_revision": REGISTRATION.scenario_revision,
            "evidence_class": "live_token",
            "generated_at": _now(),
            "status": "fail",
            "failure_code": "direct_turn_boundary_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "redacted_secret_files": redacted_secret_files,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure
    finally:
        if shipper is not None:
            try:
                _write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - best-effort final shipper stop
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Opaque execution-variant scheduling key from
    # resume_assurance.execution_variant_key(); this cell has no authored
    # variant, so the value is a synthetic "cell:<provider>:<assertion_id>:
    # <scenario_id>" string. It is accepted (the real execute_retained_plan
    # always passes --variant for a non-generic producer) but never echoed
    # into result.json -- see the comment at its use site.
    parser.add_argument("--variant", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    # NOTE: the real (unmodified) provider_factory/assurance.py
    # execute_retained_plan only passes --codex-bin for provider == "codex".
    # Every other provider, cursor included, gets --longhouse-cli and
    # --provider-bin. Do not rename these to --cursor-bin.
    parser.add_argument("--longhouse-cli", required=True, type=Path)
    parser.add_argument("--provider-bin", required=True, type=Path)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout-secs", type=float, default=120.0)
    parser.add_argument("--max-archive-lag-secs", type=float, default=20.0)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    args.api_url = os.environ.get(RUNTIME_API_URL_ENV, "")
    args.agents_token = os.environ.get(RUNTIME_AGENTS_TOKEN_ENV, "")
    if not args.api_url or not args.agents_token:
        print(json.dumps({"status": "fail", "failure_code": "runtime_host_control_credentials_missing"}))
        return 2
    if not args.engine.is_file() or not os.access(args.engine, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_engine_missing"}))
        return 2
    if not args.longhouse_cli.is_file() or not os.access(args.longhouse_cli, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "longhouse_cli_missing"}))
        return 2
    if not args.provider_bin.is_file() or not os.access(args.provider_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "cursor_binary_missing"}))
        return 2
    result = run_turn_boundary(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
