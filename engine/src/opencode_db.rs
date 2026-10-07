//! OpenCode SQLite transcript reader.
//!
//! OpenCode stores durable history in `~/.local/share/opencode/opencode.db`
//! rather than append-only JSONL. This module projects that local SQLite shape
//! into the same normalized parser events used by the rest of the shipper.

use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, TimeZone, Utc};
use rusqlite::{params, Connection, OpenFlags};
use serde::Deserialize;
use serde_json::value::RawValue;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::config::get_longhouse_home;
use crate::media_redaction::{
    redact_inline_image_data_url, redact_source_line_with_media, InlineImageRedaction,
};
use crate::pipeline::parser::{
    ParseResult, ParsedEvent, ParsedMediaObject, ParsedSourceLine, Role, SessionMetadata,
};

const SOURCE_OFFSET_SCALE: u64 = 1_000_000;
const MAX_SOURCE_FILE_URL_CHARS: usize = 512;

#[derive(Debug, Clone)]
pub struct OpenCodeSessionCandidate {
    pub provider_session_id: String,
    pub source_key: String,
    pub version: u64,
    pub fingerprint: String,
    /// When OpenCode created the session, for the machine's import scope.
    pub created_ms: Option<i64>,
    /// The folder the session ran in, for the machine's import scope.
    pub directory: Option<String>,
}

impl OpenCodeSessionCandidate {
    /// Whether the machine's import scope lets this session be shipped.
    pub fn in_import_scope(&self, scope: &crate::import_scope::ImportScope) -> bool {
        // A row with no creation time is not known to be new, so it is old.
        scope.admits(
            Some(
                self.created_ms
                    .and_then(chrono::DateTime::from_timestamp_millis)
                    .unwrap_or(chrono::DateTime::UNIX_EPOCH),
            ),
            || self.directory.clone(),
        )
    }
}

#[derive(Debug, Clone)]
struct OpenCodeSessionRow {
    project_id: Option<String>,
    parent_id: Option<String>,
    agent: Option<String>,
    project_worktree: Option<String>,
    project_name: Option<String>,
    directory: Option<String>,
    path: Option<String>,
    title: Option<String>,
    version: Option<String>,
    time_created: i64,
    time_updated: i64,
}

#[derive(Debug)]
struct OpenCodeMessageRow {
    id: String,
    time_created: i64,
    time_updated: i64,
    data: String,
}

#[derive(Debug)]
struct OpenCodePartRow {
    id: String,
    message_id: String,
    time_created: i64,
    time_updated: i64,
    data: String,
}

#[derive(Debug, Deserialize)]
struct OpenCodeSessionClassificationSidecar {
    provider: Option<String>,
    provider_session_id: Option<String>,
    environment: Option<String>,
    origin_kind: Option<String>,
    launch_actor: Option<String>,
    launch_surface: Option<String>,
    hatch_run_id: Option<String>,
    parent_longhouse_session_id: Option<String>,
    parent_thread_id: Option<String>,
    parent_provider_session_id: Option<String>,
}

#[derive(Debug, Clone, Default)]
struct OpenCodeTaskChildEvidence {
    agent: Option<String>,
    tool_call_id: Option<String>,
    metadata: Value,
}

pub fn is_opencode_database_path(path: &Path) -> bool {
    path.file_name()
        .and_then(|value| value.to_str())
        .map(|value| value == "opencode.db")
        .unwrap_or(false)
}

pub fn opencode_source_key(db_path: &Path, provider_session_id: &str) -> String {
    format!("{}#opencode:{}", db_path.display(), provider_session_id)
}

pub fn longhouse_session_id_for_opencode(provider_session_id: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("opencode:{provider_session_id}").as_bytes(),
    )
    .to_string()
}

pub fn managed_longhouse_session_id_for_opencode(provider_session_id: &str) -> Option<String> {
    managed_longhouse_session_id_for_opencode_from_roots(
        provider_session_id,
        &opencode_state_roots(),
    )
}

/// A newly created top-level session can become visible in OpenCode's SQLite
/// store a few milliseconds before the private server event monitor updates its
/// managed bridge state. Hold that source briefly instead of permanently
/// archiving its first batch as an unrelated shadow session.
pub fn managed_binding_may_be_pending(metadata: &SessionMetadata) -> bool {
    managed_binding_may_be_pending_from_roots(metadata, &opencode_state_roots(), Utc::now())
}

fn managed_binding_may_be_pending_from_roots(
    metadata: &SessionMetadata,
    roots: &[PathBuf],
    now: DateTime<Utc>,
) -> bool {
    if metadata.is_sidechain
        || metadata.forked_from_session_id.is_some()
        || metadata.subagent_id.is_some()
    {
        return false;
    }
    let Some(provider_session_id) = metadata.provider_session_id.as_deref() else {
        return false;
    };
    let Some(cwd) = metadata.cwd.as_deref() else {
        return false;
    };
    let Some(started_at) = metadata.started_at else {
        return false;
    };
    let age = now.signed_duration_since(started_at);
    if age < chrono::Duration::seconds(-2) || age > chrono::Duration::seconds(5) {
        return false;
    }
    for root in roots {
        let Ok(entries) = fs::read_dir(root) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) != Some("json") {
                continue;
            }
            let Some(value) = fs::read(&path)
                .ok()
                .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok())
            else {
                continue;
            };
            let same_workspace = value
                .get("cwd")
                .and_then(Value::as_str)
                .is_some_and(|state_cwd| paths_resolve_equal(state_cwd, cwd));
            let different_active_session = value
                .get("provider_session_id")
                .and_then(Value::as_str)
                .is_some_and(|active| active != provider_session_id);
            let server_alive = value
                .get("pid")
                .and_then(Value::as_u64)
                .and_then(|pid| i32::try_from(pid).ok())
                .is_some_and(|pid| pid > 0 && unsafe { libc::kill(pid, 0) == 0 });
            if same_workspace && different_active_session && server_alive {
                return true;
            }
        }
    }
    false
}

fn paths_resolve_equal(left: &str, right: &str) -> bool {
    let left = PathBuf::from(left);
    let right = PathBuf::from(right);
    match (fs::canonicalize(&left), fs::canonicalize(&right)) {
        (Ok(left), Ok(right)) => left == right,
        _ => left == right,
    }
}

#[cfg(test)]
pub fn list_opencode_sessions(db_path: &Path) -> Result<Vec<OpenCodeSessionCandidate>> {
    list_opencode_sessions_inner(db_path, None, true)
}

/// List cheap change watermarks without hashing every message and part.
///
/// The daemon calls this on every provider DB wake. Fingerprints are computed
/// only for candidates whose watermark moved (or an explicit reconciliation
/// pass), keeping an idle tick proportional to the changed sessions rather
/// than the provider's lifetime corpus.
#[cfg(test)]
pub fn list_opencode_session_watermarks(db_path: &Path) -> Result<Vec<OpenCodeSessionCandidate>> {
    list_opencode_sessions_inner(db_path, None, false)
}

pub fn list_opencode_sessions_page(
    db_path: &Path,
    limit: usize,
    offset: usize,
) -> Result<Vec<OpenCodeSessionCandidate>> {
    if limit == 0 {
        return Ok(Vec::new());
    }
    // No fingerprints: the one caller walks the DB session by session and
    // reads each session's records itself, so hashing every message and part
    // of the whole page here first was a second read of the corpus whose
    // result nothing looks at.
    list_opencode_sessions_inner(db_path, Some((limit, offset)), false)
}

fn list_opencode_sessions_inner(
    db_path: &Path,
    page: Option<(usize, usize)>,
    include_fingerprints: bool,
) -> Result<Vec<OpenCodeSessionCandidate>> {
    let conn = open_readonly(db_path)?;
    let has_agent_column = sqlite_column_exists(&conn, "session", "agent")?;
    // These MAX lookups use OpenCode's session indexes and read timestamps,
    // not content. The hot path therefore detects a normal message/part write
    // without hashing every historical payload. The periodic durability pass
    // remains the repair path for a provider bug or hand-edited row that
    // changes content without advancing any timestamp.
    let version_expression = "MAX(\
        s.time_updated, \
        COALESCE((SELECT MAX(m.time_updated) FROM message m WHERE m.session_id = s.id), 0), \
        COALESCE((SELECT MAX(p.time_updated) FROM part p WHERE p.session_id = s.id), 0)\
    )";
    let sql = format!(
        r#"
        SELECT s.id, {version_expression} AS version_ms, s.time_created, s.directory
        FROM session s
        ORDER BY version_ms DESC, s.id ASC
        {}
        "#,
        if page.is_some() {
            "LIMIT ?1 OFFSET ?2"
        } else {
            ""
        }
    );
    let mut stmt = conn.prepare(&sql)?;
    let map_row = |row: &rusqlite::Row<'_>| -> rusqlite::Result<OpenCodeSessionCandidate> {
        let provider_session_id: String = row.get(0)?;
        let version_ms: i64 = row.get(1)?;
        Ok(OpenCodeSessionCandidate {
            source_key: opencode_source_key(db_path, &provider_session_id),
            provider_session_id,
            version: version_from_ms(version_ms),
            fingerprint: String::new(),
            created_ms: row.get(2)?,
            directory: row.get(3)?,
        })
    };
    let mut rows = match page {
        Some((limit, offset)) => stmt.query(params![
            i64::try_from(limit).unwrap_or(i64::MAX),
            i64::try_from(offset).unwrap_or(i64::MAX)
        ])?,
        None => stmt.query([])?,
    };

    let mut sessions = Vec::new();
    while let Some(row) = rows.next()? {
        let mut candidate = map_row(row)?;
        if include_fingerprints {
            candidate.fingerprint =
                session_fingerprint(&conn, &candidate.provider_session_id, has_agent_column)?;
        }
        sessions.push(candidate);
    }
    Ok(sessions)
}

#[cfg(test)]
pub fn opencode_session_fingerprints(
    db_path: &Path,
    provider_session_ids: &[String],
) -> Result<Vec<String>> {
    let conn = open_readonly(db_path)?;
    let has_agent_column = sqlite_column_exists(&conn, "session", "agent")?;
    provider_session_ids
        .iter()
        .map(|provider_session_id| {
            session_fingerprint(&conn, provider_session_id, has_agent_column)
        })
        .collect()
}

/// A signature of every managed-state file an OpenCode source can be bound
/// through, or `None` when one was written too recently to vouch for. The same
/// signature later means no binding evidence appeared, changed or went away.
pub fn managed_state_signature() -> Option<String> {
    opencode_state_roots()
        .iter()
        .map(|root| crate::dir_cache::rested_signature(root))
        .collect::<Option<Vec<_>>>()
        .map(|signatures| signatures.join(","))
}

fn opencode_state_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Ok(longhouse_home) = get_longhouse_home() {
        roots.push(longhouse_home.join("managed-local/opencode/bridge/sessions"));
    }
    if let Some(root) = std::env::var_os("LONGHOUSE_OPENCODE_STATE_ROOT") {
        roots.push(PathBuf::from(root));
    }
    if let Some(provider_home) = std::env::var_os("CLAUDE_CONFIG_DIR") {
        roots.push(
            PathBuf::from(provider_home)
                .join("managed-local")
                .join("opencode-server"),
        );
    }
    if let Some(home) = std::env::var_os("HOME") {
        roots.push(
            PathBuf::from(&home)
                .join(".claude")
                .join("managed-local")
                .join("opencode-server"),
        );
        roots.push(
            PathBuf::from(&home)
                .join(".longhouse")
                .join("managed-local")
                .join("opencode")
                .join("bridge")
                .join("sessions"),
        );
        roots.push(
            PathBuf::from(home)
                .join(".claude")
                .join("managed-local")
                .join("opencode"),
        );
    }
    roots.dedup();
    roots
}

