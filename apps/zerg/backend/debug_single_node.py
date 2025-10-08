#!/usr/bin/env python3
"""
Debug script to understand why single nodes aren't executing.
Uses the exact canvas_data structure from the logs.
"""

import sys
from pathlib import Path

# Add backend to path
sys.path.append(str(Path(__file__).parent))


def debug_single_node():
    """Debug a single AgentIdentity node with no edges."""

    print("🐛 Debugging Single Node Execution")
    print("=" * 50)

    # Exact canvas_data from the logs
    canvas_data = {
        "edges": [],
        "nodes": [
            {
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
        ],
    }

    print(f"📊 Input canvas_data: {canvas_data}")

    # Simulate the graph building process
    nodes = canvas_data.get("nodes", [])
    edges = canvas_data.get("edges", [])

    print(f"📊 Extracted nodes: {nodes}")
    print(f"📊 Extracted edges: {edges}")

    # Get all valid node IDs for validation
    valid_node_ids = {str(node.get("node_id", "unknown")) for node in nodes}
    print(f"📊 Valid node IDs: {valid_node_ids}")

    # Find nodes with no incoming edges (start nodes)
    target_nodes = {str(edge.get("to_node_id", "")) for edge in edges}
    source_nodes = {str(edge.get("from_node_id", "")) for edge in edges}

    print(f"📊 Target nodes (have incoming edges): {target_nodes}")
    print(f"📊 Source nodes (have outgoing edges): {source_nodes}")

    start_nodes = []
    end_nodes = []

    for node in nodes:
        node_id = str(node.get("node_id", "unknown"))
        if node_id not in target_nodes:
            start_nodes.append(node_id)
        if node_id not in source_nodes:
            end_nodes.append(node_id)

    print(f"📊 Start nodes (no incoming edges): {start_nodes}")
    print(f"📊 End nodes (no outgoing edges): {end_nodes}")

    # Analyze the graph structure that would be built
    print("\n🏗️  Graph Structure Analysis:")
    print(f"   START will connect to: {start_nodes}")
    print(f"   {end_nodes} will connect to END")

    if len(start_nodes) == 1 and len(end_nodes) == 1 and start_nodes[0] == end_nodes[0]:
        print("✅ Single node should form: START -> node_0 -> END")
        print("✅ This should execute the node!")
    else:
        print("❌ Unexpected graph structure")

    # Check node type handling
    for node in nodes:
        node_id = str(node.get("node_id", "unknown"))
        node_type = str(node.get("node_type", "unknown")).lower()

        print(f"\n🔍 Node {node_id}:")
        print(f"   Type: {node_type}")
        print(f"   Agent ID: {node.get('agent_id')}")

        if node_type == "agentidentity" or node_type == "agent":
            print("   ✅ Would create agent node")
        elif node_type == "tool":
            print("   ✅ Would create tool node")
        elif node_type == "trigger":
            print("   ✅ Would create trigger node")
        else:
            print("   ⚠️  Would create placeholder node")


if __name__ == "__main__":
    debug_single_node()
