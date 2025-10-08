use wasm_bindgen_test::*;

use crate::models::{NodeType, TriggerConfig, TriggerType, WorkflowNode};

wasm_bindgen_test_configure!(run_in_browser);

#[wasm_bindgen_test]
fn trigger_meta_round_trip_preserves_type_and_params() {
    // Build a trigger node with non-default subtype + params
    let mut params = std::collections::HashMap::new();
    params.insert("cron".to_string(), serde_json::json!("0 12 * * *"));

    let node_type = NodeType::Trigger {
        trigger_type: TriggerType::Schedule,
        config: TriggerConfig { params, enabled: true, filters: vec![] },
    };

    let node = WorkflowNode::new_with_type("trig1".to_string(), &node_type);

    // Serialize/deserialize through canonical path
    let json = serde_json::to_string(&node).expect("serialize node");
    let deserialized: WorkflowNode = serde_json::from_str(&json).expect("deserialize node");

    // Verify semantic type and params survive round-trip
    match deserialized.get_semantic_type() {
        NodeType::Trigger { trigger_type, config } => {
            assert!(matches!(trigger_type, TriggerType::Schedule));
            assert_eq!(config.params.get("cron").and_then(|v| v.as_str()), Some("0 12 * * *"));
        }
        _ => panic!("expected Trigger node after round-trip"),
    }
}

#[wasm_bindgen_test]
fn palette_like_creation_preserves_subtype_after_visual_updates() {
    // Simulate palette-provided trigger node
    let node_type = NodeType::Trigger {
        trigger_type: TriggerType::Email,
        config: TriggerConfig { params: Default::default(), enabled: true, filters: vec![] },
    };

    // Create and apply visual updates (without replacing entire config)
    let mut node = WorkflowNode::new_with_type("trig2".to_string(), &node_type);
    node.apply_visual(100.0, 150.0, 200.0, 80.0, "#f59e0b", "Email Trigger");

    // Re-assert semantics (as palette flow does) and verify subtype remains Email
    node.set_semantic_type(&node_type);

    match node.get_semantic_type() {
        NodeType::Trigger { trigger_type, .. } => {
            assert!(matches!(trigger_type, TriggerType::Email));
        }
        _ => panic!("expected Trigger node after visual updates"),
    }
}

