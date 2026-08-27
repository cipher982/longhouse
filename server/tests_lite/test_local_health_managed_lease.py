from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta

from zerg.services.local_health import _enrich_managed_session_titles
from zerg.services.local_health.engine_status import _collect_resolved_sessions_from_engine_status


def _engine_status(*, observed_at: datetime, ttl_ms: int = 900_000) -> dict:
    session_id = "11111111-1111-4111-8111-111111111111"
    return {
        "payload": {
            "sessions": [
                {
                    "session_id": session_id,
                    "provider": "antigravity",
                    "control_path": "managed",
                    "state": "attached",
                    "workspace": {},
                    "process": {},
                    "bridge": {"status": "ready"},
                    "evidence": {"process_observed": False},
                    "reason_codes": [],
                }
            ],
            "managed_sessions": [
                {
                    "session_id": session_id,
                    "provider": "antigravity",
                    "state": "attached",
                    "observed_at": observed_at.isoformat(),
                    "lease_ttl_ms": ttl_ms,
                }
            ],
        }
    }


def test_expired_managed_lease_becomes_unknown() -> None:
    now = datetime.now(UTC)
    resolved = _collect_resolved_sessions_from_engine_status(
        _engine_status(observed_at=now - timedelta(hours=1)),
        now=now,
    )

    assert resolved is not None
    managed, _unmanaged = resolved
    assert managed[0]["state"] == "unknown"
    assert managed[0]["bridge_status"] is None
    assert managed[0]["reason_codes"] == ["lease_expired"]


def test_fresh_managed_lease_preserves_attached_state() -> None:
    now = datetime.now(UTC)
    resolved = _collect_resolved_sessions_from_engine_status(
        _engine_status(observed_at=now - timedelta(seconds=30)),
        now=now,
    )

    assert resolved is not None
    managed, _unmanaged = resolved
    assert managed[0]["state"] == "attached"
    assert managed[0]["bridge_status"] == "ready"
    assert managed[0]["reason_codes"] == []


def test_expired_managed_lease_never_fetches_a_remote_title(tmp_path, monkeypatch) -> None:
    def refuse_urlopen(*_args, **_kwargs):
        raise AssertionError("expired lease triggered remote title hydration")

    monkeypatch.setattr("urllib.request.urlopen", refuse_urlopen)
    _enrich_managed_session_titles(
        tmp_path,
        [
            {
                "session_id": "11111111-1111-4111-8111-111111111111",
                "state": "unknown",
                "reason_codes": ["lease_expired"],
            }
        ],
        runtime_url="https://runtime.example",
        token="token",
    )
