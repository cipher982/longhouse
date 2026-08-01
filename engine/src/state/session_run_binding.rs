//! Durable session -> run bindings, with the window each run was valid for.
//!
//! Activity evidence is only meaningful when it names the run it belongs to;
//! the Runtime Host rejects any activity head whose `run_id` is not the durable
//! latest run. The live provider observations that carry `run_id` disappear the
//! instant a launcher exits — Cursor Helm, for example, deletes its per-session
//! state file on teardown. That left the last phase of a session (`idle` at the
//! final turn boundary) with no run to attach to, so it was dropped and the
//! served state stayed frozen on the previous `thinking`.
//!
//! Remembering the binding keeps the closing phase shippable after the
//! provider's own state is gone. The window matters as much as the mapping: a
//! session that resumes opens a second run while the previous run's phase may
//! still be inside its freshness window. Resolving that phase against the
//! newest mapping would ship run A's activity stamped as run B, and the host
//! would accept it because B is the durable latest run. Phases therefore
//! resolve against `run_started_at` — each attaches to the run that was live
//! when it was observed, or to nothing.

use anyhow::Result;
use chrono::{DateTime, Duration, Utc};
use rusqlite::{params, Connection};
use std::collections::HashMap;

/// How long a binding outlives its last live observation. Matched to the
/// longest phase freshness window so any ledger row that can still ship has a
/// run to attach to.
pub const BINDING_RETENTION_SECONDS: i64 = 24 * 60 * 60;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionRunWindow {
    pub session_id: String,
    pub run_id: String,
    pub provider: String,
    /// When this run began, from the provider observation — not scan wall time.
    /// Using `now` here would make the window start drift later than the phases
    /// that belong to it, which is the misbinding this type exists to prevent.
    pub run_started_at: DateTime<Utc>,
    pub observed_at: DateTime<Utc>,
}

/// Session -> runs, newest first, ready for point-in-time resolution.
#[derive(Debug, Clone, Default)]
pub struct RunWindowIndex {
    by_session: HashMap<String, Vec<(DateTime<Utc>, String)>>,
}

impl RunWindowIndex {
    /// The run that was live when `at` was observed: the most recent run whose
    /// start does not postdate the observation. Returns None when every known
    /// run began after it, which correctly drops a phase we cannot attribute.
    pub fn resolve(&self, session_id: &str, at: DateTime<Utc>) -> Option<&str> {
        self.by_session.get(session_id)?.iter().find_map(|(started_at, run_id)| {
            (*started_at <= at).then_some(run_id.as_str())
        })
    }

    pub fn is_empty(&self) -> bool {
        self.by_session.is_empty()
    }
}

pub struct SessionRunWindowStore<'a> {
    conn: &'a Connection,
}

