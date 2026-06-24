"""Shared convergence verdict for provider-session-binding proofs.

The same question — "did a managed launch and its transcript converge to ONE
steerable session for this provider-native id, or did they split?" — is asked by
both the hermetic test (building the candidate list from the DB) and the gated
live canary (building it from ``/api/agents/*``). Keep the verdict logic here so
the two can never drift, and so a future Sauron release-watch can reuse it.

This module is intentionally pure: it takes already-collected candidates and
returns a verdict. It does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass(frozen=True)
class BindingCandidate:
    """One Longhouse session observed for a given provider-native id."""

    session_id: str
    has_content: bool
    managed: bool


@dataclass(frozen=True)
class ConvergenceVerdict:
    ok: bool
    reason: str
    provider: str
    provider_session_id: str
    session_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "provider": self.provider,
            "provider_session_id": self.provider_session_id,
            "session_ids": self.session_ids,
        }


def evaluate_provider_binding_convergence(
    *,
    provider: str,
    provider_session_id: str,
    candidates: list[BindingCandidate],
) -> ConvergenceVerdict:
    """Return whether one provider-native id converged to one steerable row.

    Failure modes (each is the split-row symptom the binding kernel exists to
    prevent):

    - ``no_session``: nothing matched the native id at all.
    - ``split_row``: more than one distinct session matched — the classic bug.
    - ``no_content``: the single session has no transcript content.
    - ``not_managed``: the single session is not steerable/managed.
    """

    session_ids = sorted({c.session_id for c in candidates})

    if not candidates:
        return ConvergenceVerdict(
            ok=False,
            reason="no_session",
            provider=provider,
            provider_session_id=provider_session_id,
            session_ids=session_ids,
        )

    if len(session_ids) > 1:
        return ConvergenceVerdict(
            ok=False,
            reason="split_row",
            provider=provider,
            provider_session_id=provider_session_id,
            session_ids=session_ids,
        )

    only = candidates[0]
    if not only.has_content:
        return ConvergenceVerdict(
            ok=False,
            reason="no_content",
            provider=provider,
            provider_session_id=provider_session_id,
            session_ids=session_ids,
        )
    if not only.managed:
        return ConvergenceVerdict(
            ok=False,
            reason="not_managed",
            provider=provider,
            provider_session_id=provider_session_id,
            session_ids=session_ids,
        )

    return ConvergenceVerdict(
        ok=True,
        reason="converged",
        provider=provider,
        provider_session_id=provider_session_id,
        session_ids=session_ids,
    )
