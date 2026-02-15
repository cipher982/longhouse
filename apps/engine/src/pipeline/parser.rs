//! JSONL session file parser for Claude Code, Codex, and Gemini sessions.
//!
//! Mirrors the Python parser at `zerg/services/shipper/parser.py`.
//! Extracts meaningful events (user messages, assistant text, tool calls,
//! tool results) from JSONL files and converts them to a normalized format.

use std::io::{BufRead, BufReader};
use std::path::Path;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use memmap2::Mmap;
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use uuid::Uuid;

/// Threshold for switching from buffered read to mmap (1 MB).
const MMAP_THRESHOLD: u64 = 1_048_576;

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ParsedEvent {
    pub uuid: String,
    pub session_id: String,
    pub timestamp: DateTime<Utc>,
    pub role: Role,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_text: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_input_json: Option<Box<RawValue>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_output_text: Option<String>,
    pub source_offset: u64,
    pub raw_type: String,
    /// Only the first event per source line carries raw_line (dedup).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_line: Option<String>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct SessionMetadata {
    pub session_id: String,
    pub cwd: Option<String>,
    pub git_branch: Option<String>,
    pub project: Option<String>,
    pub version: Option<String>,
    pub started_at: Option<DateTime<Utc>>,
    pub ended_at: Option<DateTime<Utc>>,
}

pub struct ParseResult {
    pub events: Vec<ParsedEvent>,
    pub last_good_offset: u64,
    pub metadata: SessionMetadata,
}

// ---------------------------------------------------------------------------
// Raw deserialization types (minimal — only fields we need)
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct RawLine {
    r#type: Option<String>,
    timestamp: Option<String>,
    uuid: Option<String>,
    cwd: Option<String>,
    #[serde(rename = "gitBranch")]
    git_branch: Option<String>,
    version: Option<String>,
    message: Option<RawMessage>,
}

#[derive(Deserialize)]
struct RawMessage {
    /// Kept as raw JSON — avoids building a full serde_json::Value DOM tree.
    /// Parsed on-demand in extraction functions via ContentItem.
    content: Box<RawValue>,
}

/// Targeted deserialization of a single content array item.
/// Only the fields we actually use are extracted; everything else is skipped.
#[derive(Deserialize)]
struct ContentItem {
    r#type: Option<String>,
    /// Text content (for "text" items)
    text: Option<String>,
    /// Tool name (for "tool_use" items)
    name: Option<String>,
    /// Tool call ID (for "tool_use" items)
    id: Option<String>,
    /// Tool input — kept as raw JSON, never parsed into a Value tree.
    input: Option<Box<RawValue>>,
    /// Tool use ID (for "tool_result" items)
    tool_use_id: Option<String>,
    /// Tool result content — kept as raw JSON, parsed lazily for text extraction.
    #[serde(rename = "content")]
    result_content: Option<Box<RawValue>>,
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Parse a JSONL session file starting from a byte offset.
///
/// Returns events, the last good byte offset (excluding partial lines),
/// and session metadata — all in a single pass.
pub fn parse_session_file(path: &Path, offset: u64) -> Result<ParseResult> {
    let session_id = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("unknown")
        .to_string();

    let file_size = std::fs::metadata(path)
        .with_context(|| format!("Failed to stat {}", path.display()))?
        .len();

    let bytes_to_read = file_size.saturating_sub(offset);

    if bytes_to_read == 0 {
        return Ok(ParseResult {
            events: Vec::new(),
            last_good_offset: offset,
            metadata: SessionMetadata {
                session_id,
                ..Default::default()
            },
        });
    }

    // Choose strategy based on file size
    if file_size > MMAP_THRESHOLD {
        parse_mmap(path, offset, &session_id)
    } else {
        parse_buffered(path, offset, &session_id)
    }
}

// ---------------------------------------------------------------------------
// mmap-based parser (large files)
// ---------------------------------------------------------------------------

fn parse_mmap(path: &Path, offset: u64, session_id: &str) -> Result<ParseResult> {
    let file = std::fs::File::open(path)
        .with_context(|| format!("Failed to open {}", path.display()))?;

    let mmap = unsafe { Mmap::map(&file) }
        .with_context(|| format!("Failed to mmap {}", path.display()))?;

    let data = if (offset as usize) < mmap.len() {
        &mmap[offset as usize..]
    } else {
        return Ok(ParseResult {
            events: Vec::new(),
            last_good_offset: offset,
            metadata: SessionMetadata {
                session_id: session_id.to_string(),
                ..Default::default()
            },
        });
    };

    let mut events = Vec::new();
    let mut metadata = SessionMetadata {
        session_id: session_id.to_string(),
        ..Default::default()
    };
    let mut min_ts: Option<DateTime<Utc>> = None;
    let mut max_ts: Option<DateTime<Utc>> = None;
    let mut last_good_offset = offset;

    let mut pos: usize = 0;
    while pos < data.len() {
        // Find end of line
        let line_start = pos;
        let line_end = match data[pos..].iter().position(|&b| b == b'\n') {
            Some(nl) => pos + nl,
            None => {
                // No newline — partial line at EOF, don't advance offset
                break;
            }
        };

        let line_offset = offset + line_start as u64;
        let after_line = offset + line_end as u64 + 1; // past the \n

        let line_bytes = &data[line_start..line_end];
        pos = line_end + 1;

        // Skip empty/whitespace lines
        let trimmed = trim_bytes(line_bytes);
        if trimmed.is_empty() {
            last_good_offset = after_line;
            continue;
        }

        // Parse JSON
        let obj: RawLine = match serde_json::from_slice(trimmed) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(offset = line_offset, error = %e, "Failed to parse JSON line");
                // Still advance — the line is complete, just malformed
                last_good_offset = after_line;
                continue;
            }
        };

