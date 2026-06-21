"""Provider-neutral session graph evidence and projection rules."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from types import MappingProxyType
from typing import Any
from typing import Literal
from typing import Mapping
from typing import cast

LineageKind = Literal["none", "task_child", "fork", "unknown", "agent_switch", "async_prompt"]
ProjectionKind = Literal["root", "subagent", "fork", "linked", "inline_event", "run_control"]
Visibility = Literal["timeline", "hidden", "inline", "control"]
CapabilityState = Literal["supported", "unsupported", "unknown", "experimental", "observed_only"]
_EXPLICIT_LINEAGE_KINDS: frozenset[str] = frozenset({"task_child", "fork", "unknown", "agent_switch", "async_prompt"})


def _frozen_metadata(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _source_path_looks_like_subagent(source_path: str | None) -> bool:
    if not source_path:
        return False
    return "/subagents/" in source_path.replace("\\", "/")


def classify_lineage_kind(
    *,
    explicit_kind: str | None = None,
    is_sidechain: bool = False,
    parent_provider_session_id: str | None = None,
    source_path: str | None = None,
) -> LineageKind:
    """Classify raw provider lineage evidence before product projection."""

    explicit = str(explicit_kind or "").strip()
    if explicit in _EXPLICIT_LINEAGE_KINDS:
        return cast(LineageKind, explicit)

    looks_like_subagent = _source_path_looks_like_subagent(source_path)
    has_parent = bool(str(parent_provider_session_id or "").strip())
    if is_sidechain and (has_parent or looks_like_subagent):
        return "task_child"
    if has_parent or looks_like_subagent:
        return "unknown"
    return "none"


@dataclass(frozen=True)
class ObservedActor:
    """Provider-emitted actor evidence, such as OpenCode's build/explore/scout."""

    provider: str
    actor_id: str | None = None
    name: str | None = None
    mode: str | None = None
    model: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True)
class ObservedLineageEdge:
    """Provider evidence that relates one unit of work to another."""

    provider: str
    kind: LineageKind
    parent_provider_session_id: str | None = None
    child_provider_session_id: str | None = None
    parent_event_id: str | None = None
    parent_tool_call_id: str | None = None
    evidence_kind: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


def observed_lineage_from_evidence(
    *,
    provider: str,
    explicit_kind: str | None = None,
    is_sidechain: bool = False,
    source_path: str | None = None,
    parent_provider_session_id: str | None = None,
    child_provider_session_id: str | None = None,
    parent_event_id: str | None = None,
    parent_tool_call_id: str | None = None,
    evidence_kind: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ObservedLineageEdge | None:
    kind = classify_lineage_kind(
        explicit_kind=explicit_kind,
        is_sidechain=is_sidechain,
        parent_provider_session_id=parent_provider_session_id,
        source_path=source_path,
    )
    if kind == "none":
        return None
    return ObservedLineageEdge(
        provider=provider,
        kind=kind,
        parent_provider_session_id=parent_provider_session_id,
        child_provider_session_id=child_provider_session_id,
        parent_event_id=parent_event_id,
        parent_tool_call_id=parent_tool_call_id,
        evidence_kind=evidence_kind,
        metadata=metadata or {},
    )


@dataclass(frozen=True)
class ObservedRun:
    """Provider or Longhouse control/run evidence, including async prompts."""

    provider: str
    run_id: str | None = None
    kind: str | None = None
    status: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True)
class ObservedCapability:
    """Evidence-backed provider capability state."""

    provider: str
    name: str
    state: CapabilityState
    evidence: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True)
class ObservedSession:
    """Provider evidence for a session-like unit before product projection."""

    provider: str
    provider_session_id: str | None = None
    longhouse_session_id: str | None = None
    lineage: ObservedLineageEdge | None = None
    actors: tuple[ObservedActor, ...] = ()
    runs: tuple[ObservedRun, ...] = ()
    capabilities: tuple[ObservedCapability, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _frozen_metadata(self.metadata))


@dataclass(frozen=True)
class SessionProjectionDecision:
    """Product decision for observed session graph evidence."""

    projection_kind: ProjectionKind
    visibility: Visibility
    branch_kind: str | None
    attach_to_parent: bool = False
    relink_later: bool = False
    record_parent_alias: bool = False


def resolve_session_projection(
    session: ObservedSession,
    *,
    parent_thread_resolved: bool = False,
) -> SessionProjectionDecision:
    """Classify observed provider evidence into Longhouse projection intent."""

    lineage = session.lineage
    if lineage is None or lineage.kind == "none":
        return SessionProjectionDecision(
            projection_kind="root",
            visibility="timeline",
            branch_kind="root",
        )

    if lineage.kind == "task_child":
        return SessionProjectionDecision(
            projection_kind="subagent",
            visibility="hidden",
            branch_kind="subagent",
            attach_to_parent=parent_thread_resolved,
            relink_later=not parent_thread_resolved,
            record_parent_alias=bool(lineage.parent_provider_session_id),
        )

    if lineage.kind == "fork":
        return SessionProjectionDecision(
            projection_kind="fork",
            visibility="timeline",
            branch_kind="fork",
            record_parent_alias=bool(lineage.parent_provider_session_id),
        )

    if lineage.kind == "unknown":
        return SessionProjectionDecision(
            projection_kind="linked",
            visibility="timeline",
            branch_kind="root",
            record_parent_alias=bool(lineage.parent_provider_session_id),
        )

    if lineage.kind == "agent_switch":
        return SessionProjectionDecision(
            projection_kind="inline_event",
            visibility="inline",
            branch_kind=None,
        )

    if lineage.kind == "async_prompt":
        return SessionProjectionDecision(
            projection_kind="run_control",
            visibility="control",
            branch_kind=None,
        )

    return SessionProjectionDecision(
        projection_kind="linked",
        visibility="timeline",
        branch_kind="root",
        record_parent_alias=bool(lineage.parent_provider_session_id),
    )
