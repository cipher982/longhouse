#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// AnonymousSchema41 represents a AnonymousSchema41 model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub enum AnonymousSchema41 {
    #[serde(rename="running")]
    Running,
    #[serde(rename="success")]
    Success,
    #[serde(rename="failed")]
    Failed,
}

