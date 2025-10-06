use crate::generated::ws_handlers::{DashboardHandler, DashboardMessageRouter};
use crate::generated::ws_messages::{
    AgentEventData, ExecutionFinishedData, NodeLogData, NodeStateData, RunUpdateData,
};
use crate::network::topic_manager::{ITopicManager, TopicHandler}; // Import Trait
use crate::state::APP_STATE;
use std::cell::RefCell;
use std::rc::Rc;
use wasm_bindgen::JsValue;
use crate::debug_log;

// Conversion from generated RunUpdateData to ApiAgentRun
impl From<RunUpdateData> for crate::models::ApiAgentRun {
    fn from(data: RunUpdateData) -> Self {
        crate::models::ApiAgentRun {
            id: data.id,
            agent_id: data.agent_id,
            thread_id: data.thread_id,
            status: data.status,
            trigger: data.trigger.unwrap_or_else(|| "manual".to_string()),
            started_at: data.started_at,
            finished_at: data.finished_at,
            duration_ms: data.duration_ms.map(|d| d as u64), // Convert u32 to u64
            total_tokens: None,                              // Not provided in generated schema
            total_cost_usd: None,                            // Not provided in generated schema
            error: data.error,
        }
    }
}

/// Convert a raw JSON payload coming from run_update into an ApiAgentRun.
/// The websocket messages are *slim* – many optional fields from the REST
/// schema are missing.  We fill sensible defaults so that the struct can be
/// used right away by the dashboard without forcing an extra REST reload.
// Helper: apply agent_event delta to AppState in-place
fn apply_agent_event(evt: AgentEventData) {
    crate::state::APP_STATE.with(|state_ref| {
        let mut state = state_ref.borrow_mut();
        if let Some(agent) = state.agents.get_mut(&evt.id) {
            if let Some(status) = evt.status {
                agent.status = Some(status);
            }
            if let Some(ts) = evt.last_run_at {
                agent.last_run_at = Some(ts);
            }
            if let Some(ts) = evt.next_run_at {
                agent.next_run_at = Some(ts);
            }
            if let Some(err) = evt.last_error {
                agent.last_error = if err.is_empty() { None } else { Some(err) };
            }
        }
    });

    // Optimistic UI refresh
    if let Err(e) = crate::state::AppState::refresh_ui_after_state_change() {
        web_sys::console::error_1(&format!("UI refresh error: {:?}", e).into());
    }
}

/// Manages WebSocket subscriptions and message handling for the Dashboard lifecycle.
pub struct DashboardWsManager {
    // No longer stores ws_client or topic_manager fields, uses globals from AppState.
    // Store the handler Rc to allow unsubscribing later.
    // Make pub(crate) for test access
    pub(crate) agent_subscription_handler: Option<TopicHandler>,
    // Store the message router for generated routing
    pub(crate) message_router: Option<DashboardMessageRouter<DashboardWsManager>>,
}

// Implement the generated DashboardHandler trait
impl DashboardHandler for DashboardWsManager {
    fn handle_run_update(&self, data: RunUpdateData) -> Result<(), JsValue> {
        let run_struct: crate::models::ApiAgentRun = data.into();
        let agent_id = run_struct.agent_id;

        crate::state::dispatch_global_message(crate::messages::Message::ReceiveRunUpdate {
            agent_id,
            run: run_struct,
        });

        // Note: Toast notifications are handled by the agent reducer to avoid duplication
        Ok(())
    }

    fn handle_agent_event(&self, data: AgentEventData) -> Result<(), JsValue> {
        apply_agent_event(data);
        Ok(())
    }

    fn handle_execution_finished(&self, data: ExecutionFinishedData) -> Result<(), JsValue> {
        crate::state::dispatch_global_message(crate::messages::Message::ExecutionFinished {
            execution_id: data.execution_id,
            phase: "finished".to_string(),
            result: data.result.clone(),
            error: data.error_message.clone(),
        });
        Ok(())
    }

    fn handle_node_state(&self, data: NodeStateData) -> Result<(), JsValue> {
        crate::state::dispatch_global_message(crate::messages::Message::UpdateNodeStatus {
            node_id: data.node_id.clone(),
            phase: data.phase.clone(),
            result: data.result.clone(),
        });
        Ok(())
    }

    fn handle_node_log(&self, data: NodeLogData) -> Result<(), JsValue> {
        crate::state::dispatch_global_message(crate::messages::Message::AppendExecutionLog {
            execution_id: data.execution_id,
            node_id: data.node_id.clone(),
            stream: data.stream.clone(),
            text: data.text.clone(),
        });
        Ok(())
    }

