"""Assertion functions for eval test cases.

This module provides assertion functions that validate eval results:
- contains: Text contains substring
- regex: Text matches regex pattern
- tool_called: Supervisor called specific tool
- worker_spawned: Number of workers spawned
- latency_ms: Execution time bounds
- total_tokens: Token usage bounds
- status: Run status check
- llm_graded: LLM-as-judge evaluation
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evals.runner import EvalMetrics


def assert_contains(
    metrics: EvalMetrics,
    value: str,
    case_insensitive: bool = False,
) -> tuple[bool, str]:
    """Assert that result text contains substring.

    Args:
        metrics: EvalMetrics from run
        value: Substring to search for
        case_insensitive: Whether to ignore case

    Returns:
        (passed, message) tuple
    """
    if not metrics.result_text:
        return False, f"No result text (status={metrics.status})"

    text = metrics.result_text
    search_value = value

    if case_insensitive:
        text = text.lower()
        search_value = search_value.lower()

    if search_value in text:
        return True, f"Result contains '{value}'"
    else:
        return False, f"Result does not contain '{value}'"


def assert_regex(
    metrics: EvalMetrics,
    pattern: str,
    flags: str = "",
) -> tuple[bool, str]:
    """Assert that result text matches regex pattern.

    Args:
        metrics: EvalMetrics from run
        pattern: Regex pattern
        flags: Regex flags (e.g., "i" for case-insensitive)

    Returns:
        (passed, message) tuple
    """
    if not metrics.result_text:
        return False, f"No result text (status={metrics.status})"

    # Parse flags
    re_flags = 0
    if "i" in flags.lower():
        re_flags |= re.IGNORECASE
    if "m" in flags.lower():
        re_flags |= re.MULTILINE
    if "s" in flags.lower():
        re_flags |= re.DOTALL

    if re.search(pattern, metrics.result_text, re_flags):
        return True, f"Result matches pattern '{pattern}'"
    else:
        return False, f"Result does not match pattern '{pattern}'"


def assert_tool_called(
    metrics: EvalMetrics,
    tool_name: str,
) -> tuple[bool, str]:
    """Assert that a specific tool was called during the run.

    Args:
        metrics: EvalMetrics from run
        tool_name: Name of the tool that should have been called

    Returns:
        (passed, message) tuple
    """
    if tool_name in metrics.tools_called:
        return True, f"Tool '{tool_name}' was called"

    observed = ", ".join(metrics.tools_called) if metrics.tools_called else "(none)"
    return False, f"Tool '{tool_name}' was not called (observed: {observed})"


def assert_worker_spawned(
    metrics: EvalMetrics,
    count: int | None = None,
    min_count: int | None = None,
    max_count: int | None = None,
) -> tuple[bool, str]:
    """Assert number of workers spawned.

    Args:
        metrics: EvalMetrics from run
        count: Exact count expected (if provided)
        min_count: Minimum count (if provided)
        max_count: Maximum count (if provided)

    Returns:
        (passed, message) tuple
    """
    actual = metrics.workers_spawned

    if count is not None:
        if actual == count:
            return True, f"Spawned {actual} worker(s) (expected {count})"
        else:
            return False, f"Spawned {actual} worker(s), expected {count}"

    if min_count is not None and actual < min_count:
        return False, f"Spawned {actual} worker(s), expected at least {min_count}"

    if max_count is not None and actual > max_count:
        return False, f"Spawned {actual} worker(s), expected at most {max_count}"

    return True, f"Spawned {actual} worker(s)"


def assert_latency_ms(
    metrics: EvalMetrics,
    max_ms: int | None = None,
    min_ms: int | None = None,
) -> tuple[bool, str]:
    """Assert execution time bounds.

    Args:
        metrics: EvalMetrics from run
        max_ms: Maximum latency in milliseconds
        min_ms: Minimum latency in milliseconds

    Returns:
        (passed, message) tuple
    """
    actual = metrics.latency_ms

    if max_ms is not None and actual > max_ms:
        return False, f"Latency {actual}ms exceeds max {max_ms}ms"

    if min_ms is not None and actual < min_ms:
        return False, f"Latency {actual}ms below min {min_ms}ms"

    return True, f"Latency {actual}ms within bounds"


def assert_total_tokens(
    metrics: EvalMetrics,
    max_tokens: int | None = None,
) -> tuple[bool, str]:
    """Assert token usage bounds.

    Args:
        metrics: EvalMetrics from run
        max_tokens: Maximum total tokens

    Returns:
        (passed, message) tuple
    """
    actual = metrics.total_tokens

    if max_tokens is not None and actual > max_tokens:
        return False, f"Used {actual} tokens, max {max_tokens}"

    return True, f"Used {actual} tokens"


def assert_status(
    metrics: EvalMetrics,
    expected: str,
) -> tuple[bool, str]:
    """Assert run status.

    Args:
        metrics: EvalMetrics from run
        expected: Expected status (success, failed, deferred)

    Returns:
        (passed, message) tuple
    """
    if metrics.status == expected:
        return True, f"Status is {expected}"
    else:
        return False, f"Status is {metrics.status}, expected {expected}"


def assert_worker_result_contains(
    metrics: EvalMetrics,
    worker_id: int,
    value: str,
    case_insensitive: bool = False,
) -> tuple[bool, str]:
    """Assert that a worker's result contains specific text.

    Args:
        metrics: EvalMetrics from run
        worker_id: Ordinal index of worker (0-based, ordered by created_at)
        value: Substring to search for
        case_insensitive: Whether to ignore case

    Returns:
        (passed, message) tuple
    """
    from pathlib import Path
    from zerg.models.models import WorkerJob
    from zerg.services.worker_artifact_store import WorkerArtifactStore

    # Import db_session from runner (injected into metrics)
    # We need to query WorkerJob to get worker_id (UUID), then read artifact
    if not hasattr(metrics, "_db_session"):
        return False, "DB session not available in metrics"

    db_session = metrics._db_session

    # Get workers ordered by created_at (ordinal indexing)
    workers = (
        db_session.query(WorkerJob)
        .filter(WorkerJob.supervisor_run_id == metrics.run_id)
        .order_by(WorkerJob.created_at)
        .all()
    )

    if worker_id >= len(workers):
        return False, f"Worker index {worker_id} out of range (only {len(workers)} workers spawned)"

    job = workers[worker_id]
    if not job.worker_id:
        return False, f"Worker {worker_id} has no worker_id (not started yet)"

    # Read result from artifact store
    artifact_store = WorkerArtifactStore()
    result_path = Path(artifact_store.base_dir) / job.worker_id / "result.txt"

    if not result_path.exists():
        return False, f"Worker {worker_id} result file not found: {result_path}"

    result_text = result_path.read_text()
    search_text = result_text
    search_value = value

    if case_insensitive:
        search_text = search_text.lower()
        search_value = search_value.lower()

    if search_value in search_text:
        return True, f"Worker {worker_id} result contains '{value}'"
    else:
        return False, f"Worker {worker_id} result does not contain '{value}'"


def assert_worker_tool_called(
    metrics: EvalMetrics,
    worker_id: int,
    tool: str,
    min_calls: int = 1,
) -> tuple[bool, str]:
    """Assert that a worker called a specific tool.

    Args:
        metrics: EvalMetrics from run
        worker_id: Ordinal index of worker (0-based, ordered by created_at)
        tool: Tool name to check for
        min_calls: Minimum number of times tool should be called

    Returns:
        (passed, message) tuple
    """
    from zerg.models.agent_run_event import AgentRunEvent
    from zerg.models.models import WorkerJob

    if not hasattr(metrics, "_db_session"):
        return False, "DB session not available in metrics"

    db_session = metrics._db_session

    # Get workers ordered by created_at (ordinal indexing)
    workers = (
        db_session.query(WorkerJob)
        .filter(WorkerJob.supervisor_run_id == metrics.run_id)
        .order_by(WorkerJob.created_at)
        .all()
    )

    if worker_id >= len(workers):
        return False, f"Worker index {worker_id} out of range (only {len(workers)} workers spawned)"

    job = workers[worker_id]
    if not job.worker_id:
        return False, f"Worker {worker_id} has no worker_id (not started yet)"

    # Find the AgentRun for this worker by querying for worker_id
    from zerg.models.models import AgentRun

    worker_run = db_session.query(AgentRun).filter(AgentRun.run_id == job.worker_id).first()

    if not worker_run:
        return False, f"Worker {worker_id} has no agent run record"

    # Query events for this worker's run
    events = db_session.query(AgentRunEvent).filter(AgentRunEvent.run_id == worker_run.id).all()

    # Count tool calls
    tool_calls = 0
    for event in events:
        payload = event.payload or {}
        if payload.get("tool_name") == tool:
            tool_calls += 1

    if tool_calls >= min_calls:
        return True, f"Worker {worker_id} called '{tool}' {tool_calls} time(s) (min: {min_calls})"
    else:
        return False, f"Worker {worker_id} called '{tool}' {tool_calls} time(s), expected at least {min_calls}"


def assert_artifact_exists(
    metrics: EvalMetrics,
    worker_id: int,
    path: str,
) -> tuple[bool, str]:
    """Assert that a worker artifact file exists.

    Args:
        metrics: EvalMetrics from run
        worker_id: Ordinal index of worker (0-based, ordered by created_at)
        path: Relative path within worker's artifact directory (e.g., "metrics.jsonl")

    Returns:
        (passed, message) tuple
    """
    from pathlib import Path
    from zerg.models.models import WorkerJob
    from zerg.services.worker_artifact_store import WorkerArtifactStore

    if not hasattr(metrics, "_db_session"):
        return False, "DB session not available in metrics"

    db_session = metrics._db_session

    # Get workers ordered by created_at (ordinal indexing)
    workers = (
        db_session.query(WorkerJob)
        .filter(WorkerJob.supervisor_run_id == metrics.run_id)
        .order_by(WorkerJob.created_at)
        .all()
    )

    if worker_id >= len(workers):
        return False, f"Worker index {worker_id} out of range (only {len(workers)} workers spawned)"

    job = workers[worker_id]
    if not job.worker_id:
        return False, f"Worker {worker_id} has no worker_id (not started yet)"

    # Check if artifact exists
    artifact_store = WorkerArtifactStore()
    artifact_path = Path(artifact_store.base_dir) / job.worker_id / path

    if artifact_path.exists():
        return True, f"Worker {worker_id} artifact exists: {path}"
    else:
        return False, f"Worker {worker_id} artifact not found: {path}"


def assert_artifact_contains(
    metrics: EvalMetrics,
    worker_id: int,
    path: str,
    value: str,
    case_insensitive: bool = False,
) -> tuple[bool, str]:
    """Assert that a worker artifact file contains specific text.

    Args:
        metrics: EvalMetrics from run
        worker_id: Ordinal index of worker (0-based, ordered by created_at)
        path: Relative path within worker's artifact directory
        value: Substring to search for
        case_insensitive: Whether to ignore case

    Returns:
        (passed, message) tuple
    """
    from pathlib import Path
    from zerg.models.models import WorkerJob
    from zerg.services.worker_artifact_store import WorkerArtifactStore

    if not hasattr(metrics, "_db_session"):
        return False, "DB session not available in metrics"

    db_session = metrics._db_session

    # Get workers ordered by created_at (ordinal indexing)
    workers = (
        db_session.query(WorkerJob)
        .filter(WorkerJob.supervisor_run_id == metrics.run_id)
        .order_by(WorkerJob.created_at)
        .all()
    )

    if worker_id >= len(workers):
        return False, f"Worker index {worker_id} out of range (only {len(workers)} workers spawned)"

    job = workers[worker_id]
    if not job.worker_id:
        return False, f"Worker {worker_id} has no worker_id (not started yet)"

    # Read artifact
    artifact_store = WorkerArtifactStore()
    artifact_path = Path(artifact_store.base_dir) / job.worker_id / path

    if not artifact_path.exists():
        return False, f"Worker {worker_id} artifact not found: {path}"

    content = artifact_path.read_text()
    search_content = content
    search_value = value

    if case_insensitive:
        search_content = search_content.lower()
        search_value = search_value.lower()

    if search_value in search_content:
        return True, f"Worker {worker_id} artifact '{path}' contains '{value}'"
    else:
        return False, f"Worker {worker_id} artifact '{path}' does not contain '{value}'"


class SkipAssertion(Exception):
    """Raised when an assertion should be skipped (not failed)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def assert_llm_graded(
    metrics: EvalMetrics,
    rubric: str,
    min_score: float = 0.7,
    model: str = "gpt-5-mini",
) -> tuple[bool, str]:
    """Use LLM to grade response against rubric.

    Args:
        metrics: EvalMetrics from run
        rubric: Grading criteria
        min_score: Minimum score to pass (0.0-1.0)
        model: Model to use for grading

    Returns:
        (passed, message) tuple with score and reason

    Raises:
        SkipAssertion: When running in hermetic mode (live mode required)
    """
    import os

    # Check if we're in live mode (required for LLM grading)
    eval_mode = os.environ.get("EVAL_MODE", "hermetic")
    if eval_mode != "live":
        raise SkipAssertion(f"llm_graded requires EVAL_MODE=live (current: {eval_mode})")

    if not metrics.result_text:
        return False, f"No result text to grade (status={metrics.status})"

    from openai import AsyncOpenAI

    client = AsyncOpenAI()

    # Build grading prompt
    system_prompt = (
        "You are an eval grader. Score the response 0.0-1.0 based on the rubric. "
        "Output valid JSON with score and reason fields."
    )

    user_prompt = f"Rubric:\n{rubric}\n\nResponse to grade:\n{metrics.result_text}"

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=500,
        )

        # Parse response (guaranteed JSON by response_format)
        content = response.choices[0].message.content
        if not content:
            return False, f"LLM returned empty response (finish_reason={response.choices[0].finish_reason})"

        result = json.loads(content)
        score = float(result.get("score", 0.0))
        reason = result.get("reason", "No reason provided")

        passed = score >= min_score
        status_icon = "✓" if passed else "✗"
        return passed, f"{status_icon} Score: {score:.2f} (min: {min_score:.2f}) - {reason}"

    except json.JSONDecodeError as e:
        return False, f"Failed to parse LLM response as JSON: {e}"
    except Exception as e:
        return False, f"LLM grading error: {e}"


# Registry of asserters
ASSERTERS = {
    "contains": assert_contains,
    "regex": assert_regex,
    "tool_called": assert_tool_called,
    "worker_spawned": assert_worker_spawned,
    "latency_ms": assert_latency_ms,
    "total_tokens": assert_total_tokens,
    "status": assert_status,
    "llm_graded": assert_llm_graded,
    "worker_result_contains": assert_worker_result_contains,
    "worker_tool_called": assert_worker_tool_called,
    "artifact_exists": assert_artifact_exists,
    "artifact_contains": assert_artifact_contains,
}


async def run_assertion(
    metrics: EvalMetrics,
    assertion_type: str,
    **kwargs,
) -> tuple[bool, str]:
    """Run a single assertion.

    Args:
        metrics: EvalMetrics from run
        assertion_type: Type of assertion
        **kwargs: Assertion parameters

    Returns:
        (passed, message) tuple

    Raises:
        ValueError: If assertion type is unknown
    """
    asserter = ASSERTERS.get(assertion_type)
    if not asserter:
        raise ValueError(f"Unknown assertion type: {assertion_type}")

    # Handle async asserters
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(asserter):
        return await asserter(metrics, **kwargs)
    else:
        return asserter(metrics, **kwargs)
