//! Per-file shipping progress tracker.
//!
//! Tracks dual offsets per file:
//! - `queued_offset`: bytes enqueued for shipping (may be in spool)
//! - `acked_offset`: bytes confirmed received by server
//!
//! Gap between them means data needs recovery (re-read from spool pointers).

use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::Connection;

/// A tracked session file.
#[derive(Debug, Clone)]
pub struct TrackedFile {
    pub path: String,
    pub provider: String,
    pub queued_offset: u64,
    pub acked_offset: u64,
    pub session_id: Option<String>,
    pub provider_session_id: Option<String>,
    pub last_updated: DateTime<Utc>,
}

/// File state operations on a shared SQLite connection.
pub struct FileState<'a> {
    conn: &'a Connection,
}

impl<'a> FileState<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    /// Get the acked (confirmed) offset for a file. Returns 0 if not tracked.
    pub fn get_offset(&self, file_path: &str) -> Result<u64> {
        let result = self.conn.query_row(
            "SELECT acked_offset FROM file_state WHERE path = ?",
            [file_path],
            |row| row.get::<_, i64>(0),
        );
        match result {
            Ok(v) => Ok(v as u64),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(0),
            Err(e) => Err(e.into()),
        }
    }

    /// Get the queued offset for a file. Returns 0 if not tracked.
    pub fn get_queued_offset(&self, file_path: &str) -> Result<u64> {
        let result = self.conn.query_row(
            "SELECT queued_offset FROM file_state WHERE path = ?",
            [file_path],
            |row| row.get::<_, i64>(0),
        );
        match result {
            Ok(v) => Ok(v as u64),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(0),
            Err(e) => Err(e.into()),
        }
    }

    /// Update both offsets (used on successful ship). Monotonic — never regresses.
    pub fn set_offset(
        &self,
        file_path: &str,
        offset: u64,
        session_id: &str,
        provider_session_id: &str,
        provider: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO file_state (path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated)
             VALUES (?1, ?2, MAX(?3, 0), MAX(?3, 0), ?4, ?5, ?6)
             ON CONFLICT(path) DO UPDATE SET
                 queued_offset = MAX(queued_offset, ?3),
                 acked_offset = MAX(acked_offset, ?3),
                 session_id = ?4,
                 provider_session_id = ?5,
                 last_updated = ?6",
            rusqlite::params![file_path, provider, offset as i64, session_id, provider_session_id, now],
        )?;
        Ok(())
    }

    /// Advance queued offset only (data enqueued to spool but not yet acked).
    pub fn set_queued_offset(
        &self,
        file_path: &str,
        offset: u64,
        provider: &str,
        session_id: &str,
        provider_session_id: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO file_state (path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated)
             VALUES (?1, ?2, MAX(?3, 0), 0, ?4, ?5, ?6)
             ON CONFLICT(path) DO UPDATE SET
                 queued_offset = MAX(queued_offset, ?3),
                 session_id = COALESCE(?4, session_id),
                 provider_session_id = COALESCE(?5, provider_session_id),
                 last_updated = ?6",
            rusqlite::params![file_path, provider, offset as i64, session_id, provider_session_id, now],
        )?;
        Ok(())
    }

    /// Advance acked offset only (server confirmed receipt). Monotonic.
    pub fn set_acked_offset(&self, file_path: &str, offset: u64) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "UPDATE file_state SET acked_offset = MAX(acked_offset, ?1), last_updated = ?2
             WHERE path = ?3",
            rusqlite::params![offset as i64, now, file_path],
        )?;
        Ok(())
    }

    /// Reset both offsets to 0 (e.g., after file truncation).
    pub fn reset_offsets(&self, file_path: &str) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "UPDATE file_state SET queued_offset = 0, acked_offset = 0, last_updated = ?1
             WHERE path = ?2",
            rusqlite::params![now, file_path],
        )?;
        Ok(())
    }

    /// Get files where queued_offset > acked_offset (need recovery on startup).
    pub fn get_unacked_files(&self) -> Result<Vec<TrackedFile>> {
        let mut stmt = self.conn.prepare(
            "SELECT path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated
             FROM file_state WHERE queued_offset > acked_offset",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok(TrackedFile {
                path: row.get(0)?,
                provider: row.get(1)?,
                queued_offset: row.get::<_, i64>(2)? as u64,
                acked_offset: row.get::<_, i64>(3)? as u64,
                session_id: row.get(4)?,
                provider_session_id: row.get(5)?,
                last_updated: row
                    .get::<_, String>(6)
                    .map(|s| DateTime::parse_from_rfc3339(&s).map(|d| d.with_timezone(&Utc)).unwrap_or_else(|_| Utc::now()))?,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Get full tracking info for a file.
    pub fn get_session(&self, file_path: &str) -> Result<Option<TrackedFile>> {
        let result = self.conn.query_row(
            "SELECT path, provider, queued_offset, acked_offset, session_id, provider_session_id, last_updated
             FROM file_state WHERE path = ?",
            [file_path],
            |row| {
                Ok(TrackedFile {
                    path: row.get(0)?,
                    provider: row.get(1)?,
                    queued_offset: row.get::<_, i64>(2)? as u64,
                    acked_offset: row.get::<_, i64>(3)? as u64,
                    session_id: row.get(4)?,
                    provider_session_id: row.get(5)?,
                    last_updated: row
                        .get::<_, String>(6)
                        .map(|s| DateTime::parse_from_rfc3339(&s).map(|d| d.with_timezone(&Utc)).unwrap_or_else(|_| Utc::now()))?,
                })
            },
        );
        match result {
            Ok(f) => Ok(Some(f)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Count all tracked files.
    pub fn count(&self) -> Result<usize> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM file_state", [], |row| row.get(0))?;
        Ok(count as usize)
    }

    /// Remove entries for files that no longer exist on disk and haven't been updated recently.
    ///
    /// Deletes rows where `last_updated < N days ago AND path not found on disk`.
    /// Returns the number of rows removed.
    pub fn prune_stale(&self, days: u64) -> Result<usize> {
        let cutoff = (chrono::Utc::now() - chrono::Duration::days(days as i64)).to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT path FROM file_state WHERE last_updated < ?",
        )?;
        let paths: Vec<String> = stmt
            .query_map([&cutoff], |row| row.get(0))?
            .filter_map(|r| r.ok())
            .collect();

        let mut pruned = 0usize;
        for path in &paths {
            if !std::path::Path::new(path).exists() {
                self.conn.execute("DELETE FROM file_state WHERE path = ?", [path])?;
                pruned += 1;
            }
        }
        Ok(pruned)
    }
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
    fn test_get_offset_default() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        assert_eq!(fs.get_offset("/nonexistent").unwrap(), 0);
    }

    #[test]
    fn test_set_and_get_offset() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        fs.set_offset("/path/a.jsonl", 1000, "s1", "ps1", "claude")
            .unwrap();
        assert_eq!(fs.get_offset("/path/a.jsonl").unwrap(), 1000);
        assert_eq!(fs.get_queued_offset("/path/a.jsonl").unwrap(), 1000);
    }

    #[test]
    fn test_offset_monotonic() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        fs.set_offset("/f", 1000, "s1", "ps1", "claude").unwrap();
        // Trying to set lower offset should not regress
        fs.set_offset("/f", 500, "s1", "ps1", "claude").unwrap();
        assert_eq!(fs.get_offset("/f").unwrap(), 1000);
    }

    #[test]
    fn test_dual_offsets() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);

        // Set queued only
        fs.set_queued_offset("/f", 2000, "claude", "s1", "ps1")
            .unwrap();
        assert_eq!(fs.get_queued_offset("/f").unwrap(), 2000);
        assert_eq!(fs.get_offset("/f").unwrap(), 0); // acked still 0

        // Now ack up to 1500
        fs.set_acked_offset("/f", 1500).unwrap();
        assert_eq!(fs.get_offset("/f").unwrap(), 1500);
        assert_eq!(fs.get_queued_offset("/f").unwrap(), 2000);
    }

    #[test]
    fn test_unacked_files() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);

        fs.set_queued_offset("/a", 1000, "claude", "s1", "ps1")
            .unwrap();
        fs.set_offset("/b", 500, "s1", "ps1", "claude").unwrap();

        let unacked = fs.get_unacked_files().unwrap();
        assert_eq!(unacked.len(), 1);
        assert_eq!(unacked[0].path, "/a");
    }

    #[test]
    fn test_reset_offsets() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        fs.set_offset("/f", 1000, "s1", "ps1", "claude").unwrap();
        fs.reset_offsets("/f").unwrap();
        assert_eq!(fs.get_offset("/f").unwrap(), 0);
        assert_eq!(fs.get_queued_offset("/f").unwrap(), 0);
    }

    #[test]
    fn test_get_session() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);

        assert!(fs.get_session("/nope").unwrap().is_none());

        fs.set_offset("/f", 500, "s1", "ps1", "claude").unwrap();
        let session = fs.get_session("/f").unwrap().unwrap();
        assert_eq!(session.provider, "claude");
        assert_eq!(session.acked_offset, 500);
        assert_eq!(session.session_id, Some("s1".to_string()));
    }

    #[test]
    fn test_file_state_prune_removes_old() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);

        // Insert a file state entry with an old last_updated timestamp
        let old_date = (chrono::Utc::now() - chrono::Duration::days(35)).to_rfc3339();
        conn.execute(
            "INSERT OR REPLACE INTO file_state (path, acked_offset, queued_offset, provider, last_updated)
             VALUES ('/vanished/old.jsonl', 500, 500, 'claude', ?1)",
            [&old_date],
        ).unwrap();

        // Insert a recent file state entry
        fs.set_offset("/recent/new.jsonl", 100, "s2", "ps2", "claude").unwrap();

        // Prune entries >30 days where path doesn't exist on disk
        // Both paths don't exist on disk, but only the old one is outside the window
        let pruned = fs.prune_stale(30).unwrap();
        assert_eq!(pruned, 1, "Should prune exactly 1 stale entry");

        // Old entry is gone
        assert_eq!(fs.get_offset("/vanished/old.jsonl").unwrap(), 0, "Pruned entry should return default 0");

        // Recent entry is kept
        assert_eq!(fs.get_offset("/recent/new.jsonl").unwrap(), 100, "Recent entry should survive pruning");
    }
}
