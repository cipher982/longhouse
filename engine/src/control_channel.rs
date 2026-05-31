//! Machine Agent managed-control WebSocket client.

use std::collections::{HashMap, VecDeque};
use std::ffi::{OsStr, OsString};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use futures_util::{Sink, SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
use std::process::Stdio;
use tokio::process::Command;
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;

use crate::build_identity;
use crate::codex_bridge::{
    cmd_codex_bridge_interrupt, cmd_codex_bridge_send, cmd_codex_bridge_start,
    cmd_codex_bridge_steer, validate_codex_bridge_attached, BridgeInterruptConfig,
    BridgeLaunchMode, BridgeSendConfig, BridgeStartConfig, BridgeSteerConfig, BridgeSteerError,
};
use crate::config::ShipperConfig;
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const COMMAND_SEND_TEXT: &str = "session.send_text";
const COMMAND_INTERRUPT: &str = "session.interrupt";
const COMMAND_STEER_TEXT: &str = "session.steer_text";
const COMMAND_LAUNCH: &str = "session.launch";
const COMMAND_PROVIDER_LIVE_PROOF: &str = "provider.live_proof";
const DEFAULT_CODEX_BIN: &str = "codex";
const DEFAULT_LONGHOUSE_BIN: &str = "longhouse";
// Engine is built from the monorepo. Keep this path beside the Python reader so
// advertised supports[] and server-side contracts cannot drift silently.
const MANAGED_PROVIDER_CONTRACTS_JSON: &str =
    include_str!("../../server/zerg/config/managed_provider_contracts.json");
const LAUNCH_START_TIMEOUT_SECS: u64 = 45;
const COMPLETED_COMMAND_CACHE_CAPACITY: usize = 256;
const COMPLETED_COMMAND_CACHE_TTL_SECS: u64 = 5 * 60;
// Keep this below uvicorn/websockets' default ping timeout. Tungstenite may
// queue protocol pongs internally and flush them on the next write, so the
// app-level heartbeat also keeps server keepalive pongs moving through proxies.
const HEARTBEAT_INTERVAL_SECS: u64 = 10;
const CONTROL_CONNECT_TIMEOUT_SECS: u64 = 15;
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

fn is_executable(path: &Path) -> bool {
    let Ok(metadata) = std::fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }
    #[cfg(unix)]
    {
        metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(not(unix))]
    {
        true
    }
}

fn command_value_exists_in_path(command: &OsStr, path_value: Option<&OsStr>) -> bool {
    let command_path = Path::new(command);
    if command_path.is_absolute() || command_path.components().count() > 1 {
        return is_executable(command_path);
    }
    let Some(path_value) = path_value else {
        return false;
    };
    std::env::split_paths(path_value)
        .map(|dir| dir.join(command_path))
        .any(|candidate| is_executable(&candidate))
}

fn command_exists_in_path(command: &str, path_value: Option<&OsStr>) -> bool {
    command_value_exists_in_path(OsStr::new(command), path_value)
}

fn managed_provider_contract_items() -> &'static Vec<Value> {
    let payload = MANAGED_PROVIDER_CONTRACTS.get_or_init(|| {
        let payload: Value = serde_json::from_str(MANAGED_PROVIDER_CONTRACTS_JSON)
            .expect("managed provider contract manifest must be valid JSON");
        validate_managed_provider_contract_manifest(&payload)
            .expect("managed provider contract manifest must satisfy the engine contract");
        payload
    });
    payload
        .get("providers")
        .and_then(Value::as_array)
        .expect("managed provider contract manifest must contain providers[]")
}

fn validate_managed_provider_contract_manifest(payload: &Value) -> Result<(), String> {
    if payload.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err("schema_version must be 1".to_string());
    }
    let providers = payload
        .get("providers")
        .and_then(Value::as_array)
        .ok_or_else(|| "providers[] missing".to_string())?;
    let operations = [
        "launch_local",
        "launch_remote",
        "reattach",
        "send_input",
        "interrupt",
        "steer_active_turn",
        "terminate",
        "tail_output",
        "runtime_phase",
        "transcript_binding",
    ];
    let evidence_levels = [
        "none",
        "source_review",
        "hermetic",
        "live_no_token",
        "live_token",
    ];
    for provider in providers {
        let provider_name = provider
            .get("provider")
            .and_then(Value::as_str)
            .unwrap_or("<unknown>");
        let evidence = provider
            .get("operation_evidence")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("{provider_name}: operation_evidence must be an object"))?;
        for key in evidence.keys() {
            if !operations.contains(&key.as_str()) {
                return Err(format!(
                    "{provider_name}: unknown operation_evidence key {key}"
                ));
            }
        }
        for operation in operations {
            let supported = provider
                .get(operation)
                .and_then(Value::as_bool)
                .ok_or_else(|| {
                    format!("{provider_name}.{operation}: support flag must be boolean")
                })?;
            let entry = evidence
                .get(operation)
                .and_then(Value::as_object)
                .ok_or_else(|| format!("{provider_name}.{operation}: evidence missing"))?;
            let level = entry
                .get("level")
                .and_then(Value::as_str)
                .ok_or_else(|| format!("{provider_name}.{operation}: evidence level missing"))?;
            if !evidence_levels.contains(&level) {
                return Err(format!(
                    "{provider_name}.{operation}: unknown evidence level {level}"
                ));
            }
            let source = entry
                .get("source")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim();
            if source.is_empty() {
                return Err(format!(
                    "{provider_name}.{operation}: evidence source missing"
                ));
            }
            if supported == (level == "none") {
                return Err(format!(
                    "{provider_name}.{operation}: support flag and evidence level diverge"
                ));
            }
        }
    }
    Ok(())
}

fn provider_binary_available(
    contract: &Value,
    path_value: Option<&OsStr>,
    env_lookup: &dyn Fn(&str) -> Option<OsString>,
) -> bool {
    if let Some(env_name) = contract.get("provider_cli_env").and_then(Value::as_str) {
        if !env_name.trim().is_empty() {
            if let Some(env_value) = env_lookup(env_name) {
                return !env_value.as_os_str().is_empty()
                    && command_value_exists_in_path(env_value.as_os_str(), path_value);
            }
        }
    }

    let binary = contract
        .get("provider_cli_binary")
        .and_then(Value::as_str)
        .unwrap_or_default();
    !binary.is_empty() && command_exists_in_path(binary, path_value)
}

fn control_supports_for_path_with_env(
    path_value: Option<&OsStr>,
    env_lookup: &dyn Fn(&str) -> Option<OsString>,
) -> Vec<String> {
    let mut supports = Vec::new();
    let longhouse_available = command_exists_in_path(DEFAULT_LONGHOUSE_BIN, path_value);
    for contract in managed_provider_contract_items() {
        let requires_longhouse = contract
            .get("requires_longhouse_cli")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if requires_longhouse && !longhouse_available {
            continue;
        }
        if !provider_binary_available(contract, path_value, env_lookup) {
            continue;
        }
        if let Some(items) = contract
            .get("machine_control_supports")
            .and_then(Value::as_array)
        {
            supports.extend(items.iter().filter_map(Value::as_str).map(str::to_string));
        }
        if longhouse_available {
            if let Some(provider) = contract.get("provider").and_then(Value::as_str) {
                if provider_live_proof_supported_provider(provider) {
                    supports.push(format!("{provider}.live_proof"));
                }
            }
        }
    }
    supports
}

