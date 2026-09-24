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
    /// Cursor hook-derived phase signal drained from the outbox.
    CursorHook,
    /// Codex bridge WebSocket tracker-derived phase signal.
    CodexBridgeWs,
}

impl PhaseSource {
    pub const fn as_str(self) -> &'static str {
        match self {
            PhaseSource::ClaudeHook => "claude_hook",
            PhaseSource::CodexHook => "codex_hook",
            PhaseSource::AntigravityHook => "antigravity_hook",
            PhaseSource::CursorHook => "cursor_hook",
            PhaseSource::CodexBridgeWs => "codex_bridge",
        }
    }

    pub const fn for_hook_provider(provider: &str) -> Self {
        // Small match-returning-const helper for hook outbox coalescing.
        // Every hook-carrying provider gets an explicit arm: a silent
        // fallthrough stamped Cursor activity as `claude_hook`, which made
        // provenance unreadable in the fact store.
        match provider.as_bytes() {
            b"codex" => PhaseSource::CodexHook,
            b"antigravity" => PhaseSource::AntigravityHook,
            b"cursor" => PhaseSource::CursorHook,
            _ => PhaseSource::ClaudeHook,
        }
    }
}

/// Phase freshness windows in seconds, generated from the managed phase
/// contract. Used by the engine to decide which ledger rows to emit in
/// `engine-status.json`.
///
/// This was a hand-maintained copy whose doc comment named two Python
/// locations to keep it in lock-step with -- one of which had already been
/// refactored away.
pub use crate::managed_phase_contract::PHASE_FRESHNESS_SECONDS;

