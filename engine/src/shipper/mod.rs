//! Reusable shipping functions extracted from the `ship` command.
//!
//! Used by both `cmd_ship` (one-shot bulk) and `cmd_connect` (daemon mode).
//! Core operations: parse+compress a single file, POST and record state,
//! startup recovery, spool replay.

mod types;

pub use types::*;

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::task;

use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::{ConsecutiveErrorTracker, RecentIssueTracker};
use crate::flight::FlightRecorder;
use crate::media_upload;
use crate::opencode_db;
use crate::pipeline::batcher::{self, PlannedRangeAction, ShipRange};
use crate::pipeline::compressor::{self, CompressionAlgo};
use crate::pipeline::parser::{self, ParseResult, ParsedMediaObject, ParsedSourceLine};
use crate::shipping::client::{ShipResult, ShipperClient};
use crate::shipping_stats::{
    RecentShipStatsTracker, ShipAttemptOutcome, ShipLane, ShipStageTimings,
};
use crate::source_line_claims;
use crate::state::file_identity::identity_from_metadata;
use crate::state::file_state::FileState;
use crate::state::live_file_state::LiveFileState;
use crate::state::spool::Spool;

/// Live-transcript batch target. Each ship is one HTTP round trip; for live
/// work this is a tail-latency knob, so we keep it small.
const LIVE_TARGET_BATCH_BYTES: u64 = 512 * 1024;
const BACKGROUND_REPAIR_TARGET_BATCH_BYTES: u64 = 32 * 1024;

/// Archive / replay batch target. This lane is reconstructable background
/// repair, so bound each server write first and accept extra HTTP overhead.
/// Large replay batches can spend seconds in SQLite/source-line dedupe on a
/// huge tenant DB, which defeats the runtime's interactive control SLO.
const ARCHIVE_TARGET_BATCH_BYTES: u64 = 256 * 1024;

// The Runtime Host allows archive ingest writes to run up to 60s. Archive,
// replay, and media sends should not give up first; otherwise the server may
// still commit the batch after the client has already marked it retryable.
const ARCHIVE_INGEST_TIMEOUT: Duration = Duration::from_secs(75);
const LIVE_TRANSCRIPT_INGEST_TIMEOUT: Duration = Duration::from_secs(20);
const ARCHIVE_BACKPRESSURE_RETRY_DELAY: Duration = Duration::from_secs(60);

/// Batch sizing band, independent of `SourceLineMode`. `SourceLineMode` is a
/// Codex-specific axis (whether to ship full source lines or event-only); the
/// batch band is keyed off `WorkPriority` so non-Codex live work also gets the
/// 512 KiB live target instead of accidentally inheriting the 4 MiB archive
/// target via `SourceLineMode::Full`. Phase-6 fix; see worktree commit log.
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub(crate) enum BatchBand {
    Live,
    BackgroundRepair,
    Archive,
}

/// Pick a batch target for the given band. Live transcript ships stay small
/// (latency); archive / replay ships go bigger (amortize round trip).
/// `max_batch_bytes` (configurable hard ceiling) clamps both bands.
#[cfg(test)]
fn target_batch_bytes_for_band(band: BatchBand, max_batch_bytes: u64) -> u64 {
    target_batch_bytes_for_band_with_archive_target(band, max_batch_bytes, None)
}

fn target_batch_bytes_for_band_with_archive_target(
    band: BatchBand,
    max_batch_bytes: u64,
    archive_target_batch_bytes: Option<u64>,
) -> u64 {
    let target = match band {
        BatchBand::Live => LIVE_TARGET_BATCH_BYTES,
        BatchBand::BackgroundRepair => BACKGROUND_REPAIR_TARGET_BATCH_BYTES,
        BatchBand::Archive => archive_target_batch_bytes.unwrap_or(ARCHIVE_TARGET_BATCH_BYTES),
    };
    max_batch_bytes.min(target).max(1)
}

#[cfg(test)]
mod target_batch_bytes_tests {
    use super::*;

    #[test]
    fn live_band_keeps_small_target_regardless_of_ceiling() {
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::Live, u64::MAX),
            LIVE_TARGET_BATCH_BYTES
        );
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::Live, 50 * 1024 * 1024),
            LIVE_TARGET_BATCH_BYTES
        );
    }

    #[test]
    fn archive_band_stays_below_live_work_when_ceiling_allows() {
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::Archive, u64::MAX),
            ARCHIVE_TARGET_BATCH_BYTES
        );
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::Archive, 50 * 1024 * 1024),
            ARCHIVE_TARGET_BATCH_BYTES
        );
    }

    #[test]
    fn archive_band_can_use_adaptive_target_above_default() {
        assert_eq!(
            target_batch_bytes_for_band_with_archive_target(
                BatchBand::Archive,
                u64::MAX,
                Some(crate::scheduler::ARCHIVE_BATCH_TARGET_MAX_BYTES),
            ),
            crate::scheduler::ARCHIVE_BATCH_TARGET_MAX_BYTES
        );
    }

    #[test]
    fn archive_target_is_below_live_target() {
        // Archive/replay is reconstructable background repair. Keep accepted
        // requests shorter than live ships so one old backlog cannot starve
        // provider control and liveness after deploy.
        assert!(ARCHIVE_TARGET_BATCH_BYTES < LIVE_TARGET_BATCH_BYTES);
    }

    #[test]
    fn background_repair_uses_small_target_below_live_work() {
        // Reconciliation scans are background repair. Keep each server write
        // short enough that hot runtime/live writes can interleave between POSTs.
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::BackgroundRepair, u64::MAX),
            BACKGROUND_REPAIR_TARGET_BATCH_BYTES
        );
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::BackgroundRepair, 50 * 1024 * 1024),
            BACKGROUND_REPAIR_TARGET_BATCH_BYTES
        );
        assert!(BACKGROUND_REPAIR_TARGET_BATCH_BYTES < ARCHIVE_TARGET_BATCH_BYTES);
    }

    #[test]
    fn max_batch_bytes_clamps_both_bands() {
        // A tight max_batch_bytes ceiling must still win, on both bands.
        let tight: u64 = 16 * 1024;
        assert_eq!(target_batch_bytes_for_band(BatchBand::Live, tight), tight);
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::BackgroundRepair, tight),
            tight
        );
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::Archive, tight),
            tight
        );
    }

    #[test]
    fn zero_max_batch_bytes_floors_to_one() {
        // Defensive: never return zero so the batcher's > 0 invariant holds.
        assert_eq!(target_batch_bytes_for_band(BatchBand::Live, 0), 1);
        assert_eq!(
            target_batch_bytes_for_band(BatchBand::BackgroundRepair, 0),
            1
        );
        assert_eq!(target_batch_bytes_for_band(BatchBand::Archive, 0), 1);
    }
}

fn request_timeout_for_trace(ship_trace: Option<&ShipTraceContext>) -> Option<Duration> {
    // Live transcript sends are user-visible, but they still go through the
    // durable ingest path. A very tight timeout creates retry storms during
    // normal hosted tail latency, which is worse than waiting a few seconds.
    match ship_trace.map(|trace| trace.work_context) {
        Some("live_transcript") => Some(LIVE_TRANSCRIPT_INGEST_TIMEOUT),
        _ => Some(ARCHIVE_INGEST_TIMEOUT),
    }
}

fn ship_lane_for_trace(ship_trace: Option<&ShipTraceContext>) -> ShipLane {
    match ship_trace.map(|trace| trace.work_context) {
        Some("live_transcript") => ShipLane::Live,
        Some("spool_replay") => ShipLane::Archive,
        Some("reconciliation_scan") => ShipLane::Repair,
        _ => ShipLane::Unknown,
    }
}

fn ship_lane_for_context(
    ship_trace: Option<&ShipTraceContext>,
    cursor_mode: CursorMode,
) -> ShipLane {
    match ship_lane_for_trace(ship_trace) {
        ShipLane::Unknown => match cursor_mode {
            CursorMode::Archive => ShipLane::Archive,
            CursorMode::Live => ShipLane::Live,
        },
        lane => lane,
    }
}

pub(crate) async fn ship_opencode_database(
    path: &Path,
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    tracker: Option<&ConsecutiveErrorTracker>,
    parse_tracker: Option<&RecentIssueTracker>,
) -> Result<(usize, usize)> {
    let file_state = FileState::new(conn);
    ensure_opencode_sqlite_state_table(conn)?;
    let sessions = opencode_db::list_opencode_sessions(path)?;
    let mut sessions_shipped = 0usize;
    let mut events_shipped = 0usize;

    for candidate in sessions {
        let current_offset = file_state.get_offset(&candidate.source_key)?;
        let current_fingerprint = get_opencode_sqlite_fingerprint(conn, &candidate.source_key)?;
        let persisted_longhouse_session_id =
            get_opencode_sqlite_longhouse_session_id(conn, &candidate.source_key)?;
        if candidate.version <= current_offset
            && current_fingerprint.as_deref() == Some(candidate.fingerprint.as_str())
        {
            continue;
        }

        let parse_result =
            match opencode_db::parse_opencode_session(path, &candidate.provider_session_id) {
                Ok(result) => result,
                Err(error) => {
                    record_parse_issue(parse_tracker);
                    tracing::warn!(
                        path = %path.display(),
                        provider_session_id = %candidate.provider_session_id,
                        error = %error,
                        "Skipping OpenCode session after parse failure"
                    );
                    continue;
                }
            };

        let new_offset = parse_result.last_good_offset.max(candidate.version);
        let provider_session_id = parse_result
            .metadata
            .provider_session_id
            .as_deref()
            .unwrap_or(&candidate.provider_session_id)
            .to_string();
        let longhouse_session_id =
            opencode_db::managed_longhouse_session_id_for_opencode(&provider_session_id)
                .or(persisted_longhouse_session_id)
                .unwrap_or_else(|| parse_result.metadata.session_id.clone());
        if parse_result.events.is_empty() && parse_result.source_lines.is_empty() {
            file_state.set_offset(
                &candidate.source_key,
                new_offset,
                &longhouse_session_id,
                &provider_session_id,
                "opencode",
            )?;
            set_opencode_sqlite_fingerprint(
                conn,
                &candidate.source_key,
                &candidate.fingerprint,
                candidate.version,
                &longhouse_session_id,
            )?;
            continue;
        }

        let rewind_hint =
            (current_offset > 0).then(|| full_document_rewrite_hint(&candidate.source_key));
        let compressed = compressor::build_and_compress_with_source_lines(
            &longhouse_session_id,
            &parse_result.events,
            &parse_result.metadata,
            &candidate.source_key,
            "opencode",
            Some(&parse_result.source_lines),
            rewind_hint.as_ref().map(std::slice::from_ref),
            algo,
        )?;
        let compressed_len = compressed.len() as u64;
        let result = if compressed_len <= max_batch_bytes {
            if let Err(error) = media_upload::ensure_media_uploaded(
                client,
                &longhouse_session_id,
                "opencode",
                &candidate.source_key,
                &parse_result.media_objects,
                Some(ARCHIVE_INGEST_TIMEOUT),
            )
            .await
            {
                tracing::warn!(
                    path = %candidate.source_key,
                    provider = "opencode",
                    provider_session_id = %candidate.provider_session_id,
                    error = %error,
                    "OpenCode SQLite media upload failed; leaving cursor unchanged for next scan"
                );
                continue;
            }
            client.ship(compressed).await
        } else {
            ShipResult::PayloadTooLarge(format!(
                "compressed OpenCode SQLite payload is {compressed_len} bytes which exceeds max_batch_bytes {max_batch_bytes}"
            ))
        };
        let session_events_shipped = match result {
            ShipResult::Ok { .. } => parse_result.events.len(),
            ShipResult::PayloadTooLarge(_) | ShipResult::PayloadRejected(_, _) => {
                tracing::error!(
                    path = %candidate.source_key,
                    provider = "opencode",
                    error = %transient_error_message(&result),
                    "OpenCode SQLite payload rejected; leaving cursor unchanged for next scan"
                );
                continue;
            }
            other => {
                let error = transient_error_message(&other);
                if tracker.map_or(true, |tracker| tracker.record_error()) {
                    tracing::warn!(
                        path = %path.display(),
                        provider_session_id = %candidate.provider_session_id,
                        error = %error,
                        "OpenCode SQLite ship failed; leaving cursor unchanged for next scan"
                    );
                }
                continue;
            }
        };

        if let Some(tracker) = tracker {
            tracker.record_success();
        }
        file_state.set_offset(
            &candidate.source_key,
            new_offset,
            &longhouse_session_id,
            &provider_session_id,
            "opencode",
        )?;
        set_opencode_sqlite_fingerprint(
            conn,
            &candidate.source_key,
            &candidate.fingerprint,
            candidate.version,
            &longhouse_session_id,
        )?;
        if session_events_shipped > 0 {
            sessions_shipped += 1;
            events_shipped += session_events_shipped;
        }
    }

    Ok((sessions_shipped, events_shipped))
}

fn ensure_opencode_sqlite_state_table(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS opencode_sqlite_state (
            source_key TEXT PRIMARY KEY,
            fingerprint TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0,
            longhouse_session_id TEXT,
            updated_at TEXT NOT NULL
        );",
    )?;
    let columns: Vec<String> = conn
        .prepare("PRAGMA table_info(opencode_sqlite_state)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .filter_map(|row| row.ok())
        .collect();
    if !columns
        .iter()
        .any(|column| column == "longhouse_session_id")
    {
        conn.execute(
            "ALTER TABLE opencode_sqlite_state ADD COLUMN longhouse_session_id TEXT",
            [],
        )?;
    }
    Ok(())
}

