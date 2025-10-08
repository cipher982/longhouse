#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// Error represents a Error model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct Error {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="code")]
    pub code: i32,
    #[serde(rename="message")]
    pub message: String,
    #[serde(rename="details", skip_serializing_if = "Option::is_none")]
    pub details: Option<std::collections::HashMap<String, serde_json::Value>>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

