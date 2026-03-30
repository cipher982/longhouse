use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::{connect_async, tungstenite::Message};
use uuid::Uuid;

const BRIDGE_RUNTIME_SOURCE: &str = "codex_bridge";
const DEFAULT_PROGRESS_THROTTLE_MS: u64 = 1500;
const DEFAULT_SHIP_DELAY_MS: u64 = 150;

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
    pub machine_name: Option<String>,
    pub auto_approve: bool,
    pub state_root: Option<PathBuf>,
    pub log_file: Option<PathBuf>,
    pub start_timeout_secs: u64,
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
    pub machine_name: Option<String>,
    pub auto_approve: bool,
    pub state_file: PathBuf,
    pub log_file: PathBuf,
}

#[derive(Debug, Clone)]
pub struct BridgeSendConfig {
    pub session_id: String,
    pub text: String,
    pub state_root: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct BridgeInterruptConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
}

#[derive(Debug, Clone)]
pub struct BridgeAttachConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub codex_bin: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BridgeStateFile {
    pub session_id: String,
    pub cwd: String,
    pub codex_bin: String,
    pub ws_url: Option<String>,
    pub thread_id: Option<String>,
    pub thread_path: Option<String>,
    pub pid: u32,
    pub status: String,
    pub log_file: String,
    pub active_turn_id: Option<String>,
    pub last_turn_status: Option<String>,
    pub last_error: Option<String>,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct BridgeStartSummary {
    pub session_id: String,
    pub state_file: String,
    pub log_file: String,
    pub pid: u32,
    pub ws_url: String,
    pub thread_id: String,
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
    outbound: RpcOutbound,
    events_rx: mpsc::UnboundedReceiver<StreamEvent>,
    pending_methods: BTreeMap<u64, String>,
    next_request_id: u64,
    ws_url: String,
}

#[derive(Debug)]
struct BridgeRuntimeSink {
    http: reqwest::Client,
    api_url: String,
    api_token: String,
    session_id: String,
    machine_name: Option<String>,
    thread_id: Option<String>,
}

#[derive(Debug)]
struct BridgeContext {
    state_file: PathBuf,
    state: BridgeStateFile,
    runtime: BridgeRuntimeSink,
    current_exe: PathBuf,
    last_progress_emit: Option<Instant>,
}

#[derive(Debug, Clone)]
struct ResolvedBridgePaths {
    state_file: PathBuf,
    log_file: PathBuf,
}

/// IPC command sent from `send` to the running daemon via Unix socket.
struct IpcCommand {
    text: String,
    thread_id: String,
    reply: oneshot::Sender<Result<BridgeSendSummary>>,
}

fn ipc_socket_path(state_file: &Path) -> PathBuf {
    state_file.with_extension("sock")
}

/// Spawn a Unix socket listener that accepts IPC commands from `send` callers.
/// Each connection reads a single JSON line `{"text": "...", "thread_id": "..."}`,
/// forwards it as an `IpcCommand` to the daemon loop, and writes back the JSON result.
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

    let (reply_tx, reply_rx) = oneshot::channel();
    tx.send(IpcCommand {
        text,
        thread_id,
        reply: reply_tx,
    })
    .map_err(|_| anyhow!("daemon event loop closed"))?;

    let result = reply_rx
        .await
        .map_err(|_| anyhow!("daemon dropped reply channel"))?;

