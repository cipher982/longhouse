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
PROVIDER_FACTORY_ARTIFACT_CWD_SEGMENT = "/provider-factory/artifacts/"
PROVIDER_FACTORY_TEMP_CWD_PREFIXES = ("/tmp/provider-factory-", "/private/tmp/provider-factory-")
PROVIDER_FACTORY_LIVE_CELL_CWD_PREFIXES = ("/tmp/live-cell-run-", "/private/tmp/live-cell-run-")
PROVIDER_FACTORY_MACHINE_ID = "provider-factory-resume"
FACTORY_TITLE_ASSURANCE_PROJECT = "longhouse-title-assurance"
FACTORY_TITLE_ASSURANCE_CWD = "/factory/title-assurance"
FACTORY_TITLE_ASSURANCE_SURFACE = "factory_assurance"
PROVIDER_COORDINATION_PROBE_CWD_PREFIXES = ("/tmp/lhx-claude-coord-", "/private/tmp/lhx-claude-coord-")
PROVIDER_EVIDENCE_CWD_PREFIXES = ("/tmp/", "/private/tmp/")
PROVIDER_EVIDENCE_CWD_SEGMENT = "/evidence/raw/"
PROVIDER_NOREPLY_MARKER_RE = re.compile(r"^LONGHOUSE_[A-Za-z0-9_-]+_NOREPLY_")
PROVIDER_NOREPLY_MARKER_SQL_LIKE = r"LONGHOUSE\_%\_NOREPLY\_%"
PROVIDER_PRODUCT_CANARY_MARKER_RE = re.compile(r"^Reply with exactly LONGHOUSE_CURSOR_PRODUCT_ONE_[0-9a-f]+$")
# ``LH_`` / ``lh-`` exact-response tokens are reserved for Longhouse QA. The
# full-prompt anchor keeps ordinary sessions that merely mention a token visible.
PROVIDER_REPLY_EXACT_MARKER_RE = re.compile(
    r"^Reply (?:with )?exactly "
    r"(?:LONGHOUSE_(?:CODEX|OPENCODE|CURSOR|CLAUDE|AGY)_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_[0-9a-f]{6,}"
    r"|LH_[A-Za-z0-9_-]{6,}|lh-[a-z0-9-]{6,}"
    r"|FRESH_AFTER_CANCEL_OK|CLEAN_EXIT_ORIGINAL_COMPLETE|WARM_IDLE_OK)"
    r"(?:\."
    r"| on the first line and nothing else\."
    r"| and nothing else\.(?: Do not use (?:any )?tools\.)?)?$"
)
PROVIDER_COORDINATION_AWARENESS_MARKER_RE = re.compile(r"LONGHOUSE_CURSOR_COORD_AWARENESS_[0-9a-f]{6,}", re.IGNORECASE)
HATCH_EXECUTION_CONTRACT_RE = re.compile(
    r"^Hatch execution contract:\nThis is a single bounded, non-interactive run(?:\.| with a time budget\b)"
)
HATCH_EXECUTION_CONTRACT_SQL_LIKE = "Hatch execution contract:%This is a single bounded, non-interactive run%"
SQL_LIKE_ESCAPE = "\\"
SQL_WHITESPACE = " \t\r\n"
SYNTHETIC_BENCH_PROJECTS = frozenset({"longhouse-bench"})


def _normalize_internal_prompt(text: str | None) -> str:
    """Match the explicit whitespace normalization used by SQLite clauses."""

    return str(text or "").strip(SQL_WHITESPACE)


def is_internal_canary_provider_filter(provider: str | None) -> bool:
    return str(provider or "").strip().lower() in INTERNAL_CANARY_PROVIDER_ALIASES


def is_provider_live_canary_cwd(cwd: str | None) -> bool:
    normalized = str(cwd or "").replace("\\", "/").lower()
    # The entire provider-live namespace is Longhouse-owned proof scratch.
    # Historical launchers sometimes registered the qualification root rather
    # than its `/workspace` child, but neither is a human workspace.
    return PROVIDER_LIVE_CANARY_CWD_SEGMENT in normalized


def is_provider_live_proof_worktree_cwd(cwd: str | None) -> bool:
    normalized = str(cwd or "").replace("\\", "/").lower()
    return PROVIDER_LIVE_PROOF_WORKTREE_MARKER in normalized


def is_provider_factory_cwd(cwd: str | None) -> bool:
    """Recognize provider-factory evidence workspaces, not user repositories."""

    normalized = str(cwd or "").replace("\\", "/").lower()
    return (
        PROVIDER_FACTORY_ARTIFACT_CWD_SEGMENT in normalized
        or normalized.startswith(PROVIDER_FACTORY_TEMP_CWD_PREFIXES)
        or normalized.startswith(PROVIDER_FACTORY_LIVE_CELL_CWD_PREFIXES)
        or normalized.startswith(PROVIDER_COORDINATION_PROBE_CWD_PREFIXES)
    )


def is_provider_evidence_cwd(cwd: str | None) -> bool:
    """Recognize temporary raw provider-evidence workspaces."""

    normalized = str(cwd or "").replace("\\", "/").lower()
    return normalized.startswith(PROVIDER_EVIDENCE_CWD_PREFIXES) and PROVIDER_EVIDENCE_CWD_SEGMENT in normalized


def is_provider_factory_machine_id(machine_id: str | None) -> bool:
    return str(machine_id or "").strip().lower() == PROVIDER_FACTORY_MACHINE_ID


