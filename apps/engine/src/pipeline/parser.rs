//! Session file parser for Claude Code, Codex, and Gemini sessions.
//!
//! Mirrors the Python parser at `zerg/services/shipper/parser.py`.
//! Extracts meaningful events (user messages, assistant text, tool calls,
//! tool results) from session files and converts them to a normalized format.
//!
//! Supported formats (dispatched by file extension):
//! - **Claude** (`.jsonl`): `{type: "user"|"assistant", message: {content: ...}}`
//! - **Codex** (`.jsonl`): `{type: "response_item", payload: {type: "message"|"function_call"|..., role: ..., content: [...]}}`
//! - **Gemini** (`.json`): `{sessionId, messages: [{type: "user"|"gemini", content, toolCalls: [...]}]}`
//!
//! Gemini files are full JSON documents rewritten in-place (not JSONL appended),
//! so they are always parsed from offset 0. The backend deduplicates events by hash.

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
    /// Cross-provider call/result linkage ID.
    /// - Claude tool_use call:   item.id  (e.g. "toolu_bdrk_01...")
    /// - Claude tool_result:     item.tool_use_id
    /// - Codex function_call:    payload.call_id
    /// - Codex function_output:  payload.call_id
    /// - Gemini tool_call:       tc.id
    /// None for all non-tool events and where provider doesn't emit an ID.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
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
    pub git_repo: Option<String>,
    pub project: Option<String>,
    pub version: Option<String>,
    pub started_at: Option<DateTime<Utc>>,
    pub ended_at: Option<DateTime<Utc>>,
    pub is_sidechain: bool,
}

pub struct ParseResult {
    pub events: Vec<ParsedEvent>,
    pub last_good_offset: u64,
    pub metadata: SessionMetadata,
    /// Number of records that appeared to contain parseable content.
    /// Used by the shipper to detect suspicious zero-event outcomes:
    /// if candidate_records > 0 but events is empty, something likely went wrong.
    pub candidate_records: usize,
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
    #[serde(rename = "isSidechain")]
    is_sidechain: Option<bool>,
    /// Claude format: `{message: {content: ...}}`
    message: Option<RawMessage>,
    /// Codex format: `{payload: {type: ..., role: ..., content: [...]}}`
    payload: Option<CodexPayload>,
}

#[derive(Deserialize)]
struct RawMessage {
    /// Kept as raw JSON — avoids building a full serde_json::Value DOM tree.
    /// Parsed on-demand in extraction functions via ContentItem.
    content: Box<RawValue>,
}

// ---------------------------------------------------------------------------
// Codex-specific types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct CodexGitInfo {
    branch: Option<String>,
    repository_url: Option<String>,
}

#[derive(Deserialize)]
struct CodexPayload {
    r#type: Option<String>,
    role: Option<String>,
    /// For message types: array of content items
    content: Option<Vec<CodexContentItem>>,
    /// session_meta: session UUID
    id: Option<String>,
    /// session_meta: working directory
    cwd: Option<String>,
    /// session_meta: git info (branch + remote URL)
    git: Option<CodexGitInfo>,
    /// session_meta: CLI version
    cli_version: Option<String>,
    /// function_call: tool name
    name: Option<String>,
    /// function_call: JSON-encoded arguments
    arguments: Option<String>,
    /// function_call / function_call_output: call correlation ID
    call_id: Option<String>,
    /// function_call_output: result text
    output: Option<String>,
}

#[derive(Deserialize)]
struct CodexContentItem {
    r#type: Option<String>,
    text: Option<String>,
}

// ---------------------------------------------------------------------------
// Gemini-specific types
// ---------------------------------------------------------------------------

/// Top-level Gemini session document.
#[derive(Deserialize)]
struct GeminiSession {
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
    #[serde(rename = "startTime")]
    start_time: Option<String>,
    messages: Option<Vec<GeminiMessage>>,
}

/// A single message in a Gemini session.
#[derive(Deserialize)]
struct GeminiMessage {
    id: Option<String>,
    timestamp: Option<String>,
    /// "user" or "gemini"
    r#type: Option<String>,
    /// Content is normally a string but may be an object/array in newer Gemini
    /// CLI versions. Accept any JSON value and extract text defensively.
    content: Option<serde_json::Value>,
    #[serde(rename = "toolCalls")]
    tool_calls: Option<Vec<GeminiToolCall>>,
}

