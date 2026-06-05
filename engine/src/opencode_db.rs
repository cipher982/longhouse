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
use serde_json::value::RawValue;
use serde_json::{json, Value};
use uuid::Uuid;

use crate::pipeline::parser::{ParseResult, ParsedEvent, ParsedSourceLine, Role, SessionMetadata};

const SOURCE_OFFSET_SCALE: u64 = 1_000_000;

#[derive(Debug, Clone)]
pub struct OpenCodeSessionCandidate {
    pub provider_session_id: String,
    pub source_key: String,
    pub version: u64,
    pub fingerprint: String,
}

#[derive(Debug)]
struct OpenCodeSessionRow {
    parent_id: Option<String>,
    directory: Option<String>,
    path: Option<String>,
    title: Option<String>,
    version: Option<String>,
    time_created: i64,
}

#[derive(Debug)]
struct OpenCodeMessageRow {
    id: String,
    time_created: i64,
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

pub fn list_opencode_sessions(db_path: &Path) -> Result<Vec<OpenCodeSessionCandidate>> {
    let conn = open_readonly(db_path)?;
    let mut stmt = conn.prepare(
        r#"
        SELECT s.id,
               MAX(MAX(s.time_updated, COALESCE(m.time_updated, 0), COALESCE(p.time_updated, 0))) AS version_ms
        FROM session s
        LEFT JOIN message m ON m.session_id = s.id
        LEFT JOIN part p ON p.session_id = s.id
        GROUP BY s.id
        ORDER BY version_ms DESC, s.id ASC
        "#,
    )?;
    let rows = stmt.query_map([], |row| {
        let provider_session_id: String = row.get(0)?;
        let version_ms: i64 = row.get(1)?;
        Ok(OpenCodeSessionCandidate {
            source_key: opencode_source_key(db_path, &provider_session_id),
            provider_session_id,
            version: version_from_ms(version_ms),
            fingerprint: String::new(),
        })
    })?;

    let mut sessions = Vec::new();
    for row in rows {
        let mut candidate = row?;
        candidate.fingerprint = session_fingerprint(&conn, &candidate.provider_session_id)?;
        sessions.push(candidate);
    }
    Ok(sessions)
}

fn opencode_state_roots() -> Vec<PathBuf> {
    let mut roots = Vec::new();
    if let Some(root) = std::env::var_os("LONGHOUSE_OPENCODE_STATE_ROOT") {
        roots.push(PathBuf::from(root));
    }
    if let Some(home) = std::env::var_os("HOME") {
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
            if value.get("opencode_session_id").and_then(Value::as_str) != Some(provider_session_id)
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

pub fn parse_opencode_session(db_path: &Path, provider_session_id: &str) -> Result<ParseResult> {
    let conn = open_readonly(db_path)?;
    let session = load_session(&conn, provider_session_id)?;
    let messages = load_messages(&conn, provider_session_id)?;
    let parts = load_parts(&conn, provider_session_id)?;
    let messages_by_id: HashMap<&str, &OpenCodeMessageRow> = messages
        .iter()
        .map(|message| (message.id.as_str(), message))
        .collect();

    let longhouse_session_id = longhouse_session_id_for_opencode(provider_session_id);
    let mut events = Vec::new();
    let mut source_lines = Vec::new();
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
        let source_offset = source_offset_for_part(part, part_index);
        last_source_offset = last_source_offset.max(source_offset);
        source_lines.push(ParsedSourceLine {
            source_offset,
            raw_line: serde_json::to_string(&json!({
                "provider": "opencode",
                "session_id": provider_session_id,
                "message_id": message.id,
                "part_id": part.id,
                "message": message_data,
                "part": part_data,
            }))?,
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

    Ok(ParseResult {
        events,
        source_lines,
        last_good_offset: session_version.max(last_source_offset.saturating_add(1)),
        metadata: SessionMetadata {
            session_id: longhouse_session_id,
            provider_session_id: Some(provider_session_id.to_string()),
            forked_from_session_id: session.parent_id.clone(),
            cwd: session.directory.clone(),
            project: project_label(&session),
            version: session.version.clone(),
            started_at: Some(timestamp_from_ms(session.time_created)),
            ..Default::default()
        },
        candidate_records,
    })
}

fn open_readonly(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("opening OpenCode database {}", path.display()))?;
    conn.busy_timeout(Duration::from_millis(250))?;
    Ok(conn)
}

fn load_session(conn: &Connection, provider_session_id: &str) -> Result<OpenCodeSessionRow> {
    conn.query_row(
        r#"
        SELECT parent_id, directory, path, title, version, time_created
        FROM session
        WHERE id = ?1
        "#,
        params![provider_session_id],
        |row| {
            Ok(OpenCodeSessionRow {
                parent_id: row.get(0)?,
                directory: row.get(1)?,
                path: row.get(2)?,
                title: row.get(3)?,
                version: row.get(4)?,
                time_created: row.get(5)?,
            })
        },
    )
    .with_context(|| format!("loading OpenCode session {provider_session_id}"))
}

fn load_messages(conn: &Connection, provider_session_id: &str) -> Result<Vec<OpenCodeMessageRow>> {
    let mut stmt = conn.prepare(
        r#"
        SELECT id, time_created, data
        FROM message
        WHERE session_id = ?1
        ORDER BY time_created ASC, id ASC
        "#,
    )?;
    let rows = stmt.query_map(params![provider_session_id], |row| {
        Ok(OpenCodeMessageRow {
            id: row.get(0)?,
            time_created: row.get(1)?,
            data: row.get(2)?,
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
            let role = if role == "user" {
                Role::User
            } else {
                Role::Assistant
            };
            events.push(ParsedEvent {
                uuid: stable_event_uuid(provider_session_id, &part.id, "text"),
                session_id: longhouse_session_id.to_string(),
                timestamp: timestamp_from_ms(part.time_created.max(message.time_created)),
                role,
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
        "reasoning" | "step-start" | "step-finish" => {}
        _ => {}
    }
    Ok(())
}

fn project_label(session: &OpenCodeSessionRow) -> Option<String> {
    session
        .path
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .and_then(|path| Path::new(path).file_name().and_then(|name| name.to_str()))
        .map(str::to_string)
        .or_else(|| {
            session
                .directory
                .as_deref()
                .and_then(|directory| Path::new(directory).file_name())
                .and_then(|name| name.to_str())
                .map(str::to_string)
        })
        .or_else(|| session.title.clone())
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

fn stable_event_uuid(provider_session_id: &str, part_id: &str, suffix: &str) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("opencode:{provider_session_id}:{part_id}:{suffix}").as_bytes(),
    )
    .to_string()
}

fn session_fingerprint(conn: &Connection, provider_session_id: &str) -> Result<String> {
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
        .unwrap_or_else(Utc::now)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_fixture_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
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
            "INSERT INTO session (id, parent_id, directory, path, title, version, time_created, time_updated)
             VALUES (?1, NULL, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                "ses_test",
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
}
