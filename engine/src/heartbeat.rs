//! Periodic heartbeat emitter.
//!
//! The daemon reuses this payload for two related jobs:
//!
//! - frequent local status-file writes for ambient UX / debugging
//! - less frequent server heartbeats to `/api/agents/heartbeat`

use std::collections::HashMap;
use std::collections::HashSet;
#[cfg(target_os = "linux")]
use std::fs;
use std::path::Path;
#[cfg(target_os = "macos")]
use std::process::Command;
use std::sync::OnceLock;
use std::time::Duration;

use anyhow::Result;
use chrono::DateTime;
use chrono::Datelike;
use chrono::SecondsFormat;
use chrono::Utc;
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::build_identity::BuildIdentity;
use crate::control_channel::granted_control_operations;
use crate::managed_antigravity_scan::AntigravityHookObservation;
use crate::managed_bridge_scan::CodexBridgeObservation;
use crate::managed_claude_scan::ClaudeChannelObservation;
use crate::managed_cursor_helm_scan::CursorHelmObservation;
use crate::managed_opencode_scan::OpenCodeServerObservation;

/// Captured once per daemon process at the first write_status_file call.
/// Compared against the on-disk binary mtime to detect "restart pending".
static DAEMON_STARTED_AT: OnceLock<String> = OnceLock::new();
static MACHINE_BOOT_ID: OnceLock<Option<String>> = OnceLock::new();
use crate::config;
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::shipping::client::ShipperClient;
use crate::shipping_stats::RecentShipStatsTracker;
use crate::shipping_stats::ShipLaneSummarySet;
use crate::state::pending_source_envelope::{self, StorageV2OutboxSnapshot};
use crate::state::session_phase::PhaseLedgerRow;
use crate::state::spool::ArchiveBacklogSnapshot;
use crate::state::spool::DeadLetterEntry;
use crate::state::spool::Spool;

const HEARTBEAT_POST_TIMEOUT: Duration = Duration::from_secs(6);
const MAX_MACHINE_EVIDENCE_FACTS_PER_FAMILY: usize = 2_048;
const MAX_REDUCER_EVIDENCE_FACTS: usize = 256;
const ANTIGRAVITY_READINESS_TTL_SECS: i64 = 120;

/// Heartbeat payload sent to the server and written locally.
#[derive(Debug, Serialize, Clone)]
pub struct HeartbeatPayload {
    pub version: String,
    pub daemon_pid: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    /// RFC3339 timestamp of the last successful ship.
    pub last_ship_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    /// RFC3339 timestamp of the last ship attempt, successful or not.
    pub last_ship_attempt_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_ship_result: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_ship_latency_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_ship_http_status: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_ship_error_kind: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_ship_error_message: Option<String>,
    pub spool_pending_count: usize,
    pub spool_dead_count: usize,
    #[serde(default)]
    pub archive_backlog: ArchiveBacklogSnapshot,
    #[serde(default)]
    pub storage_v2_outbox: StorageV2OutboxSnapshot,
    pub parse_error_count_1h: u32,
    pub consecutive_ship_failures: u32,
    pub ship_attempts_1h: u32,
    pub ship_successes_1h: u32,
    pub ship_rate_limited_1h: u32,
    pub ship_server_errors_1h: u32,
    pub ship_payload_rejections_1h: u32,
    pub ship_payload_too_large_1h: u32,
    pub ship_retryable_client_errors_1h: u32,
    pub ship_connect_errors_1h: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ship_latency_p50_ms_1h: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ship_latency_p95_ms_1h: Option<u64>,
    pub ship_attempts_10m: u32,
    pub ship_successes_10m: u32,
    pub ship_rate_limited_10m: u32,
    pub ship_server_errors_10m: u32,
    pub ship_retryable_client_errors_10m: u32,
    pub ship_connect_errors_10m: u32,
    #[serde(default)]
    pub ship_lanes: ShipLaneSummarySet,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub events_per_sec_ewma_10s: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bytes_per_sec_ewma_10s: Option<f64>,
    pub disk_free_bytes: u64,
    pub is_offline: bool,
    #[serde(default)]
    pub managed_sessions: Vec<ManagedSessionLease>,
    /// Phase 5 of session-liveness-honesty: machine-observed pid/cwd
    /// bindings for unmanaged provider-CLI sessions. Empty arrays are a
    /// complete snapshot too: the Runtime Host consumes them to close sessions
    /// whose provider process is confirmed gone.
    #[serde(default)]
    pub unmanaged_session_bindings: Vec<UnmanagedSessionBinding>,
    /// Additive, versioned machine observations grouped by authority. The
    /// Runtime Host validates and retains this envelope but does not reduce it
    /// yet; legacy lease/session fields remain the compatibility authority.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub machine_evidence: Option<MachineEvidence>,
    /// Resolved local session/execution view. This is the canonical local
    /// identity graph projection for menu bar and local-health consumers:
    /// managed/unmanaged presentation state is derived after joining raw
    /// bridge, channel, transcript, and process observations.
    #[serde(default)]
    pub sessions: Vec<ResolvedLocalSession>,
    /// Stable hash of the canonical session snapshot over identity/control
    /// fields only. Timestamp-only freshness changes intentionally do not
    /// change this digest.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sessions_digest: Option<String>,
    /// Daemon-local monotonic sequence that increments whenever
    /// `sessions_digest` changes.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sessions_sequence: Option<u64>,
    /// Phase-2 adaptive backlog limiter snapshot. Drives AIMD off the
    /// `X-Ingest-Queue-Wait-Ms` header on each successful ship; absent on
    /// processes that haven't built a scheduler yet (e.g. legacy heartbeat
    /// callers).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub adaptive_backlog_limiter: Option<crate::scheduler::LimiterSnapshot>,
    /// Current path-scheduler ready/in-flight pressure by lane.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub ship_scheduler: Option<crate::scheduler::SchedulerSnapshot>,
    /// Durable, path-free discovery inventory used for onboarding progress.
    #[serde(default)]
    pub history_import: crate::state::source_inventory::HistoryImportSnapshot,
}

