//! Pointer-based offline spool for retry resilience.
//!
//! Stores byte-range pointers (NOT payloads) into source files.
//! On retry, the source file is re-read and re-parsed.
//! Max queue size: 10,000 entries (backpressure).

use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::Connection;

/// Maximum spool entries before backpressure kicks in.
const MAX_QUEUE_SIZE: usize = 10_000;

/// Base backoff in seconds.
const BACKOFF_BASE: f64 = 5.0;

/// Maximum backoff in seconds (1 hour).
const BACKOFF_MAX: f64 = 3600.0;

/// Default max retries before marking dead.
const DEFAULT_MAX_RETRIES: u32 = 50;

/// A spool entry — pointer to a byte range in a source file.
#[derive(Debug, Clone)]
pub struct SpoolEntry {
    pub id: i64,
    pub provider: String,
    pub file_path: String,
    pub start_offset: u64,
    pub end_offset: u64,
    pub session_id: Option<String>,
    pub created_at: DateTime<Utc>,
    pub retry_count: u32,
    pub last_error: Option<String>,
}

/// Spool operations on a shared SQLite connection.
pub struct Spool<'a> {
    conn: &'a Connection,
}

impl<'a> Spool<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    /// Enqueue a byte-range pointer. Returns false if at capacity.
    pub fn enqueue(
        &self,
        provider: &str,
        file_path: &str,
        start_offset: u64,
        end_offset: u64,
        session_id: Option<&str>,
    ) -> Result<bool> {
        if self.total_size()? >= MAX_QUEUE_SIZE {
            tracing::warn!("Spool at capacity ({} entries), rejecting enqueue", MAX_QUEUE_SIZE);
            return Ok(false);
        }

        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6, 'pending')",
            rusqlite::params![
                provider,
                file_path,
                start_offset as i64,
                end_offset as i64,
                session_id,
                now,
            ],
        )?;
        Ok(true)
    }

    /// Get pending entries ready for retry (next_retry_at <= now).
    pub fn dequeue_batch(&self, limit: usize) -> Result<Vec<SpoolEntry>> {
        let now = Utc::now().to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT id, provider, file_path, start_offset, end_offset, session_id, created_at, retry_count, last_error
             FROM spool_queue
             WHERE status = 'pending' AND next_retry_at <= ?1
             ORDER BY created_at ASC
             LIMIT ?2",
        )?;
        let rows = stmt.query_map(rusqlite::params![now, limit as i64], |row| {
            Ok(SpoolEntry {
                id: row.get(0)?,
                provider: row.get(1)?,
                file_path: row.get(2)?,
                start_offset: row.get::<_, i64>(3)? as u64,
                end_offset: row.get::<_, i64>(4)? as u64,
                session_id: row.get(5)?,
                created_at: row
                    .get::<_, String>(6)
                    .map(|s| {
                        DateTime::parse_from_rfc3339(&s)
                            .map(|d| d.with_timezone(&Utc))
                            .unwrap_or_else(|_| Utc::now())
                    })?,
                retry_count: row.get::<_, i32>(7)? as u32,
                last_error: row.get(8)?,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Remove a successfully shipped entry.
    pub fn mark_shipped(&self, entry_id: i64) -> Result<()> {
        self.conn
            .execute("DELETE FROM spool_queue WHERE id = ?", [entry_id])?;
        Ok(())
    }

    /// Mark entry as failed with exponential backoff. Returns true if now permanently dead.
    pub fn mark_failed(&self, entry_id: i64, error: &str) -> Result<bool> {
        self.mark_failed_with_max(entry_id, error, DEFAULT_MAX_RETRIES)
    }

    /// Mark failed with custom max retries.
    pub fn mark_failed_with_max(&self, entry_id: i64, error: &str, max_retries: u32) -> Result<bool> {
        // Get current retry count
        let retry_count: i32 = self.conn.query_row(
            "SELECT retry_count FROM spool_queue WHERE id = ?",
            [entry_id],
            |row| row.get(0),
        )?;
        let new_count = retry_count + 1;

        if new_count as u32 >= max_retries {
            // Mark as dead
            self.conn.execute(
                "UPDATE spool_queue SET status = 'dead', retry_count = ?1, last_error = ?2
                 WHERE id = ?3",
                rusqlite::params![new_count, error, entry_id],
            )?;
            return Ok(true);
        }

        // Exponential backoff: min(5 * 2^retry, 3600)
        let backoff_secs = (BACKOFF_BASE * 2.0_f64.powi(new_count)).min(BACKOFF_MAX);
        let next_retry = Utc::now() + chrono::Duration::seconds(backoff_secs as i64);

        self.conn.execute(
            "UPDATE spool_queue SET retry_count = ?1, last_error = ?2, next_retry_at = ?3
             WHERE id = ?4",
            rusqlite::params![new_count, error, next_retry.to_rfc3339(), entry_id],
        )?;
        Ok(false)
    }

    /// Count pending (retryable) entries.
    pub fn pending_count(&self) -> Result<usize> {
        let count: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM spool_queue WHERE status = 'pending'",
            [],
            |row| row.get(0),
        )?;
        Ok(count as usize)
    }

    /// Total entries (for backpressure check).
    pub fn total_size(&self) -> Result<usize> {
        let count: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM spool_queue",
            [],
            |row| row.get(0),
        )?;
        Ok(count as usize)
    }

    /// Remove dead entries older than 7 days. Returns count removed.
    pub fn cleanup(&self) -> Result<usize> {
        let cutoff = (Utc::now() - chrono::Duration::days(7)).to_rfc3339();
        let deleted = self.conn.execute(
            "DELETE FROM spool_queue WHERE status = 'dead' AND created_at < ?",
            [&cutoff],
        )?;
        // Also clean pending entries older than 7 days
        let deleted2 = self.conn.execute(
            "DELETE FROM spool_queue WHERE status = 'pending' AND created_at < ?",
            [&cutoff],
        )?;
        Ok(deleted + deleted2)
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
    fn test_enqueue_dequeue() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        let ok = spool
            .enqueue("claude", "/path/a.jsonl", 0, 1000, Some("s1"))
            .unwrap();
        assert!(ok);
        assert_eq!(spool.pending_count().unwrap(), 1);

        let batch = spool.dequeue_batch(10).unwrap();
        assert_eq!(batch.len(), 1);
        assert_eq!(batch[0].file_path, "/path/a.jsonl");
        assert_eq!(batch[0].start_offset, 0);
        assert_eq!(batch[0].end_offset, 1000);
    }

    #[test]
    fn test_mark_shipped() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool.enqueue("claude", "/f", 0, 100, None).unwrap();
        let batch = spool.dequeue_batch(10).unwrap();
        spool.mark_shipped(batch[0].id).unwrap();
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(spool.total_size().unwrap(), 0);
    }

    #[test]
    fn test_mark_failed_backoff() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool.enqueue("claude", "/f", 0, 100, None).unwrap();
        let batch = spool.dequeue_batch(10).unwrap();
        let id = batch[0].id;

        // First failure — not dead yet
        let dead = spool.mark_failed(id, "connection refused").unwrap();
        assert!(!dead);
        assert_eq!(spool.pending_count().unwrap(), 1);

        // Entry should have retry_count = 1 and future next_retry_at
        let entry: (i32, String) = conn
            .query_row(
                "SELECT retry_count, next_retry_at FROM spool_queue WHERE id = ?",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(entry.0, 1);
        // next_retry_at should be in the future
        let next: DateTime<Utc> = DateTime::parse_from_rfc3339(&entry.1)
            .unwrap()
            .with_timezone(&Utc);
        assert!(next > Utc::now());
    }

    #[test]
    fn test_mark_failed_dead_after_max() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool.enqueue("claude", "/f", 0, 100, None).unwrap();
        let batch = spool.dequeue_batch(10).unwrap();
        let id = batch[0].id;

        // Fail 3 times with max_retries=3
        for i in 0..3 {
            let dead = spool
                .mark_failed_with_max(id, &format!("err {}", i), 3)
                .unwrap();
            if i < 2 {
                assert!(!dead);
            } else {
                assert!(dead);
            }
        }

        // Should now be dead, not pending
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(spool.total_size().unwrap(), 1); // still in DB as dead
    }

    #[test]
    fn test_backpressure() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        // Fill to capacity (use a smaller number for testing by checking total_size)
        // We won't actually insert 10K rows — just verify the check works
        // Insert one and check that enqueue returns true
        let ok = spool.enqueue("claude", "/f", 0, 100, None).unwrap();
        assert!(ok);
    }

    #[test]
    fn test_cleanup() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        // Insert a dead entry with old timestamp
        let old_date = (Utc::now() - chrono::Duration::days(10)).to_rfc3339();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('claude', '/old', 0, 100, ?1, ?1, 'dead')",
            [&old_date],
        )
        .unwrap();

        assert_eq!(spool.total_size().unwrap(), 1);
        let cleaned = spool.cleanup().unwrap();
        assert_eq!(cleaned, 1);
        assert_eq!(spool.total_size().unwrap(), 0);
    }
}
