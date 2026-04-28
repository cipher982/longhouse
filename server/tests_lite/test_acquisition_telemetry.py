from __future__ import annotations

from zerg.cli import acquisition


def test_acquisition_telemetry_respects_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))
    monkeypatch.setenv("LONGHOUSE_TELEMETRY", "0")

    calls = []
    monkeypatch.setattr(acquisition.httpx, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    acquisition.emit_acquisition_event("install_success", background=False)

    assert calls == []
    assert not (tmp_path / ".longhouse" / "install-id").exists()


def test_acquisition_telemetry_posts_anonymous_payload(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))
    monkeypatch.delenv("LONGHOUSE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)

    calls = []

    def fake_post(url, *, json, timeout, headers):
        calls.append({"url": url, "json": json, "timeout": timeout, "headers": headers})

    monkeypatch.setattr(acquisition.httpx, "post", fake_post)

    acquisition.emit_acquisition_event(
        "runtime_first_start",
        command="serve",
        topology="local_runtime",
        props={"daemon": False},
        background=False,
    )

    assert len(calls) == 1
    payload = calls[0]["json"]
    assert payload["event_name"] == "runtime_first_start"
    assert payload["install_id"]
    assert payload["command"] == "serve"
    assert payload["topology"] == "local_runtime"
    assert "path" not in payload
    assert "token" not in payload
