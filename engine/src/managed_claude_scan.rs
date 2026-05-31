//! Managed-local Claude channel scanner.
//!
//! Walks `~/.claude/channels/longhouse/sessions/*.json` and reports the
//! process facts needed for the engine heartbeat. The state file contains a
//! session-scoped channel token, so this module deliberately emits only pid,
//! cwd, and readiness metadata.
//!
//! These state files are written write-once by the `longhouse claude-channel
//! serve` bridge and are only deleted by that same process on graceful
//! shutdown. Interactive sessions routinely die ungracefully (laptop sleep,
//! closed terminal, SIGKILL), so the files are orphaned by design more often
//! than not. The engine is the only always-on observer, so it owns liveness
//! truth: it validates process *identity* (not just PID existence) against the
//! recorded `started_at`, and reaps orphan files whose process is gone or whose
//! PID has been recycled. State files are untrusted hints, never authority.

use std::collections::HashMap;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use chrono::DateTime;
use chrono::Utc;
use serde::Deserialize;

/// A live process whose start time is more than this many seconds *after* the
/// session's recorded `started_at` cannot be the original session process — the
/// kernel has recycled the PID. The bridge writes `started_at` only after the
/// provider process is already running, so a legitimate process always starts
/// at or before `started_at`; the tolerance only absorbs clock granularity.
const PID_REUSE_TOLERANCE_SECS: i64 = 120;

/// Grace period before reaping a state file whose process is gone. Protects a
/// just-launched session from being reaped if it is momentarily missing from
/// the process snapshot.
const REAP_GRACE_SECS: i64 = 300;

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

/// A live process as observed by `ps`, including start time for identity.
#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessFact {
    command: String,
    start_time: Option<DateTime<Utc>>,
}

pub fn collect_observations() -> Vec<ClaudeChannelObservation> {
    let Some(state_dir) = default_claude_channel_state_dir() else {
        return Vec::new();
    };
    let process_facts = collect_process_facts_by_pid();
    let observations = collect_observations_from_with_processes(&state_dir, &process_facts);
    // The process scan is the source of truth; only reap when it actually
    // returned something, so a transient `ps` failure never deletes live state.
    reap_dead_state_files(&observations, !process_facts.is_empty(), Utc::now());
    observations
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
    collect_observations_from_with_processes(state_dir, &process_facts)
}

fn collect_process_facts_by_pid() -> HashMap<u32, ProcessFact> {
    let Ok(output) = Command::new("ps")
        .args(["-axo", "pid=,lstart=,command="])
        .output()
    else {
        return HashMap::new();
    };
    if !output.status.success() {
        return HashMap::new();
    }
    String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(parse_process_fact)
        .collect()
}

/// Parse one `ps -axo pid=,lstart=,command=` line.
///
/// `lstart` is a fixed-width 24-char field like `Sun Apr 27 10:15:23 2026`.
fn parse_process_fact(line: &str) -> Option<(u32, ProcessFact)> {
    let trimmed = line.trim_start();
    let (pid_text, rest) = trimmed.split_once(char::is_whitespace)?;
    let pid = pid_text.parse::<u32>().ok()?;
    let rest = rest.trim_start();
    if rest.len() <= 24 {
        return None;
    }
    let (lstart_raw, command) = rest.split_at(24);
    let command = command.trim().to_string();
    if command.is_empty() {
        return None;
    }
    Some((
        pid,
        ProcessFact {
            command,
            start_time: parse_lstart(lstart_raw.trim()),
        },
    ))
}

fn parse_lstart(value: &str) -> Option<DateTime<Utc>> {
    // ps -o lstart= emits local time ("Mon Apr 27 10:15:23 2026"). Parse as
    // naive and anchor to the system's local tz.
    use chrono::Local;
    use chrono::NaiveDateTime;
    use chrono::TimeZone;
    let naive = NaiveDateTime::parse_from_str(value, "%a %b %e %H:%M:%S %Y").ok()?;
    Local
        .from_local_datetime(&naive)
        .single()
        .map(|dt| dt.with_timezone(&Utc))
}

fn collect_observations_from_with_processes(
    state_dir: &Path,
    process_facts: &HashMap<u32, ProcessFact>,
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
        let started_at = state.started_at.unwrap_or_default();
        let recorded_start = parse_rfc3339(&started_at);
        let claude_alive = state
            .claude_pid
            .is_some_and(|pid| claude_process_alive(process_facts, pid, recorded_start));
        let bridge_alive = state
            .bridge_pid
            .is_some_and(|pid| claude_channel_bridge_alive(process_facts, pid, recorded_start));
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
            started_at,
            updated_at: state.updated_at.unwrap_or_default(),
            claude_alive,
            bridge_alive,
        });
    }
    out.sort_by(|a, b| a.session_id.cmp(&b.session_id));
    out
}

