//! Reusable shipping functions extracted from the `ship` command.
//!
//! Used by both `cmd_ship` (one-shot bulk) and `cmd_connect` (daemon mode).
//! Core operations: parse+compress a single file, POST and record state,
//! startup recovery, spool replay.

use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::Connection;

use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::pipeline::batcher::{self, PlannedRangeAction, ShipRange};
use crate::pipeline::compressor::{self, CompressionAlgo};
use crate::pipeline::parser::{self, ParseResult};
use crate::shipping::client::{ShipResult, ShipperClient};
use crate::state::file_state::FileState;
use crate::state::spool::Spool;

/// Result of parsing + compressing a single file.
pub struct ShipItem {
    pub path_str: String,
    pub provider: String,
    pub offset: u64,
    pub new_offset: u64,
    pub event_count: usize,
    pub session_id: String,
    pub compressed: Vec<u8>,
}

pub struct DeadLetterItem {
    pub path_str: String,
    pub provider: String,
    pub offset: u64,
    pub new_offset: u64,
    pub event_count: usize,
    pub session_id: String,
    pub reason: String,
}

pub enum PreparedAction {
    Ship(ShipItem),
    DeadLetter(DeadLetterItem),
}

pub struct PreparedFile {
    pub path_str: String,
    pub provider: String,
    pub offset: u64,
    pub new_offset: u64,
    pub session_id: String,
    pub actions: Vec<PreparedAction>,
}

impl PreparedAction {
    fn event_count(&self) -> usize {
        match self {
            PreparedAction::Ship(item) => item.event_count,
            PreparedAction::DeadLetter(item) => item.event_count,
        }
    }

    fn offset(&self) -> u64 {
        match self {
            PreparedAction::Ship(item) => item.offset,
            PreparedAction::DeadLetter(item) => item.offset,
        }
    }

    fn new_offset(&self) -> u64 {
        match self {
            PreparedAction::Ship(item) => item.new_offset,
            PreparedAction::DeadLetter(item) => item.new_offset,
        }
    }
}

impl PreparedFile {
    pub fn total_event_count(&self) -> usize {
        self.actions.iter().map(PreparedAction::event_count).sum()
    }
}

pub struct ShipPreparedOutcome {
    pub events_shipped: usize,
    pub bytes_shipped: u64,
    pub dead_lettered: usize,
    pub fully_processed: bool,
    pub had_connect_error: bool,
}

impl Default for ShipPreparedOutcome {
    fn default() -> Self {
        Self {
            events_shipped: 0,
            bytes_shipped: 0,
            dead_lettered: 0,
            fully_processed: true,
            had_connect_error: false,
        }
    }
}

enum AttemptedShip {
    Shipped(ShipItem),
    Transient {
        item: ShipItem,
        error: String,
        is_connect_error: bool,
    },
    ClientError {
        item: ShipItem,
        status_code: u16,
        body: String,
    },
}

pub enum ShipAndRecordOutcome {
    Shipped { events: usize },
    Spooled { is_connect_error: bool },
    SkippedClientError { status_code: u16 },
}

/// Parse and compress a single file from its current offset.
///
/// Returns `None` if the file has no new content, can't be read, or has no events.
pub fn prepare_file(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
) -> Result<Option<ShipItem>> {
    let path_str = path.to_string_lossy().to_string();
    let file_state = FileState::new(conn);

    let current_offset = file_state.get_offset(&path_str)?;
    let file_size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };

    // Detect truncation before parse dispatch.
    let offset = if file_size < current_offset {
        tracing::warn!(
            "File truncated: {} (was {}, now {}), resetting",
            path_str,
            current_offset,
            file_size
        );
        file_state.reset_offsets(&path_str)?;
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
        algo,
    )?;

    Ok(Some(ShipItem {
        path_str,
        provider: provider.to_string(),
        offset,
        new_offset,
        event_count,
        session_id: parse_result.metadata.session_id.clone(),
        compressed,
    }))
}