        last_good_offset = after_line;

        // Collect metadata
        collect_metadata(&obj, &mut metadata, &mut min_ts, &mut max_ts);

        // Extract events — pass raw bytes, convert to string only when needed
        let line_str = std::str::from_utf8(trimmed).unwrap_or("");
        extract_events(
            &obj,
            session_id,
            line_offset,
            line_str,
            &mut events,
        );
    }

    // Finalize metadata
    metadata.started_at = min_ts;
    metadata.ended_at = max_ts;
    if let Some(ref cwd) = metadata.cwd {
        metadata.project = Path::new(cwd)
            .file_name()
            .and_then(|s| s.to_str())
            .map(|s| s.to_string());
    }

    Ok(ParseResult {
        events,
        last_good_offset,
        metadata,
    })
}

// ---------------------------------------------------------------------------
// Buffered reader parser (small files)
// ---------------------------------------------------------------------------

fn parse_buffered(path: &Path, offset: u64, session_id: &str) -> Result<ParseResult> {
    let mut file = std::fs::File::open(path)
        .with_context(|| format!("Failed to open {}", path.display()))?;

    if offset > 0 {
        use std::io::Seek;
        file.seek(std::io::SeekFrom::Start(offset))?;
    }

    let reader = BufReader::with_capacity(64 * 1024, file);

    let mut events = Vec::new();
    let mut metadata = SessionMetadata {
        session_id: session_id.to_string(),
        ..Default::default()
    };
    let mut min_ts: Option<DateTime<Utc>> = None;
    let mut max_ts: Option<DateTime<Utc>> = None;
    let mut current_offset = offset;

    for line_result in reader.lines() {
        let line = match line_result {
            Ok(l) => l,
            Err(e) => {
                tracing::warn!(offset = current_offset, error = %e, "Failed to read line");
                break; // IO error — stop processing
            }
        };

        let line_offset = current_offset;
        // BufReader.lines() strips \n, so add 1 for the newline byte
        current_offset += line.len() as u64 + 1;

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let obj: RawLine = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(e) => {
                tracing::warn!(offset = line_offset, error = %e, "Failed to parse JSON line");
                continue;
            }
        };

        collect_metadata(&obj, &mut metadata, &mut min_ts, &mut max_ts);

        extract_events(
            &obj,
            session_id,
            line_offset,
            trimmed,
            &mut events,
        );
    }

    metadata.started_at = min_ts;
    metadata.ended_at = max_ts;
    if let Some(ref cwd) = metadata.cwd {
        metadata.project = Path::new(cwd)
            .file_name()
            .and_then(|s| s.to_str())
            .map(|s| s.to_string());
    }

    Ok(ParseResult {
        events,
        last_good_offset: current_offset,
        metadata,
    })
}

// ---------------------------------------------------------------------------
// Shared extraction logic
// ---------------------------------------------------------------------------

