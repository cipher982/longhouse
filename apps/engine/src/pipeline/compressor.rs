//! Streaming payload builder + compressor (gzip or zstd).
//!
//! The key optimization: `serde_json::to_writer` writes directly into
//! the compressor's Write impl, so the full JSON is never materialized
//! in memory. Supports gzip (default, universal) and zstd (12x faster).

use std::sync::OnceLock;

use flate2::write::GzEncoder;
use flate2::Compression as GzCompression;
use serde::Serialize;
use serde_json::value::RawValue;

use super::parser::{ParsedEvent, SessionMetadata};

/// Compression algorithm for payloads.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompressionAlgo {
    /// Standard gzip — universally supported, slower.
    Gzip,
    /// Zstandard level 1 — ~12x faster than gzip for JSON, needs server support.
    Zstd,
}

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
    pub is_sidechain: bool,
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
    pub tool_input_json: Option<&'a RawValue>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_output_text: Option<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<&'a str>,
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
                tool_input_json: e.tool_input_json.as_deref(),
                tool_output_text: e.tool_output_text.as_deref(),
                tool_call_id: e.tool_call_id.as_deref(),
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
        git_repo: metadata.git_repo.as_deref(),
        git_branch: metadata.git_branch.as_deref(),
        started_at,
        ended_at,
        provider_session_id: &metadata.session_id,
        // Allow env var override: agent-mesh sets LONGHOUSE_IS_SIDECHAIN=1 before
        // running sub-agents; the Stop hook inherits it, marking the session as automated.
        is_sidechain: metadata.is_sidechain
            || std::env::var("LONGHOUSE_IS_SIDECHAIN").as_deref() == Ok("1"),
        events: event_ingests,
    }
}

/// Build an IngestPayload and stream-compress it (gzip by default).
///
/// This is THE key optimization: `serde_json::to_writer` writes JSON tokens
/// directly into the compressor's write buffer. At no point is the full
/// JSON string materialized in memory.
pub fn build_and_compress(
    session_id: &str,
    events: &[ParsedEvent],
    metadata: &SessionMetadata,
    source_path: &str,
    provider: &str,
) -> anyhow::Result<Vec<u8>> {
    build_and_compress_with(session_id, events, metadata, source_path, provider, CompressionAlgo::Gzip)
}

/// Build an IngestPayload and stream-compress it with the specified algorithm.
pub fn build_and_compress_with(
    session_id: &str,
    events: &[ParsedEvent],
    metadata: &SessionMetadata,
    source_path: &str,
    provider: &str,
    algo: CompressionAlgo,
) -> anyhow::Result<Vec<u8>> {
    let payload = build_payload(session_id, events, metadata, source_path, provider);
    compress_payload_with(&payload, algo)
}

/// Compress an already-built payload (for benchmarking).
pub fn compress_payload(payload: &IngestPayload<'_>) -> anyhow::Result<Vec<u8>> {
    compress_payload_with(payload, CompressionAlgo::Gzip)
}

/// Compress payload with specified algorithm.
pub fn compress_payload_with(payload: &IngestPayload<'_>, algo: CompressionAlgo) -> anyhow::Result<Vec<u8>> {
    let buf = Vec::with_capacity(64 * 1024);
    match algo {
        CompressionAlgo::Gzip => {
            let mut gz = GzEncoder::new(buf, GzCompression::fast());
            serde_json::to_writer(&mut gz, payload)?;
            Ok(gz.finish()?)
        }
        CompressionAlgo::Zstd => {
            let mut zw = zstd::Encoder::new(buf, 1)?; // level 1 = fast
            serde_json::to_writer(&mut zw, payload)?;
            Ok(zw.finish()?)
        }
    }
}

