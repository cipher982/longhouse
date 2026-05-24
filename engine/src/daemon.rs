//! Daemon mode (`connect` subcommand).
//!
//! Watches provider directories for file changes using the `notify` crate
//! (FSEvents on macOS, inotify on Linux) and ships new session data
//! incrementally. Designed for 24/7 operation with minimal resources:
//! - <10 MB RSS when idle
//! - 0% CPU when idle (blocked on kernel filesystem events)
//! - Lightweight background work with bounded concurrency

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::Result;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::io::AsyncReadExt;
use tokio::sync::mpsc;
use tokio::task::JoinSet;

use crate::config::{self, ShipperConfig};
use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::flight::FlightRecorder;
use crate::heartbeat;
use crate::managed_bridge_scan;
use crate::managed_claude_scan;
use crate::managed_reaper::ManagedBridgeReaper;
use crate::outbox;
use crate::pipeline::compressor::CompressionAlgo;
use crate::scheduler::{ObservationTrace, PathJob, PathScheduler, WorkPriority};
use crate::shipper;
use crate::shipping::client::ShipperClient;
use crate::shipping_stats::RecentShipStatsTracker;
use crate::state::db::open_db;
use crate::state::db_pool::ConnectionPool;
use crate::state::file_state::FileState;
use crate::state::spool::Spool;
use crate::watcher::{SessionWatcher, WatcherEvent};

/// Configuration for the connect daemon.
pub struct ConnectConfig {
    pub shipper_config: ShipperConfig,
    pub algo: CompressionAlgo,
    pub fallback_scan_secs: u64,
    pub spool_replay_secs: u64,
    pub flight_recorder_dir: Option<PathBuf>,
}

/// How long to coalesce a burst of filesystem events before scheduling work.
/// Short enough to leave the bulk of the 500ms file-append → HTTP-send budget
/// for actual shipping; long enough to coalesce the typical JSONL append
/// burst while keeping live transcript shipping responsive. Provider writes
/// that need more coalescing are still protected by per-path in-flight work
/// and the reconciliation scanner.
const WATCHER_FLUSH_INTERVAL: Duration = Duration::from_millis(15);

const INITIAL_SPOOL_PATH_LIMIT: usize = 100;
const PERIODIC_SPOOL_PATH_LIMIT: usize = 50;
const PATH_SPOOL_REPLAY_LIMIT: usize = 50;
const LOCAL_RETRY_DELAY_SECS: u64 = 5;
const LIVE_LOCAL_RETRY_DELAY: Duration = Duration::from_millis(500);
const STARTUP_RECONCILIATION_SCAN_DELAY: Duration = Duration::from_secs(120);
const LOCAL_STATUS_INTERVAL_SECS: u64 = 1;
const SERVER_HEARTBEAT_INTERVAL_SECS: u64 = 5 * 60;
const FLIGHT_SAMPLE_INTERVAL_SECS: u64 = 5;
const LOCAL_WORK_TICK_INTERVAL: Duration = Duration::from_millis(250);
const OUTBOX_DRAIN_INTERVAL: Duration = Duration::from_millis(100);
const UNMANAGED_BINDING_REFRESH_INTERVAL: Duration = Duration::from_secs(30);
const MANAGED_WAKE_FSEVENT_DEFER_WINDOW: Duration = Duration::from_secs(30);
const MANAGED_WAKE_FSEVENT_FALLBACK_DELAY: Duration = Duration::from_secs(5);
const MAX_TRANSCRIPT_WAKE_TRACKED_PATHS: usize = 4096;
const OFFLINE_CONNECT_FAILURE_THRESHOLD: u32 = 3;
const CLAUDE_TERMINAL_EVENT_TIMEOUT: Duration = Duration::from_secs(2);
const CLAUDE_TERMINAL_EVENT_SOURCE: &str = "claude_channel_scan";
const CLAUDE_TERMINAL_EVENT_STALE_SECS: i64 = 10 * 60;
const CLAUDE_TERMINAL_EVENT_BATCH_LIMIT: usize = 128;

/// Offline / connectivity state.
struct OfflineState {
    is_offline: bool,
    offline_since: Option<Instant>,
    consecutive_connect_failures: u32,
}

impl OfflineState {
    fn new() -> Self {
        Self {
            is_offline: false,
            offline_since: None,
            consecutive_connect_failures: 0,
        }
    }

    fn record_connect_error(&mut self) -> bool {
        self.consecutive_connect_failures += 1;
        if self.consecutive_connect_failures < OFFLINE_CONNECT_FAILURE_THRESHOLD {
            return false;
        }
        if self.is_offline {
            return false;
        }
        self.is_offline = true;
        self.offline_since = Some(Instant::now());
        true
    }

