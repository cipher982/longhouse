use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::sync::{LazyLock, OnceLock};
use std::time::{Duration, Instant};

#[cfg(unix)]
use std::io::Write as _;

use anyhow::{bail, Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::io::{AsyncWriteExt, Lines};
use tokio::process::Command;

use crate::console_lifecycle::{
    ConsoleInput, ConsoleInvocation, IdleOutcome, IdleSignal, InvocationCloseReason,
    InvocationState, PendingItem, TurnBinding, TurnOrigin, WakeRequest,
};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use tokio::process::{Child, ChildStdin, ChildStdout};
use tokio::sync::{mpsc, Mutex as AsyncMutex};
use walkdir::WalkDir;

const CODEX_EXEC_RUNTIME_SOURCE: &str = "codex_app_server";
const STDERR_TAIL_LINES: usize = 40;
const APP_SERVER_TURN_TIMEOUT: Duration = Duration::from_secs(60 * 60);
const CONSOLE_WARM_POOL_TARGET: usize = 1;
/// A user waits on this one, and the Runtime Host waits only 10 s for the
/// turn-start reply (`CONSOLE_CONTROL_REPLY_TIMEOUT_SECONDS`), so it stays
/// well inside that.
const TURN_INITIALIZE_BUDGET: Duration = Duration::from_secs(5);
/// The machine-global warm worker nobody waits on. On a fresh 4-vCPU Mac
/// still importing history the prewarm and then the first turn both timed out
/// at 5 s (stranger run 10041621cd97), and under import-like load
/// `initialize` took over 20 s in 4/15 tries (control-plane spec
/// stranger-run.md F11). A prewarm that dies at 5 s leaves every later turn
/// cold; 60 s = 3x that measured 20 s tail.
const PREWARM_INITIALIZE_BUDGET: Duration = Duration::from_secs(60);
const DEFAULT_CODEX_BIN: &str = "codex";
pub const DEFAULT_CONSOLE_APPROVAL_POLICY: &str = "never";
// Console runs unattended: there is no terminal and no human to answer a
// prompt, so a confining sandbox cannot degrade into "ask first" -- it just
// fails the turn with no way to recover. Every other Console provider already
// runs unconfined (`permission_mode: "bypass"`); Codex was the lone exception,
// which made the same prompt succeed on Claude and fail here. One policy.
pub const DEFAULT_CONSOLE_SANDBOX: &str = "danger-full-access";
pub const CODEX_EXEC_ADAPTER: &str = "codex_exec";

type ConsoleReply = tokio::sync::oneshot::Sender<std::result::Result<(), String>>;

/// Live control of a running Codex Console turn, answered on `reply`.
enum ConsoleControl {
    /// Enter the turn at its next boundary (`turn/steer`).
    Steer { text: String, reply: ConsoleReply },
    /// Stop the turn now (`turn/interrupt`); it completes as `interrupted`.
    Interrupt { reply: ConsoleReply },
}

struct ParkedTurnStart {
    config: CodexExecRunConfig,
    reply: tokio::sync::oneshot::Sender<std::result::Result<(), String>>,
}

enum CodexConsoleInputControl {
    Start(ParkedTurnStart),
    Close,
}

struct CodexConsoleInput {
    sender: mpsc::UnboundedSender<CodexConsoleInputControl>,
    next_turn: AsyncMutex<Option<CodexExecRunConfig>>,
    wake_inputs: AsyncMutex<HashMap<String, CodexWakeInput>>,
}

struct CodexWakeInput {
    completions: Vec<(String, CompletedCommandExecution)>,
    expires_at: Instant,
}

impl CodexConsoleInput {
    async fn set_next_turn(&self, config: CodexExecRunConfig) {
        *self.next_turn.lock().await = Some(config);
    }

    async fn add_wake_completions(
        &self,
        wake_id: String,
        completions: Vec<(String, CompletedCommandExecution)>,
    ) {
        let mut wake_inputs = self.wake_inputs.lock().await;
        let pending = wake_inputs
            .entry(wake_id)
            .or_insert_with(|| CodexWakeInput {
                completions: Vec::new(),
                expires_at: Instant::now() + crate::console_lifecycle::RETAINED_WAKE_TTL,
            });
        pending.completions.extend(completions);
    }

    async fn wake_input_prompt(&self, wake_id: &str, now: Instant) -> Option<String> {
        let wake_inputs = self.wake_inputs.lock().await;
        let pending = wake_inputs.get(wake_id)?;
        (pending.expires_at > now).then(|| command_completion_input(&pending.completions))
    }

    async fn discard_wake_input(&self, wake_id: &str) {
        self.wake_inputs.lock().await.remove(wake_id);
    }

    async fn wake_input_expired(&self, wake_id: &str, now: Instant) -> bool {
        self.wake_inputs
            .lock()
            .await
            .get(wake_id)
            .is_some_and(|pending| pending.expires_at <= now)
    }
}

impl ConsoleInput for CodexConsoleInput {
    fn send_input<'a>(
        &'a self,
        text: &'a str,
        images: &'a [PathBuf],
    ) -> crate::console_lifecycle::InputFuture<'a> {
        Box::pin(async move {
            let mut config = self
                .next_turn
                .lock()
                .await
                .take()
                .context("Codex Console turn context was not queued")?;
            config.prompt = text.to_string();
            config.image_paths = images.to_vec();
            let (reply, outcome) = tokio::sync::oneshot::channel();
            self.sender
                .send(CodexConsoleInputControl::Start(ParkedTurnStart {
                    config,
                    reply,
                }))
                .map_err(|_| anyhow::anyhow!("Codex Console invocation is closed"))?;
            outcome
                .await
                .context("Codex Console invocation stopped before turn/start")?
                .map_err(anyhow::Error::msg)
        })
    }

    fn close_input(&self) -> crate::console_lifecycle::InputFuture<'_> {
        Box::pin(async move {
            let _ = self.sender.send(CodexConsoleInputControl::Close);
            Ok(())
        })
    }
}

fn codex_console_input_registry() -> &'static Mutex<HashMap<String, Arc<CodexConsoleInput>>> {
    static REGISTRY: LazyLock<Mutex<HashMap<String, Arc<CodexConsoleInput>>>> =
        LazyLock::new(|| Mutex::new(HashMap::new()));
    &REGISTRY
}

fn codex_console_input(launch_id: &str) -> Option<Arc<CodexConsoleInput>> {
    codex_console_input_registry()
        .lock()
        .ok()?
        .get(launch_id)
        .cloned()
}

/// The turn ended because Longhouse interrupted it: a cancellation, not a failure.
#[derive(Debug)]
struct CodexTurnInterrupted;

impl std::fmt::Display for CodexTurnInterrupted {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Codex turn interrupted from Longhouse")
    }
}

impl std::error::Error for CodexTurnInterrupted {}

/// Running Codex Console turns that can take a steer, by Longhouse run id.
/// Registered once `turn/start` returns the provider turn id and removed when
/// the turn loop exits, so a run the daemon recovered after a restart (no live
/// app-server connection) is correctly not steerable.
fn console_steer_registry() -> &'static Mutex<HashMap<String, mpsc::UnboundedSender<ConsoleControl>>>
{
    static REGISTRY: OnceLock<Mutex<HashMap<String, mpsc::UnboundedSender<ConsoleControl>>>> =
        OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(HashMap::new()))
}

struct ConsoleSteerRegistration(String);

impl Drop for ConsoleSteerRegistration {
    fn drop(&mut self) {
        if let Ok(mut registry) = console_steer_registry().lock() {
            registry.remove(&self.0);
        }
    }
}

/// Console turns that have begun but have no control channel yet: from the
/// moment the run is accepted until `turn/start` returns the provider turn id.
/// The user already sees such a turn running, so a Stop or steer that arrives in
/// this window waits for the channel instead of being told the turn is not
/// there.
fn console_starting_registry() -> &'static Mutex<HashSet<String>> {
    static REGISTRY: OnceLock<Mutex<HashSet<String>>> = OnceLock::new();
    REGISTRY.get_or_init(|| Mutex::new(HashSet::new()))
}

struct ConsoleStartingGuard(String);

impl ConsoleStartingGuard {
    fn new(run_id: &str) -> Self {
        if let Ok(mut starting) = console_starting_registry().lock() {
            starting.insert(run_id.to_string());
        }
        Self(run_id.to_string())
    }
}

impl Drop for ConsoleStartingGuard {
    fn drop(&mut self) {
        if let Ok(mut starting) = console_starting_registry().lock() {
            starting.remove(&self.0);
        }
    }
}

/// How often, and how many times, a Stop that Codex answers "no active turn" is
/// re-sent while the turn is still running on our side.
const CONSOLE_STOP_RETRY: Duration = Duration::from_millis(200);
const CONSOLE_STOP_ATTEMPTS: u32 = 25;

/// Longest a Stop or steer waits for a starting turn to register. The window is
/// the worker lease plus the `turn/start` round trip; past it the turn is not
/// coming up and the caller is told so.
const CONSOLE_START_GRACE: Duration = Duration::from_secs(8);

/// Enter the running Codex Console turn for `run_id` with `text`, through the
/// same app-server connection that started it (`turn/steer`, the Helm path).
/// `Err("turn_not_steerable")` means no turn of that run is running here.
pub async fn steer_codex_console_turn(run_id: &str, text: &str) -> std::result::Result<(), String> {
    console_control(run_id, |reply| ConsoleControl::Steer {
        text: text.to_string(),
        reply,
    })
    .await
}

/// Stop the running Codex Console turn for `run_id` (`turn/interrupt` on its
/// live connection); the run then settles as cancelled.
pub async fn interrupt_codex_console_turn(run_id: &str) -> std::result::Result<(), String> {
    // For a moment after `turn/start` acknowledges, Codex can still answer "no
    // active turn" to a Stop that is already registered, so a Stop sent right
    // then is retried briefly. If the turn ended in the meantime the Stop is
    // satisfied: a turn that is over is a turn that stopped.
    let mut refused_early = false;
    for _ in 0..CONSOLE_STOP_ATTEMPTS {
        match console_control(run_id, |reply| ConsoleControl::Interrupt { reply }).await {
            Err(reason) if reason.contains("no active turn") => refused_early = true,
            Err(reason) if refused_early && reason == "turn_not_steerable" => return Ok(()),
            other => return other,
        }
        tokio::time::sleep(CONSOLE_STOP_RETRY).await;
    }
    Err("turn_not_steerable".to_string())
}

async fn console_control(
    run_id: &str,
    control: impl FnOnce(ConsoleReply) -> ConsoleControl,
) -> std::result::Result<(), String> {
    console_control_within(run_id, CONSOLE_START_GRACE, control).await
}