fn get_opencode_sqlite_fingerprint(conn: &Connection, source_key: &str) -> Result<Option<String>> {
    let result = conn.query_row(
        "SELECT fingerprint FROM opencode_sqlite_state WHERE source_key = ?1",
        [source_key],
        |row| row.get::<_, String>(0),
    );
    match result {
        Ok(value) => Ok(Some(value)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn get_opencode_sqlite_longhouse_session_id(
    conn: &Connection,
    source_key: &str,
) -> Result<Option<String>> {
    let result = conn.query_row(
        "SELECT longhouse_session_id FROM opencode_sqlite_state WHERE source_key = ?1",
        [source_key],
        |row| row.get::<_, Option<String>>(0),
    );
    match result {
        Ok(value) => Ok(value.filter(|value| uuid::Uuid::parse_str(value).is_ok())),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn set_opencode_sqlite_fingerprint(
    conn: &Connection,
    source_key: &str,
    fingerprint: &str,
    version: u64,
    longhouse_session_id: &str,
) -> Result<()> {
    conn.execute(
        "INSERT INTO opencode_sqlite_state (source_key, fingerprint, version, longhouse_session_id, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5)
         ON CONFLICT(source_key) DO UPDATE SET
             fingerprint = excluded.fingerprint,
             version = MAX(opencode_sqlite_state.version, excluded.version),
             longhouse_session_id = COALESCE(excluded.longhouse_session_id, opencode_sqlite_state.longhouse_session_id),
             updated_at = excluded.updated_at",
        rusqlite::params![
            source_key,
            fingerprint,
            version as i64,
            longhouse_session_id,
            Utc::now().to_rfc3339()
        ],
    )?;
    Ok(())
}

/// Parse and compress a single file from its current offset.
///
/// Returns `None` if the file has no new content, can't be read, or has no events.
#[cfg(test)]
pub fn prepare_file(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
) -> Result<Option<ShipItem>> {
    let path_str = path.to_string_lossy().to_string();
    let file_state = FileState::new(conn);

    let current_offset = file_state.get_offset(&path_str)?;
    let metadata = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };
    let file_size = metadata.len();
    let current_identity = identity_from_metadata(&metadata);
    let stored_identity = file_state.get_file_identity(&path_str)?;

    // Detect truncation before parse dispatch.
    let rewind_hint = if file_identity_changed_for_cursor(
        stored_identity.as_deref(),
        current_identity.as_deref(),
        current_offset,
        current_offset,
    ) {
        tracing::warn!(
            "File replaced: {} (identity {:?} -> {:?}), resetting",
            path_str,
            stored_identity,
            current_identity
        );
        file_state.reset_offsets(&path_str)?;
        Some(file_replacement_rewind_hint(&path_str))
    } else if file_size < current_offset {
        tracing::warn!(
            "File truncated: {} (was {}, now {}), resetting",
            path_str,
            current_offset,
            file_size
        );
        file_state.reset_offsets(&path_str)?;
        Some(truncation_rewind_hint(&path_str))
    } else {
        file_state.record_file_identity_if_missing(&path_str, current_identity.as_deref())?;
        None
    };

    let offset = if rewind_hint.is_some() {
        0
    } else if file_size == current_offset {
        // No new content
        return Ok(None);
    } else {
        current_offset
    };

    let parse_result = match parser::parse_session_file(path, offset) {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!("Skip {}: {}", path_str, e);
            return Ok(None);
        }
    };

    if parse_result.events.is_empty() && parse_result.source_lines.is_empty() {
        // Heuristic: if the file has substantial content and the parser found
        // candidate records but produced no events, something likely went wrong.
        if parse_result.candidate_records > 0 && file_size >= 128 {
            tracing::warn!(
                path = %path_str,
                file_size,
                candidate_records = parse_result.candidate_records,
                "Suspicious: file has {} candidate records but produced 0 events and 0 source lines — \
                 possible parser bug or format drift",
                parse_result.candidate_records
            );
        }
        return Ok(None);
    }

    let event_count = parse_result.events.len();
    let new_offset = parse_result.last_good_offset;
    let compressed = compressor::build_and_compress_with_source_lines(
        &parse_result.metadata.session_id,
        &parse_result.events,
        &parse_result.metadata,
        &path_str,
        provider,
        Some(&parse_result.source_lines),
        rewind_hint.as_ref().map(std::slice::from_ref),
        algo,
    )?;

    Ok(Some(ShipItem {
        path_str,
        provider: provider.to_string(),
        offset,
        new_offset,
        event_count,
        session_id: parse_result.metadata.session_id.clone(),
        source_line_offsets: parse_result
            .source_lines
            .iter()
            .map(|line| line.source_offset)
            .collect(),
        source_line_refs: source_line_refs_for_lines(&parse_result.source_lines),
        media_objects: parse_result.media_objects.clone(),
        compressed,
    }))
}

/// Ship a prepared item via HTTP. On success, update both offsets.
/// On transient failure, advance queued_offset and enqueue to spool.
/// On payload rejection, retain the rejected range as a dead letter.
///
/// Returns a structured outcome so callers can distinguish shipped, spooled,
/// and dead-lettered paths without duplicating transport logic.
#[cfg(test)]
#[allow(dead_code)]
pub async fn ship_and_record(
    item: ShipItem,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<ShipAndRecordOutcome> {
    let file_state = FileState::new(conn);
    let result = client.ship(item.compressed).await;

    match result {
        ShipResult::Ok { .. } => {
            // Emit recovery message if we were in an error state
            if let Some(t) = tracker {
                if let Some(n) = t.record_success() {
                    tracing::info!(
                        "Recovered after {} ship failure(s), now shipping normally",
                        n
                    );
                }
            }
            file_state.set_offset(
                &item.path_str,
                item.new_offset,
                &item.session_id,
                &item.session_id,
                &item.provider,
            )?;
            tracing::debug!(
                "Shipped {} ({} events, {} bytes)",
                item.path_str,
                item.event_count,
                item.new_offset - item.offset
            );
            Ok(ShipAndRecordOutcome::Shipped {
                events: item.event_count,
            })
        }
        ShipResult::RateLimited
        | ShipResult::ServerError(_, _)
        | ShipResult::ServerBackpressure(_)
        | ShipResult::ConnectError(_)
        | ShipResult::RetryableClientError(_, _) => {
            let err_msg = transient_error_message(&result);

            // Rate-limited logging: log 1st failure and every 100th
            let should_log = tracker.map_or(true, |t| t.record_error());
            if should_log {
                let count = tracker.map_or(1, |t| t.consecutive_count());
                if count > 1 {
                    tracing::warn!(
                        "Ship still failing after {} attempts, latest: {}",
                        count,
                        err_msg
                    );
                } else {
                    tracing::warn!("Spooled {}: {}", item.path_str, err_msg);
                }
            }

            let spool = Spool::new(conn);
            // Fix backpressure: only advance queued_offset if enqueue succeeds.
            // If spool is full, leave the gap unacknowledged — will retry on next startup recovery.
            let enqueued = spool.enqueue(
                &item.provider,
                &item.path_str,
                item.offset,
                item.new_offset,
                Some(&item.session_id),
            )?;
            if enqueued {
                file_state.set_queued_offset(
                    &item.path_str,
                    item.new_offset,
                    &item.provider,
                    &item.session_id,
                    &item.session_id,
                )?;
            } else {
                tracing::warn!(
                    "Spool at capacity — {} will be retried on next startup",
                    item.path_str
                );
            }
            // Signal ConnectError to caller so it can enter offline mode
            let is_connect_error = matches!(result, ShipResult::ConnectError(_));
            Ok(ShipAndRecordOutcome::Spooled { is_connect_error })
        }
        ShipResult::PayloadTooLarge(body) => {
            tracing::warn!(
                "Payload too large shipping {}: {}",
                item.path_str,
                truncate_http_body(&body)
            );
            let spool = Spool::new(conn);
            let enqueued = spool.enqueue(
                &item.provider,
                &item.path_str,
                item.offset,
                item.new_offset,
                Some(&item.session_id),
            )?;
            if enqueued {
                file_state.set_queued_offset(
                    &item.path_str,
                    item.new_offset,
                    &item.provider,
                    &item.session_id,
                    &item.session_id,
                )?;
            } else {
                tracing::warn!(
                    "Spool at capacity — 413 payload for {} will be retried on next startup",
                    item.path_str
                );
            }
            Ok(ShipAndRecordOutcome::Spooled {
                is_connect_error: false,
            })
        }
        ShipResult::PayloadRejected(code, body) => {
            tracing::error!(
                "Payload rejected shipping {}: {} {}",
                item.path_str,
                code,
                truncate_http_body(&body)
            );
            let spool = Spool::new(conn);
            let error = format!("payload rejected {}:{}", code, truncate_http_body(&body));
            spool.record_dead(
                &item.provider,
                &item.path_str,
                item.offset,
                item.new_offset,
                Some(&item.session_id),
                &error,
            )?;
            file_state.set_offset(
                &item.path_str,
                item.new_offset,
                &item.session_id,
                &item.session_id,
                &item.provider,
            )?;
            Ok(ShipAndRecordOutcome::DeadLettered { status_code: code })
        }
    }
}

fn log_suspicious_empty_parse(path_str: &str, file_size: u64, candidate_records: usize) {
    if candidate_records > 0 && file_size >= 128 {
        tracing::warn!(
            path = %path_str,
            file_size,
            candidate_records,
            "Suspicious: file has {} candidate records but produced 0 events and 0 source lines — \
             possible parser bug or format drift",
            candidate_records
        );
    }
}

fn record_parse_issue(parse_tracker: Option<&RecentIssueTracker>) {
    if let Some(tracker) = parse_tracker {
        tracker.record();
    }
}

fn log_suspicious_empty_parse_with_tracker(
    path_str: &str,
    file_size: u64,
    candidate_records: usize,
    parse_tracker: Option<&RecentIssueTracker>,
) {
    if candidate_records > 0 && file_size >= 128 {
        record_parse_issue(parse_tracker);
        log_suspicious_empty_parse(path_str, file_size, candidate_records);
    }
}

fn should_ack_empty_antigravity_legacy_document(
    provider: &str,
    parse_result: &ParseResult,
    offset: u64,
    new_offset: u64,
) -> bool {
    provider == "antigravity"
        && parse_result.events.is_empty()
        && parse_result.source_lines.is_empty()
        && parse_result.candidate_records > 0
        && new_offset > offset
}

fn event_range_for_offsets(
    events: &[parser::ParsedEvent],
    start_offset: u64,
    end_offset: u64,
) -> std::ops::Range<usize> {
    let start = events.partition_point(|event| event.source_offset < start_offset);
    let end = events.partition_point(|event| event.source_offset < end_offset);
    start..end
}

fn event_is_reply_evidence(event: &parser::ParsedEvent) -> bool {
    matches!(event.role, parser::Role::Assistant | parser::Role::Tool)
}

fn range_has_reply_evidence(
    events: &[parser::ParsedEvent],
    start_offset: u64,
    end_offset: u64,
) -> bool {
    events.iter().any(|event| {
        event.source_offset >= start_offset
            && event.source_offset < end_offset
            && event_is_reply_evidence(event)
    })
}

fn dead_letter_from_raw_range(
    path_str: &str,
    provider: &str,
    session_id: &str,
    range: batcher::DeadLetterRange,
    max_batch_bytes: u64,
) -> DeadLetterItem {
    DeadLetterItem {
        path_str: path_str.to_string(),
        provider: provider.to_string(),
        offset: range.start_offset,
        new_offset: range.end_offset,
        event_count: range
            .event_range
            .end
            .saturating_sub(range.event_range.start),
        session_id: session_id.to_string(),
        reason: format!(
            "source range {}..{} is {} bytes which exceeds max_batch_bytes {}",
            range.start_offset, range.end_offset, range.byte_len, max_batch_bytes
        ),
    }
}

fn ack_only_from_raw_range(
    path_str: &str,
    provider: &str,
    session_id: &str,
    start_offset: u64,
    end_offset: u64,
) -> AckOnlyItem {
    AckOnlyItem {
        path_str: path_str.to_string(),
        provider: provider.to_string(),
        offset: start_offset,
        new_offset: end_offset,
        session_id: session_id.to_string(),
    }
}

fn replay_split_offset_for_payload_too_large(item: &ShipItem) -> Option<u64> {
    let interior_offsets: Vec<u64> = item
        .source_line_offsets
        .iter()
        .copied()
        .filter(|offset| *offset > item.offset && *offset < item.new_offset)
        .collect();
    interior_offsets.get(interior_offsets.len() / 2).copied()
}

fn truncation_rewind_hint(path_str: &str) -> compressor::SourceRewindHint {
    compressor::SourceRewindHint {
        source_path: path_str.to_string(),
        source_offset: 0,
        reason: "truncation".to_string(),
    }
}

fn file_replacement_rewind_hint(path_str: &str) -> compressor::SourceRewindHint {
    compressor::SourceRewindHint {
        source_path: path_str.to_string(),
        source_offset: 0,
        reason: "file_replaced".to_string(),
    }
}

fn full_document_rewrite_hint(path_str: &str) -> compressor::SourceRewindHint {
    compressor::SourceRewindHint {
        source_path: path_str.to_string(),
        source_offset: 0,
        reason: "full_document_rewrite".to_string(),
    }
}

fn is_full_document_provider(provider: &str) -> bool {
    provider.eq_ignore_ascii_case("antigravity")
}

pub(crate) fn file_identity_changed_for_cursor(
    stored_identity: Option<&str>,
    current_identity: Option<&str>,
    current_offset: u64,
    queued_offset: u64,
) -> bool {
    if current_offset == 0 && queued_offset == 0 {
        return false;
    }
    matches!(
        (stored_identity, current_identity),
        (Some(stored), Some(current)) if stored != current
    )
}

fn dead_letter_from_compressed_range(
    path_str: &str,
    provider: &str,
    session_id: &str,
    range: &ShipRange,
    max_batch_bytes: u64,
    compressed_len: usize,
) -> DeadLetterItem {
    DeadLetterItem {
        path_str: path_str.to_string(),
        provider: provider.to_string(),
        offset: range.start_offset,
        new_offset: range.end_offset,
        event_count: range.event_range.end.saturating_sub(range.event_range.start),
        session_id: session_id.to_string(),
        reason: format!(
            "compressed payload for source range {}..{} is {} bytes which exceeds max_batch_bytes {}",
            range.start_offset, range.end_offset, compressed_len, max_batch_bytes
        ),
    }
}

fn resolve_payload_session_id<'a>(
    provider: &str,
    parse_result: &'a ParseResult,
    session_id_override: Option<&'a str>,
) -> &'a str {
    let Some(override_session_id) = session_id_override else {
        return &parse_result.metadata.session_id;
    };

    // Override must be a valid UUID — non-UUID bindings (e.g. human-readable
    // session names from testing) would be rejected by the ingest endpoint.
    if uuid::Uuid::parse_str(override_session_id).is_err() {
        return &parse_result.metadata.session_id;
    }

    // Managed-local Codex roots deliberately override the ingest UUID so the
    // transcript binds to the Longhouse-owned session row. Forked subagent
    // transcripts are different: they carry their own native session_meta id
    // plus forked_from_id, and forcing them onto the parent Longhouse UUID
    // collapses every child transcript into the parent session.
    if provider.eq_ignore_ascii_case("codex")
        && (parse_result.metadata.forked_from_session_id.is_some()
            || parse_result.metadata.is_sidechain)
        && override_session_id != parse_result.metadata.session_id
    {
        return &parse_result.metadata.session_id;
    }

    override_session_id
}

fn event_only_ship_offsets(parse_result: &ParseResult, range: &ShipRange) -> (u64, u64) {
    if range.event_range.is_empty() {
        return (range.start_offset, range.end_offset);
    }

    let first_event_offset = parse_result.events[range.event_range.start].source_offset;
    let last_event_offset = parse_result.events[range.event_range.end - 1].source_offset;
    let next_line_idx = parse_result
        .source_lines
        .partition_point(|line| line.source_offset <= last_event_offset);
    let event_end_offset = parse_result
        .source_lines
        .get(next_line_idx)
        .map(|line| line.source_offset)
        .unwrap_or(range.end_offset);

    (
        first_event_offset.max(range.start_offset),
        event_end_offset.max(first_event_offset),
    )
}

fn media_objects_for_offset_range(
    parse_result: &ParseResult,
    start_offset: u64,
    end_offset: u64,
) -> Vec<ParsedMediaObject> {
    parse_result
        .media_objects
        .iter()
        .filter(|media| media.source_offset >= start_offset && media.source_offset < end_offset)
        .cloned()
        .collect()
}

fn source_line_refs_for_lines(lines: &[ParsedSourceLine]) -> Vec<SourceLineRef> {
    lines
        .iter()
        .map(|line| SourceLineRef {
            source_offset: line.source_offset,
            line_hash: format!("{:x}", Sha256::digest(line.raw_line.as_bytes())),
        })
        .collect()
}

fn source_line_refs_for_offset_range(
    parse_result: &ParseResult,
    start_offset: u64,
    end_offset: u64,
) -> Vec<SourceLineRef> {
    source_line_refs_for_lines(
        &parse_result.source_lines[parse_result
            .source_lines
            .partition_point(|line| line.source_offset < start_offset)
            ..parse_result
                .source_lines
                .partition_point(|line| line.source_offset < end_offset)],
    )
}

fn prepare_whole_document_action(
    parse_result: &ParseResult,
    path_str: &str,
    provider: &str,
    start_offset: u64,
    end_offset: u64,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    rewind_hint: Option<&compressor::SourceRewindHint>,
) -> Result<Vec<PreparedAction>> {
    let payload_session_id =
        resolve_payload_session_id(provider, parse_result, session_id_override);
    let compressed = compressor::build_and_compress_with(
        payload_session_id,
        &parse_result.events,
        &parse_result.metadata,
        path_str,
        provider,
        rewind_hint.map(std::slice::from_ref),
        algo,
    )?;

    if compressed.len() as u64 <= max_batch_bytes {
        return Ok(vec![PreparedAction::Ship(ShipItem {
            path_str: path_str.to_string(),
            provider: provider.to_string(),
            offset: start_offset,
            new_offset: end_offset,
            event_count: parse_result.events.len(),
            session_id: payload_session_id.to_string(),
            source_line_offsets: Vec::new(),
            source_line_refs: source_line_refs_for_offset_range(
                parse_result,
                start_offset,
                end_offset,
            ),
            media_objects: media_objects_for_offset_range(parse_result, start_offset, end_offset),
            compressed,
        })]);
    }

    Ok(vec![PreparedAction::DeadLetter(DeadLetterItem {
        path_str: path_str.to_string(),
        provider: provider.to_string(),
        offset: start_offset,
        new_offset: end_offset,
        event_count: parse_result.events.len(),
        session_id: payload_session_id.to_string(),
        reason: format!(
            "compressed whole-document payload is {} bytes which exceeds max_batch_bytes {} and has no source-line boundaries for further splitting",
            compressed.len(),
            max_batch_bytes
        ),
    })])
}

fn materialize_ship_range(
    parse_result: &ParseResult,
    path_str: &str,
    provider: &str,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    range: ShipRange,
    session_id_override: Option<&str>,
    rewind_hint: Option<&compressor::SourceRewindHint>,
    source_line_mode: SourceLineMode,
) -> Result<(Vec<PreparedAction>, bool)> {
    let payload_session_id =
        resolve_payload_session_id(provider, parse_result, session_id_override);
    let source_lines = match source_line_mode {
        SourceLineMode::Full => Some(&parse_result.source_lines[range.source_line_range.clone()]),
        SourceLineMode::EventOnly => None,
    };
    let compressed = compressor::build_and_compress_with_source_lines(
        payload_session_id,
        &parse_result.events[range.event_range.clone()],
        &parse_result.metadata,
        path_str,
        provider,
        source_lines,
        rewind_hint.map(std::slice::from_ref),
        algo,
    )?;

    if compressed.len() as u64 <= max_batch_bytes {
        let (item_offset, item_new_offset) = match source_line_mode {
            SourceLineMode::Full => (range.start_offset, range.end_offset),
            SourceLineMode::EventOnly => event_only_ship_offsets(parse_result, &range),
        };
        return Ok((
            vec![PreparedAction::Ship(ShipItem {
                path_str: path_str.to_string(),
                provider: provider.to_string(),
                offset: item_offset,
                new_offset: item_new_offset,
                event_count: range
                    .event_range
                    .end
                    .saturating_sub(range.event_range.start),
                session_id: payload_session_id.to_string(),
                source_line_offsets: match source_line_mode {
                    SourceLineMode::Full => parse_result.source_lines
                        [range.source_line_range.clone()]
                    .iter()
                    .map(|line| line.source_offset)
                    .collect(),
                    SourceLineMode::EventOnly => Vec::new(),
                },
                source_line_refs: match source_line_mode {
                    SourceLineMode::Full => source_line_refs_for_lines(
                        &parse_result.source_lines[range.source_line_range.clone()],
                    ),
                    SourceLineMode::EventOnly => Vec::new(),
                },
                media_objects: media_objects_for_offset_range(
                    parse_result,
                    item_offset,
                    item_new_offset,
                ),
                compressed,
            })],
            rewind_hint.is_some(),
        ));
    }

    let line_count = range
        .source_line_range
        .end
        .saturating_sub(range.source_line_range.start);
    if line_count <= 1 {
        return Ok((
            vec![PreparedAction::DeadLetter(
                dead_letter_from_compressed_range(
                    path_str,
                    provider,
                    &parse_result.metadata.session_id,
                    &range,
                    max_batch_bytes,
                    compressed.len(),
                ),
            )],
            false,
        ));
    }

    let mid_line_idx = range.source_line_range.start + line_count / 2;
    let mid_offset = parse_result.source_lines[mid_line_idx].source_offset;
    let left = ShipRange {
        start_offset: range.start_offset,
        end_offset: mid_offset,
        source_line_range: range.source_line_range.start..mid_line_idx,
        event_range: event_range_for_offsets(&parse_result.events, range.start_offset, mid_offset),
    };
    let right = ShipRange {
        start_offset: mid_offset,
        end_offset: range.end_offset,
        source_line_range: mid_line_idx..range.source_line_range.end,
        event_range: event_range_for_offsets(&parse_result.events, mid_offset, range.end_offset),
    };

    let (mut actions, left_used_hint) = materialize_ship_range(
        parse_result,
        path_str,
        provider,
        algo,
        max_batch_bytes,
        left,
        session_id_override,
        rewind_hint,
        source_line_mode,
    )?;
    let (right_actions, right_used_hint) = materialize_ship_range(
        parse_result,
        path_str,
        provider,
        algo,
        max_batch_bytes,
        right,
        session_id_override,
        if left_used_hint { None } else { rewind_hint },
        source_line_mode,
    )?;
    actions.extend(right_actions);
    Ok((actions, left_used_hint || right_used_hint))
}

fn build_prepared_actions(
    parse_result: &ParseResult,
    path_str: &str,
    provider: &str,
    start_offset: u64,
    end_offset: u64,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    rewind_hint: Option<&compressor::SourceRewindHint>,
    source_line_mode: SourceLineMode,
    batch_band: BatchBand,
    archive_target_batch_bytes: Option<u64>,
) -> Result<Vec<PreparedAction>> {
    if parse_result.events.is_empty()
        && parse_result.candidate_records == 0
        && !parse_result.source_lines.is_empty()
    {
        return Ok(vec![PreparedAction::AckOnly(ack_only_from_raw_range(
            path_str,
            provider,
            &parse_result.metadata.session_id,
            start_offset,
            end_offset,
        ))]);
    }

    if parse_result.source_lines.is_empty() {
        if parse_result.events.is_empty() {
            return Ok(Vec::new());
        }

        return prepare_whole_document_action(
            parse_result,
            path_str,
            provider,
            start_offset,
            end_offset,
            algo,
            max_batch_bytes,
            session_id_override,
            rewind_hint,
        );
    }

    let mut actions = Vec::new();
    let mut pending_rewind_hint = rewind_hint;
    for planned in batcher::plan_range_actions_with_limits(
        &parse_result.source_lines,
        &parse_result.events,
        start_offset,
        end_offset,
        target_batch_bytes_for_band_with_archive_target(
            batch_band,
            max_batch_bytes,
            archive_target_batch_bytes,
        ),
        max_batch_bytes,
    )? {
        match planned {
            PlannedRangeAction::Ship(range) => {
                let (range_actions, used_hint) = materialize_ship_range(
                    parse_result,
                    path_str,
                    provider,
                    algo,
                    max_batch_bytes,
                    range,
                    session_id_override,
                    pending_rewind_hint,
                    source_line_mode,
                )?;
                actions.extend(range_actions);
                if used_hint {
                    pending_rewind_hint = None;
                }
            }
            PlannedRangeAction::DeadLetter(range) => {
                if range.event_range.is_empty() {
                    actions.push(PreparedAction::AckOnly(ack_only_from_raw_range(
                        path_str,
                        provider,
                        &parse_result.metadata.session_id,
                        range.start_offset,
                        range.end_offset,
                    )));
                } else {
                    actions.push(PreparedAction::DeadLetter(dead_letter_from_raw_range(
                        path_str,
                        provider,
                        &parse_result.metadata.session_id,
                        range,
                        max_batch_bytes,
                    )))
                }
            }
        }
    }
    Ok(actions)
}

pub fn prepare_path_range(
    path: &Path,
    provider: &str,
    offset: u64,
    end_offset_cap: Option<u64>,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
) -> Result<Option<PreparedFile>> {
    prepare_path_range_with_parse_tracker(
        path,
        provider,
        offset,
        end_offset_cap,
        algo,
        max_batch_bytes,
        session_id_override,
        None,
        None,
        SourceLineMode::Full,
        BatchBand::Archive,
        None,
    )
}

pub(crate) fn prepare_path_range_with_parse_tracker(
    path: &Path,
    provider: &str,
    offset: u64,
    end_offset_cap: Option<u64>,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    parse_tracker: Option<&RecentIssueTracker>,
    rewind_hint: Option<&compressor::SourceRewindHint>,
    source_line_mode: SourceLineMode,
    batch_band: BatchBand,
    archive_target_batch_bytes: Option<u64>,
) -> Result<Option<PreparedFile>> {
    prepare_path_range_with_parse_tracker_and_trace(
        path,
        provider,
        offset,
        end_offset_cap,
        algo,
        max_batch_bytes,
        session_id_override,
        parse_tracker,
        rewind_hint,
        source_line_mode,
        batch_band,
        None,
        archive_target_batch_bytes,
    )
}

pub(crate) fn prepare_path_range_with_parse_tracker_and_trace(
    path: &Path,
    provider: &str,
    offset: u64,
    end_offset_cap: Option<u64>,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    parse_tracker: Option<&RecentIssueTracker>,
    rewind_hint: Option<&compressor::SourceRewindHint>,
    source_line_mode: SourceLineMode,
    batch_band: BatchBand,
    mut prepare_trace: Option<&mut PrepareTraceTimings>,
    archive_target_batch_bytes: Option<u64>,
) -> Result<Option<PreparedFile>> {
    let path_str = path.to_string_lossy().to_string();
    let file_size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };

    let parse_started = Instant::now();
    let parse_result = match parser::parse_session_file(path, offset) {
        Ok(result) => result,
        Err(e) => {
            if let Some(trace) = prepare_trace.as_deref_mut() {
                trace.parse_ms = Some(parse_started.elapsed().as_millis() as u64);
            }
            record_parse_issue(parse_tracker);
            tracing::warn!("Skip {}: {}", path_str, e);
            return Ok(None);
        }
    };
    if let Some(trace) = prepare_trace.as_deref_mut() {
        trace.parse_ms = Some(parse_started.elapsed().as_millis() as u64);
    }

    let new_offset = end_offset_cap
        .map(|cap| parse_result.last_good_offset.min(cap))
        .unwrap_or(parse_result.last_good_offset);

    if should_ack_empty_antigravity_legacy_document(provider, &parse_result, offset, new_offset) {
        return Ok(Some(PreparedFile {
            path_str: path_str.clone(),
            offset,
            new_offset,
            has_reply_evidence: false,
            cursor_mode: CursorMode::Archive,
            actions: vec![PreparedAction::AckOnly(AckOnlyItem {
                path_str,
                provider: provider.to_string(),
                offset,
                new_offset,
                session_id: parse_result.metadata.session_id.clone(),
            })],
        }));
    }

    if parse_result.events.is_empty() && parse_result.source_lines.is_empty() {
        log_suspicious_empty_parse_with_tracker(
            &path_str,
            file_size,
            parse_result.candidate_records,
            parse_tracker,
        );
        return Ok(None);
    }

    if new_offset <= offset {
        return Ok(None);
    }

    let has_reply_evidence = range_has_reply_evidence(&parse_result.events, offset, new_offset);

    let batch_build_started = Instant::now();
    let actions = build_prepared_actions(
        &parse_result,
        &path_str,
        provider,
        offset,
        new_offset,
        algo,
        max_batch_bytes,
        session_id_override,
        rewind_hint,
        source_line_mode,
        batch_band,
        archive_target_batch_bytes,
    )?;
    if let Some(trace) = prepare_trace.as_deref_mut() {
        trace.batch_build_ms = Some(batch_build_started.elapsed().as_millis() as u64);
    }

    if actions.is_empty() {
        return Ok(None);
    }

    Ok(Some(PreparedFile {
        path_str,
        offset,
        new_offset,
        has_reply_evidence,
        cursor_mode: match source_line_mode {
            SourceLineMode::Full => CursorMode::Archive,
            SourceLineMode::EventOnly => CursorMode::Live,
        },
        actions,
    }))
}

pub fn prepare_path_from_offset(
    path: &Path,
    provider: &str,
    offset: u64,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
) -> Result<Option<PreparedFile>> {
    prepare_path_range(path, provider, offset, None, algo, max_batch_bytes, None)
}

pub fn prepare_file_batches(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
) -> Result<Option<PreparedFile>> {
    prepare_file_batches_with_parse_tracker(
        path,
        provider,
        algo,
        conn,
        max_batch_bytes,
        session_id_override,
        None,
    )
}

pub(crate) fn prepare_file_batches_with_parse_tracker(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    parse_tracker: Option<&RecentIssueTracker>,
) -> Result<Option<PreparedFile>> {
    prepare_file_batches_with_source_line_mode_and_parse_tracker(
        path,
        provider,
        algo,
        conn,
        max_batch_bytes,
        session_id_override,
        parse_tracker,
        SourceLineMode::Full,
        BatchBand::Archive,
    )
}

pub(crate) fn prepare_file_batches_with_source_line_mode_and_parse_tracker(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    parse_tracker: Option<&RecentIssueTracker>,
    source_line_mode: SourceLineMode,
    batch_band: BatchBand,
) -> Result<Option<PreparedFile>> {
    prepare_file_batches_with_source_line_mode_parse_tracker_and_trace(
        path,
        provider,
        algo,
        conn,
        max_batch_bytes,
        session_id_override,
        parse_tracker,
        source_line_mode,
        batch_band,
        None,
    )
}

pub(crate) fn prepare_file_batches_with_source_line_mode_parse_tracker_and_trace(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
    max_batch_bytes: u64,
    session_id_override: Option<&str>,
    parse_tracker: Option<&RecentIssueTracker>,
    source_line_mode: SourceLineMode,
    batch_band: BatchBand,
    mut prepare_trace: Option<&mut PrepareTraceTimings>,
) -> Result<Option<PreparedFile>> {
    let path_str = path.to_string_lossy().to_string();
    let file_state = FileState::new(conn);
    let live_file_state = LiveFileState::new(conn);
    let cursor_mode = match source_line_mode {
        SourceLineMode::Full => CursorMode::Archive,
        SourceLineMode::EventOnly => CursorMode::Live,
    };

    let cursor_started = Instant::now();
    let current_offset = match cursor_mode {
        CursorMode::Archive => file_state.get_offset(&path_str)?,
        CursorMode::Live => live_file_state.get_offset(&path_str)?,
    };
    let queued_offset = match cursor_mode {
        CursorMode::Archive => file_state.get_queued_offset(&path_str)?,
        CursorMode::Live => current_offset,
    };
    let metadata = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };
    let file_size = metadata.len();
    let current_identity = identity_from_metadata(&metadata);
    let stored_identity = match cursor_mode {
        CursorMode::Archive => file_state.get_file_identity(&path_str)?,
        CursorMode::Live => live_file_state.get_file_identity(&path_str)?,
    };

    let mut rewind_hint = if file_identity_changed_for_cursor(
        stored_identity.as_deref(),
        current_identity.as_deref(),
        current_offset,
        queued_offset,
    ) {
        tracing::warn!(
            "File replaced: {} (identity {:?} -> {:?}), resetting",
            path_str,
            stored_identity,
            current_identity
        );
        match cursor_mode {
            CursorMode::Archive => {
                let stale = Spool::new(conn).dead_letter_pending_for_path(
                    &path_str,
                    "source file identity changed before replay; stale pointer retired",
                )?;
                if stale > 0 {
                    tracing::warn!(
                        path = %path_str,
                        stale_pending_spool_entries = stale,
                        "Retired stale pending spool entries after source replacement"
                    );
                }
                file_state.reset_offsets(&path_str)?;
            }
            CursorMode::Live => live_file_state.reset_offset(&path_str)?,
        }
        Some(file_replacement_rewind_hint(&path_str))
    } else if file_size < current_offset {
        tracing::warn!(
            "File truncated: {} (was {}, now {}), resetting",
            path_str,
            current_offset,
            file_size
        );
        match cursor_mode {
            CursorMode::Archive => file_state.reset_offsets(&path_str)?,
            CursorMode::Live => live_file_state.reset_offset(&path_str)?,
        }
        Some(truncation_rewind_hint(&path_str))
    } else {
        match cursor_mode {
            CursorMode::Archive => {
                file_state
                    .record_file_identity_if_missing(&path_str, current_identity.as_deref())?;
            }
            CursorMode::Live => {
                live_file_state
                    .record_file_identity_if_missing(&path_str, current_identity.as_deref())?;
            }
        }
        None
    };

    let offset = if rewind_hint.is_some() {
        0
    } else if queued_offset > current_offset {
        tracing::debug!(
            path = %path_str,
            acked_offset = current_offset,
            queued_offset,
            "Skipping fresh ship because file has an unacked queued gap"
        );
        return Ok(None);
    } else if file_size == current_offset {
        return Ok(None);
    } else if is_full_document_provider(provider) && current_offset > 0 {
        // Gemini session JSON is rewritten in place as a whole document. The
        // parser deliberately ignores incremental offsets and returns source
        // lines from byte 0, so resuming from an old byte offset can land in
        // the middle of a JSON line and fail batch planning. Re-ship the full
        // document; ingest dedupes existing events and branches source lines
        // using the explicit rewind hint.
        rewind_hint = Some(full_document_rewrite_hint(&path_str));
        0
    } else {
        current_offset
    };
    if let Some(trace) = prepare_trace.as_deref_mut() {
        trace.cursor_ms = Some(cursor_started.elapsed().as_millis() as u64);
    }

    prepare_path_range_with_parse_tracker_and_trace(
        path,
        provider,
        offset,
        None,
        algo,
        max_batch_bytes,
        session_id_override,
        parse_tracker,
        rewind_hint.as_ref(),
        source_line_mode,
        batch_band,
        prepare_trace,
        None,
    )
}

