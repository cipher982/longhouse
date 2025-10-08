#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// ThreadHistory represents a ThreadHistory model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct ThreadHistory {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="thread_id")]
    pub thread_id: i32,
    #[serde(rename="messages")]
    pub messages: Vec<Std::collections::HashMap<String, serde_json::Value>>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