/// Get the Content-Encoding header value for the algorithm.
pub fn content_encoding(algo: CompressionAlgo) -> &'static str {
    match algo {
        CompressionAlgo::Gzip => "gzip",
        CompressionAlgo::Zstd => "zstd",
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::collections::{BTreeMap, BTreeSet};
    use std::io::{BufRead, BufReader, Read, Seek, SeekFrom};
    use std::path::{Path, PathBuf};

    use super::*;
    use crate::pipeline::parser::Role;
    use chrono::Utc;
    use flate2::read::GzDecoder;

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
                tool_call_id: None,
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
                tool_call_id: None,
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
                tool_call_id: None,
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

    #[test]
    fn test_raw_line_preserves_full_content() {
        // raw_json must preserve the complete original source line.
        let oversized_raw = "x".repeat((32 * 1024) + 1024);
        let events = vec![ParsedEvent {
            uuid: "big".to_string(),
            session_id: "s1".to_string(),
            timestamp: Utc::now(),
            role: Role::User,
            content_text: Some("short content".to_string()),
            tool_name: None,
            tool_input_json: None,
            tool_output_text: None,
            tool_call_id: None,
            source_offset: 0,
            raw_type: "user".to_string(),
            raw_line: Some(oversized_raw),
        }];

        let meta = SessionMetadata {
            session_id: "s1".to_string(),
            ..Default::default()
        };

        let payload = build_payload("test-id", &events, &meta, "/path", "claude");
        let raw_json = payload.events[0].raw_json.expect("raw_json should be present");
        assert_eq!(raw_json, events[0].raw_line.as_deref().unwrap());
    }

    fn golden_fixture(provider: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("golden")
            .join(provider)
            .join("basic.jsonl")
    }

    fn read_source_line_without_newline(path: &Path, offset: u64) -> String {
        let file = std::fs::File::open(path).expect("open fixture");
        let mut reader = BufReader::new(file);
        reader
            .seek(SeekFrom::Start(offset))
            .expect("seek to source offset");

        let mut line = String::new();
        reader.read_line(&mut line).expect("read source line");
        while line.ends_with('\n') || line.ends_with('\r') {
            line.pop();
        }
        line
    }

    fn raw_lines_from_compressed_payload(compressed: &[u8]) -> BTreeMap<u64, String> {
        let mut decoder = GzDecoder::new(compressed);
        let mut json_str = String::new();
        decoder
            .read_to_string(&mut json_str)
            .expect("decompress payload");

        let payload: serde_json::Value =
            serde_json::from_str(&json_str).expect("payload must be valid json");
        let events = payload["events"]
            .as_array()
            .expect("payload.events must be an array");

        let mut by_offset = BTreeMap::new();
        for event in events {
            let Some(raw_json) = event["raw_json"].as_str() else {
                continue;
            };
            let offset = event["source_offset"]
                .as_u64()
                .expect("event.source_offset must be u64");
            let prev = by_offset.insert(offset, raw_json.to_string());
            assert!(
                prev.is_none(),
                "raw_json should appear once per source_offset; duplicate at {}",
                offset
            );
        }
        by_offset
    }

    fn assert_fixture_roundtrip(provider: &str) {
        let path = golden_fixture(provider);
        let parsed = crate::pipeline::parser::parse_session_file(&path, 0).expect("parse fixture");
        assert!(
            !parsed.events.is_empty(),
            "fixture must produce events for roundtrip validation"
        );

        let source_path = path.to_string_lossy().to_string();
        let compressed = build_and_compress(
            &parsed.metadata.session_id,
            &parsed.events,
            &parsed.metadata,
            &source_path,
            provider,
        )
        .expect("build and compress payload");

        let actual = raw_lines_from_compressed_payload(&compressed);
        assert!(
            !actual.is_empty(),
            "payload must include at least one raw_json line"
        );

        let unique_event_offsets: BTreeSet<u64> =
            parsed.events.iter().map(|e| e.source_offset).collect();
        assert_eq!(
            actual.len(),
            unique_event_offsets.len(),
            "each event-bearing source line must roundtrip with one raw_json entry"
        );

        // Byte-for-byte line fidelity: every shipped raw_json line must equal
        // the source file line at the same byte offset.
        for (offset, raw_json) in &actual {
            let expected = read_source_line_without_newline(&path, *offset);
            assert_eq!(
                raw_json, &expected,
                "raw_json mismatch at source_offset={}",
                offset
            );
        }

        // "Unship back to log": reconstruct event-bearing lines from payload raw_json.
        let actual_log = actual.values().cloned().collect::<Vec<_>>().join("\n");
        let expected_log = actual
            .keys()
            .map(|offset| read_source_line_without_newline(&path, *offset))
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(actual_log, expected_log);
    }

    #[test]
    fn test_ship_unship_roundtrip_claude_fixture() {
        assert_fixture_roundtrip("claude");
    }

    #[test]
    fn test_ship_unship_roundtrip_codex_fixture() {
        assert_fixture_roundtrip("codex");
    }
}
