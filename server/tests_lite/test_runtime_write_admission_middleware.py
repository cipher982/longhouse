from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import zerg.services.runtime_admission as admission_module
from zerg.middleware.json_compression import JSONCompressionMiddleware
from zerg.middleware.runtime_write_admission import RuntimeWriteAdmissionMiddleware

BIG = {"rows": [{"id": index, "title": "session title " * 4} for index in range(200)]}


class _Runtime:
    def __init__(self, *, open_: bool = True):
        self.open = open_
        self.in_flight = 0
        self.admitted = 0

    async def try_admit(self, *, path):
        if not self.open:
            return False, {
                "code": "runtime_restarting",
                "retryable": True,
                "runtime_epoch": "runtime-test",
                "admission": "draining",
                "claim": {"type": "host.lifecycle", "state": "updating"},
                "path": path,
            }
        self.in_flight += 1
        self.admitted += 1
        return True, {}

    async def release(self):
        self.in_flight -= 1


def _client(monkeypatch, runtime: _Runtime) -> tuple[TestClient, list[int]]:
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    seen_in_flight_while_streaming: list[int] = []

    async def big_json(request):
        return JSONResponse(BIG)

    async def streamed(request):
        async def stream():
            seen_in_flight_while_streaming.append(runtime.in_flight)
            yield b"data: x\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/big", big_json, methods=["GET", "POST"]), Route("/stream", streamed, methods=["POST"])])
    app.add_middleware(RuntimeWriteAdmissionMiddleware)
    app.add_middleware(JSONCompressionMiddleware)
    return TestClient(app), seen_in_flight_while_streaming


def test_json_behind_the_fence_still_leaves_gzipped(monkeypatch):
    runtime = _Runtime()
    client, _ = _client(monkeypatch, runtime)
    for method in ("GET", "POST"):
        response = client.request(method, "/big", headers={"Accept-Encoding": "gzip"})
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert response.json() == BIG
    assert runtime.admitted == 1
    assert runtime.in_flight == 0


def test_draining_runtime_refuses_writes_and_still_serves_reads(monkeypatch):
    runtime = _Runtime(open_=False)
    client, _ = _client(monkeypatch, runtime)
    refused = client.post("/big")
    assert refused.status_code == 503
    assert refused.json()["code"] == "runtime_restarting"
    assert refused.headers["retry-after"] == "2"
    assert refused.json()["admission"] == "draining"
    assert client.get("/big").status_code == 200


def test_rejected_asgi_request_consumes_a_still_sending_body_before_k1_response(monkeypatch):
    import asyncio
    import json

    runtime = _Runtime(open_=False)
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    incoming = [
        {"type": "http.request", "body": b'{"first":', "more_body": True},
        {"type": "http.request", "body": b'"part","last":true}', "more_body": False},
    ]
    receive_count = 0
    sent = []

    async def endpoint(scope, receive, send):
        raise AssertionError("a fenced request must not reach the app")

    async def receive():
        nonlocal receive_count
        if receive_count < len(incoming):
            message = incoming[receive_count]
            receive_count += 1
            return message
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            assert receive_count == len(incoming)
        sent.append(message)

    middleware = RuntimeWriteAdmissionMiddleware(endpoint)
    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/write", "headers": []},
            receive,
            send,
        )
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    headers = dict(start["headers"])
    assert start["status"] == 503
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"retry-after"] == b"2"
    payload = json.loads(body)
    assert payload["code"] == "runtime_restarting"
    assert payload["retryable"] is True
    assert payload["runtime_epoch"] == "runtime-test"
    assert payload["admission"] == "draining"
    assert payload["claim"]["type"] == "host.lifecycle"


def test_rejected_stalled_upload_gets_typed_response_and_closes_connection(monkeypatch):
    import asyncio
    import json

    from zerg.middleware import runtime_write_admission as admission_middleware

    runtime = _Runtime(open_=False)
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    monkeypatch.setattr(admission_middleware, "_REQUEST_BODY_DISCARD_TIMEOUT_SECONDS", 0.01)
    receive_count = 0
    sent = []

    async def endpoint(_scope, _receive, _send):
        raise AssertionError("a fenced request must not reach the app")

    async def receive():
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return {"type": "http.request", "body": b"partial", "more_body": True}
        await asyncio.Future()

    async def send(message):
        sent.append(message)

    middleware = RuntimeWriteAdmissionMiddleware(endpoint)
    asyncio.run(
        asyncio.wait_for(
            middleware(
                {"type": "http", "method": "POST", "path": "/write", "headers": []},
                receive,
                send,
            ),
            timeout=0.5,
        )
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    headers = dict(start["headers"])
    assert receive_count == 2
    assert start["status"] == 503
    assert headers[b"retry-after"] == b"2"
    assert headers[b"connection"] == b"close"
    payload = json.loads(body)
    assert payload["code"] == "runtime_restarting"
    assert payload["retryable"] is True


def test_draining_browser_websocket_is_rejected_with_typed_unavailability(monkeypatch):
    import asyncio
    import json

    runtime = _Runtime(open_=False)
    monkeypatch.setattr(admission_module, "runtime_admission", lambda: runtime)
    app_called = False
    sent = []

    async def endpoint(_scope, _receive, _send):
        nonlocal app_called
        app_called = True

    async def receive():
        raise AssertionError("a rejected upgrade must not read WebSocket messages")

    async def send(message):
        sent.append(message)

    middleware = RuntimeWriteAdmissionMiddleware(endpoint)
    asyncio.run(
        middleware(
            {
                "type": "websocket",
                "path": "/api/ws",
                "extensions": {"websocket.http.response": {}},
            },
            receive,
            send,
        )
    )

    start = next(message for message in sent if message["type"] == "websocket.http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "websocket.http.response.body")
    headers = dict(start["headers"])
    assert not app_called
    assert start["status"] == 503
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"retry-after"] == b"2"
    assert json.loads(body)["code"] == "runtime_restarting"


def test_a_streamed_write_stops_counting_once_its_response_starts(monkeypatch):
    runtime = _Runtime()
    client, seen = _client(monkeypatch, runtime)
    assert client.post("/stream").status_code == 200
    assert seen == [0]
    assert runtime.in_flight == 0


def test_the_runtime_host_registers_no_base_http_middleware(monkeypatch):
    # A BaseHTTPMiddleware re-streams every response, and the JSON compressor
    # passes streamed bodies through: one of these turns off gzip for the API.
    from cryptography.fernet import Fernet

    for key, value in {
        "DATABASE_URL": "sqlite://",
        "TESTING": "1",
        "AUTH_DISABLED": "1",
        "FERNET_SECRET": Fernet.generate_key().decode(),
        "JWT_SECRET": "test-jwt-secret-1234",
    }.items():
        monkeypatch.setenv(key, value)
    from zerg.main import app

    assert [m for m in app.user_middleware if isinstance(m.cls, type) and issubclass(m.cls, BaseHTTPMiddleware)] == []
