"""Pydantic and dataclass models for agent sessions and events."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from uuid import UUID

from pydantic import BaseModel
from pydantic import Field

from zerg.models.agents import AgentEvent
from zerg.models.agents import AgentSession


class EventIngest(BaseModel):
    """Schema for ingesting a single event."""

    role: str = Field(..., description="Message role: user, assistant, tool, system")
    content_text: Optional[str] = Field(None, description="Message text content")
    tool_name: Optional[str] = Field(None, description="Tool name if this is a tool call")
    tool_input_json: Optional[Dict[str, Any]] = Field(None, description="Tool call parameters")
    tool_output_text: Optional[str] = Field(None, description="Tool result")
    tool_call_id: Optional[str] = Field(None, description="Cross-provider call/result linkage ID (Claude tool_use_id, Codex call_id)")
    timestamp: datetime = Field(..., description="Event timestamp")
    source_path: Optional[str] = Field(None, description="Original source file path")
    source_offset: Optional[int] = Field(None, description="Byte offset in source file")
    raw_json: Optional[str] = Field(None, description="Original JSONL line for lossless archiving")


class SourceLineIngest(BaseModel):
    """Schema for ingesting a source line archive row."""

    source_path: str = Field(..., description="Original source file path")
    source_offset: int = Field(..., description="Byte offset in source file")
    raw_json: str = Field(..., description="Original source line without trailing newline")


class SourceRewindHintIngest(BaseModel):
    """Explicit rewind/truncation hint emitted by the engine."""

    source_path: str = Field(..., description="Original source file path")
    source_offset: int = Field(..., description="Byte offset where the rewrite starts")
    reason: str = Field(..., description="Reason for the rewind, e.g. truncation")


class SessionIngest(BaseModel):
    """Schema for ingesting a session with events."""

    id: Optional[UUID] = Field(None, description="Session UUID (generated if not provided)")
    provider: str = Field(..., description="AI provider: claude, codex, antigravity, gemini, cursor")
    environment: str = Field(..., description="Environment: production, development, test, e2e")
    project: Optional[str] = Field(None, description="Project name")
    device_id: Optional[str] = Field(None, description="Device/machine identifier")
    device_name: Optional[str] = Field(None, description="Human-friendly device label (e.g. 'laptop', 'demo-machine')")
    cwd: Optional[str] = Field(None, description="Working directory")
    git_repo: Optional[str] = Field(None, description="Git remote URL")
    git_branch: Optional[str] = Field(None, description="Git branch name")
    started_at: datetime = Field(..., description="Session start time")
    ended_at: Optional[datetime] = Field(None, description="Session end time")
    provider_session_id: Optional[str] = Field(None, description="Provider-specific session ID (e.g., Claude Code session UUID)")
    thread_root_session_id: Optional[UUID] = Field(None, description="Logical thread root session UUID")
    continued_from_session_id: Optional[UUID] = Field(None, description="Parent continuation session UUID")
    continuation_kind: Optional[str] = Field(None, description="Continuation kind: local|cloud|runner")
    origin_label: Optional[str] = Field(None, description="User-facing execution origin label, e.g. Cinder or Cloud")
    execution_home: Optional[str] = Field(
        None,
        description="Internal execution home: legacy|managed_local|managed_hosted|cloud_takeover",
    )
    branched_from_event_id: Optional[int] = Field(None, description="Event ID where this continuation branched from its parent")
    is_sidechain: bool = Field(False, description="True when session is a Task sub-agent (isSidechain:true in JSONL)")
    events: List[EventIngest] = Field(default_factory=list, description="Session events")
    source_lines: List[SourceLineIngest] = Field(default_factory=list, description="Lossless source-line archive")
    rewind_hints: List[SourceRewindHintIngest] = Field(
        default_factory=list,
        description="Explicit rewind/truncation hints from the engine",
    )


class IngestResult(BaseModel):
    """Result of an ingest operation."""

    session_id: UUID
    events_inserted: int
    events_skipped: int  # Duplicates that were skipped
    latest_inserted_event_id: Optional[int] = None
    session_created: bool
    commit_count: int = 0
    commit_ms_total: float = 0.0
    source_lines_inserted: int = 0
    store_stage_ms: Dict[str, float] = Field(default_factory=dict)


@dataclass(frozen=True)
class CompactionBoundary:
    """Active-context boundary marker derived from system metadata events."""

    event_id: int
    timestamp: datetime
    source_path: str | None
    source_offset: int | None


@dataclass(frozen=True)
class RewindSignal:
    """Detected rewind trigger in incoming payload."""

    source_path: str
    source_offset: int
    reason: str


@dataclass(frozen=True)
class SessionProjectionItem:
    kind: str  # "event" | "seam"
    session: AgentSession
    event: AgentEvent | None = None
    parent_session: AgentSession | None = None


@dataclass(frozen=True)
class SessionProjectionPage:
    path_sessions: list[AgentSession]
    items: list[SessionProjectionItem]
    total: int
    abandoned_events: int
    branch_mode: str
    page_offset: int = 0
