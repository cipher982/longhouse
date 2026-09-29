//! OpenCode Console turns over a Longhouse-owned `opencode serve`.
//!
//! One server per Console turn. The native OpenCode session persists across
//! turns; the server dies with the turn. Longhouse creates (or verifies) the
//! native session, submits the prompt with `prompt_async`, follows the turn on
//! the event stream, and decides the turn is over from OpenCode's own messages
//! (`turn_outcome`), never from a stream or a process ending.
//!
//! The event stream is progress only. A dropped stream costs progress, never
//! the outcome: every tick reconciles from `GET /session/{id}/message` and
//! `/session/status`, and missing evidence reads as unknown, not as finished.
//! The durable transcript comes from OpenCode's own database (`opencode_db`).

use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::process::{Child, Command};
use tokio::sync::mpsc;
use uuid::Uuid;

use crate::console_adapter::{stderr_tail, ClaimLiveness};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use crate::opencode_server::{HttpStatusError, OpenCodeServer};

pub const OPENCODE_RUN_ADAPTER: &str = "opencode_run";

/// A prompt was accepted but nothing answered it. OpenCode normally starts in
/// well under a second; this bounds a wedged server.
const START_TIMEOUT: Duration = Duration::from_secs(90);
/// How often the turn is reconciled from the server's own state.
const RECONCILE_EVERY: Duration = Duration::from_secs(1);
/// How long an unreachable server is tolerated before the turn is failed.
const SERVER_DOWN_AFTER: Duration = Duration::from_secs(15);
/// How long "idle before the work finished" must persist before it is a failure:
/// a brief gap between steps is not the end of the turn.
const STALL_GRACE: Duration = Duration::from_secs(5);
/// The newest text preview per part is re-emitted at most this often.
const TEXT_PREVIEW_EVERY: Duration = Duration::from_millis(200);

