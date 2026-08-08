//! Shared SQLite connection for file_state + spool_queue.
//!
//! Same DB as the Python shipper v2: `~/.longhouse/agent/longhouse-shipper.db`.
//! Forward/backward compatible — both Python and Rust can read/write.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::Connection;

use crate::config;

/// Default DB filename (same as Python).
const DB_FILENAME: &str = "longhouse-shipper.db";

/// Resolve the configured DB path (or default) without touching the file.
///
/// # Tests may not resolve the default
///
/// Under `cfg(test)` passing `None` panics instead of returning the real
/// database path. Six tests in `state/source_epoch.rs` called `open_db(None)`
/// and spent months quietly writing into a developer's live 9.7GB shipper
/// database — a fake `codex` epoch was found sitting in it, first written in
/// July. That also made one of them fail, because state survived between runs,
/// and Linux CI never saw any of it since the default path does not exist
/// there.
///
/// A test that wants a database wants a temporary one. Making the wrong call
/// loud is cheaper than auditing for it again: the panic names the fix, and it
/// cannot reach a real machine because it compiles only into test binaries.
///
/// The stronger fix is to delete the hazard from the signature — `open_db(&Path)`
/// plus a separate `open_default_db()` — so the mistake is unrepresentable
/// rather than caught at runtime. That was considered and rejected here: it is
/// 136 call sites across 27 files, and the usual argument for it does not apply
/// to this crate. That argument is that `cfg(test)` misses integration tests,
/// which compile the library without it. This crate has no `[lib]` target —
/// only two `[[bin]]`s — and `engine/tests/*.rs` drive the built binaries
/// through `Command` rather than importing anything here, so no integration
/// test can reach this function at all. If a `[lib]` target is ever added, that
/// reasoning expires and the signature should change.
pub fn resolve_db_path(db_path: Option<&Path>) -> Result<PathBuf> {
    match db_path {
        Some(p) => Ok(p.to_path_buf()),
        None => {
            #[cfg(test)]
            panic!(
                "tests must not open the default shipper database — it is the real \
                 ~/.longhouse/agent/{DB_FILENAME}. Pass Some(temp_dir.join(\"state.db\")) instead."
            );
            #[cfg(not(test))]
            default_db_path()
        }
    }
}

/// Open a fresh connection to an *already-initialized* shipper DB.
///
/// Skips the schema bootstrap `open_db` runs at startup. Use this on the hot
/// path (per-job prepare/ship) once `open_db` has been called once for the
/// process lifetime. Only sets the per-connection PRAGMAs — `journal_mode=WAL`
/// is a database-level setting persisted to the file by the cold open.
pub fn open_connection(db_path: &Path) -> Result<Connection> {
    let conn = Connection::open(db_path)
        .with_context(|| format!("opening SQLite DB: {}", db_path.display()))?;
    conn.execute_batch(
        "PRAGMA synchronous=NORMAL;
         PRAGMA busy_timeout=5000;",
    )?;
    Ok(conn)
}

