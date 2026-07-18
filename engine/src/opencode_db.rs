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
    redact_inline_image_data_url, redact_inline_image_data_urls_with_media, InlineImageRedaction,
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
}

#[derive(Debug, Clone)]
pub struct OpenCodeRawSnapshot {
    pub source_revision: String,
    pub records: Vec<Vec<u8>>,
    pub part_record_start: u64,
}

#[derive(Debug)]
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

pub fn pending_console_binding_blocks_shadow(provider_session_id: &str) -> bool {
    let Ok(registry) = crate::turn_claims::default_registry() else {
        return false;
    };
    let Ok(claims) = registry.list_nonterminal() else {
        return false;
    };
    pending_console_binding_blocks_shadow_from_claims(provider_session_id, &claims)
}

fn pending_console_binding_blocks_shadow_from_claims(
    provider_session_id: &str,
    claims: &[crate::turn_claims::TurnClaim],
) -> bool {
    claims.iter().any(|claim| {
        claim.provider == "opencode"
            && claim.adapter.as_deref() == Some(crate::opencode_run::OPENCODE_RUN_ADAPTER)
            && claim.state == "spawned"
            && claim
                .provider_thread_id
                .as_deref()
                .map_or(true, |expected| expected == provider_session_id)
    })
}

pub fn list_opencode_sessions(db_path: &Path) -> Result<Vec<OpenCodeSessionCandidate>> {
    list_opencode_sessions_inner(db_path, None)
}

pub fn list_opencode_sessions_page(
    db_path: &Path,
    limit: usize,
    offset: usize,
) -> Result<Vec<OpenCodeSessionCandidate>> {
    if limit == 0 {
        return Ok(Vec::new());
    }
    list_opencode_sessions_inner(db_path, Some((limit, offset)))
}

fn list_opencode_sessions_inner(
    db_path: &Path,
    page: Option<(usize, usize)>,
) -> Result<Vec<OpenCodeSessionCandidate>> {
    let conn = open_readonly(db_path)?;
    let has_agent_column = sqlite_column_exists(&conn, "session", "agent")?;
    let sql = format!(
        r#"
        SELECT s.id,
               MAX(
                   s.time_updated,
                   COALESCE((SELECT MAX(m.time_updated) FROM message m WHERE m.session_id = s.id), 0),
                   COALESCE((SELECT MAX(p.time_updated) FROM part p WHERE p.session_id = s.id), 0)
        ) AS version_ms
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
        candidate.fingerprint =
            session_fingerprint(&conn, &candidate.provider_session_id, has_agent_column)?;
        sessions.push(candidate);
    }
    Ok(sessions)
}

