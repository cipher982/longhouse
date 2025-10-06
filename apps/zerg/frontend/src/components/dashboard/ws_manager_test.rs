use super::ws_manager::DashboardWsManager;
use wasm_bindgen_test::*;
// TopicManager currently unused in this unit-test; remove once test expands.
use crate::network::topic_manager::{ITopicManager, Topic, TopicHandler};
use crate::network::ws_client_v2::{ConnectionState, IWsClient};
use serde_json::Value;
use std::any::Any;
use std::cell::RefCell;
use std::collections::VecDeque;
use std::default::Default;
use std::rc::Rc;
use wasm_bindgen::JsValue;

wasm_bindgen_test_configure!(run_in_browser);

// Mock WebSocket Client
#[derive(Clone)]
struct MockWsClient {
    state: Rc<RefCell<ConnectionState>>,
    sent_messages: Rc<RefCell<Vec<String>>>,
    on_connect: Option<Rc<RefCell<Box<dyn FnMut() + 'static>>>>,
    on_message: Option<Rc<RefCell<Box<dyn FnMut(serde_json::Value) + 'static>>>>,
    on_disconnect: Option<Rc<RefCell<Box<dyn FnMut() + 'static>>>>,
}

// Manual Default implementation
impl Default for MockWsClient {
    fn default() -> Self {
        Self {
            state: Rc::new(RefCell::new(ConnectionState::Disconnected)), // Set default state
            sent_messages: Rc::new(RefCell::new(Vec::new())),
            on_connect: None,
            on_message: None,
            on_disconnect: None,
        }
    }
}

impl IWsClient for MockWsClient {
    fn connect(&mut self) -> Result<(), JsValue> {
        *self.state.borrow_mut() = ConnectionState::Connected;
        if let Some(callback_rc) = &self.on_connect {
            if let Ok(mut cb) = callback_rc.try_borrow_mut() {
                (*cb)();
            }
        }
        Ok(())
    }

    fn send_serialized_message(&self, message_json: &str) -> Result<(), JsValue> {
        if *self.state.borrow() != ConnectionState::Connected {
            return Err(JsValue::from_str("WebSocket is not connected"));
        }
        self.sent_messages
            .borrow_mut()
            .push(message_json.to_string());
        Ok(())
    }

    fn connection_state(&self) -> ConnectionState {
        self.state.borrow().clone()
    }

    fn close(&mut self) -> Result<(), JsValue> {
        *self.state.borrow_mut() = ConnectionState::Disconnected;
        if let Some(callback_rc) = &self.on_disconnect {
            if let Ok(mut cb) = callback_rc.try_borrow_mut() {
                (*cb)();
            }
        }
        Ok(())
    }

    fn set_on_connect(&mut self, callback: Box<dyn FnMut() + 'static>) {
        self.on_connect = Some(Rc::new(RefCell::new(callback)));
    }

    fn set_on_message(&mut self, callback: Box<dyn FnMut(serde_json::Value) + 'static>) {
        self.on_message = Some(Rc::new(RefCell::new(callback)));
    }

