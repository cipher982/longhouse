//! Session file parser for Claude Code, Codex, Antigravity, and legacy Gemini sessions.
//!
//! Extracts meaningful events (user messages, assistant text, tool calls,
//! tool results) plus compaction-adjacent metadata boundaries from session
//! files and converts them to a normalized format.
//!
//! Supported formats (dispatched by file extension):
//! - **Claude** (`.jsonl`): `{type: "user"|"assistant", message: {content: ...}}`
//! - **Codex** (`.jsonl`): `{type: "response_item", payload: {type: "message"|"function_call"|..., role: ..., content: [...]}}`
//! - **Antigravity** (`.jsonl`): `{step_index, source, type, created_at, content, tool_calls}`
//! - **Legacy Antigravity JSON** (`.json`): `{sessionId, messages: [{type: "user"|"gemini", content, toolCalls: [...]}]}`
//!
//! Gemini files are full JSON documents rewritten in-place (not JSONL appended),
//! so they are always parsed from offset 0. The backend deduplicates events by hash.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::path::Path;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use memmap2::Mmap;
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use uuid::Uuid;

use crate::codex_source::parse_codex_subagent_source_str;
use crate::console_prompt::strip_console_run_once_prompt;
use crate::media_redaction::{redact_inline_image_data_urls_with_media, InlineImageRedaction};

/// Threshold for switching from buffered read to mmap (1 MB).
const MMAP_THRESHOLD: u64 = 1_048_576;
const EMPTY_TOOL_RESULT_PLACEHOLDER: &str = "[empty tool result]";

// ---------------------------------------------------------------------------
// Data types
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
    Tool,
    System,
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
    /// - Antigravity tool_call:  synthetic step/name id when no provider id is available
    /// None for all non-tool events and where provider doesn't emit an ID.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_call_id: Option<String>,
    pub source_offset: u64,
    pub raw_type: String,
    /// Only the first event per source line carries raw_line (dedup).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_line: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ParsedSourceLine {
    pub source_offset: u64,
    /// Full source line bytes decoded as UTF-8, without trailing newline.
    pub raw_line: String,
}

#[derive(Debug, Clone, Serialize)]
#[allow(dead_code)] // Phase 2 media store/upload consumes this parser side channel.
pub struct ParsedMediaObject {
    pub source_offset: u64,
    pub sha256: String,
    pub mime_type: String,
    pub byte_size: usize,
    pub original_chars: usize,
    pub original_line_sha256: String,
    #[serde(skip_serializing)]
    pub bytes: Vec<u8>,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct SessionMetadata {
    pub session_id: String,
    pub provider_session_id: Option<String>,
    pub forked_from_session_id: Option<String>,
    pub lineage_kind: Option<String>,
    pub subagent_id: Option<String>,
    pub subagent_prompt_id: Option<String>,
    pub subagent_tool_use_id: Option<String>,
    /// Claude dynamic-workflow run id, derived from the
    /// `.../subagents/workflows/<run>/agent-*.jsonl` path segment.
    pub workflow_run_id: Option<String>,
    /// `attributionAgent` from workflow subagent assistant lines (e.g.
    /// "workflow-subagent"); identifies the agent kind within a run.
    pub attribution_agent: Option<String>,
    /// `attributionSkill` from workflow subagent assistant lines (e.g.
    /// "deep-research"); identifies the workflow/skill that spawned the run.
    pub attribution_skill: Option<String>,
    pub cwd: Option<String>,
    pub git_branch: Option<String>,
    pub git_repo: Option<String>,
    pub project: Option<String>,
    pub environment: Option<String>,
    pub origin_kind: Option<String>,
    pub hatch_run_id: Option<String>,
    pub parent_longhouse_session_id: Option<String>,
    pub parent_thread_id: Option<String>,
    pub parent_provider_session_id: Option<String>,
    pub version: Option<String>,
    pub started_at: Option<DateTime<Utc>>,
    pub ended_at: Option<DateTime<Utc>>,
    pub is_sidechain: bool,
}

pub struct ParseResult {
    pub events: Vec<ParsedEvent>,
    pub source_lines: Vec<ParsedSourceLine>,
    #[allow(dead_code)] // Phase 2 media store/upload consumes this parser side channel.
    pub media_objects: Vec<ParsedMediaObject>,
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
    /// Antigravity transcript format.
    step_index: Option<u64>,
    source: Option<String>,
    created_at: Option<String>,
    timestamp: Option<String>,
    uuid: Option<String>,
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
    #[serde(rename = "agentId")]
    agent_id: Option<String>,
    #[serde(rename = "promptId")]
    prompt_id: Option<String>,
    cwd: Option<String>,
    #[serde(rename = "gitBranch")]
    git_branch: Option<String>,
    version: Option<String>,
    #[serde(rename = "isSidechain")]
    is_sidechain: Option<bool>,
    /// Workflow subagent attribution (assistant lines): agent kind + skill.
    #[serde(rename = "attributionAgent")]
    attribution_agent: Option<String>,
    #[serde(rename = "attributionSkill")]
    attribution_skill: Option<String>,
    /// Claude summary title/body line written during/after compaction.
    summary: Option<String>,
    /// Claude system-message subtype (e.g. compact_boundary).
    subtype: Option<String>,
    /// System-message content field.
    content: Option<String>,
    /// File-history snapshot payload.
    snapshot: Option<FileHistorySnapshot>,
    /// Optional compaction metadata payloads on system boundary lines.
    #[serde(rename = "compactMetadata")]
    compact_metadata: Option<Box<RawValue>>,
    #[serde(rename = "microcompactMetadata")]
    microcompact_metadata: Option<Box<RawValue>>,
    /// Claude format: `{message: {content: ...}}`
    message: Option<RawMessage>,
    /// Codex format: `{payload: {type: ..., role: ..., content: [...]}}`
    payload: Option<CodexPayload>,
    /// Antigravity format: model response records can carry proposed tool calls.
    tool_calls: Option<Vec<AntigravityToolCall>>,
}

#[derive(Deserialize)]
struct RawMessage {
    /// Kept as raw JSON — avoids building a full serde_json::Value DOM tree.
    /// Parsed on-demand in extraction functions via ContentItem.
    content: Box<RawValue>,
}

#[derive(Deserialize)]
struct FileHistorySnapshot {
    timestamp: Option<String>,
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
    /// session_meta: parent provider session UUID for forked subagents
    forked_from_id: Option<String>,
    /// session_meta: Codex source object, including current subagent lineage.
    source: Option<Box<RawValue>>,
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
    /// event_msg: provider reason for lifecycle/control artifacts.
    reason: Option<String>,
    /// function_call_output: result. Codex emits a plain string for text tool
    /// results, but an array of content items for image-bearing results (e.g.
    /// `view_image` -> [{type: input_image, image_url: data:...}]). Accept both
    /// so image-only results do not fail the whole line's deserialization.
    output: Option<CodexFunctionOutput>,
}

/// Codex `function_call_output.output`: either opaque result text or an array of
/// content items. Image content (`image_url`) is intentionally ignored here --
/// inline image bytes are captured by source-line media redaction, not by this
/// struct, so we never pull megabyte base64 into the parse DOM.
#[derive(Deserialize)]
#[serde(untagged)]
enum CodexFunctionOutput {
    Text(String),
    Items(Vec<CodexContentItem>),
}

#[derive(Debug, Clone)]
struct ScannedCodexSessionMeta {
    session_id: String,
    forked_from_session_id: Option<String>,
    is_sidechain: bool,
}

#[derive(Debug, Default)]
struct CodexPayloadParentage {
    forked_from_session_id: Option<String>,
    is_sidechain: bool,
}

#[derive(Deserialize)]
struct CodexContentItem {
    r#type: Option<String>,
    text: Option<String>,
}

// ---------------------------------------------------------------------------
// Antigravity-specific types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct AntigravityToolCall {
    name: Option<String>,
    args: Option<Box<RawValue>>,
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
    /// Common observed values: "user", "gemini", "info", "error"
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
            if t.is_empty() {
                None
            } else {
                Some(t)
            }
        }
        serde_json::Value::Array(arr) => {
            // Try to concatenate "text" fields from a parts array
            let text = arr
                .iter()
                .filter_map(|item| item.get("text").and_then(|t| t.as_str()))
                .collect::<Vec<_>>()
                .join("");
            if text.trim().is_empty() {
                None
            } else {
                Some(text.trim().to_string())
            }
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
    status: Option<String>,
    timestamp: Option<String>,
    result: Option<serde_json::Value>,
}

fn extract_gemini_tool_result_text(v: &serde_json::Value) -> Option<String> {
    fn value_to_text(v: &serde_json::Value) -> Option<String> {
        match v {
            serde_json::Value::String(s) => {
                let t = s.trim();
                if t.is_empty() {
                    None
                } else {
                    Some(t.to_string())
                }
            }
            serde_json::Value::Number(_) | serde_json::Value::Bool(_) => Some(v.to_string()),
            serde_json::Value::Array(_) | serde_json::Value::Object(_) => {
                serde_json::to_string(v).ok().and_then(|s| {
                    let t = s.trim();
                    if t.is_empty() {
                        None
                    } else {
                        Some(t.to_string())
                    }
                })
            }
            _ => None,
        }
    }

    fn collect_result_parts(v: &serde_json::Value, parts: &mut Vec<String>) {
        match v {
            serde_json::Value::Array(items) => {
                for item in items {
                    collect_result_parts(item, parts);
                }
            }
            serde_json::Value::Object(obj) => {
                // Gemini CLI result shape:
                // {"functionResponse":{"response":{"output":"..."} | {"error":"..."}}}
                if let Some(fr) = obj.get("functionResponse") {
                    if let Some(resp) = fr.get("response") {
                        if let Some(output) = resp.get("output").and_then(value_to_text) {
                            parts.push(output);
                        }
                        if let Some(error) = resp.get("error").and_then(value_to_text) {
                            parts.push(error);
                        }
                    }
                }

                // Generic fallback for less common shapes.
                if let Some(output) = obj.get("output").and_then(value_to_text) {
                    parts.push(output);
                }
                if let Some(error) = obj.get("error").and_then(value_to_text) {
                    parts.push(error);
                }
            }
            _ => {
                if let Some(text) = value_to_text(v) {
                    parts.push(text);
                }
            }
        }
    }

    let mut parts = Vec::new();
    collect_result_parts(v, &mut parts);

    if parts.is_empty() {
        None
    } else {
        Some(parts.join("\n\n"))
    }
}

fn hex_value(b: u8) -> Option<u16> {
    match b {
        b'0'..=b'9' => Some((b - b'0') as u16),
        b'a'..=b'f' => Some((b - b'a' + 10) as u16),
        b'A'..=b'F' => Some((b - b'A' + 10) as u16),
        _ => None,
    }
}

fn parse_u_escape(bytes: &[u8], i: usize) -> Option<u16> {
    if i + 6 > bytes.len() || bytes[i] != b'\\' || bytes[i + 1] != b'u' {
        return None;
    }
    let mut v = 0u16;
    for j in 0..4 {
        v = (v << 4) | hex_value(bytes[i + 2 + j])?;
    }
    Some(v)
}

fn sanitize_invalid_surrogate_escapes(input: &str) -> Option<String> {
    let bytes = input.as_bytes();
    let mut i = 0usize;
    let mut out: Vec<u8> = Vec::with_capacity(input.len());
    let mut changed = false;

    while i < bytes.len() {
        if let Some(code_unit) = parse_u_escape(bytes, i) {
            let is_high = (0xD800..=0xDBFF).contains(&code_unit);
            let is_low = (0xDC00..=0xDFFF).contains(&code_unit);

            if is_high {
                if let Some(next_code_unit) = parse_u_escape(bytes, i + 6) {
                    if (0xDC00..=0xDFFF).contains(&next_code_unit) {
                        out.extend_from_slice(&bytes[i..i + 12]);
                        i += 12;
                        continue;
                    }
                }
                out.extend_from_slice(br"\uFFFD");
                changed = true;
                i += 6;
                continue;
            }

            if is_low {
                out.extend_from_slice(br"\uFFFD");
                changed = true;
                i += 6;
                continue;
            }

            out.extend_from_slice(&bytes[i..i + 6]);
            i += 6;
            continue;
        }

        out.push(bytes[i]);
        i += 1;
    }

    if changed {
        String::from_utf8(out).ok()
    } else {
        None
    }
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
    let mut session_id = if let Some(antigravity_id) = antigravity_session_id_from_path(path) {
        antigravity_id
    } else if Uuid::parse_str(&raw_stem).is_ok() {
        raw_stem
    } else {
        Uuid::new_v5(&Uuid::NAMESPACE_URL, path.to_string_lossy().as_bytes()).to_string()
    };

    // Incremental parses can start after the initial session_meta line.
    // Recover canonical Codex session ID (and fork lineage) from the header
    // so replays do not fall back to filename-derived UUIDs.
    let scanned_session_meta = if offset > 0 {
        scan_codex_session_meta(path)
    } else {
        None
    };
    if let Some(scanned) = scanned_session_meta.as_ref() {
        session_id = scanned.session_id.clone();
    }

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
            source_lines: Vec::new(),
            media_objects: Vec::new(),
            last_good_offset: offset,
            candidate_records: 0,
            metadata: SessionMetadata {
                session_id,
                forked_from_session_id: scanned_session_meta
                    .as_ref()
                    .and_then(|item| item.forked_from_session_id.clone()),
                is_sidechain: scanned_session_meta
                    .as_ref()
                    .map(|item| item.is_sidechain)
                    .unwrap_or(false),
                ..Default::default()
            },
        });
    }

    // JSONL: choose strategy based on file size
    let mut result = if file_size > MMAP_THRESHOLD {
        parse_mmap(path, offset, &session_id)?
    } else {
        parse_buffered(path, offset, &session_id)?
    };

    if let Some(scanned) = scanned_session_meta.as_ref() {
        result.metadata.session_id = scanned.session_id.clone();
        if result.metadata.forked_from_session_id.is_none() {
            result.metadata.forked_from_session_id = scanned.forked_from_session_id.clone();
        }
        if scanned.is_sidechain {
            result.metadata.is_sidechain = true;
        }
    }

    // Workflow run id comes from the on-disk path, not the line contents.
    if result.metadata.workflow_run_id.is_none() {
        result.metadata.workflow_run_id = workflow_run_id_from_path(path);
    }

    let canonical_session_id = result.metadata.session_id.clone();
    if result
        .events
        .iter()
        .any(|event| event.session_id != canonical_session_id)
    {
        for event in &mut result.events {
            event.session_id = canonical_session_id.clone();
        }
    }

    Ok(result)
}