fn provider_live_proof_supported_provider(provider: &str) -> bool {
    matches!(provider, "claude" | "opencode" | "antigravity")
}

fn control_supports_for_path(path_value: Option<&OsStr>) -> Vec<String> {
    control_supports_for_path_with_env(path_value, &|name| std::env::var_os(name))
}

fn control_supports() -> Vec<String> {
    control_supports_for_path(std::env::var_os("PATH").as_deref())
}

pub fn spawn_control_channel(
    config: ShipperConfig,
    status: ControlChannelStatus,
) -> Option<JoinHandle<()>> {
    if config.api_token.as_deref().unwrap_or("").trim().is_empty() {
        status.set_disabled();
        tracing::debug!("Machine control channel disabled because no device token is configured");
        return None;
    }
    status.set_disconnected(None, None, None, None);

    Some(tokio::spawn(async move {
        run_reconnect_loop(config, status).await;
    }))
}

async fn run_reconnect_loop(config: ShipperConfig, status: ControlChannelStatus) {
    let mut backoff = Duration::from_secs(1);
    let mut last_error: Option<String> = None;
    let mut outage_started: Option<Instant> = None;
    let mut completed_commands = CompletedCommandCache::new(
        COMPLETED_COMMAND_CACHE_CAPACITY,
        Duration::from_secs(COMPLETED_COMMAND_CACHE_TTL_SECS),
    );
    loop {
        let connected_before = status.snapshot().last_connected_at;
        let result = run_once(&config, &mut completed_commands, &status).await;
        let connected_during_attempt = status.snapshot().last_connected_at != connected_before;
        let reconnect_delay = if connected_during_attempt {
            outage_started = Some(Instant::now());
            backoff = Duration::from_secs(1);
            backoff
        } else {
            outage_started.get_or_insert_with(Instant::now);
            backoff
        };

        match result {
            Ok(()) => {
                tracing::info!("Machine control channel disconnected");
                status.set_disconnected(None, None, None, Some(reconnect_delay.as_secs()));
                last_error = None;
            }
            Err(err) => {
                let error_chain = format_error_chain(&err);
                status.set_disconnected(
                    None,
                    Some("connect_failed"),
                    Some(error_chain.as_str()),
                    Some(reconnect_delay.as_secs()),
                );
                if last_error.as_deref() == Some(error_chain.as_str()) {
                    tracing::debug!(error = %error_chain, "Machine control channel connection failed");
                } else {
                    tracing::warn!(error = %error_chain, "Machine control channel connection failed");
                    last_error = Some(error_chain);
                }
            }
        }
        tokio::time::sleep(reconnect_delay).await;
        let outage_elapsed = outage_started
            .map(|started| started.elapsed())
            .unwrap_or(Duration::ZERO);
        backoff = next_reconnect_backoff(reconnect_delay, outage_elapsed);
    }
}

fn next_reconnect_backoff(current: Duration, outage_elapsed: Duration) -> Duration {
    let max_backoff = if outage_elapsed < Duration::from_secs(CONTROL_RECONNECT_SHORT_WINDOW_SECS) {
        Duration::from_secs(CONTROL_RECONNECT_SHORT_MAX_BACKOFF_SECS)
    } else {
        Duration::from_secs(CONTROL_RECONNECT_SUSTAINED_MAX_BACKOFF_SECS)
    };
    (current * 2).min(max_backoff)
}

fn format_error_chain(err: &anyhow::Error) -> String {
    err.chain()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join(": ")
}

async fn run_once(
    config: &ShipperConfig,
    completed_commands: &mut CompletedCommandCache,
    status: &ControlChannelStatus,
) -> Result<()> {
    let ws_url = control_ws_url(&config.api_url)?;
    status.set_disconnected(Some(&ws_url), None, None, None);
    let mut request = ws_url
        .as_str()
        .into_client_request()
        .context("building control websocket request")?;
    if let Some(token) = config.api_token.as_deref() {
        request.headers_mut().insert(
            "X-Agents-Token",
            HeaderValue::from_str(token).context("invalid X-Agents-Token header")?,
        );
    }

    let (mut stream, _) = tokio::time::timeout(
        Duration::from_secs(CONTROL_CONNECT_TIMEOUT_SECS),
        connect_async(request),
    )
    .await
    .map_err(|_| anyhow!("timed out connecting machine control websocket {ws_url}"))?
    .with_context(|| format!("connecting machine control websocket {ws_url}"))?;
    let hello = json!({
        "type": "hello",
        "schema_version": 1,
        "device_id": config.machine_name,
        "machine_name": config.machine_name,
        "engine_build": build_identity::COMMIT_SHORT,
        "supports": control_supports(),
    });
    send_control_message(
        &mut stream,
        Message::Text(hello.to_string()),
        "machine control hello",
        status,
    )
    .await?;
    status.set_connected(&ws_url);
    tracing::info!("Machine control channel connected to {ws_url}");

    let heartbeat_interval = Duration::from_secs(HEARTBEAT_INTERVAL_SECS);
    let mut heartbeat = tokio::time::interval(Duration::from_secs(HEARTBEAT_INTERVAL_SECS));
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    heartbeat.tick().await;
    let mut next_heartbeat_due = Instant::now() + heartbeat_interval;

    loop {
        tokio::select! {
            _ = heartbeat.tick() => {
                let now = Instant::now();
                let lateness = now.saturating_duration_since(next_heartbeat_due);
                status.record_heartbeat_lateness(lateness);
                if lateness.as_millis() > CONTROL_HEARTBEAT_LATE_WARN_MS {
                    tracing::warn!(
                        lateness_ms = duration_millis_u64(lateness),
                        heartbeat_interval_secs = HEARTBEAT_INTERVAL_SECS,
                        "Machine control heartbeat delayed; executor stall suspected"
                    );
                }
                next_heartbeat_due = now + heartbeat_interval;
                send_control_message(
                    &mut stream,
                    Message::Text(heartbeat_frame().to_string()),
                    "machine control heartbeat",
                    status,
                )
                .await?;
            }
            message = stream.next() => {
                let Some(message) = message else {
                    break;
                };
                let message = message.context("reading machine control websocket message")?;
                let text = match message {
                    Message::Text(text) => text,
                    Message::Close(frame) => {
                        tracing::info!(?frame, "Machine control channel received close frame");
                        break;
                    }
                    Message::Ping(payload) => {
                        send_control_message(
                            &mut stream,
                            Message::Pong(payload),
                            "machine control pong",
                            status,
                        )
                        .await?;
                        continue;
                    }
                    _ => {
                        continue;
                    }
                };
                let frame: Value = serde_json::from_str(&text).context("parsing machine control frame")?;
                if frame.get("type").and_then(Value::as_str) != Some("command") {
                    tracing::debug!(
                        "Ignoring machine control frame type={:?}",
                        frame.get("type")
                    );
                    continue;
                }
                let result = handle_command_frame(frame, completed_commands, config).await;
                send_control_message(
                    &mut stream,
                    Message::Text(result.to_string()),
                    "machine control command result",
                    status,
                )
                .await?;
            }
        }
    }

    Ok(())
}

