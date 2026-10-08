//! Machine Agent managed-control WebSocket client.

use std::collections::{HashMap, HashSet, VecDeque};
use std::ffi::{OsStr, OsString};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use futures_util::{Sink, SinkExt, StreamExt};
use rand::Rng;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::fs::OpenOptions;
use std::io::Write;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::Message;

use crate::antigravity_print::{
    start_antigravity_print_turn, AntigravityPrintRunConfig, ANTIGRAVITY_PRINT_ADAPTER,
};
use crate::build_identity;
use crate::claude_channel_control::{
    interrupt as claude_channel_interrupt, send_text as claude_channel_send_text,
    terminate as claude_channel_terminate, ClaudeChannelControlError, ClaudeChannelInterruptConfig,
    ClaudeChannelSendConfig,
};
use crate::claude_print::{start_claude_print_turn, ClaudePrintRunConfig, CLAUDE_PRINT_ADAPTER};
use crate::codex_bridge::{
    cmd_codex_bridge_interrupt, cmd_codex_bridge_pause_response, cmd_codex_bridge_send,
    cmd_codex_bridge_steer, validate_codex_bridge_attached, BridgeInterruptConfig,
    BridgePauseResponseConfig, BridgeSendConfig, BridgeSteerConfig, BridgeSteerError,
};
use crate::codex_exec::{start_codex_exec_once, CodexExecRunConfig, CODEX_EXEC_ADAPTER};
use crate::config::ShipperConfig;
use crate::console_adapter::ConsoleSteerOutcome;
use crate::cursor_print::{start_cursor_print_turn, CursorPrintRunConfig, CURSOR_PRINT_ADAPTER};
use crate::omp_print::{start_omp_print_turn, OmpPrintRunConfig, OMP_PRINT_ADAPTER};
use crate::opencode_run::{start_opencode_run_turn, OpenCodeRunConfig, OPENCODE_RUN_ADAPTER};
use crate::pi_print::{start_pi_print_turn, PiPrintRunConfig, PI_PRINT_ADAPTER};
use crate::turn_claims::{
    default_registry as default_turn_claim_registry, process_start_time_for_pid, ClaimOutcome,
};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

mod capabilities;
mod connection;
mod dispatch;
mod receipts;
mod turn_start;

use self::capabilities::*;
use self::connection::*;
use self::dispatch::*;
use self::receipts::*;
use self::turn_start::*;

pub(crate) use self::capabilities::granted_control_operations;
pub use self::connection::spawn_control_channel;
pub(crate) use self::dispatch::steer_local_session;

const COMMAND_SEND_TEXT: &str = "session.send_text";
const COMMAND_INTERRUPT: &str = "session.interrupt";
const COMMAND_STEER_TEXT: &str = "session.steer_text";
const COMMAND_ANSWER_PAUSE: &str = "session.answer_pause";
const COMMAND_TERMINATE: &str = "session.terminate";
const COMMAND_RUN_ONCE: &str = "session.run_once";
const COMMAND_TURN_START: &str = "session.turn.start";
const COMMAND_INVOCATION_CLOSE: &str = "session.invocation.close";
const COMMAND_TURN_INTERRUPT: &str = "session.turn.interrupt";
const COMMAND_TURN_STEER: &str = "session.turn.steer";
const COMMAND_PROVIDER_SIGN_IN_START: &str = "provider.sign_in.start";
const COMMAND_PROVIDER_SIGN_IN_CODE: &str = "provider.sign_in.code";
const COMMAND_PROVIDER_SIGN_IN_CANCEL: &str = "provider.sign_in.cancel";
// A command frame that omits `provider` is routed here. This was five
// unexplained copies of the literal "codex", which silently sent an unknown or
// missing provider into the Codex bridge and contradicts the repo's
// no-silent-fallbacks invariant. It stays for wire compatibility with older
// callers that never sent the field; the constant makes the fallback one
// reviewable decision instead of five invisible ones.
const DEFAULT_COMMAND_PROVIDER: &str = "codex";
const COMMAND_ARCHIVE_BACKLOG_CONTROL: &str = "archive.backlog_control";
const COMMAND_ARCHIVE_BACKLOG_CONTROL_V2: &str = "archive.backlog_control.v2";
const DEFAULT_CODEX_BIN: &str = "codex";
const DEFAULT_CURSOR_BIN: &str = "cursor-agent";
const DEFAULT_OPENCODE_BIN: &str = "opencode";
const DEFAULT_LONGHOUSE_BIN: &str = "longhouse";
// Re-exported rather than restated. `warm_pool_compatible` only reuses a
// prewarmed Console worker when the spawn config equals these exact values, so
// a second copy that drifted would silently cost every Console turn its warm
// start with nothing failing to say so.
use crate::codex_exec::DEFAULT_CONSOLE_APPROVAL_POLICY as REMOTE_CODEX_EXEC_APPROVAL_POLICY;
use crate::codex_exec::DEFAULT_CONSOLE_SANDBOX as REMOTE_CODEX_EXEC_SANDBOX;
const CONSOLE_DEFAULT_PERMISSION_MODE: &str = "bypass";
// Generated beside the server's copy from the same schema, so advertised
// supports[] and server-side contracts cannot drift silently. This copy omits the
// source digests the engine never reads; they change on every adapter edit.
const MANAGED_PROVIDER_CONTRACTS_JSON: &str =
    include_str!("../managed_provider_contracts.generated.json");
const REPORT_STAGE_DEADLINE_SECS: u64 = 8;
const COMPLETED_COMMAND_CACHE_CAPACITY: usize = 256;
const COMPLETED_COMMAND_CACHE_TTL_SECS: u64 = 5 * 60;
// This receipt fence covers only provider-affecting managed controls. It is
// deliberately separate from turn_claims: a lost control response must not
// make a later engine process inject the same command a second time.
const COMMAND_RECEIPT_RESULT_MAX_BYTES: usize = 64 * 1024;
const COMMAND_RECEIPT_DIR: &str = "control-command-receipts";
// Keep this below uvicorn/websockets' default ping timeout. Tungstenite may
// queue protocol pongs internally and flush them on the next write, so the
// app-level heartbeat also keeps server keepalive pongs moving through proxies.
const HEARTBEAT_INTERVAL_SECS: u64 = 10;
/// How often the engine re-probes provider CLIs and readiness after hello.
/// Only a change is sent, so a quiet machine costs one probe pass a minute.
const READINESS_REFRESH_SECS: u64 = 60;
const CONTROL_CONNECT_TIMEOUT_SECS: u64 = 15;
const CONTROL_UPDATE_CONNECT_TIMEOUT_SECS: u64 = 3;
const CONTROL_UPDATE_RETRY_MILLIS: u64 = 500;
const CONTROL_WRITE_TIMEOUT_SECS: u64 = 5;
const CONTROL_HEARTBEAT_LATE_WARN_MS: u128 = 500;
const CONTROL_RECONNECT_SHORT_MAX_BACKOFF_SECS: u64 = 5;
const CONTROL_RECONNECT_SUSTAINED_MAX_BACKOFF_SECS: u64 = 30;
const CONTROL_RECONNECT_SHORT_WINDOW_SECS: u64 = 60;
static MANAGED_PROVIDER_CONTRACTS: OnceLock<Value> = OnceLock::new();

#[derive(Clone, Debug)]
pub struct ControlChannelStatus {
    inner: Arc<Mutex<ControlChannelStatusInner>>,
}

#[derive(Clone, Debug)]
struct ControlChannelStatusInner {
    enabled: bool,
    status: String,
    ws_url: Option<String>,
    last_connected_at: Option<String>,
    last_disconnected_at: Option<String>,
    last_error_code: Option<String>,
    last_error_message: Option<String>,
    reconnect_backoff_seconds: Option<u64>,
    last_heartbeat_lateness_ms: Option<u64>,
    max_heartbeat_lateness_ms: Option<u64>,
    last_write_elapsed_ms: Option<u64>,
    max_write_elapsed_ms: Option<u64>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ControlChannelStatusSnapshot {
    pub enabled: bool,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ws_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_connected_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_disconnected_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error_message: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reconnect_backoff_seconds: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_heartbeat_lateness_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_heartbeat_lateness_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_write_elapsed_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_write_elapsed_ms: Option<u64>,
    pub supports: Vec<String>,
}

pub fn new_control_channel_status() -> ControlChannelStatus {
    ControlChannelStatus {
        inner: Arc::new(Mutex::new(ControlChannelStatusInner {
            enabled: false,
            status: "disabled".to_string(),
            ws_url: None,
            last_connected_at: None,
            last_disconnected_at: None,
            last_error_code: None,
            last_error_message: None,
            reconnect_backoff_seconds: None,
            last_heartbeat_lateness_ms: None,
            max_heartbeat_lateness_ms: None,
            last_write_elapsed_ms: None,
            max_write_elapsed_ms: None,
        })),
    }
}

impl ControlChannelStatus {
    pub fn snapshot(&self) -> ControlChannelStatusSnapshot {
        let inner = self
            .inner
            .lock()
            .expect("control channel status lock poisoned");
        ControlChannelStatusSnapshot {
            enabled: inner.enabled,
            status: inner.status.clone(),
            ws_url: inner.ws_url.clone(),
            last_connected_at: inner.last_connected_at.clone(),
            last_disconnected_at: inner.last_disconnected_at.clone(),
            last_error_code: inner.last_error_code.clone(),
            last_error_message: inner.last_error_message.clone(),
            reconnect_backoff_seconds: inner.reconnect_backoff_seconds,
            last_heartbeat_lateness_ms: inner.last_heartbeat_lateness_ms,
            max_heartbeat_lateness_ms: inner.max_heartbeat_lateness_ms,
            last_write_elapsed_ms: inner.last_write_elapsed_ms,
            max_write_elapsed_ms: inner.max_write_elapsed_ms,
            supports: if inner.enabled {
                control_supports()
            } else {
                Vec::new()
            },
        }
    }

    fn set_disabled(&self) {
        let mut inner = self
            .inner
            .lock()
            .expect("control channel status lock poisoned");
        inner.enabled = false;
        inner.status = "disabled".to_string();
        inner.ws_url = None;
        inner.reconnect_backoff_seconds = None;
        inner.last_error_code = None;
        inner.last_error_message = None;
        inner.last_heartbeat_lateness_ms = None;
        inner.max_heartbeat_lateness_ms = None;
        inner.last_write_elapsed_ms = None;
        inner.max_write_elapsed_ms = None;
    }

    fn set_connected(&self, ws_url: &str) {
        let mut inner = self
            .inner
            .lock()
            .expect("control channel status lock poisoned");
        inner.enabled = true;
        inner.status = "connected".to_string();
        inner.ws_url = Some(ws_url.to_string());
        inner.last_connected_at = Some(timestamp_now());
        inner.reconnect_backoff_seconds = None;
        inner.last_error_code = None;
        inner.last_error_message = None;
        inner.last_heartbeat_lateness_ms = None;
        inner.max_heartbeat_lateness_ms = None;
        inner.last_write_elapsed_ms = None;
        inner.max_write_elapsed_ms = None;
    }

    fn set_disconnected(
        &self,
        ws_url: Option<&str>,
        error_code: Option<&str>,
        error_message: Option<&str>,
        reconnect_backoff_seconds: Option<u64>,
    ) {
        let mut inner = self
            .inner
            .lock()
            .expect("control channel status lock poisoned");
        inner.enabled = true;
        inner.status = "disconnected".to_string();
        if let Some(ws_url) = ws_url {
            inner.ws_url = Some(ws_url.to_string());
        }
        inner.last_disconnected_at = Some(timestamp_now());
        inner.last_error_code = error_code.map(str::to_string);
        inner.last_error_message = error_message.map(str::to_string);
        inner.reconnect_backoff_seconds = reconnect_backoff_seconds;
    }

    fn record_heartbeat_lateness(&self, lateness: Duration) {
        let millis = duration_millis_u64(lateness);
        let mut inner = self
            .inner
            .lock()
            .expect("control channel status lock poisoned");
        inner.last_heartbeat_lateness_ms = Some(millis);
        inner.max_heartbeat_lateness_ms = Some(
            inner
                .max_heartbeat_lateness_ms
                .map(|current| current.max(millis))
                .unwrap_or(millis),
        );
    }

    fn record_write_elapsed(&self, elapsed: Duration) {
        let millis = duration_millis_u64(elapsed);
        let mut inner = self
            .inner
            .lock()
            .expect("control channel status lock poisoned");
        inner.last_write_elapsed_ms = Some(millis);
        inner.max_write_elapsed_ms = Some(
            inner
                .max_write_elapsed_ms
                .map(|current| current.max(millis))
                .unwrap_or(millis),
        );
    }
}

fn duration_millis_u64(duration: Duration) -> u64 {
    duration.as_millis().min(u128::from(u64::MAX)) as u64
}

fn timestamp_now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

/// The websocket URL for the control channel.
///
/// Plaintext is judged by the shared rule (`plaintext_http`): everything on
/// this channel is sensitive (the never-expiring device token rides as a
/// header and every transcript ships through it), and the engine authenticates
/// to the server but the server never authenticates back, so whoever answers
/// the handshake can start turns on this machine. `ws://` is therefore allowed
/// only to loopback, to a Tailscale address (WireGuard encrypts it), or to a
/// LAN address the user opted into.
fn control_ws_url(api_url: &str, allow_insecure_http: bool) -> Result<String> {
    let base = api_url.trim().trim_end_matches('/');
    let outcome = crate::plaintext_http::check(base, allow_insecure_http);
    if !outcome.usable() {
        bail!(
            "{}",
            crate::plaintext_http::refusal_message(api_url, outcome)
        );
    }
    match crate::plaintext_http::split_http_scheme(base) {
        Some(("http", rest)) => Ok(format!("ws://{rest}/api/agents/control/ws")),
        Some((_, rest)) => Ok(format!("wss://{rest}/api/agents/control/ws")),
        None => bail!("api_url must start with http:// or https://"),
    }
}

fn required_string(frame: &Value, key: &'static str) -> std::result::Result<String, CommandError> {
    frame
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| CommandError {
            code: "invalid_command".to_string(),
            message: format!("{key} is required"),
        })
}

fn attachment_http_client() -> std::result::Result<reqwest::Client, CommandError> {
    reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(25))
        .build()
        .map_err(|error| CommandError {
            code: "attachments_unavailable".to_string(),
            message: format!("cannot create attachment client: {error}"),
        })
}

fn attachment_stage_command_error(message: String) -> CommandError {
    let lower = message.to_ascii_lowercase();
    let retryable = lower.contains("timed out")
        || lower.contains("timeout")
        || lower.contains("exceeded")
        || lower.contains("fetching attachment")
        || lower.contains("reading body for attachment")
        || lower.contains("http 429")
        || lower.contains("http 500")
        || lower.contains("http 502")
        || lower.contains("http 503")
        || lower.contains("http 504");
    CommandError {
        code: if retryable {
            "attachment_stage_outcome_unknown".to_string()
        } else {
            "attachment_stage_failed".to_string()
        },
        message,
    }
}

