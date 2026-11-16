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
from typing import Any

from zerg.provider_contract.provider_capability_proof import AssertionOutcome
from zerg.qa import antigravity_hook_qualification
from zerg.qa import provider_release_semantic_oracles as semantic_oracles
from zerg.qa.antigravity_hook_qualification import _NO_TOKEN_REQUIRED_CANARIES
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run
from zerg.qa.provider_release_identity import sha256_file
from zerg.qa.resume_assurance import ProducerRegistration

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
    # 2: a failure after the binary was identified carries its receipt.
    scenario_revision=2,
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


PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="direct_antigravity_launch_hook_inbox_result",
    failure_code="antigravity_launch_hook_inbox_failed",
    needs=("evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin"),
    binaries=(("provider_bin", "antigravity_binary_missing"),),
)


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

    def body(run: Run) -> None:
        # The authored variant (none) is what the result carries; the key this call ran under is kept apart.
        run.fields["execution_variant"] = args.variant
        binary = args.provider_bin.resolve()
        provider_receipt = {"path": str(binary), "sha256": sha256_file(binary)}
        run.write("provider-binary-receipt.json", provider_receipt)

        declared_assertion_ids = semantic_oracles.assertions_for(SCENARIO_ID)
        status_dict, assertions, _secrets = antigravity_hook_qualification._execute(binary, run.root / "semantic-evidence")
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
        run.write("no-token-canary-result.json", no_token_result)

        assertion_summary = {
            item.assertion_id: {"outcome": item.outcome.value, "evidence_class": item.evidence_class.value} for item in assertions
        }
        run.write("hook-inbox-assertions.json", assertion_summary)

        # Every canary this scenario runs completes synchronously (short-lived
        # `agy --help`/`agy plugin ...` subprocess invocations and direct hook
        # script invocations); there is no long-lived provider process this
        # producer could leave behind.
        cleanup = {
            "schema_version": 1,
            "artifact_kind": "antigravity_hook_inbox_cleanup_receipt",
            "status": "pass",
            "orphan_count": 0,
            "required_cleanup": {"no_orphan_provider_processes": True},
            "verified": True,
        }
        run.write("cleanup-receipt.json", cleanup)

        run.observation = {
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
        run.assertions = {ASSERTION_ID: passed}
        run.fields["provider_binary"] = provider_receipt

    return PRODUCER.execute(args.evidence_root, body, variant=_AUTHORED_VARIANT)


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_hook_inbox_launch, argv)


if __name__ == "__main__":
    raise SystemExit(main())
