#!/usr/bin/env python3
"""
Session Resume Profiler - Detailed timing and behavior analysis.

Captures metrics to understand:
- Time to first token
- Event distribution
- Resume overhead vs fresh session
- Session file growth
"""

import asyncio
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# ============================================================================
# Configuration
# ============================================================================

CLAUDE_CONFIG_DIR = Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
LAB_WORKSPACE = Path(__file__).parent / "workspace"


def encode_cwd(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def get_session_dir(workspace: Path) -> Path:
    encoded = encode_cwd(str(workspace.absolute()))
    return CLAUDE_CONFIG_DIR / "projects" / encoded


# ============================================================================
# Profiling Data
# ============================================================================

@dataclass
class RunProfile:
    """Profile data for a single Claude Code run."""
    prompt: str
    resume_id: str | None
    start_time: float = 0
    end_time: float = 0
    first_token_time: float | None = None
    events: list[dict] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)
    exit_code: int | None = None

    @property
    def total_time_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000

    @property
    def time_to_first_token_ms(self) -> float | None:
        if self.first_token_time:
            return (self.first_token_time - self.start_time) * 1000
        return None

    def summary(self) -> dict:
        return {
            "prompt_len": len(self.prompt),
            "is_resume": self.resume_id is not None,
            "total_ms": round(self.total_time_ms, 1),
            "ttft_ms": round(self.time_to_first_token_ms, 1) if self.time_to_first_token_ms else None,
            "event_count": len(self.events),
            "event_types": dict(self.event_counts),
            "exit_code": self.exit_code,
        }


@dataclass
class SessionProfile:
    """Profile of a session file."""
    session_id: str
    path: Path
    size_bytes: int
    line_count: int
    modified: datetime
    event_types: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "session_id": self.session_id[:30],
            "size_kb": round(self.size_bytes / 1024, 1),
            "lines": self.line_count,
            "event_types": dict(self.event_types),
        }


# ============================================================================
# Profiler
# ============================================================================

