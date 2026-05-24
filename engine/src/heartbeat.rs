//! Periodic heartbeat emitter.
//!
//! The daemon reuses this payload for two related jobs:
//!
//! - frequent local status-file writes for ambient UX / debugging
//! - less frequent server heartbeats to `/api/agents/heartbeat`

use std::collections::HashMap;
use std::collections::HashSet;
use std::path::Path;
use std::sync::OnceLock;
use std::time::Duration;

use anyhow::Result;
use chrono::DateTime;
use chrono::Utc;
use serde::Serialize;

use crate::build_identity::BuildIdentity;
use crate::managed_bridge_scan::CodexBridgeObservation;
use crate::managed_claude_scan::ClaudeChannelObservation;

/// Captured once per daemon process at the first write_status_file call.
/// Compared against the on-disk binary mtime to detect "restart pending".
static DAEMON_STARTED_AT: OnceLock<String> = OnceLock::new();
use crate::config;
use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::shipping::client::ShipperClient;
use crate::shipping_stats::RecentShipStatsTracker;
use crate::state::session_phase::PhaseLedgerRow;
use crate::state::spool::DeadLetterEntry;
use crate::state::spool::Spool;

const HEARTBEAT_POST_TIMEOUT: Duration = Duration::from_secs(6);

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
    /// Resolved local session/execution view. This is the canonical local
    /// identity graph projection for menu bar and local-health consumers:
    /// managed/unmanaged presentation state is derived after joining raw
    /// bridge, channel, transcript, and process observations.
    #[serde(default)]
    pub sessions: Vec<ResolvedLocalSession>,
    /// Phase-2 adaptive backlog limiter snapshot. Drives AIMD off the
    /// `X-Ingest-Queue-Wait-Ms` header on each successful ship; absent on
    /// processes that haven't built a scheduler yet (e.g. legacy heartbeat
    /// callers).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub adaptive_backlog_limiter: Option<crate::scheduler::LimiterSnapshot>,
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
pub struct ResolvedLocalSession {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub session_id: Option<String>,
    pub provider: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub provider_session_id: Option<String>,
    pub control_path: String,
    pub presentation_state: String,
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase_observed_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_activity_at: Option<String>,
    pub workspace: ResolvedWorkspace,
    pub process: ResolvedProcess,
    pub bridge: ResolvedBridge,
    pub evidence: ResolvedEvidence,
    #[serde(default)]
    pub reason_codes: Vec<String>,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub started_at: Option<String>,
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

pub use crate::unmanaged_bindings::collect_unmanaged_session_bindings_with_store;

#[derive(Debug, Serialize, Clone, PartialEq, Eq)]
pub struct ManagedSessionLease {
    pub session_id: String,
    pub provider: String,
    pub machine_id: String,
    pub sequence: u64,
    pub state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub phase: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bridge_status: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thread_subscription_status: Option<String>,
    pub observed_at: String,
    pub lease_ttl_ms: u64,
}

#[derive(Debug, Clone)]
struct ManagedPhaseOverlay {
    phase: Option<String>,
    tool_name: Option<String>,
    observed_at: Option<String>,
}

/// Stats needed to build a heartbeat.
pub struct HeartbeatStats<'a> {
    pub spool: &'a Spool<'a>,
    pub tracker: &'a ConsecutiveErrorTracker,
    pub parse_tracker: &'a RecentIssueTracker,
    pub ship_stats: &'a RecentShipStatsTracker,
    pub is_offline: bool,
    pub last_ship_at: Option<String>,
}

#[derive(Debug, Serialize)]
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
            disk_free_bytes,
            is_offline: stats.is_offline,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            sessions: Vec::new(),
            adaptive_backlog_limiter: None,
        }
    }
}

