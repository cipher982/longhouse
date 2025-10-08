#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// NodeState represents a NodeState model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct NodeState {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="execution_id")]
    pub execution_id: i32,
    #[serde(rename="node_id")]
    pub node_id: String,
    #[serde(rename="status")]
    pub status: Box<AnonymousSchema41>,
    #[serde(rename="output", skip_serializing_if = "Option::is_none")]
    pub output: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[serde(rename="error", skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

