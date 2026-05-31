use std::path::Path;

use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection};

use crate::state::session_phase::KNOWN_PHASES;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ManagedSessionPhaseSignal {
    pub session_id: String,
    pub provider: String,
    pub workspace_path: Option<String>,
    pub phase_kind: String,
    pub tool_name: Option<String>,
    pub phase_source: String,
    pub observed_at: DateTime<Utc>,
}

pub struct ManagedSessionStateStore<'a> {
    conn: &'a Connection,
}

impl<'a> ManagedSessionStateStore<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    /// Upsert current managed-session phase state.
    ///
    /// The newest `phase_observed_at` wins for the canonical phase fields.
    /// Workspace metadata is best-effort and first-known: writers that do not
    /// know the canonical session root must not erase or churn an earlier cwd.
    ///
    /// Unknown phases are rejected so the local current-state row stays inside
    /// the same closed vocabulary as the shipping/runtime phase ledger.
    pub fn record_phase(&self, signal: &ManagedSessionPhaseSignal) -> Result<bool> {
        if !KNOWN_PHASES.contains(&signal.phase_kind.as_str()) {
            return Ok(false);
        }

        let observed_at = signal.observed_at.to_rfc3339();
        let workspace_path = normalize_optional_string(signal.workspace_path.clone());
        let workspace_label = workspace_path
            .as_deref()
            .and_then(derive_workspace_label)
            .map(str::to_string);
        let tool_name = normalize_optional_string(signal.tool_name.clone());

        let rows = self.conn.execute(
            "INSERT INTO managed_session_state (
                session_id,
                provider,
                workspace_path,
                workspace_label,
                phase_kind,
                tool_name,
                phase_source,
                phase_observed_at,
                last_activity_at,
                updated_at
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8, ?8)
            ON CONFLICT(session_id) DO UPDATE SET
                provider = excluded.provider,
                workspace_path = CASE
                    WHEN managed_session_state.workspace_path IS NULL THEN excluded.workspace_path
                    ELSE managed_session_state.workspace_path
                END,
                workspace_label = CASE
                    WHEN managed_session_state.workspace_label IS NULL THEN excluded.workspace_label
                    ELSE managed_session_state.workspace_label
                END,
                phase_kind = excluded.phase_kind,
                tool_name = excluded.tool_name,
                phase_source = excluded.phase_source,
                phase_observed_at = excluded.phase_observed_at,
                last_activity_at = excluded.last_activity_at,
                updated_at = excluded.updated_at
             WHERE managed_session_state.phase_observed_at IS NULL
                OR managed_session_state.phase_observed_at <= excluded.phase_observed_at",
            params![
                signal.session_id,
                signal.provider,
                workspace_path,
                workspace_label,
                signal.phase_kind,
                tool_name,
                signal.phase_source,
                observed_at,
            ],
        )?;

        Ok(rows > 0)
    }
}

fn normalize_optional_string(value: Option<String>) -> Option<String> {
    value.and_then(|raw| {
        let trimmed = raw.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_string())
    })
}

