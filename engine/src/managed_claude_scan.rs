//! Managed-local Claude channel scanner.
//!
//! Walks `~/.claude/channels/longhouse/sessions/*.json` and reports the
//! process facts needed for the engine heartbeat. The state file contains a
//! session-scoped channel token, so this module deliberately emits only pid and
//! readiness metadata.

use std::fs;
use std::path::Path;
use std::path::PathBuf;

use serde::Deserialize;

use crate::managed_bridge_scan::pid_alive;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaudeChannelObservation {
    pub session_id: String,
    pub provider_session_id: Option<String>,
    pub state_file: PathBuf,
    pub claude_pid: Option<u32>,
    pub bridge_pid: Option<u32>,
    pub ready: bool,
    pub updated_at: String,
    pub claude_alive: bool,
    pub bridge_alive: bool,
}

#[derive(Debug, Deserialize)]
struct ClaudeChannelStateFile {
    session_id: Option<String>,
    provider_session_id: Option<String>,
    claude_pid: Option<u32>,
    bridge_pid: Option<u32>,
    ready: Option<bool>,
    updated_at: Option<String>,
}

pub fn collect_observations() -> Vec<ClaudeChannelObservation> {
    let Some(state_dir) = default_claude_channel_state_dir() else {
        return Vec::new();
    };
    collect_observations_from(&state_dir)
}

pub fn default_claude_channel_state_dir() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(
        PathBuf::from(home)
            .join(".claude")
            .join("channels")
            .join("longhouse")
            .join("sessions"),
    )
}

pub fn collect_observations_from(state_dir: &Path) -> Vec<ClaudeChannelObservation> {
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(state_dir) else {
        return out;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = fs::read(&path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<ClaudeChannelStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        let claude_alive = state
            .claude_pid
            .and_then(|pid| i32::try_from(pid).ok())
            .map(pid_alive)
            .unwrap_or(false);
        let bridge_alive = state
            .bridge_pid
            .and_then(|pid| i32::try_from(pid).ok())
            .map(pid_alive)
            .unwrap_or(false);

        out.push(ClaudeChannelObservation {
            session_id,
            provider_session_id: state.provider_session_id,
            state_file: path,
            claude_pid: state.claude_pid,
            bridge_pid: state.bridge_pid,
            ready: state.ready.unwrap_or(false),
            updated_at: state.updated_at.unwrap_or_default(),
            claude_alive,
            bridge_alive,
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scan_skips_malformed_and_non_json_files() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("broken.json"), b"not-json").unwrap();
        fs::write(tmp.path().join("note.txt"), b"{}").unwrap();

        assert!(collect_observations_from(tmp.path()).is_empty());
    }

    #[test]
    fn scan_loads_channel_state_without_secret_fields() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            br#"{
              "session_id": "09b68f98-1e31-458e-b78a-6dfd062ead75",
              "provider_session_id": "09b68f98-1e31-458e-b78a-6dfd062ead75",
              "auth_token": "do-not-emit",
              "claude_pid": 999999,
              "bridge_pid": 999998,
              "ready": true,
              "updated_at": "2026-05-07T20:03:50Z"
            }"#,
        )
        .unwrap();

        let observations = collect_observations_from(tmp.path());

        assert_eq!(observations.len(), 1);
        let obs = &observations[0];
        assert_eq!(obs.session_id, "09b68f98-1e31-458e-b78a-6dfd062ead75");
        assert_eq!(
            obs.provider_session_id.as_deref(),
            Some("09b68f98-1e31-458e-b78a-6dfd062ead75")
        );
        assert_eq!(obs.claude_pid, Some(999999));
        assert_eq!(obs.bridge_pid, Some(999998));
        assert!(obs.ready);
        assert!(!obs.claude_alive);
        assert!(!obs.bridge_alive);
    }

    #[test]
    fn scan_marks_current_process_alive() {
        let tmp = tempfile::tempdir().unwrap();
        let pid = std::process::id();
        fs::write(
            tmp.path().join("session.json"),
            format!(
                r#"{{
                  "session_id": "09b68f98-1e31-458e-b78a-6dfd062ead75",
                  "claude_pid": {pid},
                  "bridge_pid": {pid},
                  "ready": true,
                  "updated_at": "2026-05-07T20:03:50Z"
                }}"#
            ),
        )
        .unwrap();

        let observations = collect_observations_from(tmp.path());

        assert_eq!(observations.len(), 1);
        assert!(observations[0].claude_alive);
        assert!(observations[0].bridge_alive);
    }
}
