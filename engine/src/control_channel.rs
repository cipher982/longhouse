//! Machine Agent managed-control WebSocket client.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::http::HeaderValue;
use tokio_tungstenite::tungstenite::Message;

use crate::build_identity;
use crate::codex_bridge::{
    cmd_codex_bridge_interrupt, cmd_codex_bridge_send, cmd_codex_bridge_steer,
    validate_codex_bridge_attached, BridgeInterruptConfig, BridgeSendConfig, BridgeSteerConfig,
    BridgeSteerError,
};
use crate::config::ShipperConfig;

const COMMAND_SEND_TEXT: &str = "session.send_text";
const COMMAND_INTERRUPT: &str = "session.interrupt";
const COMMAND_STEER_TEXT: &str = "session.steer_text";
const COMPLETED_COMMAND_CACHE_CAPACITY: usize = 256;
const COMPLETED_COMMAND_CACHE_TTL_SECS: u64 = 5 * 60;

pub fn spawn_control_channel(config: ShipperConfig) -> Option<JoinHandle<()>> {
    if config.api_token.as_deref().unwrap_or("").trim().is_empty() {
        tracing::debug!("Machine control channel disabled because no device token is configured");
        return None;
    }

    Some(tokio::spawn(async move {
        run_reconnect_loop(config).await;
    }))
}

async fn run_reconnect_loop(config: ShipperConfig) {
    let mut backoff = Duration::from_secs(1);
    let mut completed_commands = CompletedCommandCache::new(
        COMPLETED_COMMAND_CACHE_CAPACITY,
        Duration::from_secs(COMPLETED_COMMAND_CACHE_TTL_SECS),
    );
    loop {
        match run_once(&config, &mut completed_commands).await {
            Ok(()) => {
                tracing::info!("Machine control channel disconnected");
                backoff = Duration::from_secs(1);
            }
            Err(err) => {
                tracing::debug!("Machine control channel connection failed: {err}");
            }
        }
        tokio::time::sleep(backoff).await;
        backoff = (backoff * 2).min(Duration::from_secs(30));
    }
}

async fn run_once(
    config: &ShipperConfig,
    completed_commands: &mut CompletedCommandCache,
) -> Result<()> {
    let ws_url = control_ws_url(&config.api_url)?;
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

    let (stream, _) = connect_async(request)
        .await
        .with_context(|| format!("connecting machine control websocket {ws_url}"))?;
    let (mut write, mut read) = stream.split();

    let hello = json!({
        "type": "hello",
        "schema_version": 1,
        "device_id": config.machine_name,
        "machine_name": config.machine_name,
        "engine_build": build_identity::COMMIT_SHORT,
        "supports": ["codex.send", "codex.interrupt", "codex.steer"],
    });
    write
        .send(Message::Text(hello.to_string()))
        .await
        .context("sending machine control hello")?;
    tracing::info!("Machine control channel connected to {ws_url}");

    while let Some(message) = read.next().await {
        let message = message.context("reading machine control websocket message")?;
        let Message::Text(text) = message else {
            continue;
        };
        let frame: Value = serde_json::from_str(&text).context("parsing machine control frame")?;
        if frame.get("type").and_then(Value::as_str) != Some("command") {
            tracing::debug!(
                "Ignoring machine control frame type={:?}",
                frame.get("type")
            );
            continue;
        }
        let result = handle_command_frame(frame, completed_commands).await;
        write
            .send(Message::Text(result.to_string()))
            .await
            .context("sending machine control command result")?;
    }

    Ok(())
}

async fn handle_command_frame(
    frame: Value,
    completed_commands: &mut CompletedCommandCache,
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

    let result = execute_command(&frame).await;
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

async fn execute_command(frame: &Value) -> std::result::Result<Value, CommandError> {
    let session_id = required_string(frame, "session_id")?;
    let command_type = required_string(frame, "command_type")?;
    let payload = frame.get("payload").cloned().unwrap_or_else(|| json!({}));

    match command_type.as_str() {
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

    #[tokio::test]
    async fn handle_command_frame_rejects_missing_command_id() {
        let mut cache = command_cache();
        let result = handle_command_frame(json!({
            "type": "command",
            "session_id": "session-1",
            "command_type": COMMAND_SEND_TEXT,
            "payload": {"text": "continue"},
        }), &mut cache)
        .await;

        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "invalid_command");
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_unsupported_command_type() {
        let mut cache = command_cache();
        let result = handle_command_frame(json!({
            "type": "command",
            "command_id": "cmd-1",
            "session_id": "session-1",
            "command_type": "session.unknown",
            "payload": {},
        }), &mut cache)
        .await;

        assert_eq!(result["command_id"], "cmd-1");
        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "unsupported_command");
    }

    #[tokio::test]
    async fn handle_command_frame_rejects_missing_attached_session() {
        let mut cache = command_cache();
        let result = handle_command_frame(json!({
            "type": "command",
            "command_id": "cmd-missing-session",
            "session_id": "definitely-missing-control-channel-session",
            "command_type": COMMAND_SEND_TEXT,
            "payload": {"text": "continue"},
        }), &mut cache)
        .await;

        assert_eq!(result["command_id"], "cmd-missing-session");
        assert_eq!(result["ok"], false);
        assert_eq!(result["error"]["code"], "session_not_attached");
    }

    #[tokio::test]
    async fn handle_command_frame_returns_cached_result_for_duplicate_command_id() {
        let mut cache = command_cache();
        let first = handle_command_frame(json!({
            "type": "command",
            "command_id": "cmd-duplicate",
            "session_id": "session-1",
            "command_type": "session.unknown",
            "payload": {},
        }), &mut cache)
        .await;
        let second = handle_command_frame(json!({
            "type": "command",
            "command_id": "cmd-duplicate",
            "session_id": "definitely-missing-control-channel-session",
            "command_type": COMMAND_SEND_TEXT,
            "payload": {"text": "continue"},
        }), &mut cache)
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
}
