#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// Pong represents a Pong model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Pong {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="ping_id")]
    pub ping_id: String,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

