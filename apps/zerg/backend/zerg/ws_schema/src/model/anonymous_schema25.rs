#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// AnonymousSchema25 represents a AnonymousSchema25 model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub enum AnonymousSchema25 {
    #[serde(rename="assistant_token")]
    AssistantToken,
    #[serde(rename="assistant_message")]
    AssistantMessage,
    #[serde(rename="tool_output")]
    ToolOutput,
}

