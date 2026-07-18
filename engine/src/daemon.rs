//! Daemon mode (`connect` subcommand).
//!
//! Watches provider directories for file changes using the `notify` crate
//! (FSEvents on macOS, inotify on Linux) and ships new session data
//! incrementally. Designed for 24/7 operation with minimal resources:
//! - <10 MB RSS when idle
//! - 0% CPU when idle (blocked on kernel filesystem events)
//! - Lightweight background work with bounded concurrency
//!
//! Primary transcript shipping is the Live lane: provider file changes or
//! managed wake signals enqueue `WorkPriority::Live` immediately. The spool is
//! a retry/archive store for failed or incomplete shipments, not the steady
//! state live transcript path.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use anyhow::Result;
use serde::Deserialize;
use serde_json::json;
use tokio::io::AsyncReadExt;
use tokio::sync::mpsc;
use tokio::task::JoinSet;

use crate::config::{self, ShipperConfig};
use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::flight::FlightRecorder;
use crate::heartbeat;
use crate::managed_antigravity_scan;
use crate::managed_bridge_scan;
use crate::managed_claude_scan;
use crate::managed_cursor_helm_scan;
use crate::managed_opencode_scan;
use crate::outbox;
use crate::pipeline::compressor::CompressionAlgo;
use crate::scheduler::{AdaptiveLimiter, ObservationTrace, PathJob, PathScheduler, WorkPriority};
use crate::shipper;
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::StorageV2Capabilities;
use crate::shipping_stats::{RecentShipStatsTracker, ShipAttemptOutcome, ShipLane};
use crate::state::db::open_db;
use crate::state::db_pool::ConnectionPool;
use crate::state::file_state::FileState;
use crate::state::spool::Spool;
use crate::unmanaged_bindings;
use crate::watcher::{SessionWatcher, WatcherEvent};

/// Configuration for the connect daemon.
pub struct ConnectConfig {
    pub shipper_config: ShipperConfig,
    pub algo: CompressionAlgo,
    pub fallback_scan_secs: u64,
    pub spool_replay_secs: u64,
    pub archive_repair_mode: ArchiveRepairMode,
    pub flight_recorder_dir: Option<PathBuf>,
    pub prevent_sleep: bool,
}

/// Default archive/backlog repair posture for the daemon.
///
/// The operator control file may move a running daemon between these same
/// values. Keep this vocabulary aligned with server archive-backlog control.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ArchiveRepairMode {
    Paused,
    Trickle,
    Drain,
}

impl ArchiveRepairMode {
    pub fn parse(value: &str) -> Result<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "paused" | "pause" => Ok(Self::Paused),
            "trickle" | "resume" => Ok(Self::Trickle),
            "drain" | "drain-now" => Ok(Self::Drain),
            other => anyhow::bail!(
                "unsupported archive repair mode {other}; expected paused, trickle, or drain"
            ),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Paused => "paused",
            Self::Trickle => "trickle",
            Self::Drain => "drain",
        }
    }

    fn is_paused(self) -> bool {
        matches!(self, Self::Paused)
    }
}

/// How long to coalesce a burst of filesystem events before scheduling work.
/// Short enough to leave the bulk of the 500ms file-append → HTTP-send budget
/// for actual shipping; long enough to coalesce the typical JSONL append
/// burst while keeping live transcript shipping responsive. Provider writes
/// that need more coalescing are still protected by per-path in-flight work
/// and the reconciliation scanner.
const WATCHER_FLUSH_INTERVAL: Duration = Duration::from_millis(15);

const INITIAL_SPOOL_PATH_LIMIT: usize = 64;
const PERIODIC_SPOOL_PATH_LIMIT: usize = 128;
const PATH_SPOOL_REPLAY_LIMIT_PRESSURE: usize = 1;
const PATH_SPOOL_REPLAY_LIMIT_BASE: usize = 2;
const PATH_SPOOL_REPLAY_LIMIT_FAST: usize = 8;
const ARCHIVE_TRICKLE_TICK_BYTES: u64 = 512 * 1024 * 1024;
const ARCHIVE_DRAIN_TICK_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const ARCHIVE_BACKPRESSURE_MAX_DEFER: Duration = Duration::from_secs(90);
const ARCHIVE_STARTUP_REPLAY_WARMUP_MIN: Duration = Duration::from_secs(5);
const ARCHIVE_STARTUP_REPLAY_WARMUP_MAX: Duration = Duration::from_secs(20);
const LOCAL_RETRY_DELAY_SECS: u64 = 5;
const LIVE_LOCAL_RETRY_DELAY: Duration = Duration::from_millis(500);
const STARTUP_RECONCILIATION_SCAN_DELAY: Duration = Duration::from_secs(120);
const LOCAL_STATUS_INTERVAL_SECS: u64 = 1;
const MANAGED_OBSERVATION_INTERVAL_SECS: u64 = 5;
const MANAGED_FULL_RECONCILIATION_INTERVAL_SECS: u64 = 60;
const WAKE_GAP_THRESHOLD_SECS: u64 = 5;
const MACHINE_PRESENCE_INTERVAL_SECS: u64 = 60;
const SERVER_HEARTBEAT_INTERVAL_SECS: u64 = 5 * 60;
const FLIGHT_SAMPLE_INTERVAL_SECS: u64 = 5;

fn failed_shipment_retry_path_limit(limiter: &AdaptiveLimiter) -> usize {
    match limiter.archive_target_batch_bytes() {
        bytes if bytes >= crate::scheduler::ARCHIVE_BATCH_TARGET_MAX_BYTES => {
            PATH_SPOOL_REPLAY_LIMIT_FAST
        }
        bytes if bytes >= crate::scheduler::ARCHIVE_BATCH_TARGET_BASE_BYTES => {
            PATH_SPOOL_REPLAY_LIMIT_BASE
        }
        _ => PATH_SPOOL_REPLAY_LIMIT_PRESSURE,
    }
}
const LOCAL_WORK_TICK_INTERVAL: Duration = Duration::from_millis(250);
const OUTBOX_DRAIN_INTERVAL: Duration = Duration::from_millis(100);
const MANAGED_WAKE_FSEVENT_DEFER_WINDOW: Duration = Duration::from_secs(30);
const MANAGED_WAKE_FSEVENT_FALLBACK_DELAY: Duration = Duration::from_secs(5);
const MAX_TRANSCRIPT_WAKE_TRACKED_PATHS: usize = 4096;
const OFFLINE_CONNECT_FAILURE_THRESHOLD: u32 = 3;
// Stable telemetry strings for the retry/archive lane. Keep the wire names
// for historical engine-status/log readers, but keep code names explicit.
const FAILED_SHIPMENT_RETRY_CONTEXT: &str = "spool_replay";
const FAILED_SHIPMENT_RETRY_OBSERVATION_SOURCE: &str = "spool_pending";

struct WakeGapDetector {
    last_wall: SystemTime,
    last_monotonic: Instant,
}

impl WakeGapDetector {
    fn new() -> Self {
        Self {
            last_wall: SystemTime::now(),
            last_monotonic: Instant::now(),
        }
    }

    fn observe(&mut self, wall: SystemTime, monotonic: Instant) -> Option<Duration> {
        let previous_wall = self.last_wall;
        let previous_monotonic = self.last_monotonic;
        self.last_wall = wall;
        self.last_monotonic = monotonic;
        let wall_elapsed = wall.duration_since(previous_wall).ok()?;
        let monotonic_elapsed = monotonic.saturating_duration_since(previous_monotonic);
        let gap = wall_elapsed.saturating_sub(monotonic_elapsed);
        (gap >= Duration::from_secs(WAKE_GAP_THRESHOLD_SECS)).then_some(gap)
    }
}

/// Spawn caffeinate -s -w <pid> to prevent system sleep on macOS.
///
/// caffeinate exits when the given PID disappears, so crash/abort/launchd
/// restart all clean up without orphaning the sleep assertion.
pub fn spawn_caffeinate(pid: u32) -> std::io::Result<tokio::process::Child> {
    tokio::process::Command::new("caffeinate")
        .arg("-s")
        .arg("-w")
        .arg(pid.to_string())
        .spawn()
}

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
    storage_v2: Option<std::sync::Arc<StorageV2Capabilities>>,
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

fn is_opencode_database_job(job: &PathJob) -> bool {
    job.provider == "opencode" && crate::opencode_db::is_opencode_database_path(&job.path)
}

fn is_cursor_database_job(job: &PathJob) -> bool {
    job.provider == "cursor" && crate::cursor_store::is_cursor_store_database_path(&job.path)
}

