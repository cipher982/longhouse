"""
Simple test to verify LangGraph workflow engine integration.
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.append(str(Path(__file__).parent))

from zerg.services.langgraph_workflow_engine import langgraph_workflow_engine


async def test_simple_workflow():
    """Test that the workflow engine can execute a simple workflow."""

    print("🧪 Testing LangGraph Workflow Engine")
    print("=" * 40)

    # Simple test canvas data
    canvas_data = {
        "nodes": [{"id": "start", "type": "placeholder"}, {"id": "end", "type": "placeholder"}],
        "edges": [{"source": "start", "target": "end"}],
    }

    try:
        # Test graph building
        graph = langgraph_workflow_engine._build_langgraph(canvas_data, execution_id=1)
        print("✅ Graph built successfully")

        # Test execution (mock)
        from zerg.services.langgraph_workflow_engine import WorkflowState

        initial_state = WorkflowState(
            execution_id=1,
            node_outputs={},
            completed_nodes=[],
            error=None,
            db_session_factory=None,  # Mock
        )

        final_state = await graph.ainvoke(initial_state)

        assert len(final_state["node_outputs"]) == 2
        assert "start" in final_state["node_outputs"]
        assert "end" in final_state["node_outputs"]

        print("✅ Workflow executed successfully")
        print(f"✅ Completed nodes: {list(final_state['node_outputs'].keys())}")

        return True

    except Exception as e:
        print(f"❌ Test failed: {e}")
        return False


async def test_api_import():
    """Test that the API can import the LangGraph engine."""

    print("\n🧪 Testing API Integration")
    print("=" * 40)

    try:
        # Test that the router can import everything
        from zerg.routers.workflow_executions import langgraph_workflow_engine

        print("✅ Router imports LangGraph engine")
        print(f"✅ Engine type: {type(langgraph_workflow_engine).__name__}")

        return True

    except ImportError as e:
        print(f"❌ Import failed: {e}")
        return False


async def main():
    """Run workflow tests."""

    print("🚀 LangGraph Integration Test")
    print("=" * 50)

    tests = [
        test_simple_workflow,
        test_api_import,
    ]

    all_passed = True

    for test in tests:
        try:
            result = await test()
            if not result:
                all_passed = False
        except Exception as e:
            print(f"❌ Test {test.__name__} failed: {e}")
            all_passed = False

    print("\n" + "=" * 50)
    if all_passed:
        print("🎉 All tests PASSED!")
        print("✅ LangGraph engine is working")
        print("✅ API integration is clean")
        print("\n🚀 Ready to use!")
    else:
        print("💥 Some tests FAILED!")

    return all_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
