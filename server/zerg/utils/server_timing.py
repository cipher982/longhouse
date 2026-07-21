from __future__ import annotations

from contextlib import contextmanager
from time import monotonic
from typing import Iterator

from fastapi import Response

_PRODUCT_READ_SURFACES = frozenset({"timeline", "session_detail", "search", "recall", "raw_export", "worklog"})


def _sanitize_metric_name(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in value.strip())


class ServerTimingRecorder:
    """Collect lightweight per-request timing spans for API responses."""

    def __init__(self, *, surface: str | None = None) -> None:
        if surface is not None and surface not in _PRODUCT_READ_SURFACES:
            raise ValueError(f"Unsupported product read surface: {surface}")
        self._surface = surface
        self._metrics: list[tuple[str, float]] = []

    @property
    def product_surface(self) -> str | None:
        return self._surface

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        started_at = monotonic()
        try:
            yield
        finally:
            self.record(name, (monotonic() - started_at) * 1000.0)

    def record(self, name: str, duration_ms: float) -> None:
        metric_name = _sanitize_metric_name(name)
        if not metric_name:
            return
        duration_ms = max(duration_ms, 0.0)
        self._metrics.append((metric_name, duration_ms))
        if self._surface is not None:
            from zerg.metrics import product_read_stage_seconds

            product_read_stage_seconds.labels(self._surface, metric_name).observe(duration_ms / 1000.0)

    def header_value(self) -> str | None:
        if not self._metrics:
            return None
        return ", ".join(f"{name};dur={max(duration_ms, 0.1):.1f}" for name, duration_ms in self._metrics)

    def apply(self, response: Response | None) -> None:
        if response is None:
            return
        header_value = self.header_value()
        if header_value:
            response.headers["Server-Timing"] = header_value
