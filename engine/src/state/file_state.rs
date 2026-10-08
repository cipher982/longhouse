//! Per-file legacy cursor and identity store.
//!
//! The v1 shipper recorded a queued and an acked offset per file. Storage-v2
//! tracks position in source epochs instead; this table survives so a source
//! with no epoch yet can adopt its acked v1 offset (with identity proof) rather
//! than replaying from zero, and so file identity can be recorded.

use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;
#[cfg(test)]
use rusqlite::OptionalExtension;

#[cfg(test)]
use super::file_identity::current_file_identity;
#[cfg(test)]
use super::file_identity::cursor_fingerprint;
use super::file_identity::strongest_matching_file_identity;

/// A tracked session file.
#[derive(Debug, Clone)]
#[cfg(test)]
pub struct TrackedFile {
    pub path: String,
    pub provider: String,
    pub queued_offset: u64,
    pub acked_offset: u64,
    pub session_id: Option<String>,
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
    #[cfg(test)]
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

    /// Get the recorded backing-file identity for a path, if known.
    pub fn get_file_identity(&self, file_path: &str) -> Result<Option<String>> {
        let result = self.conn.query_row(
            "SELECT file_identity FROM file_state WHERE path = ?",
            [file_path],
            |row| row.get::<_, Option<String>>(0),
        );
        match result {
            Ok(v) => Ok(v),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    pub fn get_acked_cursor_fingerprint(&self, file_path: &str) -> Result<Option<String>> {
        let result = self.conn.query_row(
            "SELECT acked_cursor_fingerprint FROM file_state WHERE path = ?",
            [file_path],
            |row| row.get::<_, Option<String>>(0),
        );
        match result {
            Ok(value) => Ok(value),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    /// Return the most recently observed provider conversation for another
    /// source belonging to the same managed Longhouse session.
    pub fn previous_provider_session_id(
        &self,
        file_path: &str,
        session_id: &str,
        provider: &str,
    ) -> Result<Option<String>> {
        let result = self.conn.query_row(
            "SELECT provider_session_id
             FROM file_state
             WHERE session_id = ?1
               AND provider = ?2
               AND path != ?3
               AND provider_session_id IS NOT NULL
               AND provider_session_id != ''
             ORDER BY last_updated DESC, path DESC
             LIMIT 1",
            rusqlite::params![session_id, provider, file_path],
            |row| row.get::<_, String>(0),
        );
        match result {
            Ok(value) => Ok(Some(value)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    /// Persist the strongest identity for a source already proven continuous.
    pub fn record_continuous_file_identity(
        &self,
        file_path: &str,
        file_identity: Option<&str>,
    ) -> Result<()> {
        let Some(file_identity) = file_identity else {
            return Ok(());
        };
        let stored = self.get_file_identity(file_path)?;
        let preferred = match stored.as_deref() {
            Some(stored) => strongest_matching_file_identity(stored, file_identity),
            None => Some(file_identity),
        };
        let Some(preferred) = preferred else {
            return Ok(());
        };
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "UPDATE file_state
             SET file_identity = ?1, last_updated = ?2
             WHERE path = ?3 AND file_identity IS NOT ?1",
            rusqlite::params![preferred, now, file_path],
        )?;
        Ok(())
    }

    /// Update both offsets (used on successful ship). Monotonic — never regresses.
    #[cfg(test)]
    pub fn set_offset(
        &self,
        file_path: &str,
        offset: u64,
        session_id: &str,
        provider_session_id: &str,
        provider: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        let file_identity = self.preferred_current_identity(file_path)?;
        let acked_cursor_fingerprint = cursor_fingerprint(std::path::Path::new(file_path), offset);
        self.conn.execute(
            "INSERT INTO file_state (path, provider, queued_offset, acked_offset, file_identity, acked_cursor_fingerprint, session_id, provider_session_id, last_updated)
             VALUES (?1, ?2, MAX(?3, 0), MAX(?3, 0), ?4, ?5, ?6, ?7, ?8)
             ON CONFLICT(path) DO UPDATE SET
                 queued_offset = MAX(queued_offset, ?3),
                 acked_offset = MAX(acked_offset, ?3),
                 file_identity = COALESCE(?4, file_identity),
                 acked_cursor_fingerprint = CASE
                     WHEN ?3 >= acked_offset THEN ?5
                     ELSE acked_cursor_fingerprint
                 END,
                 session_id = ?6,
                 provider_session_id = ?7,
                 last_updated = ?8",
            rusqlite::params![
                file_path,
                provider,
                offset as i64,
                file_identity,
                acked_cursor_fingerprint,
                session_id,
                provider_session_id,
                now
            ],
        )?;
        Ok(())
    }

    /// Advance queued offset only (enqueued but not yet acked).
    #[cfg(test)]
    pub fn set_queued_offset(
        &self,
        file_path: &str,
        offset: u64,
        provider: &str,
        session_id: &str,
        provider_session_id: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        let file_identity = self.preferred_current_identity(file_path)?;
        self.conn.execute(
            "INSERT INTO file_state (path, provider, queued_offset, acked_offset, file_identity, session_id, provider_session_id, last_updated)
             VALUES (?1, ?2, MAX(?3, 0), 0, ?4, ?5, ?6, ?7)
             ON CONFLICT(path) DO UPDATE SET
                 queued_offset = MAX(queued_offset, ?3),
                 file_identity = COALESCE(?4, file_identity),
                 session_id = COALESCE(?5, session_id),
                 provider_session_id = COALESCE(?6, provider_session_id),
                 last_updated = ?7",
            rusqlite::params![
                file_path,
                provider,
                offset as i64,
                file_identity,
                session_id,
                provider_session_id,
                now
            ],
        )?;
        Ok(())
    }

    /// Advance acked offset only (server confirmed receipt). Monotonic.
    #[cfg(test)]
    pub fn set_acked_offset(&self, file_path: &str, offset: u64) -> Result<()> {
        // Sealing an untracked path, a position already behind the acked one, or
        // the exact position and boundary proof already on file changes nothing,
        // so it writes nothing: `last_updated` is when the row last changed.
        let recorded: Option<(i64, Option<String>)> = self
            .conn
            .query_row(
                "SELECT acked_offset, acked_cursor_fingerprint FROM file_state WHERE path = ?",
                [file_path],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let Some((acked_offset, recorded_fingerprint)) = recorded else {
            return Ok(());
        };
        if (offset as i64) < acked_offset {
            return Ok(());
        }
        let acked_cursor_fingerprint = cursor_fingerprint(std::path::Path::new(file_path), offset);
        if offset as i64 == acked_offset && recorded_fingerprint == acked_cursor_fingerprint {
            return Ok(());
        }
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "UPDATE file_state
             SET acked_cursor_fingerprint = CASE
                     WHEN ?1 >= acked_offset THEN ?2
                     ELSE acked_cursor_fingerprint
                 END,
                 acked_offset = MAX(acked_offset, ?1),
                 last_updated = ?3
             WHERE path = ?4",
            rusqlite::params![offset as i64, acked_cursor_fingerprint, now, file_path],
        )?;
        Ok(())
    }

    #[cfg(test)]
    fn preferred_current_identity(&self, file_path: &str) -> Result<Option<String>> {
        let current = current_file_identity(file_path);
        let Some(current) = current else {
            return Ok(None);
        };
        let stored = self.get_file_identity(file_path)?;
        Ok(Some(match stored.as_deref() {
            Some(stored) => strongest_matching_file_identity(stored, &current)
                .unwrap_or(&current)
                .to_string(),
            None => current,
        }))
    }

    /// Get full tracking info for a file.
    #[cfg(test)]
    pub fn get_session(&self, file_path: &str) -> Result<Option<TrackedFile>> {
        let result = self.conn.query_row(
            "SELECT path, provider, queued_offset, acked_offset, session_id
             FROM file_state WHERE path = ?",
            [file_path],
            |row| {
                Ok(TrackedFile {
                    path: row.get(0)?,
                    provider: row.get(1)?,
                    queued_offset: row.get::<_, i64>(2)? as u64,
                    acked_offset: row.get::<_, i64>(3)? as u64,
                    session_id: row.get(4)?,
                })
            },
        );
        match result {
            Ok(f) => Ok(Some(f)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Remove entries for files that no longer exist on disk and haven't been updated recently.
    ///
    /// Deletes rows where `last_updated < N days ago AND path not found on disk`.
    /// Returns the number of rows removed.
    pub fn prune_stale(&self, days: u64) -> Result<usize> {
        let cutoff = (chrono::Utc::now() - chrono::Duration::days(days as i64)).to_rfc3339();
        let mut stmt = self
            .conn
            .prepare("SELECT path FROM file_state WHERE last_updated < ?")?;
        let paths: Vec<String> = stmt
            .query_map([&cutoff], |row| row.get(0))?
            .filter_map(|r| r.ok())
            .collect();

        let mut pruned = 0usize;
        for path in &paths {
            if path.contains("#opencode:") {
                continue;
            }
            if !std::path::Path::new(path).exists() {
                self.conn
                    .execute("DELETE FROM file_state WHERE path = ?", [path])?;
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
    fn previous_provider_session_id_uses_another_source_in_the_same_managed_session() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        fs.set_offset("/old.jsonl", 100, "managed", "provider-old", "claude")
            .unwrap();
        fs.set_offset("/other.jsonl", 100, "other", "provider-other", "claude")
            .unwrap();

        assert_eq!(
            fs.previous_provider_session_id("/new.jsonl", "managed", "claude")
                .unwrap()
                .as_deref(),
            Some("provider-old")
        );
        assert_eq!(
            fs.previous_provider_session_id("/old.jsonl", "managed", "claude")
                .unwrap(),
            None
        );
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
    fn sealing_an_acked_offset_that_is_already_recorded_writes_nothing() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        fs.set_queued_offset("/f", 2000, "claude", "s1", "ps1")
            .unwrap();
        fs.set_acked_offset("/f", 2000).unwrap();
        let stamp = |conn: &Connection| -> String {
            conn.query_row(
                "SELECT last_updated FROM file_state WHERE path = '/f'",
                [],
                |row| row.get(0),
            )
            .unwrap()
        };
        let before = stamp(&conn);
        let changes = conn.total_changes();

        // The reconciler seals every source it finds current on every scan.
        fs.set_acked_offset("/f", 2000).unwrap();
        fs.set_acked_offset("/f", 1500).unwrap();
        fs.set_acked_offset("/untracked", 10).unwrap();
        assert_eq!(conn.total_changes(), changes);
        assert_eq!(stamp(&conn), before);
        assert_eq!(fs.get_offset("/f").unwrap(), 2000);

        // Real progress is still recorded.
        fs.set_acked_offset("/f", 2500).unwrap();
        assert_eq!(fs.get_offset("/f").unwrap(), 2500);
        assert_ne!(stamp(&conn), before);
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
        fs.set_offset("/recent/new.jsonl", 100, "s2", "ps2", "claude")
            .unwrap();

        // Prune entries >30 days where path doesn't exist on disk
        // Both paths don't exist on disk, but only the old one is outside the window
        let pruned = fs.prune_stale(30).unwrap();
        assert_eq!(pruned, 1, "Should prune exactly 1 stale entry");

        // Old entry is gone
        assert_eq!(
            fs.get_offset("/vanished/old.jsonl").unwrap(),
            0,
            "Pruned entry should return default 0"
        );

        // Recent entry is kept
        assert_eq!(
            fs.get_offset("/recent/new.jsonl").unwrap(),
            100,
            "Recent entry should survive pruning"
        );
    }

    #[test]
    fn test_file_state_prune_keeps_opencode_synthetic_source_keys() {
        let (_tmp, conn) = setup();
        let fs = FileState::new(&conn);
        let old_date = (chrono::Utc::now() - chrono::Duration::days(35)).to_rfc3339();
        let source_key = "/tmp/opencode.db#opencode:ses_123";
        conn.execute(
            "INSERT OR REPLACE INTO file_state (path, acked_offset, queued_offset, provider, session_id, provider_session_id, last_updated)
             VALUES (?1, 500, 500, 'opencode', '11111111-1111-4111-8111-111111111111', 'ses_123', ?2)",
            (source_key, old_date),
        )
        .unwrap();

        let pruned = fs.prune_stale(30).unwrap();

        assert_eq!(pruned, 0);
        assert_eq!(fs.get_offset(source_key).unwrap(), 500);
    }
}
