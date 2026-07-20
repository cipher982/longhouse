//! Machine Agent managed-control WebSocket client.

use std::collections::{HashMap, HashSet, VecDeque};
use std::ffi::{OsStr, OsString};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use futures_util::{Sink, SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
use std::process::Stdio;
use tokio::process::Command;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::MissedTickBehavior;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;
use uuid::Uuid;

use crate::build_identity;
use crate::claude_channel_control::{
    interrupt as claude_channel_interrupt, send_text as claude_channel_send_text,
    ClaudeChannelControlError, ClaudeChannelInterruptConfig, ClaudeChannelSendConfig,
};
use crate::claude_channel_launch::{
    launch_detached as launch_detached_claude_channel, ClaudeChannelLaunchConfig,
    ClaudePermissionMode,
};
use crate::codex_bridge::{
    cmd_codex_bridge_interrupt, cmd_codex_bridge_pause_response, cmd_codex_bridge_send,
    cmd_codex_bridge_start, cmd_codex_bridge_steer, validate_codex_bridge_attached,
    BridgeInterruptConfig, BridgeLaunchMode, BridgePauseResponseConfig, BridgeSendConfig,
    BridgeStartConfig, BridgeSteerConfig, BridgeSteerError,
};
use crate::codex_exec::{start_codex_exec_once, CodexExecRunConfig};
use crate::config::ShipperConfig;
use crate::console_prompt::wrap_console_run_once_prompt;
use crate::cursor_print::{start_cursor_print_turn, CursorPrintRunConfig, CURSOR_PRINT_ADAPTER};
use crate::opencode_run::{start_opencode_run_turn, OpenCodeRunConfig, OPENCODE_RUN_ADAPTER};
use crate::turn_claims::{
    default_registry as default_turn_claim_registry, process_start_time_for_pid, ClaimOutcome,
};
#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

const COMMAND_SEND_TEXT: &str = "session.send_text";
const COMMAND_INTERRUPT: &str = "session.interrupt";
const COMMAND_STEER_TEXT: &str = "session.steer_text";
const COMMAND_ANSWER_PAUSE: &str = "session.answer_pause";
const COMMAND_LAUNCH: &str = "session.launch";
const COMMAND_TERMINATE: &str = "session.terminate";
const COMMAND_RUN_ONCE: &str = "session.run_once";
const COMMAND_TURN_START: &str = "session.turn.start";
const COMMAND_TURN_INTERRUPT: &str = "session.turn.interrupt";
const COMMAND_PROVIDER_LIVE_PROOF: &str = "provider.live_proof";
const COMMAND_ARCHIVE_BACKLOG_CONTROL: &str = "archive.backlog_control";
const COMMAND_ARCHIVE_BACKLOG_CONTROL_V2: &str = "archive.backlog_control.v2";
const DEFAULT_CODEX_BIN: &str = "codex";
const DEFAULT_CURSOR_BIN: &str = "cursor-agent";
const DEFAULT_OPENCODE_BIN: &str = "opencode";
const DEFAULT_LONGHOUSE_BIN: &str = "longhouse";
// Remote detached-UI Codex launches run without the user's shell wrapper, so
// the engine owns the managed zero-prompt contract explicitly.
const REMOTE_CODEX_APPROVAL_POLICY: &str = "never";
const REMOTE_CODEX_SANDBOX: &str = "danger-full-access";
const REMOTE_CODEX_EXEC_APPROVAL_POLICY: &str = "never";
const REMOTE_CODEX_EXEC_SANDBOX: &str = "workspace-write";
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

fn remote_codex_bridge_defaults() -> (Option<String>, Option<String>) {
    (
        Some(REMOTE_CODEX_APPROVAL_POLICY.to_string()),
        Some(REMOTE_CODEX_SANDBOX.to_string()),
    )
}

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

pub(crate) fn granted_control_operations(provider: &str, attached: bool) -> Vec<String> {
    if !attached {
        return Vec::new();
    }
    let Some(contract) = managed_provider_contract_items()
        .iter()
        .find(|contract| contract.get("provider").and_then(Value::as_str) == Some(provider))
    else {
        return Vec::new();
    };
    let supports = contract
        .get("machine_control_supports")
        .and_then(Value::as_array);
    let supports_operation = |operation: &str| {
        let expected = format!("{provider}.{operation}");
        supports.is_some_and(|items| items.iter().any(|item| item.as_str() == Some(&expected)))
    };
    let mut granted = Vec::new();
    if supports_operation("interrupt") {
        granted.push("interrupt".to_string());
    }
    if supports_operation("send") {
        granted.push("send_input".to_string());
    }
    if supports_operation("terminate") {
        granted.push("terminate".to_string());
    }
    granted
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
    supports.push(COMMAND_ARCHIVE_BACKLOG_CONTROL.to_string());
    supports.push(COMMAND_ARCHIVE_BACKLOG_CONTROL_V2.to_string());
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
        match crate::cursor_print::recover_cursor_print_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered Cursor Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => tracing::warn!(%error, "Failed to reconcile Cursor Console turn claims"),
        }
        match crate::opencode_run::recover_opencode_run_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered OpenCode Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => {
                tracing::warn!(%error, "Failed to reconcile OpenCode Console turn claims")
            }
        }
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
    let (command_result_tx, mut command_result_rx) = mpsc::unbounded_channel::<(String, Value)>();
    let mut in_flight_commands: HashSet<String> = HashSet::new();

    loop {
        tokio::select! {
            Some((command_id, result)) = command_result_rx.recv() => {
                in_flight_commands.remove(&command_id);
                completed_commands.insert(command_id, result.clone());
                send_control_message(
                    &mut stream,
                    Message::Text(result.to_string()),
                    "machine control command result",
                    status,
                )
                .await?;
            }
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
                let command_id = frame
                    .get("command_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                if command_id.is_empty() {
                    let result = command_error("", "invalid_command", "command_id is required");
                    send_control_message(
                        &mut stream,
                        Message::Text(result.to_string()),
                        "machine control command result",
                        status,
                    )
                    .await?;
                    continue;
                }
                if let Some(result) = completed_commands.get(&command_id) {
                    send_control_message(
                        &mut stream,
                        Message::Text(result.to_string()),
                        "machine control command result",
                        status,
                    )
                    .await?;
                    continue;
                }
                if in_flight_commands.contains(&command_id) {
                    tracing::debug!(command_id, "Ignoring duplicate in-flight machine control command");
                    continue;
                }

                in_flight_commands.insert(command_id.clone());
                let tx = command_result_tx.clone();
                let config = config.clone();
                tokio::spawn(async move {
                    let mut no_cache = CompletedCommandCache::new(0, Duration::ZERO);
                    let result = handle_command_frame(frame, &mut no_cache, &config).await;
                    let _ = tx.send((command_id, result));
                });
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
    if command_type == COMMAND_ARCHIVE_BACKLOG_CONTROL
        || command_type == COMMAND_ARCHIVE_BACKLOG_CONTROL_V2
    {
        return run_archive_backlog_control_command(&payload).await;
    }

    let session_id = required_string(frame, "session_id")?;

    match command_type.as_str() {
        COMMAND_TURN_START => execute_turn_start(frame, &payload, &session_id, config).await,
        COMMAND_TURN_INTERRUPT => {
            let run_id = payload_required_string(&payload, "run_id")?;
            let provider = payload_required_string(&payload, "provider")?;
            let transport = match provider.as_str() {
                "cursor" => {
                    crate::cursor_print::interrupt_cursor_print_turn(&run_id, &session_id)
                        .map_err(CommandError::command_failed)?;
                    CURSOR_PRINT_ADAPTER
                }
                "opencode" => {
                    crate::opencode_run::interrupt_opencode_run_turn(&run_id, &session_id)
                        .map_err(CommandError::command_failed)?;
                    OPENCODE_RUN_ADAPTER
                }
                _ => {
                    return Err(CommandError {
                        code: "provider_unsupported".to_string(),
                        message: format!(
                            "provider={provider} has no turn-scoped interrupt adapter"
                        ),
                    });
                }
            };
            Ok(json!({
                "provider": provider,
                "transport": transport,
                "run_id": run_id,
                "interrupt_requested": true,
            }))
        }
        COMMAND_RUN_ONCE => {
            let provider = payload_required_string(&payload, "provider")?;
            if provider != "codex" {
                return Err(CommandError {
                    code: "provider_unsupported".to_string(),
                    message: format!("provider={provider} is not supported for session.run_once"),
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
            let initial_prompt = payload_required_string(&payload, "initial_prompt")?;
            let run_id = payload_required_string(&payload, "run_id")?;
            let resume_target = payload_resume_target(&payload)?;
            let launch_actor = payload_optional_string(&payload, "launch_actor");
            let launch_surface = payload_optional_string(&payload, "launch_surface");
            let provider_prompt =
                run_once_provider_prompt(&initial_prompt, resume_target.is_some());
            let local_db_path = config
                .db_path
                .clone()
                .or_else(|| crate::config::get_agent_db_path().ok());

            let api_token = config.api_token.clone().ok_or_else(|| CommandError {
                code: "provider_launch_failed".to_string(),
                message: "Machine Agent has no device token configured".to_string(),
            })?;

            let summary = start_codex_exec_once(CodexExecRunConfig {
                session_id: session_id.clone(),
                run_id: run_id.clone(),
                thread_id: None,
                turn_id: None,
                client_request_id: None,
                cwd,
                api_url: config.api_url.clone(),
                api_token,
                codex_bin: DEFAULT_CODEX_BIN.to_string(),
                approval_policy: Some(REMOTE_CODEX_EXEC_APPROVAL_POLICY.to_string()),
                sandbox: Some(REMOTE_CODEX_EXEC_SANDBOX.to_string()),
                prompt: provider_prompt,
                launch_actor,
                launch_surface,
                resume_thread_id: resume_target
                    .as_ref()
                    .map(|target| target.thread_id.clone()),
                machine_name: config.machine_name.clone(),
                local_db_path,
            })
            .await
            .map_err(|err| CommandError {
                code: "provider_launch_failed".to_string(),
                message: err.to_string(),
            })?;

            Ok(json!({
                "session_id": summary.session_id,
                "run_id": summary.run_id,
                "provider": "codex",
                "transport": "codex_app_server",
                "pid": summary.pid,
                "argv": summary.argv,
            }))
        }
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
                // Claude resumes by provider id. No transcript path is needed
                // (unlike codex) because `claude --resume <id>` reads the local store.
                // Preflight (claude-scoped on purpose): if the server passed a
                // transcript path (the adopt-unmanaged continue case), fail
                // honestly with transcript_not_found when it's gone, instead of a
                // generic provider_launch_failed. Codex resume has its own bridge
                // path and is intentionally not changed here.
                if let Some(target) = resume_target.as_ref() {
                    if let Some(path) = target.thread_path.as_ref() {
                        if !path.trim().is_empty() && !std::path::Path::new(path).exists() {
                            return Err(CommandError {
                                code: "transcript_not_found".to_string(),
                                message: format!("resume transcript no longer exists: {path}"),
                            });
                        }
                    }
                }
                let resume_provider_session_id = resume_target
                    .as_ref()
                    .map(|target| target.thread_id.clone());
                let permission_mode = match payload_optional_string(&payload, "permission_mode")
                    .as_deref()
                    .map(str::trim)
                {
                    Some("remote_approve") => ClaudePermissionMode::RemoteApprove,
                    _ => ClaudePermissionMode::Bypass,
                };
                return launch_claude_channel_session(
                    session_id.clone(),
                    cwd,
                    api_url,
                    api_token,
                    resume_provider_session_id,
                    payload_optional_string(&payload, "hook_token"),
                    permission_mode,
                )
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

            let (approval_policy, sandbox) = remote_codex_bridge_defaults();
            let summary = cmd_codex_bridge_start(BridgeStartConfig {
                session_id: session_id.clone(),
                run_id: payload_optional_string(&payload, "run_id"),
                cwd,
                api_url,
                api_token,
                codex_bin: DEFAULT_CODEX_BIN.to_string(),
                approval_policy,
                sandbox,
                model: None,
                model_reasoning_effort: None,
                machine_name: Some(config.machine_name.clone()),
                auto_approve: false,
                hold_user_input_requests: false,
                hold_permission_requests: false,
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
                let summary = claude_channel_send_text(ClaudeChannelSendConfig {
                    session_id: session_id.clone(),
                    text,
                    meta: Vec::new(),
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok(claude_channel_control_result(summary.provider_session_id));
            }
            if provider == "opencode" {
                let summary = crate::opencode_control::send_text(&session_id, &text)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "antigravity" {
                return run_antigravity_channel_command(
                    antigravity_channel_args(COMMAND_SEND_TEXT, &session_id, Some(text))?,
                    LAUNCH_START_TIMEOUT_SECS,
                )
                .await
                .map(|output| cli_output_result(output, "antigravity", "antigravity_hook_inbox"));
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::send_text(&session_id, &text, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
            }
            let attachments = crate::codex_attachments::parse_attachments(&payload)
                .map_err(CommandError::command_failed)?;
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            let summary = cmd_codex_bridge_send(BridgeSendConfig {
                session_id: session_id.clone(),
                text,
                state_root: None,
                allow_direct_ws_fallback: false,
                attachments,
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
                claude_channel_interrupt(ClaudeChannelInterruptConfig {
                    session_id,
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok(claude_channel_control_result(None));
            }
            if provider == "opencode" {
                let summary = crate::opencode_control::interrupt(&session_id)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "antigravity" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: "Antigravity hook inbox does not support remote interrupts"
                        .to_string(),
                });
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::interrupt(&session_id, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
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
        COMMAND_TERMINATE => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| "codex".to_string());
            if provider == "opencode" {
                let summary = crate::opencode_control::stop_server_bridge(&session_id)
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "pid": summary.pid,
                    "stopped": summary.stopped,
                }));
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::terminate(&session_id, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
            }
            Err(CommandError {
                code: "unsupported_command".to_string(),
                message: format!("{provider} terminate is not supported by this Machine Agent"),
            })
        }
        COMMAND_STEER_TEXT => {
            let text = payload_required_string(&payload, "text")?;
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| "codex".to_string());
            if provider == "claude" {
                let summary = claude_channel_send_text(ClaudeChannelSendConfig {
                    session_id: session_id.clone(),
                    text,
                    meta: vec![("intent".to_string(), "steer".to_string())],
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok(claude_channel_control_result(summary.provider_session_id));
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
            let attachments = crate::codex_attachments::parse_attachments(&payload)
                .map_err(CommandError::command_failed)?;
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            match cmd_codex_bridge_steer(BridgeSteerConfig {
                session_id,
                text,
                state_root: None,
                attachments,
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
        COMMAND_ANSWER_PAUSE => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| "codex".to_string());
            if provider == "claude" {
                let request_key = payload_required_string(&payload, "request_key")?;
                let decision = payload_optional_string(&payload, "decision")
                    .unwrap_or_else(|| "answer".to_string());
                let text = claude_pause_response_text(&payload)?;
                let response_text = text.clone();
                let summary = claude_channel_send_text(ClaudeChannelSendConfig {
                    session_id: session_id.clone(),
                    text,
                    meta: vec![
                        ("intent".to_string(), "pause_response".to_string()),
                        ("request_key".to_string(), request_key.clone()),
                        ("decision".to_string(), decision.clone()),
                    ],
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok({
                    let mut result = claude_channel_control_result(summary.provider_session_id);
                    if let Some(obj) = result.as_object_mut() {
                        obj.insert(
                            "pause_response".to_string(),
                            json!({
                                "status": "resolved",
                                "response_text": response_text,
                                "response_payload": {
                                    "decision": decision,
                                    "answers": payload.get("answers").cloned(),
                                    "content": payload.get("content").cloned(),
                                    "message": payload_optional_string(&payload, "message"),
                                }
                            }),
                        );
                    }
                    result
                });
            }
            if provider != "codex" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: format!("{provider} does not support remote pause responses yet"),
                });
            }
            let request_key = payload_required_string(&payload, "request_key")?;
            let decision = payload_optional_string(&payload, "decision")
                .unwrap_or_else(|| "answer".to_string());
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            let response = cmd_codex_bridge_pause_response(BridgePauseResponseConfig {
                session_id,
                state_root: None,
                request_key,
                decision,
                answers: payload.get("answers").cloned(),
                content: payload.get("content").cloned(),
                message: payload_optional_string(&payload, "message"),
            })
            .await
            .map_err(CommandError::command_failed)?;
            Ok(json!({
                "exit_code": 0,
                "stdout": serde_json::to_string(&response).unwrap_or_default(),
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
                "pause_response": response,
            }))
        }
        other => Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("Unsupported command_type={other}"),
        }),
    }
}

async fn execute_turn_start(
    frame: &Value,
    payload: &Value,
    session_id: &str,
    config: &ShipperConfig,
) -> std::result::Result<Value, CommandError> {
    let command_id = required_string(frame, "command_id")?;
    let run_id = payload_required_string(payload, "run_id")?;
    if command_id != run_id {
        return Err(CommandError {
            code: "invalid_command".to_string(),
            message: "session.turn.start command_id must equal run_id".to_string(),
        });
    }
    let thread_id = payload_required_string(payload, "thread_id")?;
    let turn_id = payload_optional_string(payload, "turn_id");
    let client_request_id = payload_optional_string(payload, "client_request_id");
    let command_received_at_ms = chrono::Utc::now().timestamp_millis();
    let server_accepted_at_ms = payload.get("server_accepted_at_ms").and_then(Value::as_i64);
    let server_dispatched_at_ms = payload
        .get("server_dispatched_at_ms")
        .and_then(Value::as_i64);
    eprintln!(
        "[console-turn] latency stage=machine_command_received session={session_id} run={run_id} turn={} request={} accepted_to_machine_ms={} dispatched_to_machine_ms={}",
        turn_id.as_deref().unwrap_or("unknown"),
        client_request_id.as_deref().unwrap_or("unknown"),
        server_accepted_at_ms
            .map(|value| command_received_at_ms.saturating_sub(value))
            .unwrap_or(-1),
        server_dispatched_at_ms
            .map(|value| command_received_at_ms.saturating_sub(value))
            .unwrap_or(-1),
    );
    let provider = payload_required_string(payload, "provider")?;
    if !matches!(provider.as_str(), "codex" | "cursor" | "opencode") {
        return Err(CommandError {
            code: "provider_unsupported".to_string(),
            message: format!("provider={provider} has no Console turn adapter"),
        });
    }
    let cwd_raw = payload_required_string(payload, "cwd")?;
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
    let message = payload_required_string(payload, "message")?;
    let resume_provider_thread_id = payload_optional_string(payload, "resume_provider_thread_id");
    let permission_mode =
        payload_optional_string(payload, "permission_mode").unwrap_or_else(|| {
            if matches!(provider.as_str(), "cursor" | "opencode") {
                "bypass"
            } else {
                "remote_approve"
            }
            .to_string()
        });
    if provider == "opencode" && permission_mode != "bypass" {
        return Err(CommandError {
            code: "permission_mode_unsupported".to_string(),
            message: "OpenCode Console currently supports bypass permission mode only".to_string(),
        });
    }
    if provider == "cursor" && !matches!(permission_mode.as_str(), "bypass" | "auto_approve") {
        return Err(CommandError {
            code: "permission_policy_unsupported".to_string(),
            message: "Cursor Console currently supports auto_approve permission policy only"
                .to_string(),
        });
    }
    let launch_actor = payload_optional_string(payload, "launch_actor");
    let launch_surface = payload_optional_string(payload, "launch_surface");
    let registry = default_turn_claim_registry().map_err(CommandError::command_failed)?;
    let claim_started = std::time::Instant::now();
    match registry
        .claim(
            &run_id,
            session_id,
            &thread_id,
            turn_id.as_deref(),
            client_request_id.as_deref(),
            &provider,
        )
        .map_err(CommandError::command_failed)?
    {
        ClaimOutcome::Existing(claim) if claim.state == "terminal" => {
            return claim.result.ok_or_else(|| CommandError {
                code: "turn_claim_invalid".to_string(),
                message: format!("terminal run {run_id} has no stored result"),
            });
        }
        ClaimOutcome::Existing(claim) if claim.state == "spawned" => {
            let process_is_same = claim
                .pid
                .zip(claim.process_start_time.as_deref())
                .and_then(|(pid, started)| {
                    crate::process_identity::collect_process_facts_by_pid()
                        .get(&pid)
                        .map(|fact| fact.lstart == started)
                })
                .unwrap_or(false);
            if process_is_same {
                return claim.result.ok_or_else(|| CommandError {
                    code: "turn_claim_invalid".to_string(),
                    message: format!("spawned run {run_id} has no stored result"),
                });
            }
            return Err(CommandError {
                code: "turn_start_ambiguous".to_string(),
                message: format!("run {run_id} was spawned but its exact process is gone without a terminal claim"),
            });
        }
        ClaimOutcome::Existing(claim) if claim.state == "failed" => {
            return Err(CommandError {
                code: "provider_launch_failed".to_string(),
                message: claim
                    .error
                    .unwrap_or_else(|| format!("run {run_id} previously failed")),
            });
        }
        ClaimOutcome::Existing(_) => {
            return Err(CommandError {
                code: "turn_start_ambiguous".to_string(),
                message: format!("run {run_id} was claimed but its spawn outcome is not proven"),
            });
        }
        ClaimOutcome::Acquired => {}
    }
    eprintln!(
        "[console-turn] latency stage=machine_claimed session={session_id} run={run_id} turn={} claim_ms={}",
        turn_id.as_deref().unwrap_or("unknown"),
        claim_started.elapsed().as_millis()
    );

    let local_db_path = config
        .db_path
        .clone()
        .or_else(|| crate::config::get_agent_db_path().ok());
    let launch_result = if provider == "cursor" {
        start_cursor_print_turn(CursorPrintRunConfig {
            session_id: session_id.to_string(),
            thread_id: thread_id.clone(),
            turn_id: turn_id.clone(),
            run_id: run_id.clone(),
            client_request_id: client_request_id.clone(),
            cwd,
            cursor_bin: std::env::var("LONGHOUSE_CURSOR_BIN")
                .unwrap_or_else(|_| DEFAULT_CURSOR_BIN.to_string()),
            prompt: message,
            resume_provider_thread_id,
            model: payload_optional_string(payload, "model"),
            permission_mode: permission_mode.clone(),
            machine_name: config.machine_name.clone(),
            local_db_path,
        })
        .await
        .map(|summary| {
            json!({
                "session_id": summary.session_id,
                "thread_id": thread_id,
                "run_id": summary.run_id,
                "provider": "cursor",
                "transport": CURSOR_PRINT_ADAPTER,
                "provider_thread_id": summary.provider_thread_id,
                "launch_id": summary.launch_id,
                "pid": summary.pid,
                "process_group_id": summary.process_group_id,
                "stdout_path": summary.stdout_path,
                "stderr_path": summary.stderr_path,
                "argv": summary.argv,
            })
        })
    } else if provider == "opencode" {
        start_opencode_run_turn(OpenCodeRunConfig {
            session_id: session_id.to_string(),
            thread_id: thread_id.clone(),
            turn_id: turn_id.clone(),
            run_id: run_id.clone(),
            client_request_id: client_request_id.clone(),
            cwd,
            opencode_bin: std::env::var("LONGHOUSE_OPENCODE_BIN")
                .unwrap_or_else(|_| DEFAULT_OPENCODE_BIN.to_string()),
            prompt: message,
            resume_provider_thread_id,
            model: payload_optional_string(payload, "model"),
            permission_mode,
            machine_name: config.machine_name.clone(),
            local_db_path,
        })
        .await
        .map(|summary| {
            json!({
                "session_id": summary.session_id,
                "thread_id": thread_id,
                "run_id": summary.run_id,
                "provider": "opencode",
                "transport": OPENCODE_RUN_ADAPTER,
                "provider_thread_id": summary.provider_thread_id,
                "launch_id": summary.launch_id,
                "pid": summary.pid,
                "process_group_id": summary.process_group_id,
                "stdout_path": summary.stdout_path,
                "stderr_path": summary.stderr_path,
                "argv": summary.argv,
            })
        })
    } else if provider == "codex" {
        let api_token = config
            .api_token
            .clone()
            .ok_or_else(|| anyhow!("Machine Agent has no device token configured"));
        match api_token {
            Ok(api_token) => start_codex_exec_once(CodexExecRunConfig {
                session_id: session_id.to_string(),
                run_id: run_id.clone(),
                thread_id: Some(thread_id.clone()),
                turn_id: turn_id.clone(),
                client_request_id: client_request_id.clone(),
                cwd,
                api_url: config.api_url.clone(),
                api_token,
                codex_bin: DEFAULT_CODEX_BIN.to_string(),
                approval_policy: Some(REMOTE_CODEX_EXEC_APPROVAL_POLICY.to_string()),
                sandbox: Some(REMOTE_CODEX_EXEC_SANDBOX.to_string()),
                prompt: message,
                launch_actor,
                launch_surface,
                resume_thread_id: resume_provider_thread_id,
                machine_name: config.machine_name.clone(),
                local_db_path,
            })
            .await
            .map(|summary| {
                json!({
                    "session_id": summary.session_id,
                    "thread_id": thread_id,
                    "run_id": summary.run_id,
                    "provider": "codex",
                    "transport": "codex_app_server",
                    "pid": summary.pid,
                    "argv": summary.argv,
                })
            }),
            Err(err) => Err(err),
        }
    } else {
        Err(anyhow!("provider={provider} has no Console turn adapter"))
    };

    match launch_result {
        Ok(result) => {
            if matches!(
                result.get("transport").and_then(Value::as_str),
                Some(CURSOR_PRINT_ADAPTER | OPENCODE_RUN_ADAPTER)
            ) {
                return Ok(result);
            }
            let pid = result
                .get("pid")
                .and_then(Value::as_u64)
                .map(|value| value as u32);
            registry
                .mark_spawned(
                    &run_id,
                    pid,
                    process_start_time_for_pid(pid),
                    result.clone(),
                )
                .map_err(|err| CommandError {
                    code: "turn_claim_update_failed".to_string(),
                    message: err.to_string(),
                })?;
            Ok(result)
        }
        Err(err) => {
            let message = err.to_string();
            let _ = registry.mark_failed(&run_id, &message);
            Err(CommandError {
                code: "provider_launch_failed".to_string(),
                message,
            })
        }
    }
}

async fn run_archive_backlog_control_command(
    payload: &Value,
) -> std::result::Result<Value, CommandError> {
    let mode = payload_required_string(payload, "mode")?;
    let normalized_mode = match mode.trim().to_ascii_lowercase().as_str() {
        "paused" | "pause" => "paused",
        "trickle" | "resume" => "trickle",
        "drain" | "drain-now" => "drain",
        other => {
            return Err(CommandError {
                code: "archive_control_invalid_mode".to_string(),
                message: format!("unsupported archive repair mode {other}"),
            });
        }
    };
    let mut control = json!({
        "mode": normalized_mode,
        "updated_at": timestamp_now(),
        "actor": payload.get("actor").and_then(Value::as_str).unwrap_or("machine_api"),
    });
    if let Some(reason) = payload
        .get("reason")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|reason| !reason.is_empty())
    {
        control["reason"] = json!(reason);
    }
    if normalized_mode != "paused" {
        let lease_seconds = payload
            .get("lease_seconds")
            .and_then(Value::as_u64)
            .unwrap_or(3600)
            .clamp(60, 86_400);
        control["expires_at"] = json!((chrono::Utc::now()
            + chrono::Duration::seconds(lease_seconds as i64))
        .to_rfc3339());
    }
    if let Some(max_tick_bytes) = payload.get("max_tick_bytes").and_then(Value::as_u64) {
        control["max_tick_bytes"] = json!(max_tick_bytes);
    }
    if let Some(include_huge) = payload.get("include_huge").and_then(Value::as_bool) {
        control["include_huge"] = json!(include_huge);
    }

    let path =
        crate::config::get_agent_archive_repair_control_path().map_err(|err| CommandError {
            code: "archive_control_write_failed".to_string(),
            message: err.to_string(),
        })?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| CommandError {
            code: "archive_control_write_failed".to_string(),
            message: err.to_string(),
        })?;
    }
    let bytes = serde_json::to_vec_pretty(&control).map_err(CommandError::command_failed)?;
    std::fs::write(&path, bytes).map_err(|err| CommandError {
        code: "archive_control_write_failed".to_string(),
        message: err.to_string(),
    })?;

    Ok(json!({
        "mode": normalized_mode,
        "path": path.to_string_lossy(),
    }))
}

fn claude_pause_response_text(payload: &Value) -> std::result::Result<String, CommandError> {
    let decision = payload_optional_string(payload, "decision")
        .unwrap_or_else(|| "answer".to_string())
        .to_ascii_lowercase();
    if let Some(message) = payload_optional_string(payload, "message") {
        if !message.trim().is_empty() {
            return Ok(message);
        }
    }
    if let Some(content) = payload.get("content") {
        if let Some(text) = content.as_str() {
            if !text.trim().is_empty() {
                return Ok(text.trim().to_string());
            }
        } else if !content.is_null() {
            let text = content.to_string();
            if !text.trim().is_empty() {
                return Ok(text.trim().to_string());
            }
        }
    }
    if let Some(answers) = payload.get("answers").and_then(Value::as_object) {
        let mut entries: Vec<_> = answers.iter().collect();
        entries.sort_by(|(left, _), (right, _)| left.cmp(right));
        let parts: Vec<String> = entries
            .into_iter()
            .filter_map(|(key, value)| {
                let label = key.trim();
                if label.is_empty() {
                    return None;
                }
                let values = claude_pause_answer_values(value);
                if values.is_empty() {
                    return None;
                }
                Some(format!("{label}: {}", values.join(", ")))
            })
            .collect();
        if !parts.is_empty() {
            return Ok(parts.join("; "));
        }
    }
    if decision == "cancel" || decision == "reject" {
        return Ok("Cancelled in Longhouse.".to_string());
    }
    Err(CommandError {
        code: "invalid_command".to_string(),
        message: "Claude pause responses require a non-empty answer message".to_string(),
    })
}

fn claude_channel_control_result(provider_session_id: Option<String>) -> Value {
    let mut result = json!({
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "provider": "claude",
        "transport": "claude_channel_bridge",
    });
    if let Some(provider_session_id) = provider_session_id {
        if !provider_session_id.trim().is_empty() {
            if let Some(obj) = result.as_object_mut() {
                obj.insert(
                    "provider_session_id".to_string(),
                    json!(provider_session_id),
                );
            }
        }
    }
    result
}

fn claude_channel_error_to_command_error(err: ClaudeChannelControlError) -> CommandError {
    match err {
        ClaudeChannelControlError::SessionNotAttached { message, .. } => {
            CommandError::session_not_attached(anyhow!(message))
        }
        ClaudeChannelControlError::CommandFailed(message) => {
            CommandError::command_failed(anyhow!(message))
        }
    }
}

fn claude_pause_answer_values(value: &Value) -> Vec<String> {
    match value {
        Value::Null => Vec::new(),
        Value::Array(items) => items
            .iter()
            .filter_map(|item| {
                let text = match item {
                    Value::Null => return None,
                    Value::String(text) => text.trim().to_string(),
                    other => other.to_string(),
                };
                if text.trim().is_empty() {
                    None
                } else {
                    Some(text)
                }
            })
            .collect(),
        Value::String(text) => {
            let text = text.trim();
            if text.is_empty() {
                Vec::new()
            } else {
                vec![text.to_string()]
            }
        }
        other => vec![other.to_string()],
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
    let run_live_token_contract =
        payload_optional_bool(payload, "run_live_token_contract").unwrap_or(false);
    let live_token_timeout_secs = payload_optional_u64(payload, "live_token_timeout_secs", 1, 600);
    let expected_provider_version = payload_optional_string(payload, "expected_provider_version");

    let mut args = vec![
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
    if run_live_token_contract {
        args.push("--run-live-token-contract".to_string());
        if let Some(value) = live_token_timeout_secs {
            args.push("--live-token-timeout-secs".to_string());
            args.push(value.to_string());
        }
    }

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
    resume_provider_session_id: Option<String>,
    hook_token: Option<String>,
    permission_mode: ClaudePermissionMode,
) -> std::result::Result<Value, CommandError> {
    let resume = resume_provider_session_id.is_some();
    let provider_session_id =
        resume_provider_session_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    let summary = launch_detached_claude_channel(ClaudeChannelLaunchConfig {
        session_id: session_id.clone(),
        provider_session_id: provider_session_id.clone(),
        cwd,
        api_url,
        api_token,
        hook_token,
        resume,
        wait_ready: Duration::from_secs(LAUNCH_START_TIMEOUT_SECS),
        claude_bin: "claude".to_string(),
        permission_mode,
        state_root: None,
        claude_dir: None,
        log_dir: None,
        script_bin: "script".to_string(),
    })
    .await
    .map_err(|err| CommandError {
        code: "provider_launch_failed".to_string(),
        message: err.to_string(),
    })?;
    Ok(json!({
        "session_id": summary.session_id,
        "provider": "claude",
        "transport": "claude_channel_bridge",
        "provider_session_id": summary.provider_session_id,
        // Echo thread_id so server-side late reconciliation can attach the new
        // run even when the synchronous response is lost. For claude the thread
        // id is the provider session id.
        "thread_id": summary.provider_session_id,
        "pid": summary.pid,
        "log_path": summary.log_path.display().to_string(),
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
    let summary = crate::opencode_control::launch_server_bridge(
        crate::opencode_control::OpenCodeLaunchConfig {
            session_id: session_id.clone(),
            cwd,
            api_url,
            api_token,
            device_id: machine_name,
            display_name,
            wait_ready: Duration::from_secs(LAUNCH_START_TIMEOUT_SECS),
            config_dir: None,
            opencode_bin: None,
            opencode_config_content: std::env::var("OPENCODE_CONFIG_CONTENT").ok(),
        },
    )
    .await
    .map_err(|err| CommandError {
        code: "provider_launch_failed".to_string(),
        message: err.to_string(),
    })?;
    Ok(json!({
        "session_id": summary.session_id,
        "provider": "opencode",
        "transport": "opencode_server_bridge",
        "provider_session_id": summary.provider_session_id,
        "thread_id": summary.provider_session_id,
        "server_url": summary.server_url,
        "pid": summary.pid,
        "log_path": summary.log_path,
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

fn run_once_provider_prompt(user_prompt: &str, is_resume: bool) -> String {
    if is_resume {
        user_prompt.to_string()
    } else {
        wrap_console_run_once_prompt(user_prompt)
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
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    static ENV_LOCK: Mutex<()> = Mutex::new(());
    const OPENCODE_TEST_SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

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

    const ENGINE_DISPATCH_SUPPORTS: &[(&str, &str, &str)] = &[
        ("codex", "send", COMMAND_SEND_TEXT),
        ("codex", "interrupt", COMMAND_INTERRUPT),
        ("codex", "steer", COMMAND_STEER_TEXT),
        ("codex", "answer_pause", COMMAND_ANSWER_PAUSE),
        ("codex", "launch", COMMAND_LAUNCH),
        ("codex", "continue", COMMAND_LAUNCH),
        ("codex", "run_once", COMMAND_RUN_ONCE),
        ("codex", "resume_run_once", COMMAND_RUN_ONCE),
        ("codex", "turn_start", COMMAND_TURN_START),
        ("cursor", "run_once", COMMAND_RUN_ONCE),
        ("cursor", "turn_start", COMMAND_TURN_START),
        ("cursor", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("opencode", "turn_start", COMMAND_TURN_START),
        ("opencode", "turn_interrupt", COMMAND_TURN_INTERRUPT),
        ("cursor", "send", COMMAND_SEND_TEXT),
        ("cursor", "interrupt", COMMAND_INTERRUPT),
        ("cursor", "terminate", COMMAND_TERMINATE),
        ("claude", "send", COMMAND_SEND_TEXT),
        ("claude", "interrupt", COMMAND_INTERRUPT),
        ("claude", "steer", COMMAND_STEER_TEXT),
        ("claude", "answer_pause", COMMAND_ANSWER_PAUSE),
        ("claude", "launch", COMMAND_LAUNCH),
        ("claude", "continue", COMMAND_LAUNCH),
        ("opencode", "send", COMMAND_SEND_TEXT),
        ("opencode", "interrupt", COMMAND_INTERRUPT),
        ("opencode", "launch", COMMAND_LAUNCH),
        ("opencode", "terminate", COMMAND_TERMINATE),
        ("antigravity", "send", COMMAND_SEND_TEXT),
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

    async fn spawn_opencode_launch_http_server(provider_session_id: &'static str) -> String {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
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
                let body = if request.target.starts_with("/global/health") {
                    r#"{"healthy":true}"#.to_string()
                } else if request.target.starts_with("/session") {
                    format!(r#"{{"id":"{provider_session_id}"}}"#)
                } else {
                    r#"{"error":"missing"}"#.to_string()
                };
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).await.unwrap();
            }
        });
        format!("http://{addr}")
    }

    fn write_fake_opencode_launch_binary(
        dir: &Path,
        server_url: &str,
        count_path: &Path,
    ) -> PathBuf {
        let path = dir.join("opencode");
        write_test_executable(
            &path,
            &format!(
                "#!/bin/sh\n\
                 echo spawn >> {count}\n\
                 echo {listen}\n\
                 while :; do sleep 60; done\n",
                count = shell_quote_path(count_path),
                listen = shell_quote(&format!("opencode server listening on {server_url}")),
            ),
        );
        path
    }

    fn shell_quote(value: &str) -> String {
        format!("'{}'", value.replace('\'', "'\\''"))
    }

    fn shell_quote_path(path: &Path) -> String {
        shell_quote(&path.display().to_string())
    }

    fn terminate_test_pid(pid: u32) {
        if pid == 0 || pid > i32::MAX as u32 {
            return;
        }
        let pid = pid as i32;
        #[cfg(unix)]
        unsafe {
            libc::killpg(pid, libc::SIGTERM);
        }
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
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
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("codex.run_once")));
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("codex.resume_run_once")));
    }

    #[test]
    fn control_channel_advertises_claude_continue() {
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
            .any(|item| item.as_str() == Some("claude.continue")));
        assert!(supports
            .iter()
            .any(|item| item.as_str() == Some("claude.answer_pause")));
        assert_eq!(
            claude_contract.get("can_resume").and_then(Value::as_bool),
            Some(true)
        );
    }

    #[test]
    fn remote_codex_launch_uses_zero_prompt_managed_defaults() {
        let (approval_policy, sandbox) = remote_codex_bridge_defaults();

        assert_eq!(approval_policy.as_deref(), Some("never"));
        assert_eq!(sandbox.as_deref(), Some("danger-full-access"));
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
    fn run_once_provider_prompt_leaves_resume_turns_unwrapped() {
        let user_prompt = "Continue with the next fix.";
        assert_eq!(run_once_provider_prompt(user_prompt, true), user_prompt);
    }

    #[test]
    fn run_once_provider_prompt_wraps_fresh_console_turns() {
        let prompt = run_once_provider_prompt("Start the task.", false);

        assert!(prompt.starts_with("Longhouse Console runtime note:"));
        assert!(prompt.ends_with("User message:\nStart the task."));
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

    #[test]
    fn reducer_control_grants_follow_dispatch_manifest_and_connection_state() {
        assert_eq!(
            granted_control_operations("cursor", true),
            vec![
                "interrupt".to_string(),
                "send_input".to_string(),
                "terminate".to_string()
            ]
        );
        assert_eq!(
            granted_control_operations("antigravity", true),
            vec!["send_input".to_string()]
        );
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
    fn unsupported_engine_dispatch_paths_stay_unadvertised() {
        let supports = manifest_machine_control_supports();
        for (provider, operation) in [
            ("opencode", "steer"),
            ("opencode", "answer_pause"),
            ("opencode", "run_once"),
            ("opencode", "resume_run_once"),
            ("antigravity", "interrupt"),
            ("antigravity", "steer"),
            ("antigravity", "answer_pause"),
            ("antigravity", "launch"),
            ("claude", "run_once"),
            ("claude", "resume_run_once"),
            ("codex", "terminate"),
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
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None);
        assert_eq!(
            supports,
            vec![
                "archive.backlog_control".to_string(),
                "archive.backlog_control.v2".to_string(),
                "opencode.send".to_string(),
                "opencode.interrupt".to_string(),
                "opencode.launch".to_string(),
                "opencode.terminate".to_string(),
                "opencode.turn_start".to_string(),
                "opencode.turn_interrupt".to_string(),
            ]
        );

        write_executable(&dir, "longhouse");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None);
        assert_eq!(
            supports,
            vec![
                "archive.backlog_control".to_string(),
                "archive.backlog_control.v2".to_string(),
                "opencode.send".to_string(),
                "opencode.interrupt".to_string(),
                "opencode.launch".to_string(),
                "opencode.terminate".to_string(),
                "opencode.turn_start".to_string(),
                "opencode.turn_interrupt".to_string(),
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
        assert!(supports.contains(&"codex.run_once".to_string()));
        assert!(supports.contains(&"codex.resume_run_once".to_string()));
        assert!(!supports.contains(&"codex.live_proof".to_string()));

        write_executable(&dir, "codex");
        write_executable(&dir, "claude");
        write_executable(&dir, "agy");
        write_executable(&dir, "cursor-agent");
        let supports = control_supports_for_path_with_env(Some(dir.as_os_str()), &|_| None);
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
            if provider_live_proof_supported_provider(provider) {
                expected.push(format!("{provider}.live_proof"));
            }
        }
        assert_eq!(supports, expected);
        assert!(supports.contains(&"codex.launch".to_string()));
        assert!(supports.contains(&"codex.continue".to_string()));
        assert!(supports.contains(&"codex.run_once".to_string()));
        assert!(supports.contains(&"codex.resume_run_once".to_string()));
        assert!(supports.contains(&"claude.launch".to_string()));
        // claude.continue must survive the installed-binary gating path, not
        // just exist in the raw manifest — this is the surface the server's
        // `claude.continue in info.supports` check actually reads.
        assert!(supports.contains(&"claude.continue".to_string()));
        assert!(supports.contains(&"opencode.launch".to_string()));
        assert!(supports.contains(&"opencode.terminate".to_string()));
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
        let _guard = ENV_LOCK.lock().unwrap();
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
    async fn handle_command_frame_routes_antigravity_send_through_longhouse_cli() {
        let _guard = ENV_LOCK.lock().unwrap();
        let unique = format!(
            "lh-antigravity-send-{}-{}",
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
                "command_id": "cmd-antigravity-send",
                "session_id": "session-1",
                "command_type": COMMAND_SEND_TEXT,
                "payload": {"provider": "antigravity", "text": "hello"},
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
        assert_eq!(result["result"]["provider"], "antigravity");
        assert_eq!(result["result"]["transport"], "antigravity_hook_inbox");
        let args = std::fs::read_to_string(&args_path).unwrap();
        assert_eq!(
            args.lines().collect::<Vec<_>>(),
            vec![
                "antigravity-channel",
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
    async fn handle_command_frame_routes_opencode_send_through_native_control() {
        let _guard = ENV_LOCK.lock().unwrap();
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
                "noReply": true,
                "parts": [{"type": "text", "text": "hello native"}],
            })
        );
    }

    #[tokio::test]
    async fn handle_command_frame_routes_opencode_interrupt_through_native_control() {
        let _guard = ENV_LOCK.lock().unwrap();
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
        let _guard = ENV_LOCK.lock().unwrap();
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
                    "run_live_token_contract": true,
                    "live_token_timeout_secs": 25,
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
                "--run-live-token-contract",
                "--live-token-timeout-secs",
                "25",
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
    async fn claude_continue_rejects_missing_resume_transcript() {
        // Adopt-unmanaged continue carries a transcript path; if it's gone we
        // must fail with transcript_not_found, not a generic launch failure.
        let tmp = std::env::temp_dir();
        let mut cache = command_cache();
        let result = handle_command_frame(
            json!({
                "type": "command",
                "command_id": "cmd-claude-missing-transcript",
                "session_id": "00000000-0000-0000-0000-000000000003",
                "command_type": COMMAND_LAUNCH,
                "payload": {
                    "provider": "claude",
                    "cwd": tmp.to_string_lossy(),
                    "mode": "continue",
                    "resume": {
                        "thread_id": "raw-provider-id",
                        "thread_path": "/does/not/exist/raw-transcript.jsonl",
                    },
                },
            }),
            &mut cache,
            &test_config(),
        )
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "transcript_not_found");
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
                    "provider": "antigravity",
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
    fn opencode_launch_routes_through_native_adapter() {
        let _guard = ENV_LOCK.lock().unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let temp = tempfile::TempDir::new().unwrap();
        let cwd = temp.path().join("project");
        std::fs::create_dir(&cwd).unwrap();
        let server_url = runtime.block_on(spawn_opencode_launch_http_server("ses_control"));
        let count_path = temp.path().join("spawn-count.txt");
        let bin_dir = temp.path().join("bin");
        std::fs::create_dir(&bin_dir).unwrap();
        let fake_bin = write_fake_opencode_launch_binary(&bin_dir, &server_url, &count_path);

        let vars = vec![
            (
                "LONGHOUSE_OPENCODE_BIN".to_string(),
                Some(fake_bin.display().to_string()),
            ),
            (
                "CLAUDE_CONFIG_DIR".to_string(),
                Some(temp.path().display().to_string()),
            ),
            ("OPENCODE_CONFIG_CONTENT".to_string(), None),
        ];
        let payload = temp_env::with_vars(vars, || {
            runtime.block_on(launch_opencode_server_session(
                OPENCODE_TEST_SESSION_ID.to_string(),
                cwd.clone(),
                "https://longhouse.test".to_string(),
                "zdt_test_token".to_string(),
                "test-machine".to_string(),
                Some("Control Launch".to_string()),
            ))
        })
        .unwrap();

        assert_eq!(
            payload["session_id"].as_str(),
            Some(OPENCODE_TEST_SESSION_ID)
        );
        assert_eq!(payload["provider"].as_str(), Some("opencode"));
        assert_eq!(
            payload["transport"].as_str(),
            Some("opencode_server_bridge")
        );
        assert_eq!(payload["provider_session_id"].as_str(), Some("ses_control"));
        assert_eq!(payload["thread_id"].as_str(), Some("ses_control"));
        assert_eq!(payload["server_url"].as_str(), Some(server_url.as_str()));
        assert_eq!(std::fs::read_to_string(&count_path).unwrap(), "spawn\n");

        if let Some(pid) = payload["pid"].as_u64() {
            terminate_test_pid(pid as u32);
        }
    }

    #[test]
    fn opencode_console_turn_start_uses_stock_run_and_resumes_native_session() {
        let _guard = ENV_LOCK.lock().unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let temp = tempfile::TempDir::new().unwrap();
        let workspace = temp.path().join("workspace");
        std::fs::create_dir(&workspace).unwrap();
        let fake = temp.path().join("opencode");
        write_test_executable(
            &fake,
            "#!/bin/sh\nprintf '%s\\n' '{\"type\":\"text\",\"sessionID\":\"ses_console_test\",\"part\":{\"type\":\"text\",\"text\":\"done\"}}'\n",
        );
        let longhouse_home = temp.path().join("longhouse");
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();

        let vars = vec![
            (
                "LONGHOUSE_HOME".to_string(),
                Some(longhouse_home.display().to_string()),
            ),
            (
                "LONGHOUSE_OPENCODE_BIN".to_string(),
                Some(fake.display().to_string()),
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
                let argv = response["result"]["argv"].as_array().unwrap();
                assert!(argv.iter().any(|value| value == "--auto"));
                assert!(!argv.iter().any(|value| {
                    matches!(
                        value.as_str(),
                        Some("--continue" | "--attach" | "--dangerously-skip-permissions")
                    )
                }));
                let deadline = std::time::Instant::now() + Duration::from_secs(5);
                loop {
                    let claim = crate::turn_claims::default_registry()
                        .unwrap()
                        .read(&run_id)
                        .unwrap();
                    if claim.state == "terminal" {
                        assert_eq!(
                            claim.provider_thread_id.as_deref(),
                            Some("ses_console_test")
                        );
                        assert_eq!(claim.result.unwrap()["terminal_state"], "run_completed");
                        break;
                    }
                    assert!(
                        std::time::Instant::now() < deadline,
                        "OpenCode fake turn timed out"
                    );
                    runtime.block_on(tokio::time::sleep(Duration::from_millis(20)));
                }
                response
            };

            let first = run_turn(None);
            assert!(!first["result"]["argv"]
                .as_array()
                .unwrap()
                .iter()
                .any(|value| value == "--session"));
            let second = run_turn(Some("ses_console_test"));
            let argv = second["result"]["argv"].as_array().unwrap();
            let session_flag = argv.iter().position(|value| value == "--session").unwrap();
            assert_eq!(argv[session_flag + 1], "ses_console_test");
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