fn phase_window_seconds(phase: &str) -> Option<i64> {
    PHASE_FRESHNESS_SECONDS
        .iter()
        .find(|(p, _)| *p == phase)
        .map(|(_, s)| *s)
        // Raw provider activity is evidence even when the canonical reducer
        // does not recognize its vocabulary yet. Keep it briefly so the
        // typed envelope can project `unknown` without making an unbounded
        // durable claim.
        .or(Some(90))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SessionPhaseSignal {
    pub session_id: String,
    pub provider: String,
    pub phase: String,
    pub tool_name: Option<String>,
    pub source: String,
    pub observed_at: DateTime<Utc>,
    /// The run this phase was observed in, from the producer's own evidence
    /// (`LONGHOUSE_RUN_ID` in the provider environment). A row that carries its
    /// own run needs no timestamp join to be attributed, so an overlapping or
    /// resumed run cannot inherit the previous run's trailing phase.
    pub run_id: Option<String>,
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
    /// different connections. Every accepted write also gets a monotonically
    /// increasing revision so the daemon can detect an accepted update even
    /// when its observed timestamp does not advance the global maximum.
    ///
    /// Unknown provider phases are retained as raw evidence. The typed
    /// heartbeat projection maps them to canonical `unknown`; legacy readers
    /// remain free to ignore vocabulary they do not understand.
    pub fn record(&self, signal: &SessionPhaseSignal) -> Result<bool> {
        let observed_at = signal.observed_at.to_rfc3339();
        let tool_name = normalize_optional_string(signal.tool_name.clone());
        let run_id = normalize_optional_string(signal.run_id.clone());
        let rows = self.conn.execute(
            "INSERT INTO session_phase_state (
                session_id,
                provider,
                phase,
                tool_name,
                source,
                observed_at,
                run_id,
                revision
            ) VALUES (
                ?1, ?2, ?3, ?4, ?5, ?6, ?7,
                COALESCE((SELECT MAX(revision) FROM session_phase_state), 0) + 1
            )
            ON CONFLICT(session_id) DO UPDATE SET
                provider = excluded.provider,
                phase = excluded.phase,
                tool_name = excluded.tool_name,
                source = excluded.source,
                observed_at = excluded.observed_at,
                run_id = excluded.run_id,
                revision = excluded.revision
             WHERE session_phase_state.observed_at <= excluded.observed_at",
            params![
                signal.session_id,
                signal.provider,
                signal.phase,
                tool_name,
                signal.source,
                observed_at,
                run_id,
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
    pub valid_until: String,
    /// The run this phase was observed in, as the producer reported it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
}

impl<'a> SessionPhaseStore<'a> {
    /// Return ledger rows whose phase + observed_at still satisfy their
    /// freshness windows. Unknown raw phases get a short evidence-only window
    /// and are projected as canonical `unknown` by the heartbeat envelope.
    pub fn fresh_rows(&self, now: DateTime<Utc>) -> Result<Vec<PhaseLedgerRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT session_id, provider, phase, tool_name, source, observed_at, run_id
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
            let run_id: Option<String> = row.get(6)?;
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
                valid_until: (observed + chrono::Duration::seconds(window_secs)).to_rfc3339(),
                run_id,
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
    /// Presentation state is disposable: losing it may cost a re-scan, never a
    /// re-ship and never a new source epoch.
    ///
    /// This is the invariant the plan's "second, delete-on-doubt store" was for.
    /// The measured reason it stays in the ledger file is that the split would
    /// touch 69 call sites to relocate 506 rows (phase 57, title 397, run window
    /// 50, inventory 1, reconciliation 1) out of a 152 MB file that no longer
    /// grows with payloads — so what is worth having is the *property*, and this
    /// test is it.
    #[test]
    fn losing_presentation_state_costs_a_rescan_not_a_reship() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let mut conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        let source = dir.path().join("session.jsonl");
        std::fs::write(&source, b"{\"type\":\"session\",\"id\":\"native-1\"}\n").unwrap();

        // A source that has been seen and shipped through position 1. The epoch
        // is created through the real call, so its file incarnation matches the
        // file rather than a hand-written guess.
        let first = crate::state::source_epoch::observe_file(
            &mut conn,
            "claude",
            "opaque-1",
            &source,
            crate::state::source_epoch::SourceLane::Durable,
            1,
            None,
            None,
            crate::state::source_epoch::SourceChangeHint::None,
        )
        .unwrap();
        assert!(
            first.created,
            "the fixture's first observation creates the epoch"
        );
        let epoch = first.source_epoch;
        // The observation already wrote the lane; advance it to what has shipped.
        conn.execute(
            "UPDATE source_epoch_lane_state SET last_position = 1
             WHERE source_epoch = ?1 AND lane = 'durable'",
            [epoch.to_string()],
        )
        .unwrap();
        SessionPhaseStore::new(&conn)
            .record(&SessionPhaseSignal {
                session_id: "session-1".into(),
                provider: "claude".into(),
                phase: "running".into(),
                tool_name: None,
                source: "test".into(),
                observed_at: chrono::Utc::now(),
                run_id: Some("run-1".into()),
            })
            .unwrap();

        // The cache is deleted: every presentation row goes.
        conn.execute("DELETE FROM session_phase_state", []).unwrap();
        conn.execute("DELETE FROM session_title_state", []).unwrap();
        conn.execute("DELETE FROM source_inventory", []).unwrap();

        // Re-observing the same source must not rotate the epoch or move the
        // durable cursor: those are the ledger's, not presentation's.
        let resolution = crate::state::source_epoch::observe_file(
            &mut conn,
            "claude",
            "opaque-1",
            &source,
            crate::state::source_epoch::SourceLane::Durable,
            1,
            None,
            None,
            crate::state::source_epoch::SourceChangeHint::None,
        )
        .unwrap();
        assert!(
            !resolution.created,
            "losing presentation must not rotate an epoch"
        );
        assert_eq!(resolution.source_epoch, epoch);
        let cursor: i64 = conn
            .query_row(
                "SELECT last_position FROM source_epoch_lane_state
                 WHERE source_epoch = ?1 AND lane = 'durable'",
                [epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cursor, 1, "the durable cursor is ledger state and stays");
        let pending: i64 = conn
            .query_row("SELECT COUNT(*) FROM pending_source_envelope", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(pending, 0, "nothing already shipped is re-prepared");

        // And the presentation row is rebuilt by the next observation.
        assert!(SessionPhaseStore::new(&conn)
            .record(&SessionPhaseSignal {
                session_id: "session-1".into(),
                provider: "claude".into(),
                phase: "running".into(),
                tool_name: None,
                source: "test".into(),
                observed_at: chrono::Utc::now(),
                run_id: Some("run-1".into()),
            })
            .unwrap());
        let rebuilt: (i64, Option<String>) = conn
            .query_row(
                "SELECT COUNT(*), MAX(run_id) FROM session_phase_state",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            rebuilt.0, 1,
            "the projection is rebuilt from the next observation"
        );
        assert_eq!(
            rebuilt.1.as_deref(),
            Some("run-1"),
            "the rebuilt row carries the run its producer named"
        );
    }

    use super::*;

    fn signal(observed_at: &str, phase: &str, tool_name: Option<&str>) -> SessionPhaseSignal {
        SessionPhaseSignal {
            session_id: "sess-1".to_string(),
            provider: "claude".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.map(ToString::to_string),
            source: "claude_hook".to_string(),
            run_id: None,
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
    fn record_retains_unknown_phase_as_raw_evidence() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = SessionPhaseStore::new(&conn);

        let wrote = store
            .record(&signal("2026-04-19T00:00:00Z", "typo_phase", None))
            .unwrap();
        assert!(wrote, "unknown raw activity must land in the ledger");

        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM session_phase_state", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1);
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
    fn fresh_rows_drops_stale_but_retains_fresh_unknown() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();

        // Insert one fresh + one stale + one fresh unknown-phase row directly.
        conn.execute(
            "INSERT INTO session_phase_state (session_id, provider, phase, tool_name, source, observed_at)
             VALUES
                ('fresh', 'claude', 'running', 'Bash', 'claude_hook', '2026-04-19T12:00:00+00:00'),
                ('stale', 'claude', 'thinking', NULL, 'claude_hook', '2026-04-19T10:00:00+00:00'),
                ('bogus', 'claude', 'provider_custom_phase', NULL, 'claude_hook', '2026-04-19T12:04:30+00:00')",
            [],
        ).unwrap();

        let store = SessionPhaseStore::new(&conn);
        let now = DateTime::parse_from_rfc3339("2026-04-19T12:05:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let rows = store.fresh_rows(now).unwrap();

        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].session_id, "bogus");
        assert_eq!(rows[0].phase, "provider_custom_phase");
        assert_eq!(rows[0].valid_until, "2026-04-19T12:06:00+00:00");
        assert_eq!(rows[1].session_id, "fresh");
        assert_eq!(rows[1].phase, "running");
        assert_eq!(rows[1].tool_name.as_deref(), Some("Bash"));
        assert_eq!(rows[1].valid_until, "2026-04-19T12:10:00+00:00");
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
