use std::collections::{BTreeMap, HashMap, VecDeque};
use std::ffi::OsString;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::OnceLock;
use std::time::Duration;

#[cfg(unix)]
use std::io::Write as _;

use anyhow::{Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::io::{AsyncWriteExt, Lines};
use tokio::process::Command;
use tokio::process::{Child, ChildStdin, ChildStdout};
use tokio::sync::mpsc;
use walkdir::WalkDir;

const CODEX_DISABLE_UPDATE_CHECK_CONFIG: &str = "check_for_update_on_startup=false";
const CODEX_EXEC_RUNTIME_SOURCE: &str = "codex_app_server";
const STDERR_TAIL_LINES: usize = 40;
const APP_SERVER_TURN_TIMEOUT: Duration = Duration::from_secs(60 * 60);
const CONSOLE_WARM_POOL_TARGET: usize = 1;
const CONSOLE_WARM_WORKER_TTL: Duration = Duration::from_secs(120);
const DEFAULT_CODEX_BIN: &str = "codex";
const DEFAULT_CONSOLE_APPROVAL_POLICY: &str = "never";
const DEFAULT_CONSOLE_SANDBOX: &str = "workspace-write";

struct AppServerRpc {
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
    next_id: u64,
    seq: u64,
}

struct InitializedCodexWorker {
    child: Child,
    rpc: AppServerRpc,
    stderr_tail: Arc<Mutex<VecDeque<String>>>,
    stderr_task: Option<tokio::task::JoinHandle<()>>,
    pid: Option<u32>,
    pgid: Option<i32>,
    argv: Vec<String>,
    ready_at: std::time::Instant,
}

struct CodexConsoleWorkerPool {
    workers: Vec<InitializedCodexWorker>,
    spawning: usize,
    active_process_groups: HashMap<u32, i32>,
    shutting_down: bool,
    spawn_finished: Arc<tokio::sync::Notify>,
    active_finished: Arc<tokio::sync::Notify>,
}

impl Default for CodexConsoleWorkerPool {
    fn default() -> Self {
        Self {
            workers: Vec::new(),
            spawning: 0,
            active_process_groups: HashMap::new(),
            shutting_down: false,
            spawn_finished: Arc::new(tokio::sync::Notify::new()),
            active_finished: Arc::new(tokio::sync::Notify::new()),
        }
    }
}

impl CodexConsoleWorkerPool {
    fn reserve_spawn_slot(&mut self) -> bool {
        if self.shutting_down
            || self.workers.len() + self.spawning >= CONSOLE_WARM_POOL_TARGET
        {
            return false;
        }
        self.spawning += 1;
        true
    }
}

static CODEX_CONSOLE_WORKER_POOL: OnceLock<tokio::sync::Mutex<CodexConsoleWorkerPool>> =
    OnceLock::new();

fn console_worker_pool() -> &'static tokio::sync::Mutex<CodexConsoleWorkerPool> {
    CODEX_CONSOLE_WORKER_POOL.get_or_init(Default::default)
}

#[derive(Default)]
struct AppServerProjection {
    item_text: BTreeMap<String, String>,
    item_seq: BTreeMap<String, u64>,
    tool_command: BTreeMap<String, String>,
    tool_output: BTreeMap<String, String>,
    tool_seq: BTreeMap<String, u64>,
    transcript_seq: u64,
}

#[derive(Debug, PartialEq, Eq)]
enum ProjectedAppServerEvent {
    Phase {
        phase: &'static str,
        tool_name: Option<String>,
    },
    AssistantItem {
        item_id: String,
        item_seq: u64,
        seq: u64,
        delta: String,
        text: String,
        completed: bool,
    },
    ToolItem {
        item_id: String,
        command: String,
        output: String,
        status: String,
        seq: u64,
        completed: bool,
    },
}

