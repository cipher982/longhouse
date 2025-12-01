#!/usr/bin/env python3

"""
Debug workflow execution to see what's happening
"""

import asyncio
import requests
from backend.zerg.services.langgraph_workflow_engine import langgraph_workflow_engine

async def debug_execution():
    print("🔍 Debugging workflow execution")
    
    # Reset database and create test workflow
    requests.post("http://localhost:8001/admin/reset-database")
    
    # Create agent
    agent_response = requests.post("http://localhost:8001/api/agents/", json={
        "name": "Debug Agent",
        "system_instructions": "You are a debug agent.",
        "task_instructions": "Execute the given task.",
        "model": "gpt-4o-mini"
    })
    agent_id = agent_response.json()['id']
    print(f"✅ Created agent {agent_id}")
    
    # Create workflow
    workflow_response = requests.post("http://localhost:8001/api/workflows/", json={
        "name": "Debug Workflow",
        "canvas_data": {
            "nodes": [
                {
                    "node_id": "trigger_1",
                    "node_type": {"Trigger": {"trigger_type": "Manual", "config": {}}},
                    "text": "▶ Start",
                    "x": 100.0,
                    "y": 100.0
                }
            ],
            "edges": []
        }
    })
    workflow_id = workflow_response.json()['id']
    print(f"✅ Created workflow {workflow_id}")
    
    try:
        # Test direct execution (old method)
        print("🚀 Testing direct execution (old method)...")
        execution_id = await langgraph_workflow_engine.execute_workflow(workflow_id)
        print(f"✅ Direct execution completed with ID: {execution_id}")
        
        # Test new method
        print("🚀 Testing new method with pre-created execution...")
        
        # Create execution record directly
        from backend.zerg.crud import crud
        from backend.zerg.database import get_session_factory
        
        session_factory = get_session_factory()
        with session_factory() as db:
            execution = crud.create_workflow_execution(
                db, 
                workflow_id=workflow_id, 
                status="running", 
                triggered_by="debug"
            )
            execution_id = execution.id
            print(f"✅ Created execution record: {execution_id}")
        
        # Now run the execution
        await langgraph_workflow_engine.execute_workflow_with_id(workflow_id, execution_id)
        print(f"✅ New method execution completed")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(debug_execution())