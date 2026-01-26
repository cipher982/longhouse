#!/usr/bin/env python3
"""Debug script: Capture and display EXACT LLM input during concierge resume.

This script helps postmortem why the LLM might spawn a second commis after resume.
It shows the raw messages that would be sent to OpenAI so we can diagnose:
- Is the tool result missing or malformed?
- Is the message history corrupted?
- Is there something confusing the LLM?

Usage:
    cd apps/zerg/backend

    # Option 1: Analyze a real run from the database
    uv run python scripts/debug_resume_llm_input.py --run-id 82

    # Option 2: Simulate the bug scenario with mock data
    uv run python scripts/debug_resume_llm_input.py --simulate

    # Option 3: Check production logs on zerg server
    ssh zerg "cat ~/data/llm_requests/2026-01-13T01-58*.json"
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add the backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ["TESTING"] = "1"


def format_message(msg, index: int) -> str:
    """Format a single message for display."""
    msg_type = type(msg).__name__
    role = getattr(msg, "type", getattr(msg, "role", "unknown"))
    content = getattr(msg, "content", "")
    tool_calls = getattr(msg, "tool_calls", None)
    tool_call_id = getattr(msg, "tool_call_id", None)
    name = getattr(msg, "name", None)

    lines = [f"[{index}] {msg_type} (role={role})"]

    if name:
        lines.append(f"    name: {name}")
    if tool_call_id:
        lines.append(f"    tool_call_id: {tool_call_id}")

    if isinstance(content, str):
        lines.append(f"    content_length: {len(content)} chars")
        # Show full content for tool results (critical for debugging)
        if role == "tool" or len(content) < 1000:
            lines.append(f"    content: {content}")
        else:
            lines.append(f"    content: {content[:500]}...")
    elif content:
        lines.append(f"    content: {content}")

    if tool_calls:
        lines.append(f"    tool_calls: {json.dumps(tool_calls, indent=6)}")

    return "\n".join(lines)


def dump_messages(messages: list, label: str) -> None:
    """Pretty print messages for debugging."""
    print(f"\n{'='*80}")
    print(f"LLM INPUT: {label}")
    print(f"{'='*80}")
    print(f"Total messages: {len(messages)}")
    print()

    for i, msg in enumerate(messages):
        print(format_message(msg, i))
        print()


async def simulate_bug_scenario():
    """Simulate the exact bug scenario from the logs."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    print("="*80)
    print("SIMULATING: The exact bug scenario from 2026-01-13 01:58")
    print("="*80)

    # This is what the LLM SHOULD see after commis completes:
    print("\n--- EXPECTED message history (after commis completes) ---")

    system_prompt = """You are Jarvis, a helpful AI assistant with access to various tools.

When a user asks for something, you can:
1. Answer directly if you know the answer
2. Use tools to help accomplish tasks
3. Spawn commis for complex tasks that need tool access

When you call spawn_commis, it will create a background commis to handle the task.
Once the commis completes, you'll receive the result and should synthesize it for the user.

IMPORTANT: When you see a tool result that says "Commis job N completed:", that means
the task is DONE. You should summarize the result for the user, NOT spawn another commis."""

    user_message = "check disk space on cube real quick"

    # The AI's first response - called spawn_commis
    first_ai_response = AIMessage(
        content="",
        tool_calls=[{
            "id": "call_abc123",
            "name": "spawn_commis",
            "args": {"task": "Check disk space on cube"}
        }]
    )

    # The tool result from spawn_commis AFTER commis completed
    # This is what interrupt() returns after Command(resume=commis_result)
    tool_result = ToolMessage(
        content="""Commis job 41 completed:

Cube is at 45% disk usage. Docker images and volumes are the largest consumers.

Details:
- /dev/sda1: 45% used (234GB / 512GB)
- Largest directories:
  - /var/lib/docker: 156GB
  - /home: 42GB
  - /var/log: 8GB""",
        tool_call_id="call_abc123",
        name="spawn_commis"
    )

    # This is what the LLM should see on resume
    messages_on_resume = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
        first_ai_response,
        tool_result,
    ]

    dump_messages(messages_on_resume, "What LLM SHOULD see after resume")

    print("\n" + "="*80)
    print("ANALYSIS: What might cause the LLM to spawn again?")
    print("="*80)
    print("""
Possible causes for the double-spawn bug:

1. CHECKPOINT ISSUE: The LangGraph checkpoint might not include the ToolMessage
   properly. On resume, the LLM might only see [system, human, ai] without the
   tool result.

2. MESSAGE FORMAT: The tool_call_id might not match between the AIMessage's
   tool_calls[0].id and the ToolMessage's tool_call_id. OpenAI requires these
   to match exactly.

3. TOOL NAME: The ToolMessage might be missing the 'name' field, which OpenAI
   requires for function calling.

4. REPLAY BEHAVIOR: LangGraph might be replaying the tool calls from checkpoint
   before continuing, causing spawn_commis to be called twice.

5. PROMPT CONFUSION: The LLM might see "check disk space on cube" in the user
   message but "Check disk space on cube" (capitalized) in the spawn_commis args,
   and decide the original request wasn't fully addressed.

TO DIAGNOSE:
1. Check the actual LLM logs on production: ssh zerg "ls ~/data/llm_requests/"
2. Look for logs around 2026-01-13T01:58
3. Compare what the LLM actually saw vs what it should have seen
""")


