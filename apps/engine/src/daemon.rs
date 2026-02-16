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
use crate::pipeline::compressor::CompressionAlgo;
use crate::shipper;
use crate::shipping::client::ShipperClient;
use crate::state::db::open_db;
use crate::watcher::SessionWatcher;

/// Configuration for the connect daemon.
pub struct ConnectConfig {
    pub shipper_config: ShipperConfig,
    pub algo: CompressionAlgo,
    pub flush_interval: Duration,
    pub fallback_scan_secs: u64,
    pub spool_replay_secs: u64,
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

    // 5. Initial full scan (catch up on anything missed while stopped)
    tracing::info!("Running initial full scan...");
    let (files, events) = shipper::full_scan(&providers, &conn, &client, config.algo).await?;
    tracing::info!(
        "Initial scan: shipped {} files, {} events in {:.1}s",
        files,
        events,
        start.elapsed().as_secs_f64()
    );

    // 6. Replay any pending spool entries
    let (spool_ok, spool_fail) = shipper::replay_spool_batch(&conn, &client, config.algo, 100).await?;
    if spool_ok > 0 || spool_fail > 0 {
        tracing::info!("Spool replay: {} shipped, {} failed", spool_ok, spool_fail);
    }

    // 7. Start file watcher
    let mut watcher = SessionWatcher::new(&providers)?;
    tracing::info!("Daemon ready — watching for file changes (flush interval: {:?})", config.flush_interval);

    // 8. Main event loop
    let fallback_interval = Duration::from_secs(config.fallback_scan_secs.max(10));
    let spool_interval = Duration::from_secs(config.spool_replay_secs.max(5));

    let mut fallback_timer = tokio::time::interval(fallback_interval);
    fallback_timer.tick().await; // consume first immediate tick

    let mut spool_timer = tokio::time::interval(spool_interval);
    spool_timer.tick().await; // consume first immediate tick

    loop {
        tokio::select! {
            biased;

            // Shutdown signals
            _ = shutdown_signal() => {
                tracing::info!("Shutdown signal received, exiting gracefully...");
                break;
            }

            // File change events (primary path)
            batch = watcher.next_batch(config.flush_interval) => {
                match batch {
                    Some(paths) if !paths.is_empty() => {
                        ship_batch(&paths, &providers, &conn, &client, config.algo).await;
                    }
                    Some(_) => {} // empty batch, timer elapsed with no events
                    None => {
                        tracing::warn!("File watcher stopped unexpectedly");
                        break;
                    }
                }
            }

            // Periodic full scan (catch missed events)
            _ = fallback_timer.tick() => {
                tracing::debug!("Running fallback full scan...");
                match shipper::full_scan(&providers, &conn, &client, config.algo).await {
                    Ok((f, e)) => {
                        if f > 0 {
                            tracing::info!("Fallback scan: shipped {} files, {} events", f, e);
                        }
                    }
                    Err(e) => tracing::warn!("Fallback scan error: {}", e),
                }
            }

            // Spool replay (retry failed shipments)
            _ = spool_timer.tick() => {
                match shipper::replay_spool_batch(&conn, &client, config.algo, 50).await {
                    Ok((ok, fail)) => {
                        if ok > 0 || fail > 0 {
                            tracing::info!("Spool replay: {} shipped, {} failed", ok, fail);
                        }
                    }
                    Err(e) => tracing::warn!("Spool replay error: {}", e),
                }
            }
        }
    }

    tracing::info!("Daemon shutdown complete");
    Ok(())
}

/// Ship a batch of changed file paths.
async fn ship_batch(
    paths: &[std::path::PathBuf],
    providers: &[ProviderConfig],
    conn: &rusqlite::Connection,
    client: &ShipperClient,
    algo: CompressionAlgo,
) {
    let batch_start = Instant::now();
    let mut shipped = 0usize;
    let mut events = 0usize;

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
                match shipper::ship_and_record(item, client, conn).await {
                    Ok(e) => {
                        if e > 0 {
                            shipped += 1;
                            events += e;
                        }
                    }
                    Err(e) => {
                        tracing::warn!("Error shipping {}: {}", path.display(), e);
                    }
                }
            }
            Ok(None) => {} // no new content
            Err(e) => {
                tracing::warn!("Error preparing {}: {}", path.display(), e);
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
