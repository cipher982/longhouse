from fastapi import FastAPI


def test_error_handler_sets_origin_when_allowed(unauthenticated_client, monkeypatch):
    # Patch allowed origins to a strict list for this test
    import zerg.main as main

    monkeypatch.setattr(main, "cors_origins", ["http://allowed.test"])  # type: ignore[attr-defined]

    # Register a throw route to trigger the error handler
    app: FastAPI = main.app

    @app.get("/boom-test")
    async def boom_test():  # noqa: D401
        raise RuntimeError("boom")

    client = unauthenticated_client
    r = client.get("/boom-test", headers={"Origin": "http://allowed.test"})
    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == "http://allowed.test"
    assert r.headers.get("vary") == "Origin"


def test_error_handler_omits_origin_when_disallowed(unauthenticated_client, monkeypatch):
    import zerg.main as main

    monkeypatch.setattr(main, "cors_origins", ["http://allowed.test"])  # type: ignore[attr-defined]

    app: FastAPI = main.app

    @app.get("/boom-test-2")
    async def boom_test_2():  # noqa: D401
        raise RuntimeError("boom2")

    client = unauthenticated_client
    r = client.get("/boom-test-2", headers={"Origin": "http://evil.test"})
    assert r.status_code == 500
    # No CORS allow-origin header for disallowed origins
    assert r.headers.get("access-control-allow-origin") is None
    assert r.headers.get("vary") == "Origin"
