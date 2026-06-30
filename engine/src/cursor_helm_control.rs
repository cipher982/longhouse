//! Cursor Helm live-control client.
//!
//! `longhouse cursor` (the Helm launcher) owns the PTY master and the
//! `cursor-agent` child. It binds a per-session Unix domain socket and writes a
//! private state file under `~/.longhouse/managed-local/cursor-helm/`. The
//! Machine Agent forwards remote `session.send_text` / `session.interrupt` /
//! `session.terminate` commands to that socket: the launcher is the only
//! process that can inject terminal input (it holds the PTY master fd) and it
//! owns the child pid for signaling.
//!
//! The engine connects per command, sends one JSON line, reads one JSON reply,
//! then closes. Per-command connect is intentional: commands are infrequent,
//! the launcher is transient, and this is engine-restart safe (a held
//! connection would need heartbeat/reconnect machinery for no gain on
//! same-user localhost IPC).

use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::Result;
use serde::Deserialize;
use serde_json::json;
use serde_json::Value;

use crate::config::get_longhouse_home;
use crate::managed_bridge_scan::pid_alive;

pub const CURSOR_HELM_TRANSPORT: &str = "cursor_helm";

const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const SOCKET_CONNECT_TIMEOUT: Duration = Duration::from_secs(2);

/// Directory the Helm launcher writes per-session state files into:
/// `~/.longhouse/managed-local/cursor-helm/`.
pub fn default_cursor_helm_state_dir() -> Option<PathBuf> {
    get_longhouse_home().ok().map(|home| {
        home.join("managed-local")
            .join("cursor-helm")
    })
}

/// Resolve the state directory, honoring an explicit override (tests / isolation).
fn resolve_state_dir(state_root: Option<&Path>) -> Result<PathBuf> {
    if let Some(root) = state_root {
        return Ok(root.to_path_buf());
    }
    default_cursor_helm_state_dir()
        .ok_or_else(|| anyhow::anyhow!("could not resolve longhouse home for cursor-helm state"))
}