    fn mark_online(&mut self) -> Option<Duration> {
        self.consecutive_connect_failures = 0;
        if self.is_offline {
            let duration = self.offline_since.map(|t| t.elapsed());
            self.is_offline = false;
            self.offline_since = None;
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
    flight_recorder: Option<FlightRecorder>,
    limiter: std::sync::Arc<crate::scheduler::AdaptiveLimiter>,
    /// Reusable shipper-DB connections. Schema bootstrap has already run
    /// during `run()`; per-job code uses leases instead of `open_db`.
    db_pool: ConnectionPool,
}

struct PathTaskResult {
    job: PathJob,
    events_shipped: usize,
    resolved_spool: usize,
    failed_spool: usize,
    had_connect_error: bool,
    rerun_priority: Option<WorkPriority>,
    local_retry_after: Option<Duration>,
    local_retry_priority: Option<WorkPriority>,
    processing_elapsed: Duration,
}

struct DeferredRetry {
    due_at: Instant,
    provider: &'static str,
    priority: WorkPriority,
    observation: ObservationTrace,
}

struct HeartbeatPostResult {
    signature: String,
    reason: &'static str,
    result: Result<(), String>,
    join_elapsed_ms: u64,
    task_elapsed_ms: u64,
}

struct OutboxCollectResult {
    presence: outbox::OutboxLocalDrainResult,
    runtime_posts: Vec<outbox::PendingRuntimeEventPost>,
    elapsed_ms: u64,
}

#[derive(Debug, Clone)]
struct ClaudeLiveChannelSession {
    session_id: String,
    provider_session_id: Option<String>,
    claude_pid: Option<u32>,
}

#[derive(Debug, Clone)]
struct ClaudeTerminalSignal {
    dedupe_key: String,
    observed_at: chrono::DateTime<chrono::Utc>,
    event: Value,
}

struct ClaudeTerminalPostResult {
    dedupe_keys: Vec<String>,
    result: Result<(), String>,
    join_elapsed_ms: u64,
    task_elapsed_ms: u64,
}

struct UnmanagedBindingRefreshResult {
    reason: &'static str,
    result: Result<Vec<heartbeat::UnmanagedSessionBinding>, String>,
    elapsed_ms: u64,
}

#[derive(Debug, Clone, Deserialize)]
struct TranscriptWakeSignal {
    provider: String,
    path: PathBuf,
    phase: String,
    #[serde(default = "now_ms")]
    observed_at_ms: i64,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    turn_id: Option<String>,
    #[serde(default)]
    wake_reason: Option<String>,
    #[serde(default)]
    file_len_hint: Option<u64>,
    #[serde(skip)]
    received_at_ms: Option<i64>,
}

struct DiscoveryTaskResult {
    files: Vec<(PathBuf, &'static str)>,
    priority: WorkPriority,
    reason: &'static str,
}

struct ManagedObservationScanResult {
    reason: &'static str,
    codex_observations: Vec<managed_bridge_scan::CodexBridgeObservation>,
    claude_observations: Vec<managed_claude_scan::ClaudeChannelObservation>,
    elapsed_ms: u64,
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
    let flight_recorder = config
        .flight_recorder_dir
        .clone()
        .map(FlightRecorder::start)
        .transpose()?;
    if let Some(recorder) = flight_recorder.as_ref() {
        recorder.record(json!({
            "schema": "flight_event.v1",
            "kind": "startup",
            "machine_name": &config.shipper_config.machine_name,
            "api_url": &config.shipper_config.api_url,
            "flight_recorder_dir": config.flight_recorder_dir.as_ref().map(|path| path.to_string_lossy().to_string()),
        }));
        tracing::info!(
            dir = %config.flight_recorder_dir.as_ref().map(|path| path.display().to_string()).unwrap_or_default(),
            "Machine Agent flight recorder enabled"
        );
    }
    let adaptive_limiter = crate::scheduler::AdaptiveLimiter::new();
    // Pool sized for the live cap + headroom for retry/scan tasks. Idle pool
    // is bounded; spillover connections are dropped on return.
    let db_pool = ConnectionPool::new(
        config.shipper_config.db_path.as_deref(),
        config.shipper_config.workers.max(1) + 4,
    )?;
    let task_context = PathTaskContext {
        shipper_config: config.shipper_config.clone(),
        client: client.clone(),
        algo: config.algo,
        tracker: tracker.clone(),
        parse_tracker: parse_tracker.clone(),
        ship_stats: ship_stats.clone(),
        flight_recorder: flight_recorder.clone(),
        limiter: std::sync::Arc::clone(&adaptive_limiter),
        db_pool: db_pool.clone(),
    };

    // 6. Start file watcher before catch-up work so live changes queue immediately.
    let mut watcher = SessionWatcher::new(&providers)?;
    tracing::info!(
        "Daemon ready — watching for file changes (flush interval: {:?})",
        WATCHER_FLUSH_INTERVAL
    );

    // 7. Build bounded per-path scheduler and queue startup work.
    // Total breadth comes from `config.workers` (num_cpus by default); the
    // scheduler enforces per-priority caps (LIVE_IN_FLIGHT_CAP=8 in scheduler.rs)
    // and a Live reservation so backlog work can't drain Live slots.
    let max_in_flight = config.shipper_config.workers.max(1);
    let mut scheduler =
        PathScheduler::with_limiter(max_in_flight, std::sync::Arc::clone(&adaptive_limiter));
    let mut in_flight = JoinSet::new();
    let mut discovery_tasks: JoinSet<DiscoveryTaskResult> = JoinSet::new();
    let mut managed_observation_scan_tasks: JoinSet<ManagedObservationScanResult> = JoinSet::new();
    let mut deferred_retries = HashMap::new();

    let initial_retry_paths =
        queue_pending_spool_paths(&mut scheduler, &conn, INITIAL_SPOOL_PATH_LIMIT)?;
    maybe_start_managed_observation_scan(&mut managed_observation_scan_tasks, "startup");
    tracing::info!(
        "Queued startup catch-up: {} retry paths; startup reconciliation deferred by {:?} (max {} concurrent)",
        initial_retry_paths,
        STARTUP_RECONCILIATION_SCAN_DELAY,
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
    local_status_timer.tick().await; // consume first immediate tick
    let mut flight_sample_timer =
        tokio::time::interval(Duration::from_secs(FLIGHT_SAMPLE_INTERVAL_SECS));
    flight_sample_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    flight_sample_timer.tick().await; // consume first immediate tick

    let mut outbox_timer = tokio::time::interval(OUTBOX_DRAIN_INTERVAL);
    outbox_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    outbox_timer.tick().await; // consume first immediate tick
    let mut local_retry_timer = tokio::time::interval(LOCAL_WORK_TICK_INTERVAL);
    local_retry_timer.tick().await; // consume first immediate tick
    let startup_reconciliation_timer = tokio::time::sleep(STARTUP_RECONCILIATION_SCAN_DELAY);
    tokio::pin!(startup_reconciliation_timer);
    let mut startup_reconciliation_pending = true;

    let mut offline = OfflineState::new();
    let mut last_ship_at: Option<String> = None;
    let mut last_runtime_truth_signature: Option<String> = None;
    let mut runtime_truth_bootstrapped = false;
    let mut last_unmanaged_session_bindings: Option<Vec<heartbeat::UnmanagedSessionBinding>> = None;
    let mut last_unmanaged_session_bindings_refreshed_at: Option<Instant> = None;
    let mut latest_transcript_wake_observed: HashMap<PathBuf, i64> = HashMap::new();
    let mut managed_codex_transcript_paths: HashSet<PathBuf> = HashSet::new();
    let mut bridge_reaper = ManagedBridgeReaper::from_env();
    let mut outbox_collect_tasks: JoinSet<OutboxCollectResult> = JoinSet::new();
    let mut outbox_post_tasks: JoinSet<(usize, usize, u64, u64)> = JoinSet::new();
    let mut runtime_outbox_post_tasks: JoinSet<(usize, usize, u64, u64)> = JoinSet::new();
    let mut heartbeat_post_tasks: JoinSet<HeartbeatPostResult> = JoinSet::new();
    let mut claude_terminal_post_tasks: JoinSet<ClaudeTerminalPostResult> = JoinSet::new();
    let mut unmanaged_binding_refresh_tasks: JoinSet<UnmanagedBindingRefreshResult> =
        JoinSet::new();
    let mut live_claude_channels: HashMap<String, ClaudeLiveChannelSession> = HashMap::new();
    let mut pending_claude_terminal_signals: HashMap<String, ClaudeTerminalSignal> = HashMap::new();

    let outbox_dir = config::get_agent_outbox_dir()?;
    let runtime_events_outbox_dir = config::get_agent_runtime_events_outbox_dir()?;
    let status_path = config::get_agent_status_path()?;
    if let Some(parent) = status_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let control_channel_status = crate::control_channel::new_control_channel_status();
    let control_channel_task = crate::control_channel::spawn_control_channel(
        config.shipper_config.clone(),
        control_channel_status.clone(),
    );
    let (transcript_wake_tx, mut transcript_wake_rx) = mpsc::unbounded_channel();
    let transcript_wake_task = spawn_transcript_wake_listener(transcript_wake_tx)?;
    maybe_start_unmanaged_binding_refresh(
        &mut unmanaged_binding_refresh_tasks,
        config.shipper_config.db_path.clone(),
        config.shipper_config.machine_name.clone(),
        last_unmanaged_session_bindings_refreshed_at,
        Instant::now(),
        "startup",
    );

    loop {
        pump_ready_local_work(
            &mut scheduler,
            &mut in_flight,
            &task_context,
            &mut deferred_retries,
            offline.is_offline,
        );

        tokio::select! {
            biased;

            // Shutdown signals
            _ = shutdown_signal() => {
                tracing::info!("Shutdown signal received, exiting gracefully...");
                break;
            }

            // Managed transcript wakes are the lowest-latency completion lane.
            // Periodic local status/outbox work can do synchronous filesystem
            // and SQLite reads, so do not let a ready timer win the select race
            // while a turn-completion wake is already waiting.
            Some(signal) = transcript_wake_rx.recv() => {
                if let Some(path) = enqueue_transcript_wake_signal(
                    &mut scheduler,
                    &mut latest_transcript_wake_observed,
                    signal,
                ) {
                    deferred_retries.remove(&path);
                    pump_ready_local_work(
                        &mut scheduler,
                        &mut in_flight,
                        &task_context,
                        &mut deferred_retries,
                        offline.is_offline,
                    );
                }
            }

            task_result = in_flight.join_next(), if scheduler.has_in_flight() => {
                match task_result {
                    Some(Ok(result)) => {
                        let retry_path = result.job.path.clone();
                        let retry_provider = result.job.provider;
                        scheduler.complete(&retry_path, result.rerun_priority);
                        if let Some(delay) = result.local_retry_after {
                            let priority = result.local_retry_priority.unwrap_or(result.job.priority);
                            deferred_retries.insert(retry_path, DeferredRetry {
                                due_at: Instant::now() + delay,
                                provider: retry_provider,
                                priority,
                                observation: result.job.observation.clone(),
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
                            if offline.record_connect_error() {
                                tracing::warn!(
                                    threshold = OFFLINE_CONNECT_FAILURE_THRESHOLD,
                                    "Connection error threshold reached while processing {} — entering offline mode",
                                    result.job.path.display()
                                );
                            } else {
                                tracing::warn!(
                                    consecutive_connect_errors = offline.consecutive_connect_failures,
                                    threshold = OFFLINE_CONNECT_FAILURE_THRESHOLD,
                                    "Connection error while processing {}; keeping local shipping active",
                                    result.job.path.display()
                                );
                            }
                        } else if result.events_shipped > 0 || result.resolved_spool > 0 {
                            last_ship_at = Some(chrono::Utc::now().to_rfc3339());
                            if let Some(duration) = offline.mark_online() {
                                last_runtime_truth_signature = None;
                                tracing::info!(
                                    "Back online after {:.0}s — resuming shipping",
                                    duration.as_secs_f64()
                                );
                            }
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

            outbox_collect_result = outbox_collect_tasks.join_next(), if !outbox_collect_tasks.is_empty() => {
                match outbox_collect_result {
                    Some(Ok(result)) => {
                        if result.elapsed_ms > 100 {
                            tracing::warn!(
                                elapsed_ms = result.elapsed_ms,
                                presence_posts = result.presence.posts.len(),
                                runtime_posts = result.runtime_posts.len(),
                                "Outbox collection was slow"
                            );
                        }
                        if !result.presence.signals.is_empty() {
                            tracing::debug!(
                                signal_count = result.presence.signals.len(),
                                "Ignoring hook outbox transcript catch-up signals"
                            );
                        }
                        if !result.presence.posts.is_empty() {
                            if outbox_post_tasks.is_empty() {
                                let client = client.clone();
                                let posts = result.presence.posts;
                                let post_count = posts.len();
                                outbox_post_tasks.spawn_local(async move {
                                    let join_started = Instant::now();
                                    let post_task = tokio::spawn(async move {
                                        let task_started = Instant::now();
                                        let (sent, kept) =
                                            outbox::post_pending_presence_files(&client, posts).await;
                                        (sent, kept, task_started.elapsed().as_millis() as u64)
                                    });
                                    match post_task.await {
                                        Ok((sent, kept, task_elapsed_ms)) => (
                                            sent,
                                            kept,
                                            join_started.elapsed().as_millis() as u64,
                                            task_elapsed_ms,
                                        ),
                                        Err(err) => {
                                            tracing::warn!(
                                                post_count,
                                                "Outbox presence POST worker task failed: {}",
                                                err
                                            );
                                            (
                                                0,
                                                post_count,
                                                join_started.elapsed().as_millis() as u64,
                                                join_started.elapsed().as_millis() as u64,
                                            )
                                        }
                                    }
                                });
                            } else {
                                tracing::debug!(
                                    pending_posts = result.presence.posts.len(),
                                    "Skipping outbox presence POST while previous POST is still in flight"
                                );
                            }
                        }
                        if !result.runtime_posts.is_empty() {
                            if runtime_outbox_post_tasks.is_empty() {
                                let client = client.clone();
                                let runtime_posts = result.runtime_posts;
                                let post_count = runtime_posts.len();
                                runtime_outbox_post_tasks.spawn_local(async move {
                                    let join_started = Instant::now();
                                    let post_task = tokio::spawn(async move {
                                        let task_started = Instant::now();
                                        let (sent, kept) =
                                            outbox::post_pending_runtime_event_files(&client, runtime_posts)
                                                .await;
                                        (sent, kept, task_started.elapsed().as_millis() as u64)
                                    });
                                    match post_task.await {
                                        Ok((sent, kept, task_elapsed_ms)) => (
                                            sent,
                                            kept,
                                            join_started.elapsed().as_millis() as u64,
                                            task_elapsed_ms,
                                        ),
                                        Err(err) => {
                                            tracing::warn!(
                                                post_count,
                                                "Outbox runtime-event POST worker task failed: {}",
                                                err
                                            );
                                            (
                                                0,
                                                post_count,
                                                join_started.elapsed().as_millis() as u64,
                                                join_started.elapsed().as_millis() as u64,
                                            )
                                        }
                                    }
                                });
                            } else {
                                tracing::debug!(
                                    pending_posts = result.runtime_posts.len(),
                                    "Skipping outbox runtime-event POST while previous POST is still in flight"
                                );
                            }
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Outbox collection task failed: {}", err);
                    }
                    None => {}
                }
            }

            outbox_post_result = outbox_post_tasks.join_next(), if !outbox_post_tasks.is_empty() => {
                match outbox_post_result {
                    Some(Ok((sent, kept, join_elapsed_ms, task_elapsed_ms))) => {
                        let local_join_delay_ms = join_elapsed_ms.saturating_sub(task_elapsed_ms);
                        if kept > 0 {
                            tracing::warn!(
                                sent,
                                kept,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Outbox presence POST kept files for retry"
                            );
                        } else if join_elapsed_ms > 1_000 {
                            tracing::warn!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Outbox presence POST was slow"
                            );
                        } else if sent > 0 {
                            tracing::debug!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                "Outbox presence POST sent files"
                            );
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Outbox presence POST task failed: {}", err);
                    }
                    None => {}
                }
            }

            runtime_outbox_post_result = runtime_outbox_post_tasks.join_next(), if !runtime_outbox_post_tasks.is_empty() => {
                match runtime_outbox_post_result {
                    Some(Ok((sent, kept, join_elapsed_ms, task_elapsed_ms))) => {
                        let local_join_delay_ms = join_elapsed_ms.saturating_sub(task_elapsed_ms);
                        if kept > 0 {
                            tracing::warn!(
                                sent,
                                kept,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Outbox runtime-event POST kept files for retry"
                            );
                        } else if join_elapsed_ms > 1_000 {
                            tracing::warn!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Outbox runtime-event POST was slow"
                            );
                        } else if sent > 0 {
                            tracing::debug!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                "Outbox runtime-event POST sent files"
                            );
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Outbox runtime-event POST task failed: {}", err);
                    }
                    None => {}
                }
            }

            heartbeat_post_result = heartbeat_post_tasks.join_next(), if !heartbeat_post_tasks.is_empty() => {
                match heartbeat_post_result {
                    Some(Ok(result)) => {
                        let local_join_delay_ms =
                            result.join_elapsed_ms.saturating_sub(result.task_elapsed_ms);
                        if result.task_elapsed_ms > 1_000 || local_join_delay_ms > 1_000 {
                            tracing::warn!(
                                reason = result.reason,
                                task_elapsed_ms = result.task_elapsed_ms,
                                join_elapsed_ms = result.join_elapsed_ms,
                                local_join_delay_ms,
                                "Heartbeat POST was slow"
                            );
                        }
                        match result.result {
                            Ok(()) => {
                                tracing::debug!(
                                    reason = result.reason,
                                    task_elapsed_ms = result.task_elapsed_ms,
                                    join_elapsed_ms = result.join_elapsed_ms,
                                    "Runtime truth snapshot sent after local process/control change"
                                );
                                last_runtime_truth_signature = Some(result.signature);
                            }
                            Err(err) => {
                                last_runtime_truth_signature = None;
                                tracing::debug!(
                                    reason = result.reason,
                                    "Runtime truth snapshot send failed: {}",
                                    err
                                );
                            }
                        }
                    }
                    Some(Err(err)) => {
                        last_runtime_truth_signature = None;
                        tracing::warn!("Heartbeat POST task failed: {}", err);
                    }
                    None => {}
                }
            }

            claude_terminal_post_result = claude_terminal_post_tasks.join_next(), if !claude_terminal_post_tasks.is_empty() => {
                match claude_terminal_post_result {
                    Some(Ok(result)) => {
                        let local_join_delay_ms =
                            result.join_elapsed_ms.saturating_sub(result.task_elapsed_ms);
                        if result.task_elapsed_ms > 1_000 || local_join_delay_ms > 1_000 {
                            tracing::warn!(
                                task_elapsed_ms = result.task_elapsed_ms,
                                join_elapsed_ms = result.join_elapsed_ms,
                                local_join_delay_ms,
                                "Managed Claude terminal signal POST was slow"
                            );
                        }
                        match result.result {
                            Ok(()) => {
                                for key in result.dedupe_keys {
                                    pending_claude_terminal_signals.remove(&key);
                                }
                            }
                            Err(err) => {
                                tracing::debug!(
                                    "Managed Claude terminal signal POST failed: {}",
                                    err
                                );
                            }
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Managed Claude terminal signal task failed: {}", err);
                    }
                    None => {}
                }
            }

            unmanaged_binding_refresh_result = unmanaged_binding_refresh_tasks.join_next(), if !unmanaged_binding_refresh_tasks.is_empty() => {
                match unmanaged_binding_refresh_result {
                    Some(Ok(result)) => {
                        match result.result {
                            Ok(bindings) => {
                                if result.elapsed_ms > 1_000 {
                                    tracing::warn!(
                                        reason = result.reason,
                                        binding_count = bindings.len(),
                                        elapsed_ms = result.elapsed_ms,
                                        "Unmanaged binding refresh was slow"
                                    );
                                } else {
                                    tracing::debug!(
                                        reason = result.reason,
                                        binding_count = bindings.len(),
                                        elapsed_ms = result.elapsed_ms,
                                        "Unmanaged binding refresh completed"
                                    );
                                }
                                last_unmanaged_session_bindings = Some(bindings);
                                last_unmanaged_session_bindings_refreshed_at = Some(Instant::now());
                            }
                            Err(err) => {
                                tracing::warn!(
                                    reason = result.reason,
                                    elapsed_ms = result.elapsed_ms,
                                    "Unmanaged binding refresh failed: {}",
                                    err
                                );
                            }
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Unmanaged binding refresh task failed: {}", err);
                    }
                    None => {}
                }
            }

            managed_observation_scan_result = managed_observation_scan_tasks.join_next(), if !managed_observation_scan_tasks.is_empty() => {
                match managed_observation_scan_result {
                    Some(Ok(result)) => {
                        if result.elapsed_ms > 250 {
                            tracing::warn!(
                                reason = result.reason,
                                codex_count = result.codex_observations.len(),
                                claude_count = result.claude_observations.len(),
                                elapsed_ms = result.elapsed_ms,
                                "Managed observation scan was slow"
                            );
                        } else {
                            tracing::debug!(
                                reason = result.reason,
                                codex_count = result.codex_observations.len(),
                                claude_count = result.claude_observations.len(),
                                elapsed_ms = result.elapsed_ms,
                                "Managed observation scan completed"
                            );
                        }
                        refresh_managed_codex_transcript_paths(
                            &mut managed_codex_transcript_paths,
                            &result.codex_observations,
                        );
                        reconcile_claude_terminal_signals(
                            &mut live_claude_channels,
                            &mut pending_claude_terminal_signals,
                            &config.shipper_config.machine_name,
                            &result.claude_observations,
                            chrono::Utc::now(),
                        );
                        maybe_spawn_claude_terminal_post(
                            &mut claude_terminal_post_tasks,
                            client.clone(),
                            &pending_claude_terminal_signals,
                            offline.is_offline,
                        );
                        pump_ready_local_work(
                            &mut scheduler,
                            &mut in_flight,
                            &task_context,
                            &mut deferred_retries,
                            offline.is_offline,
                        );
                        maybe_start_unmanaged_binding_refresh(
                            &mut unmanaged_binding_refresh_tasks,
                            config.shipper_config.db_path.clone(),
                            config.shipper_config.machine_name.clone(),
                            last_unmanaged_session_bindings_refreshed_at,
                            Instant::now(),
                            result.reason,
                        );
                        let empty_unmanaged_bindings: &[heartbeat::UnmanagedSessionBinding] = &[];
                        let unmanaged_binding_override =
                            last_unmanaged_session_bindings.as_deref().unwrap_or(empty_unmanaged_bindings);
                        let payload = write_local_status_snapshot(
                            &conn,
                            &tracker,
                            &parse_tracker,
                            &ship_stats,
                            offline.is_offline,
                            &last_ship_at,
                            &config.shipper_config.machine_name,
                            &status_path,
                            &result.codex_observations,
                            &result.claude_observations,
                            serde_json::to_value(control_channel_status.snapshot()).ok(),
                            Some(unmanaged_binding_override),
                            Some(adaptive_limiter.as_ref()),
                        );
                        bridge_reaper.tick(&result.codex_observations);
                        let signature = runtime_truth_signature(&payload);
                        if !runtime_truth_bootstrapped {
                            last_runtime_truth_signature = Some(signature);
                            runtime_truth_bootstrapped = true;
                            continue;
                        }
                        if !offline.is_offline && last_runtime_truth_signature.as_deref() != Some(signature.as_str()) {
                            if heartbeat_post_tasks.is_empty() {
                                spawn_heartbeat_post(
                                    &mut heartbeat_post_tasks,
                                    client.clone(),
                                    payload,
                                    signature,
                                    "runtime_truth_change",
                                );
                            } else {
                                last_runtime_truth_signature = None;
                                tracing::debug!(
                                    "Runtime truth snapshot changed while a heartbeat POST is still in flight"
                                );
                            }
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Managed observation scan task failed: {}", err);
                    }
                    None => {}
                }
            }

            _ = &mut startup_reconciliation_timer, if startup_reconciliation_pending && !offline.is_offline => {
                startup_reconciliation_pending = false;
                maybe_start_reconciliation_scan(
                    &mut discovery_tasks,
                    &providers,
                    &scheduler,
                    &deferred_retries,
                    "startup reconciliation",
                );
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

            // File change events (primary path). Keep collecting changes during
            // soft offline windows so short transport hiccups cannot stale the
            // local outbox or miss session wakeups.
            Some(first_event) = watcher.next_event() => {
                // Keep the coalescing wait cancellable by transcript wakes.
                // The wake socket is the managed-session completion lane, so
                // it should not sit behind filesystem batching.
                let flush = tokio::time::sleep(WATCHER_FLUSH_INTERVAL);
                tokio::pin!(flush);
                loop {
                    tokio::select! {
                        biased;
                        Some(signal) = transcript_wake_rx.recv() => {
                            if let Some(path) = enqueue_transcript_wake_signal(
                                &mut scheduler,
                                &mut latest_transcript_wake_observed,
                                signal,
                            ) {
                                deferred_retries.remove(&path);
                                pump_ready_local_work(
                                    &mut scheduler,
                                    &mut in_flight,
                                    &task_context,
                                    &mut deferred_retries,
                                    offline.is_offline,
                                );
                            }
                        }
                        _ = &mut flush => {
                            break;
                        }
                    }
                }
                let events = watcher.collect_ready_batch(first_event);
                for event in events {
                    if let Some(provider) = discovery::provider_for_path(&event.path, &providers) {
                        if should_defer_fsevent_for_managed_wake(
                            &latest_transcript_wake_observed,
                            &managed_codex_transcript_paths,
                            &event,
                            provider,
                        ) {
                            let observation = ObservationTrace {
                                source: "fsevent",
                                observed_at_ms: event.observed_at_ms,
                                latest_observed_at_ms: Some(
                                    event.latest_observed_at_ms.max(event.observed_at_ms),
                                ),
                                wake_received_at_ms: None,
                                enqueued_at_ms: now_ms(),
                                session_id: None,
                                turn_id: None,
                                wake_reason: None,
                                file_len_hint: None,
                            };
                            deferred_retries.insert(
                                event.path.clone(),
                                DeferredRetry {
                                    due_at: Instant::now() + MANAGED_WAKE_FSEVENT_FALLBACK_DELAY,
                                    provider,
                                    priority: WorkPriority::Live,
                                    observation,
                                },
                            );
                            tracing::debug!(
                                provider,
                                path = %event.path.display(),
                                observed_at_ms = event.observed_at_ms,
                                latest_observed_at_ms = event.latest_observed_at_ms,
                                delay_ms = MANAGED_WAKE_FSEVENT_FALLBACK_DELAY.as_millis(),
                                "Deferring filesystem live ship because managed wake socket owns this turn"
                            );
                            continue;
                        }
                        scheduler.enqueue_observed_window(
                            event.path,
                            provider,
                            WorkPriority::Live,
                            "fsevent",
                            event.observed_at_ms,
                            event.latest_observed_at_ms,
                        );
                    } else {
                        tracing::debug!(
                            "Skipping file outside known providers: {}",
                            event.path.display()
                        );
                    }
                }
            }

            // Periodic reconciliation scan — repair missed file-watch work after
            // restarts, sleeps, or dropped OS notifications.
            _ = fallback_timer.tick(), if !offline.is_offline => {
                maybe_start_reconciliation_scan(
                    &mut discovery_tasks,
                    &providers,
                    &scheduler,
                    &deferred_retries,
                    "reconciliation scan",
                );
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

            // Outbox drain: presence events written by hooks. These are runtime
            // overlay signals only; transcript shipping is owned by filesystem
            // events plus reconciliation scans.
            _ = outbox_timer.tick() => {
                if outbox_collect_tasks.is_empty() {
                    let outbox_dir = outbox_dir.clone();
                    let runtime_events_outbox_dir = runtime_events_outbox_dir.clone();
                    let db_path = config.shipper_config.db_path.clone();
                    outbox_collect_tasks.spawn_blocking(move || {
                        let started = Instant::now();
                        let presence = outbox::collect_outbox_with_local_state_result(
                            &outbox_dir,
                            db_path.as_deref(),
                        );
                        let runtime_posts =
                            outbox::collect_runtime_event_outbox(&runtime_events_outbox_dir);
                        OutboxCollectResult {
                            presence,
                            runtime_posts,
                            elapsed_ms: started.elapsed().as_millis() as u64,
                        }
                    });
                }
            }

            // Wake the loop when delayed local retry work may now be ready.
            _ = local_retry_timer.tick(), if !deferred_retries.is_empty() => {}

            _ = flight_sample_timer.tick(), if flight_recorder.is_some() => {
                if let Some(recorder) = flight_recorder.as_ref() {
                    record_flight_sample(
                        recorder,
                        &conn,
                        &outbox_dir,
                        &control_channel_status,
                        &ship_stats,
                        &config.shipper_config.machine_name,
                        in_flight.len(),
                        scheduler.has_pending_work(),
                        deferred_retries.len(),
                        offline.is_offline,
                    );
                }
            }

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
                maybe_start_managed_observation_scan(&mut managed_observation_scan_tasks, "local_status");
            }

            // Periodic server heartbeat
            _ = heartbeat_timer.tick() => {
                let observations = managed_bridge_scan::collect_observations();
                refresh_managed_codex_transcript_paths(
                    &mut managed_codex_transcript_paths,
                    &observations,
                );
                let claude_observations = managed_claude_scan::collect_observations();
                reconcile_claude_terminal_signals(
                    &mut live_claude_channels,
                    &mut pending_claude_terminal_signals,
                    &config.shipper_config.machine_name,
                    &claude_observations,
                    chrono::Utc::now(),
                );
                maybe_spawn_claude_terminal_post(
                    &mut claude_terminal_post_tasks,
                    client.clone(),
                    &pending_claude_terminal_signals,
                    offline.is_offline,
                );
                pump_ready_local_work(
                    &mut scheduler,
                    &mut in_flight,
                    &task_context,
                    &mut deferred_retries,
                    offline.is_offline,
                );
                maybe_start_unmanaged_binding_refresh(
                    &mut unmanaged_binding_refresh_tasks,
                    config.shipper_config.db_path.clone(),
                    config.shipper_config.machine_name.clone(),
                    last_unmanaged_session_bindings_refreshed_at,
                    Instant::now(),
                    "heartbeat",
                );
                let empty_unmanaged_bindings: &[heartbeat::UnmanagedSessionBinding] = &[];
                let unmanaged_binding_override =
                    last_unmanaged_session_bindings.as_deref().unwrap_or(empty_unmanaged_bindings);
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
                    serde_json::to_value(control_channel_status.snapshot()).ok(),
                    Some(unmanaged_binding_override),
                    Some(adaptive_limiter.as_ref()),
                );
                if !offline.is_offline {
                    runtime_truth_bootstrapped = true;
                    if heartbeat_post_tasks.is_empty() {
                        let signature = runtime_truth_signature(&payload);
                        spawn_heartbeat_post(
                            &mut heartbeat_post_tasks,
                            client.clone(),
                            payload,
                            signature,
                            "periodic_heartbeat",
                        );
                    } else {
                        last_runtime_truth_signature = None;
                        tracing::debug!("Skipping periodic heartbeat while a heartbeat POST is still in flight");
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

#[allow(clippy::too_many_arguments)]
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
    control_channel: Option<Value>,
    unmanaged_session_binding_override: Option<&[heartbeat::UnmanagedSessionBinding]>,
    limiter: Option<&crate::scheduler::AdaptiveLimiter>,
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
    payload.adaptive_backlog_limiter = limiter.map(|l| l.snapshot());
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
    let unmanaged_session_bindings =
        if let Some(cached_bindings) = unmanaged_session_binding_override {
            cached_bindings.to_vec()
        } else {
            heartbeat::collect_unmanaged_session_bindings_with_store(
                conn,
                machine_id,
                chrono::Utc::now(),
            )
        };
    payload.unmanaged_session_bindings =
        heartbeat::filter_unmanaged_bindings_owned_by_managed_observations(
            unmanaged_session_bindings,
            observations,
            claude_observations,
        );
    payload.sessions = heartbeat::resolved_sessions_from_observations(
        &payload.managed_sessions,
        &payload.unmanaged_session_bindings,
        observations,
        claude_observations,
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
    heartbeat::write_status_file(
        &payload,
        &stats,
        phase_ledger,
        ledger_status,
        control_channel,
        status_path,
    );
    payload
}

fn record_flight_sample(
    recorder: &FlightRecorder,
    conn: &rusqlite::Connection,
    outbox_dir: &Path,
    control_channel_status: &crate::control_channel::ControlChannelStatus,
    ship_stats: &RecentShipStatsTracker,
    machine_name: &str,
    in_flight_jobs: usize,
    scheduler_pending: bool,
    deferred_retry_count: usize,
    offline: bool,
) {
    recorder.record(json!({
        "schema": "flight_sample.v1",
        "kind": "sample",
        "machine_name": machine_name,
        "outbox": crate::flight::outbox_snapshot(outbox_dir),
        "spool": crate::flight::spool_snapshot(conn),
        "process": crate::flight::process_snapshot(),
        "disk": crate::flight::disk_snapshot(outbox_dir),
        "control_channel": serde_json::to_value(control_channel_status.snapshot()).ok(),
        "ship_stats": crate::flight::ship_stats_snapshot(ship_stats.summary()),
        "runtime": {
            "offline": offline,
            "in_flight_jobs": in_flight_jobs,
            "scheduler_pending": scheduler_pending,
            "deferred_retry_count": deferred_retry_count,
        },
    }));
}

fn runtime_truth_signature(payload: &heartbeat::HeartbeatPayload) -> String {
    let mut sessions: Vec<String> = payload
        .sessions
        .iter()
        .map(|session| {
            let mut join_keys = session.evidence.join_keys.clone();
            join_keys.sort();
            let mut reason_codes = session.reason_codes.clone();
            reason_codes.sort();
            format!(
                "{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
                session.provider,
                session.session_id.as_deref().unwrap_or(""),
                session.provider_session_id.as_deref().unwrap_or(""),
                session.control_path,
                session.presentation_state,
                session.state,
                session.phase.as_deref().unwrap_or(""),
                session.tool_name.as_deref().unwrap_or(""),
                session.workspace.cwd.as_deref().unwrap_or(""),
                session.workspace.label.as_deref().unwrap_or(""),
                session.workspace.branch.as_deref().unwrap_or(""),
                session
                    .process
                    .pid
                    .map(|pid| pid.to_string())
                    .unwrap_or_default(),
                session.process.process_start_time.as_deref().unwrap_or(""),
                session
                    .bridge
                    .bridge_pid
                    .map(|pid| pid.to_string())
                    .unwrap_or_default(),
                session
                    .bridge
                    .app_server_pid
                    .map(|pid| pid.to_string())
                    .unwrap_or_default(),
                session.bridge.status.as_deref().unwrap_or(""),
                session
                    .bridge
                    .thread_subscription_status
                    .as_deref()
                    .unwrap_or(""),
                join_keys.join(","),
                reason_codes.join(",")
            )
        })
        .collect();
    sessions.sort();

    format!("sessions=[{}]", sessions.join(";"))
}

fn local_retry_delay(priority: WorkPriority) -> Duration {
    if priority == WorkPriority::Live {
        LIVE_LOCAL_RETRY_DELAY
    } else {
        Duration::from_secs(LOCAL_RETRY_DELAY_SECS)
    }
}

fn spool_retry_delay_for_path(conn: &rusqlite::Connection, path: &Path) -> Option<Duration> {
    let retry_at = match Spool::new(conn).next_retry_at_for_path(&path.to_string_lossy()) {
        Ok(retry_at) => retry_at?,
        Err(err) => {
            tracing::warn!(
                path = %path.display(),
                "Could not read next spool retry time: {}",
                err
            );
            return None;
        }
    };
    match (retry_at - chrono::Utc::now()).to_std() {
        Ok(delay) => Some(delay),
        Err(_) => Some(Duration::ZERO),
    }
}

fn enqueue_discovered_files(
    scheduler: &mut PathScheduler,
    all_files: Vec<(PathBuf, &'static str)>,
    priority: WorkPriority,
) -> usize {
    let count = all_files.len();
    let source = discovery_observation_source(priority);
    for (path, provider) in all_files {
        scheduler.enqueue_observed(path, provider, priority, source, now_ms());
    }
    count
}

fn discovery_observation_source(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Scan => "reconciliation_scan",
        _ => "discovery_scan",
    }
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

fn maybe_start_reconciliation_scan(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    reason: &'static str,
) {
    if !discovery_tasks.is_empty() {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because discovery is still running"
        );
        return;
    }

    if scheduler.has_pending_work() || !deferred_retries.is_empty() {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan while live local work is pending"
        );
        return;
    }

    tracing::debug!(reason, "Starting reconciliation scan in background");
    start_discovery_task(discovery_tasks, providers, WorkPriority::Scan, reason);
}

fn maybe_start_unmanaged_binding_refresh(
    refresh_tasks: &mut JoinSet<UnmanagedBindingRefreshResult>,
    db_path: Option<PathBuf>,
    machine_id: String,
    last_refreshed_at: Option<Instant>,
    now: Instant,
    reason: &'static str,
) {
    if !refresh_tasks.is_empty() {
        return;
    }
    if last_refreshed_at.is_some_and(|refreshed_at| {
        now.duration_since(refreshed_at) < UNMANAGED_BINDING_REFRESH_INTERVAL
    }) {
        return;
    }

    refresh_tasks.spawn_blocking(move || {
        let started = Instant::now();
        let result = open_db(db_path.as_deref())
            .map_err(|err| err.to_string())
            .map(|conn| {
                heartbeat::collect_unmanaged_session_bindings_with_store(
                    &conn,
                    &machine_id,
                    chrono::Utc::now(),
                )
            });
        UnmanagedBindingRefreshResult {
            reason,
            result,
            elapsed_ms: started.elapsed().as_millis() as u64,
        }
    });
}

fn maybe_start_managed_observation_scan(
    scan_tasks: &mut JoinSet<ManagedObservationScanResult>,
    reason: &'static str,
) {
    if !scan_tasks.is_empty() {
        return;
    }

    scan_tasks.spawn_blocking(move || {
        let started = Instant::now();
        let codex_observations = managed_bridge_scan::collect_observations();
        let claude_observations = managed_claude_scan::collect_observations();
        ManagedObservationScanResult {
            reason,
            codex_observations,
            claude_observations,
            elapsed_ms: started.elapsed().as_millis() as u64,
        }
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
                let Ok(mut signal) = serde_json::from_slice::<TranscriptWakeSignal>(&buf) else {
                    return;
                };
                signal.received_at_ms = Some(now_ms());
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

fn spawn_heartbeat_post(
    tasks: &mut JoinSet<HeartbeatPostResult>,
    client: ShipperClient,
    payload: heartbeat::HeartbeatPayload,
    signature: String,
    reason: &'static str,
) {
    tasks.spawn_local(async move {
        let join_started = Instant::now();
        let heartbeat_task = tokio::spawn(async move {
            let task_started = Instant::now();
            let result = heartbeat::send_heartbeat(&client, &payload)
                .await
                .map_err(|err| err.to_string());
            (result, task_started.elapsed().as_millis() as u64)
        });
        let (result, task_elapsed_ms) = match heartbeat_task.await {
            Ok((result, task_elapsed_ms)) => (result, task_elapsed_ms),
            Err(err) => {
                let elapsed_ms = join_started.elapsed().as_millis() as u64;
                (
                    Err(format!("heartbeat POST worker task failed: {err}")),
                    elapsed_ms,
                )
            }
        };
        HeartbeatPostResult {
            signature,
            reason,
            result,
            join_elapsed_ms: join_started.elapsed().as_millis() as u64,
            task_elapsed_ms,
        }
    });
}

fn reconcile_claude_terminal_signals(
    live_channels: &mut HashMap<String, ClaudeLiveChannelSession>,
    pending_signals: &mut HashMap<String, ClaudeTerminalSignal>,
    machine_name: &str,
    observations: &[managed_claude_scan::ClaudeChannelObservation],
    observed_at: chrono::DateTime<chrono::Utc>,
) {
    prune_stale_claude_terminal_signals(pending_signals, observed_at);
    let mut observed_session_ids = HashSet::new();

    for obs in observations {
        observed_session_ids.insert(obs.session_id.clone());
        if obs.claude_alive {
            live_channels.insert(
                obs.session_id.clone(),
                ClaudeLiveChannelSession {
                    session_id: obs.session_id.clone(),
                    provider_session_id: obs.provider_session_id.clone(),
                    claude_pid: obs.claude_pid,
                },
            );
            continue;
        }

        let previous = live_channels.remove(&obs.session_id);
        let should_close = previous.is_some()
            || obs.claude_pid.is_some()
            || obs.ready
            || obs.bridge_alive
            || obs.bridge_pid.is_some();
        if !should_close {
            continue;
        }
        let provider_session_id = obs
            .provider_session_id
            .clone()
            .or_else(|| {
                previous
                    .as_ref()
                    .and_then(|seen| seen.provider_session_id.clone())
            })
            .unwrap_or_else(|| obs.session_id.clone());
        let pid = obs
            .claude_pid
            .or_else(|| previous.as_ref().and_then(|seen| seen.claude_pid));
        let dedupe_key = claude_terminal_dedupe_key(&obs.session_id, pid, "process_gone");
        pending_signals
            .entry(dedupe_key.clone())
            .or_insert_with(|| ClaudeTerminalSignal {
                dedupe_key: dedupe_key.clone(),
                observed_at,
                event: claude_terminal_event(
                    machine_name,
                    &obs.session_id,
                    &provider_session_id,
                    "process_gone",
                    "process_gone",
                    pid,
                    "process_gone",
                    observed_at,
                    &dedupe_key,
                ),
            });
    }

    let disappeared: Vec<ClaudeLiveChannelSession> = live_channels
        .iter()
        .filter(|(session_id, _)| !observed_session_ids.contains(*session_id))
        .map(|(_, seen)| seen.clone())
        .collect();
    for seen in disappeared {
        live_channels.remove(&seen.session_id);
        let provider_session_id = seen
            .provider_session_id
            .clone()
            .unwrap_or_else(|| seen.session_id.clone());
        let dedupe_key =
            claude_terminal_dedupe_key(&seen.session_id, seen.claude_pid, "channel_state_gone");
        pending_signals
            .entry(dedupe_key.clone())
            .or_insert_with(|| ClaudeTerminalSignal {
                dedupe_key: dedupe_key.clone(),
                observed_at,
                event: claude_terminal_event(
                    machine_name,
                    &seen.session_id,
                    &provider_session_id,
                    "process_gone",
                    "channel_state_gone",
                    seen.claude_pid,
                    "channel_state_gone",
                    observed_at,
                    &dedupe_key,
                ),
            });
    }
}

fn prune_stale_claude_terminal_signals(
    pending_signals: &mut HashMap<String, ClaudeTerminalSignal>,
    now: chrono::DateTime<chrono::Utc>,
) {
    pending_signals.retain(|_, signal| {
        now.signed_duration_since(signal.observed_at).num_seconds()
            <= CLAUDE_TERMINAL_EVENT_STALE_SECS
    });
}

fn claude_terminal_dedupe_key(session_id: &str, pid: Option<u32>, reason: &str) -> String {
    format!(
        "claude-channel-scan:terminal:{session_id}:{}:{reason}",
        pid.map(|value| value.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    )
}

fn claude_terminal_event(
    machine_name: &str,
    session_id: &str,
    provider_session_id: &str,
    terminal_state: &str,
    terminal_reason: &str,
    claude_pid: Option<u32>,
    close_observation: &str,
    observed_at: chrono::DateTime<chrono::Utc>,
    dedupe_key: &str,
) -> Value {
    json!({
        "runtime_key": format!("claude:{provider_session_id}"),
        "session_id": session_id,
        "provider": "claude",
        "device_id": machine_name,
        "source": CLAUDE_TERMINAL_EVENT_SOURCE,
        "kind": "terminal_signal",
        "phase": Value::Null,
        "tool_name": Value::Null,
        "occurred_at": observed_at.to_rfc3339(),
        "dedupe_key": dedupe_key,
        "payload": {
            "terminal_state": terminal_state,
            "terminal_reason": terminal_reason,
            "terminal_source": CLAUDE_TERMINAL_EVENT_SOURCE,
            "provider_session_id": provider_session_id,
            "claude_pid": claude_pid,
            "close_observation": close_observation,
        },
    })
}

fn maybe_spawn_claude_terminal_post(
    tasks: &mut JoinSet<ClaudeTerminalPostResult>,
    client: ShipperClient,
    pending_signals: &HashMap<String, ClaudeTerminalSignal>,
    offline: bool,
) {
    if offline || !tasks.is_empty() || pending_signals.is_empty() {
        return;
    }
    let signals = pending_claude_terminal_batch(pending_signals);
    tasks.spawn_local(async move {
        let join_started = Instant::now();
        let signal_count = signals.len();
        let post_task = tokio::spawn(async move {
            let task_started = Instant::now();
            let result = post_claude_terminal_signals(client, signals).await;
            (result, task_started.elapsed().as_millis() as u64)
        });
        match post_task.await {
            Ok((mut result, task_elapsed_ms)) => {
                result.join_elapsed_ms = join_started.elapsed().as_millis() as u64;
                result.task_elapsed_ms = task_elapsed_ms;
                result
            }
            Err(err) => {
                let elapsed_ms = join_started.elapsed().as_millis() as u64;
                tracing::warn!(
                    signal_count,
                    "Managed Claude terminal signal POST worker task failed: {}",
                    err
                );
                ClaudeTerminalPostResult {
                    dedupe_keys: Vec::new(),
                    result: Err(format!(
                        "managed Claude terminal POST worker task failed: {err}"
                    )),
                    join_elapsed_ms: elapsed_ms,
                    task_elapsed_ms: elapsed_ms,
                }
            }
        }
    });
}

fn pending_claude_terminal_batch(
    pending_signals: &HashMap<String, ClaudeTerminalSignal>,
) -> Vec<ClaudeTerminalSignal> {
    pending_signals
        .values()
        .take(CLAUDE_TERMINAL_EVENT_BATCH_LIMIT)
        .cloned()
        .collect()
}

async fn post_claude_terminal_signals(
    client: ShipperClient,
    signals: Vec<ClaudeTerminalSignal>,
) -> ClaudeTerminalPostResult {
    let dedupe_keys: Vec<String> = signals
        .iter()
        .map(|signal| signal.dedupe_key.clone())
        .collect();
    let events: Vec<Value> = signals.into_iter().map(|signal| signal.event).collect();
    let result = match serde_json::to_vec(&json!({ "events": events })) {
        Ok(body) => client
            .post_json_with_timeout(
                "/api/agents/runtime/events/batch",
                body,
                Some(CLAUDE_TERMINAL_EVENT_TIMEOUT),
            )
            .await
            .map_err(|err| err.to_string()),
        Err(err) => Err(err.to_string()),
    };
    ClaudeTerminalPostResult {
        dedupe_keys,
        result,
        join_elapsed_ms: 0,
        task_elapsed_ms: 0,
    }
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
        scheduler.enqueue_observed(
            PathBuf::from(pending.file_path),
            provider,
            WorkPriority::Retry,
            "spool_pending",
            now_ms(),
        );
        queued += 1;
    }
    Ok(queued)
}

fn provider_name_to_static(provider: &str) -> Option<&'static str> {
    match provider {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "antigravity" => Some("antigravity"),
        "gemini" => Some("gemini"),
        _ => None,
    }
}

fn work_context(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Live => "live_transcript",
        WorkPriority::Retry => "spool_replay",
        WorkPriority::Scan => "reconciliation_scan",
    }
}

fn batch_band_for_priority(priority: WorkPriority) -> shipper::BatchBand {
    match priority {
        WorkPriority::Live => shipper::BatchBand::Live,
        WorkPriority::Scan => shipper::BatchBand::BackgroundRepair,
        WorkPriority::Retry => shipper::BatchBand::Archive,
    }
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn start_ready_jobs(
    scheduler: &mut PathScheduler,
    in_flight: &mut JoinSet<PathTaskResult>,
    task_context: &PathTaskContext,
    live_only: bool,
) {
    let mut next_job = if live_only {
        scheduler.pop_launchable_live()
    } else {
        scheduler.pop_launchable()
    };
    while let Some(job) = next_job {
        let task_context = task_context.clone();
        in_flight.spawn_local(run_path_job(job, task_context));
        next_job = if live_only {
            scheduler.pop_launchable_live()
        } else {
            scheduler.pop_launchable()
        };
    }
}

fn pump_ready_local_work(
    scheduler: &mut PathScheduler,
    in_flight: &mut JoinSet<PathTaskResult>,
    task_context: &PathTaskContext,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
    offline: bool,
) {
    drain_due_local_retries(scheduler, deferred_retries);
    if !offline {
        start_ready_jobs(scheduler, in_flight, task_context, false);
    } else {
        start_ready_jobs(scheduler, in_flight, task_context, true);
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
            scheduler.enqueue_observation(path, retry.provider, retry.priority, retry.observation);
        }
    }
}

#[cfg(test)]
fn ignore_transcript_shipping_for_signals(
    _conn: &rusqlite::Connection,
    signals: Vec<outbox::DrainedPresenceSignal>,
) {
    if !signals.is_empty() {
        tracing::debug!(
            signal_count = signals.len(),
            "Hook outbox signals do not schedule transcript shipping"
        );
    }
}

#[cfg(test)]
fn filter_new_outbox_signals(
    signals: Vec<outbox::DrainedPresenceSignal>,
    seen: &mut HashSet<String>,
) -> Vec<outbox::DrainedPresenceSignal> {
    if seen.len() > 4096 {
        seen.clear();
    }

    signals
        .into_iter()
        .filter(|signal| seen.insert(outbox_signal_mark(signal)))
        .collect()
}

#[cfg(test)]
fn outbox_signal_mark(signal: &outbox::DrainedPresenceSignal) -> String {
    format!(
        "{}|{}|{}|{}|{}",
        signal.provider,
        signal.session_id,
        signal.phase,
        signal.observed_at.timestamp_millis(),
        signal
            .transcript_path
            .as_ref()
            .map(|path| path.to_string_lossy())
            .unwrap_or_default(),
    )
}

fn record_transcript_wake_hint(
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    signal: TranscriptWakeSignal,
) -> Option<(PathBuf, &'static str, ObservationTrace)> {
    let Some(provider) = provider_name_to_static(&signal.provider) else {
        tracing::debug!(
            provider = %signal.provider,
            "Skipping transcript wake for unknown provider"
        );
        return None;
    };
    if !signal.path.exists() {
        tracing::debug!(
            provider = %signal.provider,
            path = %signal.path.display(),
            "Skipping transcript wake for missing path"
        );
        return None;
    }
    if !remember_transcript_wake_observation(
        latest_transcript_wake_observed,
        &signal.path,
        signal.observed_at_ms,
    ) {
        tracing::debug!(
            provider = %signal.provider,
            path = %signal.path.display(),
            phase = %signal.phase,
            wake_reason = signal.wake_reason.as_deref().unwrap_or("unknown"),
            observed_at_ms = signal.observed_at_ms,
            "Skipping stale transcript wake"
        );
        return None;
    }
    let should_ship = signal.wake_reason.as_deref() == Some("turn_completed");
    tracing::debug!(
        provider,
        path = %signal.path.display(),
        phase = %signal.phase,
        wake_reason = signal.wake_reason.as_deref().unwrap_or("unknown"),
        observed_at_ms = signal.observed_at_ms,
        received_at_ms = signal.received_at_ms.unwrap_or(0),
        session_id = signal.session_id.as_deref().unwrap_or("unknown"),
        turn_id = signal.turn_id.as_deref().unwrap_or("unknown"),
        file_len_hint = signal.file_len_hint.unwrap_or(0),
        should_ship,
        "Transcript wake recorded"
    );
    if !should_ship {
        return None;
    }
    Some((
        signal.path,
        provider,
        ObservationTrace {
            source: "wake_socket",
            observed_at_ms: signal.observed_at_ms,
            latest_observed_at_ms: None,
            wake_received_at_ms: signal.received_at_ms,
            enqueued_at_ms: now_ms(),
            session_id: signal.session_id,
            turn_id: signal.turn_id,
            wake_reason: signal.wake_reason,
            file_len_hint: signal.file_len_hint,
        },
    ))
}

fn enqueue_transcript_wake_signal(
    scheduler: &mut PathScheduler,
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    signal: TranscriptWakeSignal,
) -> Option<PathBuf> {
    if let Some((path, provider, observation)) =
        record_transcript_wake_hint(latest_transcript_wake_observed, signal)
    {
        let scheduled_path = path.clone();
        scheduler.enqueue_observation(path, provider, WorkPriority::Live, observation);
        Some(scheduled_path)
    } else {
        None
    }
}

fn should_defer_fsevent_for_managed_wake(
    latest_transcript_wake_observed: &HashMap<PathBuf, i64>,
    managed_codex_transcript_paths: &HashSet<PathBuf>,
    event: &WatcherEvent,
    provider: &str,
) -> bool {
    if provider != "codex" {
        return false;
    }
    if managed_codex_transcript_paths.contains(&event.path) {
        return true;
    }
    let Some(last_wake_observed_at_ms) = latest_transcript_wake_observed.get(&event.path) else {
        return false;
    };
    let suppress_ms = MANAGED_WAKE_FSEVENT_DEFER_WINDOW.as_millis() as i64;
    now_ms().saturating_sub(*last_wake_observed_at_ms) <= suppress_ms
}

fn refresh_managed_codex_transcript_paths(
    managed_codex_transcript_paths: &mut HashSet<PathBuf>,
    observations: &[managed_bridge_scan::CodexBridgeObservation],
) {
    managed_codex_transcript_paths.clear();
    for observation in observations {
        if !(observation.bridge_alive
            || observation.app_server_alive
            || observation.has_tui_attachment)
        {
            continue;
        }
        let Some(path) = observation.thread_path.as_deref() else {
            continue;
        };
        managed_codex_transcript_paths.insert(PathBuf::from(path));
    }
}

fn remember_transcript_wake_observation(
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    path: &Path,
    observed_at_ms: i64,
) -> bool {
    // Bridge wakes can arrive out of order; keep the newest wake per transcript
    // path so a late binding wake cannot resurrect an already-completed turn.
    if let Some(latest_observed_at_ms) = latest_transcript_wake_observed.get(path) {
        if observed_at_ms <= *latest_observed_at_ms {
            return false;
        }
    }

    if !latest_transcript_wake_observed.contains_key(path)
        && latest_transcript_wake_observed.len() >= MAX_TRANSCRIPT_WAKE_TRACKED_PATHS
    {
        if let Some(oldest_path) = latest_transcript_wake_observed
            .iter()
            .min_by_key(|(_, observed)| *observed)
            .map(|(oldest_path, _)| oldest_path.clone())
        {
            latest_transcript_wake_observed.remove(&oldest_path);
        }
    }

    latest_transcript_wake_observed.insert(path.to_path_buf(), observed_at_ms);
    true
}

#[cfg(test)]
fn ignore_transcript_shipping_for_codex_observations(
    observations: &[managed_bridge_scan::CodexBridgeObservation],
) {
    if !observations.is_empty() {
        let transcript_path_hints = observations
            .iter()
            .filter(|observation| observation.thread_path.is_some())
            .count();
        tracing::debug!(
            observation_count = observations.len(),
            transcript_path_hints,
            "Codex bridge observations do not schedule transcript shipping"
        );
    }
}

#[cfg(test)]
fn resolve_transcript_path_for_session(
    conn: &rusqlite::Connection,
    session_id: &str,
    provider: &str,
) -> Option<PathBuf> {
    find_transcript_path(conn, "file_state", session_id, provider)
        .or_else(|| find_transcript_path(conn, "session_binding", session_id, provider))
}

#[cfg(test)]
fn find_transcript_path(
    conn: &rusqlite::Connection,
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

struct PreparedPathJobFile {
    prepared: shipper::PreparedFile,
    trace_timings: shipper::PrepareTraceTimings,
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
) -> Result<Option<PreparedPathJobFile>> {
    let path = job.path.clone();
    let provider = job.provider;
    let work_context_label = work_context(job.priority);
    let algo = task_context.algo;
    let max_batch_bytes = task_context.shipper_config.max_batch_bytes;
    let parse_tracker = task_context.parse_tracker.clone();
    let session_id_hint = job.observation.session_id.clone();
    let db_pool = task_context.db_pool.clone();
    let source_line_mode = if job.priority == WorkPriority::Live && provider == "codex" {
        shipper::SourceLineMode::EventOnly
    } else {
        shipper::SourceLineMode::Full
    };
    // Band is keyed off WorkPriority, NOT SourceLineMode. Live work stays
    // latency-sized; background repair stays tiny so it cannot monopolize the
    // hosted write lane; explicit retry still amortizes the round trip.
    let batch_band = batch_band_for_priority(job.priority);
    let blocking_span = tracing::info_span!(
        "engine.ship.prepare.blocking",
        longhouse.provider = %provider,
        longhouse.work_context = %work_context_label,
    );
    let blocking_queued_at = Instant::now();

    tokio::task::spawn_blocking(move || {
        let _enter = blocking_span.enter();
        let mut trace_timings = shipper::PrepareTraceTimings::default();
        trace_timings.blocking_queue_wait_ms =
            Some(blocking_queued_at.elapsed().as_millis() as u64);
        let open_db_started = Instant::now();
        let conn = db_pool.get()?;
        trace_timings.open_db_ms = Some(open_db_started.elapsed().as_millis() as u64);
        let identity_started = Instant::now();
        let canonical = std::fs::canonicalize(&path)
            .unwrap_or_else(|_| path.clone())
            .to_string_lossy()
            .to_string();

        // Wake-originated managed Codex jobs carry the session identity from
        // the bridge, so they can skip the offset-0 binding race wait.
        let mut session_id_override = session_id_hint;
        let mut binding_wait_ms = 0u64;
        let session_id_source = if session_id_override.is_some() {
            "wake"
        } else {
            let binding = crate::state::session_binding::SessionBinding::new(&conn);
            session_id_override = binding.get(&canonical)?;
            if session_id_override.is_none() {
                let file_state = crate::state::file_state::FileState::new(&conn);
                let current_offset = file_state
                    .get_offset(&canonical)
                    .or_else(|_| file_state.get_offset(&path.to_string_lossy()))?;
                if current_offset == 0 {
                    let binding_wait_started = Instant::now();
                    std::thread::sleep(std::time::Duration::from_millis(300));
                    binding_wait_ms = binding_wait_started.elapsed().as_millis() as u64;
                    session_id_override = binding.get(&canonical)?;
                }
            }
            if session_id_override.is_some() {
                "binding"
            } else {
                "parsed"
            }
        };
        trace_timings.identity_ms = Some(identity_started.elapsed().as_millis() as u64);
        tracing::debug!(
            path = %path.display(),
            provider,
            session_id_source,
            binding_wait_ms,
            "Prepared session identity for ship job"
        );
        trace_timings.binding_wait_ms = Some(binding_wait_ms);

        let prepared = shipper::prepare_file_batches_with_source_line_mode_parse_tracker_and_trace(
            &path,
            provider,
            algo,
            &conn,
            max_batch_bytes,
            session_id_override.as_deref(),
            Some(&parse_tracker),
            source_line_mode,
            batch_band,
            Some(&mut trace_timings),
        )?;
        Ok(prepared.map(|prepared| PreparedPathJobFile {
            prepared,
            trace_timings,
        }))
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
    let task_started = Instant::now();
    let job_started_at_ms = chrono::Utc::now().timestamp_millis();
    let mut result = PathTaskResult {
        job,
        events_shipped: 0,
        resolved_spool: 0,
        failed_spool: 0,
        had_connect_error: false,
        rerun_priority: None,
        local_retry_after: None,
        local_retry_priority: None,
        processing_elapsed: Duration::ZERO,
    };

    let conn = match task_context.db_pool.get() {
        Ok(conn) => conn,
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!(
                    "Error opening shipper DB for {}: {}",
                    result.job.path.display(),
                    e
                );
            }
            result.local_retry_after = Some(local_retry_delay(result.job.priority));
            return finish_path_task(result, task_started);
        }
    };

    if result.job.priority != WorkPriority::Live {
        let replay_prepare_at_ms = chrono::Utc::now().timestamp_millis();
        let replay_trace = shipper::ShipTraceContext {
            work_context: "spool_replay",
            observation_source: result.job.observation.source,
            observed_at_ms: result.job.observation.observed_at_ms,
            latest_observed_at_ms: result.job.observation.latest_observed_at_ms,
            wake_received_at_ms: result.job.observation.wake_received_at_ms,
            enqueued_at_ms: result.job.observation.enqueued_at_ms,
            job_started_at_ms,
            prepare_started_at_ms: replay_prepare_at_ms,
            prepare_finished_at_ms: replay_prepare_at_ms,
            prepare_blocking_queue_wait_ms: None,
            prepare_open_db_ms: None,
            prepare_identity_ms: None,
            prepare_cursor_ms: None,
            prepare_binding_wait_ms: None,
            prepare_parse_ms: None,
            prepare_batch_build_ms: None,
            session_id_hint: result.job.observation.session_id.clone(),
            turn_id: result.job.observation.turn_id.clone(),
            wake_reason: result.job.observation.wake_reason.clone(),
            file_len_hint: result.job.observation.file_len_hint,
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
            task_context.flight_recorder.as_ref(),
            Some(&replay_trace),
            Some(task_context.limiter.as_ref()),
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
                result.local_retry_after = Some(local_retry_delay(result.job.priority));
                return finish_path_task(result, task_started);
            }
        }

        if result.had_connect_error {
            return finish_path_task(result, task_started);
        }

        let ready_spool_remaining = Spool::new(&conn)
            .pending_entries_for_path(&result.job.path.to_string_lossy(), 1)
            .map(|entries| !entries.is_empty())
            .unwrap_or(false);
        if ready_spool_remaining {
            result.rerun_priority = Some(WorkPriority::Retry);
        } else if result.failed_spool > 0 {
            result.local_retry_after = spool_retry_delay_for_path(&conn, &result.job.path);
            result.local_retry_priority = Some(WorkPriority::Retry);
        }
    }

    let file_start = Instant::now();
    let prepare_started_at_ms = chrono::Utc::now().timestamp_millis();
    match prepare_file_for_job(&result.job, &task_context).await {
        Ok(Some(prepared_for_job)) => {
            let PreparedPathJobFile {
                prepared,
                trace_timings,
            } = prepared_for_job;
            let prepare_finished_at_ms = chrono::Utc::now().timestamp_millis();
            let event_count = prepared.total_event_count();
            let byte_count = prepared.new_offset.saturating_sub(prepared.offset);
            let prepared_offset = prepared.offset;
            let prepared_new_offset = prepared.new_offset;
            let ship_trace = shipper::ShipTraceContext {
                work_context: work_context(result.job.priority),
                observation_source: result.job.observation.source,
                observed_at_ms: result.job.observation.observed_at_ms,
                latest_observed_at_ms: result.job.observation.latest_observed_at_ms,
                wake_received_at_ms: result.job.observation.wake_received_at_ms,
                enqueued_at_ms: result.job.observation.enqueued_at_ms,
                job_started_at_ms,
                prepare_started_at_ms,
                prepare_finished_at_ms,
                prepare_blocking_queue_wait_ms: trace_timings.blocking_queue_wait_ms,
                prepare_open_db_ms: trace_timings.open_db_ms,
                prepare_identity_ms: trace_timings.identity_ms,
                prepare_cursor_ms: trace_timings.cursor_ms,
                prepare_binding_wait_ms: trace_timings.binding_wait_ms,
                prepare_parse_ms: trace_timings.parse_ms,
                prepare_batch_build_ms: trace_timings.batch_build_ms,
                session_id_hint: result.job.observation.session_id.clone(),
                turn_id: result.job.observation.turn_id.clone(),
                wake_reason: result.job.observation.wake_reason.clone(),
                file_len_hint: result.job.observation.file_len_hint,
            };
            match shipper::ship_prepared_file_with_trace(
                prepared,
                &task_context.client,
                &conn,
                Some(&task_context.tracker),
                Some(&task_context.ship_stats),
                Some(&ship_trace),
                task_context.flight_recorder.as_ref(),
                Some(task_context.limiter.as_ref()),
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
                    if !outcome.fully_processed && result.job.priority == WorkPriority::Live {
                        result.local_retry_after = Some(LIVE_LOCAL_RETRY_DELAY);
                        result.local_retry_priority = Some(WorkPriority::Retry);
                    }
                }
                Err(e) => {
                    if task_context.tracker.record_error() {
                        tracing::warn!("Error shipping {}: {}", result.job.path.display(), e);
                    }
                    result.local_retry_after = Some(local_retry_delay(result.job.priority));
                }
            }
        }
        Ok(None) => {}
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!("Error preparing {}: {}", result.job.path.display(), e);
            }
            result.local_retry_after = Some(local_retry_delay(result.job.priority));
        }
    }

    finish_path_task(result, task_started)
}

fn finish_path_task(mut result: PathTaskResult, started: Instant) -> PathTaskResult {
    result.processing_elapsed = started.elapsed();
    result
}

/// Wait for SIGINT (Ctrl-C) or SIGTERM.
async fn shutdown_signal() {
    use tokio::signal::unix::{SignalKind, signal};

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
            ship_attempts_10m: 0,
            ship_successes_10m: 0,
            ship_rate_limited_10m: 0,
            ship_server_errors_10m: 0,
            ship_retryable_client_errors_10m: 0,
            ship_connect_errors_10m: 0,
            disk_free_bytes: 0,
            is_offline: false,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            sessions: Vec::new(),
            adaptive_backlog_limiter: None,
        }
    }

    fn unmanaged_binding(session_id: &str, pid: u32) -> heartbeat::UnmanagedSessionBinding {
        heartbeat::UnmanagedSessionBinding {
            machine_id: "cinder".to_string(),
            provider: "claude".to_string(),
            provider_session_id: session_id.to_string(),
            source_path: Some(format!("/tmp/{session_id}.jsonl")),
            source_inode: None,
            source_device: None,
            pid: Some(pid),
            process_start_time: Some("2026-05-05T12:00:00Z".to_string()),
            cwd: Some("/tmp/project".to_string()),
            source_offset: None,
            source_mtime: None,
            observed_at: "2026-05-05T12:00:02Z".to_string(),
        }
    }

    fn resolved_session(
        provider: &str,
        session_id: Option<&str>,
        provider_session_id: Option<&str>,
        control_path: &str,
        state: &str,
        pid: Option<u32>,
    ) -> heartbeat::ResolvedLocalSession {
        heartbeat::ResolvedLocalSession {
            session_id: session_id.map(str::to_string),
            provider: provider.to_string(),
            provider_session_id: provider_session_id.map(str::to_string),
            control_path: control_path.to_string(),
            presentation_state: if control_path == "managed" {
                format!("managed_{state}")
            } else {
                control_path.to_string()
            },
            state: state.to_string(),
            phase: Some("idle".to_string()),
            tool_name: None,
            phase_observed_at: Some("2026-05-05T12:00:01Z".to_string()),
            last_activity_at: Some("2026-05-05T12:00:02Z".to_string()),
            workspace: heartbeat::ResolvedWorkspace {
                cwd: Some("/tmp/project".to_string()),
                label: Some("project".to_string()),
                branch: None,
            },
            process: heartbeat::ResolvedProcess {
                pid,
                process_start_time: Some("2026-05-05T12:00:00Z".to_string()),
                started_at: Some("2026-05-05T12:00:00Z".to_string()),
            },
            bridge: heartbeat::ResolvedBridge::default(),
            evidence: heartbeat::ResolvedEvidence {
                process_observed: pid.is_some(),
                transcript_observed: provider_session_id.is_some(),
                bridge_state: None,
                hook_seen_at: Some("2026-05-05T12:00:02Z".to_string()),
                join_keys: provider_session_id
                    .map(|id| vec![format!("provider_session_id={id}")])
                    .unwrap_or_default(),
            },
            reason_codes: Vec::new(),
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
            launch_mode: Some("tui".to_string()),
            ws_url: Some("ws://127.0.0.1:1111".to_string()),
            status: "ready".to_string(),
            thread_id: Some("thread-live".to_string()),
            thread_path: Some(transcript_path.display().to_string()),
            active_turn_id: active_turn_id.map(str::to_string),
            last_turn_status: last_turn_status.map(str::to_string),
            last_error: None,
            thread_subscription_status: Some("subscribed".to_string()),
            bridge_pid: 12344,
            app_server_pid: None,
            app_server_pgid: None,
            updated_at: updated_at.to_string(),
            bridge_alive,
            has_tui_attachment: false,
            app_server_alive: false,
        }
    }

    #[test]
    fn test_write_local_status_snapshot_uses_cached_unmanaged_bindings() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let status = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let tracker = ConsecutiveErrorTracker::new();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();
        let cached = vec![unmanaged_binding("sess-cached", 42)];

        let payload = write_local_status_snapshot(
            &conn,
            &tracker,
            &parse_tracker,
            &ship_stats,
            false,
            &None,
            "cinder",
            status.path(),
            &[],
            &[],
            None,
            Some(&cached),
            None,
        );

        assert_eq!(payload.unmanaged_session_bindings, cached);
    }

    #[test]
    fn test_runtime_truth_signature_ignores_observation_timestamps() {
        let mut first = empty_heartbeat_payload();
        first.sessions.push(resolved_session(
            "claude",
            None,
            Some("sess-1"),
            "unmanaged",
            "unmanaged",
            Some(42),
        ));
        let mut second = first.clone();
        second.sessions[0].phase_observed_at = Some("2026-05-05T12:00:10Z".to_string());
        second.sessions[0].last_activity_at = Some("2026-05-05T12:00:11Z".to_string());
        second.sessions[0].evidence.hook_seen_at = Some("2026-05-05T12:00:11Z".to_string());

        assert_eq!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_runtime_truth_signature_changes_when_process_identity_changes() {
        let mut first = empty_heartbeat_payload();
        first.sessions.push(resolved_session(
            "claude",
            None,
            Some("sess-1"),
            "unmanaged",
            "unmanaged",
            Some(42),
        ));
        let mut second = first.clone();
        second.sessions[0].process.pid = Some(43);

        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_runtime_truth_signature_changes_on_managed_lease_state() {
        let mut first = empty_heartbeat_payload();
        first.sessions.push(resolved_session(
            "codex",
            Some("managed-session"),
            Some("thread-1"),
            "managed",
            "attached",
            Some(42),
        ));
        let mut second = first.clone();
        second.sessions[0].state = "detached".to_string();
        second.sessions[0].presentation_state = "managed_detached".to_string();

        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_claude_terminal_signal_generated_when_seen_process_dies() {
        let mut live = HashMap::new();
        let mut pending = HashMap::new();
        let observed_at = chrono::DateTime::parse_from_rfc3339("2026-05-12T20:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let live_obs = managed_claude_scan::ClaudeChannelObservation {
            session_id: "session-123".to_string(),
            provider_session_id: Some("provider-123".to_string()),
            state_file: PathBuf::from("/tmp/session-123.json"),
            claude_pid: Some(123),
            bridge_pid: Some(456),
            ready: true,
            updated_at: "2026-05-12T19:59:59Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
        };
        let dead_obs = managed_claude_scan::ClaudeChannelObservation {
            claude_alive: false,
            bridge_alive: false,
            updated_at: "2026-05-12T20:00:00Z".to_string(),
            ..live_obs.clone()
        };

        reconcile_claude_terminal_signals(
            &mut live,
            &mut pending,
            "cinder",
            &[live_obs],
            observed_at,
        );
        assert!(pending.is_empty());

        reconcile_claude_terminal_signals(
            &mut live,
            &mut pending,
            "cinder",
            &[dead_obs],
            observed_at,
        );

        assert_eq!(pending.len(), 1);
        let signal = pending.values().next().unwrap();
        assert_eq!(signal.event["runtime_key"], "claude:provider-123");
        assert_eq!(signal.event["source"], CLAUDE_TERMINAL_EVENT_SOURCE);
        assert_eq!(signal.event["kind"], "terminal_signal");
        assert_eq!(signal.event["payload"]["terminal_state"], "process_gone");
        assert_eq!(signal.event["payload"]["terminal_reason"], "process_gone");
        assert_eq!(
            signal.event["payload"]["terminal_source"],
            CLAUDE_TERMINAL_EVENT_SOURCE
        );
        assert!(live.is_empty());
    }

    #[test]
    fn test_claude_terminal_signal_generated_when_channel_state_disappears() {
        let mut live = HashMap::new();
        let mut pending = HashMap::new();
        let observed_at = chrono::DateTime::parse_from_rfc3339("2026-05-12T20:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let live_obs = managed_claude_scan::ClaudeChannelObservation {
            session_id: "session-123".to_string(),
            provider_session_id: Some("provider-123".to_string()),
            state_file: PathBuf::from("/tmp/session-123.json"),
            claude_pid: Some(123),
            bridge_pid: Some(456),
            ready: true,
            updated_at: "2026-05-12T19:59:59Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
        };

        reconcile_claude_terminal_signals(
            &mut live,
            &mut pending,
            "cinder",
            &[live_obs],
            observed_at,
        );
        reconcile_claude_terminal_signals(&mut live, &mut pending, "cinder", &[], observed_at);

        assert_eq!(pending.len(), 1);
        let signal = pending.values().next().unwrap();
        assert_eq!(
            signal.event["payload"]["close_observation"],
            "channel_state_gone"
        );
        assert_eq!(signal.event["payload"]["terminal_state"], "process_gone");
        assert!(live.is_empty());
    }

    #[test]
    fn test_claude_terminal_signal_generated_from_dead_channel_file_without_live_cache() {
        let mut live = HashMap::new();
        let mut pending = HashMap::new();
        let observed_at = chrono::DateTime::parse_from_rfc3339("2026-05-12T20:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let dead_obs = managed_claude_scan::ClaudeChannelObservation {
            session_id: "session-123".to_string(),
            provider_session_id: Some("provider-123".to_string()),
            state_file: PathBuf::from("/tmp/session-123.json"),
            claude_pid: Some(123),
            bridge_pid: Some(456),
            ready: true,
            updated_at: "2026-05-12T20:00:00Z".to_string(),
            claude_alive: false,
            bridge_alive: false,
        };

        reconcile_claude_terminal_signals(
            &mut live,
            &mut pending,
            "cinder",
            &[dead_obs],
            observed_at,
        );

        assert_eq!(pending.len(), 1);
        let signal = pending.values().next().unwrap();
        assert_eq!(
            signal.dedupe_key,
            "claude-channel-scan:terminal:session-123:123:process_gone"
        );
        assert_eq!(signal.event["payload"]["terminal_reason"], "process_gone");
    }

    #[test]
    fn test_claude_terminal_signals_are_pruned_and_batched() {
        let now = chrono::DateTime::parse_from_rfc3339("2026-05-12T20:00:00Z")
            .unwrap()
            .with_timezone(&chrono::Utc);
        let fresh_at = now - chrono::Duration::seconds(30);
        let stale_at = now - chrono::Duration::seconds(CLAUDE_TERMINAL_EVENT_STALE_SECS + 1);
        let mut pending = HashMap::new();

        pending.insert(
            "stale".to_string(),
            ClaudeTerminalSignal {
                dedupe_key: "stale".to_string(),
                observed_at: stale_at,
                event: json!({"dedupe_key": "stale"}),
            },
        );
        for index in 0..(CLAUDE_TERMINAL_EVENT_BATCH_LIMIT + 5) {
            let key = format!("fresh-{index}");
            pending.insert(
                key.clone(),
                ClaudeTerminalSignal {
                    dedupe_key: key,
                    observed_at: fresh_at,
                    event: json!({"index": index}),
                },
            );
        }

        prune_stale_claude_terminal_signals(&mut pending, now);

        assert!(!pending.contains_key("stale"));
        assert_eq!(pending.len(), CLAUDE_TERMINAL_EVENT_BATCH_LIMIT + 5);
        assert_eq!(
            pending_claude_terminal_batch(&pending).len(),
            CLAUDE_TERMINAL_EVENT_BATCH_LIMIT
        );
    }

    #[test]
    fn test_codex_bridge_observation_without_completed_turn_does_not_schedule_transcript_shipping()
    {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let observation =
            codex_bridge_observation(transcript.path(), None, None, "2026-05-01T00:00:00Z", true);

        ignore_transcript_shipping_for_codex_observations(&[observation]);
    }

    #[test]
    fn test_codex_bridge_completed_turn_observation_stays_runtime_only() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let observation = codex_bridge_observation(
            transcript.path(),
            None,
            Some("completed"),
            "2026-05-01T00:00:00Z",
            true,
        );

        ignore_transcript_shipping_for_codex_observations(&[observation]);

        let repeated = codex_bridge_observation(
            transcript.path(),
            None,
            Some("completed"),
            "2026-05-01T00:00:00Z",
            true,
        );
        ignore_transcript_shipping_for_codex_observations(&[repeated]);
    }

    #[test]
    fn test_codex_bridge_observation_ignores_dead_bridge() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let observation =
            codex_bridge_observation(transcript.path(), None, None, "2026-05-01T00:00:00Z", false);

        ignore_transcript_shipping_for_codex_observations(&[observation]);
    }

    #[test]
    fn test_outbox_signal_filter_dedupes_kept_presence_file() {
        let observed_at = chrono::Utc::now();
        let transcript_path = PathBuf::from("/tmp/transcript.jsonl");
        let signal = outbox::DrainedPresenceSignal {
            session_id: "sess-outbox".to_string(),
            provider: "codex".to_string(),
            phase: "idle".to_string(),
            observed_at,
            transcript_path: Some(transcript_path.clone()),
        };
        let mut seen = HashSet::new();

        let first = filter_new_outbox_signals(vec![signal.clone()], &mut seen);
        let second = filter_new_outbox_signals(vec![signal.clone()], &mut seen);
        let mut changed_phase = signal;
        changed_phase.phase = "thinking".to_string();
        let third = filter_new_outbox_signals(vec![changed_phase], &mut seen);

        assert_eq!(first.len(), 1);
        assert!(second.is_empty());
        assert_eq!(third.len(), 1);
        assert_eq!(first[0].transcript_path.as_ref(), Some(&transcript_path));
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
    fn test_presence_signal_does_not_schedule_bound_terminal_transcript() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();

        FileState::new(&conn)
            .set_offset(&path, 100, "sess-signal", "sess-signal", "claude")
            .unwrap();

        ignore_transcript_shipping_for_signals(
            &conn,
            vec![outbox::DrainedPresenceSignal {
                session_id: "sess-signal".to_string(),
                provider: "claude".to_string(),
                phase: "idle".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: None,
            }],
        );
    }

    #[test]
    fn test_presence_signal_with_hook_path_does_not_schedule_transcript_shipping() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();

        ignore_transcript_shipping_for_signals(
            &conn,
            vec![outbox::DrainedPresenceSignal {
                session_id: "sess-hook-path".to_string(),
                provider: "codex".to_string(),
                phase: "thinking".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: Some(transcript.path().to_path_buf()),
            }],
        );
    }

    #[test]
    fn test_managed_presence_signal_does_not_arm_transcript_shipping() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(transcript.path()).unwrap();
        let managed_session_id = "22222222-2222-4222-8222-222222222222";
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), managed_session_id, "codex")
            .unwrap();

        ignore_transcript_shipping_for_signals(
            &conn,
            vec![outbox::DrainedPresenceSignal {
                session_id: managed_session_id.to_string(),
                provider: "codex".to_string(),
                phase: "thinking".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: Some(transcript.path().to_path_buf()),
            }],
        );
    }

    #[test]
    fn test_turn_started_transcript_wake_records_hint_without_shipping() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();

        let scheduled = record_transcript_wake_hint(
            &mut latest_wakes,
            TranscriptWakeSignal {
                provider: "codex".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "running".to_string(),
                observed_at_ms: 123,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("turn_started".to_string()),
                file_len_hint: Some(456),
                received_at_ms: Some(124),
            },
        );

        assert_eq!(latest_wakes.get(transcript.path()), Some(&123));
        assert!(scheduled.is_none());
    }

    #[test]
    fn test_progress_wake_records_hint_without_shipping() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();

        let scheduled = record_transcript_wake_hint(
            &mut latest_wakes,
            TranscriptWakeSignal {
                provider: "codex".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "running".to_string(),
                observed_at_ms: 123,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("progress".to_string()),
                file_len_hint: Some(456),
                received_at_ms: Some(124),
            },
        );

        assert_eq!(latest_wakes.get(transcript.path()), Some(&123));
        assert!(scheduled.is_none());
    }

    #[test]
    fn test_turn_completed_transcript_wake_schedules_live_archive_shipping() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();

        let scheduled = record_transcript_wake_hint(
            &mut latest_wakes,
            TranscriptWakeSignal {
                provider: "codex".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "idle".to_string(),
                observed_at_ms: 123,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("turn_completed".to_string()),
                file_len_hint: Some(456),
                received_at_ms: Some(124),
            },
        )
        .expect("completed turn wakes should schedule archive shipping");

        assert_eq!(latest_wakes.get(transcript.path()), Some(&123));
        assert_eq!(scheduled.0, transcript.path());
        assert_eq!(scheduled.1, "codex");
        assert_eq!(scheduled.2.source, "wake_socket");
        assert_eq!(scheduled.2.observed_at_ms, 123);
        assert_eq!(scheduled.2.wake_received_at_ms, Some(124));
        assert_eq!(scheduled.2.session_id.as_deref(), Some("session-123"));
        assert_eq!(scheduled.2.turn_id.as_deref(), Some("turn-123"));
        assert_eq!(scheduled.2.wake_reason.as_deref(), Some("turn_completed"));
        assert_eq!(scheduled.2.file_len_hint, Some(456));
    }

    #[test]
    fn test_enqueue_transcript_wake_signal_queues_live_work() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();
        let mut scheduler = PathScheduler::new(4);

        assert!(
            enqueue_transcript_wake_signal(
                &mut scheduler,
                &mut latest_wakes,
                TranscriptWakeSignal {
                    provider: "codex".to_string(),
                    path: transcript.path().to_path_buf(),
                    phase: "idle".to_string(),
                    observed_at_ms: 123,
                    session_id: Some("session-123".to_string()),
                    turn_id: Some("turn-123".to_string()),
                    wake_reason: Some("turn_completed".to_string()),
                    file_len_hint: Some(456),
                    received_at_ms: Some(124),
                },
            )
            .is_some()
        );

        let launched = scheduler.pop_launchable().unwrap();
        assert_eq!(launched.path, transcript.path());
        assert_eq!(launched.provider, "codex");
        assert_eq!(launched.priority, WorkPriority::Live);
        assert_eq!(launched.observation.source, "wake_socket");
        assert_eq!(
            launched.observation.wake_reason.as_deref(),
            Some("turn_completed")
        );
    }

    #[test]
    fn test_recent_managed_wake_defers_codex_fsevent_shipping() {
        let path = PathBuf::from("/tmp/managed-codex.jsonl");
        let now = now_ms();
        let event = WatcherEvent {
            path: path.clone(),
            observed_at_ms: now,
            latest_observed_at_ms: now,
        };
        let latest_wakes = HashMap::from([(path, now)]);

        assert!(should_defer_fsevent_for_managed_wake(
            &latest_wakes,
            &HashSet::new(),
            &event,
            "codex"
        ));
        assert!(!should_defer_fsevent_for_managed_wake(
            &latest_wakes,
            &HashSet::new(),
            &event,
            "claude"
        ));
    }

    #[test]
    fn test_old_managed_wake_does_not_suppress_codex_fsevent_shipping() {
        let path = PathBuf::from("/tmp/managed-codex.jsonl");
        let now = now_ms();
        let old = now - MANAGED_WAKE_FSEVENT_DEFER_WINDOW.as_millis() as i64 - 1;
        let event = WatcherEvent {
            path: path.clone(),
            observed_at_ms: now,
            latest_observed_at_ms: now,
        };
        let latest_wakes = HashMap::from([(path, old)]);

        assert!(!should_defer_fsevent_for_managed_wake(
            &latest_wakes,
            &HashSet::new(),
            &event,
            "codex"
        ));
    }

    #[test]
    fn test_managed_codex_path_defers_fsevent_before_first_wake() {
        let path = PathBuf::from("/tmp/managed-codex.jsonl");
        let now = now_ms();
        let event = WatcherEvent {
            path: path.clone(),
            observed_at_ms: now,
            latest_observed_at_ms: now,
        };
        let managed_paths = HashSet::from([path]);

        assert!(should_defer_fsevent_for_managed_wake(
            &HashMap::new(),
            &managed_paths,
            &event,
            "codex"
        ));
    }

    #[test]
    fn test_refresh_managed_codex_transcript_paths_keeps_live_bridges() {
        let path = PathBuf::from("/tmp/managed-live.jsonl");
        let inactive_path = PathBuf::from("/tmp/managed-dead.jsonl");
        let observations = vec![
            codex_bridge_observation(&path, None, None, "2026-05-05T12:00:02Z", true),
            managed_bridge_scan::CodexBridgeObservation {
                bridge_alive: false,
                app_server_alive: false,
                has_tui_attachment: false,
                thread_path: Some(inactive_path.display().to_string()),
                ..codex_bridge_observation(&inactive_path, None, None, "2026-05-05T12:00:02Z", true)
            },
        ];
        let mut managed_paths = HashSet::new();

        refresh_managed_codex_transcript_paths(&mut managed_paths, &observations);

        assert!(managed_paths.contains(&path));
        assert!(!managed_paths.contains(&inactive_path));
    }

    #[test]
    fn test_stale_active_wake_does_not_replace_newer_wake_hint() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();

        let scheduled = record_transcript_wake_hint(
            &mut latest_wakes,
            TranscriptWakeSignal {
                provider: "codex".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "idle".to_string(),
                observed_at_ms: 200,
                session_id: Some("session-123".to_string()),
                turn_id: Some("turn-123".to_string()),
                wake_reason: Some("turn_completed".to_string()),
                file_len_hint: Some(456),
                received_at_ms: Some(201),
            },
        );
        assert!(scheduled.is_some());

        let stale_scheduled = record_transcript_wake_hint(
            &mut latest_wakes,
            TranscriptWakeSignal {
                provider: "codex".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "running".to_string(),
                observed_at_ms: 100,
                session_id: Some("session-123".to_string()),
                turn_id: None,
                wake_reason: Some("binding".to_string()),
                file_len_hint: Some(123),
                received_at_ms: Some(250),
            },
        );

        assert_eq!(latest_wakes.get(transcript.path()), Some(&200));
        assert!(stale_scheduled.is_none());
    }

    #[test]
    fn test_transcript_wake_observation_tracker_is_bounded() {
        let mut latest_wakes = HashMap::new();
        for i in 0..MAX_TRANSCRIPT_WAKE_TRACKED_PATHS {
            latest_wakes.insert(PathBuf::from(format!("/tmp/old-{i}.jsonl")), i as i64);
        }

        assert!(remember_transcript_wake_observation(
            &mut latest_wakes,
            Path::new("/tmp/new.jsonl"),
            MAX_TRANSCRIPT_WAKE_TRACKED_PATHS as i64,
        ));

        assert_eq!(latest_wakes.len(), MAX_TRANSCRIPT_WAKE_TRACKED_PATHS);
        assert!(!latest_wakes.contains_key(Path::new("/tmp/old-0.jsonl")));
        assert_eq!(
            latest_wakes.get(Path::new("/tmp/new.jsonl")),
            Some(&(MAX_TRANSCRIPT_WAKE_TRACKED_PATHS as i64))
        );
    }

    #[test]
    fn test_transcript_wake_observation_tracker_rejects_older_value() {
        let mut latest_wakes = HashMap::from([(PathBuf::from("/tmp/session.jsonl"), 200)]);

        assert!(!remember_transcript_wake_observation(
            &mut latest_wakes,
            Path::new("/tmp/session.jsonl"),
            100,
        ));

        assert_eq!(
            latest_wakes.get(Path::new("/tmp/session.jsonl")),
            Some(&200)
        );
    }

    #[test]
    fn test_unknown_provider_signal_does_not_schedule_transcript_shipping() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();

        FileState::new(&conn)
            .set_offset(&path, 100, "sess-unknown", "sess-unknown", "claude")
            .unwrap();

        ignore_transcript_shipping_for_signals(
            &conn,
            vec![outbox::DrainedPresenceSignal {
                session_id: "sess-unknown".to_string(),
                provider: "unknown-provider".to_string(),
                phase: "idle".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: None,
            }],
        );
    }

    #[test]
    fn test_presence_signal_does_not_ship_bound_transcript_tail() {
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

        ignore_transcript_shipping_for_signals(
            &conn,
            vec![outbox::DrainedPresenceSignal {
                session_id: managed_session_id.to_string(),
                provider: "claude".to_string(),
                phase: "needs_user".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: None,
            }],
        );
        assert_eq!(FileState::new(&conn).get_offset(&canonical).unwrap_or(0), 0);
        assert_eq!(
            crate::state::session_binding::SessionBinding::new(&conn)
                .get(&canonical)
                .unwrap(),
            Some(managed_session_id.to_string())
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
                    priority: WorkPriority::Live,
                    observation: ObservationTrace {
                        source: "wake_socket",
                        observed_at_ms: 100,
                        latest_observed_at_ms: None,
                        wake_received_at_ms: Some(101),
                        enqueued_at_ms: 0,
                        session_id: Some("session-live".to_string()),
                        turn_id: Some("turn-live".to_string()),
                        wake_reason: Some("progress".to_string()),
                        file_len_hint: Some(42),
                    },
                },
            ),
            (
                PathBuf::from("/tmp/retry-later.jsonl"),
                DeferredRetry {
                    due_at: now + Duration::from_secs(60),
                    provider: "claude",
                    priority: WorkPriority::Retry,
                    observation: ObservationTrace {
                        source: "local_retry",
                        observed_at_ms: 200,
                        latest_observed_at_ms: None,
                        wake_received_at_ms: None,
                        enqueued_at_ms: 0,
                        session_id: None,
                        turn_id: None,
                        wake_reason: None,
                        file_len_hint: None,
                    },
                },
            ),
        ]);

        drain_due_local_retries(&mut scheduler, &mut deferred_retries);

        let launched = scheduler.pop_launchable().unwrap();
        assert_eq!(launched.path, PathBuf::from("/tmp/retry-now.jsonl"));
        assert_eq!(launched.priority, WorkPriority::Live);
        assert_eq!(launched.observation.source, "wake_socket");
        assert_eq!(
            launched.observation.session_id.as_deref(),
            Some("session-live")
        );
        assert_eq!(
            launched.observation.wake_reason.as_deref(),
            Some("progress")
        );
        assert_eq!(deferred_retries.len(), 1);
        assert!(deferred_retries.contains_key(&PathBuf::from("/tmp/retry-later.jsonl")));
    }

