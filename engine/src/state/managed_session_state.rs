//! Compatibility adapter for callers that still publish the old managed
//! phase shape. There is only one persisted phase authority:
//! `session_phase_state` via `SessionPhaseStore`.

use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::Connection;

use crate::state::session_phase::{SessionPhaseSignal, SessionPhaseStore};

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

    pub fn record_phase(&self, signal: &ManagedSessionPhaseSignal) -> Result<bool> {
        let _ = &signal.workspace_path;
        SessionPhaseStore::new(self.conn).record(&SessionPhaseSignal {
            session_id: signal.session_id.clone(),
            provider: signal.provider.clone(),
            phase: signal.phase_kind.clone(),
            tool_name: signal.tool_name.clone(),
            source: signal.phase_source.clone(),
            observed_at: signal.observed_at,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compatibility_writer_uses_the_single_phase_ledger() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let observed_at = DateTime::parse_from_rfc3339("2026-04-22T15:01:02Z")
            .unwrap()
            .with_timezone(&Utc);

        ManagedSessionStateStore::new(&conn)
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

        let row: (String, String, Option<String>) = conn
            .query_row(
                "SELECT provider, phase, tool_name FROM session_phase_state WHERE session_id = 'sess-1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row, ("claude".to_string(), "running".to_string(), Some("Bash".to_string())));
        assert!(conn
            .prepare("SELECT 1 FROM managed_session_state")
            .is_err());
    }
}
