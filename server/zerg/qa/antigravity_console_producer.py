#!/usr/bin/env python3
"""Antigravity Console qualification through `agy acp`, against a scripted model.

The Console lifecycle itself (a turn, a resume, a mid-turn steer, an interrupt, the cleanup receipt) is the shared one in
``provider_console_lifecycle``. Antigravity gets its own producer because its cells run on the scripted-model evidence
class alone: the CI Gemini key is free tier, so a live-token cell would fail on quota, not on Longhouse, and a
registration binds credentials per provider or per evidence class, never both (antigravity-parity.md, step 6).
"""

from __future__ import annotations

import dataclasses
from typing import Any

from zerg.qa import provider_console_lifecycle as lifecycle
from zerg.qa.producer_shell import Producer
from zerg.qa.producer_shell import Run

PROVIDER = "antigravity"
SCENARIO_ID = f"{PROVIDER}_console_adapter_lifecycle"
ASSERTION_ID = lifecycle.ASSERTION_ID
SUPPORTED_VARIANT = lifecycle.SUPPORTED_VARIANT
STEER_VARIANT = lifecycle.STEER_VARIANTS[PROVIDER]
# The shared oracle judges the observation; this module only runs it.
console_lifecycle_assertions = lifecycle.console_lifecycle_assertions

REGISTRATION = dataclasses.replace(
    lifecycle.REGISTRATION,
    producer_id="antigravity.console_lifecycle.v1",
    # 1: the first Antigravity-only registration, split from provider.console_lifecycle.v1 revision 24.
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_ids=(SCENARIO_ID,),
    providers=(PROVIDER,),
    evidence_classes=("scripted_model",),
    credential_binding_ids=(),
    credential_binding_ids_by_provider={},
    credential_binding_ids_by_evidence_class={"scripted_model": ("runtime_host_control",)},
    required_artifacts_by_scenario={},
    implementation="server/zerg/qa/antigravity_console_producer.py",
    executable_module="zerg.qa.antigravity_console_producer",
)

PRODUCER = Producer(
    REGISTRATION,
    artifact_kind="provider_console_lifecycle_result",
    failure_code="provider_console_lifecycle_failed",
    needs=("provider", "variant", "evidence_root", "repo_root", "engine", "longhouse_cli", "provider_bin", "provider_version", "model"),
    binaries=(("engine", "engine_missing"), ("longhouse_cli", "longhouse_cli_missing"), ("provider_bin", "provider_bin_missing")),
    variants=(SUPPORTED_VARIANT, STEER_VARIANT),
    variant_required=False,
    negative_controls=(lifecycle.CONSOLE_STEER_FAULT,),
    refuse=lambda args: lifecycle._refuse(args),
)


def run_console_lifecycle(args: Any) -> dict[str, Any]:
    steer = args.variant == STEER_VARIANT

    def body(run: Run) -> None:
        reached = lifecycle._run_live(PROVIDER, SUPPORTED_VARIANT if steer else args.variant, args, run.root, steer=steer)
        run.observation = reached.pop("observation")
        run.assertions = reached.pop("assertions")
        run.fields.update(reached)

    return lifecycle.run_console_cell(
        PRODUCER,
        args,
        body,
        provider=PROVIDER,
        variant=None if steer else args.variant,
        scenario_id=SCENARIO_ID,
    )


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_console_lifecycle, argv)


if __name__ == "__main__":
    raise SystemExit(main())