/// The control channel of `run_id`'s running turn, waiting up to `grace` for one
/// that is still starting. A run that is neither registered nor starting has
/// ended (or never began) and is refused at once.
async fn console_sender(
    run_id: &str,
    grace: Duration,
) -> std::result::Result<mpsc::UnboundedSender<ConsoleControl>, String> {
    let deadline = std::time::Instant::now() + grace;
    loop {
        if let Some(sender) = console_steer_registry()
            .lock()
            .map_err(|_| "console control registry poisoned".to_string())?
            .get(run_id)
            .cloned()
        {
            return Ok(sender);
        }
        let starting = console_starting_registry()
            .lock()
            .map_err(|_| "console control registry poisoned".to_string())?
            .contains(run_id);
        if !starting || std::time::Instant::now() >= deadline {
            return Err("turn_not_steerable".to_string());
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

async fn console_control_within(
    run_id: &str,
    grace: Duration,
    control: impl FnOnce(ConsoleReply) -> ConsoleControl,
) -> std::result::Result<(), String> {
    let sender = console_sender(run_id, grace).await?;
    let (reply, outcome) = tokio::sync::oneshot::channel();
    sender
        .send(control(reply))
        .map_err(|_| "turn_not_steerable".to_string())?;
    match tokio::time::timeout(Duration::from_secs(15), outcome).await {
        Ok(Ok(result)) => result,
        Ok(Err(_)) => Err("turn_not_steerable".to_string()),
        Err(_) => Err("steer_outcome_unknown".to_string()),
    }
}

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
    /// When the in-flight prewarm reserved its slot. A turn waits on a spawn
    /// only while it is younger than the prewarm's own budget; an older one is
    /// a counter that was never released, and must not block every turn.
    spawn_started_at: Option<std::time::Instant>,
    active_process_groups: HashMap<u32, (i32, Option<Arc<str>>)>,
    shutting_down: bool,
    spawn_finished: Arc<tokio::sync::Notify>,
    active_finished: Arc<tokio::sync::Notify>,
}

impl Default for CodexConsoleWorkerPool {
    fn default() -> Self {
        Self {
            workers: Vec::new(),
            spawning: 0,
            spawn_started_at: None,
            active_process_groups: HashMap::new(),
            shutting_down: false,
            spawn_finished: Arc::new(tokio::sync::Notify::new()),
            active_finished: Arc::new(tokio::sync::Notify::new()),
        }
    }
}

impl CodexConsoleWorkerPool {
    fn reserve_spawn_slot(&mut self) -> bool {
        if self.shutting_down || self.workers.len() + self.spawning >= CONSOLE_WARM_POOL_TARGET {
            return false;
        }
        self.spawning += 1;
        self.spawn_started_at = Some(std::time::Instant::now());
        true
    }

    fn prewarm_in_flight(&self) -> bool {
        self.spawning > 0
            && self.spawn_started_at.is_none_or(|started| {
                started.elapsed() < PREWARM_INITIALIZE_BUDGET + Duration::from_secs(10)
            })
    }
}

static CODEX_CONSOLE_WORKER_POOL: OnceLock<tokio::sync::Mutex<CodexConsoleWorkerPool>> =
    OnceLock::new();

fn console_worker_pool() -> &'static tokio::sync::Mutex<CodexConsoleWorkerPool> {
    CODEX_CONSOLE_WORKER_POOL.get_or_init(Default::default)
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct CompletedCommandExecution {
    command: String,
    status: String,
    exit_code: Option<i32>,
    output: String,
}

#[derive(Default)]
struct AppServerProjection {
    item_text: BTreeMap<String, String>,
    item_seq: BTreeMap<String, u64>,
    tool_command: BTreeMap<String, String>,
    tool_output: BTreeMap<String, String>,
    tool_seq: BTreeMap<String, u64>,
    active_commands: BTreeMap<String, String>,
    completed_commands: BTreeMap<String, CompletedCommandExecution>,
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

/// The short tool token the activity axis displays, never the command itself.
///
/// `RuntimeEventIngest.tool_name` caps at 128 characters and the UI renders this
/// as a name ("Using shell"), so a shell command belongs in the tool item's
/// payload rather than here. The Helm bridge already maps these item types the
/// same way (`codex_bridge.rs::tracked_item_tool_name`); Console sending the raw
/// command was drift, and it 422'd every batch a long command rode in.
fn app_server_tool_token(item_type: &str, params: &Value) -> String {
    match item_type {
        "commandExecution" => "shell".to_string(),
        "fileChange" => "edit".to_string(),
        "mcpToolCall" | "dynamicToolCall" | "collabAgentToolCall" => {
            json_string(params, &["item", "tool"]).unwrap_or_else(|| item_type.to_string())
        }
        other => other.to_string(),
    }
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
                self.active_commands
                    .insert(item_id.clone(), command.clone());
                self.completed_commands.remove(&item_id);
                vec![
                    // `running` is the managed phase contract's name for tool
                    // execution (see config/managed_phase_contract.json), and the
                    // Helm bridge already speaks it. Console used to emit `tool`,
                    // which no server vocabulary knows: it carried no freshness
                    // window and projected to activity `unknown`, so a Console run
                    // went dark after its opening `thinking` fact expired.
                    //
                    // `tool_name` is a short token, not the command. The wire
                    // field caps at 128 chars (`RuntimeEventIngest.tool_name`),
                    // so sending the command 422'd the whole coalesced batch
                    // whenever it ran long — taking every event batched with it.
                    // The command still travels in the tool item's payload.
                    ProjectedAppServerEvent::Phase {
                        phase: "running",
                        tool_name: Some(app_server_tool_token("commandExecution", params)),
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
                if output.len() > 4 * 1024 {
                    let tail = bounded_output_tail(output);
                    *output = tail;
                }
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
                let exit_code = params
                    .get("item")
                    .and_then(|item| item.get("exitCode"))
                    .and_then(Value::as_i64)
                    .and_then(|value| i32::try_from(value).ok());
                self.active_commands.remove(&item_id);
                self.completed_commands.insert(
                    item_id.clone(),
                    CompletedCommandExecution {
                        command: command.clone(),
                        status: status.clone(),
                        exit_code,
                        output: bounded_output_tail(&output),
                    },
                );
                let seq = self.tool_seq.entry(item_id.clone()).or_default();
                *seq += 1;
                let seq = *seq;
                let projected = ProjectedAppServerEvent::ToolItem {
                    item_id: item_id.clone(),
                    command,
                    output,
                    status,
                    seq,
                    completed: true,
                };
                self.tool_command.remove(&item_id);
                self.tool_output.remove(&item_id);
                self.tool_seq.remove(&item_id);
                vec![projected]
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
                let Some(text) = text else {
                    self.item_text.remove(&item_id);
                    self.item_seq.remove(&item_id);
                    return Vec::new();
                };
                let item_seq = {
                    let item_seq = self.item_seq.entry(item_id.clone()).or_default();
                    *item_seq += 1;
                    *item_seq
                };
                self.transcript_seq += 1;
                let projected = ProjectedAppServerEvent::AssistantItem {
                    item_id: item_id.clone(),
                    item_seq,
                    seq: self.transcript_seq,
                    delta: String::new(),
                    text,
                    completed: true,
                };
                self.item_text.remove(&item_id);
                self.item_seq.remove(&item_id);
                vec![projected]
            }
            "turn/started" => vec![ProjectedAppServerEvent::Phase {
                phase: "thinking",
                tool_name: None,
            }],
            _ => Vec::new(),
        }
    }
}

fn bounded_output_tail(output: &str) -> String {
    bounded_output_tail_with_limits(output, 4 * 1024, 40)
}

fn bounded_output_tail_with_limits(output: &str, max_bytes: usize, max_lines: usize) -> String {
    if max_bytes == 0 || max_lines == 0 {
        return String::new();
    }
    let mut byte_start = output.len().saturating_sub(max_bytes);
    while !output.is_char_boundary(byte_start) {
        byte_start += 1;
    }
    let mut end = output.len();
    if end > byte_start && output.as_bytes()[end - 1] == b'\n' {
        end -= 1;
    }
    let tail = &output[byte_start..end];
    let mut line_start = 0;
    let mut lines = 0;
    for (index, byte) in tail.bytes().enumerate().rev() {
        if byte == b'\n' {
            lines += 1;
            if lines == max_lines {
                line_start = index + 1;
                break;
            }
        }
    }
    output[byte_start + line_start..].to_string()
}

fn pending_command_items(projection: &AppServerProjection, response: &Value) -> Vec<PendingItem> {
    let mut items = BTreeMap::new();
    for (id, command) in &projection.active_commands {
        items.insert(
            id.clone(),
            PendingItem {
                id: id.clone(),
                kind: "shell".to_string(),
                status: "running".to_string(),
                description: Some(command.clone()),
            },
        );
    }
    if let Some(terminals) = response.get("data").and_then(Value::as_array) {
        for terminal in terminals {
            let Some(id) = terminal.get("itemId").and_then(Value::as_str) else {
                continue;
            };
            let command = terminal
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or_default();
            items.insert(
                id.to_string(),
                PendingItem {
                    id: id.to_string(),
                    kind: "shell".to_string(),
                    status: "running".to_string(),
                    description: Some(command.to_string()),
                },
            );
        }
    }
    items.into_values().collect()
}

fn command_completion_summary(item: &CompletedCommandExecution) -> String {
    format!(
        "{} (exit code {})",
        item.command,
        item.exit_code
            .map(|code| code.to_string())
            .unwrap_or_else(|| "unknown".to_string())
    )
}

fn command_completion_input(items: &[(String, CompletedCommandExecution)]) -> String {
    let mut input = String::from(
        "Longhouse background-task completion (not typed by the user):\nLast output is bounded to 40 lines / 4 KB total.\n",
    );
    let mut output_bytes = 4 * 1024;
    let mut output_lines = 40;
    for (_, item) in items {
        let exit_code = item
            .exit_code
            .map(|code| code.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        input.push_str(&format!(
            "Command: {}\nExit code: {}\nStatus: {}\nOutput:\n",
            item.command, exit_code, item.status
        ));
        if item.output.is_empty() {
            input.push_str("(no output)\n");
            continue;
        }
        let output = bounded_output_tail_with_limits(&item.output, output_bytes, output_lines);
        if output.is_empty() {
            input.push_str("(omitted: shared output bound reached)\n");
            continue;
        }
        output_bytes = output_bytes.saturating_sub(output.len());
        output_lines = output_lines.saturating_sub(output.lines().count());
        input.push_str(&output);
        if !output.ends_with('\n') {
            input.push('\n');
        }
    }
    input
}

#[derive(Clone, Debug)]
pub struct CodexExecRunConfig {
    pub session_id: String,
    pub run_id: String,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub client_request_id: Option<String>,
    pub origin: String,
    pub wake_id: Option<String>,
    pub invocation_id: Option<String>,
    pub cwd: PathBuf,
    pub api_url: String,
    pub api_token: String,
    pub codex_bin: String,
    pub approval_policy: Option<String>,
    pub sandbox: Option<String>,
    pub model: Option<String>,
    pub prompt: String,
    /// Staged image files for this turn, sent as `localImage` items ahead of
    /// the prompt text (same shape the Helm bridge sends).
    pub image_paths: Vec<PathBuf>,
    pub launch_actor: Option<String>,
    pub launch_surface: Option<String>,
    pub resume_thread_id: Option<String>,
    /// Parent thread to fork from on this turn. Distinct from
    /// `resume_thread_id` because the two mean opposite things to the provider:
    /// a resume continues one thread, a fork produces a second one. Conflating
    /// them is how the first branching design ended up appending two
    /// conversations to a single rollout.
    pub fork_thread_id: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
    /// Where the completion wake goes. `None` is the socket the daemon
    /// listens on (`$LONGHOUSE_HOME/agent/transcript-wake.sock`); tests pass a
    /// private path so a wake cannot reach another test's listener.
    pub transcript_wake_socket: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CodexExecRunSummary {
    pub session_id: String,
    pub run_id: String,
    pub pid: Option<u32>,
    pub process_group_id: Option<i32>,
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
    local_db_path: Option<PathBuf>,
    transcript_wake_socket: Option<PathBuf>,
    event_tx: mpsc::Sender<Vec<Value>>,
    critical_event_tx: mpsc::Sender<Vec<Value>>,
    queued_events: Arc<AtomicUsize>,
}

pub fn codex_exec_args(config: &CodexExecRunConfig) -> Vec<OsString> {
    let mut args = vec![
        OsString::from("-c"),
        OsString::from(crate::codex_config::DISABLE_UPDATE_CHECK),
    ];
    if let Some(approval_policy) = normalized_optional(&config.approval_policy) {
        args.push(OsString::from("-c"));
        args.push(OsString::from(crate::codex_config::string_override(
            "approval_policy",
            &approval_policy,
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
        PREWARM_INITIALIZE_BUDGET,
    )
    .await;
    let mut pool = console_worker_pool().lock().await;
    pool.spawning = pool.spawning.saturating_sub(1);
    if pool.spawning == 0 {
        pool.spawn_started_at = None;
    }
    let spawn_finished = pool.spawn_finished.clone();
    let mut discard = None;
    match result {
        Ok(worker) if !pool.shutting_down && pool.workers.len() < CONSOLE_WARM_POOL_TARGET => {
            eprintln!(
                "[codex-exec] latency stage=warm_worker_ready pid={} pool_size={}",
                worker.pid.unwrap_or(0),
                pool.workers.len() + 1
            );
            pool.workers.push(worker);
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

enum WarmLease {
    Worker(InitializedCodexWorker),
    /// Nothing warm and nothing starting: a cold start is the only way.
    Cold,
    /// The prewarm is still starting past the turn's budget.
    StillStarting,
}

/// Lease the warm worker, waiting until `deadline` for one the prewarm is
/// still starting rather than missing it and cold-starting a second.
async fn lease_warm_worker_by(deadline: tokio::time::Instant) -> WarmLease {
    loop {
        if let Some(worker) = lease_warm_worker().await {
            return WarmLease::Worker(worker);
        }
        let wait = {
            let pool = console_worker_pool().lock().await;
            if !pool.workers.is_empty() {
                // The prewarm finished between the lease and this lock.
                continue;
            }
            if pool.shutting_down || !pool.prewarm_in_flight() {
                return WarmLease::Cold;
            }
            let mut wait = Box::pin(pool.spawn_finished.clone().notified_owned());
            wait.as_mut().enable();
            wait
        };
        if tokio::time::timeout_at(deadline, wait).await.is_err() {
            eprintln!("[codex-exec] latency stage=warm_worker_miss reason=prewarm_still_starting");
            return WarmLease::StillStarting;
        }
    }
}

async fn lease_warm_worker() -> Option<InitializedCodexWorker> {
    let mut pool = console_worker_pool().lock().await;
    if pool.shutting_down {
        return None;
    }
    while let Some(mut worker) = pool.workers.pop() {
        if warm_worker_is_alive(&mut worker) {
            if let (Some(pid), Some(pgid)) = (worker.pid, worker.pgid) {
                pool.active_process_groups.insert(pid, (pgid, None));
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

fn warm_worker_is_alive(worker: &mut InitializedCodexWorker) -> bool {
    worker.child.try_wait().ok().flatten().is_none()
}

async fn register_active_worker(worker: &InitializedCodexWorker, launch_id: &str) -> bool {
    let mut pool = console_worker_pool().lock().await;
    if pool.shutting_down {
        return false;
    }
    if let (Some(pid), Some(pgid)) = (worker.pid, worker.pgid) {
        pool.active_process_groups
            .insert(pid, (pgid, Some(Arc::from(launch_id))));
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
    initialize_budget: Duration,
) -> Result<InitializedCodexWorker> {
    let config = CodexExecRunConfig {
        session_id: session_id.unwrap_or("warm-anonymous").to_string(),
        run_id: "warm-anonymous".to_string(),
        thread_id: None,
        turn_id: None,
        client_request_id: None,
        origin: "user".to_string(),
        wake_id: None,
        invocation_id: None,
        cwd: process_cwd.to_path_buf(),
        api_url: String::new(),
        api_token: String::new(),
        codex_bin: codex_bin.to_string(),
        approval_policy: approval_policy.map(str::to_string),
        sandbox: sandbox.map(str::to_string),
        model: None,
        prompt: String::new(),
        image_paths: Vec::new(),
        launch_actor: launch_actor.map(str::to_string),
        launch_surface: launch_surface.map(str::to_string),
        resume_thread_id: None,
        fork_thread_id: None,
        machine_name: String::new(),
        local_db_path: None,
        transcript_wake_socket: None,
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
    // Actor and surface are in NEVER_INHERITED_KEYS, so they go through the
    // overlay rather than after it. Setting them afterwards worked only because
    // the scrub happens to run first, which is the ordering hazard the `owned`
    // parameter exists to remove.
    let mut owned: Vec<(&str, &str)> = Vec::new();
    if let Some(actor) = launch_actor {
        owned.push(("LONGHOUSE_LAUNCH_ACTOR", actor));
    }
    if let Some(surface) = launch_surface {
        owned.push(("LONGHOUSE_LAUNCH_SURFACE", surface));
    }
    if let Some(session_id) = session_id {
        ManagedIdentity::new(ManagedProvider::Codex, session_id).apply(&mut command, &owned);
    } else {
        // An anonymous worker is meant to carry no session. Scrubbing is what
        // makes that true: without it the worker carries whichever managed
        // session happened to spawn it.
        ManagedIdentity::scrub(&mut command);
        for (key, value) in &owned {
            command.env(key, value);
        }
        command.env("LONGHOUSE_CONSOLE_WORKER", "1");
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
    let stdin = child
        .stdin
        .take()
        .context("Codex worker stdin unavailable")?;
    let stdout = child
        .stdout
        .take()
        .context("Codex worker stdout unavailable")?;
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
    let initialize_result = tokio::time::timeout(initialize_budget, async {
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

/// Get an initialized worker for a turn by `deadline`, the turn's one start
/// budget. A user is waiting: an in-flight prewarm gets the budget, and a cold
/// start happens only when nothing is starting. A second `codex` started
/// beside a slow prewarm competes with it for the same disk and fails the same
/// way, so say what is happening instead. A cold start after the prewarm failed
/// gets only what the wait left, so the turn-start reply still reaches the
/// Runtime Host inside its 10 s (`CONSOLE_CONTROL_REPLY_TIMEOUT_SECONDS`).
async fn start_console_worker_by(
    config: &CodexExecRunConfig,
    warm_compatible: bool,
    deadline: tokio::time::Instant,
) -> Result<(InitializedCodexWorker, bool)> {
    let lease = if warm_compatible {
        lease_warm_worker_by(deadline).await
    } else {
        WarmLease::Cold
    };
    match lease {
        WarmLease::Worker(worker) => Ok((worker, true)),
        WarmLease::StillStarting => anyhow::bail!(
            "Codex is still starting on this machine; its first start can take up to a minute while the machine is busy. Send again in a minute."
        ),
        WarmLease::Cold => {
            let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
            if remaining.is_zero() {
                anyhow::bail!("Codex could not start on this machine in time. Send again.");
            }
            let worker = spawn_initialized_codex_worker(
                &config.codex_bin,
                normalized_optional(&config.approval_policy).as_deref(),
                normalized_optional(&config.sandbox).as_deref(),
                &config.cwd,
                Some(&config.session_id),
                normalized_optional(&config.launch_actor).as_deref(),
                normalized_optional(&config.launch_surface).as_deref(),
                remaining,
            )
            .await?;
            Ok((worker, false))
        }
    }
}

struct CodexRuntimeTurn {
    sink: CodexExecRuntimeSink,
    event_pump: tokio::task::JoinHandle<()>,
}

fn start_runtime_turn(config: &CodexExecRunConfig) -> Result<CodexRuntimeTurn> {
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
        local_db_path: config.local_db_path.clone(),
        transcript_wake_socket: config.transcript_wake_socket.clone(),
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
        crate::config::get_agent_runtime_events_outbox_dir()?,
    ));
    Ok(CodexRuntimeTurn { sink, event_pump })
}

async fn finish_runtime_turn(runtime: CodexRuntimeTurn, session_id: &str, run_id: &str) {
    let CodexRuntimeTurn {
        sink,
        mut event_pump,
    } = runtime;
    drop(sink);
    match tokio::time::timeout(EVENT_PUMP_DRAIN_BUDGET, &mut event_pump).await {
        Ok(Ok(())) => {}
        Ok(Err(err)) => eprintln!("[codex-exec] runtime event pump join failed: {err}"),
        Err(_) => {
            event_pump.abort();
            eprintln!(
                "[codex-exec] runtime event pump drain timed out session={session_id} run={run_id}"
            );
        }
    }
}

fn turn_binding(config: &CodexExecRunConfig) -> TurnBinding {
    TurnBinding {
        run_id: config.run_id.clone(),
        turn_id: config.turn_id.clone(),
        client_request_id: config.client_request_id.clone(),
        origin: if config.origin == "wake" {
            TurnOrigin::Wake
        } else {
            TurnOrigin::User
        },
    }
}

fn record_codex_run(
    config: &CodexExecRunConfig,
    launch_id: &str,
    pid: u32,
    process_group_id: i32,
    provider_thread_id: &str,
    argv: &[String],
    adopted_parked_invocation: bool,
) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    registry.mark_spawned_invocation(
        &config.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        CODEX_EXEC_ADAPTER,
        launch_id,
        Some(provider_thread_id),
        "",
        "",
        json!({"argv": argv}),
    )?;
    registry.record_invocation_turn(&config.run_id, &config.origin, adopted_parked_invocation)?;
    Ok(())
}

async fn complete_codex_idle(
    invocation: &ConsoleInvocation,
    sink: &CodexExecRuntimeSink,
    outcome: IdleOutcome,
) {
    let _ = invocation.persist_pending_claim();
    if outcome.has_active_turn {
        sink.post_terminal(
            &outcome.signal.terminal_state,
            outcome.signal.exit_code,
            outcome.signal.stderr,
            &invocation.launch_id,
            outcome.invocation_state,
            outcome.pending_count,
        )
        .await;
    }
    if outcome.invocation_state == InvocationState::Parked {
        if let Some((binding, snapshot)) = invocation.delegation_snapshot() {
            if binding.run_id == sink.run_id {
                sink.post_delegation_snapshot(&invocation.launch_id, snapshot)
                    .await;
            }
        }
    }
    if outcome.invocation_state == InvocationState::Closed {
        let _ = invocation.close_input().await;
    }
}

pub async fn close_parked_codex_invocation(
    claim: &crate::turn_claims::TurnClaim,
    invocation: Arc<ConsoleInvocation>,
    machine_name: &str,
) -> Result<Option<crate::console_lifecycle::InvocationCloseOutcome>> {
    let outcome = crate::console_lifecycle::close_parked_invocation(
        &invocation,
        claim,
        machine_name,
        CODEX_EXEC_RUNTIME_SOURCE,
        crate::config::get_agent_runtime_events_outbox_dir(),
    )
    .await?;
    if let Some(outcome) = &outcome {
        if let Ok(mut inputs) = codex_console_input_registry().lock() {
            inputs.remove(&outcome.invocation_id);
        }
    }
    Ok(outcome)
}
pub async fn start_codex_exec_once(config: CodexExecRunConfig) -> Result<CodexExecRunSummary> {
    if config.origin == "wake" {
        let Some(invocation_id) = config.invocation_id.as_deref() else {
            return cancel_missing_codex_wake(config).await;
        };
        let Some(invocation) = crate::console_lifecycle::lookup_launch(invocation_id) else {
            return cancel_missing_codex_wake(config).await;
        };
        return bind_codex_wake_turn(config, invocation).await;
    }
    if config.origin == "user" && config.fork_thread_id.is_none() {
        if let Some(provider_thread_id) = config.resume_thread_id.as_deref() {
            if let Some(invocation) = crate::console_lifecycle::lookup("codex", provider_thread_id)
            {
                match invocation.state() {
                    InvocationState::Parked => {
                        return adopt_parked_codex_turn(config, invocation).await;
                    }
                    InvocationState::Responding => {
                        let Some(wake_id) = invocation.pending_wake_id() else {
                            bail!("Codex Console invocation already has an active turn");
                        };
                        let mut config = config;
                        config.wake_id = Some(wake_id);
                        return bind_codex_wake_turn(config, invocation).await;
                    }
                    InvocationState::Closed => {
                        invocation.wait_stopped().await;
                        crate::console_lifecycle::unregister(&invocation.launch_id);
                    }
                }
            }
        }
    }
    start_new_codex_exec(config).await
}

async fn start_new_codex_exec(config: CodexExecRunConfig) -> Result<CodexExecRunSummary> {
    let starting = ConsoleStartingGuard::new(&config.run_id);
    let warm_compatible = warm_pool_compatible(&config);
    let (mut worker, warm_hit) = start_console_worker_by(
        &config,
        warm_compatible,
        tokio::time::Instant::now() + TURN_INITIALIZE_BUDGET,
    )
    .await?;
    let launch_id = uuid::Uuid::new_v4().to_string();
    if !register_active_worker(&worker, &launch_id).await {
        shutdown_worker_process_group(&mut worker.child, worker.pgid).await?;
        unregister_active_worker(worker.pid).await;
        anyhow::bail!("Codex Console worker rejected because the Machine Agent is shutting down");
    }
    let pid = worker.pid;
    let process_group_id = worker.pgid;
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
    let runtime = match start_runtime_turn(&config) {
        Ok(runtime) => runtime,
        Err(error) => {
            shutdown_worker_process_group(&mut worker.child, worker.pgid).await?;
            unregister_active_worker(worker.pid).await;
            return Err(error);
        }
    };
    let task_config = config.clone();
    tokio::spawn(async move {
        let mut worker = worker;
        if let Err(error) = run_codex_invocation(
            &mut worker,
            task_config.clone(),
            launch_id.clone(),
            runtime,
            warm_hit,
            leased_at,
            starting,
        )
        .await
        {
            tracing::error!(run_id = %task_config.run_id, %error, "Codex Console invocation failed");
        }
        let shutdown = crate::process_group::shutdown_owned_child(
            &mut worker.child,
            worker.pgid,
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        if !shutdown.is_gone() {
            retain_surviving_codex_invocation(&launch_id);
            tracing::warn!(
                pid = worker.pid.unwrap_or_default(),
                "Codex Console worker shutdown remains unverified"
            );
        }
        unregister_active_worker(worker.pid).await;
        crate::console_lifecycle::unregister(&launch_id);
        if let Ok(mut inputs) = codex_console_input_registry().lock() {
            inputs.remove(&launch_id);
        }
        if let Some(task) = worker.stderr_task {
            let _ = task.await;
        }
    });

    Ok(CodexExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid,
        process_group_id,
        argv,
    })
}

async fn adopt_parked_codex_turn(
    config: CodexExecRunConfig,
    invocation: Arc<ConsoleInvocation>,
) -> Result<CodexExecRunSummary> {
    let input = codex_console_input(&invocation.launch_id)
        .context("parked Codex invocation has no app-server input channel")?;
    let previous =
        crate::turn_claims::default_registry()?.read(&invocation.latest_turn().run_id)?;
    let argv = claim_argv(&previous);
    let (pid, process_group_id) = invocation.process_identity();
    record_codex_run(
        &config,
        &invocation.launch_id,
        pid,
        process_group_id,
        &invocation.provider_thread_id,
        &argv,
        true,
    )?;
    let starting = ConsoleStartingGuard::new(&config.run_id);
    input.set_next_turn(config.clone()).await;
    invocation
        .send_user_input(turn_binding(&config), &config.prompt, &config.image_paths)
        .await?;
    drop(starting);
    Ok(CodexExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid: Some(pid),
        process_group_id: Some(process_group_id),
        argv,
    })
}

async fn bind_codex_wake_turn(
    mut config: CodexExecRunConfig,
    invocation: Arc<ConsoleInvocation>,
) -> Result<CodexExecRunSummary> {
    let wake_id = config
        .wake_id
        .as_deref()
        .context("Codex wake turn omitted wake_id")?;
    let is_user_turn = config.origin == "user";
    let input = codex_console_input(&invocation.launch_id)
        .context("parked Codex invocation has no app-server input channel")?;
    if invocation.pending_wake_id().as_deref() != Some(wake_id) {
        return if is_user_turn {
            retry_codex_user_after_wake_miss(config, invocation).await
        } else {
            cancel_missing_codex_wake(config).await
        };
    }
    let wake_prompt = if is_user_turn {
        None
    } else {
        let Some(prompt) = input.wake_input_prompt(wake_id, Instant::now()).await else {
            return cancel_missing_codex_wake(config).await;
        };
        Some(prompt)
    };
    let previous =
        crate::turn_claims::default_registry()?.read(&invocation.latest_turn().run_id)?;
    let argv = claim_argv(&previous);
    let (pid, process_group_id) = invocation.process_identity();
    let starting = ConsoleStartingGuard::new(&config.run_id);
    let claims = crate::turn_claims::default_registry()?;
    let binding = turn_binding(&config);
    match invocation.bind_wake(&invocation.launch_id, wake_id, binding, || {
        claims.mark_spawned_invocation(
            &config.run_id,
            pid,
            process_group_id,
            crate::turn_claims::process_start_time_for_pid(Some(pid)),
            CODEX_EXEC_ADAPTER,
            &invocation.launch_id,
            Some(&invocation.provider_thread_id),
            "",
            "",
            json!({"argv": argv}),
        )?;
        claims.record_invocation_turn(&config.run_id, &config.origin, true)?;
        Ok(())
    }) {
        Ok(_) => {}
        Err(error) if error.is::<crate::console_lifecycle::WakeTargetGone>() => {
            return if is_user_turn {
                retry_codex_user_after_wake_miss(config, invocation).await
            } else {
                cancel_missing_codex_wake(config).await
            };
        }
        Err(error) => return Err(error),
    }
    if let Some(prompt) = wake_prompt {
        config.prompt = prompt;
    }
    input.discard_wake_input(wake_id).await;
    input.set_next_turn(config.clone()).await;
    invocation
        .write_input(&config.prompt, &config.image_paths)
        .await?;
    drop(starting);
    Ok(CodexExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid: Some(pid),
        process_group_id: Some(process_group_id),
        argv,
    })
}

async fn retry_codex_user_after_wake_miss(
    config: CodexExecRunConfig,
    invocation: Arc<ConsoleInvocation>,
) -> Result<CodexExecRunSummary> {
    match invocation.state() {
        InvocationState::Parked => adopt_parked_codex_turn(config, invocation).await,
        InvocationState::Closed => {
            invocation.wait_stopped().await;
            crate::console_lifecycle::unregister(&invocation.launch_id);
            start_new_codex_exec(config).await
        }
        InvocationState::Responding => {
            bail!("Codex Console invocation began another turn before the user turn was bound")
        }
    }
}

async fn cancel_missing_codex_wake(config: CodexExecRunConfig) -> Result<CodexExecRunSummary> {
    let runtime = start_runtime_turn(&config)?;
    let invocation_id = config
        .invocation_id
        .as_deref()
        .unwrap_or("missing-invocation");
    runtime
        .sink
        .post_terminal(
            "run_cancelled",
            None,
            Some("wake_target_gone".to_string()),
            invocation_id,
            InvocationState::Closed,
            0,
        )
        .await;
    finish_runtime_turn(runtime, &config.session_id, &config.run_id).await;
    Ok(CodexExecRunSummary {
        session_id: config.session_id,
        run_id: config.run_id,
        pid: None,
        process_group_id: None,
        argv: Vec::new(),
    })
}

fn claim_argv(claim: &crate::turn_claims::TurnClaim) -> Vec<String> {
    claim
        .result
        .as_ref()
        .and_then(|result| result.get("argv"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect()
}

/// Reconcile Codex Console claims left behind by an engine restart.
///
/// Codex app-server has no stdout file to replay or stdio connection to adopt.
/// Recovery therefore kills only a process group whose recorded boot, PID,
/// start time, and group identity still match, then closes the invocation.
/// Unknown process identity is left untouched; silence is not completion.
pub async fn recover_codex_exec_turns(
    machine_name: &str,
    _local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let outbox_dir = crate::config::get_agent_runtime_events_outbox_dir()?;
    let Some(process_facts) = crate::process_identity::try_collect_process_facts_by_pid() else {
        tracing::warn!("Process inventory unavailable; leaving Codex Console claims untouched");
        return Ok(0);
    };
    let stopped =
        recover_live_codex_exec_claims(&registry, &outbox_dir, machine_name, &process_facts)
            .await?;
    let reconciled = reconcile_codex_exec_claims(
        &registry,
        &outbox_dir,
        machine_name,
        crate::process_identity::try_collect_process_facts_by_pid(),
    )?;
    Ok(stopped + reconciled)
}

#[derive(Debug, PartialEq, Eq)]
enum CodexExecProcessIdentity {
    Alive,
    Gone(&'static str),
    Unknown(&'static str),
}

fn codex_recovery_claims(
    registry: &crate::turn_claims::TurnClaimRegistry,
) -> Result<Vec<crate::turn_claims::TurnClaim>> {
    let mut seen_invocations = HashSet::new();
    let mut recoverable = Vec::new();
    for claim in registry.list_all()?.into_iter().rev() {
        if claim.provider != "codex" || claim.adapter.as_deref() != Some(CODEX_EXEC_ADAPTER) {
            continue;
        }
        let invocation_key = claim
            .launch_id
            .as_deref()
            .unwrap_or(&claim.run_id)
            .to_string();
        if !seen_invocations.insert(invocation_key) {
            continue;
        }
        let invocation_state = claim.invocation_state.as_deref();
        if invocation_state == Some("closed")
            || (claim.state != "spawned" && invocation_state != Some("parked"))
        {
            continue;
        }
        recoverable.push(claim);
    }
    Ok(recoverable)
}

async fn recover_live_codex_exec_claims(
    registry: &crate::turn_claims::TurnClaimRegistry,
    outbox_dir: &Path,
    machine_name: &str,
    process_facts: &std::collections::HashMap<u32, crate::process_identity::ProcessFact>,
) -> Result<usize> {
    let mut recovered = 0;
    for claim in codex_recovery_claims(registry)? {
        if codex_exec_process_identity(&claim, process_facts) != CodexExecProcessIdentity::Alive {
            continue;
        }
        let Some(pid) = claim.pid else {
            continue;
        };
        let Some(process_group_id) = claim.process_group_id else {
            tracing::warn!(
                run_id = %claim.run_id,
                "Leaving live orphaned Codex Console worker without a recorded process group"
            );
            continue;
        };
        let group_identity_matches = claim.process_group_is_from_this_boot()
            && process_group_id > 0
            && pid == process_group_id as u32
            && claim.owned_processes.iter().any(|owned| {
                owned.pid == pid
                    && owned.process_group_id == process_group_id
                    && owned.process_start_time.as_deref() == claim.process_start_time.as_deref()
            })
            && crate::process_group::group_is_alive(process_group_id)
            && codex_process_is_group_leader(pid, process_group_id);
        if !group_identity_matches {
            tracing::warn!(
                run_id = %claim.run_id,
                pid,
                process_group_id,
                "Leaving live Codex Console worker whose exact process-group identity is unverified"
            );
            continue;
        }
        let outcome = crate::process_group::shutdown_group(
            process_group_id,
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        if !outcome.is_gone() {
            tracing::error!(
                run_id = %claim.run_id,
                process_group_id,
                outcome = outcome.as_str(),
                "Codex Console worker survived Machine Agent recovery shutdown"
            );
            continue;
        }
        let detail =
            "Codex Console closed during Machine Agent restart; app-server stdio cannot be reattached";
        if settle_codex_restart_claim(registry, outbox_dir, machine_name, &claim, detail) {
            recovered += 1;
        }
    }
    Ok(recovered)
}

fn codex_process_is_group_leader(pid: u32, process_group_id: i32) -> bool {
    crate::process_group::leader_group_for(pid) == Some(process_group_id)
}

fn settle_codex_restart_claim(
    registry: &crate::turn_claims::TurnClaimRegistry,
    outbox_dir: &Path,
    machine_name: &str,
    claim: &crate::turn_claims::TurnClaim,
    detail: &str,
) -> bool {
    let result = (|| -> Result<bool> {
        let claim = registry.read(&claim.run_id)?;
        if claim.invocation_state.as_deref() == Some("closed") {
            return Ok(false);
        }
        if matches!(claim.state.as_str(), "terminal" | "failed") {
            // Response completion and invocation closure are independent facts.
            // The response stays immutable. Retain the separate closing record so
            // the daemon can retry it even if this startup recovery runs only once.
            let mut stopped = crate::console_lifecycle::stopped_items_for_claim(&claim);
            if stopped.is_empty() {
                let invocation_id = claim.launch_id.as_deref().unwrap_or(&claim.run_id);
                stopped.push(PendingItem {
                    id: invocation_id.to_string(),
                    kind: "invocation".to_string(),
                    status: "stopped".to_string(),
                    description: Some("Codex app-server invocation".to_string()),
                });
            }
            match crate::console_lifecycle::publish_invocation_closed(
                registry,
                outbox_dir,
                &claim,
                machine_name,
                CODEX_EXEC_RUNTIME_SOURCE,
                InvocationCloseReason::MachineAgentRestart,
                &stopped,
            ) {
                Ok(handed_off) => return Ok(handed_off),
                Err(error) => {
                    tracing::warn!(%error, run_id = %claim.run_id, "Codex invocation close remains retryable");
                    return Ok(false);
                }
            }
        }
        let event =
            codex_exec_recovery_terminal_event(&claim, machine_name, "run_cancelled", detail);
        match crate::outbox::retain_and_enqueue_terminal_event(
            registry,
            outbox_dir,
            &claim.run_id,
            "run_cancelled",
            Some(detail.to_string()),
            event,
        ) {
            Ok((_, safe_to_retire)) => Ok(safe_to_retire),
            Err(error) => {
                tracing::warn!(
                    %error,
                    run_id = %claim.run_id,
                    "Failed to retain recovered Codex Console terminal event"
                );
                Ok(false)
            }
        }
    })();
    match result {
        Ok(settled) => settled,
        Err(error) => {
            tracing::warn!(%error, run_id = %claim.run_id, "Codex restart claim remains retryable");
            false
        }
    }
}

fn reconcile_codex_exec_claims(
    registry: &crate::turn_claims::TurnClaimRegistry,
    outbox_dir: &Path,
    machine_name: &str,
    process_facts: Option<std::collections::HashMap<u32, crate::process_identity::ProcessFact>>,
) -> Result<usize> {
    let claims = codex_recovery_claims(registry)?;
    // One coherent inventory for the whole pass. `try_collect_process_fact` per
    // claim cannot tell an absent pid from a `ps` that failed to run or parse,
    // and this is a caller reconciling durable state -- exactly what
    // `try_collect_process_facts_by_pid`'s own doc comment warns about. Getting
    // that wrong terminates live turns, and a terminal is an actuator: it
    // settles the console FIFO and dispatches the next queued turn, so a false
    // "gone" runs turn N+1 concurrently with a turn N that never stopped.
    let Some(process_facts) = process_facts else {
        tracing::warn!(
            "Process inventory unavailable; leaving Codex Console turn claims for a later scan"
        );
        return Ok(0);
    };
    let mut recovered = 0;
    for claim in claims {
        match codex_exec_process_identity(&claim, &process_facts) {
            CodexExecProcessIdentity::Alive => {
                tracing::warn!(
                    run_id = %claim.run_id,
                    pid = claim.pid.unwrap_or_default(),
                    "Leaving live orphaned Codex Console turn claim for a future recovery scan"
                );
            }
            CodexExecProcessIdentity::Unknown(reason) => {
                tracing::warn!(
                    run_id = %claim.run_id,
                    pid = claim.pid.unwrap_or_default(),
                    reason,
                    "Could not establish Codex Console process identity; leaving turn claim active"
                );
            }
            CodexExecProcessIdentity::Gone(reason) => {
                let detail =
                    format!("Codex Console worker stopped during Machine Agent restart ({reason})");
                if settle_codex_restart_claim(registry, outbox_dir, machine_name, &claim, &detail) {
                    recovered += 1;
                }
            }
        }
    }
    Ok(recovered)
}

fn codex_exec_process_identity(
    claim: &crate::turn_claims::TurnClaim,
    process_facts: &std::collections::HashMap<u32, crate::process_identity::ProcessFact>,
) -> CodexExecProcessIdentity {
    let Some(pid) = claim.pid else {
        return CodexExecProcessIdentity::Unknown("claim has no recorded pid");
    };
    let Some(fact) = process_facts.get(&pid) else {
        return CodexExecProcessIdentity::Gone("recorded pid is no longer running");
    };
    let Some(recorded_boot_id) = claim.boot_id.as_deref() else {
        return CodexExecProcessIdentity::Unknown("claim has no recorded boot id");
    };
    let Some(current_boot_id) = crate::heartbeat::machine_boot_id() else {
        return CodexExecProcessIdentity::Unknown("current machine boot id is unavailable");
    };
    if recorded_boot_id != current_boot_id {
        return CodexExecProcessIdentity::Gone("machine boot id changed");
    }
    let Some(recorded_start_raw) = claim.process_start_time.as_deref() else {
        return CodexExecProcessIdentity::Unknown("claim has no recorded process start time");
    };
    let Some(recorded_start) = crate::process_identity::parse_lstart(recorded_start_raw) else {
        return CodexExecProcessIdentity::Unknown("recorded process start time is invalid");
    };
    if !crate::process_identity::command_contains_basename(&fact.command, "codex") {
        return CodexExecProcessIdentity::Gone("pid now names a non-Codex process");
    }
    if !crate::process_identity::started_before_or_near_recorded(&fact, Some(recorded_start)) {
        return CodexExecProcessIdentity::Gone("process start time is newer than the claim");
    }
    if fact.lstart.trim() != recorded_start_raw.trim() {
        return CodexExecProcessIdentity::Gone("process start time changed");
    }
    CodexExecProcessIdentity::Alive
}

fn codex_exec_recovery_terminal_event(
    claim: &crate::turn_claims::TurnClaim,
    machine_name: &str,
    terminal_state: &str,
    detail: &str,
) -> Value {
    json!({
        "runtime_key": format!("codex:{}", claim.session_id),
        "session_id": claim.session_id,
        "run_id": claim.run_id,
        "thread_id": claim.thread_id,
        "provider": "codex",
        "device_id": machine_name,
        "source": CODEX_EXEC_RUNTIME_SOURCE,
        "kind": "terminal_signal",
        "phase": Value::Null,
        "tool_name": Value::Null,
        "occurred_at": Utc::now().to_rfc3339(),
        "dedupe_key": format!("codex-exec:{}:{}:terminal", claim.session_id, claim.run_id),
        "payload": {
            "managed_transport": CODEX_EXEC_RUNTIME_SOURCE,
            "execution_lifetime": "persistent",
            "terminal_state": terminal_state,
            "terminal_reason": "machine_agent_restart",
            "terminal_source": CODEX_EXEC_RUNTIME_SOURCE,
            "exit_code": Value::Null,
            "stderr_tail": detail,
            "turn_id": claim.turn_id,
            "client_request_id": claim.client_request_id,
            "invocation": {
                "id": claim.launch_id,
                "state": "closed",
                "pending_count": claim.pending_count,
            },
        }
    })
}

/// Which app-server method starts this turn.
///
/// Fork outranks resume: a turn carrying a fork parent is a branch's first
/// turn, and it must produce a second thread rather than continue the first.
fn app_server_thread_method(
    fork_thread_id: Option<&str>,
    resume_thread_id: Option<&str>,
) -> &'static str {
    match (fork_thread_id, resume_thread_id) {
        (Some(_), _) => "thread/fork",
        (None, Some(_)) => "thread/resume",
        (None, None) => "thread/start",
    }
}

/// Refuse a branch whose fork silently behaved like a resume.
///
/// If an upstream change ever degrades `thread/fork`, the child would adopt the
/// parent's thread, two Longhouse sessions would claim one rollout, and the
/// alias uniqueness index would reject the second binding with nothing louder
/// than a logged warning. Fail the run instead: a failed branch is recoverable,
/// a silently merged one is not.
fn ensure_fork_produced_a_new_thread(
    fork_thread_id: Option<&str>,
    provider_thread_id: &str,
) -> Result<()> {
    if let Some(parent_thread_id) = fork_thread_id {
        if provider_thread_id == parent_thread_id {
            bail!(
                "Codex thread/fork returned the parent thread {parent_thread_id}; refusing to run a branch that would share the parent's rollout"
            );
        }
    }
    Ok(())
}

async fn start_app_server_thread(
    rpc: &mut AppServerRpc,
    sink: &CodexExecRuntimeSink,
    projection: &mut AppServerProjection,
    config: &CodexExecRunConfig,
) -> Result<(String, Option<String>)> {
    let method = app_server_thread_method(
        config.fork_thread_id.as_deref(),
        config.resume_thread_id.as_deref(),
    );
    let mut thread_params = json!({
        "cwd": config.cwd.to_string_lossy(),
        "approvalPolicy": normalized_optional(&config.approval_policy),
        "sandbox": normalized_optional(&config.sandbox),
    });
    if let Some(thread_id) = config
        .fork_thread_id
        .as_deref()
        .or(config.resume_thread_id.as_deref())
    {
        thread_params["threadId"] = Value::String(thread_id.to_string());
    }
    let response = rpc.request(method, thread_params, sink, projection).await?;
    let provider_thread_id = json_string(&response, &["thread", "id"])
        .context("Codex app-server thread response omitted thread.id")?;
    ensure_fork_produced_a_new_thread(config.fork_thread_id.as_deref(), &provider_thread_id)?;
    let thread_path = json_string(&response, &["thread", "path"])
        .or_else(|| codex_rollout_path(&provider_thread_id).map(|path| path.display().to_string()));
    Ok((provider_thread_id, thread_path))
}

async fn run_codex_invocation(
    worker: &mut InitializedCodexWorker,
    config: CodexExecRunConfig,
    launch_id: String,
    mut runtime: CodexRuntimeTurn,
    warm_hit: bool,
    leased_at: std::time::Instant,
    starting: ConsoleStartingGuard,
) -> Result<()> {
    let mut projection = AppServerProjection::default();
    let (provider_thread_id, thread_path) =
        match start_app_server_thread(&mut worker.rpc, &runtime.sink, &mut projection, &config)
            .await
        {
            Ok(binding) => binding,
            Err(error) => {
                runtime
                    .sink
                    .post_terminal(
                        "run_failed",
                        None,
                        Some(error.to_string()),
                        &launch_id,
                        InvocationState::Closed,
                        0,
                    )
                    .await;
                finish_runtime_turn(runtime, &config.session_id, &config.run_id).await;
                return Err(error);
            }
        };
    let pid = worker
        .pid
        .context("Codex app-server worker has no process id")?;
    let process_group_id = worker
        .pgid
        .context("Codex app-server worker has no process-group id")?;
    let (control_tx, mut control_rx) = mpsc::unbounded_channel();
    let input = Arc::new(CodexConsoleInput {
        sender: control_tx,
        next_turn: AsyncMutex::new(None),
        wake_inputs: AsyncMutex::new(HashMap::new()),
    });
    let invocation = Arc::new(ConsoleInvocation::new(
        "codex",
        provider_thread_id.clone(),
        launch_id.clone(),
        pid,
        process_group_id,
        turn_binding(&config),
        input.clone(),
    ));
    if let Err(error) = crate::console_lifecycle::register(invocation.clone()) {
        runtime
            .sink
            .post_terminal(
                "run_failed",
                None,
                Some(error.to_string()),
                &launch_id,
                InvocationState::Closed,
                0,
            )
            .await;
        finish_runtime_turn(runtime, &config.session_id, &config.run_id).await;
        return Err(error);
    }
    if let Err(error) = record_codex_run(
        &config,
        &launch_id,
        pid,
        process_group_id,
        &provider_thread_id,
        &worker.argv,
        false,
    ) {
        crate::console_lifecycle::unregister(&launch_id);
        runtime
            .sink
            .post_terminal(
                "run_failed",
                None,
                Some(error.to_string()),
                &launch_id,
                InvocationState::Closed,
                0,
            )
            .await;
        finish_runtime_turn(runtime, &config.session_id, &config.run_id).await;
        return Err(error);
    }
    codex_console_input_registry()
        .lock()
        .map_err(|_| anyhow::anyhow!("Codex input registry poisoned"))?
        .insert(launch_id.clone(), input.clone());

    let mut current_config = config;
    let mut current_warm_hit = warm_hit;
    let mut current_leased_at = leased_at;
    let mut next_start_reply = None;
    let mut next_starting = Some(starting);
    'turns: loop {
        let turn_result = run_app_server_turn(
            &mut worker.rpc,
            &provider_thread_id,
            thread_path.as_deref(),
            &runtime.sink,
            &current_config.prompt,
            &current_config.image_paths,
            normalized_optional(&current_config.model).as_deref(),
            &mut projection,
            current_warm_hit,
            current_leased_at,
            next_start_reply.take(),
            next_starting.take(),
        )
        .await;
        if let Err(error) = turn_result {
            let interrupted = error
                .chain()
                .any(|cause| cause.is::<CodexTurnInterrupted>());
            let terminal_state = if interrupted {
                "run_cancelled"
            } else {
                "run_failed"
            };
            let pending_count = invocation.pending_count();
            invocation.take_active_turn();
            runtime
                .sink
                .post_terminal(
                    terminal_state,
                    None,
                    (!interrupted).then(|| error.to_string()),
                    &launch_id,
                    InvocationState::Closed,
                    pending_count,
                )
                .await;
            finish_runtime_turn(runtime, &current_config.session_id, &current_config.run_id).await;
            invocation.process_exited();
            if let Ok(registry) = crate::turn_claims::default_registry() {
                let _ = registry.record_invocation_state(
                    &current_config.run_id,
                    "closed",
                    pending_count,
                );
            }
            crate::console_lifecycle::unregister(&launch_id);
            return Ok(());
        }

        let pending_at_idle = projection
            .active_commands
            .iter()
            .map(|(id, command)| PendingItem {
                id: id.clone(),
                kind: "shell".to_string(),
                status: "running".to_string(),
                description: Some(command.clone()),
            })
            .collect::<Vec<_>>();
        let terminal_response = worker
            .rpc
            .request_quiet(
                "thread/backgroundTerminals/list",
                json!({"threadId": provider_thread_id}),
                &mut projection,
            )
            .await
            .unwrap_or_else(|error| {
                tracing::debug!(%error, "Codex background-terminal listing unavailable");
                Value::Null
            });
        let mut pending_by_id = BTreeMap::new();
        for item in pending_at_idle
            .into_iter()
            .chain(pending_command_items(&projection, &terminal_response))
        {
            pending_by_id.insert(item.id.clone(), item);
        }
        projection
            .completed_commands
            .retain(|id, _| pending_by_id.contains_key(id));
        let pending = pending_by_id.values().cloned().collect::<Vec<_>>();
        invocation.replace_pending(pending, Vec::new());
        let idle_signal = IdleSignal {
            terminal_state: "run_completed".to_string(),
            exit_code: Some(0),
            stderr: None,
        };
        let outcome = invocation
            .idle(idle_signal)
            .or_else(|| invocation.finish_pending_user_input())
            .context("Codex Console idle arrived without an active turn")?;
        let invocation_state = outcome.invocation_state;
        complete_codex_idle(&invocation, &runtime.sink, outcome).await;
        if invocation_state == InvocationState::Closed {
            invocation.process_exited();
            crate::console_lifecycle::unregister(&launch_id);
            finish_runtime_turn(runtime, &current_config.session_id, &current_config.run_id).await;
            return Ok(());
        }

        let mut known_pending = pending_by_id;
        trigger_codex_wake(
            &invocation,
            &input,
            &runtime.sink,
            &mut known_pending,
            &mut projection,
        )
        .await;
        loop {
            tokio::select! {
                message = control_rx.recv() => {
                    match message {
                        Some(CodexConsoleInputControl::Start(start)) => {
                            finish_runtime_turn(runtime, &current_config.session_id, &current_config.run_id).await;
                            current_config = start.config;
                            runtime = match start_runtime_turn(&current_config) {
                                Ok(runtime) => runtime,
                                Err(error) => {
                                    let _ = start.reply.send(Err(error.to_string()));
                                    return Err(error);
                                }
                            };
                            current_warm_hit = false;
                            current_leased_at = std::time::Instant::now();
                            next_start_reply = Some(start.reply);
                            continue 'turns;
                        }
                        Some(CodexConsoleInputControl::Close) | None => {
                            let pending_count = invocation.pending_count();
                            invocation.process_exited();
                            if let Ok(registry) = crate::turn_claims::default_registry() {
                                let _ = registry.record_invocation_state(
                                    &invocation.latest_turn().run_id,
                                    "closed",
                                    pending_count,
                                );
                            }
                            crate::console_lifecycle::unregister(&launch_id);
                            finish_runtime_turn(runtime, &current_config.session_id, &current_config.run_id).await;
                            return Ok(());
                        }
                    }
                }
                _ = tokio::time::sleep(Duration::from_millis(250)) => {
                    match worker.rpc.request_quiet(
                        "thread/backgroundTerminals/list",
                        json!({"threadId": provider_thread_id}),
                        &mut projection,
                    ).await {
                        Ok(_) => {}
                        Err(error) => {
                            tracing::debug!(%error, "Codex background-terminal polling failed");
                        }
                    }
                    if worker.child.try_wait().ok().flatten().is_some() {
                        let pending_count = invocation.pending_count();
                        invocation.process_exited();
                        if let Ok(registry) = crate::turn_claims::default_registry() {
                            let _ = registry.record_invocation_state(
                                &invocation.latest_turn().run_id,
                                "closed",
                                pending_count,
                            );
                        }
                        crate::console_lifecycle::unregister(&launch_id);
                        finish_runtime_turn(
                            runtime,
                            &current_config.session_id,
                            &current_config.run_id,
                        )
                        .await;
                        return Ok(());
                    }
                    trigger_codex_wake(
                        &invocation,
                        &input,
                        &runtime.sink,
                        &mut known_pending,
                        &mut projection,
                    ).await;
                    if let Some(wake_id) = invocation.pending_wake_id() {
                        if input.wake_input_expired(&wake_id, Instant::now()).await {
                            if let Some(outcome) = invocation.expire_pending_wake(
                                &wake_id,
                                IdleSignal {
                                    terminal_state: "run_completed".to_string(),
                                    exit_code: Some(0),
                                    stderr: None,
                                },
                            ) {
                                input.discard_wake_input(&wake_id).await;
                                let invocation_state = outcome.invocation_state;
                                let pending_count = outcome.pending_count;
                                let run_id = outcome.binding.run_id.clone();
                                complete_codex_idle(&invocation, &runtime.sink, outcome).await;
                                if let Ok(registry) = crate::turn_claims::default_registry() {
                                    let _ = registry.record_invocation_state(
                                        &run_id,
                                        invocation_state.as_str(),
                                        pending_count,
                                    );
                                }
                                if invocation_state == InvocationState::Closed {
                                    invocation.process_exited();
                                    crate::console_lifecycle::unregister(&launch_id);
                                    finish_runtime_turn(
                                        runtime,
                                        &current_config.session_id,
                                        &current_config.run_id,
                                    )
                                    .await;
                                    return Ok(());
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

async fn trigger_codex_wake(
    invocation: &ConsoleInvocation,
    input: &CodexConsoleInput,
    sink: &CodexExecRuntimeSink,
    pending: &mut BTreeMap<String, PendingItem>,
    projection: &mut AppServerProjection,
) {
    let completed = pending
        .iter()
        .filter_map(|(id, pending_item)| {
            projection
                .completed_commands
                .get(id)
                .map(|completion| (id.clone(), pending_item.clone(), completion.clone()))
        })
        .collect::<Vec<_>>();
    if completed.is_empty() {
        return;
    }
    let existing_wake_id = invocation.pending_wake_id();
    let wake = if existing_wake_id.is_none() {
        let task_ids = completed
            .iter()
            .map(|(id, _, _)| id.clone())
            .collect::<Vec<_>>();
        let summary = completed
            .iter()
            .map(|(_, _, item)| command_completion_summary(item))
            .collect::<Vec<_>>()
            .join("; ");
        Some(invocation.response_started(json!({
            "kind": "task_completed",
            "task_ids": task_ids,
            "summary": summary,
        })))
    } else {
        None
    };
    let wake_id = wake
        .as_ref()
        .and_then(|wake| wake.as_ref().map(|wake| wake.wake_id.clone()))
        .or(existing_wake_id);
    let Some(wake_id) = wake_id else {
        return;
    };
    let prompt_items = completed
        .iter()
        .map(|(id, _, item)| (id.clone(), item.clone()))
        .collect::<Vec<_>>();
    input.add_wake_completions(wake_id, prompt_items).await;
    for (id, pending_item, completion) in completed {
        invocation.update_pending_item(
            PendingItem {
                id: pending_item.id.clone(),
                kind: "shell".to_string(),
                status: completion.status,
                description: Some(completion.command),
            },
            false,
        );
        pending.remove(&id);
        projection.completed_commands.remove(&id);
    }
    let _ = invocation.persist_pending_claim();
    if let Some(Some(wake)) = wake {
        sink.post_wake_signal(&wake).await;
    }
}

async fn run_app_server_turn(
    rpc: &mut AppServerRpc,
    provider_thread_id: &str,
    thread_path: Option<&str>,
    sink: &CodexExecRuntimeSink,
    prompt: &str,
    image_paths: &[PathBuf],
    model: Option<&str>,
    projection: &mut AppServerProjection,
    warm_hit: bool,
    leased_at: std::time::Instant,
    mut start_reply: Option<tokio::sync::oneshot::Sender<std::result::Result<(), String>>>,
    starting: Option<ConsoleStartingGuard>,
) -> Result<()> {
    sink.post_provider_binding(provider_thread_id, thread_path)
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
    let mut turn_params = json!({
        "threadId": provider_thread_id,
        "input": crate::codex_attachments::build_user_input_items_from_paths(prompt, image_paths),
    });
    if let Some(model) = model {
        turn_params["model"] = Value::String(model.to_string());
    }
    let turn_response = match rpc
        .request("turn/start", turn_params, sink, projection)
        .await
    {
        Ok(response) => response,
        Err(error) => {
            if let Some(reply) = start_reply.take() {
                let _ = reply.send(Err(error.to_string()));
            }
            return Err(error);
        }
    };
    sink.post_latency_stage(
        "turn_start_ack",
        json!({
            "warm_hit": warm_hit,
            "write_to_ack_ms": turn_write_started.elapsed().as_millis(),
        }),
    )
    .await;
    let expected_turn_id = match json_string(&turn_response, &["turn", "id"]) {
        Some(turn_id) => turn_id,
        None => {
            let error = anyhow::anyhow!("Codex app-server turn/start omitted turn.id");
            if let Some(reply) = start_reply.take() {
                let _ = reply.send(Err(error.to_string()));
            }
            return Err(error);
        }
    };
    sink.post_phase("thinking", None).await;
    sink.post_live_user_item(prompt).await;

    let (steer_tx, mut steer_rx) = mpsc::unbounded_channel::<ConsoleControl>();
    let mut interrupt_requested = false;
    if let Ok(mut registry) = console_steer_registry().lock() {
        registry.insert(sink.run_id.clone(), steer_tx);
    }
    let _steer_registration = ConsoleSteerRegistration(sink.run_id.clone());
    if let Some(reply) = start_reply.take() {
        let _ = reply.send(Ok(()));
    }
    drop(starting);
    let mut pending_steers: HashMap<
        u64,
        (
            tokio::sync::oneshot::Sender<std::result::Result<(), String>>,
            String,
        ),
    > = HashMap::new();

    let turn_outcome = tokio::time::timeout(APP_SERVER_TURN_TIMEOUT, async {
        loop {
            let value = tokio::select! {
                value = rpc.next_value() => value?,
                Some(control) = steer_rx.recv() => {
                    let id = rpc.next_id;
                    rpc.next_id += 1;
                    let (request, reply) = match control {
                        ConsoleControl::Steer { text, reply } => (
                            json!({
                                "id": id,
                                "method": "turn/steer",
                                "params": {
                                    "threadId": provider_thread_id,
                                    "expectedTurnId": expected_turn_id,
                                    "input": crate::codex_attachments::build_user_input_items_from_paths(&text, &[]),
                                },
                            }),
                            reply,
                        ),
                        ConsoleControl::Interrupt { reply } => {
                            interrupt_requested = true;
                            (
                                json!({
                                    "id": id,
                                    "method": "turn/interrupt",
                                    "params": {"threadId": provider_thread_id, "turnId": expected_turn_id},
                                }),
                                reply,
                            )
                        }
                    };
                    match rpc.write(&request).await {
                        Ok(()) => {
                            let method = request["method"].as_str().unwrap_or("turn/steer");
                            pending_steers.insert(id, (reply, method.to_string()));
                        }
                        Err(error) => {
                            let _ = reply.send(Err(format!("control write failed: {error}")));
                        }
                    }
                    continue;
                }
            };
            if value.get("method").is_none() {
                if let Some((reply, method)) = value
                    .get("id")
                    .and_then(Value::as_u64)
                    .and_then(|id| pending_steers.remove(&id))
                {
                    let outcome = match value.get("error") {
                        Some(error) => Err(format!("{method} failed: {error}")),
                        None => Ok(()),
                    };
                    if outcome.is_ok() {
                        sink.post_phase("thinking", None).await;
                    }
                    let _ = reply.send(outcome);
                    continue;
                }
            }
            if value.get("id").is_some() && value.get("method").is_some() {
                rpc.respond_to_server_request(&value).await?;
                continue;
            }
            rpc.seq += 1;
            sink.post_app_server_event(rpc.seq, &value, projection).await;
            if value.get("method").and_then(Value::as_str) == Some("turn/completed") {
                let completed_turn_id = json_string(&value, &["params", "turn", "id"])
                    .context("Codex turn/completed omitted params.turn.id")?;
                if completed_turn_id != expected_turn_id {
                    anyhow::bail!(
                        "Codex completed unexpected turn {completed_turn_id}; expected {expected_turn_id}"
                    );
                }
                let status = json_string(&value, &["params", "turn", "status"])
                    .unwrap_or_else(|| "completed".to_string());
                if status == "interrupted" && interrupt_requested {
                    if let Some(path) = thread_path {
                        sink.wake_transcript_shipper(path, &completed_turn_id, "turn_interrupted")
                            .await;
                    }
                    return Err(anyhow::Error::new(CodexTurnInterrupted));
                }
                if status != "completed" {
                    anyhow::bail!(codex_turn_failure(&status, &value));
                }
                if let Some(path) = thread_path {
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
    .await;
    drop(_steer_registration);
    for (_, (reply, _)) in pending_steers.drain() {
        let _ = reply.send(Err("turn_ended".to_string()));
    }
    while let Ok(control) = steer_rx.try_recv() {
        let (ConsoleControl::Steer { reply, .. } | ConsoleControl::Interrupt { reply }) = control;
        let _ = reply.send(Err("turn_ended".to_string()));
    }
    turn_outcome.context("Codex app-server turn timed out")??;
    Ok(())
}
fn retain_surviving_codex_invocation(launch_id: &str) {
    let recovery = (|| -> Result<()> {
        let registry = crate::turn_claims::default_registry()?;
        let claims = registry.list_all_shared()?;
        if let Some(claim) = claims.iter().rev().find(|claim| {
            claim.provider == "codex"
                && claim.adapter.as_deref() == Some(CODEX_EXEC_ADAPTER)
                && claim.launch_id.as_deref() == Some(launch_id)
        }) {
            registry.record_shutdown_survived(&claim.run_id, claim.pending_count)?;
        }
        Ok(())
    })();
    if let Err(error) = recovery {
        tracing::warn!(%error, launch_id, "Could not retain surviving Codex invocation for recovery");
    }
}

async fn shutdown_worker_process_group(child: &mut Child, pgid: Option<i32>) -> Result<()> {
    let outcome = crate::process_group::shutdown_owned_child(
        child,
        pgid,
        crate::process_group::DEFAULT_GRACE,
    )
    .await;
    if !outcome.is_gone() {
        tracing::warn!(
            pgid = pgid.unwrap_or_default(),
            outcome = outcome.as_str(),
            "Codex worker process group survived SIGKILL"
        );
    }
    Ok(())
}

/// How long shutdown may wait for console workers to settle.
///
/// Shutdown has to terminate. A worker whose unregister never runs — an
/// aborted task, a process reaped out from under the pool — would otherwise
/// hold the daemon open forever, and the daemon awaits this before returning
/// from its SIGTERM path.
const CONSOLE_SHUTDOWN_BUDGET: Duration = Duration::from_secs(5);

/// Wait for a pool notification that was armed while the pool lock was held.
///
/// `notify_waiters` stores no permit, and a `Notified` future does not register
/// interest until it is first polled. Building the future under the lock and
/// awaiting it after the lock is released therefore loses every wakeup that
/// lands in between — which is exactly when the notifiers fire, because both
/// update the pool, release the lock, and only then notify. The caller arms the
/// future under the lock so this can only wait for a notification still to come.
async fn await_pool_settle(
    wait: std::pin::Pin<Box<tokio::sync::futures::OwnedNotified>>,
    deadline: tokio::time::Instant,
    stage: &str,
) -> bool {
    if tokio::time::timeout_at(deadline, wait).await.is_err() {
        eprintln!("[codex-exec] shutdown proceeding while {stage} is still outstanding");
        return false;
    }
    true
}

pub async fn shutdown_codex_console_worker_pool() {
    shutdown_codex_console_worker_pool_within(CONSOLE_SHUTDOWN_BUDGET).await;
}

async fn shutdown_codex_console_worker_pool_within(budget: Duration) {
    let deadline = tokio::time::Instant::now() + budget;
    loop {
        let wait = {
            let mut pool = console_worker_pool().lock().await;
            pool.shutting_down = true;
            if pool.spawning == 0 {
                None
            } else {
                let mut wait = Box::pin(pool.spawn_finished.clone().notified_owned());
                wait.as_mut().enable();
                Some(wait)
            }
        };
        let Some(wait) = wait else { break };
        if !await_pool_settle(wait, deadline, "a warm-worker spawn").await {
            break;
        }
    }
    let (workers, active_process_groups) = {
        let mut pool = console_worker_pool().lock().await;
        (
            std::mem::take(&mut pool.workers),
            pool.active_process_groups
                .values()
                .cloned()
                .collect::<Vec<_>>(),
        )
    };
    for (pgid, launch_id) in active_process_groups {
        // Stop and verify tracked active groups, retaining the exact invocation
        // identity even when its task cannot settle before the shutdown budget.
        let outcome =
            crate::process_group::shutdown_group(pgid, crate::process_group::DEFAULT_GRACE).await;
        if !outcome.is_gone() {
            tracing::warn!(
                pgid,
                outcome = outcome.as_str(),
                "Codex console process group survived SIGKILL during shutdown"
            );
            if let Some(launch_id) = launch_id.as_deref() {
                retain_surviving_codex_invocation(launch_id);
            }
        }
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
                let mut wait = Box::pin(pool.active_finished.clone().notified_owned());
                wait.as_mut().enable();
                Some(wait)
            }
        };
        let Some(wait) = wait else { break };
        if !await_pool_settle(wait, deadline, "an active console worker").await {
            break;
        }
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

    async fn request_quiet(
        &mut self,
        method: &str,
        params: Value,
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
                projection.apply(&value);
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

/// A failed turn names Codex's own reason (`params.turn.error.message`), so a
/// 401 reaches the session as itself rather than as a bare status.
fn codex_turn_failure(status: &str, completed: &Value) -> String {
    match json_string(completed, &["params", "turn", "error", "message"]) {
        Some(message) => format!("Codex turn ended with status {status}: {message}"),
        None => format!("Codex turn ended with status {status}"),
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
        // One slot per session: the daemon records the local ledger from it
        // and sends it. A tool start is a transition of its own because the
        // tool name is part of the statement, so it publishes immediately.
        let observed_at = Utc::now();
        crate::status_slot::publish_console_phase(
            "codex",
            CODEX_EXEC_RUNTIME_SOURCE,
            &self.session_id,
            &self.run_id,
            &observed_at.to_rfc3339(),
            phase,
            tool_name.as_deref(),
            json!({
                "execution_lifetime": "persistent",
                "thread_id": self.thread_id,
                "device_id": self.machine_name,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
            }),
        );
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
                "execution_lifetime": "persistent",
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

    async fn post_delegation_snapshot(&self, invocation_id: &str, snapshot: Value) {
        let observed_at = snapshot
            .get("observed_at")
            .and_then(Value::as_str)
            .unwrap_or_default();
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "delegation_signal",
            "occurred_at": if observed_at.is_empty() { Utc::now().to_rfc3339() } else { observed_at.to_string() },
            "dedupe_key": format!("codex-console:{invocation_id}:{}:delegation:{observed_at}", self.run_id),
            "payload": {"delegation": snapshot}
        })])
        .await;
    }

    async fn post_wake_signal(&self, wake: &WakeRequest) {
        self.post_events(vec![json!({
            "runtime_key": format!("codex:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "codex",
            "device_id": self.machine_name,
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "wake_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("wake:{}", wake.wake_id),
            "payload": {
                "invocation_id": wake.invocation_id,
                "wake_id": wake.wake_id,
                "provider_thread_id": wake.provider_thread_id,
                "trigger": wake.trigger,
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
                "execution_lifetime": "persistent",
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
                "execution_lifetime": "persistent",
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

    /// The socket the daemon listens on. It is derived from the Longhouse
    /// home, not from `local_db_path`: a `--db` outside `$LONGHOUSE_HOME/agent`
    /// (simlab, scratch harnesses) put every wake next to a socket nobody
    /// listened on, so durable ingest waited out the periodic scan instead.
    fn transcript_wake_socket_path(&self) -> Option<PathBuf> {
        self.transcript_wake_socket
            .clone()
            .or_else(|| crate::config::get_agent_transcript_wake_socket_path().ok())
    }

    #[cfg(unix)]
    async fn wake_transcript_shipper(
        &self,
        source_path: &str,
        provider_turn_id: &str,
        wake_reason: &str,
    ) {
        let Some(socket_path) = self.transcript_wake_socket_path() else {
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
            std::fs::metadata(source_path)
                .ok()
                .map(|metadata| metadata.len()),
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
                Value::String("persistent".to_string()),
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
        invocation_id: &str,
        invocation_state: InvocationState,
        pending_count: usize,
    ) {
        let observed_at = Utc::now();
        self.persist_local_phase("finished", None, observed_at);
        let terminal_event = json!({
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
                "execution_lifetime": "persistent",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CODEX_EXEC_RUNTIME_SOURCE,
                "exit_code": exit_code,
                "stderr_tail": stderr_tail,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "invocation": {
                    "id": invocation_id,
                    "state": invocation_state.as_str(),
                    "pending_count": pending_count,
                }
            }
        });
        let handoff = crate::turn_claims::default_registry().and_then(|registry| {
            crate::config::get_agent_runtime_events_outbox_dir().and_then(|outbox| {
                crate::outbox::retain_and_enqueue_terminal_event(
                    &registry,
                    &outbox,
                    &self.run_id,
                    terminal_state,
                    stderr_tail.clone(),
                    terminal_event.clone(),
                )
            })
        });
        let (direct_event, safe_to_retire) = match handoff {
            Ok((event, safe_to_retire)) => (event, safe_to_retire),
            Err(error) => {
                eprintln!(
                    "[codex-exec] terminal claim write failed for {} run {}: {error:#}; keeping the status slot",
                    self.session_id,
                    self.run_id
                );
                let conflict = error
                    .downcast_ref::<crate::turn_claims::TerminalEventConflict>()
                    .is_some();
                let (event, already_durable, retained_in_claim) =
                    match crate::turn_claims::default_registry()
                        .and_then(|registry| registry.read(&self.run_id))
                    {
                        Ok(claim) => match claim.terminal_event {
                            Some(_) if claim.terminal_event_handed_off => (None, true, false),
                            Some(event) => (Some(event), false, true),
                            None if conflict => (None, false, false),
                            None => (Some(terminal_event), false, false),
                        },
                        Err(read_error) => {
                            eprintln!(
                            "[codex-exec] terminal claim unreadable for {} run {}: {read_error:#}",
                            self.session_id, self.run_id
                        );
                            // A known conflict cannot publish the replacement.
                            // IO failure must still preserve the observed native
                            // outcome in the independent durable outbox.
                            if conflict {
                                (None, false, false)
                            } else {
                                (Some(terminal_event), false, false)
                            }
                        }
                    };
                let durable = if let Some(event) = event.as_ref() {
                    match crate::config::get_agent_runtime_events_outbox_dir().and_then(|outbox| {
                        crate::outbox::enqueue_runtime_event_for_handoff(&outbox, event)
                    }) {
                        Ok(true) if retained_in_claim => crate::turn_claims::default_registry()
                            .and_then(|registry| registry.mark_terminal_event_handed_off(&self.run_id, event))
                            .unwrap_or_else(|error| {
                                eprintln!("[codex-exec] terminal handoff acknowledgment failed: {error:#}");
                                false
                            }),
                        Ok(durable) => durable,
                        Err(error) => {
                            eprintln!(
                                "[codex-exec] fallback terminal outbox write failed for {} run {}: {error:#}",
                                self.session_id, self.run_id
                            );
                            false
                        }
                    }
                } else {
                    already_durable
                };
                (event, durable)
            }
        };
        if safe_to_retire {
            crate::status_slot::retire_console_run(
                "codex",
                CODEX_EXEC_RUNTIME_SOURCE,
                &self.session_id,
                &self.run_id,
            );
        } else {
            eprintln!(
                "[codex-exec] terminal record remains pending for {} run {}; keeping the status slot",
                self.session_id,
                self.run_id
            );
        }
        // Keep the low-latency pump alongside the durable outbox handoff. It
        // receives the claim's exact stored event, not a freshly rebuilt retry.
        if let Some(event) = direct_event {
            self.post_events(vec![event]).await;
        }
    }

    fn persist_local_provider_binding(
        &self,
        provider_thread_id: &str,
        known_source_path: Option<&str>,
    ) {
        let source_path = known_source_path
            .map(PathBuf::from)
            .or_else(|| codex_rollout_path(provider_thread_id));
        if let Some(source_path) = source_path.as_deref() {
            // A local claim, not a database row: the daemon projects it into the
            // binding discovery reads, and the claim records the thread this
            // binding was made for. Without that thread the shipper cannot tell
            // a fork Longhouse started from one a managed parent left behind,
            // and it must assume the latter.
            if let Err(error) = crate::managed_source_claim::confirm_identity(
                &self.session_id,
                "codex",
                source_path,
                provider_thread_id,
                None,
                None,
            ) {
                eprintln!("[codex-exec] persist transcript claim failed: {error:#}");
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
        if let Err(err) = crate::hook_outbox::enqueue_local_phase(
            db_path,
            &self.session_id,
            "codex",
            phase,
            tool_name.as_deref(),
            CODEX_EXEC_RUNTIME_SOURCE,
            &observed_at.to_rfc3339(),
            Some(self.run_id.as_str()),
        ) {
            eprintln!(
                "[codex-exec] enqueue local phase failed for {}: {err}",
                self.session_id
            );
        }
    }

    async fn post_events(&self, events: Vec<Value>) {
        // Acceptance testing drops a terminal here, on the real path, so the
        // detection can be proven rather than assumed. Disarmed unless a control
        // file names this exact session; see fault_injection.
        let events = crate::fault_injection::filter_runtime_events(events);
        if events.is_empty() {
            return;
        }
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
const EVENT_PUMP_POST_TIMEOUT: Duration = Duration::from_secs(5);
const EVENT_PUMP_POST_ATTEMPTS: u32 = 3;
/// How long end-of-run waits for the pump to finish its last batch.
///
/// This must exceed one batch's worst-case retry budget. When it did not, the
/// abort below could fire mid-retry and discard the terminal signal the hosted
/// console turn needs to settle.
const EVENT_PUMP_DRAIN_BUDGET: Duration =
    Duration::from_secs((EVENT_PUMP_POST_TIMEOUT.as_secs() + 1) * EVENT_PUMP_POST_ATTEMPTS as u64);

fn events_are_critical(events: &[Value]) -> bool {
    events.iter().any(|event| {
        event_is_state_bearing(event)
            || matches!(
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
    outbox_dir: PathBuf,
) {
    let http = reqwest::Client::new();
    let url = format!(
        "{}/api/agents/runtime/events/batch",
        api_url.trim_end_matches('/')
    );
    loop {
        let first = tokio::select! {
            biased;
            event = critical_receiver.recv(),
                if !(critical_receiver.is_closed() && critical_receiver.is_empty()) => event,
            event = receiver.recv(),
                if !(receiver.is_closed() && receiver.is_empty()) => event,
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
        for attempt in 0..EVENT_PUMP_POST_ATTEMPTS {
            let started = std::time::Instant::now();
            let response = http
                .post(&url)
                .header("X-Agents-Token", &api_token)
                .json(&payload)
                .timeout(EVENT_PUMP_POST_TIMEOUT)
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
                    let status = response.status();
                    let retryable = status.is_server_error() || status.as_u16() == 429;
                    // A rejection the server will never accept still has to say
                    // which event it refused, or the batch vanishes with no way
                    // to tell a schema drift from a transport fault.
                    let detail = if retryable {
                        String::new()
                    } else {
                        let body = response.text().await.unwrap_or_default();
                        format!(" detail={}", body.chars().take(400).collect::<String>())
                    };
                    eprintln!(
                        "[codex-exec] runtime event post failed session={session_id} run={run_id} status={status} attempt={} events={event_count} retryable={retryable}{detail}",
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
            if attempt + 1 < EVENT_PUMP_POST_ATTEMPTS {
                tokio::time::sleep(Duration::from_millis(100 * u64::from(attempt + 1))).await;
            }
        }
        if !delivered {
            let remaining = queued_events
                .fetch_sub(event_count, Ordering::Relaxed)
                .saturating_sub(event_count);
            let spilled = spill_runtime_events(&outbox_dir, &payload);
            eprintln!(
                "[codex-exec] runtime event batch undelivered session={session_id} run={run_id} events={event_count} spilled={spilled} remaining={remaining}"
            );
        }
    }
}

/// Is losing this event a loss of state rather than a loss of liveness?
///
/// Terminal, binding, wake, and delegation signals cannot be reconstructed from previews:
/// the hosted console turn settles on the terminal signal, the session binds to
/// its provider thread, and wake/delegation facts drive the parked lifecycle.
/// Everything else on this channel is preview — the archive ships the same
/// content durably on its own path — so it may be dropped when delivery fails.
fn event_is_state_bearing(event: &Value) -> bool {
    matches!(
        event.get("kind").and_then(Value::as_str),
        Some("terminal_signal" | "binding_signal" | "wake_signal" | "delegation_signal")
    )
}

/// Hand an undelivered batch's state-bearing events to the daemon's durable
/// runtime-event outbox.
///
/// The direct pump exists for latency, not durability: it retries three times
/// and then has nowhere to put the batch. Terminal signals travel this path, so
/// a network blip at end-of-run used to leave the hosted console turn `active`
/// forever, with no evidence anywhere that the run had finished. The outbox
/// keeps files until the server accepts them, so the shared drain loop
/// redelivers what the pump could not.
///
/// Only state-bearing events are spilled. Spilling the whole batch would turn a
/// long outage into thousands of preview files describing a run the archive has
/// already recorded in full.
///
/// Returns the number of events successfully written to disk.
fn spill_runtime_events(outbox_dir: &std::path::Path, payload: &Value) -> usize {
    let Some(events) = payload.get("events").and_then(Value::as_array) else {
        return 0;
    };
    let mut spilled = 0usize;
    for event in events.iter().filter(|event| event_is_state_bearing(event)) {
        match crate::outbox::enqueue_runtime_event(outbox_dir, event) {
            Ok(()) => spilled += 1,
            Err(error) => {
                eprintln!("[codex-exec] runtime outbox spill failed: {error}");
            }
        }
    }
    spilled
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
    #[test]
    fn a_failed_turn_carries_the_codex_error_message() {
        let failed = serde_json::json!({
            "method": "turn/completed",
            "params": {"turn": {"id": "t1", "status": "failed",
                "error": {"message": "unexpected status 401 Unauthorized: Incorrect API key provided"}}},
        });
        assert_eq!(
            super::codex_turn_failure("failed", &failed),
            "Codex turn ended with status failed: unexpected status 401 Unauthorized: Incorrect API key provided"
        );
        let bare = serde_json::json!({"params": {"turn": {"status": "failed"}}});
        assert_eq!(
            super::codex_turn_failure("failed", &bare),
            "Codex turn ended with status failed"
        );
    }

    #[tokio::test]
    async fn stop_during_turn_start_waits_for_the_turn_to_register() {
        let run_id = "stop-during-start";
        let _starting = ConsoleStartingGuard::new(run_id);
        let (tx, mut rx) = mpsc::unbounded_channel::<ConsoleControl>();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(120)).await;
            console_steer_registry()
                .lock()
                .unwrap()
                .insert(run_id.to_string(), tx);
            if let Some(ConsoleControl::Interrupt { reply }) = rx.recv().await {
                let _ = reply.send(Ok(()));
            }
        });
        let outcome = interrupt_codex_console_turn(run_id).await;
        console_steer_registry().lock().unwrap().remove(run_id);
        assert_eq!(outcome, Ok(()));
    }

    /// Register a control channel for `run_id` whose replies to each Stop come
    /// from `answers` in order (the last one repeats); returns the Stops seen.
    fn scripted_stop_channel(
        run_id: &'static str,
        answers: Vec<std::result::Result<(), String>>,
        unregister_after: Option<usize>,
    ) -> Arc<AtomicUsize> {
        let seen = Arc::new(AtomicUsize::new(0));
        let counter = seen.clone();
        let (tx, mut rx) = mpsc::unbounded_channel::<ConsoleControl>();
        console_steer_registry()
            .lock()
            .unwrap()
            .insert(run_id.to_string(), tx);
        tokio::spawn(async move {
            while let Some(ConsoleControl::Interrupt { reply }) = rx.recv().await {
                let index = counter.fetch_add(1, Ordering::SeqCst);
                let _ = reply.send(answers[index.min(answers.len() - 1)].clone());
                if unregister_after == Some(index + 1) {
                    console_steer_registry().lock().unwrap().remove(run_id);
                }
            }
        });
        seen
    }

    const NO_ACTIVE_TURN: &str =
        "turn/interrupt failed: {\"code\":-32600,\"message\":\"no active turn to interrupt\"}";

    #[tokio::test]
    async fn a_stop_codex_refuses_just_after_turn_start_is_retried_until_it_lands() {
        let run_id = "stop-refused-early";
        let seen = scripted_stop_channel(
            run_id,
            vec![
                Err(NO_ACTIVE_TURN.into()),
                Err(NO_ACTIVE_TURN.into()),
                Ok(()),
            ],
            None,
        );
        let outcome = interrupt_codex_console_turn(run_id).await;
        console_steer_registry().lock().unwrap().remove(run_id);
        assert_eq!(outcome, Ok(()));
        assert_eq!(seen.load(Ordering::SeqCst), 3);
    }

    #[tokio::test]
    async fn a_stop_for_a_turn_that_ended_meanwhile_is_a_stop_that_worked() {
        let run_id = "stop-turn-ended-meanwhile";
        // Codex says "no active turn" because the turn is over; the channel then
        // goes away with the turn, so the retry finds no run to stop.
        let seen = scripted_stop_channel(run_id, vec![Err(NO_ACTIVE_TURN.into())], Some(1));
        assert_eq!(interrupt_codex_console_turn(run_id).await, Ok(()));
        assert_eq!(seen.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn any_other_stop_failure_is_not_retried_or_hidden() {
        let run_id = "stop-real-failure";
        let seen = scripted_stop_channel(
            run_id,
            vec![Err("control write failed: broken".into())],
            None,
        );
        let outcome = interrupt_codex_console_turn(run_id).await;
        console_steer_registry().lock().unwrap().remove(run_id);
        assert_eq!(outcome, Err("control write failed: broken".to_string()));
        assert_eq!(seen.load(Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn stop_for_a_run_that_is_not_starting_is_refused_at_once() {
        let started = std::time::Instant::now();
        let outcome = console_control_within("never-started", Duration::from_secs(5), |reply| {
            ConsoleControl::Interrupt { reply }
        })
        .await;
        assert_eq!(outcome, Err("turn_not_steerable".to_string()));
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[tokio::test]
    async fn stop_gives_up_when_a_starting_turn_never_registers() {
        let run_id = "starting-never-registers";
        let _starting = ConsoleStartingGuard::new(run_id);
        let outcome = console_control_within(run_id, Duration::from_millis(150), |reply| {
            ConsoleControl::Interrupt { reply }
        })
        .await;
        assert_eq!(outcome, Err("turn_not_steerable".to_string()));
    }

    #[test]
    fn fork_outranks_resume_when_both_are_present() {
        // A branch's first turn carries the parent to fork from. If resume won,
        // the branch would continue the parent's thread instead of leaving it
        // alone, which is the whole failure this design exists to avoid.
        assert_eq!(
            app_server_thread_method(Some("parent-thread"), Some("some-thread")),
            "thread/fork"
        );
        assert_eq!(
            app_server_thread_method(None, Some("own-thread")),
            "thread/resume"
        );
        assert_eq!(app_server_thread_method(None, None), "thread/start");
    }

    #[test]
    fn a_fork_that_returns_the_parent_thread_is_refused() {
        let parent = "dddddddd-1111-2222-3333-444455556666";
        let error = ensure_fork_produced_a_new_thread(Some(parent), parent)
            .expect_err("a fork returning its parent must not be allowed to run");
        assert!(error.to_string().contains(parent));
        assert!(error.to_string().contains("refusing"));
    }

    #[test]
    fn a_fork_that_returns_a_new_thread_proceeds() {
        assert!(ensure_fork_produced_a_new_thread(
            Some("dddddddd-1111-2222-3333-444455556666"),
            "eeeeeeee-1111-2222-3333-444455556666",
        )
        .is_ok());
    }

    #[test]
    fn identity_is_only_checked_for_forks() {
        // A resume returning the thread it resumed is correct, not a fault.
        assert!(ensure_fork_produced_a_new_thread(None, "same-thread").is_ok());
    }

    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::process::CommandExt;
    use std::thread;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::{TcpListener, UnixListener};
    use tokio::sync::mpsc;

    struct IsolatedLonghouseHome {
        previous: Option<std::ffi::OsString>,
    }

    impl IsolatedLonghouseHome {
        fn set(path: &Path) -> Self {
            let previous = std::env::var_os("LONGHOUSE_HOME");
            unsafe {
                std::env::set_var("LONGHOUSE_HOME", path.as_os_str());
            }
            Self { previous }
        }
    }

    impl Drop for IsolatedLonghouseHome {
        fn drop(&mut self) {
            unsafe {
                match self.previous.take() {
                    Some(value) => std::env::set_var("LONGHOUSE_HOME", value),
                    None => std::env::remove_var("LONGHOUSE_HOME"),
                }
            }
        }
    }

    fn config() -> CodexExecRunConfig {
        CodexExecRunConfig {
            session_id: "11111111-1111-4111-8111-111111111111".to_string(),
            run_id: "22222222-2222-4222-8222-222222222222".to_string(),
            thread_id: Some("44444444-4444-4444-8444-444444444444".to_string()),
            turn_id: Some("55555555-5555-4555-8555-555555555555".to_string()),
            client_request_id: Some("request-1".to_string()),
            origin: "user".to_string(),
            wake_id: None,
            invocation_id: None,
            cwd: PathBuf::from("/tmp/project"),
            api_url: "http://localhost:8080".to_string(),
            api_token: "token".to_string(),
            codex_bin: "codex".to_string(),
            approval_policy: Some(DEFAULT_CONSOLE_APPROVAL_POLICY.to_string()),
            sandbox: Some(DEFAULT_CONSOLE_SANDBOX.to_string()),
            model: None,
            prompt: "Do one bounded turn".to_string(),
            image_paths: Vec::new(),
            launch_actor: None,
            launch_surface: None,
            resume_thread_id: None,
            fork_thread_id: None,
            machine_name: "cinder".to_string(),
            local_db_path: None,
            transcript_wake_socket: None,
        }
    }

    /// The daemon awaits this on its SIGTERM path, so it must return even when
    /// the pool never reports the outstanding work settled. Before the budget,
    /// a spawn that never notified left the engine running forever after
    /// SIGTERM: it broke its main loop, then blocked here and never exited.
    ///
    /// A short budget keeps this fast without paused time (tokio `test-util`
    /// as a dev-dependency splits the build graph and recompiles the engine);
    /// a regression still shows up as a hung test rather than a slow one.
    /// Only `spawning` is seeded. An active process group would make shutdown
    /// signal that group for real, and this pool is a process-wide global.
    #[tokio::test]
    async fn console_shutdown_returns_when_outstanding_work_never_reports() {
        let _agent_state = crate::console_adapter::agent_state_guard();
        {
            let mut pool = console_worker_pool().lock().await;
            pool.spawning = 1;
        }

        shutdown_codex_console_worker_pool_within(Duration::from_millis(50)).await;

        let mut pool = console_worker_pool().lock().await;
        assert!(pool.shutting_down, "shutdown must latch the pool closed");
        pool.spawning = 0;
        pool.shutting_down = false;
    }

    #[test]
    fn five_hundred_sessions_reserve_only_machine_global_pool_target() {
        let mut pool = CodexConsoleWorkerPool::default();
        let reservations = (0..500).filter(|_| pool.reserve_spawn_slot()).count();

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
            TURN_INITIALIZE_BUDGET,
        )
        .await
        .unwrap();
        worker.ready_at = std::time::Instant::now() - Duration::from_secs(5 * 60);
        assert!(
            warm_worker_is_alive(&mut worker),
            "idle age must not evict the machine-global warm worker"
        );
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

    #[tokio::test]
    async fn a_turn_takes_the_worker_a_slow_prewarm_is_still_starting() {
        // Stranger run 10041621cd97: the first turn missed a prewarm that was
        // still starting and cold-started a second `codex` beside it.
        let _agent_state = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let fake_codex = temp.path().join("codex");
        fs::write(
            &fake_codex,
            r#"#!/usr/bin/env python3
import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        time.sleep(0.4)
        print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake/1"}}), flush=True)
    elif msg.get("method") == "initialized":
        time.sleep(60)
"#,
        )
        .unwrap();
        let mut permissions = fs::metadata(&fake_codex).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&fake_codex, permissions).unwrap();

        let nothing_starting =
            lease_warm_worker_by(tokio::time::Instant::now() + Duration::from_secs(5)).await;
        assert!(
            matches!(nothing_starting, WarmLease::Cold),
            "no prewarm in flight: lease answers at once"
        );

        // A slot reserved longer ago than any prewarm can run is a leaked
        // counter: turns cold-start rather than all failing behind it.
        {
            let mut pool = console_worker_pool().lock().await;
            pool.spawning += 1;
            pool.spawn_started_at = Some(
                std::time::Instant::now() - PREWARM_INITIALIZE_BUDGET - Duration::from_secs(11),
            );
        }
        let leaked =
            lease_warm_worker_by(tokio::time::Instant::now() + Duration::from_secs(5)).await;
        assert!(matches!(leaked, WarmLease::Cold));

        // A prewarm that outlives the turn budget is reported, not raced.
        console_worker_pool().lock().await.spawn_started_at = Some(std::time::Instant::now());
        let late =
            lease_warm_worker_by(tokio::time::Instant::now() + Duration::from_millis(50)).await;
        assert!(matches!(late, WarmLease::StillStarting));

        // `spawning` stays reserved for the real prewarm below.
        let fake = fake_codex.to_str().unwrap().to_string();
        let cwd = temp.path().to_path_buf();
        let prewarm = tokio::spawn(async move {
            let worker = spawn_initialized_codex_worker(
                &fake,
                Some("never"),
                Some("workspace-write"),
                &cwd,
                None,
                None,
                None,
                PREWARM_INITIALIZE_BUDGET,
            )
            .await
            .unwrap();
            let mut pool = console_worker_pool().lock().await;
            pool.spawning -= 1;
            pool.spawn_started_at = None;
            pool.workers.push(worker);
            let finished = pool.spawn_finished.clone();
            drop(pool);
            finished.notify_waiters();
        });

        let leased =
            lease_warm_worker_by(tokio::time::Instant::now() + Duration::from_secs(5)).await;
        prewarm.await.unwrap();
        let WarmLease::Worker(mut worker) = leased else {
            panic!("the turn waits for the prewarm instead of missing it");
        };
        console_worker_pool()
            .lock()
            .await
            .active_process_groups
            .clear();
        shutdown_worker_process_group(&mut worker.child, worker.pgid)
            .await
            .unwrap();
    }

    #[tokio::test]
    async fn a_cold_start_after_a_failed_prewarm_gets_only_the_budget_left() {
        // Review rv-20261004T213524Z-7c287cb-e292 F1: a prewarm failing just
        // before the turn's deadline handed the cold start a fresh full budget,
        // so the start could outlast the Runtime Host's 10 s turn-start wait.
        let _agent_state = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let fake_codex = temp.path().join("codex");
        fs::write(
            &fake_codex,
            r#"#!/usr/bin/env python3
import json, sys, time
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        time.sleep(1.5)
        print(json.dumps({"id": msg["id"], "result": {"userAgent": "fake/1"}}), flush=True)
"#,
        )
        .unwrap();
        let mut permissions = fs::metadata(&fake_codex).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&fake_codex, permissions).unwrap();
        let mut run = config();
        run.codex_bin = fake_codex.to_str().unwrap().to_string();
        run.cwd = temp.path().to_path_buf();

        {
            let mut pool = console_worker_pool().lock().await;
            pool.spawning += 1;
            pool.spawn_started_at = Some(std::time::Instant::now());
        }
        let prewarm_fails = tokio::spawn(async {
            tokio::time::sleep(Duration::from_millis(250)).await;
            let mut pool = console_worker_pool().lock().await;
            pool.spawning -= 1;
            pool.spawn_started_at = None;
            let finished = pool.spawn_finished.clone();
            drop(pool);
            finished.notify_waiters();
        });

        let started = std::time::Instant::now();
        let result = start_console_worker_by(
            &run,
            true,
            tokio::time::Instant::now() + Duration::from_millis(400),
        )
        .await;
        let elapsed = started.elapsed();
        prewarm_fails.await.unwrap();
        console_worker_pool()
            .lock()
            .await
            .active_process_groups
            .clear();
        assert!(
            result.is_err(),
            "the cold start got more than the 150 ms the wait left"
        );
        // 400 ms budget plus the process-group cleanup grace, far short of
        // the 1.5 s a fresh budget would have allowed.
        assert!(elapsed < Duration::from_millis(1400), "took {elapsed:?}");
    }

    fn runtime_sink(transcript_wake_socket: Option<PathBuf>) -> CodexExecRuntimeSink {
        let config = config();
        CodexExecRuntimeSink {
            session_id: config.session_id,
            run_id: config.run_id,
            thread_id: config.thread_id,
            turn_id: config.turn_id,
            client_request_id: config.client_request_id,
            machine_name: config.machine_name,
            local_db_path: None,
            transcript_wake_socket,
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
        assert_eq!(
            payload["client_request_id"],
            sink.client_request_id.unwrap()
        );
        assert_eq!(payload["provider_turn_id"], "provider-turn-7");
        assert_eq!(payload["wake_reason"], "turn_completed");
        assert_eq!(payload["file_len_hint"], 321);
    }

    #[test]
    fn completion_wake_targets_the_daemon_socket_not_the_db_directory() {
        let _guard = crate::console_adapter::agent_state_guard();
        let home = tempfile::tempdir().unwrap();
        let elsewhere = tempfile::tempdir().unwrap();
        let mut sink = runtime_sink(None);
        // `--db` outside the agent dir, as simlab and scratch harnesses run it.
        sink.local_db_path = Some(elsewhere.path().join("longhouse-shipper.db"));
        let resolved = temp_env::with_vars(
            [
                ("LONGHOUSE_HOME", Some(home.path().display().to_string())),
                ("CLAUDE_CONFIG_DIR", None),
            ],
            || sink.transcript_wake_socket_path(),
        );
        assert_eq!(
            resolved,
            Some(home.path().join("agent").join("transcript-wake.sock"))
        );
    }

    #[tokio::test]
    async fn completion_wake_reaches_the_resolved_socket() {
        let temp = tempfile::tempdir().unwrap();
        let agent_dir = temp.path().join("agent");
        fs::create_dir_all(&agent_dir).unwrap();
        let socket_path = agent_dir.join("transcript-wake.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();
        let rollout = temp.path().join("rollout.jsonl");
        fs::write(&rollout, b"provider evidence").unwrap();
        let sink = runtime_sink(Some(socket_path.clone()));

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
            tempfile::tempdir().unwrap().keep(),
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
    async fn undelivered_terminal_signal_spills_to_the_durable_outbox() {
        // The hosted console turn only settles when the terminal signal lands.
        // If the pump exhausts its retries the batch must survive on disk for
        // the daemon's outbox drain, or the turn stays `active` forever.
        let outbox = tempfile::tempdir().unwrap();
        let (tx, rx) = mpsc::channel(EVENT_PUMP_QUEUE_CAPACITY);
        let (critical_tx, critical_rx) = mpsc::channel(EVENT_PUMP_CRITICAL_CAPACITY);
        let queued = Arc::new(AtomicUsize::new(1));
        let pump = tokio::spawn(run_runtime_event_pump(
            // Unroutable port: every attempt fails, exercising the spill path.
            "http://127.0.0.1:1".to_string(),
            "token".to_string(),
            "session-1".to_string(),
            "run-1".to_string(),
            rx,
            critical_rx,
            queued.clone(),
            outbox.path().to_path_buf(),
        ));

        critical_tx
            .send(vec![json!({
                "kind": "terminal_signal",
                "dedupe_key": "codex-exec:session-1:run-1:terminal",
                "payload": {"terminal_state": "run_completed"},
            })])
            .await
            .unwrap();
        // Preview traffic shares the batch but not the durability guarantee:
        // the archive ships the same content on its own path, so spilling it
        // would turn a long outage into thousands of redundant files.
        tx.send(vec![json!({
            "kind": "progress_signal",
            "dedupe_key": "console:live:run-1:item-1:1",
            "payload": {"progress_kind": "bridge_live_transcript_delta"},
        })])
        .await
        .unwrap();
        drop(tx);
        drop(critical_tx);
        tokio::time::timeout(EVENT_PUMP_DRAIN_BUDGET, pump)
            .await
            .expect("pump must finish within the drain budget it advertises")
            .unwrap();

        let spilled = crate::outbox::collect_runtime_event_outbox(outbox.path());
        assert_eq!(
            spilled.len(),
            1,
            "exactly the terminal signal must survive on disk",
        );
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
    async fn bounded_preview_queue_preserves_critical_state_lane() {
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
        sink.post_events(vec![json!({"kind": "wake_signal"})]).await;

        let terminal = critical_event_rx.recv().await.unwrap();
        assert_eq!(terminal[0]["kind"], "terminal_signal");
        let wake = critical_event_rx.recv().await.unwrap();
        assert_eq!(wake[0]["kind"], "wake_signal");
        // One queued preview plus both critical events remain in the sink.
        assert_eq!(sink.queued_events.load(Ordering::Relaxed), 3);
    }

    /// `RuntimeEventIngest.tool_name` caps at 128 characters and rejects the
    /// whole batch above it, so a long shell command in this field 422'd every
    /// event batched alongside it. Keep the wire field a token regardless of
    /// how long the command is.
    #[test]
    fn tool_phase_reports_a_token_not_the_command() {
        const WIRE_TOOL_NAME_LIMIT: usize = 128;
        let mut projection = AppServerProjection::default();
        let command = format!(
            "cd /Users/davidrose/git/zerg && {}",
            "git log --oneline ".repeat(20)
        );
        assert!(
            command.len() > WIRE_TOOL_NAME_LIMIT,
            "fixture must exceed the wire cap"
        );

        let projected = projection.apply(&json!({
            "method": "item/started",
            "params": {"item": {"id": "exec-1", "type": "commandExecution", "command": command}},
        }));

        let phase = projected
            .iter()
            .find_map(|event| match event {
                ProjectedAppServerEvent::Phase { phase, tool_name } => {
                    Some((*phase, tool_name.clone()))
                }
                _ => None,
            })
            .expect("a tool start must project a phase");
        assert_eq!(phase.0, "running");
        assert_eq!(phase.1.as_deref(), Some("shell"));

        // The command itself is not lost -- it rides the tool item's payload.
        let carried = projected.iter().any(|event| {
            matches!(event, ProjectedAppServerEvent::ToolItem { command: value, .. } if value == &command)
        });
        assert!(carried, "the command must still reach the tool item");
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
    fn codex_app_server_args_omit_model_and_keep_console_defaults() {
        let mut config = config();
        config.model = Some("gpt-5.3-codex-low".to_string());
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
                "danger-full-access",
                "app-server",
                "--listen",
                "stdio://",
            ]
        );
    }

    #[test]
    fn warm_pool_accepts_a_protocol_model_override() {
        let mut config = config();
        config.model = Some("gpt-5.3-codex-low".to_string());

        assert!(warm_pool_compatible(&config));
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
                "danger-full-access",
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
                "execution_lifetime": "persistent",
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
            Value::String("persistent".to_string()),
        );
        assert_eq!(obj["managed_transport"], "codex_app_server");
        assert_eq!(obj["execution_lifetime"], "persistent");
    }

    #[test]
    fn codex_wake_output_tail_is_bounded() {
        let output = (0..100)
            .map(|line| format!("line-{line} {}\n", "x".repeat(100)))
            .collect::<String>();
        let tail = bounded_output_tail(&output);
        assert!(tail.len() <= 4 * 1024);
        assert!(tail.lines().count() <= 40);
        assert!(tail.contains("line-99"));
    }
    #[test]
    fn bounded_output_tail_counts_trailing_blank_line_within_cap() {
        let output = "old\nmiddle\nlast\n\n";
        let tail = bounded_output_tail_with_limits(output, 1024, 2);
        assert_eq!(tail, "last\n\n");
        assert_eq!(tail.lines().count(), 2);
    }
    #[tokio::test]
    async fn codex_unbound_wake_input_uses_console_retention_ttl() {
        let (sender, _receiver) = mpsc::unbounded_channel();
        let input = CodexConsoleInput {
            sender,
            next_turn: AsyncMutex::new(None),
            wake_inputs: AsyncMutex::new(HashMap::new()),
        };
        let wake_id = "launch:1".to_string();
        input
            .add_wake_completions(
                wake_id.clone(),
                vec![(
                    "exec-1".to_string(),
                    CompletedCommandExecution {
                        command: "echo complete".to_string(),
                        status: "completed".to_string(),
                        exit_code: Some(0),
                        output: "done\n".to_string(),
                    },
                )],
            )
            .await;
        let now = Instant::now();
        let expires_at = now + crate::console_lifecycle::RETAINED_WAKE_TTL + Duration::from_secs(1);
        assert!(!input.wake_input_expired(&wake_id, now).await);
        assert!(input.wake_input_prompt(&wake_id, now).await.is_some());
        assert!(input.wake_input_expired(&wake_id, expires_at).await);
        assert!(input
            .wake_input_prompt(&wake_id, expires_at)
            .await
            .is_none());
        input.discard_wake_input(&wake_id).await;
        assert!(input.wake_input_prompt(&wake_id, now).await.is_none());
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
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let _home = IsolatedLonghouseHome::set(&longhouse_home);
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
    elif method == "thread/backgroundTerminals/list":
        emit({"id": msg["id"], "result": {"data": [], "nextCursor": None}})
    elif method == "turn/start":
        if msg.get("params", {}).get("model") != "gpt-5.3-codex-low":
            sys.exit(9)
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
        run_config.session_id = uuid::Uuid::new_v4().to_string();
        run_config.run_id = uuid::Uuid::new_v4().to_string();
        run_config.thread_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.turn_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.client_request_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.cwd = temp.path().join("workspace");
        fs::create_dir_all(&run_config.cwd).unwrap();
        run_config.codex_bin = fake_codex.display().to_string();
        run_config.api_url = api_url;
        run_config.resume_thread_id = Some("provider-thread".to_string());
        run_config.transcript_wake_socket = Some(longhouse_home.join("agent/transcript-wake.sock"));
        run_config.model = Some("gpt-5.3-codex-low".to_string());
        crate::turn_claims::default_registry()
            .unwrap()
            .claim(
                &run_config.run_id,
                &run_config.session_id,
                run_config.thread_id.as_deref().unwrap(),
                run_config.turn_id.as_deref(),
                run_config.client_request_id.as_deref(),
                "codex",
            )
            .unwrap();
        let summary = start_codex_exec_once(run_config).await.unwrap();
        assert!(
            summary
                .argv
                .iter()
                .all(|argument| !argument.contains("model=")),
            "the selected model must not reach the provider process argv"
        );

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
        assert_eq!(
            crate::config::get_longhouse_home().unwrap(),
            longhouse_home,
            "default state roots resolve into this test's private Longhouse home"
        );
        let claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&summary.run_id)
            .unwrap();
        let launch_id = claim.launch_id.clone().expect("invocation launch id");
        wait_for_codex_invocation_closed(&launch_id).await;
        assert_eq!(claim.invocation_state.as_deref(), Some("closed"));
        assert_eq!(claim.state, "terminal");
        assert_eq!(
            crate::config::get_agent_runtime_events_outbox_dir().unwrap(),
            longhouse_home.join("agent/runtime-events-outbox")
        );
        assert!(
            longhouse_home
                .join("managed-local/claims")
                .join(format!("{}.json", summary.session_id))
                .exists(),
            "source bindings are private to this run"
        );
        assert!(
            !crate::status_slot::slot_path(
                &crate::status_slot::status_slot_dir(&longhouse_home.join("agent"),),
                &summary.session_id,
            )
            .exists(),
            "the closed fake run leaves no current status"
        );
        assert!(
            !longhouse_home.join("agent/transcript-wake.sock").exists(),
            "the completion wake socket is private and not left behind"
        );
    }

    fn fake_app_server_for_scenario(temp: &Path, scenario: &str) -> (PathBuf, PathBuf) {
        let codex_bin = temp.join("codex");
        let input_log = temp.join("turn-inputs.txt");
        let script = r#"#!/usr/bin/env python3
import json, subprocess, sys, time
scenario = "__SCENARIO__"
input_log = __INPUT_LOG__
pending = {}
turn_count = 0
list_count = 0

def emit(value):
    print(json.dumps(value), flush=True)

def start_item(item_id, command):
    pending[item_id] = command
    emit({"method": "item/started", "params": {"item": {
        "id": item_id, "type": "commandExecution", "command": command,
        "status": "inProgress"
    }}})

def complete_item(item_id):
    command = pending.pop(item_id, None)
    if command is None:
        return
    emit({"method": "item/completed", "params": {"item": {
        "id": item_id, "type": "commandExecution", "command": command,
        "aggregatedOutput": "completed output for " + item_id + "\n",
        "status": "completed", "exitCode": 0
    }}})

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        emit({"id": msg["id"], "result": {"userAgent": "fake/1"}})
    elif method == "initialized":
        pass
    elif method in ("thread/start", "thread/resume", "thread/fork"):
        emit({"id": msg["id"], "result": {"thread": {
            "id": "provider-thread", "path": "/tmp/fake-codex-rollout.jsonl"
        }}})
    elif method == "turn/start":
        turn_count += 1
        turn_id = "provider-turn-" + str(turn_count)
        input_text = "\n".join(
            item.get("text", "") for item in msg.get("params", {}).get("input", [])
            if item.get("type") == "text"
        )
        with open(input_log, "a", encoding="utf-8") as stream:
            stream.write(input_text + "\n")
        emit({"id": msg["id"], "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        emit({"method": "turn/started", "params": {"turn": {"id": turn_id, "status": "inProgress"}}})
        if scenario == "UserDuringPendingWake" and turn_count == 2:
            time.sleep(1)
        fresh_stop_resume = scenario == "StopWhileParked" and input_text == "fresh turn after stop"
        if turn_count == 1 and scenario != "Plain" and not fresh_stop_resume:
            start_item("exec-1", "python3 -c 'print(\"first\")'")
            if scenario == "StopWhileParked":
                background = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                with open(input_log + ".pid", "w", encoding="utf-8") as stream:
                    stream.write(str(background.pid))
            if scenario == "WakePending":
                start_item("exec-2", "python3 -c 'print(\"second\")'")
        elif scenario in ("Background", "UserSend") or (scenario == "WakePending" and turn_count >= 3):
            for item_id in list(pending):
                complete_item(item_id)
        emit({"method": "item/agentMessage/delta", "params": {
            "itemId": "message-" + str(turn_count), "delta": "fake response"
        }})
        emit({"method": "item/completed", "params": {"item": {
            "id": "message-" + str(turn_count), "type": "agentMessage"
        }}})
        emit({"method": "turn/completed", "params": {
            "turn": {"id": turn_id, "status": "completed"}
        }})
    elif method == "thread/backgroundTerminals/list":
        list_count += 1
        if scenario == "WakePending" and list_count == 2:
            complete_item("exec-1")
        elif scenario == "WakeDrained" and list_count == 2:
            complete_item("exec-1")
        elif scenario == "UserDuringPendingWake" and list_count == 2:
            complete_item("exec-1")
        data = [{
            "itemId": item_id, "processId": "process-" + item_id,
            "command": command, "cwd": "/tmp"
        } for item_id, command in pending.items()]
        emit({"id": msg["id"], "result": {"data": data, "nextCursor": None}})
"#
            .replace("__SCENARIO__", scenario)
            .replace(
                "__INPUT_LOG__",
                &serde_json::to_string(&input_log.display().to_string()).unwrap(),
            );
        fs::write(&codex_bin, script).unwrap();
        let mut permissions = fs::metadata(&codex_bin).unwrap().permissions();
        permissions.set_mode(0o755);
        fs::set_permissions(&codex_bin, permissions).unwrap();
        (codex_bin, input_log)
    }

    fn claim_codex_test_run(config: &CodexExecRunConfig) {
        let registry = crate::turn_claims::default_registry().unwrap();
        assert!(matches!(
            registry
                .claim(
                    &config.run_id,
                    &config.session_id,
                    config.thread_id.as_deref().unwrap(),
                    config.turn_id.as_deref(),
                    config.client_request_id.as_deref(),
                    "codex",
                )
                .unwrap(),
            crate::turn_claims::ClaimOutcome::Acquired
        ));
    }

    fn scenario_run_config(
        root: &Path,
        api_url: &str,
        codex_bin: &Path,
        prompt: &str,
    ) -> CodexExecRunConfig {
        let mut run_config = config();
        run_config.session_id = uuid::Uuid::new_v4().to_string();
        run_config.run_id = uuid::Uuid::new_v4().to_string();
        run_config.thread_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.turn_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.client_request_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.cwd = root.to_path_buf();
        run_config.api_url = api_url.to_string();
        run_config.codex_bin = codex_bin.display().to_string();
        run_config.prompt = prompt.to_string();
        run_config.resume_thread_id = None;
        run_config.fork_thread_id = None;
        run_config.transcript_wake_socket = Some(root.join("longhouse/agent/transcript-wake.sock"));
        run_config
    }

    async fn wait_for_captured_event(
        receiver: &mut mpsc::UnboundedReceiver<Vec<Value>>,
        events: &mut Vec<Value>,
        predicate: impl Fn(&Value) -> bool,
    ) -> Value {
        tokio::time::timeout(Duration::from_secs(15), async {
            loop {
                if let Some(event) = events.iter().find(|event| predicate(event)).cloned() {
                    return event;
                }
                let batch = receiver.recv().await.expect("runtime event stream closed");
                events.extend(batch);
            }
        })
        .await
        .expect("timed out waiting for Codex runtime event")
    }

    async fn wait_for_codex_invocation_closed(launch_id: &str) {
        tokio::time::timeout(Duration::from_secs(10), async {
            while crate::console_lifecycle::lookup_launch(launch_id).is_some() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("Codex invocation did not close");
    }

    fn runtime_outbox_events() -> Vec<Value> {
        let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
        fs::read_dir(outbox)
            .unwrap()
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
            .filter_map(|path| fs::read(path).ok())
            .filter_map(|bytes| serde_json::from_slice(&bytes).ok())
            .collect()
    }

    fn run_codex_scenario(
        scenario: crate::console_lifecycle::conformance::LifecycleScenario,
    ) -> crate::console_lifecycle::conformance::ScenarioFuture {
        Box::pin(async move { run_codex_scenario_inner(scenario).await })
    }

    async fn run_codex_scenario_inner(
        scenario: crate::console_lifecycle::conformance::LifecycleScenario,
    ) -> crate::console_lifecycle::conformance::ScenarioOutcome {
        use crate::console_lifecycle::conformance::{LifecycleScenario, ScenarioOutcome};

        if scenario == LifecycleScenario::Restart {
            run_codex_restart_scenario().await;
            return ScenarioOutcome::Passed;
        }
        let _agent_state = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let _home = IsolatedLonghouseHome::set(&longhouse_home);
        let scenario_name = format!("{scenario:?}");
        let (fake_codex, input_log) = fake_app_server_for_scenario(temp.path(), &scenario_name);
        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let mut first_config =
            scenario_run_config(temp.path(), &api_url, &fake_codex, "scenario=background");
        first_config.prompt = if scenario == LifecycleScenario::Plain {
            "plain turn".to_string()
        } else {
            "start a background command".to_string()
        };
        claim_codex_test_run(&first_config);
        let first = start_codex_exec_once(first_config.clone()).await.unwrap();
        let mut events = Vec::new();
        wait_for_captured_event(&mut received, &mut events, |event| {
            event["run_id"] == first.run_id && event["kind"] == "terminal_signal"
        })
        .await;
        let first_claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&first.run_id)
            .unwrap();
        let launch_id = first_claim.launch_id.clone().unwrap();
        match scenario {
            LifecycleScenario::Plain => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("closed"));
                assert_eq!(first_claim.pending_count, 0);
                assert_fake_process_group_gone(first.process_group_id.unwrap()).await;
                return ScenarioOutcome::Passed;
            }
            LifecycleScenario::Background
            | LifecycleScenario::UserSend
            | LifecycleScenario::WakePending
            | LifecycleScenario::WakeDrained
            | LifecycleScenario::StopWhileParked => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                let expected_pending = if scenario == LifecycleScenario::WakePending {
                    2
                } else {
                    1
                };
                assert_eq!(first_claim.pending_count, expected_pending);
                let delegation = wait_for_captured_event(&mut received, &mut events, |event| {
                    event["run_id"] == first.run_id && event["kind"] == "delegation_signal"
                })
                .await;
                assert_eq!(
                    delegation["payload"]["delegation"]["count"],
                    expected_pending
                );
                assert_eq!(
                    delegation["payload"]["delegation"]["items"][0]["kind"],
                    "shell"
                );
                assert!(crate::process_group::group_is_alive(
                    first.process_group_id.unwrap()
                ));
                assert!(
                    !crate::status_slot::slot_path(
                        &crate::status_slot::status_slot_dir(&longhouse_home.join("agent")),
                        &first.session_id,
                    )
                    .exists(),
                    "parked background work retains its claim but not foreground thinking"
                );
            }
            LifecycleScenario::Restart => unreachable!(),
            _ => return ScenarioOutcome::Unsupported("not_implemented:scenario_not_in_phase_one"),
        }
        if scenario == LifecycleScenario::StopWhileParked {
            let background_pid: u32 = fs::read_to_string(format!("{}.pid", input_log.display()))
                .unwrap()
                .parse()
                .unwrap();
            assert!(crate::process_identity::try_collect_process_fact(background_pid).is_some());
            let invocation = crate::console_lifecycle::lookup_launch(&launch_id).unwrap();
            let stopped =
                close_parked_codex_invocation(&first_claim, invocation, "codex-lifecycle-test")
                    .await
                    .unwrap()
                    .expect("parked Codex invocation should close");
            assert_eq!(stopped.stopped.len(), 1);
            assert_eq!(stopped.stopped[0].id, "exec-1");
            assert!(!crate::process_group::group_is_alive(
                first.process_group_id.unwrap()
            ));
            assert!(crate::process_identity::try_collect_process_fact(background_pid).is_none());
            wait_for_codex_invocation_closed(&launch_id).await;

            let close_events = runtime_outbox_events()
                .into_iter()
                .filter(|event| event["session_id"] == first.session_id)
                .collect::<Vec<_>>();
            let closed = close_events
                .iter()
                .find(|event| event["kind"] == "invocation_closed")
                .expect("Codex user-stop close event");
            assert_eq!(closed["run_id"], first.run_id);
            assert_eq!(closed["provider"], "codex");
            assert_eq!(closed["source"], CODEX_EXEC_RUNTIME_SOURCE);
            assert_eq!(closed["payload"]["invocation_id"], launch_id);
            assert_eq!(closed["payload"]["reason"], "user_stop");
            assert_eq!(closed["payload"]["stopped"][0]["id"], "exec-1");
            let empty = close_events
                .iter()
                .find(|event| {
                    event["kind"] == "delegation_signal"
                        && event["dedupe_key"] == format!("close:{launch_id}:delegation")
                })
                .expect("empty delegation snapshot after close");
            assert_eq!(empty["payload"]["delegation"]["count"], 0);

            let mut resumed_config =
                scenario_run_config(temp.path(), &api_url, &fake_codex, "fresh turn after stop");
            resumed_config.session_id = first.session_id.clone();
            resumed_config.thread_id = first_config.thread_id.clone();
            resumed_config.resume_thread_id = Some("provider-thread".to_string());
            resumed_config.origin = "user".to_string();
            claim_codex_test_run(&resumed_config);
            let resumed = start_codex_exec_once(resumed_config).await.unwrap();
            assert_ne!(resumed.pid, first.pid);
            assert_ne!(resumed.process_group_id, first.process_group_id);
            let resumed_terminal = wait_for_captured_event(&mut received, &mut events, |event| {
                event["run_id"] == resumed.run_id && event["kind"] == "terminal_signal"
            })
            .await;
            assert_eq!(
                resumed_terminal["payload"]["terminal_state"],
                "run_completed"
            );
            let resumed_claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&resumed.run_id)
                .unwrap();
            assert_eq!(resumed_claim.invocation_state.as_deref(), Some("closed"));
            assert_eq!(resumed_claim.pending_count, 0);
            wait_for_codex_invocation_closed(&resumed_claim.launch_id.clone().unwrap()).await;
            assert_fake_process_group_gone(resumed.process_group_id.unwrap()).await;
            return ScenarioOutcome::Passed;
        }

        if matches!(
            scenario,
            LifecycleScenario::WakePending | LifecycleScenario::WakeDrained
        ) {
            let wake = wait_for_captured_event(&mut received, &mut events, |event| {
                event["session_id"] == first.session_id && event["kind"] == "wake_signal"
            })
            .await;
            assert_eq!(wake["payload"]["invocation_id"], launch_id);
            assert_eq!(wake["payload"]["trigger"]["kind"], "task_completed");
            assert!(wake["payload"]["trigger"]["summary"]
                .as_str()
                .unwrap()
                .contains("exit code 0"));
            let mut wake_config = scenario_run_config(temp.path(), &api_url, &fake_codex, "");
            wake_config.session_id = first.session_id.clone();
            wake_config.thread_id = first_config.thread_id.clone();
            wake_config.origin = "wake".to_string();
            wake_config.wake_id = wake["payload"]["wake_id"].as_str().map(str::to_string);
            wake_config.invocation_id = Some(launch_id.clone());
            wake_config.resume_thread_id = Some("provider-thread".to_string());
            claim_codex_test_run(&wake_config);
            let wake_run = start_codex_exec_once(wake_config).await.unwrap();
            assert_eq!(wake_run.pid, first.pid);
            let wake_terminal = wait_for_captured_event(&mut received, &mut events, |event| {
                event["run_id"] == wake_run.run_id && event["kind"] == "terminal_signal"
            })
            .await;
            let wake_claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&wake_run.run_id)
                .unwrap();
            assert_eq!(wake_claim.origin.as_deref(), Some("wake"));
            assert!(wake_claim.adopted_parked_invocation);
            let wake_text = events.iter().find_map(|event| {
                (event["run_id"] == wake_run.run_id
                    && event["payload"]["progress_kind"] == "console_live_user_item")
                    .then(|| event["payload"]["text"].as_str())
                    .flatten()
            });
            let wake_text = wake_text.expect("wake turn must contain Longhouse-authored input");
            assert!(wake_text.starts_with("Longhouse background-task completion"));
            assert!(wake_text.contains("Command: python3"));
            assert!(wake_text.contains("Exit code: 0"));
            assert!(wake_text.contains("completed output for exec-1"));
            assert_eq!(wake_terminal["payload"]["execution_lifetime"], "persistent");
            if scenario == LifecycleScenario::WakePending {
                assert_eq!(wake_claim.invocation_state.as_deref(), Some("parked"));
                assert_eq!(wake_claim.pending_count, 1);
            } else {
                assert_eq!(wake_claim.invocation_state.as_deref(), Some("closed"));
                assert_eq!(wake_claim.pending_count, 0);
                wait_for_codex_invocation_closed(&launch_id).await;
                assert_fake_process_group_gone(first.process_group_id.unwrap()).await;
                return ScenarioOutcome::Passed;
            }
        }

        let mut user_config = scenario_run_config(
            temp.path(),
            &api_url,
            &fake_codex,
            "continue from the parked invocation",
        );
        user_config.session_id = first.session_id.clone();
        user_config.thread_id = first_config.thread_id.clone();
        user_config.resume_thread_id = Some("provider-thread".to_string());
        user_config.origin = "user".to_string();
        claim_codex_test_run(&user_config);
        let user_run = start_codex_exec_once(user_config.clone()).await.unwrap();
        assert_eq!(user_run.pid, first.pid);
        assert_eq!(user_run.process_group_id, first.process_group_id);
        let user_terminal = wait_for_captured_event(&mut received, &mut events, |event| {
            event["run_id"] == user_run.run_id && event["kind"] == "terminal_signal"
        })
        .await;
        let user_claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&user_run.run_id)
            .unwrap();
        assert_eq!(user_claim.origin.as_deref(), Some("user"));
        assert!(user_claim.adopted_parked_invocation);
        assert_eq!(user_claim.invocation_state.as_deref(), Some("closed"));
        assert_eq!(user_terminal["payload"]["invocation"]["state"], "closed");
        assert!(fs::read_to_string(input_log)
            .unwrap()
            .contains("continue from the parked invocation"));
        wait_for_codex_invocation_closed(&launch_id).await;
        assert_fake_process_group_gone(first.process_group_id.unwrap()).await;
        ScenarioOutcome::Passed
    }

    async fn assert_fake_process_group_gone(process_group_id: i32) {
        tokio::time::timeout(Duration::from_secs(5), async {
            while crate::process_group::group_is_alive(process_group_id) {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("fake Codex process group survived lifecycle close");
    }

    async fn run_codex_restart_scenario() {
        let _agent_state = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let (child, start) = spawn_fake_codex_process(temp.path());
        let run_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        let launch_id = uuid::Uuid::new_v4().to_string();
        let pid = child.id();
        let process_group_id = i32::try_from(pid).unwrap();
        let child_reaper = thread::spawn(move || {
            let mut child = child;
            child.wait()
        });
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "codex")
            .unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                pid,
                process_group_id,
                Some(start),
                CODEX_EXEC_ADAPTER,
                &launch_id,
                Some("provider-thread"),
                "",
                "",
                json!({"argv": ["fake-codex"]}),
            )
            .unwrap();
        registry
            .record_invocation_pending_items(
                &run_id,
                vec![PendingItem {
                    id: "exec-1".to_string(),
                    kind: "shell".to_string(),
                    status: "running".to_string(),
                    description: Some("python3 background command".to_string()),
                }],
            )
            .unwrap();
        registry
            .mark_terminal(&run_id, "run_completed", None)
            .unwrap();
        registry
            .record_invocation_state(&run_id, "parked", 1)
            .unwrap();
        let process_facts = crate::process_identity::try_collect_process_facts_by_pid().unwrap();
        assert_eq!(
            recover_live_codex_exec_claims(&registry, &outbox, "fake-box", &process_facts)
                .await
                .unwrap(),
            1
        );
        let claim = registry.read(&run_id).unwrap();
        assert_eq!(claim.state, "terminal");
        assert_eq!(claim.result.unwrap()["terminal_state"], "run_completed");
        assert_eq!(claim.invocation_state.as_deref(), Some("closed"));
        assert_eq!(claim.pending_count, 1);
        let status = child_reaper.join().unwrap().unwrap();
        assert!(!status.success());
        let events = fs::read_dir(&outbox)
            .unwrap()
            .flatten()
            .filter(|entry| {
                entry.path().extension().and_then(|value| value.to_str()) == Some("json")
            })
            .filter_map(|entry| fs::read(entry.path()).ok())
            .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .collect::<Vec<_>>();
        assert!(!events
            .iter()
            .any(|event| event["kind"] == "terminal_signal"));
        let closed = events
            .iter()
            .find(|event| event["kind"] == "invocation_closed")
            .expect("restart invocation-closed event must be durable");
        assert_eq!(closed["run_id"], run_id);
        assert_eq!(closed["source"], CODEX_EXEC_RUNTIME_SOURCE);
        assert_eq!(closed["payload"]["reason"], "machine_agent_restart");
        assert_eq!(closed["payload"]["stopped"][0]["id"], "exec-1");
        assert!(events.iter().any(|event| {
            event["kind"] == "delegation_signal"
                && event["dedupe_key"] == format!("close:{launch_id}:delegation")
                && event["payload"]["delegation"]["count"] == 0
        }));
    }

    #[tokio::test]
    async fn codex_console_lifecycle_conformance_runs_phase_one_scenarios() {
        let adapters: [(
            &'static str,
            crate::console_lifecycle::conformance::ScenarioRunner,
        ); 1] = [("codex", run_codex_scenario)];
        crate::console_lifecycle::conformance::run_phase_one(&adapters).await;
    }
    #[tokio::test]
    async fn user_turn_supersedes_unbound_codex_wake_on_same_worker() {
        let _agent_state = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let scenario = "UserDuringPendingWake";
        let (fake_codex, input_log) = fake_app_server_for_scenario(temp.path(), scenario);
        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let first_config = scenario_run_config(
            temp.path(),
            &api_url,
            &fake_codex,
            "start background command",
        );
        claim_codex_test_run(&first_config);
        let first = start_codex_exec_once(first_config.clone()).await.unwrap();
        let mut events = Vec::new();
        wait_for_captured_event(&mut received, &mut events, |event| {
            event["run_id"] == first.run_id && event["kind"] == "terminal_signal"
        })
        .await;
        let first_claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&first.run_id)
            .unwrap();
        assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
        let launch_id = first_claim.launch_id.clone().unwrap();
        let wake = wait_for_captured_event(&mut received, &mut events, |event| {
            event["session_id"] == first.session_id && event["kind"] == "wake_signal"
        })
        .await;
        assert_eq!(wake["payload"]["trigger"]["kind"], "task_completed");

        let mut user_config = scenario_run_config(
            temp.path(),
            &api_url,
            &fake_codex,
            "user turn supersedes the unbound wake",
        );
        user_config.session_id = first.session_id.clone();
        user_config.thread_id = first_config.thread_id.clone();
        user_config.resume_thread_id = Some("provider-thread".to_string());
        user_config.origin = "user".to_string();
        claim_codex_test_run(&user_config);
        let user_run = start_codex_exec_once(user_config).await.unwrap();
        assert_eq!(user_run.pid, first.pid);
        assert_eq!(user_run.process_group_id, first.process_group_id);
        let active_invocation = crate::console_lifecycle::lookup_launch(&launch_id).unwrap();
        assert_eq!(active_invocation.state(), InvocationState::Responding);
        assert_eq!(active_invocation.pending_wake_id(), None);
        let mut stale_wake_config = scenario_run_config(temp.path(), &api_url, &fake_codex, "");
        stale_wake_config.session_id = first.session_id.clone();
        stale_wake_config.thread_id = first_config.thread_id.clone();
        stale_wake_config.resume_thread_id = Some("provider-thread".to_string());
        stale_wake_config.origin = "wake".to_string();
        stale_wake_config.wake_id = wake["payload"]["wake_id"].as_str().map(str::to_string);
        stale_wake_config.invocation_id = Some(launch_id.clone());
        claim_codex_test_run(&stale_wake_config);
        let stale_wake = start_codex_exec_once(stale_wake_config).await.unwrap();
        assert_eq!(stale_wake.pid, None);
        let cancelled = wait_for_captured_event(&mut received, &mut events, |event| {
            event["run_id"] == stale_wake.run_id && event["kind"] == "terminal_signal"
        })
        .await;
        assert_eq!(cancelled["payload"]["terminal_state"], "run_cancelled");
        assert_eq!(cancelled["payload"]["stderr_tail"], "wake_target_gone");
        assert!(crate::console_lifecycle::lookup_launch(&launch_id).is_some());
        let user_terminal = wait_for_captured_event(&mut received, &mut events, |event| {
            event["run_id"] == user_run.run_id && event["kind"] == "terminal_signal"
        })
        .await;
        let user_claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&user_run.run_id)
            .unwrap();
        assert_eq!(user_claim.origin.as_deref(), Some("user"));
        assert!(user_claim.adopted_parked_invocation);
        assert_eq!(user_terminal["payload"]["invocation"]["state"], "closed");
        assert!(fs::read_to_string(input_log)
            .unwrap()
            .contains("user turn supersedes the unbound wake"));
        wait_for_codex_invocation_closed(&launch_id).await;
        assert_fake_process_group_gone(first.process_group_id.unwrap()).await;
    }

    /// Drive one real turn and return (provider thread id, assistant text).
    async fn run_installed_codex_turn(
        run_config: CodexExecRunConfig,
        needle: &str,
    ) -> (String, bool) {
        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let mut run_config = run_config;
        run_config.api_url = api_url;
        run_config.codex_bin =
            std::env::var("LONGHOUSE_TEST_CODEX_BIN").unwrap_or_else(|_| "codex".to_string());
        start_codex_exec_once(run_config).await.unwrap();

        let mut provider_thread_id = None;
        let mut saw_needle = false;
        let mut saw_terminal = false;
        tokio::time::timeout(Duration::from_secs(180), async {
            while let Some(batch) = received.recv().await {
                for event in batch {
                    if event.get("kind").and_then(Value::as_str) == Some("binding_signal") {
                        provider_thread_id =
                            json_string(&event, &["payload", "provider_thread_id"]);
                    }
                    saw_needle |= json_string(&event, &["payload", "live_text"])
                        .is_some_and(|text| text.contains(needle));
                    saw_terminal |=
                        event.get("kind").and_then(Value::as_str) == Some("terminal_signal");
                }
                if saw_terminal && provider_thread_id.is_some() {
                    break;
                }
            }
        })
        .await
        .expect("installed Codex turn did not reach a terminal signal");

        (
            provider_thread_id.expect("adapter never bound a provider thread"),
            saw_needle,
        )
    }

    /// The premise the whole branching design rests on: a fork produces a
    /// *second* thread that still remembers the parent, and the child's later
    /// turns continue the child rather than the parent.
    ///
    /// Unit tests cover the method choice and the fail-closed identity check
    /// against a stub. This is the part only the installed binary can answer.
    #[tokio::test]
    #[ignore = "calls the installed Codex provider; run explicitly as an external contract canary"]
    async fn installed_codex_forks_into_a_new_thread_that_remembers_the_parent() {
        let _agent_state = crate::console_adapter::agent_state_guard();
        let state_dir = tempfile::tempdir().unwrap();
        let longhouse_home = state_dir.path().join("longhouse");
        let _home = IsolatedLonghouseHome::set(&longhouse_home);
        let db_path = state_dir.path().join("state.db");
        let cwd = std::env::current_dir().unwrap();

        // Parent: plant a fact only this thread knows.
        let mut parent = config();
        parent.session_id = uuid::Uuid::new_v4().to_string();
        parent.run_id = uuid::Uuid::new_v4().to_string();
        parent.thread_id = Some(uuid::Uuid::new_v4().to_string());
        parent.turn_id = Some(uuid::Uuid::new_v4().to_string());
        parent.cwd = cwd.clone();
        parent.local_db_path = Some(db_path.clone());
        parent.prompt =
            "Remember the token BRANCH_PARENT_TOKEN. Reply with exactly PARENT_READY.".to_string();
        let (parent_thread_id, _) = run_installed_codex_turn(parent, "PARENT_READY").await;

        // Branch: fork the parent and ask for the planted fact back.
        let child_session_id = uuid::Uuid::new_v4().to_string();
        let longhouse_thread_id = uuid::Uuid::new_v4().to_string();
        let mut child = config();
        child.cwd = cwd.clone();
        child.local_db_path = Some(db_path.clone());
        child.session_id = child_session_id.to_string();
        child.run_id = uuid::Uuid::new_v4().to_string();
        child.thread_id = Some(longhouse_thread_id.clone());
        child.fork_thread_id = Some(parent_thread_id.clone());
        child.prompt = "Reply with exactly the token you were asked to remember.".to_string();
        let (child_thread_id, recalled) =
            run_installed_codex_turn(child, "BRANCH_PARENT_TOKEN").await;

        assert_ne!(
            child_thread_id, parent_thread_id,
            "thread/fork returned the parent thread; the child would share its rollout"
        );
        assert!(
            recalled,
            "the fork did not carry the parent's context forward"
        );

        // The binding names the child's own thread, which is what makes the
        // child's transcript attributable to the child's session rather than
        // read as an inherited parent binding.
        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        let binding = crate::state::session_binding::SessionBinding::new(&conn);
        let child_path = codex_rollout_path(&child_thread_id)
            .expect("forked child rollout should exist on disk");
        let (bound_session, bound_thread) = binding
            .get_with_thread_for_provider(&child_path.to_string_lossy(), "codex")
            .unwrap()
            .expect("the fork should have bound its own rollout path");
        assert_eq!(bound_session, child_session_id);
        assert_eq!(bound_thread.as_deref(), Some(child_thread_id.as_str()));

        // Turn two continues the child, not the parent.
        let mut second = config();
        second.cwd = cwd;
        second.local_db_path = Some(db_path.clone());
        second.session_id = child_session_id.to_string();
        second.run_id = uuid::Uuid::new_v4().to_string();
        second.thread_id = Some(longhouse_thread_id);
        second.resume_thread_id = Some(child_thread_id.clone());
        second.prompt = "Reply with exactly SECOND_TURN_OK.".to_string();
        let (second_thread_id, _) = run_installed_codex_turn(second, "SECOND_TURN_OK").await;
        assert_eq!(
            second_thread_id, child_thread_id,
            "the child's second turn must resume the child, never the parent"
        );
    }

    #[tokio::test]
    #[ignore = "calls the installed Codex provider; run explicitly as an external contract canary"]
    async fn installed_codex_completes_through_production_console_adapter() {
        let _agent_state = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let _home = IsolatedLonghouseHome::set(&longhouse_home);
        let (api_url, mut received) = spawn_runtime_capture_server().await;
        let mut run_config = config();
        run_config.session_id = uuid::Uuid::new_v4().to_string();
        run_config.run_id = uuid::Uuid::new_v4().to_string();
        run_config.thread_id = Some(uuid::Uuid::new_v4().to_string());
        run_config.cwd = std::env::current_dir().unwrap();
        run_config.api_url = api_url;
        run_config.codex_bin =
            std::env::var("LONGHOUSE_TEST_CODEX_BIN").unwrap_or_else(|_| "codex".to_string());
        run_config.transcript_wake_socket = Some(longhouse_home.join("agent/transcript-wake.sock"));
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

    #[test]
    fn codex_exec_process_gone_claim_is_closed_at_registry_and_outbox() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let run_id = "22222222-2222-4222-8222-222222222222";
        registry
            .claim(
                run_id,
                "11111111-1111-4111-8111-111111111111",
                "44444444-4444-4444-8444-444444444444",
                Some("55555555-5555-4555-8555-555555555555"),
                None,
                "codex",
            )
            .unwrap();
        registry
            .mark_spawned(
                run_id,
                Some(u32::MAX),
                Some(i32::MAX),
                Some("Mon Jan  1 00:00:00 2024".to_string()),
                "codex_exec",
                json!({"transport": "codex_app_server"}),
            )
            .unwrap();

        assert_eq!(
            reconcile_codex_exec_claims(
                &registry,
                &outbox,
                "cinder",
                crate::process_identity::try_collect_process_facts_by_pid(),
            )
            .unwrap(),
            1
        );
        let claim = registry.read(run_id).unwrap();
        assert_eq!(claim.state, "terminal");
        assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
        let event_path = fs::read_dir(&outbox)
            .unwrap()
            .flatten()
            .find(|entry| entry.path().extension().and_then(|value| value.to_str()) == Some("json"))
            .expect("recovered terminal must be durably queued")
            .path();
        let event: Value = serde_json::from_slice(&fs::read(event_path).unwrap()).unwrap();
        assert_eq!(event["kind"], "terminal_signal");
        assert_eq!(event["payload"]["terminal_state"], "run_cancelled");
        assert_eq!(event["payload"]["terminal_reason"], "machine_agent_restart");
    }

    fn spawn_fake_codex_process(temp: &Path) -> (std::process::Child, String) {
        let fake_codex = temp.join("codex");
        std::os::unix::fs::symlink("/bin/sleep", &fake_codex).unwrap();
        let mut command = std::process::Command::new(&fake_codex);
        command.arg("60");
        unsafe {
            command.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let child = command.spawn().unwrap();
        let start = (0..20)
            .find_map(|_| {
                let start = crate::turn_claims::process_start_time_for_pid(Some(child.id()));
                if start.is_none() {
                    thread::sleep(Duration::from_millis(10));
                }
                start
            })
            .expect("fake Codex process must expose a start time");
        (child, start)
    }

    fn seed_codex_claim(
        registry: &crate::turn_claims::TurnClaimRegistry,
        run_id: &str,
        pid: Option<u32>,
        process_start_time: Option<String>,
    ) {
        registry
            .claim(
                run_id,
                "11111111-1111-4111-8111-111111111111",
                "44444444-4444-4444-8444-444444444444",
                Some("55555555-5555-4555-8555-555555555555"),
                None,
                "codex",
            )
            .unwrap();
        registry
            .mark_spawned(
                run_id,
                pid,
                pid.and_then(|value| i32::try_from(value).ok()),
                process_start_time,
                CODEX_EXEC_ADAPTER,
                json!({"transport": CODEX_EXEC_RUNTIME_SOURCE}),
            )
            .unwrap();
    }

    #[test]
    fn reconcile_does_not_attach_live_codex_exec_workers() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let (mut child, start) = spawn_fake_codex_process(temp.path());
        seed_codex_claim(
            &registry,
            "22222222-2222-4222-8222-222222222222",
            Some(child.id()),
            Some(start),
        );

        assert_eq!(
            reconcile_codex_exec_claims(
                &registry,
                &outbox,
                "cinder",
                crate::process_identity::try_collect_process_facts_by_pid(),
            )
            .unwrap(),
            0
        );
        assert_eq!(
            registry
                .read("22222222-2222-4222-8222-222222222222")
                .unwrap()
                .state,
            "spawned"
        );
        assert!(crate::outbox::collect_runtime_event_outbox(&outbox).is_empty());
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[tokio::test]
    async fn live_codex_exec_invocation_is_killed_on_machine_agent_restart() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let (child, start) = spawn_fake_codex_process(temp.path());
        let run_id = uuid::Uuid::new_v4().to_string();
        let session_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        let launch_id = uuid::Uuid::new_v4().to_string();
        let pid = child.id();
        let process_group_id = i32::try_from(pid).unwrap();
        let child_reaper = thread::spawn(move || {
            let mut child = child;
            child.wait()
        });
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "codex")
            .unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                pid,
                process_group_id,
                Some(start),
                CODEX_EXEC_ADAPTER,
                &launch_id,
                Some("provider-thread"),
                "",
                "",
                json!({"argv": ["fake-codex"]}),
            )
            .unwrap();
        registry
            .record_invocation_state(&run_id, "parked", 1)
            .unwrap();
        let process_facts = crate::process_identity::try_collect_process_facts_by_pid().unwrap();

        assert_eq!(
            recover_live_codex_exec_claims(&registry, &outbox, "fake-box", &process_facts)
                .await
                .unwrap(),
            1
        );
        let claim = registry.read(&run_id).unwrap();
        assert_eq!(claim.state, "terminal");
        assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
        assert_eq!(claim.invocation_state.as_deref(), Some("closed"));
        assert_eq!(claim.pending_count, 1);
        assert!(!crate::process_group::group_is_alive(process_group_id));
        assert!(!child_reaper.join().unwrap().unwrap().success());
        let event_path = fs::read_dir(&outbox)
            .unwrap()
            .flatten()
            .find(|entry| entry.path().extension().and_then(|value| value.to_str()) == Some("json"))
            .expect("restart closure must be durably queued")
            .path();
        let event: Value = serde_json::from_slice(&fs::read(event_path).unwrap()).unwrap();
        assert_eq!(event["payload"]["terminal_reason"], "machine_agent_restart");
        assert_eq!(event["payload"]["invocation"]["state"], "closed");
    }

    #[test]
    fn rebooted_codex_exec_claim_is_process_gone_even_if_pid_is_alive() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let claims_dir = temp.path().join("claims");
        let registry = crate::turn_claims::TurnClaimRegistry::new(claims_dir.clone());
        let outbox = temp.path().join("outbox");
        let (mut child, start) = spawn_fake_codex_process(temp.path());
        let run_id = "22222222-2222-4222-8222-222222222222";
        seed_codex_claim(&registry, run_id, Some(child.id()), Some(start));

        let path = claims_dir.join(format!("{run_id}.json"));
        let mut value: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        value["boot_id"] = json!("different-boot");
        fs::write(&path, serde_json::to_vec_pretty(&value).unwrap()).unwrap();

        assert_eq!(
            reconcile_codex_exec_claims(
                &registry,
                &outbox,
                "cinder",
                crate::process_identity::try_collect_process_facts_by_pid(),
            )
            .unwrap(),
            1
        );
        assert_eq!(registry.read(run_id).unwrap().state, "terminal");
        assert_eq!(
            registry.read(run_id).unwrap().result.unwrap()["terminal_state"],
            "run_cancelled"
        );
        child.kill().unwrap();
        child.wait().unwrap();
    }

    #[test]
    fn recycled_codex_exec_pid_is_process_gone_when_start_time_changes() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let (mut child, _actual_start) = spawn_fake_codex_process(temp.path());
        seed_codex_claim(
            &registry,
            "22222222-2222-4222-8222-222222222222",
            Some(child.id()),
            Some("Mon Jan  1 00:00:00 2024".to_string()),
        );

        assert_eq!(
            reconcile_codex_exec_claims(
                &registry,
                &outbox,
                "cinder",
                crate::process_identity::try_collect_process_facts_by_pid(),
            )
            .unwrap(),
            1
        );
        assert_eq!(
            registry
                .read("22222222-2222-4222-8222-222222222222")
                .unwrap()
                .result
                .unwrap()["terminal_state"],
            "run_cancelled"
        );
        child.kill().unwrap();
        child.wait().unwrap();
    }

    /// A terminal is an actuator: settling a console turn dispatches the next
    /// queued one. So an inventory we could not read must never read as "gone",
    /// or one failed `ps` at daemon start runs turn N+1 against a turn N that
    /// never stopped.
    #[test]
    fn unreadable_process_inventory_never_terminalizes_a_claim() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let run_id = "88888888-8888-4888-8888-888888888888";
        registry
            .claim(
                run_id,
                "11111111-1111-4111-8111-111111111111",
                "44444444-4444-4444-8444-444444444444",
                Some("55555555-5555-4555-8555-555555555555"),
                None,
                "codex",
            )
            .unwrap();
        registry
            .mark_spawned(
                run_id,
                Some(u32::MAX),
                Some(i32::MAX),
                Some("Mon Jan  1 00:00:00 2024".to_string()),
                "codex_exec",
                json!({"transport": "codex_app_server"}),
            )
            .unwrap();

        // `None` is what `try_collect_process_facts_by_pid` returns when `ps`
        // fails to run or returns an incoherent scan -- not when the machine
        // genuinely has no such process.
        assert_eq!(
            reconcile_codex_exec_claims(&registry, &outbox, "cinder", None).unwrap(),
            0,
            "an unreadable inventory must recover nothing"
        );

        let claims = registry.list_nonterminal().unwrap();
        assert!(
            claims.iter().any(|claim| claim.run_id == run_id),
            "the claim must survive for a later scan"
        );
        assert!(
            crate::outbox::collect_runtime_event_outbox(&outbox).is_empty(),
            "no terminal may be published from an unreadable inventory"
        );
    }

    #[test]
    fn codex_exec_claim_without_start_identity_is_left_ambiguous() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let (mut child, _start) = spawn_fake_codex_process(temp.path());
        seed_codex_claim(
            &registry,
            "22222222-2222-4222-8222-222222222222",
            Some(child.id()),
            None,
        );

        assert_eq!(
            reconcile_codex_exec_claims(
                &registry,
                &outbox,
                "cinder",
                crate::process_identity::try_collect_process_facts_by_pid(),
            )
            .unwrap(),
            0
        );
        assert_eq!(
            registry
                .read("22222222-2222-4222-8222-222222222222")
                .unwrap()
                .state,
            "spawned"
        );
        assert!(crate::outbox::collect_runtime_event_outbox(&outbox).is_empty());
        child.kill().unwrap();
        child.wait().unwrap();
    }
    #[test]
    fn terminal_handoff_failure_keeps_status_and_pending_invocation_until_durable() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let session_id = uuid::Uuid::new_v4().to_string();
        let thread_id = uuid::Uuid::new_v4().to_string();
        let run_id = uuid::Uuid::new_v4().to_string();
        let invocation_id = uuid::Uuid::new_v4().to_string();
        let successor_run_id = uuid::Uuid::new_v4().to_string();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                let agent_dir = crate::config::get_agent_dir().unwrap();
                let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
                std::fs::create_dir_all(outbox.parent().unwrap()).unwrap();
                std::fs::write(&outbox, b"outbox path is a file").unwrap();
                let registry = crate::turn_claims::default_registry().unwrap();
                registry
                    .claim(&run_id, &session_id, &thread_id, None, None, "codex")
                    .unwrap();
                let mut sink = runtime_sink(None);
                sink.session_id = session_id.clone();
                sink.run_id = run_id.clone();
                sink.thread_id = Some(thread_id);

                sink.post_phase("thinking", None).await;
                sink.post_terminal(
                    "run_completed",
                    Some(0),
                    None,
                    &invocation_id,
                    InvocationState::Parked,
                    2,
                )
                .await;
                let status_dir = crate::status_slot::status_slot_dir(&agent_dir);
                assert!(crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id && slot.run_id == run_id));
                let claim = registry.read(&run_id).unwrap();
                assert_eq!(claim.state, "terminal");
                assert_eq!(claim.invocation_state.as_deref(), Some("parked"));
                assert_eq!(claim.pending_count, 2);
                assert!(crate::outbox::collect_runtime_event_outbox(&outbox).is_empty());

                std::fs::remove_file(&outbox).unwrap();
                sink.post_terminal(
                    "run_completed",
                    Some(0),
                    None,
                    &invocation_id,
                    InvocationState::Parked,
                    2,
                )
                .await;
                assert_eq!(
                    crate::outbox::collect_runtime_event_outbox(&outbox).len(),
                    1
                );
                assert!(!crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id));

                crate::status_slot::publish_console_phase(
                    "codex",
                    CODEX_EXEC_RUNTIME_SOURCE,
                    &session_id,
                    &successor_run_id,
                    &Utc::now().to_rfc3339(),
                    "thinking",
                    None,
                    json!({}),
                );
                crate::status_slot::retire_console_run(
                    "codex",
                    CODEX_EXEC_RUNTIME_SOURCE,
                    &session_id,
                    &run_id,
                );
                assert!(crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id && slot.run_id == successor_run_id));
            });
        });
    }
    #[test]
    fn completed_parked_response_closes_on_restart_without_replacing_its_outcome() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let outbox = temp.path().join("outbox");
        let run_id = uuid::Uuid::new_v4().to_string();
        seed_codex_claim(&registry, &run_id, Some(u32::MAX), Some("old-birth".into()));
        registry
            .record_invocation_state(&run_id, "parked", 2)
            .unwrap();
        let original = json!({
            "runtime_key": "codex:11111111-1111-4111-8111-111111111111",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "run_id": run_id,
            "thread_id": "44444444-4444-4444-8444-444444444444",
            "provider": "codex",
            "device_id": "cinder",
            "source": CODEX_EXEC_RUNTIME_SOURCE,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("codex-exec:response:{run_id}:terminal"),
            "payload": {
                "terminal_state": "run_completed",
                "invocation": {"id": "invocation", "state": "parked", "pending_count": 2}
            }
        });
        assert!(
            crate::outbox::retain_and_enqueue_terminal_event(
                &registry,
                &outbox,
                &run_id,
                "run_completed",
                None,
                original.clone(),
            )
            .unwrap()
            .1
        );
        let completed = registry.read(&run_id).unwrap();
        let blocked = temp.path().join("blocked-outbox");
        fs::write(&blocked, b"not a directory").unwrap();
        assert!(!settle_codex_restart_claim(
            &registry,
            &blocked,
            "cinder",
            &completed,
            "Machine Agent restarted",
        ));
        assert_eq!(
            registry.read(&run_id).unwrap().invocation_state.as_deref(),
            Some("parked")
        );
        fs::remove_file(&blocked).unwrap();
        assert!(settle_codex_restart_claim(
            &registry,
            &blocked,
            "cinder",
            &completed,
            "Machine Agent restarted",
        ));
        let reloaded = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"))
            .read(&run_id)
            .unwrap();
        assert_eq!(
            reloaded.result.as_ref().unwrap()["terminal_state"],
            "run_completed"
        );
        assert_eq!(reloaded.terminal_event.as_ref(), Some(&original));
        assert_eq!(reloaded.invocation_state.as_deref(), Some("closed"));
        assert_eq!(reloaded.pending_count, 2);
        let events = fs::read_dir(&blocked)
            .unwrap()
            .flatten()
            .filter(|entry| {
                entry.path().extension().and_then(|value| value.to_str()) == Some("json")
            })
            .map(|entry| serde_json::from_slice::<Value>(&fs::read(entry.path()).unwrap()).unwrap())
            .collect::<Vec<_>>();
        let closing = events
            .iter()
            .find(|event| event["kind"] == "invocation_closed")
            .expect("restart closure must be durably queued");
        assert!(events.iter().any(|event| {
            event["kind"] == "delegation_signal" && event["payload"]["delegation"]["count"] == 0
        }));
        assert_eq!(closing["payload"]["reason"], "machine_agent_restart");
        assert!(closing["payload"].get("terminal_state").is_none());
        assert_ne!(closing["dedupe_key"], original["dedupe_key"]);
        assert!(codex_recovery_claims(&registry).unwrap().is_empty());
    }
    #[test]
    fn one_unwritable_restart_claim_does_not_abort_later_settlement() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let claims_dir = temp.path().join("claims");
        let registry = crate::turn_claims::TurnClaimRegistry::new(claims_dir.clone());
        let good = uuid::Uuid::new_v4().to_string();
        let bad = uuid::Uuid::new_v4().to_string();
        for run_id in [&good, &bad] {
            seed_codex_claim(&registry, run_id, Some(u32::MAX), Some("old-birth".into()));
            registry
                .record_invocation_state(run_id, "parked", 2)
                .unwrap();
            registry
                .mark_terminal(run_id, "run_completed", None)
                .unwrap();
        }
        let lock_path = claims_dir.join(format!(".{bad}.lock"));
        fs::remove_file(&lock_path).unwrap();
        fs::create_dir(&lock_path).unwrap();
        let settled = reconcile_codex_exec_claims(
            &registry,
            &temp.path().join("outbox"),
            "cinder",
            Some(std::collections::HashMap::new()),
        )
        .unwrap();
        assert_eq!(settled, 1);
        assert_eq!(
            registry.read(&bad).unwrap().invocation_state.as_deref(),
            Some("parked")
        );
        assert_eq!(
            registry.read(&good).unwrap().invocation_state.as_deref(),
            Some("closed")
        );
    }
    #[test]
    fn missing_terminal_claim_preserves_the_outcome_in_the_independent_outbox() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                let mut sink = runtime_sink(None);
                sink.session_id = uuid::Uuid::new_v4().to_string();
                sink.run_id = uuid::Uuid::new_v4().to_string();
                sink.post_phase("thinking", None).await;
                sink.post_terminal(
                    "run_completed",
                    Some(0),
                    None,
                    "invocation",
                    InvocationState::Parked,
                    2,
                )
                .await;
                let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
                let terminal = fs::read_dir(&outbox)
                    .unwrap()
                    .flatten()
                    .filter_map(|entry| fs::read(entry.path()).ok())
                    .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                    .find(|event| {
                        event["kind"] == "terminal_signal" && event["run_id"] == sink.run_id
                    })
                    .unwrap();
                assert_eq!(terminal["payload"]["terminal_state"], "run_completed");
                assert_eq!(terminal["payload"]["invocation"]["state"], "parked");
                assert_eq!(terminal["payload"]["invocation"]["pending_count"], 2);
                assert!(
                    crate::status_slot::read_all(&crate::status_slot::status_slot_dir(
                        &crate::config::get_agent_dir().unwrap()
                    ),)
                    .iter()
                    .all(|slot| slot.run_id != sink.run_id)
                );
            });
        });
    }

    #[test]
    fn surviving_codex_worker_remains_eligible_for_restart_recovery() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            let registry = crate::turn_claims::default_registry().unwrap();
            let run_id = uuid::Uuid::new_v4().to_string();
            let launch_id = uuid::Uuid::new_v4().to_string();
            seed_codex_claim(&registry, &run_id, Some(424242), Some("known-birth".into()));
            registry
                .mark_spawned_invocation(
                    &run_id,
                    424242,
                    424242,
                    Some("known-birth".into()),
                    CODEX_EXEC_ADAPTER,
                    &launch_id,
                    None,
                    "",
                    "",
                    json!({}),
                )
                .unwrap();
            registry
                .record_invocation_state(&run_id, "closed", 2)
                .unwrap();
            registry
                .mark_terminal(&run_id, "run_completed", None)
                .unwrap();
            retain_surviving_codex_invocation(&launch_id);
            let survivor = registry.read(&run_id).unwrap();
            assert_eq!(survivor.invocation_state.as_deref(), Some("parked"));
            assert_eq!(
                survivor.result.as_ref().unwrap()["terminal_state"],
                "run_completed"
            );
            assert!(codex_recovery_claims(&registry)
                .unwrap()
                .iter()
                .any(|claim| claim.run_id == run_id));
        });
    }
    #[test]
    fn conflicting_terminal_fallback_delivers_only_the_retained_response_and_acknowledges_it() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                let registry = crate::turn_claims::default_registry().unwrap();
                let mut sink = runtime_sink(None);
                sink.session_id = uuid::Uuid::new_v4().to_string();
                sink.run_id = uuid::Uuid::new_v4().to_string();
                registry.claim(
                    &sink.run_id, &sink.session_id, &uuid::Uuid::new_v4().to_string(),
                    None, None, "codex",
                ).unwrap();
                let original = json!({
                    "runtime_key": format!("codex:{}", sink.session_id),
                    "session_id": sink.session_id,
                    "run_id": sink.run_id,
                    "provider": "codex",
                    "device_id": "cinder",
                    "source": CODEX_EXEC_RUNTIME_SOURCE,
                    "kind": "terminal_signal",
                    "occurred_at": Utc::now().to_rfc3339(),
                    "dedupe_key": format!("codex-exec:{}:{}:terminal", sink.session_id, sink.run_id),
                    "payload": {"terminal_state": "run_completed"}
                });
                registry.mark_terminal_with_event(
                    &sink.run_id, "run_completed", None, original.clone(),
                ).unwrap();
                sink.post_phase("thinking", None).await;
                sink.post_terminal(
                    "run_cancelled", None, None, "invocation",
                    InvocationState::Closed, 0,
                ).await;
                let saved = registry.read(&sink.run_id).unwrap();
                assert_eq!(saved.result.as_ref().unwrap()["terminal_state"], "run_completed");
                assert!(saved.terminal_event_handed_off);
                let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
                let outcomes = fs::read_dir(&outbox).unwrap().flatten()
                    .filter_map(|entry| fs::read(entry.path()).ok())
                    .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                    .filter(|event| event["kind"] == "terminal_signal")
                    .collect::<Vec<_>>();
                assert_eq!(outcomes, vec![original]);
                assert!(crate::status_slot::read_all(
                    &crate::status_slot::status_slot_dir(&crate::config::get_agent_dir().unwrap()),
                ).iter().all(|slot| slot.run_id != sink.run_id));
            });
        });
    }
}
