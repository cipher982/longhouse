//! Small Warp universal-agent protocol emitter used by managed launchers and hooks.
//!
//! The marker is inert outside Warp: it is only written when the controlling
//! environment identifies Warp (`TERM_PROGRAM=WarpTerminal`, plus non-empty
//! `WARP_CLI_AGENT_PROTOCOL_VERSION` and `WARP_CLIENT_VERSION`).

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::io::{IsTerminal, Write};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};

const MAX_OSC_SEQUENCE_BYTES: usize = 4096;
const EXTRA_FIELD_MAX_CHARS: usize = 160;
const SUPPORTED_PROTOCOL_VERSION: u64 = 1;
pub(crate) const WARP_STATE_FILE_ENV: &str = "LONGHOUSE_WARP_STATE_FILE";
pub(crate) const REMOTE_VIEWERS_DIR_ENV: &str = "LONGHOUSE_REMOTE_VIEWERS_DIR";

fn is_warp_env(
    term_program: Option<&str>,
    protocol_version: Option<&str>,
    client_version: Option<&str>,
) -> bool {
    term_program == Some("WarpTerminal")
        && protocol_version.is_some_and(|value| !value.trim().is_empty())
        && client_version.is_some_and(|value| !value.trim().is_empty())
}

#[allow(dead_code)]
pub(crate) fn is_warp_env_current() -> bool {
    let term_program = std::env::var("TERM_PROGRAM").ok();
    let protocol_version = std::env::var("WARP_CLI_AGENT_PROTOCOL_VERSION").ok();
    let client_version = std::env::var("WARP_CLIENT_VERSION").ok();
    is_warp_env(
        term_program.as_deref(),
        protocol_version.as_deref(),
        client_version.as_deref(),
    )
}

#[cfg(unix)]
#[allow(dead_code)]
pub(crate) fn viewer_locks_want_warp(dir: &Path) -> Option<bool> {
    let entries = std::fs::read_dir(dir).ok()?;
    let mut active_viewers = 0usize;
    let mut all_warp = true;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|ext| ext.to_str()) != Some("lock") {
            continue;
        }
        let Ok(file) = std::fs::OpenOptions::new().read(true).open(&path) else {
            continue;
        };
        let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
        if rc == 0 {
            unsafe {
                libc::flock(file.as_raw_fd(), libc::LOCK_UN);
            }
            continue;
        }
        let err = std::io::Error::last_os_error().raw_os_error();
        if err == Some(libc::EWOULDBLOCK) || err == Some(libc::EAGAIN) {
            active_viewers += 1;
            let contents = std::fs::read_to_string(&path).unwrap_or_default();
            if !contents.lines().any(|line| line.trim() == "warp=1") {
                all_warp = false;
            }
        }
    }
    (active_viewers > 0).then_some(all_warp)
}

#[cfg(not(unix))]
#[allow(dead_code)]
pub(crate) fn viewer_locks_want_warp(_dir: &Path) -> Option<bool> {
    None
}

pub(crate) fn is_warp_terminal() -> bool {
    if let Some(dir) = std::env::var_os(REMOTE_VIEWERS_DIR_ENV).filter(|val| !val.is_empty()) {
        if let Some(want_warp) = viewer_locks_want_warp(Path::new(&dir)) {
            return want_warp;
        }
    }
    is_warp_env_current()
}

fn protocol_version() -> u64 {
    std::env::var("WARP_CLI_AGENT_PROTOCOL_VERSION")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(|version| version.clamp(1, SUPPORTED_PROTOCOL_VERSION))
        .unwrap_or(SUPPORTED_PROTOCOL_VERSION)
}

fn strip_control_chars(raw: &str) -> String {
    raw.chars().filter(|ch| !ch.is_control()).collect()
}

fn sanitize_osc_text(raw: &str, max_chars: usize) -> String {
    if max_chars == 0 {
        return String::new();
    }
    let mut normalized = String::new();
    let mut prev_space = false;
    for ch in raw.chars() {
        if ch.is_control() {
            if matches!(ch, '\n' | '\r' | '\t') && !prev_space && !normalized.is_empty() {
                normalized.push(' ');
                prev_space = true;
            }
            continue;
        }
        if ch == ' ' {
            if prev_space || normalized.is_empty() {
                continue;
            }
            prev_space = true;
        } else {
            prev_space = false;
        }
        normalized.push(ch);
    }
    let trimmed = normalized.trim();
    if trimmed.chars().count() <= max_chars {
        return trimmed.to_string();
    }
    if max_chars > 3 {
        let mut truncated: String = trimmed.chars().take(max_chars - 3).collect();
        truncated.push_str("...");
        truncated
    } else {
        trimmed.chars().take(max_chars).collect()
    }
}