#[tracing::instrument(
    level = "info",
    name = "engine.ship.attempt",
    skip(item, client, tracker, ship_stats, flight_recorder, limiter),
    fields(
        otel.kind = "client",
        http.request.method = "POST",
        http.route = "/api/agents/ingest",
        http.response.status_code = tracing::field::Empty,
        longhouse.provider = %item.provider,
        longhouse.ship.event_count = item.event_count as u64,
        longhouse.ship.range_bytes = item.new_offset.saturating_sub(item.offset),
        longhouse.ship.outcome = tracing::field::Empty,
        longhouse.ship.error_kind = tracing::field::Empty,
    )
)]
async fn attempt_ship(
    mut item: ShipItem,
    client: &ShipperClient,
    tracker: Option<&ConsecutiveErrorTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
    ship_trace: Option<&ShipTraceContext>,
    ship_lane: ShipLane,
    flight_recorder: Option<&FlightRecorder>,
    limiter: Option<&crate::scheduler::AdaptiveLimiter>,
) -> AttemptedShip {
    let http_send_started_at_ms = chrono::Utc::now().timestamp_millis();
    let request_timeout = request_timeout_for_trace(ship_trace);
    let mut flight_record = build_ship_trace_value(&item, ship_trace, http_send_started_at_ms);
    if let Some(timeout) = request_timeout {
        insert_json_field(
            &mut flight_record,
            "request_timeout_ms",
            json!(timeout.as_millis().min(u128::from(u64::MAX)) as u64),
        );
    } else {
        insert_json_field(&mut flight_record, "request_timeout_ms", Value::Null);
    }
    let trace_header = ship_trace.and_then(|_| {
        let mut header_record = flight_record.clone();
        remove_json_field(&mut header_record, "kind");
        remove_json_field(&mut header_record, "path");
        remove_json_field(&mut header_record, "request_timeout_ms");
        serde_json::to_string(&header_record).ok()
    });
    if matches!(ship_lane, ShipLane::Archive | ShipLane::Repair)
        && !item.source_line_refs.is_empty()
    {
        match source_line_claims::claim_source_lines_present(
            client,
            &item.session_id,
            &item.path_str,
            &item.source_line_refs,
            request_timeout,
        )
        .await
        {
            Ok(summary) if summary.claimed > 0 && summary.missing == 0 => {
                if !item.media_objects.is_empty() {
                    if let Err(err) = media_upload::ensure_media_uploaded(
                        client,
                        &item.session_id,
                        &item.provider,
                        &item.path_str,
                        &item.media_objects,
                        request_timeout,
                    )
                    .await
                    {
                        return AttemptedShip::MediaUploadFailed {
                            item,
                            error: err.to_string(),
                        };
                    }
                }
                tracing::info!(
                    path = %item.path_str,
                    provider = %item.provider,
                    session_id = %item.session_id,
                    source_lines = summary.present,
                    "Archive transcript range already durable; reconciling without replay"
                );
                return AttemptedShip::Reconciled(item);
            }
            Ok(summary) => {
                tracing::debug!(
                    path = %item.path_str,
                    provider = %item.provider,
                    session_id = %item.session_id,
                    source_lines_present = summary.present,
                    source_lines_missing = summary.missing,
                    "Archive transcript range still needs ingest"
                );
            }
            Err(error) => {
                tracing::debug!(
                    path = %item.path_str,
                    provider = %item.provider,
                    session_id = %item.session_id,
                    error = %error,
                    "Source-line reconciliation unavailable; continuing with ingest"
                );
            }
        }
    }
    if !item.media_objects.is_empty() {
        match media_upload::ensure_media_uploaded(
            client,
            &item.session_id,
            &item.provider,
            &item.path_str,
            &item.media_objects,
            request_timeout,
        )
        .await
        {
            Ok(summary) => {
                tracing::debug!(
                    path = %item.path_str,
                    provider = %item.provider,
                    session_id = %item.session_id,
                    media_claimed = summary.claimed,
                    media_present = summary.already_present,
                    media_uploaded = summary.uploaded,
                    "Archive media ready before ingest"
                );
            }
            Err(err) => {
                return AttemptedShip::MediaUploadFailed {
                    item,
                    error: err.to_string(),
                };
            }
        }
    }
    let payload = std::mem::take(&mut item.compressed);
    let attempt_started = std::time::Instant::now();
    let client_for_task = client.clone();
    let trace_header_for_task = trace_header.clone();
    let ship_task = tokio::spawn(async move {
        let task_started = std::time::Instant::now();
        let result = if trace_header_for_task.is_none() && request_timeout.is_none() {
            client_for_task.ship(payload).await
        } else {
            client_for_task
                .ship_with_trace_and_timeout(
                    payload,
                    trace_header_for_task.as_deref(),
                    request_timeout,
                )
                .await
        };
        (result, task_started.elapsed().as_millis() as u64)
    });
    let (result, latency_ms) = match ship_task.await {
        Ok(result) => result,
        Err(err) => (
            ShipResult::ConnectError(crate::shipping::client::ConnectErrorDetail {
                kind: "task_join",
                message: format!("HTTP ship task failed: {err}"),
            }),
            attempt_started.elapsed().as_millis() as u64,
        ),
    };
    let join_latency_ms = attempt_started.elapsed().as_millis() as u64;
    let http_finished_at_ms = chrono::Utc::now().timestamp_millis();
    let local_join_delay_ms = join_latency_ms.saturating_sub(latency_ms);
    if local_join_delay_ms > 1_000 {
        tracing::warn!(
            latency_ms,
            join_latency_ms,
            local_join_delay_ms,
            "HTTP ship task completed slower from local scheduler perspective"
        );
    }
    let span = tracing::Span::current();
    let (outcome, http_status) = classify_ship_attempt_result(&result);
    let is_backpressure = ship_result_is_backpressure(&result);
    let byte_count = item.new_offset.saturating_sub(item.offset);
    span.record(
        "longhouse.ship.outcome",
        tracing::field::display(outcome.as_str()),
    );
    if let Some(status) = http_status {
        span.record("http.response.status_code", tracing::field::display(status));
    }
    let error_kind = transient_error_kind(&result);
    let error_message = match &result {
        ShipResult::Ok { .. } => None,
        _ => Some(transient_error_message(&result)),
    };
    let stage_timings = stage_timings_for_trace(
        ship_trace,
        http_send_started_at_ms,
        http_finished_at_ms,
        latency_ms,
    );
    if let Some(kind) = error_kind {
        span.record("longhouse.ship.error_kind", tracing::field::display(kind));
    }
    if is_backpressure {
        if let Some(limiter) = limiter {
            limiter.observe_backpressure(ship_result_retry_after(&result));
        }
    }
    if let Some(stats) = ship_stats {
        stats.record_with_lane_detail_and_stages(
            ship_lane,
            outcome,
            latency_ms,
            http_status,
            error_kind,
            error_message.as_deref(),
            item.event_count as u32,
            byte_count,
            is_backpressure,
            stage_timings,
        );
        if ship_lane == ShipLane::Live {
            if let Some(limiter) = limiter {
                let live = stats.summary().lanes.live;
                limiter.observe_live_latency(
                    live.latency_p95_ms_1h,
                    live.stage_latency_p95_ms_1h
                        .get("enqueue_to_job_ms")
                        .copied(),
                );
            }
        }
    }
    if is_backpressure {
        tracing::debug!(
            path = %item.path_str,
            provider = %item.provider,
            error_kind = error_kind.unwrap_or("backpressure"),
            error = %error_message.as_deref().unwrap_or("runtime backpressure"),
            latency_ms,
            "Runtime asked archive replay to retry later"
        );
    }

    match result {
        ShipResult::Ok { server_timing } => {
            record_flight_attempt(
                flight_recorder,
                &flight_record,
                http_finished_at_ms,
                latency_ms,
                join_latency_ms,
                outcome.as_str(),
                http_status,
                error_kind,
                "shipped",
            );
            if let Some(t) = tracker {
                if let Some(n) = t.record_success() {
                    tracing::info!(
                        "Recovered after {} ship failure(s), now shipping normally",
                        n
                    );
                }
            }
            if server_timing.is_observed() {
                tracing::debug!(
                    target: "longhouse_engine::server_timing",
                    queue_wait_ms = ?server_timing.queue_wait_ms,
                    exec_ms = ?server_timing.exec_ms,
                    label = ?server_timing.label,
                    lane = ?server_timing.lane,
                    admission_state = ?server_timing.admission_state,
                    "server-side ingest timing"
                );
            }
            if let Some(limiter) = limiter {
                match server_timing.queue_wait_ms {
                    Some(qw) => limiter.observe_ingest_timing(
                        qw,
                        server_timing.exec_ms,
                        server_timing.commit_count,
                        server_timing.commit_ms,
                        server_timing.chunk_size,
                        server_timing.store_stage_ms.clone(),
                    ),
                    None => limiter.note_missing_signal(),
                }
            }
            tracing::debug!(
                "Shipped {} ({} events, {} bytes)",
                item.path_str,
                item.event_count,
                item.new_offset - item.offset
            );
            AttemptedShip::Shipped(item)
        }
        ShipResult::RateLimited
        | ShipResult::ServerError(_, _)
        | ShipResult::ServerBackpressure(_)
        | ShipResult::ConnectError(_)
        | ShipResult::RetryableClientError(_, _) => {
            record_flight_attempt(
                flight_recorder,
                &flight_record,
                http_finished_at_ms,
                latency_ms,
                join_latency_ms,
                outcome.as_str(),
                http_status,
                error_kind,
                "retryable",
            );
            let error = error_message.unwrap_or_else(|| transient_error_message(&result));
            let should_log = if is_backpressure {
                true
            } else {
                tracker.map_or(true, |t| t.record_error())
            };
            if should_log {
                if is_backpressure {
                    tracing::info!(
                        path = %item.path_str,
                        provider = %item.provider,
                        error_kind = error_kind.unwrap_or("backpressure"),
                        error = %error,
                        retry_after_ms = ship_result_retry_after(&result)
                            .unwrap_or(ARCHIVE_BACKPRESSURE_RETRY_DELAY)
                            .as_millis() as u64,
                        latency_ms,
                        "Archive replay deferred by runtime backpressure"
                    );
                } else {
                    let count = tracker.map_or(1, |t| t.consecutive_count());
                    if count > 1 {
                        tracing::warn!(
                            path = %item.path_str,
                            provider = %item.provider,
                            error_kind = error_kind.unwrap_or("unknown"),
                            error = %error,
                            latency_ms,
                            "Ship still failing after {} attempts",
                            count
                        );
                    } else {
                        tracing::warn!(
                            path = %item.path_str,
                            provider = %item.provider,
                            error_kind = error_kind.unwrap_or("unknown"),
                            error = %error,
                            latency_ms,
                            ingest_url = %client.ingest_url(),
                            "Shipping attempt failed; queued range for retry"
                        );
                    }
                }
            }

            AttemptedShip::Transient {
                item,
                error,
                is_connect_error: matches!(result, ShipResult::ConnectError(_)),
                is_backpressure,
                retry_after: ship_result_retry_after(&result),
            }
        }
        ShipResult::PayloadTooLarge(body) => {
            record_flight_attempt(
                flight_recorder,
                &flight_record,
                http_finished_at_ms,
                latency_ms,
                join_latency_ms,
                outcome.as_str(),
                http_status,
                error_kind,
                "retryable_payload_too_large",
            );
            tracing::warn!(
                "Payload too large shipping {}: {}",
                item.path_str,
                truncate_http_body(&body)
            );
            if let Some(limiter) = limiter {
                limiter.observe_backpressure(Some(ARCHIVE_BACKPRESSURE_RETRY_DELAY));
            }
            AttemptedShip::PayloadTooLarge { item }
        }
        ShipResult::PayloadRejected(status_code, body) => {
            record_flight_attempt(
                flight_recorder,
                &flight_record,
                http_finished_at_ms,
                latency_ms,
                join_latency_ms,
                outcome.as_str(),
                http_status,
                error_kind,
                "dead_letter",
            );
            tracing::error!(
                "Payload rejected shipping {}: {} {}",
                item.path_str,
                status_code,
                truncate_http_body(&body)
            );
            AttemptedShip::PayloadRejected {
                item,
                status_code,
                body,
            }
        }
    }
}

fn nonnegative_delta_ms(start_ms: i64, end_ms: i64) -> Option<u64> {
    end_ms
        .checked_sub(start_ms)
        .and_then(|delta| u64::try_from(delta).ok())
}

fn stage_timings_for_trace(
    trace: Option<&ShipTraceContext>,
    http_send_started_at_ms: i64,
    http_finished_at_ms: i64,
    http_latency_ms: u64,
) -> Option<ShipStageTimings> {
    let trace = trace?;
    Some(ShipStageTimings {
        observed_at_ms: Some(trace.observed_at_ms),
        latest_observed_at_ms: trace.latest_observed_at_ms,
        http_send_started_at_ms: Some(http_send_started_at_ms),
        http_finished_at_ms: Some(http_finished_at_ms),
        observation_window_ms: trace
            .latest_observed_at_ms
            .and_then(|latest_ms| nonnegative_delta_ms(trace.observed_at_ms, latest_ms)),
        observation_to_enqueue_ms: nonnegative_delta_ms(trace.observed_at_ms, trace.enqueued_at_ms),
        observation_to_wake_ms: trace
            .wake_received_at_ms
            .and_then(|wake_ms| nonnegative_delta_ms(trace.observed_at_ms, wake_ms)),
        wake_to_enqueue_ms: trace
            .wake_received_at_ms
            .and_then(|wake_ms| nonnegative_delta_ms(wake_ms, trace.enqueued_at_ms)),
        enqueue_to_job_ms: nonnegative_delta_ms(trace.enqueued_at_ms, trace.job_started_at_ms),
        observed_to_job_ms: nonnegative_delta_ms(trace.observed_at_ms, trace.job_started_at_ms),
        prepare_ms: nonnegative_delta_ms(trace.prepare_started_at_ms, trace.prepare_finished_at_ms),
        job_to_http_ms: nonnegative_delta_ms(trace.job_started_at_ms, http_send_started_at_ms),
        observed_to_http_send_ms: nonnegative_delta_ms(
            trace.observed_at_ms,
            http_send_started_at_ms,
        ),
        http_latency_ms: Some(http_latency_ms),
        job_to_ack_ms: nonnegative_delta_ms(trace.job_started_at_ms, http_finished_at_ms),
        observed_to_ack_ms: nonnegative_delta_ms(trace.observed_at_ms, http_finished_at_ms),
    })
}

