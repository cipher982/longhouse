#!/usr/bin/env python3
"""
Measure coordination tool usage and prompt cache hit rates for agent sessions.

Analyzes transcripts from Claude (via ~/.claude/projects) and OMP (via ~/.omp/agent/sessions)
for the past N days, reporting:
  1. Coordination tool usage per provider (peers, inbox, tail, send, reply, search_sessions, recall, recall_context)
  2. Prompt cache hit rates (median, p10, and totals)

Usage:
  python3 scripts/ops/coordination_metrics.py [--days 7] [--json]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple
from collections import defaultdict
import statistics


COORDINATION_TOOLS = {
    "peers",
    "inbox",
    "tail",
    "send",
    "reply",
    "search_sessions",
    "recall",
    "recall_context",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7, help="Number of days to analyze")
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON instead of table"
    )
    return parser.parse_args()


def get_cutoff_timestamp(days: int) -> float:
    """Get the mtime cutoff for files (exclusive, older files are skipped)."""
    cutoff = datetime.now() - timedelta(days=days)
    return cutoff.timestamp()


def should_process_file(file_path: Path, cutoff: float) -> bool:
    """Check if file mtime is within the window."""
    try:
        mtime = file_path.stat().st_mtime
        return mtime >= cutoff
    except (OSError, ValueError):
        return False


def process_claude_file(file_path: Path) -> Tuple[List[str], Dict[str, Any], Dict[str, set]]:
    """
    Process a Claude transcript file.
    Returns (session_ids, {tool_name: count}, {tool_name: set(session_ids)})
    """
    session_ids = set()
    tool_counts = defaultdict(int)
    sessions_by_tool = defaultdict(set)

    try:
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Track session ID
                if "sessionId" in entry:
                    session_ids.add(entry["sessionId"])

                # Find tool calls in assistant messages
                if entry.get("type") == "assistant" and "message" in entry:
                    message = entry["message"]
                    if message.get("role") == "assistant" and "content" in message:
                        for content_item in message.get("content", []):
                            if content_item.get("type") == "tool_use":
                                tool_name = content_item.get("name", "")
                                # Strip mcp__ prefix if present
                                if tool_name.startswith("mcp__"):
                                    tool_name = tool_name[5:]
                                    # Handle variations like mcp__longhouse__peers
                                    if "__" in tool_name and tool_name.split("__")[0] in (
                                        "claude_ai",
                                        "context7",
                                        "life-hub-agents",
                                        "longhouse",
                                        "longhouse-channel",
                                    ):
                                        # Extract just the tool name part
                                        parts = tool_name.split("__")
                                        if len(parts) > 1:
                                            tool_name = parts[-1]

                                if tool_name in COORDINATION_TOOLS:
                                    tool_counts[tool_name] += 1
                                    sessions_by_tool[tool_name].add(
                                        entry.get("sessionId", "unknown")
                                    )
    except (IOError, OSError):
        pass

    return list(session_ids), dict(tool_counts), dict(sessions_by_tool)


def process_claude_cache_file(file_path: Path) -> Tuple[float, int, int, int]:
    """
    Calculate cache hit rate for a Claude session file.
    Returns (session_hit_rate, total_input, total_cache_read, total_cache_creation)
    """
    total_input = 0
    total_cache_creation = 0
    total_cache_read = 0

    try:
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if entry.get("type") == "assistant" and "message" in entry:
                    message = entry["message"]
                    usage = message.get("usage", {})
                    if usage:
                        total_input += usage.get("input_tokens", 0)
                        total_cache_creation += usage.get("cache_creation_input_tokens", 0)
                        total_cache_read += usage.get("cache_read_input_tokens", 0)
    except (IOError, OSError):
        pass

    total = total_input + total_cache_creation + total_cache_read
    if total > 0:
        hit_rate = total_cache_read / total
        return hit_rate, total_input, total_cache_read, total_cache_creation
    return 0.0, 0, 0, 0


def process_omp_file(file_path: Path) -> Tuple[List[str], Dict[str, Any], Dict[str, set]]:
    """
    Process an OMP session file.
    Returns (session_ids, {tool_name: count}, {tool_name: set(session_ids)})
    """
    session_ids = []
    tool_counts = defaultdict(int)
    sessions_by_tool = defaultdict(set)

    try:
        with open(file_path, "r") as f:
            # One file is one session. Longhouse-launched files are named
            # longhouse-<uuid>.jsonl; OMP's own are timestamped and nested.
            filename = file_path.stem
            session_id = filename.removeprefix("longhouse-")
            session_ids.append(session_id)

            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Find tool calls in assistant messages
                if entry.get("type") == "message":
                    message = entry.get("message", {})
                    if message.get("role") == "assistant" and "content" in message:
                        for content_item in message.get("content", []):
                            if content_item.get("type") == "toolCall":
                                tool_name = content_item.get("name", "")
                                if tool_name in COORDINATION_TOOLS:
                                    tool_counts[tool_name] += 1
                                    sessions_by_tool[tool_name].add(session_id)
    except (IOError, OSError):
        pass

    return session_ids, dict(tool_counts), dict(sessions_by_tool)


def process_omp_cache_file(file_path: Path) -> Tuple[float, int, int, int]:
    """
    Calculate cache hit rate for an OMP session file.
    Returns (session_hit_rate, total_input, total_cache_read, total_cache_write)
    """
    total_input = 0
    total_cache_write = 0
    total_cache_read = 0

    try:
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                if entry.get("type") == "message":
                    message = entry.get("message", {})
                    usage = message.get("usage", {})
                    if usage:
                        total_input += usage.get("input", 0)
                        total_cache_write += usage.get("cacheWrite", 0)
                        total_cache_read += usage.get("cacheRead", 0)
    except (IOError, OSError):
        pass

    total = total_input + total_cache_write + total_cache_read
    if total > 0:
        hit_rate = total_cache_read / total
        return hit_rate, total_input, total_cache_read, total_cache_write
    return 0.0, 0, 0, 0


def main():
    args = parse_args()
    cutoff = get_cutoff_timestamp(args.days)

    # Initialize results
    results = {
        "claude": {
            "sessions_total": set(),
            "tool_calls": defaultdict(int),
            "sessions_by_tool": defaultdict(set),
            "cache_rates": [],
        },
        "omp": {
            "sessions_total": set(),
            "tool_calls": defaultdict(int),
            "sessions_by_tool": defaultdict(set),
            "cache_rates": [],
        },
    }

    # Process Claude files
    claude_dir = Path.home() / ".claude" / "projects"
    if claude_dir.exists():
        for project_dir in claude_dir.iterdir():
            if project_dir.is_dir():
                for file_path in project_dir.glob("*.jsonl"):
                    if should_process_file(file_path, cutoff):
                        session_ids, tools, sessions_by_tool = process_claude_file(
                            file_path
                        )
                        results["claude"]["sessions_total"].update(session_ids)
                        for tool, count in tools.items():
                            results["claude"]["tool_calls"][tool] += count
                        for tool, sessions in sessions_by_tool.items():
                            results["claude"]["sessions_by_tool"][tool].update(sessions)

                        hit_rate, inp, cache_read, cache_creation = process_claude_cache_file(file_path)
                        if hit_rate > 0 or inp > 0:
                            results["claude"]["cache_rates"].append((hit_rate, inp, cache_read, cache_creation))

    # Process OMP files (nested in subdirectories)
    omp_dir = Path.home() / ".omp" / "agent" / "sessions"
    if omp_dir.exists():
        for file_path in omp_dir.glob("**/*.jsonl"):
            if should_process_file(file_path, cutoff):
                session_ids, tools, sessions_by_tool = process_omp_file(file_path)
                results["omp"]["sessions_total"].update(session_ids)
                for tool, count in tools.items():
                    results["omp"]["tool_calls"][tool] += count
                for tool, sessions in sessions_by_tool.items():
                    results["omp"]["sessions_by_tool"][tool].update(sessions)

                hit_rate, inp, cache_read, cache_write = process_omp_cache_file(file_path)
                if hit_rate > 0 or inp > 0:
                    results["omp"]["cache_rates"].append((hit_rate, inp, cache_read, cache_write))

    # Prepare output
    output = {
        "window_days": args.days,
        "providers": {},
    }

    for provider in ["claude", "omp"]:
        sessions_total = len(results[provider]["sessions_total"])
        tool_calls = results[provider]["tool_calls"]
        sessions_by_tool = results[provider]["sessions_by_tool"]
        cache_rates = results[provider]["cache_rates"]

        # Build coordination tools table
        coord_table = {
            "sessions_total": sessions_total,
            "tools": {},
        }

        for tool in sorted(COORDINATION_TOOLS):
            coord_table["tools"][tool] = {
                "total_calls": tool_calls.get(tool, 0),
                "sessions_using": len(sessions_by_tool.get(tool, set())),
            }

        # Calculate cache stats
        cache_stats = {}
        if cache_rates:
            # cache_rates is list of (hit_rate, input, cache_read, cache_creation/write)
            per_session_rates = [rate for rate, _, _, _ in cache_rates]
            sorted_rates = sorted(per_session_rates)
            median_rate = statistics.median(sorted_rates)
            p10_rate = sorted_rates[int(len(sorted_rates) * 0.1)]

            # Calculate token-weighted overall hit rate
            total_input = sum(inp for _, inp, _, _ in cache_rates)
            total_cache_read = sum(read for _, _, read, _ in cache_rates)
            total_cache_creation = sum(creation for _, _, _, creation in cache_rates)

            if provider == "claude":
                grand_total = total_input + total_cache_creation + total_cache_read
            else:  # omp
                grand_total = total_input + total_cache_creation + total_cache_read

            weighted_hit_rate = total_cache_read / grand_total if grand_total > 0 else 0.0

            cache_stats = {
                "sessions_with_usage": len(cache_rates),
                "median_hit_rate": round(median_rate, 4),
                "p10_hit_rate": round(p10_rate, 4),
                "median_hit_rate_pct": f"{median_rate * 100:.2f}%",
                "p10_hit_rate_pct": f"{p10_rate * 100:.2f}%",
                "weighted_hit_rate": round(weighted_hit_rate, 4),
                "weighted_hit_rate_pct": f"{weighted_hit_rate * 100:.2f}%",
                "total_tokens": grand_total,
            }

        output["providers"][provider] = {
            "coordination": coord_table,
            "cache": cache_stats,
        }

    if args.json:
        # Convert sets to lists for JSON serialization
        for provider_data in output["providers"].values():
            pass  # Already serializable now
        print(json.dumps(output, indent=2))
    else:
        # Pretty-print table format
        print(f"Coordination Metrics — Last {args.days} days\n")

        for provider in ["claude", "omp"]:
            provider_data = output["providers"][provider]
            coord = provider_data["coordination"]
            cache = provider_data["cache"]

            print(f"\n{provider.upper()}")
            print("=" * 60)
            print(f"Sessions in window: {coord['sessions_total']}")

            if coord["sessions_total"] > 0:
                print("\nCoordination Tool Usage:")
                print("-" * 60)
                print(
                    f"{'Tool':<20} {'Sessions':<12} {'Total Calls':<12}"
                )
                print("-" * 60)
                for tool in sorted(COORDINATION_TOOLS):
                    tool_data = coord["tools"][tool]
                    sessions = tool_data["sessions_using"]
                    calls = tool_data["total_calls"]
                    print(f"{tool:<20} {sessions:<12} {calls:<12}")

            if cache:
                print("\nPrompt Cache Hit Rates:")
                print("-" * 60)
                print(
                    f"Sessions with usage: {cache['sessions_with_usage']}"
                )
                print(f"Median hit rate: {cache['median_hit_rate_pct']}")
                print(f"P10 hit rate: {cache['p10_hit_rate_pct']}")
                print(f"Token-weighted hit rate: {cache['weighted_hit_rate_pct']}")
                print(f"Total tokens (input + cache): {cache['total_tokens']:,}")
            else:
                print("\nPrompt Cache: No usage data found")

        print("\n")


if __name__ == "__main__":
    main()
