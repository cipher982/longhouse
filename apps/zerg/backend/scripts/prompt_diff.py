#!/usr/bin/env python3
"""Compare prompts between supervisor and worker to identify inconsistencies.

This script highlights differences in guidance, rules, and constraints between
the supervisor and worker prompts that might cause coordination issues.

Usage:
    uv run scripts/prompt_diff.py               # Show side-by-side comparison
    uv run scripts/prompt_diff.py --issues      # Only show potential issues
    uv run scripts/prompt_diff.py --worker W123 # Compare with worker artifact
"""

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.database import get_db
from zerg.prompts.composer import build_supervisor_prompt, build_worker_prompt


def find_key_sections(prompt: str) -> dict[str, str]:
    """Extract key sections from a prompt for comparison."""
    sections = {}
    lines = prompt.split("\n")

    current_section = None
    current_content = []

    for line in lines:
        # Detect section headers (## heading)
        if line.startswith("## "):
            # Save previous section
            if current_section:
                sections[current_section] = "\n".join(current_content).strip()

            # Start new section
            current_section = line[3:].strip()
            current_content = []
        elif current_section:
            current_content.append(line)

    # Save last section
    if current_section:
        sections[current_section] = "\n".join(current_content).strip()

    return sections


def analyze_differences(supervisor_prompt: str, worker_prompt: str) -> list[str]:
    """Analyze prompts and identify potential issues."""
    issues = []

    sup_sections = find_key_sections(supervisor_prompt)
    work_sections = find_key_sections(worker_prompt)

    # Check for instruction conflicts
    if "When to Spawn Workers" in sup_sections:
        spawn_guidance = sup_sections["When to Spawn Workers"]
        if "infrastructure" in spawn_guidance.lower():
            issues.append(
                "⚠️  Supervisor has detailed 'When to Spawn Workers' section, "
                "but worker may not understand supervisor's delegation logic"
            )

    # Check if worker has execution guidance
    if "How to Execute" not in work_sections and "CRITICAL" not in worker_prompt:
        issues.append("⚠️  Worker prompt may lack clear execution guidance")

    # Check for contradictory instructions
    if "minimal" in supervisor_prompt.lower() and "minimal" not in worker_prompt.lower():
        issues.append("⚠️  Supervisor emphasizes 'minimal' but worker doesn't - may cause over-engineering")

    if "one command" in worker_prompt.lower() and "one command" not in supervisor_prompt.lower():
        issues.append("✓ Worker has 'one command' constraint, but supervisor should be aware when delegating")

    # Token efficiency
    sup_len = len(supervisor_prompt)
    work_len = len(worker_prompt)
    total_len = sup_len + work_len

    if total_len > 50000:
        issues.append(f"⚠️  Combined prompt length: {total_len:,} chars (~{total_len // 4:,} tokens) - very long")

    # Check for redundant user context
    sup_has_user_context = "User Context" in sup_sections
    work_has_user_context = "Additional Context" in work_sections or "User Context" in work_sections

    if sup_has_user_context and work_has_user_context:
        issues.append("⚠️  Both supervisor and worker have user context sections - potential duplication")

    return issues


def show_side_by_side(supervisor_prompt: str, worker_prompt: str) -> None:
    """Show side-by-side comparison of prompts."""
    sup_sections = find_key_sections(supervisor_prompt)
    work_sections = find_key_sections(worker_prompt)

    all_sections = set(sup_sections.keys()) | set(work_sections.keys())

    print("\n" + "=" * 100)
    print("SECTION COMPARISON (Supervisor vs Worker)")
    print("=" * 100)

    for section in sorted(all_sections):
        in_sup = section in sup_sections
        in_work = section in work_sections

        status = ""
        if in_sup and in_work:
            status = "BOTH"
        elif in_sup:
            status = "SUP ONLY"
        elif in_work:
            status = "WORKER ONLY"

        print(f"\n## {section} [{status}]")

        if in_sup:
            sup_preview = sup_sections[section][:200].replace("\n", " ")
            print(f"  Supervisor: {sup_preview}...")

        if in_work:
            work_preview = work_sections[section][:200].replace("\n", " ")
            print(f"  Worker: {work_preview}...")


