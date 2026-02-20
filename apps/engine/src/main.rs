mod bench;
mod config;
mod daemon;
mod discovery;
mod error_tracker;
mod heartbeat;
mod outbox;
mod pipeline;
mod shipper;
mod shipping;
mod state;
mod watcher;

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::time::Instant;

use clap::{Parser, Subcommand};
use rayon::prelude::*;

use config::ShipperConfig;
use pipeline::compressor::CompressionAlgo;
use shipping::client::{ShipResult, ShipperClient};
use state::db::open_db;
use state::file_state::FileState;
use state::spool::Spool;

fn parse_compression_algo(s: &str) -> anyhow::Result<CompressionAlgo> {
    match s.to_lowercase().as_str() {
        "gzip" | "gz" => Ok(CompressionAlgo::Gzip),
        "zstd" | "zstandard" => Ok(CompressionAlgo::Zstd),
        _ => anyhow::bail!("Unknown compression: {}. Use 'gzip' or 'zstd'", s),
    }
}

#[derive(Parser)]
#[command(name = "longhouse-engine", version, about = "Longhouse session shipper (Rust engine)")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Parse a JSONL file and report event counts (dev/validation tool)
    Parse {
        /// Path to session JSONL file
        path: PathBuf,

        /// Byte offset to start from
        #[arg(long, default_value = "0")]
        offset: u64,

        /// Print parsed events as JSON
        #[arg(long)]
        dump_events: bool,

        /// Also build + gzip-compress the ingest payload
        #[arg(long)]
        compress: bool,
    },

    /// Run multi-file benchmark (compare against Python profiling baselines)
    Bench {
        /// Scale level: L1 (1 file, largest), L2 (10% of files), L3 (100% of files)
        #[arg(long, default_value = "L1")]
        level: String,

        /// Include compression in benchmark
        #[arg(long)]
        compress: bool,

        /// Use rayon parallel file processing
        #[arg(long)]
        parallel: bool,

        /// Number of worker threads (default: num_cpus)
        #[arg(long, default_value = "0")]
        workers: usize,

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "gzip")]
        compression: String,
    },

    /// Daemon mode: watch for file changes and ship incrementally
    Connect {
        /// API URL override (default: from ~/.claude/longhouse-url)
        #[arg(long)]
        url: Option<String>,

        /// API token override (default: from ~/.claude/longhouse-device-token)
        #[arg(long)]
        token: Option<String>,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "zstd")]
        compression: String,

        /// Flush interval in milliseconds (how long to coalesce file events)
        #[arg(long, default_value = "500")]
        flush_ms: u64,

        /// Fallback full scan interval in seconds
        #[arg(long, default_value = "300")]
        fallback_scan_secs: u64,

        /// Spool replay interval in seconds
        #[arg(long, default_value = "30")]
        spool_replay_secs: u64,

        /// Log directory for rolling log files (default: ~/.claude/logs, or LONGHOUSE_LOG_DIR env)
        #[arg(long)]
        log_dir: Option<PathBuf>,
    },

    /// One-shot: scan all provider sessions and ship new events
    Ship {
        /// API URL override (default: from ~/.claude/longhouse-url)
        #[arg(long)]
        url: Option<String>,

        /// API token override (default: from ~/.claude/longhouse-device-token)
        #[arg(long)]
        token: Option<String>,

        /// SQLite DB path override
        #[arg(long)]
        db: Option<PathBuf>,

        /// Ship a single file instead of scanning all providers
        #[arg(long)]
        file: Option<PathBuf>,

        /// Provider name override when using --file (claude, codex, gemini)
        #[arg(long)]
        provider: Option<String>,

        /// Number of parallel workers (default: num_cpus)
        #[arg(long, default_value = "0")]
        workers: usize,

        /// Dry run: parse and compress but don't POST
        #[arg(long)]
        dry_run: bool,

        /// JSON output (machine readable)
        #[arg(long)]
        json: bool,

        /// Compression algorithm: gzip (default) or zstd
        #[arg(long, default_value = "gzip")]
        compression: String,
    },
}