/// Ship a prepared item via HTTP. On success, update both offsets.
/// On transient failure, advance queued_offset and enqueue to spool.
/// On client error (4xx), skip (advance offsets to avoid re-processing).
///
/// Returns a structured outcome so callers can distinguish shipped, spooled,
/// and skipped-client-error paths without duplicating transport logic.
pub async fn ship_and_record(
    item: ShipItem,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<ShipAndRecordOutcome> {
    let file_state = FileState::new(conn);
    let result = client.ship(item.compressed).await;

    match result {
        ShipResult::Ok(_) => {
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
        ShipResult::RateLimited | ShipResult::ServerError(_, _) | ShipResult::ConnectError(_) => {
            let err_msg = match &result {
                ShipResult::RateLimited => "rate limited".to_string(),
                ShipResult::ServerError(code, body) => {
                    format!("{}:{}", code, &body[..body.len().min(200)])
                }
                ShipResult::ConnectError(e) => e.clone(),
                _ => unreachable!(),
            };

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
        ShipResult::ClientError(code, body) => {
            tracing::error!(
                "Client error shipping {}: {} {}",
                item.path_str,
                code,
                &body[..body.len().min(200)]
            );
            if code == 413 {
                // 413 = payload too large. The data is valid but the payload
                // exceeds a proxy/server limit. Spool for retry — a future
                // version with byte-based batching will split it into smaller
                // chunks. Do NOT advance offsets (that loses data).
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
            } else {
                // Other 4xx (400, 401, 403, 422) — skip to avoid infinite re-processing
                file_state.set_offset(
                    &item.path_str,
                    item.new_offset,
                    &item.session_id,
                    &item.session_id,
                    &item.provider,
                )?;
                Ok(ShipAndRecordOutcome::SkippedClientError { status_code: code })
            }
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

fn event_range_for_offsets(
    events: &[parser::ParsedEvent],
    start_offset: u64,
    end_offset: u64,
) -> std::ops::Range<usize> {
    let start = events.partition_point(|event| event.source_offset < start_offset);
    let end = events.partition_point(|event| event.source_offset < end_offset);
    start..end
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

fn prepare_whole_document_action(
    parse_result: &ParseResult,
    path_str: &str,
    provider: &str,
    start_offset: u64,
    end_offset: u64,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
) -> Result<Vec<PreparedAction>> {
    let compressed = compressor::build_and_compress_with(
        &parse_result.metadata.session_id,
        &parse_result.events,
        &parse_result.metadata,
        path_str,
        provider,
        algo,
    )?;

    if compressed.len() as u64 <= max_batch_bytes {
        return Ok(vec![PreparedAction::Ship(ShipItem {
            path_str: path_str.to_string(),
            provider: provider.to_string(),
            offset: start_offset,
            new_offset: end_offset,
            event_count: parse_result.events.len(),
            session_id: parse_result.metadata.session_id.clone(),
            compressed,
        })]);
    }

    Ok(vec![PreparedAction::DeadLetter(DeadLetterItem {
        path_str: path_str.to_string(),
        provider: provider.to_string(),
        offset: start_offset,
        new_offset: end_offset,
        event_count: parse_result.events.len(),
        session_id: parse_result.metadata.session_id.clone(),
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
) -> Result<Vec<PreparedAction>> {
    let compressed = compressor::build_and_compress_with_source_lines(
        &parse_result.metadata.session_id,
        &parse_result.events[range.event_range.clone()],
        &parse_result.metadata,
        path_str,
        provider,
        Some(&parse_result.source_lines[range.source_line_range.clone()]),
        algo,
    )?;

    if compressed.len() as u64 <= max_batch_bytes {
        return Ok(vec![PreparedAction::Ship(ShipItem {
            path_str: path_str.to_string(),
            provider: provider.to_string(),
            offset: range.start_offset,
            new_offset: range.end_offset,
            event_count: range
                .event_range
                .end
                .saturating_sub(range.event_range.start),
            session_id: parse_result.metadata.session_id.clone(),
            compressed,
        })]);
    }

    let line_count = range
        .source_line_range
        .end
        .saturating_sub(range.source_line_range.start);
    if line_count <= 1 {
        return Ok(vec![PreparedAction::DeadLetter(
            dead_letter_from_compressed_range(
                path_str,
                provider,
                &parse_result.metadata.session_id,
                &range,
                max_batch_bytes,
                compressed.len(),
            ),
        )]);
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

    let mut actions = materialize_ship_range(
        parse_result,
        path_str,
        provider,
        algo,
        max_batch_bytes,
        left,
    )?;
    actions.extend(materialize_ship_range(
        parse_result,
        path_str,
        provider,
        algo,
        max_batch_bytes,
        right,
    )?);
    Ok(actions)
}

fn build_prepared_actions(
    parse_result: &ParseResult,
    path_str: &str,
    provider: &str,
    start_offset: u64,
    end_offset: u64,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
) -> Result<Vec<PreparedAction>> {
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
        );
    }

    let mut actions = Vec::new();
    for planned in batcher::plan_range_actions(
        &parse_result.source_lines,
        &parse_result.events,
        start_offset,
        end_offset,
        max_batch_bytes,
    )? {
        match planned {
            PlannedRangeAction::Ship(range) => actions.extend(materialize_ship_range(
                parse_result,
                path_str,
                provider,
                algo,
                max_batch_bytes,
                range,
            )?),
            PlannedRangeAction::DeadLetter(range) => {
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
    Ok(actions)
}

pub fn prepare_path_range(
    path: &Path,
    provider: &str,
    offset: u64,
    end_offset_cap: Option<u64>,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
) -> Result<Option<PreparedFile>> {
    let path_str = path.to_string_lossy().to_string();
    let file_size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };

    let parse_result = match parser::parse_session_file(path, offset) {
        Ok(result) => result,
        Err(e) => {
            tracing::warn!("Skip {}: {}", path_str, e);
            return Ok(None);
        }
    };

    if parse_result.events.is_empty() && parse_result.source_lines.is_empty() {
        log_suspicious_empty_parse(&path_str, file_size, parse_result.candidate_records);
        return Ok(None);
    }

    let new_offset = end_offset_cap
        .map(|cap| parse_result.last_good_offset.min(cap))
        .unwrap_or(parse_result.last_good_offset);

    if new_offset <= offset {
        return Ok(None);
    }

    let actions = build_prepared_actions(
        &parse_result,
        &path_str,
        provider,
        offset,
        new_offset,
        algo,
        max_batch_bytes,
    )?;

    if actions.is_empty() {
        return Ok(None);
    }

    Ok(Some(PreparedFile {
        path_str,
        provider: provider.to_string(),
        offset,
        new_offset,
        session_id: parse_result.metadata.session_id.clone(),
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
    prepare_path_range(path, provider, offset, None, algo, max_batch_bytes)
}

pub fn prepare_file_batches(
    path: &Path,
    provider: &str,
    algo: CompressionAlgo,
    conn: &Connection,
    max_batch_bytes: u64,
) -> Result<Option<PreparedFile>> {
    let path_str = path.to_string_lossy().to_string();
    let file_state = FileState::new(conn);

    let current_offset = file_state.get_offset(&path_str)?;
    let queued_offset = file_state.get_queued_offset(&path_str)?;
    let file_size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };

    let offset = if file_size < current_offset {
        tracing::warn!(
            "File truncated: {} (was {}, now {}), resetting",
            path_str,
            current_offset,
            file_size
        );
        file_state.reset_offsets(&path_str)?;
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
    } else {
        current_offset
    };

    prepare_path_range(path, provider, offset, None, algo, max_batch_bytes)
}

async fn attempt_ship(
    mut item: ShipItem,
    client: &ShipperClient,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> AttemptedShip {
    let payload = std::mem::take(&mut item.compressed);
    let result = client.ship(payload).await;

    match result {
        ShipResult::Ok(_) => {
            if let Some(t) = tracker {
                if let Some(n) = t.record_success() {
                    tracing::info!(
                        "Recovered after {} ship failure(s), now shipping normally",
                        n
                    );
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
        ShipResult::RateLimited | ShipResult::ServerError(_, _) | ShipResult::ConnectError(_) => {
            let error = match &result {
                ShipResult::RateLimited => "rate limited".to_string(),
                ShipResult::ServerError(code, body) => {
                    format!("{}:{}", code, &body[..body.len().min(200)])
                }
                ShipResult::ConnectError(error) => error.clone(),
                _ => unreachable!(),
            };
            let should_log = tracker.map_or(true, |t| t.record_error());
            if should_log {
                let count = tracker.map_or(1, |t| t.consecutive_count());
                if count > 1 {
                    tracing::warn!(
                        "Ship still failing after {} attempts, latest: {}",
                        count,
                        error
                    );
                } else {
                    tracing::warn!("Spooled {}: {}", item.path_str, error);
                }
            }

            AttemptedShip::Transient {
                item,
                error,
                is_connect_error: matches!(result, ShipResult::ConnectError(_)),
            }
        }
        ShipResult::ClientError(status_code, body) => {
            tracing::error!(
                "Client error shipping {}: {} {}",
                item.path_str,
                status_code,
                &body[..body.len().min(200)]
            );
            AttemptedShip::ClientError {
                item,
                status_code,
                body,
            }
        }
    }
}

pub async fn ship_prepared_file(
    prepared: PreparedFile,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<ShipPreparedOutcome> {
    let file_state = FileState::new(conn);
    let spool = Spool::new(conn);
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
                file_state.set_offset(
                    &item.path_str,
                    item.new_offset,
                    &item.session_id,
                    &item.session_id,
                    &item.provider,
                )?;
                outcome.dead_lettered += 1;
            }
            PreparedAction::Ship(item) => match attempt_ship(item, client, tracker).await {
                AttemptedShip::Shipped(item) => {
                    file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.session_id,
                        &item.session_id,
                        &item.provider,
                    )?;
                    outcome.events_shipped += item.event_count;
                    outcome.bytes_shipped += item.new_offset - item.offset;
                }
                AttemptedShip::Transient {
                    item,
                    error: _,
                    is_connect_error,
                } => {
                    let enqueued = spool.enqueue(
                        &item.provider,
                        &item.path_str,
                        item.offset,
                        prepared.new_offset,
                        Some(&item.session_id),
                    )?;
                    if enqueued {
                        file_state.set_queued_offset(
                            &item.path_str,
                            prepared.new_offset,
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
                AttemptedShip::ClientError {
                    item,
                    status_code,
                    body: _,
                } => {
                    if status_code == 413 {
                        let enqueued = spool.enqueue(
                            &item.provider,
                            &item.path_str,
                            item.offset,
                            prepared.new_offset,
                            Some(&item.session_id),
                        )?;
                        if enqueued {
                            file_state.set_queued_offset(
                                &item.path_str,
                                prepared.new_offset,
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

                    file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.session_id,
                        &item.session_id,
                        &item.provider,
                    )?;
                }
            },
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

/// Replay pending spool entries. Returns (entries fully resolved, entries failed/backed off).
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
    let spool = Spool::new(conn);
    let file_state = FileState::new(conn);
    let pending = spool.dequeue_batch(limit)?;

    let mut shipped = 0usize;
    let mut failed = 0usize;

    'entry_loop: for entry in &pending {
        let path = PathBuf::from(&entry.file_path);
        if !path.exists() {
            tracing::warn!("Spool file missing: {}", entry.file_path);
            spool.mark_failed_with_max(entry.id, "file missing", 0)?;
            failed += 1;
            continue;
        }

        let prepared = match prepare_path_range(
            &path,
            &entry.provider,
            entry.start_offset,
            Some(entry.end_offset),
            algo,
            max_batch_bytes,
        ) {
            Ok(Some(prepared)) => prepared,
            Ok(None) => {
                if entry.start_offset >= entry.end_offset {
                    spool.mark_shipped(entry.id)?;
                    shipped += 1;
                } else {
                    spool.mark_failed(entry.id, "no complete lines ready for replay")?;
                    failed += 1;
                }
                continue;
            }
            Err(e) => {
                spool.mark_failed(entry.id, &e.to_string())?;
                failed += 1;
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
                PreparedAction::Ship(item) => match attempt_ship(item, client, None).await {
                    AttemptedShip::Shipped(item) => {
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
                    } => {
                        if is_connect_error {
                            break 'entry_loop;
                        }
                        spool.mark_failed(entry.id, &error)?;
                        failed += 1;
                        continue 'entry_loop;
                    }
                    AttemptedShip::ClientError {
                        item,
                        status_code,
                        body,
                    } => {
                        if status_code == 413 {
                            spool.mark_failed_with_max(
                                entry.id,
                                "413 payload too large during replay",
                                u32::MAX,
                            )?;
                            failed += 1;
                            continue 'entry_loop;
                        }

                        let error = format!(
                            "client error {}:{}",
                            status_code,
                            &body[..body.len().min(200)]
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
                },
            }

            if entry_done {
                break;
            }
        }

        if entry_done {
            shipped += 1;
        }
    }

    // Cleanup old dead entries
    let cleaned = spool.cleanup()?;
    if cleaned > 0 {
        tracing::info!("Cleaned {} old spool entries", cleaned);
    }

    Ok((shipped, failed))
}

/// Run a full scan: discover all provider files, prepare and ship any with new content.
/// Returns (files_shipped, events_shipped).
pub async fn full_scan(
    providers: &[ProviderConfig],
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<(usize, usize)> {
    full_scan_with_batch_bytes(providers, conn, client, algo, u64::MAX, tracker).await
}

pub async fn full_scan_with_batch_bytes(
    providers: &[ProviderConfig],
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<(usize, usize)> {
    let all_files = discovery::discover_all_files(providers);
    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;

    for (path, provider_name) in &all_files {
        match prepare_file_batches(path, provider_name, algo, conn, max_batch_bytes) {
            Ok(Some(prepared)) => {
                let outcome = ship_prepared_file(prepared, client, conn, tracker).await?;
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
    use flate2::read::GzDecoder;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    fn make_db() -> (tempfile::NamedTempFile, Connection) {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();
        (tmp, conn)
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
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 400).unwrap();
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
    fn test_prepare_gemini_file_without_source_lines_uses_whole_document_fallback() {
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
            prepare_file_batches(&path, "gemini", CompressionAlgo::Gzip, &conn, 32).unwrap();
        let prepared = prepared.expect("gemini file should still prepare without source lines");

        assert_eq!(prepared.actions.len(), 1);
        let action = prepared.actions.into_iter().next().unwrap();
        match action {
            PreparedAction::DeadLetter(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, std::fs::metadata(&path).unwrap().len());
                assert_eq!(item.event_count, 2);
                assert!(
                    item.reason.contains("whole-document payload"),
                    "reason should explain why batching could not split the file"
                );
            }
            PreparedAction::Ship(_) => panic!("tiny batch limit should dead-letter whole doc"),
        }

        let prepared = prepare_file_batches(
            &path,
            "gemini",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
        )
        .unwrap();
        let prepared = prepared.expect("gemini file should prepare at normal batch limit");
        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::Ship(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, std::fs::metadata(&path).unwrap().len());
                assert_eq!(item.event_count, 2);
            }
            PreparedAction::DeadLetter(_) => {
                panic!("normal batch limit should ship whole-document gemini payload")
            }
        }
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
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 800).unwrap();
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
            .block_on(ship_prepared_file(prepared, &client, &conn, None))
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
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 600).unwrap();
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
            .block_on(ship_prepared_file(prepared, &client, &conn, None))
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
        assert!(file_end > line2_end, "test requires newer bytes beyond queued gap");

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
            prepare_file_batches(&path, "claude", CompressionAlgo::Gzip, &conn, 10_000).unwrap();
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
    fn test_replay_413_stays_pending_with_backoff() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            claude_session_lines(),
            "99991111-2222-3333-4444-555566667777.jsonl",
        );
        let path = dir
            .path()
            .join("99991111-2222-3333-4444-555566667777.jsonl");
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
        )
        .unwrap()
        .expect("replay range should prepare into multiple batches");
        let ship_offsets: Vec<(u64, u64)> = prepared
            .actions
            .iter()
            .filter_map(|action| match action {
                PreparedAction::Ship(item) => Some((item.offset, item.new_offset)),
                PreparedAction::DeadLetter(_) => None,
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
}
