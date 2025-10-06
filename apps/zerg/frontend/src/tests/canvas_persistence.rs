//! WASM unit test that reproduces the “missing SaveState after node-drop”
//! regression.  It simulates the minimal sequence of reducer messages a user
//! produces when they drag an agent card onto the canvas and release the
//! mouse (StopDragging) followed by the next animation frame
//! (AnimationTick).
//!
//! The current bug means no `Command::SaveState` is queued, therefore the
//! assertion below **fails** on the buggy codebase.  Once the persistence
//! logic is fixed, the test will pass and guard against regressions.

use wasm_bindgen_test::*;

use agent_platform_frontend::state::AppState;
use agent_platform_frontend::messages::{Message, Command};
use agent_platform_frontend::models::NodeType;

wasm_bindgen_test_configure!(run_in_browser);

#[wasm_bindgen_test]
fn save_state_is_queued_after_node_drop() {
    // 1. fresh in-memory state
    let mut state = AppState::new();

    // 2. Simulate the Drag-&-Drop sequence a user performs when adding an
    //    agent to the canvas.
    agent_platform_frontend::update::update(&mut state, Message::AddCanvasNode {
        agent_id: Some(123),
        x: 100.0,
        y: 100.0,
        node_type: NodeType::AgentIdentity,
        text: "Agent123".into(),
    });

    // User releases the mouse button -> StopDragging message
    agent_platform_frontend::update::update(&mut state, Message::StopDragging);

    // Next animation frame – reducer will decide whether to queue the
    // debounced save command.
    let cmds = agent_platform_frontend::update::update(&mut state, Message::AnimationTick);

    assert!(cmds.iter().any(|c| matches!(c, Command::SaveState)),
            "AnimationTick must queue Command::SaveState so layout persists");
}