fn derive_workspace_label(workspace_path: &str) -> Option<&str> {
    Path::new(workspace_path)
        .file_name()
        .and_then(|value| value.to_str())
        .map(str::trim)
        .filter(|value| !value.is_empty())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;

    fn setup() -> (tempfile::NamedTempFile, Connection) {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();
        (tmp, conn)
    }

    #[test]
    fn record_phase_persists_workspace_metadata() {
        let (_tmp, conn) = setup();
        let store = ManagedSessionStateStore::new(&conn);

        let observed_at = DateTime::parse_from_rfc3339("2026-04-22T15:01:02Z")
            .unwrap()
            .with_timezone(&Utc);
        let written = store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "claude".to_string(),
                workspace_path: Some("/Users/test/git/acme".to_string()),
                phase_kind: "running".to_string(),
                tool_name: Some("Bash".to_string()),
                phase_source: "claude_hook".to_string(),
                observed_at,
            })
            .unwrap();

        assert!(written);
        let row: (String, String, String, Option<String>, String) = conn
            .query_row(
                "SELECT provider, workspace_path, workspace_label, tool_name, phase_kind
                 FROM managed_session_state
                 WHERE session_id = 'sess-1'",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(row.0, "claude");
        assert_eq!(row.1, "/Users/test/git/acme");
        assert_eq!(row.2, "acme");
        assert_eq!(row.3, Some("Bash".to_string()));
        assert_eq!(row.4, "running");
    }

    #[test]
    fn record_phase_rejects_unknown_phases() {
        let (_tmp, conn) = setup();
        let store = ManagedSessionStateStore::new(&conn);

        let written = store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "claude".to_string(),
                workspace_path: Some("/Users/test/git/acme".to_string()),
                phase_kind: "totally_wrong".to_string(),
                tool_name: None,
                phase_source: "claude_hook".to_string(),
                observed_at: Utc::now(),
            })
            .unwrap();

        assert!(!written);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM managed_session_state", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn record_phase_keeps_newer_row_and_preserves_workspace_when_missing() {
        let (_tmp, conn) = setup();
        let store = ManagedSessionStateStore::new(&conn);

        let old_time = DateTime::parse_from_rfc3339("2026-04-22T15:01:02Z")
            .unwrap()
            .with_timezone(&Utc);
        let new_time = DateTime::parse_from_rfc3339("2026-04-22T15:05:02Z")
            .unwrap()
            .with_timezone(&Utc);

        store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "codex".to_string(),
                workspace_path: Some("/Users/test/git/assistants-service".to_string()),
                phase_kind: "thinking".to_string(),
                tool_name: None,
                phase_source: "codex_hook".to_string(),
                observed_at: old_time,
            })
            .unwrap();
        store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "codex".to_string(),
                workspace_path: None,
                phase_kind: "idle".to_string(),
                tool_name: None,
                phase_source: "codex_bridge".to_string(),
                observed_at: new_time,
            })
            .unwrap();

        let written = store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "codex".to_string(),
                workspace_path: Some("/tmp/wrong".to_string()),
                phase_kind: "running".to_string(),
                tool_name: Some("shell".to_string()),
                phase_source: "codex_bridge".to_string(),
                observed_at: old_time,
            })
            .unwrap();

        assert!(!written);
        let row: (String, String, String, String) = conn
            .query_row(
                "SELECT workspace_path, workspace_label, phase_kind, phase_source
                 FROM managed_session_state
                 WHERE session_id = 'sess-1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(row.0, "/Users/test/git/assistants-service");
        assert_eq!(row.1, "assistants-service");
        assert_eq!(row.2, "idle");
        assert_eq!(row.3, "codex_bridge");
    }

    #[test]
    fn record_phase_keeps_first_known_workspace_when_newer_signal_disagrees() {
        let (_tmp, conn) = setup();
        let store = ManagedSessionStateStore::new(&conn);

        let old_time = DateTime::parse_from_rfc3339("2026-04-22T15:01:02Z")
            .unwrap()
            .with_timezone(&Utc);
        let new_time = DateTime::parse_from_rfc3339("2026-04-22T15:05:02Z")
            .unwrap()
            .with_timezone(&Utc);

        store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "codex".to_string(),
                workspace_path: Some("/Users/test/git/assistants-service".to_string()),
                phase_kind: "thinking".to_string(),
                tool_name: None,
                phase_source: "codex_hook".to_string(),
                observed_at: old_time,
            })
            .unwrap();
        store
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: "sess-1".to_string(),
                provider: "codex".to_string(),
                workspace_path: Some("/tmp/wrong-workspace".to_string()),
                phase_kind: "idle".to_string(),
                tool_name: None,
                phase_source: "codex_bridge".to_string(),
                observed_at: new_time,
            })
            .unwrap();

        let row: (String, String, String) = conn
            .query_row(
                "SELECT workspace_path, workspace_label, phase_kind
                 FROM managed_session_state
                 WHERE session_id = 'sess-1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row.0, "/Users/test/git/assistants-service");
        assert_eq!(row.1, "assistants-service");
        assert_eq!(row.2, "idle");
    }
}
