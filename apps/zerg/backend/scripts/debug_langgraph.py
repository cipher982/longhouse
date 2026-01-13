#!/usr/bin/env python3
"""
Debug LangGraph state and checkpoints - AI-optimized for minimal tokens.

Usage:
    uv run scripts/debug_langgraph.py inspect <thread_id>     # LangGraph checkpoint state
    uv run scripts/debug_langgraph.py history <thread_id>     # Checkpoint history
    uv run scripts/debug_langgraph.py validate <thread_id>    # Validate message integrity
    uv run scripts/debug_langgraph.py thread <thread_id>      # DB ThreadMessages (compact)
    uv run scripts/debug_langgraph.py batch --stdin           # Batch queries from JSON
    uv run scripts/debug_langgraph.py resume-dry-run <run_id> # Simulate resume

Batch query example (minimal tokens):
    echo '{"queries":[{"op":"thread","thread_id":1},{"op":"validate","thread_id":"1"}]}' | \\
        uv run scripts/debug_langgraph.py batch --stdin
"""

import sys
import os
import asyncio
import argparse
import json
import uuid
import hashlib
from datetime import datetime
from typing import Any
from collections import Counter

# Add backend to path (works from repo root)
backend_path = os.path.join(os.path.dirname(__file__), "..")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from zerg.database import get_session_factory, db_session
from zerg.services.checkpointer import get_checkpointer
from zerg.models.models import AgentRun, ThreadMessage
from langchain_core.messages import BaseMessage, messages_to_dict

class DebugEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, uuid.UUID)):
            return str(obj)
        if hasattr(obj, "dict") and callable(obj.dict):
            return obj.dict()
        return super().default(obj)

async def get_cp():
    return get_checkpointer()

async def cmd_inspect(args):
    """Dump current state of a thread"""
    cp = await get_cp()
    config = {"configurable": {"thread_id": args.thread_id}}
    if args.checkpoint_id:
        config["configurable"]["checkpoint_id"] = args.checkpoint_id

    try:
        # We need to access the underlying async checkpointer
        checkpoint_tuple = await cp.aget_tuple(config)

        if not checkpoint_tuple:
            print(json.dumps({"error": "No checkpoint found"}, cls=DebugEncoder))
            return

        config = checkpoint_tuple.config
        checkpoint = checkpoint_tuple.checkpoint
        metadata = checkpoint_tuple.metadata
        parent_config = checkpoint_tuple.parent_config

        output = {
            "config": config,
            "metadata": metadata,
            "parent_config": parent_config,
            "checkpoint": {
                "v": checkpoint.get("v"),
                "ts": checkpoint.get("ts"),
                "channel_values": {},
                "versions_seen": checkpoint.get("versions_seen")
            }
        }

        # Serialize channel values (messages are here)
        if "channel_values" in checkpoint:
            for k, v in checkpoint["channel_values"].items():
                if isinstance(v, list) and all(isinstance(x, BaseMessage) for x in v):
                    output["checkpoint"]["channel_values"][k] = messages_to_dict(v)
                else:
                    output["checkpoint"]["channel_values"][k] = str(v)

        print(json.dumps(output, cls=DebugEncoder, indent=2))

    except Exception as e:
        print(json.dumps({"error": str(e)}, cls=DebugEncoder))


async def cmd_history(args):
    """List recent checkpoints"""
    cp = await get_cp()
    config = {"configurable": {"thread_id": args.thread_id}}

    checkpoints = []
    async for checkpoint in cp.alist(config, limit=args.limit):
        # checkpoint is a CheckpointTuple
        checkpoints.append({
            "checkpoint_id": checkpoint.checkpoint["id"],
            "ts": checkpoint.checkpoint.get("ts"),
            "metadata": checkpoint.metadata,
            "parent_checkpoint_id": checkpoint.parent_config.get("configurable", {}).get("checkpoint_id") if checkpoint.parent_config else None
        })

    print(json.dumps(checkpoints, cls=DebugEncoder, indent=2))


