#!/usr/bin/env python3
"""Evaluate prompt quality and performance through automated test cases.

This script defines test scenarios and measures how prompts perform:
- Tool call efficiency (fewer is better)
- Token budget usage (lower is better)
- Task completion accuracy
- Anti-pattern detection

Usage:
    uv run scripts/eval_prompts.py                    # Run all test cases
    uv run scripts/eval_prompts.py --case simple_disk # Run specific test
    uv run scripts/eval_prompts.py --baseline        # Compare against baseline
    uv run scripts/eval_prompts.py --report          # Generate detailed report
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent directory for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class TestCase:
    """A test case for evaluating prompts."""

    id: str
    description: str
    user_query: str
    expected_behavior: dict[str, Any]
    category: str  # "simple", "complex", "multi_step"


# Test case definitions
TEST_CASES = [
    TestCase(
        id="simple_disk",
        description="Simple disk space check - should be ONE tool call",
        user_query="check disk space on cube",
        expected_behavior={
            "max_tool_calls": 1,
            "max_tokens": 10000,
            "should_spawn_commis": True,
            "worker_should_use_df": True,
            "keywords_in_result": ["disk", "%", "GB"],
        },
        category="simple",
    ),
    TestCase(
        id="simple_memory",
        description="Simple memory check - ONE tool call",
        user_query="check memory on clifford",
        expected_behavior={
            "max_tool_calls": 1,
            "max_tokens": 10000,
            "should_spawn_commis": True,
            "worker_should_use_free": True,
            "keywords_in_result": ["memory", "GB"],
        },
        category="simple",
    ),
    TestCase(
        id="simple_containers",
        description="List running containers - ONE tool call",
        user_query="list docker containers on cube",
        expected_behavior={
            "max_tool_calls": 1,
            "max_tokens": 10000,
            "should_spawn_commis": True,
            "keywords_in_result": ["container", "docker"],
        },
        category="simple",
    ),
    TestCase(
        id="complex_disk_analysis",
        description="Detailed disk analysis - multiple commands OK",
        user_query="analyze disk usage on cube, find what's using the most space, identify cleanup opportunities",
        expected_behavior={
            "max_tool_calls": 3,  # df + du + docker system df acceptable
            "max_tokens": 20000,
            "should_spawn_commis": True,
            "keywords_in_result": ["disk", "GB", "cleanup"],
        },
        category="complex",
    ),
    TestCase(
        id="multi_server_check",
        description="Check multiple servers in parallel",
        user_query="check disk space on cube, clifford, and zerg",
        expected_behavior={
            "max_tool_calls": 3,  # Should spawn 3 workers in parallel
            "max_tokens": 30000,
            "should_spawn_commis": True,
            "parallel_workers": 3,
            "keywords_in_result": ["cube", "clifford", "zerg"],
        },
        category="multi_step",
    ),
    TestCase(
        id="conversation_followup",
        description="Follow-up question - should NOT spawn new worker",
        user_query="what did you find?",
        expected_behavior={
            "max_tool_calls": 1,  # Should use read_commis_result
            "max_tokens": 5000,
            "should_spawn_commis": False,
            "should_query_past_workers": True,
        },
        category="simple",
    ),
    TestCase(
        id="over_specification_trap",
        description="Worker task should be concise, not over-specified",
        user_query="check disk space on cube",
        expected_behavior={
            "max_task_length": 100,  # Supervisor's task to worker should be brief
            "task_should_not_contain": ["run df", "execute", "sudo du"],  # Don't tell worker HOW
            "task_should_contain": ["disk", "cube"],  # Just WHAT
        },
        category="anti_pattern",
    ),
]


def load_logged_session(log_dir: Path, session_query: str) -> list[dict]:
    """Load logs from a specific session matching a query.

    Args:
        log_dir: Directory containing LLM request logs
        session_query: Part of the query to match (e.g., "check disk")

    Returns:
        List of log entries for that session
    """
    logs = []
    for file_path in sorted(log_dir.glob("*.json")):
        try:
            with open(file_path) as f:
                data = json.load(f)

            # Check if this log matches the query
            messages = data.get("messages", [])
            for msg in messages:
                content = msg.get("content", "").lower()
                if session_query.lower() in content:
                    logs.append(data)
                    break
        except Exception as e:
            print(f"Warning: Failed to parse {file_path.name}: {e}", file=sys.stderr)

    return logs


def analyze_session_logs(logs: list[dict], test_case: TestCase) -> dict[str, Any]:
    """Analyze session logs against test case expectations.

    Returns:
        Score dictionary with pass/fail for each criterion
    """
    results = {
        "test_id": test_case.id,
        "total_logs": len(logs),
        "passed": [],
        "failed": [],
        "metrics": {},
    }

    if not logs:
        results["failed"].append("No logs found for this test case")
        return results

    # Count tool iterations
    tool_iterations = sum(
        1 for log in logs if "tool_iteration" in log.get("phase", "")
    )
    results["metrics"]["tool_iterations"] = tool_iterations

    # Count total tokens
    total_tokens = 0
    for log in logs:
        if log.get("type") == "response":
            usage = log.get("response", {}).get("usage_metadata", {})
            total_tokens += usage.get("total_tokens", 0)
    results["metrics"]["total_tokens"] = total_tokens

    # Check against expectations
    expected = test_case.expected_behavior

    if "max_tool_calls" in expected:
        if tool_iterations <= expected["max_tool_calls"]:
            results["passed"].append(
                f"Tool calls: {tool_iterations} <= {expected['max_tool_calls']}"
            )
        else:
            results["failed"].append(
                f"Tool calls: {tool_iterations} > {expected['max_tool_calls']} (too many)"
            )

    if "max_tokens" in expected:
        if total_tokens <= expected["max_tokens"]:
            results["passed"].append(
                f"Tokens: {total_tokens:,} <= {expected['max_tokens']:,}"
            )
        else:
            results["failed"].append(
                f"Tokens: {total_tokens:,} > {expected['max_tokens']:,} (inefficient)"
            )

    # Check if worker was spawned
    if "should_spawn_commis" in expected:
        worker_spawned = any(log.get("worker_id") for log in logs)
        if worker_spawned == expected["should_spawn_commis"]:
            results["passed"].append(
                f"Worker spawned: {worker_spawned} (expected: {expected['should_spawn_commis']})"
            )
        else:
            results["failed"].append(
                f"Worker spawned: {worker_spawned} (expected: {expected['should_spawn_commis']})"
            )

    # Check for over-specification in supervisor's task delegation
    if "max_task_length" in expected:
        for log in logs:
            if log.get("phase") == "initial" and log.get("worker_id"):
                # This is supervisor delegating to worker
                messages = log.get("messages", [])
                for msg in messages:
                    if msg.get("role") == "human":
                        task = msg.get("content", "")
                        task_len = len(task)
                        if task_len <= expected["max_task_length"]:
                            results["passed"].append(
                                f"Task concise: {task_len} chars <= {expected['max_task_length']}"
                            )
                        else:
                            results["failed"].append(
                                f"Task too long: {task_len} chars > {expected['max_task_length']} (over-specified)"
                            )

                        # Check for anti-patterns
                        if "task_should_not_contain" in expected:
                            for forbidden in expected["task_should_not_contain"]:
                                if forbidden.lower() in task.lower():
                                    results["failed"].append(
                                        f"Task over-specified: contains '{forbidden}'"
                                    )

    return results


def detect_anti_patterns(logs: list[dict]) -> list[str]:
    """Detect common anti-patterns in logged sessions.

    Returns:
        List of anti-pattern descriptions
    """
    patterns = []

    # Pattern 1: Over-specification in task delegation
    for log in logs:
        if log.get("phase") == "initial" and log.get("worker_id"):
            messages = log.get("messages", [])
            for msg in messages:
                if msg.get("role") == "human":
                    task = msg.get("content", "")
                    # Check for command-level detail
                    if any(
                        cmd in task.lower()
                        for cmd in ["df -h", "du -", "docker system", "sudo"]
                    ):
                        patterns.append(
                            f"Over-specification: Supervisor told worker exact commands: {task[:100]}..."
                        )

    # Pattern 2: Excessive tool iterations for simple tasks
    simple_keywords = ["disk space", "memory", "list containers"]
    for log in logs:
        messages = log.get("messages", [])
        has_simple_request = any(
            any(kw in msg.get("content", "").lower() for kw in simple_keywords)
            for msg in messages
        )
        if has_simple_request:
            worker_id = log.get("worker_id")
            if worker_id:
                # Count tool iterations for this worker
                worker_logs = [l for l in logs if l.get("worker_id") == worker_id]
                tool_iters = sum(
                    1 for l in worker_logs if "tool_iteration" in l.get("phase", "")
                )
                if tool_iters > 2:
                    patterns.append(
                        f"Excessive iterations: Simple task took {tool_iters} tool calls"
                    )

    # Pattern 3: Supervisor doing worker tasks
    for log in logs:
        if not log.get("worker_id"):  # Supervisor
            messages = log.get("messages", [])
            for msg in messages:
                if msg.get("role") == "ai":
                    # Check for tool calls
                    tool_calls = msg.get("tool_calls", [])
                    for tc in tool_calls:
                        if tc.get("name") in ["ssh_exec", "runner_exec"]:
                            patterns.append(
                                "Supervisor used execution tool directly (should spawn worker)"
                            )

    # Pattern 4: Token bloat in system prompts
    for log in logs:
        messages = log.get("messages", [])
        for msg in messages:
            if msg.get("role") == "system":
                content_len = len(msg.get("content", ""))
                if content_len > 15000:  # ~3750 tokens
                    patterns.append(
                        f"Bloated system prompt: {content_len:,} chars (~{content_len // 4:,} tokens)"
                    )

    return patterns


def run_evaluation(log_dir: Path, test_cases: list[TestCase]) -> dict[str, Any]:
    """Run all test cases and generate report.

    Args:
        log_dir: Directory containing LLM logs
        test_cases: List of test cases to evaluate

    Returns:
        Evaluation report with scores and recommendations
    """
    report = {
        "timestamp": datetime.now().isoformat(),
        "test_results": [],
        "overall_score": 0,
        "anti_patterns": [],
        "recommendations": [],
    }

    for test_case in test_cases:
        print(f"\nRunning test: {test_case.id} ({test_case.category})")
        print(f"  Query: {test_case.user_query}")

        # Find logs matching this test
        logs = load_logged_session(log_dir, test_case.user_query)

        if not logs:
            print(f"  ⚠️  No logs found - test skipped")
            continue

        # Analyze logs
        result = analyze_session_logs(logs, test_case)

        # Detect anti-patterns
        patterns = detect_anti_patterns(logs)
        result["anti_patterns"] = patterns

        # Print results
        print(f"  Metrics: {result['metrics']}")
        print(f"  Passed: {len(result['passed'])}")
        print(f"  Failed: {len(result['failed'])}")

        if result["failed"]:
            print(f"  ❌ Failures:")
            for failure in result["failed"]:
                print(f"     - {failure}")

        if patterns:
            print(f"  ⚠️  Anti-patterns detected:")
            for pattern in patterns:
                print(f"     - {pattern}")

        report["test_results"].append(result)
        report["anti_patterns"].extend(patterns)

    # Calculate overall score
    total_tests = len(report["test_results"])
    passed_tests = sum(
        1 for r in report["test_results"] if len(r["failed"]) == 0
    )
    report["overall_score"] = (
        (passed_tests / total_tests * 100) if total_tests > 0 else 0
    )

    # Generate recommendations
    report["recommendations"] = generate_recommendations(report)

    return report


def generate_recommendations(report: dict[str, Any]) -> list[str]:
    """Generate actionable recommendations based on evaluation results."""
    recommendations = []

    # Analyze failures
    common_failures = defaultdict(int)
    for result in report["test_results"]:
        for failure in result["failed"]:
            if "Tool calls" in failure:
                common_failures["excessive_tool_calls"] += 1
            elif "Tokens" in failure:
                common_failures["token_inefficiency"] += 1
            elif "over-specified" in failure:
                common_failures["over_specification"] += 1

    # Generate specific recommendations
    if common_failures["excessive_tool_calls"] > 0:
        recommendations.append(
            f"[HIGH] Reduce tool iterations: {common_failures['excessive_tool_calls']} tests had excessive tool calls. "
            "Strengthen worker prompt's 'ONE command, then stop' guidance."
        )

    if common_failures["over_specification"] > 0:
        recommendations.append(
            f"[HIGH] Prevent over-specification: {common_failures['over_specification']} tests had detailed task instructions. "
            "Supervisor should pass user queries nearly verbatim to workers."
        )

    if common_failures["token_inefficiency"] > 0:
        recommendations.append(
            f"[MEDIUM] Optimize token usage: {common_failures['token_inefficiency']} tests exceeded token budgets. "
            "Review system prompt length and reduce redundant context."
        )

    # Anti-pattern recommendations
    pattern_types = defaultdict(int)
    for pattern in report["anti_patterns"]:
        if "Over-specification" in pattern:
            pattern_types["over_spec"] += 1
        elif "Excessive iterations" in pattern:
            pattern_types["iterations"] += 1
        elif "Bloated system prompt" in pattern:
            pattern_types["bloat"] += 1

    if pattern_types["bloat"] > 0:
        recommendations.append(
            "[MEDIUM] System prompts are bloated (>15K chars). "
            "Consider splitting into focused sections or using prompt caching."
        )

    return recommendations


def print_report(report: dict[str, Any]) -> None:
    """Pretty-print the evaluation report."""
    print("\n" + "=" * 100)
    print("PROMPT EVALUATION REPORT")
    print("=" * 100)
    print(f"\nTimestamp: {report['timestamp']}")
    print(f"Overall Score: {report['overall_score']:.1f}%")

    print("\n## Test Results")
    print(f"{'Test ID':<30} {'Category':<15} {'Passed':<10} {'Failed':<10}")
    print("-" * 70)
    for result in report["test_results"]:
        test_id = result.get("test_id", "unknown")[:28]
        # Find category from test cases
        category = next(
            (tc.category for tc in TEST_CASES if tc.id == result["test_id"]),
            "unknown",
        )
        passed = len(result["passed"])
        failed = len(result["failed"])
        print(f"{test_id:<30} {category:<15} {passed:<10} {failed:<10}")

    print("\n## Anti-Patterns Detected")
    if report["anti_patterns"]:
        unique_patterns = list(set(report["anti_patterns"]))
        for i, pattern in enumerate(unique_patterns, 1):
            print(f"\n{i}. {pattern}")
    else:
        print("\n✓ No anti-patterns detected")

    print("\n## Recommendations")
    if report["recommendations"]:
        for i, rec in enumerate(report["recommendations"], 1):
            print(f"\n{i}. {rec}")
    else:
        print("\n✓ No recommendations - prompts are performing well")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate prompt quality through test cases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run all test cases
  uv run scripts/eval_prompts.py

  # Run specific test
  uv run scripts/eval_prompts.py --case simple_disk

  # Generate detailed report
  uv run scripts/eval_prompts.py --report

  # Save report to file
  uv run scripts/eval_prompts.py --output report.json
        """,
    )
    parser.add_argument(
        "--case",
        type=str,
        help="Run specific test case by ID",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Generate detailed report",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Save report to JSON file",
    )
    parser.add_argument(
        "--category",
        type=str,
        choices=["simple", "complex", "multi_step", "anti_pattern"],
        help="Run only tests in this category",
    )

    args = parser.parse_args()

    # Find log directory
    log_dir = Path("data/llm_requests")
    if not log_dir.exists():
        print(f"ERROR: Log directory not found: {log_dir}", file=sys.stderr)
        print("Make sure LLM_REQUEST_LOG=1 is set to enable logging.", file=sys.stderr)
        sys.exit(1)

    # Filter test cases
    test_cases = TEST_CASES
    if args.case:
        test_cases = [tc for tc in TEST_CASES if tc.id == args.case]
        if not test_cases:
            print(f"ERROR: Test case not found: {args.case}", file=sys.stderr)
            sys.exit(1)
    elif args.category:
        test_cases = [tc for tc in TEST_CASES if tc.category == args.category]

    # Run evaluation
    report = run_evaluation(log_dir, test_cases)

    # Print report
    print_report(report)

    # Save to file if requested
    if args.output:
        output_path = Path(args.output)
        with open(output_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n✓ Report saved to: {output_path}")


if __name__ == "__main__":
    main()