def is_factory_title_assurance_session(
    *,
    provider: str | None,
    environment: str | None,
    project: str | None,
    cwd: str | None,
    machine_id: str | None,
    origin_kind: str | None,
    hidden_from_default_timeline: bool | int | None,
    launch_actor: str | None,
    launch_surface: str | None,
) -> bool:
    """Recognize the one factory-owned session intentionally carrying title debt."""

    return (
        str(provider or "").strip().lower() == "claude"
        and str(environment or "").strip().lower() == "local"
        and str(project or "").strip() == FACTORY_TITLE_ASSURANCE_PROJECT
        and str(cwd or "").strip() == FACTORY_TITLE_ASSURANCE_CWD
        and is_provider_factory_machine_id(machine_id)
        and str(origin_kind or "").strip() == "console"
        and bool(hidden_from_default_timeline)
        and str(launch_actor or "").strip() == "automation"
        and str(launch_surface or "").strip() == FACTORY_TITLE_ASSURANCE_SURFACE
    )


def factory_title_assurance_session_clause(model):
    """SQL twin of :func:`is_factory_title_assurance_session`."""

    columns = getattr(model, "c", model)
    return and_(
        func.lower(func.coalesce(columns.provider, "")) == "claude",
        func.lower(func.coalesce(columns.environment, "")) == "local",
        columns.project == FACTORY_TITLE_ASSURANCE_PROJECT,
        columns.cwd == FACTORY_TITLE_ASSURANCE_CWD,
        func.lower(func.coalesce(columns.machine_id, "")) == PROVIDER_FACTORY_MACHINE_ID,
        columns.origin_kind == "console",
        columns.hidden_from_default_timeline == 1,
        columns.launch_actor == "automation",
        columns.launch_surface == FACTORY_TITLE_ASSURANCE_SURFACE,
    )


def is_provider_coordination_awareness_marker(text: str | None) -> bool:
    return bool(PROVIDER_COORDINATION_AWARENESS_MARKER_RE.search(str(text or "").strip()))


def is_provider_noreply_marker(text: str | None) -> bool:
    return bool(PROVIDER_NOREPLY_MARKER_RE.match(_normalize_internal_prompt(text)))


def is_provider_product_canary_marker(text: str | None) -> bool:
    """Recognize the bounded Cursor product canary prompt."""

    return bool(PROVIDER_PRODUCT_CANARY_MARKER_RE.fullmatch(_normalize_internal_prompt(text)))


def is_provider_reply_exact_marker(text: str | None) -> bool:
    """Recognize Longhouse's exact-response provider proof markers."""

    return bool(PROVIDER_REPLY_EXACT_MARKER_RE.fullmatch(_normalize_internal_prompt(text)))


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
    machine_id: str | None = None,
    first_user_text: str | None = None,
) -> str | None:
    """Return the normalized environment for provider proof/canary sessions."""
    if (
        is_provider_live_canary_cwd(cwd)
        or is_provider_live_proof_worktree_cwd(cwd)
        or is_provider_factory_cwd(cwd)
        or is_provider_evidence_cwd(cwd)
        or is_provider_factory_machine_id(machine_id)
        or is_provider_noreply_marker(first_user_text)
        or is_provider_product_canary_marker(first_user_text)
        or is_provider_reply_exact_marker(first_user_text)
        or is_provider_coordination_awareness_marker(first_user_text)
    ):
        return "test"
    return None


def provider_proof_session_clause(model):
    """Return a SQLAlchemy clause matching provider live-proof sessions."""
    columns = getattr(model, "c", model)
    cwd = func.lower(func.coalesce(columns.cwd, ""))
    # SQLite's one-argument trim() removes spaces only, while the canonical
    # Python classifier uses str.strip(). Provider transcripts commonly retain
    # a trailing newline, so name the shared whitespace alphabet explicitly.
    first_user = func.trim(func.coalesce(columns.first_user_message_preview, ""), SQL_WHITESPACE)
    machine_id_column = getattr(columns, "machine_id", None)
    if machine_id_column is None:
        machine_id_column = getattr(columns, "device_id", None)
    product_marker = first_user.op("REGEXP")(PROVIDER_PRODUCT_CANARY_MARKER_RE.pattern)
    noreply_marker = first_user.op("REGEXP")(PROVIDER_NOREPLY_MARKER_RE.pattern)
    reply_exact_marker = first_user.op("REGEXP")(PROVIDER_REPLY_EXACT_MARKER_RE.pattern)
    six_hex = "[0-9a-f]" * 6
    coordination_awareness_marker = func.lower(first_user).op("GLOB")("*longhouse_cursor_coord_awareness_" + six_hex + "*")
    metadata_markers = [coordination_awareness_marker]
    if machine_id_column is not None:
        metadata_markers.append(func.lower(func.coalesce(machine_id_column, "")) == PROVIDER_FACTORY_MACHINE_ID)
    return or_(
        cwd.like(f"%{PROVIDER_LIVE_CANARY_CWD_SEGMENT}%"),
        cwd.like(f"%{PROVIDER_LIVE_PROOF_WORKTREE_MARKER}%"),
        cwd.like(f"%{PROVIDER_FACTORY_ARTIFACT_CWD_SEGMENT}%"),
        cwd.like("/tmp/provider-factory-%"),
        cwd.like("/private/tmp/provider-factory-%"),
        cwd.like("/tmp/live-cell-run-%"),
        cwd.like("/private/tmp/live-cell-run-%"),
        cwd.like("/tmp/lhx-claude-coord-%"),
        cwd.like("/private/tmp/lhx-claude-coord-%"),
        cwd.like("/tmp/%/evidence/raw/%"),
        cwd.like("/private/tmp/%/evidence/raw/%"),
        noreply_marker,
        product_marker,
        reply_exact_marker,
        *metadata_markers,
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
