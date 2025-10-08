#!/usr/bin/env python3
"""
Debug script to test the agent node execution logic without full LangGraph.
"""

import sys
from pathlib import Path

# Add backend to path
sys.path.append(str(Path(__file__).parent))

from zerg.services.langgraph_workflow_engine import WorkflowState


def debug_agent_node_logic():
    """Debug the agent node creation logic."""

    print("🐛 Debugging Agent Node Logic")
    print("=" * 50)

    # Simulate the node_config from canvas_data
    node_config = {
        "agent_id": 3,
        "color": "#2ecc71",
        "height": 80.0,
        "is_dragging": False,
        "is_selected": False,
        "node_id": "node_0",
        "node_type": "AgentIdentity",
        "parent_id": None,
        "text": "New Agent 81",
        "width": 200.0,
        "x": 280.0,
        "y": 178.0,
    }

    print(f"📊 Node config: {node_config}")

    # Test the agent node creation logic
    node_id = str(node_config.get("node_id", "unknown"))
    agent_id = node_config.get("agent_id")

    print(f"📊 Extracted node_id: {node_id}")
    print(f"📊 Extracted agent_id: {agent_id}")

    # Check if this is correct for the agent node function
    if not agent_id:
        print("❌ ISSUE FOUND: agent_id is None or missing!")
        print("   The agent node function would fail because it can't find the agent")
        return False

    print("✅ agent_id extracted successfully")

    # Check message extraction
    user_message = node_config.get("message", "Execute this task")
    print(f"📊 User message: {user_message}")

    print("\n🔍 Agent Node Function Analysis:")
    print(f"   1. node_id: {node_id} ✅")
    print(f"   2. agent_id: {agent_id} ✅")
    print(f"   3. user_message: {user_message} ✅")
    print("   4. Would query database for agent...")
    print("   5. Would create thread and run agent...")
    print("   6. Would return state update...")

    return True


def debug_workflow_state():
    """Debug the WorkflowState structure."""

    print("\n🐛 Debugging WorkflowState")
    print("=" * 50)

    # Create a test state similar to what would be used
    test_state = WorkflowState(
        execution_id=1,
        node_outputs={},
        completed_nodes=[],
        error=None,
    )

    print(f"📊 Initial state: {test_state}")

    # Test state update like the agent node would return
    state_update = {"node_outputs": {"node_0": {"agent_id": 3, "type": "agent"}}, "completed_nodes": ["node_0"]}

    print(f"📊 Expected state update: {state_update}")

    return True


if __name__ == "__main__":
    success1 = debug_agent_node_logic()
    success2 = debug_workflow_state()

    if success1 and success2:
        print("\n✅ Agent node logic looks correct!")
        print("🤔 The issue might be:")
        print("   1. Agent ID 3 doesn't exist in the database")
        print("   2. Database connection issues")
        print("   3. Exception in AgentRunner.run_thread()")
        print("   4. LangGraph not calling the node function")
        print("   5. Silent exception handling somewhere")
    else:
        print("\n❌ Found issues in agent node logic!")
