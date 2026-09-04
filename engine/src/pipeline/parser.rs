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
use std::path::PathBuf;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use memmap2::Mmap;
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use serde_json::{json, Value};
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_uuid: Option<String>,
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

/// A provider-authored fact beside the transcript (turn duration, recap,
/// title). Emitted for lines the render surface does not show; never widens
/// `ParsedEvent`. `payload` is the provider's own fields, bounded per kind.
#[derive(Debug, Clone, Serialize)]
pub struct ParsedProviderFact {
    pub kind: String,
    pub at: DateTime<Utc>,
    pub source_offset: u64,
    pub payload: serde_json::Value,
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
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
    pub hatch_run_id: Option<String>,
    pub parent_longhouse_session_id: Option<String>,
    pub parent_thread_id: Option<String>,
    pub parent_provider_session_id: Option<String>,
    pub version: Option<String>,
    pub started_at: Option<DateTime<Utc>>,
    pub ended_at: Option<DateTime<Utc>>,
    pub is_sidechain: bool,
    /// True when a path binding exists that names *this* provider thread, so
    /// the managed session it points at genuinely owns this transcript. A
    /// binding a managed parent left on whatever file appeared next does not
    /// qualify. Set by the shipper after the binding is read, never by parsing.
    pub managed_binding_is_exact: bool,
}

impl SessionMetadata {
    /// A conversation someone forked, as opposed to a worker the model spawned.
    /// Codex marks both with a parent id; only the subagent is a sidechain.
    pub fn is_plain_fork(&self) -> bool {
        self.forked_from_session_id.is_some() && !self.is_sidechain
    }

    /// Whether this transcript belongs behind its parent rather than in the
    /// timeline. Provider subagents always do. A plain fork does only when no
    /// binding names this thread — that is, when Longhouse did not start it.
    pub fn is_hidden_child(&self) -> bool {
        self.is_sidechain || (self.is_plain_fork() && !self.managed_binding_is_exact)
    }

    /// Whether a managed session id supplied for this path may be trusted to
    /// name the session that owns this transcript.
    pub fn honors_managed_binding(&self) -> bool {
        !self.is_sidechain && (!self.is_plain_fork() || self.managed_binding_is_exact)
    }
}

pub struct ParseResult {
    pub events: Vec<ParsedEvent>,
    pub source_lines: Vec<ParsedSourceLine>,
    #[allow(dead_code)] // Phase 2 media store/upload consumes this parser side channel.
    pub media_objects: Vec<ParsedMediaObject>,
    /// Provider facts parsed from non-transcript lines (see `ParsedProviderFact`).
    pub provider_facts: Vec<ParsedProviderFact>,
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
struct RawOrigin {
    kind: Option<String>,
}

#[derive(Deserialize)]
struct RawLine {
    r#type: Option<String>,
    /// Cursor's current agent-transcript JSONL format uses a top-level role
    /// alongside the same `{message:{content:...}}` envelope as Claude.
    role: Option<String>,
    /// Antigravity transcript format.
    step_index: Option<u64>,
    source: Option<String>,
    created_at: Option<String>,
    timestamp: Option<String>,
    uuid: Option<String>,
    #[serde(rename = "parentUuid")]
    parent_uuid: Option<String>,
    /// Claude task notifications identify themselves outside the message body.
    // Keep this raw so an unrelated provider shape under `origin` cannot make
    // the whole line unparsable. The notification helper decodes only the
    // object form it owns.
    origin: Option<Box<RawValue>>,
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
    /// Claude user rows carry these flags for provider-authored metadata such
    /// as compaction summaries and local-command caveats.
    #[serde(rename = "isMeta")]
    is_meta: Option<bool>,
    #[serde(rename = "isCompactSummary")]
    is_compact_summary: Option<bool>,
    /// Workflow subagent attribution (assistant lines): agent kind + skill.
    #[serde(rename = "attributionAgent")]
    attribution_agent: Option<String>,
    #[serde(rename = "attributionSkill")]
    attribution_skill: Option<String>,
    /// Claude assistant lines: the effort the request ran at.
    effort: Option<String>,
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
    /// pi carries the role inside the envelope: `{type:"message",
    /// message:{role, content}}`. Claude encodes it as the line type and Cursor
    /// puts it at the top level, so this is the third of three placements.
    role: Option<String>,
    /// Claude assistant lines: the provider's own model/usage accounting.
    /// Kept raw; only turn-ending lines are turned into a usage fact.
    model: Option<String>,
    stop_reason: Option<String>,
    usage: Option<Box<RawValue>>,
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
    /// custom_tool_call: arbitrary JSON input (often a plain command string).
    input: Option<Box<RawValue>>,
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
    #[serde(rename = "messageId")]
    message_id: Option<serde_json::Value>,
    #[serde(rename = "sessionId")]
    session_id: Option<String>,
    timestamp: Option<String>,
    /// Common observed values: "user", "gemini", "info", "error"
    r#type: Option<String>,
    /// Content is normally a string but may be an object/array in newer Gemini
    /// CLI versions. Accept any JSON value and extract text defensively.
    content: Option<serde_json::Value>,
    /// Legacy Antigravity/Gemini `logs.json` calls this field `message`.
    message: Option<serde_json::Value>,
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
            provider_facts: Vec::new(),
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

