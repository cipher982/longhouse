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
/// Returns (events_shipped, is_connect_error).
/// is_connect_error is true when the server was unreachable — callers
/// should enter offline mode and stop shipping until connectivity recovers.
pub async fn ship_and_record(
    item: ShipItem,
    client: &ShipperClient,
    conn: &Connection,
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<(usize, bool)> {
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
            Ok((item.event_count, false))
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
            Ok((0, is_connect_error))
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
            Ok((0, false))
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
    tracker: Option<&ConsecutiveErrorTracker>,
) -> Result<(usize, usize)> {
    let all_files = discovery::discover_all_files(providers);
    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;

    for (path, provider_name) in &all_files {
        match prepare_file(path, provider_name, algo, conn) {
            Ok(Some(item)) => {
                let (events, _is_connect_err) = ship_and_record(item, client, conn, tracker).await?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;
    use std::io::Write;

    fn make_db() -> (tempfile::NamedTempFile, Connection) {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();
        (tmp, conn)
    }

    fn claude_session_lines() -> &'static str {
        concat!(
            r#"{"type":"user","uuid":"11111111-1111-1111-1111-111111111111","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#, "\n",
            r#"{"type":"assistant","uuid":"22222222-2222-2222-2222-222222222222","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi there"}]}}"#, "\n",
        )
    }

    fn codex_session_lines() -> &'static str {
        concat!(
            r#"{"type":"session_meta","timestamp":"2026-02-15T10:00:00Z","payload":{"type":"session_meta","id":"cccccccc-1111-2222-3333-444455556666","cwd":"/tmp/test","cli_version":"0.1.0"}}"#, "\n",
            r#"{"type":"response_item","timestamp":"2026-02-15T10:00:01Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"hello from codex"}]}}"#, "\n",
            r#"{"type":"response_item","timestamp":"2026-02-15T10:00:02Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hi from codex"}]}}"#, "\n",
        )
    }

    /// Write content to a temp file with a UUID-based name.
    fn write_session_file(content: &str, name: &str) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(name);
        std::fs::write(&path, content).unwrap();
        dir
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
        let path = dir.path().join("aaaa1111-2222-3333-4444-555566667777.jsonl");
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
        assert!(result.is_none(), "Stale offset should cause file to be skipped");
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
        let path = dir.path().join("aaaa1111-2222-3333-4444-555566667777.jsonl");
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
        let path = dir.path().join(
            "rollout-2026-02-15T10-00-00-cccc1111-2222-3333-4444-555566667777.jsonl",
        );

        let result = prepare_file(&path, "codex", CompressionAlgo::Gzip, &conn).unwrap();
        assert!(result.is_some(), "Codex file should be prepared");
        let item = result.unwrap();
        // session_meta provides session_id override
        assert_eq!(item.session_id, "cccccccc-1111-2222-3333-444455556666");
        assert_eq!(item.event_count, 2); // user + assistant messages
        assert_eq!(item.provider, "codex");
    }

    // ---------------------------------------------------------------
    // Regression: subagent files get deterministic UUID v5
    // ---------------------------------------------------------------

    #[test]
    fn test_subagent_file_gets_uuid() {
        let (_tmp, conn) = make_db();
        let dir = write_session_file(
            claude_session_lines(),
            "agent-a51c878.jsonl",
        );
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
        let dir = write_session_file(
            claude_session_lines(),
            "agent-a51c878.jsonl",
        );
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
        let path = dir.path().join("bbbb1111-2222-3333-4444-555566667777.jsonl");

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
        assert_eq!(item.offset, 0, "Should start from offset 0 after truncation");
        assert_eq!(item.event_count, 2);
    }

    // ---------------------------------------------------------------
    // Regression: incremental append ships only new events
    // ---------------------------------------------------------------

    #[test]
    fn test_incremental_append_ships_new_events_only() {
        let (_tmp, conn) = make_db();
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("dddd1111-2222-3333-4444-555566667777.jsonl");

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
        let mut f = std::fs::OpenOptions::new().append(true).open(&path).unwrap();
        writeln!(f, r#"{{"type":"assistant","uuid":"inc-2","timestamp":"2026-02-15T10:00:01Z","message":{{"content":[{{"type":"text","text":"second"}}]}}}}"#).unwrap();

        // Second prepare ships only the new event
        let result2 = prepare_file(&path, "claude", CompressionAlgo::Gzip, &conn).unwrap();
        let item2 = result2.unwrap();
        assert_eq!(item2.event_count, 1, "Should only ship the appended event");
        assert_eq!(item2.offset, item1.new_offset, "Should start from previous offset");
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
        fs.set_offset("/tmp/test.jsonl", 100, "sess-1", "sess-1", "claude").unwrap();
        fs.set_queued_offset("/tmp/test.jsonl", 500, "claude", "sess-1", "sess-1").unwrap();

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
        fs.set_offset("/bp/test.jsonl", 0, "sess-bp", "sess-bp", "claude").unwrap();

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
        let enqueued = spool.enqueue("claude", "/bp/test.jsonl", 0, 100, Some("sess-bp")).unwrap();
        assert!(!enqueued, "Spool should be full");

        // queued_offset must remain at 0 (not advanced to 100)
        let qoff = fs.get_queued_offset("/bp/test.jsonl").unwrap();
        assert_eq!(qoff, 0, "queued_offset must not advance when spool is full");
    }
}