def show_unified_diff(supervisor_prompt: str, worker_prompt: str) -> None:
    """Show unified diff of the two prompts."""
    sup_lines = supervisor_prompt.splitlines(keepends=True)
    work_lines = worker_prompt.splitlines(keepends=True)

    diff = difflib.unified_diff(sup_lines, work_lines, fromfile="supervisor", tofile="worker", lineterm="")

    print("\n" + "=" * 100)
    print("UNIFIED DIFF (Supervisor → Worker)")
    print("=" * 100)
    print()

    for line in diff:
        print(line.rstrip())


def main():
    parser = argparse.ArgumentParser(
        description="Compare supervisor and worker prompts for inconsistencies",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Show issues only
  uv run scripts/prompt_diff.py --issues

  # Show side-by-side section comparison
  uv run scripts/prompt_diff.py --sections

  # Show full unified diff
  uv run scripts/prompt_diff.py --diff

  # Compare with specific worker artifact
  uv run scripts/prompt_diff.py --worker data/workers/W123
        """,
    )
    parser.add_argument(
        "--issues",
        action="store_true",
        help="Only show potential issues (default if no other flag)",
    )
    parser.add_argument(
        "--sections",
        action="store_true",
        help="Show side-by-side section comparison",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Show unified diff",
    )
    parser.add_argument(
        "--worker",
        type=str,
        help="Compare with worker artifact thread.jsonl (path to worker dir)",
    )

    args = parser.parse_args()

    # If no flags specified, default to issues
    if not any([args.issues, args.sections, args.diff]):
        args.issues = True

    # Get prompts
    db = next(get_db())
    try:
        from zerg.models.models import User

        user = db.query(User).first()
        if not user:
            print("ERROR: No users found. Create a user first.", file=sys.stderr)
            sys.exit(1)

        supervisor_prompt = build_supervisor_prompt(user)
        worker_prompt = build_worker_prompt(user)

        # If worker artifact specified, read from file
        if args.worker:
            worker_dir = Path(args.worker)
            thread_file = worker_dir / "thread.jsonl"

            if not thread_file.exists():
                print(f"ERROR: Worker artifact not found: {thread_file}", file=sys.stderr)
                sys.exit(1)

            # Extract system message from thread.jsonl
            import json

            with open(thread_file) as f:
                for line in f:
                    msg = json.loads(line)
                    if msg.get("role") == "system":
                        worker_prompt = msg.get("content", "")
                        break

        # Show requested views
        if args.issues:
            print("\n" + "=" * 100)
            print("PROMPT ANALYSIS - POTENTIAL ISSUES")
            print("=" * 100)

            issues = analyze_differences(supervisor_prompt, worker_prompt)

            if issues:
                for issue in issues:
                    print(f"\n{issue}")
            else:
                print("\n✓ No obvious issues detected")

            # Metrics
            print("\n" + "=" * 100)
            print("METRICS")
            print("=" * 100)
            print(f"\nSupervisor prompt: {len(supervisor_prompt):,} chars (~{len(supervisor_prompt) // 4:,} tokens)")
            print(f"Worker prompt: {len(worker_prompt):,} chars (~{len(worker_prompt) // 4:,} tokens)")
            print(
                f"Combined: {len(supervisor_prompt) + len(worker_prompt):,} chars (~{(len(supervisor_prompt) + len(worker_prompt)) // 4:,} tokens)"
            )

        if args.sections:
            show_side_by_side(supervisor_prompt, worker_prompt)

        if args.diff:
            show_unified_diff(supervisor_prompt, worker_prompt)

    finally:
        db.close()


if __name__ == "__main__":
    main()
