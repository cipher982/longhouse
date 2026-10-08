"""Unit test for the canary observer's SSE frame parser.

The parser has to handle the spec's weird edge cases: multi-line data,
: comments, blank-line dispatch, missing optional fields. Freeze the
behavior so a future change doesn't silently break the observer.
"""

import importlib.util
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest


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


def _run_observer(monkeypatch, tmp_path, handler):
    observer = _load_observer()
    session_file = tmp_path / "session-id"
    session_file.write_text(str(uuid4()))
    monkeypatch.setattr(observer, "SESSION_ID_FILE", session_file)
    monkeypatch.setattr(observer.signal, "signal", lambda *_args: None)
    monkeypatch.setenv("LONGHOUSE_CANARY_URL", "http://canary.test")
    monkeypatch.setenv("LONGHOUSE_CANARY_TOKEN", "synthetic-canary-token")
    monkeypatch.setenv("LONGHOUSE_AGENTS_TOKEN", "synthetic-agents-token")
    client = httpx.Client
    monkeypatch.setattr(
        observer.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )
    return observer.main()


def test_permanent_stream_refusal_exits_without_retry(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) > 1:
            pytest.fail("a permanent stream refusal must not be retried")
        return httpx.Response(401, stream=httpx.ByteStream(b"unauthorized"))

    assert _run_observer(monkeypatch, tmp_path, handler) == 3
    assert len(requests) == 1


def test_malformed_marker_never_reports_a_delivery(monkeypatch, tmp_path):
    def handler(request):
        if request.method != "GET":
            pytest.fail("a marker without a producer sequence must not report delivery")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=httpx.ByteStream(b'event: canary_observation\ndata: {"pubsub_seq":7,"server_now_ms":1800000000035}\n\n'),
        )

    assert _run_observer(monkeypatch, tmp_path, handler) == 3


def test_duplicate_producer_sequence_is_not_a_second_delivery(monkeypatch, tmp_path):
    emitted_at_ms = int(time.time() * 1000) - 10
    markers = [
        {
            "canary_seq": 41,
            "canary_emitted_at_ms": emitted_at_ms,
            "server_fanout_at_ms": emitted_at_ms + 2,
            "server_now_ms": emitted_at_ms + 3,
            "pubsub_seq": cursor,
        }
        for cursor in (7, 8)
    ]
    deliveries = []

    def handler(request):
        if request.method == "GET":
            frames = "".join(f"event: canary_observation\ndata: {json.dumps(marker)}\n\n" for marker in markers)
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=httpx.ByteStream(frames.encode()),
            )
        delivery = json.loads(request.content)
        deliveries.append(delivery["canary_seq"])
        return httpx.Response(200, json={"ok": True, "hop": "sse", "seq": delivery["canary_seq"]})

    assert _run_observer(monkeypatch, tmp_path, handler) == 3
    assert deliveries == [41]


def test_observer_waits_for_initial_server_bootstrap(monkeypatch, tmp_path):
    stream_requests = 0
    deliveries = []

    def handler(request):
        nonlocal stream_requests
        if request.method == "GET":
            stream_requests += 1
            if stream_requests == 1:
                return httpx.Response(404, stream=httpx.ByteStream(b"not bootstrapped"))
            if stream_requests > 2:
                return httpx.Response(401, stream=httpx.ByteStream(b"unauthorized"))
            emitted_at_ms = int(time.time() * 1000) - 10
            marker = {
                "canary_seq": 41,
                "canary_emitted_at_ms": emitted_at_ms,
                "server_fanout_at_ms": emitted_at_ms + 2,
                "server_now_ms": emitted_at_ms + 3,
                "pubsub_seq": 7,
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=httpx.ByteStream(f"event: canary_observation\ndata: {json.dumps(marker)}\n\n".encode()),
            )
        delivery = json.loads(request.content)
        deliveries.append(delivery["canary_seq"])
        return httpx.Response(200, json={"ok": True, "hop": "sse", "seq": delivery["canary_seq"]})

    assert _run_observer(monkeypatch, tmp_path, handler) == 3
    assert deliveries == [41]


def test_hop_line_names_each_hop_and_sums_to_the_sla_latency():
    observer = _load_observer()
    line = observer._hop_line(41, 1_000, 1_150, 1_151, 1_230)
    assert line == "canary seq=41 total_ms=230 to_fanout_ms=150 fanout_to_sse_ms=1 to_observer_ms=79"