impl AppServerProjection {
    fn apply(&mut self, event: &Value) -> Vec<ProjectedAppServerEvent> {
        let method = event.get("method").and_then(Value::as_str).unwrap_or("");
        let params = event.get("params").unwrap_or(&Value::Null);
        match method {
            "item/agentMessage/delta" => {
                let Some(item_id) = params.get("itemId").and_then(Value::as_str) else {
                    return Vec::new();
                };
                let Some(delta) = params.get("delta").and_then(Value::as_str) else {
                    return Vec::new();
                };
                let text = self.item_text.entry(item_id.to_string()).or_default();
                text.push_str(delta);
                let text = text.clone();
                let item_seq = self.item_seq.entry(item_id.to_string()).or_default();
                *item_seq += 1;
                self.transcript_seq += 1;
                vec![ProjectedAppServerEvent::AssistantItem {
                    item_id: item_id.to_string(),
                    item_seq: *item_seq,
                    seq: self.transcript_seq,
                    delta: delta.to_string(),
                    text,
                    completed: false,
                }]
            }
            "item/started"
                if json_string(params, &["item", "type"]).as_deref()
                    == Some("commandExecution") =>
            {
                let item_id = json_string(params, &["item", "id"])
                    .unwrap_or_else(|| "unknown-tool".to_string());
                let command = json_string(params, &["item", "command"]).unwrap_or_default();
                self.tool_command.insert(item_id.clone(), command.clone());
                self.tool_seq.insert(item_id.clone(), 1);
                vec![
                    ProjectedAppServerEvent::Phase {
                        phase: "tool",
                        tool_name: Some(command.clone()),
                    },
                    ProjectedAppServerEvent::ToolItem {
                        item_id,
                        command,
                        output: String::new(),
                        status: "inProgress".to_string(),
                        seq: 1,
                        completed: false,
                    },
                ]
            }
            "item/commandExecution/outputDelta" => {
                let Some(item_id) = params.get("itemId").and_then(Value::as_str) else {
                    return Vec::new();
                };
                let delta = params.get("delta").and_then(Value::as_str).unwrap_or("");
                let output = self.tool_output.entry(item_id.to_string()).or_default();
                output.push_str(delta);
                let seq = self.tool_seq.entry(item_id.to_string()).or_default();
                *seq += 1;
                vec![ProjectedAppServerEvent::ToolItem {
                    item_id: item_id.to_string(),
                    command: self.tool_command.get(item_id).cloned().unwrap_or_default(),
                    output: output.clone(),
                    status: "inProgress".to_string(),
                    seq: *seq,
                    completed: false,
                }]
            }
            "item/completed"
                if json_string(params, &["item", "type"]).as_deref()
                    == Some("commandExecution") =>
            {
                let item_id = json_string(params, &["item", "id"])
                    .unwrap_or_else(|| "unknown-tool".to_string());
                let command = json_string(params, &["item", "command"])
                    .or_else(|| self.tool_command.get(&item_id).cloned())
                    .unwrap_or_default();
                let output = json_string(params, &["item", "aggregatedOutput"])
                    .or_else(|| self.tool_output.get(&item_id).cloned())
                    .unwrap_or_default();
                let status = json_string(params, &["item", "status"])
                    .unwrap_or_else(|| "completed".to_string());
                let seq = self.tool_seq.entry(item_id.clone()).or_default();
                *seq += 1;
                vec![ProjectedAppServerEvent::ToolItem {
                    item_id,
                    command,
                    output,
                    status,
                    seq: *seq,
                    completed: true,
                }]
            }
            "item/completed"
                if matches!(
                    json_string(params, &["item", "type"]).as_deref(),
                    Some("agentMessage" | "assistantMessage")
                ) =>
            {
                let Some(item_id) = json_string(params, &["item", "id"]).or_else(|| {
                    params
                        .get("itemId")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                }) else {
                    return Vec::new();
                };
                let text = self
                    .item_text
                    .get(&item_id)
                    .cloned()
                    .or_else(|| json_string(params, &["item", "text"]))
                    .or_else(|| {
                        params
                            .get("item")?
                            .get("content")?
                            .as_array()?
                            .iter()
                            .find_map(|part| part.get("text").and_then(Value::as_str))
                            .map(str::to_string)
                    });
                let Some(text) = text else { return Vec::new() };
                let item_seq = self.item_seq.entry(item_id.clone()).or_default();
                *item_seq += 1;
                self.transcript_seq += 1;
                vec![ProjectedAppServerEvent::AssistantItem {
                    item_id,
                    item_seq: *item_seq,
                    seq: self.transcript_seq,
                    delta: String::new(),
                    text,
                    completed: true,
                }]
            }
            "turn/started" => vec![ProjectedAppServerEvent::Phase {
                phase: "thinking",
                tool_name: None,
            }],
            _ => Vec::new(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct CodexExecRunConfig {
    pub session_id: String,
    pub run_id: String,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub codex_bin: String,
    pub approval_policy: Option<String>,
    pub sandbox: Option<String>,
    pub prompt: String,
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
    pub resume_thread_id: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CodexExecRunSummary {
    pub session_id: String,
    pub run_id: String,
    pub pid: Option<u32>,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct CodexExecRuntimeSink {
    session_id: String,
    run_id: String,
    thread_id: Option<String>,
    turn_id: Option<String>,
    client_request_id: Option<String>,
    machine_name: String,
    cwd: String,
    local_db_path: Option<PathBuf>,
    event_tx: mpsc::Sender<Vec<Value>>,
    critical_event_tx: mpsc::Sender<Vec<Value>>,
    queued_events: Arc<AtomicUsize>,
}

pub fn codex_exec_args(config: &CodexExecRunConfig) -> Vec<OsString> {
    let mut args = vec![
        OsString::from("-c"),
        OsString::from(CODEX_DISABLE_UPDATE_CHECK_CONFIG),
    ];
    if let Some(approval_policy) = normalized_optional(&config.approval_policy) {
        args.push(OsString::from("-c"));
        args.push(OsString::from(format!(
            "approval_policy={}",
            toml_quote_string(&approval_policy)
        )));
    }
    if let Some(sandbox) = normalized_optional(&config.sandbox) {
        args.push(OsString::from("-s"));
        args.push(OsString::from(sandbox));
    }
    args.push(OsString::from("app-server"));
    args.push(OsString::from("--listen"));
    args.push(OsString::from("stdio://"));
    args
}

fn toml_quote_string(value: &str) -> String {
    let escaped = value.replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{escaped}\"")
}

fn warm_pool_compatible(config: &CodexExecRunConfig) -> bool {
    config.codex_bin == DEFAULT_CODEX_BIN
        && normalized_optional(&config.approval_policy).as_deref()
            == Some(DEFAULT_CONSOLE_APPROVAL_POLICY)
        && normalized_optional(&config.sandbox).as_deref() == Some(DEFAULT_CONSOLE_SANDBOX)
}

pub async fn prewarm_codex_console_workers() {
    {
        let mut pool = console_worker_pool().lock().await;
        if !pool.reserve_spawn_slot() {
            return;
        }
    }
    let neutral_cwd = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir);
    let result = spawn_initialized_codex_worker(
        DEFAULT_CODEX_BIN,
        Some(DEFAULT_CONSOLE_APPROVAL_POLICY),
        Some(DEFAULT_CONSOLE_SANDBOX),
        &neutral_cwd,
        None,
        None,
        None,
    )
    .await;
    let mut pool = console_worker_pool().lock().await;
    pool.spawning = pool.spawning.saturating_sub(1);
    let spawn_finished = pool.spawn_finished.clone();
    let mut discard = None;
    match result {
        Ok(worker)
            if !pool.shutting_down && pool.workers.len() < CONSOLE_WARM_POOL_TARGET =>
        {
            let worker_pid = worker.pid;
            eprintln!(
                "[codex-exec] latency stage=warm_worker_ready pid={} pool_size={}",
                worker.pid.unwrap_or(0),
                pool.workers.len() + 1
            );
            pool.workers.push(worker);
            if let Some(worker_pid) = worker_pid {
                tokio::spawn(reap_warm_worker_after_ttl(worker_pid));
            }
        }
        Ok(worker) => discard = Some(worker),
        Err(err) => eprintln!(
            "[codex-exec] latency stage=warm_worker_miss reason=prewarm_failed error={err}"
        ),
    }
    drop(pool);
    if let Some(mut worker) = discard {
        eprintln!(
            "[codex-exec] latency stage=warm_worker_reaped pid={} reason=surplus",
            worker.pid.unwrap_or(0)
        );
        let _ = shutdown_worker_process_group(&mut worker.child, worker.pgid).await;
    }
    spawn_finished.notify_waiters();
}

async fn reap_warm_worker_after_ttl(pid: u32) {
    tokio::time::sleep(CONSOLE_WARM_WORKER_TTL).await;
    let mut pool = console_worker_pool().lock().await;
    let Some(index) = pool
        .workers
        .iter()
        .position(|worker| worker.pid == Some(pid) && worker.ready_at.elapsed() >= CONSOLE_WARM_WORKER_TTL)
    else {
        return;
    };
    let mut worker = pool.workers.remove(index);
    let pool_size = pool.workers.len();
    drop(pool);
    eprintln!(
        "[codex-exec] latency stage=warm_worker_reaped pid={} reason=ttl pool_size={}",
        worker.pid.unwrap_or(0),
        pool_size
    );
    let _ = shutdown_worker_process_group(&mut worker.child, worker.pgid).await;
}

async fn lease_warm_worker() -> Option<InitializedCodexWorker> {
    let mut pool = console_worker_pool().lock().await;
    if pool.shutting_down {
        return None;
    }
    while let Some(mut worker) = pool.workers.pop() {
        if worker.ready_at.elapsed() < CONSOLE_WARM_WORKER_TTL
            && worker.child.try_wait().ok().flatten().is_none()
        {
            if let (Some(pid), Some(pgid)) = (worker.pid, worker.pgid) {
                pool.active_process_groups.insert(pid, pgid);
            }
            return Some(worker);
        }
        eprintln!(
            "[codex-exec] latency stage=warm_worker_miss reason=worker_exited pid={}",
            worker.pid.unwrap_or(0)
        );
    }
    None
}

async fn register_active_worker(worker: &InitializedCodexWorker) -> bool {
    let mut pool = console_worker_pool().lock().await;
    if pool.shutting_down {
        return false;
    }
    if let (Some(pid), Some(pgid)) = (worker.pid, worker.pgid) {
        pool.active_process_groups.insert(pid, pgid);
    }
    true
}

async fn unregister_active_worker(pid: Option<u32>) {
    let Some(pid) = pid else { return };
    let mut pool = console_worker_pool().lock().await;
    pool.active_process_groups.remove(&pid);
    pool.active_finished.notify_waiters();
}

#[allow(clippy::too_many_arguments)]
async fn spawn_initialized_codex_worker(
    codex_bin: &str,
    approval_policy: Option<&str>,
    sandbox: Option<&str>,
    process_cwd: &std::path::Path,
    session_id: Option<&str>,
    launch_actor: Option<&str>,
    launch_surface: Option<&str>,
) -> Result<InitializedCodexWorker> {
    let config = CodexExecRunConfig {
        session_id: session_id.unwrap_or("warm-anonymous").to_string(),
        run_id: "warm-anonymous".to_string(),
        thread_id: None,
        turn_id: None,
        client_request_id: None,
        cwd: process_cwd.to_path_buf(),
        api_url: String::new(),
        api_token: String::new(),
        codex_bin: codex_bin.to_string(),
        approval_policy: approval_policy.map(str::to_string),
        sandbox: sandbox.map(str::to_string),
        prompt: String::new(),
        launch_actor: launch_actor.map(str::to_string),
        launch_surface: launch_surface.map(str::to_string),
        resume_thread_id: None,
        machine_name: String::new(),
        local_db_path: None,
    };
    let args = codex_exec_args(&config);
    let argv = std::iter::once(OsString::from(codex_bin))
        .chain(args.iter().cloned())
        .map(|item| item.to_string_lossy().to_string())
        .collect::<Vec<_>>();
    let mut command = Command::new(codex_bin);
    command
        .args(&args)
        .current_dir(process_cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    if let Some(session_id) = session_id {
        command.env("LONGHOUSE_MANAGED_SESSION_ID", session_id);
    } else {
        command.env("LONGHOUSE_CONSOLE_WORKER", "1");
    }
    if let Some(actor) = launch_actor {
        command.env("LONGHOUSE_LAUNCH_ACTOR", actor);
    }
    if let Some(surface) = launch_surface {
        command.env("LONGHOUSE_LAUNCH_SURFACE", surface);
    }
    #[cfg(unix)]
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{codex_bin}` app-server worker"))?;
    let pid = child.id();
    let pgid = pid.and_then(|value| i32::try_from(value).ok());
    let stdin = child.stdin.take().context("Codex worker stdin unavailable")?;
    let stdout = child.stdout.take().context("Codex worker stdout unavailable")?;
    let stderr = child.stderr.take();
    let stderr_tail = Arc::new(Mutex::new(VecDeque::with_capacity(STDERR_TAIL_LINES)));
    let stderr_task = stderr.map(|stream| {
        let tail = stderr_tail.clone();
        tokio::spawn(async move { read_stderr_tail(stream, tail).await })
    });
    let mut rpc = AppServerRpc {
        stdin,
        lines: BufReader::new(stdout).lines(),
        next_id: 2,
        seq: 0,
    };
    let initialize_result = tokio::time::timeout(Duration::from_secs(5), async {
        rpc.write(&json!({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "longhouse_console",
                    "title": "Longhouse Console",
                    "version": env!("CARGO_PKG_VERSION"),
                },
                "capabilities": { "experimentalApi": true },
            }
        }))
        .await?;
        loop {
            let value = rpc.next_value().await?;
            if value.get("id").and_then(Value::as_u64) == Some(1) {
                if let Some(error) = value.get("error") {
                    anyhow::bail!("Codex worker initialize failed: {error}");
                }
                break;
            }
            if value.get("id").is_some() && value.get("method").is_some() {
                rpc.respond_to_server_request(&value).await?;
            }
        }
        rpc.notify("initialized", json!({})).await?;
        Ok::<(), anyhow::Error>(())
    })
    .await;
    if let Err(error) = initialize_result
        .map_err(|_| anyhow::anyhow!("Codex worker initialize timed out"))
        .and_then(|result| result)
    {
        let _ = shutdown_worker_process_group(&mut child, pgid).await;
        if let Some(task) = stderr_task {
            let _ = task.await;
        }
        return Err(error);
    }
    Ok(InitializedCodexWorker {
        child,
        rpc,
        stderr_tail,
        stderr_task,
        pid,
        pgid,
        argv,
        ready_at: std::time::Instant::now(),
    })
}

pub async fn start_codex_exec_once(config: CodexExecRunConfig) -> Result<CodexExecRunSummary> {
    let warm_compatible = warm_pool_compatible(&config);
    let warm_worker = if warm_compatible {
        lease_warm_worker().await
    } else {
        None
    };
    let warm_hit = warm_worker.is_some();
    let mut worker = match warm_worker {
        Some(worker) => worker,
        None => {
            spawn_initialized_codex_worker(
                &config.codex_bin,
                normalized_optional(&config.approval_policy).as_deref(),
                normalized_optional(&config.sandbox).as_deref(),
                &config.cwd,
                Some(&config.session_id),
                normalized_optional(&config.launch_actor).as_deref(),
                normalized_optional(&config.launch_surface).as_deref(),
            )
            .await?
        }
    };
    if !warm_hit && !register_active_worker(&worker).await {
        shutdown_worker_process_group(&mut worker.child, worker.pgid).await?;
        anyhow::bail!("Codex Console worker rejected because the Machine Agent is shutting down");
    }
    let pid = worker.pid;
    let argv = worker.argv.clone();
    let leased_at = std::time::Instant::now();
    eprintln!(
        "[codex-exec] latency stage=warm_worker_lease session={} run={} hit={} pid={} ready_age_ms={}",
        config.session_id,
        config.run_id,
        warm_hit,
        pid.unwrap_or(0),
        worker.ready_at.elapsed().as_millis()
    );
    if warm_compatible {
        tokio::spawn(prewarm_codex_console_workers());
    }
    let (event_tx, event_rx) = mpsc::channel(EVENT_PUMP_QUEUE_CAPACITY);
    let (critical_event_tx, critical_event_rx) = mpsc::channel(EVENT_PUMP_CRITICAL_CAPACITY);
    let queued_events = Arc::new(AtomicUsize::new(0));
    let sink = CodexExecRuntimeSink {
        session_id: config.session_id.clone(),
        run_id: config.run_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        client_request_id: config.client_request_id.clone(),
        machine_name: config.machine_name.clone(),
        cwd: config.cwd.to_string_lossy().to_string(),
        local_db_path: config.local_db_path.clone(),
        event_tx,
        critical_event_tx,
        queued_events: queued_events.clone(),
    };
    let event_pump = tokio::spawn(run_runtime_event_pump(
        config.api_url.clone(),
        config.api_token.clone(),
        config.session_id.clone(),
        config.run_id.clone(),
        event_rx,
        critical_event_rx,
        queued_events,
    ));
    let monitor_sink = sink.clone();
    let stderr_tail = worker.stderr_tail.clone();

    let prompt = config.prompt.clone();
    let cwd = config.cwd.clone();
    let approval_policy = config.approval_policy.clone();
    let sandbox = config.sandbox.clone();
    let resume_thread_id = config.resume_thread_id.clone();
    tokio::spawn(async move {
        let mut run_result = run_app_server_turn(
            &mut worker.child,
            worker.rpc,
            &monitor_sink,
            &prompt,
            &cwd,
            approval_policy.as_deref(),
            sandbox.as_deref(),
            resume_thread_id.as_deref(),
            warm_hit,
            leased_at,
        )
        .await;
        if let Err(kill_error) =
            shutdown_worker_process_group(&mut worker.child, worker.pgid).await
        {
            run_result = match run_result {
                Err(original) => Err(original.context(format!(
                    "also failed to stop Codex app-server process group: {kill_error}"
                ))),
                Ok(value) => {
                    eprintln!(
                        "[codex-exec] worker process-group cleanup failed pid={} error={kill_error}",
                        worker.pid.unwrap_or(0)
                    );
                    Ok(value)
                }
            };
        }
        unregister_active_worker(worker.pid).await;
        if let Some(task) = worker.stderr_task {
            let _ = task.await;
        }
        let (terminal_state, exit_code, detail) = match run_result {
            Ok(exit_code) if exit_code == Some(0) => (
                "run_completed",
                exit_code,
                stderr_tail_snapshot(&stderr_tail),
            ),
            Ok(exit_code) => (
                "run_failed",
                exit_code,
                Some(format!("Codex app-server exited with code {exit_code:?}")),
            ),
            Err(err) => (
                "run_failed",
                worker
                    .child
                    .try_wait()
                    .ok()
                    .flatten()
                    .and_then(|s| s.code()),
                Some(err.to_string()),
            ),
        };
        monitor_sink
            .post_terminal(terminal_state, exit_code, detail)
            .await;
        let terminal_session_id = monitor_sink.session_id.clone();
        let terminal_run_id = monitor_sink.run_id.clone();
        drop(monitor_sink);
        let mut event_pump = event_pump;
        match tokio::time::timeout(Duration::from_secs(6), &mut event_pump).await {
            Ok(Ok(())) => {}
            Ok(Err(err)) => eprintln!("[codex-exec] runtime event pump join failed: {err}"),
            Err(_) => {
                event_pump.abort();
                eprintln!(
                    "[codex-exec] runtime event pump drain timed out session={terminal_session_id} run={terminal_run_id}"
                );
            }
        }
    });

    Ok(CodexExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid,
        argv,
    })
}

async fn run_app_server_turn(
    child: &mut Child,
    mut rpc: AppServerRpc,
    sink: &CodexExecRuntimeSink,
    prompt: &str,
    cwd: &std::path::Path,
    approval_policy: Option<&str>,
    sandbox: Option<&str>,
    resume_thread_id: Option<&str>,
    warm_hit: bool,
    leased_at: std::time::Instant,
) -> Result<Option<i32>> {
    let mut projection = AppServerProjection::default();

    let method = if resume_thread_id.is_some() {
        "thread/resume"
    } else {
        "thread/start"
    };
    let mut thread_params = json!({
        "cwd": cwd.to_string_lossy(),
        "approvalPolicy": approval_policy,
        "sandbox": sandbox,
    });
    if let Some(thread_id) = resume_thread_id {
        thread_params["threadId"] = Value::String(thread_id.to_string());
    }
    let thread_response = rpc
        .request(method, thread_params, sink, &mut projection)
        .await?;
    let provider_thread_id = json_string(&thread_response, &["thread", "id"])
        .context("Codex app-server thread response omitted thread.id")?;
    let thread_path = json_string(&thread_response, &["thread", "path"])
        .or_else(|| codex_rollout_path(&provider_thread_id).map(|path| path.display().to_string()));
    sink.post_provider_binding(&provider_thread_id, thread_path.as_deref())
        .await;

    sink.post_latency_stage(
        "turn_start_write",
        json!({
            "warm_hit": warm_hit,
            "lease_to_write_ms": leased_at.elapsed().as_millis(),
        }),
    )
    .await;
    let turn_write_started = std::time::Instant::now();
    let turn_response = rpc
        .request(
            "turn/start",
            json!({
                "threadId": provider_thread_id,
                "input": [{"type": "text", "text": prompt}],
            }),
            sink,
            &mut projection,
        )
        .await?;
    sink.post_latency_stage(
        "turn_start_ack",
        json!({
            "warm_hit": warm_hit,
            "write_to_ack_ms": turn_write_started.elapsed().as_millis(),
        }),
    )
    .await;
    let expected_turn_id = json_string(&turn_response, &["turn", "id"])
        .context("Codex app-server turn/start omitted turn.id")?;
    sink.post_phase("thinking", None).await;
    sink.post_live_user_item(prompt).await;

    tokio::time::timeout(APP_SERVER_TURN_TIMEOUT, async {
        loop {
            let value = rpc.next_value().await?;
            if value.get("id").is_some() && value.get("method").is_some() {
                rpc.respond_to_server_request(&value).await?;
                continue;
            }
            rpc.seq += 1;
            sink.post_app_server_event(rpc.seq, &value, &mut projection)
                .await;
            if value.get("method").and_then(Value::as_str) == Some("turn/completed") {
                let completed_turn_id = json_string(&value, &["params", "turn", "id"]);
                let completed_turn_id = completed_turn_id
                    .context("Codex turn/completed omitted params.turn.id")?;
                if completed_turn_id != expected_turn_id {
                    anyhow::bail!(
                        "Codex completed unexpected turn {completed_turn_id}; expected {expected_turn_id}"
                    );
                }
                let status = json_string(&value, &["params", "turn", "status"])
                    .unwrap_or_else(|| "completed".to_string());
                if status != "completed" {
                    anyhow::bail!("Codex turn ended with status {status}");
                }
                if let Some(path) = thread_path.as_deref() {
                    sink.wake_transcript_shipper(path, &completed_turn_id, "turn_completed")
                        .await;
                } else {
                    eprintln!(
                        "[codex-exec] latency stage=durable_wake_miss session={} run={} provider_turn={} reason=path_missing",
                        sink.session_id, sink.run_id, completed_turn_id
                    );
                }
                break;
            }
        }
        Ok::<(), anyhow::Error>(())
    })
    .await
    .context("Codex app-server turn timed out")??;

    rpc.stdin.shutdown().await?;
    drop(rpc);
    let status = match tokio::time::timeout(Duration::from_secs(5), child.wait()).await {
        Ok(status) => status?,
        Err(_) => {
            child.kill().await?;
            let status = child.wait().await?;
            anyhow::bail!(
                "Codex app-server did not exit after turn completion; killed with status {status}"
            );
        }
    };
    Ok(status.code())
}

async fn shutdown_worker_process_group(child: &mut Child, pgid: Option<i32>) -> Result<()> {
    if let Some(pgid) = pgid {
        shutdown_process_group(pgid).await;
    }
    if child.try_wait()?.is_none() {
        child.start_kill()?;
    }
    let _ = child.wait().await;
    Ok(())
}

async fn shutdown_process_group(pgid: i32) {
    #[cfg(unix)]
    {
        unsafe {
            libc::killpg(pgid, libc::SIGTERM);
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        if unsafe { libc::killpg(pgid, 0) } == 0 {
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
        }
    }
    #[cfg(not(unix))]
    let _ = pgid;
}

pub async fn shutdown_codex_console_worker_pool() {
    loop {
        let wait = {
            let mut pool = console_worker_pool().lock().await;
            pool.shutting_down = true;
            if pool.spawning == 0 {
                None
            } else {
                Some(pool.spawn_finished.clone().notified_owned())
            }
        };
        let Some(wait) = wait else { break };
        wait.await;
    }
    let (workers, active_process_groups) = {
        let mut pool = console_worker_pool().lock().await;
        (
            std::mem::take(&mut pool.workers),
            pool.active_process_groups.values().copied().collect::<Vec<_>>(),
        )
    };
    for pgid in active_process_groups {
        shutdown_process_group(pgid).await;
    }
    for mut worker in workers {
        if let Err(err) = shutdown_worker_process_group(&mut worker.child, worker.pgid).await {
            eprintln!(
                "[codex-exec] warm worker shutdown failed pid={} error={err}",
                worker.pid.unwrap_or(0)
            );
        }
    }
    loop {
        let wait = {
            let pool = console_worker_pool().lock().await;
            if pool.active_process_groups.is_empty() {
                None
            } else {
                Some(pool.active_finished.clone().notified_owned())
            }
        };
        let Some(wait) = wait else { break };
        wait.await;
    }
}

impl AppServerRpc {
    async fn write(&mut self, value: &Value) -> Result<()> {
        self.stdin
            .write_all(format!("{}\n", serde_json::to_string(value)?).as_bytes())
            .await?;
        self.stdin.flush().await?;
        Ok(())
    }

    async fn notify(&mut self, method: &str, params: Value) -> Result<()> {
        self.write(&json!({"method": method, "params": params}))
            .await
    }

    async fn request(
        &mut self,
        method: &str,
        params: Value,
        sink: &CodexExecRuntimeSink,
        projection: &mut AppServerProjection,
    ) -> Result<Value> {
        let id = self.next_id;
        self.next_id += 1;
        self.write(&json!({"id": id, "method": method, "params": params}))
            .await?;
        loop {
            let value = self.next_value().await?;
            if value.get("id").and_then(Value::as_u64) == Some(id) && value.get("method").is_none()
            {
                if let Some(error) = value.get("error") {
                    anyhow::bail!("{method} failed: {error}");
                }
                return Ok(value.get("result").cloned().unwrap_or(Value::Null));
            }
            if value.get("id").is_some() && value.get("method").is_some() {
                self.respond_to_server_request(&value).await?;
            } else {
                self.seq += 1;
                sink.post_app_server_event(self.seq, &value, projection)
                    .await;
            }
        }
    }

    async fn next_value(&mut self) -> Result<Value> {
        loop {
            let line = self
                .lines
                .next_line()
                .await?
                .context("Codex app-server closed stdout")?;
            if !line.trim().is_empty() {
                return serde_json::from_str(&line)
                    .with_context(|| format!("invalid Codex app-server JSON: {line}"));
            }
        }
    }

    async fn respond_to_server_request(&mut self, request: &Value) -> Result<()> {
        let id = request.get("id").cloned().unwrap_or(Value::Null);
        let method = request.get("method").and_then(Value::as_str).unwrap_or("");
        let result = match method {
            "item/commandExecution/requestApproval" | "item/fileChange/requestApproval" => {
                json!({"decision": "decline"})
            }
            "item/permissions/requestApproval" => json!({"scope": "turn", "permissions": {}}),
            "item/tool/requestUserInput" => json!({"answers": {}}),
            "mcpServer/elicitation/request" => json!({"action": "decline", "content": null}),
            "applyPatchApproval" | "execCommandApproval" => json!({"decision": "Denied"}),
            _ => anyhow::bail!("unsupported Codex app-server request: {method}"),
        };
        self.write(&json!({"id": id, "result": result})).await
    }
}

fn json_string(value: &Value, path: &[&str]) -> Option<String> {
    let mut current = value;
    for key in path {
        current = current.get(*key)?;
    }
    current.as_str().map(str::to_string)
}

async fn read_stderr_tail(stream: tokio::process::ChildStderr, tail: Arc<Mutex<VecDeque<String>>>) {
    let mut lines = BufReader::new(stream).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let mut guard = tail.lock().expect("codex exec stderr tail lock poisoned");
        if guard.len() >= STDERR_TAIL_LINES {
            guard.pop_front();
        }
        guard.push_back(trimmed.to_string());
    }
}

fn stderr_tail_snapshot(tail: &Arc<Mutex<VecDeque<String>>>) -> Option<String> {
    let guard = tail.lock().expect("codex exec stderr tail lock poisoned");
    if guard.is_empty() {
        None
    } else {
        Some(guard.iter().cloned().collect::<Vec<_>>().join("\n"))
    }
}

fn normalized_optional(value: &Option<String>) -> Option<String> {
    value
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

impl CodexExecRuntimeSink {
    async fn post_latency_stage(&self, stage: &str, metrics: Value) {
        eprintln!(
            "[codex-exec] latency stage={stage} session={} run={} turn={} metrics={metrics}",
            self.session_id,
            self.run_id,
            self.turn_id.as_deref().unwrap_or("unknown"),
        );
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("console:latency:{}:{}", self.run_id, stage),
            "payload": {
                "progress_kind": "console_latency_stage",
                "stage": stage,
                "metrics": metrics,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
            }
        })])
        .await;
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        let phase_identity = if phase == "tool" {
            format!("tool:{}", uuid::Uuid::new_v4())
        } else {
            phase.to_string()
        };
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("codex-app-server:{}:{}:phase:{}", self.session_id, self.run_id, phase_identity),
            "payload": {
                "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
            }
        })])
        .await;
    }

    async fn post_live_user_item(&self, text: &str) {
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": "codex_console_live",
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("console:user:{}", self.run_id),
            "payload": {
                "progress_kind": "console_live_user_item",
                "managed_transport": "codex_app_server",
                "execution_lifetime": "one_shot",
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "text": text,
                "input_origin": {
                    "authored_via": "longhouse",
                    "client_request_id": self.client_request_id,
                    "turn_id": self.turn_id,
                    "run_id": self.run_id,
                },
            }
        })])
        .await;
    }

    async fn post_app_server_event(
        &self,
        seq: u64,
        event: &Value,
        projection: &mut AppServerProjection,
    ) {
        self.post_progress(
            seq,
            json!({"progress_kind": "codex_app_server_jsonrpc", "seq": seq, "event": event}),
            None,
        )
        .await;

        for projected in projection.apply(event) {
            match projected {
                ProjectedAppServerEvent::Phase { phase, tool_name } => {
                    self.post_phase(phase, tool_name).await;
                }
                ProjectedAppServerEvent::AssistantItem {
                    item_id,
                    item_seq,
                    seq,
                    delta,
                    text,
                    completed,
                } => {
                    self.post_live_transcript(&item_id, item_seq, seq, &delta, &text, completed)
                        .await;
                }
                ProjectedAppServerEvent::ToolItem {
                    item_id,
                    command,
                    output,
                    status,
                    seq,
                    completed,
                } => {
                    self.post_live_tool_item(&item_id, &command, &output, &status, seq, completed)
                        .await;
                }
            }
        }
    }

    async fn post_live_tool_item(
        &self,
        item_id: &str,
        command: &str,
        output: &str,
        status: &str,
        seq: u64,
        completed: bool,
    ) {
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": "codex_console_live",
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("console:tool:{}:{}:{}", self.run_id, item_id, seq),
            "payload": {
                "progress_kind": "console_live_tool_item",
                "managed_transport": "codex_app_server",
                "execution_lifetime": "one_shot",
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "item_id": item_id,
                "command": command,
                "output": output,
                "status": status,
                "seq": seq,
                "completed": completed,
            }
        })])
        .await;
    }

    async fn post_live_transcript(
        &self,
        item_id: &str,
        item_seq: u64,
        seq: u64,
        delta: &str,
        live_text: &str,
        item_completed: bool,
    ) {
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": "codex_bridge_live",
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("console:live:{}:{}:{}", self.run_id, item_id, item_seq),
            "payload": {
                "progress_kind": "bridge_live_transcript_delta",
                "managed_transport": "codex_app_server",
                "execution_lifetime": "one_shot",
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "item_id": item_id,
                "seq": seq,
                "item_seq": item_seq,
                "delta": delta,
                "live_text": live_text,
                "item_completed": item_completed,
                "turn_completed": false,
            }
        })])
        .await;
    }

    async fn post_provider_binding(&self, provider_thread_id: &str, source_path: Option<&str>) {
        self.persist_local_provider_binding(provider_thread_id, source_path);
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("codex-app-server:{}:{}:binding", self.session_id, self.run_id),
            "payload": {
                "provider_session_id": provider_thread_id,
                "provider_thread_id": provider_thread_id,
                "source_path": source_path,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
            }
        })])
        .await;
    }

    #[cfg(unix)]
    async fn wake_transcript_shipper(
        &self,
        source_path: &str,
        provider_turn_id: &str,
        wake_reason: &str,
    ) {
        let socket_path = self
            .local_db_path
            .as_deref()
            .and_then(std::path::Path::parent)
            .map(|parent| parent.join("transcript-wake.sock"))
            .or_else(|| crate::config::get_agent_transcript_wake_socket_path().ok());
        let Some(socket_path) = socket_path else {
            eprintln!(
                "[codex-exec] latency stage=durable_wake_miss session={} run={} reason=socket_unresolved",
                self.session_id, self.run_id
            );
            return;
        };
        if !socket_path.exists() {
            eprintln!(
                "[codex-exec] latency stage=durable_wake_miss session={} run={} reason=socket_missing socket={}",
                self.session_id,
                self.run_id,
                socket_path.display()
            );
            return;
        }
        let payload = transcript_wake_payload(
            self,
            source_path,
            provider_turn_id,
            wake_reason,
            std::fs::metadata(source_path).ok().map(|metadata| metadata.len()),
        );
        let bytes = payload.to_string().into_bytes();
        let socket_display = socket_path.display().to_string();
        let write = tokio::task::spawn_blocking(move || -> std::io::Result<()> {
            let mut stream = std::os::unix::net::UnixStream::connect(socket_path)?;
            stream.set_write_timeout(Some(Duration::from_millis(50)))?;
            stream.write_all(&bytes)
        });
        match tokio::time::timeout(Duration::from_millis(75), write).await {
            Ok(Ok(Ok(()))) => eprintln!(
                "[codex-exec] latency stage=durable_wake_sent session={} run={} turn={} provider_turn={} path={}",
                self.session_id,
                self.run_id,
                self.turn_id.as_deref().unwrap_or("unknown"),
                provider_turn_id,
                source_path
            ),
            Ok(Ok(Err(err))) => eprintln!(
                "[codex-exec] latency stage=durable_wake_miss session={} run={} reason=connect_or_write_failed socket={} error={err}",
                self.session_id, self.run_id, socket_display
            ),
            Ok(Err(err)) => eprintln!(
                "[codex-exec] latency stage=durable_wake_miss session={} run={} reason=join_failed error={err}",
                self.session_id, self.run_id
            ),
            Err(_) => eprintln!(
                "[codex-exec] latency stage=durable_wake_miss session={} run={} reason=timeout socket={}",
                self.session_id, self.run_id, socket_display
            ),
        }
    }

    #[cfg(not(unix))]
    async fn wake_transcript_shipper(
        &self,
        _source_path: &str,
        _provider_turn_id: &str,
        _wake_reason: &str,
    ) {
    }

    async fn post_progress(
        &self,
        seq: u64,
        mut payload: Value,
        provider_thread_id: Option<String>,
    ) {
        if let Some(obj) = payload.as_object_mut() {
            obj.insert(
                "managed_transport".to_string(),
                Value::String(CODEX_EXEC_RUNTIME_SOURCE.to_string()),
            );
            obj.insert(
                "execution_lifetime".to_string(),
                Value::String("one_shot".to_string()),
            );
        }
        let mut events = vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "progress_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("codex-exec:{}:{}:stdout:{seq}", self.session_id, self.run_id),
            "payload": payload,
        })];
        if let Some(provider_thread_id) = provider_thread_id {
            events.push(json!({
                "runtime_key": format!("codex:{}", self.session_id),
                "session_id": self.session_id,
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "provider": "codex",
                "device_id": self.machine_name,
                "source": CODEX_EXEC_RUNTIME_SOURCE,
                "kind": "binding_signal",
                "occurred_at": Utc::now().to_rfc3339(),
                "dedupe_key": format!("codex-exec:{}:{}:binding", self.session_id, self.run_id),
                "payload": {
                    "provider_session_id": provider_thread_id,
                    "turn_id": self.turn_id,
                    "client_request_id": self.client_request_id,
                },
            }));
        }
        self.post_events(events).await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr_tail: Option<String>,
    ) {
        crate::turn_claims::mark_terminal(&self.run_id, terminal_state, stderr_tail.clone());
        let observed_at = Utc::now();
        self.persist_local_phase("finished", None, observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "phase": Value::Null,
            "tool_name": Value::Null,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("codex-exec:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CODEX_EXEC_RUNTIME_SOURCE,
                "exit_code": exit_code,
                "stderr_tail": stderr_tail,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
            }
        })])
        .await;
    }

    fn persist_local_provider_binding(
        &self,
        provider_thread_id: &str,
        known_source_path: Option<&str>,
    ) {
        let source_path = known_source_path
            .map(PathBuf::from)
            .or_else(|| codex_rollout_path(provider_thread_id));
        if let Some(db_path) = self.local_db_path.as_deref() {
            if let Some(source_path) = source_path.as_deref() {
                match crate::state::db::open_db(Some(db_path)) {
                    Ok(conn) => {
                        let binding = crate::state::session_binding::SessionBinding::new(&conn);
                        if let Err(err) =
                            binding.bind(&source_path.to_string_lossy(), &self.session_id, "codex")
                        {
                            eprintln!("[codex-exec] persist transcript binding failed: {err}");
                        }
                    }
                    Err(err) => eprintln!("[codex-exec] open transcript binding DB failed: {err}"),
                }
            }
        }
        if let Ok(registry) = crate::turn_claims::default_registry() {
            let source = source_path.as_ref().map(|path| path.to_string_lossy());
            let _ =
                registry.mark_provider_binding(&self.run_id, provider_thread_id, source.as_deref());
        }
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
                eprintln!("[codex-exec] open local phase DB failed: {err}");
                return;
            }
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "codex".to_string(),
            phase: phase.to_string(),
            tool_name,
            source: CODEX_EXEC_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[codex-exec] persist local phase failed for {}: {err}",
                self.session_id
            );
        }
        let managed_signal = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "codex".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name: signal.tool_name.clone(),
            phase_source: CODEX_EXEC_RUNTIME_SOURCE.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::managed_session_state::ManagedSessionStateStore::new(&conn)
            .record_phase(&managed_signal)
        {
            eprintln!(
                "[codex-exec] persist managed session state failed for {}: {err}",
                self.session_id
            );
        }
    }

    async fn post_events(&self, events: Vec<Value>) {
        let count = events.len();
        self.queued_events.fetch_add(count, Ordering::Relaxed);
        if events_are_critical(&events) {
            if self.critical_event_tx.send(events).await.is_err() {
                self.queued_events.fetch_sub(count, Ordering::Relaxed);
                eprintln!(
                    "[codex-exec] runtime critical event pump closed session={} run={} dropped_events={count}",
                    self.session_id, self.run_id
                );
            }
            return;
        }
        match self.event_tx.try_send(events) {
            Ok(()) => {}
            Err(mpsc::error::TrySendError::Full(_)) => {
                let remaining = self
                    .queued_events
                    .fetch_sub(count, Ordering::Relaxed)
                    .saturating_sub(count);
                eprintln!(
                    "[codex-exec] latency stage=runtime_queue_drop session={} run={} dropped_events={count} queued={remaining} reason=bounded_queue_full",
                    self.session_id, self.run_id
                );
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                self.queued_events.fetch_sub(count, Ordering::Relaxed);
                eprintln!(
                    "[codex-exec] runtime event pump closed session={} run={} dropped_events={count}",
                    self.session_id, self.run_id
                );
            }
        }
    }
}

const EVENT_PUMP_COALESCE_WINDOW: Duration = Duration::from_millis(8);
const EVENT_PUMP_MAX_BATCH: usize = 128;
const EVENT_PUMP_QUEUE_CAPACITY: usize = 256;
const EVENT_PUMP_CRITICAL_CAPACITY: usize = 64;

fn events_are_critical(events: &[Value]) -> bool {
    events.iter().any(|event| {
        matches!(
            event.get("kind").and_then(Value::as_str),
            Some("terminal_signal" | "binding_signal")
        ) || matches!(
            json_string(event, &["payload", "progress_kind"]).as_deref(),
            Some("console_live_tool_item")
        ) && event
            .get("payload")
            .and_then(|payload| payload.get("completed"))
            .and_then(Value::as_bool)
            == Some(true)
            || event
            .get("payload")
            .and_then(|payload| payload.get("item_completed"))
            .and_then(Value::as_bool)
            == Some(true)
    })
}

async fn run_runtime_event_pump(
    api_url: String,
    api_token: String,
    session_id: String,
    run_id: String,
    mut receiver: mpsc::Receiver<Vec<Value>>,
    mut critical_receiver: mpsc::Receiver<Vec<Value>>,
    queued_events: Arc<AtomicUsize>,
) {
    let http = reqwest::Client::new();
    let url = format!(
        "{}/api/agents/runtime/events/batch",
        api_url.trim_end_matches('/')
    );
    loop {
        let first = tokio::select! {
            biased;
            event = critical_receiver.recv(), if !critical_receiver.is_closed() => event,
            event = receiver.recv(), if !receiver.is_closed() => event,
            else => None,
        };
        let Some(first) = first else {
            if receiver.is_closed() && critical_receiver.is_closed() {
                break;
            }
            continue;
        };
        let queued_at = std::time::Instant::now();
        let mut events = first;
        if receiver.is_empty() && critical_receiver.is_empty() {
            tokio::time::sleep(EVENT_PUMP_COALESCE_WINDOW).await;
        }
        while events.len() < EVENT_PUMP_MAX_BATCH {
            if let Ok(mut next) = critical_receiver.try_recv() {
                events.append(&mut next);
            } else if let Ok(mut next) = receiver.try_recv() {
                events.append(&mut next);
            } else {
                break;
            }
        }
        let event_count = events.len();
        let payload = json!({ "events": events });
        let mut delivered = false;
        for attempt in 0..3 {
            let started = std::time::Instant::now();
            let response = http
                .post(&url)
                .header("X-Agents-Token", &api_token)
                .json(&payload)
                .timeout(Duration::from_secs(5))
                .send()
                .await;
            match response {
                Ok(response) if response.status().is_success() => {
                    let remaining = queued_events
                        .fetch_sub(event_count, Ordering::Relaxed)
                        .saturating_sub(event_count);
                    eprintln!(
                        "[codex-exec] latency stage=runtime_batch_ack session={session_id} run={run_id} events={event_count} queue_ms={} http_ms={} remaining={remaining}",
                        queued_at.elapsed().as_millis(),
                        started.elapsed().as_millis(),
                    );
                    delivered = true;
                    break;
                }
                Ok(response) => {
                    let retryable = response.status().is_server_error()
                        || response.status().as_u16() == 429;
                    eprintln!(
                        "[codex-exec] runtime event post failed session={session_id} run={run_id} status={} attempt={} events={event_count} retryable={retryable}",
                        response.status(),
                        attempt + 1
                    );
                    if !retryable {
                        break;
                    }
                }
                Err(err) => eprintln!(
                    "[codex-exec] runtime event post failed session={session_id} run={run_id} attempt={} events={event_count} error={err}",
                    attempt + 1
                ),
            }
            if attempt < 2 {
                tokio::time::sleep(Duration::from_millis(100 * (attempt + 1))).await;
            }
        }
        if !delivered {
            let remaining = queued_events
                .fetch_sub(event_count, Ordering::Relaxed)
                .saturating_sub(event_count);
            eprintln!(
                "[codex-exec] runtime event batch dropped after retries session={session_id} run={run_id} events={event_count} remaining={remaining}"
            );
        }
    }
}

fn transcript_wake_payload(
    sink: &CodexExecRuntimeSink,
    source_path: &str,
    provider_turn_id: &str,
    wake_reason: &str,
    file_len_hint: Option<u64>,
) -> Value {
    json!({
        "provider": "codex",
        "path": source_path,
        "phase": "idle",
        "session_id": sink.session_id,
        "run_id": sink.run_id,
        "turn_id": sink.turn_id,
        "provider_turn_id": provider_turn_id,
        "client_request_id": sink.client_request_id,
        "wake_reason": wake_reason,
        "observed_at_ms": Utc::now().timestamp_millis(),
        "file_len_hint": file_len_hint,
    })
}

fn codex_rollout_path(provider_thread_id: &str) -> Option<PathBuf> {
    let codex_home = std::env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".codex")))?;
    find_codex_rollout_path(&codex_home.join("sessions"), provider_thread_id)
}

fn find_codex_rollout_path(
    sessions_root: &std::path::Path,
    provider_thread_id: &str,
) -> Option<PathBuf> {
    let suffix = format!("-{provider_thread_id}.jsonl");
    WalkDir::new(sessions_root)
        .min_depth(1)
        .max_depth(5)
        .into_iter()
        .filter_map(|entry| entry.ok())
        .find(|entry| {
            entry.file_type().is_file() && entry.file_name().to_string_lossy().ends_with(&suffix)
        })
        .map(|entry| entry.into_path())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, UnixListener};
    use tokio::sync::mpsc;

    fn config() -> CodexExecRunConfig {
        CodexExecRunConfig {
            session_id: "11111111-1111-4111-8111-111111111111".to_string(),
            run_id: "22222222-2222-4222-8222-222222222222".to_string(),
            thread_id: Some("44444444-4444-4444-8444-444444444444".to_string()),
            turn_id: Some("55555555-5555-4555-8555-555555555555".to_string()),
            client_request_id: Some("request-1".to_string()),
            cwd: PathBuf::from("/tmp/project"),
            api_url: "http://localhost:8080".to_string(),
            api_token: "token".to_string(),
            codex_bin: "codex".to_string(),
            approval_policy: Some("never".to_string()),
            sandbox: Some("workspace-write".to_string()),
            prompt: "Do one bounded turn".to_string(),
            launch_actor: None,
            launch_surface: None,
            resume_thread_id: None,
            machine_name: "cinder".to_string(),
            local_db_path: None,
        }
    }

    #[test]
    fn five_hundred_sessions_reserve_only_machine_global_pool_target() {
        let mut pool = CodexConsoleWorkerPool::default();
        let reservations = (0..500)
            .filter(|_| pool.reserve_spawn_slot())
            .count();

        assert_eq!(reservations, CONSOLE_WARM_POOL_TARGET);
        assert_eq!(pool.spawning, CONSOLE_WARM_POOL_TARGET);
    }

    #[tokio::test]
    async fn worker_shutdown_reaps_provider_owned_process_group_children() {
        let temp = tempfile::tempdir().unwrap();
        let fake_codex = temp.path().join("codex");
        let child_pid_path = temp.path().join("child.pid");
        fs::write(
            &fake_codex,
            format!(
                r#"#!/usr/bin/env python3
import json, os, subprocess, sys, time
child = subprocess.Popen(["sleep", "60"])
open({pid_path:?}, "w").write(str(child.pid))
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({{"id": msg["id"], "result": {{"userAgent": "fake/1"}}}}), flush=True)
    elif msg.get("method") == "initialized":
        time.sleep(60)
"#,
                pid_path = child_pid_path.display().to_string()
            ),
        )
        .unwrap();
        let mut permissions = fs::metadata(&fake_codex).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&fake_codex, permissions).unwrap();

        let mut worker = spawn_initialized_codex_worker(
            fake_codex.to_str().unwrap(),
            Some("never"),
            Some("workspace-write"),
            temp.path(),
            None,
            None,
            None,
        )
        .await
        .unwrap();
        let child_pid: i32 = fs::read_to_string(&child_pid_path)
            .unwrap()
            .parse()
            .unwrap();

        shutdown_worker_process_group(&mut worker.child, worker.pgid)
            .await
            .unwrap();

        let deadline = std::time::Instant::now() + Duration::from_secs(1);
        while unsafe { libc::kill(child_pid, 0) } == 0 && std::time::Instant::now() < deadline {
            tokio::task::yield_now().await;
        }
        assert_ne!(unsafe { libc::kill(child_pid, 0) }, 0);
    }

    fn runtime_sink(local_db_path: Option<PathBuf>) -> CodexExecRuntimeSink {
        let config = config();
        CodexExecRuntimeSink {
            session_id: config.session_id,
            run_id: config.run_id,
            thread_id: config.thread_id,
            turn_id: config.turn_id,
            client_request_id: config.client_request_id,
            machine_name: config.machine_name,
            cwd: config.cwd.to_string_lossy().to_string(),
            local_db_path,
            event_tx: mpsc::channel(EVENT_PUMP_QUEUE_CAPACITY).0,
            critical_event_tx: mpsc::channel(EVENT_PUMP_CRITICAL_CAPACITY).0,
            queued_events: Arc::new(AtomicUsize::new(0)),
        }
    }

    #[test]
    fn completion_wake_carries_full_turn_correlation() {
        let sink = runtime_sink(None);
        let payload = transcript_wake_payload(
            &sink,
            "/tmp/rollout.jsonl",
            "provider-turn-7",
            "turn_completed",
            Some(321),
        );

        assert_eq!(payload["session_id"], sink.session_id);
        assert_eq!(payload["run_id"], sink.run_id);
        assert_eq!(payload["turn_id"], sink.turn_id.unwrap());
        assert_eq!(payload["client_request_id"], sink.client_request_id.unwrap());
        assert_eq!(payload["provider_turn_id"], "provider-turn-7");
        assert_eq!(payload["wake_reason"], "turn_completed");
        assert_eq!(payload["file_len_hint"], 321);
    }

    #[tokio::test]
    async fn completion_wake_uses_agent_socket_next_to_local_db() {
        let temp = tempfile::tempdir().unwrap();
        let agent_dir = temp.path().join("agent");
        fs::create_dir_all(&agent_dir).unwrap();
        let socket_path = agent_dir.join("transcript-wake.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let rollout = temp.path().join("rollout.jsonl");
        fs::write(&rollout, b"provider evidence").unwrap();
        let sink = runtime_sink(Some(agent_dir.join("longhouse-shipper.db")));

        sink.wake_transcript_shipper(
            rollout.to_str().unwrap(),
            "provider-turn-8",
            "turn_completed",
        )
        .await;

        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(1), listener.accept())
            .await
            .unwrap()
            .unwrap();
        let mut bytes = Vec::new();
        stream.read_to_end(&mut bytes).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(payload["run_id"], sink.run_id);
        assert_eq!(payload["provider_turn_id"], "provider-turn-8");
        assert_eq!(payload["file_len_hint"], 17);
    }

    #[tokio::test]
    async fn runtime_event_pump_coalesces_adjacent_provider_events() {
        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let (tx, rx) = mpsc::channel(EVENT_PUMP_QUEUE_CAPACITY);
        let (critical_tx, critical_rx) = mpsc::channel(EVENT_PUMP_CRITICAL_CAPACITY);
        let queued = Arc::new(AtomicUsize::new(2));
        let pump = tokio::spawn(run_runtime_event_pump(
            api_url,
            "token".to_string(),
            "session-1".to_string(),
            "run-1".to_string(),
            rx,
            critical_rx,
            queued.clone(),
        ));

        tx.try_send(vec![json!({"seq": 1})]).unwrap();
        tx.try_send(vec![json!({"seq": 2})]).unwrap();
        drop(tx);
        drop(critical_tx);

        let batch = tokio::time::timeout(Duration::from_secs(1), received.recv())
            .await
            .unwrap()
            .unwrap();
        pump.await.unwrap();
        assert_eq!(batch.len(), 2);
        assert_eq!(batch[0]["seq"], 1);
        assert_eq!(batch[1]["seq"], 2);
        assert_eq!(queued.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn provider_event_enqueue_never_waits_for_runtime_host() {
        let (tx, _rx) = mpsc::channel(EVENT_PUMP_QUEUE_CAPACITY);
        let sink = CodexExecRuntimeSink {
            event_tx: tx,
            queued_events: Arc::new(AtomicUsize::new(0)),
            ..runtime_sink(None)
        };

        tokio::time::timeout(
            Duration::from_millis(10),
            sink.post_events(vec![json!({"kind": "progress_signal"})]),
        )
        .await
        .expect("provider reader must only enqueue, never perform HTTP");
        assert_eq!(sink.queued_events.load(Ordering::Relaxed), 1);
    }

    #[tokio::test]
    async fn bounded_preview_queue_preserves_critical_terminal_lane() {
        let (event_tx, _event_rx) = mpsc::channel(1);
        let (critical_event_tx, mut critical_event_rx) =
            mpsc::channel(EVENT_PUMP_CRITICAL_CAPACITY);
        let sink = CodexExecRuntimeSink {
            event_tx,
            critical_event_tx,
            queued_events: Arc::new(AtomicUsize::new(0)),
            ..runtime_sink(None)
        };

        sink.post_events(vec![json!({"kind": "progress_signal"})])
            .await;
        sink.post_events(vec![json!({"kind": "progress_signal"})])
            .await;
        sink.post_events(vec![json!({"kind": "terminal_signal"})])
            .await;

        let critical = critical_event_rx.recv().await.unwrap();
        assert_eq!(critical[0]["kind"], "terminal_signal");
        assert_eq!(sink.queued_events.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn in_progress_tool_updates_use_bounded_preview_lane() {
        let in_progress = vec![json!({
            "kind": "progress_signal",
            "payload": {"progress_kind": "console_live_tool_item", "completed": false}
        })];
        let completed = vec![json!({
            "kind": "progress_signal",
            "payload": {"progress_kind": "console_live_tool_item", "completed": true}
        })];

        assert!(!events_are_critical(&in_progress));
        assert!(events_are_critical(&completed));
    }

    #[test]
    fn codex_app_server_args_are_noninteractive_and_bounded() {
        let args = codex_exec_args(&config())
            .into_iter()
            .map(|value| value.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(
            args,
            vec![
                "-c",
                "check_for_update_on_startup=false",
                "-c",
                "approval_policy=\"never\"",
                "-s",
                "workspace-write",
                "app-server",
                "--listen",
                "stdio://",
            ]
        );
    }

    #[test]
    fn codex_app_server_resume_is_rpc_state_not_process_argv() {
        let mut config = config();
        config.resume_thread_id = Some("33333333-3333-4333-8333-333333333333".to_string());
        config.prompt = "Continue with one bounded follow-up".to_string();

        let args = codex_exec_args(&config)
            .into_iter()
            .map(|value| value.to_string_lossy().to_string())
            .collect::<Vec<_>>();

        assert_eq!(
            args,
            vec![
                "-c",
                "check_for_update_on_startup=false",
                "-c",
                "approval_policy=\"never\"",
                "-s",
                "workspace-write",
                "app-server",
                "--listen",
                "stdio://",
            ]
        );
    }

    #[test]
    fn terminal_payload_uses_run_terminal_state() {
        let session_id = "session-1".to_string();
        let run_id = "run-1".to_string();
        let machine_name = "cinder".to_string();
        let event = json!({
            "runtime_key": format!("codex:{}", session_id),
            "session_id": session_id,
            "run_id": run_id,
            "provider": "codex",
            "device_id": machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": "test",
            "payload": {
                "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
                "execution_lifetime": "one_shot",
                "terminal_state": "run_completed",
                "terminal_reason": "run_completed",
                "terminal_source": CODEX_EXEC_RUNTIME_SOURCE,
                "exit_code": 0,
            }
        });

        assert_eq!(event["payload"]["terminal_state"], "run_completed");
        assert_eq!(event["run_id"], "run-1");
    }

    #[test]
    fn progress_payload_marks_codex_app_server_transport() {
        let payload = json!({"progress_kind": "codex_app_server_jsonrpc"});
        let mut obj = payload.as_object().unwrap().clone();
        obj.insert(
            "managed_transport".to_string(),
            Value::String(CODEX_EXEC_RUNTIME_SOURCE.to_string()),
        );
        obj.insert(
            "execution_lifetime".to_string(),
            Value::String("one_shot".to_string()),
        );
        assert_eq!(obj["managed_transport"], "codex_app_server");
        assert_eq!(obj["execution_lifetime"], "one_shot");
    }

    #[test]
    fn real_app_server_shapes_keep_message_boundaries_and_failed_tools() {
        let mut projection = AppServerProjection::default();
        let events = [
            json!({"method":"item/agentMessage/delta","params":{"itemId":"msg_a","delta":"First"}}),
            json!({"method":"item/completed","params":{"item":{"id":"msg_a","type":"agentMessage"}}}),
            json!({"method":"item/agentMessage/delta","params":{"itemId":"msg_b","delta":"Second"}}),
            json!({"method":"item/completed","params":{"item":{"id":"msg_c","type":"assistantMessage","text":"Completed without delta"}}}),
            json!({"method":"item/started","params":{"item":{"id":"exec_a","type":"commandExecution","command":"printf ok","status":"inProgress"}}}),
            json!({"method":"item/completed","params":{"item":{"id":"exec_a","type":"commandExecution","command":"printf ok","aggregatedOutput":"parse error\n","status":"failed","exitCode":1}}}),
        ];
        let projected = events
            .iter()
            .flat_map(|event| projection.apply(event))
            .collect::<Vec<_>>();

        assert!(projected.contains(&ProjectedAppServerEvent::AssistantItem {
            item_id: "msg_a".to_string(),
            item_seq: 2,
            seq: 2,
            delta: String::new(),
            text: "First".to_string(),
            completed: true,
        }));
        assert!(projected.contains(&ProjectedAppServerEvent::AssistantItem {
            item_id: "msg_b".to_string(),
            item_seq: 1,
            seq: 3,
            delta: "Second".to_string(),
            text: "Second".to_string(),
            completed: false,
        }));
        assert!(projected.contains(&ProjectedAppServerEvent::ToolItem {
            item_id: "exec_a".to_string(),
            command: "printf ok".to_string(),
            output: "parse error\n".to_string(),
            status: "failed".to_string(),
            seq: 2,
            completed: true,
        }));
        assert!(projected.contains(&ProjectedAppServerEvent::AssistantItem {
            item_id: "msg_c".to_string(),
            item_seq: 1,
            seq: 4,
            delta: String::new(),
            text: "Completed without delta".to_string(),
            completed: true,
        }));
    }

    #[tokio::test]
    async fn bounded_app_server_turn_streams_binding_text_tool_and_terminal() {
        let temp = tempfile::tempdir().unwrap();
        let fake_codex = temp.path().join("codex");
        fs::write(
            &fake_codex,
            r#"#!/usr/bin/env python3
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        emit({"id": msg["id"], "result": {"userAgent": "fake/1"}})
    elif method == "initialized":
        pass
    elif method == "thread/resume":
        if msg.get("params", {}).get("threadId") != "provider-thread":
            sys.exit(8)
        emit({"id": msg["id"], "result": {"thread": {"id": "provider-thread", "path": "/tmp/rollout-provider-thread.jsonl"}}})
    elif method == "turn/start":
        emit({"id": msg["id"], "result": {"turn": {"id": "provider-turn", "status": "inProgress"}}})
        emit({"method": "turn/started", "params": {"turn": {"id": "provider-turn", "status": "inProgress"}}})
        emit({"method": "item/agentMessage/delta", "params": {"itemId": "msg-1", "delta": "Working now"}})
        emit({"method": "item/completed", "params": {"item": {"id": "msg-1", "type": "agentMessage"}}})
        emit({"method": "item/started", "params": {"item": {"id": "exec-1", "type": "commandExecution", "command": "pwd", "status": "inProgress"}}})
        emit({"method": "item/completed", "params": {"item": {"id": "exec-1", "type": "commandExecution", "command": "pwd", "aggregatedOutput": "/tmp\n", "status": "completed", "exitCode": 0}}})
        emit({"method": "turn/completed", "params": {"turn": {"id": "provider-turn", "status": "completed"}}})
"#,
        )
        .unwrap();
        let mut permissions = fs::metadata(&fake_codex).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&fake_codex, permissions).unwrap();

        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let mut run_config = config();
        run_config.cwd = temp.path().join("workspace");
        fs::create_dir_all(&run_config.cwd).unwrap();
        run_config.codex_bin = fake_codex.display().to_string();
        run_config.api_url = api_url;
        run_config.resume_thread_id = Some("provider-thread".to_string());
        let summary = start_codex_exec_once(run_config).await.unwrap();

        let mut events = Vec::new();
        tokio::time::timeout(Duration::from_secs(10), async {
            while let Some(batch) = received.recv().await {
                events.extend(batch);
                if events.iter().any(|event: &Value| {
                    event.get("kind").and_then(Value::as_str) == Some("terminal_signal")
                }) {
                    break;
                }
            }
        })
        .await
        .unwrap();

        assert!(summary.pid.is_some());
        assert!(events.iter().any(|event| {
            event.get("kind").and_then(Value::as_str) == Some("binding_signal")
                && json_string(event, &["payload", "provider_thread_id"]).as_deref()
                    == Some("provider-thread")
        }));
        assert!(events.iter().any(|event| {
            json_string(event, &["payload", "progress_kind"]).as_deref()
                == Some("console_live_user_item")
                && json_string(event, &["payload", "input_origin", "authored_via"]).as_deref()
                    == Some("longhouse")
        }));
        assert!(events.iter().any(|event| {
            json_string(event, &["payload", "progress_kind"]).as_deref()
                == Some("bridge_live_transcript_delta")
                && json_string(event, &["payload", "live_text"]).as_deref() == Some("Working now")
        }));
        assert!(events.iter().any(|event| {
            json_string(event, &["payload", "progress_kind"]).as_deref()
                == Some("console_live_tool_item")
                && json_string(event, &["payload", "output"]).as_deref() == Some("/tmp\n")
        }));
        assert!(events.iter().any(|event| {
            event.get("kind").and_then(Value::as_str) == Some("terminal_signal")
                && json_string(event, &["payload", "terminal_state"]).as_deref()
                    == Some("run_completed")
        }));
    }

    #[tokio::test]
    #[ignore = "calls the installed Codex provider; run explicitly as an external contract canary"]
    async fn installed_codex_completes_through_production_console_adapter() {
        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let mut run_config = config();
        run_config.cwd = std::env::current_dir().unwrap();
        run_config.api_url = api_url;
        run_config.codex_bin =
            std::env::var("LONGHOUSE_TEST_CODEX_BIN").unwrap_or_else(|_| "codex".to_string());
        run_config.prompt = "Reply with exactly PRODUCTION_ADAPTER_CANARY_OK.".to_string();
        prewarm_codex_console_workers().await;
        let summary = start_codex_exec_once(run_config).await.unwrap();

        let mut saw_text = false;
        let mut saw_terminal = false;
        let mut warm_lease_to_turn_write_ms = None;
        tokio::time::timeout(Duration::from_secs(120), async {
            while let Some(batch) = received.recv().await {
                for event in batch {
                    if json_string(&event, &["payload", "progress_kind"]).as_deref()
                        == Some("console_latency_stage")
                        && json_string(&event, &["payload", "stage"]).as_deref()
                            == Some("turn_start_write")
                        && event["payload"]["metrics"]["warm_hit"].as_bool() == Some(true)
                    {
                        warm_lease_to_turn_write_ms =
                            event["payload"]["metrics"]["lease_to_write_ms"].as_u64();
                    }
                    saw_text |= json_string(&event, &["payload", "live_text"])
                        .is_some_and(|text| text.contains("PRODUCTION_ADAPTER_CANARY_OK"));
                    saw_terminal |= event.get("kind").and_then(Value::as_str)
                        == Some("terminal_signal")
                        && json_string(&event, &["payload", "terminal_state"]).as_deref()
                            == Some("run_completed");
                }
                if saw_text && saw_terminal && warm_lease_to_turn_write_ms.is_some() {
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert!(summary.pid.is_some());
        assert!(
            warm_lease_to_turn_write_ms.is_some_and(|elapsed| elapsed < 500),
            "warm lease did not write turn/start within 500ms: {warm_lease_to_turn_write_ms:?}"
        );
        assert!(saw_text, "installed Codex emitted no live assistant text");
        assert!(
            saw_terminal,
            "installed Codex turn did not settle successfully"
        );
    }

    async fn spawn_runtime_capture_server() -> (String, mpsc::UnboundedReceiver<Vec<Value>>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let address = listener.local_addr().unwrap();
        let (tx, rx) = mpsc::unbounded_channel();
        tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    break;
                };
                let mut bytes = Vec::new();
                let body = loop {
                    let mut chunk = [0u8; 4096];
                    let read = stream.read(&mut chunk).await.unwrap();
                    if read == 0 {
                        break Vec::new();
                    }
                    bytes.extend_from_slice(&chunk[..read]);
                    let Some(header_end) =
                        bytes.windows(4).position(|window| window == b"\r\n\r\n")
                    else {
                        continue;
                    };
                    let head = String::from_utf8_lossy(&bytes[..header_end]);
                    let content_length = head
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())?
                        })
                        .unwrap_or(0);
                    let body_start = header_end + 4;
                    if bytes.len() >= body_start + content_length {
                        break bytes[body_start..body_start + content_length].to_vec();
                    }
                };
                if let Ok(value) = serde_json::from_slice::<Value>(&body) {
                    let events = value
                        .get("events")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default();
                    let _ = tx.send(events);
                }
                stream
                    .write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                    .await
                    .unwrap();
            }
        });
        (format!("http://{address}"), rx)
    }

    #[test]
    fn finds_codex_rollout_by_provider_thread_id() {
        let temp = tempfile::tempdir().unwrap();
        let thread_id = "019f6b93-edf6-7bd0-a757-b5195a61abdd";
        let day = temp.path().join("2026/07/16");
        std::fs::create_dir_all(&day).unwrap();
        let rollout = day.join(format!("rollout-2026-07-16T11-38-04-{thread_id}.jsonl"));
        std::fs::write(&rollout, "{}\n").unwrap();

        assert_eq!(
            find_codex_rollout_path(temp.path(), thread_id),
            Some(rollout)
        );
    }
}