fn resolve_log_dir(log_dir_arg: Option<&std::path::Path>) -> std::path::PathBuf {
    if let Some(p) = log_dir_arg {
        return p.to_path_buf();
    }
    if let Ok(dir) = std::env::var("LONGHOUSE_LOG_DIR") {
        return std::path::PathBuf::from(dir);
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    std::path::PathBuf::from(home).join(".claude").join("logs")
}

fn prune_old_logs(log_dir: &std::path::Path, keep_days: u64) {
    let cutoff = std::time::SystemTime::now()
        .checked_sub(std::time::Duration::from_secs(keep_days * 86400))
        .unwrap_or(std::time::UNIX_EPOCH);

    if let Ok(entries) = std::fs::read_dir(log_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            // tracing_appender rolling::daily creates files named "engine.log.YYYY-MM-DD"
            // Match by file_name prefix rather than extension
            let is_engine_log = path
                .file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("engine.log"))
                .unwrap_or(false);
            if is_engine_log {
                if let Ok(meta) = std::fs::metadata(&path) {
                    if let Ok(modified) = meta.modified() {
                        if modified < cutoff {
                            let _ = std::fs::remove_file(&path);
                        }
                    }
                }
            }
        }
    }
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    // For Connect (daemon) mode: use rolling file appender.
    // For all other commands: log to stderr as usual.
    let _guard;
    match &cli.command {
        Commands::Connect { log_dir, .. } => {
            let log_path = resolve_log_dir(log_dir.as_deref());
            std::fs::create_dir_all(&log_path)?;
            prune_old_logs(&log_path, 7);

            let file_appender = tracing_appender::rolling::daily(&log_path, "engine.log");
            let (non_blocking, guard) = tracing_appender::non_blocking(file_appender);
            _guard = Some(guard);

            tracing_subscriber::fmt()
                .with_writer(non_blocking)
                .with_ansi(false)
                .with_env_filter(
                    tracing_subscriber::EnvFilter::from_default_env()
                        .add_directive("longhouse_engine=info".parse()?),
                )
                .init();
        }
        _ => {
            _guard = None;
            tracing_subscriber::fmt()
                .with_env_filter(
                    tracing_subscriber::EnvFilter::from_default_env()
                        .add_directive("longhouse_engine=info".parse()?),
                )
                .init();
        }
    }

    match cli.command {
        Commands::Connect {
            url,
            token,
            db,
            compression,
            flush_ms,
            fallback_scan_secs,
            spool_replay_secs,
            log_dir: _,
        } => {
            let algo = parse_compression_algo(&compression)?;
            let shipper_config = ShipperConfig::from_env()?.with_overrides(
                url.as_deref(),
                token.as_deref(),
                db.as_deref(),
                None,
            );

            let connect_config = daemon::ConnectConfig {
                shipper_config,
                algo,
                flush_interval: std::time::Duration::from_millis(flush_ms),
                fallback_scan_secs,
                spool_replay_secs,
            };

            // Use current_thread runtime for minimal resource usage
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()?;
            rt.block_on(daemon::run(connect_config))?;
        }
        Commands::Parse {
            path,
            offset,
            dump_events,
            compress,
        } => {
            cmd_parse(&path, offset, dump_events, compress)?;
        }
        Commands::Bench {
            level,
            compress,
            parallel,
            workers,
            compression,
        } => {
            let algo = parse_compression_algo(&compression)?;
            cmd_bench(&level, compress, parallel, workers, algo)?;
        }
        Commands::Ship {
            url,
            token,
            db,
            file,
            provider,
            workers,
            dry_run,
            json,
            compression,
        } => {
            let algo = parse_compression_algo(&compression)?;
            // Build tokio runtime for async HTTP
            let rt = tokio::runtime::Runtime::new()?;
            if let Some(path) = file.as_ref() {
                rt.block_on(cmd_ship_file(
                    path,
                    provider.as_deref(),
                    url.as_deref(),
                    token.as_deref(),
                    db.as_deref(),
                    dry_run,
                    json,
                    algo,
                ))?;
            } else {
                rt.block_on(cmd_ship(
                    url.as_deref(),
                    token.as_deref(),
                    db.as_deref(),
                    workers,
                    dry_run,
                    json,
                    algo,
                ))?;
            }
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// ship subcommand
// ---------------------------------------------------------------------------

async fn cmd_ship(
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    workers: usize,
    dry_run: bool,
    json_output: bool,
    algo: CompressionAlgo,
) -> anyhow::Result<()> {
    let start = Instant::now();

    // Load config
    let config = ShipperConfig::from_env()?.with_overrides(
        url,
        token,
        db_path,
        if workers > 0 { Some(workers) } else { None },
    );

    if !json_output {
        eprintln!("Shipping to: {}", config.api_url);
        if dry_run {
            eprintln!("DRY RUN — will parse and compress but not POST");
        }
    }

    // Open state DB
    let conn = open_db(config.db_path.as_deref())?;

    // Startup recovery: re-enqueue gaps (queued > acked)
    {
        let file_state = FileState::new(&conn);
        let spool = Spool::new(&conn);
        let unacked = file_state.get_unacked_files()?;
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
        if !unacked.is_empty() && !json_output {
            eprintln!("Recovered {} unacked file gaps into spool", unacked.len());
        }
    }

    // Discover files
    let all_files = bench::discover_session_files();
    if !json_output {
        eprintln!("Found {} session files", all_files.len());
    }

    // Filter to files with new content
    let file_state = FileState::new(&conn);
    let mut files_to_ship: Vec<(PathBuf, u64)> = Vec::new(); // (path, offset_to_start_from)

    for path in &all_files {
        let path_str = path.to_string_lossy();
        let current_offset = file_state.get_offset(&path_str)?;
        let file_size = match std::fs::metadata(path) {
            Ok(m) => m.len(),
            Err(_) => continue,
        };

        if file_size < current_offset {
            // File truncated (got smaller than our offset) — reset and re-ship
            tracing::warn!(
                "File truncated: {} (was {}, now {}), resetting",
                path_str,
                current_offset,
                file_size
            );
            file_state.reset_offsets(&path_str)?;
            files_to_ship.push((path.clone(), 0));
        } else if file_size > current_offset {
            // New content available
            files_to_ship.push((path.clone(), current_offset));
        }
        // file_size == current_offset: no new content, skip
    }

    if !json_output {
        eprintln!(
            "{} files with new content to ship",
            files_to_ship.len()
        );
    }

    if files_to_ship.is_empty() {
        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "files_scanned": all_files.len(),
                "files_shipped": 0,
                "events_shipped": 0,
                "total_seconds": start.elapsed().as_secs_f64(),
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else {
            eprintln!("Nothing to ship — all files up to date.");
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
    let num_workers = if workers > 0 { workers } else { num_cpus::get() };
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

    struct ShipItem {
        path_str: String,
        offset: u64,
        new_offset: u64,
        event_count: usize,
        session_id: String,
        /// Compressed payload for live HTTP shipping. Empty for dry-run (saves memory).
        compressed: Vec<u8>,
    }

    let ship_items: Vec<Option<ShipItem>> = files_to_ship
        .par_iter()
        .map(|(path, offset)| {
            let path_str = path.to_string_lossy().to_string();
            let file_size = match std::fs::metadata(path) {
                Ok(m) => m.len(),
                Err(_) => return None,
            };

            let parse_result = match pipeline::parser::parse_session_file(path, *offset) {
                Ok(r) => r,
                Err(e) => {
                    tracing::warn!("Skip {}: {}", path_str, e);
                    return None;
                }
            };

            if parse_result.events.is_empty() {
                return None;
            }

            let event_count = parse_result.events.len();
            let new_offset = file_size;

            // Always compress (this is the real work we're benchmarking).
            // For dry-run, drop the result immediately to save memory.
            let compressed = match pipeline::compressor::build_and_compress_with(
                &parse_result.metadata.session_id,
                &parse_result.events,
                &parse_result.metadata,
                &path_str,
                "claude",
                algo,
            ) {
                Ok(c) => {
                    if dry_run { Vec::new() } else { c }
                }
                Err(e) => {
                    tracing::warn!("Compress failed {}: {}", path_str, e);
                    return None;
                }
            };

            let session_id = parse_result.metadata.session_id.clone();

            // Progress reporting
            let done = files_done.fetch_add(1, Ordering::Relaxed) + 1;
            bytes_done.fetch_add(file_size - offset, Ordering::Relaxed);
            events_done.fetch_add(event_count, Ordering::Relaxed);

            if !json_output && (done % 1000 == 0 || done == total_files) {
                let elapsed = start.elapsed().as_secs_f64();
                let mb = bytes_done.load(Ordering::Relaxed) as f64 / 1_048_576.0;
                let evts = events_done.load(Ordering::Relaxed);
                eprintln!(
                    "  [{}/{}] {} events, {:.1} MB, {:.1} MB/s",
                    done, total_files, evts, mb, mb / elapsed,
                );
            }

            Some(ShipItem {
                path_str,
                offset: *offset,
                new_offset,
                event_count,
                session_id,
                compressed,
            })
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
        // Batch all state writes in a single transaction (8000+ writes → ~10ms)
        conn.execute_batch("BEGIN")?;
        for item in &ship_items {
            match item {
                Some(item) => {
                    file_state.set_offset(
                        &item.path_str,
                        item.new_offset,
                        &item.session_id,
                        &item.session_id,
                        "claude",
                    )?;
                    files_shipped += 1;
                    events_shipped += item.event_count;
                    bytes_shipped += item.new_offset - item.offset;
                }
                None => {
                    files_skipped += 1;
                }
            }
        }
        conn.execute_batch("COMMIT")?;
    }

    // Live HTTP shipping (skip if dry run — already handled above)
    if !dry_run {
    for item in ship_items {
        let item = match item {
            Some(item) => item,
            None => {
                files_skipped += 1;
                continue;
            }
        };

        // Ship via HTTP
        let client = client.as_ref().unwrap();
        let result = client.ship(item.compressed).await;

        match result {
            ShipResult::Ok(_) => {
                file_state.set_offset(
                    &item.path_str,
                    item.new_offset,
                    &item.session_id,
                    &item.session_id,
                    "claude",
                )?;
                files_shipped += 1;
                events_shipped += item.event_count;
                bytes_shipped += item.new_offset - item.offset;
            }
            ShipResult::RateLimited | ShipResult::ServerError(_, _) | ShipResult::ConnectError(_) => {
                let spool = Spool::new(&conn);
                file_state.set_queued_offset(
                    &item.path_str,
                    item.new_offset,
                    "claude",
                    &item.session_id,
                    &item.session_id,
                )?;
                spool.enqueue(
                    "claude",
                    &item.path_str,
                    item.offset,
                    item.new_offset,
                    Some(&item.session_id),
                )?;
                files_failed += 1;

                let err_msg = match &result {
                    ShipResult::RateLimited => "rate limited".to_string(),
                    ShipResult::ServerError(code, body) => format!("{}:{}", code, &body[..body.len().min(200)]),
                    ShipResult::ConnectError(e) => e.clone(),
                    _ => unreachable!(),
                };
                tracing::warn!("Failed to ship {}: {}", item.path_str, err_msg);
            }
            ShipResult::ClientError(code, body) => {
                tracing::error!(
                    "Client error shipping {}: {} {}",
                    item.path_str,
                    code,
                    &body[..body.len().min(200)]
                );
                file_state.set_offset(
                    &item.path_str,
                    item.new_offset,
                    &item.session_id,
                    &item.session_id,
                    "claude",
                )?;
                files_skipped += 1;
            }
        }
    }
    } // end if !dry_run

    // Replay spool (if not dry run)
    let mut spool_replayed = 0usize;
    if !dry_run {
        let spool = Spool::new(&conn);
        let pending = spool.dequeue_batch(100)?;
        if !pending.is_empty() && !json_output {
            eprintln!("Replaying {} spool entries...", pending.len());
        }
        let client = client.as_ref().unwrap();
        for entry in &pending {
            // Re-read and re-parse the source file range
            let path = PathBuf::from(&entry.file_path);
            if !path.exists() {
                tracing::warn!("Spool file missing: {}", entry.file_path);
                spool.mark_failed_with_max(entry.id, "file missing", 0)?;
                continue;
            }

            let parse_result = match pipeline::parser::parse_session_file(&path, entry.start_offset) {
                Ok(r) => r,
                Err(e) => {
                    spool.mark_failed(entry.id, &e.to_string())?;
                    continue;
                }
            };

            if parse_result.events.is_empty() {
                spool.mark_shipped(entry.id)?;
                continue;
            }

            let compressed = pipeline::compressor::build_and_compress_with(
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
                    spool_replayed += 1;
                }
                ShipResult::ConnectError(_) => {
                    // Don't mark failed on connect error — will retry next cycle
                    break;
                }
                ShipResult::RateLimited | ShipResult::ServerError(_, _) => {
                    spool.mark_failed(entry.id, "server error during replay")?;
                }
                ShipResult::ClientError(code, _) => {
                    spool.mark_failed_with_max(entry.id, &format!("client error {}", code), 0)?;
                }
            }
        }

        // Cleanup old dead entries
        let cleaned = spool.cleanup()?;
        if cleaned > 0 {
            tracing::info!("Cleaned {} old spool entries", cleaned);
        }
    }

    let total_elapsed = start.elapsed();

    if json_output {
        let spool = Spool::new(&conn);
        let summary = serde_json::json!({
            "status": "ok",
            "files_scanned": all_files.len(),
            "files_shipped": files_shipped,
            "files_failed": files_failed,
            "files_skipped": files_skipped,
            "events_shipped": events_shipped,
            "bytes_shipped": bytes_shipped,
            "spool_replayed": spool_replayed,
            "spool_pending": spool.pending_count()?,
            "total_seconds": total_elapsed.as_secs_f64(),
            "throughput_mb_s": bytes_shipped as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
            "dry_run": dry_run,
        });
        println!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        eprintln!("\n=== Ship Results ===");
        eprintln!("Files shipped: {}", files_shipped);
        eprintln!("Events shipped: {}", events_shipped);
        eprintln!("Bytes shipped: {:.2} MB", bytes_shipped as f64 / 1_048_576.0);
        if files_failed > 0 {
            eprintln!("Files failed (spooled): {}", files_failed);
        }
        if spool_replayed > 0 {
            eprintln!("Spool replayed: {}", spool_replayed);
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

fn detect_provider_for_file(
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
        Some("json") => Ok("gemini".to_string()),
        _ => anyhow::bail!(
            "Unable to determine provider for {} (use --provider)",
            path.display()
        ),
    }
}

async fn cmd_ship_file(
    path: &std::path::Path,
    provider_override: Option<&str>,
    url: Option<&str>,
    token: Option<&str>,
    db_path: Option<&std::path::Path>,
    dry_run: bool,
    json_output: bool,
    algo: CompressionAlgo,
) -> anyhow::Result<()> {
    if !path.exists() {
        anyhow::bail!("File not found: {}", path.display());
    }

    let provider = detect_provider_for_file(path, provider_override)?;

    let config = ShipperConfig::from_env()?.with_overrides(url, token, db_path, None);

    if !json_output {
        eprintln!("Shipping file: {}", path.display());
        eprintln!("Provider: {}", provider);
        if dry_run {
            eprintln!("DRY RUN — will parse and compress but not POST");
        }
    }

    let conn = open_db(config.db_path.as_deref())?;

    let prepared = shipper::prepare_file(path, &provider, algo, &conn)?;
    let item = match prepared {
        Some(item) => item,
        None => {
            println!("No new events");
            return Ok(());
        }
    };

    if dry_run {
        let file_state = FileState::new(&conn);
        file_state.set_offset(
            &item.path_str,
            item.new_offset,
            &item.session_id,
            &item.session_id,
            &item.provider,
        )?;

        if json_output {
            let summary = serde_json::json!({
                "status": "ok",
                "file": item.path_str,
                "events_shipped": item.event_count,
                "dry_run": true,
            });
            println!("{}", serde_json::to_string_pretty(&summary)?);
        } else {
            println!("Shipped {} events", item.event_count);
        }
        return Ok(());
    }

    let client = ShipperClient::with_compression(&config, algo)?;
    let (events_shipped, _is_connect_err) =
        shipper::ship_and_record(item, &client, &conn, None).await?;

    if json_output {
        let summary = serde_json::json!({
            "status": "ok",
            "file": path.display().to_string(),
            "events_shipped": events_shipped,
            "dry_run": false,
        });
        println!("{}", serde_json::to_string_pretty(&summary)?);
    } else {
        println!("Shipped {} events", events_shipped);
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// parse subcommand
// ---------------------------------------------------------------------------

fn cmd_parse(path: &PathBuf, offset: u64, dump_events: bool, compress: bool) -> anyhow::Result<()> {
    let start = Instant::now();

    let file_size = std::fs::metadata(path)?.len();
    eprintln!(
        "Parsing {} ({:.2} MB) from offset {}",
        path.display(),
        file_size as f64 / 1_048_576.0,
        offset
    );

    let parse_start = Instant::now();
    let result = pipeline::parser::parse_session_file(path, offset)?;
    let parse_elapsed = parse_start.elapsed();

    eprintln!(
        "Parsed {} events, metadata extracted in {:.3}s",
        result.events.len(),
        parse_elapsed.as_secs_f64()
    );

    if let Some(ref meta) = result.metadata.cwd {
        eprintln!("  cwd: {}", meta);
    }
    if let Some(ref branch) = result.metadata.git_branch {
        eprintln!("  branch: {}", branch);
    }
    if let Some(ref started) = result.metadata.started_at {
        eprintln!("  started: {}", started);
    }
    if let Some(ref ended) = result.metadata.ended_at {
        eprintln!("  ended: {}", ended);
    }

    if dump_events {
        for event in &result.events {
            let json = serde_json::to_string(event)?;
            println!("{}", json);
        }
    }

    if compress {
        let compress_start = Instant::now();
        let source_path = path.to_string_lossy();
        let compressed = pipeline::compressor::build_and_compress(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
        )?;
        let compress_elapsed = compress_start.elapsed();

        // Calculate uncompressed size for ratio
        let payload = pipeline::compressor::build_payload(
            "test-session-id",
            &result.events,
            &result.metadata,
            &source_path,
            "claude",
        );
        let uncompressed = serde_json::to_vec(&payload)?;

        eprintln!(
            "Compressed: {:.2} MB JSON → {:.2} MB gzip ({:.1}x ratio) in {:.3}s",
            uncompressed.len() as f64 / 1_048_576.0,
            compressed.len() as f64 / 1_048_576.0,
            uncompressed.len() as f64 / compressed.len() as f64,
            compress_elapsed.as_secs_f64()
        );
    }

    let bytes_processed = file_size - offset;
    let total_elapsed = start.elapsed();
    eprintln!(
        "\nTotal: {:.3}s, {:.1} MB/s, {} events/s",
        total_elapsed.as_secs_f64(),
        bytes_processed as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
        (result.events.len() as f64 / total_elapsed.as_secs_f64()) as u64
    );

    // Machine-readable JSON summary
    let summary = serde_json::json!({
        "file": path.display().to_string(),
        "file_size_bytes": file_size,
        "offset": offset,
        "bytes_processed": bytes_processed,
        "event_count": result.events.len(),
        "parse_seconds": parse_elapsed.as_secs_f64(),
        "total_seconds": total_elapsed.as_secs_f64(),
        "throughput_mb_s": bytes_processed as f64 / 1_048_576.0 / total_elapsed.as_secs_f64(),
        "events_per_sec": (result.events.len() as f64 / total_elapsed.as_secs_f64()) as u64,
        "metadata": {
            "cwd": result.metadata.cwd,
            "git_branch": result.metadata.git_branch,
            "started_at": result.metadata.started_at.map(|t| t.to_rfc3339()),
            "ended_at": result.metadata.ended_at.map(|t| t.to_rfc3339()),
        }
    });
    println!("{}", serde_json::to_string_pretty(&summary)?);

    Ok(())
}

// ---------------------------------------------------------------------------
// bench subcommand
// ---------------------------------------------------------------------------

fn cmd_bench(level: &str, compress: bool, parallel: bool, workers: usize, algo: CompressionAlgo) -> anyhow::Result<()> {
    eprintln!("Discovering session files...");
    let all_files = bench::discover_session_files();
    eprintln!("Found {} non-empty JSONL files", all_files.len());

    let total_bytes: u64 = all_files
        .iter()
        .filter_map(|p| std::fs::metadata(p).ok())
        .map(|m| m.len())
        .sum();
    eprintln!("Total: {:.2} GB on disk", total_bytes as f64 / 1_073_741_824.0);

    let files: Vec<PathBuf> = match level.to_uppercase().as_str() {
        "L1" => {
            // Single largest file
            all_files.into_iter().take(1).collect()
        }
        "L2" => {
            // 10% sample (top files by size)
            let count = (all_files.len() + 9) / 10;
            all_files.into_iter().take(count).collect()
        }
        "L3" => {
            // All files
            all_files
        }
        _ => {
            anyhow::bail!("Unknown level: {}. Use L1, L2, or L3", level);
        }
    };

    let sample_bytes: u64 = files
        .iter()
        .filter_map(|p| std::fs::metadata(p).ok())
        .map(|m| m.len())
        .sum();

    let num_workers = if workers == 0 {
        num_cpus::get()
    } else {
        workers
    };

    eprintln!(
        "\n--- {} benchmark: {} files, {:.2} GB ---",
        level.to_uppercase(),
        files.len(),
        sample_bytes as f64 / 1_073_741_824.0
    );
    eprintln!(
        "Mode: {}, Compress: {}",
        if parallel {
            format!("parallel ({} workers)", num_workers)
        } else {
            "sequential".to_string()
        },
        if compress { "yes" } else { "parse-only" }
    );

    let result = if parallel {
        bench::run_benchmark_parallel_with(&files, compress, num_workers, algo)
    } else {
        bench::run_benchmark_with(&files, compress, algo)
    };
    result.print_summary();

    Ok(())
}