    let response = match result {
        Ok(summary) => json!({
            "ok": true,
            "session_id": summary.session_id,
            "thread_id": summary.thread_id,
            "turn_id": summary.turn_id,
            "turn_status": summary.turn_status,
        }),
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
        session_id: config.session_id.clone(),
        cwd: config.cwd.display().to_string(),
        codex_bin: config.codex_bin.clone(),
        ws_url: None,
        thread_id: None,
        thread_path: None,
        pid: 0,
        status: "starting".to_string(),
        log_file: paths.log_file.display().to_string(),
        active_turn_id: None,
        last_turn_status: None,
        last_error: None,
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
    if let Some(policy) = config.approval_policy.as_deref() {
        child.arg("--approval-policy").arg(policy);
    }
    if let Some(sandbox) = config.sandbox.as_deref() {
        child.arg("--sandbox").arg(sandbox);
    }
    if let Some(model) = config.model.as_deref() {
        child.arg("--model").arg(model);
    }
    if let Some(machine_name) = config.machine_name.as_deref() {
        child.arg("--machine-name").arg(machine_name);
    }
    if config.auto_approve {
        child.arg("--auto-approve");
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
            let thread_id = state
                .thread_id
                .clone()
                .context("bridge marked ready without thread_id")?;
            return Ok(BridgeStartSummary {
                session_id: state.session_id,
                state_file: paths.state_file.display().to_string(),
                log_file: paths.log_file.display().to_string(),
                pid: state.pid,
                ws_url,
                thread_id,
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
    let initial_state = BridgeStateFile {
        session_id: config.session_id.clone(),
        cwd: config.cwd.display().to_string(),
        codex_bin: config.codex_bin.clone(),
        ws_url: None,
        thread_id: None,
        thread_path: None,
        pid,
        status: "starting".to_string(),
        log_file: config.log_file.display().to_string(),
        active_turn_id: None,
        last_turn_status: None,
        last_error: None,
        updated_at: Utc::now().to_rfc3339(),
    };
    write_state_file(&config.state_file, &initial_state)?;

    let current_exe =
        std::env::current_exe().context("resolving current executable for codex-bridge run")?;
    let mut client = spawn_app_server_client(&config).await?;
    let ws_url = client.ws_url.clone();
    let thread_response = start_managed_thread(&mut client, &config).await?;
    let thread_id = extract_string(&thread_response, &["thread", "id"])
        .context("missing thread.id in codex bridge thread/start response")?;
    let thread_path = extract_string(&thread_response, &["thread", "path"]);

    // Seed the rollout file so `codex resume <thread_id> --remote` can find it.
    // The app-server creates the thread in memory but only writes the JSONL file
    // after the first turn completes, so `codex resume` would fail on a fresh
    // zero-turn thread without this bootstrap.
    if let Some(ref tp) = thread_path {
        let rollout = Path::new(tp);
        if !rollout.exists() {
            if let Some(parent) = rollout.parent() {
                let _ = fs::create_dir_all(parent);
            }
            let meta = json!({
                "timestamp": Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "timestamp": Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Millis, true),
                    "cwd": config.cwd.display().to_string(),
                    "originator": "longhouse_codex_bridge",
                }
            });
            if let Err(e) = fs::write(rollout, format!("{}\n", meta)) {
                eprintln!("[codex-bridge] warning: could not seed rollout file {}: {e}", tp);
            }
        }
    }

    let mut context = BridgeContext {
        state_file: config.state_file.clone(),
        state: BridgeStateFile {
            session_id: config.session_id.clone(),
            cwd: config.cwd.display().to_string(),
            codex_bin: config.codex_bin.clone(),
            ws_url: Some(ws_url.clone()),
            thread_id: Some(thread_id.clone()),
            thread_path: thread_path.clone(),
            pid,
            status: "ready".to_string(),
            log_file: config.log_file.display().to_string(),
            active_turn_id: None,
            last_turn_status: None,
            last_error: None,
            updated_at: Utc::now().to_rfc3339(),
        },
        runtime: BridgeRuntimeSink {
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(5))
                .build()
                .context("building bridge runtime HTTP client")?,
            api_url: config.api_url.clone(),
            api_token: config.api_token.clone(),
            session_id: config.session_id.clone(),
            machine_name: config.machine_name.clone(),
            thread_id: Some(thread_id.clone()),
        },
        current_exe,
        last_progress_emit: None,
    };
    write_state_file(&context.state_file, &context.state)?;
    context
        .runtime
        .post_phase(
            "idle",
            format!("bridge:launch:{}", context.state.session_id),
            None,
        )
        .await;

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
                        process_notification(&value, &config, &mut context).await?;
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
            Some(cmd) = ipc_rx.recv() => {
                let result = handle_ipc_turn_start(&mut client, &context, &cmd).await;
                let _ = cmd.reply.send(result);
            }
        }
    }
}

