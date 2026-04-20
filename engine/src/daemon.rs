//! Daemon mode (`connect` subcommand).
//!
//! Watches provider directories for file changes using the `notify` crate
//! (FSEvents on macOS, inotify on Linux) and ships new session data
//! incrementally. Designed for 24/7 operation with minimal resources:
//! - <10 MB RSS when idle
//! - 0% CPU when idle (blocked on kernel filesystem events)
//! - Current-thread tokio runtime, with blocking file work offloaded

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::Result;
use tokio::task::JoinSet;

use crate::config::{self, ShipperConfig};
use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::heartbeat;
use crate::outbox;
use crate::pipeline::compressor::CompressionAlgo;
use crate::scheduler::{PathJob, PathScheduler, WorkPriority};
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

const DAEMON_MAX_IN_FLIGHT_CAP: usize = 4;
const INITIAL_SPOOL_PATH_LIMIT: usize = 100;
const PERIODIC_SPOOL_PATH_LIMIT: usize = 50;
const PATH_SPOOL_REPLAY_LIMIT: usize = 50;
const LOCAL_RETRY_DELAY_SECS: u64 = 5;
const LOCAL_STATUS_INTERVAL_SECS: u64 = 10;
const SERVER_HEARTBEAT_INTERVAL_SECS: u64 = 5 * 60;

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

#[derive(Clone)]
struct PathTaskContext {
    shipper_config: ShipperConfig,
    client: ShipperClient,
    algo: CompressionAlgo,
    tracker: ConsecutiveErrorTracker,
    parse_tracker: RecentIssueTracker,
}

struct PathTaskResult {
    job: PathJob,
    events_shipped: usize,
    resolved_spool: usize,
    failed_spool: usize,
    had_connect_error: bool,
    rerun_priority: Option<WorkPriority>,
    local_retry_after: Option<Duration>,
}

struct DeferredRetry {
    due_at: Instant,
    provider: &'static str,
}

