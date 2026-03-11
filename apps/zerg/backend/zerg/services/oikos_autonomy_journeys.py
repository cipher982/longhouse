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

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[5]
DecisionCallable = Callable[["AutonomyContextPacket"], "AutonomyDecision | Awaitable[AutonomyDecision]"]
DEFAULT_AUTONOMY_JOURNEY_FIXTURE_PATH = _BACKEND_ROOT / "tests_lite" / "fixtures" / "oikos_autonomy_journeys.yml"
DEFAULT_AUTONOMY_ARTIFACT_ROOT = _REPO_ROOT / ".tmp" / "oikos-autonomy-journeys"


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-") or "journey"


def _optional_clean_str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    return str(value).strip()


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
            project=_optional_clean_str(data, "project"),
            last_user_message=_optional_clean_str(data, "last_user_message"),
            last_ai_message=_optional_clean_str(data, "last_ai_message"),
            summary=_optional_clean_str(data, "summary"),
            presence_state=_optional_clean_str(data, "presence_state"),
            blocked_reason=_optional_clean_str(data, "blocked_reason"),
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
            description=_optional_clean_str(data, "description"),
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
    needs_human: bool | None = None
    forbidden_actions: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ExpectedJourneyOutcome":
        payload = data or {}
        return cls(
            decision=str(payload.get("decision", "")).strip(),
            action_count=int(payload.get("action_count", 0)),
            needs_human=bool(payload["needs_human"]) if "needs_human" in payload else None,
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
    assertions: list["AutonomyAssertionResult"]
    run_dir: Path
    manifest_path: Path
    context_path: Path
    decision_path: Path
    assertions_path: Path


@dataclass(frozen=True)
class AutonomyAssertionResult:
    """One deterministic check against the expected journey outcome."""

    name: str
    passed: bool
    message: str
    expected: Any | None = None
    actual: Any | None = None


def load_autonomy_journey_cases(path: Path) -> list[AutonomyJourneyCase]:
    """Load journey fixtures from YAML."""
    raw = yaml.safe_load(path.read_text()) or {}
    cases = raw.get("cases") or []
    return [AutonomyJourneyCase.from_dict(case) for case in cases]


async def baseline_shadow_decider(packet: AutonomyContextPacket) -> AutonomyDecision:
    """Cheap deterministic baseline used for harness validation and local dogfooding."""
    ai_text = (packet.primary_session.last_ai_message or "").lower()
    trigger_summary = (packet.trigger.summary or "").lower()

    if packet.trigger.type == "session_blocked":
        return AutonomyDecision(
            decision="escalate",
            rationale="The session is blocked on a real product fork and needs user input.",
            summary="Escalate the blocker to the user instead of auto-continuing.",
            proposed_actions=[
                AutonomyProposedAction(
                    kind="notify_user",
                    target_session_id=packet.primary_session.session_id,
                    summary="Send a concise summary of the product fork to the user.",
                )
            ],
            needs_human=True,
        )

    if packet.trigger.type == "session_needs_user":
        return AutonomyDecision(
            decision="ignore",
            rationale="The session is paused for user input, with no reason for Oikos to re-prompt yet.",
            summary="Leave the session parked until the user comes back.",
            proposed_actions=[],
            needs_human=False,
        )

    if packet.trigger.type == "duplicate_wakeup" or "duplicate wakeup" in trigger_summary:
        return AutonomyDecision(
            decision="ignore",
            rationale="The wakeup repeats an existing blocked or needs_user state and would only create churn.",
            summary="Ignore the duplicate wakeup and avoid busywork.",
            proposed_actions=[],
            needs_human=False,
        )

    if packet.trigger.type == "session_completed" and "tests were not run" in ai_text:
        return AutonomyDecision(
            decision="continue_session",
            rationale="The session explicitly left one bounded verification step undone.",
            summary="Continue the session to run the pending targeted tests.",
            proposed_actions=[
                AutonomyProposedAction(
                    kind="continue_session",
                    target_session_id=packet.primary_session.session_id,
                    summary="Ask the same session to run the pending targeted tests.",
                )
            ],
            needs_human=False,
        )

    return AutonomyDecision(
        decision="ignore",
        rationale="Nothing in the wakeup suggests a meaningful next action.",
        summary="No follow-up action needed.",
        proposed_actions=[],
        needs_human=False,
    )


async def run_autonomy_journeys(
    *,
    fixture_path: Path = DEFAULT_AUTONOMY_JOURNEY_FIXTURE_PATH,
    artifact_root: Path = DEFAULT_AUTONOMY_ARTIFACT_ROOT,
    decider: DecisionCallable = baseline_shadow_decider,
) -> list[AutonomyJourneyResult]:
    """Execute a fixture file of autonomy journeys and persist artifacts."""
    cases = load_autonomy_journey_cases(fixture_path)
    artifact_root.mkdir(parents=True, exist_ok=True)
    runner = OikosAutonomyJourneyRunner(artifact_root=artifact_root, decider=decider)
    results: list[AutonomyJourneyResult] = []
    for case in cases:
        results.append(await runner.run_case(case))
    return results


def evaluate_journey_assertions(
    case: AutonomyJourneyCase,
    decision: AutonomyDecision,
) -> list[AutonomyAssertionResult]:
    """Evaluate a decision against the fixture's expected outcome."""
    assertions = [
        AutonomyAssertionResult(
            name="decision",
            passed=decision.decision == case.expected.decision,
            message=f"decision={decision.decision} expected={case.expected.decision}",
            expected=case.expected.decision,
            actual=decision.decision,
        ),
        AutonomyAssertionResult(
            name="action_count",
            passed=len(decision.proposed_actions) == case.expected.action_count,
            message=f"action_count={len(decision.proposed_actions)} expected={case.expected.action_count}",
            expected=case.expected.action_count,
            actual=len(decision.proposed_actions),
        ),
    ]

    if case.expected.needs_human is not None:
        assertions.append(
            AutonomyAssertionResult(
                name="needs_human",
                passed=decision.needs_human == case.expected.needs_human,
                message=f"needs_human={decision.needs_human} expected={case.expected.needs_human}",
                expected=case.expected.needs_human,
                actual=decision.needs_human,
            )
        )

    proposed_action_kinds = {action.kind for action in decision.proposed_actions}
    for forbidden_action in case.expected.forbidden_actions:
        assertions.append(
            AutonomyAssertionResult(
                name=f"forbidden_action:{forbidden_action}",
                passed=forbidden_action not in proposed_action_kinds,
                message=f"forbidden_action={forbidden_action} present={forbidden_action in proposed_action_kinds}",
                expected=False,
                actual=forbidden_action in proposed_action_kinds,
            )
        )
    return assertions


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

        assertions = evaluate_journey_assertions(case, decision)
        run_dir = self._prepare_run_dir(case.id)
        manifest_path, context_path, decision_path, assertions_path = self._persist_artifacts(
            run_dir=run_dir,
            case=case,
            context_packet=context_packet,
            decision=decision,
            assertions=assertions,
        )
        return AutonomyJourneyResult(
            case_id=case.id,
            context_packet=context_packet,
            decision=decision,
            assertions=assertions,
            run_dir=run_dir,
            manifest_path=manifest_path,
            context_path=context_path,
            decision_path=decision_path,
            assertions_path=assertions_path,
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
        assertions: list[AutonomyAssertionResult],
    ) -> tuple[Path, Path, Path, Path]:
        manifest_path = run_dir / "manifest.json"
        context_path = run_dir / "context.json"
        decision_path = run_dir / "decision.json"
        assertions_path = run_dir / "assertions.json"

        manifest = {
            "case_id": case.id,
            "description": case.description,
            "shadow_mode": case.policy.shadow_mode,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "decision": decision.decision,
            "proposed_action_count": len(decision.proposed_actions),
            "assertion_count": len(assertions),
            "assertions_passed": all(assertion.passed for assertion in assertions),
        }

        manifest_path.write_text(json.dumps(manifest, indent=2, default=_json_default) + "\n")
        context_path.write_text(json.dumps(asdict(context_packet), indent=2, default=_json_default) + "\n")
        decision_path.write_text(json.dumps(asdict(decision), indent=2, default=_json_default) + "\n")
        assertions_payload = [asdict(assertion) for assertion in assertions]
        assertions_path.write_text(json.dumps(assertions_payload, indent=2, default=_json_default) + "\n")
        return manifest_path, context_path, decision_path, assertions_path
