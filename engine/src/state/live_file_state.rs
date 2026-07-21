//! Realtime-only per-file cursor for live transcript shipping.
//!
//! This cursor is intentionally separate from `file_state`: live Codex sends
//! can skip non-event context rows for latency, while the archival cursor still
//! owns full raw-source shipping.

use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;

use super::file_identity::{current_file_identity, strongest_matching_file_identity};

pub struct LiveFileState<'a> {
    conn: &'a Connection,
}

impl<'a> LiveFileState<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    pub fn get_offset(&self, file_path: &str) -> Result<u64> {
        let result = self.conn.query_row(
            "SELECT offset FROM live_file_state WHERE path = ?",
            [file_path],
            |row| row.get::<_, i64>(0),
        );
        match result {
            Ok(v) => Ok(v as u64),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(0),
            Err(e) => Err(e.into()),
        }
    }

    pub fn get_file_identity(&self, file_path: &str) -> Result<Option<String>> {
        let result = self.conn.query_row(
            "SELECT file_identity FROM live_file_state WHERE path = ?",
            [file_path],
            |row| row.get::<_, Option<String>>(0),
        );
        match result {
            Ok(v) => Ok(v),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

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
            "UPDATE live_file_state
             SET file_identity = ?1, updated_at = ?2
             WHERE path = ?3 AND file_identity IS NOT ?1",
            rusqlite::params![preferred, now, file_path],
        )?;
        Ok(())
    }

    pub fn set_offset(
        &self,
        file_path: &str,
        offset: u64,
        provider: &str,
        session_id: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        let file_identity = self.preferred_current_identity(file_path)?;
        self.conn.execute(
            "INSERT INTO live_file_state (path, provider, offset, file_identity, session_id, updated_at)
             VALUES (?1, ?2, MAX(?3, 0), ?4, ?5, ?6)
             ON CONFLICT(path) DO UPDATE SET
                 offset = MAX(offset, ?3),
                 file_identity = COALESCE(?4, file_identity),
                 session_id = ?5,
                 updated_at = ?6",
            rusqlite::params![file_path, provider, offset as i64, file_identity, session_id, now],
        )?;
        Ok(())
    }

    pub fn reset_offset(&self, file_path: &str) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        let file_identity = self.preferred_current_identity(file_path)?;
        self.conn.execute(
            "UPDATE live_file_state
             SET offset = 0,
                 file_identity = COALESCE(?1, file_identity),
                 updated_at = ?2
             WHERE path = ?3",
            rusqlite::params![file_identity, now, file_path],
        )?;
        Ok(())
    }

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
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn live_file_state_is_monotonic() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let state = LiveFileState::new(&conn);

        state
            .set_offset("/tmp/a.jsonl", 200, "codex", "session-1")
            .unwrap();
        state
            .set_offset("/tmp/a.jsonl", 100, "codex", "session-1")
            .unwrap();

        assert_eq!(state.get_offset("/tmp/a.jsonl").unwrap(), 200);
        assert_eq!(state.get_offset("/tmp/missing.jsonl").unwrap(), 0);
    }
}