struct DiscoveryTaskResult {
    files: Vec<(PathBuf, &'static str)>,
    priority: WorkPriority,
    reason: &'static str,
}

/// Run the connect daemon. This function blocks until shutdown signal.
pub async fn run(config: ConnectConfig) -> Result<()> {
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
    let parse_tracker = RecentIssueTracker::new();
    let task_context = PathTaskContext {
        shipper_config: config.shipper_config.clone(),
        client: client.clone(),
        algo: config.algo,
        tracker: tracker.clone(),
        parse_tracker: parse_tracker.clone(),
    };

    // 6. Start file watcher before catch-up work so live changes queue immediately.
    let mut watcher = SessionWatcher::new(&providers)?;
    tracing::info!(
        "Daemon ready — watching for file changes (flush interval: {:?})",
        config.flush_interval
    );

    // 7. Build bounded per-path scheduler and queue startup work.
    let max_in_flight = daemon_max_in_flight(&config.shipper_config);
    let mut scheduler = PathScheduler::new(max_in_flight);
    let mut in_flight = JoinSet::new();
    let mut discovery_tasks = JoinSet::new();
    let mut deferred_retries = HashMap::new();

    start_discovery_task(
        &mut discovery_tasks,
        &providers,
        WorkPriority::Scan,
        "startup catch-up",
    );
    let initial_retry_paths =
        queue_pending_spool_paths(&mut scheduler, &conn, INITIAL_SPOOL_PATH_LIMIT)?;
    tracing::info!(
        "Queued startup catch-up: {} retry paths; background scan started (max {} concurrent)",
        initial_retry_paths,
        max_in_flight
    );

    // 8. Main event loop
    let fallback_interval = Duration::from_secs(config.fallback_scan_secs.max(10));
    let spool_interval = Duration::from_secs(config.spool_replay_secs.max(5));
    let health_check_interval = Duration::from_secs(60);
    let prune_interval = Duration::from_secs(24 * 3600);
    let heartbeat_interval = Duration::from_secs(SERVER_HEARTBEAT_INTERVAL_SECS);

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
    let mut local_status_timer =
        tokio::time::interval(Duration::from_secs(LOCAL_STATUS_INTERVAL_SECS));

    let mut outbox_timer = tokio::time::interval(Duration::from_secs(1));
    outbox_timer.tick().await; // consume first immediate tick
    let mut local_retry_timer = tokio::time::interval(Duration::from_secs(1));
    local_retry_timer.tick().await; // consume first immediate tick

    let mut offline = OfflineState::new();
    let mut last_ship_at: Option<String> = None;

    let outbox_dir = config::get_agent_outbox_dir()?;
    let status_path = config::get_agent_status_path()?;
    if let Some(parent) = status_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    loop {
        drain_due_local_retries(&mut scheduler, &mut deferred_retries);
        if !offline.is_offline {
            start_ready_jobs(&mut scheduler, &mut in_flight, &task_context);
        }

        tokio::select! {
            biased;

            // Shutdown signals
            _ = shutdown_signal() => {
                tracing::info!("Shutdown signal received, exiting gracefully...");
                break;
            }

            task_result = in_flight.join_next(), if scheduler.has_in_flight() => {
                match task_result {
                    Some(Ok(result)) => {
                        let retry_path = result.job.path.clone();
                        let retry_provider = result.job.provider;
                        scheduler.complete(&retry_path, result.rerun_priority);
                        if let Some(delay) = result.local_retry_after {
                            deferred_retries.insert(retry_path, DeferredRetry {
                                due_at: Instant::now() + delay,
                                provider: retry_provider,
                            });
                        }
                        if result.resolved_spool > 0 || result.failed_spool > 0 {
                            tracing::info!(
                                "Path retry {}: {} resolved, {} failed",
                                result.job.path.display(),
                                result.resolved_spool,
                                result.failed_spool
                            );
                        }
                        if result.had_connect_error {
                            offline.mark_offline();
                            tracing::warn!(
                                "Connection error while processing {} — entering offline mode",
                                result.job.path.display()
                            );
                        } else if result.events_shipped > 0 {
                            last_ship_at = Some(chrono::Utc::now().to_rfc3339());
                        }
                    }
                    Some(Err(e)) => {
                        return Err(anyhow::anyhow!("path task failed: {}", e));
                    }
                    None => {}
                }
            }

            discovery_result = discovery_tasks.join_next(), if !discovery_tasks.is_empty() => {
                match discovery_result {
                    Some(Ok(result)) => {
                        let queued = enqueue_discovered_files(
                            &mut scheduler,
                            result.files,
                            result.priority,
                        );
                        tracing::debug!("Queued {} paths for {}", queued, result.reason);
                    }
                    Some(Err(e)) => {
                        tracing::warn!("Background discovery task failed: {}", e);
                    }
                    None => {}
                }
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
                        for path in paths {
                            let provider = match discovery::provider_for_path(&path, &providers) {
                                Some(provider) => provider,
                                None => {
                                    tracing::debug!(
                                        "Skipping file outside known providers: {}",
                                        path.display()
                                    );
                                    continue;
                                }
                            };
                            scheduler.enqueue(path, provider, WorkPriority::Watch);
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
                if discovery_tasks.is_empty() {
                    tracing::debug!("Starting fallback full scan in background...");
                    start_discovery_task(
                        &mut discovery_tasks,
                        &providers,
                        WorkPriority::Scan,
                        "fallback scan",
                    );
                } else {
                    tracing::debug!("Skipping fallback full scan tick because discovery is still running");
                }
            }

            // Spool replay (retry failed shipments) — skip when offline
            _ = spool_timer.tick(), if !offline.is_offline => {
                match queue_pending_spool_paths(&mut scheduler, &conn, PERIODIC_SPOOL_PATH_LIMIT) {
                    Ok(queued) => {
                        if queued > 0 {
                            tracing::debug!("Queued {} retry paths from spool", queued);
                        }
                    }
                    Err(e) => tracing::warn!("Spool replay error: {}", e),
                }
            }

            // Outbox drain: presence events written by hooks (every 1s, skip when offline)
            _ = outbox_timer.tick(), if !offline.is_offline => {
                let (sent, kept) = outbox::drain_outbox_with_local_state(
                    &outbox_dir,
                    &client,
                    config.shipper_config.db_path.as_deref(),
                )
                .await;
                if sent > 0 || kept > 0 {
                    tracing::debug!("Outbox drain: {} sent, {} pending", sent, kept);
                }
            }

            // Wake the loop when a delayed local retry may now be ready.
            _ = local_retry_timer.tick(), if !deferred_retries.is_empty() => {}

            // Daily: prune stale file_state and session_binding entries
            _ = prune_timer.tick() => {
                let fs = FileState::new(&conn);
                match fs.prune_stale(30) {
                    Ok(n) if n > 0 => tracing::info!("Daily prune: removed {} stale file_state entries", n),
                    Ok(_) => {}
                    Err(e) => tracing::warn!("Daily prune error: {}", e),
                }
                let sb = crate::state::session_binding::SessionBinding::new(&conn);
                match sb.prune_stale(30) {
                    Ok(n) if n > 0 => tracing::info!("Daily prune: removed {} stale session_binding entries", n),
                    Ok(_) => {}
                    Err(e) => tracing::warn!("Session binding prune error: {}", e),
                }
            }

            // Frequent local status file refresh for ambient UX and debugging
            _ = local_status_timer.tick() => {
                write_local_status_snapshot(
                    &conn,
                    &tracker,
                    &parse_tracker,
                    offline.is_offline,
                    &last_ship_at,
                    &status_path,
                );
            }

            // Periodic server heartbeat
            _ = heartbeat_timer.tick() => {
                let payload = write_local_status_snapshot(
                    &conn,
                    &tracker,
                    &parse_tracker,
                    offline.is_offline,
                    &last_ship_at,
                    &status_path,
                );
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

fn write_local_status_snapshot(
    conn: &rusqlite::Connection,
    tracker: &ConsecutiveErrorTracker,
    parse_tracker: &RecentIssueTracker,
    is_offline: bool,
    last_ship_at: &Option<String>,
    status_path: &Path,
) -> heartbeat::HeartbeatPayload {
    let spool = Spool::new(conn);
    let stats = heartbeat::HeartbeatStats {
        spool: &spool,
        tracker,
        parse_tracker,
        is_offline,
        last_ship_at: last_ship_at.clone(),
    };
    let payload = heartbeat::HeartbeatPayload::build(&stats);
    heartbeat::write_status_file(&payload, &stats, status_path);
    payload
}

fn daemon_max_in_flight(config: &ShipperConfig) -> usize {
    config.workers.max(1).min(DAEMON_MAX_IN_FLIGHT_CAP)
}

fn enqueue_discovered_files(
    scheduler: &mut PathScheduler,
    all_files: Vec<(PathBuf, &'static str)>,
    priority: WorkPriority,
) -> usize {
    let count = all_files.len();
    for (path, provider) in all_files {
        scheduler.enqueue(path, provider, priority);
    }
    count
}

fn start_discovery_task(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
    priority: WorkPriority,
    reason: &'static str,
) {
    let providers = providers.to_vec();
    discovery_tasks.spawn_blocking(move || DiscoveryTaskResult {
        files: discovery::discover_all_files(&providers),
        priority,
        reason,
    });
}

fn queue_pending_spool_paths(
    scheduler: &mut PathScheduler,
    conn: &rusqlite::Connection,
    limit: usize,
) -> Result<usize> {
    let spool = Spool::new(conn);
    let cleaned = spool.cleanup()?;
    if cleaned > 0 {
        tracing::info!("Cleaned {} old spool entries", cleaned);
    }

    let mut queued = 0usize;
    for pending in spool.pending_paths(limit)? {
        let Some(provider) = provider_name_to_static(&pending.provider) else {
            tracing::warn!(
                "Skipping pending spool path with unknown provider {}: {}",
                pending.provider,
                pending.file_path
            );
            continue;
        };
        scheduler.enqueue(
            PathBuf::from(pending.file_path),
            provider,
            WorkPriority::Retry,
        );
        queued += 1;
    }
    Ok(queued)
}

fn provider_name_to_static(provider: &str) -> Option<&'static str> {
    match provider {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "gemini" => Some("gemini"),
        _ => None,
    }
}

fn work_context(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Watch => "watch",
        WorkPriority::Retry => "spool_replay",
        WorkPriority::Scan => "fallback_scan",
    }
}

fn start_ready_jobs(
    scheduler: &mut PathScheduler,
    in_flight: &mut JoinSet<PathTaskResult>,
    task_context: &PathTaskContext,
) {
    while let Some(job) = scheduler.pop_launchable() {
        let task_context = task_context.clone();
        in_flight.spawn_local(run_path_job(job, task_context));
    }
}

fn drain_due_local_retries(
    scheduler: &mut PathScheduler,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
) {
    let now = Instant::now();
    let ready_paths: Vec<_> = deferred_retries
        .iter()
        .filter_map(|(path, retry)| (retry.due_at <= now).then_some(path.clone()))
        .collect();

    for path in ready_paths {
        if let Some(retry) = deferred_retries.remove(&path) {
            scheduler.enqueue(path, retry.provider, WorkPriority::Retry);
        }
    }
}

async fn prepare_file_for_job(
    job: &PathJob,
    task_context: &PathTaskContext,
) -> Result<Option<shipper::PreparedFile>> {
    let path = job.path.clone();
    let provider = job.provider;
    let algo = task_context.algo;
    let db_path = task_context.shipper_config.db_path.clone();
    let max_batch_bytes = task_context.shipper_config.max_batch_bytes;
    let parse_tracker = task_context.parse_tracker.clone();

    tokio::task::spawn_blocking(move || {
        let conn = open_db(db_path.as_deref())?;
        let canonical = std::fs::canonicalize(&path)
            .unwrap_or_else(|_| path.clone())
            .to_string_lossy()
            .to_string();

        // Check session_binding for managed session ID override.
        // For brand-new files (offset 0), the binding may not have landed yet
        // (e.g. Codex bridge writes it on thread/started, which races with
        // fsevents). Retry once after a short delay to close the window.
        let binding = crate::state::session_binding::SessionBinding::new(&conn);
        let mut session_id_override = binding.get(&canonical)?;
        if session_id_override.is_none() {
            let file_state = crate::state::file_state::FileState::new(&conn);
            let current_offset = file_state
                .get_offset(&canonical)
                .or_else(|_| file_state.get_offset(&path.to_string_lossy()))?;
            if current_offset == 0 {
                std::thread::sleep(std::time::Duration::from_millis(300));
                session_id_override = binding.get(&canonical)?;
            }
        }

        shipper::prepare_file_batches_with_parse_tracker(
            &path,
            provider,
            algo,
            &conn,
            max_batch_bytes,
            session_id_override.as_deref(),
            Some(&parse_tracker),
        )
    })
    .await?
}

async fn run_path_job(job: PathJob, task_context: PathTaskContext) -> PathTaskResult {
    let mut result = PathTaskResult {
        job,
        events_shipped: 0,
        resolved_spool: 0,
        failed_spool: 0,
        had_connect_error: false,
        rerun_priority: None,
        local_retry_after: None,
    };

    let conn = match open_db(task_context.shipper_config.db_path.as_deref()) {
        Ok(conn) => conn,
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!(
                    "Error opening shipper DB for {}: {}",
                    result.job.path.display(),
                    e
                );
            }
            result.local_retry_after = Some(Duration::from_secs(LOCAL_RETRY_DELAY_SECS));
            return result;
        }
    };

    match shipper::replay_spool_for_path_with_batch_bytes_and_parse_tracker(
        &conn,
        &task_context.client,
        task_context.algo,
        &result.job.path,
        PATH_SPOOL_REPLAY_LIMIT,
        task_context.shipper_config.max_batch_bytes,
        Some(&task_context.parse_tracker),
    )
    .await
    {
        Ok(replay_outcome) => {
            result.resolved_spool = replay_outcome.resolved;
            result.failed_spool = replay_outcome.failed;
            result.had_connect_error = replay_outcome.had_connect_error;
        }
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!(
                    "Error replaying spool for {}: {}",
                    result.job.path.display(),
                    e
                );
            }
            result.local_retry_after = Some(Duration::from_secs(LOCAL_RETRY_DELAY_SECS));
            return result;
        }
    }

    if result.had_connect_error {
        return result;
    }

    let ready_spool_remaining = Spool::new(&conn)
        .pending_entries_for_path(&result.job.path.to_string_lossy(), 1)
        .map(|entries| !entries.is_empty())
        .unwrap_or(false);
    if ready_spool_remaining {
        result.rerun_priority = Some(WorkPriority::Retry);
    }

    let file_start = Instant::now();
    match prepare_file_for_job(&result.job, &task_context).await {
        Ok(Some(prepared)) => {
            let event_count = prepared.total_event_count();
            let byte_count = prepared.new_offset.saturating_sub(prepared.offset);
            match shipper::ship_prepared_file(
                prepared,
                &task_context.client,
                &conn,
                Some(&task_context.tracker),
            )
            .await
            {
                Ok(outcome) => {
                    shipper::log_slow_file_processing(
                        work_context(result.job.priority),
                        Path::new(&result.job.path),
                        result.job.provider,
                        event_count,
                        byte_count,
                        outcome.dead_lettered,
                        file_start.elapsed(),
                    );
                    result.events_shipped = outcome.events_shipped;
                    if outcome.had_connect_error {
                        result.had_connect_error = true;
                    }
                }
                Err(e) => {
                    if task_context.tracker.record_error() {
                        tracing::warn!("Error shipping {}: {}", result.job.path.display(), e);
                    }
                    result.local_retry_after = Some(Duration::from_secs(LOCAL_RETRY_DELAY_SECS));
                }
            }
        }
        Ok(None) => {}
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!("Error preparing {}: {}", result.job.path.display(), e);
            }
            result.local_retry_after = Some(Duration::from_secs(LOCAL_RETRY_DELAY_SECS));
        }
    }

    result
}

