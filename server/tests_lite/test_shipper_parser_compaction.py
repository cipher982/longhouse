import json
from pathlib import Path
from tempfile import TemporaryDirectory

from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.parser import parse_session_file_full


def _write_jsonl(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_session_file_emits_compaction_system_events(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "compaction-session.jsonl",
        [
            '{"type":"summary","summary":"Session compacted to summary","leafUuid":"leaf-1"}',
            '{"type":"file-history-snapshot","snapshot":{"timestamp":"2026-01-01T00:00:01Z"}}',
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "content": "Conversation compacted",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "compactMetadata": {"trigger": "auto", "preTokens": 155708},
                },
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "type": "system",
                    "subtype": "microcompact_boundary",
                    "content": "Context microcompacted",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "microcompactMetadata": {"trigger": "auto", "preTokens": 11975},
                },
                separators=(",", ":"),
            ),
            '{"type":"progress","timestamp":"2026-01-01T00:00:03Z","data":{"type":"hook_progress"}}',
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:04Z","message":{"content":"real message"}}',
        ],
    )

    events = list(parse_session_file(path))

    assert [event.raw_type for event in events] == [
        "summary",
        "file-history-snapshot",
        "compact_boundary",
        "microcompact_boundary",
        "user",
    ]
    assert all(event.role == "system" for event in events[:4])
    assert events[4].role == "user"
    assert events[0].content_text == "Session compacted to summary"
    assert events[1].content_text.startswith("File history snapshot")
    assert events[2].content_text == "Conversation compacted [trigger=auto pre_tokens=155708]"
    assert events[3].content_text == "Context microcompacted [trigger=auto pre_tokens=11975]"


def test_parse_session_file_full_tracks_offsets_with_compaction_events(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "offsets-session.jsonl",
        [
            '{"type":"summary","summary":"Compact summary"}',
            json.dumps(
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "content": "Conversation compacted",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "compactMetadata": {"trigger": "auto", "preTokens": 123},
                },
                separators=(",", ":"),
            ),
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:03Z","message":{"content":"hello"}}',
        ],
    )

    events, last_good_offset, metadata = parse_session_file_full(path)

    assert len(events) == 3
    assert events[0].raw_type == "summary"
    assert events[1].raw_type == "compact_boundary"
    assert events[2].raw_type == "user"
    assert last_good_offset == path.stat().st_size
    assert metadata.started_at is not None
    assert metadata.ended_at is not None


def test_parse_session_file_full_does_not_promote_generic_workspace_project(tmp_path):
    with TemporaryDirectory(prefix="lh-workspace-project-test-", dir="/tmp") as temp_root:
        workspace = Path(temp_root) / "workspace"
        workspace.mkdir()
        path = _write_jsonl(
            tmp_path,
            "workspace-session.jsonl",
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u1",
                        "timestamp": "2026-01-01T00:00:03Z",
                        "cwd": str(workspace),
                        "message": {"content": "hello"},
                    },
                    separators=(",", ":"),
                ),
            ],
        )

        _events, _last_good_offset, metadata = parse_session_file_full(path)

        assert metadata.cwd == str(workspace)
        assert metadata.project is None


def test_parse_session_file_full_keeps_workspace_when_git_root(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".git").mkdir(parents=True)
    path = _write_jsonl(
        tmp_path,
        "workspace-git-session.jsonl",
        [
            json.dumps(
                {
                    "type": "user",
                    "uuid": "u1",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "cwd": str(workspace),
                    "message": {"content": "hello"},
                },
                separators=(",", ":"),
            ),
        ],
    )

    _events, _last_good_offset, metadata = parse_session_file_full(path)

    assert metadata.cwd == str(workspace)
    assert metadata.project == "workspace"


def test_progress_events_are_intentionally_dropped(tmp_path):
    """Progress events are high-volume hook/tool noise.

    They are preserved in the source archive but intentionally excluded from
    parsed events. This is a deliberate design choice, not a bug — do not
    "fix" by adding them back without updating this test.
    """
    path = _write_jsonl(
        tmp_path,
        "progress-session.jsonl",
        [
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:01Z","message":{"content":"hello"}}',
            '{"type":"progress","timestamp":"2026-01-01T00:00:02Z","data":{"type":"hook_progress"}}',
            '{"type":"progress","timestamp":"2026-01-01T00:00:02Z","data":{"type":"tool_progress","tool":"Read"}}',
            '{"type":"progress","timestamp":"2026-01-01T00:00:02Z","data":{"type":"hook_progress"}}',
            '{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"text","text":"hi"}]}}',
        ],
    )

    events = list(parse_session_file(path))
    raw_types = [e.raw_type for e in events]

    assert "progress" not in raw_types
    assert raw_types == ["user", "assistant"]

    # Also verify via parse_session_file_full
    events_full, _, _ = parse_session_file_full(path)
    assert "progress" not in [e.raw_type for e in events_full]
    assert len(events_full) == 2
