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
use rusqlite::Connection;
use serde::Deserialize;
use tokio::io::AsyncReadExt;
use tokio::sync::mpsc;
use tokio::task::JoinSet;

use crate::config::{self, ShipperConfig};
use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::heartbeat;
use crate::managed_bridge_scan;
use crate::managed_claude_scan;
use crate::managed_reaper::ManagedBridgeReaper;
use crate::outbox;
use crate::pipeline::compressor::CompressionAlgo;
use crate::scheduler::{PathJob, PathScheduler, WorkPriority};
use crate::shipper;
use crate::shipping::client::ShipperClient;
use crate::shipping_stats::RecentShipStatsTracker;
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
const LOCAL_STATUS_INTERVAL_SECS: u64 = 1;
const SERVER_HEARTBEAT_INTERVAL_SECS: u64 = 5 * 60;
const ACTIVE_TRANSCRIPT_POLL_INTERVAL: Duration = Duration::from_secs(1);
const ACTIVE_TRANSCRIPT_POLL_TTL: Duration = Duration::from_secs(2 * 60 * 60);
const TERMINAL_CATCHUP_DELAYS: [Duration; 3] = [
    Duration::from_secs(0),
    Duration::from_secs(1),
    Duration::from_secs(3),
];

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
    ship_stats: RecentShipStatsTracker,
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

#[derive(Debug, Clone)]
struct TranscriptCatchup {
    due_at: Instant,
    path: PathBuf,
    provider: &'static str,
}

#[derive(Debug, Clone)]
struct ActiveTranscriptPoll {
    due_at: Instant,
    expires_at: Instant,
    provider: &'static str,
}