/// Wait for SIGINT (Ctrl-C) or SIGTERM.
async fn shutdown_signal() {
    use tokio::signal::unix::{signal, SignalKind};

    let ctrl_c = tokio::signal::ctrl_c();
    let mut sigterm = signal(SignalKind::terminate()).expect("failed to install SIGTERM handler");

    tokio::select! {
        _ = ctrl_c => {},
        _ = sigterm.recv() => {},
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_drain_due_local_retries_enqueues_only_ready_paths() {
        let mut scheduler = PathScheduler::new(4);
        let now = Instant::now();
        let mut deferred_retries = HashMap::from([
            (
                PathBuf::from("/tmp/retry-now.jsonl"),
                DeferredRetry {
                    due_at: now - Duration::from_secs(1),
                    provider: "claude",
                },
            ),
            (
                PathBuf::from("/tmp/retry-later.jsonl"),
                DeferredRetry {
                    due_at: now + Duration::from_secs(60),
                    provider: "claude",
                },
            ),
        ]);

        drain_due_local_retries(&mut scheduler, &mut deferred_retries);

        let launched = scheduler.pop_launchable().unwrap();
        assert_eq!(launched.path, PathBuf::from("/tmp/retry-now.jsonl"));
        assert_eq!(launched.priority, WorkPriority::Retry);
        assert_eq!(deferred_retries.len(), 1);
        assert!(deferred_retries.contains_key(&PathBuf::from("/tmp/retry-later.jsonl")));
    }
}