fn collect_metadata(
    obj: &RawLine,
    meta: &mut SessionMetadata,
    min_ts: &mut Option<DateTime<Utc>>,
    max_ts: &mut Option<DateTime<Utc>>,
) {
    if meta.cwd.is_none() {
        if let Some(ref cwd) = obj.cwd {
            meta.cwd = Some(cwd.clone());
        }
    }
    if meta.git_branch.is_none() {
        if let Some(ref branch) = obj.git_branch {
            meta.git_branch = Some(branch.clone());
        }
    }
    if meta.version.is_none() {
        if let Some(ref ver) = obj.version {
            meta.version = Some(ver.clone());
        }
    }
    if let Some(ts) = obj.timestamp.as_deref().and_then(parse_timestamp) {
        match min_ts {
            Some(ref existing) if ts < *existing => *min_ts = Some(ts),
            None => *min_ts = Some(ts),
            _ => {}
        }
        match max_ts {
            Some(ref existing) if ts > *existing => *max_ts = Some(ts),
            None => *max_ts = Some(ts),
            _ => {}
        }
    }
}

fn extract_events(
    obj: &RawLine,
    session_id: &str,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
) {
    let event_type = obj.r#type.as_deref().unwrap_or("");

    // Skip metadata-only types
    match event_type {
        "summary" | "file-history-snapshot" | "progress" => return,
        _ => {}
    }

    let timestamp = obj
        .timestamp
        .as_deref()
        .and_then(parse_timestamp)
        .unwrap_or_else(Utc::now);

    let msg_uuid = obj
        .uuid
        .as_deref()
        .unwrap_or("")
        .to_string();
    let msg_uuid = if msg_uuid.is_empty() {
        Uuid::new_v4().to_string()
    } else {
        msg_uuid
    };

    let content_raw = match &obj.message {
        Some(m) => &m.content,
        None => return,
    };

    // Parse content items from raw JSON on-demand.
    // This is where the RawValue optimization pays off: the initial RawLine
    // parse skipped building a Value tree for content entirely. Now we parse
    // only the fields we need via ContentItem.
    let content_str = content_raw.get();

    match event_type {
        "user" => {
            extract_user_events(
                content_str,
                session_id,
                &msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
            );
        }
        "assistant" => {
            extract_assistant_events(
                content_str,
                session_id,
                &msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
            );
        }
        _ => {
            // Unknown type — skip
        }
    }
}

fn extract_user_events(
    content_str: &str,
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
) {
    // Try parsing as array of ContentItems
    if let Ok(items) = serde_json::from_str::<Vec<ContentItem>>(content_str) {
        // Check if any items are tool_results
        let has_tool_result = items.iter().any(|item| {
            item.r#type.as_deref() == Some("tool_result")
        });

        if has_tool_result {
            extract_tool_results_from_items(
                &items,
                session_id,
                msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
            );
        } else {
            // Regular user message — extract text from items
            let text = extract_user_content_from_items(&items);
            if let Some(text) = text {
                if !text.trim().is_empty() {
                    events.push(ParsedEvent {
                        uuid: msg_uuid.to_string(),
                        session_id: session_id.to_string(),
                        timestamp,
                        role: Role::User,
                        content_text: Some(text),
                        tool_name: None,
                        tool_input_json: None,
                        tool_output_text: None,
                        source_offset: line_offset,
                        raw_type: "user".to_string(),
                        raw_line: Some(raw_line.to_string()),
                    });
                }
            }
        }
    } else if let Ok(text) = serde_json::from_str::<String>(content_str) {
        // Plain string content
        if !text.trim().is_empty() {
            events.push(ParsedEvent {
                uuid: msg_uuid.to_string(),
                session_id: session_id.to_string(),
                timestamp,
                role: Role::User,
                content_text: Some(text),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                source_offset: line_offset,
                raw_type: "user".to_string(),
                raw_line: Some(raw_line.to_string()),
            });
        }
    }
}

fn extract_assistant_events(
    content_str: &str,
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
) {
    let items: Vec<ContentItem> = match serde_json::from_str(content_str) {
        Ok(v) => v,
        Err(_) => return,
    };

    let mut first = true;
    for (idx, item) in items.iter().enumerate() {
        let item_type = item.r#type.as_deref().unwrap_or("");

        match item_type {
            "text" => {
                let text = item.text.as_deref().unwrap_or("");
                if !text.trim().is_empty() {
                    events.push(ParsedEvent {
                        uuid: format!("{}-text-{}", msg_uuid, idx),
                        session_id: session_id.to_string(),
                        timestamp,
                        role: Role::Assistant,
                        content_text: Some(text.to_string()),
                        tool_name: None,
                        tool_input_json: None,
                        tool_output_text: None,
                        source_offset: line_offset,
                        raw_type: "assistant".to_string(),
                        raw_line: if first {
                            first = false;
                            Some(raw_line.to_string())
                        } else {
                            None
                        },
                    });
                }
            }
            "tool_use" => {
                let tool_name = item.name.as_deref().unwrap_or("").to_string();
                let tool_id = item.id.as_deref().unwrap_or("");
                let uuid_suffix = if tool_id.is_empty() {
                    format!("{}", idx)
                } else {
                    tool_id.to_string()
                };

                // tool_input stays as Box<RawValue> — zero-copy pass-through
                let tool_input = item.input.as_ref().and_then(|raw| {
                    // Only keep if it's a JSON object (starts with '{')
                    let s = raw.get().trim();
                    if s.starts_with('{') {
                        // Clone the RawValue box (just copies the string, not a DOM tree)
                        Some(raw.clone())
                    } else {
                        None
                    }
                });

                events.push(ParsedEvent {
                    uuid: format!("{}-tool-{}", msg_uuid, uuid_suffix),
                    session_id: session_id.to_string(),
                    timestamp,
                    role: Role::Assistant,
                    content_text: None,
                    tool_name: Some(tool_name),
                    tool_input_json: tool_input,
                    tool_output_text: None,
                    source_offset: line_offset,
                    raw_type: "assistant".to_string(),
                    raw_line: if first {
                        first = false;
                        Some(raw_line.to_string())
                    } else {
                        None
                    },
                });
            }
            _ => {
                // thinking, etc. — skip
            }
        }
    }
}

