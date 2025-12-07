"""LLM-based decision maker for roundabout monitoring.

This module provides an optional LLM-based decision layer for the roundabout
monitoring loop. It uses a tiny, fast model to analyze worker state and decide
whether to wait, exit early, cancel, or peek.

The LLM decider is gated by:
- Poll interval (only call every N polls)
- Max calls budget (limit total LLM calls per job)
- Timeout (fail fast on slow responses)

Default is safe: on any LLM failure, returns "wait" to continue monitoring.
"""

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zerg.services.roundabout_monitor import DecisionContext, ToolActivity

logger = logging.getLogger(__name__)


class DecisionMode(Enum):
    """Decision mode for roundabout monitoring."""

    HEURISTIC = "heuristic"  # Rules-based only (default, safe)
    LLM = "llm"  # LLM-based only
    HYBRID = "hybrid"  # Heuristic first, then LLM for ambiguous cases


# Default configuration
DEFAULT_DECISION_MODE = DecisionMode.HEURISTIC
DEFAULT_LLM_POLL_INTERVAL = 2  # Call LLM every N polls
DEFAULT_LLM_MAX_CALLS = 3  # Max LLM calls per job
DEFAULT_LLM_TIMEOUT_SECONDS = 1.5  # Max time to wait for LLM response
DEFAULT_LLM_MODEL = "gpt-4o-mini"  # Fast, cheap model


@dataclass
class LLMDecisionPayload:
    """Compact payload for LLM decision making.

    Kept under ~1-2KB to minimize latency and cost.
    """

    job_id: int
    status: str
    elapsed_seconds: float
    is_stuck: bool
    last_3_tools: list[dict]  # name, status, duration_ms, error (truncated)
    activity_counts: dict  # total, completed, failed, monitoring_checks
    log_tail: str  # 400-800 chars of recent output


@dataclass
class LLMDecisionResult:
    """Result from LLM decision call."""

    action: str  # wait, exit, cancel, peek
    rationale: str
    response_time_ms: float
    was_fallback: bool = False  # True if fell back to wait due to error/timeout


@dataclass
class LLMDeciderStats:
    """Statistics for LLM decider calls."""

    calls_made: int = 0
    calls_succeeded: int = 0
    calls_timed_out: int = 0
    calls_errored: int = 0
    calls_skipped_budget: int = 0
    calls_skipped_interval: int = 0
    total_response_time_ms: float = 0.0

    def record_call(self, result: LLMDecisionResult) -> None:
        """Record a call result."""
        self.calls_made += 1
        self.total_response_time_ms += result.response_time_ms
        if result.was_fallback:
            if "timeout" in result.rationale.lower():
                self.calls_timed_out += 1
            else:
                self.calls_errored += 1
        else:
            self.calls_succeeded += 1

    def record_skip(self, reason: str) -> None:
        """Record a skipped call."""
        if reason == "budget":
            self.calls_skipped_budget += 1
        elif reason == "interval":
            self.calls_skipped_interval += 1

    def to_dict(self) -> dict:
        """Convert to dictionary for activity summary.

        Always includes skip counters when non-zero, even if no LLM calls were made.
        This ensures observability when jobs stay in skip state due to budget/interval.
        """
        result = {}

        # Always include call stats if any calls were made
        if self.calls_made > 0:
            result["llm_calls"] = self.calls_made
            result["llm_calls_succeeded"] = self.calls_succeeded
            result["llm_avg_response_ms"] = round(
                self.total_response_time_ms / self.calls_made, 1
            )

        # Include non-zero counts (even if no calls were made)
        if self.calls_timed_out > 0:
            result["llm_timeouts"] = self.calls_timed_out
        if self.calls_errored > 0:
            result["llm_errors"] = self.calls_errored
        if self.calls_skipped_budget > 0:
            result["llm_skipped_budget"] = self.calls_skipped_budget
        if self.calls_skipped_interval > 0:
            result["llm_skipped_interval"] = self.calls_skipped_interval

        return result


def build_decision_payload(ctx: "DecisionContext") -> LLMDecisionPayload:
    """Build a compact payload from DecisionContext.

    Keeps payload under ~1-2KB by:
    - Only including last 3 tool activities
    - Truncating log tail to 400-800 chars
    - Using compact field names
    """
    # Last 3 tools with essential info only
    last_3 = ctx.tool_activities[-3:] if ctx.tool_activities else []
    tools_compact = [
        {
            "name": t.tool_name,
            "status": t.status,
            "duration_ms": t.duration_ms,
            "error": t.error[:100] if t.error else None,
        }
        for t in last_3
    ]

    # Activity counts
    total = len(ctx.tool_activities)
    completed = sum(1 for t in ctx.tool_activities if t.status == "completed")
    failed = sum(1 for t in ctx.tool_activities if t.status == "failed")

    # Log tail (truncate to 400-800 chars)
    log_tail = ""
    if ctx.last_tool_output:
        log_tail = ctx.last_tool_output[-600:]  # Take last 600 chars
        if len(ctx.last_tool_output) > 600:
            log_tail = "..." + log_tail

    return LLMDecisionPayload(
        job_id=ctx.job_id,
        status=ctx.status,
        elapsed_seconds=round(ctx.elapsed_seconds, 1),
        is_stuck=ctx.is_stuck,
        last_3_tools=tools_compact,
        activity_counts={
            "total": total,
            "completed": completed,
            "failed": failed,
            "monitoring_checks": ctx.polls_without_progress,  # Use polls counter
        },
        log_tail=log_tail,
    )


