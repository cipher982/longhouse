//! Managed-local Cursor Helm scanner.
//!
//! `longhouse cursor` (the Helm launcher) writes a private per-session state
//! file under `~/.longhouse/managed-local/cursor-helm/<session>.json` and binds
//! a Unix socket the engine forwards remote commands to. The Machine Agent
//! scans that directory each heartbeat and emits a managed-session lease for
//! every live launcher, so the Runtime Host promotes the connection
//! detached -> attached and the UI offers send/interrupt/terminate. Dead state
//! (launcher pid gone or socket missing) is omitted so the server detaches.

use std::fs;
use std::path::Path;
use std::collections::HashMap;
use std::path::PathBuf;

use serde::Deserialize;

pub use crate::cursor_helm_control::default_cursor_helm_state_dir;
use crate::process_identity::ProcessFact;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CursorHelmObservation {
    pub session_id: String,
    pub state_file: PathBuf,
    pub socket_path: Option<PathBuf>,
    pub cwd: Option<String>,
    pub launcher_pid: Option<u32>,
    pub cursor_pid: Option<u32>,
    pub started_at: String,
    pub updated_at: String,
    /// Launcher pid is alive AND the control socket exists — the session is
    /// remotely steerable right now.
    pub live: bool,
}

#[derive(Debug, Deserialize)]
struct CursorHelmStateFile {
    session_id: Option<String>,
    #[serde(default)]
    socket_path: Option<String>,
    #[serde(default)]
    launcher_pid: Option<u32>,
    #[serde(default)]
    cursor_pid: Option<u32>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    started_at: Option<String>,
    #[serde(default)]
    updated_at: Option<String>,
    /// Launcher publishes ready=true only after the provider child is running.
    /// Missing/false must not produce a live remote-control lease.
    #[serde(default)]
    ready: Option<bool>,
}

pub(crate) fn collect_observations_from_processes(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<CursorHelmObservation> {
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
        let Ok(state) = serde_json::from_slice::<CursorHelmStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        let socket_str = state.socket_path.unwrap_or_default().trim().to_string();
        let socket_path = if socket_str.is_empty() {
            None
        } else {
            Some(PathBuf::from(socket_str))
        };
        let launcher_alive = state
            .launcher_pid
            .and_then(|pid| process_facts.get(&pid))
            .is_some_and(|fact| {
                fact.command.contains("cursor_helm")
                    || (fact.command.contains("longhouse") && fact.command.contains("cursor"))
            });
        let socket_present = socket_path.as_ref().map(|p| p.exists()).unwrap_or(false);
        let ready = state.ready.unwrap_or(false);
        let live = launcher_alive && socket_present && ready;
        out.push(CursorHelmObservation {
            session_id,
            state_file: path,
            socket_path,
            cwd: state.cwd.filter(|value| !value.trim().is_empty()),
            launcher_pid: state.launcher_pid,
            cursor_pid: state.cursor_pid,
            started_at: state.started_at.unwrap_or_default(),
            updated_at: state.updated_at.unwrap_or_default(),
            live,
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_dir() -> tempfile::TempDir {
        tempfile::TempDir::new().unwrap()
    }

    fn launcher_facts(pid: u32) -> HashMap<u32, ProcessFact> {
        HashMap::from([(
            pid,
            ProcessFact {
                pid,
                tty: "ttys001".to_string(),
                stat: "S+".to_string(),
                lstart: String::new(),
                command: "python -m zerg.cli longhouse cursor".to_string(),
                start_time: None,
            },
        )])
    }

    fn write_state(dir: &Path, session_id: &str, socket: &Path, launcher_pid: Option<u32>) {
        write_state_with_ready(dir, session_id, socket, launcher_pid, true);
    }

    fn write_state_with_ready(
        dir: &Path,
        session_id: &str,
        socket: &Path,
        launcher_pid: Option<u32>,
        ready: bool,
    ) {
        let mut value = json!({
            "session_id": session_id,
            "socket_path": socket.to_string_lossy(),
            "cursor_pid": 99999,
            "ready": ready,
            "started_at": "2026-06-30T00:00:00Z",
            "updated_at": "2026-06-30T00:00:00Z",
        });
        if let Some(pid) = launcher_pid {
            value["launcher_pid"] = json!(pid);
        }
        fs::write(dir.join(format!("{session_id}.json")), value.to_string()).unwrap();
    }

    #[test]
    fn empty_dir_yields_no_observations() {
        let dir = tmp_dir();
        assert!(collect_observations_from_processes(dir.path(), &HashMap::new()).is_empty());
    }

    #[test]
    fn live_session_has_live_flag_when_pid_and_socket_present() {
        let dir = tmp_dir();
        let socket = dir.path().join("a.sock");
        // create the socket file so exists() is true (a real bind isn't needed
        // for the scan's exists() check)
        fs::File::create(&socket).unwrap();
        // use this process's pid as the "launcher" so pid_alive is true
        write_state(dir.path(), "sess-live", &socket, Some(std::process::id()));
        let obs = collect_observations_from_processes(
            dir.path(),
            &launcher_facts(std::process::id()),
        );
        let live = obs.iter().find(|o| o.session_id == "sess-live").unwrap();
        assert!(live.live);
    }

    #[test]
    fn dead_pid_is_not_live() {
        let dir = tmp_dir();
        let socket = dir.path().join("b.sock");
        fs::File::create(&socket).unwrap();
        write_state(dir.path(), "sess-dead-pid", &socket, Some(2_000_000));
        let obs = collect_observations_from_processes(dir.path(), &HashMap::new());
        let dead = obs
            .iter()
            .find(|o| o.session_id == "sess-dead-pid")
            .unwrap();
        assert!(!dead.live);
    }

    #[test]
    fn missing_socket_is_not_live() {
        let dir = tmp_dir();
        let socket = dir.path().join("absent.sock");
        write_state(
            dir.path(),
            "sess-no-sock",
            &socket,
            Some(std::process::id()),
        );
        let obs = collect_observations_from_processes(
            dir.path(),
            &launcher_facts(std::process::id()),
        );
        let no_sock = obs.iter().find(|o| o.session_id == "sess-no-sock").unwrap();
        assert!(!no_sock.live);
    }

    #[test]
    fn not_ready_is_not_live_even_with_pid_and_socket() {
        let dir = tmp_dir();
        let socket = dir.path().join("c.sock");
        fs::File::create(&socket).unwrap();
        write_state_with_ready(
            dir.path(),
            "sess-not-ready",
            &socket,
            Some(std::process::id()),
            false,
        );
        let obs = collect_observations_from_processes(
            dir.path(),
            &launcher_facts(std::process::id()),
        );
        let pending = obs
            .iter()
            .find(|o| o.session_id == "sess-not-ready")
            .unwrap();
        assert!(!pending.live);
    }

    #[test]
    fn missing_session_id_is_skipped() {
        let dir = tmp_dir();
        let value = json!({"socket_path": dir.path().join("x.sock").to_string_lossy()});
        fs::write(dir.path().join("nope.json"), value.to_string()).unwrap();
        let obs = collect_observations_from_processes(dir.path(), &HashMap::new());
        assert!(obs.iter().all(|o| !o.session_id.is_empty()));
    }
}
