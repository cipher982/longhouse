//! Durable storage-v2 write intent.
//!
//! The provider source is mutable. Once an envelope is prepared, this row is
//! the retry authority until the Runtime Host returns its exact receipt.

use anyhow::{bail, Context, Result};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::Serialize;
use uuid::Uuid;

const MAX_PENDING_OUTBOX_BYTES: u64 = 1024 * 1024 * 1024;

fn pending_outbox_has_capacity(current_bytes: u64, candidate_bytes: u64) -> bool {
    current_bytes.saturating_add(candidate_bytes) <= MAX_PENDING_OUTBOX_BYTES
}

#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct StorageV2OutboxSnapshot {
    pub pending_count: u64,
    pub pending_bytes: u64,
    pub oldest_pending_at: Option<String>,
    pub blocked_source_count: u64,
    pub blocked_bytes: u64,
    pub oldest_blocked_at: Option<String>,
    pub latest_block_kind: Option<String>,
    pub latest_block_detail: Option<String>,
    pub byte_limit: u64,
    pub error: Option<String>,
}

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
    pub blocked_at: Option<String>,
    pub block_kind: Option<String>,
    pub block_detail: Option<String>,
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
            blocked_at: None,
            block_kind: None,
            block_detail: None,
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
                last_attempt_at, blocked_at, block_kind, block_detail
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
                pending.last_attempt_at, pending.blocked_at,
                pending.block_kind, pending.block_detail
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
                last_attempt_at, blocked_at, block_kind, block_detail
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
    let existing = tx
        .query_row(
            "SELECT source_epoch, source_path, range_start, range_end, envelope_id,
                    request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                    has_reply_evidence, has_more, created_at, attempt_count,
                    last_attempt_at, blocked_at, block_kind, block_detail
             FROM pending_source_envelope
             WHERE source_epoch = ?1",
            [candidate.source_epoch.to_string()],
            row_to_pending,
        )
        .optional()?;
    if let Some(existing) = existing {
        tx.commit()?;
        return Ok(existing);
    }
    let current_bytes: i64 = tx.query_row(
        "SELECT COALESCE(SUM(length(request_body_zstd) + length(media_objects_zstd)), 0)
         FROM pending_source_envelope
         WHERE blocked_at IS NULL",
        [],
        |row| row.get(0),
    )?;
    let current_bytes = u64::try_from(current_bytes).context("pending outbox size is negative")?;
    let candidate_bytes = u64::try_from(
        candidate.request_body_zstd.len() + candidate.media_objects_zstd.len(),
    )
    .context("pending envelope size exceeds u64")?;
    if !pending_outbox_has_capacity(current_bytes, candidate_bytes) {
        bail!(
            "storage-v2 pending outbox byte limit exceeded ({MAX_PENDING_OUTBOX_BYTES} bytes)"
        );
    }
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
                    last_attempt_at, blocked_at, block_kind, block_detail
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
         WHERE source_epoch = ?2 AND blocked_at IS NULL",
        params![Utc::now().to_rfc3339(), source_epoch.to_string()],
    )?;
    if changed != 1 {
        bail!("pending storage-v2 envelope disappeared before send");
    }
    Ok(())
}

pub fn quarantine(conn: &Connection, source_epoch: Uuid, kind: &str, detail: &str) -> Result<bool> {
    let changed = conn.execute(
        "UPDATE pending_source_envelope
         SET blocked_at = ?1, block_kind = ?2, block_detail = ?3
         WHERE source_epoch = ?4 AND blocked_at IS NULL",
        params![
            Utc::now().to_rfc3339(),
            kind,
            detail,
            source_epoch.to_string()
        ],
    )?;
    if changed > 1 {
        bail!("quarantining one source changed multiple pending envelopes");
    }
    Ok(changed == 1)
}

pub fn source_is_blocked(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<bool> {
    conn.query_row(
        "SELECT EXISTS(
            SELECT 1
            FROM pending_source_envelope AS pending
            JOIN source_epoch_registry AS epoch
              ON epoch.source_epoch = pending.source_epoch
            WHERE epoch.provider = ?1 AND epoch.opaque_source_id = ?2
              AND pending.blocked_at IS NOT NULL
         )",
        params![provider, opaque_source_id],
        |row| row.get(0),
    )
    .context("checking blocked storage-v2 source")
}

