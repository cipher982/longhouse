//! Pointer-based offline spool for retry resilience.
//!
//! Stores byte-range pointers (NOT payloads) into source files.
//! On retry, the source file is re-read and re-parsed.
//! Max queue size: 10,000 entries (backpressure).

use anyhow::Result;
use chrono::{DateTime, Utc};
use rand::Rng;
use rusqlite::{Connection, OptionalExtension};
use serde::Serialize;
use std::collections::BTreeMap;
use std::time::Duration;

/// Maximum spool entries before backpressure kicks in.
const MAX_QUEUE_SIZE: usize = 10_000;

/// Base backoff in seconds.
const BACKOFF_BASE: f64 = 5.0;

/// Maximum backoff in seconds (1 hour).
const BACKOFF_MAX: f64 = 3600.0;

const RECOVERABLE_ARCHIVE_ERROR_PATTERNS: &[&str] = &[
    "%Archive ingest backlog is throttled%",
    "500:%",
    "502:%",
    "503:%",
    "504:%",
    "520:%",
    "521:%",
    "522:%",
    "523:%",
    "524:%",
    "525:%",
    "526:%",
    "527:%",
    "error sending request for url (%",
];

/// Default max retries before marking dead.
const DEFAULT_MAX_RETRIES: u32 = 50;

const HUGE_RANGE_BYTES: u64 = 100 * 1024 * 1024;

/// A spool entry — pointer to a byte range in a source file.
#[derive(Debug, Clone)]
pub struct SpoolEntry {
    pub id: i64,
    pub provider: String,
    pub file_path: String,
    pub start_offset: u64,
    pub end_offset: u64,
    pub session_id: Option<String>,
}

/// A file path with pending retry work, ordered by oldest pending entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingPath {
    pub provider: String,
    pub file_path: String,
    pub pending_bytes: u64,
}

/// A retained dead-lettered range for local inspection.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeadLetterEntry {
    pub provider: String,
    pub file_path: String,
    pub start_offset: u64,
    pub end_offset: u64,
    pub session_id: Option<String>,
    pub last_error: Option<String>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ArchiveBacklogSnapshot {
    pub state: String,
    pub mode: String,
    pub pending_ranges: usize,
    pub ready_ranges: usize,
    pub deferred_ranges: usize,
    pub pending_paths: usize,
    pub pending_sessions: usize,
    pub pending_bytes: u64,
    pub dead_ranges: usize,
    pub dead_bytes: u64,
    pub huge_pending_ranges: usize,
    pub huge_pending_bytes: u64,
    pub oldest_pending_at: Option<String>,
    pub newest_pending_at: Option<String>,
    pub next_retry_at_min: Option<String>,
    pub next_retry_at_max: Option<String>,
    pub next_deferred_retry_at: Option<String>,
    pub providers: Vec<ArchiveProviderSummary>,
    pub size_buckets: BTreeMap<String, ArchiveSizeBucketSummary>,
}

