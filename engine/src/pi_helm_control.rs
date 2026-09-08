//! Machine-side client for a stock Pi Helm launch.
//!
//! The launcher owns the provider process and the Unix socket. This module
//! only reads the launch contract, proves the recorded launcher identity, and
//! forwards one authenticated command at a time. The provider's native
//! session id and the launch generation are included in every request so a
//! stale client cannot steer a replacement session.

use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::Result;
use serde::Deserialize;
use serde_json::{json, Value};

pub const PI_HELM_TRANSPORT: &str = "pi_helm_channel";
const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const SOCKET_CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_FRAME_BYTES: usize = 512 * 1024;

pub fn default_pi_helm_state_dir() -> Option<PathBuf> {
    crate::config::get_longhouse_home()
        .ok()
        .map(|home| home.join("managed-local").join("pi-helm"))
}

fn resolve_state_dir(state_root: Option<&Path>) -> Result<PathBuf> {
    state_root
        .map(Path::to_path_buf)
        .or_else(default_pi_helm_state_dir)
        .ok_or_else(|| anyhow::anyhow!("could not resolve Longhouse home for Pi Helm state"))
}

fn state_file_path(session_id: &str, state_root: Option<&Path>) -> Result<PathBuf> {
    Ok(resolve_state_dir(state_root)?.join(format!("{session_id}.json")))
}

#[derive(Debug, Deserialize)]
struct PiHelmStateFile {
    session_id: Option<String>,
    run_id: Option<String>,
    provider_session_id: Option<String>,
    connection_id: Option<String>,
    lease_generation: Option<String>,
    socket_path: Option<String>,
    channel_token: Option<String>,
    launcher_pid: Option<u32>,
    launcher_process_start_time: Option<String>,
    provider_pid: Option<u32>,
    provider_process_start_time: Option<String>,
    #[serde(default)]
    ready: bool,
    status: Option<String>,
}

#[derive(Debug, Clone)]
struct PiHelmState {
    socket_path: PathBuf,
    channel_token: String,
    run_id: String,
    provider_session_id: String,
    connection_id: String,
    lease_generation: String,
}

#[derive(Debug, Clone)]
pub struct PiHelmCommandSummary {
    pub provider_session_id: Option<String>,
    pub status: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub(crate) enum CommandKind {
    Send,
    Steer,
    Abort,
    Terminate,
}

impl CommandKind {
    fn as_str(self) -> &'static str {
        match self {
            Self::Send => "send",
            Self::Steer => "steer",
            Self::Abort => "abort",
            Self::Terminate => "terminate",
        }
    }
}

fn load_state(
    session_id: &str,
    state_root: Option<&Path>,
) -> std::result::Result<PiHelmState, PiHelmControlError> {
    let path = state_file_path(session_id, state_root).map_err(PiHelmControlError::failed)?;
    let bytes = fs::read(&path).map_err(|_| {
        PiHelmControlError::not_attached(format!(
            "Pi Helm state file not found at {}",
            path.display()
        ))
    })?;
    let state: PiHelmStateFile = serde_json::from_slice(&bytes)
        .map_err(|error| PiHelmControlError::failed(error.to_string()))?;
    if state.session_id.as_deref() != Some(session_id) {
        return Err(PiHelmControlError::not_attached(
            "Pi Helm session identity mismatch",
        ));
    }
    if !state.ready || state.status.as_deref() != Some("ready") {
        return Err(PiHelmControlError::not_attached(
            "Pi Helm extension channel is not ready",
        ));
    }
    let launcher_pid = state.launcher_pid.ok_or_else(|| {
        PiHelmControlError::not_attached("Pi Helm state is missing launcher identity")
    })?;
    let launcher_start = state
        .launcher_process_start_time
        .as_deref()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            PiHelmControlError::not_attached("Pi Helm state is missing launcher birth identity")
        })?;
    let provider_pid = state.provider_pid.ok_or_else(|| {
        PiHelmControlError::not_attached("Pi Helm state is missing provider identity")
    })?;
    let provider_start = state
        .provider_process_start_time
        .as_deref()
        .filter(|value| !value.trim().is_empty())
        .ok_or_else(|| {
            PiHelmControlError::not_attached("Pi Helm state is missing provider birth identity")
        })?;
    let facts = crate::process_identity::try_collect_process_facts_by_pid().ok_or_else(|| {
        PiHelmControlError::not_attached("Pi Helm process identity could not be verified")
    })?;
    let launcher_fact = facts.get(&launcher_pid).ok_or_else(|| {
        PiHelmControlError::not_attached("Pi Helm launcher process is not running")
    })?;
    if launcher_fact.lstart != launcher_start
        || !(launcher_fact.command.contains("pi_helm")
            || (launcher_fact.command.contains("longhouse")
                && launcher_fact.command.contains("pi-helm")))
    {
        return Err(PiHelmControlError::not_attached(
            "Pi Helm launcher identity changed",
        ));
    }
    let provider_fact = facts.get(&provider_pid).ok_or_else(|| {
        PiHelmControlError::not_attached("Pi Helm provider process is not running")
    })?;
    // Pi changes its process title after startup. The recorded child PID and
    // birth identity, plus the authenticated ready channel, establish ownership.
    if provider_fact.lstart != provider_start {
        return Err(PiHelmControlError::not_attached(
            "Pi Helm provider identity changed",
        ));
    }
    let socket_path = PathBuf::from(
        state
            .socket_path
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                PiHelmControlError::not_attached("Pi Helm state is missing its control socket")
            })?,
    );
    if !socket_path.exists() {
        return Err(PiHelmControlError::not_attached(
            "Pi Helm control socket is missing",
        ));
    }
    Ok(PiHelmState {
        socket_path,
        run_id: state
            .run_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| PiHelmControlError::failed("Pi Helm state is missing run identity"))?,
        channel_token: state
            .channel_token
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                PiHelmControlError::failed("Pi Helm state is missing channel authority")
            })?,
        provider_session_id: state
            .provider_session_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                PiHelmControlError::failed("Pi Helm state is missing native session identity")
            })?,
        connection_id: state
            .connection_id
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                PiHelmControlError::failed("Pi Helm state is missing connection identity")
            })?,
        lease_generation: state
            .lease_generation
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| {
                PiHelmControlError::failed("Pi Helm state is missing lease generation")
            })?,
    })
}

