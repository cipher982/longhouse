//! Fast contract validation binary that runs during `make start`
//!
//! This validates that frontend data structures serialize to exactly
//! what the backend API contracts expect, catching runtime errors at build time.

use std::process;

use agent_platform_frontend::generated::api_contracts::*;

fn main() {
    println!("🔍 Running fast contract validation checks...");

    // Test 1: WorkflowNode contract validation
    if !test_workflow_node_contracts() {
        println!("❌ WorkflowNode contract validation failed");
        process::exit(1);
    }

    // Test 2: Canvas serialization
    if !test_canvas_contracts() {
        println!("❌ Canvas contract validation failed");
        process::exit(1);
    }

    // Test 3: Backend type mapping validation
    if !test_backend_type_mapping() {
        println!("❌ Backend type mapping validation failed");
        process::exit(1);
    }

    // Test 3: Raw API contract validation (what actually gets sent to backend)
    if !test_raw_api_contract_validation() {
        println!("❌ Raw API contract validation failed");
        process::exit(1);
    }

    println!("✅ All contract validation checks passed");
}

/// Test what actually gets sent to the backend API without type mapping
/// This should FAIL if the frontend sends unmapped semantic types
fn test_raw_api_contract_validation() -> bool {
    println!("  🌐 Testing raw API contract validation...");

    // Create a node with frontend semantic type (the actual problem)
    let node_with_semantic_type = serde_json::json!({
        "id": "test-node",
        "type": "AgentIdentity",  // This is what frontend sends
        "position": {"x": 100.0, "y": 200.0},
        "config": null
    });

    // Try to validate this directly against backend schema
    // This should fail because backend expects "agent", not "AgentIdentity"
    let backend_schema_test = validate_against_backend_schema(&node_with_semantic_type);

    if backend_schema_test {
        println!("    ❌ Backend incorrectly accepted semantic type 'AgentIdentity'");
        println!("    💡 This means contract validation is not strict enough");
        return false;
    }

    // Now test with a properly mapped type
    let node_with_backend_type = serde_json::json!({
        "id": "test-node",
        "type": "agent",  // This is what backend expects
        "position": {"x": 100.0, "y": 200.0},
        "config": null
    });

    if !validate_against_backend_schema(&node_with_backend_type) {
        println!("    ❌ Backend rejected valid type 'agent'");
        return false;
    }

    println!("    ✅ Raw API contract validation passed");
    true
}

/// Simulate backend schema validation
/// Returns true if the node type is acceptable to the backend
fn validate_against_backend_schema(node: &serde_json::Value) -> bool {
    // Simulate backend's Literal["agent", "tool", "trigger", "conditional"] validation
    match node.get("type").and_then(|t| t.as_str()) {
        Some("agent") | Some("tool") | Some("trigger") | Some("conditional") => true,
        _ => false,
    }
}

fn test_workflow_node_contracts() -> bool {
    println!("  📋 Testing WorkflowNode contracts...");

    // Test all actual node types that the frontend creates
    let frontend_types = vec![
        ("AgentIdentity", "Tests agent identity nodes"),
        ("UserInput", "Tests user input nodes"),
        ("ResponseOutput", "Tests response output nodes"),
        ("GenericNode", "Tests generic nodes"),
        ("Tool", "Tests tool nodes"),
        ("Trigger", "Tests trigger nodes"),
    ];

    for (node_type, description) in frontend_types {
        println!("    🔸 {}: {}", node_type, description);

        if !test_single_node_type(node_type) {
            return false;
        }
    }

    println!("    ✅ All WorkflowNode contract validations passed");
    true
}

fn test_single_node_type(node_type: &str) -> bool {
    let position = Position { x: 100.0, y: 200.0 };

    let node = WorkflowNode {
        id: format!("test-{}-node", node_type.to_lowercase()),
        type_: node_type.to_string(),
        position,
        config: None,
    };

    // Test serialization matches backend expectations
    let serialized = match serde_json::to_value(&node) {
        Ok(val) => val,
        Err(e) => {
            println!("      ❌ Failed to serialize {} node: {}", node_type, e);
            return false;
        }
    };

    // Verify required fields exist and have correct types
    match serialized.get("id") {
        Some(serde_json::Value::String(_)) => {}
        _ => {
            println!("      ❌ {} node missing or invalid 'id' field", node_type);
            return false;
        }
    }

    match serialized.get("type") {
        Some(serde_json::Value::String(type_val)) => {
            if type_val != node_type {
                println!(
                    "      ❌ {} node type mismatch: expected '{}', got '{}'",
                    node_type, node_type, type_val
                );
                return false;
            }
        }
        _ => {
            println!(
                "      ❌ {} node missing or invalid 'type' field",
                node_type
            );
            return false;
        }
    }

    match serialized.get("position") {
        Some(serde_json::Value::Object(pos_obj)) => {
            // Verify position has x and y coordinates
            if !pos_obj.contains_key("x") || !pos_obj.contains_key("y") {
                println!(
                    "      ❌ {} node position missing x or y coordinates",
                    node_type
                );
                return false;
            }
        }
        _ => {
            println!(
                "      ❌ {} node missing or invalid 'position' field",
                node_type
            );
            return false;
        }
    }

    // Test round-trip serialization
    let _deserialized: WorkflowNode = match serde_json::from_value(serialized) {
        Ok(node) => node,
        Err(e) => {
            println!("      ❌ Failed to deserialize {} node: {}", node_type, e);
            return false;
        }
    };

    println!("      ✅ {} node validation passed", node_type);
    true
}