fn build_ship_trace_value(
    item: &ShipItem,
    trace: Option<&ShipTraceContext>,
    http_send_started_at_ms: i64,
) -> Value {
    let mut value = json!({
        "schema": "ship_trace.v1",
        "kind": "ship_attempt",
        "trace_id": format!("{}:{}:{}:{}", item.session_id, item.offset, item.new_offset, http_send_started_at_ms),
        "provider": &item.provider,
        "session_id": &item.session_id,
        "path": &item.path_str,
        "work_context": trace.map(|trace| trace.work_context).unwrap_or("untraced"),
        "observation_source": trace.map(|trace| trace.observation_source).unwrap_or("unknown"),
        "event_count": item.event_count,
        "offset": item.offset,
        "new_offset": item.new_offset,
        "range_bytes": item.new_offset.saturating_sub(item.offset),
        "http_send_started_at_ms": http_send_started_at_ms,
    });

    if let Some(trace) = trace {
        insert_json_field(&mut value, "wake_reason", json!(trace.wake_reason));
        insert_json_field(&mut value, "turn_id", json!(trace.turn_id));
        insert_json_field(&mut value, "session_id_hint", json!(trace.session_id_hint));
        insert_json_field(&mut value, "file_len_hint", json!(trace.file_len_hint));
        insert_json_field(&mut value, "observed_at_ms", json!(trace.observed_at_ms));
        insert_json_field(
            &mut value,
            "latest_observed_at_ms",
            json!(trace.latest_observed_at_ms),
        );
        insert_json_field(
            &mut value,
            "wake_received_at_ms",
            json!(trace.wake_received_at_ms),
        );
        insert_json_field(&mut value, "enqueued_at_ms", json!(trace.enqueued_at_ms));
        insert_json_field(
            &mut value,
            "job_started_at_ms",
            json!(trace.job_started_at_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_started_at_ms",
            json!(trace.prepare_started_at_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_finished_at_ms",
            json!(trace.prepare_finished_at_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_blocking_queue_wait_ms",
            json!(trace.prepare_blocking_queue_wait_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_open_db_ms",
            json!(trace.prepare_open_db_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_identity_ms",
            json!(trace.prepare_identity_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_cursor_ms",
            json!(trace.prepare_cursor_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_binding_wait_ms",
            json!(trace.prepare_binding_wait_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_parse_ms",
            json!(trace.prepare_parse_ms),
        );
        insert_json_field(
            &mut value,
            "prepare_batch_build_ms",
            json!(trace.prepare_batch_build_ms),
        );
        insert_json_field(
            &mut value,
            "observation_to_enqueue_ms",
            json!(trace.enqueued_at_ms.saturating_sub(trace.observed_at_ms)),
        );
        insert_json_field(
            &mut value,
            "observation_window_ms",
            json!(trace
                .latest_observed_at_ms
                .map(|latest_ms| latest_ms.saturating_sub(trace.observed_at_ms))),
        );
        insert_json_field(
            &mut value,
            "observation_to_wake_ms",
            json!(trace
                .wake_received_at_ms
                .map(|wake_ms| wake_ms.saturating_sub(trace.observed_at_ms))),
        );
        insert_json_field(
            &mut value,
            "wake_to_enqueue_ms",
            json!(trace
                .wake_received_at_ms
                .map(|wake_ms| trace.enqueued_at_ms.saturating_sub(wake_ms))),
        );
        insert_json_field(
            &mut value,
            "enqueue_to_job_ms",
            json!(trace.job_started_at_ms.saturating_sub(trace.enqueued_at_ms)),
        );
        insert_json_field(
            &mut value,
            "observed_to_job_ms",
            json!(trace.job_started_at_ms.saturating_sub(trace.observed_at_ms)),
        );
        insert_json_field(
            &mut value,
            "prepare_ms",
            json!(trace
                .prepare_finished_at_ms
                .saturating_sub(trace.prepare_started_at_ms)),
        );
        insert_json_field(
            &mut value,
            "job_to_http_ms",
            json!(http_send_started_at_ms.saturating_sub(trace.job_started_at_ms)),
        );
    }

    value
}

fn record_flight_attempt(
    recorder: Option<&FlightRecorder>,
    base_record: &Value,
    http_finished_at_ms: i64,
    http_latency_ms: u64,
    http_join_latency_ms: u64,
    outcome: &str,
    http_status: Option<u16>,
    error_kind: Option<&str>,
    retry_decision: &str,
) {
    let Some(recorder) = recorder else {
        return;
    };
    let mut value = base_record.clone();
    insert_json_field(
        &mut value,
        "http_finished_at_ms",
        json!(http_finished_at_ms),
    );
    insert_json_field(&mut value, "http_latency_ms", json!(http_latency_ms));
    insert_json_field(
        &mut value,
        "http_join_elapsed_ms",
        json!(http_join_latency_ms),
    );
    insert_json_field(&mut value, "outcome", json!(outcome));
    insert_json_field(&mut value, "http_status", json!(http_status));
    insert_json_field(&mut value, "error_kind", json!(error_kind));
    insert_json_field(&mut value, "retry_decision", json!(retry_decision));
    recorder.record(value);
}

fn insert_json_field(value: &mut Value, key: &str, field_value: Value) {
    if let Value::Object(map) = value {
        map.insert(key.to_string(), field_value);
    }
}

fn remove_json_field(value: &mut Value, key: &str) {
    if let Value::Object(map) = value {
        map.remove(key);
    }
}

fn classify_ship_attempt_result(result: &ShipResult) -> (ShipAttemptOutcome, Option<u16>) {
    match result {
        ShipResult::Ok { .. } => (ShipAttemptOutcome::Ok, None),
        ShipResult::RateLimited => (ShipAttemptOutcome::RateLimited, Some(429)),
        ShipResult::ServerError(code, _) => (ShipAttemptOutcome::ServerError, Some(*code)),
        ShipResult::ServerBackpressure(detail) => {
            (ShipAttemptOutcome::ServerError, Some(detail.status_code))
        }
        ShipResult::PayloadRejected(code, _) => (ShipAttemptOutcome::PayloadRejected, Some(*code)),
        ShipResult::PayloadTooLarge(_) => (ShipAttemptOutcome::PayloadTooLarge, Some(413)),
        ShipResult::RetryableClientError(code, _) => {
            (ShipAttemptOutcome::RetryableClientError, Some(*code))
        }
        ShipResult::ConnectError(_) => (ShipAttemptOutcome::ConnectError, None),
    }
}

fn transient_error_kind(result: &ShipResult) -> Option<&'static str> {
    match result {
        ShipResult::Ok { .. } => None,
        ShipResult::RateLimited => Some("rate_limited"),
        ShipResult::ServerError(_, _) => Some("server_response"),
        ShipResult::ServerBackpressure(detail) => Some(detail.kind),
        ShipResult::PayloadRejected(_, _) => Some("payload_rejected"),
        ShipResult::PayloadTooLarge(_) => Some("payload_too_large"),
        ShipResult::RetryableClientError(401 | 403, _) => Some("auth"),
        ShipResult::RetryableClientError(_, _) => Some("client_response"),
        ShipResult::ConnectError(detail) => Some(detail.kind),
    }
}

fn transient_error_message(result: &ShipResult) -> String {
    match result {
        ShipResult::Ok { .. } => "ok".to_string(),
        ShipResult::RateLimited => "rate limited".to_string(),
        ShipResult::ServerError(code, body) => format!("{}:{}", code, truncate_http_body(body)),
        ShipResult::ServerBackpressure(detail) => {
            format!(
                "{}:{}:{}",
                detail.status_code,
                detail.kind,
                truncate_http_body(&detail.body)
            )
        }
        ShipResult::PayloadRejected(code, body) => format!("{}:{}", code, truncate_http_body(body)),
        ShipResult::PayloadTooLarge(body) => format!("413:{}", truncate_http_body(body)),
        ShipResult::RetryableClientError(code, body) => {
            format!("{}:{}", code, truncate_http_body(body))
        }
        ShipResult::ConnectError(detail) => detail.message.clone(),
    }
}

fn ship_result_is_backpressure(result: &ShipResult) -> bool {
    match result {
        ShipResult::RateLimited => true,
        ShipResult::ServerBackpressure(_) => true,
        ShipResult::ServerError(503, body) => body.contains("Archive ingest backlog is throttled"),
        _ => false,
    }
}

fn ship_result_retry_after(result: &ShipResult) -> Option<Duration> {
    match result {
        ShipResult::ServerBackpressure(detail) => detail
            .retry_after_seconds
            .filter(|value| value.is_finite() && *value > 0.0)
            .map(Duration::from_secs_f64),
        _ => None,
    }
}

pub async fn ship_prepared_file(
    prepared: PreparedFile,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
) -> Result<ShipPreparedOutcome> {
    ship_prepared_file_with_trace(
        prepared, client, conn, tracker, ship_stats, None, None, None,
    )
    .await
}

pub async fn ship_prepared_file_with_trace(
    prepared: PreparedFile,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
    ship_trace: Option<&ShipTraceContext>,
    flight_recorder: Option<&FlightRecorder>,
    limiter: Option<&crate::scheduler::AdaptiveLimiter>,
) -> Result<ShipPreparedOutcome> {
    let file_state = FileState::new(conn);
    let live_file_state = LiveFileState::new(conn);
    let spool = Spool::new(conn);
    let cursor_mode = prepared.cursor_mode;
    let prepared_new_offset = prepared.new_offset;
    let mut outcome = ShipPreparedOutcome::default();

    for action in prepared.actions {
        match action {
            PreparedAction::DeadLetter(item) => {
                tracing::error!(
                    "Dead-lettering {} range {}..{}: {}",
                    item.path_str,
                    item.offset,
                    item.new_offset,
                    item.reason
                );
                spool.record_dead(
                    &item.provider,
                    &item.path_str,
                    item.offset,
                    item.new_offset,
                    Some(&item.session_id),
                    &item.reason,
                )?;
                match cursor_mode {
                    CursorMode::Archive => file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.session_id,
                        &item.session_id,
                        &item.provider,
                    )?,
                    CursorMode::Live => live_file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.provider,
                        &item.session_id,
                    )?,
                }
                outcome.dead_lettered += 1;
            }
            PreparedAction::AckOnly(item) => {
                tracing::debug!(
                    path = %item.path_str,
                    provider = %item.provider,
                    offset = item.offset,
                    new_offset = item.new_offset,
                    "Acknowledging ignorable whole-document session without shipping"
                );
                match cursor_mode {
                    CursorMode::Archive => file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.session_id,
                        &item.session_id,
                        &item.provider,
                    )?,
                    CursorMode::Live => live_file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.provider,
                        &item.session_id,
                    )?,
                }
            }
            PreparedAction::Ship(item) => {
                match attempt_ship(
                    item,
                    client,
                    tracker,
                    ship_stats,
                    ship_trace,
                    ship_lane_for_context(ship_trace, cursor_mode),
                    flight_recorder,
                    limiter,
                )
                .await
                {
                    AttemptedShip::Reconciled(item) => {
                        match cursor_mode {
                            CursorMode::Archive => file_state.set_offset(
                                &item.path_str,
                                item.new_offset,
                                &item.session_id,
                                &item.session_id,
                                &item.provider,
                            )?,
                            CursorMode::Live => live_file_state.set_offset(
                                &item.path_str,
                                item.new_offset,
                                &item.provider,
                                &item.session_id,
                            )?,
                        }
                        outcome.bytes_shipped += item.new_offset - item.offset;
                    }
                    AttemptedShip::Shipped(item) => {
                        match cursor_mode {
                            CursorMode::Archive => file_state.set_offset(
                                &item.path_str,
                                item.new_offset,
                                &item.session_id,
                                &item.session_id,
                                &item.provider,
                            )?,
                            CursorMode::Live => live_file_state.set_offset(
                                &item.path_str,
                                item.new_offset,
                                &item.provider,
                                &item.session_id,
                            )?,
                        }
                        outcome.events_shipped += item.event_count;
                        outcome.bytes_shipped += item.new_offset - item.offset;
                    }
                    AttemptedShip::Transient {
                        item,
                        error: _,
                        is_connect_error,
                        is_backpressure: _,
                        retry_after: _,
                    } => {
                        if cursor_mode == CursorMode::Live {
                            outcome.fully_processed = false;
                            outcome.had_connect_error = is_connect_error;
                            return Ok(outcome);
                        }
                        let enqueued = spool.enqueue(
                            &item.provider,
                            &item.path_str,
                            item.offset,
                            prepared_new_offset,
                            Some(&item.session_id),
                        )?;
                        if enqueued {
                            file_state.set_queued_offset(
                                &item.path_str,
                                prepared_new_offset,
                                &item.provider,
                                &item.session_id,
                                &item.session_id,
                            )?;
                        } else {
                            tracing::warn!(
                                "Spool at capacity — {} will be retried on next startup",
                                item.path_str
                            );
                        }
                        outcome.fully_processed = false;
                        outcome.had_connect_error = is_connect_error;
                        return Ok(outcome);
                    }
                    AttemptedShip::PayloadTooLarge { item } => {
                        if cursor_mode == CursorMode::Live {
                            outcome.fully_processed = false;
                            return Ok(outcome);
                        }
                        let enqueued = spool.enqueue(
                            &item.provider,
                            &item.path_str,
                            item.offset,
                            prepared_new_offset,
                            Some(&item.session_id),
                        )?;
                        if enqueued {
                            file_state.set_queued_offset(
                                &item.path_str,
                                prepared_new_offset,
                                &item.provider,
                                &item.session_id,
                                &item.session_id,
                            )?;
                        } else {
                            tracing::warn!(
                                "Spool at capacity — 413 payload for {} will be retried on next startup",
                                item.path_str
                            );
                        }
                        outcome.fully_processed = false;
                        return Ok(outcome);
                    }
                    AttemptedShip::PayloadRejected {
                        item,
                        status_code,
                        body,
                    } => {
                        if cursor_mode == CursorMode::Live {
                            outcome.fully_processed = false;
                            return Ok(outcome);
                        }
                        let error = format!(
                            "payload rejected {}:{}",
                            status_code,
                            truncate_http_body(&body)
                        );
                        spool.record_dead(
                            &item.provider,
                            &item.path_str,
                            item.offset,
                            item.new_offset,
                            Some(&item.session_id),
                            &error,
                        )?;
                        match cursor_mode {
                            CursorMode::Archive => file_state.set_offset(
                                &item.path_str,
                                item.new_offset,
                                &item.session_id,
                                &item.session_id,
                                &item.provider,
                            )?,
                            CursorMode::Live => live_file_state.set_offset(
                                &item.path_str,
                                item.new_offset,
                                &item.provider,
                                &item.session_id,
                            )?,
                        }
                        outcome.dead_lettered += 1;
                    }
                    AttemptedShip::MediaUploadFailed { item, error } => {
                        tracing::warn!(
                            path = %item.path_str,
                            provider = %item.provider,
                            session_id = %item.session_id,
                            error = %error,
                            "Archive media upload failed; leaving cursor unchanged for reparse"
                        );
                        outcome.fully_processed = false;
                        return Ok(outcome);
                    }
                }
            }
        }
    }

    Ok(outcome)
}

/// Startup recovery: find files where queued_offset > acked_offset
/// and re-enqueue their gaps into the spool.
pub fn run_startup_recovery(conn: &Connection) -> Result<usize> {
    let file_state = FileState::new(conn);
    let spool = Spool::new(conn);
    let unacked = file_state.get_unacked_files()?;
    let pending_before = spool.pending_count()?;

    for f in &unacked {
        tracing::info!(
            "Recovering gap for {}: acked={}, queued={}",
            f.path,
            f.acked_offset,
            f.queued_offset
        );
        spool.enqueue(
            &f.provider,
            &f.path,
            f.acked_offset,
            f.queued_offset,
            f.session_id.as_deref(),
        )?;
    }

    let pending_after = spool.pending_count()?;
    Ok(pending_after.saturating_sub(pending_before))
}

pub(crate) fn recover_gap_for_path(
    conn: &Connection,
    file_path: &Path,
) -> Result<GapRecoveryOutcome> {
    let file_state = FileState::new(conn);
    let spool = Spool::new(conn);
    let target_path = file_path.to_string_lossy().to_string();

    let Some(target) = file_state
        .get_unacked_files()?
        .into_iter()
        .find(|tracked| tracked.path == target_path)
    else {
        return Ok(GapRecoveryOutcome {
            had_gap: false,
            replay_ready: false,
        });
    };

    tracing::info!(
        path = %target.path,
        acked_offset = target.acked_offset,
        queued_offset = target.queued_offset,
        "Recovering queued gap for explicit ship --file request"
    );
    let replay_ready = spool.enqueue(
        &target.provider,
        &target.path,
        target.acked_offset,
        target.queued_offset,
        target.session_id.as_deref(),
    )?;
    Ok(GapRecoveryOutcome {
        had_gap: true,
        replay_ready,
    })
}

/// Replay pending spool entries. Returns (entries fully resolved, entries failed/backed off).
#[cfg(test)]
pub async fn replay_spool_batch(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    limit: usize,
) -> Result<(usize, usize)> {
    replay_spool_batch_with_batch_bytes(conn, client, algo, limit, u64::MAX).await
}

pub async fn replay_spool_batch_with_batch_bytes(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    limit: usize,
    max_batch_bytes: u64,
) -> Result<(usize, usize)> {
    replay_spool_batch_with_batch_bytes_and_parse_tracker(
        conn,
        client,
        algo,
        limit,
        max_batch_bytes,
        None,
        None,
    )
    .await
}

pub(crate) async fn replay_spool_batch_with_batch_bytes_and_parse_tracker(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    limit: usize,
    max_batch_bytes: u64,
    parse_tracker: Option<&RecentIssueTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
) -> Result<(usize, usize)> {
    let spool = Spool::new(conn);
    let pending = spool.dequeue_batch(limit)?;
    let outcome = replay_spool_entries(
        conn,
        client,
        algo,
        &pending,
        max_batch_bytes,
        parse_tracker,
        ship_stats,
        None,
        None,
        None,
    )
    .await?;

    // Cleanup old dead entries
    let cleaned = spool.cleanup()?;
    if cleaned > 0 {
        tracing::info!("Cleaned {} old spool entries", cleaned);
    }

    Ok((outcome.resolved, outcome.failed))
}

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
pub(crate) async fn replay_spool_for_path_with_batch_bytes_and_parse_tracker(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    file_path: &Path,
    limit: usize,
    max_batch_bytes: u64,
    parse_tracker: Option<&RecentIssueTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
    flight_recorder: Option<&FlightRecorder>,
    ship_trace: Option<&ShipTraceContext>,
    limiter: Option<&crate::scheduler::AdaptiveLimiter>,
) -> Result<ReplaySpoolOutcome> {
    let spool = Spool::new(conn);
    let pending = spool.pending_entries_for_path(&file_path.to_string_lossy(), limit)?;
    replay_spool_entries(
        conn,
        client,
        algo,
        &pending,
        max_batch_bytes,
        parse_tracker,
        ship_stats,
        flight_recorder,
        ship_trace,
        limiter,
    )
    .await
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn replay_ready_spool_for_path_with_batch_bytes_and_parse_tracker(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    file_path: &Path,
    limit: usize,
    max_batch_bytes: u64,
    parse_tracker: Option<&RecentIssueTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
    flight_recorder: Option<&FlightRecorder>,
    ship_trace: Option<&ShipTraceContext>,
    limiter: Option<&crate::scheduler::AdaptiveLimiter>,
) -> Result<ReplaySpoolOutcome> {
    let spool = Spool::new(conn);
    if limit > 1 {
        let merged =
            spool.coalesce_ready_adjacent_for_path(&file_path.to_string_lossy(), limit * 4)?;
        if merged > 0 {
            tracing::debug!(
                path = %file_path.display(),
                merged,
                "Coalesced adjacent ready archive ranges before replay"
            );
        }
    }
    let pending = spool.pending_entries_for_path_ready(&file_path.to_string_lossy(), limit)?;
    replay_spool_entries(
        conn,
        client,
        algo,
        &pending,
        max_batch_bytes,
        parse_tracker,
        ship_stats,
        flight_recorder,
        ship_trace,
        limiter,
    )
    .await
}

pub(crate) async fn replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    file_path: &Path,
    limit: usize,
    max_batch_bytes: u64,
    parse_tracker: Option<&RecentIssueTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
) -> Result<ReplaySpoolOutcome> {
    let spool = Spool::new(conn);
    let pending = spool.pending_entries_for_path_now(&file_path.to_string_lossy(), limit)?;
    replay_spool_entries(
        conn,
        client,
        algo,
        &pending,
        max_batch_bytes,
        parse_tracker,
        ship_stats,
        None,
        None,
        None,
    )
    .await
}

async fn prepare_spool_entry_for_replay(
    entry: crate::state::spool::SpoolEntry,
    path: PathBuf,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    archive_target_batch_bytes: Option<u64>,
    parse_tracker: Option<&RecentIssueTracker>,
) -> Result<Option<PreparedFile>> {
    let parse_tracker = parse_tracker.cloned();
    let provider = entry.provider.clone();
    let blocking_span =
        tracing::info_span!("engine.spool.prepare.blocking", longhouse.provider = %provider);
    task::spawn_blocking(move || {
        let _enter = blocking_span.enter();
        prepare_path_range_with_parse_tracker(
            &path,
            &entry.provider,
            entry.start_offset,
            Some(entry.end_offset),
            algo,
            max_batch_bytes,
            entry.session_id.as_deref(),
            parse_tracker.as_ref(),
            None,
            SourceLineMode::Full,
            BatchBand::Archive,
            archive_target_batch_bytes,
        )
    })
    .await?
}

#[tracing::instrument(
    level = "info",
    name = "engine.spool.replay",
    skip(conn, client, pending, parse_tracker, ship_stats, flight_recorder, ship_trace, limiter),
    fields(longhouse.spool.pending_entries = pending.len() as u64)
)]
#[allow(clippy::too_many_arguments)]
async fn replay_spool_entries(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    pending: &[crate::state::spool::SpoolEntry],
    max_batch_bytes: u64,
    parse_tracker: Option<&RecentIssueTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
    flight_recorder: Option<&FlightRecorder>,
    ship_trace: Option<&ShipTraceContext>,
    limiter: Option<&crate::scheduler::AdaptiveLimiter>,
) -> Result<ReplaySpoolOutcome> {
    let spool = Spool::new(conn);
    let file_state = FileState::new(conn);
    let mut outcome = ReplaySpoolOutcome::default();

    'entry_loop: for entry in pending {
        let path = PathBuf::from(&entry.file_path);
        let metadata = match std::fs::metadata(&path) {
            Ok(metadata) => metadata,
            Err(_) => {
                tracing::warn!("Spool file missing: {}", entry.file_path);
                spool.mark_failed_with_max(entry.id, "file missing", 0)?;
                outcome.failed += 1;
                continue;
            }
        };
        let current_identity = identity_from_metadata(&metadata);
        let stored_identity = file_state.get_file_identity(&entry.file_path)?;
        if file_identity_changed_for_cursor(
            stored_identity.as_deref(),
            current_identity.as_deref(),
            entry.start_offset,
            entry.end_offset,
        ) {
            tracing::warn!(
                path = %entry.file_path,
                stored_identity = ?stored_identity,
                current_identity = ?current_identity,
                "Stale spool pointer source was replaced; retiring pending ranges for path"
            );
            let retired = spool.dead_letter_pending_for_path(
                &entry.file_path,
                "source file identity changed before replay; stale pointer retired",
            )?;
            file_state.reset_offsets(&entry.file_path)?;
            outcome.failed += retired.max(1);
            continue;
        }

        let archive_target_batch_bytes =
            limiter.map(crate::scheduler::AdaptiveLimiter::archive_target_batch_bytes);
        let prepared = match prepare_spool_entry_for_replay(
            entry.clone(),
            path,
            algo,
            max_batch_bytes,
            archive_target_batch_bytes,
            parse_tracker,
        )
        .await
        {
            Ok(Some(prepared)) => prepared,
            Ok(None) => {
                if entry.start_offset >= entry.end_offset {
                    spool.mark_shipped(entry.id)?;
                    outcome.resolved += 1;
                } else {
                    spool.mark_failed(entry.id, "no complete lines ready for replay")?;
                    outcome.failed += 1;
                }
                continue;
            }
            Err(e) => {
                spool.mark_failed(entry.id, &e.to_string())?;
                outcome.failed += 1;
                continue;
            }
        };

        let mut entry_done = false;
        for action in prepared.actions {
            match action {
                PreparedAction::DeadLetter(item) => {
                    tracing::error!(
                        "Dead-lettering replay range {} {}..{}: {}",
                        item.path_str,
                        item.offset,
                        item.new_offset,
                        item.reason
                    );
                    spool.record_dead(
                        &item.provider,
                        &item.path_str,
                        item.offset,
                        item.new_offset,
                        Some(&item.session_id),
                        &item.reason,
                    )?;
                    file_state.set_acked_offset(&entry.file_path, item.new_offset)?;
                    if item.new_offset >= entry.end_offset {
                        spool.mark_shipped(entry.id)?;
                        entry_done = true;
                    } else {
                        spool.advance_start(entry.id, item.new_offset)?;
                    }
                }
                PreparedAction::AckOnly(item) => {
                    file_state.set_acked_offset(&entry.file_path, item.new_offset)?;
                    if item.new_offset >= entry.end_offset {
                        spool.mark_shipped(entry.id)?;
                        entry_done = true;
                    } else {
                        spool.advance_start(entry.id, item.new_offset)?;
                    }
                }
                PreparedAction::Ship(item) => {
                    match attempt_ship(
                        item,
                        client,
                        None,
                        ship_stats,
                        ship_trace,
                        ship_lane_for_context(ship_trace, CursorMode::Archive),
                        flight_recorder,
                        limiter,
                    )
                    .await
                    {
                        AttemptedShip::Reconciled(item) => {
                            file_state.set_acked_offset(&entry.file_path, item.new_offset)?;
                            if item.new_offset >= entry.end_offset {
                                spool.mark_shipped(entry.id)?;
                                entry_done = true;
                            } else {
                                spool.advance_start(entry.id, item.new_offset)?;
                            }
                        }
                        AttemptedShip::Shipped(item) => {
                            outcome.events_shipped += item.event_count;
                            file_state.set_acked_offset(&entry.file_path, item.new_offset)?;
                            if item.new_offset >= entry.end_offset {
                                spool.mark_shipped(entry.id)?;
                                entry_done = true;
                            } else {
                                spool.advance_start(entry.id, item.new_offset)?;
                            }
                        }
                        AttemptedShip::Transient {
                            item: _,
                            error,
                            is_connect_error,
                            is_backpressure,
                            retry_after,
                        } => {
                            if is_backpressure {
                                let retry_delay =
                                    retry_after.unwrap_or(ARCHIVE_BACKPRESSURE_RETRY_DELAY);
                                let deferred = spool.defer_pending_for_path(
                                    &entry.file_path,
                                    &error,
                                    retry_delay,
                                )?;
                                tracing::info!(
                                    path = %entry.file_path,
                                    deferred,
                                    retry_after_ms = retry_delay.as_millis() as u64,
                                    "Deferred spool path after runtime backpressure"
                                );
                                outcome.failed += 1;
                                break 'entry_loop;
                            }
                            spool.mark_failed(entry.id, &error)?;
                            outcome.failed += 1;
                            if is_connect_error {
                                outcome.had_connect_error = true;
                                break 'entry_loop;
                            }
                            if crate::state::spool::is_recoverable_archive_error(&error) {
                                break 'entry_loop;
                            }
                            continue 'entry_loop;
                        }
                        AttemptedShip::PayloadTooLarge { item } => {
                            if let Some(split_offset) =
                                replay_split_offset_for_payload_too_large(&item)
                            {
                                let mut ranges = vec![
                                    (item.offset, split_offset),
                                    (split_offset, item.new_offset),
                                ];
                                if item.new_offset < entry.end_offset {
                                    ranges.push((item.new_offset, entry.end_offset));
                                }
                                let written = spool.replace_pending_entry_with_ranges(
                                    entry.id,
                                    &item.provider,
                                    &item.path_str,
                                    Some(&item.session_id),
                                    &ranges,
                                    "413 payload too large during replay; split range for immediate retry",
                                )?;
                                if written > 0 {
                                    tracing::info!(
                                        path = %item.path_str,
                                        offset = item.offset,
                                        split_offset,
                                        new_offset = item.new_offset,
                                        tail_end_offset = entry.end_offset,
                                        child_ranges = written,
                                        "Split archive replay range after 413 payload rejection"
                                    );
                                    outcome.failed += 1;
                                    continue 'entry_loop;
                                }
                            }
                            spool.mark_failed_with_max(
                                entry.id,
                                "413 payload too large during replay",
                                u32::MAX,
                            )?;
                            outcome.failed += 1;
                            continue 'entry_loop;
                        }
                        AttemptedShip::PayloadRejected {
                            item,
                            status_code,
                            body,
                        } => {
                            let error = format!(
                                "payload rejected {}:{}",
                                status_code,
                                truncate_http_body(&body)
                            );
                            spool.record_dead(
                                &item.provider,
                                &item.path_str,
                                item.offset,
                                item.new_offset,
                                Some(&item.session_id),
                                &error,
                            )?;
                            file_state.set_acked_offset(&entry.file_path, item.new_offset)?;
                            if item.new_offset >= entry.end_offset {
                                spool.mark_shipped(entry.id)?;
                                entry_done = true;
                            } else {
                                spool.advance_start(entry.id, item.new_offset)?;
                            }
                        }
                        AttemptedShip::MediaUploadFailed { item, error } => {
                            let deferred = spool.defer_pending_for_path(
                                &entry.file_path,
                                &error,
                                ARCHIVE_BACKPRESSURE_RETRY_DELAY,
                            )?;
                            tracing::warn!(
                                path = %item.path_str,
                                provider = %item.provider,
                                session_id = %item.session_id,
                                deferred,
                                error = %error,
                                "Deferred spool path after archive media upload failure"
                            );
                            outcome.failed += 1;
                            break 'entry_loop;
                        }
                    }
                }
            }

            if entry_done {
                break;
            }
        }

        if entry_done {
            outcome.resolved += 1;
        }
    }

    Ok(outcome)
}

