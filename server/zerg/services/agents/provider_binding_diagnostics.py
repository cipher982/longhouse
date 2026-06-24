"""Read-back surface for recorded provider-session-binding diagnostics.

Ingest records ``provider_binding_conflict`` and ``provider_binding_missing``
session observations when the provider-session-binding invariant is violated
(see ``docs/specs/provider-session-binding.md``). Those rows are append-only
evidence; nothing read them back until this helper.

IMPORTANT — observed vs current. These are observation rows, not live state.
The observation ids are deterministic, so re-recording the same conflict does
NOT refresh ``observed_at``; a persistent issue can therefore age out of any
lookback window. Always report this as "a recent binding diagnostic was
observed," never as "this session is currently degraded." Authoritative current
state is a capability-projection concern, not this read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy.orm import Session

from zerg.models.agents import SessionObservation
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_CONFLICT
from zerg.services.session_observations import OBS_KIND_PROVIDER_BINDING_MISSING
from zerg.services.session_observations import decode_observation_payload_json

DEFAULT_LOOKBACK = timedelta(days=7)
SAMPLE_LIMIT = 20

BINDING_DIAGNOSTIC_KINDS = (
    OBS_KIND_PROVIDER_BINDING_CONFLICT,
    OBS_KIND_PROVIDER_BINDING_MISSING,
)


@dataclass(frozen=True)
class BindingDiagnosticSample:
    kind: str
    provider: str | None
    provider_session_id: str | None
    session_id: str | None
    observed_at: str | None
    # Conflict-only context; None for missing-binding rows.
    existing_thread_id: str | None = None
    requested_thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": self.kind,
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "session_id": self.session_id,
            "observed_at": self.observed_at,
        }
        if self.existing_thread_id is not None:
            d["existing_thread_id"] = self.existing_thread_id
        if self.requested_thread_id is not None:
            d["requested_thread_id"] = self.requested_thread_id
        return d


@dataclass(frozen=True)
class ProviderBindingDiagnosticsSummary:
    conflict_count: int = 0
    missing_count: int = 0
    affected_session_ids: list[str] = field(default_factory=list)
    affected_provider_session_ids: list[str] = field(default_factory=list)
    most_recent_observed_at: str | None = None
    samples: list[BindingDiagnosticSample] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.conflict_count + self.missing_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "conflict_count": self.conflict_count,
            "missing_count": self.missing_count,
            "total": self.total,
            "affected_session_ids": self.affected_session_ids,
            "affected_provider_session_ids": self.affected_provider_session_ids,
            "most_recent_observed_at": self.most_recent_observed_at,
            "samples": [sample.to_dict() for sample in self.samples],
        }


def _payload_dict(observation: SessionObservation) -> dict[str, Any]:
    raw = decode_observation_payload_json(observation)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def summarize_provider_binding_diagnostics(
    db: Session,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    sample_limit: int = SAMPLE_LIMIT,
) -> ProviderBindingDiagnosticsSummary:
    """Summarize recently observed provider-binding diagnostics.

    Pure read. ``since`` defaults to ``now - DEFAULT_LOOKBACK``. The returned
    summary describes *observed* diagnostics, not current session state.
    """

    reference = now or datetime.now(timezone.utc)
    cutoff = since if since is not None else reference - DEFAULT_LOOKBACK

    rows = (
        db.query(SessionObservation)
        .filter(SessionObservation.kind.in_(BINDING_DIAGNOSTIC_KINDS))
        .filter(SessionObservation.observed_at >= cutoff)
        .order_by(SessionObservation.observed_at.desc(), SessionObservation.id.desc())
        .all()
    )

    conflict_count = 0
    missing_count = 0
    affected_sessions: list[str] = []
    seen_sessions: set[str] = set()
    affected_native_ids: list[str] = []
    seen_native_ids: set[str] = set()
    most_recent: str | None = None
    samples: list[BindingDiagnosticSample] = []

    for row in rows:
        if row.kind == OBS_KIND_PROVIDER_BINDING_CONFLICT:
            conflict_count += 1
        elif row.kind == OBS_KIND_PROVIDER_BINDING_MISSING:
            missing_count += 1
        else:
            continue

        payload = _payload_dict(row)
        provider_session_id = str(payload.get("provider_session_id") or "").strip() or None
        observed_at = _isoformat(row.observed_at)
        if most_recent is None and observed_at is not None:
            # rows are ordered observed_at DESC, so the first non-null wins
            most_recent = observed_at

        session_id = str(row.session_id) if row.session_id is not None else None
        if session_id and session_id not in seen_sessions:
            seen_sessions.add(session_id)
            affected_sessions.append(session_id)
        if provider_session_id and provider_session_id not in seen_native_ids:
            seen_native_ids.add(provider_session_id)
            affected_native_ids.append(provider_session_id)

        if len(samples) < sample_limit:
            samples.append(
                BindingDiagnosticSample(
                    kind=row.kind,
                    provider=row.provider,
                    provider_session_id=provider_session_id,
                    session_id=session_id,
                    observed_at=observed_at,
                    existing_thread_id=(
                        str(payload.get("existing_thread_id")).strip() or None if payload.get("existing_thread_id") is not None else None
                    ),
                    requested_thread_id=(
                        str(payload.get("requested_thread_id")).strip() or None if payload.get("requested_thread_id") is not None else None
                    ),
                )
            )

    return ProviderBindingDiagnosticsSummary(
        conflict_count=conflict_count,
        missing_count=missing_count,
        affected_session_ids=affected_sessions,
        affected_provider_session_ids=affected_native_ids,
        most_recent_observed_at=most_recent,
        samples=samples,
    )
