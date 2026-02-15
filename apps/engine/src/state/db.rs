//! Shared SQLite connection for file_state + spool_queue.
//!
//! Same DB as the Python shipper v2: `~/.claude/longhouse-shipper.db`.
//! Forward/backward compatible — both Python and Rust can read/write.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::Connection;

/// Default DB filename (same as Python).
const DB_FILENAME: &str = "longhouse-shipper.db";

/// Open (or create) the shipper database with WAL mode and proper pragmas.
pub fn open_db(db_path: Option<&Path>) -> Result<Connection> {
    let path = match db_path {
        Some(p) => p.to_path_buf(),
        None => default_db_path()?,
    };

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
        ON spool_queue(status, next_retry_at);",
    )?;

    tracing::debug!("Opened shipper DB: {}", path.display());
    Ok(conn)
}

/// Resolve the default DB path: `~/.claude/longhouse-shipper.db`.
fn default_db_path() -> Result<PathBuf> {
    let home = std::env::var("HOME").context("HOME not set")?;
    let claude_dir = if let Ok(config_dir) = std::env::var("CLAUDE_CONFIG_DIR") {
        PathBuf::from(config_dir)
    } else {
        PathBuf::from(home).join(".claude")
    };
    Ok(claude_dir.join(DB_FILENAME))
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
}
