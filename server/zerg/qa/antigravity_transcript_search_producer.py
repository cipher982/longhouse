#!/usr/bin/env python3
"""Antigravity transcript ingest and search through `agy acp`, against a scripted model.

The harness is the shared one in ``transcript_search_producer``; Antigravity gets its own registration because its
cells run on the scripted-model evidence class alone (the CI Gemini key is free tier) and a registration binds
credentials per provider or per evidence class, never both (antigravity-parity.md, step 6).
"""

from __future__ import annotations

import argparse
import dataclasses
from typing import Any

from zerg.qa import transcript_search_producer as search
from zerg.qa.producer_shell import Producer

PROVIDER = "antigravity"
SCENARIO_ID = f"{PROVIDER}_transcript_search"
ASSERTION_ID = search.ASSERTION_ID

REGISTRATION = dataclasses.replace(
    search.REGISTRATION,
    producer_id="antigravity.transcript_search.v1",
    # 1: the first Antigravity-only registration, split from provider.transcript_search.v1 revision 8.
    producer_revision=1,
    scenario_id=SCENARIO_ID,
    scenario_ids=(SCENARIO_ID,),
    providers=(PROVIDER,),
    evidence_classes=("scripted_model",),
    credential_binding_ids=(),
    credential_binding_ids_by_provider={},
    credential_binding_ids_by_evidence_class={"scripted_model": ("runtime_host_control",)},
    required_artifacts_by_scenario={},
    implementation="server/zerg/qa/antigravity_transcript_search_producer.py",
    executable_module="zerg.qa.antigravity_transcript_search_producer",
)

PRODUCER = Producer(
    REGISTRATION,
    artifact_kind=search.PRODUCER.artifact_kind,
    failure_code=search.PRODUCER.failure_code,
    needs=search.PRODUCER.needs,
    binaries=search.PRODUCER.binaries,
    variant_required=False,
    negative_controls=search.NEGATIVE_CONTROLS,
)


def run_transcript_search(args: argparse.Namespace) -> dict[str, Any]:
    return PRODUCER.execute(
        args.evidence_root,
        lambda run: search._reach(run, PROVIDER, args),
        failure_status="inconclusive" if args.negative_control else "fail",
        provider=PROVIDER,
        scenario_id=SCENARIO_ID,
    )


def main(argv: list[str] | None = None) -> int:
    return PRODUCER.main(run_transcript_search, argv)


if __name__ == "__main__":
    raise SystemExit(main())
