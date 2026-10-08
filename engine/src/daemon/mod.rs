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
//! managed wake signals enqueue `WorkPriority::Live` immediately. Retries come
//! from storage-v2 pending envelopes; there is no separate retry store.

use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use anyhow::Context as _;
use anyhow::Result;
use serde::Deserialize;
use serde_json::json;
use tokio::io::AsyncReadExt;
use tokio::sync::{mpsc, watch};
use tokio::task::JoinSet;

use crate::config::{self, ShipperConfig};
use crate::discovery::{self, ProviderConfig};
use crate::error_tracker::ErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::flight::FlightRecorder;
use crate::heartbeat;
use crate::managed_antigravity_scan;
use crate::managed_bridge_scan;
use crate::managed_claude_scan;
use crate::managed_cursor_helm_scan;
use crate::managed_omp_helm_scan;
use crate::managed_opencode_scan;
use crate::managed_pi_helm_scan;
use crate::managed_resume_scan;
use crate::outbox;
use crate::pipeline::compressor::CompressionAlgo;
use crate::scheduler::{
    shipping_max_in_flight, ObservationTrace, PathJob, PathScheduler, WorkPriority,
};
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::{require_storage_v2_cutover, StorageV2Capabilities};
use crate::shipping_stats::{RecentShipStatsTracker, ShipAttemptOutcome, ShipLane};
use crate::state::db::open_db;
use crate::state::db_pool::ConnectionPool;
use crate::state::file_state::FileState;
use crate::unmanaged_bindings;
use crate::watcher::{SessionWatcher, WatcherEvent};

mod archive_repair;
mod discovery_scans;
mod managed_observation;
mod path_jobs;
mod projection;
mod startup;
mod status_slots;
mod transcript_wake;

use self::archive_repair::*;
use self::discovery_scans::*;
use self::managed_observation::*;
use self::path_jobs::*;
use self::projection::*;
use self::startup::*;
use self::status_slots::*;
use self::transcript_wake::*;

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

const LOCAL_RETRY_DELAY_SECS: u64 = 5;
const LIVE_LOCAL_RETRY_DELAY: Duration = Duration::from_millis(500);
const STARTUP_RECONCILIATION_SCAN_DELAY: Duration = Duration::from_secs(30);
const LOCAL_STATUS_INTERVAL_SECS: u64 = 1;
/// How long the local status projection may take before it is worth reporting.
///
/// Derived from its own cadence rather than picked: the projection runs every
/// `LOCAL_STATUS_INTERVAL_SECS`, so spending a quarter of that on one pass is
/// the point where it stops being background work.
///
/// The previous value was a hardcoded 50ms against an observed p50 of 195ms, so
/// the warning fired on nearly every tick — 11,866 of one 20,000-line window,
/// and a 112MB log in a day. A warning that is always on carries no
/// information, and the volume buried the ones that did.
const LOCAL_STATUS_BUDGET_MS: u64 = LOCAL_STATUS_INTERVAL_SECS * 1000 / 4;
/// Minimum gap between budget-overrun reports.
///
/// A slow projection is a persistent condition, not an event. Reporting it once
/// a minute with a count of how many ticks were over preserves the signal and
/// removes the spam; reporting every tick did the opposite of both.
const LOCAL_STATUS_BUDGET_REPORT_INTERVAL: Duration = Duration::from_secs(60);
const MANAGED_OBSERVATION_INTERVAL_SECS: u64 = 5;
const STATUS_OWNER_SNAPSHOT_MAX_AGE: Duration =
    Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
// One PID per Console claim; reserve the other half for managed owner rows.
const STATUS_OWNER_REFRESH_BATCH_SIZE: usize =
    crate::process_identity::TARGETED_PROCESS_FACT_BATCH_MAX / 2;
// Each managed launch contributes at most two owner PIDs to the bounded probe.
const STATUS_MANAGED_OWNER_REFRESH_BATCH_SIZE: usize =
    crate::process_identity::TARGETED_PROCESS_FACT_BATCH_MAX / 4;
/// How long a managed-enumeration certificate stays usable on the wire.
///
/// The Runtime Host refuses a certificate older than its own bound, so a
/// projection that is only rebuilt when observations change would ship a stale
/// claim on every quiet beat -- the machine would look un-enumerated exactly
/// when it is idle. Re-enumerate well inside the host's window so every shipped
/// beat carries a claim that is still true.
const MANAGED_CERTIFICATE_KEEPALIVE_SECS: u64 = 45;
pub(crate) const MANAGED_FULL_RECONCILIATION_INTERVAL_SECS: u64 = 60;
const WAKE_GAP_THRESHOLD_SECS: u64 = 5;
const MACHINE_PRESENCE_INTERVAL_SECS: u64 = 60;
const SERVER_HEARTBEAT_INTERVAL_SECS: u64 = 60;
const FLIGHT_SAMPLE_INTERVAL_SECS: u64 = 5;
/// Status sends in flight. Sessions are independent; one that keeps failing
/// must not hold up everyone else's current status.
const STATUS_POST_CONCURRENCY: usize = 8;

const LOCAL_WORK_TICK_INTERVAL: Duration = Duration::from_millis(250);
const OUTBOX_DRAIN_INTERVAL: Duration = Duration::from_millis(100);
/// How long a runtime-outbox sweep that removed nothing keeps the next one
/// from starting. The sweep collapses byte-identical status repeats (the
/// 2026-09-17 flood was 9,382 of 9,394); a flood of distinct events gives it
/// nothing to remove, yet each pass re-read the whole directory, ~900 MB on
/// cinder every ~15 s on 2026-10-05, alongside delivery. Collection still
/// collapses repeats inside its own window meanwhile, so five minutes bounds
/// the waste without letting a real repeat flood grow unattended for long.
const RUNTIME_SWEEP_QUIET_AFTER_NOTHING: Duration = Duration::from_secs(300);
/// How long to coalesce a burst of phase-ledger writes before rebuilding the
/// local projection.
///
/// A phase write is what makes a turn boundary visible, but nothing used to
/// schedule a projection from one: `maybe_start_projection_build` fired only on
/// inventory change, reconciliation completion, or a queued rerun, so a
/// `thinking -> idle` transition waited for the 60s full reconciliation. Adding
/// activity to the snapshot digest without this only moved the ceiling from the
/// 300s heartbeat to that 60s timer.
///
/// Debouncing matters because providers are chatty: the Codex bridge posts a
/// phase for every item start, completion, and thread-status change, with no
/// same-phase suppression, plus a 30s keepalive. Projecting each one directly
/// would turn one tool-heavy turn into many full machine snapshots and host
/// POSTs. Matched to `WATCHER_FLUSH_INTERVAL`, which solves the same burst
/// problem for transcript appends.
const PHASE_PROJECTION_DEBOUNCE: Duration = Duration::from_millis(15);

const MANAGED_WAKE_FSEVENT_DEFER_WINDOW: Duration = Duration::from_secs(30);
const MANAGED_WAKE_FSEVENT_FALLBACK_DELAY: Duration = Duration::from_millis(250);
const MAX_TRANSCRIPT_WAKE_TRACKED_PATHS: usize = 4096;
const OFFLINE_CONNECT_FAILURE_THRESHOLD: u32 = 3;
// Stable telemetry strings for the retry/archive lane. Keep the wire names
// for historical engine-status/log readers, but keep code names explicit.
const FAILED_SHIPMENT_RETRY_CONTEXT: &str = "spool_replay";
const STORAGE_V2_PENDING_RETRY_OBSERVATION_SOURCE: &str = "storage_v2_pending";

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
    client: ShipperClient,
    tracker: ErrorTracker,
    ship_stats: RecentShipStatsTracker,
    limiter: std::sync::Arc<crate::scheduler::AdaptiveLimiter>,
    /// Reusable shipper-DB connections. Schema bootstrap has already run
    /// during `run()`; per-job code uses leases instead of `open_db`.
    db_pool: ConnectionPool,
    storage_v2: std::sync::Arc<StorageV2Capabilities>,
    shutdown: watch::Sender<bool>,
}

struct PathWorkersShutdown(watch::Sender<bool>);

impl Drop for PathWorkersShutdown {
    fn drop(&mut self) {
        self.0.send_replace(true);
    }
}

struct PathTaskResult {
    job: PathJob,
    events_shipped: usize,
    bytes_shipped: u64,
    had_connect_error: bool,
    rerun_priority: Option<WorkPriority>,
    local_retry_after: Option<Duration>,
    local_retry_priority: Option<WorkPriority>,
    reconciled_to_head: bool,
    processing_elapsed: Duration,
}

enum PathStorageV2ShipResult {
    Shipped(crate::storage_v2_shipper::StorageV2ShipOutcome),
    Current,
    WaitingOnClaim,
    Continue,
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
    result: Result<heartbeat::HeartbeatPostAck, String>,
    metrics: heartbeat::HeartbeatPostMetrics,
    sent_evidence_identities: Vec<heartbeat::EvidenceIdentity>,
    join_elapsed_ms: u64,
    task_elapsed_ms: u64,
}

struct PendingTruthHeartbeat {
    payload: heartbeat::HeartbeatPayload,
    signature: String,
}

struct MachinePresencePostResult {
    result: Result<bool, String>,
    task_elapsed_ms: u64,
}

