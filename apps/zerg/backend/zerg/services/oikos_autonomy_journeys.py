"""Shadow-mode journey harness for proactive Oikos.

This module is intentionally small and generic. It provides:

- a durable journey case format
- a compact context-packet builder
- a pluggable decider interface
- artifact persistence for later inspection

It does NOT implement the full proactive runtime. The first use case is
dogfooding and QA for autonomy decisions before broader automation lands.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Awaitable
from typing import Callable

import yaml

DecisionCallable = Callable[["AutonomyContextPacket"], "AutonomyDecision | Awaitable[AutonomyDecision]"]


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-") or "journey"


@dataclass(frozen=True)
class AutonomyTrigger:
    """Reason Oikos woke up for a shadow-mode decision pass."""

    type: str
    source_session_id: str
    summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomyTrigger":
        return cls(
            type=str(data.get("type", "")).strip(),
            source_session_id=str(data.get("source_session_id", "")).strip(),
            summary=str(data["summary"]).strip() if data.get("summary") is not None else None,
            payload=dict(data.get("payload") or {}),
        )


@dataclass(frozen=True)
class AutonomySessionSnapshot:
    """Compact snapshot of one coding-agent session relevant to a decision."""

    session_id: str
    provider: str
    status: str
    resumable: bool = False
    project: str | None = None
    last_user_message: str | None = None
    last_ai_message: str | None = None
    summary: str | None = None
    presence_state: str | None = None
    blocked_reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomySessionSnapshot":
        return cls(
            session_id=str(data.get("session_id", "")).strip(),
            provider=str(data.get("provider", "")).strip(),
            status=str(data.get("status", "")).strip(),
            resumable=bool(data.get("resumable", False)),
            project=str(data["project"]).strip() if data.get("project") is not None else None,
            last_user_message=(str(data["last_user_message"]).strip() if data.get("last_user_message") is not None else None),
            last_ai_message=str(data["last_ai_message"]).strip() if data.get("last_ai_message") is not None else None,
            summary=str(data["summary"]).strip() if data.get("summary") is not None else None,
            presence_state=str(data["presence_state"]).strip() if data.get("presence_state") is not None else None,
            blocked_reason=str(data["blocked_reason"]).strip() if data.get("blocked_reason") is not None else None,
        )


@dataclass(frozen=True)
class AutonomyPolicy:
    """Operator-mode preferences applied to a decision pass."""

    shadow_mode: bool = True
    allow_continue: bool = False
    allow_notify: bool = True
    allow_small_repairs: bool = False
    cadence_minutes: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AutonomyPolicy":
        payload = data or {}
        cadence = payload.get("cadence_minutes")
        return cls(
            shadow_mode=bool(payload.get("shadow_mode", True)),
            allow_continue=bool(payload.get("allow_continue", False)),
            allow_notify=bool(payload.get("allow_notify", True)),
            allow_small_repairs=bool(payload.get("allow_small_repairs", False)),
            cadence_minutes=int(cadence) if cadence is not None else None,
        )


@dataclass(frozen=True)
class AutonomyArtifactRef:
    """Durable evidence pointer that a decider can inspect later if needed."""

    label: str
    path: str
    description: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomyArtifactRef":
        return cls(
            label=str(data.get("label", "")).strip(),
            path=str(data.get("path", "")).strip(),
            description=str(data["description"]).strip() if data.get("description") is not None else None,
        )


@dataclass(frozen=True)
class AutonomyProposedAction:
    """Bounded action Oikos would like to take."""

    kind: str
    target_session_id: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AutonomyDecision:
    """Decision produced by a shadow-mode autonomy pass."""

    decision: str
    rationale: str
    summary: str
    proposed_actions: list[AutonomyProposedAction] = field(default_factory=list)
    needs_human: bool = False


@dataclass(frozen=True)
class AutonomyContextPacket:
    """Compact, reconstructable packet given to the autonomy decider."""

    case_id: str
    description: str
    trigger: AutonomyTrigger
    primary_session: AutonomySessionSnapshot
    active_sessions: list[AutonomySessionSnapshot]
    policy: AutonomyPolicy
    artifacts: list[AutonomyArtifactRef]


@dataclass(frozen=True)
class ExpectedJourneyOutcome:
    """Expectation block stored in journey fixtures for deterministic testing."""

    decision: str
    action_count: int = 0
    forbidden_actions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExpectedJourneyOutcome":
        payload = data or {}
        return cls(
            decision=str(payload.get("decision", "")).strip(),
            action_count=int(payload.get("action_count", 0)),
            forbidden_actions=[str(item).strip() for item in payload.get("forbidden_actions", [])],
        )


@dataclass(frozen=True)
class AutonomyJourneyCase:
    """One realistic autonomy journey fixture."""

    id: str
    description: str
    trigger: AutonomyTrigger
    primary_session: AutonomySessionSnapshot
    active_sessions: list[AutonomySessionSnapshot]
    policy: AutonomyPolicy
    artifacts: list[AutonomyArtifactRef]
    expected: ExpectedJourneyOutcome

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutonomyJourneyCase":
        return cls(
            id=str(data.get("id", "")).strip(),
            description=str(data.get("description", "")).strip(),
            trigger=AutonomyTrigger.from_dict(data.get("trigger") or {}),
            primary_session=AutonomySessionSnapshot.from_dict(data.get("primary_session") or {}),
            active_sessions=[AutonomySessionSnapshot.from_dict(item) for item in (data.get("active_sessions") or [])],
            policy=AutonomyPolicy.from_dict(data.get("policy")),
            artifacts=[AutonomyArtifactRef.from_dict(item) for item in (data.get("artifacts") or [])],
            expected=ExpectedJourneyOutcome.from_dict(data.get("expected")),
        )


@dataclass(frozen=True)
class AutonomyJourneyResult:
    """Result from running one shadow-mode journey case."""

    case_id: str
    context_packet: AutonomyContextPacket
    decision: AutonomyDecision
    run_dir: Path
    manifest_path: Path
    context_path: Path
    decision_path: Path


def load_autonomy_journey_cases(path: Path) -> list[AutonomyJourneyCase]:
    """Load journey fixtures from YAML."""
    raw = yaml.safe_load(path.read_text()) or {}
    cases = raw.get("cases") or []
    return [AutonomyJourneyCase.from_dict(case) for case in cases]


class OikosAutonomyJourneyRunner:
    """Run autonomy journey cases and persist their artifacts."""

    def __init__(self, *, artifact_root: Path, decider: DecisionCallable):
        self.artifact_root = artifact_root
        self.decider = decider

    def build_context_packet(self, case: AutonomyJourneyCase) -> AutonomyContextPacket:
        """Build the compact packet a future model-backed decider would inspect."""
        return AutonomyContextPacket(
            case_id=case.id,
            description=case.description,
            trigger=case.trigger,
            primary_session=case.primary_session,
            active_sessions=case.active_sessions,
            policy=case.policy,
            artifacts=case.artifacts,
        )

    async def run_case(self, case: AutonomyJourneyCase) -> AutonomyJourneyResult:
        """Execute one journey case using the configured decider."""
        context_packet = self.build_context_packet(case)
        decision = self.decider(context_packet)
        if inspect.isawaitable(decision):
            decision = await decision

        if not isinstance(decision, AutonomyDecision):
            raise TypeError("decider must return AutonomyDecision")

        run_dir = self._prepare_run_dir(case.id)
        manifest_path, context_path, decision_path = self._persist_artifacts(
            run_dir=run_dir,
            case=case,
            context_packet=context_packet,
            decision=decision,
        )
        return AutonomyJourneyResult(
            case_id=case.id,
            context_packet=context_packet,
            decision=decision,
            run_dir=run_dir,
            manifest_path=manifest_path,
            context_path=context_path,
            decision_path=decision_path,
        )

    def _prepare_run_dir(self, case_id: str) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = self.artifact_root / f"{timestamp}-{_slugify(case_id)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_dir

    def _persist_artifacts(
        self,
        *,
        run_dir: Path,
        case: AutonomyJourneyCase,
        context_packet: AutonomyContextPacket,
        decision: AutonomyDecision,
    ) -> tuple[Path, Path, Path]:
        manifest_path = run_dir / "manifest.json"
        context_path = run_dir / "context.json"
        decision_path = run_dir / "decision.json"

        manifest = {
            "case_id": case.id,
            "description": case.description,
            "shadow_mode": case.policy.shadow_mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decision": decision.decision,
            "proposed_action_count": len(decision.proposed_actions),
        }

        manifest_path.write_text(json.dumps(manifest, indent=2, default=_json_default) + "\n")
        context_path.write_text(json.dumps(asdict(context_packet), indent=2, default=_json_default) + "\n")
        decision_path.write_text(json.dumps(asdict(decision), indent=2, default=_json_default) + "\n")
        return manifest_path, context_path, decision_path
