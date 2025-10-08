#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// SendMessage represents a SendMessage model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct SendMessage {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="thread_id")]
    pub thread_id: i32,
    #[serde(rename="content")]
    pub content: String,
    #[serde(rename="metadata", skip_serializing_if = "Option::is_none")]
    pub metadata: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

