//! Small Warp universal-agent protocol emitter used by managed launchers and hooks.
//!
//! The marker is inert outside Warp: it is only written when the controlling
//! environment identifies Warp (`TERM_PROGRAM=WarpTerminal`, plus non-empty
//! `WARP_CLI_AGENT_PROTOCOL_VERSION` and `WARP_CLIENT_VERSION`).

use serde_json::{Map, Value};
use std::io::{IsTerminal, Write};
use std::path::Path;

const MAX_OSC_SEQUENCE_BYTES: usize = 4096;
const EXTRA_FIELD_MAX_CHARS: usize = 160;
const SUPPORTED_PROTOCOL_VERSION: u64 = 1;

fn is_warp_env(
    term_program: Option<&str>,
    protocol_version: Option<&str>,
    client_version: Option<&str>,
) -> bool {
    term_program == Some("WarpTerminal")
        && protocol_version.is_some_and(|value| !value.trim().is_empty())
        && client_version.is_some_and(|value| !value.trim().is_empty())
}

pub(crate) fn is_warp_terminal() -> bool {
    let term_program = std::env::var("TERM_PROGRAM").ok();
    let protocol_version = std::env::var("WARP_CLI_AGENT_PROTOCOL_VERSION").ok();
    let client_version = std::env::var("WARP_CLIENT_VERSION").ok();
    is_warp_env(
        term_program.as_deref(),
        protocol_version.as_deref(),
        client_version.as_deref(),
    )
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

pub(crate) fn emit_session_event(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) {
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
}
