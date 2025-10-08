#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// Envelope represents a Envelope model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Envelope {
    #[serde(rename="v")]
    pub v: i32,
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="topic")]
    pub topic: String,
    #[serde(rename="ts")]
    pub ts: i32,
    #[serde(rename="req_id", skip_serializing_if = "Option::is_none")]
    pub req_id: Option<String>,
    #[serde(rename="data")]
    pub data: std::collections::HashMap<String, serde_json::Value>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

