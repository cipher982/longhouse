//! The authenticated control-socket client shared by the OMP and Pi Helm
//! launchers.
//!
//! Each launcher owns its provider process and a Unix socket; the Machine Agent
//! forwards one command at a time. `omp_helm_control` and `pi_helm_control`
//! keep what genuinely differs (state-file shape, launcher identity checks,
//! the native-id field name, OMP's channel-less terminate) and share the
//! error type, command vocabulary, grant check and the socket round trip.

use std::path::Path;
use std::time::Duration;

use serde_json::Value;

const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const SOCKET_CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
const MAX_FRAME_BYTES: usize = 512 * 1024;

#[derive(Debug, Clone, Copy)]
pub enum CommandKind {
    Send,
    Steer,
    Abort,
    Terminate,
}

impl CommandKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Send => "send",
            Self::Steer => "steer",
            Self::Abort => "abort",
            Self::Terminate => "terminate",
        }
    }
}

#[derive(Debug)]
pub struct HelmControlError {
    code: String,
    message: String,
}

impl HelmControlError {
    pub fn code(&self) -> &str {
        &self.code
    }

    pub fn message(&self) -> &str {
        &self.message
    }

    pub(crate) fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }

    pub(crate) fn not_attached(message: impl Into<String>) -> Self {
        Self::new("session_not_attached", message)
    }

    pub(crate) fn failed(message: impl std::fmt::Display) -> Self {
        Self::new("command_failed", message.to_string())
    }

    /// The grant names an execution owner this launch no longer has.
    pub(crate) fn stale_channel(provider: &str) -> Self {
        Self::new(
            "stale_channel",
            format!("{provider} control grant no longer identifies the current execution owner"),
        )
    }
}

impl std::fmt::Display for HelmControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for HelmControlError {}

/// Refuse a command whose grant no longer names this launch's run, connection
/// and lease generation. `provider` is the short label (`OMP`, `Pi`).
pub(crate) fn check_grant(
    provider: &str,
    expected_grant: Option<&Value>,
    run_id: &str,
    connection_id: &str,
    lease_generation: &str,
) -> Result<(), HelmControlError> {
    let Some(grant) = expected_grant else {
        return Ok(());
    };
    for (field, expected) in [
        ("run_id", run_id),
        ("connection_id", connection_id),
        ("lease_generation", lease_generation),
    ] {
        if grant.get(field).and_then(Value::as_str) != Some(expected) {
            return Err(HelmControlError::stale_channel(provider));
        }
    }
    Ok(())
}

/// Send one authenticated request over the launcher's socket and return its
/// successful reply. `label` names the channel in errors (`OMP Helm`).
pub(crate) async fn round_trip(
    label: &str,
    socket_path: &Path,
    request: &Value,
) -> Result<Value, HelmControlError> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    let mut bytes = serde_json::to_vec(request).map_err(HelmControlError::failed)?;
    bytes.push(b'\n');
    let mut stream = tokio::time::timeout(
        SOCKET_CONNECT_TIMEOUT,
        tokio::net::UnixStream::connect(socket_path),
    )
    .await
    .map_err(|_| HelmControlError::not_attached(format!("timed out connecting to {label} socket")))?
    .map_err(|error| {
        HelmControlError::not_attached(format!("{label} socket connect failed: {error}"))
    })?;
    stream
        .write_all(&bytes)
        .await
        .map_err(HelmControlError::failed)?;
    stream.shutdown().await.ok();
    let mut reply = Vec::new();
    tokio::time::timeout(
        COMMAND_TIMEOUT,
        (&mut stream)
            .take((MAX_FRAME_BYTES + 1) as u64)
            .read_to_end(&mut reply),
    )
    .await
    .map_err(|_| HelmControlError::failed(format!("{label} command timed out")))?
    .map_err(HelmControlError::failed)?;
    if reply.len() > MAX_FRAME_BYTES {
        return Err(HelmControlError::failed(format!(
            "{label} reply exceeds frame limit"
        )));
    }
    let value: Value = serde_json::from_slice(&reply).map_err(HelmControlError::failed)?;
    if value.get("ok").and_then(Value::as_bool) != Some(true) {
        let error = value.get("error");
        return Err(HelmControlError::new(
            error
                .and_then(|value| value.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("command_failed"),
            error
                .and_then(|value| value.get("message"))
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| format!("{label} command failed")),
        ));
    }
    Ok(value)
}
