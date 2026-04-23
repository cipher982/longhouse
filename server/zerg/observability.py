"""Minimal OpenTelemetry bootstrap and span helpers for Longhouse.

MVP rules:
- manual spans only; no blanket framework auto-instrumentation
- OTLP export is opt-in via standard OTEL endpoint env vars
- no prompt/response payload capture; only IDs, counts, sizes, and timings
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

from zerg import build_info
from zerg.config import get_settings

logger = logging.getLogger(__name__)

_STATE_UNCONFIGURED = "unconfigured"
_STATE_CONFIGURED = "configured"
_STATE_DISABLED = "disabled"

_state = _STATE_UNCONFIGURED
_state_lock = threading.Lock()


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _otlp_endpoint_configured() -> bool:
    return bool(
        (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "") or "").strip() or (os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "") or "").strip()
    )


def _build_resource_attributes() -> dict[str, bool | str]:
    settings = get_settings()
    identity = build_info.load()
    return {
        "service.name": "longhouse-runtime",
        "service.version": identity.version,
        "service.instance.id": socket.gethostname(),
        "deployment.environment.name": settings.environment or settings.app_mode.value,
        "longhouse.app_mode": settings.app_mode.value,
        "longhouse.build.channel": identity.channel,
        "longhouse.build.commit": identity.commit_short,
        "longhouse.build.dirty": identity.dirty,
        "longhouse.build.qualified_version": identity.qualified_version,
    }


def _normalize_attribute_value(value: object) -> bool | float | int | str | tuple[bool | float | int | str, ...]:
    if isinstance(value, (bool, float, int, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        normalized = tuple(_normalize_attribute_value(item) for item in value if item is not None)
        if normalized and all(isinstance(item, type(normalized[0])) for item in normalized):
            return normalized
        return tuple(str(item) for item in normalized)
    return str(value)


def configure_observability(*, span_processors: list[SpanProcessor] | None = None) -> bool:
    """Configure the process-global tracer provider once.

    Returns True when tracing is active in-process. Export remains opt-in:
    without an explicit OTLP endpoint env var we install no exporter.
    """

    global _state

    if _state == _STATE_DISABLED:
        return False
    if _state == _STATE_CONFIGURED:
        return True

    with _state_lock:
        if _state == _STATE_DISABLED:
            return False
        if _state == _STATE_CONFIGURED:
            return True
        if _truthy(os.getenv("OTEL_SDK_DISABLED")):
            _state = _STATE_DISABLED
            logger.info("OpenTelemetry disabled via OTEL_SDK_DISABLED")
            return False

        current_provider = trace.get_tracer_provider()
        if current_provider.__class__.__name__ != "ProxyTracerProvider":
            _state = _STATE_CONFIGURED
            logger.info("OpenTelemetry tracer provider already configured; reusing existing provider")
            return True

        provider = TracerProvider(resource=Resource.create(_build_resource_attributes()))
        processors = list(span_processors or [])
        if not processors and _otlp_endpoint_configured():
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            processors.append(BatchSpanProcessor(OTLPSpanExporter()))
        for processor in processors:
            provider.add_span_processor(processor)

        trace.set_tracer_provider(provider)
        _state = _STATE_CONFIGURED

        if processors:
            logger.info("OpenTelemetry configured with %d span processor(s)", len(processors))
        else:
            logger.info(
                "OpenTelemetry configured without an exporter; set OTEL_EXPORTER_OTLP_ENDPOINT "
                "or OTEL_EXPORTER_OTLP_TRACES_ENDPOINT to enable OTLP export"
            )
        return True


def get_tracer(name: str) -> trace.Tracer:
    configure_observability()
    return trace.get_tracer(name)


def set_span_attributes(span: Span, attributes: Mapping[str, object | None]) -> None:
    for key, value in attributes.items():
        if value is None:
            continue
        span.set_attribute(key, _normalize_attribute_value(value))


def mark_span_error(span: Span, error: Exception | str) -> None:
    description = str(error)
    if isinstance(error, Exception):
        span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, description))


def shutdown_observability() -> None:
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if callable(shutdown):
        shutdown()