/// Build lease views from a pre-collected set of bridge observations.
/// Kept pure (no fs/ps side effects) so the reaper and tests can share
/// the same scan pass.
pub fn leases_from_observations(
    conn: &rusqlite::Connection,
    machine_id: &str,
    observations: &[CodexBridgeObservation],
    now: DateTime<Utc>,
) -> Vec<ManagedSessionLease> {
    let phase_overlay = load_managed_phase_overlay(conn);
    let sequence = now.timestamp_millis().max(0) as u64;
    let observed_at = now.to_rfc3339();
    let mut leases = Vec::with_capacity(observations.len());

    for obs in observations {
        if codex_bridge_observation_is_stopped(obs) {
            continue;
        }
        let overlay = phase_overlay.get(&obs.session_id);
        let thread_failed = obs.thread_subscription_status.as_deref() == Some("failed");
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
            phase: match lease_state {
                "attached" => Some(
                    overlay
                        .and_then(|row| normalize_managed_phase(row.phase.as_deref()))
                        .unwrap_or_else(|| "idle".to_string()),
                ),
                _ => None,
            },
            tool_name: overlay.and_then(|row| row.tool_name.clone()),
            bridge_status: Some(obs.status.clone()),
            thread_subscription_status: obs.thread_subscription_status.clone(),
            observed_at: overlay
                .and_then(|row| row.observed_at.clone())
                .unwrap_or_else(|| obs.updated_at.clone())
                .if_empty(observed_at.clone()),
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
/// Only live Claude provider processes are emitted. The server treats
/// `managed_sessions` as a complete snapshot, so a previously leased session
/// disappearing from this list becomes an explicit `process_gone` terminal
/// signal there.
pub fn leases_from_claude_channel_observations(
    conn: &rusqlite::Connection,
    machine_id: &str,
    observations: &[ClaudeChannelObservation],
    now: DateTime<Utc>,
) -> Vec<ManagedSessionLease> {
    let phase_overlay = load_managed_phase_overlay(conn);
    let sequence = now.timestamp_millis().max(0) as u64;
    let observed_at = now.to_rfc3339();
    let mut leases = Vec::with_capacity(observations.len());

    for obs in observations {
        if !obs.claude_alive {
            continue;
        }
        let overlay = phase_overlay.get(&obs.session_id);
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
            phase: match lease_state {
                "attached" => Some(
                    overlay
                        .and_then(|row| normalize_managed_phase(row.phase.as_deref()))
                        .unwrap_or_else(|| "idle".to_string()),
                ),
                _ => None,
            },
            tool_name: overlay.and_then(|row| row.tool_name.clone()),
            bridge_status: Some(if obs.ready && obs.bridge_alive {
                "ready".to_string()
            } else if obs.bridge_alive {
                "not_ready".to_string()
            } else {
                "bridge_down".to_string()
            }),
            thread_subscription_status: None,
            observed_at: overlay
                .and_then(|row| row.observed_at.clone())
                .unwrap_or_else(|| obs.updated_at.clone())
                .if_empty(observed_at.clone()),
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
) -> Vec<UnmanagedSessionBinding> {
    let managed_codex = ManagedCodexKeys::from_observations(codex_observations);
    let managed_claude = ManagedClaudeKeys::from_observations(claude_observations);

    bindings
        .into_iter()
        .filter(|binding| {
            !binding_owned_by_codex(binding, &managed_codex)
                && !binding_owned_by_claude(binding, &managed_claude)
        })
        .collect()
}

pub fn resolved_sessions_from_observations(
    managed_sessions: &[ManagedSessionLease],
    unmanaged_bindings: &[UnmanagedSessionBinding],
    codex_observations: &[CodexBridgeObservation],
    claude_observations: &[ClaudeChannelObservation],
) -> Vec<ResolvedLocalSession> {
    let codex_by_session: HashMap<&str, &CodexBridgeObservation> = codex_observations
        .iter()
        .map(|obs| (obs.session_id.as_str(), obs))
        .collect();
    let claude_by_session: HashMap<&str, &ClaudeChannelObservation> = claude_observations
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
        presentation_state: managed_presentation_state(&lease.state),
        state: lease.state.clone(),
        phase: lease.phase.clone(),
        tool_name: lease.tool_name.clone(),
        phase_observed_at: Some(lease.observed_at.clone()),
        last_activity_at: Some(lease.observed_at.clone()),
        workspace: workspace_from_cwd(cwd),
        process: ResolvedProcess {
            pid: process_pid,
            process_start_time: None,
            started_at: None,
        },
        bridge: ResolvedBridge {
            bridge_pid: obs.map(|obs| obs.bridge_pid),
            app_server_pid: process_pid,
            ws_url: obs.and_then(|obs| obs.ws_url.clone()),
            heartbeat_at: obs.map(|obs| obs.updated_at.clone()),
            status: obs.map(|obs| obs.status.clone()),
            thread_subscription_status: obs.and_then(|obs| obs.thread_subscription_status.clone()),
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

fn resolved_managed_claude_session(
    lease: &ManagedSessionLease,
    obs: Option<&ClaudeChannelObservation>,
) -> ResolvedLocalSession {
    let provider_session_id = obs.and_then(|obs| obs.provider_session_id.clone());
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
        presentation_state: managed_presentation_state(&lease.state),
        state: lease.state.clone(),
        phase: lease.phase.clone(),
        tool_name: lease.tool_name.clone(),
        phase_observed_at: Some(lease.observed_at.clone()),
        last_activity_at: Some(lease.observed_at.clone()),
        workspace: ResolvedWorkspace::default(),
        process: ResolvedProcess {
            pid: process_pid,
            process_start_time: None,
            started_at: None,
        },
        bridge: ResolvedBridge {
            bridge_pid: obs.and_then(|obs| obs.bridge_pid),
            app_server_pid: None,
            ws_url: None,
            heartbeat_at: obs.map(|obs| obs.updated_at.clone()),
            status: lease.bridge_status.clone(),
            thread_subscription_status: None,
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
        presentation_state: managed_presentation_state(&lease.state),
        state: lease.state.clone(),
        phase: lease.phase.clone(),
        tool_name: lease.tool_name.clone(),
        phase_observed_at: Some(lease.observed_at.clone()),
        last_activity_at: Some(lease.observed_at.clone()),
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
        presentation_state: "unmanaged".to_string(),
        state: "unmanaged".to_string(),
        phase: None,
        tool_name: None,
        phase_observed_at: None,
        last_activity_at: Some(binding.observed_at.clone()),
        workspace: workspace_from_cwd(binding.cwd.clone()),
        process: ResolvedProcess {
            pid: binding.pid,
            process_start_time: binding.process_start_time.clone(),
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

fn managed_presentation_state(state: &str) -> String {
    match state {
        "attached" => "managed_attached",
        "detached" => "managed_detached",
        "degraded" => "managed_degraded",
        _ => "stale_evidence",
    }
    .to_string()
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
    if pgid > 0 { Some(pgid) } else { None }
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

fn normalize_managed_phase(value: Option<&str>) -> Option<String> {
    let phase = value?.trim();
    match phase {
        "idle" | "thinking" | "running" | "blocked" | "needs_user" => Some(phase.to_string()),
        _ => None,
    }
}

fn load_managed_phase_overlay(conn: &rusqlite::Connection) -> HashMap<String, ManagedPhaseOverlay> {
    let mut rows = HashMap::new();
    let Ok(mut stmt) = conn.prepare(
        "SELECT session_id, phase_kind, tool_name, phase_observed_at
         FROM managed_session_state
         WHERE provider IN ('codex', 'claude')",
    ) else {
        return rows;
    };
    let Ok(iter) = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            ManagedPhaseOverlay {
                phase: row.get::<_, Option<String>>(1)?,
                tool_name: row.get::<_, Option<String>>(2)?,
                observed_at: row.get::<_, Option<String>>(3)?,
            },
        ))
    }) else {
        return rows;
    };
    for item in iter.flatten() {
        rows.insert(item.0, item.1);
    }
    rows
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
    payload: &HeartbeatPayload,
    stats: &HeartbeatStats<'_>,
    phase_ledger: Vec<PhaseLedgerRow>,
    ledger_status: PhaseLedgerStatus,
    control_channel: Option<serde_json::Value>,
    status_path: &std::path::Path,
) {
    #[derive(Serialize)]
    struct StatusFile<'a> {
        #[serde(flatten)]
        payload: &'a HeartbeatPayload,
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

    let recent_dead_letters = stats
        .spool
        .recent_dead(5)
        .unwrap_or_default()
        .into_iter()
        .map(status_dead_letter_from_entry)
        .collect();
    let now_utc = chrono::Utc::now();
    let daemon_started_at = DAEMON_STARTED_AT
        .get_or_init(|| now_utc.to_rfc3339())
        .clone();
    let (binary_path, binary_mtime) = inspect_current_exe();
    let status = StatusFile {
        payload,
        build: BuildIdentity::current(),
        binary_path,
        binary_mtime,
        daemon_started_at,
        recent_dead_letters,
        phase_ledger,
        phase_ledger_status: ledger_status,
        control_channel,
        last_updated: now_utc.to_rfc3339(),
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
            disk_free_bytes: 1_000_000_000,
            is_offline: false,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            sessions: Vec::new(),
            adaptive_backlog_limiter: None,
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
            sessions: Vec::new(),
            adaptive_backlog_limiter: None,
        };

        let json = serde_json::to_string(&payload).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed["managed_sessions"][0]["provider"], "codex");
        assert_eq!(parsed["managed_sessions"][0]["state"], "attached");
        assert_eq!(parsed["managed_sessions"][0]["phase"], "idle");
        assert_eq!(parsed["managed_sessions"][0]["lease_ttl_ms"], 900_000);
    }

    fn test_observation(session_id: &str, ws_url: &str) -> CodexBridgeObservation {
        CodexBridgeObservation {
            session_id: session_id.to_string(),
            state_file: PathBuf::from(format!("/tmp/{session_id}.json")),
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
            app_server_pid: Some(12345),
            app_server_pgid: Some(12345),
            updated_at: "2026-04-26T00:00:00Z".to_string(),
            bridge_alive: true,
            has_tui_attachment: true,
            app_server_alive: true,
        }
    }

    #[test]
    fn leases_from_observations_classifies_attached_with_phase_overlay() {
        use crate::state::managed_session_state::{
            ManagedSessionPhaseSignal, ManagedSessionStateStore,
        };

        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let session_id = "7474a2a1-ab9f-4a10-9726-898e895fedf0";
        let now = Utc::now();
        ManagedSessionStateStore::new(&conn)
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: session_id.to_string(),
                provider: "codex".to_string(),
                workspace_path: None,
                phase_kind: "running".to_string(),
                tool_name: Some("Shell".to_string()),
                phase_source: "codex_bridge".to_string(),
                observed_at: now,
            })
            .unwrap();

        let obs = test_observation(session_id, "ws://127.0.0.1:45678/session");
        let leases = leases_from_observations(&conn, "cinder", &[obs], now);

        assert_eq!(leases.len(), 1);
        let lease = &leases[0];
        assert_eq!(lease.session_id, session_id);
        assert_eq!(lease.state, "attached");
        assert_eq!(lease.phase.as_deref(), Some("running"));
        assert_eq!(lease.tool_name.as_deref(), Some("Shell"));
    }

    #[test]
    fn leases_from_observations_classifies_detached_ui_ready_bridge_as_attached() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let now = Utc::now();

        let mut obs = test_observation(
            "59612f92-0e4c-4031-b236-c4091f13da40",
            "ws://127.0.0.1:45679/session",
        );
        obs.has_tui_attachment = false;
        obs.app_server_alive = true;
        obs.thread_id = Some("thread-detached-ui".to_string());

        let leases = leases_from_observations(&conn, "cinder", &[obs], now);

        assert_eq!(leases.len(), 1);
        assert_eq!(leases[0].state, "attached");
        assert_eq!(leases[0].phase.as_deref(), Some("idle"));
    }

    #[test]
    fn leases_from_observations_classifies_degraded_and_detached() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let now = Utc::now();

        let mut degraded = test_observation("degraded-session", "ws://127.0.0.1:45679/session");
        degraded.has_tui_attachment = false;
        degraded.thread_id = None; // alive but no TUI/detached-UI thread -> degraded
        let mut detached = test_observation("detached-session", "ws://127.0.0.1:45680/session");
        detached.bridge_alive = false; // lock not held → detached

        let leases = leases_from_observations(&conn, "cinder", &[degraded, detached], now);

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
    fn leases_from_observations_skips_stopped_codex_bridges() {
        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let now = Utc::now();

        let mut stopped = test_observation("stopped-session", "ws://127.0.0.1:45681/session");
        stopped.status = "stopped".to_string();
        stopped.bridge_alive = false;
        stopped.has_tui_attachment = false;
        stopped.app_server_alive = false;

        let live = test_observation("live-session", "ws://127.0.0.1:45682/session");

        let leases = leases_from_observations(&conn, "cinder", &[stopped, live], now);

        assert_eq!(leases.len(), 1);
        assert_eq!(leases[0].session_id, "live-session");
    }

    #[test]
    fn leases_from_claude_channel_observations_emit_live_channel_sessions() {
        use crate::managed_claude_scan::ClaudeChannelObservation;
        use crate::state::managed_session_state::{
            ManagedSessionPhaseSignal, ManagedSessionStateStore,
        };

        let db = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(db.path())).unwrap();
        let session_id = "09b68f98-1e31-458e-b78a-6dfd062ead75";
        let now = Utc::now();
        ManagedSessionStateStore::new(&conn)
            .record_phase(&ManagedSessionPhaseSignal {
                session_id: session_id.to_string(),
                provider: "claude".to_string(),
                workspace_path: None,
                phase_kind: "needs_user".to_string(),
                tool_name: None,
                phase_source: "claude_hook".to_string(),
                observed_at: now,
            })
            .unwrap();

        let live = ClaudeChannelObservation {
            session_id: session_id.to_string(),
            provider_session_id: Some(session_id.to_string()),
            state_file: PathBuf::from("/tmp/live.json"),
            claude_pid: Some(123),
            bridge_pid: Some(124),
            ready: true,
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
        };
        let dead = ClaudeChannelObservation {
            session_id: "19b68f98-1e31-458e-b78a-6dfd062ead75".to_string(),
            provider_session_id: None,
            state_file: PathBuf::from("/tmp/dead.json"),
            claude_pid: Some(223),
            bridge_pid: Some(224),
            ready: true,
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: false,
            bridge_alive: false,
        };

        let leases = leases_from_claude_channel_observations(&conn, "cinder", &[live, dead], now);

        assert_eq!(leases.len(), 1);
        let lease = &leases[0];
        assert_eq!(lease.session_id, session_id);
        assert_eq!(lease.provider, "claude");
        assert_eq!(lease.state, "attached");
        assert_eq!(lease.phase.as_deref(), Some("needs_user"));
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

        let filtered =
            filter_unmanaged_bindings_owned_by_managed_observations(bindings, &[obs], &[]);

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

        let filtered =
            filter_unmanaged_bindings_owned_by_managed_observations(bindings.clone(), &[obs], &[]);

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

        let filtered =
            filter_unmanaged_bindings_owned_by_managed_observations(bindings, &[obs], &[]);

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

        let filtered =
            filter_unmanaged_bindings_owned_by_managed_observations(bindings.clone(), &[obs], &[]);

        assert_eq!(filtered, bindings);
    }

    #[test]
    fn filters_unmanaged_claude_binding_owned_by_channel() {
        use crate::managed_claude_scan::ClaudeChannelObservation;

        let obs = ClaudeChannelObservation {
            session_id: "managed-claude".to_string(),
            provider_session_id: Some("claude-provider-session".to_string()),
            state_file: PathBuf::from("/tmp/managed-claude.json"),
            claude_pid: Some(321),
            bridge_pid: Some(322),
            ready: true,
            updated_at: "2026-05-07T20:03:50Z".to_string(),
            claude_alive: true,
            bridge_alive: true,
        };
        let bindings = vec![
            test_binding("claude", "claude-provider-session", 999),
            test_binding("claude", "other-claude-session", 321),
            test_binding("claude", "real-unmanaged", 987),
        ];

        let filtered =
            filter_unmanaged_bindings_owned_by_managed_observations(bindings, &[], &[obs]);

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

        let sessions = resolved_sessions_from_observations(&[lease], &[unmanaged], &[obs], &[]);

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
        assert_eq!(managed.presentation_state, "managed_attached");
        assert_eq!(managed.workspace.label.as_deref(), Some("zerg"));
        assert_eq!(managed.workspace.cwd.as_deref(), Some("/Users/test/git/zerg"));
        assert_eq!(managed.phase.as_deref(), Some("thinking"));
        assert_eq!(managed.tool_name.as_deref(), Some("Bash"));
        assert_eq!(
            managed.phase_observed_at.as_deref(),
            Some("2026-05-05T12:00:00Z")
        );
        assert_eq!(
            managed.last_activity_at.as_deref(),
            Some("2026-05-05T12:00:00Z")
        );
        assert_eq!(managed.bridge.bridge_pid, Some(111));
        assert_eq!(managed.bridge.app_server_pid, Some(222));
        assert_eq!(managed.bridge.status.as_deref(), Some("ready"));
        assert_eq!(
            managed.bridge.thread_subscription_status.as_deref(),
            Some("subscribed")
        );
        assert!(
            managed
                .evidence
                .join_keys
                .iter()
                .any(|key| key == "provider_session_id=thread-managed")
        );

        let unmanaged = sessions
            .iter()
            .find(|session| session.control_path == "unmanaged")
            .unwrap();
        assert_eq!(unmanaged.provider, "claude");
        assert_eq!(unmanaged.presentation_state, "unmanaged");
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
        assert!(
            unmanaged
                .evidence
                .join_keys
                .iter()
                .any(|key| key == "source_path=/tmp/claude-unmanaged.jsonl")
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
            is_offline: true,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            sessions: Vec::new(),
            adaptive_backlog_limiter: None,
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
            disk_free_bytes: 10,
            is_offline: false,
            managed_sessions: Vec::new(),
            unmanaged_session_bindings: Vec::new(),
            sessions: Vec::new(),
            adaptive_backlog_limiter: None,
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
            spool: &spool,
            tracker: &tracker,
            parse_tracker: &parse_tracker,
            ship_stats: &ship_stats,
            is_offline: false,
            last_ship_at: payload.last_ship_at.clone(),
        };

        let status_path = dir.path().join("agent").join("engine-status.json");
        write_status_file(
            &payload,
            &stats,
            Vec::new(),
            PhaseLedgerStatus::Ok,
            Some(serde_json::json!({
                "enabled": true,
                "status": "connected",
                "supports": ["codex.launch"],
            })),
            &status_path,
        );

        let json = std::fs::read_to_string(status_path).unwrap();
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
        };
        let stats = HeartbeatStats {
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
        write_status_file(
            &payload,
            &stats,
            phase_ledger,
            PhaseLedgerStatus::Ok,
            None,
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
        };
        let stats = HeartbeatStats {
            spool: &spool,
            tracker: &tracker,
            parse_tracker: &parse_tracker,
            ship_stats: &ship_stats,
            is_offline: false,
            last_ship_at: None,
        };

        let status_path = dir.path().join("agent").join("engine-status.json");
        write_status_file(
            &payload,
            &stats,
            Vec::new(),
            PhaseLedgerStatus::ReadFailed("db locked".to_string()),
            None,
            &status_path,
        );

        let json = std::fs::read_to_string(status_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["phase_ledger"], serde_json::json!([]));
        assert_eq!(parsed["phase_ledger_status"], "read_failed: db locked");
    }
}
