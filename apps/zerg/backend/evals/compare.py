"""Comparison CLI for eval results.

This module provides a CLI to compare two eval runs and show:
- Pass rate delta
- Latency regression/improvement
- Token usage difference
- Per-case status changes

Usage:
    python -m evals.compare <baseline_file> <variant_file>
    Or via Make: make eval-compare BASELINE=baseline VARIANT=improved
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from evals.results_store import load_result


def format_delta(value: int | float, is_percentage: bool = False) -> str:
    """Format a delta value with color coding.

    Args:
        value: The delta value
        is_percentage: Whether to format as percentage

    Returns:
        Colored string representation
    """
    if is_percentage:
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.1f}%"
    else:
        sign = "+" if value > 0 else ""
        return f"{sign}{value}"


def compare_results(baseline_file: str, variant_file: str) -> None:
    """Compare two eval result files and print delta report.

    Args:
        baseline_file: Path to baseline result JSON
        variant_file: Path to variant result JSON
    """
    # Load results
    baseline = load_result(baseline_file)
    variant = load_result(variant_file)

    print("\n" + "=" * 80)
    print(f"📊 Eval Comparison Report")
    print("=" * 80)
    print(f"\nBaseline: {baseline.course_id}")
    print(f"  Commit:  {baseline.commit}")
    print(f"  Date:    {baseline.timestamp}")
    print(f"\nVariant:  {variant.course_id}")
    print(f"  Commit:  {variant.commit}")
    print(f"  Date:    {variant.timestamp}")
    print()

    # Summary comparison
    print("=" * 80)
    print("📈 Summary Statistics")
    print("=" * 80)

    # Pass rate
    pass_rate_delta = (variant.summary.pass_rate - baseline.summary.pass_rate) * 100
    print(f"\nPass Rate:")
    print(f"  Baseline: {baseline.summary.pass_rate*100:.1f}% ({baseline.summary.passed}/{baseline.summary.total})")
    print(f"  Variant:  {variant.summary.pass_rate*100:.1f}% ({variant.summary.passed}/{variant.summary.total})")
    print(f"  Delta:    {format_delta(pass_rate_delta, is_percentage=True)}")

    # Latency
    latency_delta = variant.summary.avg_latency_ms - baseline.summary.avg_latency_ms
    latency_pct = (latency_delta / baseline.summary.avg_latency_ms * 100) if baseline.summary.avg_latency_ms > 0 else 0
    print(f"\nAvg Latency:")
    print(f"  Baseline: {baseline.summary.avg_latency_ms}ms")
    print(f"  Variant:  {variant.summary.avg_latency_ms}ms")
    print(f"  Delta:    {format_delta(latency_delta)}ms ({format_delta(latency_pct, is_percentage=True)})")

    # Token usage
    token_delta = variant.summary.total_tokens - baseline.summary.total_tokens
    token_pct = (token_delta / baseline.summary.total_tokens * 100) if baseline.summary.total_tokens > 0 else 0
    print(f"\nTotal Tokens:")
    print(f"  Baseline: {baseline.summary.total_tokens:,}")
    print(f"  Variant:  {variant.summary.total_tokens:,}")
    print(f"  Delta:    {format_delta(token_delta)} ({format_delta(token_pct, is_percentage=True)})")

    # Cost
    cost_delta = variant.summary.total_cost_usd - baseline.summary.total_cost_usd
    print(f"\nEstimated Cost:")
    print(f"  Baseline: ${baseline.summary.total_cost_usd:.4f}")
    print(f"  Variant:  ${variant.summary.total_cost_usd:.4f}")
    print(f"  Delta:    ${cost_delta:+.4f}")

    # Per-case comparison
    print("\n" + "=" * 80)
    print("📋 Per-Case Status Changes")
    print("=" * 80)

    # Create lookup for baseline cases
    baseline_cases = {c.id: c for c in baseline.cases}

    # Track changes
    regressions = []
    improvements = []
    unchanged_pass = []
    unchanged_fail = []

    for variant_case in variant.cases:
        baseline_case = baseline_cases.get(variant_case.id)
        if not baseline_case:
            print(f"\n⚠️  Case {variant_case.id} not found in baseline")
            continue

        baseline_passed = baseline_case.status == "passed"
        variant_passed = variant_case.status == "passed"

        if baseline_passed and not variant_passed:
            regressions.append((variant_case.id, baseline_case, variant_case))
        elif not baseline_passed and variant_passed:
            improvements.append((variant_case.id, baseline_case, variant_case))
        elif variant_passed:
            unchanged_pass.append(variant_case.id)
        else:
            unchanged_fail.append(variant_case.id)

    # Show regressions (most important)
    if regressions:
        print(f"\n🔴 Regressions ({len(regressions)}):")
        for case_id, baseline_case, variant_case in regressions:
            print(f"  - {case_id}")
            print(f"      Latency: {baseline_case.latency_ms}ms → {variant_case.latency_ms}ms")
            print(f"      Reason:  {variant_case.failure_reason}")
    else:
        print("\n✅ No regressions!")

    # Show improvements
    if improvements:
        print(f"\n🟢 Improvements ({len(improvements)}):")
        for case_id, baseline_case, variant_case in improvements:
            print(f"  - {case_id}")
            print(f"      Latency: {baseline_case.latency_ms}ms → {variant_case.latency_ms}ms")
    else:
        print("\n  No improvements (all baseline cases already passing)")

    # Summary counts
    print(f"\n📊 Status Summary:")
    print(f"  Regressions:     {len(regressions)}")
    print(f"  Improvements:    {len(improvements)}")
    print(f"  Unchanged (pass): {len(unchanged_pass)}")
    print(f"  Unchanged (fail): {len(unchanged_fail)}")

    # Recommendation
    print("\n" + "=" * 80)
    print("🎯 Recommendation")
    print("=" * 80)

    if regressions:
        print("\n❌ REJECT: Variant introduces regressions")
        print(f"   Fix {len(regressions)} failing case(s) before deploying")
        sys.exit(1)
    elif pass_rate_delta < -5:
        print("\n⚠️  WARNING: Pass rate decreased significantly")
        print("   Review failures before deploying")
        sys.exit(1)
    elif pass_rate_delta > 5:
        print("\n✅ APPROVE: Pass rate improved!")
        print("   Variant is better than baseline")
    elif latency_pct > 20:
        print("\n⚠️  WARNING: Latency increased significantly")
        print("   Consider performance optimization")
    elif latency_pct < -20:
        print("\n✅ APPROVE: Latency improved significantly!")
    elif token_pct > 20:
        print("\n⚠️  WARNING: Token usage increased significantly")
        print("   Consider cost implications")
    elif token_pct < -20:
        print("\n✅ APPROVE: Token usage decreased significantly!")
    else:
        print("\n✅ NEUTRAL: Variant performs similarly to baseline")
        print("   No major regressions detected")

    print("\n" + "=" * 80 + "\n")


def main():
    """Main entry point for comparison CLI."""
    parser = argparse.ArgumentParser(
        description="Compare two eval result files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m evals.compare baseline.json variant.json
  python -m evals.compare \\
    results/eval-2025-12-30-baseline-7fd28ac.json \\
    results/eval-2025-12-30-improved-7fd28ac.json
        """,
    )
    parser.add_argument("baseline", help="Path to baseline result JSON file")
    parser.add_argument("variant", help="Path to variant result JSON file")

    args = parser.parse_args()

    # Validate files exist
    baseline_path = Path(args.baseline)
    variant_path = Path(args.variant)

    if not baseline_path.exists():
        print(f"❌ Baseline file not found: {args.baseline}")
        sys.exit(1)

    if not variant_path.exists():
        print(f"❌ Variant file not found: {args.variant}")
        sys.exit(1)

    # Run comparison
    try:
        compare_results(str(baseline_path), str(variant_path))
    except Exception as e:
        print(f"❌ Comparison failed: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
