use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::process_identity::{
    collect_process_facts_by_pid, command_contains_basename, parse_rfc3339,
    started_before_or_near_recorded, ProcessFact,
};
use chrono::DateTime;
use chrono::Utc;
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::json;
use thiserror::Error;
use tokio::time::sleep;
use uuid::Uuid;

const DEFAULT_READY_WAIT: Duration = Duration::from_secs(10);
const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(100);
const DEFAULT_HTTP_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Error)]
pub enum ClaudeChannelControlError {
    #[error("Claude channel is not attached for session {session_id}: {message}")]
    SessionNotAttached { session_id: String, message: String },
    #[error("Claude channel command failed: {0}")]
    CommandFailed(String),
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelSendConfig {
    pub session_id: String,
    pub text: String,
    pub meta: Vec<(String, String)>,
    pub state_root: Option<PathBuf>,
    pub wait_timeout: Option<Duration>,
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelInterruptConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub wait_timeout: Option<Duration>,
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelInspectConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub wait_timeout: Option<Duration>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeChannelSendSummary {
    pub provider_session_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeChannelInterruptSummary {
    pub pid: i32,
}

#[derive(Deserialize)]
struct ClaudeChannelState {
    provider_session_id: Option<String>,
    auth_token: Option<String>,
    port: Option<u16>,
    claude_pid: Option<i32>,
    ready: Option<bool>,
    started_at: Option<String>,
}

pub async fn send_text(
    config: ClaudeChannelSendConfig,
) -> Result<ClaudeChannelSendSummary, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let state = wait_for_ready_state(
        &config.session_id,
        config.state_root.as_deref(),
        wait_timeout,
        DEFAULT_POLL_INTERVAL,
    )
    .await?;
    let port = state
        .port
        .ok_or_else(|| not_attached(&config.session_id, "state is missing port"))?;
    if port == 0 {
        return Err(not_attached(&config.session_id, "state has invalid port"));
    }
    let auth_token = state
        .auth_token
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| not_attached(&config.session_id, "state is missing channel auth token"))?;

    let mut meta = serde_json::Map::new();
    meta.insert("injected_by".to_string(), json!("longhouse"));
    meta.insert(
        "longhouse_session_id".to_string(),
        json!(config.session_id.clone()),
    );
    for (key, value) in config.meta {
        let normalized_key = key.trim();
        if !normalized_key.is_empty() {
            meta.insert(normalized_key.to_string(), json!(value));
        }
    }

    let client = reqwest::Client::builder()
        .timeout(DEFAULT_HTTP_TIMEOUT)
        .build()
        .map_err(|err| ClaudeChannelControlError::CommandFailed(err.to_string()))?;
    let url = format!("http://127.0.0.1:{port}/inject");
    let response = client
        .post(url)
        .header("X-Longhouse-Channel-Token", auth_token)
        .json(&json!({
            "content": config.text,
            "meta": meta,
        }))
        .send()
        .await
        .map_err(|err| {
            ClaudeChannelControlError::CommandFailed(format!(
                "bridge injection request failed: {err}"
            ))
        })?;
    if response.status() != StatusCode::NO_CONTENT {
        return Err(ClaudeChannelControlError::CommandFailed(format!(
            "bridge injection returned HTTP {}",
            response.status().as_u16()
        )));
    }

    Ok(ClaudeChannelSendSummary {
        provider_session_id: state.provider_session_id,
    })
}

pub async fn interrupt(
    config: ClaudeChannelInterruptConfig,
) -> Result<ClaudeChannelInterruptSummary, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let state = wait_for_ready_state(
        &config.session_id,
        config.state_root.as_deref(),
        wait_timeout,
        DEFAULT_POLL_INTERVAL,
    )
    .await?;
    let pid = state
        .claude_pid
        .filter(|pid| *pid > 0)
        .ok_or_else(|| not_attached(&config.session_id, "state is missing claude_pid"))?;
    verify_claude_interrupt_target(
        &config.session_id,
        pid,
        state.started_at.as_deref().and_then(parse_rfc3339),
    )?;
    signal_interrupt(pid).map_err(|err| {
        ClaudeChannelControlError::CommandFailed(format!(
            "failed to interrupt Claude process {pid}: {err}"
        ))
    })?;
    Ok(ClaudeChannelInterruptSummary { pid })
}