    fn set_on_disconnect(&mut self, callback: Box<dyn FnMut() + 'static>) {
        self.on_disconnect = Some(Rc::new(RefCell::new(callback)));
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

impl MockWsClient {
    fn new() -> Self {
        Self::default() // Use the manual Default impl
    }

    fn get_sent_messages(&self) -> Vec<String> {
        self.sent_messages.borrow().clone()
    }

    fn simulate_receive_message(&self, message: Value) {
        if let Some(callback_rc) = &self.on_message {
            if let Ok(mut cb) = callback_rc.try_borrow_mut() {
                (*cb)(message);
            } else {
                web_sys::console::error_1(
                    &"Failed to borrow on_message callback mutably in simulate_receive_message"
                        .into(),
                );
            }
        } else {
            web_sys::console::warn_1(
                &"simulate_receive_message called but no on_message handler is set on MockWsClient"
                    .into(),
            );
        }
    }
}

// Mock TopicManager
#[derive(Clone, Default)]
struct MockTopicManager {
    calls: Rc<RefCell<VecDeque<String>>>,
    handlers: Rc<RefCell<std::collections::HashMap<String, Vec<TopicHandler>>>>,
}

impl MockTopicManager {
    fn new() -> Self {
        Self::default()
    }

    // Keep helper methods specific to the mock
    fn get_calls(&self) -> VecDeque<String> {
        self.calls.borrow().clone()
    }

    fn get_handlers(&self, topic: &str) -> Vec<TopicHandler> {
        self.handlers
            .borrow()
            .get(topic)
            .cloned()
            .unwrap_or_default()
    }
}

// --- Implement the ITopicManager trait for the mock ---
impl ITopicManager for MockTopicManager {
    fn subscribe(&mut self, topic: Topic, handler: TopicHandler) -> Result<(), JsValue> {
        let call_log = format!("subscribe::{}", topic);
        self.calls.borrow_mut().push_back(call_log);
        self.handlers
            .borrow_mut()
            .entry(topic)
            .or_default()
            .push(handler);
        Ok(())
    }

    fn unsubscribe_handler(
        &mut self,
        topic: &Topic,
        handler_to_remove: &TopicHandler,
    ) -> Result<(), JsValue> {
        let call_log = format!("unsubscribe_handler::{}", topic);
        self.calls.borrow_mut().push_back(call_log);

        let mut removed = false;
        if let Some(handlers) = self.handlers.borrow_mut().get_mut(topic) {
            if let Some(pos) = handlers
                .iter()
                .position(|h| Rc::ptr_eq(h, handler_to_remove))
            {
                handlers.remove(pos);
                removed = true;
            }
        }
        if !removed {
            web_sys::console::warn_1(&format!("MockTopicManager: unsubscribe_handler called but handler not found for topic {}", topic).into());
        }
        Ok(())
    }

    // Implement other trait methods if needed by tests
}

// Test Setup Helper
// Now returns the manager, the concrete mock Rc, and the trait object Rc
fn setup_test() -> (
    DashboardWsManager,
    Rc<RefCell<MockTopicManager>>,
    Rc<RefCell<dyn ITopicManager>>,
) {
    let mock_topic_manager = MockTopicManager::new();
    // Create Rc for the concrete type
    let concrete_mock_rc = Rc::new(RefCell::new(mock_topic_manager));
    // Create Rc for the trait object by cloning and casting
    let trait_object_rc = concrete_mock_rc.clone() as Rc<RefCell<dyn ITopicManager>>;
    let manager = DashboardWsManager::new();
    (manager, concrete_mock_rc, trait_object_rc)
}

// New Tests

#[wasm_bindgen_test]
async fn test_dashboard_manager_subscribe() {
    // Get both Rc types from setup
    let (mut manager, concrete_mock_rc, trait_object_rc) = setup_test();

    // Insert a dummy agent so the production code subscribes to exactly one
    // concrete topic ("agent:1") instead of zero, which is expected runtime
    // behaviour.
    {
        use crate::messages::Message;
        use crate::models::ApiAgent;

        // Construct a *single* dummy agent and dispatch via the global message
        // bus so state mutation happens inside `update()` – keeps tests aligned
        // with the production Elm-style flow and avoids direct `APP_STATE`
        // borrows.
        let dummy_agent = ApiAgent {
            id: Some(1),
            name: "Dummy".to_string(),
            status: None,
            system_instructions: None,
            task_instructions: None,
            model: None,
            temperature: None,
            created_at: None,
            updated_at: None,
            schedule: None,
            next_run_at: None,
            last_run_at: None,
            last_error: None,
            owner_id: None,
            owner: None,
        };

        crate::state::dispatch_global_message(Message::AgentsRefreshed(vec![dummy_agent]));
    }

    // Pass the TRAIT OBJECT to the method under test
    let result = manager.subscribe_to_agent_events(trait_object_rc.clone());
    assert!(result.is_ok(), "subscribe_to_agent_events failed");

    // Use the CONCRETE MOCK Rc to verify calls
    let calls = concrete_mock_rc.borrow().get_calls();
    assert_eq!(calls.len(), 1, "Expected 1 call to mock topic manager");
    assert_eq!(
        calls[0], "subscribe::agent:1",
        "Expected subscribe call for agent:1"
    );

    assert!(
        manager.agent_subscription_handler.is_some(),
        "Manager should store the handler"
    );

    // Use the CONCRETE MOCK Rc to verify handlers
    // Ensure the handler was registered on the concrete per-agent topic that
    // the DashboardWsManager subscribes to (wild-card topics are *not*
    // supported by the backend at runtime).
    let handlers = concrete_mock_rc.borrow().get_handlers("agent:1");
    assert_eq!(
        handlers.len(),
        1,
        "Mock should have one handler for agent:1"
    );
}

#[wasm_bindgen_test]
async fn test_dashboard_manager_cleanup() {
    // Get both Rc types
    let (mut manager, concrete_mock_rc, trait_object_rc) = setup_test();

    // Insert dummy agent with id 1 as in subscribe test
    {
        use crate::messages::Message;
        use crate::models::ApiAgent;

        let dummy_agent = ApiAgent {
            id: Some(1),
            name: "Dummy".to_string(),
            status: None,
            system_instructions: None,
            task_instructions: None,
            model: None,
            temperature: None,
            created_at: None,
            updated_at: None,
            schedule: None,
            next_run_at: None,
            last_run_at: None,
            last_error: None,
            owner_id: None,
            owner: None,
        };

        crate::state::dispatch_global_message(Message::AgentsRefreshed(vec![dummy_agent]));
    }

    // Setup: Subscribe first using the TRAIT OBJECT
    manager
        .subscribe_to_agent_events(trait_object_rc.clone())
        .expect("Subscribe failed during setup");
    let handler_rc = manager
        .agent_subscription_handler
        .clone()
        .expect("Handler should exist after subscribe");
    // Clear calls using the CONCRETE MOCK Rc
    concrete_mock_rc.borrow_mut().calls.borrow_mut().clear();

    // Action: Call cleanup using the TRAIT OBJECT
    let result = manager.cleanup(trait_object_rc.clone());
    assert!(result.is_ok(), "cleanup failed");

    // Verification: Check calls using the CONCRETE MOCK Rc
    let calls = concrete_mock_rc.borrow().get_calls();
    assert_eq!(
        calls.len(),
        1,
        "Expected 1 call to mock topic manager during cleanup"
    );
    assert_eq!(
        calls[0], "unsubscribe_handler::agent:1",
        "Expected unsubscribe_handler call for agent:1"
    );

    assert!(
        manager.agent_subscription_handler.is_none(),
        "Manager should clear the handler on cleanup"
    );

    // Verification: Check handlers using the CONCRETE MOCK Rc
    let handlers_after_cleanup = concrete_mock_rc.borrow().get_handlers("agent:1");
    assert!(
        !handlers_after_cleanup
            .iter()
            .any(|h| Rc::ptr_eq(h, &handler_rc)),
        "Handler should be removed from mock topic manager"
    );
}

// TODO: Add test for event handling side effect (requires mocking api_client::load_agents)
// #[wasm_bindgen_test]
// async fn test_dashboard_handler_triggers_load_agents() { ... }

// Remove old tests that are no longer valid due to struct changes
/*
#[wasm_bindgen_test]
async fn test_dashboard_ws_initialization() { ... }
#[wasm_bindgen_test]
async fn test_dashboard_ws_cleanup() { ... }
#[wasm_bindgen_test]
async fn test_subscription_management() { ... }
#[wasm_bindgen_test]
async fn test_reconnection_handling() { ... }
#[wasm_bindgen_test]
async fn test_agent_event_handling() { ... }
*/
