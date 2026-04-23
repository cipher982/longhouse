from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from uuid import uuid4

from zerg import observability


@dataclass
class _FakeIdentity:
    version: str = "0.2.0"
    commit: str = "b672fccae990c020de56139d38dcd9990bae7aa0"
    commit_short: str = "b672fcca"
    dirty: bool = False
    built_at: str = "2026-04-23T18:03:12Z"
    channel: str = "dev"

    @property
    def qualified_version(self) -> str:
        return "0.2.0-dev+b672fcca"


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


def test_build_resource_attributes_uses_build_identity_and_settings(monkeypatch) -> None:
    fake_settings = SimpleNamespace(environment="dogfood", app_mode=SimpleNamespace(value="dev"))
    monkeypatch.setattr(observability, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(observability.build_info, "load", lambda: _FakeIdentity())
    monkeypatch.setattr(observability.socket, "gethostname", lambda: "test-host")

    attrs = observability._build_resource_attributes()

    assert attrs["service.name"] == "longhouse-runtime"
    assert attrs["service.version"] == "0.2.0"
    assert attrs["service.instance.id"] == "test-host"
    assert attrs["deployment.environment.name"] == "dogfood"
    assert attrs["longhouse.app_mode"] == "dev"
    assert attrs["longhouse.build.commit"] == "b672fcca"
    assert attrs["longhouse.build.qualified_version"] == "0.2.0-dev+b672fcca"


def test_otlp_endpoint_detection_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert observability._otlp_endpoint_configured() is False

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
    assert observability._otlp_endpoint_configured() is True

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces")
    assert observability._otlp_endpoint_configured() is True


def test_set_span_attributes_normalizes_datetimes_and_uuids() -> None:
    span = _FakeSpan()
    observed_at = datetime(2026, 4, 23, 20, 15, tzinfo=timezone.utc)
    request_id = uuid4()

    observability.set_span_attributes(
        span,
        {
            "longhouse.turn.request_id": request_id,
            "longhouse.turn.observed_at": observed_at,
            "longhouse.turn.phase_ms.total": 12.5,
            "longhouse.turn.missing": None,
        },
    )

    assert span.attributes["longhouse.turn.request_id"] == str(request_id)
    assert span.attributes["longhouse.turn.observed_at"] == observed_at.isoformat()
    assert span.attributes["longhouse.turn.phase_ms.total"] == 12.5
    assert "longhouse.turn.missing" not in span.attributes