async def cmd_validate(args):
    """Validate thread state for common issues"""
    cp = await get_cp()
    config = {"configurable": {"thread_id": args.thread_id}}

    checkpoint_tuple = await cp.aget_tuple(config)
    if not checkpoint_tuple:
        print(json.dumps({"error": "No checkpoint found"}, cls=DebugEncoder))
        return

    config = checkpoint_tuple.config
    checkpoint = checkpoint_tuple.checkpoint
    metadata = checkpoint_tuple.metadata

    issues = []

    # Check messages
    if "channel_values" in checkpoint and "messages" in checkpoint["channel_values"]:
        messages = checkpoint["channel_values"]["messages"]

        # Check 1: Duplicate Tool Call IDs
        tool_call_ids = []
        for msg in messages:
            if hasattr(msg, "tool_call_id") and msg.tool_call_id:
                tool_call_ids.append(msg.tool_call_id)
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_ids.append(tc["id"])

        counts = Counter(tool_call_ids)
        dupes = [k for k, v in counts.items() if v > 2] # Usually 1 definition + 1 result = 2 refs. >2 is suspicious?
        # Actually, tool_call_id in ToolMessage should match id in AIMessage.tool_calls.
        # So we expect pairs. If we see same ID in multiple ToolMessages, that's a dupe.

        tool_msg_ids = [msg.tool_call_id for msg in messages if hasattr(msg, "tool_call_id") and msg.tool_call_id]
        tool_msg_counts = Counter(tool_msg_ids)
        tool_msg_dupes = [k for k, v in tool_msg_counts.items() if v > 1]

        if tool_msg_dupes:
            issues.append(f"Duplicate ToolMessages for ids: {tool_msg_dupes}")

        # Check 2: ToolMessage without preceding AIMessage
        for i, msg in enumerate(messages):
            if msg.type == "tool":
                if i == 0:
                    issues.append(f"ToolMessage at index 0 (no preceding AI message)")
                    continue
                prev = messages[i-1]
                if prev.type != "ai":
                    issues.append(f"ToolMessage at index {i} preceded by {prev.type} (expected ai)")
                elif not hasattr(prev, "tool_calls") or not prev.tool_calls:
                    issues.append(f"ToolMessage at index {i} preceded by AIMessage without tool_calls")
                else:
                    # Check if ID matches
                    found = False
                    for tc in prev.tool_calls:
                        if tc["id"] == msg.tool_call_id:
                            found = True
                            break
                    if not found:
                        issues.append(f"ToolMessage at index {i} has id {msg.tool_call_id} not found in preceding AIMessage tool_calls")

    print(json.dumps({"valid": len(issues) == 0, "issues": issues}, cls=DebugEncoder, indent=2))


async def cmd_thread(args):
    """Inspect ThreadMessages from DB (compact, AI-friendly format)"""
    with db_session() as db:
        query = db.query(ThreadMessage).filter(ThreadMessage.thread_id == args.thread_id)
        query = query.order_by(ThreadMessage.id)
        if args.limit:
            query = query.limit(args.limit)
        messages = query.all()

        def msg_to_compact(m: ThreadMessage) -> dict:
            """Convert to minimal token format"""
            result = {
                "id": m.id,
                "role": m.role,
                "len": len(m.content) if m.content else 0,
            }
            # Only include preview if there's content
            if m.content:
                preview = m.content[:60]
                if len(m.content) > 60:
                    preview += "..."
                result["preview"] = preview

            # Extract tool info from metadata
            meta = m.message_metadata or {}
            if meta.get("tool_call_id"):
                result["tool_call_id"] = meta["tool_call_id"]
            if meta.get("tool_calls"):
                result["tool_calls"] = [tc.get("id") for tc in meta["tool_calls"]]

            return result

        output = {
            "thread_id": args.thread_id,
            "count": len(messages),
            "messages": [msg_to_compact(m) for m in messages]
        }

        indent = None if args.compact else 2
        print(json.dumps(output, cls=DebugEncoder, indent=indent))


async def cmd_thread_query(params: dict) -> dict:
    """Internal: thread query for batch mode"""
    thread_id = params.get("thread_id")
    limit = params.get("limit", 100)

    with db_session() as db:
        query = db.query(ThreadMessage).filter(ThreadMessage.thread_id == thread_id)
        query = query.order_by(ThreadMessage.id).limit(limit)
        messages = query.all()

        def msg_to_compact(m: ThreadMessage) -> dict:
            result = {
                "id": m.id,
                "role": m.role,
                "len": len(m.content) if m.content else 0,
            }
            if m.content:
                result["preview"] = m.content[:60] + ("..." if len(m.content) > 60 else "")
            meta = m.message_metadata or {}
            if meta.get("tool_call_id"):
                result["tool_call_id"] = meta["tool_call_id"]
            if meta.get("tool_calls"):
                result["tool_calls"] = [tc.get("id") for tc in meta["tool_calls"]]
            return result

        return {
            "thread_id": thread_id,
            "count": len(messages),
            "messages": [msg_to_compact(m) for m in messages]
        }


async def cmd_validate_query(params: dict) -> dict:
    """Internal: validate query for batch mode"""
    thread_id = params.get("thread_id")
    cp = await get_cp()
    config = {"configurable": {"thread_id": str(thread_id)}}

    checkpoint_tuple = await cp.aget_tuple(config)
    if not checkpoint_tuple:
        return {"error": "No checkpoint found"}

    checkpoint = checkpoint_tuple.checkpoint
    issues = []

    if "channel_values" in checkpoint and "messages" in checkpoint["channel_values"]:
        messages = checkpoint["channel_values"]["messages"]
        issues.extend(_validate_duplicate_tool_messages(messages))
        issues.extend(_validate_tool_message_ordering(messages))
        issues.extend(_validate_tool_response_count(messages))
        issues.extend(_validate_duplicate_content(messages))

    return {"valid": len(issues) == 0, "issues": issues}