#[derive(Debug, Clone, Deserialize)]
struct TranscriptWakeSignal {
    provider: String,
    path: PathBuf,
    phase: String,
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
    let ship_stats = RecentShipStatsTracker::new();
    let task_context = PathTaskContext {
        shipper_config: config.shipper_config.clone(),
        client: client.clone(),
        algo: config.algo,
        tracker: tracker.clone(),
        parse_tracker: parse_tracker.clone(),
        ship_stats: ship_stats.clone(),
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
    let mut transcript_catchups = Vec::new();
    let mut active_transcript_polls = HashMap::new();

    start_discovery_task(
        &mut discovery_tasks,
        &providers,
        WorkPriority::Scan,
        "startup reconciliation",
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
    let mut last_runtime_truth_signature: Option<String> = None;
    let mut runtime_truth_bootstrapped = false;
    let mut codex_terminal_catchup_marks: HashMap<PathBuf, String> = HashMap::new();
    let mut bridge_reaper = ManagedBridgeReaper::from_env();

    let outbox_dir = config::get_agent_outbox_dir()?;
    let status_path = config::get_agent_status_path()?;
    if let Some(parent) = status_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let control_channel_task =
        crate::control_channel::spawn_control_channel(config.shipper_config.clone());
    let (transcript_wake_tx, mut transcript_wake_rx) = mpsc::unbounded_channel();
    let transcript_wake_task = spawn_transcript_wake_listener(transcript_wake_tx)?;

    loop {
        drain_due_local_retries(&mut scheduler, &mut deferred_retries);
        drain_due_transcript_catchups(&mut scheduler, &mut transcript_catchups);
        drain_due_active_transcript_polls(&mut scheduler, &mut active_transcript_polls);
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

            Some(signal) = transcript_wake_rx.recv() => {
                schedule_transcript_catchup_for_wake(
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    signal,
                );
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
                            last_runtime_truth_signature = None;
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

            // Periodic reconciliation scan — repair missed hook/watch work after
            // restarts, sleeps, or dropped OS notifications. Active-session
            // freshness should come from hook catch-up and watcher jobs.
            _ = fallback_timer.tick(), if !offline.is_offline => {
                if discovery_tasks.is_empty() {
                    tracing::debug!("Starting reconciliation scan in background...");
                    start_discovery_task(
                        &mut discovery_tasks,
                        &providers,
                        WorkPriority::Scan,
                        "reconciliation scan",
                    );
                } else {
                    tracing::debug!("Skipping reconciliation scan tick because discovery is still running");
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
                let outbox_result = outbox::drain_outbox_with_local_state_result(
                    &outbox_dir,
                    &client,
                    config.shipper_config.db_path.as_deref(),
                )
                .await;
                if outbox_result.sent > 0 || outbox_result.kept > 0 {
                    tracing::debug!(
                        "Outbox drain: {} sent, {} pending",
                        outbox_result.sent,
                        outbox_result.kept
                    );
                }
                if !outbox_result.signals.is_empty() {
                    schedule_transcript_catchups_for_signals(
                        &conn,
                        &mut transcript_catchups,
                        &mut active_transcript_polls,
                        outbox_result.signals,
                    );
                }
            }

            // Wake the loop when delayed local retry/catch-up work may now be ready.
            _ = local_retry_timer.tick(), if !deferred_retries.is_empty() || !transcript_catchups.is_empty() || !active_transcript_polls.is_empty() => {}

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
                let observations = managed_bridge_scan::collect_observations();
                let claude_observations = managed_claude_scan::collect_observations();
                schedule_transcript_catchups_for_codex_observations(
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    &mut codex_terminal_catchup_marks,
                    &observations,
                );
                let payload = write_local_status_snapshot(
                    &conn,
                    &tracker,
                    &parse_tracker,
                    &ship_stats,
                    offline.is_offline,
                    &last_ship_at,
                    &config.shipper_config.machine_name,
                    &status_path,
                    &observations,
                    &claude_observations,
                );
                bridge_reaper.tick(&observations);
                let signature = runtime_truth_signature(&payload);
                if !runtime_truth_bootstrapped {
                    last_runtime_truth_signature = Some(signature);
                    runtime_truth_bootstrapped = true;
                    continue;
                }
                if !offline.is_offline && last_runtime_truth_signature.as_deref() != Some(signature.as_str()) {
                    match heartbeat::send_heartbeat(&client, &payload).await {
                        Ok(()) => {
                            tracing::debug!("Runtime truth snapshot sent after local process/control change");
                            last_runtime_truth_signature = Some(signature);
                        }
                        Err(e) => {
                            last_runtime_truth_signature = None;
                            tracing::debug!("Runtime truth snapshot send failed: {}", e);
                        }
                    }
                }
            }

            // Periodic server heartbeat
            _ = heartbeat_timer.tick() => {
                let observations = managed_bridge_scan::collect_observations();
                let claude_observations = managed_claude_scan::collect_observations();
                schedule_transcript_catchups_for_codex_observations(
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    &mut codex_terminal_catchup_marks,
                    &observations,
                );
                let payload = write_local_status_snapshot(
                    &conn,
                    &tracker,
                    &parse_tracker,
                    &ship_stats,
                    offline.is_offline,
                    &last_ship_at,
                    &config.shipper_config.machine_name,
                    &status_path,
                    &observations,
                    &claude_observations,
                );
                if !offline.is_offline {
                    runtime_truth_bootstrapped = true;
                    if let Err(e) = heartbeat::send_heartbeat(&client, &payload).await {
                        last_runtime_truth_signature = None;
                        tracing::debug!("Heartbeat send failed: {}", e);
                    } else {
                        last_runtime_truth_signature = Some(runtime_truth_signature(&payload));
                    }
                }
            }
        }
    }

    if let Some(task) = control_channel_task {
        task.abort();
    }
    if let Some(task) = transcript_wake_task {
        task.abort();
    }
    tracing::info!("Daemon shutdown complete");
    Ok(())
}

fn write_local_status_snapshot(
    conn: &rusqlite::Connection,
    tracker: &ConsecutiveErrorTracker,
    parse_tracker: &RecentIssueTracker,
    ship_stats: &RecentShipStatsTracker,
    is_offline: bool,
    last_ship_at: &Option<String>,
    machine_id: &str,
    status_path: &Path,
    observations: &[managed_bridge_scan::CodexBridgeObservation],
    claude_observations: &[managed_claude_scan::ClaudeChannelObservation],
) -> heartbeat::HeartbeatPayload {
    let spool = Spool::new(conn);
    let stats = heartbeat::HeartbeatStats {
        spool: &spool,
        tracker,
        parse_tracker,
        ship_stats,
        is_offline,
        last_ship_at: last_ship_at.clone(),
    };
    let mut payload = heartbeat::HeartbeatPayload::build(&stats);
    let now = chrono::Utc::now();
    payload.managed_sessions =
        heartbeat::leases_from_observations(conn, machine_id, observations, now);
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_claude_channel_observations(
            conn,
            machine_id,
            claude_observations,
            now,
        ));
    payload.managed_sessions.sort_by(|a, b| {
        a.provider
            .cmp(&b.provider)
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    payload.unmanaged_session_bindings = heartbeat::collect_unmanaged_session_bindings_with_store(
        conn,
        machine_id,
        chrono::Utc::now(),
    );
    // Compute the fresh ledger view up front so a read failure is both
    // logged and encoded in the status file as `phase_ledger_status`.
    // Downstream readers (verify-runtime-truth, local-health) can then
    // tell an intentionally empty ledger apart from one the engine
    // couldn't read this tick.
    let (phase_ledger, ledger_status) =
        match crate::state::session_phase::SessionPhaseStore::new(conn)
            .fresh_rows(chrono::Utc::now())
        {
            Ok(rows) => (rows, heartbeat::PhaseLedgerStatus::Ok),
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    "failed to read fresh phase_ledger rows for engine-status.json"
                );
                (
                    Vec::new(),
                    heartbeat::PhaseLedgerStatus::ReadFailed(err.to_string()),
                )
            }
        };
    heartbeat::write_status_file(&payload, &stats, phase_ledger, ledger_status, status_path);
    payload
}

fn runtime_truth_signature(payload: &heartbeat::HeartbeatPayload) -> String {
    let mut managed: Vec<String> = payload
        .managed_sessions
        .iter()
        .map(|lease| {
            format!(
                "{}|{}|{}|{}|{}|{}|{}",
                lease.provider,
                lease.session_id,
                lease.machine_id,
                lease.state,
                lease.phase.as_deref().unwrap_or(""),
                lease.tool_name.as_deref().unwrap_or(""),
                lease.bridge_status.as_deref().unwrap_or("")
            )
        })
        .collect();
    managed.sort();

    let mut unmanaged: Vec<String> = payload
        .unmanaged_session_bindings
        .iter()
        .map(|binding| {
            format!(
                "{}|{}|{}|{}|{}|{}|{}",
                binding.machine_id,
                binding.provider,
                binding.provider_session_id,
                binding.pid.map(|pid| pid.to_string()).unwrap_or_default(),
                binding.process_start_time.as_deref().unwrap_or(""),
                binding.cwd.as_deref().unwrap_or(""),
                binding.source_path.as_deref().unwrap_or("")
            )
        })
        .collect();
    unmanaged.sort();

    format!(
        "managed=[{}];unmanaged=[{}]",
        managed.join(";"),
        unmanaged.join(";")
    )
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

#[cfg(unix)]
fn spawn_transcript_wake_listener(
    tx: mpsc::UnboundedSender<TranscriptWakeSignal>,
) -> Result<Option<tokio::task::JoinHandle<()>>> {
    let socket_path = config::get_agent_transcript_wake_socket_path()?;
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let _ = std::fs::remove_file(&socket_path);
    let listener = tokio::net::UnixListener::bind(&socket_path)?;
    tracing::debug!(
        path = %socket_path.display(),
        "Transcript wake listener started"
    );
    Ok(Some(tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            let tx = tx.clone();
            tokio::spawn(async move {
                let mut buf = Vec::with_capacity(1024);
                if stream.read_to_end(&mut buf).await.is_err() {
                    return;
                }
                let Ok(signal) = serde_json::from_slice::<TranscriptWakeSignal>(&buf) else {
                    return;
                };
                let _ = tx.send(signal);
            });
        }
    })))
}

