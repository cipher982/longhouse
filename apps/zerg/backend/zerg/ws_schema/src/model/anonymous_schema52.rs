#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// AnonymousSchema52 represents a AnonymousSchema52 model.
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
pub enum AnonymousSchema52 {
    #[serde(rename="stdout")]
    Stdout,
    #[serde(rename="stderr")]
    Stderr,
}

