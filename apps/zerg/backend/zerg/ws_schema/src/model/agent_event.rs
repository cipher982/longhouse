#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// AgentEvent represents a AgentEvent model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct AgentEvent {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="data")]
    pub data: std::collections::HashMap<String, serde_json::Value>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