#[cfg(not(unix))]
fn spawn_transcript_wake_listener(
    _tx: mpsc::UnboundedSender<TranscriptWakeSignal>,
) -> Result<Option<tokio::task::JoinHandle<()>>> {
    Ok(None)
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
        WorkPriority::Catchup => "hook_catchup",
        WorkPriority::Retry => "spool_replay",
        WorkPriority::Scan => "reconciliation_scan",
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

fn drain_due_transcript_catchups(
    scheduler: &mut PathScheduler,
    transcript_catchups: &mut Vec<TranscriptCatchup>,
) {
    let now = Instant::now();
    let mut index = 0usize;
    while index < transcript_catchups.len() {
        if transcript_catchups[index].due_at <= now {
            let catchup = transcript_catchups.swap_remove(index);
            scheduler.enqueue(catchup.path, catchup.provider, WorkPriority::Catchup);
        } else {
            index += 1;
        }
    }
}

fn drain_due_active_transcript_polls(
    scheduler: &mut PathScheduler,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
) {
    let now = Instant::now();
    let ready_paths: Vec<_> = active_transcript_polls
        .iter()
        .filter_map(|(path, poll)| (poll.due_at <= now).then_some(path.clone()))
        .collect();

    for path in ready_paths {
        let Some(poll) = active_transcript_polls.get(&path).cloned() else {
            continue;
        };
        if now >= poll.expires_at || !path.exists() {
            active_transcript_polls.remove(&path);
            continue;
        }

        scheduler.enqueue(path.clone(), poll.provider, WorkPriority::Catchup);
        if let Some(poll) = active_transcript_polls.get_mut(&path) {
            poll.due_at = now + ACTIVE_TRANSCRIPT_POLL_INTERVAL;
        }
    }
}

fn schedule_transcript_catchups_for_signals(
    conn: &Connection,
    transcript_catchups: &mut Vec<TranscriptCatchup>,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
    signals: Vec<outbox::DrainedPresenceSignal>,
) {
    for signal in signals {
        let Some(provider) = provider_name_to_static(&signal.provider) else {
            tracing::debug!(
                provider = %signal.provider,
                session_id = %signal.session_id,
                "Skipping transcript catch-up for unknown provider"
            );
            continue;
        };

        let Some(path) = resolve_transcript_path_for_signal(conn, &signal, provider) else {
            tracing::debug!(
                provider = %signal.provider,
                session_id = %signal.session_id,
                phase = %signal.phase,
                "No transcript path known for presence-driven catch-up"
            );
            continue;
        };

        schedule_transcript_catchup(
            transcript_catchups,
            active_transcript_polls,
            path,
            provider,
            &signal.phase,
        );
    }
}

fn schedule_transcript_catchup_for_wake(
    transcript_catchups: &mut Vec<TranscriptCatchup>,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
    signal: TranscriptWakeSignal,
) {
    let Some(provider) = provider_name_to_static(&signal.provider) else {
        tracing::debug!(
            provider = %signal.provider,
            "Skipping transcript wake for unknown provider"
        );
        return;
    };
    if !signal.path.exists() {
        tracing::debug!(
            provider = %signal.provider,
            path = %signal.path.display(),
            "Skipping transcript wake for missing path"
        );
        return;
    }
    schedule_transcript_catchup(
        transcript_catchups,
        active_transcript_polls,
        signal.path,
        provider,
        &signal.phase,
    );
}

fn schedule_transcript_catchups_for_codex_observations(
    transcript_catchups: &mut Vec<TranscriptCatchup>,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
    terminal_catchup_marks: &mut HashMap<PathBuf, String>,
    observations: &[managed_bridge_scan::CodexBridgeObservation],
) {
    for observation in observations {
        if !(observation.bridge_alive
            || observation.has_tui_attachment
            || observation.app_server_alive)
        {
            continue;
        }
        let Some(path) = observation
            .thread_path
            .as_deref()
            .map(PathBuf::from)
            .filter(|path| path.exists())
        else {
            continue;
        };
        let phase = codex_observation_transcript_phase(observation);
        if is_terminal_or_attention_phase(phase) {
            if terminal_catchup_marks.get(&path) == Some(&observation.updated_at) {
                continue;
            }
            terminal_catchup_marks.insert(path.clone(), observation.updated_at.clone());
        } else {
            terminal_catchup_marks.remove(&path);
        }

        schedule_transcript_catchup(
            transcript_catchups,
            active_transcript_polls,
            path,
            "codex",
            phase,
        );
    }
}

fn codex_observation_transcript_phase(
    observation: &managed_bridge_scan::CodexBridgeObservation,
) -> &'static str {
    if observation.active_turn_id.is_some() {
        return "running";
    }

    match observation.last_turn_status.as_deref() {
        Some("completed") | Some("failed") | Some("cancelled") => "idle",
        _ => "running",
    }
}

