//! Durable storage-v2 write intent.
//!
//! The provider source is mutable. Once an envelope is prepared, this row is
//! the retry authority until the Runtime Host returns its exact receipt.

use anyhow::{bail, Context, Result};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingSourceEnvelope {
    pub source_epoch: Uuid,
    pub source_path: String,
    pub range_start: u64,
    pub range_end: u64,
    pub envelope_id: String,
    pub request_body_zstd: Vec<u8>,
    pub media_objects_zstd: Vec<u8>,
    pub raw_bytes: u64,
    pub event_count: usize,
    pub has_reply_evidence: bool,
    pub has_more: bool,
    pub created_at: String,
    pub attempt_count: u64,
    pub last_attempt_at: Option<String>,
}

impl PendingSourceEnvelope {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        source_epoch: Uuid,
        source_path: String,
        range_start: u64,
        range_end: u64,
        envelope_id: String,
        request_body_zstd: Vec<u8>,
        media_objects_zstd: Vec<u8>,
        raw_bytes: u64,
        event_count: usize,
        has_reply_evidence: bool,
        has_more: bool,
    ) -> Self {
        Self {
            source_epoch,
            source_path,
            range_start,
            range_end,
            envelope_id,
            request_body_zstd,
            media_objects_zstd,
            raw_bytes,
            event_count,
            has_reply_evidence,
            has_more,
            created_at: Utc::now().to_rfc3339(),
            attempt_count: 0,
            last_attempt_at: None,
        }
    }
}

pub fn load_for_path(
    conn: &Connection,
    source_path: &str,
) -> Result<Option<PendingSourceEnvelope>> {
    conn.query_row(
        "SELECT source_epoch, source_path, range_start, range_end, envelope_id,
                request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                has_reply_evidence, has_more, created_at, attempt_count,
                last_attempt_at
         FROM pending_source_envelope
         WHERE source_path = ?1
         ORDER BY created_at, source_epoch
         LIMIT 1",
        [source_path],
        row_to_pending,
    )
    .optional()
    .context("loading pending storage-v2 envelope by source path")
}

pub fn load_for_source(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<Option<PendingSourceEnvelope>> {
    conn.query_row(
        "SELECT pending.source_epoch, pending.source_path, pending.range_start,
                pending.range_end, pending.envelope_id,
                pending.request_body_zstd, pending.media_objects_zstd,
                pending.raw_bytes, pending.event_count,
                pending.has_reply_evidence, pending.has_more,
                pending.created_at, pending.attempt_count,
                pending.last_attempt_at
         FROM pending_source_envelope AS pending
         JOIN source_epoch_registry AS epoch
           ON epoch.source_epoch = pending.source_epoch
         WHERE epoch.provider = ?1 AND epoch.opaque_source_id = ?2
         ORDER BY pending.created_at, pending.source_epoch
         LIMIT 1",
        params![provider, opaque_source_id],
        row_to_pending,
    )
    .optional()
    .context("loading pending storage-v2 envelope by source identity")
}

pub fn load_for_epoch(
    conn: &Connection,
    source_epoch: Uuid,
) -> Result<Option<PendingSourceEnvelope>> {
    conn.query_row(
        "SELECT source_epoch, source_path, range_start, range_end, envelope_id,
                request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                has_reply_evidence, has_more, created_at, attempt_count,
                last_attempt_at
         FROM pending_source_envelope
         WHERE source_epoch = ?1",
        [source_epoch.to_string()],
        row_to_pending,
    )
    .optional()
    .context("loading pending storage-v2 envelope by source epoch")
}

/// Persist the first prepared intent for an epoch and return the durable winner.
/// A concurrent preparer may have inserted a different EOF-bounded range first;
/// callers must always send the returned row, never their in-memory candidate.
pub fn persist_or_load(
    conn: &mut Connection,
    candidate: &PendingSourceEnvelope,
) -> Result<PendingSourceEnvelope> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute(
        "INSERT INTO pending_source_envelope (
            source_epoch, source_path, range_start, range_end, envelope_id,
            request_body_zstd, media_objects_zstd, raw_bytes, event_count,
            has_reply_evidence, has_more, created_at, attempt_count,
            last_attempt_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, 0, NULL)
         ON CONFLICT(source_epoch) DO NOTHING",
        params![
            candidate.source_epoch.to_string(),
            candidate.source_path,
            to_sql_u64(candidate.range_start)?,
            to_sql_u64(candidate.range_end)?,
            candidate.envelope_id,
            candidate.request_body_zstd,
            candidate.media_objects_zstd,
            to_sql_u64(candidate.raw_bytes)?,
            i64::try_from(candidate.event_count).context("event count exceeds SQLite INTEGER")?,
            candidate.has_reply_evidence,
            candidate.has_more,
            candidate.created_at,
        ],
    )?;
    let persisted = tx
        .query_row(
            "SELECT source_epoch, source_path, range_start, range_end, envelope_id,
                    request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                    has_reply_evidence, has_more, created_at, attempt_count,
                    last_attempt_at
             FROM pending_source_envelope
             WHERE source_epoch = ?1",
            [candidate.source_epoch.to_string()],
            row_to_pending,
        )
        .context("reloading persisted storage-v2 envelope")?;
    tx.commit()?;
    Ok(persisted)
}

