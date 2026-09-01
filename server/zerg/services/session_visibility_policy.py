"""Canonical origin/system visibility policy for session presentation.

Raw provenance stays independently queryable.  This module owns only the
product decision derived from positive, durable evidence; user preferences and
organizational state remain separate axes.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_
from sqlalchemy import false
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select

from zerg.services.internal_sessions import INTERNAL_CANARY_LABEL_PREFIXES
from zerg.services.internal_sessions import INTERNAL_CANARY_PROVIDER_ALIASES
from zerg.services.internal_sessions import classify_provider_proof_environment

HIDDEN_ORIGIN_KINDS = frozenset({"hatch_automation", "test_or_canary"})
HIDDEN_ENVIRONMENTS = frozenset({"test", "e2e"})
# A harness that declares its own launch surface has already told us it is not
# product traffic. Recognizing that declaration is what keeps QA and factory
# runs out of the timeline without every new harness inventing a project name
# that some listing has to learn about later.
HIDDEN_LAUNCH_SURFACES = frozenset({"product-e2e", "e2e", "test", "qa", "ci", "canary", "factory_assurance"})
# Reasons that mean "test scope", so an explicit test-scope read reveals them.
TEST_SCOPE_REASON_KEYS = frozenset({"test_environment", "test_launch_surface"})
HUMAN_LAUNCH_ACTORS = frozenset({"user", "human_ui", "human_shell"})
ACTIVE_USER_STATES = frozenset({"active", "parked"})


@dataclass(frozen=True)
class SessionVisibilityFacts:
    provider: str | None = None
    project: str | None = None
    environment: str | None = None
    origin_kind: str | None = None
    launch_actor: str | None = None
    launch_surface: str | None = None
    cwd: str | None = None
    machine_id: str | None = None
    primary_thread_is_worker_only: bool = False
    # Same product fact, second evidence source. The legacy graph proves a
    # worker through its thread's branch_kind; storage-v2 sessions have no live
    # thread row to carry that, so the engine's observation is stored on the
    # session itself. One reason key either way — this is not a second policy.
    is_subagent: bool = False


@dataclass(frozen=True)
class OriginVisibilityDecision:
    system_hidden: bool
    title_origin_eligible: bool
    reason_keys: tuple[str, ...]


def evaluate_origin_visibility(facts: SessionVisibilityFacts) -> OriginVisibilityDecision:
    """Evaluate positive system-hidden evidence without reading a stored flag."""

    reasons: list[str] = []
    origin_kind = _normalized(facts.origin_kind)
    environment = _normalized(facts.environment)
    launch_actor = _normalized(facts.launch_actor)
    provider = _normalized(facts.provider)
    project = _normalized(facts.project)
    machine_id = _normalized(facts.machine_id)

    if origin_kind in HIDDEN_ORIGIN_KINDS:
        reasons.append("hidden_origin")
    if environment in HIDDEN_ENVIRONMENTS:
        reasons.append("test_environment")
    if _normalized(facts.launch_surface) in HIDDEN_LAUNCH_SURFACES:
        reasons.append("test_launch_surface")
    if launch_actor == "automation":
        reasons.append("automation_actor")
    if classify_provider_proof_environment(cwd=facts.cwd, machine_id=facts.machine_id) == "test":
        reasons.append("provider_proof")
    if _is_internal_canary(provider=provider, project=project, machine_id=machine_id):
        reasons.append("internal_canary")
    if facts.primary_thread_is_worker_only or facts.is_subagent:
        reasons.append("worker_only")

    unique_reasons = tuple(dict.fromkeys(reasons))
    hidden = bool(unique_reasons)
    return OriginVisibilityDecision(
        system_hidden=hidden,
        title_origin_eligible=not hidden,
        reason_keys=unique_reasons,
    )


def visible_in_test_scope(decision: OriginVisibilityDecision) -> bool:
    """Return true only when test-scope declarations are the sole hidden reasons."""

    return bool(decision.reason_keys) and set(decision.reason_keys) <= TEST_SCOPE_REASON_KEYS


def known_hidden_evidence_clause(model, *, include_test: bool = False):
    """SQL twin for scalar evidence available on a denormalized session row."""

    from zerg.services.internal_sessions import provider_proof_session_clause

    columns = getattr(model, "c", model)
    clauses = []
    origin_kind = _column(columns, "origin_kind")
    environment = _column(columns, "environment")
    launch_actor = _column(columns, "launch_actor")
    launch_surface = _column(columns, "launch_surface")
    provider = _column(columns, "provider")
    project = _column(columns, "project")
    machine = _column(columns, "machine_id", "device_id")

    is_subagent = _column(columns, "is_subagent")
    if is_subagent is not None:
        clauses.append(func.coalesce(is_subagent, 0) == 1)
    if origin_kind is not None:
        clauses.append(func.lower(func.coalesce(origin_kind, "")).in_(HIDDEN_ORIGIN_KINDS))
    if environment is not None and not include_test:
        clauses.append(func.lower(func.coalesce(environment, "")).in_(HIDDEN_ENVIRONMENTS))
    if launch_actor is not None:
        clauses.append(func.lower(func.coalesce(launch_actor, "")) == "automation")
    if launch_surface is not None and not include_test:
        clauses.append(func.lower(func.coalesce(launch_surface, "")).in_(HIDDEN_LAUNCH_SURFACES))
    if provider is not None:
        provider_value = func.lower(func.coalesce(provider, ""))
        clauses.append(provider_value.in_(INTERNAL_CANARY_PROVIDER_ALIASES))
    if project is not None:
        project_value = func.lower(func.coalesce(project, ""))
        for prefix in INTERNAL_CANARY_LABEL_PREFIXES:
            clauses.extend((project_value == prefix, project_value.like(f"{prefix}-%")))
    if machine is not None:
        machine_value = func.lower(func.coalesce(machine, ""))
        for prefix in INTERNAL_CANARY_LABEL_PREFIXES:
            clauses.extend((machine_value == prefix, machine_value.like(f"%-{prefix}")))
    # Provider-proof recognition already degrades gracefully between device_id
    # and machine_id and is the single SQL authority for its exact markers.
    if all(_column(columns, field) is not None for field in ("provider", "project", "cwd")) and machine is not None:
        clauses.append(provider_proof_session_clause(model))

    return or_(*clauses) if clauses else false()


def primary_worker_only_clause(model, thread_model):
    """Correlated graph evidence for a worker-only primary thread."""

    columns = getattr(model, "c", model)
    threads = getattr(thread_model, "c", thread_model)
    session_id = _column(columns, "session_id", "id")
    if session_id is None:
        return false()
    return (
        select(threads.id)
        .where(
            threads.session_id == session_id,
            threads.is_primary == 1,
            threads.branch_kind == "subagent",
        )
        .exists()
    )


def effective_system_hidden_clause(model, *, include_test: bool = False, worker_only_evidence=None):
    """Stored projection plus defensive positive evidence for default reads."""

    columns = getattr(model, "c", model)
    persisted = _column(columns, "hidden_from_default_timeline")
    evidence = known_hidden_evidence_clause(model, include_test=include_test)
    if worker_only_evidence is not None:
        evidence = or_(evidence, worker_only_evidence)
    if persisted is None:
        return evidence
    if not include_test:
        return or_(func.coalesce(persisted, 0) != 0, evidence)
    environment = _column(columns, "environment")
    launch_surface = _column(columns, "launch_surface")
    if environment is None and launch_surface is None:
        return or_(func.coalesce(persisted, 0) != 0, evidence)
    # The projection does not retain a reason bitset. In explicit test scope,
    # discount a persisted bit on a row that declared test scope, then re-apply
    # every stronger automation/proof marker. This reveals ordinary test
    # sessions without leaking Hatch, provider proof, canaries, or automation
    # actors.
    persisted_terms = [func.coalesce(persisted, 0) != 0]
    if environment is not None:
        persisted_terms.append(~func.lower(func.coalesce(environment, "")).in_(HIDDEN_ENVIRONMENTS))
    if launch_surface is not None:
        persisted_terms.append(~func.lower(func.coalesce(launch_surface, "")).in_(HIDDEN_LAUNCH_SURFACES))
    persisted_non_test = and_(*persisted_terms)
    return or_(persisted_non_test, evidence)


def default_visible_clause(
    model,
    *,
    include_system_hidden: bool = False,
    include_test: bool = False,
    worker_only_evidence=None,
):
    """Default product visibility over fields present on a session/card row."""

    columns = getattr(model, "c", model)
    clauses = []
    if not include_system_hidden:
        clauses.append(
            ~effective_system_hidden_clause(
                model,
                include_test=include_test,
                worker_only_evidence=worker_only_evidence,
            )
        )
    user_hidden = _column(columns, "user_hidden_from_timeline")
    if user_hidden is not None:
        clauses.append(func.coalesce(user_hidden, 0) == 0)
    user_state = _column(columns, "user_state")
    if user_state is not None:
        clauses.append(func.lower(func.coalesce(user_state, "active")).in_(ACTIVE_USER_STATES))
    return and_(*clauses) if clauses else ~false()


def title_origin_eligible_clause(model):
    """Rows without positive automation/test evidence may incur title debt."""

    return ~known_hidden_evidence_clause(model)


def facts_from_row(row, *, primary_thread_is_worker_only: bool = False) -> SessionVisibilityFacts:
    """Build normalized policy input from an ORM object or mapping."""

    return SessionVisibilityFacts(
        provider=_value(row, "provider"),
        project=_value(row, "project"),
        environment=_value(row, "environment"),
        origin_kind=_value(row, "origin_kind"),
        launch_actor=_value(row, "launch_actor"),
        launch_surface=_value(row, "launch_surface"),
        cwd=_value(row, "cwd"),
        machine_id=_value(row, "machine_id") or _value(row, "device_id"),
        primary_thread_is_worker_only=primary_thread_is_worker_only,
    )


def _is_internal_canary(*, provider: str, project: str, machine_id: str) -> bool:
    if provider in INTERNAL_CANARY_PROVIDER_ALIASES:
        return True
    return any(
        project == prefix or project.startswith(f"{prefix}-") or machine_id == prefix or machine_id.endswith(f"-{prefix}")
        for prefix in INTERNAL_CANARY_LABEL_PREFIXES
    )


def _normalized(value: object) -> str:
    return str(value or "").strip().lower()


def _column(columns, *names):
    for name in names:
        value = getattr(columns, name, None)
        if value is not None:
            return value
    return None


def _value(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and name in mapping:
        return mapping[name]
    return getattr(row, name, None)


__all__ = [
    "ACTIVE_USER_STATES",
    "HIDDEN_ENVIRONMENTS",
    "HIDDEN_LAUNCH_SURFACES",
    "HIDDEN_ORIGIN_KINDS",
    "HUMAN_LAUNCH_ACTORS",
    "OriginVisibilityDecision",
    "SessionVisibilityFacts",
    "TEST_SCOPE_REASON_KEYS",
    "default_visible_clause",
    "effective_system_hidden_clause",
    "evaluate_origin_visibility",
    "facts_from_row",
    "known_hidden_evidence_clause",
    "primary_worker_only_clause",
    "title_origin_eligible_clause",
    "visible_in_test_scope",
]