    fn handle_ops_event(&self, _data: crate::generated::ws_messages::OpsEventData) -> Result<(), JsValue> {
        // Placeholder: ops ticker integration will append to a rolling buffer (N=200)
        // For now, no-op to satisfy typed routing until Ops dashboard UI lands.
        Ok(())
    }
}

impl DashboardWsManager {
    /// Create a new DashboardWsManager instance.
    /// It doesn't hold the ws_client or topic_manager, assumes they are managed globally.
    pub fn new() -> Self {
        Self {
            agent_subscription_handler: None,
            message_router: None,
        }
    }

    /// Initialize subscriptions for the dashboard.
    /// Assumes global WebSocket is already connected or will connect.
    pub fn initialize(&mut self) -> Result<(), JsValue> {
        // Get the global TopicManager from AppState
        let topic_manager_rc = APP_STATE.with(|state_ref| {
            // We still need the Rc<RefCell<dyn ITopicManager>> here
            state_ref.borrow().topic_manager.clone() as Rc<RefCell<dyn ITopicManager>>
        });
        self.subscribe_to_agent_events(topic_manager_rc)?;
        Ok(())
    }

    /// Subscribe to agent-related events using the provided ITopicManager.
    // Change signature to accept the trait object
    pub(crate) fn subscribe_to_agent_events(
        &mut self,
        topic_manager_rc: Rc<RefCell<dyn ITopicManager>>,
    ) -> Result<(), JsValue> {
        // Get current agent IDs
        let agent_ids: Vec<u32> = crate::state::APP_STATE
            .with(|state_ref| state_ref.borrow().agents.keys().cloned().collect());

        // If we already have a handler, just subscribe new agents to it
        if let Some(existing_handler) = &self.agent_subscription_handler {
            debug_log!("DashboardWsManager: Using existing handler for new agents");

            let mut topic_manager = topic_manager_rc.borrow_mut();
            for aid in agent_ids {
                let topic = format!("agent:{}", aid);
                let cloned: TopicHandler = Rc::clone(&existing_handler) as TopicHandler;
                // This might create duplicates but TopicManager should handle that
                topic_manager.subscribe(topic, cloned)?;
            }
            return Ok(());
        }

        // Create new handler only if we don't have one
        debug_log!(
            "DashboardWsManager: Creating new handler with generated routing"
        );
        let mut topic_manager = topic_manager_rc.borrow_mut();

        // Create the message router using generated infrastructure
        let manager_rc = Rc::new(RefCell::new(DashboardWsManager::new()));
        let mut router = DashboardMessageRouter::new(manager_rc.clone());

        // Create envelope-based handler that uses the generated router with smart topic routing
        let handler = Rc::new(RefCell::new(move |data: serde_json::Value| {
            // Parse envelope first for proper topic-based routing
            if let Ok(envelope) =
                serde_json::from_value::<crate::generated::ws_messages::Envelope>(data.clone())
            {
                // Use generated topic routing to determine handler
                match crate::generated::ws_handlers::get_handler_for_topic(&envelope.topic) {
                    Some("dashboard") => {
                        // Dashboard messages: agent events, run updates, workflow execution
                        if let Err(e) = router.route_message(&envelope) {
                            web_sys::console::error_1(
                                &format!("Dashboard router error: {:?}", e).into(),
                            );
                        }
                    }
                    Some("chat") => {
                        // Thread messages handled by chat components - skip
                        debug_log!(
                            "Skipping chat message type: {}",
                            envelope.message_type
                        );
                        return;
                    }
                    Some("system") => {
                        // System messages (ping/pong/error) handled by WebSocket connection - skip
                        debug_log!(
                            "Skipping system message type: {}",
                            envelope.message_type
                        );
                        return;
                    }
                    Some(other) => {
                        // Other handler types - log and attempt routing anyway
                        web_sys::console::warn_1(
                            &format!(
                                "Unhandled handler type '{}' for topic: {} message type: {}",
                                other, envelope.topic, envelope.message_type
                            )
                            .into(),
                        );

                        // Fallback: try dashboard routing for unknown handlers
                        if let Err(e) = router.route_message(&envelope) {
                            web_sys::console::error_1(
                                &format!("Fallback router error: {:?}", e).into(),
                            );
                        }
                    }
                    None => {
                        // Unknown topic pattern - log and attempt routing anyway
                        web_sys::console::warn_1(
                            &format!(
                                "Unknown topic pattern: {} for message type: {}",
                                envelope.topic, envelope.message_type
                            )
                            .into(),
                        );

                        // Fallback: try dashboard routing for unknown patterns
                        if let Err(e) = router.route_message(&envelope) {
                            web_sys::console::error_1(
                                &format!("Fallback router error: {:?}", e).into(),
                            );
                        }
                    }
                }
            } else {
                web_sys::console::warn_1(
                    &format!("DashboardWsManager: Failed to parse envelope from WebSocket data")
                        .into(),
                );
            }
        }));

        self.agent_subscription_handler = Some(handler.clone());

        // Subscribe to each existing agent individually – backend does not
        // support wildcards for normal runtime traffic.
        for aid in agent_ids {
            let topic = format!("agent:{}", aid);
            let cloned: TopicHandler = Rc::clone(&handler) as TopicHandler;
            topic_manager.subscribe(topic, cloned)?;
        }
        Ok(())
    }