async fn handle_ipc_turn_start(
    client: &mut RpcClient,
    context: &BridgeContext,
    cmd: &IpcCommand,
) -> Result<BridgeSendSummary> {
    let response = send_request(
        client,
        "turn/start",
        json!({
            "threadId": cmd.thread_id,
            "input": [{"type": "text", "text": cmd.text}],
        }),
    )
    .await?;
    let turn_id = extract_string(&response, &["turn", "id"])
        .context("missing turn.id in IPC turn/start response")?;
    let turn_status =
        extract_string(&response, &["turn", "status"]).unwrap_or_else(|| "inProgress".to_string());
    Ok(BridgeSendSummary {
        session_id: context.state.session_id.clone(),
        thread_id: cmd.thread_id.clone(),
        turn_id,
        turn_status,
    })
}

pub async fn cmd_codex_bridge_send(config: BridgeSendConfig) -> Result<BridgeSendSummary> {
    if config.text.trim().is_empty() {
        bail!("text must not be empty");
    }
    let state = load_ready_state(&config.session_id, config.state_root.as_deref())?;
    let thread_id = state
        .thread_id
        .clone()
        .context("bridge state is missing thread_id")?;

    // Try daemon IPC socket first — routes through the persistent connection
    // which preserves full conversation context.
    let paths = resolve_bridge_paths(config.state_root.as_deref(), &config.session_id, None)?;
    let sock_path = ipc_socket_path(&paths.state_file);
    #[cfg(unix)]
    if sock_path.exists() {
        match send_via_ipc(&sock_path, &config.text, &thread_id).await {
            Ok(summary) => return Ok(summary),
            Err(e) => {
                eprintln!("[codex-bridge] IPC send failed, falling back to direct WebSocket: {e}");
            }
        }
    }

    // Fallback: direct WebSocket (loses conversation context but still works)
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
#[cfg(unix)]
async fn send_via_ipc(
    sock_path: &Path,
    text: &str,
    thread_id: &str,
) -> Result<BridgeSendSummary> {
    let mut stream = tokio::net::UnixStream::connect(sock_path)
        .await
        .with_context(|| format!("connecting to IPC socket {}", sock_path.display()))?;

    let mut request = serde_json::to_vec(&json!({
        "text": text,
        "thread_id": thread_id,
    }))?;
    request.push(b'\n');
    stream.write_all(&request).await?;
    stream.shutdown().await?;

    let mut response_buf = Vec::new();
    stream.read_to_end(&mut response_buf).await?;
    let response: Value =
        serde_json::from_slice(&response_buf).context("parsing IPC response")?;

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
        .arg("resume")
        .arg(&thread_id)
        .arg("--enable")
        .arg("tui_app_server")
        .arg("--remote")
        .arg(&ws_url)
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

fn read_log_tail(path: &Path, max_chars: usize) -> String {
    let text = fs::read_to_string(path).unwrap_or_default();
    if text.len() <= max_chars {
        return text;
    }
    text[text.len() - max_chars..].to_string()
}

async fn spawn_app_server_client(config: &BridgeRunConfig) -> Result<RpcClient> {
    let mut command = Command::new(&config.codex_bin);
    command
        .arg("app-server")
        .arg("--listen")
        .arg("ws://127.0.0.1:0")
        .arg("--enable")
        .arg("codex_hooks")
        .arg("--enable")
        .arg("exec_permission_approvals")
        .arg("--enable")
        .arg("request_permissions_tool");
    if let Some(src) = config.session_source.as_deref() {
        command.arg("--session-source").arg(src);
    }
    command
        .env("LONGHOUSE_SESSION_ID", &config.session_id)
        .env("LONGHOUSE_HOOK_URL", &config.api_url)
        .env("LONGHOUSE_HOOK_TOKEN", &config.api_token)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{}` app-server", config.codex_bin))?;
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

    let ws_url = tokio::time::timeout(Duration::from_secs(10), ws_listen_rx)
        .await
        .context("timed out waiting for app-server websocket listener")?
        .context("app-server websocket listener never announced a URL")?;
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
            }
        }),
    )
    .await?;
    send_notification(client, "initialized", json!({})).await
}

async fn start_managed_thread(client: &mut RpcClient, config: &BridgeRunConfig) -> Result<Value> {
    initialize_client(client).await?;
    let mut params = Map::new();
    params.insert(
        "cwd".to_string(),
        Value::String(config.cwd.display().to_string()),
    );
    if let Some(policy) = config.approval_policy.as_ref() {
        params.insert("approvalPolicy".to_string(), Value::String(policy.clone()));
    }
    if let Some(sandbox) = config.sandbox.as_ref() {
        params.insert("sandbox".to_string(), Value::String(sandbox.clone()));
    }
    if let Some(model) = config.model.as_ref() {
        params.insert("model".to_string(), Value::String(model.clone()));
    }
    send_request(client, "thread/start", Value::Object(params)).await
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

    match method {
        "item/commandExecution/requestApproval"
        | "item/fileChange/requestApproval"
        | "item/permissions/requestApproval" => {
            context
                .runtime
                .post_phase(
                    "blocked",
                    format!(
                        "bridge:blocked:{}:{}",
                        context.state.session_id,
                        Uuid::new_v4()
                    ),
                    Some("approval".to_string()),
                )
                .await;
        }
        "item/tool/requestUserInput" | "mcpServer/elicitation/request" => {
            context
                .runtime
                .post_phase(
                    "needs_user",
                    format!(
                        "bridge:needs-user:{}:{}",
                        context.state.session_id,
                        Uuid::new_v4()
                    ),
                    None,
                )
                .await;
        }
        _ => {}
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
) -> Result<()> {
    let Some(method) = value.get("method").and_then(Value::as_str) else {
        return Ok(());
    };
    let params = value.get("params").cloned().unwrap_or(Value::Null);
    match method {
        "turn/started" => {
            context.state.active_turn_id = extract_string(&params, &["turn", "id"]);
            context.state.last_turn_status = extract_string(&params, &["turn", "status"]);
            write_state_file(&context.state_file, &context.state)?;
            context
                .runtime
                .post_phase(
                    "thinking",
                    format!(
                        "bridge:thinking:{}:{}",
                        context.state.session_id,
                        context
                            .state
                            .active_turn_id
                            .clone()
                            .unwrap_or_else(|| Uuid::new_v4().to_string())
                    ),
                    None,
                )
                .await;
        }
        "item/agentMessage/delta" => {
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
        "turn/completed" => {
            context.state.last_turn_status = extract_string(&params, &["turn", "status"]);
            context.state.active_turn_id = None;
            write_state_file(&context.state_file, &context.state)?;
            context
                .runtime
                .post_phase(
                    "idle",
                    format!(
                        "bridge:idle:{}:{}",
                        context.state.session_id,
                        Uuid::new_v4()
                    ),
                    None,
                )
                .await;
            if let Some(thread_path) = context.state.thread_path.clone() {
                let session_id = context.state.session_id.clone();
                let api_url = config.api_url.clone();
                let api_token = config.api_token.clone();
                let current_exe = context.current_exe.clone();
                tokio::spawn(async move {
                    tokio::time::sleep(Duration::from_millis(DEFAULT_SHIP_DELAY_MS)).await;
                    if let Err(err) = ship_thread_file(
                        &current_exe,
                        &thread_path,
                        &session_id,
                        &api_url,
                        &api_token,
                    )
                    .await
                    {
                        eprintln!("[codex-bridge] per-turn ship failed: {err}");
                    }
                });
            }
        }
        "thread/status/changed" => {
            if let Some(status_type) = extract_string(&params, &["thread", "status", "type"]) {
                if status_type == "idle" {
                    context
                        .runtime
                        .post_phase(
                            "idle",
                            format!(
                                "bridge:thread-idle:{}:{}",
                                context.state.session_id,
                                Uuid::new_v4()
                            ),
                            None,
                        )
                        .await;
                }
            }
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
    Ok(())
}

async fn ship_thread_file(
    engine_exe: &Path,
    thread_path: &str,
    session_id: &str,
    api_url: &str,
    api_token: &str,
) -> Result<()> {
    let output = Command::new(engine_exe)
        .arg("ship")
        .arg("--file")
        .arg(thread_path)
        .arg("--provider")
        .arg("codex")
        .arg("--url")
        .arg(api_url)
        .arg("--token")
        .arg(api_token)
        .arg("--session-id")
        .arg(session_id)
        .arg("--require-reply-evidence")
        .arg("--json")
        .output()
        .await
        .with_context(|| format!("spawning per-turn ship for {}", thread_path))?;
    if !output.status.success() {
        bail!(
            "ship exited with {} stderr={}",
            output.status,
            String::from_utf8_lossy(&output.stderr)
        );
    }
    Ok(())
}

impl BridgeRuntimeSink {
    async fn post_phase(
        &self,
        phase: &str,
        dedupe_key: String,
        tool_name: Option<String>,
    ) {
        let freshness_ms = match phase {
            "thinking" => Some(90_000),
            "running" => Some(600_000),
            "idle" => Some(600_000),
            "blocked" => Some(86_400_000),
            "needs_user" => Some(86_400_000),
            _ => Some(600_000),
        };
        self.post_runtime_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": BRIDGE_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": Utc::now().to_rfc3339(),
            "freshness_ms": freshness_ms,
            "dedupe_key": dedupe_key,
            "payload": {
                "managed_transport": "codex_app_server",
                "thread_id": self.thread_id,
            }
        })])
        .await;
    }

    async fn post_progress(&self, dedupe_key: String) {
        self.post_runtime_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": BRIDGE_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": Utc::now().to_rfc3339(),
            "freshness_ms": 90_000,
            "dedupe_key": dedupe_key,
            "payload": {
                "managed_transport": "codex_app_server",
                "thread_id": self.thread_id,
            }
        })])
        .await;
    }

    async fn post_runtime_events(&self, events: Vec<Value>) {
        let response = match self
            .http
            .post(format!(
                "{}/api/agents/runtime/events/batch",
                self.api_url.trim_end_matches('/')
            ))
            .header("X-Agents-Token", &self.api_token)
            .json(&json!({ "events": events }))
            .send()
            .await
        {
            Ok(r) => r,
            Err(e) => {
                eprintln!("[codex-bridge] runtime ingest network error: {e}");
                return;
            }
        };
        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            eprintln!("[codex-bridge] runtime ingest failed: {status} {body}");
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

async fn shutdown_child(client: &mut RpcClient) -> Result<()> {
    if let Some(ref mut child) = client.child {
        if child.try_wait()?.is_none() {
            let _ = child.start_kill();
        }
        let _ = child.wait().await;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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
    fn should_emit_progress_respects_throttle() {
        assert!(should_emit_progress(None, 1000));
        assert!(!should_emit_progress(Some(Instant::now()), 10_000));
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

    #[test]
    fn ipc_socket_path_is_sibling_of_state_file() {
        let state = Path::new("/tmp/codex-bridge/session-42.json");
        let sock = ipc_socket_path(state);
        assert_eq!(sock, Path::new("/tmp/codex-bridge/session-42.sock"));
    }
}
