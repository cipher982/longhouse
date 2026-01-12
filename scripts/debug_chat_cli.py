import asyncio
import json
import httpx
import sys
import os

# Configuration
BASE_URL = os.getenv("BASE_URL", "http://localhost:30080")
API_URL = f"{BASE_URL}/api"

async def debug_chat_flow():
    print(f"--- Starting Debug Chat CLI against {BASE_URL} ---")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Send Message
        print("\n[1] Sending message...")
        payload = {
            "message": "check health of my server", # Should trigger worker
            "model": "gpt-5.2", # Use default model
            "message_id": "debug-cli-001"  # Client-generated message ID
        }

        try:
            # We need to manually handle the SSE stream from the POST request
            async with client.stream("POST", f"{API_URL}/jarvis/chat", json=payload) as response:
                print(f"Response status: {response.status_code}")

                run_id = None

                async for line in response.aiter_lines():
                    if not line.strip():
                        continue

                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        try:
                            data = json.loads(data_str)
                            event_type = data.get("type")
                            print(f"  <- EVENT: {event_type}")

                            # Capture run_id
                            if not run_id and "run_id" in data.get("payload", {}):
                                run_id = data["payload"]["run_id"]
                                print(f"     [!] Run ID detected: {run_id}")

                            # Print key details
                            if event_type == "supervisor_deferred":
                                print(f"     [D] DEFERRED: {data['payload'].get('message')}")
                            elif event_type == "supervisor_complete":
                                print(f"     [C] COMPLETE: {data['payload'].get('result')}")
                            elif event_type == "worker_complete":
                                print(f"     [W] WORKER DONE: {data['payload'].get('worker_id')}")

                        except json.JSONDecodeError:
                            print(f"  <- RAW: {data_str}")

        except Exception as e:
            print(f"Error during chat stream: {e}")
            return

        if not run_id:
            print("❌ Failed to capture Run ID. Aborting.")
            return

        print(f"\n[2] Checking Database State for Run {run_id}...")
        # Check original run status
        res = await client.get(f"{API_URL}/jarvis/runs/{run_id}")
        if res.status_code == 200:
            run_data = res.json()
            print(f"  Run {run_id} Status: {run_data['status']}")
        else:
            print(f"  Failed to get run {run_id}: {res.status_code}")

        # Check for continuations
        print("\n[3] Searching for Continuations...")
        # We'll list recent runs and look for continuation_of_run_id == run_id
        res = await client.get(f"{API_URL}/jarvis/runs?limit=10")
        if res.status_code == 200:
            runs = res.json()
            # API returns list of summaries directly
            continuation = next((r for r in runs if r.get('continuation_of_run_id') == run_id), None)

            if continuation:
                cid = continuation['id']
                print(f"  ✅ FOUND CONTINUATION RUN: {cid}")
                print(f"     Status: {continuation['status']}")
                print(f"     Summary: {continuation['summary']}")

                # Fetch events for continuation to see if they were emitted
                events_res = await client.get(f"{API_URL}/jarvis/runs/{cid}/events")
                events = events_res.json().get('events', [])
                print(f"     Events count: {len(events)}")
                if events:
                    print(f"     First event: {events[0]['event_type']}")
                    print(f"     Last event: {events[-1]['event_type']}")
            else:
                print("  ❌ NO CONTINUATION FOUND in DB.")
        else:
            print(f"  Failed to list runs: {res.status_code}")

if __name__ == "__main__":
    try:
        asyncio.run(debug_chat_flow())
    except KeyboardInterrupt:
        pass
