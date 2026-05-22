from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_runner():
    path = ROOT / "scripts" / "ops" / "run-managed-claude-poc.py"
    spec = importlib.util.spec_from_file_location("run_managed_claude_poc", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_assistant_transcript_match_ignores_injected_user_prompt(tmp_path, monkeypatch):
    runner = _load_runner()
    session_id = "11111111-1111-4111-8111-111111111111"
    transcript_dir = tmp_path / ".claude" / "projects" / "repo"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{session_id}.jsonl"
    expected = "LONGHOUSE CLAUDE PROFILE READY"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": f"Please reply with exactly: {expected}"}}),
                json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Still working"}]}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    matched, path, line, timestamp = runner.assistant_transcript_contains(session_id, expected)

    assert matched is False
    assert path is None
    assert line is None
    assert timestamp is None


def test_assistant_transcript_match_finds_assistant_response(tmp_path, monkeypatch):
    runner = _load_runner()
    session_id = "22222222-2222-4222-8222-222222222222"
    transcript_dir = tmp_path / ".claude" / "projects" / "repo"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{session_id}.jsonl"
    expected = "LONGHOUSE CLAUDE PROFILE READY"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": f"Please reply with exactly: {expected}"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "2026-05-22T11:31:00.000Z",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": expected}]},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    matched, path, line, timestamp = runner.assistant_transcript_contains(session_id, expected)

    assert matched is True
    assert path == str(transcript)
    assert line == 2
    assert timestamp == "2026-05-22T11:31:00.000Z"


def test_read_json_file_returns_dict_only(tmp_path):
    runner = _load_runner()
    valid = tmp_path / "summary.json"
    valid.write_text(json.dumps({"hosted_archive_event_count": 2}), encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text(json.dumps([1, 2]), encoding="utf-8")

    assert runner.read_json_file(valid) == {"hosted_archive_event_count": 2}
    assert runner.read_json_file(array) is None
    assert runner.read_json_file(tmp_path / "missing.json") is None
