#!/usr/bin/env python3
"""Live test script for Supervisor flow via Jarvis API.

This script tests the full supervisor flow against a running server.
It simulates what a user would experience through the Jarvis UI.

Usage:
    # Against local dev server (AUTH_DISABLED=1)
    python scripts/test_supervisor_live.py

    # Against specific server
    python scripts/test_supervisor_live.py --base-url http://localhost:8000

    # With authentication token
    python scripts/test_supervisor_live.py --token YOUR_JWT_TOKEN
"""

import argparse
import json
import sys
import time
from typing import Generator

import requests


def parse_sse_events(response: requests.Response) -> Generator[dict, None, None]:
    """Parse SSE events from a streaming response.

    Yields:
        Dict with 'event' and 'data' keys for each SSE event
    """
    event_type = None
    data_lines = []

    for line in response.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            # Empty line signals end of event
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    parsed_data = json.loads(data)
                except json.JSONDecodeError:
                    parsed_data = data

                yield {
                    "event": event_type or "message",
                    "data": parsed_data,
                }

            event_type = None
            data_lines = []


def test_supervisor_dispatch(base_url: str, headers: dict, task: str) -> dict:
    """Test POST /api/jarvis/supervisor endpoint.

    Args:
        base_url: Server base URL
        headers: Request headers (with auth if needed)
        task: Task to send to supervisor

    Returns:
        Response data with run_id, thread_id, stream_url
    """
    print(f"\n{'='*60}")
    print(f"DISPATCHING TASK: {task}")
    print(f"{'='*60}")

    response = requests.post(
        f"{base_url}/api/jarvis/supervisor",
        json={"task": task},
        headers=headers,
    )

    if response.status_code != 200:
        print(f"ERROR: {response.status_code}")
        print(response.text)
        sys.exit(1)

    data = response.json()
    print(f"Run ID: {data['run_id']}")
    print(f"Thread ID: {data['thread_id']}")
    print(f"Status: {data['status']}")
    print(f"Stream URL: {data['stream_url']}")

    return data


def test_supervisor_events(base_url: str, headers: dict, run_id: int, timeout: int = 60) -> list:
    """Test GET /api/stream/runs/{run_id} SSE stream.

    Args:
        base_url: Server base URL
        headers: Request headers (with auth if needed)
        run_id: Supervisor run ID to track
        timeout: Maximum seconds to wait for events

    Returns:
        List of events received
    """
    print(f"\n{'='*60}")
    print(f"LISTENING TO SSE EVENTS (run_id={run_id})")
    print(f"{'='*60}")

    events = []
    start_time = time.time()

    try:
        response = requests.get(
            f"{base_url}/api/stream/runs/{run_id}",
            headers=headers,
            stream=True,
            timeout=timeout,
        )

        if response.status_code != 200:
            print(f"ERROR: {response.status_code}")
            print(response.text)
            return events

        for event in parse_sse_events(response):
            elapsed = time.time() - start_time
            event_type = event["event"]
            data = event["data"]

            events.append(event)

            # Pretty print event
            print(f"\n[{elapsed:.1f}s] EVENT: {event_type}")

            if isinstance(data, dict):
                payload = data.get("payload", data)
                seq = data.get("seq", "N/A")
                print(f"  seq: {seq}")

                for key, value in payload.items():
                    if key not in ("owner_id",):  # Skip sensitive fields
                        val_str = str(value)[:100]
                        print(f"  {key}: {val_str}")
            else:
                print(f"  {data}")

            # Check for completion events
            if event_type in ("supervisor_complete", "error"):
                print(f"\n[{elapsed:.1f}s] SUPERVISOR COMPLETED")
                break

            # Safety timeout
            if elapsed > timeout:
                print(f"\n[{elapsed:.1f}s] TIMEOUT - stopping")
                break

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except requests.exceptions.Timeout:
        print(f"\nConnection timeout after {timeout}s")
    except Exception as e:
        print(f"\nError: {e}")

    return events


def test_cancel(base_url: str, headers: dict, run_id: int) -> dict:
    """Test POST /api/jarvis/supervisor/{run_id}/cancel endpoint.

    Args:
        base_url: Server base URL
        headers: Request headers (with auth if needed)
        run_id: Supervisor run ID to cancel

    Returns:
        Response data
    """
    print(f"\n{'='*60}")
    print(f"TESTING CANCEL (run_id={run_id})")
    print(f"{'='*60}")

    response = requests.post(
        f"{base_url}/api/jarvis/supervisor/{run_id}/cancel",
        headers=headers,
    )

    if response.status_code != 200:
        print(f"ERROR: {response.status_code}")
        print(response.text)
        return {}

    data = response.json()
    print(f"Run ID: {data['run_id']}")
    print(f"Status: {data['status']}")
    print(f"Message: {data['message']}")

    return data


def run_disk_health_check(base_url: str, headers: dict) -> None:
    """Run a disk health check task through the supervisor.

    This tests the full flow: supervisor -> spawn_worker -> ssh_exec
    """
    task = (
        "Check disk usage on all infrastructure servers (cube, clifford, zerg, slim). "
        "Report which servers have more than 80% disk usage. "
        "Use ssh_exec to run 'df -h' on each server."
    )

    # Dispatch the task
    dispatch_data = test_supervisor_dispatch(base_url, headers, task)

    # Listen to events
    events = test_supervisor_events(base_url, headers, dispatch_data["run_id"], timeout=120)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Total events received: {len(events)}")

    event_types = [e["event"] for e in events]
    for et in set(event_types):
        print(f"  {et}: {event_types.count(et)}")

    # Check for worker spawns
    worker_events = [e for e in events if "worker" in e["event"].lower()]
    if worker_events:
        print(f"\nWorker events: {len(worker_events)}")
        for we in worker_events:
            print(f"  - {we['event']}")


def main():
    parser = argparse.ArgumentParser(description="Test Supervisor flow against live server")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Server base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        help="JWT auth token (for production; dev mode uses AUTH_DISABLED)",
    )
    parser.add_argument(
        "--task",
        help="Custom task to send (default: disk health check)",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        help="Run a simple test (what time is it?)",
    )

    args = parser.parse_args()

    # Set up headers
    headers = {"Content-Type": "application/json"}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    print(f"Testing against: {args.base_url}")
    print(f"Auth: {'Token provided' if args.token else 'Using AUTH_DISABLED mode'}")

    if args.simple:
        task = "What time is it?"
        dispatch_data = test_supervisor_dispatch(args.base_url, headers, task)
        test_supervisor_events(args.base_url, headers, dispatch_data["run_id"], timeout=30)
    elif args.task:
        dispatch_data = test_supervisor_dispatch(args.base_url, headers, args.task)
        test_supervisor_events(args.base_url, headers, dispatch_data["run_id"], timeout=120)
    else:
        run_disk_health_check(args.base_url, headers)


if __name__ == "__main__":
    main()