struct OutboxCollectResult {
    presence: outbox::OutboxLocalDrainResult,
    elapsed_ms: u64,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct StatusOwnerKey {
    provider: String,
    session_id: String,
    run_id: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct StatusOwnerClaimIdentity {
    pid: Option<u32>,
    process_start_time: Option<String>,
    boot_id: Option<String>,
}

impl From<&crate::turn_claims::TurnClaim> for StatusOwnerClaimIdentity {
    fn from(claim: &crate::turn_claims::TurnClaim) -> Self {
        Self {
            pid: claim.pid,
            process_start_time: claim.process_start_time.clone(),
            boot_id: claim.boot_id.clone(),
        }
    }
}

#[derive(Clone, Default)]
struct StatusOwnerEvidence {
    active: HashSet<StatusOwnerKey>,
    ended: HashSet<StatusOwnerKey>,
    terminal_pending: HashSet<StatusOwnerKey>,
    claim_identities: HashMap<StatusOwnerKey, StatusOwnerClaimIdentity>,
}

impl StatusOwnerEvidence {
    fn merge(&mut self, other: Self) {
        for owner in other.active {
            if other.claim_identities.contains_key(&owner) {
                // A process-validated current claim is more specific than a
                // stale provider row naming the same session/run.
                self.ended.remove(&owner);
                self.terminal_pending.remove(&owner);
                self.active.insert(owner);
            } else if !self.ended.contains(&owner) && !self.terminal_pending.contains(&owner) {
                self.active.insert(owner);
            }
        }
        for owner in other.ended {
            self.active.remove(&owner);
            self.terminal_pending.remove(&owner);
            self.ended.insert(owner);
        }
        for owner in other.terminal_pending {
            self.active.remove(&owner);
            self.ended.remove(&owner);
            self.terminal_pending.insert(owner);
        }
        self.claim_identities.extend(other.claim_identities);
    }
}

/// One pass over the session status slots.
struct StatusSlotResult {
    slots: Vec<crate::status_slot::StatusSlot>,
    recorded: Vec<(String, (String, u64))>,
    owner_refresh_cursor: usize,
    managed_owner_refresh_cursor: usize,
    elapsed_ms: u64,
}
#[derive(Debug, Default)]
struct StatusPostResult {
    accepted: Vec<(String, (String, u64))>,
    rejected: Vec<StatusPostRejection>,
    /// The preview each accepted post left the host holding.
    previews: Vec<(String, crate::status_slot::PreviewIdentity)>,
}

#[derive(Debug, Clone)]
struct StatusPostRejection {
    session_id: String,
    version: (String, u64),
    changed: bool,
}

#[derive(Debug, Clone)]
struct RejectedStatusSlot {
    version: (String, u64),
    changed: bool,
    retry_at: Instant,
}

/// One session's status, and what the host already holds of it.
struct PendingStatus {
    slot: crate::status_slot::StatusSlot,
    changed: bool,
    /// The preview the host last accepted for this session. A slot still
    /// carrying it restates the phase without restating the preview.
    stated_preview: Option<crate::status_slot::PreviewIdentity>,
}

/// What the Runtime Host has accepted of each session's status. Nothing is
/// queued: an unsent or failed slot is simply sent again, with whatever value
/// it holds by then.
#[derive(Default)]
struct StatusLedger {
    /// The accepted version, and when the host accepted it.
    sent: HashMap<String, ((String, u64), Instant)>,
    /// Rejected observations remain durable slots; only their exact version
    /// waits for the assertion interval before retrying.
    rejected: HashMap<String, RejectedStatusSlot>,
    /// The live preview the host last accepted. The slot version says the
    /// status was rewritten, not that its preview changed: a provider
    /// restating its phase on a timer rewrites the slot with the preview it
    /// already showed.
    previews: HashMap<String, crate::status_slot::PreviewIdentity>,
}

impl StatusLedger {
    /// A session with no slot has no current status, so nothing here needs to
    /// remember it. A newer slot observation replaces a rejected one, while an
    /// unchanged rejection remains suppressed until its bounded retry time.
    fn retain_live(&mut self, slots: &[crate::status_slot::StatusSlot]) {
        self.sent
            .retain(|session_id, _| slots.iter().any(|slot| &slot.session_id == session_id));
        self.previews
            .retain(|session_id, _| slots.iter().any(|slot| &slot.session_id == session_id));
        self.rejected.retain(|session_id, rejected| {
            slots
                .iter()
                .any(|slot| &slot.session_id == session_id && slot.version() == rejected.version)
        });
    }

    /// A slot the host has already accepted is not resent as a change.
    /// Everything else is sent as it stands now, not as it stood when it
    /// changed, and a slot the host has accepted is still asserted on an
    /// interval.
    fn pending(
        &self,
        slots: Vec<crate::status_slot::StatusSlot>,
        now: Instant,
    ) -> Vec<PendingStatus> {
        slots
            .into_iter()
            .filter_map(|slot| {
                let changed = status_slot_pending(
                    &slot,
                    self.sent.get(&slot.session_id),
                    self.rejected.get(&slot.session_id),
                    now,
                )?;
                let stated_preview = self.previews.get(&slot.session_id).cloned();
                Some(PendingStatus {
                    slot,
                    changed,
                    stated_preview,
                })
            })
            .collect()
    }

    fn settle(&mut self, result: StatusPostResult, now: Instant) {
        for (session_id, version) in result.accepted {
            self.rejected.remove(&session_id);
            self.sent.insert(session_id, (version, now));
        }
        for (session_id, preview) in result.previews {
            self.previews.insert(session_id, preview);
        }
        for rejection in result.rejected {
            self.rejected.insert(
                rejection.session_id,
                RejectedStatusSlot {
                    version: rejection.version,
                    changed: rejection.changed,
                    retry_at: now + crate::status_slot::STATUS_ASSERTION_INTERVAL,
                },
            );
        }
    }
}

/// Runtime status collection is its own lane. It shares no gate with presence,
/// because a runtime backlog used to stop Claude presence and local-phase
/// collection outright: one flooded producer starved every other provider.
struct RuntimeCollectResult {
    posts: Vec<outbox::PendingRuntimeEventPost>,
    measurement: heartbeat::RuntimeEventOutboxSnapshot,
    elapsed_ms: u64,
    saturated: bool,
}

struct UnmanagedBindingRefreshResult {
    generation: u64,
    managed_observation_generation: u64,
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
    files: Vec<discovery::DiscoveredFile>,
    inventory: crate::state::source_inventory::SourceInventoryObservation,
    enqueue_files: bool,
    priority: WorkPriority,
    reason: &'static str,
}

struct OpenHistoryReconciliation {
    attempt_id: u64,
    remaining_paths: HashSet<PathBuf>,
}

/// Live Helm sessions as the disk guard sees them: the agent's own pids, as
/// verified by each provider scan. Work running under them is the session's.
fn disk_guard_sessions(
    observations: &ManagedObservationSnapshot,
) -> Vec<crate::disk_guard::SessionRoot> {
    fn live(pid: Option<u32>, alive: bool) -> Option<u32> {
        pid.filter(|pid| alive && *pid > 1)
    }
    fn root<const N: usize>(
        session_id: &str,
        provider: &str,
        pids: [Option<u32>; N],
    ) -> Option<crate::disk_guard::SessionRoot> {
        let pids: Vec<u32> = pids.into_iter().flatten().collect();
        (!pids.is_empty()).then(|| crate::disk_guard::SessionRoot {
            session_id: session_id.to_string(),
            provider: provider.to_string(),
            pids,
        })
    }
    let omp = observations.omp.iter().filter_map(|o| {
        let pids = [
            live(o.launcher_pid, o.launcher_alive),
            live(o.provider_pid, o.provider_alive),
        ];
        root(&o.session_id, "omp", pids)
    });
    let pi = observations.pi.iter().filter_map(|o| {
        let pids = [
            live(o.launcher_pid, o.launcher_alive),
            live(o.provider_pid, o.provider_alive),
        ];
        root(&o.session_id, "pi", pids)
    });
    let claude = observations.claude.iter().filter_map(|o| {
        let pids = [
            live(o.claude_pid, o.claude_alive),
            live(o.bridge_pid, o.bridge_alive),
        ];
        root(&o.session_id, "claude", pids)
    });
    let codex = observations.codex.iter().filter_map(|o| {
        let pids = [
            live(Some(o.bridge_pid), o.bridge_alive),
            live(o.app_server_pid, o.app_server_alive),
        ];
        root(&o.session_id, "codex", pids)
    });
    let cursor = observations.cursor.iter().filter_map(|o| {
        let pids = [
            live(o.launcher_pid, o.launcher_alive),
            live(o.cursor_pid, o.launcher_alive),
        ];
        root(&o.session_id, "cursor", pids)
    });
    let opencode = observations
        .opencode
        .iter()
        .filter_map(|o| root(&o.session_id, "opencode", [live(o.pid, o.server_alive)]));
    omp.chain(pi)
        .chain(claude)
        .chain(codex)
        .chain(cursor)
        .chain(opencode)
        .collect()
}

fn managed_provider_state_dirs() -> Vec<PathBuf> {
    let mut dirs: Vec<PathBuf> = [
        managed_bridge_scan::default_codex_bridge_state_dir(),
        managed_antigravity_scan::default_antigravity_state_dir(),
        managed_claude_scan::default_claude_channel_state_dir(),
        managed_opencode_scan::default_opencode_server_state_dir(),
        managed_cursor_helm_scan::default_cursor_helm_state_dir(),
        managed_pi_helm_scan::default_pi_helm_state_dir(),
        managed_omp_helm_scan::default_omp_helm_state_dir(),
    ]
    .into_iter()
    .flatten()
    .collect();
    if let Ok(longhouse_home) = config::get_longhouse_home() {
        for dir in managed_resume_scan::resume_contract_dirs(&longhouse_home) {
            // Create the evidence roots before starting notify. A provider may
            // write its first retained contract after daemon startup; watching
            // only an absent child directory would miss that transition.
            if let Err(error) = std::fs::create_dir_all(&dir) {
                tracing::warn!(
                    path = %dir.display(),
                    %error,
                    "Unable to create retained resume evidence directory"
                );
            }
            dirs.push(dir);
        }
    }
    dirs
}

/// Run the connect daemon. This function blocks until shutdown signal.
/// The daemon loop's state: every local `run` sets up that the loop or its
/// teardown reads. Fields are declared in reverse of the order `run` created
/// them, so they drop among themselves in the order the locals did. `state` is
/// declared last, so it drops before the locals that stay in `run`
/// (`_path_shutdown_guard`, `db_pool`, `_caffeinate`, the pinned timers); none of
/// those depends on a field's drop (the guard re-sends a shutdown the teardown
/// already sent). Keep new fields in reverse creation order.
struct DaemonState {
    sigint: tokio::signal::unix::Signal,
    sigterm: tokio::signal::unix::Signal,
    transcript_wake_task: Option<tokio::task::JoinHandle<()>>,
    transcript_wake_rx: mpsc::UnboundedReceiver<TranscriptWakeSignal>,
    control_channel_task: Option<tokio::task::JoinHandle<()>>,
    control_channel_status: crate::control_channel::ControlChannelStatus,
    status_path: PathBuf,
    runtime_events_outbox_dir: PathBuf,
    outbox_dir: PathBuf,
    daily_maintenance_tasks: JoinSet<()>,
    storage_maintenance_tasks: JoinSet<()>,
    unmanaged_binding_refresh_generation: Option<(u64, u64)>,
    unmanaged_binding_refresh_tasks: JoinSet<UnmanagedBindingRefreshResult>,
    machine_presence_post_tasks: JoinSet<MachinePresencePostResult>,
    host_link_poll_tasks: JoinSet<Result<()>>,
    last_serving_generation: u64,
    host_link_changed: watch::Receiver<crate::host_link::HostLinkStatus>,
    heartbeat_post_tasks: JoinSet<HeartbeatPostResult>,
    acknowledged_machine_evidence: heartbeat::AcknowledgedEvidenceHashes,
    runtime_outbox_retry_after: Option<Instant>,
    runtime_outbox_consecutive_failures: u32,
    runtime_outbox_post_tasks: JoinSet<(usize, usize, u64, u64)>,
    outbox_post_tasks: JoinSet<(usize, usize, u64, u64)>,
    status_recorded: HashMap<String, (String, u64)>,
    status_ledger: StatusLedger,
    status_post_tasks: JoinSet<StatusPostResult>,
    status_slot_tasks: JoinSet<StatusSlotResult>,
    runtime_sweep_quiet_until: Option<Instant>,
    runtime_sweep_tasks: JoinSet<outbox::RuntimeOutboxSweep>,
    runtime_collect_tasks: JoinSet<RuntimeCollectResult>,
    outbox_collect_tasks: JoinSet<OutboxCollectResult>,
    latest_runtime_event_outbox: heartbeat::RuntimeEventOutboxSnapshot,
    latest_transcript_wake_observed: HashMap<PathBuf, i64>,
    last_unmanaged_session_bindings: Option<Vec<heartbeat::UnmanagedSessionBinding>>,
    unmanaged_binding_refresh_failed: bool,
    last_projected_unmanaged_snapshot_complete: bool,
    certificate_keepalive_warned: bool,
    last_certified_at: Option<Instant>,
    last_managed_captured_at: String,
    last_projected_managed_snapshot_complete: bool,
    last_projected_managed_scan_partial: bool,
    managed_owner_refresh_cursor: usize,
    status_owner_refresh_cursor: usize,
    last_status_owners_at: Option<Instant>,
    last_status_owners: Arc<StatusOwnerEvidence>,
    last_projected_managed_observations: ManagedObservationSnapshot,
    last_resume_contracts: Option<Arc<[managed_resume_scan::ResumeContractObservation]>>,
    last_full_reconciled_at: Option<String>,
    projection_budget_reported_at: Option<Instant>,
    projection_worst_elapsed_ms: u64,
    projection_over_budget_ticks: u64,
    managed_observation_valid: bool,
    managed_observation_generation: u64,
    projection_generation: u64,
    projection_build_pending: bool,
    pending_full_reconciliation: bool,
    pending_wake_reconciliation: bool,
    wake_gap_detector: WakeGapDetector,
    managed_reconciliation: heartbeat::ProjectionReconciliation,
    heartbeat_transport: heartbeat::HeartbeatTransportStatus,
    last_status_projection: Option<heartbeat::StatusFileProjection>,
    pending_truth_heartbeat: Option<PendingTruthHeartbeat>,
    last_truth_heartbeat_at: Option<Instant>,
    session_snapshot_state: SessionSnapshotState,
    last_runtime_truth_signature: Option<String>,
    last_ship_at: Option<String>,
    offline: OfflineState,
    startup_reconciliation_pending: bool,
    last_phase_watermark: Option<i64>,
    phase_projection_pending: bool,
    update_http_client: reqwest::Client,
    update_check_timer: tokio::time::Interval,
    local_retry_timer: tokio::time::Interval,
    outbox_timer: tokio::time::Interval,
    disk_guard: Option<crate::disk_guard::DiskGuard>,
    disk_guard_timer: tokio::time::Interval,
    flight_sample_timer: tokio::time::Interval,
    machine_presence_timer: tokio::time::Interval,
    managed_full_reconciliation_timer: tokio::time::Interval,
    managed_observation_timer: tokio::time::Interval,
    local_status_timer: tokio::time::Interval,
    heartbeat_timer: tokio::time::Interval,
    prune_timer: tokio::time::Interval,
    health_timer: tokio::time::Interval,
    failed_ship_retry_timer: tokio::time::Interval,
    scope_timer: tokio::time::Interval,
    provider_roots_timer: tokio::time::Interval,
    fallback_timer: tokio::time::Interval,
    managed_full_reconciliation_not_before: Instant,
    shipping_progress: heartbeat::ShippingProgressObservation,
    deferred_retries: HashMap<PathBuf, DeferredRetry>,
    projection_build_tasks: JoinSet<ProjectionBuildResult>,
    opencode_title_refresh_tasks: JoinSet<Result<()>>,
    last_managed_observations: ManagedObservationSnapshot,
    last_reconcile_started_at: Instant,
    reconcile_tasks: JoinSet<ReconcileScanResult>,
    managed_observation_scan_tasks: JoinSet<ManagedObservationScanResult>,
    open_history_reconciliation: Option<OpenHistoryReconciliation>,
    discovery_tasks: JoinSet<DiscoveryTaskResult>,
    in_flight: JoinSet<Option<PathTaskResult>>,
    scheduler: PathScheduler,
    watcher: SessionWatcher,
    managed_state_dirs: Vec<PathBuf>,
    task_context: PathTaskContext,
    path_shutdown: watch::Sender<bool>,
    adaptive_limiter: Arc<crate::scheduler::AdaptiveLimiter>,
    flight_recorder: Option<FlightRecorder>,
    ship_stats: RecentShipStatsTracker,
    parse_tracker: RecentIssueTracker,
    pending_provider_roots: Vec<ProviderConfig>,
    providers: Vec<ProviderConfig>,
    host_link: crate::host_link::HostLink,
    client: ShipperClient,
    last_scope_fingerprint: Option<(SystemTime, u64)>,
    scope_dir: PathBuf,
    conn: rusqlite::Connection,
    projection_db_path: PathBuf,
}

pub async fn run(config: ConnectConfig) -> Result<()> {
    // 1. Open state DB
    let projection_db_path =
        crate::state::db::resolve_db_path(config.shipper_config.db_path.as_deref())?;
    let conn = open_db(Some(&projection_db_path)).inspect_err(|error| {
        record_startup_refusal(startup_storage_reason(error), &format!("{error:#}"));
    })?;
    crate::state::source_inventory::abandon_open_reconciliation(&conn).inspect_err(|error| {
        record_startup_refusal(startup_storage_reason(error), &format!("{error:#}"));
    })?;

    // 1b. Settle which local history this machine may ship before any scan
    // runs. A machine that already shipped keeps all of it; a new one starts
    // from now on, so old transcripts are imported only once someone chooses.
    let import_scope = crate::config::resolve_import_scope(&conn).inspect_err(|error| {
        record_startup_refusal("import_scope_invalid", &format!("{error:#}"));
    })?;
    tracing::info!(
        chosen_via = %import_scope.chosen_via,
        "Import scope: {}",
        import_scope.describe()
    );
    let scope_dir = crate::config::get_machine_dir()?;
    let last_scope_fingerprint = crate::import_scope::fingerprint(&scope_dir);

    // 2. Prune stale file_state entries (files deleted from disk, >30 days old)
    {
        let fs = FileState::new(&conn);
        match fs.prune_stale(30) {
            Ok(n) if n > 0 => tracing::info!("Pruned {} stale file_state entries", n),
            Ok(_) => {}
            Err(e) => tracing::warn!("file_state prune error: {}", e),
        }
    }

    // 3. Reconcile the frozen payload store before anything ships: delete files
    // no row references, and hold — never silently drop — a row whose payload is
    // missing, because that intent cannot be sent and can be re-prepared.
    match crate::state::pending_source_envelope::reconcile_frozen_payloads(&conn) {
        Ok(report) if report.orphans_removed > 0 || report.missing_blocked > 0 => {
            tracing::warn!(
                orphans_removed = report.orphans_removed,
                missing_blocked = report.missing_blocked,
                "Frozen payload reconciliation found work to do"
            );
        }
        Ok(_) => {}
        Err(error) => tracing::warn!(
            error = %format!("{error:#}"),
            "Frozen payload reconciliation failed; shipping continues"
        ),
    }

    // 3b. A block is the last engine's verdict, not this one's. Re-judge every
    // blocked source now instead of waiting out backoff earned under old logic.
    match crate::state::pending_source_envelope::wake_blocked_for_new_engine(&conn) {
        Ok(woken) if woken > 0 => {
            tracing::info!(
                woken,
                "Blocked sources due for re-examination by this engine"
            )
        }
        Ok(_) => {}
        Err(error) => tracing::warn!(
            error = %format!("{error:#}"),
            "Could not wake blocked sources; they keep their existing schedule"
        ),
    }

    // 4. Create HTTP client and settle the one transcript lane this engine has.
    // Storage-v2 is not a preference here: it is the only shipping protocol the
    // Machine Agent still implements. A host that cannot accept it gets a
    // refusal, not a shipping loop that would drop the user's history in
    // silence.
    let client = ShipperClient::with_compression(&config.shipper_config, config.algo)?;
    let host_link = client.host_link().clone();
    tracing::info!("Shipping to: {}", config.shipper_config.api_url);
    // A host that is briefly unreachable used to end the daemon before it
    // captured anything, so a restart during a deploy or a short network drop
    // cost the user their local history with no local record of it. Retry
    // inside a bounded window first; a host that is genuinely gone still gets a
    // refusal rather than a shipping loop that drops history in silence.
    let negotiated = match client
        .negotiate_storage_v2_at_startup(&config.shipper_config.machine_name)
        .await
    {
        Ok(negotiated) => negotiated,
        Err(error) => {
            let reason = if error.is::<crate::shipping::storage_v2::MachineIdentityMismatch>() {
                "machine_identity_mismatch"
            } else {
                "runtime_unavailable"
            };
            record_startup_refusal(reason, &format!("{error:#}"));
            return Err(error);
        }
    };
    let storage_v2 = match require_storage_v2_cutover(negotiated, &config.shipper_config.api_url) {
        Ok(capabilities) => {
            tracing::info!(
                tenant_id = %capabilities.tenant_id,
                "Runtime Host accepts storage-v2 transcript shipping"
            );
            std::sync::Arc::new(capabilities)
        }
        Err(error) => {
            tracing::error!("{error}");
            // Leave the reason where a human can find it. The daemon exits
            // before it ever writes engine-status.json, so without this the
            // only record is a launchd log and every local-health surface just
            // says the engine is missing -- which is true and useless.
            record_startup_refusal("runtime_protocol_unsupported", &format!("{error:#}"));
            return Err(error);
        }
    };

    // 4. Discover providers. ACP creates run files after the daemon starts;
    // establish its engine-owned root first so the watcher includes it.
    std::fs::create_dir_all(crate::config::get_agent_dir()?.join("cursor-acp-source"))?;
    let providers = discovery::get_providers();
    let pending_provider_roots = discovery::configured_provider_roots();
    if providers.is_empty() {
        tracing::warn!("No provider directories found — nothing to watch");
        return Ok(());
    }
    for p in &providers {
        tracing::info!("Provider {}: {}", p.name, p.root.display());
    }

    // 5. Create error tracker (shared across all ship operations)
    let tracker = ErrorTracker::new();
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
    let (path_shutdown, _) = watch::channel(false);
    let _path_shutdown_guard = PathWorkersShutdown(path_shutdown.clone());
    let task_context = PathTaskContext {
        client: client.clone(),
        tracker: tracker.clone(),
        ship_stats: ship_stats.clone(),
        limiter: std::sync::Arc::clone(&adaptive_limiter),
        db_pool: db_pool.clone(),
        storage_v2,
        shutdown: path_shutdown.clone(),
    };

    // 6. Start file watcher before catch-up work so live changes queue immediately.
    let managed_state_dirs = managed_provider_state_dirs();
    for state_dir in &managed_state_dirs {
        std::fs::create_dir_all(state_dir)?;
    }
    let watcher = SessionWatcher::new(&providers, &managed_state_dirs)?;
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
    // CPU count sizes local worker pools; shipping always reserves enough slots
    // for live work plus the configured backlog budget.
    let max_in_flight = shipping_max_in_flight(config.shipper_config.workers);
    let mut scheduler =
        PathScheduler::with_limiter(max_in_flight, std::sync::Arc::clone(&adaptive_limiter));
    let in_flight = JoinSet::new();
    let mut discovery_tasks: JoinSet<DiscoveryTaskResult> = JoinSet::new();
    start_inventory_task(&mut discovery_tasks, &providers);
    let open_history_reconciliation: Option<OpenHistoryReconciliation> = None;
    let mut managed_observation_scan_tasks: JoinSet<ManagedObservationScanResult> = JoinSet::new();
    let reconcile_tasks: JoinSet<ReconcileScanResult> = JoinSet::new();
    let last_reconcile_started_at = Instant::now();
    let last_managed_observations = ManagedObservationSnapshot::default();
    let opencode_title_refresh_tasks: JoinSet<Result<()>> = JoinSet::new();
    let projection_build_tasks: JoinSet<ProjectionBuildResult> = JoinSet::new();
    let mut deferred_retries = HashMap::new();
    let shipping_progress = heartbeat::ShippingProgressObservation::new(Instant::now());
    let startup_archive_mode =
        read_archive_repair_control().normalized_mode(config.archive_repair_mode);
    match queue_storage_v2_pending_retry_paths(
        &mut scheduler,
        &conn,
        config.archive_repair_mode,
        &mut deferred_retries,
    ) {
        Ok(queued) if queued > 0 => tracing::info!(
            queued,
            "Queued immutable storage-v2 exact retries at startup"
        ),
        Ok(_) => {}
        Err(error) => tracing::warn!(
            error = %error,
            "Unable to queue immutable storage-v2 exact retries at startup"
        ),
    }
    maybe_start_managed_observation_scan(
        projection_db_path.clone(),
        &mut managed_observation_scan_tasks,
        "startup",
        true,
        &last_managed_observations,
    );
    let managed_full_reconciliation_not_before =
        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
    tracing::info!(
        "Startup reconciliation deferred by {:?} (max {} concurrent)",
        STARTUP_RECONCILIATION_SCAN_DELAY,
        max_in_flight
    );

    // 8. Main event loop
    let fallback_interval = Duration::from_secs(config.fallback_scan_secs.max(10));
    let failed_ship_retry_interval = Duration::from_secs(config.spool_replay_secs.max(5));
    let health_check_interval = Duration::from_secs(60);
    let prune_interval = crate::state::recover::DAILY_MAINTENANCE_INTERVAL;
    let heartbeat_interval = Duration::from_secs(SERVER_HEARTBEAT_INTERVAL_SECS);

    let mut fallback_timer = tokio::time::interval(fallback_interval);
    fallback_timer.tick().await; // consume first immediate tick
                                 // A newly installed provider may create its first transcript store long
                                 // after startup. Refresh only the small root list, not the whole archive.
    let mut provider_roots_timer = tokio::time::interval(Duration::from_secs(1));
    provider_roots_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    provider_roots_timer.tick().await;
    // `longhouse machine scope` rewrites the scope file while this daemon runs.
    // Widening it must backfill what became eligible now, not at the next
    // periodic scan, so a change is noticed within seconds and rescanned.
    let mut scope_timer = tokio::time::interval(Duration::from_secs(2));
    scope_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    scope_timer.tick().await;

    let mut failed_ship_retry_timer = tokio::time::interval(failed_ship_retry_interval);
    failed_ship_retry_timer.tick().await; // consume first immediate tick

    let mut health_timer = tokio::time::interval(health_check_interval);
    health_timer.tick().await; // consume first immediate tick

    // Armed from the last completed pass, not from process start: an interval
    // timer that begins counting at startup is reset by every restart, and this
    // daemon restarts several times a day. See
    // `state::recover::daily_maintenance_delay`.
    let prune_timer = tokio::time::interval_at(
        tokio::time::Instant::now()
            + crate::state::recover::daily_maintenance_delay(
                &projection_db_path,
                chrono::Utc::now(),
            ),
        prune_interval,
    );

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
    let mut disk_guard_timer = tokio::time::interval(crate::disk_guard::TICK);
    disk_guard_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    let disk_guard = config::get_longhouse_home()
        .ok()
        .map(|home| crate::disk_guard::DiskGuard::new(home, crate::disk_guard::state_path()));

    let mut outbox_timer = tokio::time::interval(OUTBOX_DRAIN_INTERVAL);
    outbox_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    outbox_timer.tick().await; // consume first immediate tick
    let mut local_retry_timer = tokio::time::interval(LOCAL_WORK_TICK_INTERVAL);
    local_retry_timer.tick().await; // consume first immediate tick
    let mut update_check_timer = tokio::time::interval(crate::update::UPDATE_CHECK_INTERVAL);
    update_check_timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
    // A dedicated client: this talks to GitHub, not the Runtime Host, and must
    // follow redirects without carrying any Longhouse credential.
    let update_http_client = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::limited(5))
        .user_agent(concat!("longhouse-engine/", env!("CARGO_PKG_VERSION")))
        .build()
        .unwrap_or_default();
    // Keep the first immediate tick: a daemon that has just started is exactly
    // when a stale machine most needs to learn it is behind.
    // Armed by a phase-ledger write, cleared when the projection is scheduled.
    // Coalesces a burst into one build; periodic reconciliation stays the
    // repair path for anything a notification misses.
    let phase_projection_timer = tokio::time::sleep(PHASE_PROJECTION_DEBOUNCE);
    tokio::pin!(phase_projection_timer);
    let phase_projection_pending = false;
    // Highest accepted phase-ledger revision, to detect out-of-process writes.
    let last_phase_watermark: Option<i64> = None;
    let startup_reconciliation_timer = tokio::time::sleep(STARTUP_RECONCILIATION_SCAN_DELAY);
    tokio::pin!(startup_reconciliation_timer);
    let startup_reconciliation_pending = !startup_archive_mode.is_paused();

    let offline = OfflineState::new();
    let last_ship_at: Option<String> = None;
    let last_runtime_truth_signature: Option<String> = None;
    let session_snapshot_state = SessionSnapshotState::default();
    let last_truth_heartbeat_at: Option<Instant> = None;
    let pending_truth_heartbeat: Option<PendingTruthHeartbeat> = None;
    let last_status_projection: Option<heartbeat::StatusFileProjection> = None;
    let heartbeat_transport = heartbeat::HeartbeatTransportStatus::default();
    let managed_reconciliation =
        heartbeat::ProjectionReconciliation::running("startup", chrono::Utc::now().to_rfc3339());
    let wake_gap_detector = WakeGapDetector::new();
    let pending_wake_reconciliation = false;
    let pending_full_reconciliation = false;
    let projection_build_pending = false;
    let projection_generation = 0_u64;
    let managed_observation_generation = 0_u64;
    // Cached rebuilds cannot turn a failed observation into fresh evidence.
    // Only a subsequent valid managed scan makes these inputs publishable.
    let managed_observation_valid = false;
    // Budget-overrun reporting state: how many ticks were over since the last
    // report, the worst one seen, and when we last said anything.
    let projection_over_budget_ticks = 0_u64;
    let projection_worst_elapsed_ms = 0_u64;
    let projection_budget_reported_at: Option<Instant> = None;
    let last_full_reconciled_at: Option<String> = None;
    let last_resume_contracts: Option<Arc<[managed_resume_scan::ResumeContractObservation]>> = None;
    let last_projected_managed_observations = ManagedObservationSnapshot::default();
    let last_status_owners = Arc::new(StatusOwnerEvidence::default());
    let last_status_owners_at: Option<Instant> = None;
    let status_owner_refresh_cursor = 0_usize;
    let managed_owner_refresh_cursor = 0_usize;
    let last_projected_managed_scan_partial = false;
    let last_projected_managed_snapshot_complete = false;
    let last_managed_captured_at = String::new();
    let last_certified_at: Option<Instant> = None;
    let certificate_keepalive_warned = false;
    let last_projected_unmanaged_snapshot_complete = false;
    let unmanaged_binding_refresh_failed = false;
    let last_unmanaged_session_bindings: Option<Vec<heartbeat::UnmanagedSessionBinding>> = None;
    let latest_transcript_wake_observed: HashMap<PathBuf, i64> = HashMap::new();
    let latest_runtime_event_outbox = heartbeat::RuntimeEventOutboxSnapshot::default();
    let outbox_collect_tasks: JoinSet<OutboxCollectResult> = JoinSet::new();
    let runtime_collect_tasks: JoinSet<RuntimeCollectResult> = JoinSet::new();
    let runtime_sweep_tasks: JoinSet<outbox::RuntimeOutboxSweep> = JoinSet::new();
    // A sweep that removed nothing found a flood of distinct events, which it
    // cannot reduce; re-reading the whole directory again in seconds only
    // competes with delivery for the disk. See RUNTIME_SWEEP_QUIET_AFTER_NOTHING.
    let runtime_sweep_quiet_until: Option<Instant> = None;
    let status_slot_tasks: JoinSet<StatusSlotResult> = JoinSet::new();
    let status_post_tasks: JoinSet<StatusPostResult> = JoinSet::new();
    let status_ledger = StatusLedger::default();
    // What the local phase ledger already holds. Recording an unchanged phase
    // every 100ms bumps its revision, and the projection debounce watches that
    // watermark: the daemon would schedule a rebuild forever.
    let status_recorded: HashMap<String, (String, u64)> = HashMap::new();
    let outbox_post_tasks: JoinSet<(usize, usize, u64, u64)> = JoinSet::new();
    let runtime_outbox_post_tasks: JoinSet<(usize, usize, u64, u64)> = JoinSet::new();
    let runtime_outbox_consecutive_failures: u32 = 0;
    let runtime_outbox_retry_after: Option<Instant> = None;
    let acknowledged_machine_evidence = heartbeat::AcknowledgedEvidenceHashes::default();
    let heartbeat_post_tasks: JoinSet<HeartbeatPostResult> = JoinSet::new();
    let host_link_changed = host_link.subscribe();
    let last_serving_generation = 0_u64;
    let host_link_poll_tasks: JoinSet<Result<()>> = JoinSet::new();
    let machine_presence_post_tasks: JoinSet<MachinePresencePostResult> = JoinSet::new();
    let unmanaged_binding_refresh_tasks: JoinSet<UnmanagedBindingRefreshResult> = JoinSet::new();
    let unmanaged_binding_refresh_generation: Option<(u64, u64)> = None;
    let storage_maintenance_tasks: JoinSet<()> = JoinSet::new();
    // The daily pass gets its own set: sharing one with the cursor-drain work
    // meant a due pass could be skipped outright when a drain was in flight,
    // and the timer would not come back for another day.
    let daily_maintenance_tasks: JoinSet<()> = JoinSet::new();

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
        host_link.clone(),
    );
    // Anonymous, machine-global Console warmth: one initialized stock Codex
    // app-server regardless of how many durable sessions exist. Failure is a
    // measured cold-path miss and never disables the control channel.
    tokio::spawn(crate::codex_exec::prewarm_codex_console_workers());
    let (transcript_wake_tx, transcript_wake_rx) = mpsc::unbounded_channel();
    let transcript_wake_task = spawn_transcript_wake_listener(transcript_wake_tx)?;
    // These must outlive the loop. tokio delivers a signal only to receivers
    // that already exist, so building them inside the select rebuilt them on
    // every iteration and dropped the previous ones. A SIGTERM landing while
    // this loop body was busy -- and it does synchronous filesystem and SQLite
    // work -- therefore reached nobody, and the stream built on the next
    // iteration had no idea it had happened. The Machine Agent ignored its
    // first SIGTERM and exited only on a second, because the second arrived
    // while a stream was being polled. A long-lived stream latches the signal
    // instead, so the next poll sees it no matter when it arrived.
    let sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        .context("install SIGTERM handler")?;
    let sigint = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())
        .context("install SIGINT handler")?;
    let mut state = DaemonState {
        sigint,
        sigterm,
        transcript_wake_task,
        transcript_wake_rx,
        control_channel_task,
        control_channel_status,
        status_path,
        runtime_events_outbox_dir,
        outbox_dir,
        daily_maintenance_tasks,
        storage_maintenance_tasks,
        unmanaged_binding_refresh_generation,
        unmanaged_binding_refresh_tasks,
        machine_presence_post_tasks,
        host_link_poll_tasks,
        last_serving_generation,
        host_link_changed,
        heartbeat_post_tasks,
        acknowledged_machine_evidence,
        runtime_outbox_retry_after,
        runtime_outbox_consecutive_failures,
        runtime_outbox_post_tasks,
        outbox_post_tasks,
        status_recorded,
        status_ledger,
        status_post_tasks,
        status_slot_tasks,
        runtime_sweep_quiet_until,
        runtime_sweep_tasks,
        runtime_collect_tasks,
        outbox_collect_tasks,
        latest_runtime_event_outbox,
        latest_transcript_wake_observed,
        last_unmanaged_session_bindings,
        unmanaged_binding_refresh_failed,
        last_projected_unmanaged_snapshot_complete,
        certificate_keepalive_warned,
        last_certified_at,
        last_managed_captured_at,
        last_projected_managed_snapshot_complete,
        last_projected_managed_scan_partial,
        managed_owner_refresh_cursor,
        status_owner_refresh_cursor,
        last_status_owners_at,
        last_status_owners,
        last_projected_managed_observations,
        last_resume_contracts,
        last_full_reconciled_at,
        projection_budget_reported_at,
        projection_worst_elapsed_ms,
        projection_over_budget_ticks,
        managed_observation_valid,
        managed_observation_generation,
        projection_generation,
        projection_build_pending,
        pending_full_reconciliation,
        pending_wake_reconciliation,
        wake_gap_detector,
        managed_reconciliation,
        heartbeat_transport,
        last_status_projection,
        pending_truth_heartbeat,
        last_truth_heartbeat_at,
        session_snapshot_state,
        last_runtime_truth_signature,
        last_ship_at,
        offline,
        startup_reconciliation_pending,
        last_phase_watermark,
        phase_projection_pending,
        update_http_client,
        update_check_timer,
        local_retry_timer,
        outbox_timer,
        disk_guard,
        disk_guard_timer,
        flight_sample_timer,
        machine_presence_timer,
        managed_full_reconciliation_timer,
        managed_observation_timer,
        local_status_timer,
        heartbeat_timer,
        prune_timer,
        health_timer,
        failed_ship_retry_timer,
        scope_timer,
        provider_roots_timer,
        fallback_timer,
        managed_full_reconciliation_not_before,
        shipping_progress,
        deferred_retries,
        projection_build_tasks,
        opencode_title_refresh_tasks,
        last_managed_observations,
        last_reconcile_started_at,
        reconcile_tasks,
        managed_observation_scan_tasks,
        open_history_reconciliation,
        discovery_tasks,
        in_flight,
        scheduler,
        watcher,
        managed_state_dirs,
        task_context,
        path_shutdown,
        adaptive_limiter,
        flight_recorder,
        ship_stats,
        parse_tracker,
        pending_provider_roots,
        providers,
        host_link,
        client,
        last_scope_fingerprint,
        scope_dir,
        conn,
        projection_db_path,
    };
    loop {
        match queue_failed_shipment_retries_if_idle(
            &mut state.scheduler,
            &state.conn,
            state.offline.is_offline,
            config.archive_repair_mode,
            &mut state.deferred_retries,
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
        pump_ready_local_work(
            &mut state.scheduler,
            &mut state.in_flight,
            &state.task_context,
            &mut state.deferred_retries,
            &mut state.shipping_progress,
            state.offline.is_offline,
            archive_repair_is_paused(config.archive_repair_mode),
        );

        // Drain finished reconciliation before handling any event this tick, so
        // a sustained event stream cannot starve it. The expensive half — walking
        // the bound working set, statting each source, reading cursors — runs in a
        // blocking task; only the enqueue happens on the loop.
        while let Some(joined) = state.reconcile_tasks.try_join_next() {
            match joined {
                Ok(result) => {
                    if let Some(error) = result.error.as_deref() {
                        tracing::warn!(error, "bound-source reconciliation failed");
                    }
                    if result.scanned > 0 {
                        tracing::debug!(
                            scanned = result.scanned,
                            behind = result.targets.len(),
                            elapsed_ms = result.elapsed_ms,
                            "bound-source reconciliation"
                        );
                    }
                    for target in result.targets {
                        let Some(provider) = discovery::canonical_provider_name(&target.provider)
                        else {
                            continue;
                        };
                        if !retry_admission_open(&target.path, &mut state.deferred_retries) {
                            continue;
                        }
                        let observed_at_ms = now_ms();
                        // Debug, not info: a source that stays behind is
                        // re-decided every tick, and a persistent condition
                        // logged per tick is how a log stops being readable.
                        // Starvation stays visible through the coverage gate,
                        // which reports per-class coverage and exit codes.
                        tracing::debug!(
                            provider,
                            path = %target.path.display(),
                            lag_bytes = target.lag_bytes,
                            never_shipped = target.never_shipped,
                            "Bound source is behind; scheduling a shipment"
                        );
                        state.scheduler.enqueue_observed_window(
                            target.path,
                            provider,
                            WorkPriority::Live,
                            "reconcile",
                            observed_at_ms,
                            observed_at_ms,
                        );
                    }
                }
                Err(error) => {
                    tracing::warn!(error = %error, "bound-source reconciliation task failed");
                }
            }
        }
        if state.reconcile_tasks.is_empty()
            && state.last_reconcile_started_at.elapsed() >= RECONCILE_INTERVAL
        {
            state.last_reconcile_started_at = Instant::now();
            maybe_start_reconcile_scan(
                &mut state.reconcile_tasks,
                config.shipper_config.db_path.clone(),
            );
        }

        tokio::select! {
            biased;

            // Shutdown signals
            _ = state.sigterm.recv() => {
                tracing::info!("Shutdown signal received, exiting gracefully...");
                break;
            }
            _ = state.sigint.recv() => {
                tracing::info!("Shutdown signal received, exiting gracefully...");
                break;
            }

            // Managed transcript wakes are the lowest-latency completion lane.
            // Periodic local status/outbox work can do synchronous filesystem
            // and SQLite reads, so do not let a ready timer win the select race
            // while a turn-completion wake is already waiting.
            Some(signal) = state.transcript_wake_rx.recv() => {
                state.on_transcript_wake(&config, signal);
            }

            task_result = state.in_flight.join_next(), if state.scheduler.has_in_flight() => {
                state.on_path_task_done(task_result)?;
            }

            discovery_result = state.discovery_tasks.join_next(), if !state.discovery_tasks.is_empty() => {
                state.on_discovery_done(&config, discovery_result);
            }

            outbox_collect_result = state.outbox_collect_tasks.join_next(), if !state.outbox_collect_tasks.is_empty() => {
                state.on_outbox_collect_done(phase_projection_timer.as_mut(), outbox_collect_result).await;
            }

            status_slot_result = state.status_slot_tasks.join_next(), if !state.status_slot_tasks.is_empty() => {
                state.on_status_slot_done(status_slot_result).await;
            }
            status_post_result = state.status_post_tasks.join_next(), if !state.status_post_tasks.is_empty() => {
                state.on_status_post_done(status_post_result);
            }

            runtime_collect_result = state.runtime_collect_tasks.join_next(), if !state.runtime_collect_tasks.is_empty() => {
                state.on_runtime_collect_done(runtime_collect_result).await;
            }

            runtime_sweep_result = state.runtime_sweep_tasks.join_next(), if !state.runtime_sweep_tasks.is_empty() => {
                state.on_runtime_sweep_done(runtime_sweep_result);
            }

            outbox_post_result = state.outbox_post_tasks.join_next(), if !state.outbox_post_tasks.is_empty() => {
                state.on_outbox_post_done(outbox_post_result);
            }

            runtime_outbox_post_result = state.runtime_outbox_post_tasks.join_next(), if !state.runtime_outbox_post_tasks.is_empty() => {
                state.on_runtime_outbox_post_done(runtime_outbox_post_result);
            }

            heartbeat_post_result = state.heartbeat_post_tasks.join_next(), if !state.heartbeat_post_tasks.is_empty() => {
                state.on_heartbeat_post_done(heartbeat_post_result);
            }

            machine_presence_post_result = state.machine_presence_post_tasks.join_next(), if !state.machine_presence_post_tasks.is_empty() => {
                state.on_machine_presence_post_done(machine_presence_post_result);
            }
            host_link_poll_result = state.host_link_poll_tasks.join_next(), if !state.host_link_poll_tasks.is_empty() => {
                state.on_host_link_poll_done(host_link_poll_result);
            }
            host_link_event = state.host_link_changed.changed() => {
                state.on_host_link_changed(host_link_event);
            }
            _ = state.storage_maintenance_tasks.join_next(), if !state.storage_maintenance_tasks.is_empty() => {}
            _ = state.daily_maintenance_tasks.join_next(), if !state.daily_maintenance_tasks.is_empty() => {}

            unmanaged_binding_refresh_result = state.unmanaged_binding_refresh_tasks.join_next(), if !state.unmanaged_binding_refresh_tasks.is_empty() => {
                state.on_unmanaged_binding_refresh_done(&config, unmanaged_binding_refresh_result);
            }

            managed_observation_scan_result = state.managed_observation_scan_tasks.join_next(), if !state.managed_observation_scan_tasks.is_empty() => {
                state.on_managed_observation_scan_done(&config, managed_observation_scan_result);
            }

            opencode_title_refresh_result = state.opencode_title_refresh_tasks.join_next(), if !state.opencode_title_refresh_tasks.is_empty() => {
                state.on_opencode_title_refresh_done(opencode_title_refresh_result);
            }

            projection_build_result = state.projection_build_tasks.join_next(), if !state.projection_build_tasks.is_empty() => {
                state.on_projection_build_done(&config, projection_build_result);
            }

            // Debounced projection rebuild for phase-ledger writes. If a build
            // is already running, `projection_build_pending` makes it run
            // exactly once more afterwards, so a phase observed mid-build is
            // never stranded until the next reconciliation.
            _ = &mut phase_projection_timer, if state.phase_projection_pending => {
                state.on_phase_projection_due(&config);
            }

            _ = &mut startup_reconciliation_timer, if state.startup_reconciliation_pending && !state.offline.is_offline => {
                state.on_startup_reconciliation_due(&config);
            }

            // Health check when offline (every 60s)
            _ = state.health_timer.tick(), if state.offline.is_offline => {
                state.on_health_tick().await;
            }

            // Live transcript lane (primary path): provider file appends enqueue
            // WorkPriority::Live. Managed wake signals can pre-empt the small
            // filesystem coalescing window.
            Some(first_event) = state.watcher.next_event() => {
                state.on_watcher_event(&config, first_event).await;
            }

            _ = state.scope_timer.tick() => {
                state.on_scope_tick();
            }

            _ = state.provider_roots_timer.tick(), if !state.pending_provider_roots.is_empty() => {
                state.on_provider_roots_tick();
            }

            // Periodic reconciliation scan — repair missed file-watch work after
            // restarts, sleeps, or dropped OS notifications.
            _ = state.fallback_timer.tick(), if !state.offline.is_offline => {
                state.on_fallback_tick(&config);
            }

            // Retry lane: storage-v2 pending envelopes. Never the primary live
            // transcript lane.
            _ = state.failed_ship_retry_timer.tick(), if !state.offline.is_offline => {
                state.on_failed_ship_retry_tick(&config);
            }

            // Outbox drain: presence events written by hooks. These are runtime
            // overlay signals only; transcript shipping is owned by filesystem
            // events plus reconciliation scans.
            _ = state.outbox_timer.tick() => {
                state.on_outbox_tick(&config, phase_projection_timer.as_mut());
            }

            // Wake the loop when delayed local retry work may now be ready.
            _ = state.local_retry_timer.tick(), if !state.deferred_retries.is_empty() => {}

            _ = state.flight_sample_timer.tick(), if state.flight_recorder.is_some() => {
                state.on_flight_sample_tick(&config);
            }

            _ = state.disk_guard_timer.tick(), if state.disk_guard.is_some() => {
                state.on_disk_guard_tick(&config).await;
            }

            // Daily: prune stale file_state and session_binding entries
            _ = state.update_check_timer.tick() => {
                state.on_update_check_tick().await;
            }
            _ = state.prune_timer.tick() => {
                state.on_prune_tick();
            }

            // Frequent local status file refresh for ambient UX and debugging
            _ = state.local_status_timer.tick() => {
                state.on_local_status_tick().await;
            }

            _ = state.managed_full_reconciliation_timer.tick() => {
                state.on_managed_full_reconciliation_tick();
            }

            _ = state.managed_observation_timer.tick() => {
                state.on_managed_observation_tick();
            }

            _ = state.machine_presence_timer.tick() => {
                state.on_machine_presence_tick();
            }

            _ = tokio::time::sleep_until(tokio::time::Instant::from_std(
                truth_heartbeat_due_at(state.last_truth_heartbeat_at, Instant::now()),
            )), if state.pending_truth_heartbeat.is_some() && state.heartbeat_post_tasks.is_empty() => {
                state.on_truth_heartbeat_due();
            }
            // Periodic server heartbeat
            _ = state.heartbeat_timer.tick() => {
                state.on_heartbeat_tick();
            }
        }
    }

    state.path_shutdown.send_replace(true);
    state.in_flight.abort_all();
    while state.in_flight.join_next().await.is_some() {}
    if let Some(task) = state.control_channel_task {
        task.abort();
    }
    if let Some(task) = state.transcript_wake_task {
        task.abort();
    }
    crate::codex_exec::shutdown_codex_console_worker_pool().await;
    tracing::info!("Daemon shutdown complete");
    Ok(())
}

