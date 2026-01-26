#!/usr/bin/env python3
"""
Debug script to test the fiche node execution logic without dependencies.
"""


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


def debug_potential_issues():
    """Debug potential issues that could cause 0 nodes executed."""

    print("\n🔍 Potential Issues Analysis")
    print("=" * 50)

    issues = [
        "1. Fiche ID 3 doesn't exist in the database",
        "2. Database connection fails during execution",
        "3. FicheRunner.run_thread() throws an exception",
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
    print("1. Add more logging to the fiche node function")
    print("2. Check if fiche ID 3 exists in the database")
    print("3. Verify the LangGraph execution actually calls nodes")
    print("4. Add try-catch around the entire execution to see exceptions")
    print("5. Test with a simpler placeholder node instead of fiche")


if __name__ == "__main__":
    success = debug_fiche_node_logic()
    debug_potential_issues()

    if success:
        print("\n✅ Fiche node logic looks correct!")
        print("🤔 The issue is likely in execution flow, not node logic")
    else:
        print("\n❌ Found issues in fiche node logic!")
