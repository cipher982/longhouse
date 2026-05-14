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
use rusqlite::Connection;
use serde::Deserialize;
use serde_json::{json, Value};
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
    pub flight_recorder_dir: Option<PathBuf>,
}

const DAEMON_MAX_IN_FLIGHT_CAP: usize = 4;
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
const ACTIVE_TRANSCRIPT_POLL_INTERVAL: Duration = Duration::from_millis(250);
const ACTIVE_TRANSCRIPT_POLL_SLOW_THRESHOLD: Duration = Duration::from_secs(2);
const ACTIVE_TRANSCRIPT_POLL_SLOW_BACKOFF: Duration = Duration::from_secs(5);
const ACTIVE_TRANSCRIPT_POLL_TTL: Duration = Duration::from_secs(2 * 60 * 60);
const MAX_TRANSCRIPT_WAKE_TRACKED_PATHS: usize = 4096;
const UNMANAGED_HOOK_CATCHUP_DELAY: Duration = Duration::from_secs(30);
const TERMINAL_CATCHUP_DELAYS: [Duration; 3] = [
    Duration::from_secs(0),
    Duration::from_secs(1),
    Duration::from_secs(3),
];
const CLAUDE_TERMINAL_EVENT_TIMEOUT: Duration = Duration::from_secs(2);
const CLAUDE_TERMINAL_EVENT_SOURCE: &str = "claude_channel_scan";
const CLAUDE_TERMINAL_EVENT_STALE_SECS: i64 = 10 * 60;
const CLAUDE_TERMINAL_EVENT_BATCH_LIMIT: usize = 128;

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
    flight_recorder: Option<FlightRecorder>,
}

struct PathTaskResult {
    job: PathJob,
    events_shipped: usize,
    resolved_spool: usize,
    failed_spool: usize,
    had_connect_error: bool,
    rerun_priority: Option<WorkPriority>,
    local_retry_after: Option<Duration>,
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
}

#[derive(Debug, Clone)]
struct TranscriptCatchup {
    due_at: Instant,
    path: PathBuf,
    provider: &'static str,
    observation_source: &'static str,
    observed_at_ms: i64,
    wake_received_at_ms: Option<i64>,
    session_id: Option<String>,
    turn_id: Option<String>,
    wake_reason: Option<String>,
    file_len_hint: Option<u64>,
}