pub async fn inspect_state(
    config: ClaudeChannelInspectConfig,
) -> Result<serde_json::Value, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let path = state_file_path(&config.session_id, config.state_root.as_deref())?;
    let deadline = Instant::now() + wait_timeout;
    loop {
        match read_state_value(&path) {
            Ok(mut value) => {
                if let Some(object) = value.as_object_mut() {
                    if object.contains_key("auth_token") {
                        object.insert("auth_token".to_string(), json!("<redacted>"));
                    }
                }
                return Ok(value);
            }
            Err(StateReadError::Missing) => {}
            Err(StateReadError::Invalid(message)) => {
                return Err(not_attached(&config.session_id, &message));
            }
        }
        if Instant::now() >= deadline {
            return Err(not_attached(
                &config.session_id,
                &format!("state did not appear at {}", path.display()),
            ));
        }
        sleep(DEFAULT_POLL_INTERVAL).await;
    }
}

async fn wait_for_ready_state(
    session_id: &str,
    state_root: Option<&Path>,
    timeout: Duration,
    poll_interval: Duration,
) -> Result<ClaudeChannelState, ClaudeChannelControlError> {
    let path = state_file_path(session_id, state_root)?;
    let deadline = Instant::now() + timeout;
    let mut last_not_ready = false;
    loop {
        match read_state_file(&path) {
            Ok(state) => {
                if state.ready.unwrap_or(false) {
                    return Ok(state);
                }
                last_not_ready = true;
            }
            Err(StateReadError::Missing) => {}
            Err(StateReadError::Invalid(message)) => {
                return Err(not_attached(session_id, &message));
            }
        }
        if Instant::now() >= deadline {
            let message = if last_not_ready {
                format!("state at {} did not become ready", path.display())
            } else {
                format!("state did not appear at {}", path.display())
            };
            return Err(not_attached(session_id, &message));
        }
        sleep(poll_interval).await;
    }
}

fn state_file_path(
    session_id: &str,
    state_root: Option<&Path>,
) -> Result<PathBuf, ClaudeChannelControlError> {
    let normalized = Uuid::parse_str(session_id)
        .map_err(|_| not_attached(session_id, "session id is not a UUID"))?;
    let root = match state_root {
        Some(path) => path.to_path_buf(),
        None => default_state_root().map_err(|message| not_attached(session_id, &message))?,
    };
    Ok(root.join("sessions").join(format!("{normalized}.json")))
}

fn default_state_root() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "HOME is not set".to_string())?;
    Ok(PathBuf::from(home).join(".claude/channels/longhouse"))
}

enum StateReadError {
    Missing,
    Invalid(String),
}

fn read_state_file(path: &Path) -> Result<ClaudeChannelState, StateReadError> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(StateReadError::Missing);
        }
        Err(err) => return Err(StateReadError::Invalid(err.to_string())),
    };
    serde_json::from_str(&raw)
        .map_err(|err| StateReadError::Invalid(format!("state is invalid JSON: {err}")))
}

fn read_state_value(path: &Path) -> Result<serde_json::Value, StateReadError> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(StateReadError::Missing);
        }
        Err(err) => return Err(StateReadError::Invalid(err.to_string())),
    };
    serde_json::from_str(&raw)
        .map_err(|err| StateReadError::Invalid(format!("state is invalid JSON: {err}")))
}

fn verify_claude_interrupt_target(
    session_id: &str,
    pid: i32,
    recorded_start: Option<DateTime<Utc>>,
) -> Result<(), ClaudeChannelControlError> {
    let pid_u32 = u32::try_from(pid).map_err(|_| {
        not_attached(
            session_id,
            &format!("state has invalid claude_pid {pid} for interrupt"),
        )
    })?;
    let process_facts = collect_process_facts_by_pid();
    let Some(fact) = process_facts.get(&pid_u32) else {
        return Err(not_attached(
            session_id,
            &format!("recorded Claude process {pid} is not running"),
        ));
    };
    if !claude_interrupt_target_matches(fact, recorded_start) {
        return Err(not_attached(
            session_id,
            &format!("recorded Claude process {pid} no longer matches the channel state"),
        ));
    }
    Ok(())
}

