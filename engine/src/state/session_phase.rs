use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection};

/// Where a phase observation came from. Each provenance ties to a concrete
/// engine writer path. Kept as a closed set so drift between callers can't
/// introduce a silent fourth value the server doesn't know about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PhaseSource {
    /// Claude hook-derived phase signal drained from the outbox.
    ClaudeHook,
    /// Codex hook-derived phase signal drained from the outbox.
    CodexHook,
    /// Antigravity hook-derived phase signal drained from the outbox.
    AntigravityHook,
    /// Codex bridge WebSocket tracker-derived phase signal.
    CodexBridgeWs,
}

impl PhaseSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            PhaseSource::ClaudeHook => "claude_hook",
            PhaseSource::CodexHook => "codex_hook",
            PhaseSource::AntigravityHook => "antigravity_hook",
            PhaseSource::CodexBridgeWs => "codex_bridge",
        }
    }

    pub const fn for_hook_provider(provider: &str) -> Self {
        // Small match-returning-const helper for hook outbox coalescing.
        match provider.as_bytes() {
            b"codex" => PhaseSource::CodexHook,
            b"antigravity" => PhaseSource::AntigravityHook,
            _ => PhaseSource::ClaudeHook,
        }
    }
}

/// Canonical phase vocabulary the runtime reducer understands. Duplicated on
/// the server in `session_runtime.PHASE_FRESHNESS`; keep the two lists in lock
/// step or the overlay will silently drop observations.
pub const KNOWN_PHASES: &[&str] = &[
    "thinking",
    "running",
    "blocked",
    "needs_user",
    "idle",
    "finished",
];

/// Phase freshness windows in seconds. MUST stay in lock-step with
/// `server/zerg/services/local_health.py::_PHASE_FRESHNESS_SECONDS` and
/// `server/zerg/services/session_runtime.py::PHASE_FRESHNESS`. Used by the
/// engine to decide which ledger rows to emit in `engine-status.json`.
pub const PHASE_FRESHNESS_SECONDS: &[(&str, i64)] = &[
    ("thinking", 90),
    ("running", 10 * 60),
    ("idle", 10 * 60),
    ("blocked", 24 * 60 * 60),
    ("needs_user", 10 * 60),
    ("finished", 10 * 60),
];

fn phase_window_seconds(phase: &str) -> Option<i64> {
    PHASE_FRESHNESS_SECONDS
        .iter()
        .find(|(p, _)| *p == phase)
        .map(|(_, s)| *s)
}

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

    /// LWW upsert. Always stores RFC3339 with `+00:00`, so string comparison in
    /// the `WHERE` clause is monotonic. A single statement keeps the check and
    /// the write atomic — no SELECT-then-write race between writers on
    /// different connections.
    ///
    /// Rejects phases outside `KNOWN_PHASES` so a typo in a hook script or
    /// bridge update can't pollute the ledger with values the overlay or
    /// server reducer can't interpret. Rejection returns `Ok(false)`.
    pub fn record(&self, signal: &SessionPhaseSignal) -> Result<bool> {
        if !KNOWN_PHASES.contains(&signal.phase.as_str()) {
            return Ok(false);
        }
        let observed_at = signal.observed_at.to_rfc3339();
        let tool_name = normalize_optional_string(signal.tool_name.clone());
        let rows = self.conn.execute(
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
                observed_at = excluded.observed_at
             WHERE session_phase_state.observed_at <= excluded.observed_at",
            params![
                signal.session_id,
                signal.provider,
                signal.phase,
                tool_name,
                signal.source,
                observed_at,
            ],
        )?;
        Ok(rows > 0)
    }
}

/// One row of the `session_phase_state` table. Used by status emission so
/// consumers reading `engine-status.json` see the same shape the SQL ledger
/// holds.
#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
pub struct PhaseLedgerRow {
    pub session_id: String,
    pub provider: String,
    pub phase: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    pub source: String,
    pub observed_at: String,
}

