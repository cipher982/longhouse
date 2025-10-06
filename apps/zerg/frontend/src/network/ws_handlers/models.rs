use std::cell::RefCell;
use std::rc::Rc;
use wasm_bindgen::JsValue;
use crate::debug_log;
use crate::state;
use crate::models_config::ModelConfig;
use crate::network::messages::builders;
use crate::network::topic_manager::TopicHandler;

const MODELS_TOPIC: &str = "models";

/// Initialize model subscriptions and fetching via WebSockets
pub fn init_models_websocket() -> Result<(), JsValue> {
    // Get the websocket client and topic manager
    let (_, topic_manager_rc) = state::APP_STATE.with(|state_ref| {
        let state = state_ref.borrow();
        (state.ws_client.clone(), state.topic_manager.clone())
    });

    // Create a handler for model data
    let models_handler: TopicHandler = Rc::new(RefCell::new(move |data| {
        // Parse models from the WebSocket message data
        debug_log!("Received models: {:?}", data);

        if let Some(models_array) = data.as_array() {
            // Try to parse into ModelConfig objects
            let parsed_models: Vec<ModelConfig> = models_array
                .iter()
                .filter_map(|v| serde_json::from_value(v.clone()).ok())
                .collect();

            // Convert to format used by frontend
            let model_tuples = parsed_models
                .iter()
                .map(|m| (m.id.clone(), m.display_name.clone()))
                .collect();

            // Update app state with model data
            state::APP_STATE.with(|state| {
                let mut state = state.borrow_mut();
                state.available_models = model_tuples;

                // If no model is selected yet, select the first one
                if state.selected_model.is_empty() && !state.available_models.is_empty() {
                    state.selected_model = state.available_models[0].0.clone();
                }

                // Update UI if needed
                crate::state::AppState::refresh_ui_after_state_change().unwrap_or_else(|e| {
                    web_sys::console::error_1(&format!("Error refreshing UI: {:?}", e).into());
                });
            });
        }
    }));

    // Subscribe to the models topic
    {
        let mut topic_manager = topic_manager_rc.borrow_mut();
        topic_manager.subscribe(MODELS_TOPIC.to_string(), models_handler)?;
    }

    // Send a request for models data
    request_models()?;

    Ok(())
}

/// Send a request for models data via WebSocket
pub fn request_models() -> Result<(), JsValue> {
    let ws_client_rc = state::APP_STATE.with(|state| {
        let state = state.borrow();
        state.ws_client.clone()
    });

    // Create models request message
    let msg = builders::create_models_request();

    // Serialize and send
    let msg_json = serde_json::to_string(&msg)
        .map_err(|e| JsValue::from_str(&format!("Error serializing models request: {}", e)))?;

    // Send the message
    let ws_client = ws_client_rc.borrow();
    ws_client.send_serialized_message(&msg_json)?;

    debug_log!("Sent models request via WebSocket");

    Ok(())
}