fn state_file_path(session_id: &str, state_root: Option<&Path>) -> Result<PathBuf> {
    let dir = resolve_state_dir(state_root)?;
    Ok(dir.join(format!("{session_id}.json")))
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct CursorHelmStateFile {
    session_id: Option<String>,
    #[serde(default)]
    socket_path: Option<String>,
    #[serde(default)]
    launcher_pid: Option<u32>,
    #[serde(default)]
    cursor_pid: Option<u32>,
    #[serde(default)]
    started_at: Option<String>,
    #[serde(default)]
    updated_at: Option<String>,
}

#[derive(Debug, Clone)]
struct CursorHelmState {
    socket_path: PathBuf,
}

/// Engine-side typed error. `session_not_attached` means the launcher state is
/// missing, the launcher pid is dead, or the control socket is gone — the
/// session is not currently remotely steerable. `command_failed` wraps every
/// other failure (connect/IO/protocol/launcher-reported error).
#[derive(Debug)]
pub struct CursorHelmControlError {
    code: String,
    message: String,
}

impl CursorHelmControlError {
    pub fn code(&self) -> &str {
        &self.code
    }
    pub fn message(&self) -> &str {
        &self.message
    }
    fn not_attached(message: impl Into<String>) -> Self {
        Self {
            code: "session_not_attached".to_string(),
            message: message.into(),
        }
    }
    fn failed(message: impl std::fmt::Display) -> Self {
        Self {
            code: "command_failed".to_string(),
            message: message.to_string(),
        }
    }
}

impl std::fmt::Display for CursorHelmControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for CursorHelmControlError {}

/// Summary returned for a successfully forwarded command.
#[derive(Debug, Clone)]
pub struct CursorHelmCommandSummary {
    pub exit_code: i64,
    pub stdout: String,
    pub stderr: String,
}

fn load_state(session_id: &str, state_root: Option<&Path>) -> std::result::Result<CursorHelmState, CursorHelmControlError> {
    let path = state_file_path(session_id, state_root).map_err(CursorHelmControlError::failed)?;
    let bytes = fs::read(&path).map_err(|_| {
        CursorHelmControlError::not_attached(format!(
            "cursor helm state file not found at {}",
            path.display()
        ))
    })?;
    let state: CursorHelmStateFile =
        serde_json::from_slice(&bytes).map_err(|e| CursorHelmControlError::failed(e.to_string()))?;
    let parsed_session_id = state
        .session_id
        .unwrap_or_default()
        .trim()
        .to_string();
    if parsed_session_id.is_empty() || parsed_session_id != session_id {
        return Err(CursorHelmControlError::not_attached(
            "cursor helm state session_id mismatch",
        ));
    }
    let socket_str = state
        .socket_path
        .unwrap_or_default()
        .trim()
        .to_string();
    if socket_str.is_empty() {
        return Err(CursorHelmControlError::not_attached(
            "cursor helm state missing socket_path",
        ));
    }
    let socket_path = PathBuf::from(socket_str);
    let launcher_pid = state.launcher_pid.and_then(|pid| i32::try_from(pid).ok());
    // The launcher pid is the authority that the socket is live. If the state
    // records a pid and it is dead, the socket is stale — do not attempt the
    // connect (it would hang until timeout on a path the launcher no longer
    // serves).
    if let Some(pid) = launcher_pid {
        if !pid_alive(pid) {
            return Err(CursorHelmControlError::not_attached(
                "cursor helm launcher process is not running",
            ));
        }
    }
    Ok(CursorHelmState {
        socket_path,
    })
}

#[derive(Debug, Clone, Copy)]
enum CommandKind {
    Send,
    Interrupt,
    Terminate,
}

impl CommandKind {
    fn as_str(&self) -> &'static str {
        match self {
            CommandKind::Send => "send",
            CommandKind::Interrupt => "interrupt",
            CommandKind::Terminate => "terminate",
        }
    }
}

async fn dispatch_command(
    session_id: &str,
    kind: CommandKind,
    text: Option<&str>,
    state_root: Option<&Path>,
) -> std::result::Result<CursorHelmCommandSummary, CursorHelmControlError> {
    let state = load_state(session_id, state_root)?;
    if !state.socket_path.exists() {
        return Err(CursorHelmControlError::not_attached(format!(
            "cursor helm control socket missing at {}",
            state.socket_path.display()
        )));
    }

    let mut request = json!({
        "command_id": format!("cursor-helm:{}:{}", session_id, kind.as_str()),
        "kind": kind.as_str(),
    });
    if let Some(text) = text {
        request["text"] = json!(text);
    }
    let mut request_bytes = serde_json::to_vec(&request).map_err(CursorHelmControlError::failed)?;
    request_bytes.push(b'\n');

    let connect = tokio::time::timeout(
        SOCKET_CONNECT_TIMEOUT,
        tokio::net::UnixStream::connect(&state.socket_path),
    )
    .await
    .map_err(|_| {
        CursorHelmControlError::not_attached(format!(
            "timed out connecting to cursor helm socket {}",
            state.socket_path.display()
        ))
    })?;
    let mut stream = connect.map_err(|e| {
        CursorHelmControlError::not_attached(format!(
            "cursor helm socket connect failed: {e}"
        ))
    })?;

    use tokio::io::AsyncWriteExt;
    stream.write_all(&request_bytes).await.map_err(|e| {
        CursorHelmControlError::failed(format!("cursor helm socket write failed: {e}"))
    })?;
    stream.shutdown().await.ok();

    use tokio::io::AsyncReadExt;
    let reply_fut = async {
        let mut buf = Vec::with_capacity(4096);
        stream.read_to_end(&mut buf).await?;
        Ok::<Vec<u8>, std::io::Error>(buf)
    };
    let reply_bytes = tokio::time::timeout(COMMAND_TIMEOUT, reply_fut)
        .await
        .map_err(|_| {
            CursorHelmControlError::failed(format!(
                "cursor helm {} timed out after {}s",
                kind.as_str(),
                COMMAND_TIMEOUT.as_secs()
            ))
        })?
        .map_err(CursorHelmControlError::failed)?;

    parse_reply(&reply_bytes)
}

