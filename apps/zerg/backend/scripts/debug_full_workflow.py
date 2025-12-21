#!/usr/bin/env python3
"""
Debug script to test the full LangGraph workflow execution.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add backend to path
sys.path.append(str(Path(__file__).parent))

# Configure detailed logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


async def debug_full_workflow():
    """Debug the full LangGraph workflow execution."""

    print("🐛 Debugging Full LangGraph Workflow")
    print("=" * 60)

    # Simulate canvas_data with single agent node
    canvas_data = {
        "nodes": [
            {
                "node_id": "node_0",
                "node_type": "AgentIdentity",
                "agent_id": 3,
                "text": "New Agent 81",
                "message": "Hello, please execute this task",
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

        print("\n🔧 Building LangGraph...")

        # Build the graph
        from langgraph.checkpoint.memory import MemorySaver

        checkpointer = MemorySaver()
        graph = engine._build_langgraph(canvas_data, execution_id=999, checkpointer=checkpointer)

        print("✅ LangGraph built successfully")
        print(f"📈 Graph type: {type(graph)}")

        # Test graph execution
        print("\n🚀 Testing graph execution...")

        initial_state = WorkflowState(execution_id=999, node_outputs={}, completed_nodes=[], error=None)

        config = {"configurable": {"thread_id": "test_workflow_999"}}

        print(f"📊 Initial state: {initial_state}")
        print(f"⚙️  Config: {config}")

        # Test streaming execution
        print("\n🌊 Testing streaming execution...")
        chunk_count = 0
        final_state = None

        try:
            async for chunk in graph.astream(initial_state, config):
                chunk_count += 1
                print(f"📦 Chunk #{chunk_count}: {chunk}")

                if chunk:
                    # LangGraph returns chunks with node_id as key, containing the state update
                    for node_id, state_update in chunk.items():
                        print(f"   🔍 Node {node_id} update: {state_update}")
                        if isinstance(state_update, dict):
                            final_state = state_update
                            completed = len(state_update.get("completed_nodes", []))
                            print(f"   ✅ Nodes completed so far: {completed}")

                            # Show node outputs
                            node_outputs = state_update.get("node_outputs", {})
                            if node_outputs:
                                print(f"   📊 Node outputs keys: {list(node_outputs.keys())}")
                                for output_node_id, output in node_outputs.items():
                                    print(
                                        f"   🔍 {output_node_id}: {output.get('type', 'unknown')} - "
                                        f"{output.get('response', 'no response')[:50]}..."
                                    )
                else:
                    print(f"   ⚠️  Empty chunk #{chunk_count}")

            print(f"\n🏁 Streaming completed with {chunk_count} chunks")

            if final_state:
                completed_nodes = final_state.get("completed_nodes", [])
                print(f"✅ Final completed nodes: {completed_nodes}")
                print(f"📊 Final node outputs: {list(final_state.get('node_outputs', {}).keys())}")

                if len(completed_nodes) > 0:
                    print("🎉 SUCCESS: Nodes were executed!")
                    return True
                else:
                    print("❌ ISSUE: No nodes were executed")
                    return False
            else:
                print("❌ ISSUE: No final state received")
                return False

        except Exception as e:
            print(f"❌ Error during streaming execution: {e}")
            import traceback

            traceback.print_exc()
            return False

    except Exception as e:
        print(f"❌ Error in workflow setup: {e}")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(debug_full_workflow())
    if success:
        print("\n✅ Full workflow debug completed successfully!")
    else:
        print("\n❌ Full workflow debug found issues!")