#[derive(Clone, Debug)]
pub struct OpenCodeRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub opencode_bin: String,
    pub prompt: String,
    /// Staged image files, sent as inline `file` parts of the prompt.
    pub image_paths: Vec<PathBuf>,
    pub resume_provider_thread_id: Option<String>,
    pub model: Option<String>,
    pub permission_mode: String,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct OpenCodeRunSummary {
    pub session_id: String,
    pub thread_id: String,
    pub run_id: String,
    pub provider_thread_id: Option<String>,
    pub launch_id: String,
    pub pid: u32,
    pub process_group_id: i32,
    pub stdout_path: String,
    pub stderr_path: String,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct OpenCodeRunSink {
    session_id: String,
    thread_id: String,
    turn_id: Option<String>,
    run_id: String,
    client_request_id: Option<String>,
    expected_provider_thread_id: Option<String>,
    launch_id: String,
    process_group_id: Option<i32>,
    machine_name: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

/// What a restarted engine needs to reconnect to a turn, kept (0600, in the
/// run's private directory) beside its claim.
#[derive(Clone, Debug, Serialize, Deserialize)]
struct ServeState {
    server_url: String,
    username: String,
    password: String,
    directory: String,
    provider_session_id: Option<String>,
    /// Every user message this turn submitted, in order.
    message_ids: Vec<String>,
}

impl ServeState {
    const FILE: &'static str = "serve.json";

    fn server(&self) -> OpenCodeServer {
        OpenCodeServer {
            url: self.server_url.clone(),
            username: self.username.clone(),
            password: self.password.clone(),
            directory: self.directory.clone(),
        }
    }

    fn path(run_dir: &Path) -> PathBuf {
        run_dir.join(Self::FILE)
    }

    fn save(&self, run_dir: &Path) -> Result<()> {
        atomic_write_json(&Self::path(run_dir), &serde_json::to_value(self)?)
    }

    fn load(run_dir: &Path) -> Result<Self> {
        let path = Self::path(run_dir);
        serde_json::from_slice(
            &std::fs::read(&path)
                .with_context(|| format!("reading OpenCode server state {}", path.display()))?,
        )
        .context("OpenCode server state is invalid")
    }
}

// ---------------------------------------------------------------------------
// Turn outcome: decided from OpenCode's own messages.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
struct MessageView {
    id: String,
    role: String,
    parent_id: Option<String>,
    /// `stop`, `tool-calls`, ... once the model finished a step.
    finish: Option<String>,
    /// `(error name, message)` when the message ended in an error.
    error: Option<(String, String)>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum TurnOutcome {
    Running,
    Completed,
    Cancelled,
    /// Idle with work unfinished. Not yet a failure: it must persist for
    /// `STALL_GRACE`, so a gap between steps is not mistaken for the end.
    Stalled(String),
    Failed(String),
}

fn parse_messages(list: &Value) -> Vec<MessageView> {
    list.as_array()
        .map(|items| {
            items
                .iter()
                .filter_map(|item| {
                    let info = item.get("info")?;
                    let error = info
                        .get("error")
                        .filter(|error| !error.is_null())
                        .map(|error| {
                            let name = error
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("Error")
                                .to_string();
                            let message = error
                                .get("data")
                                .and_then(|data| data.get("message"))
                                .and_then(Value::as_str)
                                .or_else(|| error.get("message").and_then(Value::as_str))
                                .unwrap_or("")
                                .to_string();
                            (name, message)
                        });
                    Some(MessageView {
                        id: info.get("id")?.as_str()?.to_string(),
                        role: info.get("role")?.as_str()?.to_string(),
                        parent_id: info
                            .get("parentID")
                            .and_then(Value::as_str)
                            .map(str::to_string),
                        finish: info
                            .get("finish")
                            .and_then(Value::as_str)
                            .map(str::to_string),
                        error,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

/// Is the turn over, and how did it end?
///
/// The assistant messages that answer anything this turn submitted (the prompt
/// and, later, each steer) are the turn: after a steer the continuation is
/// parented at the steer message, not the prompt, so the parent id alone would
/// lose it. An errored answer ends the turn at once. Otherwise the turn is over
/// only when the session is idle and the last answer finished with `stop`;
/// `tool-calls` means another step is coming (idle then is `Stalled`, which the
/// caller times out), and no answer yet means the turn has not started (the
/// caller times that out too).
fn turn_outcome(messages: &[MessageView], submitted: &[String], busy: bool) -> TurnOutcome {
    let answers: Vec<&MessageView> = messages
        .iter()
        .filter(|message| {
            message.role == "assistant"
                && message
                    .parent_id
                    .as_ref()
                    .is_some_and(|parent| submitted.contains(parent))
        })
        .collect();
    if let Some((name, message)) = answers.iter().find_map(|message| message.error.as_ref()) {
        return if name == "MessageAbortedError" {
            TurnOutcome::Cancelled
        } else if message.is_empty() {
            TurnOutcome::Failed(format!("OpenCode provider error: {name}"))
        } else {
            TurnOutcome::Failed(format!("OpenCode provider error: {name}: {message}"))
        };
    }
    if busy {
        return TurnOutcome::Running;
    }
    match answers.last().map(|message| message.finish.as_deref()) {
        None => TurnOutcome::Running,
        Some(Some("stop")) => TurnOutcome::Completed,
        Some(Some("tool-calls")) | Some(None) => {
            TurnOutcome::Stalled("OpenCode went idle before it finished its work".to_string())
        }
        Some(Some(other)) => {
            TurnOutcome::Failed(format!("OpenCode ended the turn with finish={other}"))
        }
    }
}

/// The first user message that was not there before the prompt was posted.
fn new_user_message(messages: &[MessageView], known: &HashSet<String>) -> Option<String> {
    messages
        .iter()
        .find(|message| message.role == "user" && !known.contains(&message.id))
        .map(|message| message.id.clone())
}

/// `provider/model` -> the `{providerID, modelID}` a prompt carries.
fn model_ref(model: &str) -> Option<Value> {
    let (provider, model_id) = model.trim().split_once('/')?;
    (!provider.is_empty() && !model_id.is_empty())
        .then(|| json!({"providerID": provider, "modelID": model_id}))
}

fn mime_for_image(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        _ => "application/octet-stream",
    }
}

fn prompt_body(config: &OpenCodeRunConfig) -> Result<Value> {
    let images: Vec<crate::input_attachments::StagedAttachment> = config
        .image_paths
        .iter()
        .map(|path| crate::input_attachments::StagedAttachment {
            path: path.clone(),
            mime_type: mime_for_image(path).to_string(),
        })
        .collect();
    // Attachment-only turns send no blank text part.
    let text = config.prompt.trim();
    let parts = crate::opencode_control::prompt_parts(text, &images)?;
    let mut body = json!({ "parts": parts });
    if let Some(model) = normalized_optional(&config.model)
        .as_deref()
        .and_then(model_ref)
    {
        body["model"] = model;
    }
    Ok(body)
}

// ---------------------------------------------------------------------------
// Live progress: native events -> the stream events the server already reads.
// ---------------------------------------------------------------------------

enum Action {
    /// A preview event for the timeline: `{type, timestamp, sessionID, part}`.
    Stream(Value),
    /// A permission is pending: Console is bypass-only, so it is approved.
    ApprovePermission(String),
    /// A question has no human to answer it: it is rejected, never left hanging.
    RejectQuestion(String),
    /// Something that can end the turn happened: reconcile now.
    Reconcile,
}

struct Projector {
    session_id: String,
    /// message id -> role, from `message.updated`.
    roles: HashMap<String, String>,
    /// (part id, status) already emitted, so a repeated update is one event.
    emitted: HashSet<(String, String)>,
    text: HashMap<String, String>,
    last_text_emit: HashMap<String, Instant>,
}

impl Projector {
    fn new(session_id: &str) -> Self {
        Self {
            session_id: session_id.to_string(),
            roles: HashMap::new(),
            emitted: HashSet::new(),
            text: HashMap::new(),
            last_text_emit: HashMap::new(),
        }
    }

    fn is_ours(&self, value: Option<&Value>) -> bool {
        value.and_then(Value::as_str) == Some(self.session_id.as_str())
    }

    fn assistant_message(&self, message_id: Option<&str>) -> bool {
        // A part can outrun its message.updated; only a known user message is
        // excluded.
        message_id.is_none_or(|id| self.roles.get(id).map(String::as_str) != Some("user"))
    }

    /// True the first time `(part, key)` is delivered, false after.
    fn mark_delivered(&mut self, part_id: &str, key: &str) -> bool {
        self.emitted.insert((part_id.to_string(), key.to_string()))
    }

    /// A restarted engine has not seen what the previous one already logged;
    /// learn it from the stream log so backfill never repeats it.
    fn seed_from_log(&mut self, path: &Path) {
        let Ok(text) = std::fs::read_to_string(path) else {
            return;
        };
        for line in text.lines() {
            let Ok(event) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let Some(part) = event.get("part") else {
                continue;
            };
            let Some(id) = part.get("id").and_then(Value::as_str) else {
                continue;
            };
            let key = match event.get("type").and_then(Value::as_str) {
                Some("step_start") => "step_start".to_string(),
                Some("step_finish") => "step_finish".to_string(),
                Some("tool_use") => part
                    .get("state")
                    .and_then(|state| state.get("status"))
                    .and_then(Value::as_str)
                    .unwrap_or("pending")
                    .to_string(),
                Some("text")
                    if part
                        .get("time")
                        .and_then(|time| time.get("end"))
                        .is_some_and(|end| !end.is_null()) =>
                {
                    "final".to_string()
                }
                _ => continue,
            };
            self.mark_delivered(id, &key);
        }
    }

    fn stream_event(kind: &str, part: &Value) -> Action {
        Action::Stream(json!({
            "type": kind,
            "timestamp": Utc::now().timestamp_millis(),
            "sessionID": part.get("sessionID"),
            "part": part,
        }))
    }

    fn interpret(&mut self, frame: &Value, now: Instant) -> Vec<Action> {
        let props = frame.get("properties").unwrap_or(&Value::Null);
        match frame.get("type").and_then(Value::as_str) {
            Some("message.updated") => {
                let Some(info) = props.get("info") else {
                    return Vec::new();
                };
                if !self.is_ours(info.get("sessionID")) {
                    return Vec::new();
                }
                if let (Some(id), Some(role)) = (
                    info.get("id").and_then(Value::as_str),
                    info.get("role").and_then(Value::as_str),
                ) {
                    self.roles.insert(id.to_string(), role.to_string());
                }
                let finished = info
                    .get("time")
                    .and_then(|time| time.get("completed"))
                    .is_some_and(|completed| !completed.is_null());
                if finished {
                    vec![Action::Reconcile]
                } else {
                    Vec::new()
                }
            }
            Some("message.part.updated") => {
                let Some(part) = props.get("part") else {
                    return Vec::new();
                };
                if !self.is_ours(part.get("sessionID"))
                    || !self.assistant_message(part.get("messageID").and_then(Value::as_str))
                {
                    return Vec::new();
                }
                let part_id = part.get("id").and_then(Value::as_str).unwrap_or_default();
                match part.get("type").and_then(Value::as_str) {
                    Some("step-start") if self.mark_delivered(part_id, "step_start") => {
                        vec![Self::stream_event("step_start", part)]
                    }
                    Some("step-finish") if self.mark_delivered(part_id, "step_finish") => {
                        vec![Self::stream_event("step_finish", part)]
                    }
                    Some("text") => {
                        if let Some(text) = part.get("text").and_then(Value::as_str) {
                            self.text.insert(part_id.to_string(), text.to_string());
                        }
                        let finished = part
                            .get("time")
                            .and_then(|time| time.get("end"))
                            .is_some_and(|end| !end.is_null());
                        if finished {
                            self.mark_delivered(part_id, "final");
                        }
                        vec![Self::stream_event("text", part)]
                    }
                    Some("tool") => {
                        let status = part
                            .get("state")
                            .and_then(|state| state.get("status"))
                            .and_then(Value::as_str)
                            .unwrap_or("pending")
                            .to_string();
                        if self.mark_delivered(part_id, &status) {
                            vec![Self::stream_event("tool_use", part)]
                        } else {
                            Vec::new()
                        }
                    }
                    _ => Vec::new(),
                }
            }
            Some("message.part.delta") => {
                if !self.is_ours(props.get("sessionID"))
                    || props.get("field").and_then(Value::as_str) != Some("text")
                    || !self.assistant_message(props.get("messageID").and_then(Value::as_str))
                {
                    return Vec::new();
                }
                let (Some(part_id), Some(delta)) = (
                    props.get("partID").and_then(Value::as_str),
                    props.get("delta").and_then(Value::as_str),
                ) else {
                    return Vec::new();
                };
                let text = self.text.entry(part_id.to_string()).or_default();
                text.push_str(delta);
                let due = self
                    .last_text_emit
                    .get(part_id)
                    .is_none_or(|last| now.duration_since(*last) >= TEXT_PREVIEW_EVERY);
                if !due {
                    return Vec::new();
                }
                self.last_text_emit.insert(part_id.to_string(), now);
                let part = json!({
                    "id": part_id,
                    "messageID": props.get("messageID"),
                    "sessionID": self.session_id,
                    "type": "text",
                    "text": text,
                });
                vec![Self::stream_event("text", &part)]
            }
            Some("session.idle") | Some("session.error") => {
                if self.is_ours(props.get("sessionID")) || props.get("sessionID").is_none() {
                    vec![Action::Reconcile]
                } else {
                    Vec::new()
                }
            }
            Some("session.status") => {
                let idle = props
                    .get("status")
                    .and_then(|status| status.get("type"))
                    .and_then(Value::as_str)
                    == Some("idle");
                if idle && self.is_ours(props.get("sessionID")) {
                    vec![Action::Reconcile]
                } else {
                    Vec::new()
                }
            }
            // This server exists for this one turn: any permission it asks for,
            // including a subagent's, is the turn's own.
            Some("permission.asked") => props
                .get("id")
                .and_then(Value::as_str)
                .map(|id| vec![Action::ApprovePermission(id.to_string())])
                .unwrap_or_default(),
            Some("question.asked") => props
                .get("id")
                .and_then(Value::as_str)
                .map(|id| vec![Action::RejectQuestion(id.to_string())])
                .unwrap_or_default(),
            _ => Vec::new(),
        }
    }
}

/// What the event pump hands the turn: a frame, or a note that the stream
/// (re)connected so the turn re-reads state it may have missed.
enum Frame {
    Event(Value),
    Connected,
}

async fn pump_events(server: OpenCodeServer, frames: mpsc::Sender<Frame>) {
    loop {
        if let Ok(mut response) = server.events().await {
            if frames.send(Frame::Connected).await.is_err() {
                return;
            }
            let mut pending = String::new();
            while let Ok(Some(chunk)) = response.chunk().await {
                pending.push_str(&String::from_utf8_lossy(&chunk));
                while let Some((boundary, delimiter_len)) =
                    crate::opencode_bridge::sse_frame_boundary(&pending)
                {
                    let frame = pending[..boundary].to_string();
                    pending.drain(..boundary + delimiter_len);
                    for line in frame.lines() {
                        let Some(data) = line.strip_prefix("data:") else {
                            continue;
                        };
                        if let Ok(event) = serde_json::from_str::<Value>(data.trim()) {
                            if frames.send(Frame::Event(event)).await.is_err() {
                                return;
                            }
                        }
                    }
                }
            }
        }
        if frames.is_closed() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(300)).await;
    }
}

/// The stream events Longhouse projected, one JSON object per line: the
/// provider-response evidence a run retains as its `stdout`.
struct StreamLog {
    file: File,
    seq: u64,
}

impl StreamLog {
    fn open(path: &Path) -> Result<Self> {
        Ok(Self {
            file: private_output_file(path)?,
            seq: 0,
        })
    }

    fn append(&mut self, event: &Value) -> Result<u64> {
        self.seq += 1;
        let mut line = serde_json::to_vec(event)?;
        line.push(b'\n');
        self.file.write_all(&line)?;
        Ok(self.seq)
    }

    /// Continue a log a previous engine started: append, and count its lines.
    fn reopen(path: &Path) -> Result<Self> {
        let seq = std::fs::read(path)
            .map(|bytes| bytes.iter().filter(|byte| **byte == b'\n').count() as u64)
            .unwrap_or(0);
        Ok(Self {
            file: OpenOptions::new()
                .create(true)
                .append(true)
                .mode(0o600)
                .open(path)?,
            seq,
        })
    }

    fn bytes(&self) -> u64 {
        self.file.metadata().map(|meta| meta.len()).unwrap_or(0)
    }
}

struct SpawnedServer {
    child: Child,
    pid: u32,
    process_group_id: i32,
    server: OpenCodeServer,
    argv: Vec<String>,
}

/// Start `opencode serve` in its own process group on a fresh loopback port
/// with a fresh password. Output goes to the given files.
fn spawn_server(
    opencode_bin: &str,
    cwd: &Path,
    stdout: File,
    stderr: File,
    longhouse_session_id: &str,
    run_id: &str,
) -> Result<SpawnedServer> {
    let port = {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0))
            .context("could not reserve a loopback port for OpenCode")?;
        listener.local_addr()?.port()
    };
    let password = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
    let args = [
        "serve".to_string(),
        "--hostname".to_string(),
        "127.0.0.1".to_string(),
        "--port".to_string(),
        port.to_string(),
        "--pure".to_string(),
    ];
    let mut command = Command::new(opencode_bin);
    command
        .args(&args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr))
        // The user's own OpenCode configuration applies; only the server
        // credentials are Longhouse's.
        .env_remove("OPENCODE_CONFIG")
        .env_remove("OPENCODE_CONFIG_CONTENT")
        .env("OPENCODE_SERVER_USERNAME", crate::opencode_server::USERNAME)
        .env("OPENCODE_SERVER_PASSWORD", &password);
    ManagedIdentity::new(ManagedProvider::Opencode, longhouse_session_id)
        .with_run_id(run_id)
        .apply(&mut command, &[]);
    #[cfg(unix)]
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let child = command
        .spawn()
        .with_context(|| format!("spawning `{opencode_bin} serve`"))?;
    let pid = child.id().context("opencode serve returned no pid")?;
    let process_group_id =
        i32::try_from(pid).context("OpenCode pid exceeds process-group range")?;
    let mut argv = vec![opencode_bin.to_string()];
    argv.extend(args);
    Ok(SpawnedServer {
        child,
        pid,
        process_group_id,
        server: OpenCodeServer {
            url: format!("http://127.0.0.1:{port}"),
            username: crate::opencode_server::USERNAME.to_string(),
            password,
            directory: cwd.to_string_lossy().to_string(),
        },
        argv,
    })
}

// ---------------------------------------------------------------------------
// Starting, following and finishing a turn.
// ---------------------------------------------------------------------------

pub async fn start_opencode_run_turn(config: OpenCodeRunConfig) -> Result<OpenCodeRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = normalized_optional(&config.turn_id) {
        validate_uuid(&turn_id, "turn_id")?;
    }
    if config.permission_mode != "bypass" {
        anyhow::bail!(
            "OpenCode Console supports permission_mode=bypass only; remote approval is unavailable"
        );
    }
    let resume_provider_thread_id = normalized_optional(&config.resume_provider_thread_id);
    if let Some(value) = resume_provider_thread_id.as_deref() {
        validate_provider_thread_id(value)?;
    }

    let launch_id = Uuid::new_v4().to_string();
    let state_root = opencode_console_root()?;
    let lock_key = resume_provider_thread_id
        .as_deref()
        .unwrap_or(&config.session_id);
    let lock = acquire_turn_lock(&state_root, lock_key)?;
    reserve_binding(
        &state_root,
        &config.session_id,
        &config.thread_id,
        config.turn_id.as_deref(),
        &config.run_id,
        config.client_request_id.as_deref(),
        resume_provider_thread_id.as_deref(),
        &launch_id,
    )?;

    match start_reserved_turn(&config, resume_provider_thread_id, launch_id.clone(), lock).await {
        Ok(summary) => Ok(summary),
        Err(error) => {
            rollback_binding(&state_root, &config.session_id, &launch_id);
            Err(error)
        }
    }
}

async fn start_reserved_turn(
    config: &OpenCodeRunConfig,
    resume_provider_thread_id: Option<String>,
    launch_id: String,
    lock: File,
) -> Result<OpenCodeRunSummary> {
    let run_dir = crate::config::get_agent_dir()?
        .join("opencode-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    // `stdout.jsonl` is the provider-response evidence: the stream events
    // Longhouse projected, one per line. The server's own output is separate.
    let stdout_path = run_dir.join("stdout.jsonl");
    let stderr_path = run_dir.join("stderr.log");
    let stream = StreamLog::open(&stdout_path)?;
    let spawned = spawn_server(
        &config.opencode_bin,
        &config.cwd,
        private_output_file(&run_dir.join("server.log"))?,
        private_output_file(&stderr_path)?,
        &config.session_id,
        &config.run_id,
    )?;
    let SpawnedServer {
        child,
        pid,
        process_group_id,
        server,
        argv,
    } = spawned;
    let started = async {
        let state = ServeState {
            server_url: server.url.clone(),
            username: server.username.clone(),
            password: server.password.clone(),
            directory: server.directory.clone(),
            provider_session_id: resume_provider_thread_id.clone(),
            message_ids: Vec::new(),
        };
        state.save(&run_dir)?;
        let model = normalized_optional(&config.model);
        let result = json!({
            "session_id": config.session_id,
            "thread_id": config.thread_id,
            "run_id": config.run_id,
            "provider": "opencode",
            "transport": OPENCODE_RUN_ADAPTER,
            "provider_thread_id": resume_provider_thread_id,
            "launch_id": launch_id,
            "pid": pid,
            "process_group_id": process_group_id,
            "stdout_path": stdout_path,
            "stderr_path": stderr_path,
            "cwd": config.cwd,
            "machine_name": config.machine_name,
            "argv": argv,
            "model": model,
            "server_url": server.url,
        });
        crate::turn_claims::default_registry()?.mark_spawned_invocation(
            &config.run_id,
            pid,
            process_group_id,
            crate::turn_claims::process_start_time_for_pid(Some(pid)),
            OPENCODE_RUN_ADAPTER,
            &launch_id,
            resume_provider_thread_id.as_deref(),
            &stdout_path.to_string_lossy(),
            &stderr_path.to_string_lossy(),
            result,
        )?;
        anyhow::Ok(state)
    }
    .await;
    let state = match started {
        Ok(state) => state,
        Err(error) => {
            cleanup_process_group(Some(process_group_id)).await;
            return Err(error);
        }
    };
    let sink = OpenCodeRunSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        expected_provider_thread_id: resume_provider_thread_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        machine_name: config.machine_name.clone(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
    };
    let turn_config = config.clone();
    tokio::spawn(async move {
        drive_turn(
            child,
            server,
            state,
            run_dir,
            stream,
            sink,
            turn_config,
            lock,
        )
        .await;
    });
    Ok(OpenCodeRunSummary {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        run_id: config.run_id.clone(),
        provider_thread_id: resume_provider_thread_id,
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        argv,
    })
}

#[allow(clippy::too_many_arguments)]
async fn drive_turn(
    mut child: Child,
    server: OpenCodeServer,
    mut state: ServeState,
    run_dir: PathBuf,
    mut stream: StreamLog,
    sink: OpenCodeRunSink,
    config: OpenCodeRunConfig,
    _lock: File,
) {
    sink.post_phase("thinking", None).await;
    let (frames_tx, mut frames) = mpsc::channel::<Frame>(256);
    let mut pump = None;
    let begun = begin_turn(
        &mut child, &server, &mut state, &run_dir, &sink, &config, &frames_tx, &mut pump,
    )
    .await;
    let (provider_session_id, posted_at) = match begun {
        Ok(begun) => begun,
        Err(error) => {
            if let Some(pump) = pump {
                pump.abort();
            }
            let session = state.provider_session_id.clone();
            finish_turn(
                &server,
                &sink,
                &mut stream,
                &mut Projector::new(session.as_deref().unwrap_or_default()),
                Some(child),
                session.as_deref(),
                &[],
                TurnOutcome::Failed(format!("{error:#}")),
                &run_dir,
            )
            .await;
            return;
        }
    };
    let mut projector = Projector::new(&provider_session_id);
    let outcome = follow_turn(
        Some(&mut child),
        &server,
        &sink,
        &mut stream,
        &mut projector,
        &mut frames,
        &provider_session_id,
        &state.message_ids,
        posted_at,
        &run_dir,
    )
    .await;
    if let Some(pump) = pump {
        pump.abort();
    }
    finish_turn(
        &server,
        &sink,
        &mut stream,
        &mut projector,
        Some(child),
        Some(&provider_session_id),
        &state.message_ids,
        outcome,
        &run_dir,
    )
    .await;
}

/// Readiness, the native session, the subscription, the prompt. Returns the
/// native session id and when the prompt was accepted.
#[allow(clippy::too_many_arguments)]
async fn begin_turn(
    child: &mut Child,
    server: &OpenCodeServer,
    state: &mut ServeState,
    run_dir: &Path,
    sink: &OpenCodeRunSink,
    config: &OpenCodeRunConfig,
    frames: &mpsc::Sender<Frame>,
    pump: &mut Option<tokio::task::JoinHandle<()>>,
) -> Result<(String, Instant)> {
    crate::opencode_server::wait_ready(server, child).await?;
    let mut known = HashSet::new();
    let provider_session_id = match normalized_optional(&config.resume_provider_thread_id) {
        Some(id) => {
            // Resume never creates a session behind the user's back.
            match server.get(&format!("/session/{id}")).await {
                Ok(_) => {}
                Err(error) if http_status(&error) == Some(404) => {
                    anyhow::bail!("OpenCode session {id} does not exist in this data directory")
                }
                Err(error) => return Err(error),
            }
            let existing = server.get(&format!("/session/{id}/message")).await?;
            known.extend(
                parse_messages(&existing)
                    .into_iter()
                    .map(|message| message.id),
            );
            id
        }
        None => {
            let created = server
                .post("/session", Some(json!({"title": "Longhouse Console"})))
                .await?;
            let id = created
                .get("id")
                .and_then(Value::as_str)
                .context("OpenCode created a session without an id")?
                .to_string();
            validate_provider_thread_id(&id)?;
            promote_binding(sink, &id)?;
            crate::turn_claims::default_registry()?.mark_provider_binding(
                &sink.run_id,
                &id,
                None,
            )?;
            sink.post_binding(&id).await;
            id
        }
    };
    state.provider_session_id = Some(provider_session_id.clone());
    state.save(run_dir)?;

    // Subscribe before submitting, so the first step's progress is not missed.
    *pump = Some(tokio::spawn(pump_events(server.clone(), frames.clone())));

    server
        .post(
            &format!("/session/{provider_session_id}/prompt_async"),
            Some(prompt_body(config)?),
        )
        .await
        .context("OpenCode did not accept the prompt")?;
    let posted_at = Instant::now();
    // OpenCode assigns the user message id; the session has one execution owner,
    // so the first user message that was not there before is this turn's.
    let deadline = Instant::now() + Duration::from_secs(10);
    let message_id = loop {
        let list = server
            .get(&format!("/session/{provider_session_id}/message"))
            .await?;
        if let Some(id) = new_user_message(&parse_messages(&list), &known) {
            break id;
        }
        anyhow::ensure!(
            Instant::now() < deadline,
            "OpenCode accepted the prompt but never recorded it"
        );
        tokio::time::sleep(Duration::from_millis(150)).await;
    };
    state.message_ids.push(message_id);
    state.save(run_dir)?;
    Ok((provider_session_id, posted_at))
}

fn http_status(error: &anyhow::Error) -> Option<u16> {
    error
        .downcast_ref::<HttpStatusError>()
        .map(|error| error.status)
}

async fn observe(server: &OpenCodeServer, session_id: &str) -> Result<(Vec<MessageView>, bool)> {
    let list = server
        .get(&format!("/session/{session_id}/message"))
        .await?;
    let statuses = server.get("/session/status").await?;
    // The map lists only sessions that are doing something; absent is idle.
    let busy = statuses
        .get(session_id)
        .and_then(|status| status.get("type"))
        .and_then(Value::as_str)
        .is_some_and(|kind| kind != "idle");
    Ok((parse_messages(&list), busy))
}

/// Approve a permission once; each request id is answered at most once.
fn approve(server: &OpenCodeServer, replied: &mut HashSet<String>, id: String) {
    if replied.insert(id.clone()) {
        spawn_reply(
            server,
            format!("/permission/{id}/reply"),
            Some(json!({"reply": "once"})),
        );
    }
}

/// Reject a question: there is no human to answer it, and it must not hang the turn.
fn reject(server: &OpenCodeServer, replied: &mut HashSet<String>, id: String) {
    if replied.insert(id.clone()) {
        spawn_reply(server, format!("/question/{id}/reject"), None);
    }
}

/// Answer whatever the server is waiting on, asking it directly.
async fn answer_pending(server: &OpenCodeServer, replied: &mut HashSet<String>) {
    for (path, is_permission) in [("/permission", true), ("/question", false)] {
        let Ok(pending) = server.get(path).await else {
            continue;
        };
        for id in pending
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|request| request.get("id").and_then(Value::as_str))
        {
            if is_permission {
                approve(server, replied, id.to_string());
            } else {
                reject(server, replied, id.to_string());
            }
        }
    }
}

fn spawn_reply(server: &OpenCodeServer, path: String, body: Option<Value>) {
    let server = server.clone();
    tokio::spawn(async move {
        if let Err(error) = server.post(&path, body).await {
            eprintln!("[opencode-run] {path} was not answered: {error:#}");
        }
    });
}

async fn emit(
    stream: &mut StreamLog,
    sink: &OpenCodeRunSink,
    event: Value,
    provider_session_id: &str,
) {
    match stream.append(&event) {
        Ok(seq) => {
            sink.post_stream_event(seq, event, Some(provider_session_id))
                .await;
            if let Ok(registry) = crate::turn_claims::default_registry() {
                let _ = registry.mark_projection_checkpoint(&sink.run_id, stream.bytes(), seq);
            }
        }
        Err(error) => eprintln!("[opencode-run] stream log write failed: {error}"),
    }
}

/// Follow the turn until OpenCode's own state says it is over. Returns how it
/// ended; never returns `Running`.
#[allow(clippy::too_many_arguments)]
async fn follow_turn(
    mut child: Option<&mut Child>,
    server: &OpenCodeServer,
    sink: &OpenCodeRunSink,
    stream: &mut StreamLog,
    projector: &mut Projector,
    frames: &mut mpsc::Receiver<Frame>,
    provider_session_id: &str,
    submitted: &[String],
    posted_at: Instant,
    run_dir: &Path,
) -> TurnOutcome {
    let mut ticker = tokio::time::interval(RECONCILE_EVERY);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut unreachable_since: Option<Instant> = None;
    let mut stalled_since: Option<Instant> = None;
    let mut replied = HashSet::new();
    loop {
        let mut reconcile = false;
        tokio::select! {
            frame = frames.recv() => match frame {
                Some(Frame::Event(event)) => {
                    for action in projector.interpret(&event, Instant::now()) {
                        match action {
                            Action::Stream(event) => {
                                emit(stream, sink, event, provider_session_id).await;
                            }
                            Action::ApprovePermission(id) => approve(server, &mut replied, id),
                            Action::RejectQuestion(id) => reject(server, &mut replied, id),
                            Action::Reconcile => reconcile = true,
                        }
                    }
                }
                Some(Frame::Connected) => reconcile = true,
                None => {}
            },
            _ = ticker.tick() => reconcile = true,
        }
        if let Some(child) = child.as_deref_mut() {
            if let Ok(Some(status)) = child.try_wait() {
                let tail = stderr_tail(&run_dir.join("stderr.log"))
                    .map(|tail| format!(": {tail}"))
                    .unwrap_or_default();
                return TurnOutcome::Failed(format!(
                    "OpenCode server exited during the turn ({status}){tail}"
                ));
            }
        }
        if !reconcile {
            continue;
        }
        // The stream delivers permission and question requests, but a dropped
        // stream or a restarted engine must not leave one waiting for ever.
        answer_pending(server, &mut replied).await;
        match observe(server, provider_session_id).await {
            Ok((messages, busy)) => {
                unreachable_since = None;
                let outcome = turn_outcome(&messages, submitted, busy);
                if !matches!(outcome, TurnOutcome::Stalled(_)) {
                    stalled_since = None;
                }
                match outcome {
                    TurnOutcome::Running => {
                        let answered = messages.iter().any(|message| {
                            message.role == "assistant"
                                && message
                                    .parent_id
                                    .as_ref()
                                    .is_some_and(|parent| submitted.contains(parent))
                        });
                        if !busy && !answered && posted_at.elapsed() > START_TIMEOUT {
                            return TurnOutcome::Failed(
                                "OpenCode accepted the prompt but never started the turn"
                                    .to_string(),
                            );
                        }
                    }
                    TurnOutcome::Stalled(reason) => {
                        let since = *stalled_since.get_or_insert_with(Instant::now);
                        if since.elapsed() >= STALL_GRACE {
                            return TurnOutcome::Failed(reason);
                        }
                    }
                    settled => return settled,
                }
            }
            Err(error) => {
                let since = *unreachable_since.get_or_insert_with(Instant::now);
                if since.elapsed() > SERVER_DOWN_AFTER {
                    return TurnOutcome::Failed(format!(
                        "OpenCode server became unreachable: {error:#}"
                    ));
                }
            }
        }
    }
}

/// Anything OpenCode recorded for this turn that the event stream did not
/// deliver (a dropped connection, a turn that finished before it connected), so
/// the retained response evidence never depends on the stream.
fn backfill(list: &Value, submitted: &[String], projector: &mut Projector) -> Vec<Value> {
    let mut events = Vec::new();
    for item in list.as_array().into_iter().flatten() {
        let Some(info) = item.get("info") else {
            continue;
        };
        let answers = info.get("role").and_then(Value::as_str) == Some("assistant")
            && info
                .get("parentID")
                .and_then(Value::as_str)
                .is_some_and(|parent| submitted.iter().any(|id| id == parent));
        if !answers {
            continue;
        }
        for part in item
            .get("parts")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let id = part.get("id").and_then(Value::as_str).unwrap_or_default();
            let kind = match part.get("type").and_then(Value::as_str) {
                Some("step-start") => "step_start",
                Some("step-finish") => "step_finish",
                Some("text") => "text",
                Some("tool") => "tool_use",
                _ => continue,
            };
            let key = match kind {
                "tool_use" => part
                    .get("state")
                    .and_then(|state| state.get("status"))
                    .and_then(Value::as_str)
                    .unwrap_or("pending")
                    .to_string(),
                "text" => "final".to_string(),
                other => other.to_string(),
            };
            if kind == "text"
                && part
                    .get("text")
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
            {
                continue;
            }
            if projector.mark_delivered(id, &key) {
                if let Action::Stream(event) = Projector::stream_event(kind, part) {
                    events.push(event);
                }
            }
        }
    }
    events
}

#[allow(clippy::too_many_arguments)]
async fn finish_turn(
    server: &OpenCodeServer,
    sink: &OpenCodeRunSink,
    stream: &mut StreamLog,
    projector: &mut Projector,
    child: Option<Child>,
    provider_session_id: Option<&str>,
    submitted: &[String],
    outcome: TurnOutcome,
    run_dir: &Path,
) {
    if let Some(session_id) = provider_session_id {
        if let Ok(list) = server.get(&format!("/session/{session_id}/message")).await {
            for event in backfill(&list, submitted, projector) {
                emit(stream, sink, event, session_id).await;
            }
        }
    }
    // The server dies with the turn: stop its whole group, verified. The group
    // leader is our child and stays a zombie until reaped, which would keep the
    // group "alive" for the check, so reap it while the group is being stopped.
    let reap = async {
        if let Some(mut child) = child {
            let _ = tokio::time::timeout(Duration::from_secs(10), child.wait()).await;
        }
    };
    tokio::join!(cleanup_process_group(sink.process_group_id), reap);
    let cancel_requested = crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(&sink.run_id))
        .ok()
        .and_then(|claim| claim.cancel_requested_at)
        .is_some();
    let (terminal, error) = match outcome {
        TurnOutcome::Completed => ("run_completed", None),
        TurnOutcome::Cancelled => ("run_cancelled", None),
        TurnOutcome::Failed(_) if cancel_requested => ("run_cancelled", None),
        TurnOutcome::Failed(reason) => ("run_failed", Some(reason)),
        TurnOutcome::Running | TurnOutcome::Stalled(_) => (
            "run_failed",
            Some("OpenCode turn ended without an outcome".to_string()),
        ),
    };
    let error = error.map(|reason| {
        // A server that died leaves its reason on stderr.
        match stderr_tail(&run_dir.join("stderr.log")) {
            Some(tail) if !reason.contains(&tail) => format!("{reason}: {tail}"),
            _ => reason,
        }
    });
    sink.post_terminal(terminal, None, error, provider_session_id)
        .await;
}

// ---------------------------------------------------------------------------
// Recovery and interruption.
// ---------------------------------------------------------------------------

pub async fn recover_opencode_run_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    // One coherent inventory for the pass; `None` means `ps` was unreadable,
    // which must leave claims alone rather than settle them.
    let inventory = crate::process_identity::try_collect_process_facts_by_pid();
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        if claim.adapter.as_deref() != Some(OPENCODE_RUN_ADAPTER) || claim.state != "spawned" {
            continue;
        }
        let Some(run_dir) = claim
            .stdout_path
            .as_deref()
            .map(PathBuf::from)
            .and_then(|path| path.parent().map(Path::to_path_buf))
        else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("OpenCode Console claim has no run directory".to_string()),
            );
            continue;
        };
        let sink = OpenCodeRunSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            expected_provider_thread_id: claim.provider_thread_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            machine_name: machine_name.to_string(),
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        match crate::console_adapter::claim_liveness(&claim, inventory.as_ref()) {
            ClaimLiveness::Live => {
                let lock_key = claim
                    .provider_thread_id
                    .as_deref()
                    .unwrap_or(&claim.session_id);
                let lock = acquire_turn_lock(&opencode_console_root()?, lock_key)?;
                tokio::spawn(async move {
                    recover_turn(run_dir, sink, lock).await;
                });
                recovered += 1;
            }
            ClaimLiveness::Gone => {
                sink.post_terminal(
                    "run_failed",
                    None,
                    Some("OpenCode server is gone; the turn can no longer be observed".to_string()),
                    claim.provider_thread_id.as_deref(),
                )
                .await;
            }
            ClaimLiveness::Unknown => tracing::warn!(
                run_id = %claim.run_id,
                "Process inventory unavailable; leaving OpenCode Console turn claim for a later scan"
            ),
        }
    }
    Ok(recovered)
}