fn managed_longhouse_session_id_for_opencode_from_roots(
    provider_session_id: &str,
    roots: &[PathBuf],
) -> Option<String> {
    let provider_session_id = provider_session_id.trim();
    if provider_session_id.is_empty() {
        return None;
    }
    for root in roots {
        // Read once per change to the directory, not once per session asked
        // about: a scan asks about every session in the database.
        let states = crate::dir_cache::parsed_json_dir(root, parse_state_file, |left, right| {
            left.0.cmp(&right.0)
        })
        .unwrap_or_default();
        for (_path, value) in states.iter() {
            let provider = value
                .get("provider")
                .and_then(Value::as_str)
                .unwrap_or("opencode");
            if provider != "opencode" {
                continue;
            }
            let state_provider_session_id = value
                .get("provider_session_id")
                .or_else(|| value.get("opencode_session_id"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty());
            let prior_provider_session_id = value
                .get("previous_provider_session_ids")
                .and_then(Value::as_array)
                .is_some_and(|values| {
                    values
                        .iter()
                        .filter_map(Value::as_str)
                        .map(str::trim)
                        .any(|value| value == provider_session_id)
                });
            if state_provider_session_id != Some(provider_session_id) && !prior_provider_session_id
            {
                continue;
            }
            let Some(longhouse_session_id) = value
                .get("longhouse_session_id")
                .or_else(|| value.get("session_id"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
            else {
                continue;
            };
            if Uuid::parse_str(longhouse_session_id).is_ok() {
                return Some(longhouse_session_id.to_string());
            }
        }
    }
    None
}

fn parse_state_file(path: &Path, bytes: std::io::Result<Vec<u8>>) -> Option<(PathBuf, Value)> {
    let value = serde_json::from_str::<Value>(&String::from_utf8(bytes.ok()?).ok()?).ok()?;
    Some((path.to_path_buf(), value))
}

const MAX_PROVIDER_FACT_PAYLOAD_CHARS: usize = 8_192;

fn push_opencode_fact(
    facts: &mut Vec<crate::pipeline::parser::ParsedProviderFact>,
    kind: &str,
    at: DateTime<Utc>,
    source_offset: u64,
    payload: Value,
) {
    if facts
        .iter()
        .any(|fact| fact.source_offset == source_offset && fact.kind == kind)
    {
        return;
    }
    let Ok(encoded) = serde_json::to_string(&payload) else {
        return;
    };
    if encoded.chars().count() > MAX_PROVIDER_FACT_PAYLOAD_CHARS {
        return;
    }
    facts.push(crate::pipeline::parser::ParsedProviderFact {
        kind: kind.to_string(),
        at,
        source_offset,
        payload,
    });
}

pub fn parse_opencode_session(db_path: &Path, provider_session_id: &str) -> Result<ParseResult> {
    let conn = open_readonly(db_path)?;
    let rows = load_session_rows(&conn, provider_session_id)?;
    Ok(parse_rows(&conn, provider_session_id, &rows, None)?.result)
}

/// Project rows into events. With `shipped_parts`, only those parts are
/// projected and each source line is paired with its part's record ordinal;
/// without, every part is.
fn parse_rows(
    conn: &Connection,
    provider_session_id: &str,
    rows: &OpenCodeSessionRows,
    shipped_parts: Option<&HashMap<String, u64>>,
) -> Result<OpenCodeParse> {
    let OpenCodeSessionRows {
        session,
        messages,
        parts,
    } = rows;
    let messages_by_id: HashMap<&str, &OpenCodeMessageRow> = messages
        .iter()
        .map(|message| (message.id.as_str(), message))
        .collect();
    let mut provider_facts = Vec::new();

    let longhouse_session_id = longhouse_session_id_for_opencode(provider_session_id);
    let mut events = Vec::new();
    let mut source_lines = Vec::new();
    let mut media_objects = Vec::new();
    let mut candidate_records = 0usize;
    let mut last_source_offset = 0u64;
    let mut line_ordinals = Vec::new();

    for (part_index, part) in parts.iter().enumerate() {
        let ordinal = match shipped_parts {
            Some(shipped) => match shipped.get(&part.id) {
                Some(ordinal) => Some(*ordinal),
                None => continue,
            },
            None => None,
        };
        let Some(message) = messages_by_id.get(part.message_id.as_str()).copied() else {
            continue;
        };
        candidate_records += 1;
        let message_data: Value = serde_json::from_str(&message.data)
            .with_context(|| format!("parsing OpenCode message {}", message.id))?;
        let part_data: Value = serde_json::from_str(&part.data)
            .with_context(|| format!("parsing OpenCode part {}", part.id))?;
        let (source_part_data, mut part_media) = source_line_part_data(&part_data);
        let source_offset = source_offset_for_part(part, part_index);
        last_source_offset = last_source_offset.max(source_offset);
        let original_source_raw = serde_json::to_string(&json!({
            "provider": "opencode",
            "session_id": provider_session_id,
            "message_id": message.id,
            "part_id": part.id,
            "message": message_data.clone(),
            "part": part_data.clone(),
        }))?;
        let original_line_sha256 = format!("{:x}", Sha256::digest(original_source_raw.as_bytes()));
        let source_raw = serde_json::to_string(&json!({
            "provider": "opencode",
            "session_id": provider_session_id,
            "message_id": message.id,
            "part_id": part.id,
            "message": message_data.clone(),
            "part": source_part_data,
        }))?;
        let redacted_source_raw = redact_source_line_with_media(&source_raw, None);
        part_media.extend(redacted_source_raw.media);
        media_objects.extend(parsed_media_objects_from_redactions(
            source_offset,
            &original_line_sha256,
            part_media,
        ));
        source_lines.push(ParsedSourceLine {
            source_offset,
            raw_line: redacted_source_raw.raw_line,
        });
        line_ordinals.extend(ordinal);

        let role = message_data
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("assistant");
        extract_events_from_part(
            provider_session_id,
            &longhouse_session_id,
            message,
            part,
            &part_data,
            role,
            source_offset,
            &mut events,
        )?;
        if let Some(spawn) = opencode_task_spawn_evidence(&part_data) {
            let parent_claim = spawn
                .metadata
                .get("parentSessionId")
                .or_else(|| spawn.metadata.get("parent_session_id"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty());
            if !parent_claim.is_some_and(|value| value != provider_session_id) {
                let mut child = serde_json::Map::new();
                child.insert(
                    "provider_session_id".to_string(),
                    Value::from(spawn.child_provider_session_id),
                );
                if let Some(call_id) = spawn.tool_call_id {
                    child.insert("parent_tool_call_id".to_string(), Value::from(call_id));
                }
                child.insert("metadata".to_string(), spawn.metadata);
                push_opencode_fact(
                    &mut provider_facts,
                    "delegation.spawn",
                    timestamp_from_ms(part.time_created.max(message.time_created)),
                    source_offset,
                    json!({ "children": [Value::Object(child)] }),
                );
            }
        }
        if let Some(activity) = opencode_task_activity_evidence(&part_data) {
            let parent_claim = activity
                .metadata
                .get("parentSessionId")
                .or_else(|| activity.metadata.get("parent_session_id"))
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty());
            if !parent_claim.is_some_and(|value| value != provider_session_id) {
                let mut activity_payload = serde_json::Map::new();
                activity_payload.insert(
                    "provider_session_id".to_string(),
                    Value::from(activity.child_provider_session_id),
                );
                activity_payload.insert("kind".to_string(), Value::from(activity.kind));
                activity_payload.insert("event_id".to_string(), Value::from(part.id.clone()));
                if let Some(tool_call_id) = activity.tool_call_id {
                    activity_payload
                        .insert("parent_tool_call_id".to_string(), Value::from(tool_call_id));
                }
                if let Some(occurred_at_ms) = activity.occurred_at_ms {
                    activity_payload
                        .insert("occurred_at_ms".to_string(), Value::from(occurred_at_ms));
                }
                activity_payload.insert("metadata".to_string(), activity.metadata);
                push_opencode_fact(
                    &mut provider_facts,
                    "delegation.activity",
                    timestamp_from_ms(part.time_updated.max(message.time_updated)),
                    source_offset,
                    Value::Object(activity_payload),
                );
            }
        }
    }

    events.sort_by(|left, right| {
        left.source_offset
            .cmp(&right.source_offset)
            .then(left.uuid.cmp(&right.uuid))
    });

    let session_version = opencode_session_version(conn, provider_session_id)?
        .unwrap_or_else(|| last_source_offset.saturating_add(1));

    let task_child = match session.parent_id.as_deref() {
        Some(parent_id) => opencode_task_child_evidence(conn, parent_id, provider_session_id)?,
        None => None,
    };
    let task_child_agent = task_child
        .as_ref()
        .and_then(|evidence| evidence.agent.as_deref())
        .or(session.agent.as_deref())
        .map(str::to_string);
    let lineage_kind = opencode_lineage_kind(session, task_child.is_some());
    let classification = opencode_session_classification_sidecar(provider_session_id);
    if let (Some(parent_provider_session_id), Some(evidence)) =
        (session.parent_id.as_deref(), task_child.as_ref())
    {
        if parent_provider_session_id != provider_session_id {
            push_opencode_fact(
                &mut provider_facts,
                "delegation.metadata",
                timestamp_from_ms(session.time_created),
                0,
                json!({
                    "provider_session_id": provider_session_id,
                    "parent_provider_session_id": parent_provider_session_id,
                    "metadata": evidence.metadata.clone(),
                }),
            );
        }
    }

    let result = ParseResult {
        events,
        source_lines,
        provider_facts,
        media_objects,
        last_good_offset: session_version.max(last_source_offset.saturating_add(1)),
        metadata: SessionMetadata {
            session_id: longhouse_session_id,
            provider_session_id: Some(provider_session_id.to_string()),
            forked_from_session_id: session.parent_id.clone(),
            lineage_kind,
            subagent_id: if task_child.is_some() {
                task_child_agent.clone()
            } else {
                None
            },
            subagent_tool_use_id: task_child
                .as_ref()
                .and_then(|evidence| evidence.tool_call_id.clone()),
            attribution_agent: if task_child.is_some() {
                task_child_agent
            } else {
                None
            },
            cwd: session.directory.clone(),
            project: project_label(session),
            environment: classification
                .as_ref()
                .and_then(opencode_session_environment_override_from_sidecar),
            origin_kind: classification
                .as_ref()
                .and_then(|sidecar| sidecar.origin_kind.clone()),
            launch_actor: classification
                .as_ref()
                .and_then(|sidecar| sidecar.launch_actor.clone()),
            launch_surface: classification
                .as_ref()
                .and_then(|sidecar| sidecar.launch_surface.clone()),
            hatch_run_id: classification
                .as_ref()
                .and_then(|sidecar| sidecar.hatch_run_id.clone()),
            parent_longhouse_session_id: classification
                .as_ref()
                .and_then(|sidecar| sidecar.parent_longhouse_session_id.clone()),
            parent_thread_id: classification
                .as_ref()
                .and_then(|sidecar| sidecar.parent_thread_id.clone()),
            parent_provider_session_id: classification
                .as_ref()
                .and_then(|sidecar| sidecar.parent_provider_session_id.clone()),
            version: session.version.clone(),
            started_at: Some(timestamp_from_ms(session.time_created)),
            is_sidechain: task_child.is_some(),
            ..Default::default()
        },
        candidate_records,
    };
    Ok(OpenCodeParse {
        result,
        line_ordinals,
    })
}

/// A row OpenCode has not written for this long is treated as finished even if
/// its content says it is still in flight: a crashed run leaves a `running`
/// tool call behind for good, and waiting longer for it costs nothing (the rest
/// of the session ships around it). The other side is a real tool call still
/// running after this long, which ships as it stands and replaces the epoch when
/// it completes: one replay, so the bound is long.
const IN_FLIGHT_STALE_MS: i64 = 2 * 60 * 60 * 1000;

/// OpenCode's placeholder project for a directory that is not a git checkout.
/// Its `worktree` is overwritten with the working directory of whichever
/// OpenCode process last ran outside a repository, so it is a fact about that
/// process and not about any session that points at the project.
const SHARED_PROJECT_ID: &str = "global";

const STREAM_REVISION_PREFIX: &str = "opencode-stream-v2:";

/// Parts at most this large are parsed to tell whether they are in flight.
const SMALL_PART_BYTES: usize = 16 * 1024;

fn hex32(bytes: &[u8; 32]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Everything one session has in the database, read in one transaction.
struct OpenCodeSessionRows {
    session: OpenCodeSessionRow,
    messages: Vec<OpenCodeMessageRow>,
    parts: Vec<OpenCodePartRow>,
}

fn load_session_rows(conn: &Connection, provider_session_id: &str) -> Result<OpenCodeSessionRows> {
    let mut session = load_session(conn, provider_session_id)?;
    session.agent = load_session_agent(conn, provider_session_id)?;
    Ok(OpenCodeSessionRows {
        session,
        messages: load_messages(conn, provider_session_id)?,
        parts: load_parts(conn, provider_session_id)?,
    })
}

/// The raw records one OpenCode session contributes to storage-v2, in the
/// order they are shipped, and what that order is checked against.
///
/// OpenCode keeps a session as mutable SQLite rows: a tool call is written
/// `running` and rewritten `completed`, an assistant message is rewritten with
/// its tokens when the turn ends, and a user message gets its diff summary once
/// the turn is done. A stream that hashed every row therefore changed on every
/// write, and every change replayed the whole session under a new epoch.
///
/// This stream is append-shaped instead:
///
/// * a row joins it once it has stopped changing (a tool call that is no longer
///   `pending` or `running`, a text or reasoning part that has an end time, an
///   assistant message that has a completion time; a user message always);
/// * rows are ordered by when they settled, so a row that settles later is
///   always at the tail;
/// * the session record leads the stream and only its title is checked: OpenCode
///   names a session after its first exchange, once, while it is small, and a
///   later rename is a person's edit. Its other fields (`agent`, which flips
///   with the mode, `version`, `time_updated`) are archived as of the first
///   envelope: the host reads none of them (its session facts are recomputed from
///   the rows for every envelope), and checking `agent` would replace the epoch
///   every time someone switches mode;
/// * a message is checked without its `summary`, which OpenCode recomputes after
///   the turn and which is derived from the parts.
///
/// The epoch's revision is a chain over the rows shipped so far. New rows extend
/// the chain and change nothing before them, so the epoch stands. A row that
/// changes after it settled, one that appears before the tail, or one that
/// vanishes breaks the chain, and only that rotates the epoch.
pub struct OpenCodeStream {
    session_id: String,
    /// The bytes shipped: record 0 is the session, the rest are settled rows.
    pub records: Vec<Vec<u8>>,
    /// What each record contributes to the revision chain.
    digests: Vec<[u8; 32]>,
    /// Record ordinal of each shipped part, by part id.
    part_ordinals: HashMap<String, u64>,
    /// When the earliest row still in flight will be treated as finished.
    next_wake_ms: Option<i64>,
    rows: OpenCodeSessionRows,
}

/// Events, facts and media of the shipped parts, and where each part's raw
/// record sits in the stream.
pub struct OpenCodeParse {
    pub result: ParseResult,
    /// The record ordinal of each entry of `result.source_lines`.
    pub line_ordinals: Vec<u64>,
}

enum Settling {
    /// Finished; the row sorts by this timestamp.
    Settled(i64),
    /// Still being written, and not yet old enough to give up on.
    InFlight { wake_ms: i64 },
}

fn settle_or_wait(in_flight: bool, time_updated: i64, now_ms: i64) -> Settling {
    if !in_flight {
        return Settling::Settled(time_updated);
    }
    let wake_ms = time_updated.saturating_add(IN_FLIGHT_STALE_MS);
    if now_ms >= wake_ms {
        Settling::Settled(time_updated)
    } else {
        Settling::InFlight { wake_ms }
    }
}

/// Whether a part is still being written. Unrecognised shapes are finished:
/// a part type or status this code has not heard of ships as it always has.
fn part_is_in_flight(data: &str) -> bool {
    // Nearly every byte is a long-finished tool output, and this runs on every
    // walk, so a large part is parsed only if it says it is running or pending.
    // A small one is always parsed: text and reasoning parts are small, and a
    // substring test for an end time is fooled by any other field of that name
    // and by any spacing in the JSON.
    if data.len() > SMALL_PART_BYTES
        && !data.contains("\"running\"")
        && !data.contains("\"pending\"")
        && !data.contains("\"type\":\"text\"")
        && !data.contains("\"type\":\"reasoning\"")
    {
        return false;
    }
    let Ok(part) = serde_json::from_str::<Value>(data) else {
        return false;
    };
    match part.get("type").and_then(Value::as_str) {
        Some("tool") => matches!(
            part.pointer("/state/status").and_then(Value::as_str),
            Some("pending" | "running")
        ),
        Some("text" | "reasoning") => {
            part.pointer("/time/start").is_some() && part.pointer("/time/end").is_none()
        }
        _ => false,
    }
}

fn message_settling(message: &OpenCodeMessageRow, now_ms: i64) -> Settling {
    let data: Option<Value> = serde_json::from_str(&message.data).ok();
    let role = data
        .as_ref()
        .and_then(|data| data.get("role"))
        .and_then(Value::as_str);
    if role == Some("user") {
        return Settling::Settled(message.time_created);
    }
    let time = data.as_ref().and_then(|data| data.get("time"));
    if let Some(completed) = time
        .and_then(|time| time.get("completed"))
        .and_then(Value::as_i64)
    {
        return Settling::Settled(completed);
    }
    let mid_turn = role == Some("assistant")
        && time
            .and_then(|time| time.get("created"))
            .is_some_and(|created| !created.is_null());
    settle_or_wait(mid_turn, message.time_updated, now_ms)
}

/// What a message contributes to the revision chain. `summary` is OpenCode's
/// per-turn diff, computed after the turn from the parts and rewritten in
/// place; a later `time_updated` with nothing else changed is not a change.
fn message_chain_digest(message: &OpenCodeMessageRow) -> [u8; 32] {
    let stable = match serde_json::from_str::<Value>(&message.data) {
        Ok(Value::Object(mut fields)) => {
            fields.remove("summary");
            Value::Object(fields).to_string()
        }
        _ => message.data.clone(),
    };
    let mut hasher = Sha256::new();
    hasher.update(message.id.as_bytes());
    hasher.update([0]);
    hasher.update(message.time_created.to_be_bytes());
    hasher.update(stable.as_bytes());
    hasher.finalize().into()
}

/// What the session record contributes to the revision chain: its title.
fn session_chain_digest(session: &OpenCodeSessionRow) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"title\0");
    hasher.update(session.title.as_deref().unwrap_or("").as_bytes());
    hasher.finalize().into()
}

fn session_record(session: &OpenCodeSessionRow, provider_session_id: &str) -> Result<Vec<u8>> {
    Ok(serde_json::to_vec(&json!({
        "kind": "session",
        "provider": "opencode",
        "provider_session_id": provider_session_id,
        "project_id": session.project_id,
        "parent_id": session.parent_id,
        "agent": session.agent,
        "project_worktree": session.project_worktree,
        "project_name": session.project_name,
        "directory": session.directory,
        "path": session.path,
        "title": session.title,
        "version": session.version,
        "time_created": session.time_created,
        "time_updated": session.time_updated,
    }))?)
}

/// The raw records of one session, as of `now_ms`.
pub fn opencode_session_stream(
    db_path: &Path,
    provider_session_id: &str,
    now_ms: i64,
) -> Result<OpenCodeStream> {
    let conn = open_readonly(db_path)?;
    // One read transaction, so a message and the parts written with it are
    // seen together or not at all.
    let rows = {
        let tx = conn.unchecked_transaction()?;
        load_session_rows(&tx, provider_session_id)?
    };
    build_stream(provider_session_id, rows, now_ms)
}

fn build_stream(
    provider_session_id: &str,
    rows: OpenCodeSessionRows,
    now_ms: i64,
) -> Result<OpenCodeStream> {
    // (settled at, messages before parts, id, index): a total order that never
    // reaches back once a row is in it.
    let mut settled: Vec<(i64, u8, &str, usize)> =
        Vec::with_capacity(rows.messages.len() + rows.parts.len());
    let mut next_wake_ms: Option<i64> = None;
    let mut wait_until = |wake_ms: i64| {
        next_wake_ms = Some(next_wake_ms.map_or(wake_ms, |known| known.min(wake_ms)));
    };
    for (index, message) in rows.messages.iter().enumerate() {
        match message_settling(message, now_ms) {
            Settling::Settled(at) => settled.push((at, 0, message.id.as_str(), index)),
            Settling::InFlight { wake_ms } => wait_until(wake_ms),
        }
    }
    for (index, part) in rows.parts.iter().enumerate() {
        match settle_or_wait(part_is_in_flight(&part.data), part.time_updated, now_ms) {
            Settling::Settled(at) => settled.push((at, 1, part.id.as_str(), index)),
            Settling::InFlight { wake_ms } => wait_until(wake_ms),
        }
    }
    settled.sort_unstable();

    let mut records = Vec::with_capacity(settled.len() + 1);
    let mut digests = Vec::with_capacity(settled.len() + 1);
    let mut part_ordinals = HashMap::new();
    records.push(session_record(&rows.session, provider_session_id)?);
    digests.push(session_chain_digest(&rows.session));
    for (_, kind, _, index) in settled {
        if kind == 0 {
            let message = &rows.messages[index];
            records.push(serde_json::to_vec(&json!({
                "kind": "message",
                "provider": "opencode",
                "provider_session_id": provider_session_id,
                "message_id": message.id,
                "message_time_created": message.time_created,
                "message_time_updated": message.time_updated,
                "message_data": message.data,
            }))?);
            digests.push(message_chain_digest(message));
        } else {
            let part = &rows.parts[index];
            let bytes = serde_json::to_vec(&json!({
                "kind": "part",
                "provider": "opencode",
                "provider_session_id": provider_session_id,
                "message_id": part.message_id,
                "part_id": part.id,
                "part_time_created": part.time_created,
                "part_time_updated": part.time_updated,
                "part_data": part.data,
            }))?;
            part_ordinals.insert(part.id.clone(), records.len() as u64);
            digests.push(Sha256::digest(&bytes).into());
            records.push(bytes);
        }
    }
    Ok(OpenCodeStream {
        session_id: provider_session_id.to_string(),
        records,
        digests,
        part_ordinals,
        next_wake_ms,
        rows,
    })
}

impl OpenCodeStream {
    /// When rows still in flight will be treated as finished, if any are.
    pub fn next_wake_ms(&self) -> Option<i64> {
        self.next_wake_ms
    }

    /// The revision of the whole stream as it stands.
    pub fn revision(&self) -> String {
        self.revision_through(self.records.len())
    }

    fn revision_through(&self, records: usize) -> String {
        format!(
            "{STREAM_REVISION_PREFIX}{records}:{}",
            hex32(&self.chain_through(records))
        )
    }

    /// A chain over the settled rows among the first `records` records.
    fn chain_through(&self, records: usize) -> [u8; 32] {
        let mut chain: [u8; 32] = Sha256::digest(STREAM_REVISION_PREFIX.as_bytes()).into();
        for digest in self.digests.iter().take(records) {
            let mut hasher = Sha256::new();
            hasher.update(chain);
            hasher.update(digest);
            chain = hasher.finalize().into();
        }
        chain
    }

    /// The revision to observe for an epoch that last vouched for `stored`.
    ///
    /// While every record `stored` vouches for is still what the stream holds
    /// at that place, the epoch stands, so `stored` comes back unchanged. When
    /// one was rewritten, moved or removed (or `stored` is not a stream
    /// revision at all, as with epochs from before this one), the answer is
    /// the current revision, which differs and so rotates the epoch.
    pub fn continuing_revision(&self, stored: Option<&str>) -> String {
        let vouched = stored
            .and_then(|stored| stored.strip_prefix(STREAM_REVISION_PREFIX))
            .and_then(|stored| stored.split_once(':'))
            .and_then(|(records, chain)| Some((records.parse::<usize>().ok()?, chain)))
            .is_some_and(|(records, chain)| {
                records <= self.records.len() && hex32(&self.chain_through(records)) == chain
            });
        match stored {
            Some(stored) if vouched => stored.to_string(),
            _ => self.revision(),
        }
    }
}

pub fn parse_opencode_stream(db_path: &Path, stream: &OpenCodeStream) -> Result<OpenCodeParse> {
    let conn = open_readonly(db_path)?;
    parse_rows(
        &conn,
        &stream.session_id,
        &stream.rows,
        Some(&stream.part_ordinals),
    )
}

#[cfg(test)]
thread_local! {
    /// Times an OpenCode database was opened for reading on this thread, for any
    /// reason: a scan pass over a database that did not move must not open it.
    pub(crate) static DATABASE_OPENS: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

fn open_readonly(path: &Path) -> Result<Connection> {
    #[cfg(test)]
    DATABASE_OPENS.with(|opens| opens.set(opens.get() + 1));
    let uri = sqlite_readonly_uri(path);
    let conn = Connection::open_with_flags(
        &uri,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .with_context(|| format!("opening OpenCode database {}", path.display()))?;
    conn.busy_timeout(Duration::from_secs(2))?;
    Ok(conn)
}

fn sqlite_readonly_uri(path: &Path) -> String {
    let path = path.to_string_lossy();
    let mut uri = String::from("file:");
    for byte in path.as_bytes() {
        match *byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'/' | b'.' | b'-' | b'_' => {
                uri.push(char::from(*byte));
            }
            _ => {
                uri.push_str(&format!("%{byte:02X}"));
            }
        }
    }
    uri.push_str("?mode=ro");
    uri
}

fn load_session(conn: &Connection, provider_session_id: &str) -> Result<OpenCodeSessionRow> {
    if sqlite_table_exists(conn, "project")? && sqlite_column_exists(conn, "session", "project_id")?
    {
        let mut session = conn
            .query_row(
                // Modern OpenCode DBs attach sessions to project.worktree through
                // project_id. The join tolerates missing project rows; older
                // schemas fall back to directory/path below.
                r#"
                SELECT s.project_id, s.parent_id, p.worktree, p.name, s.directory, s.path,
                       s.title, s.version, s.time_created, s.time_updated
                FROM session s
                LEFT JOIN project p ON p.id = s.project_id
                WHERE s.id = ?1
                "#,
                params![provider_session_id],
                |row| {
                    Ok(OpenCodeSessionRow {
                        project_id: row.get(0)?,
                        parent_id: row.get(1)?,
                        agent: None,
                        project_worktree: row.get(2)?,
                        project_name: row.get(3)?,
                        directory: row.get(4)?,
                        path: row.get(5)?,
                        title: row.get(6)?,
                        version: row.get(7)?,
                        time_created: row.get(8)?,
                        time_updated: row.get(9)?,
                    })
                },
            )
            .with_context(|| format!("loading OpenCode session {provider_session_id}"))?;
        if session.project_id.as_deref() == Some(SHARED_PROJECT_ID) {
            // Not this session's: see `SHARED_PROJECT_ID`.
            session.project_worktree = None;
            session.project_name = None;
        }
        return Ok(session);
    }

    conn.query_row(
        r#"
        SELECT parent_id, directory, path, title, version, time_created, time_updated
        FROM session
        WHERE id = ?1
        "#,
        params![provider_session_id],
        |row| {
            Ok(OpenCodeSessionRow {
                project_id: None,
                parent_id: row.get(0)?,
                agent: None,
                project_worktree: None,
                project_name: None,
                directory: row.get(1)?,
                path: row.get(2)?,
                title: row.get(3)?,
                version: row.get(4)?,
                time_created: row.get(5)?,
                time_updated: row.get(6)?,
            })
        },
    )
    .with_context(|| format!("loading legacy OpenCode session {provider_session_id}"))
}

fn load_session_agent(conn: &Connection, provider_session_id: &str) -> Result<Option<String>> {
    if !sqlite_column_exists(conn, "session", "agent")? {
        return Ok(None);
    }
    let agent = conn
        .query_row(
            "SELECT agent FROM session WHERE id = ?1",
            params![provider_session_id],
            |row| row.get::<_, Option<String>>(0),
        )
        .with_context(|| format!("loading OpenCode session agent {provider_session_id}"))?;
    Ok(agent
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string))
}

fn sqlite_table_exists(conn: &Connection, table: &str) -> Result<bool> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
        params![table],
        |row| row.get(0),
    )?;
    Ok(count > 0)
}

