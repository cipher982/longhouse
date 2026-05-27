"""Snapshot-driven tests for ``build_session_runtime_display``.

Each snapshot in ``runtime_display_snapshots/`` is a self-contained scenario
expressed as JSON: ``input`` (the runtime view, capabilities, binding facts
needed to build the projection) and ``expected_runtime_display`` (the full
projection).

To regenerate after an intentional contract change::

    UPDATE_RUNTIME_DISPLAY_SNAPSHOTS=1 uv run pytest \
        server/tests_lite/test_runtime_display_snapshots.py

Read the diff. If the change is wrong, the failing snapshot tells you which
scenario regressed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import build_session_runtime_display

SNAPSHOT_DIR = Path(__file__).parent / "runtime_display_snapshots"
UPDATE_ENV = "UPDATE_RUNTIME_DISPLAY_SNAPSHOTS"


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _serialize_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


_RUNTIME_VIEW_DT_FIELDS = (
    "phase_started_at",
    "last_progress_at",
    "presence_updated_at",
    "last_live_at",
    "timeline_anchor_at",
)


def _runtime_view_from_input(payload: dict[str, Any]) -> SessionRuntimeView:
    values = dict(payload)
    for field in _RUNTIME_VIEW_DT_FIELDS:
        if field in values:
            values[field] = _parse_dt(values[field])
    return SessionRuntimeView(**values)


def _capabilities_from_input(payload: dict[str, Any]) -> KernelSessionCapabilities:
    return KernelSessionCapabilities(**payload)


def _build_kwargs(input_payload: dict[str, Any]) -> dict[str, Any]:
    runtime_view = _runtime_view_from_input(input_payload["runtime_view"])
    capabilities = _capabilities_from_input(input_payload["capabilities"])
    kwargs: dict[str, Any] = {
        "runtime_view": runtime_view,
        "capabilities": capabilities,
        "ended_at": _parse_dt(input_payload.get("ended_at")),
    }
    optional_passthrough = (
        "binding_host_state",
        "binding_terminal_reason",
        "user_messages",
        "assistant_messages",
        "has_visible_transcript_preview",
        "has_pending_response_turn",
    )
    for field in optional_passthrough:
        if field in input_payload:
            kwargs[field] = input_payload[field]
    if "last_activity_at" in input_payload:
        kwargs["last_activity_at"] = _parse_dt(input_payload["last_activity_at"])
    if "now" in input_payload:
        kwargs["now"] = _parse_dt(input_payload["now"])
    return kwargs


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_dt(value)
    if isinstance(value, dict):
        return {k: _normalize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_for_json(v) for v in value]
    return value


def _projection_to_jsonable(display) -> dict[str, Any]:
    return _normalize_for_json(asdict(display))


def _load_snapshots() -> list[tuple[str, Path]]:
    files = sorted(SNAPSHOT_DIR.glob("*.json"))
    return [(path.stem, path) for path in files]


SNAPSHOTS = _load_snapshots()


@pytest.mark.parametrize(
    "name,path",
    SNAPSHOTS,
    ids=[name for name, _ in SNAPSHOTS],
)
def test_runtime_display_snapshot(name: str, path: Path) -> None:
    payload = json.loads(path.read_text())
    kwargs = _build_kwargs(payload["input"])
    display = build_session_runtime_display(**kwargs)
    actual = _projection_to_jsonable(display)

    if os.environ.get(UPDATE_ENV):
        payload["expected_runtime_display"] = actual
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return

    expected = payload["expected_runtime_display"]
    assert actual == expected, (
        f"Snapshot {name} drifted. Re-run with {UPDATE_ENV}=1 if the change is intentional."
    )


def test_snapshot_directory_not_empty() -> None:
    assert SNAPSHOTS, "No runtime_display snapshots found; the contract is unenforced."
