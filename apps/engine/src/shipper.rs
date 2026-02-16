//! Reusable shipping functions extracted from the `ship` command.
//!
//! Used by both `cmd_ship` (one-shot bulk) and `cmd_connect` (daemon mode).
//! Core operations: parse+compress a single file, POST and record state,
//! startup recovery, spool replay.

use std::path::{Path, PathBuf};

use anyhow::Result;
use rusqlite::Connection;

use crate::discovery::{self, ProviderConfig};
use crate::pipeline::compressor::{self, CompressionAlgo};
use crate::pipeline::parser;
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

    // Detect truncation
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

    if parse_result.events.is_empty() {
        return Ok(None);
    }

    let event_count = parse_result.events.len();
    let compressed = compressor::build_and_compress_with(
        &parse_result.metadata.session_id,
        &parse_result.events,
        &parse_result.metadata,
        &path_str,
        provider,
        algo,
    )?;

    Ok(Some(ShipItem {
        path_str,
        provider: provider.to_string(),
        offset,
        new_offset: file_size,
        event_count,
        session_id: parse_result.metadata.session_id.clone(),
        compressed,
    }))
}

/// Ship a prepared item via HTTP. On success, update both offsets.
/// On transient failure, advance queued_offset and enqueue to spool.
/// On client error (4xx), skip (advance offsets to avoid re-processing).
///
/// Returns the number of events shipped (0 on failure).
pub async fn ship_and_record(
    item: ShipItem,
    client: &ShipperClient,
    conn: &Connection,
) -> Result<usize> {
    let file_state = FileState::new(conn);
    let result = client.ship(item.compressed).await;

    match result {
        ShipResult::Ok(_) => {
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
            Ok(item.event_count)
        }
        ShipResult::RateLimited | ShipResult::ServerError(_, _) | ShipResult::ConnectError(_) => {
            let spool = Spool::new(conn);
            file_state.set_queued_offset(
                &item.path_str,
                item.new_offset,
                &item.provider,
                &item.session_id,
                &item.session_id,
            )?;
            spool.enqueue(
                &item.provider,
                &item.path_str,
                item.offset,
                item.new_offset,
                Some(&item.session_id),
            )?;

            let err_msg = match &result {
                ShipResult::RateLimited => "rate limited".to_string(),
                ShipResult::ServerError(code, body) => {
                    format!("{}:{}", code, &body[..body.len().min(200)])
                }
                ShipResult::ConnectError(e) => e.clone(),
                _ => unreachable!(),
            };
            tracing::warn!("Spooled {}: {}", item.path_str, err_msg);
            Ok(0)
        }
        ShipResult::ClientError(code, body) => {
            tracing::error!(
                "Client error shipping {}: {} {}",
                item.path_str,
                code,
                &body[..body.len().min(200)]
            );
            // Skip this file — advance offsets to avoid infinite re-processing
            file_state.set_offset(
                &item.path_str,
                item.new_offset,
                &item.session_id,
                &item.session_id,
                &item.provider,
            )?;
            Ok(0)
        }
    }
}

/// Startup recovery: find files where queued_offset > acked_offset
/// and re-enqueue their gaps into the spool.
pub fn run_startup_recovery(conn: &Connection) -> Result<usize> {
    let file_state = FileState::new(conn);
    let spool = Spool::new(conn);
    let unacked = file_state.get_unacked_files()?;
    let count = unacked.len();

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

    Ok(count)
}

/// Replay pending spool entries. Returns (shipped, failed).
pub async fn replay_spool_batch(
    conn: &Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    limit: usize,
) -> Result<(usize, usize)> {
    let spool = Spool::new(conn);
    let file_state = FileState::new(conn);
    let pending = spool.dequeue_batch(limit)?;

    let mut shipped = 0usize;
    let mut failed = 0usize;

    for entry in &pending {
        let path = PathBuf::from(&entry.file_path);
        if !path.exists() {
            tracing::warn!("Spool file missing: {}", entry.file_path);
            spool.mark_failed_with_max(entry.id, "file missing", 0)?;
            failed += 1;
            continue;
        }

        let parse_result = match parser::parse_session_file(&path, entry.start_offset) {
            Ok(r) => r,
            Err(e) => {
                spool.mark_failed(entry.id, &e.to_string())?;
                failed += 1;
                continue;
            }
        };

        if parse_result.events.is_empty() {
            spool.mark_shipped(entry.id)?;
            file_state.set_acked_offset(&entry.file_path, entry.end_offset)?;
            shipped += 1;
            continue;
        }

        let compressed = compressor::build_and_compress_with(
            &parse_result.metadata.session_id,
            &parse_result.events,
            &parse_result.metadata,
            &entry.file_path,
            &entry.provider,
            algo,
        )?;

        match client.ship(compressed).await {
            ShipResult::Ok(_) => {
                spool.mark_shipped(entry.id)?;
                file_state.set_acked_offset(&entry.file_path, entry.end_offset)?;
                shipped += 1;
            }
            ShipResult::ConnectError(_) => {
                // Don't mark failed — will retry next cycle
                break;
            }
            ShipResult::RateLimited | ShipResult::ServerError(_, _) => {
                spool.mark_failed(entry.id, "server error during replay")?;
                failed += 1;
            }
            ShipResult::ClientError(code, _) => {
                spool.mark_failed_with_max(entry.id, &format!("client error {}", code), 0)?;
                failed += 1;
            }
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
) -> Result<(usize, usize)> {
    let all_files = discovery::discover_all_files(providers);
    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;

    for (path, provider_name) in &all_files {
        match prepare_file(path, provider_name, algo, conn) {
            Ok(Some(item)) => {
                let events = ship_and_record(item, client, conn).await?;
                if events > 0 {
                    files_shipped += 1;
                    events_shipped += events;

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
