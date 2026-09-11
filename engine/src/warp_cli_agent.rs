//! Small Warp universal-agent protocol emitter used by managed launchers.
//!
//! The marker is inert outside Warp: it is only written when the controlling
//! environment identifies Warp and the launcher still owns a terminal.

use serde_json::json;
use std::io::{IsTerminal, Write};
use std::path::Path;

fn warp_terminal() -> bool {
    std::env::var("TERM_PROGRAM").as_deref() == Ok("WarpTerminal")
        && std::env::var_os("WARP_CLI_AGENT_PROTOCOL_VERSION").is_some()
        && std::env::var_os("WARP_CLIENT_VERSION").is_some()
}

fn project_name(cwd: &Path, project: Option<&str>) -> String {
    project
        .filter(|value| !value.trim().is_empty())
        .map(str::to_owned)
        .or_else(|| {
            cwd.file_name()
                .and_then(|value| value.to_str())
                .map(str::to_owned)
        })
        .unwrap_or_else(|| "longhouse".to_owned())
}

pub(crate) fn session_event_marker(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) -> String {
    let payload = json!({
        "v": 1,
        "agent": agent,
        "event": event,
        "session_id": session_id,
        "cwd": cwd,
        "project": project_name(cwd, project),
    });
    format!("\x1b]777;notify;warp://cli-agent;{payload}\x07")
}

pub(crate) fn emit_session_event(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) {
    if !warp_terminal() || !std::io::stdout().is_terminal() {
        return;
    }
    let marker = session_event_marker(agent, event, session_id, cwd, project);
    let mut stdout = std::io::stdout().lock();
    let _ = stdout
        .write_all(marker.as_bytes())
        .and_then(|()| stdout.flush());
}

pub(crate) fn emit_tty_event(
    agent: &str,
    event: &str,
    session_id: &str,
    cwd: &Path,
    project: Option<&str>,
) {
    if !warp_terminal() {
        return;
    }
    let Ok(mut tty) = std::fs::OpenOptions::new().write(true).open("/dev/tty") else {
        return;
    };
    let marker = session_event_marker(agent, event, session_id, cwd, project);
    let _ = tty.write_all(marker.as_bytes()).and_then(|()| tty.flush());
}

#[cfg(test)]
mod tests {
    use super::session_event_marker;
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
}
