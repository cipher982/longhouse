from __future__ import annotations

from types import SimpleNamespace

from zerg.cli import acquisition


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


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


def test_once_telemetry_does_not_mark_when_opted_out(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))
    monkeypatch.setenv("LONGHOUSE_TELEMETRY", "0")

    calls = []
    monkeypatch.setattr(acquisition.httpx, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    acquisition.emit_acquisition_event_once("runtime-first-start", "runtime_first_start")

    assert calls == []
    assert not (tmp_path / ".longhouse" / "acquisition-events.json").exists()


def test_once_telemetry_marks_only_after_success(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))
    monkeypatch.delenv("LONGHOUSE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)

    calls = []

    def fake_post(url, *, json, timeout, headers):
        calls.append(json)
        return _Response(503 if len(calls) == 1 else 202)

    monkeypatch.setattr(acquisition.httpx, "post", fake_post)

    acquisition.emit_acquisition_event_once("runtime-first-start", "runtime_first_start")
    assert not (tmp_path / ".longhouse" / "acquisition-events.json").exists()

    acquisition.emit_acquisition_event_once("runtime-first-start", "runtime_first_start")
    marker = tmp_path / ".longhouse" / "acquisition-events.json"
    assert marker.exists()
    assert len(calls) == 2
    assert [call["event_name"] for call in calls] == ["runtime_first_start", "runtime_first_start"]
    assert "runtime-first-start" in marker.read_text(encoding="utf-8")


def test_install_metadata_event_classifies_package_ref_kind(monkeypatch, tmp_path):
    monkeypatch.setenv("LONGHOUSE_HOME", str(tmp_path / ".longhouse"))
    monkeypatch.delenv("LONGHOUSE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CI", raising=False)

    calls = []

    def fake_post(url, *, json, timeout, headers):
        calls.append(json)
        return _Response(202)

    monkeypatch.setattr(acquisition.httpx, "post", fake_post)

    acquisition.emit_install_metadata_event(
        SimpleNamespace(
            install_method="uv",
            install_source="pypi",
            channel="stable",
            package_ref="longhouse==0.1.26",
        )
    )
    acquisition.emit_install_metadata_event(
        SimpleNamespace(
            install_method="uv",
            install_source="custom",
            channel="stable",
            package_ref="https://example.invalid/longhouse.whl",
        )
    )
    acquisition.emit_install_metadata_event(
        SimpleNamespace(
            install_method="uv",
            install_source="custom",
            channel="stable",
            package_ref="../dist/longhouse.whl",
        )
    )

    assert [call["event_name"] for call in calls] == ["install_success"] * 3
    assert [call["props"]["package_ref_kind"] for call in calls] == [
        "pypi_version",
        "url",
        "local_path",
    ]
    assert all("longhouse.whl" not in str(call["props"]) for call in calls)