/// Reconnect to a turn whose server outlived the engine and settle it from what
/// OpenCode recorded. Nothing is resubmitted: the prompt was either accepted
/// (its message id is in the server state) or the turn never began.
async fn recover_turn(run_dir: PathBuf, sink: OpenCodeRunSink, _lock: File) {
    let state = match ServeState::load(&run_dir) {
        Ok(state) => state,
        Err(error) => {
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal("run_failed", None, Some(format!("{error:#}")), None)
                .await;
            return;
        }
    };
    let server = state.server();
    let session_id = state
        .provider_session_id
        .clone()
        .or_else(|| sink.expected_provider_thread_id.clone());
    let mut stream = match StreamLog::reopen(&run_dir.join("stdout.jsonl")) {
        Ok(stream) => stream,
        Err(error) => {
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal("run_failed", None, Some(format!("{error:#}")), None)
                .await;
            return;
        }
    };
    let Some(session_id) = session_id.filter(|_| !state.message_ids.is_empty()) else {
        finish_turn(
            &server,
            &sink,
            &mut stream,
            &mut Projector::new(""),
            None,
            None,
            &[],
            TurnOutcome::Failed("the engine restarted before the prompt was submitted".to_string()),
            &run_dir,
        )
        .await;
        return;
    };
    let (frames_tx, mut frames) = mpsc::channel::<Frame>(256);
    let pump = tokio::spawn(pump_events(server.clone(), frames_tx));
    let mut projector = Projector::new(&session_id);
    projector.seed_from_log(&run_dir.join("stdout.jsonl"));
    let outcome = follow_turn(
        None,
        &server,
        &sink,
        &mut stream,
        &mut projector,
        &mut frames,
        &session_id,
        &state.message_ids,
        Instant::now(),
        &run_dir,
    )
    .await;
    pump.abort();
    finish_turn(
        &server,
        &sink,
        &mut stream,
        &mut projector,
        None,
        Some(&session_id),
        &state.message_ids,
        outcome,
        &run_dir,
    )
    .await;
}

