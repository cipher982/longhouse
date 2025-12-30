#!/usr/bin/env python3
"""Analyze LLM request logs to identify prompt issues and performance patterns.

This script reads the data/llm_requests/*.json files and provides insights:
- Timeline view of LLM calls during a conversation
- Token usage per phase
- Tool iteration counts (detecting excessive back-and-forth)
- Phase durations and anomalies

Usage:
    uv run scripts/analyze_llm_requests.py                # Analyze most recent session
    uv run scripts/analyze_llm_requests.py --last 3       # Last 3 sessions
    uv run scripts/analyze_llm_requests.py --all          # All sessions
    uv run scripts/analyze_llm_requests.py --worker W123  # Specific worker
    uv run scripts/analyze_llm_requests.py --verbose      # Show full messages
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_filename(filename: str) -> dict[str, Any]:
    """Parse LLM request log filename to extract metadata.

    Format: YYYY-MM-DDTHH-MM-SS_<phase>[_<worker_id>][_response].json
    """
    stem = Path(filename).stem
    parts = stem.split("_")

    timestamp_str = parts[0]
    phase = parts[1] if len(parts) > 1 else "unknown"
    worker_id = None
    is_response = False

    # Check for worker_id and response indicator
    for part in parts[2:]:
        if part == "response":
            is_response = True
        elif part.startswith("2025-"):
            # This is a worker_id with timestamp format
            worker_id = "_".join(parts[2:-1]) if is_response else "_".join(parts[2:])
            break

    # Convert timestamp format: YYYY-MM-DDTHH-MM-SS -> YYYY-MM-DDTHH:MM:SS
    # Replace only the time portion hyphens with colons
    if "T" in timestamp_str:
        date_part, time_part = timestamp_str.split("T")
        time_part = time_part.replace("-", ":")
        timestamp_str = f"{date_part}T{time_part}"

    return {
        "timestamp": datetime.fromisoformat(timestamp_str),
        "phase": phase,
        "worker_id": worker_id,
        "is_response": is_response,
        "filename": filename,
    }


def load_request_logs(log_dir: Path) -> list[dict]:
    """Load all LLM request logs from directory."""
    logs = []
    for file_path in sorted(log_dir.glob("*.json")):
        try:
            with open(file_path) as f:
                data = json.load(f)

            # Add parsed metadata
            meta = parse_filename(file_path.name)
            data["_meta"] = meta

            logs.append(data)
        except Exception as e:
            print(f"Warning: Failed to parse {file_path.name}: {e}", file=sys.stderr)

    return logs


def group_by_session(logs: list[dict]) -> dict[str, list[dict]]:
    """Group logs by session based on supervisor 'initial' phase timestamps.

    A session starts when a supervisor (worker_id=null) makes an 'initial' phase call.
    All subsequent logs until the next supervisor initial are part of that session.
    """
    # Sort logs by timestamp first
    sorted_logs = sorted(logs, key=lambda x: x["_meta"]["timestamp"])

    # Find all supervisor initial timestamps as session boundaries
    session_starts = []
    for log in sorted_logs:
        # Use JSON fields directly (more reliable than filename parsing)
        phase = log.get("phase") or log["_meta"]["phase"]
        worker_id = log.get("worker_id") or log["_meta"]["worker_id"]
        is_response = log.get("type") == "response" or log["_meta"]["is_response"]

        # Supervisor initial request marks a new session
        if phase == "initial" and worker_id is None and not is_response:
            session_starts.append(log["_meta"]["timestamp"])

    # If no supervisor initials found, fall back to first timestamp
    if not session_starts:
        session_starts = [sorted_logs[0]["_meta"]["timestamp"]] if sorted_logs else []

    # Assign each log to a session
    sessions = defaultdict(list)
    for log in sorted_logs:
        log_ts = log["_meta"]["timestamp"]

        # Find which session this log belongs to
        session_key = None
        for i, start_ts in enumerate(session_starts):
            next_ts = session_starts[i + 1] if i + 1 < len(session_starts) else None
            if log_ts >= start_ts and (next_ts is None or log_ts < next_ts):
                session_key = start_ts.strftime("%Y-%m-%d %H:%M:%S")
                break

        if session_key is None and session_starts:
            # Log is before first session, assign to first
            session_key = session_starts[0].strftime("%Y-%m-%d %H:%M:%S")

        if session_key:
            sessions[session_key].append(log)

    return sessions


def analyze_session(logs: list[dict], verbose: bool = False) -> None:
    """Analyze a single session's LLM calls."""
    if not logs:
        return

    # Sort by timestamp
    logs = sorted(logs, key=lambda x: x["_meta"]["timestamp"])

    # Session metadata
    first = logs[0]
    first_ts = first["_meta"]["timestamp"]
    last_ts = logs[-1]["_meta"]["timestamp"]
    duration = (last_ts - first_ts).total_seconds()

    print(f"\n{'=' * 80}")
    print(f"Session: {first_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration: {duration:.1f}s")
    print(f"Total LLM calls: {len(logs)}")
    print("=" * 80)

    # Group by phase and worker
    phase_counts = defaultdict(int)
    worker_calls = defaultdict(list)
    total_tokens = 0

    for log in logs:
        meta = log["_meta"]
        # Prefer JSON body fields over filename-parsed metadata
        phase = log.get("phase") or meta["phase"]
        worker_id = log.get("worker_id") or meta["worker_id"] or "supervisor"
        is_response = log.get("type") == "response" or meta["is_response"]

        phase_counts[phase] += 1

        # Track worker activity
        worker_calls[worker_id].append(log)

        # Token counting (only from responses)
        # Token data is in response.usage_metadata (not root-level usage)
        if is_response:
            response = log.get("response", {})
            usage = response.get("usage_metadata", {})
            if usage:
                total_tokens += usage.get("total_tokens", 0)

    # Phase breakdown
    print("\n## Phase Breakdown")
    print(f"{'Phase':<20} {'Count':<10}")
    print("-" * 30)
    for phase, count in sorted(phase_counts.items()):
        print(f"{phase:<20} {count:<10}")

    # Worker analysis
    print("\n## Worker Activity")
    for worker_id, calls in sorted(worker_calls.items()):
        requests = [c for c in calls if not c["_meta"]["is_response"]]
        responses = [c for c in calls if c["_meta"]["is_response"]]

        print(f"\n### {worker_id}")
        print(f"  Requests: {len(requests)}")
        print(f"  Responses: {len(responses)}")

        # Count tool iterations (prefer JSON body phase field)
        tool_iterations = sum(
            1 for c in requests
            if "tool_iteration" in (c.get("phase") or c["_meta"]["phase"])
        )
        if tool_iterations > 0:
            print(f"  Tool iterations: {tool_iterations}")
            if tool_iterations > 2:
                print(f"    ⚠️  HIGH: Worker made {tool_iterations} tool calls")

        # Token usage (from response.usage_metadata)
        worker_tokens = sum(
            r.get("response", {}).get("usage_metadata", {}).get("total_tokens", 0)
            for r in responses
            if r.get("response", {}).get("usage_metadata")
        )
        if worker_tokens > 0:
            print(f"  Tokens: {worker_tokens:,}")

    # Timeline view
    print("\n## Timeline")
    print(f"{'Time':<12} {'Phase':<20} {'Worker':<15} {'Type':<10} {'Tokens':<10}")
    print("-" * 80)

    for log in logs:
        meta = log["_meta"]
        ts = meta["timestamp"]
        # Prefer JSON body fields over filename-parsed metadata
        phase = (log.get("phase") or meta["phase"])[:18]
        worker_id = (log.get("worker_id") or meta["worker_id"] or "supervisor")[:13]
        is_response = log.get("type") == "response" or meta["is_response"]
        req_type = "response" if is_response else "request"

        tokens = ""
        if is_response:
            usage = log.get("response", {}).get("usage_metadata", {})
            if usage:
                tokens = f"{usage.get('total_tokens', 0):,}"

        time_str = ts.strftime("%H:%M:%S")
        print(f"{time_str:<12} {phase:<20} {worker_id:<15} {req_type:<10} {tokens:<10}")

    # Verbose mode: show message details
    if verbose:
        print("\n## Message Details")
        for i, log in enumerate(logs):
            meta = log["_meta"]
            print(f"\n### Call {i + 1}: {meta['phase']} ({meta['timestamp'].strftime('%H:%M:%S')})")

            if "messages" in log and not meta["is_response"]:
                print(f"Message count: {log.get('message_count', len(log['messages']))}")
                for msg in log["messages"]:
                    role = msg.get("role", "unknown")
                    content_len = msg.get("content_length", len(msg.get("content", "")))
                    print(f"  - {role}: {content_len:,} chars")

    # Summary and warnings
    print("\n## Summary")
    print(f"Total tokens: {total_tokens:,}")

    warnings = []
    if total_tokens > 100000:
        warnings.append(f"⚠️  Very high token usage: {total_tokens:,} tokens")

    for worker_id, calls in worker_calls.items():
        # Prefer JSON body phase field
        tool_iters = sum(
            1 for c in calls
            if "tool_iteration" in (c.get("phase") or c["_meta"]["phase"])
        )
        if tool_iters > 3:
            warnings.append(f"⚠️  {worker_id}: {tool_iters} tool iterations (expected ≤2 for simple tasks)")

    if warnings:
        print("\n### Warnings")
        for warning in warnings:
            print(f"  {warning}")
    else:
        print("  ✓ No anomalies detected")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze LLM request logs for debugging prompts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze most recent session
  uv run scripts/analyze_llm_requests.py

  # Show last 3 sessions
  uv run scripts/analyze_llm_requests.py --last 3

  # Analyze all sessions
  uv run scripts/analyze_llm_requests.py --all

  # Show full message details
  uv run scripts/analyze_llm_requests.py --verbose
        """,
    )
    parser.add_argument(
        "--last",
        type=int,
        default=1,
        help="Number of recent sessions to analyze (default: 1)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Analyze all sessions",
    )
    parser.add_argument(
        "--worker",
        type=str,
        help="Filter to specific worker ID",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show full message details",
    )

    args = parser.parse_args()

    # Find log directory
    log_dir = Path("data/llm_requests")
    if not log_dir.exists():
        print(f"ERROR: Log directory not found: {log_dir}", file=sys.stderr)
        print("Make sure LLM_REQUEST_LOG=1 is set to enable logging.", file=sys.stderr)
        sys.exit(1)

    # Load logs
    logs = load_request_logs(log_dir)
    if not logs:
        print("No logs found.", file=sys.stderr)
        sys.exit(1)

    # Filter by worker if specified
    if args.worker:
        logs = [log for log in logs if log.get("worker_id") == args.worker or log["_meta"]["worker_id"] == args.worker]
        if not logs:
            print(f"No logs found for worker: {args.worker}", file=sys.stderr)
            sys.exit(1)

    # Group by session
    sessions = group_by_session(logs)

    # Select sessions to analyze
    if args.all:
        selected_sessions = sorted(sessions.items())
    else:
        selected_sessions = sorted(sessions.items())[-args.last :]

    # Analyze each session
    for session_key, session_logs in selected_sessions:
        analyze_session(session_logs, verbose=args.verbose)


if __name__ == "__main__":
    main()
