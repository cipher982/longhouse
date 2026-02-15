//! Streaming payload builder + gzip compressor.
//!
//! The key optimization: `serde_json::to_writer` writes directly into
//! `GzEncoder`, so the full JSON is never materialized in memory.
//! This eliminates the 79% gzip bottleneck from the Python version.

use std::sync::OnceLock;

use flate2::write::GzEncoder;
use flate2::Compression;
use serde::Serialize;

use super::parser::{ParsedEvent, SessionMetadata};

/// Cached hostname — called once, reused for all payloads.
fn cached_hostname() -> &'static str {
    static HOSTNAME: OnceLock<String> = OnceLock::new();
    HOSTNAME.get_or_init(|| {
        std::process::Command::new("hostname")
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "unknown".to_string())
    })
}

// ---------------------------------------------------------------------------
// Payload types (match Python ingest API exactly)
// ---------------------------------------------------------------------------

#[derive(Serialize)]
pub struct IngestPayload<'a> {
    pub id: &'a str,
    pub provider: &'a str,
    pub environment: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub project: Option<&'a str>,
    pub device_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub git_repo: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub git_branch: Option<&'a str>,
    pub started_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ended_at: Option<String>,
    pub provider_session_id: &'a str,
    pub events: Vec<EventIngest<'a>>,
}

#[derive(Serialize)]
pub struct EventIngest<'a> {
    pub role: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_text: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_input_json: Option<&'a serde_json::Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_output_text: Option<&'a str>,
    pub timestamp: String,
    pub source_path: &'a str,
    pub source_offset: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_json: Option<&'a str>,
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Build an IngestPayload from parsed events and metadata.
pub fn build_payload<'a>(
    session_id: &'a str,
    events: &'a [ParsedEvent],
    metadata: &'a SessionMetadata,
    source_path: &'a str,
    provider: &'a str,
) -> IngestPayload<'a> {
    let hostname = cached_hostname();

    let started_at = metadata
        .started_at
        .map(|t| t.to_rfc3339())
        .or_else(|| {
            events
                .iter()
                .map(|e| e.timestamp)
                .min()
                .map(|t| t.to_rfc3339())
        })
        .unwrap_or_else(|| chrono::Utc::now().to_rfc3339());

    let ended_at = metadata
        .ended_at
        .map(|t| t.to_rfc3339())
        .or_else(|| {
            events
                .iter()
                .map(|e| e.timestamp)
                .max()
                .map(|t| t.to_rfc3339())
        });

    let event_ingests: Vec<EventIngest<'a>> = events
        .iter()
        .map(|e| {
            let role = match e.role {
                super::parser::Role::User => "user",
                super::parser::Role::Assistant => "assistant",
                super::parser::Role::Tool => "tool",
            };
            EventIngest {
                role,
                content_text: e.content_text.as_deref(),
                tool_name: e.tool_name.as_deref(),
                tool_input_json: e.tool_input_json.as_ref(),
                tool_output_text: e.tool_output_text.as_deref(),
                timestamp: e.timestamp.to_rfc3339(),
                source_path,
                source_offset: e.source_offset,
                raw_json: e.raw_line.as_deref(),
            }
        })
        .collect();

    IngestPayload {
        id: session_id,
        provider,
        environment: "production",
        project: metadata.project.as_deref(),
        device_id: format!("shipper-{}", hostname),
        cwd: metadata.cwd.as_deref(),
        git_repo: None,
        git_branch: metadata.git_branch.as_deref(),
        started_at,
        ended_at,
        provider_session_id: &metadata.session_id,
        events: event_ingests,
    }
}

/// Build an IngestPayload and stream-compress it to gzip bytes.
///
/// This is THE key optimization: `serde_json::to_writer` writes JSON tokens
/// directly into the `GzEncoder`'s write buffer. At no point is the full
/// JSON string materialized in memory.
pub fn build_and_compress(
    session_id: &str,
    events: &[ParsedEvent],
    metadata: &SessionMetadata,
    source_path: &str,
    provider: &str,
) -> anyhow::Result<Vec<u8>> {
    let payload = build_payload(session_id, events, metadata, source_path, provider);

    // Stream serialize directly into gzip compressor
    let mut gz = GzEncoder::new(Vec::with_capacity(64 * 1024), Compression::fast());
    serde_json::to_writer(&mut gz, &payload)?;
    let compressed = gz.finish()?;

    Ok(compressed)
}