fn project_name(cwd: &Path, project: Option<&str>) -> String {
    let raw = project
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .or_else(|| {
            cwd.file_name()
                .and_then(|value| value.to_str())
                .map(str::to_owned)
        })
        .unwrap_or_else(|| "longhouse".to_owned());
    let clean = strip_control_chars(&raw);
    if clean.trim().is_empty() {
        "longhouse".to_owned()
    } else {
        clean
    }
}

fn format_marker(payload: &Map<String, Value>) -> String {
    let json = Value::Object(payload.clone()).to_string();
    format!("\x1b]777;notify;warp://cli-agent;{json}\x07")
}

pub(crate) fn session_event_marker_with_fields(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
    extra: &[(&str, &str)],
) -> String {
    let mut base = Map::new();
    base.insert("v".to_string(), Value::from(protocol_version()));
    base.insert(
        "agent".to_string(),
        Value::String(strip_control_chars(agent)),
    );
    base.insert(
        "event".to_string(),
        Value::String(strip_control_chars(event)),
    );
    base.insert(
        "session_id".to_string(),
        Value::String(strip_control_chars(session_id)),
    );
    base.insert(
        "cwd".to_string(),
        Value::String(strip_control_chars(&cwd.to_string_lossy())),
    );
    base.insert(
        "project".to_string(),
        Value::String(project_name(cwd, project)),
    );

    let mut full = base.clone();
    for (key, val) in extra {
        if key.trim().is_empty() {
            continue;
        }
        let cleaned = sanitize_osc_text(val, EXTRA_FIELD_MAX_CHARS);
        if !cleaned.is_empty() {
            full.insert((*key).to_string(), Value::String(cleaned));
        }
    }

    let marker = format_marker(&full);
    if marker.len() <= MAX_OSC_SEQUENCE_BYTES {
        return marker;
    }

    let base_marker = format_marker(&base);
    if base_marker.len() <= MAX_OSC_SEQUENCE_BYTES {
        return base_marker;
    }

    if let Some(Value::String(cwd_value)) = base.get_mut("cwd") {
        let excess = base_marker.len().saturating_sub(MAX_OSC_SEQUENCE_BYTES);
        let keep_bytes = cwd_value.len().saturating_sub(excess + 16);
        let mut truncated = String::new();
        for ch in cwd_value.chars() {
            if truncated.len() + ch.len_utf8() > keep_bytes {
                break;
            }
            truncated.push(ch);
        }
        *cwd_value = truncated;
    }
    format_marker(&base)
}