/// Run a full scan: discover all provider files, prepare and ship any with new content.
/// Returns (files_shipped, events_shipped).
#[cfg(test)]
#[allow(dead_code)]
pub async fn full_scan(
    providers: &[ProviderConfig],
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<(usize, usize)> {
    full_scan_with_batch_bytes(providers, conn, client, algo, u64::MAX, tracker).await
}

#[allow(dead_code)]
pub async fn full_scan_with_batch_bytes(
    providers: &[ProviderConfig],
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<(usize, usize)> {
    full_scan_with_batch_bytes_and_parse_tracker(
        providers,
        conn,
        client,
        algo,
        max_batch_bytes,
        tracker,
        None,
    )
    .await
}

pub(crate) async fn full_scan_with_batch_bytes_and_parse_tracker(
    providers: &[ProviderConfig],
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    tracker: Option<&ConsecutiveErrorTracker>,
    parse_tracker: Option<&RecentIssueTracker>,
) -> Result<(usize, usize)> {
    let all_files = discovery::discover_all_files(providers);
    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;

    for (path, provider_name) in &all_files {
        let file_start = std::time::Instant::now();
        if *provider_name == "opencode" && opencode_db::is_opencode_database_path(path) {
            match ship_opencode_database(
                path,
                conn,
                client,
                algo,
                max_batch_bytes,
                tracker,
                parse_tracker,
            )
            .await
            {
                Ok((sessions, events)) => {
                    if sessions > 0 {
                        files_shipped += sessions;
                        events_shipped += events;
                        log_slow_file_processing(
                            "opencode_sqlite_scan",
                            path,
                            provider_name,
                            events,
                            0,
                            0,
                            file_start.elapsed(),
                        );
                    }
                }
                Err(error) => {
                    tracing::warn!(
                        "Error preparing OpenCode database {}: {}",
                        path.display(),
                        error
                    );
                }
            }
            continue;
        }
        match prepare_file_batches_with_source_line_mode_and_parse_tracker(
            path,
            provider_name,
            algo,
            conn,
            max_batch_bytes,
            None,
            parse_tracker,
            SourceLineMode::Full,
            BatchBand::BackgroundRepair,
        ) {
            Ok(Some(prepared)) => {
                let event_count = prepared.total_event_count();
                let byte_count = prepared.new_offset.saturating_sub(prepared.offset);
                let outcome = ship_prepared_file(prepared, client, conn, tracker, None).await?;
                log_slow_file_processing(
                    "reconciliation_scan",
                    path,
                    provider_name,
                    event_count,
                    byte_count,
                    outcome.dead_lettered,
                    file_start.elapsed(),
                );
                if outcome.events_shipped > 0 || outcome.dead_lettered > 0 {
                    files_shipped += 1;
                    events_shipped += outcome.events_shipped;

                    if files_shipped % 100 == 0 {
                        tracing::info!(
                            "Full scan progress: {} files, {} events shipped",
                            files_shipped,
                            events_shipped
                        );
                    }
                }
            }
            Ok(None) => {} // no new content
            Err(e) => {
                tracing::warn!("Error preparing {}: {}", path.display(), e);
            }
        }
    }

    Ok((files_shipped, events_shipped))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::ShipperConfig;
    use crate::state::db::open_db;
    use base64::{engine::general_purpose, Engine as _};
    use flate2::read::GzDecoder;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    static OPENCODE_STATE_ROOT_ENV_LOCK: Mutex<()> = Mutex::new(());

    fn make_db() -> (tempfile::NamedTempFile, Connection) {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();
        (tmp, conn)
    }

    struct EnvVarGuard {
        key: &'static str,
        previous: Option<std::ffi::OsString>,
    }

    impl EnvVarGuard {
        fn set(key: &'static str, value: &std::ffi::OsStr) -> Self {
            let previous = std::env::var_os(key);
            std::env::set_var(key, value);
            Self { key, previous }
        }

        fn remove(key: &'static str) -> Self {
            let previous = std::env::var_os(key);
            std::env::remove_var(key);
            Self { key, previous }
        }
    }

    impl Drop for EnvVarGuard {
        fn drop(&mut self) {
            if let Some(previous) = self.previous.as_ref() {
                std::env::set_var(self.key, previous);
            } else {
                std::env::remove_var(self.key);
            }
        }
    }

    fn claude_session_lines() -> &'static str {
        concat!(
            r#"{"type":"user","uuid":"11111111-1111-1111-1111-111111111111","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
            "\n",
            r#"{"type":"assistant","uuid":"22222222-2222-2222-2222-222222222222","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi there"}]}}"#,
            "\n",
        )
    }

    fn codex_session_lines() -> &'static str {
        concat!(
            r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#,
            "\n",
            r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from codex"}]}}"#,
            "\n",
            r#"{"type":"response_item","timestamp":"2026-02-15T10:00:02Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hi from codex"}]}}"#,
            "\n",
        )
    }

    fn codex_forked_session_lines() -> &'static str {
        concat!(
            r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"dddddddd-1111-2222-3333-444455556666","forked_from_id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#,
            "\n",
            r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from forked codex child"}]}}"#,
            "\n",
        )
    }

    fn codex_source_subagent_session_lines() -> &'static str {
        concat!(
            r#"{"type":"session_meta","timestamp":"2026-04-29T19:48:36Z","payload":{"type":"session_meta","id":"019ddb6e-114f-7643-89db-86c31a2aa706","source":{"subagent":{"thread_spawn":{"parent_thread_id":"019dd708-573a-7131-a4d9-9ee855520483","depth":1,"agent_nickname":"Ptolemy","agent_role":"default"}}},"cwd":"/tmp/test","cli_version":"0.125.0"}}"#,
            "\n",
            r#"{"type":"response_item","timestamp":"2026-04-29T19:48:37Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from source subagent"}]}}"#,
            "\n",
        )
    }

    fn codex_review_subagent_session_lines() -> &'static str {
        concat!(
            r#"{"type":"session_meta","timestamp":"2026-04-29T19:48:36Z","payload":{"type":"session_meta","id":"019ddb6e-114f-7643-89db-86c31a2aa706","source":{"subagent":{"review":{}}},"cwd":"/tmp/test","cli_version":"0.125.0"}}"#,
            "\n",
            r#"{"type":"response_item","timestamp":"2026-04-29T19:48:37Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from review subagent"}]}}"#,
            "\n",
        )
    }

    /// Write content to a temp file with a UUID-based name.
    fn write_session_file(content: &str, name: &str) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(name);
        std::fs::write(&path, content).unwrap();
        dir
    }

    fn spawn_http_response_server(
        status_line: &str,
        body: &str,
    ) -> (String, std::thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let status_line = status_line.to_string();
        let body = body.to_string();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0_u8; 8192];
            let _ = stream.read(&mut buf);
            let response = format!(
                "HTTP/1.1 {}\r\nContent-Length: {}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
                status_line,
                body.len(),
                body,
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        (format!("http://{}", addr), handle)
    }

    fn find_header_end(buf: &[u8]) -> Option<usize> {
        buf.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn parse_content_length(headers: &[u8]) -> usize {
        let header_text = String::from_utf8_lossy(headers);
        header_text
            .lines()
            .find_map(|line| {
                let mut parts = line.splitn(2, ':');
                let name = parts.next()?.trim();
                let value = parts.next()?.trim();
                if name.eq_ignore_ascii_case("content-length") {
                    value.parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0)
    }

    fn expects_continue(headers: &[u8]) -> bool {
        String::from_utf8_lossy(headers)
            .lines()
            .any(|line| line.eq_ignore_ascii_case("expect: 100-continue"))
    }

    fn read_request_body(stream: &mut std::net::TcpStream) -> Vec<u8> {
        let mut buf = Vec::new();
        let mut chunk = [0_u8; 4096];

        loop {
            let n = stream.read(&mut chunk).unwrap();
            if n == 0 {
                return Vec::new();
            }
            buf.extend_from_slice(&chunk[..n]);
            if let Some(header_end) = find_header_end(&buf) {
                let body_start = header_end + 4;
                let content_length = parse_content_length(&buf[..body_start]);
                if expects_continue(&buf[..body_start]) {
                    stream.write_all(b"HTTP/1.1 100 Continue\r\n\r\n").unwrap();
                }
                while buf.len() < body_start + content_length {
                    let n = stream.read(&mut chunk).unwrap();
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&chunk[..n]);
                }
                return buf[body_start..body_start + content_length].to_vec();
            }
        }
    }

    fn spawn_http_sequence_server(
        responses: &[(&str, &str)],
    ) -> (
        String,
        Arc<Mutex<Vec<Vec<u8>>>>,
        std::thread::JoinHandle<()>,
    ) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let responses: Vec<(String, String)> = responses
            .iter()
            .map(|(status, body)| ((*status).to_string(), (*body).to_string()))
            .collect();
        let captured = Arc::new(Mutex::new(Vec::new()));
        let captured_clone = Arc::clone(&captured);

        let handle = std::thread::spawn(move || {
            for (status_line, body) in responses {
                let (mut stream, _) = listener.accept().unwrap();
                let request_body = read_request_body(&mut stream);
                captured_clone.lock().unwrap().push(request_body);
                let response = format!(
                    "HTTP/1.1 {}\r\nContent-Length: {}\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}",
                    status_line,
                    body.len(),
                    body,
                );
                stream.write_all(response.as_bytes()).unwrap();
            }
        });

        (format!("http://{}", addr), captured, handle)
    }

    fn decode_payload_source_offsets(compressed: &[u8]) -> Vec<u64> {
        let mut decoder = GzDecoder::new(compressed);
        let mut json_str = String::new();
        decoder.read_to_string(&mut json_str).unwrap();
        let payload: serde_json::Value = serde_json::from_str(&json_str).unwrap();
        payload["source_lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|line| line["source_offset"].as_u64().unwrap())
            .collect()
    }

    fn decode_payload_session_id(compressed: &[u8]) -> String {
        let mut decoder = GzDecoder::new(compressed);
        let mut json_str = String::new();
        decoder.read_to_string(&mut json_str).unwrap();
        let payload: serde_json::Value = serde_json::from_str(&json_str).unwrap();
        payload["id"].as_str().unwrap().to_string()
    }

    fn decode_payload_rewind_hints(compressed: &[u8]) -> Vec<(String, u64, String)> {
        let mut decoder = GzDecoder::new(compressed);
        let mut json_str = String::new();
        decoder.read_to_string(&mut json_str).unwrap();
        let payload: serde_json::Value = serde_json::from_str(&json_str).unwrap();
        payload["rewind_hints"]
            .as_array()
            .map(|items| {
                items
                    .iter()
                    .map(|item| {
                        (
                            item["source_path"].as_str().unwrap().to_string(),
                            item["source_offset"].as_u64().unwrap(),
                            item["reason"].as_str().unwrap().to_string(),
                        )
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    fn make_line(uuid: &str, text: &str) -> String {
        format!(
            r#"{{"type":"user","uuid":"{}","timestamp":"2026-02-15T10:00:00Z","message":{{"content":"{}"}}}}"#,
            uuid, text
        )
    }

    fn make_test_client(url: &str) -> ShipperClient {
        let mut config = ShipperConfig::default();
        config.api_url = url.to_string();
        config.timeout_seconds = 5;
        ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap()
    }

    fn create_opencode_fixture_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE session (
                id text PRIMARY KEY,
                parent_id text,
                directory text,
                path text,
                title text,
                version text,
                time_created integer NOT NULL,
                time_updated integer NOT NULL
            );
            CREATE TABLE message (
                id text PRIMARY KEY,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );
            CREATE TABLE part (
                id text PRIMARY KEY,
                message_id text NOT NULL,
                session_id text NOT NULL,
                time_created integer NOT NULL,
                time_updated integer NOT NULL,
                data text NOT NULL
            );
            "#,
        )
        .unwrap();
        conn.execute(
            "INSERT INTO session (id, parent_id, directory, path, title, version, time_created, time_updated)
             VALUES (?1, NULL, ?2, ?3, ?4, ?5, ?6, ?7)",
            rusqlite::params![
                "ses_test",
                "/tmp/opencode-project",
                "tmp/opencode-project",
                "OpenCode fixture",
                "1.15.7",
                1_779_100_000_000_i64,
                1_779_100_000_300_i64,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                "msg_user",
                "ses_test",
                1_779_100_000_010_i64,
                1_779_100_000_010_i64,
                r#"{"role":"user"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                "prt_user",
                "msg_user",
                "ses_test",
                1_779_100_000_011_i64,
                1_779_100_000_011_i64,
                r#"{"type":"text","text":"hello from OpenCode"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO message (id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params![
                "msg_assistant",
                "ses_test",
                1_779_100_000_100_i64,
                1_779_100_000_300_i64,
                r#"{"role":"assistant"}"#,
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                "prt_assistant",
                "msg_assistant",
                "ses_test",
                1_779_100_000_200_i64,
                1_779_100_000_300_i64,
                r#"{"type":"text","text":"hi from OpenCode"}"#,
            ],
        )
        .unwrap();
    }

    fn insert_opencode_large_image_part(path: &Path, image_data: &str) {
        let conn = Connection::open(path).unwrap();
        conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![
                "prt_large_image",
                "msg_user",
                "ses_test",
                1_779_100_000_012_i64,
                1_779_100_000_012_i64,
                json!({
                    "type": "file",
                    "mime": "image/png",
                    "filename": "large-screenshot",
                    "url": format!("data:image/png;base64,{image_data}"),
                    "source": {
                        "type": "file",
                        "path": "large-screenshot",
                        "text": {"value": "[Image 1]", "start": 0, "end": 9}
                    }
                })
                .to_string(),
            ],
        )
        .unwrap();
    }

    fn decode_payload(compressed: &[u8]) -> serde_json::Value {
        let mut decoder = GzDecoder::new(compressed);
        let mut json_str = String::new();
        decoder.read_to_string(&mut json_str).unwrap();
        serde_json::from_str(&json_str).unwrap()
    }

    #[tokio::test]
    async fn test_ship_opencode_database_posts_changed_session_and_advances_cursor() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_opencode_fixture_db(&db_path);
        let (_state_file, conn) = make_db();
        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);

        let (sessions, events) = ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();

        assert_eq!(sessions, 1);
        assert_eq!(events, 2);
        handle.join().unwrap();
        let bodies = captured.lock().unwrap();
        assert_eq!(bodies.len(), 1);
        let payload = decode_payload(&bodies[0]);
        assert_eq!(payload["provider"], "opencode");
        assert_eq!(payload["provider_session_id"], "ses_test");
        assert_eq!(payload["events"][0]["role"], "user");
        assert_eq!(payload["events"][0]["content_text"], "hello from OpenCode");
        assert_eq!(payload["events"][1]["role"], "assistant");
        assert_eq!(payload["events"][1]["content_text"], "hi from OpenCode");
        assert!(payload["id"].as_str().unwrap().contains('-'));

        let source_key = opencode_db::opencode_source_key(&db_path, "ses_test");
        assert!(FileState::new(&conn).get_offset(&source_key).unwrap() > 0);

        let (skipped_sessions, skipped_events) = ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!((skipped_sessions, skipped_events), (0, 0));
    }

    #[tokio::test]
    async fn test_ship_opencode_database_uploads_large_inline_image_before_small_ingest() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_opencode_fixture_db(&db_path);
        let image_bytes = vec![17u8; 1024 * 1024];
        let image_data = general_purpose::STANDARD.encode(&image_bytes);
        let image_sha256 = format!("{:x}", Sha256::digest(&image_bytes));
        insert_opencode_large_image_part(&db_path, &image_data);
        let (_state_file, conn) = make_db();
        let claim_body = format!(
            r#"{{"needed":["{}"],"present":[],"rejected":[]}}"#,
            image_sha256
        );
        let (url, captured, handle) = spawn_http_sequence_server(&[
            ("200 OK", &claim_body),
            ("200 OK", "{}"),
            ("200 OK", "{}"),
        ]);
        let client = make_test_client(&url);

        let (sessions, events) = ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();

        assert_eq!(sessions, 1);
        assert_eq!(events, 3);
        handle.join().unwrap();
        let bodies = captured.lock().unwrap();
        assert_eq!(bodies.len(), 3);

        let claim: serde_json::Value = serde_json::from_slice(&bodies[0]).unwrap();
        assert_eq!(claim["items"][0]["sha256"], image_sha256);
        assert_eq!(claim["items"][0]["mime_type"], "image/png");
        assert_eq!(claim["items"][0]["byte_size"], image_bytes.len());
        assert_eq!(claim["items"][0]["provider"], "opencode");
        assert_eq!(bodies[1], image_bytes);

        let mut decoder = GzDecoder::new(&bodies[2][..]);
        let mut ingest_json = String::new();
        decoder.read_to_string(&mut ingest_json).unwrap();
        assert!(ingest_json.contains("longhouse_media_ref:sha256="));
        assert!(!ingest_json.contains(&image_data));
        assert!(
            ingest_json.len() < 8_000,
            "redacted OpenCode ingest body should stay small, got {} bytes",
            ingest_json.len()
        );
    }

    #[tokio::test]
    async fn test_ship_opencode_database_persists_managed_session_binding() {
        let _lock = OPENCODE_STATE_ROOT_ENV_LOCK.lock().unwrap();
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_opencode_fixture_db(&db_path);
        let state_root = temp.path().join("managed-state");
        std::fs::create_dir_all(&state_root).unwrap();
        let managed_session_id = "11111111-2222-4333-8444-555555555555";
        std::fs::write(
            state_root.join("managed.state.json"),
            serde_json::json!({
                "schema_version": 1,
                "provider": "opencode",
                "longhouse_session_id": managed_session_id,
                "opencode_session_id": "ses_test",
                "phase": "idle"
            })
            .to_string(),
        )
        .unwrap();
        let (_state_file, conn) = make_db();
        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);

        let env_guard = EnvVarGuard::set("LONGHOUSE_OPENCODE_STATE_ROOT", state_root.as_os_str());
        ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();
        drop(env_guard);
        handle.join().unwrap();
        let first_payload = decode_payload(&captured.lock().unwrap()[0]);
        assert_eq!(first_payload["id"], managed_session_id);

        std::fs::remove_dir_all(&state_root).unwrap();
        let _removed_env = EnvVarGuard::remove("LONGHOUSE_OPENCODE_STATE_ROOT");
        Connection::open(&db_path)
            .unwrap()
            .execute(
                "UPDATE part SET data = ?1 WHERE id = 'prt_assistant'",
                [r#"{"type":"text","text":"managed binding survived"}"#],
            )
            .unwrap();
        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);

        ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();

        handle.join().unwrap();
        let second_payload = decode_payload(&captured.lock().unwrap()[0]);
        assert_eq!(second_payload["id"], managed_session_id);
        assert_eq!(
            second_payload["events"][1]["content_text"],
            "managed binding survived"
        );
    }

    #[tokio::test]
    async fn test_ship_opencode_database_rejected_payload_leaves_cursor_unchanged() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_opencode_fixture_db(&db_path);
        let (_state_file, conn) = make_db();
        let client = make_test_client("http://127.0.0.1:9");

        let (sessions, events) = ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            1,
            None,
            None,
        )
        .await
        .unwrap();

        assert_eq!((sessions, events), (0, 0));
        let source_key = opencode_db::opencode_source_key(&db_path, "ses_test");
        assert_eq!(FileState::new(&conn).get_offset(&source_key).unwrap(), 0);
    }

    #[tokio::test]
    async fn test_ship_opencode_database_reships_same_timestamp_content_change() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("opencode.db");
        create_opencode_fixture_db(&db_path);
        let (_state_file, conn) = make_db();
        let (url, captured, handle) =
            spawn_http_sequence_server(&[("200 OK", "{}"), ("200 OK", "{}")]);
        let client = make_test_client(&url);

        ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();

        let opencode_conn = Connection::open(&db_path).unwrap();
        opencode_conn
            .execute(
                "UPDATE part SET data = ?1 WHERE id = 'prt_user'",
                [r#"{"type":"text","text":"HELLO from OpenCode"}"#],
            )
            .unwrap();

        ship_opencode_database(
            &db_path,
            &conn,
            &client,
            CompressionAlgo::Gzip,
            10_000_000,
            None,
            None,
        )
        .await
        .unwrap();

        handle.join().unwrap();
        let bodies = captured.lock().unwrap();
        assert_eq!(bodies.len(), 2);
        let payload = decode_payload(&bodies[1]);
        assert_eq!(payload["events"][0]["content_text"], "HELLO from OpenCode");
    }

    fn make_ship_trace(work_context: &'static str) -> ShipTraceContext {
        ShipTraceContext {
            work_context,
            observation_source: "test",
            observed_at_ms: 1,
            latest_observed_at_ms: None,
            wake_received_at_ms: None,
            enqueued_at_ms: 2,
            job_started_at_ms: 3,
            prepare_started_at_ms: 4,
            prepare_finished_at_ms: 5,
            prepare_blocking_queue_wait_ms: Some(0),
            prepare_open_db_ms: Some(0),
            prepare_identity_ms: Some(0),
            prepare_cursor_ms: Some(0),
            prepare_binding_wait_ms: Some(0),
            prepare_parse_ms: Some(0),
            prepare_batch_build_ms: Some(0),
            session_id_hint: None,
            turn_id: None,
            wake_reason: None,
            file_len_hint: None,
        }
    }

    #[test]
    fn live_transcript_timeout_tolerates_hosted_tail_latency() {
        let trace = make_ship_trace("live_transcript");

        assert_eq!(
            request_timeout_for_trace(Some(&trace)),
            Some(Duration::from_secs(20))
        );
    }

    #[test]
    fn non_live_transcript_uses_archive_timeout() {
        let trace = make_ship_trace("reconciliation_scan");

        assert_eq!(
            request_timeout_for_trace(Some(&trace)),
            Some(Duration::from_secs(75))
        );
        assert_eq!(
            request_timeout_for_trace(None),
            Some(Duration::from_secs(75))
        );
    }

    // ---------------------------------------------------------------
    // Regression: stale offsets skip files (the Python-era bug)
    // ---------------------------------------------------------------

    #[test]
    fn test_stale_offset_skips_file() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            claude_session_lines(),
            "aaaa1111-2222-3333-4444-555566667777.jsonl",
        );
        let path = dir
            .path()
            .join("aaaa1111-2222-3333-4444-555566667777.jsonl");
        let file_size = std::fs::metadata(&path).unwrap().len();

        // Simulate Python shipper having set the offset to file_size
        let fs = FileState::new(&conn);
        fs.set_offset(
            &path.to_string_lossy(),
            file_size,
            "aaaa1111-2222-3333-4444-555566667777",
            "aaaa1111-2222-3333-4444-555566667777",
            "claude",
        )
        .unwrap();

        // prepare_file should return None (no new content)
        let result = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(
            result.is_none(),
            "Stale offset should cause file to be skipped"
        );
    }

    // ---------------------------------------------------------------
    // Regression: after offset reset, files ship correctly
    // ---------------------------------------------------------------

    #[test]
    fn test_reset_offset_enables_shipping() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            claude_session_lines(),
            "aaaa1111-2222-3333-4444-555566667777.jsonl",
        );
        let path = dir
            .path()
            .join("aaaa1111-2222-3333-4444-555566667777.jsonl");
        let file_size = std::fs::metadata(&path).unwrap().len();

        // Set stale offset
        let fs = FileState::new(&conn);
        fs.set_offset(
            &path.to_string_lossy(),
            file_size,
            "aaaa1111-2222-3333-4444-555566667777",
            "aaaa1111-2222-3333-4444-555566667777",
            "claude",
        )
        .unwrap();

        // Reset offset to 0
        fs.reset_offsets(&path.to_string_lossy()).unwrap();

        // Now prepare_file should return Some with events
        let result = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(result.is_some(), "After reset, file should be prepared");
        let item = result.unwrap();
        assert_eq!(item.event_count, 2);
        assert_eq!(item.session_id, "aaaa1111-2222-3333-4444-555566667777");
    }

    // ---------------------------------------------------------------
    // Regression: Codex files parse and prepare correctly
    // ---------------------------------------------------------------

    #[test]
    fn test_prepare_codex_file() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            codex_session_lines(),
            "rollout-2026-02-15T10-00-00-cccc1111-2222-3333-4444-555566667777.jsonl",
        );
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-cccc1111-2222-3333-4444-555566667777.jsonl");

        let result = prepare_file(&path, "codex", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(result.is_some(), "Codex file should be prepared");
        let item = result.unwrap();
        // session_meta provides session_id override
        assert_eq!(item.session_id, "cccccccc-1111-2222-3333-444455556666");
        assert_eq!(item.event_count, 2); // user + assistant messages
        assert_eq!(item.provider, "codex");
    }

    #[test]
    fn test_stale_stored_codex_session_id_is_not_reused() {
        let (_tmp, conn) = make_db();
        let session_meta = r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#;
        let user_line = r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from codex"}]}}"#;

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-2026-02-15T10-00-00-foo.jsonl");
        std::fs::write(&path, format!("{}\n{}\n", session_meta, user_line)).unwrap();

        // Simulate stale file_state from a previous bugged ship:
        // offset points after session_meta, but stored session_id is wrong.
        let stale_session_id = "0d07131e-36c0-52f0-8bc6-3d52985240d8";
        let offset = (session_meta.len() + 1) as u64;
        let fs = FileState::new(&conn);
        fs.set_offset(
            &path.to_string_lossy(),
            offset,
            stale_session_id,
            stale_session_id,
            "codex",
        )
        .unwrap();

        let result = prepare_file(&path, "codex", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(
            result.is_some(),
            "Codex file should still prepare from incremental offset"
        );
        let item = result.unwrap();

        // Parser-resolved canonical ID must win over stale file_state.
        assert_eq!(item.session_id, "cccccccc-1111-2222-3333-444455556666");
    }

    #[test]
    fn test_prepare_codex_event_only_source_lines_skips_context_rows() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-cccc1111-2222-3333-4444-555566667777.jsonl");
        let session_meta = r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#;
        let developer_context = r#"{"type":"response_item","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"large injected context"}]}}"#;
        let user_message = r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from codex"}]}}"#;
        let user_end =
            (session_meta.len() + 1 + developer_context.len() + 1 + user_message.len() + 1) as u64;
        std::fs::write(
            &path,
            format!("{session_meta}\n{developer_context}\n{user_message}\n"),
        )
        .unwrap();
        let user_offset = (session_meta.len() + 1 + developer_context.len() + 1) as u64;

        let prepared = prepare_file_batches_with_source_line_mode_and_parse_tracker(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            10_000,
            Some("019d2869-1111-7222-8333-aaaaaaaaaaaa"),
            None,
            SourceLineMode::EventOnly,
            BatchBand::Live,
        )
        .unwrap()
        .expect("codex user message should prepare");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.offset, user_offset);
                assert_eq!(item.new_offset, user_end);
                assert_eq!(
                    decode_payload_source_offsets(&item.compressed),
                    vec![user_offset]
                );
            }
            PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => {
                panic!("codex user message should ship")
            }
        }
    }

    #[tokio::test]
    async fn test_live_codex_event_only_uses_live_cursor_not_archive_cursor() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-cccc1111-2222-3333-4444-555566667777.jsonl");
        let session_meta = r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#;
        let developer_context = r#"{"type":"response_item","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"large injected context"}]}}"#;
        let user_message = r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from codex"}]}}"#;
        std::fs::write(
            &path,
            format!("{session_meta}\n{developer_context}\n{user_message}\n"),
        )
        .unwrap();
        let path_str = path.to_string_lossy().to_string();
        let user_offset = (session_meta.len() + 1 + developer_context.len() + 1) as u64;
        let user_end =
            (session_meta.len() + 1 + developer_context.len() + 1 + user_message.len() + 1) as u64;
        let prepared = prepare_file_batches_with_source_line_mode_and_parse_tracker(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            10_000,
            Some("019d2869-1111-7222-8333-aaaaaaaaaaaa"),
            None,
            SourceLineMode::EventOnly,
            BatchBand::Live,
        )
        .unwrap()
        .expect("codex user message should prepare");

        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);
        let outcome = ship_prepared_file(prepared, &client, &conn, None, None)
            .await
            .unwrap();
        handle.join().unwrap();

        assert_eq!(outcome.events_shipped, 1);
        assert_eq!(FileState::new(&conn).get_offset(&path_str).unwrap(), 0);
        assert_eq!(
            crate::state::live_file_state::LiveFileState::new(&conn)
                .get_offset(&path_str)
                .unwrap(),
            user_end
        );
        let bodies = captured.lock().unwrap();
        assert_eq!(bodies.len(), 1);
        assert_eq!(decode_payload_source_offsets(&bodies[0]), vec![user_offset]);
    }

    #[test]
    fn test_prepare_codex_forked_child_ignores_managed_parent_override() {
        let (_tmp, _conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-dddd1111-2222-3333-4444-555566667777.jsonl");
        std::fs::write(&path, codex_forked_session_lines()).unwrap();

        let session_meta = r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"dddddddd-1111-2222-3333-444455556666","forked_from_id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#;
        let offset = (session_meta.len() + 1) as u64;
        let managed_parent_longhouse_id = "019d2869-1111-7222-8333-aaaaaaaaaaaa";

        let prepared = prepare_path_range(
            &path,
            "codex",
            offset,
            None,
            CompressionAlgo::Gzip,
            10_000,
            Some(managed_parent_longhouse_id),
        )
        .unwrap()
        .expect("forked codex child should prepare from incremental offset");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.session_id, "dddddddd-1111-2222-3333-444455556666");
                assert_eq!(
                    decode_payload_session_id(&item.compressed),
                    "dddddddd-1111-2222-3333-444455556666"
                );
            }
            PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => {
                panic!("forked codex child should ship, not ack-only or dead-letter")
            }
        }
    }

    #[test]
    fn test_prepare_codex_source_subagent_ignores_managed_parent_override() {
        let (_tmp, _conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-2026-04-29T19-48-36-child.jsonl");
        std::fs::write(&path, codex_source_subagent_session_lines()).unwrap();

        let session_meta = r#"{"type":"session_meta","timestamp":"2026-04-29T19:48:36Z","payload":{"type":"session_meta","id":"019ddb6e-114f-7643-89db-86c31a2aa706","source":{"subagent":{"thread_spawn":{"parent_thread_id":"019dd708-573a-7131-a4d9-9ee855520483","depth":1,"agent_nickname":"Ptolemy","agent_role":"default"}}},"cwd":"/tmp/test","cli_version":"0.125.0"}}"#;
        let offset = (session_meta.len() + 1) as u64;
        let managed_parent_longhouse_id = "c3026405-5e99-447f-ae5c-baacd848ac47";

        let prepared = prepare_path_range(
            &path,
            "codex",
            offset,
            None,
            CompressionAlgo::Gzip,
            10_000,
            Some(managed_parent_longhouse_id),
        )
        .unwrap()
        .expect("source subagent should prepare from incremental offset");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.session_id, "019ddb6e-114f-7643-89db-86c31a2aa706");
                assert_eq!(
                    decode_payload_session_id(&item.compressed),
                    "019ddb6e-114f-7643-89db-86c31a2aa706"
                );
            }
            PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => {
                panic!("source subagent should ship, not ack-only or dead-letter")
            }
        }
    }

    #[test]
    fn test_prepare_codex_non_thread_spawn_subagent_ignores_managed_parent_override() {
        let (_tmp, _conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-04-29T19-48-36-review-child.jsonl");
        std::fs::write(&path, codex_review_subagent_session_lines()).unwrap();

        let managed_parent_longhouse_id = "c3026405-5e99-447f-ae5c-baacd848ac47";

        let prepared = prepare_path_range(
            &path,
            "codex",
            0,
            None,
            CompressionAlgo::Gzip,
            10_000,
            Some(managed_parent_longhouse_id),
        )
        .unwrap()
        .expect("review subagent should prepare");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.session_id, "019ddb6e-114f-7643-89db-86c31a2aa706");
                assert_eq!(
                    decode_payload_session_id(&item.compressed),
                    "019ddb6e-114f-7643-89db-86c31a2aa706"
                );
            }
            PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => {
                panic!("review subagent should ship, not ack-only or dead-letter")
            }
        }
    }

    #[test]
    fn test_prepare_codex_injected_parent_context_keeps_child_session_id_with_override() {
        let (_tmp, _conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("rollout-2026-03-23T17-14-43-child.jsonl");
        let child_session_meta = r#"{"type":"session_meta","timestamp":"2026-03-23T17:14:43.614Z","payload":{"type":"session_meta","id":"019d1bb1-15c1-78c0-b4bc-f830965f237b","forked_from_id":"019d1805-66b6-78f1-aca9-91225867663d","cwd":"/Users/test/project"}}"#;
        let parent_session_meta = r#"{"type":"session_meta","timestamp":"2026-03-23T17:14:43.615Z","payload":{"type":"session_meta","id":"019d1805-66b6-78f1-aca9-91225867663d","cwd":"/Users/test/project"}}"#;
        let user_line = r#"{"type":"response_item","timestamp":"2026-03-23T17:14:44.000Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from child"}]}}"#;
        std::fs::write(
            &path,
            format!(
                "{}\n{}\n{}\n",
                child_session_meta, parent_session_meta, user_line
            ),
        )
        .unwrap();

        let offset = (child_session_meta.len() + 1) as u64;
        let managed_parent_longhouse_id = "019d2869-1111-7222-8333-aaaaaaaaaaaa";

        let prepared = prepare_path_range(
            &path,
            "codex",
            offset,
            None,
            CompressionAlgo::Gzip,
            10_000,
            Some(managed_parent_longhouse_id),
        )
        .unwrap()
        .expect("forked codex child should prepare from incremental offset");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.session_id, "019d1bb1-15c1-78c0-b4bc-f830965f237b");
                assert_eq!(
                    decode_payload_session_id(&item.compressed),
                    "019d1bb1-15c1-78c0-b4bc-f830965f237b"
                );
            }
            PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => {
                panic!("forked codex child should ship, not ack-only or dead-letter")
            }
        }
    }

    // ---------------------------------------------------------------
    // Regression: subagent files get deterministic UUID v5
    // ---------------------------------------------------------------

    #[test]
    fn test_subagent_file_gets_uuid() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(claude_session_lines(), "agent-a51c878.jsonl");
        let path = dir.path().join("agent-a51c878.jsonl");

        let result = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(result.is_some(), "Subagent file should be prepared");
        let item = result.unwrap();
        // Should be a valid UUID (v5), not "agent-a51c878"
        assert!(
            uuid::Uuid::parse_str(&item.session_id).is_ok(),
            "Session ID should be a valid UUID, got: {}",
            item.session_id
        );
        assert_ne!(item.session_id, "agent-a51c878");
    }

    // ---------------------------------------------------------------
    // Regression: subagent UUID is deterministic (same path = same UUID)
    // ---------------------------------------------------------------

    #[test]
    fn test_subagent_uuid_is_deterministic() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(claude_session_lines(), "agent-a51c878.jsonl");
        let path = dir.path().join("agent-a51c878.jsonl");

        let result1 = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        // Reset offset so we can prepare again
        let fs = FileState::new(&conn);
        fs.reset_offsets(&path.to_string_lossy()).unwrap();
        let result2 = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();

        assert_eq!(
            result1.unwrap().session_id,
            result2.unwrap().session_id,
            "Same file path should produce same UUID"
        );
    }

    // ---------------------------------------------------------------
    // Regression: truncated files reset offset and re-ship
    // ---------------------------------------------------------------

    #[test]
    fn test_truncated_file_resets_and_ships() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            claude_session_lines(),
            "bbbb1111-2222-3333-4444-555566667777.jsonl",
        );
        let path = dir
            .path()
            .join("bbbb1111-2222-3333-4444-555566667777.jsonl");

        // Set offset way beyond actual file size (simulates truncation)
        let fs = FileState::new(&conn);
        fs.set_offset(
            &path.to_string_lossy(),
            999999,
            "bbbb1111-2222-3333-4444-555566667777",
            "bbbb1111-2222-3333-4444-555566667777",
            "claude",
        )
        .unwrap();

        // prepare_file should detect truncation, reset, and parse from 0
        let result = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(result.is_some(), "Truncated file should be re-processed");
        let item = result.unwrap();
        assert_eq!(
            item.offset, 0,
            "Should start from offset 0 after truncation"
        );
        assert_eq!(item.event_count, 2);
    }

    #[test]
    fn test_replaced_file_resets_cursor_and_retires_stale_spool_pointer() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replace1111-2222-3333-4444-555566667777.jsonl");
        let path_str = path.to_string_lossy().to_string();
        let old_line = make_line("replace-old", "old");
        std::fs::write(&path, format!("{old_line}\n")).unwrap();
        let old_end = std::fs::metadata(&path).unwrap().len();

        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            old_end,
            "claude",
            "replace1111-2222-3333-4444-555566667777",
            "replace1111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                old_end,
                Some("replace1111-2222-3333-4444-555566667777"),
            )
            .unwrap();
        assert_eq!(spool.pending_count().unwrap(), 1);

        let replacement = dir.path().join("replacement.tmp");
        let new_line = make_line("replace-new", "new file content after replacement");
        std::fs::write(&replacement, format!("{new_line}\n")).unwrap();
        std::fs::rename(&replacement, &path).unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap()
                .expect("replacement should be treated as a fresh file");

        assert_eq!(prepared.offset, 0);
        assert_eq!(prepared.total_event_count(), 1);
        assert_eq!(fs.get_offset(&path_str).unwrap(), 0);
        assert_eq!(fs.get_queued_offset(&path_str).unwrap(), 0);
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(spool.dead_count().unwrap(), 1);

        let first_ship = prepared
            .actions
            .iter()
            .find_map(|action| match action {
                PreparedAction::Ship(item) => Some(item),
                PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => None,
            })
            .expect("replacement should prepare a ship payload");
        assert_eq!(
            decode_payload_rewind_hints(&first_ship.compressed),
            vec![(path_str, 0, "file_replaced".to_string())]
        );
    }

    #[tokio::test]
    async fn test_replay_dead_letters_stale_pointer_when_file_replaced() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replayreplace1111-2222-3333-4444-555566667777.jsonl");
        let path_str = path.to_string_lossy().to_string();
        let old_line = make_line("replay-replace-old", "old");
        std::fs::write(&path, format!("{old_line}\n")).unwrap();
        let old_end = std::fs::metadata(&path).unwrap().len();

        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            old_end,
            "claude",
            "replayreplace1111-2222-3333-4444-555566667777",
            "replayreplace1111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                old_end,
                Some("replayreplace1111-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let replacement = dir.path().join("replay-replacement.tmp");
        let new_line = make_line("replay-replace-new", "new file");
        std::fs::write(&replacement, format!("{new_line}\n")).unwrap();
        std::fs::rename(&replacement, &path).unwrap();

        let client = make_test_client("http://127.0.0.1:9");
        let replay = replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
            &conn,
            &client,
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            None,
        )
        .await
        .unwrap();

        assert_eq!(replay.resolved, 0);
        assert_eq!(replay.failed, 1);
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(spool.dead_count().unwrap(), 1);
        assert_eq!(fs.get_offset(&path_str).unwrap(), 0);
        assert_eq!(fs.get_queued_offset(&path_str).unwrap(), 0);
    }

    #[test]
    fn test_replaced_file_resets_live_cursor() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replacelive1111-2222-3333-4444-555566667777.jsonl");
        let path_str = path.to_string_lossy().to_string();
        let old_line = make_line("replace-live-old", "old live");
        std::fs::write(&path, format!("{old_line}\n")).unwrap();
        let old_end = std::fs::metadata(&path).unwrap().len();

        let live = LiveFileState::new(&conn);
        live.set_offset(
            &path_str,
            old_end,
            "claude",
            "replacelive1111-2222-3333-4444-555566667777",
        )
        .unwrap();

        let replacement = dir.path().join("replacement-live.tmp");
        let new_line = make_line("replace-live-new", "new live file");
        std::fs::write(&replacement, format!("{new_line}\n")).unwrap();
        std::fs::rename(&replacement, &path).unwrap();

        let prepared = prepare_file_batches_with_source_line_mode_and_parse_tracker(
            &path,
            "claude",
            CompressionAlgo::Gzip,
            &conn,
            10_000,
            None,
            None,
            SourceLineMode::EventOnly,
            BatchBand::Live,
        )
        .unwrap()
        .expect("replacement should be treated as fresh live content");

        assert_eq!(prepared.cursor_mode, CursorMode::Live);
        assert_eq!(prepared.offset, 0);
        assert_eq!(prepared.total_event_count(), 1);
        assert_eq!(live.get_offset(&path_str).unwrap(), 0);
    }

    // ---------------------------------------------------------------
    // Regression: incremental append ships only new events
    // ---------------------------------------------------------------

    #[test]
    fn test_incremental_append_ships_new_events_only() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("dddd1111-2222-3333-4444-555566667777.jsonl");

        // Write initial content
        let line1 = r#"{"type":"user","uuid":"inc-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"first"}}"#;
        std::fs::write(&path, format!("{}\n", line1)).unwrap();

        // First prepare ships 1 event
        let result1 = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        let item1 = result1.unwrap();
        assert_eq!(item1.event_count, 1);

        // Record the offset (simulating ship_and_record success)
        let fs = FileState::new(&conn);
        fs.set_offset(
            &path.to_string_lossy(),
            item1.new_offset,
            "dddd1111-2222-3333-4444-555566667777",
            "dddd1111-2222-3333-4444-555566667777",
            "claude",
        )
        .unwrap();

        // Append more content
        let mut f = std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap();
        writeln!(f, r#"{{"type":"assistant","uuid":"inc-2","timestamp":"2026-02-15T10:00:01Z","message":{{"content":[{{"type":"text","text":"second"}}]}}}}"#).unwrap();

        // Second prepare ships only the new event
        let result2 = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        let item2 = result2.unwrap();
        assert_eq!(item2.event_count, 1, "Should only ship the appended event");
        assert_eq!(
            item2.offset, item1.new_offset,
            "Should start from previous offset"
        );
    }

    #[test]
    fn test_prepare_file_uses_last_good_offset_for_partial_eof() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("eeee1111-2222-3333-4444-555566667777.jsonl");

        let complete = r#"{"type":"user","uuid":"partial-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"complete"}}"#;
        let partial = r#"{"type":"assistant","uuid":"partial-2","timestamp":"2026-02-15T10:00:01Z","message":{"con"#;
        std::fs::write(&path, format!("{}\n{}", complete, partial)).unwrap();

        let result = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        let item = result.expect("prepare_file should ship the complete line");

        assert_eq!(item.event_count, 1);
        assert_eq!(item.offset, 0);
        assert_eq!(item.new_offset, (complete.len() + 1) as u64);
        assert!(
            item.new_offset < std::fs::metadata(&path).unwrap().len(),
            "new_offset should stop before the incomplete trailing line",
        );
    }

    #[tokio::test]
    async fn test_unacked_spool_gap_blocks_newer_bytes_until_replay_resolves() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("queuedgap1111-2222-3333-4444-555566667777.jsonl");
        let path_str = path.to_string_lossy().to_string();

        let first = make_line("queued-gap-1", "first");
        let second = make_line("queued-gap-2", "second");
        std::fs::write(&path, format!("{first}\n")).unwrap();

        let prepared_first =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap()
                .expect("first complete line should prepare");
        let first_end = prepared_first.new_offset;

        let (failing_url, _captured_fail, failing_handle) =
            spawn_http_sequence_server(&[("500 Internal Server Error", "{}")]);
        let failing_client = make_test_client(&failing_url);
        let failed = ship_prepared_file(prepared_first, &failing_client, &conn, None, None)
            .await
            .unwrap();
        failing_handle.join().unwrap();

        assert!(!failed.fully_processed);
        let file_state = FileState::new(&conn);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), first_end);
        assert_eq!(Spool::new(&conn).pending_count().unwrap(), 1);

        std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(format!("{second}\n").as_bytes())
            .unwrap();
        let final_end = std::fs::metadata(&path).unwrap().len();

        let blocked =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap();
        assert!(
            blocked.is_none(),
            "fresh bytes must wait while an older queued gap is unacked"
        );

        let (replay_url, captured_replay, replay_handle) =
            spawn_http_sequence_server(&[("200 OK", "{}")]);
        let replay_client = make_test_client(&replay_url);
        let replay = replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
            &conn,
            &replay_client,
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            None,
        )
        .await
        .unwrap();
        replay_handle.join().unwrap();

        assert_eq!(replay.resolved, 1);
        assert_eq!(replay.failed, 0);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), first_end);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), first_end);
        assert_eq!(Spool::new(&conn).pending_count().unwrap(), 0);
        assert_eq!(
            decode_payload_source_offsets(&captured_replay.lock().unwrap()[0]),
            vec![0]
        );

        let prepared_second =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap()
                .expect("newer line should prepare once queued gap is resolved");
        assert_eq!(prepared_second.offset, first_end);
        assert_eq!(prepared_second.new_offset, final_end);
        assert_eq!(prepared_second.total_event_count(), 1);
    }

    #[tokio::test]
    async fn test_reconciliation_scan_repairs_missed_append() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let provider_root = dir.path().join("projects");
        std::fs::create_dir_all(&provider_root).unwrap();
        let path = provider_root.join("reconcile1111-2222-3333-4444-555566667777.jsonl");
        let path_str = path.to_string_lossy().to_string();
        let first = make_line("reconcile-1", "first");
        let second = make_line("reconcile-2", "second");
        std::fs::write(&path, format!("{first}\n")).unwrap();
        let first_end = std::fs::metadata(&path).unwrap().len();

        FileState::new(&conn)
            .set_offset(
                &path_str,
                first_end,
                "reconcile1111-2222-3333-4444-555566667777",
                "reconcile1111-2222-3333-4444-555566667777",
                "claude",
            )
            .unwrap();

        std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(format!("{second}\n").as_bytes())
            .unwrap();

        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);
        let providers = vec![ProviderConfig {
            name: "claude",
            root: provider_root,
            extension: "jsonl",
        }];

        let (files_shipped, events_shipped) =
            full_scan(&providers, &conn, &client, CompressionAlgo::Gzip, None)
                .await
                .unwrap();
        handle.join().unwrap();

        assert_eq!(files_shipped, 1);
        assert_eq!(events_shipped, 1);
        assert_eq!(
            FileState::new(&conn).get_offset(&path_str).unwrap(),
            std::fs::metadata(&path).unwrap().len()
        );
        let bodies = captured.lock().unwrap().clone();
        assert_eq!(bodies.len(), 1);
        assert_eq!(decode_payload_source_offsets(&bodies[0]), vec![first_end]);
    }

    #[tokio::test]
    async fn test_reconciliation_scan_uses_background_repair_batch_size() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let provider_root = dir.path().join("projects");
        std::fs::create_dir_all(&provider_root).unwrap();
        let path = provider_root.join("reconcile-batch-1111-2222-3333-444455556666.jsonl");

        let large_text = "x".repeat(280 * 1024);
        let lines = [
            make_line("reconcile-batch-1", &large_text),
            make_line("reconcile-batch-2", &large_text),
            make_line("reconcile-batch-3", &large_text),
        ];
        let mut offsets = Vec::new();
        let mut cursor = 0_u64;
        for line in &lines {
            offsets.push(cursor);
            cursor += (line.len() + 1) as u64;
        }
        std::fs::write(&path, format!("{}\n{}\n{}\n", lines[0], lines[1], lines[2])).unwrap();

        let (url, captured, handle) =
            spawn_http_sequence_server(&[("200 OK", "{}"), ("200 OK", "{}"), ("200 OK", "{}")]);
        let client = make_test_client(&url);
        let providers = vec![ProviderConfig {
            name: "claude",
            root: provider_root,
            extension: "jsonl",
        }];

        let (files_shipped, events_shipped) =
            full_scan(&providers, &conn, &client, CompressionAlgo::Gzip, None)
                .await
                .unwrap();
        handle.join().unwrap();

        assert_eq!(files_shipped, 1);
        assert_eq!(events_shipped, 3);
        let bodies = captured.lock().unwrap().clone();
        assert_eq!(
            bodies.len(),
            3,
            "background reconciliation scans should split large repairs into small POSTs"
        );
        assert_eq!(decode_payload_source_offsets(&bodies[0]), vec![offsets[0]]);
        assert_eq!(decode_payload_source_offsets(&bodies[1]), vec![offsets[1]]);
        assert_eq!(decode_payload_source_offsets(&bodies[2]), vec![offsets[2]]);
    }

    #[test]
    fn test_prepare_file_batches_marks_user_only_partial_turn_as_not_reply_ready() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replyready1111-2222-3333-4444-555566667777.jsonl");

        let complete = r#"{"type":"user","uuid":"replyready-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"complete"}}"#;
        let partial = r#"{"type":"assistant","uuid":"replyready-2","timestamp":"2026-02-15T10:00:01Z","message":{"con"#;
        std::fs::write(&path, format!("{}\n{}", complete, partial)).unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap()
                .expect("file should still prepare the complete user line");

        assert!(!prepared.has_reply_evidence);
        assert_eq!(prepared.total_event_count(), 1);
        assert_eq!(prepared.new_offset, (complete.len() + 1) as u64);
    }

    #[test]
    fn test_prepare_file_batches_split_small_batch_limit() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("batch1111-2222-3333-4444-555566667777.jsonl");
        let lines = vec![
            make_line("batch-1", &"a".repeat(64)),
            make_line("batch-2", &"b".repeat(64)),
            make_line("batch-3", &"c".repeat(64)),
        ];
        std::fs::write(&path, format!("{}\n{}\n{}\n", lines[0], lines[1], lines[2])).unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 400, None).unwrap();
        let prepared = prepared.expect("file should prepare into batched actions");

        assert!(
            prepared.actions.len() >= 2,
            "small batch limit should split the file into multiple actions"
        );

        let covered: Vec<(u64, u64)> = prepared
            .actions
            .iter()
            .map(|action| (action.offset(), action.new_offset()))
            .collect();
        assert_eq!(covered.first().copied(), Some((0, covered[0].1)));
        assert_eq!(
            covered.last().map(|(_, end)| *end),
            Some(prepared.new_offset),
            "last batch must end at the prepared file end offset"
        );
        for pair in covered.windows(2) {
            assert_eq!(
                pair[0].1, pair[1].0,
                "prepared batches must be contiguous with no gaps or overlap"
            );
        }
    }

    #[test]
    fn test_prepare_file_batches_truncation_hint_only_on_first_ship_batch() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("truncated-batch1111-2222-3333-4444-555566667777.jsonl");
        let lines = vec![
            make_line("batch-1", &"a".repeat(64)),
            make_line("batch-2", &"b".repeat(64)),
            make_line("batch-3", &"c".repeat(64)),
            make_line("batch-4", &"d".repeat(64)),
        ];
        std::fs::write(
            &path,
            format!("{}\n{}\n{}\n{}\n", lines[0], lines[1], lines[2], lines[3]),
        )
        .unwrap();

        let fs = FileState::new(&conn);
        fs.set_offset(
            &path.to_string_lossy(),
            999999,
            "truncated-batch1111-2222-3333-4444-555566667777",
            "truncated-batch1111-2222-3333-4444-555566667777",
            "claude",
        )
        .unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 550, None).unwrap();
        let prepared = prepared.expect("truncated file should prepare into ship batches");

        let ship_items: Vec<ShipItem> = prepared
            .actions
            .into_iter()
            .filter_map(|action| match action {
                PreparedAction::Ship(item) => Some(item),
                PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => None,
            })
            .collect();
        assert!(
            ship_items.len() >= 2,
            "small batch limit should split the truncated file into multiple ship payloads"
        );

        assert_eq!(
            decode_payload_rewind_hints(&ship_items[0].compressed),
            vec![(
                path.to_string_lossy().to_string(),
                0,
                "truncation".to_string(),
            )]
        );
        for item in ship_items.iter().skip(1) {
            assert!(
                decode_payload_rewind_hints(&item.compressed).is_empty(),
                "only the first ship batch after truncation should carry the explicit rewind hint"
            );
        }
    }

    #[test]
    fn test_prepare_antigravity_legacy_json_file_uses_source_line_boundaries_when_archive_is_available(
    ) {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session-gemini.json");
        let gemini = serde_json::json!({
            "sessionId": "5053c934-f66d-4fea-96af-f95181de5986",
            "startTime": "2026-02-20T15:59:12.296Z",
            "messages": [
                {
                    "id": "msg-1",
                    "timestamp": "2026-02-20T15:59:12.296Z",
                    "type": "user",
                    "content": "Reply with exactly: \"gemini ok\""
                },
                {
                    "id": "msg-2",
                    "timestamp": "2026-02-20T15:59:15.853Z",
                    "type": "gemini",
                    "content": "gemini ok"
                }
            ]
        });
        std::fs::write(&path, serde_json::to_vec_pretty(&gemini).unwrap()).unwrap();

        let prepared =
            prepare_file_batches(&path, "antigravity", CompressionAlgo::Gzip, &conn, 32, None)
                .unwrap();
        let prepared =
            prepared.expect("legacy JSON file should still prepare with source-line archive");

        assert!(
            prepared.actions.len() > 1,
            "source-line archive should let legacy JSON split instead of whole-document fallback"
        );
        assert!(prepared.actions.iter().all(|action| match action {
            PreparedAction::DeadLetter(item) => !item.reason.contains("whole-document payload"),
            _ => true,
        }));
        assert!(
            prepared.actions.iter().any(|action| matches!(
                action,
                PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_)
            )),
            "tiny batch limit should produce line-bounded follow-up actions"
        );

        let prepared = prepare_file_batches(
            &path,
            "antigravity",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
            None,
        )
        .unwrap();
        let prepared = prepared.expect("legacy JSON file should prepare at normal batch limit");
        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, std::fs::metadata(&path).unwrap().len());
                assert_eq!(item.event_count, 2);
            }
            PreparedAction::DeadLetter(_) => {
                panic!("normal batch limit should ship whole-document legacy JSON payload")
            }
            PreparedAction::AckOnly(_) => {
                panic!("conversation legacy JSON file should not ack-only")
            }
        }
    }

    #[test]
    fn test_prepare_antigravity_legacy_json_rewritten_document_rewinds_from_previous_offset() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session-gemini-rewritten.json");
        let gemini = serde_json::json!({
            "sessionId": "5053c934-f66d-4fea-96af-f95181de5986",
            "startTime": "2026-02-20T15:59:12.296Z",
            "messages": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "timestamp": "2026-02-20T15:59:12.296Z",
                    "type": "user",
                    "content": "Reply with exactly: \"gemini ok\""
                },
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "timestamp": "2026-02-20T15:59:15.853Z",
                    "type": "gemini",
                    "content": format!("gemini ok {}", "x".repeat(700))
                }
            ]
        });
        std::fs::write(&path, serde_json::to_vec_pretty(&gemini).unwrap()).unwrap();
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();
        assert!(
            file_len > 497,
            "fixture should be large enough to reproduce the real offset"
        );

        FileState::new(&conn)
            .set_offset(
                &path_str,
                497,
                "5053c934-f66d-4fea-96af-f95181de5986",
                "5053c934-f66d-4fea-96af-f95181de5986",
                "antigravity",
            )
            .unwrap();

        let prepared = prepare_file_batches(
            &path,
            "antigravity",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
            None,
        )
        .unwrap()
        .expect("rewritten legacy JSON file should prepare from the beginning");

        assert_eq!(prepared.offset, 0);
        assert_eq!(prepared.new_offset, file_len);
        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, file_len);
                assert_eq!(item.event_count, 2);
            }
            PreparedAction::AckOnly(_) | PreparedAction::DeadLetter(_) => {
                panic!("conversation legacy JSON rewrite should ship as a full document")
            }
        }
    }

    #[test]
    fn test_prepare_antigravity_legacy_json_info_file_acknowledges_without_shipping() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("session-gemini-info.json");
        let gemini = serde_json::json!({
            "sessionId": "61d59eea-a6ca-4ebf-a6e1-dc25c25296c4",
            "startTime": "2026-02-22T01:05:34.121Z",
            "messages": [
                {
                    "id": "3ece1c1b-6a6c-40af-8aa7-c6d6d991ab50",
                    "timestamp": "2026-02-22T01:05:34.121Z",
                    "type": "info",
                    "content": "Update successful! The new version will be used on your next run."
                }
            ]
        });
        std::fs::write(&path, serde_json::to_vec_pretty(&gemini).unwrap()).unwrap();

        let prepared = prepare_file_batches(
            &path,
            "antigravity",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
            None,
        )
        .unwrap();
        let prepared = prepared.expect("legacy JSON info file should no longer be skipped");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::AckOnly(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, std::fs::metadata(&path).unwrap().len());
                assert_eq!(item.provider, "antigravity");
            }
            PreparedAction::Ship(_) | PreparedAction::DeadLetter(_) => {
                panic!("legacy JSON info-only file should be acknowledged without shipping")
            }
        }
    }

    #[test]
    fn test_prepare_codex_oversize_compacted_line_acknowledges_without_dead_letter() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-03-10T02-45-21-019cd67e-36d7-7271-af29-9dcc8aba405e.jsonl");
        let compacted = serde_json::json!({
            "timestamp": "2026-03-11T23:11:59.551Z",
            "type": "compacted",
            "payload": {
                "message": "",
                "replacement_history": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "x".repeat(512)
                    }]
                }]
            }
        });
        std::fs::write(&path, format!("{}\n", compacted)).unwrap();

        let prepared =
            prepare_file_batches(&path, "codex", CompressionAlgo::Gzip, &conn, 200, None).unwrap();
        let prepared = prepared.expect("oversize compacted line should be acknowledged");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::AckOnly(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, std::fs::metadata(&path).unwrap().len());
                assert_eq!(item.provider, "codex");
            }
            PreparedAction::Ship(_) | PreparedAction::DeadLetter(_) => {
                panic!("oversize zero-event compacted line should not be dead-lettered")
            }
        }
    }

    #[tokio::test]
    async fn test_ship_prepared_file_ack_only_updates_offsets() {
        let (_tmp, conn) = make_db();
        let client = make_test_client("http://127.0.0.1:9");
        let prepared = PreparedFile {
            path_str: "/tmp/antigravity-legacy-info.json".to_string(),
            offset: 0,
            new_offset: 413,
            has_reply_evidence: false,
            cursor_mode: CursorMode::Archive,
            actions: vec![PreparedAction::AckOnly(AckOnlyItem {
                path_str: "/tmp/antigravity-legacy-info.json".to_string(),
                provider: "antigravity".to_string(),
                offset: 0,
                new_offset: 413,
                session_id: "61d59eea-a6ca-4ebf-a6e1-dc25c25296c4".to_string(),
            })],
        };

        let outcome = ship_prepared_file(prepared, &client, &conn, None, None)
            .await
            .unwrap();
        let fs = FileState::new(&conn);
        assert_eq!(outcome.events_shipped, 0);
        assert_eq!(outcome.dead_lettered, 0);
        assert!(outcome.fully_processed);
        assert_eq!(
            fs.get_offset("/tmp/antigravity-legacy-info.json").unwrap(),
            413
        );
        assert_eq!(
            fs.get_queued_offset("/tmp/antigravity-legacy-info.json")
                .unwrap(),
            413
        );
    }

    #[tokio::test]
    async fn test_replay_oversize_zero_event_range_clears_pending_entry() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-03-10T02-45-21-019cd67e-36d7-7271-af29-9dcc8aba405e.jsonl");
        let compacted = serde_json::json!({
            "timestamp": "2026-03-11T23:11:59.551Z",
            "type": "compacted",
            "payload": {
                "message": "",
                "replacement_history": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": "x".repeat(512)
                    }]
                }]
            }
        });
        std::fs::write(&path, format!("{}\n", compacted)).unwrap();

        let spool = Spool::new(&conn);
        let file_state = FileState::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();
        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "codex",
                "019cd67e-36d7-7271-af29-9dcc8aba405e",
                "019cd67e-36d7-7271-af29-9dcc8aba405e",
            )
            .unwrap();
        spool
            .enqueue(
                "codex",
                &path_str,
                0,
                file_len,
                Some("019cd67e-36d7-7271-af29-9dcc8aba405e"),
            )
            .unwrap();

        let client = make_test_client("http://127.0.0.1:9");
        let (ok, failed) =
            replay_spool_batch_with_batch_bytes(&conn, &client, CompressionAlgo::Gzip, 10, 200)
                .await
                .unwrap();

        assert_eq!(ok, 1);
        assert_eq!(failed, 0);
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), file_len);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), file_len);
    }

    #[tokio::test]
    async fn test_replay_retryable_client_error_stays_pending() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replay-retryable-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let spool = Spool::new(&conn);
        let file_state = FileState::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();
        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "claude",
                "replay-retryable-1111-2222-3333-444455556666",
                "replay-retryable-1111-2222-3333-444455556666",
            )
            .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_len,
                Some("replay-retryable-1111-2222-3333-444455556666"),
            )
            .unwrap();

        let (url, _captured, handle) =
            spawn_http_sequence_server(&[("401 Unauthorized", "{\"detail\":\"bad token\"}")]);
        let client = make_test_client(&url);

        let (ok, failed) =
            replay_spool_batch_with_batch_bytes(&conn, &client, CompressionAlgo::Gzip, 10, 10_000)
                .await
                .unwrap();
        handle.join().unwrap();

        assert_eq!(ok, 0);
        assert_eq!(failed, 1);
        assert_eq!(spool.pending_count().unwrap(), 1);
        assert_eq!(spool.dead_count().unwrap(), 0);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), file_len);
    }

    #[tokio::test]
    async fn test_replay_payload_rejection_dead_letters_range() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replay-payload-rejected-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let spool = Spool::new(&conn);
        let file_state = FileState::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();
        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "claude",
                "replay-payload-rejected-1111-2222-3333-444455556666",
                "replay-payload-rejected-1111-2222-3333-444455556666",
            )
            .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_len,
                Some("replay-payload-rejected-1111-2222-3333-444455556666"),
            )
            .unwrap();

        let (url, _captured, handle) = spawn_http_sequence_server(&[(
            "422 Unprocessable Entity",
            "{\"detail\":\"Invalid payload: missing field\"}",
        )]);
        let client = make_test_client(&url);

        let (ok, failed) =
            replay_spool_batch_with_batch_bytes(&conn, &client, CompressionAlgo::Gzip, 10, 10_000)
                .await
                .unwrap();
        handle.join().unwrap();

        assert_eq!(ok, 1);
        assert_eq!(failed, 0);
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(spool.dead_count().unwrap(), 1);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), file_len);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), file_len);
    }

    #[tokio::test]
    async fn test_replay_spool_for_path_processes_only_target_file() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path_a = dir
            .path()
            .join("aaaaaaaa-1111-2222-3333-444455556666.jsonl");
        let path_b = dir
            .path()
            .join("bbbbbbbb-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path_a, claude_session_lines()).unwrap();
        std::fs::write(&path_b, claude_session_lines()).unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let path_a_str = path_a.to_string_lossy().to_string();
        let path_b_str = path_b.to_string_lossy().to_string();
        let path_a_len = std::fs::metadata(&path_a).unwrap().len();
        let path_b_len = std::fs::metadata(&path_b).unwrap().len();

        file_state
            .set_queued_offset(
                &path_a_str,
                path_a_len,
                "claude",
                "aaaaaaaa-1111-2222-3333-444455556666",
                "aaaaaaaa-1111-2222-3333-444455556666",
            )
            .unwrap();
        file_state
            .set_queued_offset(
                &path_b_str,
                path_b_len,
                "claude",
                "bbbbbbbb-1111-2222-3333-444455556666",
                "bbbbbbbb-1111-2222-3333-444455556666",
            )
            .unwrap();

        spool
            .enqueue(
                "claude",
                &path_a_str,
                0,
                path_a_len,
                Some("aaaaaaaa-1111-2222-3333-444455556666"),
            )
            .unwrap();
        spool
            .enqueue(
                "claude",
                &path_b_str,
                0,
                path_b_len,
                Some("bbbbbbbb-1111-2222-3333-444455556666"),
            )
            .unwrap();

        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        let client = make_test_client(&url);
        let outcome = replay_spool_for_path_with_batch_bytes_and_parse_tracker(
            &conn,
            &client,
            CompressionAlgo::Gzip,
            &path_a,
            10,
            10_000,
            None,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();
        handle.join().unwrap();

        assert_eq!(outcome.resolved, 1);
        assert_eq!(outcome.failed, 0);
        assert_eq!(outcome.events_shipped, 2);
        assert!(!outcome.had_connect_error);
        assert!(spool
            .pending_entries_for_path(&path_a_str, 10)
            .unwrap()
            .is_empty());
        assert_eq!(
            spool
                .pending_entries_for_path(&path_b_str, 10)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(file_state.get_offset(&path_a_str).unwrap(), path_a_len);
        assert_eq!(file_state.get_offset(&path_b_str).unwrap(), 0);
    }

    #[tokio::test]
    async fn test_replay_spool_for_path_records_ship_stats() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("eeeeeeee-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();

        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "claude",
                "eeeeeeee-1111-2222-3333-444455556666",
                "eeeeeeee-1111-2222-3333-444455556666",
            )
            .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_len,
                Some("eeeeeeee-1111-2222-3333-444455556666"),
            )
            .unwrap();

        let ship_stats = RecentShipStatsTracker::new();
        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        let client = make_test_client(&url);
        let outcome = replay_spool_for_path_with_batch_bytes_and_parse_tracker(
            &conn,
            &client,
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            Some(&ship_stats),
            None,
            None,
            None,
        )
        .await
        .unwrap();
        handle.join().unwrap();

        assert_eq!(outcome.resolved, 1);
        let summary = ship_stats.summary();
        assert_eq!(summary.ship_attempts_1h, 1);
        assert_eq!(summary.ship_successes_1h, 1);
        assert_eq!(summary.last_ship_result.as_deref(), Some("ok"));
        assert_eq!(summary.last_ship_http_status, None);
        assert_eq!(summary.lanes.archive.attempts_1h, 1);
        assert_eq!(summary.lanes.archive.successes_1h, 1);
        assert_eq!(summary.lanes.archive.bytes_1h, file_len);
        assert!(summary.lanes.archive.events_1h > 0);
    }

    #[test]
    fn test_typed_server_backpressure_uses_retry_after() {
        let result =
            ShipResult::ServerBackpressure(crate::shipping::client::ServerBackpressureDetail {
                status_code: 503,
                kind: "archive_ingest_backpressure",
                body: "{\"detail\":\"Archive ingest backlog is throttled\"}".to_string(),
                lane: Some("archive".to_string()),
                retry_after_seconds: Some(5.0),
            });

        assert!(ship_result_is_backpressure(&result));
        assert_eq!(
            transient_error_kind(&result),
            Some("archive_ingest_backpressure")
        );
        assert_eq!(
            ship_result_retry_after(&result),
            Some(Duration::from_secs(5))
        );
    }

    #[tokio::test]
    async fn test_replay_spool_connect_error_records_backoff() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replay-connect-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();

        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "claude",
                "replay-connect-1111-2222-3333-444455556666",
                "replay-connect-1111-2222-3333-444455556666",
            )
            .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_len,
                Some("replay-connect-1111-2222-3333-444455556666"),
            )
            .unwrap();

        let before = chrono::Utc::now();
        let ship_stats = RecentShipStatsTracker::new();
        let outcome = replay_spool_for_path_with_batch_bytes_and_parse_tracker(
            &conn,
            &make_test_client("http://127.0.0.1:9"),
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            Some(&ship_stats),
            None,
            None,
            None,
        )
        .await
        .unwrap();

        assert_eq!(outcome.resolved, 0);
        assert_eq!(outcome.failed, 1);
        assert!(outcome.had_connect_error);
        assert_eq!(spool.pending_count().unwrap(), 1);

        let row: (String, i64, String, String) = conn
            .query_row(
                "SELECT status, retry_count, last_error, next_retry_at FROM spool_queue WHERE file_path = ?1",
                [&path_str],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(row.0, "pending");
        assert_eq!(row.1, 1);
        assert!(!row.2.is_empty());
        let next_retry = chrono::DateTime::parse_from_rfc3339(&row.3)
            .unwrap()
            .with_timezone(&chrono::Utc);
        assert!(next_retry > before);

        let summary = ship_stats.summary();
        assert_eq!(summary.ship_attempts_1h, 1);
        assert_eq!(summary.ship_connect_errors_1h, 1);
        assert_eq!(summary.ship_attempts_10m, 1);
        assert_eq!(summary.ship_connect_errors_10m, 1);
    }

    #[tokio::test]
    async fn test_replay_ready_spool_for_path_replays_never_failed_future_retry() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("cccccccc-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();

        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "claude",
                "cccccccc-1111-2222-3333-444455556666",
                "cccccccc-1111-2222-3333-444455556666",
            )
            .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_len,
                Some("cccccccc-1111-2222-3333-444455556666"),
            )
            .unwrap();
        let future_retry_at = (chrono::Utc::now() + chrono::Duration::minutes(5)).to_rfc3339();
        conn.execute(
            "UPDATE spool_queue SET next_retry_at = ?1 WHERE file_path = ?2",
            rusqlite::params![future_retry_at, path_str],
        )
        .unwrap();

        let regular = replay_spool_for_path_with_batch_bytes_and_parse_tracker(
            &conn,
            &make_test_client("http://127.0.0.1:9"),
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();
        assert_eq!(regular.resolved, 0);
        assert_eq!(regular.events_shipped, 0);

        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        let client = make_test_client(&url);
        let ready = replay_ready_spool_for_path_with_batch_bytes_and_parse_tracker(
            &conn,
            &client,
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            None,
            None,
            None,
            None,
        )
        .await
        .unwrap();
        handle.join().unwrap();

        assert_eq!(ready.resolved, 1);
        assert_eq!(ready.failed, 0);
        assert_eq!(ready.events_shipped, 2);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), file_len);
        assert!(spool
            .pending_entries_for_path_now(&path_str, 10)
            .unwrap()
            .is_empty());
    }

    #[tokio::test]
    async fn test_replay_spool_for_path_now_preserves_spooled_session_id() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("dddddddd-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&path).unwrap().len();
        let override_session_id = "019d2869-1111-7222-8333-aaaaaaaaaaaa";

        file_state
            .set_queued_offset(
                &path_str,
                file_len,
                "claude",
                override_session_id,
                "dddddddd-1111-2222-3333-444455556666",
            )
            .unwrap();
        spool
            .enqueue("claude", &path_str, 0, file_len, Some(override_session_id))
            .unwrap();

        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);
        let outcome = replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
            &conn,
            &client,
            CompressionAlgo::Gzip,
            &path,
            10,
            10_000,
            None,
            None,
        )
        .await
        .unwrap();
        handle.join().unwrap();

        assert_eq!(outcome.resolved, 1);
        assert_eq!(outcome.events_shipped, 2);
        let requests = captured.lock().unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(decode_payload_session_id(&requests[0]), override_session_id);
    }

    #[test]
    fn test_ship_prepared_file_spools_remaining_tail_after_midstream_failure() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("shiptail1111-2222-3333-4444-555566667777.jsonl");
        let lines = vec![
            make_line("tail-1", &"x".repeat(64)),
            make_line("tail-2", &"y".repeat(64)),
            make_line("tail-3", &"z".repeat(64)),
            make_line("tail-4", &"q".repeat(64)),
            make_line("tail-5", &"r".repeat(64)),
            make_line("tail-6", &"s".repeat(64)),
        ];
        std::fs::write(
            &path,
            format!(
                "{}\n{}\n{}\n{}\n{}\n{}\n",
                lines[0], lines[1], lines[2], lines[3], lines[4], lines[5]
            ),
        )
        .unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 800, None).unwrap();
        let prepared = prepared.expect("file should prepare");
        assert!(
            prepared.actions.len() >= 2,
            "test requires multiple prepared ship actions"
        );
        let ship_offsets: Vec<(u64, u64)> = prepared
            .actions
            .iter()
            .filter_map(|action| match action {
                PreparedAction::Ship(item) => Some((item.offset, item.new_offset)),
                PreparedAction::DeadLetter(_) => None,
                PreparedAction::AckOnly(_) => None,
            })
            .collect();
        assert!(
            ship_offsets.len() >= 2,
            "test requires at least two ship batches after any dead letters"
        );
        let first_batch_end = ship_offsets[0].1;
        let second_batch_start = ship_offsets[1].0;
        let final_end = prepared.new_offset;

        let (url, _captured, handle) =
            spawn_http_sequence_server(&[("200 OK", "{}"), ("500 Internal Server Error", "oops")]);
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let outcome = rt
            .block_on(ship_prepared_file(prepared, &client, &conn, None, None))
            .unwrap();
        handle.join().unwrap();

        assert!(!outcome.fully_processed);
        assert!(outcome.events_shipped > 0);

        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let path_str = path.to_string_lossy().to_string();
        assert_eq!(fs.get_offset(&path_str).unwrap(), first_batch_end);
        assert_eq!(fs.get_queued_offset(&path_str).unwrap(), final_end);

        let pending = spool.dequeue_batch(10).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].start_offset, second_batch_start);
        assert_eq!(pending[0].end_offset, final_end);
    }

    #[test]
    fn test_ship_prepared_file_dead_letters_oversize_range_and_keeps_tail_moving() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("deadletter1111-2222-3333-4444-555566667777.jsonl");
        let huge = make_line("dead-1", &"h".repeat(800));
        let tail = make_line("dead-2", "small-tail");
        std::fs::write(&path, format!("{}\n{}\n", huge, tail)).unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 600, None).unwrap();
        let prepared = prepared.expect("file should prepare with dead-letter + tail");
        let ship_batches = prepared
            .actions
            .iter()
            .filter(|action| matches!(action, PreparedAction::Ship(_)))
            .count();
        assert_eq!(
            ship_batches, 1,
            "expected only the small tail to remain shippable"
        );

        let (url, _captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let outcome = rt
            .block_on(ship_prepared_file(prepared, &client, &conn, None, None))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(outcome.dead_lettered, 1);
        assert_eq!(outcome.events_shipped, 1);
        assert!(outcome.fully_processed);

        let path_str = path.to_string_lossy().to_string();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let file_end = std::fs::metadata(&path).unwrap().len();
        assert_eq!(fs.get_offset(&path_str).unwrap(), file_end);

        let status: String = conn
            .query_row(
                "SELECT status FROM spool_queue WHERE file_path = ?1 ORDER BY id ASC LIMIT 1",
                [&path_str],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "dead");
        assert_eq!(spool.total_size().unwrap(), 1);
    }

    #[tokio::test]
    async fn test_ship_prepared_file_retryable_client_error_spools_batch() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("retryable-client-error-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap()
                .expect("file should prepare");
        let file_len = std::fs::metadata(&path).unwrap().len();
        let path_str = path.to_string_lossy().to_string();

        let (url, _captured, handle) =
            spawn_http_sequence_server(&[("401 Unauthorized", "{\"detail\":\"bad token\"}")]);
        let client = make_test_client(&url);

        let outcome = ship_prepared_file(prepared, &client, &conn, None, None)
            .await
            .unwrap();
        handle.join().unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        assert_eq!(outcome.dead_lettered, 0);
        assert_eq!(outcome.events_shipped, 0);
        assert!(!outcome.fully_processed);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), file_len);
        assert_eq!(spool.pending_count().unwrap(), 1);
        assert_eq!(spool.dead_count().unwrap(), 0);
    }

    #[tokio::test]
    async fn test_ship_prepared_file_payload_rejection_dead_letters_batch() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("payload-rejected-1111-2222-3333-444455556666.jsonl");
        std::fs::write(&path, claude_session_lines()).unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap()
                .expect("file should prepare");
        let file_len = std::fs::metadata(&path).unwrap().len();
        let path_str = path.to_string_lossy().to_string();

        let (url, _captured, handle) = spawn_http_sequence_server(&[(
            "422 Unprocessable Entity",
            "{\"detail\":\"Invalid payload: missing field\"}",
        )]);
        let client = make_test_client(&url);

        let outcome = ship_prepared_file(prepared, &client, &conn, None, None)
            .await
            .unwrap();
        handle.join().unwrap();

        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        assert_eq!(outcome.dead_lettered, 1);
        assert_eq!(outcome.events_shipped, 0);
        assert!(outcome.fully_processed);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), file_len);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), file_len);
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(spool.dead_count().unwrap(), 1);
    }

    // ---------------------------------------------------------------
    // Startup recovery enqueues gaps correctly
    // ---------------------------------------------------------------

    #[test]
    fn test_startup_recovery_enqueues_unacked() {
        let (_tmp, conn) = make_db();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);

        // Simulate a file that was queued but not acked (daemon crashed mid-ship)
        fs.set_offset("/tmp/test.jsonl", 100, "sess-1", "sess-1", "claude")
            .unwrap();
        fs.set_queued_offset("/tmp/test.jsonl", 500, "claude", "sess-1", "sess-1")
            .unwrap();

        // Run recovery
        let count = run_startup_recovery(&conn).unwrap();
        assert_eq!(count, 1, "Should find 1 unacked file");

        // Check spool has the entry
        let pending = spool.dequeue_batch(10).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].file_path, "/tmp/test.jsonl");
        assert_eq!(pending[0].start_offset, 100);
        assert_eq!(pending[0].end_offset, 500);
    }

    #[test]
    fn test_startup_recovery_is_idempotent_when_gap_already_pending() {
        let (_tmp, conn) = make_db();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);

        fs.set_offset("/tmp/test.jsonl", 100, "sess-1", "sess-1", "claude")
            .unwrap();
        fs.set_queued_offset("/tmp/test.jsonl", 500, "claude", "sess-1", "sess-1")
            .unwrap();
        spool
            .enqueue("claude", "/tmp/test.jsonl", 100, 500, Some("sess-1"))
            .unwrap();

        let count = run_startup_recovery(&conn).unwrap();
        assert_eq!(count, 0, "existing pending gap should not be duplicated");

        let pending_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM spool_queue WHERE status = 'pending' AND file_path = '/tmp/test.jsonl'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(pending_count, 1);
    }

    #[test]
    fn test_recover_gap_for_path_enqueues_only_target_gap() {
        let (_tmp, conn) = make_db();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);

        fs.set_offset("/tmp/target.jsonl", 100, "target", "target", "claude")
            .unwrap();
        fs.set_queued_offset("/tmp/target.jsonl", 500, "claude", "target", "target")
            .unwrap();
        fs.set_offset("/tmp/other.jsonl", 20, "other", "other", "claude")
            .unwrap();
        fs.set_queued_offset("/tmp/other.jsonl", 90, "claude", "other", "other")
            .unwrap();

        let recovered =
            recover_gap_for_path(&conn, std::path::Path::new("/tmp/target.jsonl")).unwrap();
        assert!(recovered.had_gap);
        assert!(recovered.replay_ready);

        let target_pending = spool
            .pending_entries_for_path_now("/tmp/target.jsonl", 10)
            .unwrap();
        let other_pending = spool
            .pending_entries_for_path_now("/tmp/other.jsonl", 10)
            .unwrap();
        assert_eq!(target_pending.len(), 1);
        assert!(other_pending.is_empty());
        assert_eq!(target_pending[0].start_offset, 100);
        assert_eq!(target_pending[0].end_offset, 500);
    }

    #[test]
    fn test_recover_gap_for_path_reports_spool_backpressure() {
        let (_tmp, conn) = make_db();
        let fs = FileState::new(&conn);

        conn.execute_batch(
            "WITH RECURSIVE cnt(x) AS (
                SELECT 0
                UNION ALL
                SELECT x + 1 FROM cnt WHERE x < 9999
            )
            INSERT INTO spool_queue (
                provider,
                file_path,
                start_offset,
                end_offset,
                created_at,
                next_retry_at,
                status
            )
            SELECT
                'claude',
                '/tmp/fill-' || x || '.jsonl',
                x,
                x + 1,
                datetime('now'),
                datetime('now'),
                'pending'
            FROM cnt;",
        )
        .unwrap();

        fs.set_offset("/tmp/target.jsonl", 100, "target", "target", "claude")
            .unwrap();
        fs.set_queued_offset("/tmp/target.jsonl", 500, "claude", "target", "target")
            .unwrap();

        let recovered =
            recover_gap_for_path(&conn, std::path::Path::new("/tmp/target.jsonl")).unwrap();
        assert!(recovered.had_gap);
        assert!(!recovered.replay_ready);
    }

    #[test]
    fn test_prepare_file_batches_skips_when_unacked_gap_exists() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("queuedgap1111-2222-3333-4444-555566667777.jsonl");
        let line1 = make_line("gap-1", "one");
        let line2 = make_line("gap-2", "two");
        let line3 = make_line("gap-3", "three-newer");
        std::fs::write(&path, format!("{}\n{}\n{}\n", line1, line2, line3)).unwrap();

        let path_str = path.to_string_lossy().to_string();
        let line1_end = (line1.len() + 1) as u64;
        let line2_end = (line1.len() + 1 + line2.len() + 1) as u64;
        let file_end = std::fs::metadata(&path).unwrap().len();
        assert!(
            file_end > line2_end,
            "test requires newer bytes beyond queued gap"
        );

        let fs = FileState::new(&conn);
        fs.set_offset(
            &path_str,
            line1_end,
            "queuedgap1111-2222-3333-4444-555566667777",
            "queuedgap1111-2222-3333-4444-555566667777",
            "claude",
        )
        .unwrap();
        fs.set_queued_offset(
            &path_str,
            line2_end,
            "claude",
            "queuedgap1111-2222-3333-4444-555566667777",
            "queuedgap1111-2222-3333-4444-555566667777",
        )
        .unwrap();

        let prepared =
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000, None)
                .unwrap();
        assert!(
            prepared.is_none(),
            "fresh shipping must stop while an earlier queued gap is still unacked"
        );
    }

    // ---------------------------------------------------------------
    // Backpressure: spool full → queued_offset not advanced
    // ---------------------------------------------------------------

    #[test]
    fn test_spool_backpressure_does_not_advance_offset() {
        use crate::state::db::open_db;

        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);

        // Record acked_offset = 0 for the file
        fs.set_offset("/bp/test.jsonl", 0, "sess-bp", "sess-bp", "claude")
            .unwrap();

        // Fill spool to capacity by inserting MAX_SPOOL_ENTRIES rows directly
        // Use a very large start_offset so they won't be the same as our test entry
        for i in 0..10_000usize {
            conn.execute(
                "INSERT INTO spool_queue (provider, file_path, start_offset, end_offset, created_at, next_retry_at, status)
                 VALUES ('claude', '/filler', ?1, ?2, datetime('now'), datetime('now'), 'pending')",
                rusqlite::params![i as i64 * 1000, (i + 1) as i64 * 1000],
            ).unwrap();
        }

        // Now enqueue should fail (spool full) for a new entry
        let enqueued = spool
            .enqueue("claude", "/bp/test.jsonl", 0, 100, Some("sess-bp"))
            .unwrap();
        assert!(!enqueued, "Spool should be full");

        // queued_offset must remain at 0 (not advanced to 100)
        let qoff = fs.get_queued_offset("/bp/test.jsonl").unwrap();
        assert_eq!(qoff, 0, "queued_offset must not advance when spool is full");
    }

    #[test]
    fn test_replay_413_without_split_boundary_stays_pending_with_backoff() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("99991111-2222-3333-4444-555566667777.jsonl");
        std::fs::write(&path, format!("{}\n", make_line("413-one-line", "only"))).unwrap();
        let path_str = path.to_string_lossy().to_string();
        let file_size = std::fs::metadata(&path).unwrap().len();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            file_size,
            "claude",
            "99991111-2222-3333-4444-555566667777",
            "99991111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_size,
                Some("99991111-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let (url, handle) = spawn_http_response_server("413 Payload Too Large", "too large");
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let (shipped, failed) = rt
            .block_on(replay_spool_batch(
                &conn,
                &client,
                CompressionAlgo::Gzip,
                10,
            ))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(shipped, 0);
        assert_eq!(failed, 1);
        assert_eq!(fs.get_offset(&path_str).unwrap(), 0);

        let (status, retry_count, last_error): (String, i64, String) = conn
            .query_row(
                "SELECT status, retry_count, last_error FROM spool_queue WHERE file_path = ?1",
                [&path_str],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(status, "pending");
        assert_eq!(retry_count, 1);
        assert!(last_error.contains("413"));
    }

    #[test]
    fn test_replay_413_splits_ready_child_ranges_at_source_line_boundary() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("split4131111-2222-3333-4444-555566667777.jsonl");
        let lines = vec![
            make_line("split-1", &"a".repeat(64)),
            make_line("split-2", &"b".repeat(64)),
            make_line("split-3", &"c".repeat(64)),
            make_line("split-4", &"d".repeat(64)),
            make_line("split-5", &"e".repeat(64)),
            make_line("split-6", &"f".repeat(64)),
        ];
        std::fs::write(
            &path,
            format!(
                "{}\n{}\n{}\n{}\n{}\n{}\n",
                lines[0], lines[1], lines[2], lines[3], lines[4], lines[5]
            ),
        )
        .unwrap();

        let full_end = std::fs::metadata(&path).unwrap().len();
        let path_str = path.to_string_lossy().to_string();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            full_end,
            "claude",
            "split4131111-2222-3333-4444-555566667777",
            "split4131111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                full_end,
                Some("split4131111-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let prepared = prepare_path_range(
            &path,
            "claude",
            0,
            Some(full_end),
            CompressionAlgo::Gzip,
            800,
            None,
        )
        .unwrap()
        .expect("replay range should prepare into multiple batches");
        let ship_items: Vec<&ShipItem> = prepared
            .actions
            .iter()
            .filter_map(|action| match action {
                PreparedAction::Ship(item) => Some(item),
                PreparedAction::DeadLetter(_) | PreparedAction::AckOnly(_) => None,
            })
            .collect();
        let failed_index = ship_items
            .iter()
            .position(|item| replay_split_offset_for_payload_too_large(item).is_some())
            .expect("prepared replay should include at least one splittable ship batch");
        let progress_offset = if failed_index == 0 {
            0
        } else {
            ship_items[failed_index - 1].new_offset
        };
        let failed_item = ship_items[failed_index];
        let split_offset = replay_split_offset_for_payload_too_large(failed_item)
            .expect("failed batch should have an interior source-line split point");

        let mut responses = vec![("200 OK", "{}"); failed_index];
        responses.push(("413 Payload Too Large", "too large"));
        let (url, _captured, handle) = spawn_http_sequence_server(&responses);
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let (shipped, failed) = rt
            .block_on(replay_spool_batch_with_batch_bytes(
                &conn,
                &client,
                CompressionAlgo::Gzip,
                10,
                800,
            ))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(shipped, 0);
        assert_eq!(failed, 1);
        assert_eq!(fs.get_offset(&path_str).unwrap(), progress_offset);

        let rows: Vec<(i64, i64, i64, String)> = {
            let mut stmt = conn
                .prepare(
                    "SELECT start_offset, end_offset, retry_count, status
                     FROM spool_queue
                     WHERE file_path = ?1
                     ORDER BY start_offset ASC",
                )
                .unwrap();
            stmt.query_map([&path_str], |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
            })
            .unwrap()
            .collect::<std::result::Result<Vec<_>, _>>()
            .unwrap()
        };

        let mut expected = vec![
            (
                failed_item.offset as i64,
                split_offset as i64,
                0,
                "pending".to_string(),
            ),
            (
                split_offset as i64,
                failed_item.new_offset as i64,
                0,
                "pending".to_string(),
            ),
        ];
        if failed_item.new_offset < full_end {
            expected.push((
                failed_item.new_offset as i64,
                full_end as i64,
                0,
                "pending".to_string(),
            ));
        }
        assert_eq!(rows, expected);
        assert_eq!(spool.dequeue_batch(10).unwrap().len(), expected.len());
    }

    #[test]
    fn test_replay_413_cools_archive_batch_target() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            claude_session_lines(),
            "99992222-2222-3333-4444-555566667777.jsonl",
        );
        let path = dir
            .path()
            .join("99992222-2222-3333-4444-555566667777.jsonl");
        let path_str = path.to_string_lossy().to_string();
        let file_size = std::fs::metadata(&path).unwrap().len();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            file_size,
            "claude",
            "99992222-2222-3333-4444-555566667777",
            "99992222-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_size,
                Some("99992222-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let limiter = crate::scheduler::AdaptiveLimiter::new();
        let (url, handle) = spawn_http_response_server("413 Payload Too Large", "too large");
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let outcome = rt
            .block_on(replay_spool_for_path_with_batch_bytes_and_parse_tracker(
                &conn,
                &client,
                CompressionAlgo::Gzip,
                &path,
                10,
                u64::MAX,
                None,
                None,
                None,
                None,
                Some(limiter.as_ref()),
            ))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(outcome.resolved, 0);
        assert_eq!(outcome.failed, 1);
        let snapshot = limiter.snapshot();
        assert_eq!(
            snapshot.archive_target_batch_bytes,
            crate::scheduler::ARCHIVE_BATCH_TARGET_MIN_BYTES
        );
        assert_eq!(snapshot.total_backpressure, 1);
    }

    #[test]
    fn test_replay_success_acks_only_complete_bytes() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("88881111-2222-3333-4444-555566667777.jsonl");
        let complete = r#"{"type":"user","uuid":"replay-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"complete"}}"#;
        let partial = r#"{"type":"assistant","uuid":"replay-2","timestamp":"2026-02-15T10:00:01Z","message":{"con"#;
        std::fs::write(&path, format!("{}\n{}", complete, partial)).unwrap();

        let path_str = path.to_string_lossy().to_string();
        let file_size = std::fs::metadata(&path).unwrap().len();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            file_size,
            "claude",
            "88881111-2222-3333-4444-555566667777",
            "88881111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                file_size,
                Some("88881111-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let (shipped, failed) = rt
            .block_on(replay_spool_batch(
                &conn,
                &client,
                CompressionAlgo::Gzip,
                10,
            ))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(shipped, 0);
        assert_eq!(failed, 0);
        assert_eq!(
            fs.get_offset(&path_str).unwrap(),
            (complete.len() + 1) as u64
        );
        assert_eq!(fs.get_queued_offset(&path_str).unwrap(), file_size);
        let row: (i64, i64, String) = conn
            .query_row(
                "SELECT start_offset, end_offset, status FROM spool_queue WHERE file_path = ?1",
                [&path_str],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row.0 as u64, (complete.len() + 1) as u64);
        assert_eq!(row.1 as u64, file_size);
        assert_eq!(row.2, "pending");
    }

    #[test]
    fn test_replay_ships_only_exact_spooled_range_not_newer_bytes() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replaycap1111-2222-3333-4444-555566667777.jsonl");
        let line1 = make_line("cap-1", "one");
        let line2 = make_line("cap-2", "two");
        let line3 = make_line("cap-3", "three-newer");
        std::fs::write(&path, format!("{}\n{}\n{}\n", line1, line2, line3)).unwrap();

        let line2_offset = (line1.len() + 1) as u64;
        let line3_offset = (line1.len() + 1 + line2.len() + 1) as u64;
        let replay_end = line3_offset;
        let path_str = path.to_string_lossy().to_string();

        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            replay_end,
            "claude",
            "replaycap1111-2222-3333-4444-555566667777",
            "replaycap1111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                replay_end,
                Some("replaycap1111-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let (url, captured, handle) = spawn_http_sequence_server(&[("200 OK", "{}")]);
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let (shipped, failed) = rt
            .block_on(replay_spool_batch_with_batch_bytes(
                &conn,
                &client,
                CompressionAlgo::Gzip,
                10,
                10_000,
            ))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(shipped, 1);
        assert_eq!(failed, 0);

        let bodies = captured.lock().unwrap().clone();
        assert_eq!(bodies.len(), 1);
        let offsets = decode_payload_source_offsets(&bodies[0]);
        assert_eq!(offsets, vec![0, line2_offset]);
        assert!(!offsets.contains(&line3_offset));
    }

    #[test]
    fn test_replay_advances_spool_entry_after_partial_success() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("replayprogress1111-2222-3333-4444-555566667777.jsonl");
        let lines = vec![
            make_line("progress-1", &"a".repeat(64)),
            make_line("progress-2", &"b".repeat(64)),
            make_line("progress-3", &"c".repeat(64)),
            make_line("progress-4", &"d".repeat(64)),
            make_line("progress-5", &"e".repeat(64)),
            make_line("progress-6", &"f".repeat(64)),
        ];
        std::fs::write(
            &path,
            format!(
                "{}\n{}\n{}\n{}\n{}\n{}\n",
                lines[0], lines[1], lines[2], lines[3], lines[4], lines[5]
            ),
        )
        .unwrap();

        let full_end = std::fs::metadata(&path).unwrap().len();
        let path_str = path.to_string_lossy().to_string();
        let fs = FileState::new(&conn);
        let spool = Spool::new(&conn);
        fs.set_queued_offset(
            &path_str,
            full_end,
            "claude",
            "replayprogress1111-2222-3333-4444-555566667777",
            "replayprogress1111-2222-3333-4444-555566667777",
        )
        .unwrap();
        spool
            .enqueue(
                "claude",
                &path_str,
                0,
                full_end,
                Some("replayprogress1111-2222-3333-4444-555566667777"),
            )
            .unwrap();

        let prepared = prepare_path_range(
            &path,
            "claude",
            0,
            Some(full_end),
            CompressionAlgo::Gzip,
            800,
            None,
        )
        .unwrap()
        .expect("replay range should prepare into multiple batches");
        let ship_offsets: Vec<(u64, u64)> = prepared
            .actions
            .iter()
            .filter_map(|action| match action {
                PreparedAction::Ship(item) => Some((item.offset, item.new_offset)),
                PreparedAction::DeadLetter(_) => None,
                PreparedAction::AckOnly(_) => None,
            })
            .collect();
        assert!(
            ship_offsets.len() >= 2,
            "replay progress test requires at least two ship batches"
        );
        let first_batch_end = ship_offsets[0].1;
        let second_batch_start = ship_offsets[1].0;

        let (url, _captured, handle) =
            spawn_http_sequence_server(&[("200 OK", "{}"), ("500 Internal Server Error", "oops")]);
        let client = make_test_client(&url);
        let rt = tokio::runtime::Runtime::new().unwrap();

        let (shipped, failed) = rt
            .block_on(replay_spool_batch_with_batch_bytes(
                &conn,
                &client,
                CompressionAlgo::Gzip,
                10,
                800,
            ))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(shipped, 0);
        assert_eq!(failed, 1);
        assert_eq!(fs.get_offset(&path_str).unwrap(), first_batch_end);

        let row: (i64, i64, i64, String) = conn
            .query_row(
                "SELECT start_offset, end_offset, retry_count, status FROM spool_queue WHERE file_path = ?1",
                [&path_str],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(row.0 as u64, second_batch_start);
        assert_eq!(row.1 as u64, full_end);
        assert_eq!(row.2, 1);
        assert_eq!(row.3, "pending");
    }

    #[test]
    fn test_ship_trace_value_excludes_payload_content() {
        let item = ShipItem {
            path_str: "/tmp/session.jsonl".to_string(),
            provider: "codex".to_string(),
            offset: 10,
            new_offset: 20,
            event_count: 2,
            session_id: "session-1".to_string(),
            source_line_offsets: Vec::new(),
            source_line_refs: Vec::new(),
            media_objects: Vec::new(),
            compressed: b"secret transcript payload".to_vec(),
        };

        let value = build_ship_trace_value(&item, None, 1234);
        let encoded = serde_json::to_string(&value).unwrap();

        assert!(encoded.contains("ship_trace.v1"));
        assert!(!encoded.contains("secret transcript payload"));
        assert_eq!(value.get("range_bytes").and_then(Value::as_u64), Some(10));
    }

    #[test]
    fn test_ship_prepared_file_uploads_media_before_ingest() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let image_bytes = vec![9u8; 1024 * 1024];
        let image_data = general_purpose::STANDARD.encode(&image_bytes);
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = dir
            .path()
            .join("019c638d-0000-0000-0000-000000000555.jsonl");
        std::fs::write(&path, format!("{line}\n")).unwrap();

        let prepared = prepare_file_batches(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            10_000_000,
            None,
        )
        .unwrap()
        .expect("prepared media ship item");
        let (media, source_line_ref) = match &prepared.actions[0] {
            PreparedAction::Ship(item) => {
                assert_eq!(item.media_objects.len(), 1);
                assert_eq!(item.source_line_refs.len(), 1);
                (
                    item.media_objects[0].clone(),
                    item.source_line_refs[0].clone(),
                )
            }
            _ => panic!("expected ship action"),
        };
        let source_claim_missing = json!({
            "present": [],
            "missing": [{
                "source_path": path.to_string_lossy(),
                "source_offset": source_line_ref.source_offset,
                "line_hash": source_line_ref.line_hash,
            }],
            "rejected": [],
        })
        .to_string();
        let claim_body = format!(
            r#"{{"needed":["{}"],"present":[],"rejected":[]}}"#,
            media.sha256
        );
        let (url, captured, handle) = spawn_http_sequence_server(&[
            ("200 OK", &source_claim_missing),
            ("200 OK", &claim_body),
            ("200 OK", "{}"),
            ("200 OK", "{}"),
        ]);
        let client = make_test_client(&url);

        let outcome = rt
            .block_on(ship_prepared_file(prepared, &client, &conn, None, None))
            .unwrap();
        handle.join().unwrap();
        assert_eq!(outcome.events_shipped, 1);
        assert!(outcome.fully_processed);

        let bodies = captured.lock().unwrap();
        assert_eq!(bodies.len(), 4);
        let claim: serde_json::Value = serde_json::from_slice(&bodies[1]).unwrap();
        assert_eq!(claim["items"][0]["sha256"], media.sha256);
        assert_eq!(claim["items"][0]["mime_type"], "image/png");
        assert_eq!(claim["items"][0]["source_offset"], 0);
        assert_eq!(claim["items"][0]["provider"], "codex");
        assert_eq!(bodies[2], image_bytes);
        assert_eq!(bodies[2], media.bytes);

        let mut decoder = GzDecoder::new(&bodies[3][..]);
        let mut ingest_json = String::new();
        decoder.read_to_string(&mut ingest_json).unwrap();
        assert!(ingest_json.contains("longhouse_media_ref:sha256="));
        assert!(!ingest_json.contains(&image_data));
        assert!(
            ingest_json.len() < 8_000,
            "redacted Codex ingest body should stay small, got {} bytes",
            ingest_json.len()
        );
    }

    #[test]
    fn test_media_upload_failure_leaves_archive_cursor_unchanged() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let image_data = "A".repeat(4096);
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = dir
            .path()
            .join("019c638d-0000-0000-0000-000000000556.jsonl");
        std::fs::write(&path, format!("{line}\n")).unwrap();
        let path_str = path.to_string_lossy().to_string();

        let prepared = prepare_file_batches(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            512 * 1024,
            None,
        )
        .unwrap()
        .expect("prepared media ship item");
        let media = match &prepared.actions[0] {
            PreparedAction::Ship(item) => item.media_objects[0].clone(),
            _ => panic!("expected ship action"),
        };
        let claim_body = format!(
            r#"{{"needed":["{}"],"present":[],"rejected":[]}}"#,
            media.sha256
        );
        let (url, captured, handle) = spawn_http_sequence_server(&[
            ("200 OK", &claim_body),
            ("500 Internal Server Error", "upload down"),
        ]);
        let client = make_test_client(&url);

        let outcome = rt
            .block_on(ship_prepared_file(prepared, &client, &conn, None, None))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(captured.lock().unwrap().len(), 2);
        assert_eq!(outcome.events_shipped, 0);
        assert!(!outcome.fully_processed);
        let file_state = FileState::new(&conn);
        assert_eq!(file_state.get_offset(&path_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&path_str).unwrap(), 0);
    }

    #[test]
    fn test_archive_replay_reconciles_already_durable_source_lines_without_reupload() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let image_data = "A".repeat(4096);
        let session_id = "019c638d-0000-0000-0000-000000000558";
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = dir.path().join(format!("{session_id}.jsonl"));
        std::fs::write(&path, format!("{line}\n")).unwrap();
        let path_str = path.to_string_lossy().to_string();

        let prepared = prepare_file_batches(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            512 * 1024,
            None,
        )
        .unwrap()
        .expect("prepared media ship item");
        let (source_line_ref, media) = match &prepared.actions[0] {
            PreparedAction::Ship(item) => {
                assert_eq!(item.source_line_refs.len(), 1);
                assert_eq!(item.media_objects.len(), 1);
                (
                    item.source_line_refs[0].clone(),
                    item.media_objects[0].clone(),
                )
            }
            _ => panic!("expected ship action"),
        };
        let end_offset = prepared.new_offset;
        let spool = Spool::new(&conn);
        spool
            .enqueue("codex", &path_str, 0, end_offset, Some(session_id))
            .unwrap();
        let claim_body = json!({
            "present": [{
                "source_path": path_str,
                "source_offset": source_line_ref.source_offset,
                "line_hash": source_line_ref.line_hash,
            }],
            "missing": [],
            "rejected": [],
        })
        .to_string();
        let media_claim_body = json!({
            "needed": [],
            "present": [media.sha256],
            "rejected": [],
        })
        .to_string();
        let (url, captured, handle) =
            spawn_http_sequence_server(&[("200 OK", &claim_body), ("200 OK", &media_claim_body)]);
        let client = make_test_client(&url);

        let replay = rt
            .block_on(
                replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
                    &conn,
                    &client,
                    CompressionAlgo::Gzip,
                    &path,
                    10,
                    512 * 1024,
                    None,
                    None,
                ),
            )
            .unwrap();
        handle.join().unwrap();

        assert_eq!(captured.lock().unwrap().len(), 2);
        assert_eq!(replay.resolved, 1);
        assert_eq!(replay.failed, 0);
        assert_eq!(replay.events_shipped, 0);
        assert_eq!(spool.pending_count().unwrap(), 0);
        assert_eq!(FileState::new(&conn).get_offset(&path_str).unwrap(), 0);
    }

    #[test]
    fn test_archive_ship_reconciles_already_durable_source_lines_without_reupload() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let image_data = "A".repeat(4096);
        let session_id = "019c638d-0000-0000-0000-000000000559";
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = dir.path().join(format!("{session_id}.jsonl"));
        std::fs::write(&path, format!("{line}\n")).unwrap();
        let path_str = path.to_string_lossy().to_string();

        let prepared = prepare_file_batches(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            512 * 1024,
            None,
        )
        .unwrap()
        .expect("prepared media ship item");
        let (source_line_ref, media) = match &prepared.actions[0] {
            PreparedAction::Ship(item) => (
                item.source_line_refs[0].clone(),
                item.media_objects[0].clone(),
            ),
            _ => panic!("expected ship action"),
        };
        let end_offset = prepared.new_offset;
        let claim_body = json!({
            "present": [{
                "source_path": path_str,
                "source_offset": source_line_ref.source_offset,
                "line_hash": source_line_ref.line_hash,
            }],
            "missing": [],
            "rejected": [],
        })
        .to_string();
        let media_claim_body = json!({
            "needed": [],
            "present": [media.sha256],
            "rejected": [],
        })
        .to_string();
        let (url, captured, handle) =
            spawn_http_sequence_server(&[("200 OK", &claim_body), ("200 OK", &media_claim_body)]);
        let client = make_test_client(&url);

        let outcome = rt
            .block_on(ship_prepared_file(prepared, &client, &conn, None, None))
            .unwrap();
        handle.join().unwrap();

        assert_eq!(captured.lock().unwrap().len(), 2);
        assert_eq!(outcome.events_shipped, 0);
        assert!(outcome.fully_processed);
        assert_eq!(
            FileState::new(&conn).get_offset(&path_str).unwrap(),
            end_offset
        );
    }

    #[test]
    fn test_archive_replay_missing_or_unavailable_source_line_claim_falls_through() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let image_data = "A".repeat(4096);
        let session_id = "019c638d-0000-0000-0000-000000000560";
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = dir.path().join(format!("{session_id}.jsonl"));
        std::fs::write(&path, format!("{line}\n")).unwrap();
        let path_str = path.to_string_lossy().to_string();

        let prepared = prepare_file_batches(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            512 * 1024,
            None,
        )
        .unwrap()
        .expect("prepared media ship item");
        let (source_line_ref, media) = match &prepared.actions[0] {
            PreparedAction::Ship(item) => (
                item.source_line_refs[0].clone(),
                item.media_objects[0].clone(),
            ),
            _ => panic!("expected ship action"),
        };
        let end_offset = prepared.new_offset;
        let spool = Spool::new(&conn);
        spool
            .enqueue("codex", &path_str, 0, end_offset, Some(session_id))
            .unwrap();
        let source_claim_missing = json!({
            "present": [],
            "missing": [{
                "source_path": path_str,
                "source_offset": source_line_ref.source_offset,
                "line_hash": source_line_ref.line_hash,
            }],
            "rejected": [],
        })
        .to_string();
        let media_claim_present = format!(
            r#"{{"needed":[],"present":["{}"],"rejected":[]}}"#,
            media.sha256
        );
        let (url, captured, handle) = spawn_http_sequence_server(&[
            ("200 OK", &source_claim_missing),
            ("200 OK", &media_claim_present),
            ("200 OK", "{}"),
        ]);
        let client = make_test_client(&url);

        let replay = rt
            .block_on(
                replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
                    &conn,
                    &client,
                    CompressionAlgo::Gzip,
                    &path,
                    10,
                    512 * 1024,
                    None,
                    None,
                ),
            )
            .unwrap();
        handle.join().unwrap();

        assert_eq!(captured.lock().unwrap().len(), 3);
        assert_eq!(replay.resolved, 1);
        assert_eq!(replay.failed, 0);
        assert_eq!(replay.events_shipped, 1);
        assert_eq!(spool.pending_count().unwrap(), 0);

        let fallback_session_id = "019c638d-0000-0000-0000-000000000561";
        let path_fallback = dir.path().join(format!("{fallback_session_id}.jsonl"));
        std::fs::write(&path_fallback, format!("{line}\n")).unwrap();
        let fallback_path_str = path_fallback.to_string_lossy().to_string();
        let fallback_prepared = prepare_file_batches(
            &path_fallback,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            512 * 1024,
            None,
        )
        .unwrap()
        .expect("prepared fallback media ship item");
        let fallback_end_offset = fallback_prepared.new_offset;
        spool
            .enqueue(
                "codex",
                &fallback_path_str,
                0,
                fallback_end_offset,
                Some(fallback_session_id),
            )
            .unwrap();
        let fallback_media = match &fallback_prepared.actions[0] {
            PreparedAction::Ship(item) => item.media_objects[0].clone(),
            _ => panic!("expected ship action"),
        };
        let fallback_media_claim_present = format!(
            r#"{{"needed":[],"present":["{}"],"rejected":[]}}"#,
            fallback_media.sha256
        );
        let (fallback_url, fallback_captured, fallback_handle) = spawn_http_sequence_server(&[
            ("404 Not Found", r#"{"detail":"not found"}"#),
            ("200 OK", &fallback_media_claim_present),
            ("200 OK", "{}"),
        ]);
        let fallback_client = make_test_client(&fallback_url);

        let fallback_replay = rt
            .block_on(
                replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
                    &conn,
                    &fallback_client,
                    CompressionAlgo::Gzip,
                    &path_fallback,
                    10,
                    512 * 1024,
                    None,
                    None,
                ),
            )
            .unwrap();
        fallback_handle.join().unwrap();

        assert_eq!(fallback_captured.lock().unwrap().len(), 3);
        assert_eq!(fallback_replay.resolved, 1);
        assert_eq!(fallback_replay.failed, 0);
        assert_eq!(fallback_replay.events_shipped, 1);
    }

    #[test]
    fn test_replay_media_upload_failure_defers_without_dead_letter_retry() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let image_data = "A".repeat(4096);
        let session_id = "019c638d-0000-0000-0000-000000000557";
        let line = format!(
            r#"{{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{{"type":"message","role":"user","content":[{{"type":"input_image","image_url":"data:image/png;base64,{image_data}"}}]}}}}"#
        );
        let path = dir.path().join(format!("{session_id}.jsonl"));
        std::fs::write(&path, format!("{line}\n")).unwrap();
        let path_str = path.to_string_lossy().to_string();

        let prepared = prepare_file_batches(
            &path,
            "codex",
            CompressionAlgo::Gzip,
            &conn,
            512 * 1024,
            None,
        )
        .unwrap()
        .expect("prepared media ship item");
        let media = match &prepared.actions[0] {
            PreparedAction::Ship(item) => item.media_objects[0].clone(),
            _ => panic!("expected ship action"),
        };
        let end_offset = prepared.new_offset;
        let claim_body = format!(
            r#"{{"needed":["{}"],"present":[],"rejected":[]}}"#,
            media.sha256
        );
        let spool = Spool::new(&conn);
        spool
            .enqueue("codex", &path_str, 0, end_offset, Some(session_id))
            .unwrap();
        let entry_id = spool.pending_entries_for_path(&path_str, 1).unwrap()[0].id;
        let (url, captured, handle) = spawn_http_sequence_server(&[
            ("200 OK", &claim_body),
            ("500 Internal Server Error", "upload down"),
        ]);
        let client = make_test_client(&url);

        let replay = rt
            .block_on(
                replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
                    &conn,
                    &client,
                    CompressionAlgo::Gzip,
                    &path,
                    10,
                    512 * 1024,
                    None,
                    None,
                ),
            )
            .unwrap();
        handle.join().unwrap();

        assert_eq!(captured.lock().unwrap().len(), 2);
        assert_eq!(replay.resolved, 0);
        assert_eq!(replay.failed, 1);
        assert_eq!(spool.pending_count().unwrap(), 1);
        assert_eq!(spool.dead_count().unwrap(), 0);
        let (retry_count, status, last_error): (i64, String, String) = conn
            .query_row(
                "SELECT retry_count, status, last_error FROM spool_queue WHERE id = ?1",
                [entry_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(retry_count, 0);
        assert_eq!(status, "pending");
        assert!(last_error.contains("uploading archive media"));
        assert_eq!(FileState::new(&conn).get_offset(&path_str).unwrap(), 0);
    }

    #[test]
    fn test_ship_trace_value_includes_coalesced_observation_window() {
        let item = ShipItem {
            path_str: "/tmp/session.jsonl".to_string(),
            provider: "codex".to_string(),
            offset: 10,
            new_offset: 20,
            event_count: 2,
            session_id: "session-1".to_string(),
            source_line_offsets: Vec::new(),
            source_line_refs: Vec::new(),
            media_objects: Vec::new(),
            compressed: Vec::new(),
        };
        let mut trace = make_ship_trace("live_transcript");
        trace.observed_at_ms = 100;
        trace.latest_observed_at_ms = Some(145);
        trace.enqueued_at_ms = 160;
        trace.job_started_at_ms = 170;

        let value = build_ship_trace_value(&item, Some(&trace), 180);

        assert_eq!(
            value.get("latest_observed_at_ms").and_then(Value::as_i64),
            Some(145)
        );
        assert_eq!(
            value.get("observation_window_ms").and_then(Value::as_i64),
            Some(45)
        );
        assert_eq!(
            value
                .get("observation_to_enqueue_ms")
                .and_then(Value::as_i64),
            Some(60)
        );
    }

    #[test]
    fn test_stage_timings_for_trace_surfaces_observed_to_ack() {
        let mut trace = make_ship_trace("live_transcript");
        trace.observed_at_ms = 1_000;
        trace.latest_observed_at_ms = Some(1_025);
        trace.wake_received_at_ms = Some(1_010);
        trace.enqueued_at_ms = 1_030;
        trace.job_started_at_ms = 1_045;
        trace.prepare_started_at_ms = 1_046;
        trace.prepare_finished_at_ms = 1_060;

        let timings = stage_timings_for_trace(Some(&trace), 1_080, 1_105, 25).unwrap();

        assert_eq!(timings.observed_at_ms, Some(1_000));
        assert_eq!(timings.latest_observed_at_ms, Some(1_025));
        assert_eq!(timings.http_send_started_at_ms, Some(1_080));
        assert_eq!(timings.http_finished_at_ms, Some(1_105));
        assert_eq!(timings.observation_window_ms, Some(25));
        assert_eq!(timings.observation_to_wake_ms, Some(10));
        assert_eq!(timings.wake_to_enqueue_ms, Some(20));
        assert_eq!(timings.enqueue_to_job_ms, Some(15));
        assert_eq!(timings.prepare_ms, Some(14));
        assert_eq!(timings.job_to_http_ms, Some(35));
        assert_eq!(timings.http_latency_ms, Some(25));
        assert_eq!(timings.observed_to_ack_ms, Some(105));
    }
}
