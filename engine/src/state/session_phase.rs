use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection, OptionalExtension};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionPhaseSignal {
    pub session_id: String,
    pub provider: String,
    pub phase: String,
    pub tool_name: Option<String>,
    pub source: String,
    pub observed_at: DateTime<Utc>,
}

pub struct SessionPhaseStore<'a> {
    conn: &'a Connection,
}

impl<'a> SessionPhaseStore<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    pub fn record(&self, signal: &SessionPhaseSignal) -> Result<bool> {
        let next_observed_at = signal.observed_at;
        let existing_observed_at: Option<String> = self
            .conn
            .query_row(
                "SELECT observed_at FROM session_phase_state WHERE session_id = ?1",
                params![signal.session_id],
                |row| row.get(0),
            )
            .optional()?;

        if let Some(existing_raw) = existing_observed_at.as_deref() {
            if let Some(existing) = parse_observed_at(existing_raw) {
                if existing > next_observed_at {
                    return Ok(false);
                }
            }
        }

        let observed_at = next_observed_at.to_rfc3339();
        let tool_name = normalize_optional_string(signal.tool_name.clone());
        self.conn.execute(
            "INSERT INTO session_phase_state (
                session_id,
                provider,
                phase,
                tool_name,
                source,
                observed_at
            ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)
            ON CONFLICT(session_id) DO UPDATE SET
                provider = excluded.provider,
                phase = excluded.phase,
                tool_name = excluded.tool_name,
                source = excluded.source,
                observed_at = excluded.observed_at",
            params![
                signal.session_id,
                signal.provider,
                signal.phase,
                tool_name,
                signal.source,
                observed_at,
            ],
        )?;

        Ok(true)
    }
}

fn parse_observed_at(raw: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|value| value.with_timezone(&Utc))
}

fn normalize_optional_string(value: Option<String>) -> Option<String> {
    value.and_then(|raw| {
        let trimmed = raw.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_string())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn signal(observed_at: &str, phase: &str, tool_name: Option<&str>) -> SessionPhaseSignal {
        SessionPhaseSignal {
            session_id: "sess-1".to_string(),
            provider: "claude".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.map(ToString::to_string),
            source: "claude_hook".to_string(),
            observed_at: DateTime::parse_from_rfc3339(observed_at)
                .unwrap()
                .with_timezone(&Utc),
        }
    }

    #[test]
    fn record_inserts_latest_phase_signal() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = SessionPhaseStore::new(&conn);

        assert!(store
            .record(&signal("2026-04-19T00:00:00Z", "thinking", None))
            .unwrap());

        let row: (String, String, Option<String>, String, String) = conn
            .query_row(
                "SELECT provider, phase, tool_name, source, observed_at
                 FROM session_phase_state
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
        assert_eq!(row.1, "thinking");
        assert_eq!(row.2, None);
        assert_eq!(row.3, "claude_hook");
        assert_eq!(row.4, "2026-04-19T00:00:00+00:00");
    }

    #[test]
    fn record_ignores_older_signal() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = SessionPhaseStore::new(&conn);

        assert!(store
            .record(&signal("2026-04-19T00:10:00Z", "running", Some("Bash")))
            .unwrap());
        assert!(!store
            .record(&signal("2026-04-19T00:05:00Z", "idle", None))
            .unwrap());

        let row: (String, Option<String>, String) = conn
            .query_row(
                "SELECT phase, tool_name, observed_at
                 FROM session_phase_state
                 WHERE session_id = 'sess-1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();

        assert_eq!(row.0, "running");
        assert_eq!(row.1, Some("Bash".to_string()));
        assert_eq!(row.2, "2026-04-19T00:10:00+00:00");
    }

    #[test]
    fn record_replaces_equal_or_newer_signal() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = SessionPhaseStore::new(&conn);

        assert!(store
            .record(&signal("2026-04-19T00:10:00Z", "running", Some("Bash")))
            .unwrap());
        assert!(store
            .record(&signal("2026-04-19T00:10:00Z", "blocked", Some("Edit")))
            .unwrap());

        let row: (String, Option<String>) = conn
            .query_row(
                "SELECT phase, tool_name
                 FROM session_phase_state
                 WHERE session_id = 'sess-1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();

        assert_eq!(row.0, "blocked");
        assert_eq!(row.1, Some("Edit".to_string()));
    }
}
