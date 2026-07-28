#!/usr/bin/env python3
"""Generate the provider factory plan matrix.

`load_facts()` + `plan_run(facts, provider, build_provenance, trigger)`
(server/zerg/qa/provider_factory_model.py) over every
(provider, build_provenance, trigger) cell. See
docs/specs/provider-factory-coherence.md's "Phase 1 model" section: this is
the "generated plan matrix" the Target architecture section calls for, in the
reduced form Phase 1 asks for — it reproduces current execution exactly and
renders never-run cells explicitly, but does not yet drive any real
execution. Phase 2 wires a version of this into control-plane.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "docs" / "generated" / "provider_factory_plan.json"

sys.path.insert(0, str(ROOT / "server"))

from zerg.qa.provider_factory_model import ALL_PROVIDERS  # noqa: E402
from zerg.qa.provider_factory_model import BuildProvenance  # noqa: E402
from zerg.qa.provider_factory_model import Trigger  # noqa: E402
from zerg.qa.provider_factory_model import load_facts  # noqa: E402
from zerg.qa.provider_factory_model import plan_run  # noqa: E402


def build_matrix() -> dict:
    facts = load_facts()
    cells = []
    for provider in ALL_PROVIDERS:
        for provenance in BuildProvenance:
            for trigger in Trigger:
                cell = plan_run(facts, provider, provenance, trigger)
                cells.append(asdict(cell))
    cells.sort(key=lambda c: (c["provider"], c["build_provenance"], c["trigger"]))
    return {
        "description": (
            "plan_run(provider, build_provenance, trigger) over every cell. "
            "'never_run' is a first-class result, not an omission — see "
            "docs/specs/provider-factory-coherence.md."
        ),
        "providers": list(ALL_PROVIDERS),
        "build_provenances": [p.value for p in BuildProvenance],
        "triggers": [t.value for t in Trigger],
        "never_run_count": sum(1 for c in cells if c["status"] == "never_run"),
        "runs_count": sum(1 for c in cells if c["status"] == "runs"),
        "cells": cells,
    }


def render(matrix: dict) -> str:
    return json.dumps(matrix, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Write the generated plan matrix artifact.")
    parser.add_argument(
        "--check", action="store_true", help="Fail if the generated plan matrix differs from the checked-in artifact."
    )
    args = parser.parse_args()

    rendered = render(build_matrix())
    current = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else ""

    if args.check:
        if rendered != current:
            print(
                f"{OUTPUT_PATH} is out of date; run scripts/generate_provider_factory_plan.py --write",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.write:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(rendered, encoding="utf-8")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
