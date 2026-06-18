//! Managed-local OpenCode server-bridge scanner.
//!
//! `longhouse opencode` starts stock `opencode serve` and writes a private
//! state file under the provider config home. The Machine Agent must include
//! that bridge in its complete managed-session heartbeat; otherwise the Runtime
//! Host correctly interprets the missing session as detached.

use std::fs;
use std::path::Path;
use std::path::PathBuf;

use serde::Deserialize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpenCodeServerObservation {
    pub session_id: String,
    pub provider_session_id: String,
    pub state_file: PathBuf,
    pub cwd: Option<String>,
    pub server_url: Option<String>,
    pub pid: Option<u32>,
    pub started_at: String,
    pub updated_at: String,
    pub server_alive: bool,
}

#[derive(Debug, Deserialize)]
struct OpenCodeServerStateFile {
    session_id: Option<String>,
    provider_session_id: Option<String>,
    server_url: Option<String>,
    pid: Option<u32>,
    cwd: Option<String>,
    started_at: Option<String>,
    updated_at: Option<String>,
}

pub fn collect_observations() -> Vec<OpenCodeServerObservation> {
    let Some(state_dir) = default_opencode_server_state_dir() else {
        return Vec::new();
    };
    collect_observations_from(&state_dir)
}

pub fn default_opencode_server_state_dir() -> Option<PathBuf> {
    let provider_home = std::env::var_os("CLAUDE_CONFIG_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".claude")))?;
    Some(provider_home.join("managed-local").join("opencode-server"))
}

pub fn collect_observations_from(state_dir: &Path) -> Vec<OpenCodeServerObservation> {
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
        let Ok(state) = serde_json::from_slice::<OpenCodeServerStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        let provider_session_id = state
            .provider_session_id
            .unwrap_or_default()
            .trim()
            .to_string();
        if session_id.is_empty() || provider_session_id.is_empty() {
            continue;
        }
        let server_alive = state
            .pid
            .and_then(|pid| i32::try_from(pid).ok())
            .map(crate::managed_bridge_scan::pid_alive)
            .unwrap_or(false);

        out.push(OpenCodeServerObservation {
            session_id,
            provider_session_id,
            state_file: path,
            cwd: state.cwd.filter(|value| !value.trim().is_empty()),
            server_url: state.server_url.filter(|value| !value.trim().is_empty()),
            pid: state.pid,
            started_at: state.started_at.unwrap_or_default(),
            updated_at: state.updated_at.unwrap_or_default(),
            server_alive,
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_state_dir_uses_provider_home() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        let claude_home = temp.path().join("claude-config");
        temp_env::with_vars(
            [
                ("HOME", Some(home.display().to_string())),
                ("CLAUDE_CONFIG_DIR", Some(claude_home.display().to_string())),
            ],
            || {
                assert_eq!(
                    default_opencode_server_state_dir().unwrap(),
                    claude_home.join("managed-local").join("opencode-server")
                );
            },
        );
    }

    #[test]
    fn scan_redacts_secret_state_to_public_observation() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": "longhouse-session",
                "provider_session_id": "opencode-session",
                "server_url": "http://127.0.0.1:12345",
                "pid": 999999,
                "cwd": "/Users/test/repo",
                "username": "opencode",
                "password": "secret",
                "started_at": "2026-06-17T10:00:00Z",
                "updated_at": "2026-06-17T10:00:01Z"
            })
            .to_string(),
        )
        .unwrap();

        let obs = collect_observations_from(tmp.path());

        assert_eq!(obs.len(), 1);
        assert_eq!(obs[0].session_id, "longhouse-session");
        assert_eq!(obs[0].provider_session_id, "opencode-session");
        assert_eq!(obs[0].cwd.as_deref(), Some("/Users/test/repo"));
        assert_eq!(obs[0].server_url.as_deref(), Some("http://127.0.0.1:12345"));
        assert!(!obs[0].server_alive);
    }
}