pub fn snapshot(conn: &Connection) -> Result<StorageV2OutboxSnapshot> {
    let (
        pending_count,
        pending_bytes,
        oldest_pending_at,
        blocked_source_count,
        blocked_bytes,
        oldest_blocked_at,
    ): (i64, i64, Option<String>, i64, i64, Option<String>) = conn.query_row(
        "SELECT
            COALESCE(SUM(CASE WHEN blocked_at IS NULL THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN blocked_at IS NULL
                THEN length(request_body_zstd) + length(media_objects_zstd) ELSE 0 END), 0),
            MIN(CASE WHEN blocked_at IS NULL THEN created_at END),
            COALESCE(SUM(CASE WHEN blocked_at IS NOT NULL THEN 1 ELSE 0 END), 0),
            COALESCE(SUM(CASE WHEN blocked_at IS NOT NULL
                THEN length(request_body_zstd) + length(media_objects_zstd) ELSE 0 END), 0),
            MIN(blocked_at)
         FROM pending_source_envelope",
        [],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?)),
    )?;
    let latest_block = conn
        .query_row(
            "SELECT block_kind, block_detail
             FROM pending_source_envelope
             WHERE blocked_at IS NOT NULL
             ORDER BY blocked_at DESC, source_epoch
             LIMIT 1",
            [],
            |row| Ok((row.get::<_, Option<String>>(0)?, row.get::<_, Option<String>>(1)?)),
        )
        .optional()?;
    Ok(StorageV2OutboxSnapshot {
        pending_count: u64::try_from(pending_count).context("pending outbox count is negative")?,
        pending_bytes: u64::try_from(pending_bytes).context("pending outbox bytes are negative")?,
        oldest_pending_at,
        blocked_source_count: u64::try_from(blocked_source_count)
            .context("blocked source count is negative")?,
        blocked_bytes: u64::try_from(blocked_bytes).context("blocked source bytes are negative")?,
        oldest_blocked_at,
        latest_block_kind: latest_block.as_ref().and_then(|value| value.0.clone()),
        latest_block_detail: latest_block.and_then(|value| value.1),
        byte_limit: MAX_PENDING_OUTBOX_BYTES,
        error: None,
    })
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