/// Compress an already-built payload to gzip bytes (for benchmarking).
pub fn compress_payload(payload: &IngestPayload<'_>) -> anyhow::Result<Vec<u8>> {
    let mut gz = GzEncoder::new(Vec::with_capacity(64 * 1024), Compression::fast());
    serde_json::to_writer(&mut gz, payload)?;
    let compressed = gz.finish()?;
    Ok(compressed)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipeline::parser::Role;
    use chrono::Utc;
    use flate2::read::GzDecoder;
    use std::io::Read;

    fn make_test_events() -> Vec<ParsedEvent> {
        vec![
            ParsedEvent {
                uuid: "e1".to_string(),
                session_id: "s1".to_string(),
                timestamp: Utc::now(),
                role: Role::User,
                content_text: Some("Hello world".to_string()),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                source_offset: 0,
                raw_type: "user".to_string(),
                raw_line: Some(r#"{"type":"user","message":{"content":"Hello world"}}"#.to_string()),
            },
            ParsedEvent {
                uuid: "e2".to_string(),
                session_id: "s1".to_string(),
                timestamp: Utc::now(),
                role: Role::Assistant,
                content_text: Some("Hi there!".to_string()),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                source_offset: 100,
                raw_type: "assistant".to_string(),
                raw_line: None,
            },
        ]
    }

    #[test]
    fn test_build_payload() {
        let events = make_test_events();
        let meta = SessionMetadata {
            session_id: "s1".to_string(),
            cwd: Some("/home/user/proj".to_string()),
            project: Some("proj".to_string()),
            ..Default::default()
        };

        let payload = build_payload("test-id", &events, &meta, "/path/to/file", "claude");
        assert_eq!(payload.id, "test-id");
        assert_eq!(payload.provider, "claude");
        assert_eq!(payload.events.len(), 2);
        assert_eq!(payload.events[0].role, "user");
        assert_eq!(payload.events[1].role, "assistant");
        // First event has raw_json, second doesn't
        assert!(payload.events[0].raw_json.is_some());
        assert!(payload.events[1].raw_json.is_none());
    }

    #[test]
    fn test_streaming_compress_roundtrip() {
        let events = make_test_events();
        let meta = SessionMetadata {
            session_id: "s1".to_string(),
            cwd: Some("/proj".to_string()),
            project: Some("proj".to_string()),
            ..Default::default()
        };

        let compressed =
            build_and_compress("test-id", &events, &meta, "/path/to/file", "claude").unwrap();

        // Decompress and verify valid JSON
        let mut decoder = GzDecoder::new(&compressed[..]);
        let mut json_str = String::new();
        decoder.read_to_string(&mut json_str).unwrap();

        let parsed: serde_json::Value = serde_json::from_str(&json_str).unwrap();
        assert_eq!(parsed["id"], "test-id");
        assert_eq!(parsed["provider"], "claude");
        assert_eq!(parsed["events"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn test_compression_ratio() {
        // Generate enough events to see meaningful compression
        let mut events = Vec::new();
        for i in 0..100 {
            events.push(ParsedEvent {
                uuid: format!("e{}", i),
                session_id: "s1".to_string(),
                timestamp: Utc::now(),
                role: Role::Assistant,
                content_text: Some(format!("This is response number {} with some repeated text to help compression.", i)),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                source_offset: i * 100,
                raw_type: "assistant".to_string(),
                raw_line: if i == 0 { Some("raw".to_string()) } else { None },
            });
        }

        let meta = SessionMetadata {
            session_id: "s1".to_string(),
            ..Default::default()
        };

        let compressed =
            build_and_compress("test-id", &events, &meta, "/path", "claude").unwrap();
        let uncompressed = serde_json::to_vec(&build_payload("test-id", &events, &meta, "/path", "claude")).unwrap();

        // Compressed should be significantly smaller
        let ratio = uncompressed.len() as f64 / compressed.len() as f64;
        assert!(
            ratio > 2.0,
            "Expected compression ratio > 2x, got {:.1}x ({} → {} bytes)",
            ratio,
            uncompressed.len(),
            compressed.len()
        );
    }
}
