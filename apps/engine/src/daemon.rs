//! Daemon mode (`connect` subcommand).
//!
//! Watches provider directories for file changes using the `notify` crate
//! (FSEvents on macOS, inotify on Linux) and ships new session data
//! incrementally. Designed for 24/7 operation with minimal resources:
//! - <10 MB RSS when idle
//! - 0% CPU when idle (blocked on kernel filesystem events)
//! - Single-threaded tokio runtime (current_thread)

use std::time::{Duration, Instant};

use anyhow::Result;

use crate::config::ShipperConfig;
use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::heartbeat;
use crate::pipeline::compressor::CompressionAlgo;
use crate::shipper;
use crate::shipping::client::ShipperClient;
use crate::state::db::open_db;
use crate::state::file_state::FileState;
use crate::state::spool::Spool;
use crate::watcher::SessionWatcher;

/// Configuration for the connect daemon.
pub struct ConnectConfig {
    pub shipper_config: ShipperConfig,
    pub algo: CompressionAlgo,
    pub flush_interval: Duration,
    pub fallback_scan_secs: u64,
    pub spool_replay_secs: u64,
}

/// Offline / connectivity state.
struct OfflineState {
    is_offline: bool,
    offline_since: Option<Instant>,
    consecutive_failures: u32,
}

impl OfflineState {
    fn new() -> Self {
        Self {
            is_offline: false,
            offline_since: None,
            consecutive_failures: 0,
        }
    }

    fn mark_offline(&mut self) {
        if !self.is_offline {
            self.is_offline = true;
            self.offline_since = Some(Instant::now());
        }
        self.consecutive_failures += 1;
    }

    fn mark_online(&mut self) -> Option<Duration> {
        if self.is_offline {
            let duration = self.offline_since.map(|t| t.elapsed());
            self.is_offline = false;
            self.offline_since = None;
            self.consecutive_failures = 0;
            duration
        } else {
            None
        }
    }
}

/// Run the connect daemon. This function blocks until shutdown signal.
pub async fn run(config: ConnectConfig) -> Result<()> {
    let start = Instant::now();

    // 1. Open state DB
    let conn = open_db(config.shipper_config.db_path.as_deref())?;

    // 2. Startup recovery
    let recovered = shipper::run_startup_recovery(&conn)?;
    if recovered > 0 {
        tracing::info!("Recovered {} unacked file gaps into spool", recovered);
    }

    // 2b. Prune stale file_state entries (files deleted from disk, >30 days old)
    {
        let fs = FileState::new(&conn);
        match fs.prune_stale(30) {
            Ok(n) if n > 0 => tracing::info!("Pruned {} stale file_state entries", n),
            Ok(_) => {}
            Err(e) => tracing::warn!("file_state prune error: {}", e),
        }
    }

    // 3. Create HTTP client
    let client = ShipperClient::with_compression(&config.shipper_config, config.algo)?;
    tracing::info!("Shipping to: {}", client.ingest_url());

    // 4. Discover providers
    let providers = discovery::get_providers();
    if providers.is_empty() {
        tracing::warn!("No provider directories found — nothing to watch");
        return Ok(());
    }
    for p in &providers {
        tracing::info!("Provider {}: {}", p.name, p.root.display());
    }

    // 5. Create error tracker (shared across all ship operations)
    let tracker = ConsecutiveErrorTracker::new();

    // 6. Initial full scan (catch up on anything missed while stopped)
    tracing::info!("Running initial full scan...");
    let (files, events) = shipper::full_scan(&providers, &conn, &client, config.algo, Some(&tracker)).await?;
    tracing::info!(
        "Initial scan: shipped {} files, {} events in {:.1}s",
        files,
        events,
        start.elapsed().as_secs_f64()
    );

    // 7. Replay any pending spool entries
    let (spool_ok, spool_fail) = shipper::replay_spool_batch(&conn, &client, config.algo, 100).await?;
    if spool_ok > 0 || spool_fail > 0 {
        tracing::info!("Spool replay: {} shipped, {} failed", spool_ok, spool_fail);
    }

    // 8. Start file watcher
    let mut watcher = SessionWatcher::new(&providers)?;
    tracing::info!("Daemon ready — watching for file changes (flush interval: {:?})", config.flush_interval);

    // 9. Main event loop
    let fallback_interval = Duration::from_secs(config.fallback_scan_secs.max(10));
    let spool_interval = Duration::from_secs(config.spool_replay_secs.max(5));
    let health_check_interval = Duration::from_secs(60);
    let prune_interval = Duration::from_secs(24 * 3600);
    let heartbeat_interval = Duration::from_secs(5 * 60);

    let mut fallback_timer = tokio::time::interval(fallback_interval);
    fallback_timer.tick().await; // consume first immediate tick

    let mut spool_timer = tokio::time::interval(spool_interval);
    spool_timer.tick().await; // consume first immediate tick

    let mut health_timer = tokio::time::interval(health_check_interval);
    health_timer.tick().await; // consume first immediate tick

    let mut prune_timer = tokio::time::interval(prune_interval);
    prune_timer.tick().await; // consume first immediate tick

    let mut heartbeat_timer = tokio::time::interval(heartbeat_interval);
    heartbeat_timer.tick().await; // consume first immediate tick

    let mut offline = OfflineState::new();
    let mut last_ship_at: Option<String> = None;

    // Resolve claude dir for status file
    let claude_dir = {
        let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
        let base = std::env::var("CLAUDE_CONFIG_DIR")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|_| std::path::PathBuf::from(home).join(".claude"));
        base
    };

    loop {
        tokio::select! {
            biased;

            // Shutdown signals
            _ = shutdown_signal() => {
                tracing::info!("Shutdown signal received, exiting gracefully...");
                break;
            }

            // Health check when offline (every 60s)
            _ = health_timer.tick(), if offline.is_offline => {
                match client.health_check().await {
                    Ok(true) => {
                        if let Some(duration) = offline.mark_online() {
                            tracing::info!(
                                "Back online after {:.0}s — resuming shipping",
                                duration.as_secs_f64()
                            );
                        }
                    }
                    _ => {
                        tracing::debug!("Still offline (health check failed)");
                    }
                }
            }

            // File change events (primary path) — skip when offline
            batch = watcher.next_batch(config.flush_interval), if !offline.is_offline => {
                match batch {
                    Some(paths) if !paths.is_empty() => {
                        let (had_connect_error, shipped_events) = ship_batch(&paths, &providers, &conn, &client, config.algo, &tracker).await;
                        if had_connect_error {
                            offline.mark_offline();
                            tracing::warn!(
                                "Connection error — entering offline mode, will retry every 60s"
                            );
                        } else if shipped_events > 0 {
                            last_ship_at = Some(chrono::Utc::now().to_rfc3339());
                        }
                    }
                    Some(_) => {} // empty batch, timer elapsed with no events
                    None => {
                        tracing::warn!("File watcher stopped unexpectedly");
                        break;
                    }
                }
            }

            // Periodic full scan (catch missed events) — skip when offline
            _ = fallback_timer.tick(), if !offline.is_offline => {
                tracing::debug!("Running fallback full scan...");
                match shipper::full_scan(&providers, &conn, &client, config.algo, Some(&tracker)).await {
                    Ok((f, e)) => {
                        if f > 0 {
                            tracing::info!("Fallback scan: shipped {} files, {} events", f, e);
                        }
                    }
                    Err(e) => {
                        // Check if it's a connection error → go offline
                        let msg = e.to_string();
                        if msg.contains("connect") || msg.contains("ConnectError") {
                            offline.mark_offline();
                            tracing::warn!("Fallback scan connect error — entering offline mode");
                        } else {
                            tracing::warn!("Fallback scan error: {}", e);
                        }
                    }
                }
            }

            // Spool replay (retry failed shipments) — skip when offline
            _ = spool_timer.tick(), if !offline.is_offline => {
                match shipper::replay_spool_batch(&conn, &client, config.algo, 50).await {
                    Ok((ok, fail)) => {
                        if ok > 0 || fail > 0 {
                            tracing::info!("Spool replay: {} shipped, {} failed", ok, fail);
                        }
                    }
                    Err(e) => tracing::warn!("Spool replay error: {}", e),
                }
            }

            // Daily: prune stale file_state entries
            _ = prune_timer.tick() => {
                let fs = FileState::new(&conn);
                match fs.prune_stale(30) {
                    Ok(n) if n > 0 => tracing::info!("Daily prune: removed {} stale file_state entries", n),
                    Ok(_) => {}
                    Err(e) => tracing::warn!("Daily prune error: {}", e),
                }
            }

            // Periodic heartbeat
            _ = heartbeat_timer.tick() => {
                let spool = Spool::new(&conn);
                let stats = heartbeat::HeartbeatStats {
                    spool: &spool,
                    tracker: &tracker,
                    is_offline: offline.is_offline,
                    last_ship_at: last_ship_at.clone(),
                };
                let payload = heartbeat::HeartbeatPayload::build(&stats);
                heartbeat::write_status_file(&payload, &claude_dir);
                if !offline.is_offline {
                    if let Err(e) = heartbeat::send_heartbeat(&client, &payload).await {
                        tracing::debug!("Heartbeat send failed: {}", e);
                    }
                }
            }
        }
    }

    tracing::info!("Daemon shutdown complete");
    Ok(())
}

