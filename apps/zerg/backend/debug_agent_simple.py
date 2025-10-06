#!/usr/bin/env python3
"""
Debug script to test the agent node execution logic without dependencies.
"""


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


def debug_potential_issues():
    """Debug potential issues that could cause 0 nodes executed."""

    print("\n🔍 Potential Issues Analysis")
    print("=" * 50)

    issues = [
        "1. Agent ID 3 doesn't exist in the database",
        "2. Database connection fails during execution",
        "3. AgentRunner.run_thread() throws an exception",
        "4. LangGraph isn't actually calling the node function",
        "5. Exception is thrown but caught and swallowed silently",
        "6. The state update isn't being returned properly",
        "7. LangGraph has issues with single-node graphs",
        "8. The graph compilation fails silently",
        "9. START -> node_0 -> END path isn't being executed",
    ]

    for issue in issues:
        print(f"❓ {issue}")

    print("\n🔧 Debugging Steps:")
    print("1. Add more logging to the agent node function")
    print("2. Check if agent ID 3 exists in the database")
    print("3. Verify the LangGraph execution actually calls nodes")
    print("4. Add try-catch around the entire execution to see exceptions")
    print("5. Test with a simpler placeholder node instead of agent")


if __name__ == "__main__":
    success = debug_agent_node_logic()
    debug_potential_issues()

    if success:
        print("\n✅ Agent node logic looks correct!")
        print("🤔 The issue is likely in execution flow, not node logic")
    else:
        print("\n❌ Found issues in agent node logic!")