    // A Task/Agent subagent names its own spawning tool call in a sidecar
    // beside the transcript. Nothing inside the transcript carries it, so
    // without this read the child can never be bound to the row that spawned
    // it. Workflow children have no sidecar; their binding runs through the
    // run id and the parent's tool result instead.
    if result.metadata.subagent_tool_use_id.is_none() {
        result.metadata.subagent_tool_use_id = subagent_tool_use_id_from_sidecar(path);
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

/// Read `toolUseId` from the `agent-<id>.meta.json` sidecar Claude writes beside
/// a Task/Agent subagent transcript.
///
/// Fail-closed: a missing, unreadable, oversized, or malformed sidecar yields
/// no binding, and the child renders as an unattached subagent rather than
/// being attached to a guess.
fn subagent_tool_use_id_from_sidecar(path: &Path) -> Option<String> {
    const MAX_SIDECAR_BYTES: u64 = 64 * 1024;

    let stem = path.file_stem().and_then(|value| value.to_str())?;
    if !stem.starts_with("agent-") {
        return None;
    }
    let sidecar = path.with_file_name(format!("{stem}.meta.json"));
    if std::fs::metadata(&sidecar).ok()?.len() > MAX_SIDECAR_BYTES {
        return None;
    }
    let raw = std::fs::read_to_string(&sidecar).ok()?;
    let parsed: serde_json::Value = serde_json::from_str(&raw).ok()?;
    let tool_use_id = parsed.get("toolUseId")?.as_str()?.trim();
    (!tool_use_id.is_empty()).then(|| tool_use_id.to_string())
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

    // Codex stamps `forked_from_id` on both a provider subagent and an ordinary
    // fork, and only the subagent carries `source.subAgent`. Reading
    // `forked_from_id` first therefore classified every fork as a hidden
    // sidechain — which is right for a worker the model spawned and wrong for a
    // conversation someone deliberately branched. The source is the
    // discriminator, so it has to be consulted first.
    if let Some(source) = payload
        .source
        .as_ref()
        .and_then(|source| parse_codex_subagent_source_str(source.get()))
    {
        return CodexPayloadParentage {
            forked_from_session_id: source
                .parent_thread_id
                .filter(|candidate| Uuid::parse_str(candidate).is_ok())
                .or(forked_from_session_id),
            is_sidechain: true,
        };
    }

    // A plain fork keeps its parent pointer and is not a sidechain. Whether it
    // is *visible* is decided later, by whether a binding names this thread:
    // Longhouse-initiated forks are sessions of their own, and a fork taken by
    // hand outside Longhouse stays hidden as it does today.
    CodexPayloadParentage {
        forked_from_session_id,
        is_sidechain: false,
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

fn empty_gemini_result(session_id: &str, file_size: u64) -> ParseResult {
    ParseResult {
        events: Vec::new(),
        source_lines: Vec::new(),
        media_objects: Vec::new(),
        provider_facts: Vec::new(),
        last_good_offset: file_size,
        candidate_records: 0,
        metadata: SessionMetadata {
            session_id: session_id.to_string(),
            ..Default::default()
        },
    }
}

fn parse_gemini_json(path: &Path, session_id: &str) -> Result<ParseResult> {
    let content = std::fs::read_to_string(path)
        .with_context(|| format!("Failed to read {}", path.display()))?;
    let file_size = content.len() as u64;
    let (source_lines, media_objects) = capture_text_source_lines(&content);

    let document: serde_json::Value = match serde_json::from_str(&content) {
        Ok(value) => value,
        Err(primary_error) => {
            if let Some(sanitized) = sanitize_invalid_surrogate_escapes(&content) {
                match serde_json::from_str(&sanitized) {
                    Ok(value) => {
                        tracing::debug!(
                            path = %path.display(),
                            error = %primary_error,
                            "Recovered Gemini JSON after surrogate-escape repair"
                        );
                        value
                    }
                    Err(repaired_error) => {
                        tracing::debug!(
                            path = %path.display(),
                            error = %primary_error,
                            repaired_error = %repaired_error,
                            "Failed to parse Gemini JSON (including repaired payload)"
                        );
                        return Ok(empty_gemini_result(session_id, file_size));
                    }
                }
            } else {
                tracing::debug!(path = %path.display(), error = %primary_error, "Failed to parse Gemini JSON");
                return Ok(empty_gemini_result(session_id, file_size));
            }
        }
    };

    let (document_session_id, document_start_time, messages) = if document.is_array() {
        let messages: Vec<GeminiMessage> = match serde_json::from_value(document) {
            Ok(messages) => messages,
            Err(error) => {
                tracing::debug!(path = %path.display(), error = %error, "Failed to parse Gemini logs array");
                return Ok(empty_gemini_result(session_id, file_size));
            }
        };
        (
            messages.iter().find_map(|message| message.session_id.clone()),
            None,
            messages,
        )
    } else {
        let session: GeminiSession = match serde_json::from_value(document) {
            Ok(session) => session,
            Err(error) => {
                tracing::debug!(path = %path.display(), error = %error, "Failed to parse Gemini session document");
                return Ok(empty_gemini_result(session_id, file_size));
            }
        };
        (session.session_id, session.start_time, session.messages.unwrap_or_default())
    };

    // Use the sessionId from the document if it's a valid UUID; otherwise keep stem-derived.
    let canonical_session_id = document_session_id
        .as_deref()
        .filter(|id| Uuid::parse_str(id).is_ok())
        .unwrap_or(session_id)
        .to_string();

    let mut events = Vec::new();
    let mut metadata = SessionMetadata {
        session_id: canonical_session_id.clone(),
        ..Default::default()
    };

    if let Some(start_time) = document_start_time.as_deref() {
        metadata.started_at = parse_timestamp(start_time);
    }
    let mut candidate_records = 0;

    for (message_index, msg) in messages.into_iter().enumerate() {
        let msg_type = msg.r#type.as_deref().unwrap_or("");
        let msg_id = if let Some(id) = msg
            .id
            .as_deref()
            .filter(|id| Uuid::parse_str(id).is_ok())
        {
            id.to_string()
        } else {
            let key = msg
                .message_id
                .as_ref()
                .and_then(|value| match value {
                    serde_json::Value::String(value) => (!value.is_empty()).then_some(value.clone()),
                    serde_json::Value::Number(value) => Some(value.to_string()),
                    _ => None,
                })
                .unwrap_or_else(|| message_index.to_string());
            Uuid::new_v5(
                &Uuid::NAMESPACE_URL,
                format!("gemini:{}:{}", canonical_session_id, key).as_bytes(),
            )
            .to_string()
        };

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
                let text = msg
                    .content
                    .as_ref()
                    .or(msg.message.as_ref())
                    .and_then(extract_gemini_text);
                if let Some(text) = text {
                    events.push(ParsedEvent {
                        uuid: msg_id.clone(),
                        parent_uuid: None,
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
                let text = msg
                    .content
                    .as_ref()
                    .or(msg.message.as_ref())
                    .and_then(extract_gemini_text);
                if let Some(text) = text {
                    events.push(ParsedEvent {
                        uuid: msg_id.clone(),
                        parent_uuid: None,
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
                        parent_uuid: None,
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
                            parent_uuid: None,
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
        provider_facts: Vec::new(),
        last_good_offset: file_size,
        candidate_records,
        metadata,
    })
}

// ---------------------------------------------------------------------------
// Git repo detection
// ---------------------------------------------------------------------------

/// Walk up from `cwd` to find the nearest Git directory. Worktrees store a
/// `.git` pointer file rather than a directory, so resolve that pointer to the
/// per-worktree Git directory before returning.
fn find_git_dir(cwd: &Path) -> Option<std::path::PathBuf> {
    let mut dir = cwd;
    loop {
        let candidate = dir.join(".git");
        if candidate.is_dir() {
            return Some(candidate);
        }
        if candidate.is_file() {
            let pointer = std::fs::read_to_string(&candidate).ok()?;
            let raw_git_dir = pointer.trim().strip_prefix("gitdir:")?.trim();
            let git_dir = Path::new(raw_git_dir);
            return Some(if git_dir.is_absolute() {
                git_dir.to_path_buf()
            } else {
                dir.join(git_dir)
            });
        }
        dir = dir.parent()?;
    }
}

/// Resolve the shared Git directory for a linked worktree. Its `commondir`
/// points back to the repository's canonical `.git` directory, which owns the
/// remote config and gives all worktrees the same repository identity.
fn find_common_git_dir(git_dir: &Path) -> std::path::PathBuf {
    let Ok(raw_common_dir) = std::fs::read_to_string(git_dir.join("commondir")) else {
        return git_dir.to_path_buf();
    };
    let common_dir = Path::new(raw_common_dir.trim());
    let resolved = if common_dir.is_absolute() {
        common_dir.to_path_buf()
    } else {
        git_dir.join(common_dir)
    };
    resolved.canonicalize().unwrap_or(resolved)
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
///
/// Providers that keep their working directory outside the transcript stream
/// (Cursor's sibling `meta.json`, Antigravity's conversation store) recover the
/// cwd from their own sidecar and then call this, so that every provider lands
/// on one project derivation instead of growing a parallel one.
pub fn resolve_git_info(cwd: &Path) -> (Option<String>, Option<String>) {
    let git_dir = match find_git_dir(cwd) {
        Some(d) => d,
        None => {
            // No git repo — fall back to cwd basename, but do not promote
            // generic temp workspace directories into report-level projects.
            let project = project_from_cwd_basename(cwd);
            return (project, None);
        }
    };

    let common_git_dir = find_common_git_dir(&git_dir);

    // git root = parent of the canonical/shared .git directory
    let git_root = common_git_dir.parent().unwrap_or(cwd);
    let project = git_root
        .file_name()
        .and_then(|s| s.to_str())
        .map(|s| s.to_string());

    // Read remote URL from .git/config
    let git_repo = read_git_remote_url(&common_git_dir.join("config"));

    (project, git_repo)
}

/// Directory basenames that name a container, not a project.
///
/// Providers and the provider factory run sessions inside generic scratch
/// directories (`.../workspace`, `/run/lhq/sandbox-home/c/w`). Promoting that
/// basename to a project name files unrelated sessions under one invented
/// project, so every attribution path refuses it rather than each path growing
/// its own rule. This is a heuristic over basenames — the durable fix is for
/// ephemeral run directories to carry no project at all.
pub fn is_generic_workspace_label(label: &str) -> bool {
    matches!(label.trim(), "" | "workspace" | "ws" | "w")
}

fn project_from_cwd_basename(cwd: &Path) -> Option<String> {
    // A session started in the home directory is not a session about a project
    // called after the user. Antigravity resolves eight of its sessions to
    // `$HOME`, and "davidrose" is not a project.
    if std::env::var_os("HOME")
        .map(PathBuf::from)
        .is_some_and(|home| home == cwd)
    {
        return None;
    }
    let label = cwd.file_name().and_then(|s| s.to_str())?.trim();
    if is_generic_workspace_label(label) {
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
            provider_facts: Vec::new(),
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
    let mut provider_facts: Vec<ParsedProviderFact> = Vec::new();
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
    let mut codex_facts = codex_fact_state_for(path, offset);

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
        extract_provider_facts(
            &obj,
            trimmed,
            line_offset,
            &mut provider_facts,
            &mut codex_facts,
        );
    }

    // Finalize metadata
    metadata.started_at = min_ts;
    metadata.ended_at = max_ts;
    if metadata.session_id.is_empty() {
        metadata.session_id = session_id.to_string();
    }
    finalize_workspace_metadata(&mut metadata, path);

    Ok(ParseResult {
        events,
        source_lines,
        media_objects,
        provider_facts,
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
    let mut provider_facts: Vec<ParsedProviderFact> = Vec::new();
    let mut metadata = SessionMetadata::default();
    let mut min_ts: Option<DateTime<Utc>> = None;
    let mut max_ts: Option<DateTime<Utc>> = None;
    let mut current_offset = offset;
    let mut candidate_lines: usize = 0;
    // See parse_mmap: seed antigravity call/result pairing across the resume boundary.
    let mut antigravity_pending = seed_antigravity_pending(path, offset);
    let mut codex_pending = CodexPending::default();
    let mut codex_facts = codex_fact_state_for(path, offset);
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
        extract_provider_facts(
            &obj,
            trimmed.as_bytes(),
            line_offset,
            &mut provider_facts,
            &mut codex_facts,
        );
    }

    metadata.started_at = min_ts;
    metadata.ended_at = max_ts;
    if metadata.session_id.is_empty() {
        metadata.session_id = session_id.to_string();
    }
    finalize_workspace_metadata(&mut metadata, path);

    Ok(ParseResult {
        events,
        source_lines,
        media_objects,
        provider_facts,
        last_good_offset: current_offset,
        candidate_records: candidate_lines,
        metadata,
    })
}

// ---------------------------------------------------------------------------
// Shared extraction logic
// ---------------------------------------------------------------------------

/// Resolve `project` and `git_repo` once every record has been read.
///
/// Providers that never write a working directory into the transcript get one
/// last chance here, from their own sidecars, before the session is filed with
/// no project at all. Recovery feeds `metadata.cwd` and then goes through the
/// same `resolve_git_info` every other provider uses, so there is one project
/// derivation rather than one per provider.
fn finalize_workspace_metadata(metadata: &mut SessionMetadata, path: &Path) {
    if metadata.cwd.is_none() {
        if let Some(conversation_id) = antigravity_session_id_from_path(path) {
            if let Some(workspace) =
                crate::antigravity_workspace::antigravity_workspace(path, &conversation_id)
            {
                metadata.cwd = Some(workspace.cwd);
                if metadata.git_repo.is_none() {
                    metadata.git_repo = workspace.git_repo;
                }
            }
        }
    }

    let Some(ref cwd) = metadata.cwd else {
        return;
    };
    let (project, git_repo) = resolve_git_info(Path::new(cwd));
    metadata.project = project;
    // Only use disk-resolved git_repo if the transcript or a sidecar already
    // provided one (e.g. Codex carries it in session_meta).
    if metadata.git_repo.is_none() {
        metadata.git_repo = git_repo;
    }
}

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

/// Provider text bounded by characters, never splitting a UTF-8 scalar.
fn bounded_text(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

fn claude_task_tag_value(body: &str, tag: &str) -> Option<String> {
    let open = format!("<{tag}>");
    let close = format!("</{tag}>");
    let start = body.find(&open)? + open.len();
    let end = body[start..].find(&close)? + start;
    let value = body[start..end].split_whitespace().collect::<Vec<_>>().join(" ");
    (!value.is_empty()).then_some(bounded_text(&value, 2_000))
}

fn claude_task_notification_summary(content: &str) -> Option<String> {
    let trimmed = content.trim();
    let body_and_tail = trimmed.strip_prefix("<task-notification>")?;
    let close_tag = "</task-notification>";
    let close = body_and_tail.find(close_tag)?;
    let body = body_and_tail[..close].trim();
    // Claude can append one or more provider-authored system reminders to a
    // task envelope. Keep this allowlist narrow: arbitrary trailing prose is
    // not enough to promote pasted XML into a provider notification.
    let mut tail = body_and_tail[close + close_tag.len()..].trim();
    while !tail.is_empty() {
        let reminder = tail.strip_prefix("<system-reminder>")?;
        let reminder_close = "</system-reminder>";
        let end = reminder.find(reminder_close)?;
        tail = reminder[end + reminder_close.len()..].trim();
    }
    if let Some(summary) = claude_task_tag_value(body, "summary") {
        return Some(summary);
    }
    if let Some(status) = claude_task_tag_value(body, "status") {
        return Some(bounded_text(&format!("Background task {status}"), 2_000));
    }
    Some("Background task update".to_string())
}

fn claude_task_notification_display(obj: &RawLine) -> Option<String> {
    let origin = obj
        .origin
        .as_ref()
        .and_then(|raw| serde_json::from_str::<RawOrigin>(raw.get()).ok());
    if origin.as_ref()?.kind.as_deref() != Some("task-notification") {
        return None;
    }
    if !matches!(obj.r#type.as_deref(), None | Some("user") | Some("message")) {
        return None;
    }
    let message = obj.message.as_ref()?;
    if message.role.as_deref().is_some_and(|role| role != "user") {
        return None;
    }
    let content = extract_text_from_raw_content(message.content.get())?;
    claude_task_notification_summary(&content)
}

fn claude_native_meta_record(obj: &RawLine) -> bool {
    obj.is_meta == Some(true)
        && matches!(obj.r#type.as_deref(), None | Some("user"))
        && obj
            .message
            .as_ref()
            .and_then(|message| message.role.as_deref())
            .is_none_or(|role| role == "user")
}

fn claude_command_record_text(content: &str) -> bool {
    let mut remainder = content.trim();
    let Some(after_open) = remainder.strip_prefix("<command-name>") else {
        return false;
    };
    let Some(command_end) = after_open.find("</command-name>") else {
        return false;
    };
    remainder = &after_open[command_end + "</command-name>".len()..];
    for (open, close) in [
        ("<command-message>", "</command-message>"),
        ("<command-args>", "</command-args>"),
    ] {
        let candidate = remainder.trim_start();
        if !candidate.starts_with(open) {
            continue;
        }
        let Some(body) = candidate.strip_prefix(open) else {
            return false;
        };
        let Some(end) = body.find(close) else {
            return false;
        };
        remainder = &body[end + close.len()..];
    }
    remainder.trim().is_empty()
}

fn claude_native_local_command_meta_record(obj: &RawLine) -> bool {
    if !claude_native_meta_record(obj) {
        return false;
    }
    let Some(content) = obj
        .message
        .as_ref()
        .and_then(|message| extract_text_from_raw_content(message.content.get()))
    else {
        return false;
    };
    let content = content.trim();
    if claude_command_record_text(content) {
        return true;
    }
    if content.starts_with("<local-command-stdout>") && content.ends_with("</local-command-stdout>") {
        return true;
    }
    let Some(caveat_end) = content.find("</local-command-caveat>") else {
        return false;
    };
    let tail = content[caveat_end + "</local-command-caveat>".len()..].trim();
    content.starts_with("<local-command-caveat>")
        && (tail.is_empty() || tail.starts_with("<command-name>"))
}

fn claude_provider_system_record(obj: &RawLine) -> bool {
    if claude_task_notification_display(obj).is_some() {
        return false;
    }
    let origin = obj
        .origin
        .as_ref()
        .and_then(|raw| serde_json::from_str::<RawOrigin>(raw.get()).ok());
    if origin.as_ref().and_then(|value| value.kind.as_deref()) == Some("channel") {
        return false;
    }
    obj.is_compact_summary == Some(true)
        || (claude_native_meta_record(obj) && !claude_native_local_command_meta_record(obj))
}

/// Provider facts live on lines the transcript surface never renders. Match
/// on the cheap discriminators first; only a matched line is re-parsed as a
/// JSON tree, so the hot path pays nothing for ordinary user/assistant rows.
fn extract_provider_facts(
    obj: &RawLine,
    trimmed: &[u8],
    line_offset: u64,
    facts: &mut Vec<ParsedProviderFact>,
    codex: &mut CodexFactState,
) {
    // Codex rollout lines carry their discriminator under `payload.type`;
    // these three top-level types are Codex's alone.
    if matches!(
        obj.r#type.as_deref(),
        Some("event_msg") | Some("turn_context") | Some("compacted")
    ) {
        extract_codex_provider_facts(obj, trimmed, line_offset, facts, codex);
        return;
    }
    let kind = match (obj.r#type.as_deref(), obj.subtype.as_deref()) {
        (Some("system"), Some("turn_duration")) => "turn.duration",
        (Some("system"), Some("away_summary")) => "session.recap",
        (Some("system"), Some("api_error")) => "turn.api_error",
        (Some("system"), Some("compact_boundary")) => "context.compaction",
        (Some("ai-title"), _) => "session.title",
        // One usage fact per turn, on the assistant line that ends it. Every
        // assistant line carries usage; the turn-ending one is the context
        // size the next prompt will start from.
        (Some("assistant"), _)
            if obj
                .message
                .as_ref()
                .is_some_and(|m| m.usage.is_some() && m.stop_reason.as_deref() == Some("end_turn")) =>
        {
            "turn.usage"
        }
        _ => return,
    };
    // Assistant lines are the hot path; their fact comes from the fields serde
    // already split off, never from re-parsing the (often large) line.
    let value = if kind == "turn.usage" {
        serde_json::Value::Null
    } else {
        match serde_json::from_slice::<serde_json::Value>(trimmed) {
            Ok(value) => value,
            Err(_) => return,
        }
    };
    // `ai-title` lines carry no timestamp of their own; the shipper orders
    // facts by source position anyway, and the title has no clock semantics.
    let at = match obj.timestamp.as_deref().and_then(parse_timestamp) {
        Some(at) => at,
        None if kind == "session.title" => DateTime::<Utc>::UNIX_EPOCH,
        None => return,
    };
    let payload = match kind {
        "turn.duration" => {
            let Some(duration_ms) = value.get("durationMs").and_then(|v| v.as_u64()) else {
                return;
            };
            let mut payload = serde_json::Map::new();
            payload.insert(
                "duration_ms".to_string(),
                serde_json::Value::from(duration_ms),
            );
            if let Some(count) = value.get("messageCount").and_then(|v| v.as_u64()) {
                payload.insert("message_count".to_string(), serde_json::Value::from(count));
            }
            for (wire, ours) in [
                ("pendingBackgroundAgentCount", "pending_background_agents"),
                ("pendingWorkflowCount", "pending_workflows"),
            ] {
                if let Some(count) = value.get(wire).and_then(|v| v.as_u64()) {
                    payload.insert(ours.to_string(), serde_json::Value::from(count));
                }
            }
            serde_json::Value::Object(payload)
        }
        "session.recap" => {
            // "(disable recaps in /config)" is terminal chrome, not the recap.
            let Some(text) = value
                .get("content")
                .and_then(|v| v.as_str())
                .map(|t| t.trim_end_matches("(disable recaps in /config)").trim())
                .filter(|t| !t.is_empty())
            else {
                return;
            };
            serde_json::json!({ "text": bounded_text(text, 2_000) })
        }
        "turn.usage" => {
            let Some(message) = obj.message.as_ref() else { return };
            let Some(usage) = message
                .usage
                .as_deref()
                .and_then(|raw| serde_json::from_str::<serde_json::Value>(raw.get()).ok())
            else {
                return;
            };
            let mut payload = serde_json::Map::new();
            if let Some(model) = message.model.as_deref() {
                payload.insert("model".to_string(), serde_json::Value::from(model));
            }
            if let Some(effort) = obj.effort.as_deref() {
                payload.insert("effort".to_string(), serde_json::Value::from(effort));
            }
            for key in [
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            ] {
                if let Some(count) = usage.get(key).and_then(|v| v.as_u64()) {
                    payload.insert(key.to_string(), serde_json::Value::from(count));
                }
            }
            if let Some(thinking) = usage
                .get("output_tokens_details")
                .and_then(|d| d.get("thinking_tokens"))
                .and_then(|v| v.as_u64())
            {
                payload.insert("thinking_tokens".to_string(), serde_json::Value::from(thinking));
            }
            for key in ["service_tier", "speed"] {
                if let Some(text) = usage.get(key).and_then(|v| v.as_str()) {
                    payload.insert(key.to_string(), serde_json::Value::from(text));
                }
            }
            if !payload.contains_key("output_tokens") {
                return;
            }
            // Claude's context is everything it read on this call: fresh
            // input plus both cache classes. Named here so every provider's
            // usage fact carries the same key for the same meaning.
            let context_tokens: u64 = [
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ]
            .iter()
            .filter_map(|key| payload.get(*key).and_then(|v| v.as_u64()))
            .sum();
            payload.insert(
                "context_tokens".to_string(),
                serde_json::Value::from(context_tokens),
            );
            serde_json::Value::Object(payload)
        }
        "turn.api_error" => {
            let mut payload = serde_json::Map::new();
            if let Some(error) = value.get("error").and_then(|v| v.as_str()) {
                payload.insert("error".to_string(), serde_json::Value::from(bounded_text(error, 500)));
            }
            for (wire, ours) in [
                ("retryAttempt", "retry_attempt"),
                ("maxRetries", "max_retries"),
                ("retryInMs", "retry_in_ms"),
            ] {
                if let Some(count) = value.get(wire).and_then(|v| v.as_u64()) {
                    payload.insert(ours.to_string(), serde_json::Value::from(count));
                }
            }
            if payload.is_empty() {
                return;
            }
            serde_json::Value::Object(payload)
        }
        "context.compaction" => {
            let Some(meta) = value.get("compactMetadata").and_then(|v| v.as_object()) else {
                return;
            };
            let mut payload = serde_json::Map::new();
            if let Some(trigger) = meta.get("trigger").and_then(|v| v.as_str()) {
                payload.insert("trigger".to_string(), serde_json::Value::from(trigger));
            }
            for (wire, ours) in [
                ("preTokens", "pre_tokens"),
                ("postTokens", "post_tokens"),
                ("durationMs", "duration_ms"),
            ] {
                if let Some(count) = meta.get(wire).and_then(|v| v.as_u64()) {
                    payload.insert(ours.to_string(), serde_json::Value::from(count));
                }
            }
            if payload.is_empty() {
                return;
            }
            serde_json::Value::Object(payload)
        }
        "session.title" => {
            let Some(title) = value
                .get("aiTitle")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|t| !t.is_empty())
            else {
                return;
            };
            serde_json::json!({ "title": bounded_text(title, 255) })
        }
        _ => return,
    };
    facts.push(ParsedProviderFact {
        kind: kind.to_string(),
        at,
        source_offset: line_offset,
        payload,
    });
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
        if obj.role.is_some() {
            // Cursor agent transcripts do not emit a UUID per record. The
            // byte offset is stable across incremental parses and therefore
            // keeps render event identities deterministic.
            format!("cursor-line-{line_offset}")
        } else {
            Uuid::new_v4().to_string()
        }
    } else {
        msg_uuid
    };

    if obj.role.is_some() {
        extract_cursor_events(
            obj,
            session_id,
            &msg_uuid,
            timestamp,
            line_offset,
            raw_line,
            events,
        );
        return;
    }

    // Claude's background-task completion envelope is a provider-authored
    // status row, not a user prompt. Keep the source line archived verbatim,
    // but project the row as a compact system notification for clients.
    if let Some(display_text) = claude_task_notification_display(obj) {
        events.push(ParsedEvent {
            uuid: msg_uuid,
            parent_uuid: obj.parent_uuid.clone(),
            session_id: session_id.to_string(),
            timestamp,
            role: Role::System,
            content_text: Some(display_text),
            tool_name: None,
            tool_input_json: None,
            tool_output_text: None,
            tool_call_id: None,
            source_offset: line_offset,
            raw_type: "claude_task_notification".to_string(),
            raw_line: Some(raw_line.to_string()),
        });
        return;
    }

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

    // pi types every record `message` and carries the role inside the envelope,
    // so neither the Claude nor the Cursor placement finds it. Without this the
    // lines match no branch at all: a completed pi Console turn wrote and bound
    // a transcript and served a session with zero events.
    let effective_role = match event_type {
        "message" => obj
            .message
            .as_ref()
            .and_then(|m| m.role.as_deref())
            .unwrap_or(""),
        other => other,
    };

    // Claude's compact summaries and other `isMeta` user rows are provider
    // context, not user prompts. Preserve their raw source line while keeping
    // them out of the conversational timeline. Local-command caveats are
    // intentionally excluded here and continue through semantic classification
    // because their sibling command/output rows establish the control record.
    if effective_role == "user" && claude_provider_system_record(obj) {
        if let Some(content) = extract_text_from_raw_content(content_str) {
            if !content.trim().is_empty() {
                events.push(ParsedEvent {
                    uuid: msg_uuid,
                    parent_uuid: obj.parent_uuid.clone(),
                    session_id: session_id.to_string(),
                    timestamp,
                    role: Role::System,
                    content_text: Some(content),
                    tool_name: None,
                    tool_input_json: None,
                    tool_output_text: None,
                    tool_call_id: None,
                    source_offset: line_offset,
                    raw_type: if obj.is_compact_summary == Some(true) {
                        "claude_compact_summary".to_string()
                    } else {
                        "claude_meta".to_string()
                    },
                    raw_line: Some(raw_line.to_string()),
                });
            }
        }
        return;
    }

    match effective_role {
        "user" => {
            let event_start = events.len();
            extract_user_events(
                content_str,
                session_id,
                &msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
            );
            for event in &mut events[event_start..] {
                event.parent_uuid = obj.parent_uuid.clone();
            }
        }
        "assistant" => {
            let event_start = events.len();
            extract_assistant_events(
                content_str,
                session_id,
                &msg_uuid,
                timestamp,
                line_offset,
                raw_line,
                events,
            );
            for event in &mut events[event_start..] {
                event.parent_uuid = obj.parent_uuid.clone();
            }
        }
        _ => {
            // Unknown type — skip
        }
    }
}

const CURSOR_INJECTION_TAGS: [(&str, &str); 6] = [
    ("<user_info>", "</user_info>"),
    ("<agent_transcripts>", "</agent_transcripts>"),
    ("<rules>", "</rules>"),
    ("<system_reminder>", "</system_reminder>"),
    ("<attached_files>", "</attached_files>"),
    ("<system_notification>", "</system_notification>"),
];

fn cursor_query_is_inside_injection(text: &str, query_start: usize) -> bool {
    CURSOR_INJECTION_TAGS.iter().any(|(open, close)| {
        let Some(open_start) = text[..query_start].rfind(open) else {
            return false;
        };
        text[open_start + open.len()..]
            .find(close)
            .is_some_and(|close_offset| open_start + open.len() + close_offset >= query_start)
    })
}

fn cursor_user_query_text(text: &str) -> Option<String> {
    let trimmed = text.trim();
    let mut search_from = 0;
    while let Some(relative_start) = trimmed[search_from..].find("<user_query>") {
        let start = search_from + relative_start;
        search_from = start + "<user_query>".len();
        if cursor_query_is_inside_injection(trimmed, start) {
            continue;
        }
        let prefix = trimmed[..start].trim();
        if !prefix.is_empty()
            && !((prefix.starts_with("<timestamp>") && prefix.ends_with("</timestamp>"))
                || CURSOR_INJECTION_TAGS
                    .iter()
                    .any(|(marker, _)| prefix.starts_with(marker)))
        {
            continue;
        }
        let Some(end) = trimmed.rfind("</user_query>") else {
            continue;
        };
        if end <= start || !trimmed[end + "</user_query>".len()..].trim().is_empty() {
            continue;
        }
        return Some(trimmed[start + "<user_query>".len()..end].trim().to_string());
    }
    None
}

fn cursor_has_injection_marker(text: &str) -> bool {
    let lines: Vec<&str> = text.lines().collect();
    lines.iter().enumerate().any(|(index, line)| {
        let line = line.trim_start();
        CURSOR_INJECTION_TAGS.iter().any(|(marker, close)| {
            line.starts_with(marker)
                && (line[marker.len()..].contains(close)
                    || lines[index + 1..]
                        .iter()
                        .any(|next| CURSOR_INJECTION_TAGS.iter().any(|(other, _)| next.trim_start().starts_with(other))))
        })
    })
}

pub(crate) fn cursor_user_text(text: &str) -> (String, Role) {
    if let Some(body) = cursor_user_query_text(text) {
        return (body, Role::User);
    }
    if cursor_has_injection_marker(text) {
        return (text.to_string(), Role::System);
    }
    (text.to_string(), Role::User)
}

fn cursor_value_text(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Array(items) => {
            let parts = items
                .iter()
                .filter_map(|item| {
                    item.get("text")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                        .or_else(|| item.get("content").and_then(cursor_value_text))
                })
                .collect::<Vec<_>>();
            (!parts.is_empty()).then(|| parts.join(""))
        }
        Value::Object(object) => object
            .get("text")
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| object.get("content").and_then(cursor_value_text))
            .or_else(|| object.get("result").and_then(cursor_value_text)),
        _ => None,
    }
}

fn cursor_event(
    uuid: String,
    session_id: &str,
    timestamp: DateTime<Utc>,
    role: Role,
    content_text: Option<String>,
    tool_name: Option<String>,
    tool_input_json: Option<Box<RawValue>>,
    tool_output_text: Option<String>,
    tool_call_id: Option<String>,
    line_offset: u64,
    raw_type: &str,
    raw_line: &str,
) -> ParsedEvent {
    ParsedEvent {
        uuid,
        parent_uuid: None,
        session_id: session_id.to_string(),
        timestamp,
        role,
        content_text,
        tool_name,
        tool_input_json,
        tool_output_text,
        tool_call_id,
        source_offset: line_offset,
        raw_type: raw_type.to_string(),
        raw_line: Some(raw_line.to_string()),
    }
}

fn extract_cursor_events(
    obj: &RawLine,
    session_id: &str,
    msg_uuid: &str,
    timestamp: DateTime<Utc>,
    line_offset: u64,
    raw_line: &str,
    events: &mut Vec<ParsedEvent>,
) {
    let Some(message) = obj.message.as_ref() else {
        return;
    };
    let Ok(content) = serde_json::from_str::<Value>(message.content.get()) else {
        return;
    };
    let blocks = match content.clone() {
        Value::Array(items) => items,
        Value::String(text) => vec![json!({"type":"text", "text":text})],
        other => vec![json!({"type":"text", "text":other.to_string()})],
    };
    let source_role = obj.role.as_deref().unwrap_or("assistant");
    let mut emitted = false;
    for (index, block) in blocks.iter().enumerate() {
        let kind = block.get("type").and_then(Value::as_str).unwrap_or("text");
        let event_uuid = format!("{msg_uuid}-cursor-{index}");
        match kind {
            "text" | "reasoning" | "redacted-reasoning" => {
                let Some(text) = block.get("text").and_then(Value::as_str) else {
                    continue;
                };
                if text.trim().is_empty() {
                    continue;
                }
                let (text, role) = if source_role == "user" {
                    cursor_user_text(text)
                } else {
                    (
                        text.to_string(),
                        match source_role {
                            "tool" => Role::Tool,
                            "system" => Role::System,
                            _ => Role::Assistant,
                        },
                    )
                };
                events.push(cursor_event(
                    event_uuid,
                    session_id,
                    timestamp,
                    role,
                    Some(text),
                    None,
                    None,
                    None,
                    None,
                    line_offset,
                    "cursor_text",
                    raw_line,
                ));
                emitted = true;
            }
            "tool-call" | "tool_call" | "tool-use" | "tool_use" => {
                let tool_name = block
                    .get("toolName")
                    .or_else(|| block.get("name"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let tool_call_id = block
                    .get("toolCallId")
                    .or_else(|| block.get("tool_call_id"))
                    .or_else(|| block.get("id"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let input = block
                    .get("args")
                    .or_else(|| block.get("input"))
                    .or_else(|| block.get("arguments"))
                    .and_then(|value| RawValue::from_string(value.to_string()).ok());
                events.push(cursor_event(
                    event_uuid,
                    session_id,
                    timestamp,
                    Role::Assistant,
                    None,
                    tool_name,
                    input,
                    None,
                    tool_call_id,
                    line_offset,
                    "cursor_tool_call",
                    raw_line,
                ));
                emitted = true;
            }
            "tool-result" | "tool_result" => {
                let tool_call_id = block
                    .get("toolCallId")
                    .or_else(|| block.get("tool_call_id"))
                    .or_else(|| block.get("id"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let tool_name = block
                    .get("toolName")
                    .or_else(|| block.get("name"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                let output = block
                    .get("result")
                    .or_else(|| block.get("content"))
                    .and_then(cursor_value_text)
                    .or_else(|| block.get("output").and_then(cursor_value_text));
                events.push(cursor_event(
                    event_uuid,
                    session_id,
                    timestamp,
                    Role::Tool,
                    None,
                    tool_name,
                    None,
                    output.or_else(|| Some(EMPTY_TOOL_RESULT_PLACEHOLDER.to_string())),
                    tool_call_id,
                    line_offset,
                    "cursor_tool_result",
                    raw_line,
                ));
                emitted = true;
            }
            _ => {}
        }
    }
    if !emitted && source_role == "user" {
        if let Some(text) = cursor_value_text(&content) {
            let (text, role) = cursor_user_text(&text);
            if !text.trim().is_empty() {
                events.push(cursor_event(
                    format!("{msg_uuid}-cursor-fallback"),
                    session_id,
                    timestamp,
                    role,
                    Some(text),
                    None,
                    None,
                    None,
                    None,
                    line_offset,
                    "cursor_text",
                    raw_line,
                ));
            }
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
    let trimmed = content.trim();
    if trimmed.starts_with(start_tag) && trimmed.ends_with(end_tag) {
        let body = trimmed[start_tag.len()..trimmed.len() - end_tag.len()].trim();
        if !body.is_empty() {
            return body.to_string();
        }
    }
    // A tag-looking substring can be quoted in an ordinary request. Without
    // an exact outer envelope, preserve the provider text and let the UI show
    // the evidence rather than silently deleting surrounding prose.
    trimmed.to_string()
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
                    parent_uuid: None,
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
                parent_uuid: None,
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
            parent_uuid: None,
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
                parent_uuid: None,
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
                parent_uuid: None,
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
                parent_uuid: None,
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
const CODEX_INTERNAL_CONTEXT_PREFIX: &str = "<codex_internal_context";
const CODEX_TURN_INTERRUPTED_TEXT: &str = "User interrupted the turn";
const CODEX_TURN_INTERRUPTED_RAW_TYPE: &str = "codex_turn_interrupted";
const CODEX_TURN_INTERRUPTED_MARKER_RAW_TYPE: &str = "codex_turn_interrupted_marker";

#[derive(Debug, Default)]
struct CodexPending {
    suppress_next_turn_aborted_marker: bool,
}

fn codex_text_is_internal_context(text: &str) -> bool {
    let trimmed = text.trim_start();
    let Some(opening_end) = trimmed.find('>') else {
        return false;
    };
    if !trimmed[..=opening_end].starts_with(CODEX_INTERNAL_CONTEXT_PREFIX) {
        return false;
    }
    if !trimmed[CODEX_INTERNAL_CONTEXT_PREFIX.len()..]
        .chars()
        .next()
        .is_some_and(|character| character.is_ascii_whitespace() || character == '>')
    {
        return false;
    }
    let closing = "</codex_internal_context>";
    trimmed.ends_with(closing) && trimmed.len() > opening_end + 1 + closing.len()
}

fn codex_text_is_agents_instructions(text: &str) -> bool {
    let trimmed = text.trim();
    trimmed.starts_with("# AGENTS.md instructions")
        && trimmed.contains("<INSTRUCTIONS>")
        && trimmed.ends_with("</INSTRUCTIONS>")
}

fn codex_text_is_provider_system(text: &str) -> bool {
    if codex_text_is_internal_context(text) || codex_text_is_agents_instructions(text) {
        return true;
    }
    let trimmed = text.trim();
    [
        ("<environment_context>", "</environment_context>"),
        ("<permissions instructions>", "</permissions instructions>"),
        ("<collaboration_mode>", "</collaboration_mode>"),
        (CODEX_TURN_ABORTED_PREFIX, "</turn_aborted>"),
    ]
    .iter()
    .any(|(opening, closing)| {
        trimmed.starts_with(opening)
            && trimmed.ends_with(closing)
            && trimmed.len() > opening.len() + closing.len()
    })
}

/// Codex spreads one turn's accounting over several lines: `turn_context`
/// names the model and effort at the start, `token_count` repeats the usage
/// after every model call, and `task_complete` (or `turn_aborted`) closes the
/// turn with its duration. The facts are one per turn, stamped on the closing
/// line, so the parser carries the latest settings and usage forward.
#[derive(Default)]
struct CodexFactState {
    /// The turn the carried usage belongs to. A new turn starts with no
    /// usage of its own: a turn that closes before any model call reports
    /// tokens yields no usage fact rather than the previous turn's counts
    /// under this turn's model.
    turn_id: Option<String>,
    model: Option<String>,
    effort: Option<String>,
    /// `info.last_token_usage` from the most recent `token_count` line.
    last_usage: Option<serde_json::Map<String, serde_json::Value>>,
    context_window: Option<u64>,
}

impl CodexFactState {
    fn begin_turn(&mut self, turn_id: Option<&str>) {
        let turn_id = turn_id.map(str::to_string);
        if turn_id.is_some() && turn_id == self.turn_id {
            return;
        }
        self.turn_id = turn_id;
        self.last_usage = None;
    }

    fn note_task_started(&mut self, payload: &serde_json::Map<String, serde_json::Value>) {
        self.begin_turn(payload.get("turn_id").and_then(|v| v.as_str()));
        if let Some(window) = payload.get("model_context_window").and_then(|v| v.as_u64()) {
            self.context_window = Some(window);
        }
    }

    fn note_turn_context(&mut self, payload: &serde_json::Map<String, serde_json::Value>) {
        self.begin_turn(payload.get("turn_id").and_then(|v| v.as_str()));
        if let Some(model) = payload.get("model").and_then(|v| v.as_str()) {
            self.model = Some(model.to_string());
        }
        let effort = payload.get("effort").and_then(|v| v.as_str()).or_else(|| {
            payload
                .get("collaboration_mode")
                .and_then(|m| m.get("settings"))
                .and_then(|s| s.get("reasoning_effort"))
                .and_then(|v| v.as_str())
        });
        if let Some(effort) = effort {
            self.effort = Some(effort.to_string());
        }
    }

    fn note_token_count(&mut self, payload: &serde_json::Map<String, serde_json::Value>) {
        let Some(info) = payload.get("info").and_then(|v| v.as_object()) else {
            return;
        };
        if let Some(last) = info.get("last_token_usage").and_then(|v| v.as_object()) {
            self.last_usage = Some(last.clone());
        }
        if let Some(window) = info.get("model_context_window").and_then(|v| v.as_u64()) {
            self.context_window = Some(window);
        }
    }

    /// The context size Codex itself reports is `last_token_usage.total_tokens`
    /// (`TokenUsage::tokens_in_context_window` in the protocol crate), so the
    /// payload names it outright instead of leaving the server to sum input
    /// classes that overlap in OpenAI's accounting.
    fn usage_payload(&self) -> Option<serde_json::Value> {
        let last = self.last_usage.as_ref()?;
        let mut payload = serde_json::Map::new();
        if let Some(model) = self.model.as_deref() {
            payload.insert("model".to_string(), serde_json::Value::from(model));
        }
        if let Some(effort) = self.effort.as_deref() {
            payload.insert("effort".to_string(), serde_json::Value::from(effort));
        }
        for (wire, ours) in [
            ("input_tokens", "input_tokens"),
            ("cached_input_tokens", "cache_read_input_tokens"),
            ("cache_write_input_tokens", "cache_creation_input_tokens"),
            ("output_tokens", "output_tokens"),
            ("reasoning_output_tokens", "thinking_tokens"),
            ("total_tokens", "context_tokens"),
        ] {
            if let Some(count) = last.get(wire).and_then(|v| v.as_u64()) {
                payload.insert(ours.to_string(), serde_json::Value::from(count));
            }
        }
        if let Some(window) = self.context_window {
            payload.insert("context_window".to_string(), serde_json::Value::from(window));
        }
        if !payload.contains_key("output_tokens") {
            return None;
        }
        Some(serde_json::Value::Object(payload))
    }
}

/// On an incremental parse the turn's `turn_context` and `token_count` lines
/// may sit before `offset`; replay the ones in a bounded window ending there so
/// the closing line still yields its usage fact.
fn seed_codex_fact_state(path: &Path, offset: u64) -> CodexFactState {
    let mut state = CodexFactState::default();
    if offset == 0 {
        return state;
    }
    const SEED_WINDOW_BYTES: u64 = 1024 * 1024;
    let window = SEED_WINDOW_BYTES.min(offset);
    let start = offset - window;
    let Ok(mut file) = std::fs::File::open(path) else {
        return state;
    };
    use std::io::{Read, Seek};
    if file.seek(std::io::SeekFrom::Start(start)).is_err() {
        return state;
    }
    let mut buf = vec![0u8; window as usize];
    if file.read_exact(&mut buf).is_err() {
        return state;
    }
    let search: &[u8] = if start > 0 {
        match buf.iter().position(|&b| b == b'\n') {
            Some(nl) => &buf[nl + 1..],
            None => return state,
        }
    } else {
        &buf[..]
    };
    for line in search.split(|&b| b == b'\n') {
        let line = trim_bytes(line);
        if line.is_empty() {
            continue;
        }
        if !contains_bytes(line, b"\"turn_context\"")
            && !contains_bytes(line, b"\"token_count\"")
            && !contains_bytes(line, b"\"task_started\"")
        {
            continue;
        }
        let Ok(value) = serde_json::from_slice::<serde_json::Value>(line) else {
            continue;
        };
        let Some(payload) = value.get("payload").and_then(|v| v.as_object()) else {
            continue;
        };
        let payload_type = payload.get("type").and_then(|v| v.as_str());
        match value.get("type").and_then(|v| v.as_str()) {
            Some("turn_context") => state.note_turn_context(payload),
            Some("event_msg") if payload_type == Some("task_started") => state.note_task_started(payload),
            Some("event_msg") if payload_type == Some("token_count") => state.note_token_count(payload),
            _ => {}
        }
    }
    state
}

/// Codex rollouts are the only transcripts that carry the turn signals the
/// seed replays; every other provider's incremental parse skips the read.
fn is_codex_rollout_path(path: &Path) -> bool {
    path.file_name()
        .and_then(|name| name.to_str())
        .is_some_and(|name| name.starts_with("rollout-") && name.ends_with(".jsonl"))
}

fn codex_fact_state_for(path: &Path, offset: u64) -> CodexFactState {
    if is_codex_rollout_path(path) {
        seed_codex_fact_state(path, offset)
    } else {
        CodexFactState::default()
    }
}

fn contains_bytes(haystack: &[u8], needle: &[u8]) -> bool {
    !needle.is_empty() && haystack.windows(needle.len()).any(|window| window == needle)
}

/// Codex provider facts. Only the handful of line shapes that carry a signal
/// are parsed as a JSON tree; `response_item` rows, the transcript hot path,
/// never reach this point.
fn extract_codex_provider_facts(
    obj: &RawLine,
    trimmed: &[u8],
    line_offset: u64,
    facts: &mut Vec<ParsedProviderFact>,
    state: &mut CodexFactState,
) {
    let line_type = obj.r#type.as_deref().unwrap_or("");
    let payload_type = obj
        .payload
        .as_ref()
        .and_then(|p| p.r#type.as_deref())
        .unwrap_or("");
    let wanted = matches!(
        (line_type, payload_type),
        ("turn_context", _)
            | ("compacted", _)
            | (
                "event_msg",
                "task_started" | "token_count" | "task_complete" | "turn_aborted" | "error" | "stream_error"
            )
    );
    if !wanted {
        return;
    }
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(trimmed) else {
        return;
    };
    let Some(payload) = value.get("payload").and_then(|v| v.as_object()) else {
        return;
    };
    match (line_type, payload_type) {
        ("turn_context", _) => {
            state.note_turn_context(payload);
            return;
        }
        ("event_msg", "task_started") => {
            state.note_task_started(payload);
            return;
        }
        ("event_msg", "token_count") => {
            state.note_token_count(payload);
            return;
        }
        _ => {}
    }
    let Some(at) = obj.timestamp.as_deref().and_then(parse_timestamp) else {
        return;
    };
    let mut push = |kind: &str, payload: serde_json::Value| {
        facts.push(ParsedProviderFact {
            kind: kind.to_string(),
            at,
            source_offset: line_offset,
            payload,
        });
    };
    match (line_type, payload_type) {
        ("compacted", _) => {
            let mut fact = serde_json::Map::new();
            if let Some(items) = payload.get("replacement_history").and_then(|v| v.as_array()) {
                fact.insert(
                    "replacement_items".to_string(),
                    serde_json::Value::from(items.len()),
                );
            }
            if let Some(pre) = state
                .last_usage
                .as_ref()
                .and_then(|u| u.get("total_tokens"))
                .and_then(|v| v.as_u64())
            {
                fact.insert("pre_tokens".to_string(), serde_json::Value::from(pre));
            }
            push("context.compaction", serde_json::Value::Object(fact));
        }
        ("event_msg", "task_complete" | "turn_aborted") => {
            if let Some(duration_ms) = payload.get("duration_ms").and_then(|v| v.as_u64()) {
                let mut fact = serde_json::Map::new();
                fact.insert(
                    "duration_ms".to_string(),
                    serde_json::Value::from(duration_ms),
                );
                fact.insert(
                    "outcome".to_string(),
                    serde_json::Value::from(if payload_type == "task_complete" {
                        "completed"
                    } else {
                        "aborted"
                    }),
                );
                if let Some(reason) = payload.get("reason").and_then(|v| v.as_str()) {
                    fact.insert("reason".to_string(), serde_json::Value::from(reason));
                }
                if let Some(ttft) = payload
                    .get("time_to_first_token_ms")
                    .and_then(|v| v.as_u64())
                {
                    fact.insert(
                        "time_to_first_token_ms".to_string(),
                        serde_json::Value::from(ttft),
                    );
                }
                if let Some(turn_id) = payload.get("turn_id").and_then(|v| v.as_str()) {
                    fact.insert("turn_id".to_string(), serde_json::Value::from(turn_id));
                }
                push("turn.duration", serde_json::Value::Object(fact));
            }
            // The context the next prompt starts from is whatever the last
            // model call reported, whether the turn finished or was stopped.
            if let Some(usage) = state.usage_payload() {
                push("turn.usage", usage);
            }
        }
        ("event_msg", "error" | "stream_error") => {
            let Some(message) = payload
                .get("message")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|m| !m.is_empty())
            else {
                return;
            };
            let mut fact = serde_json::Map::new();
            fact.insert(
                "error".to_string(),
                serde_json::Value::from(bounded_text(message, 500)),
            );
            fact.insert("kind".to_string(), serde_json::Value::from(payload_type));
            if let Some(details) = payload
                .get("additional_details")
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|d| !d.is_empty())
            {
                fact.insert(
                    "details".to_string(),
                    serde_json::Value::from(bounded_text(details, 500)),
                );
            }
            push("turn.api_error", serde_json::Value::Object(fact));
        }
        _ => {}
    }
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
        parent_uuid: None,
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
    let trimmed = text.trim();
    trimmed.starts_with(CODEX_TURN_ABORTED_PREFIX)
        && trimmed.ends_with("</turn_aborted>")
        && trimmed.len() > CODEX_TURN_ABORTED_PREFIX.len() + "</turn_aborted>".len()
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
                let input_texts = content_items.iter().filter_map(|item| {
                    (item.r#type.as_deref() == Some("input_text")).then_some(item.text.as_deref().unwrap_or(""))
                });
                let input_texts = input_texts.collect::<Vec<_>>();
                if input_texts.iter().any(|text| codex_text_is_turn_aborted_marker(text)) {
                    if pending.suppress_next_turn_aborted_marker {
                        pending.suppress_next_turn_aborted_marker = false;
                    } else {
                        events.push(ParsedEvent {
                            uuid: format!("{}-action-turn-interrupted-marker", msg_uuid),
                            parent_uuid: None,
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
                if input_texts
                    .iter()
                    .any(|text| codex_text_is_provider_system(text))
                {
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
                parent_uuid: None,
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
        "function_call" | "custom_tool_call" => {
            pending.suppress_next_turn_aborted_marker = false;
            let is_custom = payload.r#type.as_deref() == Some("custom_tool_call");
            let tool_name = payload.name.as_deref().unwrap_or("").to_string();
            let call_id = payload.call_id.as_deref().unwrap_or("");
            let uuid_suffix = if call_id.is_empty() { "0" } else { call_id };

            // Parse arguments string as raw JSON
            let tool_input = if is_custom {
                payload
                    .input
                    .as_ref()
                    .and_then(|input| RawValue::from_string(input.get().to_string()).ok())
            } else {
                payload.arguments.as_ref().and_then(|args| {
                    let trimmed = args.trim();
                    if trimmed.starts_with('{') {
                        RawValue::from_string(trimmed.to_string()).ok()
                    } else {
                        None
                    }
                })
            };

            events.push(ParsedEvent {
                uuid: format!("{}-tool-{}", msg_uuid, uuid_suffix),
                parent_uuid: None,
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
                raw_type: if is_custom {
                    "codex_custom_tool_call".to_string()
                } else {
                    "codex_function_call".to_string()
                },
                raw_line: Some(raw_line.to_string()),
            });
        }
        "function_call_output" | "custom_tool_call_output" => {
            pending.suppress_next_turn_aborted_marker = false;
            let is_custom = payload.r#type.as_deref() == Some("custom_tool_call_output");
            let call_id = payload.call_id.as_deref().unwrap_or("");
            let uuid_suffix = if call_id.is_empty() { "0" } else { call_id };

            // Empty success outputs must still emit a result event so the call
            // does not look orphaned/running forever (same contract as Claude
            // EMPTY_TOOL_RESULT_PLACEHOLDER). Missing `output` entirely is still
            // treated as incomplete evidence and skipped.
            let tool_output_text = match payload.output.as_ref() {
                Some(CodexFunctionOutput::Text(text)) => {
                    if text.is_empty() {
                        EMPTY_TOOL_RESULT_PLACEHOLDER.to_string()
                    } else {
                        text.clone()
                    }
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
                        EMPTY_TOOL_RESULT_PLACEHOLDER.to_string()
                    }
                }
                None => return,
            };

            events.push(ParsedEvent {
                uuid: format!("{}-result-{}", msg_uuid, uuid_suffix),
                parent_uuid: None,
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
                raw_type: if is_custom {
                    "codex_custom_tool_call_output".to_string()
                } else {
                    "codex_function_call_output".to_string()
                },
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
                        parent_uuid: None,
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
                parent_uuid: None,
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
                        parent_uuid: None,
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
                    parent_uuid: None,
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
                parent_uuid: None,
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
    fn test_claude_meta_and_compact_rows_are_system_but_channel_stays_user() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "claude-meta.jsonl",
            &[
                r#"{"type":"user","uuid":"meta-1","isMeta":true,"message":{"role":"user","content":"Continue from the summary."}}"#,
                r#"{"type":"user","uuid":"compact-1","isCompactSummary":true,"message":{"role":"user","content":"This session is being continued from a summary."}}"#,
                r#"{"type":"user","uuid":"channel-1","isMeta":true,"origin":{"kind":"channel"},"message":{"role":"user","content":"A real channel message."}}"#,
                r#"{"type":"user","uuid":"command-1","isMeta":true,"message":{"role":"user","content":"<command-name>/effort</command-name>"}}"#,
                r#"{"type":"user","uuid":"prompt-1","message":{"role":"user","content":"Build the feature."}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 5);
        let meta = result
            .events
            .iter()
            .find(|event| event.content_text.as_deref() == Some("Continue from the summary."))
            .unwrap();
        assert_eq!(meta.role, Role::System);
        assert_eq!(meta.raw_type, "claude_meta");
        let compact = result
            .events
            .iter()
            .find(|event| event.content_text.as_deref() == Some("This session is being continued from a summary."))
            .unwrap();
        assert_eq!(compact.role, Role::System);
        assert_eq!(compact.raw_type, "claude_compact_summary");
        assert!(result.events.iter().any(|event| {
            event.role == Role::User && event.content_text.as_deref() == Some("A real channel message.")
        }));
        assert!(result.events.iter().any(|event| {
            event.role == Role::User
                && event.content_text.as_deref() == Some("<command-name>/effort</command-name>")
        }));
        assert!(result.events.iter().any(|event| {
            event.role == Role::User && event.content_text.as_deref() == Some("Build the feature.")
        }));
    }

    #[test]
    fn test_parse_pi_message_envelope_yields_events() {
        // Captured from the pi Console turn that served an empty session: the
        // transcript was written and bound, the run completed, and the archive
        // held nothing. pi types every record `message` and puts the role inside
        // the envelope, which is neither Claude's placement (role is the type)
        // nor Cursor's (role at top level), so every line matched no branch.
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "2026-08-24T00-51-28-286Z_01a03140-3b9e-7866-bc73-73a1719f2a84.jsonl",
            &[
                r#"{"type":"session","version":3,"id":"01a03140-3b9e-7866-bc73-73a1719f2a84","timestamp":"2026-08-24T00:51:28.286Z","cwd":"/Users/davidrose/git/zerg"}"#,
                r#"{"type":"model_change","id":"a1","parentId":null,"timestamp":"2026-08-24T00:51:28.400Z","provider":"openrouter","modelId":"x"}"#,
                r#"{"type":"message","id":"e924ad85","parentId":"72149a9a","timestamp":"2026-08-24T00:51:28.811Z","message":{"role":"user","content":[{"type":"text","text":"Reply with exactly LH_SERVED_pi and nothing else."}]}}"#,
                r#"{"type":"message","id":"4fe8d1c0","parentId":"e924ad85","timestamp":"2026-08-24T00:51:31.861Z","message":{"role":"assistant","content":[{"type":"text","text":"LH_SERVED_pi"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(
            result.events.len(),
            2,
            "a completed pi turn must archive its user prompt and assistant reply"
        );
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("LH_SERVED_pi")
        );
    }

    #[test]
    fn test_parse_cursor_agent_transcript_messages_and_injections() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000099.jsonl",
            &[
                r#"{"role":"user","message":{"content":[{"type":"text","text":"<user_query>Reply with exactly LH_CURSOR_SEED_abc123 and no other text.</user_query>"}]}}"#,
                r#"{"role":"assistant","message":{"content":[{"type":"text","text":"LH_CURSOR_SEED_abc123"}]}}"#,
                r#"{"role":"user","message":{"content":[{"type":"text","text":"<agent_transcripts>context injected by Cursor</agent_transcripts>"}]}}"#,
                r#"{"type":"turn_ended","status":"success"}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 3);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Reply with exactly LH_CURSOR_SEED_abc123 and no other text.")
        );
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("LH_CURSOR_SEED_abc123")
        );
        assert_eq!(result.events[2].role, Role::System);
        assert_eq!(
            result.metadata.session_id,
            "019c638d-0000-0000-0000-000000000099"
        );
    }

    #[test]
    fn test_cursor_nested_history_query_and_quoted_marker_are_fail_open() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "cursor-boundaries.jsonl",
            &[
                r#"{"role":"user","message":{"content":[{"type":"text","text":"<agent_transcripts>\n<user_query>old history</user_query>\n</agent_transcripts>\n<user_query>current prompt</user_query>"}]}}"#,
                r#"{"role":"user","message":{"content":[{"type":"text","text":"Please explain the literal <rules> marker."}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[0].content_text.as_deref(), Some("current prompt"));
        assert_eq!(result.events[1].role, Role::User);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("Please explain the literal <rules> marker.")
        );
    }

    #[test]
    /// A Task/Agent subagent's spawning tool call exists only in the sidecar.
    /// Without this read the child can never be bound to the row that spawned
    /// it, which is the whole navigational value of nesting.
    #[test]
    fn claude_task_subagent_reads_tool_use_id_from_its_sidecar() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "agent-a7bc5fa882b7a5890.jsonl",
            &[&json!({
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-06-22T21:02:00.000Z",
                "isSidechain": true,
                "sessionId": "04757373-353b-43b1-b3fd-07b42385ccdc",
                "agentId": "a7bc5fa882b7a5890",
                "cwd": "/Users/davidrose/git/zerg",
                "message": {"content": "Map steer-loop send path"}
            })
            .to_string()],
        );
        std::fs::write(
            path.with_file_name("agent-a7bc5fa882b7a5890.meta.json"),
            r#"{"agentType":"Explore","description":"Map steer-loop send path","toolUseId":"toolu_bdrk_01D9TRPzuFXSbfdQojYmK34J"}"#,
        )
        .unwrap();

        let result = parse_session_file(&path, 0).unwrap();

        assert!(result.metadata.is_sidechain);
        assert_eq!(
            result.metadata.subagent_tool_use_id.as_deref(),
            Some("toolu_bdrk_01D9TRPzuFXSbfdQojYmK34J")
        );
    }

    /// Fail closed. A malformed sidecar must leave the child unattached rather
    /// than attached to a guess, and a transcript with no sidecar at all is the
    /// ordinary workflow case.
    #[test]
    fn subagent_sidecar_binding_fails_closed() {
        let dir = tempfile::tempdir().unwrap();
        let line = json!({
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-06-22T21:02:00.000Z",
            "isSidechain": true,
            "sessionId": "04757373-353b-43b1-b3fd-07b42385ccdc",
            "agentId": "a7bc5fa882b7a5890",
            "message": {"content": "work"}
        })
        .to_string();
        let lines = [line.as_str()];

        let missing = make_jsonl_file(dir.path(), "agent-amissing.jsonl", &lines);
        assert!(parse_session_file(&missing, 0)
            .unwrap()
            .metadata
            .subagent_tool_use_id
            .is_none());

        let malformed = make_jsonl_file(dir.path(), "agent-amalformed.jsonl", &lines);
        std::fs::write(
            malformed.with_file_name("agent-amalformed.meta.json"),
            "{not json",
        )
        .unwrap();
        assert!(parse_session_file(&malformed, 0)
            .unwrap()
            .metadata
            .subagent_tool_use_id
            .is_none());

        let empty = make_jsonl_file(dir.path(), "agent-aempty.jsonl", &lines);
        std::fs::write(
            empty.with_file_name("agent-aempty.meta.json"),
            r#"{"agentType":"Explore","toolUseId":"   "}"#,
        )
        .unwrap();
        assert!(parse_session_file(&empty, 0)
            .unwrap()
            .metadata
            .subagent_tool_use_id
            .is_none());
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
    fn test_claude_multi_tool_use_explodes_and_pairs_by_id_when_reordered() {
        // One assistant JSONL record with N tool_use blocks → N call events.
        // Results arriving out of order must still carry tool_use_id → tool_call_id.
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "claude-multi.jsonl",
            &[
                r#"{"type":"assistant","uuid":"a-multi","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"Looking around"},{"type":"tool_use","id":"toolu_grep1","name":"Grep","input":{"pattern":"Longhouse"}},{"type":"tool_use","id":"toolu_grep2","name":"Grep","input":{"pattern":"tool_call"}},{"type":"tool_use","id":"toolu_read1","name":"Read","input":{"file_path":"/repo/README.md"}}]}}"#,
                r#"{"type":"user","uuid":"u-r2","timestamp":"2026-01-01T00:00:02Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_grep2","content":"match-b"}]}}"#,
                r#"{"type":"user","uuid":"u-r1","timestamp":"2026-01-01T00:00:03Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_grep1","content":"match-a"}]}}"#,
                r##"{"type":"user","uuid":"u-r3","timestamp":"2026-01-01T00:00:04Z","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_read1","content":"# README"}]}}"##,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 7);

        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("Looking around")
        );
        assert_eq!(result.events[1].tool_name.as_deref(), Some("Grep"));
        assert_eq!(
            result.events[1].tool_call_id.as_deref(),
            Some("toolu_grep1")
        );
        assert_eq!(
            result.events[2].tool_call_id.as_deref(),
            Some("toolu_grep2")
        );
        assert_eq!(result.events[3].tool_name.as_deref(), Some("Read"));
        assert_eq!(
            result.events[3].tool_call_id.as_deref(),
            Some("toolu_read1")
        );

        assert_eq!(result.events[4].role, Role::Tool);
        assert_eq!(
            result.events[4].tool_call_id.as_deref(),
            Some("toolu_grep2")
        );
        assert_eq!(
            result.events[4].tool_output_text.as_deref(),
            Some("match-b")
        );
        assert_eq!(
            result.events[5].tool_call_id.as_deref(),
            Some("toolu_grep1")
        );
        assert_eq!(
            result.events[5].tool_output_text.as_deref(),
            Some("match-a")
        );
        assert_eq!(
            result.events[6].tool_call_id.as_deref(),
            Some("toolu_read1")
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
    fn test_codex_custom_tool_call_and_output() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-000000000003.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-07-16T15:38:17Z","payload":{"type":"custom_tool_call","name":"exec","input":"const result = await tools.exec_command({cmd:\"sed -n '1,80p' docs/as-built.md\"}); text(result.output)","call_id":"call_HEf"}}"#,
                r#"{"type":"response_item","timestamp":"2026-07-16T15:38:18Z","payload":{"type":"custom_tool_call_output","call_id":"call_HEf","output":[{"type":"input_text","text":"Bilstein B6 shocks on all four corners"}]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 2);
        assert_eq!(result.events[0].role, Role::Assistant);
        assert_eq!(result.events[0].tool_name.as_deref(), Some("exec"));
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("call_HEf"));
        assert_eq!(result.events[0].raw_type, "codex_custom_tool_call");
        let input = result.events[0].tool_input_json.as_ref().unwrap().get();
        assert_eq!(
            serde_json::from_str::<String>(input).unwrap(),
            "const result = await tools.exec_command({cmd:\"sed -n '1,80p' docs/as-built.md\"}); text(result.output)"
        );
        assert_eq!(result.events[1].role, Role::Tool);
        assert_eq!(result.events[1].tool_call_id.as_deref(), Some("call_HEf"));
        assert_eq!(
            result.events[1].tool_output_text.as_deref(),
            Some("Bilstein B6 shocks on all four corners")
        );
        assert_eq!(result.events[1].raw_type, "codex_custom_tool_call_output");
    }

    #[test]
    fn test_codex_empty_function_call_output_emits_placeholder() {
        // Empty stdout is still a completed tool result. Historically the Codex
        // parser dropped empty string outputs, leaving the call unpaired.
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000empty.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:13Z","payload":{"type":"function_call","name":"shell","arguments":"{\"cmd\":\"true\"}","call_id":"call_empty"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:14Z","payload":{"type":"function_call_output","call_id":"call_empty","output":""}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:15Z","payload":{"type":"function_call_output","call_id":"call_empty_items","output":[]}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 3);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("call_empty"));
        assert_eq!(result.events[1].role, Role::Tool);
        assert_eq!(result.events[1].tool_call_id.as_deref(), Some("call_empty"));
        assert_eq!(
            result.events[1].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
        assert_eq!(
            result.events[2].tool_call_id.as_deref(),
            Some("call_empty_items")
        );
        assert_eq!(
            result.events[2].tool_output_text.as_deref(),
            Some(EMPTY_TOOL_RESULT_PLACEHOLDER)
        );
    }

    #[test]
    fn test_codex_multi_function_calls_preserve_call_ids_when_reordered() {
        let dir = tempfile::tempdir().unwrap();
        let path = make_jsonl_file(
            dir.path(),
            "019c638d-0000-0000-0000-00000000multi.jsonl",
            &[
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:15Z","payload":{"type":"function_call","name":"shell","arguments":"{\"cmd\":\"echo a\"}","call_id":"call_a"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:16Z","payload":{"type":"function_call","name":"shell","arguments":"{\"cmd\":\"echo b\"}","call_id":"call_b"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:17Z","payload":{"type":"function_call_output","call_id":"call_b","output":"b"}}"#,
                r#"{"type":"response_item","timestamp":"2026-02-15T17:06:18Z","payload":{"type":"function_call_output","call_id":"call_a","output":"a"}}"#,
            ],
        );

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(result.events.len(), 4);
        assert_eq!(result.events[0].tool_call_id.as_deref(), Some("call_a"));
        assert_eq!(result.events[1].tool_call_id.as_deref(), Some("call_b"));
        assert_eq!(result.events[2].tool_call_id.as_deref(), Some("call_b"));
        assert_eq!(result.events[2].tool_output_text.as_deref(), Some("b"));
        assert_eq!(result.events[3].tool_call_id.as_deref(), Some("call_a"));
        assert_eq!(result.events[3].tool_output_text.as_deref(), Some("a"));
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
    fn antigravity_transcript_recovers_its_workspace_from_the_sidecar() {
        let temp = tempfile::tempdir().unwrap();
        let id = "5f62a636-1412-4afe-9cfd-a5079e0a0366";
        let transcript = temp
            .path()
            .join("brain")
            .join(id)
            .join(".system_generated")
            .join("logs")
            .join("transcript.jsonl");
        std::fs::create_dir_all(transcript.parent().unwrap()).unwrap();
        // A real antigravity record: no cwd anywhere in the stream.
        std::fs::write(
            &transcript,
            r#"{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","created_at":"2026-08-22T20:48:39Z","content":"hello"}
"#,
        )
        .unwrap();
        let workspace = temp.path().join("g55");
        std::fs::create_dir_all(&workspace).unwrap();
        std::fs::write(
            temp.path().join("history.jsonl"),
            format!(
                r#"{{"display":"hello","workspace":"{}","conversationId":"{id}"}}"#,
                workspace.display()
            ),
        )
        .unwrap();

        let result = parse_session_file(&transcript, 0).unwrap();
        assert_eq!(
            result.metadata.cwd.as_deref(),
            Some(workspace.to_string_lossy().as_ref())
        );
        assert_eq!(result.metadata.project.as_deref(), Some("g55"));
    }

    #[test]
    fn a_home_directory_session_has_no_project() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("someuser");
        std::fs::create_dir_all(&home).unwrap();
        let previous = std::env::var_os("HOME");
        std::env::set_var("HOME", &home);
        let (project, git_repo) = resolve_git_info(&home);
        match previous {
            Some(value) => std::env::set_var("HOME", value),
            None => std::env::remove_var("HOME"),
        }
        // Without this the session files itself under a project named after
        // the user, which is what Antigravity's $HOME sessions would produce.
        assert_eq!(project, None);
        assert_eq!(git_repo, None);
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
    fn resolve_git_info_uses_shared_repo_identity_for_linked_worktree() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("longhouse");
        let common_git_dir = repo.join(".git");
        let worktree_git_dir = common_git_dir.join("worktrees/coordination");
        let worktree = dir.path().join("longhouse-agent-coordination-loop");
        std::fs::create_dir_all(&worktree_git_dir).unwrap();
        std::fs::create_dir_all(&worktree).unwrap();
        std::fs::write(
            common_git_dir.join("config"),
            "[remote \"origin\"]\n\turl = git@github.com:cipher982/longhouse.git\n",
        )
        .unwrap();
        std::fs::write(worktree_git_dir.join("commondir"), "../..\n").unwrap();
        std::fs::write(
            worktree.join(".git"),
            format!("gitdir: {}\n", worktree_git_dir.display()),
        )
        .unwrap();

        let (project, git_repo) = resolve_git_info(&worktree);

        assert_eq!(project.as_deref(), Some("longhouse"));
        assert_eq!(
            git_repo.as_deref(),
            Some("git@github.com:cipher982/longhouse.git")
        );
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
        // A plain fork is not a sidechain: only a provider subagent is, and it
        // says so with `source.subAgent`. Whether this fork is visible is the
        // shipper's call, from whether a binding names this thread.
        assert!(!result.metadata.is_sidechain);
        assert!(result.metadata.is_plain_fork());
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
        // A plain fork is not a sidechain: only a provider subagent is, and it
        // says so with `source.subAgent`. Whether this fork is visible is the
        // shipper's call, from whether a binding names this thread.
        assert!(!result.metadata.is_sidechain);
        assert!(result.metadata.is_plain_fork());
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
        // A plain fork is not a sidechain: only a provider subagent is, and it
        // says so with `source.subAgent`. Whether this fork is visible is the
        // shipper's call, from whether a binding names this thread.
        assert!(!result.metadata.is_sidechain);
        assert!(result.metadata.is_plain_fork());
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
    fn test_gemini_legacy_logs_array_uses_message_field() {
        let dir = tempfile::tempdir().unwrap();
        let session_json = r#"[
            {"sessionId":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","messageId":0,"type":"user","message":"hello from legacy Gemini","timestamp":"2026-01-10T10:00:00Z"},
            {"sessionId":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa","messageId":1,"type":"gemini","message":"legacy answer","timestamp":"2026-01-10T10:00:01Z"}
        ]"#;
        let path = make_json_file(dir.path(), "logs.json", session_json);

        let result = parse_session_file(&path, 0).unwrap();

        assert_eq!(result.events.len(), 2);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(result.events[0].content_text.as_deref(), Some("hello from legacy Gemini"));
        assert_eq!(result.events[1].role, Role::Assistant);
        assert_eq!(result.metadata.session_id, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
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
        // Codex's goal context, AGENTS.md, and environment context injected as
        // role=user must be dropped.
        let dir = tempfile::tempdir().unwrap();

        // Build lines programmatically to avoid backslash escaping issues in raw strings
        let goal_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-03-01T09:59:59Z",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "<codex_internal_context source=\"goal\">\n<objective>keep working</objective>\n</codex_internal_context>"}]
            }
        }).to_string();
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
        let lookalike_line = serde_json::json!({
            "type": "response_item",
            "timestamp": "2026-03-01T10:00:02Z",
            "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Please quote <codex_internal_context source=\"goal\"> in your answer"}]
            }
        }).to_string();

        let path = {
            let path = dir
                .path()
                .join("019c638d-0000-0000-0000-000000000012.jsonl");
            let mut f = std::fs::File::create(&path).unwrap();
            use std::io::Write;
            writeln!(f, "{}", goal_line).unwrap();
            writeln!(f, "{}", agents_line).unwrap();
            writeln!(f, "{}", env_line).unwrap();
            writeln!(f, "{}", real_line).unwrap();
            writeln!(f, "{}", lookalike_line).unwrap();
            path
        };

        let result = parse_session_file(&path, 0).unwrap();
        assert_eq!(
            result.events.len(),
            2,
            "only real user messages should survive"
        );
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("please help me debug this")
        );
        assert_eq!(result.events[1].role, Role::User);
        assert_eq!(
            result.events[1].content_text.as_deref(),
            Some("Please quote <codex_internal_context source=\"goal\"> in your answer")
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

    #[test]
    fn test_antigravity_user_request_wrapper_is_fail_open() {
        assert_eq!(
            antigravity_user_text("<USER_REQUEST>\nfix it\n</USER_REQUEST>"),
            "fix it"
        );
        assert_eq!(
            antigravity_user_text("quoted <USER_REQUEST>fix it</USER_REQUEST> text"),
            "quoted <USER_REQUEST>fix it</USER_REQUEST> text"
        );
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
