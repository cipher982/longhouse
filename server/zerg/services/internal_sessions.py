"""Internal synthetic session filters shared by user-facing listings."""

from __future__ import annotations

import re

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import or_

INTERNAL_CANARY_PROVIDER_ALIASES = {"canary", "cnary"}
INTERNAL_CANARY_LABEL_PREFIXES = ("canary", "cnary")
PROVIDER_LIVE_CANARY_CWD_SEGMENT = "/canaries/provider-live/"
PROVIDER_LIVE_PROOF_WORKTREE_MARKER = "longhouse-provider-live-proof"
PROVIDER_FACTORY_ARTIFACT_CWD_SEGMENT = "/provider-factory/artifacts/"
PROVIDER_FACTORY_TEMP_CWD_SEGMENT = "/provider-factory-"
PROVIDER_FACTORY_LIVE_CELL_CWD_SEGMENT = "/live-cell-run-"
PROVIDER_COORDINATION_PROBE_CWD_SEGMENT = "/lhx-claude-coord-"
PROVIDER_NOREPLY_MARKER_RE = re.compile(r"^LONGHOUSE_[A-Za-z0-9_-]+_NOREPLY_")
PROVIDER_NOREPLY_MARKER_SQL_LIKE = r"LONGHOUSE\_%\_NOREPLY\_%"
PROVIDER_PRODUCT_CANARY_MARKER_RE = re.compile(r"^Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_[0-9a-f]+$")
PROVIDER_PRODUCT_CANARY_MARKER_PREFIX = "Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_"
PROVIDER_PRODUCT_CANARY_MARKER_SQL_LIKE = r"Reply with exactly LONGHOUSE\_CURSOR\_PRODUCT\_ONE\_%"
PROVIDER_REPLY_EXACT_MARKER_RE = re.compile(
    r"^Reply (?:with )?exactly "
    r"(?:LONGHOUSE_(?:CODEX|OPENCODE|CURSOR|CLAUDE|AGY)_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_[0-9a-f]{6,}"
    r"|FRESH_AFTER_CANCEL_OK|CLEAN_EXIT_ORIGINAL_COMPLETE|WARM_IDLE_OK)"
    r"(?:\.| and nothing else\.)?$"
)
PROVIDER_REPLY_EXACT_MARKER_PROVIDERS = ("CODEX", "OPENCODE", "CURSOR", "CLAUDE", "AGY")
PROVIDER_REPLY_EXACT_BARE_MARKERS = ("FRESH_AFTER_CANCEL_OK", "CLEAN_EXIT_ORIGINAL_COMPLETE", "WARM_IDLE_OK")
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


def is_provider_factory_cwd(cwd: str | None) -> bool:
    """Recognize provider-factory evidence workspaces, not user repositories."""

    normalized = str(cwd or "").replace("\\", "/").lower()
    return (
        PROVIDER_FACTORY_ARTIFACT_CWD_SEGMENT in normalized
        or PROVIDER_FACTORY_TEMP_CWD_SEGMENT in normalized
        or PROVIDER_FACTORY_LIVE_CELL_CWD_SEGMENT in normalized
        or PROVIDER_COORDINATION_PROBE_CWD_SEGMENT in normalized
    )


def is_provider_noreply_marker(text: str | None) -> bool:
    return bool(PROVIDER_NOREPLY_MARKER_RE.match(str(text or "").strip()))


def is_provider_product_canary_marker(text: str | None) -> bool:
    """Recognize the bounded Cursor product canary prompt."""

    return bool(PROVIDER_PRODUCT_CANARY_MARKER_RE.fullmatch(str(text or "").strip()))


def is_provider_reply_exact_marker(text: str | None) -> bool:
    """Recognize Longhouse's exact-response provider proof markers."""

    return bool(PROVIDER_REPLY_EXACT_MARKER_RE.fullmatch(str(text or "").strip()))


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
        or is_provider_factory_cwd(cwd)
        or is_provider_noreply_marker(first_user_text)
        or is_provider_product_canary_marker(first_user_text)
        or is_provider_reply_exact_marker(first_user_text)
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
    six_hex = "[0-9a-f]" * 6
    reply_exact_marker = []
    for phrase in ("Reply exactly", "Reply with exactly"):
        for provider in PROVIDER_REPLY_EXACT_MARKER_PROVIDERS:
            prefix = f"{phrase} LONGHOUSE_{provider}_"
            escaped_prefix = prefix.replace("_", SQL_LIKE_ESCAPE + "_")
            marker_body = func.substr(first_user, len(prefix) + 1)
            marker_core = case(
                (marker_body.like("% and nothing else."), func.substr(marker_body, 1, func.length(marker_body) - 18)),
                (marker_body.like("%."), func.substr(marker_body, 1, func.length(marker_body) - 1)),
                else_=marker_body,
            )
            reply_exact_marker.append(
                and_(
                    first_user.like(f"{escaped_prefix}%", escape=SQL_LIKE_ESCAPE),
                    ~(marker_core.op("GLOB")("*[^A-Za-z0-9_]*")),
                    or_(
                        and_(
                            marker_core.op("GLOB")(f"*_{six_hex}*"),
                            ~(marker_core.op("GLOB")("*_[0-9a-f]*[^0-9a-f]")),
                        ),
                    ),
                )
            )
    reply_exact_marker.extend(
        or_(
            first_user == f"{phrase} {marker}",
            first_user == f"{phrase} {marker}.",
            first_user == f"{phrase} {marker} and nothing else.",
        )
        for phrase in ("Reply exactly", "Reply with exactly")
        for marker in PROVIDER_REPLY_EXACT_BARE_MARKERS
    )
    return or_(
        cwd.like(f"%{PROVIDER_LIVE_CANARY_CWD_SEGMENT}%/workspace"),
        cwd.like(f"%{PROVIDER_LIVE_PROOF_WORKTREE_MARKER}%"),
        cwd.like(f"%{PROVIDER_FACTORY_ARTIFACT_CWD_SEGMENT}%"),
        cwd.like(f"%{PROVIDER_FACTORY_TEMP_CWD_SEGMENT}%"),
        cwd.like(f"%{PROVIDER_FACTORY_LIVE_CELL_CWD_SEGMENT}%"),
        cwd.like(f"%{PROVIDER_COORDINATION_PROBE_CWD_SEGMENT}%"),
        first_user.like(PROVIDER_NOREPLY_MARKER_SQL_LIKE, escape=SQL_LIKE_ESCAPE),
        product_marker,
        *reply_exact_marker,
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
