#!/usr/bin/env python3
"""Live integration test for New Features (KV, Tasks, Web Search) via Jarvis API.

This script tests the "new stuff" added to the Zerg/Jarvis platform.
It assumes a running backend server at localhost:8000 (or specified base URL).

Usage:
    python apps/zerg/backend/scripts/test_new_features.py --token <JWT_TOKEN>
"""

import argparse
import json
import sys
import time
import requests
from typing import Generator, Any

# Re-using logic from test_supervisor_live.py for consistency
def parse_sse_events(response: requests.Response) -> Generator[dict, None, None]:
    event_type = None
    data_lines = []
    for line in response.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())
        elif line == "":
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    parsed_data = json.loads(data)
                except json.JSONDecodeError:
                    parsed_data = data
                yield {"event": event_type or "message", "data": parsed_data}
            event_type = None
            data_lines = []

def dispatch_task(base_url: str, headers: dict, task: str) -> dict:
    print(f"\n[DISPATCH] {task}")
    try:
        response = requests.post(
            f"{base_url}/api/jarvis/supervisor",
            json={"task": task},
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"FAILED to dispatch: {e}")
        if 'response' in locals():
            print(response.text)
        sys.exit(1)

def wait_for_completion(base_url: str, headers: dict, run_id: int, timeout: int = 60) -> str:
    """Waits for supervisor_complete event and returns the final assistant message."""
    print(f"[WAITING] Run ID: {run_id}...", end="", flush=True)
    start_time = time.time()
    final_message = ""

    try:
        response = requests.get(
            f"{base_url}/api/jarvis/supervisor/events",
            params={"run_id": run_id},
            headers=headers,
            stream=True,
            timeout=timeout,
        )

        for event in parse_sse_events(response):
            elapsed = time.time() - start_time
            if elapsed > timeout:
                print(" TIMEOUT")
                return ""

            event_type = event["event"]
            data = event["data"]

            if event_type == "agent_event":
                payload = data.get("payload", {})
                if payload.get("type") == "tool_start":
                    print(f"\n  [TOOL] {payload.get('tool_name')} args={payload.get('args')}", end="")
                if payload.get("type") == "tool_result":
                    print(f" -> Result length: {len(str(payload.get('result')))}", end="")

            if event_type == "supervisor_complete":
                print(" DONE")
                if isinstance(data, dict) and "message" in data:
                    final_message = data["message"]
                elif isinstance(data, dict) and "payload" in data:
                     final_message = data["payload"].get("message", "")
                return final_message

            if event_type == "error":
                print(f" ERROR: {data}")
                return ""

    except Exception as e:
        print(f"\nError reading stream: {e}")

    return final_message

def test_kv_memory(base_url: str, headers: dict):
    print("\n--- TEST: KV Memory Tools ---")

    # 1. Store
    key = f"test_key_{int(time.time())}"
    value = "secret_verification_value"
    task_store = f"Use your memory tool to save the value '{value}' under the key '{key}'."

    data = dispatch_task(base_url, headers, task_store)
    wait_for_completion(base_url, headers, data["run_id"])

    # 2. Retrieve
    task_retrieve = f"What is the value stored in your memory under '{key}'? Answer with just the value."
    data = dispatch_task(base_url, headers, task_retrieve)
    result = wait_for_completion(base_url, headers, data["run_id"])

    if value in result:
        print(f"✅ KV Verify Passed: Found '{value}' in response.")
    else:
        print(f"❌ KV Verify Failed: Expected '{value}', got '{result}'")

def test_task_tools(base_url: str, headers: dict):
    print("\n--- TEST: Task Management Tools ---")

    task_name = f"Test Task {int(time.time())}"

    # 1. Create
    task_create = f"Create a new task on my list called '{task_name}'."
    data = dispatch_task(base_url, headers, task_create)
    wait_for_completion(base_url, headers, data["run_id"])

    # 2. List/Verify
    task_list = "List my current tasks and tell me if you see the one we just created."
    data = dispatch_task(base_url, headers, task_list)
    result = wait_for_completion(base_url, headers, data["run_id"])

    if task_name in result:
        print(f"✅ Task Verify Passed: Found '{task_name}' in list.")
    else:
        print(f"❌ Task Verify Failed: '{task_name}' not found in '{result}'")

def test_web_search(base_url: str, headers: dict):
    print("\n--- TEST: Web Search ---")

    # Simple query that requires external info
    query = "What is the capital of France?"
    task = f"Search the web to find out: {query}"

    data = dispatch_task(base_url, headers, task)
    result = wait_for_completion(base_url, headers, data["run_id"])

    if "Paris" in result:
        print(f"✅ Web Search Passed: Found 'Paris' in response.")
    else:
        print(f"❌ Web Search Failed: Did not find 'Paris' in '{result}'")

def main():
    parser = argparse.ArgumentParser(description="Test New Features")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--token", help="JWT Token (optional if AUTH_DISABLED=1)")
    args = parser.parse_args()

    headers = {
        "Content-Type": "application/json"
    }
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    try:
        test_kv_memory(args.base_url, headers)
        test_task_tools(args.base_url, headers)
        test_web_search(args.base_url, headers)

        print("\n\n🎉 All Feature Tests Completed (Check ✅/❌ above)")

    except KeyboardInterrupt:
        print("\nAborted.")

if __name__ == "__main__":
    main()
