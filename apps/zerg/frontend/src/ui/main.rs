use serde_json;
use wasm_bindgen::prelude::*;
use web_sys::Document;

// Removed: create_input_panel (canvas controls now in workflow bar)

// Removed: setup_toolbar_event_handlers (canvas controls now in workflow bar)

// Setup base UI components
pub fn setup_ui(document: &Document) -> Result<(), JsValue> {
    // Create agent modal component (shared between views)
    crate::components::agent_config_modal::AgentConfigModal::init(document)?;

    // Ensure workflows are loaded on initial app startup – especially when
    // landing directly on the Canvas page without going through the view
    // toggle reducer.
    wasm_bindgen_futures::spawn_local(async {
        match crate::network::api_client::ApiClient::get_workflows().await {
            Ok(json_str) => {
                if let Ok(api_wfs) =
                    serde_json::from_str::<Vec<crate::models::ApiWorkflow>>(&json_str)
                {
                    crate::state::dispatch_global_message(
                        crate::messages::Message::WorkflowsLoaded(api_wfs),
                    );
                }
            }
            Err(e) => {
                web_sys::console::error_1(
                    &format!("Failed to fetch workflows on startup: {:?}", e).into(),
                );
            }
        }
    });

    // Setup event handlers that are common across views
    crate::ui::events::setup_ui_event_handlers(document)?;

    Ok(())
}
