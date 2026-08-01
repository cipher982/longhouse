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


def test_harness_backed_profiles_fill_the_staged_release_diagonal() -> None:
    diagonal = render_diagonal_status(load_facts())
    for provider in {"codex", "claude", "opencode", "antigravity"}:
        line = next(line for line in diagonal.splitlines() if line.startswith(f"| {provider} |"))
        assert "runs — 23 scenarios" in line
    for provider in set(ALL_PROVIDERS) - {"codex", "claude", "opencode", "antigravity"}:
        line = next(line for line in diagonal.splitlines() if line.startswith(f"| {provider} |"))
        assert "never runs" in line


def test_generated_tables_artifact_is_not_stale() -> None:
    # Hatch Sol review, 2026-07-29: the prior two tests prove the render
    # functions behave correctly, but neither checks that what is *published*
    # matches them -- so a schema change altering a cell's text (a scenario
    # count, an orphaned-scenario list) would leave the checked-in table
    # silently stale while both tests kept passing. That's the exact failure
    # mode this generator exists to kill.
    #
    # Until 2026-07-31 the published copy was the tables embedded in
    # docs/specs/provider-factory-coherence.md. That spec is private now, so
    # the drift check splits in two: this half guards the generated artifact
    # in this repo, and control-plane's
    # test_provider_factory_status_tables_match_spec.py asserts the spec
    # embeds that artifact verbatim.
    artifact_path = default_repo_root() / "docs/generated/provider_factory_status_tables.md"
    artifact_text = artifact_path.read_text(encoding="utf-8")
    facts = load_facts()

    assert render_status_table(facts) in artifact_text, (
        "docs/generated/provider_factory_status_tables.md's status table is stale — "
        "run scripts/generate_provider_factory_plan.py --write"
    )
    assert render_diagonal_status(facts) in artifact_text, (
        "docs/generated/provider_factory_status_tables.md's diagonal table is stale — "
        "run scripts/generate_provider_factory_plan.py --write"
    )