async def analyze_run(course_id: int):
    """Analyze a specific run from the database."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from zerg.config import settings
    from zerg.models.models import Course, CommisJob
    from zerg.services.thread_service import ThreadService

    engine = create_engine(settings.database_url)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    try:
        run = db.query(Course).filter(Course.id == course_id).first()
        if not run:
            print(f"Run {course_id} not found")
            return

        print(f"\n{'='*80}")
        print(f"ANALYZING: Run {course_id}")
        print(f"{'='*80}")
        print(f"Status: {run.status}")
        print(f"Thread ID: {run.thread_id}")
        print(f"Started: {run.started_at}")
        print(f"Finished: {run.finished_at}")

        # Get commis for this run
        commis = db.query(CommisJob).filter(
            CommisJob.concierge_course_id == course_id
        ).order_by(CommisJob.created_at).all()

        print(f"\nCommis spawned: {len(commis)}")
        for w in commis:
            print(f"  - Job {w.id}: '{w.task}' ({w.status})")

        # Get thread messages
        thread_service = ThreadService()
        messages = thread_service.get_thread_messages_as_langchain(db, run.thread_id)

        dump_messages(messages, f"Messages in thread {run.thread_id}")

    finally:
        db.close()


async def check_llm_logs():
    """Check if there are LLM logs from the bug timeframe."""
    log_dir = Path("data/llm_requests")
    if not log_dir.exists():
        print("No local LLM logs found (data/llm_requests/ doesn't exist)")
        print("\nTo check production logs:")
        print("  ssh zerg \"ls ~/data/llm_requests/2026-01-13T01-58* 2>/dev/null\"")
        return

    # Look for logs from Jan 13 around the bug time
    target_files = list(log_dir.glob("2026-01-13T01-58*.json"))
    if not target_files:
        target_files = list(log_dir.glob("2026-01-13*.json"))

    if not target_files:
        print("No logs found from 2026-01-13")
        print("\nMost recent logs:")
        all_logs = sorted(log_dir.glob("*.json"))[-10:]
        for f in all_logs:
            print(f"  {f.name}")
        return

    print(f"Found {len(target_files)} logs from the bug timeframe")
    for f in sorted(target_files):
        print(f"\n--- {f.name} ---")
        with open(f) as fp:
            data = json.load(fp)
            print(json.dumps(data, indent=2)[:2000])


async def main():
    parser = argparse.ArgumentParser(description="Debug LLM input during concierge resume")
    parser.add_argument("--run-id", type=int, help="Analyze a specific run from database")
    parser.add_argument("--simulate", action="store_true", help="Simulate the bug scenario")
    parser.add_argument("--check-logs", action="store_true", help="Check LLM request logs")

    args = parser.parse_args()

    if args.course_id:
        await analyze_run(args.course_id)
    elif args.check_logs:
        await check_llm_logs()
    else:
        # Default: simulate the bug scenario
        await simulate_bug_scenario()


if __name__ == "__main__":
    asyncio.run(main())
