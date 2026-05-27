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


def _user_prompt_row(expected: str) -> dict:
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": f"Please reply with exactly: {expected}",
        },
    }


def _assistant_text_row(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }


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
                json.dumps(_user_prompt_row(expected)),
                json.dumps(_assistant_text_row("Still working")),
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
                json.dumps(_user_prompt_row(expected)),
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


def test_assistant_transcript_match_can_start_after_cursor(tmp_path, monkeypatch):
    runner = _load_runner()
    session_id = "33333333-3333-4333-8333-333333333333"
    transcript_dir = tmp_path / ".claude" / "projects" / "repo"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / f"{session_id}.jsonl"
    expected = "STEER TOKEN ACCEPTED"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(_assistant_text_row(expected)),
                json.dumps(_assistant_text_row("Still working")),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    cursor = runner.transcript_line_counts(session_id)
    matched_before, *_ = runner.assistant_transcript_contains(
        session_id,
        expected,
        after_line_counts=cursor,
    )
    assert matched_before is False

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant_text_row(expected)) + "\n")

    matched_after, path, line, _timestamp = runner.assistant_transcript_contains(
        session_id,
        expected,
        after_line_counts=cursor,
    )

    assert matched_after is True
    assert path == str(transcript)
    assert line == 3


def test_read_json_file_returns_dict_only(tmp_path):
    runner = _load_runner()
    valid = tmp_path / "summary.json"
    valid.write_text(json.dumps({"hosted_archive_event_count": 2}), encoding="utf-8")
    array = tmp_path / "array.json"
    array.write_text(json.dumps([1, 2]), encoding="utf-8")

    assert runner.read_json_file(valid) == {"hosted_archive_event_count": 2}
    assert runner.read_json_file(array) is None
    assert runner.read_json_file(tmp_path / "missing.json") is None


def test_build_channel_send_command_adds_steer_metadata():
    runner = _load_runner()

    command = runner.build_channel_send_command(
        "11111111-1111-4111-8111-111111111111",
        "course correct",
        meta={"intent": "steer"},
    )

    assert command == [
        "longhouse",
        "claude-channel",
        "send",
        "--session-id",
        "11111111-1111-4111-8111-111111111111",
        "--text",
        "course correct",
        "--meta",
        "intent=steer",
    ]


def test_build_channel_send_command_omits_empty_metadata():
    runner = _load_runner()

    command = runner.build_channel_send_command(
        "11111111-1111-4111-8111-111111111111",
        "continue",
        meta={},
    )

    assert command == [
        "longhouse",
        "claude-channel",
        "send",
        "--session-id",
        "11111111-1111-4111-8111-111111111111",
        "--text",
        "continue",
    ]
