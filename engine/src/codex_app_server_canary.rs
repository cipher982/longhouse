use std::collections::BTreeMap;
use std::fs;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, oneshot, Mutex};
use tokio::time::{sleep, Instant};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use uuid::Uuid;
use walkdir::WalkDir;

use crate::text::truncate_tail_chars;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum AppServerTransport {
    Stdio,
    WebSocket,
}

pub fn parse_app_server_transport(value: &str) -> Result<AppServerTransport> {
    match value.trim().to_ascii_lowercase().as_str() {
        "" | "stdio" => Ok(AppServerTransport::Stdio),
        "websocket" | "ws" => Ok(AppServerTransport::WebSocket),
        other => bail!("Unsupported app-server transport '{other}'. Use stdio or websocket"),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RemoteTuiSubscribePhase {
    PreTurn,
    PostTurn,
    AfterRollout,
}

impl RemoteTuiSubscribePhase {
    fn as_str(self) -> &'static str {
        match self {
            Self::PreTurn => "preturn",
            Self::PostTurn => "postturn",
            Self::AfterRollout => "after_rollout",
        }
    }
}

pub fn parse_remote_tui_subscribe_phase(value: &str) -> Result<RemoteTuiSubscribePhase> {
    match value.trim().to_ascii_lowercase().as_str() {
        "" | "postturn" | "post_turn" | "post-turn" => Ok(RemoteTuiSubscribePhase::PostTurn),
        "preturn" | "pre_turn" | "pre-turn" => Ok(RemoteTuiSubscribePhase::PreTurn),
        "afterrollout" | "after_rollout" | "after-rollout" | "rollout" => {
            Ok(RemoteTuiSubscribePhase::AfterRollout)
        }
        other => bail!(
            "Unsupported remote TUI subscribe phase '{other}'. Use preturn, postturn, or after_rollout"
        ),
    }
}

#[derive(Debug, Clone)]
pub struct CanaryConfig {
    pub prompt: String,
    pub cwd: PathBuf,
    pub home_override: Option<PathBuf>,
    pub approval_policy: String,
    pub sandbox: String,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub codex_bin: String,
    pub app_server_transport: AppServerTransport,
    pub listen_port: u16,
    pub session_source: String,
    pub resume_thread_id: Option<String>,
    pub steer_text: Option<String>,
    pub steer_after_ms: u64,
    pub interrupt_after_ms: Option<u64>,
    pub auto_approve: bool,
    pub spawn_remote_tui: bool,
    pub remote_tui_grace_ms: u64,
    pub remote_tui_log: Option<PathBuf>,
    pub remote_tui_subscribe_phase: RemoteTuiSubscribePhase,
    pub probe_thread_read: bool,
    pub probe_thread_list: bool,
    pub event_timeout_secs: u64,
    pub log_jsonl: Option<PathBuf>,
    pub isolate_home: bool,
    pub keep_home: bool,
    pub verify_hooks: bool,
    pub ws_read_throttle_ms: u64,
    pub proxy_codex_ws: bool,
}

#[derive(Debug, Serialize)]
pub struct CanarySummary {
    pub codex_bin: String,
    pub cwd: String,
    pub app_server_transport: AppServerTransport,
    pub app_server_ws_url: Option<String>,
    pub session_source: String,
    pub sandbox: String,
    pub isolated_home: bool,
    pub effective_home_path: String,
    pub effective_codex_home_path: String,
    pub isolated_home_path: Option<String>,
    pub thread_id: String,
    pub thread_path: Option<String>,
    pub thread_path_exists: bool,
    pub thread_path_within_home: bool,
    pub turn_id: String,
    pub turn_status: String,
    pub assistant_text: String,
    pub hook_session_id: Option<String>,
    pub hook_states: Vec<String>,
    pub hook_notification_counts: BTreeMap<String, usize>,
    pub server_request_counts: BTreeMap<String, usize>,
    pub item_started_counts: BTreeMap<String, usize>,
    pub item_completed_counts: BTreeMap<String, usize>,
    pub thread_active_flag_counts: BTreeMap<String, usize>,
    pub thread_read_turn_count: Option<usize>,
    pub thread_list_count: Option<usize>,
    pub thread_list_contains_thread: Option<bool>,
    pub remote_tui_spawned: bool,
    pub remote_tui_alive_after_grace: Option<bool>,
    pub remote_tui_alive_before_shutdown: Option<bool>,
    pub remote_tui_subscribe_phase: String,
    pub remote_tui_log: Option<String>,
    pub remote_tui_stderr_lines: Vec<String>,
    pub log_jsonl: Option<String>,
    pub sent_requests: BTreeMap<String, usize>,
    pub received_notifications: BTreeMap<String, usize>,
    pub response_errors: Vec<String>,
    pub stderr_lines: Vec<String>,
}

#[derive(Debug)]
enum StreamEvent {
    Rpc(Value),
    Stderr(String),
    StdoutParseError(String),
}

#[derive(Debug)]
enum ScheduledAction {
    Steer(String),
    Interrupt,
}

#[derive(Debug)]
struct RpcClient {
    child: Child,
    outbound: RpcOutbound,
    events_rx: mpsc::UnboundedReceiver<StreamEvent>,
    next_request_id: u64,
    pending_methods: BTreeMap<u64, String>,
    ws_url: Option<String>,
}

#[derive(Debug)]
enum RpcOutbound {
    Stdio(ChildStdin),
    WebSocket(mpsc::UnboundedSender<String>),
}

#[derive(Debug)]
struct RemoteTuiHandle {
    child: Child,
    log_path: PathBuf,
    stderr_lines: Arc<Mutex<Vec<String>>>,
}

#[derive(Debug, Default)]
struct ObservationState {
    thread_id: Option<String>,
    thread_path: Option<String>,
    turn_id: Option<String>,
    turn_status: Option<String>,
    assistant_text: String,
    sent_requests: BTreeMap<String, usize>,
    received_notifications: BTreeMap<String, usize>,
    server_request_counts: BTreeMap<String, usize>,
    item_started_counts: BTreeMap<String, usize>,
    item_completed_counts: BTreeMap<String, usize>,
    thread_active_flag_counts: BTreeMap<String, usize>,
    response_errors: Vec<String>,
    stderr_lines: Vec<String>,
}

struct JsonlLogger {
    file: Option<File>,
}

impl JsonlLogger {
    fn new(path: Option<&Path>) -> Result<Self> {
        let file = match path {
            Some(path) => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent)
                        .with_context(|| format!("creating log directory {}", parent.display()))?;
                }
                Some(File::create(path).with_context(|| format!("creating {}", path.display()))?)
            }
            None => None,
        };
        Ok(Self { file })
    }

    fn write_value(&mut self, direction: &str, payload: Value) -> Result<()> {
        let Some(file) = self.file.as_mut() else {
            return Ok(());
        };
        let line = json!({
            "ts": Utc::now().to_rfc3339(),
            "direction": direction,
            "payload": payload,
        });
        serde_json::to_writer(&mut *file, &line)?;
        file.write_all(b"\n")?;
        file.flush()?;
        Ok(())
    }

    fn write_text(&mut self, direction: &str, text: &str) -> Result<()> {
        let Some(file) = self.file.as_mut() else {
            return Ok(());
        };
        let line = json!({
            "ts": Utc::now().to_rfc3339(),
            "direction": direction,
            "text": text,
        });
        serde_json::to_writer(&mut *file, &line)?;
        file.write_all(b"\n")?;
        file.flush()?;
        Ok(())
    }
}

