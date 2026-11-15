"""The mutation edits still land on the current source, exactly once each (a stale mutation would silently test nothing)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from zerg.qa import terminal_truth_kinds as kinds
from zerg.qa import terminal_truth_mutations as mutations
from zerg.qa.terminal_truth_kinds import WOKEN_STATE_CHECK
from zerg.qa.terminal_truth_mutations import MUTATIONS


@pytest.mark.parametrize("mutation", [m for m in MUTATIONS if m.side == "server" or m.asset is not None], ids=lambda m: m.name)
def test_each_source_edit_mutation_applies_exactly_once_and_changes_the_file(mutation) -> None:
    """The server edits and the embedded asset edits. A Rust engine defect is a switch in the source instead (below)."""

    path = mutations.REPO_ROOT / mutation.file
    original = path.read_text()
    mutated = mutations.mutated_text(mutation, original)
    assert mutated != original
    assert mutation.old not in mutated or mutation.old in mutation.new


def test_every_defect_of_2026_11_11_has_a_mutation_and_a_named_stage() -> None:
    assert {m.name for m in MUTATIONS} == {
        "pi-helm-text-forward",
        "claude-helm-text-forward",
        "opencode-host-publish",
        "omp-session-dir-rule",
        "antigravity-message-chunk-arm",
        "antigravity-helm-reader",
        "host-live-item-drop",
        "host-thinking-drop",
        "host-call-ordered-against-reply",
        "heartbeat-state-change-not-announced",
        "claude-helm-thinking-forward",
        "claude-helm-thinking-as-reply",
        "claude-helm-tool-forward",
        "claude-helm-subagent-forward",
        "antigravity-helm-thinking-forward",
        "antigravity-helm-thinking-as-reply",
        "antigravity-helm-tool-forward",
        "omp-helm-thinking-forward",
        "omp-helm-thinking-as-reply",
        "omp-helm-tool-forward",
        "pi-helm-thinking-forward",
        "pi-helm-thinking-as-reply",
        "pi-helm-tool-forward",
        "omp-console-thinking-forward",
        "omp-console-thinking-as-reply",
        "pi-console-thinking-forward",
        "pi-console-thinking-as-reply",
        "codex-helm-thinking-forward",
        "codex-helm-thinking-as-reply",
        "codex-helm-tool-forward",
        "codex-console-tool-name",
        "codex-helm-subagent-forward",
        "codex-helm-subagent-as-tool",
        "codex-console-subagent-forward",
        "opencode-helm-thinking-forward",
        "opencode-helm-thinking-as-reply",
        "opencode-helm-tool-forward",
        "opencode-helm-subagent-as-tool",
        "opencode-console-thinking-forward",
        "opencode-console-thinking-as-reply",
        "opencode-console-tool-output-forward",
    }
    assert all(m.stages and m.cell.count("/") == 1 for m in MUTATIONS)


def test_a_row_that_is_not_ok_says_which_cell_went_wrong_and_why() -> None:
    baseline = {"check": "mutation x", "expected_stage": "kind", "baseline": "fail", "baseline_stage": "preflight",
                "baseline_reason": "the provider\nnever became ready", "observed": "not run", "ok": False}  # fmt: skip
    assert mutations.why_not(baseline) == (
        "the unmutated cell came out fail at preflight, so the mutation cannot be judged: the provider never became ready"
    )
    mutated = {"check": "mutation x", "expected_stage": "kind", "baseline": "pass", "observed_outcome": "fail",
               "observed_stage": "state_shown", "reason": "woke nobody", "ok": False}  # fmt: skip
    assert mutations.why_not(mutated) == "expected a failure at kind, saw fail at state_shown: woke nobody"
    half_speed = {"check": "half-speed fake model", "expected_stage": "(pass)", "observed_outcome": "fail",
                  "observed_stage": "client_received", "reason": "", "ok": False}  # fmt: skip
    assert mutations.why_not(half_speed) == "expected a pass, saw fail at client_received: no reason recorded"


def test_a_reply_cell_that_fails_unmutated_keeps_where_each_marker_was_late(monkeypatch, tmp_path) -> None:
    """omp/helm, alone on its own host, delivered 1.3 s and 1.9 s late on the runner (2026-11-13) and the row kept only the median."""

    import json

    from zerg.qa.terminal_truth_verdicts import CellResult

    reply_mutation = next(m for m in MUTATIONS if m.name == "omp-session-dir-rule")
    monkeypatch.setattr(mutations, "SCRATCH", tmp_path / "tt-mut")
    monkeypatch.setattr(mutations.tt, "load_expectations", lambda: {})
    late = CellResult(
        provider="omp", mode="helm", status="measured", outcome="fail", first_failed_stage="stream_delivered",
        reason="stream_delivered: median_delay_within_budget: median 1.861 s, budget 1.0 s",
        stages={"preflight": {"ok": True, "reason": ""}, "stream_delivered": {"ok": False, "reason": "median 1.861 s, budget 1.0 s"}},
        pass_shape="cold", stream_stage_s={"TT0000": {"paint": 1.5, "engine_stamp": 1.4, "host_publish": 3.3, "client_receive": 3.4}},
    )  # fmt: skip
    monkeypatch.setattr(mutations.tt, "run_suite", lambda *a, **k: [late])
    out = tmp_path / "rows.json"
    args = mutations.argparse.Namespace(
        engine=tmp_path / "engine", longhouse_cli=tmp_path / "cli", fault_engine=tmp_path / "engine", fault_longhouse_cli=tmp_path / "cli", only=[reply_mutation.name],
        tokens=100, rate=20.0, out=out,
    )  # fmt: skip

    assert mutations.run_mutations(args) == 1

    (row,) = json.loads(out.read_text())
    assert row["ok"] is False and row["baseline_stage"] == "stream_delivered"
    assert row["evidence"]["stream_stage_s"]["TT0000"]["host_publish"] == 3.3 and row["evidence"]["pass_shape"] == "cold"
    assert row["evidence"]["stage_reasons"] == {"stream_delivered": "median 1.861 s, budget 1.0 s"}


def test_a_failing_unmutated_cell_is_reported_with_its_reason_and_its_mutation_is_not_run(monkeypatch, tmp_path, capsys) -> None:
    """On the Linux runner 7 unmutated cells failed (2026-11-12) and the log said only "fail": 14 minutes of rebuilds, no reason."""

    import json

    engine_mutation = next(m for m in MUTATIONS if m.side == "engine" and m.scenario)
    monkeypatch.setattr(mutations, "SCRATCH", tmp_path / "tt-mut")
    monkeypatch.setattr(mutations.tt, "load_expectations", lambda: {})

    failed = {"outcome": "fail", "reason": "preflight: the provider never became ready", "checks": [{"name": "preflight", "ok": False}]}
    calls: list[dict] = []
    monkeypatch.setattr(mutations, "run_kinds_suite", lambda *a, **k: calls.append(k) or [failed])
    out = tmp_path / "rows.json"
    args = mutations.argparse.Namespace(
        engine=tmp_path / "engine", longhouse_cli=tmp_path / "cli", fault_engine=tmp_path / "engine", fault_longhouse_cli=tmp_path / "cli", only=[engine_mutation.name],
        tokens=100, rate=20.0, out=out,
    )  # fmt: skip

    assert mutations.run_mutations(args) == 1
    assert len(calls) == 2, "the mutated cell ran for a row that had already failed"  # the unmutated cell and its one more try

    (row,) = json.loads(out.read_text())
    assert row["baseline"] == "fail" and row["observed"] == "not run" and row["ok"] is False
    assert row["baseline_retried"] is True and row["baseline_first_failure"].startswith("preflight: ")  # it got one more try
    assert row["evidence"]["checks"] == failed["checks"]
    assert row["baseline_reason"] == "preflight: the provider never became ready"
    printed = capsys.readouterr().out
    assert f"NO mutation {engine_mutation.name}: the unmutated cell came out fail at preflight" in printed
    assert "the provider never became ready" in printed


def _run_one_engine_mutation(monkeypatch, tmp_path, results, expectation):
    """Run one live-kinds engine mutation with the cells replaced; returns (exit status, rows, calls made)."""

    import json

    mutation = next(m for m in MUTATIONS if m.name == "codex-helm-thinking-as-reply")
    scratch = tmp_path / "tt-mut"
    monkeypatch.setattr(mutations, "SCRATCH", scratch)
    monkeypatch.setattr(mutations.tt, "load_expectations", lambda: {})
    queue = list(results)
    monkeypatch.setattr(mutations, "run_kinds_suite", lambda *a, **k: [queue.pop(0)])
    monkeypatch.setattr(kinds, "kinds_expectation", expectation)
    out = tmp_path / "rows.json"
    args = mutations.argparse.Namespace(
        engine=tmp_path / "engine", longhouse_cli=tmp_path / "cli", fault_engine=tmp_path / "engine", fault_longhouse_cli=tmp_path / "cli", only=[mutation.name],
        tokens=100, rate=20.0, out=out,
    )  # fmt: skip
    status = mutations.run_mutations(args)
    return status, json.loads(out.read_text()), len(results) - len(queue)


_WOKEN_ONLY = {
    "outcome": "fail",
    "reason": f"{WOKEN_STATE_CHECK}: the host served 'Thinking' for 10.5 s and a woken-only client never showed it",
    "checks": [{"name": WOKEN_STATE_CHECK, "ok": False}],
}


def test_a_mutated_cell_is_judged_at_the_first_stage_that_lost_the_item_not_at_the_unshown_state_after_it(monkeypatch, tmp_path) -> None:
    """Runner, 2026-11-12: reasoning sent as the reply woke no client, and the row read state_shown where it wanted kind."""

    def expectation(result):
        lost = result.get("reason")  # only the mutated cell carries one
        return {"ok": not lost, "stage": "kind" if lost else "-", "engine_posted": "live", "stream": "live"}

    status, (row,), cells = _run_one_engine_mutation(monkeypatch, tmp_path, [{"outcome": "measured"}, _WOKEN_ONLY], expectation)

    assert (row["baseline"], row["observed_outcome"], row["observed_stage"], row["ok"]) == ("pass", "fail", "kind", True)
    assert "never showed it" in row["reason"]
    assert (status, cells) == (0, 2)


def test_an_unshown_state_with_the_item_intact_upstream_is_still_reported_at_state_shown(monkeypatch, tmp_path) -> None:
    intact = {"ok": True, "stage": "-", "engine_posted": "live", "stream": "live"}

    status, (row,), _ = _run_one_engine_mutation(monkeypatch, tmp_path, [{"outcome": "measured"}, _WOKEN_ONLY], lambda _r: intact)

    assert (row["observed_outcome"], row["observed_stage"], row["ok"]) == ("fail", "state_shown", False)
    assert row["evidence"]["checks"] == _WOKEN_ONLY["checks"]
    assert status == 1


def test_an_unmutated_cell_that_fails_is_run_once_more_and_the_first_failure_stays_on_the_row(monkeypatch, tmp_path, capsys) -> None:
    never_ready = {
        "outcome": "fail",
        "reason": "provider_ran: provider exited before it was ready",
        "checks": [{"name": "provider_ran", "ok": False}],
    }
    lost = {"outcome": "measured", "lost": True}

    def expectation(result):
        return {"ok": not result.get("lost"), "stage": "kind" if result.get("lost") else "-", "engine_posted": "live", "stream": "live"}

    status, (row,), cells = _run_one_engine_mutation(monkeypatch, tmp_path, [never_ready, {"outcome": "measured"}, lost], expectation)

    assert (row["baseline"], row["baseline_retried"], row["observed_stage"], row["ok"]) == ("pass", True, "kind", True)
    assert (status, cells) == (0, 3)
    printed = capsys.readouterr().out
    assert "| pass (second try) |" in printed
    assert "RETRIED mutation codex-helm-thinking-as-reply: the unmutated cell failed once at preflight: provider_ran" in printed


def test_a_late_state_in_the_unmutated_cell_is_a_finding_and_does_not_stop_a_mutation_about_another_stage(
    monkeypatch, tmp_path, capsys
) -> None:
    """Runner, 2026-11-12: OpenCode Console's "Thinking" expired with no frame, and three mutations about other stages never ran."""

    lost = {"outcome": "measured", "lost": True}

    def expectation(result):
        return {"ok": not result.get("lost"), "stage": "kind" if result.get("lost") else "-", "engine_posted": "live", "stream": "live"}

    status, (row,), cells = _run_one_engine_mutation(monkeypatch, tmp_path, [_WOKEN_ONLY, lost], expectation)

    assert (row["baseline"], row["observed_stage"], row["ok"]) == ("pass", "kind", True)
    assert "never showed it" in row["state_finding"] and row["state_finding_evidence"]["checks"] == _WOKEN_ONLY["checks"]
    assert "baseline_retried" not in row
    assert (status, cells) == (0, 2)  # a finding is reported, it is not a miss
    printed = capsys.readouterr().out
    assert "| pass (state finding) |" in printed
    assert "FINDING codex/helm (thinking), unmutated: " in printed


