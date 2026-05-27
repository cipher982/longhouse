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

use std::fs;
use std::fs::OpenOptions;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use crate::codex_bridge::BridgeStateFile;

/// Raw, per-bridge signals captured by a single scan pass.
#[derive(Debug, Clone)]
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

/// Collect observations from the default state directory, using a fresh
/// `ps -axo command=` read for TUI attachment inference.
pub fn collect_observations() -> Vec<CodexBridgeObservation> {
    let Some(state_dir) = default_codex_bridge_state_dir() else {
        return Vec::new();
    };
    let process_commands = collect_process_commands();
    collect_observations_from(&state_dir, &process_commands)
}

pub fn default_codex_bridge_state_dir() -> Option<PathBuf> {
    crate::config::get_codex_bridge_state_dir().ok()
}

pub fn collect_process_commands() -> Vec<String> {
    let Ok(output) = Command::new("ps").args(["-axo", "command="]).output() else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::to_string)
        .collect()
}

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
    process_commands: &[String],
) -> Vec<CodexBridgeObservation> {
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
        let Ok(state) = serde_json::from_slice::<BridgeStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        let bridge_alive = bridge_lock_is_held(&path);
        let has_tui_attachment = state
            .ws_url
            .as_deref()
            .is_some_and(|ws| codex_tui_process_attached(process_commands, ws));
        let app_server_alive = state
            .app_server_pid
            .and_then(|pid| i32::try_from(pid).ok())
            .map(pid_alive)
            .unwrap_or(false);

        out.push(CodexBridgeObservation {
            session_id,
            state_file: path,
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
        let obs = collect_observations_from(tmp.path(), &[]);
        assert!(obs.is_empty());
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