pub async fn run(config: CanaryConfig) -> Result<CanarySummary> {
    if config.prompt.trim().is_empty() {
        bail!("prompt must not be empty");
    }
    if !config.cwd.is_dir() {
        bail!("cwd does not exist: {}", config.cwd.display());
    }
    if config.spawn_remote_tui && config.app_server_transport != AppServerTransport::WebSocket {
        bail!("--spawn-remote-tui requires --app-server-transport websocket");
    }
    if config.verify_hooks
        && config.resume_thread_id.is_some()
        && config.home_override.is_none()
        && config.isolate_home
    {
        bail!(
            "--verify-hooks with isolated HOME cannot resume an existing thread; use --real-home"
        );
    }

    let isolated_home = if let Some(home) = config.home_override.as_ref() {
        fs::create_dir_all(home.join(".codex"))?;
        fs::create_dir_all(home.join(".longhouse").join("agent").join("outbox"))?;
        prepare_isolated_home(home, config.verify_hooks).await?;
        None
    } else if config.isolate_home {
        let home = create_isolated_home()?;
        prepare_isolated_home(&home, config.verify_hooks).await?;
        Some(home)
    } else {
        None
    };
    let effective_home = config
        .home_override
        .clone()
        .or_else(|| isolated_home.clone())
        .unwrap_or(home_dir()?);
    let canonical_effective_home = effective_home
        .canonicalize()
        .unwrap_or_else(|_| effective_home.clone());

    let effective_codex_home = effective_home.join(".codex");
    let spawn_home_override = config.home_override.as_deref().or(isolated_home.as_deref());

    let hook_session_id = if config.verify_hooks {
        Some(Uuid::new_v4().to_string())
    } else {
        None
    };
    let outbox_dir = if config.verify_hooks {
        Some(
            effective_home
                .join(".longhouse")
                .join("agent")
                .join("outbox"),
        )
    } else {
        None
    };

    let mut logger = JsonlLogger::new(config.log_jsonl.as_deref())?;
    let mut client = spawn_client(
        &config,
        spawn_home_override,
        hook_session_id.as_deref(),
        &mut logger,
    )
    .await?;
    let app_server_ws_url = client.ws_url.clone();
    let mut state = ObservationState::default();
    let mut remote_tui = None;
    let mut remote_tui_alive_after_grace = None;

    let deadline = Instant::now() + Duration::from_secs(config.event_timeout_secs);
    let initialize_response = send_request(
        &config,
        &mut client,
        &mut state,
        &mut logger,
        "initialize",
        json!({
            "clientInfo": {
                "name": "longhouse_canary",
                "title": "Longhouse Codex App Server Canary",
                "version": env!("CARGO_PKG_VERSION"),
            },
            "capabilities": {
                "experimentalApi": true,
            }
        }),
        deadline,
    )
    .await?;
    let _ = initialize_response;
    send_notification(&mut client, &mut logger, "initialized", json!({})).await?;

    let needs_remote_tui_owned_thread =
        config.spawn_remote_tui && config.resume_thread_id.is_none();
    let subscribe_before_turn = needs_remote_tui_owned_thread
        && config.remote_tui_subscribe_phase == RemoteTuiSubscribePhase::PreTurn;
    let subscribe_after_turn = needs_remote_tui_owned_thread
        && config.remote_tui_subscribe_phase == RemoteTuiSubscribePhase::PostTurn;
    let subscribe_after_rollout = needs_remote_tui_owned_thread
        && config.remote_tui_subscribe_phase == RemoteTuiSubscribePhase::AfterRollout;
    let (thread_id, mut thread_path) = if needs_remote_tui_owned_thread {
        let mut handle = spawn_remote_tui(
            &config,
            app_server_ws_url
                .as_deref()
                .context("websocket transport did not expose a listen URL")?,
            None,
            spawn_home_override,
        )
        .await?;
        wait_for_thread_started(&config, &mut client, &mut state, &mut logger, deadline).await?;
        sleep(Duration::from_millis(config.remote_tui_grace_ms)).await;
        let alive = handle.is_alive().await?;
        remote_tui_alive_after_grace = Some(alive);
        if !alive {
            let stderr_lines = handle.stderr_lines().await;
            let log_tail = handle.log_tail(4000);
            bail!(
                "remote TUI exited early after launch. stderr={:?} log_tail={}",
                stderr_lines,
                log_tail
            );
        }
        let thread_id = state
            .thread_id
            .clone()
            .context("remote TUI did not emit thread/started")?;
        let mut thread_path = state.thread_path.clone();
        if subscribe_after_rollout && thread_path.is_none() {
            bail!(
                "remote TUI thread/started did not include a rollout path; cannot enforce after_rollout subscribe phase"
            );
        }
        if subscribe_before_turn {
            let resume_response = subscribe_to_thread(
                &config,
                &mut client,
                &mut state,
                &mut logger,
                deadline,
                &thread_id,
                thread_path.as_deref(),
            )
            .await?;
            thread_path = extract_string(&resume_response, &["thread", "path"]).or(thread_path);
            state.thread_path = thread_path.clone();
        }
        remote_tui = Some(handle);
        (thread_id, thread_path)
    } else {
        let thread_response = if let Some(thread_id) = config.resume_thread_id.as_deref() {
            send_request(
                &config,
                &mut client,
                &mut state,
                &mut logger,
                "thread/resume",
                json!({
                    "threadId": thread_id,
                    "cwd": config.cwd.to_string_lossy(),
                    "approvalPolicy": config.approval_policy,
                    "sandbox": config.sandbox,
                    "model": config.model.clone(),
                }),
                deadline,
            )
            .await?
        } else {
            send_request(
                &config,
                &mut client,
                &mut state,
                &mut logger,
                "thread/start",
                json!({
                    "cwd": config.cwd.to_string_lossy(),
                    "approvalPolicy": config.approval_policy,
                    "sandbox": config.sandbox,
                    "model": config.model.clone(),
                }),
                deadline,
            )
            .await?
        };
        let thread_id = extract_string(&thread_response, &["thread", "id"])
            .context("missing thread.id in thread response")?;
        let thread_path = extract_string(&thread_response, &["thread", "path"]);
        state.thread_id = Some(thread_id.clone());
        state.thread_path = thread_path.clone();

        if config.spawn_remote_tui {
            if let Some(path) = thread_path.as_deref() {
                wait_for_thread_rollout(Path::new(path), Duration::from_secs(5)).await?;
            }
            let mut handle = spawn_remote_tui(
                &config,
                app_server_ws_url
                    .as_deref()
                    .context("websocket transport did not expose a listen URL")?,
                Some(&thread_id),
                spawn_home_override,
            )
            .await?;
            sleep(Duration::from_millis(config.remote_tui_grace_ms)).await;
            let alive = handle.is_alive().await?;
            remote_tui_alive_after_grace = Some(alive);
            if !alive {
                let stderr_lines = handle.stderr_lines().await;
                let log_tail = handle.log_tail(4000);
                bail!(
                    "remote TUI exited early after launch. stderr={:?} log_tail={}",
                    stderr_lines,
                    log_tail
                );
            }
            remote_tui = Some(handle);
        }

        (thread_id, thread_path)
    };

    let mut turn_params = json!({
        "threadId": thread_id.clone(),
        "input": [{"type": "text", "text": config.prompt}],
    });
    if let Some(model) = config.model.as_deref() {
        turn_params["model"] = Value::String(model.to_string());
    }
    if let Some(effort) = config.effort.as_deref() {
        turn_params["effort"] = Value::String(effort.to_string());
    }
    let turn_response = send_request(
        &config,
        &mut client,
        &mut state,
        &mut logger,
        "turn/start",
        turn_params,
        deadline,
    )
    .await?;
    let turn_id = extract_string(&turn_response, &["turn", "id"])
        .context("missing turn.id in turn/start response")?;
    state.turn_id = Some(turn_id.clone());
    state.turn_status = extract_string(&turn_response, &["turn", "status"]);

    if subscribe_after_turn || subscribe_after_rollout {
        if subscribe_after_rollout {
            let path = thread_path
                .as_deref()
                .context("missing rollout path for after_rollout subscribe phase")?;
            wait_for_thread_rollout(Path::new(path), Duration::from_secs(5)).await?;
        }
        let resume_response = subscribe_to_thread(
            &config,
            &mut client,
            &mut state,
            &mut logger,
            deadline,
            &thread_id,
            thread_path.as_deref(),
        )
        .await?;
        thread_path = extract_string(&resume_response, &["thread", "path"]).or(thread_path);
        state.thread_path = thread_path.clone();
    }

    let (action_tx, mut action_rx) = mpsc::unbounded_channel();
    if let Some(text) = config.steer_text.clone() {
        let delay = config.steer_after_ms;
        let action_tx = action_tx.clone();
        tokio::spawn(async move {
            sleep(Duration::from_millis(delay)).await;
            let _ = action_tx.send(ScheduledAction::Steer(text));
        });
    }
    if let Some(delay) = config.interrupt_after_ms {
        let action_tx = action_tx.clone();
        tokio::spawn(async move {
            sleep(Duration::from_millis(delay)).await;
            let _ = action_tx.send(ScheduledAction::Interrupt);
        });
    }
    drop(action_tx);

    while state.turn_status.as_deref() != Some("completed")
        && state.turn_status.as_deref() != Some("failed")
        && state.turn_status.as_deref() != Some("interrupted")
    {
        tokio::select! {
            Some(action) = action_rx.recv() => {
                match action {
                    ScheduledAction::Steer(text) => {
                        send_request(
                            &config,
                            &mut client,
                            &mut state,
                            &mut logger,
                            "turn/steer",
                            json!({
                                "threadId": thread_id,
                                "expectedTurnId": turn_id,
                                "input": [{"type": "text", "text": text}],
                            }),
                            deadline,
                        ).await?;
                    }
                    ScheduledAction::Interrupt => {
                        send_request(
                            &config,
                            &mut client,
                            &mut state,
                            &mut logger,
                            "turn/interrupt",
                            json!({
                                "threadId": thread_id,
                                "turnId": turn_id,
                            }),
                            deadline,
                        ).await?;
                    }
                }
            }
            event = recv_event(&mut client, deadline) => {
                process_event(&config, event?, &mut client, &mut state, &mut logger).await?;
            }
        }
    }

    if config.verify_hooks {
        sleep(Duration::from_millis(750)).await;
    }

    let thread_read_turn_count = if config.probe_thread_read {
        let thread_read = send_request(
            &config,
            &mut client,
            &mut state,
            &mut logger,
            "thread/read",
            json!({
                "threadId": thread_id,
                "includeTurns": true,
            }),
            deadline,
        )
        .await?;
        thread_read
            .get("thread")
            .and_then(|thread| thread.get("turns"))
            .and_then(Value::as_array)
            .map(Vec::len)
    } else {
        None
    };

    let (thread_list_count, thread_list_contains_thread) = if config.probe_thread_list {
        let thread_list = send_request(
            &config,
            &mut client,
            &mut state,
            &mut logger,
            "thread/list",
            json!({
                "limit": 50,
                "cwd": config.cwd.to_string_lossy(),
                "sourceKinds": ["appServer", "custom", "cli", "vscode"],
            }),
            deadline,
        )
        .await?;
        let threads = thread_list
            .get("data")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let contains_thread = threads.iter().any(|thread| {
            thread
                .get("id")
                .and_then(Value::as_str)
                .is_some_and(|candidate| candidate == thread_id)
        });
        (Some(threads.len()), Some(contains_thread))
    } else {
        (None, None)
    };

    let hook_states = if let (Some(outbox_dir), Some(hook_session_id)) =
        (outbox_dir.as_deref(), hook_session_id.as_deref())
    {
        wait_for_hook_states(outbox_dir, hook_session_id).await?
    } else {
        Vec::new()
    };

    let remote_tui_alive_before_shutdown = match remote_tui.as_mut() {
        Some(handle) => Some(handle.is_alive().await?),
        None => None,
    };
    let remote_tui_log = remote_tui
        .as_ref()
        .map(|handle| handle.log_path.display().to_string());
    let remote_tui_stderr_lines = match remote_tui.as_ref() {
        Some(handle) => handle.stderr_lines().await,
        None => Vec::new(),
    };
    if let Some(handle) = remote_tui.as_mut() {
        handle.shutdown().await?;
    }

    shutdown_child(&mut client).await?;

    let isolated_home_path = if config.keep_home {
        isolated_home
            .as_ref()
            .map(|path| path.display().to_string())
    } else {
        if let Some(path) = isolated_home.as_deref() {
            let _ = fs::remove_dir_all(path);
        }
        None
    };

    let hook_notification_counts = state
        .received_notifications
        .iter()
        .filter(|(method, _)| method.starts_with("hook/"))
        .map(|(method, count)| (method.clone(), *count))
        .collect::<BTreeMap<_, _>>();

    let summary = CanarySummary {
        codex_bin: config.codex_bin,
        cwd: config.cwd.display().to_string(),
        app_server_transport: config.app_server_transport,
        app_server_ws_url,
        session_source: config.session_source,
        sandbox: config.sandbox,
        isolated_home: config.isolate_home,
        effective_home_path: effective_home.display().to_string(),
        effective_codex_home_path: effective_codex_home.display().to_string(),
        isolated_home_path,
        thread_id,
        thread_path: thread_path.clone(),
        thread_path_exists: thread_path
            .as_deref()
            .map(Path::new)
            .is_some_and(Path::exists),
        thread_path_within_home: thread_path.as_deref().map(Path::new).is_some_and(|path| {
            path.canonicalize()
                .unwrap_or_else(|_| path.to_path_buf())
                .starts_with(&canonical_effective_home)
        }),
        turn_id,
        turn_status: state.turn_status.unwrap_or_else(|| "unknown".to_string()),
        assistant_text: state.assistant_text.trim().to_string(),
        hook_session_id,
        hook_states,
        hook_notification_counts,
        server_request_counts: state.server_request_counts,
        item_started_counts: state.item_started_counts,
        item_completed_counts: state.item_completed_counts,
        thread_active_flag_counts: state.thread_active_flag_counts,
        thread_read_turn_count,
        thread_list_count,
        thread_list_contains_thread,
        remote_tui_spawned: config.spawn_remote_tui,
        remote_tui_alive_after_grace,
        remote_tui_alive_before_shutdown,
        remote_tui_subscribe_phase: config.remote_tui_subscribe_phase.as_str().to_string(),
        remote_tui_log,
        remote_tui_stderr_lines,
        log_jsonl: config.log_jsonl.map(|path| path.display().to_string()),
        sent_requests: state.sent_requests,
        received_notifications: state.received_notifications,
        response_errors: state.response_errors,
        stderr_lines: state.stderr_lines,
    };

    if config.verify_hooks
        && !contains_subsequence(&summary.hook_states, &["idle", "thinking", "idle"])
    {
        bail!(
            "Codex hooks did not produce the expected idle/thinking/idle sequence in isolated outbox. observed={:?} hook_notifications={:?}",
            summary.hook_states,
            summary.hook_notification_counts
        );
    }

    Ok(summary)
}

