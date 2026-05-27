use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use reqwest::header::{HeaderMap, HeaderValue, USER_AGENT};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use tracing::{info, warn};
use uuid::Uuid;

use crate::codex_source::{codex_rollout_file_is_subagent, codex_thread_value_is_subagent};
use crate::text::truncate_tail_chars;

const BRIDGE_RUNTIME_SOURCE: &str =
    crate::state::session_phase::PhaseSource::CodexBridgeWs.as_str();
const DEFAULT_PROGRESS_THROTTLE_MS: u64 = 1500;
const LIVE_RUNTIME_EVENT_TIMEOUT: Duration = Duration::from_millis(1500);
const LIVE_RUNTIME_EVENT_SLOW_LOG_MS: u128 = 500;
const ACTIVE_PHASE_KEEPALIVE_MS: u64 = 30_000;
const THREAD_SUBSCRIBE_BACKGROUND_RETRY_MS: u64 = 500;
const THREAD_SUBSCRIBE_RETRY_ATTEMPTS: usize = 8;
const THREAD_SUBSCRIBE_RETRY_DELAY_MS: u64 = 250;
const CODEX_DISABLE_UPDATE_CHECK_CONFIG: &str = "check_for_update_on_startup=false";
pub const BRIDGE_STATE_SCHEMA_VERSION: u32 = 1;
pub const LAUNCH_MODE_DETACHED_UI: &str = "detached_ui";
pub const LAUNCH_MODE_TUI: &str = "tui";
pub const LEGACY_LAUNCH_MODE_HEADLESS: &str = "headless";
// Readers accept the old dogfood `headless` value, but writers emit the product
// lifecycle name directly.
pub const PERSISTED_DETACHED_UI_LAUNCH_MODE: &str = LAUNCH_MODE_DETACHED_UI;
const BRIDGE_OPT_OUT_NOTIFICATION_METHODS: &[&str] = &[
    "item/plan/delta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
    "item/reasoning/textDelta",
    "turn/plan/updated",
    "turn/diff/updated",
    "thread/tokenUsage/updated",
    "rawResponseItem/completed",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BridgeLaunchMode {
    Tui,
    DetachedUi,
}

impl BridgeLaunchMode {
    pub fn persisted_state_value(self) -> &'static str {
        match self {
            Self::Tui => LAUNCH_MODE_TUI,
            Self::DetachedUi => PERSISTED_DETACHED_UI_LAUNCH_MODE,
        }
    }

    pub fn cli_value(self) -> &'static str {
        match self {
            Self::Tui => LAUNCH_MODE_TUI,
            Self::DetachedUi => "detached-ui",
        }
    }

    pub fn from_cli_value(value: &str) -> Option<Self> {
        let value = value.trim();
        if value.eq_ignore_ascii_case(LAUNCH_MODE_TUI) {
            return Some(Self::Tui);
        }
        if value.eq_ignore_ascii_case(LAUNCH_MODE_DETACHED_UI)
            || value.eq_ignore_ascii_case("detached-ui")
            || value.eq_ignore_ascii_case(LEGACY_LAUNCH_MODE_HEADLESS)
        {
            return Some(Self::DetachedUi);
        }
        None
    }
}

#[derive(Debug, Clone)]
pub struct BridgeStartConfig {
    pub session_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub codex_bin: String,
    pub approval_policy: Option<String>,
    pub sandbox: Option<String>,
    pub model: Option<String>,
    pub model_reasoning_effort: Option<String>,
    pub machine_name: Option<String>,
    pub auto_approve: bool,
    pub state_root: Option<PathBuf>,
    pub longhouse_home: Option<PathBuf>,
    pub log_file: Option<PathBuf>,
    pub start_timeout_secs: u64,
    /// When true, the bridge's run loop invokes `thread/start` itself before
    /// marking the bridge ready. This is independent from launch lifecycle.
    pub create_initial_thread: bool,
    pub launch_mode: BridgeLaunchMode,
}

#[derive(Debug, Clone)]
pub struct BridgeRunConfig {
    pub session_id: String,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub codex_bin: String,
    pub session_source: Option<String>,
    pub approval_policy: Option<String>,
    pub sandbox: Option<String>,
    pub model: Option<String>,
    pub model_reasoning_effort: Option<String>,
    pub machine_name: Option<String>,
    pub auto_approve: bool,
    pub longhouse_home: Option<PathBuf>,
    pub state_file: PathBuf,
    pub log_file: PathBuf,
    /// When true, the bridge calls `thread/start` itself instead of waiting
    /// for a TUI attach.
    pub create_initial_thread: bool,
    pub launch_mode: BridgeLaunchMode,
}

#[derive(Debug, Clone)]
pub struct BridgeSendConfig {
    pub session_id: String,
    pub text: String,
    pub state_root: Option<PathBuf>,
    pub allow_direct_ws_fallback: bool,
    pub attachments: Vec<crate::codex_attachments::AttachmentRef>,
}

#[derive(Debug, Clone)]
pub struct BridgeInterruptConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct BridgeSteerConfig {
    pub session_id: String,
    pub text: String,
    pub state_root: Option<PathBuf>,
    pub attachments: Vec<crate::codex_attachments::AttachmentRef>,
}

