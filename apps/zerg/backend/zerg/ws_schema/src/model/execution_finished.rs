#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// ExecutionFinished represents a ExecutionFinished model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub struct ExecutionFinished {
    #[serde(rename="type")]
    pub reserved_type: String,
    #[serde(rename="execution_id")]
    pub execution_id: i32,
    #[serde(rename="status")]
    pub status: Box<AnonymousSchema46>,
    #[serde(rename="error", skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(rename="duration_ms", skip_serializing_if = "Option::is_none")]
    pub duration_ms: Option<i32>,
    #[serde(rename="additionalProperties", skip_serializing_if = "Option::is_none")]
    pub additional_properties: Option<std::collections::HashMap<String, serde_json::Value>>,
}

