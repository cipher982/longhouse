from pathlib import Path

from zerg.services.shipper.parser import parse_session_file
from zerg.services.shipper.parser import parse_session_file_full


def _write_jsonl(tmp_path: Path, name: str, lines: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_parse_session_file_emits_image_tool_result_placeholder(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "image-tool-result.jsonl",
        [
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_img","content":[{"type":"image","source":{"type":"base64","data":"abc123"}}]}]}}',
        ],
    )

    events = list(parse_session_file(path))

    assert len(events) == 1
    assert events[0].role == "tool"
    assert events[0].tool_call_id == "toolu_img"
    assert events[0].tool_output_text == "[image result]"


def test_parse_session_file_full_emits_tool_reference_placeholder(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "tool-reference-result.jsonl",
        [
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_refs","content":[{"type":"tool_reference","tool_name":"TaskCreate"},{"type":"tool_reference","tool_name":"TaskUpdate"},{"type":"tool_reference","tool_name":"TaskList"}]}]}}',
        ],
    )

    events, last_good_offset, metadata = parse_session_file_full(path)

    assert len(events) == 1
    assert events[0].role == "tool"
    assert events[0].tool_call_id == "toolu_refs"
    assert events[0].tool_output_text == "[tool references: TaskCreate, TaskUpdate, TaskList]"
    assert last_good_offset == path.stat().st_size
    assert metadata.session_id == path.stem


def test_parse_session_file_emits_empty_success_tool_result_placeholder(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "empty-tool-result.jsonl",
        [
            '{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"tool_use","id":"toolu_empty","name":"Bash","input":{"command":"touch done"}}]}}',
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_empty","content":""}]}}',
            '{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_empty_text","content":[{"type":"text","text":""}]}]}}',
        ],
    )

    events = list(parse_session_file(path))

    assert [(event.role, event.tool_call_id, event.tool_output_text) for event in events] == [
        ("assistant", "toolu_empty", None),
        ("tool", "toolu_empty", "[empty tool result]"),
        ("tool", "toolu_empty_text", "[empty tool result]"),
    ]


def test_parse_session_file_emits_json_tool_result_object(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "object-tool-result.jsonl",
        [
            '{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_object","content":{"status":"ok","count":0}}]}}',
            '{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_false","content":false}]}}',
        ],
    )

    events = list(parse_session_file(path))

    assert [(event.tool_call_id, event.tool_output_text) for event in events] == [
        ("toolu_object", '{"status":"ok","count":0}'),
        ("toolu_false", "false"),
    ]