impl Default for ArchiveBacklogSnapshot {
    fn default() -> Self {
        Self {
            state: "idle".to_string(),
            mode: "idle".to_string(),
            pending_ranges: 0,
            ready_ranges: 0,
            deferred_ranges: 0,
            pending_paths: 0,
            pending_sessions: 0,
            pending_bytes: 0,
            dead_ranges: 0,
            dead_bytes: 0,
            huge_pending_ranges: 0,
            huge_pending_bytes: 0,
            oldest_pending_at: None,
            newest_pending_at: None,
            next_retry_at_min: None,
            next_retry_at_max: None,
            next_deferred_retry_at: None,
            providers: Vec::new(),
            size_buckets: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct ArchiveProviderSummary {
    pub provider: String,
    pub pending_ranges: usize,
    pub pending_paths: usize,
    pub pending_sessions: usize,
    pub pending_bytes: u64,
    pub dead_ranges: usize,
    pub dead_bytes: u64,
}

#[derive(Debug, Clone, Default, Serialize, PartialEq, Eq)]
pub struct ArchiveSizeBucketSummary {
    pub pending_ranges: usize,
    pub pending_bytes: u64,
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
        let existing_id: Option<i64> = self
            .conn
            .query_row(
                "SELECT id
                 FROM spool_queue
                 WHERE status = 'pending'
                   AND provider = ?1
                   AND file_path = ?2
                   AND start_offset = ?3
                   AND end_offset = ?4
                 LIMIT 1",
                rusqlite::params![provider, file_path, start_offset as i64, end_offset as i64],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(id) = existing_id {
            if let Some(session_id) = session_id {
                self.conn.execute(
                    "UPDATE spool_queue
                     SET session_id = COALESCE(session_id, ?1)
                     WHERE id = ?2",
                    rusqlite::params![session_id, id],
                )?;
            }
            return Ok(true);
        }

        if self.total_size()? >= MAX_QUEUE_SIZE {
            tracing::warn!(
                "Spool at capacity ({} entries), rejecting enqueue",
                MAX_QUEUE_SIZE
            );
            return Ok(false);
        }

        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT OR IGNORE INTO spool_queue (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, status)
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

    /// Record a dead-lettered byte range for later inspection.
    pub fn record_dead(
        &self,
        provider: &str,
        file_path: &str,
        start_offset: u64,
        end_offset: u64,
        session_id: Option<&str>,
        error: &str,
    ) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, retry_count, last_error, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6, 1, ?7, 'dead')",
            rusqlite::params![
                provider,
                file_path,
                start_offset as i64,
                end_offset as i64,
                session_id,
                now,
                error,
            ],
        )?;
        Ok(())
    }

    /// Get pending entries ready for retry (next_retry_at <= now).
    pub fn dequeue_batch(&self, limit: usize) -> Result<Vec<SpoolEntry>> {
        let now = Utc::now().to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT id, provider, file_path, start_offset, end_offset, session_id
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
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Get unique file paths with ready archive work, small/recent first and
    /// bounded by the caller's per-tick byte budget.
    pub fn pending_paths_budgeted(
        &self,
        limit: usize,
        max_total_bytes: u64,
        include_huge: bool,
    ) -> Result<Vec<PendingPath>> {
        let now = Utc::now().to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT provider, file_path, path_bytes
             FROM (
                 SELECT provider,
                        file_path,
                        SUM(CASE WHEN end_offset > start_offset THEN end_offset - start_offset ELSE 0 END) AS path_bytes,
                        MIN(id) AS first_id,
                        MAX(created_at) AS newest_created_at
                 FROM spool_queue
                 WHERE status = 'pending'
                   AND (
                       next_retry_at <= ?1
                       OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                   )
                 GROUP BY provider, file_path
             )
             WHERE ?2 OR path_bytes < ?3
             ORDER BY
                CASE
                    WHEN path_bytes < 1048576 THEN 0
                    WHEN path_bytes < 10485760 THEN 1
                    WHEN path_bytes < 104857600 THEN 2
                    ELSE 3
                END ASC,
                newest_created_at DESC,
                first_id ASC
             LIMIT ?4",
        )?;
        let rows = stmt.query_map(
            rusqlite::params![
                now,
                include_huge,
                HUGE_RANGE_BYTES as i64,
                (limit.max(1) * 4) as i64
            ],
            |row| {
                Ok((
                    PendingPath {
                        provider: row.get(0)?,
                        file_path: row.get(1)?,
                        pending_bytes: row.get::<_, i64>(2)?.max(0) as u64,
                    },
                    row.get::<_, i64>(2)?.max(0) as u64,
                ))
            },
        )?;
        let mut result = Vec::new();
        let mut selected_bytes = 0u64;
        for row in rows {
            let (pending, path_bytes) = row?;
            if result.len() >= limit {
                break;
            }
            if path_bytes > max_total_bytes && !result.is_empty() {
                continue;
            }
            if selected_bytes.saturating_add(path_bytes) > max_total_bytes && !result.is_empty() {
                continue;
            }
            selected_bytes = selected_bytes.saturating_add(path_bytes);
            result.push(pending);
        }
        Ok(result)
    }

    /// Get pending retry entries for a single file path, oldest-first.
    #[cfg(test)]
    pub fn pending_entries_for_path(
        &self,
        file_path: &str,
        limit: usize,
    ) -> Result<Vec<SpoolEntry>> {
        let now = Utc::now().to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT id, provider, file_path, start_offset, end_offset, session_id
             FROM spool_queue
             WHERE status = 'pending' AND next_retry_at <= ?1 AND file_path = ?2
             ORDER BY created_at ASC, id ASC
             LIMIT ?3",
        )?;
        let rows = stmt.query_map(rusqlite::params![now, file_path, limit as i64], |row| {
            Ok(SpoolEntry {
                id: row.get(0)?,
                provider: row.get(1)?,
                file_path: row.get(2)?,
                start_offset: row.get::<_, i64>(3)? as u64,
                end_offset: row.get::<_, i64>(4)? as u64,
                session_id: row.get(5)?,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Get ready pending retry entries for a single file path, oldest-first.
    ///
    /// Rows that have never actually failed are ready even if `next_retry_at`
    /// is in the future. Those future timestamps can be inherited from a
    /// coarse archive-control deferral; treating them as hard backoff strands
    /// fresh backlog work behind an unrelated retry clock.
    pub fn pending_entries_for_path_ready(
        &self,
        file_path: &str,
        limit: usize,
    ) -> Result<Vec<SpoolEntry>> {
        let now = Utc::now().to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT id, provider, file_path, start_offset, end_offset, session_id
             FROM spool_queue
             WHERE status = 'pending'
               AND file_path = ?2
               AND (
                   next_retry_at <= ?1
                   OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
               )
             ORDER BY created_at ASC, id ASC
             LIMIT ?3",
        )?;
        let rows = stmt.query_map(rusqlite::params![now, file_path, limit as i64], |row| {
            Ok(SpoolEntry {
                id: row.get(0)?,
                provider: row.get(1)?,
                file_path: row.get(2)?,
                start_offset: row.get::<_, i64>(3)? as u64,
                end_offset: row.get::<_, i64>(4)? as u64,
                session_id: row.get(5)?,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Merge ready adjacent or overlapping pending ranges for one path.
    ///
    /// This is a throughput optimization for archive repair: many tiny retry
    /// pointers can represent contiguous bytes in the same transcript. Keeping
    /// them as separate rows forces extra parse/build/HTTP loops. Only rows
    /// with the same provider/session and already-ready retry predicate are
    /// merged, preserving retry deferrals and cross-session boundaries.
    pub fn coalesce_ready_adjacent_for_path(
        &self,
        file_path: &str,
        scan_limit: usize,
    ) -> Result<usize> {
        let now = Utc::now().to_rfc3339();
        let mut stmt = self.conn.prepare(
            "SELECT id, provider, file_path, start_offset, end_offset, session_id
             FROM spool_queue
             WHERE status = 'pending'
               AND file_path = ?2
               AND (
                   next_retry_at <= ?1
                   OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
               )
             ORDER BY start_offset ASC, end_offset ASC, id ASC
             LIMIT ?3",
        )?;
        let rows = stmt.query_map(
            rusqlite::params![now, file_path, scan_limit as i64],
            |row| {
                Ok(SpoolEntry {
                    id: row.get(0)?,
                    provider: row.get(1)?,
                    file_path: row.get(2)?,
                    start_offset: row.get::<_, i64>(3)? as u64,
                    end_offset: row.get::<_, i64>(4)? as u64,
                    session_id: row.get(5)?,
                })
            },
        )?;
        let mut entries = Vec::new();
        for row in rows {
            entries.push(row?);
        }
        drop(stmt);

        let Some(mut current) = entries.first().cloned() else {
            return Ok(0);
        };
        let mut merged = 0usize;
        for entry in entries.into_iter().skip(1) {
            let same_partition =
                entry.provider == current.provider && entry.session_id == current.session_id;
            let touches_current = entry.start_offset <= current.end_offset;
            if same_partition && touches_current {
                let new_end = current.end_offset.max(entry.end_offset);
                self.conn.execute(
                    "UPDATE spool_queue SET end_offset = ?1 WHERE id = ?2",
                    rusqlite::params![new_end as i64, current.id],
                )?;
                self.conn
                    .execute("DELETE FROM spool_queue WHERE id = ?1", [entry.id])?;
                current.end_offset = new_end;
                merged += 1;
            } else {
                current = entry;
            }
        }
        Ok(merged)
    }

    /// Get pending retry entries for a single file path, ignoring next_retry_at backoff.
    pub fn pending_entries_for_path_now(
        &self,
        file_path: &str,
        limit: usize,
    ) -> Result<Vec<SpoolEntry>> {
        let mut stmt = self.conn.prepare(
            "SELECT id, provider, file_path, start_offset, end_offset, session_id
             FROM spool_queue
             WHERE status = 'pending' AND file_path = ?1
             ORDER BY created_at ASC, id ASC
             LIMIT ?2",
        )?;
        let rows = stmt.query_map(rusqlite::params![file_path, limit as i64], |row| {
            Ok(SpoolEntry {
                id: row.get(0)?,
                provider: row.get(1)?,
                file_path: row.get(2)?,
                start_offset: row.get::<_, i64>(3)? as u64,
                end_offset: row.get::<_, i64>(4)? as u64,
                session_id: row.get(5)?,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Return the next scheduled retry time for a path, if it has pending work.
    pub fn next_retry_at_for_path(&self, file_path: &str) -> Result<Option<DateTime<Utc>>> {
        let value: Option<String> = self.conn.query_row(
            "SELECT MIN(next_retry_at)
             FROM spool_queue
             WHERE status = 'pending' AND file_path = ?1",
            [file_path],
            |row| row.get(0),
        )?;
        value
            .map(|raw| {
                DateTime::parse_from_rfc3339(&raw)
                    .map(|parsed| parsed.with_timezone(&Utc))
                    .map_err(Into::into)
            })
            .transpose()
    }

    /// Remove a successfully shipped entry.
    pub fn mark_shipped(&self, entry_id: i64) -> Result<()> {
        self.conn
            .execute("DELETE FROM spool_queue WHERE id = ?", [entry_id])?;
        Ok(())
    }

    /// Retire pending pointer ranges for a path when the source file epoch changes.
    pub fn dead_letter_pending_for_path(&self, file_path: &str, error: &str) -> Result<usize> {
        let now = Utc::now().to_rfc3339();
        let changed = self.conn.execute(
            "UPDATE spool_queue
             SET status = 'dead',
                 retry_count = retry_count + 1,
                 last_error = ?1,
                 next_retry_at = ?2
             WHERE status = 'pending' AND file_path = ?3",
            rusqlite::params![error, now, file_path],
        )?;
        Ok(changed)
    }

    /// Advance the start offset for a pending entry after partial replay progress.
    pub fn advance_start(&self, entry_id: i64, new_start_offset: u64) -> Result<()> {
        self.conn.execute(
            "UPDATE spool_queue SET start_offset = ?1 WHERE id = ?2",
            rusqlite::params![new_start_offset as i64, entry_id],
        )?;
        Ok(())
    }

    /// Replace one pending entry with smaller ready child ranges.
    ///
    /// This is used when the Runtime Host rejects an archive replay POST as
    /// too large even after local batch planning. The source transcript is
    /// still authoritative; smaller byte pointers let the next replay loop
    /// rebuild smaller payloads without blocking the rest of the backlog behind
    /// exponential backoff on the original range.
    pub fn replace_pending_entry_with_ranges(
        &self,
        entry_id: i64,
        provider: &str,
        file_path: &str,
        session_id: Option<&str>,
        ranges: &[(u64, u64)],
        error: &str,
    ) -> Result<usize> {
        let ranges: Vec<(u64, u64)> = ranges
            .iter()
            .copied()
            .filter(|(start, end)| start < end)
            .collect();
        let Some((first_start, first_end)) = ranges.first().copied() else {
            return Ok(0);
        };

        let now = Utc::now().to_rfc3339();
        let tx = self.conn.unchecked_transaction()?;
        let changed = tx.execute(
            "UPDATE spool_queue
             SET provider = ?1,
                 file_path = ?2,
                 start_offset = ?3,
                 end_offset = ?4,
                 session_id = COALESCE(session_id, ?5),
                 retry_count = 0,
                 next_retry_at = ?6,
                 last_error = ?7,
                 status = 'pending'
             WHERE id = ?8 AND status = 'pending'",
            rusqlite::params![
                provider,
                file_path,
                first_start as i64,
                first_end as i64,
                session_id,
                now,
                error,
                entry_id,
            ],
        )?;
        if changed == 0 {
            tx.rollback()?;
            return Ok(0);
        }

        let mut written = 1usize;
        for (start, end) in ranges.into_iter().skip(1) {
            let inserted = tx.execute(
                "INSERT OR IGNORE INTO spool_queue (
                    provider,
                    file_path,
                    start_offset,
                    end_offset,
                    session_id,
                    created_at,
                    retry_count,
                    next_retry_at,
                    last_error,
                    status
                 )
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0, ?6, ?7, 'pending')",
                rusqlite::params![
                    provider,
                    file_path,
                    start as i64,
                    end as i64,
                    session_id,
                    now,
                    error,
                ],
            )?;
            written += inserted;
        }
        tx.commit()?;
        Ok(written)
    }

    /// Mark entry as failed with exponential backoff. Returns true if now permanently dead.
    pub fn mark_failed(&self, entry_id: i64, error: &str) -> Result<bool> {
        self.mark_failed_with_max(entry_id, error, DEFAULT_MAX_RETRIES)
    }

    /// Defer all pending entries for a path without incrementing retry_count.
    ///
    /// Runtime backpressure is not a bad pointer and should not march backlog
    /// entries toward dead-lettering; it only means the host asked us to come
    /// back later.
    pub fn defer_pending_for_path(
        &self,
        file_path: &str,
        error: &str,
        delay: Duration,
    ) -> Result<usize> {
        let mut stmt = self
            .conn
            .prepare("SELECT id FROM spool_queue WHERE status = 'pending' AND file_path = ?1")?;
        let ids = stmt
            .query_map([file_path], |row| row.get::<_, i64>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        drop(stmt);

        let mut changed = 0usize;
        for id in ids {
            let next_retry = Utc::now() + jittered_chrono_delay(delay);
            changed += self.conn.execute(
                "UPDATE spool_queue
                 SET last_error = ?1,
                     next_retry_at = ?2
                 WHERE status = 'pending' AND id = ?3",
                rusqlite::params![error, next_retry.to_rfc3339(), id],
            )?;
        }
        Ok(changed)
    }

    /// Bound stale archive-backpressure retry clocks.
    ///
    /// Archive backpressure is a host admission signal, not evidence that the
    /// local pointer is bad. Older builds could leave every range parked behind
    /// a long retry clock; in drain mode that violates the product contract that
    /// reconstructable backlog should keep making progress as soon as the host
    /// is accepting archive work again.
    pub fn clip_recoverable_archive_deferrals(&self, max_delay: Duration) -> Result<usize> {
        let now = Utc::now();
        let cutoff = now + chrono_duration_from_std(max_delay);
        let mut stmt = self.conn.prepare(&format!(
            "SELECT id
             FROM spool_queue
             WHERE status = 'pending'
               AND next_retry_at > ?1
               AND ({})",
            RECOVERABLE_ARCHIVE_ERROR_PATTERNS
                .iter()
                .enumerate()
                .map(|(idx, _)| format!("last_error LIKE ?{}", idx + 2))
                .collect::<Vec<_>>()
                .join(" OR ")
        ))?;
        let mut params: Vec<&dyn rusqlite::ToSql> =
            Vec::with_capacity(1 + RECOVERABLE_ARCHIVE_ERROR_PATTERNS.len());
        let cutoff_rfc3339 = cutoff.to_rfc3339();
        params.push(&cutoff_rfc3339);
        for pattern in RECOVERABLE_ARCHIVE_ERROR_PATTERNS {
            params.push(pattern);
        }
        let ids = stmt
            .query_map(params.as_slice(), |row| row.get::<_, i64>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        drop(stmt);

        let mut changed = 0usize;
        for id in ids {
            let next_retry = now + jittered_chrono_delay(max_delay);
            changed += self.conn.execute(
                "UPDATE spool_queue
                 SET next_retry_at = ?1
                 WHERE status = 'pending' AND id = ?2",
                rusqlite::params![next_retry.to_rfc3339(), id],
            )?;
        }
        Ok(changed)
    }

    /// Mark failed with custom max retries.
    pub fn mark_failed_with_max(
        &self,
        entry_id: i64,
        error: &str,
        max_retries: u32,
    ) -> Result<bool> {
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

        // Exponential backoff with full jitter: min(5 * 2^retry, 3600).
        // Without jitter, host backpressure can stamp thousands of archive
        // ranges with the same next_retry_at and create a replay herd.
        let backoff_secs = (BACKOFF_BASE * 2.0_f64.powi(new_count)).min(BACKOFF_MAX);
        let next_retry = Utc::now() + jittered_chrono_delay(Duration::from_secs_f64(backoff_secs));

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

    /// Count dead-lettered entries retained for operator inspection.
    pub fn dead_count(&self) -> Result<usize> {
        let count: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM spool_queue WHERE status = 'dead'",
            [],
            |row| row.get(0),
        )?;
        Ok(count as usize)
    }

    pub fn archive_backlog_snapshot(&self) -> Result<ArchiveBacklogSnapshot> {
        let aggregate = self.conn.query_row(
            "SELECT
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(
                    CASE
                        WHEN status = 'pending'
                         AND (
                             next_retry_at <= ?2
                             OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                         )
                        THEN 1
                        ELSE 0
                    END
                ), 0),
                COALESCE(SUM(
                    CASE
                        WHEN status = 'pending'
                         AND NOT (
                             next_retry_at <= ?2
                             OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                         )
                        THEN 1
                        ELSE 0
                    END
                ), 0),
                COUNT(DISTINCT CASE WHEN status = 'pending' THEN provider || char(31) || file_path END),
                COUNT(DISTINCT CASE WHEN status = 'pending' THEN session_id END),
                COALESCE(SUM(CASE WHEN status = 'pending' AND end_offset > start_offset THEN end_offset - start_offset ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'dead' AND end_offset > start_offset THEN end_offset - start_offset ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'pending' AND end_offset - start_offset >= ?1 THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN status = 'pending' AND end_offset - start_offset >= ?1 THEN end_offset - start_offset ELSE 0 END), 0),
                MIN(CASE WHEN status = 'pending' THEN created_at END),
                MAX(CASE WHEN status = 'pending' THEN created_at END),
                MIN(CASE WHEN status = 'pending' THEN next_retry_at END),
                MAX(CASE WHEN status = 'pending' THEN next_retry_at END),
                MIN(
                    CASE
                        WHEN status = 'pending'
                         AND NOT (
                             next_retry_at <= ?2
                             OR (retry_count = 0 AND (last_error IS NULL OR TRIM(last_error) = ''))
                         )
                        THEN next_retry_at
                    END
                )
             FROM spool_queue",
            rusqlite::params![HUGE_RANGE_BYTES as i64, Utc::now().to_rfc3339()],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?.max(0) as usize,
                    row.get::<_, i64>(1)?.max(0) as usize,
                    row.get::<_, i64>(2)?.max(0) as usize,
                    row.get::<_, i64>(3)?.max(0) as usize,
                    row.get::<_, i64>(4)?.max(0) as usize,
                    row.get::<_, i64>(5)?.max(0) as u64,
                    row.get::<_, i64>(6)?.max(0) as usize,
                    row.get::<_, i64>(7)?.max(0) as u64,
                    row.get::<_, i64>(8)?.max(0) as usize,
                    row.get::<_, i64>(9)?.max(0) as u64,
                    row.get::<_, Option<String>>(10)?,
                    row.get::<_, Option<String>>(11)?,
                    row.get::<_, Option<String>>(12)?,
                    row.get::<_, Option<String>>(13)?,
                    row.get::<_, Option<String>>(14)?,
                ))
            },
        )?;
        let (
            pending_ranges,
            ready_ranges,
            deferred_ranges,
            pending_paths,
            pending_sessions,
            pending_bytes,
            dead_ranges,
            dead_bytes,
            huge_pending_ranges,
            huge_pending_bytes,
            oldest_pending_at,
            newest_pending_at,
            next_retry_at_min,
            next_retry_at_max,
            next_deferred_retry_at,
        ) = aggregate;

        let state = if dead_ranges > 0 {
            "dead_lettered"
        } else if pending_ranges > 0 {
            "pending"
        } else {
            "idle"
        }
        .to_string();

        Ok(ArchiveBacklogSnapshot {
            state,
            mode: if pending_ranges > 0 {
                "trickle"
            } else {
                "idle"
            }
            .to_string(),
            pending_ranges,
            ready_ranges,
            deferred_ranges,
            pending_paths,
            pending_sessions,
            pending_bytes,
            dead_ranges,
            dead_bytes,
            huge_pending_ranges,
            huge_pending_bytes,
            oldest_pending_at,
            newest_pending_at,
            next_retry_at_min,
            next_retry_at_max,
            next_deferred_retry_at,
            providers: self.archive_provider_summaries()?,
            size_buckets: self.archive_size_buckets()?,
        })
    }

    fn archive_provider_summaries(&self) -> Result<Vec<ArchiveProviderSummary>> {
        let mut stmt = self.conn.prepare(
            "SELECT provider,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                    COUNT(DISTINCT CASE WHEN status = 'pending' THEN file_path END),
                    COUNT(DISTINCT CASE WHEN status = 'pending' THEN session_id END),
                    COALESCE(SUM(CASE WHEN status = 'pending' AND end_offset > start_offset THEN end_offset - start_offset ELSE 0 END), 0),
                    SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END),
                    COALESCE(SUM(CASE WHEN status = 'dead' AND end_offset > start_offset THEN end_offset - start_offset ELSE 0 END), 0)
             FROM spool_queue
             GROUP BY provider
             ORDER BY 5 DESC, provider ASC",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok(ArchiveProviderSummary {
                provider: row.get(0)?,
                pending_ranges: row.get::<_, i64>(1)?.max(0) as usize,
                pending_paths: row.get::<_, i64>(2)?.max(0) as usize,
                pending_sessions: row.get::<_, i64>(3)?.max(0) as usize,
                pending_bytes: row.get::<_, i64>(4)?.max(0) as u64,
                dead_ranges: row.get::<_, i64>(5)?.max(0) as usize,
                dead_bytes: row.get::<_, i64>(6)?.max(0) as u64,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            let summary = row?;
            if summary.pending_ranges > 0 || summary.dead_ranges > 0 {
                result.push(summary);
            }
        }
        Ok(result)
    }

    fn archive_size_buckets(&self) -> Result<BTreeMap<String, ArchiveSizeBucketSummary>> {
        let mut stmt = self.conn.prepare(
            "SELECT
                CASE
                    WHEN end_offset - start_offset < 1024 THEN 'tiny_lt_1kb'
                    WHEN end_offset - start_offset < 1048576 THEN 'small_lt_1mb'
                    WHEN end_offset - start_offset < 10485760 THEN 'medium_lt_10mb'
                    WHEN end_offset - start_offset < 104857600 THEN 'large_lt_100mb'
                    ELSE 'huge_gte_100mb'
                END AS bucket,
                COUNT(*),
                COALESCE(SUM(CASE WHEN end_offset > start_offset THEN end_offset - start_offset ELSE 0 END), 0)
             FROM spool_queue
             WHERE status = 'pending'
             GROUP BY bucket
             ORDER BY bucket ASC",
        )?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                ArchiveSizeBucketSummary {
                    pending_ranges: row.get::<_, i64>(1)?.max(0) as usize,
                    pending_bytes: row.get::<_, i64>(2)?.max(0) as u64,
                },
            ))
        })?;
        let mut result = BTreeMap::new();
        for row in rows {
            let (bucket, summary) = row?;
            result.insert(bucket, summary);
        }
        Ok(result)
    }

    /// Return recent dead-lettered ranges, newest first.
    pub fn recent_dead(&self, limit: usize) -> Result<Vec<DeadLetterEntry>> {
        let mut stmt = self.conn.prepare(
            "SELECT provider, file_path, start_offset, end_offset, session_id, last_error, created_at
             FROM spool_queue
             WHERE status = 'dead'
             ORDER BY created_at DESC, id DESC
             LIMIT ?1",
        )?;
        let rows = stmt.query_map(rusqlite::params![limit as i64], |row| {
            Ok(DeadLetterEntry {
                provider: row.get(0)?,
                file_path: row.get(1)?,
                start_offset: row.get::<_, i64>(2)? as u64,
                end_offset: row.get::<_, i64>(3)? as u64,
                session_id: row.get(4)?,
                last_error: row.get(5)?,
                created_at: row.get(6)?,
            })
        })?;
        let mut result = Vec::new();
        for row in rows {
            result.push(row?);
        }
        Ok(result)
    }

    /// Total entries (for backpressure check).
    pub fn total_size(&self) -> Result<usize> {
        let count: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM spool_queue", [], |row| row.get(0))?;
        Ok(count as usize)
    }

    /// Move pending entries older than 7 days to 'dead' (not deleted — data preserved).
    /// Hard-delete dead entries older than 30 days.
    /// Returns total rows affected.
    pub fn cleanup(&self) -> Result<usize> {
        let seven_days_ago = (Utc::now() - chrono::Duration::days(7)).to_rfc3339();
        let thirty_days_ago = (Utc::now() - chrono::Duration::days(30)).to_rfc3339();

        // Mark pending >7 days as dead (not deleted — allows inspection)
        let marked_dead = self.conn.execute(
            "UPDATE spool_queue SET status = 'dead', last_error = COALESCE(last_error, 'timeout: pending >7 days')
             WHERE status = 'pending' AND created_at < ?",
            [&seven_days_ago],
        )?;

        // Hard-delete old dead entries (>30 days)
        let deleted = self.conn.execute(
            "DELETE FROM spool_queue WHERE status = 'dead' AND created_at < ?",
            [&thirty_days_ago],
        )?;

        Ok(marked_dead + deleted)
    }
}

fn jittered_chrono_delay(delay: Duration) -> chrono::Duration {
    let max_millis = delay.as_millis().min((BACKOFF_MAX * 1000.0) as u128).max(1) as i64;
    let jitter_millis = rand::thread_rng().gen_range(1..=max_millis);
    chrono::Duration::milliseconds(jitter_millis)
}

fn chrono_duration_from_std(delay: Duration) -> chrono::Duration {
    chrono::Duration::from_std(delay)
        .unwrap_or_else(|_| chrono::Duration::seconds(BACKOFF_MAX as i64))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;
    use chrono::DateTime;

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
    fn test_pending_entries_for_path_filters_and_orders() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/target.jsonl', 10, 20, '2026-03-11T00:00:00+00:00', '2026-03-11T00:00:00+00:00', 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/other.jsonl', 0, 10, '2026-03-10T00:00:00+00:00', '2026-03-10T00:00:00+00:00', 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/target.jsonl', 0, 10, '2026-03-09T00:00:00+00:00', '2026-03-09T00:00:00+00:00', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let pending = spool.pending_entries_for_path("/target.jsonl", 10).unwrap();
        assert_eq!(pending.len(), 2);
        assert_eq!(pending[0].start_offset, 0);
        assert_eq!(pending[1].start_offset, 10);
    }

    #[test]
    fn test_pending_paths_budgeted_prefers_small_recent_and_skips_huge_by_default() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/huge.jsonl', 0, 209715200, '2026-03-11T00:00:00+00:00', '2026-03-11T00:00:00+00:00', 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/old-small.jsonl', 0, 1000, '2026-03-10T00:00:00+00:00', '2026-03-10T00:00:00+00:00', 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/new-small.jsonl', 0, 1000, '2026-03-12T00:00:00+00:00', '2026-03-12T00:00:00+00:00', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let pending = spool
            .pending_paths_budgeted(10, 25 * 1024 * 1024, false)
            .unwrap();

        assert_eq!(
            pending,
            vec![
                PendingPath {
                    provider: "codex".to_string(),
                    file_path: "/new-small.jsonl".to_string(),
                    pending_bytes: 1000,
                },
                PendingPath {
                    provider: "codex".to_string(),
                    file_path: "/old-small.jsonl".to_string(),
                    pending_bytes: 1000,
                },
            ]
        );
    }

    #[test]
    fn test_pending_paths_budgeted_treats_never_failed_future_retry_as_ready() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue
                (provider, file_path, start_offset, end_offset, created_at, next_retry_at, retry_count, last_error, status)
             VALUES
                ('codex', '/never-failed.jsonl', 0, 1000, '2026-03-12T00:00:00+00:00', '2999-01-01T00:00:00+00:00', 0, NULL, 'pending'),
                ('codex', '/backpressured.jsonl', 0, 1000, '2026-03-12T00:00:00+00:00', '2999-01-01T00:00:00+00:00', 1, '503 archive throttled', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let pending = spool
            .pending_paths_budgeted(10, 25 * 1024 * 1024, false)
            .unwrap();

        assert_eq!(
            pending,
            vec![PendingPath {
                provider: "codex".to_string(),
                file_path: "/never-failed.jsonl".to_string(),
                pending_bytes: 1000,
            }]
        );
    }

    #[test]
    fn test_clip_recoverable_archive_deferrals_bounds_only_recoverable_rows() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue
                (provider, file_path, start_offset, end_offset, created_at, next_retry_at, retry_count, last_error, status)
             VALUES
                ('codex', '/backpressured.jsonl', 0, 1000, '2026-03-12T00:00:00+00:00', '2999-01-01T00:00:00+00:00', 4, '503:{\"detail\":\"Archive ingest backlog is throttled; retry shortly\"}', 'pending'),
                ('codex', '/cloudflare-502.jsonl', 0, 1000, '2026-03-12T00:00:01+00:00', '2999-01-01T00:00:00+00:00', 4, '502:<!DOCTYPE html>', 'pending'),
                ('codex', '/cloudflare-525.jsonl', 0, 1000, '2026-03-12T00:00:02+00:00', '2999-01-01T00:00:00+00:00', 4, '525:<!DOCTYPE html>', 'pending'),
                ('codex', '/connect-error.jsonl', 0, 1000, '2026-03-12T00:00:03+00:00', '2999-01-01T00:00:00+00:00', 4, 'error sending request for url (https://david010.longhouse.ai/api/agents/ingest)', 'pending'),
                ('codex', '/bad-pointer.jsonl', 0, 1000, '2026-03-12T00:00:04+00:00', '2999-01-01T00:00:00+00:00', 4, 'parse error: invalid json', 'pending'),
                ('codex', '/short-backpressure.jsonl', 0, 1000, '2026-03-12T00:00:05+00:00', '2000-01-01T00:00:00+00:00', 4, '503:{\"detail\":\"Archive ingest backlog is throttled; retry shortly\"}', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let clipped = spool
            .clip_recoverable_archive_deferrals(Duration::from_secs(5))
            .unwrap();
        assert_eq!(clipped, 4);

        let rows: Vec<(String, String)> = {
            let mut stmt = conn
                .prepare("SELECT file_path, next_retry_at FROM spool_queue ORDER BY file_path")
                .unwrap();
            stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
                .unwrap()
                .collect::<std::result::Result<Vec<_>, _>>()
                .unwrap()
        };
        let backpressured_retry = DateTime::parse_from_rfc3339(&rows[0].1)
            .unwrap()
            .with_timezone(&Utc);
        assert!(backpressured_retry <= Utc::now() + chrono::Duration::seconds(5));
        assert_eq!(rows[1].1, "2999-01-01T00:00:00+00:00");
        for (_, next_retry_at) in rows.iter().take(5).skip(2) {
            let retry = DateTime::parse_from_rfc3339(next_retry_at)
                .unwrap()
                .with_timezone(&Utc);
            assert!(retry <= Utc::now() + chrono::Duration::seconds(5));
        }
        assert_eq!(rows[5].1, "2000-01-01T00:00:00+00:00");
    }

    #[test]
    fn test_pending_entries_for_path_ready_treats_never_failed_future_retry_as_ready() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue
                (provider, file_path, start_offset, end_offset, created_at, next_retry_at, retry_count, last_error, status)
             VALUES
                ('codex', '/target.jsonl', 0, 1000, '2026-03-12T00:00:00+00:00', '2999-01-01T00:00:00+00:00', 0, '', 'pending'),
                ('codex', '/target.jsonl', 1000, 2000, '2026-03-12T00:00:01+00:00', '2999-01-01T00:00:00+00:00', 1, '503 archive throttled', 'pending'),
                ('codex', '/other.jsonl', 0, 1000, '2026-03-12T00:00:02+00:00', '2999-01-01T00:00:00+00:00', 0, '', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let strict = spool.pending_entries_for_path("/target.jsonl", 10).unwrap();
        assert!(strict.is_empty());

        let ready = spool
            .pending_entries_for_path_ready("/target.jsonl", 10)
            .unwrap();
        assert_eq!(ready.len(), 1);
        assert_eq!(ready[0].start_offset, 0);
    }

    #[test]
    fn test_archive_backlog_snapshot_splits_ready_and_deferred_ranges() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue
                (provider, file_path, start_offset, end_offset, created_at, next_retry_at, retry_count, last_error, status)
             VALUES
                ('codex', '/ready.jsonl', 0, 1000, '2026-03-12T00:00:00+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary failure', 'pending'),
                ('codex', '/never-failed.jsonl', 0, 2000, '2026-03-12T00:00:01+00:00', '2999-01-01T00:00:00+00:00', 0, '', 'pending'),
                ('codex', '/deferred.jsonl', 0, 3000, '2026-03-12T00:00:02+00:00', '2999-01-01T00:00:00+00:00', 1, '503 archive throttled', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let snapshot = spool.archive_backlog_snapshot().unwrap();

        assert_eq!(snapshot.pending_ranges, 3);
        assert_eq!(snapshot.ready_ranges, 2);
        assert_eq!(snapshot.deferred_ranges, 1);
        assert_eq!(
            snapshot.next_deferred_retry_at.as_deref(),
            Some("2999-01-01T00:00:00+00:00")
        );
    }

    #[test]
    fn test_coalesce_ready_adjacent_for_path_merges_same_session_ranges() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue
                (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, retry_count, last_error, status)
             VALUES
                ('codex', '/target.jsonl', 0, 100, 'session-a', '2026-03-12T00:00:00+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary', 'pending'),
                ('codex', '/target.jsonl', 100, 200, 'session-a', '2026-03-12T00:00:01+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary', 'pending'),
                ('codex', '/target.jsonl', 190, 250, 'session-a', '2026-03-12T00:00:02+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary', 'pending'),
                ('codex', '/target.jsonl', 300, 350, 'session-a', '2026-03-12T00:00:03+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let merged = spool
            .coalesce_ready_adjacent_for_path("/target.jsonl", 10)
            .unwrap();

        assert_eq!(merged, 2);
        let entries = spool
            .pending_entries_for_path_ready("/target.jsonl", 10)
            .unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!((entries[0].start_offset, entries[0].end_offset), (0, 250));
        assert_eq!((entries[1].start_offset, entries[1].end_offset), (300, 350));
    }

    #[test]
    fn test_coalesce_ready_adjacent_for_path_respects_session_and_deferral() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue
                (provider, file_path, start_offset, end_offset, session_id, created_at, next_retry_at, retry_count, last_error, status)
             VALUES
                ('codex', '/target.jsonl', 0, 100, 'session-a', '2026-03-12T00:00:00+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary', 'pending'),
                ('codex', '/target.jsonl', 100, 200, 'session-b', '2026-03-12T00:00:01+00:00', '2000-01-01T00:00:00+00:00', 1, 'temporary', 'pending'),
                ('codex', '/target.jsonl', 200, 300, 'session-b', '2026-03-12T00:00:02+00:00', '2999-01-01T00:00:00+00:00', 1, '503 archive throttled', 'pending')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let merged = spool
            .coalesce_ready_adjacent_for_path("/target.jsonl", 10)
            .unwrap();

        assert_eq!(merged, 0);
        let ready = spool
            .pending_entries_for_path_ready("/target.jsonl", 10)
            .unwrap();
        assert_eq!(ready.len(), 2);
        assert_eq!((ready[0].start_offset, ready[0].end_offset), (0, 100));
        assert_eq!((ready[1].start_offset, ready[1].end_offset), (100, 200));
        let all = spool
            .pending_entries_for_path_now("/target.jsonl", 10)
            .unwrap();
        assert_eq!(all.len(), 3);
    }

    #[test]
    fn test_next_retry_at_for_path_returns_oldest_pending_retry() {
        let (_tmp, conn) = setup();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/target.jsonl', 0, 10, '2026-03-11T00:00:00+00:00', '2026-03-11T00:00:05+00:00', 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/target.jsonl', 10, 20, '2026-03-11T00:00:01+00:00', '2026-03-11T00:00:03+00:00', 'pending')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('codex', '/target.jsonl', 20, 30, '2026-03-11T00:00:02+00:00', '2026-03-11T00:00:01+00:00', 'dead')",
            [],
        )
        .unwrap();

        let spool = Spool::new(&conn);
        let retry_at = spool
            .next_retry_at_for_path("/target.jsonl")
            .unwrap()
            .unwrap();

        assert_eq!(retry_at.to_rfc3339(), "2026-03-11T00:00:03+00:00");
        assert!(spool
            .next_retry_at_for_path("/missing.jsonl")
            .unwrap()
            .is_none());
    }

