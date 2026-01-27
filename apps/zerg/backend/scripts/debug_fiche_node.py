#!/usr/bin/env python3
"""
Debug script to test the fiche node execution logic without full LangGraph.
"""

import sys
from pathlib import Path

# Add backend to path
sys.path.append(str(Path(__file__).parent))

from zerg.services.langgraph_workflow_engine import WorkflowState


def debug_fiche_node_logic():
    """Debug the fiche node creation logic."""

    print("🐛 Debugging Fiche Node Logic")
    print("=" * 50)

    # Simulate the node_config from canvas_data
    node_config = {
        "fiche_id": 3,
        "color": "#2ecc71",
        "height": 80.0,
        "is_dragging": False,
        "is_selected": False,
        "node_id": "node_0",
        "node_type": "ficheidentity",
        "parent_id": None,
        "text": "New Fiche 81",
        "width": 200.0,
        "x": 280.0,
        "y": 178.0,
    }

    print(f"📊 Node config: {node_config}")

    # Test the fiche node creation logic
    node_id = str(node_config.get("node_id", "unknown"))
    fiche_id = node_config.get("fiche_id")

    print(f"📊 Extracted node_id: {node_id}")
    print(f"📊 Extracted fiche_id: {fiche_id}")

    # Check if this is correct for the fiche node function
    if not fiche_id:
        print("❌ ISSUE FOUND: fiche_id is None or missing!")
        print("   The fiche node function would fail because it can't find the fiche")
        return False

    print("✅ fiche_id extracted successfully")

    # Check message extraction
    user_message = node_config.get("message", "Execute this task")
    print(f"📊 User message: {user_message}")

    print("\n🔍 Fiche Node Function Analysis:")
    print(f"   1. node_id: {node_id} ✅")
    print(f"   2. fiche_id: {fiche_id} ✅")
    print(f"   3. user_message: {user_message} ✅")
    print("   4. Would query database for fiche...")
    print("   5. Would create thread and run fiche...")
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

    # Test state update like the fiche node would return
    state_update = {"node_outputs": {"node_0": {"fiche_id": 3, "type": "fiche"}}, "completed_nodes": ["node_0"]}

    print(f"📊 Expected state update: {state_update}")

    return True


if __name__ == "__main__":
    success1 = debug_fiche_node_logic()
    success2 = debug_workflow_state()

    if success1 and success2:
        print("\n✅ Fiche node logic looks correct!")
        print("🤔 The issue might be:")
        print("   1. Fiche ID 3 doesn't exist in the database")
        print("   2. Database connection issues")
        print("   3. Exception in FicheRunner.run_thread()")
        print("   4. LangGraph not calling the node function")
        print("   5. Silent exception handling somewhere")
    else:
        print("\n❌ Found issues in fiche node logic!")
