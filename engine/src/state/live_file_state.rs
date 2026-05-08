//! Realtime-only per-file cursor for live transcript shipping.
//!
//! This cursor is intentionally separate from `file_state`: live Codex sends
//! can skip non-event context rows for latency, while the archival cursor still
//! owns full raw-source shipping.

use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;

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

    pub fn set_offset(
        &self,
        file_path: &str,
        offset: u64,
        provider: &str,
        session_id: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO live_file_state (path, provider, offset, session_id, updated_at)
             VALUES (?1, ?2, MAX(?3, 0), ?4, ?5)
             ON CONFLICT(path) DO UPDATE SET
                 offset = MAX(offset, ?3),
                 session_id = ?4,
                 updated_at = ?5",
            rusqlite::params![file_path, provider, offset as i64, session_id, now],
        )?;
        Ok(())
    }

    pub fn reset_offset(&self, file_path: &str) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "UPDATE live_file_state SET offset = 0, updated_at = ?1 WHERE path = ?2",
            rusqlite::params![now, file_path],
        )?;
        Ok(())
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