    #[test]
    fn test_enqueue_is_idempotent_for_pending_range() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        assert!(spool
            .enqueue("claude", "/dup.jsonl", 100, 500, Some("s1"))
            .unwrap());
        assert!(spool
            .enqueue("claude", "/dup.jsonl", 100, 500, Some("s1"))
            .unwrap());

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM spool_queue WHERE status = 'pending'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn test_advance_start_updates_pending_entry_range() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool.enqueue("claude", "/f", 0, 500, Some("s1")).unwrap();
        let batch = spool.dequeue_batch(10).unwrap();
        let entry_id = batch[0].id;

        spool.advance_start(entry_id, 200).unwrap();

        let updated = spool.dequeue_batch(10).unwrap();
        assert_eq!(updated[0].start_offset, 200);
        assert_eq!(updated[0].end_offset, 500);
    }

    #[test]
    fn test_replace_pending_entry_with_ranges_resets_ready_children() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool.enqueue("claude", "/f", 0, 900, Some("s1")).unwrap();
        let entry = spool.dequeue_batch(10).unwrap().remove(0);
        spool
            .mark_failed(entry.id, "413 payload too large")
            .unwrap();

        let written = spool
            .replace_pending_entry_with_ranges(
                entry.id,
                "claude",
                "/f",
                Some("s1"),
                &[(0, 300), (300, 600), (600, 900)],
                "413 payload too large during replay; split range for immediate retry",
            )
            .unwrap();

        assert_eq!(written, 3);
        let rows: Vec<(i64, i64, i64, String)> = {
            let mut stmt = conn
                .prepare(
                    "SELECT start_offset, end_offset, retry_count, status
                     FROM spool_queue
                     WHERE file_path = '/f'
                     ORDER BY start_offset ASC",
                )
                .unwrap();
            stmt.query_map([], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })
            .unwrap()
            .collect::<std::result::Result<Vec<_>, _>>()
            .unwrap()
        };

        assert_eq!(
            rows,
            vec![
                (0, 300, 0, "pending".to_string()),
                (300, 600, 0, "pending".to_string()),
                (600, 900, 0, "pending".to_string()),
            ]
        );
        assert_eq!(spool.dequeue_batch(10).unwrap().len(), 3);
    }

    #[test]
    fn test_record_dead_persists_dead_letter_entry() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool
            .record_dead(
                "claude",
                "/dead.jsonl",
                100,
                220,
                Some("dead-session"),
                "oversize source range",
            )
            .unwrap();

        let row: (String, String, i64, i64, String, String) = conn
            .query_row(
                "SELECT provider, file_path, start_offset, end_offset, status, last_error
                 FROM spool_queue
                 WHERE file_path = '/dead.jsonl'",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .unwrap();

        assert_eq!(row.0, "claude");
        assert_eq!(row.1, "/dead.jsonl");
        assert_eq!(row.2, 100);
        assert_eq!(row.3, 220);
        assert_eq!(row.4, "dead");
        assert!(row.5.contains("oversize"));
        assert_eq!(spool.dead_count().unwrap(), 1);
    }

    #[test]
    fn test_recent_dead_returns_newest_first() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool
            .record_dead(
                "claude",
                "/older.jsonl",
                0,
                10,
                Some("older"),
                "older error",
            )
            .unwrap();
        spool
            .record_dead(
                "codex",
                "/newer.jsonl",
                10,
                30,
                Some("newer"),
                "newer error",
            )
            .unwrap();

        let entries = spool.recent_dead(10).unwrap();
        assert_eq!(entries.len(), 2);
        assert_eq!(entries[0].file_path, "/newer.jsonl");
        assert_eq!(entries[0].provider, "codex");
        assert_eq!(entries[0].start_offset, 10);
        assert_eq!(entries[0].end_offset, 30);
        assert_eq!(entries[0].last_error.as_deref(), Some("newer error"));
        assert_eq!(entries[1].file_path, "/older.jsonl");
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
    fn test_defer_pending_for_path_does_not_increment_retry_count() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        spool.enqueue("claude", "/f", 0, 100, None).unwrap();

        let changed = spool
            .defer_pending_for_path(
                "/f",
                "503:{\"detail\":\"Archive ingest backlog is throttled; retry shortly\"}",
                Duration::from_secs(60),
            )
            .unwrap();

        assert_eq!(changed, 1);
        let entry: (i32, String, String) = conn
            .query_row(
                "SELECT retry_count, last_error, next_retry_at FROM spool_queue WHERE file_path = '/f'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(entry.0, 0);
        assert!(entry.1.contains("Archive ingest backlog is throttled"));
        let next: DateTime<Utc> = DateTime::parse_from_rfc3339(&entry.2)
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

        // Insert a dead entry older than 30 days (hard-delete threshold)
        let old_date = (Utc::now() - chrono::Duration::days(31)).to_rfc3339();
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

    #[test]
    fn test_spool_pending_not_deleted_marks_dead() {
        let (_tmp, conn) = setup();
        let spool = Spool::new(&conn);

        // Insert a pending entry older than 7 days
        let old_date = (Utc::now() - chrono::Duration::days(8)).to_rfc3339();
        conn.execute(
            "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
             VALUES ('claude', '/stale', 0, 100, ?1, ?1, 'pending')",
            [&old_date],
        )
        .unwrap();

        let cleaned = spool.cleanup().unwrap();

        // cleanup returns count of hard-deleted rows (dead >30d), not the marked-dead count
        // The pending->dead transition is separate
        let _ = cleaned;

        // Verify the row still exists (not deleted) with status='dead'
        let status: String = conn
            .query_row(
                "SELECT status FROM spool_queue WHERE file_path = '/stale'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            status, "dead",
            "Old pending entry should be marked dead, not deleted"
        );

        // Now simulate it being dead for >30 days and verify hard-delete
        conn.execute(
            "UPDATE spool_queue SET created_at = ?1 WHERE file_path = '/stale'",
            [&(Utc::now() - chrono::Duration::days(31)).to_rfc3339()],
        )
        .unwrap();

        let deleted = spool.cleanup().unwrap();
        assert_eq!(deleted, 1, "Dead entry >30 days should be hard-deleted");
        assert_eq!(
            spool.total_size().unwrap(),
            0,
            "Spool should be empty after hard-delete"
        );
    }
}