/// Helm attachments for non-Codex providers: fetch the blobs into an
/// input-scoped tmpdir the provider can read. Codex Helm fetches inside its
/// own bridge, so this returns nothing for it. Antigravity Helm carries a
/// plain string to its hook inbox (and that transport is itself broken), so
/// an attachment there is an explicit error, never a silently text-only send.
async fn stage_helm_attachments(
    config: &ShipperConfig,
    session_id: &str,
    provider: &str,
    input_id: Option<&str>,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> std::result::Result<Vec<crate::input_attachments::StagedAttachment>, CommandError> {
    if attachments.is_empty() || provider.eq_ignore_ascii_case("codex") {
        // Codex's app-server bridge fetches the durable refs itself. Staging
        // here would fetch every blob twice and leave an unused input cache.
        return Ok(Vec::new());
    }
    if crate::input_attachments::attachment_delivery(provider, "helm").is_none() {
        return Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("{provider} Helm does not accept image attachments"),
        });
    }
    let api_token = config
        .api_token
        .as_deref()
        .filter(|token| !token.trim().is_empty())
        .ok_or_else(|| CommandError {
            code: "attachments_unavailable".to_string(),
            message: "cannot fetch attachments without the Machine Agent token".to_string(),
        })?;
    // The durable command id scopes the staging directory when it is a plain
    // token; anything else gets a fresh UUID rather than a rejected path.
    let dir = input_id
        .filter(|value| !value.trim().is_empty())
        .and_then(|value| crate::input_attachments::helm_staging_dir(session_id, value).ok())
        .map(Ok)
        .unwrap_or_else(|| {
            crate::input_attachments::helm_staging_dir(
                session_id,
                &uuid::Uuid::new_v4().to_string(),
            )
        })
        .map_err(CommandError::command_failed)?;
    let http = attachment_http_client()?;
    crate::input_attachments::stage(
        &http,
        &config.api_url,
        api_token,
        session_id,
        attachments,
        &dir,
    )
    .await
    .map_err(CommandError::command_failed)
}

fn payload_required_string(
    payload: &Value,
    key: &'static str,
) -> std::result::Result<String, CommandError> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .map(str::to_string)
        .ok_or_else(|| CommandError {
            code: "invalid_command".to_string(),
            message: format!("payload.{key} is required"),
        })
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct LaunchResumeTarget {
    thread_id: String,
    thread_path: Option<String>,
}

