from scripts.render_provider_factory_status import render_diagonal_status
from scripts.render_provider_factory_status import render_status_table
from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_factory_model import load_facts
from zerg.qa.repo_root import default_repo_root


def test_status_table_has_one_row_per_provider_per_wired_combination() -> None:
    table = render_status_table(load_facts())
    # 4 wired trigger/provenance combinations x 5 providers, plus 2 header lines.
    assert len(table.splitlines()) == 2 + 4 * len(ALL_PROVIDERS)
    assert "codex" in table
    assert "cursor" in table


def test_diagonal_is_empty_for_every_provider() -> None:
    # This is the epic's central, previously hand-asserted claim
    # ("The diagonal is empty because no code path executes a real upstream
    # binary through the universal scenario set") — generated from plan_run()
    # instead of typed prose. If any provider's diagonal cell ever starts
    # running, this test breaks and the doc claim must be re-examined, not
    # silently left stale.
    diagonal = render_diagonal_status(load_facts())
    for provider in ALL_PROVIDERS:
        line = next(line for line in diagonal.splitlines() if line.startswith(f"| {provider} |"))
        assert "never runs" in line


def test_generated_tables_embedded_in_the_spec_are_not_stale() -> None:
    # Hatch Sol review, 2026-07-29: the prior two tests prove the render
    # functions behave correctly, but neither ever reads
    # docs/specs/provider-factory-coherence.md -- so a schema change that
    # alters a cell's text (a scenario count, an orphaned-scenario list) would
    # leave the doc's checked-in table silently stale while both tests kept
    # passing. That's the exact failure mode this generator exists to kill.
    # This closes it directly: both tables are embedded in the doc verbatim
    # (see scripts/render_provider_factory_status.py's own regenerate
    # instructions), so exact substring containment is a real drift check,
    # not a fuzzy one.
    spec_path = default_repo_root() / "docs/specs/provider-factory-coherence.md"
    spec_text = spec_path.read_text(encoding="utf-8")
    facts = load_facts()

    assert render_status_table(facts) in spec_text, (
        "docs/specs/provider-factory-coherence.md's status table is stale — "
        "run scripts/render_provider_factory_status.py and update the doc"
    )
    assert render_diagonal_status(facts) in spec_text, (
        "docs/specs/provider-factory-coherence.md's diagonal table is stale — "
        "run scripts/render_provider_factory_status.py and update the doc"
    )
