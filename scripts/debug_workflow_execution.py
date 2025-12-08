#!/usr/bin/env python3

"""
Debug script to test workflow execution end-to-end
"""

import requests
import json
import time
import sys

BASE_URL = "http://localhost:8001"

def debug_print(message):
    print(f"🔍 {message}")
    sys.stdout.flush()

def test_backend_health():
    """Test if backend is responding"""
    debug_print("Testing backend health...")
    try:
        response = requests.get(f"{BASE_URL}/api/agents")
        debug_print(f"Backend response: {response.status_code}")
        return response.status_code == 200
    except Exception as e:
        debug_print(f"Backend connection failed: {e}")
        return False

def reset_database():
    """Reset database for clean test"""
    debug_print("Resetting database...")
    try:
        response = requests.post(f"{BASE_URL}/admin/reset-database")
        debug_print(f"Database reset: {response.json()}")
        return response.status_code == 200
    except Exception as e:
        debug_print(f"Database reset failed: {e}")
        return False

def create_test_workflow():
    """Create a workflow with trigger and agent nodes"""
    debug_print("Creating test workflow...")

    workflow_data = {
        "name": "Debug Test Workflow",
        "description": "Manual trigger + agent",
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
                    "config": {"agent_id": 1}
                }
            ],
            "edges": [
                {"from_node_id": "trigger_1", "to_node_id": "agent_1", "config": {}}
            ]
        }
    }

    try:
        response = requests.post(f"{BASE_URL}/api/workflows/",
                               json=workflow_data,
                               headers={"Content-Type": "application/json"})

        if response.status_code in [200, 201]:
            workflow = response.json()
            debug_print(f"Created workflow ID: {workflow['id']}")
            return workflow['id']
        else:
            debug_print(f"Workflow creation failed: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        debug_print(f"Workflow creation error: {e}")
        return None

def create_test_agent():
    """Create a test agent"""
    debug_print("Creating test agent...")

    agent_data = {
        "name": "Test Agent",
        "system_instructions": "You are a test agent.",
        "task_instructions": "Execute the given task.",
        "model": "gpt-5-nano"
    }

    try:
        response = requests.post(f"{BASE_URL}/api/agents/",
                               json=agent_data,
                               headers={"Content-Type": "application/json"})

        if response.status_code in [200, 201]:
            agent = response.json()
            debug_print(f"Created agent ID: {agent['id']}")
            return agent['id']
        else:
            debug_print(f"Agent creation failed: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        debug_print(f"Agent creation error: {e}")
        return None

def execute_workflow(workflow_id):
    """Execute the workflow and monitor progress"""
    debug_print(f"Executing workflow {workflow_id}...")

    try:
        response = requests.post(f"{BASE_URL}/api/workflow-executions/{workflow_id}/start")

        if response.status_code == 200:
            execution = response.json()
            execution_id = execution['execution_id']
            debug_print(f"Started execution ID: {execution_id}")
            debug_print(f"Initial status: {execution.get('status', 'unknown')}")

            # Monitor execution for 30 seconds
            for i in range(30):
                time.sleep(1)
                try:
                    # Try to get execution status
                    status_response = requests.get(f"{BASE_URL}/api/workflow-executions/{execution_id}/status")
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        debug_print(f"Status check {i+1}: {status_data.get('status', 'unknown')}")

                        if status_data.get('status') in ['success', 'failed']:
                            debug_print(f"Execution completed with status: {status_data['status']}")
                            return True
                    else:
                        debug_print(f"Status check {i+1}: endpoint returned {status_response.status_code}")
                except Exception as e:
                    debug_print(f"Status check {i+1} failed: {e}")

            debug_print("Execution monitoring timed out after 30 seconds")
            return False

        else:
            debug_print(f"Execution failed: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        debug_print(f"Execution error: {e}")
        return False

def main():
    """Main debug workflow"""
    debug_print("Starting workflow execution debug")

    # Test backend connection
    if not test_backend_health():
        debug_print("❌ Backend not responding")
        return False

    # Reset database
    if not reset_database():
        debug_print("❌ Database reset failed")
        return False

    # Create agent first
    agent_id = create_test_agent()
    if not agent_id:
        debug_print("❌ Agent creation failed")
        return False

    # Create workflow
    workflow_id = create_test_workflow()
    if not workflow_id:
        debug_print("❌ Workflow creation failed")
        return False

    # Execute workflow
    success = execute_workflow(workflow_id)

    if success:
        debug_print("✅ Workflow execution completed successfully")
    else:
        debug_print("❌ Workflow execution failed or timed out")

    return success

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