fn payload_resume_target(
    payload: &Value,
) -> std::result::Result<Option<LaunchResumeTarget>, CommandError> {
    let mode = payload
        .get("mode")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("fresh");
    if mode != "fresh" && mode != "continue" {
        return Err(CommandError {
            code: "invalid_command".to_string(),
            message: "payload.mode must be fresh or continue".to_string(),
        });
    }
    let resume = payload.get("resume");
    if mode != "continue" && resume.is_none() {
        return Ok(None);
    }
    if mode != "continue" {
        return Err(CommandError {
            code: "invalid_command".to_string(),
            message: "payload.resume requires mode=continue".to_string(),
        });
    }
    let Some(resume) = resume.and_then(Value::as_object) else {
        return Err(CommandError {
            code: "invalid_command".to_string(),
            message: "payload.resume is required for mode=continue".to_string(),
        });
    };
    let thread_id = resume
        .get("thread_id")
        .or_else(|| resume.get("threadId"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(|| CommandError {
            code: "invalid_command".to_string(),
            message: "payload.resume.thread_id is required for mode=continue".to_string(),
        })?;
    let thread_path = resume
        .get("thread_path")
        .or_else(|| resume.get("threadPath"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    Ok(Some(LaunchResumeTarget {
        thread_id,
        thread_path,
    }))
}

fn payload_optional_string(payload: &Value, key: &'static str) -> Option<String> {
    payload
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn command_error(command_id: &str, code: &str, message: &str) -> Value {
    json!({
        "type": "command_result",
        "command_id": command_id,
        "ok": false,
        "error": {
            "code": code,
            "message": message,
        },
    })
}

#[derive(Debug)]
struct CommandError {
    code: String,
    message: String,
}

impl CommandError {
    fn command_failed(error: impl Into<anyhow::Error>) -> Self {
        Self {
            code: "command_failed".to_string(),
            message: error.into().to_string(),
        }
    }

    fn turn_ended(message: impl Into<String>) -> Self {
        Self {
            code: "turn_ended".to_string(),
            message: message.into(),
        }
    }

    fn session_not_attached(error: impl Into<anyhow::Error>) -> Self {
        Self {
            code: "session_not_attached".to_string(),
            message: error.into().to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use parking_lot::Mutex;
    use std::sync::Arc;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, TcpStream};
    use uuid::Uuid;

    const COMMAND_LAUNCH: &str = "session.launch";

    // Poison-tolerant on purpose: every mutation under this lock is made through an
    // RAII guard whose Drop restores the previous value, and Drop runs while unwinding.
    // So a panicking test leaves the environment clean, and the poison flag carries no
    // information -- it only converts one real failure into a wall of PoisonError noise
    // from every other test that shares the lock.

    fn command_cache() -> CompletedCommandCache {
        CompletedCommandCache::new(16, Duration::from_secs(60))
    }

    fn test_config() -> ShipperConfig {
        ShipperConfig {
            api_url: "http://localhost:8000".to_string(),
            api_token: Some("test-token".to_string()),
            machine_name: "test-machine".to_string(),
            ..ShipperConfig::default()
        }
    }

    #[test]
    fn console_interrupt_refuses_a_foreign_process_group() {
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                for (provider, adapter) in [
                    ("claude", CLAUDE_PRINT_ADAPTER),
                    ("cursor", CURSOR_PRINT_ADAPTER),
                    ("opencode", OPENCODE_RUN_ADAPTER),
                    ("pi", PI_PRINT_ADAPTER),
                    ("omp", OMP_PRINT_ADAPTER),
                    ("antigravity", ANTIGRAVITY_PRINT_ADAPTER),
                ] {
                    let mut owner = tokio::process::Command::new("sleep")
                        .arg("30")
                        .process_group(0)
                        .kill_on_drop(true)
                        .spawn()
                        .unwrap();
                    let mut unrelated = tokio::process::Command::new("sleep")
                        .arg("30")
                        .process_group(0)
                        .kill_on_drop(true)
                        .spawn()
                        .unwrap();
                    let pid = owner.id().unwrap();
                    let run_id = Uuid::new_v4().to_string();
                    let session_id = Uuid::new_v4().to_string();
                    let thread_id = Uuid::new_v4().to_string();
                    use std::os::unix::fs::OpenOptionsExt;
                    let rpc_dir = temp.path().join(format!("{provider}-{run_id}"));
                    std::fs::create_dir_all(&rpc_dir).unwrap();
                    let stdout_path = rpc_dir.join("stdout.log");
                    let stderr_path = rpc_dir.join("stderr.log");
                    let _rpc_reader = if matches!(provider, "pi" | "omp") {
                        let fifo = rpc_dir.join(crate::console_rpc::RPC_STDIN);
                        crate::console_rpc::create_fifo(&fifo).unwrap();
                        Some(
                            std::fs::OpenOptions::new()
                                .read(true)
                                .write(true)
                                .custom_flags(libc::O_NONBLOCK)
                                .open(fifo)
                                .unwrap(),
                        )
                    } else {
                        None
                    };
                    let registry = crate::turn_claims::default_registry().unwrap();
                    registry
                        .claim(
                            &run_id,
                            &session_id,
                            &thread_id,
                            Some("turn"),
                            None,
                            provider,
                        )
                        .unwrap();
                    registry
                        .mark_spawned_invocation(
                            &run_id,
                            pid,
                            unrelated.id().unwrap() as i32,
                            crate::turn_claims::process_start_time_for_pid(Some(pid)),
                            adapter,
                            "launch",
                            None,
                            &stdout_path.to_string_lossy(),
                            &stderr_path.to_string_lossy(),
                            json!({}),
                        )
                        .unwrap();
                    let command = json!({
                        "command_type": COMMAND_TURN_INTERRUPT,
                        "session_id": session_id,
                        "payload": {
                            "provider": provider, "run_id": run_id,
                            "thread_id": thread_id, "turn_id": "turn"
                        }
                    });
                    let refused = execute_command(&command, &test_config()).await;
                    tokio::time::sleep(Duration::from_millis(50)).await;
                    assert!(
                        refused.is_err(),
                        "{provider} accepted a foreign process group"
                    );
                    assert!(
                        unrelated.try_wait().unwrap().is_none()
                            && owner.try_wait().unwrap().is_none(),
                        "{provider} signalled an unowned group"
                    );
                    assert!(registry
                        .read(&run_id)
                        .unwrap()
                        .cancel_requested_at
                        .is_none());

                    registry
                        .mark_spawned_invocation(
                            &run_id,
                            pid,
                            pid as i32,
                            crate::turn_claims::process_start_time_for_pid(Some(pid)),
                            adapter,
                            "launch",
                            None,
                            &stdout_path.to_string_lossy(),
                            &stderr_path.to_string_lossy(),
                            json!({}),
                        )
                        .unwrap();
                    // Production has a provider monitor reaping the child while
                    // an interrupt may synchronously verify group termination.
                    let mut owner_wait = tokio::spawn(async move { owner.wait().await });
                    let accepted = execute_command(&command, &test_config()).await;
                    let owner_exited =
                        tokio::time::timeout(Duration::from_secs(2), &mut owner_wait).await;
                    if owner_exited.is_err() {
                        owner_wait.abort();
                        let _ = owner_wait.await;
                    }
                    let unrelated_survived = unrelated.try_wait().unwrap().is_none();
                    let _ = unrelated.kill().await;
                    assert!(
                        accepted.is_ok(),
                        "{provider} refused its owned group: {accepted:?}"
                    );
                    assert!(
                        matches!(owner_exited, Ok(Ok(Ok(_)))),
                        "{provider} did not interrupt its owner"
                    );
                    assert!(
                        unrelated_survived,
                        "{provider} interrupted the unrelated process"
                    );
                }
            });
        });
    }

    fn write_test_executable(path: &Path, body: &str) {
        std::fs::write(path, body).unwrap();
        #[cfg(unix)]
        {
            let mut perms = std::fs::metadata(path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(path, perms).unwrap();
        }
    }

    fn manifest_machine_control_supports() -> Vec<String> {
        managed_provider_contract_items()
            .iter()
            .flat_map(|contract| {
                contract
                    .get("machine_control_supports")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
            })
            .collect()
    }

    // This is a manually maintained mirror of the provider branches in
    // `execute_command`; it is not authoritative production dispatch logic.
    // Keep it synchronized by hand until parity can invoke the real handlers
    // hermetically without triggering provider side effects.
    const ENGINE_DISPATCH_SUPPORTS: &[(&str, &str, &str)] = &[
        ("codex", "send", COMMAND_SEND_TEXT),
        ("codex", "interrupt", COMMAND_INTERRUPT),
        ("codex", "steer", COMMAND_STEER_TEXT),
        ("codex", "answer_pause", COMMAND_ANSWER_PAUSE),
        ("codex", "run_once", COMMAND_RUN_ONCE),
        ("codex", "resume_run_once", COMMAND_RUN_ONCE),
        ("codex", "turn_start", COMMAND_TURN_START),
        ("codex", "turn_steer", COMMAND_TURN_STEER),
        ("codex", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("codex", "invocation_close", COMMAND_INVOCATION_CLOSE),
        ("opencode", "turn_start", COMMAND_TURN_START),
        ("opencode", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("opencode", "answer_pause", COMMAND_ANSWER_PAUSE),
        ("claude", "send", COMMAND_SEND_TEXT),
        ("claude", "interrupt", COMMAND_INTERRUPT),
        ("claude", "steer", COMMAND_STEER_TEXT),
        ("claude", "terminate", COMMAND_TERMINATE),
        ("claude", "answer_pause", COMMAND_ANSWER_PAUSE),
        ("claude", "turn_start", COMMAND_TURN_START),
        ("claude", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("claude", "turn_steer", COMMAND_TURN_STEER),
        ("claude", "invocation_close", COMMAND_INVOCATION_CLOSE),
        ("opencode", "send", COMMAND_SEND_TEXT),
        ("opencode", "interrupt", COMMAND_INTERRUPT),
        ("opencode", "steer", COMMAND_STEER_TEXT),
        ("opencode", "terminate", COMMAND_TERMINATE),
        ("antigravity", "send", COMMAND_SEND_TEXT),
        ("antigravity", "turn_start", COMMAND_TURN_START),
        ("cursor", "send", COMMAND_SEND_TEXT),
        ("cursor", "interrupt", COMMAND_INTERRUPT),
        ("cursor", "steer", COMMAND_STEER_TEXT),
        ("cursor", "terminate", COMMAND_TERMINATE),
        ("cursor", "turn_start", COMMAND_TURN_START),
        ("cursor", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("pi", "turn_start", COMMAND_TURN_START),
        ("pi", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("pi", "turn_steer", COMMAND_TURN_STEER),
        ("pi", "send", COMMAND_SEND_TEXT),
        ("pi", "steer", COMMAND_STEER_TEXT),
        ("pi", "interrupt", COMMAND_INTERRUPT),
        ("pi", "terminate", COMMAND_TERMINATE),
        ("omp", "send", COMMAND_SEND_TEXT),
        ("omp", "interrupt", COMMAND_INTERRUPT),
        ("omp", "steer", COMMAND_STEER_TEXT),
        ("omp", "terminate", COMMAND_TERMINATE),
        ("omp", "turn_start", COMMAND_TURN_START),
        ("omp", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("omp", "turn_steer", COMMAND_TURN_STEER),
        ("omp", "invocation_close", COMMAND_INVOCATION_CLOSE),
        ("opencode", "turn_steer", COMMAND_TURN_STEER),
    ];

    fn support_dispatch_command(provider: &str, operation: &str) -> Option<&'static str> {
        ENGINE_DISPATCH_SUPPORTS
            .iter()
            .find(|(supported_provider, supported_operation, _command)| {
                provider == *supported_provider && operation == *supported_operation
            })
            .map(|(_, _, command)| *command)
    }

    #[derive(Debug)]
    struct RecordedHttpRequest {
        target: String,
        body: String,
    }

    type RecordedHttpRequestRx = tokio::sync::oneshot::Receiver<RecordedHttpRequest>;

    struct FakeRuntimeHost {
        api_url: String,
        open_at: Arc<Mutex<Option<Instant>>>,
        reconnect_at_rx: tokio::sync::oneshot::Receiver<Instant>,
        outbox_event_rx: tokio::sync::oneshot::Receiver<Value>,
        tasks: Vec<tokio::task::JoinHandle<()>>,
    }

    async fn read_fake_http_request(stream: &mut TcpStream) -> (String, Vec<u8>) {
        let mut bytes = Vec::new();
        let mut chunk = [0_u8; 8192];
        let header_end = loop {
            let read = stream.read(&mut chunk).await.unwrap();
            assert!(read > 0, "fake Runtime Host received an incomplete request");
            bytes.extend_from_slice(&chunk[..read]);
            if let Some(end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                break end;
            }
        };
        let headers = String::from_utf8_lossy(&bytes[..header_end]).to_ascii_lowercase();
        let request_line = headers.lines().next().unwrap_or_default().to_string();
        let content_length = headers
            .lines()
            .filter_map(|line| line.split_once(':'))
            .find(|(name, _)| name.trim() == "content-length")
            .and_then(|(_, value)| value.trim().parse::<usize>().ok())
            .unwrap_or_default();
        let body_start = header_end + 4;
        let mut body = bytes.get(body_start..).unwrap_or_default().to_vec();
        body.truncate(content_length);
        while body.len() < content_length {
            let remaining = content_length - body.len();
            let limit = remaining.min(chunk.len());
            let read = stream.read(&mut chunk[..limit]).await.unwrap();
            assert!(read > 0, "fake Runtime Host received a truncated body");
            body.extend_from_slice(&chunk[..read]);
        }
        (request_line, body)
    }

    async fn write_fake_http_response(stream: &mut TcpStream, status: u16, body: &[u8]) {
        let reason = match status {
            200 => "OK",
            204 => "No Content",
            404 => "Not Found",
            _ => "Test Response",
        };
        let headers = format!(
            "HTTP/1.1 {status} {reason}\r\n\
             Content-Type: application/json\r\n\
             Content-Length: {}\r\n\
             Connection: close\r\n\r\n",
            body.len()
        );
        stream.write_all(headers.as_bytes()).await.unwrap();
        stream.write_all(body).await.unwrap();
    }

    async fn spawn_fake_runtime_host() -> FakeRuntimeHost {
        let http_listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let http_addr = http_listener.local_addr().unwrap();
        let websocket_listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let websocket_addr = websocket_listener.local_addr().unwrap();
        let front_listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let front_addr = front_listener.local_addr().unwrap();
        let open_at: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
        let (reconnect_at_tx, reconnect_at_rx) = tokio::sync::oneshot::channel();
        let (outbox_event_tx, outbox_event_rx) = tokio::sync::oneshot::channel();

        let http_open_at = open_at.clone();
        let http_task = tokio::spawn(async move {
            let mut outbox_event_tx = Some(outbox_event_tx);
            loop {
                let (mut stream, _) = http_listener.accept().await.unwrap();
                let (request, body) = read_fake_http_request(&mut stream).await;
                let path = request.split_whitespace().nth(1).unwrap_or_default();
                if path == "/api/health" {
                    let is_open = match *http_open_at.lock() {
                        Some(opens_at) => Instant::now() >= opens_at,
                        None => false,
                    };
                    let response = if is_open {
                        json!({"runtime":{"epoch":"runtime-new","admission":"open"}})
                    } else {
                        json!({"runtime":{"epoch":"runtime-old","admission":"draining"}})
                    };
                    write_fake_http_response(
                        &mut stream,
                        200,
                        &serde_json::to_vec(&response).unwrap(),
                    )
                    .await;
                } else if path == "/api/agents/runtime/events/batch" {
                    if let (Some(sender), Ok(payload)) = (
                        outbox_event_tx.take(),
                        serde_json::from_slice::<Value>(&body),
                    ) {
                        let _ = sender.send(payload);
                    }
                    write_fake_http_response(&mut stream, 204, &[]).await;
                } else {
                    write_fake_http_response(&mut stream, 404, b"{}").await;
                }
            }
        });

        let websocket_open_at = open_at.clone();
        let websocket_task = tokio::spawn(async move {
            let mut first_connection = true;
            let mut reconnect_at_tx = Some(reconnect_at_tx);
            loop {
                let (stream, _) = websocket_listener.accept().await.unwrap();
                if first_connection {
                    first_connection = false;
                    let mut websocket = tokio_tungstenite::accept_async(stream).await.unwrap();
                    let hello = websocket.next().await.unwrap().unwrap();
                    let Message::Text(hello) = hello else {
                        panic!("expected the engine hello frame");
                    };
                    assert_eq!(
                        serde_json::from_str::<Value>(hello.as_ref()).unwrap()["type"],
                        "hello"
                    );
                    let now = chrono::Utc::now();
                    let open_time = Instant::now() + Duration::from_secs(3);
                    *websocket_open_at.lock() = Some(open_time);
                    let timestamp =
                        |seconds| (now + chrono::Duration::seconds(seconds)).to_rfc3339();
                    let lifecycle = json!({
                        "type": "host.lifecycle",
                        "state": "updating",
                        "runtime_epoch": "runtime-old",
                        "attempt_id": "fake-attempt",
                        "phase": "drain",
                        "expected_back_by": timestamp(30),
                        "deadline": timestamp(360),
                        "cutoff": timestamp(960)
                    });
                    websocket
                        .send(Message::Text(lifecycle.to_string().into()))
                        .await
                        .unwrap();
                    websocket
                        .send(Message::Close(Some(
                            tokio_tungstenite::tungstenite::protocol::frame::CloseFrame {
                                code: CloseCode::Restart,
                                reason: "host.lifecycle".into(),
                            },
                        )))
                        .await
                        .unwrap();
                    continue;
                }

                let opens_at = *websocket_open_at.lock();
                if opens_at.is_none_or(|opens_at| Instant::now() < opens_at) {
                    drop(stream);
                    continue;
                }
                let mut websocket = tokio_tungstenite::accept_async(stream).await.unwrap();
                let hello = websocket.next().await.unwrap().unwrap();
                let Message::Text(hello) = hello else {
                    panic!("expected the engine reconnect hello frame");
                };
                assert_eq!(
                    serde_json::from_str::<Value>(hello.as_ref()).unwrap()["type"],
                    "hello"
                );
                if let Some(sender) = reconnect_at_tx.take() {
                    let _ = sender.send(Instant::now());
                }
                let serving = json!({
                    "type": "host.lifecycle",
                    "state": "serving",
                    "runtime_epoch": "runtime-new",
                    "attempt_id": null,
                    "phase": null,
                    "expected_back_by": null,
                    "deadline": null,
                    "cutoff": null
                });
                websocket
                    .send(Message::Text(serving.to_string().into()))
                    .await
                    .unwrap();
                while let Some(message) = websocket.next().await {
                    if matches!(message, Ok(Message::Close(_)) | Err(_)) {
                        break;
                    }
                }
            }
        });

        let proxy_http_addr = http_addr;
        let proxy_websocket_addr = websocket_addr;
        let proxy_task = tokio::spawn(async move {
            loop {
                let (mut incoming, _) = front_listener.accept().await.unwrap();
                let http_addr = proxy_http_addr;
                let websocket_addr = proxy_websocket_addr;
                tokio::spawn(async move {
                    let mut request_line = Vec::new();
                    loop {
                        let mut byte = [0_u8; 1];
                        let Ok(read) = incoming.read(&mut byte).await else {
                            return;
                        };
                        if read == 0 {
                            return;
                        }
                        request_line.push(byte[0]);
                        if request_line.ends_with(b"\r\n") {
                            break;
                        }
                    }
                    let target = String::from_utf8_lossy(&request_line);
                    let backend_addr = if target.contains("/api/agents/control/ws") {
                        websocket_addr
                    } else {
                        http_addr
                    };
                    let Ok(mut backend) = TcpStream::connect(backend_addr).await else {
                        return;
                    };
                    if backend.write_all(&request_line).await.is_ok() {
                        let _ = tokio::io::copy_bidirectional(&mut incoming, &mut backend).await;
                    }
                });
            }
        });

        FakeRuntimeHost {
            api_url: format!("http://{front_addr}"),
            open_at,
            reconnect_at_rx,
            outbox_event_rx,
            tasks: vec![http_task, websocket_task, proxy_task],
        }
    }

    #[test]
    fn fake_runtime_host_reconnects_within_one_second_and_flushes_the_outbox() {
        let _guard = crate::console_adapter::agent_state_guard();
        let runtime = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let temp = tempfile::tempdir().unwrap();
        let empty_path = tempfile::tempdir().unwrap();
        let vars = [
            ("HOME", Some(temp.path().as_os_str())),
            ("LONGHOUSE_HOME", Some(temp.path().as_os_str())),
            ("PATH", Some(empty_path.path().as_os_str())),
            ("LONGHOUSE_CODEX_BIN", None::<&std::ffi::OsStr>),
            ("LONGHOUSE_CLAUDE_BIN", None::<&std::ffi::OsStr>),
            ("LONGHOUSE_OPENCODE_BIN", None::<&std::ffi::OsStr>),
            ("LONGHOUSE_ANTIGRAVITY_BIN", None::<&std::ffi::OsStr>),
            ("LONGHOUSE_CURSOR_BIN", None::<&std::ffi::OsStr>),
            ("LONGHOUSE_PI_BIN", None::<&std::ffi::OsStr>),
            ("LONGHOUSE_OMP_BIN", None::<&std::ffi::OsStr>),
        ];
        temp_env::with_vars(vars, || {
            runtime.block_on(async {
                let mut fake_host = spawn_fake_runtime_host().await;
                let db_path = temp.path().join("runtime-host-test.sqlite");
                let config = ShipperConfig {
                    api_url: fake_host.api_url.clone(),
                    api_token: Some("fake-device-token".to_string()),
                    machine_name: "fake-machine".to_string(),
                    db_path: Some(db_path),
                    ..ShipperConfig::default()
                };
                let client = crate::shipping::client::ShipperClient::with_compression(
                    &config,
                    crate::pipeline::compressor::CompressionAlgo::Gzip,
                )
                .unwrap();
                let host_link = client.host_link().clone();
                let status = new_control_channel_status();
                let reconnect_task =
                    tokio::spawn(run_reconnect_loop(config, status, host_link.clone()));

                let outbox_dir = temp.path().join("runtime-event-outbox");
                let queued_event = json!({
                    "runtime_key": "claude:fake-session",
                    "session_id": "fake-session",
                    "provider": "claude",
                    "device_id": "fake-machine",
                    "source": "claude_channel_wrapper",
                    "kind": "terminal_signal",
                    "occurred_at": chrono::Utc::now().to_rfc3339(),
                    "dedupe_key": "fake-terminal-signal",
                    "payload": {
                        "terminal_state": "session_ended",
                        "terminal_reason": "provider_exit",
                        "terminal_source": "claude_channel_wrapper",
                        "provider_session_id": "fake-session",
                        "exit_code": 0
                    }
                });
                crate::outbox::enqueue_runtime_event(&outbox_dir, &queued_event).unwrap();

                let polling_client = client.clone();
                let polling_link = host_link.clone();
                let poll_task = tokio::spawn(async move {
                    loop {
                        if polling_link.is_updating() {
                            let _ = polling_client.poll_runtime_admission().await;
                            if polling_link.snapshot().state == "serving" {
                                return;
                            }
                            tokio::time::sleep(Duration::from_secs(1)).await;
                        } else if polling_link.snapshot().state == "serving" {
                            return;
                        } else {
                            tokio::time::sleep(Duration::from_millis(25)).await;
                        }
                    }
                });

                let flush_link = host_link.clone();
                let flush_client = client.clone();
                let flush_dir = outbox_dir.clone();
                let flush_task = tokio::spawn(async move {
                    let mut changed = flush_link.subscribe();
                    loop {
                        if flush_link.serving_generation() > 0
                            && flush_link.snapshot().state == "serving"
                        {
                            return crate::outbox::drain_runtime_event_outbox(
                                &flush_dir,
                                &flush_client,
                            )
                            .await;
                        }
                        if changed.changed().await.is_err() {
                            return (0, 1);
                        }
                    }
                });

                let reconnect_at =
                    tokio::time::timeout(Duration::from_secs(8), &mut fake_host.reconnect_at_rx)
                        .await
                        .expect("engine did not reconnect after Runtime Host opened")
                        .expect("fake Runtime Host reconnect observer stopped");
                let opened_at = (*fake_host.open_at.lock())
                    .expect("fake Runtime Host did not schedule its open");
                let reconnect_latency = reconnect_at.duration_since(opened_at);
                println!(
                    "fake Runtime Host reconnect latency: {} ms",
                    reconnect_latency.as_millis()
                );
                assert!(
                    reconnect_latency <= Duration::from_secs(1),
                    "engine reconnect took {:?} after admission opened",
                    reconnect_latency
                );

                let (sent, kept) = tokio::time::timeout(Duration::from_secs(3), flush_task)
                    .await
                    .expect("queued runtime-event outbox item was not flushed")
                    .unwrap();
                assert_eq!((sent, kept), (1, 0));
                let posted =
                    tokio::time::timeout(Duration::from_secs(2), &mut fake_host.outbox_event_rx)
                        .await
                        .expect("fake Runtime Host did not receive the queued event")
                        .expect("outbox receiver stopped");
                assert_eq!(posted["events"][0]["session_id"], "fake-session");

                reconnect_task.abort();
                poll_task.abort();
                for task in fake_host.tasks.drain(..) {
                    task.abort();
                }
            });
        });
    }
    async fn spawn_single_http_request_server() -> (String, RecordedHttpRequestRx) {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let addr = listener.local_addr().unwrap();
        let (tx, rx) = tokio::sync::oneshot::channel();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut bytes = Vec::new();
            let mut header_end = None;
            let mut content_length = 0usize;
            loop {
                let mut chunk = [0u8; 1024];
                let read = stream.read(&mut chunk).await.unwrap();
                if read == 0 {
                    break;
                }
                bytes.extend_from_slice(&chunk[..read]);
                if header_end.is_none() {
                    header_end = http_header_end(&bytes);
                    if let Some(end) = header_end {
                        let head = String::from_utf8_lossy(&bytes[..end]);
                        content_length = http_content_length(&head);
                    }
                }
                if let Some(end) = header_end {
                    if bytes.len() >= end + 4 + content_length {
                        break;
                    }
                }
            }
            let request = parse_http_request(&bytes);
            let _ = tx.send(request);
            stream
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}",
                )
                .await
                .unwrap();
        });
        (format!("http://{addr}"), rx)
    }

    async fn spawn_claude_inject_server() -> (u16, tokio::sync::mpsc::Receiver<RecordedHttpRequest>)
    {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = tokio::sync::mpsc::channel(8);
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let tx = tx.clone();
                tokio::spawn(async move {
                    let mut bytes = Vec::new();
                    let mut header_end = None;
                    let mut content_length = 0usize;
                    loop {
                        let mut chunk = [0u8; 1024];
                        let read = stream.read(&mut chunk).await.unwrap();
                        if read == 0 {
                            break;
                        }
                        bytes.extend_from_slice(&chunk[..read]);
                        if header_end.is_none() {
                            header_end = http_header_end(&bytes);
                            if let Some(end) = header_end {
                                let head = String::from_utf8_lossy(&bytes[..end]);
                                content_length = http_content_length(&head);
                            }
                        }
                        if let Some(end) = header_end {
                            if bytes.len() >= end + 4 + content_length {
                                break;
                            }
                        }
                    }
                    let request = parse_http_request(&bytes);
                    let _ = tx.send(request).await;
                    stream
                        .write_all(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                        .await
                        .unwrap();
                });
            }
        });
        (port, rx)
    }

    fn write_claude_channel_state(home: &Path, session_id: &str, port: u16) {
        let state_path = home
            .join(".claude/channels/longhouse/sessions")
            .join(format!("{session_id}.json"));
        std::fs::create_dir_all(state_path.parent().unwrap()).unwrap();
        std::fs::write(
            state_path,
            serde_json::to_vec(&json!({
                "session_id": session_id,
                "provider_session_id": "claude-provider-1",
                "auth_token": "test-channel-token",
                "port": port,
                "claude_pid": 12345,
                "ready": true,
            }))
            .unwrap(),
        )
        .unwrap();
    }

    fn http_header_end(bytes: &[u8]) -> Option<usize> {
        bytes.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn http_content_length(head: &str) -> usize {
        head.lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0)
    }

    fn parse_http_request(bytes: &[u8]) -> RecordedHttpRequest {
        let text = String::from_utf8_lossy(bytes);
        let (head, body) = text.split_once("\r\n\r\n").unwrap_or((&text, ""));
        let target = head
            .lines()
            .next()
            .and_then(|line| line.split_whitespace().nth(1))
            .unwrap()
            .to_string();
        RecordedHttpRequest {
            target,
            body: body.to_string(),
        }
    }

    fn write_opencode_control_state(config_dir: &Path, session_id: &str, server_url: &str) {
        let state_dir = config_dir.join("managed-local").join("opencode-server");
        std::fs::create_dir_all(&state_dir).unwrap();
        std::fs::write(
            state_dir.join(format!("{session_id}.json")),
            serde_json::to_string(&json!({
                "schema_version": 1,
                "session_id": session_id,
                "provider_session_id": "ses_native",
                "server_url": server_url,
                "cwd": "/tmp/native opencode",
                "username": "opencode",
                "password": "secret-password",
            }))
            .unwrap(),
        )
        .unwrap();
    }

    #[test]
    fn control_ws_url_converts_http_and_https() {
        assert_eq!(
            control_ws_url("http://localhost:8000", false).unwrap(),
            "ws://localhost:8000/api/agents/control/ws"
        );
        assert_eq!(
            control_ws_url("https://demo.longhouse.ai/", false).unwrap(),
            "wss://demo.longhouse.ai/api/agents/control/ws"
        );
    }

    #[test]
    fn control_ws_url_allows_plaintext_to_loopback_and_tailscale_only() {
        for (allowed, ws) in [
            ("http://127.0.0.1:8000", "ws://127.0.0.1:8000"),
            ("http://localhost:8000", "ws://localhost:8000"),
            ("http://[::1]:8000", "ws://[::1]:8000"),
            ("http://100.64.0.1:8000/", "ws://100.64.0.1:8000"),
            (
                "http://box.tail1234.ts.net:8000",
                "ws://box.tail1234.ts.net:8000",
            ),
            (
                "http://[fd7a:115c:a1e0::1]:8000",
                "ws://[fd7a:115c:a1e0::1]:8000",
            ),
        ] {
            assert_eq!(
                control_ws_url(allowed, false).unwrap(),
                format!("{ws}/api/agents/control/ws"),
                "{allowed} should be allowed"
            );
        }
        for refused in [
            "http://demo.longhouse.ai",
            "http://100.63.255.255:8000",
            "http://100.128.0.0:8000",
            "http://localhost.attacker.example:8000",
            "http://user@evil.example/",
        ] {
            // Not even the LAN opt-in opens a public address.
            for allow in [false, true] {
                let err = control_ws_url(refused, allow).unwrap_err().to_string();
                assert!(
                    err.contains("Refusing plaintext"),
                    "{refused} should be refused, got: {err}"
                );
            }
        }
    }

    #[test]
    fn control_ws_url_reads_the_scheme_case_insensitively() {
        assert_eq!(
            control_ws_url("HTTP://100.64.0.1:8000", false).unwrap(),
            "ws://100.64.0.1:8000/api/agents/control/ws"
        );
        assert_eq!(
            control_ws_url("Https://demo.longhouse.ai", false).unwrap(),
            "wss://demo.longhouse.ai/api/agents/control/ws"
        );
        assert!(control_ws_url("HTTP://demo.longhouse.ai", false)
            .unwrap_err()
            .to_string()
            .contains("Refusing plaintext"));
    }

    #[test]
    fn control_ws_url_keeps_a_lan_address_behind_the_opt_in() {
        let refused = control_ws_url("http://192.168.1.20:8000", false)
            .unwrap_err()
            .to_string();
        assert!(
            refused.contains("--allow-insecure-http"),
            "the refusal must name the opt-in, got: {refused}"
        );
        assert_eq!(
            control_ws_url("http://192.168.1.20:8000", true).unwrap(),
            "ws://192.168.1.20:8000/api/agents/control/ws"
        );
    }

    #[test]
    fn heartbeat_frame_uses_server_schema() {
        assert_eq!(heartbeat_frame(), json!({"type": "heartbeat"}));
    }

    #[test]
    fn readiness_update_is_sent_only_when_capabilities_change() {
        let signed_out = json!({
            "supports": ["claude.turn_start"],
            "provider_readiness": {"claude": {"state": "not_authenticated"}},
        });
        let signed_in = json!({
            "supports": ["claude.turn_start"],
            "provider_readiness": {"claude": {"state": "ready", "detail": "max plan"}},
        });

        assert_eq!(readiness_update_frame(&signed_out, &signed_out), None);
        assert_eq!(
            readiness_update_frame(&signed_out, &signed_in),
            Some(json!({
                "type": "readiness_update",
                "supports": ["claude.turn_start"],
                "provider_readiness": {"claude": {"state": "ready", "detail": "max plan"}},
            }))
        );
    }

    #[test]
    fn heartbeat_interval_stays_inside_server_keepalive_window() {
        assert!(HEARTBEAT_INTERVAL_SECS <= 10);
    }

    #[test]
    fn reconnect_backoff_stays_short_then_backs_off_for_sustained_outages() {
        let short_window = Duration::from_secs(CONTROL_RECONNECT_SHORT_WINDOW_SECS - 1);
        assert_eq!(
            next_reconnect_backoff(Duration::from_secs(4), short_window),
            Duration::from_secs(CONTROL_RECONNECT_SHORT_MAX_BACKOFF_SECS)
        );

        let sustained = Duration::from_secs(CONTROL_RECONNECT_SHORT_WINDOW_SECS + 1);
        assert_eq!(
            next_reconnect_backoff(
                Duration::from_secs(CONTROL_RECONNECT_SHORT_MAX_BACKOFF_SECS),
                sustained
            ),
            Duration::from_secs(CONTROL_RECONNECT_SHORT_MAX_BACKOFF_SECS * 2)
        );
        assert_eq!(
            next_reconnect_backoff(Duration::from_secs(20), sustained),
            Duration::from_secs(CONTROL_RECONNECT_SUSTAINED_MAX_BACKOFF_SECS)
        );
    }

    #[test]
    fn control_channel_status_tracks_connection_state() {
        let _guard = crate::console_adapter::agent_state_guard();
        let status = new_control_channel_status();
        assert_eq!(status.snapshot().enabled, false);
        assert_eq!(status.snapshot().status, "disabled");

        status.set_disconnected(
            Some("wss://example.test/api/agents/control/ws"),
            Some("connect_failed"),
            Some("tls handshake failed"),
            Some(4),
        );
        let disconnected = status.snapshot();
        assert_eq!(disconnected.enabled, true);
        assert_eq!(disconnected.status, "disconnected");
        assert_eq!(
            disconnected.ws_url.as_deref(),
            Some("wss://example.test/api/agents/control/ws")
        );
        assert_eq!(
            disconnected.last_error_code.as_deref(),
            Some("connect_failed")
        );
        assert_eq!(disconnected.supports, control_supports());

        status.set_connected("wss://example.test/api/agents/control/ws");
        let connected = status.snapshot();
        assert_eq!(connected.status, "connected");
        assert_eq!(connected.last_error_code, None);
        assert_eq!(connected.reconnect_backoff_seconds, None);
        assert!(connected.last_connected_at.is_some());
    }

    #[test]
    fn control_channel_keeps_codex_console_and_helm_controls_without_remote_launch() {
        let codex_contract = managed_provider_contract_items()
            .iter()
            .find(|item| item.get("provider").and_then(Value::as_str) == Some("codex"))
            .expect("codex contract exists");
        let supports = codex_contract
            .get("machine_control_supports")
            .and_then(Value::as_array)
            .expect("codex contract has machine_control_supports");
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("codex.send")));
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("codex.run_once")));
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("codex.resume_run_once")));
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("codex.turn_start")));
        assert!(!supports
            .iter()
            .any(|item| item.as_str() == Some("codex.launch")));
        assert!(!supports
            .iter()
            .any(|item| item.as_str() == Some("codex.continue")));
        assert_eq!(
            codex_contract.get("launch_local").and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn control_channel_keeps_claude_helm_controls_without_remote_launch() {
        let claude_contract = managed_provider_contract_items()
            .iter()
            .find(|item| item.get("provider").and_then(Value::as_str) == Some("claude"))
            .expect("claude contract exists");
        let supports = claude_contract
            .get("machine_control_supports")
            .and_then(Value::as_array)
            .expect("claude contract has machine_control_supports");
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("claude.send")));
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("claude.answer_pause")));
        assert!(!supports
            .iter()
            .any(|item| item.as_str() == Some("claude.launch")));
        assert!(!supports
            .iter()
            .any(|item| item.as_str() == Some("claude.continue")));
        assert_eq!(
            claude_contract.get("can_resume").and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn launch_resume_target_parses_continue_payload() {
        let target = payload_resume_target(&json!({
            "mode": "continue",
            "resume": {
                "thread_id": "thread-abc",
                "thread_path": "/tmp/thread-abc.jsonl",
            }
        }))
        .unwrap()
        .unwrap();

        assert_eq!(target.thread_id, "thread-abc");
        assert_eq!(target.thread_path.as_deref(), Some("/tmp/thread-abc.jsonl"));
    }

    #[test]
    fn launch_resume_target_requires_thread_id_for_continue() {
        let err = payload_resume_target(&json!({
            "mode": "continue",
            "resume": {
                "thread_path": "/tmp/thread-abc.jsonl",
            }
        }))
        .unwrap_err();

        assert_eq!(err.code, "invalid_command");
        assert!(err.message.contains("thread_id"));
    }

    #[test]
    fn launch_resume_target_rejects_resume_without_continue_mode() {
        let err = payload_resume_target(&json!({
            "resume": {
                "thread_id": "thread-abc",
            }
        }))
        .unwrap_err();

        assert_eq!(err.code, "invalid_command");
        assert!(err.message.contains("mode=continue"));
    }

    #[test]
    fn console_turn_provider_admission_covers_every_admitted_console_adapter() {
        for provider in ["codex", "claude", "opencode", "cursor", "pi"] {
            assert!(
                console_turn_provider_supported(provider),
                "{provider} must reach its Console adapter"
            );
        }
        assert!(console_turn_provider_supported("antigravity"));
        assert!(!console_turn_provider_supported("unknown"));
    }

    #[test]
    fn console_provider_uses_the_exact_staged_binary_override() {
        let staged = OsString::from("/provider/codex-1.2.3/codex");
        let resolved = console_provider_binary_with_env("codex", &|name| {
            (name == "LONGHOUSE_CODEX_BIN").then(|| staged.clone())
        });
        assert_eq!(resolved, "/provider/codex-1.2.3/codex");
        assert_eq!(
            console_provider_binary_with_env("codex", &|_| None),
            DEFAULT_CODEX_BIN
        );

        let staged_claude = OsString::from("/provider/claude-4.5.0/claude-staged");
        let resolved_claude = console_provider_binary_with_env("claude", &|name| {
            (name == "LONGHOUSE_CLAUDE_BIN").then(|| staged_claude.clone())
        });
        assert_eq!(resolved_claude, "/provider/claude-4.5.0/claude-staged");
    }
    #[test]
    fn provider_readiness_snapshot_never_discovers_ambient_provider_clis() {
        let _guard = crate::console_adapter::agent_state_guard();
        let empty_path = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let snapshot = temp_env::with_vars(
            [
                ("PATH", Some(empty_path.path().as_os_str())),
                ("LONGHOUSE_CODEX_BIN", None::<&OsStr>),
                ("LONGHOUSE_CLAUDE_BIN", None::<&OsStr>),
                ("LONGHOUSE_OPENCODE_BIN", None::<&OsStr>),
                ("LONGHOUSE_ANTIGRAVITY_BIN", None::<&OsStr>),
                ("LONGHOUSE_CURSOR_BIN", None::<&OsStr>),
                ("LONGHOUSE_PI_BIN", None::<&OsStr>),
                ("LONGHOUSE_OMP_BIN", None::<&OsStr>),
            ],
            || runtime.block_on(provider_readiness_snapshot()),
        );

        let entries = snapshot.as_object().unwrap();
        assert!(!entries.is_empty());
        assert!(entries
            .values()
            .all(|entry| { entry.get("state").and_then(Value::as_str) == Some("cli_missing") }));
    }

    #[test]
    fn provider_readiness_snapshot_runs_only_an_explicit_binary_override() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let empty_path_entry = temp.path().join("empty");
        let fake_codex = temp.path().join("codex");
        write_test_executable(&fake_codex, "#!/bin/sh\nexit 0\n");
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let snapshot = temp_env::with_vars(
            [
                ("PATH", Some(empty_path_entry.as_os_str())),
                ("LONGHOUSE_CODEX_BIN", Some(fake_codex.as_os_str())),
                ("LONGHOUSE_CLAUDE_BIN", None::<&OsStr>),
                ("LONGHOUSE_OPENCODE_BIN", None::<&OsStr>),
                ("LONGHOUSE_ANTIGRAVITY_BIN", None::<&OsStr>),
                ("LONGHOUSE_CURSOR_BIN", None::<&OsStr>),
                ("LONGHOUSE_PI_BIN", None::<&OsStr>),
                ("LONGHOUSE_OMP_BIN", None::<&OsStr>),
            ],
            || runtime.block_on(provider_readiness_snapshot()),
        );

        assert_eq!(snapshot["codex"]["state"], "ready");
        for provider in ["claude", "opencode", "antigravity", "cursor", "pi", "omp"] {
            assert_eq!(snapshot[provider]["state"], "cli_missing", "{provider}");
        }
    }

    #[test]
    fn managed_provider_contract_manifest_includes_operation_evidence() {
        let payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON).unwrap();
        validate_managed_provider_contract_manifest(&payload).unwrap();
        assert_eq!(payload["schema_version"].as_u64(), Some(1));
        let providers = payload["providers"].as_array().unwrap();
        for provider in providers {
            let provider_name = provider["provider"].as_str().unwrap();
            let evidence = provider["operation_evidence"].as_object().unwrap();
            for operation in [
                "launch_local",
                "run_once",
                "reattach",
                "send_input",
                "interrupt",
                "steer_active_turn",
                "answer_pause",
                "turn_start",
                "terminate",
                "tail_output",
                "runtime_phase",
                "transcript_binding",
                "fork_thread",
            ] {
                let supported = provider[operation].as_bool().unwrap();
                let level = evidence[operation]["level"].as_str().unwrap();
                assert!(
                    !evidence[operation]["source"]
                        .as_str()
                        .unwrap_or_default()
                        .trim()
                        .is_empty(),
                    "{provider_name}.{operation} missing evidence source"
                );
                assert_eq!(
                    level == "none",
                    !supported,
                    "{provider_name}.{operation} support and evidence level diverged"
                );
            }
        }
    }

    #[test]
    fn managed_provider_contract_manifest_validation_rejects_evidence_drift() {
        let mut payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON).unwrap();
        let first_provider = payload["providers"][0].as_object_mut().unwrap();
        first_provider
            .get_mut("operation_evidence")
            .unwrap()
            .as_object_mut()
            .unwrap()
            .insert(
                "made_up".to_string(),
                json!({"level": "none", "source": "test"}),
            );

        let error = validate_managed_provider_contract_manifest(&payload).unwrap_err();
        assert!(error.contains("unknown operation_evidence key made_up"));
    }

    #[test]
    fn managed_provider_contract_manifest_rejects_unadmitted_console_support() {
        let mut payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON).unwrap();
        let antigravity = payload["providers"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|provider| provider["provider"] == "antigravity")
            .unwrap();
        // Divergence in either direction is the defect. Asserting it against a
        // provider the engine *does* admit keeps this test honest as providers
        // are promoted: it cannot quietly start passing because the example
        // provider gained a Console adapter.
        assert!(console_turn_provider_supported("antigravity"));
        antigravity["turn_start"] = json!(false);
        // Keep the per-operation support/evidence pair self-consistent so the
        // Console-admission check is the one that fires, not the earlier
        // evidence-level guard.
        antigravity["operation_evidence"]["turn_start"]["level"] = json!("none");

        let error = validate_managed_provider_contract_manifest(&payload).unwrap_err();
        assert!(error.contains("manifest support and real Console admission diverge"));
    }

    #[test]
    fn managed_provider_contract_manifest_rejects_console_control_for_maintenance_tier() {
        let mut payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON).unwrap();
        let codex = payload["providers"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|provider| provider["provider"] == "codex")
            .unwrap();
        codex["support_tier"] = json!("maintenance");

        let error = validate_managed_provider_contract_manifest(&payload).unwrap_err();
        assert!(error.contains("maintenance providers cannot advertise Console control"));
    }

    #[test]
    fn managed_provider_contract_manifest_rejects_invocation_close_without_pending_work() {
        let mut payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON).unwrap();
        let pi = payload["providers"]
            .as_array_mut()
            .unwrap()
            .iter_mut()
            .find(|provider| provider["provider"] == "pi")
            .unwrap();
        pi["machine_control_supports"]
            .as_array_mut()
            .unwrap()
            .push(json!("pi.invocation_close"));

        let error = validate_managed_provider_contract_manifest(&payload).unwrap_err();
        assert!(error.contains("invocation_close is only admitted"));
    }
    #[test]
    fn manifest_machine_control_supports_have_engine_dispatch_paths() {
        for support in manifest_machine_control_supports() {
            let (provider, operation) = support
                .split_once('.')
                .unwrap_or_else(|| panic!("support {support} must be provider.operation"));
            assert!(
                support_dispatch_command(provider, operation).is_some(),
                "manifest advertises {support}, but engine dispatch has no provider operation path"
            );
        }
    }

    /// `ENGINE_DISPATCH_SUPPORTS` is a hand-kept mirror, so the tests above can
    /// agree with it while `execute_command` disagrees. This one drives the
    /// real dispatcher: for every manifest provider, `session.terminate` against
    /// a session that does not exist either reaches that provider's terminate
    /// path (any error but `unsupported_command`) or falls through to
    /// `unsupported_command`. Which one must match `<provider>.terminate` in the
    /// manifest, in both directions.
    #[test]
    fn terminate_dispatch_matches_manifest_terminate_support_for_every_provider() {
        let supports = manifest_machine_control_supports();
        let providers: std::collections::BTreeSet<String> = managed_provider_contract_items()
            .iter()
            .filter_map(|contract| contract.get("provider").and_then(Value::as_str))
            .map(str::to_string)
            .collect();
        assert!(!providers.is_empty());
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        for provider in providers {
            let frame = json!({
                "command_type": COMMAND_TERMINATE,
                "command_id": uuid::Uuid::new_v4().to_string(),
                "session_id": uuid::Uuid::new_v4().to_string(),
                "payload": {"provider": provider},
            });
            let outcome = temp_env::with_vars(
                [
                    ("LONGHOUSE_HOME", Some(temp.path().as_os_str())),
                    ("HOME", Some(temp.path().as_os_str())),
                ],
                || runtime.block_on(execute_command(&frame, &ShipperConfig::default())),
            );
            let dispatched = !matches!(&outcome, Err(error) if error.code == "unsupported_command");
            let declared = supports.contains(&format!("{provider}.terminate"));
            assert_eq!(
                dispatched, declared,
                "{provider}: execute_command terminate dispatched={dispatched}, manifest declares {provider}.terminate={declared} ({outcome:?})"
            );
        }
    }

    #[test]
    fn reducer_control_grants_follow_dispatch_manifest_and_connection_state() {
        assert_eq!(
            granted_control_operations("cursor", true),
            ["interrupt", "send_input", "terminate"]
        );
        // Antigravity grants send and nothing else: the hook inbox delivers a
        // user turn and has no interrupt or active-turn steer to deliver.
        assert_eq!(
            granted_control_operations("antigravity", true),
            ["send_input"]
        );
        // Detached means the hook has not been seen; the grant goes with it.
        assert!(granted_control_operations("antigravity", false).is_empty());
        assert!(granted_control_operations("antigravity", false).is_empty());
        assert!(granted_control_operations("cursor", false).is_empty());
        assert!(granted_control_operations("unknown", true).is_empty());
    }

    #[test]
    fn managed_engine_dispatch_paths_are_manifest_backed() {
        let supports = manifest_machine_control_supports();
        for (provider, operation, _command) in ENGINE_DISPATCH_SUPPORTS {
            let support = format!("{provider}.{operation}");
            assert!(
                supports.contains(&support),
                "engine dispatch path {support} must be declared in managed provider manifest"
            );
            assert!(
                support_dispatch_command(provider, operation).is_some(),
                "engine dispatch path {support} must map to a control command"
            );
        }
    }

    #[test]
    fn invocation_close_support_is_limited_to_work_owning_adapters() {
        let supports = manifest_machine_control_supports();
        for provider in ["claude", "codex", "omp"] {
            let support = format!("{provider}.invocation_close");
            assert!(supports.contains(&support), "missing {support}");
            assert_eq!(
                support_dispatch_command(provider, "invocation_close"),
                Some(COMMAND_INVOCATION_CLOSE)
            );
        }
        for provider in ["antigravity", "cursor", "opencode", "pi"] {
            let support = format!("{provider}.invocation_close");
            assert!(!supports.contains(&support), "unexpected {support}");
            assert_eq!(support_dispatch_command(provider, "invocation_close"), None);
        }
    }

    #[test]
    fn invocation_close_for_unknown_run_is_idempotent() {
        let temp = tempfile::tempdir().unwrap();
        let run_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        let frame = json!({
            "command_type": COMMAND_INVOCATION_CLOSE,
            "command_id": format!("{run_id}:close"),
            "session_id": session_id,
            "payload": {
                "provider": "claude",
                "run_id": run_id,
                "thread_id": thread_id,
                "reason": "user_stop",
            }
        });
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let result = temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                execute_command(&frame, &ShipperConfig::default())
                    .await
                    .unwrap()
            })
        });
        assert_eq!(result["closed"], false);
        assert!(result["invocation_id"].is_null());
        assert_eq!(result["stopped"], json!([]));
    }

    struct CloseTestInput;

    impl crate::console_lifecycle::ConsoleInput for CloseTestInput {
        fn send_input<'a>(
            &'a self,
            _text: &'a str,
            _images: &'a [PathBuf],
        ) -> crate::console_lifecycle::InputFuture<'a> {
            Box::pin(async { Ok(()) })
        }

        fn close_input(&self) -> crate::console_lifecycle::InputFuture<'_> {
            Box::pin(async { Ok(()) })
        }
    }

    #[test]
    fn invocation_close_dispatches_to_claude_and_posts_close_snapshot() {
        exercise_claude_invocation_close(false);
    }

    #[test]
    fn invocation_close_outbox_failure_still_closes_and_unregisters() {
        exercise_claude_invocation_close(true);
    }

    fn exercise_claude_invocation_close(outbox_failure: bool) {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        if outbox_failure {
            let agent_dir = temp.path().join("agent");
            std::fs::create_dir_all(&agent_dir).unwrap();
            std::fs::write(agent_dir.join("runtime-events-outbox"), "not a directory").unwrap();
        }
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                let mut child = tokio::process::Command::new("sleep")
                    .arg("30")
                    .process_group(0)
                    .kill_on_drop(true)
                    .spawn()
                    .unwrap();
                let pid = child.id().unwrap();
                let process_group_id = i32::try_from(pid).unwrap();
                let child_reaper = tokio::spawn(async move { child.wait().await.unwrap() });
                let run_id = Uuid::new_v4().to_string();
                let session_id = Uuid::new_v4().to_string();
                let thread_id = Uuid::new_v4().to_string();
                let provider_thread_id = Uuid::new_v4().to_string();
                let launch_id = Uuid::new_v4().to_string();
                let registry = default_turn_claim_registry().unwrap();
                registry
                    .claim(&run_id, &session_id, &thread_id, None, None, "claude")
                    .unwrap();
                registry
                    .mark_spawned_invocation(
                        &run_id,
                        pid,
                        process_group_id,
                        process_start_time_for_pid(Some(pid)),
                        CLAUDE_PRINT_ADAPTER,
                        &launch_id,
                        Some(&provider_thread_id),
                        "",
                        "",
                        json!({"transport": "claude_print"}),
                    )
                    .unwrap();
                registry
                    .record_invocation_turn(&run_id, "user", false)
                    .unwrap();
                let pending = crate::console_lifecycle::PendingItem {
                    id: "task-1".to_string(),
                    kind: "monitor".to_string(),
                    status: "running".to_string(),
                    description: Some("watch files".to_string()),
                };
                registry
                    .record_invocation_pending_items(&run_id, vec![pending.clone()])
                    .unwrap();
                registry
                    .mark_terminal(&run_id, "run_completed", None)
                    .unwrap();
                registry
                    .record_invocation_state(&run_id, "parked", 1)
                    .unwrap();
                let invocation = Arc::new(crate::console_lifecycle::ConsoleInvocation::new(
                    "claude",
                    provider_thread_id,
                    launch_id.clone(),
                    pid,
                    process_group_id,
                    crate::console_lifecycle::TurnBinding {
                        run_id: run_id.clone(),
                        turn_id: None,
                        client_request_id: None,
                        origin: crate::console_lifecycle::TurnOrigin::User,
                    },
                    Arc::new(CloseTestInput),
                ));
                invocation.replace_pending(vec![pending], Vec::new());
                invocation
                    .idle(crate::console_lifecycle::IdleSignal {
                        terminal_state: "run_completed".to_string(),
                        exit_code: Some(0),
                        stderr: None,
                    })
                    .unwrap();
                crate::console_lifecycle::register(invocation).unwrap();
                let frame = json!({
                    "command_type": COMMAND_INVOCATION_CLOSE,
                    "command_id": format!("{run_id}:close"),
                    "session_id": session_id,
                    "payload": {
                        "provider": "claude",
                        "run_id": run_id,
                        "thread_id": thread_id,
                        "reason": "user_stop",
                    }
                });
                assert!(command_requires_restart_fence(&frame));
                let mut config = test_config();
                config.machine_name = "close-test-machine".to_string();
                let result = execute_command(&frame, &config).await.unwrap();
                assert_eq!(result["closed"], true);
                assert_eq!(result["cleanup"], "complete");
                if outbox_failure {
                    assert!(result["error_note"]
                        .as_str()
                        .is_some_and(|message| !message.is_empty()));
                } else {
                    assert!(result.get("error_note").is_none());
                }
                assert_eq!(result["invocation_id"], launch_id);
                assert_eq!(result["stopped"][0]["id"], "task-1");
                assert_eq!(result["stopped"][0]["kind"], "monitor");
                assert_eq!(result["stopped"][0]["description"], "watch files");
                let status = child_reaper.await.unwrap();
                assert!(!status.success());
                let closed_claim = registry.read(&run_id).unwrap();
                assert_eq!(closed_claim.invocation_state.as_deref(), Some("closed"));
                assert_eq!(closed_claim.pending_count, 1);
                assert_eq!(closed_claim.pending_items[0].id, "task-1");
                assert!(crate::console_lifecycle::lookup_launch(&launch_id).is_none());
                assert!(!crate::process_group::group_is_alive(process_group_id));
                let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
                // The close event is retained in the claim either way; only a
                // successful outbox handoff marks it handed off. The daemon
                // replays a retained one until it lands.
                let retained = closed_claim
                    .invocation_close_event
                    .as_ref()
                    .expect("close event is retained for replay");
                assert_eq!(retained["kind"], "invocation_closed");
                assert_eq!(retained["payload"]["reason"], "user_stop");
                assert_eq!(
                    closed_claim.invocation_close_event_handed_off,
                    !outbox_failure
                );
                if outbox_failure {
                    assert!(std::fs::read_dir(outbox).is_err());
                } else {
                    let events = std::fs::read_dir(outbox)
                        .unwrap()
                        .flatten()
                        .filter_map(|entry| std::fs::read(entry.path()).ok())
                        .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                        .collect::<Vec<_>>();
                    let closed = events
                        .iter()
                        .find(|event| {
                            event["session_id"] == session_id
                                && event["kind"] == "invocation_closed"
                        })
                        .expect("close event is durable");
                    assert_eq!(closed["payload"]["reason"], "user_stop");
                    assert!(events.iter().any(|event| {
                        event["session_id"] == session_id
                            && event["kind"] == "delegation_signal"
                            && event["payload"]["delegation"]["count"] == 0
                    }));
                }
            });
        });
    }

    #[test]
    fn unsupported_engine_dispatch_paths_stay_unadvertised() {
        let supports = manifest_machine_control_supports();
        for (provider, operation) in [
            ("opencode", "run_once"),
            ("opencode", "resume_run_once"),
            ("opencode", "launch"),
            ("antigravity", "interrupt"),
            ("antigravity", "steer"),
            ("antigravity", "answer_pause"),
            ("antigravity", "launch"),
            ("claude", "run_once"),
            ("claude", "resume_run_once"),
            ("claude", "launch"),
            ("claude", "continue"),
            ("codex", "terminate"),
            ("codex", "launch"),
            ("codex", "continue"),
            ("antigravity", "invocation_close"),
            ("cursor", "invocation_close"),
            ("opencode", "invocation_close"),
            ("pi", "invocation_close"),
        ] {
            let support = format!("{provider}.{operation}");
            assert!(
                !supports.contains(&support),
                "manifest must not advertise unsupported dispatch path {support}"
            );
            assert_eq!(
                support_dispatch_command(provider, operation),
                None,
                "engine dispatch table must not route unsupported path {support}"
            );
        }
    }

    #[test]
    fn control_supports_are_gated_by_installed_provider_commands() {
        let unique = format!(
            "lh-control-supports-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();

        fn write_executable(dir: &std::path::Path, name: &str) {
            let path = dir.join(name);
            std::fs::write(&path, "#!/bin/sh\nexit 0\n").unwrap();
            #[cfg(unix)]
            {
                let mut perms = std::fs::metadata(&path).unwrap().permissions();
                perms.set_mode(0o755);
                std::fs::set_permissions(&path, perms).unwrap();
            }
        }

        write_executable(&dir, "opencode");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None, true);
        assert_eq!(
            supports,
            vec![
                "archive.backlog_control".to_string(),
                "archive.backlog_control.v2".to_string(),
                "opencode.send".to_string(),
                "opencode.interrupt".to_string(),
                "opencode.steer".to_string(),
                "opencode.answer_pause".to_string(),
                "opencode.terminate".to_string(),
                "opencode.turn_start".to_string(),
                "opencode.turn_interrupt".to_string(),
                "opencode.turn_steer".to_string(),
            ]
        );

        write_executable(&dir, "longhouse");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None, true);
        assert_eq!(
            supports,
            vec![
                "archive.backlog_control".to_string(),
                "archive.backlog_control.v2".to_string(),
                "opencode.send".to_string(),
                "opencode.interrupt".to_string(),
                "opencode.steer".to_string(),
                "opencode.answer_pause".to_string(),
                "opencode.terminate".to_string(),
                "opencode.turn_start".to_string(),
                "opencode.turn_interrupt".to_string(),
                "opencode.turn_steer".to_string(),
            ]
        );

        write_executable(&dir, "custom-codex");
        let supports = control_supports_for_path_with_env(
            Some(dir.as_os_str()),
            &|name| {
                if name == "LONGHOUSE_CODEX_BIN" {
                    Some(dir.join("custom-codex").into_os_string())
                } else {
                    None
                }
            },
            true,
        );
        assert!(!supports.contains(&"codex.launch".to_string()));
        assert!(!supports.contains(&"codex.continue".to_string()));
        assert!(supports.contains(&"codex.run_once".to_string()));
        assert!(supports.contains(&"codex.resume_run_once".to_string()));
        assert!(supports.contains(&"codex.turn_start".to_string()));

        // Capability discovery and Console dispatch resolve the same exact
        // staged Claude executable even though no binary named `claude`
        // exists on PATH.
        write_executable(&dir, "custom-claude");
        let staged_claude = dir.join("custom-claude").into_os_string();
        let env_lookup =
            |name: &str| (name == "LONGHOUSE_CLAUDE_BIN").then(|| staged_claude.clone());
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &env_lookup, true);
        assert!(supports.contains(&"claude.turn_start".to_string()));
        assert_eq!(
            console_provider_binary_with_env("claude", &env_lookup),
            dir.join("custom-claude").to_string_lossy()
        );

        write_executable(&dir, "codex");
        write_executable(&dir, "claude");
        write_executable(&dir, "agy");
        write_executable(&dir, "cursor-agent");
        write_executable(&dir, "pi");
        write_executable(&dir, "omp");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None, true);
        let mut expected = vec![
            "archive.backlog_control".to_string(),
            "archive.backlog_control.v2".to_string(),
        ];
        for contract in managed_provider_contract_items() {
            expected.extend(
                contract
                    .get("machine_control_supports")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_string),
            );
            let provider = contract.get("provider").and_then(Value::as_str).unwrap();
            if crate::sign_in::declared_sign_in(contract).is_some() {
                expected.push(format!("{provider}.sign_in"));
            }
        }
        assert_eq!(supports, expected);
        assert!(supports.contains(&"claude.sign_in".to_string()));
        assert!(supports.contains(&"codex.sign_in".to_string()));
        assert!(!supports.contains(&"codex.launch".to_string()));
        assert!(!supports.contains(&"codex.continue".to_string()));
        assert!(supports.contains(&"codex.run_once".to_string()));
        assert!(supports.contains(&"codex.resume_run_once".to_string()));
        assert!(supports.contains(&"codex.turn_start".to_string()));
        assert!(!supports.contains(&"claude.launch".to_string()));
        assert!(!supports.contains(&"claude.continue".to_string()));
        assert!(supports.contains(&"claude.send".to_string()));
        assert!(supports.contains(&"claude.turn_start".to_string()));

        let not_ready = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None, false);
        assert!(not_ready.contains(&"claude.send".to_string()));
        assert!(not_ready.contains(&"claude.turn_interrupt".to_string()));
        assert!(!not_ready.contains(&"claude.turn_start".to_string()));
        assert!(!supports.contains(&"opencode.launch".to_string()));
        assert!(supports.contains(&"opencode.terminate".to_string()));
        assert!(supports.contains(&"opencode.turn_start".to_string()));
        assert!(supports.contains(&"antigravity.send".to_string()));
        assert!(!supports.contains(&"antigravity.interrupt".to_string()));
        assert!(!supports.contains(&"antigravity.steer".to_string()));
        assert!(!supports.contains(&"antigravity.launch".to_string()));

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_missing_command_id() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "session_id": "session-1",
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"text": "continue"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "invalid_command");
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_unsupported_command_type() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-1",
                "session_id": "session-1",
                "command_type": "session.unknown",
                "payload": {},
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["command_id"], "cmd-1");
        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "unsupported_command");
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_missing_attached_session() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-missing-session",
                "session_id": "definitely-missing-control-channel-session",
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"text": "continue"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["command_id"], "cmd-missing-session");
        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "session_not_attached");
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_turn_interrupt_without_exact_identity() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-malformed-turn-interrupt",
                "session_id": "session-1",
                "command_type": COMMAND_TURN_INTERRUPT,
                "payload": {
                    "provider": "cursor",
                    "run_id": "run-1",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["command_id"], "cmd-malformed-turn-interrupt");
        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "invalid_command");
        assert!(result["error"]["message"]
            .as_str()
            .unwrap()
            .contains("payload.turn_id"));
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_malformed_codex_attachments_before_dispatch() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-bad-attachments",
                "session_id": "definitely-missing-control-channel-session",
                "command_type": COMMAND_SEND_TEXT,
                "payload": {
                    "provider": "codex",
                    "text": "continue",
                    "attachments": [{"id": "not-a-uuid"}],
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["command_id"], "cmd-bad-attachments");
        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "command_failed");
        assert!(result["error"]["message"]
            .as_str()
            .unwrap()
            .contains("attachments[0] is not a valid AttachmentRef"));
    }

    #[tokio::test]
    async fn handle_command_frame_returns_cached_result_for_duplicate_command_id() {
        let mut cache = command_cache();
        let first = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-duplicate",
                "session_id": "session-1",
                "command_type": "session.unknown",
                "payload": {},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        let second = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-duplicate",
                "session_id": "definitely-missing-control-channel-session",
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"text": "continue"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(first, second);
        assert_eq!(second["error"]["code"], "unsupported_command");
    }

    #[tokio::test]
    async fn handle_command_frame_routes_claude_control_natively() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let (port, mut rx) = spawn_claude_inject_server().await;
        let session_id = "11111111-1111-4111-8111-111111111111";
        write_claude_channel_state(temp.path(), session_id, port);

        let old_home = std::env::var_os("HOME");
        std::env::set_var("HOME", temp.path().as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-claude-send",
                "session_id": session_id,
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"provider": "claude", "text": "hello"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "claude");
        assert_eq!(result["result"]["transport"], "claude_channel_bridge");
        let request = rx.recv().await.unwrap();
        let body: Value = serde_json::from_str(&request.body).unwrap();
        assert_eq!(request.target, "/inject");
        assert_eq!(body["content"], "hello");
        assert_eq!(body["meta"]["injected_by"], "longhouse");
        assert_eq!(body["meta"]["longhouse_session_id"], session_id);

        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-claude-steer",
                "session_id": session_id,
                "command_type": COMMAND_STEER_TEXT,
                "payload": {"provider": "claude", "text": "course correct"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        assert_eq!(result["ok"], true);
        let request = rx.recv().await.unwrap();
        let body: Value = serde_json::from_str(&request.body).unwrap();
        assert_eq!(body["content"], "course correct");
        assert_eq!(body["meta"]["intent"], "steer");

        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-claude-answer-pause",
                "session_id": session_id,
                "command_type": COMMAND_ANSWER_PAUSE,
                "payload": {
                    "provider": "claude",
                    "request_key": "pause-key",
                    "message": "Use the smaller plan",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["pause_response"]["status"], "resolved");
        let request = rx.recv().await.unwrap();
        let body: Value = serde_json::from_str(&request.body).unwrap();
        assert_eq!(body["content"], "Use the smaller plan");
        assert_eq!(body["meta"]["intent"], "pause_response");
        assert_eq!(body["meta"]["request_key"], "pause-key");
        assert_eq!(body["meta"]["decision"], "answer");

        if let Some(value) = old_home {
            std::env::set_var("HOME", value);
        } else {
            std::env::remove_var("HOME");
        }
    }

    #[tokio::test]
    async fn handle_command_frame_routes_antigravity_send_through_the_hook_inbox() {
        // Antigravity Helm send no longer subprocesses to the (nonexistent)
        // `longhouse antigravity-channel send` CLI. The engine writes the
        // hook-inbox message file directly (see antigravity_channel_control.rs)
        // and waits for the shipped hook to claim it. This test plays the
        // hook's part: it watches for the queued message and drops a claim
        // receipt, then asserts the command surfaces that claim.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let old_home = std::env::var_os("LONGHOUSE_HOME");
        std::env::set_var("LONGHOUSE_HOME", temp.path());

        let session_id = "session-1";
        let inbox_dir = temp
            .path()
            .join("managed-local")
            .join("antigravity")
            .join("inbox")
            .join(session_id);

        let claimer = tokio::spawn({
            let inbox_dir = inbox_dir.clone();
            async move {
                let message_path = loop {
                    if let Ok(entries) = std::fs::read_dir(&inbox_dir) {
                        if let Some(entry) = entries
                            .filter_map(|entry| entry.ok())
                            .find(|entry| entry.file_name().to_string_lossy().starts_with("msg-"))
                        {
                            break entry.path();
                        }
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(10)).await;
                };
                let message_id = message_path
                    .file_stem()
                    .unwrap()
                    .to_string_lossy()
                    .strip_prefix("msg-")
                    .unwrap()
                    .to_string();
                let claimed_dir = inbox_dir.join("claimed");
                std::fs::create_dir_all(&claimed_dir).unwrap();
                std::fs::write(
                    claimed_dir.join(format!("claimed-msg-{message_id}.json")),
                    serde_json::to_vec(&json!({
                        "id": message_id,
                        "claimed_at": "2026-01-01T00:00:00.000000Z",
                    }))
                    .unwrap(),
                )
                .unwrap();
            }
        });

        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-antigravity-send",
                "session_id": session_id,
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"provider": "antigravity", "text": "hello"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        claimer.await.unwrap();

        if let Some(value) = old_home {
            std::env::set_var("LONGHOUSE_HOME", value);
        } else {
            std::env::remove_var("LONGHOUSE_HOME");
        }

        assert_eq!(result["ok"], true, "{result}");
        assert_eq!(result["result"]["transport"], "antigravity_hook_inbox");
        assert_eq!(
            result["result"]["claimed_at"], "2026-01-01T00:00:00.000000Z",
            "{result}"
        );
    }

    #[tokio::test]
    async fn handle_command_frame_routes_opencode_send_through_native_control() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::TempDir::new().unwrap();
        let empty_path = temp.path().join("empty-path");
        let config_dir = temp.path().join("claude-config");
        std::fs::create_dir_all(&empty_path).unwrap();
        let (server_url, request_rx) = spawn_single_http_request_server().await;
        let session_id = "11111111-1111-4111-8111-111111111111";
        write_opencode_control_state(&config_dir, session_id, &server_url);

        let old_path = std::env::var_os("PATH");
        let old_claude_config_dir = std::env::var_os("CLAUDE_CONFIG_DIR");
        std::env::set_var("PATH", empty_path.as_os_str());
        std::env::set_var("CLAUDE_CONFIG_DIR", config_dir.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-opencode-native-send",
                "session_id": session_id,
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"provider": "opencode", "text": "hello native"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        if let Some(value) = old_path {
            std::env::set_var("PATH", value);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(value) = old_claude_config_dir {
            std::env::set_var("CLAUDE_CONFIG_DIR", value);
        } else {
            std::env::remove_var("CLAUDE_CONFIG_DIR");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "opencode");
        assert_eq!(result["result"]["transport"], "opencode_server_bridge");
        assert_eq!(result["result"]["provider_session_id"], "ses_native");
        let request = request_rx.await.unwrap();
        assert_eq!(
            request.target,
            "/session/ses_native/prompt_async?directory=%2Ftmp%2Fnative+opencode"
        );
        assert_eq!(
            serde_json::from_str::<Value>(&request.body).unwrap(),
            json!({
                "parts": [{"type": "text", "text": "hello native"}],
            })
        );
    }

    #[tokio::test]
    async fn handle_command_frame_routes_opencode_steer_through_native_control() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::TempDir::new().unwrap();
        let empty_path = temp.path().join("empty-path");
        let config_dir = temp.path().join("claude-config");
        std::fs::create_dir_all(&empty_path).unwrap();
        let (server_url, request_rx) = spawn_single_http_request_server().await;
        let session_id = "11111111-1111-4111-8111-111111111111";
        write_opencode_control_state(&config_dir, session_id, &server_url);

        let old_path = std::env::var_os("PATH");
        let old_claude_config_dir = std::env::var_os("CLAUDE_CONFIG_DIR");
        std::env::set_var("PATH", empty_path.as_os_str());
        std::env::set_var("CLAUDE_CONFIG_DIR", config_dir.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-opencode-native-steer",
                "session_id": session_id,
                "command_type": COMMAND_STEER_TEXT,
                "payload": {"provider": "opencode", "text": "change course"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        if let Some(value) = old_path {
            std::env::set_var("PATH", value);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(value) = old_claude_config_dir {
            std::env::set_var("CLAUDE_CONFIG_DIR", value);
        } else {
            std::env::remove_var("CLAUDE_CONFIG_DIR");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "opencode");
        assert_eq!(result["result"]["transport"], "opencode_server_bridge");
        assert_eq!(result["result"]["provider_session_id"], "ses_native");
        let request = request_rx.await.unwrap();
        assert_eq!(
            request.target,
            "/session/ses_native/prompt_async?directory=%2Ftmp%2Fnative+opencode"
        );
        assert_eq!(
            serde_json::from_str::<Value>(&request.body).unwrap(),
            json!({
                "parts": [{"type": "text", "text": "change course"}],
            })
        );
    }

    #[tokio::test]
    async fn handle_command_frame_routes_opencode_interrupt_through_native_control() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::TempDir::new().unwrap();
        let empty_path = temp.path().join("empty-path");
        let config_dir = temp.path().join("claude-config");
        std::fs::create_dir_all(&empty_path).unwrap();
        let (server_url, request_rx) = spawn_single_http_request_server().await;
        let session_id = "11111111-1111-4111-8111-111111111111";
        write_opencode_control_state(&config_dir, session_id, &server_url);

        let old_path = std::env::var_os("PATH");
        let old_claude_config_dir = std::env::var_os("CLAUDE_CONFIG_DIR");
        std::env::set_var("PATH", empty_path.as_os_str());
        std::env::set_var("CLAUDE_CONFIG_DIR", config_dir.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-opencode-native-interrupt",
                "session_id": session_id,
                "command_type": COMMAND_INTERRUPT,
                "payload": {"provider": "opencode"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        if let Some(value) = old_path {
            std::env::set_var("PATH", value);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(value) = old_claude_config_dir {
            std::env::set_var("CLAUDE_CONFIG_DIR", value);
        } else {
            std::env::remove_var("CLAUDE_CONFIG_DIR");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "opencode");
        assert_eq!(result["result"]["transport"], "opencode_server_bridge");
        assert_eq!(result["result"]["provider_session_id"], "ses_native");
        let request = request_rx.await.unwrap();
        assert_eq!(
            request.target,
            "/session/ses_native/abort?directory=%2Ftmp%2Fnative+opencode"
        );
        assert!(request.body.is_empty());
    }

    #[tokio::test]
    async fn handle_command_frame_routes_opencode_terminate_through_native_control() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::TempDir::new().unwrap();
        let empty_path = temp.path().join("empty-path");
        let config_dir = temp.path().join("claude-config");
        std::fs::create_dir_all(&empty_path).unwrap();
        let session_id = "11111111-1111-4111-8111-111111111111";
        write_opencode_control_state(&config_dir, session_id, "http://127.0.0.1:12345");

        let old_path = std::env::var_os("PATH");
        let old_claude_config_dir = std::env::var_os("CLAUDE_CONFIG_DIR");
        std::env::set_var("PATH", empty_path.as_os_str());
        std::env::set_var("CLAUDE_CONFIG_DIR", config_dir.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-opencode-native-terminate",
                "session_id": session_id,
                "command_type": COMMAND_TERMINATE,
                "payload": {"provider": "opencode"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
        if let Some(value) = old_path {
            std::env::set_var("PATH", value);
        } else {
            std::env::remove_var("PATH");
        }
        if let Some(value) = old_claude_config_dir {
            std::env::set_var("CLAUDE_CONFIG_DIR", value);
        } else {
            std::env::remove_var("CLAUDE_CONFIG_DIR");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "opencode");
        assert_eq!(result["result"]["transport"], "opencode_server_bridge");
        assert_eq!(result["result"]["pid"], Value::Null);
        assert_eq!(result["result"]["stopped"], false);
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_non_opencode_terminate() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-codex-terminate",
                "session_id": "11111111-1111-4111-8111-111111111111",
                "command_type": COMMAND_TERMINATE,
                "payload": {"provider": "codex"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "unsupported_command");
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_unproven_provider_steer_paths() {
        let mut cache = command_cache();
        let antigravity = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-antigravity-steer",
                "session_id": "session-1",
                "command_type": COMMAND_STEER_TEXT,
                "payload": {"provider": "antigravity", "text": "change course"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(antigravity["ok"], false);
        // Refused for lacking an active-turn steer path, not for being
        // Antigravity. The hook inbox delivers a turn; it cannot redirect one.
        assert_eq!(antigravity["error"]["code"], "unsupported_command");
    }

    #[test]
    fn completed_command_cache_evicts_oldest_result() {
        let mut cache = CompletedCommandCache::new(1, Duration::from_secs(60));
        cache.insert("cmd-1".to_string(), json!({"command_id": "cmd-1"}));
        cache.insert("cmd-2".to_string(), json!({"command_id": "cmd-2"}));

        assert_eq!(cache.get("cmd-1"), None);
        assert_eq!(cache.get("cmd-2").unwrap()["command_id"], "cmd-2");
    }

    #[tokio::test]
    async fn restart_fence_returns_indeterminate_instead_of_replaying_accepted_control() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("longhouse-shipper.db");
        let command_id = "managed-control:session-1:session.send_text:req-1";
        let frame = json!({
            "type": "command",
            "command_id": command_id,
            "session_id": "session-1",
            "command_type": COMMAND_SEND_TEXT,
            "payload": {
                "provider": "codex",
                "text": "continue",
                "longhouse_control_grant": {
                    "lease_generation": "lease-generation-1",
                    "run_id": "run-1",
                    "connection_id": "connection-1"
                }
            }
        });
        let identity = command_receipt_identity(&frame, command_id);
        let store = Arc::new(DurableCommandReceiptStore::for_db_path(&db_path));
        assert!(matches!(
            store.claim(&identity).unwrap(),
            DurableCommandReceiptOutcome::Claimed
        ));
        drop(store);

        // Reopening the store models an engine restart. The accepted boundary
        // is durable even though no provider side effect is run by this test.
        let restarted_store = Arc::new(DurableCommandReceiptStore::for_db_path(&db_path));
        let mut cache = command_cache().with_durable_receipts(Some(restarted_store));
        let result = handle_command_frame(frame, &mut cache, &test_config()).await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "command_indeterminate");
    }

    #[tokio::test]
    async fn restart_fence_covers_accepted_console_turn_interrupt() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("longhouse-shipper.db");
        let command_id = "run-1:interrupt";
        let frame = json!({
            "type": "command",
            "command_id": command_id,
            "session_id": "session-1",
            "command_type": COMMAND_TURN_INTERRUPT,
            "payload": {
                "provider": "claude",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "thread_id": "thread-1"
            }
        });
        let identity = command_receipt_identity(&frame, command_id);
        let store = Arc::new(DurableCommandReceiptStore::for_db_path(&db_path));
        assert!(matches!(
            store.claim(&identity).unwrap(),
            DurableCommandReceiptOutcome::Claimed
        ));
        drop(store);

        // Console turn interrupts reuse run_id:interrupt after a reconnect. The
        // durable acceptance boundary must win over a replay after restart.
        let restarted_store = Arc::new(DurableCommandReceiptStore::for_db_path(&db_path));
        let mut cache = command_cache().with_durable_receipts(Some(restarted_store));
        let result = handle_command_frame(frame, &mut cache, &test_config()).await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "command_indeterminate");
    }

    #[tokio::test]
    async fn run_once_rejects_missing_initial_prompt() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-run-once-no-prompt",
                "session_id": "00000000-0000-0000-0000-000000000101",
                "command_type": COMMAND_RUN_ONCE,
                "payload": {
                    "provider": "codex",
                    "cwd": "/tmp",
                    "run_id": "00000000-0000-0000-0000-000000000201",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "invalid_command");
        assert!(result["error"]["message"]
            .as_str()
            .unwrap()
            .contains("initial_prompt"));
    }

    #[tokio::test]
    async fn run_once_rejects_unsupported_provider() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-run-once-provider",
                "session_id": "00000000-0000-0000-0000-000000000102",
                "command_type": COMMAND_RUN_ONCE,
                "payload": {
                    "provider": "claude",
                    "cwd": "/tmp",
                    "run_id": "00000000-0000-0000-0000-000000000202",
                    "initial_prompt": "do it",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "provider_unsupported");
    }

    #[tokio::test]
    async fn run_once_resume_rejects_missing_thread_id() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-run-once-resume-no-thread",
                "session_id": "00000000-0000-0000-0000-000000000103",
                "command_type": COMMAND_RUN_ONCE,
                "payload": {
                    "provider": "codex",
                    "cwd": "/tmp",
                    "run_id": "00000000-0000-0000-0000-000000000203",
                    "initial_prompt": "continue it",
                    "mode": "continue",
                    "resume": {
                        "thread_path": "/tmp/thread.jsonl"
                    }
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "invalid_command");
        assert!(result["error"]["message"]
            .as_str()
            .unwrap()
            .contains("thread_id"));
    }

    #[tokio::test]
    async fn session_launch_is_unsupported_command() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-launch-removed",
                "session_id": "00000000-0000-0000-0000-000000000001",
                "command_type": COMMAND_LAUNCH,
                "payload": {
                    "provider": "codex",
                    "cwd": "/tmp",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "unsupported_command");
        assert!(result["error"]["message"]
            .as_str()
            .unwrap()
            .contains(COMMAND_LAUNCH));
    }

    #[test]
    fn opencode_console_turn_start_runs_an_owned_server_and_resumes_the_native_session() {
        let _guard = crate::console_adapter::agent_state_guard();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let temp = tempfile::TempDir::new().unwrap();
        let workspace = temp.path().join("workspace");
        std::fs::create_dir(&workspace).unwrap();
        let fake = temp.path().join("opencode");
        write_test_executable(&fake, crate::opencode_run::fake_server::SCRIPT);
        let requests = temp.path().join("requests.log");
        let longhouse_home = temp.path().join("longhouse");
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let native = crate::opencode_run::fake_server::SESSION;

        let vars = vec![
            (
                "LONGHOUSE_HOME".to_string(),
                Some(longhouse_home.display().to_string()),
            ),
            (
                "LONGHOUSE_OPENCODE_BIN".to_string(),
                Some(fake.display().to_string()),
            ),
            (
                "OPENCODE_FAKE_LOG".to_string(),
                Some(requests.display().to_string()),
            ),
        ];
        temp_env::with_vars(vars, || {
            let run_turn = |resume: Option<&str>| {
                let run_id = Uuid::new_v4().to_string();
                let mut payload = json!({
                    "provider": "opencode",
                    "thread_id": thread_id,
                    "turn_id": Uuid::new_v4().to_string(),
                    "run_id": run_id,
                    "client_request_id": format!("request-{run_id}"),
                    "cwd": workspace,
                    "message": "reply once",
                    "permission_mode": "bypass",
                });
                if let Some(provider_thread_id) = resume {
                    payload["resume_provider_thread_id"] = json!(provider_thread_id);
                }
                let mut cache = command_cache();
                let response = runtime.block_on(handle_command_frame(
                    json!({
                        "type": "command",
                        "command_id": run_id,
                        "session_id": session_id,
                        "command_type": COMMAND_TURN_START,
                        "payload": payload,
                    }),
                    &mut cache,
                    &test_config(),
                ));
                assert_eq!(response["ok"], true, "{response}");
                // A server the engine owns, never a `run` and never a session flag.
                let argv = response["result"]["argv"].as_array().unwrap();
                assert!(argv.iter().any(|value| value == "serve"));
                assert!(!argv.iter().any(|value| {
                    matches!(
                        value.as_str(),
                        Some("run" | "--auto" | "--session" | "--attach" | "--continue")
                    )
                }));
                let deadline = std::time::Instant::now() + Duration::from_secs(20);
                loop {
                    let claim = crate::turn_claims::default_registry()
                        .unwrap()
                        .read(&run_id)
                        .unwrap();
                    if claim.state == "terminal" {
                        assert_eq!(claim.provider_thread_id.as_deref(), Some(native));
                        assert_eq!(claim.result.unwrap()["terminal_state"], "run_completed");
                        break;
                    }
                    assert!(
                        std::time::Instant::now() < deadline,
                        "OpenCode fake turn timed out"
                    );
                    runtime.block_on(async { tokio::time::sleep(Duration::from_millis(20)).await });
                }
                response
            };

            let created = |log: &Path| {
                std::fs::read_to_string(log)
                    .unwrap_or_default()
                    .lines()
                    .filter(|line| *line == "POST /session")
                    .count()
            };
            run_turn(None);
            assert_eq!(created(&requests), 1);
            run_turn(Some(native));
            // The resume turn looked the session up and did not create another.
            assert_eq!(created(&requests), 1);
            assert!(std::fs::read_to_string(&requests)
                .unwrap()
                .lines()
                .any(|line| line == format!("GET /session/{native}")));
        });
    }

    #[test]
    fn antigravity_console_claim_survives_dispatch_and_recovers_its_native_source() {
        let _guard_runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let home = temp.path().join("home");
        let longhouse_home = temp.path().join("longhouse");
        let native_id = Uuid::new_v4().to_string();
        let session_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        let transcript = home
            .join(".gemini/antigravity-cli/brain")
            .join(&native_id)
            .join(".system_generated/logs/transcript_full.jsonl");
        std::fs::create_dir_all(transcript.parent().unwrap()).unwrap();
        let fake = temp.path().join("agy");
        write_test_executable(
            &fake,
            &format!(
                r#"#!/bin/sh
set -eu
previous=""
for value in "$@"; do
  if [ "$previous" = "--output-format" ]; then format="$value"; fi
  previous="$value"
done
test "$format" = "stream-json"
printf '%s\n' '{{"event":"init","conversation_id":"{native_id}","init":{{"cwd":"/tmp"}}}}'
printf '%s\n' '{{"step_index":0,"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","created_at":"2026-09-06T22:07:35Z","content":"hello"}}' > '{}'
printf '%s\n' '{{"event":"result","result":{{"conversation_id":"{native_id}","status":"SUCCESS","response":"done"}}}}'
"#,
                transcript.display()
            ),
        );
        let db_path = temp.path().join("state.db");
        crate::state::db::open_db(Some(&db_path)).unwrap();
        let mut config = test_config();
        config.db_path = Some(db_path.clone());
        temp_env::with_vars(
            [
                ("HOME", Some(home.as_os_str())),
                ("LONGHOUSE_HOME", Some(longhouse_home.as_os_str())),
                ("LONGHOUSE_ANTIGRAVITY_BIN", Some(fake.as_os_str())),
            ],
            || {
                // A current-thread runtime returns from dispatch before the spawned
                // monitor is polled. Dropping it models losing the engine monitor,
                // while the provider's structured stdout survives.
                let runtime = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                let mut cache = command_cache();
                let response = runtime.block_on(handle_command_frame(
                    json!({
                        "type": "command",
                        "command_id": run_id,
                        "session_id": session_id,
                        "command_type": COMMAND_TURN_START,
                        "payload": {
                            "provider": "antigravity",
                            "thread_id": Uuid::new_v4().to_string(),
                            "run_id": run_id,
                            "cwd": temp.path(),
                            "message": "reply once",
                            "permission_mode": "bypass"
                        }
                    }),
                    &mut cache,
                    &config,
                ));
                assert_eq!(response["ok"], true, "{response}");
                let registry = crate::turn_claims::default_registry().unwrap();
                let claim = registry.read(&run_id).unwrap();
                assert_eq!(claim.adapter.as_deref(), Some(ANTIGRAVITY_PRINT_ADAPTER));
                let pid = claim.pid.unwrap() as i32;
                let mut status = 0;
                assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
                assert!(libc::WIFEXITED(status));
                assert_eq!(libc::WEXITSTATUS(status), 0);
                drop(runtime);
                assert_eq!(registry.read(&run_id).unwrap().state, "spawned");

                let recovered = tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                    .unwrap();
                // Older dispatchers mislabeled this exact provider invocation as
                // codex_exec. Codex recovery must not settle another provider, and
                // Antigravity must recognize its recorded transport on recovery.
                registry
                    .mark_spawned(
                        &run_id,
                        claim.pid,
                        claim.process_group_id,
                        claim.process_start_time,
                        "codex_exec",
                        claim.result.unwrap(),
                    )
                    .unwrap();
                recovered
                    .block_on(crate::codex_exec::recover_codex_exec_turns(
                        "test-machine",
                        Some(db_path.clone()),
                    ))
                    .unwrap();
                assert_eq!(registry.read(&run_id).unwrap().state, "spawned");
                recovered
                    .block_on(crate::antigravity_print::recover_antigravity_print_turns(
                        "test-machine",
                        Some(db_path.clone()),
                    ))
                    .unwrap();
                let settled = registry.read(&run_id).unwrap();
                assert_eq!(settled.state, "terminal");
                assert_eq!(settled.result.unwrap()["terminal_state"], "run_completed");
                assert_eq!(
                    settled.provider_thread_id.as_deref(),
                    Some(native_id.as_str())
                );
                // The launcher's durable authority is the claim file; the
                // database binding is the daemon's projection of it. Assert the
                // end state discovery actually reads, through the real
                // projection, rather than a binding written by the launcher.
                let claim = crate::managed_source_claim::read_claim(&session_id)
                    .unwrap()
                    .expect("recovery confirms the source claim");
                assert_eq!(claim.native_session_id.as_deref(), Some(native_id.as_str()));
                crate::managed_source_claim::project_claims(&db_path).unwrap();
                let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
                assert_eq!(
                    crate::state::session_binding::SessionBinding::new(&conn)
                        .get_for_provider(
                            &std::fs::canonicalize(&transcript)
                                .unwrap()
                                .to_string_lossy(),
                            "antigravity"
                        )
                        .unwrap(),
                    Some(session_id.clone()),
                );
            },
        );
    }

    #[test]
    fn claude_console_turn_start_is_bounded_bound_and_natively_resumable() {
        let _guard = crate::console_adapter::agent_state_guard();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let temp = tempfile::TempDir::new().unwrap();
        let workspace = temp.path().join("workspace");
        std::fs::create_dir(&workspace).unwrap();
        let args_path = temp.path().join("claude-args.txt");
        let env_path = temp.path().join("claude-env.txt");
        let prompt_path = temp.path().join("claude-prompt.json");
        let fake = temp.path().join("claude");
        write_test_executable(
            &fake,
            &format!(
                r#"#!/bin/sh
printf '%s\n' "$@" > '{}'
printf '%s|%s|%s|%s\n' "$LONGHOUSE_MANAGED_SESSION_ID" "$LONGHOUSE_RUN_ID" "$LONGHOUSE_CHANNEL_SESSION_ID" "$LONGHOUSE_PERMISSION_HOOK_ENABLED" > '{}'
IFS= read -r input
printf '%s\n' "$input" > '{}'
provider_id=""
previous=""
for value in "$@"; do
  if [ "$previous" = "--session-id" ] || [ "$previous" = "--resume" ]; then provider_id="$value"; fi
  previous="$value"
done
printf '{{"type":"system","subtype":"init","session_id":"%s"}}\n' "$provider_id"
printf '%s\n' "$input" | sed 's/^{{/{{"isReplay":true,/'
case "$input" in *'sleep prompt'*) sleep 30;; esac
printf '{{"type":"assistant","message":{{"content":[{{"type":"text","text":"done"}}]}}}}\n'
printf '{{"type":"result","subtype":"success","is_error":false}}\n'
"#,
                args_path.display(),
                env_path.display(),
                prompt_path.display(),
            ),
        );
        let claude_home = temp.path().join("claude-home");
        let hook_dir = claude_home.join("hooks");
        std::fs::create_dir_all(&hook_dir).unwrap();
        std::fs::write(hook_dir.join("longhouse-hook.sh"), "#!/bin/sh\n").unwrap();
        std::fs::write(
            claude_home.join("settings.json"),
            serde_json::to_vec(&json!({
                "hooks": {"SessionStart": [{"hooks": [{"command": hook_dir.join("longhouse-hook.sh")}]}]}
            }))
            .unwrap(),
        )
        .unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let vars = vec![
            (
                "LONGHOUSE_HOME".to_string(),
                Some(longhouse_home.display().to_string()),
            ),
            (
                "CLAUDE_CONFIG_DIR".to_string(),
                Some(claude_home.display().to_string()),
            ),
            (
                "LONGHOUSE_CLAUDE_BIN".to_string(),
                Some(fake.display().to_string()),
            ),
            (
                "LONGHOUSE_CHANNEL_SESSION_ID".to_string(),
                Some("must-clear".to_string()),
            ),
            (
                "LONGHOUSE_PERMISSION_HOOK_ENABLED".to_string(),
                Some("1".to_string()),
            ),
        ];
        temp_env::with_vars(vars, || {
            let run_turn = |resume: Option<&str>| {
                let run_id = Uuid::new_v4().to_string();
                let mut payload = json!({
                    "provider": "claude",
                    "thread_id": thread_id,
                    "turn_id": Uuid::new_v4().to_string(),
                    "run_id": run_id,
                    "client_request_id": format!("request-{run_id}"),
                    "cwd": workspace,
                    "message": "private prompt",
                    "permission_mode": "bypass",
                });
                if let Some(provider_thread_id) = resume {
                    payload["resume_provider_thread_id"] = json!(provider_thread_id);
                }
                let mut cache = command_cache();
                let response = runtime.block_on(handle_command_frame(
                    json!({
                        "type": "command",
                        "command_id": run_id,
                        "session_id": session_id,
                        "command_type": COMMAND_TURN_START,
                        "payload": payload,
                    }),
                    &mut cache,
                    &test_config(),
                ));
                assert_eq!(response["ok"], true, "{response}");
                let argv = response["result"]["argv"].as_array().unwrap();
                assert!(argv.iter().any(|value| value == "--input-format"));
                assert!(argv.iter().any(|value| value == "stream-json"));
                assert!(argv.iter().any(|value| value == "--replay-user-messages"));
                assert!(!argv.iter().any(|value| value == "private prompt"));
                let deadline = std::time::Instant::now() + Duration::from_secs(30);
                loop {
                    let claim = crate::turn_claims::default_registry()
                        .unwrap()
                        .read(&run_id)
                        .unwrap();
                    if claim.state == "terminal" {
                        assert!(claim.provider_identity_confirmed);
                        assert_eq!(claim.result.unwrap()["terminal_state"], "run_completed");
                        break;
                    }
                    assert!(
                        std::time::Instant::now() < deadline,
                        "Claude fake turn timed out"
                    );
                    runtime.block_on(async { tokio::time::sleep(Duration::from_millis(20)).await });
                }
                let submitted: Value =
                    serde_json::from_str(&std::fs::read_to_string(&prompt_path).unwrap()).unwrap();
                assert_eq!(
                    submitted,
                    json!({
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "private prompt"}]
                        }
                    })
                );
                assert_eq!(
                    std::fs::read_to_string(&env_path).unwrap().trim(),
                    format!("{session_id}|{run_id}||")
                );
                response
            };

            let first = run_turn(None);
            let provider_thread_id = first["result"]["provider_thread_id"]
                .as_str()
                .unwrap()
                .to_string();
            let first_args = std::fs::read_to_string(&args_path).unwrap();
            assert!(first_args.contains("--session-id"));
            assert!(first_args.contains(&provider_thread_id));
            assert!(!first_args.contains("private prompt"));

            let second = run_turn(Some(&provider_thread_id));
            assert_eq!(second["result"]["provider_thread_id"], provider_thread_id);
            let second_args = std::fs::read_to_string(&args_path).unwrap();
            assert!(second_args.contains("--resume"));
            assert!(second_args.contains(&provider_thread_id));

            let interrupt_run_id = Uuid::new_v4().to_string();
            let interrupt_turn_id = Uuid::new_v4().to_string();
            let mut cache = command_cache();
            let started = runtime.block_on(handle_command_frame(
                json!({
                    "type": "command",
                    "command_id": interrupt_run_id,
                    "session_id": session_id,
                    "command_type": COMMAND_TURN_START,
                    "payload": {
                        "provider": "claude",
                        "thread_id": thread_id,
                        "turn_id": interrupt_turn_id,
                        "run_id": interrupt_run_id,
                        "client_request_id": "claude-interrupt",
                        "cwd": workspace,
                        "message": "sleep prompt",
                        "permission_mode": "bypass",
                        "resume_provider_thread_id": provider_thread_id,
                    },
                }),
                &mut cache,
                &test_config(),
            ));
            assert_eq!(started["ok"], true, "{started}");
            let interrupt_deadline = std::time::Instant::now() + Duration::from_secs(30);
            loop {
                let claim = crate::turn_claims::default_registry()
                    .unwrap()
                    .read(&interrupt_run_id)
                    .unwrap();
                if claim.provider_identity_confirmed {
                    break;
                }
                assert!(std::time::Instant::now() < interrupt_deadline);
                runtime.block_on(async { tokio::time::sleep(Duration::from_millis(20)).await });
            }
            crate::claude_print::interrupt_claude_print_turn(
                &interrupt_run_id,
                &session_id,
                &thread_id,
                &interrupt_turn_id,
            )
            .unwrap();
            let cancel_deadline = std::time::Instant::now() + Duration::from_secs(30);
            loop {
                let claim = crate::turn_claims::default_registry()
                    .unwrap()
                    .read(&interrupt_run_id)
                    .unwrap();
                if claim.state == "terminal" {
                    assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
                    break;
                }
                assert!(std::time::Instant::now() < cancel_deadline);
                runtime.block_on(async { tokio::time::sleep(Duration::from_millis(20)).await });
            }

            // A wake binds a run to a response Claude already started; it
            // carries no input, so an empty message must not be rejected
            // (2026-10-07: every wake failed "payload.message is required").
            let wake_run_id = Uuid::new_v4().to_string();
            let mut cache = command_cache();
            let wake = runtime.block_on(handle_command_frame(
                json!({
                    "type": "command",
                    "command_id": wake_run_id,
                    "session_id": session_id,
                    "command_type": COMMAND_TURN_START,
                    "payload": {
                        "provider": "claude",
                        "thread_id": thread_id,
                        "turn_id": Uuid::new_v4().to_string(),
                        "run_id": wake_run_id,
                        "client_request_id": "wake:gone:1",
                        "cwd": workspace,
                        "message": "",
                        "permission_mode": "bypass",
                        "origin": "wake",
                        "wake_id": "gone:1",
                        "invocation_id": "gone",
                        "resume_provider_thread_id": provider_thread_id,
                    },
                }),
                &mut cache,
                &test_config(),
            ));
            assert_eq!(wake["ok"], true, "{wake}");
        });
    }

    #[tokio::test]
    async fn opencode_console_rejects_invisible_interactive_permission_mode() {
        let run_id = Uuid::new_v4().to_string();
        let mut cache = command_cache();
        let response = handle_command_frame(
            json!({
                "type": "command",
                "command_id": run_id,
                "session_id": Uuid::new_v4().to_string(),
                "command_type": COMMAND_TURN_START,
                "payload": {
                    "provider": "opencode",
                    "thread_id": Uuid::new_v4().to_string(),
                    "turn_id": Uuid::new_v4().to_string(),
                    "run_id": run_id,
                    "cwd": std::env::temp_dir(),
                    "message": "do work",
                    "permission_mode": "remote_approve",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "permission_mode_unsupported");
    }

    #[tokio::test]
    async fn cursor_console_rejects_policies_without_a_valid_control_path() {
        for permission_mode in ["provider_local", "remote_human", "remote_approve"] {
            let run_id = Uuid::new_v4().to_string();
            let mut cache = command_cache();
            let response = handle_command_frame(
                json!({
                    "type": "command",
                    "command_id": run_id,
                    "session_id": Uuid::new_v4().to_string(),
                    "command_type": COMMAND_TURN_START,
                    "payload": {
                        "provider": "cursor",
                        "thread_id": Uuid::new_v4().to_string(),
                        "turn_id": Uuid::new_v4().to_string(),
                        "run_id": run_id,
                        "cwd": std::env::temp_dir(),
                        "message": "do work",
                        "permission_mode": permission_mode,
                    },
                }),
                &mut cache,
                &test_config(),
            )
            .await;

            assert_eq!(response["ok"], false, "{permission_mode}: {response}");
            assert_eq!(
                response["error"]["code"], "permission_policy_unsupported",
                "{permission_mode}: {response}"
            );
        }
    }

    #[test]
    fn claude_pause_response_text_derives_structured_answers() {
        let payload = json!({
            "answers": {
                "timeline": ["Two weeks", "Show HN"],
                "success_metric": "Real users + feedback",
            },
        });

        assert_eq!(
            claude_pause_response_text(&payload).unwrap(),
            "success_metric: Real users + feedback; timeline: Two weeks, Show HN"
        );
    }

    #[test]
    fn attachment_stage_errors_keep_transient_fetches_queued() {
        assert_eq!(
            attachment_stage_command_error("attachment staging exceeded 8s".to_string()).code,
            "attachment_stage_outcome_unknown"
        );
        assert_eq!(
            attachment_stage_command_error("attachment_fetch_failed: HTTP 404".to_string()).code,
            "attachment_stage_failed"
        );
    }
}