/// Replace only the serialized request representation after the Runtime Host
/// proves that the raw envelope identity is valid but its render generation
/// must join an already-registered parser revision. Raw source bytes, range,
/// envelope identity, media, and the durable cursor remain unchanged.
pub fn replace_request_body_after_render_conflict(
    conn: &Connection,
    source_epoch: Uuid,
    envelope_id: &str,
    expected_request_body_zstd: &[u8],
    replacement_request_body_zstd: &[u8],
) -> Result<()> {
    let changed = conn.execute(
        "UPDATE pending_source_envelope
         SET request_body_zstd = ?1,
             blocked_at = NULL,
             block_kind = NULL,
             block_detail = NULL
         WHERE source_epoch = ?2 AND envelope_id = ?3
           AND request_body_zstd = ?4",
        params![
            replacement_request_body_zstd,
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
        ],
    )?;
    if changed != 1 {
        bail!("render-generation recovery no longer matches the pending envelope");
    }
    Ok(())
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

/// Replace a conflicting envelope with its unaccepted suffix after every
/// hosted prefix range has been proven from the persisted raw records.
pub fn reconcile_proven_prefix(
    conn: &mut Connection,
    source_epoch: Uuid,
    expected_envelope_id: &str,
    expected_start: u64,
    proven_through: u64,
    replacement: Option<&PendingSourceEnvelope>,
) -> Result<()> {
    if proven_through <= expected_start {
        bail!("source reconciliation must advance the cursor");
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let old_end: i64 = tx
        .query_row(
            "SELECT range_end FROM pending_source_envelope
             WHERE source_epoch = ?1 AND envelope_id = ?2
               AND range_start = ?3 AND blocked_at IS NULL",
            params![
                source_epoch.to_string(),
                expected_envelope_id,
                to_sql_u64(expected_start)?,
            ],
            |row| row.get(0),
        )
        .context("reconciliation no longer matches the pending envelope")?;
    let old_end = u64::try_from(old_end).context("pending range end is negative")?;
    if proven_through > old_end {
        bail!("source reconciliation exceeds the pending envelope");
    }
    match replacement {
        Some(replacement)
            if replacement.source_epoch == source_epoch
                && replacement.range_start == proven_through
                && replacement.range_end == old_end => {}
        Some(_) => bail!("reconciled suffix does not exactly cover the pending remainder"),
        None if proven_through == old_end => {}
        None => bail!("reconciliation would discard an unaccepted suffix"),
    }
    let advanced = tx.execute(
        "UPDATE source_epoch_lane_state
         SET last_position = ?1, updated_at = ?2
         WHERE source_epoch = ?3 AND lane = 'durable' AND last_position = ?4",
        params![
            to_sql_u64(proven_through)?,
            Utc::now().to_rfc3339(),
            source_epoch.to_string(),
            to_sql_u64(expected_start)?,
        ],
    )?;
    if advanced != 1 {
        bail!("source epoch lane cursor changed before reconciliation");
    }
    let deleted = tx.execute(
        "DELETE FROM pending_source_envelope
         WHERE source_epoch = ?1 AND envelope_id = ?2",
        params![source_epoch.to_string(), expected_envelope_id],
    )?;
    if deleted != 1 {
        bail!("pending envelope disappeared during reconciliation");
    }
    if let Some(replacement) = replacement {
        tx.execute(
            "INSERT INTO pending_source_envelope (
                source_epoch, source_path, range_start, range_end, envelope_id,
                request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                has_reply_evidence, has_more, created_at, attempt_count,
                last_attempt_at, blocked_at, block_kind, block_detail
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12,
                       0, NULL, NULL, NULL, NULL)",
            params![
                replacement.source_epoch.to_string(),
                replacement.source_path,
                to_sql_u64(replacement.range_start)?,
                to_sql_u64(replacement.range_end)?,
                replacement.envelope_id,
                replacement.request_body_zstd,
                replacement.media_objects_zstd,
                to_sql_u64(replacement.raw_bytes)?,
                i64::try_from(replacement.event_count)
                    .context("event count exceeds SQLite INTEGER")?,
                replacement.has_reply_evidence,
                replacement.has_more,
                replacement.created_at,
            ],
        )?;
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
        blocked_at: row.get(14)?,
        block_kind: row.get(15)?,
        block_detail: row.get(16)?,
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

#[cfg(test)]
mod tests {
    use super::{
        pending_outbox_has_capacity, persist_or_load, quarantine, snapshot,
        PendingSourceEnvelope, MAX_PENDING_OUTBOX_BYTES,
    };
    use crate::state::db::open_db;
    use uuid::Uuid;

    #[test]
    fn pending_outbox_capacity_is_exact_and_overflow_safe() {
        assert!(pending_outbox_has_capacity(MAX_PENDING_OUTBOX_BYTES - 1, 1));
        assert!(!pending_outbox_has_capacity(MAX_PENDING_OUTBOX_BYTES, 1));
        assert!(!pending_outbox_has_capacity(u64::MAX, u64::MAX));
    }

    #[test]
    fn quarantined_bytes_do_not_consume_live_prepare_capacity() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let blocked_epoch = Uuid::new_v4();
        let blocked = candidate(blocked_epoch, "blocked");
        persist_or_load(&mut conn, &blocked).unwrap();
        assert!(quarantine(&mut conn, blocked_epoch, "source_epoch_conflict", "proof mismatch").unwrap());
        let live = candidate(Uuid::new_v4(), "live");
        persist_or_load(&mut conn, &live).unwrap();

        let state = snapshot(&conn).unwrap();
        assert_eq!(state.blocked_source_count, 1);
        assert_eq!(state.pending_count, 1);
        assert_eq!(state.pending_bytes, 2);
        assert_eq!(state.blocked_bytes, 2);
    }

    fn candidate(source_epoch: Uuid, source_path: &str) -> PendingSourceEnvelope {
        PendingSourceEnvelope {
            source_epoch,
            source_path: source_path.to_string(),
            range_start: 0,
            range_end: 1,
            envelope_id: "a".repeat(64),
            request_body_zstd: vec![1],
            media_objects_zstd: vec![2],
            raw_bytes: 1,
            event_count: 1,
            has_reply_evidence: true,
            has_more: false,
            created_at: "2026-07-15T00:00:00Z".to_string(),
            attempt_count: 0,
            last_attempt_at: None,
            blocked_at: None,
            block_kind: None,
            block_detail: None,
        }
    }
}
