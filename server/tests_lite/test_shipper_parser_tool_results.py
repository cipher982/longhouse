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
