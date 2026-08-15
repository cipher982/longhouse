"""Internal synthetic session filters shared by user-facing listings."""

from __future__ import annotations

import re

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_

INTERNAL_CANARY_PROVIDER_ALIASES = {"canary", "cnary"}
INTERNAL_CANARY_LABEL_PREFIXES = ("canary", "cnary")
PROVIDER_LIVE_CANARY_CWD_SEGMENT = "/canaries/provider-live/"
PROVIDER_LIVE_PROOF_WORKTREE_MARKER = "longhouse-provider-live-proof"
PROVIDER_NOREPLY_MARKER_RE = re.compile(r"^LONGHOUSE_[A-Za-z0-9_-]+_NOREPLY_")
PROVIDER_NOREPLY_MARKER_SQL_LIKE = r"LONGHOUSE\_%\_NOREPLY\_%"
PROVIDER_PRODUCT_CANARY_MARKER_RE = re.compile(r"^Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_[0-9a-f]+$")
PROVIDER_PRODUCT_CANARY_MARKER_PREFIX = "Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_"
PROVIDER_PRODUCT_CANARY_MARKER_SQL_LIKE = r"Reply with exactly LONGHOUSE\_CURSOR\_PRODUCT\_ONE\_%"
HATCH_EXECUTION_CONTRACT_RE = re.compile(
    r"^Hatch execution contract:\nThis is a single bounded, non-interactive run(?:\.| with a time budget\b)"
)
HATCH_EXECUTION_CONTRACT_SQL_LIKE = "Hatch execution contract:%This is a single bounded, non-interactive run%"
SQL_LIKE_ESCAPE = "\\"


def is_internal_canary_provider_filter(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in INTERNAL_CANARY_PROVIDER_ALIASES


def is_provider_live_canary_cwd(cwd: str | None) -> bool:
    normalized = str(cwd or "").replace("\\", "/")
    return PROVIDER_LIVE_CANARY_CWD_SEGMENT in normalized and normalized.endswith("/workspace")


def is_provider_live_proof_worktree_cwd(cwd: str | None) -> bool:
    normalized = str(cwd or "").replace("\\", "/").lower()
    return PROVIDER_LIVE_PROOF_WORKTREE_MARKER in normalized


def is_provider_noreply_marker(text: str | None) -> bool:
    return bool(PROVIDER_NOREPLY_MARKER_RE.match(str(text or "").strip()))


def is_provider_product_canary_marker(text: str | None) -> bool:
    """Recognize the bounded Cursor product canary prompt."""

    return bool(PROVIDER_PRODUCT_CANARY_MARKER_RE.fullmatch(str(text or "").strip()))


def is_hatch_execution_contract(text: str | None) -> bool:
    """Recognize Hatch's exact bounded-run preamble."""

    return bool(HATCH_EXECUTION_CONTRACT_RE.match(str(text or "").strip()))


def hatch_automation_session_clause(model):
    """Return a SQLAlchemy clause matching the exact Hatch run preamble."""

    columns = getattr(model, "c", model)
    first_user = func.trim(func.coalesce(columns.first_user_message_preview, ""))
    return first_user.like(HATCH_EXECUTION_CONTRACT_SQL_LIKE)


def classify_provider_proof_environment(
    *,
    cwd: str | None = None,
    first_user_text: str | None = None,
) -> str | None:
    """Return the normalized environment for provider proof/canary sessions."""
    if (
        is_provider_live_canary_cwd(cwd)
        or is_provider_live_proof_worktree_cwd(cwd)
        or is_provider_noreply_marker(first_user_text)
        or is_provider_product_canary_marker(first_user_text)
    ):
        return "test"
    return None


def provider_proof_session_clause(model):
    """Return a SQLAlchemy clause matching provider live-proof sessions."""
    columns = getattr(model, "c", model)
    cwd = func.lower(func.coalesce(columns.cwd, ""))
    first_user = func.trim(func.coalesce(columns.first_user_message_preview, ""))
    product_suffix = func.substr(first_user, len(PROVIDER_PRODUCT_CANARY_MARKER_PREFIX) + 1)
    product_marker = and_(
        first_user.like(PROVIDER_PRODUCT_CANARY_MARKER_SQL_LIKE, escape=SQL_LIKE_ESCAPE),
        func.length(product_suffix) > 0,
        ~(product_suffix.op("GLOB")("*[^0-9a-f]*")),
    )
    return or_(
        cwd.like(f"%{PROVIDER_LIVE_CANARY_CWD_SEGMENT}%/workspace"),
        cwd.like(f"%{PROVIDER_LIVE_PROOF_WORKTREE_MARKER}%"),
        first_user.like(PROVIDER_NOREPLY_MARKER_SQL_LIKE, escape=SQL_LIKE_ESCAPE),
        product_marker,
    )


def internal_canary_session_clause(model):
    """Return a SQLAlchemy clause matching synthetic canary/debug sessions.

    The canary producer should write provider=canary/project=canary, but live
    dogfood data already has typo/legacy rows. User-facing timeline defaults
    should hide all of them; explicit provider=canary remains the debug escape.
    """

    provider = func.lower(func.coalesce(model.provider, ""))
    project = func.lower(func.coalesce(model.project, ""))
    device_id = func.lower(func.coalesce(model.device_id, ""))
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
        *label_clauses,
    )
