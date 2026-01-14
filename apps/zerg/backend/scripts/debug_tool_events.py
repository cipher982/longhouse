#!/usr/bin/env python
"""Debug script to verify tool event flow.

This script simulates the complete event flow from worker tool execution
to SSE output, helping verify Phase 2 implementation is working correctly.

Usage:
    cd apps/zerg/backend
    uv run python scripts/debug_tool_events.py
"""

import asyncio
import sys
from datetime import UTC, datetime

# Add parent to path for imports
sys.path.insert(0, ".")

from zerg.events.event_bus import EventType, event_bus


async def simulate_tool_events():
    """Simulate tool events as they would be emitted during worker execution."""
    print("=" * 60)
    print("Tool Event Flow Debug Script")
    print("=" * 60)

    # Simulate event data
    worker_id = "2024-12-06T15-30-00_debug-test"
    owner_id = 1
    run_id = 999

    received_events = []

    async def event_collector(event):
        """Collect events for verification."""
        received_events.append(event)
        print(f"  📡 Received: {event.get('event_type', 'unknown')}")

    # Subscribe to tool events (like SSE endpoint does)
    print("\n1. Subscribing to tool events...")
    event_bus.subscribe(EventType.WORKER_TOOL_STARTED, event_collector)
    event_bus.subscribe(EventType.WORKER_TOOL_COMPLETED, event_collector)
    event_bus.subscribe(EventType.WORKER_TOOL_FAILED, event_collector)
    print("   ✓ Subscribed to WORKER_TOOL_STARTED")
    print("   ✓ Subscribed to WORKER_TOOL_COMPLETED")
    print("   ✓ Subscribed to WORKER_TOOL_FAILED")

    # Emit WORKER_TOOL_STARTED
    print("\n2. Emitting WORKER_TOOL_STARTED event...")
    await event_bus.publish(
        EventType.WORKER_TOOL_STARTED,
        {
            "event_type": "worker_tool_started",
            "worker_id": worker_id,
            "owner_id": owner_id,
            "run_id": run_id,
            "tool_name": "ssh_exec",
            "tool_call_id": "call_001",
            "tool_args_preview": "{'host': 'cube', 'command': 'df -h'}",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    # Give event loop a chance to process
    await asyncio.sleep(0.1)

    # Emit WORKER_TOOL_COMPLETED
    print("\n3. Emitting WORKER_TOOL_COMPLETED event...")
    await event_bus.publish(
        EventType.WORKER_TOOL_COMPLETED,
        {
            "event_type": "worker_tool_completed",
            "worker_id": worker_id,
            "owner_id": owner_id,
            "run_id": run_id,
            "tool_name": "ssh_exec",
            "tool_call_id": "call_001",
            "duration_ms": 823,
            "result_preview": "Filesystem      Size  Used Avail Use%...",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    await asyncio.sleep(0.1)

    # Emit another WORKER_TOOL_STARTED
    print("\n4. Emitting second WORKER_TOOL_STARTED event...")
    await event_bus.publish(
        EventType.WORKER_TOOL_STARTED,
        {
            "event_type": "worker_tool_started",
            "worker_id": worker_id,
            "owner_id": owner_id,
            "run_id": run_id,
            "tool_name": "http_request",
            "tool_call_id": "call_002",
            "tool_args_preview": "{'url': 'https://api.example.com/status'}",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    await asyncio.sleep(0.1)

    # Emit WORKER_TOOL_FAILED
    print("\n5. Emitting WORKER_TOOL_FAILED event...")
    await event_bus.publish(
        EventType.WORKER_TOOL_FAILED,
        {
            "event_type": "worker_tool_failed",
            "worker_id": worker_id,
            "owner_id": owner_id,
            "run_id": run_id,
            "tool_name": "http_request",
            "tool_call_id": "call_002",
            "duration_ms": 125,
            "error": "Connection timeout after 5000ms",
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )

    await asyncio.sleep(0.1)

    # Cleanup
    event_bus.unsubscribe(EventType.WORKER_TOOL_STARTED, event_collector)
    event_bus.unsubscribe(EventType.WORKER_TOOL_COMPLETED, event_collector)
    event_bus.unsubscribe(EventType.WORKER_TOOL_FAILED, event_collector)

    # Summary
    print("\n" + "=" * 60)
    print("Results")
    print("=" * 60)
    print(f"Events emitted: 4")
    print(f"Events received: {len(received_events)}")

    if len(received_events) == 4:
        print("\n✅ SUCCESS: All events were received by subscribers")

        # Print event details
        print("\nEvent Details:")
        for i, event in enumerate(received_events, 1):
            event_type = event.get("event_type", "unknown")
            tool_name = event.get("tool_name", "unknown")
            status = "started" if "started" in event_type else ("completed" if "completed" in event_type else "failed")
            print(f"  {i}. {tool_name} [{status}]")
            if "duration_ms" in event:
                print(f"     Duration: {event['duration_ms']}ms")
            if "error" in event:
                print(f"     Error: {event['error']}")
    else:
        print(f"\n❌ FAILURE: Expected 4 events, got {len(received_events)}")
        return False

    return True


async def verify_event_types_exist():
    """Verify that EventType enum has the tool event types."""
    print("\n" + "=" * 60)
    print("Verifying EventType enum")
    print("=" * 60)

    required_types = [
        "WORKER_TOOL_STARTED",
        "WORKER_TOOL_COMPLETED",
        "WORKER_TOOL_FAILED",
    ]

    all_present = True
    for type_name in required_types:
        if hasattr(EventType, type_name):
            value = getattr(EventType, type_name).value
            print(f"  ✓ EventType.{type_name} = '{value}'")
        else:
            print(f"  ✗ EventType.{type_name} is MISSING")
            all_present = False

    if all_present:
        print("\n✅ All required EventType values are present")
    else:
        print("\n❌ Some EventType values are missing")

    return all_present


async def main():
    """Run all verification steps."""
    print("\n🔧 Debug Script: Worker Tool Events (Phase 2)\n")

    # Verify event types exist
    types_ok = await verify_event_types_exist()

    # Simulate event flow
    flow_ok = await simulate_tool_events()

    print("\n" + "=" * 60)
    print("Final Summary")
    print("=" * 60)

    if types_ok and flow_ok:
        print("✅ All checks passed! Tool event flow is working correctly.")
        print("\nThe events will flow as follows:")
        print("  1. Worker executes tool → supervisor_react_engine emits event")
        print("  2. Event bus delivers to subscribers")
        print("  3. SSE endpoint receives and forwards to frontend")
        print("  4. Frontend event bus receives and updates UI")
        return 0
    else:
        print("❌ Some checks failed. Review the output above.")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
