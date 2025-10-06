#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// NodeLog represents a NodeLog model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct NodeLog {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="execution_id")]
    pub execution_id: i32,
    #[serde(rename="node_id")]
    pub node_id: String,
    #[serde(rename="stream")]
    pub stream: Box<AnonymousSchema52>,
    #[serde(rename="text")]
    pub text: String,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

