#!/usr/bin/env python3
"""
Debug script to test LangGraph workflow execution with enhanced logging.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add backend to path
sys.path.append(str(Path(__file__).parent))

# Configure detailed logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


async def debug_agent_node_only():
    """Debug just the agent node function without LangGraph."""

    print("🐛 Debugging Agent Node Function Only")
    print("=" * 60)

    # Simulate canvas_data with single agent node
    canvas_data = {
        "nodes": [
            {
                "node_id": "node_0",
                "node_type": "AgentIdentity",
                "agent_id": 3,
                "text": "New Agent 81",
                "message": "Hello, please respond to this message",
                "x": 280.0,
                "y": 178.0,
                "width": 200.0,
                "height": 80.0,
                "color": "#2ecc71",
                "is_selected": False,
                "is_dragging": False,
                "parent_id": None,
            }
        ],
        "edges": [],
    }

    print(f"📊 Canvas data: {canvas_data}")

    try:
        # Import and create the workflow engine
        from zerg.services.langgraph_workflow_engine import LangGraphWorkflowEngine
        from zerg.services.langgraph_workflow_engine import WorkflowState

        engine = LangGraphWorkflowEngine()

        # Store nodes/edges for node function creation
        engine._current_nodes = canvas_data["nodes"]
        engine._current_edges = canvas_data["edges"]

        # Get the node config
        node_config = canvas_data["nodes"][0]

        print("\n🧪 Testing agent node function directly...")
        print(f"📊 Node config: {node_config}")

        # Create the agent node function
        agent_node_func = engine._create_agent_node(node_config)

        # Create test state
        test_state = WorkflowState(execution_id=999, node_outputs={}, completed_nodes=[], error=None)

        print(f"📊 Test state: {test_state}")
        print("🎯 Calling agent node function...")

        # This should show us exactly where the issue is
        result = await agent_node_func(test_state)

        print(f"✅ Agent node completed! Result: {result}")
        return True

    except Exception as e:
        print(f"❌ Error in agent node execution: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(debug_agent_node_only())
    if success:
        print("\n✅ Debug completed successfully!")
    else:
        print("\n❌ Debug found issues!")