impl<'a> SessionRunWindowStore<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    /// Upsert one (session, run) window. `run_started_at` is immutable once
    /// recorded; only the liveness stamp advances.
    pub fn record(&self, window: &SessionRunWindow) -> Result<bool> {
        let rows = self.conn.execute(
            "INSERT INTO session_run_window (
                session_id,
                run_id,
                provider,
                run_started_at,
                last_observed_at
            ) VALUES (?1, ?2, ?3, ?4, ?5)
            ON CONFLICT(session_id, run_id) DO UPDATE SET
                provider = excluded.provider,
                last_observed_at = excluded.last_observed_at
             WHERE session_run_window.last_observed_at <= excluded.last_observed_at",
            params![
                window.session_id,
                window.run_id,
                window.provider,
                window.run_started_at.to_rfc3339(),
                window.observed_at.to_rfc3339(),
            ],
        )?;
        Ok(rows > 0)
    }

    /// Index of every retained window, newest run first per session.
    pub fn index(&self, now: DateTime<Utc>) -> Result<RunWindowIndex> {
        let cutoff = (now - Duration::seconds(BINDING_RETENTION_SECONDS)).to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT session_id, run_started_at, run_id
             FROM session_run_window
             WHERE last_observed_at >= ?1
             ORDER BY session_id, run_started_at DESC",
        )?;
        let mut rows = stmt.query(params![cutoff])?;
        let mut by_session: HashMap<String, Vec<(DateTime<Utc>, String)>> = HashMap::new();
        while let Some(row) = rows.next()? {
            let session_id: String = row.get(0)?;
            let started_at: String = row.get(1)?;
            let run_id: String = row.get(2)?;
            let Ok(started_at) = DateTime::parse_from_rfc3339(&started_at) else {
                continue;
            };
            by_session
                .entry(session_id)
                .or_default()
                .push((started_at.with_timezone(&Utc), run_id));
        }
        Ok(RunWindowIndex { by_session })
    }

    /// Drop windows past retention. One row per session-run, so this is a small
    /// periodic sweep rather than hot-path work.
    pub fn prune(&self, now: DateTime<Utc>) -> Result<usize> {
        let cutoff = (now - Duration::seconds(BINDING_RETENTION_SECONDS)).to_rfc3339();
        let removed = self.conn.execute(
            "DELETE FROM session_run_window WHERE last_observed_at < ?1",
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
            "CREATE TABLE session_run_window (
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                run_started_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                PRIMARY KEY (session_id, run_id)
            );",
        )
        .unwrap();
        conn
    }

    fn at(value: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(value).unwrap().with_timezone(&Utc)
    }

    fn window(run_id: &str, run_started_at: &str, observed_at: &str) -> SessionRunWindow {
        SessionRunWindow {
            session_id: "sess-1".to_string(),
            run_id: run_id.to_string(),
            provider: "cursor".to_string(),
            run_started_at: at(run_started_at),
            observed_at: at(observed_at),
        }
    }

    #[test]
    fn binding_outlives_the_observation_that_created_it() {
        // The regression this table exists for: the launcher is long gone, but
        // the closing `idle` still needs a run to attach to.
        let conn = conn();
        let store = SessionRunWindowStore::new(&conn);
        store
            .record(&window("run-a", "2026-08-01T13:10:00Z", "2026-08-01T13:11:00Z"))
            .unwrap();
        let index = store.index(at("2026-08-01T13:25:00Z")).unwrap();
        assert_eq!(
            index.resolve("sess-1", at("2026-08-01T13:11:51Z")),
            Some("run-a")
        );
    }

    #[test]
    fn phase_from_the_previous_run_does_not_bind_to_the_new_one() {
        // Run A ends leaving a phase inside its freshness window; the session
        // resumes as run B. Binding A's phase to B would ship it as B's
        // activity and the host would accept it, because B is the latest run.
        let conn = conn();
        let store = SessionRunWindowStore::new(&conn);
        store
            .record(&window("run-a", "2026-08-01T13:00:00Z", "2026-08-01T13:05:00Z"))
            .unwrap();
        store
            .record(&window("run-b", "2026-08-01T13:10:00Z", "2026-08-01T13:11:00Z"))
            .unwrap();
        let index = store.index(at("2026-08-01T13:12:00Z")).unwrap();

        assert_eq!(
            index.resolve("sess-1", at("2026-08-01T13:04:00Z")),
            Some("run-a"),
            "a phase observed during run A must stay with run A"
        );
        assert_eq!(
            index.resolve("sess-1", at("2026-08-01T13:11:00Z")),
            Some("run-b"),
            "a phase observed during run B belongs to run B"
        );
    }

    #[test]
    fn phase_older_than_every_known_run_is_unattributable() {
        let conn = conn();
        let store = SessionRunWindowStore::new(&conn);
        store
            .record(&window("run-a", "2026-08-01T13:10:00Z", "2026-08-01T13:11:00Z"))
            .unwrap();
        let index = store.index(at("2026-08-01T13:12:00Z")).unwrap();
        assert_eq!(index.resolve("sess-1", at("2026-08-01T12:00:00Z")), None);
    }

    #[test]
    fn run_start_is_immutable_across_re_observation() {
        let conn = conn();
        let store = SessionRunWindowStore::new(&conn);
        store
            .record(&window("run-a", "2026-08-01T13:10:00Z", "2026-08-01T13:11:00Z"))
            .unwrap();
        // A later scan reports the same run; the window start must not drift
        // forward past phases that already belong to it.
        store
            .record(&window("run-a", "2026-08-01T13:20:00Z", "2026-08-01T13:21:00Z"))
            .unwrap();
        let index = store.index(at("2026-08-01T13:22:00Z")).unwrap();
        assert_eq!(
            index.resolve("sess-1", at("2026-08-01T13:11:51Z")),
            Some("run-a")
        );
    }

    #[test]
    fn prune_drops_windows_past_retention() {
        let conn = conn();
        let store = SessionRunWindowStore::new(&conn);
        store
            .record(&window("run-a", "2026-08-01T13:10:00Z", "2026-08-01T13:11:00Z"))
            .unwrap();
        let now = at("2026-08-03T13:11:00Z");
        assert_eq!(store.prune(now).unwrap(), 1);
        assert!(store.index(now).unwrap().is_empty());
    }
}