    #[test]
    fn test_reconciliation_discovery_queues_reconciliation_source() {
        let mut scheduler = PathScheduler::new(4);
        let path = PathBuf::from("/tmp/reconciliation-session.jsonl");

        let queued = enqueue_discovered_files(
            &mut scheduler,
            vec![(path.clone(), "claude")],
            WorkPriority::Scan,
        );

        assert_eq!(queued, 1);
        let job = scheduler.pop_launchable().expect("scan job queued");
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Scan);
        assert_eq!(job.observation.source, "reconciliation_scan");
        assert_eq!(work_context(job.priority), "reconciliation_scan");
        assert_eq!(
            batch_band_for_priority(job.priority),
            shipper::BatchBand::BackgroundRepair
        );
    }

    #[test]
    fn test_batch_band_for_priority_keeps_retry_archive_sized() {
        assert_eq!(
            batch_band_for_priority(WorkPriority::Live),
            shipper::BatchBand::Live
        );
        assert_eq!(
            batch_band_for_priority(WorkPriority::Retry),
            shipper::BatchBand::Archive
        );
        assert_eq!(
            batch_band_for_priority(WorkPriority::Scan),
            shipper::BatchBand::BackgroundRepair
        );
    }

    #[test]
    fn test_live_retry_delay_is_shorter_than_background_retry() {
        assert_eq!(
            local_retry_delay(WorkPriority::Live),
            LIVE_LOCAL_RETRY_DELAY
        );
        assert_eq!(
            local_retry_delay(WorkPriority::Scan),
            Duration::from_secs(LOCAL_RETRY_DELAY_SECS)
        );
    }
}