async fn send_control_message<S>(
    stream: &mut S,
    message: Message,
    context: &'static str,
    status: &ControlChannelStatus,
) -> Result<()>
where
    S: Sink<Message, Error = tokio_tungstenite::tungstenite::Error> + Unpin,
{
    let started = Instant::now();
    tokio::time::timeout(
        Duration::from_secs(CONTROL_WRITE_TIMEOUT_SECS),
        stream.send(message),
    )
    .await
    .map_err(|_| anyhow!("timed out sending {context}"))?
    .with_context(|| format!("sending {context}"))?;
    let elapsed = started.elapsed();
    status.record_write_elapsed(elapsed);
    if elapsed > Duration::from_secs(1) {
        tracing::warn!(
            context,
            elapsed_ms = duration_millis_u64(elapsed),
            "Machine control websocket send was slow"
        );
    }
    Ok(())
}

fn heartbeat_frame() -> Value {
    json!({"type": "heartbeat"})
}

async fn handle_command_frame(
    frame: Value,
    completed_commands: &mut CompletedCommandCache,
    config: &ShipperConfig,
) -> Value {
    let command_id = frame
        .get("command_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if command_id.is_empty() {
        return command_error("", "invalid_command", "command_id is required");
    }

    if let Some(result) = completed_commands.get(&command_id) {
        return result;
    }

    let result = execute_command(&frame, config).await;
    let response = match result {
        Ok(result) => json!({
            "type": "command_result",
            "command_id": &command_id,
            "ok": true,
            "result": result,
        }),
        Err(CommandError { code, message }) => command_error(&command_id, &code, &message),
    };
    completed_commands.insert(command_id, response.clone());
    response
}

async fn execute_command(
    frame: &Value,
    config: &ShipperConfig,
) -> std::result::Result<Value, CommandError> {
    let command_type = required_string(frame, "command_type")?;
    let payload = frame.get("payload").cloned().unwrap_or_else(|| json!({}));

    if command_type == COMMAND_PROVIDER_LIVE_PROOF {
        return run_provider_live_proof_command(&payload).await;
    }

    let session_id = required_string(frame, "session_id")?;

    match command_type.as_str() {
        COMMAND_LAUNCH => {
            let provider = payload_required_string(&payload, "provider")?;
            if provider != "codex" && provider != "claude" && provider != "opencode" {
                return Err(CommandError {
                    code: "provider_unsupported".to_string(),
                    message: format!("provider={provider} is not supported by this engine build"),
                });
            }
            let cwd_raw = payload_required_string(&payload, "cwd")?;
            let cwd = PathBuf::from(&cwd_raw);
            if !cwd.is_absolute() {
                return Err(CommandError {
                    code: "cwd_not_allowed".to_string(),
                    message: "cwd must be absolute".to_string(),
                });
            }
            if !cwd.is_dir() {
                return Err(CommandError {
                    code: "cwd_not_found".to_string(),
                    message: format!("cwd does not exist: {}", cwd.display()),
                });
            }
            let api_url = config.api_url.clone();
            let api_token = config.api_token.clone().ok_or_else(|| CommandError {
                code: "provider_launch_failed".to_string(),
                message: "Machine Agent has no device token configured".to_string(),
            })?;
            let resume_target = payload_resume_target(&payload)?;

            if provider == "claude" {
                return launch_claude_channel_session(session_id.clone(), cwd, api_url, api_token)
                    .await;
            }
            if provider == "opencode" {
                return launch_opencode_server_session(
                    session_id.clone(),
                    cwd,
                    api_url,
                    api_token,
                    config.machine_name.clone(),
                    payload_optional_string(&payload, "display_name"),
                )
                .await;
            }

            let summary = cmd_codex_bridge_start(BridgeStartConfig {
                session_id: session_id.clone(),
                cwd,
                api_url,
                api_token,
                codex_bin: DEFAULT_CODEX_BIN.to_string(),
                approval_policy: None,
                sandbox: None,
                model: None,
                model_reasoning_effort: None,
                machine_name: Some(config.machine_name.clone()),
                auto_approve: false,
                state_root: None,
                longhouse_home: None,
                log_file: None,
                start_timeout_secs: LAUNCH_START_TIMEOUT_SECS,
                // Detached-UI remote launch: there is no visible TUI to create
                // a thread, so we ask the bridge to call thread/start itself.
                create_initial_thread: resume_target.is_none(),
                resume_thread_id: resume_target
                    .as_ref()
                    .map(|target| target.thread_id.clone()),
                resume_thread_path: resume_target
                    .as_ref()
                    .and_then(|target| target.thread_path.clone()),
                launch_mode: BridgeLaunchMode::DetachedUi,
            })
            .await
            .map_err(|err| CommandError {
                code: "provider_launch_failed".to_string(),
                message: err.to_string(),
            })?;

            Ok(json!({
                "session_id": summary.session_id,
                "provider": "codex",
                "transport": "codex_app_server",
                "ws_url": summary.ws_url,
                "thread_id": summary.thread_id,
                "thread_path": summary.thread_path,
            }))
        }
        COMMAND_SEND_TEXT => {
            let text = payload_required_string(&payload, "text")?;
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| "codex".to_string());
            if provider == "claude" {
                return run_claude_channel_command(
                    claude_channel_args(COMMAND_SEND_TEXT, &session_id, Some(text))?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "claude", "claude_channel_bridge"));
            }
            if provider == "opencode" {
                return run_opencode_channel_command(
                    opencode_channel_args(COMMAND_SEND_TEXT, &session_id, Some(text))?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "opencode", "opencode_server_bridge"));
            }
            if provider == "antigravity" {
                return run_antigravity_channel_command(
                    antigravity_channel_args(COMMAND_SEND_TEXT, &session_id, Some(text))?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "antigravity", "antigravity_hook_inbox"));
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            let summary = cmd_codex_bridge_send(BridgeSendConfig {
                session_id: session_id.clone(),
                text,
                state_root: None,
                allow_direct_ws_fallback: false,
                attachments: Vec::new(),
            })
            .await
            .map_err(|err| CommandError::command_failed(err))?;
            Ok(json!({
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
                "thread_id": summary.thread_id,
                "turn_id": summary.turn_id,
                "turn_status": summary.turn_status,
            }))
        }
        COMMAND_INTERRUPT => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| "codex".to_string());
            if provider == "claude" {
                return run_claude_channel_command(
                    claude_channel_args(COMMAND_INTERRUPT, &session_id, None)?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "claude", "claude_channel_bridge"));
            }
            if provider == "opencode" {
                return run_opencode_channel_command(
                    opencode_channel_args(COMMAND_INTERRUPT, &session_id, None)?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "opencode", "opencode_server_bridge"));
            }
            if provider == "antigravity" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: "Antigravity hook inbox does not support remote interrupts"
                        .to_string(),
                });
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            cmd_codex_bridge_interrupt(BridgeInterruptConfig {
                session_id,
                state_root: None,
            })
            .await
            .map_err(|err| CommandError::command_failed(err))?;
            Ok(json!({
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
            }))
        }
        COMMAND_STEER_TEXT => {
            let text = payload_required_string(&payload, "text")?;
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| "codex".to_string());
            if provider == "claude" {
                return run_claude_channel_command(
                    claude_channel_args(COMMAND_STEER_TEXT, &session_id, Some(text))?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "claude", "claude_channel_bridge"));
            }
            if provider == "opencode" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: "OpenCode server bridge does not support active-turn steer"
                        .to_string(),
                });
            }
            if provider == "antigravity" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: "Antigravity hook inbox does not support active-turn steer"
                        .to_string(),
                });
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            match cmd_codex_bridge_steer(BridgeSteerConfig {
                session_id,
                text,
                state_root: None,
                attachments: Vec::new(),
            })
            .await
            {
                Ok(()) => Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "codex",
                    "transport": "codex_app_server",
                })),
                Err(BridgeSteerError::NoActiveTurn) => Err(CommandError::turn_ended(
                    "bridge state does not have an active turn to steer",
                )),
                Err(BridgeSteerError::TurnEnded(message)) => Err(CommandError::turn_ended(message)),
                Err(err) => Err(CommandError::command_failed(err)),
            }
        }
        other => Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("Unsupported command_type={other}"),
        }),
    }
}

