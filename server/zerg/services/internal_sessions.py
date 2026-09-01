"""Internal synthetic session filters shared by user-facing listings."""

from __future__ import annotations

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
SYNTHETIC_BENCH_PROJECTS = frozenset({"longhouse-bench"})


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


def classify_provider_proof_environment(
    *,
    cwd: str | None = None,
    machine_id: str | None = None,
) -> str | None:
    """Return the normalized environment for provider proof/canary sessions based on path/machine namespace."""
    if (
        is_provider_live_canary_cwd(cwd)
        or is_provider_live_proof_worktree_cwd(cwd)
        or is_provider_factory_cwd(cwd)
        or is_provider_evidence_cwd(cwd)
        or is_provider_factory_machine_id(machine_id)
    ):
        return "test"
    return None


def provider_proof_session_clause(model):
    """Return a SQLAlchemy clause matching provider live-proof sessions by path and machine namespace."""
    columns = getattr(model, "c", model)
    cwd = func.lower(func.coalesce(columns.cwd, ""))
    machine_id_column = getattr(columns, "machine_id", None)
    if machine_id_column is None:
        machine_id_column = getattr(columns, "device_id", None)
    clauses = [
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
    ]
    if machine_id_column is not None:
        clauses.append(func.lower(func.coalesce(machine_id_column, "")) == PROVIDER_FACTORY_MACHINE_ID)
    return or_(*clauses)


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
