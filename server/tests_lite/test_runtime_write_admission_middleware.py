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
            return False, {"code": "runtime_draining", "path": path, "retryable": True}
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
    assert refused.json()["code"] == "runtime_draining"
    assert client.get("/big").status_code == 200


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

    assert [m for m in app.user_middleware if m.cls is BaseHTTPMiddleware] == []