fn schedule_transcript_catchup(
    transcript_catchups: &mut Vec<TranscriptCatchup>,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
    path: PathBuf,
    provider: &'static str,
    phase: &str,
) {
    if is_terminal_or_attention_phase(phase) {
        transcript_catchups.retain(|existing| existing.path != path);
        if active_transcript_polls.remove(&path).is_some() {
            tracing::info!(
                path = %path.display(),
                provider,
                phase,
                "Stopped active transcript polling"
            );
        }
        let now = Instant::now();
        for delay in TERMINAL_CATCHUP_DELAYS {
            transcript_catchups.push(TranscriptCatchup {
                due_at: now + delay,
                path: path.clone(),
                provider,
            });
        }
        return;
    }

    if is_active_phase(phase) {
        let now = Instant::now();
        let was_polling = active_transcript_polls.contains_key(&path);
        active_transcript_polls.insert(
            path.clone(),
            ActiveTranscriptPoll {
                due_at: now + ACTIVE_TRANSCRIPT_POLL_INTERVAL,
                expires_at: now + ACTIVE_TRANSCRIPT_POLL_TTL,
                provider,
            },
        );
        if !was_polling {
            tracing::info!(
                path = %path.display(),
                provider,
                phase,
                "Started active transcript polling"
            );
        }
        if !transcript_catchups.iter().any(|item| item.path == path) {
            transcript_catchups.push(TranscriptCatchup {
                due_at: now,
                path,
                provider,
            });
        }
    }
}

fn resolve_transcript_path_for_signal(
    conn: &Connection,
    signal: &outbox::DrainedPresenceSignal,
    provider: &str,
) -> Option<PathBuf> {
    if let Some(path) = signal.transcript_path.as_ref() {
        if path.exists() {
            return Some(path.clone());
        }
        tracing::debug!(
            provider,
            session_id = %signal.session_id,
            path = %path.display(),
            "Hook-provided transcript path does not exist"
        );
    }

    resolve_transcript_path_for_session(conn, &signal.session_id, provider)
}

fn resolve_transcript_path_for_session(
    conn: &Connection,
    session_id: &str,
    provider: &str,
) -> Option<PathBuf> {
    find_transcript_path(conn, "file_state", session_id, provider)
        .or_else(|| find_transcript_path(conn, "session_binding", session_id, provider))
}

fn find_transcript_path(
    conn: &Connection,
    table: &str,
    session_id: &str,
    provider: &str,
) -> Option<PathBuf> {
    let order_column = match table {
        "file_state" => "last_updated",
        "session_binding" => "updated_at",
        _ => return None,
    };
    let sql = format!(
        "SELECT path FROM {table} WHERE session_id = ?1 AND provider = ?2 ORDER BY {order_column} DESC LIMIT 1",
    );
    let result = conn.query_row(&sql, rusqlite::params![session_id, provider], |row| {
        row.get::<_, String>(0)
    });
    match result {
        Ok(path) if Path::new(&path).exists() => Some(PathBuf::from(path)),
        Ok(_) | Err(rusqlite::Error::QueryReturnedNoRows) => None,
        Err(err) => {
            tracing::debug!(
                error = %err,
                table,
                session_id,
                provider,
                "Failed to resolve transcript path"
            );
            None
        }
    }
}