fn is_cursor_acp_source_job(job: &PathJob) -> bool {
    job.provider == "cursor_acp"
        && job.path.extension().and_then(|value| value.to_str()) == Some("jsonl")
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

struct MachinePresencePostResult {
    result: Result<bool, String>,
    task_elapsed_ms: u64,
}

struct OutboxCollectResult {
    presence: outbox::OutboxLocalDrainResult,
    runtime_posts: Vec<outbox::PendingRuntimeEventPost>,
    elapsed_ms: u64,
}

struct UnmanagedBindingRefreshResult {
    generation: u64,
    reason: &'static str,
    full_reconciliation_candidate: bool,
    managed: ManagedObservationSnapshot,
    managed_scan_partial: bool,
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

#[derive(Clone)]
struct ManagedObservationScanResult {
    reason: &'static str,
    full_reconciliation: bool,
    process_inventory_valid: bool,
    process_inventory: Vec<unmanaged_bindings::ProcessInfo>,
    codex_observations: Vec<managed_bridge_scan::CodexBridgeObservation>,
    antigravity_observations: Vec<managed_antigravity_scan::AntigravityHookObservation>,
    claude_observations: Vec<managed_claude_scan::ClaudeChannelObservation>,
    opencode_observations: Vec<managed_opencode_scan::OpenCodeServerObservation>,
    cursor_observations: Vec<managed_cursor_helm_scan::CursorHelmObservation>,
    process_inventory_ms: u64,
    codex_elapsed_ms: u64,
    antigravity_elapsed_ms: u64,
    claude_elapsed_ms: u64,
    opencode_elapsed_ms: u64,
    cursor_elapsed_ms: u64,
    retained_stale_rows: usize,
    elapsed_ms: u64,
}

#[derive(Clone, Default, PartialEq, Eq)]
struct ManagedObservationSnapshot {
    codex: Vec<managed_bridge_scan::CodexBridgeObservation>,
    antigravity: Vec<managed_antigravity_scan::AntigravityHookObservation>,
    claude: Vec<managed_claude_scan::ClaudeChannelObservation>,
    opencode: Vec<managed_opencode_scan::OpenCodeServerObservation>,
    cursor: Vec<managed_cursor_helm_scan::CursorHelmObservation>,
}

struct ProjectionBuildInput {
    generation: u64,
    managed_scan_partial: bool,
    process_snapshot_complete: bool,
    db_path: PathBuf,
    tracker: ConsecutiveErrorTracker,
    parse_tracker: RecentIssueTracker,
    ship_stats: RecentShipStatsTracker,
    is_offline: bool,
    last_ship_at: Option<String>,
    machine_id: String,
    managed: ManagedObservationSnapshot,
    unmanaged: Vec<heartbeat::UnmanagedSessionBinding>,
    limiter: crate::scheduler::LimiterSnapshot,
    scheduler: crate::scheduler::SchedulerSnapshot,
    archive_repair_mode: ArchiveRepairMode,
    last_full_reconciled_at: Option<String>,
    session_snapshot_state: SessionSnapshotState,
}

struct ProjectionBuildResult {
    generation: u64,
    managed_scan_partial: bool,
    result: Result<(heartbeat::StatusFileProjection, SessionSnapshotState), String>,
    elapsed_ms: u64,
}

impl ManagedObservationSnapshot {
    fn from_result(result: &ManagedObservationScanResult) -> Self {
        Self {
            codex: result.codex_observations.clone(),
            antigravity: result.antigravity_observations.clone(),
            claude: result.claude_observations.clone(),
            opencode: result.opencode_observations.clone(),
            cursor: result.cursor_observations.clone(),
        }
    }

    fn contains_state_file(&self, path: &Path) -> bool {
        self.codex.iter().any(|row| row.state_file == path)
            || self.antigravity.iter().any(|row| row.state_file == path)
            || self.claude.iter().any(|row| row.state_file == path)
            || self.opencode.iter().any(|row| row.state_file == path)
            || self.cursor.iter().any(|row| row.state_file == path)
    }

    fn projection_equivalent(&self, other: &Self) -> bool {
        let mut left = self.clone();
        let mut right = other.clone();
        for snapshot in [&mut left, &mut right] {
            for row in &mut snapshot.codex {
                row.updated_at.clear();
                row.active_turn_id = None;
                row.last_turn_status = None;
            }
            for row in &mut snapshot.antigravity {
                row.updated_at.clear();
            }
            for row in &mut snapshot.claude {
                row.updated_at.clear();
            }
            for row in &mut snapshot.opencode {
                row.updated_at.clear();
            }
            for row in &mut snapshot.cursor {
                row.updated_at.clear();
            }
        }
        left == right
    }

    fn current_only(&self) -> Self {
        Self {
            codex: self
                .codex
                .iter()
                .filter(|row| row.bridge_alive || row.app_server_alive || row.has_tui_attachment)
                .cloned()
                .collect(),
            antigravity: self.antigravity.clone(),
            claude: self
                .claude
                .iter()
                .filter(|row| row.claude_alive || row.bridge_alive)
                .cloned()
                .collect(),
            opencode: self
                .opencode
                .iter()
                .filter(|row| row.server_alive || row.has_tui_attachment)
                .cloned()
                .collect(),
            cursor: self.cursor.iter().filter(|row| row.live).cloned().collect(),
        }
    }
}

fn managed_provider_state_dirs() -> Vec<PathBuf> {
    [
        managed_bridge_scan::default_codex_bridge_state_dir(),
        managed_antigravity_scan::default_antigravity_state_dir(),
        managed_claude_scan::default_claude_channel_state_dir(),
        managed_opencode_scan::default_opencode_server_state_dir(),
        managed_cursor_helm_scan::default_cursor_helm_state_dir(),
    ]
    .into_iter()
    .flatten()
    .collect()
}

#[derive(Clone, Debug, Default, Deserialize)]
struct ArchiveRepairControl {
    mode: Option<String>,
    expires_at: Option<String>,
    max_tick_bytes: Option<u64>,
    include_huge: Option<bool>,
    actor: Option<String>,
    reason: Option<String>,
    updated_at: Option<String>,
}

impl ArchiveRepairControl {
    fn active_override(&self) -> bool {
        let mode = self
            .mode
            .as_deref()
            .unwrap_or("")
            .trim()
            .to_ascii_lowercase();
        if matches!(mode.as_str(), "paused" | "pause") {
            return true;
        }
        let Some(expires_at) = self.expires_at.as_deref() else {
            return false;
        };
        chrono::DateTime::parse_from_rfc3339(expires_at)
            .map(|value| value.with_timezone(&chrono::Utc) > chrono::Utc::now())
            .unwrap_or(false)
    }

    fn normalized_mode(&self, default_mode: ArchiveRepairMode) -> ArchiveRepairMode {
        if !self.active_override() {
            return default_mode;
        }
        match self
            .mode
            .as_deref()
            .unwrap_or(default_mode.as_str())
            .trim()
            .to_ascii_lowercase()
            .as_str()
        {
            "paused" | "pause" => ArchiveRepairMode::Paused,
            "trickle" | "resume" => ArchiveRepairMode::Trickle,
            "drain" | "drain-now" => ArchiveRepairMode::Drain,
            _ => default_mode,
        }
    }

    fn is_paused(&self, default_mode: ArchiveRepairMode) -> bool {
        self.normalized_mode(default_mode).is_paused()
    }

    fn tick_bytes(&self, default_mode: ArchiveRepairMode) -> u64 {
        if !self.active_override() {
            return match default_mode {
                ArchiveRepairMode::Drain => ARCHIVE_DRAIN_TICK_BYTES,
                ArchiveRepairMode::Paused | ArchiveRepairMode::Trickle => {
                    ARCHIVE_TRICKLE_TICK_BYTES
                }
            };
        }
        match self.normalized_mode(default_mode) {
            ArchiveRepairMode::Drain => self.max_tick_bytes.unwrap_or(ARCHIVE_DRAIN_TICK_BYTES),
            ArchiveRepairMode::Paused | ArchiveRepairMode::Trickle => {
                self.max_tick_bytes.unwrap_or(ARCHIVE_TRICKLE_TICK_BYTES)
            }
        }
    }

    fn includes_huge(&self) -> bool {
        if self.active_override() {
            self.include_huge.unwrap_or(true)
        } else {
            true
        }
    }
}

fn read_archive_repair_control() -> ArchiveRepairControl {
    let Ok(path) = config::get_agent_archive_repair_control_path() else {
        return ArchiveRepairControl::default();
    };
    let Ok(bytes) = std::fs::read(&path) else {
        return ArchiveRepairControl::default();
    };
    match serde_json::from_slice::<ArchiveRepairControl>(&bytes) {
        Ok(control) => control,
        Err(err) => {
            tracing::warn!(
                path = %path.display(),
                error = %err,
                "Ignoring invalid archive repair control file"
            );
            ArchiveRepairControl::default()
        }
    }
}

fn apply_archive_repair_control(
    payload: &mut heartbeat::HeartbeatPayload,
    control: &ArchiveRepairControl,
    default_mode: ArchiveRepairMode,
) {
    let mode = control.normalized_mode(default_mode);
    payload.archive_backlog.mode = mode.as_str().to_string();
    payload.archive_backlog.pause_actor = None;
    payload.archive_backlog.pause_reason = None;
    payload.archive_backlog.pause_updated_at = None;
    if mode == ArchiveRepairMode::Paused && payload.archive_backlog.pending_ranges > 0 {
        payload.archive_backlog.state = "paused".to_string();
        payload.archive_backlog.pause_actor = control.actor.clone();
        payload.archive_backlog.pause_reason = control.reason.clone();
        payload.archive_backlog.pause_updated_at = control.updated_at.clone();
        return;
    }
    if payload.archive_backlog.dead_ranges > 0 {
        payload.archive_backlog.state = "dead_lettered".to_string();
        return;
    }
    if payload.archive_backlog.pending_ranges == 0 {
        payload.archive_backlog.state = "complete".to_string();
        return;
    }
    let uploading = payload
        .ship_scheduler
        .as_ref()
        .is_some_and(|scheduler| scheduler.in_flight_retry > 0);
    payload.archive_backlog.state = if uploading {
        "uploading"
    } else if payload.archive_backlog.ready_ranges == 0
        && payload.archive_backlog.deferred_ranges > 0
    {
        "blocked"
    } else {
        "scanning"
    }
    .to_string();
}

fn archive_repair_is_paused(default_mode: ArchiveRepairMode) -> bool {
    read_archive_repair_control().is_paused(default_mode)
}

fn archive_startup_replay_warmup_delay(
    mode: ArchiveRepairMode,
    jitter_seed: f64,
) -> Option<Duration> {
    if mode.is_paused() {
        return None;
    }
    let jitter_seed = jitter_seed.clamp(0.0, 1.0);
    let window_ms = ARCHIVE_STARTUP_REPLAY_WARMUP_MAX
        .as_millis()
        .saturating_sub(ARCHIVE_STARTUP_REPLAY_WARMUP_MIN.as_millis())
        .min(u128::from(u64::MAX)) as u64;
    Some(
        ARCHIVE_STARTUP_REPLAY_WARMUP_MIN
            + Duration::from_millis((window_ms as f64 * jitter_seed) as u64),
    )
}

/// Run the connect daemon. This function blocks until shutdown signal.
pub async fn run(config: ConnectConfig) -> Result<()> {
    // 1. Open state DB
    let projection_db_path =
        crate::state::db::resolve_db_path(config.shipper_config.db_path.as_deref())?;
    let conn = open_db(Some(&projection_db_path))?;

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
    let storage_v2 = match client
        .storage_v2_capabilities(
            &config.shipper_config.machine_name,
            Some(Duration::from_secs(5)),
        )
        .await?
    {
        Some(capabilities) if capabilities.cutover => {
            tracing::info!(
                tenant_id = %capabilities.tenant_id,
                "Runtime Host requires storage-v2 transcript shipping"
            );
            Some(std::sync::Arc::new(capabilities))
        }
        Some(capabilities) => {
            tracing::info!(
                tenant_id = %capabilities.tenant_id,
                "Runtime Host accepts storage-v2 but has not enabled cutover"
            );
            None
        }
        None => {
            tracing::info!(
                "Runtime Host does not advertise storage-v2; using the legacy ingest protocol"
            );
            None
        }
    };

    // 4. Discover providers. ACP creates run files after the daemon starts;
    // establish its engine-owned root first so the watcher includes it.
    std::fs::create_dir_all(crate::config::get_agent_dir()?.join("cursor-acp-source"))?;
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
        storage_v2,
    };

    // 6. Start file watcher before catch-up work so live changes queue immediately.
    let managed_state_dirs = managed_provider_state_dirs();
    for state_dir in &managed_state_dirs {
        std::fs::create_dir_all(state_dir)?;
    }
    let mut watcher = SessionWatcher::new(&providers, &managed_state_dirs)?;
    tracing::info!(
        "Daemon ready — watching for file changes (flush interval: {:?})",
        WATCHER_FLUSH_INTERVAL
    );

    // 6b. Prevent system sleep if configured. On macOS this prevents
    // lid-close sleep by holding a PreventUserIdleSystemSleep assertion
    // via caffeinate -s. caffeinate -w <pid> exits when the daemon PID
    // disappears, so SIGKILL/abort/launchd restart all clean up cleanly.
    let _caffeinate = if config.prevent_sleep {
        let pid = std::process::id();
        match spawn_caffeinate(pid) {
            Ok(child) => {
                tracing::info!("Sleep prevention active (caffeinate -s -w {})", pid);
                Some(child)
            }
            Err(e) => {
                tracing::warn!("Failed to start caffeinate for sleep prevention: {}", e);
                None
            }
        }
    } else {
        None
    };

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
    let mut last_managed_observations = ManagedObservationSnapshot::default();
    let mut opencode_title_refresh_tasks: JoinSet<Result<()>> = JoinSet::new();
    let mut projection_build_tasks: JoinSet<ProjectionBuildResult> = JoinSet::new();
    let mut deferred_retries = HashMap::new();
    let startup_archive_mode =
        read_archive_repair_control().normalized_mode(config.archive_repair_mode);
    let startup_archive_replay_delay =
        archive_startup_replay_warmup_delay(startup_archive_mode, rand::random::<f64>());
    maybe_start_managed_observation_scan(
        &mut managed_observation_scan_tasks,
        "startup",
        true,
        &last_managed_observations,
    );
    if let Some(delay) = startup_archive_replay_delay {
        tracing::info!(
            mode = startup_archive_mode.as_str(),
            warmup_ms = delay.as_millis() as u64,
            "Deferred startup archive replay by jittered warmup; live lanes remain active"
        );
    } else {
        tracing::info!("Startup archive replay paused by archive repair mode");
    }
    tracing::info!(
        "Startup reconciliation deferred by {:?} (max {} concurrent)",
        STARTUP_RECONCILIATION_SCAN_DELAY,
        max_in_flight
    );

    // 8. Main event loop
    let fallback_interval = Duration::from_secs(config.fallback_scan_secs.max(10));
    let failed_ship_retry_interval = Duration::from_secs(config.spool_replay_secs.max(5));
    let health_check_interval = Duration::from_secs(60);
    let prune_interval = Duration::from_secs(24 * 3600);
    let heartbeat_interval = Duration::from_secs(SERVER_HEARTBEAT_INTERVAL_SECS);

    let mut fallback_timer = tokio::time::interval(fallback_interval);
    fallback_timer.tick().await; // consume first immediate tick

    let mut failed_ship_retry_timer = tokio::time::interval(failed_ship_retry_interval);
    failed_ship_retry_timer.tick().await; // consume first immediate tick

    let mut health_timer = tokio::time::interval(health_check_interval);
    health_timer.tick().await; // consume first immediate tick

    let mut prune_timer = tokio::time::interval(prune_interval);
    prune_timer.tick().await; // consume first immediate tick

    let mut heartbeat_timer = tokio::time::interval(heartbeat_interval);
    heartbeat_timer.tick().await; // consume first immediate tick
    let mut local_status_timer =
        tokio::time::interval(Duration::from_secs(LOCAL_STATUS_INTERVAL_SECS));
    local_status_timer.tick().await; // consume first immediate tick
    let mut managed_observation_timer =
        tokio::time::interval(Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS));
    managed_observation_timer.tick().await; // startup scan already owns the first pass
    let mut managed_full_reconciliation_timer = tokio::time::interval(Duration::from_secs(
        MANAGED_FULL_RECONCILIATION_INTERVAL_SECS,
    ));
    managed_full_reconciliation_timer.tick().await; // startup scan is already full
    let mut machine_presence_timer =
        tokio::time::interval(Duration::from_secs(MACHINE_PRESENCE_INTERVAL_SECS));
    machine_presence_timer.tick().await; // consume first immediate tick
    let mut flight_sample_timer =
        tokio::time::interval(Duration::from_secs(FLIGHT_SAMPLE_INTERVAL_SECS));
    flight_sample_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    flight_sample_timer.tick().await; // consume first immediate tick

    let mut outbox_timer = tokio::time::interval(OUTBOX_DRAIN_INTERVAL);
    outbox_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    outbox_timer.tick().await; // consume first immediate tick
    let mut local_retry_timer = tokio::time::interval(LOCAL_WORK_TICK_INTERVAL);
    local_retry_timer.tick().await; // consume first immediate tick
    let startup_archive_replay_timer =
        tokio::time::sleep(startup_archive_replay_delay.unwrap_or(Duration::ZERO));
    tokio::pin!(startup_archive_replay_timer);
    let mut startup_archive_replay_pending = startup_archive_replay_delay.is_some();
    let startup_reconciliation_timer = tokio::time::sleep(STARTUP_RECONCILIATION_SCAN_DELAY);
    tokio::pin!(startup_reconciliation_timer);
    let mut startup_reconciliation_pending = !startup_archive_mode.is_paused();

    let mut offline = OfflineState::new();
    let mut last_ship_at: Option<String> = None;
    let mut last_runtime_truth_signature: Option<String> = None;
    let mut runtime_truth_bootstrapped = false;
    let mut session_snapshot_state = SessionSnapshotState::default();
    let mut last_status_projection: Option<heartbeat::StatusFileProjection> = None;
    let mut managed_reconciliation =
        heartbeat::ProjectionReconciliation::running("startup", chrono::Utc::now().to_rfc3339());
    let mut wake_gap_detector = WakeGapDetector::new();
    let mut pending_wake_reconciliation = false;
    let mut pending_full_reconciliation = false;
    let mut pending_periodic_observation = false;
    let mut projection_build_pending = false;
    let mut projection_generation = 0_u64;
    let mut last_full_reconciled_at: Option<String> = None;
    let mut last_projected_managed_observations = ManagedObservationSnapshot::default();
    let mut last_projected_managed_scan_partial = false;
    let mut last_projected_process_snapshot_complete = false;
    let mut last_unmanaged_session_bindings: Option<Vec<heartbeat::UnmanagedSessionBinding>> = None;
    let mut latest_transcript_wake_observed: HashMap<PathBuf, i64> = HashMap::new();
    let mut managed_codex_transcript_paths: HashSet<PathBuf> = HashSet::new();
    let mut outbox_collect_tasks: JoinSet<OutboxCollectResult> = JoinSet::new();
    let mut outbox_post_tasks: JoinSet<(usize, usize, u64, u64)> = JoinSet::new();
    let mut runtime_outbox_post_tasks: JoinSet<(usize, usize, u64, u64)> = JoinSet::new();
    let mut heartbeat_post_tasks: JoinSet<HeartbeatPostResult> = JoinSet::new();
    let mut machine_presence_post_tasks: JoinSet<MachinePresencePostResult> = JoinSet::new();
    let mut unmanaged_binding_refresh_tasks: JoinSet<UnmanagedBindingRefreshResult> =
        JoinSet::new();

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
    // Anonymous, machine-global Console warmth: one initialized stock Codex
    // app-server regardless of how many durable sessions exist. Failure is a
    // measured cold-path miss and never disables the control channel.
    tokio::spawn(crate::codex_exec::prewarm_codex_console_workers());
    let (transcript_wake_tx, mut transcript_wake_rx) = mpsc::unbounded_channel();
    let transcript_wake_task = spawn_transcript_wake_listener(transcript_wake_tx)?;
    loop {
        if !startup_archive_replay_pending {
            match queue_failed_shipment_retries_if_idle(
                &mut scheduler,
                &conn,
                offline.is_offline,
                PERIODIC_SPOOL_PATH_LIMIT,
                Some(adaptive_limiter.as_ref()),
                config.archive_repair_mode,
            ) {
                Ok(queued) if queued > 0 => {
                    tracing::info!(
                        queued,
                        "Queued failed-shipment retry paths after local scheduler drained"
                    );
                }
                Ok(_) => {}
                Err(e) => tracing::warn!(
                    "Failed-shipment retry error while refilling idle scheduler: {}",
                    e
                ),
            }
        }
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

            machine_presence_post_result = machine_presence_post_tasks.join_next(), if !machine_presence_post_tasks.is_empty() => {
                match machine_presence_post_result {
                    Some(Ok(result)) => {
                        match result.result {
                            Ok(true) => {
                                tracing::debug!(task_elapsed_ms = result.task_elapsed_ms, "Machine presence POST sent");
                            }
                            Ok(false) => {
                                tracing::debug!(task_elapsed_ms = result.task_elapsed_ms, "Machine presence collection disabled");
                            }
                            Err(err) => {
                                tracing::debug!("Machine presence POST failed: {}", err);
                            }
                        }
                    }
                    Some(Err(err)) => {
                        tracing::warn!("Machine presence POST task failed: {}", err);
                    }
                    None => {}
                }
            }

            unmanaged_binding_refresh_result = unmanaged_binding_refresh_tasks.join_next(), if !unmanaged_binding_refresh_tasks.is_empty() => {
                match unmanaged_binding_refresh_result {
                    Some(Ok(result)) => {
                        let stale = result.generation != projection_generation;
                        if stale {
                            tracing::debug!(
                                generation = result.generation,
                                latest_generation = projection_generation,
                                "Discarded stale unmanaged reconciliation result"
                            );
                        }
                        if !stale { match result.result {
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
                                last_projected_managed_observations = result.managed;
                                last_projected_managed_scan_partial = result.managed_scan_partial;
                                last_projected_process_snapshot_complete =
                                    result.full_reconciliation_candidate;
                                last_unmanaged_session_bindings = Some(bindings);
                                if result.full_reconciliation_candidate {
                                    last_full_reconciled_at = Some(chrono::Utc::now().to_rfc3339());
                                }
                                let input = ProjectionBuildInput {
                                    generation: projection_generation,
                                    managed_scan_partial: last_projected_managed_scan_partial,
                                    process_snapshot_complete:
                                        last_projected_process_snapshot_complete,
                                    db_path: projection_db_path.clone(),
                                    tracker: tracker.clone(),
                                    parse_tracker: parse_tracker.clone(),
                                    ship_stats: ship_stats.clone(),
                                    is_offline: offline.is_offline,
                                    last_ship_at: last_ship_at.clone(),
                                    machine_id: config.shipper_config.machine_name.clone(),
                                    managed: last_projected_managed_observations.clone(),
                                    unmanaged: last_unmanaged_session_bindings.clone().unwrap_or_default(),
                                    limiter: adaptive_limiter.snapshot(),
                                    scheduler: scheduler.snapshot(),
                                    archive_repair_mode: config.archive_repair_mode,
                                    last_full_reconciled_at: last_full_reconciled_at.clone(),
                                    session_snapshot_state: session_snapshot_state.clone(),
                                };
                                if !maybe_start_projection_build(&mut projection_build_tasks, input) {
                                    projection_build_pending = true;
                                }
                            }
                            Err(err) => {
                                projection_generation = projection_generation.saturating_add(1);
                                managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::failed("unmanaged_binding");
                                tracing::warn!(
                                    reason = result.reason,
                                    elapsed_ms = result.elapsed_ms,
                                    "Unmanaged binding refresh failed: {}",
                                    err
                                );
                            }
                        }}
                    }
                    Some(Err(err)) => {
                        projection_generation = projection_generation.saturating_add(1);
                        managed_reconciliation =
                            heartbeat::ProjectionReconciliation::failed("unmanaged_binding");
                        tracing::warn!("Unmanaged binding refresh task failed: {}", err);
                    }
                    None => {}
                }
                if unmanaged_binding_refresh_tasks.is_empty()
                    && managed_observation_scan_tasks.is_empty()
                {
                    if pending_wake_reconciliation
                        && maybe_start_managed_observation_scan(
                            &mut managed_observation_scan_tasks,
                            "wake",
                            true,
                            &last_managed_observations,
                        )
                    {
                        pending_wake_reconciliation = false;
                        managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                            "wake",
                            chrono::Utc::now().to_rfc3339(),
                        );
                    } else if pending_full_reconciliation
                        && maybe_start_managed_observation_scan(
                            &mut managed_observation_scan_tasks,
                            "full_reconciliation",
                            true,
                            &last_managed_observations,
                        )
                    {
                        pending_full_reconciliation = false;
                        managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                            "full_reconciliation",
                            chrono::Utc::now().to_rfc3339(),
                        );
                    } else if pending_periodic_observation
                        && maybe_start_managed_observation_scan(
                            &mut managed_observation_scan_tasks,
                            "periodic",
                            false,
                            &last_managed_observations,
                        )
                    {
                        pending_periodic_observation = false;
                    }
                }
            }

            managed_observation_scan_result = managed_observation_scan_tasks.join_next(), if !managed_observation_scan_tasks.is_empty() => {
                match managed_observation_scan_result {
                    Some(Ok(result)) => {
                        if result.elapsed_ms > 250 {
                            tracing::warn!(
                                reason = result.reason,
                                full_reconciliation = result.full_reconciliation,
                                process_inventory_valid = result.process_inventory_valid,
                                codex_count = result.codex_observations.len(),
                                antigravity_count = result.antigravity_observations.len(),
                                claude_count = result.claude_observations.len(),
                                opencode_count = result.opencode_observations.len(),
                                cursor_count = result.cursor_observations.len(),
                                process_inventory_ms = result.process_inventory_ms,
                                codex_elapsed_ms = result.codex_elapsed_ms,
                                antigravity_elapsed_ms = result.antigravity_elapsed_ms,
                                claude_elapsed_ms = result.claude_elapsed_ms,
                                opencode_elapsed_ms = result.opencode_elapsed_ms,
                                cursor_elapsed_ms = result.cursor_elapsed_ms,
                                retained_stale_rows = result.retained_stale_rows,
                                elapsed_ms = result.elapsed_ms,
                                "Managed observation scan was slow"
                            );
                        } else {
                            tracing::debug!(
                                reason = result.reason,
                                full_reconciliation = result.full_reconciliation,
                                process_inventory_valid = result.process_inventory_valid,
                                codex_count = result.codex_observations.len(),
                                antigravity_count = result.antigravity_observations.len(),
                                claude_count = result.claude_observations.len(),
                                opencode_count = result.opencode_observations.len(),
                                cursor_count = result.cursor_observations.len(),
                                process_inventory_ms = result.process_inventory_ms,
                                codex_elapsed_ms = result.codex_elapsed_ms,
                                antigravity_elapsed_ms = result.antigravity_elapsed_ms,
                                claude_elapsed_ms = result.claude_elapsed_ms,
                                opencode_elapsed_ms = result.opencode_elapsed_ms,
                                cursor_elapsed_ms = result.cursor_elapsed_ms,
                                retained_stale_rows = result.retained_stale_rows,
                                elapsed_ms = result.elapsed_ms,
                                "Managed observation scan completed"
                            );
                        }
                        if !result.process_inventory_valid {
                            projection_generation = projection_generation.saturating_add(1);
                            tracing::warn!(
                                reason = result.reason,
                                "Managed observation scan retained prior truth because process inventory failed"
                            );
                            managed_reconciliation = heartbeat::ProjectionReconciliation::failed(
                                result.reason,
                            );
                            if pending_wake_reconciliation {
                                if maybe_start_managed_observation_scan(
                                    &mut managed_observation_scan_tasks,
                                    "wake",
                                    true,
                                    &last_managed_observations,
                                ) {
                                    pending_wake_reconciliation = false;
                                }
                            } else if pending_full_reconciliation
                                && maybe_start_managed_observation_scan(
                                    &mut managed_observation_scan_tasks,
                                    "full_reconciliation",
                                    true,
                                    &last_managed_observations,
                                )
                            {
                                pending_full_reconciliation = false;
                            }
                            continue;
                        }
                        let next_managed_observations =
                            ManagedObservationSnapshot::from_result(&result).current_only();
                        let managed_observations_changed = !next_managed_observations
                            .projection_equivalent(&last_managed_observations);
                        last_managed_observations = next_managed_observations;
                        let managed_scan_partial = result.retained_stale_rows > 0;
                        refresh_managed_codex_transcript_paths(
                            &mut managed_codex_transcript_paths,
                            &result.codex_observations,
                        );
                        pump_ready_local_work(
                            &mut scheduler,
                            &mut in_flight,
                            &task_context,
                            &mut deferred_retries,
                            offline.is_offline,
                        );
                        let managed_process_pids = managed_process_pids_from_observations(
                            &result.codex_observations,
                            &result.claude_observations,
                            &result.opencode_observations,
                            &result.cursor_observations,
                        );
                        let should_refresh_unmanaged =
                            result.full_reconciliation || managed_observations_changed;
                        let paired_generation = projection_generation.saturating_add(1);
                        let paired_refresh_started = should_refresh_unmanaged
                            && maybe_start_unmanaged_binding_refresh(
                                &mut unmanaged_binding_refresh_tasks,
                                config.shipper_config.db_path.clone(),
                                config.shipper_config.machine_name.clone(),
                                managed_process_pids.clone(),
                                result.process_inventory.clone(),
                                result.reason,
                                paired_generation,
                                last_managed_observations.clone(),
                                managed_scan_partial,
                                result.full_reconciliation && result.retained_stale_rows == 0,
                            );
                        if paired_refresh_started {
                            projection_generation = paired_generation;
                        } else if result.full_reconciliation {
                            if result.reason == "wake" {
                                pending_wake_reconciliation = true;
                            } else {
                                pending_full_reconciliation = true;
                            }
                        } else if managed_observations_changed {
                            defer_managed_pair_retry(
                                &mut projection_generation,
                                &mut pending_full_reconciliation,
                            );
                        }
                        maybe_start_opencode_title_refresh(
                            &mut opencode_title_refresh_tasks,
                            config.shipper_config.db_path.clone(),
                            result.opencode_observations.clone(),
                        );
                        if pending_wake_reconciliation
                            && unmanaged_binding_refresh_tasks.is_empty()
                            && maybe_start_managed_observation_scan(
                                &mut managed_observation_scan_tasks,
                                "wake",
                                true,
                                &last_managed_observations,
                            )
                        {
                            pending_wake_reconciliation = false;
                            managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                                "wake",
                                chrono::Utc::now().to_rfc3339(),
                            );
                        } else if pending_full_reconciliation
                            && unmanaged_binding_refresh_tasks.is_empty()
                            && maybe_start_managed_observation_scan(
                                &mut managed_observation_scan_tasks,
                                "full_reconciliation",
                                true,
                                &last_managed_observations,
                            )
                        {
                            pending_full_reconciliation = false;
                            managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                                "full_reconciliation",
                                chrono::Utc::now().to_rfc3339(),
                            );
                        } else if pending_periodic_observation
                            && maybe_start_managed_observation_scan(
                                &mut managed_observation_scan_tasks,
                                "periodic",
                                false,
                                &last_managed_observations,
                            )
                        {
                            pending_periodic_observation = false;
                        }
                    }
                    Some(Err(err)) => {
                        projection_generation = projection_generation.saturating_add(1);
                        tracing::warn!("Managed observation scan task failed: {}", err);
                        managed_reconciliation = heartbeat::ProjectionReconciliation::failed(
                            managed_reconciliation
                                .reason
                                .clone()
                                .unwrap_or_else(|| "managed_observation".to_string()),
                        );
                    }
                    None => {}
                }
            }

            opencode_title_refresh_result = opencode_title_refresh_tasks.join_next(), if !opencode_title_refresh_tasks.is_empty() => {
                match opencode_title_refresh_result {
                    Some(Ok(Ok(()))) | None => {}
                    Some(Ok(Err(err))) => tracing::warn!(error = %err, "OpenCode title refresh failed"),
                    Some(Err(err)) => tracing::warn!(error = %err, "OpenCode title refresh task failed"),
                }
            }

            projection_build_result = projection_build_tasks.join_next(), if !projection_build_tasks.is_empty() => {
                match projection_build_result {
                    Some(Ok(result)) => {
                        let is_current = result.generation == projection_generation;
                        match result.result {
                        Ok((projection, next_snapshot_state)) => {
                            if result.elapsed_ms > 50 {
                                tracing::warn!(
                                    projection_elapsed_ms = result.elapsed_ms,
                                    "Local status projection exceeded background budget"
                                );
                            }
                            if !is_current {
                                tracing::debug!(
                                    generation = result.generation,
                                    latest_generation = projection_generation,
                                    "Discarded stale local status projection"
                                );
                            } else {
                            session_snapshot_state = next_snapshot_state;
                            if result.managed_scan_partial {
                                managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::failed("provider_state_partial");
                            } else if managed_observation_scan_tasks.is_empty()
                                && unmanaged_binding_refresh_tasks.is_empty()
                                && !pending_wake_reconciliation
                                && !pending_full_reconciliation
                            {
                                managed_reconciliation = heartbeat::ProjectionReconciliation::idle();
                            }
                            heartbeat::write_status_file(
                                &projection,
                                serde_json::to_value(control_channel_status.snapshot()).ok(),
                                &managed_reconciliation,
                                &status_path,
                            );
                            let payload = projection.payload.clone();
                            last_status_projection = Some(projection);
                            let signature = runtime_truth_signature(&payload);
                            if !runtime_truth_bootstrapped {
                                last_runtime_truth_signature = Some(signature);
                                runtime_truth_bootstrapped = true;
                            } else if !offline.is_offline
                                && last_runtime_truth_signature.as_deref() != Some(signature.as_str())
                            {
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
                                }
                            }
                            }
                        }
                        Err(error) => {
                            tracing::warn!(error = %error, "Local status projection build failed");
                            if is_current {
                                managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::failed("projection_build");
                            }
                        }
                    }
                    },
                    Some(Err(error)) => {
                        tracing::warn!(error = %error, "Local status projection task failed");
                        if !projection_build_pending {
                            managed_reconciliation =
                                heartbeat::ProjectionReconciliation::failed("projection_build");
                        }
                    }
                    None => {}
                }

                if projection_build_pending {
                    projection_build_pending = false;
                    let input = ProjectionBuildInput {
                        generation: projection_generation,
                        managed_scan_partial: last_projected_managed_scan_partial,
                        process_snapshot_complete: last_projected_process_snapshot_complete,
                        db_path: projection_db_path.clone(),
                        tracker: tracker.clone(),
                        parse_tracker: parse_tracker.clone(),
                        ship_stats: ship_stats.clone(),
                        is_offline: offline.is_offline,
                        last_ship_at: last_ship_at.clone(),
                        machine_id: config.shipper_config.machine_name.clone(),
                        managed: last_projected_managed_observations.clone(),
                        unmanaged: last_unmanaged_session_bindings.clone().unwrap_or_default(),
                        limiter: adaptive_limiter.snapshot(),
                        scheduler: scheduler.snapshot(),
                        archive_repair_mode: config.archive_repair_mode,
                        last_full_reconciled_at: last_full_reconciled_at.clone(),
                        session_snapshot_state: session_snapshot_state.clone(),
                    };
                    let _ = maybe_start_projection_build(&mut projection_build_tasks, input);
                }
            }

            _ = &mut startup_archive_replay_timer, if startup_archive_replay_pending && !offline.is_offline => {
                startup_archive_replay_pending = false;
                match queue_failed_shipment_retry_paths(
                    &mut scheduler,
                    &conn,
                    INITIAL_SPOOL_PATH_LIMIT,
                    Some(adaptive_limiter.as_ref()),
                    config.archive_repair_mode,
                ) {
                    Ok(queued) => {
                        tracing::info!(
                            queued,
                            "Queued startup archive replay after jittered warmup"
                        );
                    }
                    Err(e) => tracing::warn!(
                        "Failed-shipment retry error after startup archive warmup: {}",
                        e
                    ),
                }
            }

            _ = &mut startup_reconciliation_timer, if startup_reconciliation_pending && !offline.is_offline => {
                startup_reconciliation_pending = false;
                maybe_start_reconciliation_scan(
                    &mut discovery_tasks,
                    &providers,
                    &scheduler,
                    &deferred_retries,
                    config.archive_repair_mode,
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

            // Live transcript lane (primary path): provider file appends enqueue
            // WorkPriority::Live. Managed wake signals can pre-empt the small
            // filesystem coalescing window.
            Some(first_event) = watcher.next_event() => {
                let managed_state_changes = handle_live_transcript_file_events(
                    &mut watcher,
                    first_event,
                    &providers,
                    &managed_state_dirs,
                    &mut transcript_wake_rx,
                    &mut scheduler,
                    &mut latest_transcript_wake_observed,
                    &managed_codex_transcript_paths,
                    &mut deferred_retries,
                    &mut in_flight,
                    &task_context,
                    offline.is_offline,
                ).await;
                if !managed_state_changes.is_empty() {
                    let requires_discovery = managed_state_changes_require_full_reconciliation(
                        &last_managed_observations,
                        &managed_state_changes,
                    );
                    if requires_discovery {
                        // Invalidate any older managed/unmanaged pair before it can
                        // publish the new managed child as Shadow ownership.
                        projection_generation = projection_generation.saturating_add(1);
                        if maybe_start_managed_observation_scan(
                            &mut managed_observation_scan_tasks,
                            "managed_state_discovery",
                            true,
                            &last_managed_observations,
                        ) {
                            managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                                "managed_state_discovery",
                                chrono::Utc::now().to_rfc3339(),
                            );
                        } else {
                            pending_full_reconciliation = true;
                        }
                    } else {
                        tracing::debug!(
                            event_count = managed_state_changes.len(),
                            "Known managed state changed; bounded periodic observation owns refresh"
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
                    config.archive_repair_mode,
                    "reconciliation scan",
                );
            }

            // Retry/archive lane: replay failed or incomplete shipments from
            // the spool. This timer is never the primary live transcript lane.
            _ = failed_ship_retry_timer.tick(), if !offline.is_offline && !startup_archive_replay_pending => {
                match queue_failed_shipment_retry_paths(
                    &mut scheduler,
                    &conn,
                    PERIODIC_SPOOL_PATH_LIMIT,
                    Some(adaptive_limiter.as_ref()),
                    config.archive_repair_mode,
                ) {
                    Ok(queued) => {
                        if queued > 0 {
                            tracing::debug!("Queued {} failed-shipment retry paths from spool", queued);
                        }
                    }
                    Err(e) => tracing::warn!("Failed-shipment retry error: {}", e),
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
                if let Some(gap) = wake_gap_detector.observe(SystemTime::now(), Instant::now()) {
                    tracing::info!(wake_gap_ms = gap.as_millis() as u64, "Detected system wake gap");
                    if maybe_start_managed_observation_scan(
                        &mut managed_observation_scan_tasks,
                        "wake",
                        true,
                        &last_managed_observations,
                    ) {
                        managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                            "wake",
                            chrono::Utc::now().to_rfc3339(),
                        );
                    } else {
                        pending_wake_reconciliation = true;
                        managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                            "wake",
                            chrono::Utc::now().to_rfc3339(),
                        );
                    }
                }
                if let Some(projection) = last_status_projection.as_ref() {
                    heartbeat::write_status_file(
                        projection,
                        serde_json::to_value(control_channel_status.snapshot()).ok(),
                        &managed_reconciliation,
                        &status_path,
                    );
                } else {
                    heartbeat::refresh_existing_status_pulse(
                        &managed_reconciliation,
                        &status_path,
                    );
                }
            }

            _ = managed_full_reconciliation_timer.tick() => {
                if maybe_start_managed_observation_scan(
                    &mut managed_observation_scan_tasks,
                    "full_reconciliation",
                    true,
                    &last_managed_observations,
                ) {
                    managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                        "full_reconciliation",
                        chrono::Utc::now().to_rfc3339(),
                    );
                } else {
                    pending_full_reconciliation = true;
                    managed_reconciliation = heartbeat::ProjectionReconciliation::running(
                        "full_reconciliation",
                        chrono::Utc::now().to_rfc3339(),
                    );
                }
            }

            _ = managed_observation_timer.tick() => {
                if maybe_start_managed_observation_scan(
                    &mut managed_observation_scan_tasks,
                    "periodic",
                    false,
                    &last_managed_observations,
                ) {
                    pending_periodic_observation = false;
                } else {
                    pending_periodic_observation = true;
                }
            }

            _ = machine_presence_timer.tick() => {
                if !offline.is_offline {
                    if machine_presence_post_tasks.is_empty() {
                        spawn_machine_presence_post(
                            &mut machine_presence_post_tasks,
                            client.clone(),
                        );
                    } else {
                        tracing::debug!("Skipping machine presence POST while previous POST is still in flight");
                    }
                }
            }

            // Periodic server heartbeat
            _ = heartbeat_timer.tick() => {
                if let Some(projection) = last_status_projection.as_ref() {
                    heartbeat::write_status_file(
                        projection,
                        serde_json::to_value(control_channel_status.snapshot()).ok(),
                        &managed_reconciliation,
                        &status_path,
                    );
                    if !offline.is_offline {
                        runtime_truth_bootstrapped = true;
                        if heartbeat_post_tasks.is_empty() {
                            let payload = projection.payload.clone();
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
                } else {
                    tracing::debug!("Skipping periodic heartbeat until the startup managed observation scan completes");
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
    crate::codex_exec::shutdown_codex_console_worker_pool().await;
    tracing::info!("Daemon shutdown complete");
    Ok(())
}

fn maybe_start_projection_build(
    tasks: &mut JoinSet<ProjectionBuildResult>,
    input: ProjectionBuildInput,
) -> bool {
    if !tasks.is_empty() {
        return false;
    }
    tasks.spawn_blocking(move || {
        let started = Instant::now();
        let ProjectionBuildInput {
            generation,
            managed_scan_partial,
            process_snapshot_complete,
            db_path,
            tracker,
            parse_tracker,
            ship_stats,
            is_offline,
            last_ship_at,
            machine_id,
            managed,
            unmanaged,
            limiter,
            scheduler,
            archive_repair_mode,
            last_full_reconciled_at,
            mut session_snapshot_state,
        } = input;
        let result = crate::state::db::open_connection(&db_path)
            .map_err(|error| error.to_string())
            .map(|conn| {
                let mut projection = build_local_status_projection(
                    &conn,
                    &tracker,
                    &parse_tracker,
                    &ship_stats,
                    is_offline,
                    &last_ship_at,
                    &machine_id,
                    &managed.codex,
                    &managed.antigravity,
                    &managed.claude,
                    &managed.opencode,
                    &managed.cursor,
                    &unmanaged,
                    process_snapshot_complete,
                    Some(limiter),
                    Some(scheduler),
                    archive_repair_mode,
                    &mut session_snapshot_state,
                );
                projection.set_last_reconciled_at(last_full_reconciled_at);
                (projection, session_snapshot_state)
            });
        ProjectionBuildResult {
            generation,
            managed_scan_partial,
            result,
            elapsed_ms: started.elapsed().as_millis() as u64,
        }
    });
    true
}

#[allow(clippy::too_many_arguments)]
fn build_local_status_projection(
    conn: &rusqlite::Connection,
    tracker: &ConsecutiveErrorTracker,
    parse_tracker: &RecentIssueTracker,
    ship_stats: &RecentShipStatsTracker,
    is_offline: bool,
    last_ship_at: &Option<String>,
    machine_id: &str,
    observations: &[managed_bridge_scan::CodexBridgeObservation],
    antigravity_observations: &[managed_antigravity_scan::AntigravityHookObservation],
    claude_observations: &[managed_claude_scan::ClaudeChannelObservation],
    opencode_observations: &[managed_opencode_scan::OpenCodeServerObservation],
    cursor_observations: &[managed_cursor_helm_scan::CursorHelmObservation],
    unmanaged_session_bindings: &[heartbeat::UnmanagedSessionBinding],
    process_snapshot_complete: bool,
    limiter_snapshot: Option<crate::scheduler::LimiterSnapshot>,
    scheduler_snapshot: Option<crate::scheduler::SchedulerSnapshot>,
    archive_repair_mode: ArchiveRepairMode,
    session_snapshot_state: &mut SessionSnapshotState,
) -> heartbeat::StatusFileProjection {
    let spool = Spool::new(conn);
    let stats = heartbeat::HeartbeatStats {
        conn,
        spool: &spool,
        tracker,
        parse_tracker,
        ship_stats,
        is_offline,
        last_ship_at: last_ship_at.clone(),
    };
    let mut payload = heartbeat::HeartbeatPayload::build(&stats);
    let archive_control = read_archive_repair_control();
    payload.adaptive_backlog_limiter = limiter_snapshot;
    payload.ship_scheduler = scheduler_snapshot;
    apply_archive_repair_control(&mut payload, &archive_control, archive_repair_mode);
    let now = chrono::Utc::now();
    let phase_overlay = heartbeat::load_managed_phase_overlay(conn);
    payload.managed_sessions =
        heartbeat::leases_from_observations(&phase_overlay, machine_id, observations, now);
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_claude_channel_observations(
            &phase_overlay,
            machine_id,
            claude_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_opencode_server_observations(
            &phase_overlay,
            machine_id,
            opencode_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_cursor_helm_observations(
            &phase_overlay,
            machine_id,
            cursor_observations,
            now,
        ));
    payload.managed_sessions.sort_by(|a, b| {
        a.provider
            .cmp(&b.provider)
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    payload.unmanaged_session_bindings =
        heartbeat::filter_unmanaged_bindings_owned_by_managed_observations(
            unmanaged_session_bindings.to_vec(),
            observations,
            claude_observations,
            opencode_observations,
            cursor_observations,
        );
    // Compute the fresh activity ledger once and feed the raw rows into the
    // typed evidence envelope. Activity facts remain independent of control
    // leases and of the resolved presentation projection below.
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
    payload.machine_evidence = Some(heartbeat::machine_evidence_from_observations(
        observations,
        antigravity_observations,
        claude_observations,
        opencode_observations,
        cursor_observations,
        unmanaged_session_bindings,
        &phase_ledger,
        process_snapshot_complete,
        now,
    ));
    payload.sessions = heartbeat::resolved_sessions_from_observations(
        &payload.managed_sessions,
        &payload.unmanaged_session_bindings,
        observations,
        claude_observations,
        opencode_observations,
        cursor_observations,
    );
    heartbeat::apply_machine_boot_identity(&mut payload.sessions);
    heartbeat::apply_local_titles(conn, &mut payload.sessions);
    session_snapshot_state.annotate(&mut payload);
    heartbeat::build_status_file_projection(payload, &stats, phase_ledger, ledger_status)
}

fn observe_active_opencode_titles(
    conn: &rusqlite::Connection,
    observations: &[managed_opencode_scan::OpenCodeServerObservation],
) {
    let Some(home) = std::env::var_os("HOME") else {
        return;
    };
    let db_path = PathBuf::from(home)
        .join(".local")
        .join("share")
        .join("opencode")
        .join("opencode.db");
    if !db_path.is_file() {
        return;
    }
    for observation in observations {
        if matches!(
            crate::state::session_title::get(conn, &observation.session_id),
            Ok(Some(_))
        ) {
            continue;
        }
        let Ok(parsed) =
            crate::opencode_db::parse_opencode_session(&db_path, &observation.provider_session_id)
        else {
            continue;
        };
        if let Err(error) = crate::state::session_title::observe_parse_result(
            conn,
            &observation.session_id,
            &parsed,
        ) {
            tracing::warn!(
                session_id = observation.session_id,
                error = %error,
                "Unable to persist active OpenCode prompt title"
            );
        }
    }
}

fn maybe_start_opencode_title_refresh(
    tasks: &mut JoinSet<Result<()>>,
    db_path: Option<PathBuf>,
    observations: Vec<managed_opencode_scan::OpenCodeServerObservation>,
) {
    if !tasks.is_empty() || observations.is_empty() {
        return;
    }
    tasks.spawn_blocking(move || {
        let db_path = crate::state::db::resolve_db_path(db_path.as_deref())?;
        let conn = crate::state::db::open_connection(&db_path)?;
        observe_active_opencode_titles(&conn, &observations);
        Ok(())
    });
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
    payload
        .sessions_digest
        .clone()
        .unwrap_or_else(|| heartbeat::session_snapshot_digest(payload))
}

#[derive(Clone, Default)]
struct SessionSnapshotState {
    last_digest: Option<String>,
    sequence: u64,
}

impl SessionSnapshotState {
    fn annotate(&mut self, payload: &mut heartbeat::HeartbeatPayload) {
        let digest = heartbeat::session_snapshot_digest(payload);
        if self.last_digest.as_deref() != Some(digest.as_str()) {
            self.sequence = self.sequence.saturating_add(1);
            self.last_digest = Some(digest.clone());
        }
        payload.sessions_digest = Some(digest);
        payload.sessions_sequence = Some(self.sequence);
    }
}

fn local_retry_delay(priority: WorkPriority) -> Duration {
    if priority == WorkPriority::Live {
        LIVE_LOCAL_RETRY_DELAY
    } else {
        Duration::from_secs(LOCAL_RETRY_DELAY_SECS)
    }
}

fn storage_v2_backpressure_retry_delay(priority: WorkPriority, retry_after: Duration) -> Duration {
    if priority == WorkPriority::Live {
        retry_after.min(Duration::from_secs(1))
    } else {
        retry_after
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
    let min_delay = Duration::from_secs(LOCAL_RETRY_DELAY_SECS);
    let delay = (retry_at - chrono::Utc::now())
        .to_std()
        .unwrap_or(Duration::ZERO);
    Some(delay.max(min_delay))
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
    archive_repair_mode: ArchiveRepairMode,
    reason: &'static str,
) {
    if archive_repair_is_paused(archive_repair_mode) {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because archive repair is paused"
        );
        return;
    }

    if !discovery_tasks.is_empty() {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because discovery is still running"
        );
        return;
    }

    if scheduler.has_pending_priority(WorkPriority::Scan) {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because one is already pending"
        );
        return;
    }

    let live_retry_pending = deferred_retries
        .values()
        .any(|retry| retry.priority == WorkPriority::Live);
    if scheduler.has_pending_priority(WorkPriority::Live) || live_retry_pending {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan while live local work is pending"
        );
        return;
    }

    tracing::debug!(reason, "Starting reconciliation scan in background");
    start_discovery_task(discovery_tasks, providers, WorkPriority::Scan, reason);
}

#[allow(clippy::too_many_arguments)]
async fn handle_live_transcript_file_events(
    watcher: &mut SessionWatcher,
    first_event: WatcherEvent,
    providers: &[ProviderConfig],
    managed_state_dirs: &[PathBuf],
    transcript_wake_rx: &mut mpsc::UnboundedReceiver<TranscriptWakeSignal>,
    scheduler: &mut PathScheduler,
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    managed_codex_transcript_paths: &HashSet<PathBuf>,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
    in_flight: &mut JoinSet<PathTaskResult>,
    task_context: &PathTaskContext,
    offline: bool,
) -> Vec<PathBuf> {
    // Keep the coalescing wait cancellable by transcript wakes. The wake socket
    // is the managed-session completion lane, so it should not sit behind
    // filesystem batching.
    let flush = tokio::time::sleep(WATCHER_FLUSH_INTERVAL);
    tokio::pin!(flush);
    loop {
        tokio::select! {
            biased;
            Some(signal) = transcript_wake_rx.recv() => {
                if let Some(path) = enqueue_transcript_wake_signal(
                    scheduler,
                    latest_transcript_wake_observed,
                    signal,
                ) {
                    deferred_retries.remove(&path);
                    pump_ready_local_work(
                        scheduler,
                        in_flight,
                        task_context,
                        deferred_retries,
                        offline,
                    );
                }
            }
            _ = &mut flush => {
                break;
            }
        }
    }

    let events = watcher.collect_ready_batch(first_event);
    let (managed_state_changes, transcript_events) =
        partition_managed_state_events(events, managed_state_dirs);
    for event in transcript_events {
        let Some((session_path, provider)) =
            discovery::session_path_for_watcher_event(&event.path, providers)
        else {
            tracing::debug!(
                "Skipping file outside known providers: {}",
                event.path.display()
            );
            continue;
        };

        let session_event = WatcherEvent {
            path: session_path,
            observed_at_ms: event.observed_at_ms,
            latest_observed_at_ms: event.latest_observed_at_ms,
        };
        if should_defer_fsevent_for_managed_wake(
            latest_transcript_wake_observed,
            managed_codex_transcript_paths,
            &session_event,
            provider,
        ) {
            let observation = ObservationTrace {
                source: "fsevent",
                observed_at_ms: session_event.observed_at_ms,
                latest_observed_at_ms: Some(
                    session_event
                        .latest_observed_at_ms
                        .max(session_event.observed_at_ms),
                ),
                wake_received_at_ms: None,
                enqueued_at_ms: now_ms(),
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            };
            deferred_retries.insert(
                session_event.path.clone(),
                DeferredRetry {
                    due_at: Instant::now() + MANAGED_WAKE_FSEVENT_FALLBACK_DELAY,
                    provider,
                    priority: WorkPriority::Live,
                    observation,
                },
            );
            tracing::debug!(
                provider,
                path = %session_event.path.display(),
                observed_at_ms = session_event.observed_at_ms,
                latest_observed_at_ms = session_event.latest_observed_at_ms,
                delay_ms = MANAGED_WAKE_FSEVENT_FALLBACK_DELAY.as_millis(),
                "Deferring filesystem live ship because managed wake socket owns this turn"
            );
            continue;
        }

        scheduler.enqueue_observed_window(
            session_event.path,
            provider,
            WorkPriority::Live,
            "fsevent",
            session_event.observed_at_ms,
            session_event.latest_observed_at_ms,
        );
    }
    managed_state_changes
}

fn partition_managed_state_events(
    events: Vec<WatcherEvent>,
    managed_state_dirs: &[PathBuf],
) -> (Vec<PathBuf>, Vec<WatcherEvent>) {
    let mut managed = Vec::new();
    let mut transcripts = Vec::new();
    for event in events {
        if managed_state_dirs
            .iter()
            .any(|state_dir| event.path.starts_with(state_dir))
        {
            managed.push(event.path);
        } else {
            transcripts.push(event);
        }
    }
    (managed, transcripts)
}

fn managed_state_changes_require_full_reconciliation(
    observations: &ManagedObservationSnapshot,
    paths: &[PathBuf],
) -> bool {
    paths
        .iter()
        .any(|path| !observations.contains_state_file(path))
}

fn maybe_start_unmanaged_binding_refresh(
    refresh_tasks: &mut JoinSet<UnmanagedBindingRefreshResult>,
    db_path: Option<PathBuf>,
    machine_id: String,
    excluded_managed_pids: HashSet<u32>,
    process_inventory: Vec<unmanaged_bindings::ProcessInfo>,
    reason: &'static str,
    generation: u64,
    managed: ManagedObservationSnapshot,
    managed_scan_partial: bool,
    full_reconciliation_candidate: bool,
) -> bool {
    if !refresh_tasks.is_empty() {
        return false;
    }

    refresh_tasks.spawn_blocking(move || {
        let started = Instant::now();
        let result = open_db(db_path.as_deref())
            .map_err(|err| err.to_string())
            .and_then(|conn| {
                unmanaged_bindings::collect_unmanaged_session_bindings_with_process_inventory(
                    &conn,
                    &machine_id,
                    chrono::Utc::now(),
                    &excluded_managed_pids,
                    process_inventory,
                )
            });
        UnmanagedBindingRefreshResult {
            generation,
            reason,
            full_reconciliation_candidate,
            managed,
            managed_scan_partial,
            result,
            elapsed_ms: started.elapsed().as_millis() as u64,
        }
    });
    true
}

fn managed_process_pids_from_observations(
    codex: &[managed_bridge_scan::CodexBridgeObservation],
    claude: &[managed_claude_scan::ClaudeChannelObservation],
    opencode: &[managed_opencode_scan::OpenCodeServerObservation],
    cursor: &[managed_cursor_helm_scan::CursorHelmObservation],
) -> HashSet<u32> {
    let mut pids = HashSet::new();
    for observation in codex {
        if observation.bridge_alive {
            pids.insert(observation.bridge_pid);
        }
        if observation.app_server_alive {
            pids.extend(observation.app_server_pid);
        }
    }
    for observation in claude {
        if observation.claude_alive {
            pids.extend(observation.claude_pid);
        }
        if observation.bridge_alive {
            pids.extend(observation.bridge_pid);
        }
    }
    for observation in opencode {
        if observation.server_alive {
            pids.extend(observation.pid);
        }
    }
    for observation in cursor {
        if observation.live {
            pids.extend(observation.launcher_pid);
            pids.extend(observation.cursor_pid);
        }
    }
    pids
}

fn maybe_start_managed_observation_scan(
    scan_tasks: &mut JoinSet<ManagedObservationScanResult>,
    reason: &'static str,
    full_reconciliation: bool,
    previous: &ManagedObservationSnapshot,
) -> bool {
    if !scan_tasks.is_empty() {
        return false;
    }

    let previous = previous.clone();
    scan_tasks.spawn_blocking(move || {
        let previous = if full_reconciliation {
            previous
        } else {
            previous.current_only()
        };
        let started = Instant::now();
        let process_started = Instant::now();
        let process_inventory = crate::process_identity::try_collect_process_facts_by_pid();
        let process_inventory_valid = process_inventory.is_some();
        let process_facts = process_inventory.unwrap_or_default();
        let unmanaged_process_inventory = process_facts
            .values()
            .filter_map(|fact| {
                Some(unmanaged_bindings::ProcessInfo {
                    pid: fact.pid,
                    start_time: fact.start_time?,
                    start_time_key: fact.lstart.clone(),
                    command: fact.command.clone(),
                })
            })
            .collect();
        let process_inventory_ms = process_started.elapsed().as_millis() as u64;
        let codex_started = Instant::now();
        let mut codex_observations = if full_reconciliation {
            managed_bridge_scan::default_codex_bridge_state_dir()
                .map(|state_dir| {
                    managed_bridge_scan::collect_observations_from(&state_dir, &process_facts)
                })
                .unwrap_or_default()
        } else {
            let paths = previous
                .codex
                .iter()
                .map(|row| row.state_file.clone())
                .collect::<Vec<_>>();
            managed_bridge_scan::collect_observations_from_paths(&paths, &process_facts)
        };
        let codex_elapsed_ms = codex_started.elapsed().as_millis() as u64;

        let antigravity_started = Instant::now();
        let mut antigravity_observations = if full_reconciliation {
            managed_antigravity_scan::default_antigravity_state_dir()
                .map(|state_dir| managed_antigravity_scan::collect_observations_from(&state_dir))
                .unwrap_or_default()
        } else {
            let paths = previous
                .antigravity
                .iter()
                .map(|row| row.state_file.clone())
                .collect::<Vec<_>>();
            managed_antigravity_scan::collect_observations_from_paths(&paths)
        };
        let retained_antigravity = retain_existing_observations(
            &mut antigravity_observations,
            &previous.antigravity,
            |observation| &observation.state_file,
        );
        let antigravity_elapsed_ms = antigravity_started.elapsed().as_millis() as u64;

        let claude_started = Instant::now();
        let mut claude_observations = if full_reconciliation {
            managed_claude_scan::default_claude_channel_state_dir()
                .map(|state_dir| {
                    managed_claude_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    )
                })
                .unwrap_or_default()
        } else {
            let paths = previous
                .claude
                .iter()
                .map(|row| row.state_file.clone())
                .collect::<Vec<_>>();
            managed_claude_scan::collect_observations_from_paths(&paths, &process_facts)
        };
        let retained_codex =
            retain_existing_observations(&mut codex_observations, &previous.codex, |observation| {
                &observation.state_file
            });
        let retained_claude = retain_existing_observations(
            &mut claude_observations,
            &previous.claude,
            |observation| &observation.state_file,
        );
        let claude_elapsed_ms = claude_started.elapsed().as_millis() as u64;

        let opencode_started = Instant::now();
        let mut opencode_observations = if full_reconciliation {
            managed_opencode_scan::default_opencode_server_state_dir()
                .map(|state_dir| {
                    managed_opencode_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    )
                })
                .unwrap_or_default()
        } else {
            let paths = previous
                .opencode
                .iter()
                .map(|row| row.state_file.clone())
                .collect::<Vec<_>>();
            managed_opencode_scan::collect_observations_from_paths(&paths, &process_facts)
        };
        let opencode_elapsed_ms = opencode_started.elapsed().as_millis() as u64;

        let cursor_started = Instant::now();
        let mut cursor_observations = if full_reconciliation {
            managed_cursor_helm_scan::default_cursor_helm_state_dir()
                .map(|state_dir| {
                    managed_cursor_helm_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    )
                })
                .unwrap_or_default()
        } else {
            let paths = previous
                .cursor
                .iter()
                .map(|row| row.state_file.clone())
                .collect::<Vec<_>>();
            managed_cursor_helm_scan::collect_observations_from_paths(&paths, &process_facts)
        };
        let retained_opencode = retain_existing_observations(
            &mut opencode_observations,
            &previous.opencode,
            |observation| &observation.state_file,
        );
        let retained_cursor = retain_existing_observations(
            &mut cursor_observations,
            &previous.cursor,
            |observation| &observation.state_file,
        );
        let cursor_elapsed_ms = cursor_started.elapsed().as_millis() as u64;
        ManagedObservationScanResult {
            reason,
            full_reconciliation,
            process_inventory_valid,
            process_inventory: unmanaged_process_inventory,
            codex_observations,
            antigravity_observations,
            claude_observations,
            opencode_observations,
            cursor_observations,
            process_inventory_ms,
            codex_elapsed_ms,
            antigravity_elapsed_ms,
            claude_elapsed_ms,
            opencode_elapsed_ms,
            cursor_elapsed_ms,
            retained_stale_rows: retained_codex.len()
                + retained_antigravity.len()
                + retained_claude.len()
                + retained_opencode.len()
                + retained_cursor.len(),
            elapsed_ms: started.elapsed().as_millis() as u64,
        }
    });
    true
}

fn retain_existing_observations<T: Clone>(
    current: &mut Vec<T>,
    previous: &[T],
    state_file: impl Fn(&T) -> &Path,
) -> HashSet<PathBuf> {
    let current_paths = current
        .iter()
        .map(|observation| state_file(observation).to_path_buf())
        .collect::<HashSet<_>>();
    let retained = previous
        .iter()
        .filter_map(|observation| {
            let path = state_file(observation);
            (path.exists() && !current_paths.contains(path)).then(|| observation.clone())
        })
        .collect::<Vec<_>>();
    let retained_paths = retained
        .iter()
        .map(|observation| state_file(observation).to_path_buf())
        .collect();
    current.extend(retained);
    retained_paths
}

fn defer_managed_pair_retry(projection_generation: &mut u64, pending_full: &mut bool) {
    // The in-flight unmanaged refresh was paired with older managed truth.
    // Invalidate it now and retry both halves together once that lane is idle.
    *projection_generation = projection_generation.saturating_add(1);
    *pending_full = true;
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

fn spawn_machine_presence_post(
    tasks: &mut JoinSet<MachinePresencePostResult>,
    client: ShipperClient,
) {
    tasks.spawn_local(async move {
        let task_started = Instant::now();
        let result = crate::machine_presence::send_machine_presence_if_enabled(&client)
            .await
            .map_err(|err| err.to_string());
        MachinePresencePostResult {
            result,
            task_elapsed_ms: task_started.elapsed().as_millis() as u64,
        }
    });
}

fn queue_failed_shipment_retry_paths(
    scheduler: &mut PathScheduler,
    conn: &rusqlite::Connection,
    limit: usize,
    limiter: Option<&AdaptiveLimiter>,
    archive_repair_mode: ArchiveRepairMode,
) -> Result<usize> {
    let spool = Spool::new(conn);
    let cleaned = spool.cleanup()?;
    if cleaned > 0 {
        tracing::info!("Cleaned {} old spool entries", cleaned);
    }

    let control = read_archive_repair_control();
    if control.is_paused(archive_repair_mode) {
        tracing::debug!("Archive replay paused by local control file");
        return Ok(0);
    }

    let pressure_allows_huge = limiter.map_or(true, AdaptiveLimiter::huge_range_eligible);
    let include_huge = control.includes_huge() && pressure_allows_huge;
    if control.includes_huge() && !pressure_allows_huge {
        tracing::debug!("Skipping huge archive replay paths while host pressure is above target");
    }
    let clipped = spool.clip_archive_backpressure_deferrals(ARCHIVE_BACKPRESSURE_MAX_DEFER)?;
    if clipped > 0 {
        tracing::info!(
            clipped,
            max_defer_ms = ARCHIVE_BACKPRESSURE_MAX_DEFER.as_millis() as u64,
            "Clipped stale archive backpressure retry clocks"
        );
    }

    let mut queued = 0usize;
    for pending in spool.pending_paths_budgeted(
        limit,
        control.tick_bytes(archive_repair_mode),
        include_huge,
    )? {
        let Some(provider) = provider_name_to_static(&pending.provider) else {
            tracing::warn!(
                "Skipping pending spool path with unknown provider {}: {}",
                pending.provider,
                pending.file_path
            );
            continue;
        };
        scheduler.enqueue_observed_with_estimated_bytes(
            PathBuf::from(pending.file_path),
            provider,
            WorkPriority::Retry,
            FAILED_SHIPMENT_RETRY_OBSERVATION_SOURCE,
            now_ms(),
            Some(pending.pending_bytes),
        );
        queued += 1;
    }
    Ok(queued)
}

fn queue_failed_shipment_retries_if_idle(
    scheduler: &mut PathScheduler,
    conn: &rusqlite::Connection,
    offline: bool,
    limit: usize,
    limiter: Option<&AdaptiveLimiter>,
    archive_repair_mode: ArchiveRepairMode,
) -> Result<usize> {
    if offline || scheduler.has_pending_work() {
        return Ok(0);
    }
    queue_failed_shipment_retry_paths(scheduler, conn, limit, limiter, archive_repair_mode)
}

fn provider_name_to_static(provider: &str) -> Option<&'static str> {
    match provider {
        "claude" => Some("claude"),
        "codex" => Some("codex"),
        "cursor" => Some("cursor"),
        "antigravity" => Some("antigravity"),
        "gemini" => Some("antigravity"),
        _ => None,
    }
}

fn work_context(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Live => "live_transcript",
        WorkPriority::Retry => FAILED_SHIPMENT_RETRY_CONTEXT,
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

    // Pool checkout may wait on SQLite contention. This task runs on the
    // LocalSet that owns wake/control dispatch, so a synchronous checkout here
    // can stall every managed session behind archive workers. Keep the wait on
    // the blocking pool just like parsing/preparation below.
    let db_pool = task_context.db_pool.clone();
    let mut conn = match tokio::task::spawn_blocking(move || db_pool.get()).await {
        Ok(Ok(conn)) => conn,
        Ok(Err(e)) => {
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
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!(
                    "Shipper DB checkout task failed for {}: {}",
                    result.job.path.display(),
                    e
                );
            }
            result.local_retry_after = Some(local_retry_delay(result.job.priority));
            return finish_path_task(result, task_started);
        }
    };

    // Cursor stores are source-faithful only through the native storage-v2
    // adapter. Never replay an old pointer spool or parse/post a lossy legacy
    // projection when this Runtime Host lacks the v2 cutover.
    if (is_cursor_database_job(&result.job) || is_cursor_acp_source_job(&result.job))
        && task_context.storage_v2.is_none()
    {
        if let Err(error) = Spool::new(&conn).dead_letter_pending_for_provider(
            "cursor",
            "Cursor legacy pointer spool retired: storage-v2 source receipt is required",
        ) {
            tracing::warn!(error = %error, "Unable to retire legacy Cursor spool entries");
        }
        tracing::warn!(
            path = %result.job.path.display(),
            "Cursor store is not shipped: Runtime Host has no storage-v2 cutover"
        );
        return finish_path_task(result, task_started);
    }

    if result.job.priority != WorkPriority::Live && task_context.storage_v2.is_none() {
        let replay_prepare_at_ms = chrono::Utc::now().timestamp_millis();
        let replay_trace = shipper::ShipTraceContext {
            work_context: FAILED_SHIPMENT_RETRY_CONTEXT,
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
        match shipper::replay_ready_spool_for_path_with_batch_bytes_and_parse_tracker(
            &conn,
            &task_context.client,
            task_context.algo,
            &result.job.path,
            failed_shipment_retry_path_limit(task_context.limiter.as_ref()),
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
            .pending_entries_for_path_ready(&result.job.path.to_string_lossy(), 1)
            .map(|entries| !entries.is_empty())
            .unwrap_or(false);
        if result.failed_spool > 0 {
            result.local_retry_after = spool_retry_delay_for_path(&conn, &result.job.path);
            result.local_retry_priority = Some(WorkPriority::Retry);
            return finish_path_task(result, task_started);
        } else if ready_spool_remaining {
            result.rerun_priority = Some(WorkPriority::Retry);
        }
    }

    if let Some(capabilities) = task_context.storage_v2.as_deref() {
        let lane = if result.job.priority == WorkPriority::Live {
            "live"
        } else {
            "repair"
        };
        let stats_lane = if result.job.priority == WorkPriority::Live {
            ShipLane::Live
        } else {
            ShipLane::Repair
        };
        let timeout = if lane == "live" {
            Duration::from_secs(20)
        } else {
            Duration::from_secs(75)
        };
        let ship_started = Instant::now();
        let ship_result = if is_opencode_database_job(&result.job) {
            crate::storage_v2_shipper::ship_next_opencode_envelope(
                &mut conn,
                &task_context.client,
                capabilities,
                &result.job.path,
                lane,
                timeout,
            )
            .await
        } else if is_cursor_database_job(&result.job) {
            crate::storage_v2_shipper::ship_next_cursor_envelope(
                &mut conn,
                &task_context.client,
                capabilities,
                &result.job.path,
                lane,
                timeout,
            )
            .await
        } else if is_cursor_acp_source_job(&result.job) {
            crate::storage_v2_shipper::ship_next_cursor_acp_envelope(
                &mut conn,
                &task_context.client,
                capabilities,
                &result.job.path,
                lane,
                timeout,
            )
            .await
        } else {
            crate::storage_v2_shipper::ship_next_envelope(
                &mut conn,
                &task_context.client,
                capabilities,
                &result.job.path,
                result.job.provider,
                result.job.observation.session_id.as_deref(),
                lane,
                timeout,
            )
            .await
        };
        match ship_result {
            Ok(Some(outcome)) => {
                let latency_ms = ship_started.elapsed().as_millis() as u64;
                task_context.ship_stats.record_with_lane_detail_and_stages(
                    stats_lane,
                    ShipAttemptOutcome::Ok,
                    latency_ms,
                    None,
                    None,
                    None,
                    outcome.events_shipped as u32,
                    outcome.bytes_shipped,
                    false,
                    None,
                );
                task_context.ship_stats.record_events_and_bytes_shipped(
                    stats_lane,
                    outcome.events_shipped as u32,
                    outcome.bytes_shipped,
                    latency_ms,
                );
                if let Some(failures) = task_context.tracker.record_success() {
                    tracing::info!(
                        failures,
                        "Storage-v2 shipping recovered after consecutive failures"
                    );
                }
                result.events_shipped = outcome.events_shipped;
                if outcome.has_more {
                    result.rerun_priority = Some(result.job.priority);
                } else if let Err(error) =
                    retire_legacy_spool_after_storage_v2(&conn, &result.job.path)
                {
                    tracing::warn!(
                        path = %result.job.path.display(),
                        error = %error,
                        "Storage-v2 reached source head but legacy spool retirement failed"
                    );
                    result.local_retry_after = Some(local_retry_delay(result.job.priority));
                }
                tracing::info!(
                    path = %result.job.path.display(),
                    provider = result.job.provider,
                    lane,
                    bytes_shipped = outcome.bytes_shipped,
                    events_shipped = outcome.events_shipped,
                    "Shipped storage-v2 source envelope"
                );
            }
            Ok(None) => {
                if let Err(error) = retire_legacy_spool_after_storage_v2(&conn, &result.job.path) {
                    tracing::warn!(
                        path = %result.job.path.display(),
                        error = %error,
                        "Storage-v2 source is current but legacy spool retirement failed"
                    );
                    result.local_retry_after = Some(local_retry_delay(result.job.priority));
                }
            }
            Err(error) => {
                if let Some(blocked) =
                    error.downcast_ref::<crate::storage_v2_shipper::StorageV2SourceBlocked>()
                {
                    task_context.ship_stats.record_with_lane_detail_and_stages(
                        stats_lane,
                        ShipAttemptOutcome::PayloadRejected,
                        ship_started.elapsed().as_millis() as u64,
                        Some(409),
                        Some("storage_v2_source_blocked"),
                        Some(&blocked.to_string()),
                        0,
                        0,
                        false,
                        None,
                    );
                    if blocked.newly_blocked {
                        tracing::warn!(
                            path = %result.job.path.display(),
                            provider = result.job.provider,
                            source_epoch = %blocked.source_epoch,
                            kind = blocked.kind,
                            detail = blocked.detail,
                            "Storage-v2 source quarantined; automatic retries stopped"
                        );
                    }
                    return finish_path_task(result, task_started);
                }
                if error
                    .downcast_ref::<crate::storage_v2_shipper::StorageV2PreparationError>()
                    .is_some()
                {
                    if task_context.tracker.record_error() {
                        tracing::warn!(
                            path = %result.job.path.display(),
                            provider = result.job.provider,
                            error = %error,
                            "Storage-v2 source preparation failed; retrying locally"
                        );
                    }
                    result.local_retry_after = Some(local_retry_delay(result.job.priority));
                    return finish_path_task(result, task_started);
                }
                let backpressure =
                    error.downcast_ref::<crate::shipping::client::StorageV2Backpressure>();
                task_context.ship_stats.record_with_lane_detail_and_stages(
                    stats_lane,
                    ShipAttemptOutcome::RetryableClientError,
                    ship_started.elapsed().as_millis() as u64,
                    backpressure.map(|_| 503),
                    Some(if backpressure.is_some() {
                        "storage_lane_busy"
                    } else {
                        "storage_v2_ship_failed"
                    }),
                    Some(&error.to_string()),
                    0,
                    0,
                    backpressure.is_some(),
                    None,
                );
                if let Some(backpressure) = backpressure {
                    task_context
                        .limiter
                        .observe_backpressure(Some(backpressure.retry_after));
                }
                if task_context.tracker.record_error() {
                    tracing::warn!(
                        path = %result.job.path.display(),
                        provider = result.job.provider,
                        lane,
                        error = %error,
                        "Storage-v2 ship failed; durable cursor remains unchanged"
                    );
                }
                result.local_retry_after = Some(
                    backpressure
                        .map(|value| {
                            storage_v2_backpressure_retry_delay(
                                result.job.priority,
                                value.retry_after,
                            )
                        })
                        .unwrap_or_else(|| local_retry_delay(result.job.priority)),
                );
            }
        }
        return finish_path_task(result, task_started);
    }

    if is_opencode_database_job(&result.job) {
        let file_start = Instant::now();
        let opencode_trace = shipper::ShipTraceContext {
            work_context: work_context(result.job.priority),
            observation_source: result.job.observation.source,
            observed_at_ms: result.job.observation.observed_at_ms,
            latest_observed_at_ms: result.job.observation.latest_observed_at_ms,
            wake_received_at_ms: result.job.observation.wake_received_at_ms,
            enqueued_at_ms: result.job.observation.enqueued_at_ms,
            job_started_at_ms,
            prepare_started_at_ms: job_started_at_ms,
            prepare_finished_at_ms: chrono::Utc::now().timestamp_millis(),
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
        match shipper::ship_opencode_database_with_trace(
            &result.job.path,
            &conn,
            &task_context.client,
            task_context.algo,
            task_context.shipper_config.max_batch_bytes,
            Some(&task_context.tracker),
            Some(&task_context.parse_tracker),
            if result.job.priority == WorkPriority::Scan {
                shipper::OpenCodeShipMode::ReconcileDurability
            } else {
                shipper::OpenCodeShipMode::ChangedOnly
            },
            Some(&opencode_trace),
        )
        .await
        {
            Ok(outcome) => {
                let sessions_shipped = outcome.sessions_shipped;
                let events_shipped = outcome.events_shipped;
                if sessions_shipped > 0 {
                    tracing::info!(
                        context = work_context(result.job.priority),
                        path = %result.job.path.display(),
                        provider = result.job.provider,
                        sessions_shipped,
                        events_shipped,
                        elapsed_ms = file_start.elapsed().as_millis() as u64,
                        "Shipped OpenCode SQLite database"
                    );
                }
                shipper::log_slow_file_processing(
                    work_context(result.job.priority),
                    Path::new(&result.job.path),
                    result.job.provider,
                    events_shipped,
                    0,
                    0,
                    file_start.elapsed(),
                );
                result.events_shipped = events_shipped;
                if outcome.reconciliation_pending {
                    result.rerun_priority = Some(WorkPriority::Scan);
                }
            }
            Err(e) => {
                if task_context.tracker.record_error() {
                    tracing::warn!(
                        "Error shipping OpenCode database {}: {}",
                        result.job.path.display(),
                        e
                    );
                }
                result.local_retry_after = Some(local_retry_delay(result.job.priority));
            }
        }
        return finish_path_task(result, task_started);
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
                tracing::warn!(
                    path = %result.job.path.display(),
                    error = %e,
                    "Source preparation failed; waiting for new filesystem evidence"
                );
            }
            // Preparation errors are deterministic for the current source
            // bytes. A timer retry only burns CPU and transport capacity. The
            // watcher will enqueue immediately when the source changes; the
            // slow fallback scan remains the crash/restart safety net.
        }
    }

    finish_path_task(result, task_started)
}

fn retire_legacy_spool_after_storage_v2(
    conn: &rusqlite::Connection,
    path: &Path,
) -> anyhow::Result<usize> {
    let spool = Spool::new(conn);
    let path = path.to_string_lossy();
    let mut retired = 0usize;
    loop {
        let pending = spool.pending_entries_for_path_now(&path, 1_000)?;
        if pending.is_empty() {
            return Ok(retired);
        }
        for entry in pending {
            spool.mark_shipped(entry.id)?;
            retired += 1;
        }
    }
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

    #[derive(Clone)]
    struct RetainedFixtureRow {
        path: PathBuf,
        value: u32,
    }

    #[test]
    fn watcher_batch_separates_managed_state_from_transcript_events() {
        let managed_root = PathBuf::from("/tmp/managed/codex-bridge");
        let managed = WatcherEvent {
            path: managed_root.join("new-session.json"),
            observed_at_ms: 1,
            latest_observed_at_ms: 1,
        };
        let transcript = WatcherEvent {
            path: PathBuf::from("/tmp/transcripts/session.jsonl"),
            observed_at_ms: 2,
            latest_observed_at_ms: 2,
        };

        let (managed_paths, transcript_events) = partition_managed_state_events(
            vec![transcript.clone(), managed.clone()],
            &[managed_root],
        );

        assert_eq!(managed_paths, vec![managed.path]);
        assert_eq!(transcript_events, vec![transcript]);
    }

    #[test]
    fn unknown_managed_state_path_requires_full_reconciliation() {
        let observation = codex_bridge_observation(
            Path::new("/tmp/transcript.jsonl"),
            None,
            None,
            "2026-07-16T00:00:00Z",
            true,
        );
        let known_path = observation.state_file.clone();
        let snapshot = ManagedObservationSnapshot {
            codex: vec![observation],
            ..ManagedObservationSnapshot::default()
        };

        assert!(!managed_state_changes_require_full_reconciliation(
            &snapshot,
            &[known_path],
        ));
        assert!(managed_state_changes_require_full_reconciliation(
            &snapshot,
            &[PathBuf::from("/tmp/new-managed-session.json")],
        ));
    }

    #[test]
    fn managed_projection_equivalence_ignores_writer_churn_but_not_tui_state() {
        let observation = codex_bridge_observation(
            Path::new("/tmp/transcript.jsonl"),
            Some("turn-1"),
            Some("running"),
            "2026-07-16T00:00:00Z",
            true,
        );
        let first = ManagedObservationSnapshot {
            codex: vec![observation],
            ..ManagedObservationSnapshot::default()
        };
        let mut writer_churn = first.clone();
        writer_churn.codex[0].updated_at = "2026-07-16T00:00:05Z".to_string();
        writer_churn.codex[0].active_turn_id = Some("turn-2".to_string());
        writer_churn.codex[0].last_turn_status = Some("completed".to_string());

        assert!(first.projection_equivalent(&writer_churn));

        writer_churn.codex[0].has_tui_attachment = true;
        assert!(!first.projection_equivalent(&writer_churn));

        let mut dead = first.codex[0].clone();
        dead.session_id = "dead-history".to_string();
        dead.bridge_alive = false;
        dead.app_server_alive = false;
        dead.has_tui_attachment = false;
        let with_history = ManagedObservationSnapshot {
            codex: vec![first.codex[0].clone(), dead],
            ..ManagedObservationSnapshot::default()
        };
        let current = with_history.current_only();
        assert_eq!(current.codex.len(), 1);
        assert_eq!(current.codex[0].session_id, first.codex[0].session_id);
    }

    #[test]
    fn deferred_managed_pair_invalidates_stale_refresh_and_requests_full_retry() {
        let mut generation = 41;
        let mut pending_full = false;

        defer_managed_pair_retry(&mut generation, &mut pending_full);

        assert_eq!(generation, 42);
        assert!(pending_full);
    }

    #[test]
    fn partial_scan_retains_prior_row_only_while_state_file_still_exists() {
        let temp = tempfile::tempdir().unwrap();
        let existing = temp.path().join("existing.json");
        let removed = temp.path().join("removed.json");
        std::fs::write(&existing, "{}").unwrap();
        let previous = vec![
            RetainedFixtureRow {
                path: existing,
                value: 1,
            },
            RetainedFixtureRow {
                path: removed,
                value: 2,
            },
        ];
        let mut current = Vec::new();

        retain_existing_observations(&mut current, &previous, |row| &row.path);

        assert_eq!(current.len(), 1);
        assert_eq!(current[0].value, 1);
    }

    #[test]
    fn wake_gap_detector_separates_suspend_gap_from_normal_timer_jitter() {
        let monotonic = Instant::now();
        let wall = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000);
        let mut detector = WakeGapDetector {
            last_wall: wall,
            last_monotonic: monotonic,
        };

        assert_eq!(
            detector.observe(
                wall + Duration::from_secs(1),
                monotonic + Duration::from_secs(1),
            ),
            None
        );
        assert_eq!(
            detector.observe(
                wall + Duration::from_secs(12),
                monotonic + Duration::from_secs(2),
            ),
            Some(Duration::from_secs(10))
        );

        // A wall-clock correction resets the baseline instead of poisoning
        // every later wake comparison.
        assert_eq!(
            detector.observe(
                wall + Duration::from_secs(5),
                monotonic + Duration::from_secs(3),
            ),
            None
        );
        assert_eq!(
            detector.observe(
                wall + Duration::from_secs(6),
                monotonic + Duration::from_secs(4),
            ),
            None
        );
    }

    fn test_observation() -> ObservationTrace {
        ObservationTrace {
            source: "test",
            observed_at_ms: 1,
            latest_observed_at_ms: None,
            wake_received_at_ms: None,
            enqueued_at_ms: 2,
            session_id: None,
            turn_id: None,
            wake_reason: None,
            file_len_hint: None,
        }
    }

    #[test]
    fn test_storage_v2_source_head_retires_only_matching_legacy_spool_rows() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let spool = Spool::new(&conn);
        spool
            .enqueue("codex", "/tmp/target.jsonl", 0, 100, Some("target"))
            .unwrap();
        spool
            .enqueue("codex", "/tmp/target.jsonl", 100, 200, Some("target"))
            .unwrap();
        spool
            .enqueue("codex", "/tmp/other.jsonl", 0, 100, Some("other"))
            .unwrap();

        assert_eq!(
            retire_legacy_spool_after_storage_v2(&conn, Path::new("/tmp/target.jsonl")).unwrap(),
            2
        );
        assert!(spool
            .pending_entries_for_path_now("/tmp/target.jsonl", 10)
            .unwrap()
            .is_empty());
        assert_eq!(
            spool
                .pending_entries_for_path_now("/tmp/other.jsonl", 10)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn test_opencode_database_job_uses_sqlite_shipper_path() {
        let job = PathJob {
            path: PathBuf::from("/tmp/opencode.db"),
            provider: "opencode",
            priority: WorkPriority::Scan,
            observation: test_observation(),
        };
        assert!(is_opencode_database_job(&job));

        let wal_job = PathJob {
            path: PathBuf::from("/tmp/opencode.db-wal"),
            provider: "opencode",
            priority: WorkPriority::Scan,
            observation: test_observation(),
        };
        assert!(!is_opencode_database_job(&wal_job));

        let codex_job = PathJob {
            path: PathBuf::from("/tmp/opencode.db"),
            provider: "codex",
            priority: WorkPriority::Scan,
            observation: test_observation(),
        };
        assert!(!is_opencode_database_job(&codex_job));
    }

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
            archive_backlog: crate::state::spool::ArchiveBacklogSnapshot::default(),
            storage_v2_outbox:
                crate::state::pending_source_envelope::StorageV2OutboxSnapshot::default(),
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
            ship_lanes: crate::shipping_stats::ShipLaneSummarySet::default(),
            events_per_sec_ewma_10s: None,
            bytes_per_sec_ewma_10s: None,
            disk_free_bytes: 0,
            is_offline: false,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            machine_evidence: None,
            sessions: Vec::new(),
            sessions_digest: None,
            sessions_sequence: None,
            adaptive_backlog_limiter: None,
            ship_scheduler: None,
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
            timeline_title: None,
            first_user_message: None,
            title_state: None,
            title_source: None,
            workspace: heartbeat::ResolvedWorkspace {
                cwd: Some("/tmp/project".to_string()),
                label: Some("project".to_string()),
                branch: None,
            },
            process: heartbeat::ResolvedProcess {
                pid,
                process_start_time: Some("2026-05-05T12:00:00Z".to_string()),
                boot_id: None,
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
            schema_version: crate::codex_bridge::BRIDGE_STATE_SCHEMA_VERSION,
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
            bridge_process_start_time: Some("Mon May  5 11:58:00 2026".to_string()),
            app_server_pid: None,
            app_server_process_start_time: None,
            app_server_pgid: None,
            updated_at: updated_at.to_string(),
            bridge_alive,
            has_tui_attachment: false,
            app_server_alive: false,
        }
    }

    #[test]
    fn test_build_local_status_projection_uses_cached_unmanaged_bindings() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let tracker = ConsecutiveErrorTracker::new();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();
        let cached = vec![unmanaged_binding("sess-cached", 42)];
        let mut session_snapshot_state = SessionSnapshotState::default();

        let projection = build_local_status_projection(
            &conn,
            &tracker,
            &parse_tracker,
            &ship_stats,
            false,
            &None,
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &cached,
            false,
            None,
            None,
            ArchiveRepairMode::Drain,
            &mut session_snapshot_state,
        );

        assert_eq!(projection.payload.unmanaged_session_bindings, cached);
    }

    #[test]
    fn test_build_local_status_projection_sequences_only_digest_changes() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let tracker = ConsecutiveErrorTracker::new();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();
        let cached = vec![unmanaged_binding("sess-cached", 42)];
        let mut session_snapshot_state = SessionSnapshotState::default();

        let first = build_local_status_projection(
            &conn,
            &tracker,
            &parse_tracker,
            &ship_stats,
            false,
            &None,
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &cached,
            false,
            None,
            None,
            ArchiveRepairMode::Drain,
            &mut session_snapshot_state,
        );
        let second = build_local_status_projection(
            &conn,
            &tracker,
            &parse_tracker,
            &ship_stats,
            false,
            &None,
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &cached,
            false,
            None,
            None,
            ArchiveRepairMode::Drain,
            &mut session_snapshot_state,
        );
        let changed = vec![unmanaged_binding("sess-cached", 43)];
        let third = build_local_status_projection(
            &conn,
            &tracker,
            &parse_tracker,
            &ship_stats,
            false,
            &None,
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &changed,
            false,
            None,
            None,
            ArchiveRepairMode::Drain,
            &mut session_snapshot_state,
        );

        assert_eq!(
            first.payload.sessions_digest,
            second.payload.sessions_digest
        );
        assert_eq!(first.payload.sessions_sequence, Some(1));
        assert_eq!(second.payload.sessions_sequence, Some(1));
        assert_ne!(
            second.payload.sessions_digest,
            third.payload.sessions_digest
        );
        assert_eq!(third.payload.sessions_sequence, Some(2));
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
    fn test_runtime_truth_signature_changes_when_process_boot_identity_changes() {
        let mut first = empty_heartbeat_payload();
        first.sessions.push(resolved_session(
            "claude",
            None,
            Some("sess-1"),
            "unmanaged",
            "unmanaged",
            Some(42),
        ));
        first.sessions[0].process.boot_id = Some("macos:1777970400:0".to_string());
        let mut second = first.clone();
        second.sessions[0].process.boot_id = Some("macos:1778056800:0".to_string());

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
    fn test_cursor_turn_completed_wake_schedules_native_store_shipping() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();

        let scheduled = record_transcript_wake_hint(
            &mut latest_wakes,
            TranscriptWakeSignal {
                provider: "cursor".to_string(),
                path: transcript.path().to_path_buf(),
                phase: "idle".to_string(),
                observed_at_ms: 123,
                session_id: Some("cursor-session".to_string()),
                turn_id: Some("cursor-generation".to_string()),
                wake_reason: Some("turn_completed".to_string()),
                file_len_hint: Some(4096),
                received_at_ms: Some(124),
            },
        )
        .expect("completed Cursor turns should schedule native store shipping");

        assert_eq!(scheduled.0, transcript.path());
        assert_eq!(scheduled.1, "cursor");
        assert_eq!(scheduled.2.source, "wake_socket");
        assert_eq!(scheduled.2.session_id.as_deref(), Some("cursor-session"));
        assert_eq!(scheduled.2.turn_id.as_deref(), Some("cursor-generation"));
    }

    #[test]
    fn test_enqueue_transcript_wake_signal_queues_live_work() {
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut latest_wakes = HashMap::new();
        let mut scheduler = PathScheduler::new(4);

        assert!(enqueue_transcript_wake_signal(
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
        .is_some());

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
    fn test_queue_failed_shipment_retries_if_idle_refills_drained_scheduler() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let path = transcript.path().to_string_lossy().to_string();
        Spool::new(&conn)
            .enqueue("codex", &path, 0, 100, Some("session-id"))
            .unwrap();

        let mut scheduler = PathScheduler::new(4);
        let queued = queue_failed_shipment_retries_if_idle(
            &mut scheduler,
            &conn,
            false,
            10,
            None,
            ArchiveRepairMode::Drain,
        )
        .unwrap();

        assert_eq!(queued, 1);
        let job = scheduler
            .pop_launchable()
            .expect("failed-shipment retry job queued");
        assert_eq!(job.path, PathBuf::from(&path));
        assert_eq!(job.priority, WorkPriority::Retry);
        assert_eq!(
            job.observation.source,
            FAILED_SHIPMENT_RETRY_OBSERVATION_SOURCE
        );
    }

    #[test]
    fn test_paused_mode_does_not_queue_failed_shipment_retry_paths() {
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_vars(
            [
                (
                    "LONGHOUSE_HOME",
                    Some(temp.path().join("lh").display().to_string()),
                ),
                ("HOME", Some(temp.path().join("home").display().to_string())),
            ],
            || {
                let db = tempfile::NamedTempFile::new().unwrap();
                let transcript = tempfile::NamedTempFile::new().unwrap();
                let conn = open_db(Some(db.path())).unwrap();
                let path = transcript.path().to_string_lossy().to_string();
                Spool::new(&conn)
                    .enqueue("codex", &path, 0, 100, Some("session-id"))
                    .unwrap();

                let mut scheduler = PathScheduler::new(4);
                let queued = queue_failed_shipment_retry_paths(
                    &mut scheduler,
                    &conn,
                    10,
                    None,
                    ArchiveRepairMode::Paused,
                )
                .unwrap();

                assert_eq!(queued, 0);
                assert!(scheduler.pop_launchable().is_none());
            },
        );
    }

    #[test]
    fn test_archive_startup_replay_warmup_delays_non_paused_modes() {
        assert_eq!(
            archive_startup_replay_warmup_delay(ArchiveRepairMode::Paused, 1.0),
            None
        );
        assert_eq!(
            archive_startup_replay_warmup_delay(ArchiveRepairMode::Trickle, 0.0),
            Some(ARCHIVE_STARTUP_REPLAY_WARMUP_MIN)
        );
        assert_eq!(
            archive_startup_replay_warmup_delay(ArchiveRepairMode::Drain, 1.0),
            Some(ARCHIVE_STARTUP_REPLAY_WARMUP_MAX)
        );
    }

    #[test]
    fn test_running_control_file_can_resume_paused_archive_replay_as_trickle() {
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_vars(
            [
                (
                    "LONGHOUSE_HOME",
                    Some(temp.path().join("lh").display().to_string()),
                ),
                ("HOME", Some(temp.path().join("home").display().to_string())),
            ],
            || {
                let db = tempfile::NamedTempFile::new().unwrap();
                let transcript = tempfile::NamedTempFile::new().unwrap();
                let conn = open_db(Some(db.path())).unwrap();
                let path = transcript.path().to_string_lossy().to_string();
                Spool::new(&conn)
                    .enqueue("codex", &path, 0, 100, Some("session-id"))
                    .unwrap();

                let mut scheduler = PathScheduler::new(4);
                let queued = queue_failed_shipment_retry_paths(
                    &mut scheduler,
                    &conn,
                    10,
                    None,
                    ArchiveRepairMode::Paused,
                )
                .unwrap();
                assert_eq!(queued, 0);

                let control_path = config::get_agent_archive_repair_control_path().unwrap();
                std::fs::create_dir_all(control_path.parent().unwrap()).unwrap();
                let expires_at = (chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339();
                std::fs::write(
                    &control_path,
                    serde_json::to_vec(&json!({"mode": "trickle", "expires_at": expires_at}))
                        .unwrap(),
                )
                .unwrap();

                let queued = queue_failed_shipment_retry_paths(
                    &mut scheduler,
                    &conn,
                    10,
                    None,
                    ArchiveRepairMode::Paused,
                )
                .unwrap();
                assert_eq!(queued, 1);
                let job = scheduler.pop_launchable().expect("trickle queued replay");
                assert_eq!(job.path, PathBuf::from(&path));
                assert_eq!(job.priority, WorkPriority::Retry);
            },
        );
    }

    #[test]
    fn test_queue_failed_shipment_retries_suppress_huge_under_host_pressure() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        Spool::new(&conn)
            .enqueue(
                "codex",
                "/tmp/small-ready.jsonl",
                0,
                100,
                Some("small-session"),
            )
            .unwrap();
        Spool::new(&conn)
            .enqueue(
                "codex",
                "/tmp/huge-ready.jsonl",
                0,
                200 * 1024 * 1024,
                Some("huge-session"),
            )
            .unwrap();

        let limiter = AdaptiveLimiter::new();
        for _ in 0..4 {
            limiter.observe(1_000.0);
        }
        assert!(!limiter.huge_range_eligible());

        let mut scheduler = PathScheduler::new(4);
        let queued = queue_failed_shipment_retries_if_idle(
            &mut scheduler,
            &conn,
            false,
            10,
            Some(limiter.as_ref()),
            ArchiveRepairMode::Drain,
        )
        .unwrap();

        assert_eq!(queued, 1);
        let job = scheduler
            .pop_launchable()
            .expect("small failed-shipment retry job queued");
        assert_eq!(job.path, PathBuf::from("/tmp/small-ready.jsonl"));
        assert_eq!(job.priority, WorkPriority::Retry);
        assert!(scheduler.pop_launchable().is_none());
    }

    #[test]
    fn test_failed_shipment_retry_path_limit_tracks_archive_pressure() {
        let limiter = AdaptiveLimiter::new();
        assert_eq!(failed_shipment_retry_path_limit(limiter.as_ref()), 2);

        limiter.observe_backpressure(Some(Duration::from_secs(5)));
        assert_eq!(failed_shipment_retry_path_limit(limiter.as_ref()), 1);

        let limiter = AdaptiveLimiter::new();
        for _ in 0..4 {
            limiter.observe_ingest_timing(10.0, Some(50.0), None, None, None, None);
        }
        assert_eq!(failed_shipment_retry_path_limit(limiter.as_ref()), 8);
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
    fn test_archive_repair_mode_parse_and_control_precedence() {
        assert_eq!(
            ArchiveRepairMode::parse("paused").unwrap(),
            ArchiveRepairMode::Paused
        );
        assert_eq!(
            ArchiveRepairMode::parse("resume").unwrap(),
            ArchiveRepairMode::Trickle
        );
        assert_eq!(
            ArchiveRepairMode::parse("drain-now").unwrap(),
            ArchiveRepairMode::Drain
        );
        assert!(ArchiveRepairMode::parse("enabled").is_err());

        let unset = ArchiveRepairControl::default();
        assert_eq!(
            unset.normalized_mode(ArchiveRepairMode::Paused),
            ArchiveRepairMode::Paused
        );
        assert_eq!(
            unset.normalized_mode(ArchiveRepairMode::Drain),
            ArchiveRepairMode::Drain
        );

        let operator_control = ArchiveRepairControl {
            mode: Some("trickle".to_string()),
            expires_at: Some((chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339()),
            max_tick_bytes: None,
            include_huge: None,
            ..Default::default()
        };
        assert_eq!(
            operator_control.normalized_mode(ArchiveRepairMode::Paused),
            ArchiveRepairMode::Trickle
        );

        let invalid_control = ArchiveRepairControl {
            mode: Some("enabled".to_string()),
            expires_at: Some((chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339()),
            max_tick_bytes: None,
            include_huge: None,
            ..Default::default()
        };
        assert_eq!(
            invalid_control.normalized_mode(ArchiveRepairMode::Paused),
            ArchiveRepairMode::Paused
        );

        let expired_control = ArchiveRepairControl {
            mode: Some("drain".to_string()),
            expires_at: Some((chrono::Utc::now() - chrono::Duration::minutes(1)).to_rfc3339()),
            max_tick_bytes: Some(4 * 1024 * 1024 * 1024),
            include_huge: Some(true),
            ..Default::default()
        };
        assert_eq!(
            expired_control.normalized_mode(ArchiveRepairMode::Paused),
            ArchiveRepairMode::Paused
        );
        assert!(!expired_control.active_override());

        let legacy_drain = ArchiveRepairControl {
            mode: Some("drain".to_string()),
            expires_at: None,
            max_tick_bytes: Some(4 * 1024 * 1024 * 1024),
            include_huge: Some(true),
            ..Default::default()
        };
        assert_eq!(
            legacy_drain.normalized_mode(ArchiveRepairMode::Drain),
            ArchiveRepairMode::Drain
        );
    }

    #[test]
    fn test_archive_paused_status_is_distinct_from_offline() {
        let mut payload = empty_heartbeat_payload();
        payload.archive_backlog.pending_ranges = 2;
        payload.archive_backlog.state = "ready".to_string();
        let control = ArchiveRepairControl {
            mode: Some("paused".to_string()),
            actor: Some("menu_bar".to_string()),
            reason: Some("user paused while travelling".to_string()),
            updated_at: Some("2026-07-13T12:00:00Z".to_string()),
            ..Default::default()
        };

        apply_archive_repair_control(&mut payload, &control, ArchiveRepairMode::Paused);

        assert_eq!(payload.archive_backlog.mode, "paused");
        assert_eq!(payload.archive_backlog.state, "paused");
        assert_eq!(
            payload.archive_backlog.pause_actor.as_deref(),
            Some("menu_bar")
        );
        assert_eq!(
            payload.archive_backlog.pause_reason.as_deref(),
            Some("user paused while travelling")
        );
        assert!(!payload.is_offline);
    }

    #[test]
    fn test_archive_trickle_status_does_not_keep_stale_paused_state() {
        let mut payload = empty_heartbeat_payload();
        payload.archive_backlog.pending_ranges = 2;
        payload.archive_backlog.ready_ranges = 2;
        payload.archive_backlog.state = "paused".to_string();
        let control = ArchiveRepairControl {
            mode: Some("trickle".to_string()),
            expires_at: Some((chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339()),
            max_tick_bytes: None,
            include_huge: None,
            ..Default::default()
        };

        apply_archive_repair_control(&mut payload, &control, ArchiveRepairMode::Paused);

        assert_eq!(payload.archive_backlog.mode, "trickle");
        assert_eq!(payload.archive_backlog.state, "scanning");

        let mut scheduler = PathScheduler::new(2);
        scheduler.enqueue(
            PathBuf::from("/archive.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        let _job = scheduler.pop_launchable().unwrap();
        payload.ship_scheduler = Some(scheduler.snapshot());
        apply_archive_repair_control(&mut payload, &control, ArchiveRepairMode::Paused);
        assert_eq!(payload.archive_backlog.state, "uploading");

        payload.ship_scheduler = None;
        payload.archive_backlog.ready_ranges = 0;
        payload.archive_backlog.deferred_ranges = 2;
        apply_archive_repair_control(&mut payload, &control, ArchiveRepairMode::Paused);
        assert_eq!(payload.archive_backlog.state, "blocked");
    }

    #[tokio::test]
    async fn test_paused_mode_skips_reconciliation_scan_task() {
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_vars(
            [
                (
                    "LONGHOUSE_HOME",
                    Some(temp.path().join("lh").display().to_string()),
                ),
                ("HOME", Some(temp.path().join("home").display().to_string())),
            ],
            || {
                let mut discovery_tasks = JoinSet::new();
                let providers = vec![ProviderConfig {
                    name: "codex",
                    root: PathBuf::from("/tmp/no-scan-when-paused"),
                    extension: "jsonl",
                }];
                let scheduler = PathScheduler::new(4);
                let deferred_retries = HashMap::new();

                maybe_start_reconciliation_scan(
                    &mut discovery_tasks,
                    &providers,
                    &scheduler,
                    &deferred_retries,
                    ArchiveRepairMode::Paused,
                    "test paused scan",
                );

                assert!(discovery_tasks.is_empty());
            },
        );
    }

    #[tokio::test]
    async fn test_archive_retry_backlog_does_not_starve_reconciliation_scan() {
        let mut discovery_tasks = JoinSet::new();
        let providers = vec![ProviderConfig {
            name: "codex",
            root: PathBuf::from("/tmp/reconciliation-alongside-retry"),
            extension: "jsonl",
        }];
        let mut scheduler = PathScheduler::new(4);
        scheduler.enqueue(
            PathBuf::from("/tmp/archive-retry.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        let deferred_retries = HashMap::new();

        maybe_start_reconciliation_scan(
            &mut discovery_tasks,
            &providers,
            &scheduler,
            &deferred_retries,
            ArchiveRepairMode::Trickle,
            "test retry backlog",
        );

        assert_eq!(discovery_tasks.len(), 1);
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
        assert_eq!(
            storage_v2_backpressure_retry_delay(WorkPriority::Live, Duration::from_secs(5),),
            Duration::from_secs(1)
        );
        assert_eq!(
            storage_v2_backpressure_retry_delay(WorkPriority::Scan, Duration::from_secs(5),),
            Duration::from_secs(5)
        );
    }

    #[test]
    fn test_spawn_caffeinate_uses_correct_args() {
        let pid = std::process::id();
        let child = spawn_caffeinate(pid).expect("caffeinate should spawn");
        let id = child.id().expect("child should have a PID");

        // caffeinate should be running as our child
        assert!(id > 0);

        // read its cmdline to verify args
        let output = std::process::Command::new("ps")
            .args(["-o", "args=", "-p", &id.to_string()])
            .output()
            .expect("ps should succeed");
        let cmdline = String::from_utf8_lossy(&output.stdout);

        assert!(
            cmdline.contains("-s"),
            "caffeinate should have -s flag, got: {}",
            cmdline
        );
        assert!(
            cmdline.contains("-w"),
            "caffeinate should have -w flag, got: {}",
            cmdline
        );
        assert!(
            cmdline.contains(&pid.to_string()),
            "caffeinate should watch daemon PID {}, got: {}",
            pid,
            cmdline
        );
    }

    #[test]
    fn test_caffeinate_child_exits_when_dropped() {
        let pid = std::process::id();
        let child = spawn_caffeinate(pid).expect("caffeinate should spawn");
        let caffeinate_pid = child.id().expect("child should have a PID");

        // Drop the handle — since we use -w <pid> and caffeinate watches
        // the daemon PID (us), it will keep running until our process exits.
        // For the test we just verify the child was spawned successfully.
        drop(child);

        // Brief wait then check — caffeinate should still be alive since our
        // test PID hasn't exited (caffeinate waits for -w <pid> to die).
        std::thread::sleep(std::time::Duration::from_millis(100));

        let status = std::process::Command::new("kill")
            .args(["-0", &caffeinate_pid.to_string()])
            .status();
        // kill -0 returns success if the process exists
        assert!(
            status.map(|s| s.success()).unwrap_or(false),
            "caffeinate (pid {}) should still be alive watching daemon pid {}",
            caffeinate_pid,
            pid
        );
    }
}