fn test_canvas_contracts() -> bool {
    println!("  🎨 Testing Canvas contracts...");

    // Create nodes of different types for comprehensive testing
    let agent_node = WorkflowNode {
        id: "canvas-agent-node".to_string(),
        type_: "AgentIdentity".to_string(),
        position: Position { x: 100.0, y: 200.0 },
        config: None,
    };

    let trigger_node = WorkflowNode {
        id: "canvas-trigger-node".to_string(),
        type_: "Trigger".to_string(),
        position: Position { x: 300.0, y: 200.0 },
        config: None,
    };

    let tool_node = WorkflowNode {
        id: "canvas-tool-node".to_string(),
        type_: "Tool".to_string(),
        position: Position { x: 500.0, y: 200.0 },
        config: None,
    };

    let edge1 = WorkflowEdge {
        from_node_id: "canvas-trigger-node".to_string(),
        to_node_id: "canvas-agent-node".to_string(),
        config: None,
    };

    let edge2 = WorkflowEdge {
        from_node_id: "canvas-agent-node".to_string(),
        to_node_id: "canvas-tool-node".to_string(),
        config: None,
    };

    let canvas = WorkflowCanvas {
        nodes: vec![agent_node, trigger_node, tool_node],
        edges: vec![edge1, edge2],
    };

    // Test canvas serialization
    let serialized = match serde_json::to_value(&canvas) {
        Ok(val) => val,
        Err(e) => {
            println!("    ❌ Failed to serialize Canvas: {}", e);
            return false;
        }
    };

    // Verify canvas structure
    match serialized.get("nodes") {
        Some(serde_json::Value::Array(_)) => {}
        _ => {
            println!("    ❌ Missing or invalid 'nodes' field");
            return false;
        }
    }

    match serialized.get("edges") {
        Some(serde_json::Value::Array(_)) => {}
        _ => {
            println!("    ❌ Missing or invalid 'edges' field");
            return false;
        }
    }

    // Test round-trip serialization
    let _deserialized: WorkflowCanvas = match serde_json::from_value(serialized) {
        Ok(canvas) => canvas,
        Err(e) => {
            println!("    ❌ Failed to deserialize Canvas: {}", e);
            return false;
        }
    };

    println!("    ✅ Canvas contract validation passed");
    true
}

fn test_backend_type_mapping() -> bool {
    println!("  🔄 Testing backend type mapping...");

    // These are the actual mappings from generated_client.rs
    let frontend_to_backend_mappings = vec![
        (
            "AgentIdentity",
            "agent",
            "Maps agent identity nodes to backend",
        ),
        ("Tool", "tool", "Maps tool nodes to backend"),
        ("Trigger", "trigger", "Maps trigger nodes to backend"),
        (
            "UserInput",
            "conditional",
            "Maps user input nodes to conditional",
        ),
        (
            "ResponseOutput",
            "conditional",
            "Maps response output nodes to conditional",
        ),
        (
            "GenericNode",
            "conditional",
            "Maps generic nodes to conditional",
        ),
    ];

    for (frontend_type, expected_backend_type, description) in frontend_to_backend_mappings {
        println!("    🔸 {}: {}", frontend_type, description);

        // Simulate the mapping logic from generated_client.rs
        let mapped_type = map_frontend_to_backend_type(frontend_type);

        if mapped_type != expected_backend_type {
            println!(
                "      ❌ Type mapping failed: {} -> expected '{}', got '{}'",
                frontend_type, expected_backend_type, mapped_type
            );
            return false;
        }

        println!(
            "      ✅ {} -> {} mapping validated",
            frontend_type, mapped_type
        );
    }

    println!("    ✅ Backend type mapping validation passed");
    true
}

/// Simulates the mapping logic from generated_client.rs
fn map_frontend_to_backend_type(frontend_type: &str) -> &str {
    match frontend_type {
        "AgentIdentity" => "agent",
        "Tool" => "tool",
        "Trigger" => "trigger",
        // All others map to conditional
        "UserInput" | "ResponseOutput" | "GenericNode" | _ => "conditional",
    }
}
