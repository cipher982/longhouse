//! Periodic heartbeat emitter.
//!
//! The daemon reuses this payload for two related jobs:
//!
//! - frequent local status-file writes for ambient UX / debugging
//! - less frequent server heartbeats to `/api/agents/heartbeat`

use std::sync::OnceLock;

use anyhow::Result;
use serde::Serialize;

use crate::build_identity::BuildIdentity;

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
    pub disk_free_bytes: u64,
    pub is_offline: bool,
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
            disk_free_bytes,
            is_offline: stats.is_offline,
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
    client.post_json("/api/agents/heartbeat", json).await
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
        .and_then(|st| chrono::DateTime::<chrono::Utc>::from(st).to_rfc3339().into());
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
            disk_free_bytes: 1_000_000_000,
            is_offline: false,
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
        assert_eq!(parsed["is_offline"], false);
        assert!(parsed["last_ship_at"].is_string());
        assert!(parsed["last_ship_attempt_at"].is_string());
        assert_eq!(parsed["last_ship_result"], "ok");
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
            is_offline: true,
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
            disk_free_bytes: 10,
            is_offline: false,
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
            &status_path,
        );

        let json = std::fs::read_to_string(status_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["phase_ledger"], serde_json::json!([]));
        assert_eq!(parsed["phase_ledger_status"], "read_failed: db locked");
    }
}