def _validate_duplicate_tool_messages(messages: list) -> list:
    """Check for duplicate ToolMessage IDs"""
    issues = []
    tool_msg_ids = [msg.tool_call_id for msg in messages if hasattr(msg, "tool_call_id") and msg.tool_call_id]
    counts = Counter(tool_msg_ids)
    dupes = [k for k, v in counts.items() if v > 1]
    if dupes:
        issues.append(f"Duplicate ToolMessages for ids: {dupes}")
    return issues


def _validate_tool_message_ordering(messages: list) -> list:
    """Check that ToolMessages follow AIMessages with matching tool_calls"""
    issues = []
    for i, msg in enumerate(messages):
        if getattr(msg, "type", None) == "tool":
            if i == 0:
                issues.append("ToolMessage at index 0 (no preceding AI message)")
                continue
            prev = messages[i - 1]
            if getattr(prev, "type", None) != "ai":
                issues.append(f"ToolMessage at index {i} preceded by {prev.type} (expected ai)")
            elif not hasattr(prev, "tool_calls") or not prev.tool_calls:
                issues.append(f"ToolMessage at index {i} preceded by AIMessage without tool_calls")
            else:
                found = any(tc["id"] == msg.tool_call_id for tc in prev.tool_calls)
                if not found:
                    issues.append(f"ToolMessage at {i}: id {msg.tool_call_id} not in preceding AIMessage")
    return issues


def _validate_tool_response_count(messages: list) -> list:
    """Every tool_call should have exactly one response"""
    issues = []
    tool_responses: dict[str, int] = {}
    for msg in messages:
        if hasattr(msg, "tool_call_id") and msg.tool_call_id:
            tool_responses[msg.tool_call_id] = tool_responses.get(msg.tool_call_id, 0) + 1

    for tc_id, count in tool_responses.items():
        if count > 1:
            issues.append(f"Tool call {tc_id[:12]}... has {count} responses (expected 1)")
    return issues


def _validate_duplicate_content(messages: list) -> list:
    """Check for consecutive tool messages with identical content"""
    issues = []
    prev_content = None
    prev_type = None
    for i, msg in enumerate(messages):
        msg_type = getattr(msg, "type", None)
        content = getattr(msg, "content", None)

        # Handle non-string content (e.g. list of content blocks)
        content_str = str(content) if not isinstance(content, str) else content

        if msg_type == "tool" and prev_type == "tool" and content_str == prev_content and content_str:
            # Hash for brevity
            content_hash = hashlib.sha256(content_str.encode()).hexdigest()[:8]
            issues.append(f"Duplicate tool content at {i-1},{i} (hash: {content_hash})")
        prev_content = content_str
        prev_type = msg_type
    return issues


async def cmd_batch(args):
    """Run multiple queries from stdin JSON - minimal token batch mode"""
    input_data = json.loads(sys.stdin.read())
    queries = input_data.get("queries", [])

    results = []
    for q in queries:
        op = q.get("op")
        try:
            if op == "thread":
                data = await cmd_thread_query(q)
            elif op == "validate":
                data = await cmd_validate_query(q)
            elif op == "inspect":
                data = await cmd_inspect_query(q)
            else:
                data = {"error": f"Unknown op: {op}"}
            results.append({"op": op, "ok": "error" not in data, "data": data})
        except Exception as e:
            results.append({"op": op, "ok": False, "error": str(e)})

    output = {"ok": all(r["ok"] for r in results), "results": results}
    print(json.dumps(output, cls=DebugEncoder))


async def cmd_inspect_query(params: dict) -> dict:
    """Internal: inspect query for batch mode"""
    thread_id = params.get("thread_id")
    cp = await get_cp()
    config = {"configurable": {"thread_id": str(thread_id)}}

    checkpoint_tuple = await cp.aget_tuple(config)
    if not checkpoint_tuple:
        return {"error": "No checkpoint found"}

    checkpoint = checkpoint_tuple.checkpoint
    msg_count = 0
    if "channel_values" in checkpoint and "messages" in checkpoint["channel_values"]:
        msg_count = len(checkpoint["channel_values"]["messages"])

    return {
        "thread_id": thread_id,
        "checkpoint_id": checkpoint.get("id"),
        "ts": checkpoint.get("ts"),
        "message_count": msg_count,
    }