pub(crate) fn session_event_marker(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) -> String {
    session_event_marker_with_fields(agent, event, session_id, cwd, project, &[])
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub(crate) struct WarpSessionState {
    pub agent: String,
    pub session_id: String,
    pub cwd: String,
    pub project: String,
    pub last_event: String,
    pub updated_at: String,
}

#[allow(dead_code)]
pub(crate) fn has_warp_state_file() -> bool {
    std::env::var_os(WARP_STATE_FILE_ENV).is_some_and(|value| !value.is_empty())
}

#[allow(dead_code)]
pub(crate) fn record_warp_state(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) {
    let Some(path) = std::env::var_os(WARP_STATE_FILE_ENV).filter(|value| !value.is_empty()) else {
        return;
    };
    let path = PathBuf::from(path);
    let state = WarpSessionState {
        agent: strip_control_chars(agent),
        session_id: strip_control_chars(session_id),
        cwd: strip_control_chars(&cwd.to_string_lossy()),
        project: project_name(cwd, project),
        last_event: strip_control_chars(event),
        updated_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true),
    };
    let Ok(bytes) = serde_json::to_vec(&state) else {
        return;
    };
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let tmp = path.with_extension(format!("json.tmp.{}", uuid::Uuid::new_v4().simple()));
    if std::fs::write(&tmp, &bytes).is_err() {
        let _ = std::fs::remove_file(&tmp);
        return;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    if std::fs::rename(&tmp, &path).is_err() {
        let _ = std::fs::remove_file(&tmp);
    }
}

#[allow(dead_code)]
pub(crate) fn read_warp_state(path: &Path) -> Option<WarpSessionState> {
    let bytes = std::fs::read(path).ok()?;
    serde_json::from_slice(&bytes).ok()
}

#[allow(dead_code)]
pub(crate) fn replay_markers_for_state(state: &WarpSessionState) -> Vec<String> {
    if state.last_event == "session_end" {
        return Vec::new();
    }
    let cwd = Path::new(&state.cwd);
    let mut markers = vec![session_event_marker(
        &state.agent,
        "session_start",
        &state.session_id,
        cwd,
        Some(&state.project),
    )];
    if matches!(
        state.last_event.as_str(),
        "prompt_submit" | "input_required" | "stop"
    ) {
        markers.push(session_event_marker(
            &state.agent,
            &state.last_event,
            &state.session_id,
            cwd,
            Some(&state.project),
        ));
    }
    markers
}

#[allow(dead_code)]
pub(crate) fn emit_replay_markers(path: &Path) {
    if !is_warp_env_current() || !std::io::stdout().is_terminal() {
        return;
    }
    let Some(state) = read_warp_state(path) else {
        return;
    };
    let markers = replay_markers_for_state(&state);
    if markers.is_empty() {
        return;
    }
    let mut stdout = std::io::stdout().lock();
    for marker in markers {
        let _ = stdout.write_all(marker.as_bytes());
    }
    let _ = stdout.flush();
}

pub(crate) fn emit_session_event(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) {
    record_warp_state(agent, event, session_id, cwd, project);
    if !is_warp_terminal() || !std::io::stdout().is_terminal() {
        return;
    }
    let marker = session_event_marker(agent, event, session_id, cwd, project);
    let mut stdout = std::io::stdout().lock();
    let _ = stdout
        .write_all(marker.as_bytes())
        .and_then(|()| stdout.flush());
}

pub(crate) fn write_tty_marker(marker: &str) -> bool {
    let Ok(mut tty) = std::fs::OpenOptions::new().write(true).open("/dev/tty") else {
        return false;
    };
    tty.write_all(marker.as_bytes())
        .and_then(|()| tty.flush())
        .is_ok()
}

pub(crate) fn emit_tty_event(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) -> bool {
    record_warp_state(agent, event, session_id, cwd, project);
    if !is_warp_terminal() {
        return false;
    }
    let marker = session_event_marker(agent, event, session_id, cwd, project);
    write_tty_marker(&marker)
}

#[cfg(test)]
mod tests {
    use super::{
        is_warp_env, session_event_marker, session_event_marker_with_fields, MAX_OSC_SEQUENCE_BYTES,
    };
    use serde_json::Value;
    use std::path::Path;

    #[test]
    fn marker_preserves_provider_identity_and_framing() {
        let marker = session_event_marker(
            "pi",
            "session_start",
            "session-1",
            Path::new("/tmp/demo"),
            None,
        );
        let payload = marker
            .strip_prefix("\x1b]777;notify;warp://cli-agent;")
            .and_then(|value| value.strip_suffix('\x07'))
            .expect("Warp OSC 777 framing");
        assert_eq!(
            serde_json::from_str::<Value>(payload).unwrap(),
            serde_json::json!({
                "v": 1,
                "agent": "pi",
                "event": "session_start",
                "session_id": "session-1",
                "cwd": "/tmp/demo",
                "project": "demo",
            })
        );
    }

    #[test]
    fn marker_with_fields_sanitizes_control_bytes_and_stays_bounded() {
        let marker = session_event_marker_with_fields(
            "claude",
            "permission_request",
            "sess-1",
            Path::new("/tmp/demo"),
            None,
            &[
                ("tool_name", "Bash\x1b]9;evil\x07"),
                ("summary", "Wants to run Bash:\n\techo hi\x07"),
                ("empty", "   "),
            ],
        );
        assert!(marker.len() <= MAX_OSC_SEQUENCE_BYTES);
        assert!(
            !marker[1..marker.len() - 1].contains('\x1b')
                && !marker[1..marker.len() - 1].contains('\x07'),
            "inner OSC payload must never contain ESC or BEL"
        );
        let payload = marker
            .strip_prefix("\x1b]777;notify;warp://cli-agent;")
            .and_then(|value| value.strip_suffix('\x07'))
            .expect("Warp OSC 777 framing");
        let parsed: Value = serde_json::from_str(payload).unwrap();
        assert_eq!(parsed["tool_name"], "Bash]9;evil");
        assert_eq!(parsed["summary"], "Wants to run Bash: echo hi");
        assert!(parsed.get("empty").is_none());
    }

    #[test]
    fn warp_env_requires_non_empty_protocol_and_client_versions() {
        assert!(is_warp_env(
            Some("WarpTerminal"),
            Some("1"),
            Some("v0.2026.10.21.08.25.stable_02")
        ));
        assert!(!is_warp_env(
            Some("Apple_Terminal"),
            Some("1"),
            Some("v0.2026.10.21.08.25.stable_02")
        ));
        assert!(!is_warp_env(
            Some("WarpTerminal"),
            Some(""),
            Some("v0.2026.10.21.08.25.stable_02")
        ));
        assert!(!is_warp_env(Some("WarpTerminal"), Some("1"), Some("   ")));
        assert!(!is_warp_env(Some("WarpTerminal"), None, Some("1.0")));
    }

    #[test]
    fn record_and_replay_markers_handle_all_session_states() {
        let dir = tempfile::tempdir().unwrap();
        let state_path = dir.path().join("state.warp.json");
        temp_env::with_var(
            super::WARP_STATE_FILE_ENV,
            Some(state_path.to_str().unwrap()),
            || {
                assert!(super::has_warp_state_file());
                super::record_warp_state(
                    "omp",
                    "prompt_submit",
                    "sess-42",
                    Path::new("/tmp/demo"),
                    Some("custom-proj"),
                );
            },
        );
        let state = super::read_warp_state(&state_path).expect("state saved");
        assert_eq!(state.agent, "omp");
        assert_eq!(state.session_id, "sess-42");
        assert_eq!(state.project, "custom-proj");
        assert_eq!(state.last_event, "prompt_submit");

        let markers = super::replay_markers_for_state(&state);
        assert_eq!(markers.len(), 2);
        assert!(markers[0].contains(r#""event":"session_start""#));
        assert!(markers[1].contains(r#""event":"prompt_submit""#));

        let mut start_only = state.clone();
        start_only.last_event = "session_start".to_string();
        assert_eq!(super::replay_markers_for_state(&start_only).len(), 1);

        let mut attn = state.clone();
        attn.last_event = "input_required".to_string();
        let attn_markers = super::replay_markers_for_state(&attn);
        assert_eq!(attn_markers.len(), 2);
        assert!(attn_markers[1].contains(r#""event":"input_required""#));

        let mut stopped = state.clone();
        stopped.last_event = "stop".to_string();
        let stop_markers = super::replay_markers_for_state(&stopped);
        assert_eq!(stop_markers.len(), 2);
        assert!(stop_markers[1].contains(r#""event":"stop""#));

        let mut ended = state;
        ended.last_event = "session_end".to_string();
        assert!(super::replay_markers_for_state(&ended).is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn viewer_locks_require_live_lock_and_all_warp_viewers() {
        use std::os::fd::AsRawFd;

        let dir = tempfile::tempdir().unwrap();
        assert_eq!(super::viewer_locks_want_warp(dir.path()), None);

        // Unlocked stale file is ignored.
        let stale = dir.path().join("stale.lock");
        std::fs::write(&stale, "warp=1\n").unwrap();
        assert_eq!(super::viewer_locks_want_warp(dir.path()), None);

        // Held warp=1 viewer enables Warp emission.
        let warp_path = dir.path().join("v1.lock");
        let warp_file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&warp_path)
            .unwrap();
        std::fs::write(&warp_path, "warp=1\n").unwrap();
        assert_eq!(
            unsafe { libc::flock(warp_file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        assert_eq!(super::viewer_locks_want_warp(dir.path()), Some(true));

        // Adding a non-Warp viewer suppresses mixed emissions.
        let plain_path = dir.path().join("v2.lock");
        let plain_file = std::fs::OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&plain_path)
            .unwrap();
        std::fs::write(&plain_path, "warp=0\n").unwrap();
        assert_eq!(
            unsafe { libc::flock(plain_file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
        assert_eq!(super::viewer_locks_want_warp(dir.path()), Some(false));
    }
}