/// One machine-observed binding of an unmanaged provider CLI process to
/// its JSONL transcript. See `server/zerg/routers/heartbeat.py` and
/// `docs/specs/session-liveness-honesty.md` Phase 5.
#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct UnmanagedSessionBinding {
    pub machine_id: String,
    pub provider: String,
    pub provider_session_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_inode: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_device: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_start_time: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_offset: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_mtime: Option<String>,
    pub observed_at: String,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct MachineEvidence {
    pub schema_version: u16,
    pub observed_at: String,
    /// Reducer-grade identities are kept beside the additive fact families so
    /// v1 fact payloads remain readable during the compatibility window.
    #[serde(default)]
    pub identities: Vec<EvidenceIdentity>,
    #[serde(default)]
    pub process: Vec<ProcessEvidence>,
    #[serde(default)]
    pub activity: Vec<ActivityEvidence>,
    #[serde(default)]
    pub control: Vec<ControlEvidence>,
    #[serde(default)]
    pub transcript: Vec<TranscriptEvidence>,
    #[serde(default)]
    pub process_snapshot_scopes: Vec<ProcessSnapshotScope>,
    #[serde(default)]
    pub readiness: Vec<ReadinessEvidence>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct EvidenceIdentity {
    pub fact_family: String,
    pub fact_index: usize,
    pub subject_key: String,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_epoch: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_seq: Option<u64>,
    pub sequenced: bool,
    pub dedupe_key: String,
    pub evidence_hash: String,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ProcessSnapshotScope {
    pub scope: String,
    pub complete: bool,
    pub captured_at: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub machine_boot_id: Option<String>,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure_reason: Option<String>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ReadinessEvidence {
    pub authority_class: String,
    pub provider: String,
    pub session_id: String,
    pub operation: String,
    pub hook_installed: bool,
    pub recent_hook_observed: bool,
    pub claim_observed: bool,
    pub response_observed: bool,
    pub continuation_observed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hook_event: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hook_observed_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub claim_message_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub claimed_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_event: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub response_status: Option<String>,
    pub observed_at: String,
    pub valid_until: String,
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_locator: Option<String>,
    #[serde(default)]
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ProcessEvidence {
    pub authority_class: String,
    pub provider: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    pub role: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_start_time: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub boot_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    pub alive: bool,
    pub source: String,
    pub observed_at: String,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ActivityEvidence {
    pub authority_class: String,
    pub provider: String,
    pub session_id: String,
    /// Durable run authority. Absent phase-only observations remain
    /// diagnostic and are not eligible for run reduction.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    pub kind: String,
    pub raw_kind: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    pub source: String,
    pub observed_at: String,
    pub valid_until: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub raw_locator: Option<String>,
    #[serde(default)]
    pub reason_codes: Vec<String>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ControlEvidence {
    pub authority_class: String,
    pub provider: String,
    pub session_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    /// Immutable adapter connection identity plus its scoped lease epoch.
    /// Scanner observations without both values remain diagnostic-only.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub connection_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub lease_generation: Option<String>,
    pub ownership: String,
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bridge_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_subscription_status: Option<String>,
    pub lease_ttl_ms: u64,
    #[serde(default)]
    pub granted_operations: Vec<String>,
    pub source: String,
    pub observed_at: String,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct TranscriptEvidence {
    pub authority_class: String,
    pub provider: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    pub provider_session_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_inode: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_device: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_offset: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_mtime: Option<String>,
    pub source: String,
    pub observed_at: String,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ResolvedLocalSession {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    pub provider: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    pub control_path: String,
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase_observed_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_activity_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeline_title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub first_user_message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title_state: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title_source: Option<String>,
    pub workspace: ResolvedWorkspace,
    pub process: ResolvedProcess,
    pub bridge: ResolvedBridge,
    pub evidence: ResolvedEvidence,
    #[serde(default)]
    pub reason_codes: Vec<String>,
}

pub fn apply_local_titles(conn: &rusqlite::Connection, sessions: &mut [ResolvedLocalSession]) {
    for session in sessions {
        let Some(session_id) = session.session_id.as_deref() else {
            continue;
        };
        let Ok(Some(title)) = crate::state::session_title::get(conn, session_id) else {
            continue;
        };
        session.timeline_title = Some(title.title);
        session.first_user_message = Some(title.first_user_message);
        session.title_state = Some("pending".to_string());
        session.title_source = Some("prompt".to_string());
    }
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq, Default)]
pub struct ResolvedWorkspace {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub cwd: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub branch: Option<String>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq, Default)]
pub struct ResolvedProcess {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_start_time: Option<String>,
    /// Machine boot identity paired with pid + process-start identity. This is
    /// intentionally opaque because Linux and macOS expose different stable
    /// boot markers.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub boot_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
}

/// Fill the boot identity for process observations before the heartbeat leaves
/// the Machine Agent. A boot id is machine-scoped, but belongs with each
/// process tuple so a receiver never mistakes a recycled PID after reboot for
/// the prior process.
pub(crate) fn apply_machine_boot_identity(sessions: &mut [ResolvedLocalSession]) {
    let boot_id = machine_boot_id();
    apply_boot_identity(sessions, boot_id.as_deref());
}

fn apply_boot_identity(sessions: &mut [ResolvedLocalSession], boot_id: Option<&str>) {
    for session in sessions {
        if session.process.pid.is_some() {
            session.process.boot_id = boot_id.map(str::to_string);
        }
    }
}

fn machine_boot_id() -> Option<String> {
    MACHINE_BOOT_ID.get_or_init(detect_machine_boot_id).clone()
}

#[cfg(target_os = "linux")]
fn detect_machine_boot_id() -> Option<String> {
    fs::read_to_string("/proc/sys/kernel/random/boot_id")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .map(|value| format!("linux:{value}"))
}

#[cfg(target_os = "macos")]
fn detect_machine_boot_id() -> Option<String> {
    let output = Command::new("sysctl")
        .args(["-n", "kern.boottime"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    macos_boot_id_from_sysctl(&String::from_utf8_lossy(&output.stdout))
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn detect_machine_boot_id() -> Option<String> {
    None
}

#[cfg(target_os = "macos")]
fn macos_boot_id_from_sysctl(raw: &str) -> Option<String> {
    let field = |name: &str| {
        raw.split_once(name)
            .and_then(|(_, tail)| tail.split(|ch: char| !ch.is_ascii_digit()).next())
            .filter(|value| !value.is_empty())
    };
    let seconds = field("sec = ")?;
    let micros = field("usec = ")?;
    Some(format!("macos:{seconds}:{micros}"))
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq, Default)]
pub struct ResolvedBridge {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bridge_pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub app_server_pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ws_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub heartbeat_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_subscription_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub launch_mode: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ui_attached: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ui_presence: Option<String>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq, Default)]
pub struct ResolvedEvidence {
    pub process_observed: bool,
    pub transcript_observed: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bridge_state: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub hook_seen_at: Option<String>,
    #[serde(default)]
    pub join_keys: Vec<String>,
}

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ManagedSessionLease {
    pub session_id: String,
    pub provider: String,
    pub machine_id: String,
    pub sequence: u64,
    pub state: String,
    /// Deprecated local fields retained only for the resolved-session wire
    /// shape. Lease construction never populates activity.
    #[serde(skip)]
    pub phase: Option<String>,
    #[serde(skip)]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bridge_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_subscription_status: Option<String>,
    pub observed_at: String,
    pub lease_ttl_ms: u64,
}

/// Stats needed to build a heartbeat.
pub struct HeartbeatStats<'a> {
    pub conn: &'a rusqlite::Connection,
    pub spool: &'a Spool<'a>,
    pub tracker: &'a ConsecutiveErrorTracker,
    pub parse_tracker: &'a RecentIssueTracker,
    pub ship_stats: &'a RecentShipStatsTracker,
    pub is_offline: bool,
    pub last_ship_at: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
struct StatusDeadLetter {
    provider: String,
    file_path: String,
    start_offset: u64,
    end_offset: u64,
    range_bytes: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    session_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    last_error: Option<String>,
    created_at: String,
}

impl HeartbeatPayload {
    pub fn build(stats: &HeartbeatStats<'_>) -> Self {
        let spool_pending_count = stats.spool.pending_count().unwrap_or(0);
        let spool_dead_count = stats.spool.dead_count().unwrap_or(0);
        let archive_backlog = stats.spool.archive_backlog_snapshot().unwrap_or_default();
        let storage_v2_outbox =
            pending_source_envelope::snapshot(stats.conn).unwrap_or_else(|error| {
                StorageV2OutboxSnapshot {
                    error: Some(error.to_string()),
                    ..StorageV2OutboxSnapshot::default()
                }
            });
        let history_import = crate::state::source_inventory::HistoryImportSnapshot::load(
            stats.conn,
            &storage_v2_outbox,
        );
        let parse_error_count_1h = stats.parse_tracker.count_last_hour();
        let consecutive_ship_failures = stats.tracker.consecutive_count();
        let disk_free_bytes = get_disk_free();
        let ship_stats = stats.ship_stats.summary();

        HeartbeatPayload {
            version: BuildIdentity::current().qualified(),
            daemon_pid: std::process::id(),
            last_ship_at: stats.last_ship_at.clone(),
            last_ship_attempt_at: ship_stats.last_ship_attempt_at,
            last_ship_result: ship_stats.last_ship_result,
            last_ship_latency_ms: ship_stats.last_ship_latency_ms,
            last_ship_http_status: ship_stats.last_ship_http_status,
            last_ship_error_kind: ship_stats.last_ship_error_kind,
            last_ship_error_message: ship_stats.last_ship_error_message,
            spool_pending_count,
            spool_dead_count,
            archive_backlog,
            storage_v2_outbox,
            parse_error_count_1h,
            consecutive_ship_failures,
            ship_attempts_1h: ship_stats.ship_attempts_1h,
            ship_successes_1h: ship_stats.ship_successes_1h,
            ship_rate_limited_1h: ship_stats.ship_rate_limited_1h,
            ship_server_errors_1h: ship_stats.ship_server_errors_1h,
            ship_payload_rejections_1h: ship_stats.ship_payload_rejections_1h,
            ship_payload_too_large_1h: ship_stats.ship_payload_too_large_1h,
            ship_retryable_client_errors_1h: ship_stats.ship_retryable_client_errors_1h,
            ship_connect_errors_1h: ship_stats.ship_connect_errors_1h,
            ship_latency_p50_ms_1h: ship_stats.ship_latency_p50_ms_1h,
            ship_latency_p95_ms_1h: ship_stats.ship_latency_p95_ms_1h,
            ship_attempts_10m: ship_stats.ship_attempts_10m,
            ship_successes_10m: ship_stats.ship_successes_10m,
            ship_rate_limited_10m: ship_stats.ship_rate_limited_10m,
            ship_server_errors_10m: ship_stats.ship_server_errors_10m,
            ship_retryable_client_errors_10m: ship_stats.ship_retryable_client_errors_10m,
            ship_connect_errors_10m: ship_stats.ship_connect_errors_10m,
            ship_lanes: ship_stats.lanes,
            events_per_sec_ewma_10s: ship_stats.events_per_sec_ewma_10s,
            bytes_per_sec_ewma_10s: ship_stats.bytes_per_sec_ewma_10s,
            disk_free_bytes,
            is_offline: stats.is_offline,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            machine_evidence: None,
            sessions: Vec::new(),
            sessions_digest: None,
            sessions_sequence: None,
            adaptive_backlog_limiter: None,
            ship_scheduler: None,
            history_import,
        }
    }
}

/// Deterministic digest for the local session truth graph.
///
/// The digest covers fields that change session identity, ownership, control,
/// or visible state. It deliberately excludes observation timestamps and
/// freshness counters so frequent heartbeats can refresh liveness without
/// forcing the Runtime Host to re-run snapshot cleanup work.
pub fn session_snapshot_digest(payload: &HeartbeatPayload) -> String {
    let mut sessions: Vec<String> = payload
        .sessions
        .iter()
        .map(|session| {
            let mut join_keys = session.evidence.join_keys.clone();
            join_keys.sort();
            let mut reason_codes = session.reason_codes.clone();
            reason_codes.sort();
            format!(
                "{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}|{}",
                session.provider,
                session.session_id.as_deref().unwrap_or(""),
                session.provider_session_id.as_deref().unwrap_or(""),
                session.control_path,
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
                session.process.boot_id.as_deref().unwrap_or(""),
                session.process.started_at.as_deref().unwrap_or(""),
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
                session.bridge.launch_mode.as_deref().unwrap_or(""),
                session
                    .bridge
                    .ui_attached
                    .map(|attached| attached.to_string())
                    .unwrap_or_default(),
                session.bridge.ui_presence.as_deref().unwrap_or(""),
                session.evidence.process_observed,
                session.evidence.transcript_observed,
                session.evidence.bridge_state.as_deref().unwrap_or(""),
                join_keys.join(","),
                reason_codes.join(",")
            )
        })
        .collect();
    sessions.sort();
    let signature = format!("sessions=[{}]", sessions.join(";"));
    let mut hasher = Sha256::new();
    hasher.update(signature.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Build lease views from a pre-collected set of bridge observations.
/// Kept pure (no fs/ps side effects) so the reaper and tests can share
/// the same scan pass.
pub(crate) fn leases_from_observations(
    machine_id: &str,
    observations: &[CodexBridgeObservation],
    now: DateTime<Utc>,
) -> Vec<ManagedSessionLease> {
    let sequence = now.timestamp_millis().max(0) as u64;
    let observed_at = now.to_rfc3339();
    let mut leases = Vec::with_capacity(observations.len());

    for obs in observations {
        if codex_bridge_observation_is_stopped(obs) {
            continue;
        }
        let thread_failed = matches!(
            obs.thread_subscription_status.as_deref(),
            Some("failed") | Some("provider_thread_switched")
        );
        let has_bridge_error = obs
            .last_error
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty());
        let bridge_ready = obs.status == "ready";

        let detached_ui_control_ready = bridge_ready
            && obs.app_server_alive
            && obs
                .thread_id
                .as_deref()
                .is_some_and(|value| !value.trim().is_empty());
        let lease_state = if !obs.bridge_alive {
            "detached"
        } else if (bridge_ready && obs.has_tui_attachment || detached_ui_control_ready)
            && !thread_failed
            && !has_bridge_error
        {
            "attached"
        } else {
            "degraded"
        };

        leases.push(ManagedSessionLease {
            session_id: obs.session_id.clone(),
            provider: "codex".to_string(),
            machine_id: machine_id.trim().to_string(),
            sequence,
            state: lease_state.to_string(),
            phase: None,
            tool_name: None,
            bridge_status: Some(obs.status.clone()),
            thread_subscription_status: obs.thread_subscription_status.clone(),
            observed_at: obs.updated_at.clone().if_empty(observed_at.clone()),
            lease_ttl_ms: 15 * 60 * 1000,
        });
    }

    leases.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    leases
}

fn codex_bridge_observation_is_stopped(obs: &CodexBridgeObservation) -> bool {
    obs.status.trim().eq_ignore_ascii_case("stopped")
}

/// Build managed-session leases for Claude channel sessions.
///
/// Only live Claude provider processes are emitted. Disappearance withdraws
/// the current control observation; terminal lifecycle remains explicit
/// provider/process evidence rather than a lease-side inference.
pub(crate) fn leases_from_claude_channel_observations(
    machine_id: &str,
    observations: &[ClaudeChannelObservation],
    now: DateTime<Utc>,
) -> Vec<ManagedSessionLease> {
    let sequence = now.timestamp_millis().max(0) as u64;
    let observed_at = now.to_rfc3339();
    let mut leases = Vec::with_capacity(observations.len());

    for obs in observations {
        if !obs.claude_alive {
            continue;
        }
        let lease_state = if obs.ready && obs.bridge_alive {
            "attached"
        } else {
            "degraded"
        };

        leases.push(ManagedSessionLease {
            session_id: obs.session_id.clone(),
            provider: "claude".to_string(),
            machine_id: machine_id.trim().to_string(),
            sequence,
            state: lease_state.to_string(),
            phase: None,
            tool_name: None,
            bridge_status: Some(if obs.ready && obs.bridge_alive {
                "ready".to_string()
            } else if obs.bridge_alive {
                "not_ready".to_string()
            } else {
                "bridge_down".to_string()
            }),
            thread_subscription_status: None,
            observed_at: obs.updated_at.clone().if_empty(observed_at.clone()),
            lease_ttl_ms: 15 * 60 * 1000,
        });
    }

    leases.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    leases
}

/// Build managed-session leases for OpenCode server-bridge sessions.
///
/// OpenCode does not have a separate lease sidecar like Codex. A live
/// `opencode serve` process plus Longhouse's private bridge state is the
/// readiness observation. Dead state files are omitted from the complete
/// heartbeat snapshot so the Runtime Host can detach the prior connection.
pub(crate) fn leases_from_opencode_server_observations(
    machine_id: &str,
    observations: &[OpenCodeServerObservation],
    now: DateTime<Utc>,
) -> Vec<ManagedSessionLease> {
    let sequence = now.timestamp_millis().max(0) as u64;
    let observed_at = now.to_rfc3339();
    let mut leases = Vec::with_capacity(observations.len());

    for obs in observations {
        if !obs.server_alive {
            continue;
        }
        let state = if obs.health_ready {
            "attached"
        } else {
            "degraded"
        };
        leases.push(ManagedSessionLease {
            session_id: obs.session_id.clone(),
            provider: "opencode".to_string(),
            machine_id: machine_id.trim().to_string(),
            sequence,
            state: state.to_string(),
            phase: None,
            tool_name: None,
            bridge_status: Some(
                if obs.health_ready {
                    "ready"
                } else {
                    "health_unavailable"
                }
                .to_string(),
            ),
            thread_subscription_status: None,
            observed_at: obs.updated_at.clone().if_empty(observed_at.clone()),
            lease_ttl_ms: 15 * 60 * 1000,
        });
    }

    leases.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    leases
}

/// Cursor Helm leases: a live `longhouse cursor` launcher (pid alive + control
/// socket present) is the readiness observation. Dead state files are omitted so
/// the Runtime Host detaches the prior connection. There is no separate lease
/// sidecar; the launcher's state file + socket IS the liveness signal.
pub(crate) fn leases_from_cursor_helm_observations(
    machine_id: &str,
    observations: &[CursorHelmObservation],
    now: DateTime<Utc>,
) -> Vec<ManagedSessionLease> {
    let sequence = now.timestamp_millis().max(0) as u64;
    let observed_at = now.to_rfc3339();
    let mut leases = Vec::with_capacity(observations.len());

    for obs in observations {
        if !obs.live {
            continue;
        }
        leases.push(ManagedSessionLease {
            session_id: obs.session_id.clone(),
            provider: "cursor".to_string(),
            machine_id: machine_id.trim().to_string(),
            sequence,
            state: "attached".to_string(),
            phase: None,
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: None,
            observed_at: obs.updated_at.clone().if_empty(observed_at.clone()),
            lease_ttl_ms: 15 * 60 * 1000,
        });
    }

    leases.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    leases
}

pub fn filter_unmanaged_bindings_owned_by_managed_observations(
    bindings: Vec<UnmanagedSessionBinding>,
    codex_observations: &[CodexBridgeObservation],
    claude_observations: &[ClaudeChannelObservation],
    opencode_observations: &[OpenCodeServerObservation],
    cursor_observations: &[CursorHelmObservation],
) -> Vec<UnmanagedSessionBinding> {
    let managed_codex = ManagedCodexKeys::from_observations(codex_observations);
    let managed_claude = ManagedClaudeKeys::from_observations(claude_observations);
    let mut managed_pids = HashSet::new();
    for observation in codex_observations {
        if observation.bridge_alive {
            managed_pids.insert(observation.bridge_pid);
        }
        if observation.app_server_alive {
            managed_pids.extend(observation.app_server_pid);
        }
    }
    for observation in claude_observations {
        if observation.claude_alive {
            managed_pids.extend(observation.claude_pid);
        }
        if observation.bridge_alive {
            managed_pids.extend(observation.bridge_pid);
        }
    }
    for observation in opencode_observations {
        if observation.server_alive {
            managed_pids.extend(observation.pid);
        }
    }
    for observation in cursor_observations {
        if observation.live {
            managed_pids.extend(observation.launcher_pid);
            managed_pids.extend(observation.cursor_pid);
        }
    }

    bindings
        .into_iter()
        .filter(|binding| {
            !binding.pid.is_some_and(|pid| managed_pids.contains(&pid))
                && !binding_owned_by_codex(binding, &managed_codex)
                && !binding_owned_by_claude(binding, &managed_claude)
        })
        .collect()
}

/// Build the additive v1 evidence envelope directly from scanner/ledger facts.
/// This must not consume `ManagedSessionLease` or `ResolvedLocalSession`: both
/// are compatibility projections that already mix independent authorities.
pub(crate) fn machine_evidence_from_observations(
    machine_id: &str,
    codex_observations: &[CodexBridgeObservation],
    antigravity_observations: &[AntigravityHookObservation],
    claude_observations: &[ClaudeChannelObservation],
    opencode_observations: &[OpenCodeServerObservation],
    cursor_observations: &[CursorHelmObservation],
    unmanaged_bindings: &[UnmanagedSessionBinding],
    phase_rows: &[PhaseLedgerRow],
    process_snapshot_complete: bool,
    now: DateTime<Utc>,
) -> MachineEvidence {
    let envelope_observed_at = now.to_rfc3339();
    let boot_id = machine_boot_id();
    let mut process = Vec::new();
    let mut control = Vec::new();
    let mut transcript = Vec::new();
    let mut readiness = antigravity_observations
        .iter()
        .map(|observation| antigravity_readiness_evidence(observation, now))
        .collect::<Vec<_>>();

    let observed_at = |value: &str| {
        if value.trim().is_empty() {
            envelope_observed_at.clone()
        } else {
            value.to_string()
        }
    };

    for obs in codex_observations {
        let at = observed_at(&obs.updated_at);
        process.push(ProcessEvidence {
            authority_class: "exact_process_identity".to_string(),
            provider: "codex".to_string(),
            session_id: Some(obs.session_id.clone()),
            provider_session_id: obs.thread_id.clone(),
            role: "bridge".to_string(),
            pid: Some(obs.bridge_pid),
            process_start_time: obs.bridge_process_start_time.clone(),
            boot_id: boot_id.clone(),
            cwd: obs.cwd.clone(),
            alive: obs.bridge_alive,
            source: "codex_bridge_scan".to_string(),
            observed_at: at.clone(),
        });
        if let Some(pid) = obs.app_server_pid {
            process.push(ProcessEvidence {
                authority_class: "exact_process_identity".to_string(),
                provider: "codex".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: obs.thread_id.clone(),
                role: "app_server".to_string(),
                pid: Some(pid),
                process_start_time: obs.app_server_process_start_time.clone(),
                boot_id: boot_id.clone(),
                cwd: obs.cwd.clone(),
                alive: obs.app_server_alive,
                source: "codex_bridge_scan".to_string(),
                observed_at: at.clone(),
            });
        }
        if !codex_bridge_observation_is_stopped(obs) && obs.run_id.is_some() {
            let thread_failed = matches!(
                obs.thread_subscription_status.as_deref(),
                Some("failed") | Some("provider_thread_switched")
            );
            let has_bridge_error = obs
                .last_error
                .as_deref()
                .is_some_and(|value| !value.trim().is_empty());
            let detached_control_ready = obs.status == "ready"
                && obs.app_server_alive
                && obs
                    .thread_id
                    .as_deref()
                    .is_some_and(|value| !value.trim().is_empty());
            let state = if !obs.bridge_alive {
                "detached"
            } else if (obs.status == "ready" && obs.has_tui_attachment || detached_control_ready)
                && !thread_failed
                && !has_bridge_error
            {
                "attached"
            } else {
                "degraded"
            };
            control.push(ControlEvidence {
                authority_class: "provider_control".to_string(),
                provider: "codex".to_string(),
                session_id: obs.session_id.clone(),
                provider_session_id: obs.thread_id.clone(),
                connection_id: obs.connection_id.clone(),
                lease_generation: obs.lease_generation.clone(),
                run_id: obs.run_id.clone(),
                granted_operations: granted_control_operations("codex", state == "attached"),
                ownership: "managed".to_string(),
                state: state.to_string(),
                bridge_status: Some(obs.status.clone()),
                thread_subscription_status: obs.thread_subscription_status.clone(),
                lease_ttl_ms: 15 * 60 * 1000,
                source: "codex_bridge_scan".to_string(),
                observed_at: envelope_observed_at.clone(),
            });
        }
        if let Some(provider_session_id) = obs
            .thread_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            transcript.push(TranscriptEvidence {
                authority_class: "source_cursor".to_string(),
                provider: "codex".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: provider_session_id.to_string(),
                source_path: obs.thread_path.clone(),
                source_inode: None,
                source_device: None,
                source_offset: None,
                source_mtime: None,
                source: "codex_bridge_scan".to_string(),
                observed_at: at,
            });
        }
    }

    for obs in claude_observations {
        let at = observed_at(&obs.updated_at);
        if let Some(pid) = obs.claude_pid {
            process.push(ProcessEvidence {
                authority_class: "exact_process_identity".to_string(),
                provider: "claude".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: obs.provider_session_id.clone(),
                role: "provider".to_string(),
                pid: Some(pid),
                process_start_time: Some(obs.started_at.clone())
                    .filter(|value| !value.trim().is_empty()),
                boot_id: boot_id.clone(),
                cwd: obs.cwd.clone(),
                alive: obs.claude_alive,
                source: "claude_channel_scan".to_string(),
                observed_at: at.clone(),
            });
        }
        if let Some(pid) = obs.bridge_pid {
            process.push(ProcessEvidence {
                authority_class: "exact_process_identity".to_string(),
                provider: "claude".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: obs.provider_session_id.clone(),
                role: "bridge".to_string(),
                pid: Some(pid),
                process_start_time: Some(obs.started_at.clone())
                    .filter(|value| !value.trim().is_empty()),
                boot_id: boot_id.clone(),
                cwd: obs.cwd.clone(),
                alive: obs.bridge_alive,
                source: "claude_channel_scan".to_string(),
                observed_at: at.clone(),
            });
        }
        if let Some(run_id) = &obs.run_id {
            let state = if !obs.claude_alive {
                "detached"
            } else if obs.ready && obs.bridge_alive {
                "attached"
            } else {
                "degraded"
            };
            control.push(ControlEvidence {
                authority_class: "provider_control".to_string(),
                provider: "claude".to_string(),
                session_id: obs.session_id.clone(),
                provider_session_id: obs.provider_session_id.clone(),
                connection_id: obs.connection_id.clone(),
                lease_generation: obs.lease_generation.clone(),
                run_id: Some(run_id.clone()),
                granted_operations: granted_control_operations("claude", state == "attached"),
                ownership: "managed".to_string(),
                state: state.to_string(),
                bridge_status: Some(
                    if obs.ready && obs.bridge_alive {
                        "ready"
                    } else if obs.bridge_alive {
                        "not_ready"
                    } else {
                        "bridge_down"
                    }
                    .to_string(),
                ),
                thread_subscription_status: None,
                lease_ttl_ms: 15 * 60 * 1000,
                source: "claude_channel_scan".to_string(),
                observed_at: envelope_observed_at.clone(),
            });
        }
        if let Some(provider_session_id) = obs
            .provider_session_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            transcript.push(TranscriptEvidence {
                authority_class: "source_cursor".to_string(),
                provider: "claude".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: provider_session_id.to_string(),
                source_path: None,
                source_inode: None,
                source_device: None,
                source_offset: None,
                source_mtime: None,
                source: "claude_channel_scan".to_string(),
                observed_at: at,
            });
        }
    }

    for obs in opencode_observations {
        let at = observed_at(&obs.updated_at);
        if let Some(pid) = obs.pid {
            process.push(ProcessEvidence {
                authority_class: "exact_process_identity".to_string(),
                provider: "opencode".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: Some(obs.provider_session_id.clone()),
                role: "provider".to_string(),
                pid: Some(pid),
                process_start_time: Some(obs.process_start_time.clone())
                    .filter(|value| !value.trim().is_empty()),
                boot_id: boot_id.clone(),
                cwd: obs.cwd.clone(),
                alive: obs.server_alive,
                source: "opencode_server_scan".to_string(),
                observed_at: at.clone(),
            });
        }
        if let Some(run_id) = &obs.run_id {
            let state = if !obs.server_alive {
                "detached"
            } else if obs.health_ready {
                "attached"
            } else {
                "degraded"
            };
            control.push(ControlEvidence {
                authority_class: "provider_control".to_string(),
                provider: "opencode".to_string(),
                session_id: obs.session_id.clone(),
                provider_session_id: Some(obs.provider_session_id.clone()),
                connection_id: obs.connection_id.clone(),
                lease_generation: obs.lease_generation.clone(),
                run_id: Some(run_id.clone()),
                granted_operations: granted_control_operations("opencode", state == "attached"),
                ownership: "managed".to_string(),
                state: state.to_string(),
                bridge_status: Some(
                    if obs.health_ready {
                        "ready"
                    } else {
                        "health_unavailable"
                    }
                    .to_string(),
                ),
                thread_subscription_status: None,
                lease_ttl_ms: 15 * 60 * 1000,
                source: "opencode_server_scan".to_string(),
                observed_at: envelope_observed_at.clone(),
            });
        }
        transcript.push(TranscriptEvidence {
            authority_class: "source_cursor".to_string(),
            provider: "opencode".to_string(),
            session_id: Some(obs.session_id.clone()),
            provider_session_id: obs.provider_session_id.clone(),
            source_path: None,
            source_inode: None,
            source_device: None,
            source_offset: None,
            source_mtime: None,
            source: "opencode_server_scan".to_string(),
            observed_at: at,
        });
    }

    for obs in cursor_observations {
        let at = observed_at(&obs.updated_at);
        for (role, pid, start_time, alive) in [
            (
                "launcher",
                obs.launcher_pid,
                obs.launcher_process_start_time.clone(),
                obs.launcher_alive,
            ),
            (
                "provider",
                obs.cursor_pid,
                obs.cursor_process_start_time.clone(),
                obs.cursor_pid.is_some(),
            ),
        ] {
            if let Some(pid) = pid {
                process.push(ProcessEvidence {
                    authority_class: "exact_process_identity".to_string(),
                    provider: "cursor".to_string(),
                    session_id: Some(obs.session_id.clone()),
                    provider_session_id: None,
                    role: role.to_string(),
                    pid: Some(pid),
                    process_start_time: start_time.filter(|value| !value.trim().is_empty()),
                    boot_id: boot_id.clone(),
                    cwd: obs.cwd.clone(),
                    alive,
                    source: "cursor_helm_scan".to_string(),
                    observed_at: at.clone(),
                });
            }
        }
        if let Some(run_id) = &obs.run_id {
            let state = if obs.live { "attached" } else { "detached" };
            control.push(ControlEvidence {
                authority_class: "provider_control".to_string(),
                provider: "cursor".to_string(),
                session_id: obs.session_id.clone(),
                provider_session_id: None,
                connection_id: obs.connection_id.clone(),
                lease_generation: obs.lease_generation.clone(),
                run_id: Some(run_id.clone()),
                granted_operations: granted_control_operations("cursor", state == "attached"),
                ownership: "managed".to_string(),
                state: state.to_string(),
                bridge_status: Some(if obs.live { "ready" } else { "unavailable" }.to_string()),
                thread_subscription_status: None,
                lease_ttl_ms: 15 * 60 * 1000,
                source: "cursor_helm_scan".to_string(),
                observed_at: envelope_observed_at.clone(),
            });
        }
    }

    for binding in unmanaged_bindings {
        if binding.pid.is_some() {
            process.push(ProcessEvidence {
                authority_class: "exact_process_identity".to_string(),
                provider: binding.provider.clone(),
                session_id: None,
                provider_session_id: Some(binding.provider_session_id.clone()),
                role: "provider".to_string(),
                pid: binding.pid,
                process_start_time: binding.process_start_time.clone(),
                boot_id: boot_id.clone(),
                cwd: binding.cwd.clone(),
                alive: true,
                source: "unmanaged_process_scan".to_string(),
                observed_at: observed_at(&binding.observed_at),
            });
        }
        let source_mtime = nonempty(binding.source_mtime.as_deref());
        if binding.source_offset.is_some() && source_mtime.is_none() {
            continue;
        }
        transcript.push(TranscriptEvidence {
            authority_class: "source_cursor".to_string(),
            provider: binding.provider.clone(),
            session_id: None,
            provider_session_id: binding.provider_session_id.clone(),
            source_path: binding.source_path.clone(),
            source_inode: binding.source_inode,
            source_device: binding.source_device,
            source_offset: binding.source_offset,
            source_mtime: source_mtime.map(str::to_string),
            source: "unmanaged_transcript_scan".to_string(),
            observed_at: source_mtime
                .map(&observed_at)
                .unwrap_or_else(|| observed_at(&binding.observed_at)),
        });
    }

    for obs in antigravity_observations {
        let at = observed_at(&obs.updated_at);
        // Hook state proves a provider session was observed, but does not own
        // a provider process. Emit that limitation honestly instead of
        // manufacturing liveness from hook readiness.
        process.push(ProcessEvidence {
            authority_class: "exact_process_identity".to_string(),
            provider: "antigravity".to_string(),
            session_id: Some(obs.session_id.clone()),
            provider_session_id: obs.provider_session_id.clone(),
            role: "provider".to_string(),
            pid: None,
            process_start_time: None,
            boot_id: boot_id.clone(),
            cwd: obs.cwd.clone(),
            alive: false,
            source: "antigravity_hook_state".to_string(),
            observed_at: at.clone(),
        });
        if let Some(provider_session_id) = obs
            .provider_session_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            transcript.push(TranscriptEvidence {
                authority_class: "source_cursor".to_string(),
                provider: "antigravity".to_string(),
                session_id: Some(obs.session_id.clone()),
                provider_session_id: provider_session_id.to_string(),
                source_path: obs.transcript_path.clone(),
                source_inode: None,
                source_device: None,
                source_offset: None,
                source_mtime: None,
                source: "antigravity_hook_state".to_string(),
                observed_at: at,
            });
        }
    }

    let managed_run_ids = codex_observations
        .iter()
        .filter_map(|observation| {
            observation
                .run_id
                .as_deref()
                .map(|run_id| (observation.session_id.as_str(), run_id))
        })
        .chain(claude_observations.iter().filter_map(|observation| {
            observation
                .run_id
                .as_deref()
                .map(|run_id| (observation.session_id.as_str(), run_id))
        }))
        .chain(opencode_observations.iter().filter_map(|observation| {
            observation
                .run_id
                .as_deref()
                .map(|run_id| (observation.session_id.as_str(), run_id))
        }))
        .chain(cursor_observations.iter().filter_map(|observation| {
            observation
                .run_id
                .as_deref()
                .map(|run_id| (observation.session_id.as_str(), run_id))
        }))
        .collect::<HashMap<_, _>>();
    let mut activity = phase_rows
        .iter()
        .filter_map(|row| {
            managed_run_ids
                .get(row.session_id.as_str())
                .copied()
                .map(|run_id| activity_evidence_from_phase_row(row, Some(run_id)))
        })
        .collect::<Vec<_>>();

    process.sort_by(|a, b| {
        (&a.provider, &a.session_id, &a.role, a.pid).cmp(&(
            &b.provider,
            &b.session_id,
            &b.role,
            b.pid,
        ))
    });
    activity.sort_by(|a, b| (&a.provider, &a.session_id).cmp(&(&b.provider, &b.session_id)));
    control.sort_by(|a, b| (&a.provider, &a.session_id).cmp(&(&b.provider, &b.session_id)));
    transcript.sort_by(|a, b| {
        (&a.provider, &a.provider_session_id).cmp(&(&b.provider, &b.provider_session_id))
    });
    readiness.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    process.truncate(MAX_MACHINE_EVIDENCE_FACTS_PER_FAMILY);
    activity.truncate(MAX_MACHINE_EVIDENCE_FACTS_PER_FAMILY);
    control.truncate(MAX_MACHINE_EVIDENCE_FACTS_PER_FAMILY);
    transcript.truncate(MAX_MACHINE_EVIDENCE_FACTS_PER_FAMILY);
    readiness.truncate(MAX_MACHINE_EVIDENCE_FACTS_PER_FAMILY);

    let identities = reducer_evidence_identities(
        machine_id,
        &process,
        &activity,
        &control,
        &transcript,
        &readiness,
    );

    MachineEvidence {
        schema_version: 3,
        observed_at: envelope_observed_at.clone(),
        identities,
        process,
        activity,
        control,
        transcript,
        process_snapshot_scopes: vec![
            ProcessSnapshotScope {
                scope: "managed_state_files".to_string(),
                complete: process_snapshot_complete,
                captured_at: envelope_observed_at.clone(),
                machine_boot_id: boot_id.clone(),
                source: "managed_provider_scan".to_string(),
                failure_reason: (!process_snapshot_complete)
                    .then(|| "incremental_or_partial_scan".to_string()),
            },
            ProcessSnapshotScope {
                scope: "unmanaged_provider_processes".to_string(),
                complete: process_snapshot_complete,
                captured_at: envelope_observed_at.clone(),
                machine_boot_id: boot_id,
                source: "unmanaged_process_scan".to_string(),
                failure_reason: (!process_snapshot_complete)
                    .then(|| "incremental_or_partial_scan".to_string()),
            },
        ],
        readiness,
    }
}

fn reducer_evidence_identities(
    machine_id: &str,
    process: &[ProcessEvidence],
    activity: &[ActivityEvidence],
    control: &[ControlEvidence],
    transcript: &[TranscriptEvidence],
    readiness: &[ReadinessEvidence],
) -> Vec<EvidenceIdentity> {
    let mut families = [Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new()];

    for (fact_index, fact) in process.iter().enumerate() {
        let (Some(pid), Some(start), Some(boot)) = (
            fact.pid,
            nonempty(fact.process_start_time.as_deref()),
            nonempty(fact.boot_id.as_deref()),
        ) else {
            continue;
        };
        let generation = stable_component(&format!("{}:{pid}:{start}", fact.provider));
        families[0].push(evidence_identity(
            "process",
            fact_index,
            format!(
                "process:{}:{}:{}:{pid}:{generation}",
                stable_component(machine_id),
                fact.provider,
                stable_component(boot),
            ),
            &fact.source,
            Some(generation),
            None,
            fact,
        ));
    }

    for (fact_index, fact) in transcript.iter().enumerate() {
        let epoch_material = format!(
            "transcript-evidence-v2:{}:{}:{}",
            fact.source_path.as_deref().unwrap_or(""),
            fact.source_device
                .map(|value| value.to_string())
                .unwrap_or_default(),
            fact.source_inode
                .map(|value| value.to_string())
                .unwrap_or_default(),
        );
        let source_epoch =
            (!epoch_material.starts_with("::")).then(|| stable_component(&epoch_material));
        families[3].push(evidence_identity(
            "transcript",
            fact_index,
            format!(
                "thread:{}:{}",
                fact.provider,
                stable_component(&fact.provider_session_id)
            ),
            &fact.source,
            source_epoch,
            fact.source_offset,
            fact,
        ));
    }

    for (fact_index, fact) in readiness.iter().enumerate() {
        let claim = nonempty(fact.claim_message_id.as_deref()).map(stable_component);
        let source_epoch = stable_component(&format!(
            "readiness-evidence-v2:{}",
            claim.as_deref().unwrap_or("")
        ));
        families[4].push(evidence_identity(
            "readiness",
            fact_index,
            format!("readiness:{}:{}", fact.session_id, fact.operation),
            &fact.source,
            Some(source_epoch),
            None,
            fact,
        ));
    }

    for (fact_index, fact) in activity.iter().enumerate() {
        let Some(run_id) = nonempty(fact.run_id.as_deref()) else {
            continue;
        };
        families[1].push(evidence_identity(
            "activity",
            fact_index,
            format!("run:{run_id}"),
            &fact.source,
            Some(run_id.to_string()),
            None,
            fact,
        ));
    }

    for (fact_index, fact) in control.iter().enumerate() {
        let (Some(connection_id), Some(lease_generation)) = (
            nonempty(fact.connection_id.as_deref()),
            nonempty(fact.lease_generation.as_deref()),
        ) else {
            continue;
        };
        families[2].push(evidence_identity(
            "control",
            fact_index,
            format!("connection:{connection_id}:{lease_generation}"),
            &fact.source,
            Some(lease_generation.to_string()),
            None,
            fact,
        ));
    }

    // Reserve capacity across independent families. A process-heavy machine
    // must not starve transcript or readiness evidence merely because process
    // facts sort first.
    let mut identities = Vec::new();
    let mut index = 0;
    while identities.len() < MAX_REDUCER_EVIDENCE_FACTS {
        let before = identities.len();
        for family in &families {
            if let Some(identity) = family.get(index) {
                identities.push(identity.clone());
                if identities.len() == MAX_REDUCER_EVIDENCE_FACTS {
                    break;
                }
            }
        }
        if identities.len() == before {
            break;
        }
        index += 1;
    }
    identities
}

fn evidence_identity<T: Serialize>(
    fact_family: &str,
    fact_index: usize,
    subject_key: String,
    source: &str,
    source_epoch: Option<String>,
    source_seq: Option<u64>,
    fact: &T,
) -> EvidenceIdentity {
    let value = serde_json::to_value(fact).expect("typed machine evidence must serialize");
    let bytes = serde_json::to_vec(
        &canonical_evidence_value(value).expect("typed machine evidence cannot contain floats"),
    )
    .expect("canonical machine evidence must serialize");
    let evidence_hash = format!("{:x}", Sha256::digest(bytes));
    let sequenced = source_seq.is_some();
    let position = source_seq
        .map(|value| value.to_string())
        .unwrap_or_else(|| evidence_hash.clone());
    let dedupe_key = stable_component(&format!(
        "{fact_family}:{subject_key}:{source}:{}:{position}",
        source_epoch.as_deref().unwrap_or("")
    ));
    EvidenceIdentity {
        fact_family: fact_family.to_string(),
        fact_index,
        subject_key,
        source: source.to_string(),
        source_epoch,
        source_seq,
        sequenced,
        dedupe_key,
        evidence_hash,
    }
}

fn stable_component(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

fn canonical_evidence_value(value: serde_json::Value) -> Result<serde_json::Value, &'static str> {
    canonical_evidence_value_for_field(value, None)
}

fn canonical_evidence_value_for_field(
    value: serde_json::Value,
    field: Option<&str>,
) -> Result<serde_json::Value, &'static str> {
    match value {
        serde_json::Value::Array(values) => Ok(serde_json::Value::Array(
            values
                .into_iter()
                .map(|value| canonical_evidence_value_for_field(value, field))
                .collect::<Result<_, _>>()?,
        )),
        // serde_json's default Map is a BTreeMap because the preserve_order
        // feature is disabled, so rebuilding the object defines sorted keys.
        serde_json::Value::Object(values) => Ok(serde_json::Value::Object(
            values
                .into_iter()
                .map(|(key, value)| {
                    let canonical = canonical_evidence_value_for_field(value, Some(&key))?;
                    Ok((key, canonical))
                })
                .collect::<Result<_, &'static str>>()?,
        )),
        serde_json::Value::Number(number) if number.is_f64() => {
            Err("floating-point machine evidence is not canonical")
        }
        serde_json::Value::String(value) if field.is_some_and(canonical_timestamp_field) => {
            // Chrono accepts RFC 3339 leap-second syntax while Python's
            // datetime does not. Preserve unsupported values identically in
            // both implementations instead of silently rewriting evidence.
            let canonical = if !has_canonical_rfc3339_syntax(&value)
                || value.as_bytes().get(17..19) == Some(b"60")
            {
                value
            } else {
                match DateTime::parse_from_rfc3339(&value) {
                    Ok(parsed) => {
                        let utc = parsed.with_timezone(&Utc);
                        if (1..=9999).contains(&utc.year()) {
                            utc.to_rfc3339_opts(SecondsFormat::Micros, true)
                        } else {
                            value
                        }
                    }
                    Err(_) => value,
                }
            };
            Ok(serde_json::Value::String(canonical))
        }
        other => Ok(other),
    }
}

fn has_canonical_rfc3339_syntax(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() < 20
        || !bytes[0..4].iter().all(u8::is_ascii_digit)
        || &bytes[0..4] == b"0000"
        || bytes[4] != b'-'
        || !bytes[5..7].iter().all(u8::is_ascii_digit)
        || bytes[7] != b'-'
        || !bytes[8..10].iter().all(u8::is_ascii_digit)
        || bytes[10] != b'T'
        || !bytes[11..13].iter().all(u8::is_ascii_digit)
        || bytes[13] != b':'
        || !bytes[14..16].iter().all(u8::is_ascii_digit)
        || bytes[16] != b':'
        || !bytes[17..19].iter().all(u8::is_ascii_digit)
    {
        return false;
    }

    let suffix_start = if bytes[19] == b'.' {
        let Some(offset) = bytes[20..].iter().position(|byte| !byte.is_ascii_digit()) else {
            return false;
        };
        if offset == 0 {
            return false;
        }
        20 + offset
    } else {
        19
    };
    let suffix = &bytes[suffix_start..];
    suffix == b"Z"
        || (suffix.len() == 6
            && matches!(suffix[0], b'+' | b'-')
            && suffix[1..3].iter().all(u8::is_ascii_digit)
            && suffix[3] == b':'
            && suffix[4..6].iter().all(u8::is_ascii_digit))
}

fn canonical_timestamp_field(field: &str) -> bool {
    matches!(
        field,
        "observed_at"
            | "valid_until"
            | "source_mtime"
            | "hook_observed_at"
            | "claimed_at"
            | "response_at"
    )
}

fn nonempty(value: Option<&str>) -> Option<&str> {
    value.map(str::trim).filter(|value| !value.is_empty())
}

fn antigravity_readiness_evidence(
    observation: &AntigravityHookObservation,
    now: DateTime<Utc>,
) -> ReadinessEvidence {
    let as_of = parse_utc(Some(&observation.updated_at)).unwrap_or(now);
    let hook_time = parse_utc(observation.last_hook_observed_at.as_deref());
    let claim_time = parse_utc(observation.last_claimed_at.as_deref());
    let response_time = parse_utc(observation.last_response_at.as_deref());
    let is_fresh = |value: Option<DateTime<Utc>>| {
        value.is_some_and(|at| {
            as_of >= at && as_of - at <= chrono::Duration::seconds(ANTIGRAVITY_READINESS_TTL_SECS)
        })
    };
    let recent_hook_observed = is_fresh(hook_time);
    let claim_observed = is_fresh(claim_time);
    let response_matches_claim =
        observation
            .last_claimed_message_id
            .as_ref()
            .is_some_and(|message_id| {
                observation
                    .last_response_claimed_message_ids
                    .iter()
                    .any(|candidate| candidate == message_id)
            });
    let response_observed = is_fresh(response_time)
        && observation.last_response_status.as_deref() == Some("ok")
        && response_matches_claim;
    let continuation_observed = response_observed && observation.last_continuation_requested;
    let hook_installed = observation.schema_version >= 2 && hook_time.is_some();
    let mut reason_codes = Vec::new();
    if !hook_installed {
        reason_codes.push("hook_identity_unproven".to_string());
    }
    if !recent_hook_observed {
        reason_codes.push("hook_observation_missing_or_expired".to_string());
    }
    if !claim_observed {
        reason_codes.push("claim_missing_or_expired".to_string());
    }
    if !response_observed {
        reason_codes.push("matching_response_missing_or_expired".to_string());
    }
    let valid_until = [hook_time, claim_time, response_time]
        .into_iter()
        .flatten()
        .map(|at| at + chrono::Duration::seconds(ANTIGRAVITY_READINESS_TTL_SECS))
        .min()
        .unwrap_or(as_of)
        .to_rfc3339();

    ReadinessEvidence {
        authority_class: "operation_proof".to_string(),
        provider: "antigravity".to_string(),
        session_id: observation.session_id.clone(),
        operation: "send_input".to_string(),
        hook_installed,
        recent_hook_observed,
        claim_observed,
        response_observed,
        continuation_observed,
        hook_event: observation.last_hook_event.clone(),
        hook_observed_at: observation.last_hook_observed_at.clone(),
        claim_message_id: observation.last_claimed_message_id.clone(),
        claimed_at: observation.last_claimed_at.clone(),
        response_event: observation.last_response_event.clone(),
        response_at: observation.last_response_at.clone(),
        response_status: observation.last_response_status.clone(),
        observed_at: observation.updated_at.clone(),
        valid_until,
        source: "antigravity_hook_state".to_string(),
        raw_locator: Some(observation.state_file.display().to_string()),
        reason_codes,
    }
}

fn parse_utc(value: Option<&str>) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value?)
        .ok()
        .map(|at| at.with_timezone(&Utc))
}

fn activity_evidence_from_phase_row(
    row: &PhaseLedgerRow,
    run_id: Option<&str>,
) -> ActivityEvidence {
    let raw_kind = row.phase.trim().to_string();
    let kind = match raw_kind.as_str() {
        "thinking" | "running" | "blocked" | "stalled" | "needs_user" | "idle" => raw_kind.clone(),
        _ => "unknown".to_string(),
    };
    let reason_codes = if raw_kind == "finished" {
        // A provider phase alone has neither run nor process authority. Keep
        // the raw observation, but do not turn it into a positive run terminal.
        vec!["run_terminal_authority_missing".to_string()]
    } else if kind == "unknown" {
        vec!["unknown_provider_activity".to_string()]
    } else {
        Vec::new()
    };
    ActivityEvidence {
        authority_class: "provider_runtime".to_string(),
        provider: row.provider.clone(),
        session_id: row.session_id.clone(),
        run_id: run_id.map(str::to_string),
        kind,
        raw_kind,
        tool_name: row.tool_name.clone(),
        detail: None,
        source: row.source.clone(),
        observed_at: row.observed_at.clone(),
        valid_until: row.valid_until.clone(),
        raw_locator: None,
        reason_codes,
    }
}

pub fn resolved_sessions_from_observations(
    managed_sessions: &[ManagedSessionLease],
    unmanaged_bindings: &[UnmanagedSessionBinding],
    codex_observations: &[CodexBridgeObservation],
    claude_observations: &[ClaudeChannelObservation],
    opencode_observations: &[OpenCodeServerObservation],
    cursor_observations: &[CursorHelmObservation],
) -> Vec<ResolvedLocalSession> {
    let codex_by_session: HashMap<&str, &CodexBridgeObservation> = codex_observations
        .iter()
        .map(|obs| (obs.session_id.as_str(), obs))
        .collect();
    let claude_by_session: HashMap<&str, &ClaudeChannelObservation> = claude_observations
        .iter()
        .map(|obs| (obs.session_id.as_str(), obs))
        .collect();
    let opencode_by_session: HashMap<&str, &OpenCodeServerObservation> = opencode_observations
        .iter()
        .map(|obs| (obs.session_id.as_str(), obs))
        .collect();
    let cursor_by_session: HashMap<&str, &CursorHelmObservation> = cursor_observations
        .iter()
        .map(|obs| (obs.session_id.as_str(), obs))
        .collect();

    let mut sessions = Vec::with_capacity(managed_sessions.len() + unmanaged_bindings.len());
    for lease in managed_sessions {
        match lease.provider.as_str() {
            "codex" => sessions.push(resolved_managed_codex_session(
                lease,
                codex_by_session.get(lease.session_id.as_str()).copied(),
            )),
            "claude" => sessions.push(resolved_managed_claude_session(
                lease,
                claude_by_session.get(lease.session_id.as_str()).copied(),
            )),
            "opencode" => sessions.push(resolved_managed_opencode_session(
                lease,
                opencode_by_session.get(lease.session_id.as_str()).copied(),
            )),
            "cursor" => sessions.push(resolved_managed_cursor_session(
                lease,
                cursor_by_session.get(lease.session_id.as_str()).copied(),
            )),
            _ => sessions.push(resolved_managed_generic_session(lease)),
        }
    }
    for binding in unmanaged_bindings {
        sessions.push(resolved_unmanaged_session(binding));
    }
    sessions.sort_by(|a, b| {
        a.control_path
            .cmp(&b.control_path)
            .then_with(|| a.provider.cmp(&b.provider))
            .then_with(|| {
                a.session_id
                    .as_deref()
                    .unwrap_or("")
                    .cmp(b.session_id.as_deref().unwrap_or(""))
            })
            .then_with(|| {
                a.provider_session_id
                    .as_deref()
                    .unwrap_or("")
                    .cmp(b.provider_session_id.as_deref().unwrap_or(""))
            })
    });
    sessions
}

fn resolved_managed_opencode_session(
    lease: &ManagedSessionLease,
    obs: Option<&OpenCodeServerObservation>,
) -> ResolvedLocalSession {
    let provider_session_id = obs.map(|obs| obs.provider_session_id.clone());
    let transcript_observed = provider_session_id.is_some();
    let process_pid = obs.and_then(|obs| obs.pid);
    let mut join_keys = vec![format!("session_id={}", lease.session_id)];
    if let Some(provider_session_id) = provider_session_id.as_deref() {
        join_keys.push(format!("provider_session_id={provider_session_id}"));
    }
    if let Some(pid) = process_pid {
        join_keys.push(format!("opencode_pid={pid}"));
    }

    ResolvedLocalSession {
        session_id: Some(lease.session_id.clone()),
        provider: lease.provider.clone(),
        provider_session_id,
        control_path: "managed".to_string(),
        state: lease.state.clone(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: None,
        timeline_title: None,
        first_user_message: None,
        title_state: None,
        title_source: None,
        workspace: workspace_from_cwd(obs.and_then(|obs| obs.cwd.clone())),
        process: ResolvedProcess {
            pid: process_pid,
            process_start_time: obs
                .map(|obs| obs.process_start_time.trim())
                .filter(|value| !value.is_empty())
                .map(str::to_string),
            boot_id: None,
            started_at: obs
                .map(|obs| obs.started_at.clone())
                .filter(|value| !value.trim().is_empty()),
        },
        bridge: ResolvedBridge {
            bridge_pid: process_pid,
            app_server_pid: None,
            ws_url: None,
            heartbeat_at: obs
                .map(|obs| obs.updated_at.clone())
                .filter(|value| !value.trim().is_empty()),
            status: lease.bridge_status.clone(),
            thread_subscription_status: None,
            launch_mode: obs
                .map(|obs| obs.launch_mode.trim())
                .filter(|value| !value.is_empty())
                .map(str::to_string)
                .or_else(|| Some("server_bridge".to_string())),
            ui_attached: obs.map(|obs| obs.has_tui_attachment),
            ui_presence: opencode_ui_presence(&lease.state, obs).map(str::to_string),
        },
        evidence: ResolvedEvidence {
            process_observed: obs.is_some_and(|obs| obs.server_alive),
            transcript_observed,
            bridge_state: lease.bridge_status.clone(),
            hook_seen_at: None,
            join_keys,
        },
        reason_codes: Vec::new(),
    }
}

/// Project UI presence for an OpenCode managed session from its launch mode.
///
/// Crucially this does NOT influence the lease `state`: a live server stays
/// `attached` for control-liveness purposes regardless of UI presence. Only the
/// human-facing presence differs — a live foreground `opencode attach` TUI is a
/// foreground terminal, while unattached/persistent servers are background and
/// reattachable. Legacy state files (empty launch_mode) report no presence.
fn opencode_ui_presence(
    lease_state: &str,
    obs: Option<&OpenCodeServerObservation>,
) -> Option<&'static str> {
    match lease_state {
        "detached" => return Some("detached"),
        "degraded" => return Some("degraded"),
        _ => {}
    }

    let obs = obs?;
    match obs.launch_mode.trim() {
        "attached_tui" if obs.has_tui_attachment => Some("foreground_tui"),
        "attached_tui" => Some("background"),
        "keep_server" | "detached" => Some("background"),
        _ => None,
    }
}

fn resolved_managed_codex_session(
    lease: &ManagedSessionLease,
    obs: Option<&CodexBridgeObservation>,
) -> ResolvedLocalSession {
    let cwd = obs.and_then(|obs| obs.cwd.clone());
    let provider_session_id = obs.and_then(|obs| obs.thread_id.clone());
    let thread_path = obs.and_then(|obs| obs.thread_path.clone());
    let process_pid = obs.and_then(|obs| obs.app_server_pid);
    let mut join_keys = vec![format!("session_id={}", lease.session_id)];
    if let Some(provider_session_id) = provider_session_id.as_deref() {
        join_keys.push(format!("provider_session_id={provider_session_id}"));
    }
    if let Some(thread_path) = thread_path.as_deref() {
        join_keys.push(format!("thread_path={thread_path}"));
    }
    if let Some(pid) = process_pid {
        join_keys.push(format!("app_server_pid={pid}"));
    }

    ResolvedLocalSession {
        session_id: Some(lease.session_id.clone()),
        provider: lease.provider.clone(),
        provider_session_id,
        control_path: "managed".to_string(),
        state: lease.state.clone(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: None,
        timeline_title: None,
        first_user_message: None,
        title_state: None,
        title_source: None,
        workspace: workspace_from_cwd(cwd),
        process: ResolvedProcess {
            pid: process_pid,
            process_start_time: obs
                .and_then(|obs| obs.app_server_process_start_time.as_deref())
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string),
            boot_id: None,
            started_at: None,
        },
        bridge: ResolvedBridge {
            bridge_pid: obs.map(|obs| obs.bridge_pid),
            app_server_pid: process_pid,
            ws_url: obs.and_then(|obs| obs.ws_url.clone()),
            heartbeat_at: obs.map(|obs| obs.updated_at.clone()),
            status: obs.map(|obs| obs.status.clone()),
            thread_subscription_status: obs.and_then(|obs| obs.thread_subscription_status.clone()),
            launch_mode: obs.and_then(|obs| obs.launch_mode.clone()),
            ui_attached: obs.map(|obs| obs.has_tui_attachment),
            ui_presence: codex_ui_presence(&lease.state, obs).map(str::to_string),
        },
        evidence: ResolvedEvidence {
            process_observed: obs.is_some_and(|obs| obs.app_server_alive || obs.has_tui_attachment),
            transcript_observed: thread_path.is_some(),
            bridge_state: obs.map(|obs| obs.status.clone()),
            hook_seen_at: None,
            join_keys,
        },
        reason_codes: Vec::new(),
    }
}

fn codex_ui_presence(
    lease_state: &str,
    obs: Option<&CodexBridgeObservation>,
) -> Option<&'static str> {
    match lease_state {
        "detached" => return Some("detached"),
        "degraded" => return Some("degraded"),
        _ => {}
    }

    let obs = obs?;
    match obs.launch_mode.as_deref().map(str::trim) {
        Some("tui") if obs.has_tui_attachment => Some("foreground_tui"),
        Some("detached_ui") if lease_state == "attached" => Some("background"),
        _ => None,
    }
}

fn claude_ui_presence(
    lease_state: &str,
    obs: Option<&ClaudeChannelObservation>,
) -> Option<&'static str> {
    match lease_state {
        "detached" => return Some("detached"),
        "degraded" => return Some("degraded"),
        _ => {}
    }

    // Claude has no bridge-owned launch_mode; foreground-ness is read from the
    // live process's controlling terminal via the process scan.
    let obs = obs?;
    if obs.claude_foreground_tui {
        Some("foreground_tui")
    } else if obs.claude_alive {
        Some("background")
    } else {
        None
    }
}