/// Delete state files whose Claude process is gone or whose PID was recycled.
///
/// Cleanup is the engine's responsibility because the bridge only deletes its
/// own file on graceful shutdown, which ungraceful deaths skip. Returns the
/// number of files reaped.
fn reap_dead_state_files(
    observations: &[ClaudeChannelObservation],
    process_scan_valid: bool,
    now: DateTime<Utc>,
) -> usize {
    if !process_scan_valid {
        return 0;
    }
    let mut reaped = 0;
    for obs in observations {
        if obs.claude_alive {
            continue;
        }
        // A dead-but-very-recent session may simply be missing from a stale
        // process snapshot; leave it alone until the grace window passes.
        if let Some(started) = parse_rfc3339(&obs.started_at) {
            if (now - started).num_seconds() < REAP_GRACE_SECS {
                continue;
            }
        }
        if fs::remove_file(&obs.state_file).is_ok() {
            reaped += 1;
            eprintln!(
                "managed_claude_scan: reaped orphan state file for dead session {} ({})",
                obs.session_id,
                obs.state_file.display()
            );
        }
    }
    reaped
}

fn parse_rfc3339(value: &str) -> Option<DateTime<Utc>> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return None;
    }
    DateTime::parse_from_rfc3339(trimmed)
        .ok()
        .map(|dt| dt.with_timezone(&Utc))
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
    match (fact.start_time, recorded_start) {
        (Some(proc_start), Some(recorded)) => {
            (proc_start - recorded).num_seconds() <= PID_REUSE_TOLERANCE_SECS
        }
        _ => true,
    }
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

    fn fact(command: &str, start: Option<&str>) -> ProcessFact {
        ProcessFact {
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

        let observations = collect_observations_from_with_processes(tmp.path(), &process_facts);

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

        let observations = collect_observations_from_with_processes(tmp.path(), &process_facts);

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

        let observations = collect_observations_from_with_processes(tmp.path(), &process_facts);

        assert_eq!(observations.len(), 1);
        assert!(
            !observations[0].claude_alive,
            "a PID that started after started_at must be treated as reused"
        );
    }

    #[test]
    fn reaper_deletes_orphan_state_file_for_dead_session() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("ghost.json");
        fs::write(
            &path,
            r#"{
                  "session_id": "ghost",
                  "claude_pid": 36943,
                  "ready": true,
                  "started_at": "2026-04-07T19:38:09Z",
                  "updated_at": "2026-04-07T19:38:09Z"
                }"#,
        )
        .unwrap();
        // PID recycled into a different claude process much later → not alive.
        let process_facts = HashMap::from([(
            36943,
            fact(
                "/Users/test/.local/bin/longhouse claude",
                Some("2026-05-28T20:40:28Z"),
            ),
        )]);

        let observations = collect_observations_from_with_processes(tmp.path(), &process_facts);
        let now = parse_rfc3339("2026-05-29T00:00:00Z").unwrap();
        let reaped = reap_dead_state_files(&observations, true, now);

        assert_eq!(reaped, 1);
        assert!(!path.exists(), "orphan state file should be deleted");
    }

    #[test]
    fn reaper_keeps_live_session_file() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("live.json");
        fs::write(
            &path,
            r#"{
                  "session_id": "live",
                  "claude_pid": 101,
                  "ready": true,
                  "started_at": "2026-05-28T20:03:48Z",
                  "updated_at": "2026-05-28T20:03:50Z"
                }"#,
        )
        .unwrap();
        let process_facts = HashMap::from([(
            101,
            fact("claude --resume", Some("2026-05-28T20:03:47Z")),
        )]);

        let observations = collect_observations_from_with_processes(tmp.path(), &process_facts);
        let now = parse_rfc3339("2026-05-29T00:00:00Z").unwrap();
        let reaped = reap_dead_state_files(&observations, true, now);

        assert_eq!(reaped, 0);
        assert!(path.exists(), "live session file must survive");
    }

    #[test]
    fn reaper_keeps_recent_dead_session_within_grace() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("recent.json");
        fs::write(
            &path,
            r#"{
                  "session_id": "recent",
                  "claude_pid": 999999,
                  "ready": true,
                  "started_at": "2026-05-29T00:00:00Z",
                  "updated_at": "2026-05-29T00:00:00Z"
                }"#,
        )
        .unwrap();
        // No matching process → dead, but started seconds ago.
        let observations = collect_observations_from_with_processes(tmp.path(), &HashMap::new());
        let now = parse_rfc3339("2026-05-29T00:00:30Z").unwrap();

        // Process scan empty → never reap regardless.
        assert_eq!(reap_dead_state_files(&observations, false, now), 0);
        // Even with a valid scan, the grace window protects it.
        assert_eq!(reap_dead_state_files(&observations, true, now), 0);
        assert!(path.exists());
    }

    #[test]
    fn parses_lsof_cwd_output() {
        assert_eq!(
            parse_lsof_cwd_output("p11713\nfcwd\nn/Users/test/git/acme\n").as_deref(),
            Some("/Users/test/git/acme")
        );
    }
}
