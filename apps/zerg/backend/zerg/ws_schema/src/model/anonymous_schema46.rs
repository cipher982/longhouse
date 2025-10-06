#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// AnonymousSchema46 represents a AnonymousSchema46 model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub enum AnonymousSchema46 {
    #[serde(rename="success")]
    Success,
    #[serde(rename="failed")]
    Failed,
}

