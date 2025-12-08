#!/usr/bin/env python3

"""
Test WebSocket subscription to workflow_execution topic
"""

import asyncio
import json
import websockets
import requests

async def test_websocket_subscription():
    print("🔌 Testing WebSocket subscription to workflow_execution topic")

    # Reset database and create test workflow
    requests.post("http://localhost:8001/admin/reset-database")

    # Create agent
    agent_response = requests.post("http://localhost:8001/api/agents/", json={
        "name": "Test Agent",
        "system_instructions": "You are a test agent.",
        "task_instructions": "Execute the given task.",
        "model": "gpt-5-nano"
    })
    agent_id = agent_response.json()['id']
    print(f"✅ Created agent {agent_id}")

    # Create workflow
    workflow_response = requests.post("http://localhost:8001/api/workflows/", json={
        "name": "WS Test Workflow",
        "description": "WebSocket test",
        "canvas": {
            "nodes": [
                {
                    "id": "trigger_1",
                    "type": "trigger",
                    "position": {"x": 100.0, "y": 100.0},
                    "config": {"trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}}
                },
                {
                    "id": "agent_1",
                    "type": "agent",
                    "position": {"x": 300.0, "y": 100.0},
                    "config": {"agent_id": agent_id}
                }
            ],
            "edges": [
                {"from_node_id": "trigger_1", "to_node_id": "agent_1", "config": {}}
            ]
        }
    })
    workflow_id = workflow_response.json()['id']
    print(f"✅ Created workflow {workflow_id}")

    # Connect to WebSocket
    uri = "ws://localhost:8001/api/ws"
    try:
        async with websockets.connect(uri) as websocket:
            print("🔌 Connected to WebSocket")

            # Subscribe to workflow_execution topic
            subscribe_message = {
                "type": "subscribe",
                "topics": [f"workflow_execution:{workflow_id}"],
                "message_id": "test-123"
            }

            await websocket.send(json.dumps(subscribe_message))
            print(f"📤 Sent subscription request: {subscribe_message}")

            # Wait for subscription response
            response = await websocket.recv()
            print(f"📥 Subscription response: {response}")

            # Start workflow execution
            print("🚀 Starting workflow execution...")
            execution_response = requests.post(f"http://localhost:8001/api/workflow-executions/{workflow_id}/start")
            execution_data = execution_response.json()
            execution_id = execution_data['execution_id']
            print(f"✅ Started execution {execution_id}")

            # Listen for WebSocket messages for 10 seconds
            print("👂 Listening for WebSocket messages...")
            try:
                for i in range(10):
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    print(f"📨 Received message {i+1}: {message}")

                    # Parse message to check if it's execution_finished
                    try:
                        msg_data = json.loads(message)
                        if msg_data.get('type') == 'execution_finished':
                            print("🎉 Received execution_finished message!")
                            return True
                    except:
                        pass

            except asyncio.TimeoutError:
                print("⏰ No more messages received")

            print("❌ Did not receive execution_finished message")
            return False

    except Exception as e:
        print(f"❌ WebSocket connection failed: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_websocket_subscription())
    print(f"Test result: {'✅ PASS' if success else '❌ FAIL'}")
