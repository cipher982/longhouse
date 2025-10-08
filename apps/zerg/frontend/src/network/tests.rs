#[cfg(test)]
mod tests {
    use crate::generated::ws_messages::{Envelope, ThreadMessageData};
    use serde_json::json;

    #[test]
    fn parse_uppercase_envelope() {
        let env_json = json!({
            "v": 1,
            "type": "THREAD_MESSAGE",
            "topic": "thread:1",
            "req_id": null,
            "ts": 0,
            "data": {
                "thread_id": 1,
                "message": {
                    "id": 123,
                    "thread_id": 1,
                    "role": "assistant",
                    "content": "hi",
                    "timestamp": null,
                    "message_type": null,
                    "tool_name": null,
                    "tool_call_id": null,
                    "tool_input": null,
                    "parent_id": null
                }
            }
        });
        // Parse as Envelope
        let env: Envelope = serde_json::from_value(env_json.clone()).unwrap();
        assert_eq!(env.topic, "thread:1");
        // Parse as WsMessage directly
        let ws: WsMessage = serde_json::from_value(env_json.clone()).unwrap();
        if let WsMessage::ThreadMessage { data } = ws {
            let m: crate::models::ApiThreadMessage = data.into();
            assert_eq!(m.content, "hi");
        } else {
            panic!("wrong variant");
        }
    }
}
