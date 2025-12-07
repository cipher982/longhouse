#!/usr/bin/env python3
"""Validation script for LLM-gated roundabout decision making.

This script tests the LLM decider in isolation to validate:
1. The prompt design returns valid actions (wait/exit/cancel/peek)
2. Response times are acceptable (< 1.5s)
3. The decider handles various scenarios correctly

Run: cd apps/zerg/backend && uv run python scripts/test_llm_decider.py
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

# Add the parent directory to the path for imports
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class MockToolActivity:
    """Mock tool activity for testing."""
    tool_name: str
    status: str  # started, completed, failed
    duration_ms: int | None = None
    error: str | None = None


@dataclass
class LLMDecisionPayload:
    """Compact payload for LLM decision making."""
    job_id: int
    status: str
    elapsed_seconds: float
    is_stuck: bool
    last_3_tools: list[dict]  # name, status, duration_ms, error
    activity_counts: dict  # total, completed, failed, monitoring_checks
    log_tail: str  # 400-800 chars of recent output


def build_decision_payload(
    job_id: int,
    status: str,
    elapsed_seconds: float,
    is_stuck: bool,
    tool_activities: list[MockToolActivity],
    monitoring_checks: int,
    last_tool_output: str | None,
) -> LLMDecisionPayload:
    """Build a compact payload for LLM decision making.

    Keeps payload under ~1-2KB by:
    - Only including last 3 tool activities
    - Truncating log tail to 400-800 chars
    - Using compact field names
    """
    # Last 3 tools with essential info only
    last_3 = tool_activities[-3:] if tool_activities else []
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
    total = len(tool_activities)
    completed = sum(1 for t in tool_activities if t.status == "completed")
    failed = sum(1 for t in tool_activities if t.status == "failed")

    # Log tail (truncate to 400-800 chars)
    log_tail = ""
    if last_tool_output:
        log_tail = last_tool_output[-600:]  # Take last 600 chars
        if len(last_tool_output) > 600:
            log_tail = "..." + log_tail

    return LLMDecisionPayload(
        job_id=job_id,
        status=status,
        elapsed_seconds=round(elapsed_seconds, 1),
        is_stuck=is_stuck,
        last_3_tools=tools_compact,
        activity_counts={
            "total": total,
            "completed": completed,
            "failed": failed,
            "monitoring_checks": monitoring_checks,
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
    model: str = "gpt-4o-mini",
    timeout_seconds: float = 1.5,
) -> tuple[str, str, float]:
    """Call LLM to make a decision.

    Args:
        payload: The decision context
        model: Model to use (default: gpt-4o-mini for speed/cost)
        timeout_seconds: Max time to wait for response

    Returns:
        Tuple of (action, rationale, response_time_ms)
    """
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
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
        else:
            # Invalid response, default to wait
            action = "wait"
            rationale = f"Invalid LLM response '{raw_response}', defaulting to wait"

        return action, rationale, elapsed_ms

    except asyncio.TimeoutError:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return "wait", f"LLM timeout after {elapsed_ms:.0f}ms, defaulting to wait", elapsed_ms
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return "wait", f"LLM error: {e}, defaulting to wait", elapsed_ms


# Test scenarios
TEST_SCENARIOS = [
    {
        "name": "Normal progress - should wait",
        "payload": {
            "job_id": 1,
            "status": "running",
            "elapsed_seconds": 10.0,
            "is_stuck": False,
            "tool_activities": [
                MockToolActivity("ssh_exec", "completed", 1200),
                MockToolActivity("file_read", "started"),
            ],
            "monitoring_checks": 2,
            "last_tool_output": "Reading file contents...",
        },
        "expected_action": "wait",
    },
    {
        "name": "Final answer visible - should exit",
        "payload": {
            "job_id": 2,
            "status": "running",
            "elapsed_seconds": 15.0,
            "is_stuck": False,
            "tool_activities": [
                MockToolActivity("ssh_exec", "completed", 800),
                MockToolActivity("analyze", "completed", 500),
            ],
            "monitoring_checks": 3,
            "last_tool_output": "Result: Disk usage is 78% on cube server. All partitions healthy. Summary: No immediate action required.",
        },
        "expected_action": "exit",
    },
    {
        "name": "Stuck operation - should cancel",
        "payload": {
            "job_id": 3,
            "status": "running",
            "elapsed_seconds": 90.0,
            "is_stuck": True,
            "tool_activities": [
                MockToolActivity("ssh_exec", "completed", 1000),
                MockToolActivity("ssh_exec", "started"),  # Running for 65+ seconds
            ],
            "monitoring_checks": 18,
            "last_tool_output": "Connecting to server... (started 65 seconds ago)",
        },
        "expected_action": "cancel",
    },
    {
        "name": "Multiple failures - should cancel",
        "payload": {
            "job_id": 4,
            "status": "running",
            "elapsed_seconds": 30.0,
            "is_stuck": False,
            "tool_activities": [
                MockToolActivity("ssh_exec", "failed", 100, "Connection refused"),
                MockToolActivity("ssh_exec", "failed", 100, "Connection refused"),
                MockToolActivity("ssh_exec", "failed", 100, "Connection refused"),
            ],
            "monitoring_checks": 6,
            "last_tool_output": "Error: Connection refused. Retrying...",
        },
        "expected_action": "cancel",
    },
    {
        "name": "Worker success status - should exit",
        "payload": {
            "job_id": 5,
            "status": "success",
            "elapsed_seconds": 20.0,
            "is_stuck": False,
            "tool_activities": [
                MockToolActivity("ssh_exec", "completed", 1500),
                MockToolActivity("format_result", "completed", 200),
            ],
            "monitoring_checks": 4,
            "last_tool_output": "Task completed successfully.",
        },
        "expected_action": "exit",
    },
    {
        "name": "Early progress, clear success indicator - should exit",
        "payload": {
            "job_id": 6,
            "status": "running",
            "elapsed_seconds": 8.0,
            "is_stuck": False,
            "tool_activities": [
                MockToolActivity("http_request", "completed", 300),
            ],
            "monitoring_checks": 1,
            "last_tool_output": "Done. The API returned status 200. All systems operational.",
        },
        "expected_action": "exit",
    },
]


async def run_tests():
    """Run all test scenarios and report results."""
    print("=" * 60)
    print("LLM Decider Validation Tests")
    print("=" * 60)
    print()

    results = []
    total_time = 0.0

    for scenario in TEST_SCENARIOS:
        print(f"Testing: {scenario['name']}")
        print("-" * 40)

        # Build payload
        payload_args = scenario["payload"]
        payload = build_decision_payload(
            job_id=payload_args["job_id"],
            status=payload_args["status"],
            elapsed_seconds=payload_args["elapsed_seconds"],
            is_stuck=payload_args["is_stuck"],
            tool_activities=payload_args["tool_activities"],
            monitoring_checks=payload_args["monitoring_checks"],
            last_tool_output=payload_args["last_tool_output"],
        )

        # Print payload size
        payload_json = json.dumps(asdict(payload))
        print(f"  Payload size: {len(payload_json)} bytes")

        # Call LLM
        action, rationale, response_time = await call_llm_decider(payload)
        total_time += response_time

        # Check result
        expected = scenario["expected_action"]
        passed = action == expected
        status = "✓ PASS" if passed else "✗ FAIL"

        print(f"  Expected: {expected}")
        print(f"  Got: {action}")
        print(f"  Rationale: {rationale}")
        print(f"  Response time: {response_time:.0f}ms")
        print(f"  {status}")
        print()

        results.append({
            "name": scenario["name"],
            "expected": expected,
            "actual": action,
            "passed": passed,
            "response_time_ms": response_time,
        })

    # Summary
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"Passed: {passed}/{total}")
    print(f"Total LLM time: {total_time:.0f}ms")
    print(f"Avg response time: {total_time/total:.0f}ms")

    # Check timing constraint
    avg_time = total_time / total
    if avg_time > 1500:
        print(f"\n⚠️  WARNING: Average response time {avg_time:.0f}ms exceeds 1500ms target")
    else:
        print(f"\n✓ Response times within target (<1500ms)")

    # List failures
    failures = [r for r in results if not r["passed"]]
    if failures:
        print("\nFailed tests:")
        for f in failures:
            print(f"  - {f['name']}: expected {f['expected']}, got {f['actual']}")

    return passed == total


async def test_timeout_handling():
    """Test that LLM timeout is handled gracefully."""
    print("\n" + "=" * 60)
    print("Testing timeout handling...")
    print("=" * 60)

    # Create a payload that will trigger a real call
    payload = build_decision_payload(
        job_id=99,
        status="running",
        elapsed_seconds=10.0,
        is_stuck=False,
        tool_activities=[MockToolActivity("test", "completed", 100)],
        monitoring_checks=2,
        last_tool_output="test output",
    )

    # Test with very short timeout (should timeout)
    action, rationale, elapsed = await call_llm_decider(payload, timeout_seconds=0.001)
    print(f"  Ultra-short timeout (0.001s): action={action}, time={elapsed:.0f}ms")
    print(f"  Rationale: {rationale}")

    if action == "wait" and "timeout" in rationale.lower():
        print("  ✓ Timeout handled correctly - defaulted to wait")
    else:
        print("  ✗ Timeout may not have been handled correctly")


if __name__ == "__main__":
    # Check for API key
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY environment variable not set")
        print("Set it and run again: export OPENAI_API_KEY='your-key'")
        sys.exit(1)

    async def main():
        all_passed = await run_tests()
        await test_timeout_handling()
        return all_passed

    success = asyncio.run(main())
    sys.exit(0 if success else 1)
