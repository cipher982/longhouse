from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from zerg.qa.ios_workspace_selection_source_producer import ASSERTION_ID
from zerg.qa.ios_workspace_selection_source_producer import REGISTRATION
from zerg.qa.ios_workspace_selection_source_producer import SOURCE_PATH
from zerg.qa.ios_workspace_selection_source_producer import evaluate_launch_workspace_selection_source
from zerg.qa.ios_workspace_selection_source_producer import run

ROOT = Path(__file__).resolve().parents[2]


def _production_source() -> str:
    return (ROOT / SOURCE_PATH).read_text(encoding="utf-8")


def test_product_assurance_cell_matches_source_producer_registration() -> None:
    contract = yaml.safe_load((ROOT / "schemas/product_assurance.yml").read_text(encoding="utf-8"))
    rows = [row for row in contract["assertions"] if row["assertion_id"] == ASSERTION_ID]

    assert rows == [
        {
            "subject_kind": "longhouse_product",
            "capability": "launch.workspace_suggestions",
            "disposition": "implemented",
            "assertion_id": ASSERTION_ID,
            "variant": None,
            "scenario_id": REGISTRATION.scenario_id,
            "minimum_scenario_revision": REGISTRATION.scenario_revision,
            "oracle_source": REGISTRATION.oracle_source,
            "acceptable_evidence": ["hermetic"],
            "max_age_seconds": 86400,
        }
    ]


def test_source_oracle_proves_production_selection_and_wiring() -> None:
    observation = evaluate_launch_workspace_selection_source(_production_source())

    assert observation == {
        "passed": True,
        "production_swift_source_read": True,
        "implicit_cached_default_reconciled_from_fresh_ranking": True,
        "explicit_absolute_choice_preserved": True,
        "picker_marks_explicit_choice": True,
        "machine_change_resets_implicit_default": True,
        "unlaunchable_machine_clears_implicit_default": True,
        "legacy_cache_generation_invalidated": True,
    }


def test_source_oracle_rejects_original_cached_implicit_default_bug() -> None:
    source = _production_source()
    fixed = """            let selection = resolveFreshWorkspaceSelection(
                currentPath: normalizedCwd,
                source: workspaceSelectionSource,
                suggestions: suggestions
            )
            cwd = selection.path
            workspaceSelectionSource = selection.source
"""
    original_bug = """            if normalizedCwd.isEmpty, let first = suggestions.first?.path {
                cwd = first
            }
"""
    assert source.count(fixed) == 1

    observation = evaluate_launch_workspace_selection_source(source.replace(fixed, original_bug))

    assert observation["passed"] is False
    assert observation["implicit_cached_default_reconciled_from_fresh_ranking"] is False


def test_source_oracle_rejects_picker_that_does_not_mark_explicit_choice() -> None:
    source = _production_source()
    assignment = "                            workspaceSelectionSource = .explicitUserChoice\n"
    assert source.count(assignment) == 1

    observation = evaluate_launch_workspace_selection_source(source.replace(assignment, ""))

    assert observation["passed"] is False
    assert observation["picker_marks_explicit_choice"] is False


def test_product_producer_retains_source_bound_evidence(tmp_path: Path) -> None:
    result = run(tmp_path / "evidence", repo_root=ROOT)

    assert REGISTRATION.subject_kind == "longhouse_product"
    assert REGISTRATION.providers == ()
    assert REGISTRATION.provider_artifact_required is False
    assert REGISTRATION.acquisition_methods == ("hermetic_source_under_test",)
    assert result["status"] == "pass"
    assert result["assertions"] == {ASSERTION_ID: True}
    assert result["observation"]["source_path"] == SOURCE_PATH.as_posix()
    assert result["observation"]["source_sha256"].startswith("sha256:")
    assert {item["path"] for item in result["artifact_manifest"]} == {
        "cleanup-receipt.json",
        "selection-contract-observation.json",
    }


def test_product_producer_registration_and_cli_execute_without_xcode(tmp_path: Path) -> None:
    registration = subprocess.run(
        [sys.executable, "-m", "zerg.qa.ios_workspace_selection_source_producer", "--registration"],
        cwd=ROOT / "server",
        capture_output=True,
        text=True,
        check=False,
    )
    assert registration.returncode == 0, registration.stderr
    assert json.loads(registration.stdout)["producer_id"] == REGISTRATION.producer_id

    evidence = tmp_path / "cli-evidence"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "zerg.qa.ios_workspace_selection_source_producer",
            "--evidence-root",
            str(evidence),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout)["status"] == "pass"
    assert {path.name for path in evidence.iterdir()} == {
        "cleanup-receipt.json",
        "selection-contract-observation.json",
        "result.json",
    }