fn claude_channel_args(
    command_type: &str,
    session_id: &str,
    text: Option<String>,
) -> std::result::Result<Vec<String>, CommandError> {
    match command_type {
        COMMAND_SEND_TEXT => Ok(vec![
            "claude-channel".to_string(),
            "send".to_string(),
            "--session-id".to_string(),
            session_id.to_string(),
            "--text".to_string(),
            text.ok_or_else(|| CommandError {
                code: "invalid_command".to_string(),
                message: "text is required".to_string(),
            })?,
        ]),
        COMMAND_INTERRUPT => Ok(vec![
            "claude-channel".to_string(),
            "interrupt".to_string(),
            "--session-id".to_string(),
            session_id.to_string(),
        ]),
        COMMAND_STEER_TEXT => Ok(vec![
            "claude-channel".to_string(),
            "send".to_string(),
            "--session-id".to_string(),
            session_id.to_string(),
            "--text".to_string(),
            text.ok_or_else(|| CommandError {
                code: "invalid_command".to_string(),
                message: "text is required".to_string(),
            })?,
            "--meta".to_string(),
            "intent=steer".to_string(),
        ]),
        _ => Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("unsupported Claude channel command {command_type}"),
        }),
    }
}

fn opencode_channel_args(
    command_type: &str,
    session_id: &str,
    text: Option<String>,
) -> std::result::Result<Vec<String>, CommandError> {
    match command_type {
        COMMAND_SEND_TEXT => Ok(vec![
            "opencode-channel".to_string(),
            "send".to_string(),
            "--session-id".to_string(),
            session_id.to_string(),
            "--text".to_string(),
            text.ok_or_else(|| CommandError {
                code: "invalid_command".to_string(),
                message: "text is required".to_string(),
            })?,
        ]),
        COMMAND_INTERRUPT => Ok(vec![
            "opencode-channel".to_string(),
            "interrupt".to_string(),
            "--session-id".to_string(),
            session_id.to_string(),
        ]),
        _ => Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("unsupported OpenCode channel command {command_type}"),
        }),
    }
}

fn antigravity_channel_args(
    command_type: &str,
    session_id: &str,
    text: Option<String>,
) -> std::result::Result<Vec<String>, CommandError> {
    match command_type {
        COMMAND_SEND_TEXT => Ok(vec![
            "antigravity-channel".to_string(),
            "send".to_string(),
            "--session-id".to_string(),
            session_id.to_string(),
            "--text".to_string(),
            text.ok_or_else(|| CommandError {
                code: "invalid_command".to_string(),
                message: "text is required".to_string(),
            })?,
        ]),
        _ => Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("unsupported Antigravity channel command {command_type}"),
        }),
    }
}

struct CliCommandOutput {
    exit_code: i32,
    stdout: String,
    stderr: String,
}

async fn run_longhouse_command(
    args: Vec<String>,
    timeout_secs: u64,
    envs: Vec<(&str, String)>,
) -> std::result::Result<CliCommandOutput, CommandError> {
    let mut command = Command::new(DEFAULT_LONGHOUSE_BIN);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    for (key, value) in envs {
        command.env(key, value);
    }

    let child = command.spawn().map_err(|err| CommandError {
        code: "provider_launch_failed".to_string(),
        message: format!("failed to start longhouse command: {err}"),
    })?;
    let output = tokio::time::timeout(Duration::from_secs(timeout_secs), child.wait_with_output())
        .await
        .map_err(|_| CommandError {
            code: "provider_launch_failed".to_string(),
            message: format!("longhouse command timed out after {timeout_secs} seconds"),
        })?
        .map_err(|err| CommandError {
            code: "provider_launch_failed".to_string(),
            message: format!("longhouse command failed: {err}"),
        })?;

    Ok(CliCommandOutput {
        exit_code: output.status.code().unwrap_or(1),
        stdout: String::from_utf8_lossy(&output.stdout).to_string(),
        stderr: String::from_utf8_lossy(&output.stderr).to_string(),
    })
}

async fn run_claude_channel_command(
    args: Vec<String>,
    timeout_secs: u64,
) -> std::result::Result<CliCommandOutput, CommandError> {
    let output = run_longhouse_command(args, timeout_secs, Vec::new()).await?;
    if output.exit_code != 0 {
        return Err(CommandError {
            code: "command_failed".to_string(),
            message: nonempty_cli_error(&output),
        });
    }
    Ok(output)
}

async fn run_opencode_channel_command(
    args: Vec<String>,
    timeout_secs: u64,
) -> std::result::Result<CliCommandOutput, CommandError> {
    let output = run_longhouse_command(args, timeout_secs, Vec::new()).await?;
    if output.exit_code != 0 {
        return Err(CommandError {
            code: "command_failed".to_string(),
            message: nonempty_cli_error(&output),
        });
    }
    Ok(output)
}

