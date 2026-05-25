from __future__ import annotations

import os
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import uuid4

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("TESTING", "1")


def _attempt(
    *,
    state: str | None,
    run_id=None,
    error_code: str | None = None,
    error_message: str | None = None,
    expires_at: datetime | None = None,
) -> Any:
    from zerg.models.agents import SessionLaunchAttempt

    return SessionLaunchAttempt(
        session_id=uuid4(),
        run_id=run_id,
        provider="codex",
        host_id="cinder",
        state=state,
        error_code=error_code,
        error_message=error_message,
        expires_at=expires_at,
    )


def _project(attempt):
    from zerg.services.session_launch_lifecycle import project_remote_launch_lifecycle

    return project_remote_launch_lifecycle(attempt)


@pytest.mark.parametrize(
    ("raw_state", "run_id", "expected"),
    [
        ("pending", None, "launching"),
        ("", None, "launching"),
        (None, None, "launching"),
        ("dispatched", None, "launching_unknown"),
        ("adopted", None, "live"),
        ("pending", uuid4(), "live"),
        ("dispatched", uuid4(), "live"),
        ("failed", uuid4(), "launch_failed"),
        ("abandoned", uuid4(), "launch_orphaned"),
    ],
)
def test_remote_launch_lifecycle_transition_matrix(raw_state, run_id, expected):
    lifecycle = _project(_attempt(state=raw_state, run_id=run_id))

    assert lifecycle is not None
    assert lifecycle.state == expected


def test_remote_launch_lifecycle_carries_user_visible_error_and_lease():
    lease_until = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    lifecycle = _project(
        _attempt(
            state="failed",
            error_code="cwd_not_found",
            error_message="Workspace missing",
            expires_at=lease_until,
        )
    )

    assert lifecycle is not None
    assert lifecycle.state == "launch_failed"
    assert lifecycle.error_code == "cwd_not_found"
    assert lifecycle.error_message == "Check the workspace path: Workspace missing"
    assert lifecycle.lease_until == lease_until


def test_remote_launch_lifecycle_normalizes_unknown_error_codes():
    lifecycle = _project(
        _attempt(
            state="failed",
            error_code="engine_stacktrace",
            error_message="internal details stay in the message",
        )
    )

    assert lifecycle is not None
    assert lifecycle.state == "launch_failed"
    assert lifecycle.error_code == "provider_launch_failed"
    assert lifecycle.error_message == "Provider failed to start: internal details stay in the message"


def test_remote_launch_lifecycle_requires_durable_attempt():
    assert _project(None) is None
