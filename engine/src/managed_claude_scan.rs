//! Managed-local Claude channel scanner.
//!
//! Walks `~/.claude/channels/longhouse/sessions/*.json` and reports the
//! process facts needed for the engine heartbeat. The state file contains a
//! session-scoped channel token, so this module deliberately emits only pid,
//! cwd, and readiness metadata.
//!
//! The engine validates process *identity* (not just PID existence) against
//! the recorded `started_at`. State files are untrusted hints, never authority;
//! only their owning bridge removes them, avoiding races with atomic rewrites.

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use chrono::DateTime;
use chrono::Utc;
use serde::Deserialize;

#[cfg(test)]
use crate::process_identity::collect_process_facts_by_pid;
use crate::process_identity::{
    command_contains_basename, parse_rfc3339, started_before_or_near_recorded, ProcessFact,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ClaudeChannelObservation {
    pub session_id: String,
    pub provider_session_id: Option<String>,
    pub state_file: PathBuf,
    pub cwd: Option<String>,
    pub claude_pid: Option<u32>,
    pub bridge_pid: Option<u32>,
    pub ready: bool,
    pub started_at: String,
    pub updated_at: String,
    pub claude_alive: bool,
    pub bridge_alive: bool,
    /// True when the live `claude` process owns a foreground controlling
    /// terminal — an interactive, attached TUI. False when detached/background
    /// or when the process is not alive. Drives `foreground_tui` UI presence.
    pub claude_foreground_tui: bool,
}

#[derive(Debug, Deserialize)]
struct ClaudeChannelStateFile {
    session_id: Option<String>,
    provider_session_id: Option<String>,
    cwd: Option<String>,
    claude_pid: Option<u32>,
    bridge_pid: Option<u32>,
    ready: Option<bool>,
    started_at: Option<String>,
    updated_at: Option<String>,
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
    let process_facts = collect_process_facts_by_pid();
    collect_observations_from_processes(state_dir, &process_facts)
}

pub(crate) fn collect_observations_from_processes(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<ClaudeChannelObservation> {
    let paths = state_file_paths(state_dir);
    collect_observations_from_paths(&paths, process_facts)
}

pub(crate) fn collect_observations_from_paths(
    paths: &[PathBuf],
    process_facts: &HashMap<u32, ProcessFact>,
) -> Vec<ClaudeChannelObservation> {
    let mut out = Vec::new();
    for path in paths {
        let path = path.as_path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let Ok(bytes) = fs::read(path) else {
            continue;
        };
        let Ok(state) = serde_json::from_slice::<ClaudeChannelStateFile>(&bytes) else {
            continue;
        };
        let session_id = state.session_id.unwrap_or_default().trim().to_string();
        if session_id.is_empty() {
            continue;
        }
        let started_at = state.started_at.unwrap_or_default();
        let recorded_start = parse_rfc3339(&started_at);
        let claude_alive = state
            .claude_pid
            .is_some_and(|pid| claude_process_alive(process_facts, pid, recorded_start));
        let bridge_alive = state
            .bridge_pid
            .is_some_and(|pid| claude_channel_bridge_alive(process_facts, pid, recorded_start));
        let claude_foreground_tui = claude_alive
            && state.claude_pid.is_some_and(|pid| {
                process_facts
                    .get(&pid)
                    .is_some_and(ProcessFact::is_foreground_tty)
            });
        let cwd = state.cwd.or_else(|| {
            state
                .claude_pid
                .filter(|_| claude_alive)
                .and_then(process_cwd)
        });

        out.push(ClaudeChannelObservation {
            session_id,
            provider_session_id: state.provider_session_id,
            state_file: path.to_path_buf(),
            cwd,
            claude_pid: state.claude_pid,
            bridge_pid: state.bridge_pid,
            ready: state.ready.unwrap_or(false),
            started_at,
            updated_at: state.updated_at.unwrap_or_default(),
            claude_alive,
            bridge_alive,
            claude_foreground_tui,
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

/// True only if the PID currently runs Claude *and* it is the same process the
/// session recorded — a process that started after `started_at` (beyond clock
/// tolerance) is a recycled PID, not our session.
fn claude_process_alive(
    process_facts: &HashMap<u32, ProcessFact>,
    pid: u32,
    recorded_start: Option<DateTime<Utc>>,
) -> bool {
    let Some(fact) = process_facts.get(&pid) else {
        return false;
    };
    if !command_contains_basename(&fact.command, "claude") {
        return false;
    }
    process_identity_matches(fact, recorded_start)
}

fn claude_channel_bridge_alive(
    process_facts: &HashMap<u32, ProcessFact>,
    pid: u32,
    recorded_start: Option<DateTime<Utc>>,
) -> bool {
    let Some(fact) = process_facts.get(&pid) else {
        return false;
    };
    let looks_like_bridge = fact.command.contains("longhouse")
        && fact.command.contains("claude-channel")
        && fact.command.contains("serve");
    if !looks_like_bridge {
        return false;
    }
    process_identity_matches(fact, recorded_start)
}

/// Reject a PID whose process started meaningfully after the session was
/// recorded. If either timestamp is unknown we cannot prove reuse, so we fall
/// back to the command-name check alone (prior behavior).
fn process_identity_matches(fact: &ProcessFact, recorded_start: Option<DateTime<Utc>>) -> bool {
    started_before_or_near_recorded(fact, recorded_start)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn fact(command: &str, start: Option<&str>) -> ProcessFact {
        fact_with_tty(command, start, "ttys000", "S+")
    }

    fn fact_with_tty(command: &str, start: Option<&str>, tty: &str, stat: &str) -> ProcessFact {
        ProcessFact {
            pid: 0,
            tty: tty.to_string(),
            stat: stat.to_string(),
            lstart: String::new(),
            command: command.to_string(),
            start_time: start.and_then(parse_rfc3339),
        }
    }

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
        assert!(!obs.claude_foreground_tui);
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
                  "started_at": "2026-05-07T20:03:50Z",
                  "updated_at": "2026-05-07T20:03:50Z"
                }"#,
        )
        .unwrap();
        let process_facts = HashMap::from([
            (
                101,
                fact(
                    "claude --dangerously-skip-permissions",
                    Some("2026-05-07T20:03:48Z"),
                ),
            ),
            (
                102,
                fact(
                    "/Users/test/.local/bin/longhouse claude-channel serve",
                    Some("2026-05-07T20:03:49Z"),
                ),
            ),
        ]);

        let observations = collect_observations_from_processes(tmp.path(), &process_facts);

        assert_eq!(observations.len(), 1);
        assert!(observations[0].claude_alive);
        assert!(observations[0].bridge_alive);
        // Foreground TTY (ttys000 / S+) -> attached interactive TUI.
        assert!(observations[0].claude_foreground_tui);
    }

    #[test]
    fn scan_reports_background_claude_as_not_foreground() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            r#"{
                  "session_id": "09b68f98-1e31-458e-b78a-6dfd062ead75",
                  "claude_pid": 101,
                  "ready": true,
                  "started_at": "2026-05-07T20:03:50Z",
                  "updated_at": "2026-05-07T20:03:50Z"
                }"#,
        )
        .unwrap();
        // Live claude, but no controlling terminal (detached / backgrounded).
        let process_facts = HashMap::from([(
            101,
            fact_with_tty(
                "claude --dangerously-skip-permissions",
                Some("2026-05-07T20:03:48Z"),
                "??",
                "Ss",
            ),
        )]);

        let observations = collect_observations_from_processes(tmp.path(), &process_facts);

        assert_eq!(observations.len(), 1);
        assert!(observations[0].claude_alive);
        assert!(!observations[0].claude_foreground_tui);
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
        let process_facts = HashMap::from([
            (
                101,
                fact(
                    "/System/Library/PrivateFrameworks/CascadeSets.framework/SetStoreUpdateService",
                    None,
                ),
            ),
            (102, fact("/usr/libexec/some-other-helper", None)),
        ]);

        let observations = collect_observations_from_processes(tmp.path(), &process_facts);

        assert_eq!(observations.len(), 1);
        assert!(!observations[0].claude_alive);
        assert!(!observations[0].bridge_alive);
    }

    #[test]
    fn scan_rejects_reused_claude_pid_by_start_time() {
        // The exact field bug: PID still runs `claude`, but it started 51 days
        // after the session was recorded, so the kernel recycled it.
        let tmp = tempfile::tempdir().unwrap();
        fs::write(
            tmp.path().join("session.json"),
            r#"{
                  "session_id": "eaa4041a-0402-4afd-8b79-f64fdd3bd292",
                  "claude_pid": 36943,
                  "bridge_pid": 37175,
                  "ready": true,
                  "started_at": "2026-04-07T19:38:09Z",
                  "updated_at": "2026-04-07T19:38:09Z"
                }"#,
        )
        .unwrap();
        let process_facts = HashMap::from([(
            36943,
            fact(
                "/Users/test/.local/bin/longhouse claude",
                Some("2026-05-28T20:40:28Z"),
            ),
        )]);

        let observations = collect_observations_from_processes(tmp.path(), &process_facts);

        assert_eq!(observations.len(), 1);
        assert!(
            !observations[0].claude_alive,
            "a PID that started after started_at must be treated as reused"
        );
    }

    #[test]
    fn parses_lsof_cwd_output() {
        assert_eq!(
            parse_lsof_cwd_output("p11713\nfcwd\nn/Users/test/git/acme\n").as_deref(),
            Some("/Users/test/git/acme")
        );
    }
}