def test_a_late_state_stops_the_one_mutation_that_is_about_that_stage(monkeypatch, tmp_path) -> None:
    import json

    mutation = next(m for m in MUTATIONS if m.name == "heartbeat-state-change-not-announced")
    assert mutation.stages == ("state_shown",) and mutation.side == "server"
    monkeypatch.setattr(mutations, "SCRATCH", tmp_path / "tt-mut")
    monkeypatch.setattr(mutations.tt, "load_expectations", lambda: {})
    calls = []
    monkeypatch.setattr(mutations, "run_kinds_suite", lambda *a, **k: [calls.append(1) or _WOKEN_ONLY])
    monkeypatch.setattr(kinds, "kinds_expectation", lambda _r: {"ok": True, "stage": "-", "engine_posted": "live", "stream": "live"})
    out = tmp_path / "rows.json"
    args = mutations.argparse.Namespace(
        engine=tmp_path / "engine", longhouse_cli=tmp_path / "cli", fault_engine=tmp_path / "engine", fault_longhouse_cli=tmp_path / "cli", only=[mutation.name],
        tokens=100, rate=20.0, out=out,
    )  # fmt: skip

    assert mutations.run_mutations(args) == 1

    (row,) = json.loads(out.read_text())
    assert (row["baseline"], row["baseline_stage"], row["observed"], row["ok"]) == ("fail", "state_shown", "not run", False)
    assert "state_finding" not in row and len(calls) == 2  # one retry, then a miss