async fn run_antigravity_channel_command(
    args: Vec<String>,
    timeout_secs: u64,
) -> std::result::Result<CliCommandOutput, CommandError> {
    let output = run_longhouse_command(args, timeout_secs, Vec::new()).await?;
    if output.exit_code != 0 {
        return Err(CommandError {
            code: "command_failed".to_string(),
            message: nonempty_cli_error(&output),
        });
    }
    Ok(output)
}

async fn run_provider_live_proof_command(
    payload: &Value,
) -> std::result::Result<Value, CommandError> {
    let provider = payload_required_string(payload, "provider")?;
    if !provider_live_proof_supported_provider(&provider) {
        return Err(CommandError {
            code: "provider_unsupported".to_string(),
            message: format!("provider={provider} is not supported for provider live proof"),
        });
    }
    let publish = payload_optional_bool(payload, "publish").unwrap_or(true);
    let timeout_secs = payload_optional_u64(payload, "timeout_secs", 1, 900).unwrap_or(120);
    let expected_provider_version = payload_optional_string(payload, "expected_provider_version");

    let args = vec![
        "provider-live".to_string(),
        if publish {
            "publish".to_string()
        } else {
            "canary".to_string()
        },
        "--provider".to_string(),
        provider.clone(),
        "--json".to_string(),
    ];

    let output = run_longhouse_command(args, timeout_secs, Vec::new()).await?;
    let payload_json: Value =
        serde_json::from_str(output.stdout.trim()).map_err(|err| CommandError {
            code: "provider_live_proof_failed".to_string(),
            message: format!(
                "provider-live returned invalid JSON: {err}; exit_code={}; stderr={}",
                output.exit_code,
                output.stderr.trim()
            ),
        })?;
    let artifact = if publish {
        read_published_provider_live_artifact(&payload_json, &provider)?
    } else {
        payload_json.clone()
    };
    let version_match =
        provider_live_proof_version_match(expected_provider_version.as_deref(), &artifact)?;

    Ok(json!({
        "provider": provider,
        "transport": "provider_live_proof",
        "publish": publish,
        "expected_provider_version": expected_provider_version,
        "provider_version_match": version_match,
        "exit_code": output.exit_code,
        "stderr": output.stderr,
        "payload": payload_json,
        "artifact": artifact,
    }))
}

fn read_published_provider_live_artifact(
    payload: &Value,
    provider: &str,
) -> std::result::Result<Value, CommandError> {
    let result = payload
        .get("results")
        .and_then(Value::as_array)
        .and_then(|items| {
            items
                .iter()
                .find(|item| item.get("provider").and_then(Value::as_str) == Some(provider))
        })
        .ok_or_else(|| CommandError {
            code: "provider_live_proof_failed".to_string(),
            message: format!("provider-live publish did not return a result for {provider}"),
        })?;
    let stable_path = result
        .get("stable_path")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| CommandError {
            code: "provider_live_proof_failed".to_string(),
            message: format!("provider-live publish did not return stable_path for {provider}"),
        })?;
    let text = std::fs::read_to_string(stable_path).map_err(|err| CommandError {
        code: "provider_live_proof_failed".to_string(),
        message: format!("failed to read provider live proof artifact {stable_path}: {err}"),
    })?;
    serde_json::from_str(&text).map_err(|err| CommandError {
        code: "provider_live_proof_failed".to_string(),
        message: format!("provider live proof artifact {stable_path} is invalid JSON: {err}"),
    })
}

fn provider_live_proof_version_match(
    expected_provider_version: Option<&str>,
    artifact: &Value,
) -> std::result::Result<Value, CommandError> {
    let expected_raw = match expected_provider_version
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        Some(value) => value,
        None => return Ok(json!({"status": "not_requested"})),
    };
    let artifact_raw = artifact
        .get("provider_version")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    let normalized_expected = normalize_provider_version(expected_raw);
    let normalized_artifact = normalize_provider_version(artifact_raw);
    let matches = normalized_expected.is_some()
        && normalized_artifact.is_some()
        && normalized_expected == normalized_artifact;
    let details = json!({
        "status": if matches { "match" } else { "mismatch" },
        "expected_provider_version": expected_raw,
        "artifact_provider_version": artifact_raw,
        "normalized_expected_provider_version": normalized_expected,
        "normalized_artifact_provider_version": normalized_artifact,
    });
    if matches {
        Ok(details)
    } else {
        Err(CommandError {
            code: "provider_version_mismatch".to_string(),
            message: format!(
                "provider live proof version mismatch: expected {expected_raw}, artifact reported {}",
                if artifact_raw.is_empty() { "<missing>" } else { artifact_raw }
            ),
        })
    }
}

fn normalize_provider_version(raw: &str) -> Option<String> {
    let value = raw.trim();
    if value.is_empty() {
        return None;
    }
    let chars: Vec<char> = value.chars().collect();
    for start in 0..chars.len() {
        if !chars[start].is_ascii_digit() {
            continue;
        }
        let mut idx = start;
        let mut dot_count = 0;
        while idx < chars.len() {
            let ch = chars[idx];
            if ch.is_ascii_digit() {
                idx += 1;
                continue;
            }
            if ch == '.' && idx + 1 < chars.len() && chars[idx + 1].is_ascii_digit() {
                dot_count += 1;
                idx += 1;
                continue;
            }
            break;
        }
        if dot_count < 2 {
            continue;
        }
        if idx < chars.len() && (chars[idx] == '-' || chars[idx] == '+') {
            idx += 1;
            while idx < chars.len()
                && (chars[idx].is_ascii_alphanumeric() || matches!(chars[idx], '.' | '-' | '+'))
            {
                idx += 1;
            }
        }
        return Some(
            chars[start..idx]
                .iter()
                .collect::<String>()
                .to_ascii_lowercase(),
        );
    }
    Some(value.trim_start_matches('v').to_ascii_lowercase())
}

async fn launch_claude_channel_session(
    session_id: String,
    cwd: PathBuf,
    api_url: String,
    api_token: String,
) -> std::result::Result<Value, CommandError> {
    let output = run_longhouse_command(
        vec![
            "claude-channel".to_string(),
            "launch".to_string(),
            "--session-id".to_string(),
            session_id.clone(),
            "--provider-session-id".to_string(),
            session_id.clone(),
            "--cwd".to_string(),
            cwd.display().to_string(),
            "--api-url".to_string(),
            api_url,
            "--wait-ready-secs".to_string(),
            LAUNCH_START_TIMEOUT_SECS.to_string(),
        ],
        LAUNCH_START_TIMEOUT_SECS * 2,
        vec![("LONGHOUSE_CLAUDE_REMOTE_LAUNCH_TOKEN", api_token)],
    )
    .await?;
    if output.exit_code != 0 {
        return Err(CommandError {
            code: "provider_launch_failed".to_string(),
            message: nonempty_cli_error(&output),
        });
    }
    let payload: Value =
        serde_json::from_str(output.stdout.trim()).map_err(|err| CommandError {
            code: "provider_launch_failed".to_string(),
            message: format!(
                "Claude launch returned invalid JSON: {err}; stderr={}",
                output.stderr.trim()
            ),
        })?;
    Ok(json!({
        "session_id": payload.get("session_id").and_then(Value::as_str).unwrap_or(&session_id),
        "provider": "claude",
        "transport": "claude_channel_bridge",
        "provider_session_id": payload
            .get("provider_session_id")
            .and_then(Value::as_str)
            .unwrap_or(&session_id),
        "pid": payload.get("pid").cloned().unwrap_or(Value::Null),
        "log_path": payload.get("log_path").cloned().unwrap_or(Value::Null),
    }))
}