/// Ship a batch of changed file paths.
/// Returns (had_connect_error, total_events_shipped).
async fn ship_batch(
    paths: &[std::path::PathBuf],
    providers: &[ProviderConfig],
    conn: &rusqlite::Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
    tracker: &ConsecutiveErrorTracker,
) -> (bool, usize) {
    let batch_start = Instant::now();
    let mut shipped = 0usize;
    let mut events = 0usize;
    let mut had_connect_error = false;

    for path in paths {
        let provider = match discovery::provider_for_path(path, providers) {
            Some(p) => p,
            None => {
                tracing::debug!("Skipping file outside known providers: {}", path.display());
                continue;
            }
        };

        match shipper::prepare_file(path, provider, algo, conn) {
            Ok(Some(item)) => {
                match shipper::ship_and_record(item, client, conn, Some(tracker)).await {
                    Ok((e, is_connect_err)) => {
                        if is_connect_err {
                            had_connect_error = true;
                        } else if e > 0 {
                            shipped += 1;
                            events += e;
                        }
                    }
                    Err(e) => {
                        // Unexpected error (not a ShipResult variant)
                        if tracker.record_error() {
                            tracing::warn!("Error shipping {}: {}", path.display(), e);
                        }
                    }
                }
            }
            Ok(None) => {} // no new content
            Err(e) => {
                if tracker.record_error() {
                    tracing::warn!("Error preparing {}: {}", path.display(), e);
                }
            }
        }
    }

    if shipped > 0 {
        tracing::info!(
            "Shipped {} files ({} events) in {:.0}ms",
            shipped,
            events,
            batch_start.elapsed().as_millis()
        );
    }

    (had_connect_error, events)
}

/// Wait for SIGINT (Ctrl-C) or SIGTERM.
async fn shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};

    let ctrl_c = tokio::signal::ctrl_c();
    let mut sigterm = signal(SignalKind::terminate())
        .expect("failed to install SIGTERM handler");

    tokio::select! {
        _ = ctrl_c => {},
        _ = sigterm.recv() => {},
    }
}