fn resolved_managed_cursor_session(
    lease: &ManagedSessionLease,
    obs: Option<&CursorHelmObservation>,
) -> ResolvedLocalSession {
    // Cursor Helm is terminal-owned: the launcher holds the PTY master and the
    // control socket. Project workspace + UI presence from that observation so
    // local-health / menu bar rows match Codex/Claude/OpenCode shape.
    let cwd = obs.and_then(|obs| obs.cwd.clone());
    let launcher_pid = obs.and_then(|obs| obs.launcher_pid);
    let cursor_pid = obs.and_then(|obs| obs.cursor_pid);
    let mut join_keys = vec![format!("session_id={}", lease.session_id)];
    if let Some(pid) = launcher_pid {
        join_keys.push(format!("launcher_pid={pid}"));
    }
    if let Some(pid) = cursor_pid {
        join_keys.push(format!("cursor_pid={pid}"));
    }
    if let Some(state_file) = obs.map(|obs| obs.state_file.display().to_string()) {
        if !state_file.trim().is_empty() {
            join_keys.push(format!("state_file={state_file}"));
        }
    }

    ResolvedLocalSession {
        session_id: Some(lease.session_id.clone()),
        provider: lease.provider.clone(),
        provider_session_id: None,
        control_path: "managed".to_string(),
        state: lease.state.clone(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: None,
        timeline_title: None,
        first_user_message: None,
        title_state: None,
        title_source: None,
        workspace: workspace_from_cwd(cwd),
        process: ResolvedProcess {
            pid: cursor_pid,
            process_start_time: obs
                .and_then(|obs| obs.cursor_process_start_time.as_deref())
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .map(str::to_string),
            boot_id: None,
            started_at: obs
                .map(|obs| obs.started_at.clone())
                .filter(|value| !value.trim().is_empty()),
        },
        bridge: ResolvedBridge {
            bridge_pid: launcher_pid,
            app_server_pid: cursor_pid,
            ws_url: None,
            heartbeat_at: obs
                .map(|obs| obs.updated_at.clone())
                .filter(|value| !value.trim().is_empty()),
            status: lease.bridge_status.clone(),
            thread_subscription_status: None,
            launch_mode: obs.map(|_| "tui".to_string()),
            ui_attached: obs.map(|obs| obs.live),
            ui_presence: cursor_ui_presence(&lease.state, obs).map(str::to_string),
        },
        evidence: ResolvedEvidence {
            process_observed: obs.is_some_and(|obs| obs.live && obs.cursor_pid.is_some()),
            transcript_observed: false,
            bridge_state: lease.bridge_status.clone(),
            hook_seen_at: None,
            join_keys,
        },
        reason_codes: Vec::new(),
    }
}

fn cursor_ui_presence(
    lease_state: &str,
    obs: Option<&CursorHelmObservation>,
) -> Option<&'static str> {
    match lease_state {
        "detached" => return Some("detached"),
        "degraded" => return Some("degraded"),
        _ => {}
    }

    // Live Helm = launcher pid alive + control socket present. That is the
    // terminal-attached control path; there is no detached_ui Cursor mode yet.
    let obs = obs?;
    if obs.live {
        Some("foreground_tui")
    } else {
        None
    }
}

