#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// StreamChunk represents a StreamChunk model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct StreamChunk {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="thread_id")]
    pub thread_id: i32,
    #[serde(rename="content")]
    pub content: String,
    #[serde(rename="chunk_type")]
    pub chunk_type: Box<AnonymousSchema25>,
    #[serde(rename="tool_name", skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(rename="tool_call_id", skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

