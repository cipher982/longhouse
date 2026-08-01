//! Durable session -> run bindings.
//!
//! Activity evidence is only meaningful when it names the run it belongs to;
//! the Runtime Host rejects any activity head whose `run_id` is not the
//! durable latest run. The live provider observations that carry `run_id`
//! disappear the instant a launcher exits — Cursor Helm, for example, deletes
//! its per-session state file on teardown. That left the last phase of a
//! session (`idle` at the final turn boundary) with no run to attach to, so it
//! was dropped and the served state stayed frozen on the previous `thinking`.
//!
//! Remembering the binding keeps the closing phase shippable after the
//! provider's own state is gone. Live observations still win; this is only the
//! fallback for a session whose observation has already vanished.

use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use rusqlite::{params, Connection};
use std::collections::HashMap;

/// How long a binding outlives its last live observation. Matched to the
/// longest phase freshness window so any ledger row that can still ship has a
/// run to attach to.
pub const BINDING_RETENTION_SECONDS: i64 = 24 * 60 * 60;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionRunBinding {
    pub session_id: String,
    pub provider: String,
    pub run_id: String,
    pub observed_at: DateTime<Utc>,
}

pub struct SessionRunBindingStore<'a> {
    conn: &'a Connection,
}

impl<'a> SessionRunBindingStore<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    /// LWW upsert keyed on `observed_at`, matching `SessionPhaseStore::record`
    /// so a late-arriving stale observation cannot rebind a session backwards.
    pub fn record(&self, binding: &SessionRunBinding) -> Result<bool> {
        let rows = self.conn.execute(
            "INSERT INTO session_run_binding (
                session_id,
                provider,
                run_id,
                observed_at
            ) VALUES (?1, ?2, ?3, ?4)
            ON CONFLICT(session_id) DO UPDATE SET
                provider = excluded.provider,
                run_id = excluded.run_id,
                observed_at = excluded.observed_at
             WHERE session_run_binding.observed_at <= excluded.observed_at",
            params![
                binding.session_id,
                binding.provider,
                binding.run_id,
                binding.observed_at.to_rfc3339(),
            ],
        )?;
        Ok(rows > 0)
    }

    /// Bindings still inside the retention window, as `session_id -> run_id`.
    pub fn remembered(&self, now: DateTime<Utc>) -> Result<HashMap<String, String>> {
        let cutoff = (now - Duration::seconds(BINDING_RETENTION_SECONDS)).to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT session_id, run_id
             FROM session_run_binding
             WHERE observed_at >= ?1",
        )?;
        let mut rows = stmt.query(params![cutoff])?;
        let mut out = HashMap::new();
        while let Some(row) = rows.next()? {
            out.insert(row.get(0)?, row.get(1)?);
        }
        Ok(out)
    }

    /// Drop bindings past the retention window. One row per session, so this
    /// is a small periodic sweep rather than hot-path work.
    pub fn prune(&self, now: DateTime<Utc>) -> Result<usize> {
        let cutoff = (now - Duration::seconds(BINDING_RETENTION_SECONDS)).to_rfc3339();
        let removed = self.conn.execute(
            "DELETE FROM session_run_binding WHERE observed_at < ?1",
            params![cutoff],
        )?;
        Ok(removed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE session_run_binding (
                session_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                run_id TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );",
        )
        .unwrap();
        conn
    }

    fn binding(run_id: &str, observed_at: &str) -> SessionRunBinding {
        SessionRunBinding {
            session_id: "sess-1".to_string(),
            provider: "cursor".to_string(),
            run_id: run_id.to_string(),
            observed_at: DateTime::parse_from_rfc3339(observed_at)
                .unwrap()
                .with_timezone(&Utc),
        }
    }

    #[test]
    fn remembers_the_latest_binding() {
        let conn = conn();
        let store = SessionRunBindingStore::new(&conn);
        assert!(store.record(&binding("run-a", "2026-08-01T13:10:00Z")).unwrap());
        assert!(store.record(&binding("run-b", "2026-08-01T13:11:00Z")).unwrap());
        let now = DateTime::parse_from_rfc3339("2026-08-01T13:12:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(
            store.remembered(now).unwrap().get("sess-1").map(String::as_str),
            Some("run-b")
        );
    }

    #[test]
    fn older_observation_does_not_rebind() {
        let conn = conn();
        let store = SessionRunBindingStore::new(&conn);
        assert!(store.record(&binding("run-b", "2026-08-01T13:11:00Z")).unwrap());
        assert!(!store.record(&binding("run-a", "2026-08-01T13:10:00Z")).unwrap());
        let now = DateTime::parse_from_rfc3339("2026-08-01T13:12:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(
            store.remembered(now).unwrap().get("sess-1").map(String::as_str),
            Some("run-b")
        );
    }

    #[test]
    fn binding_outlives_the_observation_that_created_it() {
        // The regression this table exists for: the launcher is long gone, but
        // the closing `idle` still needs a run to attach to.
        let conn = conn();
        let store = SessionRunBindingStore::new(&conn);
        store.record(&binding("run-a", "2026-08-01T13:11:00Z")).unwrap();
        let now = DateTime::parse_from_rfc3339("2026-08-01T13:25:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(
            store.remembered(now).unwrap().get("sess-1").map(String::as_str),
            Some("run-a")
        );
    }

    #[test]
    fn prune_drops_bindings_past_retention() {
        let conn = conn();
        let store = SessionRunBindingStore::new(&conn);
        store.record(&binding("run-a", "2026-08-01T13:11:00Z")).unwrap();
        let now = DateTime::parse_from_rfc3339("2026-08-03T13:11:00Z")
            .unwrap()
            .with_timezone(&Utc);
        assert_eq!(store.prune(now).unwrap(), 1);
        assert!(store.remembered(now).unwrap().is_empty());
    }
}