#[derive(Debug, Clone)]
struct ActiveTranscriptPoll {
    due_at: Instant,
    expires_at: Instant,
    provider: &'static str,
    session_id: Option<String>,
    turn_id: Option<String>,
    wake_reason: Option<String>,
    file_len_hint: Option<u64>,
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
    let task_context = PathTaskContext {
        shipper_config: config.shipper_config.clone(),
        client: client.clone(),
        algo: config.algo,
        tracker: tracker.clone(),
        parse_tracker: parse_tracker.clone(),
        ship_stats: ship_stats.clone(),
        flight_recorder: flight_recorder.clone(),
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
    let mut discovery_tasks: JoinSet<DiscoveryTaskResult> = JoinSet::new();
    let mut deferred_retries = HashMap::new();
    let mut transcript_catchups = Vec::new();
    let mut active_transcript_polls = HashMap::new();

    let initial_retry_paths =
        queue_pending_spool_paths(&mut scheduler, &conn, INITIAL_SPOOL_PATH_LIMIT)?;
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
    let mut codex_terminal_catchup_marks: HashMap<PathBuf, String> = HashMap::new();
    let mut latest_transcript_wake_observed: HashMap<PathBuf, i64> = HashMap::new();
    let mut bridge_reaper = ManagedBridgeReaper::from_env();
    let mut outbox_post_tasks: JoinSet<(usize, usize)> = JoinSet::new();
    let mut heartbeat_post_tasks: JoinSet<HeartbeatPostResult> = JoinSet::new();
    let mut claude_terminal_post_tasks: JoinSet<ClaudeTerminalPostResult> = JoinSet::new();
    let mut outbox_signal_marks: HashSet<String> = HashSet::new();
    let mut live_claude_channels: HashMap<String, ClaudeLiveChannelSession> = HashMap::new();
    let mut pending_claude_terminal_signals: HashMap<String, ClaudeTerminalSignal> = HashMap::new();

    let outbox_dir = config::get_agent_outbox_dir()?;
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

    loop {
        pump_ready_local_work(
            &mut scheduler,
            &mut in_flight,
            &task_context,
            &mut deferred_retries,
            &mut transcript_catchups,
            &mut active_transcript_polls,
            offline.is_offline,
        );

        tokio::select! {
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
                        backoff_slow_active_transcript_poll(
                            &retry_path,
                            result.processing_elapsed,
                            &mut active_transcript_polls,
                        );
                        if let Some(delay) = result.local_retry_after {
                            deferred_retries.insert(retry_path, DeferredRetry {
                                due_at: Instant::now() + delay,
                                provider: retry_provider,
                                priority: result.job.priority,
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
                    &mut latest_transcript_wake_observed,
                    signal,
                );
                pump_ready_local_work(
                    &mut scheduler,
                    &mut in_flight,
                    &task_context,
                    &mut deferred_retries,
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    offline.is_offline,
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

            outbox_post_result = outbox_post_tasks.join_next(), if !outbox_post_tasks.is_empty() => {
                match outbox_post_result {
                    Some(Ok((sent, kept))) => {
                        if sent > 0 || kept > 0 {
                            tracing::debug!("Outbox presence POST: {} sent, {} pending", sent, kept);
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Outbox presence POST task failed: {}", err);
                    }
                    None => {}
                }
            }

            heartbeat_post_result = heartbeat_post_tasks.join_next(), if !heartbeat_post_tasks.is_empty() => {
                match heartbeat_post_result {
                    Some(Ok(result)) => {
                        match result.result {
                            Ok(()) => {
                                tracing::debug!(
                                    reason = result.reason,
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

            _ = &mut startup_reconciliation_timer, if startup_reconciliation_pending && !offline.is_offline => {
                startup_reconciliation_pending = false;
                maybe_start_reconciliation_scan(
                    &mut discovery_tasks,
                    &providers,
                    &scheduler,
                    &deferred_retries,
                    &transcript_catchups,
                    &active_transcript_polls,
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

            // File change events (primary path) — skip when offline
            batch = watcher.next_batch(config.flush_interval), if !offline.is_offline => {
                match batch {
                    Some(events) if !events.is_empty() => {
                        for event in events {
                            let provider = match discovery::provider_for_path(&event.path, &providers) {
                                Some(provider) => provider,
                                None => {
                                    tracing::debug!(
                                        "Skipping file outside known providers: {}",
                                        event.path.display()
                                    );
                                    continue;
                                }
                            };
                            scheduler.enqueue_observed(
                                event.path,
                                provider,
                                WorkPriority::Watch,
                                "fsevent",
                                event.observed_at_ms,
                            );
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
                maybe_start_reconciliation_scan(
                    &mut discovery_tasks,
                    &providers,
                    &scheduler,
                    &deferred_retries,
                    &transcript_catchups,
                    &active_transcript_polls,
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

            // Outbox drain: presence events written by hooks. Transcript
            // catch-up is local truth, so keep collecting local phase signals
            // even while remote POSTs are paused by offline mode.
            _ = outbox_timer.tick() => {
                let outbox_result = outbox::collect_outbox_with_local_state_result(
                    &outbox_dir,
                    config.shipper_config.db_path.as_deref(),
                );
                let new_signals =
                    filter_new_outbox_signals(outbox_result.signals, &mut outbox_signal_marks);
                if !new_signals.is_empty() {
                    schedule_transcript_catchups_for_signals(
                        &conn,
                        &mut transcript_catchups,
                        &mut active_transcript_polls,
                        new_signals,
                    );
                    pump_ready_local_work(
                        &mut scheduler,
                        &mut in_flight,
                        &task_context,
                        &mut deferred_retries,
                        &mut transcript_catchups,
                        &mut active_transcript_polls,
                        offline.is_offline,
                    );
                }
                if !offline.is_offline && !outbox_result.posts.is_empty() {
                    if outbox_post_tasks.is_empty() {
                        let client = client.clone();
                        outbox_post_tasks.spawn_local(async move {
                            outbox::post_pending_presence_files(&client, outbox_result.posts).await
                        });
                    } else {
                        tracing::debug!(
                            pending_posts = outbox_result.posts.len(),
                            "Skipping outbox presence POST while previous POST is still in flight"
                        );
                    }
                }
            }

            // Wake the loop when delayed local retry/catch-up work may now be ready.
            _ = local_retry_timer.tick(), if !deferred_retries.is_empty() || !transcript_catchups.is_empty() || !active_transcript_polls.is_empty() => {}

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
                        transcript_catchups.len(),
                        active_transcript_polls.len(),
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
                let observations = managed_bridge_scan::collect_observations();
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
                schedule_transcript_catchups_for_codex_observations(
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    &mut codex_terminal_catchup_marks,
                    &observations,
                );
                pump_ready_local_work(
                    &mut scheduler,
                    &mut in_flight,
                    &task_context,
                    &mut deferred_retries,
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    offline.is_offline,
                );
                let live_local_work_waiting = live_local_work_pending(
                    &scheduler,
                    &deferred_retries,
                    &transcript_catchups,
                    &active_transcript_polls,
                );
                let reused_unmanaged_bindings =
                    live_local_work_waiting && last_unmanaged_session_bindings.is_some();
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
                    if reused_unmanaged_bindings {
                        last_unmanaged_session_bindings.as_deref()
                    } else {
                        None
                    },
                );
                if !reused_unmanaged_bindings {
                    last_unmanaged_session_bindings =
                        Some(payload.unmanaged_session_bindings.clone());
                }
                bridge_reaper.tick(&observations);
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

            // Periodic server heartbeat
            _ = heartbeat_timer.tick() => {
                let observations = managed_bridge_scan::collect_observations();
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
                schedule_transcript_catchups_for_codex_observations(
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    &mut codex_terminal_catchup_marks,
                    &observations,
                );
                pump_ready_local_work(
                    &mut scheduler,
                    &mut in_flight,
                    &task_context,
                    &mut deferred_retries,
                    &mut transcript_catchups,
                    &mut active_transcript_polls,
                    offline.is_offline,
                );
                let live_local_work_waiting = live_local_work_pending(
                    &scheduler,
                    &deferred_retries,
                    &transcript_catchups,
                    &active_transcript_polls,
                );
                let reused_unmanaged_bindings =
                    live_local_work_waiting && last_unmanaged_session_bindings.is_some();
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
                    if reused_unmanaged_bindings {
                        last_unmanaged_session_bindings.as_deref()
                    } else {
                        None
                    },
                );
                if !reused_unmanaged_bindings {
                    last_unmanaged_session_bindings =
                        Some(payload.unmanaged_session_bindings.clone());
                }
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
    payload.unmanaged_session_bindings =
        if let Some(cached_bindings) = unmanaged_session_binding_override {
            cached_bindings.to_vec()
        } else {
            heartbeat::collect_unmanaged_session_bindings_with_store(
                conn,
                machine_id,
                chrono::Utc::now(),
            )
        };
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

fn live_local_work_pending(
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    transcript_catchups: &[TranscriptCatchup],
    active_transcript_polls: &HashMap<PathBuf, ActiveTranscriptPoll>,
) -> bool {
    scheduler.has_pending_work()
        || !deferred_retries.is_empty()
        || !transcript_catchups.is_empty()
        || !active_transcript_polls.is_empty()
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
    transcript_catchup_count: usize,
    active_transcript_poll_count: usize,
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
            "transcript_catchup_count": transcript_catchup_count,
            "active_transcript_poll_count": active_transcript_poll_count,
        },
    }));
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

fn local_retry_delay(priority: WorkPriority) -> Duration {
    if priority == WorkPriority::Live {
        LIVE_LOCAL_RETRY_DELAY
    } else {
        Duration::from_secs(LOCAL_RETRY_DELAY_SECS)
    }
}

fn enqueue_discovered_files(
    scheduler: &mut PathScheduler,
    all_files: Vec<(PathBuf, &'static str)>,
    priority: WorkPriority,
) -> usize {
    let count = all_files.len();
    for (path, provider) in all_files {
        scheduler.enqueue_observed(path, provider, priority, "discovery_scan", now_ms());
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

fn maybe_start_reconciliation_scan(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    transcript_catchups: &[TranscriptCatchup],
    active_transcript_polls: &HashMap<PathBuf, ActiveTranscriptPoll>,
    reason: &'static str,
) {
    if !discovery_tasks.is_empty() {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because discovery is still running"
        );
        return;
    }

    if scheduler.has_pending_work()
        || !deferred_retries.is_empty()
        || !transcript_catchups.is_empty()
        || !active_transcript_polls.is_empty()
    {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan while live local work is pending"
        );
        return;
    }

    tracing::debug!(reason, "Starting reconciliation scan in background");
    start_discovery_task(discovery_tasks, providers, WorkPriority::Scan, reason);
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
        let result = heartbeat::send_heartbeat(&client, &payload)
            .await
            .map_err(|err| err.to_string());
        HeartbeatPostResult {
            signature,
            reason,
            result,
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
    tasks.spawn_local(async move { post_claude_terminal_signals(client, signals).await });
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
        "gemini" => Some("gemini"),
        _ => None,
    }
}

fn work_context(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Live => "live_transcript",
        WorkPriority::Watch => "watch",
        WorkPriority::Catchup => "hook_catchup",
        WorkPriority::Retry => "spool_replay",
        WorkPriority::Scan => "reconciliation_scan",
    }
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn clean_optional_string(value: Option<String>) -> Option<String> {
    value.and_then(|raw| {
        let trimmed = raw.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_string())
    })
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
    transcript_catchups: &mut Vec<TranscriptCatchup>,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
    offline: bool,
) {
    drain_due_local_retries(scheduler, deferred_retries);
    drain_due_transcript_catchups(scheduler, transcript_catchups);
    drain_due_active_transcript_polls(scheduler, active_transcript_polls);
    if !offline {
        start_ready_jobs(
            scheduler,
            in_flight,
            task_context,
            !active_transcript_polls.is_empty(),
        );
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

fn drain_due_transcript_catchups(
    scheduler: &mut PathScheduler,
    transcript_catchups: &mut Vec<TranscriptCatchup>,
) {
    let now = Instant::now();
    let mut index = 0usize;
    while index < transcript_catchups.len() {
        if transcript_catchups[index].due_at <= now {
            let catchup = transcript_catchups.swap_remove(index);
            scheduler.enqueue_observation(
                catchup.path,
                catchup.provider,
                transcript_catchup_priority(catchup.observation_source),
                ObservationTrace {
                    source: catchup.observation_source,
                    observed_at_ms: catchup.observed_at_ms,
                    wake_received_at_ms: catchup.wake_received_at_ms,
                    enqueued_at_ms: 0,
                    session_id: catchup.session_id,
                    turn_id: catchup.turn_id,
                    wake_reason: catchup.wake_reason,
                    file_len_hint: catchup.file_len_hint,
                },
            );
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

        if scheduler.path_in_flight(&path) {
            if let Some(poll) = active_transcript_polls.get_mut(&path) {
                poll.due_at = now + ACTIVE_TRANSCRIPT_POLL_INTERVAL;
            }
            continue;
        }

        scheduler.enqueue_observation(
            path.clone(),
            poll.provider,
            WorkPriority::Live,
            ObservationTrace {
                source: "active_poll",
                observed_at_ms: now_ms(),
                wake_received_at_ms: None,
                enqueued_at_ms: 0,
                session_id: poll.session_id.clone(),
                turn_id: poll.turn_id.clone(),
                wake_reason: poll.wake_reason.clone(),
                file_len_hint: poll.file_len_hint,
            },
        );
        if let Some(poll) = active_transcript_polls.get_mut(&path) {
            poll.due_at = now + ACTIVE_TRANSCRIPT_POLL_INTERVAL;
        }
    }
}

fn backoff_slow_active_transcript_poll(
    path: &Path,
    processing_elapsed: Duration,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
) {
    if processing_elapsed < ACTIVE_TRANSCRIPT_POLL_SLOW_THRESHOLD {
        return;
    }

    let Some(poll) = active_transcript_polls.get_mut(path) else {
        return;
    };

    let due_at = Instant::now() + ACTIVE_TRANSCRIPT_POLL_SLOW_BACKOFF;
    if poll.due_at < due_at {
        poll.due_at = due_at;
    }
    tracing::debug!(
        path = %path.display(),
        elapsed_ms = processing_elapsed.as_millis() as u64,
        backoff_ms = ACTIVE_TRANSCRIPT_POLL_SLOW_BACKOFF.as_millis() as u64,
        "Backed off slow active transcript poll"
    );
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

        let managed_bound_path = path_has_session_binding(conn, &path, provider);
        let observation_source = if managed_bound_path {
            "outbox_signal"
        } else {
            "outbox_signal_unmanaged"
        };

        let catchup_start = transcript_catchups.len();
        schedule_transcript_catchup(
            transcript_catchups,
            active_transcript_polls,
            path,
            provider,
            &signal.phase,
            observation_source,
            signal.observed_at.timestamp_millis(),
            None,
            managed_bound_path,
            None,
            None,
            None,
            None,
        );
        if !managed_bound_path {
            for catchup in &mut transcript_catchups[catchup_start..] {
                catchup.due_at += UNMANAGED_HOOK_CATCHUP_DELAY;
            }
        }
    }
}

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

fn schedule_transcript_catchup_for_wake(
    transcript_catchups: &mut Vec<TranscriptCatchup>,
    active_transcript_polls: &mut HashMap<PathBuf, ActiveTranscriptPoll>,
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
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
        return;
    }
    schedule_transcript_catchup(
        transcript_catchups,
        active_transcript_polls,
        signal.path,
        provider,
        &signal.phase,
        "wake_socket",
        signal.observed_at_ms,
        signal.received_at_ms,
        true,
        clean_optional_string(signal.session_id),
        clean_optional_string(signal.turn_id),
        clean_optional_string(signal.wake_reason),
        signal.file_len_hint,
    );
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
            "bridge_scan",
            now_ms(),
            None,
            true,
            Some(observation.session_id.clone()),
            observation.active_turn_id.clone(),
            None,
            None,
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
    observation_source: &'static str,
    observed_at_ms: i64,
    wake_received_at_ms: Option<i64>,
    allow_active_poll: bool,
    session_id: Option<String>,
    turn_id: Option<String>,
    wake_reason: Option<String>,
    file_len_hint: Option<u64>,
) {
    if is_terminal_or_attention_phase(phase) {
        let has_wake_socket_catchup = transcript_catchups
            .iter()
            .any(|existing| existing.path == path && existing.observation_source == "wake_socket");
        if active_transcript_polls.remove(&path).is_some() {
            tracing::info!(
                path = %path.display(),
                provider,
                phase,
                "Stopped active transcript polling"
            );
        }
        if has_wake_socket_catchup && observation_source != "wake_socket" {
            return;
        }
        transcript_catchups.retain(|existing| existing.path != path);
        let now = Instant::now();
        for delay in TERMINAL_CATCHUP_DELAYS {
            transcript_catchups.push(TranscriptCatchup {
                due_at: now + delay,
                path: path.clone(),
                provider,
                observation_source,
                observed_at_ms,
                wake_received_at_ms,
                session_id: session_id.clone(),
                turn_id: turn_id.clone(),
                wake_reason: wake_reason.clone(),
                file_len_hint,
            });
        }
        return;
    }

    if is_active_phase(phase) {
        let now = Instant::now();
        if allow_active_poll {
            let was_polling = active_transcript_polls.contains_key(&path);
            active_transcript_polls.insert(
                path.clone(),
                ActiveTranscriptPoll {
                    due_at: now,
                    expires_at: now + ACTIVE_TRANSCRIPT_POLL_TTL,
                    provider,
                    session_id: session_id.clone(),
                    turn_id: turn_id.clone(),
                    wake_reason: wake_reason.clone(),
                    file_len_hint,
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
        }
        // Turn-start wakes arrive before Codex has necessarily written useful
        // rollout content. Progress wakes happen after output changes and
        // should enter the immediate live lane.
        let wake_is_turn_start =
            observation_source == "wake_socket" && wake_reason.as_deref() == Some("turn_started");
        if wake_is_turn_start {
            return;
        }

        if observation_source == "wake_socket" {
            transcript_catchups.retain(|item| item.path != path);
        } else if transcript_catchups.iter().any(|item| item.path == path) {
            return;
        }

        {
            transcript_catchups.push(TranscriptCatchup {
                due_at: now,
                path,
                provider,
                observation_source,
                observed_at_ms,
                wake_received_at_ms,
                session_id,
                turn_id,
                wake_reason,
                file_len_hint,
            });
        }
    }
}

fn transcript_catchup_priority(observation_source: &str) -> WorkPriority {
    match observation_source {
        "wake_socket" | "outbox_signal" => WorkPriority::Live,
        "bridge_scan" => WorkPriority::Catchup,
        _ => WorkPriority::Watch,
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

fn path_has_session_binding(conn: &Connection, path: &Path, _provider: &str) -> bool {
    let binding = crate::state::session_binding::SessionBinding::new(conn);
    let canonical = std::fs::canonicalize(path)
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .to_string();
    binding.get(&canonical).ok().flatten().is_some()
        || binding
            .get(&path.to_string_lossy())
            .ok()
            .flatten()
            .is_some()
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
    let db_path = task_context.shipper_config.db_path.clone();
    let max_batch_bytes = task_context.shipper_config.max_batch_bytes;
    let parse_tracker = task_context.parse_tracker.clone();
    let session_id_hint = job.observation.session_id.clone();
    let source_line_mode = if job.priority == WorkPriority::Live && provider == "codex" {
        shipper::SourceLineMode::EventOnly
    } else {
        shipper::SourceLineMode::Full
    };
    let blocking_span = tracing::info_span!(
        "engine.ship.prepare.blocking",
        longhouse.provider = %provider,
        longhouse.work_context = %work_context_label,
    );

    tokio::task::spawn_blocking(move || {
        let _enter = blocking_span.enter();
        let mut trace_timings = shipper::PrepareTraceTimings::default();
        let open_db_started = Instant::now();
        let conn = open_db(db_path.as_deref())?;
        trace_timings.open_db_ms = Some(open_db_started.elapsed().as_millis() as u64);
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
        processing_elapsed: Duration::ZERO,
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
            result.local_retry_after = Some(local_retry_delay(result.job.priority));
            return finish_path_task(result, task_started);
        }
    };

    if result.job.priority != WorkPriority::Live {
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
                wake_received_at_ms: result.job.observation.wake_received_at_ms,
                enqueued_at_ms: result.job.observation.enqueued_at_ms,
                job_started_at_ms,
                prepare_started_at_ms,
                prepare_finished_at_ms,
                prepare_open_db_ms: trace_timings.open_db_ms,
                prepare_binding_wait_ms: trace_timings.binding_wait_ms,
                prepare_parse_ms: trace_timings.parse_ms,
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
        );

        assert_eq!(payload.unmanaged_session_bindings, cached);
    }

    #[test]
    fn test_live_local_work_pending_includes_active_transcript_polls() {
        let scheduler = PathScheduler::new(4);
        let deferred_retries = HashMap::new();
        let transcript_catchups = Vec::new();
        let mut active_polls = HashMap::new();
        active_polls.insert(
            PathBuf::from("/tmp/live.jsonl"),
            ActiveTranscriptPoll {
                due_at: Instant::now(),
                expires_at: Instant::now() + Duration::from_secs(30),
                provider: "codex",
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            },
        );

        assert!(live_local_work_pending(
            &scheduler,
            &deferred_retries,
            &transcript_catchups,
            &active_polls,
        ));
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
            "test",
            123,
            None,
            true,
            None,
            None,
            None,
            None,
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
        assert_eq!(job.priority, WorkPriority::Watch);
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
            "test",
            123,
            None,
            true,
            None,
            None,
            None,
            None,
        );
        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path.clone(),
            "claude",
            "running",
            "test",
            124,
            None,
            true,
            None,
            None,
            None,
            None,
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(active_polls.len(), 1);

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);

        let job = scheduler.pop_launchable().expect("active catch-up queued");
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Watch);
        assert_eq!(job.observation.source, "test");
        assert_eq!(job.observation.observed_at_ms, 123);
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
                "test",
                123,
                None,
                true,
                None,
                None,
                None,
                None,
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
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            },
        );

        let mut scheduler = PathScheduler::new(4);
        drain_due_active_transcript_polls(&mut scheduler, &mut active_polls);

        let job = scheduler.pop_launchable().expect("active poll queued");
        assert_eq!(job.path, path);
        assert_eq!(job.provider, "codex");
        assert_eq!(job.priority, WorkPriority::Live);
        assert_eq!(active_polls.len(), 1);
        assert!(active_polls[&path].due_at > now);
    }

    #[test]
    fn test_active_transcript_poll_skips_path_already_in_flight() {
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
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            },
        );

        let mut scheduler = PathScheduler::new(4);
        scheduler.enqueue(path.clone(), "codex", WorkPriority::Live);
        let _in_flight = scheduler.pop_launchable().expect("job launched");

        drain_due_active_transcript_polls(&mut scheduler, &mut active_polls);

        assert!(
            scheduler.pop_launchable().is_none(),
            "active polling should not queue a rerun while the same path is already in flight"
        );
        assert_eq!(active_polls.len(), 1);
        assert!(active_polls[&path].due_at > now);
    }

    #[test]
    fn test_slow_path_job_backs_off_active_transcript_poll() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        let now = Instant::now();
        let mut active_polls = HashMap::new();
        active_polls.insert(
            path.clone(),
            ActiveTranscriptPoll {
                due_at: now + ACTIVE_TRANSCRIPT_POLL_INTERVAL,
                expires_at: now + Duration::from_secs(60),
                provider: "codex",
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            },
        );

        backoff_slow_active_transcript_poll(
            &path,
            ACTIVE_TRANSCRIPT_POLL_SLOW_THRESHOLD + Duration::from_millis(1),
            &mut active_polls,
        );

        assert!(
            active_polls[&path].due_at >= now + ACTIVE_TRANSCRIPT_POLL_SLOW_BACKOFF,
            "slow active transcript jobs should not be immediately re-polled"
        );
    }

    #[test]
    fn test_wake_socket_active_phase_waits_for_poll() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let path = tmp.path().to_path_buf();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path.clone(),
            "codex",
            "running",
            "wake_socket",
            123,
            Some(125),
            true,
            Some("session-123".to_string()),
            Some("turn-123".to_string()),
            Some("turn_started".to_string()),
            Some(456),
        );

        assert!(catchups.is_empty());
        assert!(active_polls.contains_key(&path));
        let mut scheduler = PathScheduler::new(4);
        drain_due_active_transcript_polls(&mut scheduler, &mut active_polls);
        let job = scheduler.pop_launchable().expect("wake active poll queued");
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Live);
        assert_eq!(job.observation.source, "active_poll");
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

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);
        let job = scheduler
            .pop_launchable()
            .expect("bridge scan catch-up queued");
        assert_eq!(job.path, transcript.path());
        assert_eq!(job.priority, WorkPriority::Catchup);
        assert_eq!(job.observation.source, "bridge_scan");
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
                observation_source: "test",
                observed_at_ms: 123,
                wake_received_at_ms: None,
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            },
            TranscriptCatchup {
                due_at: now + Duration::from_secs(30),
                path: later.path().to_path_buf(),
                provider: "claude",
                observation_source: "test",
                observed_at_ms: 124,
                wake_received_at_ms: None,
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            },
        ];

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);

        let job = scheduler.pop_launchable().expect("ready catch-up queued");
        assert_eq!(job.path, ready.path());
        assert_eq!(job.priority, WorkPriority::Watch);
        assert_eq!(job.observation.source, "test");
        assert_eq!(job.observation.observed_at_ms, 123);
        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].path, later.path());
    }

    #[test]
    fn test_outbox_signal_catchup_uses_live_priority() {
        let ready = tempfile::NamedTempFile::new().unwrap();
        let now = Instant::now();
        let mut catchups = vec![TranscriptCatchup {
            due_at: now - Duration::from_secs(1),
            path: ready.path().to_path_buf(),
            provider: "codex",
            observation_source: "outbox_signal",
            observed_at_ms: 123,
            wake_received_at_ms: None,
            session_id: None,
            turn_id: None,
            wake_reason: None,
            file_len_hint: None,
        }];

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);

        let job = scheduler.pop_launchable().expect("outbox catch-up queued");
        assert_eq!(job.path, ready.path());
        assert_eq!(job.priority, WorkPriority::Live);
        assert_eq!(job.observation.source, "outbox_signal");
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
    fn test_wake_socket_catchup_replaces_pending_outbox_catchup() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let now = Instant::now();
        let mut catchups = vec![TranscriptCatchup {
            due_at: now + Duration::from_secs(30),
            path: transcript.path().to_path_buf(),
            provider: "codex",
            observation_source: "outbox_signal",
            observed_at_ms: 100,
            wake_received_at_ms: None,
            session_id: None,
            turn_id: None,
            wake_reason: None,
            file_len_hint: None,
        }];
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            transcript.path().to_path_buf(),
            "codex",
            "running",
            "wake_socket",
            200,
            Some(205),
            true,
            Some("session-123".to_string()),
            Some("turn-123".to_string()),
            Some("progress".to_string()),
            Some(456),
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].observation_source, "wake_socket");
        assert_eq!(catchups[0].observed_at_ms, 200);
        assert_eq!(catchups[0].wake_received_at_ms, Some(205));
        assert_eq!(catchups[0].session_id.as_deref(), Some("session-123"));
        assert_eq!(catchups[0].turn_id.as_deref(), Some("turn-123"));
        assert_eq!(catchups[0].wake_reason.as_deref(), Some("progress"));
        assert_eq!(catchups[0].file_len_hint, Some(456));
    }

    #[test]
    fn test_outbox_catchup_does_not_replace_pending_wake_socket_catchup() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let now = Instant::now();
        let mut catchups = vec![TranscriptCatchup {
            due_at: now,
            path: transcript.path().to_path_buf(),
            provider: "codex",
            observation_source: "wake_socket",
            observed_at_ms: 100,
            wake_received_at_ms: Some(105),
            session_id: Some("session-123".to_string()),
            turn_id: Some("turn-123".to_string()),
            wake_reason: Some("progress".to_string()),
            file_len_hint: Some(456),
        }];
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            transcript.path().to_path_buf(),
            "codex",
            "running",
            "outbox_signal",
            200,
            None,
            true,
            None,
            None,
            None,
            None,
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].observation_source, "wake_socket");
        assert_eq!(catchups[0].observed_at_ms, 100);
        assert_eq!(catchups[0].session_id.as_deref(), Some("session-123"));
        assert_eq!(catchups[0].file_len_hint, Some(456));
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
        let before = Instant::now();
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
        assert_eq!(catchups[0].observation_source, "outbox_signal_unmanaged");
        assert!(
            catchups[0].due_at >= before + UNMANAGED_HOOK_CATCHUP_DELAY,
            "unmanaged hook catch-ups should yield the immediate lane to managed work"
        );
        assert!(
            active_polls.is_empty(),
            "unmanaged hook signals get one catch-up but do not arm active polling"
        );
    }

    #[test]
    fn test_managed_presence_signal_arms_active_polling() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(transcript.path()).unwrap();
        let managed_session_id = "22222222-2222-4222-8222-222222222222";
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), managed_session_id, "codex")
            .unwrap();

        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let before = Instant::now();
        schedule_transcript_catchups_for_signals(
            &conn,
            &mut catchups,
            &mut active_polls,
            vec![outbox::DrainedPresenceSignal {
                session_id: managed_session_id.to_string(),
                provider: "codex".to_string(),
                phase: "thinking".to_string(),
                observed_at: chrono::Utc::now(),
                transcript_path: Some(transcript.path().to_path_buf()),
            }],
        );

        assert_eq!(catchups.len(), 1);
        assert_eq!(catchups[0].observation_source, "outbox_signal");
        assert!(
            catchups[0].due_at < before + UNMANAGED_HOOK_CATCHUP_DELAY,
            "managed hook catch-ups must stay on the immediate lane"
        );
        assert!(active_polls.contains_key(transcript.path()));
    }

    #[test]
    fn test_transcript_wake_starts_active_polling() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let mut latest_wakes = HashMap::new();

        schedule_transcript_catchup_for_wake(
            &mut catchups,
            &mut active_polls,
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

        assert!(catchups.is_empty());
        assert!(active_polls.contains_key(transcript.path()));
    }

    #[test]
    fn test_progress_wake_enqueues_live_catchup_with_metadata() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let mut latest_wakes = HashMap::new();

        schedule_transcript_catchup_for_wake(
            &mut catchups,
            &mut active_polls,
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

        assert_eq!(catchups.len(), 1);
        assert!(active_polls.contains_key(transcript.path()));

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);
        let job = scheduler.pop_launchable().expect("progress wake queued");
        assert_eq!(job.priority, WorkPriority::Live);
        assert_eq!(job.observation.source, "wake_socket");
        assert_eq!(job.observation.observed_at_ms, 123);
        assert_eq!(job.observation.wake_received_at_ms, Some(124));
        assert_eq!(job.observation.session_id.as_deref(), Some("session-123"));
        assert_eq!(job.observation.turn_id.as_deref(), Some("turn-123"));
        assert_eq!(job.observation.wake_reason.as_deref(), Some("progress"));
        assert_eq!(job.observation.file_len_hint, Some(456));
    }

    #[test]
    fn test_stale_active_wake_does_not_replace_newer_terminal_wake() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();
        let mut latest_wakes = HashMap::new();

        schedule_transcript_catchup_for_wake(
            &mut catchups,
            &mut active_polls,
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

        schedule_transcript_catchup_for_wake(
            &mut catchups,
            &mut active_polls,
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

        assert_eq!(catchups.len(), TERMINAL_CATCHUP_DELAYS.len());
        assert!(active_polls.is_empty());

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);
        let job = scheduler.pop_launchable().expect("terminal wake queued");
        assert_eq!(job.observation.source, "wake_socket");
        assert_eq!(job.observation.observed_at_ms, 200);
        assert_eq!(
            job.observation.wake_reason.as_deref(),
            Some("turn_completed")
        );
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
    fn test_terminal_outbox_signal_does_not_replace_wake_catchup() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let path = transcript.path().to_path_buf();
        let mut catchups = Vec::new();
        let mut active_polls = HashMap::new();

        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path.clone(),
            "codex",
            "idle",
            "wake_socket",
            123,
            Some(124),
            true,
            Some("session-123".to_string()),
            Some("turn-123".to_string()),
            Some("turn_completed".to_string()),
            Some(456),
        );
        schedule_transcript_catchup(
            &mut catchups,
            &mut active_polls,
            path,
            "codex",
            "idle",
            "outbox_signal",
            130,
            None,
            true,
            None,
            None,
            None,
            None,
        );

        let mut scheduler = PathScheduler::new(4);
        drain_due_transcript_catchups(&mut scheduler, &mut catchups);
        let job = scheduler.pop_launchable().expect("terminal wake queued");
        assert_eq!(job.observation.source, "wake_socket");
        assert_eq!(
            job.observation.wake_reason.as_deref(),
            Some("turn_completed")
        );
        assert_eq!(job.observation.session_id.as_deref(), Some("session-123"));
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
            flight_recorder: None,
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
                    priority: WorkPriority::Live,
                    observation: ObservationTrace {
                        source: "wake_socket",
                        observed_at_ms: 100,
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
    fn test_live_retry_delay_is_shorter_than_background_retry() {
        assert_eq!(
            local_retry_delay(WorkPriority::Live),
            LIVE_LOCAL_RETRY_DELAY
        );
        assert_eq!(
            local_retry_delay(WorkPriority::Watch),
            Duration::from_secs(LOCAL_RETRY_DELAY_SECS)
        );
    }
}