fn sqlite_column_exists(conn: &Connection, table: &str, column: &str) -> Result<bool> {
    let escaped_table = table.replace('"', "\"\"");
    let mut stmt = conn.prepare(&format!("PRAGMA table_info(\"{escaped_table}\")"))?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        if name == column {
            return Ok(true);
        }
    }
    Ok(false)
}

fn load_messages(conn: &Connection, provider_session_id: &str) -> Result<Vec<OpenCodeMessageRow>> {
    let mut stmt = conn.prepare(
        r#"
        SELECT id, time_created, time_updated, data
        FROM message
        WHERE session_id = ?1
        ORDER BY time_created ASC, id ASC
        "#,
    )?;
    let rows = stmt.query_map(params![provider_session_id], |row| {
        Ok(OpenCodeMessageRow {
            id: row.get(0)?,
            time_created: row.get(1)?,
            time_updated: row.get(2)?,
            data: row.get(3)?,
        })
    })?;
    let mut messages = Vec::new();
    for row in rows {
        messages.push(row?);
    }
    Ok(messages)
}

fn load_parts(conn: &Connection, provider_session_id: &str) -> Result<Vec<OpenCodePartRow>> {
    let mut stmt = conn.prepare(
        r#"
        SELECT id, message_id, time_created, time_updated, data
        FROM part
        WHERE session_id = ?1
        ORDER BY time_created ASC, id ASC
        "#,
    )?;
    let rows = stmt.query_map(params![provider_session_id], |row| {
        Ok(OpenCodePartRow {
            id: row.get(0)?,
            message_id: row.get(1)?,
            time_created: row.get(2)?,
            time_updated: row.get(3)?,
            data: row.get(4)?,
        })
    })?;
    let mut parts = Vec::new();
    for row in rows {
        parts.push(row?);
    }
    Ok(parts)
}