/// Extract a plain-text string from a Gemini content value.
/// Returns `None` (skip event) if no text can be extracted.
fn extract_gemini_text(v: &serde_json::Value) -> Option<String> {
    match v {
        serde_json::Value::String(s) => {
            let t = s.trim().to_string();
            if t.is_empty() { None } else { Some(t) }
        }
        serde_json::Value::Array(arr) => {
            // Try to concatenate "text" fields from a parts array
            let text = arr
                .iter()
                .filter_map(|item| item.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("");
            if text.trim().is_empty() { None } else { Some(text.trim().to_string()) }
        }
        serde_json::Value::Object(obj) => {
            // Try common text field names
            obj.get("text")
                .or_else(|| obj.get("parts"))
                .and_then(|v| extract_gemini_text(v))
        }
        _ => None,
    }
}

/// A tool call inside a Gemini message.
#[derive(Deserialize)]
struct GeminiToolCall {
    id: Option<String>,
    name: Option<String>,
    args: Option<serde_json::Value>,
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
    /// Error flag on tool_result items (true = tool call failed/rejected)
    is_error: Option<bool>,
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Parse a session file starting from a byte offset.
///
/// Dispatches to the appropriate parser based on file extension:
/// - `.json` → Gemini full-document parser (offset is ignored; always parses from 0)
/// - `.jsonl` (or any other) → JSONL line-by-line parser (Claude/Codex)
///
/// Returns events, the last good byte offset (excluding partial lines),
/// and session metadata.
pub fn parse_session_file(path: &Path, offset: u64) -> Result<ParseResult> {
    let is_gemini = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.eq_ignore_ascii_case("json"))
        .unwrap_or(false);

    let raw_stem = path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("unknown")
        .to_string();

    // Ensure session_id is a valid UUID. Non-UUID stems (e.g. "agent-a51c878")
    // get a deterministic UUID v5 derived from the full file path.
    let session_id = if Uuid::parse_str(&raw_stem).is_ok() {
        raw_stem
    } else {
        Uuid::new_v5(&Uuid::NAMESPACE_URL, path.to_string_lossy().as_bytes()).to_string()
    };

    // Gemini: full JSON document, always parse from 0 (file is rewritten in place)
    if is_gemini {
        return parse_gemini_json(path, &session_id);
    }

    let file_size = std::fs::metadata(path)
        .with_context(|| format!("Failed to stat {}", path.display()))?
        .len();

    let bytes_to_read = file_size.saturating_sub(offset);

    if bytes_to_read == 0 {
        return Ok(ParseResult {
            events: Vec::new(),
            last_good_offset: offset,
            candidate_records: 0,
            metadata: SessionMetadata {
                session_id,
                ..Default::default()
            },
        });
    }

    // JSONL: choose strategy based on file size
    if file_size > MMAP_THRESHOLD {
        parse_mmap(path, offset, &session_id)
    } else {
        parse_buffered(path, offset, &session_id)
    }
}

// ---------------------------------------------------------------------------
// Gemini JSON parser
// ---------------------------------------------------------------------------

/// Parse a Gemini session file (full JSON document, not JSONL).
///
/// Gemini stores one session per `.json` file inside
/// `~/.gemini/tmp/<projectHash>/chats/session-<timestamp>-<id>.json`.
/// The file is a single JSON object with a `messages` array — it is
/// rewritten in its entirety on every update (not appended).  We
/// therefore always parse from offset 0 and return `file_size` as
/// `last_good_offset`.  The ingest backend deduplicates events by hash,
/// so re-shipping an unchanged session is harmless.
fn parse_gemini_json(path: &Path, session_id: &str) -> Result<ParseResult> {
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("Failed to read {}", path.display()))?;
    let file_size = content.len() as u64;

    let session: GeminiSession = match serde_json::from_str(&content) {
        Ok(s) => s,
        Err(e) => {
            tracing::debug!(path = %path.display(), error = %e, "Failed to parse Gemini JSON");
            return Ok(ParseResult {
                events: Vec::new(),
                last_good_offset: file_size,
                candidate_records: 0,
                metadata: SessionMetadata {
                    session_id: session_id.to_string(),
                    ..Default::default()
                },
            });
        }
    };

    // Use the sessionId from the document if it's a valid UUID; otherwise keep stem-derived.
    let canonical_session_id = session
        .session_id
        .as_deref()
        .filter(|id| Uuid::parse_str(id).is_ok())
        .unwrap_or(session_id)
        .to_string();

    let mut events = Vec::new();
    let mut metadata = SessionMetadata {
        session_id: canonical_session_id.clone(),
        ..Default::default()
    };

    if let Some(start_time) = session.start_time.as_deref() {
        metadata.started_at = parse_timestamp(start_time);
    }

    let messages = session.messages.unwrap_or_default();
    let candidate_records = messages.len();

    for msg in messages {
        let msg_type = msg.r#type.as_deref().unwrap_or("");
        let msg_id = msg
            .id
            .as_deref()
            .filter(|id| Uuid::parse_str(id).is_ok())
            .map(|id| id.to_string())
            .unwrap_or_else(|| Uuid::new_v4().to_string());

        let timestamp = msg
            .timestamp
            .as_deref()
            .and_then(parse_timestamp)
            .unwrap_or_else(Utc::now);

        // Track session end time
        match metadata.ended_at {
            Some(ref existing) if timestamp > *existing => metadata.ended_at = Some(timestamp),
            None => metadata.ended_at = Some(timestamp),
            _ => {}
        }

        match msg_type {
            "user" => {
                let text = msg.content.as_ref().and_then(extract_gemini_text);
                if let Some(text) = text {
                    events.push(ParsedEvent {
                        uuid: msg_id.clone(),
                        session_id: canonical_session_id.clone(),
                        timestamp,
                        role: Role::User,
                        content_text: Some(text),
                        tool_name: None,
                        tool_input_json: None,
                        tool_output_text: None,
                        tool_call_id: None,
                        source_offset: 0,
                        raw_type: "gemini_user".to_string(),
                        raw_line: None,
                    });
                }
            }
            "gemini" => {
                // Assistant text response
                let text = msg.content.as_ref().and_then(extract_gemini_text);
                if let Some(text) = text {
                    events.push(ParsedEvent {
                        uuid: msg_id.clone(),
                        session_id: canonical_session_id.clone(),
                        timestamp,
                        role: Role::Assistant,
                        content_text: Some(text),
                        tool_name: None,
                        tool_input_json: None,
                        tool_output_text: None,
                        tool_call_id: None,
                        source_offset: 0,
                        raw_type: "gemini_assistant".to_string(),
                        raw_line: None,
                    });
                }

                // Tool calls embedded in the assistant message
                for (idx, tc) in msg.tool_calls.unwrap_or_default().into_iter().enumerate() {
                    let tc_name = tc.name.as_deref().unwrap_or("").to_string();
                    if tc_name.is_empty() {
                        continue;
                    }
                    let tc_id = tc
                        .id
                        .as_deref()
                        .map(|id| id.to_string())
                        .unwrap_or_else(|| format!("{}", idx));

                    let tool_input = tc.args.map(|v| {
                        RawValue::from_string(v.to_string()).ok()
                    }).flatten();

                    let gemini_tc_id = tc.id.as_deref().filter(|s| !s.is_empty()).map(|s| s.to_string());
                    events.push(ParsedEvent {
                        uuid: format!("{}-tool-{}", msg_id, tc_id),
                        session_id: canonical_session_id.clone(),
                        timestamp,
                        role: Role::Assistant,
                        content_text: None,
                        tool_name: Some(tc_name),
                        tool_input_json: tool_input,
                        tool_output_text: None,
                        tool_call_id: gemini_tc_id,
                        source_offset: 0,
                        raw_type: "gemini_tool_call".to_string(),
                        raw_line: None,
                    });
                }
            }
            _ => {
                // Unknown message type — skip
            }
        }
    }

    Ok(ParseResult {
        events,
        last_good_offset: file_size,
        candidate_records,
        metadata,
    })
}