async fn launch_opencode_server_session(
    session_id: String,
    cwd: PathBuf,
    api_url: String,
    api_token: String,
    machine_name: String,
    display_name: Option<String>,
) -> std::result::Result<Value, CommandError> {
    let mut args = vec![
        "opencode-channel".to_string(),
        "launch".to_string(),
        "--session-id".to_string(),
        session_id.clone(),
        "--cwd".to_string(),
        cwd.display().to_string(),
        "--api-url".to_string(),
        api_url,
        "--device-id".to_string(),
        machine_name,
        "--wait-ready-secs".to_string(),
        LAUNCH_START_TIMEOUT_SECS.to_string(),
    ];
    if let Some(display_name) = display_name {
        args.push("--display-name".to_string());
        args.push(display_name);
    }
    let output = run_longhouse_command(
        args,
        LAUNCH_START_TIMEOUT_SECS * 2,
        vec![("LONGHOUSE_OPENCODE_REMOTE_LAUNCH_TOKEN", api_token)],
    )
    .await?;
    if output.exit_code != 0 {
        return Err(CommandError {
            code: "provider_launch_failed".to_string(),
            message: nonempty_cli_error(&output),
        });
    }
    let payload: Value =
        serde_json::from_str(output.stdout.trim()).map_err(|err| CommandError {
            code: "provider_launch_failed".to_string(),
            message: format!(
                "OpenCode launch returned invalid JSON: {err}; stderr={}",
                output.stderr.trim()
            ),
        })?;
    Ok(json!({
        "session_id": payload.get("session_id").and_then(Value::as_str).unwrap_or(&session_id),
        "provider": "opencode",
        "transport": "opencode_server_bridge",
        "provider_session_id": payload
            .get("provider_session_id")
            .and_then(Value::as_str)
            .unwrap_or(&session_id),
        "server_url": payload.get("server_url").cloned().unwrap_or(Value::Null),
        "pid": payload.get("pid").cloned().unwrap_or(Value::Null),
        "log_path": payload.get("log_path").cloned().unwrap_or(Value::Null),
    }))
}

fn cli_output_result(output: CliCommandOutput, provider: &str, transport: &str) -> Value {
    json!({
        "exit_code": output.exit_code,
        "stdout": output.stdout,
        "stderr": output.stderr,
        "provider": provider,
        "transport": transport,
    })
}

fn nonempty_cli_error(output: &CliCommandOutput) -> String {
    let stderr = output.stderr.trim();
    if !stderr.is_empty() {
        return stderr.to_string();
    }
    let stdout = output.stdout.trim();
    if !stdout.is_empty() {
        return stdout.to_string();
    }
    format!("longhouse command exited {}", output.exit_code)
}

struct CachedCommandResult {
    completed_at: Instant,
    result: Value,
}

struct CompletedCommandCache {
    capacity: usize,
    ttl: Duration,
    entries: HashMap<String, CachedCommandResult>,
    order: VecDeque<String>,
}