fn extract_events_from_part(
    provider_session_id: &str,
    longhouse_session_id: &str,
    message: &OpenCodeMessageRow,
    part: &OpenCodePartRow,
    part_data: &Value,
    role: &str,
    source_offset: u64,
    events: &mut Vec<ParsedEvent>,
) -> Result<()> {
    let part_type = part_data.get("type").and_then(Value::as_str).unwrap_or("");
    match part_type {
        "text" => {
            let text = part_data.get("text").and_then(Value::as_str).unwrap_or("");
            if text.trim().is_empty() {
                return Ok(());
            }
            events.push(ParsedEvent {
                uuid: stable_event_uuid(provider_session_id, &part.id, "text"),
                parent_uuid: None,
                session_id: longhouse_session_id.to_string(),
                timestamp: timestamp_from_ms(part.time_created.max(message.time_created)),
                role: event_role(role),
                content_text: Some(text.to_string()),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset,
                raw_type: format!("opencode_{part_type}"),
                raw_line: Some(part.data.clone()),
            });
        }
        "tool" => {
            let tool_name = part_data
                .get("tool")
                .and_then(Value::as_str)
                .unwrap_or("tool")
                .to_string();
            let call_id = part_data
                .get("callID")
                .and_then(Value::as_str)
                .map(str::to_string);
            let state = part_data.get("state").unwrap_or(&Value::Null);
            let input = state.get("input").and_then(raw_value_from_json);
            events.push(ParsedEvent {
                uuid: stable_event_uuid(provider_session_id, &part.id, "tool_call"),
                parent_uuid: None,
                session_id: longhouse_session_id.to_string(),
                timestamp: timestamp_from_ms(part.time_created.max(message.time_created)),
                role: Role::Assistant,
                content_text: None,
                tool_name: Some(tool_name),
                tool_input_json: input,
                tool_output_text: None,
                tool_call_id: call_id.clone(),
                source_offset,
                raw_type: "opencode_tool_call".to_string(),
                raw_line: Some(part.data.clone()),
            });
            if let Some(output) = tool_output_text(state) {
                events.push(ParsedEvent {
                    uuid: stable_event_uuid(provider_session_id, &part.id, "tool_result"),
                    parent_uuid: None,
                    session_id: longhouse_session_id.to_string(),
                    timestamp: timestamp_from_ms(part.time_updated.max(part.time_created)),
                    role: Role::Tool,
                    content_text: None,
                    tool_name: None,
                    tool_input_json: None,
                    tool_output_text: Some(output),
                    tool_call_id: call_id,
                    source_offset: source_offset.saturating_add(1),
                    raw_type: "opencode_tool_result".to_string(),
                    raw_line: None,
                });
            }
        }
        "file" => {
            if let Some(text) = file_part_text(part_data) {
                events.push(ParsedEvent {
                    uuid: stable_event_uuid(provider_session_id, &part.id, "file"),
                    parent_uuid: None,
                    session_id: longhouse_session_id.to_string(),
                    timestamp: timestamp_from_ms(part.time_created.max(message.time_created)),
                    role: event_role(role),
                    content_text: Some(text),
                    tool_name: None,
                    tool_input_json: None,
                    tool_output_text: None,
                    tool_call_id: None,
                    source_offset,
                    raw_type: "opencode_file".to_string(),
                    raw_line: None,
                });
            }
        }
        "patch" => {
            if let Some(text) = patch_part_text(part_data) {
                events.push(ParsedEvent {
                    uuid: stable_event_uuid(provider_session_id, &part.id, "patch"),
                    parent_uuid: None,
                    session_id: longhouse_session_id.to_string(),
                    timestamp: timestamp_from_ms(part.time_created.max(message.time_created)),
                    role: event_role(role),
                    content_text: Some(text),
                    tool_name: None,
                    tool_input_json: None,
                    tool_output_text: None,
                    tool_call_id: None,
                    source_offset,
                    raw_type: "opencode_patch".to_string(),
                    raw_line: None,
                });
            }
        }
        "reasoning" | "step-start" | "step-finish" => {}
        _ => {
            // OpenCode adds part types independently of the message schema.
            // Preserve a future textual part as conservative system context;
            // silently dropping it makes provider drift look like a clean
            // transcript, while guessing assistant/user would invent role.
            let text = part_data
                .get("text")
                .and_then(Value::as_str)
                .or_else(|| part_data.get("content").and_then(Value::as_str))
                .unwrap_or("");
            if text.trim().is_empty() {
                return Ok(());
            }
            events.push(ParsedEvent {
                uuid: stable_event_uuid(provider_session_id, &part.id, "unknown_text"),
                parent_uuid: None,
                session_id: longhouse_session_id.to_string(),
                timestamp: timestamp_from_ms(part.time_created.max(message.time_created)),
                role: Role::System,
                content_text: Some(text.to_string()),
                tool_name: None,
                tool_input_json: None,
                tool_output_text: None,
                tool_call_id: None,
                source_offset,
                raw_type: format!("opencode_unknown_{part_type}"),
                raw_line: Some(part.data.clone()),
            });
        }
    }
    Ok(())
}

fn event_role(role: &str) -> Role {
    match role {
        "user" => Role::User,
        "assistant" => Role::Assistant,
        "system" | "developer" => Role::System,
        "tool" => Role::Tool,
        _ => Role::System,
    }
}

fn file_part_text(part_data: &Value) -> Option<String> {
    let label = part_data
        .pointer("/source/text/value")
        .and_then(Value::as_str)
        .or_else(|| part_data.get("filename").and_then(Value::as_str))
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("file");
    let filename = part_data
        .get("filename")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let mime = part_data
        .get("mime")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty());

    let mut details = Vec::new();
    if let Some(filename) = filename {
        if filename != label {
            details.push(filename.to_string());
        }
    }
    if let Some(mime) = mime {
        details.push(mime.to_string());
    }

    if details.is_empty() {
        Some(format!("Attached file: {label}"))
    } else {
        Some(format!("Attached file: {label} ({})", details.join(", ")))
    }
}

fn patch_part_text(part_data: &Value) -> Option<String> {
    let files: Vec<String> = part_data
        .get("files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .collect();
    if files.is_empty() {
        return None;
    }
    let shown: Vec<String> = files.iter().take(8).cloned().collect();
    let suffix = files
        .len()
        .checked_sub(shown.len())
        .filter(|remaining| *remaining > 0)
        .map(|remaining| format!(", and {remaining} more"))
        .unwrap_or_default();
    Some(format!("Patch: {}{}", shown.join(", "), suffix))
}

fn parsed_media_objects_from_redactions(
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

fn source_line_part_data(part_data: &Value) -> (Value, Vec<InlineImageRedaction>) {
    let mut value = part_data.clone();
    if value.get("type").and_then(Value::as_str) != Some("file") {
        return (value, Vec::new());
    }
    let Some(object) = value.as_object_mut() else {
        return (value, Vec::new());
    };
    let Some(url) = object
        .get("url")
        .and_then(Value::as_str)
        .map(str::to_string)
    else {
        return (value, Vec::new());
    };
    if let Some(redaction) = redact_inline_image_data_url(&url) {
        object.insert(
            "url".to_string(),
            Value::String(redaction.placeholder.clone()),
        );
        object.insert("url_truncated".to_string(), Value::Bool(true));
        object.insert(
            "url_original_chars".to_string(),
            Value::Number(serde_json::Number::from(redaction.original_chars as u64)),
        );
        object.insert(
            "url_media_sha256".to_string(),
            Value::String(redaction.sha256.clone()),
        );
        object.insert(
            "url_media_bytes".to_string(),
            Value::Number(serde_json::Number::from(redaction.byte_size as u64)),
        );
        object.insert(
            "url_media_mime_type".to_string(),
            Value::String(redaction.mime_type.clone()),
        );
        return (value, vec![redaction]);
    }

    if url.len() <= MAX_SOURCE_FILE_URL_CHARS && !url.starts_with("data:") {
        return (value, Vec::new());
    }

    let mut preview = url
        .chars()
        .take(MAX_SOURCE_FILE_URL_CHARS)
        .collect::<String>();
    preview.push_str("...[truncated]");
    object.insert("url".to_string(), Value::String(preview));
    object.insert("url_truncated".to_string(), Value::Bool(true));
    object.insert(
        "url_original_chars".to_string(),
        Value::Number(serde_json::Number::from(url.len() as u64)),
    );
    (value, Vec::new())
}

fn project_label(session: &OpenCodeSessionRow) -> Option<String> {
    // Basename-derived candidates go through the same generic-container refusal
    // the transcript parser uses. `project_name` is OpenCode's own explicit
    // name and is trusted as-is. Without this, an OpenCode session run in
    // `.../provider-live-evidence/workspace` files itself under a project
    // literally called `workspace`, which the parser path already refuses.
    use crate::pipeline::parser::is_generic_workspace_label;
    fn project_basename(value: &str) -> Option<String> {
        let label = path_basename(value)?;
        if is_generic_workspace_label(label) {
            return None;
        }
        Some(label.to_string())
    }

    session
        .project_worktree
        .as_deref()
        .filter(|value| value.trim() != "/")
        .and_then(project_basename)
        .or_else(|| {
            session
                .project_name
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
        })
        .or_else(|| session.directory.as_deref().and_then(project_basename))
        .or_else(|| session.path.as_deref().and_then(project_basename))
        .or_else(|| session.title.clone())
}

fn path_basename(path: &str) -> Option<&str> {
    Path::new(path.trim())
        .file_name()
        .and_then(|name| name.to_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

fn opencode_session_classification_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(root) = std::env::var_os("LONGHOUSE_OPENCODE_SESSION_METADATA_ROOT") {
        roots.push(PathBuf::from(root));
    }
    if let Ok(home) = get_longhouse_home() {
        roots.push(home.join("provider-session-metadata").join("opencode"));
        roots.push(
            home.join("provider-live-proof")
                .join("sessions")
                .join("opencode"),
        );
    }
    roots
}

#[cfg(test)]
fn opencode_session_environment_override_from_roots(
    provider_session_id: &str,
    roots: &[PathBuf],
) -> Option<String> {
    opencode_session_classification_sidecar_from_roots(provider_session_id, roots)
        .as_ref()
        .and_then(opencode_session_environment_override_from_sidecar)
}

fn opencode_session_environment_override_from_sidecar(
    sidecar: &OpenCodeSessionClassificationSidecar,
) -> Option<String> {
    let environment = sidecar.environment.as_deref()?.trim();
    if matches!(environment, "test" | "e2e") {
        return Some(environment.to_string());
    }
    None
}

fn opencode_session_classification_sidecar(
    provider_session_id: &str,
) -> Option<OpenCodeSessionClassificationSidecar> {
    opencode_session_classification_sidecar_from_roots(
        provider_session_id,
        &opencode_session_classification_roots(),
    )
}

fn opencode_session_classification_sidecar_from_roots(
    provider_session_id: &str,
    roots: &[PathBuf],
) -> Option<OpenCodeSessionClassificationSidecar> {
    for root in roots {
        let path = root.join(format!("{provider_session_id}.json"));
        let Ok(text) = fs::read_to_string(&path) else {
            continue;
        };
        let Ok(sidecar) = serde_json::from_str::<OpenCodeSessionClassificationSidecar>(&text)
        else {
            continue;
        };
        if sidecar.provider.as_deref() != Some("opencode") {
            continue;
        }
        if sidecar.provider_session_id.as_deref() != Some(provider_session_id) {
            continue;
        }
        return Some(sidecar);
    }
    None
}

fn tool_output_text(state: &Value) -> Option<String> {
    for key in ["output", "error"] {
        if let Some(value) = state.get(key) {
            if let Some(text) = value.as_str() {
                if !text.trim().is_empty() {
                    return Some(text.to_string());
                }
            } else if !value.is_null() {
                return Some(value.to_string());
            }
        }
    }
    None
}

fn raw_value_from_json(value: &Value) -> Option<Box<RawValue>> {
    if value.is_null() {
        return None;
    }
    RawValue::from_string(serde_json::to_string(value).ok()?).ok()
}

#[derive(Debug, Clone)]
struct OpenCodeTaskSpawnEvidence {
    child_provider_session_id: String,
    tool_call_id: Option<String>,
    metadata: Value,
}
#[derive(Debug, Clone)]
struct OpenCodeTaskActivityEvidence {
    child_provider_session_id: String,
    tool_call_id: Option<String>,
    kind: String,
    metadata: Value,
    occurred_at_ms: Option<i64>,
}

fn opencode_task_output_child_id(output: &str) -> Option<&str> {
    let start = output.find("<task id=\"")? + "<task id=\"".len();
    let rest = &output[start..];
    let end = rest.find('"')?;
    let id = rest[..end].trim();
    (!id.is_empty()).then_some(id)
}

fn opencode_task_spawn_evidence(part_data: &Value) -> Option<OpenCodeTaskSpawnEvidence> {
    if part_data.get("type").and_then(Value::as_str) != Some("tool")
        || part_data.get("tool").and_then(Value::as_str) != Some("task")
    {
        return None;
    }
    let state = part_data.get("state").unwrap_or(&Value::Null);
    let metadata_value = state
        .get("metadata")
        .or_else(|| part_data.get("metadata"))
        .unwrap_or(&Value::Null);
    let input = state.get("input").unwrap_or(&Value::Null);
    let child_provider_session_id =
        string_field(metadata_value, &["sessionId", "sessionID", "session_id"])
            .or_else(|| {
                state
                    .get("output")
                    .and_then(Value::as_str)
                    .and_then(opencode_task_output_child_id)
            })?
            .to_string();
    let tool_call_id = part_data
        .get("callID")
        .and_then(Value::as_str)
        .or_else(|| part_data.get("callId").and_then(Value::as_str))
        .filter(|call_id| !call_id.trim().is_empty())
        .map(str::to_string);
    let mut metadata = metadata_value.as_object().cloned().unwrap_or_default();
    // The agent selector is native input when OpenCode does not repeat it in
    // state.metadata. Preserve the provider spelling rather than normalizing it.
    if !metadata.contains_key("agent") {
        for key in ["subagent_type", "subagentType", "agent"] {
            if let Some(value) = input.get(key) {
                metadata.insert(key.to_string(), value.clone());
                break;
            }
        }
    }
    if let Some(call_id) = tool_call_id.as_deref() {
        metadata
            .entry("callID".to_string())
            .or_insert_with(|| Value::from(call_id.to_string()));
    }
    Some(OpenCodeTaskSpawnEvidence {
        child_provider_session_id,
        tool_call_id,
        metadata: Value::Object(metadata),
    })
}
fn opencode_task_activity_evidence(part_data: &Value) -> Option<OpenCodeTaskActivityEvidence> {
    let spawn = opencode_task_spawn_evidence(part_data)?;
    let state = part_data.get("state").unwrap_or(&Value::Null);
    let kind = string_field(state, &["status"])?;
    let mut metadata = spawn.metadata.as_object().cloned().unwrap_or_default();
    metadata.insert("status".to_string(), Value::from(kind.to_string()));
    if let Some(time) = state.get("time") {
        metadata.insert("time".to_string(), time.clone());
    }
    for key in [
        "progress",
        "tokens",
        "requests",
        "toolCount",
        "currentTool",
        "error",
        "failure",
    ] {
        if let Some(value) = state.get(key) {
            metadata.insert(key.to_string(), value.clone());
        }
    }
    let terminal = matches!(
        kind,
        "completed" | "failed" | "error" | "cancelled" | "canceled"
    );
    let time_key = if terminal { "end" } else { "start" };
    let occurred_at_ms = state
        .get("time")
        .and_then(Value::as_object)
        .and_then(|time| time.get(time_key))
        .and_then(|value| {
            value
                .as_i64()
                .or_else(|| value.as_u64().and_then(|value| i64::try_from(value).ok()))
        });
    Some(OpenCodeTaskActivityEvidence {
        child_provider_session_id: spawn.child_provider_session_id,
        tool_call_id: spawn.tool_call_id,
        kind: kind.to_string(),
        metadata: Value::Object(metadata),
        occurred_at_ms,
    })
}

fn opencode_task_child_evidence(
    conn: &Connection,
    parent_provider_session_id: &str,
    child_provider_session_id: &str,
) -> Result<Option<OpenCodeTaskChildEvidence>> {
    if parent_provider_session_id.trim().is_empty()
        || child_provider_session_id.trim().is_empty()
        || parent_provider_session_id == child_provider_session_id
    {
        return Ok(None);
    }
    let parts = load_parts(conn, parent_provider_session_id)?;
    for part in parts {
        let part_data: Value = serde_json::from_str(&part.data)
            .with_context(|| format!("parsing OpenCode parent task part {}", part.id))?;
        let Some(evidence) = opencode_task_spawn_evidence(&part_data) else {
            continue;
        };
        let state = part_data.get("state").unwrap_or(&Value::Null);
        let metadata_parent = evidence
            .metadata
            .get("parentSessionId")
            .or_else(|| evidence.metadata.get("parent_session_id"))
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty());
        if metadata_parent.is_some_and(|value| value != parent_provider_session_id) {
            continue;
        }
        let child_matches = evidence.child_provider_session_id == child_provider_session_id
            || state
                .get("output")
                .and_then(Value::as_str)
                .and_then(opencode_task_output_child_id)
                == Some(child_provider_session_id);
        if !child_matches {
            continue;
        }
        let agent = string_field(
            &evidence.metadata,
            &["agent", "subagent_type", "subagentType"],
        )
        .or_else(|| {
            state
                .get("input")
                .and_then(|input| string_field(input, &["subagent_type", "subagentType", "agent"]))
        })
        .map(str::to_string);
        return Ok(Some(OpenCodeTaskChildEvidence {
            agent,
            tool_call_id: evidence.tool_call_id,
            metadata: evidence.metadata,
        }));
    }
    Ok(None)
}

fn opencode_lineage_kind(session: &OpenCodeSessionRow, is_task_child: bool) -> Option<String> {
    if is_task_child {
        return Some("task_child".to_string());
    }
    session.parent_id.as_ref()?;
    if session
        .title
        .as_deref()
        .map(str::to_lowercase)
        .is_some_and(|title| title.contains("fork #"))
    {
        return Some("fork".to_string());
    }
    Some("unknown".to_string())
}

fn string_field<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a str> {
    for key in keys {
        if let Some(found) = value
            .get(*key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|found| !found.is_empty())
        {
            return Some(found);
        }
    }
    None
}

fn stable_event_uuid(provider_session_id: &str, part_id: &str, suffix: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("opencode:{provider_session_id}:{part_id}:{suffix}").as_bytes(),
    )
    .to_string()
}

fn session_fingerprint(
    conn: &Connection,
    provider_session_id: &str,
    has_agent_column: bool,
) -> Result<String> {
    let mut hash = Fnv1a64::default();
    hash.update(provider_session_id.as_bytes());

    let mut session_stmt = conn.prepare(
        r#"
        SELECT id, COALESCE(parent_id, ''), COALESCE(directory, ''), COALESCE(path, ''),
               COALESCE(title, ''), COALESCE(version, ''), time_created, time_updated
        FROM session
        WHERE id = ?1
        "#,
    )?;
    session_stmt.query_row(params![provider_session_id], |row| {
        for index in 0..6 {
            let value: String = row.get(index)?;
            hash.update_field(&value);
        }
        let time_created: i64 = row.get(6)?;
        let time_updated: i64 = row.get(7)?;
        hash.update_i64(time_created);
        hash.update_i64(time_updated);
        Ok::<(), rusqlite::Error>(())
    })?;
    if has_agent_column {
        let agent: Option<String> = conn.query_row(
            "SELECT agent FROM session WHERE id = ?1",
            params![provider_session_id],
            |row| row.get(0),
        )?;
        hash.update_field(agent.as_deref().unwrap_or(""));
    }

    let mut message_stmt = conn.prepare(
        r#"
        SELECT id, time_created, time_updated, data
        FROM message
        WHERE session_id = ?1
        ORDER BY id ASC
        "#,
    )?;
    let messages = message_stmt.query_map(params![provider_session_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
        ))
    })?;
    for row in messages {
        let (id, time_created, time_updated, data) = row?;
        hash.update_field(&id);
        hash.update_i64(time_created);
        hash.update_i64(time_updated);
        hash.update_field(&data);
    }

    let mut part_stmt = conn.prepare(
        r#"
        SELECT id, message_id, time_created, time_updated, data
        FROM part
        WHERE session_id = ?1
        ORDER BY id ASC
        "#,
    )?;
    let parts = part_stmt.query_map(params![provider_session_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, String>(4)?,
        ))
    })?;
    for row in parts {
        let (id, message_id, time_created, time_updated, data) = row?;
        hash.update_field(&id);
        hash.update_field(&message_id);
        hash.update_i64(time_created);
        hash.update_i64(time_updated);
        hash.update_field(&data);
    }

    Ok(format!("{:016x}", hash.finish()))
}