#[derive(Debug)]
pub struct PiHelmControlError {
    code: String,
    message: String,
}

impl PiHelmControlError {
    pub fn code(&self) -> &str {
        &self.code
    }

    pub fn message(&self) -> &str {
        &self.message
    }

    fn not_attached(message: impl Into<String>) -> Self {
        Self {
            code: "session_not_attached".into(),
            message: message.into(),
        }
    }

    fn failed(message: impl std::fmt::Display) -> Self {
        Self {
            code: "command_failed".into(),
            message: message.to_string(),
        }
    }
}

impl std::fmt::Display for PiHelmControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for PiHelmControlError {}

pub(crate) async fn dispatch(
    session_id: &str,
    kind: CommandKind,
    text: Option<&str>,
    state_root: Option<&Path>,
    expected_grant: Option<&Value>,
) -> std::result::Result<PiHelmCommandSummary, PiHelmControlError> {
    let state = load_state(session_id, state_root)?;
    if let Some(grant) = expected_grant {
        for (field, expected) in [
            ("run_id", state.run_id.as_str()),
            ("connection_id", state.connection_id.as_str()),
            ("lease_generation", state.lease_generation.as_str()),
        ] {
            if grant.get(field).and_then(Value::as_str) != Some(expected) {
                return Err(PiHelmControlError {
                    code: "stale_channel".into(),
                    message: "Pi control grant no longer identifies the current execution owner"
                        .into(),
                });
            }
        }
    }
    let mut request = json!({
        "kind": kind.as_str(),
        "auth_token": state.channel_token,
        "session_id": session_id,
        "provider_session_id": state.provider_session_id,
        "connection_id": state.connection_id,
        "lease_generation": state.lease_generation,
    });
    if let Some(text) = text {
        request["text"] = json!(text);
    }
    let mut bytes = serde_json::to_vec(&request).map_err(PiHelmControlError::failed)?;
    bytes.push(b'\n');

    let mut stream = tokio::time::timeout(
        SOCKET_CONNECT_TIMEOUT,
        tokio::net::UnixStream::connect(&state.socket_path),
    )
    .await
    .map_err(|_| PiHelmControlError::not_attached("timed out connecting to Pi Helm socket"))?
    .map_err(|error| {
        PiHelmControlError::not_attached(format!("Pi Helm socket connect failed: {error}"))
    })?;

    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    stream
        .write_all(&bytes)
        .await
        .map_err(PiHelmControlError::failed)?;
    stream.shutdown().await.ok();
    let mut reply = Vec::new();
    tokio::time::timeout(
        COMMAND_TIMEOUT,
        (&mut stream)
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_to_end(&mut reply),
    )
    .await
    .map_err(|_| PiHelmControlError::failed("Pi Helm command timed out"))?
    .map_err(PiHelmControlError::failed)?;
    if reply.len() > MAX_FRAME_BYTES {
        return Err(PiHelmControlError::failed(
            "Pi Helm reply exceeds frame limit",
        ));
    }
    let value: Value = serde_json::from_slice(&reply).map_err(PiHelmControlError::failed)?;
    if value.get("ok").and_then(Value::as_bool) != Some(true) {
        let error = value.get("error");
        let code = error
            .and_then(|value| value.get("code"))
            .and_then(Value::as_str)
            .unwrap_or("command_failed");
        let message = error
            .and_then(|value| value.get("message"))
            .and_then(Value::as_str)
            .unwrap_or("Pi Helm command failed");
        return Err(PiHelmControlError {
            code: code.into(),
            message: message.into(),
        });
    }
    Ok(PiHelmCommandSummary {
        provider_session_id: value
            .get("provider_session_id")
            .and_then(Value::as_str)
            .map(str::to_owned),
        status: value
            .get("status")
            .and_then(Value::as_str)
            .map(str::to_owned),
    })
}