impl<'a> SessionPhaseStore<'a> {
    /// Return ledger rows whose phase + observed_at still satisfy the
    /// canonical freshness windows. Unknown phases are dropped: the
    /// overlay/server reducer can't interpret them either, so there's no
    /// point emitting them.
    pub fn fresh_rows(&self, now: DateTime<Utc>) -> Result<Vec<PhaseLedgerRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT session_id, provider, phase, tool_name, source, observed_at
             FROM session_phase_state",
        )?;
        let mut rows = stmt.query([])?;
        let mut out = Vec::new();
        while let Some(row) = rows.next()? {
            let phase: String = row.get(2)?;
            let Some(window_secs) = phase_window_seconds(&phase) else {
                continue;
            };
            let observed_at: String = row.get(5)?;
            let observed = match DateTime::parse_from_rfc3339(&observed_at) {
                Ok(dt) => dt.with_timezone(&Utc),
                Err(_) => continue,
            };
            let age_secs = (now - observed).num_seconds();
            if age_secs > window_secs {
                continue;
            }
            out.push(PhaseLedgerRow {
                session_id: row.get(0)?,
                provider: row.get(1)?,
                phase,
                tool_name: row.get(3)?,
                source: row.get(4)?,
                observed_at,
            });
        }
        out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
        Ok(out)
    }
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
    fn hook_phase_source_matches_provider() {
        assert_eq!(
            PhaseSource::for_hook_provider("claude").as_str(),
            "claude_hook"
        );
        assert_eq!(
            PhaseSource::for_hook_provider("codex").as_str(),
            "codex_hook"
        );
        assert_eq!(
            PhaseSource::for_hook_provider("antigravity").as_str(),
            "antigravity_hook"
        );
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

    #[test]
    fn record_rejects_unknown_phase() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = SessionPhaseStore::new(&conn);

        let wrote = store
            .record(&signal("2026-04-19T00:00:00Z", "typo_phase", None))
            .unwrap();
        assert!(!wrote, "unknown phase must not land in the ledger");

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM session_phase_state", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn record_is_lww_across_independent_connections() {
        // Two connections race to write: newer observed_at must win regardless
        // of which connection commits first. The single-statement conditional
        // UPSERT means the stale writer can never overwrite a fresh commit.
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let _bootstrap = crate::state::db::open_db(Some(tmp.path())).unwrap();

        let conn_a = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let conn_b = crate::state::db::open_db(Some(tmp.path())).unwrap();

        // B writes a fresh signal first.
        SessionPhaseStore::new(&conn_b)
            .record(&signal("2026-04-19T00:20:00Z", "running", Some("Bash")))
            .unwrap();

        // A holds onto a stale signal and commits after B. The WHERE clause
        // on the UPSERT must prevent A from overwriting B's newer row.
        let written = SessionPhaseStore::new(&conn_a)
            .record(&signal("2026-04-19T00:05:00Z", "idle", None))
            .unwrap();
        assert!(!written, "stale writer must not overwrite fresh row");

        let row: (String, String) = conn_b
            .query_row(
                "SELECT phase, observed_at
                 FROM session_phase_state
                 WHERE session_id = 'sess-1'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(row.0, "running");
        assert_eq!(row.1, "2026-04-19T00:20:00+00:00");
    }

    #[test]
    fn fresh_rows_drops_stale_and_unknown() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();

        // Insert one fresh + one stale + one unknown-phase row directly.
        // The store's `record()` would reject the unknown one, so we bypass it
        // to simulate a pre-existing legacy row from an older engine build.
        conn.execute(
            "INSERT INTO session_phase_state (session_id, provider, phase, tool_name, source, observed_at)
             VALUES
                ('fresh', 'claude', 'running', 'Bash', 'claude_hook', '2026-04-19T12:00:00+00:00'),
                ('stale', 'claude', 'thinking', NULL, 'claude_hook', '2026-04-19T10:00:00+00:00'),
                ('bogus', 'claude', 'typo_phase', NULL, 'claude_hook', '2026-04-19T12:00:00+00:00')",
            [],
        ).unwrap();

        let store = SessionPhaseStore::new(&conn);
        let now = DateTime::parse_from_rfc3339("2026-04-19T12:05:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let rows = store.fresh_rows(now).unwrap();

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].session_id, "fresh");
        assert_eq!(rows[0].phase, "running");
        assert_eq!(rows[0].tool_name.as_deref(), Some("Bash"));
    }

    #[test]
    fn fresh_rows_respects_per_phase_windows() {
        // thinking is 90s; running is 10m; both insertions sit just inside
        // their respective windows.
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        conn.execute(
            "INSERT INTO session_phase_state (session_id, provider, phase, tool_name, source, observed_at)
             VALUES
                ('think-old', 'claude', 'thinking', NULL, 'claude_hook', '2026-04-19T11:58:00+00:00'),
                ('run-old', 'claude', 'running', 'Bash', 'claude_hook', '2026-04-19T11:55:00+00:00')",
            [],
        ).unwrap();

        let store = SessionPhaseStore::new(&conn);
        let now = DateTime::parse_from_rfc3339("2026-04-19T12:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let rows = store.fresh_rows(now).unwrap();

        // thinking row is 2m old — outside 90s window; running row is 5m old —
        // inside 10m window.
        let ids: Vec<&str> = rows.iter().map(|r| r.session_id.as_str()).collect();
        assert_eq!(ids, vec!["run-old"]);
    }
}