fn opencode_session_version(conn: &Connection, provider_session_id: &str) -> Result<Option<u64>> {
    let version_ms = conn.query_row(
        r#"
            SELECT MAX(
                s.time_updated,
                COALESCE((SELECT MAX(m.time_updated) FROM message m WHERE m.session_id = s.id), 0),
                COALESCE((SELECT MAX(p.time_updated) FROM part p WHERE p.session_id = s.id), 0)
            )
            FROM session s
            WHERE s.id = ?1
            "#,
        params![provider_session_id],
        |row| row.get::<_, Option<i64>>(0),
    )?;
    Ok(version_ms.map(version_from_ms))
}

#[derive(Debug)]
struct Fnv1a64(u64);

impl Default for Fnv1a64 {
    fn default() -> Self {
        Self(0xcbf29ce484222325)
    }
}

impl Fnv1a64 {
    fn update(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.0 ^= u64::from(*byte);
            self.0 = self.0.wrapping_mul(0x100000001b3);
        }
    }

    fn update_field(&mut self, value: &str) {
        self.update(&(value.len() as u64).to_le_bytes());
        self.update(value.as_bytes());
    }

    fn update_i64(&mut self, value: i64) {
        self.update(&value.to_le_bytes());
    }

    fn finish(&self) -> u64 {
        self.0
    }
}

fn source_offset_for_part(part: &OpenCodePartRow, part_index: usize) -> u64 {
    let base = part.time_created.max(0) as u64;
    base.saturating_mul(SOURCE_OFFSET_SCALE).saturating_add(
        (part_index as u64)
            .min((SOURCE_OFFSET_SCALE - 2) / 2)
            .saturating_mul(2),
    )
}

fn version_from_ms(ms: i64) -> u64 {
    (ms.max(0) as u64)
        .saturating_mul(SOURCE_OFFSET_SCALE)
        .saturating_add(SOURCE_OFFSET_SCALE - 1)
}

