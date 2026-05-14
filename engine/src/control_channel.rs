//! Machine Agent managed-control WebSocket client.

use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
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
    BridgeSendConfig, BridgeStartConfig, BridgeSteerConfig, BridgeSteerError,
};
use crate::config::ShipperConfig;
use std::path::PathBuf;

const COMMAND_SEND_TEXT: &str = "session.send_text";
const COMMAND_INTERRUPT: &str = "session.interrupt";
const COMMAND_STEER_TEXT: &str = "session.steer_text";
const COMMAND_LAUNCH: &str = "session.launch";
const DEFAULT_CODEX_BIN: &str = "codex";
const LAUNCH_START_TIMEOUT_SECS: u64 = 45;
const COMPLETED_COMMAND_CACHE_CAPACITY: usize = 256;
const COMPLETED_COMMAND_CACHE_TTL_SECS: u64 = 5 * 60;
const HEARTBEAT_INTERVAL_SECS: u64 = 25;
const CONTROL_CONNECT_TIMEOUT_SECS: u64 = 15;
const CONTROL_SUPPORTS: [&str; 4] = [
    "codex.send",
    "codex.interrupt",
    "codex.steer",
    "codex.launch",
];

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
            supports: if inner.enabled {
                CONTROL_SUPPORTS
                    .iter()
                    .map(|item| item.to_string())
                    .collect()
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
}

