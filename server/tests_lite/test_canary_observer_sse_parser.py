"""Unit test for the canary observer's SSE frame parser.

The parser has to handle the spec's weird edge cases: multi-line data,
: comments, blank-line dispatch, missing optional fields. Freeze the
behavior so a future change doesn't silently break the observer.
"""

import importlib.util
from io import StringIO
from pathlib import Path


def _load_observer():
    repo_root = Path(__file__).resolve().parents[2]
    path = repo_root / "scripts" / "canary" / "observer.py"
    spec = importlib.util.spec_from_file_location("canary_observer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResponse:
    def __init__(self, lines: list[str]):
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


def test_sse_parser_basic_event():
    observer = _load_observer()
    resp = _FakeResponse(
        [
            "event: workspace_changed",
            'data: {"session_id":"abc","latest_event_id":42}',
            "",
        ]
    )
    frames = list(observer._iter_sse(resp))
    assert frames == [("workspace_changed", '{"session_id":"abc","latest_event_id":42}')]


def test_sse_parser_multiple_frames():
    observer = _load_observer()
    resp = _FakeResponse(
        [
            "event: connected",
            'data: {"session_id":"abc"}',
            "",
            "event: workspace_changed",
            'data: {"latest_event_id":1}',
            "",
            "event: workspace_changed",
            'data: {"latest_event_id":2}',
            "",
        ]
    )
    frames = list(observer._iter_sse(resp))
    assert len(frames) == 3
    assert frames[0] == ("connected", '{"session_id":"abc"}')
    assert frames[1] == ("workspace_changed", '{"latest_event_id":1}')
    assert frames[2] == ("workspace_changed", '{"latest_event_id":2}')


def test_sse_parser_ignores_comments():
    observer = _load_observer()
    resp = _FakeResponse(
        [
            ": keep-alive comment",
            "event: heartbeat",
            'data: {"timestamp":"2026-04-26T00:00:00Z"}',
            "",
        ]
    )
    frames = list(observer._iter_sse(resp))
    assert frames == [("heartbeat", '{"timestamp":"2026-04-26T00:00:00Z"}')]


def test_sse_parser_multiline_data():
    observer = _load_observer()
    resp = _FakeResponse(
        [
            "event: workspace_changed",
            "data: line1",
            "data: line2",
            "",
        ]
    )
    frames = list(observer._iter_sse(resp))
    assert frames == [("workspace_changed", "line1\nline2")]


def test_sse_parser_strips_leading_space_only():
    observer = _load_observer()
    resp = _FakeResponse(
        [
            "event:workspace_changed",
            "data:{}",
            "",
        ]
    )
    frames = list(observer._iter_sse(resp))
    # SSE spec: single leading space after colon is stripped; no space means
    # value is consumed as-is.
    assert frames == [("workspace_changed", "{}")]
