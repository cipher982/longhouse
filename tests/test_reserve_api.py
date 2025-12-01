#!/usr/bin/env python3

"""
Test the new reserve/start API pattern
"""

import requests
import time

def test_reserve_start_pattern():
    print("🔍 Testing reserve/start API pattern")
    
    # Reset database and create test workflow
    requests.post("http://localhost:8001/admin/reset-database")
    
    # Create agent
    agent_response = requests.post("http://localhost:8001/api/agents/", json={
        "name": "Test Agent",
        "system_instructions": "You are a test agent.",
        "task_instructions": "Execute the given task.",
        "model": "gpt-4o-mini"
    })
    agent_id = agent_response.json()['id']
    print(f"✅ Created agent {agent_id}")
    
    # Create workflow
    workflow_response = requests.post("http://localhost:8001/api/workflows/", json={
        "name": "Reserve Test Workflow",
        "description": "Reserve/start test",
        "canvas": {
            "nodes": [
                {
                    "id": "trigger_1",
                    "type": "trigger",
                    "position": {"x": 100.0, "y": 100.0},
                    "config": {"trigger": {"type": "manual", "config": {"enabled": True, "params": {}, "filters": []}}}
                }
            ],
            "edges": []
        }
    })
    workflow_id = workflow_response.json()['id']
    print(f"✅ Created workflow {workflow_id}")
    
    try:
        # Test reserve endpoint
        print("🔄 Testing reserve endpoint...")
        reserve_response = requests.post(f"http://localhost:8001/api/workflow-executions/{workflow_id}/reserve")
        reserve_data = reserve_response.json()
        execution_id = reserve_data['execution_id']
        print(f"✅ Reserved execution ID: {execution_id}, status: {reserve_data['status']}")
        
        # Check execution status
        status_response = requests.get(f"http://localhost:8001/api/workflow-executions/{execution_id}/status")
        status_data = status_response.json()
        print(f"📊 Execution status: {status_data['status']}")
        
        # Now start the reserved execution
        print("🚀 Starting reserved execution...")
        start_response = requests.post(f"http://localhost:8001/api/workflow-executions/executions/{execution_id}/start")
        start_data = start_response.json()
        print(f"✅ Started execution: {start_data}")
        
        # Wait a moment and check status again
        time.sleep(3)
        final_status_response = requests.get(f"http://localhost:8001/api/workflow-executions/{execution_id}/status")
        final_status_data = final_status_response.json()
        print(f"📊 Final execution status: {final_status_data['status']}")
        
        return final_status_data['status'] == 'success'
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == "__main__":
    success = test_reserve_start_pattern()
    print(f"Test result: {'✅ PASS' if success else '❌ FAIL'}")
