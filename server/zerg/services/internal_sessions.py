"""Internal synthetic session filters shared by user-facing listings."""

from __future__ import annotations

import re

from sqlalchemy import func
from sqlalchemy import or_

INTERNAL_CANARY_PROVIDER_ALIASES = {"canary", "cnary"}
INTERNAL_CANARY_LABEL_PREFIXES = ("canary", "cnary")
PROVIDER_LIVE_CANARY_CWD_SEGMENT = "/.longhouse/canaries/provider-live/"
PROVIDER_NOREPLY_MARKER_RE = re.compile(r"^LONGHOUSE_[A-Za-z0-9_-]+_NOREPLY_")


def is_internal_canary_provider_filter(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in INTERNAL_CANARY_PROVIDER_ALIASES


def is_provider_live_canary_cwd(cwd: str | None) -> bool:
    normalized = str(cwd or "").replace("\\", "/")
    return PROVIDER_LIVE_CANARY_CWD_SEGMENT in normalized and normalized.endswith("/workspace")


def is_provider_noreply_marker(text: str | None) -> bool:
    return bool(PROVIDER_NOREPLY_MARKER_RE.match(str(text or "").strip()))


def classify_provider_proof_environment(
    *,
    cwd: str | None = None,
    first_user_text: str | None = None,
) -> str | None:
    """Return the normalized environment for provider proof/canary sessions."""
    if is_provider_live_canary_cwd(cwd) or is_provider_noreply_marker(first_user_text):
        return "test"
    return None


def internal_canary_session_clause(model):
    """Return a SQLAlchemy clause matching synthetic canary/debug sessions.

    The canary producer should write provider=canary/project=canary, but live
    dogfood data already has typo/legacy rows. User-facing timeline defaults
    should hide all of them; explicit provider=canary remains the debug escape.
    """

    provider = func.lower(func.coalesce(model.provider, ""))
    project = func.lower(func.coalesce(model.project, ""))
    device_id = func.lower(func.coalesce(model.device_id, ""))
    cwd = func.lower(func.coalesce(model.cwd, ""))

    label_clauses = []
    for prefix in INTERNAL_CANARY_LABEL_PREFIXES:
        label_clauses.extend(
            [
                project == prefix,
                project.like(f"{prefix}-%"),
                device_id == prefix,
                device_id.like(f"%-{prefix}"),
            ]
        )

    return or_(
        provider.in_(INTERNAL_CANARY_PROVIDER_ALIASES),
        cwd.like(f"%{PROVIDER_LIVE_CANARY_CWD_SEGMENT}%/workspace"),
        *label_clauses,
    )
