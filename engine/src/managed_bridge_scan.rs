//! Managed-local Codex bridge scanner.
//!
//! Walks the configured Longhouse codex-bridge state directory once per tick and
//! produces `Vec<CodexBridgeObservation>` with the raw signals needed by
//! both the heartbeat lease builder and the orphan-bridge reaper.
//!
//! Extracted so there is a single source of truth for process-scan +
//! lock-held + state-file parsing. Previously the lease builder computed
//! a collapsed `attached|degraded|detached` view and dropped fields the
//! reaper needs (`active_turn_id`, `last_turn_status`,
//! `has_tui_attachment`, app-server pid/pgid).

use std::collections::HashMap;
use std::fs;
use std::fs::OpenOptions;
use std::path::Path;
use std::path::PathBuf;

use crate::codex_bridge::BridgeStateFile;
use crate::process_identity::{command_contains_basename, lstart_matches_recorded, ProcessFact};

/// Raw, per-bridge signals captured by a single scan pass.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CodexBridgeObservation {
    pub session_id: String,
    pub state_file: PathBuf,
    pub schema_version: u32,
    pub cwd: Option<String>,
    pub launch_mode: Option<String>,
    pub ws_url: Option<String>,
    pub status: String,
    pub thread_id: Option<String>,
    #[allow(dead_code)]
    pub thread_path: Option<String>,
    pub active_turn_id: Option<String>,
    pub last_turn_status: Option<String>,
    pub last_error: Option<String>,
    pub thread_subscription_status: Option<String>,
    pub bridge_pid: u32,
    pub app_server_pid: Option<u32>,
    pub app_server_pgid: Option<i32>,
    pub updated_at: String,
    /// `true` when the bridge holds its exclusive `.lock`; that is the
    /// authoritative signal that the bridge daemon process is alive.
    pub bridge_alive: bool,
    /// `true` when at least one process in the system has
    /// `--remote <ws_url>` in its command line.
    pub has_tui_attachment: bool,
    /// `true` when the recorded `app_server_pid` / `app_server_pgid`
    /// still exists. Used for the Class-B orphan (bridge daemon died,
    /// app-server still listening).
    pub app_server_alive: bool,
}

pub fn default_codex_bridge_state_dir() -> Option<PathBuf> {
    crate::config::get_codex_bridge_state_dir().ok()
}

#[cfg(test)]
pub fn codex_tui_process_attached(process_commands: &[String], ws_url: &str) -> bool {
    let normalized_ws = ws_url.trim();
    if normalized_ws.is_empty() {
        return false;
    }
    process_commands
        .iter()
        .any(|command| command.contains(normalized_ws) && command.contains("--remote"))
}

/// `true` if process with `pid` still exists (via `kill(pid, 0)`).
#[cfg(unix)]
pub fn pid_alive(pid: i32) -> bool {
    if pid <= 0 {
        return false;
    }
    // SAFETY: signal 0 just checks for existence; no side effects.
    unsafe { libc::kill(pid, 0) == 0 }
}

#[cfg(not(unix))]
pub fn pid_alive(_pid: i32) -> bool {
    false
}

/// `true` when the `.lock` file adjacent to `state_file` is held in
/// exclusive write mode by some other process. That is the signal the
/// bridge daemon is alive.
pub fn bridge_lock_is_held(state_file: &Path) -> bool {
    let lock_path = state_file.with_extension("lock");
    let Ok(file) = OpenOptions::new()
        .read(true)
        .write(true)
        .truncate(false)
        .open(lock_path)
    else {
        return false;
    };
    let mut lock = fd_lock::RwLock::new(file);
    let is_held = match lock.try_write() {
        Ok(_guard) => false,
        Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => true,
        Err(_) => false,
    };
    is_held
}

