#!/usr/bin/env python3
"""Direct OpenCode serve/session/restart contract producer.

Wraps the already-implemented, already-tested no-token OpenCode server canary
(``zerg.qa.opencode_server_qualification`` / ``zerg.qa.provider_live_canary``)
in the native-producer CLI + result.json contract used by the trusted
qualification sandbox, so ``session.launch.helm``
(``serve_session_contract_preserved``) and ``session.reattach.helm``
(``process_restart_reattach_preserved``) get the same continuous
re-verification ``session.resume.helm`` already gets.

Both assertions share one scenario_id (``opencode_server_contract``) and one
real execution: ``opencode_server_qualification.opencode_server_contract_
oracle()`` already computes both outcomes from a single
``run_provider_live_canary()`` pass against the exact-binary contract (start
``opencode serve``, create/get a session, prove ``prompt_async`` no-reply
delivery and ``session.abort``, kill the process, restart it, and prove the
same provider session/thread survives -- see
``zerg.qa.provider_live_canary.run_opencode_live_canary``, read in full while
building this producer). This producer runs that pass once per invocation
via ``opencode_server_qualification._execute`` (the module's own tested
executor, not a re-implementation) and reports the specific assertion_id the
sandbox's synthetic ``--variant`` execution key names, while recording both
outcomes in ``assertions`` for completeness -- either invocation's result is
admissible for its own cell regardless of the other cell's outcome.

Evidence class is ``live_no_token``: this producer never sends a real prompt
to a model and needs no provider credential -- ``run_opencode_live_canary``'s
own module docstring is explicit about this ("No-token upstream managed-
provider canaries... Token-spending provider behavior belongs to the
explicit release-canary lane"). Process cleanup is the oracle's own
responsibility: ``run_opencode_live_canary`` has an unconditional
``finally: _stop_process_group(process)`` (read in full), so no orphan
``opencode serve`` process should survive a completed call regardless of
outcome; this producer also independently re-checks that no process with the
resolved provider binary's path and a ``serve`` argument remains, rather than
only trusting that internal guarantee.

NOTE on oracle_source/oracle_entrypoint: schemas/managed_providers.yml
declares ``oracle_source: server/zerg/qa/provider_release_semantic_oracles.py``
for both assertion ids. That 31-line file (read in full) is the "declared
postconditions" half of an already-established two-file pattern in this
codebase: it names which assertion ids belong to a scenario
(``assertions_for(scenario_id)``); the actual judgment executor lives in the
scenario's own qualification module (here, ``opencode_server_qualification.
opencode_server_contract_oracle``), exactly mirroring how the Resume
producers declare ``oracle_source=server/zerg/qa/provider_resume_oracles.py``
(assertion definitions) while ``implementation=`` points at the executor.
``oracle_entrypoint`` below therefore names ``assertions_for`` (the actual
function in the schema-declared file), not
``opencode_server_contract_oracle`` (which lives in ``implementation``'s
sibling module, imported and called directly below). This is a documented
mapping, not a gap like the sibling turn-boundary producer's.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from zerg.qa import opencode_server_qualification
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.services.provider_capability_proof import AssertionOutcome

REGISTRATION = ProducerRegistration(
    producer_id="opencode.server_contract.v1",
    producer_revision=1,
    scenario_id="opencode_server_contract",
    scenario_revision=1,
    # Neither assertion declares a "variant" key in the schema, so both
    # authored variants are None -- see the sibling turn-boundary producer's
    # REGISTRATION comment for why that is the correct value to match
    # _producer_supports_cell's exact-tuple comparison.
    assertion_cells=(
        ("serve_session_contract_preserved", None),  # type: ignore[arg-type]
        ("process_restart_reattach_preserved", None),  # type: ignore[arg-type]
    ),
    providers=("opencode",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_no_token",),
    observed_activity=(
        "opencode_server_started",
        "opencode_session_created",
        "opencode_session_get_matched",
        "opencode_prompt_async_no_reply_delivered",
        "opencode_session_aborted",
        "opencode_process_restart_reattach_succeeded",
        "opencode_attach_command_shape_verified",
    ),
    acquisition_methods=("staged_release", "observed_install"),
    # This scenario never spends a provider credential or a Runtime Host
    # control-plane credential (see module docstring); nothing to bind.
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "provider_live_canary_artifact",
        "server_contract_receipt",
        "cleanup_receipt",
    ),
    required_cleanup=(
        "opencode_server_process_exited",
        "no_orphan_provider_processes",
    ),
    implementation="server/zerg/qa/opencode_server_contract_producer.py",
    oracle_source="server/zerg/qa/provider_release_semantic_oracles.py",
    oracle_entrypoint="assertions_for",
    executable_module="zerg.qa.opencode_server_contract_producer",
    provider_artifact_required=True,
    executable=True,
)

_KNOWN_ASSERTION_IDS = tuple(assertion_id for assertion_id, _variant in REGISTRATION.assertion_cells)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _artifact_manifest(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "result.json"
    ]


def requested_assertion_id(variant: str) -> str:
    """Recover which assertion cell this invocation is proving.

    The sandbox does not pass ``--assertion-id`` for a non-generic producer
    (see ``provider_factory/assurance.py``'s ``execute_retained_plan``, read
    in full while building this producer): it only passes the synthetic
    execution-variant key ``zerg.qa.resume_assurance.execution_variant_key()``
    computes for any cell whose schema entry declares no explicit "variant".
    Both of this producer's cells are unvarianted, so ``--variant`` arrives
    as ``cell:opencode:<assertion_id>:opencode_server_contract``. Parse that
    exact shape rather than guessing; fail loudly on a mismatch instead of
    silently defaulting to one assertion, since a wrong guess here would
    misreport which capability was actually proven.
    """

    for assertion_id in _KNOWN_ASSERTION_IDS:
        if variant == f"cell:opencode:{assertion_id}:{REGISTRATION.scenario_id}":
            return assertion_id
    if variant in _KNOWN_ASSERTION_IDS:
        # Defensive fallback if a human ever registers an explicit variant
        # equal to the literal assertion_id instead of relying on the
        # synthetic execution-variant key.
        return variant
    raise RuntimeError(
        f"unrecognized --variant {variant!r} for scenario {REGISTRATION.scenario_id!r}; "
        f"expected the synthetic execution-variant key for one of {_KNOWN_ASSERTION_IDS}"
    )


def _no_orphan_opencode_server_processes(provider_bin: Path) -> bool:
    """Independently re-check for a leaked ``<provider_bin> serve`` process.

    ``run_opencode_live_canary``'s own ``finally: _stop_process_group(process)``
    (provider_live_canary.py) is the primary cleanup guarantee; this is a
    best-effort second check in the same spirit as other producers'
    independent process-death verification, not a replacement for it.
    """

    resolved = str(provider_bin.resolve())
    try:
        pids = [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return True
    for pid in pids:
        try:
            cmdline = (Path("/proc") / pid / "cmdline").read_bytes().split(b"\x00")
        except OSError:
            continue
        args = [part.decode("utf-8", "replace") for part in cmdline if part]
        if args and args[0] == resolved and "serve" in args:
            return False
    return True


def run_server_contract(args: argparse.Namespace) -> dict[str, Any]:
    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    provider_receipt = {
        "path": str(args.provider_bin),
        "sha256": _sha256(args.provider_bin),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    target_assertion_id = requested_assertion_id(args.variant)

    canary_evidence_root = root / "provider-live-canary"
    observation_payload, semantic_assertions, _secrets = opencode_server_qualification._execute(args.provider_bin, canary_evidence_root)
    _write_json(root / "provider-live-canary-artifact.json", observation_payload)

    outcomes = {item.assertion_id: item.outcome for item in semantic_assertions}
    assertions = {assertion_id: (outcome is AssertionOutcome.PASS) for assertion_id, outcome in outcomes.items()}
    if set(assertions) != set(_KNOWN_ASSERTION_IDS):
        raise RuntimeError(f"opencode_server_contract_oracle returned an unexpected assertion set: {sorted(assertions)}")

    process_clean = _no_orphan_opencode_server_processes(args.provider_bin)
    cleanup_receipt = {
        "opencode_server_process_exited": process_clean,
        "no_orphan_provider_processes": process_clean,
        "method": "run_opencode_live_canary_finally_stop_process_group_plus_independent_proc_scan",
    }
    _write_json(root / "cleanup-receipt.json", cleanup_receipt)

    server_contract_receipt = {
        "requested_assertion_id": target_assertion_id,
        "outcomes": {assertion_id: outcome.value for assertion_id, outcome in outcomes.items()},
        "canary_statuses": {
            name: (row or {}).get("status")
            for name, row in dict((observation_payload.get("provider_live_canary") or {}).get("canaries") or {}).items()
        },
    }
    _write_json(root / "server-contract-receipt.json", server_contract_receipt)

    observation = {
        "provider": "opencode",
        "requested_assertion_id": target_assertion_id,
        **observation_payload,
        **cleanup_receipt,
    }

    result: dict[str, Any] = {
        "schema_version": 1,
        "artifact_kind": "direct_server_contract_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "opencode",
        # Neither cell has an authored variant (see REGISTRATION comment);
        # this must equal the compiled command's "variant" field, not the
        # synthetic --variant execution key this process was invoked with.
        "variant": None,
        "scenario_id": REGISTRATION.scenario_id,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_no_token",
        "generated_at": _now(),
        "status": "pass" if assertions[target_assertion_id] else "fail",
        "observation": observation,
        "assertions": assertions,
        "invoked_with_execution_variant": args.variant,
        "provider_binary": provider_receipt,
        "artifact_manifest": [],
    }
    result["artifact_manifest"] = _artifact_manifest(root)
    _write_json(root / "result.json", result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", required=True, type=Path)
    # No authored variant exists for either cell; the sandbox still passes
    # the synthetic execution-variant key naming which assertion_id this
    # invocation must prove -- see requested_assertion_id().
    parser.add_argument("--variant", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--longhouse-cli", required=True, type=Path)
    parser.add_argument("--provider-bin", required=True, type=Path)
    parser.add_argument("--registration", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--registration"]:
        print(json.dumps(REGISTRATION.to_dict(), indent=2, sort_keys=True))
        return 0
    args = _parser().parse_args(arguments)
    # --engine and --repo-root are accepted (and required) to match this
    # producer registry's uniform native-producer calling convention (see
    # provider_factory/assurance.py's execute_retained_plan), but this
    # scenario's real work -- opencode_server_qualification._execute -- only
    # needs the provider binary; the Longhouse engine and repo checkout are
    # not otherwise exercised by this specific contract.
    if not args.provider_bin.is_file() or not os.access(args.provider_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "opencode_binary_missing"}))
        return 2
    result = run_server_contract(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
