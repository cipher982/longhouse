use std::collections::BTreeMap;
use std::fs;
use std::fs::File;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::mpsc;
use tokio::time::{sleep, Instant};
use uuid::Uuid;
use walkdir::WalkDir;

#[derive(Debug, Clone)]
pub struct CanaryConfig {
    pub prompt: String,
    pub cwd: PathBuf,
    pub home_override: Option<PathBuf>,
    pub model: Option<String>,
    pub effort: Option<String>,
    pub codex_bin: String,
    pub session_source: String,
    pub resume_thread_id: Option<String>,
    pub steer_text: Option<String>,
    pub steer_after_ms: u64,
    pub interrupt_after_ms: Option<u64>,
    pub event_timeout_secs: u64,
    pub log_jsonl: Option<PathBuf>,
    pub isolate_home: bool,
    pub keep_home: bool,
    pub verify_hooks: bool,
}

#[derive(Debug, Serialize)]
pub struct CanarySummary {
    pub codex_bin: String,
    pub cwd: String,
    pub session_source: String,
    pub isolated_home: bool,
    pub effective_home_path: String,
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
    stdin: ChildStdin,
    events_rx: mpsc::UnboundedReceiver<StreamEvent>,
    next_request_id: u64,
    pending_methods: BTreeMap<u64, String>,
}

#[derive(Debug, Default)]
struct ObservationState {
    thread_id: Option<String>,
    turn_id: Option<String>,
    turn_status: Option<String>,
    assistant_text: String,
    sent_requests: BTreeMap<String, usize>,
    received_notifications: BTreeMap<String, usize>,
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
        fs::create_dir_all(home.join(".claude").join("outbox"))?;
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

    let hook_session_id = if config.verify_hooks {
        Some(Uuid::new_v4().to_string())
    } else {
        None
    };
    let outbox_dir = if config.verify_hooks {
        Some(effective_home.join(".claude").join("outbox"))
    } else {
        None
    };

    let mut logger = JsonlLogger::new(config.log_jsonl.as_deref())?;
    let mut client = spawn_client(
        &config,
        isolated_home.as_deref(),
        hook_session_id.as_deref(),
        &mut logger,
    )
    .await?;
    let mut state = ObservationState::default();

