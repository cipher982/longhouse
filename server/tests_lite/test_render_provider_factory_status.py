from scripts.render_provider_factory_status import render_diagonal_status
from scripts.render_provider_factory_status import render_status_table
from zerg.qa.provider_factory_model import ALL_PROVIDERS
from zerg.qa.provider_factory_model import load_facts


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