fn is_terminal_or_attention_phase(phase: &str) -> bool {
    matches!(phase, "idle" | "needs_user" | "blocked")
}

fn is_active_phase(phase: &str) -> bool {
    matches!(phase, "thinking" | "running")
}

#[tracing::instrument(
    level = "info",
    name = "engine.ship.prepare",
    skip(task_context),
    fields(
        longhouse.provider = %job.provider,
        longhouse.work_context = %work_context(job.priority),
    )
)]
async fn prepare_file_for_job(
    job: &PathJob,
    task_context: &PathTaskContext,
) -> Result<Option<shipper::PreparedFile>> {
    let path = job.path.clone();
    let provider = job.provider;
    let work_context_label = work_context(job.priority);
    let algo = task_context.algo;
    let db_path = task_context.shipper_config.db_path.clone();
    let max_batch_bytes = task_context.shipper_config.max_batch_bytes;
    let parse_tracker = task_context.parse_tracker.clone();
    let blocking_span = tracing::info_span!(
        "engine.ship.prepare.blocking",
        longhouse.provider = %provider,
        longhouse.work_context = %work_context_label,
    );

    tokio::task::spawn_blocking(move || {
        let _enter = blocking_span.enter();
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

#[tracing::instrument(
    level = "info",
    name = "engine.path_job",
    skip(task_context),
    fields(
        longhouse.provider = %job.provider,
        longhouse.work_context = %work_context(job.priority),
    )
)]
async fn run_path_job(job: PathJob, task_context: PathTaskContext) -> PathTaskResult {
    let job_started_at_ms = chrono::Utc::now().timestamp_millis();
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
        Some(&task_context.ship_stats),
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
    let prepare_started_at_ms = chrono::Utc::now().timestamp_millis();
    match prepare_file_for_job(&result.job, &task_context).await {
        Ok(Some(prepared)) => {
            let prepare_finished_at_ms = chrono::Utc::now().timestamp_millis();
            let event_count = prepared.total_event_count();
            let byte_count = prepared.new_offset.saturating_sub(prepared.offset);
            let prepared_offset = prepared.offset;
            let prepared_new_offset = prepared.new_offset;
            let ship_trace = shipper::ShipTraceContext {
                work_context: work_context(result.job.priority),
                job_started_at_ms,
                prepare_started_at_ms,
                prepare_finished_at_ms,
            };
            match shipper::ship_prepared_file_with_trace(
                prepared,
                &task_context.client,
                &conn,
                Some(&task_context.tracker),
                Some(&task_context.ship_stats),
                Some(&ship_trace),
            )
            .await
            {
                Ok(outcome) => {
                    if outcome.events_shipped > 0 || outcome.dead_lettered > 0 {
                        tracing::info!(
                            context = work_context(result.job.priority),
                            path = %result.job.path.display(),
                            provider = result.job.provider,
                            offset = prepared_offset,
                            new_offset = prepared_new_offset,
                            event_count,
                            events_shipped = outcome.events_shipped,
                            byte_count,
                            bytes_shipped = outcome.bytes_shipped,
                            dead_lettered = outcome.dead_lettered,
                            elapsed_ms = file_start.elapsed().as_millis() as u64,
                            "Shipped transcript path"
                        );
                    }
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

    fn empty_heartbeat_payload() -> heartbeat::HeartbeatPayload {
        heartbeat::HeartbeatPayload {
            version: "test".to_string(),
            daemon_pid: 123,
            last_ship_at: None,
            last_ship_attempt_at: None,
            last_ship_result: None,
            last_ship_latency_ms: None,
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 0,
            spool_dead_count: 0,
            parse_error_count_1h: 0,
            consecutive_ship_failures: 0,
            ship_attempts_1h: 0,
            ship_successes_1h: 0,
            ship_rate_limited_1h: 0,
            ship_server_errors_1h: 0,
            ship_payload_rejections_1h: 0,
            ship_payload_too_large_1h: 0,
            ship_retryable_client_errors_1h: 0,
            ship_connect_errors_1h: 0,
            ship_latency_p50_ms_1h: None,
            ship_latency_p95_ms_1h: None,
            disk_free_bytes: 0,
            is_offline: false,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
        }
    }

    fn codex_bridge_observation(
        transcript_path: &Path,
        active_turn_id: Option<&str>,
        last_turn_status: Option<&str>,
        updated_at: &str,
        bridge_alive: bool,
    ) -> managed_bridge_scan::CodexBridgeObservation {
        managed_bridge_scan::CodexBridgeObservation {
            session_id: "sess-codex-managed".to_string(),
            state_file: PathBuf::from("/tmp/sess-codex-managed.json"),
            cwd: Some("/tmp".to_string()),
            ws_url: Some("ws://127.0.0.1:1111".to_string()),
            status: "ready".to_string(),
            thread_id: Some("thread-live".to_string()),
            thread_path: Some(transcript_path.display().to_string()),
            active_turn_id: active_turn_id.map(str::to_string),
            last_turn_status: last_turn_status.map(str::to_string),
            last_error: None,
            thread_subscription_status: Some("subscribed".to_string()),
            app_server_pid: None,
            app_server_pgid: None,
            updated_at: updated_at.to_string(),
            bridge_alive,
            has_tui_attachment: false,
            app_server_alive: false,
        }
    }

    #[test]
    fn test_runtime_truth_signature_ignores_observation_timestamps() {
        let mut first = empty_heartbeat_payload();
        first
            .unmanaged_session_bindings
            .push(heartbeat::UnmanagedSessionBinding {
                machine_id: "cinder".to_string(),
                provider: "claude".to_string(),
                provider_session_id: "sess-1".to_string(),
                source_path: Some("/tmp/sess-1.jsonl".to_string()),
                source_inode: None,
                source_device: None,
                pid: Some(42),
                process_start_time: Some("2026-05-05T12:00:00Z".to_string()),
                cwd: Some("/tmp/project".to_string()),
                source_offset: Some(100),
                source_mtime: Some("2026-05-05T12:00:01Z".to_string()),
                observed_at: "2026-05-05T12:00:02Z".to_string(),
            });
        let mut second = first.clone();
        second.unmanaged_session_bindings[0].source_offset = Some(200);
        second.unmanaged_session_bindings[0].source_mtime =
            Some("2026-05-05T12:00:10Z".to_string());
        second.unmanaged_session_bindings[0].observed_at = "2026-05-05T12:00:11Z".to_string();

        assert_eq!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_runtime_truth_signature_changes_when_process_identity_changes() {
        let mut first = empty_heartbeat_payload();
        first
            .unmanaged_session_bindings
            .push(heartbeat::UnmanagedSessionBinding {
                machine_id: "cinder".to_string(),
                provider: "claude".to_string(),
                provider_session_id: "sess-1".to_string(),
                source_path: Some("/tmp/sess-1.jsonl".to_string()),
                source_inode: None,
                source_device: None,
                pid: Some(42),
                process_start_time: Some("2026-05-05T12:00:00Z".to_string()),
                cwd: Some("/tmp/project".to_string()),
                source_offset: None,
                source_mtime: None,
                observed_at: "2026-05-05T12:00:02Z".to_string(),
            });
        let mut second = first.clone();
        second.unmanaged_session_bindings[0].pid = Some(43);

        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_runtime_truth_signature_changes_on_managed_lease_state() {
        let mut first = empty_heartbeat_payload();
        first.managed_sessions.push(heartbeat::ManagedSessionLease {
            session_id: "managed-session".to_string(),
            provider: "codex".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 10,
            state: "attached".to_string(),
            phase: Some("idle".to_string()),
            tool_name: None,
            bridge_status: Some("healthy".to_string()),
            thread_subscription_status: None,
            observed_at: "2026-05-05T12:00:02Z".to_string(),
            lease_ttl_ms: 900_000,
        });
        let mut second = first.clone();
        second.managed_sessions[0].state = "detached".to_string();

        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_terminal_phase_schedules_immediate_and_delayed_catchups() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path.clone(),
            "claude",
            "needs_user",
        );

        assert_eq!(catchups.len(), 3);
        assert!(active_polls.is_empty());

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);

        let job = scheduler
            .pop_launchable()
            .expect("immediate catch-up queued");
        assert_eq!(job.path, path);
        assert_eq!(job.provider, "claude");
        assert_eq!(job.priority, WorkPriority::Catchup);
        assert_eq!(catchups.len(), 2, "delayed catch-ups remain queued");
    }

    #[test]
    fn test_active_phase_schedules_single_catchup() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path.clone(),
            "claude",
            "thinking",
        );
        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path.clone(),
            "claude",
            "running",
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(active_polls.len(), 1);

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);

        let job = scheduler.pop_launchable().expect("active catch-up queued");
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Catchup);
        assert!(catchups.is_empty());
    }

    #[test]
    fn test_active_phase_does_not_grow_pending_catchups() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();

        for _ in 0..20 {
            schedule_transcript_catchup(
                &mut catchups,
                &mut active_polls,
                path.clone(),
                "claude",
                "thinking",
            );
        }

        assert_eq!(catchups.len(), 1);
        assert_eq!(active_polls.len(), 1);
    }

    #[test]
    fn test_active_transcript_poll_enqueues_recurring_catchup() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        let now = Instant::now();
        let mut active_polls = HashMap::new();
        active_polls.insert(
            path.clone(),
            ActiveTranscriptPoll {
                due_at: now - Duration::from_secs(1),
                expires_at: now + Duration::from_secs(60),
                provider: "codex",
            },
        );

        let mut scheduler = PathScheduler::new(4);
        drain_due_active_transcript_polls(&mut scheduler, &mut active_polls);

        let job = scheduler.pop_launchable().expect("active poll queued");
        assert_eq!(job.path, path);
        assert_eq!(job.provider, "codex");
        assert_eq!(job.priority, WorkPriority::Catchup);
        assert_eq!(active_polls.len(), 1);
        assert!(active_polls[&path].due_at > now);
    }

    #[test]
    fn test_codex_bridge_observation_starts_transcript_polling() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let observation =
            codex_bridge_observation(transcript.path(), None, None, "2026-05-01T00:00:00Z", true);
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let mut terminal_marks = HashMap::new();

        schedule_transcript_catchups_for_codex_observations(
            &mut catchups,
            &mut active_polls,
            &mut terminal_marks,
            &[observation],
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].path, transcript.path());
        assert_eq!(catchups[0].provider, "codex");
        assert!(active_polls.contains_key(transcript.path()));
    }

    #[test]
    fn test_codex_bridge_observation_completed_turn_schedules_terminal_catchups() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let observation = codex_bridge_observation(
            transcript.path(),
            None,
            Some("completed"),
            "2026-05-01T00:00:00Z",
            true,
        );
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let mut terminal_marks = HashMap::new();

        schedule_transcript_catchups_for_codex_observations(
            &mut catchups,
            &mut active_polls,
            &mut terminal_marks,
            &[observation],
        );

        assert_eq!(catchups.len(), TERMINAL_CATCHUP_DELAYS.len());
        assert!(active_polls.is_empty());
        assert!(catchups
            .iter()
            .all(|catchup| catchup.path == transcript.path()));

        let repeated = codex_bridge_observation(
            transcript.path(),
            None,
            Some("completed"),
            "2026-05-01T00:00:00Z",
            true,
        );
        schedule_transcript_catchups_for_codex_observations(
            &mut catchups,
            &mut active_polls,
            &mut terminal_marks,
            &[repeated],
        );
        assert_eq!(catchups.len(), TERMINAL_CATCHUP_DELAYS.len());
    }

    #[test]
    fn test_codex_bridge_observation_ignores_dead_bridge() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let observation =
            codex_bridge_observation(transcript.path(), None, None, "2026-05-01T00:00:00Z", false);
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let mut terminal_marks = HashMap::new();

        schedule_transcript_catchups_for_codex_observations(
            &mut catchups,
            &mut active_polls,
            &mut terminal_marks,
            &[observation],
        );

        assert!(catchups.is_empty());
        assert!(active_polls.is_empty());
    }

    #[test]
    fn test_drain_transcript_catchups_leaves_future_entries() {
        let ready = tempfile::NamedTempFile::new().unwrap();
        let later = tempfile::NamedTempFile::new().unwrap();
        let now = Instant::now();
        let mut catchups = vec![
            TranscriptCatchup {
                due_at: now - Duration::from_secs(1),
                path: ready.path().to_path_buf(),
                provider: "claude",
            },
            TranscriptCatchup {
                due_at: now + Duration::from_secs(30),
                path: later.path().to_path_buf(),
                provider: "claude",
            },
        ];

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);

        let job = scheduler.pop_launchable().expect("ready catch-up queued");
        assert_eq!(job.path, ready.path());
        assert_eq!(job.priority, WorkPriority::Catchup);
        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].path, later.path());
    }

    #[test]
    fn test_resolves_transcript_path_from_file_state() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();

        FileState::new(&conn)
            .set_offset(&path, 100, "sess-file-state", "sess-file-state", "claude")
            .unwrap();

        assert_eq!(
            resolve_transcript_path_for_session(&conn, "sess-file-state", "claude"),
            Some(transcript.path().to_path_buf())
        );
    }

    #[test]
    fn test_resolves_transcript_path_from_session_binding() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();

        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&path, "sess-binding", "claude")
            .unwrap();

        assert_eq!(
            resolve_transcript_path_for_session(&conn, "sess-binding", "claude"),
            Some(transcript.path().to_path_buf())
        );
    }

    #[test]
    fn test_resolve_transcript_path_falls_back_from_stale_file_state_to_binding() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let binding_transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();

        FileState::new(&conn)
            .set_offset(
                "/tmp/longhouse-stale-transcript-does-not-exist.jsonl",
                100,
                "sess-fallback",
                "sess-fallback",
                "claude",
            )
            .unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(
                &binding_transcript.path().to_string_lossy(),
                "sess-fallback",
                "claude",
            )
            .unwrap();

        assert_eq!(
            resolve_transcript_path_for_session(&conn, "sess-fallback", "claude"),
            Some(binding_transcript.path().to_path_buf())
        );
    }

    #[test]
    fn test_presence_signal_schedules_bound_terminal_transcript() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();

        FileState::new(&conn)
            .set_offset(&path, 100, "sess-signal", "sess-signal", "claude")
            .unwrap();

        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        schedule_transcript_catchups_for_signals(
            &conn,
            &mut catchups,
            &mut active_polls,
            vec![outbox::DrainedPresenceSignal {
                session_id: "sess-signal".to_string(),
                provider: "claude".to_string(),
                phase: "idle".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: None,
            }],
        );

        assert_eq!(catchups.len(), 3);
        assert!(catchups
            .iter()
            .all(|catchup| catchup.path == transcript.path()));
    }

    #[test]
    fn test_presence_signal_uses_hook_transcript_path_before_local_binding() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();

        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        schedule_transcript_catchups_for_signals(
            &conn,
            &mut catchups,
            &mut active_polls,
            vec![outbox::DrainedPresenceSignal {
                session_id: "sess-hook-path".to_string(),
                provider: "codex".to_string(),
                phase: "thinking".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: Some(transcript.path().to_path_buf()),
            }],
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].path, transcript.path());
        assert!(active_polls.contains_key(transcript.path()));
    }

    #[test]
    fn test_transcript_wake_schedules_immediate_catchup() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup_for_wake(
            &mut catchups,
            &mut active_polls,
            TranscriptWakeSignal {
                provider: "codex".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "running".to_string(),
            },
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].path, transcript.path());
        assert!(active_polls.contains_key(transcript.path()));
    }

    #[test]
    fn test_unknown_provider_signal_does_not_schedule_catchup() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();

        FileState::new(&conn)
            .set_offset(&path, 100, "sess-unknown", "sess-unknown", "claude")
            .unwrap();

        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        schedule_transcript_catchups_for_signals(
            &conn,
            &mut catchups,
            &mut active_polls,
            vec![outbox::DrainedPresenceSignal {
                session_id: "sess-unknown".to_string(),
                provider: "unknown-provider".to_string(),
                phase: "idle".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: None,
            }],
        );

        assert!(catchups.is_empty());
    }

    async fn spawn_ingest_server() -> (
        std::net::SocketAddr,
        std::sync::Arc<std::sync::Mutex<Vec<String>>>,
        tokio::task::JoinHandle<()>,
    ) {
        use std::sync::{Arc, Mutex};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let paths: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let paths_clone = paths.clone();

        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };

                let mut buf = vec![0u8; 4096];
                let mut total = 0usize;
                loop {
                    let n = socket.read(&mut buf[total..]).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    total += n;
                    if buf[..total].windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                    if total == buf.len() {
                        buf.resize(buf.len() * 2, 0);
                    }
                }

                let head = String::from_utf8_lossy(&buf[..total]).into_owned();
                let path = head
                    .lines()
                    .next()
                    .and_then(|line| line.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();
                paths_clone.lock().unwrap().push(path);

                let content_len = head
                    .lines()
                    .find(|line| line.to_ascii_lowercase().starts_with("content-length:"))
                    .and_then(|line| line.split(':').nth(1))
                    .and_then(|value| value.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                let header_end = buf[..total]
                    .windows(4)
                    .position(|window| window == b"\r\n\r\n")
                    .unwrap()
                    + 4;
                let mut body_read = total - header_end;
                while body_read < content_len {
                    let n = socket.read(&mut buf).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    body_read += n;
                }

                let _ = socket
                    .write_all(b"HTTP/1.1 204\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    .await;
                let _ = socket.shutdown().await;
            }
        });

        (addr, paths, handle)
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_presence_catchup_ships_bound_transcript_tail_and_advances_offset() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir
            .path()
            .join("11111111-1111-4111-8111-111111111111.jsonl");
        std::fs::write(
            &transcript,
            concat!(
                r#"{"type":"assistant","uuid":"a1","timestamp":"2026-01-01T00:00:01Z","message":{"content":[{"type":"text","text":"done"}]}}"#,
                "\n"
            ),
        )
        .unwrap();

        let managed_session_id = "22222222-2222-4222-8222-222222222222";
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(&transcript)
            .unwrap()
            .to_string_lossy()
            .to_string();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical, managed_session_id, "claude")
            .unwrap();

        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        schedule_transcript_catchups_for_signals(
            &conn,
            &mut catchups,
            &mut active_polls,
            vec![outbox::DrainedPresenceSignal {
                session_id: managed_session_id.to_string(),
                provider: "claude".to_string(),
                phase: "needs_user".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: None,
            }],
        );
        assert_eq!(catchups.len(), 3);

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);
        let job = scheduler
            .pop_launchable()
            .expect("presence-driven catch-up queued");

        let (addr, logged_paths, server) = spawn_ingest_server().await;
        let api_url = format!("http://{}", addr);
        let shipper_config = ShipperConfig::default().with_overrides(
            Some(&api_url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client =
            ShipperClient::with_compression(&shipper_config, CompressionAlgo::Gzip).unwrap();
        let task_context = PathTaskContext {
            shipper_config,
            client,
            algo: CompressionAlgo::Gzip,
            tracker: ConsecutiveErrorTracker::new(),
            parse_tracker: RecentIssueTracker::new(),
            ship_stats: RecentShipStatsTracker::new(),
        };

        let result = run_path_job(job, task_context).await;
        assert_eq!(result.events_shipped, 1);

        let expected_offset = std::fs::metadata(&transcript).unwrap().len();
        assert_eq!(
            FileState::new(&conn).get_offset(&canonical).unwrap(),
            expected_offset
        );
        assert_eq!(
            FileState::new(&conn)
                .get_session(&canonical)
                .unwrap()
                .and_then(|tracked| tracked.session_id),
            Some(managed_session_id.to_string())
        );

        server.abort();
        assert_eq!(
            logged_paths.lock().unwrap().as_slice(),
            ["/api/agents/ingest"]
        );
    }

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