pub fn mark_attempt(conn: &Connection, source_epoch: Uuid) -> Result<()> {
    let changed = conn.execute(
        "UPDATE pending_source_envelope
         SET attempt_count = attempt_count + 1, last_attempt_at = ?1
         WHERE source_epoch = ?2",
        params![Utc::now().to_rfc3339(), source_epoch.to_string()],
    )?;
    if changed != 1 {
        bail!("pending storage-v2 envelope disappeared before send");
    }
    Ok(())
}

/// Remove an intent that was prepared for a product gate but never sent.
/// Once an attempt starts, exact retry remains authoritative and cannot be
/// discarded by a later caller.
pub fn discard_unattempted(
    conn: &Connection,
    source_epoch: Uuid,
    envelope_id: &str,
) -> Result<bool> {
    Ok(conn.execute(
        "DELETE FROM pending_source_envelope
         WHERE source_epoch = ?1 AND envelope_id = ?2 AND attempt_count = 0",
        params![source_epoch.to_string(), envelope_id],
    )? == 1)
}

/// Advance the durable cursor and forget the exact retry in one transaction.
pub fn acknowledge_and_delete(
    conn: &mut Connection,
    source_epoch: Uuid,
    expected_envelope_id: &str,
    expected_start: u64,
    acknowledged_through: u64,
) -> Result<()> {
    if acknowledged_through < expected_start {
        bail!("source epoch acknowledgement cannot move backward");
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let pending_matches = tx
        .query_row(
            "SELECT envelope_id = ?2 AND range_start = ?3 AND range_end = ?4
             FROM pending_source_envelope
             WHERE source_epoch = ?1",
            params![
                source_epoch.to_string(),
                expected_envelope_id,
                to_sql_u64(expected_start)?,
                to_sql_u64(acknowledged_through)?,
            ],
            |row| row.get::<_, bool>(0),
        )
        .optional()?;
    if pending_matches.is_none() {
        let cursor: Option<i64> = tx
            .query_row(
                "SELECT last_position FROM source_epoch_lane_state
                 WHERE source_epoch = ?1 AND lane = 'durable'",
                [source_epoch.to_string()],
                |row| row.get(0),
            )
            .optional()?;
        if cursor == Some(to_sql_u64(acknowledged_through)?) {
            tx.commit()?;
            return Ok(());
        }
        bail!("storage-v2 receipt has no matching pending envelope or acknowledged cursor");
    }
    if pending_matches == Some(false) {
        bail!("storage-v2 receipt does not match the durable pending envelope");
    }
    let changed = tx.execute(
        "UPDATE source_epoch_lane_state
         SET last_position = ?1, updated_at = ?2
         WHERE source_epoch = ?3 AND lane = 'durable' AND last_position = ?4",
        params![
            to_sql_u64(acknowledged_through)?,
            Utc::now().to_rfc3339(),
            source_epoch.to_string(),
            to_sql_u64(expected_start)?,
        ],
    )?;
    if changed != 1 {
        bail!("source epoch lane cursor changed before acknowledgement");
    }
    let deleted = tx.execute(
        "DELETE FROM pending_source_envelope
         WHERE source_epoch = ?1 AND envelope_id = ?2",
        params![source_epoch.to_string(), expected_envelope_id],
    )?;
    if deleted != 1 {
        bail!("pending storage-v2 envelope disappeared during acknowledgement");
    }
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
pub fn count(conn: &Connection) -> Result<u64> {
    let value: i64 = conn.query_row("SELECT COUNT(*) FROM pending_source_envelope", [], |row| {
        row.get(0)
    })?;
    u64::try_from(value).context("pending envelope count is negative")
}

fn row_to_pending(row: &rusqlite::Row<'_>) -> rusqlite::Result<PendingSourceEnvelope> {
    let source_epoch: String = row.get(0)?;
    let range_start: i64 = row.get(2)?;
    let range_end: i64 = row.get(3)?;
    let raw_bytes: i64 = row.get(7)?;
    let event_count: i64 = row.get(8)?;
    let attempt_count: i64 = row.get(12)?;
    Ok(PendingSourceEnvelope {
        source_epoch: Uuid::parse_str(&source_epoch).map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(
                0,
                rusqlite::types::Type::Text,
                Box::new(error),
            )
        })?,
        source_path: row.get(1)?,
        range_start: from_sql_u64(2, range_start)?,
        range_end: from_sql_u64(3, range_end)?,
        envelope_id: row.get(4)?,
        request_body_zstd: row.get(5)?,
        media_objects_zstd: row.get(6)?,
        raw_bytes: from_sql_u64(7, raw_bytes)?,
        event_count: usize::try_from(event_count).map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(
                8,
                rusqlite::types::Type::Integer,
                Box::new(error),
            )
        })?,
        has_reply_evidence: row.get(9)?,
        has_more: row.get(10)?,
        created_at: row.get(11)?,
        attempt_count: from_sql_u64(12, attempt_count)?,
        last_attempt_at: row.get(13)?,
    })
}

fn to_sql_u64(value: u64) -> Result<i64> {
    i64::try_from(value).context("source envelope value exceeds SQLite INTEGER")
}

fn from_sql_u64(index: usize, value: i64) -> rusqlite::Result<u64> {
    u64::try_from(value).map_err(|error| {
        rusqlite::Error::FromSqlConversionFailure(
            index,
            rusqlite::types::Type::Integer,
            Box::new(error),
        )
    })
}
