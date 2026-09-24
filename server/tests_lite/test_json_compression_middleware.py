import gzip

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.responses import Response
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from zerg.middleware.json_compression import JSONCompressionMiddleware
from zerg.middleware.json_compression import accepts_gzip

BIG = {"rows": [{"id": index, "title": "session title " * 4} for index in range(200)]}


def _client() -> TestClient:
    async def big_json(request):
        return JSONResponse(BIG)

    async def small_json(request):
        return JSONResponse({"ok": True})

    async def events(request):
        async def stream():
            yield b"data: " + b"x" * 4000 + b"\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def partial(request):
        return Response(b"y" * 4000, status_code=206, media_type="application/json", headers={"Content-Range": "bytes 0-3999/8000"})

    async def image(request):
        return Response(b"z" * 4000, media_type="image/png")

    async def encoded(request):
        return Response(gzip.compress(b"{}" * 2000), media_type="application/json", headers={"Content-Encoding": "gzip"})

    async def streamed_json(request):
        async def stream():
            yield b"[" + b"1," * 2000
            yield b"1]"

        return StreamingResponse(stream(), media_type="application/json")

    app = Starlette(
        routes=[
            Route("/big", big_json),
            Route("/small", small_json),
            Route("/events", events),
            Route("/partial", partial),
            Route("/image", image),
            Route("/encoded", encoded),
            Route("/streamed", streamed_json),
        ]
    )
    app.add_middleware(JSONCompressionMiddleware)
    return TestClient(app)


def test_large_json_is_gzipped_for_a_client_that_accepts_it():
    response = _client().get("/big", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert int(response.headers["content-length"]) < 2000
    assert "Accept-Encoding" in response.headers["vary"]
    assert response.json() == BIG


def test_client_without_gzip_gets_identity_bytes():
    response = _client().get("/big", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in response.headers
    assert response.json() == BIG


def test_everything_else_passes_through_untouched():
    client = _client()
    for path in ("/small", "/events", "/partial", "/image", "/streamed"):
        response = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in response.headers, path
    encoded = client.get("/encoded", headers={"Accept-Encoding": "gzip"})
    assert encoded.headers["content-encoding"] == "gzip"
    assert encoded.content == b"{}" * 2000  # decoded once, not twice


def test_accept_encoding_parsing_honours_zero_quality():
    def scope(value: str) -> dict:
        return {"headers": [(b"accept-encoding", value.encode())]}

    assert accepts_gzip(scope("gzip, deflate, br"))
    assert accepts_gzip(scope("br;q=1.0, gzip;q=0.8"))
    assert accepts_gzip(scope("*"))
    assert not accepts_gzip(scope("gzip;q=0"))
    assert not accepts_gzip(scope("identity"))
    assert not accepts_gzip({"headers": []})