/// Open (or create) the shipper database with WAL mode and proper pragmas.
pub fn open_db(db_path: Option<&Path>) -> Result<Connection> {
    let path = resolve_db_path(db_path)?;

    // Ensure parent directory exists
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("creating DB directory: {}", parent.display()))?;
    }

    let conn = Connection::open(&path)
        .with_context(|| format!("opening SQLite DB: {}", path.display()))?;

    // Pragmas matching Python shipper
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=NORMAL;
         PRAGMA busy_timeout=5000;",
    )?;

    // Create tables
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS file_state (
            path TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            queued_offset INTEGER NOT NULL DEFAULT 0,
            acked_offset INTEGER NOT NULL DEFAULT 0,
            file_identity TEXT,
            acked_cursor_fingerprint TEXT,
            session_id TEXT,
            provider_session_id TEXT,
            last_updated TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS spool_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            session_id TEXT,
            created_at TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            next_retry_at TEXT NOT NULL,
            last_error TEXT,
            status TEXT DEFAULT 'pending'
        );

        CREATE INDEX IF NOT EXISTS idx_spool_status
        ON spool_queue(status, next_retry_at);

        CREATE TABLE IF NOT EXISTS session_binding (
            path TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS live_file_state (
            path TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            offset INTEGER NOT NULL DEFAULT 0,
            file_identity TEXT,
            session_id TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS session_phase_state (
            session_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            phase TEXT NOT NULL,
            tool_name TEXT,
            source TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0
        );

        -- Runs a provider observation bound a session to, with the window each
        -- run was valid for. Provider state files vanish the moment a launcher
        -- exits, so without a durable binding the final `idle` of a session has
        -- no run to attach to and the served activity head stays frozen on the
        -- last live phase.
        --
        -- One row per (session, run), not per session. A session that resumes
        -- gets a second run while the previous run's phase may still be inside
        -- its freshness window; binding that phase to the newer run would ship
        -- run A's activity stamped as run B, and the Runtime Host would accept
        -- it because B is the durable latest run. Phases resolve against
        -- `run_started_at` so each one attaches to the run that was live when
        -- it was observed.
        CREATE TABLE IF NOT EXISTS session_run_window (
            session_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            run_started_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            PRIMARY KEY (session_id, run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_session_run_window_session
            ON session_run_window(session_id, run_started_at DESC);

        -- Superseded by session_run_window. Held only the latest run per
        -- session with no validity window, which is the misbinding above.
        DROP TABLE IF EXISTS session_run_binding;

        CREATE TABLE IF NOT EXISTS session_title_state (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            first_user_message TEXT NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS unmanaged_process_binding_state (
            provider TEXT NOT NULL,
            provider_session_id TEXT NOT NULL,
            source_path TEXT,
            pid INTEGER NOT NULL,
            process_start_time TEXT NOT NULL,
            process_start_time_key TEXT NOT NULL,
            cwd TEXT,
            observed_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (provider, provider_session_id)
        );

        CREATE TABLE IF NOT EXISTS source_epoch_registry (
            source_epoch TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            opaque_source_id TEXT NOT NULL,
            file_incarnation TEXT NOT NULL,
            predecessor_epoch TEXT,
            start_reason TEXT NOT NULL,
            max_observed_len INTEGER NOT NULL,
            source_revision TEXT,
            bound_session_id TEXT,
            provider_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            ended_at TEXT,
            end_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS source_epoch_lane_state (
            source_epoch TEXT NOT NULL,
            lane TEXT NOT NULL,
            last_position INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (source_epoch, lane),
            FOREIGN KEY (source_epoch) REFERENCES source_epoch_registry(source_epoch)
        );

        CREATE TABLE IF NOT EXISTS pending_source_envelope (
            source_epoch TEXT PRIMARY KEY,
            source_path TEXT NOT NULL,
            range_start INTEGER NOT NULL,
            range_end INTEGER NOT NULL,
            envelope_id TEXT NOT NULL,
            request_body_zstd BLOB NOT NULL,
            media_objects_zstd BLOB NOT NULL,
            raw_bytes INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            has_reply_evidence INTEGER NOT NULL,
            has_more INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            blocked_at TEXT,
            block_kind TEXT,
            block_detail TEXT,
            -- When this row should next be looked at. NOT NULL on purpose:
            -- classification may postpone work, never remove it from
            -- consideration. See the migration in this file for why.
            wake_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00',
            FOREIGN KEY (source_epoch) REFERENCES source_epoch_registry(source_epoch)
        );

        CREATE TABLE IF NOT EXISTS pending_source_envelope_supersession (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_epoch TEXT NOT NULL,
            envelope_id TEXT NOT NULL,
            old_request_body_zstd BLOB NOT NULL,
            new_request_body_zstd BLOB NOT NULL,
            reason TEXT NOT NULL,
            proof_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_epoch) REFERENCES source_epoch_registry(source_epoch)
        );

        CREATE TABLE IF NOT EXISTS cursor_store_root_state (
            conversation_uuid TEXT PRIMARY KEY,
            root_blob_id TEXT NOT NULL,
            message_blob_ids_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cursor_store_raw_record (
            source_epoch TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            source_position INTEGER NOT NULL,
            record_bytes BLOB NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source_epoch, record_hash),
            UNIQUE (source_epoch, source_position),
            FOREIGN KEY (source_epoch) REFERENCES source_epoch_registry(source_epoch)
        );

        CREATE TABLE IF NOT EXISTS cursor_store_capture_cursor (
            source_epoch TEXT PRIMARY KEY,
            last_blob_id TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (source_epoch) REFERENCES source_epoch_registry(source_epoch)
        );

        CREATE TABLE IF NOT EXISTS source_inventory (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            schema_version INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            content_sha256 TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            scan_duration_ms INTEGER NOT NULL,
            scan_error_count INTEGER NOT NULL,
            source_count INTEGER NOT NULL,
            source_bytes INTEGER NOT NULL,
            wal_bytes INTEGER NOT NULL,
            footprint_bytes INTEGER NOT NULL,
            providers_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history_reconciliation (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            open_attempt_id INTEGER,
            open_inventory_generation INTEGER,
            open_content_sha256 TEXT,
            open_discovered_source_count INTEGER,
            open_scan_error_count INTEGER,
            open_started_at TEXT,
            sealed_attempt_id INTEGER,
            sealed_inventory_generation INTEGER,
            sealed_content_sha256 TEXT,
            sealed_at TEXT
        );",
    )?;

    let file_state_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(file_state)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !file_state_columns.contains("file_identity") {
        conn.execute_batch("ALTER TABLE file_state ADD COLUMN file_identity TEXT;")?;
    }
    if !file_state_columns.contains("acked_cursor_fingerprint") {
        conn.execute_batch("ALTER TABLE file_state ADD COLUMN acked_cursor_fingerprint TEXT;")?;
    }

    let live_file_state_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(live_file_state)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !live_file_state_columns.contains("file_identity") {
        conn.execute_batch("ALTER TABLE live_file_state ADD COLUMN file_identity TEXT;")?;
    }

    let session_phase_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(session_phase_state)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !session_phase_columns.contains("revision") {
        // Existing ledgers start at zero. The first accepted write after the
        // upgrade allocates the first monotonic revision; timestamp ordering
        // remains the LWW truth coordinate.
        conn.execute_batch(
            "ALTER TABLE session_phase_state
             ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;",
        )?;
    }

    let unmanaged_binding_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(unmanaged_process_binding_state)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !unmanaged_binding_columns.contains("process_start_time_key") {
        conn.execute_batch(
            "ALTER TABLE unmanaged_process_binding_state
             ADD COLUMN process_start_time_key TEXT NOT NULL DEFAULT '';",
        )?;
    }

    let source_epoch_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(source_epoch_registry)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !source_epoch_columns.contains("source_revision") {
        conn.execute_batch("ALTER TABLE source_epoch_registry ADD COLUMN source_revision TEXT;")?;
    }
    if !source_epoch_columns.contains("bound_session_id") {
        conn.execute_batch("ALTER TABLE source_epoch_registry ADD COLUMN bound_session_id TEXT;")?;
    }
    if !source_epoch_columns.contains("provider_session_id") {
        conn.execute_batch(
            "ALTER TABLE source_epoch_registry ADD COLUMN provider_session_id TEXT;",
        )?;
    }

    let pending_envelope_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(pending_source_envelope)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !pending_envelope_columns.contains("blocked_at") {
        conn.execute_batch(
            "ALTER TABLE pending_source_envelope ADD COLUMN blocked_at TEXT;
             ALTER TABLE pending_source_envelope ADD COLUMN block_kind TEXT;
             ALTER TABLE pending_source_envelope ADD COLUMN block_detail TEXT;",
        )?;
    }
    if !pending_envelope_columns.contains("wake_at") {
        // When this row should next be looked at.
        //
        // Blocking a source used to remove it from consideration outright.
        // Removing that filter fixed the absorbing state and replaced it with
        // the opposite problem: every blocked row is now woken on every restart
        // and re-examined on every watcher tick, each costing a Runtime Host
        // manifest fetch. Unbounded re-examination is how "always schedulable"
        // becomes a load incident.
        //
        // `wake_at` is what lets classification *postpone* a row without ever
        // removing it. Existing rows default to the epoch so nothing is
        // stranded by the migration: an old row is due immediately, gets one
        // examination, and is then scheduled properly.
        conn.execute_batch(
            "ALTER TABLE pending_source_envelope
             ADD COLUMN wake_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00';",
        )?;
    }

    let supersession_columns: std::collections::HashSet<String> = conn
        .prepare("PRAGMA table_info(pending_source_envelope_supersession)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<std::result::Result<_, _>>()?;
    if !supersession_columns.contains("proof_json") {
        conn.execute_batch(
            "ALTER TABLE pending_source_envelope_supersession
             ADD COLUMN proof_json TEXT NOT NULL DEFAULT '{}';",
        )?;
    }
    // Supersession rows are an audit trail, and each stored both exact request
    // bodies in full — roughly 2.4MB per row, 1.29GB across 539 rows on the
    // machine that motivated this, second only to the Cursor store. Nothing in
    // production reads them back; the only `SELECT`s are in tests.
    //
    // Digest and length keep what the audit actually needs: proof of which body
    // replaced which. The bodies themselves are cleared here, once, so the space
    // is recovered on databases that already grew.
    if !supersession_columns.contains("old_request_body_sha256") {
        conn.execute_batch(
            "ALTER TABLE pending_source_envelope_supersession
                 ADD COLUMN old_request_body_sha256 TEXT;
             ALTER TABLE pending_source_envelope_supersession
                 ADD COLUMN new_request_body_sha256 TEXT;
             ALTER TABLE pending_source_envelope_supersession
                 ADD COLUMN old_request_body_len INTEGER;
             ALTER TABLE pending_source_envelope_supersession
                 ADD COLUMN new_request_body_len INTEGER;",
        )?;
        // Backfill lengths before dropping the bytes. The digest cannot be
        // computed in SQLite, so historical rows keep a null hash rather than a
        // wrong one — length plus reason still identifies them, and inventing a
        // hash would be worse than admitting it was never recorded.
        conn.execute_batch(
            "UPDATE pending_source_envelope_supersession
                SET old_request_body_len = length(old_request_body_zstd),
                    new_request_body_len = length(new_request_body_zstd);
             UPDATE pending_source_envelope_supersession
                SET old_request_body_zstd = x'', new_request_body_zstd = x'';",
        )?;
    }

    // Old builds could create duplicate pending pointers for the same file/range.
    // Collapse those rows before enforcing uniqueness so restart recovery becomes idempotent.
    conn.execute(
        "DELETE FROM spool_queue
         WHERE status = 'pending'
           AND id NOT IN (
             SELECT MIN(id)
             FROM spool_queue
             WHERE status = 'pending'
             GROUP BY provider, file_path, start_offset, end_offset
           )",
        [],
    )?;

    conn.execute_batch(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_spool_pending_unique
         ON spool_queue(provider, file_path, start_offset, end_offset)
         WHERE status = 'pending';

         CREATE INDEX IF NOT EXISTS idx_session_phase_provider_observed
         ON session_phase_state(provider, observed_at DESC);

         CREATE INDEX IF NOT EXISTS idx_live_file_state_updated
         ON live_file_state(provider, updated_at DESC);

         CREATE INDEX IF NOT EXISTS idx_unmanaged_process_binding_observed
         ON unmanaged_process_binding_state(provider, observed_at DESC);

         CREATE UNIQUE INDEX IF NOT EXISTS idx_source_epoch_current
         ON source_epoch_registry(provider, opaque_source_id)
         WHERE ended_at IS NULL;

         CREATE INDEX IF NOT EXISTS idx_source_epoch_incarnation
         ON source_epoch_registry(provider, opaque_source_id, file_incarnation, created_at DESC);

         CREATE INDEX IF NOT EXISTS idx_pending_source_envelope_path
         ON pending_source_envelope(source_path, created_at);

         CREATE INDEX IF NOT EXISTS idx_pending_source_envelope_wake
         ON pending_source_envelope(wake_at, source_epoch);

         CREATE INDEX IF NOT EXISTS idx_pending_source_supersession_epoch
         ON pending_source_envelope_supersession(source_epoch, created_at);",
    )?;

    tracing::debug!("Opened shipper DB: {}", path.display());
    Ok(conn)
}

/// Resolve the default DB path: `~/.longhouse/agent/longhouse-shipper.db`.
fn default_db_path() -> Result<PathBuf> {
    let path = config::get_agent_db_path()?;
    debug_assert_eq!(
        path.file_name().and_then(|value| value.to_str()),
        Some(DB_FILENAME)
    );
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_open_in_memory() {
        // Use a temp file instead of :memory: to test real file behavior
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();

        // Tables should exist
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('file_state', 'spool_queue')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 2);
    }

    #[test]
    fn test_wal_mode() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();

        let mode: String = conn
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
    }

    #[test]
    fn pending_retry_plan_starts_from_due_envelopes() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();

        let plan = conn
            .prepare(
                "EXPLAIN QUERY PLAN
                 SELECT epoch.provider, pending.source_path,
                        SUM(pending.raw_bytes), MIN(pending.created_at)
                 FROM pending_source_envelope AS pending
                 JOIN source_epoch_registry AS epoch
                   ON epoch.source_epoch = pending.source_epoch
                 WHERE pending.wake_at <= ?1
                 GROUP BY epoch.provider, pending.source_path
                 ORDER BY MIN(pending.created_at), epoch.provider, pending.source_path",
            )
            .unwrap()
            .query_map(["2026-08-08T00:00:00Z"], |row| row.get::<_, String>(3))
            .unwrap()
            .collect::<std::result::Result<Vec<_>, _>>()
            .unwrap();

        assert!(
            plan.iter()
                .any(|detail| detail.contains("idx_pending_source_envelope_wake")),
            "pending retry plan must use the due-time index: {plan:?}"
        );
        assert!(
            plan.iter().all(|detail| !detail.contains("SCAN epoch")),
            "pending retry plan must not scan every source epoch: {plan:?}"
        );
    }

    #[test]
    fn test_open_db_dedupes_pending_spool_rows_before_unique_index() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = Connection::open(tmp.path()).unwrap();
        conn.execute_batch(
            "CREATE TABLE file_state (
                path TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                queued_offset INTEGER NOT NULL DEFAULT 0,
                acked_offset INTEGER NOT NULL DEFAULT 0,
                session_id TEXT,
                provider_session_id TEXT,
                last_updated TEXT NOT NULL
            );
            CREATE TABLE spool_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                file_path TEXT NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                session_id TEXT,
                created_at TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                next_retry_at TEXT NOT NULL,
                last_error TEXT,
                status TEXT DEFAULT 'pending'
            );",
        )
        .unwrap();

        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('claude', '/dup.jsonl', 100, 500, datetime('now'), datetime('now'), 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('claude', '/dup.jsonl', 100, 500, datetime('now'), datetime('now'), 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('claude', '/dup.jsonl', 100, 500, datetime('now'), datetime('now'), 'dead')",
            [],
        )
        .unwrap();
        drop(conn);

        let conn = open_db(Some(tmp.path())).unwrap();
        let pending_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM spool_queue WHERE status = 'pending'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let dead_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM spool_queue WHERE status = 'dead'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(pending_count, 1);
        assert_eq!(dead_count, 1);

        let err = conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('claude', '/dup.jsonl', 100, 500, datetime('now'), datetime('now'), 'pending')",
            [],
        );
        assert!(
            err.is_err(),
            "unique pending range index should reject duplicates"
        );
    }

    #[test]
    fn test_open_db_adds_source_metadata_to_existing_epoch_registry() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = Connection::open(tmp.path()).unwrap();
        conn.execute_batch(
            "CREATE TABLE source_epoch_registry (
                source_epoch TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                opaque_source_id TEXT NOT NULL,
                file_incarnation TEXT NOT NULL,
                predecessor_epoch TEXT,
                start_reason TEXT NOT NULL,
                max_observed_len INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT,
                end_reason TEXT
            );",
        )
        .unwrap();
        drop(conn);

        let conn = open_db(Some(tmp.path())).unwrap();
        let columns: std::collections::HashSet<String> = conn
            .prepare("PRAGMA table_info(source_epoch_registry)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .collect::<std::result::Result<_, _>>()
            .unwrap();
        assert!(columns.contains("source_revision"));
        assert!(columns.contains("bound_session_id"));
        assert!(columns.contains("provider_session_id"));
    }

    #[test]
    fn test_open_db_adds_revision_to_existing_phase_ledger() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = Connection::open(tmp.path()).unwrap();
        conn.execute_batch(
            "CREATE TABLE session_phase_state (
                session_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                phase TEXT NOT NULL,
                tool_name TEXT,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL
            );
            INSERT INTO session_phase_state
                (session_id, provider, phase, source, observed_at)
            VALUES ('s1', 'codex', 'thinking', 'codex_bridge', '2026-08-01T13:10:00Z');",
        )
        .unwrap();
        drop(conn);

        let conn = open_db(Some(tmp.path())).unwrap();
        let revision: i64 = conn
            .query_row(
                "SELECT revision FROM session_phase_state WHERE session_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(revision, 0);
    }
}