/// Stop the running turn: ask OpenCode to abort it. The turn's monitor then
/// sees the aborted reply and settles the run as cancelled. If the server
/// cannot be asked, the identity-checked process group is interrupted instead.
pub async fn interrupt_opencode_run_turn(
    run_id: &str,
    session_id: &str,
    thread_id: &str,
    turn_id: &str,
) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id
        || claim.thread_id != thread_id
        || claim.turn_id.as_deref() != Some(turn_id)
        || claim.provider != "opencode"
    {
        anyhow::bail!(
            "OpenCode Console turn claim does not match the requested session, thread, or turn"
        );
    }
    if claim.adapter.as_deref() != Some(OPENCODE_RUN_ADAPTER) || claim.state != "spawned" {
        anyhow::bail!("OpenCode Console turn is not active");
    }
    let run_dir = claim
        .stdout_path
        .as_deref()
        .map(PathBuf::from)
        .and_then(|path| path.parent().map(Path::to_path_buf))
        .context("OpenCode Console turn has no run directory")?;
    let served = ServeState::load(&run_dir).ok().and_then(|state| {
        let session = state.provider_session_id.clone()?;
        Some((state, session))
    });
    let Some((state, session)) = served else {
        // Nothing to ask (no native session yet, or the server state is
        // unreadable): stop the identity-checked process group instead.
        return interrupt_server_group(&registry, &claim);
    };
    registry.mark_cancel_requested(run_id)?;
    match state
        .server()
        .post(&format!("/session/{session}/abort"), None)
        .await
    {
        Ok(_) => Ok(()),
        // The server answered and refused: that is the answer.
        Err(error) if http_status(&error).is_some() => Err(error),
        Err(error) => {
            // The abort can land and the turn's own monitor can settle the run and
            // stop the server before the reply is written, which drops this
            // connection. A run that has settled is a run that stopped.
            for _ in 0..20 {
                if registry
                    .read(run_id)
                    .map_or(true, |claim| claim.state != "spawned")
                {
                    return Ok(());
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            // The server did not answer and the run is still going: stop the
            // process group it lives in.
            eprintln!(
                "[opencode-run] abort request failed, interrupting the process group: {error:#}"
            );
            interrupt_server_group(&registry, &claim)
        }
    }
}

fn interrupt_server_group(
    registry: &crate::turn_claims::TurnClaimRegistry,
    claim: &crate::turn_claims::TurnClaim,
) -> Result<()> {
    let pid = claim
        .pid
        .context("OpenCode Console turn has no provider pid")?;
    let expected_start = claim
        .process_start_time
        .as_deref()
        .context("OpenCode Console turn has no process-start identity")?;
    let actual = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .cloned()
        .context("OpenCode Console provider process is gone")?;
    if actual.lstart != expected_start {
        anyhow::bail!("OpenCode Console provider pid identity changed");
    }
    let pgid = claim
        .process_group_id
        .context("OpenCode Console turn has no process-group identity")?;
    let actual_pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
    if actual_pgid != pgid || crate::process_group::leader_group_for(pid) != Some(pgid) {
        anyhow::bail!("OpenCode Console provider process-group identity changed");
    }
    // Only a group whose identity was just proven is signalled or marked.
    registry.mark_cancel_requested(&claim.run_id)?;
    if unsafe { libc::killpg(pgid, libc::SIGINT) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting OpenCode Console process group");
        }
    }
    Ok(())
}