// ---------------------------------------------------------------------------
// Git repo detection
// ---------------------------------------------------------------------------

/// Walk up from `cwd` to find the nearest `.git/` directory.
/// Returns the path to the `.git` directory if found.
fn find_git_dir(cwd: &Path) -> Option<std::path::PathBuf> {
    let mut dir = cwd;
    loop {
        let candidate = dir.join(".git");
        if candidate.exists() {
            return Some(candidate);
        }
        dir = dir.parent()?;
    }
}

/// Parse the `url` of the `[remote "origin"]` section from a `.git/config` file.
fn read_git_remote_url(git_config: &std::path::Path) -> Option<String> {
    let content = std::fs::read_to_string(git_config).ok()?;
    let mut in_origin = false;
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            in_origin = trimmed == r#"[remote "origin"]"#;
            continue;
        }
        if in_origin {
            if let Some(rest) = trimmed.strip_prefix("url") {
                if let Some(url) = rest.trim_start().strip_prefix('=') {
                    return Some(url.trim().to_string());
                }
            }
        }
    }
    None
}

/// Resolve `git_repo` (remote origin URL) and the canonical `project` name
/// (git root folder name) from a working directory path.
///
/// Returns `(project, git_repo)` — either may be `None`.
fn resolve_git_info(cwd: &Path) -> (Option<String>, Option<String>) {
    let git_dir = match find_git_dir(cwd) {
        Some(d) => d,
        None => {
            // No git repo — fall back to cwd basename
            let project = cwd
                .file_name()
                .and_then(|s| s.to_str())
                .map(|s| s.to_string());
            return (project, None);
        }
    };

    // git root = parent of .git dir
    let git_root = git_dir.parent().unwrap_or(cwd);
    let project = git_root
        .file_name()
        .and_then(|s| s.to_str())
        .map(|s| s.to_string());

    // Read remote URL from .git/config
    let git_repo = read_git_remote_url(&git_dir.join("config"));

    (project, git_repo)
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
            candidate_records: 0,
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
    let mut candidate_lines: usize = 0;

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

        candidate_lines += 1;

        // Parse JSON
        let obj: RawLine = match serde_json::from_slice(trimmed) {
            Ok(v) => v,
            Err(e) => {
                tracing::debug!(offset = line_offset, error = %e, "Failed to parse JSON line");
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
        let (project, git_repo) = resolve_git_info(Path::new(cwd));
        metadata.project = project;
        // Only use disk-resolved git_repo if session_meta didn't already
        // provide one (e.g. Codex sessions carry it in the payload).
        if metadata.git_repo.is_none() {
            metadata.git_repo = git_repo;
        }
    }

    Ok(ParseResult {
        events,
        last_good_offset,
        candidate_records: candidate_lines,
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
    let mut candidate_lines: usize = 0;

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

        candidate_lines += 1;

        let obj: RawLine = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(e) => {
                tracing::debug!(offset = line_offset, error = %e, "Failed to parse JSON line");
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
        let (project, git_repo) = resolve_git_info(Path::new(cwd));
        metadata.project = project;
        // Only use disk-resolved git_repo if session_meta didn't already
        // provide one (e.g. Codex sessions carry it in the payload).
        if metadata.git_repo.is_none() {
            metadata.git_repo = git_repo;
        }
    }

    Ok(ParseResult {
        events,
        last_good_offset: current_offset,
        candidate_records: candidate_lines,
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
    // Claude metadata fields
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

    // Codex session_meta: extract cwd, version, session_id, and git info from payload
    if obj.r#type.as_deref() == Some("session_meta") {
        if let Some(ref payload) = obj.payload {
            if meta.cwd.is_none() {
                if let Some(ref cwd) = payload.cwd {
                    meta.cwd = Some(cwd.clone());
                }
            }
            if meta.version.is_none() {
                if let Some(ref ver) = payload.cli_version {
                    meta.version = Some(ver.clone());
                }
            }
            // Override session_id with the canonical one from session_meta
            if let Some(ref id) = payload.id {
                if Uuid::parse_str(id).is_ok() {
                    meta.session_id = id.clone();
                }
            }
            // Extract git branch and remote URL directly from session_meta.
            // These are authoritative — no need to read .git/config from disk.
            if let Some(ref git) = payload.git {
                if meta.git_branch.is_none() {
                    if let Some(ref branch) = git.branch {
                        meta.git_branch = Some(branch.clone());
                    }
                }
                if meta.git_repo.is_none() {
                    if let Some(ref url) = git.repository_url {
                        meta.git_repo = Some(url.clone());
                    }
                }
            }
        }
    }

    // Once-true-stays-true: any line with isSidechain:true marks the whole session
    if obj.is_sidechain == Some(true) {
        meta.is_sidechain = true;
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

    // Skip metadata-only types (Claude + Codex)
    match event_type {
        "summary" | "file-history-snapshot" | "progress"
        | "session_meta" | "turn_context" | "event_msg" => return,
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

    // Codex format: {type: "response_item", payload: {...}}
    if event_type == "response_item" {
        if let Some(ref payload) = obj.payload {
            extract_codex_events(payload, session_id, &msg_uuid, timestamp, line_offset, raw_line, events);
        }
        return;
    }

    // Claude format: {type: "user"|"assistant", message: {content: ...}}
    let content_raw = match &obj.message {
        Some(m) => &m.content,
        None => return,
    };

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

// ---------------------------------------------------------------------------
// Codex extraction
// ---------------------------------------------------------------------------

fn extract_codex_events(
    payload: &CodexPayload,
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
) {
    let payload_type = payload.r#type.as_deref().unwrap_or("");

    match payload_type {
        "message" => {
            let role_str = payload.role.as_deref().unwrap_or("");
            // developer messages are system context — skip
            if role_str == "developer" {
                return;
            }

            let role = match role_str {
                "user" => Role::User,
                "assistant" => Role::Assistant,
                _ => return,
            };

            // Extract text from content items
            let text = payload
                .content
                .as_ref()
                .map(|items| {
                    items
                        .iter()
                        .filter_map(|item| {
                            let t = item.r#type.as_deref().unwrap_or("");
                            if t == "input_text" || t == "output_text" {
                                item.text.as_deref()
                            } else {
                                None
                            }
                        })
                        .collect::<Vec<_>>()
                        .join("\n")
                })
                .unwrap_or_default();

            if text.trim().is_empty() {
                return;
            }

            events.push(ParsedEvent {
                uuid: msg_uuid.to_string(),
                session_id: session_id.to_string(),
                timestamp,
                role,
                content_text: Some(text),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset: line_offset,
                raw_type: format!("codex_{}", role_str),
                raw_line: Some(raw_line.to_string()),
            });
        }
        "function_call" => {
            let tool_name = payload.name.as_deref().unwrap_or("").to_string();
            let call_id = payload.call_id.as_deref().unwrap_or("");
            let uuid_suffix = if call_id.is_empty() { "0" } else { call_id };

            // Parse arguments string as raw JSON
            let tool_input = payload.arguments.as_ref().and_then(|args| {
                let trimmed = args.trim();
                if trimmed.starts_with('{') {
                    RawValue::from_string(trimmed.to_string()).ok()
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
                tool_call_id: if call_id.is_empty() { None } else { Some(call_id.to_string()) },
                source_offset: line_offset,
                raw_type: "codex_function_call".to_string(),
                raw_line: Some(raw_line.to_string()),
            });
        }
        "function_call_output" => {
            let call_id = payload.call_id.as_deref().unwrap_or("");
            let uuid_suffix = if call_id.is_empty() { "0" } else { call_id };

            let output = payload.output.as_deref().unwrap_or("");
            if output.is_empty() {
                return;
            }

            events.push(ParsedEvent {
                uuid: format!("{}-result-{}", msg_uuid, uuid_suffix),
                session_id: session_id.to_string(),
                timestamp,
                role: Role::Tool,
                content_text: None,
                tool_name: None,
                tool_input_json: None,
                tool_output_text: Some(output.to_string()),
                tool_call_id: if call_id.is_empty() { None } else { Some(call_id.to_string()) },
                source_offset: line_offset,
                raw_type: "codex_function_call_output".to_string(),
                raw_line: Some(raw_line.to_string()),
            });
        }
        "reasoning" => {
            // Skip reasoning blocks (internal model thinking)
        }
        _ => {
            // Unknown Codex payload type — skip
        }
    }
}

// ---------------------------------------------------------------------------
// Claude extraction
// ---------------------------------------------------------------------------

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
                        tool_call_id: None,
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
                tool_call_id: None,
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
                        tool_call_id: None,
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
                    tool_call_id: if tool_id.is_empty() { None } else { Some(tool_id.to_string()) },
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

        // Use extracted text, or fall back to "[tool error]" for empty-content error results
        // so the result event is still emitted and the call/result pair stays linked.
        let output_text = match result_text {
            Some(ref t) if !t.is_empty() => Some(t.clone()),
            _ if item.is_error == Some(true) => Some("[tool error]".to_string()),
            _ => None,
        };

        if let Some(text) = output_text {
            events.push(ParsedEvent {
                uuid: format!("{}-result-{}", msg_uuid, uuid_suffix),
                session_id: session_id.to_string(),
                timestamp,
                role: Role::Tool,
                content_text: None,
                tool_name: None,
                tool_input_json: None,
                tool_output_text: Some(text),
                tool_call_id: if tool_use_id.is_empty() { None } else { Some(tool_use_id.to_string()) },
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

    // -----------------------------------------------------------------------
    // Codex format tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_codex_user_message() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-ea04-7983-a845-d0b68a77fa62.jsonl",
            &[
                r#"{"type":"session_meta","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"session_meta","id":"019c638d-ea04-7983-a845-d0b68a77fa62","cwd":"/Users/test/project","cli_version":"0.1.2"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:11Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Hello from Codex"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[0].content_text.as_deref(), Some("Hello from Codex"));
        assert_eq!(result.events[0].raw_type, "codex_user");
        // Metadata from session_meta
        assert_eq!(result.metadata.cwd.as_deref(), Some("/Users/test/project"));
        assert_eq!(result.metadata.version.as_deref(), Some("0.1.2"));
        assert_eq!(result.metadata.session_id, "019c638d-ea04-7983-a845-d0b68a77fa62");
    }

    #[test]
    fn test_codex_assistant_message() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000001.jsonl",
            &[r#"{"type":"response_item","timestamp":"2026-02-15T17:06:12Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Here is the answer"}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Assistant);
        assert_eq!(result.events[0].content_text.as_deref(), Some("Here is the answer"));
        assert_eq!(result.events[0].raw_type, "codex_assistant");
    }

    #[test]
    fn test_codex_function_call_and_output() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000002.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:13Z","payload":{"type":"function_call","name":"shell","arguments":"{\"cmd\":\"ls -la\"}","call_id":"call_123"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:14Z","payload":{"type":"function_call_output","call_id":"call_123","output":"file1.txt\nfile2.txt"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);

        // Tool call
        assert_eq!(result.events[0].role, Role::Assistant);
        assert_eq!(result.events[0].tool_name.as_deref(), Some("shell"));
        assert_eq!(result.events[0].raw_type, "codex_function_call");
        assert!(result.events[0].tool_input_json.is_some());

        // Tool output
        assert_eq!(result.events[1].role, Role::Tool);
        assert_eq!(result.events[1].tool_output_text.as_deref(), Some("file1.txt\nfile2.txt"));
        assert_eq!(result.events[1].raw_type, "codex_function_call_output");
    }

    #[test]
    fn test_codex_skip_developer_and_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000003.jsonl",
            &[
                r#"{"type":"session_meta","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"session_meta","id":"019c638d-0000-0000-0000-000000000003"}}"#,
                r#"{"type":"event_msg","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"token_count","count":42}}"#,
                r#"{"type":"turn_context","timestamp":"2026-02-15T17:06:10Z","payload":{}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:11Z","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"system prompt"}]}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:11Z","payload":{"type":"reasoning","content":"thinking..."}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:12Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"real user message"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        // Only the user message should produce an event
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].content_text.as_deref(), Some("real user message"));
    }

    #[test]
    fn test_codex_git_info_from_session_meta() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-01-10T11-00-00-019c638d-ea04-7983-a845-d0b68a77fa62.jsonl",
            &[
                r#"{"timestamp":"2026-01-10T11:00:00.000Z","type":"session_meta","payload":{"id":"019c638d-ea04-7983-a845-d0b68a77fa62","cwd":"/Users/test/zorb","cli_version":"0.105.0","git":{"commit_hash":"abc123","branch":"feature/my-branch","repository_url":"git@github.com:org/zorb.git"}}}"#,
                r#"{"timestamp":"2026-01-10T11:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        // session_id from payload, not filename v5 UUID
        assert_eq!(result.metadata.session_id, "019c638d-ea04-7983-a845-d0b68a77fa62");
        assert_eq!(result.metadata.cwd.as_deref(), Some("/Users/test/zorb"));
        assert_eq!(result.metadata.git_branch.as_deref(), Some("feature/my-branch"));
        // git_repo from session_meta payload, not disk
        assert_eq!(result.metadata.git_repo.as_deref(), Some("git@github.com:org/zorb.git"));
        // project derived from cwd basename
        assert_eq!(result.metadata.project.as_deref(), Some("zorb"));
    }

    // -----------------------------------------------------------------------
    // Subagent UUID generation tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_non_uuid_filename_gets_deterministic_uuid() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "agent-a51c878.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        // Should be a valid UUID (v5 derived from path)
        assert!(Uuid::parse_str(&result.metadata.session_id).is_ok(),
            "Non-UUID filename should get a deterministic UUID, got: {}", result.metadata.session_id);

        // Parse again — should get the same UUID (deterministic)
        let result2 = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.metadata.session_id, result2.metadata.session_id);
    }

    #[test]
    fn test_uuid_filename_preserved() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "3334cc69-974a-46a5-84e3-64459521135c.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.metadata.session_id, "3334cc69-974a-46a5-84e3-64459521135c");
    }

    #[test]
    fn test_codex_session_meta_overrides_filename_uuid() {
        let dir = tempfile::tempdir().unwrap();
        // Filename UUID differs from session_meta id
        let path = make_jsonl_file(
            dir.path(),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl",
            &[
                r#"{"type":"session_meta","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"session_meta","id":"019c638d-ea04-7983-a845-d0b68a77fa62","cwd":"/test"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:11Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        // session_meta id should take precedence
        assert_eq!(result.metadata.session_id, "019c638d-ea04-7983-a845-d0b68a77fa62");
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

    // -----------------------------------------------------------------------
    // Gemini JSON format tests
    // -----------------------------------------------------------------------

    fn make_json_file(dir: &Path, name: &str, content: &str) -> std::path::PathBuf {
        let path = dir.join(name);
        std::fs::write(&path, content).unwrap();
        path
    }

    #[test]
    fn test_gemini_parse_user_and_assistant() {
        let dir = tempfile::tempdir().unwrap();
        let session_json = r#"{
            "sessionId": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "projectHash": "abc123",
            "startTime": "2026-01-10T10:00:00Z",
            "lastUpdated": "2026-01-10T10:01:00Z",
            "messages": [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "timestamp": "2026-01-10T10:00:00Z",
                    "type": "user",
                    "content": "What is 2+2?"
                },
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "timestamp": "2026-01-10T10:00:05Z",
                    "type": "gemini",
                    "content": "2+2 equals 4."
                }
            ]
        }"#;
        let path = make_json_file(
            dir.path(),
            "session-2026-01-10T10-00-00-a1b2c3d4.json",
            session_json,
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[0].content_text.as_deref(), Some("What is 2+2?"));
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(result.events[1].content_text.as_deref(), Some("2+2 equals 4."));
        // Session ID from document takes precedence over stem-derived
        assert_eq!(result.metadata.session_id, "a1b2c3d4-e5f6-7890-abcd-ef1234567890");
        assert!(result.metadata.started_at.is_some());
    }

    #[test]
    fn test_gemini_parse_tool_calls() {
        let dir = tempfile::tempdir().unwrap();
        let session_json = r#"{
            "sessionId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "startTime": "2026-01-10T11:00:00Z",
            "messages": [
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "timestamp": "2026-01-10T11:00:00Z",
                    "type": "user",
                    "content": "Read the README"
                },
                {
                    "id": "44444444-4444-4444-4444-444444444444",
                    "timestamp": "2026-01-10T11:00:05Z",
                    "type": "gemini",
                    "content": "I will read it now.",
                    "toolCalls": [
                        {
                            "id": "tc-001",
                            "name": "read_file",
                            "args": {"file_path": "README.md"}
                        }
                    ]
                }
            ]
        }"#;
        let path = make_json_file(dir.path(), "session-2026-01-10T11-00-00-bbbb.json", session_json);

        let result = parse_session_file(&path, 0).unwrap();
        // user message + assistant text + tool call = 3 events
        assert_eq!(result.events.len(), 3);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(result.events[1].content_text.as_deref(), Some("I will read it now."));
        assert_eq!(result.events[2].role, Role::Assistant);
        assert_eq!(result.events[2].tool_name.as_deref(), Some("read_file"));
        assert!(result.events[2].tool_input_json.is_some());
    }

    #[test]
    fn test_gemini_offset_ignored() {
        // Gemini files always parse from 0 regardless of offset argument.
        let dir = tempfile::tempdir().unwrap();
        let session_json = r#"{
            "sessionId": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "startTime": "2026-01-10T12:00:00Z",
            "messages": [
                {
                    "id": "55555555-5555-5555-5555-555555555555",
                    "timestamp": "2026-01-10T12:00:00Z",
                    "type": "user",
                    "content": "Hello Gemini"
                }
            ]
        }"#;
        let path = make_json_file(dir.path(), "session-cccc.json", session_json);

        let file_size = std::fs::metadata(&path).unwrap().len();
        // Even with offset = file_size, we still get events (offset is ignored for .json)
        let result = parse_session_file(&path, file_size).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].content_text.as_deref(), Some("Hello Gemini"));
    }

    #[test]
    fn test_gemini_invalid_json_returns_empty() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_json_file(dir.path(), "session-bad.json", "not valid json {{{");

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 0);
    }

    // -----------------------------------------------------------------------
    // tool_call_id pairing tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_claude_tool_use_carries_tool_call_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"tool_use","id":"toolu_bdrk_01ABC","name":"Bash","input":{"command":"ls"}}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].tool_name.as_deref(), Some("Bash"));
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("toolu_bdrk_01ABC"));
    }

    #[test]
    fn test_claude_tool_result_carries_tool_call_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_bdrk_01ABC","content":"file contents here"}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("toolu_bdrk_01ABC"));
    }

    #[test]
    fn test_claude_call_and_result_share_same_tool_call_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"tool_use","id":"toolu_01XYZ","name":"Read","input":{"file_path":"/tmp/f"}}]}}"#,
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_01XYZ","content":"file contents"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);

        let call = &result.events[0];
        let res = &result.events[1];

        assert_eq!(call.role, Role::Assistant);
        assert_eq!(call.tool_call_id.as_deref(), Some("toolu_01XYZ"));

        assert_eq!(res.role, Role::Tool);
        assert_eq!(res.tool_call_id.as_deref(), Some("toolu_01XYZ"));

        // Same ID links them
        assert_eq!(call.tool_call_id, res.tool_call_id);
    }

    #[test]
    fn test_codex_function_call_carries_call_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:13Z","payload":{"type":"function_call","name":"shell","arguments":"{\"cmd\":\"ls -la\"}","call_id":"call_abc123"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:14Z","payload":{"type":"function_call_output","call_id":"call_abc123","output":"file1.txt\nfile2.txt"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);

        let call = &result.events[0];
        let res = &result.events[1];

        assert_eq!(call.tool_call_id.as_deref(), Some("call_abc123"));
        assert_eq!(res.tool_call_id.as_deref(), Some("call_abc123"));
        assert_eq!(call.tool_call_id, res.tool_call_id);
    }

    #[test]
    fn test_is_error_empty_content_emits_placeholder() {
        // is_error:true with no content should still emit an event (keeps call paired)
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_01ERR","content":"","is_error":true}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("toolu_01ERR"));
        assert_eq!(result.events[0].tool_output_text.as_deref(), Some("[tool error]"));
    }

    #[test]
    fn test_is_error_with_content_uses_content() {
        // is_error:true WITH content should use the actual content, not placeholder
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_01ERR","content":"The user rejected this action.","is_error":true}]}}"#],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].tool_output_text.as_deref(), Some("The user rejected this action."));
    }

    #[test]
    fn test_non_tool_events_have_no_tool_call_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Hello"}}"#,
                r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"Hi"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);
        assert!(result.events[0].tool_call_id.is_none());
        assert!(result.events[1].tool_call_id.is_none());
    }
}