async fn spawn_client(
    config: &CanaryConfig,
    home_override: Option<&Path>,
    hook_session_id: Option<&str>,
    logger: &mut JsonlLogger,
) -> Result<RpcClient> {
    const TEXT_FILE_BUSY_OS_ERROR: i32 = 26;
    const SPAWN_ATTEMPTS: usize = 5;
    const SPAWN_RETRY_DELAY: Duration = Duration::from_millis(25);

    let mut child = None;
    for attempt in 0..SPAWN_ATTEMPTS {
        let mut command = app_server_command(config, home_override, hook_session_id);
        match command.spawn() {
            Ok(process) => {
                child = Some(process);
                break;
            }
            Err(error)
                if error.raw_os_error() == Some(TEXT_FILE_BUSY_OS_ERROR)
                    && attempt + 1 < SPAWN_ATTEMPTS =>
            {
                sleep(SPAWN_RETRY_DELAY).await;
            }
            Err(error) => {
                return Err(error)
                    .with_context(|| format!("spawning `{}` app-server", config.codex_bin));
            }
        }
    }
    let mut child = child.with_context(|| format!("spawning `{}` app-server", config.codex_bin))?;
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

    logger.write_text(
        "meta",
        &format!(
            "spawned {} app-server{}{}",
            config.codex_bin,
            home_override
                .map(|path| format!(" HOME={}", path.display()))
                .unwrap_or_default(),
            home_override
                .map(|path| format!(" CODEX_HOME={}", path.join(".codex").display()))
                .unwrap_or_default()
        ),
    )?;

    let (outbound, ws_url) = match config.app_server_transport {
        AppServerTransport::Stdio => (
            RpcOutbound::Stdio(child.stdin.take().context("missing app-server stdin")?),
            None,
        ),
        AppServerTransport::WebSocket => {
            let upstream_ws_url = tokio::time::timeout(Duration::from_secs(10), ws_listen_rx)
                .await
                .context("timed out waiting for websocket listen URL")?
                .context("app-server websocket listener did not announce a URL")?;
            let ws_url = if config.proxy_codex_ws {
                crate::codex_ws_relay::spawn(&upstream_ws_url)
                    .await
                    .with_context(|| format!("spawning WS relay in front of {upstream_ws_url}"))?
            } else {
                upstream_ws_url
            };
            let (ws_stream, _response) = connect_async(ws_url.as_str())
                .await
                .with_context(|| format!("connecting websocket client to {ws_url}"))?;
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
            let read_throttle_ms = config.ws_read_throttle_ms;
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
                    if read_throttle_ms > 0 {
                        tokio::time::sleep(Duration::from_millis(read_throttle_ms)).await;
                    }
                }
            });
            (RpcOutbound::WebSocket(outbound_tx), Some(ws_url))
        }
    };

    Ok(RpcClient {
        child,
        outbound,
        events_rx,
        next_request_id: 1,
        pending_methods: BTreeMap::new(),
        ws_url,
    })
}

