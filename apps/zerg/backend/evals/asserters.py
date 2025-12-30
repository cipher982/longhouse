"""Assertion functions for eval test cases.

This module provides assertion functions that validate eval results:
- contains: Text contains substring
- regex: Text matches regex pattern
- tool_called: Supervisor called specific tool
- worker_spawned: Number of workers spawned
- latency_ms: Execution time bounds
- total_tokens: Token usage bounds
- status: Run status check
"""

from __future__ import annotations

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
    # Query tool calls from the database
    # We need to check the agent_run's checkpoint for tool calls
    # For now, we'll return a stub implementation that checks the result text
    # TODO: Implement proper tool call tracking in Phase 2

    # Stub implementation - check if tool name appears in result
    if metrics.result_text and tool_name.lower() in metrics.result_text.lower():
        return True, f"Tool '{tool_name}' was called"
    else:
        return False, f"Tool '{tool_name}' was not called"


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


# Registry of asserters
ASSERTERS = {
    "contains": assert_contains,
    "regex": assert_regex,
    "tool_called": assert_tool_called,
    "worker_spawned": assert_worker_spawned,
    "latency_ms": assert_latency_ms,
    "total_tokens": assert_total_tokens,
    "status": assert_status,
}


def run_assertion(
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

    return asserter(metrics, **kwargs)
