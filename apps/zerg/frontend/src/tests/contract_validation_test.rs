//! Contract validation tests that prevent runtime API failures
//!
//! These tests ensure that frontend data structures serialize to exactly
//! the format that backend API contracts expect. If these tests fail,
//! runtime errors like "Canvas data doesn't match contract" will occur.

use wasm_bindgen_test::*;

wasm_bindgen_test_configure!(run_in_browser);

use crate::generated::workflow::*;
use crate::network::generated_client::*;
use crate::models::*;
use crate::generated::api_contracts::{Agent, AgentStatus};

/// CRITICAL BUILD-TIME TEST: This test ensures ALL data serialization
/// matches API contracts. If this test fails, the contracts are broken
/// and runtime failures WILL occur.
#[wasm_bindgen_test]
    fn test_comprehensive_workflow_node_contract_validation() {
        // Create a real WorkflowNode as the frontend creates it
        let node = WorkflowNode::new_with_semantic_type("test_node".to_string(), NodeSemanticType::GenericNode);

        // Serialize it as JSON (this is what gets sent to backend)
        let serialized = serde_json::to_value(&node)
            .expect("WorkflowNode should serialize without errors");

        // This MUST succeed - if it fails, canvas will break at runtime
        let validated: WorkflowNodeContract = serde_json::from_value(serialized.clone())
            .expect("CRITICAL: WorkflowNode serialization does not match API contract! This will cause runtime canvas failures.");

        // Validate all required fields are present and correctly typed
        assert!(!validated.id.is_empty(), "Node ID must not be empty");
        assert!(!validated.node_type.is_empty(), "Node type must not be empty");

        // Verify position field is always present and has correct structure
        // This catches the "missing field position" error
        assert!(validated.position.x >= 0.0, "Position X must be valid");
        assert!(validated.position.y >= 0.0, "Position Y must be valid");

        println!("✅ WorkflowNode contract validation PASSED");
    }

#[wasm_bindgen_test]
fn test_comprehensive_workflow_canvas_contract_validation() {
        // Create a real WorkflowCanvas with nodes and edges
        let mut canvas = WorkflowCanvas::default();

        // Add a real node
        let node = WorkflowNode::new_with_semantic_type("node1".to_string(), NodeSemanticType::GenericNode);
        canvas.nodes.push(node);

        // Add a real edge
        let edge = WorkflowEdge {
            from_node_id: "trigger".to_string(),
            to_node_id: "node1".to_string(),
            config: serde_json::Map::new(),
        };
        canvas.edges.push(edge);

        // Serialize the full canvas
        let serialized = serde_json::to_value(&canvas)
            .expect("WorkflowCanvas should serialize without errors");

        // Create the data structure that the API expects
        let api_payload = serde_json::json!({
            "edges": canvas.edges,
            "nodes": canvas.nodes
        });

        // This MUST succeed - if it fails, canvas API calls will break
        let validated: WorkflowDataContract = serde_json::from_value(api_payload)
            .expect("CRITICAL: Canvas data does not match API contract! This will cause 'Canvas data doesn't match contract' errors.");

        // Validate the structure
        assert_eq!(validated.nodes.len(), 1, "Node count must match");
        assert_eq!(validated.edges.len(), 1, "Edge count must match");

        // Validate edge field naming consistency
        assert_eq!(validated.edges[0].from_node_id, "trigger", "Edge from_node_id must use consistent naming");
        assert_eq!(validated.edges[0].to_node_id, "node1", "Edge to_node_id must use consistent naming");

        println!("✅ WorkflowCanvas contract validation PASSED");
    }

/// CRITICAL: Test AgentStatus enum serialization matches backend API
#[wasm_bindgen_test]
fn test_agent_status_enum_contract_validation() {
    // Test all valid AgentStatus values serialize correctly
    let statuses = vec![
        AgentStatus::Idle,
        AgentStatus::Running,
        AgentStatus::Error,
        AgentStatus::Processing,
    ];

    for status in statuses {
        let serialized = serde_json::to_value(&status)
            .expect("AgentStatus should serialize without errors");

        // Must deserialize back to the same enum variant
        let deserialized: AgentStatus = serde_json::from_value(serialized.clone())
            .expect("CRITICAL: AgentStatus serialization contract broken! This will cause agent API failures.");

        assert_eq!(status, deserialized, "AgentStatus round-trip must be consistent");
    }

    // Test Agent with status field
    let agent = Agent {
        id: 123,
        name: "Test Agent".to_string(),
        status: AgentStatus::Running,
        system_instructions: "Test".to_string(),
        task_instructions: Some("Task".to_string()),
        model: Some("gpt-4".to_string()),
        created_at: None,
        updated_at: None,
    };

    let serialized = serde_json::to_value(&agent)
        .expect("Agent should serialize without errors");

    // Validate status field is correctly serialized
    assert_eq!(serialized["status"], "running", "Agent.status must serialize to correct string");

    println!("✅ AgentStatus contract validation PASSED");
}

/// This test catches field name mismatches that cause contract validation to fail
#[wasm_bindgen_test]
fn test_field_name_consistency() {
        let node = WorkflowNode::new_with_semantic_type("test".to_string(), NodeSemanticType::GenericNode);
        let serialized = serde_json::to_value(&node).unwrap();

        // The serialized node MUST have these exact field names to match backend expectations
        assert!(serialized.get("id").is_some(), "Node must serialize with 'id' field (not 'node_id')");
        assert!(serialized.get("type").is_some(), "Node must serialize with 'type' field (not 'node_type')");
        assert!(serialized.get("position").is_some(), "Node must serialize with 'position' field");

        // Verify position has correct structure
        let position = serialized.get("position").unwrap();
        assert!(position.get("x").is_some(), "Position must have 'x' field");
        assert!(position.get("y").is_some(), "Position must have 'y' field");

        println!("✅ Field name consistency validation PASSED");
    }

/// Test that catches type mismatches between frontend types and contract expectations
#[wasm_bindgen_test]
fn test_type_consistency() {
        let node = WorkflowNode::new_with_semantic_type("test".to_string(), NodeSemanticType::GenericNode);
        let serialized = serde_json::to_value(&node).unwrap();

        // Verify the types match exactly what the contract expects
        let id = serialized.get("id").unwrap();
        assert!(id.is_string(), "ID must be string type");

        let node_type = serialized.get("type").unwrap();
        assert!(node_type.is_string(), "Type must be string type");

        let position = serialized.get("position").unwrap();
        assert!(position.is_object(), "Position must be object type");

        let x = position.get("x").unwrap();
        let y = position.get("y").unwrap();
        assert!(x.is_f64(), "Position X must be f64");
        assert!(y.is_f64(), "Position Y must be f64");

        println!("✅ Type consistency validation PASSED");
}