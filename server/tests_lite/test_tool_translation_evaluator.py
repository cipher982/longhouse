from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from zerg.cli.main import app
from zerg.services.agents.models import EventIngest
from zerg.services.session_views import EventResponse
from zerg.services.shipper.parser import _extract_assistant_events
from zerg.services.tool_translation_evaluator import evaluate_codex_archive
from zerg.services.tool_translation_evaluator import evaluate_manifest
from zerg.services.tool_translation_evaluator import write_evidence_package

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "tests" / "fixtures" / "tool-translation" / "manifest.json"


def test_golden_replay_conserves_and_pairs_every_provider_event():
    first = evaluate_manifest(GOLDEN)
    second = evaluate_manifest(GOLDEN)

    assert first["passed"] is True
    assert first["stable_identity_digest"] == second["stable_identity_digest"]
    assert first["totals"]["source_events"] == 18
    assert first["totals"]["canonical_events"] == 18
    assert first["totals"]["outer_calls"] == 9
    assert first["totals"]["paired"] == 9
    assert first["totals"]["exact"] == 7
    assert first["totals"]["parsed"] == 1
    assert first["totals"]["unknown"] == 1
    assert first["totals"]["inferred_children"] == 1
    assert first["totals"]["wrappers_retained"] == 0
    assert first["totals"]["visible_rows"] == 9
    assert first["totals"]["lost"] == 0
    assert first["totals"]["duplicated"] == 0
    assert first["totals"]["unattributed"] == 0
    assert first["consequence_slices"] == {
        "approval": 1,
        "external_effect": 1,
        "failure": 1,
        "mutation": 1,
        "read_only": 5,
        "unknown": 1,
    }
    assert set(first["providers"]) == {"antigravity", "claude", "codex", "cursor", "opencode"}
    assert first["unknowns"] == [
        {
            "provider": "cursor",
            "tool_name": "CallDynamicTool",
            "input_shape": {"fixture": "bool"},
            "count": 1,
            "with_result_id": 1,
        }
    ]
    assert first["errors"] == []
    assert first["verdicts"]["transcript"] == {
        "verdict": "yellow",
        "reasons": ["unknown_shapes_present"],
    }
    assert first["verdicts"]["control"]["verdict"] == "not_evaluated"
    assert first["factory_health"] == {
        "state": "synthetic",
        "reason": "hermetic_replay_not_discovery",
        "complete_window": False,
    }


def test_cli_emits_machine_readable_report_without_payload_values():
    result = CliRunner().invoke(app, ["translation", "evaluate", "--corpus", str(GOLDEN), "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["passed"] is True
    assert report["manifest_id"] == "tool-translation-golden-v1"
    assert "/fixture/example.md" not in result.stdout
    assert "const r = await" not in result.stdout


def test_string_tool_input_survives_parser_ingest_and_response_contracts():
    raw_input = 'const r = await tools.exec_command({cmd:"pwd"}); text(r.output);'
    parsed = list(
        _extract_assistant_events(
            {
                "uuid": "message-1",
                "timestamp": "2026-07-23T12:00:00Z",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "call-1", "name": "exec", "input": raw_input}
                    ]
                },
            },
            "session-1",
            0,
        )
    )

    assert parsed[0].tool_input_json == raw_input
    ingest = EventIngest(
        role="assistant",
        tool_name="exec",
        tool_input_json=raw_input,
        timestamp="2026-07-23T12:00:00Z",
    )
    response = EventResponse(
        id="event-1",
        role="assistant",
        tool_name="exec",
        tool_input_json=ingest.tool_input_json,
        timestamp="2026-07-23T12:00:00Z",
    )
    assert response.model_dump(mode="json")["tool_input_json"] == raw_input


def test_native_codex_discovery_is_value_free_and_writes_shared_evidence_package(tmp_path):
    archive = tmp_path / "sessions"
    archive.mkdir()
    call = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "call-secret",
            "input": 'const r = await tools.exec_command({cmd:"secret value"}); text(r.output);',
        },
    }
    result = {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "call-secret", "output": "secret result"},
    }
    (archive / "session.jsonl").write_text(json.dumps(call) + "\n" + json.dumps(result) + "\n")

    report = evaluate_codex_archive(archive)
    package = write_evidence_package(report, tmp_path / "factory")

    assert report["passed"] is True
    assert report["totals"]["paired"] == 1
    assert report["totals"]["wrappers_receded"] == 1
    assert report["verdicts"]["transcript"]["verdict"] == "green"
    rendered = json.dumps(report)
    assert "secret value" not in rendered
    assert "secret result" not in rendered
    assert (package / "shape-inventory.json").is_file()
    assert (package / "presentation-report.json").is_file()


def test_native_codex_discovery_covers_function_calls_and_spaced_session_metadata(tmp_path):
    archive = tmp_path / "sessions"
    archive.mkdir()
    rows = [
        {"type": "session_meta", "payload": {"cli_version": "9.9.9"}},
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "request_user_input",
                "call_id": "question-1",
                "arguments": {"question": "Continue?"},
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "question-1", "output": "yes"},
        },
    ]
    (archive / "session.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    report = evaluate_codex_archive(archive)

    assert report["reports"]["shape_unknown"]["provider_releases"] == {"9.9.9": 1}
    assert report["totals"]["outer_calls"] == 1
    assert report["totals"]["paired"] == 1
    assert report["totals"]["exact"] == 1


def test_factory_status_is_explicitly_unknown_without_discovery(tmp_path):
    result = CliRunner().invoke(
        app,
        ["provider", "factory", "status", "--evidence-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["factory_health"] == {
        "state": "unknown",
        "reason": "discovery_missing",
    }


def test_hermetic_replay_cannot_mark_discovery_current(tmp_path):
    report = evaluate_manifest(GOLDEN)
    write_evidence_package(report, tmp_path)

    result = CliRunner().invoke(
        app,
        ["provider", "factory", "status", "--evidence-root", str(tmp_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["factory_health"] == {
        "state": "unknown",
        "reason": "hermetic_replay_not_discovery",
    }
