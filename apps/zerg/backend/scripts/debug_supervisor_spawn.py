#!/usr/bin/env python3
"""Debug script to trace supervisor behavior for infrastructure requests.

Run with: cd apps/zerg/backend && uv run python scripts/debug_supervisor_spawn.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from zerg.database import default_session_factory
from zerg.crud import crud
from zerg.prompts.composer import build_supervisor_prompt
from zerg.services.supervisor_service import SupervisorService


def main():
    db = default_session_factory()

    # Get user and their context
    user = crud.get_user(db, 1)
    print("=" * 60)
    print("USER CONTEXT")
    print("=" * 60)
    print(f"User: {user.email}")
    print(f"Context keys: {list(user.context.keys()) if user.context else 'EMPTY'}")

    if user.context:
        servers = user.context.get("servers", [])
        print(f"Servers: {[s['name'] for s in servers]}")
    else:
        print("⚠️  NO USER CONTEXT - servers won't be in prompt!")

    # Check supervisor agent
    print("\n" + "=" * 60)
    print("SUPERVISOR AGENT")
    print("=" * 60)

    svc = SupervisorService(db)
    result = svc.get_or_create_supervisor_agent(user.id)
    # Handle both return types
    if isinstance(result, tuple):
        agent, thread = result
    else:
        agent = result
        thread = None

    print(f"Agent ID: {agent.id}")
    print(f"Agent name: {agent.name}")
    print(f"Allowed tools: {agent.allowed_tools}")

    # Check if spawn_commis is in tools
    if "spawn_commis" in (agent.allowed_tools or []):
        print("✅ spawn_commis is in allowed_tools")
    else:
        print("❌ spawn_commis NOT in allowed_tools!")

    # Check ACTUAL thread messages (runtime state, not just config)
    print("\n" + "=" * 60)
    print("THREAD MESSAGES (RUNTIME STATE)")
    print("=" * 60)

    from sqlalchemy import text
    thread_id = 1  # Supervisor thread
    messages = db.execute(text(
        f"SELECT id, role, LEFT(content, 80), sent_at FROM thread_messages WHERE thread_id = {thread_id} ORDER BY sent_at"
    )).fetchall()

    print(f"Thread {thread_id} has {len(messages)} messages")
    system_msgs = [m for m in messages if m[1] == 'system']
    print(f"System messages: {len(system_msgs)}")

    if len(system_msgs) == 0:
        print("❌ CRITICAL: Thread has NO system message - LLM is running blind!")
    elif messages[0][1] != 'system':
        print(f"❌ CRITICAL: First message is {messages[0][1]}, not system!")
        print(f"   System message is at position {[i for i, m in enumerate(messages) if m[1] == 'system'][0]}")
    else:
        print("✅ System message is first in thread")

    print("\nFirst 5 messages:")
    for i, (msg_id, role, content, sent_at) in enumerate(messages[:5]):
        print(f"  {i}. [{role}] {content[:60]}...")

    # Check prompt content (what's CONFIGURED, may differ from what's IN the thread)
    print("\n" + "=" * 60)
    print("PROMPT ANALYSIS (CONFIGURED)")
    print("=" * 60)

    prompt = agent.system_instructions or ""

    # Check for key phrases
    checks = [
        ("Available Servers", "Available Servers" in prompt),
        ("cube server", "cube" in prompt.lower()),
        ("Spawn a worker immediately", "Spawn a worker immediately" in prompt),
        ("Don't preemptively", "Don't preemptively" in prompt),
        ("runner_list", "runner_list" in prompt),
    ]

    for name, found in checks:
        status = "✅" if found else "❌"
        print(f"{status} {name}: {'found' if found else 'NOT FOUND'}")

    # Show the Infrastructure Access section
    print("\n" + "=" * 60)
    print("INFRASTRUCTURE ACCESS SECTION")
    print("=" * 60)

    if "## Infrastructure Access" in prompt:
        start = prompt.find("## Infrastructure Access")
        end = prompt.find("##", start + 10)
        if end == -1:
            end = start + 1000
        section = prompt[start:end]
        print(section[:800])
    else:
        print("❌ No Infrastructure Access section found!")

    # Show Available Servers section
    print("\n" + "=" * 60)
    print("AVAILABLE SERVERS SECTION")
    print("=" * 60)

    if "## Available Servers" in prompt:
        start = prompt.find("## Available Servers")
        end = prompt.find("##", start + 10)
        if end == -1:
            end = start + 500
        section = prompt[start:end]
        print(section[:500])
    else:
        print("❌ No Available Servers section found!")

    db.close()
    print("\n" + "=" * 60)
    print("DIAGNOSIS")
    print("=" * 60)

    issues = []
    if not user.context:
        issues.append("User context is empty - run auto-seed")
    if "spawn_commis" not in (agent.allowed_tools or []):
        issues.append("spawn_commis not in allowed_tools")
    if "cube" not in prompt.lower():
        issues.append("'cube' not in prompt - user context not injected")
    if "Spawn a worker immediately" not in prompt:
        issues.append("New prompt not loaded - restart backend?")

    if issues:
        print("Issues found:")
        for issue in issues:
            print(f"  ❌ {issue}")
    else:
        print("✅ Everything looks correct. Model may just be ignoring instructions.")
        print("   Try a more direct prompt: 'spawn a worker to check disk on cube'")


if __name__ == "__main__":
    main()
