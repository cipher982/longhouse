use wasm_bindgen_test::*;

use crate::messages::Message;
use crate::models::ApiWorkflow;
use crate::state::AppState;

wasm_bindgen_test_configure!(run_in_browser);

fn count_triggers(state: &AppState) -> usize {
    state
        .workflow_nodes
        .values()
        .filter(|n| matches!(n.get_semantic_type(), crate::models::NodeType::Trigger { .. }))
        .count()
}

fn empty_workflow(id: u32, name: &str) -> ApiWorkflow {
    ApiWorkflow {
        id,
        owner_id: 0,
        name: name.to_string(),
        description: Some("test".to_string()),
        canvas: serde_json::json!({ "nodes": [], "edges": [] }),
        is_active: true,
        created_at: None,
        updated_at: None,
    }
}

#[wasm_bindgen_test]
fn no_frontend_trigger_creation_on_workflow_load() {
    let mut state = AppState::new();

    // Phase 2: Frontend no longer creates triggers - backend provides complete workflows
    let wf = empty_workflow(1, "wf-load");
    crate::update::update(&mut state, Message::CurrentWorkflowLoaded(wf.clone()));
    assert_eq!(count_triggers(&state), 0, "no triggers created by frontend (backend provides via templates)");

    crate::update::update(&mut state, Message::CurrentWorkflowLoaded(wf));
    assert_eq!(count_triggers(&state), 0, "still no frontend trigger creation after reload");
}

#[wasm_bindgen_test]
fn default_trigger_added_on_select_workflow_once() {
    let mut state = AppState::new();

    let wf = empty_workflow(2, "wf-select");
    state.workflows.insert(wf.id, wf);

    crate::update::update(&mut state, Message::SelectWorkflow { workflow_id: 2 });
    assert_eq!(count_triggers(&state), 0, "no triggers added by SelectWorkflow (moved to CurrentWorkflowLoaded)");

    crate::update::update(&mut state, Message::SelectWorkflow { workflow_id: 2 });
    assert_eq!(count_triggers(&state), 0, "still no triggers after reselection");
}

#[wasm_bindgen_test]
fn no_race_condition_with_backend_templates() {
    let mut state = AppState::new();

    // Phase 2: No race condition possible since frontend doesn't create triggers
    let wf = empty_workflow(3, "wf-race");
    
    // SelectWorkflow: No trigger creation (as expected)
    state.workflows.insert(wf.id, wf.clone());
    crate::update::update(&mut state, Message::SelectWorkflow { workflow_id: 3 });
    assert_eq!(count_triggers(&state), 0, "SelectWorkflow never creates triggers");
    
    // CurrentWorkflowLoaded: No trigger creation (backend provides complete workflows)
    crate::update::update(&mut state, Message::CurrentWorkflowLoaded(wf.clone()));
    assert_eq!(count_triggers(&state), 0, "CurrentWorkflowLoaded doesn't create triggers (backend templates)");
    
    // Rapid sequence: Still no triggers (race condition eliminated)
    crate::update::update(&mut state, Message::SelectWorkflow { workflow_id: 3 });
    crate::update::update(&mut state, Message::CurrentWorkflowLoaded(wf));
    assert_eq!(count_triggers(&state), 0, "no race condition possible with backend templates");
}

