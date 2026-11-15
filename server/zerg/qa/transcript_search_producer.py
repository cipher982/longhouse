#!/usr/bin/env python3
"""Live proof that a provider's own output becomes served-searchable.

One producer serves every provider with an admitted Console turn (Claude,
Codex, Cursor, OpenCode, Pi, OMP). It enters through the Runtime Host HTTP API
exactly like ``provider_console_lifecycle``: a disposable real Machine Agent
receives ``session.turn.start``, launches the staged stock provider binary,
and ships the provider's native transcript. The verdict comes from
:mod:`zerg.qa.search_ingest_oracle` -- a marker only the model's output
contains must come back from served lexical search on the exact session.

``--negative-control ingest_redact_marker`` runs against a Machine Agent built
with ``qa-fault-injection``; the agent blanks the marker out of every render
record before shipping. The run passes only when the fault receipt proves the
fault fired, the provider still produced the marker, and search missed it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from zerg.qa import provider_console_lifecycle as console
from zerg.qa import search_ingest_oracle as oracle
from zerg.qa.codex_auth import login_with_api_key
from zerg.qa.live_session_toolkit import RUNTIME_AGENTS_TOKEN_ENV
from zerg.qa.live_session_toolkit import RUNTIME_API_URL_ENV
from zerg.qa.live_session_toolkit import TranscriptShipper
from zerg.qa.live_session_toolkit import isolated_provider_home
from zerg.qa.live_session_toolkit import require_disposable_runtime
from zerg.qa.live_session_toolkit import retire_qualification_session
from zerg.qa.live_session_toolkit import start_transcript_shipper
from zerg.qa.live_session_toolkit import write_json
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.resume_assurance import ProducerRegistration

PROVIDERS = ("codex", "claude", "opencode", "cursor", "pi", "omp", "antigravity")
ASSERTION_ID = "transcript_ingested_searchable"
NEGATIVE_CONTROLS = (oracle.FAULT_NAME,)
SCENARIO_IDS = tuple(f"{provider}_transcript_search" for provider in PROVIDERS)
# A model occasionally mangles the join; a fresh marker on a fresh turn keeps
# that provider flake apart from the ingest verdict without weakening it.
_MARKER_ATTEMPTS = 2

REGISTRATION = ProducerRegistration(
    producer_id="provider.transcript_search.v1",
    producer_revision=1,
    scenario_id=SCENARIO_IDS[0],
    scenario_ids=SCENARIO_IDS,
    # 4: a run that raised mid-session carries what its wait was waiting on.
    # 5: a process that has exited counts as stopped before its parent collects it (live_kit.pid_dead).
    # 6: a control whose run raised before a verdict says so (producer_shell): whether its fault had fired, and that
    # its preconditions were not evaluated.
    # 7: Antigravity joins the providers.
    scenario_revision=7,
    assertion_cells=((ASSERTION_ID, None),),
    providers=PROVIDERS,
    platforms=("linux", "darwin"),
    architectures=("x86_64", "aarch64"),
    modes=("console",),
    evidence_classes=("live_token",),
    observed_activity=(
        "provider_produced_joined_marker",
        "machine_agent_flushed_native_transcript",
        "served_lexical_search_returned_exact_session",
        "match_on_assistant_event",
        "canary_session_retired",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    credential_binding_ids=(),
    credential_binding_ids_by_provider={provider: (f"{provider}_provider_token", "runtime_host_control") for provider in PROVIDERS},
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "transcript_flush_receipt",
        "search_probe_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=("no_orphan_provider_processes", "canary_session_hidden"),
    implementation="server/zerg/qa/transcript_search_producer.py",
    oracle_source="server/zerg/qa/search_ingest_oracle.py",
    oracle_entrypoint="search_ingest_verdict",
    executable_module="zerg.qa.transcript_search_producer",
    provider_artifact_required=True,
    subject_kind="provider_release",
)


def _fault_receipts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _flush_ok(receipt: dict[str, Any]) -> bool:
    return (
        receipt.get("status") == "pass"
        and receipt.get("exit_code") == 0
        and receipt.get("daemon_paused") is True
        and receipt.get("daemon_restarted") is True
    )


def _search_turn(
    *,
    provider: str,
    args: argparse.Namespace,
    api_url: str,
    token: str,
    session_id: str,
    thread_id: str,
    longhouse_home: Path,
    shipper: TranscriptShipper,
    marker: dict[str, str],
    claims: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = oracle.search_prompt(marker)
    request_id = f"transcript-search-{uuid4()}"
    started = console._start_turn(api_url=api_url, token=token, session_id=session_id, message=prompt, request_id=request_id)
    run_id = str(started["run_id"])
    claim = console._wait_claim(console._claim_path(longhouse_home, run_id), states=frozenset({"terminal", "failed"}))
    claims.append(claim)
    if claim.get("state") != "terminal":
        raise RuntimeError(f"search Console turn did not complete: {claim.get('state')}")
    console._require_run_completed(provider, claim, turn="search", marker=marker["marker"])
    mismatches = console.turn_identity_mismatches(claim, provider=provider, session_id=session_id, thread_id=thread_id, run_id=run_id)
    if mismatches:
        raise RuntimeError(f"search Console turn lost its exact session identity: {json.dumps(mismatches, sort_keys=True, default=str)}")
    if not console._claim_uses_provider_binary(claim, args.provider_bin):
        raise RuntimeError("search Console turn did not launch the staged provider binary")
    console._wait_turn_terminal(
        api_url=api_url,
        token=token,
        session_id=session_id,
        message=prompt,
        request_id=request_id,
        turn_id=str(started["turn_id"]),
        run_id=run_id,
    )
    evidence = console._claim_output_evidence(provider, claim, marker["marker"]) or {}
    return {
        "marker": marker["marker"],
        "prompt": prompt,
        "run_id": run_id,
        "provider_marker_count": evidence.get("provider_response_marker_count"),
        "provider_response_excerpt": evidence.get("provider_response_excerpt"),
    }


def _reach(run: Run, provider: str, args: argparse.Namespace) -> None:
    root = run.root
    home = isolated_provider_home()
    environment = console._provider_environment(provider, args, home)
    fault_receipt_path = root / "qa-fault-receipt.jsonl"
    marker = oracle.new_marker()
    if args.negative_control:
        environment["LONGHOUSE_QA_FAULT"] = args.negative_control
        environment["LONGHOUSE_QA_FAULT_MARKER"] = marker["marker"]
        environment["LONGHOUSE_QA_FAULT_RECEIPT"] = str(fault_receipt_path)
    if provider == "codex":
        auth_receipt = login_with_api_key(
            args.provider_bin, api_key=str(environment.get("CODEX_API_KEY") or ""), environment=environment, cwd=home
        )
        environment.pop("CODEX_API_KEY", None)
        environment.pop("OPENAI_API_KEY", None)
        write_json(root / "provider-auth-receipt.json", auth_receipt)
    if provider == "claude":
        console._configure_claude_hook(args, environment)

    api_url = str(os.environ.get(RUNTIME_API_URL_ENV) or "").strip().rstrip("/")
    token = str(os.environ.get(RUNTIME_AGENTS_TOKEN_ENV) or "").strip()
    require_disposable_runtime(api_url)
    observed_version, raw_version = console._probe_version(provider, args.provider_bin)
    if observed_version != args.provider_version:
        raise RuntimeError(f"{provider} staged release version mismatch: expected {args.provider_version}, observed {observed_version}")
    binary_receipt = {
        "provider": provider,
        "path": str(args.provider_bin),
        "sha256": console._sha256_file(args.provider_bin),
        "version": observed_version,
        "raw_version_output": raw_version,
    }
    write_json(root / "provider-binary-receipt.json", binary_receipt)
    run.fields["provider_binary"] = binary_receipt

    _runtime_root, _unused, workspace, longhouse_home = console._console_runtime_paths(home)
    workspace.mkdir(mode=0o700, parents=True)
    if provider == "cursor":
        console.subprocess.run(
            ["git", "init", "--quiet"],
            cwd=workspace,
            env={**environment, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
            capture_output=True,
            timeout=30,
            check=True,
        )
    args.api_url = api_url
    args.agents_token = token
    claims: list[dict[str, Any]] = []
    shipper: TranscriptShipper | None = None
    session_id: str | None = None
    observation: dict[str, Any] = {"provider": provider}
    failure: BaseException | None = None
    try:
        shipper = start_transcript_shipper(
            provider, args, home=home, environment=environment, evidence_root=root / "shipper", longhouse_home=longhouse_home
        )
        longhouse_home = Path(environment["LONGHOUSE_HOME"])
        created = console._create_session(
            api_url=api_url,
            token=token,
            provider=provider,
            device_id=str(shipper.receipt["machine_name"]),
            cwd=workspace,
            model=args.model,
        )
        session_id = str(created["session_id"])
        observation["session_id"] = session_id
        attempts: list[dict[str, Any]] = []
        for attempt in range(_MARKER_ATTEMPTS):
            if attempt:
                if args.negative_control:
                    # The fault is bound to one marker at Machine Agent start.
                    break
                marker = oracle.new_marker()
            turn = _search_turn(
                provider=provider,
                args=args,
                api_url=api_url,
                token=token,
                session_id=session_id,
                thread_id=str(created["thread_id"]),
                longhouse_home=longhouse_home,
                shipper=shipper,
                marker=marker,
                claims=claims,
            )
            attempts.append(turn)
            count = turn.get("provider_marker_count")
            if isinstance(count, int) and count >= 1:
                break
        observation.update(attempts[-1])
        observation["marker_attempts"] = len(attempts)
        flush = console._retain_flush_diagnostics(shipper.flush("transcript-search"))
        write_json(root / "transcript-flush-receipt.json", flush)
        observation["flush_ok"] = _flush_ok(flush)
        search = oracle.probe_served_search(api_url, token, observation["marker"], expected_session_id=session_id)
        observation["search"] = search
        write_json(root / "search-probe-receipt.json", search)
    except Exception as exc:  # noqa: BLE001 - retained as a typed failure
        failure = exc
        # What each turn's claim actually carried, so a lost identity can be
        # read as a product regression or a harness gap from the evidence.
        observation["claims"] = [console.claim_identity_summary(claim) for claim in claims]
    finally:
        # A completed Console turn has already released its run; terminate only
        # a run a failure may have left owning a provider.
        termination = console._terminate_live_qualification_session(api_url, token, session_id) if failure is not None else None
        console._force_cleanup(claims)
        processes_dead = console._wait_owned_processes_dead(claims) if claims else True
        shipper_stop: dict[str, Any] | None = None
        if shipper is not None:
            try:
                shipper_stop = dict(shipper.stop())
            except Exception as exc:  # noqa: BLE001 - cleanup reports, never raises
                shipper_stop = {"status": "fail", "error": f"{type(exc).__name__}: {exc}"}
        retirement = (
            retire_qualification_session(api_url, token, session_id, provider=provider, project=f"provider-console-{provider}")
            if session_id
            else {"status": "fail", "error": "session_id_unavailable"}
        )
        cleanup = {
            "schema_version": 1,
            "artifact_kind": "transcript_search_cleanup_receipt",
            "session_id": session_id,
            "termination_dispatch": termination,
            "owned_processes": console._owned_process_evidence(claims),
            "shipper_stop": shipper_stop,
            "session_retirement": retirement,
            "required_cleanup": {
                "no_orphan_provider_processes": processes_dead,
                "canary_session_hidden": (
                    retirement.get("status") == "pass"
                    and retirement.get("archived") is True
                    and retirement.get("hidden") is True
                    and retirement.get("present_in_served_inventory") is False
                ),
            },
        }
        cleanup["orphan_count"] = 0 if processes_dead else len(claims)
        cleanup["status"] = "pass" if all(cleanup["required_cleanup"].values()) else "fail"
        write_json(root / "cleanup-receipt.json", cleanup)

    run.observation = observation
    verdict = oracle.search_ingest_verdict(observation)
    cleanup_ok = cleanup["status"] == "pass"
    if args.negative_control:
        # A control's result is the control's verdict, not an assertion's: a
        # failed run under the fault is inconclusive, and says why.
        control = oracle.negative_control_verdict(observation, fault_receipts=_fault_receipts(fault_receipt_path))
        if failure is not None:
            control["status"] = "inconclusive"
            run.fields.update(failure_code=PRODUCER.failure_code, error=f"{type(failure).__name__}: {failure}")
        run.control(control, status=control["status"] if cleanup_ok else "inconclusive")
        return
    run.fields["search_verdict"] = verdict
    if failure is not None:
        # The harness raised, so the run never finished: a search verdict over
        # its partial observation is not one. With no assertion reached, the
        # typed failure keeps that observation as evidence instead of a False.
        raise failure
    run.assertions = {ASSERTION_ID: verdict["passed"] and cleanup_ok}
    if not verdict["passed"]:
        run.fields["failure_code"] = verdict["failure_code"]


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="provider_transcript_search_result",
    failure_code="transcript_search_harness_failed",
    needs=("provider", "evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "provider_version", "model"),
    binaries=(("engine", "engine_missing"), ("longhouse_cli", "longhouse_cli_missing"), ("provider_bin", "provider_bin_missing")),
    variant_required=False,
    negative_controls=NEGATIVE_CONTROLS,
)


def run_transcript_search(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(
        args.evidence_root,
        lambda run: _reach(run, args.provider, args),
        failure_status="inconclusive" if args.negative_control else "fail",
        provider=args.provider,
        scenario_id=f"{args.provider}_transcript_search",
    )


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_transcript_search, argv)


if __name__ == "__main__":
    raise SystemExit(main())
