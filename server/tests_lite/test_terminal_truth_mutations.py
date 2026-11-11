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
        "opencode-host-publish",
        "omp-session-dir-rule",
        "opencode-part-type-check",
        "antigravity-step-update-arm",
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