/// Extract the dynamic-workflow run id from a Claude workflow subagent path:
/// `.../subagents/workflows/<run>/agent-*.jsonl` -> `<run>`.
fn workflow_run_id_from_path(path: &Path) -> Option<String> {
    let components: Vec<&str> = path
        .components()
        .filter_map(|component| component.as_os_str().to_str())
        .collect();
    for window in components.windows(3) {
        if window[0] == "subagents" && window[1] == "workflows" && !window[2].is_empty() {
            return Some(window[2].to_string());
        }
    }
    None
}

/// Seed the antigravity pending-call queue from the record immediately before an
/// incremental resume `offset`.
///
/// Antigravity tool results inherit their call id from the adjacent preceding
/// planner. The shipper resumes at a stored byte offset, which routinely lands
/// between a `PLANNER_RESPONSE` and its result record, so without this seed every
/// flush boundary would mint a fresh live orphan. Bounded backward scan: read the
/// single line ending at `offset`. Returns empty pending if `offset == 0`, the prior
/// record is not an antigravity planner with tool_calls, or anything fails.
fn seed_antigravity_pending(path: &Path, offset: u64) -> AntigravityPending {
    if offset == 0 {
        return AntigravityPending::default();
    }
    // Read a bounded window ending at `offset`, then take the last complete line.
    const SEED_WINDOW_BYTES: u64 = 256 * 1024;
    let window = SEED_WINDOW_BYTES.min(offset);
    let start = offset - window;

    let Ok(mut file) = std::fs::File::open(path) else {
        return AntigravityPending::default();
    };
    use std::io::{Read, Seek};
    if file.seek(std::io::SeekFrom::Start(start)).is_err() {
        return AntigravityPending::default();
    }
    let mut buf = vec![0u8; window as usize];
    if file.read_exact(&mut buf).is_err() {
        return AntigravityPending::default();
    }
    // Drop a possibly-partial leading line when we started mid-file.
    let search = if start > 0 {
        match buf.iter().position(|&b| b == b'\n') {
            Some(nl) => &buf[nl + 1..],
            None => return AntigravityPending::default(),
        }
    } else {
        &buf[..]
    };
    // The record ending at `offset` is the last newline-terminated line in the window.
    let trimmed = trim_bytes(search);
    let last_line = match trimmed.iter().rposition(|&b| b == b'\n') {
        Some(nl) => &trimmed[nl + 1..],
        None => trimmed,
    };
    if last_line.is_empty() {
        return AntigravityPending::default();
    }
    let Ok(obj) = serde_json::from_slice::<RawLine>(last_line) else {
        return AntigravityPending::default();
    };
    if !is_antigravity_line(&obj) {
        return AntigravityPending::default();
    }
    let Some(tool_calls) = obj.tool_calls.as_ref() else {
        return AntigravityPending::default();
    };
    let mut call_ids: VecDeque<String> = VecDeque::new();
    for (idx, call) in tool_calls.iter().enumerate() {
        let has_name = call
            .name
            .as_ref()
            .map(|name| !name.trim().is_empty())
            .unwrap_or(false);
        if !has_name {
            continue;
        }
        if let Some(step) = obj.step_index {
            call_ids.push_back(format!("antigravity-{step}-{idx}"));
        }
    }
    if call_ids.is_empty() {
        return AntigravityPending::default();
    }
    AntigravityPending {
        call_ids,
        next_result_step: obj.step_index.map(|step| step + 1),
    }
}

fn antigravity_session_id_from_path(path: &Path) -> Option<String> {
    let components: Vec<&str> = path
        .components()
        .filter_map(|component| component.as_os_str().to_str())
        .collect();
    for window in components.windows(2) {
        if window[0] == "brain" && Uuid::parse_str(window[1]).is_ok() {
            return Some(window[1].to_string());
        }
    }
    None
}

/// Scan the start of a JSONL file for Codex `session_meta` identity fields.
///
/// This is intentionally bounded to avoid large-file overhead on every parse.
fn scan_codex_session_meta(path: &Path) -> Option<ScannedCodexSessionMeta> {
    const SESSION_META_SCAN_LIMIT_BYTES: usize = 256 * 1024;

    let file = std::fs::File::open(path).ok()?;
    let mut reader = BufReader::with_capacity(16 * 1024, file);
    let mut line = String::new();
    let mut bytes_scanned = 0usize;

    while bytes_scanned < SESSION_META_SCAN_LIMIT_BYTES {
        line.clear();
        let n = reader.read_line(&mut line).ok()?;
        if n == 0 {
            break;
        }
        bytes_scanned += n;

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let obj: RawLine = match serde_json::from_str(trimmed) {
            Ok(v) => v,
            Err(_) => continue,
        };

        if obj.r#type.as_deref() != Some("session_meta") {
            continue;
        }

        let payload = obj.payload.as_ref()?;
        let id = payload.id.as_ref()?;
        if Uuid::parse_str(id).is_ok() {
            let parentage = codex_payload_parentage(payload);
            return Some(ScannedCodexSessionMeta {
                session_id: id.clone(),
                forked_from_session_id: parentage.forked_from_session_id,
                is_sidechain: parentage.is_sidechain,
            });
        }
    }

    None
}

fn codex_payload_parentage(payload: &CodexPayload) -> CodexPayloadParentage {
    let forked_from_session_id = payload
        .forked_from_id
        .as_ref()
        .filter(|candidate| Uuid::parse_str(candidate).is_ok())
        .cloned();
    if forked_from_session_id.is_some() {
        return CodexPayloadParentage {
            forked_from_session_id,
            is_sidechain: true,
        };
    }

    let Some(source) = payload
        .source
        .as_ref()
        .and_then(|source| parse_codex_subagent_source_str(source.get()))
    else {
        return CodexPayloadParentage::default();
    };

    CodexPayloadParentage {
        forked_from_session_id: source
            .parent_thread_id
            .filter(|candidate| Uuid::parse_str(candidate).is_ok()),
        is_sidechain: true,
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
fn parsed_media_objects(
    source_offset: u64,
    original_line_sha256: &str,
    media: Vec<InlineImageRedaction>,
) -> Vec<ParsedMediaObject> {
    media
        .into_iter()
        .map(|item| ParsedMediaObject {
            source_offset,
            sha256: item.sha256,
            mime_type: item.mime_type,
            byte_size: item.byte_size,
            original_chars: item.original_chars,
            original_line_sha256: original_line_sha256.to_string(),
            bytes: item.bytes,
        })
        .collect()
}

fn capture_text_source_lines(content: &str) -> (Vec<ParsedSourceLine>, Vec<ParsedMediaObject>) {
    if content.is_empty() {
        return (Vec::new(), Vec::new());
    }

    let mut source_lines = Vec::new();
    let mut media_objects = Vec::new();
    let mut offset = 0u64;
    for chunk in content.split_inclusive('\n') {
        let trimmed = chunk.strip_suffix('\n').unwrap_or(chunk);
        let raw_line = trimmed.strip_suffix('\r').unwrap_or(trimmed);
        let redacted = redact_inline_image_data_urls_with_media(raw_line);
        media_objects.extend(parsed_media_objects(
            offset,
            &redacted.original_line_sha256,
            redacted.media,
        ));
        source_lines.push(ParsedSourceLine {
            source_offset: offset,
            raw_line: redacted.raw_line,
        });
        offset += chunk.as_bytes().len() as u64;
    }
    (source_lines, media_objects)
}

fn parse_gemini_json(path: &Path, session_id: &str) -> Result<ParseResult> {
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("Failed to read {}", path.display()))?;
    let file_size = content.len() as u64;
    let (source_lines, media_objects) = capture_text_source_lines(&content);

    let session: GeminiSession = match serde_json::from_str(&content) {
        Ok(s) => s,
        Err(primary_error) => {
            if let Some(sanitized) = sanitize_invalid_surrogate_escapes(&content) {
                match serde_json::from_str(&sanitized) {
                    Ok(s) => {
                        tracing::debug!(
                            path = %path.display(),
                            error = %primary_error,
                            "Recovered Gemini JSON after surrogate-escape repair"
                        );
                        s
                    }
                    Err(repaired_error) => {
                        tracing::debug!(
                            path = %path.display(),
                            error = %primary_error,
                            repaired_error = %repaired_error,
                            "Failed to parse Gemini JSON (including repaired payload)"
                        );
                        return Ok(ParseResult {
                            events: Vec::new(),
                            source_lines: Vec::new(),
                            media_objects: Vec::new(),
                            last_good_offset: file_size,
                            candidate_records: 0,
                            metadata: SessionMetadata {
                                session_id: session_id.to_string(),
                                ..Default::default()
                            },
                        });
                    }
                }
            } else {
                tracing::debug!(path = %path.display(), error = %primary_error, "Failed to parse Gemini JSON");
                return Ok(ParseResult {
                    events: Vec::new(),
                    source_lines: Vec::new(),
                    media_objects: Vec::new(),
                    last_good_offset: file_size,
                    candidate_records: 0,
                    metadata: SessionMetadata {
                        session_id: session_id.to_string(),
                        ..Default::default()
                    },
                });
            }
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
    let mut candidate_records = 0;

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
                candidate_records += 1;
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
                candidate_records += 1;
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

                    let tool_input = tc
                        .args
                        .map(|v| RawValue::from_string(v.to_string()).ok())
                        .flatten();

                    let tc_timestamp = tc
                        .timestamp
                        .as_deref()
                        .and_then(parse_timestamp)
                        .unwrap_or(timestamp);

                    let tool_output_text = tc
                        .result
                        .as_ref()
                        .and_then(extract_gemini_tool_result_text)
                        .or_else(|| match tc.status.as_deref() {
                            Some("error") => Some("[tool error]".to_string()),
                            Some("cancelled") => Some("[tool cancelled]".to_string()),
                            _ => None,
                        });

                    let gemini_tc_id = tc
                        .id
                        .as_deref()
                        .filter(|s| !s.is_empty())
                        .map(|s| s.to_string());
                    events.push(ParsedEvent {
                        uuid: format!("{}-tool-{}", msg_id, tc_id),
                        session_id: canonical_session_id.clone(),
                        timestamp: tc_timestamp,
                        role: Role::Assistant,
                        content_text: None,
                        tool_name: Some(tc_name),
                        tool_input_json: tool_input,
                        tool_output_text: None,
                        tool_call_id: gemini_tc_id.clone(),
                        source_offset: 0,
                        raw_type: "gemini_tool_call".to_string(),
                        raw_line: None,
                    });

                    if let Some(output_text) = tool_output_text {
                        events.push(ParsedEvent {
                            uuid: format!("{}-result-{}", msg_id, tc_id),
                            session_id: canonical_session_id.clone(),
                            timestamp: tc_timestamp,
                            role: Role::Tool,
                            content_text: None,
                            tool_name: None,
                            tool_input_json: None,
                            tool_output_text: Some(output_text),
                            tool_call_id: gemini_tc_id,
                            source_offset: 0,
                            raw_type: "gemini_tool_result".to_string(),
                            raw_line: None,
                        });
                    }
                }
            }
            _ => {
                // Unknown message type — skip
            }
        }
    }

    Ok(ParseResult {
        events,
        source_lines,
        media_objects,
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
            // No git repo — fall back to cwd basename, but do not promote
            // generic temp workspace directories into report-level projects.
            let project = project_from_cwd_basename(cwd);
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

fn project_from_cwd_basename(cwd: &Path) -> Option<String> {
    let label = cwd.file_name().and_then(|s| s.to_str())?.trim();
    if label.is_empty() || label == "workspace" {
        return None;
    }
    Some(label.to_string())
}

// ---------------------------------------------------------------------------
// mmap-based parser (large files)
// ---------------------------------------------------------------------------

fn parse_mmap(path: &Path, offset: u64, session_id: &str) -> Result<ParseResult> {
    let file =
        std::fs::File::open(path).with_context(|| format!("Failed to open {}", path.display()))?;

    let mmap = unsafe { Mmap::map(&file) }
        .with_context(|| format!("Failed to mmap {}", path.display()))?;

    let data = if (offset as usize) < mmap.len() {
        &mmap[offset as usize..]
    } else {
        return Ok(ParseResult {
            events: Vec::new(),
            source_lines: Vec::new(),
            media_objects: Vec::new(),
            last_good_offset: offset,
            candidate_records: 0,
            metadata: SessionMetadata {
                session_id: session_id.to_string(),
                ..Default::default()
            },
        });
    };

    let mut events = Vec::new();
    let mut source_lines = Vec::new();
    let mut media_objects = Vec::new();
    let mut metadata = SessionMetadata::default();
    let mut min_ts: Option<DateTime<Utc>> = None;
    let mut max_ts: Option<DateTime<Utc>> = None;
    let mut last_good_offset = offset;
    let mut candidate_lines: usize = 0;
    // Antigravity tool result records inherit their call id from the adjacent
    // preceding planner. On incremental resume (offset > 0) the planner may live in
    // the prior batch, so seed from the record before `offset`.
    let mut antigravity_pending = seed_antigravity_pending(path, offset);
    let mut codex_pending = CodexPending::default();

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

        let redacted_line = if let Ok(line_str) = std::str::from_utf8(line_bytes) {
            let redacted = redact_inline_image_data_urls_with_media(line_str);
            media_objects.extend(parsed_media_objects(
                line_offset,
                &redacted.original_line_sha256,
                redacted.media,
            ));
            source_lines.push(ParsedSourceLine {
                source_offset: line_offset,
                raw_line: redacted.raw_line.clone(),
            });
            redacted.raw_line
        } else {
            String::new()
        };

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

        extract_events(
            &obj,
            session_id,
            line_offset,
            &redacted_line,
            &mut events,
            &mut antigravity_pending,
            &mut codex_pending,
        );
    }

    // Finalize metadata
    metadata.started_at = min_ts;
    metadata.ended_at = max_ts;
    if metadata.session_id.is_empty() {
        metadata.session_id = session_id.to_string();
    }
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
        source_lines,
        media_objects,
        last_good_offset,
        candidate_records: candidate_lines,
        metadata,
    })
}

