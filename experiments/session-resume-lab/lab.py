#!/usr/bin/env python3
"""
Session Resume Lab - Understand Claude Code session behavior.

This standalone script tests and visualizes how Claude Code sessions work,
helping validate the turn-by-turn resume approach before Zerg integration.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

# Rich for nice output (optional, falls back to plain)
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.live import Live
    from rich.text import Text
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False
    print("Note: Install 'rich' for prettier output: uv add rich")


# ============================================================================
# Configuration
# ============================================================================

# Where Claude stores sessions
CLAUDE_CONFIG_DIR = Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))

# Test workspace for our experiments
LAB_WORKSPACE = Path(__file__).parent / "workspace"

# Encoding function matching Claude Code's algorithm
def encode_cwd(path: str) -> str:
    """Encode path the way Claude Code does."""
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def get_session_dir(workspace: Path) -> Path:
    """Get the session directory for a workspace."""
    encoded = encode_cwd(str(workspace.absolute()))
    return CLAUDE_CONFIG_DIR / "projects" / encoded


# ============================================================================
# Session Inspector
# ============================================================================

def inspect_sessions(workspace: Path) -> list[dict]:
    """List all sessions for a workspace."""
    session_dir = get_session_dir(workspace)
    if not session_dir.exists():
        return []

    sessions = []
    for f in sorted(session_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        # Parse first and last line to get session info
        lines = f.read_text().strip().split("\n")
        first = json.loads(lines[0]) if lines else {}
        last = json.loads(lines[-1]) if lines else {}

        sessions.append({
            "id": f.stem,
            "path": f,
            "size_kb": f.stat().st_size / 1024,
            "lines": len(lines),
            "modified": datetime.fromtimestamp(f.stat().st_mtime),
            "first_type": first.get("type"),
            "last_type": last.get("type"),
        })

    return sessions


def inspect_session_content(session_path: Path, last_n: int = 20) -> list[dict]:
    """Get the last N events from a session."""
    lines = session_path.read_text().strip().split("\n")
    events = []
    for line in lines[-last_n:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"raw": line})
    return events


# ============================================================================
# Claude Code Runner
# ============================================================================

async def run_claude(
    prompt: str,
    workspace: Path,
    resume_id: str | None = None,
    stream: bool = True,
) -> AsyncIterator[dict]:
    """
    Run Claude Code and yield streaming events.

    This is the core function that shows exactly what happens during execution.
    """
    cmd = ["claude"]

    if resume_id:
        cmd.extend(["--resume", resume_id])

    cmd.extend(["-p", prompt])

    if stream:
        cmd.extend(["--output-format", "stream-json", "--verbose"])

    # Add --print to avoid TUI
    cmd.append("--print")

    print(f"\n[CMD] {' '.join(cmd)}")
    print(f"[CWD] {workspace}")
    print("-" * 60)

    start_time = time.time()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace,
    )

    event_count = 0

    async for line in proc.stdout:
        line = line.decode().strip()
        if not line:
            continue

        try:
            event = json.loads(line)
            event_count += 1
            event["_seq"] = event_count
            event["_elapsed_ms"] = int((time.time() - start_time) * 1000)
            yield event
        except json.JSONDecodeError:
            yield {"_raw": line, "_seq": event_count, "_elapsed_ms": int((time.time() - start_time) * 1000)}

    # Wait for completion
    await proc.wait()

    elapsed = time.time() - start_time
    yield {
        "_type": "lab_complete",
        "_exit_code": proc.returncode,
        "_total_events": event_count,
        "_elapsed_sec": round(elapsed, 2),
    }


# ============================================================================
# Test Scenarios
# ============================================================================

async def test_create_session(workspace: Path):
    """Test: Create a new session."""
    print("\n" + "=" * 60)
    print("TEST: Create New Session")
    print("=" * 60)

    prompt = "Say 'Hello from session lab!' and nothing else."

    events = []
    async for event in run_claude(prompt, workspace):
        events.append(event)
        print_event(event)

    # Find the session that was created
    sessions = inspect_sessions(workspace)
    if sessions:
        print(f"\n[RESULT] Session created: {sessions[0]['id']}")
        return sessions[0]["id"]
    else:
        print("\n[ERROR] No session found!")
        return None


async def test_resume_session(workspace: Path, session_id: str):
    """Test: Resume an existing session."""
    print("\n" + "=" * 60)
    print(f"TEST: Resume Session {session_id[:20]}...")
    print("=" * 60)

    prompt = "What was the first thing you said in this session? Quote it exactly."

    events = []
    async for event in run_claude(prompt, workspace, resume_id=session_id):
        events.append(event)
        print_event(event)

    return events


async def test_multi_turn_chat(workspace: Path):
    """Test: Simulate a multi-turn chat conversation."""
    print("\n" + "=" * 60)
    print("TEST: Multi-Turn Chat Simulation")
    print("=" * 60)

    messages = [
        "Remember this secret code: BLUE-FALCON-42. Just acknowledge you've stored it.",
        "What's the secret code I told you?",
        "Now change the code to RED-HAWK-99 and confirm.",
        "What's the current secret code?",
    ]

    session_id = None

    for i, msg in enumerate(messages):
        print(f"\n--- Turn {i + 1}/{len(messages)} ---")
        print(f"[USER] {msg}")
        print()

        async for event in run_claude(msg, workspace, resume_id=session_id):
            print_event(event)

            # Capture session ID from first turn
            if event.get("type") == "system" and event.get("session_id"):
                session_id = event["session_id"]

        # If we didn't get session_id from events, find it from files
        if not session_id:
            sessions = inspect_sessions(workspace)
            if sessions:
                session_id = sessions[0]["id"]

        print(f"\n[SESSION] Using: {session_id[:30] if session_id else 'None'}...")

        # Small delay between turns
        await asyncio.sleep(1)

    return session_id


async def test_inspect_all(workspace: Path):
    """Test: Inspect all sessions and their content."""
    print("\n" + "=" * 60)
    print("TEST: Inspect Sessions")
    print("=" * 60)

    sessions = inspect_sessions(workspace)

    if not sessions:
        print("\nNo sessions found for this workspace.")
        print(f"Workspace: {workspace}")
        print(f"Session dir: {get_session_dir(workspace)}")
        return

    print(f"\nFound {len(sessions)} session(s):\n")

    for i, s in enumerate(sessions):
        print(f"[{i + 1}] {s['id'][:40]}...")
        print(f"    Modified: {s['modified']}")
        print(f"    Size: {s['size_kb']:.1f} KB, Lines: {s['lines']}")
        print(f"    First event: {s['first_type']}, Last event: {s['last_type']}")
        print()

    # Show content of most recent
    if sessions:
        print("\n--- Last 10 events from most recent session ---\n")
        events = inspect_session_content(sessions[0]["path"], last_n=10)
        for e in events:
            print_event_compact(e)


# ============================================================================
# Output Helpers
# ============================================================================

def print_event(event: dict):
    """Print a single event with formatting."""
    if "_type" in event and event["_type"] == "lab_complete":
        print(f"\n[COMPLETE] Exit: {event['_exit_code']}, Events: {event['_total_events']}, Time: {event['_elapsed_sec']}s")
        return

    if "_raw" in event:
        print(f"[RAW] {event['_raw'][:100]}")
        return

    event_type = event.get("type", "unknown")
    elapsed = event.get("_elapsed_ms", 0)
    seq = event.get("_seq", 0)

    # Color coding by type
    prefix = f"[{seq:03d} +{elapsed:05d}ms]"

    if event_type == "assistant":
        # Assistant message - show content
        msg = event.get("message", {})
        content = msg.get("content", [])
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")[:200]
                print(f"{prefix} [ASSISTANT] {text}")
            elif block.get("type") == "tool_use":
                print(f"{prefix} [TOOL_CALL] {block.get('name')}")

    elif event_type == "user":
        msg = event.get("message", {})
        content = msg.get("content", [])
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")[:100]
                print(f"{prefix} [USER] {text}")

    elif event_type == "result":
        result = event.get("result", "")[:100]
        print(f"{prefix} [RESULT] {result}")

    elif event_type == "system":
        session_id = event.get("session_id", "")
        print(f"{prefix} [SYSTEM] session_id={session_id[:30]}...")

    else:
        # Generic event
        keys = [k for k in event.keys() if not k.startswith("_")]
        print(f"{prefix} [{event_type.upper()}] keys={keys}")


def print_event_compact(event: dict):
    """Print event in compact form for inspection."""
    event_type = event.get("type", "unknown")

    if event_type == "assistant":
        msg = event.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "")[:80]
                        print(f"  [A] {text}...")
                    elif block.get("type") == "tool_use":
                        print(f"  [A] tool: {block.get('name')}")
                elif isinstance(block, str):
                    print(f"  [A] {block[:80]}...")
        elif isinstance(content, str):
            print(f"  [A] {content[:80]}...")

    elif event_type == "user":
        msg = event.get("message", {})
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text = block.get("text", "")[:80]
                        print(f"  [U] {text}...")
                elif isinstance(block, str):
                    print(f"  [U] {block[:80]}...")
        elif isinstance(content, str):
            print(f"  [U] {content[:80]}...")

    elif event_type == "result":
        result = event.get("result", "")
        if isinstance(result, str):
            print(f"  [R] {result[:60]}...")
        else:
            print(f"  [R] {str(result)[:60]}...")

    else:
        print(f"  [{event_type}]")


# ============================================================================
# Interactive Mode
# ============================================================================

async def interactive_chat(workspace: Path):
    """Interactive chat mode - type messages, see responses."""
    print("\n" + "=" * 60)
    print("INTERACTIVE CHAT MODE")
    print("=" * 60)
    print("\nType messages to chat with Claude. Commands:")
    print("  /sessions  - List sessions")
    print("  /inspect   - Inspect current session")
    print("  /new       - Start fresh session")
    print("  /quit      - Exit")
    print()

    session_id = None

    # Check for existing sessions
    sessions = inspect_sessions(workspace)
    if sessions:
        print(f"Found {len(sessions)} existing session(s).")
        print(f"Most recent: {sessions[0]['id'][:40]}...")
        print("Using most recent session. Type /new to start fresh.\n")
        session_id = sessions[0]["id"]

    while True:
        try:
            user_input = input("\n[YOU] ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting...")
            break

        if not user_input:
            continue

        # Commands
        if user_input == "/quit":
            break
        elif user_input == "/sessions":
            await test_inspect_all(workspace)
            continue
        elif user_input == "/inspect":
            if session_id:
                session_dir = get_session_dir(workspace)
                session_path = session_dir / f"{session_id}.jsonl"
                if session_path.exists():
                    events = inspect_session_content(session_path, last_n=15)
                    print("\n--- Session Content ---")
                    for e in events:
                        print_event_compact(e)
            continue
        elif user_input == "/new":
            session_id = None
            print("Starting fresh session...")
            continue

        # Regular message
        print()
        async for event in run_claude(user_input, workspace, resume_id=session_id):
            print_event(event)

            # Capture session ID
            if event.get("type") == "system" and event.get("session_id"):
                session_id = event["session_id"]

        # Update session_id from files if not captured
        if not session_id:
            sessions = inspect_sessions(workspace)
            if sessions:
                session_id = sessions[0]["id"]


# ============================================================================
# Main
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(description="Session Resume Lab")
    parser.add_argument("--test", choices=["create", "resume", "chat", "inspect", "interactive"],
                       default="interactive", help="Test to run")
    parser.add_argument("--session", help="Session ID for resume test")
    parser.add_argument("--workspace", help="Workspace path", default=str(LAB_WORKSPACE))
    args = parser.parse_args()

    workspace = Path(args.workspace)

    # Ensure workspace exists
    workspace.mkdir(parents=True, exist_ok=True)

    # Create a simple file so Claude knows it's a real workspace
    readme = workspace / "README.md"
    if not readme.exists():
        readme.write_text("# Session Resume Lab Workspace\n\nTest workspace for session experiments.\n")

    print(f"Session Resume Lab")
    print(f"==================")
    print(f"Workspace: {workspace}")
    print(f"Claude Config: {CLAUDE_CONFIG_DIR}")
    print(f"Session Dir: {get_session_dir(workspace)}")

    if args.test == "create":
        await test_create_session(workspace)

    elif args.test == "resume":
        session_id = args.session
        if not session_id:
            sessions = inspect_sessions(workspace)
            if sessions:
                session_id = sessions[0]["id"]
            else:
                print("No sessions found. Run --test create first.")
                return
        await test_resume_session(workspace, session_id)

    elif args.test == "chat":
        await test_multi_turn_chat(workspace)

    elif args.test == "inspect":
        await test_inspect_all(workspace)

    elif args.test == "interactive":
        await interactive_chat(workspace)


if __name__ == "__main__":
    asyncio.run(main())
