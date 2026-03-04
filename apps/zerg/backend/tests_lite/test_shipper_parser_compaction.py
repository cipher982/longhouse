import json
from pathlib import Path

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