def build_llm_prompt(payload: LLMDecisionPayload) -> tuple[str, str]:
    """Build system and user prompts for LLM decision.

    Returns:
        Tuple of (system_prompt, user_prompt)
    """
    system_prompt = """You are a worker monitoring assistant. Given the current state of a background task, decide the next action.

Return EXACTLY ONE word from: wait, exit, cancel, peek

Decision rules:
- wait: Default. Continue monitoring if task is progressing normally.
- exit: Return immediately if the task has clearly produced a final answer or result. Look for output containing "Result:", "Summary:", "Done.", "Completed", or similar completion indicators.
- cancel: Abort the worker if it appears stuck (>60s on one operation), has repeated failures, or is clearly on a wrong path.
- peek: Request more details if you need to see full logs before deciding. Use sparingly.

When in doubt, return "wait". Be conservative with exit/cancel."""

    # Format payload as compact JSON for user prompt
    user_content = json.dumps(asdict(payload), indent=2)
    user_prompt = f"""Worker monitoring check:

{user_content}

What action should be taken? Reply with exactly one word: wait, exit, cancel, or peek."""

    return system_prompt, user_prompt


async def call_llm_decider(
    payload: LLMDecisionPayload,
    model: str = DEFAULT_LLM_MODEL,
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
) -> LLMDecisionResult:
    """Call LLM to make a decision.

    Args:
        payload: The decision context
        model: Model to use (default: gpt-4o-mini for speed/cost)
        timeout_seconds: Max time to wait for response

    Returns:
        LLMDecisionResult with action, rationale, and timing

    Note:
        Supports custom OpenAI-compatible endpoints via OPENAI_BASE_URL env var.
        This enables Azure OpenAI or other compatible providers.
    """
    import os
    import time

    from openai import AsyncOpenAI

    from zerg.config import get_settings

    settings = get_settings()

    # Build client kwargs - support custom base URL for Azure/compatible endpoints
    client_kwargs = {"api_key": settings.openai_api_key}
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        client_kwargs["base_url"] = base_url

    client = AsyncOpenAI(**client_kwargs)
    system_prompt, user_prompt = build_llm_prompt(payload)

    start = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=8,  # We only need one word
                temperature=0,
            ),
            timeout=timeout_seconds,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Parse response
        raw_response = response.choices[0].message.content.strip().lower()

        # Validate response is one of the valid actions
        valid_actions = {"wait", "exit", "cancel", "peek"}
        if raw_response in valid_actions:
            action = raw_response
            rationale = f"LLM decided: {action}"
            was_fallback = False
        else:
            # Invalid response, default to wait
            action = "wait"
            rationale = f"Invalid LLM response '{raw_response}', defaulting to wait"
            was_fallback = True

        logger.debug(
            f"LLM decider for job {payload.job_id}: {action} ({elapsed_ms:.0f}ms)"
        )
        return LLMDecisionResult(
            action=action,
            rationale=rationale,
            response_time_ms=elapsed_ms,
            was_fallback=was_fallback,
        )

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.warning(
            f"LLM decider timeout for job {payload.job_id} after {elapsed_ms:.0f}ms"
        )
        return LLMDecisionResult(
            action="wait",
            rationale=f"LLM timeout after {elapsed_ms:.0f}ms, defaulting to wait",
            response_time_ms=elapsed_ms,
            was_fallback=True,
        )
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.warning(f"LLM decider error for job {payload.job_id}: {e}")
        return LLMDecisionResult(
            action="wait",
            rationale=f"LLM error: {e}, defaulting to wait",
            response_time_ms=elapsed_ms,
            was_fallback=True,
        )


async def decide_roundabout_action(
    ctx: "DecisionContext",
    model: str = DEFAULT_LLM_MODEL,
    timeout_seconds: float = DEFAULT_LLM_TIMEOUT_SECONDS,
) -> tuple[str, str, LLMDecisionResult]:
    """High-level function to make an LLM-based roundabout decision.

    Args:
        ctx: The decision context from roundabout monitor
        model: LLM model to use
        timeout_seconds: Max time for LLM call

    Returns:
        Tuple of (action, rationale, result)
    """
    payload = build_decision_payload(ctx)
    result = await call_llm_decider(payload, model, timeout_seconds)
    return result.action, result.rationale, result