fn timestamp_from_ms(ms: i64) -> DateTime<Utc> {
    Utc.timestamp_millis_opt(ms)
        .single()
        .unwrap_or(DateTime::UNIX_EPOCH)
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use super::*;
    use base64::{engine::general_purpose, Engine as _};

    // Poison-tolerant on purpose: every mutation under this lock is made through an
    // RAII guard whose Drop restores the previous value, and Drop runs while unwinding.
    // So a panicking test leaves the environment clean, and the poison flag carries no
    // information -- it only converts one real failure into a wall of PoisonError noise
    // from every other test that shares the lock.

    struct EnvGuard {
        key: &'static str,
        previous: Option<OsString>,
    }

    impl EnvGuard {
        fn set(key: &'static str, value: &str) -> Self {
            let previous = std::env::var_os(key);
            std::env::set_var(key, value);
            Self { key, previous }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            if let Some(previous) = self.previous.as_ref() {
                std::env::set_var(self.key, previous);
            } else {
                std::env::remove_var(self.key);
            }
        }
    }

    fn create_fixture_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE session (
                id text PRIMARY KEY,
                project_id text NOT NULL,
                parent_id text,
                directory text,
                path text,
                title text,
                version text,
                time_created integer NOT NULL,
                time_updated integer NOT NULL
            );
            CREATE TABLE project (
                id text PRIMARY KEY,
                worktree text NOT NULL,
                name text
            );
            CREATE TABLE message (
                id text PRIMARY KEY,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );
            CREATE TABLE part (
                id text PRIMARY KEY,
                message_id text NOT NULL,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );
            "#,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO project (id, worktree, name) VALUES (?1, ?2, NULL)",
            params!["proj_longhouse", "/Users/davidrose/git/zerg/longhouse"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO session (id, project_id, parent_id, directory, path, title, version, time_created, time_updated)
             VALUES (?1, ?2, NULL, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                "ses_test",
                "proj_longhouse",
                "/Users/davidrose/git/zerg/longhouse",
                "Users/davidrose/git/zerg/longhouse",
                "Longhouse work",
                "1.15.7",
                1_779_000_000_000_i64,
                1_779_000_001_000_i64,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                "msg_user",
                "ses_test",
                1_779_000_000_010_i64,
                1_779_000_000_020_i64,
                r#"{"role":"user"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_user",
                "msg_user",
                "ses_test",
                1_779_000_000_011_i64,
                1_779_000_000_011_i64,
                r#"{"type":"text","text":"hello OpenCode"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                "msg_assistant",
                "ses_test",
                1_779_000_000_100_i64,
                1_779_000_001_000_i64,
                r#"{"role":"assistant"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_tool",
                "msg_assistant",
                "ses_test",
                1_779_000_000_110_i64,
                1_779_000_000_190_i64,
                r#"{"type":"tool","tool":"bash","callID":"call_1","state":{"status":"completed","input":{"command":"pwd"},"output":"/tmp\n"}}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_text",
                "msg_assistant",
                "ses_test",
                1_779_000_000_200_i64,
                1_779_000_000_300_i64,
                r#"{"type":"text","text":"done"}"#,
            ],
        )
        .unwrap();
    }

    fn stream_now_ms() -> i64 {
        chrono::Utc::now().timestamp_millis()
    }

    fn record_kinds_and_ids(stream: &OpenCodeStream) -> Vec<(String, String)> {
        stream
            .records
            .iter()
            .map(|record| {
                let value: Value = serde_json::from_slice(record).unwrap();
                let id = value
                    .get("part_id")
                    .or_else(|| value.get("message_id"))
                    .or_else(|| value.get("provider_session_id"))
                    .and_then(Value::as_str)
                    .unwrap()
                    .to_string();
                (value["kind"].as_str().unwrap().to_string(), id)
            })
            .collect()
    }

    #[test]
    fn stream_preserves_exact_database_strings_in_stable_record_order() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_fixture_db(&db_path);
        let first = opencode_session_stream(&db_path, "ses_test", stream_now_ms()).unwrap();
        let second = opencode_session_stream(&db_path, "ses_test", stream_now_ms()).unwrap();
        assert_eq!(first.revision(), second.revision());
        assert_eq!(first.records, second.records);
        assert!(String::from_utf8(first.records[0].clone())
            .unwrap()
            .contains("\"kind\":\"session\""));
        assert!(first.records.iter().skip(1).any(|record| {
            String::from_utf8_lossy(record)
                .contains("{\\\"type\\\":\\\"text\\\",\\\"text\\\":\\\"hello OpenCode\\\"}")
        }));
    }

    #[test]
    fn stream_leads_with_the_session_and_orders_rows_by_when_they_settled() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_fixture_db(&db_path);
        let stream = opencode_session_stream(&db_path, "ses_test", stream_now_ms()).unwrap();
        // A user message settles when it is written; a tool call or answer when
        // its last write lands; an assistant message with no completion time to
        // read is taken at its last write.
        let order: Vec<String> = record_kinds_and_ids(&stream)
            .into_iter()
            .map(|(kind, id)| format!("{kind}:{id}"))
            .collect();
        assert_eq!(
            order,
            [
                "session:ses_test",
                "message:msg_user",
                "part:prt_user",
                "part:prt_tool",
                "part:prt_text",
                "message:msg_assistant",
            ]
        );
    }

    #[test]
    fn the_revision_chain_survives_appends_and_notices_rewrites() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_fixture_db(&db_path);
        let now = stream_now_ms();
        let before = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        let stored = before.revision();
        assert_eq!(before.continuing_revision(Some(&stored)), stored);
        assert_eq!(before.continuing_revision(None), stored);

        let provider = Connection::open(&db_path).unwrap();
        // Nobody owns these: the session row's write time and OpenCode version,
        // the project row, and a message's diff summary or write time.
        provider
            .execute(
                "UPDATE session SET version = '9.9', time_updated = time_updated + 5",
                [],
            )
            .unwrap();
        provider
            .execute("UPDATE project SET worktree = '/elsewhere'", [])
            .unwrap();
        provider
            .execute(
                "UPDATE message SET time_updated = time_updated + 5, data = '{\"role\":\"user\",\"summary\":{\"diffs\":[]}}' WHERE id = 'msg_user'",
                [],
            )
            .unwrap();
        let touched = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        assert_eq!(touched.revision(), stored);

        // A row that settles after the others is an append: the old chain still
        // holds, and the epoch may now vouch for one record more.
        provider
            .execute(
                "INSERT INTO part VALUES ('prt_late', 'msg_assistant', 'ses_test', 1779000002000, 1779000002000, '{\"type\":\"text\",\"text\":\"later\"}')",
                [],
            )
            .unwrap();
        let appended = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        assert_eq!(appended.records.len(), before.records.len() + 1);
        assert_eq!(appended.continuing_revision(Some(&stored)), stored);
        assert_ne!(appended.revision(), stored);
        let extended = appended.revision();
        assert_eq!(appended.continuing_revision(Some(&extended)), extended);

        // A settled row that changes ends it, and so does one that vanishes or
        // one that turns up before the end of the stream.
        provider
            .execute(
                "UPDATE part SET data = '{\"type\":\"text\",\"text\":\"done!\"}', time_updated = 1779000000301 WHERE id = 'prt_text'",
                [],
            )
            .unwrap();
        let rewritten = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        assert_eq!(
            rewritten.continuing_revision(Some(&extended)),
            rewritten.revision()
        );
        assert_ne!(rewritten.revision(), extended);

        provider
            .execute("DELETE FROM part WHERE id = 'prt_late'", [])
            .unwrap();
        let shrunk = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        assert_eq!(
            shrunk.continuing_revision(Some(&extended)),
            shrunk.revision()
        );

        // An epoch from before stream revisions vouches for nothing.
        assert_eq!(
            before.continuing_revision(Some(&"a".repeat(64))),
            before.revision()
        );
    }

    #[test]
    fn a_renamed_session_ends_its_epoch() {
        // OpenCode names a session after its first exchange, and the session
        // record is the only place the archive keeps the name.
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_fixture_db(&db_path);
        let now = stream_now_ms();
        let before = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        let stored = before.revision();
        Connection::open(&db_path)
            .unwrap()
            .execute("UPDATE session SET title = 'Fix the shipper'", [])
            .unwrap();
        let after = opencode_session_stream(&db_path, "ses_test", now).unwrap();
        assert_eq!(after.records.len(), before.records.len());
        assert_ne!(after.continuing_revision(Some(&stored)), stored);
        assert_eq!(after.continuing_revision(Some(&stored)), after.revision());
    }

    #[test]
    fn a_shared_project_is_not_a_fact_about_its_sessions() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_fixture_db(&db_path);
        let provider = Connection::open(&db_path).unwrap();
        provider
            .execute("INSERT INTO project (id, worktree, name) VALUES ('global', '/private/tmp/agents/oc-research/ws', NULL)", [])
            .unwrap();
        provider
            .execute("UPDATE session SET project_id = 'global'", [])
            .unwrap();

        let stream = opencode_session_stream(&db_path, "ses_test", stream_now_ms()).unwrap();
        let session: Value = serde_json::from_slice(&stream.records[0]).unwrap();
        assert_eq!(session["project_id"], "global");
        assert!(session["project_worktree"].is_null());
        assert!(session["project_name"].is_null());
        // The label comes from where the session ran, not from whichever
        // process last touched the shared project.
        let parsed = parse_opencode_session(&db_path, "ses_test").unwrap();
        assert_eq!(parsed.metadata.project.as_deref(), Some("longhouse"));

        // A real repository's project is the session's own.
        provider
            .execute("UPDATE session SET project_id = 'proj_longhouse'", [])
            .unwrap();
        let stream = opencode_session_stream(&db_path, "ses_test", stream_now_ms()).unwrap();
        let session: Value = serde_json::from_slice(&stream.records[0]).unwrap();
        assert_eq!(
            session["project_worktree"],
            "/Users/davidrose/git/zerg/longhouse"
        );
    }

    #[test]
    fn rows_still_being_written_are_recognised_and_given_up_on_late() {
        let now = 1_800_000_000_000_i64;
        let in_flight = [
            r#"{"type":"tool","tool":"bash","state":{"status":"running","input":{}}}"#,
            r#"{"type":"tool","tool":"bash","state":{"status":"pending","input":{}}}"#,
            r#"{"type":"text","text":"","time":{"start":5}}"#,
            r#"{"type":"reasoning","text":"","time":{"start":5}}"#,
            // Spacing in the JSON does not change what it says.
            r#"{"type": "text", "text": "", "time": {"start": 5}}"#,
            // Another field named "end" is not the part's end time.
            r#"{"type":"text","text":"x","time":{"start":5},"other":{"end":1}}"#,
        ];
        let finished = [
            r#"{"type":"tool","tool":"bash","state":{"status":"completed","output":"still running"}}"#,
            r#"{"type":"tool","tool":"bash","state":{"status":"error"}}"#,
            r#"{"type":"text","text":"x","time":{"start":5,"end":6}}"#,
            r#"{"type":"text","text":"typed by a person"}"#,
            r#"{"type":"step-start"}"#,
            r#"{"type":"something-new","state":{"status":"running"}}"#,
            r#"not json but says "running""#,
        ];
        for data in in_flight {
            assert!(part_is_in_flight(data), "{data}");
        }
        for data in finished {
            assert!(!part_is_in_flight(data), "{data}");
        }

        let message = |data: &str, updated: i64| OpenCodeMessageRow {
            id: "msg".to_string(),
            time_created: 100,
            time_updated: updated,
            data: data.to_string(),
        };
        let settled = |settling: Settling| match settling {
            Settling::Settled(at) => Some(at),
            Settling::InFlight { .. } => None,
        };
        // A user message is finished when it is written; its diff summary comes
        // later and is not a reason to wait.
        assert_eq!(
            settled(message_settling(&message(r#"{"role":"user"}"#, now), now)),
            Some(100)
        );
        // An assistant message settles when the turn completes.
        let completed = r#"{"role":"assistant","time":{"created":110,"completed":190}}"#;
        assert_eq!(
            settled(message_settling(&message(completed, now), now)),
            Some(190)
        );
        // Mid-turn it waits, until the row is old enough to be given up on.
        let mid_turn = r#"{"role":"assistant","time":{"created":110}}"#;
        assert_eq!(
            settled(message_settling(&message(mid_turn, now - 1_000), now)),
            None
        );
        assert_eq!(
            settled(message_settling(
                &message(mid_turn, now - IN_FLIGHT_STALE_MS),
                now
            )),
            Some(now - IN_FLIGHT_STALE_MS)
        );
        // A shape this code has not seen ships as it always has.
        assert_eq!(
            settled(message_settling(
                &message(r#"{"role":"assistant"}"#, 777),
                now
            )),
            Some(777)
        );
    }

    #[test]
    fn parse_opencode_session_projects_sqlite_rows_into_events() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();

        assert_eq!(
            result.metadata.provider_session_id.as_deref(),
            Some("ses_test")
        );
        assert_eq!(
            result.metadata.cwd.as_deref(),
            Some("/Users/davidrose/git/zerg/longhouse")
        );
        assert_eq!(result.metadata.project.as_deref(), Some("longhouse"));
        assert_eq!(result.events.len(), 4);
        assert_eq!(result.events[0].role, Role::User);
        assert_eq!(
            result.events[0].content_text.as_deref(),
            Some("hello OpenCode")
        );
        assert_eq!(result.events[1].tool_name.as_deref(), Some("bash"));
        assert_eq!(result.events[1].tool_call_id.as_deref(), Some("call_1"));
        assert_eq!(
            result.events[1]
                .tool_input_json
                .as_ref()
                .map(|raw| raw.get()),
            Some(r#"{"command":"pwd"}"#)
        );
        assert_eq!(result.events[2].role, Role::Tool);
        assert_eq!(result.events[2].tool_output_text.as_deref(), Some("/tmp\n"));
        assert_eq!(result.events[3].content_text.as_deref(), Some("done"));
        assert_eq!(result.source_lines.len(), 3);
        assert!(result.last_good_offset > result.events[3].source_offset);
    }

    #[test]
    fn parse_opencode_preserves_system_and_unknown_textual_parts() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);

        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                "msg_system",
                "ses_test",
                1_779_000_000_050_i64,
                1_779_000_000_050_i64,
                r#"{"role":"system"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_system",
                "msg_system",
                "ses_test",
                1_779_000_000_051_i64,
                1_779_000_000_051_i64,
                r#"{"type":"text","text":"provider system context"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                "msg_future",
                "ses_test",
                1_779_000_000_350_i64,
                1_779_000_000_350_i64,
                r#"{"role":"future-role"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_future",
                "msg_future",
                "ses_test",
                1_779_000_000_351_i64,
                1_779_000_000_351_i64,
                r#"{"type":"future_text","text":"future provider context"}"#,
            ],
        )
        .unwrap();
        drop(conn);

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();
        assert!(result.events.iter().any(|event| {
            event.role == Role::System
                && event.content_text.as_deref() == Some("provider system context")
        }));
        assert!(result.events.iter().any(|event| {
            event.role == Role::System
                && event.content_text.as_deref() == Some("future provider context")
                && event.raw_type == "opencode_unknown_future_text"
        }));
    }

    #[test]
    fn project_label_prefers_worktree_over_generic_opencode_path() {
        let session = OpenCodeSessionRow {
            project_id: None,
            parent_id: None,
            agent: None,
            project_worktree: Some("/Users/davidrose/git/zerg/longhouse".to_string()),
            project_name: None,
            directory: Some("/Users/davidrose/git/zerg/longhouse".to_string()),
            path: Some("/private/tmp/opencode/workspace".to_string()),
            title: Some("OpenCode work".to_string()),
            version: None,
            time_created: 1_779_000_000_000_i64,
            time_updated: 1_779_000_000_000_i64,
        };

        assert_eq!(project_label(&session).as_deref(), Some("longhouse"));
    }

    #[test]
    fn project_label_refuses_generic_container_basenames() {
        // The real corpus shape: a provider-live canary run whose every path
        // ends in `workspace`. Filing 921 of these under a project literally
        // called "workspace" is what this refusal prevents.
        let canary = OpenCodeSessionRow {
            project_id: None,
            parent_id: None,
            agent: None,
            project_worktree: Some(
                "/Users/davidrose/.longhouse/canaries/provider-live/opencode/workspace".to_string(),
            ),
            project_name: None,
            directory: Some(
                "/Users/davidrose/.longhouse/canaries/provider-live/opencode/workspace".to_string(),
            ),
            path: Some("/private/tmp/provider-live-evidence/workspace".to_string()),
            title: None,
            version: None,
            time_created: 1_779_000_000_000_i64,
            time_updated: 1_779_000_000_000_i64,
        };
        assert_eq!(project_label(&canary), None);

        // An explicit OpenCode project name is authoritative and survives even
        // when every basename around it is generic.
        let named = OpenCodeSessionRow {
            project_name: Some("g55".to_string()),
            ..canary.clone()
        };
        assert_eq!(project_label(&named).as_deref(), Some("g55"));
    }

    #[test]
    fn project_label_prefers_worktree_over_project_name() {
        let session = OpenCodeSessionRow {
            project_id: None,
            parent_id: None,
            agent: None,
            project_worktree: Some("/Users/davidrose/git/sauron/jobs".to_string()),
            project_name: Some("sauron".to_string()),
            directory: Some("/Users/davidrose/git/sauron/jobs".to_string()),
            path: Some("/private/tmp/opencode/workspace".to_string()),
            title: Some("OpenCode work".to_string()),
            version: None,
            time_created: 1_779_000_000_000_i64,
            time_updated: 1_779_000_000_000_i64,
        };

        assert_eq!(project_label(&session).as_deref(), Some("jobs"));
    }

    #[test]
    fn load_session_supports_legacy_schema_without_project_table() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE session (
                id text PRIMARY KEY,
                parent_id text,
                directory text,
                path text,
                title text,
                version text,
                time_created integer NOT NULL,
                time_updated integer NOT NULL
            );
            "#,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO session (id, parent_id, directory, path, title, version, time_created, time_updated)
             VALUES (?1, NULL, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                "ses_legacy",
                "/tmp/opencode-work",
                "tmp/opencode-work",
                "Legacy OpenCode",
                "1.15.7",
                1_779_000_000_000_i64,
                1_779_000_001_000_i64,
            ],
        )
        .unwrap();

        let session = load_session(&conn, "ses_legacy").unwrap();

        assert_eq!(session.project_worktree, None);
        assert_eq!(session.directory.as_deref(), Some("/tmp/opencode-work"));
        assert_eq!(project_label(&session).as_deref(), Some("opencode-work"));
    }

    #[test]
    fn opencode_session_environment_override_uses_provider_live_sidecar() {
        let temp = tempfile::tempdir().unwrap();
        let sidecar_root = temp.path().join("sidecars");
        fs::create_dir_all(&sidecar_root).unwrap();
        fs::write(
            sidecar_root.join("ses_test.json"),
            json!({
                "artifact_kind": "provider_live_session_classification",
                "provider": "opencode",
                "provider_session_id": "ses_test",
                "environment": "test"
            })
            .to_string(),
        )
        .unwrap();

        let environment =
            opencode_session_environment_override_from_roots("ses_test", &[sidecar_root]);

        assert_eq!(environment.as_deref(), Some("test"));
    }

    #[test]
    fn opencode_session_classification_sidecar_carries_hatch_origin() {
        let temp = tempfile::tempdir().unwrap();
        let sidecar_root = temp.path().join("sidecars");
        fs::create_dir_all(&sidecar_root).unwrap();
        fs::write(
            sidecar_root.join("ses_test.json"),
            json!({
                "provider": "opencode",
                "provider_session_id": "ses_test",
                "origin_kind": "hatch_automation",
                "launch_actor": "automation",
                "launch_surface": "hatch",
                "hatch_run_id": "hatch-run-1",
                "parent_longhouse_session_id": "11111111-1111-4111-8111-111111111111",
                "parent_thread_id": "22222222-2222-4222-8222-222222222222",
                "parent_provider_session_id": "ses_parent"
            })
            .to_string(),
        )
        .unwrap();

        let sidecar =
            opencode_session_classification_sidecar_from_roots("ses_test", &[sidecar_root])
                .unwrap();

        assert_eq!(sidecar.origin_kind.as_deref(), Some("hatch_automation"));
        assert_eq!(sidecar.launch_actor.as_deref(), Some("automation"));
        assert_eq!(sidecar.launch_surface.as_deref(), Some("hatch"));
        assert_eq!(sidecar.hatch_run_id.as_deref(), Some("hatch-run-1"));
        assert_eq!(
            sidecar.parent_longhouse_session_id.as_deref(),
            Some("11111111-1111-4111-8111-111111111111")
        );
        assert_eq!(
            sidecar.parent_thread_id.as_deref(),
            Some("22222222-2222-4222-8222-222222222222")
        );
        assert_eq!(
            sidecar.parent_provider_session_id.as_deref(),
            Some("ses_parent")
        );
    }

    #[test]
    fn parse_opencode_session_reads_hatch_origin_sidecar_from_metadata_root() {
        let _lock = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let sidecar_root = temp.path().join("sidecars");
        fs::create_dir_all(&sidecar_root).unwrap();
        let _root = EnvGuard::set(
            "LONGHOUSE_OPENCODE_SESSION_METADATA_ROOT",
            sidecar_root.to_str().unwrap(),
        );
        fs::write(
            sidecar_root.join("ses_test.json"),
            json!({
                "provider": "opencode",
                "provider_session_id": "ses_test",
                "origin_kind": "hatch_automation",
                "launch_actor": "automation",
                "launch_surface": "hatch",
                "hatch_run_id": "hatch-run-1",
                "parent_longhouse_session_id": "11111111-1111-4111-8111-111111111111",
                "parent_thread_id": "22222222-2222-4222-8222-222222222222",
                "parent_provider_session_id": "ses_parent"
            })
            .to_string(),
        )
        .unwrap();

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();

        assert_eq!(
            result.metadata.origin_kind.as_deref(),
            Some("hatch_automation")
        );
        assert_eq!(result.metadata.hatch_run_id.as_deref(), Some("hatch-run-1"));
        assert_eq!(result.metadata.launch_actor.as_deref(), Some("automation"));
        assert_eq!(result.metadata.launch_surface.as_deref(), Some("hatch"));
        assert_eq!(
            result.metadata.parent_longhouse_session_id.as_deref(),
            Some("11111111-1111-4111-8111-111111111111")
        );
        assert_eq!(
            result.metadata.parent_thread_id.as_deref(),
            Some("22222222-2222-4222-8222-222222222222")
        );
        assert_eq!(
            result.metadata.parent_provider_session_id.as_deref(),
            Some("ses_parent")
        );
    }

    #[test]
    fn opencode_session_environment_override_rejects_mismatched_sidecar() {
        let temp = tempfile::tempdir().unwrap();
        let sidecar_root = temp.path().join("sidecars");
        fs::create_dir_all(&sidecar_root).unwrap();
        fs::write(
            sidecar_root.join("ses_test.json"),
            json!({
                "provider": "opencode",
                "provider_session_id": "other_session",
                "environment": "test"
            })
            .to_string(),
        )
        .unwrap();

        let environment =
            opencode_session_environment_override_from_roots("ses_test", &[sidecar_root]);

        assert_eq!(environment, None);
    }

    #[test]
    fn parse_opencode_session_projects_file_and_patch_parts() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                "msg_file",
                "ses_test",
                1_779_000_000_400_i64,
                1_779_000_000_400_i64,
                r#"{"role":"user"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_file",
                "msg_file",
                "ses_test",
                1_779_000_000_401_i64,
                1_779_000_000_401_i64,
                json!({
                    "type": "file",
                    "mime": "image/png",
                    "filename": "clipboard",
                    "url": format!("data:image/png;base64,{}", "A".repeat(900)),
                    "source": {
                        "type": "file",
                        "path": "clipboard",
                        "text": {"value": "[Image 1]", "start": 0, "end": 9}
                    }
                })
                .to_string(),
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_patch",
                "msg_assistant",
                "ses_test",
                1_779_000_000_500_i64,
                1_779_000_000_500_i64,
                json!({
                    "type": "patch",
                    "hash": "abc123",
                    "files": ["/tmp/a.txt", "/tmp/b.txt"]
                })
                .to_string(),
            ],
        )
        .unwrap();

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();
        let visible_text: Vec<&str> = result
            .events
            .iter()
            .filter_map(|event| event.content_text.as_deref())
            .collect();

        assert!(visible_text.contains(&"Attached file: [Image 1] (clipboard, image/png)"));
        assert!(visible_text.contains(&"Patch: /tmp/a.txt, /tmp/b.txt"));
        let file_source_line = result
            .source_lines
            .iter()
            .find(|line| line.raw_line.contains("\"part_id\":\"prt_file\""))
            .unwrap();
        assert!(file_source_line.raw_line.contains("\"url_truncated\":true"));
        assert!(file_source_line
            .raw_line
            .contains("\"url_original_chars\":922"));
        assert!(!file_source_line.raw_line.contains(&"A".repeat(900)));
        assert_eq!(result.media_objects.len(), 1);
        assert_eq!(
            result.media_objects[0].source_offset,
            file_source_line.source_offset
        );
        assert_eq!(result.media_objects[0].mime_type, "image/png");
        assert_eq!(result.media_objects[0].byte_size, 675);
        assert_eq!(result.media_objects[0].original_chars, 922);
        assert_eq!(result.media_objects[0].bytes, vec![0u8; 675]);
    }

    #[test]
    fn parse_opencode_large_inline_image_source_line_is_redacted() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let image_bytes = vec![42u8; 1024 * 1024];
        let image_data = general_purpose::STANDARD.encode(&image_bytes);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_large_file",
                "msg_user",
                "ses_test",
                1_779_000_000_401_i64,
                1_779_000_000_401_i64,
                json!({
                    "type": "file",
                    "mime": "image/png",
                    "filename": "large-screenshot",
                    "url": format!("data:image/png;base64,{image_data}"),
                    "source": {
                        "type": "file",
                        "path": "large-screenshot",
                        "text": {"value": "[Image 2]", "start": 0, "end": 9}
                    }
                })
                .to_string(),
            ],
        )
        .unwrap();

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();
        let file_source_line = result
            .source_lines
            .iter()
            .find(|line| line.raw_line.contains("\"part_id\":\"prt_large_file\""))
            .unwrap();

        assert!(file_source_line
            .raw_line
            .contains("longhouse_media_ref:sha256="));
        assert!(file_source_line.raw_line.contains("\"url_truncated\":true"));
        assert!(!file_source_line.raw_line.contains(&image_data));
        assert!(
            file_source_line.raw_line.len() < 2_000,
            "redacted OpenCode source line should stay small, got {} bytes",
            file_source_line.raw_line.len()
        );
        let media = result
            .media_objects
            .iter()
            .find(|media| media.source_offset == file_source_line.source_offset)
            .unwrap();
        assert_eq!(media.mime_type, "image/png");
        assert_eq!(media.byte_size, image_bytes.len());
        assert_eq!(media.bytes, image_bytes);
    }

    #[test]
    fn open_readonly_reads_wal_database_with_writer_present() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("open code.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.pragma_update(None, "journal_mode", "WAL").unwrap();
        conn.execute_batch(
            "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT);
             INSERT INTO sample (value) VALUES ('committed');",
        )
        .unwrap();

        let writer = Connection::open(&db_path).unwrap();
        writer
            .execute_batch("BEGIN IMMEDIATE; INSERT INTO sample (value) VALUES ('pending');")
            .unwrap();

        let readonly = open_readonly(&db_path).unwrap();
        let count: i64 = readonly
            .query_row("SELECT COUNT(*) FROM sample", [], |row| row.get(0))
            .unwrap();

        assert_eq!(count, 1);
    }

    #[test]
    fn list_opencode_sessions_returns_synthetic_source_keys() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);

        let sessions = list_opencode_sessions(&db_path).unwrap();

        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].provider_session_id, "ses_test");
        assert!(sessions[0].source_key.ends_with("#opencode:ses_test"));
        assert!(sessions[0].version > 0);
    }

    /// The scan walks the DB session by session and reads each session's records
    /// itself; hashing every message and part of the whole page first was a second
    /// read of the corpus that nothing looked at.
    #[test]
    fn the_session_page_a_scan_walks_does_not_hash_lifetime_content() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);

        let page = list_opencode_sessions_page(&db_path, 64, 0).unwrap();

        assert_eq!(page.len(), 1);
        assert_eq!(page[0].provider_session_id, "ses_test");
        assert!(page[0].fingerprint.is_empty());
    }

    /// One database holds every session the machine ever ran, so the scope is
    /// applied per session, by the session's own creation time and folder.
    #[test]
    fn opencode_sessions_are_gated_one_by_one() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        // The fixture's session was created in May 2026 in the Longhouse folder.
        create_fixture_db(&db_path);
        let now_ms = chrono::Utc::now().timestamp_millis();
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO session (id, project_id, parent_id, directory, path, title, version, time_created, time_updated)
             VALUES ('ses_new', 'proj_longhouse', NULL, '/work/new', NULL, 'new', '1', ?1, ?1)",
            params![now_ms + 60_000],
        )
        .unwrap();
        drop(conn);
        let sessions = list_opencode_sessions_page(&db_path, 64, 0).unwrap();
        let since = chrono::Utc::now();
        let scope = crate::import_scope::ImportScope::starting(since, "cli");
        let admitted: Vec<&str> = sessions
            .iter()
            .filter(|session| session.in_import_scope(&scope))
            .map(|session| session.provider_session_id.as_str())
            .collect();
        assert_eq!(admitted, vec!["ses_new"]);

        // A project opts the old session back in by the folder it ran in.
        let with_project = crate::import_scope::ImportScope {
            projects: vec![std::path::PathBuf::from("/Users/davidrose/git/zerg")],
            ..scope.clone()
        };
        let mut admitted: Vec<&str> = sessions
            .iter()
            .filter(|session| session.in_import_scope(&with_project))
            .map(|session| session.provider_session_id.as_str())
            .collect();
        admitted.sort();
        assert_eq!(admitted, vec!["ses_new", "ses_test"]);

        // Nothing restricted: both.
        let all = crate::import_scope::ImportScope::all("cli");
        assert!(sessions.iter().all(|session| session.in_import_scope(&all)));

        // A row with no creation time is not known to be new.
        let undated = OpenCodeSessionCandidate {
            created_ms: None,
            directory: None,
            ..sessions[0].clone()
        };
        assert!(!undated.in_import_scope(&scope));
    }

    #[test]
    fn session_watermarks_do_not_hash_lifetime_content() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let fingerprints =
            opencode_session_fingerprints(&db_path, &["ses_test".to_string()]).unwrap();
        assert!(!fingerprints[0].is_empty());

        let before = list_opencode_session_watermarks(&db_path).unwrap();
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "UPDATE part
             SET time_updated = (SELECT MAX(time_updated) FROM session) + 1
             WHERE id = 'prt_text'",
            [],
        )
        .unwrap();
        drop(conn);

        let sessions = list_opencode_session_watermarks(&db_path).unwrap();

        assert_eq!(sessions.len(), 1);
        assert_eq!(sessions[0].provider_session_id, "ses_test");
        assert!(sessions[0].version > before[0].version);
        assert!(sessions[0].fingerprint.is_empty());
    }

    #[test]
    fn opencode_session_fingerprint_includes_agent_column_when_present() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute("ALTER TABLE session ADD COLUMN agent text", [])
            .unwrap();
        conn.execute(
            "UPDATE session SET agent = ?1 WHERE id = 'ses_test'",
            ["build"],
        )
        .unwrap();

        let before = session_fingerprint(&conn, "ses_test", true).unwrap();
        conn.execute(
            "UPDATE session SET agent = ?1 WHERE id = 'ses_test'",
            ["explore"],
        )
        .unwrap();
        let after = session_fingerprint(&conn, "ses_test", true).unwrap();

        assert_ne!(before, after);
    }

    #[test]
    fn parse_opencode_session_keeps_native_parent_provider_id() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "UPDATE session SET parent_id = ?1 WHERE id = 'ses_test'",
            ["ses_parent"],
        )
        .unwrap();

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();

        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some("ses_parent")
        );
        assert_eq!(result.metadata.lineage_kind.as_deref(), Some("unknown"));
        assert!(!result.metadata.is_sidechain);
        assert_eq!(result.metadata.subagent_id, None);
    }

    #[test]
    fn parse_opencode_session_marks_title_fork_lineage() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "UPDATE session SET parent_id = ?1, title = ?2 WHERE id = 'ses_test'",
            ["ses_parent", "Parent OpenCode work (fork #1)"],
        )
        .unwrap();

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();

        assert_eq!(result.metadata.lineage_kind.as_deref(), Some("fork"));
        assert!(!result.metadata.is_sidechain);
    }

    #[test]
    fn parse_opencode_session_marks_task_child_sidechain_from_parent_tool_metadata() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_fixture_db(&db_path);
        let conn = Connection::open(&db_path).unwrap();
        conn.execute("ALTER TABLE session ADD COLUMN agent text", [])
            .unwrap();
        conn.execute(
            "UPDATE session SET parent_id = ?1, agent = ?2 WHERE id = 'ses_test'",
            params!["ses_parent", "explore"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO session (id, project_id, parent_id, directory, path, title, version, time_created, time_updated, agent)
             VALUES (?1, ?2, NULL, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                "ses_parent",
                "proj_longhouse",
                "/Users/davidrose/git/zerg/longhouse",
                "Users/davidrose/git/zerg/longhouse",
                "Parent OpenCode work",
                "1.15.7",
                1_779_000_000_000_i64,
                1_779_000_001_000_i64,
                "build",
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                "msg_parent_task",
                "ses_parent",
                1_779_000_000_050_i64,
                1_779_000_000_060_i64,
                r#"{"role":"assistant"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                "prt_parent_task",
                "msg_parent_task",
                "ses_parent",
                1_779_000_000_051_i64,
                1_779_000_000_061_i64,
                json!({
                    "type": "tool",
                    "callID": "call_task",
                    "tool": "task",
                    "state": {
                        "status": "completed",
                        "input": {
                            "prompt": "inspect parser",
                            "description": "Inspect parser",
                            "subagent_type": "explore"
                        },
                        "title": "Inspect parser",
                        "metadata": {
                            "parentSessionId": "ses_parent",
                            "sessionId": "ses_test",
                            "background": true,
                            "jobId": "ses_test"
                        },
                        "output": "<task id=\"ses_test\" state=\"completed\">done</task>",
                        "time": {"start": 1_779_000_000_051_i64, "end": 1_779_000_000_061_i64}
                    }
                })
                .to_string(),
            ],
        )
        .unwrap();

        let result = parse_opencode_session(&db_path, "ses_test").unwrap();

        assert!(result.metadata.is_sidechain);
        assert_eq!(result.metadata.lineage_kind.as_deref(), Some("task_child"));
        assert_eq!(
            result.metadata.forked_from_session_id.as_deref(),
            Some("ses_parent")
        );
        assert_eq!(result.metadata.subagent_id.as_deref(), Some("explore"));
        assert_eq!(
            result.metadata.attribution_agent.as_deref(),
            Some("explore")
        );
        assert_eq!(
            result.metadata.subagent_tool_use_id.as_deref(),
            Some("call_task")
        );
        let parent_result = parse_opencode_session(&db_path, "ses_parent").unwrap();
        let spawn_fact = parent_result
            .provider_facts
            .iter()
            .find(|fact| fact.kind == "delegation.spawn")
            .expect("parent task evidence emits a spawn fact");
        assert_eq!(
            spawn_fact
                .payload
                .get("children")
                .and_then(Value::as_array)
                .and_then(|children| children.first())
                .and_then(|child| child.get("provider_session_id"))
                .and_then(Value::as_str),
            Some("ses_test")
        );
        assert_eq!(
            spawn_fact
                .payload
                .get("children")
                .and_then(Value::as_array)
                .and_then(|children| children.first())
                .and_then(|child| child.get("metadata"))
                .and_then(|metadata| metadata.get("background"))
                .and_then(Value::as_bool),
            Some(true)
        );
        let activity_fact = parent_result
            .provider_facts
            .iter()
            .find(|fact| fact.kind == "delegation.activity")
            .expect("parent task status emits an activity fact");
        assert_eq!(activity_fact.payload["provider_session_id"], "ses_test");
        assert_eq!(activity_fact.payload["kind"], "completed");
        assert_eq!(activity_fact.payload["event_id"], "prt_parent_task");
        assert_eq!(activity_fact.payload["parent_tool_call_id"], "call_task");
        assert_eq!(
            activity_fact.payload["occurred_at_ms"],
            1_779_000_000_061_i64
        );
        assert_eq!(activity_fact.payload["metadata"]["status"], "completed");
        assert_eq!(
            activity_fact.payload["metadata"]["time"]["end"],
            1_779_000_000_061_i64
        );
        assert_eq!(activity_fact.payload["metadata"]["background"], true);
        assert_eq!(activity_fact.payload["metadata"]["jobId"], "ses_test");
    }
    #[test]
    fn opencode_task_activity_preserves_failure_and_rejects_reusable_job_id() {
        let failed = json!({
            "type": "tool",
            "tool": "task",
            "callID": "call_failed",
            "state": {
                "status": "failed",
                "metadata": {
                    "parentSessionId": "ses_parent",
                    "sessionId": "ses_child",
                    "background": true,
                    "jobId": "bg_1"
                },
                "time": {
                    "start": 1_779_000_000_100_i64,
                    "end": 1_779_000_000_250_i64
                },
                "progress": {"completed": 3, "total": 9},
                "error": {"code": "provider_failed"}
            }
        });
        let evidence =
            opencode_task_activity_evidence(&failed).expect("native child status is linked");
        assert_eq!(evidence.child_provider_session_id, "ses_child");
        assert_eq!(evidence.kind, "failed");
        assert_eq!(evidence.tool_call_id.as_deref(), Some("call_failed"));
        assert_eq!(evidence.occurred_at_ms, Some(1_779_000_000_250_i64));
        assert_eq!(evidence.metadata["status"], "failed");
        assert_eq!(evidence.metadata["jobId"], "bg_1");
        assert_eq!(evidence.metadata["progress"]["completed"], 3);
        assert_eq!(evidence.metadata["error"]["code"], "provider_failed");

        // OpenCode reuses bg_N slots. A job handle without sessionId or a
        // provider task output id is not a child identity and emits nothing.
        let unlinked = json!({
            "type": "tool",
            "tool": "task",
            "callID": "call_unlinked",
            "state": {
                "status": "running",
                "metadata": {
                    "parentSessionId": "ses_parent",
                    "background": true,
                    "jobId": "bg_1"
                },
                "time": {"start": 1_779_000_000_300_i64}
            }
        });
        assert!(opencode_task_activity_evidence(&unlinked).is_none());
        assert!(opencode_task_spawn_evidence(&unlinked).is_none());
    }

    #[test]
    fn blank_native_call_id_does_not_create_a_tool_edge() {
        let part = json!({
            "type": "tool",
            "tool": "task",
            "callID": "  ",
            "state": {
                "status": "completed",
                "metadata": {"sessionId": "ses_child"},
                "output": "<task id=\"ses_child\" state=\"completed\">done</task>"
            }
        });
        let evidence = opencode_task_spawn_evidence(&part).expect("child session evidence");
        assert_eq!(evidence.tool_call_id, None);
        assert!(!evidence
            .metadata
            .as_object()
            .unwrap()
            .contains_key("callID"));
    }

    #[test]
    fn self_parent_provider_id_is_not_task_lineage() {
        let parent = "ses_test";
        let child = "ses_test";
        assert!(opencode_task_child_evidence(
            &Connection::open_in_memory().unwrap(),
            parent,
            child
        )
        .unwrap()
        .is_none());
    }

    #[test]
    fn mismatched_parent_claim_cannot_cross_opencode_scope() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE part (
                id text PRIMARY KEY,
                message_id text NOT NULL,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );",
        )
        .unwrap();
        let task = json!({
            "type": "tool",
            "tool": "task",
            "callID": "call-cross-scope",
            "state": {
                "status": "completed",
                "metadata": {
                    "parentSessionId": "ses_other_parent",
                    "sessionId": "ses_child"
                },
                "output": "<task id=\"ses_child\" state=\"completed\">done</task>"
            }
        });
        conn.execute(
            "INSERT INTO part
             (id, message_id, session_id, time_created, time_updated, data)
             VALUES ('part-cross-scope', 'message-cross-scope', 'ses_parent',
                     1, 2, ?1)",
            [task.to_string()],
        )
        .unwrap();

        assert!(
            opencode_task_child_evidence(&conn, "ses_parent", "ses_child")
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn output_only_child_id_survives_late_metadata_arrival() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE part (
                id text PRIMARY KEY,
                message_id text NOT NULL,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );",
        )
        .unwrap();
        let task = json!({
            "type": "tool",
            "tool": "task",
            "callID": "call-late-child",
            "state": {
                "status": "completed",
                "metadata": {},
                "output": "<task id=\"ses_late_child\" state=\"completed\">done</task>"
            }
        });
        conn.execute(
            "INSERT INTO part
             (id, message_id, session_id, time_created, time_updated, data)
             VALUES ('part-late-child', 'message-late-child', 'ses_parent',
                     1, 2, ?1)",
            [task.to_string()],
        )
        .unwrap();

        let evidence = opencode_task_child_evidence(&conn, "ses_parent", "ses_late_child")
            .unwrap()
            .expect("completed output identifies the child");
        assert_eq!(evidence.tool_call_id.as_deref(), Some("call-late-child"));
    }

    #[test]
    fn source_offsets_leave_room_for_tool_result_events() {
        let first = OpenCodePartRow {
            id: "prt_1".to_string(),
            message_id: "msg_1".to_string(),
            time_created: 1_779_000_000_110_i64,
            time_updated: 1_779_000_000_190_i64,
            data: "{}".to_string(),
        };
        let second = OpenCodePartRow {
            id: "prt_2".to_string(),
            message_id: "msg_1".to_string(),
            time_created: 1_779_000_000_110_i64,
            time_updated: 1_779_000_000_191_i64,
            data: "{}".to_string(),
        };

        let first_offset = source_offset_for_part(&first, 0);
        let first_result_offset = first_offset + 1;
        let second_offset = source_offset_for_part(&second, 1);

        assert!(first_result_offset < second_offset);
    }

    /// A scan asks about every session in the database, and each ask used to
    /// read and parse every managed-state file on the machine.
    #[test]
    fn asking_about_many_sessions_reads_the_managed_state_once() {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("managed-local").join("opencode");
        std::fs::create_dir_all(&state_root).unwrap();
        let state_file = state_root.join("state.json");
        let write_state = |native_id: &str| {
            std::fs::write(
                &state_file,
                serde_json::json!({
                    "schema_version": 1,
                    "provider": "opencode",
                    "longhouse_session_id": "11111111-1111-4111-8111-111111111111",
                    "opencode_session_id": native_id,
                })
                .to_string(),
            )
            .unwrap();
            std::fs::OpenOptions::new()
                .write(true)
                .open(&state_file)
                .unwrap()
                .set_modified(std::time::SystemTime::now() - std::time::Duration::from_secs(60))
                .unwrap();
        };
        write_state("ses_native");

        let before = crate::dir_cache::FILES_PARSED.with(|parsed| parsed.get());
        for index in 0..5 {
            assert_eq!(
                managed_longhouse_session_id_for_opencode_from_roots(
                    &format!("ses_other_{index}"),
                    &[state_root.clone()]
                ),
                None
            );
        }
        assert!(managed_longhouse_session_id_for_opencode_from_roots(
            "ses_native",
            &[state_root.clone()]
        )
        .is_some());
        assert_eq!(
            crate::dir_cache::FILES_PARSED.with(|parsed| parsed.get()) - before,
            1,
            "the managed state was re-read"
        );

        // A state file that now names a different session is read again.
        write_state("ses_renamed");
        assert!(managed_longhouse_session_id_for_opencode_from_roots(
            "ses_native",
            &[state_root.clone()]
        )
        .is_none());
        assert!(
            managed_longhouse_session_id_for_opencode_from_roots("ses_renamed", &[state_root])
                .is_some()
        );
    }

    #[test]
    fn managed_state_maps_native_opencode_id_to_longhouse_session_id() {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("managed-local").join("opencode");
        std::fs::create_dir_all(&state_root).unwrap();
        let longhouse_session_id = "11111111-1111-4111-8111-111111111111";
        std::fs::write(
            state_root.join("11111111-1111-4111-8111-111111111111.state.json"),
            serde_json::json!({
                "schema_version": 1,
                "provider": "opencode",
                "longhouse_session_id": longhouse_session_id,
                "opencode_session_id": "ses_native",
                "phase": "idle"
            })
            .to_string(),
        )
        .unwrap();

        assert_eq!(
            managed_longhouse_session_id_for_opencode_from_roots("ses_native", &[state_root])
                .as_deref(),
            Some(longhouse_session_id)
        );
    }

    #[test]
    fn managed_server_state_maps_provider_session_id_to_longhouse_session_id() {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("managed-local").join("opencode-server");
        std::fs::create_dir_all(&state_root).unwrap();
        let longhouse_session_id = "22222222-2222-4222-8222-222222222222";
        std::fs::write(
            state_root.join("22222222-2222-4222-8222-222222222222.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": longhouse_session_id,
                "provider_session_id": "ses_native_server",
                "previous_provider_session_ids": ["ses_before_reset"],
                "server_url": "http://127.0.0.1:12345",
                "pid": 12345,
                "cwd": "/tmp/project",
                "started_at": "2026-06-23T12:00:00Z",
                "updated_at": "2026-06-23T12:00:01Z"
            })
            .to_string(),
        )
        .unwrap();

        assert_eq!(
            managed_longhouse_session_id_for_opencode_from_roots(
                "ses_native_server",
                std::slice::from_ref(&state_root),
            )
            .as_deref(),
            Some(longhouse_session_id)
        );
        assert_eq!(
            managed_longhouse_session_id_for_opencode_from_roots(
                "ses_before_reset",
                std::slice::from_ref(&state_root),
            )
            .as_deref(),
            Some(longhouse_session_id)
        );
    }

    #[test]
    fn fresh_top_level_session_waits_for_matching_managed_server_event() {
        let temp = tempfile::tempdir().unwrap();
        let state_root = temp.path().join("states");
        let workspace = temp.path().join("workspace");
        std::fs::create_dir_all(&state_root).unwrap();
        std::fs::create_dir_all(&workspace).unwrap();
        std::fs::write(
            state_root.join("managed.json"),
            serde_json::json!({
                "session_id": "22222222-2222-4222-8222-222222222222",
                "provider_session_id": "ses_before_reset",
                "pid": std::process::id(),
                "cwd": workspace,
            })
            .to_string(),
        )
        .unwrap();
        let now = Utc::now();
        let metadata = SessionMetadata {
            provider_session_id: Some("ses_after_reset".to_string()),
            cwd: Some(workspace.to_string_lossy().into_owned()),
            started_at: Some(now),
            ..Default::default()
        };

        assert!(managed_binding_may_be_pending_from_roots(
            &metadata,
            std::slice::from_ref(&state_root),
            now
        ));
        let old = SessionMetadata {
            started_at: Some(now - chrono::Duration::seconds(6)),
            ..metadata
        };
        assert!(!managed_binding_may_be_pending_from_roots(
            &old,
            std::slice::from_ref(&state_root),
            now
        ));
    }
}
