"""Results store for eval test cases.

This module provides:
- JSON serialization of eval results
- Per-commis temp file merging (xdist-safe)
- Result file naming with variant + commit hash
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evals.runner import EvalMetrics


@dataclass
class AssertionResult:
    """Single assertion result."""

    type: str
    passed: bool
    message: str
    expected: str | int | None = None
    actual: str | int | None = None


@dataclass
class CaseResult:
    """Result for a single eval case."""

    id: str
    status: str  # 'passed' | 'failed' | 'skipped'
    latency_ms: int
    total_tokens: int
    commis_spawned: int
    assertions: list[AssertionResult]
    failure_reason: str | None = None


@dataclass
class EvalRunSummary:
    """Summary statistics for an eval run."""

    total: int
    passed: int
    failed: int
    skipped: int
    pass_rate: float
    avg_latency_ms: int
    total_tokens: int
    total_cost_usd: float


@dataclass
class EvalRunResult:
    """Complete eval run result."""

    course_id: str
    variant: str
    timestamp: str
    commit: str
    config: dict
    summary: EvalRunSummary
    cases: list[CaseResult]


def get_commit_hash() -> str:
    """Get current git commit (short hash).

    Returns:
        Short commit hash (7 chars) or 'unknown' if not in git repo
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def generate_course_id(variant: str = "baseline") -> str:
    """Generate a unique run ID.

    Format: eval-{timestamp}-{variant}-{commit}
    Example: eval-2025-12-30T14-03-22-baseline-7fd28ac

    Args:
        variant: Variant name (default: 'baseline')

    Returns:
        Unique run ID string
    """
    # Include time to avoid clobbering results when running multiple times per day
    # on the same commit/variant.
    date_str = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    commit_hash = get_commit_hash()
    return f"eval-{date_str}-{variant}-{commit_hash}"


def get_results_dir() -> Path:
    """Get the results directory path.

    Returns:
        Path to results directory
    """
    # Determine the base directory (relative to this file)
    evals_dir = Path(__file__).parent
    results_dir = evals_dir / "results"
    results_dir.mkdir(exist_ok=True)
    return results_dir


def get_temp_results_dir() -> Path:
    """Get the temp results directory for per-commis files.

    Returns:
        Path to temp results directory
    """
    temp_dir = get_results_dir() / ".tmp"
    temp_dir.mkdir(exist_ok=True)
    return temp_dir


def save_result_temp(commis_id: str, case_result: CaseResult) -> None:
    """Save a single case result to per-commis temp file.

    This is called during test execution (potentially in parallel via pytest-xdist).
    Each commis writes to its own temp file to avoid race conditions.

    Args:
        commis_id: Pytest commis ID (e.g., 'gw0', 'gw1', or 'master')
        case_result: Result to save
    """
    temp_dir = get_temp_results_dir()
    temp_file = temp_dir / f"{commis_id}.jsonl"

    # Append to commis's temp file (JSONL format - one JSON object per line)
    with open(temp_file, "a") as f:
        json.dump(asdict(case_result), f)
        f.write("\n")


def merge_results(variant: str, model: str | None = None, commit: str | None = None) -> str:
    """Merge per-commis temp files into final result JSON.

    This should be called ONCE after all tests complete (in pytest_sessionfinish).
    Only the master node should call this (not xdist commis).

    Args:
        variant: Variant name used for this run
        model: Model used (optional, for config)
        commit: Commit hash (optional, will auto-detect if not provided)

    Returns:
        Path to the merged result file
    """
    temp_dir = get_temp_results_dir()
    temp_files = list(temp_dir.glob("*.jsonl"))

    if not temp_files:
        raise ValueError("No temp result files found to merge")

    # Collect all case results from temp files
    all_cases: list[CaseResult] = []
    for temp_file in temp_files:
        with open(temp_file) as f:
            for line in f:
                if line.strip():
                    case_data = json.loads(line)
                    # Reconstruct nested dataclasses
                    assertions = [AssertionResult(**a) for a in case_data.get("assertions", [])]
                    case = CaseResult(
                        id=case_data["id"],
                        status=case_data["status"],
                        latency_ms=case_data["latency_ms"],
                        total_tokens=case_data["total_tokens"],
                        commis_spawned=case_data["commis_spawned"],
                        assertions=assertions,
                        failure_reason=case_data.get("failure_reason"),
                    )
                    all_cases.append(case)

    # Calculate summary statistics
    total = len(all_cases)
    passed = sum(1 for c in all_cases if c.status == "passed")
    failed = sum(1 for c in all_cases if c.status == "failed")
    skipped = sum(1 for c in all_cases if c.status == "skipped")
    pass_rate = passed / total if total > 0 else 0.0

    avg_latency = sum(c.latency_ms for c in all_cases) // total if total > 0 else 0
    total_tokens = sum(c.total_tokens for c in all_cases)

    # Estimate cost (rough approximation using GPT-4o-mini pricing)
    # Input: $0.15/1M tokens, Output: $0.60/1M tokens
    # Assume 50/50 split for simplicity
    total_cost_usd = (total_tokens / 1_000_000) * 0.375  # Average of input/output rates

    summary = EvalRunSummary(
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        pass_rate=pass_rate,
        avg_latency_ms=avg_latency,
        total_tokens=total_tokens,
        total_cost_usd=round(total_cost_usd, 4),
    )

    # Generate run ID and result object
    commit_hash = commit or get_commit_hash()
    course_id = generate_course_id(variant)
    timestamp = datetime.now(timezone.utc).isoformat()

    config = {"variant": variant}
    if model:
        config["model"] = model

    result = EvalRunResult(
        course_id=course_id,
        variant=variant,
        timestamp=timestamp,
        commit=commit_hash,
        config=config,
        summary=summary,
        cases=all_cases,
    )

    # Write to final result file
    results_dir = get_results_dir()
    result_file = results_dir / f"{course_id}.json"

    with open(result_file, "w") as f:
        json.dump(asdict(result), f, indent=2)

    return str(result_file)


def cleanup_temp_results() -> None:
    """Remove all temporary result files.

    Should be called after successful merge.
    """
    temp_dir = get_temp_results_dir()
    for temp_file in temp_dir.glob("*.jsonl"):
        temp_file.unlink()


def load_result(result_file: str) -> EvalRunResult:
    """Load a result file.

    Args:
        result_file: Path to result JSON file

    Returns:
        EvalRunResult object
    """
    with open(result_file) as f:
        data = json.load(f)

    # Reconstruct nested dataclasses
    summary = EvalRunSummary(**data["summary"])
    cases = []
    for case_data in data["cases"]:
        assertions = [AssertionResult(**a) for a in case_data.get("assertions", [])]
        case = CaseResult(
            id=case_data["id"],
            status=case_data["status"],
            latency_ms=case_data["latency_ms"],
            total_tokens=case_data["total_tokens"],
            commis_spawned=case_data["commis_spawned"],
            assertions=assertions,
            failure_reason=case_data.get("failure_reason"),
        )
        cases.append(case)

    return EvalRunResult(
        course_id=data["course_id"],
        variant=data["variant"],
        timestamp=data["timestamp"],
        commit=data["commit"],
        config=data["config"],
        summary=summary,
        cases=cases,
    )


def get_commis_id() -> str:
    """Get the current pytest-xdist commis ID.

    Returns:
        Commis ID (e.g., 'gw0', 'gw1', or 'master' if not using xdist)
    """
    return os.environ.get("PYTEST_XDIST_COMMIS", "master")