fn app_server_command(
    config: &CanaryConfig,
    home_override: Option<&Path>,
    hook_session_id: Option<&str>,
) -> Command {
    let listen_arg = match config.app_server_transport {
        AppServerTransport::Stdio => "stdio://".to_string(),
        AppServerTransport::WebSocket => format!("ws://127.0.0.1:{}", config.listen_port),
    };
    let mut command = Command::new(&config.codex_bin);
    command
        .arg("app-server")
        .arg("--listen")
        .arg(&listen_arg)
        .arg("--enable")
        .arg("hooks")
        .arg("--enable")
        .arg("exec_permission_approvals")
        .arg("--enable")
        .arg("request_permissions_tool")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if config.app_server_transport == AppServerTransport::Stdio {
        command.stdin(Stdio::piped());
    } else {
        command.stdin(Stdio::null());
    }
    if let Some(home) = home_override {
        command.env("HOME", home);
        command.env("CODEX_HOME", home.join(".codex"));
    }
    if let Some(session_id) = hook_session_id {
        command.env("LONGHOUSE_MANAGED_SESSION_ID", session_id);
    }
    command
}

async fn send_notification(
    client: &mut RpcClient,
    logger: &mut JsonlLogger,
    method: &str,
    params: Value,
) -> Result<()> {
    let payload = json!({
        "method": method,
        "params": params,
    });
    logger.write_value("client_notification", payload.clone())?;
    send_payload(client, &payload).await
}

async fn send_payload(client: &mut RpcClient, payload: &Value) -> Result<()> {
    let line = serde_json::to_string(payload)?;
    match &mut client.outbound {
        RpcOutbound::Stdio(stdin) => {
            stdin.write_all(line.as_bytes()).await?;
            stdin.write_all(b"\n").await?;
            stdin.flush().await?;
        }
        RpcOutbound::WebSocket(tx) => {
            tx.send(line)
                .map_err(|_| anyhow!("websocket outbound channel closed"))?;
        }
    }
    Ok(())
}

async fn send_request(
    config: &CanaryConfig,
    client: &mut RpcClient,
    state: &mut ObservationState,
    logger: &mut JsonlLogger,
    method: &str,
    params: Value,
    deadline: Instant,
) -> Result<Value> {
    let request_id = client.next_request_id;
    client.next_request_id += 1;
    client
        .pending_methods
        .insert(request_id, method.to_string());
    *state.sent_requests.entry(method.to_string()).or_insert(0) += 1;
    let payload = json!({
        "id": request_id,
        "method": method,
        "params": params,
    });
    logger.write_value("client_request", payload.clone())?;
    send_payload(client, &payload).await?;

    loop {
        let event = recv_event(client, deadline).await?;
        match event {
            StreamEvent::Rpc(value) => {
                logger.write_value("server_message", value.clone())?;
                if value.get("id").is_some() && value.get("method").is_some() {
                    handle_server_request(config, value, client, state, logger).await?;
                    continue;
                }
                if let Some(id) = value.get("id").and_then(Value::as_u64) {
                    let method_name = client
                        .pending_methods
                        .remove(&id)
                        .unwrap_or_else(|| format!("request#{id}"));
                    if id == request_id {
                        if let Some(error) = value.get("error") {
                            let message = format!("{method_name} failed: {}", error);
                            state.response_errors.push(message.clone());
                            bail!(message);
                        }
                        return Ok(value.get("result").cloned().ok_or_else(|| {
                            anyhow!("response for {method_name} missing result")
                        })?);
                    }
                    if let Some(error) = value.get("error") {
                        state.response_errors.push(format!(
                            "{method_name} failed while waiting for {method}: {}",
                            error
                        ));
                    }
                } else {
                    process_rpc_value(value, state)?;
                }
            }
            StreamEvent::Stderr(line) => {
                logger.write_text("server_stderr", &line)?;
                state.stderr_lines.push(line);
            }
            StreamEvent::StdoutParseError(detail) => {
                logger.write_text("server_protocol_error", &detail)?;
                bail!("app-server emitted invalid JSONL on stdout: {detail}");
            }
        }
    }
}

async fn recv_event(client: &mut RpcClient, deadline: Instant) -> Result<StreamEvent> {
    let now = Instant::now();
    if now >= deadline {
        bail!("timed out waiting for app-server events");
    }
    let timeout = deadline - now;
    let event = tokio::time::timeout(timeout, client.events_rx.recv())
        .await
        .context("timed out waiting for app-server stream")?;
    match event {
        Some(event) => Ok(event),
        None => {
            if let Some(status) = client.child.try_wait()? {
                bail!("codex app-server exited early with status {status}");
            }
            bail!("codex app-server closed its output stream unexpectedly");
        }
    }
}