fn extract_tool_results_from_items(
    items: &[ContentItem],
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
) {
    let mut first = true;
    for (idx, item) in items.iter().enumerate() {
        if item.r#type.as_deref() != Some("tool_result") {
            continue;
        }

        let tool_use_id = item.tool_use_id.as_deref().unwrap_or("");
        let uuid_suffix = if tool_use_id.is_empty() {
            format!("{}", idx)
        } else {
            tool_use_id.to_string()
        };

        let result_text = item.result_content.as_ref().and_then(|raw| {
            extract_text_from_raw_content(raw.get())
        });

        if let Some(text) = result_text {
            if !text.is_empty() {
                events.push(ParsedEvent {
                    uuid: format!("{}-result-{}", msg_uuid, uuid_suffix),
                    session_id: session_id.to_string(),
                    timestamp,
                    role: Role::Tool,
                    content_text: None,
                    tool_name: None,
                    tool_input_json: None,
                    tool_output_text: Some(text),
                    source_offset: line_offset,
                    raw_type: "tool_result".to_string(),
                    raw_line: if first {
                        first = false;
                        Some(raw_line.to_string())
                    } else {
                        None
                    },
                });
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Content extraction helpers
// ---------------------------------------------------------------------------

fn extract_user_content_from_items(items: &[ContentItem]) -> Option<String> {
    let mut parts = Vec::new();
    for item in items {
        match item.r#type.as_deref() {
            Some("text") => {
                if let Some(ref text) = item.text {
                    parts.push(text.clone());
                }
            }
            Some("tool_result") => {
                if let Some(ref raw) = item.result_content {
                    if let Some(text) = extract_text_from_raw_content(raw.get()) {
                        parts.push(text);
                    }
                }
            }
            _ => {}
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("\n"))
    }
}

/// Extract text from a raw JSON content field (tool_result content).
/// Handles: plain string, array of {type: "text", text: "..."}, or fallback to raw JSON.
fn extract_text_from_raw_content(raw_json: &str) -> Option<String> {
    let trimmed = raw_json.trim();

    // Plain string: "some text"
    if trimmed.starts_with('"') {
        if let Ok(s) = serde_json::from_str::<String>(trimmed) {
            return Some(s);
        }
    }

    // Array of content parts
    if trimmed.starts_with('[') {
        #[derive(Deserialize)]
        struct TextPart {
            r#type: Option<String>,
            text: Option<String>,
        }

        if let Ok(parts) = serde_json::from_str::<Vec<TextPart>>(trimmed) {
            let mut texts = Vec::new();
            for part in &parts {
                if part.r#type.as_deref() == Some("text") {
                    if let Some(ref text) = part.text {
                        texts.push(text.clone());
                    }
                }
            }
            if texts.is_empty() {
                return None;
            }
            return Some(texts.join("\n"));
        }
    }

    // Fallback: raw JSON as string
    Some(trimmed.to_string())
}

// ---------------------------------------------------------------------------
// Timestamp parsing
// ---------------------------------------------------------------------------

fn parse_timestamp(ts: &str) -> Option<DateTime<Utc>> {
    if ts.is_empty() {
        return None;
    }

    // Try RFC3339 first (most common)
    if let Ok(dt) = DateTime::parse_from_rfc3339(ts) {
        return Some(dt.with_timezone(&Utc));
    }

    // Handle "Z" suffix → "+00:00"
    let normalized = if ts.ends_with('Z') {
        format!("{}+00:00", &ts[..ts.len() - 1])
    } else {
        ts.to_string()
    };

    DateTime::parse_from_rfc3339(&normalized)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
}

// ---------------------------------------------------------------------------
// Byte utilities
// ---------------------------------------------------------------------------

fn trim_bytes(bytes: &[u8]) -> &[u8] {
    let start = bytes.iter().position(|&b| !b.is_ascii_whitespace()).unwrap_or(bytes.len());
    let end = bytes.iter().rposition(|&b| !b.is_ascii_whitespace()).map_or(start, |p| p + 1);
    &bytes[start..end]
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn make_jsonl_file(dir: &Path, name: &str, lines: &[&str]) -> std::path::PathBuf {
        let path = dir.join(name);
        let mut f = std::fs::File::create(&path).unwrap();
        for line in lines {
            writeln!(f, "{}", line).unwrap();
        }
        path
    }

    #[test]
    fn test_parse_user_message() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Hello world"},"cwd":"/home/user/project","gitBranch":"main"}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[0].content_text.as_deref(), Some("Hello world"));
        assert_eq!(result.metadata.cwd.as_deref(), Some("/home/user/project"));
        assert_eq!(result.metadata.git_branch.as_deref(), Some("main"));
        assert_eq!(result.metadata.project.as_deref(), Some("project"));
    }

    #[test]
    fn test_parse_assistant_text_and_tool() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"Let me check"},{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/tmp/foo"}}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);

        // First event: text
        assert_eq!(result.events[0].role, Role::Assistant);
        assert_eq!(result.events[0].content_text.as_deref(), Some("Let me check"));
        assert!(result.events[0].raw_line.is_some(), "First event should have raw_line");

        // Second event: tool_use
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(result.events[1].tool_name.as_deref(), Some("Read"));
        assert!(result.events[1].raw_line.is_none(), "Second event should NOT have raw_line");
    }

    #[test]
    fn test_raw_line_dedup() {
        let dir = tempfile::tempdir().unwrap();
        // Assistant line with 3 content items → should yield 3 events, only first has raw_line
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"one"},{"type":"text","text":"two"},{"type":"text","text":"three"}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 3);
        assert!(result.events[0].raw_line.is_some());
        assert!(result.events[1].raw_line.is_none());
        assert!(result.events[2].raw_line.is_none());
    }

    #[test]
    fn test_tool_result_extraction() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"file contents here"}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(result.events[0].tool_output_text.as_deref(), Some("file contents here"));
    }

    #[test]
    fn test_skip_metadata_types() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"summary","timestamp":"2026-01-01T00:00:00Z"}"#,
                r#"{"type":"file-history-snapshot","timestamp":"2026-01-01T00:00:01Z"}"#,
                r#"{"type":"progress","timestamp":"2026-01-01T00:00:02Z"}"#,
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:03Z","message":{"content":"real message"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].content_text.as_deref(), Some("real message"));
    }

    #[test]
    fn test_offset_resume() {
        let dir = tempfile::tempdir().unwrap();
        let line1 = r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"first"}}"#;
        let line2 = r#"{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:01Z","message":{"content":"second"}}"#;
        let path = make_jsonl_file(dir.path(), "test-session.jsonl", &[line1, line2]);

        // Parse from offset past first line
        let offset = (line1.len() + 1) as u64; // +1 for newline
        let result = parse_session_file(&path, offset).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].content_text.as_deref(), Some("second"));
    }

    #[test]
    fn test_partial_line_at_eof() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test-session.jsonl");
        {
            let mut f = std::fs::File::create(&path).unwrap();
            // Complete line + partial line (no trailing newline)
            write!(
                f,
                "{}\n{}",
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"complete"}}"#,
                r#"{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:01Z","message":{"con"#
            )
            .unwrap();
        }

        let result = parse_session_file(&path, 0).unwrap();
        // mmap parser: only the complete line should be parsed
        // The partial line has no \n so it's treated as incomplete
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].content_text.as_deref(), Some("complete"));
    }

    #[test]
    fn test_metadata_timestamps() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T10:00:00Z","message":{"content":"early"},"cwd":"/proj","version":"1.0"}"#,
                r#"{"type":"user","uuid":"u2","timestamp":"2026-01-01T12:00:00Z","message":{"content":"late"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert!(result.metadata.started_at.is_some());
        assert!(result.metadata.ended_at.is_some());
        assert!(result.metadata.started_at.unwrap() < result.metadata.ended_at.unwrap());
        assert_eq!(result.metadata.version.as_deref(), Some("1.0"));
    }
}