async def cmd_resume_dry_run(args):
    """Simulate what happens if we resume this run"""
    from zerg.services.worker_resume import _count_leading_system_messages
    from zerg.services.thread_service import ThreadService

    session_factory = get_session_factory()
    with session_factory() as db:
        run = db.query(AgentRun).filter(AgentRun.id == args.run_id).first()
        if not run:
            print(json.dumps({"error": f"Run {args.run_id} not found"}, cls=DebugEncoder))
            return

        thread = run.thread
        thread_service = ThreadService()

        # Get DB messages
        db_messages = thread_service.get_thread_messages_as_langchain(db, thread.id)
        conversation_msgs = [m for m in db_messages if getattr(m, "type", None) != "system"]

        print(json.dumps({
            "info": "DB State loaded",
            "db_message_count": len(db_messages),
            "conversation_msg_count": len(conversation_msgs),
            "last_db_message": messages_to_dict([conversation_msgs[-1]]) if conversation_msgs else None
        }, cls=DebugEncoder, indent=2))

        # Simulate resume result
        # We assume args.result is a string (like from CLI)
        # But in reality it might be a dict if it comes from a tool.
        # For this dry run we just treating it as a raw string result usually.

        print("\n--- Simulation ---")

        # Logic from worker_resume.py
        # If we had a list of messages returned from the graph execution (simulated here)
        # We can't easily simulate the FULL graph execution without running it.
        # But we can check the "fresh messages" path logic.

        from langchain_core.messages import AIMessage, ToolMessage

        last_conv_msg = conversation_msgs[-1] if conversation_msgs else None
        use_fresh_messages = False

        if isinstance(last_conv_msg, AIMessage) and last_conv_msg.tool_calls:
             tool_call_ids = {tc["id"] for tc in last_conv_msg.tool_calls}
             responded_ids = {m.tool_call_id for m in conversation_msgs if isinstance(m, ToolMessage)}
             pending_ids = tool_call_ids - responded_ids

             if pending_ids:
                 print(f"Plan: Use FRESH MESSAGES path (Pending IDs: {pending_ids})")
                 print(f"Action: Would insert ToolMessage for {list(pending_ids)[0]}")
             else:
                 print("Plan: Normal resume path (All tool calls responded)")
        else:
            print("Plan: Normal resume path (Last message not AI with tool calls)")


async def main():
    parser = argparse.ArgumentParser(
        description="Debug LangGraph state - AI-optimized for minimal tokens",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s thread 1                    # DB messages (compact)
  %(prog)s validate 1                  # Check message integrity
  %(prog)s inspect 1                   # LangGraph checkpoint state
  %(prog)s batch --stdin               # Batch queries from JSON
"""
    )
    subparsers = parser.add_subparsers(dest="command")

    # inspect - LangGraph checkpoint state
    inspect_parser = subparsers.add_parser("inspect", help="LangGraph checkpoint state")
    inspect_parser.add_argument("thread_id", type=str)
    inspect_parser.add_argument("--checkpoint-id", type=str, default=None)

    # history - checkpoint history
    history_parser = subparsers.add_parser("history", help="Checkpoint history")
    history_parser.add_argument("thread_id", type=str)
    history_parser.add_argument("--limit", type=int, default=10)

    # validate - message integrity checks
    validate_parser = subparsers.add_parser("validate", help="Validate message integrity")
    validate_parser.add_argument("thread_id", type=str)

    # thread - DB ThreadMessages (compact format)
    thread_parser = subparsers.add_parser("thread", help="DB ThreadMessages (compact)")
    thread_parser.add_argument("thread_id", type=int)
    thread_parser.add_argument("--limit", type=int, default=None, help="Max messages")
    thread_parser.add_argument("--compact", action="store_true", help="No indentation")

    # batch - multiple queries from stdin
    batch_parser = subparsers.add_parser("batch", help="Batch queries from stdin JSON")
    batch_parser.add_argument("--stdin", action="store_true", required=True)
    # Note: 'inspect' op in batch mode returns a summary (thread_id, cp_id, count), not full dump.

    # resume-dry-run - simulate resume
    resume_parser = subparsers.add_parser("resume-dry-run", help="Simulate resume")
    resume_parser.add_argument("run_id", type=int)
    resume_parser.add_argument("--result", type=str, default="success")

    args = parser.parse_args()

    if args.command == "inspect":
        await cmd_inspect(args)
    elif args.command == "history":
        await cmd_history(args)
    elif args.command == "validate":
        await cmd_validate(args)
    elif args.command == "thread":
        await cmd_thread(args)
    elif args.command == "batch":
        await cmd_batch(args)
    elif args.command == "resume-dry-run":
        await cmd_resume_dry_run(args)
    else:
        parser.print_help()

if __name__ == "__main__":
    asyncio.run(main())