    let deadline = Instant::now() + Duration::from_secs(config.event_timeout_secs);
    let initialize_response = send_request(
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

    let thread_response = if let Some(thread_id) = config.resume_thread_id.as_deref() {
        send_request(
            &mut client,
            &mut state,
            &mut logger,
            "thread/resume",
            json!({
                "threadId": thread_id,
                "cwd": config.cwd.to_string_lossy(),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "model": config.model.clone(),
            }),
            deadline,
        )
        .await?
    } else {
        send_request(
            &mut client,
            &mut state,
            &mut logger,
            "thread/start",
            json!({
                "cwd": config.cwd.to_string_lossy(),
                "approvalPolicy": "never",
                "sandbox": "read-only",
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

    let mut turn_params = json!({
        "threadId": thread_id,
        "input": [{"type": "text", "text": config.prompt}],
    });
    if let Some(model) = config.model.as_deref() {
        turn_params["model"] = Value::String(model.to_string());
    }
    if let Some(effort) = config.effort.as_deref() {
        turn_params["effort"] = Value::String(effort.to_string());
    }
    let turn_response = send_request(
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
                process_event(event?, &mut client, &mut state, &mut logger)?;
            }
        }
    }

    if config.verify_hooks {
        sleep(Duration::from_millis(750)).await;
    }

    let hook_states = if let (Some(outbox_dir), Some(hook_session_id)) =
        (outbox_dir.as_deref(), hook_session_id.as_deref())
    {
        wait_for_hook_states(outbox_dir, hook_session_id).await?
    } else {
        Vec::new()
    };

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

    let summary = CanarySummary {
        codex_bin: config.codex_bin,
        cwd: config.cwd.display().to_string(),
        session_source: config.session_source,
        isolated_home: config.isolate_home,
        effective_home_path: effective_home.display().to_string(),
        isolated_home_path,
        thread_id,
        thread_path: thread_path.clone(),
        thread_path_exists: thread_path
            .as_deref()
            .map(Path::new)
            .is_some_and(Path::exists),
        thread_path_within_home: thread_path
            .as_deref()
            .map(Path::new)
            .is_some_and(|path| path.starts_with(&effective_home)),
        turn_id,
        turn_status: state.turn_status.unwrap_or_else(|| "unknown".to_string()),
        assistant_text: state.assistant_text.trim().to_string(),
        hook_session_id,
        hook_states,
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
            "Codex hooks did not produce the expected idle/thinking/idle sequence in isolated outbox. observed={:?}",
            summary.hook_states
        );
    }

    Ok(summary)
}

async fn spawn_client(
    config: &CanaryConfig,
    isolated_home: Option<&Path>,
    hook_session_id: Option<&str>,
    logger: &mut JsonlLogger,
) -> Result<RpcClient> {
    let mut command = Command::new(&config.codex_bin);
    command
        .arg("app-server")
        .arg("--listen")
        .arg("stdio://")
        .arg("--session-source")
        .arg(&config.session_source)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(home) = isolated_home {
        command.env("HOME", home);
    }
    if let Some(session_id) = hook_session_id {
        command.env("LONGHOUSE_SESSION_ID", session_id);
    }

    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{}` app-server", config.codex_bin))?;
    let stdin = child.stdin.take().context("missing app-server stdin")?;
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
    tokio::spawn(async move {
        let mut lines = BufReader::new(stderr).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = stderr_tx.send(StreamEvent::Stderr(line));
        }
    });

    logger.write_text(
        "meta",
        &format!(
            "spawned {} app-server{}",
            config.codex_bin,
            isolated_home
                .map(|path| format!(" HOME={}", path.display()))
                .unwrap_or_default()
        ),
    )?;

    Ok(RpcClient {
        child,
        stdin,
        events_rx,
        next_request_id: 1,
        pending_methods: BTreeMap::new(),
    })
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
    let line = serde_json::to_string(&payload)?;
    client.stdin.write_all(line.as_bytes()).await?;
    client.stdin.write_all(b"\n").await?;
    client.stdin.flush().await?;
    Ok(())
}

async fn send_request(
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
    let line = serde_json::to_string(&payload)?;
    client.stdin.write_all(line.as_bytes()).await?;
    client.stdin.write_all(b"\n").await?;
    client.stdin.flush().await?;

    loop {
        let event = recv_event(client, deadline).await?;
        match event {
            StreamEvent::Rpc(value) => {
                logger.write_value("server_message", value.clone())?;
                if value.get("id").is_some() && value.get("method").is_some() {
                    bail!("received unsupported app-server request from server while waiting for {method}: {}", value);
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

fn process_event(
    event: StreamEvent,
    client: &mut RpcClient,
    state: &mut ObservationState,
    logger: &mut JsonlLogger,
) -> Result<()> {
    match event {
        StreamEvent::Rpc(value) => {
            logger.write_value("server_message", value.clone())?;
            if value.get("id").is_some() && value.get("method").is_some() {
                bail!(
                    "received unsupported app-server request from server: {}",
                    value
                );
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
            if let Some(id) = extract_string(&params, &["thread", "id"]) {
                state.thread_id = Some(id);
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
    let _ = client.stdin.shutdown().await;
    if client.child.try_wait()?.is_none() {
        let _ = client.child.start_kill();
    }
    let _ = client.child.wait().await;
    Ok(())
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
    fs::create_dir_all(root.join(".claude").join("outbox"))?;
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
        let source_outbox = real_home
            .join(".claude")
            .join("outbox")
            .display()
            .to_string();
        let isolated_outbox = home.join(".claude").join("outbox").display().to_string();
        fs::write(
            &hook_script_path,
            text.replace(&source_outbox, &isolated_outbox),
        )?;
    }

    Ok(())
}

fn home_dir() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("HOME is not set"))
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
        emit({"method": "turn/completed", "params": {"threadId": "thr_test", "turn": {"id": "turn_test", "status": "completed", "items": []}}})
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
            model: None,
            effort: None,
            codex_bin: bin.display().to_string(),
            session_source: "longhouse-test".to_string(),
            resume_thread_id: None,
            steer_text: None,
            steer_after_ms: 250,
            interrupt_after_ms: None,
            event_timeout_secs: 5,
            log_jsonl: Some(log_path.clone()),
            isolate_home: false,
            keep_home: false,
            verify_hooks: false,
        })
        .await
        .unwrap();

        assert_eq!(summary.thread_id, "thr_test");
        assert_eq!(summary.thread_path, None);
        assert!(!summary.thread_path_within_home);
        assert_eq!(summary.turn_id, "turn_test");
        assert_eq!(summary.turn_status, "completed");
        assert_eq!(summary.assistant_text, "CANARY");
        assert!(
            summary
                .received_notifications
                .get("turn/completed")
                .copied()
                .unwrap_or_default()
                >= 1
        );
        let log_text = fs::read_to_string(log_path).unwrap();
        assert!(log_text.contains("\"direction\":\"client_request\""));
        assert!(log_text.contains("\"direction\":\"server_message\""));
    }
}
