//! Periodic heartbeat emitter.
//!
//! The daemon reuses this payload for two related jobs:
//!
//! - frequent local status-file writes for ambient UX / debugging
//! - less frequent server heartbeats to `/api/agents/heartbeat`

use std::path::PathBuf;

use anyhow::Result;
use serde::Serialize;

use crate::error_tracker::ConsecutiveErrorTracker;
use crate::error_tracker::RecentIssueTracker;
use crate::shipping::client::ShipperClient;
use crate::state::spool::DeadLetterEntry;
use crate::state::spool::Spool;

const ENGINE_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Heartbeat payload sent to the server and written locally.
#[derive(Debug, Serialize, Clone)]
pub struct HeartbeatPayload {
    pub version: String,
    pub daemon_pid: u32,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_ship_at: Option<String>,
    pub spool_pending_count: usize,
    pub spool_dead_count: usize,
    pub parse_error_count_1h: u32,
    pub consecutive_ship_failures: u32,
    pub disk_free_bytes: u64,
    pub is_offline: bool,
}

/// Stats needed to build a heartbeat.
pub struct HeartbeatStats<'a> {
    pub spool: &'a Spool<'a>,
    pub tracker: &'a ConsecutiveErrorTracker,
    pub parse_tracker: &'a RecentIssueTracker,
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

        HeartbeatPayload {
            version: ENGINE_VERSION.to_string(),
            daemon_pid: std::process::id(),
            last_ship_at: stats.last_ship_at.clone(),
            spool_pending_count,
            spool_dead_count,
            parse_error_count_1h,
            consecutive_ship_failures,
            disk_free_bytes,
            is_offline: stats.is_offline,
        }
    }
}

/// Send heartbeat to server via the existing authenticated client.
pub async fn send_heartbeat(client: &ShipperClient, payload: &HeartbeatPayload) -> Result<()> {
    let json = serde_json::to_vec(payload)?;
    client.post_json("/api/agents/heartbeat", json).await
}

/// Write status to `~/.claude/engine-status.json`.
pub fn write_status_file(
    payload: &HeartbeatPayload,
    stats: &HeartbeatStats<'_>,
    claude_dir: &std::path::Path,
) {
    #[derive(Serialize)]
    struct StatusFile<'a> {
        #[serde(flatten)]
        payload: &'a HeartbeatPayload,
        recent_dead_letters: Vec<StatusDeadLetter>,
        last_updated: String,
    }

    let recent_dead_letters = stats
        .spool
        .recent_dead(5)
        .unwrap_or_default()
        .into_iter()
        .map(status_dead_letter_from_entry)
        .collect();
    let now = chrono::Utc::now().to_rfc3339();
    let status = StatusFile {
        payload,
        recent_dead_letters,
        last_updated: now,
    };

    let path = claude_dir.join("engine-status.json");
    if let Ok(json) = serde_json::to_string_pretty(&status) {
        let _ = std::fs::write(&path, json);
    }
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

/// Get free bytes on the filesystem containing `~/.claude`.
fn get_disk_free() -> u64 {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    let claude_dir = PathBuf::from(home).join(".claude");
    disk_free_bytes(&claude_dir)
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
            spool_pending_count: 5,
            spool_dead_count: 1,
            parse_error_count_1h: 0,
            consecutive_ship_failures: 2,
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
        assert_eq!(parsed["is_offline"], false);
        assert!(parsed["last_ship_at"].is_string());
    }

    #[test]
    fn test_heartbeat_payload_no_last_ship() {
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 1,
            last_ship_at: None,
            spool_pending_count: 0,
            spool_dead_count: 0,
            parse_error_count_1h: 0,
            consecutive_ship_failures: 0,
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
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 42,
            last_ship_at: Some("2026-03-10T00:00:00Z".to_string()),
            spool_pending_count: 2,
            spool_dead_count: 3,
            parse_error_count_1h: 0,
            consecutive_ship_failures: 0,
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
            is_offline: false,
            last_ship_at: payload.last_ship_at.clone(),
        };

        write_status_file(&payload, &stats, dir.path());

        let json = std::fs::read_to_string(dir.path().join("engine-status.json")).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["spool_dead_count"], 3);
        assert_eq!(parsed["recent_dead_letters"][0]["provider"], "codex");
        assert_eq!(
            parsed["recent_dead_letters"][0]["file_path"],
            "/tmp/dead-range.jsonl"
        );
        assert_eq!(parsed["recent_dead_letters"][0]["range_bytes"], 120);
    }
}