class SessionProfiler:
    """Profiles Claude Code session behavior."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.runs: list[RunProfile] = []
        self.session_profiles: list[SessionProfile] = []

    async def run_claude(
        self,
        prompt: str,
        resume_id: str | None = None,
    ) -> RunProfile:
        """Run Claude Code with profiling."""
        profile = RunProfile(prompt=prompt, resume_id=resume_id)

        cmd = ["claude"]
        if resume_id:
            cmd.extend(["--resume", resume_id])
        cmd.extend(["-p", prompt, "--output-format", "stream-json", "--verbose", "--print"])

        profile.start_time = time.time()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
        )

        async for line in proc.stdout:
            line = line.decode().strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                event_type = event.get("type", "unknown")

                # Track first assistant token
                if event_type == "assistant" and profile.first_token_time is None:
                    profile.first_token_time = time.time()

                profile.events.append(event)
                profile.event_counts[event_type] = profile.event_counts.get(event_type, 0) + 1

            except json.JSONDecodeError:
                pass

        await proc.wait()
        profile.end_time = time.time()
        profile.exit_code = proc.returncode

        self.runs.append(profile)
        return profile

    def profile_sessions(self) -> list[SessionProfile]:
        """Profile all session files."""
        session_dir = get_session_dir(self.workspace)
        if not session_dir.exists():
            return []

        profiles = []
        for f in session_dir.glob("*.jsonl"):
            content = f.read_text()
            lines = content.strip().split("\n")

            event_types: dict[str, int] = {}
            for line in lines:
                try:
                    event = json.loads(line)
                    et = event.get("type", "unknown")
                    event_types[et] = event_types.get(et, 0) + 1
                except json.JSONDecodeError:
                    pass

            profile = SessionProfile(
                session_id=f.stem,
                path=f,
                size_bytes=f.stat().st_size,
                line_count=len(lines),
                modified=datetime.fromtimestamp(f.stat().st_mtime),
                event_types=event_types,
            )
            profiles.append(profile)

        self.session_profiles = profiles
        return profiles

    def get_latest_session_id(self) -> str | None:
        """Get the most recent session ID."""
        profiles = self.profile_sessions()
        if profiles:
            profiles.sort(key=lambda p: p.modified, reverse=True)
            return profiles[0].session_id
        return None


# ============================================================================
# Benchmark Suite
# ============================================================================

async def run_benchmark(workspace: Path) -> dict[str, Any]:
    """
    Run a comprehensive benchmark suite.

    Tests:
    1. Fresh session creation
    2. Session resume
    3. Multi-turn conversation
    4. Resume overhead comparison
    """
    profiler = SessionProfiler(workspace)
    results: dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "workspace": str(workspace),
        "tests": {},
    }

    print("=" * 60)
    print("SESSION RESUME BENCHMARK")
    print("=" * 60)

    # Test 1: Fresh session
    print("\n[1/4] Fresh session creation...")
    fresh_profile = await profiler.run_claude(
        "Say 'benchmark test' and nothing else.",
    )
    results["tests"]["fresh_session"] = fresh_profile.summary()
    print(f"  Total: {fresh_profile.total_time_ms:.0f}ms, TTFT: {fresh_profile.time_to_first_token_ms:.0f}ms")

    session_id = profiler.get_latest_session_id()

    # Test 2: Resume same session
    if session_id:
        print("\n[2/4] Resume session...")
        resume_profile = await profiler.run_claude(
            "What did you just say?",
            resume_id=session_id,
        )
        results["tests"]["resume_session"] = resume_profile.summary()
        print(f"  Total: {resume_profile.total_time_ms:.0f}ms, TTFT: {resume_profile.time_to_first_token_ms:.0f}ms")

    # Test 3: Multi-turn (5 turns)
    print("\n[3/4] Multi-turn conversation (5 turns)...")
    turn_times = []
    turn_ttfts = []

    for i in range(5):
        turn_profile = await profiler.run_claude(
            f"Turn {i + 1}: Remember the number {i * 100}. Confirm briefly.",
            resume_id=session_id,
        )
        turn_times.append(turn_profile.total_time_ms)
        if turn_profile.time_to_first_token_ms:
            turn_ttfts.append(turn_profile.time_to_first_token_ms)

        session_id = profiler.get_latest_session_id()
        print(f"  Turn {i + 1}: {turn_profile.total_time_ms:.0f}ms")

    results["tests"]["multi_turn"] = {
        "turns": 5,
        "total_times_ms": turn_times,
        "avg_total_ms": statistics.mean(turn_times),
        "avg_ttft_ms": statistics.mean(turn_ttfts) if turn_ttfts else None,
    }

    # Test 4: Session file growth
    print("\n[4/4] Session file analysis...")
    session_profiles = profiler.profile_sessions()
    if session_profiles:
        latest = max(session_profiles, key=lambda p: p.modified)
        results["tests"]["session_file"] = latest.summary()
        print(f"  Session size: {latest.size_bytes / 1024:.1f} KB")
        print(f"  Event count: {latest.line_count}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    if "fresh_session" in results["tests"] and "resume_session" in results["tests"]:
        fresh_ttft = results["tests"]["fresh_session"].get("ttft_ms", 0) or 0
        resume_ttft = results["tests"]["resume_session"].get("ttft_ms", 0) or 0
        overhead = resume_ttft - fresh_ttft
        print(f"\nResume overhead (TTFT): {overhead:+.0f}ms")

    if "multi_turn" in results["tests"]:
        mt = results["tests"]["multi_turn"]
        print(f"Multi-turn avg: {mt['avg_total_ms']:.0f}ms per turn")

    # Write results
    results_file = workspace / "benchmark_results.json"
    results_file.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nFull results: {results_file}")

    return results


# ============================================================================
# Event Stream Inspector
# ============================================================================

async def inspect_event_stream(workspace: Path):
    """
    Detailed inspection of what events Claude Code emits.

    This helps understand the exact format for integration.
    """
    profiler = SessionProfiler(workspace)

    print("=" * 60)
    print("EVENT STREAM INSPECTOR")
    print("=" * 60)

    print("\nRunning Claude with detailed event capture...")

    profile = await profiler.run_claude(
        "List 3 things about Python. Be brief.",
    )

    print("\n--- Event Stream Analysis ---\n")

    # Group events by type
    by_type: dict[str, list[dict]] = {}
    for event in profile.events:
        et = event.get("type", "unknown")
        if et not in by_type:
            by_type[et] = []
        by_type[et].append(event)

    for event_type, events in sorted(by_type.items()):
        print(f"\n{event_type.upper()} ({len(events)} events)")
        print("-" * 40)

        # Show first event of each type with full structure
        first = events[0]
        # Remove large content for readability
        display = {}
        for k, v in first.items():
            if isinstance(v, str) and len(v) > 200:
                display[k] = v[:200] + "..."
            elif isinstance(v, dict):
                display[k] = {kk: "..." if isinstance(vv, (str, list)) and len(str(vv)) > 50 else vv
                             for kk, vv in v.items()}
            else:
                display[k] = v

        print(json.dumps(display, indent=2))

    print("\n--- Key Observations ---\n")
    print(f"Total events: {len(profile.events)}")
    print(f"Event types: {list(profile.event_counts.keys())}")
    print(f"Time to first token: {profile.time_to_first_token_ms:.0f}ms" if profile.time_to_first_token_ms else "No TTFT")


# ============================================================================
# Main
# ============================================================================

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Session Resume Profiler")
    parser.add_argument("--mode", choices=["benchmark", "events", "sessions"],
                       default="benchmark", help="Profiling mode")
    parser.add_argument("--workspace", default=str(LAB_WORKSPACE))
    args = parser.parse_args()

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    # Ensure workspace has a file
    (workspace / "README.md").write_text("# Profiler Workspace\n")

    if args.mode == "benchmark":
        await run_benchmark(workspace)
    elif args.mode == "events":
        await inspect_event_stream(workspace)
    elif args.mode == "sessions":
        profiler = SessionProfiler(workspace)
        profiles = profiler.profile_sessions()
        print(f"\nFound {len(profiles)} sessions:\n")
        for p in profiles:
            print(json.dumps(p.summary(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
