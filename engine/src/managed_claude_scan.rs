//! Managed-local Claude channel scanner.
//!
//! Walks `~/.claude/channels/longhouse/sessions/*.json` and reports the
//! process facts needed for the engine heartbeat. The state file contains a
//! session-scoped channel token, so this module deliberately emits only pid,
//! cwd, and readiness metadata.

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use serde::Deserialize;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaudeChannelObservation {
    pub session_id: String,
    pub provider_session_id: Option<String>,
    pub state_file: PathBuf,
    pub cwd: Option<String>,
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
    cwd: Option<String>,
    claude_pid: Option<u32>,
    bridge_pid: Option<u32>,
    ready: Option<bool>,
    updated_at: Option<String>,
}

pub fn collect_observations() -> Vec<ClaudeChannelObservation> {
    let Some(state_dir) = default_claude_channel_state_dir() else {
        return Vec::new();
    };
    let process_commands = collect_process_commands_by_pid();
    collect_observations_from_with_processes(&state_dir, &process_commands)
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

#[cfg(test)]
pub fn collect_observations_from(state_dir: &Path) -> Vec<ClaudeChannelObservation> {
    let process_commands = collect_process_commands_by_pid();
    collect_observations_from_with_processes(state_dir, &process_commands)
}

fn collect_process_commands_by_pid() -> HashMap<u32, String> {
    let Ok(output) = Command::new("ps").args(["-axo", "pid=,command="]).output() else {
        return HashMap::new();
    };
    if !output.status.success() {
        return HashMap::new();
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(parse_process_command)
        .collect()
}

fn parse_process_command(line: &str) -> Option<(u32, String)> {
    let trimmed = line.trim_start();
    let (pid_text, command) = trimmed.split_once(char::is_whitespace)?;
    let pid = pid_text.parse::<u32>().ok()?;
    let command = command.trim().to_string();
    if command.is_empty() {
        return None;
    }
    Some((pid, command))
}

fn collect_observations_from_with_processes(
    state_dir: &Path,
    process_commands: &HashMap<u32, String>,
) -> Vec<ClaudeChannelObservation> {
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
            .is_some_and(|pid| claude_process_alive(process_commands, pid));
        let bridge_alive = state
            .bridge_pid
            .is_some_and(|pid| claude_channel_bridge_alive(process_commands, pid));
        let cwd = state.cwd.or_else(|| {
            state
                .claude_pid
                .filter(|_| claude_alive)
                .and_then(process_cwd)
        });

        out.push(ClaudeChannelObservation {
            session_id,
            provider_session_id: state.provider_session_id,
            state_file: path,
            cwd,
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

fn claude_process_alive(process_commands: &HashMap<u32, String>, pid: u32) -> bool {
    process_commands
        .get(&pid)
        .is_some_and(|command| command_contains_basename(command, "claude"))
}

fn claude_channel_bridge_alive(process_commands: &HashMap<u32, String>, pid: u32) -> bool {
    process_commands.get(&pid).is_some_and(|command| {
        command.contains("longhouse")
            && command.contains("claude-channel")
            && command.contains("serve")
    })
}

fn process_cwd(pid: u32) -> Option<String> {
    #[cfg(target_os = "linux")]
    {
        fs::read_link(format!("/proc/{pid}/cwd"))
            .ok()
            .map(|path| path.display().to_string())
            .filter(|path| !path.trim().is_empty())
    }
    #[cfg(target_os = "macos")]
    {
        process_cwd_via_lsof(pid)
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        let _ = pid;
        None
    }
}

#[cfg(target_os = "macos")]
fn process_cwd_via_lsof(pid: u32) -> Option<String> {
    let output = Command::new("lsof")
        .args(["-a", "-p", &pid.to_string(), "-d", "cwd", "-Fn"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    parse_lsof_cwd_output(&String::from_utf8_lossy(&output.stdout))
}

#[cfg(any(test, target_os = "macos"))]
fn parse_lsof_cwd_output(output: &str) -> Option<String> {
    output.lines().find_map(|line| {
        line.strip_prefix('n')
            .map(str::trim)
            .filter(|path| !path.is_empty())
            .map(str::to_string)
    })
}

fn command_contains_basename(command: &str, expected: &str) -> bool {
    command.split_whitespace().any(|part| {
        Path::new(part)
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name == expected)
    })
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
              "cwd": "/Users/test/git/zerg",
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
        assert_eq!(obs.cwd.as_deref(), Some("/Users/test/git/zerg"));
        assert!(obs.ready);
        assert!(!obs.claude_alive);
        assert!(!obs.bridge_alive);
    }

    #[test]
    fn scan_matches_process_commands_by_pid() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            r#"{
                  "session_id": "09b68f98-1e31-458e-b78a-6dfd062ead75",
                  "claude_pid": 101,
                  "bridge_pid": 102,
                  "ready": true,
                  "updated_at": "2026-05-07T20:03:50Z"
                }"#,
        )
        .unwrap();
        let process_commands = HashMap::from([
            (101, "claude --dangerously-skip-permissions".to_string()),
            (
                102,
                "/Users/test/.local/bin/longhouse claude-channel serve".to_string(),
            ),
        ]);

        let observations = collect_observations_from_with_processes(tmp.path(), &process_commands);

        assert_eq!(observations.len(), 1);
        assert!(observations[0].claude_alive);
        assert!(observations[0].bridge_alive);
    }

    #[test]
    fn scan_rejects_reused_non_claude_pid() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            r#"{
                  "session_id": "09b68f98-1e31-458e-b78a-6dfd062ead75",
                  "claude_pid": 101,
                  "bridge_pid": 102,
                  "ready": true,
                  "updated_at": "2026-05-07T20:03:50Z"
                }"#,
        )
        .unwrap();
        let process_commands = HashMap::from([
            (
                101,
                "/System/Library/PrivateFrameworks/CascadeSets.framework/SetStoreUpdateService"
                    .to_string(),
            ),
            (102, "/usr/libexec/some-other-helper".to_string()),
        ]);

        let observations = collect_observations_from_with_processes(tmp.path(), &process_commands);

        assert_eq!(observations.len(), 1);
        assert!(!observations[0].claude_alive);
        assert!(!observations[0].bridge_alive);
    }

    #[test]
    fn parses_lsof_cwd_output() {
        assert_eq!(
            parse_lsof_cwd_output("p11713\nfcwd\nn/Users/test/git/zeta\n").as_deref(),
            Some("/Users/test/git/zeta")
        );
    }
}