impl DaemonState {
    fn on_transcript_wake(&mut self, config: &ConnectConfig, signal: TranscriptWakeSignal) {
        if enqueue_transcript_wake_signal(
            &self.conn,
            &mut self.scheduler,
            &mut self.latest_transcript_wake_observed,
            &mut self.deferred_retries,
            signal,
        )
        .is_some()
        {
            pump_ready_local_work(
                &mut self.scheduler,
                &mut self.in_flight,
                &self.task_context,
                &mut self.deferred_retries,
                &mut self.shipping_progress,
                self.offline.is_offline,
                archive_repair_is_paused(config.archive_repair_mode),
            );
        }
    }
    fn on_path_task_done(
        &mut self,
        task_result: Option<Result<Option<PathTaskResult>, tokio::task::JoinError>>,
    ) -> Result<()> {
        {
            match task_result {
                Some(Ok(Some(result))) => {
                    let retry_path = result.job.path.clone();
                    let retry_provider = result.job.provider;
                    let reconciled_to_head = result.reconciled_to_head
                        && result.rerun_priority.is_none()
                        && result.local_retry_after.is_none();
                    if result.local_retry_after.is_some() {
                        self.scheduler.complete_without_rerun(&retry_path);
                    } else {
                        self.scheduler.complete(&retry_path, result.rerun_priority);
                    }
                    if let Some(delay) = result.local_retry_after {
                        let priority = result.local_retry_priority.unwrap_or(result.job.priority);
                        self.deferred_retries.insert(
                            retry_path.clone(),
                            DeferredRetry {
                                due_at: Instant::now() + delay,
                                provider: retry_provider,
                                priority,
                                observation: result.job.observation.clone(),
                            },
                        );
                    }
                    // Checking an unchanged source head advances reconciliation
                    // without creating a new upload receipt.
                    if reconciled_to_head || result.events_shipped > 0 || result.bytes_shipped > 0 {
                        self.shipping_progress.record_progress(Instant::now());
                    }
                    if result.had_connect_error {
                        if self.offline.record_connect_error() {
                            self.shipping_progress.reset_after_sleep(Instant::now());
                            tracing::warn!(
                                    threshold = OFFLINE_CONNECT_FAILURE_THRESHOLD,
                                    "Connection error threshold reached while processing {} — entering offline mode",
                                    result.job.path.display()
                                );
                        } else {
                            tracing::warn!(
                                    consecutive_connect_errors = self.offline.consecutive_connect_failures,
                                    threshold = OFFLINE_CONNECT_FAILURE_THRESHOLD,
                                    "Connection error while processing {}; keeping local shipping active",
                                    result.job.path.display()
                                );
                        }
                    } else if result.events_shipped > 0 || result.bytes_shipped > 0 {
                        self.last_ship_at = Some(chrono::Utc::now().to_rfc3339());
                        if let Some(duration) = self.offline.mark_online() {
                            self.last_runtime_truth_signature = None;
                            tracing::info!(
                                "Back online after {:.0}s — resuming shipping",
                                duration.as_secs_f64()
                            );
                        }
                    }
                    if reconciled_to_head
                        && !self.scheduler.has_path(&retry_path)
                        && !self.deferred_retries.contains_key(&retry_path)
                    {
                        if let Some(open) = self.open_history_reconciliation.as_mut() {
                            open.remaining_paths.remove(&retry_path);
                        }
                    }
                    maybe_seal_history_reconciliation(
                        &mut self.conn,
                        &mut self.open_history_reconciliation,
                        &self.scheduler,
                        &self.deferred_retries,
                        self.discovery_tasks.is_empty(),
                    );
                }
                Some(Err(e)) => {
                    return Err(anyhow::anyhow!("path task failed: {}", e));
                }
                None => {}
                Some(Ok(None)) => {}
            }
        }
        Ok(())
    }
    fn on_discovery_done(
        &mut self,
        config: &ConnectConfig,
        discovery_result: Option<Result<DiscoveryTaskResult, tokio::task::JoinError>>,
    ) {
        match discovery_result {
            Some(Ok(result)) => {
                let reconciliation_paths = result
                    .enqueue_files
                    .then(|| result.files.iter().map(|file| file.path.clone()).collect());
                let previous_inventory_generation =
                    crate::state::source_inventory::load_inventory(&self.conn)
                        .ok()
                        .flatten()
                        .map(|inventory| inventory.generation);
                let inventory =
                    crate::state::source_inventory::persist_inventory(&self.conn, result.inventory);
                if result.enqueue_files {
                    let queued = enqueue_discovered_files(
                        &mut self.scheduler,
                        result.files,
                        result.priority,
                        &mut self.deferred_retries,
                    );
                    tracing::debug!("Queued {} paths for {}", queued, result.reason);
                }
                match inventory {
                    Ok(snapshot) => {
                        tracing::info!(
                            generation = snapshot.generation,
                            source_count = snapshot.source_count,
                            footprint_bytes = snapshot.footprint_bytes,
                            scan_error_count = snapshot.scan_error_count,
                            "Updated durable transcript source inventory"
                        );
                        if let Some(remaining_paths) = reconciliation_paths {
                            match crate::state::source_inventory::begin_reconciliation(
                                &mut self.conn,
                                &snapshot,
                            ) {
                                Ok(Some(attempt)) => {
                                    self.open_history_reconciliation =
                                        Some(OpenHistoryReconciliation {
                                            attempt_id: attempt.attempt_id,
                                            remaining_paths,
                                        });
                                }
                                Ok(None) => self.open_history_reconciliation = None,
                                Err(error) => {
                                    self.open_history_reconciliation = None;
                                    tracing::warn!(
                                        error = %error,
                                        "Failed to begin transcript reconciliation seal"
                                    );
                                }
                            }
                            maybe_seal_history_reconciliation(
                                &mut self.conn,
                                &mut self.open_history_reconciliation,
                                &self.scheduler,
                                &self.deferred_retries,
                                self.discovery_tasks.is_empty(),
                            );
                        }
                        if inventory_change_requires_projection(
                            previous_inventory_generation,
                            snapshot.generation,
                            self.last_unmanaged_session_bindings.is_some(),
                        ) && self.managed_observation_valid
                        {
                            self.projection_generation =
                                self.projection_generation.saturating_add(1);
                            let input = ProjectionBuildInput {
                                generation: self.projection_generation,
                                managed_observation_generation: self.managed_observation_generation,
                                managed_scan_partial: self.last_projected_managed_scan_partial,
                                managed_snapshot_complete: self
                                    .last_projected_managed_snapshot_complete,
                                managed_captured_at: self.last_managed_captured_at.clone(),
                                unmanaged_snapshot_complete: self
                                    .last_projected_unmanaged_snapshot_complete,
                                db_path: self.projection_db_path.clone(),
                                parse_tracker: self.parse_tracker.clone(),
                                ship_stats: self.ship_stats.clone(),
                                is_offline: self.offline.is_offline,
                                last_ship_at: self.last_ship_at.clone(),
                                machine_id: config.shipper_config.machine_name.clone(),
                                managed: self.last_projected_managed_observations.clone(),
                                unmanaged: self
                                    .last_unmanaged_session_bindings
                                    .clone()
                                    .unwrap_or_default(),
                                limiter: self.adaptive_limiter.snapshot(),
                                scheduler: self.scheduler.snapshot(),
                                archive_repair_mode: config.archive_repair_mode,
                                last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                                continuation: self.last_resume_contracts.clone(),
                                session_snapshot_state: self.session_snapshot_state.clone(),
                            };
                            if !maybe_start_projection_build(
                                &mut self.projection_build_tasks,
                                input,
                            ) {
                                self.projection_build_pending = true;
                            }
                        }
                    }
                    Err(error) => tracing::warn!(
                        error = %error,
                        "Failed to persist transcript source inventory"
                    ),
                }
            }
            Some(Err(e)) => {
                tracing::warn!("Background discovery task failed: {}", e);
            }
            None => {}
        }
    }
    async fn on_outbox_collect_done(
        &mut self,
        mut phase_projection_timer: std::pin::Pin<&mut tokio::time::Sleep>,
        outbox_collect_result: Option<Result<OutboxCollectResult, tokio::task::JoinError>>,
    ) {
        match outbox_collect_result {
            Some(Ok(result)) => {
                if result.elapsed_ms > 100 {
                    tracing::warn!(
                        elapsed_ms = result.elapsed_ms,
                        presence_posts = result.presence.posts.len(),
                        "Outbox collection was slow"
                    );
                }
                if !result.presence.signals.is_empty() {
                    tracing::debug!(
                        signal_count = result.presence.signals.len(),
                        "Ignoring hook outbox transcript catch-up signals"
                    );
                    // The phase ledger just moved, which is the only
                    // evidence a turn boundary produces. Arm the
                    // debounce rather than projecting per signal.
                    if arm_phase_projection(&mut self.phase_projection_pending) {
                        phase_projection_timer
                            .as_mut()
                            .reset(tokio::time::Instant::now() + PHASE_PROJECTION_DEBOUNCE);
                    }
                }
                if !result.presence.posts.is_empty() {
                    if self.outbox_post_tasks.is_empty() {
                        let client = self.client.clone();
                        let posts = result.presence.posts;
                        let post_count = posts.len();
                        // spawn, not spawn_local: the wrapper only awaits
                        // the worker task, and a local task is polled on
                        // the LocalSet's driving thread. Multi-second work
                        // there (a full reconciliation pass is 10-20s) then
                        // holds this gate closed while the network is idle.
                        self.outbox_post_tasks.spawn(async move {
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
            }
            Some(Err(err)) => {
                tracing::warn!("Outbox collection task failed: {}", err);
            }
            None => {}
        }
    }
    async fn on_status_slot_done(
        &mut self,
        status_slot_result: Option<Result<StatusSlotResult, tokio::task::JoinError>>,
    ) {
        match status_slot_result {
            Some(Ok(result)) => {
                self.status_owner_refresh_cursor = result.owner_refresh_cursor;
                self.managed_owner_refresh_cursor = result.managed_owner_refresh_cursor;
                if result.elapsed_ms > 100 {
                    tracing::warn!(
                        elapsed_ms = result.elapsed_ms,
                        slots = result.slots.len(),
                        recorded = result.recorded.len(),
                        "Status slot pass was slow"
                    );
                }
                let live: HashSet<String> = result
                    .slots
                    .iter()
                    .map(|slot| slot.session_id.clone())
                    .collect();
                for (session_id, version) in result.recorded {
                    self.status_recorded.insert(session_id, version);
                }
                // A session with no slot has no current status, so
                // neither map needs to remember it.
                self.status_recorded
                    .retain(|session_id, _| live.contains(session_id));
                self.status_ledger.retain_live(&result.slots);
                let pending = self.status_ledger.pending(result.slots, Instant::now());
                if !pending.is_empty() && self.status_post_tasks.is_empty() {
                    let client = self.client.clone();
                    self.status_post_tasks
                        .spawn(async move { post_status_slots(&client, pending).await });
                }
            }
            Some(Err(err)) => {
                tracing::warn!("Status slot task failed: {}", err);
            }
            None => {}
        }
    }
    fn on_status_post_done(
        &mut self,
        status_post_result: Option<Result<StatusPostResult, tokio::task::JoinError>>,
    ) {
        match status_post_result {
            Some(Ok(result)) => self.status_ledger.settle(result, Instant::now()),
            Some(Err(err)) => {
                tracing::warn!("Status slot POST task failed: {}", err);
            }
            None => {}
        }
    }
    async fn on_runtime_collect_done(
        &mut self,
        runtime_collect_result: Option<Result<RuntimeCollectResult, tokio::task::JoinError>>,
    ) {
        match runtime_collect_result {
            Some(Ok(result)) => {
                self.latest_runtime_event_outbox = result.measurement;
                if let Some(projection) = self.last_status_projection.as_mut() {
                    projection.set_runtime_event_outbox(self.latest_runtime_event_outbox.clone());
                }
                // A saturated pass means the directory holds more than
                // one pass can inspect, so the newest observation is
                // not reliably in it. Reduce it to current status in a
                // task of its own: the live lane must keep collecting
                // and posting while that runs.
                if result.saturated
                    && self.runtime_sweep_tasks.is_empty()
                    && self
                        .runtime_sweep_quiet_until
                        .is_none_or(|until| Instant::now() >= until)
                {
                    let runtime_events_outbox_dir = self.runtime_events_outbox_dir.clone();
                    self.runtime_sweep_tasks.spawn_blocking(move || {
                        outbox::sweep_runtime_event_outbox(
                            &runtime_events_outbox_dir,
                            outbox::RUNTIME_EVENT_SWEEP_LIMIT,
                        )
                    });
                }
                if result.elapsed_ms > 100 {
                    tracing::warn!(
                        elapsed_ms = result.elapsed_ms,
                        runtime_posts = result.posts.len(),
                        "Runtime-event collection was slow"
                    );
                }
                if !result.posts.is_empty() {
                    let retry_due = self
                        .runtime_outbox_retry_after
                        .map(|retry_at| Instant::now() >= retry_at)
                        .unwrap_or(true);
                    if self.runtime_outbox_post_tasks.is_empty() && retry_due {
                        let client = self.client.clone();
                        let runtime_posts = result.posts;
                        let post_count = runtime_posts.len();
                        // spawn, not spawn_local: see the presence path
                        // above. This is the live-transcript lane, so a
                        // starved wrapper here shows up as minutes-stale
                        // events on every client.
                        self.runtime_outbox_post_tasks.spawn(async move {
                            let join_started = Instant::now();
                            let post_task = tokio::spawn(async move {
                                let task_started = Instant::now();
                                let (sent, kept) = outbox::post_pending_runtime_event_files(
                                    &client,
                                    runtime_posts,
                                )
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
                                    pending_posts = result.posts.len(),
                                    "Skipping outbox runtime-event POST while previous POST is still in flight"
                                );
                    }
                }
            }
            Some(Err(err)) => {
                tracing::warn!("Runtime-event collection task failed: {}", err);
            }
            None => {}
        }
    }
    fn on_runtime_sweep_done(
        &mut self,
        runtime_sweep_result: Option<Result<outbox::RuntimeOutboxSweep, tokio::task::JoinError>>,
    ) {
        match runtime_sweep_result {
            Some(Ok(sweep)) => {
                tracing::warn!(
                    inspected = sweep.inspected,
                    discarded = sweep.discarded,
                    more = sweep.more,
                    "Swept superseded runtime status out of a flooded outbox"
                );
                // More work does not mean another pass right now. The
                // next saturated collection arms the next sweep on the
                // ordinary tick; chaining blocking passes back to back
                // would starve every other lane on this loop.
                self.runtime_sweep_quiet_until = (sweep.discarded == 0)
                    .then(|| Instant::now() + RUNTIME_SWEEP_QUIET_AFTER_NOTHING);
            }
            Some(Err(err)) => {
                tracing::warn!("Runtime-event outbox sweep task failed: {}", err);
            }
            None => {}
        }
    }
    fn on_outbox_post_done(
        &mut self,
        outbox_post_result: Option<Result<(usize, usize, u64, u64), tokio::task::JoinError>>,
    ) {
        match outbox_post_result {
            Some(Ok((sent, kept, join_elapsed_ms, task_elapsed_ms))) => {
                let local_join_delay_ms = join_elapsed_ms.saturating_sub(task_elapsed_ms);
                if kept > 0 {
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST deferred during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST kept files for retry"
                        );
                    }
                } else if join_elapsed_ms > 1_000 {
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            sent,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST was delayed during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            sent,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST was slow"
                        );
                    }
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
                if self.host_link.explains_failure(&err.to_string()) {
                    tracing::debug!(
                        "Outbox presence POST task deferred during Runtime Host update: {}",
                        err
                    );
                } else {
                    tracing::warn!("Outbox presence POST task failed: {}", err);
                }
            }
            None => {}
        }
    }
    fn on_runtime_outbox_post_done(
        &mut self,
        runtime_outbox_post_result: Option<
            Result<(usize, usize, u64, u64), tokio::task::JoinError>,
        >,
    ) {
        match runtime_outbox_post_result {
            Some(Ok((sent, kept, join_elapsed_ms, task_elapsed_ms))) => {
                let local_join_delay_ms = join_elapsed_ms.saturating_sub(task_elapsed_ms);
                // A mixed result proves the Runtime Host is accepting
                // work. Do not let one session's retained event throttle
                // every other session; back off only when this pass made
                // no progress at all.
                if kept > 0 && sent == 0 {
                    self.runtime_outbox_consecutive_failures =
                        self.runtime_outbox_consecutive_failures.saturating_add(1);
                    let backoff_multiplier =
                        1u64 << self.runtime_outbox_consecutive_failures.min(4);
                    let delay = LIVE_LOCAL_RETRY_DELAY
                        .saturating_mul(backoff_multiplier as u32)
                        .min(Duration::from_secs(LOCAL_RETRY_DELAY_SECS));
                    self.runtime_outbox_retry_after = Some(Instant::now() + delay);
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            retry_delay_ms = delay.as_millis() as u64,
                            "Runtime-event outbox deferred during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            retry_delay_ms = delay.as_millis() as u64,
                            "Outbox runtime-event POST kept all files for retry"
                        );
                    }
                } else {
                    self.runtime_outbox_consecutive_failures = 0;
                    self.runtime_outbox_retry_after = None;
                    // Delivery made progress, so more may be waiting:
                    // collect again now rather than on the next tick.
                    // An empty outbox ends this, because a pass that
                    // finds nothing starts no POST.
                    if sent > 0 {
                        self.outbox_timer.reset_immediately();
                    }
                    if kept > 0 {
                        tracing::warn!(
                                    sent,
                                    kept,
                                    task_elapsed_ms,
                                    join_elapsed_ms,
                                    local_join_delay_ms,
                                    "Outbox runtime-event POST kept some files while other files progressed"
                                );
                    } else if join_elapsed_ms > 1_000 {
                        if self.host_link.is_updating() {
                            tracing::debug!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Runtime-event outbox POST was delayed during Runtime Host update"
                            );
                        } else {
                            tracing::warn!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Outbox runtime-event POST was slow"
                            );
                        }
                    } else if sent > 0 {
                        tracing::debug!(
                            sent,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            "Outbox runtime-event POST sent files"
                        );
                    }
                }
            }
            Some(Err(err)) => {
                self.runtime_outbox_consecutive_failures =
                    self.runtime_outbox_consecutive_failures.saturating_add(1);
                self.runtime_outbox_retry_after = Some(Instant::now() + LIVE_LOCAL_RETRY_DELAY);
                if self.host_link.explains_failure(&err.to_string()) {
                    tracing::debug!(
                        "Runtime-event outbox task deferred during Runtime Host update: {}",
                        err
                    );
                } else {
                    tracing::warn!("Outbox runtime-event POST task failed: {}", err);
                }
            }
            None => {}
        }
    }
    fn on_heartbeat_post_done(
        &mut self,
        heartbeat_post_result: Option<Result<HeartbeatPostResult, tokio::task::JoinError>>,
    ) {
        match heartbeat_post_result {
            Some(Ok(result)) => {
                self.heartbeat_transport.record_send_metrics(
                    result.reason,
                    result.metrics.raw_bytes,
                    result.metrics.wire_bytes,
                    result.metrics.latency_ms,
                );
                let local_join_delay_ms = result
                    .join_elapsed_ms
                    .saturating_sub(result.task_elapsed_ms);
                if result.task_elapsed_ms > 1_000 || local_join_delay_ms > 1_000 {
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            reason = result.reason,
                            task_elapsed_ms = result.task_elapsed_ms,
                            join_elapsed_ms = result.join_elapsed_ms,
                            local_join_delay_ms,
                            "Heartbeat POST was delayed during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            reason = result.reason,
                            task_elapsed_ms = result.task_elapsed_ms,
                            join_elapsed_ms = result.join_elapsed_ms,
                            local_join_delay_ms,
                            "Heartbeat POST was slow"
                        );
                    }
                }
                self.acknowledged_machine_evidence
                    .record_send_result(&result.result, &result.sent_evidence_identities);
                if let Some(evidence) = self
                    .last_status_projection
                    .as_ref()
                    .and_then(|projection| projection.payload.machine_evidence.as_ref())
                {
                    self.acknowledged_machine_evidence
                        .prune_to_current(&evidence.candidate_identities);
                } else {
                    self.acknowledged_machine_evidence.prune_to_current(&[]);
                }
                match result.result {
                    Ok(ack) => {
                        let evidence_was_refused = self.heartbeat_transport.evidence_refused();
                        let evidence_changed = self
                            .heartbeat_transport
                            .record_evidence_ack(ack.evidence_ack.as_deref());
                        let evidence_refused = self.heartbeat_transport.evidence_refused();
                        let recovered = self
                            .heartbeat_transport
                            .record_success(chrono::Utc::now().to_rfc3339());
                        tracing::debug!(
                            reason = result.reason,
                            task_elapsed_ms = result.task_elapsed_ms,
                            join_elapsed_ms = result.join_elapsed_ms,
                            evidence_state = %self.heartbeat_transport.evidence_state,
                            "Runtime truth snapshot sent after local process/control change"
                        );
                        if evidence_changed && evidence_refused {
                            tracing::warn!(
                                reason = result.reason,
                                evidence_state = %self.heartbeat_transport.evidence_state,
                                "Heartbeat machine evidence was refused"
                            );
                        } else if evidence_was_refused
                            && self.heartbeat_transport.evidence_state == "applied"
                        {
                            tracing::info!("Heartbeat machine evidence recovered");
                        }
                        if recovered {
                            tracing::info!("Heartbeat POST recovered");
                        }
                        self.last_runtime_truth_signature = Some(result.signature.clone());
                        if self
                            .pending_truth_heartbeat
                            .as_ref()
                            .is_some_and(|pending| pending.signature == result.signature)
                        {
                            self.pending_truth_heartbeat = None;
                        }
                    }
                    Err(err) => {
                        let error = heartbeat::bounded_heartbeat_error(&err);
                        let transitioned = self
                            .heartbeat_transport
                            .record_failure(chrono::Utc::now().to_rfc3339(), &error);
                        if self
                            .pending_truth_heartbeat
                            .as_ref()
                            .is_some_and(|pending| pending.signature == result.signature)
                        {
                            self.pending_truth_heartbeat = None;
                        }
                        // The failed send did not deliver this truth: forget it, so the
                        // next projection retries it (after the 1 s window) instead of
                        // waiting for the 60 s periodic heartbeat.
                        if self.last_runtime_truth_signature.as_deref()
                            == Some(result.signature.as_str())
                        {
                            self.last_runtime_truth_signature = None;
                        }
                        if self.host_link.explains_failure(&error) {
                            tracing::debug!(
                                reason = result.reason,
                                error = %error,
                                retry_after_secs = result.metrics.retry_after
                                    .map(|delay| delay.as_secs_f64()),
                                "Heartbeat POST deferred during Runtime Host update"
                            );
                        } else if transitioned {
                            tracing::warn!(
                                reason = result.reason,
                                error = %error,
                                "Heartbeat POST failed"
                            );
                        } else {
                            tracing::debug!(
                                reason = result.reason,
                                error = %error,
                                "Heartbeat POST remains degraded"
                            );
                        }
                    }
                }
                publish_heartbeat_transport_status(
                    &self.heartbeat_transport,
                    &mut self.last_status_projection,
                    serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.host_link,
                    &self.status_path,
                );
            }
            Some(Err(err)) => {
                let error = heartbeat::bounded_heartbeat_error(&err.to_string());
                let transitioned = self
                    .heartbeat_transport
                    .record_failure(chrono::Utc::now().to_rfc3339(), &error);
                if self.host_link.explains_failure(&error) {
                    tracing::debug!(error = %error, "Heartbeat POST task deferred during Runtime Host update");
                } else if transitioned {
                    tracing::warn!(error = %error, "Heartbeat POST task failed");
                } else {
                    tracing::debug!(error = %error, "Heartbeat POST task remains degraded");
                }
                publish_heartbeat_transport_status(
                    &self.heartbeat_transport,
                    &mut self.last_status_projection,
                    serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.host_link,
                    &self.status_path,
                );
            }
            None => {}
        }
    }
    fn on_machine_presence_post_done(
        &mut self,
        machine_presence_post_result: Option<
            Result<MachinePresencePostResult, tokio::task::JoinError>,
        >,
    ) {
        match machine_presence_post_result {
            Some(Ok(result)) => match result.result {
                Ok(true) => {
                    tracing::debug!(
                        task_elapsed_ms = result.task_elapsed_ms,
                        "Machine presence POST sent"
                    );
                }
                Ok(false) => {
                    tracing::debug!(
                        task_elapsed_ms = result.task_elapsed_ms,
                        "Machine presence collection disabled"
                    );
                }
                Err(err) => {
                    tracing::debug!("Machine presence POST failed: {}", err);
                }
            },
            Some(Err(err)) => {
                tracing::warn!("Machine presence POST task failed: {}", err);
            }
            None => {}
        }
    }
    fn on_host_link_poll_done(
        &mut self,
        host_link_poll_result: Option<Result<Result<()>, tokio::task::JoinError>>,
    ) {
        match host_link_poll_result {
            Some(Ok(Ok(()))) | None => {}
            Some(Ok(Err(error))) => {
                tracing::debug!(%error, "Runtime Host admission poll failed");
            }
            Some(Err(error)) => {
                tracing::debug!(%error, "Runtime Host admission poll task failed");
            }
        }
    }
    fn on_host_link_changed(&mut self, host_link_event: Result<(), watch::error::RecvError>) {
        if host_link_event.is_ok() {
            let serving_generation = self.host_link.serving_generation();
            if serving_generation > self.last_serving_generation {
                self.last_serving_generation = serving_generation;
                let now = Instant::now();
                for retry in self.deferred_retries.values_mut() {
                    retry.due_at = now;
                }
                self.runtime_outbox_consecutive_failures = 0;
                self.runtime_outbox_retry_after = None;
                self.adaptive_limiter.reset_backpressure_cooldown();
                self.outbox_timer.reset_immediately();
                self.failed_ship_retry_timer.reset_immediately();
                self.heartbeat_timer.reset_immediately();
                self.pending_truth_heartbeat = None;
            }
        }
    }
    fn on_unmanaged_binding_refresh_done(
        &mut self,
        config: &ConnectConfig,
        unmanaged_binding_refresh_result: Option<
            Result<UnmanagedBindingRefreshResult, tokio::task::JoinError>,
        >,
    ) {
        let refresh_generation = self.unmanaged_binding_refresh_generation.take();
        match unmanaged_binding_refresh_result {
            Some(Ok(result)) => {
                let stale = result.generation != self.projection_generation;
                let managed_observation_current =
                    result.managed_observation_generation == self.managed_observation_generation;
                if stale {
                    tracing::debug!(
                        generation = result.generation,
                        latest_generation = self.projection_generation,
                        "Discarded stale unmanaged reconciliation result"
                    );
                } else if !managed_observation_current {
                    tracing::debug!(
                        result_managed_observation_generation =
                            result.managed_observation_generation,
                        latest_managed_observation_generation = self.managed_observation_generation,
                        "Applying unmanaged result without replacing newer managed observations"
                    );
                }
                if !stale && self.managed_observation_valid {
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
                            if managed_observation_current {
                                self.last_projected_managed_observations = result.managed;
                                self.last_projected_managed_scan_partial =
                                    result.managed_scan_partial;
                                self.last_projected_managed_snapshot_complete =
                                    result.full_reconciliation_candidate;
                                self.last_projected_unmanaged_snapshot_complete =
                                    result.full_reconciliation_candidate;
                                self.unmanaged_binding_refresh_failed = false;
                                if result.full_reconciliation_candidate {
                                    self.last_full_reconciled_at =
                                        Some(chrono::Utc::now().to_rfc3339());
                                }
                            } else {
                                self.last_projected_unmanaged_snapshot_complete = false;
                            }
                            self.last_unmanaged_session_bindings = Some(bindings);
                        }
                        Err(err) if !managed_observation_current => {
                            tracing::debug!(
                                reason = result.reason,
                                "Discarded stale unmanaged binding refresh failure: {}",
                                err
                            );
                        }
                        Err(err) => {
                            // Managed state files are authoritative for Helm ownership.
                            // Optional Shadow process discovery must not suppress a newly
                            // observed managed run. Publish that managed truth with the
                            // last-known unmanaged bindings, but mark only the unmanaged
                            // scope incomplete so the Runtime Host cannot close missing
                            // Shadow sessions from this partial observation.
                            if managed_observation_current {
                                self.last_projected_managed_observations = result.managed;
                                self.last_projected_managed_scan_partial =
                                    result.managed_scan_partial;
                                self.last_projected_managed_snapshot_complete =
                                    result.full_reconciliation_candidate;
                            }
                            self.last_projected_unmanaged_snapshot_complete = false;
                            // Shadow discovery is optional. Do not turn a
                            // per-pid lsof failure into an immediate full
                            // managed scan, which would repeatedly advance
                            // projection generations and starve managed
                            // truth. The scheduled full observation is the
                            // retry path; keep this projection incomplete
                            // so missing Shadow sessions remain unknown.
                            self.pending_full_reconciliation = false;
                            self.unmanaged_binding_refresh_failed = true;
                            // Shadow discovery is optional evidence. Keep the
                            // retained managed projection usable and mark the
                            // retry as in progress; a single lsof failure
                            // must not turn local health into a failed
                            // reconciliation or erase current sessions.
                            self.managed_reconciliation
                                .start("unmanaged_binding", chrono::Utc::now().to_rfc3339());
                            tracing::warn!(
                                reason = result.reason,
                                elapsed_ms = result.elapsed_ms,
                                "Unmanaged binding refresh failed: {}",
                                err
                            );
                        }
                    }
                    let input = ProjectionBuildInput {
                        generation: self.projection_generation,
                        managed_observation_generation: self.managed_observation_generation,
                        managed_scan_partial: self.last_projected_managed_scan_partial,
                        managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                        managed_captured_at: self.last_managed_captured_at.clone(),
                        unmanaged_snapshot_complete: self
                            .last_projected_unmanaged_snapshot_complete,
                        db_path: self.projection_db_path.clone(),
                        parse_tracker: self.parse_tracker.clone(),
                        ship_stats: self.ship_stats.clone(),
                        is_offline: self.offline.is_offline,
                        last_ship_at: self.last_ship_at.clone(),
                        machine_id: config.shipper_config.machine_name.clone(),
                        managed: self.last_projected_managed_observations.clone(),
                        unmanaged: self
                            .last_unmanaged_session_bindings
                            .clone()
                            .unwrap_or_default(),
                        limiter: self.adaptive_limiter.snapshot(),
                        scheduler: self.scheduler.snapshot(),
                        archive_repair_mode: config.archive_repair_mode,
                        last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                        continuation: self.last_resume_contracts.clone(),
                        session_snapshot_state: self.session_snapshot_state.clone(),
                    };
                    if !maybe_start_projection_build(&mut self.projection_build_tasks, input) {
                        self.projection_build_pending = true;
                    }
                }
            }
            Some(Err(err)) => {
                let refresh_is_current = refresh_generation
                    == Some((
                        self.projection_generation,
                        self.managed_observation_generation,
                    ));
                if refresh_is_current {
                    self.unmanaged_binding_refresh_failed = true;
                    // This task only refreshes optional Shadow bindings.
                    // Preserve the last coherent managed projection and
                    // expose the retry as reconciling rather than making
                    // the whole local session inventory failed.
                    self.managed_reconciliation
                        .start("unmanaged_binding", chrono::Utc::now().to_rfc3339());
                    heartbeat::refresh_existing_status_pulse(
                        &self.managed_reconciliation,
                        &mut self.shipping_progress,
                        self.offline.is_offline,
                        &self.status_path,
                        &self.heartbeat_transport,
                        Some(&self.host_link.snapshot()),
                    );
                    tracing::warn!("Unmanaged binding refresh task failed: {}", err);
                } else {
                    tracing::debug!(
                        refresh_generation = ?refresh_generation,
                        latest_generation = self.projection_generation,
                        latest_managed_observation_generation =
                            self.managed_observation_generation,
                        "Discarded stale unmanaged binding task failure: {}",
                        err
                    );
                }
            }
            None => {}
        }
        if self.unmanaged_binding_refresh_tasks.is_empty()
            && self.managed_observation_scan_tasks.is_empty()
        {
            if self.pending_wake_reconciliation
                && maybe_start_managed_observation_scan(
                    self.projection_db_path.clone(),
                    &mut self.managed_observation_scan_tasks,
                    "wake",
                    true,
                    &self.last_managed_observations,
                )
            {
                self.pending_wake_reconciliation = false;
                self.managed_full_reconciliation_not_before =
                    Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                self.managed_reconciliation
                    .start("wake", chrono::Utc::now().to_rfc3339());
            } else if managed_full_reconciliation_ready(
                self.pending_full_reconciliation
                    || certificate_needs_refresh(self.last_certified_at, Instant::now()),
                self.managed_observation_scan_tasks.is_empty(),
                Instant::now(),
                self.managed_full_reconciliation_not_before,
            ) && maybe_start_managed_observation_scan(
                self.projection_db_path.clone(),
                &mut self.managed_observation_scan_tasks,
                "full_reconciliation",
                true,
                &self.last_managed_observations,
            ) {
                self.pending_full_reconciliation = false;
                self.managed_full_reconciliation_not_before =
                    Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                self.managed_reconciliation
                    .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
            }
        }
    }
    fn on_managed_observation_scan_done(
        &mut self,
        config: &ConnectConfig,
        managed_observation_scan_result: Option<
            Result<ManagedObservationScanResult, tokio::task::JoinError>,
        >,
    ) {
        match managed_observation_scan_result {
            Some(Ok(mut result)) => {
                self.last_status_owners_at = result.status_owner_snapshot_at;
                self.last_status_owners = Arc::new(std::mem::take(&mut result.status_owners));
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
                        pi_count = result.pi_observations.len(),
                        omp_count = result.omp_observations.len(),
                        omp_live_count = result
                            .omp_observations
                            .iter()
                            .filter(|observation| observation.live)
                            .count(),
                        process_inventory_ms = result.process_inventory_ms,
                        codex_elapsed_ms = result.codex_elapsed_ms,
                        antigravity_elapsed_ms = result.antigravity_elapsed_ms,
                        claude_elapsed_ms = result.claude_elapsed_ms,
                        opencode_elapsed_ms = result.opencode_elapsed_ms,
                        cursor_elapsed_ms = result.cursor_elapsed_ms,
                        pi_elapsed_ms = result.pi_elapsed_ms,
                        omp_elapsed_ms = result.omp_elapsed_ms,
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
                        pi_count = result.pi_observations.len(),
                        omp_count = result.omp_observations.len(),
                        omp_live_count = result
                            .omp_observations
                            .iter()
                            .filter(|observation| observation.live)
                            .count(),
                        process_inventory_ms = result.process_inventory_ms,
                        codex_elapsed_ms = result.codex_elapsed_ms,
                        antigravity_elapsed_ms = result.antigravity_elapsed_ms,
                        claude_elapsed_ms = result.claude_elapsed_ms,
                        opencode_elapsed_ms = result.opencode_elapsed_ms,
                        cursor_elapsed_ms = result.cursor_elapsed_ms,
                        pi_elapsed_ms = result.pi_elapsed_ms,
                        omp_elapsed_ms = result.omp_elapsed_ms,
                        retained_stale_rows = result.retained_stale_rows,
                        elapsed_ms = result.elapsed_ms,
                        "Managed observation scan completed"
                    );
                }
                // Report only. `process_inventory_valid` proves `ps`
                // ran; it does not prove the five provider scanners
                // did, and a scanner that fails is indistinguishable
                // from a provider with no sessions. Killing on that
                // basis would destroy live work.
                if !result.orphan_processes.is_empty() {
                    crate::managed_process_janitor::report_orphan_processes(
                        &result.orphan_processes,
                    );
                }
                if !result.process_inventory_valid {
                    self.managed_observation_valid = false;
                    self.projection_generation = self.projection_generation.saturating_add(1);
                    tracing::warn!(
                                reason = result.reason,
                                "Managed observation scan retained prior truth because process inventory failed"
                            );
                    self.managed_reconciliation =
                        heartbeat::ProjectionReconciliation::failed("process_inventory");
                    self.managed_reconciliation
                        .start("process_inventory", chrono::Utc::now().to_rfc3339());
                    heartbeat::refresh_existing_status_pulse(
                        &self.managed_reconciliation,
                        &mut self.shipping_progress,
                        self.offline.is_offline,
                        &self.status_path,
                        &self.heartbeat_transport,
                        Some(&self.host_link.snapshot()),
                    );
                    if self.pending_wake_reconciliation {
                        if maybe_start_managed_observation_scan(
                            self.projection_db_path.clone(),
                            &mut self.managed_observation_scan_tasks,
                            "wake",
                            true,
                            &self.last_managed_observations,
                        ) {
                            self.pending_wake_reconciliation = false;
                        }
                    } else if managed_full_reconciliation_ready(
                        self.pending_full_reconciliation
                            || certificate_needs_refresh(self.last_certified_at, Instant::now()),
                        self.managed_observation_scan_tasks.is_empty(),
                        Instant::now(),
                        self.managed_full_reconciliation_not_before,
                    ) && maybe_start_managed_observation_scan(
                        self.projection_db_path.clone(),
                        &mut self.managed_observation_scan_tasks,
                        "full_reconciliation",
                        true,
                        &self.last_managed_observations,
                    ) {
                        self.pending_full_reconciliation = false;
                        self.managed_full_reconciliation_not_before =
                            Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                    }
                    return;
                }
                self.managed_observation_valid = true;
                if result.full_reconciliation {
                    // Start-time gating does not prevent a long scan
                    // from immediately retriggering on events observed
                    // during its own walk. Hold the next full pass for
                    // one observation interval after completion.
                    self.managed_full_reconciliation_not_before =
                        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                }
                if let Some(continuation) = &result.continuation {
                    self.last_resume_contracts = Some(continuation.clone());
                }
                let next_managed_observations =
                    ManagedObservationSnapshot::from_result(&result).current_only();
                let managed_observations_changed = !next_managed_observations
                    .projection_equivalent(&self.last_managed_observations);
                let (managed_scan_partial, managed_snapshot_complete) =
                    managed_scan_certificate(&result);
                if managed_snapshot_complete {
                    self.last_certified_at = Some(Instant::now());
                    self.certificate_keepalive_warned = false;
                } else if let Some(refreshed_at) = self.last_certified_at {
                    // Keep the last claim only while it is still inside
                    // the keepalive; past that the beat ships without
                    // absence authority, which fails closed.
                    if refreshed_at.elapsed()
                        > Duration::from_secs(MANAGED_CERTIFICATE_KEEPALIVE_SECS)
                        && !self.certificate_keepalive_warned
                    {
                        self.certificate_keepalive_warned = true;
                        tracing::warn!(
                                    reason = result.reason,
                                    retained_stale_rows = result.retained_stale_rows,
                                    "Managed enumeration has not certified within the keepalive window; beats ship without absence authority"
                                );
                    }
                }
                let managed_evidence_changed = managed_observations_changed
                    || result.full_reconciliation
                    || managed_scan_partial != self.last_projected_managed_scan_partial;
                if managed_evidence_changed {
                    self.managed_observation_generation =
                        self.managed_observation_generation.saturating_add(1);
                }
                self.last_managed_observations = next_managed_observations;
                project_binding_liveness(&self.conn, &self.last_managed_observations);
                pump_ready_local_work(
                    &mut self.scheduler,
                    &mut self.in_flight,
                    &self.task_context,
                    &mut self.deferred_retries,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    archive_repair_is_paused(config.archive_repair_mode),
                );
                let managed_process_pids = managed_process_pids_from_observations(
                    &result.codex_observations,
                    &result.claude_observations,
                    &result.opencode_observations,
                    &result.cursor_observations,
                    &result.pi_observations,
                    &result.omp_observations,
                );
                let should_refresh_unmanaged =
                    result.full_reconciliation || managed_observations_changed;
                let paired_generation = self.projection_generation.saturating_add(1);
                let paired_refresh_started = should_refresh_unmanaged
                    && maybe_start_unmanaged_binding_refresh(
                        &mut self.unmanaged_binding_refresh_tasks,
                        config.shipper_config.db_path.clone(),
                        config.shipper_config.machine_name.clone(),
                        managed_process_pids.clone(),
                        result.process_inventory.clone(),
                        result.reason,
                        paired_generation,
                        self.managed_observation_generation,
                        self.last_managed_observations.clone(),
                        managed_scan_partial,
                        managed_scan_certificate(&result).1,
                    );
                if paired_refresh_started {
                    self.projection_generation = paired_generation;
                    self.unmanaged_binding_refresh_generation = Some((
                        self.projection_generation,
                        self.managed_observation_generation,
                    ));
                } else if result.full_reconciliation {
                    if result.reason == "wake" {
                        self.pending_wake_reconciliation = true;
                    } else {
                        self.pending_full_reconciliation = true;
                    }
                } else if managed_observations_changed {
                    defer_managed_pair_retry(
                        &mut self.projection_generation,
                        &mut self.pending_full_reconciliation,
                    );
                }

                // A valid managed scan is fresh evidence even when the
                // optional unmanaged/Shadow refresh is still running.
                // Publish it with the cached Shadow rows only as
                // incomplete evidence; the refresh result can replace
                // this projection later without blocking managed truth.
                self.last_projected_managed_observations = self.last_managed_observations.clone();
                self.last_managed_captured_at = result.captured_at.clone();
                self.last_projected_managed_scan_partial = managed_scan_partial;
                self.last_projected_managed_snapshot_complete = managed_snapshot_complete;
                self.last_projected_unmanaged_snapshot_complete = false;
                let input = ProjectionBuildInput {
                    generation: self.projection_generation,
                    managed_observation_generation: self.managed_observation_generation,
                    managed_scan_partial: self.last_projected_managed_scan_partial,
                    managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                    managed_captured_at: self.last_managed_captured_at.clone(),
                    unmanaged_snapshot_complete: self.last_projected_unmanaged_snapshot_complete,
                    db_path: self.projection_db_path.clone(),
                    parse_tracker: self.parse_tracker.clone(),
                    ship_stats: self.ship_stats.clone(),
                    is_offline: self.offline.is_offline,
                    last_ship_at: self.last_ship_at.clone(),
                    machine_id: config.shipper_config.machine_name.clone(),
                    managed: self.last_projected_managed_observations.clone(),
                    unmanaged: self
                        .last_unmanaged_session_bindings
                        .clone()
                        .unwrap_or_default(),
                    limiter: self.adaptive_limiter.snapshot(),
                    scheduler: self.scheduler.snapshot(),
                    archive_repair_mode: config.archive_repair_mode,
                    last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                    continuation: self.last_resume_contracts.clone(),
                    session_snapshot_state: self.session_snapshot_state.clone(),
                };
                if !maybe_start_projection_build(&mut self.projection_build_tasks, input) {
                    self.projection_build_pending = true;
                }
                maybe_start_opencode_title_refresh(
                    &mut self.opencode_title_refresh_tasks,
                    config.shipper_config.db_path.clone(),
                    result.opencode_observations.clone(),
                );
                if self.pending_wake_reconciliation
                    && self.unmanaged_binding_refresh_tasks.is_empty()
                    && maybe_start_managed_observation_scan(
                        self.projection_db_path.clone(),
                        &mut self.managed_observation_scan_tasks,
                        "wake",
                        true,
                        &self.last_managed_observations,
                    )
                {
                    self.pending_wake_reconciliation = false;
                    self.managed_full_reconciliation_not_before =
                        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                    self.managed_reconciliation
                        .start("wake", chrono::Utc::now().to_rfc3339());
                } else if managed_full_reconciliation_ready(
                    self.pending_full_reconciliation,
                    self.unmanaged_binding_refresh_tasks.is_empty()
                        && self.managed_observation_scan_tasks.is_empty(),
                    Instant::now(),
                    self.managed_full_reconciliation_not_before,
                ) && maybe_start_managed_observation_scan(
                    self.projection_db_path.clone(),
                    &mut self.managed_observation_scan_tasks,
                    "full_reconciliation",
                    true,
                    &self.last_managed_observations,
                ) {
                    self.pending_full_reconciliation = false;
                    self.managed_full_reconciliation_not_before =
                        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                    self.managed_reconciliation
                        .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
                }
            }
            Some(Err(err)) => {
                self.managed_observation_valid = false;
                self.projection_generation = self.projection_generation.saturating_add(1);
                tracing::warn!("Managed observation scan task failed: {}", err);
                self.managed_reconciliation = heartbeat::ProjectionReconciliation::failed(
                    self.managed_reconciliation
                        .reason
                        .clone()
                        .unwrap_or_else(|| "managed_observation".to_string()),
                );
                heartbeat::refresh_existing_status_pulse(
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.status_path,
                    &self.heartbeat_transport,
                    Some(&self.host_link.snapshot()),
                );
            }
            None => {}
        }
    }
    fn on_opencode_title_refresh_done(
        &mut self,
        opencode_title_refresh_result: Option<Result<Result<()>, tokio::task::JoinError>>,
    ) {
        match opencode_title_refresh_result {
            Some(Ok(Ok(()))) | None => {}
            Some(Ok(Err(err))) => tracing::warn!(error = %err, "OpenCode title refresh failed"),
            Some(Err(err)) => tracing::warn!(error = %err, "OpenCode title refresh task failed"),
        }
    }
    fn on_projection_build_done(
        &mut self,
        config: &ConnectConfig,
        projection_build_result: Option<Result<ProjectionBuildResult, tokio::task::JoinError>>,
    ) {
        match projection_build_result {
            Some(Ok(result)) => {
                let is_current = self.managed_observation_valid
                    && result.generation == self.projection_generation
                    && result.managed_observation_generation == self.managed_observation_generation
                    && result.managed_scan_partial == self.last_projected_managed_scan_partial
                    && result.managed_snapshot_complete
                        == self.last_projected_managed_snapshot_complete
                    && result.unmanaged_snapshot_complete
                        == self.last_projected_unmanaged_snapshot_complete;
                match result.result {
                    Ok((mut projection, next_snapshot_state)) => {
                        if result.elapsed_ms > LOCAL_STATUS_BUDGET_MS {
                            self.projection_over_budget_ticks =
                                self.projection_over_budget_ticks.saturating_add(1);
                            self.projection_worst_elapsed_ms =
                                self.projection_worst_elapsed_ms.max(result.elapsed_ms);
                            let due = self.projection_budget_reported_at.is_none_or(|at| {
                                at.elapsed() >= LOCAL_STATUS_BUDGET_REPORT_INTERVAL
                            });
                            if due {
                                tracing::warn!(
                                    over_budget_ticks = self.projection_over_budget_ticks,
                                    worst_elapsed_ms = self.projection_worst_elapsed_ms,
                                    budget_ms = LOCAL_STATUS_BUDGET_MS,
                                    "Local status projection exceeded background budget"
                                );
                                self.projection_over_budget_ticks = 0;
                                self.projection_worst_elapsed_ms = 0;
                                self.projection_budget_reported_at = Some(Instant::now());
                            }
                        }
                        if !is_current {
                            tracing::debug!(
                                generation = result.generation,
                                latest_generation = self.projection_generation,
                                managed_scan_partial = result.managed_scan_partial,
                                latest_managed_scan_partial =
                                    self.last_projected_managed_scan_partial,
                                unmanaged_snapshot_complete = result.unmanaged_snapshot_complete,
                                latest_unmanaged_snapshot_complete =
                                    self.last_projected_unmanaged_snapshot_complete,
                                "Discarded stale local status projection"
                            );
                        } else {
                            self.session_snapshot_state = next_snapshot_state;
                            if result.managed_scan_partial {
                                self.managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::failed(
                                        "provider_state_partial",
                                    );
                            } else if result.managed_snapshot_complete
                                && result.unmanaged_snapshot_complete
                                && self.managed_observation_scan_tasks.is_empty()
                                && self.unmanaged_binding_refresh_tasks.is_empty()
                                && !self.unmanaged_binding_refresh_failed
                            {
                                // Only a complete paired observation clears a failure.
                                // A cached/managed-only projection is not recovery.
                                self.managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::idle();
                            }
                            self.shipping_progress.observe_pending_work(
                                heartbeat::payload_has_pending_work(&projection.payload)
                                    || known_pending_local_work(
                                        &self.scheduler,
                                        &self.deferred_retries,
                                        archive_repair_is_paused(config.archive_repair_mode),
                                    ),
                                Instant::now(),
                            );
                            projection.set_heartbeat_transport(self.heartbeat_transport.clone());
                            projection.set_host_link(self.host_link.snapshot());
                            projection
                                .set_runtime_event_outbox(self.latest_runtime_event_outbox.clone());
                            if let Some(evidence) = projection.payload.machine_evidence.as_ref() {
                                self.acknowledged_machine_evidence
                                    .prune_to_current(&evidence.candidate_identities);
                            } else {
                                self.acknowledged_machine_evidence.prune_to_current(&[]);
                            }
                            heartbeat::write_status_file(
                                &mut projection,
                                serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                                &self.managed_reconciliation,
                                &mut self.shipping_progress,
                                self.offline.is_offline,
                                &self.status_path,
                            );
                            let payload = projection.payload.clone();
                            self.last_status_projection = Some(projection);
                            let signature = runtime_truth_signature(&payload);
                            if !self.offline.is_offline {
                                if runtime_truth_changed(
                                    self.last_runtime_truth_signature.as_deref(),
                                    &signature,
                                ) {
                                    let now = Instant::now();
                                    let due =
                                        truth_heartbeat_due_at(self.last_truth_heartbeat_at, now);
                                    if self.heartbeat_post_tasks.is_empty() && due <= now {
                                        self.heartbeat_transport
                                            .record_attempt(chrono::Utc::now().to_rfc3339());
                                        publish_heartbeat_transport_status(
                                            &self.heartbeat_transport,
                                            &mut self.last_status_projection,
                                            serde_json::to_value(
                                                self.control_channel_status.snapshot(),
                                            )
                                            .ok(),
                                            &self.managed_reconciliation,
                                            &mut self.shipping_progress,
                                            self.offline.is_offline,
                                            &self.host_link,
                                            &self.status_path,
                                        );
                                        spawn_heartbeat_post(
                                            &mut self.heartbeat_post_tasks,
                                            self.client.clone(),
                                            payload,
                                            signature.clone(),
                                            "runtime_truth_change",
                                            &mut self.acknowledged_machine_evidence,
                                        );
                                        self.last_truth_heartbeat_at = Some(now);
                                        self.last_runtime_truth_signature = Some(signature);
                                        self.pending_truth_heartbeat = None;
                                    } else {
                                        self.pending_truth_heartbeat =
                                            Some(PendingTruthHeartbeat { payload, signature });
                                    }
                                } else {
                                    self.pending_truth_heartbeat = None;
                                }
                            }
                        }
                    }
                    Err(error) => {
                        tracing::warn!(error = %error, "Local status projection build failed");
                        if is_current {
                            self.managed_reconciliation =
                                heartbeat::ProjectionReconciliation::failed("projection_build");
                        }
                    }
                }
            }
            Some(Err(error)) => {
                tracing::warn!(error = %error, "Local status projection task failed");
                if !self.projection_build_pending {
                    self.managed_reconciliation =
                        heartbeat::ProjectionReconciliation::failed("projection_build");
                }
            }
            None => {}
        }

        if self.projection_build_pending && self.managed_observation_valid {
            self.projection_build_pending = false;
            let input = ProjectionBuildInput {
                generation: self.projection_generation,
                managed_observation_generation: self.managed_observation_generation,
                managed_scan_partial: self.last_projected_managed_scan_partial,
                managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                managed_captured_at: self.last_managed_captured_at.clone(),
                unmanaged_snapshot_complete: self.last_projected_unmanaged_snapshot_complete,
                db_path: self.projection_db_path.clone(),
                parse_tracker: self.parse_tracker.clone(),
                ship_stats: self.ship_stats.clone(),
                is_offline: self.offline.is_offline,
                last_ship_at: self.last_ship_at.clone(),
                machine_id: config.shipper_config.machine_name.clone(),
                managed: self.last_projected_managed_observations.clone(),
                unmanaged: self
                    .last_unmanaged_session_bindings
                    .clone()
                    .unwrap_or_default(),
                limiter: self.adaptive_limiter.snapshot(),
                scheduler: self.scheduler.snapshot(),
                archive_repair_mode: config.archive_repair_mode,
                last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                continuation: self.last_resume_contracts.clone(),
                session_snapshot_state: self.session_snapshot_state.clone(),
            };
            let _ = maybe_start_projection_build(&mut self.projection_build_tasks, input);
        }
    }
    fn on_phase_projection_due(&mut self, config: &ConnectConfig) {
        self.phase_projection_pending = false;
        // Only after the first full projection: before that the
        // managed snapshot is empty, and projecting would ship a
        // sessionless digest that the first real scan immediately
        // replaces.
        if self.last_status_projection.is_some() && self.managed_observation_valid {
            let input = ProjectionBuildInput {
                generation: self.projection_generation,
                managed_observation_generation: self.managed_observation_generation,
                managed_scan_partial: self.last_projected_managed_scan_partial,
                managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                managed_captured_at: self.last_managed_captured_at.clone(),
                unmanaged_snapshot_complete: self.last_projected_unmanaged_snapshot_complete,
                db_path: self.projection_db_path.clone(),
                parse_tracker: self.parse_tracker.clone(),
                ship_stats: self.ship_stats.clone(),
                is_offline: self.offline.is_offline,
                last_ship_at: self.last_ship_at.clone(),
                machine_id: config.shipper_config.machine_name.clone(),
                managed: self.last_projected_managed_observations.clone(),
                unmanaged: self
                    .last_unmanaged_session_bindings
                    .clone()
                    .unwrap_or_default(),
                limiter: self.adaptive_limiter.snapshot(),
                scheduler: self.scheduler.snapshot(),
                archive_repair_mode: config.archive_repair_mode,
                last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                continuation: self.last_resume_contracts.clone(),
                session_snapshot_state: self.session_snapshot_state.clone(),
            };
            if !maybe_start_projection_build(&mut self.projection_build_tasks, input) {
                self.projection_build_pending = true;
            }
        }
    }
    fn on_startup_reconciliation_due(&mut self, config: &ConnectConfig) {
        self.startup_reconciliation_pending = false;
        maybe_start_reconciliation_scan(
            &mut self.discovery_tasks,
            &self.providers,
            &self.scheduler,
            &self.deferred_retries,
            config.archive_repair_mode,
            "startup reconciliation",
        );
    }
    async fn on_health_tick(&mut self) {
        match self.client.health_check().await {
            Ok(true) => {
                if let Some(duration) = self.offline.mark_online() {
                    self.shipping_progress.reset_after_sleep(Instant::now());
                    self.last_runtime_truth_signature = None;
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
    async fn on_watcher_event(&mut self, config: &ConnectConfig, first_event: WatcherEvent) {
        let managed_state_changes = handle_live_transcript_file_events(
            &mut self.watcher,
            first_event,
            &self.providers,
            &self.managed_state_dirs,
            &self.conn,
            &mut self.transcript_wake_rx,
            &mut self.scheduler,
            &mut self.latest_transcript_wake_observed,
            &mut self.deferred_retries,
            &mut self.in_flight,
            &self.task_context,
            &mut self.shipping_progress,
            self.offline.is_offline,
            archive_repair_is_paused(config.archive_repair_mode),
        )
        .await;
        if !managed_state_changes.is_empty() {
            let requires_discovery = managed_state_changes_require_full_reconciliation(
                &self.last_managed_observations,
                &managed_state_changes,
            );
            if requires_discovery {
                // A watcher burst can contain many transient paths while
                // a provider is writing one session. Queue one bounded
                // full walk; the 5s observation tick starts it after the
                // burst instead of starting one walk per event batch.
                self.projection_generation = self.projection_generation.saturating_add(1);
                self.pending_full_reconciliation = true;
                tracing::debug!(
                    event_count = managed_state_changes.len(),
                    "Queued managed state discovery for coalesced observation"
                );
            } else {
                tracing::debug!(
                    event_count = managed_state_changes.len(),
                    "Known managed state changed; bounded periodic observation owns refresh"
                );
            }
        }
    }
    fn on_scope_tick(&mut self) {
        let current = crate::import_scope::fingerprint(&self.scope_dir);
        if current != self.last_scope_fingerprint {
            self.last_scope_fingerprint = current;
            if current.is_none() {
                // The file was deleted under a running daemon. What it
                // enforces did not change (it keeps the scope it last
                // knew), so put the file back rather than rescan.
                if crate::config::restore_lost_scope_file(&self.scope_dir) {
                    tracing::warn!(
                        "The import scope file was deleted; restored it from the running scope"
                    );
                }
            } else {
                let scope = crate::config::import_scope();
                crate::config::record_import_scope(&self.conn, &scope);
                tracing::info!("Import scope: {}", scope.describe());
                start_discovery_task(
                    &mut self.discovery_tasks,
                    &self.providers,
                    WorkPriority::Scan,
                    "import scope changed",
                );
            }
        }
    }
    fn on_provider_roots_tick(&mut self) {
        if self
            .watcher
            .refresh_provider_roots(&mut self.providers, &mut self.pending_provider_roots)
        {
            start_discovery_task(
                &mut self.discovery_tasks,
                &self.providers,
                WorkPriority::Scan,
                "new provider transcript root",
            );
        }
    }
    fn on_fallback_tick(&mut self, config: &ConnectConfig) {
        maybe_start_reconciliation_scan(
            &mut self.discovery_tasks,
            &self.providers,
            &self.scheduler,
            &self.deferred_retries,
            config.archive_repair_mode,
            "reconciliation scan",
        );
    }
    fn on_failed_ship_retry_tick(&mut self, config: &ConnectConfig) {
        let mut queued_retries = 0usize;
        match queue_storage_v2_pending_retry_paths(
            &mut self.scheduler,
            &self.conn,
            config.archive_repair_mode,
            &mut self.deferred_retries,
        ) {
            Ok(queued) => queued_retries = queued_retries.saturating_add(queued),
            Err(error) => tracing::warn!(
                error = %error,
                "Immutable storage-v2 retry error"
            ),
        }
        // Copied Cursor bytes are only needed until the host receipts
        // them; nothing deleted them before, so they grew to 8.5GB of
        // a 9.7GB local database. Run one bounded batch in the existing
        // single-flight maintenance worker, after retry queueing above
        // makes owed envelopes visible to the safety predicate. Never
        // put this writer on the event-loop connection: even a bounded
        // batch must not pause live transcript scheduling.
        if self.storage_maintenance_tasks.is_empty() {
            let db_path = self.projection_db_path.clone();
            self.storage_maintenance_tasks.spawn_blocking(move || {
                let result = crate::state::db::open_connection(&db_path).and_then(|conn| {
                    crate::state::cursor_store_records::drain_receipted_cursor_records(&conn)
                });
                match result {
                    Ok(0) => {}
                    Ok(drained) => {
                        tracing::info!(drained, "drained bounded batch of receipted Cursor records")
                    }
                    Err(error) => tracing::warn!(
                        error = %error,
                        "Cursor record drain error"
                    ),
                }
            });
        }
        if queued_retries > 0 {
            tracing::debug!(
                queued_retries,
                "Queued durable retry paths on periodic refill"
            );
        }
    }
    fn on_outbox_tick(
        &mut self,
        config: &ConnectConfig,
        mut phase_projection_timer: std::pin::Pin<&mut tokio::time::Sleep>,
    ) {
        // The hook outbox is only one of five phase-ledger writers. The
        // Codex bridge, both Console adapters, and OpenCode all run as
        // separate processes and write the shared ledger directly, so
        // the daemon never sees their phases through the outbox at all
        // — those transitions fell through to the 60s reconciliation
        // and produced the long tail that survived the debounce.
        //
        // Watching the ledger watermark covers every producer at once,
        // including any added later, without each one having to signal
        // the daemon. One MAX() over a table with one row per session.
        if let Some(watermark) = latest_phase_watermark(&self.conn) {
            if self.last_phase_watermark != Some(watermark) {
                self.last_phase_watermark = Some(watermark);
                if arm_phase_projection(&mut self.phase_projection_pending) {
                    phase_projection_timer
                        .as_mut()
                        .reset(tokio::time::Instant::now() + PHASE_PROJECTION_DEBOUNCE);
                }
            }
        }
        // Files remain durable while a POST is in flight. Do not scan,
        // parse, and persist them again every 100ms until that attempt
        // has either removed them or made them eligible for retry.
        if self.outbox_collect_tasks.is_empty() && self.outbox_post_tasks.is_empty() {
            let outbox_dir = self.outbox_dir.clone();
            let db_path = config.shipper_config.db_path.clone();
            self.outbox_collect_tasks.spawn_blocking(move || {
                let started = Instant::now();
                let presence =
                    outbox::collect_outbox_with_local_state_result(&outbox_dir, db_path.as_deref());
                OutboxCollectResult {
                    presence,
                    elapsed_ms: started.elapsed().as_millis() as u64,
                }
            });
        }
        if self.status_slot_tasks.is_empty() {
            let agent_dir = self
                .runtime_events_outbox_dir
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| self.runtime_events_outbox_dir.clone());
            let db_path = config.shipper_config.db_path.clone();
            let already_recorded = self.status_recorded.clone();
            let owner_evidence = self.last_status_owners.clone();
            let owner_evidence_fresh = status_owner_snapshot_is_fresh(self.last_status_owners_at);
            let owner_refresh_cursor = self.status_owner_refresh_cursor;
            let managed_refresh_cursor = self.managed_owner_refresh_cursor;
            self.status_slot_tasks.spawn_blocking(move || {
                let started = Instant::now();
                let dir = crate::status_slot::status_slot_dir(&agent_dir);
                let slots = crate::status_slot::read_all(&dir);
                let claims = read_status_slot_claims_for_slots(&slots);
                let (owners, owner_refresh_cursor, next_managed_refresh_cursor) =
                    status_owner_evidence_for_slots(
                        &slots,
                        &claims,
                        owner_evidence.as_ref(),
                        owner_evidence_fresh,
                        crate::heartbeat::machine_boot_id().as_deref(),
                        owner_refresh_cursor,
                        managed_refresh_cursor,
                    );
                let slots = reconcile_status_slots(&dir, slots, &owners);
                // The phase ledger is local truth, and the daemon is
                // its single writer. Recording here is what lets a
                // provider callback stop writing a file per frame.
                let recorded =
                    record_status_slot_phases(db_path.as_deref(), &slots, &already_recorded);
                StatusSlotResult {
                    slots,
                    recorded,
                    owner_refresh_cursor,
                    managed_owner_refresh_cursor: next_managed_refresh_cursor,
                    elapsed_ms: started.elapsed().as_millis() as u64,
                }
            });
        }
        if self.runtime_collect_tasks.is_empty()
            && self.runtime_outbox_post_tasks.is_empty()
            && self
                .runtime_outbox_retry_after
                .map(|retry_at| Instant::now() >= retry_at)
                .unwrap_or(true)
        {
            let runtime_events_outbox_dir = self.runtime_events_outbox_dir.clone();
            self.runtime_collect_tasks.spawn_blocking(move || {
                let started = Instant::now();
                let pass = outbox::collect_runtime_event_outbox_pass(&runtime_events_outbox_dir);
                let measurement = heartbeat::RuntimeEventOutboxSnapshot {
                    pending_count: pass.pending_count,
                    pending_count_is_lower_bound: pass.pending_count_is_lower_bound,
                    inspected_count: pass.inspected_count,
                    saturated: pass.saturated,
                    oldest_pending_at: pass.oldest_pending_at.map(|at| at.to_rfc3339()),
                    observed_at: pass.observed_at.map(|at| at.to_rfc3339()),
                };
                RuntimeCollectResult {
                    posts: pass.posts,
                    measurement,
                    elapsed_ms: started.elapsed().as_millis() as u64,
                    saturated: pass.saturated,
                }
            });
        }
    }
    fn on_flight_sample_tick(&mut self, config: &ConnectConfig) {
        if let Some(recorder) = self.flight_recorder.as_ref() {
            record_flight_sample(
                recorder,
                &self.outbox_dir,
                &self.runtime_events_outbox_dir,
                &self.control_channel_status,
                &self.ship_stats,
                &config.shipper_config.machine_name,
                self.in_flight.len(),
                self.scheduler.has_pending_work(),
                self.deferred_retries.len(),
                self.offline.is_offline,
            );
        }
    }
    async fn on_disk_guard_tick(&mut self, config: &ConnectConfig) {
        if let Some(guard) = self.disk_guard.as_mut() {
            let outcome = guard.tick(&disk_guard_sessions(&self.last_managed_observations));
            if let Some((title, body)) = outcome.notify.as_ref() {
                crate::disk_guard::notify_desktop(title, body);
            }
            for steer in outcome.steers {
                let shipper_config = config.shipper_config.clone();
                tokio::spawn(async move {
                    if let Err(error) = crate::control_channel::steer_local_session(
                        &shipper_config,
                        &steer.provider,
                        &steer.session_id,
                        &steer.text,
                    )
                    .await
                    {
                        tracing::info!(
                            session_id = %steer.session_id,
                            provider = %steer.provider,
                            %error,
                            "Disk guard could not steer session"
                        );
                    }
                });
            }
        }
    }
    async fn on_update_check_tick(&mut self) {
        // Spawned, not awaited. A download runs for as long as the
        // transfer takes, and awaiting it here would stop shipping,
        // heartbeat, and control work for that whole window.
        //
        // Runtime Host reachability is deliberately not consulted:
        // `offline` describes the Longhouse transport, which says
        // nothing about whether GitHub is reachable, and gating on it
        // would hide a stale machine whose own Runtime Host is down.
        let client = self.update_http_client.clone();
        tokio::task::spawn(async move {
            crate::update::run_check_tick(&client).await;
        });
    }
    fn on_prune_tick(&mut self) {
        let fs = FileState::new(&self.conn);
        match fs.prune_stale(30) {
            Ok(n) if n > 0 => tracing::info!("Daily prune: removed {} stale file_state entries", n),
            Ok(_) => {}
            Err(e) => tracing::warn!("Daily prune error: {}", e),
        }
        let sb = crate::state::session_binding::SessionBinding::new(&self.conn);
        match sb.prune_stale(30) {
            Ok(n) if n > 0 => {
                tracing::info!("Daily prune: removed {} stale session_binding entries", n)
            }
            Ok(_) => {}
            Err(e) => tracing::warn!("Session binding prune error: {}", e),
        }
        if self.daily_maintenance_tasks.is_empty() {
            let db_path = self.projection_db_path.clone();
            self.daily_maintenance_tasks.spawn_blocking(move || {
                crate::state::recover::run_daily_storage_maintenance(&db_path);
            });
        }
    }
    async fn on_local_status_tick(&mut self) {
        if let Some(gap) = self
            .wake_gap_detector
            .observe(SystemTime::now(), Instant::now())
        {
            self.shipping_progress.reset_after_sleep(Instant::now());
            tracing::info!(
                wake_gap_ms = gap.as_millis() as u64,
                "Detected system wake gap"
            );
            if maybe_start_managed_observation_scan(
                self.projection_db_path.clone(),
                &mut self.managed_observation_scan_tasks,
                "wake",
                true,
                &self.last_managed_observations,
            ) {
                self.managed_full_reconciliation_not_before =
                    Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                self.managed_reconciliation
                    .start("wake", chrono::Utc::now().to_rfc3339());
            } else {
                self.pending_wake_reconciliation = true;
                self.managed_reconciliation
                    .start("wake", chrono::Utc::now().to_rfc3339());
            }
        }
        let host_link_status = self.host_link.snapshot();
        if matches!(host_link_status.state.as_str(), "updating" | "slow_update")
            && self.host_link_poll_tasks.len() < 3
        {
            let client = self.client.clone();
            self.host_link_poll_tasks
                .spawn_local(async move { client.poll_runtime_admission().await });
        }
        if let Some(projection) = self.last_status_projection.as_mut() {
            projection.set_heartbeat_transport(self.heartbeat_transport.clone());
            projection.set_host_link(host_link_status.clone());
            heartbeat::write_status_file(
                projection,
                serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                &self.managed_reconciliation,
                &mut self.shipping_progress,
                self.offline.is_offline,
                &self.status_path,
            );
        } else {
            heartbeat::refresh_existing_status_pulse(
                &self.managed_reconciliation,
                &mut self.shipping_progress,
                self.offline.is_offline,
                &self.status_path,
                &self.heartbeat_transport,
                Some(&host_link_status),
            );
        }
    }
    fn on_managed_full_reconciliation_tick(&mut self) {
        if managed_full_reconciliation_ready(
            !self.pending_wake_reconciliation,
            self.managed_observation_scan_tasks.is_empty(),
            Instant::now(),
            self.managed_full_reconciliation_not_before,
        ) && maybe_start_managed_observation_scan(
            self.projection_db_path.clone(),
            &mut self.managed_observation_scan_tasks,
            "full_reconciliation",
            true,
            &self.last_managed_observations,
        ) {
            self.managed_full_reconciliation_not_before =
                Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
            self.managed_reconciliation
                .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
        } else {
            self.pending_full_reconciliation = true;
            self.managed_reconciliation
                .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
        }
    }
    fn on_managed_observation_tick(&mut self) {
        // Coalesce a periodic tick and watcher burst while a full scan
        // is running. A queued full request wins on the next tick, but
        // the cooldown prevents a completion/event feedback loop.
        if managed_full_reconciliation_ready(
            self.pending_full_reconciliation,
            self.managed_observation_scan_tasks.is_empty(),
            Instant::now(),
            self.managed_full_reconciliation_not_before,
        ) && maybe_start_managed_observation_scan(
            self.projection_db_path.clone(),
            &mut self.managed_observation_scan_tasks,
            "full_reconciliation",
            true,
            &self.last_managed_observations,
        ) {
            self.pending_full_reconciliation = false;
            self.managed_full_reconciliation_not_before =
                Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
            self.managed_reconciliation
                .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
        } else if !self.pending_full_reconciliation && !self.pending_wake_reconciliation {
            maybe_start_managed_observation_scan(
                self.projection_db_path.clone(),
                &mut self.managed_observation_scan_tasks,
                "periodic",
                self.last_resume_contracts.is_none(),
                &self.last_managed_observations,
            );
        }
    }
    fn on_machine_presence_tick(&mut self) {
        if !self.offline.is_offline {
            if self.machine_presence_post_tasks.is_empty() {
                spawn_machine_presence_post(
                    &mut self.machine_presence_post_tasks,
                    self.client.clone(),
                );
            } else {
                tracing::debug!(
                    "Skipping machine presence POST while previous POST is still in flight"
                );
            }
        }
    }
    fn on_truth_heartbeat_due(&mut self) {
        if let Some(pending) = self.pending_truth_heartbeat.take() {
            if !self.offline.is_offline
                && runtime_truth_changed(
                    self.last_runtime_truth_signature.as_deref(),
                    &pending.signature,
                )
            {
                let now = Instant::now();
                self.heartbeat_transport
                    .record_attempt(chrono::Utc::now().to_rfc3339());
                publish_heartbeat_transport_status(
                    &self.heartbeat_transport,
                    &mut self.last_status_projection,
                    serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.host_link,
                    &self.status_path,
                );
                spawn_heartbeat_post(
                    &mut self.heartbeat_post_tasks,
                    self.client.clone(),
                    pending.payload,
                    pending.signature.clone(),
                    "runtime_truth_change",
                    &mut self.acknowledged_machine_evidence,
                );
                self.last_truth_heartbeat_at = Some(now);
                self.last_runtime_truth_signature = Some(pending.signature);
            }
        }
    }
    fn on_heartbeat_tick(&mut self) {
        if let Some(projection) = self.last_status_projection.as_mut() {
            projection.set_heartbeat_transport(self.heartbeat_transport.clone());
            projection.set_host_link(self.host_link.snapshot());
            heartbeat::write_status_file(
                projection,
                serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                &self.managed_reconciliation,
                &mut self.shipping_progress,
                self.offline.is_offline,
                &self.status_path,
            );
            if !self.offline.is_offline {
                if self.heartbeat_post_tasks.is_empty() {
                    self.heartbeat_transport
                        .record_attempt(chrono::Utc::now().to_rfc3339());
                    projection.set_heartbeat_transport(self.heartbeat_transport.clone());
                    projection.set_host_link(self.host_link.snapshot());
                    heartbeat::write_status_file(
                        projection,
                        serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                        &self.managed_reconciliation,
                        &mut self.shipping_progress,
                        self.offline.is_offline,
                        &self.status_path,
                    );
                    let payload = projection.payload.clone();
                    let signature = runtime_truth_signature(&payload);
                    self.last_runtime_truth_signature = Some(signature.clone());
                    self.pending_truth_heartbeat = None;
                    spawn_heartbeat_post(
                        &mut self.heartbeat_post_tasks,
                        self.client.clone(),
                        payload,
                        signature,
                        "periodic_heartbeat",
                        &mut self.acknowledged_machine_evidence,
                    );
                } else {
                    tracing::debug!(
                        "Skipping periodic heartbeat while a heartbeat POST is still in flight"
                    );
                }
            }
        } else {
            tracing::debug!(
                "Skipping periodic heartbeat until the startup managed observation scan completes"
            );
        }
    }
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
    outbox_dir: &Path,
    runtime_events_outbox_dir: &Path,
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
        "runtime_event_outbox": crate::flight::runtime_event_outbox_snapshot(runtime_events_outbox_dir),
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

/// Per-shipment counter for the reducer identity window.
///
/// The heartbeat's identity budget is smaller than the fact set on a busy
/// machine, so the window has to move. A counter sweeps it; wall time does not,
/// because a periodic cadence can alias against a family's length and republish
/// the same entries forever.
///
/// It advances on **shipment**, not on projection build. Not every build is
/// POSTed — a change that arrives while a POST is in flight is dropped, and the
/// periodic heartbeat re-POSTs the previous projection — so advancing per build
/// would make the shipped windows an irregular subsample of the built ones, and
/// that subsample can alias exactly as wall time did. Reading the value at build
/// and advancing at ship means every window that is built is shipped before the
/// next one is chosen.
static EVIDENCE_ROTATION: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

fn current_evidence_rotation() -> usize {
    EVIDENCE_ROTATION.load(std::sync::atomic::Ordering::Relaxed)
}

fn advance_evidence_rotation() {
    EVIDENCE_ROTATION.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
}

fn runtime_truth_signature(payload: &heartbeat::HeartbeatPayload) -> String {
    let session_digest = payload
        .sessions_digest
        .clone()
        .unwrap_or_else(|| heartbeat::session_snapshot_digest(payload));
    let continuation_digest = heartbeat::continuation_evidence_digest(payload);
    format!("sessions={session_digest}|continuation={continuation_digest}")
}

fn runtime_truth_changed(previous: Option<&str>, current: &str) -> bool {
    previous != Some(current)
}
const TRUTH_HEARTBEAT_COALESCE_WINDOW: Duration = Duration::from_secs(1);

fn truth_heartbeat_due_at(last_sent: Option<Instant>, now: Instant) -> Instant {
    last_sent
        .map(|last_sent| (last_sent + TRUTH_HEARTBEAT_COALESCE_WINDOW).max(now))
        .unwrap_or(now)
}

fn inventory_change_requires_projection(
    previous_generation: Option<u64>,
    current_generation: u64,
    managed_pair_ready: bool,
) -> bool {
    managed_pair_ready && previous_generation != Some(current_generation)
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

fn storage_v2_backpressure_retry_delay(
    priority: WorkPriority,
    retry_after: Duration,
    host_updating: bool,
) -> Duration {
    if host_updating || priority != WorkPriority::Live {
        retry_after
    } else {
        retry_after.min(Duration::from_secs(1))
    }
}

fn maybe_start_unmanaged_binding_refresh(
    refresh_tasks: &mut JoinSet<UnmanagedBindingRefreshResult>,
    db_path: Option<PathBuf>,
    machine_id: String,
    excluded_managed_pids: HashSet<u32>,
    process_inventory: Vec<unmanaged_bindings::ProcessInfo>,
    reason: &'static str,
    generation: u64,
    managed_observation_generation: u64,
    managed: ManagedObservationSnapshot,
    managed_scan_partial: bool,
    full_reconciliation_candidate: bool,
) -> bool {
    if !refresh_tasks.is_empty() {
        return false;
    }

    refresh_tasks.spawn_blocking(move || {
        let started = Instant::now();
        let result = crate::state::db::resolve_db_path(db_path.as_deref())
            .and_then(|path| crate::state::db::open_connection(&path))
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
            managed_observation_generation,
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

fn publish_heartbeat_transport_status(
    heartbeat_transport: &heartbeat::HeartbeatTransportStatus,
    last_status_projection: &mut Option<heartbeat::StatusFileProjection>,
    control_channel: Option<serde_json::Value>,
    managed_reconciliation: &heartbeat::ProjectionReconciliation,
    shipping_progress: &mut heartbeat::ShippingProgressObservation,
    is_offline: bool,
    host_link: &crate::host_link::HostLink,
    status_path: &Path,
) {
    let host_link_status = host_link.snapshot();
    if let Some(projection) = last_status_projection.as_mut() {
        projection.set_heartbeat_transport(heartbeat_transport.clone());
        projection.set_host_link(host_link_status);
        heartbeat::write_status_file(
            projection,
            control_channel,
            managed_reconciliation,
            shipping_progress,
            is_offline,
            status_path,
        );
    } else {
        heartbeat::refresh_existing_status_pulse(
            managed_reconciliation,
            shipping_progress,
            is_offline,
            status_path,
            heartbeat_transport,
            Some(&host_link_status),
        );
    }
}

fn spawn_heartbeat_post(
    tasks: &mut JoinSet<HeartbeatPostResult>,
    client: ShipperClient,
    mut payload: heartbeat::HeartbeatPayload,
    signature: String,
    reason: &'static str,
    acknowledged: &mut heartbeat::AcknowledgedEvidenceHashes,
) {
    if let Err(error) = heartbeat::prepare_machine_evidence_for_send(
        &mut payload,
        acknowledged,
        current_evidence_rotation(),
    ) {
        tasks.spawn_local(async move {
            HeartbeatPostResult {
                signature,
                reason,
                result: Err(error),
                metrics: heartbeat::HeartbeatPostMetrics::default(),
                sent_evidence_identities: Vec::new(),
                join_elapsed_ms: 0,
                task_elapsed_ms: 0,
            }
        });
        return;
    }
    // This payload's identity window is now committed to the wire, so the next
    // projection may choose the next one. See `EVIDENCE_ROTATION`.
    advance_evidence_rotation();
    tasks.spawn_local(async move {
        let join_started = Instant::now();
        let heartbeat_task = tokio::spawn(async move {
            let task_started = Instant::now();
            let attempt = heartbeat::send_heartbeat(&client, payload).await;
            (attempt, task_started.elapsed().as_millis() as u64)
        });
        let (attempt, task_elapsed_ms) = match heartbeat_task.await {
            Ok((attempt, task_elapsed_ms)) => (attempt, task_elapsed_ms),
            Err(err) => {
                let elapsed_ms = join_started.elapsed().as_millis() as u64;
                (
                    heartbeat::HeartbeatSendAttempt {
                        result: Err(format!("heartbeat POST worker task failed: {err}")),
                        metrics: heartbeat::HeartbeatPostMetrics {
                            latency_ms: elapsed_ms,
                            ..heartbeat::HeartbeatPostMetrics::default()
                        },
                        sent_evidence_identities: Vec::new(),
                    },
                    elapsed_ms,
                )
            }
        };
        HeartbeatPostResult {
            signature,
            reason,
            result: attempt.result,
            metrics: attempt.metrics,
            sent_evidence_identities: attempt.sent_evidence_identities,
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

#[cfg(test)]
mod tests {
    /// The daemon is the phase ledger's single writer, which is what lets an
    /// OMP callback stop handing it a file per frame. It records from the
    /// session's status slot instead — but only what changed, because an
    /// unchanged phase rewritten every 100ms bumps the ledger revision, and
    /// the projection debounce watches that watermark: the daemon would
    /// schedule a rebuild forever.
    #[test]
    fn status_slots_write_the_phase_ledger_only_when_they_change() {
        use std::collections::HashMap;

        let temp = tempfile::tempdir().expect("tempdir");
        let db_path = temp.path().join("agent.db");
        // The daemon bootstraps the schema once at startup; this stands in for
        // that, because the recorder deliberately uses the hot-path opener.
        crate::state::db::open_db(Some(&db_path)).expect("bootstrap ledger schema");
        let slot = |seq: u64, phase: &str, observed_at: &str| crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: "session-1".into(),
            provider: "omp".into(),
            runtime_key: "omp:session-1".into(),
            run_id: "run-1".into(),
            source: "omp_helm_channel".into(),
            phase: phase.into(),
            tool_name: None,
            observed_at: observed_at.into(),
            payload: serde_json::json!({}),
            preview: None,
            producer_epoch: "epoch-1".into(),
            seq,
        };

        let mut recorded: HashMap<String, (String, u64)> = HashMap::new();
        let first = super::record_status_slot_phases(
            Some(&db_path),
            &[slot(1, "thinking", "2026-09-17T15:00:01Z")],
            &recorded,
        );
        assert_eq!(first.len(), 1, "a new observation reaches the ledger");
        for (session_id, version) in first {
            recorded.insert(session_id, version);
        }

        // The same slot, seen again on the next tick.
        let unchanged = super::record_status_slot_phases(
            Some(&db_path),
            &[slot(1, "thinking", "2026-09-17T15:00:01Z")],
            &recorded,
        );
        assert!(unchanged.is_empty(), "an unchanged slot writes nothing");

        let advanced = super::record_status_slot_phases(
            Some(&db_path),
            &[slot(2, "idle", "2026-09-17T15:00:02Z")],
            &recorded,
        );
        assert_eq!(advanced.len(), 1, "a real transition is recorded");

        let connection = crate::state::db::open_connection(&db_path).expect("open ledger");
        let phase: String = connection
            .query_row(
                "SELECT phase FROM session_phase_state WHERE session_id = ?1",
                rusqlite::params!["session-1"],
                |row| row.get(0),
            )
            .expect("ledger row");
        assert_eq!(phase, "idle");
    }

    #[test]
    fn managed_scan_recovers_console_status_and_revalidates_it_until_terminal() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        temp_env::with_vars(
            [
                ("HOME", Some(temp.path().as_os_str())),
                ("LONGHOUSE_HOME", Some(temp.path().as_os_str())),
            ],
            || {
                runtime.block_on(async {
                    let registry = crate::turn_claims::default_registry().unwrap();
                    let session = uuid::Uuid::new_v4().to_string();
                    let run = uuid::Uuid::new_v4().to_string();
                    registry
                        .claim(
                            &run,
                            &session,
                            &uuid::Uuid::new_v4().to_string(),
                            None,
                            None,
                            "codex",
                        )
                        .unwrap();
                    crate::status_slot::publish_console_phase(
                        "codex",
                        "codex_app_server",
                        &session,
                        &run,
                        &chrono::Utc::now().to_rfc3339(),
                        "thinking",
                        None,
                        serde_json::json!({"execution_lifetime": "persistent"}),
                    );
                    let status_dir = crate::status_slot::status_slot_dir(
                        &crate::config::get_agent_dir().unwrap(),
                    );
                    let process = crate::process_identity::try_collect_process_facts_by_pid()
                        .unwrap()
                        .remove(&std::process::id())
                        .unwrap();
                    let mut scans = tokio::task::JoinSet::new();
                    for stage in 0..4 {
                        if stage == 1 {
                            registry
                                .mark_spawned(
                                    &run,
                                    Some(process.pid),
                                    None,
                                    Some(process.lstart.clone()),
                                    "codex_exec",
                                    serde_json::json!({}),
                                )
                                .unwrap();
                        } else if stage == 3 {
                            registry.mark_terminal(&run, "run_completed", None).unwrap();
                        }
                        assert!(super::maybe_start_managed_observation_scan(
                            temp.path().join("state.db"),
                            &mut scans,
                            "test",
                            false,
                            &super::ManagedObservationSnapshot::default(),
                        ));
                        let result = scans.join_next().await.unwrap().unwrap();
                        let visible = super::reconcile_status_slots(
                            &status_dir,
                            crate::status_slot::read_all(&status_dir),
                            &result.status_owners,
                        );
                        if stage == 1 || stage == 2 {
                            assert_eq!(visible.len(), 1);
                            assert_eq!(visible[0].run_id, run);
                            assert_eq!(visible[0].phase, "thinking");
                        } else {
                            assert!(visible.is_empty());
                        }
                        assert_eq!(
                            crate::status_slot::slot_path(&status_dir, &session).exists(),
                            stage != 3,
                        );
                    }
                });
            },
        );
    }

    #[test]
    fn console_status_tracks_the_exact_claim_until_terminal_and_not_after() {
        let temp = tempfile::tempdir().expect("tempdir");
        let status_dir = crate::status_slot::status_slot_dir(temp.path());
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("turn-claims"));
        let session_id = uuid::Uuid::new_v4().to_string();
        let run_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "codex")
            .expect("claim");
        let process_start = "Mon Jan  1 00:00:00 2024";
        let mut spawned = registry
            .mark_spawned(
                &run_id,
                Some(424_242),
                Some(424_242),
                Some(process_start.into()),
                "codex_exec",
                serde_json::json!({}),
            )
            .expect("spawn");
        let boot_id = "test-boot";
        spawned.boot_id = Some(boot_id.into());
        let mut process_facts = std::collections::HashMap::new();
        process_facts.insert(
            424_242,
            crate::process_identity::ProcessFact {
                pid: 424_242,
                tty: "??".into(),
                stat: "S".into(),
                lstart: process_start.into(),
                command: "fake-codex".into(),
                start_time: None,
            },
        );
        let slot = crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: session_id.clone(),
            provider: "codex".into(),
            runtime_key: format!("codex:{session_id}"),
            run_id: run_id.clone(),
            source: "codex_app_server".into(),
            phase: "thinking".into(),
            tool_name: None,
            observed_at: chrono::Utc::now().to_rfc3339(),
            payload: serde_json::json!({}),
            preview: None,
            producer_epoch: "epoch".into(),
            seq: 1,
        };
        crate::status_slot::publish(&status_dir, &slot).expect("publish");
        let live_owners = super::status_owner_evidence_from_claims(
            &[spawned.clone()],
            Some(&process_facts),
            Some(boot_id),
        );
        let live = super::reconcile_status_slots(
            &status_dir,
            crate::status_slot::read_all(&status_dir),
            &live_owners,
        );
        assert_eq!(live.len(), 1, "an exact live Console owner keeps status");
        assert_eq!(
            super::StatusLedger::default()
                .pending(live, std::time::Instant::now())
                .len(),
            1,
            "the current Console status reaches the delivery lane"
        );

        let unknown_owners =
            super::status_owner_evidence_from_claims(&[spawned.clone()], None, Some(boot_id));
        let unknown = super::reconcile_status_slots(
            &status_dir,
            crate::status_slot::read_all(&status_dir),
            &unknown_owners,
        );
        assert!(
            unknown.is_empty(),
            "without a valid process inventory, a slot is not asserted"
        );
        assert!(
            crate::status_slot::slot_path(&status_dir, &session_id).exists(),
            "unknown ownership does not delete the source slot"
        );
        assert!(super::StatusLedger::default()
            .pending(unknown, std::time::Instant::now())
            .is_empty());
        assert_eq!(registry.read(&run_id).unwrap().state, "spawned");

        let terminal = registry
            .mark_terminal(&run_id, "run_completed", None)
            .expect("terminal");
        let terminal_owners =
            super::status_owner_evidence_from_claims(&[terminal], None, Some(boot_id));
        let closed = super::reconcile_status_slots(
            &status_dir,
            crate::status_slot::read_all(&status_dir),
            &terminal_owners,
        );
        assert!(closed.is_empty());
        assert!(
            !crate::status_slot::slot_path(&status_dir, &session_id).exists(),
            "a terminal claim retires only its matching fake-run slot"
        );
        assert!(
            super::StatusLedger::default()
                .pending(closed, std::time::Instant::now())
                .is_empty(),
            "a closed fake run cannot be reasserted"
        );
    }

    #[test]
    fn status_slot_cadence_recovers_unknown_console_owner_and_new_run() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().expect("tempdir");
        let status_dir = crate::status_slot::status_slot_dir(temp.path());
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("turn-claims"));
        let session_id = uuid::Uuid::new_v4().to_string();
        let first_run = uuid::Uuid::new_v4().to_string();
        let first_thread = uuid::Uuid::new_v4().to_string();
        registry
            .claim(&first_run, &session_id, &first_thread, None, None, "codex")
            .expect("first claim");
        let pid = std::process::id();
        let process =
            crate::process_identity::try_collect_process_fact(pid).expect("test process identity");
        let slot_for = |run_id: &str, seq| crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: session_id.clone(),
            provider: "codex".into(),
            runtime_key: format!("codex:{session_id}"),
            run_id: run_id.into(),
            source: "codex_app_server".into(),
            phase: "thinking".into(),
            tool_name: None,
            observed_at: chrono::Utc::now().to_rfc3339(),
            payload: serde_json::json!({"execution_lifetime": "persistent"}),
            preview: None,
            producer_epoch: "epoch".into(),
            seq,
        };
        let first_slot = slot_for(&first_run, 1);
        crate::status_slot::publish(&status_dir, &first_slot).expect("publish first slot");
        let claimed = registry.read(&first_run).expect("read claim");
        let (unknown, cursor, managed_cursor) = super::status_owner_evidence_for_slots(
            std::slice::from_ref(&first_slot),
            &[claimed],
            &super::StatusOwnerEvidence::default(),
            false,
            Some("test-boot"),
            0,
            0,
        );
        assert!(unknown.active.is_empty());
        assert!(
            super::reconcile_status_slots(
                &status_dir,
                crate::status_slot::read_all(&status_dir),
                &unknown,
            )
            .is_empty(),
            "an unspawned claim must not assert status"
        );
        assert!(crate::status_slot::slot_path(&status_dir, &session_id).exists());

        let mut first_spawn = registry
            .mark_spawned(
                &first_run,
                Some(pid),
                Some(i32::try_from(pid).expect("pid fits process group")),
                Some(process.lstart.clone()),
                "codex_exec",
                serde_json::json!({}),
            )
            .expect("spawn first owner");
        first_spawn.boot_id = Some("test-boot".into());
        let (first_live, cursor, managed_cursor) = super::status_owner_evidence_for_slots(
            std::slice::from_ref(&first_slot),
            &[first_spawn],
            &super::StatusOwnerEvidence::default(),
            false,
            Some("test-boot"),
            cursor,
            managed_cursor,
        );
        let first_key = super::status_owner_key("codex", &session_id, &first_run).unwrap();
        assert!(first_live.active.contains(&first_key));
        assert_eq!(
            super::reconcile_status_slots(
                &status_dir,
                crate::status_slot::read_all(&status_dir),
                &first_live,
            )
            .len(),
            1,
            "the one-second slot pass resolves the exact new process identity"
        );

        let second_run = uuid::Uuid::new_v4().to_string();
        registry
            .claim(
                &second_run,
                &session_id,
                &uuid::Uuid::new_v4().to_string(),
                None,
                None,
                "codex",
            )
            .expect("successor claim");
        let mut second_spawn = registry
            .mark_spawned(
                &second_run,
                Some(pid),
                Some(i32::try_from(pid).expect("pid fits process group")),
                Some(process.lstart),
                "codex_exec",
                serde_json::json!({}),
            )
            .expect("spawn successor");
        second_spawn.boot_id = Some("test-boot".into());
        let second_slot = slot_for(&second_run, 2);
        crate::status_slot::publish(&status_dir, &second_slot).expect("publish successor slot");
        let (second_live, _, _) = super::status_owner_evidence_for_slots(
            std::slice::from_ref(&second_slot),
            &[second_spawn],
            &first_live,
            true,
            Some("test-boot"),
            cursor,
            managed_cursor,
        );
        let second_key = super::status_owner_key("codex", &session_id, &second_run).unwrap();
        assert!(second_live.active.contains(&second_key));
        assert_eq!(
            super::reconcile_status_slots(
                &status_dir,
                crate::status_slot::read_all(&status_dir),
                &second_live,
            )
            .len(),
            1,
            "a successor run is resolved without inheriting its predecessor's owner"
        );
    }

    #[test]
    fn stale_owner_snapshot_does_not_renew_helm_status() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().expect("tempdir");
        let status_dir = crate::status_slot::status_slot_dir(temp.path());
        let session_id = uuid::Uuid::new_v4().to_string();
        let run_id = uuid::Uuid::new_v4().to_string();
        let slot = crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: session_id.clone(),
            provider: "omp".into(),
            runtime_key: format!("omp:{session_id}"),
            run_id: run_id.clone(),
            source: "omp_helm_channel".into(),
            phase: "thinking".into(),
            tool_name: None,
            observed_at: chrono::Utc::now().to_rfc3339(),
            payload: serde_json::json!({"execution_lifetime": "interactive"}),
            preview: None,
            producer_epoch: "epoch".into(),
            seq: 1,
        };
        crate::status_slot::publish(&status_dir, &slot).expect("publish");
        let mut stale = super::StatusOwnerEvidence::default();
        stale
            .active
            .insert(super::status_owner_key("omp", &session_id, &run_id).unwrap());
        let stale_at = Some(
            std::time::Instant::now()
                - super::STATUS_OWNER_SNAPSHOT_MAX_AGE
                - std::time::Duration::from_secs(1),
        );
        let stale_snapshot_fresh = super::status_owner_snapshot_is_fresh(stale_at);
        assert!(!stale_snapshot_fresh);

        let (current, _, _) = temp_env::with_vars(
            [
                ("HOME", Some(temp.path().as_os_str())),
                ("LONGHOUSE_HOME", Some(temp.path().as_os_str())),
            ],
            || {
                super::status_owner_evidence_for_slots(
                    std::slice::from_ref(&slot),
                    &[],
                    &stale,
                    stale_snapshot_fresh,
                    None,
                    0,
                    0,
                )
            },
        );
        assert!(current.active.is_empty());
        assert!(super::reconcile_status_slots(
            &status_dir,
            crate::status_slot::read_all(&status_dir),
            &current,
        )
        .is_empty());
        assert!(
            crate::status_slot::slot_path(&status_dir, &session_id).exists(),
            "stale or unavailable process evidence is unknown, not terminal"
        );
    }

    #[test]
    fn new_helm_status_resolves_from_exact_state_without_prior_scan() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().expect("tempdir");
        temp_env::with_vars(
            [
                ("HOME", Some(temp.path().as_os_str())),
                ("LONGHOUSE_HOME", Some(temp.path().as_os_str())),
            ],
            || {
                let session_id = uuid::Uuid::new_v4().to_string();
                let run_id = uuid::Uuid::new_v4().to_string();
                let pid = std::process::id();
                let process = crate::process_identity::try_collect_process_fact(pid)
                    .expect("test process identity");
                let state_dir = crate::managed_omp_helm_scan::default_omp_helm_state_dir()
                    .expect("OMP state directory");
                std::fs::create_dir_all(&state_dir).expect("create OMP state directory");
                let state_path = state_dir.join(format!("{session_id}.json"));
                let now = chrono::Utc::now().to_rfc3339();
                std::fs::write(
                    &state_path,
                    serde_json::to_vec(&serde_json::json!({
                        "session_id": session_id.clone(),
                        "run_id": run_id.clone(),
                        "launcher_pid": pid,
                        "launcher_process_start_time": process.lstart.clone(),
                        "provider_pid": pid,
                        "provider_process_start_time": process.lstart.clone(),
                        "started_at": now.clone(),
                        "updated_at": now,
                        "status": "ready",
                        "ready": true
                    }))
                    .expect("serialize OMP state"),
                )
                .expect("write OMP state");
                let status_dir = crate::status_slot::status_slot_dir(
                    &crate::config::get_agent_dir().expect("agent directory"),
                );
                let slot = crate::status_slot::StatusSlot {
                    schema: crate::status_slot::STATUS_SLOT_SCHEMA,
                    session_id: session_id.clone(),
                    provider: "omp".into(),
                    runtime_key: format!("omp:{session_id}"),
                    run_id: run_id.clone(),
                    source: crate::omp_helm_control::OMP_HELM_TRANSPORT.into(),
                    phase: "thinking".into(),
                    tool_name: None,
                    observed_at: chrono::Utc::now().to_rfc3339(),
                    payload: serde_json::json!({"execution_lifetime": "interactive"}),
                    preview: None,
                    producer_epoch: "epoch".into(),
                    seq: 1,
                };
                crate::status_slot::publish(&status_dir, &slot).expect("publish Helm slot");
                let (owners, _, _) = super::status_owner_evidence_for_slots(
                    std::slice::from_ref(&slot),
                    &[],
                    &super::StatusOwnerEvidence::default(),
                    false,
                    None,
                    0,
                    0,
                );
                let key = super::status_owner_key("omp", &session_id, &run_id).unwrap();
                assert!(owners.active.contains(&key));
                assert_eq!(
                    super::reconcile_status_slots(
                        &status_dir,
                        crate::status_slot::read_all(&status_dir),
                        &owners,
                    )
                    .len(),
                    1,
                    "a new Helm owner resolves on the first slot pass without a managed scan"
                );
            },
        );
    }

    #[test]
    fn daemon_scan_replays_retained_terminal_event_before_retiring_status() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().expect("tempdir");
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("runtime");
        temp_env::with_vars(
            [
                ("HOME", Some(temp.path().as_os_str())),
                ("LONGHOUSE_HOME", Some(temp.path().as_os_str())),
            ],
            || {
                runtime.block_on(async {
                    let registry = crate::turn_claims::default_registry().expect("registry");
                    let session_id = uuid::Uuid::new_v4().to_string();
                    let run_id = uuid::Uuid::new_v4().to_string();
                    registry
                        .claim(
                            &run_id,
                            &session_id,
                            &uuid::Uuid::new_v4().to_string(),
                            None,
                            None,
                            "codex",
                        )
                        .expect("claim");
                    let status_dir = crate::status_slot::status_slot_dir(
                        &crate::config::get_agent_dir().expect("agent directory"),
                    );
                    let slot = crate::status_slot::StatusSlot {
                        schema: crate::status_slot::STATUS_SLOT_SCHEMA,
                        session_id: session_id.clone(),
                        provider: "codex".into(),
                        runtime_key: format!("codex:{session_id}"),
                        run_id: run_id.clone(),
                        source: "codex_app_server".into(),
                        phase: "thinking".into(),
                        tool_name: None,
                        observed_at: chrono::Utc::now().to_rfc3339(),
                        payload: serde_json::json!({"execution_lifetime": "persistent"}),
                        preview: None,
                        producer_epoch: "epoch".into(),
                        seq: 1,
                    };
                    crate::status_slot::publish(&status_dir, &slot).expect("publish");
                    let event = serde_json::json!({
                        "runtime_key": format!("codex:{session_id}"),
                        "session_id": session_id,
                        "provider": "codex",
                        "run_id": run_id,
                        "source": "codex_app_server",
                        "kind": "terminal_signal",
                        "occurred_at": chrono::Utc::now().to_rfc3339(),
                        "dedupe_key": format!("test-terminal:{run_id}"),
                        "payload": {"terminal_state": "run_completed"},
                    });
                    registry
                        .mark_terminal_with_event(&run_id, "run_completed", None, event.clone())
                        .expect("retain terminal event")
                        .expect("event remains pending");
                    let outbox_dir =
                        crate::config::get_agent_runtime_events_outbox_dir().expect("outbox");
                    std::fs::write(&outbox_dir, b"not a directory").expect("block outbox");
                    assert!(
                        crate::outbox::retry_retained_terminal_event(
                            &registry,
                            &outbox_dir,
                            &run_id,
                        )
                        .is_err(),
                        "the first terminal handoff attempt must fail"
                    );
                    assert!(!registry
                        .terminal_event_handed_off(&run_id)
                        .expect("read failed handoff"));
                    assert_eq!(
                        registry
                            .pending_terminal_event(&run_id)
                            .expect("pending event"),
                        Some(event.clone())
                    );
                    std::fs::remove_file(&outbox_dir).expect("unblock outbox");
                    let pending_claim = registry.read(&run_id).expect("read pending claim");
                    let pending =
                        super::status_owner_evidence_from_claims(&[pending_claim], None, None);
                    assert!(super::reconcile_status_slots(
                        &status_dir,
                        crate::status_slot::read_all(&status_dir),
                        &pending,
                    )
                    .is_empty());
                    assert!(
                        crate::status_slot::slot_path(&status_dir, &slot.session_id).exists(),
                        "a pending terminal handoff suppresses status but keeps its slot"
                    );

                    let mut scans = tokio::task::JoinSet::new();
                    assert!(super::maybe_start_managed_observation_scan(
                        temp.path().join("state.db"),
                        &mut scans,
                        "terminal_replay_test",
                        false,
                        &super::ManagedObservationSnapshot::default(),
                    ));
                    let result = scans.join_next().await.expect("scan task").expect("scan");
                    assert!(
                        registry
                            .terminal_event_handed_off(&run_id)
                            .expect("read handoff state"),
                        "the daemon scan durably replays the retained event"
                    );
                    let still_pending = super::reconcile_status_slots(
                        &status_dir,
                        crate::status_slot::read_all(&status_dir),
                        &result.status_owners,
                    );
                    assert!(still_pending.is_empty());
                    assert!(
                        crate::status_slot::read_all(&status_dir)
                            .iter()
                            .any(|current| current.session_id == slot.session_id),
                        "the scan's pre-handoff evidence cannot retire before acknowledgment"
                    );

                    let handed_off = registry.read(&run_id).expect("read handed-off claim");
                    let ended = super::status_owner_evidence_from_claims(&[handed_off], None, None);
                    assert!(super::reconcile_status_slots(
                        &status_dir,
                        crate::status_slot::read_all(&status_dir),
                        &ended,
                    )
                    .is_empty());
                    assert!(
                        crate::status_slot::read_all(&status_dir)
                            .iter()
                            .all(|current| current.session_id != slot.session_id),
                        "status is retired only after its exact terminal event is handed off"
                    );

                    let outbox_dir = crate::config::get_agent_runtime_events_outbox_dir()
                        .expect("runtime-event outbox");
                    let found = std::fs::read_dir(&outbox_dir)
                        .expect("outbox entries")
                        .filter_map(std::result::Result::ok)
                        .filter(|entry| {
                            entry.path().extension().and_then(|value| value.to_str())
                                == Some("json")
                        })
                        .any(|entry| {
                            std::fs::read(entry.path())
                                .ok()
                                .and_then(|bytes| {
                                    serde_json::from_slice::<serde_json::Value>(&bytes).ok()
                                })
                                .is_some_and(|queued| queued == event)
                        });
                    assert!(
                        found,
                        "the exact retained terminal event reaches the outbox"
                    );
                    // Closing a parked process is recoverable even after its
                    // response status has already been retired.
                    registry
                        .record_invocation_state(&run_id, "parked", 2)
                        .unwrap();
                    let mut closing = event.clone();
                    closing["kind"] = serde_json::json!("invocation_closed");
                    closing["dedupe_key"] = serde_json::json!(format!("close:{run_id}"));
                    closing["payload"] = serde_json::json!({
                        "invocation_id": run_id,
                        "reason": "machine_agent_restart",
                        "stopped": [{"kind": "invocation", "id": run_id}]
                    });
                    registry
                        .retain_invocation_close_event(&run_id, closing.clone())
                        .unwrap();
                    let reloaded = crate::turn_claims::default_registry().unwrap();
                    assert!(reloaded
                        .read(&run_id)
                        .unwrap()
                        .has_pending_runtime_handoff());
                    assert!(super::maybe_start_managed_observation_scan(
                        temp.path().join("state.db"),
                        &mut scans,
                        "invocation_close_replay_test",
                        false,
                        &super::ManagedObservationSnapshot::default(),
                    ));
                    scans.join_next().await.unwrap().unwrap();
                    let closed = reloaded.read(&run_id).unwrap();
                    assert_eq!(closed.invocation_state.as_deref(), Some("closed"));
                    assert_eq!(closed.invocation_close_event.as_ref(), Some(&closing));
                    assert!(closed.invocation_close_event_handed_off);
                    assert_eq!(closed.terminal_event.as_ref(), Some(&event));
                    assert_eq!(
                        closed.result.as_ref().unwrap()["terminal_state"],
                        "run_completed"
                    );
                    registry
                        .record_invocation_state(&run_id, "parked", 99)
                        .unwrap();
                    let late = registry.read(&run_id).unwrap();
                    assert_eq!(late.invocation_state.as_deref(), Some("closed"));
                    assert_eq!(late.pending_count, 2);
                });
            },
        );
    }

    #[test]
    fn live_helm_status_is_delivered_but_failed_inventory_is_not_terminal() {
        let temp = tempfile::tempdir().expect("tempdir");
        let status_dir = crate::status_slot::status_slot_dir(temp.path());
        let observation = omp_observation(None, true);
        let mut process_facts = std::collections::HashMap::new();
        process_facts.insert(
            2,
            crate::process_identity::ProcessFact {
                pid: 2,
                tty: "??".into(),
                stat: "S".into(),
                lstart: "start".into(),
                command: "omp".into(),
                start_time: None,
            },
        );
        let live_scan = super::ManagedObservationScanResult {
            process_inventory_valid: true,
            omp_observations: vec![observation.clone()],
            ..Default::default()
        };
        let live_owners = super::status_owner_evidence_from_scan(&live_scan, &process_facts);
        let slot = crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: observation.session_id.clone(),
            provider: "omp".into(),
            runtime_key: "omp:session-omp".into(),
            run_id: observation.run_id.clone().unwrap(),
            source: "omp_helm_channel".into(),
            phase: "thinking".into(),
            tool_name: None,
            observed_at: chrono::Utc::now().to_rfc3339(),
            payload: serde_json::json!({}),
            preview: None,
            producer_epoch: "epoch".into(),
            seq: 1,
        };
        crate::status_slot::publish(&status_dir, &slot).expect("publish");
        let live = super::reconcile_status_slots(
            &status_dir,
            crate::status_slot::read_all(&status_dir),
            &live_owners,
        );
        assert_eq!(
            super::StatusLedger::default()
                .pending(live, std::time::Instant::now())
                .len(),
            1,
            "a matching live Helm process keeps status responsive"
        );

        let unknown_scan = super::ManagedObservationScanResult {
            omp_observations: vec![observation.clone()],
            ..Default::default()
        };
        let unknown_owners = super::status_owner_evidence_from_scan(&unknown_scan, &process_facts);
        let unknown = super::reconcile_status_slots(
            &status_dir,
            crate::status_slot::read_all(&status_dir),
            &unknown_owners,
        );
        assert!(
            unknown.is_empty(),
            "failed inventory is not a live assertion"
        );
        assert!(crate::status_slot::slot_path(&status_dir, "session-omp").exists());
        assert!(super::StatusLedger::default()
            .pending(unknown, std::time::Instant::now())
            .is_empty());
    }

    #[test]
    fn rejected_status_slots_retry_on_cadence_and_newer_versions_replace_them() {
        use std::time::{Duration, Instant};

        let slot = |seq| crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: "session-1".into(),
            provider: "omp".into(),
            runtime_key: "omp:session-1".into(),
            run_id: "run-1".into(),
            source: "omp_helm_channel".into(),
            phase: "thinking".into(),
            tool_name: None,
            observed_at: "2026-09-17T15:00:01Z".into(),
            payload: serde_json::json!({}),
            preview: None,
            producer_epoch: "epoch-1".into(),
            seq,
        };
        let current = slot(1);
        let now = Instant::now();
        let accepted = (current.version(), now);
        assert_eq!(
            super::status_slot_pending(&current, None, None, now),
            Some(true),
            "a transient failure does not advance the accepted watermark"
        );
        assert_eq!(
            super::status_slot_pending(&current, Some(&accepted), None, now),
            None,
            "an accepted slot is not resent before its assertion interval"
        );

        let suppressed = super::RejectedStatusSlot {
            version: current.version(),
            changed: true,
            retry_at: now + Duration::from_secs(5),
        };
        assert_eq!(
            super::status_slot_pending(&current, Some(&accepted), Some(&suppressed), now),
            None,
            "a permanent rejection is bounded rather than retried every tick"
        );
        assert_eq!(
            super::status_slot_pending(
                &current,
                Some(&accepted),
                Some(&super::RejectedStatusSlot {
                    retry_at: now - Duration::from_secs(1),
                    ..suppressed.clone()
                }),
                now,
            ),
            Some(true),
            "the same rejected observation remains retryable"
        );

        let newer = slot(2);
        assert_eq!(
            super::status_slot_pending(&newer, Some(&accepted), Some(&suppressed), now),
            Some(true),
            "a newer observation replaces a rejected version immediately"
        );
    }

    /// Serves the runtime-events endpoint and records, per request, the kind
    /// and preview sequence of every event in it.
    async fn spawn_runtime_event_recorder(
        status: std::sync::Arc<std::sync::atomic::AtomicU16>,
    ) -> (
        std::net::SocketAddr,
        std::sync::Arc<std::sync::Mutex<Vec<Vec<(String, Option<u64>)>>>>,
        tokio::task::JoinHandle<()>,
    ) {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let requests = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let recorded = requests.clone();
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };
                let mut request = Vec::new();
                let header_end = loop {
                    let mut buffer = [0u8; 4096];
                    let read = socket.read(&mut buffer).await.unwrap_or(0);
                    if read == 0 {
                        break None;
                    }
                    request.extend_from_slice(&buffer[..read]);
                    if let Some(at) = request.windows(4).position(|window| window == b"\r\n\r\n") {
                        break Some(at + 4);
                    }
                };
                let Some(header_end) = header_end else {
                    continue;
                };
                let length = String::from_utf8_lossy(&request[..header_end])
                    .lines()
                    .find(|line| line.to_ascii_lowercase().starts_with("content-length:"))
                    .and_then(|line| line.split(':').nth(1))
                    .and_then(|value| value.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                while request.len() < header_end + length {
                    let mut buffer = [0u8; 4096];
                    let read = socket.read(&mut buffer).await.unwrap_or(0);
                    if read == 0 {
                        break;
                    }
                    request.extend_from_slice(&buffer[..read]);
                }
                let body: serde_json::Value =
                    serde_json::from_slice(&request[header_end..]).unwrap_or_default();
                recorded.lock().unwrap().push(
                    body["events"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .map(|event| {
                            (
                                event["kind"].as_str().unwrap_or_default().to_string(),
                                event["payload"]["seq"].as_u64(),
                            )
                        })
                        .collect(),
                );
                let status = status.load(std::sync::atomic::Ordering::SeqCst);
                let response =
                    format!("HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                let _ = socket.write_all(response.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });
        (addr, requests, handle)
    }

    /// The OMP extension restates its phase every 20 seconds, and each
    /// restatement rewrites the session's slot: a new version and a new
    /// observation time, carrying the preview the last turn ended on. The phase
    /// is a fresh observation; the preview is not. Sending it again made the
    /// Runtime Host suppress an identical batch three times a minute for as long
    /// as the session sat idle. The host is told a preview once, and told again
    /// the moment it changes.
    #[tokio::test(flavor = "current_thread")]
    async fn a_phase_restatement_does_not_resend_the_preview_the_host_holds() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;
        use std::sync::atomic::{AtomicU16, Ordering};
        use std::sync::Arc;
        use std::time::{Duration, Instant};

        let status = Arc::new(AtomicU16::new(204));
        let (addr, requests, server) = spawn_runtime_event_recorder(status.clone()).await;
        let client = ShipperClient::with_compression(
            &ShipperConfig::default().with_overrides(
                Some(&format!("http://{addr}")),
                None,
                None,
                None,
                None,
                None,
            ),
            CompressionAlgo::Gzip,
        )
        .unwrap();
        let slot = |seq: u64, observed_at: &str, preview_seq: u64| crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: "session-1".into(),
            provider: "omp".into(),
            runtime_key: "omp:session-1".into(),
            run_id: "run-1".into(),
            source: "omp_helm_channel".into(),
            phase: "idle".into(),
            tool_name: None,
            observed_at: observed_at.into(),
            payload: serde_json::json!({}),
            preview: Some(crate::status_slot::StatusPreview {
                turn_id: "turn-1".into(),
                seq: preview_seq,
                live_text: "the answer".into(),
                turn_completed: false,
                progress_kind: "omp_helm_stream".into(),
                provider_session_id: None,
            }),
            producer_epoch: "epoch-1".into(),
            seq,
        };
        async fn deliver(
            ledger: &mut super::StatusLedger,
            client: &ShipperClient,
            slot: crate::status_slot::StatusSlot,
            now: Instant,
        ) {
            let pending = ledger.pending(vec![slot], now);
            assert_eq!(pending.len(), 1, "a new slot version is always pending");
            let result = super::post_status_slots(client, pending).await;
            ledger.settle(result, now);
        }
        let mut ledger = super::StatusLedger::default();
        let start = Instant::now();
        let at = |seconds: u64| start + Duration::from_secs(seconds);

        // A transient failure tells the host nothing, so the preview is still
        // owed on the next attempt.
        status.store(503, Ordering::SeqCst);
        deliver(
            &mut ledger,
            &client,
            slot(1, "2026-09-29T18:20:32Z", 58),
            at(0),
        )
        .await;
        status.store(204, Ordering::SeqCst);
        deliver(
            &mut ledger,
            &client,
            slot(2, "2026-09-29T18:20:32Z", 58),
            at(1),
        )
        .await;

        // The keepalive rewrites the slot: same preview, newer observation.
        deliver(
            &mut ledger,
            &client,
            slot(3, "2026-09-29T18:20:52Z", 58),
            at(21),
        )
        .await;
        deliver(
            &mut ledger,
            &client,
            slot(4, "2026-09-29T18:21:12Z", 58),
            at(41),
        )
        .await;

        // A real update carries a new sequence, and reaches the host at once.
        deliver(
            &mut ledger,
            &client,
            slot(5, "2026-09-29T18:21:13Z", 59),
            at(42),
        )
        .await;
        deliver(
            &mut ledger,
            &client,
            slot(6, "2026-09-29T18:21:33Z", 59),
            at(62),
        )
        .await;

        server.abort();
        let phase = |kind: &str| (kind.to_string(), None);
        let progress = |seq: u64| ("progress_signal".to_string(), Some(seq));
        let sent = requests.lock().unwrap().clone();
        assert_eq!(
            sent,
            vec![
                // The failed attempt, which the host never accepted.
                vec![phase("phase_signal"), progress(58)],
                // Retried with everything it still owes.
                vec![phase("phase_signal"), progress(58)],
                // Keepalives: the phase is a fresh observation, the preview is not.
                vec![phase("phase_signal")],
                vec![phase("phase_signal")],
                // A new sequence is a new statement.
                vec![phase("phase_signal"), progress(59)],
                vec![phase("phase_signal")],
            ]
        );
    }

    /// A slot whose timestamp the ledger cannot parse is skipped, not fatal.
    #[test]
    fn an_unparseable_slot_timestamp_does_not_stop_the_others() {
        use std::collections::HashMap;

        let temp = tempfile::tempdir().expect("tempdir");
        let db_path = temp.path().join("agent.db");
        crate::state::db::open_db(Some(&db_path)).expect("bootstrap ledger schema");
        let mut broken = crate::status_slot::StatusSlot {
            schema: crate::status_slot::STATUS_SLOT_SCHEMA,
            session_id: "broken".into(),
            provider: "omp".into(),
            runtime_key: "omp:broken".into(),
            run_id: "run-1".into(),
            source: "omp_helm_channel".into(),
            phase: "thinking".into(),
            tool_name: None,
            observed_at: "not-a-timestamp".into(),
            payload: serde_json::json!({}),
            preview: None,
            producer_epoch: "epoch-1".into(),
            seq: 1,
        };
        let mut healthy = broken.clone();
        healthy.session_id = "healthy".into();
        healthy.observed_at = "2026-09-17T15:00:01Z".into();
        broken.observed_at = "not-a-timestamp".into();

        let recorded =
            super::record_status_slot_phases(Some(&db_path), &[broken, healthy], &HashMap::new());

        assert_eq!(recorded.len(), 1);
        assert_eq!(recorded[0].0, "healthy");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn blocking_path_work_does_not_delay_live_dispatch() {
        tokio::task::LocalSet::new()
            .run_until(async {
                let (_shutdown, receiver) = tokio::sync::watch::channel(false);
                let mut tasks = tokio::task::JoinSet::new();
                let (started, ready) = tokio::sync::oneshot::channel();
                let began = std::time::Instant::now();
                super::spawn_path_worker(&mut tasks, receiver, move || async move {
                    let non_send = std::rc::Rc::new(7);
                    started.send(()).unwrap();
                    std::thread::sleep(std::time::Duration::from_millis(750));
                    tokio::task::yield_now().await;
                    *non_send
                });
                ready.await.unwrap();
                tokio::time::sleep(std::time::Duration::from_millis(20)).await;
                assert!(
                    began.elapsed() < std::time::Duration::from_millis(500),
                    "archive preparation blocked the live dispatch loop"
                );
                assert_eq!(tasks.join_next().await.unwrap().unwrap(), Some(7));
            })
            .await;
    }

    #[tokio::test(flavor = "current_thread")]
    async fn dropping_path_worker_owner_cancels_pending_network_work() {
        let (shutdown, receiver) = tokio::sync::watch::channel(false);
        let owner = super::PathWorkersShutdown(shutdown);
        let mut tasks = tokio::task::JoinSet::new();
        let (started, ready) = tokio::sync::oneshot::channel();
        super::spawn_path_worker(&mut tasks, receiver, move || async move {
            started.send(()).unwrap();
            std::future::pending::<usize>().await
        });
        ready.await.unwrap();
        drop(owner);
        let result = tokio::time::timeout(std::time::Duration::from_secs(1), tasks.join_next())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        assert_eq!(result, None);
    }

    #[test]
    fn ledger_watermark_detects_out_of_process_phase_writes() {
        // The Codex bridge and Console adapters write session_phase_state from
        // their own processes. Nothing reaches the daemon's outbox, so the
        // watermark is the only way it learns a phase moved.
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE session_phase_state (
                session_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                phase TEXT NOT NULL,
                tool_name TEXT,
                source TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0
            );",
        )
        .unwrap();
        assert_eq!(
            latest_phase_watermark(&conn),
            None,
            "empty ledger has no watermark"
        );

        conn.execute(
            "INSERT INTO session_phase_state VALUES ('s1','codex','thinking',NULL,'codex_bridge','2026-08-01T13:10:00+00:00',1)",
            [],
        )
        .unwrap();
        let first = latest_phase_watermark(&conn).unwrap();

        // A different session transitions later; the watermark must advance.
        conn.execute(
            "INSERT INTO session_phase_state VALUES ('s2','codex','idle',NULL,'codex_bridge','2026-08-01T13:11:00+00:00',2)",
            [],
        )
        .unwrap();
        let second = latest_phase_watermark(&conn).unwrap();
        assert_ne!(
            first, second,
            "a later phase from any session must move the watermark"
        );

        // Re-reading without a write must not re-arm the debounce.
        assert_eq!(latest_phase_watermark(&conn).unwrap(), second);
    }

    #[test]
    fn ledger_watermark_moves_for_equal_and_out_of_order_accepted_updates() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = crate::state::session_phase::SessionPhaseStore::new(&conn);

        let signal = |session_id: &str, observed_at: &str, phase: &str| {
            crate::state::session_phase::SessionPhaseSignal {
                session_id: session_id.to_string(),
                provider: "codex".to_string(),
                phase: phase.to_string(),
                tool_name: None,
                source: "codex_bridge".to_string(),
                observed_at: chrono::DateTime::parse_from_rfc3339(observed_at)
                    .unwrap()
                    .with_timezone(&chrono::Utc),
                run_id: None,
            }
        };

        assert!(store
            .record(&signal("s1", "2026-08-01T13:10:00Z", "thinking"))
            .unwrap());
        let first = latest_phase_watermark(&conn).unwrap();
        assert!(store
            .record(&signal("s1", "2026-08-01T13:10:00+00:00", "running"))
            .unwrap());
        let equal_timestamp = latest_phase_watermark(&conn).unwrap();
        assert!(equal_timestamp > first);
        assert!(store
            .record(&signal("s2", "2026-08-01T13:09:59Z", "idle"))
            .unwrap());
        let out_of_order = latest_phase_watermark(&conn).unwrap();
        assert!(out_of_order > equal_timestamp);
        assert!(!store
            .record(&signal("s1", "2026-08-01T13:09:58Z", "stale"))
            .unwrap());
        assert_eq!(latest_phase_watermark(&conn), Some(out_of_order));
    }

    #[test]
    fn phase_writes_coalesce_into_one_debounce_window() {
        // A tool-heavy turn emits many phases. They must produce one projection
        // rebuild, and the window must not slide, or a busy turn never rebuilds.
        let mut pending = false;
        assert!(
            arm_phase_projection(&mut pending),
            "first write opens the window"
        );
        for _ in 0..50 {
            assert!(
                !arm_phase_projection(&mut pending),
                "writes inside an open window must coalesce, not re-arm"
            );
        }
        // Timer fires and the loop clears the flag.
        pending = false;
        assert!(
            arm_phase_projection(&mut pending),
            "the next write after a rebuild opens a fresh window"
        );
    }
    use super::*;

    #[tokio::test]
    async fn startup_inventory_discovers_sources_without_enqueuing_import_work() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("session.jsonl"), b"history").unwrap();
        let providers = vec![ProviderConfig {
            name: "claude",
            root: temp.path().to_path_buf(),
            extension: "jsonl",
        }];
        let mut tasks = JoinSet::new();

        start_inventory_task(&mut tasks, &providers);
        let result = tasks.join_next().await.unwrap().unwrap();

        assert!(!result.enqueue_files);
        assert!(result.files.is_empty());
        assert_eq!(result.inventory.source_count, 1);
        assert_eq!(result.reason, "startup inventory");
    }

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
    fn full_reconciliation_cooldown_coalesces_inflight_burst() {
        let now = Instant::now();
        let not_before = now + std::time::Duration::from_secs(5);
        assert!(!managed_full_reconciliation_ready(
            true, true, now, not_before,
        ));
        assert!(!managed_full_reconciliation_ready(
            true, false, not_before, not_before,
        ));
        assert!(managed_full_reconciliation_ready(
            true, true, not_before, not_before,
        ));
        assert!(!managed_full_reconciliation_ready(
            false, true, not_before, not_before,
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
    fn current_only_retains_dead_cursor_run_for_terminal_evidence() {
        let dead = managed_cursor_helm_scan::CursorHelmObservation {
            session_id: "dead-cursor".to_string(),
            provider_session_id: Some("cursor-thread".to_string()),
            run_id: Some("run-dead-cursor".to_string()),
            connection_id: None,
            lease_generation: None,
            state_file: PathBuf::from("/tmp/dead-cursor.json"),
            socket_path: Some(PathBuf::from("/tmp/dead-cursor.sock")),
            cwd: Some("/tmp/project".to_string()),
            launcher_pid: Some(1234),
            launcher_process_start_time: Some("Tue Jul  8 22:50:19 2026".to_string()),
            cursor_pid: Some(1235),
            cursor_process_start_time: Some("Tue Jul  8 22:50:20 2026".to_string()),
            started_at: "2026-07-08T22:50:19Z".to_string(),
            updated_at: "2026-07-08T22:50:19Z".to_string(),
            launcher_alive: false,
            live: false,
        };
        let snapshot = ManagedObservationSnapshot {
            cursor: vec![dead.clone()],
            ..ManagedObservationSnapshot::default()
        };

        let retained = snapshot.current_only();
        assert_eq!(retained.cursor, vec![dead]);

        let mut no_run = snapshot.cursor[0].clone();
        no_run.run_id = None;
        let without_run = ManagedObservationSnapshot {
            cursor: vec![no_run],
            ..ManagedObservationSnapshot::default()
        };
        assert!(without_run.current_only().cursor.is_empty());
    }

    fn dead_codex_row(session_id: &str) -> managed_bridge_scan::CodexBridgeObservation {
        managed_bridge_scan::CodexBridgeObservation {
            session_id: session_id.to_string(),
            run_id: Some(format!("run-{session_id}")),
            connection_id: Some(format!("connection-{session_id}")),
            lease_generation: Some(format!("generation-{session_id}")),
            state_file: PathBuf::from(format!("/tmp/{session_id}.json")),
            schema_version: 2,
            cwd: Some("/tmp/project".to_string()),
            launch_mode: Some("tui".to_string()),
            ws_url: Some(format!("ws://127.0.0.1/{session_id}")),
            status: "ready".to_string(),
            thread_id: Some("thread".to_string()),
            thread_path: None,
            active_turn_id: None,
            last_turn_status: None,
            last_error: None,
            thread_subscription_status: None,
            stopped_at: None,
            terminal_state: None,
            terminal_reason: None,
            bridge_pid: 1234,
            bridge_process_start_time: Some("Tue Jul  8 22:50:19 2026".to_string()),
            app_server_pid: Some(1235),
            app_server_process_start_time: Some("Tue Jul  8 22:50:20 2026".to_string()),
            app_server_pgid: None,
            updated_at: "2026-07-08T22:50:19Z".to_string(),
            bridge_alive: false,
            has_tui_attachment: false,
            app_server_alive: false,
        }
    }

    fn dead_opencode_row(session_id: &str) -> managed_opencode_scan::OpenCodeServerObservation {
        managed_opencode_scan::OpenCodeServerObservation {
            session_id: session_id.to_string(),
            run_id: Some(format!("run-{session_id}")),
            connection_id: Some(format!("connection-{session_id}")),
            lease_generation: Some(format!("generation-{session_id}")),
            provider_session_id: "opencode-thread".to_string(),
            state_file: PathBuf::from(format!("/tmp/{session_id}.json")),
            cwd: Some("/tmp/project".to_string()),
            server_url: Some("http://127.0.0.1:4096".to_string()),
            pid: Some(2468),
            started_at: "2026-07-08T22:50:19Z".to_string(),
            updated_at: "2026-07-08T22:50:19Z".to_string(),
            server_alive: false,
            health_ready: false,
            has_tui_attachment: false,
            launch_mode: "attached_tui".to_string(),
            owner_wrapper_pid: Some(2469),
            owner_wrapper_start_time: "Tue Jul  8 22:50:19 2026".to_string(),
            process_start_time: "Tue Jul  8 22:50:20 2026".to_string(),
        }
    }

    fn scan_result_fixture(
        enumeration_complete: bool,
        retained: usize,
    ) -> ManagedObservationScanResult {
        ManagedObservationScanResult {
            enumeration_complete,
            retained_stale_rows: retained,
            ..ManagedObservationScanResult::default()
        }
    }

    #[test]
    fn certificate_keepalive_refreshes_before_the_host_would_refuse_it() {
        let now = Instant::now();
        // Never certified: the first beat must go and get one.
        assert!(certificate_needs_refresh(None, now));
        // Fresh: do not rescan on every beat.
        assert!(!certificate_needs_refresh(Some(now), now));
        // Aged past the keepalive: re-enumerate, or the next beats ship a claim
        // the Runtime Host will refuse.
        let stale = now - Duration::from_secs(MANAGED_CERTIFICATE_KEEPALIVE_SECS + 1);
        assert!(certificate_needs_refresh(Some(stale), now));
        // Inside the host's own bound, so the refreshed claim is still true on
        // arrival.
        assert!(MANAGED_CERTIFICATE_KEEPALIVE_SECS < 90);
    }

    #[test]
    fn managed_scan_certifies_every_pass_that_actually_enumerated() {
        // The certificate describes this generation's enumeration, so a pass
        // that is not a "full reconciliation" may still certify -- that is the
        // whole point of enumerating every pass.
        let mut incremental = scan_result_fixture(true, 0);
        incremental.full_reconciliation = false;
        assert_eq!(managed_scan_certificate(&incremental), (false, true));

        // An entry carried forward unaccounted for is a failure to enumerate.
        let partial = scan_result_fixture(false, 1);
        assert_eq!(managed_scan_certificate(&partial), (true, false));

        // An unresolved provider directory is the same failure, even with no
        // retained rows: "no sessions" and "no directory" are different claims.
        let unresolved = scan_result_fixture(false, 0);
        assert_eq!(managed_scan_certificate(&unresolved), (false, false));
    }

    #[test]
    fn current_only_retains_dead_provider_run_for_terminal_evidence() {
        // A run whose owner died abruptly can only be closed by the exact
        // pid/start-time evidence in its observation, so the projected snapshot
        // must keep that row once it still names a run. A dead row that names no
        // run is still dropped: it can close nothing and only adds noise.
        let dead_claude = managed_claude_scan::ClaudeChannelObservation {
            session_id: "dead-claude".to_string(),
            run_id: Some("run-dead-claude".to_string()),
            connection_id: Some("connection-dead-claude".to_string()),
            lease_generation: Some("generation-dead-claude".to_string()),
            provider_session_id: Some("claude-thread".to_string()),
            state_file: PathBuf::from("/tmp/dead-claude.json"),
            cwd: Some("/tmp/project".to_string()),
            claude_pid: Some(4321),
            bridge_pid: Some(4322),
            ready: true,
            started_at: "2026-09-24T14:27:32Z".to_string(),
            updated_at: "2026-09-24T14:27:32Z".to_string(),
            claude_alive: false,
            bridge_alive: false,
            claude_foreground_tui: false,
        };
        let dead_codex = dead_codex_row("dead-codex");
        let dead_opencode = dead_opencode_row("dead-opencode");
        let snapshot = ManagedObservationSnapshot {
            codex: vec![dead_codex.clone()],
            claude: vec![dead_claude.clone()],
            opencode: vec![dead_opencode.clone()],
            ..ManagedObservationSnapshot::default()
        };

        let without_runs = ManagedObservationSnapshot {
            codex: vec![managed_bridge_scan::CodexBridgeObservation {
                run_id: None,
                ..dead_codex.clone()
            }],
            claude: vec![managed_claude_scan::ClaudeChannelObservation {
                run_id: None,
                ..dead_claude.clone()
            }],
            opencode: vec![managed_opencode_scan::OpenCodeServerObservation {
                run_id: None,
                ..dead_opencode.clone()
            }],
            ..ManagedObservationSnapshot::default()
        };

        let retained = snapshot.current_only();
        assert_eq!(retained.claude, vec![dead_claude]);
        assert_eq!(retained.codex, vec![dead_codex]);
        assert_eq!(retained.opencode, vec![dead_opencode]);

        let dropped = without_runs.current_only();
        assert!(dropped.claude.is_empty());
        assert!(dropped.codex.is_empty());
        assert!(dropped.opencode.is_empty());
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
            shipping_progress: heartbeat::ShippingProgress::default(),
            archive_backlog: crate::state::archive_backlog::ArchiveBacklogSnapshot::default(),
            storage_v2_outbox:
                crate::state::pending_source_envelope::StorageV2OutboxSnapshot::default(),
            runtime_event_outbox: heartbeat::RuntimeEventOutboxSnapshot::default(),
            parse_error_count_1h: 0,
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
            local_database_bytes: None,
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
            history_import: Default::default(),
            update: None,
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
            run_id: None,
            connection_id: None,
            lease_generation: None,
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
            stopped_at: None,
            terminal_state: None,
            terminal_reason: None,
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
    fn codex_contract_retention_includes_stopped_resumable_threads() {
        let temp = tempfile::tempdir().unwrap();
        let rollout = temp.path().join("rollout.jsonl");
        std::fs::write(&rollout, b"{}\n").unwrap();
        let mut observation = codex_bridge_observation(
            &rollout,
            None,
            Some("completed"),
            "2026-08-02T00:00:00Z",
            false,
        );
        observation.status = "stopped".to_string();
        observation.stopped_at = Some("2026-08-02T00:00:01Z".to_string());
        observation.terminal_state = Some("session_ended".to_string());
        observation.terminal_reason = Some("user_closed".to_string());
        assert!(codex_contract_must_be_retained(&observation));

        let contract_dir = temp.path().join("contracts");
        std::fs::create_dir(&contract_dir).unwrap();
        let contract_path = contract_dir.join(format!("{}.json", observation.session_id));
        std::fs::write(&contract_path, b"{}").unwrap();
        let retained = HashSet::from([observation.session_id.clone()]);
        assert_eq!(
            crate::managed_contract_janitor::sweep_orphan_contracts(
                &contract_dir,
                &retained,
                std::time::SystemTime::now() + Duration::from_secs(7200),
            ),
            0
        );
        assert!(contract_path.is_file());

        std::fs::remove_file(&rollout).unwrap();
        assert!(!codex_contract_must_be_retained(&observation));
        assert_eq!(
            crate::managed_contract_janitor::sweep_orphan_contracts(
                &contract_dir,
                &HashSet::new(),
                std::time::SystemTime::now() + Duration::from_secs(7200),
            ),
            1
        );
        assert!(!contract_path.exists());

        std::fs::write(&rollout, b"{}\n").unwrap();
        observation.terminal_reason = Some("unknown".to_string());
        assert!(!codex_contract_must_be_retained(&observation));

        observation.bridge_alive = true;
        assert!(codex_contract_must_be_retained(&observation));
    }

    #[test]
    fn test_build_local_status_projection_uses_cached_unmanaged_bindings() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();
        let cached = vec![unmanaged_binding("sess-cached", 42)];
        let mut session_snapshot_state = SessionSnapshotState::default();

        let projection = build_local_status_projection(
            &conn,
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
            &[],
            &cached,
            true,
            "2026-09-25T12:00:00Z",
            false,
            None,
            None,
            ArchiveRepairMode::Drain,
            &mut session_snapshot_state,
        );

        assert_eq!(projection.payload.unmanaged_session_bindings, cached);
        assert!(projection.payload.sessions.is_empty());
        let scopes = projection
            .payload
            .machine_evidence
            .as_ref()
            .unwrap()
            .process_snapshot_scopes
            .iter()
            .map(|scope| (scope.scope.as_str(), scope.complete))
            .collect::<HashMap<_, _>>();
        assert_eq!(scopes.get("managed_state_files"), Some(&true));
        assert_eq!(scopes.get("unmanaged_provider_processes"), Some(&false));
    }

    #[test]
    fn truth_change_heartbeat_coalescing_caps_immediate_sends_at_one_per_second() {
        let first_send = Instant::now();
        let burst_due = truth_heartbeat_due_at(None, first_send);
        assert_eq!(burst_due, first_send);

        let second_change = first_send + Duration::from_millis(100);
        let third_change = first_send + Duration::from_millis(900);
        let second_due = truth_heartbeat_due_at(Some(first_send), second_change);
        let third_due = truth_heartbeat_due_at(Some(first_send), third_change);
        assert_eq!(second_due, first_send + Duration::from_secs(1));
        assert_eq!(third_due, second_due);

        let next_change = first_send + Duration::from_millis(1_100);
        assert_eq!(
            truth_heartbeat_due_at(Some(second_due), next_change),
            first_send + Duration::from_secs(2)
        );
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
        first.machine_evidence = Some(heartbeat::MachineEvidence {
            schema_version: 3,
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            identities: Vec::new(),
            candidate_identities: std::sync::Arc::<[heartbeat::EvidenceIdentity]>::from(Vec::new()),
            run: Vec::new(),
            process: Vec::new(),
            activity: vec![heartbeat::ActivityEvidence {
                authority_class: "provider_runtime".to_string(),
                provider: "claude".to_string(),
                session_id: "sess-1".to_string(),
                run_id: Some("run-1".to_string()),
                kind: "running".to_string(),
                raw_kind: "running".to_string(),
                tool_name: Some("shell".to_string()),
                detail: None,
                source: "hook".to_string(),
                observed_at: "2026-05-05T12:00:00Z".to_string(),
                valid_until: "2026-05-05T12:00:30Z".to_string(),
                raw_locator: None,
                reason_codes: Vec::new(),
            }],
            control: Vec::new(),
            transcript: Vec::new(),
            process_snapshot_scopes: Vec::new(),
            readiness: Vec::new(),
            continuation: Vec::new(),
        });
        let mut second = first.clone();
        second.sessions[0].phase_observed_at = Some("2026-05-05T12:00:10Z".to_string());
        second.sessions[0].last_activity_at = Some("2026-05-05T12:00:11Z".to_string());
        second.sessions[0].evidence.hook_seen_at = Some("2026-05-05T12:00:11Z".to_string());
        let second_evidence = second.machine_evidence.as_mut().unwrap();
        second_evidence.observed_at = "2026-05-05T12:00:05Z".to_string();
        second_evidence.activity[0].observed_at = "2026-05-05T12:00:05Z".to_string();
        second_evidence.activity[0].valid_until = "2026-05-05T12:00:35Z".to_string();

        assert_eq!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
        let mut tool_changed = first.clone();
        tool_changed.sessions[0].tool_name = Some("shell".to_string());
        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&tool_changed)
        );

        let mut phase_changed = first.clone();
        phase_changed.sessions[0].phase = Some("working".to_string());
        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&phase_changed)
        );

        let mut activity_changed = first.clone();
        activity_changed.machine_evidence.as_mut().unwrap().activity[0].kind =
            "needs_user".to_string();
        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&activity_changed)
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

        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&second)
        );
    }

    #[test]
    fn test_runtime_truth_signature_tracks_stable_continuation_identity() {
        let mut first = empty_heartbeat_payload();
        first.machine_evidence = Some(heartbeat::MachineEvidence {
            schema_version: 3,
            observed_at: "2026-08-03T12:00:00Z".to_string(),
            identities: Vec::new(),
            candidate_identities: std::sync::Arc::<[heartbeat::EvidenceIdentity]>::from(Vec::new()),
            run: Vec::new(),
            process: Vec::new(),
            activity: Vec::new(),
            control: Vec::new(),
            transcript: Vec::new(),
            process_snapshot_scopes: Vec::new(),
            readiness: Vec::new(),
            continuation: vec![heartbeat::ContinuationEvidence {
                authority_class: "managed".to_string(),
                provider: "opencode".to_string(),
                session_id: "session-1".to_string(),
                provider_session_id: Some("ses_1".to_string()),
                cwd: Some("/tmp/workspace".to_string()),
                contract_state: "valid".to_string(),
                unavailable_reason: None,
                observed_at: "2026-08-03T12:00:00Z".to_string(),
                valid_until: "2026-08-03T12:20:00Z".to_string(),
                source: "managed_resume_contract_scan".to_string(),
                raw_locator: "opencode/session-1".to_string(),
            }],
        });
        let mut timestamp_only = first.clone();
        timestamp_only
            .machine_evidence
            .as_mut()
            .unwrap()
            .observed_at = "2026-08-03T12:00:05Z".to_string();
        timestamp_only
            .machine_evidence
            .as_mut()
            .unwrap()
            .continuation[0]
            .observed_at = "2026-08-03T12:00:05Z".to_string();
        timestamp_only
            .machine_evidence
            .as_mut()
            .unwrap()
            .continuation[0]
            .valid_until = "2026-08-03T12:20:05Z".to_string();
        assert_eq!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&timestamp_only)
        );

        let mut changed = first.clone();
        changed.machine_evidence.as_mut().unwrap().continuation[0].provider_session_id =
            Some("ses_2".to_string());
        assert_ne!(
            runtime_truth_signature(&first),
            runtime_truth_signature(&changed)
        );
    }

    #[test]
    fn test_initial_runtime_truth_requires_immediate_heartbeat() {
        assert!(runtime_truth_changed(None, "first-snapshot"));
        assert!(!runtime_truth_changed(
            Some("first-snapshot"),
            "first-snapshot"
        ));
    }

    #[test]
    fn test_startup_inventory_waits_for_managed_projection_pair() {
        assert!(!inventory_change_requires_projection(None, 1, false));
        assert!(inventory_change_requires_projection(None, 1, true));
        assert!(!inventory_change_requires_projection(Some(1), 1, true));
        assert!(inventory_change_requires_projection(Some(1), 2, true));
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
    fn test_progress_wake_ships_for_providers_without_a_completion_lane() {
        // OMP and Pi never send turn_completed, so a progress wake is the only
        // managed evidence that new transcript content exists. Assert the
        // predicate rather than the full hint path: sibling tests in this
        // binary mutate process-wide HOME, and the hint path canonicalizes
        // through the provider root.
        for provider in ["omp", "pi"] {
            for reason in ["binding", "phase", "progress"] {
                assert!(
                    transcript_wake_ships(provider, Some(reason)),
                    "{provider} {reason} wake must schedule a ship"
                );
            }
        }
        // A completion wake still ships for every provider.
        assert!(transcript_wake_ships("codex", Some("turn_completed")));
        // Providers with a completion lane keep coalescing behind it.
        assert!(!transcript_wake_ships("codex", Some("progress")));
        assert!(!transcript_wake_ships("claude", Some("phase")));
        assert!(!transcript_wake_ships("codex", None));
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
    fn antigravity_mirror_completion_wake_uses_the_discovered_source() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir
            .path()
            .join("brain/conversation/.system_generated/logs/transcript_full.jsonl");
        std::fs::create_dir_all(transcript.parent().unwrap()).unwrap();
        std::fs::write(&transcript, b"canonical snapshot\n").unwrap();
        let mirror = transcript.with_file_name("transcript.jsonl");
        std::fs::write(&mirror, b"truncated summary has different bytes\n").unwrap();
        let mut latest_wakes = HashMap::new();
        let wake = |path: PathBuf, observed_at_ms| TranscriptWakeSignal {
            provider: "antigravity".to_string(),
            path,
            phase: "idle".to_string(),
            observed_at_ms,
            session_id: Some("managed-session".to_string()),
            turn_id: None,
            wake_reason: Some("turn_completed".to_string()),
            file_len_hint: Some(45),
            received_at_ms: Some(observed_at_ms),
        };
        let scheduled = record_transcript_wake_hint(&mut latest_wakes, wake(mirror.clone(), 123))
            .expect("the provider mirror hint must wake the canonical source");
        assert_eq!(scheduled.0, transcript);
        assert_eq!(scheduled.2.file_len_hint, Some(19));
        assert_eq!(scheduled.2.session_id.as_deref(), Some("managed-session"));
        assert!(
            record_transcript_wake_hint(&mut latest_wakes, wake(transcript.clone(), 123)).is_none()
        );

        // A missing canonical source must not enroll the still-present mirror.
        std::fs::remove_file(&transcript).unwrap();
        assert!(record_transcript_wake_hint(&mut latest_wakes, wake(mirror, 124)).is_none());
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
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let mut latest_wakes = HashMap::new();
        let mut scheduler = PathScheduler::new(4);

        assert!(enqueue_transcript_wake_signal(
            &conn,
            &mut scheduler,
            &mut latest_wakes,
            &mut HashMap::new(),
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
    fn test_cursor_agent_transcript_wake_persists_managed_session_binding() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir
            .path()
            .join("projects/workspace/agent-transcripts/conversation.jsonl");
        std::fs::create_dir_all(transcript.parent().unwrap()).unwrap();
        std::fs::write(&transcript, b"{}\n").unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let observation = ObservationTrace {
            source: "wake_socket",
            observed_at_ms: 123,
            latest_observed_at_ms: None,
            wake_received_at_ms: Some(124),
            enqueued_at_ms: 125,
            session_id: Some("managed-session".to_string()),
            turn_id: Some("turn-1".to_string()),
            wake_reason: Some("turn_completed".to_string()),
            file_len_hint: Some(2),
        };

        persist_cursor_agent_transcript_binding(&conn, &transcript, "cursor", &observation);

        let canonical = std::fs::canonicalize(&transcript).unwrap();
        assert_eq!(
            crate::state::session_binding::SessionBinding::new(&conn)
                .get_for_provider(&canonical.to_string_lossy(), "cursor")
                .unwrap()
                .as_deref(),
            Some("managed-session")
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
            &event,
            "codex"
        ));
        assert!(!should_defer_fsevent_for_managed_wake(
            &latest_wakes,
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
            &event,
            "codex"
        ));
    }

    #[test]
    fn test_codex_fsevent_ships_without_prior_wake() {
        let path = PathBuf::from("/tmp/managed-codex.jsonl");
        let now = now_ms();
        let event = WatcherEvent {
            path: path.clone(),
            observed_at_ms: now,
            latest_observed_at_ms: now,
        };

        assert!(!should_defer_fsevent_for_managed_wake(
            &HashMap::new(),
            &event,
            "codex"
        ));
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
                .get_for_provider(&canonical, "claude")
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
    fn test_storage_v2_pending_retry_is_queued_immediately() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(db.path())).unwrap();
        let epoch = uuid::Uuid::new_v4();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, 'codex', 'source-a', 'fixture', 'initial', 10, ?2, ?2)",
            rusqlite::params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        let pending = crate::state::pending_source_envelope::PendingSourceEnvelope::new(
            epoch,
            transcript.path().to_string_lossy().to_string(),
            0,
            10,
            "a".repeat(64),
            vec![1],
            vec![2],
            10,
            1,
            true,
            false,
        );
        crate::state::pending_source_envelope::persist_or_load(&mut conn, &pending).unwrap();

        let mut scheduler = PathScheduler::new(4);
        assert_eq!(
            queue_storage_v2_pending_retry_paths(
                &mut scheduler,
                &conn,
                ArchiveRepairMode::Drain,
                &mut HashMap::new(),
            )
            .unwrap(),
            1
        );
        assert_eq!(scheduler.snapshot().ready_retry_bytes, 10);
        let job = scheduler.pop_launchable().expect("storage-v2 retry queued");
        assert_eq!(job.path, transcript.path());
        assert_eq!(job.provider, "codex");
        assert_eq!(job.priority, WorkPriority::Retry);
        assert_eq!(
            job.observation.source,
            STORAGE_V2_PENDING_RETRY_OBSERVATION_SOURCE
        );
    }

    #[test]
    fn test_storage_v2_pending_retry_resumes_without_process_restart() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
                let mut conn = open_db(Some(db.path())).unwrap();
                let epoch = uuid::Uuid::new_v4();
                conn.execute(
                    "INSERT INTO source_epoch_registry (
                         source_epoch, provider, opaque_source_id, file_incarnation,
                         start_reason, max_observed_len, created_at, updated_at
                     ) VALUES (?1, 'codex', 'source-a', 'fixture', 'initial', 10, ?2, ?2)",
                    rusqlite::params![epoch.to_string(), "2026-07-15T00:00:00Z"],
                )
                .unwrap();
                let pending = crate::state::pending_source_envelope::PendingSourceEnvelope::new(
                    epoch,
                    transcript.path().to_string_lossy().to_string(),
                    0,
                    10,
                    "a".repeat(64),
                    vec![1],
                    vec![2],
                    10,
                    1,
                    true,
                    false,
                );
                crate::state::pending_source_envelope::persist_or_load(&mut conn, &pending)
                    .unwrap();

                let control_path = config::get_agent_archive_repair_control_path().unwrap();
                std::fs::create_dir_all(control_path.parent().unwrap()).unwrap();
                std::fs::write(
                    &control_path,
                    serde_json::to_vec(&json!({"mode": "paused"})).unwrap(),
                )
                .unwrap();

                let mut scheduler = PathScheduler::new(4);
                assert_eq!(
                    queue_storage_v2_pending_retry_paths(
                        &mut scheduler,
                        &conn,
                        ArchiveRepairMode::Drain,
                        &mut HashMap::new(),
                    )
                    .unwrap(),
                    0
                );
                assert!(scheduler.pop_launchable().is_none());

                let expires_at = (chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339();
                std::fs::write(
                    &control_path,
                    serde_json::to_vec(&json!({
                        "mode": "trickle",
                        "expires_at": expires_at
                    }))
                    .unwrap(),
                )
                .unwrap();

                assert_eq!(
                    queue_storage_v2_pending_retry_paths(
                        &mut scheduler,
                        &conn,
                        ArchiveRepairMode::Drain,
                        &mut HashMap::new(),
                    )
                    .unwrap(),
                    1
                );
                let job = scheduler
                    .pop_launchable()
                    .expect("storage-v2 retry queued after resume");
                assert_eq!(job.path, transcript.path());
                assert_eq!(
                    job.observation.source,
                    STORAGE_V2_PENDING_RETRY_OBSERVATION_SOURCE
                );
            },
        );
    }

    #[test]
    fn test_reconciliation_discovery_queues_reconciliation_source() {
        let mut scheduler = PathScheduler::new(4);
        let path = PathBuf::from("/tmp/reconciliation-session.jsonl");

        let queued = enqueue_discovered_files(
            &mut scheduler,
            vec![discovery::DiscoveredFile {
                path: path.clone(),
                provider: "claude",
                modified_at_ms: now_ms(),
            }],
            WorkPriority::Scan,
            &mut HashMap::new(),
        );

        assert_eq!(queued, 1);
        let job = scheduler.pop_launchable().expect("scan job queued");
        assert_eq!(job.path, path);
        assert_eq!(job.priority, WorkPriority::Scan);
        assert_eq!(job.observation.source, "reconciliation_scan");
        assert_eq!(work_context(job.priority), "reconciliation_scan");
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
            ..Default::default()
        };
        assert_eq!(
            operator_control.normalized_mode(ArchiveRepairMode::Paused),
            ArchiveRepairMode::Trickle
        );

        let invalid_control = ArchiveRepairControl {
            mode: Some("enabled".to_string()),
            expires_at: Some((chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339()),
            ..Default::default()
        };
        assert_eq!(
            invalid_control.normalized_mode(ArchiveRepairMode::Paused),
            ArchiveRepairMode::Paused
        );

        let expired_control = ArchiveRepairControl {
            mode: Some("drain".to_string()),
            expires_at: Some((chrono::Utc::now() - chrono::Duration::minutes(1)).to_rfc3339()),
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
        payload.storage_v2_outbox.pending_count = 1;
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
        assert!(!heartbeat::payload_has_pending_work(&payload));
    }

    #[test]
    fn test_archive_pause_keeps_already_queued_history_and_still_launches_live() {
        assert!(local_work_is_live_only(false, true));
        assert!(local_work_is_live_only(true, false));
        assert!(!local_work_is_live_only(false, false));

        let mut scheduler = PathScheduler::new(4);
        let history = PathBuf::from("/tmp/history.jsonl");
        let live = PathBuf::from("/tmp/live.jsonl");
        scheduler.enqueue(history.clone(), "codex", WorkPriority::Scan);
        assert!(!known_pending_local_work(&scheduler, &HashMap::new(), true));
        scheduler.enqueue(live.clone(), "codex", WorkPriority::Live);
        assert!(known_pending_local_work(&scheduler, &HashMap::new(), true));

        let launched = scheduler.pop_launchable_live().unwrap();
        assert_eq!(launched.path, live);
        assert!(scheduler.pop_launchable_live().is_none());
        let snapshot = scheduler.snapshot();
        assert_eq!(snapshot.ready_scan, 1);
        assert_eq!(snapshot.in_flight_scan, 0);
        assert_eq!(snapshot.in_flight_live, 1);

        scheduler.complete(&live, None);
        let resumed = scheduler.pop_launchable().unwrap();
        assert_eq!(resumed.path, history);
        assert_eq!(resumed.priority, WorkPriority::Scan);
    }

    #[test]
    fn test_sealed_current_ignores_verification_scan_but_not_live_or_retry_work() {
        let mut scheduler = PathScheduler::new(4);
        scheduler.enqueue(
            PathBuf::from("/tmp/verify.jsonl"),
            "codex",
            WorkPriority::Scan,
        );
        let snapshot = scheduler.snapshot();
        assert!(history_runtime_work_active(false, 1, Some(&snapshot)));
        assert!(!history_runtime_work_active(true, 1, Some(&snapshot)));

        scheduler.enqueue(
            PathBuf::from("/tmp/live.jsonl"),
            "codex",
            WorkPriority::Live,
        );
        assert!(history_runtime_work_active(
            true,
            0,
            Some(&scheduler.snapshot())
        ));

        let mut retry_scheduler = PathScheduler::new(4);
        retry_scheduler.enqueue(
            PathBuf::from("/tmp/retry.jsonl"),
            "codex",
            WorkPriority::Retry,
        );
        assert!(history_runtime_work_active(
            true,
            0,
            Some(&retry_scheduler.snapshot())
        ));
    }

    #[test]
    fn test_archive_pause_with_nothing_held_reads_complete() {
        let mut payload = empty_heartbeat_payload();
        let control = ArchiveRepairControl {
            mode: Some("paused".to_string()),
            actor: Some("menu_bar".to_string()),
            ..Default::default()
        };

        apply_archive_repair_control(&mut payload, &control, ArchiveRepairMode::Paused);

        assert_eq!(payload.archive_backlog.mode, "paused");
        assert_eq!(payload.archive_backlog.state, "complete");
        assert!(payload.archive_backlog.pause_actor.is_none());
    }

    #[test]
    fn test_archive_trickle_status_does_not_keep_stale_paused_state() {
        let mut payload = empty_heartbeat_payload();
        payload.archive_backlog.state = "paused".to_string();
        payload.storage_v2_outbox.pending_count = 2;
        let control = ArchiveRepairControl {
            mode: Some("trickle".to_string()),
            expires_at: Some((chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339()),
            ..Default::default()
        };

        apply_archive_repair_control(&mut payload, &control, ArchiveRepairMode::Paused);

        assert_eq!(payload.archive_backlog.mode, "trickle");
        assert_eq!(payload.archive_backlog.state, "complete");
    }

    #[tokio::test]
    async fn test_paused_mode_skips_reconciliation_scan_task() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
            storage_v2_backpressure_retry_delay(WorkPriority::Live, Duration::from_secs(5), false,),
            Duration::from_secs(1)
        );
        assert_eq!(
            storage_v2_backpressure_retry_delay(WorkPriority::Scan, Duration::from_secs(5), false,),
            Duration::from_secs(5)
        );
        assert_eq!(
            storage_v2_backpressure_retry_delay(WorkPriority::Live, Duration::from_secs(5), true,),
            Duration::from_secs(5)
        );
    }

    #[test]
    fn pending_storage_retry_respects_an_existing_local_backoff() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(db.path())).unwrap();
        let epoch = uuid::Uuid::new_v4();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, 'codex', 'source-a', 'fixture', 'initial', 10, ?2, ?2)",
            rusqlite::params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        let pending = crate::state::pending_source_envelope::PendingSourceEnvelope::new(
            epoch,
            transcript.path().to_string_lossy().to_string(),
            0,
            10,
            "a".repeat(64),
            vec![1],
            vec![2],
            10,
            1,
            true,
            false,
        );
        crate::state::pending_source_envelope::persist_or_load(&mut conn, &pending).unwrap();

        let path = transcript.path().to_path_buf();
        let mut deferred_retries = HashMap::from([(
            path.clone(),
            DeferredRetry {
                due_at: Instant::now() + Duration::from_secs(60),
                provider: "codex",
                priority: WorkPriority::Retry,
                observation: test_observation(),
            },
        )]);
        let mut scheduler = PathScheduler::new(4);

        assert_eq!(
            queue_storage_v2_pending_retry_paths(
                &mut scheduler,
                &conn,
                ArchiveRepairMode::Drain,
                &mut deferred_retries,
            )
            .unwrap(),
            0
        );
        assert!(scheduler.pop_launchable().is_none());
        assert!(deferred_retries.contains_key(&path));
    }

    // caffeinate is a macOS binary. These have never passed on Linux; they
    // simply never ran, because CI only runs the engine suite when engine/
    // changes and most commits do not touch it.
    #[cfg(target_os = "macos")]
    #[tokio::test]
    async fn test_spawn_caffeinate_uses_correct_args() {
        // Spawns by name and reads the process table: hold the shared agent-state
        // lock so a concurrent test cannot empty PATH under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let pid = std::process::id();
        let mut child = spawn_caffeinate(pid).expect("caffeinate should spawn");
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
        child.kill().await.expect("stop fixture caffeinate");
    }

    #[cfg(target_os = "macos")]
    #[tokio::test]
    async fn test_caffeinate_child_exits_when_dropped() {
        // Spawns by name and reads the process table: hold the shared agent-state
        // lock so a concurrent test cannot empty PATH under it.
        let _guard = crate::console_adapter::agent_state_guard();
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
        let _ = std::process::Command::new("kill")
            .arg(caffeinate_pid.to_string())
            .status();
    }

    fn omp_observation(
        session_file: Option<PathBuf>,
        live: bool,
    ) -> managed_omp_helm_scan::OmpHelmObservation {
        managed_omp_helm_scan::OmpHelmObservation {
            session_id: "session-omp".to_string(),
            native_session_id: Some("native-omp".to_string()),
            run_id: Some("run-omp".to_string()),
            connection_id: Some("connection-omp".to_string()),
            lease_generation: Some("generation-omp".to_string()),
            state_file: PathBuf::from("/nonexistent/state.json"),
            session_file,
            socket_path: None,
            cwd: Some("/tmp".to_string()),
            launcher_pid: Some(1),
            launcher_process_start_time: Some("start".to_string()),
            provider_pid: Some(2),
            provider_process_start_time: Some("start".to_string()),
            started_at: "2026-09-13T00:00:00Z".to_string(),
            updated_at: "2026-09-13T00:00:01Z".to_string(),
            phase: Some("running".to_string()),
            tool_name: None,
            status: "ready".to_string(),
            launcher_alive: true,
            provider_alive: true,
            live,
            control_ready: live,
        }
    }

    fn reconcile_targets_for(conn: &rusqlite::Connection) -> Vec<ReconcileTarget> {
        crate::state::session_binding::SessionBinding::new(conn)
            .list_bindings()
            .unwrap()
            .iter()
            .filter_map(|binding| reconcile_target_for(conn, binding))
            .collect()
    }

    fn bound_source(
        dir: &std::path::Path,
        name: &str,
        bytes: usize,
        session: &str,
    ) -> (PathBuf, rusqlite::Connection, String) {
        let transcript = dir.join(name);
        std::fs::write(&transcript, vec![b'x'; bytes]).unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(&transcript).unwrap();
        let canonical_text = canonical.to_string_lossy().to_string();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical_text, session, "omp")
            .unwrap();
        (canonical, conn, canonical_text)
    }

    #[test]
    fn a_bound_source_behind_its_lane_is_scheduled() {
        let dir = tempfile::tempdir().unwrap();
        let (canonical, conn, _) =
            bound_source(dir.path(), "omp-big.jsonl", 300 * 1024, "session-omp");
        let mut conn = conn;
        let opaque = crate::storage_v2_shipper::opaque_source_id(&canonical.to_string_lossy());
        crate::state::source_epoch::observe_file(
            &mut conn,
            "omp",
            &opaque,
            &canonical,
            crate::state::source_epoch::SourceLane::Durable,
            0,
            None,
            Some("session-omp"),
            crate::state::source_epoch::SourceChangeHint::None,
        )
        .unwrap();

        let targets = reconcile_targets_for(&conn);

        assert_eq!(targets.len(), 1);
        assert_eq!(targets[0].provider, "omp");
        assert!(targets[0].lag_bytes >= STARVED_LIVE_TRANSCRIPT_BYTES);
    }

    /// A refused source stays behind its file for as long as it is refused, so
    /// "behind" alone would schedule it every tick forever. It is left alone
    /// while its backoff runs and picked up again when the row is woken.
    #[test]
    fn a_refused_source_is_not_scheduled_until_its_backoff_elapses() {
        let dir = tempfile::tempdir().unwrap();
        let (canonical, conn, _) =
            bound_source(dir.path(), "omp-refused.jsonl", 300 * 1024, "session-omp");
        let mut conn = conn;
        let opaque = crate::storage_v2_shipper::opaque_source_id(&canonical.to_string_lossy());
        let resolution = crate::state::source_epoch::observe_file(
            &mut conn,
            "omp",
            &opaque,
            &canonical,
            crate::state::source_epoch::SourceLane::Durable,
            0,
            None,
            Some("session-omp"),
            crate::state::source_epoch::SourceChangeHint::None,
        )
        .unwrap();
        let pending = crate::state::pending_source_envelope::PendingSourceEnvelope::new(
            resolution.source_epoch,
            canonical.to_string_lossy().to_string(),
            0,
            10,
            "a".repeat(64),
            vec![1],
            vec![2],
            10,
            1,
            true,
            false,
        );
        crate::state::pending_source_envelope::persist_or_load(&mut conn, &pending).unwrap();
        assert_eq!(
            reconcile_targets_for(&conn).len(),
            1,
            "an unblocked pending source is scheduled as before"
        );

        crate::state::pending_source_envelope::quarantine(
            &conn,
            resolution.source_epoch,
            "source_epoch_conflict_unresolved",
            "predecessor_not_open_for_this_identity",
        )
        .unwrap();
        assert!(
            reconcile_targets_for(&conn).is_empty(),
            "a refused source inside its backoff must not be re-scheduled every tick"
        );

        assert_eq!(
            crate::state::pending_source_envelope::wake_blocked_for_new_engine(&conn).unwrap(),
            1
        );
        assert_eq!(reconcile_targets_for(&conn).len(), 1);
    }

    #[test]
    fn a_current_lane_is_not_scheduled() {
        let dir = tempfile::tempdir().unwrap();
        let bytes = 300 * 1024;
        let (canonical, conn, _) =
            bound_source(dir.path(), "omp-current.jsonl", bytes, "session-omp");
        let mut conn = conn;
        let opaque = crate::storage_v2_shipper::opaque_source_id(&canonical.to_string_lossy());
        crate::state::source_epoch::observe_file(
            &mut conn,
            "omp",
            &opaque,
            &canonical,
            crate::state::source_epoch::SourceLane::Durable,
            bytes as u64,
            None,
            Some("session-omp"),
            crate::state::source_epoch::SourceChangeHint::None,
        )
        .unwrap();

        assert!(
            reconcile_targets_for(&conn).is_empty(),
            "a source whose lane is current must not be scheduled"
        );
    }

    #[test]
    fn an_unbound_source_is_not_scheduled() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("stranger.jsonl"), vec![b'y'; 300 * 1024]).unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();

        assert!(reconcile_targets_for(&conn).is_empty());
    }

    #[test]
    fn a_stale_small_lag_is_scheduled_too() {
        let dir = tempfile::tempdir().unwrap();
        let bytes = STARVED_LIVE_TRANSCRIPT_BYTES as usize / 8;
        let (canonical, conn, _) = bound_source(dir.path(), "omp-slow.jsonl", bytes, "session-omp");
        let mut conn = conn;
        let opaque = crate::storage_v2_shipper::opaque_source_id(&canonical.to_string_lossy());
        let resolution = crate::state::source_epoch::observe_file(
            &mut conn,
            "omp",
            &opaque,
            &canonical,
            crate::state::source_epoch::SourceLane::Durable,
            0,
            None,
            Some("session-omp"),
            crate::state::source_epoch::SourceChangeHint::None,
        )
        .unwrap();
        conn.execute(
            "UPDATE source_epoch_lane_state SET updated_at = '2020-01-01T00:00:00+00:00' WHERE source_epoch = ?1",
            rusqlite::params![resolution.source_epoch.to_string()],
        )
        .unwrap();

        let targets = reconcile_targets_for(&conn);

        assert_eq!(
            targets.len(),
            1,
            "a stale lane with a small lag must be scheduled"
        );
        assert!(!targets[0].never_shipped);
    }

    #[test]
    fn a_bound_source_that_never_shipped_is_scheduled_whatever_its_size() {
        let dir = tempfile::tempdir().unwrap();
        // Tiny on purpose: without an epoch there is no lane and no lane age, so
        // neither the byte threshold nor staleness would ever admit it.
        let (_canonical, conn, _) = bound_source(dir.path(), "omp-first.jsonl", 24, "session-omp");

        let targets = reconcile_targets_for(&conn);

        assert_eq!(targets.len(), 1);
        assert!(targets[0].never_shipped);
    }

    #[test]
    fn a_retired_launch_keeps_its_binding_for_drain() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("omp-retired.jsonl");
        std::fs::write(&transcript, b"{}\n").unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(&transcript).unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), "session-omp", "omp")
            .unwrap();

        // The launcher is gone but the file it owned still has records to ship.
        let snapshot = ManagedObservationSnapshot {
            omp: vec![omp_observation(Some(transcript.clone()), false)],
            ..Default::default()
        };
        project_binding_liveness(&conn, &snapshot);

        let listed = crate::state::session_binding::SessionBinding::new(&conn)
            .list_bindings()
            .unwrap();
        assert_eq!(listed.len(), 1, "an exited owner must not lose its binding");
        assert_eq!(
            listed[0].state,
            crate::state::session_binding::BINDING_STATE_EXITED
        );
    }

    fn binding_state(conn: &rusqlite::Connection, path: &std::path::Path) -> String {
        conn.query_row(
            "SELECT state FROM session_binding WHERE path = ?1",
            [std::fs::canonicalize(path)
                .unwrap()
                .to_string_lossy()
                .as_ref()],
            |row| row.get(0),
        )
        .unwrap()
    }

    #[test]
    fn a_live_run_reactivates_its_binding_and_a_restated_state_writes_nothing() {
        use crate::state::wal_window::WalWindow;
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("omp-resumed.jsonl");
        std::fs::write(&transcript, b"{}\n").unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(&transcript).unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), "session-omp", "omp")
            .unwrap();
        let stopped = ManagedObservationSnapshot {
            omp: vec![omp_observation(Some(transcript.clone()), false)],
            ..Default::default()
        };
        let running = ManagedObservationSnapshot {
            omp: vec![omp_observation(Some(transcript.clone()), true)],
            ..Default::default()
        };

        project_binding_liveness(&conn, &stopped);
        assert_eq!(binding_state(&conn, &transcript), "exited");

        // The process evidence, not the file, is what brings it back.
        project_binding_liveness(&conn, &running);
        assert_eq!(binding_state(&conn, &transcript), "active");
        project_binding_liveness(&conn, &stopped);
        assert_eq!(binding_state(&conn, &transcript), "exited");

        let window = WalWindow::open(&conn);
        for _pass in 0..3 {
            project_binding_liveness(&conn, &stopped);
        }
        let cost = window.cost(&conn);
        assert!(cost.is_zero(), "restating an exit wrote: {cost:?}");
    }

    #[test]
    fn a_resumed_run_beside_its_retired_predecessor_keeps_the_transcript_active() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("omp-shared.jsonl");
        std::fs::write(&transcript, b"{}\n").unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(&transcript).unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), "session-omp", "omp")
            .unwrap();

        // Two state files name the transcript, in either order: one for the
        // dead first run and one for the live resumed run of the same session.
        for live_first in [true, false] {
            let mut rows = vec![
                omp_observation(Some(transcript.clone()), false),
                omp_observation(Some(transcript.clone()), true),
            ];
            if !live_first {
                rows.reverse();
            }
            let snapshot = ManagedObservationSnapshot {
                omp: rows,
                ..Default::default()
            };
            project_binding_liveness(&conn, &snapshot);
            assert_eq!(binding_state(&conn, &transcript), "active");
        }
    }

    #[test]
    fn a_run_that_ended_does_not_retire_the_binding_of_the_session_that_took_its_path() {
        let dir = tempfile::tempdir().unwrap();
        let transcript = dir.path().join("omp-reused.jsonl");
        std::fs::write(&transcript, b"{}\n").unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let canonical = std::fs::canonicalize(&transcript).unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), "later-session", "omp")
            .unwrap();

        // `omp_observation` is the run of "session-omp", which no longer owns it.
        let snapshot = ManagedObservationSnapshot {
            omp: vec![omp_observation(Some(transcript.clone()), false)],
            ..Default::default()
        };
        project_binding_liveness(&conn, &snapshot);

        assert_eq!(binding_state(&conn, &transcript), "active");
    }

    #[test]
    fn test_running_control_file_can_resume_paused_archive_replay_as_trickle() {
        // Mutates process-global environment: hold the shared agent-state
        // lock so a concurrent test does not spawn under this one's PATH.
        let _guard = crate::console_adapter::agent_state_guard();
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
                assert!(archive_repair_is_paused(ArchiveRepairMode::Paused));

                let control_path = config::get_agent_archive_repair_control_path().unwrap();
                std::fs::create_dir_all(control_path.parent().unwrap()).unwrap();
                let expires_at = (chrono::Utc::now() + chrono::Duration::hours(1)).to_rfc3339();
                std::fs::write(
                    &control_path,
                    serde_json::to_vec(&json!({"mode": "trickle", "expires_at": expires_at}))
                        .unwrap(),
                )
                .unwrap();

                assert_eq!(
                    read_archive_repair_control().normalized_mode(ArchiveRepairMode::Paused),
                    ArchiveRepairMode::Trickle
                );
                assert!(!archive_repair_is_paused(ArchiveRepairMode::Paused));
            },
        );
    }
}