async fn process_event(
    config: &CanaryConfig,
    event: StreamEvent,
    client: &mut RpcClient,
    state: &mut ObservationState,
    logger: &mut JsonlLogger,
) -> Result<()> {
    match event {
        StreamEvent::Rpc(value) => {
            logger.write_value("server_message", value.clone())?;
            if value.get("id").is_some() && value.get("method").is_some() {
                handle_server_request(config, value, client, state, logger).await?;
                return Ok(());
            }
            if let Some(id) = value.get("id").and_then(Value::as_u64) {
                let method_name = client
                    .pending_methods
                    .remove(&id)
                    .unwrap_or_else(|| format!("request#{id}"));
                if let Some(error) = value.get("error") {
                    state
                        .response_errors
                        .push(format!("{method_name} failed: {}", error));
                }
                return Ok(());
            }
            process_rpc_value(value, state)?;
        }
        StreamEvent::Stderr(line) => {
            logger.write_text("server_stderr", &line)?;
            state.stderr_lines.push(line);
        }
        StreamEvent::StdoutParseError(detail) => {
            logger.write_text("server_protocol_error", &detail)?;
            bail!("app-server emitted invalid JSONL on stdout: {detail}");
        }
    }
    Ok(())
}

async fn wait_for_thread_started(
    config: &CanaryConfig,
    client: &mut RpcClient,
    state: &mut ObservationState,
    logger: &mut JsonlLogger,
    deadline: Instant,
) -> Result<()> {
    while state.thread_id.is_none() {
        let event = recv_event(client, deadline).await?;
        process_event(config, event, client, state, logger).await?;
    }
    Ok(())
}

async fn subscribe_to_thread(
    config: &CanaryConfig,
    client: &mut RpcClient,
    state: &mut ObservationState,
    logger: &mut JsonlLogger,
    deadline: Instant,
    thread_id: &str,
    thread_path: Option<&str>,
) -> Result<Value> {
    let mut params = json!({
        "threadId": thread_id,
        "cwd": config.cwd.to_string_lossy(),
        "approvalPolicy": config.approval_policy,
        "sandbox": config.sandbox,
        "model": config.model.clone(),
    });
    if let Some(path) = thread_path {
        params["path"] = Value::String(path.to_string());
    }

    let mut last_error = None;
    for attempt in 0..=20 {
        match send_request(
            config,
            client,
            state,
            logger,
            "thread/resume",
            params.clone(),
            deadline,
        )
        .await
        {
            Ok(response) => return Ok(response),
            Err(err) => {
                let error_text = err.to_string();
                last_error = Some(err);
                if is_retryable_thread_subscription_error(&error_text) && attempt < 20 {
                    sleep(Duration::from_millis(250)).await;
                    continue;
                }
                break;
            }
        }
    }

    Err(last_error.expect("thread subscribe retry loop should capture an error"))
}

fn is_retryable_thread_subscription_error(message: &str) -> bool {
    message.contains("no rollout found for thread id")
        || (message.contains("failed to load rollout") && message.contains("is empty"))
}

async fn handle_server_request(
    config: &CanaryConfig,
    value: Value,
    client: &mut RpcClient,
    state: &mut ObservationState,
    logger: &mut JsonlLogger,
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
    *state
        .server_request_counts
        .entry(method.to_string())
        .or_insert(0) += 1;

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
        _ => bail!(
            "received unsupported app-server request from server: {}",
            value
        ),
    };

    send_response(client, logger, request_id, result).await
}

async fn send_response(
    client: &mut RpcClient,
    logger: &mut JsonlLogger,
    request_id: Value,
    result: Value,
) -> Result<()> {
    let payload = json!({
        "id": request_id,
        "result": result,
    });
    logger.write_value("client_response", payload.clone())?;
    send_payload(client, &payload).await
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
                .unwrap_or_else(|| vec![Value::String("canary".to_string())])
        } else {
            Vec::new()
        };
        answers.insert(id.to_string(), json!({ "answers": question_answers }));
    }
    Value::Object(answers)
}

fn process_rpc_value(value: Value, state: &mut ObservationState) -> Result<()> {
    let Some(method) = value.get("method").and_then(Value::as_str) else {
        return Ok(());
    };
    *state
        .received_notifications
        .entry(method.to_string())
        .or_insert(0) += 1;
    let params = value.get("params").cloned().unwrap_or(Value::Null);
    match method {
        "thread/started" | "thread/status/changed" => {
            if let Some(id) = extract_string(&params, &["thread", "id"])
                .or_else(|| extract_string(&params, &["threadId"]))
            {
                state.thread_id = Some(id);
            }
            if let Some(path) = extract_string(&params, &["thread", "path"]) {
                state.thread_path = Some(path);
            }
            let status = params
                .get("status")
                .or_else(|| params.get("thread").and_then(|thread| thread.get("status")));
            if let Some(flags) = status
                .and_then(|value| value.get("activeFlags"))
                .and_then(Value::as_array)
            {
                for flag in flags {
                    if let Some(flag_name) = flag.as_str() {
                        *state
                            .thread_active_flag_counts
                            .entry(flag_name.to_string())
                            .or_insert(0) += 1;
                    }
                }
            }
        }
        "turn/started" | "turn/completed" => {
            if let Some(id) = extract_string(&params, &["turn", "id"]) {
                state.turn_id = Some(id);
            }
            if let Some(status) = extract_string(&params, &["turn", "status"]) {
                state.turn_status = Some(status);
            }
        }
        "item/started" => {
            if let Some(item_type) = extract_string(&params, &["item", "type"]) {
                *state.item_started_counts.entry(item_type).or_insert(0) += 1;
            }
        }
        "item/completed" => {
            if let Some(item_type) = extract_string(&params, &["item", "type"]) {
                *state.item_completed_counts.entry(item_type).or_insert(0) += 1;
            }
        }
        "item/agentMessage/delta" => {
            if let Some(delta) = extract_string(&params, &["delta"]) {
                state.assistant_text.push_str(&delta);
            }
        }
        _ => {}
    }
    Ok(())
}

async fn shutdown_child(client: &mut RpcClient) -> Result<()> {
    if let RpcOutbound::Stdio(stdin) = &mut client.outbound {
        let _ = stdin.shutdown().await;
    }
    if client.child.try_wait()?.is_none() {
        let _ = client.child.start_kill();
    }
    let _ = client.child.wait().await;
    Ok(())
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

async fn spawn_remote_tui(
    config: &CanaryConfig,
    ws_url: &str,
    thread_id: Option<&str>,
    home_override: Option<&Path>,
) -> Result<RemoteTuiHandle> {
    let log_path = config.remote_tui_log.clone().unwrap_or_else(|| {
        std::env::temp_dir().join(format!("longhouse-codex-remote-{}.log", Uuid::new_v4()))
    });
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("creating remote TUI log directory {}", parent.display()))?;
    }
    let remote_exec = match thread_id {
        Some(thread_id) => format!(
            "exec {} resume {} --enable tui_app_server --remote {} --no-alt-screen",
            shell_quote(&config.codex_bin),
            shell_quote(thread_id),
            shell_quote(ws_url),
        ),
        None => format!(
            "exec {} --enable tui_app_server --remote {} --no-alt-screen",
            shell_quote(&config.codex_bin),
            shell_quote(ws_url),
        ),
    };
    let remote_cmd = format!(
        "stty rows 40 cols 120 2>/dev/null || true; export LINES=40 COLUMNS=120 TERM=${{TERM:-xterm-256color}}; {remote_exec}"
    );
    let mut command = Command::new("script");
    command
        .arg("-q")
        .arg(&log_path)
        .arg("zsh")
        .arg("-lc")
        .arg(remote_cmd)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(home) = home_override {
        command.env("HOME", home);
        command.env("CODEX_HOME", home.join(".codex"));
    }
    let mut child = command
        .spawn()
        .context("spawning remote Codex TUI through `script`")?;
    let stderr = child
        .stderr
        .take()
        .context("missing remote TUI stderr pipe")?;
    let stderr_lines = Arc::new(Mutex::new(Vec::new()));
    let stderr_lines_clone = stderr_lines.clone();
    tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            stderr_lines_clone.lock().await.push(line);
        }
    });

    Ok(RemoteTuiHandle {
        child,
        log_path,
        stderr_lines,
    })
}