# ----- an engine mutation is a runtime switch in the QA engine


ENGINE_SOURCES = sorted((mutations.REPO_ROOT / "engine" / "src").glob("**/*.rs"))
SWITCH_CALL = re.compile(r'qa_fault::mutant\(\s*"([a-z0-9-]+)"\s*\)')


def _switches_in_engine_source() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in ENGINE_SOURCES:
        for name in SWITCH_CALL.findall(path.read_text(errors="replace")):
            found[name] = found.get(name, 0) + 1
    return found


def test_every_rust_engine_defect_has_its_switch_in_the_engine_source_and_every_switch_has_a_check() -> None:
    """A check whose switch is not in the engine would run the unmutated engine and be reported as a miss; a switch no check
    names is a defect nobody tests. Both directions fail here, before a 15 minute run does."""

    in_source = _switches_in_engine_source()
    rust = {m.engine_switch for m in MUTATIONS if m.side == "engine" and m.asset is None}
    assert rust - set(in_source) == set(), "a mutation names a switch the engine source does not read"
    assert set(in_source) - rust == set(), "the engine source has a switch no mutation turns on"


def test_an_embedded_asset_defect_is_one_edit_of_a_file_the_engine_embeds_and_writes_out_through_the_patch_hook() -> None:
    engine_text = "\n".join(path.read_text(errors="replace") for path in ENGINE_SOURCES)
    assets = {m.asset for m in MUTATIONS if m.asset is not None}
    assert assets == {"longhouse-pi-helm.ts", "claude-mod/hooks/register.js"}
    for asset in assets:
        assert f'include_str!("../assets/{asset}")' in engine_text  # the engine embeds it
        assert f'patch_asset("{asset}"' in engine_text or "patch_asset(EXTENSION_FILE_NAME" in engine_text  # and writes it through the hook


