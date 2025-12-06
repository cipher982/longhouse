#!/usr/bin/env python3
"""Test what validation LangGraph provides out of the box."""

import logging
import sys
from pathlib import Path
from typing import Any, Dict

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from typing_extensions import TypedDict

# Simple state for testing
class TestState(TypedDict):
    value: str

def dummy_node(state: TestState) -> TestState:
    return {"value": state.get("value", "") + "_processed"}

def test_langgraph_validation():
    """Test various invalid graph scenarios to see what LangGraph catches."""

    print("Testing LangGraph validation capabilities...")

    test_cases = [
        ("valid_simple_graph", create_valid_graph),
        ("orphaned_node", create_orphaned_node_graph),
        ("circular_dependency", create_circular_graph),
        ("missing_start", create_no_start_graph),
        ("disconnected_components", create_disconnected_graph),
        ("empty_graph", create_empty_graph),
        ("invalid_edge_targets", create_invalid_edge_graph),
    ]

    results = {}

    for test_name, graph_creator in test_cases:
        print(f"\n--- Testing: {test_name} ---")
        try:
            graph = graph_creator()
            compiled = graph.compile(checkpointer=MemorySaver())
            print(f"✅ {test_name}: Compiled successfully")
            results[test_name] = "SUCCESS"
        except Exception as e:
            print(f"❌ {test_name}: Failed with error: {type(e).__name__}: {e}")
            results[test_name] = f"ERROR: {type(e).__name__}: {e}"

    print("\n=== SUMMARY ===")
    for test_name, result in results.items():
        print(f"{test_name}: {result}")

    return results

def create_valid_graph():
    """Create a valid graph: START -> node1 -> END"""
    graph = StateGraph(TestState)
    graph.add_node("node1", dummy_node)
    graph.add_edge(START, "node1")
    graph.add_edge("node1", END)
    return graph

def create_orphaned_node_graph():
    """Create graph with orphaned node not connected to anything"""
    graph = StateGraph(TestState)
    graph.add_node("node1", dummy_node)
    graph.add_node("orphaned", dummy_node)  # Not connected
    graph.add_edge(START, "node1")
    graph.add_edge("node1", END)
    return graph

def create_circular_graph():
    """Create graph with circular dependency: node1 -> node2 -> node1"""
    graph = StateGraph(TestState)
    graph.add_node("node1", dummy_node)
    graph.add_node("node2", dummy_node)
    graph.add_edge(START, "node1")
    graph.add_edge("node1", "node2")
    graph.add_edge("node2", "node1")  # Circular!
    graph.add_edge("node2", END)
    return graph

def create_no_start_graph():
    """Create graph without START connection"""
    graph = StateGraph(TestState)
    graph.add_node("node1", dummy_node)
    graph.add_node("node2", dummy_node)
    graph.add_edge("node1", "node2")
    graph.add_edge("node2", END)
    # Missing: graph.add_edge(START, "node1")
    return graph

def create_disconnected_graph():
    """Create graph with disconnected components"""
    graph = StateGraph(TestState)
    graph.add_node("node1", dummy_node)
    graph.add_node("node2", dummy_node)
    graph.add_node("node3", dummy_node)

    # Connected component 1
    graph.add_edge(START, "node1")
    graph.add_edge("node1", END)

    # Disconnected component 2
    graph.add_edge("node2", "node3")  # Not reachable from START
    return graph

def create_empty_graph():
    """Create completely empty graph"""
    return StateGraph(TestState)

def create_invalid_edge_graph():
    """Create graph with edges to non-existent nodes"""
    graph = StateGraph(TestState)
    graph.add_node("node1", dummy_node)
    graph.add_edge(START, "node1")
    graph.add_edge("node1", "nonexistent")  # Invalid target
    return graph

if __name__ == "__main__":
    # Suppress LangGraph logging for cleaner output
    logging.getLogger("langgraph").setLevel(logging.WARNING)

    test_langgraph_validation()