fn shell_quote(value: &str) -> String {
    let escaped = value.replace('\'', r#"'\''"#);
    format!("'{escaped}'")
}

impl RemoteTuiHandle {
    async fn is_alive(&mut self) -> Result<bool> {
        Ok(self.child.try_wait()?.is_none())
    }

    async fn stderr_lines(&self) -> Vec<String> {
        self.stderr_lines.lock().await.clone()
    }

    fn log_tail(&self, max_chars: usize) -> String {
        let text = fs::read_to_string(&self.log_path).unwrap_or_default();
        truncate_tail_chars(&text, max_chars)
    }

    async fn shutdown(&mut self) -> Result<()> {
        if self.child.try_wait()?.is_none() {
            let _ = self.child.start_kill();
        }
        let _ = self.child.wait().await;
        Ok(())
    }
}

fn extract_string(value: &Value, path: &[&str]) -> Option<String> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    current.as_str().map(ToString::to_string)
}

fn create_isolated_home() -> Result<PathBuf> {
    let root = std::env::temp_dir().join(format!("longhouse-codex-canary-{}", Uuid::new_v4()));
    fs::create_dir_all(root.join(".codex"))?;
    fs::create_dir_all(root.join(".longhouse").join("agent").join("outbox"))?;
    Ok(root)
}

async fn prepare_isolated_home(home: &Path, verify_hooks: bool) -> Result<()> {
    let real_home = home_dir()?;
    let source_codex = real_home.join(".codex");
    if !source_codex.exists() {
        bail!(
            "{} does not exist; Codex must be configured first",
            source_codex.display()
        );
    }

    copy_optional_file(
        &source_codex.join("auth.json"),
        &home.join(".codex").join("auth.json"),
    )?;
    copy_optional_file(
        &source_codex.join("config.toml"),
        &home.join(".codex").join("config.toml"),
    )?;
    copy_optional_tree(
        &source_codex.join("skills"),
        &home.join(".codex").join("skills"),
    )?;
    copy_optional_tree(
        &source_codex.join("plugins"),
        &home.join(".codex").join("plugins"),
    )?;

    if verify_hooks {
        copy_optional_file(
            &source_codex.join("hooks.json"),
            &home.join(".codex").join("hooks.json"),
        )?;
        copy_optional_tree(
            &source_codex.join("hooks"),
            &home.join(".codex").join("hooks"),
        )?;
        if !home.join(".codex").join("hooks.json").exists() {
            bail!(
                "{} is missing; install Codex hooks in the real HOME before using --verify-hooks",
                source_codex.join("hooks.json").display()
            );
        }
        rewrite_isolated_codex_hook_assets(home, &real_home)?;
    }

    Ok(())
}

fn copy_optional_file(source: &Path, destination: &Path) -> Result<()> {
    if !source.exists() {
        return Ok(());
    }
    if let Some(parent) = destination.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::copy(source, destination)
        .with_context(|| format!("copying {} -> {}", source.display(), destination.display()))?;
    Ok(())
}

fn copy_optional_tree(source: &Path, destination: &Path) -> Result<()> {
    if !source.exists() {
        return Ok(());
    }
    if destination.exists() {
        return Ok(());
    }
    let metadata = fs::symlink_metadata(source)
        .with_context(|| format!("reading metadata for {}", source.display()))?;
    if metadata.file_type().is_symlink() {
        let target = fs::read_link(source)?;
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(target, destination)?;
        }
        #[cfg(not(unix))]
        {
            let _ = target;
            bail!("symlinked Codex directories are only supported on unix for the canary");
        }
        return Ok(());
    }

    fs::create_dir_all(destination)?;
    for entry in WalkDir::new(source) {
        let entry = entry?;
        let relative = entry.path().strip_prefix(source)?;
        let target = destination.join(relative);
        if entry.file_type().is_dir() {
            fs::create_dir_all(&target)?;
            continue;
        }
        if entry.file_type().is_symlink() {
            let link_target = fs::read_link(entry.path())
                .with_context(|| format!("reading symlink {}", entry.path().display()))?;
            #[cfg(unix)]
            {
                std::os::unix::fs::symlink(link_target, &target).with_context(|| {
                    format!(
                        "symlinking {} -> {}",
                        entry.path().display(),
                        target.display()
                    )
                })?;
            }
            #[cfg(not(unix))]
            {
                let _ = link_target;
                bail!("symlinked Codex tree entries are only supported on unix for the canary");
            }
            continue;
        }
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::copy(entry.path(), &target).with_context(|| {
            format!("copying {} -> {}", entry.path().display(), target.display())
        })?;
    }
    Ok(())
}

fn rewrite_isolated_codex_hook_assets(home: &Path, real_home: &Path) -> Result<()> {
    let hooks_json_path = home.join(".codex").join("hooks.json");
    if hooks_json_path.exists() {
        let text = fs::read_to_string(&hooks_json_path)?;
        let source_hook = real_home
            .join(".codex")
            .join("hooks")
            .join("longhouse-codex-hook.sh")
            .display()
            .to_string();
        let isolated_hook = home
            .join(".codex")
            .join("hooks")
            .join("longhouse-codex-hook.sh")
            .display()
            .to_string();
        fs::write(&hooks_json_path, text.replace(&source_hook, &isolated_hook))?;
    }

    let hook_script_path = home
        .join(".codex")
        .join("hooks")
        .join("longhouse-codex-hook.sh");
    if hook_script_path.exists() {
        let text = fs::read_to_string(&hook_script_path)?;
        let source_longhouse_home = real_home.join(".longhouse").display().to_string();
        let isolated_longhouse_home = home.join(".longhouse").display().to_string();
        let source_outbox = real_home
            .join(".longhouse")
            .join("agent")
            .join("outbox")
            .display()
            .to_string();
        let isolated_outbox = home
            .join(".longhouse")
            .join("agent")
            .join("outbox")
            .display()
            .to_string();
        fs::write(
            &hook_script_path,
            text.replace(&source_longhouse_home, &isolated_longhouse_home)
                .replace(&source_outbox, &isolated_outbox),
        )?;
    }

    Ok(())
}

fn home_dir() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("HOME is not set"))
}

fn thread_rollout_is_ready(path: &Path) -> bool {
    fs::metadata(path)
        .map(|metadata| metadata.is_file() && metadata.len() > 0)
        .unwrap_or(false)
}

async fn wait_for_thread_rollout(path: &Path, timeout: Duration) -> Result<()> {
    let deadline = Instant::now() + timeout;
    loop {
        if thread_rollout_is_ready(path) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            bail!(
                "thread rollout did not materialize before remote TUI launch: {}",
                path.display()
            );
        }
        sleep(Duration::from_millis(50)).await;
    }
}

