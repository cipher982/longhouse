// Contract-first: All message types generated from AsyncAPI schema
// No legacy format support - envelope-only architecture

// Re-export all generated types
pub use crate::generated::ws_messages::{
    AgentEventData, ErrorData, PingData, PongData, SendMessageData, StreamChunkData, StreamEndData,
    StreamStartData, SubscribeData, ThreadEventData, ThreadMessageData, UnsubscribeData,
};

/// Modern message builders using generated types and envelope system
pub mod builders {
    use super::*;
    use crate::generated::ws_messages::Envelope;

    use uuid::Uuid;

    /// Helper: generate a random UUID for the `message_id` field.
    fn new_id() -> String {
        Uuid::new_v4().to_string()
    }

    /// Build a typed subscribe message using generated envelope system
    pub fn create_subscribe(topics: Vec<String>) -> Envelope {
        let data = SubscribeData {
            topics,
            message_id: Some(new_id()),
        };

        Envelope::new(
            "subscribe".to_string(),
            "system".to_string(),
            serde_json::to_value(data).unwrap(),
            Some(new_id()),
        )
    }

    /// Build a typed unsubscribe message using generated envelope system
    pub fn create_unsubscribe(topics: Vec<String>) -> Envelope {
        let data = UnsubscribeData {
            topics,
            message_id: Some(new_id()),
        };

        Envelope::new(
            "unsubscribe".to_string(),
            "system".to_string(),
            serde_json::to_value(data).unwrap(),
            Some(new_id()),
        )
    }

    /// Build a typed ping message using generated envelope system
    pub fn create_ping() -> Envelope {
        let data = PingData {
            timestamp: Some(js_sys::Date::now() as u32),
        };

        Envelope::new(
            "ping".to_string(),
            "system".to_string(),
            serde_json::to_value(data).unwrap(),
            Some(new_id()),
        )
    }

    /// Build a typed send message using envelope system
    pub fn create_send_message(thread_id: i32, content: &str) -> Envelope {
        let data = SendMessageData {
            thread_id: thread_id as u32,
            content: content.to_string(),
            metadata: None,
        };

        Envelope::new(
            "send_message".to_string(),
            format!("thread:{}", thread_id),
            serde_json::to_value(data).unwrap(),
            Some(new_id()),
        )
    }

    /// Build a models request message - placeholder for missing functionality
    pub fn create_models_request() -> Envelope {
        // This is a placeholder - models should be fetched via REST API, not WebSocket
        Envelope::new(
            "subscribe".to_string(),
            "system".to_string(),
            serde_json::to_value(SubscribeData {
                topics: vec!["models".to_string()],
                message_id: Some(new_id()),
            })
            .unwrap(),
            Some(new_id()),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use wasm_bindgen_test::*;

    wasm_bindgen_test_configure!(run_in_browser);

    #[wasm_bindgen_test]
    fn test_modern_ping_message() {
        let envelope = builders::create_ping();

        // Test envelope structure
        assert_eq!(envelope.v, 1);
        assert_eq!(envelope.message_type, "ping");
        assert_eq!(envelope.topic, "system");
        assert!(envelope.req_id.is_some());

        // Test validation
        let json_value = serde_json::to_value(&envelope).unwrap();
        assert!(crate::generated::ws_messages::validate_envelope(&json_value).is_ok());
    }

    #[wasm_bindgen_test]
    fn test_modern_subscribe_message() {
        let topics = vec!["thread:123".to_string(), "agent:456".to_string()];
        let envelope = builders::create_subscribe(topics.clone());

        // Test envelope structure
        assert_eq!(envelope.message_type, "subscribe");
        assert_eq!(envelope.topic, "system");

        // Test data payload
        let data = &envelope.data;
        if let serde_json::Value::Object(map) = data {
            assert!(map.contains_key("topics"));
            if let Some(serde_json::Value::Array(topic_array)) = map.get("topics") {
                assert_eq!(topic_array.len(), 2);
            }
        } else {
            panic!("Expected data to be an object");
        }

        // Test validation
        let json_value = serde_json::to_value(&envelope).unwrap();
        assert!(crate::generated::ws_messages::validate_envelope(&json_value).is_ok());
    }

    #[wasm_bindgen_test]
    fn test_envelope_error_parsing() {
        // Test modern envelope-based error parsing
        let error_envelope = json!({
            "v": 1,
            "type": "error",
            "topic": "system",
            "ts": 1642000000000i64,
            "data": {
                "error": "Test error",
                "details": null
            }
        });

        let parsed: crate::generated::ws_messages::Envelope =
            serde_json::from_value(error_envelope).unwrap();

        assert_eq!(parsed.message_type, "error");
        assert_eq!(parsed.topic, "system");
        if let serde_json::Value::Object(map) = &parsed.data {
            assert!(map.contains_key("error"));
        } else {
            panic!("Expected data to be an object");
        }
    }

    #[wasm_bindgen_test]
    fn test_generated_agent_event_data() {
        let data = AgentEventData {
            id: 123,
            status: Some("active".to_string()),
            last_run_at: None,
            next_run_at: None,
            last_error: None,
            name: Some("Test Agent".to_string()),
            description: None,
        };

        let json = serde_json::to_string(&data).unwrap();
        assert!(json.contains("\"id\":123"));
        assert!(json.contains("\"name\":\"Test Agent\""));
        assert!(json.contains("\"status\":\"active\""));
    }

    #[wasm_bindgen_test]
    fn test_send_message_builder() {
        let envelope = builders::create_send_message(123, "Hello world");

        // Test envelope structure
        assert_eq!(envelope.message_type, "send_message");
        assert_eq!(envelope.topic, "thread:123");
        assert!(envelope.req_id.is_some());

        // Test data payload
        let data = &envelope.data;
        assert_eq!(data.get("thread_id").and_then(|v| v.as_u64()), Some(123));
        assert_eq!(
            data.get("content").and_then(|v| v.as_str()),
            Some("Hello world")
        );

        // Test validation
        let json_value = serde_json::to_value(&envelope).unwrap();
        assert!(crate::generated::ws_messages::validate_envelope(&json_value).is_ok());
    }
}
