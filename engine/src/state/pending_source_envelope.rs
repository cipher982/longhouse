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
    /// Blocked sources a re-examination can plausibly clear on its own.
    ///
    /// These two fields have been read by three consumers — Rust `device.rs`,
    /// Python `local_health/`, and Python `agent_heartbeat_health.py` — and
    /// emitted by nobody, so every block presented identically as permanent red
    /// and `device.rs` took its "requires repair" fallback for all of them.
    /// A count that only a consumer knows about is not a signal.
    ///
    /// The split is by `block_kind`, which is the only evidence available here
    /// about whether local recovery has anything to try. It deliberately does
    /// not promise success: `reconciling` means a recovery path exists and is
    /// scheduled, not that it will work.
    pub reconciling_blocked_source_count: u64,
    /// Blocked sources with no local recovery path, which is what should
    /// actually reach a user as "needs you".
    pub unresolved_blocked_source_count: u64,
    pub blocked_bytes: u64,
    pub oldest_blocked_at: Option<String>,
    pub latest_block_kind: Option<String>,
    pub latest_block_detail: Option<String>,
    pub byte_limit: u64,
    pub error: Option<String>,
}

/// Whether a recorded block has a local recovery path that will be retried.
///
/// `source_epoch_conflict` covers the range disagreements `resync_behind_host`
/// resolves against host truth. `source_epoch_conflict_unresolved` means the
/// Runtime Host has no epoch at all, which no local action can fix — it needs
/// remote authority to re-register, so it is honestly "needs you".
pub fn block_kind_is_reconciling(block_kind: Option<&str>) -> bool {
    matches!(block_kind, Some("source_epoch_conflict"))
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PendingSourceRetryPath {
    pub provider: String,
    pub source_path: String,
    pub raw_bytes: u64,
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
         ORDER BY (blocked_at IS NOT NULL), created_at, source_epoch
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

/// List exact-retry work that must be rescheduled after a process restart.
///
/// The request body itself remains authoritative in `pending_source_envelope`;
/// this projection contains only enough information to wake the bounded path
/// scheduler. Multiple epochs for one path collapse into one scheduler job.
pub fn retry_paths(conn: &Connection) -> Result<Vec<PendingSourceRetryPath>> {
    let mut statement = conn.prepare(
        // Selection is by time due, and by nothing else.
        //
        // Blocked rows used to be filtered out here, so a quarantined source
        // was skipped by restart recovery as well as by the live path and
        // nothing in the process ever looked at it again. Removing that filter
        // fixed the absorbing state; selecting on `wake_at` is what keeps the
        // fix from becoming an unbounded re-examination loop.
        //
        // The shape is the point: classification lives in `blocked_at` and
        // `block_kind`, which this query does not mention. A predicate can
        // postpone a row by moving `wake_at`; it cannot remove one from
        // consideration, because there is nothing here to filter on.
        "SELECT epoch.provider, pending.source_path,
                SUM(pending.raw_bytes), MIN(pending.created_at)
         FROM pending_source_envelope AS pending
         JOIN source_epoch_registry AS epoch
           ON epoch.source_epoch = pending.source_epoch
         WHERE pending.wake_at <= ?1
         GROUP BY epoch.provider, pending.source_path
         ORDER BY MIN(pending.created_at), epoch.provider, pending.source_path",
    )?;
    let rows = statement.query_map([Utc::now().to_rfc3339()], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    })?;
    rows.map(|row| {
        let (provider, source_path, raw_bytes) = row?;
        Ok(PendingSourceRetryPath {
            provider,
            source_path,
            raw_bytes: u64::try_from(raw_bytes).context("pending retry bytes are negative")?,
        })
    })
    .collect()
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
    let candidate_bytes =
        u64::try_from(candidate.request_body_zstd.len() + candidate.media_objects_zstd.len())
            .context("pending envelope size exceeds u64")?;
    if !pending_outbox_has_capacity(current_bytes, candidate_bytes) {
        bail!("storage-v2 pending outbox byte limit exceeded ({MAX_PENDING_OUTBOX_BYTES} bytes)");
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

/// How long to wait before re-examining a blocked source, by attempt count.
///
/// Blocking no longer removes a row from consideration, which is what fixed the
/// absorbing state — and immediately created the opposite risk, since every
/// blocked row would otherwise be re-examined on every watcher tick and every
/// restart, each costing a Runtime Host manifest fetch.
///
/// The curve is deliberately coarse. Nothing here is time-critical: a source
/// that has been blocked for a day loses nothing by being looked at hourly
/// rather than continuously, and the cheapest re-examination is the one that
/// does not happen.
fn reexamine_backoff(attempt_count: u64) -> chrono::Duration {
    match attempt_count {
        0..=1 => chrono::Duration::minutes(1),
        2..=3 => chrono::Duration::minutes(15),
        4..=6 => chrono::Duration::hours(1),
        _ => chrono::Duration::hours(6),
    }
}

pub fn quarantine(conn: &Connection, source_epoch: Uuid, kind: &str, detail: &str) -> Result<bool> {
    let now = Utc::now();
    // Read the attempt count so repeated blocks back off rather than spinning.
    let attempts: u64 = conn
        .query_row(
            "SELECT attempt_count FROM pending_source_envelope WHERE source_epoch = ?1",
            [source_epoch.to_string()],
            |row| row.get::<_, i64>(0),
        )
        .optional()?
        .map(|value| u64::try_from(value).unwrap_or(0))
        .unwrap_or(0);
    let wake_at = now + reexamine_backoff(attempts);
    let changed = conn.execute(
        "UPDATE pending_source_envelope
         SET blocked_at = ?1, block_kind = ?2, block_detail = ?3, wake_at = ?5
         WHERE source_epoch = ?4 AND blocked_at IS NULL",
        params![
            now.to_rfc3339(),
            kind,
            detail,
            source_epoch.to_string(),
            wake_at.to_rfc3339()
        ],
    )?;
    if changed > 1 {
        bail!("quarantining one source changed multiple pending envelopes");
    }
    Ok(changed == 1)
}

/// Push a blocked source's next examination out after one that found nothing.
///
/// A re-examination that changes nothing still costs a manifest fetch, so the
/// row must move further out rather than coming straight back. Without this the
/// backoff set at quarantine time only ever applies once.
pub fn defer_reexamination(conn: &Connection, source_epoch: Uuid) -> Result<()> {
    let attempts: u64 = conn
        .query_row(
            "SELECT attempt_count FROM pending_source_envelope WHERE source_epoch = ?1",
            [source_epoch.to_string()],
            |row| row.get::<_, i64>(0),
        )
        .optional()?
        .map(|value| u64::try_from(value).unwrap_or(0))
        .unwrap_or(0);
    let wake_at = Utc::now() + reexamine_backoff(attempts.saturating_add(1));
    conn.execute(
        "UPDATE pending_source_envelope SET wake_at = ?2 WHERE source_epoch = ?1",
        params![source_epoch.to_string(), wake_at.to_rfc3339()],
    )?;
    Ok(())
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
    )?;
    // Split the blocked total by whether local recovery has anything to try.
    // Counted in Rust rather than SQL so the classification lives in exactly one
    // place — `block_kind_is_reconciling` — instead of being duplicated as a
    // CASE expression that can drift from it.
    let mut reconciling_blocked_source_count: u64 = 0;
    let mut unresolved_blocked_source_count: u64 = 0;
    {
        let mut statement = conn.prepare(
            "SELECT block_kind FROM pending_source_envelope WHERE blocked_at IS NOT NULL",
        )?;
        let kinds = statement.query_map([], |row| row.get::<_, Option<String>>(0))?;
        for kind in kinds {
            if block_kind_is_reconciling(kind?.as_deref()) {
                reconciling_blocked_source_count += 1;
            } else {
                unresolved_blocked_source_count += 1;
            }
        }
    }
    let latest_block = conn
        .query_row(
            "SELECT block_kind, block_detail
             FROM pending_source_envelope
             WHERE blocked_at IS NOT NULL
             ORDER BY blocked_at DESC, source_epoch
             LIMIT 1",
            [],
            |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                ))
            },
        )
        .optional()?;
    Ok(StorageV2OutboxSnapshot {
        pending_count: u64::try_from(pending_count).context("pending outbox count is negative")?,
        pending_bytes: u64::try_from(pending_bytes).context("pending outbox bytes are negative")?,
        oldest_pending_at,
        blocked_source_count: u64::try_from(blocked_source_count)
            .context("blocked source count is negative")?,
        reconciling_blocked_source_count,
        unresolved_blocked_source_count,
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

/// Drop a frozen envelope whose range no longer describes the work to do.
///
/// After the durable cursor is resynced to the Runtime Host's watermark, the
/// pending body describes a range starting *after* a gap the host never
/// received, so it can never be accepted as-is. Deleting it lets the next
/// prepare rebuild from the corrected cursor and send the missing bytes
/// contiguously.
///
/// Unlike [`discard_unattempted`] this deliberately applies to attempted and
/// blocked rows — those are exactly the ones a resync exists to rescue — so it
/// keys on the exact envelope identity instead of an attempt count, and returns
/// false rather than deleting anything if that identity has moved on.
pub fn discard_after_cursor_resync(
    conn: &Connection,
    source_epoch: Uuid,
    envelope_id: &str,
) -> Result<bool> {
    Ok(conn.execute(
        "DELETE FROM pending_source_envelope
         WHERE source_epoch = ?1 AND envelope_id = ?2",
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

/// Replace a rejected request only after local lineage proof establishes that
/// its requested epoch never opened remotely and every skipped predecessor is
/// empty. The old and new exact bodies remain durable for audit/restart proof.
pub fn replace_request_body_after_lineage_repair(
    conn: &mut Connection,
    source_epoch: Uuid,
    envelope_id: &str,
    expected_request_body_zstd: &[u8],
    replacement_request_body_zstd: &[u8],
    reason: &str,
    proof_json: &str,
) -> Result<()> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let changed = tx.execute(
        "UPDATE pending_source_envelope
         SET request_body_zstd = ?1,
             blocked_at = NULL,
             block_kind = NULL,
             block_detail = NULL
         WHERE source_epoch = ?2 AND envelope_id = ?3
           AND request_body_zstd = ?4
           AND blocked_at IS NOT NULL
           AND block_kind = 'source_epoch_conflict_unresolved'
           AND block_detail LIKE '%source_epoch_not_found%'",
        params![
            replacement_request_body_zstd,
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
        ],
    )?;
    if changed != 1 {
        bail!("lineage recovery no longer matches the blocked pending envelope");
    }
    tx.execute(
        "INSERT INTO pending_source_envelope_supersession (
             source_epoch, envelope_id, old_request_body_zstd,
             new_request_body_zstd, reason, proof_json, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
            replacement_request_body_zstd,
            reason,
            proof_json,
            Utc::now().to_rfc3339(),
        ],
    )?;
    tx.commit()?;
    Ok(())
}

/// Retire a quarantined request only after the Runtime Host proves that its
/// source epoch was closed in favor of a locally durable replacement epoch.
/// The exact frozen body and host proof remain durable in the supersession
/// audit even though the obsolete retry itself is removed.
pub fn retire_after_host_replacement(
    conn: &mut Connection,
    source_epoch: Uuid,
    envelope_id: &str,
    expected_range_start: u64,
    retired_through: u64,
    expected_request_body_zstd: &[u8],
    reason: &str,
    proof_json: &str,
) -> Result<()> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let advanced = tx.execute(
        "UPDATE source_epoch_lane_state
         SET last_position = ?1, updated_at = ?2
         WHERE source_epoch = ?3 AND lane = 'durable' AND last_position = ?4",
        params![
            to_sql_u64(retired_through)?,
            Utc::now().to_rfc3339(),
            source_epoch.to_string(),
            to_sql_u64(expected_range_start)?,
        ],
    )?;
    if advanced != 1 {
        bail!("host replacement proof no longer matches the source durable cursor");
    }
    let changed = tx.execute(
        "DELETE FROM pending_source_envelope
         WHERE source_epoch = ?1 AND envelope_id = ?2
           AND request_body_zstd = ?3
           AND blocked_at IS NOT NULL
           AND block_kind = 'source_epoch_conflict'",
        params![
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
        ],
    )?;
    if changed != 1 {
        bail!("host replacement proof no longer matches the blocked pending envelope");
    }
    tx.execute(
        "INSERT INTO pending_source_envelope_supersession (
             source_epoch, envelope_id, old_request_body_zstd,
             new_request_body_zstd, reason, proof_json, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
            Vec::<u8>::new(),
            reason,
            proof_json,
            Utc::now().to_rfc3339(),
        ],
    )?;
    tx.commit()?;
    Ok(())
}

/// Retire a blocked request whose byte range was proven to contain no
/// canonical events or media. The raw request and proof remain in the
/// supersession audit, while the durable cursor advances past provider
/// startup metadata that Longhouse does not ingest.
pub fn retire_empty_source(
    conn: &mut Connection,
    source_epoch: Uuid,
    envelope_id: &str,
    expected_range_start: u64,
    retired_through: u64,
    expected_request_body_zstd: &[u8],
    reason: &str,
    proof_json: &str,
) -> Result<()> {
    if retired_through <= expected_range_start {
        bail!("empty source retirement must advance the durable cursor");
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let advanced = tx.execute(
        "UPDATE source_epoch_lane_state
         SET last_position = ?1, updated_at = ?2
         WHERE source_epoch = ?3 AND lane = 'durable' AND last_position = ?4",
        params![
            to_sql_u64(retired_through)?,
            Utc::now().to_rfc3339(),
            source_epoch.to_string(),
            to_sql_u64(expected_range_start)?,
        ],
    )?;
    if advanced != 1 {
        bail!("empty source retirement proof no longer matches the durable cursor");
    }
    let changed = tx.execute(
        "DELETE FROM pending_source_envelope
         WHERE source_epoch = ?1 AND envelope_id = ?2
           AND request_body_zstd = ?3
           AND blocked_at IS NOT NULL
           AND block_kind = 'source_epoch_conflict_unresolved'",
        params![
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
        ],
    )?;
    if changed != 1 {
        bail!("empty source retirement proof no longer matches the blocked envelope");
    }
    tx.execute(
        "INSERT INTO pending_source_envelope_supersession (
             source_epoch, envelope_id, old_request_body_zstd,
             new_request_body_zstd, reason, proof_json, created_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            source_epoch.to_string(),
            envelope_id,
            expected_request_body_zstd,
            Vec::<u8>::new(),
            reason,
            proof_json,
            Utc::now().to_rfc3339(),
        ],
    )?;
    tx.commit()?;
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
        block_kind_is_reconciling, pending_outbox_has_capacity, persist_or_load, quarantine,
        replace_request_body_after_lineage_repair, retire_after_host_replacement, retry_paths,
        defer_reexamination, snapshot, PendingSourceEnvelope, MAX_PENDING_OUTBOX_BYTES,
    };
    use crate::state::db::open_db;
    use rusqlite::params;
    use uuid::Uuid;

    #[test]
    fn pending_outbox_capacity_is_exact_and_overflow_safe() {
        assert!(pending_outbox_has_capacity(MAX_PENDING_OUTBOX_BYTES - 1, 1));
        assert!(!pending_outbox_has_capacity(MAX_PENDING_OUTBOX_BYTES, 1));
        assert!(!pending_outbox_has_capacity(u64::MAX, u64::MAX));
    }

    #[test]
    fn a_blocked_source_is_postponed_rather_than_removed() {
        // The whole shape of the fix: quarantine sets a future wake_at instead
        // of excluding the row, so it is invisible *now* and due *later*. If a
        // future change filters on blocked_at again, the second assertion here
        // keeps passing while the third starts failing.
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let epoch = Uuid::new_v4();
        register_epoch(&conn, epoch, "claude");
        persist_or_load(&mut conn, &candidate(epoch, "/tmp/blocked.jsonl")).unwrap();

        assert_eq!(retry_paths(&conn).unwrap().len(), 1, "due immediately");

        quarantine(&mut conn, epoch, "source_epoch_conflict", "range gap").unwrap();
        assert!(
            retry_paths(&conn).unwrap().is_empty(),
            "a freshly blocked source is postponed, not due right now"
        );

        // Time passing is the only thing that should bring it back.
        conn.execute(
            "UPDATE pending_source_envelope SET wake_at = '1970-01-01T00:00:00+00:00'",
            [],
        )
        .unwrap();
        assert_eq!(
            retry_paths(&conn).unwrap().len(),
            1,
            "once due, a blocked source is scheduled like any other row; it is \
             re-examined against host truth and stays blocked only if there is \
             genuinely nothing to do"
        );
    }

    #[test]
    fn repeated_blocks_back_off_instead_of_spinning() {
        // Without a growing interval, removing the absorbing state just trades
        // it for a manifest fetch on every watcher tick.
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let epoch = Uuid::new_v4();
        register_epoch(&conn, epoch, "claude");
        persist_or_load(&mut conn, &candidate(epoch, "/tmp/blocked.jsonl")).unwrap();
        quarantine(&mut conn, epoch, "source_epoch_conflict", "gap").unwrap();

        let first: String = conn
            .query_row("SELECT wake_at FROM pending_source_envelope", [], |row| {
                row.get(0)
            })
            .unwrap();
        // Simulate attempts accumulating, then a fruitless re-examination.
        conn.execute(
            "UPDATE pending_source_envelope SET attempt_count = 8",
            [],
        )
        .unwrap();
        defer_reexamination(&conn, epoch).unwrap();
        let later: String = conn
            .query_row("SELECT wake_at FROM pending_source_envelope", [], |row| {
                row.get(0)
            })
            .unwrap();

        assert!(
            later > first,
            "a re-examination that found nothing must push the next one further \
             out, not schedule it again immediately ({later} vs {first})"
        );
    }

    #[test]
    fn the_blocked_split_is_emitted_and_sums_to_the_total() {
        // These two counters were read by three consumers and produced by
        // nobody, so device.rs took its "requires repair" fallback for every
        // block and a healing source looked identical to a stuck one.
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let healing = Uuid::new_v4();
        register_epoch(&conn, healing, "claude");
        persist_or_load(&mut conn, &candidate(healing, "/tmp/healing.jsonl")).unwrap();
        quarantine(&mut conn, healing, "source_epoch_conflict", "range gap").unwrap();

        let stuck = Uuid::new_v4();
        register_epoch(&conn, stuck, "claude");
        persist_or_load(&mut conn, &candidate(stuck, "/tmp/stuck.jsonl")).unwrap();
        quarantine(
            &mut conn,
            stuck,
            "source_epoch_conflict_unresolved",
            "host has no epoch",
        )
        .unwrap();

        let state = snapshot(&conn).unwrap();
        assert_eq!(state.blocked_source_count, 2);
        assert_eq!(state.reconciling_blocked_source_count, 1);
        assert_eq!(state.unresolved_blocked_source_count, 1);
        assert_eq!(
            state.reconciling_blocked_source_count + state.unresolved_blocked_source_count,
            state.blocked_source_count,
            "the split must account for every blocked source or consumers will \
             silently under-report one side"
        );
    }

    #[test]
    fn an_unknown_block_kind_counts_as_needing_a_human() {
        // Failing closed matters here: a new block kind nobody has written
        // recovery for must not be reported as quietly healing.
        assert!(!block_kind_is_reconciling(Some("something_new")));
        assert!(!block_kind_is_reconciling(None));
        assert!(block_kind_is_reconciling(Some("source_epoch_conflict")));
    }

    #[test]
    fn quarantined_bytes_do_not_consume_live_prepare_capacity() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let blocked_epoch = Uuid::new_v4();
        register_epoch(&conn, blocked_epoch, "claude");
        let blocked = candidate(blocked_epoch, "blocked");
        persist_or_load(&mut conn, &blocked).unwrap();
        assert!(quarantine(
            &mut conn,
            blocked_epoch,
            "source_epoch_conflict",
            "proof mismatch"
        )
        .unwrap());
        let live_epoch = Uuid::new_v4();
        register_epoch(&conn, live_epoch, "claude");
        let live = candidate(live_epoch, "live");
        persist_or_load(&mut conn, &live).unwrap();

        let state = snapshot(&conn).unwrap();
        assert_eq!(state.blocked_source_count, 1);
        assert_eq!(state.pending_count, 1);
        assert_eq!(state.pending_bytes, 2);
        assert_eq!(state.blocked_bytes, 2);
    }

    #[test]
    fn retry_paths_are_provider_scoped_deduplicated_and_time_gated() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let active = Uuid::new_v4();
        let second = Uuid::new_v4();
        let blocked = Uuid::new_v4();
        for (epoch, provider, source_id) in [
            (active, "codex", "source-a"),
            (second, "codex", "source-b"),
            (blocked, "claude", "source-c"),
        ] {
            conn.execute(
                "INSERT INTO source_epoch_registry (
                     source_epoch, provider, opaque_source_id, file_incarnation,
                     start_reason, max_observed_len, created_at, updated_at
                 ) VALUES (?1, ?2, ?3, 'fixture', 'initial', 1, ?4, ?4)",
                params![
                    epoch.to_string(),
                    provider,
                    source_id,
                    "2026-07-15T00:00:00Z"
                ],
            )
            .unwrap();
        }
        let mut first = candidate(active, "/tmp/shared.jsonl");
        first.raw_bytes = 3;
        persist_or_load(&mut conn, &first).unwrap();
        let mut same_path = candidate(second, "/tmp/shared.jsonl");
        same_path.raw_bytes = 5;
        same_path.envelope_id = "b".repeat(64);
        persist_or_load(&mut conn, &same_path).unwrap();
        let blocked_envelope = candidate(blocked, "/tmp/blocked.jsonl");
        persist_or_load(&mut conn, &blocked_envelope).unwrap();
        quarantine(&mut conn, blocked, "fixture", "blocked").unwrap();

        // Selection is by time due. A freshly blocked source is postponed, not
        // excluded — it carries a future `wake_at` and returns on its own once
        // that passes. `a_blocked_source_is_postponed_rather_than_removed`
        // covers the full cycle including the return; this case exists for the
        // provider scoping and same-path deduplication.
        let paths = retry_paths(&conn).unwrap();
        assert_eq!(
            paths,
            vec![super::PendingSourceRetryPath {
                provider: "codex".to_string(),
                source_path: "/tmp/shared.jsonl".to_string(),
                raw_bytes: 8,
            }],
            "two epochs on one path collapse into a single job, and the blocked \
             claude source is due later rather than now"
        );
    }

    #[test]
    fn lineage_repair_is_audited_and_only_unblocks_missing_predecessors() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let epoch = Uuid::new_v4();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, 'cursor', 'fixture-source', 'fixture',
                       'initial', 1, ?2, ?2)",
            params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO source_epoch_lane_state (
                 source_epoch, lane, last_position, updated_at
             ) VALUES (?1, 'durable', 0, ?2)",
            params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        let pending = candidate(epoch, "/tmp/cursor.db");
        persist_or_load(&mut conn, &pending).unwrap();
        quarantine(
            &mut conn,
            epoch,
            "source_epoch_conflict_unresolved",
            "source_epoch_not_found: predecessor is absent",
        )
        .unwrap();

        replace_request_body_after_lineage_repair(
            &mut conn,
            epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            b"replacement",
            "fixture lineage proof",
            r#"{"v":1,"fixture":true}"#,
        )
        .unwrap();
        let repaired = super::load_for_epoch(&conn, epoch).unwrap().unwrap();
        assert_eq!(repaired.request_body_zstd, b"replacement");
        assert!(repaired.blocked_at.is_none());
        let audit: (Vec<u8>, Vec<u8>, String, String) = conn
            .query_row(
                "SELECT old_request_body_zstd, new_request_body_zstd, reason, proof_json
                 FROM pending_source_envelope_supersession WHERE source_epoch = ?1",
                [epoch.to_string()],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(audit.0, pending.request_body_zstd);
        assert_eq!(audit.1, b"replacement");
        assert_eq!(audit.2, "fixture lineage proof");
        assert_eq!(audit.3, r#"{"v":1,"fixture":true}"#);
        assert!(replace_request_body_after_lineage_repair(
            &mut conn,
            epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            b"stale replacement",
            "stale proof",
            r#"{"v":1}"#,
        )
        .is_err());
        let audit_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM pending_source_envelope_supersession WHERE source_epoch = ?1",
                [epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(audit_count, 1);
    }

    #[test]
    fn host_replacement_proof_retires_exact_blocked_body_with_audit() {
        let dir = tempfile::tempdir().unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let epoch = Uuid::new_v4();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, 'cursor', 'fixture-source', 'fixture',
                       'initial', 1, ?2, ?2)",
            params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO source_epoch_lane_state (
                 source_epoch, lane, last_position, updated_at
             ) VALUES (?1, 'durable', 0, ?2)",
            params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        let pending = candidate(epoch, "/tmp/cursor.db");
        persist_or_load(&mut conn, &pending).unwrap();
        quarantine(
            &mut conn,
            epoch,
            "source_epoch_conflict",
            "fixture replacement conflict",
        )
        .unwrap();

        retire_after_host_replacement(
            &mut conn,
            epoch,
            &pending.envelope_id,
            pending.range_start,
            pending.range_end,
            &pending.request_body_zstd,
            "fixture host replacement proof",
            r#"{"v":1,"fixture":true}"#,
        )
        .unwrap();
        assert!(super::load_for_epoch(&conn, epoch).unwrap().is_none());
        assert_eq!(
            conn.query_row(
                "SELECT last_position FROM source_epoch_lane_state
                 WHERE source_epoch = ?1 AND lane = 'durable'",
                [epoch.to_string()],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
        let audit: (Vec<u8>, Vec<u8>, String) = conn
            .query_row(
                "SELECT old_request_body_zstd, new_request_body_zstd, reason
                 FROM pending_source_envelope_supersession WHERE source_epoch = ?1",
                [epoch.to_string()],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(audit.0, pending.request_body_zstd);
        assert!(audit.1.is_empty());
        assert_eq!(audit.2, "fixture host replacement proof");
    }

    /// Register the epoch a pending row points at.
    ///
    /// `pending_source_envelope` carries a foreign key to
    /// `source_epoch_registry`, so a fixture that skips this cannot insert at
    /// all. Tests that forgot it failed with a bare "FOREIGN KEY constraint
    /// failed" rather than anything about what they were checking.
    fn register_epoch(conn: &rusqlite::Connection, source_epoch: Uuid, provider: &str) {
        conn.execute(
            "INSERT OR IGNORE INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, ?2, ?3, 'fixture', 'initial', 1, ?4, ?4)",
            params![
                source_epoch.to_string(),
                provider,
                format!("source-{source_epoch}"),
                "2026-07-15T00:00:00Z"
            ],
        )
        .unwrap();
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
