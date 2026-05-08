//! Reusable shipping functions extracted from the `ship` command.
//!
//! Used by both `cmd_ship` (one-shot bulk) and `cmd_connect` (daemon mode).
//! Core operations: parse+compress a single file, POST and record state,
//! startup recovery, spool replay.

mod types;

pub use types::*;

use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::Connection;
use tokio::task;

use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::{ConsecutiveErrorTracker, RecentIssueTracker};
use crate::pipeline::batcher::{self, PlannedRangeAction, ShipRange};
use crate::pipeline::compressor::{self, CompressionAlgo};
use crate::pipeline::parser::{self, ParseResult};
use crate::shipping::client::{ShipResult, ShipperClient};
use crate::shipping_stats::{RecentShipStatsTracker, ShipAttemptOutcome};
use crate::state::file_state::FileState;
use crate::state::live_file_state::LiveFileState;
use crate::state::spool::Spool;

const TARGET_BATCH_BYTES: u64 = 512 * 1024;

fn target_batch_bytes(max_batch_bytes: u64) -> u64 {
    max_batch_bytes.min(TARGET_BATCH_BYTES).max(1)
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
    let file_size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };

    // Detect truncation before parse dispatch.
    let rewind_hint = if file_size < current_offset {
        tracing::warn!(
            "File truncated: {} (was {}, now {}), resetting",
            path_str,
            current_offset,
            file_size
        );
        file_state.reset_offsets(&path_str)?;
        Some(truncation_rewind_hint(&path_str))
    } else {
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
        ShipResult::Ok => {
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

fn should_ack_empty_gemini_document(
    provider: &str,
    parse_result: &ParseResult,
    offset: u64,
    new_offset: u64,
) -> bool {
    provider == "gemini"
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

fn truncation_rewind_hint(path_str: &str) -> compressor::SourceRewindHint {
    compressor::SourceRewindHint {
        source_path: path_str.to_string(),
        source_offset: 0,
        reason: "truncation".to_string(),
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
    provider.eq_ignore_ascii_case("gemini")
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
        target_batch_bytes(max_batch_bytes),
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
            record_parse_issue(parse_tracker);
            tracing::warn!("Skip {}: {}", path_str, e);
            return Ok(None);
        }
    };

    let new_offset = end_offset_cap
        .map(|cap| parse_result.last_good_offset.min(cap))
        .unwrap_or(parse_result.last_good_offset);

    if should_ack_empty_gemini_document(provider, &parse_result, offset, new_offset) {
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
    )?;

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
) -> Result<Option<PreparedFile>> {
    let path_str = path.to_string_lossy().to_string();
    let file_state = FileState::new(conn);
    let live_file_state = LiveFileState::new(conn);
    let cursor_mode = match source_line_mode {
        SourceLineMode::Full => CursorMode::Archive,
        SourceLineMode::EventOnly => CursorMode::Live,
    };

    let current_offset = match cursor_mode {
        CursorMode::Archive => file_state.get_offset(&path_str)?,
        CursorMode::Live => live_file_state.get_offset(&path_str)?,
    };
    let queued_offset = match cursor_mode {
        CursorMode::Archive => file_state.get_queued_offset(&path_str)?,
        CursorMode::Live => current_offset,
    };
    let file_size = match std::fs::metadata(path) {
        Ok(m) => m.len(),
        Err(e) => {
            tracing::warn!("Cannot stat {}: {}", path_str, e);
            return Ok(None);
        }
    };

    let mut rewind_hint = if file_size < current_offset {
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

    prepare_path_range_with_parse_tracker(
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
    )
}

#[tracing::instrument(
    level = "info",
    name = "engine.ship.attempt",
    skip(item, client, tracker, ship_stats),
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
) -> AttemptedShip {
    let http_send_started_at_ms = chrono::Utc::now().timestamp_millis();
    let trace_header = ship_trace.and_then(|trace| {
        serde_json::to_string(&serde_json::json!({
            "schema": "ship_trace.v1",
            "trace_id": format!("{}:{}:{}:{}", item.session_id, item.offset, item.new_offset, http_send_started_at_ms),
            "provider": item.provider,
            "session_id": item.session_id,
            "work_context": trace.work_context,
            "observation_source": trace.observation_source,
            "event_count": item.event_count,
            "offset": item.offset,
            "new_offset": item.new_offset,
            "range_bytes": item.new_offset.saturating_sub(item.offset),
            "observed_at_ms": trace.observed_at_ms,
            "enqueued_at_ms": trace.enqueued_at_ms,
            "job_started_at_ms": trace.job_started_at_ms,
            "prepare_started_at_ms": trace.prepare_started_at_ms,
            "prepare_finished_at_ms": trace.prepare_finished_at_ms,
            "http_send_started_at_ms": http_send_started_at_ms,
            "observation_to_enqueue_ms": trace.enqueued_at_ms.saturating_sub(trace.observed_at_ms),
            "enqueue_to_job_ms": trace.job_started_at_ms.saturating_sub(trace.enqueued_at_ms),
            "observed_to_job_ms": trace.job_started_at_ms.saturating_sub(trace.observed_at_ms),
            "prepare_ms": trace.prepare_finished_at_ms.saturating_sub(trace.prepare_started_at_ms),
            "job_to_http_ms": http_send_started_at_ms.saturating_sub(trace.job_started_at_ms),
        }))
        .ok()
    });
    let payload = std::mem::take(&mut item.compressed);
    let attempt_started = std::time::Instant::now();
    let result = if let Some(trace_header) = trace_header.as_deref() {
        client.ship_with_trace(payload, Some(trace_header)).await
    } else {
        client.ship(payload).await
    };
    let latency_ms = attempt_started.elapsed().as_millis() as u64;
    let span = tracing::Span::current();
    let (outcome, http_status) = classify_ship_attempt_result(&result);
    span.record(
        "longhouse.ship.outcome",
        tracing::field::display(outcome.as_str()),
    );
    if let Some(status) = http_status {
        span.record("http.response.status_code", tracing::field::display(status));
    }
    let error_kind = transient_error_kind(&result);
    let error_message = match &result {
        ShipResult::Ok => None,
        _ => Some(transient_error_message(&result)),
    };
    if let Some(kind) = error_kind {
        span.record("longhouse.ship.error_kind", tracing::field::display(kind));
    }
    if let Some(stats) = ship_stats {
        if error_kind.is_none() && error_message.is_none() {
            stats.record(outcome, latency_ms, http_status);
        } else {
            stats.record_with_detail(
                outcome,
                latency_ms,
                http_status,
                error_kind,
                error_message.as_deref(),
            );
        }
    }

    match result {
        ShipResult::Ok => {
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
        ShipResult::RateLimited
        | ShipResult::ServerError(_, _)
        | ShipResult::ConnectError(_)
        | ShipResult::RetryableClientError(_, _) => {
            let error = error_message.unwrap_or_else(|| transient_error_message(&result));
            let should_log = tracker.map_or(true, |t| t.record_error());
            if should_log {
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

            AttemptedShip::Transient {
                item,
                error,
                is_connect_error: matches!(result, ShipResult::ConnectError(_)),
            }
        }
        ShipResult::PayloadTooLarge(body) => {
            tracing::warn!(
                "Payload too large shipping {}: {}",
                item.path_str,
                truncate_http_body(&body)
            );
            AttemptedShip::PayloadTooLarge { item }
        }
        ShipResult::PayloadRejected(status_code, body) => {
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

fn classify_ship_attempt_result(result: &ShipResult) -> (ShipAttemptOutcome, Option<u16>) {
    match result {
        ShipResult::Ok => (ShipAttemptOutcome::Ok, None),
        ShipResult::RateLimited => (ShipAttemptOutcome::RateLimited, Some(429)),
        ShipResult::ServerError(code, _) => (ShipAttemptOutcome::ServerError, Some(*code)),
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
        ShipResult::Ok => None,
        ShipResult::RateLimited => Some("rate_limited"),
        ShipResult::ServerError(_, _) => Some("server_response"),
        ShipResult::PayloadRejected(_, _) => Some("payload_rejected"),
        ShipResult::PayloadTooLarge(_) => Some("payload_too_large"),
        ShipResult::RetryableClientError(401 | 403, _) => Some("auth"),
        ShipResult::RetryableClientError(_, _) => Some("client_response"),
        ShipResult::ConnectError(detail) => Some(detail.kind),
    }
}

fn transient_error_message(result: &ShipResult) -> String {
    match result {
        ShipResult::Ok => "ok".to_string(),
        ShipResult::RateLimited => "rate limited".to_string(),
        ShipResult::ServerError(code, body) => format!("{}:{}", code, truncate_http_body(body)),
        ShipResult::PayloadRejected(code, body) => format!("{}:{}", code, truncate_http_body(body)),
        ShipResult::PayloadTooLarge(body) => format!("413:{}", truncate_http_body(body)),
        ShipResult::RetryableClientError(code, body) => {
            format!("{}:{}", code, truncate_http_body(body))
        }
        ShipResult::ConnectError(detail) => detail.message.clone(),
    }
}

pub async fn ship_prepared_file(
    prepared: PreparedFile,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
) -> Result<ShipPreparedOutcome> {
    ship_prepared_file_with_trace(prepared, client, conn, tracker, ship_stats, None).await
}

pub async fn ship_prepared_file_with_trace(
    prepared: PreparedFile,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
    ship_trace: Option<&ShipTraceContext>,
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
                match attempt_ship(item, client, tracker, ship_stats, ship_trace).await {
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
    )
    .await?;

    // Cleanup old dead entries
    let cleaned = spool.cleanup()?;
    if cleaned > 0 {
        tracing::info!("Cleaned {} old spool entries", cleaned);
    }

    Ok((outcome.resolved, outcome.failed))
}

pub(crate) async fn replay_spool_for_path_with_batch_bytes_and_parse_tracker(
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
    let pending = spool.pending_entries_for_path(&file_path.to_string_lossy(), limit)?;
    replay_spool_entries(
        conn,
        client,
        algo,
        &pending,
        max_batch_bytes,
        parse_tracker,
        ship_stats,
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
    )
    .await
}

async fn prepare_spool_entry_for_replay(
    entry: crate::state::spool::SpoolEntry,
    path: PathBuf,
    algo: CompressionAlgo,
    max_batch_bytes: u64,
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
        )
    })
    .await?
}

#[tracing::instrument(
    level = "info",
    name = "engine.spool.replay",
    skip(conn, client, pending, parse_tracker, ship_stats),
    fields(longhouse.spool.pending_entries = pending.len() as u64)
)]
async fn replay_spool_entries(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    pending: &[crate::state::spool::SpoolEntry],
    max_batch_bytes: u64,
    parse_tracker: Option<&RecentIssueTracker>,
    ship_stats: Option<&RecentShipStatsTracker>,
) -> Result<ReplaySpoolOutcome> {
    let spool = Spool::new(conn);
    let file_state = FileState::new(conn);
    let mut outcome = ReplaySpoolOutcome::default();

    'entry_loop: for entry in pending {
        let path = PathBuf::from(&entry.file_path);
        if !path.exists() {
            tracing::warn!("Spool file missing: {}", entry.file_path);
            spool.mark_failed_with_max(entry.id, "file missing", 0)?;
            outcome.failed += 1;
            continue;
        }

        let prepared = match prepare_spool_entry_for_replay(
            entry.clone(),
            path,
            algo,
            max_batch_bytes,
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
                    match attempt_ship(item, client, None, ship_stats, None).await {
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
                        } => {
                            if is_connect_error {
                                outcome.had_connect_error = true;
                                break 'entry_loop;
                            }
                            spool.mark_failed(entry.id, &error)?;
                            outcome.failed += 1;
                            continue 'entry_loop;
                        }
                        AttemptedShip::PayloadTooLarge { item: _ } => {
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
        match prepare_file_batches_with_parse_tracker(
            path,
            provider_name,
            algo,
            conn,
            max_batch_bytes,
            None,
            parse_tracker,
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
    fn test_prepare_gemini_file_uses_source_line_boundaries_when_archive_is_available() {
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
            prepare_file_batches(&path, "gemini", CompressionAlgo::Gzip, &conn, 32, None).unwrap();
        let prepared = prepared.expect("gemini file should still prepare with source-line archive");

        assert!(
            prepared.actions.len() > 1,
            "source-line archive should let gemini split instead of whole-document fallback"
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
            "gemini",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
            None,
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
            PreparedAction::AckOnly(_) => panic!("conversation gemini file should not ack-only"),
        }
    }

    #[test]
    fn test_prepare_gemini_rewritten_document_rewinds_from_previous_offset() {
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
                "gemini",
            )
            .unwrap();

        let prepared = prepare_file_batches(
            &path,
            "gemini",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
            None,
        )
        .unwrap()
        .expect("rewritten gemini file should prepare from the beginning");

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
                panic!("conversation gemini rewrite should ship as a full document")
            }
        }
    }

    #[test]
    fn test_prepare_gemini_info_file_acknowledges_without_shipping() {
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
            "gemini",
            CompressionAlgo::Gzip,
            &conn,
            5 * 1024 * 1024,
            None,
        )
        .unwrap();
        let prepared = prepared.expect("gemini info file should no longer be skipped");

        assert_eq!(prepared.actions.len(), 1);
        match prepared.actions.into_iter().next().unwrap() {
            PreparedAction::AckOnly(item) => {
                assert_eq!(item.offset, 0);
                assert_eq!(item.new_offset, std::fs::metadata(&path).unwrap().len());
                assert_eq!(item.provider, "gemini");
            }
            PreparedAction::Ship(_) | PreparedAction::DeadLetter(_) => {
                panic!("gemini info-only file should be acknowledged without shipping")
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
            path_str: "/tmp/gemini-info.json".to_string(),
            offset: 0,
            new_offset: 413,
            has_reply_evidence: false,
            cursor_mode: CursorMode::Archive,
            actions: vec![PreparedAction::AckOnly(AckOnlyItem {
                path_str: "/tmp/gemini-info.json".to_string(),
                provider: "gemini".to_string(),
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
        assert_eq!(fs.get_offset("/tmp/gemini-info.json").unwrap(), 413);
        assert_eq!(fs.get_queued_offset("/tmp/gemini-info.json").unwrap(), 413);
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
    }

    #[tokio::test]
    async fn test_replay_spool_for_path_now_ignores_backoff_for_target_file() {
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
        conn.execute(
            "UPDATE spool_queue SET next_retry_at = datetime('now', '+5 minutes') WHERE file_path = ?1",
            [&path_str],
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
        )
        .await
        .unwrap();
        assert_eq!(regular.resolved, 0);
        assert_eq!(regular.events_shipped, 0);

        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        let client = make_test_client(&url);
        let immediate = replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
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

        assert_eq!(immediate.resolved, 1);
        assert_eq!(immediate.failed, 0);
        assert_eq!(immediate.events_shipped, 2);
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
}
