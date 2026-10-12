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
import os
import subprocess
from pathlib import Path
from typing import Any

from zerg.qa import cursor_helm_product_e2e
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import bound_terminal_recordings
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import qualification_secrets
from zerg.qa.live_session_toolkit import secret_scan
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import wait_pid_dead
from zerg.qa.live_session_toolkit import wait_process_group_dead
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.resume_assurance import ProducerRegistration

# The default Cursor model baked into cursor_helm_product_e2e.py itself.
# Prefer the factory's configured CURSOR_MODEL (matching how
# _assurance_child_environment forwards it) and only fall back to this when
# unset, rather than inventing a different default of our own.
_DEFAULT_CURSOR_MODEL = "gpt-5.3-codex-low"


def _bound_product_terminal_recording(root: Path) -> None:
    """Keep the product canary's full-screen PTY diagnostic within proof limits."""

    product_e2e_root = root / "product-e2e"
    if product_e2e_root.is_dir():
        bound_terminal_recordings(
            product_e2e_root,
            provider="cursor-turn-boundary",
            states=[],
            recording_names=("terminal.raw",),
            checkpoint_name=None,
        )


REGISTRATION = ProducerRegistration(
    producer_id="cursor.turn_boundary.v1",
    producer_revision=2,
    scenario_id="cursor_turn_boundary_quiescent",
    # 4: a failed result carries the binary receipt and follows the one failure rule.
    scenario_revision=4,
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


def _reach(run: Run, args: argparse.Namespace) -> None:
    root = run.root
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": sha256_file(args.provider_bin),
        "version": subprocess.run(
            [str(args.provider_bin), "--version"], capture_output=True, text=True, timeout=30, check=False
        ).stdout.strip(),
    }
    write_json(root / "provider-binary-receipt.json", provider_receipt)
    run.fields["provider_binary"] = provider_receipt

    home = isolated_provider_home()
    shipper: TranscriptShipper | None = None
    report: dict[str, Any] | None = None
    previous_cursor_bin = os.environ.get("LONGHOUSE_CURSOR_BIN")
    previous_longhouse_home = os.environ.get("LONGHOUSE_HOME")
    try:
        # The Machine Agent computes its advertised control capabilities once
        # when its WebSocket channel starts. Bind the exact staged Cursor
        # binary before starting it; setting this only before `longhouse
        # cursor` lets Helm launch, but leaves cursor.send/interrupt/terminate
        # absent from the already-connected control channel.
        os.environ["LONGHOUSE_CURSOR_BIN"] = str(args.provider_bin)
        os.environ["LONGHOUSE_ORIGIN_KIND"] = "test_or_canary"
        os.environ["LONGHOUSE_LAUNCH_ACTOR"] = "automation"
        os.environ["LONGHOUSE_LAUNCH_SURFACE"] = "test"

        # Passing os.environ itself (not a copy) so the machine-identity
        # LONGHOUSE_HOME binding start_transcript_shipper writes lands on
        # this process's real environment -- run_product_e2e's own
        # get_zerg_url()/load_token() calls read local file config resolved
        # from that same env var, and its launched `longhouse cursor`
        # subprocess inherits it too (it passes no explicit env=).
        shipper = start_transcript_shipper(
            "cursor",
            args,
            home=home,
            environment=os.environ,
            evidence_root=root,
        )
        write_json(root / "transcript-shipper-receipt.json", shipper.receipt)

        e2e_args = argparse.Namespace(
            workspace=home / "canaries" / "provider-live" / "cursor" / "turn-boundary" / "workspace",
            artifact_root=root / "product-e2e",
            timeout=args.timeout_secs,
            max_archive_lag=args.max_archive_lag_secs,
            model=(args.model or os.environ.get("CURSOR_MODEL", "").strip() or _DEFAULT_CURSOR_MODEL),
            longhouse_bin=str(args.longhouse_cli),
            engine_bin=str(args.engine),
            api_url=args.api_url,
            agents_token=args.agents_token,
            turn_boundary_only=True,
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
            report = cursor_helm_product_e2e.failed_report(exc)
            raise
        finally:
            if isinstance(report, dict):
                write_json(root / "product-e2e-report.json", report)

        raw_cursor_pid = report.get("cursor_pid")
        cursor_pid = (
            raw_cursor_pid if isinstance(raw_cursor_pid, int) and not isinstance(raw_cursor_pid, bool) and raw_cursor_pid > 1 else None
        )
        provider_process_dead = cursor_pid is not None and wait_pid_dead(cursor_pid)
        provider_process_group_dead = cursor_pid is not None and wait_process_group_dead(cursor_pid)
        session_stopped = report.get("run_lifecycle_after_teardown") == "ended"
        no_orphans = provider_process_dead and provider_process_group_dead
        required_cleanup = {
            "session_stopped": session_stopped,
            "no_orphan_provider_processes": no_orphans,
        }
        native_ok = report.get("status") == "passed" and all(required_cleanup.values())
        observation = {
            "product_e2e_status": report.get("status"),
            "run_lifecycle_after_teardown": report.get("run_lifecycle_after_teardown"),
            "activity_after_teardown": report.get("activity_after_teardown"),
            "run_id_stable_across_session": report.get("run_id_stable_across_session"),
            "process_alive_after_cancel": report.get("process_alive_after_cancel"),
            "archive_lag_seconds": report.get("archive_lag_seconds"),
            "machine_agent_restart": report.get("machine_agent_restart"),
            **required_cleanup,
            # run_product_e2e's own settled() is the postcondition: it raises
            # before returning if served activity fails to reach "quiescent"
            # at any of the first/remote/post-interrupt-recovery turn
            # boundaries it exercises. A clean, non-raising "passed" report
            # is proof that held at every boundary checked.
            "quiescent_at_every_observed_turn_boundary": native_ok,
        }
        assertions = {"activity_returns_to_quiescent_at_turn_boundary": native_ok}
        write_json(
            root / "cleanup-receipt.json",
            {
                "schema_version": 1,
                "artifact_kind": "cursor_turn_boundary_cleanup_receipt",
                "delegated_to": "cursor_helm_product_e2e.run_product_e2e finally block",
                "session_id": report.get("session_id"),
                "status": "pass" if all(required_cleanup.values()) else "fail",
                "orphan_count": 0 if no_orphans else None,
                "required_cleanup": required_cleanup,
                "diagnostics": {
                    "cursor_pid": cursor_pid,
                    "cursor_pgid": cursor_pid,
                    "provider_process_dead": provider_process_dead,
                    "provider_process_group_dead": provider_process_group_dead,
                },
            },
        )
        if shipper is not None:
            write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            shipper = None
        run.observation = observation
        run.assertions = assertions
        run.fields["session_id"] = report.get("session_id")
    finally:
        if shipper is not None:
            try:
                write_json(root / "transcript-shipper-receipt.json", shipper.stop())
            except Exception:  # noqa: BLE001 - best-effort final shipper stop
                pass
        for name, previous in (
            ("LONGHOUSE_CURSOR_BIN", previous_cursor_bin),
            ("LONGHOUSE_HOME", previous_longhouse_home),
        ):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        _bound_product_terminal_recording(root)
        run.fields["redacted_secret_files"] = secret_scan(root, list(qualification_secrets(dict(os.environ), args.agents_token)))


def run_turn_boundary(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(args.evidence_root, lambda run: _reach(run, args))


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout-secs", type=float, default=120.0)
    parser.add_argument("--max-archive-lag-secs", type=float, default=20.0)


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="direct_turn_boundary_result",
    failure_code="direct_turn_boundary_failed",
    needs=("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin"),
    binaries=(
        ("engine", "longhouse_engine_missing"),
        ("longhouse_cli", "longhouse_cli_missing"),
        ("provider_bin", "cursor_binary_missing"),
    ),
    runtime_host=True,
    add_arguments=_add_arguments,
)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_turn_boundary, argv)


if __name__ == "__main__":
    raise SystemExit(main())