fn claude_interrupt_target_matches(
    fact: &ProcessFact,
    recorded_start: Option<DateTime<Utc>>,
) -> bool {
    command_contains_basename(&fact.command, "claude")
        && started_before_or_near_recorded(fact, recorded_start)
}

fn signal_interrupt(pid: i32) -> std::io::Result<()> {
    #[cfg(unix)]
    unsafe {
        // The Claude MCP bridge can share a process group with Claude when the
        // provider launches the server, so interrupt only the recorded Claude
        // process. Group signaling can destroy the bridge/control channel.
        if libc::kill(pid, libc::SIGINT) == 0 {
            return Ok(());
        }
        Err(std::io::Error::last_os_error())
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "Claude interrupt is unsupported on this platform",
        ))
    }
}

fn not_attached(session_id: &str, message: &str) -> ClaudeChannelControlError {
    ClaudeChannelControlError::SessionNotAttached {
        session_id: session_id.to_string(),
        message: message.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

    struct RecordedRequest {
        headers: String,
        body: Value,
    }

    async fn spawn_inject_server(
        status: &'static str,
    ) -> (u16, oneshot::Receiver<RecordedRequest>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = oneshot::channel();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut raw = Vec::new();
            let mut buffer = [0_u8; 4096];
            loop {
                let n = stream.read(&mut buffer).await.unwrap();
                if n == 0 {
                    break;
                }
                raw.extend_from_slice(&buffer[..n]);
                if let Some(header_end) = find_header_end(&raw) {
                    let headers = String::from_utf8_lossy(&raw[..header_end]).to_string();
                    let content_length = headers
                        .lines()
                        .find_map(|line| {
                            line.strip_prefix("content-length:")
                                .or_else(|| line.strip_prefix("Content-Length:"))
                        })
                        .and_then(|value| value.trim().parse::<usize>().ok())
                        .unwrap_or(0);
                    let body_start = header_end + 4;
                    if raw.len() >= body_start + content_length {
                        let body =
                            serde_json::from_slice(&raw[body_start..body_start + content_length])
                                .unwrap();
                        let _ = tx.send(RecordedRequest { headers, body });
                        let response = format!(
                            "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        );
                        stream.write_all(response.as_bytes()).await.unwrap();
                        break;
                    }
                }
            }
        });
        (port, rx)
    }

    fn find_header_end(raw: &[u8]) -> Option<usize> {
        raw.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn write_state(root: &Path, session_id: &str, payload: Value) {
        let path = state_file_path(session_id, Some(root)).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, serde_json::to_vec(&payload).unwrap()).unwrap();
    }

    fn process_fact(command: &str, started_at: Option<&str>) -> ProcessFact {
        ProcessFact {
            pid: 101,
            tty: "??".to_string(),
            stat: "Ss".to_string(),
            lstart: "".to_string(),
            command: command.to_string(),
            start_time: started_at.and_then(parse_rfc3339),
        }
    }

    #[tokio::test]
    async fn send_text_injects_payload_and_default_meta() {
        let temp = tempfile::tempdir().unwrap();
        let (port, rx) = spawn_inject_server("204 No Content").await;
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "session_id": SESSION_ID,
                "provider_session_id": "claude-provider-1",
                "auth_token": "secret-token",
                "port": port,
                "ready": true,
            }),
        );

        let summary = send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "hello".to_string(),
            meta: vec![],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap();

        assert_eq!(
            summary.provider_session_id.as_deref(),
            Some("claude-provider-1")
        );
        let request = rx.await.unwrap();
        assert!(request
            .headers
            .to_ascii_lowercase()
            .contains("x-longhouse-channel-token: secret-token"));
        assert_eq!(request.body["content"], "hello");
        assert_eq!(request.body["meta"]["injected_by"], "longhouse");
        assert_eq!(request.body["meta"]["longhouse_session_id"], SESSION_ID);
    }

    #[tokio::test]
    async fn send_text_preserves_control_meta() {
        let temp = tempfile::tempdir().unwrap();
        let (port, rx) = spawn_inject_server("204 No Content").await;
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "auth_token": "secret-token",
                "port": port,
                "ready": true,
            }),
        );

        send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "course correct".to_string(),
            meta: vec![("intent".to_string(), "steer".to_string())],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap();

        let request = rx.await.unwrap();
        assert_eq!(request.body["content"], "course correct");
        assert_eq!(request.body["meta"]["intent"], "steer");
    }

    #[tokio::test]
    async fn bridge_failure_does_not_leak_auth_token() {
        let temp = tempfile::tempdir().unwrap();
        let (port, _rx) = spawn_inject_server("403 Forbidden").await;
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "auth_token": "very-secret-token",
                "port": port,
                "ready": true,
            }),
        );

        let err = send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "hello".to_string(),
            meta: vec![],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap_err();

        let message = err.to_string();
        assert!(message.contains("HTTP 403"));
        assert!(!message.contains("very-secret-token"));
    }

    #[tokio::test]
    async fn missing_state_is_session_not_attached() {
        let temp = tempfile::tempdir().unwrap();

        let err = send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "hello".to_string(),
            meta: vec![],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: Some(Duration::from_millis(10)),
        })
        .await
        .unwrap_err();

        assert!(matches!(
            err,
            ClaudeChannelControlError::SessionNotAttached { .. }
        ));
    }

    #[tokio::test]
    async fn inspect_state_redacts_auth_token() {
        let temp = tempfile::tempdir().unwrap();
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "session_id": SESSION_ID,
                "provider_session_id": "claude-provider-1",
                "auth_token": "very-secret-token",
                "port": 4242,
                "ready": true,
            }),
        );

        let state = inspect_state(ClaudeChannelInspectConfig {
            session_id: SESSION_ID.to_string(),
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap();

        assert_eq!(state["session_id"], SESSION_ID);
        assert_eq!(state["auth_token"], "<redacted>");
        assert!(!serde_json::to_string(&state)
            .unwrap()
            .contains("very-secret-token"));
    }

    #[test]
    fn claude_interrupt_target_requires_claude_command() {
        let fact = process_fact(
            "/System/Library/PrivateFrameworks/CascadeSets.framework/SetStoreUpdateService",
            None,
        );

        assert!(!claude_interrupt_target_matches(&fact, None));
    }

    #[test]
    fn claude_interrupt_target_rejects_reused_claude_pid_by_start_time() {
        let fact = process_fact(
            "/Users/test/.local/bin/claude --resume 11111111-1111-4111-8111-111111111111",
            Some("2026-05-28T20:40:28Z"),
        );
        let recorded_start = parse_rfc3339("2026-04-07T19:38:09Z");

        assert!(!claude_interrupt_target_matches(&fact, recorded_start));
    }

    #[test]
    fn claude_interrupt_target_accepts_matching_recorded_claude_process() {
        let fact = process_fact(
            "/Users/test/.local/bin/claude --resume 11111111-1111-4111-8111-111111111111",
            Some("2026-04-07T19:38:08Z"),
        );
        let recorded_start = parse_rfc3339("2026-04-07T19:38:09Z");

        assert!(claude_interrupt_target_matches(&fact, recorded_start));
    }

    #[cfg(unix)]
    #[test]
    fn interrupt_signals_only_target_child() {
        use std::os::unix::process::ExitStatusExt;
        use std::process::Command;

        let mut child = Command::new("/bin/sh")
            .arg("-c")
            .arg("trap 'exit 42' INT; while :; do sleep 1; done")
            .spawn()
            .unwrap();
        let mut sibling = Command::new("/bin/sh")
            .arg("-c")
            .arg("trap 'exit 43' INT; while :; do sleep 1; done")
            .spawn()
            .unwrap();
        let pid = i32::try_from(child.id()).unwrap();

        signal_interrupt(pid).unwrap();
        let status = child.wait().unwrap();

        assert!(status.signal() == Some(libc::SIGINT) || status.code() == Some(42));
        assert!(sibling.try_wait().unwrap().is_none());
        unsafe {
            libc::kill(i32::try_from(sibling.id()).unwrap(), libc::SIGTERM);
        }
        let _ = sibling.wait();
    }
}
