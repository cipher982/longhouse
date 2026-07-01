"""Typed Runtime Host write-backpressure responses."""

from __future__ import annotations

from typing import NoReturn

from fastapi import HTTPException
from fastapi import status

HOT_WRITE_BACKPRESSURE_KIND = "hot_write_backpressure"
HOT_WRITE_BACKPRESSURE_DETAIL = "Longhouse live write lane is busy; retry shortly"
HOT_WRITE_RETRY_AFTER_SECONDS = 2


def hot_write_backpressure_headers(
    ws: object,
    *,
    admission_state: str = "queue_timeout",
    retry_after_seconds: int = HOT_WRITE_RETRY_AFTER_SECONDS,
) -> dict[str, str]:
    """Return typed headers for non-ingest hot write pressure."""

    headers = {
        "Retry-After": str(retry_after_seconds),
        "X-Longhouse-Write-Backpressure": HOT_WRITE_BACKPRESSURE_KIND,
        "X-Longhouse-Write-Error-Kind": HOT_WRITE_BACKPRESSURE_KIND,
        "X-Longhouse-Write-Lane": "hot",
        "X-Longhouse-Write-Admission-State": admission_state,
    }
    queue_depth = int(getattr(ws, "queue_depth", 0) or 0)
    if queue_depth > 0:
        headers["X-Longhouse-Writer-Queue-Depth"] = str(queue_depth)
    active_label = str(getattr(ws, "active_label", "") or "")
    if active_label:
        headers["X-Longhouse-Writer-Active-Label"] = active_label
    active_age_ms = float(getattr(ws, "active_age_ms", 0.0) or 0.0)
    if active_age_ms > 0:
        headers["X-Longhouse-Writer-Active-Age-Ms"] = f"{active_age_ms:.1f}"
    return headers


def raise_hot_write_backpressure(
    ws: object,
    *,
    admission_state: str = "queue_timeout",
    retry_after_seconds: int = HOT_WRITE_RETRY_AFTER_SECONDS,
) -> NoReturn:
    """Raise a typed 503 for live-route write queue pressure."""

    headers = hot_write_backpressure_headers(
        ws,
        admission_state=admission_state,
        retry_after_seconds=retry_after_seconds,
    )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=HOT_WRITE_BACKPRESSURE_DETAIL,
        headers=headers,
    )