fn parse_reply(bytes: &[u8]) -> std::result::Result<CursorHelmCommandSummary, CursorHelmControlError> {
    let reply: Value =
        serde_json::from_slice(bytes).map_err(|e| CursorHelmControlError::failed(e.to_string()))?;
    if reply.get("ok").and_then(Value::as_bool) == Some(true) {
        let exit_code = reply
            .get("exit_code")
            .and_then(Value::as_i64)
            .unwrap_or(0);
        let stdout = reply
            .get("stdout")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let stderr = reply
            .get("stderr")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        return Ok(CursorHelmCommandSummary {
            exit_code,
            stdout,
            stderr,
        });
    }
    let error = reply.get("error");
    let code = error
        .and_then(|e| e.get("code"))
        .and_then(Value::as_str)
        .unwrap_or("command_failed");
    let message = error
        .and_then(|e| e.get("message"))
        .and_then(Value::as_str)
        .unwrap_or("cursor helm command failed");
    // A launcher-reported not-attached propagates as not-attached so the server
    // capability projection can degrade the session cleanly.
    let err = if code == "session_not_attached" {
        CursorHelmControlError::not_attached(message)
    } else {
        CursorHelmControlError::failed(message)
    };
    Err(err)
}

pub async fn send_text(
    session_id: &str,
    text: &str,
    state_root: Option<&Path>,
) -> std::result::Result<CursorHelmCommandSummary, CursorHelmControlError> {
    dispatch_command(session_id, CommandKind::Send, Some(text), state_root).await
}

pub async fn interrupt(
    session_id: &str,
    state_root: Option<&Path>,
) -> std::result::Result<CursorHelmCommandSummary, CursorHelmControlError> {
    dispatch_command(session_id, CommandKind::Interrupt, None, state_root).await
}

pub async fn terminate(
    session_id: &str,
    state_root: Option<&Path>,
) -> std::result::Result<CursorHelmCommandSummary, CursorHelmControlError> {
    dispatch_command(session_id, CommandKind::Terminate, None, state_root).await
}