async fn wait_for_hook_states(outbox_dir: &Path, hook_session_id: &str) -> Result<Vec<String>> {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        let states = collect_hook_states(outbox_dir, hook_session_id)?;
        if contains_subsequence(&states, &["idle", "thinking", "idle"])
            || Instant::now() >= deadline
        {
            return Ok(states);
        }
        sleep(Duration::from_millis(250)).await;
    }
}

fn collect_hook_states(outbox_dir: &Path, hook_session_id: &str) -> Result<Vec<String>> {
    if !outbox_dir.exists() {
        return Ok(Vec::new());
    }
    let mut matches: Vec<(std::time::SystemTime, String)> = Vec::new();
    for entry in fs::read_dir(outbox_dir)? {
        let entry = entry?;
        let path = entry.path();
        let name = path
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or_default();
        if !name.starts_with("prs.") || !name.ends_with(".json") {
            continue;
        }
        let text = fs::read_to_string(&path)?;
        let Ok(value) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        if extract_string(&value, &["session_id"]).as_deref() != Some(hook_session_id) {
            continue;
        }
        let state = match extract_string(&value, &["state"]) {
            Some(state) => state,
            None => continue,
        };
        let modified = fs::metadata(&path)
            .and_then(|metadata| metadata.modified())
            .unwrap_or(std::time::SystemTime::UNIX_EPOCH);
        matches.push((modified, state));
    }
    matches.sort_by_key(|(modified, _)| *modified);
    Ok(matches.into_iter().map(|(_, state)| state).collect())
}

