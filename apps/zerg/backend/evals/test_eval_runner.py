"""Eval test runner - loads YAML datasets and executes test cases.

This module generates pytest test cases from YAML datasets.
Each test case:
1. Runs the supervisor with the input task
2. Captures metrics
3. Runs all assertions
4. Reports pass/fail

Run with: pytest apps/zerg/backend/evals/
Or via Make: make eval
"""

from __future__ import annotations

import asyncio

import pytest

from evals.asserters import run_assertion


def pytest_generate_tests(metafunc):
    """Generate test cases from YAML datasets.

    This hook dynamically creates test cases from all .yml files in datasets/
    """
    if "eval_case" in metafunc.fixturenames:
        # Load datasets directly (not via fixture, which isn't available during generation)
        from evals.conftest import load_eval_datasets

        datasets = load_eval_datasets()

        # Collect all test cases
        test_cases = []
        test_ids = []

        for dataset_name, dataset in datasets.items():
            for case in dataset.cases:
                test_cases.append((dataset_name, case))
                test_ids.append(f"{dataset_name}::{case.id}")

        metafunc.parametrize("eval_case", test_cases, ids=test_ids)


@pytest.mark.asyncio
async def test_eval_case(eval_case, eval_runner):
    """Execute a single eval case and run assertions.

    Args:
        eval_case: (dataset_name, EvalCase) tuple
        eval_runner: EvalRunner fixture
    """
    dataset_name, case = eval_case

    print(f"\n{'='*60}")
    print(f"Running: {case.id}")
    print(f"Category: {case.category}")
    if case.description:
        print(f"Description: {case.description}")
    print(f"Input: {case.input}")
    print(f"{'='*60}")

    # Run the case
    metrics = await eval_runner.run_case(
        task=case.input,
        timeout=case.timeout,
    )

    print(f"\nResults:")
    print(f"  Status: {metrics.status}")
    print(f"  Latency: {metrics.latency_ms}ms")
    print(f"  Tokens: {metrics.total_tokens}")
    print(f"  Workers: {metrics.workers_spawned}")
    if metrics.result_text:
        result_preview = metrics.result_text[:100] + "..." if len(metrics.result_text) > 100 else metrics.result_text
        print(f"  Result: {result_preview}")

    # Run assertions
    print(f"\nAssertions:")
    all_passed = True
    for assertion in case.assert_:
        # Extract parameters for assertion
        params = {}
        if assertion.value is not None:
            # Map 'value' to appropriate parameter name based on assertion type
            if assertion.type == "contains":
                params["value"] = assertion.value
            elif assertion.type == "status":
                params["expected"] = assertion.value
            elif assertion.type == "regex":
                params["pattern"] = assertion.value
            elif assertion.type == "tool_called":
                params["tool_name"] = assertion.value

        if assertion.max is not None:
            if assertion.type == "latency_ms":
                params["max_ms"] = assertion.max
            elif assertion.type == "total_tokens":
                params["max_tokens"] = assertion.max

        if assertion.min is not None:
            if assertion.type == "worker_spawned":
                params["min_count"] = assertion.min

        if assertion.count is not None:
            params["count"] = assertion.count

        if assertion.case_insensitive:
            params["case_insensitive"] = assertion.case_insensitive

        # Run assertion
        passed, message = run_assertion(metrics, assertion.type, **params)
        status_icon = "✓" if passed else "✗"
        print(f"  {status_icon} {assertion.type}: {message}")

        if not passed:
            all_passed = False

    print(f"{'='*60}\n")

    # Fail test if any assertion failed
    assert all_passed, f"Test case {case.id} failed one or more assertions"
