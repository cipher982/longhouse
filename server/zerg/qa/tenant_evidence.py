"""What a disposable Runtime Host held when a wait on its ingest expired.

A producer that fails with ``waited_on: longhouse_ingest`` says Longhouse never
served what the provider wrote. Whether that is projection lag or an ingest that
never committed depends on what the tenant held at that moment, and the tenant
is deleted after the run. This captures it first, into one JSON file in the
run's evidence root: the raw-object manifest (what ingest committed), the
storage-v2 rendered events and the forensic events the canary polled, the
projection, the served state diagnostics, and the tail of the local engine
log (what the engine says it shipped).

Every read is independent and bounded. A read that fails records its error and
the capture goes on: this runs inside a failure path and must never replace the
failure it documents. The Runtime Host exposes no log endpoint, so the host's
own server log tail is not here; the file says so explicitly rather than
leaving it absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from zerg.qa.wait_kind import LONGHOUSE_INGEST
from zerg.qa.wait_kind import waited_on

FILE_NAME = "tenant-evidence.json"
ENGINE_LOG_TAIL_BYTES = 64 * 1024
MANIFEST_PAGE_LIMIT = 512
MANIFEST_MAX_PAGES = 8
_REQUEST_TIMEOUT = 15.0


def should_capture(error: BaseException | None) -> bool:
    """Only a failed wait on Longhouse's ingest leaves a tenant worth reading."""

    return waited_on(error) == LONGHOUSE_INGEST


def _get(url: str, headers: dict[str, str], path: str, params: dict[str, Any] | None = None) -> Any:
    try:
        response = httpx.get(f"{url}{path}", headers=headers, params=params, timeout=_REQUEST_TIMEOUT)
    except httpx.HTTPError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:4000]
    if response.is_error:
        return {"error": f"HTTP {response.status_code}", "body": body}
    return body


def _manifest(url: str, headers: dict[str, str], session_id: str) -> dict[str, Any]:
    objects: list[Any] = []
    meta: dict[str, Any] = {}
    cursor: str | None = None
    for _ in range(MANIFEST_MAX_PAGES):
        params: dict[str, Any] = {"limit": MANIFEST_PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        page = _get(url, headers, f"/api/agents/sessions/{session_id}/objects/manifest", params)
        if not isinstance(page, dict) or "error" in page:
            return {"objects": objects, "error": page}
        objects.extend(page.get("objects") or [])
        meta = {k: v for k, v in page.items() if k not in ("objects", "next_cursor")}
        cursor = page.get("next_cursor")
        if not cursor:
            return {**meta, "objects": objects, "complete": True}
    return {**meta, "objects": objects, "complete": False}


def _engine_log_tail(log_dir: Path | None) -> dict[str, Any]:
    if log_dir is None or not log_dir.is_dir():
        return {"error": "engine log directory not found"}
    logs = sorted((p for p in log_dir.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime)
    if not logs:
        return {"error": "no engine log files"}
    newest = logs[-1]
    with newest.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - ENGINE_LOG_TAIL_BYTES))
        text = handle.read().decode("utf-8", errors="replace")
    return {"file": newest.name, "tail": text}


def capture_tenant_evidence(
    *,
    url: str,
    token: str,
    session_id: str,
    artifact_root: Path,
    engine_log_dir: Path | None = None,
    extra: Callable[[], dict[str, Any]] | None = None,
) -> Path | None:
    """Write ``tenant-evidence.json`` into ``artifact_root`` and return its path.

    Never raises: returns None when even the file cannot be written.
    """

    base = url.rstrip("/")
    headers = {"X-Agents-Token": token}
    sid = session_id
    try:
        evidence: dict[str, Any] = {
            "schema_version": 1,
            "captured_at": datetime.now(UTC).isoformat(),
            "session_id": sid,
            "ingest_receipts_raw_object_manifest": _manifest(base, headers, sid),
            "storage_v2_events_tail": _get(
                base, headers, f"/api/agents/storage/v2/sessions/{sid}/events", {"anchor": "tail", "limit": 100}
            ),
            "storage_v2_events_start": _get(
                base, headers, f"/api/agents/storage/v2/sessions/{sid}/events", {"anchor": "start", "limit": 100}
            ),
            "served_events_forensic": _get(
                base,
                headers,
                f"/api/agents/sessions/{sid}/events",
                {"context_mode": "forensic", "branch_mode": "head", "limit": 100},
            ),
            "projection_tail": _get(base, headers, f"/api/agents/sessions/{sid}/projection", {"anchor": "tail", "limit": 100}),
            "session": _get(base, headers, f"/api/agents/sessions/{sid}"),
            "state_diagnostics": _get(base, headers, f"/api/agents/sessions/{sid}/state-diagnostics"),
            "engine_log_tail": _engine_log_tail(engine_log_dir),
            "server_log_tail": {"error": "the Runtime Host API exposes no log endpoint; not capturable from the verifier"},
        }
        if extra is not None:
            evidence["extra"] = extra()
        path = artifact_root / FILE_NAME
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n")
        return path
    except Exception:  # noqa: BLE001 - evidence capture must not replace the failure it documents
        return None