    /// Clean up WebSocket subscriptions for the dashboard.
    // Change signature to accept the trait object
    #[allow(dead_code)]
    pub fn cleanup(
        &mut self,
        topic_manager_rc: Rc<RefCell<dyn ITopicManager>>,
    ) -> Result<(), JsValue> {
        let mut topic_manager = topic_manager_rc.borrow_mut();

        if let Some(handler) = self.agent_subscription_handler.take() {
            debug_log!("DashboardWsManager: Cleaning up agent subscription handler");
            // We do not track which specific agent topics we subscribed to.
            // As a simple cleanup we iterate over the agents currently in
            // state and attempt to unsubscribe from their topics.
            let agent_ids: Vec<u32> = crate::state::APP_STATE
                .with(|state_ref| state_ref.borrow().agents.keys().cloned().collect());

            for aid in agent_ids {
                let topic = format!("agent:{}", aid);
                let _ = topic_manager.unsubscribe_handler(&topic, &handler);
            }
        } else {
            web_sys::console::warn_1(
                &"DashboardWsManager cleanup: No handler found to unsubscribe.".into(),
            );
        }
        Ok(())
    }

    // Test-specific helpers might need removal or adjustment as the manager no longer holds state.
    // #[cfg(test)]
    // pub fn get_connection_state(&self) -> String {
    //     // Cannot get state from self anymore
    // }

    // #[cfg(test)]
    // pub fn is_subscribed_to_topic(&self, topic: &str) -> bool {
    //    // Cannot check subscription state from self anymore
    //    // Need to check global TopicManager via AppState in tests
    // }
}

// Create a singleton instance of the DashboardWsManager
thread_local! {
    pub static DASHBOARD_WS: RefCell<Option<DashboardWsManager>> = RefCell::new(None);
}

/// Initialize the dashboard WebSocket manager singleton
pub fn init_dashboard_ws() -> Result<(), JsValue> {
    DASHBOARD_WS.with(|cell| {
        let mut manager_opt = cell.borrow_mut();
        if let Some(mgr) = manager_opt.as_mut() {
            // Already initialised – ensure we are subscribed to any new agents.
            debug_log!("DashboardWsManager: refreshing subscriptions for new agents");
            let topic_manager_trait_rc = APP_STATE.with(|state_ref| {
                state_ref.borrow().topic_manager.clone() as Rc<RefCell<dyn ITopicManager>>
            });
            mgr.subscribe_to_agent_events(topic_manager_trait_rc)?;
        } else {
            debug_log!("Initializing DashboardWsManager singleton...");
            let mut manager = DashboardWsManager::new();
            manager.initialize()?;
            *manager_opt = Some(manager);
        }
        Ok(())
    })
}

/// Cleanup the dashboard WebSocket manager singleton subscriptions
#[allow(dead_code)]
pub fn cleanup_dashboard_ws() -> Result<(), JsValue> {
    DASHBOARD_WS.with(|cell| {
        let mut manager_opt = cell.borrow_mut();
        if let Some(manager) = manager_opt.as_mut() {
            debug_log!("Cleaning up DashboardWsManager singleton...");
            // Get the trait object from APP_STATE to pass to cleanup
            let topic_manager_trait_rc = APP_STATE.with(|state_ref| {
                state_ref.borrow().topic_manager.clone() as Rc<RefCell<dyn ITopicManager>>
            });
            manager.cleanup(topic_manager_trait_rc)?; // Pass the trait object
        }
        Ok(())
    })
}