/// A stand-in for `opencode serve`, speaking the slice of the v1 API the adapter
/// uses, for tests that drive the real adapter end to end (here and in the
/// control channel). Modes, from `OPENCODE_FAKE_MODE`: `ok` answers at once,
/// `error` answers with a provider error, `missing` has no such session,
/// `permission` waits for a permission reply, `hold` waits for an abort.
/// Requests are appended to `OPENCODE_FAKE_LOG`.
#[cfg(test)]
pub(crate) mod fake_server {
    pub(crate) const SESSION: &str = "ses_console_test";
    pub(crate) const SCRIPT: &str = r##"#!/usr/bin/env python3
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[sys.argv.index("--port") + 1])
SES = "ses_console_test"
mode = os.environ.get("OPENCODE_FAKE_MODE", "ok")
log_path = os.environ.get("OPENCODE_FAKE_LOG")
lock = threading.Lock()
state = {"messages": [], "busy": False, "turn": 0, "permissions": [], "released": threading.Event()}


def record(line):
    if log_path:
        with lock, open(log_path, "a") as handle:
            handle.write(line + "\n")


def add_reply(turn, error=None):
    info = {"id": "msg_a%d" % turn, "role": "assistant", "parentID": "msg_u%d" % turn,
            "sessionID": SES, "finish": None if error else "stop", "time": {"completed": 1}}
    if error:
        info["error"] = error
    parts = [] if error else [{"id": "prt_a%d" % turn, "type": "text", "text": "done",
                               "sessionID": SES, "messageID": info["id"], "time": {"end": 2}}]
    state["messages"].append({"info": info, "parts": parts})


def answer(turn):
    time.sleep(0.15)
    if mode == "error":
        add_reply(turn, {"name": "APIError", "data": {"message": "boom"}})
    elif mode == "hold":
        state["released"].wait(60)
        add_reply(turn, {"name": "MessageAbortedError", "data": {"message": "aborted"}})
    elif mode == "permission":
        state["permissions"].append({"id": "per_1", "sessionID": SES, "permission": "bash",
                                     "patterns": [], "metadata": {}, "always": []})
        state["released"].wait(60)
        add_reply(turn)
    else:
        add_reply(turn)
    state["busy"] = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, code, body=None):
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        record("GET " + path)
        if path == "/global/health":
            return self.send_json(200, {"healthy": True})
        if path == "/session/status":
            return self.send_json(200, {SES: {"type": "busy"}} if state["busy"] else {})
        if path == "/permission":
            return self.send_json(200, state["permissions"])
        if path == "/question":
            return self.send_json(200, [])
        if path == "/session/" + SES:
            if mode == "missing":
                return self.send_json(404, {"name": "NotFoundError"})
            return self.send_json(200, {"id": SES})
        if path == "/session/%s/message" % SES:
            return self.send_json(200, state["messages"])
        if path == "/event":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"type":"server.connected","properties":{}}\n\n')
            self.wfile.flush()
            while True:
                time.sleep(1)
        self.send_json(404, {"name": "NotFoundError"})

    def do_POST(self):
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        record("POST " + path)
        if path == "/session":
            return self.send_json(200, {"id": SES})
        if path == "/session/%s/prompt_async" % SES:
            state["turn"] += 1
            turn = state["turn"]
            state["messages"].append({"info": {"id": "msg_u%d" % turn, "role": "user", "sessionID": SES},
                                      "parts": [{"id": "prt_u%d" % turn, "type": "text", "text": "prompt"}]})
            state["busy"] = True
            threading.Thread(target=answer, args=(turn,), daemon=True).start()
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/session/%s/abort" % SES:
            state["released"].set()
            return self.send_json(200, True)
        if path == "/permission/per_1/reply":
            state["permissions"] = []
            state["released"].set()
            return self.send_json(200, True)
        self.send_json(404, {"name": "NotFoundError"})


server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
server.daemon_threads = True
server.serve_forever()
"##;
}

// ---------------------------------------------------------------------------
// Claim, binding and file helpers.
// ---------------------------------------------------------------------------

fn validate_provider_thread_id(value: &str) -> Result<()> {
    if !value.starts_with("ses_") || value.len() <= 4 {
        anyhow::bail!("OpenCode provider session id must be an opaque ses_... identity");
    }
    Ok(())
}

async fn cleanup_process_group(process_group_id: Option<i32>) {
    crate::console_adapter::cleanup_process_group("opencode-run", process_group_id).await;
}

fn opencode_console_root() -> Result<PathBuf> {
    Ok(crate::config::get_longhouse_home()?
        .join("managed-local")
        .join("opencode-console"))
}

fn acquire_turn_lock(root: &Path, key: &str) -> Result<File> {
    use std::os::fd::AsRawFd;
    let locks = root.join("turn-locks");
    std::fs::create_dir_all(&locks)?;
    set_private_dir(&locks)?;
    let safe_key = key.replace(
        |character: char| {
            !character.is_ascii_alphanumeric() && character != '-' && character != '_'
        },
        "_",
    );
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .open(locks.join(format!("{safe_key}.lock")))?;
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        anyhow::bail!("OpenCode session {key} already has an execution owner");
    }
    Ok(file)
}

fn atomic_write_json(path: &Path, value: &Value) -> Result<()> {
    use std::io::Write;
    let parent = path.parent().context("state path has no parent")?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("state"),
        Uuid::new_v4()
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(&serde_json::to_vec(value)?)?;
    file.sync_all()?;
    std::fs::rename(temporary, path)?;
    Ok(())
}

fn private_output_file(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?)
}

fn set_private_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    Ok(())
}

fn normalized_optional(value: &Option<String>) -> Option<String> {
    value
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn validate_uuid(value: &str, label: &str) -> Result<()> {
    Uuid::parse_str(value).with_context(|| format!("{label} must be a UUID"))?;
    Ok(())
}

fn promote_binding(sink: &OpenCodeRunSink, provider_thread_id: &str) -> Result<()> {
    let root = opencode_console_root()?;
    let pending = root
        .join("binding-probes")
        .join(format!("{}.json", sink.session_id));
    let existing: Value = serde_json::from_slice(&std::fs::read(&pending)?)?;
    if existing.get("status").and_then(Value::as_str) != Some("pending")
        || existing.get("launch_id").and_then(Value::as_str) != Some(&sink.launch_id)
        || existing.get("session_id").and_then(Value::as_str) != Some(&sink.session_id)
    {
        anyhow::bail!("OpenCode stream identity does not match its pending Console binding");
    }
    if let Some(expected) = existing.get("provider_session_id").and_then(Value::as_str) {
        if expected != provider_thread_id {
            anyhow::bail!("OpenCode stream identity does not match requested resume identity");
        }
    }
    atomic_write_json(
        &pending,
        &json!({
            "schema_version": 1,
            "provider": "opencode",
            "adapter": OPENCODE_RUN_ADAPTER,
            "status": "observed",
            "session_id": sink.session_id,
            "thread_id": sink.thread_id,
            "turn_id": sink.turn_id,
            "run_id": sink.run_id,
            "client_request_id": sink.client_request_id,
            "provider_session_id": provider_thread_id,
            "launch_id": sink.launch_id,
            "observed_at": Utc::now().to_rfc3339(),
        }),
    )?;
    let managed_root =
        crate::config::get_longhouse_home()?.join("managed-local/opencode/bridge/sessions");
    std::fs::create_dir_all(&managed_root)?;
    set_private_dir(&managed_root)?;
    atomic_write_json(
        &managed_root.join(format!("{}.json", sink.session_id)),
        &json!({
            "schema_version": 1,
            "provider": "opencode",
            "longhouse_session_id": sink.session_id,
            "thread_id": sink.thread_id,
            "provider_session_id": provider_thread_id,
            "adapter": OPENCODE_RUN_ADAPTER,
            "launch_id": sink.launch_id,
            "updated_at": Utc::now().to_rfc3339(),
        }),
    )
}

#[allow(clippy::too_many_arguments)]
fn reserve_binding(
    root: &Path,
    session_id: &str,
    thread_id: &str,
    turn_id: Option<&str>,
    run_id: &str,
    client_request_id: Option<&str>,
    provider_thread_id: Option<&str>,
    launch_id: &str,
) -> Result<()> {
    let bindings = root.join("binding-probes");
    std::fs::create_dir_all(&bindings)?;
    set_private_dir(&bindings)?;
    atomic_write_json(
        &bindings.join(format!("{session_id}.json")),
        &json!({
            "schema_version": 1,
            "provider": "opencode",
            "adapter": OPENCODE_RUN_ADAPTER,
            "status": "pending",
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "run_id": run_id,
            "client_request_id": client_request_id,
            "provider_session_id": provider_thread_id,
            "launch_id": launch_id,
            "expires_at": (Utc::now() + chrono::Duration::minutes(10)).to_rfc3339(),
        }),
    )
}

fn rollback_binding(root: &Path, session_id: &str, launch_id: &str) {
    let target = root
        .join("binding-probes")
        .join(format!("{session_id}.json"));
    let matches = std::fs::read(&target)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|value| {
            value.get("status").and_then(Value::as_str) == Some("pending")
                && value.get("launch_id").and_then(Value::as_str) == Some(launch_id)
        });
    if matches {
        let _ = std::fs::remove_file(target);
    }
}

