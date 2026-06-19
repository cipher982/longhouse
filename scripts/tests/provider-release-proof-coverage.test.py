#!/usr/bin/env python3
"""Schema tests for the provider release-proof coverage map."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_PATH = REPO_ROOT / "docs" / "specs" / "provider-release-proof-coverage.json"
SPEC_PATH = REPO_ROOT / "docs" / "specs" / "provider-release-proof.md"

EXPECTED_PROVIDERS = ("claude", "codex", "opencode", "antigravity")
PROVIDER_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex/OpenAI",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
}
EXPECTED_SURFACES = (
    "install/stage exact version",
    "binary identity",
    "auth/status shape",
    "launch managed session",
    "session id/path binding",
    "transcript/log parse",
    "ingest into Longhouse",
    "timeline/session projection",
    "send input",
    "interrupt/abort/steer",
    "reattach/resume",
    "tool/tool-result shape",
    "live-token behavior",
)
REQUIRED_SCENARIO_FIELDS = {
    "provider",
    "scenario_id",
    "provider_version",
    "baseline_scope",
    "baseline_boundary",
    "promoted_to_sauron",
}
REQUIRED_ROW_FIELDS = {
    "provider",
    "surface",
    "covered",
    "test_evidence",
    "proof_boundary",
    "fake_or_real",
    "runs_in_ci",
    "runs_in_sauron_release_watch",
    "accepted_baseline",
    "baseline_scenarios",
    "failure_actionable",
}
ALLOWED_COVERED = {"yes", "partial", "no"}
ALLOWED_FAKE_OR_REAL = {"fake", "real", "mixed", "none"}
ALLOWED_BASELINE = {"none", "parser_fixture", "release_proof"}
ALLOWED_PROOF_BOUNDARY = {
    "none",
    "unsupported",
    "fixture",
    "hermetic",
    "live_no_token",
    "live_no_token_or_fake",
    "hermetic_or_live_no_token",
    "hermetic_or_live_token",
    "hermetic_or_launch_flag_shape",
    "hermetic_or_launch_flag_shape_or_machine_live",
    "hermetic_or_machine_live_token",
    "fixture_or_hermetic",
    "path_or_override",
    "isolated_npm_package",
    "real_release_asset",
    "hermetic_plus_manual_live_token",
    "hermetic_plus_manual_live_token_or_machine_live_token",
    "manual_live_token",
    "manual_live_token_or_machine_live_token",
    "manual_live_token_or_fake",
    "managed_runtime_live_token_or_fake",
}


def _load() -> dict:
    payload = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _metric_table(markdown: str) -> dict[str, int]:
    in_table = False
    metrics: dict[str, int] = {}
    for line in markdown.splitlines():
        if line == "| Metric | Count |":
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        if line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) == 2:
            metrics[cells[0]] = int(cells[1])
    return metrics


def _provider_shape_table(markdown: str) -> dict[str, dict[str, int]]:
    in_table = False
    rows: dict[str, dict[str, int]] = {}
    for line in markdown.splitlines():
        if line == "| Provider | Yes | Partial | No | CI rows | Sauron rows | Release baselines |":
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        if line.startswith("| ---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 7:
            continue
        provider, yes, partial, no, ci, sauron, release_baselines = cells
        rows[provider] = {
            "yes": int(yes),
            "partial": int(partial),
            "no": int(no),
            "ci": int(ci),
            "sauron": int(sauron),
            "release_baselines": int(release_baselines),
        }
    return rows


def _count(rows: list[dict], **matches: object) -> int:
    return sum(
        1
        for row in rows
        if all(row.get(key) == value for key, value in matches.items())
    )


def test_coverage_map_has_full_provider_surface_grid() -> None:
    payload = _load()

    assert payload["schema_version"] == 2
    assert tuple(payload["providers"]) == EXPECTED_PROVIDERS
    assert tuple(payload["surfaces"]) == EXPECTED_SURFACES

    rows = payload["rows"]
    assert len(rows) == len(EXPECTED_PROVIDERS) * len(EXPECTED_SURFACES)
    seen = {(row["provider"], row["surface"]) for row in rows}
    assert len(seen) == len(rows)
    assert seen == {
        (provider, surface)
        for provider in EXPECTED_PROVIDERS
        for surface in EXPECTED_SURFACES
    }


def test_accepted_release_proof_scenarios_are_explicit() -> None:
    payload = _load()
    scenarios = payload.get("accepted_release_proof_scenarios")
    assert isinstance(scenarios, list)
    assert scenarios

    seen: set[tuple[str, str]] = set()
    for scenario in scenarios:
        missing = REQUIRED_SCENARIO_FIELDS - set(scenario)
        assert not missing, f"accepted scenario missing {sorted(missing)}"
        assert scenario["provider"] in EXPECTED_PROVIDERS, scenario
        assert scenario["scenario_id"], scenario
        assert scenario["provider_version"], scenario
        assert scenario["baseline_scope"], scenario
        assert scenario["baseline_boundary"], scenario
        assert isinstance(scenario["promoted_to_sauron"], bool), scenario
        key = (scenario["provider"], scenario["scenario_id"])
        assert key not in seen, scenario
        seen.add(key)


def test_coverage_rows_are_auditable() -> None:
    payload = _load()
    accepted_scenarios = {
        (scenario["provider"], scenario["scenario_id"])
        for scenario in payload["accepted_release_proof_scenarios"]
    }

    for row in payload["rows"]:
        missing = REQUIRED_ROW_FIELDS - set(row)
        assert not missing, f"{row.get('provider')} {row.get('surface')} missing {sorted(missing)}"
        assert row["covered"] in ALLOWED_COVERED, row
        assert row["fake_or_real"] in ALLOWED_FAKE_OR_REAL, row
        assert row["accepted_baseline"] in ALLOWED_BASELINE, row
        assert row["proof_boundary"] in ALLOWED_PROOF_BOUNDARY, row
        assert isinstance(row["test_evidence"], list), row
        assert isinstance(row["baseline_scenarios"], list), row
        assert isinstance(row["runs_in_ci"], bool), row
        assert isinstance(row["runs_in_sauron_release_watch"], bool), row
        assert isinstance(row["failure_actionable"], bool), row

        if row["covered"] != "no" or row["runs_in_ci"] or row["runs_in_sauron_release_watch"]:
            assert row["test_evidence"], f"{row['provider']} {row['surface']} needs evidence"
        if row["covered"] != "no":
            assert row["fake_or_real"] != "none", row
            assert row["proof_boundary"] not in {"none", "unsupported"}, row
        if row["covered"] == "no":
            assert row["accepted_baseline"] == "none", row
            assert row["baseline_scenarios"] == [], row
            assert not row["runs_in_ci"], row
            assert not row["runs_in_sauron_release_watch"], row
            assert row["proof_boundary"] in {"none", "unsupported"}, row
        if row["accepted_baseline"] == "release_proof":
            assert row["baseline_scenarios"], row
            for scenario_id in row["baseline_scenarios"]:
                assert (row["provider"], scenario_id) in accepted_scenarios, row
        else:
            assert row["baseline_scenarios"] == [], row
        has_sauron_evidence = any(str(item).startswith("Sauron:") for item in row["test_evidence"])
        if row["runs_in_sauron_release_watch"] and not has_sauron_evidence:
            assert row["runs_in_ci"], row


def test_spec_snapshot_tables_match_coverage_json() -> None:
    payload = _load()
    rows = payload["rows"]
    markdown = SPEC_PATH.read_text(encoding="utf-8")

    metrics = _metric_table(markdown)
    assert metrics == {
        "Providers": len(payload["providers"]),
        "Contract surfaces per provider": len(payload["surfaces"]),
        "Total provider/surface rows": len(rows),
        "Covered `yes`": _count(rows, covered="yes"),
        "Covered `partial`": _count(rows, covered="partial"),
        "Covered `no`": _count(rows, covered="no"),
        "Rows running in Longhouse CI": sum(1 for row in rows if row["runs_in_ci"]),
        "Rows running in Sauron release-watch": sum(
            1 for row in rows if row["runs_in_sauron_release_watch"]
        ),
        "Rows with accepted parser-fixture baselines": _count(
            rows, accepted_baseline="parser_fixture"
        ),
        "Rows with accepted release-proof baselines": _count(
            rows, accepted_baseline="release_proof"
        ),
    }

    provider_shape = _provider_shape_table(markdown)
    assert set(provider_shape) == set(PROVIDER_LABELS.values())
    for provider, label in PROVIDER_LABELS.items():
        provider_rows = [row for row in rows if row["provider"] == provider]
        assert provider_shape[label] == {
            "yes": _count(provider_rows, covered="yes"),
            "partial": _count(provider_rows, covered="partial"),
            "no": _count(provider_rows, covered="no"),
            "ci": sum(1 for row in provider_rows if row["runs_in_ci"]),
            "sauron": sum(
                1 for row in provider_rows if row["runs_in_sauron_release_watch"]
            ),
            "release_baselines": _count(
                provider_rows, accepted_baseline="release_proof"
            ),
        }


def main() -> int:
    tests = [
        test_coverage_map_has_full_provider_surface_grid,
        test_accepted_release_proof_scenarios_are_explicit,
        test_coverage_rows_are_auditable,
        test_spec_snapshot_tables_match_coverage_json,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
