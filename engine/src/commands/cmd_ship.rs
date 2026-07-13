//! Ship command — one-shot session shipping and single-file shipping.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use rayon::prelude::*;

use crate::config::ShipperConfig;
use crate::discovery;
use crate::opencode_db;
use crate::pipeline::compressor::CompressionAlgo;
use crate::shipper;
use crate::shipping::client::ShipperClient;
use crate::state::db::open_db;
use crate::state::file_identity::identity_from_metadata;
use crate::state::file_state::FileState;
use crate::state::spool::Spool;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

pub fn detect_provider_for_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
) -> anyhow::Result<String> {
    if let Some(p) = provider_override {
        return Ok(p.to_lowercase());
    }

    let providers = discovery::get_providers();
    if let Some(p) = discovery::provider_for_path(path, &providers) {
        return Ok(p.to_string());
    }

    let ext = path
        .extension()
        .and_then(|e| e.to_str())
        .map(|e| e.to_lowercase());

    match ext.as_deref() {
        Some("jsonl") => Ok("claude".to_string()),
        Some("json") => Ok("antigravity".to_string()),
        _ => anyhow::bail!(
            "Unable to determine provider for {} (use --provider)",
            path.display()
        ),
    }
}

pub fn recent_dead_letter_json(
    spool: &Spool<'_>,
    limit: usize,
) -> anyhow::Result<Vec<serde_json::Value>> {
    Ok(spool
        .recent_dead(limit)?
        .into_iter()
        .map(|entry| {
            serde_json::json!({
                "provider": entry.provider,
                "file_path": entry.file_path,
                "start_offset": entry.start_offset,
                "end_offset": entry.end_offset,
                "range_bytes": entry.end_offset.saturating_sub(entry.start_offset),
                "session_id": entry.session_id,
                "last_error": entry.last_error,
                "created_at": entry.created_at,
            })
        })
        .collect())
}

pub fn print_recent_dead_letters(
    spool: &Spool<'_>,
    limit: usize,
    printer: impl Fn(&str),
) -> anyhow::Result<()> {
    for entry in spool.recent_dead(limit)? {
        let reason = entry.last_error.as_deref().unwrap_or("no error recorded");
        let line = format!(
            "  - [{}] {} {}..{} ({} bytes): {}",
            entry.provider,
            entry.file_path,
            entry.start_offset,
            entry.end_offset,
            entry.end_offset.saturating_sub(entry.start_offset),
            reason
        );
        printer(&line);
    }
    Ok(())
}

pub fn reported_ship_events(
    replay_events_shipped: usize,
    shipped_events: usize,
    require_reply_evidence: bool,
    reply_evidence_pending: bool,
) -> usize {
    if require_reply_evidence && reply_evidence_pending {
        return 0;
    }
    replay_events_shipped + shipped_events
}

async fn ship_path_storage_v2(
    conn: &mut rusqlite::Connection,
    client: &ShipperClient,
    capabilities: &crate::shipping::storage_v2::StorageV2Capabilities,
    path: &std::path::Path,
    provider: &str,
    session_id_override: Option<&str>,
    require_reply_evidence: bool,
    request_timeout: Duration,
) -> anyhow::Result<(usize, bool)> {
    let mut events_shipped = 0usize;
    loop {
        let prepared = if provider == "opencode" && opencode_db::is_opencode_database_path(path) {
            crate::storage_v2_shipper::prepare_next_opencode_envelope(conn, capabilities, path)?
        } else {
            crate::storage_v2_shipper::prepare_next_envelope(
                conn,
                capabilities,
                path,
                provider,
                session_id_override,
            )?
        };
        let Some(prepared) = prepared else {
            return Ok((events_shipped, false));
        };
        if require_reply_evidence && !prepared.has_reply_evidence {
            return Ok((events_shipped, true));
        }
        let outcome = crate::storage_v2_shipper::ship_prepared_envelope(
            conn,
            client,
            capabilities,
            prepared,
            "repair",
            request_timeout,
        )
        .await?;
        events_shipped += outcome.events_shipped;
        if !outcome.has_more {
            return Ok((events_shipped, false));
        }
    }
}

