//! Startup refusals recorded for local health.

use super::*;

/// Write the reason a daemon start was refused, for `local-health` to read.
///
/// Best-effort by construction: the process is already exiting on a real error,
/// and failing to write the explanation must not replace it with an I/O error.
pub(super) fn record_startup_refusal(reason: &str, message: &str) {
    let Ok(status_path) = config::get_agent_status_path() else {
        return;
    };
    if let Some(parent) = status_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    // Deliberately no engine_pulse_at: the daemon is exiting, and a fresh pulse
    // would tell every local-health surface the engine is alive. The file exists
    // only to carry the reason; liveness still reads as down, which is true.
    // The pid and time name this attempt, so `machine repair` can see that the
    // restarted agent answered (with a refusal) instead of waiting out its
    // whole window for a sample that will never come.
    let payload = serde_json::json!({
        "daemon_pid": std::process::id(),
        "last_updated": chrono::Utc::now().to_rfc3339(),
        "startup_refused": true,
        "startup_refusal_kind": reason,
        "startup_refusal": message,
    });
    if let Ok(serialized) = serde_json::to_vec_pretty(&payload) {
        let _ = std::fs::write(&status_path, serialized);
    }
}

pub(super) fn startup_storage_reason(error: &anyhow::Error) -> &'static str {
    let code = error.chain().find_map(|cause| {
        cause
            .downcast_ref::<rusqlite::Error>()
            .and_then(rusqlite::Error::sqlite_error_code)
    });
    match code {
        Some(rusqlite::ErrorCode::NotADatabase | rusqlite::ErrorCode::DatabaseCorrupt) => {
            "state_database_corrupt"
        }
        Some(rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked) => {
            "state_database_locked"
        }
        Some(rusqlite::ErrorCode::ReadOnly | rusqlite::ErrorCode::PermissionDenied) => {
            "state_database_readonly"
        }
        Some(rusqlite::ErrorCode::DiskFull) => "disk_full",
        _ => "state_database_unavailable",
    }
}
