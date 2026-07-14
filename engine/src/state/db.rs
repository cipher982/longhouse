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
pub fn resolve_db_path(db_path: Option<&Path>) -> Result<PathBuf> {
    match db_path {
        Some(p) => Ok(p.to_path_buf()),
        None => default_db_path(),
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
            observed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS managed_session_state (
            session_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            workspace_path TEXT,
            workspace_label TEXT,
            phase_kind TEXT,
            tool_name TEXT,
            phase_source TEXT,
            phase_observed_at TEXT,
            last_activity_at TEXT,
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

         CREATE INDEX IF NOT EXISTS idx_managed_session_state_provider_updated
         ON managed_session_state(provider, updated_at DESC);

         CREATE INDEX IF NOT EXISTS idx_unmanaged_process_binding_observed
         ON unmanaged_process_binding_state(provider, observed_at DESC);

         CREATE UNIQUE INDEX IF NOT EXISTS idx_source_epoch_current
         ON source_epoch_registry(provider, opaque_source_id)
         WHERE ended_at IS NULL;

         CREATE INDEX IF NOT EXISTS idx_source_epoch_incarnation
         ON source_epoch_registry(provider, opaque_source_id, file_incarnation, created_at DESC);",
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
    }
}