// ---------------------------------------------------------------------------
// Buffered reader parser (small files)
// ---------------------------------------------------------------------------

fn parse_buffered(path: &Path, offset: u64, session_id: &str) -> Result<ParseResult> {
    let mut file =
        std::fs::File::open(path).with_context(|| format!("Failed to open {}", path.display()))?;

    if offset > 0 {
        use std::io::Seek;
        file.seek(std::io::SeekFrom::Start(offset))?;
    }

    let mut reader = BufReader::with_capacity(64 * 1024, file);

    let mut events = Vec::new();
    let mut source_lines = Vec::new();
    let mut media_objects = Vec::new();
    let mut metadata = SessionMetadata::default();
    let mut min_ts: Option<DateTime<Utc>> = None;
    let mut max_ts: Option<DateTime<Utc>> = None;
    let mut current_offset = offset;
    let mut candidate_lines: usize = 0;
    // See parse_mmap: seed antigravity call/result pairing across the resume boundary.
    let mut antigravity_pending = seed_antigravity_pending(path, offset);
    let mut codex_pending = CodexPending::default();
    let mut line = String::new();

    loop {
        line.clear();
        let bytes_read = match reader.read_line(&mut line) {
            Ok(n) => n,
            Err(e) => {
                tracing::warn!(offset = current_offset, error = %e, "Failed to read line");
                break; // IO error — stop processing
            }
        };
        if bytes_read == 0 {
            break;
        }

        if !line.ends_with('\n') {
            // Partial line at EOF — do not advance offset or process it yet.
            break;
        }

        if line.ends_with('\n') {
            line.pop();
            if line.ends_with('\r') {
                line.pop();
            }
        }

        let line_offset = current_offset;
        current_offset += bytes_read as u64;

        let redacted = redact_inline_image_data_urls_with_media(&line);
        media_objects.extend(parsed_media_objects(
            line_offset,
            &redacted.original_line_sha256,
            redacted.media,
        ));
        let redacted_line = redacted.raw_line;
        source_lines.push(ParsedSourceLine {
            source_offset: line_offset,
            raw_line: redacted_line.clone(),
        });

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
            &redacted_line,
            &mut events,
            &mut antigravity_pending,
            &mut codex_pending,
        );
    }

    metadata.started_at = min_ts;
    metadata.ended_at = max_ts;
    if metadata.session_id.is_empty() {
        metadata.session_id = session_id.to_string();
    }
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
        source_lines,
        media_objects,
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
            meta.git_branch = normalize_git_branch(branch);
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
            if meta.session_id.is_empty() {
                if let Some(ref id) = payload.id {
                    if Uuid::parse_str(id).is_ok() {
                        meta.session_id = id.clone();
                    }
                }
            }
            if meta.forked_from_session_id.is_none() {
                let parentage = codex_payload_parentage(payload);
                if let Some(parent_thread_id) = parentage.forked_from_session_id {
                    meta.forked_from_session_id = Some(parent_thread_id);
                }
                if parentage.is_sidechain {
                    meta.is_sidechain = true;
                }
            }
            // Extract git branch and remote URL directly from session_meta.
            // These are authoritative — no need to read .git/config from disk.
            if let Some(ref git) = payload.git {
                if meta.git_branch.is_none() {
                    if let Some(ref branch) = git.branch {
                        meta.git_branch = normalize_git_branch(branch);
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
        if meta.forked_from_session_id.is_none() {
            if let Some(parent_session_id) = obj
                .session_id
                .as_deref()
                .map(str::trim)
                .filter(|candidate| Uuid::parse_str(candidate).is_ok())
            {
                meta.forked_from_session_id = Some(parent_session_id.to_string());
            }
        }
        if meta.subagent_id.is_none() {
            if let Some(agent_id) = obj
                .agent_id
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                meta.subagent_id = Some(agent_id.to_string());
            }
        }
        if meta.subagent_prompt_id.is_none() {
            if let Some(prompt_id) = obj
                .prompt_id
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
            {
                meta.subagent_prompt_id = Some(prompt_id.to_string());
            }
        }
    }

    // Workflow attribution lives on assistant lines, independent of the
    // isSidechain gate above. First non-empty value wins for the session.
    if meta.attribution_agent.is_none() {
        if let Some(agent) = obj
            .attribution_agent
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            meta.attribution_agent = Some(agent.to_string());
        }
    }
    if meta.attribution_skill.is_none() {
        if let Some(skill) = obj
            .attribution_skill
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            meta.attribution_skill = Some(skill.to_string());
        }
    }

    if let Some(ts) = obj
        .timestamp
        .as_deref()
        .or(obj.created_at.as_deref())
        .and_then(parse_timestamp)
    {
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

fn normalize_git_branch(branch: &str) -> Option<String> {
    let trimmed = branch.trim();
    if trimmed.is_empty() || trimmed.eq_ignore_ascii_case("HEAD") {
        return None;
    }
    Some(trimmed.to_string())
}

fn extract_events(
    obj: &RawLine,
    session_id: &str,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
    antigravity_pending: &mut AntigravityPending,
    codex_pending: &mut CodexPending,
) {
    let event_type = obj.r#type.as_deref().unwrap_or("");

    if is_antigravity_line(obj) {
        extract_antigravity_events(
            obj,
            session_id,
            line_offset,
            raw_line,
            events,
            antigravity_pending,
        );
        return;
    }

    // Keep compaction-adjacent records as first-class system events.
    if let Some(meta_event) =
        extract_compaction_metadata_event(obj, session_id, line_offset, raw_line)
    {
        events.push(meta_event);
        return;
    }

    // Skip non-compaction metadata-only types (Claude + Codex). Codex
    // `event_msg.turn_aborted` is the one allowlisted lifecycle/control record
    // we preserve as an action source.
    match event_type {
        "progress" | "session_meta" | "turn_context" => return,
        _ => {}
    }

    let timestamp = obj
        .timestamp
        .as_deref()
        .and_then(parse_timestamp)
        .unwrap_or_else(Utc::now);

    let msg_uuid = obj.uuid.as_deref().unwrap_or("").to_string();
    let msg_uuid = if msg_uuid.is_empty() {
        Uuid::new_v4().to_string()
    } else {
        msg_uuid
    };

    // Codex lifecycle/control event: {type: "event_msg", payload: {...}}
    if event_type == "event_msg" {
        if let Some(ref payload) = obj.payload {
            extract_codex_event_msg(
                payload,
                session_id,
                &msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
                codex_pending,
            );
        }
        return;
    }

    // Codex format: {type: "response_item", payload: {...}}
    if event_type == "response_item" {
        if let Some(ref payload) = obj.payload {
            extract_codex_events(
                payload,
                session_id,
                &msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
                codex_pending,
            );
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

fn is_antigravity_line(obj: &RawLine) -> bool {
    obj.step_index.is_some() && obj.source.is_some() && obj.created_at.is_some()
}

fn antigravity_timestamp(obj: &RawLine) -> DateTime<Utc> {
    obj.created_at
        .as_deref()
        .or(obj.timestamp.as_deref())
        .and_then(parse_timestamp)
        .unwrap_or_else(Utc::now)
}

fn antigravity_uuid(obj: &RawLine, line_offset: u64, suffix: &str) -> String {
    match obj.step_index {
        Some(step) => format!("antigravity-step-{step}-{suffix}"),
        None => format!("antigravity-offset-{line_offset}-{suffix}"),
    }
}

fn antigravity_user_text(content: &str) -> String {
    let start_tag = "<USER_REQUEST>";
    let end_tag = "</USER_REQUEST>";
    if let Some(start) = content.find(start_tag) {
        let after_start = start + start_tag.len();
        if let Some(end) = content[after_start..].find(end_tag) {
            let text = content[after_start..after_start + end].trim();
            if !text.is_empty() {
                return text.to_string();
            }
        }
    }
    content.trim().to_string()
}

fn antigravity_tool_name_from_type(event_type: &str) -> String {
    event_type.trim().to_ascii_lowercase().replace('_', "-")
}

/// Pending antigravity tool-call ids awaiting their result records.
///
/// Antigravity splits a tool call and its result across adjacent records: a
/// `PLANNER_RESPONSE` carries `tool_calls: [{name, args}]` at step N, and the
/// immediately-following `MODEL` non-`_RESPONSE` record at step N+1 is that call's
/// result. The result records carry no correlation id of their own, so we thread the
/// call ids forward and let each result inherit one. Pairing is by adjacency (the
/// alias `list_dir` -> `LIST_DIRECTORY` makes tool-name matching unreliable), so any
/// interleaving record clears the queue to stay fail-closed.
#[derive(Default)]
struct AntigravityPending {
    /// Call ids emitted by the most recent planner, in order, not yet consumed.
    call_ids: VecDeque<String>,
    /// The step_index the next result is expected at. A planner at step N is
    /// followed by its result(s) at N+1, N+2, ... (one per call, in order). Advances
    /// on each consumed result so a multi-call planner pairs consecutive results.
    next_result_step: Option<u64>,
}

fn extract_antigravity_events(
    obj: &RawLine,
    session_id: &str,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
    pending: &mut AntigravityPending,
) {
    let event_type = obj.r#type.as_deref().unwrap_or("");
    let source = obj.source.as_deref().unwrap_or("");
    let timestamp = antigravity_timestamp(obj);
    let mut emitted_raw_line = false;

    if event_type == "USER_INPUT" {
        // Interleaving non-result record: a pending call had no adjacent result.
        *pending = AntigravityPending::default();
        if let Some(content) = obj.content.as_deref() {
            let text = antigravity_user_text(content);
            if !text.is_empty() {
                events.push(ParsedEvent {
                    uuid: antigravity_uuid(obj, line_offset, "user"),
                    session_id: session_id.to_string(),
                    timestamp,
                    role: Role::User,
                    content_text: Some(text),
                    tool_name: None,
                    tool_input_json: None,
                    tool_output_text: None,
                    tool_call_id: None,
                    source_offset: line_offset,
                    raw_type: "antigravity_user".to_string(),
                    raw_line: Some(raw_line.to_string()),
                });
            }
        }
        return;
    }

    let mut emitted_calls_this_record = false;
    if let Some(tool_calls) = obj.tool_calls.as_ref() {
        // A new planner supersedes any prior unconsumed call.
        let mut fresh: VecDeque<String> = VecDeque::new();
        for (idx, call) in tool_calls.iter().enumerate() {
            let Some(tool_name) = call.name.as_ref().filter(|name| !name.trim().is_empty()) else {
                continue;
            };
            let call_id = obj
                .step_index
                .map(|step| format!("antigravity-{step}-{idx}"));
            if let Some(ref id) = call_id {
                fresh.push_back(id.clone());
            }
            events.push(ParsedEvent {
                uuid: antigravity_uuid(obj, line_offset, &format!("tool-{idx}")),
                session_id: session_id.to_string(),
                timestamp,
                role: Role::Assistant,
                content_text: None,
                tool_name: Some(tool_name.clone()),
                tool_input_json: call.args.clone(),
                tool_output_text: None,
                tool_call_id: call_id,
                source_offset: line_offset,
                raw_type: "antigravity_tool_call".to_string(),
                raw_line: if emitted_raw_line {
                    None
                } else {
                    emitted_raw_line = true;
                    Some(raw_line.to_string())
                },
            });
        }
        emitted_calls_this_record = true;
        *pending = AntigravityPending {
            call_ids: fresh,
            next_result_step: obj.step_index.map(|step| step + 1),
        };
    }

    // Update call/result pairing state for THIS record, independent of whether it
    // emits a content event, so content-less and empty-content records still clear a
    // stale pending call. A tool RESULT is strictly a MODEL-source record whose type
    // does not end in `_RESPONSE` (the planner is the `_RESPONSE` record carrying the
    // calls). Anything else interleaving a pending call fails closed.
    let is_tool_result = source == "MODEL" && !event_type.ends_with("_RESPONSE");
    let result_tool_call_id: Option<String> = if emitted_calls_this_record {
        // Queue was just populated by this planner record — keep it; emit no id here.
        None
    } else if is_tool_result {
        let adjacent = match (pending.next_result_step, obj.step_index) {
            (Some(expected), Some(result_step)) => result_step == expected,
            // step_index is guaranteed present for antigravity records; any absence
            // is treated as a mismatch and fails closed.
            _ => false,
        };
        if adjacent {
            let id = pending.call_ids.pop_front();
            if id.is_some() {
                // Next call in this planner pairs to the following result step.
                pending.next_result_step = obj.step_index.map(|step| step + 1);
            }
            id
        } else {
            // Result at an unexpected step — interleaving/mismatch; fail closed.
            *pending = AntigravityPending::default();
            None
        }
    } else {
        // SYSTEM, assistant `_RESPONSE` content, or any other antigravity record
        // interleaves a pending call; fail closed.
        *pending = AntigravityPending::default();
        None
    };

    if let Some(content) = obj.content.as_deref() {
        let text = content.trim();
        if text.is_empty() {
            return;
        }
        let is_assistant = source == "MODEL" && event_type.ends_with("_RESPONSE");
        let role = if is_assistant {
            Role::Assistant
        } else if source == "SYSTEM" {
            Role::System
        } else {
            Role::Tool
        };
        let is_tool_role = matches!(role, Role::Tool);
        let tool_call_id = result_tool_call_id;

        let (content_text, tool_name, tool_output_text, raw_type) = match &role {
            Role::Tool => (
                None,
                Some(antigravity_tool_name_from_type(event_type)),
                Some(text.to_string()),
                "antigravity_tool_result",
            ),
            Role::System => (Some(text.to_string()), None, None, "antigravity_system"),
            _ => (Some(text.to_string()), None, None, "antigravity_assistant"),
        };
        events.push(ParsedEvent {
            uuid: antigravity_uuid(
                obj,
                line_offset,
                if is_tool_role {
                    "tool-result"
                } else {
                    "content"
                },
            ),
            session_id: session_id.to_string(),
            timestamp,
            role,
            content_text,
            tool_name,
            tool_input_json: None,
            tool_output_text,
            tool_call_id,
            source_offset: line_offset,
            raw_type: raw_type.to_string(),
            raw_line: if emitted_raw_line {
                None
            } else {
                Some(raw_line.to_string())
            },
        });
    }
}

fn extract_compaction_metadata_event(
    obj: &RawLine,
    session_id: &str,
    line_offset: u64,
    raw_line: &str,
) -> Option<ParsedEvent> {
    let event_type = obj.r#type.as_deref().unwrap_or("");

    match event_type {
        "summary" => {
            let mut content = obj
                .summary
                .clone()
                .or_else(|| obj.content.clone())
                .unwrap_or_else(|| "Conversation compacted".to_string());
            if content.trim().is_empty() {
                content = "Conversation compacted".to_string();
            }
            Some(ParsedEvent {
                uuid: obj
                    .uuid
                    .clone()
                    .unwrap_or_else(|| format!("meta-summary-{}", line_offset)),
                session_id: session_id.to_string(),
                timestamp: metadata_timestamp(obj),
                role: Role::System,
                content_text: Some(content),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset: line_offset,
                raw_type: "summary".to_string(),
                raw_line: Some(raw_line.to_string()),
            })
        }
        "file-history-snapshot" => {
            let mut content = "File history snapshot".to_string();
            if let Some(ts) = obj.snapshot.as_ref().and_then(|s| s.timestamp.as_ref()) {
                if !ts.trim().is_empty() {
                    content = format!("File history snapshot ({})", ts);
                }
            }
            Some(ParsedEvent {
                uuid: obj
                    .uuid
                    .clone()
                    .unwrap_or_else(|| format!("meta-file-history-snapshot-{}", line_offset)),
                session_id: session_id.to_string(),
                timestamp: metadata_timestamp(obj),
                role: Role::System,
                content_text: Some(content),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset: line_offset,
                raw_type: "file-history-snapshot".to_string(),
                raw_line: Some(raw_line.to_string()),
            })
        }
        "system" => {
            let subtype = obj.subtype.as_deref().unwrap_or("");
            if subtype != "compact_boundary" && subtype != "microcompact_boundary" {
                return None;
            }

            let mut content = obj.content.clone().unwrap_or_else(|| {
                if subtype == "microcompact_boundary" {
                    "Context microcompacted".to_string()
                } else {
                    "Conversation compacted".to_string()
                }
            });

            if let Some(hint) = compact_metadata_hint(if subtype == "microcompact_boundary" {
                obj.microcompact_metadata.as_deref()
            } else {
                obj.compact_metadata.as_deref()
            }) {
                content = format!("{} [{}]", content, hint);
            }

            Some(ParsedEvent {
                uuid: obj
                    .uuid
                    .clone()
                    .unwrap_or_else(|| format!("meta-{}-{}", subtype, line_offset)),
                session_id: session_id.to_string(),
                timestamp: metadata_timestamp(obj),
                role: Role::System,
                content_text: Some(content),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset: line_offset,
                raw_type: subtype.to_string(),
                raw_line: Some(raw_line.to_string()),
            })
        }
        _ => None,
    }
}

fn metadata_timestamp(obj: &RawLine) -> DateTime<Utc> {
    obj.timestamp
        .as_deref()
        .and_then(parse_timestamp)
        .or_else(|| {
            obj.snapshot
                .as_ref()
                .and_then(|s| s.timestamp.as_deref())
                .and_then(parse_timestamp)
        })
        .unwrap_or_else(Utc::now)
}

fn compact_metadata_hint(raw: Option<&RawValue>) -> Option<String> {
    let raw = raw?;
    let value: serde_json::Value = serde_json::from_str(raw.get()).ok()?;
    let mut parts: Vec<String> = Vec::new();

    if let Some(trigger) = value.get("trigger").and_then(|v| v.as_str()) {
        if !trigger.trim().is_empty() {
            parts.push(format!("trigger={}", trigger));
        }
    }

    if let Some(pre_tokens) = value.get("preTokens").and_then(|v| v.as_i64()) {
        parts.push(format!("pre_tokens={}", pre_tokens));
    }

    if parts.is_empty() {
        None
    } else {
        Some(parts.join(" "))
    }
}

// ---------------------------------------------------------------------------
// Codex extraction
// ---------------------------------------------------------------------------

const CODEX_TURN_ABORTED_PREFIX: &str = "<turn_aborted>";
const CODEX_TURN_INTERRUPTED_TEXT: &str = "User interrupted the turn";
const CODEX_TURN_INTERRUPTED_RAW_TYPE: &str = "codex_turn_interrupted";
const CODEX_TURN_INTERRUPTED_MARKER_RAW_TYPE: &str = "codex_turn_interrupted_marker";

#[derive(Debug, Default)]
struct CodexPending {
    suppress_next_turn_aborted_marker: bool,
}

fn extract_codex_event_msg(
    payload: &CodexPayload,
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
    pending: &mut CodexPending,
) {
    let payload_type = payload.r#type.as_deref().unwrap_or("");
    if payload_type != "turn_aborted" {
        return;
    }

    let reason = payload.reason.as_deref().unwrap_or("");
    if reason != "interrupted" {
        return;
    }

    events.push(ParsedEvent {
        uuid: format!("{}-action-turn-interrupted", msg_uuid),
        session_id: session_id.to_string(),
        timestamp,
        role: Role::System,
        content_text: Some(CODEX_TURN_INTERRUPTED_TEXT.to_string()),
        tool_name: None,
        tool_input_json: None,
        tool_output_text: None,
        tool_call_id: None,
        source_offset: line_offset,
        raw_type: CODEX_TURN_INTERRUPTED_RAW_TYPE.to_string(),
        raw_line: Some(raw_line.to_string()),
    });
    pending.suppress_next_turn_aborted_marker = true;
}

fn codex_text_is_turn_aborted_marker(text: &str) -> bool {
    text.trim_start().starts_with(CODEX_TURN_ABORTED_PREFIX)
}

fn extract_codex_events(
    payload: &CodexPayload,
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
    pending: &mut CodexPending,
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
            if role != Role::User {
                pending.suppress_next_turn_aborted_marker = false;
            }

            let content_items: &[CodexContentItem] = payload
                .content
                .as_ref()
                .map(|v| v.as_slice())
                .unwrap_or(&[]);

            // Filter Codex context-injection user messages.
            // Codex prepends AGENTS.md, environment context, and permission instructions
            // as role=user (not role=developer), so we detect them by content prefix.
            if role == Role::User {
                let injected_prefixes = [
                    "# AGENTS.md instructions",
                    "<environment_context>",
                    "<permissions instructions>",
                    "<collaboration_mode>",
                ];
                let first_text = content_items
                    .iter()
                    .find_map(|item| {
                        if item.r#type.as_deref() == Some("input_text") {
                            item.text.as_deref()
                        } else {
                            None
                        }
                    })
                    .unwrap_or("");
                if codex_text_is_turn_aborted_marker(first_text) {
                    if pending.suppress_next_turn_aborted_marker {
                        pending.suppress_next_turn_aborted_marker = false;
                    } else {
                        events.push(ParsedEvent {
                            uuid: format!("{}-action-turn-interrupted-marker", msg_uuid),
                            session_id: session_id.to_string(),
                            timestamp,
                            role: Role::System,
                            content_text: Some(CODEX_TURN_INTERRUPTED_TEXT.to_string()),
                            tool_name: None,
                            tool_input_json: None,
                            tool_output_text: None,
                            tool_call_id: None,
                            source_offset: line_offset,
                            raw_type: CODEX_TURN_INTERRUPTED_MARKER_RAW_TYPE.to_string(),
                            raw_line: Some(raw_line.to_string()),
                        });
                    }
                    return;
                }
                pending.suppress_next_turn_aborted_marker = false;
                if injected_prefixes.iter().any(|p| first_text.starts_with(p)) {
                    return;
                }
            }

            // Count image attachments for placeholder text
            let image_count = content_items
                .iter()
                .filter(|item| item.r#type.as_deref() == Some("input_image"))
                .count();

            // Extract real text: join input_text/output_text, strip XML image wrapper tags
            // that Codex injects as <image name=...> / </image> around image blocks.
            let real_text: String = content_items
                .iter()
                .filter_map(|item| {
                    let t = item.r#type.as_deref().unwrap_or("");
                    if t == "input_text" || t == "output_text" {
                        item.text.as_deref()
                    } else {
                        None
                    }
                })
                .filter(|t| {
                    let trimmed = t.trim();
                    !(trimmed.starts_with("<image ") || trimmed == "</image>")
                })
                .collect::<Vec<_>>()
                .join("\n");
            let real_text = strip_console_run_once_prompt(&real_text)
                .map(str::to_string)
                .unwrap_or(real_text);

            // If there are images but no real text, emit a placeholder so the user
            // event is always stored (prevents assistant appearing as first event).
            let text = if real_text.trim().is_empty() && image_count > 0 {
                if image_count == 1 {
                    "[image attached]".to_string()
                } else {
                    format!("[{} images attached]", image_count)
                }
            } else {
                real_text
            };

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
            pending.suppress_next_turn_aborted_marker = false;
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
                tool_call_id: if call_id.is_empty() {
                    None
                } else {
                    Some(call_id.to_string())
                },
                source_offset: line_offset,
                raw_type: "codex_function_call".to_string(),
                raw_line: Some(raw_line.to_string()),
            });
        }
        "function_call_output" => {
            pending.suppress_next_turn_aborted_marker = false;
            let call_id = payload.call_id.as_deref().unwrap_or("");
            let uuid_suffix = if call_id.is_empty() { "0" } else { call_id };

            // Resolve the tool-result text. String outputs are opaque (kept as-is,
            // even if they happen to look like JSON). Array outputs are content
            // items: join their text, and when the only content is image(s), emit a
            // placeholder so the result event still exists. The image bytes
            // themselves are captured by source-line media redaction and bound back
            // to this event via its source coordinate, so we do not extract media here.
            let tool_output_text = match payload.output.as_ref() {
                Some(CodexFunctionOutput::Text(text)) => {
                    if text.is_empty() {
                        return;
                    }
                    text.clone()
                }
                Some(CodexFunctionOutput::Items(items)) => {
                    let image_count = items
                        .iter()
                        .filter(|item| item.r#type.as_deref() == Some("input_image"))
                        .count();
                    let text: String = items
                        .iter()
                        .filter_map(|item| item.text.as_deref())
                        .filter(|t| !t.trim().is_empty())
                        .collect::<Vec<_>>()
                        .join("\n");
                    if !text.trim().is_empty() {
                        text
                    } else if image_count == 1 {
                        "[image result]".to_string()
                    } else if image_count > 1 {
                        format!("[{} image results]", image_count)
                    } else {
                        return;
                    }
                }
                None => return,
            };

            events.push(ParsedEvent {
                uuid: format!("{}-result-{}", msg_uuid, uuid_suffix),
                session_id: session_id.to_string(),
                timestamp,
                role: Role::Tool,
                content_text: None,
                tool_name: None,
                tool_input_json: None,
                tool_output_text: Some(tool_output_text),
                tool_call_id: if call_id.is_empty() {
                    None
                } else {
                    Some(call_id.to_string())
                },
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
        let has_tool_result = items
            .iter()
            .any(|item| item.r#type.as_deref() == Some("tool_result"));

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
                    tool_call_id: if tool_id.is_empty() {
                        None
                    } else {
                        Some(tool_id.to_string())
                    },
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

        let result_text = item
            .result_content
            .as_ref()
            .and_then(|raw| extract_text_from_raw_content(raw.get()));

        // Use extracted text, or fall back to "[tool error]" for empty-content error results
        // so the result event is still emitted and the call/result pair stays linked.
        let output_text = match result_text {
            Some(ref t) if !t.is_empty() => Some(t.clone()),
            _ if item.is_error == Some(true) => Some("[tool error]".to_string()),
            _ => Some(EMPTY_TOOL_RESULT_PLACEHOLDER.to_string()),
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
                tool_call_id: if tool_use_id.is_empty() {
                    None
                } else {
                    Some(tool_use_id.to_string())
                },
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

    if trimmed == "null" {
        return None;
    }

    // Plain string: "some text"
    if trimmed.starts_with('"') {
        if let Ok(s) = serde_json::from_str::<String>(trimmed) {
            return Some(s);
        }
    }

    // Array of content parts
    if trimmed.starts_with('[') {
        #[derive(Deserialize)]
        struct ToolResultPart {
            r#type: Option<String>,
            text: Option<String>,
            tool_name: Option<String>,
        }

        if let Ok(parts) = serde_json::from_str::<Vec<ToolResultPart>>(trimmed) {
            let mut texts = Vec::new();
            for part in &parts {
                if part.r#type.as_deref() == Some("text") {
                    if let Some(ref text) = part.text {
                        texts.push(text.clone());
                    }
                }
            }
            if !texts.is_empty() {
                return Some(texts.join("\n"));
            }

            let image_count = parts
                .iter()
                .filter(|part| part.r#type.as_deref() == Some("image"))
                .count();
            if image_count > 0 {
                return Some(if image_count == 1 {
                    "[image result]".to_string()
                } else {
                    format!("[{} image results]", image_count)
                });
            }

            let tool_refs: Vec<String> = parts
                .iter()
                .filter(|part| part.r#type.as_deref() == Some("tool_reference"))
                .filter_map(|part| part.tool_name.as_ref().cloned())
                .collect();
            if !tool_refs.is_empty() {
                let preview = tool_refs
                    .iter()
                    .take(3)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ");
                let suffix = if tool_refs.len() > 3 {
                    format!(", +{} more", tool_refs.len() - 3)
                } else {
                    String::new()
                };
                return Some(format!("[tool references: {}{}]", preview, suffix));
            }

            let mut part_types: Vec<String> = Vec::new();
            for part in &parts {
                if let Some(part_type) = part.r#type.as_ref() {
                    if !part_types.iter().any(|existing| existing == part_type) {
                        part_types.push(part_type.clone());
                    }
                }
            }
            if !part_types.is_empty() {
                return Some(format!("[non-text tool result: {}]", part_types.join(", ")));
            }

            return None;
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
    let start = bytes
        .iter()
        .position(|&b| !b.is_ascii_whitespace())
        .unwrap_or(bytes.len());
    let end = bytes
        .iter()
        .rposition(|&b| !b.is_ascii_whitespace())
        .map_or(start, |p| p + 1);
    &bytes[start..end]
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use sha2::{Digest, Sha256};
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
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Hello world"},"cwd":"/home/user/project","gitBranch":"main"}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Hello world")
        );
        assert_eq!(result.metadata.cwd.as_deref(), Some("/home/user/project"));
        assert_eq!(result.metadata.git_branch.as_deref(), Some("main"));
        assert_eq!(result.metadata.project.as_deref(), Some("project"));
    }

    #[test]
    fn test_parse_claude_sidechain_parentage() {
        let dir = tempfile::tempdir().unwrap();
        let parent_id = "f6a553e2-8aca-49c4-9823-3b3d8690fd2e";
        let path = make_jsonl_file(
            dir.path(),
            "agent-a0325d64b2dc7300f.jsonl",
            &[&json!({
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-06-02T00:19:31.215Z",
                "isSidechain": true,
                "sessionId": parent_id,
                "agentId": "a0325d64b2dc7300f",
                "promptId": "be1331ba-91c3-4670-a113-7f1c63773df8",
                "cwd": "/Users/davidrose/git/cipher982",
                "gitBranch": "main",
                "message": {"content": "Deploy crims on drose.io"}
            })
            .to_string()],
        );

        let result = parse_session_file(&path, 0).unwrap();

        assert!(result.metadata.is_sidechain);
        assert_ne!(result.metadata.session_id, parent_id);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some(parent_id)
        );
        assert_eq!(
            result.metadata.subagent_id.as_deref(),
            Some("a0325d64b2dc7300f")
        );
        assert_eq!(
            result.metadata.subagent_prompt_id.as_deref(),
            Some("be1331ba-91c3-4670-a113-7f1c63773df8")
        );
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].session_id, result.metadata.session_id);
    }

    #[test]
    fn test_parse_user_message_filters_head_branch() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"Hello world"},"cwd":"/home/user/project","gitBranch":"HEAD"}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.metadata.git_branch, None);
    }

    #[test]
    fn test_parse_assistant_text_and_tool() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"Let me check"},{"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"/tmp/foo"}}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);

        // First event: text
        assert_eq!(result.events[0].role, Role::Assistant);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Let me check")
        );
        assert!(
            result.events[0].raw_line.is_some(),
            "First event should have raw_line"
        );

        // Second event: tool_use
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(result.events[1].tool_name.as_deref(), Some("Read"));
        assert!(
            result.events[1].raw_line.is_none(),
            "Second event should NOT have raw_line"
        );
    }

    #[test]
    fn test_raw_line_dedup() {
        let dir = tempfile::tempdir().unwrap();
        // Assistant line with 3 content items → should yield 3 events, only first has raw_line
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"one"},{"type":"text","text":"two"},{"type":"text","text":"three"}]}}"#,
            ],
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
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"t1","content":"file contents here"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some("file contents here")
        );
    }

    #[test]
    fn test_tool_result_image_content_emits_placeholder() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"t-image","content":[{"type":"image","source":{"type":"base64","data":"abc123"}}]}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("t-image"));
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some("[image result]")
        );
    }

    #[test]
    fn test_tool_result_tool_references_emit_placeholder() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"t-search","content":[{"type":"tool_reference","tool_name":"TaskCreate"},{"type":"tool_reference","tool_name":"TaskUpdate"},{"type":"tool_reference","tool_name":"TaskList"}]}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("t-search"));
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some("[tool references: TaskCreate, TaskUpdate, TaskList]")
        );
    }

    #[test]
    fn test_emit_compaction_metadata_types() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"summary","summary":"Conversation compacted at checkpoint"}"#,
                r#"{"type":"file-history-snapshot","snapshot":{"timestamp":"2026-01-01T00:00:01Z"}}"#,
                r#"{"type":"system","subtype":"compact_boundary","content":"Conversation compacted","timestamp":"2026-01-01T00:00:01Z","compactMetadata":{"trigger":"auto","preTokens":155708}}"#,
                r#"{"type":"progress","timestamp":"2026-01-01T00:00:02Z"}"#,
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:03Z","message":{"content":"real message"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 4);

        assert_eq!(result.events[0].role, Role::System);
        assert_eq!(result.events[0].raw_type, "summary");
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Conversation compacted at checkpoint")
        );

        assert_eq!(result.events[1].role, Role::System);
        assert_eq!(result.events[1].raw_type, "file-history-snapshot");
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("File history snapshot (2026-01-01T00:00:01Z)")
        );

        assert_eq!(result.events[2].role, Role::System);
        assert_eq!(result.events[2].raw_type, "compact_boundary");
        assert_eq!(
            result.events[2].content_text.as_deref(),
            Some("Conversation compacted [trigger=auto pre_tokens=155708]")
        );

        // progress stays skipped (high-volume hook noise)
        assert_eq!(result.events[3].role, Role::User);
        assert_eq!(
            result.events[3].content_text.as_deref(),
            Some("real message")
        );
    }

    #[test]
    fn test_source_lines_capture_full_lines_including_metadata() {
        let dir = tempfile::tempdir().unwrap();
        let raw_meta = r#"  {"type":"progress","timestamp":"2026-01-01T00:00:00Z"}  "#;
        let raw_user = r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:01Z","message":{"content":"hello"}}"#;
        let path = make_jsonl_file(dir.path(), "test-session.jsonl", &[raw_meta, raw_user]);

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(
            result.events.len(),
            1,
            "metadata lines should not become events"
        );
        assert_eq!(
            result.source_lines.len(),
            2,
            "all source lines should be archived"
        );
        assert_eq!(result.source_lines[0].source_offset, 0);
        assert_eq!(result.source_lines[0].raw_line, raw_meta);
        assert_eq!(
            result.source_lines[1].source_offset,
            (raw_meta.len() + 1) as u64
        );
        assert_eq!(result.source_lines[1].raw_line, raw_user);
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
        let complete = r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"complete"}}"#;
        {
            let mut f = std::fs::File::create(&path).unwrap();
            // Complete line + partial line (no trailing newline)
            write!(
                f,
                "{}\n{}",
                complete,
                r#"{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:01Z","message":{"con"#
            )
            .unwrap();
        }

        let result = parse_session_file(&path, 0).unwrap();
        // mmap parser: only the complete line should be parsed
        // The partial line has no \n so it's treated as incomplete
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].content_text.as_deref(), Some("complete"));
        assert_eq!(result.last_good_offset, (complete.len() + 1) as u64);
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
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Hello from Codex")
        );
        assert_eq!(result.events[0].raw_type, "codex_user");
        // Metadata from session_meta
        assert_eq!(result.metadata.cwd.as_deref(), Some("/Users/test/project"));
        assert_eq!(result.metadata.version.as_deref(), Some("0.1.2"));
        assert_eq!(
            result.metadata.session_id,
            "019c638d-ea04-7983-a845-d0b68a77fa62"
        );
    }

    #[test]
    fn test_codex_assistant_message() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000001.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:12Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Here is the answer"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Assistant);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Here is the answer")
        );
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
        assert_eq!(
            result.events[1].tool_output_text.as_deref(),
            Some("file1.txt\nfile2.txt")
        );
        assert_eq!(result.events[1].raw_type, "codex_function_call_output");
    }

    #[test]
    fn test_codex_function_call_output_image_array_emits_tool_event() {
        // Codex image-returning tools (e.g. view_image) emit function_call_output
        // with `output` as an ARRAY of content items instead of a string. The line
        // must still parse and emit a role=Tool result event carrying a placeholder,
        // so the image (captured separately by source-line media redaction) has an
        // event to bind to. Regression for historical screenshots never rendering.
        let dir = tempfile::tempdir().unwrap();
        let img = format!("data:image/png;base64,{}", "A".repeat(800));
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000ff02.jsonl",
            &[
                // image-only result
                &format!(
                    r#"{{"type":"response_item","timestamp":"2026-02-15T17:06:14Z","payload":{{"type":"function_call_output","call_id":"call_img","output":[{{"type":"input_image","image_url":"{img}"}}]}}}}"#
                ),
                // result array carrying text plus an image -> text wins
                &format!(
                    r#"{{"type":"response_item","timestamp":"2026-02-15T17:06:15Z","payload":{{"type":"function_call_output","call_id":"call_both","output":[{{"type":"output_text","text":"saw a cat"}},{{"type":"input_image","image_url":"{img}"}}]}}}}"#
                ),
                // plain string result still works unchanged
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:16Z","payload":{"type":"function_call_output","call_id":"call_text","output":"plain text"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 3, "all three results must emit events");

        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("call_img"));
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some("[image result]")
        );

        assert_eq!(result.events[1].tool_call_id.as_deref(), Some("call_both"));
        assert_eq!(
            result.events[1].tool_output_text.as_deref(),
            Some("saw a cat")
        );

        assert_eq!(result.events[2].tool_call_id.as_deref(), Some("call_text"));
        assert_eq!(
            result.events[2].tool_output_text.as_deref(),
            Some("plain text")
        );

        // The inline image bytes should be captured as media objects by source-line
        // redaction, independent of the event.
        assert!(
            !result.media_objects.is_empty(),
            "image tool result bytes should be redacted into media objects"
        );
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
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("real user message")
        );
    }

    #[test]
    fn test_codex_turn_aborted_event_msg_becomes_system_action_source() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000ab01.jsonl",
            &[
                r#"{"type":"event_msg","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"turn_aborted","turn_id":"turn_123","reason":"interrupted"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:12Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"next real prompt"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);
        assert_eq!(result.events[0].role, Role::System);
        assert_eq!(result.events[0].raw_type, CODEX_TURN_INTERRUPTED_RAW_TYPE);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some(CODEX_TURN_INTERRUPTED_TEXT)
        );
        assert_eq!(result.events[1].role, Role::User);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("next real prompt")
        );
    }

    #[test]
    fn test_codex_turn_aborted_non_interrupted_reason_is_ignored() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000ab11.jsonl",
            &[
                r#"{"type":"event_msg","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"turn_aborted","turn_id":"turn_123","reason":"timeout"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:12Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"real prompt"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("real prompt")
        );
    }

    #[test]
    fn test_codex_turn_aborted_marker_only_becomes_system_action_source() {
        let dir = tempfile::tempdir().unwrap();
        let marker_text =
            "<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>";
        let marker_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-02-15T17:06:11Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": marker_text}]
            }
        })
        .to_string();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000ab02.jsonl",
            &[&marker_line],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::System);
        assert_eq!(
            result.events[0].raw_type,
            CODEX_TURN_INTERRUPTED_MARKER_RAW_TYPE
        );
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some(CODEX_TURN_INTERRUPTED_TEXT)
        );
    }

    #[test]
    fn test_codex_turn_aborted_paired_marker_dedupes_to_one_action_source() {
        let dir = tempfile::tempdir().unwrap();
        let marker_text =
            "<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>";
        let marker_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-02-15T17:06:11Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": marker_text}]
            }
        })
        .to_string();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000ab03.jsonl",
            &[
                r#"{"type":"event_msg","timestamp":"2026-02-15T17:06:10Z","payload":{"type":"turn_aborted","turn_id":"turn_123","reason":"interrupted"}}"#,
                &marker_line,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::System);
        assert_eq!(result.events[0].raw_type, CODEX_TURN_INTERRUPTED_RAW_TYPE);
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
        assert_eq!(
            result.metadata.session_id,
            "019c638d-ea04-7983-a845-d0b68a77fa62"
        );
        assert_eq!(result.metadata.cwd.as_deref(), Some("/Users/test/zorb"));
        assert_eq!(
            result.metadata.git_branch.as_deref(),
            Some("feature/my-branch")
        );
        // git_repo from session_meta payload, not disk
        assert_eq!(
            result.metadata.git_repo.as_deref(),
            Some("git@github.com:org/zorb.git")
        );
        // project derived from cwd basename
        assert_eq!(result.metadata.project.as_deref(), Some("zorb"));
    }

    #[test]
    fn resolve_git_info_does_not_promote_generic_workspace_basename() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        std::fs::create_dir_all(&workspace).unwrap();

        let (project, git_repo) = resolve_git_info(&workspace);

        assert_eq!(project, None);
        assert_eq!(git_repo, None);
    }

    #[test]
    fn resolve_git_info_keeps_workspace_when_it_is_a_git_root() {
        let dir = tempfile::tempdir().unwrap();
        let workspace = dir.path().join("workspace");
        std::fs::create_dir_all(workspace.join(".git")).unwrap();

        let (project, _git_repo) = resolve_git_info(&workspace);

        assert_eq!(project.as_deref(), Some("workspace"));
    }

    #[test]
    fn test_codex_session_meta_filters_head_branch() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-01-10T11-00-00-019c638d-ea04-7983-a845-d0b68a77fa62.jsonl",
            &[
                r#"{"timestamp":"2026-01-10T11:00:00.000Z","type":"session_meta","payload":{"id":"019c638d-ea04-7983-a845-d0b68a77fa62","cwd":"/Users/test/zorb","cli_version":"0.105.0","git":{"commit_hash":"abc123","branch":"HEAD","repository_url":"git@github.com:org/zorb.git"}}}"#,
                r#"{"timestamp":"2026-01-10T11:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.metadata.git_branch, None);
        assert_eq!(
            result.metadata.git_repo.as_deref(),
            Some("git@github.com:org/zorb.git")
        );
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
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        // Should be a valid UUID (v5 derived from path)
        assert!(
            Uuid::parse_str(&result.metadata.session_id).is_ok(),
            "Non-UUID filename should get a deterministic UUID, got: {}",
            result.metadata.session_id
        );

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
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hello"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(
            result.metadata.session_id,
            "3334cc69-974a-46a5-84e3-64459521135c"
        );
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
        assert_eq!(
            result.metadata.session_id,
            "019c638d-ea04-7983-a845-d0b68a77fa62"
        );
        assert_eq!(
            result.events[0].session_id,
            "019c638d-ea04-7983-a845-d0b68a77fa62"
        );
    }

    #[test]
    fn test_codex_offset_recovers_session_meta_id_buffered() {
        let dir = tempfile::tempdir().unwrap();
        let canonical_id = "019c638d-ea04-7983-a845-d0b68a77fa62";
        let session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-02-15T17:06:10Z\",\"payload\":{{\"id\":\"{}\",\"cwd\":\"/test\"}}}}",
            canonical_id
        );
        let user_line = r#"{"type":"response_item","timestamp":"2026-02-15T17:06:11Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-02-15T17-06-10-suffix.jsonl",
            &[&session_meta, user_line],
        );

        // Skip the session_meta line to simulate incremental parse without stored session_id.
        let offset = (session_meta.len() + 1) as u64;
        let result = parse_session_file(&path, offset).unwrap();

        assert_eq!(result.metadata.session_id, canonical_id);
        assert_eq!(result.events.len(), 1);
    }

    #[test]
    fn test_codex_offset_recovers_forked_from_session_id() {
        let dir = tempfile::tempdir().unwrap();
        let child_id = "019c638d-ea04-7983-a845-d0b68a77fa62";
        let parent_id = "019c638d-ea04-7983-a845-d0b68a77fa63";
        let session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-02-15T17:06:10Z\",\"payload\":{{\"id\":\"{}\",\"forked_from_id\":\"{}\",\"cwd\":\"/test\"}}}}",
            child_id, parent_id
        );
        let user_line = r#"{"type":"response_item","timestamp":"2026-02-15T17:06:11Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-02-15T17-06-10-suffix.jsonl",
            &[&session_meta, user_line],
        );

        let offset = (session_meta.len() + 1) as u64;
        let result = parse_session_file(&path, offset).unwrap();

        assert_eq!(result.metadata.session_id, child_id);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some(parent_id)
        );
        assert!(result.metadata.is_sidechain);
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].session_id, child_id);
    }

    #[test]
    fn test_codex_session_meta_source_thread_spawn_marks_sidechain() {
        let dir = tempfile::tempdir().unwrap();
        let child_id = "019ddb6e-114f-7643-89db-86c31a2aa706";
        let parent_id = "019dd708-573a-7131-a4d9-9ee855520483";
        let session_meta = json!({
            "type": "session_meta",
            "timestamp": "2026-04-29T19:48:36Z",
            "payload": {
                "id": child_id,
                "cwd": "/Users/test/project",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1,
                            "agent_nickname": "Ptolemy",
                            "agent_role": "default"
                        }
                    }
                }
            }
        })
        .to_string();
        let user_line = r#"{"type":"response_item","timestamp":"2026-04-29T19:48:37Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-child.jsonl",
            &[&session_meta, user_line],
        );

        let result = parse_session_file(&path, 0).unwrap();

        assert_eq!(result.metadata.session_id, child_id);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some(parent_id)
        );
        assert!(result.metadata.is_sidechain);
        assert_eq!(result.events[0].session_id, child_id);
    }

    #[test]
    fn test_codex_offset_recovers_source_thread_spawn_parent() {
        let dir = tempfile::tempdir().unwrap();
        let child_id = "019ddb6e-114f-7643-89db-86c31a2aa706";
        let parent_id = "019dd708-573a-7131-a4d9-9ee855520483";
        let session_meta = json!({
            "type": "session_meta",
            "timestamp": "2026-04-29T19:48:36Z",
            "payload": {
                "id": child_id,
                "cwd": "/Users/test/project",
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_id,
                            "depth": 1
                        }
                    }
                }
            }
        })
        .to_string();
        let user_line = r#"{"type":"response_item","timestamp":"2026-04-29T19:48:37Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-child.jsonl",
            &[&session_meta, user_line],
        );

        let offset = (session_meta.len() + 1) as u64;
        let result = parse_session_file(&path, offset).unwrap();

        assert_eq!(result.metadata.session_id, child_id);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some(parent_id)
        );
        assert!(result.metadata.is_sidechain);
        assert_eq!(result.events[0].session_id, child_id);
    }

    #[test]
    fn test_codex_non_thread_spawn_subagent_marks_sidechain_without_parent() {
        let dir = tempfile::tempdir().unwrap();
        let child_id = "019ddb6e-114f-7643-89db-86c31a2aa706";
        let session_meta = json!({
            "type": "session_meta",
            "timestamp": "2026-04-29T19:48:36Z",
            "payload": {
                "id": child_id,
                "cwd": "/Users/test/project",
                "source": {
                    "subagent": {
                        "review": {}
                    }
                }
            }
        })
        .to_string();
        let user_line = r#"{"type":"response_item","timestamp":"2026-04-29T19:48:37Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-review-child.jsonl",
            &[&session_meta, user_line],
        );

        let result = parse_session_file(&path, 0).unwrap();

        assert_eq!(result.metadata.session_id, child_id);
        assert_eq!(result.metadata.forked_from_session_id, None);
        assert!(result.metadata.is_sidechain);
        assert_eq!(result.events[0].session_id, child_id);
    }

    #[test]
    fn test_codex_first_session_meta_wins_when_parent_context_is_injected() {
        let dir = tempfile::tempdir().unwrap();
        let child_id = "019d1bb1-15c1-78c0-b4bc-f830965f237b";
        let parent_id = "019d1805-66b6-78f1-aca9-91225867663d";
        let child_session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-03-23T17:14:43.614Z\",\"payload\":{{\"id\":\"{}\",\"forked_from_id\":\"{}\",\"cwd\":\"/Users/test/project\"}}}}",
            child_id, parent_id
        );
        let parent_session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-03-23T17:14:43.615Z\",\"payload\":{{\"id\":\"{}\",\"cwd\":\"/Users/test/project\"}}}}",
            parent_id
        );
        let user_line = r#"{"type":"response_item","timestamp":"2026-03-23T17:14:44.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from child"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-03-23T17-14-43-child.jsonl",
            &[&child_session_meta, &parent_session_meta, user_line],
        );

        let result = parse_session_file(&path, 0).unwrap();

        assert_eq!(result.metadata.session_id, child_id);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some(parent_id)
        );
        assert!(result.metadata.is_sidechain);
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].session_id, child_id);
    }

    #[test]
    fn test_codex_offset_keeps_child_session_meta_when_parent_context_is_injected() {
        let dir = tempfile::tempdir().unwrap();
        let child_id = "019d1bb1-15c1-78c0-b4bc-f830965f237b";
        let parent_id = "019d1805-66b6-78f1-aca9-91225867663d";
        let child_session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-03-23T17:14:43.614Z\",\"payload\":{{\"id\":\"{}\",\"forked_from_id\":\"{}\",\"cwd\":\"/Users/test/project\"}}}}",
            child_id, parent_id
        );
        let parent_session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-03-23T17:14:43.615Z\",\"payload\":{{\"id\":\"{}\",\"cwd\":\"/Users/test/project\"}}}}",
            parent_id
        );
        let user_line = r#"{"type":"response_item","timestamp":"2026-03-23T17:14:44.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from child"}]}}"#;
        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-03-23T17-14-43-child.jsonl",
            &[&child_session_meta, &parent_session_meta, user_line],
        );

        let offset = (child_session_meta.len() + 1) as u64;
        let result = parse_session_file(&path, offset).unwrap();

        assert_eq!(result.metadata.session_id, child_id);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some(parent_id)
        );
        assert!(result.metadata.is_sidechain);
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].session_id, child_id);
    }

    #[test]
    fn test_codex_offset_recovers_session_meta_id_mmap() {
        let dir = tempfile::tempdir().unwrap();
        let canonical_id = "019c638d-ea04-7983-a845-d0b68a77fa62";
        let session_meta = format!(
            "{{\"type\":\"session_meta\",\"timestamp\":\"2026-02-15T17:06:10Z\",\"payload\":{{\"id\":\"{}\",\"cwd\":\"/test\"}}}}",
            canonical_id
        );

        // Force mmap path by making total file size > MMAP_THRESHOLD.
        let big_text = "x".repeat((MMAP_THRESHOLD as usize) + 2048);
        let large_user_line = json!({
            "type": "response_item",
            "timestamp": "2026-02-15T17:06:11Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": big_text}],
            }
        })
        .to_string();

        let path = make_jsonl_file(
            dir.path(),
            "rollout-2026-02-15T17-06-10-large.jsonl",
            &[&session_meta, &large_user_line],
        );

        // Skip the session_meta line to simulate incremental parse without stored session_id.
        let offset = (session_meta.len() + 1) as u64;
        let result = parse_session_file(&path, offset).unwrap();

        assert_eq!(result.metadata.session_id, canonical_id);
        assert_eq!(result.events.len(), 1);
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
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("What is 2+2?")
        );
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("2+2 equals 4.")
        );
        // Session ID from document takes precedence over stem-derived
        assert_eq!(
            result.metadata.session_id,
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        );
        assert!(result.metadata.started_at.is_some());
    }

    #[test]
    fn test_gemini_preserves_full_source_lines_for_lossless_export() {
        let dir = tempfile::tempdir().unwrap();
        let session_json = "{\n  \"sessionId\": \"a1b2c3d4-e5f6-7890-abcd-ef1234567890\",\n  \"messages\": [\n    {\n      \"id\": \"11111111-1111-1111-1111-111111111111\",\n      \"timestamp\": \"2026-01-10T10:00:00Z\",\n      \"type\": \"user\",\n      \"content\": \"What is 2+2?\"\n    }\n  ]\n}\n";
        let path = make_json_file(
            dir.path(),
            "session-2026-01-10T10-00-00-a1b2c3d4.json",
            session_json,
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert!(!result.source_lines.is_empty());
        assert_eq!(result.source_lines[0].source_offset, 0);

        let rebuilt = result
            .source_lines
            .iter()
            .map(|line| line.raw_line.as_str())
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        assert_eq!(rebuilt, session_json);
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
        let path = make_json_file(
            dir.path(),
            "session-2026-01-10T11-00-00-bbbb.json",
            session_json,
        );

        let result = parse_session_file(&path, 0).unwrap();
        // user message + assistant text + tool call = 3 events
        assert_eq!(result.events.len(), 3);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("I will read it now.")
        );
        assert_eq!(result.events[2].role, Role::Assistant);
        assert_eq!(result.events[2].tool_name.as_deref(), Some("read_file"));
        assert!(result.events[2].tool_input_json.is_some());
    }

    #[test]
    fn test_gemini_parse_tool_call_results() {
        let dir = tempfile::tempdir().unwrap();
        let session_json = r#"{
            "sessionId": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            "startTime": "2026-01-10T11:00:00Z",
            "messages": [
                {
                    "id": "10101010-1010-1010-1010-101010101010",
                    "timestamp": "2026-01-10T11:00:00Z",
                    "type": "user",
                    "content": "Read README and then write a note"
                },
                {
                    "id": "20202020-2020-2020-2020-202020202020",
                    "timestamp": "2026-01-10T11:00:05Z",
                    "type": "gemini",
                    "content": "Running tools now.",
                    "toolCalls": [
                        {
                            "id": "tc-read",
                            "name": "read_file",
                            "args": {"file_path": "README.md"},
                            "status": "success",
                            "timestamp": "2026-01-10T11:00:06Z",
                            "result": [
                                {
                                    "functionResponse": {
                                        "id": "tc-read",
                                        "name": "read_file",
                                        "response": {
                                            "output": "README content here"
                                        }
                                    }
                                }
                            ]
                        },
                        {
                            "id": "tc-write",
                            "name": "write_file",
                            "args": {"file_path": "note.txt", "content": "done"},
                            "status": "cancelled",
                            "timestamp": "2026-01-10T11:00:07Z",
                            "result": [
                                {
                                    "functionResponse": {
                                        "id": "tc-write",
                                        "name": "write_file",
                                        "response": {
                                            "error": "[Operation Cancelled] Reason: User cancelled the operation."
                                        }
                                    }
                                }
                            ]
                        }
                    ]
                }
            ]
        }"#;
        let path = make_json_file(dir.path(), "session-gemini-tools.json", session_json);

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 6);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[1].role, Role::Assistant);

        // First tool call + result pair
        assert_eq!(result.events[2].role, Role::Assistant);
        assert_eq!(result.events[2].tool_name.as_deref(), Some("read_file"));
        assert_eq!(result.events[2].tool_call_id.as_deref(), Some("tc-read"));
        assert_eq!(result.events[3].role, Role::Tool);
        assert_eq!(result.events[3].tool_call_id.as_deref(), Some("tc-read"));
        assert_eq!(
            result.events[3].tool_output_text.as_deref(),
            Some("README content here")
        );
        assert_eq!(result.events[3].raw_type, "gemini_tool_result");

        // Second tool call + error result pair
        assert_eq!(result.events[4].role, Role::Assistant);
        assert_eq!(result.events[4].tool_name.as_deref(), Some("write_file"));
        assert_eq!(result.events[4].tool_call_id.as_deref(), Some("tc-write"));
        assert_eq!(result.events[5].role, Role::Tool);
        assert_eq!(result.events[5].tool_call_id.as_deref(), Some("tc-write"));
        assert!(result.events[5]
            .tool_output_text
            .as_deref()
            .unwrap_or("")
            .contains("cancelled"));
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
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Hello Gemini")
        );
    }

    #[test]
    fn test_gemini_invalid_json_returns_empty() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_json_file(dir.path(), "session-bad.json", "not valid json {{{");

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 0);
    }

    #[test]
    fn test_gemini_invalid_surrogate_escape_is_repaired() {
        let dir = tempfile::tempdir().unwrap();
        let session_json = r#"{
            "sessionId": "f4a223b2-5db9-4908-b469-0fd0ca858f93",
            "messages": [
                {
                    "id": "abababab-abab-abab-abab-abababababab",
                    "timestamp": "2026-01-10T12:00:00Z",
                    "type": "user",
                    "content": "bad \ud83d text"
                }
            ]
        }"#;
        let path = make_json_file(dir.path(), "session-invalid-surrogate.json", session_json);

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::User);
        let text = result.events[0].content_text.as_deref().unwrap_or("");
        assert!(text.contains("bad"));
        assert!(text.contains("text"));
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
            &[
                r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"tool_use","id":"toolu_bdrk_01ABC","name":"Bash","input":{"command":"ls"}}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].tool_name.as_deref(), Some("Bash"));
        assert_eq!(
            result.events[0].tool_call_id.as_deref(),
            Some("toolu_bdrk_01ABC")
        );
    }

    #[test]
    fn test_claude_tool_result_carries_tool_call_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_bdrk_01ABC","content":"file contents here"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(
            result.events[0].tool_call_id.as_deref(),
            Some("toolu_bdrk_01ABC")
        );
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
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_01ERR","content":"","is_error":true}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::Tool);
        assert_eq!(
            result.events[0].tool_call_id.as_deref(),
            Some("toolu_01ERR")
        );
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some("[tool error]")
        );
    }

    #[test]
    fn test_empty_success_tool_results_emit_placeholder() {
        // Empty stdout is still a completed tool result. Dropping the event
        // leaves the assistant call looking orphaned/running forever.
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_empty","content":""}]}}"#,
                r#"{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_empty_text","content":[{"type":"text","text":""}]}]}}"#,
                r#"{"type":"user","uuid":"u3","timestamp":"2026-01-01T00:00:04Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_missing_content"}]}}"#,
                r#"{"type":"user","uuid":"u4","timestamp":"2026-01-01T00:00:05Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_empty_list","content":[]}]}}"#,
                r#"{"type":"user","uuid":"u5","timestamp":"2026-01-01T00:00:06Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_null","content":null}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 5);
        assert_eq!(
            result.events[0].tool_call_id.as_deref(),
            Some("toolu_empty")
        );
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
        assert_eq!(
            result.events[1].tool_call_id.as_deref(),
            Some("toolu_empty_text")
        );
        assert_eq!(
            result.events[1].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
        assert_eq!(
            result.events[2].tool_call_id.as_deref(),
            Some("toolu_missing_content")
        );
        assert_eq!(
            result.events[2].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
        assert_eq!(
            result.events[3].tool_call_id.as_deref(),
            Some("toolu_empty_list")
        );
        assert_eq!(
            result.events[3].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
        assert_eq!(result.events[4].tool_call_id.as_deref(), Some("toolu_null"));
        assert_eq!(
            result.events[4].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
    }

    #[test]
    fn test_json_object_tool_result_emits_raw_json_output() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_object","content":{"status":"ok","count":0}}]}}"#,
                r#"{"type":"user","uuid":"u2","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_false","content":false}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some(r#"{"status":"ok","count":0}"#)
        );
        assert_eq!(result.events[1].tool_output_text.as_deref(), Some("false"));
    }

    #[test]
    fn test_is_error_with_content_uses_content() {
        // is_error:true WITH content should use the actual content, not placeholder
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_01ERR","content":"The user rejected this action.","is_error":true}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(
            result.events[0].tool_output_text.as_deref(),
            Some("The user rejected this action.")
        );
    }

    // -----------------------------------------------------------------------
    // Codex image + context injection tests
    // -----------------------------------------------------------------------

    #[test]
    fn test_codex_image_only_message_emits_placeholder() {
        // Image-only user message must still emit an event so assistant isn't first
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000010.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":"data:image/png;base64,abc123"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(
            result.events.len(),
            1,
            "image-only message should emit placeholder event"
        );
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("[image attached]")
        );
    }

    #[test]
    fn test_codex_large_inline_image_source_line_is_redacted() {
        let dir = tempfile::tempdir().unwrap();
        let image_data = "A".repeat(4096);
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000012.jsonl",
            &[&line],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.source_lines.len(), 1);
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.media_objects.len(), 1);

        let source_line = &result.source_lines[0].raw_line;
        assert!(source_line.contains("longhouse_media_ref:sha256="));
        assert!(source_line.contains(";mime=image/png;"));
        assert!(source_line.contains(";original_chars=4118"));
        assert!(!source_line.contains(&image_data));
        assert!(source_line.len() < 512);

        let event_raw = result.events[0].raw_line.as_deref().unwrap_or("");
        assert!(event_raw.contains("longhouse_media_ref:sha256="));
        assert!(!event_raw.contains(&image_data));

        let media = &result.media_objects[0];
        assert_eq!(media.source_offset, 0);
        assert_eq!(media.mime_type, "image/png");
        assert_eq!(media.byte_size, 3072);
        assert_eq!(media.original_chars, 4118);
        assert_eq!(media.bytes, vec![0u8; 3072]);
        assert_eq!(
            media.original_line_sha256,
            format!("{:x}", Sha256::digest(line.as_bytes()))
        );
    }

    #[test]
    fn test_codex_image_with_text_strips_wrapper_tags() {
        // Mixed content: image wrapper tags stripped, real text preserved
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000011.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<image name=[Image #1]>"},{"type":"input_image","image_url":"data:image/png;base64,abc"},{"type":"input_text","text":"</image>"},{"type":"input_text","text":"[Image #1]\n\nwhat is in this screenshot?"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        let text = result.events[0].content_text.as_deref().unwrap_or("");
        // Wrapper tags stripped, real prompt preserved
        assert!(!text.contains("<image "), "wrapper tag should be stripped");
        assert!(!text.contains("</image>"), "closing tag should be stripped");
        assert!(
            text.contains("what is in this screenshot?"),
            "real prompt should be kept"
        );
    }

    #[test]
    fn test_codex_context_injection_filtered() {
        // AGENTS.md and environment context injected as role=user must be dropped
        let dir = tempfile::tempdir().unwrap();

        // Build lines programmatically to avoid backslash escaping issues in raw strings
        let agents_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:00Z",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions for /Users/foo\n\n<INSTRUCTIONS>...</INSTRUCTIONS>"}]
            }
        }).to_string();
        let env_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:00Z",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "<environment_context><cwd>/Users/foo</cwd></environment_context>"}]
            }
        }).to_string();
        let real_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:01Z",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "please help me debug this"}]
            }
        })
        .to_string();

        let path = {
            let path = dir
                .path()
                .join("019c638d-0000-0000-0000-000000000012.jsonl");
            let mut f = std::fs::File::create(&path).unwrap();
            use std::io::Write;
            writeln!(f, "{}", agents_line).unwrap();
            writeln!(f, "{}", env_line).unwrap();
            writeln!(f, "{}", real_line).unwrap();
            path
        };

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(
            result.events.len(),
            1,
            "only real user message should survive"
        );
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("please help me debug this")
        );
    }

    #[test]
    fn test_codex_console_run_once_context_unwrapped() {
        let dir = tempfile::tempdir().unwrap();
        let wrapped_prompt = crate::console_prompt::wrap_console_run_once_prompt(
            "please research the steering shaft options",
        );
        let line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:00Z",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": wrapped_prompt}]
            }
        })
        .to_string();
        let path = {
            let path = dir
                .path()
                .join("019c638d-0000-0000-0000-000000000014.jsonl");
            let mut f = std::fs::File::create(&path).unwrap();
            use std::io::Write;
            writeln!(f, "{}", line).unwrap();
            path
        };

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("please research the steering shaft options")
        );
    }

    #[test]
    fn test_codex_multiple_images_placeholder() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000013.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":"data:image/png;base64,a"},{"type":"input_image","image_url":"data:image/png;base64,b"},{"type":"input_image","image_url":"data:image/png;base64,c"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 1);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("[3 images attached]")
        );
    }

    #[test]
    fn test_antigravity_parse_user_tool_and_result() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "53116f30-f150-458c-b36e-2e30f576dc74";
        let transcript_dir = dir
            .path()
            .join(".gemini")
            .join("antigravity")
            .join("brain")
            .join(conversation_id)
            .join(".system_generated")
            .join("logs");
        std::fs::create_dir_all(&transcript_dir).unwrap();
        let path = transcript_dir.join("transcript.jsonl");
        let lines = [
            serde_json::json!({
                "step_index": 0,
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-05-21T22:27:41Z",
                "content": "<USER_REQUEST>\nfix the build\n</USER_REQUEST>"
            })
            .to_string(),
            serde_json::json!({
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-05-21T22:27:42Z",
                "tool_calls": [{"name": "list_dir", "args": {"DirectoryPath": "/tmp"}}]
            })
            .to_string(),
            serde_json::json!({
                "step_index": 2,
                "source": "MODEL",
                "type": "LIST_DIRECTORY",
                "status": "DONE",
                "created_at": "2026-05-21T22:27:43Z",
                "content": "Summary: files listed."
            })
            .to_string(),
            serde_json::json!({
                "step_index": 3,
                "source": "MODEL",
                "type": "FINAL_RESPONSE",
                "status": "DONE",
                "created_at": "2026-05-21T22:27:44Z",
                "content": "Done."
            })
            .to_string(),
        ];
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();

        let result = parse_session_file(&path, 0).unwrap();

        assert_eq!(result.metadata.session_id, conversation_id);
        assert_eq!(result.events.len(), 4);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("fix the build")
        );
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(result.events[1].tool_name.as_deref(), Some("list_dir"));
        assert_eq!(
            result.events[1]
                .tool_input_json
                .as_ref()
                .map(|raw| raw.get()),
            Some(r#"{"DirectoryPath":"/tmp"}"#)
        );
        assert_eq!(result.events[2].role, Role::Tool);
        assert_eq!(
            result.events[2].tool_output_text.as_deref(),
            Some("Summary: files listed.")
        );
        // The result must inherit the adjacent planner call's id even though the tool
        // name alias differs (list_dir -> LIST_DIRECTORY). This is the core pairing fix.
        let call_id = result.events[1].tool_call_id.clone();
        assert_eq!(call_id.as_deref(), Some("antigravity-1-0"));
        assert_eq!(result.events[2].tool_call_id, call_id);
        assert_eq!(result.events[3].role, Role::Assistant);
        assert_eq!(result.events[3].content_text.as_deref(), Some("Done."));
        // The trailing FINAL_RESPONSE is an assistant content record, not a tool result.
        assert_eq!(result.events[3].tool_call_id, None);
        assert!(result
            .events
            .iter()
            .all(|event| event.session_id == conversation_id));
    }

    /// Helper: write an antigravity transcript and return its path.
    fn write_antigravity_transcript(
        dir: &Path,
        conversation_id: &str,
        lines: &[String],
    ) -> std::path::PathBuf {
        let transcript_dir = dir
            .join(".gemini")
            .join("antigravity")
            .join("brain")
            .join(conversation_id)
            .join(".system_generated")
            .join("logs");
        std::fs::create_dir_all(&transcript_dir).unwrap();
        let path = transcript_dir.join("transcript.jsonl");
        std::fs::write(&path, lines.join("\n") + "\n").unwrap();
        path
    }

    #[test]
    fn test_antigravity_multi_tool_call_planner_pairs_in_order() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "11111111-1111-4111-8111-111111111111";
        let lines = [
            serde_json::json!({
                "step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-05-21T22:27:42Z",
                "tool_calls": [
                    {"name": "grep_search", "args": {"Query": "a"}},
                    {"name": "view_file", "args": {"Path": "b"}}
                ]
            })
            .to_string(),
            serde_json::json!({
                "step_index": 1, "source": "MODEL", "type": "GREP_SEARCH", "status": "DONE",
                "created_at": "2026-05-21T22:27:43Z", "content": "grep output"
            })
            .to_string(),
            serde_json::json!({
                "step_index": 2, "source": "MODEL", "type": "VIEW_FILE", "status": "DONE",
                "created_at": "2026-05-21T22:27:44Z", "content": "file output"
            })
            .to_string(),
        ];
        let path = write_antigravity_transcript(dir.path(), conversation_id, &lines);

        let result = parse_session_file(&path, 0).unwrap();
        // 2 calls + 2 results
        let tool_results: Vec<_> = result
            .events
            .iter()
            .filter(|e| e.role == Role::Tool)
            .collect();
        assert_eq!(tool_results.len(), 2);
        // Queue order: first result pairs to first call, second to second.
        assert_eq!(
            tool_results[0].tool_call_id.as_deref(),
            Some("antigravity-0-0")
        );
        assert_eq!(
            tool_results[1].tool_call_id.as_deref(),
            Some("antigravity-0-1")
        );
    }

    #[test]
    fn test_antigravity_result_without_planner_stays_unpaired() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "22222222-2222-4222-8222-222222222222";
        let lines = [serde_json::json!({
            "step_index": 0, "source": "MODEL", "type": "GREP_SEARCH", "status": "DONE",
            "created_at": "2026-05-21T22:27:43Z", "content": "orphan result, no planner before it"
        })
        .to_string()];
        let path = write_antigravity_transcript(dir.path(), conversation_id, &lines);

        let result = parse_session_file(&path, 0).unwrap();
        let tool = result.events.iter().find(|e| e.role == Role::Tool).unwrap();
        assert_eq!(tool.tool_call_id, None);
    }

    #[test]
    fn test_antigravity_interleaving_record_clears_pending() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "33333333-3333-4333-8333-333333333333";
        let lines = [
            serde_json::json!({
                "step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-05-21T22:27:42Z",
                "tool_calls": [{"name": "grep_search", "args": {"Query": "a"}}]
            })
            .to_string(),
            // A user turn interleaves before any result — the call had no result.
            serde_json::json!({
                "step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
                "created_at": "2026-05-21T22:27:43Z",
                "content": "<USER_REQUEST>\nnevermind\n</USER_REQUEST>"
            })
            .to_string(),
            // A later result must NOT steal the cleared call id.
            serde_json::json!({
                "step_index": 2, "source": "MODEL", "type": "GREP_SEARCH", "status": "DONE",
                "created_at": "2026-05-21T22:27:44Z", "content": "late result"
            })
            .to_string(),
        ];
        let path = write_antigravity_transcript(dir.path(), conversation_id, &lines);

        let result = parse_session_file(&path, 0).unwrap();
        let tool = result.events.iter().find(|e| e.role == Role::Tool).unwrap();
        assert_eq!(tool.tool_call_id, None);
    }

    #[test]
    fn test_antigravity_non_model_record_between_planner_and_result_clears_pending() {
        // A non-MODEL antigravity record interleaving a pending call must fail closed,
        // so a later genuine result does not steal the call id.
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "55555555-5555-4555-8555-555555555555";
        let lines = [
            serde_json::json!({
                "step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-05-21T22:27:42Z",
                "tool_calls": [{"name": "grep_search", "args": {"Query": "a"}}]
            })
            .to_string(),
            // A SYSTEM record interleaves (not a MODEL tool result).
            serde_json::json!({
                "step_index": 1, "source": "SYSTEM", "type": "CHECKPOINT", "status": "DONE",
                "created_at": "2026-05-21T22:27:43Z", "content": "checkpoint saved"
            })
            .to_string(),
            serde_json::json!({
                "step_index": 2, "source": "MODEL", "type": "GREP_SEARCH", "status": "DONE",
                "created_at": "2026-05-21T22:27:44Z", "content": "late result"
            })
            .to_string(),
        ];
        let path = write_antigravity_transcript(dir.path(), conversation_id, &lines);

        let result = parse_session_file(&path, 0).unwrap();
        let tool = result.events.iter().find(|e| e.role == Role::Tool).unwrap();
        assert_eq!(tool.tool_call_id, None);
    }

    #[test]
    fn test_antigravity_result_at_wrong_step_does_not_pair() {
        // A MODEL result whose step_index is not the expected next step must not pair,
        // and must clear pending so nothing else pairs to it either.
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "66666666-6666-4666-8666-666666666666";
        let lines = [
            serde_json::json!({
                "step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                "created_at": "2026-05-21T22:27:42Z",
                "tool_calls": [{"name": "grep_search", "args": {"Query": "a"}}]
            })
            .to_string(),
            // Result jumps to step 5 (expected was 1) — treat as non-adjacent.
            serde_json::json!({
                "step_index": 5, "source": "MODEL", "type": "GREP_SEARCH", "status": "DONE",
                "created_at": "2026-05-21T22:27:44Z", "content": "far result"
            })
            .to_string(),
        ];
        let path = write_antigravity_transcript(dir.path(), conversation_id, &lines);

        let result = parse_session_file(&path, 0).unwrap();
        let tool = result.events.iter().find(|e| e.role == Role::Tool).unwrap();
        assert_eq!(tool.tool_call_id, None);
    }

    #[test]
    fn test_antigravity_seeds_pending_across_resume_offset() {
        let dir = tempfile::tempdir().unwrap();
        let conversation_id = "44444444-4444-4444-8444-444444444444";
        let planner = serde_json::json!({
            "step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
            "created_at": "2026-05-21T22:27:42Z",
            "tool_calls": [{"name": "grep_search", "args": {"Query": "a"}}]
        })
        .to_string();
        let result_line = serde_json::json!({
            "step_index": 6, "source": "MODEL", "type": "GREP_SEARCH", "status": "DONE",
            "created_at": "2026-05-21T22:27:43Z", "content": "result in the next batch"
        })
        .to_string();
        let path = write_antigravity_transcript(
            dir.path(),
            conversation_id,
            &[planner.clone(), result_line],
        );

        // Resume offset lands right after the planner line — simulating the shipper
        // having already acked the planner in a prior batch.
        let resume_offset = (planner.len() + 1) as u64;
        let result = parse_session_file(&path, resume_offset).unwrap();

        let tool = result
            .events
            .iter()
            .find(|e| e.role == Role::Tool)
            .expect("result event present in resumed batch");
        assert_eq!(tool.tool_call_id.as_deref(), Some("antigravity-5-0"));
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

    // === Phase 0 characterization: dynamic-workflow journal.jsonl ===

    fn workflow_fixture_root() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join("workflows")
            .join("claude")
    }

    #[test]
    fn baseline_workflow_journal_parses_to_zero_events_with_source_lines() {
        // BASELINE (to be inverted in Phase 1): journal.jsonl carries only
        // {type:"started"|"result"} ledger lines — no role events — but the parser
        // still emits source_lines for them. The shipper only skips when BOTH
        // events AND source_lines are empty, so today this ships as a 0-event
        // session that then pollutes the timeline.
        let journal = workflow_fixture_root()
            .join("11111111-2222-3333-4444-555555555555")
            .join("subagents")
            .join("workflows")
            .join("wf_testrun01")
            .join("journal.jsonl");
        assert!(
            journal.exists(),
            "fixture journal missing: {}",
            journal.display()
        );

        let result = parse_session_file(&journal, 0).unwrap();
        assert_eq!(result.events.len(), 0, "journal has no role events");
        assert!(
            !result.source_lines.is_empty(),
            "BASELINE: journal still produces source lines, so the shipper does not skip it"
        );
        // No timestamps in the ledger -> no started/ended bounds.
        assert!(result.metadata.started_at.is_none());
        assert!(!result.metadata.is_sidechain);
    }

    #[test]
    fn workflow_agent_transcript_resolves_to_parent_subagent() {
        // INVARIANT: agent-*.jsonl resolves to the parent via per-line
        // isSidechain + sessionId, regardless of phase.
        let agent = workflow_fixture_root()
            .join("11111111-2222-3333-4444-555555555555")
            .join("subagents")
            .join("workflows")
            .join("wf_testrun01")
            .join("agent-a049eaf15e4dbcae3.jsonl");
        assert!(
            agent.exists(),
            "fixture agent file missing: {}",
            agent.display()
        );

        let result = parse_session_file(&agent, 0).unwrap();
        assert!(result.metadata.is_sidechain);
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some("11111111-2222-3333-4444-555555555555")
        );
        assert_eq!(
            result.metadata.subagent_id.as_deref(),
            Some("a049eaf15e4dbcae3")
        );
        // Its own session id is path-derived, not the parent's.
        assert_ne!(
            result.metadata.session_id,
            "11111111-2222-3333-4444-555555555555"
        );
    }

    #[test]
    fn workflow_agent_carries_run_id_and_attribution() {
        // Phase 2 (P2): workflow_run_id comes from the path; attribution_agent /
        // attribution_skill come from the assistant lines.
        let agent = workflow_fixture_root()
            .join("11111111-2222-3333-4444-555555555555")
            .join("subagents")
            .join("workflows")
            .join("wf_testrun01")
            .join("agent-a049eaf15e4dbcae3.jsonl");
        let result = parse_session_file(&agent, 0).unwrap();
        assert_eq!(
            result.metadata.workflow_run_id.as_deref(),
            Some("wf_testrun01")
        );
        assert_eq!(
            result.metadata.attribution_agent.as_deref(),
            Some("workflow-subagent")
        );
        assert_eq!(
            result.metadata.attribution_skill.as_deref(),
            Some("deep-research")
        );
    }

    #[test]
    fn non_workflow_session_has_no_run_id() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "test-session.jsonl",
            &[
                r#"{"type":"user","uuid":"u1","timestamp":"2026-01-01T00:00:00Z","message":{"content":"hi"}}"#,
            ],
        );
        let result = parse_session_file(&path, 0).unwrap();
        assert!(result.metadata.workflow_run_id.is_none());
        assert!(result.metadata.attribution_agent.is_none());
    }
}