fn resolved_managed_claude_session(
    lease: &ManagedSessionLease,
    obs: Option<&ClaudeChannelObservation>,
) -> ResolvedLocalSession {
    let provider_session_id = obs.and_then(|obs| obs.provider_session_id.clone());
    let cwd = obs.and_then(|obs| obs.cwd.clone());
    let process_pid = obs.and_then(|obs| obs.claude_pid);
    let mut join_keys = vec![format!("session_id={}", lease.session_id)];
    if let Some(provider_session_id) = provider_session_id.as_deref() {
        join_keys.push(format!("provider_session_id={provider_session_id}"));
    }
    if let Some(pid) = process_pid {
        join_keys.push(format!("claude_pid={pid}"));
    }
    let transcript_observed = provider_session_id.is_some();

    ResolvedLocalSession {
        session_id: Some(lease.session_id.clone()),
        provider: lease.provider.clone(),
        provider_session_id,
        control_path: "managed".to_string(),
        state: lease.state.clone(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: None,
        timeline_title: None,
        first_user_message: None,
        title_state: None,
        title_source: None,
        workspace: workspace_from_cwd(cwd),
        process: ResolvedProcess {
            pid: process_pid,
            // Claude's state-file `started_at` is also the identity boundary
            // the scanner validates against before considering this PID live.
            process_start_time: obs
                .map(|obs| obs.started_at.trim())
                .filter(|value| !value.is_empty())
                .map(str::to_string),
            boot_id: None,
            started_at: None,
        },
        bridge: ResolvedBridge {
            bridge_pid: obs.and_then(|obs| obs.bridge_pid),
            app_server_pid: None,
            ws_url: None,
            heartbeat_at: obs.map(|obs| obs.updated_at.clone()),
            status: lease.bridge_status.clone(),
            thread_subscription_status: None,
            launch_mode: None,
            ui_attached: None,
            ui_presence: claude_ui_presence(&lease.state, obs).map(str::to_string),
        },
        evidence: ResolvedEvidence {
            process_observed: obs.is_some_and(|obs| obs.claude_alive),
            transcript_observed,
            bridge_state: lease.bridge_status.clone(),
            hook_seen_at: None,
            join_keys,
        },
        reason_codes: Vec::new(),
    }
}

fn resolved_managed_generic_session(lease: &ManagedSessionLease) -> ResolvedLocalSession {
    ResolvedLocalSession {
        session_id: Some(lease.session_id.clone()),
        provider: lease.provider.clone(),
        provider_session_id: None,
        control_path: "managed".to_string(),
        state: lease.state.clone(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: None,
        timeline_title: None,
        first_user_message: None,
        title_state: None,
        title_source: None,
        workspace: ResolvedWorkspace::default(),
        process: ResolvedProcess::default(),
        bridge: ResolvedBridge::default(),
        evidence: ResolvedEvidence {
            process_observed: false,
            transcript_observed: false,
            bridge_state: lease.bridge_status.clone(),
            hook_seen_at: None,
            join_keys: vec![format!("session_id={}", lease.session_id)],
        },
        reason_codes: Vec::new(),
    }
}

fn resolved_unmanaged_session(binding: &UnmanagedSessionBinding) -> ResolvedLocalSession {
    let mut join_keys = vec![format!(
        "provider_session_id={}",
        binding.provider_session_id
    )];
    if let Some(source_path) = binding.source_path.as_deref() {
        join_keys.push(format!("source_path={source_path}"));
    }
    if let Some(pid) = binding.pid {
        join_keys.push(format!("pid={pid}"));
    }

    ResolvedLocalSession {
        session_id: None,
        provider: binding.provider.clone(),
        provider_session_id: Some(binding.provider_session_id.clone()),
        control_path: "unmanaged".to_string(),
        state: "unmanaged".to_string(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: Some(binding.observed_at.clone()),
        timeline_title: None,
        first_user_message: None,
        title_state: None,
        title_source: None,
        workspace: workspace_from_cwd(binding.cwd.clone()),
        process: ResolvedProcess {
            pid: binding.pid,
            process_start_time: binding.process_start_time.clone(),
            boot_id: None,
            started_at: binding.process_start_time.clone(),
        },
        bridge: ResolvedBridge::default(),
        evidence: ResolvedEvidence {
            process_observed: binding.pid.is_some(),
            transcript_observed: binding.source_path.is_some(),
            bridge_state: None,
            hook_seen_at: Some(binding.observed_at.clone()),
            join_keys,
        },
        reason_codes: Vec::new(),
    }
}

fn workspace_from_cwd(cwd: Option<String>) -> ResolvedWorkspace {
    let label = cwd.as_deref().and_then(|path| {
        Path::new(path)
            .file_name()
            .and_then(|name| name.to_str())
            .map(str::to_string)
    });
    ResolvedWorkspace {
        cwd,
        label,
        branch: None,
    }
}

#[derive(Default)]
struct ManagedCodexKeys {
    provider_session_ids: HashSet<String>,
    source_paths: HashSet<String>,
    pids: HashSet<u32>,
    process_group_ids: HashSet<i32>,
}

impl ManagedCodexKeys {
    fn from_observations(observations: &[CodexBridgeObservation]) -> Self {
        let mut keys = Self::default();
        for obs in observations {
            if codex_bridge_observation_is_stopped(obs)
                || !(obs.bridge_alive || obs.app_server_alive || obs.has_tui_attachment)
            {
                continue;
            }
            if let Some(thread_id) = normalized_string(obs.thread_id.as_deref()) {
                keys.provider_session_ids.insert(thread_id);
            }
            if let Some(thread_path) = normalized_path_string(obs.thread_path.as_deref()) {
                keys.source_paths.insert(thread_path);
            }
            if let Some(pid) = obs.app_server_pid {
                keys.pids.insert(pid);
            }
            if let Some(pgid) = obs.app_server_pgid.filter(|pgid| *pgid > 0) {
                keys.process_group_ids.insert(pgid);
            }
        }
        keys
    }
}

#[derive(Default)]
struct ManagedClaudeKeys {
    provider_session_ids: HashSet<String>,
    pids: HashSet<u32>,
}

impl ManagedClaudeKeys {
    fn from_observations(observations: &[ClaudeChannelObservation]) -> Self {
        let mut keys = Self::default();
        for obs in observations {
            if !(obs.claude_alive || obs.bridge_alive) {
                continue;
            }
            if let Some(provider_session_id) = normalized_string(obs.provider_session_id.as_deref())
            {
                keys.provider_session_ids.insert(provider_session_id);
            }
            if let Some(pid) = obs.claude_pid {
                keys.pids.insert(pid);
            }
            if let Some(pid) = obs.bridge_pid {
                keys.pids.insert(pid);
            }
        }
        keys
    }
}

fn binding_owned_by_codex(binding: &UnmanagedSessionBinding, keys: &ManagedCodexKeys) -> bool {
    if !binding.provider.eq_ignore_ascii_case("codex") {
        return false;
    }
    if keys
        .provider_session_ids
        .contains(binding.provider_session_id.trim())
    {
        return true;
    }
    if binding.pid.is_some_and(|pid| keys.pids.contains(&pid)) {
        return true;
    }
    if binding
        .pid
        .and_then(current_process_group_id)
        .is_some_and(|pgid| keys.process_group_ids.contains(&pgid))
    {
        return true;
    }
    binding
        .source_path
        .as_deref()
        .and_then(|source_path| normalized_path_string(Some(source_path)))
        .is_some_and(|source_path| keys.source_paths.contains(&source_path))
}

#[cfg(unix)]
fn current_process_group_id(pid: u32) -> Option<i32> {
    let pid = i32::try_from(pid).ok()?;
    let pgid = unsafe { libc::getpgid(pid) };
    if pgid > 0 {
        Some(pgid)
    } else {
        None
    }
}

#[cfg(not(unix))]
fn current_process_group_id(_pid: u32) -> Option<i32> {
    None
}

fn binding_owned_by_claude(binding: &UnmanagedSessionBinding, keys: &ManagedClaudeKeys) -> bool {
    if !binding.provider.eq_ignore_ascii_case("claude") {
        return false;
    }
    if keys
        .provider_session_ids
        .contains(binding.provider_session_id.trim())
    {
        return true;
    }
    binding.pid.is_some_and(|pid| keys.pids.contains(&pid))
}

fn normalized_string(value: Option<&str>) -> Option<String> {
    let trimmed = value?.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn normalized_path_string(value: Option<&str>) -> Option<String> {
    normalized_string(value).map(|path| Path::new(&path).to_string_lossy().to_string())
}

trait EmptyStringFallback {
    fn if_empty(self, fallback: String) -> String;
}

impl EmptyStringFallback for String {
    fn if_empty(self, fallback: String) -> String {
        if self.trim().is_empty() {
            fallback
        } else {
            self
        }
    }
}

/// Send heartbeat to server via the existing authenticated client.
#[tracing::instrument(
    level = "info",
    name = "engine.heartbeat.send",
    skip(client, payload),
    fields(
        otel.kind = "client",
        http.request.method = "POST",
        http.route = "/api/agents/heartbeat",
        longhouse.spool_pending_count = payload.spool_pending_count as u64,
        longhouse.spool_dead_count = payload.spool_dead_count as u64,
        longhouse.ship_attempts_1h = payload.ship_attempts_1h as u64,
    )
)]
pub async fn send_heartbeat(client: &ShipperClient, payload: &HeartbeatPayload) -> Result<()> {
    let json = serde_json::to_vec(payload)?;
    client
        .post_json_with_timeout("/api/agents/heartbeat", json, Some(HEARTBEAT_POST_TIMEOUT))
        .await
}

/// Result of the caller's attempt to read fresh phase-ledger rows. Serializes
/// to `"ok"` / `"read_failed: <err>"` so verify-runtime-truth can tell a
/// genuinely empty ledger apart from a ledger read that threw on emit.
#[derive(Debug, Clone)]
pub enum PhaseLedgerStatus {
    Ok,
    ReadFailed(String),
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct ProjectionReconciliation {
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
}

impl ProjectionReconciliation {
    pub fn idle() -> Self {
        Self {
            state: "idle".to_string(),
            reason: None,
            started_at: None,
        }
    }

    pub fn running(reason: impl Into<String>, started_at: impl Into<String>) -> Self {
        Self {
            state: "reconciling".to_string(),
            reason: Some(reason.into()),
            started_at: Some(started_at.into()),
        }
    }

    pub fn failed(reason: impl Into<String>) -> Self {
        Self {
            state: "failed".to_string(),
            reason: Some(reason.into()),
            started_at: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct StatusFileProjection {
    pub payload: HeartbeatPayload,
    recent_dead_letters: Vec<StatusDeadLetter>,
    phase_ledger: Vec<PhaseLedgerRow>,
    phase_ledger_status: PhaseLedgerStatus,
    generated_at: String,
    last_reconciled_at: String,
}

pub fn build_status_file_projection(
    payload: HeartbeatPayload,
    stats: &HeartbeatStats<'_>,
    phase_ledger: Vec<PhaseLedgerRow>,
    phase_ledger_status: PhaseLedgerStatus,
) -> StatusFileProjection {
    let recent_dead_letters = stats
        .spool
        .recent_dead(5)
        .unwrap_or_default()
        .into_iter()
        .map(status_dead_letter_from_entry)
        .collect();
    let generated_at = chrono::Utc::now().to_rfc3339();
    StatusFileProjection {
        payload,
        recent_dead_letters,
        phase_ledger,
        phase_ledger_status,
        generated_at: generated_at.clone(),
        last_reconciled_at: generated_at,
    }
}

impl StatusFileProjection {
    pub fn set_last_reconciled_at(&mut self, value: Option<String>) {
        if let Some(value) = value.filter(|value| !value.trim().is_empty()) {
            self.last_reconciled_at = value;
        }
    }
}

impl Serialize for PhaseLedgerStatus {
    fn serialize<S: serde::Serializer>(&self, ser: S) -> Result<S::Ok, S::Error> {
        match self {
            PhaseLedgerStatus::Ok => ser.serialize_str("ok"),
            PhaseLedgerStatus::ReadFailed(msg) => ser.serialize_str(&format!("read_failed: {msg}")),
        }
    }
}

/// Write status to `~/.longhouse/agent/engine-status.json`.
///
/// `phase_ledger` is passed in explicitly (not pulled from a store) so
/// callers can't accidentally emit an empty ledger by forgetting to wire
/// the DB — the absence of fresh rows and the absence of a reader look
/// identical otherwise. `ledger_status` encodes whether the vec is empty
/// because there are no fresh rows or because the read threw, so consumers
/// can surface the distinction. Compute both with
/// `SessionPhaseStore::new(conn).fresh_rows(now)` at the call site.
pub fn write_status_file(
    projection: &StatusFileProjection,
    control_channel: Option<serde_json::Value>,
    reconciliation: &ProjectionReconciliation,
    status_path: &std::path::Path,
) {
    #[derive(Serialize)]
    struct LocalProjectionFile<'a> {
        #[serde(skip_serializing_if = "Option::is_none")]
        version: Option<u64>,
        generated_at: &'a str,
        engine_pulse_at: &'a str,
        last_reconciled_at: &'a str,
        reconciliation: &'a ProjectionReconciliation,
    }

    #[derive(Serialize)]
    struct StatusFile<'a> {
        #[serde(flatten)]
        payload: &'a HeartbeatPayload,
        local_projection: LocalProjectionFile<'a>,
        /// Build identity compiled into the currently-running engine binary.
        /// Compare this against the on-disk engine binary via `binary_mtime`
        /// (see below) to detect "daemon needs restart" after an
        /// `make install-engine`.
        build: BuildIdentity,
        /// Path of the engine binary the daemon started from (std::env::current_exe).
        /// Stat this path on the reader side and compare mtime against
        /// `daemon_started_at` to detect whether the binary on disk is newer
        /// than the in-memory daemon.
        #[serde(skip_serializing_if = "Option::is_none")]
        binary_path: Option<String>,
        /// Modification time of the binary at the path above, ISO 8601. Captured
        /// fresh on each write. If `binary_mtime > daemon_started_at` the daemon
        /// is running a stale binary and a restart is pending.
        #[serde(skip_serializing_if = "Option::is_none")]
        binary_mtime: Option<String>,
        /// Start time of the current daemon process, ISO 8601. Captured once
        /// at process startup.
        daemon_started_at: String,
        recent_dead_letters: Vec<StatusDeadLetter>,
        /// Ledger rows whose phase is still within its freshness window.
        /// Same LWW rows that back `session_phase_state` so consumers can
        /// read this file instead of re-opening the SQLite ledger.
        phase_ledger: Vec<PhaseLedgerRow>,
        /// `"ok"` or `"read_failed: ..."`. Lets readers tell an empty-but-
        /// intentional ledger apart from a ledger that the engine couldn't
        /// read this tick.
        phase_ledger_status: PhaseLedgerStatus,
        /// Machine Agent live-control WebSocket status. This is separate from
        /// durable HTTPS shipping health; event shipping can be healthy while
        /// remote launch/control is unavailable.
        #[serde(skip_serializing_if = "Option::is_none")]
        control_channel: Option<serde_json::Value>,
        last_updated: String,
    }

    let now_utc = chrono::Utc::now();
    let now = now_utc.to_rfc3339();
    let daemon_started_at = DAEMON_STARTED_AT.get_or_init(|| now.clone()).clone();
    let (binary_path, binary_mtime) = inspect_current_exe();
    let status = StatusFile {
        payload: &projection.payload,
        local_projection: LocalProjectionFile {
            version: projection.payload.sessions_sequence,
            generated_at: &projection.generated_at,
            engine_pulse_at: &now,
            last_reconciled_at: &projection.last_reconciled_at,
            reconciliation,
        },
        build: BuildIdentity::current(),
        binary_path,
        binary_mtime,
        daemon_started_at,
        recent_dead_letters: projection.recent_dead_letters.clone(),
        phase_ledger: projection.phase_ledger.clone(),
        phase_ledger_status: projection.phase_ledger_status.clone(),
        control_channel,
        last_updated: now.clone(),
    };

    if let Some(parent) = status_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    // Atomic replace via tmp+rename so a concurrent reader never sees a
    // half-written file. Readers would otherwise hit JSONDecodeError and
    // silently drop phase_ledger from their cross-check.
    if let Ok(json) = serde_json::to_string_pretty(&status) {
        let tmp_path = status_path.with_extension("json.tmp");
        if std::fs::write(&tmp_path, json).is_ok() {
            let _ = std::fs::rename(&tmp_path, status_path);
        }
    }
}

/// Keep a prior coherent projection visibly alive while startup or wake
/// reconciliation is still rebuilding its evidence. This intentionally
/// changes only pulse/process metadata and reconciliation state; evidence and
/// `generated_at` remain the last accepted snapshot.
pub fn refresh_existing_status_pulse(
    reconciliation: &ProjectionReconciliation,
    status_path: &std::path::Path,
) {
    let Ok(bytes) = std::fs::read(status_path) else {
        return;
    };
    let Ok(mut status) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
        return;
    };
    let now = chrono::Utc::now().to_rfc3339();
    status["daemon_pid"] = serde_json::json!(std::process::id());
    status["last_updated"] = serde_json::json!(now);
    status["local_projection"]["engine_pulse_at"] = serde_json::json!(now);
    status["local_projection"]["reconciliation"] =
        serde_json::to_value(reconciliation).unwrap_or(serde_json::Value::Null);

    if let Ok(json) = serde_json::to_string_pretty(&status) {
        let tmp_path = status_path.with_extension("json.tmp");
        if std::fs::write(&tmp_path, json).is_ok() {
            let _ = std::fs::rename(&tmp_path, status_path);
        }
    }
}

fn inspect_current_exe() -> (Option<String>, Option<String>) {
    // Returns (binary_path, binary_mtime_iso8601). Both cheap filesystem
    // operations; OK to run per write. If either fails (e.g. the binary was
    // deleted between invocations), we return None and consumers can skip the
    // restart-pending check for this tick.
    let exe = match std::env::current_exe() {
        Ok(path) => path,
        Err(_) => return (None, None),
    };
    let exe_path = exe.to_string_lossy().into_owned();
    let mtime = std::fs::metadata(&exe)
        .and_then(|md| md.modified())
        .ok()
        .and_then(|st| {
            chrono::DateTime::<chrono::Utc>::from(st)
                .to_rfc3339()
                .into()
        });
    (Some(exe_path), mtime)
}

fn status_dead_letter_from_entry(entry: DeadLetterEntry) -> StatusDeadLetter {
    StatusDeadLetter {
        provider: entry.provider,
        file_path: entry.file_path,
        start_offset: entry.start_offset,
        end_offset: entry.end_offset,
        range_bytes: entry.end_offset.saturating_sub(entry.start_offset),
        session_id: entry.session_id,
        last_error: entry.last_error,
        created_at: entry.created_at,
    }
}

/// Get free bytes on the filesystem containing Longhouse agent state.
fn get_disk_free() -> u64 {
    config::get_agent_dir()
        .map(|agent_dir| disk_free_bytes(&agent_dir))
        .unwrap_or(0)
}

#[cfg(unix)]
fn disk_free_bytes(path: &std::path::Path) -> u64 {
    use std::ffi::CString;
    use std::mem::MaybeUninit;

    let path_str = match CString::new(path.to_string_lossy().as_bytes()) {
        Ok(s) => s,
        Err(_) => return 0,
    };

    unsafe {
        let mut stat: MaybeUninit<libc::statvfs> = MaybeUninit::uninit();
        if libc::statvfs(path_str.as_ptr(), stat.as_mut_ptr()) == 0 {
            let s = stat.assume_init();
            (s.f_bavail as u64) * (s.f_frsize as u64)
        } else {
            0
        }
    }
}

#[cfg(not(unix))]
fn disk_free_bytes(_path: &std::path::Path) -> u64 {
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;
    use std::path::PathBuf;

    #[test]
    fn test_heartbeat_payload_fields() {
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 12345,
            last_ship_at: Some("2026-02-18T10:00:00Z".to_string()),
            last_ship_attempt_at: Some("2026-02-18T10:00:01Z".to_string()),
            last_ship_result: Some("ok".to_string()),
            last_ship_latency_ms: Some(123),
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 5,
            spool_dead_count: 1,
            archive_backlog: ArchiveBacklogSnapshot::default(),
            storage_v2_outbox: StorageV2OutboxSnapshot::default(),
            parse_error_count_1h: 0,
            consecutive_ship_failures: 2,
            ship_attempts_1h: 7,
            ship_successes_1h: 5,
            ship_rate_limited_1h: 1,
            ship_server_errors_1h: 1,
            ship_payload_rejections_1h: 0,
            ship_payload_too_large_1h: 0,
            ship_retryable_client_errors_1h: 0,
            ship_connect_errors_1h: 1,
            ship_latency_p50_ms_1h: Some(123),
            ship_latency_p95_ms_1h: Some(250),
            ship_attempts_10m: 4,
            ship_successes_10m: 3,
            ship_rate_limited_10m: 0,
            ship_server_errors_10m: 1,
            ship_retryable_client_errors_10m: 0,
            ship_connect_errors_10m: 0,
            ship_lanes: ShipLaneSummarySet::default(),
            events_per_sec_ewma_10s: None,
            bytes_per_sec_ewma_10s: None,
            disk_free_bytes: 1_000_000_000,
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
        };

        // Must serialize correctly
        let json = serde_json::to_string(&payload).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed["version"], "0.1.0");
        assert_eq!(parsed["daemon_pid"], 12345);
        assert_eq!(parsed["spool_pending_count"], 5);
        assert_eq!(parsed["spool_dead_count"], 1);
        assert_eq!(parsed["consecutive_ship_failures"], 2);
        assert_eq!(parsed["ship_attempts_1h"], 7);
        assert_eq!(parsed["ship_successes_1h"], 5);
        assert_eq!(parsed["ship_attempts_10m"], 4);
        assert_eq!(parsed["ship_successes_10m"], 3);
        assert_eq!(parsed["is_offline"], false);
        assert_eq!(parsed["managed_sessions"], serde_json::json!([]));
        assert_eq!(parsed["unmanaged_session_bindings"], serde_json::json!([]));
        assert!(parsed["last_ship_at"].is_string());
        assert!(parsed["last_ship_attempt_at"].is_string());
        assert_eq!(parsed["last_ship_result"], "ok");
    }

    #[test]
    fn test_heartbeat_payload_includes_managed_session_leases() {
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 12345,
            last_ship_at: None,
            last_ship_attempt_at: None,
            last_ship_result: None,
            last_ship_latency_ms: None,
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 0,
            spool_dead_count: 0,
            archive_backlog: ArchiveBacklogSnapshot::default(),
            storage_v2_outbox: StorageV2OutboxSnapshot::default(),
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
            ship_lanes: ShipLaneSummarySet::default(),
            events_per_sec_ewma_10s: None,
            bytes_per_sec_ewma_10s: None,
            disk_free_bytes: 0,
            is_offline: false,
            managed_sessions: vec![ManagedSessionLease {
                session_id: "7474a2a1-ab9f-4a10-9726-898e895fedf0".to_string(),
                provider: "codex".to_string(),
                machine_id: "cinder".to_string(),
                sequence: 42,
                state: "attached".to_string(),
                phase: Some("idle".to_string()),
                tool_name: None,
                bridge_status: Some("ready".to_string()),
                thread_subscription_status: Some("subscribed".to_string()),
                observed_at: "2026-04-26T00:00:00Z".to_string(),
                lease_ttl_ms: 900_000,
            }],
            unmanaged_session_bindings: Vec::new(),
            machine_evidence: None,
            sessions: Vec::new(),
            sessions_digest: None,
            sessions_sequence: None,
            adaptive_backlog_limiter: None,
            ship_scheduler: None,
            history_import: Default::default(),
        };

        let json = serde_json::to_string(&payload).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed["managed_sessions"][0]["provider"], "codex");
        assert_eq!(parsed["managed_sessions"][0]["state"], "attached");
        assert!(parsed["managed_sessions"][0].get("phase").is_none());
        assert!(parsed["managed_sessions"][0].get("tool_name").is_none());
        assert_eq!(parsed["managed_sessions"][0]["lease_ttl_ms"], 900_000);
    }

    fn test_observation(session_id: &str, ws_url: &str) -> CodexBridgeObservation {
        CodexBridgeObservation {
            session_id: session_id.to_string(),
            run_id: Some(format!("run-{session_id}")),
            connection_id: Some(format!("connection-{session_id}")),
            lease_generation: Some(format!("lease-{session_id}")),
            state_file: PathBuf::from(format!("/tmp/{session_id}.json")),
            schema_version: crate::codex_bridge::BRIDGE_STATE_SCHEMA_VERSION,
            cwd: Some("/tmp/cwd".to_string()),
            launch_mode: Some("tui".to_string()),
            ws_url: Some(ws_url.to_string()),
            status: "ready".to_string(),
            thread_id: Some("thread-1".to_string()),
            thread_path: None,
            active_turn_id: None,
            last_turn_status: Some("completed".to_string()),
            last_error: None,
            thread_subscription_status: Some("subscribed".to_string()),
            bridge_pid: 12344,
            bridge_process_start_time: Some("Sat Apr 26 00:00:00 2026".to_string()),
            app_server_pid: Some(12345),
            app_server_process_start_time: Some("Sat Apr 26 00:00:00 2026".to_string()),
            app_server_pgid: Some(12345),
            updated_at: "2026-04-26T00:00:00Z".to_string(),
            bridge_alive: true,
            has_tui_attachment: true,
            app_server_alive: true,
        }
    }

    #[test]
    fn leases_from_observations_do_not_overlay_activity_on_control() {
        use crate::state::session_phase::{SessionPhaseSignal, SessionPhaseStore};

        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let session_id = "7474a2a1-ab9f-4a10-9726-898e895fedf0";
        let now = Utc::now();
        SessionPhaseStore::new(&conn)
            .record(&SessionPhaseSignal {
                session_id: session_id.to_string(),
                provider: "codex".to_string(),
                phase: "running".to_string(),
                tool_name: Some("Shell".to_string()),
                source: "codex_bridge".to_string(),
                observed_at: now,
            })
            .unwrap();

        let obs = test_observation(session_id, "ws://127.0.0.1:45678/session");
        let leases = leases_from_observations("cinder", &[obs], now);

        assert_eq!(leases.len(), 1);
        let lease = &leases[0];
        assert_eq!(lease.session_id, session_id);
        assert_eq!(lease.state, "attached");
        assert_eq!(lease.phase, None);
        assert_eq!(lease.tool_name, None);
    }

    #[test]
    fn leases_from_observations_classifies_detached_ui_ready_bridge_as_attached() {
        let now = Utc::now();

        let mut obs = test_observation(
            "59612f92-0e4c-4031-b236-c4091f13da40",
            "ws://127.0.0.1:45679/session",
        );
        obs.launch_mode = Some("detached_ui".to_string());
        obs.has_tui_attachment = false;
        obs.app_server_alive = true;
        obs.thread_id = Some("thread-detached-ui".to_string());

        let leases = leases_from_observations("cinder", &[obs], now);

        assert_eq!(leases.len(), 1);
        assert_eq!(leases[0].state, "attached");
        assert_eq!(leases[0].phase, None);
    }

    #[test]
    fn resolved_sessions_project_codex_ui_presence_without_changing_lease_state() {
        let now = Utc::now();

        let mut foreground = test_observation("foreground-codex", "ws://127.0.0.1:45681/session");
        foreground.launch_mode = Some("tui".to_string());
        foreground.has_tui_attachment = true;

        let mut background = test_observation("background-codex", "ws://127.0.0.1:45682/session");
        background.launch_mode = Some("detached_ui".to_string());
        background.has_tui_attachment = false;
        background.app_server_alive = true;
        background.thread_id = Some("thread-background".to_string());

        let observations = vec![foreground, background];
        let leases = leases_from_observations("cinder", &observations, now);
        let sessions =
            resolved_sessions_from_observations(&leases, &[], &observations, &[], &[], &[]);

        let foreground_session = sessions
            .iter()
            .find(|session| session.session_id.as_deref() == Some("foreground-codex"))
            .unwrap();
        assert_eq!(foreground_session.state, "attached");
        assert_eq!(
            foreground_session.bridge.launch_mode.as_deref(),
            Some("tui")
        );
        assert_eq!(foreground_session.bridge.ui_attached, Some(true));
        assert_eq!(
            foreground_session.bridge.ui_presence.as_deref(),
            Some("foreground_tui")
        );
        assert_eq!(
            foreground_session.process.process_start_time.as_deref(),
            Some("Sat Apr 26 00:00:00 2026")
        );

        let background_session = sessions
            .iter()
            .find(|session| session.session_id.as_deref() == Some("background-codex"))
            .unwrap();
        assert_eq!(background_session.state, "attached");
        assert_eq!(
            background_session.bridge.launch_mode.as_deref(),
            Some("detached_ui")
        );
        assert_eq!(background_session.bridge.ui_attached, Some(false));
        assert_eq!(
            background_session.bridge.ui_presence.as_deref(),
            Some("background")
        );
    }

    #[test]
    fn resolved_processes_carry_machine_boot_identity() {
        let lease = ManagedSessionLease {
            session_id: "managed-codex".to_string(),
            provider: "codex".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "attached".to_string(),
            phase: Some("thinking".to_string()),
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: None,
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            lease_ttl_ms: 900_000,
        };
        let observation = test_observation("managed-codex", "ws://127.0.0.1:45681/session");
        let mut sessions =
            resolved_sessions_from_observations(&[lease], &[], &[observation], &[], &[], &[]);

        apply_boot_identity(&mut sessions, Some("macos:1777970400:0"));

        assert_eq!(
            sessions[0].process.boot_id.as_deref(),
            Some("macos:1777970400:0")
        );
    }

    #[test]
    fn claude_ui_presence_maps_foreground_background_and_lease_states() {
        fn claude_obs(alive: bool, foreground: bool) -> ClaudeChannelObservation {
            ClaudeChannelObservation {
                session_id: "claude-presence".to_string(),
                run_id: None,
                connection_id: None,
                lease_generation: None,
                provider_session_id: Some("provider-claude".to_string()),
                state_file: PathBuf::from("/tmp/claude-presence.json"),
                cwd: None,
                claude_pid: Some(123),
                bridge_pid: Some(124),
                ready: true,
                started_at: "2026-05-07T20:03:50Z".to_string(),
                updated_at: "2026-05-07T20:03:50Z".to_string(),
                claude_alive: alive,
                bridge_alive: alive,
                claude_foreground_tui: foreground,
            }
        }

        // Foreground controlling terminal -> attached interactive TUI.
        assert_eq!(
            claude_ui_presence("attached", Some(&claude_obs(true, true))),
            Some("foreground_tui")
        );
        // Live but no foreground terminal -> background.
        assert_eq!(
            claude_ui_presence("attached", Some(&claude_obs(true, false))),
            Some("background")
        );
        // Not alive and no observation -> no presence projected.
        assert_eq!(
            claude_ui_presence("attached", Some(&claude_obs(false, false))),
            None
        );
        assert_eq!(claude_ui_presence("attached", None), None);
        // Lease-level states win over process signal.
        assert_eq!(
            claude_ui_presence("detached", Some(&claude_obs(true, true))),
            Some("detached")
        );
        assert_eq!(
            claude_ui_presence("degraded", Some(&claude_obs(true, true))),
            Some("degraded")
        );
    }

    fn test_opencode_observation(session_id: &str, launch_mode: &str) -> OpenCodeServerObservation {
        OpenCodeServerObservation {
            session_id: session_id.to_string(),
            run_id: Some(format!("run-{session_id}")),
            connection_id: Some(format!("connection-{session_id}")),
            lease_generation: Some(format!("lease-{session_id}")),
            provider_session_id: format!("provider-{session_id}"),
            state_file: PathBuf::from(format!("/tmp/{session_id}.json")),
            cwd: Some("/Users/test/git/acme".to_string()),
            server_url: Some("http://127.0.0.1:12345".to_string()),
            pid: Some(9876),
            started_at: "2026-05-05T11:59:00Z".to_string(),
            updated_at: "2026-05-05T12:00:00Z".to_string(),
            server_alive: true,
            health_ready: true,
            has_tui_attachment: false,
            launch_mode: launch_mode.to_string(),
            owner_wrapper_pid: Some(9000),
            owner_wrapper_start_time: "Mon May  5 11:58:00 2026".to_string(),
            process_start_time: "Mon May  5 11:59:00 2026".to_string(),
        }
    }

    fn opencode_lease(session_id: &str) -> ManagedSessionLease {
        ManagedSessionLease {
            session_id: session_id.to_string(),
            provider: "opencode".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "attached".to_string(),
            phase: Some("idle".to_string()),
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: None,
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            lease_ttl_ms: 900_000,
        }
    }

    #[test]
    fn opencode_health_failure_keeps_managed_degraded_lease() {
        let mut observation = test_opencode_observation("managed-opencode", "keep_server");
        observation.health_ready = false;

        let leases =
            leases_from_opencode_server_observations("cinder", &[observation], chrono::Utc::now());

        assert_eq!(leases.len(), 1);
        assert_eq!(leases[0].state, "degraded");
        assert_eq!(
            leases[0].bridge_status.as_deref(),
            Some("health_unavailable")
        );
    }

    #[test]
    fn resolved_sessions_project_opencode_ui_presence_without_changing_lease_state() {
        // attached_tui needs live attach proof for foreground_tui; otherwise
        // the live server remains attached for control but background in UI.
        let lease = opencode_lease("managed-opencode");
        let mut obs = test_opencode_observation("managed-opencode", "attached_tui");
        let sessions = resolved_sessions_from_observations(
            std::slice::from_ref(&lease),
            &[],
            &[],
            &[],
            std::slice::from_ref(&obs),
            &[],
        );
        let session = &sessions[0];
        assert_eq!(session.state, "attached");
        assert_eq!(
            session.process.process_start_time.as_deref(),
            Some("Mon May  5 11:59:00 2026")
        );
        assert_eq!(session.bridge.ui_attached, Some(false));
        assert_eq!(session.bridge.ui_presence.as_deref(), Some("background"));

        obs.has_tui_attachment = true;
        let sessions = resolved_sessions_from_observations(&[lease], &[], &[], &[], &[obs], &[]);
        let session = &sessions[0];
        assert_eq!(session.state, "attached");
        assert_eq!(session.bridge.ui_attached, Some(true));
        assert_eq!(
            session.bridge.ui_presence.as_deref(),
            Some("foreground_tui")
        );

        // keep_server/detached -> background;
        // every live launch mode keeps lease state "attached" so control
        // liveness (send/interrupt) is never disabled by presence alone.
        let cases = [("keep_server", "background"), ("detached", "background")];
        for (launch_mode, expected_presence) in cases {
            let lease = opencode_lease("managed-opencode");
            let obs = test_opencode_observation("managed-opencode", launch_mode);
            let sessions =
                resolved_sessions_from_observations(&[lease], &[], &[], &[], &[obs], &[]);
            let session = &sessions[0];
            assert_eq!(session.state, "attached", "launch_mode={launch_mode}");
            assert_eq!(session.bridge.ui_attached, Some(false));
            assert_eq!(
                session.bridge.ui_presence.as_deref(),
                Some(expected_presence),
                "launch_mode={launch_mode}"
            );
            assert_eq!(
                session.bridge.launch_mode.as_deref(),
                Some(launch_mode),
                "launch_mode={launch_mode}"
            );
        }
    }

    #[test]
    fn resolved_sessions_opencode_legacy_state_has_no_ui_presence() {
        // Legacy (schema v1) state files carry no launch_mode: presence is
        // unknown rather than mislabeled, and lease state is untouched.
        let lease = opencode_lease("legacy-opencode");
        let obs = test_opencode_observation("legacy-opencode", "");
        let sessions = resolved_sessions_from_observations(&[lease], &[], &[], &[], &[obs], &[]);
        let session = &sessions[0];
        assert_eq!(session.state, "attached");
        assert_eq!(session.bridge.ui_presence, None);
        assert_eq!(session.bridge.launch_mode.as_deref(), Some("server_bridge"));
    }

    #[test]
    fn leases_from_observations_classifies_degraded_and_detached() {
        let now = Utc::now();

        let mut degraded = test_observation("degraded-session", "ws://127.0.0.1:45679/session");
        degraded.has_tui_attachment = false;
        degraded.thread_id = None; // alive but no TUI/detached-UI thread -> degraded
        let mut detached = test_observation("detached-session", "ws://127.0.0.1:45680/session");
        detached.bridge_alive = false; // lock not held → detached

        let leases = leases_from_observations("cinder", &[degraded, detached], now);

        assert_eq!(leases.len(), 2);
        let degraded_lease = leases
            .iter()
            .find(|l| l.session_id == "degraded-session")
            .unwrap();
        let detached_lease = leases
            .iter()
            .find(|l| l.session_id == "detached-session")
            .unwrap();
        assert_eq!(degraded_lease.state, "degraded");
        assert_eq!(detached_lease.state, "detached");
    }

    #[test]
    fn leases_from_observations_marks_provider_thread_switch_as_degraded() {
        let now = Utc::now();

        let mut obs = test_observation("provider-switch-session", "ws://127.0.0.1:45679/session");
        obs.thread_subscription_status = Some("provider_thread_switched".to_string());

        let leases = leases_from_observations("cinder", &[obs], now);

        assert_eq!(leases.len(), 1);
        assert_eq!(leases[0].state, "degraded");
        assert_eq!(
            leases[0].thread_subscription_status.as_deref(),
            Some("provider_thread_switched")
        );
        assert_eq!(leases[0].phase, None);
    }

    #[test]
    fn leases_from_observations_skips_stopped_codex_bridges() {
        let now = Utc::now();

        let mut stopped = test_observation("stopped-session", "ws://127.0.0.1:45681/session");
        stopped.status = "stopped".to_string();
        stopped.bridge_alive = false;
        stopped.has_tui_attachment = false;
        stopped.app_server_alive = false;

        let live = test_observation("live-session", "ws://127.0.0.1:45682/session");

        let leases = leases_from_observations("cinder", &[stopped, live], now);

        assert_eq!(leases.len(), 1);
        assert_eq!(leases[0].session_id, "live-session");
    }

    #[test]
    fn leases_from_claude_channel_observations_keep_activity_off_control() {
        use crate::managed_claude_scan::ClaudeChannelObservation;
        use crate::state::session_phase::{SessionPhaseSignal, SessionPhaseStore};

        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let session_id = "09b68f98-1e31-458e-b78a-6dfd062ead75";
        let now = Utc::now();
        SessionPhaseStore::new(&conn)
            .record(&SessionPhaseSignal {
                session_id: session_id.to_string(),
                provider: "claude".to_string(),
                phase: "needs_user".to_string(),
                tool_name: None,
                source: "claude_hook".to_string(),
                observed_at: now,
            })
            .unwrap();

        let live = ClaudeChannelObservation {
            session_id: session_id.to_string(),
            run_id: None,
            connection_id: None,
            lease_generation: None,
            provider_session_id: Some(session_id.to_string()),
            state_file: PathBuf::from("/tmp/live.json"),
            cwd: Some("/Users/test/git/acme".to_string()),
            claude_pid: Some(123),
            bridge_pid: Some(124),
            ready: true,
            started_at: "2026-05-07T20:03:50Z".to_string(),
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
            claude_foreground_tui: true,
        };
        let dead = ClaudeChannelObservation {
            session_id: "19b68f98-1e31-458e-b78a-6dfd062ead75".to_string(),
            run_id: None,
            connection_id: None,
            lease_generation: None,
            provider_session_id: None,
            state_file: PathBuf::from("/tmp/dead.json"),
            cwd: None,
            claude_pid: Some(223),
            bridge_pid: Some(224),
            ready: true,
            started_at: "2026-05-07T20:03:50Z".to_string(),
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: false,
            bridge_alive: false,
            claude_foreground_tui: false,
        };

        let leases = leases_from_claude_channel_observations("cinder", &[live, dead], now);

        assert_eq!(leases.len(), 1);
        let lease = leases
            .iter()
            .find(|lease| lease.session_id == session_id)
            .unwrap();
        assert_eq!(lease.session_id, session_id);
        assert_eq!(lease.provider, "claude");
        assert_eq!(lease.state, "attached");
        assert_eq!(lease.phase, None);
        assert_eq!(lease.bridge_status.as_deref(), Some("ready"));
        assert_eq!(lease.lease_ttl_ms, 900_000);
    }

    fn test_binding(
        provider: &str,
        provider_session_id: &str,
        pid: u32,
    ) -> UnmanagedSessionBinding {
        UnmanagedSessionBinding {
            machine_id: "cinder".to_string(),
            provider: provider.to_string(),
            provider_session_id: provider_session_id.to_string(),
            source_path: Some(format!("/tmp/{provider_session_id}.jsonl")),
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

    #[test]
    fn filters_unmanaged_codex_binding_owned_by_bridge_thread() {
        let mut obs = test_observation("managed-codex", "ws://127.0.0.1:45683/session");
        obs.thread_id = Some("thread-managed".to_string());
        obs.thread_path = Some("/tmp/thread-managed.jsonl".to_string());
        obs.has_tui_attachment = false;
        obs.app_server_alive = true;

        let mut by_path = test_binding("codex", "other-thread", 456);
        by_path.source_path = Some("/tmp/thread-managed.jsonl".to_string());
        let bindings = vec![
            test_binding("codex", "thread-managed", 123),
            by_path,
            test_binding("codex", "thread-unmanaged", 789),
        ];

        let filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            bindings,
            &[obs],
            &[],
            &[],
            &[],
        );

        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].provider_session_id, "thread-unmanaged");
    }

    #[test]
    fn keeps_codex_binding_when_matching_bridge_is_stopped_and_dead() {
        let mut obs = test_observation("stopped-codex", "ws://127.0.0.1:45684/session");
        obs.thread_id = Some("thread-stopped".to_string());
        obs.status = "stopped".to_string();
        obs.bridge_alive = false;
        obs.has_tui_attachment = false;
        obs.app_server_alive = false;

        let bindings = vec![test_binding("codex", "thread-stopped", 123)];

        let filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            bindings.clone(),
            &[obs],
            &[],
            &[],
            &[],
        );

        assert_eq!(filtered, bindings);
    }

    #[cfg(unix)]
    #[test]
    fn filters_unmanaged_codex_binding_owned_by_bridge_process_group() {
        let current_pid = std::process::id();
        let current_pgid = unsafe { libc::getpgid(i32::try_from(current_pid).unwrap()) };
        assert!(current_pgid > 0);

        let mut obs = test_observation("managed-codex", "ws://127.0.0.1:45684/session");
        obs.thread_id = None;
        obs.thread_path = None;
        obs.app_server_pid = None;
        obs.app_server_pgid = Some(current_pgid);
        obs.bridge_alive = false;
        obs.has_tui_attachment = false;
        obs.app_server_alive = true;

        let bindings = vec![
            test_binding("codex", "same-process-group", current_pid),
            test_binding("codex", "real-unmanaged", u32::MAX),
        ];

        let filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            bindings,
            &[obs],
            &[],
            &[],
            &[],
        );

        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].provider_session_id, "real-unmanaged");
    }

    #[cfg(unix)]
    #[test]
    fn keeps_unmanaged_codex_binding_when_process_group_does_not_match_bridge() {
        let current_pid = std::process::id();
        let current_pgid = unsafe { libc::getpgid(i32::try_from(current_pid).unwrap()) };
        assert!(current_pgid > 0);

        let mut obs = test_observation("managed-codex", "ws://127.0.0.1:45684/session");
        obs.thread_id = None;
        obs.thread_path = None;
        obs.app_server_pid = None;
        obs.app_server_pgid = Some(current_pgid + 1);
        obs.bridge_alive = false;
        obs.has_tui_attachment = false;
        obs.app_server_alive = true;

        let bindings = vec![test_binding("codex", "real-unmanaged", current_pid)];

        let filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            bindings.clone(),
            &[obs],
            &[],
            &[],
            &[],
        );

        assert_eq!(filtered, bindings);
    }

    #[test]
    fn filters_unmanaged_claude_binding_owned_by_channel() {
        use crate::managed_claude_scan::ClaudeChannelObservation;

        let obs = ClaudeChannelObservation {
            session_id: "managed-claude".to_string(),
            run_id: None,
            connection_id: None,
            lease_generation: None,
            provider_session_id: Some("claude-provider-session".to_string()),
            state_file: PathBuf::from("/tmp/managed-claude.json"),
            cwd: None,
            claude_pid: Some(321),
            bridge_pid: Some(322),
            ready: true,
            started_at: "2026-05-07T20:03:50Z".to_string(),
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
            claude_foreground_tui: true,
        };
        let bindings = vec![
            test_binding("claude", "claude-provider-session", 999),
            test_binding("claude", "other-claude-session", 321),
            test_binding("claude", "real-unmanaged", 987),
        ];

        let filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            bindings,
            &[],
            &[obs],
            &[],
            &[],
        );

        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].provider_session_id, "real-unmanaged");
    }

    #[test]
    fn filters_unmanaged_binding_owned_by_live_opencode_server_pid() {
        let observation = test_opencode_observation("managed-opencode", "keep_server");
        let bindings = vec![
            test_binding("opencode", "managed-provider-session", 9876),
            test_binding("opencode", "real-unmanaged", 7777),
        ];

        let filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            bindings,
            &[],
            &[],
            &[observation],
            &[],
        );

        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].provider_session_id, "real-unmanaged");
    }

    #[test]
    fn resolved_sessions_join_managed_codex_evidence_and_unmanaged_bindings() {
        let mut obs = test_observation("managed-codex", "ws://127.0.0.1:45685/session");
        obs.cwd = Some("/Users/test/git/zerg".to_string());
        obs.thread_id = Some("thread-managed".to_string());
        obs.thread_path = Some("/Users/test/.codex/sessions/thread-managed.jsonl".to_string());
        obs.bridge_pid = 111;
        obs.app_server_pid = Some(222);
        obs.app_server_alive = true;

        let lease = ManagedSessionLease {
            session_id: "managed-codex".to_string(),
            provider: "codex".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "attached".to_string(),
            phase: Some("thinking".to_string()),
            tool_name: Some("Bash".to_string()),
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: Some("subscribed".to_string()),
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            lease_ttl_ms: 900_000,
        };
        let unmanaged = test_binding("claude", "claude-unmanaged", 333);

        let sessions =
            resolved_sessions_from_observations(&[lease], &[unmanaged], &[obs], &[], &[], &[]);

        assert_eq!(sessions.len(), 2);
        let managed = sessions
            .iter()
            .find(|session| session.control_path == "managed")
            .unwrap();
        assert_eq!(managed.provider, "codex");
        assert_eq!(
            managed.provider_session_id.as_deref(),
            Some("thread-managed")
        );
        assert_eq!(managed.workspace.label.as_deref(), Some("zerg"));
        assert_eq!(
            managed.workspace.cwd.as_deref(),
            Some("/Users/test/git/zerg")
        );
        assert_eq!(managed.phase, None);
        assert_eq!(managed.tool_name, None);
        assert_eq!(managed.phase_observed_at, None);
        assert_eq!(managed.last_activity_at, None);
        assert_eq!(managed.bridge.bridge_pid, Some(111));
        assert_eq!(managed.bridge.app_server_pid, Some(222));
        assert_eq!(managed.bridge.status.as_deref(), Some("ready"));
        assert_eq!(
            managed.bridge.thread_subscription_status.as_deref(),
            Some("subscribed")
        );
        assert!(managed
            .evidence
            .join_keys
            .iter()
            .any(|key| key == "provider_session_id=thread-managed"));

        let unmanaged = sessions
            .iter()
            .find(|session| session.control_path == "unmanaged")
            .unwrap();
        assert_eq!(unmanaged.provider, "claude");
        assert_eq!(unmanaged.process.pid, Some(333));
        assert_eq!(
            unmanaged.provider_session_id.as_deref(),
            Some("claude-unmanaged")
        );
        assert_eq!(unmanaged.workspace.label.as_deref(), Some("project"));
        assert_eq!(unmanaged.workspace.cwd.as_deref(), Some("/tmp/project"));
        assert_eq!(
            unmanaged.evidence.hook_seen_at.as_deref(),
            Some("2026-05-05T12:00:02Z")
        );
        assert!(unmanaged
            .evidence
            .join_keys
            .iter()
            .any(|key| key == "source_path=/tmp/claude-unmanaged.jsonl"));
    }

    #[test]
    fn resolved_sessions_project_managed_claude_workspace_from_channel_observation() {
        use crate::managed_claude_scan::ClaudeChannelObservation;

        let obs = ClaudeChannelObservation {
            session_id: "managed-claude".to_string(),
            run_id: None,
            connection_id: None,
            lease_generation: None,
            provider_session_id: Some("claude-provider-session".to_string()),
            state_file: PathBuf::from("/tmp/managed-claude.json"),
            cwd: Some("/Users/test/git/acme".to_string()),
            claude_pid: Some(321),
            bridge_pid: Some(322),
            ready: true,
            started_at: "2026-05-07T20:03:50Z".to_string(),
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
            claude_foreground_tui: true,
        };
        let lease = ManagedSessionLease {
            session_id: "managed-claude".to_string(),
            provider: "claude".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "attached".to_string(),
            phase: Some("thinking".to_string()),
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: None,
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            lease_ttl_ms: 900_000,
        };

        let sessions = resolved_sessions_from_observations(&[lease], &[], &[], &[obs], &[], &[]);

        assert_eq!(sessions.len(), 1);
        let session = &sessions[0];
        assert_eq!(session.provider, "claude");
        assert_eq!(session.control_path, "managed");
        assert_eq!(session.workspace.label.as_deref(), Some("acme"));
        assert_eq!(
            session.workspace.cwd.as_deref(),
            Some("/Users/test/git/acme")
        );
    }

    fn test_cursor_observation(session_id: &str) -> CursorHelmObservation {
        CursorHelmObservation {
            session_id: session_id.to_string(),
            run_id: Some(format!("run-{session_id}")),
            connection_id: Some(format!("connection-{session_id}")),
            lease_generation: Some(format!("lease-{session_id}")),
            state_file: PathBuf::from(format!("/tmp/{session_id}.json")),
            socket_path: Some(PathBuf::from(format!("/tmp/{session_id}.sock"))),
            cwd: Some("/Users/test/git/zerg".to_string()),
            launcher_pid: Some(4242),
            launcher_process_start_time: Some("Tue Jul  8 22:50:19 2026".to_string()),
            cursor_pid: Some(4243),
            cursor_process_start_time: Some("Tue Jul  8 22:50:20 2026".to_string()),
            started_at: "2026-07-08T22:50:19Z".to_string(),
            updated_at: "2026-07-08T22:50:19Z".to_string(),
            launcher_alive: true,
            live: true,
        }
    }

    fn cursor_lease(session_id: &str) -> ManagedSessionLease {
        ManagedSessionLease {
            session_id: session_id.to_string(),
            provider: "cursor".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "attached".to_string(),
            phase: Some("idle".to_string()),
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: None,
            observed_at: "2026-07-08T22:50:19Z".to_string(),
            lease_ttl_ms: 900_000,
        }
    }

    #[test]
    fn resolved_sessions_project_cursor_workspace_and_ui_presence() {
        let lease = cursor_lease("managed-cursor");
        let obs = test_cursor_observation("managed-cursor");
        let sessions = resolved_sessions_from_observations(
            std::slice::from_ref(&lease),
            &[],
            &[],
            &[],
            &[],
            std::slice::from_ref(&obs),
        );
        assert_eq!(sessions.len(), 1);
        let session = &sessions[0];
        assert_eq!(session.provider, "cursor");
        assert_eq!(session.control_path, "managed");
        assert_eq!(session.state, "attached");
        assert_eq!(session.workspace.label.as_deref(), Some("zerg"));
        assert_eq!(
            session.workspace.cwd.as_deref(),
            Some("/Users/test/git/zerg")
        );
        assert_eq!(session.bridge.launch_mode.as_deref(), Some("tui"));
        assert_eq!(session.bridge.ui_attached, Some(true));
        assert_eq!(
            session.bridge.ui_presence.as_deref(),
            Some("foreground_tui")
        );
        assert_eq!(session.bridge.bridge_pid, Some(4242));
        assert_eq!(session.process.pid, Some(4243));
        assert_eq!(
            session.process.process_start_time.as_deref(),
            Some("Tue Jul  8 22:50:20 2026")
        );
        assert!(session.evidence.process_observed);
        assert!(session
            .evidence
            .join_keys
            .iter()
            .any(|key| key.starts_with("launcher_pid=") && key.ends_with("4242")));
        assert!(session
            .evidence
            .join_keys
            .iter()
            .any(|key| key.starts_with("cursor_pid=") && key.ends_with("4243")));
    }

    #[test]
    fn cursor_ui_presence_maps_live_and_lease_states() {
        let live = test_cursor_observation("cursor-live");
        assert_eq!(
            cursor_ui_presence("attached", Some(&live)),
            Some("foreground_tui")
        );
        assert_eq!(
            cursor_ui_presence("detached", Some(&live)),
            Some("detached")
        );
        assert_eq!(
            cursor_ui_presence("degraded", Some(&live)),
            Some("degraded")
        );
        assert_eq!(cursor_ui_presence("attached", None), None);

        let mut dead = live.clone();
        dead.live = false;
        assert_eq!(cursor_ui_presence("attached", Some(&dead)), None);
    }

    #[test]
    fn resolved_sessions_keep_sparse_managed_cursor_without_observation() {
        let lease = cursor_lease("managed-cursor");
        let sessions = resolved_sessions_from_observations(&[lease], &[], &[], &[], &[], &[]);
        assert_eq!(sessions.len(), 1);
        let session = &sessions[0];
        assert_eq!(session.provider, "cursor");
        assert_eq!(session.control_path, "managed");
        assert_eq!(session.workspace, ResolvedWorkspace::default());
        assert_eq!(session.bridge.ui_presence, None);
        assert_eq!(session.bridge.launch_mode, None);
        assert_eq!(session.evidence.process_observed, false);
    }

    #[test]
    fn resolved_sessions_keep_sparse_managed_codex_without_observation() {
        let lease = ManagedSessionLease {
            session_id: "managed-codex".to_string(),
            provider: "codex".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "attached".to_string(),
            phase: Some("idle".to_string()),
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: Some("waiting_for_turn".to_string()),
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            lease_ttl_ms: 900_000,
        };

        let sessions = resolved_sessions_from_observations(&[lease], &[], &[], &[], &[], &[]);

        assert_eq!(sessions.len(), 1);
        let session = &sessions[0];
        assert_eq!(session.session_id.as_deref(), Some("managed-codex"));
        assert_eq!(session.provider, "codex");
        assert_eq!(session.control_path, "managed");
        assert_eq!(session.state, "attached");
        assert_eq!(session.phase, None);
        assert_eq!(session.provider_session_id, None);
        assert_eq!(session.bridge.status, None);
        assert_eq!(session.evidence.process_observed, false);
        assert_eq!(session.evidence.transcript_observed, false);
        assert_eq!(
            session.evidence.join_keys,
            vec!["session_id=managed-codex".to_string()]
        );
    }

    #[test]
    fn resolved_sessions_include_managed_opencode_server_bridge_evidence() {
        let lease = ManagedSessionLease {
            session_id: "managed-opencode".to_string(),
            provider: "opencode".to_string(),
            machine_id: "cinder".to_string(),
            sequence: 1,
            state: "degraded".to_string(),
            phase: Some("needs_user".to_string()),
            tool_name: None,
            bridge_status: Some("ready".to_string()),
            thread_subscription_status: None,
            observed_at: "2026-05-05T12:00:00Z".to_string(),
            lease_ttl_ms: 900_000,
        };
        let obs = OpenCodeServerObservation {
            session_id: "managed-opencode".to_string(),
            run_id: None,
            connection_id: None,
            lease_generation: None,
            provider_session_id: "opencode-native-session".to_string(),
            state_file: PathBuf::from("/tmp/managed-opencode.json"),
            cwd: Some("/Users/test/git/acme".to_string()),
            server_url: Some("http://127.0.0.1:12345".to_string()),
            pid: Some(9876),
            started_at: "2026-05-05T11:59:00Z".to_string(),
            updated_at: "2026-05-05T12:00:00Z".to_string(),
            server_alive: true,
            health_ready: true,
            has_tui_attachment: true,
            launch_mode: "attached_tui".to_string(),
            owner_wrapper_pid: Some(9000),
            owner_wrapper_start_time: "Mon May  5 11:58:00 2026".to_string(),
            process_start_time: "Mon May  5 11:59:00 2026".to_string(),
        };

        let sessions = resolved_sessions_from_observations(&[lease], &[], &[], &[], &[obs], &[]);

        assert_eq!(sessions.len(), 1);
        let session = &sessions[0];
        assert_eq!(session.session_id.as_deref(), Some("managed-opencode"));
        assert_eq!(session.provider, "opencode");
        assert_eq!(session.control_path, "managed");
        assert_eq!(session.state, "degraded");
        assert_eq!(session.phase, None);
        assert_eq!(
            session.provider_session_id.as_deref(),
            Some("opencode-native-session")
        );
        assert_eq!(
            session.workspace.cwd.as_deref(),
            Some("/Users/test/git/acme")
        );
        assert_eq!(session.process.pid, Some(9876));
        assert_eq!(
            session.process.process_start_time.as_deref(),
            Some("Mon May  5 11:59:00 2026")
        );
        assert_eq!(session.evidence.process_observed, true);
        assert_eq!(session.evidence.transcript_observed, true);
        assert_eq!(
            session.evidence.join_keys,
            vec![
                "session_id=managed-opencode".to_string(),
                "provider_session_id=opencode-native-session".to_string(),
                "opencode_pid=9876".to_string(),
            ]
        );
    }

    #[test]
    fn test_heartbeat_payload_no_last_ship() {
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 1,
            last_ship_at: None,
            last_ship_attempt_at: None,
            last_ship_result: None,
            last_ship_latency_ms: None,
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 0,
            spool_dead_count: 0,
            archive_backlog: ArchiveBacklogSnapshot::default(),
            storage_v2_outbox: StorageV2OutboxSnapshot::default(),
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
            ship_lanes: ShipLaneSummarySet::default(),
            events_per_sec_ewma_10s: None,
            bytes_per_sec_ewma_10s: None,
            disk_free_bytes: 0,
            is_offline: true,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            machine_evidence: None,
            sessions: Vec::new(),
            sessions_digest: None,
            sessions_sequence: None,
            adaptive_backlog_limiter: None,
            ship_scheduler: None,
            history_import: Default::default(),
        };

        let json = serde_json::to_string(&payload).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        // last_ship_at should be omitted when None
        assert!(parsed.get("last_ship_at").is_none() || parsed["last_ship_at"].is_null());
    }

    #[test]
    fn test_write_status_file_includes_dead_count() {
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let spool = Spool::new(&conn);
        let tracker = ConsecutiveErrorTracker::new();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 42,
            last_ship_at: Some("2026-03-10T00:00:00Z".to_string()),
            last_ship_attempt_at: None,
            last_ship_result: None,
            last_ship_latency_ms: None,
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 2,
            spool_dead_count: 3,
            archive_backlog: ArchiveBacklogSnapshot::default(),
            storage_v2_outbox: StorageV2OutboxSnapshot::default(),
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
            ship_lanes: ShipLaneSummarySet::default(),
            events_per_sec_ewma_10s: None,
            bytes_per_sec_ewma_10s: None,
            disk_free_bytes: 10,
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
        };

        spool
            .record_dead(
                "codex",
                "/tmp/dead-range.jsonl",
                100,
                220,
                Some("dead-session"),
                "oversize source range",
            )
            .unwrap();
        let stats = HeartbeatStats {
            conn: &conn,
            spool: &spool,
            tracker: &tracker,
            parse_tracker: &parse_tracker,
            ship_stats: &ship_stats,
            is_offline: false,
            last_ship_at: payload.last_ship_at.clone(),
        };

        let status_path = dir.path().join("agent").join("engine-status.json");
        let projection =
            build_status_file_projection(payload, &stats, Vec::new(), PhaseLedgerStatus::Ok);
        write_status_file(
            &projection,
            Some(serde_json::json!({
                "enabled": true,
                "status": "connected",
                "supports": ["codex.launch"],
            })),
            &ProjectionReconciliation::idle(),
            &status_path,
        );

        let json = std::fs::read_to_string(&status_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["spool_dead_count"], 3);
        assert_eq!(parsed["recent_dead_letters"][0]["provider"], "codex");
        assert_eq!(
            parsed["recent_dead_letters"][0]["file_path"],
            "/tmp/dead-range.jsonl"
        );
        assert_eq!(parsed["recent_dead_letters"][0]["range_bytes"], 120);
        // Callers that pass an empty ledger get an empty array, not a missing
        // key — the shape stays stable for consumers.
        assert_eq!(parsed["phase_ledger"], serde_json::json!([]));
        assert_eq!(parsed["phase_ledger_status"], "ok");
        // build block mirrors BuildIdentity::current() so menu bar / local-health
        // can detect drift between the installed CLI and the engine.
        let build = &parsed["build"];
        assert!(build.is_object(), "expected build block");
        assert!(build["version"].is_string());
        assert!(build["commit"].is_string());
        assert!(build["commit_short"].is_string());
        assert!(build["built_at"].is_string());
        assert!(build["channel"].is_string());
        assert!(build["dirty"].is_boolean());
        assert_eq!(parsed["control_channel"]["status"], "connected");
        assert_eq!(parsed["control_channel"]["supports"][0], "codex.launch");
        assert_eq!(
            parsed["local_projection"]["reconciliation"]["state"],
            "idle"
        );
        assert!(parsed["local_projection"]["engine_pulse_at"].is_string());

        let generated_at = parsed["local_projection"]["generated_at"].clone();
        let stable_dead_letters = parsed["recent_dead_letters"].clone();
        write_status_file(
            &projection,
            None,
            &ProjectionReconciliation::running("local_status", "2026-07-16T12:00:00Z"),
            &status_path,
        );
        let refreshed: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&status_path).unwrap()).unwrap();
        assert_eq!(refreshed["local_projection"]["generated_at"], generated_at);
        assert_eq!(refreshed["recent_dead_letters"], stable_dead_letters);
        assert_eq!(
            refreshed["local_projection"]["reconciliation"]["state"],
            "reconciling"
        );
        assert_eq!(
            refreshed["local_projection"]["reconciliation"]["reason"],
            "local_status"
        );

        refresh_existing_status_pulse(
            &ProjectionReconciliation::running("wake", "2026-07-16T12:01:00Z"),
            &status_path,
        );
        let pulsed: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&status_path).unwrap()).unwrap();
        assert_eq!(pulsed["local_projection"]["generated_at"], generated_at);
        assert_eq!(
            pulsed["local_projection"]["reconciliation"]["reason"],
            "wake"
        );
        assert_eq!(pulsed["daemon_pid"], std::process::id());
    }

    #[test]
    fn test_write_status_file_embeds_fresh_phase_ledger() {
        use crate::state::session_phase::{SessionPhaseSignal, SessionPhaseStore};
        use chrono::Utc;

        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let spool = Spool::new(&conn);
        let tracker = ConsecutiveErrorTracker::new();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();

        // Seed one fresh ledger row.
        SessionPhaseStore::new(&conn)
            .record(&SessionPhaseSignal {
                session_id: "sess-live".to_string(),
                provider: "claude".to_string(),
                phase: "running".to_string(),
                tool_name: Some("Bash".to_string()),
                source: "claude_hook".to_string(),
                observed_at: Utc::now(),
            })
            .unwrap();

        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 42,
            last_ship_at: None,
            last_ship_attempt_at: None,
            last_ship_result: None,
            last_ship_latency_ms: None,
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 0,
            spool_dead_count: 0,
            archive_backlog: ArchiveBacklogSnapshot::default(),
            storage_v2_outbox: StorageV2OutboxSnapshot::default(),
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
            ship_lanes: ShipLaneSummarySet::default(),
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
            history_import: Default::default(),
        };
        let stats = HeartbeatStats {
            conn: &conn,
            spool: &spool,
            tracker: &tracker,
            parse_tracker: &parse_tracker,
            ship_stats: &ship_stats,
            is_offline: false,
            last_ship_at: None,
        };

        let phase_ledger = SessionPhaseStore::new(&conn)
            .fresh_rows(Utc::now())
            .expect("fresh_rows should succeed on a live DB");

        let status_path = dir.path().join("agent").join("engine-status.json");
        let projection =
            build_status_file_projection(payload, &stats, phase_ledger, PhaseLedgerStatus::Ok);
        write_status_file(
            &projection,
            None,
            &ProjectionReconciliation::idle(),
            &status_path,
        );

        let json = std::fs::read_to_string(status_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["phase_ledger"][0]["session_id"], "sess-live");
        assert_eq!(parsed["phase_ledger"][0]["phase"], "running");
        assert_eq!(parsed["phase_ledger"][0]["tool_name"], "Bash");
        assert_eq!(parsed["phase_ledger"][0]["source"], "claude_hook");
        assert_eq!(parsed["phase_ledger_status"], "ok");
    }

    #[test]
    fn test_write_status_file_records_ledger_read_failure() {
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let spool = Spool::new(&conn);
        let tracker = ConsecutiveErrorTracker::new();
        let parse_tracker = RecentIssueTracker::new();
        let ship_stats = RecentShipStatsTracker::new();
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 42,
            last_ship_at: None,
            last_ship_attempt_at: None,
            last_ship_result: None,
            last_ship_latency_ms: None,
            last_ship_http_status: None,
            last_ship_error_kind: None,
            last_ship_error_message: None,
            spool_pending_count: 0,
            spool_dead_count: 0,
            archive_backlog: ArchiveBacklogSnapshot::default(),
            storage_v2_outbox: StorageV2OutboxSnapshot::default(),
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
            ship_lanes: ShipLaneSummarySet::default(),
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
            history_import: Default::default(),
        };
        let stats = HeartbeatStats {
            conn: &conn,
            spool: &spool,
            tracker: &tracker,
            parse_tracker: &parse_tracker,
            ship_stats: &ship_stats,
            is_offline: false,
            last_ship_at: None,
        };

        let status_path = dir.path().join("agent").join("engine-status.json");
        let projection = build_status_file_projection(
            payload,
            &stats,
            Vec::new(),
            PhaseLedgerStatus::ReadFailed("db locked".to_string()),
        );
        write_status_file(
            &projection,
            None,
            &ProjectionReconciliation::idle(),
            &status_path,
        );

        let json = std::fs::read_to_string(status_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["phase_ledger"], serde_json::json!([]));
        assert_eq!(parsed["phase_ledger_status"], "read_failed: db locked");
    }

    #[test]
    fn machine_evidence_keeps_provider_fact_families_independent() {
        let now = DateTime::parse_from_rfc3339("2026-05-08T12:00:02Z")
            .unwrap()
            .with_timezone(&Utc);
        let codex = test_observation("codex-session", "ws://127.0.0.1:45681/session");
        let claude = ClaudeChannelObservation {
            session_id: "claude-session".to_string(),
            run_id: Some("run-claude-session".to_string()),
            connection_id: Some("connection-claude-session".to_string()),
            lease_generation: Some("lease-claude-session".to_string()),
            provider_session_id: Some("claude-provider-session".to_string()),
            state_file: PathBuf::from("/tmp/claude-session.json"),
            cwd: Some("/tmp/claude".to_string()),
            claude_pid: Some(201),
            bridge_pid: Some(202),
            ready: true,
            started_at: "2026-05-08T11:59:00Z".to_string(),
            updated_at: "2026-05-08T12:00:00Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
            claude_foreground_tui: true,
        };
        let opencode = test_opencode_observation("opencode-session", "attached_tui");
        let mut cursor = test_cursor_observation("cursor-session");
        cursor.live = false;
        cursor.launcher_alive = true;
        let antigravity = UnmanagedSessionBinding {
            machine_id: "cinder".to_string(),
            provider: "antigravity".to_string(),
            provider_session_id: "antigravity-provider-session".to_string(),
            source_path: Some("/tmp/antigravity.jsonl".to_string()),
            source_inode: Some(7),
            source_device: Some(8),
            // Deliberately overlaps a managed Codex pid. Legacy filtering must
            // remove it from compatibility bindings without erasing the raw
            // Antigravity fact from typed evidence.
            pid: Some(12345),
            process_start_time: Some("Thu May  8 11:58:00 2026".to_string()),
            cwd: Some("/tmp/antigravity".to_string()),
            source_offset: Some(99),
            source_mtime: Some("2026-05-08T12:00:00Z".to_string()),
            observed_at: "2026-05-08T12:00:00Z".to_string(),
        };
        let antigravity_hook = AntigravityHookObservation {
            state_file: PathBuf::from("/tmp/antigravity-session.json"),
            schema_version: 2,
            session_id: "antigravity-session".to_string(),
            provider_session_id: Some("antigravity-provider-session".to_string()),
            cwd: Some("/tmp/antigravity".to_string()),
            transcript_path: Some("/tmp/antigravity.jsonl".to_string()),
            state: Some("thinking".to_string()),
            updated_at: "2026-05-08T12:00:02Z".to_string(),
            last_hook_event: Some("PreInvocation".to_string()),
            last_hook_observed_at: Some("2026-05-08T12:00:00Z".to_string()),
            last_claimed_message_id: Some("message-1".to_string()),
            last_claimed_at: Some("2026-05-08T12:00:01Z".to_string()),
            last_claim_event: Some("PreInvocation".to_string()),
            last_response_event: Some("PreInvocation".to_string()),
            last_response_at: Some("2026-05-08T12:00:02Z".to_string()),
            last_response_status: Some("ok".to_string()),
            last_response_claimed_message_ids: vec!["message-1".to_string()],
            last_continuation_requested: false,
        };
        let phase = PhaseLedgerRow {
            session_id: "codex-session".to_string(),
            provider: "codex".to_string(),
            phase: "running".to_string(),
            tool_name: Some("Shell".to_string()),
            source: "codex_bridge".to_string(),
            observed_at: "2026-05-08T12:00:00Z".to_string(),
            valid_until: "2026-05-08T12:10:00Z".to_string(),
        };

        let codex_observations = [codex];
        let antigravity_observations = [antigravity_hook];
        let claude_observations = [claude];
        let opencode_observations = [opencode];
        let cursor_observations = [cursor];
        let unmanaged_bindings = [antigravity];
        let legacy_filtered = filter_unmanaged_bindings_owned_by_managed_observations(
            unmanaged_bindings.to_vec(),
            &codex_observations,
            &claude_observations,
            &opencode_observations,
            &cursor_observations,
        );
        assert!(legacy_filtered.is_empty());
        let evidence = machine_evidence_from_observations(
            "cinder",
            &codex_observations,
            &antigravity_observations,
            &claude_observations,
            &opencode_observations,
            &cursor_observations,
            &unmanaged_bindings,
            std::slice::from_ref(&phase),
            true,
            now,
        );

        assert_eq!(evidence.schema_version, 3);
        assert!(evidence.identities.iter().any(|identity| {
            identity.fact_family == "activity" && identity.subject_key == "run:run-codex-session"
        }));
        for provider in ["codex", "claude", "opencode", "cursor"] {
            assert!(evidence.identities.iter().any(|identity| {
                identity.fact_family == "control"
                    && identity
                        .subject_key
                        .starts_with(&format!("connection:connection-{provider}-session:"))
            }));
        }
        assert!(evidence
            .identities
            .iter()
            .filter(|identity| identity.fact_family == "process")
            .all(|identity| {
                identity.subject_key.starts_with("process:")
                    && ["codex", "claude", "opencode", "cursor", "antigravity"]
                        .iter()
                        .any(|provider| identity.subject_key.contains(&format!(":{provider}:")))
            }));
        assert!(evidence.identities.iter().any(|identity| {
            identity.fact_family == "transcript"
                && identity.source_seq == Some(99)
                && identity.sequenced
        }));
        assert!(evidence.identities.iter().all(|identity| {
            identity.dedupe_key.len() == 64 && identity.evidence_hash.len() == 64
        }));
        let process_providers = evidence
            .process
            .iter()
            .map(|fact| fact.provider.as_str())
            .collect::<HashSet<_>>();
        assert_eq!(
            process_providers,
            HashSet::from(["codex", "claude", "opencode", "cursor", "antigravity"])
        );
        assert!(evidence
            .control
            .iter()
            .all(|fact| fact.provider != "antigravity"));
        let control_grants = |provider: &str| {
            evidence
                .control
                .iter()
                .find(|fact| fact.provider == provider)
                .map(|fact| fact.granted_operations.clone())
                .unwrap()
        };
        assert!(evidence
            .control
            .iter()
            .all(|fact| fact.observed_at == "2026-05-08T12:00:02+00:00"));
        assert_eq!(
            control_grants("codex"),
            vec!["interrupt".to_string(), "send_input".to_string()]
        );
        assert_eq!(
            control_grants("claude"),
            vec!["interrupt".to_string(), "send_input".to_string()]
        );
        assert_eq!(
            control_grants("opencode"),
            vec![
                "interrupt".to_string(),
                "send_input".to_string(),
                "terminate".to_string()
            ]
        );
        assert!(control_grants("cursor").is_empty());
        assert!(evidence.process.iter().any(|fact| {
            fact.provider == "antigravity"
                && fact.source == "antigravity_hook_state"
                && !fact.alive
                && fact.pid.is_none()
        }));
        assert!(evidence.transcript.iter().any(|fact| {
            fact.provider == "antigravity"
                && fact.source == "antigravity_hook_state"
                && fact.source_path.as_deref() == Some("/tmp/antigravity.jsonl")
        }));
        assert!(evidence
            .transcript
            .iter()
            .all(|fact| fact.provider != "cursor"));
        assert!(evidence
            .process
            .iter()
            .any(|fact| { fact.provider == "cursor" && fact.role == "launcher" && fact.alive }));
        assert!(evidence
            .control
            .iter()
            .any(|fact| fact.provider == "cursor" && fact.state == "detached"));
        assert_eq!(evidence.readiness.len(), 1);
        assert!(evidence.readiness[0].recent_hook_observed);
        assert!(evidence.readiness[0].claim_observed);
        assert!(evidence.readiness[0].response_observed);
        assert!(!evidence.readiness[0].continuation_observed);
        assert_eq!(
            evidence.activity,
            vec![ActivityEvidence {
                authority_class: "provider_runtime".to_string(),
                provider: "codex".to_string(),
                session_id: "codex-session".to_string(),
                run_id: Some("run-codex-session".to_string()),
                kind: "running".to_string(),
                raw_kind: "running".to_string(),
                tool_name: Some("Shell".to_string()),
                detail: None,
                source: "codex_bridge".to_string(),
                observed_at: "2026-05-08T12:00:00Z".to_string(),
                valid_until: "2026-05-08T12:10:00Z".to_string(),
                raw_locator: None,
                reason_codes: Vec::new(),
            }]
        );
        assert!(evidence
            .process_snapshot_scopes
            .iter()
            .all(|scope| scope.complete));
        let unknown_activity = activity_evidence_from_phase_row(
            &PhaseLedgerRow {
                session_id: "future-session".to_string(),
                provider: "future-provider".to_string(),
                phase: "provider_custom_phase".to_string(),
                tool_name: None,
                source: "future_hook".to_string(),
                observed_at: "2026-05-08T12:00:00Z".to_string(),
                valid_until: "2026-05-08T12:01:00Z".to_string(),
            },
            None,
        );
        assert_eq!(unknown_activity.kind, "unknown");
        assert_eq!(unknown_activity.raw_kind, "provider_custom_phase");
        assert_eq!(
            unknown_activity.reason_codes,
            vec!["unknown_provider_activity"]
        );

        let unauthoritative_terminal = activity_evidence_from_phase_row(
            &PhaseLedgerRow {
                session_id: "terminal-session".to_string(),
                provider: "codex".to_string(),
                phase: "finished".to_string(),
                tool_name: None,
                source: "codex_bridge".to_string(),
                observed_at: "2026-05-08T12:00:00Z".to_string(),
                valid_until: "2026-05-08T12:10:00Z".to_string(),
            },
            None,
        );
        assert_eq!(unauthoritative_terminal.kind, "unknown");
        assert_eq!(unauthoritative_terminal.raw_kind, "finished");
        assert_eq!(
            unauthoritative_terminal.reason_codes,
            vec!["run_terminal_authority_missing"]
        );

        let without_activity = machine_evidence_from_observations(
            "cinder",
            &codex_observations,
            &antigravity_observations,
            &claude_observations,
            &opencode_observations,
            &cursor_observations,
            &unmanaged_bindings,
            &[],
            false,
            now,
        );
        assert!(without_activity.activity.is_empty());
        assert_eq!(without_activity.process, evidence.process);
        assert_eq!(without_activity.control, evidence.control);
        assert_eq!(without_activity.transcript, evidence.transcript);
        assert_eq!(without_activity.readiness, evidence.readiness);
        assert!(without_activity
            .process_snapshot_scopes
            .iter()
            .all(|scope| !scope.complete));

        let serialized = serde_json::to_string(&evidence).unwrap();
        for forbidden in [
            "\"state_file\":",
            "\"ws_url\":",
            "\"server_url\":",
            "\"password\":",
            "\"token\":",
            "\"command\":",
            "\"argv\":",
        ] {
            assert!(!serialized.contains(forbidden), "leaked {forbidden}");
        }

        let reevaluated = antigravity_readiness_evidence(
            &antigravity_observations[0],
            now + chrono::Duration::minutes(5),
        );
        assert_eq!(reevaluated, evidence.readiness[0]);

        let mut stale_at_observation = antigravity_observations[0].clone();
        stale_at_observation.updated_at = "2026-05-08T12:03:00Z".to_string();
        let stale = antigravity_readiness_evidence(&stale_at_observation, now);
        assert!(!stale.recent_hook_observed);
        assert!(!stale.claim_observed);
        assert!(!stale.response_observed);
        assert_eq!(
            stale,
            antigravity_readiness_evidence(
                &stale_at_observation,
                now + chrono::Duration::minutes(5)
            )
        );
    }

    #[test]
    fn antigravity_readiness_without_proofs_is_stable_across_heartbeats() {
        let now = DateTime::parse_from_rfc3339("2026-05-08T12:05:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let observation = AntigravityHookObservation {
            state_file: PathBuf::from("/tmp/antigravity-stale.json"),
            schema_version: 1,
            session_id: "antigravity-stale".to_string(),
            provider_session_id: None,
            cwd: None,
            transcript_path: None,
            state: None,
            updated_at: "2026-05-08T12:00:00Z".to_string(),
            last_hook_event: None,
            last_hook_observed_at: None,
            last_claimed_message_id: None,
            last_claimed_at: None,
            last_claim_event: None,
            last_response_event: None,
            last_response_at: None,
            last_response_status: None,
            last_response_claimed_message_ids: Vec::new(),
            last_continuation_requested: false,
        };

        let first = antigravity_readiness_evidence(&observation, now);
        let second =
            antigravity_readiness_evidence(&observation, now + chrono::Duration::minutes(5));
        assert_eq!(first, second);
        assert_eq!(
            parse_utc(Some(&first.valid_until)),
            parse_utc(Some(&observation.updated_at))
        );

        let first_identity = reducer_evidence_identities("cinder", &[], &[], &[], &[], &[first]);
        let second_identity = reducer_evidence_identities("cinder", &[], &[], &[], &[], &[second]);
        assert_eq!(first_identity, second_identity);
        assert_eq!(first_identity.len(), 1);
        assert!(first_identity[0].source_epoch.is_some());
    }

    #[test]
    fn unmanaged_transcript_identity_uses_source_time_not_scan_time() {
        let first_now = DateTime::parse_from_rfc3339("2026-05-08T12:05:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let binding = UnmanagedSessionBinding {
            machine_id: "cinder".to_string(),
            provider: "codex".to_string(),
            provider_session_id: "codex-unmanaged".to_string(),
            source_path: Some("/tmp/codex-unmanaged.jsonl".to_string()),
            source_inode: Some(7),
            source_device: Some(8),
            pid: Some(123),
            process_start_time: Some("2026-05-08T11:00:00Z".to_string()),
            cwd: Some("/tmp".to_string()),
            source_offset: Some(99),
            source_mtime: Some("2026-05-08T12:00:00Z".to_string()),
            observed_at: first_now.to_rfc3339(),
        };
        let mut rescanned = binding.clone();
        rescanned.observed_at = (first_now + chrono::Duration::minutes(5)).to_rfc3339();

        let first = machine_evidence_from_observations(
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &[binding.clone()],
            &[],
            true,
            first_now,
        );
        let second = machine_evidence_from_observations(
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &[rescanned],
            &[],
            true,
            first_now + chrono::Duration::minutes(5),
        );
        assert_eq!(first.transcript, second.transcript);
        let first_identity = first
            .identities
            .iter()
            .find(|identity| identity.fact_family == "transcript")
            .unwrap();
        let second_identity = second
            .identities
            .iter()
            .find(|identity| identity.fact_family == "transcript")
            .unwrap();
        assert_eq!(first_identity, second_identity);
        assert_ne!(
            first_identity.source_epoch.as_deref(),
            Some(stable_component("/tmp/codex-unmanaged.jsonl:8:7").as_str())
        );

        let mut missing_mtime = binding;
        missing_mtime.source_mtime = None;
        let without_stable_position = machine_evidence_from_observations(
            "cinder",
            &[],
            &[],
            &[],
            &[],
            &[],
            &[missing_mtime],
            &[],
            true,
            first_now,
        );
        assert!(without_stable_position.transcript.is_empty());
    }

    #[test]
    fn schema_v3_omits_authority_facts_for_pre_v3_managed_state() {
        let now = DateTime::parse_from_rfc3339("2026-05-08T12:00:02Z")
            .unwrap()
            .with_timezone(&Utc);
        let mut codex = test_observation("legacy-codex", "ws://127.0.0.1:45681/session");
        codex.run_id = None;
        let phase = PhaseLedgerRow {
            session_id: codex.session_id.clone(),
            provider: "codex".to_string(),
            phase: "running".to_string(),
            tool_name: None,
            source: "codex_bridge".to_string(),
            observed_at: "2026-05-08T12:00:00Z".to_string(),
            valid_until: "2026-05-08T12:10:00Z".to_string(),
        };

        let evidence = machine_evidence_from_observations(
            "cinder",
            &[codex],
            &[],
            &[],
            &[],
            &[],
            &[],
            &[phase],
            true,
            now,
        );

        assert_eq!(evidence.schema_version, 3);
        assert!(evidence.control.is_empty());
        assert!(evidence.activity.is_empty());
        assert!(!evidence.process.is_empty());
        assert!(!evidence.transcript.is_empty());
    }

    #[test]
    fn canonical_evidence_hash_matches_server_golden_vector() {
        let vector: serde_json::Value =
            serde_json::from_str(include_str!("../../schemas/machine-evidence-hash-v1.json"))
                .unwrap();
        let value = vector.get("value").unwrap().clone();
        let canonical = serde_json::to_string(&canonical_evidence_value(value).unwrap()).unwrap();
        let digest = format!("{:x}", Sha256::digest(canonical.as_bytes()));

        assert_eq!(canonical, vector["canonical_json"].as_str().unwrap());
        assert_eq!(digest, vector["sha256"].as_str().unwrap());
        assert!(canonical_evidence_value(serde_json::json!({"float": 1.5})).is_err());
        for raw in ["-9223372036854775808", "18446744073709551615"] {
            let value: serde_json::Value = serde_json::from_str(raw).unwrap();
            assert!(canonical_evidence_value(value).is_ok());
        }
        for raw in ["-9223372036854775809", "18446744073709551616"] {
            let value: serde_json::Value = serde_json::from_str(raw).unwrap();
            assert!(canonical_evidence_value(value).is_err());
        }
        for timestamp in [
            "2026-99-99T99:99:99Z",
            "2016-12-31T23:59:60Z",
            "2026-05-08t12:00:00z",
            "2026-05-08 12:00:00Z",
            "0000-01-01T00:00:00Z",
            "0001-01-01T00:00:00+00:01",
            "9999-12-31T23:59:59-00:01",
        ] {
            let value = serde_json::json!({"observed_at": timestamp});
            assert_eq!(canonical_evidence_value(value.clone()).unwrap(), value);
        }
    }
}
