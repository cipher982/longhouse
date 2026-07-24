from datetime import UTC
from datetime import datetime

from zerg.services.managed_provider_capability_decisions import evaluate_managed_provider_capability
from zerg.services.provider_capability_contract import ProductAction
from zerg.services.provider_capability_contract import RuntimeState
from zerg.services.provider_capability_evaluator import EvaluationContext


def _context(provider: str, *, mode: str = "helm") -> EvaluationContext:
    return EvaluationContext(
        machine_id="machine-1",
        session_id="session-1",
        provider=provider,
        mode=mode,
        observed_at=datetime(2026, 7, 22, 16, 0, tzinfo=UTC),
        runtime=RuntimeState.READY,
    )


def test_undeclared_opencode_coordination_awareness_remains_unavailable() -> None:
    assert (
        evaluate_managed_provider_capability(
            capability_id="coordination.awareness.create",
            context=_context("opencode"),
        )
        is None
    )


def test_undeclared_cursor_coordination_awareness_remains_unavailable() -> None:
    assert (
        evaluate_managed_provider_capability(
            capability_id="coordination.awareness.create",
            context=_context("cursor"),
        )
        is None
    )


def test_upstream_absent_cursor_steer_is_disabled_even_with_ready_runtime() -> None:
    decision = evaluate_managed_provider_capability(
        capability_id="session.input.steer_active",
        context=_context("cursor"),
    )

    assert decision is not None
    assert decision.action is ProductAction.DISABLED
    assert "upstream_unavailable" in decision.reason_codes