fn contains_subsequence(states: &[String], wanted: &[&str]) -> bool {
    if wanted.is_empty() {
        return true;
    }
    let mut idx = 0usize;
    for state in states {
        if state == wanted[idx] {
            idx += 1;
            if idx == wanted.len() {
                return true;
            }
        }
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn contains_subsequence_handles_expected_hook_order() {
        let states = vec![
            "idle".to_string(),
            "thinking".to_string(),
            "idle".to_string(),
        ];
        assert!(contains_subsequence(&states, &["idle", "thinking", "idle"]));
        assert!(!contains_subsequence(
            &states,
            &["thinking", "idle", "thinking"]
        ));
    }

    #[test]
    fn extract_websocket_listen_url_parses_startup_line() {
        assert_eq!(
            extract_websocket_listen_url("  listening on: ws://127.0.0.1:4601"),
            Some("ws://127.0.0.1:4601".to_string())
        );
        assert_eq!(
            extract_websocket_listen_url("readyz: http://127.0.0.1:4601/readyz"),
            None
        );
    }

    #[test]
    fn thread_rollout_is_ready_requires_non_empty_file() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("rollout.jsonl");
        assert!(!thread_rollout_is_ready(&path));
        fs::write(&path, "").unwrap();
        assert!(!thread_rollout_is_ready(&path));
        fs::write(&path, "{\"ok\":true}\n").unwrap();
        assert!(thread_rollout_is_ready(&path));
    }

    #[test]
    fn parse_remote_tui_subscribe_phase_accepts_rollout_alias() {
        assert_eq!(
            parse_remote_tui_subscribe_phase("rollout").unwrap(),
            RemoteTuiSubscribePhase::AfterRollout
        );
        assert_eq!(
            parse_remote_tui_subscribe_phase("after_rollout").unwrap(),
            RemoteTuiSubscribePhase::AfterRollout
        );
    }

    #[cfg(unix)]
    #[test]
    fn copy_optional_tree_preserves_symlinked_directories_inside_tree() {
        let temp = tempfile::tempdir().unwrap();
        let source = temp.path().join("source");
        let destination = temp.path().join("destination");
        let real = temp.path().join("real-plugin");
        fs::create_dir_all(source.join("cache")).unwrap();
        fs::create_dir_all(&real).unwrap();
        fs::write(real.join("plugin.json"), "{}").unwrap();
        std::os::unix::fs::symlink(&real, source.join("cache").join("latest")).unwrap();

        copy_optional_tree(&source, &destination).unwrap();

        let copied = destination.join("cache").join("latest");
        assert!(fs::symlink_metadata(&copied)
            .unwrap()
            .file_type()
            .is_symlink());
        assert_eq!(fs::read_link(copied).unwrap(), real);
    }

    #[tokio::test]
    async fn wait_for_thread_rollout_retries_until_file_is_non_empty() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("rollout.jsonl");
        let writer_path = path.clone();
        tokio::spawn(async move {
            sleep(Duration::from_millis(75)).await;
            fs::write(&writer_path, "").unwrap();
            sleep(Duration::from_millis(75)).await;
            fs::write(&writer_path, "{\"ok\":true}\n").unwrap();
        });
        wait_for_thread_rollout(&path, Duration::from_secs(1))
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn canary_runs_against_fake_codex_app_server() {
        let temp = tempfile::tempdir().unwrap();
        let workspace = temp.path().join("workspace");
        fs::create_dir_all(&workspace).unwrap();
        let bin = temp.path().join("codex");
        fs::write(
            &bin,
            r#"#!/usr/bin/env python3
import json
import sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

if len(sys.argv) < 2 or sys.argv[1] != "app-server":
    sys.exit(2)

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        emit({"id": msg["id"], "result": {"platformFamily": "unix", "platformOs": "linux", "userAgent": "fake/1.0"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        emit({"method": "thread/started", "params": {"thread": {"id": "thr_test"}}})
        emit({"id": msg["id"], "result": {"thread": {"id": "thr_test"}}})
    elif method == "turn/start":
        emit({"id": msg["id"], "result": {"turn": {"id": "turn_test", "status": "inProgress", "items": []}}})
        emit({"method": "turn/started", "params": {"threadId": "thr_test", "turn": {"id": "turn_test", "status": "inProgress", "items": []}}})
        emit({"method": "item/started", "params": {"threadId": "thr_test", "turnId": "turn_test", "item": {"id": "item_1", "type": "assistantMessage"}}})
        emit({"method": "item/agentMessage/delta", "params": {"threadId": "thr_test", "turnId": "turn_test", "itemId": "item_1", "delta": "CANARY"}})
        emit({"method": "item/completed", "params": {"threadId": "thr_test", "turnId": "turn_test", "item": {"id": "item_1", "type": "assistantMessage"}}})
        emit({"method": "item/started", "params": {"threadId": "thr_test", "turnId": "turn_test", "item": {"id": "cmd_1", "type": "commandExecution", "status": "inProgress", "command": "pwd"}}})
        emit({"method": "item/commandExecution/outputDelta", "params": {"threadId": "thr_test", "turnId": "turn_test", "itemId": "cmd_1", "delta": "/tmp/workspace\\n"}})
        emit({"method": "item/completed", "params": {"threadId": "thr_test", "turnId": "turn_test", "item": {"id": "cmd_1", "type": "commandExecution", "status": "completed", "command": "pwd"}}})
        emit({"method": "turn/completed", "params": {"threadId": "thr_test", "turn": {"id": "turn_test", "status": "completed", "items": []}}})
    elif method == "thread/read":
        emit({"id": msg["id"], "result": {"thread": {"id": "thr_test", "turns": [{"id": "turn_test"}]}}})
    elif method == "thread/list":
        emit({"id": msg["id"], "result": {"data": [{"id": "thr_test"}], "hasMore": False}})
    else:
        emit({"id": msg["id"], "result": {}})
"#,
        )
        .unwrap();
        let mut perms = fs::metadata(&bin).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&bin, perms).unwrap();

        let log_path = temp.path().join("canary.jsonl");
        let summary = run(CanaryConfig {
            prompt: "Reply with CANARY".to_string(),
            cwd: workspace,
            home_override: None,
            approval_policy: "never".to_string(),
            sandbox: "read-only".to_string(),
            model: None,
            effort: None,
            codex_bin: bin.display().to_string(),
            app_server_transport: AppServerTransport::Stdio,
            listen_port: 0,
            session_source: "longhouse-test".to_string(),
            resume_thread_id: None,
            steer_text: None,
            steer_after_ms: 250,
            interrupt_after_ms: None,
            auto_approve: false,
            spawn_remote_tui: false,
            remote_tui_subscribe_phase: RemoteTuiSubscribePhase::PostTurn,
            remote_tui_grace_ms: 3000,
            remote_tui_log: None,
            probe_thread_read: true,
            probe_thread_list: true,
            event_timeout_secs: 5,
            log_jsonl: Some(log_path.clone()),
            isolate_home: false,
            keep_home: false,
            verify_hooks: false,
            ws_read_throttle_ms: 0,
            proxy_codex_ws: false,
        })
        .await
        .unwrap();

        assert_eq!(summary.thread_id, "thr_test");
        assert_eq!(summary.thread_path, None);
        assert!(!summary.thread_path_within_home);
        assert_eq!(summary.turn_id, "turn_test");
        assert_eq!(summary.turn_status, "completed");
        assert_eq!(summary.assistant_text, "CANARY");
        assert_eq!(summary.thread_read_turn_count, Some(1));
        assert_eq!(summary.thread_list_count, Some(1));
        assert_eq!(summary.thread_list_contains_thread, Some(true));
        assert_eq!(
            summary.item_started_counts.get("assistantMessage"),
            Some(&1)
        );
        assert_eq!(
            summary.item_completed_counts.get("assistantMessage"),
            Some(&1)
        );
        assert_eq!(
            summary.item_started_counts.get("commandExecution"),
            Some(&1)
        );
        assert_eq!(
            summary.item_completed_counts.get("commandExecution"),
            Some(&1)
        );
        assert!(
            summary
                .received_notifications
                .get("turn/completed")
                .copied()
                .unwrap_or_default()
                >= 1
        );
        assert_eq!(
            summary
                .received_notifications
                .get("item/commandExecution/outputDelta"),
            Some(&1)
        );
        let log_text = fs::read_to_string(log_path).unwrap();
        assert!(log_text.contains("\"direction\":\"client_request\""));
        assert!(log_text.contains("\"direction\":\"server_message\""));
    }

    #[tokio::test]
    async fn canary_auto_approves_server_requests() {
        let temp = tempfile::tempdir().unwrap();
        let workspace = temp.path().join("workspace");
        fs::create_dir_all(&workspace).unwrap();
        let bin = temp.path().join("codex");
        fs::write(
            &bin,
            r#"#!/usr/bin/env python3
import json
import sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def expect_response(expected_id, check):
    line = sys.stdin.readline()
    if not line:
        sys.exit(3)
    msg = json.loads(line)
    if msg.get("id") != expected_id:
        sys.exit(4)
    if not check(msg.get("result", {})):
        sys.exit(5)

if len(sys.argv) < 2 or sys.argv[1] != "app-server":
    sys.exit(2)

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        emit({"id": msg["id"], "result": {"platformFamily": "unix", "platformOs": "linux", "userAgent": "fake/1.0"}})
    elif method == "initialized":
        pass
    elif method == "thread/start":
        emit({"method": "thread/started", "params": {"thread": {"id": "thr_test"}}})
        emit({"id": msg["id"], "result": {"thread": {"id": "thr_test"}}})
    elif method == "turn/start":
        emit({"id": msg["id"], "result": {"turn": {"id": "turn_test", "status": "inProgress", "items": []}}})
        emit({"method": "turn/started", "params": {"threadId": "thr_test", "turn": {"id": "turn_test", "status": "inProgress", "items": []}}})
        emit({"method": "thread/status/changed", "params": {"threadId": "thr_test", "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}})
        emit({"id": 99, "method": "item/commandExecution/requestApproval", "params": {"threadId": "thr_test", "turnId": "turn_test", "itemId": "cmd_1"}})
        expect_response(99, lambda result: result.get("decision") == "accept")
        emit({"id": 100, "method": "item/permissions/requestApproval", "params": {"threadId": "thr_test", "turnId": "turn_test", "itemId": "perm_1", "permissions": {"network": {"hosts": ["example.com"]}}}})
        expect_response(100, lambda result: result.get("scope") == "turn" and result.get("permissions", {}).get("network", {}).get("hosts") == ["example.com"])
        emit({"method": "thread/status/changed", "params": {"threadId": "thr_test", "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]}}})
        emit({"id": 101, "method": "item/tool/requestUserInput", "params": {"threadId": "thr_test", "turnId": "turn_test", "itemId": "input_1", "questions": [{"id": "color", "header": "Color", "question": "Pick one", "options": [{"label": "blue", "description": "Blue"}, {"label": "red", "description": "Red"}]}]}})
        expect_response(101, lambda result: result.get("answers", {}).get("color", {}).get("answers") == ["blue"])
        emit({"method": "thread/status/changed", "params": {"threadId": "thr_test", "status": {"type": "active", "activeFlags": []}}})
        emit({"method": "turn/completed", "params": {"threadId": "thr_test", "turn": {"id": "turn_test", "status": "completed", "items": []}}})
    else:
        emit({"id": msg["id"], "result": {}})
"#,
        )
        .unwrap();
        let mut perms = fs::metadata(&bin).unwrap().permissions();
        perms.set_mode(0o755);
        fs::set_permissions(&bin, perms).unwrap();

        let summary = run(CanaryConfig {
            prompt: "Exercise approvals".to_string(),
            cwd: workspace,
            home_override: None,
            approval_policy: "on-request".to_string(),
            sandbox: "workspace-write".to_string(),
            model: None,
            effort: None,
            codex_bin: bin.display().to_string(),
            app_server_transport: AppServerTransport::Stdio,
            listen_port: 0,
            session_source: "longhouse-test".to_string(),
            resume_thread_id: None,
            steer_text: None,
            steer_after_ms: 250,
            interrupt_after_ms: None,
            auto_approve: true,
            spawn_remote_tui: false,
            remote_tui_subscribe_phase: RemoteTuiSubscribePhase::PostTurn,
            remote_tui_grace_ms: 3000,
            remote_tui_log: None,
            probe_thread_read: false,
            probe_thread_list: false,
            event_timeout_secs: 5,
            log_jsonl: None,
            isolate_home: false,
            keep_home: false,
            verify_hooks: false,
            ws_read_throttle_ms: 0,
            proxy_codex_ws: false,
        })
        .await
        .unwrap();

        assert_eq!(
            summary
                .server_request_counts
                .get("item/commandExecution/requestApproval"),
            Some(&1)
        );
        assert_eq!(
            summary
                .server_request_counts
                .get("item/permissions/requestApproval"),
            Some(&1)
        );
        assert_eq!(
            summary
                .server_request_counts
                .get("item/tool/requestUserInput"),
            Some(&1)
        );
        assert_eq!(
            summary.thread_active_flag_counts.get("waitingOnApproval"),
            Some(&1)
        );
        assert_eq!(
            summary.thread_active_flag_counts.get("waitingOnUserInput"),
            Some(&1)
        );
        assert_eq!(summary.turn_status, "completed");
    }
}