/// Failure modes specific to the steer IPC path. Distinguishes "the turn
/// we would have steered into has already ended" (a product concept the
/// backend must surface) from generic protocol errors.
#[derive(Debug, thiserror::Error)]
pub enum BridgeSteerError {
    #[error("bridge state does not have an active turn to steer")]
    NoActiveTurn,
    #[error("bridge state is missing required field: {0}")]
    MissingState(&'static str),
    #[error("codex app-server rejected turn/steer: {0}")]
    TurnEnded(String),
    #[error(transparent)]
    Protocol(#[from] anyhow::Error),
}

#[derive(Debug, Clone)]
pub struct BridgeStopConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub terminal_reason: Option<String>,
}

#[derive(Debug, Clone)]
pub struct BridgeAttachConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub codex_bin: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BridgeStateFile {
    #[serde(default = "default_bridge_state_schema_version")]
    pub schema_version: u32,
    pub session_id: String,
    pub cwd: String,
    pub codex_bin: String,
    /// Managed Codex launch mode. `detached_ui` means long-running app-server
    /// control without a visible terminal TUI; it is not one-shot/batch
    /// prompt-and-exit execution. Legacy state files may still contain
    /// `headless`, which is interpreted as equivalent to `detached_ui`.
    #[serde(default)]
    pub launch_mode: Option<String>,
    pub ws_url: Option<String>,
    pub thread_id: Option<String>,
    pub thread_path: Option<String>,
    pub pid: u32,
    #[serde(default)]
    pub app_server_pid: Option<u32>,
    #[serde(default)]
    pub app_server_pgid: Option<i32>,
    #[serde(default)]
    pub app_server_ws_url: Option<String>,
    pub status: String,
    pub log_file: String,
    pub active_turn_id: Option<String>,
    pub last_turn_status: Option<String>,
    pub last_error: Option<String>,
    #[serde(default)]
    pub thread_subscription_status: Option<String>,
    #[serde(default)]
    pub thread_subscription_attempts: u32,
    #[serde(default)]
    pub thread_subscription_last_error: Option<String>,
    pub updated_at: String,
}

fn default_bridge_state_schema_version() -> u32 {
    0
}

#[derive(Debug, Clone, Serialize)]
pub struct BridgeStartSummary {
    pub session_id: String,
    pub state_file: String,
    pub log_file: String,
    pub pid: u32,
    pub ws_url: String,
    /// None until a thread exists, either from TUI attach or detached-UI `thread/start`.
    pub thread_id: Option<String>,
    pub thread_path: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BridgeSendSummary {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: String,
    pub turn_status: String,
}

#[derive(Debug)]
enum StreamEvent {
    Rpc(Value),
    Stderr(String),
    StdoutParseError(String),
}

#[derive(Debug)]
enum RpcOutbound {
    WebSocket(mpsc::UnboundedSender<String>),
}

#[derive(Debug)]
struct RpcClient {
    child: Option<Child>,
    child_pid: Option<u32>,
    child_pgid: Option<i32>,
    child_ws_url: Option<String>,
    outbound: RpcOutbound,
    events_rx: mpsc::UnboundedReceiver<StreamEvent>,
    pending_methods: BTreeMap<u64, String>,
    next_request_id: u64,
    ws_url: String,
}

#[derive(Debug, Clone)]
struct BridgeRuntimeSink {
    http: reqwest::Client,
    api_url: String,
    api_token: String,
    session_id: String,
    cwd: String,
    machine_name: Option<String>,
    thread_id: Option<String>,
    local_db_path: Option<PathBuf>,
    runtime_tx: Option<mpsc::UnboundedSender<Vec<Value>>>,
    live_runtime_tx: Option<mpsc::UnboundedSender<Vec<Value>>>,
}

#[derive(Debug)]
struct BridgeContext {
    state_file: PathBuf,
    state: BridgeStateFile,
    runtime: BridgeRuntimeSink,
    last_progress_emit: Option<Instant>,
    live_transcript_seq: u64,
    live_transcript_text: String,
    runtime_tracker: CodexRuntimeTracker,
    subscribed_thread_id: Option<String>,
    rejected_thread_ids: BTreeSet<String>,
}

#[derive(Debug, Clone)]
struct ResolvedBridgePaths {
    state_file: PathBuf,
    log_file: PathBuf,
}

#[derive(Debug, Default)]
struct CodexRuntimeTracker {
    active_turn_id: Option<String>,
    attention_state: Option<CodexAttentionState>,
    active_items: BTreeMap<String, ActiveCodexItem>,
    next_item_sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum CodexAttentionState {
    Approval { tool_name: Option<String> },
    UserInput,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ActiveCodexItem {
    item_type: String,
    tool_name: Option<String>,
    sequence: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BridgeRuntimeUpdate {
    Phase {
        phase: &'static str,
        tool_name: Option<String>,
    },
    Progress,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ThreadSubscriptionStatus {
    WaitingForThread,
    WaitingForTurn,
    WaitingForRollout,
    ReadyToSubscribe,
    Subscribing,
    Retrying,
    Subscribed,
    Failed,
}

impl ThreadSubscriptionStatus {
    fn as_str(self) -> &'static str {
        match self {
            Self::WaitingForThread => "waiting_for_thread",
            Self::WaitingForTurn => "waiting_for_turn",
            Self::WaitingForRollout => "waiting_for_rollout",
            Self::ReadyToSubscribe => "ready_to_subscribe",
            Self::Subscribing => "subscribing",
            Self::Retrying => "retrying",
            Self::Subscribed => "subscribed",
            Self::Failed => "failed",
        }
    }
}

#[derive(Debug)]
enum IpcCommand {
    TurnStart {
        text: String,
        thread_id: String,
        attachments: Vec<crate::codex_attachments::AttachmentRef>,
        reply: oneshot::Sender<Result<Value>>,
    },
    /// Mid-turn steer routed through the daemon's persistent app-server
    /// connection. Lets the backend avoid a per-call WS connect +
    /// initialize_client handshake that we measured at tens of ms on
    /// localhost (much worse over network).
    Steer {
        text: String,
        thread_id: String,
        expected_turn_id: String,
        attachments: Vec<crate::codex_attachments::AttachmentRef>,
        reply: oneshot::Sender<Result<Value>>,
    },
    Stop {
        terminal_reason: String,
        reply: oneshot::Sender<Result<Value>>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum BridgeFollowup {
    SubscribeThread {
        thread_id: String,
        thread_path: Option<String>,
    },
}

const TERMINAL_REASON_BRIDGE_STOP: &str = "bridge_stop";
const TERMINAL_REASON_TERMINAL_DISCONNECTED: &str = "terminal_disconnected";

fn normalize_bridge_terminal_reason(value: Option<&str>) -> String {
    match value.map(str::trim).filter(|reason| !reason.is_empty()) {
        Some(TERMINAL_REASON_TERMINAL_DISCONNECTED) => {
            TERMINAL_REASON_TERMINAL_DISCONNECTED.to_string()
        }
        Some(TERMINAL_REASON_BRIDGE_STOP) => TERMINAL_REASON_BRIDGE_STOP.to_string(),
        Some("user_closed") => "user_closed".to_string(),
        Some("process_gone") => "process_gone".to_string(),
        Some("host_expired") => "host_expired".to_string(),
        Some("provider_signal") => "provider_signal".to_string(),
        _ => TERMINAL_REASON_BRIDGE_STOP.to_string(),
    }
}

fn ipc_socket_path(state_file: &Path) -> PathBuf {
    state_file.with_extension("sock")
}

/// Spawn a Unix socket listener that accepts IPC commands from bridge helpers.
/// Each connection reads a single JSON line and forwards it as an `IpcCommand`
/// to the daemon loop, then writes back the JSON result.
#[cfg(unix)]
fn spawn_ipc_listener(
    sock_path: PathBuf,
    tx: mpsc::UnboundedSender<IpcCommand>,
) -> Result<tokio::task::JoinHandle<()>> {
    // Clean up stale socket
    let _ = fs::remove_file(&sock_path);
    if let Some(parent) = sock_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let listener = tokio::net::UnixListener::bind(&sock_path)
        .with_context(|| format!("binding IPC socket at {}", sock_path.display()))?;

    Ok(tokio::spawn(async move {
        loop {
            let (stream, _) = match listener.accept().await {
                Ok(v) => v,
                Err(e) => {
                    eprintln!("[codex-bridge] ipc accept error: {e}");
                    continue;
                }
            };
            let tx = tx.clone();
            tokio::spawn(async move {
                if let Err(e) = handle_ipc_connection(stream, tx).await {
                    eprintln!("[codex-bridge] ipc connection error: {e}");
                }
            });
        }
    }))
}

#[cfg(unix)]
async fn handle_ipc_connection(
    mut stream: tokio::net::UnixStream,
    tx: mpsc::UnboundedSender<IpcCommand>,
) -> Result<()> {
    let mut buf = vec![0u8; 8192];
    let mut total = 0usize;
    // Read until newline or EOF
    loop {
        if total >= buf.len() {
            bail!("IPC request too large");
        }
        let n = stream.read(&mut buf[total..]).await?;
        if n == 0 {
            break;
        }
        total += n;
        if buf[..total].contains(&b'\n') {
            break;
        }
    }
    let request: Value =
        serde_json::from_slice(&buf[..total]).context("parsing IPC request JSON")?;
    let (reply_tx, reply_rx) = oneshot::channel();
    let command = match request.get("kind").and_then(Value::as_str) {
        Some("stop") => IpcCommand::Stop {
            terminal_reason: normalize_bridge_terminal_reason(
                request.get("reason").and_then(Value::as_str),
            ),
            reply: reply_tx,
        },
        Some("steer") => {
            let text = request
                .get("text")
                .and_then(Value::as_str)
                .context("IPC steer request missing 'text'")?
                .to_string();
            let thread_id = request
                .get("thread_id")
                .and_then(Value::as_str)
                .context("IPC steer request missing 'thread_id'")?
                .to_string();
            let expected_turn_id = request
                .get("expected_turn_id")
                .and_then(Value::as_str)
                .context("IPC steer request missing 'expected_turn_id'")?
                .to_string();
            let attachments = crate::codex_attachments::parse_attachments(&request)
                .context("IPC steer request has invalid attachments")?;
            IpcCommand::Steer {
                text,
                thread_id,
                expected_turn_id,
                attachments,
                reply: reply_tx,
            }
        }
        _ => {
            let text = request
                .get("text")
                .and_then(Value::as_str)
                .context("IPC request missing 'text'")?
                .to_string();
            let thread_id = request
                .get("thread_id")
                .and_then(Value::as_str)
                .context("IPC request missing 'thread_id'")?
                .to_string();
            let attachments = crate::codex_attachments::parse_attachments(&request)
                .context("IPC turn/start request has invalid attachments")?;
            IpcCommand::TurnStart {
                text,
                thread_id,
                attachments,
                reply: reply_tx,
            }
        }
    };
    tx.send(command)
        .map_err(|_| anyhow!("daemon event loop closed"))?;

    let result = reply_rx
        .await
        .map_err(|_| anyhow!("daemon dropped reply channel"))?;

    let response = match result {
        Ok(value) => match value {
            Value::Object(mut object) => {
                object.insert("ok".to_string(), Value::Bool(true));
                Value::Object(object)
            }
            other => json!({
                "ok": true,
                "result": other,
            }),
        },
        Err(e) => json!({
            "ok": false,
            "error": format!("{e:#}"),
        }),
    };
    let mut resp_bytes = serde_json::to_vec(&response)?;
    resp_bytes.push(b'\n');
    stream.write_all(&resp_bytes).await?;
    stream.shutdown().await?;
    Ok(())
}

pub async fn cmd_codex_bridge_start(config: BridgeStartConfig) -> Result<BridgeStartSummary> {
    if config.session_id.trim().is_empty() {
        bail!("session_id must not be empty");
    }
    if uuid::Uuid::parse_str(&config.session_id).is_err() {
        bail!(
            "session_id must be a valid UUID, got: {}",
            config.session_id
        );
    }
    if config.cwd.as_os_str().is_empty() || !config.cwd.is_dir() {
        bail!("cwd does not exist: {}", config.cwd.display());
    }

    let paths = resolve_bridge_paths(
        config.state_root.as_deref(),
        &config.session_id,
        config.log_file.as_deref(),
    )?;
    if let Some(parent) = paths.state_file.parent() {
        fs::create_dir_all(parent)?;
    }
    if let Some(parent) = paths.log_file.parent() {
        fs::create_dir_all(parent)?;
    }

    let mut state = BridgeStateFile {
        schema_version: BRIDGE_STATE_SCHEMA_VERSION,
        session_id: config.session_id.clone(),
        cwd: config.cwd.display().to_string(),
        codex_bin: config.codex_bin.clone(),
        launch_mode: Some(config.launch_mode.persisted_state_value().to_string()),
        ws_url: None,
        thread_id: None,
        thread_path: None,
        pid: 0,
        app_server_pid: None,
        app_server_pgid: None,
        app_server_ws_url: None,
        status: "starting".to_string(),
        log_file: paths.log_file.display().to_string(),
        active_turn_id: None,
        last_turn_status: None,
        last_error: None,
        thread_subscription_status: Some(
            ThreadSubscriptionStatus::WaitingForThread
                .as_str()
                .to_string(),
        ),
        thread_subscription_attempts: 0,
        thread_subscription_last_error: None,
        updated_at: Utc::now().to_rfc3339(),
    };
    write_state_file(&paths.state_file, &state)?;

    let current_exe =
        std::env::current_exe().context("resolving current executable for codex-bridge start")?;
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&paths.log_file)
        .with_context(|| format!("opening {}", paths.log_file.display()))?;
    let stderr = stdout
        .try_clone()
        .with_context(|| format!("cloning {}", paths.log_file.display()))?;

    let mut child = std::process::Command::new(&current_exe);
    child
        .arg("codex-bridge")
        .arg("run")
        .arg("--session-id")
        .arg(&config.session_id)
        .arg("--cwd")
        .arg(&config.cwd)
        .arg("--url")
        .arg(&config.api_url)
        .arg("--token")
        .arg(&config.api_token)
        .arg("--codex-bin")
        .arg(&config.codex_bin)
        .arg("--state-file")
        .arg(&paths.state_file)
        .arg("--log-file")
        .arg(&paths.log_file)
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        .stdin(Stdio::null());
    if let Some(longhouse_home) = config.longhouse_home.as_deref() {
        child.arg("--longhouse-home").arg(longhouse_home);
    }
    if let Some(policy) = config.approval_policy.as_deref() {
        child.arg("--approval-policy").arg(policy);
    }
    if let Some(sandbox) = config.sandbox.as_deref() {
        child.arg("--sandbox").arg(sandbox);
    }
    if let Some(model) = config.model.as_deref() {
        child.arg("--model").arg(model);
    }
    if let Some(effort) = config.model_reasoning_effort.as_deref() {
        child.arg("--model-reasoning-effort").arg(effort);
    }
    if let Some(machine_name) = config.machine_name.as_deref() {
        child.arg("--machine-name").arg(machine_name);
    }
    if config.auto_approve {
        child.arg("--auto-approve");
    }
    if config.create_initial_thread {
        child.arg("--create-initial-thread");
    }
    if config.launch_mode != BridgeLaunchMode::Tui {
        child
            .arg("--launch-mode")
            .arg(config.launch_mode.cli_value());
    }

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;

        unsafe {
            child.pre_exec(|| {
                if libc::setsid() == -1 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }

    let daemon = child.spawn().with_context(|| {
        format!(
            "spawning detached codex bridge from {}",
            current_exe.display()
        )
    })?;
    state.pid = daemon.id();
    state.updated_at = Utc::now().to_rfc3339();
    write_state_file(&paths.state_file, &state)?;

    let deadline = Instant::now() + Duration::from_secs(config.start_timeout_secs.max(1));
    loop {
        if Instant::now() >= deadline {
            let log_tail = read_log_tail(&paths.log_file, 4000);
            bail!(
                "timed out waiting for codex bridge to become ready (state_file={} log_tail={})",
                paths.state_file.display(),
                log_tail
            );
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        let state = match read_state_file(&paths.state_file) {
            Ok(state) => state,
            Err(_) => continue,
        };
        if state.status == "ready" {
            let ws_url = state
                .ws_url
                .clone()
                .context("bridge marked ready without ws_url")?;
            return Ok(BridgeStartSummary {
                session_id: state.session_id,
                state_file: paths.state_file.display().to_string(),
                log_file: paths.log_file.display().to_string(),
                pid: state.pid,
                ws_url,
                thread_id: state.thread_id.clone(),
                thread_path: state.thread_path,
            });
        }
        if state.status == "error" {
            let log_tail = read_log_tail(&paths.log_file, 4000);
            bail!(
                "codex bridge failed to start: {} (log_tail={})",
                state
                    .last_error
                    .unwrap_or_else(|| "unknown bridge startup error".to_string()),
                log_tail
            );
        }
    }
}

pub async fn cmd_codex_bridge_run(config: BridgeRunConfig) -> Result<()> {
    let pid = std::process::id();
    crate::codex_attachments::cleanup_session_tmpdir(&config.session_id);
    let initial_state = BridgeStateFile {
        schema_version: BRIDGE_STATE_SCHEMA_VERSION,
        session_id: config.session_id.clone(),
        cwd: config.cwd.display().to_string(),
        codex_bin: config.codex_bin.clone(),
        launch_mode: Some(config.launch_mode.persisted_state_value().to_string()),
        ws_url: None,
        thread_id: None,
        thread_path: None,
        pid,
        app_server_pid: None,
        app_server_pgid: None,
        app_server_ws_url: None,
        status: "starting".to_string(),
        log_file: config.log_file.display().to_string(),
        active_turn_id: None,
        last_turn_status: None,
        last_error: None,
        thread_subscription_status: Some(
            ThreadSubscriptionStatus::WaitingForThread
                .as_str()
                .to_string(),
        ),
        thread_subscription_attempts: 0,
        thread_subscription_last_error: None,
        updated_at: Utc::now().to_rfc3339(),
    };
    write_state_file(&config.state_file, &initial_state)?;

    // Acquire an exclusive advisory lock on a sidecar file for the process
    // lifetime. The kernel releases the flock when this process exits (normal,
    // crash, or SIGKILL), so readers can use a non-blocking flock() probe as a
    // liveness test immune to PID reuse. A sidecar is used instead of the
    // state file itself because state writes go through atomic rename, which
    // would replace the inode and break the lock.
    acquire_bridge_lock(&bridge_lock_path(&config.state_file))?;

    let mut client = spawn_app_server_client(&config).await?;
    let ws_url = client.ws_url.clone();
    let mut starting_state = initial_state.clone();
    starting_state.ws_url = Some(ws_url.clone());
    starting_state.app_server_pid = client.child_pid;
    starting_state.app_server_pgid = client.child_pgid;
    starting_state.app_server_ws_url = client.child_ws_url.clone();
    write_state_file(&config.state_file, &starting_state)?;

    // Initialize the protocol handshake. Legacy TUI-attached startup creates
    // the thread later; prestart paths create it below.
    initialize_client(&mut client).await?;

    // Initial thread creation path: create the thread ourselves so the session
    // is driveable before a visible TUI attaches.
    if config.create_initial_thread {
        let params = json!({
            "cwd": config.cwd.to_string_lossy(),
            "approvalPolicy": config.approval_policy,
            "sandbox": config.sandbox,
            "model": config.model,
        });
        match send_request(&mut client, "thread/start", params).await {
            Ok(response) => {
                let thread_id = response
                    .get("thread")
                    .and_then(|thread| thread.get("id"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                if thread_id.as_deref().unwrap_or("").is_empty() {
                    starting_state.status = "error".to_string();
                    starting_state.last_error =
                        Some("thread/start did not return thread id".to_string());
                    let _ = write_state_file(&config.state_file, &starting_state);
                    bail!("thread/start did not return thread id");
                }
                let thread_path = response
                    .get("thread")
                    .and_then(|thread| thread.get("path"))
                    .and_then(Value::as_str)
                    .map(str::to_string);
                starting_state.thread_id = thread_id;
                starting_state.thread_path = thread_path;
                write_state_file(&config.state_file, &starting_state)?;
                sync_thread_binding(
                    &config,
                    None,
                    starting_state.thread_path.as_deref(),
                    &config.session_id,
                );
            }
            Err(err) => {
                starting_state.status = "error".to_string();
                starting_state.last_error = Some(format!("thread/start failed: {err}"));
                let _ = write_state_file(&config.state_file, &starting_state);
                bail!("thread/start failed in bridge: {err}");
            }
        }
    }

    let initial_thread_id = starting_state.thread_id.clone();
    let initial_thread_path = starting_state.thread_path.clone();
    let initial_subscription_status = if initial_thread_id.is_some() {
        ThreadSubscriptionStatus::WaitingForTurn
    } else {
        ThreadSubscriptionStatus::WaitingForThread
    };

    let mut runtime_headers = HeaderMap::new();
    runtime_headers.insert(
        USER_AGENT,
        HeaderValue::from_str(&format!("longhouse-engine/{}", env!("CARGO_PKG_VERSION")))
            .context("invalid runtime user-agent header value")?,
    );
    let runtime_http = reqwest::Client::builder()
        .default_headers(runtime_headers)
        .timeout(Duration::from_secs(5))
        .pool_max_idle_per_host(4)
        .build()
        .context("building bridge runtime HTTP client")?;
    let (runtime_tx, runtime_rx) = mpsc::unbounded_channel::<Vec<Value>>();
    let (live_runtime_tx, live_runtime_rx) = mpsc::unbounded_channel::<Vec<Value>>();
    let runtime_worker_sink = BridgeRuntimeSink {
        http: runtime_http.clone(),
        api_url: config.api_url.clone(),
        api_token: config.api_token.clone(),
        session_id: config.session_id.clone(),
        cwd: config.cwd.display().to_string(),
        machine_name: config.machine_name.clone(),
        thread_id: initial_thread_id.clone(),
        local_db_path: None,
        runtime_tx: None,
        live_runtime_tx: None,
    };
    let _runtime_worker = spawn_runtime_event_worker(runtime_worker_sink, runtime_rx);
    let live_runtime_worker_sink = BridgeRuntimeSink {
        http: runtime_http.clone(),
        api_url: config.api_url.clone(),
        api_token: config.api_token.clone(),
        session_id: config.session_id.clone(),
        cwd: config.cwd.display().to_string(),
        machine_name: config.machine_name.clone(),
        thread_id: initial_thread_id.clone(),
        local_db_path: None,
        runtime_tx: None,
        live_runtime_tx: None,
    };
    let _live_runtime_worker =
        spawn_live_runtime_event_worker(live_runtime_worker_sink, live_runtime_rx);

    let mut context = BridgeContext {
        state_file: config.state_file.clone(),
        state: BridgeStateFile {
            schema_version: BRIDGE_STATE_SCHEMA_VERSION,
            session_id: config.session_id.clone(),
            cwd: config.cwd.display().to_string(),
            codex_bin: config.codex_bin.clone(),
            launch_mode: initial_state.launch_mode.clone(),
            ws_url: Some(ws_url.clone()),
            thread_id: initial_thread_id.clone(),
            thread_path: initial_thread_path.clone(),
            pid,
            app_server_pid: client.child_pid,
            app_server_pgid: client.child_pgid,
            app_server_ws_url: client.child_ws_url.clone(),
            status: "ready".to_string(),
            log_file: config.log_file.display().to_string(),
            active_turn_id: None,
            last_turn_status: None,
            last_error: None,
            thread_subscription_status: Some(initial_subscription_status.as_str().to_string()),
            thread_subscription_attempts: 0,
            thread_subscription_last_error: None,
            updated_at: Utc::now().to_rfc3339(),
        },
        runtime: BridgeRuntimeSink {
            http: runtime_http,
            api_url: config.api_url.clone(),
            api_token: config.api_token.clone(),
            session_id: config.session_id.clone(),
            cwd: config.cwd.display().to_string(),
            machine_name: config.machine_name.clone(),
            thread_id: initial_thread_id,
            local_db_path: resolve_bridge_agent_db_path(config.longhouse_home.as_deref()).ok(),
            runtime_tx: Some(runtime_tx),
            live_runtime_tx: Some(live_runtime_tx),
        },
        last_progress_emit: None,
        live_transcript_seq: 0,
        live_transcript_text: String::new(),
        runtime_tracker: CodexRuntimeTracker::default(),
        subscribed_thread_id: None,
        rejected_thread_ids: BTreeSet::new(),
    };
    // Mark ready so the CLI can read ws_url and launch the TUI. Prestart
    // launches already have a thread id; legacy TUI launches capture it later
    // from thread/started notifications.
    write_state_file(&context.state_file, &context.state)?;
    if config.create_initial_thread && context.state.thread_id.is_some() {
        let startup_phase = context.runtime_tracker.current_phase_update();
        emit_runtime_updates(&config, &mut context, vec![startup_phase]).await;
    }

    // Spawn IPC socket listener so `send` routes through the daemon's persistent connection
    let sock_path = ipc_socket_path(&context.state_file);
    let (ipc_tx, mut ipc_rx) = mpsc::unbounded_channel::<IpcCommand>();

    #[cfg(unix)]
    let _ipc_handle = spawn_ipc_listener(sock_path.clone(), ipc_tx)?;

    // Cleanup socket on exit
    struct SocketCleanup(PathBuf);
    impl Drop for SocketCleanup {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }
    let _sock_guard = SocketCleanup(sock_path);
    // Codex can spend minutes in model-only thinking without emitting any
    // item deltas. Refresh the live phase so Timeline does not decay to Ready.
    let mut runtime_keepalive =
        tokio::time::interval(Duration::from_millis(ACTIVE_PHASE_KEEPALIVE_MS));
    runtime_keepalive.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    runtime_keepalive.tick().await;
    let mut thread_subscribe_retry =
        tokio::time::interval(Duration::from_millis(THREAD_SUBSCRIBE_BACKGROUND_RETRY_MS));
    thread_subscribe_retry.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    thread_subscribe_retry.tick().await;

    loop {
        tokio::select! {
            event_result = recv_event(&mut client) => {
                let event = event_result?;
                match event {
                    StreamEvent::Rpc(value) => {
                        if value.get("id").is_some() && value.get("method").is_some() {
                            handle_server_request(&config, value, &mut client, &mut context).await?;
                            continue;
                        }
                        if let Some(id) = value.get("id").and_then(Value::as_u64) {
                            let _ = client.pending_methods.remove(&id);
                            continue;
                        }
                        if let Some(followup) = process_notification(&value, &config, &mut context).await? {
                            if let Err(err) =
                                handle_bridge_followup(&config, &mut client, &mut context, followup).await
                            {
                                eprintln!("[codex-bridge] thread followup failed: {err}");
                            }
                        }
                    }
                    StreamEvent::Stderr(line) => {
                        eprintln!("[codex-bridge] app-server: {line}");
                    }
                    StreamEvent::StdoutParseError(detail) => {
                        eprintln!("[codex-bridge] protocol error: {detail}");
                        update_bridge_error(&mut context, &detail)?;
                        bail!("codex bridge protocol error: {detail}");
                    }
                }
            }
            _ = thread_subscribe_retry.tick() => {
                if let Some(followup) = pending_thread_subscription(&mut context)? {
                    if let Err(err) =
                        handle_bridge_followup(&config, &mut client, &mut context, followup).await
                    {
                        eprintln!("[codex-bridge] background thread subscribe failed: {err}");
                    }
                }
            }
            Some(cmd) = ipc_rx.recv() => {
                match cmd {
                    IpcCommand::TurnStart { text, thread_id, attachments, reply } => {
                        let result = handle_ipc_turn_start(
                            &config,
                            &mut client,
                            &mut context,
                            &text,
                            &thread_id,
                            &attachments,
                        )
                        .await
                        .and_then(|summary| serde_json::to_value(summary).map_err(Into::into));
                        let _ = reply.send(result);
                    }
                    IpcCommand::Steer { text, thread_id, expected_turn_id, attachments, reply } => {
                        let result = handle_ipc_steer(
                            &config,
                            &mut client,
                            &mut context,
                            &text,
                            &thread_id,
                            &expected_turn_id,
                            &attachments,
                        )
                        .await
                        .map(|_| json!({}));
                        let _ = reply.send(result);
                    }
                    IpcCommand::Stop {
                        terminal_reason,
                        reply,
                    } => {
                        crate::codex_attachments::cleanup_session_tmpdir(&context.state.session_id);
                        context.state.status = "stopped".to_string();
                        context.state.active_turn_id = None;
                        context.state.last_error = None;
                        write_state_file(&context.state_file, &context.state)?;
                        if let Some(path) = context.state.thread_path.as_deref() {
                            wake_daemon_for_transcript(
                                &config,
                                path,
                                "idle",
                                &terminal_reason,
                                None,
                            );
                        }
                        context
                            .runtime
                            .post_terminal(
                                "session_ended",
                                &terminal_reason,
                                format!(
                                    "bridge:terminal:{}:{}",
                                    context.state.session_id,
                                    Uuid::new_v4()
                                ),
                            )
                            .await;
                        let _ = reply.send(Ok(json!({})));
                        shutdown_child(&mut client).await?;
                        break;
                    }
                }
            }
            _ = runtime_keepalive.tick() => {
                emit_runtime_keepalive(&config, &mut context).await;
            }
        }
    }

    Ok(())
}

/// Fire `turn/steer` through the daemon's persistent app-server connection.
/// Avoids the per-call WS connect + initialize handshake that direct-WS steer
/// would incur on every dispatch.
async fn handle_ipc_steer(
    config: &BridgeRunConfig,
    client: &mut RpcClient,
    context: &mut BridgeContext,
    text: &str,
    thread_id: &str,
    expected_turn_id: &str,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> Result<()> {
    let fetched = crate::codex_attachments::fetch_all(
        &context.runtime.http,
        &config.api_url,
        &config.api_token,
        &context.state.session_id,
        attachments,
    )
    .await?;
    let had_attachments = !fetched.is_empty();
    let input = crate::codex_attachments::build_user_input_items(text, &fetched);
    let result = send_request_with_runtime(
        client,
        "turn/steer",
        json!({
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": input,
        }),
        config,
        context,
    )
    .await;
    if result.is_err() && had_attachments {
        crate::codex_attachments::cleanup_session_tmpdir(&context.state.session_id);
    }
    result.map(|_| ())
}

async fn handle_ipc_turn_start(
    config: &BridgeRunConfig,
    client: &mut RpcClient,
    context: &mut BridgeContext,
    text: &str,
    thread_id: &str,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> Result<BridgeSendSummary> {
    let fetched = crate::codex_attachments::fetch_all(
        &context.runtime.http,
        &config.api_url,
        &config.api_token,
        &context.state.session_id,
        attachments,
    )
    .await?;
    let had_attachments = !fetched.is_empty();
    let input = crate::codex_attachments::build_user_input_items(text, &fetched);
    let response = match send_request_with_runtime(
        client,
        "turn/start",
        json!({
            "threadId": thread_id,
            "input": input,
        }),
        config,
        context,
    )
    .await
    {
        Ok(value) => value,
        Err(err) => {
            if had_attachments {
                crate::codex_attachments::cleanup_session_tmpdir(&context.state.session_id);
            }
            return Err(err);
        }
    };
    let turn_id = extract_string(&response, &["turn", "id"])
        .context("missing turn.id in IPC turn/start response")?;
    let turn_status =
        extract_string(&response, &["turn", "status"]).unwrap_or_else(|| "inProgress".to_string());
    context.state.active_turn_id = Some(turn_id.clone());
    context.state.last_turn_status = Some(turn_status.clone());
    context.runtime_tracker.active_turn_id = Some(turn_id.clone());
    write_state_file(&context.state_file, &context.state)?;
    if let Some(path) = context.state.thread_path.as_deref() {
        wake_daemon_for_transcript(config, path, "running", "turn_started", Some(&turn_id));
    }
    emit_runtime_updates(
        config,
        context,
        vec![context.runtime_tracker.current_phase_update()],
    )
    .await;
    Ok(BridgeSendSummary {
        session_id: context.state.session_id.clone(),
        thread_id: thread_id.to_string(),
        turn_id,
        turn_status,
    })
}

pub async fn cmd_codex_bridge_send(config: BridgeSendConfig) -> Result<BridgeSendSummary> {
    if config.text.trim().is_empty() && config.attachments.is_empty() {
        bail!("text must not be empty when no attachments are present");
    }
    let state = load_ready_state(&config.session_id, config.state_root.as_deref())?;
    let thread_id = state
        .thread_id
        .clone()
        .context("bridge state is missing thread_id")?;

    // Managed sends normally route through the daemon IPC socket so they use
    // the persistent app-server connection and preserve conversation context.
    let paths = resolve_bridge_paths(config.state_root.as_deref(), &config.session_id, None)?;
    let sock_path = ipc_socket_path(&paths.state_file);
    #[cfg(unix)]
    if sock_path.exists() {
        match send_via_ipc(&sock_path, &config.text, &thread_id, &config.attachments).await {
            Ok(summary) => return Ok(summary),
            Err(e) => {
                // Only fall back on connection failures. If the daemon accepted the
                // request but the reply was lost, retrying via direct WebSocket would
                // duplicate the turn.
                let is_connect_failure =
                    e.downcast_ref::<std::io::Error>().map_or(false, |io_err| {
                        matches!(
                            io_err.kind(),
                            std::io::ErrorKind::ConnectionRefused
                                | std::io::ErrorKind::NotFound
                                | std::io::ErrorKind::BrokenPipe
                        )
                    });
                if !is_connect_failure {
                    return Err(e.context(
                        "IPC dispatch may have succeeded; not retrying to avoid duplicate turn",
                    ));
                }
                if !config.allow_direct_ws_fallback {
                    bail!(
                        "IPC dispatch failed for managed Codex session {}; refusing direct WebSocket fallback. Restart the bridge or pass --allow-direct-ws-fallback for explicit debug/operator use: {e}",
                        config.session_id
                    );
                }
                eprintln!(
                    "[codex-bridge] IPC connect failed; explicit --allow-direct-ws-fallback enabled, routing directly to Codex app-server: {e}"
                );
            }
        }
    }

    if !config.allow_direct_ws_fallback {
        bail!(
            "IPC socket {} is missing for managed Codex session {}; refusing direct WebSocket fallback. Restart the bridge or pass --allow-direct-ws-fallback for explicit debug/operator use",
            sock_path.display(),
            config.session_id
        );
    }
    if !config.attachments.is_empty() {
        bail!(
            "direct WebSocket fallback does not support attachments; restart the bridge so the daemon IPC socket is available"
        );
    }
    eprintln!(
        "[codex-bridge] explicit --allow-direct-ws-fallback enabled; direct WebSocket sends may lose daemon conversation context"
    );

    // Explicit debug/operator fallback: direct WebSocket.
    let ws_url = state
        .ws_url
        .clone()
        .context("bridge state is missing ws_url")?;
    let mut client = connect_remote_client(&ws_url).await?;
    initialize_client(&mut client).await?;

    let response = send_request(
        &mut client,
        "turn/start",
        json!({
            "threadId": thread_id,
            "input": [{"type": "text", "text": config.text}],
        }),
    )
    .await?;
    let turn_id = extract_string(&response, &["turn", "id"])
        .context("missing turn.id in bridge send response")?;
    let turn_status =
        extract_string(&response, &["turn", "status"]).unwrap_or_else(|| "inProgress".to_string());
    shutdown_child(&mut client).await?;

    Ok(BridgeSendSummary {
        session_id: config.session_id,
        thread_id,
        turn_id,
        turn_status,
    })
}

/// Send text to the daemon via its Unix domain socket.
///
/// The entire round-trip (connect + write + read response) is bounded by a
/// timeout to prevent wedging if the daemon or app-server stalls.
const IPC_SEND_TIMEOUT: Duration = Duration::from_secs(30);
const IPC_STOP_TIMEOUT: Duration = Duration::from_secs(3);
const CHILD_SHUTDOWN_GRACE_PERIOD: Duration = Duration::from_millis(500);

#[cfg(unix)]
async fn send_via_ipc(
    sock_path: &Path,
    text: &str,
    thread_id: &str,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> Result<BridgeSendSummary> {
    tokio::time::timeout(
        IPC_SEND_TIMEOUT,
        send_via_ipc_inner(sock_path, text, thread_id, attachments),
    )
    .await
    .map_err(|_| anyhow!("IPC send timed out after {}s", IPC_SEND_TIMEOUT.as_secs()))?
}

#[cfg(unix)]
async fn send_via_ipc_inner(
    sock_path: &Path,
    text: &str,
    thread_id: &str,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> Result<BridgeSendSummary> {
    let mut stream = tokio::net::UnixStream::connect(sock_path)
        .await
        .with_context(|| format!("connecting to IPC socket {}", sock_path.display()))?;

    let mut payload = json!({
        "text": text,
        "thread_id": thread_id,
    });
    if !attachments.is_empty() {
        payload["attachments"] = serde_json::to_value(attachments)?;
    }
    let mut request = serde_json::to_vec(&payload)?;
    request.push(b'\n');
    stream.write_all(&request).await?;
    stream.shutdown().await?;

    let mut response_buf = Vec::new();
    stream.read_to_end(&mut response_buf).await?;
    let response: Value = serde_json::from_slice(&response_buf).context("parsing IPC response")?;

    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        let error = response
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown IPC error");
        bail!("daemon IPC error: {error}");
    }

    Ok(BridgeSendSummary {
        session_id: response
            .get("session_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        thread_id: response
            .get("thread_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        turn_id: response
            .get("turn_id")
            .and_then(Value::as_str)
            .context("IPC response missing turn_id")?
            .to_string(),
        turn_status: response
            .get("turn_status")
            .and_then(Value::as_str)
            .unwrap_or("inProgress")
            .to_string(),
    })
}

#[cfg(unix)]
async fn send_via_ipc_steer(
    sock_path: &Path,
    text: &str,
    thread_id: &str,
    expected_turn_id: &str,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> Result<()> {
    tokio::time::timeout(
        IPC_SEND_TIMEOUT,
        send_via_ipc_steer_inner(sock_path, text, thread_id, expected_turn_id, attachments),
    )
    .await
    .map_err(|_| anyhow!("IPC steer timed out after {}s", IPC_SEND_TIMEOUT.as_secs()))?
}

#[cfg(unix)]
async fn send_via_ipc_steer_inner(
    sock_path: &Path,
    text: &str,
    thread_id: &str,
    expected_turn_id: &str,
    attachments: &[crate::codex_attachments::AttachmentRef],
) -> Result<()> {
    let mut stream = tokio::net::UnixStream::connect(sock_path)
        .await
        .with_context(|| format!("connecting to IPC socket {}", sock_path.display()))?;

    let mut payload = json!({
        "kind": "steer",
        "text": text,
        "thread_id": thread_id,
        "expected_turn_id": expected_turn_id,
    });
    if !attachments.is_empty() {
        payload["attachments"] = serde_json::to_value(attachments)?;
    }
    let mut request = serde_json::to_vec(&payload)?;
    request.push(b'\n');
    stream.write_all(&request).await?;
    stream.shutdown().await?;

    let mut response_buf = Vec::new();
    stream.read_to_end(&mut response_buf).await?;
    let response: Value =
        serde_json::from_slice(&response_buf).context("parsing IPC steer response")?;

    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        let error = response
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown IPC steer error");
        bail!("daemon IPC steer error: {error}");
    }

    Ok(())
}

#[cfg(unix)]
async fn stop_via_ipc(sock_path: &Path, terminal_reason: &str) -> Result<()> {
    tokio::time::timeout(
        IPC_STOP_TIMEOUT,
        stop_via_ipc_inner(sock_path, terminal_reason),
    )
    .await
    .map_err(|_| anyhow!("IPC stop timed out after {}s", IPC_STOP_TIMEOUT.as_secs()))?
}

#[cfg(unix)]
fn parse_stop_ipc_response(response_buf: &[u8]) -> Result<()> {
    if response_buf.is_empty() || response_buf.iter().all(u8::is_ascii_whitespace) {
        // A stop request can race with the bridge tearing its IPC server down.
        // In that case the client sees a clean EOF instead of a JSON payload,
        // but the bridge is already exiting as requested. Treat that as
        // success so wrapper cleanup does not emit a fake error on normal
        // shutdown.
        return Ok(());
    }

    let response: Value = match serde_json::from_slice(response_buf) {
        Ok(value) => value,
        Err(err) => {
            if !response_buf.ends_with(b"\n") {
                // The bridge writes newline-terminated JSON responses. If the
                // socket closes mid-response during shutdown, prefer a clean
                // exit over surfacing a fake cleanup failure.
                warn!(
                    "codex-bridge stop IPC response ended without newline; treating shutdown race as success"
                );
                return Ok(());
            }
            return Err(err).context("parsing IPC response");
        }
    };

    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        let error = response
            .get("error")
            .and_then(Value::as_str)
            .unwrap_or("unknown IPC error");
        bail!("daemon IPC error: {error}");
    }

    Ok(())
}

#[cfg(unix)]
async fn stop_via_ipc_inner(sock_path: &Path, terminal_reason: &str) -> Result<()> {
    let mut stream = tokio::net::UnixStream::connect(sock_path)
        .await
        .with_context(|| format!("connecting to IPC socket {}", sock_path.display()))?;

    let mut request = serde_json::to_vec(&json!({
        "kind": "stop",
        "reason": normalize_bridge_terminal_reason(Some(terminal_reason)),
    }))?;
    request.push(b'\n');
    stream.write_all(&request).await?;
    stream.shutdown().await?;

    let mut response_buf = Vec::new();
    stream.read_to_end(&mut response_buf).await?;
    parse_stop_ipc_response(&response_buf)
}

#[cfg(unix)]
pub async fn cmd_codex_bridge_stop(config: BridgeStopConfig) -> Result<()> {
    let paths = resolve_bridge_paths(config.state_root.as_deref(), &config.session_id, None)?;
    let sock_path = ipc_socket_path(&paths.state_file);
    let terminal_reason = normalize_bridge_terminal_reason(config.terminal_reason.as_deref());
    if !paths.state_file.exists() {
        if sock_path.exists() {
            eprintln!(
                "bridge state file is missing for session {}; attempting IPC stop via existing socket",
                config.session_id
            );
            return stop_via_ipc(&sock_path, &terminal_reason).await;
        }
        eprintln!(
            "bridge state file is missing for session {}; no recorded child process to stop",
            config.session_id
        );
        return Ok(());
    }
    if sock_path.exists() {
        match stop_via_ipc(&sock_path, &terminal_reason).await {
            Ok(()) => return Ok(()),
            Err(err) => {
                eprintln!(
                    "bridge IPC stop failed for session {}; falling back to recorded child cleanup: {err:#}",
                    config.session_id
                );
            }
        }
    }
    let state = read_state_file(&paths.state_file)?;
    terminate_recorded_app_server(&state).await;
    remove_bridge_state_sidecars(&paths.state_file);
    Ok(())
}

#[cfg(not(unix))]
pub async fn cmd_codex_bridge_stop(_config: BridgeStopConfig) -> Result<()> {
    bail!("codex-bridge stop is only supported on unix platforms");
}

/// Send a mid-turn steer message to the Codex app-server.
///
/// On success: the app-server accepted the steer; Codex will weave the text
/// into the active turn. Transcript ingest happens via the usual hook
/// outbox path; callers should not block waiting for a reply here.
///
/// On failure: returns `BridgeSteerError::NoActiveTurn` if the bridge state
/// file reports no active turn id (the capability gate on the server side
/// can race with a natural turn completion). Returns `TurnEnded` when the
/// app-server error payload mentions turn-state issues. Otherwise wraps
/// the protocol error.
pub async fn cmd_codex_bridge_steer(
    config: BridgeSteerConfig,
) -> std::result::Result<(), BridgeSteerError> {
    let state = load_ready_state(&config.session_id, config.state_root.as_deref())
        .map_err(BridgeSteerError::Protocol)?;
    let thread_id = state
        .thread_id
        .clone()
        .ok_or(BridgeSteerError::MissingState("thread_id"))?;
    let turn_id = state
        .active_turn_id
        .clone()
        .ok_or(BridgeSteerError::NoActiveTurn)?;

    // Preferred path: route through the daemon's persistent app-server
    // connection via the IPC socket. Avoids per-call WS connect +
    // initialize_client on the hot path; keeps the app-server's per-thread
    // state consistent with the daemon's ongoing subscriptions.
    let paths = resolve_bridge_paths(config.state_root.as_deref(), &config.session_id, None)
        .map_err(BridgeSteerError::Protocol)?;
    let sock_path = ipc_socket_path(&paths.state_file);
    #[cfg(unix)]
    if sock_path.exists() {
        match send_via_ipc_steer(
            &sock_path,
            &config.text,
            &thread_id,
            &turn_id,
            &config.attachments,
        )
        .await
        {
            Ok(()) => return Ok(()),
            Err(err) => {
                let msg = format!("{err}");
                // IPC reported a protocol-shaped error from the app-server —
                // classify turn-state races the same way the direct path does.
                if classify_steer_error_as_turn_ended(&msg) {
                    return Err(BridgeSteerError::TurnEnded(msg));
                }
                // Only fall back to direct WS on connection failures to the
                // daemon itself; otherwise we'd risk a double-dispatch if
                // the daemon accepted the steer but the reply was lost.
                let is_connect_failure =
                    err.downcast_ref::<std::io::Error>()
                        .map_or(false, |io_err| {
                            matches!(
                                io_err.kind(),
                                std::io::ErrorKind::ConnectionRefused
                                    | std::io::ErrorKind::NotFound
                                    | std::io::ErrorKind::BrokenPipe
                            )
                        });
                if !is_connect_failure {
                    return Err(BridgeSteerError::Protocol(err.context(
                        "IPC steer may have been accepted by daemon; not retrying direct WS to avoid duplicate steer",
                    )));
                }
                eprintln!(
                    "[codex-bridge] IPC steer socket connect failed; falling back to direct WebSocket: {err}"
                );
            }
        }
    }

    // Fallback: direct WS (used when the daemon socket is missing or the
    // connect itself failed). Slower — full handshake per call. Direct WS
    // cannot fetch attachment blobs (no per-session tmpdir, no token), so
    // refuse rather than silently dropping the images.
    if !config.attachments.is_empty() {
        return Err(BridgeSteerError::Protocol(anyhow!(
            "direct WebSocket steer fallback does not support attachments; restart the bridge so the daemon IPC socket is available"
        )));
    }
    let ws_url = state
        .ws_url
        .clone()
        .ok_or(BridgeSteerError::MissingState("ws_url"))?;

    let mut client = connect_remote_client(&ws_url)
        .await
        .map_err(BridgeSteerError::Protocol)?;
    initialize_client(&mut client)
        .await
        .map_err(BridgeSteerError::Protocol)?;

    let send_result = send_request(
        &mut client,
        "turn/steer",
        json!({
            "threadId": thread_id,
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": config.text}],
        }),
    )
    .await;

    // Best-effort shutdown regardless of outcome.
    let _ = shutdown_child(&mut client).await;

    match send_result {
        Ok(_) => Ok(()),
        Err(err) => {
            let msg = format!("{err}");
            if classify_steer_error_as_turn_ended(&msg) {
                Err(BridgeSteerError::TurnEnded(msg))
            } else {
                Err(BridgeSteerError::Protocol(err))
            }
        }
    }
}

/// Decide whether a raw `turn/steer` error message from the Codex app-server
/// represents a turn-state race (the caller expected an active turn that had
/// already ended, been interrupted, or completed). Surfaced as a separate
/// signal so the backend can return a stable 409 without protocol coupling.
///
/// The classifier is deliberately specific — bare keywords like `expected`
/// would match generic protocol errors ("expected turn/steer response to
/// include X"), which would silently downgrade real bugs into a user-facing
/// "queue instead" prompt.
fn classify_steer_error_as_turn_ended(raw_error: &str) -> bool {
    let lower = raw_error.to_ascii_lowercase();
    if !lower.contains("turn") {
        return false;
    }
    // Phrases we've observed or can reasonably expect from the app-server
    // when the expected turn id is stale, interrupted, or already finished.
    lower.contains("expected turn id")
        || lower.contains("expectedturnid")
        || lower.contains("does not match active turn")
        || lower.contains("not active")
        || lower.contains("no active")
        || lower.contains("turn ended")
        || lower.contains("turn has already completed")
        || lower.contains("turn has already ended")
        || lower.contains("turn interrupted")
        || lower.contains("turn completed")
}

pub async fn cmd_codex_bridge_interrupt(config: BridgeInterruptConfig) -> Result<()> {
    let state = load_ready_state(&config.session_id, config.state_root.as_deref())?;
    let thread_id = state
        .thread_id
        .clone()
        .context("bridge state is missing thread_id")?;
    let turn_id = state
        .active_turn_id
        .clone()
        .context("bridge state does not have an active turn to interrupt")?;
    let ws_url = state
        .ws_url
        .clone()
        .context("bridge state is missing ws_url")?;

    let mut client = connect_remote_client(&ws_url).await?;
    initialize_client(&mut client).await?;
    let _ = send_request(
        &mut client,
        "turn/interrupt",
        json!({
            "threadId": thread_id,
            "turnId": turn_id,
        }),
    )
    .await?;
    shutdown_child(&mut client).await?;
    Ok(())
}

pub fn cmd_codex_bridge_attach(config: BridgeAttachConfig) -> Result<i32> {
    let state = load_ready_state(&config.session_id, config.state_root.as_deref())?;
    let thread_id = state
        .thread_id
        .clone()
        .context("bridge state is missing thread_id")?;
    let ws_url = state
        .ws_url
        .clone()
        .context("bridge state is missing ws_url")?;
    let codex_bin = config
        .codex_bin
        .clone()
        .unwrap_or_else(|| state.codex_bin.clone());

    let mut command = std::process::Command::new(&codex_bin);
    command
        .arg("-c")
        .arg(CODEX_DISABLE_UPDATE_CHECK_CONFIG)
        .arg("resume")
        .arg(&thread_id)
        .arg("--enable")
        .arg("tui_app_server")
        .arg("--remote")
        .arg(&ws_url)
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .current_dir(PathBuf::from(state.cwd));

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let err = command.exec();
        return Err(anyhow!("failed to exec {codex_bin}: {err}"));
    }

    #[cfg(not(unix))]
    {
        let status = command
            .status()
            .with_context(|| format!("running {codex_bin} resume --remote"))?;
        Ok(status.code().unwrap_or(1))
    }
}

fn resolve_bridge_paths(
    state_root_override: Option<&Path>,
    session_id: &str,
    log_file_override: Option<&Path>,
) -> Result<ResolvedBridgePaths> {
    let home = home_dir()?;
    let state_root = state_root_override
        .map(Path::to_path_buf)
        .unwrap_or_else(|| {
            home.join(".claude")
                .join("managed-local")
                .join("codex-bridge")
        });
    let state_file = state_root.join(format!("{session_id}.json"));
    let log_file = log_file_override
        .map(Path::to_path_buf)
        .unwrap_or_else(|| state_root.join(format!("{session_id}.log")));
    Ok(ResolvedBridgePaths {
        state_file,
        log_file,
    })
}

fn resolve_bridge_agent_db_path(longhouse_home_override: Option<&Path>) -> Result<PathBuf> {
    match longhouse_home_override {
        Some(home) => Ok(home.join("agent").join("longhouse-shipper.db")),
        None => crate::config::get_agent_db_path(),
    }
}

fn resolve_bridge_transcript_wake_socket_path(
    longhouse_home_override: Option<&Path>,
) -> Result<PathBuf> {
    match longhouse_home_override {
        Some(home) => Ok(home.join("agent").join("transcript-wake.sock")),
        None => crate::config::get_agent_transcript_wake_socket_path(),
    }
}

#[cfg(unix)]
fn wake_daemon_for_transcript(
    config: &BridgeRunConfig,
    thread_path: &str,
    phase: &str,
    wake_reason: &str,
    turn_id: Option<&str>,
) {
    let Ok(socket_path) =
        resolve_bridge_transcript_wake_socket_path(config.longhouse_home.as_deref())
    else {
        return;
    };
    if !socket_path.exists() {
        return;
    }
    let file_len_hint = fs::metadata(thread_path)
        .ok()
        .map(|metadata| metadata.len());
    let payload = json!({
        "provider": "codex",
        "path": thread_path,
        "phase": phase,
        "session_id": config.session_id,
        "turn_id": turn_id,
        "wake_reason": wake_reason,
        "observed_at_ms": Utc::now().timestamp_millis(),
        "file_len_hint": file_len_hint,
    });
    let Ok(mut stream) = std::os::unix::net::UnixStream::connect(&socket_path) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(50)));
    let _ = stream.write_all(payload.to_string().as_bytes());
}

#[cfg(not(unix))]
fn wake_daemon_for_transcript(
    _config: &BridgeRunConfig,
    _thread_path: &str,
    _phase: &str,
    _wake_reason: &str,
    _turn_id: Option<&str>,
) {
}

fn bridge_lock_path(state_file: &Path) -> PathBuf {
    state_file.with_extension("lock")
}

fn acquire_bridge_lock(lock_path: &Path) -> Result<()> {
    if let Some(parent) = lock_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(lock_path)
        .with_context(|| format!("opening bridge lock file {}", lock_path.display()))?;
    let lock = Box::leak(Box::new(fd_lock::RwLock::new(file)));
    match lock.try_write() {
        Ok(guard) => {
            // Leak the guard so the lock is held for the process lifetime.
            // The kernel releases it on exit via fd close.
            Box::leak(Box::new(guard));
            Ok(())
        }
        Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
            bail!(
                "another codex bridge already owns lock {}",
                lock_path.display()
            )
        }
        Err(err) => {
            Err(err).with_context(|| format!("acquiring exclusive lock on {}", lock_path.display()))
        }
    }
}

fn write_state_file(path: &Path, state: &BridgeStateFile) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut next = state.clone();
    next.updated_at = Utc::now().to_rfc3339();
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, serde_json::to_vec_pretty(&next)?)
        .with_context(|| format!("writing {}", tmp.display()))?;
    fs::rename(&tmp, path)
        .with_context(|| format!("renaming {} -> {}", tmp.display(), path.display()))?;
    Ok(())
}

fn read_state_file(path: &Path) -> Result<BridgeStateFile> {
    let bytes = fs::read(path).with_context(|| format!("reading {}", path.display()))?;
    let state = serde_json::from_slice::<BridgeStateFile>(&bytes)
        .with_context(|| format!("parsing {}", path.display()))?;
    Ok(state)
}

fn load_ready_state(
    session_id: &str,
    state_root_override: Option<&Path>,
) -> Result<BridgeStateFile> {
    let paths = resolve_bridge_paths(state_root_override, session_id, None)?;
    let state = read_state_file(&paths.state_file)?;
    if state.status != "ready" {
        bail!(
            "codex bridge session {session_id} is not ready (status={})",
            state.status
        );
    }
    Ok(state)
}

pub fn validate_codex_bridge_attached(
    session_id: &str,
    state_root_override: Option<&Path>,
) -> Result<()> {
    let state = load_ready_state(session_id, state_root_override)?;
    state
        .thread_id
        .as_deref()
        .filter(|thread_id| !thread_id.trim().is_empty())
        .context("bridge state is missing thread_id")?;
    Ok(())
}

fn read_log_tail(path: &Path, max_chars: usize) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    truncate_tail_chars(&text, max_chars)
}

async fn spawn_app_server_client(config: &BridgeRunConfig) -> Result<RpcClient> {
    let mut command = Command::new(&config.codex_bin);
    command.arg("-c").arg(CODEX_DISABLE_UPDATE_CHECK_CONFIG);
    if let Some(effort) = config.model_reasoning_effort.as_deref() {
        command
            .arg("-c")
            .arg(format!("model_reasoning_effort={effort}"));
    }
    if let Some(model) = config.model.as_deref() {
        command.arg("--model").arg(model);
    }
    if let Some(policy) = config.approval_policy.as_deref() {
        command.arg("--ask-for-approval").arg(policy);
    }
    if let Some(sandbox) = config.sandbox.as_deref() {
        command.arg("--sandbox").arg(sandbox);
    }
    command
        .arg("app-server")
        .arg("--listen")
        .arg("ws://127.0.0.1:0")
        .arg("--enable")
        .arg("hooks")
        .arg("--enable")
        .arg("exec_permission_approvals")
        .arg("--enable")
        .arg("request_permissions_tool");
    if let Some(src) = config.session_source.as_deref() {
        command.arg("--session-source").arg(src);
    }
    command
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .env("LONGHOUSE_HOOK_URL", &config.api_url)
        .env("LONGHOUSE_HOOK_TOKEN", &config.api_token)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    #[cfg(unix)]
    {
        unsafe {
            command.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }

    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{}` app-server", config.codex_bin))?;
    let child_pid = child.id();
    #[cfg(unix)]
    let child_pgid = child_pid
        .and_then(|pid| i32::try_from(pid).ok())
        .and_then(|pid| {
            let pgid = unsafe { libc::getpgid(pid) };
            if pgid > 0 {
                Some(pgid)
            } else {
                None
            }
        });
    #[cfg(not(unix))]
    let child_pgid = None;
    let stdout = child.stdout.take().context("missing app-server stdout")?;
    let stderr = child.stderr.take().context("missing app-server stderr")?;
    let (events_tx, events_rx) = mpsc::unbounded_channel();
    let stdout_tx = events_tx.clone();
    tokio::spawn(async move {
        let mut lines = BufReader::new(stdout).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            match serde_json::from_str::<Value>(&line) {
                Ok(value) => {
                    let _ = stdout_tx.send(StreamEvent::Rpc(value));
                }
                Err(err) => {
                    let _ = stdout_tx.send(StreamEvent::StdoutParseError(format!("{err}: {line}")));
                }
            }
        }
    });

    let stderr_tx = events_tx.clone();
    let (ws_listen_tx, ws_listen_rx) = oneshot::channel();
    tokio::spawn(async move {
        let mut maybe_ws_listen_tx = Some(ws_listen_tx);
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Some(url) = extract_websocket_listen_url(&line) {
                if let Some(tx) = maybe_ws_listen_tx.take() {
                    let _ = tx.send(url);
                }
            }
            let _ = stderr_tx.send(StreamEvent::Stderr(line));
        }
    });

    let upstream_ws_url = tokio::time::timeout(Duration::from_secs(10), ws_listen_rx)
        .await
        .context("timed out waiting for app-server websocket listener")?
        .context("app-server websocket listener never announced a URL")?;
    // Always put the backpressure relay in front of codex. Prevents codex's
    // internal mpsc(128) from filling under bursty streaming and killing the
    // WS. The same relay also fronts the remote TUI since ws_url gets written
    // to the state file. See engine/src/codex_ws_relay.rs.
    let ws_url = crate::codex_ws_relay::spawn(&upstream_ws_url)
        .await
        .with_context(|| format!("spawning codex WS relay in front of {upstream_ws_url}"))?;
    let (ws_stream, _response) = connect_async(ws_url.as_str())
        .await
        .with_context(|| format!("connecting bridge client to {ws_url}"))?;
    let (mut ws_write, mut ws_read) = ws_stream.split();
    let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
    tokio::spawn(async move {
        while let Some(line) = outbound_rx.recv().await {
            if ws_write.send(Message::Text(line.into())).await.is_err() {
                break;
            }
        }
        let _ = ws_write.close().await;
    });
    let ws_events_tx = events_tx.clone();
    tokio::spawn(async move {
        while let Some(message) = ws_read.next().await {
            match message {
                Ok(Message::Text(text)) => match serde_json::from_str::<Value>(&text) {
                    Ok(value) => {
                        let _ = ws_events_tx.send(StreamEvent::Rpc(value));
                    }
                    Err(err) => {
                        let _ = ws_events_tx
                            .send(StreamEvent::StdoutParseError(format!("{err}: {text}")));
                    }
                },
                Ok(Message::Close(_)) => break,
                Ok(_) => {}
                Err(err) => {
                    let _ = ws_events_tx
                        .send(StreamEvent::Stderr(format!("websocket read error: {err}")));
                    break;
                }
            }
        }
    });

    Ok(RpcClient {
        child: Some(child),
        child_pid,
        child_pgid,
        child_ws_url: Some(upstream_ws_url),
        outbound: RpcOutbound::WebSocket(outbound_tx),
        events_rx,
        pending_methods: BTreeMap::new(),
        next_request_id: 1,
        ws_url,
    })
}

async fn connect_remote_client(ws_url: &str) -> Result<RpcClient> {
    let (ws_stream, _response) = connect_async(ws_url)
        .await
        .with_context(|| format!("connecting remote bridge client to {ws_url}"))?;
    let (mut ws_write, mut ws_read) = ws_stream.split();
    let (events_tx, events_rx) = mpsc::unbounded_channel();
    let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
    tokio::spawn(async move {
        while let Some(line) = outbound_rx.recv().await {
            if ws_write.send(Message::Text(line.into())).await.is_err() {
                break;
            }
        }
        let _ = ws_write.close().await;
    });
    let ws_events_tx = events_tx.clone();
    tokio::spawn(async move {
        while let Some(message) = ws_read.next().await {
            match message {
                Ok(Message::Text(text)) => match serde_json::from_str::<Value>(&text) {
                    Ok(value) => {
                        let _ = ws_events_tx.send(StreamEvent::Rpc(value));
                    }
                    Err(err) => {
                        let _ = ws_events_tx
                            .send(StreamEvent::StdoutParseError(format!("{err}: {text}")));
                    }
                },
                Ok(Message::Close(_)) => break,
                Ok(_) => {}
                Err(err) => {
                    let _ = ws_events_tx
                        .send(StreamEvent::Stderr(format!("websocket read error: {err}")));
                    break;
                }
            }
        }
    });

    Ok(RpcClient {
        child: None,
        child_pid: None,
        child_pgid: None,
        child_ws_url: None,
        outbound: RpcOutbound::WebSocket(outbound_tx),
        events_rx,
        pending_methods: BTreeMap::new(),
        next_request_id: 1,
        ws_url: ws_url.to_string(),
    })
}

async fn initialize_client(client: &mut RpcClient) -> Result<()> {
    let _ = send_request(
        client,
        "initialize",
        json!({
            "clientInfo": {
                "name": "longhouse_codex_bridge",
                "title": "Longhouse Codex Bridge",
                "version": env!("CARGO_PKG_VERSION"),
            },
            "capabilities": {
                "experimentalApi": true,
                "optOutNotificationMethods": BRIDGE_OPT_OUT_NOTIFICATION_METHODS,
            }
        }),
    )
    .await?;
    send_notification(client, "initialized", json!({})).await
}

async fn send_notification(client: &mut RpcClient, method: &str, params: Value) -> Result<()> {
    let payload = json!({
        "method": method,
        "params": params,
    });
    send_payload(client, &payload).await
}

async fn send_payload(client: &mut RpcClient, payload: &Value) -> Result<()> {
    let line = serde_json::to_string(payload)?;
    match &mut client.outbound {
        RpcOutbound::WebSocket(tx) => {
            tx.send(line)
                .map_err(|_| anyhow!("codex bridge websocket outbound channel closed"))?;
        }
    }
    Ok(())
}

async fn send_request(client: &mut RpcClient, method: &str, params: Value) -> Result<Value> {
    let request_id = client.next_request_id;
    client.next_request_id += 1;
    client
        .pending_methods
        .insert(request_id, method.to_string());
    let payload = json!({
        "id": request_id,
        "method": method,
        "params": params,
    });
    send_payload(client, &payload).await?;

    loop {
        let event = recv_event(client).await?;
        match event {
            StreamEvent::Rpc(value) => {
                if value.get("id").is_some() && value.get("method").is_some() {
                    bail!("received unexpected server request while waiting for {method}: {value}");
                }
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    let method_name = client
                        .pending_methods
                        .remove(&id)
                        .unwrap_or_else(|| format!("request#{id}"));
                    if id == request_id {
                        if let Some(error) = value.get("error") {
                            bail!("{method_name} failed: {error}");
                        }
                        return value
                            .get("result")
                            .cloned()
                            .ok_or_else(|| anyhow!("response for {method_name} missing result"));
                    }
                    continue;
                }
            }
            StreamEvent::Stderr(line) => {
                eprintln!("[codex-bridge] app-server: {line}");
            }
            StreamEvent::StdoutParseError(detail) => {
                bail!("codex bridge protocol parse error while waiting for {method}: {detail}");
            }
        }
    }
}

async fn send_request_with_runtime(
    client: &mut RpcClient,
    method: &str,
    params: Value,
    config: &BridgeRunConfig,
    context: &mut BridgeContext,
) -> Result<Value> {
    let request_id = client.next_request_id;
    client.next_request_id += 1;
    client
        .pending_methods
        .insert(request_id, method.to_string());
    let payload = json!({
        "id": request_id,
        "method": method,
        "params": params,
    });
    send_payload(client, &payload).await?;

    loop {
        let event = recv_event(client).await?;
        match event {
            StreamEvent::Rpc(value) => {
                if value.get("id").is_some() && value.get("method").is_some() {
                    handle_server_request(config, value, client, context).await?;
                    continue;
                }
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    let method_name = client
                        .pending_methods
                        .remove(&id)
                        .unwrap_or_else(|| format!("request#{id}"));
                    if id == request_id {
                        if let Some(error) = value.get("error") {
                            bail!("{method_name} failed: {error}");
                        }
                        return value
                            .get("result")
                            .cloned()
                            .ok_or_else(|| anyhow!("response for {method_name} missing result"));
                    }
                    continue;
                }
                let _ = process_notification(&value, config, context).await?;
            }
            StreamEvent::Stderr(line) => {
                eprintln!("[codex-bridge] app-server: {line}");
            }
            StreamEvent::StdoutParseError(detail) => {
                bail!("codex bridge protocol parse error while waiting for {method}: {detail}");
            }
        }
    }
}

async fn recv_event(client: &mut RpcClient) -> Result<StreamEvent> {
    match client.events_rx.recv().await {
        Some(event) => Ok(event),
        None => {
            if let Some(ref mut child) = client.child {
                if let Some(status) = child.try_wait()? {
                    bail!("codex app-server exited early with status {status}");
                }
            }
            bail!("codex app-server closed its output stream unexpectedly");
        }
    }
}

async fn handle_server_request(
    config: &BridgeRunConfig,
    value: Value,
    client: &mut RpcClient,
    context: &mut BridgeContext,
) -> Result<()> {
    let method = value
        .get("method")
        .and_then(Value::as_str)
        .context("server request missing method")?;
    let request_id = value
        .get("id")
        .cloned()
        .context("server request missing id")?;
    let params = value.get("params").cloned().unwrap_or(Value::Null);

    if let Some(update) = context
        .runtime_tracker
        .handle_server_request(method, &params)
    {
        emit_runtime_updates(config, context, vec![update]).await;
    }

    let result = match method {
        "item/commandExecution/requestApproval" => json!({
            "decision": if config.auto_approve { "accept" } else { "decline" }
        }),
        "item/fileChange/requestApproval" => json!({
            "decision": if config.auto_approve { "accept" } else { "decline" }
        }),
        "item/permissions/requestApproval" => json!({
            "scope": "turn",
            "permissions": if config.auto_approve {
                params.get("permissions").cloned().unwrap_or_else(|| json!({}))
            } else {
                json!({})
            }
        }),
        "item/tool/requestUserInput" => json!({
            "answers": build_request_user_input_answers(&params, config.auto_approve)
        }),
        "mcpServer/elicitation/request" => json!({
            "action": "decline",
            "content": Value::Null,
        }),
        "applyPatchApproval" | "execCommandApproval" => json!({
            "decision": if config.auto_approve { "Approved" } else { "Denied" }
        }),
        other => bail!("unsupported server request in codex bridge: {other}"),
    };

    let payload = json!({
        "id": request_id,
        "result": result,
    });
    send_payload(client, &payload).await
}

async fn process_notification(
    value: &Value,
    config: &BridgeRunConfig,
    context: &mut BridgeContext,
) -> Result<Option<BridgeFollowup>> {
    let Some(method) = value.get("method").and_then(Value::as_str) else {
        return Ok(None);
    };
    let params = value.get("params").cloned().unwrap_or(Value::Null);
    let mut followup = None;
    match method {
        "thread/started" => {
            if notification_thread_is_subagent(&params) {
                if let Some(id) = extract_notification_thread_id(&params) {
                    eprintln!("[codex-bridge] ignoring Codex subagent thread candidate: {id}");
                }
                return Ok(None);
            }
            let previous_thread_id = context.state.thread_id.clone();
            if adopt_thread_identity(
                config,
                context,
                extract_notification_thread_id(&params),
                extract_notification_thread_path(&params),
                false,
            )? {
                emit_runtime_updates(
                    config,
                    context,
                    vec![context.runtime_tracker.current_phase_update()],
                )
                .await;
                if let Some(id) = context.state.thread_id.as_deref() {
                    if previous_thread_id.as_deref() != Some(id) {
                        eprintln!("[codex-bridge] TUI thread candidate: {id}");
                    }
                }
            }
            if followup.is_none() {
                followup = pending_thread_subscription(context)?;
            }
        }
        "turn/started" | "item/started" | "item/completed" | "thread/status/changed" => {
            if notification_is_for_different_thread(&params, context) {
                return Ok(None);
            }
            let _ = adopt_thread_identity(
                config,
                context,
                extract_notification_thread_id(&params),
                extract_notification_thread_path(&params),
                false,
            )?;
            if method == "turn/started" {
                context.state.active_turn_id = extract_string(&params, &["turn", "id"]);
                context.state.last_turn_status = extract_string(&params, &["turn", "status"]);
                context.live_transcript_seq = 0;
                context.live_transcript_text.clear();
                write_state_file(&context.state_file, &context.state)?;
                if let Some(path) = context.state.thread_path.as_deref() {
                    wake_daemon_for_transcript(
                        config,
                        path,
                        "running",
                        "turn_started",
                        context.state.active_turn_id.as_deref(),
                    );
                }
            }
            let updates = context.runtime_tracker.handle_notification(method, &params);
            emit_runtime_updates(config, context, updates).await;
            if followup.is_none() {
                followup = pending_thread_subscription(context)?;
            }
        }
        "item/agentMessage/delta"
        | "item/commandExecution/outputDelta"
        | "command/exec/outputDelta"
        | "item/fileChange/outputDelta"
        | "item/mcpToolCall/progress" => {
            if notification_is_for_different_thread(&params, context) {
                return Ok(None);
            }
            if let Some(delta) = extract_live_transcript_delta(method, &params) {
                context.live_transcript_seq += 1;
                context.live_transcript_text.push_str(delta);
                context
                    .runtime
                    .post_live_transcript_delta(
                        method,
                        delta,
                        context.state.active_turn_id.as_deref(),
                        context.live_transcript_seq,
                        &context.live_transcript_text,
                    )
                    .await;
            }
            let updates = context.runtime_tracker.handle_notification(method, &params);
            emit_runtime_updates(config, context, updates).await;
        }
        "turn/completed" => {
            if notification_is_for_different_thread(&params, context) {
                return Ok(None);
            }
            let completed_turn_id = context.state.active_turn_id.clone();
            if let Some(path) = context.state.thread_path.as_deref() {
                if let Some(text) = latest_assistant_text_from_rollout_for_completion(
                    Path::new(path),
                    &context.live_transcript_text,
                )
                .await
                {
                    context.live_transcript_text = text;
                }
            }
            if !context.live_transcript_text.is_empty() {
                context.live_transcript_seq += 1;
                context
                    .runtime
                    .post_live_transcript_completed(
                        completed_turn_id.as_deref(),
                        context.live_transcript_seq,
                        &context.live_transcript_text,
                    )
                    .await;
            }
            context.state.last_turn_status = extract_string(&params, &["turn", "status"]);
            context.state.active_turn_id = None;
            context.live_transcript_text.clear();
            write_state_file(&context.state_file, &context.state)?;
            if let Some(path) = context.state.thread_path.as_deref() {
                wake_daemon_for_transcript(
                    config,
                    path,
                    "idle",
                    "turn_completed",
                    completed_turn_id.as_deref(),
                );
            }
            let updates = context.runtime_tracker.handle_notification(method, &params);
            emit_runtime_updates(config, context, updates).await;
            // Daemon handles shipping — no per-turn ship needed.
        }
        "hook/completed" => {
            if let Some(summary) = params.get("summary") {
                if let Some(event_name) = summary.get("eventName").and_then(Value::as_str) {
                    eprintln!("[codex-bridge] hook completed: {event_name}");
                }
            }
        }
        _ => {}
    }
    Ok(followup)
}

fn thread_rollout_is_ready(path: &str) -> bool {
    std::fs::metadata(path)
        .map(|metadata| metadata.is_file() && metadata.len() > 0)
        .unwrap_or(false)
}

fn derive_thread_subscription_status(context: &BridgeContext) -> ThreadSubscriptionStatus {
    let Some(thread_id) = context.state.thread_id.as_deref() else {
        return ThreadSubscriptionStatus::WaitingForThread;
    };
    if context.subscribed_thread_id.as_deref() == Some(thread_id) {
        return ThreadSubscriptionStatus::Subscribed;
    }

    let has_turn_activity =
        context.state.active_turn_id.is_some() || context.state.last_turn_status.is_some();
    let rollout_ready = context
        .state
        .thread_path
        .as_deref()
        .map(thread_rollout_is_ready)
        .unwrap_or(false);

    if rollout_ready || (context.state.thread_path.is_none() && has_turn_activity) {
        ThreadSubscriptionStatus::ReadyToSubscribe
    } else if has_turn_activity {
        ThreadSubscriptionStatus::WaitingForRollout
    } else {
        ThreadSubscriptionStatus::WaitingForTurn
    }
}

fn update_thread_subscription_tracking(
    context: &mut BridgeContext,
    status: ThreadSubscriptionStatus,
    last_error: Option<String>,
) -> Result<()> {
    let next_status = Some(status.as_str().to_string());
    let next_error = normalize_optional_string(last_error);
    if context.state.thread_subscription_status == next_status
        && context.state.thread_subscription_last_error == next_error
    {
        return Ok(());
    }
    context.state.thread_subscription_status = next_status;
    context.state.thread_subscription_last_error = next_error;
    write_state_file(&context.state_file, &context.state)
}

fn pending_thread_subscription(context: &mut BridgeContext) -> Result<Option<BridgeFollowup>> {
    if let Some(thread_id) = context.state.thread_id.as_deref() {
        if context.rejected_thread_ids.contains(thread_id) {
            return Ok(None);
        }
    }

    let status = derive_thread_subscription_status(context);
    update_thread_subscription_tracking(
        context,
        status,
        context.state.thread_subscription_last_error.clone(),
    )?;
    if status != ThreadSubscriptionStatus::ReadyToSubscribe {
        return Ok(None);
    }
    let thread_id = context
        .state
        .thread_id
        .clone()
        .context("thread subscription requested without thread id")?;
    Ok(Some(BridgeFollowup::SubscribeThread {
        thread_id,
        thread_path: context.state.thread_path.clone(),
    }))
}

fn is_retryable_thread_subscription_error(error_text: &str) -> bool {
    error_text.contains("no rollout found for thread id")
        || (error_text.contains("failed to load rollout") && error_text.contains("is empty"))
}

fn extract_notification_thread_id(params: &Value) -> Option<String> {
    extract_string(params, &["thread", "id"])
        .or_else(|| extract_string(params, &["threadId"]))
        .or_else(|| extract_string(params, &["thread_id"]))
}

fn extract_notification_thread_path(params: &Value) -> Option<String> {
    extract_string(params, &["thread", "path"])
        .or_else(|| extract_string(params, &["threadPath"]))
        .or_else(|| extract_string(params, &["thread_path"]))
        .or_else(|| extract_string(params, &["path"]))
}

fn notification_thread_is_subagent(params: &Value) -> bool {
    params
        .get("thread")
        .map(codex_thread_value_is_subagent)
        .unwrap_or(false)
}

fn notification_is_for_different_thread(params: &Value, context: &BridgeContext) -> bool {
    let Some(current_id) = context.state.thread_id.as_deref() else {
        return false;
    };
    let Some(next_id) = extract_notification_thread_id(params) else {
        return false;
    };
    if next_id == current_id {
        return false;
    }
    eprintln!("[codex-bridge] ignoring notification for non-primary Codex thread: {next_id}");
    true
}

fn normalize_optional_string(value: Option<String>) -> Option<String> {
    value.and_then(|raw| {
        let trimmed = raw.trim();
        (!trimmed.is_empty()).then(|| trimmed.to_string())
    })
}

fn normalize_binding_path(path: &str) -> String {
    std::fs::canonicalize(path)
        .unwrap_or_else(|_| PathBuf::from(path))
        .to_string_lossy()
        .to_string()
}

fn sync_thread_binding(
    config: &BridgeRunConfig,
    old_path: Option<&str>,
    new_path: Option<&str>,
    session_id: &str,
) {
    let old_canonical = old_path.map(normalize_binding_path);
    let new_canonical = new_path.map(normalize_binding_path);
    if old_canonical == new_canonical {
        return;
    }

    match resolve_bridge_agent_db_path(config.longhouse_home.as_deref())
        .and_then(|db_path| crate::state::db::open_db(Some(&db_path)))
    {
        Ok(conn) => {
            let sb = crate::state::session_binding::SessionBinding::new(&conn);
            if let Some(old) = old_canonical.as_deref() {
                if let Err(e) = sb.unbind(old) {
                    eprintln!("[codex-bridge] session_binding clear failed: {e}");
                }
            }
            if let Some(new) = new_canonical.as_deref() {
                if let Err(e) = sb.bind(new, session_id, "codex") {
                    eprintln!("[codex-bridge] session_binding seed failed: {e}");
                }
                wake_daemon_for_transcript(config, new, "running", "binding", None);
            }
        }
        Err(e) => eprintln!("[codex-bridge] open shipper DB for binding: {e}"),
    }
}

fn thread_subscription_locked(context: &BridgeContext) -> bool {
    matches!(
        (
            context.state.thread_id.as_deref(),
            context.subscribed_thread_id.as_deref(),
        ),
        (Some(thread_id), Some(subscribed_thread_id)) if thread_id == subscribed_thread_id
    )
}

fn adopt_thread_identity(
    config: &BridgeRunConfig,
    context: &mut BridgeContext,
    next_id: Option<String>,
    next_path: Option<String>,
    allow_replace_locked: bool,
) -> Result<bool> {
    let next_id = normalize_optional_string(next_id);
    let next_path = normalize_optional_string(next_path);
    if next_id.is_none() && next_path.is_none() {
        return Ok(false);
    }

    let current_id = context.state.thread_id.clone();
    let current_path = context.state.thread_path.clone();
    let locked = thread_subscription_locked(context);

    let should_replace_id = match next_id.as_deref() {
        Some(next_id) => match current_id.as_deref() {
            None => true,
            Some(current_id) if current_id == next_id => false,
            Some(_) if allow_replace_locked => true,
            Some(_) => !locked,
        },
        None => false,
    };

    let mut desired_id = current_id.clone();
    let mut desired_path = current_path.clone();

    if should_replace_id {
        desired_id = next_id.clone();
        desired_path = next_path.clone();
    } else if let Some(next_path) = next_path.clone() {
        let path_belongs_to_current_thread =
            next_id.is_none() || current_id.as_deref() == next_id.as_deref();
        let can_replace_locked_path = allow_replace_locked || !locked || current_path.is_none();
        if path_belongs_to_current_thread
            && can_replace_locked_path
            && desired_path.as_deref() != Some(next_path.as_str())
        {
            desired_path = Some(next_path);
        }
    }

    if desired_id == current_id && desired_path == current_path {
        return Ok(false);
    }
    if desired_path != current_path {
        if let Some(path) = desired_path.as_deref() {
            if codex_rollout_file_is_subagent(Path::new(path)) {
                eprintln!(
                    "[codex-bridge] refusing to adopt Codex subagent rollout as managed primary: {path}"
                );
                return Ok(false);
            }
        }
    }

    let old_path = current_path.clone();
    context.state.thread_id = desired_id.clone();
    context.state.thread_path = desired_path.clone();
    context.runtime.thread_id = desired_id.clone();
    if desired_id != current_id {
        context.subscribed_thread_id = None;
        context.state.thread_subscription_attempts = 0;
        context.state.thread_subscription_last_error = None;
        context.state.thread_subscription_status = Some(
            derive_thread_subscription_status(context)
                .as_str()
                .to_string(),
        );
    }
    sync_thread_binding(
        config,
        old_path.as_deref(),
        desired_path.as_deref(),
        &context.state.session_id,
    );
    write_state_file(&context.state_file, &context.state)?;
    Ok(true)
}

impl CodexRuntimeTracker {
    fn handle_server_request(
        &mut self,
        method: &str,
        _params: &Value,
    ) -> Option<BridgeRuntimeUpdate> {
        self.attention_state = match method {
            "item/commandExecution/requestApproval" | "execCommandApproval" => {
                Some(CodexAttentionState::Approval {
                    tool_name: Some("shell".to_string()),
                })
            }
            "item/fileChange/requestApproval" | "applyPatchApproval" => {
                Some(CodexAttentionState::Approval {
                    tool_name: Some("edit".to_string()),
                })
            }
            "item/permissions/requestApproval" => {
                Some(CodexAttentionState::Approval { tool_name: None })
            }
            "item/tool/requestUserInput" | "mcpServer/elicitation/request" => {
                Some(CodexAttentionState::UserInput)
            }
            _ => return None,
        };
        Some(self.current_phase_update())
    }

    fn handle_notification(&mut self, method: &str, params: &Value) -> Vec<BridgeRuntimeUpdate> {
        match method {
            "turn/started" => {
                self.active_turn_id = extract_string(params, &["turn", "id"]);
                self.attention_state = None;
                self.active_items.clear();
                vec![self.current_phase_update()]
            }
            "turn/completed" => {
                self.active_turn_id = None;
                self.attention_state = None;
                self.active_items.clear();
                vec![self.current_phase_update()]
            }
            "item/started" => self
                .track_started_item(params)
                .map(|update| vec![update])
                .unwrap_or_default(),
            "item/completed" => self
                .track_completed_item(params)
                .map(|update| vec![update])
                .unwrap_or_default(),
            "thread/status/changed" => self
                .track_thread_status(params)
                .map(|update| vec![update])
                .unwrap_or_default(),
            "item/agentMessage/delta"
            | "item/commandExecution/outputDelta"
            | "command/exec/outputDelta"
            | "item/fileChange/outputDelta"
            | "item/mcpToolCall/progress" => vec![BridgeRuntimeUpdate::Progress],
            _ => Vec::new(),
        }
    }

    fn track_started_item(&mut self, params: &Value) -> Option<BridgeRuntimeUpdate> {
        let item = params.get("item")?;
        let item_id = item.get("id").and_then(Value::as_str)?.to_string();
        let item_type = item.get("type").and_then(Value::as_str)?.to_string();
        if !item_supports_runtime_tracking(item_type.as_str(), item) {
            return None;
        }
        let tool_name = tracked_item_tool_name(item_type.as_str(), item);
        self.next_item_sequence += 1;
        self.active_items.insert(
            item_id,
            ActiveCodexItem {
                item_type,
                tool_name,
                sequence: self.next_item_sequence,
            },
        );
        self.attention_state = None;
        Some(self.current_phase_update())
    }

    fn track_completed_item(&mut self, params: &Value) -> Option<BridgeRuntimeUpdate> {
        let item = params.get("item")?;
        let item_id = item.get("id").and_then(Value::as_str)?;
        if self.active_items.remove(item_id).is_none() {
            return None;
        }
        Some(self.current_phase_update())
    }

    fn track_thread_status(&mut self, params: &Value) -> Option<BridgeRuntimeUpdate> {
        match extract_thread_status_type(params)?.as_str() {
            "idle" => {
                self.active_turn_id = None;
                self.attention_state = None;
                self.active_items.clear();
                Some(self.current_phase_update())
            }
            "active" => {
                let flags = extract_thread_active_flags(params);
                if flags.iter().any(|flag| flag == "waitingOnApproval") {
                    let existing_tool = match self.attention_state.as_ref() {
                        Some(CodexAttentionState::Approval { tool_name }) => tool_name.clone(),
                        _ => None,
                    };
                    self.attention_state = Some(CodexAttentionState::Approval {
                        tool_name: self.primary_running_tool().or(existing_tool),
                    });
                } else if flags.iter().any(|flag| flag == "waitingOnUserInput") {
                    self.attention_state = Some(CodexAttentionState::UserInput);
                } else {
                    self.attention_state = None;
                }
                Some(self.current_phase_update())
            }
            _ => None,
        }
    }

    fn current_phase_update(&self) -> BridgeRuntimeUpdate {
        if let Some(attention_state) = self.attention_state.as_ref() {
            return match attention_state {
                CodexAttentionState::Approval { tool_name } => BridgeRuntimeUpdate::Phase {
                    phase: "blocked",
                    tool_name: tool_name.clone(),
                },
                CodexAttentionState::UserInput => BridgeRuntimeUpdate::Phase {
                    phase: "needs_user",
                    tool_name: None,
                },
            };
        }
        if let Some(tool_name) = self.primary_running_tool() {
            return BridgeRuntimeUpdate::Phase {
                phase: "running",
                tool_name: Some(tool_name),
            };
        }
        if self.active_turn_id.is_some() {
            return BridgeRuntimeUpdate::Phase {
                phase: "thinking",
                tool_name: None,
            };
        }
        BridgeRuntimeUpdate::Phase {
            phase: "idle",
            tool_name: None,
        }
    }

    fn keepalive_update(&self) -> Option<BridgeRuntimeUpdate> {
        match self.current_phase_update() {
            BridgeRuntimeUpdate::Phase { phase, tool_name }
                if matches!(phase, "thinking" | "running") =>
            {
                Some(BridgeRuntimeUpdate::Phase { phase, tool_name })
            }
            _ => None,
        }
    }

    fn primary_running_tool(&self) -> Option<String> {
        self.active_items
            .values()
            .max_by_key(|item| item.sequence)
            .and_then(|item| item.tool_name.clone())
    }
}

fn extract_live_transcript_delta<'a>(method: &str, params: &'a Value) -> Option<&'a str> {
    match method {
        "item/agentMessage/delta" => params.get("delta").and_then(Value::as_str),
        _ => None,
    }
}

fn latest_assistant_text_from_rollout(path: &Path) -> Option<String> {
    let contents = fs::read_to_string(path).ok()?;
    for line in contents.lines().rev() {
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if let Some(text) = assistant_text_from_rollout_event(&value) {
            return Some(text);
        }
    }
    None
}

async fn latest_assistant_text_from_rollout_for_completion(
    path: &Path,
    current_live_text: &str,
) -> Option<String> {
    let mut best = None;
    for attempt in 0..5 {
        if let Some(text) = latest_assistant_text_from_rollout(path) {
            let is_better = text.len() >= current_live_text.len()
                && (current_live_text.is_empty() || text.starts_with(current_live_text));
            if is_better {
                return Some(text);
            }
            best = Some(text);
        }
        if attempt < 4 {
            tokio::time::sleep(Duration::from_millis(40)).await;
        }
    }
    if current_live_text.is_empty() {
        return best;
    }
    None
}

fn assistant_text_from_rollout_event(value: &Value) -> Option<String> {
    let payload = value.get("payload")?;
    match payload.get("type").and_then(Value::as_str) {
        Some("agent_message") => {
            let text = payload.get("message").and_then(Value::as_str)?.trim();
            (!text.is_empty()).then(|| text.to_string())
        }
        Some("message") if payload.get("role").and_then(Value::as_str) == Some("assistant") => {
            let mut out = String::new();
            for item in payload.get("content").and_then(Value::as_array)? {
                if item.get("type").and_then(Value::as_str) != Some("output_text") {
                    continue;
                }
                if let Some(text) = item.get("text").and_then(Value::as_str) {
                    out.push_str(text);
                }
            }
            let out = out.trim().to_string();
            (!out.is_empty()).then_some(out)
        }
        _ => None,
    }
}

fn item_supports_runtime_tracking(item_type: &str, item: &Value) -> bool {
    match item_type {
        "commandExecution"
        | "fileChange"
        | "mcpToolCall"
        | "dynamicToolCall"
        | "collabAgentToolCall" => item
            .get("status")
            .and_then(Value::as_str)
            .is_some_and(|status| status == "inProgress"),
        _ => false,
    }
}

fn tracked_item_tool_name(item_type: &str, item: &Value) -> Option<String> {
    match item_type {
        "commandExecution" => Some("shell".to_string()),
        "fileChange" => Some("edit".to_string()),
        "mcpToolCall" | "dynamicToolCall" | "collabAgentToolCall" => {
            extract_string(item, &["tool"])
        }
        _ => None,
    }
}

fn extract_thread_status_type(params: &Value) -> Option<String> {
    extract_string(params, &["status", "type"])
        .or_else(|| extract_string(params, &["thread", "status", "type"]))
}

fn extract_thread_active_flags(params: &Value) -> Vec<String> {
    let status = params
        .get("status")
        .or_else(|| params.get("thread").and_then(|thread| thread.get("status")));
    status
        .and_then(|value| value.get("activeFlags"))
        .and_then(Value::as_array)
        .map(|flags| {
            flags
                .iter()
                .filter_map(|value| value.as_str().map(ToString::to_string))
                .collect()
        })
        .unwrap_or_default()
}

async fn emit_runtime_updates(
    config: &BridgeRunConfig,
    context: &mut BridgeContext,
    updates: Vec<BridgeRuntimeUpdate>,
) {
    for update in updates {
        match update {
            BridgeRuntimeUpdate::Phase { phase, tool_name } => {
                context
                    .runtime
                    .post_phase(
                        phase,
                        format!(
                            "bridge:{phase}:{}:{}",
                            context.state.session_id,
                            Uuid::new_v4()
                        ),
                        tool_name,
                    )
                    .await;
            }
            BridgeRuntimeUpdate::Progress => {
                if let Some(path) = context.state.thread_path.as_deref() {
                    wake_daemon_for_transcript(
                        config,
                        path,
                        "running",
                        "progress",
                        context.state.active_turn_id.as_deref(),
                    );
                }
                if should_emit_progress(context.last_progress_emit, DEFAULT_PROGRESS_THROTTLE_MS) {
                    context.last_progress_emit = Some(Instant::now());
                    context
                        .runtime
                        .post_progress(format!(
                            "bridge:progress:{}:{}",
                            context.state.session_id,
                            Uuid::new_v4()
                        ))
                        .await;
                }
            }
        }
    }
}

async fn emit_runtime_keepalive(config: &BridgeRunConfig, context: &mut BridgeContext) {
    if let Some(update) = context.runtime_tracker.keepalive_update() {
        emit_runtime_updates(config, context, vec![update]).await;
    }
}

fn spawn_runtime_event_worker(
    sink: BridgeRuntimeSink,
    mut rx: mpsc::UnboundedReceiver<Vec<Value>>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some(first_batch) = rx.recv().await {
            let mut batches = vec![first_batch];
            while let Ok(next_batch) = rx.try_recv() {
                batches.push(next_batch);
            }
            let events = coalesce_runtime_event_batches(batches);
            sink.post_runtime_events_blocking(events).await;
        }
    })
}

fn spawn_live_runtime_event_worker(
    sink: BridgeRuntimeSink,
    mut rx: mpsc::UnboundedReceiver<Vec<Value>>,
) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        while let Some(first_batch) = rx.recv().await {
            let mut batches = vec![first_batch];
            while let Ok(next_batch) = rx.try_recv() {
                batches.push(next_batch);
            }
            let events = coalesce_runtime_event_batches(batches);
            sink.post_runtime_events_live(events).await;
        }
    })
}

fn coalesce_runtime_event_batches(batches: Vec<Vec<Value>>) -> Vec<Value> {
    let mut events = Vec::new();
    let mut live_events: BTreeMap<String, Value> = BTreeMap::new();

    for batch in batches {
        for event in batch {
            if let Some(key) = live_transcript_event_key(&event) {
                let next_seq = live_transcript_event_seq(&event);
                let should_replace = live_events
                    .get(&key)
                    .map(|existing| next_seq >= live_transcript_event_seq(existing))
                    .unwrap_or(true);
                if should_replace {
                    live_events.insert(key, event);
                }
            } else {
                events.push(event);
            }
        }
    }

    events.extend(live_events.into_values());
    events
}

fn live_transcript_event_key(event: &Value) -> Option<String> {
    if event.get("source").and_then(Value::as_str) != Some("codex_bridge_live") {
        return None;
    }
    let payload = event.get("payload")?;
    if payload.get("progress_kind").and_then(Value::as_str) != Some("bridge_live_transcript_delta")
    {
        return None;
    }
    let runtime_key = event
        .get("runtime_key")
        .and_then(Value::as_str)
        .unwrap_or("unknown-runtime");
    let thread_id = payload
        .get("thread_id")
        .and_then(Value::as_str)
        .unwrap_or("unknown-thread");
    let turn_id = payload
        .get("turn_id")
        .and_then(Value::as_str)
        .unwrap_or("unknown-turn");
    Some(format!("{runtime_key}:{thread_id}:{turn_id}"))
}

fn live_transcript_event_seq(event: &Value) -> u64 {
    event
        .get("payload")
        .and_then(|payload| payload.get("seq"))
        .and_then(Value::as_u64)
        .unwrap_or(0)
}

impl BridgeRuntimeSink {
    async fn post_phase(&self, phase: &str, dedupe_key: String, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        // freshness_ms omitted — backend PHASE_FRESHNESS is the single source of truth.
        self.post_runtime_events_background(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": BRIDGE_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": dedupe_key,
            "payload": {
                "managed_transport": "codex_app_server",
                "thread_id": self.thread_id,
            }
        })]);
    }

    fn persist_local_phase(
        &self,
        phase: &str,
        tool_name: Option<String>,
        observed_at: chrono::DateTime<Utc>,
    ) {
        let Some(db_path) = self.local_db_path.as_deref() else {
            return;
        };

        let conn = match crate::state::db::open_db(Some(db_path)) {
            Ok(conn) => conn,
            Err(err) => {
                eprintln!("[codex-bridge] open local phase DB failed: {err}");
                return;
            }
        };

        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "codex".to_string(),
            phase: phase.to_string(),
            tool_name,
            source: BRIDGE_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[codex-bridge] persist local phase failed for {}: {err}",
                self.session_id
            );
        }
        let managed_signal = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "codex".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name: signal.tool_name.clone(),
            phase_source: BRIDGE_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::managed_session_state::ManagedSessionStateStore::new(&conn)
            .record_phase(&managed_signal)
        {
            eprintln!(
                "[codex-bridge] persist managed session state failed for {}: {err}",
                self.session_id
            );
        }
    }

    async fn post_progress(&self, dedupe_key: String) {
        self.post_runtime_events_background(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": BRIDGE_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": dedupe_key,
            "payload": {
                "managed_transport": "codex_app_server",
                "thread_id": self.thread_id,
            }
        })]);
    }

    async fn post_live_transcript_delta(
        &self,
        method: &str,
        delta: &str,
        turn_id: Option<&str>,
        seq: u64,
        live_text: &str,
    ) {
        info!(
            target: "codex_bridge::live_transcript",
            session_id = %self.session_id,
            seq,
            method,
            delta_len = delta.len(),
            text_len = live_text.len(),
            "live_transcript flush"
        );
        self.post_live_runtime_events_background(vec![self.live_transcript_delta_event(
            method,
            delta,
            turn_id,
            seq,
            live_text,
            false,
            Utc::now(),
        )]);
    }

    async fn post_live_transcript_completed(
        &self,
        turn_id: Option<&str>,
        seq: u64,
        live_text: &str,
    ) {
        info!(
            target: "codex_bridge::live_transcript",
            session_id = %self.session_id,
            seq,
            text_len = live_text.len(),
            "live_transcript completed"
        );
        self.post_live_runtime_events_background(vec![self.live_transcript_delta_event(
            "turn/completed",
            "",
            turn_id,
            seq,
            live_text,
            true,
            Utc::now(),
        )]);
    }

    fn live_transcript_delta_event(
        &self,
        method: &str,
        delta: &str,
        turn_id: Option<&str>,
        seq: u64,
        live_text: &str,
        turn_completed: bool,
        observed_at: chrono::DateTime<Utc>,
    ) -> Value {
        let thread_id = self.thread_id.as_deref().unwrap_or("unknown-thread");
        let turn_id = turn_id.unwrap_or("unknown-turn");
        json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": "codex_bridge_live",
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!(
                "bridge:live:{}:{}:{}:{}",
                self.session_id,
                thread_id,
                turn_id,
                seq
            ),
            "payload": {
                "progress_kind": "bridge_live_transcript_delta",
                "managed_transport": "codex_app_server",
                "thread_id": self.thread_id,
                "turn_id": turn_id,
                "seq": seq,
                "method": method,
                "delta": delta,
                "live_text": live_text,
                "turn_completed": turn_completed,
            }
        })
    }

    async fn post_terminal(&self, terminal_state: &str, terminal_reason: &str, dedupe_key: String) {
        let observed_at = Utc::now();
        self.persist_local_phase("finished", None, observed_at);
        self.post_runtime_events_blocking(vec![self.terminal_event(
            terminal_state,
            terminal_reason,
            &dedupe_key,
            observed_at,
        )])
        .await;
    }

    fn terminal_event(
        &self,
        terminal_state: &str,
        terminal_reason: &str,
        dedupe_key: &str,
        observed_at: chrono::DateTime<Utc>,
    ) -> Value {
        json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": BRIDGE_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": dedupe_key,
            "payload": {
                "managed_transport": "codex_app_server",
                "thread_id": self.thread_id,
                "terminal_state": terminal_state,
                "terminal_reason": terminal_reason,
                "terminal_source": BRIDGE_RUNTIME_SOURCE,
            }
        })
    }

    fn post_live_runtime_events_background(&self, events: Vec<Value>) {
        let mut events = events;
        if let Some(tx) = &self.live_runtime_tx {
            match tx.send(events) {
                Ok(()) => return,
                Err(err) => {
                    events = err.0;
                }
            }
        }
        let sink = self.clone();
        tokio::spawn(async move {
            sink.post_runtime_events_live(events).await;
        });
    }

    fn post_runtime_events_background(&self, events: Vec<Value>) {
        let mut events = events;
        if let Some(tx) = &self.runtime_tx {
            match tx.send(events) {
                Ok(()) => return,
                Err(err) => {
                    events = err.0;
                }
            }
        }
        let sink = self.clone();
        tokio::spawn(async move {
            sink.post_runtime_events_blocking(events).await;
        });
    }

    async fn post_runtime_events_live(&self, events: Vec<Value>) {
        let url = format!(
            "{}/api/agents/runtime/events/batch",
            self.api_url.trim_end_matches('/')
        );
        let started = Instant::now();
        match self
            .http
            .post(&url)
            .header("X-Agents-Token", &self.api_token)
            .timeout(LIVE_RUNTIME_EVENT_TIMEOUT)
            .json(&json!({ "events": events }))
            .send()
            .await
        {
            Ok(response) if response.status().is_success() => {
                let elapsed_ms = started.elapsed().as_millis();
                let queue_wait_ms =
                    parse_runtime_timing_header(response.headers(), "X-Runtime-Queue-Wait-Ms");
                let exec_ms = parse_runtime_timing_header(response.headers(), "X-Runtime-Exec-Ms");
                if elapsed_ms > LIVE_RUNTIME_EVENT_SLOW_LOG_MS
                    || queue_wait_ms.is_some_and(|value| value > 100.0)
                    || exec_ms.is_some_and(|value| value > 250.0)
                {
                    eprintln!(
                        "[codex-bridge] live runtime ingest slow elapsed_ms={elapsed_ms} queue_wait_ms={queue_wait_ms:?} exec_ms={exec_ms:?} event_count={}",
                        events.len()
                    );
                }
            }
            Ok(response) => {
                let status = response.status();
                let retryable = status.is_server_error() || status.as_u16() == 429;
                let body = response.text().await.unwrap_or_default();
                if retryable {
                    eprintln!("[codex-bridge] live runtime ingest retrying after {status}: {body}");
                    self.post_runtime_events_blocking(events).await;
                } else {
                    eprintln!("[codex-bridge] live runtime ingest dropped: {status} {body}");
                }
            }
            Err(err) => {
                eprintln!("[codex-bridge] live runtime ingest retrying after error: {err}");
                self.post_runtime_events_blocking(events).await;
            }
        }
    }

    async fn post_runtime_events_blocking(&self, events: Vec<Value>) {
        let url = format!(
            "{}/api/agents/runtime/events/batch",
            self.api_url.trim_end_matches('/')
        );
        for attempt in 0..3 {
            let response = match self
                .http
                .post(&url)
                .header("X-Agents-Token", &self.api_token)
                .json(&json!({ "events": events.clone() }))
                .send()
                .await
            {
                Ok(r) => r,
                Err(e) => {
                    if attempt < 2 {
                        tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64)).await;
                        continue;
                    }
                    eprintln!("[codex-bridge] runtime ingest network error: {e}");
                    return;
                }
            };
            if response.status().is_success() {
                return;
            }
            let status = response.status();
            let retryable = status.is_server_error() || status.as_u16() == 429;
            let body = response.text().await.unwrap_or_default();
            if retryable && attempt < 2 {
                tokio::time::sleep(Duration::from_millis(100 * (attempt + 1) as u64)).await;
                continue;
            }
            eprintln!("[codex-bridge] runtime ingest failed: {status} {body}");
            return;
        }
    }
}

fn build_request_user_input_answers(params: &Value, auto_approve: bool) -> Value {
    let mut answers = serde_json::Map::new();
    let Some(questions) = params.get("questions").and_then(Value::as_array) else {
        return Value::Object(answers);
    };
    for question in questions {
        let Some(id) = question.get("id").and_then(Value::as_str) else {
            continue;
        };
        let question_answers = if auto_approve {
            question
                .get("options")
                .and_then(Value::as_array)
                .and_then(|options| options.first())
                .and_then(|option| option.get("label"))
                .and_then(Value::as_str)
                .map(|label| vec![Value::String(label.to_string())])
                .unwrap_or_else(|| vec![Value::String("longhouse".to_string())])
        } else {
            Vec::new()
        };
        answers.insert(id.to_string(), json!({ "answers": question_answers }));
    }
    Value::Object(answers)
}

fn extract_string(value: &Value, path: &[&str]) -> Option<String> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    current.as_str().map(ToString::to_string)
}

fn parse_runtime_timing_header(headers: &HeaderMap, name: &'static str) -> Option<f64> {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite())
}

fn extract_websocket_listen_url(line: &str) -> Option<String> {
    let marker = "listening on:";
    let (_, tail) = line.split_once(marker)?;
    let candidate = tail.trim();
    if candidate.starts_with("ws://") || candidate.starts_with("wss://") {
        Some(candidate.to_string())
    } else {
        None
    }
}

fn should_emit_progress(last_emit: Option<Instant>, throttle_ms: u64) -> bool {
    match last_emit {
        None => true,
        Some(last) => last.elapsed() >= Duration::from_millis(throttle_ms),
    }
}

fn home_dir() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("HOME is not set"))
}

fn update_bridge_error(context: &mut BridgeContext, error: &str) -> Result<()> {
    context.state.status = "error".to_string();
    context.state.last_error = Some(error.to_string());
    write_state_file(&context.state_file, &context.state)
}

#[cfg(unix)]
fn dedicated_child_process_group_id(child: &Child) -> Option<i32> {
    let pid = child.id().and_then(|pid| i32::try_from(pid).ok())?;
    let process_group_id = unsafe { libc::getpgid(pid) };
    if process_group_id <= 0 || process_group_id != pid {
        return None;
    }
    Some(process_group_id)
}

async fn shutdown_child(client: &mut RpcClient) -> Result<()> {
    if let Some(ref mut child) = client.child {
        #[cfg(unix)]
        if let Some(process_group_id) = dedicated_child_process_group_id(child) {
            unsafe {
                let _ = libc::killpg(process_group_id, libc::SIGTERM);
            }
            tokio::time::sleep(CHILD_SHUTDOWN_GRACE_PERIOD).await;
            let process_group_still_alive = unsafe { libc::killpg(process_group_id, 0) == 0 };
            if process_group_still_alive {
                unsafe {
                    let _ = libc::killpg(process_group_id, libc::SIGKILL);
                }
            }
        }
        if child.try_wait()?.is_none() {
            let _ = child.start_kill();
        }
        let _ = child.wait().await;
    }
    Ok(())
}

#[cfg(unix)]
async fn terminate_recorded_app_server(state: &BridgeStateFile) {
    let recorded_pid = state
        .app_server_pid
        .and_then(|pid| i32::try_from(pid).ok())
        .filter(|pid| *pid > 0);
    if let (Some(pid), Some(process_group_id)) =
        (recorded_pid, state.app_server_pgid.filter(|pgid| *pgid > 0))
    {
        let current_process_group_id = unsafe { libc::getpgid(pid) };
        if current_process_group_id == process_group_id {
            unsafe {
                let _ = libc::killpg(process_group_id, libc::SIGTERM);
            }
            tokio::time::sleep(CHILD_SHUTDOWN_GRACE_PERIOD).await;
            let process_group_still_alive = unsafe { libc::killpg(process_group_id, 0) == 0 };
            if process_group_still_alive {
                unsafe {
                    let _ = libc::killpg(process_group_id, libc::SIGKILL);
                }
            }
            return;
        }
    }

    let Some(pid) = recorded_pid else {
        return;
    };
    unsafe {
        let _ = libc::kill(pid, libc::SIGTERM);
    }
    tokio::time::sleep(CHILD_SHUTDOWN_GRACE_PERIOD).await;
    let process_still_alive = unsafe { libc::kill(pid, 0) == 0 };
    if process_still_alive {
        unsafe {
            let _ = libc::kill(pid, libc::SIGKILL);
        }
    }
}

#[cfg(unix)]
fn remove_bridge_state_sidecars(state_file: &Path) {
    for suffix in ["json", "json.tmp", "lock", "sock"] {
        let candidate = if suffix == "json.tmp" {
            state_file.with_extension("json.tmp")
        } else {
            state_file.with_extension(suffix)
        };
        let _ = fs::remove_file(candidate);
    }
}

async fn handle_bridge_followup(
    config: &BridgeRunConfig,
    client: &mut RpcClient,
    context: &mut BridgeContext,
    followup: BridgeFollowup,
) -> Result<()> {
    match followup {
        BridgeFollowup::SubscribeThread {
            thread_id,
            thread_path,
        } => {
            let params = json!({
                "threadId": thread_id,
                "path": thread_path,
            });
            let mut last_error = None;
            for attempt in 0..=THREAD_SUBSCRIBE_RETRY_ATTEMPTS {
                context.state.thread_subscription_attempts =
                    context.state.thread_subscription_attempts.saturating_add(1);
                update_thread_subscription_tracking(
                    context,
                    ThreadSubscriptionStatus::Subscribing,
                    None,
                )?;
                match send_request_with_runtime(
                    client,
                    "thread/resume",
                    params.clone(),
                    config,
                    context,
                )
                .await
                {
                    Ok(response) => {
                        let resume_thread = response.get("thread").cloned().unwrap_or(Value::Null);
                        if codex_thread_value_is_subagent(&resume_thread) {
                            let resume_thread_id = extract_string(&resume_thread, &["id"])
                                .unwrap_or_else(|| thread_id.clone());
                            let error_text = format!(
                                "thread/resume returned Codex subagent thread {resume_thread_id}; refusing to adopt as managed primary"
                            );
                            context.rejected_thread_ids.insert(resume_thread_id);
                            update_thread_subscription_tracking(
                                context,
                                ThreadSubscriptionStatus::Failed,
                                Some(error_text.clone()),
                            )?;
                            bail!("{error_text}");
                        }
                        let resume_thread_id = extract_string(&resume_thread, &["id"])
                            .or_else(|| context.state.thread_id.clone())
                            .or_else(|| Some(thread_id.clone()));
                        let resume_thread_path = extract_string(&resume_thread, &["path"])
                            .or_else(|| context.state.thread_path.clone())
                            .or_else(|| thread_path.clone());
                        let _ = adopt_thread_identity(
                            config,
                            context,
                            resume_thread_id,
                            resume_thread_path,
                            true,
                        )?;
                        apply_thread_resume_snapshot(config, context, &response).await?;
                        context.subscribed_thread_id = context.state.thread_id.clone();
                        update_thread_subscription_tracking(
                            context,
                            ThreadSubscriptionStatus::Subscribed,
                            None,
                        )?;
                        return Ok(());
                    }
                    Err(err) => {
                        let error_text = err.to_string();
                        // Upstream can emit `thread/started` before the rollout file is
                        // materialized, and the running-thread resume path can also see the
                        // rollout file before its initial contents land on disk.
                        let retryable = is_retryable_thread_subscription_error(&error_text);
                        let status = if retryable {
                            ThreadSubscriptionStatus::Retrying
                        } else {
                            ThreadSubscriptionStatus::Failed
                        };
                        update_thread_subscription_tracking(
                            context,
                            status,
                            Some(error_text.clone()),
                        )?;
                        last_error = Some(err);
                        if retryable && attempt < THREAD_SUBSCRIBE_RETRY_ATTEMPTS {
                            tokio::time::sleep(Duration::from_millis(
                                THREAD_SUBSCRIBE_RETRY_DELAY_MS,
                            ))
                            .await;
                            continue;
                        }
                        if retryable {
                            update_thread_subscription_tracking(
                                context,
                                derive_thread_subscription_status(context),
                                Some(error_text),
                            )?;
                            return Ok(());
                        }
                        break;
                    }
                }
            }
            Err(last_error.expect("subscribe retry loop should capture an error"))
        }
    }
}

async fn apply_thread_resume_snapshot(
    config: &BridgeRunConfig,
    context: &mut BridgeContext,
    response: &Value,
) -> Result<()> {
    let thread = response.get("thread").cloned().unwrap_or(Value::Null);
    if thread.is_null() {
        return Ok(());
    }

    let status_params = json!({
        "thread": {
            "status": thread.get("status").cloned().unwrap_or(Value::Null),
        }
    });
    let _ = context
        .runtime_tracker
        .handle_notification("thread/status/changed", &status_params);

    let resumed_active_turn = extract_in_progress_turn(&thread);
    context.state.active_turn_id =
        resumed_active_turn.and_then(|turn| extract_string(turn, &["id"]));
    context.state.last_turn_status = resumed_active_turn
        .and_then(|turn| extract_string(turn, &["status"]))
        .or_else(|| context.state.last_turn_status.clone());
    context.runtime_tracker.active_turn_id = context.state.active_turn_id.clone();
    write_state_file(&context.state_file, &context.state)?;
    emit_runtime_updates(
        config,
        context,
        vec![context.runtime_tracker.current_phase_update()],
    )
    .await;
    Ok(())
}

fn extract_in_progress_turn(thread: &Value) -> Option<&Value> {
    thread
        .get("turns")
        .and_then(Value::as_array)
        .and_then(|turns| {
            turns
                .iter()
                .rev()
                .find(|turn| extract_string(turn, &["status"]).as_deref() == Some("inProgress"))
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::DateTime;
    use pretty_assertions::assert_eq;

    fn make_test_context(temp: &tempfile::TempDir) -> BridgeContext {
        let state_file = temp.path().join("bridge-state.json");
        let log_file = temp.path().join("bridge.log");
        BridgeContext {
            state_file: state_file.clone(),
            state: BridgeStateFile {
                schema_version: BRIDGE_STATE_SCHEMA_VERSION,
                session_id: "session-123".to_string(),
                cwd: temp.path().display().to_string(),
                codex_bin: "codex".to_string(),
                launch_mode: Some(LAUNCH_MODE_TUI.to_string()),
                ws_url: Some("ws://example.test".to_string()),
                thread_id: None,
                thread_path: None,
                pid: 42,
                app_server_pid: None,
                app_server_pgid: None,
                app_server_ws_url: None,
                status: "ready".to_string(),
                log_file: log_file.display().to_string(),
                active_turn_id: None,
                last_turn_status: None,
                last_error: None,
                thread_subscription_status: Some(
                    ThreadSubscriptionStatus::WaitingForThread
                        .as_str()
                        .to_string(),
                ),
                thread_subscription_attempts: 0,
                thread_subscription_last_error: None,
                updated_at: Utc::now().to_rfc3339(),
            },
            runtime: BridgeRuntimeSink {
                http: reqwest::Client::new(),
                api_url: "http://127.0.0.1:9".to_string(),
                api_token: "token".to_string(),
                session_id: "session-123".to_string(),
                cwd: temp.path().display().to_string(),
                machine_name: Some("test-box".to_string()),
                thread_id: None,
                local_db_path: Some(resolve_bridge_agent_db_path(Some(temp.path())).unwrap()),
                runtime_tx: None,
                live_runtime_tx: None,
            },
            last_progress_emit: None,
            live_transcript_seq: 0,
            live_transcript_text: String::new(),
            runtime_tracker: CodexRuntimeTracker::default(),
            subscribed_thread_id: None,
            rejected_thread_ids: BTreeSet::new(),
        }
    }

    #[test]
    fn bridge_state_file_keeps_legacy_state_compatible() {
        let temp = tempfile::tempdir().unwrap();
        let state_file = temp.path().join("legacy-state.json");
        fs::write(
            &state_file,
            json!({
                "session_id": "session-legacy",
                "cwd": temp.path().display().to_string(),
                "codex_bin": "codex",
                "ws_url": "ws://127.0.0.1:51234",
                "thread_id": null,
                "thread_path": null,
                "pid": 42,
                "status": "ready",
                "log_file": temp.path().join("bridge.log").display().to_string(),
                "active_turn_id": null,
                "last_turn_status": null,
                "last_error": null,
                "thread_subscription_status": "waiting_for_thread",
                "thread_subscription_attempts": 0,
                "thread_subscription_last_error": null,
                "updated_at": "2026-04-27T00:00:00Z"
            })
            .to_string(),
        )
        .unwrap();

        let state = read_state_file(&state_file).unwrap();

        assert_eq!(state.session_id, "session-legacy");
        assert_eq!(state.schema_version, 0);
        assert_eq!(state.app_server_pid, None);
        assert_eq!(state.app_server_pgid, None);
        assert_eq!(state.app_server_ws_url, None);
    }

    #[test]
    fn bridge_state_file_writes_schema_version() {
        let temp = tempfile::tempdir().unwrap();
        let state_file = temp.path().join("state.json");
        let state = BridgeStateFile {
            schema_version: BRIDGE_STATE_SCHEMA_VERSION,
            session_id: "session-123".to_string(),
            cwd: temp.path().display().to_string(),
            codex_bin: "codex".to_string(),
            launch_mode: Some(PERSISTED_DETACHED_UI_LAUNCH_MODE.to_string()),
            ws_url: None,
            thread_id: None,
            thread_path: None,
            pid: 42,
            app_server_pid: None,
            app_server_pgid: None,
            app_server_ws_url: None,
            status: "starting".to_string(),
            log_file: temp.path().join("bridge.log").display().to_string(),
            active_turn_id: None,
            last_turn_status: None,
            last_error: None,
            thread_subscription_status: None,
            thread_subscription_attempts: 0,
            thread_subscription_last_error: None,
            updated_at: Utc::now().to_rfc3339(),
        };

        write_state_file(&state_file, &state).unwrap();
        let raw: Value = serde_json::from_slice(&fs::read(&state_file).unwrap()).unwrap();

        assert_eq!(raw["schema_version"], BRIDGE_STATE_SCHEMA_VERSION);
        assert_eq!(raw["launch_mode"], LAUNCH_MODE_DETACHED_UI);
    }

    #[test]
    fn bridge_launch_mode_separates_tui_from_detached_ui_persistence() {
        assert_eq!(
            BridgeLaunchMode::Tui.persisted_state_value(),
            LAUNCH_MODE_TUI
        );
        assert_eq!(
            BridgeLaunchMode::DetachedUi.persisted_state_value(),
            LAUNCH_MODE_DETACHED_UI
        );
    }

    #[cfg(unix)]
    #[test]
    fn remove_bridge_state_sidecars_removes_json_tmp_lock_and_sock() {
        let temp = tempfile::tempdir().unwrap();
        let state_file = temp.path().join("session-1.json");
        let tmp_file = temp.path().join("session-1.json.tmp");
        let lock_file = temp.path().join("session-1.lock");
        let sock_file = temp.path().join("session-1.sock");
        for path in [&state_file, &tmp_file, &lock_file, &sock_file] {
            fs::write(path, "x").unwrap();
        }

        remove_bridge_state_sidecars(&state_file);

        for path in [&state_file, &tmp_file, &lock_file, &sock_file] {
            assert!(!path.exists(), "expected {} to be removed", path.display());
        }
    }

    fn make_test_run_config(temp: &tempfile::TempDir) -> BridgeRunConfig {
        BridgeRunConfig {
            session_id: "session-123".to_string(),
            cwd: temp.path().to_path_buf(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            codex_bin: "codex".to_string(),
            session_source: None,
            approval_policy: None,
            sandbox: None,
            model: None,
            model_reasoning_effort: None,
            machine_name: Some("test-box".to_string()),
            auto_approve: true,
            longhouse_home: Some(temp.path().to_path_buf()),
            state_file: temp.path().join("bridge-state.json"),
            log_file: temp.path().join("bridge.log"),
            create_initial_thread: false,
            launch_mode: BridgeLaunchMode::Tui,
        }
    }

    fn assert_phase_update(
        update: BridgeRuntimeUpdate,
        expected_phase: &'static str,
        expected_tool: Option<&str>,
    ) {
        assert_eq!(
            update,
            BridgeRuntimeUpdate::Phase {
                phase: expected_phase,
                tool_name: expected_tool.map(ToString::to_string),
            }
        );
    }

    #[test]
    fn resolve_bridge_paths_defaults_under_claude_dir() {
        let temp = tempfile::tempdir().unwrap();
        std::env::set_var("HOME", temp.path());
        let paths = resolve_bridge_paths(None, "session-123", None).unwrap();
        assert_eq!(
            paths.state_file.parent().unwrap(),
            temp.path()
                .join(".claude")
                .join("managed-local")
                .join("codex-bridge")
        );
        assert_eq!(
            paths.state_file,
            temp.path()
                .join(".claude")
                .join("managed-local")
                .join("codex-bridge")
                .join("session-123.json")
        );
        assert_eq!(
            paths.log_file,
            temp.path()
                .join(".claude")
                .join("managed-local")
                .join("codex-bridge")
                .join("session-123.log")
        );
    }

    #[test]
    fn resolve_bridge_agent_db_path_respects_longhouse_home_override() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = resolve_bridge_agent_db_path(Some(temp.path())).unwrap();
        assert_eq!(
            db_path,
            temp.path().join("agent").join("longhouse-shipper.db")
        );
    }

    #[test]
    fn bridge_runtime_sink_persists_local_phase() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = resolve_bridge_agent_db_path(Some(temp.path())).unwrap();
        let sink = BridgeRuntimeSink {
            http: reqwest::Client::new(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/assistants-service".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: None,
            local_db_path: Some(db_path.clone()),
            runtime_tx: None,
            live_runtime_tx: None,
        };

        let observed_at = DateTime::parse_from_rfc3339("2026-04-19T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        sink.persist_local_phase("running", Some("shell".to_string()), observed_at);

        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        let row: (String, Option<String>, String) = conn
            .query_row(
                "SELECT phase, tool_name, source
                 FROM session_phase_state
                 WHERE session_id = 'session-123'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();

        assert_eq!(row.0, "running");
        assert_eq!(row.1, Some("shell".to_string()));
        assert_eq!(row.2, BRIDGE_RUNTIME_SOURCE);

        let managed_row: (String, String, String, Option<String>, String) = conn
            .query_row(
                "SELECT provider, workspace_path, workspace_label, tool_name, phase_kind
                 FROM managed_session_state
                 WHERE session_id = 'session-123'",
                [],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();

        assert_eq!(managed_row.0, "codex");
        assert_eq!(managed_row.1, "/Users/test/git/assistants-service");
        assert_eq!(managed_row.2, "assistants-service");
        assert_eq!(managed_row.3, Some("shell".to_string()));
        assert_eq!(managed_row.4, "running");
    }

    #[test]
    fn bridge_runtime_sink_terminal_event_carries_close_cause() {
        let temp = tempfile::tempdir().unwrap();
        let sink = BridgeRuntimeSink {
            http: reqwest::Client::new(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/assistants-service".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: Some("thread-123".to_string()),
            local_db_path: Some(resolve_bridge_agent_db_path(Some(temp.path())).unwrap()),
            runtime_tx: None,
            live_runtime_tx: None,
        };

        let observed_at = DateTime::parse_from_rfc3339("2026-04-19T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let event = sink.terminal_event(
            "session_ended",
            "bridge_stop",
            "bridge:terminal:session-123:dedupe",
            observed_at,
        );

        assert_eq!(event["runtime_key"], "codex:session-123");
        assert_eq!(event["session_id"], "session-123");
        assert_eq!(event["provider"], "codex");
        assert_eq!(event["device_id"], "test-box");
        assert_eq!(event["source"], BRIDGE_RUNTIME_SOURCE);
        assert_eq!(event["kind"], "terminal_signal");
        assert_eq!(event["occurred_at"], "2026-04-19T00:00:00+00:00");
        assert_eq!(event["payload"]["managed_transport"], "codex_app_server");
        assert_eq!(event["payload"]["thread_id"], "thread-123");
        assert_eq!(event["payload"]["terminal_state"], "session_ended");
        assert_eq!(event["payload"]["terminal_reason"], "bridge_stop");
        assert_eq!(event["payload"]["terminal_source"], BRIDGE_RUNTIME_SOURCE);
    }

    #[test]
    fn bridge_runtime_sink_terminal_event_carries_terminal_disconnected() {
        let temp = tempfile::tempdir().unwrap();
        let sink = BridgeRuntimeSink {
            http: reqwest::Client::new(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/assistants-service".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: Some("thread-123".to_string()),
            local_db_path: Some(resolve_bridge_agent_db_path(Some(temp.path())).unwrap()),
            runtime_tx: None,
            live_runtime_tx: None,
        };

        let observed_at = DateTime::parse_from_rfc3339("2026-04-19T00:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let event = sink.terminal_event(
            "session_ended",
            TERMINAL_REASON_TERMINAL_DISCONNECTED,
            "bridge:terminal:session-123:dedupe",
            observed_at,
        );

        assert_eq!(event["payload"]["terminal_state"], "session_ended");
        assert_eq!(
            event["payload"]["terminal_reason"],
            TERMINAL_REASON_TERMINAL_DISCONNECTED
        );
        assert_eq!(event["payload"]["terminal_source"], BRIDGE_RUNTIME_SOURCE);
    }

    #[tokio::test]
    async fn bridge_runtime_sink_post_terminal_persists_finished_phase() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = resolve_bridge_agent_db_path(Some(temp.path())).unwrap();
        let http = reqwest::Client::builder()
            .timeout(Duration::from_millis(100))
            .build()
            .unwrap();
        let sink = BridgeRuntimeSink {
            http,
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/assistants-service".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: None,
            local_db_path: Some(db_path.clone()),
            runtime_tx: None,
            live_runtime_tx: None,
        };

        sink.post_terminal(
            "session_ended",
            "bridge_stop",
            "bridge:terminal:session-123:dedupe".to_string(),
        )
        .await;

        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        let row: (String, Option<String>, String) = conn
            .query_row(
                "SELECT phase, tool_name, source
                 FROM session_phase_state
                 WHERE session_id = 'session-123'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();

        assert_eq!(row.0, "finished");
        assert_eq!(row.1, None);
        assert_eq!(row.2, BRIDGE_RUNTIME_SOURCE);
    }

    #[test]
    fn should_emit_progress_respects_throttle() {
        assert!(should_emit_progress(None, 1000));
        assert!(!should_emit_progress(Some(Instant::now()), 10_000));
    }

    #[test]
    fn parse_runtime_timing_header_ignores_invalid_values() {
        let mut headers = HeaderMap::new();
        headers.insert("X-Runtime-Queue-Wait-Ms", HeaderValue::from_static("12.5"));
        headers.insert("X-Runtime-Exec-Ms", HeaderValue::from_static("inf"));

        assert_eq!(
            parse_runtime_timing_header(&headers, "X-Runtime-Queue-Wait-Ms"),
            Some(12.5)
        );
        assert_eq!(
            parse_runtime_timing_header(&headers, "X-Runtime-Exec-Ms"),
            None
        );
        assert_eq!(
            parse_runtime_timing_header(&headers, "X-Runtime-Missing"),
            None
        );
    }

    #[test]
    fn extract_live_transcript_delta_reads_agent_message_delta() {
        assert_eq!(
            extract_live_transcript_delta("item/agentMessage/delta", &json!({"delta": "hello"})),
            Some("hello")
        );
        assert_eq!(
            extract_live_transcript_delta(
                "item/commandExecution/outputDelta",
                &json!({"delta": "not assistant text"})
            ),
            None
        );
        assert_eq!(
            extract_live_transcript_delta("thread/status/changed", &json!({"delta": "hello"})),
            None
        );
    }

    #[test]
    fn latest_assistant_text_from_rollout_reads_recent_agent_message() {
        let temp = tempfile::tempdir().unwrap();
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(
            &rollout_path,
            [
                r#"{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"prompt"}]}}"#,
                r#"{"type":"event_msg","payload":{"type":"agent_message","message":"hello from event"}}"#,
            ]
            .join("\n"),
        )
        .unwrap();

        assert_eq!(
            latest_assistant_text_from_rollout(&rollout_path).as_deref(),
            Some("hello from event")
        );
    }

    #[test]
    fn latest_assistant_text_from_rollout_reads_response_item_output_text() {
        let temp = tempfile::tempdir().unwrap();
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(
            &rollout_path,
            [
                r#"{"type":"event_msg","payload":{"type":"agent_message","message":"older event"}}"#,
                r#"{"type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"hello "},{"type":"output_text","text":"world"}]}}"#,
            ]
            .join("\n"),
        )
        .unwrap();

        assert_eq!(
            latest_assistant_text_from_rollout(&rollout_path).as_deref(),
            Some("hello world")
        );
    }

    #[tokio::test]
    async fn completion_rollout_text_replaces_partial_live_buffer() {
        let temp = tempfile::tempdir().unwrap();
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(
            &rollout_path,
            r#"{"type":"event_msg","payload":{"type":"agent_message","message":"LH_PROBE_CODEX_MANAGED_123"}}"#,
        )
        .unwrap();

        assert_eq!(
            latest_assistant_text_from_rollout_for_completion(&rollout_path, "LH_PRO")
                .await
                .as_deref(),
            Some("LH_PROBE_CODEX_MANAGED_123")
        );
    }

    #[tokio::test]
    async fn completion_rollout_text_keeps_partial_buffer_when_latest_text_disagrees() {
        let temp = tempfile::tempdir().unwrap();
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(
            &rollout_path,
            r#"{"type":"event_msg","payload":{"type":"agent_message","message":"different answer"}}"#,
        )
        .unwrap();

        assert!(
            latest_assistant_text_from_rollout_for_completion(&rollout_path, "LH_PRO")
                .await
                .is_none()
        );
    }

    #[test]
    fn live_transcript_delta_event_uses_stable_sequence_key_and_buffer() {
        let sink = BridgeRuntimeSink {
            http: reqwest::Client::new(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/zerg".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: Some("thread-abc".to_string()),
            local_db_path: None,
            runtime_tx: None,
            live_runtime_tx: None,
        };
        let observed_at = DateTime::parse_from_rfc3339("2026-05-08T08:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let event = sink.live_transcript_delta_event(
            "item/agentMessage/delta",
            "lo",
            Some("turn-1"),
            2,
            "hello",
            false,
            observed_at,
        );

        assert_eq!(
            event["dedupe_key"],
            "bridge:live:session-123:thread-abc:turn-1:2"
        );
        assert_eq!(event["source"], "codex_bridge_live");
        assert_eq!(event["payload"]["seq"], 2);
        assert_eq!(event["payload"]["delta"], "lo");
        assert_eq!(event["payload"]["live_text"], "hello");
        assert_eq!(event["payload"]["turn_completed"], false);
        assert_eq!(event["payload"]["turn_id"], "turn-1");
        assert_eq!(event["occurred_at"], "2026-05-08T08:00:00+00:00");
    }

    #[test]
    fn live_transcript_delta_event_can_mark_turn_complete() {
        let sink = BridgeRuntimeSink {
            http: reqwest::Client::new(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/zerg".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: Some("thread-abc".to_string()),
            local_db_path: None,
            runtime_tx: None,
            live_runtime_tx: None,
        };
        let observed_at = DateTime::parse_from_rfc3339("2026-05-08T08:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let event = sink.live_transcript_delta_event(
            "turn/completed",
            "",
            Some("turn-1"),
            3,
            "hello",
            true,
            observed_at,
        );

        assert_eq!(
            event["dedupe_key"],
            "bridge:live:session-123:thread-abc:turn-1:3"
        );
        assert_eq!(event["payload"]["live_text"], "hello");
        assert_eq!(event["payload"]["turn_completed"], true);
    }

    #[test]
    fn coalesce_runtime_event_batches_keeps_latest_live_snapshot() {
        let sink = BridgeRuntimeSink {
            http: reqwest::Client::new(),
            api_url: "http://127.0.0.1:9".to_string(),
            api_token: "token".to_string(),
            session_id: "session-123".to_string(),
            cwd: "/Users/test/git/zerg".to_string(),
            machine_name: Some("test-box".to_string()),
            thread_id: Some("thread-abc".to_string()),
            local_db_path: None,
            runtime_tx: None,
            live_runtime_tx: None,
        };
        let observed_at = DateTime::parse_from_rfc3339("2026-05-08T08:00:00Z")
            .unwrap()
            .with_timezone(&Utc);
        let first_live = sink.live_transcript_delta_event(
            "item/agentMessage/delta",
            "L",
            Some("turn-1"),
            1,
            "L",
            false,
            observed_at,
        );
        let latest_live = sink.live_transcript_delta_event(
            "item/agentMessage/delta",
            "H",
            Some("turn-1"),
            2,
            "LH",
            false,
            observed_at,
        );
        let phase_event = json!({
            "runtime_key": "codex:session-123",
            "session_id": "session-123",
            "provider": "codex",
            "source": BRIDGE_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": "running",
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": "bridge:phase:session-123:running",
            "payload": {},
        });

        let events = coalesce_runtime_event_batches(vec![
            vec![first_live],
            vec![phase_event.clone()],
            vec![latest_live],
        ]);
        let live_events: Vec<&Value> = events
            .iter()
            .filter(|event| event["source"] == "codex_bridge_live")
            .collect();

        assert_eq!(events.len(), 2);
        assert!(events.iter().any(|event| event == &phase_event));
        assert_eq!(live_events.len(), 1);
        assert_eq!(live_events[0]["payload"]["seq"], 2);
        assert_eq!(live_events[0]["payload"]["live_text"], "LH");
    }

    #[test]
    fn build_request_user_input_answers_prefers_first_option_when_auto_approved() {
        let answers = build_request_user_input_answers(
            &json!({
                "questions": [{
                    "id": "color",
                    "options": [
                        {"label": "blue"},
                        {"label": "red"}
                    ]
                }]
            }),
            true,
        );
        assert_eq!(answers["color"]["answers"][0], "blue");
    }

    #[tokio::test]
    async fn initialize_client_opts_out_of_high_volume_notifications() {
        let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
        let (events_tx, events_rx) = mpsc::unbounded_channel();

        let client = RpcClient {
            child: None,
            child_pid: None,
            child_pgid: None,
            child_ws_url: None,
            outbound: RpcOutbound::WebSocket(outbound_tx),
            events_rx,
            pending_methods: BTreeMap::new(),
            next_request_id: 1,
            ws_url: "ws://example.test".to_string(),
        };

        let initialize_task = tokio::spawn(async move {
            let mut client = client;
            initialize_client(&mut client).await.unwrap();
        });

        let init_payload: Value = serde_json::from_str(&outbound_rx.recv().await.unwrap()).unwrap();
        assert_eq!(init_payload["method"], "initialize");
        assert_eq!(
            init_payload["params"]["capabilities"]["optOutNotificationMethods"],
            serde_json::to_value(BRIDGE_OPT_OUT_NOTIFICATION_METHODS).unwrap()
        );

        events_tx
            .send(StreamEvent::Rpc(json!({
                "id": 1,
                "result": {}
            })))
            .unwrap();

        initialize_task.await.unwrap();

        let initialized_payload: Value =
            serde_json::from_str(&outbound_rx.recv().await.unwrap()).unwrap();
        assert_eq!(initialized_payload["method"], "initialized");
    }

    #[test]
    fn ipc_socket_path_is_sibling_of_state_file() {
        let state = Path::new("/tmp/codex-bridge/session-42.json");
        let sock = ipc_socket_path(state);
        assert_eq!(sock, Path::new("/tmp/codex-bridge/session-42.sock"));
    }

    #[test]
    fn normalize_bridge_terminal_reason_defaults_unknown_to_bridge_stop() {
        assert_eq!(
            normalize_bridge_terminal_reason(None),
            TERMINAL_REASON_BRIDGE_STOP
        );
        assert_eq!(
            normalize_bridge_terminal_reason(Some("")),
            TERMINAL_REASON_BRIDGE_STOP
        );
        assert_eq!(
            normalize_bridge_terminal_reason(Some("future-new-reason")),
            TERMINAL_REASON_BRIDGE_STOP
        );
        assert_eq!(
            normalize_bridge_terminal_reason(Some(TERMINAL_REASON_TERMINAL_DISCONNECTED)),
            TERMINAL_REASON_TERMINAL_DISCONNECTED
        );
    }

    #[tokio::test]
    async fn stop_ipc_defaults_missing_reason_to_bridge_stop() {
        let reason = parse_stop_ipc_reason(json!({"kind": "stop"})).await;

        assert_eq!(reason, TERMINAL_REASON_BRIDGE_STOP);
    }

    #[tokio::test]
    async fn stop_ipc_preserves_terminal_disconnected_reason() {
        let reason = parse_stop_ipc_reason(json!({
            "kind": "stop",
            "reason": TERMINAL_REASON_TERMINAL_DISCONNECTED,
        }))
        .await;

        assert_eq!(reason, TERMINAL_REASON_TERMINAL_DISCONNECTED);
    }

    #[cfg(unix)]
    async fn parse_stop_ipc_reason(request: Value) -> String {
        let (mut client, server) = tokio::net::UnixStream::pair().unwrap();
        let (tx, mut rx) = mpsc::unbounded_channel();
        let task = tokio::spawn(handle_ipc_connection(server, tx));

        let mut bytes = serde_json::to_vec(&request).unwrap();
        bytes.push(b'\n');
        client.write_all(&bytes).await.unwrap();
        client.shutdown().await.unwrap();

        let command = rx.recv().await.unwrap();
        let IpcCommand::Stop {
            terminal_reason,
            reply,
        } = command
        else {
            panic!("expected stop command");
        };
        reply.send(Ok(json!({}))).unwrap();

        let mut response_buf = Vec::new();
        client.read_to_end(&mut response_buf).await.unwrap();
        parse_stop_ipc_response(&response_buf).unwrap();
        task.await.unwrap().unwrap();
        terminal_reason
    }

    #[tokio::test]
    async fn bridge_send_refuses_direct_ws_fallback_without_explicit_flag() {
        let temp = tempfile::tempdir().unwrap();
        let session_id = "session-123";
        let state = BridgeStateFile {
            schema_version: BRIDGE_STATE_SCHEMA_VERSION,
            session_id: session_id.to_string(),
            cwd: temp.path().display().to_string(),
            codex_bin: "codex".to_string(),
            launch_mode: Some(LAUNCH_MODE_DETACHED_UI.to_string()),
            ws_url: Some("ws://127.0.0.1:9".to_string()),
            thread_id: Some("thread-123".to_string()),
            thread_path: None,
            pid: 42,
            app_server_pid: None,
            app_server_pgid: None,
            app_server_ws_url: None,
            status: "ready".to_string(),
            log_file: temp.path().join("bridge.log").display().to_string(),
            active_turn_id: None,
            last_turn_status: None,
            last_error: None,
            thread_subscription_status: None,
            thread_subscription_attempts: 0,
            thread_subscription_last_error: None,
            updated_at: Utc::now().to_rfc3339(),
        };
        write_state_file(&temp.path().join(format!("{session_id}.json")), &state).unwrap();

        let err = cmd_codex_bridge_send(BridgeSendConfig {
            session_id: session_id.to_string(),
            text: "continue".to_string(),
            state_root: Some(temp.path().to_path_buf()),
            allow_direct_ws_fallback: false,
            attachments: Vec::new(),
        })
        .await
        .unwrap_err()
        .to_string();

        assert!(err.contains("refusing direct WebSocket fallback"));
        assert!(err.contains("--allow-direct-ws-fallback"));
    }

    #[test]
    fn parse_stop_ipc_response_accepts_clean_eof() {
        assert!(parse_stop_ipc_response(&[]).is_ok());
    }

    #[test]
    fn parse_stop_ipc_response_accepts_truncated_shutdown_reply() {
        assert!(parse_stop_ipc_response(br#"{"ok":true"#).is_ok());
    }

    #[test]
    fn parse_stop_ipc_response_accepts_complete_success_payload() {
        assert!(parse_stop_ipc_response(b"{\"ok\":true}\n").is_ok());
    }

    #[test]
    fn parse_stop_ipc_response_rejects_error_payload() {
        let err = parse_stop_ipc_response(br#"{"ok":false,"error":"boom"}"#)
            .expect_err("error payload should fail");
        assert!(err.to_string().contains("daemon IPC error: boom"));
    }

    #[test]
    fn codex_runtime_tracker_derives_running_and_thinking_from_item_lifecycle() {
        let mut tracker = CodexRuntimeTracker::default();

        assert_phase_update(
            tracker
                .handle_notification(
                    "turn/started",
                    &json!({
                        "turn": {"id": "turn-1", "status": "inProgress"}
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "thinking",
            None,
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "item/started",
                    &json!({
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "status": "inProgress",
                            "command": "pwd"
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "running",
            Some("shell"),
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "item/started",
                    &json!({
                        "item": {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "status": "inProgress",
                            "tool": "smart_home_get_state"
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "running",
            Some("smart_home_get_state"),
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "item/completed",
                    &json!({
                        "item": {
                            "id": "mcp-1",
                            "type": "mcpToolCall",
                            "status": "completed",
                            "tool": "smart_home_get_state"
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "running",
            Some("shell"),
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "item/completed",
                    &json!({
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "status": "completed",
                            "command": "pwd"
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "thinking",
            None,
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "turn/completed",
                    &json!({
                        "turn": {"id": "turn-1", "status": "completed"}
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "idle",
            None,
        );
    }

    #[test]
    fn codex_runtime_tracker_uses_waiting_flags_and_request_types() {
        let mut tracker = CodexRuntimeTracker::default();

        assert_phase_update(
            tracker
                .handle_notification(
                    "turn/started",
                    &json!({
                        "turn": {"id": "turn-1", "status": "inProgress"}
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "thinking",
            None,
        );

        assert_phase_update(
            tracker
                .handle_server_request(
                    "item/commandExecution/requestApproval",
                    &json!({"command": "git status"}),
                )
                .unwrap(),
            "blocked",
            Some("shell"),
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "thread/status/changed",
                    &json!({
                        "status": {
                            "type": "active",
                            "activeFlags": ["waitingOnApproval"]
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "blocked",
            Some("shell"),
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "thread/status/changed",
                    &json!({
                        "status": {
                            "type": "active",
                            "activeFlags": ["waitingOnUserInput"]
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "needs_user",
            None,
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "thread/status/changed",
                    &json!({
                        "status": {
                            "type": "active",
                            "activeFlags": []
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "thinking",
            None,
        );

        assert_phase_update(
            tracker
                .handle_server_request(
                    "item/fileChange/requestApproval",
                    &json!({"reason": "Need extra write access"}),
                )
                .unwrap(),
            "blocked",
            Some("edit"),
        );

        assert_phase_update(
            tracker
                .handle_notification(
                    "thread/status/changed",
                    &json!({
                        "status": {
                            "type": "idle"
                        }
                    }),
                )
                .into_iter()
                .next()
                .unwrap(),
            "idle",
            None,
        );
    }

    #[test]
    fn codex_runtime_tracker_keepalive_only_replays_live_execution_phases() {
        let mut tracker = CodexRuntimeTracker::default();
        assert!(tracker.keepalive_update().is_none());

        tracker.handle_notification(
            "turn/started",
            &json!({
                "turn": {"id": "turn-1", "status": "inProgress"}
            }),
        );
        assert_phase_update(
            tracker.keepalive_update().expect("thinking keepalive"),
            "thinking",
            None,
        );

        tracker.handle_notification(
            "item/started",
            &json!({
                "item": {
                    "id": "cmd-1",
                    "type": "commandExecution",
                    "status": "inProgress",
                    "command": "pwd"
                }
            }),
        );
        assert_phase_update(
            tracker.keepalive_update().expect("running keepalive"),
            "running",
            Some("shell"),
        );

        tracker.handle_notification(
            "thread/status/changed",
            &json!({
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"]
                }
            }),
        );
        assert!(tracker.keepalive_update().is_none());

        tracker.handle_notification(
            "turn/completed",
            &json!({
                "turn": {"id": "turn-1", "status": "completed"}
            }),
        );
        assert!(tracker.keepalive_update().is_none());
    }

    #[test]
    fn keepalive_interval_fits_within_thinking_freshness_budget() {
        // Backend PHASE_FRESHNESS["thinking"] = 90s is the shortest TTL.
        // Keepalive must fire before it expires so the phase stays live.
        const BACKEND_THINKING_FRESHNESS_MS: u64 = 90_000;
        assert!(
            ACTIVE_PHASE_KEEPALIVE_MS < BACKEND_THINKING_FRESHNESS_MS,
            "keepalive interval {}ms must be shorter than backend thinking freshness {}ms",
            ACTIVE_PHASE_KEEPALIVE_MS,
            BACKEND_THINKING_FRESHNESS_MS,
        );
    }

    #[test]
    fn keepalive_suppressed_for_needs_user_attention_state() {
        let mut tracker = CodexRuntimeTracker::default();

        // Enter thinking
        tracker.handle_notification(
            "turn/started",
            &json!({
                "turn": {"id": "turn-1", "status": "inProgress"}
            }),
        );
        assert!(tracker.keepalive_update().is_some());

        // Transition to needs_user (reply needed)
        tracker.handle_notification(
            "thread/status/changed",
            &json!({
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnUserInput"]
                }
            }),
        );
        assert!(
            tracker.keepalive_update().is_none(),
            "keepalive must be suppressed in needs_user state"
        );
    }

    #[tokio::test]
    async fn process_notification_requests_thread_subscription_after_first_thread_start() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);

        let followup = process_notification(
            &json!({
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "thr-live",
                        "path": "/tmp/thread.jsonl"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(context.state.thread_id.as_deref(), Some("thr-live"));
        assert_eq!(
            context.state.thread_path.as_deref(),
            Some("/tmp/thread.jsonl")
        );
        assert_eq!(context.runtime.thread_id.as_deref(), Some("thr-live"));
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::WaitingForTurn.as_str())
        );
    }

    #[tokio::test]
    async fn process_notification_waits_for_rollout_materialization_before_subscribing_known_path()
    {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        let rollout_path = temp.path().join("missing-rollout.jsonl");
        context.state.thread_id = Some("thr-live".to_string());
        context.state.thread_path = Some(rollout_path.display().to_string());
        context.runtime.thread_id = Some("thr-live".to_string());

        let followup = process_notification(
            &json!({
                "method": "turn/started",
                "params": {
                    "turn": {
                        "id": "turn-live",
                        "status": "inProgress"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(context.state.active_turn_id.as_deref(), Some("turn-live"));
        assert_eq!(
            context.state.last_turn_status.as_deref(),
            Some("inProgress")
        );
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::WaitingForRollout.as_str())
        );
    }

    #[tokio::test]
    async fn process_notification_subscribes_when_known_rollout_is_ready() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        let rollout_path = temp.path().join("ready-rollout.jsonl");
        fs::write(&rollout_path, "{\"ok\":true}\n").unwrap();
        context.state.thread_id = Some("thr-live".to_string());
        context.state.thread_path = Some(rollout_path.display().to_string());
        context.runtime.thread_id = Some("thr-live".to_string());

        let followup = process_notification(
            &json!({
                "method": "turn/started",
                "params": {
                    "turn": {
                        "id": "turn-live",
                        "status": "inProgress"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(
            followup,
            Some(BridgeFollowup::SubscribeThread {
                thread_id: "thr-live".to_string(),
                thread_path: Some(rollout_path.display().to_string()),
            })
        );
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::ReadyToSubscribe.as_str())
        );
    }

    #[tokio::test]
    async fn process_notification_does_not_replace_thread_candidate_from_turn_started() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        context.state.thread_id = Some("thr-bad".to_string());
        context.state.thread_path = Some("/tmp/bad-thread.jsonl".to_string());
        context.runtime.thread_id = Some("thr-bad".to_string());

        let db_path = resolve_bridge_agent_db_path(Some(temp.path())).unwrap();
        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        let binding = crate::state::session_binding::SessionBinding::new(&conn);
        binding
            .bind(
                &normalize_binding_path("/tmp/bad-thread.jsonl"),
                &context.state.session_id,
                "codex",
            )
            .unwrap();

        let followup = process_notification(
            &json!({
                "method": "turn/started",
                "params": {
                    "threadId": "thr-live",
                    "turn": {
                        "id": "turn-live",
                        "status": "inProgress"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(context.state.thread_id.as_deref(), Some("thr-bad"));
        assert_eq!(
            context.state.thread_path.as_deref(),
            Some("/tmp/bad-thread.jsonl")
        );
        assert_eq!(context.runtime.thread_id.as_deref(), Some("thr-bad"));
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::WaitingForThread.as_str())
        );
        assert_eq!(
            binding.get("/tmp/bad-thread.jsonl").unwrap().as_deref(),
            Some(context.state.session_id.as_str())
        );
    }

    #[tokio::test]
    async fn process_notification_ignores_turn_completed_for_different_thread() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        context.state.thread_id = Some("thr-live".to_string());
        context.state.thread_path = Some("/tmp/thread-live.jsonl".to_string());
        context.state.active_turn_id = Some("turn-live".to_string());
        context.runtime.thread_id = Some("thr-live".to_string());
        context.runtime_tracker.active_turn_id = Some("turn-live".to_string());

        let followup = process_notification(
            &json!({
                "method": "turn/completed",
                "params": {
                    "threadId": "thr-child",
                    "turn": {
                        "id": "turn-child",
                        "status": "completed"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(context.state.active_turn_id.as_deref(), Some("turn-live"));
        assert_eq!(
            context.runtime_tracker.active_turn_id.as_deref(),
            Some("turn-live")
        );
    }

    #[tokio::test]
    async fn process_notification_ignores_subagent_thread_started() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);

        let followup = process_notification(
            &json!({
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "thr-child",
                        "path": "/tmp/child.jsonl",
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": "thr-parent",
                                    "depth": 1
                                }
                            }
                        }
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(context.state.thread_id, None);
        assert_eq!(context.state.thread_path, None);
        assert_eq!(context.runtime.thread_id, None);
    }

    #[tokio::test]
    async fn process_notification_ignores_camel_subagent_thread_started() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);

        let followup = process_notification(
            &json!({
                "method": "thread/started",
                "params": {
                    "thread": {
                        "id": "thr-child",
                        "path": "/tmp/child.jsonl",
                        "source": {
                            "subAgent": {
                                "threadSpawn": {
                                    "parentThreadId": "thr-parent",
                                    "depth": 1
                                }
                            }
                        }
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(context.state.thread_id, None);
        assert_eq!(context.state.thread_path, None);
        assert_eq!(context.runtime.thread_id, None);
    }

    #[tokio::test]
    async fn adopt_thread_identity_refuses_subagent_rollout_path() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        let rollout_path = temp.path().join("child-rollout.jsonl");
        fs::write(
            &rollout_path,
            r#"{"type":"session_meta","timestamp":"2026-04-29T19:48:36Z","payload":{"id":"019ddb6e-114f-7643-89db-86c31a2aa706","source":{"subagent":{"thread_spawn":{"parent_thread_id":"019dd708-573a-7131-a4d9-9ee855520483","depth":1}}}}}"#,
        )
        .unwrap();

        let changed = adopt_thread_identity(
            &config,
            &mut context,
            Some("thr-child".to_string()),
            Some(rollout_path.display().to_string()),
            false,
        )
        .unwrap();

        assert!(!changed);
        assert_eq!(context.state.thread_id, None);
        assert_eq!(context.state.thread_path, None);
    }

    #[test]
    fn pending_thread_subscription_does_not_retry_rejected_thread() {
        let temp = tempfile::tempdir().unwrap();
        let mut context = make_test_context(&temp);
        let rollout_path = temp.path().join("child-rollout.jsonl");
        fs::write(&rollout_path, "{\"ok\":true}\n").unwrap();
        context.state.thread_id = Some("thr-child".to_string());
        context.state.thread_path = Some(rollout_path.display().to_string());
        context.state.thread_subscription_status =
            Some(ThreadSubscriptionStatus::Failed.as_str().to_string());
        context.state.thread_subscription_last_error = Some(
            "thread/resume returned Codex subagent thread thr-child; refusing to adopt as managed primary"
                .to_string(),
        );
        context.rejected_thread_ids.insert("thr-child".to_string());

        let followup = pending_thread_subscription(&mut context).unwrap();

        assert_eq!(followup, None);
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::Failed.as_str())
        );
        assert_eq!(
            context.state.thread_subscription_last_error.as_deref(),
            Some(
                "thread/resume returned Codex subagent thread thr-child; refusing to adopt as managed primary"
            )
        );
    }

    #[tokio::test]
    async fn handle_bridge_followup_rejects_subagent_resume_without_replacing_parent() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        let parent_path = temp.path().join("parent-rollout.jsonl");
        let child_path = temp.path().join("child-rollout.jsonl");
        fs::write(&parent_path, "{\"ok\":true}\n").unwrap();
        fs::write(&child_path, "{\"ok\":true}\n").unwrap();
        let parent_path_string = parent_path.display().to_string();
        let child_path_string = child_path.display().to_string();
        context.state.thread_id = Some("thr-parent".to_string());
        context.state.thread_path = Some(parent_path_string.clone());
        context.runtime.thread_id = Some("thr-parent".to_string());

        let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
        let (events_tx, events_rx) = mpsc::unbounded_channel();
        events_tx
            .send(StreamEvent::Rpc(json!({
                "id": 1,
                "result": {
                    "thread": {
                        "id": "thr-child",
                        "path": child_path_string,
                        "source": {
                            "subagent": {
                                "thread_spawn": {
                                    "parent_thread_id": "thr-parent",
                                    "depth": 1
                                }
                            }
                        }
                    }
                }
            })))
            .unwrap();
        let mut client = RpcClient {
            child: None,
            child_pid: None,
            child_pgid: None,
            child_ws_url: None,
            outbound: RpcOutbound::WebSocket(outbound_tx),
            events_rx,
            pending_methods: BTreeMap::new(),
            next_request_id: 1,
            ws_url: "ws://example.test".to_string(),
        };

        let err = handle_bridge_followup(
            &config,
            &mut client,
            &mut context,
            BridgeFollowup::SubscribeThread {
                thread_id: "thr-parent".to_string(),
                thread_path: Some(parent_path_string.clone()),
            },
        )
        .await
        .unwrap_err();

        assert!(err
            .to_string()
            .contains("thread/resume returned Codex subagent thread thr-child"));
        assert_eq!(context.state.thread_id.as_deref(), Some("thr-parent"));
        assert_eq!(
            context.state.thread_path.as_deref(),
            Some(parent_path_string.as_str())
        );
        assert_eq!(context.runtime.thread_id.as_deref(), Some("thr-parent"));
        assert_eq!(context.subscribed_thread_id, None);
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::Failed.as_str())
        );
        assert_eq!(context.state.thread_subscription_attempts, 1);
        assert!(context
            .state
            .thread_subscription_last_error
            .as_deref()
            .is_some_and(|message| message
                .contains("thread/resume returned Codex subagent thread thr-child")));
        assert!(context.rejected_thread_ids.contains("thr-child"));

        let outbound = outbound_rx.recv().await.unwrap();
        let payload: Value = serde_json::from_str(&outbound).unwrap();
        assert_eq!(
            payload.get("method").and_then(Value::as_str),
            Some("thread/resume")
        );
        assert_eq!(
            payload.pointer("/params/threadId").and_then(Value::as_str),
            Some("thr-parent")
        );
    }

    #[tokio::test]
    async fn adopt_thread_identity_does_not_replace_locked_thread() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        context.state.thread_id = Some("thr-live".to_string());
        context.state.thread_path = Some("/tmp/thread-live.jsonl".to_string());
        context.runtime.thread_id = Some("thr-live".to_string());
        context.subscribed_thread_id = Some("thr-live".to_string());

        let changed = adopt_thread_identity(
            &config,
            &mut context,
            Some("thr-other".to_string()),
            Some("/tmp/thread-other.jsonl".to_string()),
            false,
        )
        .unwrap();

        assert!(!changed);
        assert_eq!(context.state.thread_id.as_deref(), Some("thr-live"));
        assert_eq!(
            context.state.thread_path.as_deref(),
            Some("/tmp/thread-live.jsonl")
        );
    }

    #[tokio::test]
    async fn adopt_thread_identity_resets_subscription_tracking_for_new_thread() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        context.state.thread_id = Some("thr-old".to_string());
        context.state.thread_path = Some("/tmp/thread-old.jsonl".to_string());
        context.runtime.thread_id = Some("thr-old".to_string());
        context.state.thread_subscription_status =
            Some(ThreadSubscriptionStatus::Retrying.as_str().to_string());
        context.state.thread_subscription_attempts = 3;
        context.state.thread_subscription_last_error =
            Some("thread/resume failed: no rollout found".to_string());

        let changed = adopt_thread_identity(
            &config,
            &mut context,
            Some("thr-new".to_string()),
            Some("/tmp/thread-new.jsonl".to_string()),
            false,
        )
        .unwrap();

        assert!(changed);
        assert_eq!(context.state.thread_id.as_deref(), Some("thr-new"));
        assert_eq!(
            context.state.thread_path.as_deref(),
            Some("/tmp/thread-new.jsonl")
        );
        assert_eq!(context.runtime.thread_id.as_deref(), Some("thr-new"));
        assert_eq!(context.subscribed_thread_id, None);
        assert_eq!(context.state.thread_subscription_attempts, 0);
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::WaitingForTurn.as_str())
        );
        assert_eq!(context.state.thread_subscription_last_error, None);
    }

    #[tokio::test]
    async fn apply_thread_resume_snapshot_hydrates_active_turn_from_resume_response() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);

        apply_thread_resume_snapshot(
            &config,
            &mut context,
            &json!({
                "thread": {
                    "status": {
                        "type": "active",
                        "activeFlags": []
                    },
                    "turns": [
                        {"id": "turn-old", "status": "completed"},
                        {"id": "turn-live", "status": "inProgress"}
                    ]
                }
            }),
        )
        .await
        .unwrap();

        assert_eq!(context.state.active_turn_id.as_deref(), Some("turn-live"));
        assert_eq!(
            context.state.last_turn_status.as_deref(),
            Some("inProgress")
        );
        assert_eq!(
            context.runtime_tracker.active_turn_id.as_deref(),
            Some("turn-live")
        );
    }

    #[tokio::test]
    async fn process_notification_stops_requesting_thread_subscription_after_bridge_is_subscribed()
    {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(&rollout_path, "{\"ok\":true}\n").unwrap();
        context.state.thread_id = Some("thr-live".to_string());
        context.state.thread_path = Some(rollout_path.display().to_string());
        context.runtime.thread_id = Some("thr-live".to_string());

        let followup = process_notification(
            &json!({
                "method": "turn/started",
                "params": {
                    "turn": {
                        "id": "turn-live",
                        "status": "inProgress"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(
            followup,
            Some(BridgeFollowup::SubscribeThread {
                thread_id: "thr-live".to_string(),
                thread_path: Some(rollout_path.display().to_string()),
            })
        );
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::ReadyToSubscribe.as_str())
        );

        context.subscribed_thread_id = Some("thr-live".to_string());
        let followup = process_notification(
            &json!({
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "item-1",
                        "type": "commandExecution"
                    }
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        assert_eq!(
            context.state.thread_subscription_status.as_deref(),
            Some(ThreadSubscriptionStatus::Subscribed.as_str())
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn process_notification_wakes_daemon_for_agent_message_delta() {
        let temp = tempfile::tempdir().unwrap();
        let config = make_test_run_config(&temp);
        let mut context = make_test_context(&temp);
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(&rollout_path, "{\"ok\":true}\n").unwrap();
        context.state.thread_id = Some("thr-live".to_string());
        context.state.thread_path = Some(rollout_path.display().to_string());
        context.state.active_turn_id = Some("turn-live".to_string());
        context.runtime.thread_id = Some("thr-live".to_string());

        let socket_path = resolve_bridge_transcript_wake_socket_path(Some(temp.path())).unwrap();
        fs::create_dir_all(socket_path.parent().unwrap()).unwrap();
        let _ = fs::remove_file(&socket_path);
        let listener = tokio::net::UnixListener::bind(&socket_path).unwrap();

        let followup = process_notification(
            &json!({
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thr-live",
                    "delta": "hello"
                }
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(followup, None);
        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(1), listener.accept())
            .await
            .expect("wake connection")
            .unwrap();
        let mut body = Vec::new();
        stream.read_to_end(&mut body).await.unwrap();
        let payload: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(payload["provider"], "codex");
        assert_eq!(payload["path"], rollout_path.display().to_string());
        assert_eq!(payload["phase"], "running");
        assert_eq!(payload["session_id"], config.session_id);
        assert_eq!(payload["turn_id"], "turn-live");
        assert_eq!(payload["wake_reason"], "progress");
        assert!(payload["observed_at_ms"].as_i64().is_some());
        assert_eq!(
            payload["file_len_hint"].as_u64(),
            Some(fs::metadata(&rollout_path).unwrap().len())
        );
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn ipc_turn_start_marks_active_and_wakes_daemon() {
        let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
        let (events_tx, events_rx) = mpsc::unbounded_channel();
        let temp = tempfile::tempdir().unwrap();
        let rollout_path = temp.path().join("thread.jsonl");
        fs::write(&rollout_path, "{\"ok\":true}\n").unwrap();

        let socket_path = resolve_bridge_transcript_wake_socket_path(Some(temp.path())).unwrap();
        fs::create_dir_all(socket_path.parent().unwrap()).unwrap();
        let _ = fs::remove_file(&socket_path);
        let listener = tokio::net::UnixListener::bind(&socket_path).unwrap();

        let mut client = RpcClient {
            child: None,
            child_pid: None,
            child_pgid: None,
            child_ws_url: None,
            outbound: RpcOutbound::WebSocket(outbound_tx),
            events_rx,
            pending_methods: BTreeMap::new(),
            next_request_id: 1,
            ws_url: "ws://example.test".to_string(),
        };
        let mut context = make_test_context(&temp);
        context.state.thread_id = Some("thr_test".to_string());
        context.state.thread_path = Some(rollout_path.display().to_string());
        context.runtime.thread_id = Some("thr_test".to_string());
        let config = make_test_run_config(&temp);

        events_tx
            .send(StreamEvent::Rpc(json!({
                "id": 1,
                "result": {
                    "turn": {"id": "turn-live", "status": "inProgress"}
                }
            })))
            .unwrap();

        let summary = handle_ipc_turn_start(
            &config,
            &mut client,
            &mut context,
            "continue",
            "thr_test",
            &[],
        )
        .await
        .unwrap();

        assert_eq!(summary.turn_id, "turn-live");
        assert_eq!(context.state.active_turn_id.as_deref(), Some("turn-live"));
        assert_eq!(
            context.state.last_turn_status.as_deref(),
            Some("inProgress")
        );
        assert_eq!(
            context.runtime_tracker.active_turn_id.as_deref(),
            Some("turn-live")
        );

        let request_payload: Value =
            serde_json::from_str(&outbound_rx.recv().await.unwrap()).unwrap();
        assert_eq!(request_payload["method"], "turn/start");

        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(1), listener.accept())
            .await
            .expect("wake connection")
            .unwrap();
        let mut body = Vec::new();
        stream.read_to_end(&mut body).await.unwrap();
        let payload: Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(payload["provider"], "codex");
        assert_eq!(payload["path"], rollout_path.display().to_string());
        assert_eq!(payload["phase"], "running");
        assert_eq!(payload["session_id"], config.session_id);
        assert_eq!(payload["turn_id"], "turn-live");
        assert_eq!(payload["wake_reason"], "turn_started");
        assert!(payload["observed_at_ms"].as_i64().is_some());
        assert_eq!(
            payload["file_len_hint"].as_u64(),
            Some(fs::metadata(&rollout_path).unwrap().len())
        );
    }

    #[test]
    fn retryable_thread_subscription_errors_cover_missing_and_empty_rollouts() {
        assert!(is_retryable_thread_subscription_error(
            "thread/resume failed: {\"code\":-32600,\"message\":\"no rollout found for thread id thr-live\"}"
        ));
        assert!(is_retryable_thread_subscription_error(
            "thread/resume failed: {\"code\":-32603,\"message\":\"failed to load rollout `/tmp/thread.jsonl` for thread thr-live: rollout at /tmp/thread.jsonl is empty\"}"
        ));
        assert!(!is_retryable_thread_subscription_error(
            "thread/resume failed: {\"code\":-32000,\"message\":\"permission denied\"}"
        ));
    }

    #[tokio::test]
    async fn send_request_with_runtime_handles_interleaved_requests_and_notifications() {
        let (outbound_tx, mut outbound_rx) = mpsc::unbounded_channel::<String>();
        let (events_tx, events_rx) = mpsc::unbounded_channel();
        let temp = tempfile::tempdir().unwrap();

        let mut client = RpcClient {
            child: None,
            child_pid: None,
            child_pgid: None,
            child_ws_url: None,
            outbound: RpcOutbound::WebSocket(outbound_tx),
            events_rx,
            pending_methods: BTreeMap::new(),
            next_request_id: 1,
            ws_url: "ws://example.test".to_string(),
        };
        let mut context = make_test_context(&temp);
        context.state.thread_id = Some("thr_test".to_string());
        context.runtime.thread_id = Some("thr_test".to_string());
        let config = make_test_run_config(&temp);

        events_tx
            .send(StreamEvent::Rpc(json!({
                "id": "srv-1",
                "method": "item/tool/requestUserInput",
                "params": {
                    "questions": [{
                        "id": "color",
                        "options": [{"label": "blue"}, {"label": "red"}],
                    }]
                }
            })))
            .unwrap();
        events_tx
            .send(StreamEvent::Rpc(json!({
                "method": "turn/started",
                "params": {
                    "threadId": "thr_test",
                    "turn": {"id": "turn-live", "status": "inProgress", "items": []}
                }
            })))
            .unwrap();
        events_tx
            .send(StreamEvent::Rpc(json!({
                "id": 1,
                "result": {
                    "turn": {"id": "turn-live", "status": "inProgress"}
                }
            })))
            .unwrap();

        let response = send_request_with_runtime(
            &mut client,
            "turn/start",
            json!({
                "threadId": "thr_test",
                "input": [{"type": "text", "text": "continue"}],
            }),
            &config,
            &mut context,
        )
        .await
        .unwrap();

        assert_eq!(
            extract_string(&response, &["turn", "id"]).as_deref(),
            Some("turn-live")
        );
        assert_eq!(context.state.active_turn_id.as_deref(), Some("turn-live"));
        assert_eq!(
            context.state.last_turn_status.as_deref(),
            Some("inProgress")
        );

        let request_payload: Value =
            serde_json::from_str(&outbound_rx.recv().await.unwrap()).unwrap();
        assert_eq!(request_payload["method"], "turn/start");

        let approval_payload: Value =
            serde_json::from_str(&outbound_rx.recv().await.unwrap()).unwrap();
        assert_eq!(approval_payload["id"], "srv-1");
        assert_eq!(
            approval_payload["result"]["answers"]["color"]["answers"][0],
            "blue"
        );
    }

    #[test]
    fn classify_steer_error_recognizes_turn_state_races() {
        for sample in [
            "turn/steer failed: { code: -32602, message: \"turn is not active\" }",
            "turn/steer failed: expected turn id does not match active turn",
            "turn/steer failed: expectedTurnId mismatch",
            "turn/steer failed: turn has already completed",
            "turn/steer failed: turn interrupted before steer",
            "turn/steer failed: no active turn",
            "turn/steer failed: turn ended",
        ] {
            assert!(
                classify_steer_error_as_turn_ended(sample),
                "expected {sample:?} to classify as turn_ended",
            );
        }
    }

    #[tokio::test]
    async fn send_via_ipc_steer_roundtrips_ok_response() {
        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("steer.sock");
        let listener = tokio::net::UnixListener::bind(&sock).unwrap();

        let sock_clone = sock.clone();
        let server = tokio::spawn(async move {
            let (mut stream, _addr) = listener.accept().await.unwrap();
            let mut buf = Vec::new();
            tokio::io::AsyncReadExt::read_to_end(&mut stream, &mut buf)
                .await
                .unwrap();
            let request: Value = serde_json::from_slice(&buf).unwrap();
            assert_eq!(request["kind"], "steer");
            assert_eq!(request["text"], "ipc steer");
            assert_eq!(request["thread_id"], "thr-1");
            assert_eq!(request["expected_turn_id"], "turn-1");
            let response = b"{\"ok\": true}\n";
            tokio::io::AsyncWriteExt::write_all(&mut stream, response)
                .await
                .unwrap();
            drop(stream);
            let _ = sock_clone;
        });

        send_via_ipc_steer(&sock, "ipc steer", "thr-1", "turn-1", &[])
            .await
            .unwrap();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn send_via_ipc_steer_surfaces_daemon_error() {
        let tmp = tempfile::tempdir().unwrap();
        let sock = tmp.path().join("steer-err.sock");
        let listener = tokio::net::UnixListener::bind(&sock).unwrap();

        tokio::spawn(async move {
            let (mut stream, _addr) = listener.accept().await.unwrap();
            let mut buf = Vec::new();
            tokio::io::AsyncReadExt::read_to_end(&mut stream, &mut buf)
                .await
                .unwrap();
            let response = b"{\"ok\": false, \"error\": \"turn/steer failed: no active turn\"}\n";
            tokio::io::AsyncWriteExt::write_all(&mut stream, response)
                .await
                .unwrap();
        });

        let err = send_via_ipc_steer(&sock, "x", "thr", "turn", &[])
            .await
            .unwrap_err();
        let msg = format!("{err}");
        assert!(msg.contains("no active turn"), "unexpected error: {msg}");
    }

    #[test]
    fn classify_steer_error_does_not_flag_generic_protocol_errors() {
        for sample in [
            "turn/steer failed: connection reset",
            "turn/steer failed: protocol parse error",
            "turn/steer failed: thread not found",
            "turn/steer failed: unauthorized",
            // Bare `expected` should no longer be enough to trip the
            // classifier — a real protocol-shape bug would otherwise
            // silently convert to a user-actionable turn-ended prompt.
            "turn/steer failed: expected turn/steer response to include result",
        ] {
            assert!(
                !classify_steer_error_as_turn_ended(sample),
                "expected {sample:?} NOT to be mis-classified as turn_ended",
            );
        }
    }
}
