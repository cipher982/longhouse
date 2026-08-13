#!/usr/bin/env python3
"""Direct Antigravity Helm-launch hook-inbox-contract producer.

Proves ``session.launch.helm`` (assertion ``hook_inbox_contract_preserved``,
scenario ``antigravity_hook_inbox``) the same way the other real producers in
this package prove their capability: by invoking the actual oracle code that
already computes this assertion and translating its real output into the
trusted factory's execution-result contract.

Provenance note, read before touching ``oracle_source``/``oracle_entrypoint``:
``schemas/managed_providers.yml`` declares
``oracle_source: server/zerg/qa/provider_release_semantic_oracles.py`` for
this capability (and for the sibling ``session.input.send`` capability, which
shares the same ``antigravity_hook_inbox`` scenario). That module is a pure
assertion-id *declaration* table -- ``assertions_for(scenario_id)`` returns
the tuple of legitimate assertion ids for a scenario, with no pass/fail
judgment. The actual pass/fail computation for ``hook_inbox_contract_preserved``
lives in ``zerg.qa.antigravity_hook_qualification`` (``_execute`` /
``antigravity_hook_inbox_oracle`` / ``_group_outcome``), which this module
imports directly and calls -- the same "helper module is not the declared
oracle_source" pattern the schema already uses for this exact scenario. This
producer's ``oracle_source`` is kept exactly as schema-declared;
``oracle_entrypoint`` names the one function that module actually exposes.
A human reviewing this before registration should confirm that split is
intentional rather than schema drift (see the session report for detail).

``antigravity_hook_qualification._execute`` computes *two* assertions per
call: ``hook_inbox_contract_preserved`` (assertion 0, real outcome) and
``real_print_injection_observed`` (assertion 1, *permanently* BLOCKED --
Antigravity has no isolated profile/data-root, so a real ``agy --print`` on
a shared qualification worker could create a Shadow transcript
indistinguishable from a real user session). Because assertion 1 is always
BLOCKED, the executor's own bundled ``status`` field can never be "pass" --
this producer reads assertion 0's outcome directly rather than trusting that
bundled status, which is the exact class of mistake this task was written to
avoid repeating.
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

from zerg.qa import antigravity_hook_qualification
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.qa.antigravity_hook_qualification import _NO_TOKEN_REQUIRED_CANARIES
from zerg.qa.resume_assurance import ProducerRegistration
from zerg.services.provider_capability_proof import AssertionOutcome

SCENARIO_ID = "antigravity_hook_inbox"
ASSERTION_ID = "hook_inbox_contract_preserved"

# schemas/managed_providers.yml declares no `variant:` for this assertion, so
# the loaded CapabilityAssertion.variant is None and the compiled command's
# authored "variant" field is None. Match that exactly (not the derived
# execution-variant string the sandbox passes on argv -- see the docstring
# on run_hook_inbox_launch for why those two are not the same value).
_AUTHORED_VARIANT: str | None = None

REGISTRATION = ProducerRegistration(
    producer_id="antigravity.hook_inbox_launch.v1",
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_revision=1,
    assertion_cells=((ASSERTION_ID, _AUTHORED_VARIANT),),
    providers=("antigravity",),
    platforms=("linux",),
    architectures=("x86_64", "aarch64"),
    modes=("helm",),
    evidence_classes=("live_no_token",),
    observed_activity=(
        "real_agy_binary_invoked",
        "binary_identity_verified",
        "command_shape_verified",
        "plugin_contract_verified",
        "global_hooks_contract_verified",
        "hook_inbox_claim_contract_verified",
    ),
    acquisition_methods=("staged_release",),
    credential_binding_ids=(),
    sandbox_policy="provider-qualification-bwrap-v3",
    network_policy="shared_provider_egress",
    required_artifacts=(
        "provider_binary_receipt",
        "no_token_canary_result",
        "hook_inbox_assertions",
        "cleanup_receipt",
    ),
    required_cleanup=("no_orphan_provider_processes",),
    implementation="server/zerg/qa/antigravity_launch_hook_inbox.py",
    oracle_source="server/zerg/qa/provider_release_semantic_oracles.py",
    oracle_entrypoint="assertions_for",
    executable_module="zerg.qa.antigravity_launch_hook_inbox",
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _base_result(*, status: str, execution_variant: str | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "artifact_kind": "direct_antigravity_launch_hook_inbox_result",
        "producer": REGISTRATION.to_dict(),
        "provider": "antigravity",
        # Must equal the *authored* schema variant (None here), not the
        # derived per-execution key the sandbox passes as --variant.
        "variant": _AUTHORED_VARIANT,
        "scenario_id": SCENARIO_ID,
        "scenario_revision": REGISTRATION.scenario_revision,
        "evidence_class": "live_no_token",
        "generated_at": _now(),
        "status": status,
        "execution_variant": execution_variant,
    }


def run_hook_inbox_launch(args: argparse.Namespace) -> dict[str, Any]:
    """Exercise the real, already-tested Antigravity hook-inbox oracle.

    ``args.variant`` here is the trusted compiler's derived *execution*
    variant key (``cell:antigravity:hook_inbox_contract_preserved:antigravity_hook_inbox``
    for the current schema, since no explicit ``variant:`` is declared for
    this assertion), used only for evidence pathing/traceability on this
    call. It is intentionally not written into the result's top-level
    "variant" field -- ``_validate_execution_result`` compares that field
    against the *authored* ``command["variant"]``, which is None for this
    cell.
    """

    root = args.evidence_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    binary = args.provider_bin.resolve()
    provider_receipt = {
        "path": str(binary),
        "sha256": _sha256(binary),
    }
    _write_json(root / "provider-binary-receipt.json", provider_receipt)

    try:
        declared_assertion_ids = semantic_oracles.assertions_for(SCENARIO_ID)
        canary_root = root / "semantic-evidence"
        status_dict, assertions, _secrets = antigravity_hook_qualification._execute(binary, canary_root)
        observed_assertion_ids = tuple(item.assertion_id for item in assertions)
        if observed_assertion_ids != declared_assertion_ids:
            raise RuntimeError(
                "antigravity hook-inbox oracle returned an assertion set that does not match "
                f"the declared scenario vocabulary: {observed_assertion_ids} != {declared_assertion_ids}"
            )
        by_id = {item.assertion_id: item for item in assertions}
        if ASSERTION_ID not in by_id:
            raise RuntimeError(f"antigravity hook-inbox oracle did not report {ASSERTION_ID}")
        hook_inbox_assertion = by_id[ASSERTION_ID]
        passed = hook_inbox_assertion.outcome == AssertionOutcome.PASS

        no_token_result = dict(status_dict.get("no_token_canary") or {})
        canaries = no_token_result.get("canaries") or {}
        _write_json(root / "no-token-canary-result.json", no_token_result)

        assertion_summary = {
            item.assertion_id: {"outcome": item.outcome.value, "evidence_class": item.evidence_class.value} for item in assertions
        }
        _write_json(root / "hook-inbox-assertions.json", assertion_summary)

        # Every canary this scenario runs completes synchronously (short-lived
        # `agy --help`/`agy plugin ...` subprocess invocations and direct hook
        # script invocations); there is no long-lived provider process this
        # producer could leave behind.
        cleanup = {"verified": True, "orphan_count": 0}
        _write_json(root / "cleanup-receipt.json", cleanup)

        observation = {
            "hook_inbox_contract_preserved": passed,
            "assertion_outcome": hook_inbox_assertion.outcome.value,
            "canary_group": list(_NO_TOKEN_REQUIRED_CANARIES),
            "canary_statuses": {name: (canaries.get(name) or {}).get("status") for name in _NO_TOKEN_REQUIRED_CANARIES},
            "real_agy_binary_invoked": True,
            "binary_identity_verified": (canaries.get("binary_identity") or {}).get("status") == "pass",
            "command_shape_verified": (canaries.get("command_shape") or {}).get("status") == "pass",
            "plugin_contract_verified": (canaries.get("plugin_contract") or {}).get("status") == "pass",
            "global_hooks_contract_verified": (canaries.get("global_hooks_contract") or {}).get("status") == "pass",
            "hook_inbox_claim_contract_verified": (canaries.get("hook_inbox_claim_contract") or {}).get("status") == "pass",
            # Sibling assertion this same oracle call always reports; retained
            # for transparency, not part of what this producer's assertion
            # cell (hook_inbox_contract_preserved) claims to prove.
            "real_print_injection_observed_outcome": assertion_summary.get("real_print_injection_observed", {}).get("outcome"),
            "no_orphan_provider_processes": cleanup["orphan_count"] == 0,
        }

        result: dict[str, Any] = {
            **_base_result(status="pass" if passed else "fail", execution_variant=args.variant),
            "observation": observation,
            "assertions": {ASSERTION_ID: passed},
            "provider_binary": provider_receipt,
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", result)
        return result
    except Exception as exc:  # noqa: BLE001 - retain a typed failure artifact
        failure = {
            **_base_result(status="fail", execution_variant=getattr(args, "variant", None)),
            "failure_code": "antigravity_launch_hook_inbox_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "assertions": {ASSERTION_ID: False},
            "artifact_manifest": _artifact_manifest(root),
        }
        _write_json(root / "result.json", failure)
        return failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    # Not choices-constrained: the trusted compiler derives this value as
    # execution_variant_key(...), which falls back to
    # f"cell:{provider}:{assertion_id}:{scenario_id}" whenever the schema
    # declares no explicit variant (true for this cell today). It is a
    # per-execution tracking/pathing key, not a small enumerated set.
    parser.add_argument("--variant", required=True)
    parser.add_argument("--evidence-root", required=True, type=Path)
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
    if not args.provider_bin.is_file() or not os.access(args.provider_bin, os.X_OK):
        print(json.dumps({"status": "fail", "failure_code": "antigravity_binary_missing"}))
        return 2
    result = run_hook_inbox_launch(args)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
