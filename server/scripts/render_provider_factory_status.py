#!/usr/bin/env python3
"""Render the provider factory's real status as a markdown table.

docs/specs/provider-factory-coherence.md, Phase 5: "this document's status
tables become generated." Every row below comes from provider_factory_model's
plan_run() against the live schema and registry constants, not hand-typed
prose — the exact gap the epic exists to fix ("nothing derives from the
authority").

Usage:
    uv run --directory server python scripts/render_provider_factory_status.py

Prints a markdown table to stdout. CI/doc-drift checking reads this same
function directly (see tests_lite/test_render_provider_factory_status.py) so
there is one code path for "what does the factory actually do," not a
generator and a separately-maintained assertion of its output.
"""

from __future__ import annotations

from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_factory_model import BuildProvenance
from zerg.qa.provider_factory_model import ProviderFactoryFacts
from zerg.qa.provider_factory_model import Trigger
from zerg.qa.provider_factory_model import load_facts
from zerg.qa.provider_factory_model import plan_run

# The trigger/provenance pairs actually wired to a real runner today
# (release_poll on clifford, push + weekly_cron on GitHub Actions, manual for
# humans). Every other combination in the 5 x 3 x 4 cross product is either
# nonsensical (e.g. push against staged_release) or simply never invoked by
# any workflow — plan_run() still answers them, but rendering all sixty cells
# would bury the four that matter behind noise no reader asked for.
_WIRED_COMBINATIONS: tuple[tuple[str, str], ...] = (
    (Trigger.RELEASE_POLL, BuildProvenance.STAGED_RELEASE),
    (Trigger.PUSH, BuildProvenance.GENERATED_FAKE),
    (Trigger.WEEKLY_CRON, BuildProvenance.GENERATED_FAKE),
    (Trigger.MANUAL, BuildProvenance.OBSERVED_INSTALL),
)

# The diagonal this epic is about: a real upstream binary (staged_release)
# driven through the full scenario set (weekly_cron's harness_scenarios).
_DIAGONAL = (Trigger.WEEKLY_CRON, BuildProvenance.STAGED_RELEASE)


def _cell_detail(cell) -> str:
    if cell.status == "never_run":
        return f"never runs — {cell.reason}"
    count = len(cell.scenario_ids) or len(cell.harness_scenarios)
    unit = "scenario" if count == 1 else "scenarios"
    return f"runs — {count} {unit}"


def render_status_table(facts: ProviderFactoryFacts) -> str:
    lines = ["| Provider | Trigger | Build provenance | Status |", "|---|---|---|---|"]
    for provider in ALL_PROVIDERS:
        for trigger, provenance in _WIRED_COMBINATIONS:
            cell = plan_run(facts, provider, provenance, trigger)
            lines.append(f"| {provider} | {trigger} | {provenance} | {_cell_detail(cell)} |")
    return "\n".join(lines)


def render_diagonal_status(facts: ProviderFactoryFacts) -> str:
    trigger, provenance = _DIAGONAL
    lines = ["| Provider | The diagonal (real binary x full scenario set) |", "|---|---|"]
    for provider in ALL_PROVIDERS:
        cell = plan_run(facts, provider, provenance, trigger)
        lines.append(f"| {provider} | {_cell_detail(cell)} |")
    return "\n".join(lines)


def main() -> None:
    facts = load_facts()
    print(render_status_table(facts))
    print()
    print(render_diagonal_status(facts))


if __name__ == "__main__":
    main()