impl OpenCodeRunSink {
    async fn post_binding(&self, provider_thread_id: &str) {
        self.post_events(vec![json!({
            "runtime_key": format!("opencode:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "opencode",
            "device_id": self.machine_name,
            "source": OPENCODE_RUN_ADAPTER,
            "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("opencode-run:{}:{}:binding", self.session_id, self.launch_id),
            "payload": {
                "provider_session_id": provider_thread_id,
                "managed_transport": OPENCODE_RUN_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("opencode:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "opencode",
            "device_id": self.machine_name,
            "source": OPENCODE_RUN_ADAPTER,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("opencode-run:{}:{}:phase:{phase}", self.session_id, self.run_id),
            "payload": {"managed_transport": OPENCODE_RUN_ADAPTER, "execution_lifetime": "one_shot"}
        })])
        .await;
    }

    async fn post_stream_event(&self, seq: u64, event: Value, provider_thread_id: Option<&str>) {
        if event.get("type").and_then(Value::as_str) == Some("tool_use") {
            let part = event.get("part").and_then(Value::as_object);
            let status = part
                .and_then(|part| part.get("state"))
                .and_then(Value::as_object)
                .and_then(|state| state.get("status"))
                .and_then(Value::as_str);
            let phase = if matches!(status, Some("completed" | "error")) {
                "thinking"
            } else {
                "running"
            };
            self.post_phase(
                phase,
                part.and_then(|part| part.get("tool"))
                    .and_then(Value::as_str)
                    .map(str::to_string),
            )
            .await;
        }
        self.post_events(vec![json!({
            "runtime_key": format!("opencode:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "opencode",
            "device_id": self.machine_name,
            "source": OPENCODE_RUN_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("opencode-run:{}:{}:stdout:{seq}", self.session_id, self.run_id),
            "payload": {
                "progress_kind": "opencode_run_stream",
                "seq": seq,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": provider_thread_id,
                "event": event,
                "managed_transport": OPENCODE_RUN_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        error: Option<String>,
        provider_thread_id: Option<&str>,
    ) {
        crate::turn_claims::mark_terminal(
            &self.run_id,
            terminal_state,
            (terminal_state == "run_failed")
                .then(|| error.clone())
                .flatten(),
        );
        self.persist_local_phase("finished", None, Utc::now());
        self.post_events(vec![json!({
            "runtime_key": format!("opencode:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "opencode",
            "device_id": self.machine_name,
            "source": OPENCODE_RUN_ADAPTER,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("opencode-run:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": OPENCODE_RUN_ADAPTER,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": OPENCODE_RUN_ADAPTER,
                "exit_code": exit_code,
                "stderr_tail": error,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": provider_thread_id
            }
        })])
        .await;
    }

    fn persist_local_phase(
        &self,
        phase: &str,
        tool_name: Option<String>,
        observed_at: DateTime<Utc>,
    ) {
        let Some(db_path) = self.local_db_path.as_deref() else {
            return;
        };
        if let Err(err) = crate::hook_outbox::enqueue_local_phase(
            db_path,
            &self.session_id,
            "opencode",
            phase,
            tool_name.as_deref(),
            OPENCODE_RUN_ADAPTER,
            &observed_at.to_rfc3339(),
            Some(self.run_id.as_str()),
        ) {
            eprintln!(
                "[opencode-run] enqueue local phase failed for {}: {err}",
                self.session_id
            );
        }
    }

    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            if let Err(error) =
                crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event)
            {
                eprintln!("[opencode-run] runtime outbox write failed: {error}");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::process::Command;

    fn view(id: &str, role: &str, parent: Option<&str>, finish: Option<&str>) -> MessageView {
        MessageView {
            id: id.to_string(),
            role: role.to_string(),
            parent_id: parent.map(str::to_string),
            finish: finish.map(str::to_string),
            error: None,
        }
    }

    fn ids(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| value.to_string()).collect()
    }

    #[test]
    fn a_turn_is_over_when_the_session_is_idle_and_its_last_answer_stopped() {
        let messages = vec![
            view("msg_u1", "user", None, None),
            view("msg_a1", "assistant", Some("msg_u1"), Some("stop")),
        ];
        assert_eq!(
            turn_outcome(&messages, &ids(&["msg_u1"]), false),
            TurnOutcome::Completed
        );
        // A busy session is not over, whatever the last reply says.
        assert_eq!(
            turn_outcome(&messages, &ids(&["msg_u1"]), true),
            TurnOutcome::Running
        );
        // No answer yet is not over either; the caller times that out.
        assert_eq!(
            turn_outcome(&messages[..1], &ids(&["msg_u1"]), false),
            TurnOutcome::Running
        );
    }

    #[test]
    fn a_reply_to_someone_elses_message_does_not_settle_this_turn() {
        // The older exchange in a resumed session finished; this turn has not.
        let messages = vec![
            view("msg_old", "user", None, None),
            view("msg_olda", "assistant", Some("msg_old"), Some("stop")),
            view("msg_u2", "user", None, None),
        ];
        assert_eq!(
            turn_outcome(&messages, &ids(&["msg_u2"]), false),
            TurnOutcome::Running
        );
    }

    #[test]
    fn a_steered_turn_ends_with_the_reply_parented_at_the_steer() {
        // Verified live: the reply after a steer is parented at the steer
        // message, and the first reply finished with tool-calls.
        let messages = vec![
            view("msg_u1", "user", None, None),
            view("msg_a1", "assistant", Some("msg_u1"), Some("tool-calls")),
            view("msg_s1", "user", None, None),
            view("msg_a2", "assistant", Some("msg_s1"), Some("stop")),
        ];
        assert_eq!(
            turn_outcome(&messages, &ids(&["msg_u1", "msg_s1"]), false),
            TurnOutcome::Completed
        );
        // Until the steer's reply exists the last answer is a tool-calls step
        // and an idle session is stalled mid-work (a failure only if it lasts).
        assert!(matches!(
            turn_outcome(&messages[..2], &ids(&["msg_u1", "msg_s1"]), false),
            TurnOutcome::Stalled(_)
        ));
    }

    #[test]
    fn errors_end_the_turn_at_once_and_an_abort_is_a_cancellation() {
        let mut aborted = view("msg_a1", "assistant", Some("msg_u1"), None);
        aborted.error = Some(("MessageAbortedError".to_string(), String::new()));
        let mut failed = view("msg_a1", "assistant", Some("msg_u1"), None);
        failed.error = Some(("APIError".to_string(), "rate limited".to_string()));
        let user = view("msg_u1", "user", None, None);
        assert_eq!(
            turn_outcome(&[user.clone(), aborted], &ids(&["msg_u1"]), true),
            TurnOutcome::Cancelled
        );
        assert_eq!(
            turn_outcome(&[user, failed], &ids(&["msg_u1"]), true),
            TurnOutcome::Failed("OpenCode provider error: APIError: rate limited".to_string())
        );
    }

    #[test]
    fn other_finishes_are_failures_not_completions() {
        let messages = vec![
            view("msg_u1", "user", None, None),
            view("msg_a1", "assistant", Some("msg_u1"), Some("length")),
        ];
        assert_eq!(
            turn_outcome(&messages, &ids(&["msg_u1"]), false),
            TurnOutcome::Failed("OpenCode ended the turn with finish=length".to_string())
        );
    }

    #[test]
    fn messages_parse_from_the_server_list_shape() {
        // The shape `GET /session/{id}/message` returned on 1.18.32.
        let list = json!([
            {"info": {"id": "msg_1", "role": "user", "sessionID": "ses_1"}, "parts": []},
            {"info": {"id": "msg_2", "role": "assistant", "parentID": "msg_1", "finish": "stop",
                      "time": {"completed": 1}}, "parts": []},
            {"info": {"id": "msg_3", "role": "assistant", "parentID": "msg_1",
                      "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}}},
             "parts": []}
        ]);
        let messages = parse_messages(&list);
        assert_eq!(messages.len(), 3);
        assert_eq!(messages[1].finish.as_deref(), Some("stop"));
        assert_eq!(messages[1].parent_id.as_deref(), Some("msg_1"));
        assert_eq!(
            messages[2].error,
            Some(("MessageAbortedError".to_string(), "aborted".to_string()))
        );
        assert!(parse_messages(&json!({"not": "a list"})).is_empty());
    }

    #[test]
    fn our_user_message_is_the_first_one_that_was_not_there_before() {
        let known: HashSet<String> = ["msg_old".to_string()].into();
        let messages = vec![
            view("msg_old", "user", None, None),
            view("msg_olda", "assistant", Some("msg_old"), Some("stop")),
        ];
        assert_eq!(new_user_message(&messages, &known), None);
        let mut with_ours = messages;
        with_ours.push(view("msg_new", "user", None, None));
        assert_eq!(
            new_user_message(&with_ours, &known),
            Some("msg_new".to_string())
        );
    }

    #[test]
    fn the_model_travels_as_provider_and_model_id() {
        assert_eq!(
            model_ref("openrouter/deepseek/deepseek-v4.1-flash"),
            Some(json!({"providerID": "openrouter", "modelID": "deepseek/deepseek-v4.1-flash"}))
        );
        assert_eq!(model_ref("no-slash"), None);
        assert_eq!(model_ref("/x"), None);
    }

    #[test]
    fn the_prompt_carries_text_and_the_selected_model_and_no_message_id() {
        let config = OpenCodeRunConfig {
            session_id: "s".into(),
            thread_id: "t".into(),
            turn_id: None,
            run_id: "r".into(),
            client_request_id: None,
            cwd: PathBuf::from("/tmp"),
            opencode_bin: "opencode".into(),
            prompt: "  hello  ".into(),
            image_paths: Vec::new(),
            resume_provider_thread_id: None,
            model: Some("openrouter/deepseek/deepseek-v4.1-flash".into()),
            permission_mode: "bypass".into(),
            machine_name: "m".into(),
            local_db_path: None,
        };
        let body = prompt_body(&config).unwrap();
        assert_eq!(body["parts"][0]["type"], "text");
        assert_eq!(body["parts"][0]["text"], "hello");
        assert_eq!(body["model"]["providerID"], "openrouter");
        // A re-posted id appends to the stored message, so none is ever sent.
        assert!(body.get("messageID").is_none());
    }

    #[test]
    fn native_session_ids_are_opaque_not_uuids() {
        assert!(validate_provider_thread_id("ses_f158d8f99ffeektbJlv1YFCmGj").is_ok());
        assert!(validate_provider_thread_id("not-a-session").is_err());
        assert!(validate_provider_thread_id("ses_").is_err());
    }

    fn frame(kind: &str, properties: Value) -> Value {
        json!({"id": "evt_1", "type": kind, "properties": properties})
    }

    #[test]
    fn parts_of_this_session_become_the_stream_events_the_server_reads() {
        let mut projector = Projector::new("ses_1");
        let now = Instant::now();
        projector.interpret(
            &frame(
                "message.updated",
                json!({"info": {"id": "msg_u", "role": "user", "sessionID": "ses_1"}}),
            ),
            now,
        );
        projector.interpret(
            &frame(
                "message.updated",
                json!({"info": {"id": "msg_a", "role": "assistant", "sessionID": "ses_1"}}),
            ),
            now,
        );
        let text = projector.interpret(
            &frame(
                "message.part.updated",
                json!({"part": {"id": "prt_1", "messageID": "msg_a", "sessionID": "ses_1",
                                "type": "text", "text": "LH_MARKER", "time": {"end": 2}}}),
            ),
            now,
        );
        let Some(Action::Stream(event)) = text.first() else {
            panic!("a text part is a stream event");
        };
        assert_eq!(event["type"], "text");
        assert_eq!(event["part"]["text"], "LH_MARKER");
        // The user's own message parts are not the assistant's stream.
        assert!(projector
            .interpret(
                &frame(
                    "message.part.updated",
                    json!({"part": {"id": "prt_u", "messageID": "msg_u", "sessionID": "ses_1",
                                    "type": "text", "text": "the prompt"}}),
                ),
                now,
            )
            .is_empty());
        // Another session on the same server (a subagent) is not this stream.
        assert!(projector
            .interpret(
                &frame(
                    "message.part.updated",
                    json!({"part": {"id": "prt_c", "messageID": "msg_c", "sessionID": "ses_child",
                                    "type": "text", "text": "child"}}),
                ),
                now,
            )
            .is_empty());
    }

    #[test]
    fn a_tool_is_one_event_per_status_and_a_step_is_one_event() {
        let mut projector = Projector::new("ses_1");
        let now = Instant::now();
        let tool = |status: &str| {
            frame(
                "message.part.updated",
                json!({"part": {"id": "prt_t", "messageID": "msg_a", "sessionID": "ses_1",
                                "type": "tool", "tool": "bash", "callID": "c1",
                                "state": {"status": status}}}),
            )
        };
        assert_eq!(projector.interpret(&tool("running"), now).len(), 1);
        assert_eq!(projector.interpret(&tool("running"), now).len(), 0);
        assert_eq!(projector.interpret(&tool("completed"), now).len(), 1);
        let step = frame(
            "message.part.updated",
            json!({"part": {"id": "prt_s", "messageID": "msg_a", "sessionID": "ses_1",
                            "type": "step-start"}}),
        );
        assert_eq!(projector.interpret(&step, now).len(), 1);
        assert_eq!(projector.interpret(&step, now).len(), 0);
    }

    #[test]
    fn streamed_text_is_throttled_and_accumulated() {
        let mut projector = Projector::new("ses_1");
        let start = Instant::now();
        let delta = |text: &str| {
            frame(
                "message.part.delta",
                json!({"sessionID": "ses_1", "messageID": "msg_a", "partID": "prt_1",
                       "field": "text", "delta": text}),
            )
        };
        let first = projector.interpret(&delta("Hel"), start);
        assert_eq!(first.len(), 1);
        // Within the interval nothing is re-emitted, but nothing is lost.
        assert!(projector.interpret(&delta("lo"), start).is_empty());
        let later = projector.interpret(&delta("!"), start + TEXT_PREVIEW_EVERY);
        let Some(Action::Stream(event)) = later.first() else {
            panic!("a due preview is emitted");
        };
        assert_eq!(event["part"]["text"], "Hello!");
    }

    #[test]
    fn permissions_are_approved_and_questions_rejected_for_any_session() {
        let mut projector = Projector::new("ses_1");
        let now = Instant::now();
        // A subagent's permission belongs to the turn too.
        let asked = projector.interpret(
            &frame(
                "permission.asked",
                json!({"id": "per_1", "sessionID": "ses_child", "permission": "bash"}),
            ),
            now,
        );
        assert!(matches!(asked.first(), Some(Action::ApprovePermission(id)) if id == "per_1"));
        let question = projector.interpret(
            &frame(
                "question.asked",
                json!({"id": "que_1", "sessionID": "ses_1"}),
            ),
            now,
        );
        assert!(matches!(question.first(), Some(Action::RejectQuestion(id)) if id == "que_1"));
    }

    #[test]
    fn only_events_that_can_end_the_turn_ask_for_a_reconcile() {
        let mut projector = Projector::new("ses_1");
        let now = Instant::now();
        let idle = projector.interpret(
            &frame(
                "session.status",
                json!({"sessionID": "ses_1", "status": {"type": "idle"}}),
            ),
            now,
        );
        assert!(matches!(idle.first(), Some(Action::Reconcile)));
        let busy = projector.interpret(
            &frame(
                "session.status",
                json!({"sessionID": "ses_1", "status": {"type": "busy"}}),
            ),
            now,
        );
        assert!(busy.is_empty());
        let other = projector.interpret(
            &frame("session.idle", json!({"sessionID": "ses_child"})),
            now,
        );
        assert!(other.is_empty());
    }

    #[test]
    fn backfill_delivers_what_the_stream_missed_and_nothing_twice() {
        let list = json!([
            {"info": {"id": "msg_u", "role": "user"}, "parts": []},
            {"info": {"id": "msg_a", "role": "assistant", "parentID": "msg_u", "finish": "stop"},
             "parts": [
                {"id": "prt_s", "type": "step-start", "sessionID": "ses_1"},
                {"id": "prt_1", "type": "text", "text": "LH_MARKER", "sessionID": "ses_1"},
                {"id": "prt_f", "type": "step-finish", "sessionID": "ses_1"}
             ]}
        ]);
        let mut projector = Projector::new("ses_1");
        let submitted = ids(&["msg_u"]);
        let kinds: Vec<String> = backfill(&list, &submitted, &mut projector)
            .iter()
            .map(|event| event["type"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(kinds, ["step_start", "text", "step_finish"]);
        // Delivered once; a second pass adds nothing.
        assert!(backfill(&list, &submitted, &mut projector).is_empty());
        // A reply to something this turn did not submit is not backfilled.
        let mut other = Projector::new("ses_1");
        assert!(backfill(&list, &ids(&["msg_x"]), &mut other).is_empty());
    }

    #[test]
    fn a_text_the_stream_already_finished_is_not_backfilled() {
        let mut projector = Projector::new("ses_1");
        let now = Instant::now();
        projector.interpret(
            &frame(
                "message.part.updated",
                json!({"part": {"id": "prt_1", "messageID": "msg_a", "sessionID": "ses_1",
                                "type": "text", "text": "LH_MARKER", "time": {"end": 2}}}),
            ),
            now,
        );
        let list = json!([
            {"info": {"id": "msg_a", "role": "assistant", "parentID": "msg_u"},
             "parts": [{"id": "prt_1", "type": "text", "text": "LH_MARKER", "sessionID": "ses_1"}]}
        ]);
        assert!(backfill(&list, &ids(&["msg_u"]), &mut projector).is_empty());
    }

    #[test]
    fn the_stream_log_counts_lines_and_a_reopened_log_continues_them() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("stdout.jsonl");
        let mut log = StreamLog::open(&path).unwrap();
        assert_eq!(log.append(&json!({"type": "text"})).unwrap(), 1);
        assert_eq!(log.append(&json!({"type": "step_finish"})).unwrap(), 2);
        drop(log);
        let mut reopened = StreamLog::reopen(&path).unwrap();
        assert_eq!(reopened.append(&json!({"type": "text"})).unwrap(), 3);
        assert_eq!(std::fs::read_to_string(&path).unwrap().lines().count(), 3);
    }

    #[test]
    fn the_server_state_round_trips_privately() {
        use std::os::unix::fs::PermissionsExt;
        let temp = tempfile::tempdir().unwrap();
        let state = ServeState {
            server_url: "http://127.0.0.1:4096".into(),
            username: "opencode".into(),
            password: "secret".into(),
            directory: "/work".into(),
            provider_session_id: Some("ses_1".into()),
            message_ids: ids(&["msg_1"]),
        };
        state.save(temp.path()).unwrap();
        let mode = std::fs::metadata(ServeState::path(temp.path()))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o077, 0, "the password file must be private");
        let loaded = ServeState::load(temp.path()).unwrap();
        assert_eq!(loaded.message_ids, ids(&["msg_1"]));
        assert_eq!(loaded.server().password, "secret");
    }

    /// The fake server, the process environment that points the adapter at it,
    /// and the lock that serializes tests sharing that environment.
    struct FakeOpenCode {
        _lock: std::sync::MutexGuard<'static, ()>,
        saved: Vec<(&'static str, Option<std::ffi::OsString>)>,
        temp: tempfile::TempDir,
        bin: PathBuf,
        log: PathBuf,
        workspace: PathBuf,
        session_id: String,
        thread_id: String,
    }

    impl FakeOpenCode {
        fn new(mode: &str) -> Self {
            use std::os::unix::fs::PermissionsExt;
            let lock = crate::console_adapter::agent_state_guard();
            let temp = tempfile::tempdir().unwrap();
            let bin = temp.path().join("opencode");
            std::fs::write(&bin, fake_server::SCRIPT).unwrap();
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
            let log = temp.path().join("requests.log");
            let workspace = temp.path().join("workspace");
            std::fs::create_dir(&workspace).unwrap();
            let mut saved = Vec::new();
            for (key, value) in [
                (
                    "LONGHOUSE_HOME",
                    temp.path().join("longhouse").into_os_string(),
                ),
                ("OPENCODE_FAKE_MODE", mode.into()),
                ("OPENCODE_FAKE_LOG", log.clone().into_os_string()),
            ] {
                saved.push((key, std::env::var_os(key)));
                unsafe { std::env::set_var(key, value) };
            }
            Self {
                _lock: lock,
                saved,
                temp,
                bin,
                log,
                workspace,
                session_id: Uuid::new_v4().to_string(),
                thread_id: Uuid::new_v4().to_string(),
            }
        }

        /// Claim a run and start its turn.
        async fn start(&self, resume: Option<&str>) -> (String, String, OpenCodeRunSummary) {
            let run_id = Uuid::new_v4().to_string();
            let turn_id = Uuid::new_v4().to_string();
            let request_id = format!("fake-{run_id}");
            assert!(matches!(
                crate::turn_claims::default_registry()
                    .unwrap()
                    .claim(
                        &run_id,
                        &self.session_id,
                        &self.thread_id,
                        Some(&turn_id),
                        Some(&request_id),
                        "opencode",
                    )
                    .unwrap(),
                crate::turn_claims::ClaimOutcome::Acquired
            ));
            let summary = start_opencode_run_turn(OpenCodeRunConfig {
                session_id: self.session_id.clone(),
                thread_id: self.thread_id.clone(),
                turn_id: Some(turn_id.clone()),
                run_id: run_id.clone(),
                client_request_id: Some(request_id),
                cwd: self.workspace.clone(),
                opencode_bin: self.bin.display().to_string(),
                prompt: "reply once".to_string(),
                image_paths: Vec::new(),
                resume_provider_thread_id: resume.map(str::to_string),
                model: None,
                permission_mode: "bypass".to_string(),
                machine_name: "fake-opencode".to_string(),
                local_db_path: None,
            })
            .await
            .unwrap();
            (run_id, turn_id, summary)
        }

        async fn terminal(&self, run_id: &str) -> crate::turn_claims::TurnClaim {
            let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
            loop {
                let claim = crate::turn_claims::default_registry()
                    .unwrap()
                    .read(run_id)
                    .unwrap();
                if claim.state == "terminal" {
                    return claim;
                }
                assert!(tokio::time::Instant::now() < deadline, "turn never settled");
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        }

        fn requests(&self) -> Vec<String> {
            std::fs::read_to_string(&self.log)
                .unwrap_or_default()
                .lines()
                .map(str::to_string)
                .collect()
        }
    }

    impl Drop for FakeOpenCode {
        fn drop(&mut self) {
            for (key, value) in self.saved.drain(..) {
                match value {
                    Some(value) => unsafe { std::env::set_var(key, value) },
                    None => unsafe { std::env::remove_var(key) },
                }
            }
            let _ = &self.temp;
        }
    }

    fn terminal_state(claim: &crate::turn_claims::TurnClaim) -> String {
        claim
            .result
            .as_ref()
            .and_then(|result| result["terminal_state"].as_str())
            .unwrap_or_default()
            .to_string()
    }

    #[tokio::test]
    async fn a_turn_completes_on_an_owned_server_and_retains_the_reply() {
        let fake = FakeOpenCode::new("ok");
        let (run_id, _turn, summary) = fake.start(None).await;
        let claim = fake.terminal(&run_id).await;
        assert_eq!(terminal_state(&claim), "run_completed");
        assert_eq!(
            claim.provider_thread_id.as_deref(),
            Some(fake_server::SESSION)
        );
        // A server was started, not a `run`, and the model never rides on argv.
        assert!(summary.argv.iter().any(|arg| arg == "serve"));
        assert!(!summary
            .argv
            .iter()
            .any(|arg| matches!(arg.as_str(), "run" | "--auto")));
        // The reply is in the retained stream evidence.
        let stream = std::fs::read_to_string(&summary.stdout_path).unwrap();
        assert!(
            stream.contains("\"type\":\"text\"") && stream.contains("done"),
            "{stream}"
        );
        // The server died with the turn, and its credentials are private.
        assert_ne!(unsafe { libc::killpg(summary.process_group_id, 0) }, 0);
        let state_path = ServeState::path(Path::new(&summary.stdout_path).parent().unwrap());
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(
            std::fs::metadata(state_path).unwrap().permissions().mode() & 0o077,
            0
        );
    }

    #[tokio::test]
    async fn resume_verifies_the_native_session_and_never_creates_one() {
        let fake = FakeOpenCode::new("ok");
        let (first, _turn, _summary) = fake.start(None).await;
        let claim = fake.terminal(&first).await;
        let native = claim.provider_thread_id.unwrap();
        let (second, _turn, _summary) = fake.start(Some(&native)).await;
        assert_eq!(
            terminal_state(&fake.terminal(&second).await),
            "run_completed"
        );
        let requests = fake.requests();
        // One session was created, by the first turn; the second only looked.
        assert_eq!(
            requests
                .iter()
                .filter(|line| *line == "POST /session")
                .count(),
            1
        );
        assert!(requests
            .iter()
            .any(|line| *line == format!("GET /session/{native}")));
    }

    #[tokio::test]
    async fn resuming_a_session_the_server_does_not_have_fails_the_turn() {
        let fake = FakeOpenCode::new("missing");
        let (run_id, _turn, _summary) = fake.start(Some(fake_server::SESSION)).await;
        assert_eq!(terminal_state(&fake.terminal(&run_id).await), "run_failed");
        // Never a silent new session.
        assert!(!fake.requests().iter().any(|line| line == "POST /session"));
    }

    #[tokio::test]
    async fn a_provider_error_fails_the_turn() {
        let fake = FakeOpenCode::new("error");
        let (run_id, _turn, _summary) = fake.start(None).await;
        assert_eq!(terminal_state(&fake.terminal(&run_id).await), "run_failed");
    }

    #[tokio::test]
    async fn a_pending_permission_is_approved_even_when_the_stream_never_says_so() {
        // The fake's event stream carries no permission event; the turn only
        // completes because the adapter asks the server what it is waiting on.
        let fake = FakeOpenCode::new("permission");
        let (run_id, _turn, _summary) = fake.start(None).await;
        assert_eq!(
            terminal_state(&fake.terminal(&run_id).await),
            "run_completed"
        );
        assert!(fake
            .requests()
            .iter()
            .any(|line| line == "POST /permission/per_1/reply"));
    }

    #[tokio::test]
    async fn interrupt_aborts_the_session_and_the_run_settles_cancelled() {
        let fake = FakeOpenCode::new("hold");
        let (run_id, turn_id, summary) = fake.start(None).await;
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        while !fake
            .requests()
            .iter()
            .any(|line| line.ends_with("/prompt_async"))
        {
            assert!(
                tokio::time::Instant::now() < deadline,
                "prompt never reached the server"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        interrupt_opencode_run_turn(&run_id, &fake.session_id, &fake.thread_id, &turn_id)
            .await
            .unwrap();
        assert_eq!(
            terminal_state(&fake.terminal(&run_id).await),
            "run_cancelled"
        );
        assert!(fake
            .requests()
            .iter()
            .any(|line| line == &format!("POST /session/{}/abort", fake_server::SESSION)));
        assert_ne!(unsafe { libc::killpg(summary.process_group_id, 0) }, 0);
    }

    #[test]
    fn recovery_learns_what_the_previous_engine_logged_and_backfills_nothing_twice() {
        let temp = tempfile::tempdir().unwrap();
        let log = temp.path().join("stdout.jsonl");
        let mut stream = StreamLog::open(&log).unwrap();
        stream
            .append(&json!({"type": "step_start", "part": {"id": "prt_s", "type": "step-start"}}))
            .unwrap();
        stream
            .append(
                &json!({"type": "text", "part": {"id": "prt_1", "type": "text",
                            "text": "LH_MARKER", "time": {"end": 2}}}),
            )
            .unwrap();
        let list = json!([
            {"info": {"id": "msg_u", "role": "user"}, "parts": []},
            {"info": {"id": "msg_a", "role": "assistant", "parentID": "msg_u", "finish": "stop"},
             "parts": [
                {"id": "prt_s", "type": "step-start", "sessionID": "ses_1"},
                {"id": "prt_1", "type": "text", "text": "LH_MARKER", "sessionID": "ses_1"},
                {"id": "prt_f", "type": "step-finish", "sessionID": "ses_1"}
             ]}
        ]);
        let submitted = ids(&["msg_u"]);
        // A fresh projector would replay everything; a seeded one only the gap.
        let mut fresh = Projector::new("ses_1");
        assert_eq!(backfill(&list, &submitted, &mut fresh).len(), 3);
        let mut seeded = Projector::new("ses_1");
        seeded.seed_from_log(&log);
        let kinds: Vec<String> = backfill(&list, &submitted, &mut seeded)
            .iter()
            .map(|event| event["type"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(kinds, ["step_finish"]);
    }

    #[tokio::test]
    #[ignore = "requires an authenticated stock opencode and spends provider tokens"]
    async fn installed_opencode_completes_and_resumes_through_production_console_adapter() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let opencode_bin =
            std::env::var("LONGHOUSE_OPENCODE_BIN").unwrap_or_else(|_| "opencode".to_string());
        let help = Command::new(&opencode_bin)
            .args(["serve", "--help"])
            .output()
            .await
            .unwrap();
        assert!(help.status.success());
        let marker = format!("LH_OPENCODE_CONSOLE_{}", Uuid::new_v4().simple());
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();

        async fn run_turn(
            opencode_bin: &str,
            cwd: &Path,
            session_id: &str,
            thread_id: &str,
            prompt: String,
            resume: Option<String>,
        ) -> (OpenCodeRunSummary, crate::turn_claims::TurnClaim) {
            let turn_id = Uuid::new_v4().to_string();
            let run_id = Uuid::new_v4().to_string();
            let request_id = format!("canary-{}", Uuid::new_v4());
            assert!(matches!(
                crate::turn_claims::default_registry()
                    .unwrap()
                    .claim(
                        &run_id,
                        session_id,
                        thread_id,
                        Some(&turn_id),
                        Some(&request_id),
                        "opencode",
                    )
                    .unwrap(),
                crate::turn_claims::ClaimOutcome::Acquired
            ));
            let summary = start_opencode_run_turn(OpenCodeRunConfig {
                session_id: session_id.to_string(),
                thread_id: thread_id.to_string(),
                turn_id: Some(turn_id),
                run_id,
                client_request_id: Some(request_id),
                cwd: cwd.to_path_buf(),
                opencode_bin: opencode_bin.to_string(),
                prompt,
                image_paths: Vec::new(),
                resume_provider_thread_id: resume,
                model: None,
                permission_mode: "bypass".to_string(),
                machine_name: "opencode-console-canary".to_string(),
                local_db_path: None,
            })
            .await
            .unwrap();
            let deadline = tokio::time::Instant::now() + Duration::from_secs(180);
            loop {
                let claim = crate::turn_claims::default_registry()
                    .unwrap()
                    .read(&summary.run_id)
                    .unwrap();
                if claim.state == "terminal" {
                    assert_eq!(
                        claim.result.as_ref().unwrap()["terminal_state"],
                        "run_completed",
                        "stream={}\nstderr={}",
                        std::fs::read_to_string(&summary.stdout_path).unwrap_or_default(),
                        std::fs::read_to_string(&summary.stderr_path).unwrap_or_default(),
                    );
                    return (summary, claim);
                }
                assert!(
                    tokio::time::Instant::now() < deadline,
                    "OpenCode Console canary timed out"
                );
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }

        let (first, first_claim) = run_turn(
            &opencode_bin,
            temp.path(),
            &session_id,
            &thread_id,
            format!("Remember {marker}. Reply with exactly {marker} and nothing else."),
            None,
        )
        .await;
        let provider_thread_id = first_claim.provider_thread_id.unwrap();
        assert!(provider_thread_id.starts_with("ses_"));
        assert!(std::fs::read_to_string(&first.stdout_path)
            .unwrap()
            .contains(&marker));
        // The server died with the turn.
        assert_ne!(unsafe { libc::killpg(first.process_group_id, 0) }, 0);

        let (second, second_claim) = run_turn(
            &opencode_bin,
            temp.path(),
            &session_id,
            &thread_id,
            "Reply with exactly the marker I asked you to remember in the previous turn."
                .to_string(),
            Some(provider_thread_id.clone()),
        )
        .await;
        assert_eq!(
            second_claim.provider_thread_id.as_deref(),
            Some(provider_thread_id.as_str())
        );
        assert!(std::fs::read_to_string(&second.stdout_path)
            .unwrap()
            .contains(&marker));

        let interrupt_turn_id = Uuid::new_v4().to_string();
        let interrupt_run_id = Uuid::new_v4().to_string();
        assert!(matches!(
            crate::turn_claims::default_registry()
                .unwrap()
                .claim(
                    &interrupt_run_id,
                    &session_id,
                    &thread_id,
                    Some(&interrupt_turn_id),
                    Some("canary-interrupt"),
                    "opencode",
                )
                .unwrap(),
            crate::turn_claims::ClaimOutcome::Acquired
        ));
        let interrupted = start_opencode_run_turn(OpenCodeRunConfig {
            session_id: session_id.clone(),
            thread_id: thread_id.clone(),
            turn_id: Some(interrupt_turn_id.clone()),
            run_id: interrupt_run_id.clone(),
            client_request_id: Some("canary-interrupt".to_string()),
            cwd: temp.path().to_path_buf(),
            opencode_bin: opencode_bin.clone(),
            prompt: "Use the bash tool to run exactly: sleep 30. Do not finish before the command finishes."
                .to_string(),
            image_paths: Vec::new(),
            resume_provider_thread_id: Some(provider_thread_id.clone()),
            model: None,
            permission_mode: "bypass".to_string(),
            machine_name: "opencode-console-canary".to_string(),
            local_db_path: None,
        })
        .await
        .unwrap();
        let tool_deadline = tokio::time::Instant::now() + Duration::from_secs(90);
        loop {
            let stream = std::fs::read_to_string(&interrupted.stdout_path).unwrap_or_default();
            if stream.contains("\"type\":\"tool_use\"") {
                break;
            }
            assert!(
                tokio::time::Instant::now() < tool_deadline,
                "OpenCode did not begin the interrupt canary tool"
            );
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        interrupt_opencode_run_turn(
            &interrupt_run_id,
            &session_id,
            &thread_id,
            &interrupt_turn_id,
        )
        .await
        .unwrap();
        let cancel_deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        loop {
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&interrupt_run_id)
                .unwrap();
            if claim.state == "terminal" {
                assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
                break;
            }
            assert!(
                tokio::time::Instant::now() < cancel_deadline,
                "OpenCode interrupt did not settle"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        assert_ne!(unsafe { libc::killpg(interrupted.process_group_id, 0) }, 0);

        let post_cancel_marker = format!("LH_OPENCODE_AFTER_CANCEL_{}", Uuid::new_v4().simple());
        let (post_cancel, post_cancel_claim) = run_turn(
            &opencode_bin,
            temp.path(),
            &session_id,
            &thread_id,
            format!("Reply with exactly {post_cancel_marker} and nothing else."),
            Some(provider_thread_id.clone()),
        )
        .await;
        assert_eq!(
            post_cancel_claim.provider_thread_id.as_deref(),
            Some(provider_thread_id.as_str())
        );
        assert!(std::fs::read_to_string(&post_cancel.stdout_path)
            .unwrap()
            .contains(&post_cancel_marker));

        if let Some(value) = previous_home {
            unsafe {
                std::env::set_var("LONGHOUSE_HOME", value);
            }
        } else {
            unsafe {
                std::env::remove_var("LONGHOUSE_HOME");
            }
        }
    }
}