/// Build observations from a specific state dir + injected process list.
/// Exposed for tests; production path uses `collect_observations`.
pub fn collect_observations_from(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<CodexBridgeObservation> {
    let paths = state_file_paths(state_dir);
    collect_observations_from_paths(&paths, process_facts)
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<CodexBridgeObservation> {
    let mut out = Vec::new();
    for path in paths {
        let path = path.as_path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = fs::read(path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<BridgeStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        let bridge_alive = bridge_lock_is_held(path);
        let has_tui_attachment = state.ws_url.as_deref().is_some_and(|ws| {
            process_facts
                .values()
                .any(|fact| fact.command.contains(ws) && fact.command.contains("--remote"))
        });
        let app_server_alive = state
            .app_server_pid
            .and_then(|pid| process_facts.get(&pid))
            .is_some_and(|fact| {
                command_contains_basename(&fact.command, "codex")
                    && fact
                        .command
                        .split_whitespace()
                        .any(|part| part == "app-server")
                    && state
                        .app_server_process_start_time
                        .as_deref()
                        .filter(|recorded| !recorded.trim().is_empty())
                        .is_some_and(|recorded| lstart_matches_recorded(fact, recorded))
            });

        out.push(CodexBridgeObservation {
            session_id,
            state_file: path.to_path_buf(),
            schema_version: state.schema_version,
            cwd: Some(state.cwd),
            launch_mode: state.launch_mode,
            ws_url: state.ws_url,
            status: state.status,
            thread_id: state.thread_id,
            thread_path: state.thread_path,
            active_turn_id: state.active_turn_id,
            last_turn_status: state.last_turn_status,
            last_error: state.last_error,
            thread_subscription_status: state.thread_subscription_status,
            bridge_pid: state.pid,
            app_server_pid: state.app_server_pid,
            app_server_pgid: state.app_server_pgid,
            updated_at: state.updated_at,
            bridge_alive,
            has_tui_attachment,
            app_server_alive,
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

    #[test]
    fn ps_match_requires_both_remote_and_ws_url() {
        let commands = vec![
            "/opt/homebrew/bin/codex --enable tui_app_server --remote ws://127.0.0.1:65268"
                .to_string(),
            "/opt/homebrew/bin/codex resume thr_123 --enable tui_app_server --remote ws://127.0.0.1:65269"
                .to_string(),
            "/bin/bash".to_string(),
            "node /opt/homebrew/bin/codex -c foo=bar app-server --listen ws://127.0.0.1:0"
                .to_string(),
        ];
        assert!(codex_tui_process_attached(
            &commands,
            "ws://127.0.0.1:65268"
        ));
        assert!(codex_tui_process_attached(
            &commands,
            "ws://127.0.0.1:65269"
        ));
        assert!(!codex_tui_process_attached(
            &commands,
            "ws://127.0.0.1:9999"
        ));
        assert!(!codex_tui_process_attached(&commands, ""));
    }

    #[test]
    fn scan_skips_non_json_and_malformed() {
        let tmp = tempfile::tempdir().unwrap();
        // malformed
        fs::write(tmp.path().join("broken.json"), b"not-json").unwrap();
        // wrong extension
        fs::write(tmp.path().join("note.txt"), b"{}").unwrap();
        let obs = collect_observations_from(tmp.path(), &HashMap::new());
        assert!(obs.is_empty());
    }

    #[test]
    fn current_row_scan_uses_only_retained_paths_and_refreshes_tui_evidence() {
        let tmp = tempfile::tempdir().unwrap();
        let retained = tmp.path().join("retained.json");
        let undiscovered = tmp.path().join("undiscovered.json");
        for (path, session_id, ws_url) in [
            (&retained, "retained-session", "ws://127.0.0.1:65001"),
            (&undiscovered, "new-session", "ws://127.0.0.1:65002"),
        ] {
            fs::write(
                path,
                serde_json::json!({
                    "schema_version": 1,
                    "session_id": session_id,
                    "cwd": "/tmp",
                    "codex_bin": "codex",
                    "ws_url": ws_url,
                    "thread_id": "thread-1",
                    "pid": 111,
                    "status": "ready",
                    "log_file": "/tmp/codex.log",
                    "updated_at": "2026-06-30T00:00:00Z"
                })
                .to_string(),
            )
            .unwrap();
        }

        let without_tui =
            collect_observations_from_paths(std::slice::from_ref(&retained), &HashMap::new());
        assert_eq!(without_tui.len(), 1);
        assert_eq!(without_tui[0].session_id, "retained-session");
        assert!(!without_tui[0].has_tui_attachment);

        let tui = ProcessFact {
            pid: 222,
            tty: "ttys001".to_string(),
            stat: "S+".to_string(),
            lstart: "Tue Jun 30 00:00:00 2026".to_string(),
            command: "codex --remote ws://127.0.0.1:65001".to_string(),
            start_time: None,
        };
        let with_tui = collect_observations_from_paths(
            std::slice::from_ref(&retained),
            &HashMap::from([(222, tui)]),
        );
        assert_eq!(with_tui.len(), 1);
        assert!(with_tui[0].has_tui_attachment);
        assert_eq!(collect_observations_from(tmp.path(), &HashMap::new()).len(), 2);
    }

    #[test]
    fn recycled_app_server_pid_is_not_alive() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": "managed-codex",
                "cwd": "/tmp",
                "codex_bin": "codex",
                "ws_url": null,
                "thread_id": "thread-1",
                "thread_path": null,
                "pid": 111,
                "app_server_pid": 4242,
                "app_server_pgid": 4242,
                "app_server_process_start_time": "Tue Jun 30 00:00:00 2026",
                "status": "ready",
                "log_file": "/tmp/codex.log",
                "active_turn_id": null,
                "last_turn_status": null,
                "last_error": null,
                "updated_at": "2026-06-30T00:00:00Z"
            })
            .to_string(),
        )
        .unwrap();
        let mut fact = ProcessFact {
            pid: 4242,
            tty: "??".to_string(),
            stat: "S".to_string(),
            lstart: "Wed Jul  1 00:00:00 2026".to_string(),
            command: "codex app-server".to_string(),
            start_time: None,
        };
        fact.start_time = crate::process_identity::parse_rfc3339("2026-07-01T00:00:00Z");

        let observations = collect_observations_from(tmp.path(), &HashMap::from([(4242, fact)]));

        assert_eq!(observations.len(), 1);
        assert!(!observations[0].app_server_alive);
    }

    #[test]
    fn app_server_without_recorded_process_start_is_not_alive() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            serde_json::json!({
                "schema_version": 1,
                "session_id": "managed-codex",
                "cwd": "/tmp",
                "codex_bin": "codex",
                "pid": 111,
                "app_server_pid": 4242,
                "status": "ready",
                "log_file": "/tmp/codex.log",
                "updated_at": "2026-06-30T00:00:00Z"
            })
            .to_string(),
        )
        .unwrap();
        let fact = ProcessFact {
            pid: 4242,
            tty: "??".to_string(),
            stat: "S".to_string(),
            lstart: "Tue Jun 30 00:00:00 2026".to_string(),
            command: "codex app-server".to_string(),
            start_time: crate::process_identity::parse_rfc3339("2026-06-30T00:00:00Z"),
        };

        let observations = collect_observations_from(tmp.path(), &HashMap::from([(4242, fact)]));

        assert_eq!(observations.len(), 1);
        assert!(!observations[0].app_server_alive);
    }

    #[test]
    fn default_state_dir_prefers_longhouse_home_over_home() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        let longhouse_home = temp.path().join("isolated-longhouse");
        temp_env::with_vars(
            [
                ("HOME", Some(home.display().to_string())),
                ("LONGHOUSE_HOME", Some(longhouse_home.display().to_string())),
                ("CLAUDE_CONFIG_DIR", None::<String>),
            ],
            || {
                assert_eq!(
                    default_codex_bridge_state_dir().unwrap(),
                    longhouse_home.join("managed-local").join("codex-bridge")
                );
            },
        );
    }

    #[test]
    fn default_state_dir_maps_claude_config_dir_to_longhouse_sibling() {
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        let claude_home = temp.path().join(".claude");
        temp_env::with_vars(
            [
                ("HOME", Some(home.display().to_string())),
                ("LONGHOUSE_HOME", None::<String>),
                ("CLAUDE_CONFIG_DIR", Some(claude_home.display().to_string())),
            ],
            || {
                assert_eq!(
                    default_codex_bridge_state_dir().unwrap(),
                    temp.path()
                        .join(".longhouse")
                        .join("managed-local")
                        .join("codex-bridge")
                );
            },
        );
    }
}
