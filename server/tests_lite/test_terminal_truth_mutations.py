"""The mutation edits still land on the current source, exactly once each (a stale mutation would silently test nothing)."""

from __future__ import annotations

import pytest

from zerg.qa import terminal_truth_mutations as mutations
from zerg.qa.terminal_truth_mutations import MUTATIONS


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_each_mutation_applies_exactly_once_and_changes_the_file(mutation) -> None:
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
        "antigravity-step-update-arm",
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


def test_every_process_the_mutation_runner_starts_gets_a_scrubbed_environment(monkeypatch, tmp_path) -> None:
    """The runner's git, cp and cargo children must not inherit the caller's hook URL or token."""

    import subprocess

    from zerg.qa import terminal_truth_mutations as mutations

    for name, value in {"LONGHOUSE_HOOK_URL": "http://127.0.0.1:9/real", "LONGHOUSE_HOOK_TOKEN": "real", "INFISICAL_TOKEN": "real"}.items():
        monkeypatch.setenv(name, value)
    seen: list[dict[str, str]] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: seen.append(k.get("env")))
    mutations._run(["git", "status"], cwd=tmp_path)
    mutations._clone_dir(tmp_path, tmp_path / "clone")
    assert len(seen) == 2
    for environment in seen:
        assert environment is not None and not {"LONGHOUSE_HOOK_URL", "LONGHOUSE_HOOK_TOKEN", "INFISICAL_TOKEN"} & set(environment)


def test_a_row_that_is_not_ok_says_which_cell_went_wrong_and_why() -> None:
    baseline = {"check": "mutation x", "expected_stage": "kind", "baseline": "fail", "baseline_stage": "preflight",
                "baseline_reason": "the provider\nnever became ready", "observed": "not run", "ok": False}  # fmt: skip
    assert mutations.why_not(baseline) == (
        "the unmutated cell came out fail at preflight, so the mutated one was not run: the provider never became ready"
    )
    mutated = {"check": "mutation x", "expected_stage": "kind", "baseline": "pass", "observed_outcome": "fail",
               "observed_stage": "state_shown", "reason": "woke nobody", "ok": False}  # fmt: skip
    assert mutations.why_not(mutated) == "expected a failure at kind, saw fail at state_shown: woke nobody"
    half_speed = {"check": "half-speed fake model", "expected_stage": "(pass)", "observed_outcome": "fail",
                  "observed_stage": "client_received", "reason": "", "ok": False}  # fmt: skip
    assert mutations.why_not(half_speed) == "expected a pass, saw fail at client_received: no reason recorded"


def test_a_failing_unmutated_cell_is_reported_with_its_reason_and_its_mutation_is_not_built(monkeypatch, tmp_path, capsys) -> None:
    """On the Linux runner 7 unmutated cells failed (2026-11-12) and the log said only "fail": 14 minutes of rebuilds, no reason."""

    import json

    engine_mutation = next(m for m in MUTATIONS if m.side == "engine" and m.scenario)
    monkeypatch.setattr(mutations, "SCRATCH", tmp_path / "tt-mut")
    monkeypatch.setattr(mutations, "_run", lambda *a, **k: None)
    monkeypatch.setattr(mutations, "_clone_dir", lambda *a, **k: None)
    monkeypatch.setattr(mutations.tt, "load_expectations", lambda: {})

    def no_build(*_a, **_k):
        raise AssertionError("the engine was rebuilt for a row that had already failed")

    monkeypatch.setattr(mutations, "_build_engine", no_build)
    failed = {"outcome": "fail", "reason": "preflight: the provider never became ready", "checks": [{"name": "preflight", "ok": False}]}
    monkeypatch.setattr(mutations, "run_kinds_suite", lambda *a, **k: [failed])
    out = tmp_path / "rows.json"
    args = mutations.argparse.Namespace(
        engine=tmp_path / "engine", longhouse_cli=tmp_path / "cli", cargo_target=tmp_path / "target", only=[engine_mutation.name],
        skip_engine=False, tokens=100, rate=20.0, out=out,
    )  # fmt: skip

    assert mutations.run_mutations(args) == 1

    (row,) = json.loads(out.read_text())
    assert row["baseline"] == "fail" and row["observed"] == "not run" and row["ok"] is False
    assert row["baseline_retried"] is True  # it never reached the prompt, so it got one more try
    assert row["evidence"]["checks"] == failed["checks"]
    assert row["baseline_reason"] == "preflight: the provider never became ready"
    printed = capsys.readouterr().out
    assert f"NO mutation {engine_mutation.name}: the unmutated cell came out fail at preflight" in printed
    assert "the provider never became ready" in printed


def _run_one_engine_mutation(monkeypatch, tmp_path, results, expectation):
    """Run one live-kinds engine mutation with the cells, the build and git replaced; returns (exit status, rows, calls made)."""

    import json

    mutation = next(m for m in MUTATIONS if m.name == "codex-helm-thinking-as-reply")
    scratch = tmp_path / "tt-mut"
    source = scratch / "worktree" / mutation.file
    source.parent.mkdir(parents=True)
    source.write_text((mutations.REPO_ROOT / mutation.file).read_text())
    monkeypatch.setattr(mutations, "SCRATCH", scratch)
    monkeypatch.setattr(mutations, "_run", lambda *a, **k: None)
    monkeypatch.setattr(mutations, "_clone_dir", lambda *a, **k: None)
    monkeypatch.setattr(mutations.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(mutations.tt, "load_expectations", lambda: {})
    monkeypatch.setattr(mutations, "_build_engine", lambda *a, **k: (tmp_path / "engine", tmp_path / "cli"))
    queue = list(results)
    monkeypatch.setattr(mutations, "run_kinds_suite", lambda *a, **k: [queue.pop(0)])
    monkeypatch.setattr(mutations, "kinds_expectation", expectation)
    out = tmp_path / "rows.json"
    args = mutations.argparse.Namespace(
        engine=tmp_path / "engine", longhouse_cli=tmp_path / "cli", cargo_target=tmp_path / "target", only=[mutation.name],
        skip_engine=False, tokens=100, rate=20.0, out=out,
    )  # fmt: skip
    status = mutations.run_mutations(args)
    return status, json.loads(out.read_text()), len(results) - len(queue)


_WOKEN_ONLY = {
    "outcome": "fail",
    "reason": f"{mutations.WOKEN_STATE_CHECK}: the host served 'Thinking' for 10.5 s and a woken-only client never showed it",
    "checks": [{"name": mutations.WOKEN_STATE_CHECK, "ok": False}],
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


def test_an_unmutated_cell_that_never_reached_the_prompt_is_run_once_more(monkeypatch, tmp_path) -> None:
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
