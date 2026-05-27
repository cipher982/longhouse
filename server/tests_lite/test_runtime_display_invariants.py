"""Joint-state invariants for ``SessionRuntimeDisplay``.

These hand-curated rules express the constraints between axes documented in
``docs/specs/runtime-display-contract.md``. They are checked against every
snapshot in ``runtime_display_snapshots/`` so any new scenario must satisfy
the invariants. Hypothesis-driven property tests come later (see #13);
domain preconditions there should respect the same rules.

If you find an invariant fails because the reducer can legitimately produce
the "violating" state, fix the reducer or weaken the invariant — but
update the contract doc first.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")

from zerg.services.agents.kernel_capabilities import KernelSessionCapabilities
from zerg.services.session_runtime import EXPLICIT_CLOSED_TERMINAL_STATES
from zerg.services.session_runtime import SessionRuntimeView
from zerg.services.session_runtime_display import SessionRuntimeDisplay
from zerg.services.session_runtime_display import build_session_runtime_display

SNAPSHOT_DIR = Path(__file__).parent / "runtime_display_snapshots"

VALID_TRUTH_TIERS = {"managed-local", "fresh", "stale", "none"}
VALID_SIGNAL_TIERS = {
    "managed_local_transport",
    "semantic",
    "process_binding",
    "phase_signal",
    "transcript_progress",
    "none",
}
VALID_CONTROL_PATHS = {"managed", "unmanaged"}
VALID_ACTIVITY_RECENCY = {"live", "recent", "stale", "none"}
VALID_LIFECYCLES = {"open", "closed", "unknown"}
VALID_HOST_STATES = {"online", "stale", "offline", "unknown"}
VALID_TERMINAL_REASONS = {
    "session_ended",
    "user_closed",
    "process_gone",
    "host_expired",
    "provider_signal",
    None,
}
VALID_PRESENCE_STATES = {
    "thinking",
    "running",
    "idle",
    "needs_user",
    "blocked",
    "stalled",
    "syncing_transcript",
    None,
}
VALID_TONES = {
    "stalled",
    "blocked",
    "running",
    "thinking",
    "idle",
    "active",
    "inactive",
    "closed",
}


def _parse_dt(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_RUNTIME_VIEW_DT_FIELDS = (
    "phase_started_at",
    "last_progress_at",
    "presence_updated_at",
    "last_live_at",
    "timeline_anchor_at",
)


def _build_display_from_snapshot(snapshot_path: Path) -> SessionRuntimeDisplay:
    payload = json.loads(snapshot_path.read_text())
    rv = dict(payload["input"]["runtime_view"])
    for field in _RUNTIME_VIEW_DT_FIELDS:
        if field in rv:
            rv[field] = _parse_dt(rv[field])
    runtime_view = SessionRuntimeView(**rv)
    capabilities = KernelSessionCapabilities(**payload["input"]["capabilities"])
    kwargs = {
        "runtime_view": runtime_view,
        "capabilities": capabilities,
        "ended_at": _parse_dt(payload["input"].get("ended_at")),
    }
    for field in (
        "binding_host_state",
        "binding_terminal_reason",
        "user_messages",
        "assistant_messages",
        "has_visible_transcript_preview",
        "has_pending_response_turn",
    ):
        if field in payload["input"]:
            kwargs[field] = payload["input"][field]
    if "last_activity_at" in payload["input"]:
        kwargs["last_activity_at"] = _parse_dt(payload["input"]["last_activity_at"])
    if "now" in payload["input"]:
        kwargs["now"] = _parse_dt(payload["input"]["now"])
    return build_session_runtime_display(**kwargs)


def _all_displays():
    paths = sorted(SNAPSHOT_DIR.glob("*.json"))
    return [(path.stem, _build_display_from_snapshot(path)) for path in paths]


DISPLAYS = _all_displays()
INVARIANT_IDS = [name for name, _ in DISPLAYS]


@pytest.fixture(params=DISPLAYS, ids=INVARIANT_IDS)
def display(request) -> SessionRuntimeDisplay:
    _, value = request.param
    return value


def test_truth_tier_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.truth_tier in VALID_TRUTH_TIERS


def test_signal_tier_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.signal_tier in VALID_SIGNAL_TIERS


def test_control_path_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.control_path in VALID_CONTROL_PATHS


def test_activity_recency_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.activity_recency in VALID_ACTIVITY_RECENCY


def test_lifecycle_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.lifecycle in VALID_LIFECYCLES


def test_host_state_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.host_state in VALID_HOST_STATES


def test_terminal_reason_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.terminal_reason in VALID_TERMINAL_REASONS


def test_state_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.state in VALID_PRESENCE_STATES


def test_tone_is_valid(display: SessionRuntimeDisplay) -> None:
    assert display.tone in VALID_TONES


def test_is_live_equals_is_executing(display: SessionRuntimeDisplay) -> None:
    assert display.is_live == display.is_executing


def test_running_or_thinking_implies_executing(display: SessionRuntimeDisplay) -> None:
    if display.state in {"running", "thinking"}:
        assert display.is_executing


def test_managed_local_truth_implies_managed_control_path(display: SessionRuntimeDisplay) -> None:
    if display.truth_tier == "managed-local":
        assert display.control_path == "managed"
        assert display.is_managed_local_truth


def test_needs_attention_implies_blocked_and_open(display: SessionRuntimeDisplay) -> None:
    if display.needs_attention:
        assert display.state == "blocked"
        assert display.lifecycle != "closed"


def test_is_stalled_locks_state_and_tone(display: SessionRuntimeDisplay) -> None:
    if display.is_stalled:
        assert display.state == "stalled"
        assert display.tone == "stalled"


def test_closed_lifecycle_locks_outputs(display: SessionRuntimeDisplay) -> None:
    if display.lifecycle == "closed":
        assert display.terminal_reason is not None
        assert display.is_executing is False
        assert display.is_live is False
        assert display.needs_attention is False
        assert display.is_idle is True
        assert display.state is None
        assert display.headline == "Closed"
        assert display.phase_label == "Closed"
        assert display.tone == "closed"


def test_no_signal_constraints(display: SessionRuntimeDisplay) -> None:
    if display.has_signal is False:
        assert display.state is None
        assert display.truth_tier in {"stale", "none"}
        assert display.activity_recency in {"stale", "none"}


def test_syncing_transcript_excludes_idle_and_executing(display: SessionRuntimeDisplay) -> None:
    if display.state == "syncing_transcript":
        assert display.is_idle is False
        assert display.is_executing is False


def test_explicit_closed_lifecycle_uses_explicit_terminal_state() -> None:
    """If a snapshot's input has terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES,
    the projection must report lifecycle=closed."""
    for path in sorted(SNAPSHOT_DIR.glob("*.json")):
        payload = json.loads(path.read_text())
        terminal_state = payload["input"]["runtime_view"].get("terminal_state")
        if terminal_state in EXPLICIT_CLOSED_TERMINAL_STATES:
            assert payload["expected_runtime_display"]["lifecycle"] == "closed", path.stem


def test_serialization_round_trip(display: SessionRuntimeDisplay) -> None:
    """Projection must be JSON-serializable as a flat dict."""
    payload = asdict(display)
    json.dumps(payload)