/// Helper for the heartbeat scanner: is a cursor-helm state file live?
/// Mirrors the control-path liveness gate (state parses, launcher pid alive,
/// socket present). Public so the scan module can reuse it without duplicating
/// the rules.
#[allow(dead_code)]
pub fn state_is_live(session_id: &str, state_root: Option<&Path>) -> bool {
    match load_state(session_id, state_root) {
        Ok(state) => state.socket_path.exists(),
        Err(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    fn tmp_state_root() -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "cursor-helm-control-test-{}",
            std::process::id()
        ));
        let _ = fs::create_dir_all(&dir);
        dir
    }

    fn write_state(root: &Path, session_id: &str, socket_path: &Path, launcher_pid: Option<u32>) {
        let dir = root.to_path_buf();
        fs::create_dir_all(&dir).unwrap();
        let mut value = json!({
            "schema_version": 1,
            "session_id": session_id,
            "provider": "cursor",
            "control_plane": "cursor_helm",
            "socket_path": socket_path.to_string_lossy(),
            "cursor_pid": 99999,
            "started_at": "2026-06-30T00:00:00Z",
            "updated_at": "2026-06-30T00:00:00Z",
        });
        if let Some(pid) = launcher_pid {
            value["launcher_pid"] = json!(pid);
        }
        fs::write(dir.join(format!("{session_id}.json")), value.to_string()).unwrap();
    }

    async fn echo_server(socket_path: &Path, reply: Value) -> tokio::task::JoinHandle<()> {
        let _ = fs::remove_file(socket_path);
        if let Some(parent) = socket_path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        let listener = tokio::net::UnixListener::bind(socket_path).unwrap();
        let reply_bytes = format!("{}\n", reply.to_string());
        tokio::spawn(async move {
            let (mut conn, _) = listener.accept().await.unwrap();
            let mut buf = vec![0u8; 8192];
            let _ = conn.read(&mut buf).await.unwrap();
            conn.write_all(reply_bytes.as_bytes()).await.unwrap();
        })
    }

    #[tokio::test]
    async fn send_text_roundtrips_ok_response() {
        let root = tmp_state_root();
        let session_id = "send-ok-session";
        let socket = root.join("send-ok.sock");
        write_state(&root, session_id, &socket, None);
        let _server = echo_server(&socket, json!({"ok": true, "exit_code": 0, "stdout": "", "stderr": ""})).await;

        let summary = send_text(session_id, "hello", Some(&root)).await.unwrap();
        assert_eq!(summary.exit_code, 0);
    }

    #[tokio::test]
    async fn launcher_error_propagates_as_failed() {
        let root = tmp_state_root();
        let session_id = "err-session";
        let socket = root.join("err.sock");
        write_state(&root, session_id, &socket, None);
        let _server = echo_server(
            &socket,
            json!({"ok": false, "error": {"code": "injection_failed", "message": "boom"}}),
        )
        .await;

        let err = send_text(session_id, "x", Some(&root)).await.unwrap_err();
        assert_eq!(err.code(), "command_failed");
        assert!(err.message().contains("boom"));
    }

    #[tokio::test]
    async fn launcher_not_attached_propagates() {
        let root = tmp_state_root();
        let session_id = "na-session";
        let socket = root.join("na.sock");
        write_state(&root, session_id, &socket, None);
        let _server = echo_server(
            &socket,
            json!({"ok": false, "error": {"code": "session_not_attached", "message": "child gone"}}),
        )
        .await;

        let err = interrupt(session_id, Some(&root)).await.unwrap_err();
        assert_eq!(err.code(), "session_not_attached");
    }

    #[test]
    fn missing_state_file_is_not_attached() {
        let root = tmp_state_root();
        let err = load_state("nope-session", Some(&root)).unwrap_err();
        assert_eq!(err.code(), "session_not_attached");
    }

    #[test]
    fn missing_socket_is_not_attached() {
        let root = tmp_state_root();
        let session_id = "no-sock-session";
        let socket = root.join("does-not-exist.sock");
        write_state(&root, session_id, &socket, None);
        assert!(!state_is_live(session_id, Some(&root)));
        let err = load_state(session_id, Some(&root)).unwrap();
        // load_state passes (state parses, no pid to check) but socket missing.
        assert!(err.socket_path.ends_with("does-not-exist.sock"));
    }

    #[test]
    fn dead_launcher_pid_is_not_attached() {
        let root = tmp_state_root();
        let session_id = "dead-pid-session";
        let socket = root.join("dead.sock");
        // pid 2 (init) is not this user's launcher; use a pid certain not to be
        // the current process and almost certainly not alive in this context —
        // use a very high pid that nothing owns.
        write_state(&root, session_id, &socket, Some(2_000_000));
        let err = load_state(session_id, Some(&root)).unwrap_err();
        assert_eq!(err.code(), "session_not_attached");
    }

    #[tokio::test]
    async fn parse_reply_ok_extracts_fields() {
        let bytes = br#"{"ok":true,"exit_code":0,"stdout":"hi","stderr":""}"#;
        let s = parse_reply(bytes).unwrap();
        assert_eq!(s.exit_code, 0);
        assert_eq!(s.stdout, "hi");
    }

    #[tokio::test]
    async fn parse_reply_missing_ok_is_failed() {
        let bytes = br#"{"exit_code":0}"#;
        let err = parse_reply(bytes).unwrap_err();
        assert_eq!(err.code(), "command_failed");
    }
}