fn capability_error_is_unreachable_transport(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<reqwest::Error>()
            .is_some_and(reqwest::Error::is_connect)
    })
}

// ---------------------------------------------------------------------------
// cmd_ship — scan all providers and ship new events
// ---------------------------------------------------------------------------

pub async fn cmd_ship(
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    workers: usize,
    dry_run: bool,
    json_output: bool,
    algo: CompressionAlgo,
    max_batch_bytes: Option<u64>,
    machine_name: Option<&str>,
) -> anyhow::Result<()> {
    let start = Instant::now();

    // Load config
    let config = ShipperConfig::from_env()?.with_overrides(
        url,
        token,
        db_path,
        if workers > 0 { Some(workers) } else { None },
        machine_name,
        max_batch_bytes,
    );
    crate::pipeline::compressor::set_machine_name(&config.machine_name);

    if !json_output {
        eprintln!("Shipping to: {}", config.api_url);
        if dry_run {
            eprintln!("DRY RUN — will parse and compress but not POST");
        }
    }

    // Open state DB
    let mut conn = open_db(config.db_path.as_deref())?;

    if !dry_run {
        let client = ShipperClient::with_compression(&config, algo)?;
        let storage_v2 = client
            .storage_v2_capabilities(&config.machine_name, Some(Duration::from_secs(5)))
            .await?;
        if let Some(capabilities) = storage_v2.filter(|item| item.cutover) {
            let providers = discovery::get_providers();
            let mut all_files = discovery::discover_all_files(&providers);
            for pending in Spool::new(&conn).pending_paths_now(10_000)? {
                let path = PathBuf::from(&pending.file_path);
                if path.exists() && !all_files.iter().any(|(known, _)| known == &path) {
                    let provider = providers
                        .iter()
                        .find(|item| item.name == pending.provider)
                        .map(|item| item.name)
                        .unwrap_or("claude");
                    all_files.push((path, provider));
                }
            }
            let mut files_shipped = 0usize;
            let mut events_shipped = 0usize;
            for (path, provider) in &all_files {
                let (events, _) = ship_path_storage_v2(
                    &mut conn,
                    &client,
                    &capabilities,
                    path,
                    provider,
                    None,
                    false,
                    Duration::from_secs(config.timeout_seconds),
                )
                .await?;
                if events > 0 {
                    files_shipped += 1;
                    events_shipped += events;
                }
                let pending_entries = Spool::new(&conn)
                    .pending_entries_for_path_now(&path.to_string_lossy(), 10_000)?;
                for entry in pending_entries {
                    Spool::new(&conn).mark_shipped(entry.id)?;
                }
            }
            if json_output {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&serde_json::json!({
                        "status": "ok",
                        "protocol": "storage-v2",
                        "files_scanned": all_files.len(),
                        "files_shipped": files_shipped,
                        "events_shipped": events_shipped,
                        "dry_run": false,
                    }))?
                );
            } else {
                println!(
                    "Shipped {} events from {} files",
                    events_shipped, files_shipped
                );
            }
            return Ok(());
        }
    }

    let recovered = shipper::run_startup_recovery(&conn)?;
    if recovered > 0 && !json_output {
        eprintln!("Recovered {} unacked file gaps into spool", recovered);
    }

    // Discover files
    let providers = discovery::get_providers();
    let all_files = discovery::discover_all_files(&providers);
    if !json_output {
        eprintln!("Found {} session files", all_files.len());
    }

    // Filter to files with new content
    let file_state = FileState::new(&conn);
    let mut files_to_ship: Vec<(PathBuf, &'static str, u64)> = Vec::new(); // (path, provider, offset_to_start_from)
    let mut opencode_databases: Vec<PathBuf> = Vec::new();

    for (path, provider) in &all_files {
        if *provider == "opencode" && opencode_db::is_opencode_database_path(path) {
            opencode_databases.push(path.clone());
            continue;
        }

        let path_str = path.to_string_lossy();
        let current_offset = file_state.get_offset(&path_str)?;
        let metadata = match std::fs::metadata(path) {
            Ok(m) => m,
            Err(_) => continue,
        };
        let file_size = metadata.len();
        let current_identity = identity_from_metadata(&metadata);
        let stored_identity = file_state.get_file_identity(&path_str)?;

        if shipper::file_identity_changed_for_cursor(
            stored_identity.as_deref(),
            current_identity.as_deref(),
            current_offset,
            file_state.get_queued_offset(&path_str)?,
        ) {
            tracing::warn!(
                "File replaced: {} (identity {:?} -> {:?}), resetting",
                path_str,
                stored_identity,
                current_identity
            );
            let stale = Spool::new(&conn).dead_letter_pending_for_path(
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
            files_to_ship.push((path.clone(), *provider, 0));
        } else if file_size < current_offset {
            // File truncated (got smaller than our offset) — reset and re-ship
            tracing::warn!(
                "File truncated: {} (was {}, now {}), resetting",
                path_str,
                current_offset,
                file_size
            );
            file_state.reset_offsets(&path_str)?;
            files_to_ship.push((path.clone(), *provider, 0));
        } else if file_size > current_offset {
            // New content available
            files_to_ship.push((path.clone(), *provider, current_offset));
        } else {
            file_state.record_file_identity_if_missing(&path_str, current_identity.as_deref())?;
        }
        // file_size == current_offset: no new content, skip
    }

    if !json_output {
        eprintln!("{} files with new content to ship", files_to_ship.len());
    }

    if files_to_ship.is_empty() && opencode_databases.is_empty() {
        let spool = Spool::new(&conn);
        let mut spool_pending = spool.pending_count()?;
        let mut spool_dead = spool.dead_count()?;
        let mut recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
        let mut spool_replayed = 0usize;

        if !dry_run && spool_pending > 0 {
            let client = ShipperClient::with_compression(&config, algo)?;
            let (ok, _failed) = shipper::replay_spool_batch_with_batch_bytes(
                &conn,
                &client,
                algo,
                100,
                config.max_batch_bytes,
            )
            .await?;
            spool_replayed = ok;
            spool_pending = spool.pending_count()?;
            spool_dead = spool.dead_count()?;
            recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
        }

        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "files_scanned": all_files.len(),
                "files_shipped": 0,
                "events_shipped": 0,
                "spool_replayed": spool_replayed,
                "spool_pending": spool_pending,
                "spool_dead": spool_dead,
                "recent_dead_letters": recent_dead_letters,
                "total_seconds": start.elapsed().as_secs_f64(),
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else {
            eprintln!("Nothing to ship — all files up to date.");
            if spool_replayed > 0 {
                eprintln!("Spool replayed: {}", spool_replayed);
            }
            if spool_dead > 0 {
                eprintln!("Dead-lettered ranges retained: {}", spool_dead);
                print_recent_dead_letters(&spool, 3, |line| eprintln!("{}", line))?;
            }
        }
        return Ok(());
    }

    // Create HTTP client (unless dry run)
    let client = if !dry_run {
        Some(ShipperClient::with_compression(&config, algo)?)
    } else {
        None
    };

    // Configure rayon thread pool
    let num_workers = if workers > 0 {
        workers
    } else {
        num_cpus::get()
    };
    rayon::ThreadPoolBuilder::new()
        .num_threads(num_workers)
        .build_global()
        .ok(); // Ignore if already initialized

    if !json_output {
        eprintln!(
            "Processing with {} workers{}",
            num_workers,
            if dry_run { " (dry run)" } else { "" }
        );
    }

    // Phase 1: Parse + compress in parallel (CPU-bound, embarrassingly parallel)
    // Collect results for sequential state writes + HTTP shipping.
    let files_done = AtomicUsize::new(0);
    let bytes_done = AtomicU64::new(0);
    let events_done = AtomicUsize::new(0);
    let total_files = files_to_ship.len();

    let prepared_files: Vec<Option<shipper::PreparedFile>> = files_to_ship
        .par_iter()
        .map(|(path, provider, offset)| {
            let path_str = path.to_string_lossy().to_string();
            if std::fs::metadata(path).is_err() {
                return None;
            }

            let prepared = match shipper::prepare_path_from_offset(
                path,
                provider,
                *offset,
                algo,
                config.max_batch_bytes,
            ) {
                Ok(result) => result,
                Err(e) => {
                    tracing::warn!("Skip {}: {}", path_str, e);
                    return None;
                }
            };

            let prepared = match prepared {
                Some(prepared) => prepared,
                None => return None,
            };
            let event_count = prepared.total_event_count();
            let new_offset = prepared.new_offset;

            // Progress reporting
            let done = files_done.fetch_add(1, Ordering::Relaxed) + 1;
            bytes_done.fetch_add(new_offset.saturating_sub(*offset), Ordering::Relaxed);
            events_done.fetch_add(event_count, Ordering::Relaxed);

            if !json_output && (done % 1000 == 0 || done == total_files) {
                let elapsed = start.elapsed().as_secs_f64();
                let mb = bytes_done.load(Ordering::Relaxed) as f64 / 1_048_576.0;
                let evts = events_done.load(Ordering::Relaxed);
                eprintln!(
                    "  [{}/{}] {} events, {:.1} MB, {:.1} MB/s",
                    done,
                    total_files,
                    evts,
                    mb,
                    mb / elapsed,
                );
            }

            Some(prepared)
        })
        .collect();

    let parse_compress_elapsed = start.elapsed();
    if !json_output {
        eprintln!(
            "Parse+compress done in {:.1}s, writing state...",
            parse_compress_elapsed.as_secs_f64()
        );
    }

    // Phase 2: Sequential state writes + HTTP shipping
    let mut files_shipped = 0usize;
    let mut events_shipped = 0usize;
    let mut bytes_shipped = 0u64;
    let mut files_failed = 0usize;
    let mut files_skipped = 0usize;

    if dry_run {
        for prepared in &prepared_files {
            match prepared {
                Some(prepared) => {
                    files_shipped += 1;
                    events_shipped += prepared.total_event_count();
                    bytes_shipped += prepared.new_offset - prepared.offset;
                }
                None => {
                    files_skipped += 1;
                }
            }
        }
    }

    // Live HTTP shipping (skip if dry run — already handled above)
    if !dry_run {
        for prepared in prepared_files {
            let prepared = match prepared {
                Some(prepared) => prepared,
                None => {
                    files_skipped += 1;
                    continue;
                }
            };

            let client = client.as_ref().unwrap();
            let outcome = shipper::ship_prepared_file(prepared, client, &conn, None, None).await?;
            if outcome.events_shipped > 0 || outcome.dead_lettered > 0 {
                files_shipped += 1;
            }
            events_shipped += outcome.events_shipped;
            bytes_shipped += outcome.bytes_shipped;
            if !outcome.fully_processed {
                files_failed += 1;
            }
        }

        for path in &opencode_databases {
            let client = client.as_ref().unwrap();
            let (sessions, events) = shipper::ship_opencode_database(
                path,
                &conn,
                client,
                algo,
                config.max_batch_bytes,
                None,
                None,
            )
            .await?;
            if sessions > 0 {
                files_shipped += sessions;
                events_shipped += events;
            }
        }
    } // end if !dry_run

    // Replay spool (if not dry run)
    let mut spool_replayed = 0usize;
    if !dry_run {
        let client = client.as_ref().unwrap();
        let (ok, _failed) = shipper::replay_spool_batch_with_batch_bytes(
            &conn,
            client,
            algo,
            100,
            config.max_batch_bytes,
        )
        .await?;
        spool_replayed = ok;
    }

    let total_elapsed = start.elapsed();
    let spool = Spool::new(&conn);
    let spool_pending = spool.pending_count()?;
    let spool_dead = spool.dead_count()?;
    let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;

    if json_output {
        let summary = serde_json::json!({
            "status": "ok",
            "files_scanned": all_files.len(),
            "files_shipped": files_shipped,
            "files_failed": files_failed,
            "files_skipped": files_skipped,
            "events_shipped": events_shipped,
            "bytes_shipped": bytes_shipped,
            "spool_replayed": spool_replayed,
            "spool_pending": spool_pending,
            "spool_dead": spool_dead,
            "recent_dead_letters": recent_dead_letters,
            "total_seconds": total_elapsed.as_secs_f64(),
            "throughput_mb_s": bytes_shipped as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
            "dry_run": dry_run,
        });
        println!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        eprintln!("\n=== Ship Results ===");
        eprintln!("Files shipped: {}", files_shipped);
        eprintln!("Events shipped: {}", events_shipped);
        eprintln!(
            "Bytes shipped: {:.2} MB",
            bytes_shipped as f64 / 1_048_576.0
        );
        if files_failed > 0 {
            eprintln!("Files failed (spooled): {}", files_failed);
        }
        if spool_replayed > 0 {
            eprintln!("Spool replayed: {}", spool_replayed);
        }
        if spool_dead > 0 {
            eprintln!("Dead-lettered ranges retained: {}", spool_dead);
            print_recent_dead_letters(&spool, 3, |line| eprintln!("{}", line))?;
        }
        eprintln!("Total: {:.3}s", total_elapsed.as_secs_f64());
        if bytes_shipped > 0 {
            eprintln!(
                "Throughput: {:.1} MB/s",
                bytes_shipped as f64 / 1_048_576.0 / total_elapsed.as_secs_f64()
            );
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// cmd_ship_file — ship a single explicit file
// ---------------------------------------------------------------------------

pub async fn cmd_ship_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    dry_run: bool,
    json_output: bool,
    algo: CompressionAlgo,
    max_batch_bytes: Option<u64>,
    session_id_override: Option<&str>,
    require_reply_evidence: bool,
    machine_name: Option<&str>,
) -> anyhow::Result<()> {
    if !path.exists() {
        anyhow::bail!("File not found: {}", path.display());
    }

    let provider = detect_provider_for_file(path, provider_override)?;

    let config = ShipperConfig::from_env()?.with_overrides(
        url,
        token,
        db_path,
        None,
        machine_name,
        max_batch_bytes,
    );
    crate::pipeline::compressor::set_machine_name(&config.machine_name);

    if !json_output {
        eprintln!("Shipping file: {}", path.display());
        eprintln!("Provider: {}", provider);
        if dry_run {
            eprintln!("DRY RUN — will parse and compress but not POST");
        }
    }

    let mut conn = open_db(config.db_path.as_deref())?;

    if !dry_run {
        let client = ShipperClient::with_compression(&config, algo)?;
        let storage_v2 = match client
            .storage_v2_capabilities(&config.machine_name, Some(Duration::from_secs(5)))
            .await
        {
            Ok(value) => value,
            Err(error) if capability_error_is_unreachable_transport(&error) => {
                tracing::warn!(
                    %error,
                    "Runtime Host is unreachable; preserving explicit file through durable spool"
                );
                None
            }
            Err(error) => return Err(error),
        };
        if let Some(capabilities) = storage_v2.filter(|item| item.cutover) {
            let (events_shipped, reply_evidence_pending) = ship_path_storage_v2(
                &mut conn,
                &client,
                &capabilities,
                path,
                &provider,
                session_id_override,
                require_reply_evidence,
                Duration::from_secs(config.timeout_seconds),
            )
            .await?;
            if json_output {
                println!(
                    "{}",
                    serde_json::to_string_pretty(&serde_json::json!({
                        "status": "ok",
                        "protocol": "storage-v2",
                        "file": path.display().to_string(),
                        "provider": provider,
                        "events_shipped": events_shipped,
                        "dry_run": false,
                        "reply_evidence_pending": reply_evidence_pending,
                    }))?
                );
            } else if reply_evidence_pending {
                println!("No new events with reply evidence");
            } else {
                println!("Shipped {} events", events_shipped);
            }
            return Ok(());
        }
    }

    if provider == "opencode" && opencode_db::is_opencode_database_path(path) {
        let (sessions_shipped, events_shipped) = if dry_run {
            let sessions = opencode_db::list_opencode_sessions(path)?;
            let events = sessions
                .iter()
                .filter_map(|session| {
                    opencode_db::parse_opencode_session(path, &session.provider_session_id).ok()
                })
                .map(|parsed| parsed.events.len())
                .sum();
            (sessions.len(), events)
        } else {
            let client = ShipperClient::with_compression(&config, algo)?;
            shipper::ship_opencode_database(
                path,
                &conn,
                &client,
                algo,
                config.max_batch_bytes,
                None,
                None,
            )
            .await?
        };
        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "file": path.display().to_string(),
                "provider": "opencode",
                "files_shipped": sessions_shipped,
                "events_shipped": events_shipped,
                "dry_run": dry_run,
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else if dry_run {
            println!(
                "Would ship {} OpenCode session(s), {} events",
                sessions_shipped, events_shipped
            );
        } else {
            println!(
                "Shipped {} OpenCode session(s), {} events",
                sessions_shipped, events_shipped
            );
        }
        return Ok(());
    }

    let mut prepared = shipper::prepare_file_batches(
        path,
        &provider,
        algo,
        &conn,
        config.max_batch_bytes,
        session_id_override,
    )?;
    let mut reply_evidence_pending = false;
    if require_reply_evidence
        && prepared
            .as_ref()
            .is_some_and(|item| !item.has_reply_evidence)
    {
        reply_evidence_pending = true;
        prepared = None;
    }
    let mut replay_events_shipped = 0usize;
    let mut replay_failed = 0usize;
    let mut replay_had_connect_error = false;
    let mut replay_blocked_by_spool_backpressure = false;

    if prepared.is_none() && !dry_run {
        let path_str = path.to_string_lossy().to_string();
        let recovered_gap = shipper::recover_gap_for_path(&conn, path)?;
        let has_pending_replay = !Spool::new(&conn)
            .pending_entries_for_path_now(&path_str, 1)?
            .is_empty();
        replay_blocked_by_spool_backpressure =
            recovered_gap.had_gap && !recovered_gap.replay_ready && !has_pending_replay;
        if recovered_gap.replay_ready || has_pending_replay {
            let client = ShipperClient::with_compression(&config, algo)?;
            let replay = shipper::replay_spool_for_path_now_with_batch_bytes_and_parse_tracker(
                &conn,
                &client,
                algo,
                path,
                32,
                config.max_batch_bytes,
                None,
                None,
            )
            .await?;
            replay_events_shipped = replay.events_shipped;
            replay_failed = replay.failed;
            replay_had_connect_error = replay.had_connect_error;
            prepared = shipper::prepare_file_batches(
                path,
                &provider,
                algo,
                &conn,
                config.max_batch_bytes,
                session_id_override,
            )?;
            if require_reply_evidence
                && prepared
                    .as_ref()
                    .is_some_and(|item| !item.has_reply_evidence)
            {
                reply_evidence_pending = true;
                prepared = None;
            }
        }
    }

    if dry_run {
        let prepared = match prepared {
            Some(prepared) => prepared,
            None => {
                let spool = Spool::new(&conn);
                let spool_dead = spool.dead_count()?;
                let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
                if json_output {
                    let summary = serde_json::json!({
                        "status": "ok",
                        "file": path.display().to_string(),
                        "events_shipped": 0,
                        "spool_dead": spool_dead,
                        "recent_dead_letters": recent_dead_letters,
                        "dry_run": true,
                        "reply_evidence_pending": reply_evidence_pending,
                    });
                    println!("{}", serde_json::to_string_pretty(&summary)?);
                } else {
                    println!("No new events");
                    if spool_dead > 0 {
                        println!("Dead-lettered ranges retained: {}", spool_dead);
                        print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
                    }
                }
                return Ok(());
            }
        };
        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "file": prepared.path_str,
                "events_shipped": prepared.total_event_count(),
                "dry_run": true,
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else {
            println!("Would ship {} events", prepared.total_event_count());
        }
        return Ok(());
    }

    let client = ShipperClient::with_compression(&config, algo)?;
    let prepared = match prepared {
        Some(prepared) => prepared,
        None => {
            let spool = Spool::new(&conn);
            let spool_dead = spool.dead_count()?;
            let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;
            if json_output {
                let summary = serde_json::json!({
                    "status": "ok",
                    "file": path.display().to_string(),
                    "events_shipped": replay_events_shipped,
                    "spool_dead": spool_dead,
                    "recent_dead_letters": recent_dead_letters,
                    "dry_run": false,
                    "had_connect_error": replay_had_connect_error,
                    "replay_failed": replay_failed,
                    "spool_backpressure": replay_blocked_by_spool_backpressure,
                    "reply_evidence_pending": reply_evidence_pending,
                });
                println!("{}", serde_json::to_string_pretty(&summary)?);
            } else if replay_events_shipped > 0 {
                println!("Shipped {} events", replay_events_shipped);
                if replay_had_connect_error {
                    println!("Some replay work was deferred due to a connection error");
                } else if replay_failed > 0 {
                    println!(
                        "Some replay work was deferred after {} replay failure(s)",
                        replay_failed
                    );
                }
                if spool_dead > 0 {
                    println!("Dead-lettered ranges retained: {}", spool_dead);
                    print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
                }
            } else {
                if replay_blocked_by_spool_backpressure {
                    println!("Replay deferred because the local spool is at capacity");
                } else if replay_had_connect_error {
                    println!("Replay deferred due to connection error");
                } else if replay_failed > 0 {
                    println!("Replay deferred after {} replay failure(s)", replay_failed);
                } else {
                    println!("No new events");
                }
                if spool_dead > 0 {
                    println!("Dead-lettered ranges retained: {}", spool_dead);
                    print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
                }
            }
            return Ok(());
        }
    };
    let outcome = shipper::ship_prepared_file(prepared, &client, &conn, None, None).await?;
    let events_shipped = reported_ship_events(
        replay_events_shipped,
        outcome.events_shipped,
        require_reply_evidence,
        reply_evidence_pending,
    );
    let spool = Spool::new(&conn);
    let spool_dead = spool.dead_count()?;
    let recent_dead_letters = recent_dead_letter_json(&spool, 5)?;

    if json_output {
        let summary = serde_json::json!({
            "status": "ok",
            "file": path.display().to_string(),
            "events_shipped": events_shipped,
            "replay_events_shipped": replay_events_shipped,
            "spool_dead": spool_dead,
            "recent_dead_letters": recent_dead_letters,
            "dry_run": false,
        });
        println!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        println!("Shipped {} events", events_shipped);
        if spool_dead > 0 {
            println!("Dead-lettered ranges retained: {}", spool_dead);
            print_recent_dead_letters(&spool, 3, |line| println!("{}", line))?;
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Tests (moved from main.rs)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    fn make_claude_file(dir: &tempfile::TempDir, name: &str, content: &str) -> PathBuf {
        let path = dir.path().join(name);
        std::fs::write(&path, content).unwrap();
        path
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

    #[test]
    fn test_cmd_ship_file_dry_run_does_not_mutate_state() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "ffff1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"dry-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"dry-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            None,
            None,
            Some(&db_path),
            true,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            false,
            None,
        ))
        .unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        assert_eq!(
            file_state.get_offset(&file.to_string_lossy()).unwrap(),
            0,
            "dry-run should not advance acked_offset",
        );
        assert_eq!(
            file_state
                .get_queued_offset(&file.to_string_lossy())
                .unwrap(),
            0,
            "dry-run should not advance queued_offset",
        );
    }

    #[test]
    fn test_cmd_ship_file_unreachable_capability_host_spools_without_ack() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "aaaa1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"offline-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"offline-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");
        let file_len = std::fs::metadata(&file).unwrap().len();

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some("http://127.0.0.1:9"),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            false,
            None,
        ))
        .unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        assert_eq!(file_state.get_offset(&file_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&file_str).unwrap(), file_len);
        let pending = spool.pending_entries_for_path_now(&file_str, 10).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(
            (pending[0].start_offset, pending[0].end_offset),
            (0, file_len)
        );
    }

    #[test]
    fn test_cmd_ship_file_capability_responses_never_fallback_to_legacy() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        for status_line in [
            "401 Unauthorized",
            "426 Upgrade Required",
            "503 Service Unavailable",
        ] {
            let dir = tempfile::tempdir().unwrap();
            let file = make_claude_file(
                &dir,
                "bbbb1111-2222-3333-4444-555566667777.jsonl",
                concat!(
                    r#"{"type":"user","uuid":"blocked-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                    "\n",
                ),
            );
            let db_path = dir.path().join("engine.db");
            let (url, handle) = spawn_http_response_server(status_line, "{}");
            let result = rt.block_on(cmd_ship_file(
                &file,
                Some("claude"),
                Some(&url),
                Some("test-token"),
                Some(&db_path),
                false,
                true,
                CompressionAlgo::Gzip,
                None,
                None,
                false,
                None,
            ));
            handle.join().unwrap();
            assert!(
                result.is_err(),
                "{status_line} must not enter legacy shipping"
            );

            let conn = open_db(Some(&db_path)).unwrap();
            let file_str = file.to_string_lossy().to_string();
            assert_eq!(FileState::new(&conn).get_offset(&file_str).unwrap(), 0);
            assert_eq!(
                FileState::new(&conn).get_queued_offset(&file_str).unwrap(),
                0
            );
            assert!(Spool::new(&conn)
                .pending_entries_for_path_now(&file_str, 10)
                .unwrap()
                .is_empty());
        }
    }

    #[test]
    fn test_cmd_ship_file_recovers_explicit_file_gap_before_noop() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "eeee1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"gap-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"gap-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");
        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        let file_len = std::fs::metadata(&file).unwrap().len();

        file_state
            .set_queued_offset(
                &file_str,
                file_len,
                "claude",
                "eeee1111-2222-3333-4444-555566667777",
                "eeee1111-2222-3333-4444-555566667777",
            )
            .unwrap();

        let (url, handle) = spawn_http_response_server("200 OK", "{}");
        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some(&url),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            false,
            None,
        ))
        .unwrap();
        handle.join().unwrap();

        let reopened = open_db(Some(&db_path)).unwrap();
        let reopened_state = FileState::new(&reopened);
        let spool = Spool::new(&reopened);
        assert_eq!(reopened_state.get_offset(&file_str).unwrap(), file_len);
        assert_eq!(
            reopened_state.get_queued_offset(&file_str).unwrap(),
            file_len
        );
        assert!(spool
            .pending_entries_for_path_now(&file_str, 10)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn test_cmd_ship_file_require_reply_evidence_skips_user_only_partial_turn() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "replyevidence1111-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"reply-gap-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"reply-gap-2","timestamp":"2026-02-15T10:00:01Z","message":{"con"#,
            ),
        );
        let db_path = dir.path().join("engine.db");

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some("http://127.0.0.1:9"),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            true,
            None,
        ))
        .unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        assert_eq!(file_state.get_offset(&file_str).unwrap(), 0);
        assert_eq!(file_state.get_queued_offset(&file_str).unwrap(), 0);
        assert!(spool
            .pending_entries_for_path_now(&file_str, 10)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn test_cmd_ship_file_require_reply_evidence_ships_complete_turn() {
        let rt = tokio::runtime::Runtime::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let file = make_claude_file(
            &dir,
            "replyevidence2222-2222-3333-4444-555566667777.jsonl",
            concat!(
                r#"{"type":"user","uuid":"reply-ok-1","timestamp":"2026-02-15T10:00:00Z","message":{"content":"hello"}}"#,
                "\n",
                r#"{"type":"assistant","uuid":"reply-ok-2","timestamp":"2026-02-15T10:00:01Z","message":{"content":[{"type":"text","text":"hi"}]}}"#,
                "\n",
            ),
        );
        let db_path = dir.path().join("engine.db");
        let file_len = std::fs::metadata(&file).unwrap().len();
        let (url, handle) = spawn_http_response_server("200 OK", "{}");

        rt.block_on(cmd_ship_file(
            &file,
            Some("claude"),
            Some(&url),
            Some("test-token"),
            Some(&db_path),
            false,
            true,
            CompressionAlgo::Gzip,
            None,
            None,
            true,
            None,
        ))
        .unwrap();
        handle.join().unwrap();

        let conn = open_db(Some(&db_path)).unwrap();
        let file_state = FileState::new(&conn);
        let file_str = file.to_string_lossy().to_string();
        assert_eq!(file_state.get_offset(&file_str).unwrap(), file_len);
        assert_eq!(file_state.get_queued_offset(&file_str).unwrap(), file_len);
    }

    #[test]
    fn test_reported_ship_events_ignore_replay_while_reply_evidence_is_pending() {
        assert_eq!(reported_ship_events(3, 0, true, true), 0);
        assert_eq!(reported_ship_events(3, 0, true, false), 3);
        assert_eq!(reported_ship_events(0, 2, true, false), 2);
        assert_eq!(reported_ship_events(3, 2, false, true), 5);
    }
}
