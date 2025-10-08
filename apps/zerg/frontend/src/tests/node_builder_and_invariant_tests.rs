use wasm_bindgen_test::*;

use crate::models::{NodeType, TriggerConfig, TriggerType};
use crate::state::AppState;
use crate::components::node_palette::PaletteNode;

wasm_bindgen_test_configure!(run_in_browser);

#[wasm_bindgen_test]
fn builder_creates_fully_typed_trigger() {
    use crate::node_builder::NodeBuilder;

    let node = NodeBuilder::from_semantic(NodeType::Trigger {
        trigger_type: TriggerType::Webhook,
        config: TriggerConfig { params: Default::default(), enabled: true, filters: vec![] },
    })
    .id("t1".to_string())
    .at(10.0, 20.0)
    .label("Webhook Trigger")
    .build();

    match node.get_semantic_type() {
        NodeType::Trigger { trigger_type, .. } => {
            assert!(matches!(trigger_type, TriggerType::Webhook));
        }
        _ => panic!("expected Trigger node from builder"),
    }
}

#[wasm_bindgen_test]
fn invariant_prevents_second_manual_trigger_via_palette() {
    // Prepare state with one manual trigger already present
    let mut state = AppState::new();

    let manual_node = PaletteNode {
        id: "manual1".to_string(),
        name: "Manual".to_string(),
        description: "Manual trigger".to_string(),
        icon: "🖐".to_string(),
        category: "Triggers".to_string(),
        node_type: NodeType::Trigger {
            trigger_type: TriggerType::Manual,
            config: TriggerConfig { params: Default::default(), enabled: true, filters: vec![] },
        },
    };

    // First add is allowed
    crate::components::node_palette::create_node_from_palette(&mut state, &manual_node, 100.0, 100.0);

    // Second add should be rejected by invariant – count remains 1
    crate::components::node_palette::create_node_from_palette(&mut state, &manual_node, 200.0, 200.0);

    let count = state
        .workflow_nodes
        .values()
        .filter(|n| matches!(n.get_semantic_type(), NodeType::Trigger { trigger_type: TriggerType::Manual, .. }))
        .count();
    assert_eq!(count, 1, "Only one manual trigger permitted");
}

