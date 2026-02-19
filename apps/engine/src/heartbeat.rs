//! Periodic heartbeat emitter.
//!
//! Every 5 minutes, POSTs a heartbeat payload to `/api/agents/heartbeat`
//! and writes `~/.claude/engine-status.json` for local support/debugging.

use std::path::PathBuf;

use anyhow::Result;
use serde::Serialize;

use crate::error_tracker::ConsecutiveErrorTracker;
use crate::shipping::client::ShipperClient;
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
    pub parse_error_count_1h: u32,
    pub consecutive_ship_failures: u32,
    pub disk_free_bytes: u64,
    pub is_offline: bool,
}

/// Stats needed to build a heartbeat.
pub struct HeartbeatStats<'a> {
    pub spool: &'a Spool<'a>,
    pub tracker: &'a ConsecutiveErrorTracker,
    pub is_offline: bool,
    pub last_ship_at: Option<String>,
}

impl HeartbeatPayload {
    pub fn build(stats: &HeartbeatStats<'_>) -> Self {
        let spool_pending_count = stats.spool.pending_count().unwrap_or(0);
        let consecutive_ship_failures = stats.tracker.consecutive_count();
        let disk_free_bytes = get_disk_free();

        HeartbeatPayload {
            version: ENGINE_VERSION.to_string(),
            daemon_pid: std::process::id(),
            last_ship_at: stats.last_ship_at.clone(),
            spool_pending_count,
            parse_error_count_1h: 0, // placeholder — not tracked per-hour yet
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
pub fn write_status_file(payload: &HeartbeatPayload, claude_dir: &std::path::Path) {
    #[derive(Serialize)]
    struct StatusFile<'a> {
        #[serde(flatten)]
        payload: &'a HeartbeatPayload,
        last_updated: String,
    }

    let now = chrono::Utc::now().to_rfc3339();
    let status = StatusFile { payload, last_updated: now };

    let path = claude_dir.join("engine-status.json");
    if let Ok(json) = serde_json::to_string_pretty(&status) {
        let _ = std::fs::write(&path, json);
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

    #[test]
    fn test_heartbeat_payload_fields() {
        let payload = HeartbeatPayload {
            version: "0.1.0".to_string(),
            daemon_pid: 12345,
            last_ship_at: Some("2026-02-18T10:00:00Z".to_string()),
            spool_pending_count: 5,
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
}
