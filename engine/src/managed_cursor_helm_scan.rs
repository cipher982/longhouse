//! Managed-local Cursor Helm scanner.
//!
//! `longhouse cursor` (the Helm launcher) writes a private per-session state
//! file under `~/.longhouse/managed-local/cursor-helm/<session>.json` and binds
//! a Unix socket the engine forwards remote commands to. The Machine Agent
//! scans that directory each heartbeat and emits a managed-session lease for
//! every live launcher, so the Runtime Host promotes the connection
//! detached -> attached and the UI offers send/interrupt/terminate. Dead state
//! (launcher pid gone or socket missing) is omitted so the server detaches.

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;

use serde::Deserialize;

pub use crate::cursor_helm_control::default_cursor_helm_state_dir;
use crate::process_identity::{lstart_matches_recorded, ProcessFact};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CursorHelmObservation {
    pub session_id: String,
    pub state_file: PathBuf,
    pub socket_path: Option<PathBuf>,
    pub cwd: Option<String>,
    pub launcher_pid: Option<u32>,
    pub launcher_process_start_time: Option<String>,
    pub cursor_pid: Option<u32>,
    pub cursor_process_start_time: Option<String>,
    pub started_at: String,
    pub updated_at: String,
    /// Identity-valid launcher process fact, independent of control readiness.
    pub launcher_alive: bool,
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
    launcher_process_start_time: Option<String>,
    #[serde(default)]
    cursor_pid: Option<u32>,
    #[serde(default)]
    cursor_process_start_time: Option<String>,
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
    let paths = state_file_paths(state_dir);
    collect_observations_from_paths(&paths, process_facts)
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<CursorHelmObservation> {
    let mut out = Vec::new();
    for path in paths {
        let path = path.as_path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = fs::read(path) else {
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
                (fact.command.contains("cursor_helm")
                    || (fact.command.contains("longhouse") && fact.command.contains("cursor")))
                    && state
                        .launcher_process_start_time
                        .as_deref()
                        .filter(|recorded| !recorded.trim().is_empty())
                        .is_some_and(|recorded| lstart_matches_recorded(fact, recorded))
            });
        let cursor_pid = state.cursor_pid.filter(|pid| {
            process_facts.get(pid).is_some_and(|fact| {
                fact.command.contains("cursor-agent")
                    && state
                        .cursor_process_start_time
                        .as_deref()
                        .filter(|recorded| !recorded.trim().is_empty())
                        .is_some_and(|recorded| lstart_matches_recorded(fact, recorded))
            })
        });
        let socket_present = socket_path.as_ref().map(|p| p.exists()).unwrap_or(false);
        let ready = state.ready.unwrap_or(false);
        let live = launcher_alive && cursor_pid.is_some() && socket_present && ready;
        out.push(CursorHelmObservation {
            session_id,
            state_file: path.to_path_buf(),
            socket_path,
            cwd: state.cwd.filter(|value| !value.trim().is_empty()),
            launcher_pid: state.launcher_pid,
            launcher_process_start_time: state.launcher_process_start_time,
            cursor_pid,
            cursor_process_start_time: state.cursor_process_start_time,
            started_at: state.started_at.unwrap_or_default(),
            updated_at: state.updated_at.unwrap_or_default(),
            launcher_alive,
            live,
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    out
}

fn state_file_paths(state_dir: &Path) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    let Ok(entries) = fs::read_dir(state_dir) else {
        return paths;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        paths.push(path);
    }
    paths
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn tmp_dir() -> tempfile::TempDir {
        tempfile::TempDir::new().unwrap()
    }

    fn launcher_facts(pid: u32) -> HashMap<u32, ProcessFact> {
        HashMap::from([
            (
                pid,
                ProcessFact {
                    pid,
                    tty: "ttys001".to_string(),
                    stat: "S+".to_string(),
                    lstart: "Tue Jun 30 00:00:00 2026".to_string(),
                    command: "python -m zerg.cli longhouse cursor".to_string(),
                    start_time: None,
                },
            ),
            (
                99999,
                ProcessFact {
                    pid: 99999,
                    tty: "ttys001".to_string(),
                    stat: "S+".to_string(),
                    lstart: "Tue Jun 30 00:00:01 2026".to_string(),
                    command: "/usr/local/bin/cursor-agent".to_string(),
                    start_time: None,
                },
            ),
        ])
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
            "launcher_process_start_time": "Tue Jun 30 00:00:00 2026",
            "cursor_pid": 99999,
            "cursor_process_start_time": "Tue Jun 30 00:00:01 2026",
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
        let obs =
            collect_observations_from_processes(dir.path(), &launcher_facts(std::process::id()));
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
    fn recycled_launcher_pid_is_not_live() {
        let dir = tmp_dir();
        let socket = dir.path().join("reused.sock");
        fs::File::create(&socket).unwrap();
        write_state(dir.path(), "sess-reused", &socket, Some(4242));
        let mut facts = launcher_facts(4242);
        facts.get_mut(&4242).unwrap().lstart = "Wed Jul  1 00:00:00 2026".to_string();

        let observations = collect_observations_from_processes(dir.path(), &facts);

        assert!(!observations[0].live);
    }

    #[test]
    fn recycled_cursor_child_pid_is_not_live() {
        let dir = tmp_dir();
        let socket = dir.path().join("reused-child.sock");
        fs::File::create(&socket).unwrap();
        write_state(dir.path(), "sess-reused-child", &socket, Some(4242));
        let mut facts = launcher_facts(4242);
        facts.get_mut(&99999).unwrap().lstart = "Wed Jul  1 00:00:00 2026".to_string();

        let observations = collect_observations_from_processes(dir.path(), &facts);

        assert!(!observations[0].live);
        assert!(observations[0].cursor_pid.is_none());
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
        let obs =
            collect_observations_from_processes(dir.path(), &launcher_facts(std::process::id()));
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
        let obs =
            collect_observations_from_processes(dir.path(), &launcher_facts(std::process::id()));
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
