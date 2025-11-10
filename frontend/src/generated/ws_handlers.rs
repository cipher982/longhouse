// AUTO-GENERATED FILE - DO NOT EDIT
// Generated from ws-protocol-asyncapi.yml at 2025-11-10T17:33:49.182310Z
//
// Handler traits and message routing with modern patterns

use std::{cell::RefCell, rc::Rc};
use wasm_bindgen::JsValue;
use crate::generated::ws_messages::*;

// Handler trait definitions with compile-time validation

/// Handles agent management, runs, workflow execution, and ops ticker
pub trait DashboardHandler {
    fn handle_run_update(&self, data: RunUpdateData) -> Result<(), JsValue>;
    fn handle_agent_event(&self, data: AgentEventData) -> Result<(), JsValue>;
    fn handle_execution_finished(&self, data: ExecutionFinishedData) -> Result<(), JsValue>;
    fn handle_node_state(&self, data: NodeStateData) -> Result<(), JsValue>;
    fn handle_node_log(&self, data: NodeLogData) -> Result<(), JsValue>;
    fn handle_ops_event(&self, data: OpsEventData) -> Result<(), JsValue>;
}

/// Handles thread messages and streaming events
pub trait ChatHandler {
    fn handle_thread_message(&self, data: ThreadMessageData) -> Result<(), JsValue>;
    fn handle_stream_start(&self, data: StreamStartData) -> Result<(), JsValue>;
    fn handle_stream_chunk(&self, data: StreamChunkData) -> Result<(), JsValue>;
    fn handle_stream_end(&self, data: StreamEndData) -> Result<(), JsValue>;
    fn handle_assistant_id(&self, data: AssistantIdData) -> Result<(), JsValue>;
}

// Message router with enhanced error handling

/// Enhanced message router for dashboard
pub struct DashboardMessageRouter<T: DashboardHandler> {
    handler: Rc<RefCell<T>>,
}

impl<T: DashboardHandler> DashboardMessageRouter<T> {
    pub fn new(handler: Rc<RefCell<T>>) -> Self {
        Self {
            handler,
        }
    }
    
    /// Route message with enhanced error handling
    pub fn route_message(&mut self, envelope: &Envelope) -> Result<(), JsValue> {
        let message_type = &envelope.message_type;
        
        // Validate envelope first
        envelope.validate()
            .map_err(|e| JsValue::from_str(&format!("Envelope validation failed: {}", e)))?;
        
        let message_data = &envelope.data;
        
        match message_type.as_str() {
            "run_update" => {
                match serde_json::from_value::<RunUpdateData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_run_update(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse run_update: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "agent_event" => {
                match serde_json::from_value::<AgentEventData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_agent_event(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse agent_event: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "agent_created" => {
                match serde_json::from_value::<AgentEventData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_agent_event(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse agent_created: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "agent_updated" => {
                match serde_json::from_value::<AgentEventData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_agent_event(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse agent_updated: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "agent_deleted" => {
                match serde_json::from_value::<AgentEventData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_agent_event(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse agent_deleted: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "agent_state" => {
                match serde_json::from_value::<AgentEventData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_agent_event(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse agent_state: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "execution_finished" => {
                match serde_json::from_value::<ExecutionFinishedData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_execution_finished(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse execution_finished: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "node_state" => {
                match serde_json::from_value::<NodeStateData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_node_state(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse node_state: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "node_log" => {
                match serde_json::from_value::<NodeLogData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_node_log(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse node_log: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "ops_event" => {
                match serde_json::from_value::<OpsEventData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_ops_event(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse ops_event: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            _ => {
                web_sys::console::warn_1(&format!("DashboardMessageRouter: Unknown message type: {}", message_type).into());
                Ok(())
            }
        }
    }
}

/// Enhanced message router for chat
pub struct ChatMessageRouter<T: ChatHandler> {
    handler: Rc<RefCell<T>>,
}

impl<T: ChatHandler> ChatMessageRouter<T> {
    pub fn new(handler: Rc<RefCell<T>>) -> Self {
        Self {
            handler,
        }
    }
    
    /// Route message with enhanced error handling
    pub fn route_message(&mut self, envelope: &Envelope) -> Result<(), JsValue> {
        let message_type = &envelope.message_type;
        
        // Validate envelope first
        envelope.validate()
            .map_err(|e| JsValue::from_str(&format!("Envelope validation failed: {}", e)))?;
        
        let message_data = &envelope.data;
        
        match message_type.as_str() {
            "thread_message" => {
                match serde_json::from_value::<ThreadMessageData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_thread_message(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse thread_message: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "thread_message_created" => {
                match serde_json::from_value::<ThreadMessageData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_thread_message(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse thread_message_created: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "stream_start" => {
                match serde_json::from_value::<StreamStartData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_stream_start(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse stream_start: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "stream_chunk" => {
                match serde_json::from_value::<StreamChunkData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_stream_chunk(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse stream_chunk: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "stream_end" => {
                match serde_json::from_value::<StreamEndData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_stream_end(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse stream_end: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            "assistant_id" => {
                match serde_json::from_value::<AssistantIdData>(message_data.clone()) {
                    Ok(data) => self.handler.borrow().handle_assistant_id(data),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to parse assistant_id: {}", e).into());
                        Err(JsValue::from_str(&format!("Parse error: {}", e)))
                    }
                }
            }
            _ => {
                web_sys::console::warn_1(&format!("ChatMessageRouter: Unknown message type: {}", message_type).into());
                Ok(())
            }
        }
    }
}

// Helper functions for runtime usage

pub fn validate_message_format(envelope: &Envelope) -> Result<(), String> {
    envelope.validate()
}

pub fn get_handler_for_topic(topic: &str) -> Option<&'static str> {
    if topic.starts_with("agent:") || topic.starts_with("workflow_execution:") || topic.starts_with("ops:") {
        Some("dashboard")
    } else if topic.starts_with("thread:") {
        Some("chat")
    } else if topic.starts_with("system") {
        Some("system")
    } else {
        None
    }
}

