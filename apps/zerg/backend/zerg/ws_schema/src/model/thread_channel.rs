#[allow(unused)]
use super::*;
use serde::{Deserialize, Serialize};

/// ThreadChannel represents a union of types: StreamStart, StreamChunk, StreamEnd
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(untagged)]
pub enum ThreadChannel {
    #[serde(rename="StreamStart")]
    StreamStart(StreamStart),
    #[serde(rename="StreamChunk")]
    StreamChunk(StreamChunk),
    #[serde(rename="StreamEnd")]
    StreamEnd(StreamEnd),
}