impl CompletedCommandCache {
    fn new(capacity: usize, ttl: Duration) -> Self {
        Self {
            capacity,
            ttl,
            entries: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    fn get(&mut self, command_id: &str) -> Option<Value> {
        self.prune(Instant::now());
        self.entries
            .get(command_id)
            .map(|cached| cached.result.clone())
    }

    fn insert(&mut self, command_id: String, result: Value) {
        if self.capacity == 0 {
            return;
        }
        let now = Instant::now();
        self.prune(now);
        if !self.entries.contains_key(&command_id) {
            self.order.push_back(command_id.clone());
        }
        self.entries.insert(
            command_id,
            CachedCommandResult {
                completed_at: now,
                result,
            },
        );
        while self.entries.len() > self.capacity {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            self.entries.remove(&oldest);
        }
    }

    fn prune(&mut self, now: Instant) {
        while let Some(command_id) = self.order.front() {
            let expired = self
                .entries
                .get(command_id)
                .map(|cached| now.duration_since(cached.completed_at) >= self.ttl)
                .unwrap_or(true);
            if !expired {
                break;
            }
            let Some(command_id) = self.order.pop_front() else {
                break;
            };
            self.entries.remove(&command_id);
        }
    }
}

fn control_ws_url(api_url: &str) -> Result<String> {
    let base = api_url.trim().trim_end_matches('/');
    if let Some(rest) = base.strip_prefix("http://") {
        return Ok(format!("ws://{rest}/api/agents/control/ws"));
    }
    if let Some(rest) = base.strip_prefix("https://") {
        return Ok(format!("wss://{rest}/api/agents/control/ws"));
    }
    bail!("api_url must start with http:// or https://")
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

fn payload_optional_bool(payload: &Value, key: &'static str) -> Option<bool> {
    payload.get(key).and_then(Value::as_bool)
}

fn payload_optional_u64(payload: &Value, key: &'static str, min: u64, max: u64) -> Option<u64> {
    payload
        .get(key)
        .and_then(Value::as_u64)
        .map(|value| value.clamp(min, max))
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

    static ENV_LOCK: Mutex<()> = Mutex::new(());

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

    fn write_test_executable(path: &Path, body: &str) {
        std::fs::write(path, body).unwrap();
        #[cfg(unix)]
        {
            let mut perms = std::fs::metadata(path).unwrap().permissions();
            perms.set_mode(0o755);
            std::fs::set_permissions(path, perms).unwrap();
        }
    }

    #[test]
    fn control_ws_url_converts_http_and_https() {
        assert_eq!(
            control_ws_url("http://localhost:8000").unwrap(),
            "ws://localhost:8000/api/agents/control/ws"
        );
        assert_eq!(
            control_ws_url("https://demo.longhouse.ai/").unwrap(),
            "wss://demo.longhouse.ai/api/agents/control/ws"
        );
    }

    #[test]
    fn heartbeat_frame_uses_server_schema() {
        assert_eq!(heartbeat_frame(), json!({"type": "heartbeat"}));
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
        let _guard = ENV_LOCK.lock().unwrap();
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
    fn control_channel_advertises_codex_continue() {
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
            .any(|item| item.as_str() == Some("codex.continue")));
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
                "launch_remote",
                "reattach",
                "send_input",
                "interrupt",
                "steer_active_turn",
                "terminate",
                "tail_output",
                "runtime_phase",
                "transcript_binding",
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

        write_executable(&dir, "longhouse");
        write_executable(&dir, "opencode");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None);
        assert_eq!(
            supports,
            vec![
                "opencode.send".to_string(),
                "opencode.interrupt".to_string(),
                "opencode.launch".to_string(),
                "opencode.live_proof".to_string(),
            ]
        );

        write_executable(&dir, "custom-codex");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|name| {
            if name == "LONGHOUSE_CODEX_BIN" {
                Some(dir.join("custom-codex").into_os_string())
            } else {
                None
            }
        });
        assert!(supports.contains(&"codex.launch".to_string()));
        assert!(supports.contains(&"codex.continue".to_string()));
        assert!(!supports.contains(&"codex.live_proof".to_string()));

        write_executable(&dir, "codex");
        write_executable(&dir, "claude");
        write_executable(&dir, "agy");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None);
        assert!(supports.contains(&"codex.launch".to_string()));
        assert!(supports.contains(&"codex.continue".to_string()));
        assert!(supports.contains(&"claude.launch".to_string()));
        assert!(supports.contains(&"opencode.launch".to_string()));
        assert!(supports.contains(&"antigravity.send".to_string()));
        assert!(supports.contains(&"claude.live_proof".to_string()));
        assert!(supports.contains(&"opencode.live_proof".to_string()));
        assert!(supports.contains(&"antigravity.live_proof".to_string()));
        assert!(!supports.contains(&"codex.live_proof".to_string()));
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
    async fn handle_command_frame_routes_claude_send_through_longhouse_cli() {
        let _guard = ENV_LOCK.lock().unwrap();
        let unique = format!(
            "lh-claude-send-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        let args_path = dir.join("args.txt");
        write_test_executable(
            &dir.join("longhouse"),
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$LONGHOUSE_ARGS_OUT\"\nexit 0\n",
        );

        let old_path = std::env::var_os("PATH");
        let old_args_out = std::env::var_os("LONGHOUSE_ARGS_OUT");
        std::env::set_var("PATH", dir.as_os_str());
        std::env::set_var("LONGHOUSE_ARGS_OUT", args_path.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-claude-send",
                "session_id": "session-1",
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"provider": "claude", "text": "hello"},
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
        if let Some(value) = old_args_out {
            std::env::set_var("LONGHOUSE_ARGS_OUT", value);
        } else {
            std::env::remove_var("LONGHOUSE_ARGS_OUT");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "claude");
        assert_eq!(result["result"]["transport"], "claude_channel_bridge");
        let args = std::fs::read_to_string(&args_path).unwrap();
        assert_eq!(
            args.lines().collect::<Vec<_>>(),
            vec![
                "claude-channel",
                "send",
                "--session-id",
                "session-1",
                "--text",
                "hello",
            ]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn handle_command_frame_routes_provider_live_proof_without_session_id() {
        let _guard = ENV_LOCK.lock().unwrap();
        let unique = format!(
            "lh-provider-live-proof-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        let args_path = dir.join("args.txt");
        let stable_path = dir.join("claude.json");
        std::fs::write(
            &stable_path,
            r#"{"artifact_kind":"provider_live_canary","provider":"claude","provider_version":"Claude Code 2.1.153","verdict":"green"}"#,
        )
        .unwrap();
        write_test_executable(
            &dir.join("longhouse"),
            r#"#!/bin/sh
printf '%s\n' "$@" > "$LONGHOUSE_ARGS_OUT"
printf '{"artifact_kind":"provider_live_proof_publish","results":[{"provider":"claude","stable_path":"%s","verdict":"green"}]}\n' "$LONGHOUSE_STABLE_ARTIFACT"
exit 0
"#,
        );

        let old_path = std::env::var_os("PATH");
        let old_args_out = std::env::var_os("LONGHOUSE_ARGS_OUT");
        let old_stable = std::env::var_os("LONGHOUSE_STABLE_ARTIFACT");
        std::env::set_var("PATH", dir.as_os_str());
        std::env::set_var("LONGHOUSE_ARGS_OUT", args_path.as_os_str());
        std::env::set_var("LONGHOUSE_STABLE_ARTIFACT", stable_path.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-provider-live-proof",
                "command_type": COMMAND_PROVIDER_LIVE_PROOF,
                "payload": {
                    "provider": "claude",
                    "expected_provider_version": "2.1.153",
                    "timeout_secs": 30,
                },
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
        if let Some(value) = old_args_out {
            std::env::set_var("LONGHOUSE_ARGS_OUT", value);
        } else {
            std::env::remove_var("LONGHOUSE_ARGS_OUT");
        }
        if let Some(value) = old_stable {
            std::env::set_var("LONGHOUSE_STABLE_ARTIFACT", value);
        } else {
            std::env::remove_var("LONGHOUSE_STABLE_ARTIFACT");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["provider"], "claude");
        assert_eq!(result["result"]["transport"], "provider_live_proof");
        assert_eq!(result["result"]["artifact"]["verdict"], "green");
        assert_eq!(
            result["result"]["provider_version_match"]["status"],
            "match"
        );
        assert_eq!(
            result["result"]["provider_version_match"]["normalized_expected_provider_version"],
            "2.1.153"
        );
        let args = std::fs::read_to_string(&args_path).unwrap();
        assert_eq!(
            args.lines().collect::<Vec<_>>(),
            vec![
                "provider-live",
                "publish",
                "--provider",
                "claude",
                "--json",
            ]
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn provider_live_proof_rejects_expected_version_mismatch() {
        let _guard = ENV_LOCK.lock().unwrap();
        let unique = format!(
            "lh-provider-live-proof-version-mismatch-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        let stable_path = dir.join("claude.json");
        std::fs::write(
            &stable_path,
            r#"{"artifact_kind":"provider_live_canary","provider":"claude","provider_version":"Claude Code 2.1.154","verdict":"green"}"#,
        )
        .unwrap();
        write_test_executable(
            &dir.join("longhouse"),
            r#"#!/bin/sh
printf '{"artifact_kind":"provider_live_proof_publish","results":[{"provider":"claude","stable_path":"%s","verdict":"green"}]}\n' "$LONGHOUSE_STABLE_ARTIFACT"
exit 0
"#,
        );

        let old_path = std::env::var_os("PATH");
        let old_stable = std::env::var_os("LONGHOUSE_STABLE_ARTIFACT");
        std::env::set_var("PATH", dir.as_os_str());
        std::env::set_var("LONGHOUSE_STABLE_ARTIFACT", stable_path.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-provider-live-proof-version-mismatch",
                "command_type": COMMAND_PROVIDER_LIVE_PROOF,
                "payload": {
                    "provider": "claude",
                    "expected_provider_version": "2.1.153",
                },
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
        if let Some(value) = old_stable {
            std::env::set_var("LONGHOUSE_STABLE_ARTIFACT", value);
        } else {
            std::env::remove_var("LONGHOUSE_STABLE_ARTIFACT");
        }

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "provider_version_mismatch");
        assert!(result["error"]["message"]
            .as_str()
            .unwrap()
            .contains("expected 2.1.153"));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn provider_live_proof_returns_valid_red_artifact_as_command_success() {
        let _guard = ENV_LOCK.lock().unwrap();
        let unique = format!(
            "lh-provider-live-proof-red-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).unwrap();
        let stable_path = dir.join("claude.json");
        std::fs::write(
            &stable_path,
            r#"{"artifact_kind":"provider_live_canary","provider":"claude","provider_version":"test","verdict":"red"}"#,
        )
        .unwrap();
        write_test_executable(
            &dir.join("longhouse"),
            r#"#!/bin/sh
printf '{"artifact_kind":"provider_live_proof_publish","results":[{"provider":"claude","stable_path":"%s","verdict":"red"}]}\n' "$LONGHOUSE_STABLE_ARTIFACT"
exit 1
"#,
        );

        let old_path = std::env::var_os("PATH");
        let old_stable = std::env::var_os("LONGHOUSE_STABLE_ARTIFACT");
        std::env::set_var("PATH", dir.as_os_str());
        std::env::set_var("LONGHOUSE_STABLE_ARTIFACT", stable_path.as_os_str());
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-provider-live-proof-red",
                "command_type": COMMAND_PROVIDER_LIVE_PROOF,
                "payload": {"provider": "claude"},
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
        if let Some(value) = old_stable {
            std::env::set_var("LONGHOUSE_STABLE_ARTIFACT", value);
        } else {
            std::env::remove_var("LONGHOUSE_STABLE_ARTIFACT");
        }

        assert_eq!(result["ok"], true);
        assert_eq!(result["result"]["exit_code"], 1);
        assert_eq!(result["result"]["artifact"]["verdict"], "red");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_unproven_provider_steer_paths() {
        let mut cache = command_cache();
        let opencode = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-opencode-steer",
                "session_id": "session-1",
                "command_type": COMMAND_STEER_TEXT,
                "payload": {"provider": "opencode", "text": "change course"},
            }),
            &mut cache,
            &test_config(),
        )
        .await;
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

        assert_eq!(opencode["ok"], false);
        assert_eq!(opencode["error"]["code"], "unsupported_command");
        assert_eq!(antigravity["ok"], false);
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
    async fn launch_rejects_nonexistent_cwd() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-launch-missing-cwd",
                "session_id": "00000000-0000-0000-0000-000000000001",
                "command_type": COMMAND_LAUNCH,
                "payload": {
                    "provider": "codex",
                    "cwd": "/does/not/exist/anywhere-pls",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "cwd_not_found");
    }

    #[tokio::test]
    async fn launch_rejects_relative_cwd() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-launch-relative",
                "session_id": "00000000-0000-0000-0000-000000000002",
                "command_type": COMMAND_LAUNCH,
                "payload": {
                    "provider": "codex",
                    "cwd": "relative/path",
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "cwd_not_allowed");
    }

    #[tokio::test]
    async fn launch_rejects_unsupported_provider() {
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-launch-provider",
                "session_id": "00000000-0000-0000-0000-000000000003",
                "command_type": COMMAND_LAUNCH,
                "payload": {
                    "provider": "gemini",
                    "cwd": "/tmp",
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
    async fn claude_launch_requires_device_token_before_spawning() {
        let mut config = test_config();
        config.api_token = None;
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-launch-claude-no-token",
                "session_id": "00000000-0000-0000-0000-000000000004",
                "command_type": COMMAND_LAUNCH,
                "payload": {
                    "provider": "claude",
                    "cwd": "/tmp",
                },
            }),
            &mut cache,
            &config,
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "provider_launch_failed");
    }

    #[test]
    fn claude_channel_args_route_send_interrupt_and_steer() {
        assert_eq!(
            claude_channel_args(
                COMMAND_SEND_TEXT,
                "11111111-1111-4111-8111-111111111111",
                Some("hello".to_string())
            )
            .unwrap(),
            vec![
                "claude-channel",
                "send",
                "--session-id",
                "11111111-1111-4111-8111-111111111111",
                "--text",
                "hello",
            ]
        );
        assert_eq!(
            claude_channel_args(
                COMMAND_INTERRUPT,
                "11111111-1111-4111-8111-111111111111",
                None
            )
            .unwrap(),
            vec![
                "claude-channel",
                "interrupt",
                "--session-id",
                "11111111-1111-4111-8111-111111111111",
            ]
        );
        assert_eq!(
            claude_channel_args(
                COMMAND_STEER_TEXT,
                "11111111-1111-4111-8111-111111111111",
                Some("course correct".to_string())
            )
            .unwrap(),
            vec![
                "claude-channel",
                "send",
                "--session-id",
                "11111111-1111-4111-8111-111111111111",
                "--text",
                "course correct",
                "--meta",
                "intent=steer",
            ]
        );
    }

    #[test]
    fn opencode_channel_args_route_send_and_interrupt_without_steer() {
        assert_eq!(
            opencode_channel_args(
                COMMAND_SEND_TEXT,
                "11111111-1111-4111-8111-111111111111",
                Some("hello".to_string())
            )
            .unwrap(),
            vec![
                "opencode-channel",
                "send",
                "--session-id",
                "11111111-1111-4111-8111-111111111111",
                "--text",
                "hello",
            ]
        );
        assert_eq!(
            opencode_channel_args(
                COMMAND_INTERRUPT,
                "11111111-1111-4111-8111-111111111111",
                None
            )
            .unwrap(),
            vec![
                "opencode-channel",
                "interrupt",
                "--session-id",
                "11111111-1111-4111-8111-111111111111",
            ]
        );
        assert_eq!(
            opencode_channel_args(
                COMMAND_STEER_TEXT,
                "11111111-1111-4111-8111-111111111111",
                Some("course correct".to_string())
            )
            .unwrap_err()
            .code,
            "unsupported_command"
        );
    }

    #[test]
    fn antigravity_channel_args_route_send_only() {
        assert_eq!(
            antigravity_channel_args(
                COMMAND_SEND_TEXT,
                "11111111-1111-4111-8111-111111111111",
                Some("hello".to_string())
            )
            .unwrap(),
            vec![
                "antigravity-channel",
                "send",
                "--session-id",
                "11111111-1111-4111-8111-111111111111",
                "--text",
                "hello",
            ]
        );
        assert_eq!(
            antigravity_channel_args(
                COMMAND_INTERRUPT,
                "11111111-1111-4111-8111-111111111111",
                None
            )
            .unwrap_err()
            .code,
            "unsupported_command"
        );
        assert_eq!(
            antigravity_channel_args(
                COMMAND_STEER_TEXT,
                "11111111-1111-4111-8111-111111111111",
                Some("course correct".to_string())
            )
            .unwrap_err()
            .code,
            "unsupported_command"
        );
    }
}
