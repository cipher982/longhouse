"""LLM-based decision maker for roundabout monitoring.

v2.0 Philosophy: Trust the AI, Remove Scaffolding
-------------------------------------------------
This module provides the LLM-based decision layer for roundabout monitoring.
The supervisor polls worker status ("glancing at a second monitor") and the LLM
interprets what it sees to decide: wait, exit early, cancel, or peek.

This is the v2.0 default approach. Heuristic mode is deprecated but kept for
backwards compatibility.

The LLM decider is gated by hard guardrails (not heuristics):
- Poll interval (only call every N polls) - rate limiting
- Max calls budget (limit total LLM calls per job) - cost control
- Timeout (fail fast on slow responses) - responsiveness

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
    """Decision mode for roundabout monitoring.

    v2.0 default: LLM mode (trust the AI to interpret status)
    Deprecated: HEURISTIC mode (pre-programmed decision engine)
    """

    HEURISTIC = "heuristic"  # DEPRECATED: Rules-based only (v1.0 approach)
    LLM = "llm"  # v2.0 default: LLM interprets status and decides
    HYBRID = "hybrid"  # DEPRECATED: Heuristic first, then LLM for ambiguous cases


# Default configuration (v2.0: Trust the AI)
DEFAULT_DECISION_MODE = DecisionMode.LLM  # v2.0 default: let LLM interpret status
DEFAULT_LLM_POLL_INTERVAL = 2  # Call LLM every N polls (rate limiting guardrail)
DEFAULT_LLM_MAX_CALLS = 3  # Max LLM calls per job (cost control guardrail)
DEFAULT_LLM_TIMEOUT_SECONDS = 1.5  # Max time to wait for LLM response (responsiveness guardrail)


def get_routing_model() -> str:
    """Get the model for routing decisions.

    Priority:
    1. ROUNDABOUT_ROUTING_MODEL env var (explicit override via settings)
    2. Default: use_case lookup for "routing_decision" (TIER_1)

    The routing decision is tiny (~5 tokens output) but decision quality is CRITICAL.
    Cost difference between tiers is negligible for this use case, so TIER_1 is default.

    However, if you experience timeouts (the 1.5s default is tight for some models),
    you can either:
    - Set ROUNDABOUT_LLM_TIMEOUT to a higher value (e.g., 2.5)
    - Set ROUNDABOUT_ROUTING_MODEL to a faster model (e.g., "gpt-5-mini")
    """
    from zerg.config import get_settings
    from zerg.models_config import get_model_for_use_case

    settings = get_settings()
    if settings.roundabout_routing_model:
        return settings.roundabout_routing_model
    return get_model_for_use_case("routing_decision")


def get_routing_timeout() -> float:
    """Get the timeout for routing LLM calls.

    Default: 1.5s (tight but responsive)
    Override via ROUNDABOUT_LLM_TIMEOUT env var.
    """
    from zerg.config import get_settings

    return get_settings().roundabout_llm_timeout


@dataclass
class LLMDecisionPayload:
    """Compact payload for LLM decision making.

    Kept under ~1-2KB to minimize latency and cost.

    v2.0: Passes raw timing data instead of pre-computed is_stuck boolean.
    The LLM can judge whether an operation is "stuck" based on context
    (e.g., 45s for 'du -sh /var' is normal, 45s for 'ls' is stuck).
    """

    job_id: int
    status: str
    elapsed_seconds: float
    # v2.0: Raw timing instead of pre-computed is_stuck
    current_op_elapsed_seconds: float | None  # How long current operation has been running
    current_op_name: str | None  # Tool name (e.g., "ssh_exec")
    current_op_args: str | None  # Tool arguments preview (e.g., "du -sh /var/*")
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

    v2.0: Passes raw timing and operation context instead of pre-computed is_stuck.
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

    # v2.0: Extract current operation details for contextual stuck judgment
    current_op_elapsed = None
    current_op_name = None
    current_op_args = None
    if ctx.current_operation:
        current_op_elapsed = round(ctx.stuck_seconds, 1)
        current_op_name = ctx.current_operation.tool_name
        current_op_args = ctx.current_operation.args_preview

    return LLMDecisionPayload(
        job_id=ctx.job_id,
        status=ctx.status,
        elapsed_seconds=round(ctx.elapsed_seconds, 1),
        current_op_elapsed_seconds=current_op_elapsed,
        current_op_name=current_op_name,
        current_op_args=current_op_args,
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
- exit: Return immediately if the worker appears to have completed its task and produced useful output. Use your semantic understanding to recognize completion regardless of specific wording.
- cancel: Abort if stuck, repeated failures, or clearly wrong path.
- peek: Request more details if you need full logs. Use sparingly.

Judging "stuck" - use context:
- current_op_elapsed_seconds: How long the current operation has been running
- current_op_name: The tool being used (e.g., "ssh_exec")
- current_op_args: What command/args (e.g., "du -sh /var/*")

Examples: 45s for "du -sh /var" = normal. 45s for "ls" = stuck. 60s for "find / -name..." = normal.
If current_op fields are null, no operation is in progress.

When in doubt, return "wait". Be conservative with exit/cancel."""

    # Format payload as compact JSON for user prompt
    user_content = json.dumps(asdict(payload), indent=2)
    user_prompt = f"""Worker monitoring check:

{user_content}

What action should be taken? Reply with exactly one word: wait, exit, cancel, or peek."""

    return system_prompt, user_prompt


async def call_llm_decider(
    payload: LLMDecisionPayload,
    model: str | None = None,
    timeout_seconds: float | None = None,
) -> LLMDecisionResult:
    """Call LLM to make a decision.

    Args:
        payload: The decision context
        model: Model to use (default: from settings or TIER_1 via use_case lookup)
        timeout_seconds: Max time to wait for response (default: from settings, 1.5s)

    Returns:
        LLMDecisionResult with action, rationale, and timing

    Note:
        Supports custom OpenAI-compatible endpoints via OPENAI_BASE_URL env var.
        This enables Azure OpenAI or other compatible providers.

        Configure defaults via env vars:
        - ROUNDABOUT_ROUTING_MODEL: Override the model (e.g., "gpt-5-mini")
        - ROUNDABOUT_LLM_TIMEOUT: Override timeout (e.g., "2.5")
    """
    import os
    import time

    from openai import AsyncOpenAI

    from zerg.config import get_settings

    settings = get_settings()

    # Resolve defaults from settings if not provided
    if model is None:
        model = get_routing_model()
    if timeout_seconds is None:
        timeout_seconds = get_routing_timeout()

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
    model: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[str, str, LLMDecisionResult]:
    """High-level function to make an LLM-based roundabout decision.

    Args:
        ctx: The decision context from roundabout monitor
        model: LLM model to use (default: from settings or TIER_1)
        timeout_seconds: Max time for LLM call (default: from settings, 1.5s)

    Returns:
        Tuple of (action, rationale, result)
    """
    payload = build_decision_payload(ctx)
    result = await call_llm_decider(payload, model, timeout_seconds)
    return result.action, result.rationale, result


# =============================================================================
# BACKWARDS COMPATIBILITY - Legacy constant names via lazy accessor
# =============================================================================


def __getattr__(name: str):
    """Support legacy constant names via lazy loading."""
    if name == "DEFAULT_LLM_MODEL":
        # Return the routing model from settings (or default)
        return get_routing_model()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