fn timestamp_now() -> String {
    chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
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
    let mut completed_commands = CompletedCommandCache::new(
        COMPLETED_COMMAND_CACHE_CAPACITY,
        Duration::from_secs(COMPLETED_COMMAND_CACHE_TTL_SECS),
    );
    loop {
        match run_once(&config, &mut completed_commands, &status).await {
            Ok(()) => {
                tracing::info!("Machine control channel disconnected");
                status.set_disconnected(None, None, None, Some(backoff.as_secs()));
                backoff = Duration::from_secs(1);
                last_error = None;
            }
            Err(err) => {
                let error_chain = format_error_chain(&err);
                status.set_disconnected(
                    None,
                    Some("connect_failed"),
                    Some(error_chain.as_str()),
                    Some(backoff.as_secs()),
                );
                if last_error.as_deref() == Some(error_chain.as_str()) {
                    tracing::debug!(error = %error_chain, "Machine control channel connection failed");
                } else {
                    tracing::warn!(error = %error_chain, "Machine control channel connection failed");
                    last_error = Some(error_chain);
                }
            }
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
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
        "supports": CONTROL_SUPPORTS,
    });
    stream
        .send(Message::Text(hello.to_string()))
        .await
        .context("sending machine control hello")?;
    status.set_connected(&ws_url);
    tracing::info!("Machine control channel connected to {ws_url}");

    let mut heartbeat = tokio::time::interval(Duration::from_secs(HEARTBEAT_INTERVAL_SECS));
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    heartbeat.tick().await;

    loop {
        tokio::select! {
            _ = heartbeat.tick() => {
                stream
                    .send(Message::Text(heartbeat_frame().to_string()))
                    .await
                    .context("sending machine control heartbeat")?;
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
                        stream
                            .send(Message::Pong(payload))
                            .await
                            .context("sending machine control pong")?;
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
                stream
                    .send(Message::Text(result.to_string()))
                    .await
                    .context("sending machine control command result")?;
            }
        }
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
    let session_id = required_string(frame, "session_id")?;
    let command_type = required_string(frame, "command_type")?;
    let payload = frame.get("payload").cloned().unwrap_or_else(|| json!({}));

    match command_type.as_str() {
        COMMAND_LAUNCH => {
            let provider = payload_required_string(&payload, "provider")?;
            if provider != "codex" {
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
            if !cwd_under_allowed_roots(&cwd) {
                return Err(CommandError {
                    code: "cwd_not_allowed".to_string(),
                    message: format!(
                        "cwd {} is outside the Machine Agent's launch allowlist",
                        cwd.display()
                    ),
                });
            }

            let api_url = config.api_url.clone();
            let api_token = config.api_token.clone().ok_or_else(|| CommandError {
                code: "provider_launch_failed".to_string(),
                message: "Machine Agent has no device token configured".to_string(),
            })?;

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
                // Headless: there is no TUI to create a thread, so we ask the
                // bridge to call thread/start itself.
                start_thread: true,
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
            }))
        }
        COMMAND_SEND_TEXT => {
            let text = payload_required_string(&payload, "text")?;
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            let summary = cmd_codex_bridge_send(BridgeSendConfig {
                session_id: session_id.clone(),
                text,
                state_root: None,
                allow_direct_ws_fallback: false,
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
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            match cmd_codex_bridge_steer(BridgeSteerConfig {
                session_id,
                text,
                state_root: None,
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

/// Local policy for launch cwds.
///
/// A cwd is allowed when all of:
///   1. Canonical path lives under `$HOME` (or an explicit prefix listed in
///      `~/.longhouse/launch-allowlist`, one per line).
///   2. Canonical path contains a `.git` entry (file or directory) — i.e. it is
///      the root of a repo, or a subtree inside one. This keeps cwd to places
///      the user meaningfully uses for code work, instead of letting any path
///      under `$HOME` be a launch target.
///
/// Fail closed when `$HOME` is unresolvable.
fn cwd_under_allowed_roots(cwd: &std::path::Path) -> bool {
    let canonical = match cwd.canonicalize() {
        Ok(path) => path,
        Err(_) => return false,
    };
    let home = std::env::var("HOME").ok().map(PathBuf::from);
    let mut prefixes: Vec<PathBuf> = Vec::new();
    if let Some(home) = home.as_ref() {
        if let Ok(canon_home) = home.canonicalize() {
            prefixes.push(canon_home);
        }
        let override_file = home.join(".longhouse").join("launch-allowlist");
        if let Ok(contents) = std::fs::read_to_string(&override_file) {
            for line in contents.lines() {
                let trimmed = line.trim();
                if trimmed.is_empty() || trimmed.starts_with('#') {
                    continue;
                }
                let expanded = PathBuf::from(trimmed);
                if let Ok(canon) = expanded.canonicalize() {
                    prefixes.push(canon);
                }
            }
        }
    }
    if prefixes.is_empty() {
        return false;
    }
    let under_prefix = prefixes.iter().any(|root| canonical.starts_with(root));
    if !under_prefix {
        return false;
    }
    // Walk up the tree looking for a .git marker. Stops at the first allowlist
    // prefix boundary so we don't leak above the allowed root.
    let mut cursor: Option<&std::path::Path> = Some(&canonical);
    while let Some(dir) = cursor {
        if dir.join(".git").exists() {
            return true;
        }
        if prefixes.iter().any(|root| dir == root.as_path()) {
            return false;
        }
        cursor = dir.parent();
    }
    false
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
    fn control_ws_url_converts_http_and_https() {
        assert_eq!(
            control_ws_url("http://localhost:8000").unwrap(),
            "ws://localhost:8000/api/agents/control/ws"
        );
        assert_eq!(
            control_ws_url("https://david010.longhouse.ai/").unwrap(),
            "wss://david010.longhouse.ai/api/agents/control/ws"
        );
    }

    #[test]
    fn heartbeat_frame_uses_server_schema() {
        assert_eq!(heartbeat_frame(), json!({"type": "heartbeat"}));
    }

    #[test]
    fn control_channel_status_tracks_connection_state() {
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
        assert!(disconnected.supports.contains(&"codex.launch".to_string()));

        status.set_connected("wss://example.test/api/agents/control/ws");
        let connected = status.snapshot();
        assert_eq!(connected.status, "connected");
        assert_eq!(connected.last_error_code, None);
        assert_eq!(connected.reconnect_backoff_seconds, None);
        assert!(connected.last_connected_at.is_some());
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
                    "provider": "claude",
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

    #[test]
    fn cwd_under_allowed_roots_rejects_outside_home() {
        // /tmp is reliably not under $HOME on macOS/Linux
        let tmp = std::path::Path::new("/tmp");
        if tmp.exists() {
            assert!(!cwd_under_allowed_roots(tmp));
        }
    }

    #[test]
    fn cwd_under_allowed_roots_requires_git_marker() {
        use std::fs;
        let tmp = tempfile::tempdir().expect("tempdir");
        // Point $HOME at our sandbox so the policy check exercises the marker rule.
        let old_home = std::env::var("HOME").ok();
        std::env::set_var("HOME", tmp.path());

        let no_git = tmp.path().join("project-no-git");
        fs::create_dir_all(&no_git).unwrap();
        assert!(!cwd_under_allowed_roots(&no_git));

        let git_root = tmp.path().join("project-with-git");
        fs::create_dir_all(git_root.join(".git")).unwrap();
        assert!(cwd_under_allowed_roots(&git_root));

        let nested = git_root.join("subdir");
        fs::create_dir_all(&nested).unwrap();
        assert!(cwd_under_allowed_roots(&nested));

        if let Some(value) = old_home {
            std::env::set_var("HOME", value);
        } else {
            std::env::remove_var("HOME");
        }
    }
}