fn opencode_state_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
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
        let Ok(read_dir) = fs::read_dir(root) else {
            continue;
        };
        let mut paths: Vec<PathBuf> = read_dir
            .filter_map(|entry| entry.ok().map(|entry| entry.path()))
            .filter(|path| {
                path.extension()
                    .and_then(|value| value.to_str())
                    .map(|value| value == "json")
                    .unwrap_or(false)
            })
            .collect();
        paths.sort();
        for path in paths {
            let Ok(text) = fs::read_to_string(&path) else {
                continue;
            };
            let Ok(value) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
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
            if state_provider_session_id != Some(provider_session_id) {
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

pub fn parse_opencode_session(db_path: &Path, provider_session_id: &str) -> Result<ParseResult> {
    let conn = open_readonly(db_path)?;
    let mut session = load_session(&conn, provider_session_id)?;
    session.agent = load_session_agent(&conn, provider_session_id)?;
    let messages = load_messages(&conn, provider_session_id)?;
    let parts = load_parts(&conn, provider_session_id)?;
    let messages_by_id: HashMap<&str, &OpenCodeMessageRow> = messages
        .iter()
        .map(|message| (message.id.as_str(), message))
        .collect();

    let longhouse_session_id = longhouse_session_id_for_opencode(provider_session_id);
    let mut events = Vec::new();
    let mut source_lines = Vec::new();
    let mut media_objects = Vec::new();
    let mut candidate_records = 0usize;
    let mut last_source_offset = 0u64;

    for (part_index, part) in parts.iter().enumerate() {
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
        let redacted_source_raw = redact_inline_image_data_urls_with_media(&source_raw);
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
    }

    events.sort_by(|left, right| {
        left.source_offset
            .cmp(&right.source_offset)
            .then(left.uuid.cmp(&right.uuid))
    });

    let session_version = list_opencode_sessions(db_path)?
        .into_iter()
        .find(|candidate| candidate.provider_session_id == provider_session_id)
        .map(|candidate| candidate.version)
        .unwrap_or_else(|| last_source_offset.saturating_add(1));

    let task_child = match session.parent_id.as_deref() {
        Some(parent_id) => opencode_task_child_evidence(&conn, parent_id, provider_session_id)?,
        None => None,
    };
    let task_child_agent = task_child
        .as_ref()
        .and_then(|evidence| evidence.agent.as_deref())
        .or(session.agent.as_deref())
        .map(str::to_string);
    let lineage_kind = opencode_lineage_kind(&session, task_child.is_some());
    let classification = opencode_session_classification_sidecar(provider_session_id);

    Ok(ParseResult {
        events,
        source_lines,
        media_objects,
        last_good_offset: session_version.max(last_source_offset.saturating_add(1)),
        metadata: SessionMetadata {
            session_id: longhouse_session_id,
            provider_session_id: Some(provider_session_id.to_string()),
            forked_from_session_id: session.parent_id.clone(),
            lineage_kind,
            subagent_id: if task_child.is_some() {
                task_child_agent
                    .clone()
                    .or_else(|| Some(provider_session_id.to_string()))
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
            project: project_label(&session),
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
    })
}

pub fn opencode_raw_snapshot(
    db_path: &Path,
    provider_session_id: &str,
) -> Result<OpenCodeRawSnapshot> {
    let conn = open_readonly(db_path)?;
    let mut session = load_session(&conn, provider_session_id)?;
    session.agent = load_session_agent(&conn, provider_session_id)?;
    let messages = load_messages(&conn, provider_session_id)?;
    let parts = load_parts(&conn, provider_session_id)?;
    let mut records = Vec::with_capacity(messages.len() + parts.len() + 1);
    records.push(serde_json::to_vec(&json!({
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
    }))?);
    for message in &messages {
        records.push(serde_json::to_vec(&json!({
            "kind": "message",
            "provider": "opencode",
            "provider_session_id": provider_session_id,
            "message_id": message.id,
            "message_time_created": message.time_created,
            "message_time_updated": message.time_updated,
            "message_data": message.data,
        }))?);
    }
    let part_record_start =
        u64::try_from(records.len()).context("OpenCode raw record count exceeds u64")?;
    for part in parts {
        records.push(serde_json::to_vec(&json!({
            "kind": "part",
            "provider": "opencode",
            "provider_session_id": provider_session_id,
            "message_id": part.message_id,
            "part_id": part.id,
            "part_time_created": part.time_created,
            "part_time_updated": part.time_updated,
            "part_data": part.data,
        }))?);
    }
    let mut revision = Sha256::new();
    for record in &records {
        revision.update((record.len() as u64).to_be_bytes());
        revision.update(record);
    }
    Ok(OpenCodeRawSnapshot {
        source_revision: format!("{:x}", revision.finalize()),
        records,
        part_record_start,
    })
}

fn open_readonly(path: &Path) -> Result<Connection> {
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
        return conn
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
            .with_context(|| format!("loading OpenCode session {provider_session_id}"));
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
        _ => {}
    }
    Ok(())
}

fn event_role(role: &str) -> Role {
    if role == "user" {
        Role::User
    } else {
        Role::Assistant
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
    session
        .project_worktree
        .as_deref()
        .filter(|value| value.trim() != "/")
        .and_then(path_basename)
        .map(str::to_string)
        .or_else(|| {
            session
                .project_name
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string)
        })
        .or_else(|| {
            session
                .directory
                .as_deref()
                .and_then(path_basename)
                .map(str::to_string)
        })
        .or_else(|| {
            session
                .path
                .as_deref()
                .and_then(path_basename)
                .map(str::to_string)
        })
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

fn opencode_task_child_evidence(
    conn: &Connection,
    parent_provider_session_id: &str,
    child_provider_session_id: &str,
) -> Result<Option<OpenCodeTaskChildEvidence>> {
    let parts = load_parts(conn, parent_provider_session_id)?;
    for part in parts {
        let part_data: Value = serde_json::from_str(&part.data)
            .with_context(|| format!("parsing OpenCode parent task part {}", part.id))?;
        if part_data.get("type").and_then(Value::as_str) != Some("tool") {
            continue;
        }
        if part_data.get("tool").and_then(Value::as_str) != Some("task") {
            continue;
        }
        let state = part_data.get("state").unwrap_or(&Value::Null);
        let metadata = state
            .get("metadata")
            .or_else(|| part_data.get("metadata"))
            .unwrap_or(&Value::Null);
        let metadata_child_id = string_field(metadata, &["sessionId", "sessionID", "session_id"]);
        let output_child_id = state
            .get("output")
            .and_then(Value::as_str)
            .filter(|output| output.contains(&format!("<task id=\"{child_provider_session_id}\"")));
        if metadata_child_id != Some(child_provider_session_id) && output_child_id.is_none() {
            continue;
        }
        let input = state.get("input").unwrap_or(&Value::Null);
        return Ok(Some(OpenCodeTaskChildEvidence {
            agent: string_field(metadata, &["agent", "subagent_type", "subagentType"])
                .or_else(|| string_field(input, &["subagent_type", "subagentType", "agent"]))
                .map(str::to_string),
            tool_call_id: part_data
                .get("callID")
                .and_then(Value::as_str)
                .or_else(|| part_data.get("callId").and_then(Value::as_str))
                .map(str::to_string),
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
    use std::sync::Mutex;

    use super::*;
    use base64::{engine::general_purpose, Engine as _};

    static ENV_LOCK: Mutex<()> = Mutex::new(());

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

    #[test]
    fn raw_snapshot_preserves_exact_database_strings_in_stable_record_order() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_fixture_db(&db_path);
        let first = opencode_raw_snapshot(&db_path, "ses_test").unwrap();
        let second = opencode_raw_snapshot(&db_path, "ses_test").unwrap();
        assert_eq!(first.source_revision, second.source_revision);
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
        let _lock = ENV_LOCK.lock().unwrap();
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
    fn pending_console_claim_defers_shadow_until_native_binding_is_known() {
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().to_path_buf());
        let run_id = uuid::Uuid::new_v4().to_string();
        registry
            .claim(
                &run_id,
                &uuid::Uuid::new_v4().to_string(),
                &uuid::Uuid::new_v4().to_string(),
                None,
                None,
                "opencode",
            )
            .unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                42,
                42,
                Some("start".to_string()),
                crate::opencode_run::OPENCODE_RUN_ADAPTER,
                "launch",
                None,
                "/tmp/stdout",
                "/tmp/stderr",
                serde_json::json!({}),
            )
            .unwrap();
        let claims = registry.list_nonterminal().unwrap();
        assert!(pending_console_binding_blocks_shadow_from_claims(
            "ses_new", &claims
        ));

        registry
            .mark_provider_binding(&run_id, "ses_bound", None)
            .unwrap();
        let claims = registry.list_nonterminal().unwrap();
        assert!(pending_console_binding_blocks_shadow_from_claims(
            "ses_bound",
            &claims
        ));
        assert!(!pending_console_binding_blocks_shadow_from_claims(
            "ses_other",
            &claims
        ));
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
                &[state_root]
            )
            .as_deref(),
            Some(longhouse_session_id)
        );
    }
}