def test_the_environment_of_an_engine_defect_names_its_switch_and_an_asset_defect_carries_its_edit() -> None:
    import json

    rust = next(m for m in MUTATIONS if m.name == "pi-helm-tool-forward")
    assert mutations.engine_environment(rust) == {"LONGHOUSE_QA_MUTANT": "pi-helm-tool-forward"}
    shared = {
        m.name: mutations.engine_environment(m) for m in MUTATIONS if m.name.endswith("subagent-forward") and m.name.startswith("codex")
    }
    assert {env["LONGHOUSE_QA_MUTANT"] for env in shared.values()} == {"codex-subagent-forward"}  # one defect, two cells
    asset = next(m for m in MUTATIONS if m.name == "claude-helm-text-forward")
    env = mutations.engine_environment(asset)
    (patch,) = json.loads(env["LONGHOUSE_QA_ASSET_PATCH"])
    assert patch == {"asset": "claude-mod/hooks/register.js", "old": asset.old, "new": asset.new}
    server = next(m for m in MUTATIONS if m.side == "server")
    assert mutations.engine_environment(server) == {}


def test_the_qa_engine_environment_reaches_the_machine_agent_and_every_providers_launch() -> None:
    """The defects sit in the Machine Agent (console sinks, bridges) and in the Helm launchers a provider's terminal starts."""

    from zerg.qa import terminal_truth_scenarios as scenarios

    source = Path(scenarios.__file__).read_text()
    assert "for key, value in (engine_environment or {}).items():" in source
    assert "penv[key] = value" in source and "environment[key] = value" in source
