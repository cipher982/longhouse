//! Antigravity Console turns over `agy acp` (Agent Client Protocol, JSON-RPC
//! over stdio).
//!
//! One `agy acp` process per Console turn, so Console stays turn-scoped
//! (`turn-scoped-console-execution.md`). The engine drives the protocol the
//! way Pi and OMP Console are driven over RPC: the provider reads its commands
//! from a FIFO in the run directory that it opens read-write itself (it never
//! sees EOF, so the turn survives a Machine Agent restart), and its stdout goes
//! to a file the live and recovered monitors tail.
//!
//! Handshake (`initialize`, `session/new` or `session/resume`, optional
//! `session/set_model`, first `session/prompt`) runs inside
//! [`start_antigravity_acp_turn`], so the native identity is known, bound and
//! recorded before the launch returns. During the turn a steer is a second
//! `session/prompt`, Stop is `session/cancel`, images are native `image`
//! prompt parts.
//!
//! Two upstream quirks shape turn-end detection (observed on agy 1.5.9):
//!
//! * a model or quota error arrives as an ordinary `agent_message_chunk`
//!   (`"\n⚠ agent executor error: ..."`) and a `stopReason: end_turn` result
//!   with no `usage`; it is a failed turn, never a completion;
//! * a second `session/prompt` sent while the first is running makes the first
//!   prompt's RPC return `end_turn` at once while the turn continues, so a
//!   turn ends when every prompt issued has a reply, not when the first does.
//!   Issued prompt ids are appended to a sidecar file in the run directory
//!   before they are written to the FIFO, which is how a steer sent from another
//!   call path and a monitor recovered after an engine restart agree.

use std::collections::HashMap;
use std::fs::{File, OpenOptions};
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::process::{Child, Command};

use crate::console_adapter::{claim_process_liveness, stderr_tail, ClaimLiveness};
use crate::console_sink::{ConsoleProvider, ConsoleRun};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use uuid::Uuid;

pub const ANTIGRAVITY_ACP_ADAPTER: &str = "antigravity_acp";
pub const DEFAULT_ANTIGRAVITY_BIN: &str = "agy";

/// Prompts issued to the provider, one JSON object per line (`{"id", "kind"}`).
const PROMPTS_SIDECAR: &str = "acp-prompts.jsonl";
/// Present once a monitor has decided the turn is over; a steer arriving after
/// it is refused instead of being written to a process about to be killed.
const CLOSING_MARKER: &str = "acp-closing";

const INITIALIZE_ID: i64 = 1;
const SESSION_ID_REQUEST: i64 = 2;
const SET_MODEL_ID: i64 = 3;
const FIRST_PROMPT_ID: i64 = 4;
const CLOSE_ID: i64 = 5;

/// How long the provider gets to answer one handshake request. Measured on agy
/// 1.5.9: `initialize` 0.05 s, `session/new` 0.04 s, `session/set_model`
/// instant (acp1a trace). 30 s is over two orders of magnitude of headroom and
/// matches `PROMPT_ACK_DEADLINE`'s order for a loaded machine.
const HANDSHAKE_DEADLINE: Duration = Duration::from_secs(30);

/// How long a `session/cancel` may take to resolve the running prompt before
/// the process group is killed. ACP cancel is cooperative: agy must abort the
/// in-flight model stream, and a tool call runs in the background after
/// `WaitMsBeforeAsync` (5 s in the traces), so a healthy cancel resolves within
/// one model round trip (about 1 to 4 s observed). 5 s covers the slowest of
/// those plus one tick; past it the turn is disposable (the process is
/// turn-scoped and the transcript is written per step), so it is killed rather
/// than left running after the user pressed Stop.
const CANCEL_GRACE: Duration = if cfg!(test) {
    Duration::from_secs(2)
} else {
    Duration::from_secs(5)
};

/// How long the best-effort `session/close` gets before the group is killed.
const CLOSE_DEADLINE: Duration = Duration::from_secs(2);

/// How long a completed turn waits for agy's transcript file to appear. The
/// transcript is flushed per step, so it is normally there before the final
/// reply; this only covers filesystem lag.
const TRANSCRIPT_WAIT: Duration = Duration::from_secs(3);

/// Marker agy puts at the start of an error it reports as agent text.
const EXECUTOR_ERROR_MARKER: &str = "\u{26a0} agent executor error";

#[derive(Clone, Debug)]
pub struct AntigravityAcpRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub antigravity_bin: String,
    pub prompt: String,
    /// Staged image files, sent as native ACP `image` prompt parts.
    pub image_paths: Vec<PathBuf>,
    pub model: Option<String>,
    /// Continue an existing agy conversation (full id) instead of starting one.
    pub conversation_id: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct AntigravityAcpRunSummary {
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
struct AntigravityAcpSink {
    run: ConsoleRun,
    stdout_path: PathBuf,
}

static ANTIGRAVITY_CONSOLE: ConsoleProvider = ConsoleProvider {
    provider: "antigravity",
    adapter: ANTIGRAVITY_ACP_ADAPTER,
    tag: "antigravity-acp",
    lifetime: "one_shot",
};

impl std::ops::Deref for AntigravityAcpSink {
    type Target = ConsoleRun;
    fn deref(&self) -> &ConsoleRun {
        &self.run
    }
}

impl std::ops::DerefMut for AntigravityAcpSink {
    fn deref_mut(&mut self) -> &mut ConsoleRun {
        &mut self.run
    }
}

/// The provider argv for one Console turn: `agy acp` plus the Console
/// permission flags. Model and thought level are protocol calls
/// (`session/set_model`), not flags.
pub fn console_turn_argv() -> Vec<String> {
    std::iter::once("acp".to_string())
        .chain(crate::console_permission::console_argv("antigravity"))
        .collect()
}

fn run_dir_of(stdout_path: &Path) -> PathBuf {
    stdout_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."))
}

fn fifo_of(stdout_path: &Path) -> PathBuf {
    stdout_path.with_file_name(crate::console_rpc::RPC_STDIN)
}

pub async fn start_antigravity_acp_turn(
    config: AntigravityAcpRunConfig,
) -> Result<AntigravityAcpRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = normalized_optional(&config.turn_id) {
        validate_uuid(&turn_id, "turn_id")?;
    }
    // The conversation id must be the full UUID: agy silently starts a new
    // conversation when given a prefix.
    let resume_id = normalized_optional(&config.conversation_id);
    if let Some(id) = resume_id.as_deref() {
        validate_uuid(id, "conversation_id")?;
    }

    let run_dir = crate::config::get_agent_dir()?
        .join("antigravity-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    let stdout_path = run_dir.join("stdout.log");
    let stderr_path = run_dir.join("stderr.log");
    let stdout_file = private_output_file(&stdout_path)?;
    let stderr_file = private_output_file(&stderr_path)?;
    // Ownership is recorded as a local claim, so the launch no longer needs the
    // archive database to open first, and a resumed conversation's transcript
    // is fenced before agy can touch it.
    if let Some(id) = resume_id.as_deref() {
        persist_transcript_binding(
            &conversation_transcript_path(&antigravity_brain_root().context("HOME is unset")?, id),
            &config.session_id,
            id,
        )?;
        crate::turn_claims::default_registry()?.mark_provider_binding(&config.run_id, id, None)?;
    }

    let fifo = fifo_of(&stdout_path);
    crate::console_rpc::create_fifo(&fifo)?;
    let args = console_turn_argv();
    let argv = std::iter::once(config.antigravity_bin.clone())
        .chain(args.iter().cloned())
        .collect::<Vec<_>>();

    let launch_id = Uuid::new_v4().to_string();
    let mut command = Command::new(&config.antigravity_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file));
    ManagedIdentity::new(ManagedProvider::Antigravity, &config.session_id)
        .with_run_id(&config.run_id)
        .apply(&mut command, &[("LONGHOUSE_ANTIGRAVITY_ACP_MODE", "1")]);
    let fifo_c = std::ffi::CString::new(fifo.as_os_str().as_bytes())?;
    #[cfg(unix)]
    crate::console_sink::own_process_group(&mut command, move || {
        // SAFETY: runs in the forked child before exec; open/dup2/close only.
        unsafe { crate::console_rpc::adopt_fifo_as_stdin(&fifo_c) }
    });
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Err(error).with_context(|| format!("spawning `{}` acp", config.antigravity_bin))
        }
    };
    let Some(pid) = child.id() else {
        let _ = child.kill().await;
        anyhow::bail!("agy acp returned no pid");
    };
    let process_group_id =
        i32::try_from(pid).context("Antigravity pid exceeds process-group range")?;
    let sink = AntigravityAcpSink {
        run: ConsoleRun {
            provider: &ANTIGRAVITY_CONSOLE,
            session_id: config.session_id.clone(),
            thread_id: config.thread_id.clone(),
            turn_id: config.turn_id.clone(),
            run_id: config.run_id.clone(),
            client_request_id: config.client_request_id.clone(),
            launch_id: launch_id.clone(),
            process_group_id: Some(process_group_id),
            machine_name: config.machine_name.clone(),
            local_db_path: config.local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        },
        stdout_path: stdout_path.clone(),
    };
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "antigravity",
        "transport": ANTIGRAVITY_ACP_ADAPTER,
        // The ACP model is chosen by `session/set_model`, not argv, so the claim records it
        // (the factory's Console lifecycle checks the selected model here).
        "model": config.model,
        "provider_thread_id": resume_id,
        "launch_id": launch_id,
        "pid": pid,
        "process_group_id": process_group_id,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "cwd": config.cwd,
        "machine_name": config.machine_name,
        "argv": argv,
    });
    if let Err(error) = crate::turn_claims::default_registry()?.mark_spawned_invocation(
        &config.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        ANTIGRAVITY_ACP_ADAPTER,
        &launch_id,
        resume_id.as_deref(),
        &stdout_path.to_string_lossy(),
        &stderr_path.to_string_lossy(),
        result,
    ) {
        abandon_turn(&mut child, Some(process_group_id), &fifo).await;
        return Err(error).context("persisting Antigravity Console spawn identity");
    }

    let session_id = match handshake(&config, &stdout_path, resume_id.as_deref()).await {
        Ok(session_id) => session_id,
        Err(error) => {
            abandon_turn(&mut child, Some(process_group_id), &fifo).await;
            let tail = stderr_tail(&stderr_path)
                .map(|tail| format!(": {tail}"))
                .unwrap_or_default();
            return Err(error.context(format!("agy acp handshake failed{tail}")));
        }
    };
    sink.bind_session(&session_id, false).await;

    let monitor_stderr = stderr_path.clone();
    #[cfg(test)]
    let monitor_launch_id = launch_id.clone();
    let monitor_task = tokio::spawn(async move {
        monitor_acp_turn(
            MonitorProcess::Live(&mut child),
            &monitor_stderr,
            sink,
            false,
        )
        .await;
    });
    #[cfg(test)]
    crate::console_lifecycle::track_test_monitor(monitor_launch_id, monitor_task);
    #[cfg(not(test))]
    drop(monitor_task);

    Ok(AntigravityAcpRunSummary {
        session_id: config.session_id,
        thread_id: config.thread_id,
        run_id: config.run_id,
        provider_thread_id: Some(session_id),
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        argv,
    })
}

/// A launch that failed before its monitor existed: leave nothing running.
async fn abandon_turn(child: &mut Child, process_group_id: Option<i32>, fifo: &Path) {
    cleanup_process_group(process_group_id).await;
    let _ = child.kill().await;
    let _ = child.wait().await;
    let _ = std::fs::remove_file(fifo);
}

fn rpc_request(id: i64, method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

fn rpc_notification(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "method": method, "params": params})
}

/// The JSON-RPC response with `id` in the provider's stdout, if it has come.
fn find_rpc_response(stdout_path: &Path, id: i64) -> Option<Value> {
    let raw = std::fs::read(stdout_path).ok()?;
    raw.split(|byte| *byte == b'\n')
        .filter_map(|line| serde_json::from_slice::<Value>(line).ok())
        .find(|frame| {
            frame.get("id").and_then(Value::as_i64) == Some(id)
                && frame.get("method").is_none()
                && (frame.get("result").is_some() || frame.get("error").is_some())
        })
}

fn rpc_error_text(error: &Value) -> String {
    error
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| error.to_string())
}

/// Write one request and wait for its response in stdout.
async fn rpc_call(
    fifo: &Path,
    stdout_path: &Path,
    id: i64,
    method: &str,
    params: Value,
    deadline: Duration,
) -> Result<Value> {
    crate::console_rpc::write_command(fifo, &rpc_request(id, method, params)).await?;
    let until = tokio::time::Instant::now() + deadline;
    loop {
        if let Some(frame) = find_rpc_response(stdout_path, id) {
            if let Some(error) = frame.get("error") {
                anyhow::bail!("{method} failed: {}", rpc_error_text(error));
            }
            return Ok(frame.get("result").cloned().unwrap_or(Value::Null));
        }
        anyhow::ensure!(
            tokio::time::Instant::now() < until,
            "{method} was not answered within {}s",
            deadline.as_secs()
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

/// The ACP `prompt` content blocks: the text, then one native `image` block per
/// staged file.
fn prompt_blocks(text: &str, image_paths: &[PathBuf]) -> Result<Vec<Value>> {
    let mut blocks = vec![json!({"type": "text", "text": text})];
    blocks.extend(crate::console_rpc::image_entries(image_paths)?);
    Ok(blocks)
}

/// `initialize`, then `session/new` (or `session/resume` of the full id),
/// `session/set_model` when a model was chosen, then the first `session/prompt`.
/// Returns the native session id, already confirmed by the provider.
async fn handshake(
    config: &AntigravityAcpRunConfig,
    stdout_path: &Path,
    resume_id: Option<&str>,
) -> Result<String> {
    let fifo = fifo_of(stdout_path);
    let cwd = config.cwd.to_string_lossy().to_string();
    rpc_call(
        &fifo,
        stdout_path,
        INITIALIZE_ID,
        "initialize",
        json!({
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false},
        }),
        HANDSHAKE_DEADLINE,
    )
    .await?;
    let session = match resume_id {
        Some(id) => {
            rpc_call(
                &fifo,
                stdout_path,
                SESSION_ID_REQUEST,
                "session/resume",
                json!({"sessionId": id, "cwd": cwd, "mcpServers": []}),
                HANDSHAKE_DEADLINE,
            )
            .await?
        }
        None => {
            rpc_call(
                &fifo,
                stdout_path,
                SESSION_ID_REQUEST,
                "session/new",
                json!({"cwd": cwd, "mcpServers": []}),
                HANDSHAKE_DEADLINE,
            )
            .await?
        }
    };
    let session_id = session
        .get("sessionId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| resume_id.map(str::to_string))
        .context("the session response carries no sessionId")?;
    validate_uuid(&session_id, "Antigravity session id")?;
    if let Some(expected) = resume_id {
        anyhow::ensure!(
            expected == session_id,
            "agy resumed conversation {session_id} instead of the requested {expected}"
        );
    }
    if let Some(model) = normalized_optional(&config.model) {
        rpc_call(
            &fifo,
            stdout_path,
            SET_MODEL_ID,
            "session/set_model",
            json!({"sessionId": session_id, "modelId": model}),
            HANDSHAKE_DEADLINE,
        )
        .await?;
    }
    let prompt = prompt_blocks(&config.prompt, &config.image_paths)?;
    record_prompt(&run_dir_of(stdout_path), FIRST_PROMPT_ID, "prompt")?;
    crate::console_rpc::write_command(
        &fifo,
        &rpc_request(
            FIRST_PROMPT_ID,
            "session/prompt",
            json!({"sessionId": session_id, "prompt": prompt}),
        ),
    )
    .await?;
    Ok(session_id)
}

/// Lock the sidecar for the duration of `f`. Held by the monitor while it
/// decides the turn is over and by a steer while it checks and appends, so a
/// steer is either seen by the monitor or refused.
fn with_sidecar_lock<T>(run_dir: &Path, f: impl FnOnce(&mut File) -> Result<T>) -> Result<T> {
    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .append(true)
        .mode(0o600)
        .open(run_dir.join(PROMPTS_SIDECAR))
        .context("opening the Antigravity prompt sidecar")?;
    // SAFETY: flock on a descriptor we own; released when the file drops.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(std::io::Error::last_os_error()).context("locking the prompt sidecar");
    }
    f(&mut file)
}

fn record_prompt(run_dir: &Path, id: i64, kind: &str) -> Result<()> {
    with_sidecar_lock(run_dir, |file| append_prompt(file, id, kind))
}

fn append_prompt(file: &mut File, id: i64, kind: &str) -> Result<()> {
    let mut line = serde_json::to_vec(&json!({"id": id, "kind": kind}))?;
    line.push(b'\n');
    file.write_all(&line)?;
    file.sync_data().ok();
    Ok(())
}

/// Prompt ids issued so far, in order.
fn read_issued_prompts(run_dir: &Path) -> Vec<i64> {
    std::fs::read_to_string(run_dir.join(PROMPTS_SIDECAR))
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter_map(|entry| entry.get("id").and_then(Value::as_i64))
        .collect()
}

/// Enter the running Console turn: a second `session/prompt`, which agy lands
/// between tool steps. The first prompt's RPC then returns `end_turn` at once
/// while the turn continues; the monitor ends the turn when every issued prompt
/// has a reply.
pub async fn steer_antigravity_acp_turn(
    run_id: &str,
    session_id: &str,
    text: &str,
    image_paths: &[PathBuf],
) -> std::result::Result<(), String> {
    let not_steerable = || "turn_not_steerable".to_string();
    let registry = crate::turn_claims::default_registry().map_err(|err| err.to_string())?;
    let claim = registry.read(run_id).map_err(|_| not_steerable())?;
    if claim.session_id != session_id
        || claim.provider != "antigravity"
        || claim.adapter.as_deref() != Some(ANTIGRAVITY_ACP_ADAPTER)
        || claim.state != "spawned"
        || claim.cancel_requested_at.is_some()
        || claim_process_liveness(&claim) != ClaimLiveness::Live
    {
        return Err(not_steerable());
    }
    let native_session = claim.provider_thread_id.clone().ok_or_else(not_steerable)?;
    let stdout_path = PathBuf::from(claim.stdout_path.clone().ok_or_else(not_steerable)?);
    let run_dir = run_dir_of(&stdout_path);
    let fifo = fifo_of(&stdout_path);
    if !fifo.exists() {
        return Err(not_steerable());
    }
    let prompt = prompt_blocks(text, image_paths).map_err(|error| {
        eprintln!("[antigravity-acp] steer not sent, an image could not be read: {error:#}");
        "steer_failed".to_string()
    })?;
    // Microsecond clock: unique among a run's prompts and an integer JSON-RPC
    // id well inside f64's exact range.
    let id = Utc::now().timestamp_micros();
    let admitted = with_sidecar_lock(&run_dir, |file| {
        if run_dir.join(CLOSING_MARKER).exists() {
            return Ok(false);
        }
        append_prompt(file, id, "steer")?;
        Ok(true)
    })
    .map_err(|_| not_steerable())?;
    if !admitted {
        return Err(not_steerable());
    }
    crate::console_rpc::write_command(
        &fifo,
        &rpc_request(
            id,
            "session/prompt",
            json!({"sessionId": native_session, "prompt": prompt}),
        ),
    )
    .await
    .map_err(|_| not_steerable())
}

/// Stop the running Console turn: `session/cancel`, then the monitor settles
/// the claim as cancelled once the prompts resolve, or kills the process group
/// after [`CANCEL_GRACE`].
pub async fn interrupt_antigravity_acp_turn(
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
        || claim.provider != "antigravity"
    {
        anyhow::bail!(
            "Antigravity Console turn claim does not match the requested session, thread, or turn"
        );
    }
    if !is_antigravity_acp_claim(&claim) || claim.state != "spawned" {
        anyhow::bail!("Antigravity Console turn is not active");
    }
    let pid = claim
        .pid
        .context("Antigravity Console turn has no provider pid")?;
    let expected_start = claim
        .process_start_time
        .as_deref()
        .context("Antigravity Console turn has no process-start identity")?;
    let actual = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .cloned()
        .context("Antigravity Console provider process is gone")?;
    if actual.lstart != expected_start {
        anyhow::bail!("Antigravity Console provider pid identity changed");
    }
    let pgid = claim
        .process_group_id
        .context("Antigravity Console turn has no process-group identity")?;
    let actual_pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
    if actual_pgid != pgid || crate::process_group::leader_group_for(pid) != Some(pgid) {
        anyhow::bail!("Antigravity Console provider process-group identity changed");
    }
    registry.mark_cancel_requested(run_id)?;
    let stdout_path = PathBuf::from(
        claim
            .stdout_path
            .clone()
            .context("Antigravity Console turn has no stdout path")?,
    );
    // The claim is already marked, so the monitor's grace timer settles a turn
    // that ignores the notification. When the notification cannot be delivered
    // at all (no native session yet, or no process reading the FIFO) waiting
    // the grace out would only delay the kill, so the group dies now.
    let delivered = match claim.provider_thread_id.as_deref() {
        Some(native_session) => crate::console_rpc::write_command(
            &fifo_of(&stdout_path),
            &rpc_notification("session/cancel", json!({"sessionId": native_session})),
        )
        .await
        .is_ok(),
        None => false,
    };
    if !delivered {
        cleanup_process_group(Some(pgid)).await;
    }
    Ok(())
}

/// Settle or re-adopt Console turns that outlived the engine that spawned them.
///
/// Without this an engine restart strands every in-flight claim non-terminal:
/// the session shows a turn that never ends, and the run is never reported
/// failed, cancelled or complete.
pub async fn recover_antigravity_acp_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let agent_dir = crate::config::get_agent_dir()?;
    // One coherent inventory for recorded PIDs. Unrecorded spawns are checked
    // against their inherited stdout instead of treating a missing PID as dead.
    let inventory = crate::process_identity::try_collect_process_facts_by_pid();
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        let unrecorded_spawn =
            claim.provider == "antigravity" && claim.state == "claimed" && claim.adapter.is_none();
        if !unrecorded_spawn && (!is_antigravity_acp_claim(&claim) || claim.state != "spawned") {
            continue;
        }
        let stdout_path = claim_stdout_path(&claim, &agent_dir);
        let stderr_path = claim
            .stderr_path
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| stdout_path.with_file_name("stderr.log"));
        let sink = AntigravityAcpSink {
            run: ConsoleRun {
                provider: &ANTIGRAVITY_CONSOLE,
                session_id: claim.session_id.clone(),
                thread_id: claim.thread_id.clone(),
                turn_id: claim.turn_id.clone(),
                run_id: claim.run_id.clone(),
                client_request_id: claim.client_request_id.clone(),
                launch_id: claim.launch_id.clone().unwrap_or_default(),
                process_group_id: claim.process_group_id,
                machine_name: machine_name.to_string(),
                local_db_path: local_db_path.clone(),
                runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
            },
            stdout_path,
        };
        let liveness =
            recovered_claim_liveness(&claim, &sink.stdout_path, inventory.as_ref()).await;
        if liveness == ClaimLiveness::Gone && live_pid_identity_mismatch(&claim, inventory.as_ref())
        {
            tracing::warn!(
                run_id = %claim.run_id,
                "PID identity changed during Antigravity Console recovery; leaving the claim untouched"
            );
            continue;
        }
        match liveness {
            ClaimLiveness::Gone => {
                settle_recovered_dead_claim(&claim, &stderr_path, &sink).await;
            }
            ClaimLiveness::Live | ClaimLiveness::Unknown => {
                if liveness == ClaimLiveness::Unknown {
                    tracing::warn!(
                        run_id = %claim.run_id,
                        "Execution evidence unavailable; retrying Antigravity Console recovery"
                    );
                }
                #[cfg(test)]
                let monitor_launch_id = sink.launch_id.clone();
                let monitor_task = tokio::spawn(async move {
                    monitor_acp_turn(MonitorProcess::Recovered(claim), &stderr_path, sink, true)
                        .await;
                });
                #[cfg(test)]
                crate::console_lifecycle::track_test_monitor(monitor_launch_id, monitor_task);
                #[cfg(not(test))]
                drop(monitor_task);
                recovered += 1;
            }
        }
    }
    Ok(recovered)
}

enum MonitorProcess<'a> {
    /// The child this engine spawned; its exit status is observable.
    Live(&'a mut Child),
    /// A claim adopted after an engine restart; liveness comes from the claim.
    Recovered(crate::turn_claims::TurnClaim),
}

/// How one prompt's RPC ended.
#[derive(Debug, Clone, PartialEq, Eq)]
enum PromptReply {
    Stopped { reason: String, has_usage: bool },
    Failed(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Verdict {
    Pending,
    Completed,
    Failed(String),
    Cancelled,
}

/// One live signal projected from an ACP `session/update`.
#[derive(Debug, Clone, PartialEq, Eq)]
enum LiveSignal {
    /// Reply text of one message, accumulated so far.
    Text {
        item_id: String,
        seq: u64,
        delta: String,
        text: String,
        completed: bool,
    },
    /// A tool call: in progress carries the call, completed adds the output.
    Tool {
        item_id: String,
        seq: u64,
        command: String,
        output: String,
        completed: bool,
    },
}

/// What the stdout stream has said so far, folded incrementally.
#[derive(Debug, Default)]
struct AcpStream {
    run_id: String,
    offset: u64,
    pending: Vec<u8>,
    seq: u64,
    message_index: u64,
    message_text: String,
    replies: HashMap<i64, PromptReply>,
    /// The text of an error agy reported as an ordinary agent message.
    executor_error: Option<String>,
    session_id: Option<String>,
}

impl AcpStream {
    fn new(run_id: &str) -> Self {
        Self {
            run_id: run_id.to_string(),
            ..Self::default()
        }
    }

    fn message_item(&self) -> String {
        format!("{}:message:{}", self.run_id, self.message_index)
    }

    fn close_message(&mut self, signals: &mut Vec<LiveSignal>) {
        if self.message_text.is_empty() {
            return;
        }
        self.seq += 1;
        signals.push(LiveSignal::Text {
            item_id: self.message_item(),
            seq: self.seq,
            delta: String::new(),
            text: std::mem::take(&mut self.message_text),
            completed: true,
        });
        self.message_index += 1;
    }

    fn apply(&mut self, frame: &Value, signals: &mut Vec<LiveSignal>) {
        if frame.get("method").and_then(Value::as_str) == Some("session/update") {
            if let Some(update) = frame.pointer("/params/update") {
                self.apply_update(update, signals);
            }
            return;
        }
        if frame.get("method").is_some() {
            // A request from the provider (permission, fs). Console runs in
            // bypass and declares no client capabilities, so none are expected.
            return;
        }
        let Some(id) = frame.get("id").and_then(Value::as_i64) else {
            return;
        };
        if let Some(error) = frame.get("error") {
            self.replies
                .insert(id, PromptReply::Failed(rpc_error_text(error)));
            return;
        }
        let Some(result) = frame.get("result") else {
            return;
        };
        if id == SESSION_ID_REQUEST {
            self.session_id = result
                .get("sessionId")
                .and_then(Value::as_str)
                .map(str::to_string);
        }
        if let Some(reason) = result.get("stopReason").and_then(Value::as_str) {
            self.replies.insert(
                id,
                PromptReply::Stopped {
                    reason: reason.to_string(),
                    has_usage: result.get("usage").is_some(),
                },
            );
            self.close_message(signals);
        }
    }

    fn apply_update(&mut self, update: &Value, signals: &mut Vec<LiveSignal>) {
        match update.get("sessionUpdate").and_then(Value::as_str) {
            Some("agent_message_chunk") => {
                if crate::qa_fault::mutant("antigravity-message-chunk-arm") {
                    return;
                }
                let text = update
                    .pointer("/content/text")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if text.is_empty() {
                    return;
                }
                if text.trim_start().starts_with(EXECUTOR_ERROR_MARKER) {
                    let held = self.executor_error.get_or_insert_with(String::new);
                    held.push_str(text);
                }
                self.message_text.push_str(text);
                self.seq += 1;
                signals.push(LiveSignal::Text {
                    item_id: self.message_item(),
                    seq: self.seq,
                    delta: text.to_string(),
                    text: self.message_text.clone(),
                    completed: false,
                });
            }
            Some("tool_call") | Some("tool_call_update") => {
                // A tool call ends the reply text that preceded it.
                self.close_message(signals);
                let Some(call_id) = update.get("toolCallId").and_then(Value::as_str) else {
                    return;
                };
                let name = update.get("name").and_then(Value::as_str).unwrap_or("tool");
                let args = update.get("args").or_else(|| update.get("rawInput"));
                let command = args
                    .and_then(|args| args.get("CommandLine"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| match args {
                        Some(args) if !args.is_null() => format!("{name} {args}"),
                        _ => name.to_string(),
                    });
                let status = update.get("status").and_then(Value::as_str).unwrap_or("");
                let completed = matches!(status, "completed" | "failed");
                let output = update
                    .pointer("/rawOutput/output")
                    .and_then(Value::as_str)
                    .or_else(|| {
                        update
                            .pointer("/content/0/content/text")
                            .and_then(Value::as_str)
                    })
                    .unwrap_or_default()
                    .to_string();
                self.seq += 1;
                signals.push(LiveSignal::Tool {
                    item_id: format!("{}:tool:{call_id}", self.run_id),
                    seq: self.seq,
                    command,
                    output,
                    completed,
                });
            }
            _ => {}
        }
    }

    /// Apply complete stdout lines. Of consecutive text signals for one message
    /// only the newest is kept: each already carries the whole text.
    fn apply_lines(&mut self, lines: &[Vec<u8>]) -> Vec<LiveSignal> {
        let mut signals: Vec<LiveSignal> = Vec::new();
        for line in lines {
            let Ok(frame) = serde_json::from_slice::<Value>(line) else {
                continue;
            };
            let before = signals.len();
            self.apply(&frame, &mut signals);
            for index in before..signals.len() {
                if index == 0 {
                    continue;
                }
                if let (LiveSignal::Text { item_id: last, .. }, LiveSignal::Text { item_id, .. }) =
                    (&signals[index - 1], &signals[index])
                {
                    if last == item_id {
                        signals.remove(index - 1);
                        break;
                    }
                }
            }
        }
        signals
    }

    fn poll(&mut self, stdout_path: &Path) -> Result<Vec<LiveSignal>> {
        let lines = match crate::console_adapter::read_growth(
            stdout_path,
            &mut self.offset,
            &mut self.pending,
        ) {
            Ok(lines) => lines,
            Err(error)
                if error
                    .downcast_ref::<std::io::Error>()
                    .is_some_and(|error| error.kind() == std::io::ErrorKind::NotFound) =>
            {
                return Ok(Vec::new())
            }
            Err(error) => return Err(error),
        };
        Ok(self.apply_lines(&lines))
    }

    /// Where the turn stands, given the prompts issued. A turn ends when every
    /// issued prompt has a reply: with a steer outstanding the first prompt's
    /// early `end_turn` does not end it.
    fn verdict(&self, issued: &[i64], cancel_requested: bool) -> Verdict {
        if issued.is_empty() || issued.iter().any(|id| !self.replies.contains_key(id)) {
            return Verdict::Pending;
        }
        let replies: Vec<&PromptReply> = issued
            .iter()
            .filter_map(|id| self.replies.get(id))
            .collect();
        // agy answers a `session/cancel` with `stopReason: cancelled`; that can
        // arrive before the monitor's next look at the claim's cancel mark.
        if cancel_requested
            || replies.iter().any(
                |reply| matches!(reply, PromptReply::Stopped { reason, .. } if reason == "cancelled"),
            )
        {
            return Verdict::Cancelled;
        }
        if let Some(PromptReply::Failed(message)) = replies
            .iter()
            .find(|reply| matches!(reply, PromptReply::Failed(_)))
        {
            return Verdict::Failed(message.clone());
        }
        // An error reported as a chunk ends with `end_turn` and no usage.
        if let Some(error) = self.executor_error.as_deref() {
            let usageless = replies.iter().any(|reply| {
                matches!(reply, PromptReply::Stopped { reason, has_usage: false } if reason == "end_turn")
            });
            if usageless {
                return Verdict::Failed(error.trim().to_string());
            }
        }
        for reply in replies {
            if let PromptReply::Stopped { reason, .. } = reply {
                if reason != "end_turn" {
                    return Verdict::Failed(format!("agy stopped the turn: {reason}"));
                }
            }
        }
        Verdict::Completed
    }
}

fn cancel_request(run_id: &str) -> Option<String> {
    crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(run_id))
        .ok()
        .and_then(|claim| claim.cancel_requested_at)
}

async fn monitor_acp_turn(
    mut process: MonitorProcess<'_>,
    stderr_path: &Path,
    sink: AntigravityAcpSink,
    recovered: bool,
) {
    let mut current_phase: &'static str = "thinking";
    let mut current_tool: Option<String> = None;
    let mut last_phase_at = Instant::now();
    sink.post_phase(current_phase, None).await;
    let run_dir = run_dir_of(&sink.stdout_path);
    let mut stream = AcpStream::new(&sink.run_id);
    let mut wake_sent = false;
    // A recovered monitor replays the whole stdout to rebuild its state and
    // publishes nothing from it: the first run already published those signals.
    let mut replaying = recovered;
    let mut tick = 0_u64;
    let mut cancel_at: Option<String> = None;
    const PHASE_KEEPALIVE_INTERVAL: Duration = Duration::from_secs(30);
    loop {
        match stream.poll(&sink.stdout_path) {
            Ok(signals) if !replaying => {
                for signal in signals {
                    match &signal {
                        LiveSignal::Tool {
                            command, completed, ..
                        } => {
                            if *completed {
                                current_phase = "thinking";
                                current_tool = None;
                            } else {
                                current_phase = "running";
                                current_tool = Some(command.clone());
                            }
                            sink.post_phase(current_phase, current_tool.clone()).await;
                            last_phase_at = Instant::now();
                        }
                        LiveSignal::Text { .. } => {
                            if current_phase != "thinking" || current_tool.is_some() {
                                current_phase = "thinking";
                                current_tool = None;
                                sink.post_phase(current_phase, None).await;
                                last_phase_at = Instant::now();
                            }
                        }
                    }
                    sink.post_event(&sink.live_event(&signal));
                }
            }
            Ok(_) => {}
            Err(error) => {
                tracing::warn!(run_id = %sink.run_id, %error, "Antigravity live stream read failed");
            }
        }
        replaying = false;
        if !wake_sent {
            if let Some(session) = stream
                .session_id
                .clone()
                .or_else(|| claim_session(&sink.run_id))
            {
                if sink.transcript_exists(&session) {
                    sink.bind_session(&session, false).await;
                    wake_sent = true;
                }
            }
        }
        if tick % 5 == 0 && cancel_at.is_none() {
            cancel_at = cancel_request(&sink.run_id);
        }
        tick += 1;
        let cancel_requested = cancel_at.is_some();
        let issued = read_issued_prompts(&run_dir);
        let mut verdict = stream.verdict(&issued, cancel_requested);
        let mut exit_code = None;
        let mut gone = false;
        if verdict == Verdict::Pending {
            let cancel_overdue = cancel_at
                .as_deref()
                .and_then(crate::console_rpc::age_since_rfc3339)
                .is_some_and(|age| age >= CANCEL_GRACE);
            let exited = match &mut process {
                MonitorProcess::Live(child) => match child.try_wait() {
                    Ok(Some(status)) => {
                        exit_code = status.code();
                        true
                    }
                    Ok(None) => false,
                    Err(error) => {
                        verdict = Verdict::Failed(error.to_string());
                        gone = true;
                        false
                    }
                },
                MonitorProcess::Recovered(claim) => {
                    if claim.state == "claimed" && claim.pid.is_none() {
                        recovered_claim_liveness(claim, &sink.stdout_path, None).await
                            == ClaimLiveness::Gone
                    } else {
                        claim_process_liveness(claim) == ClaimLiveness::Gone
                    }
                }
            };
            if exited {
                // Drain what the provider wrote on its way out.
                tokio::time::sleep(Duration::from_millis(150)).await;
                if let Ok(signals) = stream.poll(&sink.stdout_path) {
                    for signal in signals {
                        sink.post_event(&sink.live_event(&signal));
                    }
                }
                let cancel_now = cancel_at.is_some() || cancel_request(&sink.run_id).is_some();
                verdict = match stream.verdict(&read_issued_prompts(&run_dir), cancel_now) {
                    Verdict::Pending if cancel_now => Verdict::Cancelled,
                    Verdict::Pending => Verdict::Failed(
                        stderr_tail(stderr_path)
                            .unwrap_or_else(|| "agy exited before the turn ended".to_string()),
                    ),
                    other => other,
                };
                gone = true;
            } else if cancel_overdue {
                verdict = Verdict::Cancelled;
            }
        }
        if verdict == Verdict::Pending {
            if last_phase_at.elapsed() >= PHASE_KEEPALIVE_INTERVAL {
                sink.post_phase(current_phase, current_tool.clone()).await;
                last_phase_at = Instant::now();
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
            continue;
        }
        // Decide under the sidecar lock so a steer is either seen here or
        // refused: close the turn only if nothing new was issued meanwhile.
        if !gone && verdict != Verdict::Cancelled {
            let closed = with_sidecar_lock(&run_dir, |_| {
                if read_issued_prompts(&run_dir) != issued {
                    return Ok(false);
                }
                std::fs::write(run_dir.join(CLOSING_MARKER), b"")?;
                Ok(true)
            })
            .unwrap_or(true);
            if !closed {
                continue;
            }
        }
        finish_turn(
            &mut process,
            &sink,
            &mut stream,
            verdict,
            exit_code,
            stderr_path,
            gone,
        )
        .await;
        return;
    }
}

fn claim_session(run_id: &str) -> Option<String> {
    crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(run_id))
        .ok()
        .and_then(|claim| claim.provider_thread_id)
}

/// End the turn: close the session politely, then reap the whole process
/// group (agy leaks `run_command` children when signalled), drop the FIFO and
/// report the terminal state.
async fn finish_turn(
    process: &mut MonitorProcess<'_>,
    sink: &AntigravityAcpSink,
    stream: &mut AcpStream,
    verdict: Verdict,
    exit_code: Option<i32>,
    stderr_path: &Path,
    process_gone: bool,
) {
    let fifo = fifo_of(&sink.stdout_path);
    let session = stream
        .session_id
        .clone()
        .or_else(|| claim_session(&sink.run_id));
    if !process_gone && !matches!(verdict, Verdict::Cancelled) {
        if let Some(session) = session.as_deref() {
            let closed = rpc_call(
                &fifo,
                &sink.stdout_path,
                CLOSE_ID,
                "session/close",
                json!({"sessionId": session}),
                CLOSE_DEADLINE,
            )
            .await;
            if let Err(error) = closed {
                tracing::debug!(run_id = %sink.run_id, %error, "Antigravity session/close was not acknowledged");
            }
        }
    }
    if let Ok(signals) = stream.poll(&sink.stdout_path) {
        for signal in signals {
            sink.post_event(&sink.live_event(&signal));
        }
    }
    let mut verdict = verdict;
    if let (Verdict::Completed, Some(session)) = (&verdict, session.as_deref()) {
        let until = tokio::time::Instant::now() + TRANSCRIPT_WAIT;
        while !sink.transcript_exists(session) && tokio::time::Instant::now() < until {
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        if !sink.transcript_exists(session) {
            verdict = Verdict::Failed(format!(
                "agy conversation {session} has no transcript on disk"
            ));
        }
    }
    // Ownership is independent of turn success: cancellation and model errors
    // still leave a native transcript that belongs to this Console session.
    if let Some(session) = session.as_deref() {
        sink.bind_session(session, true).await;
    }
    // A dead process's group is signalled only when it is provably ours: a
    // recovered pid may have been reused, and its group with it.
    let reap = !process_gone
        || match &*process {
            MonitorProcess::Live(_) => true,
            MonitorProcess::Recovered(claim) => recovered_group_can_be_signalled(claim),
        };
    if reap {
        cleanup_process_group(sink.process_group_id).await;
    }
    if let MonitorProcess::Live(child) = process {
        let _ = tokio::time::timeout(Duration::from_secs(2), child.wait()).await;
    }
    let _ = std::fs::remove_file(&fifo);
    match verdict {
        Verdict::Completed => sink.post_terminal("run_completed", exit_code, None).await,
        Verdict::Cancelled => sink.post_terminal("run_cancelled", exit_code, None).await,
        Verdict::Failed(message) => {
            let detail = if message.is_empty() {
                stderr_tail(stderr_path)
            } else {
                Some(message)
            };
            sink.post_terminal("run_failed", exit_code, detail).await
        }
        Verdict::Pending => unreachable!("finish_turn is called with a decided verdict"),
    }
}

async fn settle_recovered_dead_claim(
    claim: &crate::turn_claims::TurnClaim,
    stderr_path: &Path,
    sink: &AntigravityAcpSink,
) {
    let mut stream = AcpStream::new(&claim.run_id);
    let _ = stream.poll(&sink.stdout_path);
    let cancel_requested = claim.cancel_requested_at.is_some();
    let issued = read_issued_prompts(&run_dir_of(&sink.stdout_path));
    let verdict = match stream.verdict(&issued, cancel_requested) {
        Verdict::Pending if cancel_requested => Verdict::Cancelled,
        Verdict::Pending => Verdict::Failed(
            stderr_tail(stderr_path)
                .unwrap_or_else(|| "agy exited before the turn ended".to_string()),
        ),
        other => other,
    };
    // The exit status died with the engine that owned the child.
    let mut process = MonitorProcess::Recovered(claim.clone());
    finish_turn(
        &mut process,
        sink,
        &mut stream,
        verdict,
        None,
        stderr_path,
        true,
    )
    .await;
}
fn live_pid_identity_mismatch(
    claim: &crate::turn_claims::TurnClaim,
    inventory: Option<&std::collections::HashMap<u32, crate::process_identity::ProcessFact>>,
) -> bool {
    let Some(pid) = claim.pid else {
        return false;
    };
    let Some(inventory) = inventory else {
        return !claim.process_group_is_from_this_boot();
    };
    let Some(fact) = inventory.get(&pid) else {
        return false;
    };
    !claim.process_group_is_from_this_boot()
        || claim
            .process_start_time
            .as_deref()
            .is_none_or(|start| start != fact.lstart)
}

fn recovered_group_can_be_signalled(claim: &crate::turn_claims::TurnClaim) -> bool {
    let (Some(pid), Some(process_group_id)) = (claim.pid, claim.process_group_id) else {
        return false;
    };
    process_group_id == pid as i32
        && claim.process_group_is_from_this_boot()
        && crate::process_identity::try_collect_process_fact(pid).is_none()
}

/// A claim is durable before spawn, but the PID is durable only afterwards.
/// The deterministic stdout is opened before spawn and inherited by the child.
/// Its absence proves no child was launched; an existing file needs an exact
/// writer check, not an age cutoff or a guess based on the provider's argv.
async fn recovered_claim_liveness(
    claim: &crate::turn_claims::TurnClaim,
    stdout_path: &Path,
    inventory: Option<&std::collections::HashMap<u32, crate::process_identity::ProcessFact>>,
) -> ClaimLiveness {
    if claim.state != "claimed" || claim.pid.is_some() {
        return crate::console_adapter::claim_liveness(claim, inventory);
    }
    match stdout_path.try_exists() {
        Ok(false) => return ClaimLiveness::Gone,
        Ok(true) => {}
        Err(_) => return ClaimLiveness::Unknown,
    }
    #[cfg(target_os = "linux")]
    {
        return unrecorded_stdout_liveness(stdout_path, &claim.claimed_at);
    }
    #[cfg(target_os = "macos")]
    {
        let mut command = Command::new("lsof");
        command
            .args(["-nP", "-Fp", "--"])
            .arg(stdout_path)
            .kill_on_drop(true);
        // A normal full process scan on a busy Mac can exceed one second.
        // Bound recovery without mistaking that ordinary cost for lost evidence.
        let output = match tokio::time::timeout(Duration::from_secs(5), command.output()).await {
            Ok(Ok(output)) => output,
            Ok(Err(error)) => {
                tracing::debug!(run_id = %claim.run_id, %error, "Cannot inspect Antigravity stdout ownership with lsof");
                return ClaimLiveness::Unknown;
            }
            Err(_) => {
                tracing::debug!(run_id = %claim.run_id, "Antigravity stdout ownership inspection timed out");
                return ClaimLiveness::Unknown;
            }
        };
        if !output.stderr.is_empty() {
            tracing::debug!(run_id = %claim.run_id, stderr = %String::from_utf8_lossy(&output.stderr), "Antigravity stdout ownership inspection reported warnings");
        }
        lsof_output_liveness(&output)
    }
    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    ClaimLiveness::Unknown
}

#[cfg(any(target_os = "macos", test))]
fn lsof_output_liveness(output: &std::process::Output) -> ClaimLiveness {
    // A found holder is positive evidence even if an unrelated mount could
    // not be scanned. An incomplete negative scan never proves death.
    if String::from_utf8_lossy(&output.stdout).lines().any(|line| {
        line.strip_prefix('p')
            .and_then(|pid| pid.parse::<u32>().ok())
            .is_some_and(|pid| pid > 0)
    }) {
        return ClaimLiveness::Live;
    }
    if output.status.code() == Some(1) && output.stdout.is_empty() && output.stderr.is_empty() {
        return ClaimLiveness::Gone;
    }
    ClaimLiveness::Unknown
}

#[cfg(target_os = "linux")]
fn unrecorded_stdout_liveness(stdout_path: &Path, claimed_at: &str) -> ClaimLiveness {
    let inspect = || -> std::io::Result<ClaimLiveness> {
        let stdout = std::fs::metadata(stdout_path)?;
        Ok(stdout_liveness_from_processes(
            std::fs::read_dir("/proc")?,
            &stdout,
            crate::process_identity::parse_rfc3339(claimed_at),
            |pid| crate::process_identity::try_collect_process_fact(pid)?.start_time,
        ))
    };
    inspect().unwrap_or_else(|error| {
        tracing::debug!(path = %stdout_path.display(), %error, "Cannot inspect Antigravity stdout ownership through /proc");
        ClaimLiveness::Unknown
    })
}

#[cfg(target_os = "linux")]
fn stdout_liveness_from_processes(
    processes: impl IntoIterator<Item = std::io::Result<std::fs::DirEntry>>,
    stdout: &std::fs::Metadata,
    claimed_at: Option<chrono::DateTime<Utc>>,
    mut fresh_start_time: impl FnMut(u32) -> Option<chrono::DateTime<Utc>>,
) -> ClaimLiveness {
    let mut incomplete = false;
    for entry in processes {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => {
                incomplete = true;
                continue;
            }
        };
        let Ok(pid) = entry.file_name().to_string_lossy().parse::<u32>() else {
            continue;
        };
        match process_stdout_liveness(&entry.path(), stdout) {
            Ok(ClaimLiveness::Live) => return ClaimLiveness::Live,
            Ok(ClaimLiveness::Gone) => continue,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Ok(ClaimLiveness::Unknown) | Err(_) => {}
        }
        // An inaccessible fd table is not evidence about every claim. The
        // provider and its descendants are born after the durable claim; an
        // older process cannot have inherited this invocation's stdout.
        // Resolve identity after the failed inspection, not from the startup
        // inventory, which could describe a previous occupant of this PID.
        // Leave two seconds for ps/boot-time rounding, not a claim-age timeout.
        let predates_claim = fresh_start_time(pid)
            .zip(claimed_at)
            .is_some_and(|(start, claim)| start + chrono::Duration::seconds(2) < claim);
        incomplete |= !predates_claim;
    }
    if incomplete {
        ClaimLiveness::Unknown
    } else {
        ClaimLiveness::Gone
    }
}

#[cfg(target_os = "linux")]
fn process_stdout_liveness(
    process_path: &Path,
    stdout: &std::fs::Metadata,
) -> std::io::Result<ClaimLiveness> {
    use std::os::unix::fs::MetadataExt;

    // A Console child inherits our uid. Keep the existing visibility boundary.
    if std::fs::metadata(process_path)?.uid() != unsafe { libc::geteuid() } {
        return Ok(ClaimLiveness::Gone);
    }
    let mut incomplete = false;
    for descriptor in std::fs::read_dir(process_path.join("fd"))? {
        match descriptor.and_then(|descriptor| std::fs::metadata(descriptor.path())) {
            Ok(metadata) if metadata.dev() == stdout.dev() && metadata.ino() == stdout.ino() => {
                return Ok(ClaimLiveness::Live);
            }
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(_) => incomplete = true,
        }
    }
    Ok(if incomplete {
        ClaimLiveness::Unknown
    } else {
        ClaimLiveness::Gone
    })
}

fn locate_conversation_transcript(conversation_id: &str) -> Option<PathBuf> {
    let path = conversation_transcript_path(&antigravity_brain_root()?, conversation_id);
    path.is_file().then_some(path)
}

fn conversation_transcript_path(brain_root: &Path, conversation_id: &str) -> PathBuf {
    brain_root
        .join(conversation_id)
        .join(".system_generated/logs/transcript_full.jsonl")
}

fn antigravity_brain_root() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(
        PathBuf::from(home)
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain"),
    )
}

fn claim_stdout_path(claim: &crate::turn_claims::TurnClaim, agent_dir: &Path) -> PathBuf {
    claim
        .stdout_path
        .as_ref()
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            agent_dir
                .join("antigravity-console")
                .join(&claim.session_id)
                .join(&claim.run_id)
                .join("stdout.log")
        })
}

fn is_antigravity_acp_claim(claim: &crate::turn_claims::TurnClaim) -> bool {
    claim.provider == "antigravity"
        && (claim.adapter.as_deref() == Some(ANTIGRAVITY_ACP_ADAPTER)
            || claim
                .result
                .as_ref()
                .and_then(|result| result.get("transport"))
                .and_then(Value::as_str)
                == Some(ANTIGRAVITY_ACP_ADAPTER))
}

/// The native session id the provider's stdout has confirmed: the
/// `session/new` or `session/resume` response. A partially appended frame is
/// pending, not an error.
fn read_session_identity(stdout_path: &Path) -> Result<Option<String>> {
    let Some(frame) = find_rpc_response(stdout_path, SESSION_ID_REQUEST) else {
        return Ok(None);
    };
    let Some(id) = frame.pointer("/result/sessionId").and_then(Value::as_str) else {
        return Ok(None);
    };
    validate_uuid(id, "Antigravity session id")?;
    Ok(Some(id.to_string()))
}

fn persist_transcript_binding(
    transcript: &Path,
    session_id: &str,
    provider_session_id: &str,
) -> Result<()> {
    // A local claim, projected by the daemon before discovery runs.
    crate::managed_source_claim::ensure_bindable(session_id, transcript, provider_session_id)?;
    if transcript
        .file_name()
        .is_some_and(|name| name == "transcript_full.jsonl")
    {
        // Earlier bindings may name the shortened sibling. That durable owner
        // still prevents another session from taking over; it does not
        // authorize another source to be enrolled beside the full transcript.
        crate::managed_source_claim::ensure_bindable(
            session_id,
            &transcript.with_file_name("transcript.jsonl"),
            provider_session_id,
        )?;
    }
    crate::managed_source_claim::confirm_identity(
        session_id,
        "antigravity",
        transcript,
        provider_session_id,
        None,
        None,
    )?;
    Ok(())
}

fn bind_source_owner(
    conn: &rusqlite::Connection,
    transcript: &Path,
    session_id: &str,
    provider_session_id: &str,
) -> Result<()> {
    let path = crate::storage_v2_shipper::stable_source_path(transcript);
    let path = path.to_string_lossy();
    let binding = crate::state::session_binding::SessionBinding::new(conn);
    if let Some(existing) = binding.get_for_provider(&path, "antigravity")? {
        anyhow::ensure!(
            existing == session_id,
            "Antigravity transcript already has another managed owner"
        );
    }
    // Earlier bindings may name the shortened sibling. That durable owner still
    // prevents another session from taking over; it does not authorize another
    // source to be enrolled beside the full native transcript.
    if transcript
        .file_name()
        .is_some_and(|name| name == "transcript_full.jsonl")
    {
        let legacy = crate::storage_v2_shipper::stable_source_path(
            &transcript.with_file_name("transcript.jsonl"),
        );
        if let Some(existing) =
            binding.get_for_provider(&legacy.to_string_lossy(), "antigravity")?
        {
            anyhow::ensure!(
                existing == session_id,
                "Antigravity transcript already has another managed owner"
            );
        }
    }
    binding.bind_for_thread(&path, session_id, "antigravity", Some(provider_session_id))
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum SourceOwnership {
    Managed(String),
    Pending,
    Unclaimed,
}

/// Resolve exact native identity from durable claims or structured stdout.
/// Unconfirmed nonterminal launches hold unowned sources until identity arrives;
/// once known, unrelated Shadow sources are released without being retagged.
/// Terminal claims remain ownership evidence but never create a global hold.
pub(crate) fn bind_discovered_source(
    conn: &rusqlite::Connection,
    path: &Path,
    claims: &[impl std::borrow::Borrow<crate::turn_claims::TurnClaim>],
    agent_dir: &Path,
) -> Result<SourceOwnership> {
    let Some(logs) = path
        .parent()
        .filter(|p| p.file_name().is_some_and(|n| n == "logs"))
    else {
        return Ok(SourceOwnership::Unclaimed);
    };
    let Some(system) = logs
        .parent()
        .filter(|p| p.file_name().is_some_and(|n| n == ".system_generated"))
    else {
        return Ok(SourceOwnership::Unclaimed);
    };
    let Some(conversation) = system.parent() else {
        return Ok(SourceOwnership::Unclaimed);
    };
    let Some(provider_id) = conversation
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|id| Uuid::parse_str(id).is_ok())
    else {
        return Ok(SourceOwnership::Unclaimed);
    };
    let mut owner: Option<&str> = None;
    let mut pending_owner = false;
    for claim in claims
        .iter()
        .map(std::borrow::Borrow::borrow)
        .filter(|claim: &&crate::turn_claims::TurnClaim| claim.provider == "antigravity")
    {
        let nonterminal = !matches!(claim.state.as_str(), "terminal" | "failed");
        let id = if claim.provider_identity_confirmed {
            match claim.provider_thread_id.as_deref() {
                Some(id) if Uuid::parse_str(id).is_ok() => Some(id.to_string()),
                _ => {
                    tracing::warn!(run_id = %claim.run_id, "Ignoring invalid confirmed Antigravity identity");
                    None
                }
            }
        } else {
            let stdout = claim_stdout_path(claim, agent_dir);
            match read_session_identity(&stdout) {
                Ok(id) => id,
                Err(error) => {
                    tracing::warn!(run_id = %claim.run_id, %error, "Skipping invalid Antigravity claim output");
                    None
                }
            }
        };
        pending_owner |= nonterminal && id.is_none();
        if id.as_deref() != Some(provider_id) {
            continue;
        }
        if let Some(existing) = owner {
            anyhow::ensure!(
                existing == claim.session_id,
                "Antigravity thread has conflicting Console claims"
            );
        }
        owner = Some(&claim.session_id);
    }
    if let Some(session_id) = owner {
        bind_source_owner(conn, path, session_id, provider_id)?;
        return Ok(SourceOwnership::Managed(session_id.to_string()));
    }
    Ok(if pending_owner {
        SourceOwnership::Pending
    } else {
        SourceOwnership::Unclaimed
    })
}

impl AntigravityAcpSink {
    fn transcript_exists(&self, session_id: &str) -> bool {
        locate_conversation_transcript(session_id).is_some()
    }

    /// Bind the native conversation to this Console session: local claim,
    /// turn-claim identity, binding event, and (once the file exists) a wake for
    /// the transcript shipper.
    async fn bind_session(&self, session_id: &str, turn_ended: bool) {
        let Some(brain) = antigravity_brain_root() else {
            tracing::warn!(run_id = %self.run_id, "HOME is unset; Antigravity source binding is pending");
            return;
        };
        let transcript = conversation_transcript_path(&brain, session_id);
        let bound =
            persist_transcript_binding(&transcript, &self.session_id, session_id).and_then(|()| {
                crate::turn_claims::default_registry()?.mark_provider_binding(
                    &self.run_id,
                    session_id,
                    Some(&transcript.to_string_lossy()),
                )
            });
        if let Err(error) = bound {
            tracing::warn!(run_id = %self.run_id, %error, "Antigravity source binding is pending");
            return;
        }
        self.post_binding(session_id, &transcript).await;
        if transcript.is_file() {
            self.wake_transcript_shipper(&transcript, session_id, turn_ended)
                .await;
        }
    }

    async fn post_binding(&self, provider_session_id: &str, transcript: &Path) {
        self.post_event(&self.binding_event(json!({
            "provider_session_id": provider_session_id,
            "source_path": transcript.to_string_lossy(),
        })));
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        self.publish_phase(phase, tool_name.as_deref(), None);
    }

    fn live_event(&self, signal: &LiveSignal) -> Value {
        let payload = match signal {
            LiveSignal::Text {
                item_id,
                seq,
                delta,
                text,
                completed,
            } => json!({
                "progress_kind": "bridge_live_transcript_delta",
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "item_id": item_id,
                "seq": seq,
                "item_seq": seq,
                "delta": delta,
                "live_text": text,
                "item_completed": completed,
                "turn_completed": false,
            }),
            LiveSignal::Tool {
                item_id,
                seq,
                command,
                output,
                completed,
            } => json!({
                "progress_kind": "console_live_tool_item",
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "item_id": item_id,
                "command": command,
                "output": output,
                "status": if *completed { "completed" } else { "running" },
                "seq": seq,
                "completed": completed,
            }),
        };
        let seq = match signal {
            LiveSignal::Text { seq, .. } | LiveSignal::Tool { seq, .. } => *seq,
        };
        self.run_event(
            "progress_signal",
            &format!("live:{seq}"),
            self.with_transport(payload),
        )
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr: Option<String>,
    ) {
        self.persist_local_phase("finished", None, Utc::now());
        let payload =
            self.terminal_payload(terminal_state, terminal_state, exit_code, stderr.as_deref());
        let terminal_event = self.run_event("terminal_signal", "terminal", payload);
        let terminal_error = (terminal_state == "run_failed")
            .then(|| stderr.clone())
            .flatten();
        self.hand_off_terminal(terminal_state, terminal_error, terminal_event);
    }

    #[cfg(unix)]
    async fn wake_transcript_shipper(
        &self,
        source_path: &Path,
        provider_session_id: &str,
        turn_ended: bool,
    ) {
        let Some(socket_path) = crate::config::get_agent_transcript_wake_socket_path().ok() else {
            return;
        };
        if !socket_path.exists() {
            return;
        }
        let payload = json!({
            "provider": "antigravity",
            "path": source_path,
            "phase": if turn_ended { "idle" } else { "thinking" },
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "provider_turn_id": provider_session_id,
            "client_request_id": self.client_request_id,
            "wake_reason": if turn_ended { "turn_completed" } else { "turn_started" },
            "observed_at_ms": Utc::now().timestamp_millis(),
            "file_len_hint": std::fs::metadata(source_path).ok().map(|metadata| metadata.len()),
        });
        let bytes = payload.to_string().into_bytes();
        let write = tokio::task::spawn_blocking(move || -> std::io::Result<()> {
            let mut stream = std::os::unix::net::UnixStream::connect(socket_path)?;
            stream.set_write_timeout(Some(Duration::from_millis(50)))?;
            stream.write_all(&bytes)
        });
        if tokio::time::timeout(Duration::from_millis(75), write)
            .await
            .is_err()
        {
            eprintln!(
                "[antigravity-print] latency stage=durable_wake_miss session={} run={} reason=timeout",
                self.session_id, self.run_id
            );
        }
    }

    #[cfg(not(unix))]
    async fn wake_transcript_shipper(
        &self,
        _source_path: &Path,
        _provider_session_id: &str,
        _turn_ended: bool,
    ) {
    }
}

async fn cleanup_process_group(process_group_id: Option<i32>) {
    crate::console_adapter::cleanup_process_group("antigravity-acp", process_group_id).await;
}

fn private_output_file(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .mode(0o600)
        .open(path)?)
}

fn set_private_dir(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = std::fs::metadata(path)?.permissions();
    permissions.set_mode(0o700);
    std::fs::set_permissions(path, permissions)?;
    Ok(())
}

fn validate_uuid(value: &str, field: &str) -> Result<()> {
    Uuid::parse_str(value).with_context(|| format!("{field} must be a UUID"))?;
    Ok(())
}

fn normalized_optional(value: &Option<String>) -> Option<String> {
    value
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden_antigravity_sink(
        home: &crate::console_sink_golden::GoldenHome,
    ) -> AntigravityAcpSink {
        use crate::console_sink_golden::*;
        AntigravityAcpSink {
            run: ConsoleRun {
                provider: &ANTIGRAVITY_CONSOLE,
                session_id: SESSION.to_string(),
                thread_id: THREAD.to_string(),
                turn_id: Some(TURN.to_string()),
                run_id: RUN.to_string(),
                client_request_id: Some(CLIENT_REQUEST.to_string()),
                launch_id: LAUNCH.to_string(),
                process_group_id: None,
                machine_name: MACHINE.to_string(),
                local_db_path: Some(home.local_db()),
                runtime_events_outbox_dir: home.outbox(),
            },
            stdout_path: home.temp.path().join("stdout.jsonl"),
        }
    }

    #[test]
    fn console_sink_golden_antigravity() {
        use crate::console_sink_golden::*;
        let home = GoldenHome::new("antigravity");
        let sink = golden_antigravity_sink(&home);
        let transcript = home.temp.path().join("brain/conversation.pb");
        let phases = std::cell::RefCell::new(Vec::new());
        let captured = home.run(async {
            sink.post_binding(PROVIDER_THREAD, &transcript).await;
            sink.post_phase("thinking", None).await;
            *phases.borrow_mut() = home.status_rows();
            sink.post_terminal("run_failed", Some(2), Some("boom".to_string()))
                .await;
        });
        let mut captured = captured;
        captured["status_before_terminal"] = json!(phases.into_inner());
        assert_golden("antigravity", &captured);
    }

    #[test]
    #[cfg(unix)]
    fn lsof_positive_holder_survives_unrelated_scan_warnings() {
        use std::os::unix::process::ExitStatusExt;
        let mut output = std::process::Output {
            status: std::process::ExitStatus::from_raw(256),
            stdout: b"p1234\n".to_vec(),
            stderr: b"WARNING: cannot stat unrelated filesystem\n".to_vec(),
        };
        assert_eq!(lsof_output_liveness(&output), ClaimLiveness::Live);
        output.stdout.clear();
        assert_eq!(lsof_output_liveness(&output), ClaimLiveness::Unknown);
        output.stderr.clear();
        assert_eq!(lsof_output_liveness(&output), ClaimLiveness::Gone);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn linux_stdout_scan_excludes_only_proven_pre_claim_inspection_failures() {
        use std::os::unix::fs::symlink;

        let dir = tempfile::tempdir().unwrap();
        let stdout = dir.path().join("stdout.log");
        std::fs::write(&stdout, "").unwrap();
        let stdout = std::fs::metadata(stdout).unwrap();
        let proc_root = dir.path().join("proc");
        let process = proc_root.join("123");
        std::fs::create_dir_all(&process).unwrap();
        // A symlink loop gives a deterministic fd-table inspection error,
        // including when the tests run as root and chmod cannot deny access.
        symlink("fd", process.join("fd")).unwrap();
        let claimed_at = Utc::now();
        let inspect = |claim, start| {
            stdout_liveness_from_processes(
                std::fs::read_dir(&proc_root).unwrap(),
                &stdout,
                claim,
                |_| start,
            )
        };
        let old_start = claimed_at - chrono::Duration::minutes(10);
        assert_eq!(
            inspect(Some(claimed_at), Some(old_start)),
            ClaimLiveness::Gone
        );
        // A provider born in the claim's second can round down in ps. Neither
        // that overlap nor unavailable claim/process identity proves absence.
        assert_eq!(
            inspect(
                Some(claimed_at),
                Some(claimed_at - chrono::Duration::seconds(1))
            ),
            ClaimLiveness::Unknown
        );
        assert_eq!(
            inspect(
                Some(claimed_at),
                Some(claimed_at + chrono::Duration::seconds(1))
            ),
            ClaimLiveness::Unknown
        );
        assert_eq!(inspect(Some(claimed_at), None), ClaimLiveness::Unknown);
        assert_eq!(inspect(None, Some(old_start)), ClaimLiveness::Unknown);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn linux_stdout_scan_finds_writer_after_process_and_descriptor_errors() {
        use std::os::unix::fs::symlink;

        let dir = tempfile::tempdir().unwrap();
        let stdout_path = dir.path().join("stdout.log");
        std::fs::write(&stdout_path, "").unwrap();
        let stdout = std::fs::metadata(&stdout_path).unwrap();
        let proc_root = dir.path().join("proc");
        std::fs::create_dir_all(proc_root.join("1")).unwrap();
        symlink("fd", proc_root.join("1/fd")).unwrap();
        for pid in ["2", "3"] {
            let fd = proc_root.join(pid).join("fd");
            std::fs::create_dir_all(&fd).unwrap();
            symlink("0", fd.join("0")).unwrap();
        }
        symlink(&stdout_path, proc_root.join("3/fd/1")).unwrap();
        let mut entries = std::fs::read_dir(&proc_root)
            .unwrap()
            .collect::<std::io::Result<Vec<_>>>()
            .unwrap();
        entries.sort_by_key(|entry| entry.file_name());
        // Force incomplete evidence before the holder, regardless of filesystem
        // enumeration order. Missing identity must not short-circuit positives.
        let entries = std::iter::once(Err(std::io::Error::from(
            std::io::ErrorKind::PermissionDenied,
        )))
        .chain(entries.into_iter().map(Ok));
        assert_eq!(
            stdout_liveness_from_processes(entries, &stdout, Some(Utc::now()), |_| None),
            ClaimLiveness::Live
        );
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn linux_stdout_scan_does_not_retain_vanished_descriptors() {
        use std::os::unix::fs::symlink;

        let dir = tempfile::tempdir().unwrap();
        let stdout_path = dir.path().join("stdout.log");
        std::fs::write(&stdout_path, "").unwrap();
        let stdout = std::fs::metadata(stdout_path).unwrap();
        let proc_root = dir.path().join("proc");
        let fd = proc_root.join("123/fd");
        std::fs::create_dir_all(&fd).unwrap();
        symlink("already-closed", fd.join("1")).unwrap();
        assert_eq!(
            stdout_liveness_from_processes(
                std::fs::read_dir(&proc_root).unwrap(),
                &stdout,
                Some(Utc::now()),
                |_| None,
            ),
            ClaimLiveness::Gone
        );
    }

    #[test]
    fn antigravity_startup_releases_abandoned_pre_spawn_claims_without_mutating_sources() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        let longhouse_home = dir.path().join("longhouse");
        let db_path = dir.path().join("state.db");
        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        temp_env::with_vars(
            [
                ("HOME", Some(home.as_os_str())),
                ("LONGHOUSE_HOME", Some(longhouse_home.as_os_str())),
            ],
            || {
                runtime.block_on(async {
                let registry = crate::turn_claims::default_registry().unwrap();
                let agent_dir = crate::config::get_agent_dir().unwrap();
                let native_id = Uuid::new_v4().to_string();
                let native = conversation_transcript_path(&antigravity_brain_root().unwrap(), &native_id);
                let shadow = conversation_transcript_path(&antigravity_brain_root().unwrap(), &Uuid::new_v4().to_string());
                let source = b"{\"step_index\":1,\"source\":\"MODEL\",\"type\":\"PLANNER_RESPONSE\",\"status\":\"DONE\",\"created_at\":\"2026-09-06T22:07:35Z\",\"content\":\"native answer\"}\n";
                for path in [&native, &shadow] {
                    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
                    std::fs::write(path, source).unwrap();
                }
                // These are distinct crash boundaries, not elapsed-time cases:
                // before output setup, before exec, mid-response, and after the session response.
                for output in [
                    None,
                    Some(String::new()),
                    Some("{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"sessionId\":\"".to_string()),
                    Some(format!("{{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{{\"sessionId\":\"{native_id}\"}}}}\n")),
                ] {
                    let run_id = Uuid::new_v4().to_string();
                    let session_id = Uuid::new_v4().to_string();
                    registry.claim(&run_id, &session_id, &Uuid::new_v4().to_string(), None, None, "antigravity").unwrap();
                    let claim = registry.read(&run_id).unwrap();
                    let stdout = claim_stdout_path(&claim, &agent_dir);
                    if let Some(output) = output {
                        std::fs::create_dir_all(stdout.parent().unwrap()).unwrap();
                        std::fs::write(&stdout, output).unwrap();
                    }
                    let has_identity = read_session_identity(&stdout).unwrap().is_some();
                    assert_eq!(
                        bind_discovered_source(&conn, &shadow, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                        if has_identity { SourceOwnership::Unclaimed } else { SourceOwnership::Pending },
                    );
                    assert_eq!(recover_antigravity_acp_turns("test", Some(db_path.clone())).await.unwrap(), 0);
                    assert_eq!(registry.read(&run_id).unwrap().state, "terminal");
                    let terminal = read_outbox_events(&crate::config::get_agent_runtime_events_outbox_dir().unwrap())
                        .into_iter().find(|event| event["run_id"] == run_id && event["kind"] == "terminal_signal").unwrap();
                    assert_eq!(terminal["payload"]["terminal_state"], "run_failed");
                    assert_eq!(
                        bind_discovered_source(&conn, &shadow, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                        SourceOwnership::Unclaimed,
                    );
                    if has_identity {
                        assert_eq!(
                            bind_discovered_source(&conn, &native, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                            SourceOwnership::Managed(session_id),
                        );
                    }
                    assert_eq!(std::fs::read(&native).unwrap(), source);
                    assert_eq!(std::fs::read(&shadow).unwrap(), source);
                }
            })
            },
        );
    }

    #[test]
    fn antigravity_startup_retains_unrecorded_live_writer_until_identity_and_exit() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let dir = tempfile::tempdir().unwrap();
        let home = dir.path().join("home");
        let longhouse_home = dir.path().join("longhouse");
        let db_path = dir.path().join("state.db");
        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        temp_env::with_vars(
            [
                ("HOME", Some(home.as_os_str())),
                ("LONGHOUSE_HOME", Some(longhouse_home.as_os_str())),
            ],
            || {
                runtime.block_on(async {
                use tokio::io::AsyncWriteExt;

                let registry = crate::turn_claims::default_registry().unwrap();
                let agent_dir = crate::config::get_agent_dir().unwrap();
                let run_id = Uuid::new_v4().to_string();
                let session_id = Uuid::new_v4().to_string();
                let native_id = Uuid::new_v4().to_string();
                let native = conversation_transcript_path(&antigravity_brain_root().unwrap(), &native_id);
                let shadow = conversation_transcript_path(&antigravity_brain_root().unwrap(), &Uuid::new_v4().to_string());
                let source = b"{\"step_index\":1,\"source\":\"MODEL\",\"type\":\"PLANNER_RESPONSE\",\"status\":\"DONE\",\"created_at\":\"2026-09-06T22:07:35Z\",\"content\":\"native answer\"}\n";
                std::fs::create_dir_all(native.parent().unwrap()).unwrap();
                std::fs::write(&native, source).unwrap();
                registry.claim(&run_id, &session_id, &Uuid::new_v4().to_string(), None, None, "antigravity").unwrap();
                let stdout = claim_stdout_path(&registry.read(&run_id).unwrap(), &agent_dir);
                std::fs::create_dir_all(stdout.parent().unwrap()).unwrap();
                std::fs::write(stdout.with_file_name(PROMPTS_SIDECAR), b"{\"id\":4,\"kind\":\"prompt\"}\n").unwrap();
                // The child owns the durable output, but the engine died
                // before persisting any PID, adapter or provider identity.
                let mut child = Command::new("/bin/sh")
                    .arg("-c")
                    .arg(format!(
                        "read first; printf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{{\"sessionId\":\"{native_id}\"}}}}'; read second; printf '%s\\n' '{{\"jsonrpc\":\"2.0\",\"id\":4,\"result\":{{\"stopReason\":\"end_turn\",\"usage\":{{\"inputTokens\":1}}}}}}'"
                    ))
                    .stdin(Stdio::piped())
                    .stdout(Stdio::from(private_output_file(&stdout).unwrap()))
                    .kill_on_drop(true)
                    .spawn().unwrap();
                assert_eq!(recover_antigravity_acp_turns("test", Some(db_path.clone())).await.unwrap(), 1);
                assert_eq!(registry.read(&run_id).unwrap().state, "claimed");
                assert_eq!(
                    bind_discovered_source(&conn, &shadow, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                    SourceOwnership::Pending,
                );
                child.stdin.as_mut().unwrap().write_all(b"\n").await.unwrap();
                tokio::time::timeout(Duration::from_secs(5), async {
                    while !registry.read(&run_id).unwrap().provider_identity_confirmed {
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                }).await.unwrap();
                assert_eq!(
                    bind_discovered_source(&conn, &native, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                    SourceOwnership::Managed(session_id.clone()),
                );
                assert_eq!(
                    bind_discovered_source(&conn, &shadow, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                    SourceOwnership::Unclaimed,
                );
                child.stdin.as_mut().unwrap().write_all(b"\n").await.unwrap();
                assert!(child.wait().await.unwrap().success());
                tokio::time::timeout(Duration::from_secs(5), async {
                    while registry.read(&run_id).unwrap().state != "terminal" {
                        tokio::time::sleep(Duration::from_millis(20)).await;
                    }
                }).await.unwrap();
                let terminal = read_outbox_events(&crate::config::get_agent_runtime_events_outbox_dir().unwrap())
                    .into_iter().find(|event| event["run_id"] == run_id && event["kind"] == "terminal_signal").unwrap();
                assert_eq!(terminal["payload"]["terminal_state"], "run_completed");
                assert_eq!(std::fs::read(&native).unwrap(), source);
            })
            },
        );
    }

    #[test]
    fn antigravity_legacy_mirror_owner_blocks_cross_session_rebinding() {
        let dir = tempfile::tempdir().unwrap();
        let conn = crate::state::db::open_db(Some(&dir.path().join("state.db"))).unwrap();
        let native_id = Uuid::new_v4().to_string();
        let transcript = conversation_transcript_path(dir.path(), &native_id);
        let legacy = transcript.with_file_name("transcript.jsonl");
        let session_id = Uuid::new_v4().to_string();
        bind_source_owner(&conn, &legacy, &session_id, &native_id).unwrap();
        assert!(
            bind_source_owner(&conn, &transcript, &Uuid::new_v4().to_string(), &native_id).is_err()
        );
        bind_source_owner(&conn, &transcript, &session_id, &native_id).unwrap();
        let binding = crate::state::session_binding::SessionBinding::new(&conn);
        for path in [&transcript, &legacy] {
            assert_eq!(
                binding
                    .get_for_provider(
                        &crate::storage_v2_shipper::stable_source_path(path).to_string_lossy(),
                        "antigravity"
                    )
                    .unwrap(),
                Some(session_id.clone()),
            );
        }
    }

    #[test]
    fn antigravity_console_and_discovery_share_one_source_across_current_and_legacy_turns() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let dir = tempfile::Builder::new()
            .prefix("agy-")
            .tempdir_in("/tmp")
            .unwrap();
        let home = dir.path().join("home");
        let longhouse_home = dir.path().join("longhouse");
        let db_path = dir.path().join("state.db");
        crate::state::db::open_db(Some(&db_path)).unwrap();
        temp_env::with_vars(
            [
                ("HOME", Some(home.as_os_str())),
                ("LONGHOUSE_HOME", Some(longhouse_home.as_os_str())),
            ],
            || {
                runtime.block_on(async {
            async fn receive_wake(listener: &tokio::net::UnixListener) -> Value {
                tokio::time::timeout(Duration::from_secs(2), async {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    let mut bytes = Vec::new();
                    tokio::io::AsyncReadExt::read_to_end(&mut stream, &mut bytes).await.unwrap();
                    serde_json::from_slice(&bytes).unwrap()
                }).await.unwrap()
            }

            let registry = crate::turn_claims::default_registry().unwrap();
            let agent_dir = crate::config::get_agent_dir().unwrap();
            let session_id = Uuid::new_v4().to_string();
            let provider_id = Uuid::new_v4().to_string();
            let transcript = conversation_transcript_path(&antigravity_brain_root().unwrap(), &provider_id);
            std::fs::create_dir_all(transcript.parent().unwrap()).unwrap();
            std::fs::write(&transcript, b"{\"step_index\":0,\"source\":\"USER_EXPLICIT\",\"type\":\"USER_INPUT\",\"status\":\"DONE\",\"created_at\":\"2026-09-06T22:07:35Z\",\"content\":\"hello\"}\n").unwrap();
            let native_bytes = std::fs::read(&transcript).unwrap();
            let mirror = transcript.with_file_name("transcript.jsonl");
            std::fs::write(&mirror, &native_bytes).unwrap();
            let socket = crate::config::get_agent_transcript_wake_socket_path().unwrap();
            std::fs::create_dir_all(socket.parent().unwrap()).unwrap();
            let listener = tokio::net::UnixListener::bind(&socket).unwrap();
            let make_sink = || {
                let run_id = Uuid::new_v4().to_string();
                let thread_id = Uuid::new_v4().to_string();
                registry.claim(&run_id, &session_id, &thread_id, None, None, "antigravity").unwrap();
                let run_dir = agent_dir.join("antigravity-console").join(&session_id).join(&run_id);
                std::fs::create_dir_all(&run_dir).unwrap();
                AntigravityAcpSink {
                    run: ConsoleRun {
                        provider: &ANTIGRAVITY_CONSOLE,
                        session_id: session_id.clone(),
                        thread_id,
                        turn_id: None,
                        run_id,
                        client_request_id: None,
                        launch_id: Uuid::new_v4().to_string(),
                        process_group_id: None,
                        machine_name: "test".to_string(),
                        local_db_path: Some(db_path.clone()),
                        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir().unwrap(),
                    },
                    stdout_path: run_dir.join("stdout.log"),
                }
            };
            let sink = make_sink();
            std::fs::write(&sink.stdout_path, format!("{{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{{\"sessionId\":\"{provider_id}\"}}}}\n")).unwrap();
            sink.bind_session(&provider_id, false).await;
            let wake = receive_wake(&listener).await;
            assert_eq!(wake["phase"], "thinking");
            assert_eq!(wake["wake_reason"], "turn_started");
            let providers = crate::discovery::get_providers().into_iter()
                .filter(|provider| provider.name == "antigravity")
                .collect::<Vec<_>>();
            let discovered = crate::discovery::discover_all_files(&providers);
            let selected = crate::storage_v2_shipper::stable_source_path(&transcript);
            assert_eq!(discovered, vec![(selected.clone(), "antigravity")]);
            assert_eq!(crate::storage_v2_shipper::stable_source_path(Path::new(wake["path"].as_str().unwrap())), selected);
            assert_eq!(crate::discovery::session_path_for_watcher_event(&mirror, &providers), None);
            assert!(registry.list_nonterminal().unwrap().iter().any(|claim| claim.run_id == sink.run_id));
            assert_eq!(registry.read(&sink.run_id).unwrap().provider_thread_id.as_deref(), Some(provider_id.as_str()));
            finish_for_test(&sink, Verdict::Completed).await;
            let wake = receive_wake(&listener).await;
            assert_eq!(wake["phase"], "idle");
            assert_eq!(wake["wake_reason"], "turn_completed");
            assert_eq!(registry.read(&sink.run_id).unwrap().state, "terminal");
            let terminal = read_outbox_events(&sink.runtime_events_outbox_dir).into_iter()
                .find(|event| event["run_id"] == sink.run_id && event["kind"] == "terminal_signal").unwrap();
            assert_eq!(terminal["payload"]["terminal_state"], "run_completed");

            let legacy = make_sink();
            registry.mark_provider_binding(&legacy.run_id, &provider_id, Some(&mirror.to_string_lossy())).unwrap();
            finish_for_test(&legacy, Verdict::Completed).await;
            receive_wake(&listener).await;
            let terminal = read_outbox_events(&legacy.runtime_events_outbox_dir).into_iter()
                .find(|event| event["run_id"] == legacy.run_id && event["kind"] == "terminal_signal").unwrap();
            assert_eq!(terminal["payload"]["terminal_state"], "run_completed");
            // The console writes a claim; discovery reads a binding. The daemon
            // is what joins them, so the test projects the claims exactly as the
            // observation scan does before asserting what discovery sees.
            crate::managed_source_claim::project_claims(&db_path).expect("project claims");
            let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
            assert_eq!(
                crate::state::session_binding::SessionBinding::new(&conn)
                    .get_for_provider(&std::fs::canonicalize(&transcript).unwrap().to_string_lossy(), "antigravity").unwrap(),
                Some(session_id.clone()),
            );
            assert_eq!(
                crate::state::session_binding::SessionBinding::new(&conn)
                    .get_for_provider(&std::fs::canonicalize(&mirror).unwrap().to_string_lossy(), "antigravity").unwrap(),
                None,
            );
            assert_eq!(registry.read(&legacy.run_id).unwrap().source_path.as_deref(), Some(transcript.to_string_lossy().as_ref()));
            assert_eq!(std::fs::read(&transcript).unwrap(), native_bytes);
            assert_eq!(std::fs::read(&mirror).unwrap(), native_bytes);

            // A launch that terminates without a session fails rather than claiming
            // success, and its pending hold must not outlive the terminal claim.
            let missing = make_sink();
            let unrelated = conversation_transcript_path(&antigravity_brain_root().unwrap(), &Uuid::new_v4().to_string());
            assert_eq!(bind_discovered_source(&conn, &unrelated, &registry.list_all().unwrap(), &agent_dir).unwrap(), SourceOwnership::Pending);
            finish_for_test(&missing, Verdict::Failed("agy exited before the turn ended".to_string())).await;
            let terminal = read_outbox_events(&missing.runtime_events_outbox_dir).into_iter()
                .find(|event| event["run_id"] == missing.run_id && event["kind"] == "terminal_signal").unwrap();
            assert_eq!(terminal["payload"]["terminal_state"], "run_failed");
            assert_eq!(bind_discovered_source(&conn, &unrelated, &registry.list_all().unwrap(), &agent_dir).unwrap(), SourceOwnership::Unclaimed);
        })
            },
        );
    }

    #[test]
    fn antigravity_conflicting_claims_block_only_their_exact_native_source() {
        let dir = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(dir.path().join("claims"));
        let conflicted_id = Uuid::new_v4().to_string();
        let unrelated_id = Uuid::new_v4().to_string();
        let unrelated_owner = Uuid::new_v4().to_string();
        for (provider_id, session_id) in [
            (&conflicted_id, Uuid::new_v4().to_string()),
            (&conflicted_id, Uuid::new_v4().to_string()),
            (&unrelated_id, unrelated_owner.clone()),
        ] {
            let run_id = Uuid::new_v4().to_string();
            registry
                .claim(
                    &run_id,
                    &session_id,
                    &Uuid::new_v4().to_string(),
                    None,
                    None,
                    "antigravity",
                )
                .unwrap();
            registry
                .mark_provider_binding(&run_id, provider_id, None)
                .unwrap();
        }
        let conn = crate::state::db::open_db(Some(&dir.path().join("state.db"))).unwrap();
        let claims = registry.list_all().unwrap();
        let unrelated = conversation_transcript_path(&dir.path().join("brain"), &unrelated_id);
        assert_eq!(
            bind_discovered_source(&conn, &unrelated, &claims, dir.path()).unwrap(),
            SourceOwnership::Managed(unrelated_owner)
        );
        let conflicted = conversation_transcript_path(&dir.path().join("brain"), &conflicted_id);
        assert!(bind_discovered_source(&conn, &conflicted, &claims, dir.path()).is_err());
    }

    /// Settle a test sink the way `finish_turn` does for a recovered claim whose
    /// process is already gone.
    async fn finish_for_test(sink: &AntigravityAcpSink, verdict: Verdict) {
        let claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&sink.run_id)
            .unwrap();
        let mut stream = AcpStream::new(&sink.run_id);
        let mut process = MonitorProcess::Recovered(claim);
        finish_turn(
            &mut process,
            sink,
            &mut stream,
            verdict,
            None,
            &sink.stdout_path.with_file_name("stderr.log"),
            true,
        )
        .await;
    }

    fn read_outbox_events(outbox: &Path) -> Vec<Value> {
        let Ok(entries) = std::fs::read_dir(outbox) else {
            return Vec::new();
        };
        entries
            .filter_map(|entry| entry.ok())
            .filter_map(|entry| std::fs::read_to_string(entry.path()).ok())
            .filter_map(|raw| serde_json::from_str::<Value>(&raw).ok())
            .collect()
    }

    // ---- Protocol fold -------------------------------------------------

    fn frame(value: Value) -> Vec<u8> {
        serde_json::to_vec(&value).unwrap()
    }

    fn update(kind: &str, extra: Value) -> Vec<u8> {
        let mut update = json!({"sessionUpdate": kind});
        for (key, value) in extra.as_object().unwrap() {
            update[key] = value.clone();
        }
        frame(
            json!({"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"s","update":update}}),
        )
    }

    fn reply(id: i64, stop: &str, usage: bool) -> Vec<u8> {
        let mut result = json!({"stopReason": stop});
        if usage {
            result["usage"] = json!({"inputTokens": 1, "totalTokens": 2});
        }
        frame(json!({"jsonrpc":"2.0","id":id,"result":result}))
    }

    #[test]
    fn console_argv_is_acp_with_the_console_permission_flag_only() {
        assert_eq!(
            console_turn_argv(),
            vec![
                "acp".to_string(),
                "--dangerously-skip-permissions".to_string()
            ]
        );
    }

    #[test]
    fn a_turn_ends_when_every_issued_prompt_has_a_reply() {
        let mut stream = AcpStream::new("run");
        let issued = [FIRST_PROMPT_ID];
        assert_eq!(stream.verdict(&issued, false), Verdict::Pending);
        stream.apply_lines(&[reply(FIRST_PROMPT_ID, "end_turn", true)]);
        assert_eq!(stream.verdict(&issued, false), Verdict::Completed);
    }

    #[test]
    fn the_first_prompts_early_end_turn_does_not_end_a_steered_turn() {
        // The acp2 trace: the steer arrives, the first prompt's RPC returns at
        // once, the turn keeps running and the steer's reply ends it.
        let mut stream = AcpStream::new("run");
        let issued = [FIRST_PROMPT_ID, 1_700_000_000_000_000];
        stream.apply_lines(&[reply(FIRST_PROMPT_ID, "end_turn", true)]);
        assert_eq!(stream.verdict(&issued, false), Verdict::Pending);
        stream.apply_lines(&[
            update(
                "agent_message_chunk",
                json!({"content": {"type": "text", "text": "ACP_STEER_OK"}}),
            ),
            reply(1_700_000_000_000_000, "end_turn", true),
        ]);
        assert_eq!(stream.verdict(&issued, false), Verdict::Completed);
    }

    #[test]
    fn an_executor_error_chunk_with_a_usageless_end_turn_is_a_failed_turn() {
        // The acp4 trace: a 429 arrives as agent text and `end_turn` without usage.
        let mut stream = AcpStream::new("run");
        stream.apply_lines(&[
            update(
                "agent_message_chunk",
                json!({"content": {"type": "text", "text": "\n\u{26a0} agent executor error: generating and executing: Error 429"}}),
            ),
            reply(FIRST_PROMPT_ID, "end_turn", false),
        ]);
        match stream.verdict(&[FIRST_PROMPT_ID], false) {
            Verdict::Failed(message) => assert!(message.contains("Error 429"), "{message}"),
            other => panic!("expected a failed turn, got {other:?}"),
        }
    }

    #[test]
    fn a_reply_that_merely_quotes_the_error_marker_with_usage_still_completes() {
        let mut stream = AcpStream::new("run");
        stream.apply_lines(&[
            update(
                "agent_message_chunk",
                json!({"content": {"type": "text", "text": "\u{26a0} agent executor error was transient"}}),
            ),
            reply(FIRST_PROMPT_ID, "end_turn", true),
        ]);
        assert_eq!(
            stream.verdict(&[FIRST_PROMPT_ID], false),
            Verdict::Completed
        );
    }

    #[test]
    fn rpc_errors_cancel_requests_and_odd_stop_reasons_are_classified() {
        let mut stream = AcpStream::new("run");
        stream.apply_lines(&[frame(
            json!({"jsonrpc":"2.0","id":4,"error":{"code":-32000,"message":"boom"}}),
        )]);
        assert_eq!(
            stream.verdict(&[4], false),
            Verdict::Failed("boom".to_string())
        );
        assert_eq!(stream.verdict(&[4], true), Verdict::Cancelled);

        let mut stream = AcpStream::new("run");
        stream.apply_lines(&[reply(4, "cancelled", true)]);
        assert_eq!(stream.verdict(&[4], true), Verdict::Cancelled);
        // A provider-reported cancel is a cancel even before the claim shows it.
        assert_eq!(stream.verdict(&[4], false), Verdict::Cancelled);
        let mut stream = AcpStream::new("run");
        stream.apply_lines(&[reply(4, "max_tokens", true)]);
        assert_eq!(
            stream.verdict(&[4], false),
            Verdict::Failed("agy stopped the turn: max_tokens".to_string())
        );
        assert_eq!(stream.verdict(&[], false), Verdict::Pending);
    }

    #[test]
    fn message_chunks_accumulate_and_a_tool_call_closes_the_message() {
        let mut stream = AcpStream::new("run");
        let signals = stream.apply_lines(&[
            update("agent_message_chunk", json!({"content": {"type": "text", "text": "Hel"}})),
            update("agent_message_chunk", json!({"content": {"type": "text", "text": "lo"}})),
            update(
                "tool_call",
                json!({"toolCallId": "c1", "name": "run_command", "args": {"CommandLine": "echo hi"}, "status": "in_progress"}),
            ),
            update(
                "tool_call_update",
                json!({"toolCallId": "c1", "name": "run_command", "args": {"CommandLine": "echo hi"}, "status": "completed", "rawOutput": {"output": "hi\n"}}),
            ),
            update("agent_message_chunk", json!({"content": {"type": "text", "text": "Done"}})),
            reply(4, "end_turn", true),
        ]);
        let texts: Vec<_> = signals
            .iter()
            .filter_map(|signal| match signal {
                LiveSignal::Text {
                    item_id,
                    text,
                    completed,
                    ..
                } => Some((item_id.clone(), text.clone(), *completed)),
                _ => None,
            })
            .collect();
        // Only the newest text of a message is kept per read, and the tool call
        // starts a new message.
        assert_eq!(
            texts,
            vec![
                ("run:message:0".to_string(), "Hello".to_string(), true),
                ("run:message:1".to_string(), "Done".to_string(), true),
            ]
        );
        let tools: Vec<_> = signals
            .iter()
            .filter_map(|signal| match signal {
                LiveSignal::Tool {
                    command,
                    output,
                    completed,
                    ..
                } => Some((command.clone(), output.clone(), *completed)),
                _ => None,
            })
            .collect();
        assert_eq!(
            tools,
            vec![
                ("echo hi".to_string(), String::new(), false),
                ("echo hi".to_string(), "hi\n".to_string(), true),
            ]
        );
    }

    #[test]
    fn the_growth_reader_waits_for_a_whole_line_and_tolerates_a_missing_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("stdout.log");
        let mut stream = AcpStream::new("run");
        assert!(stream.poll(&path).unwrap().is_empty());
        let whole = update(
            "agent_message_chunk",
            json!({"content": {"type": "text", "text": "hi"}}),
        );
        let (head, tail) = whole.split_at(10);
        std::fs::write(&path, head).unwrap();
        assert!(stream.poll(&path).unwrap().is_empty());
        let mut file = OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(tail).unwrap();
        file.write_all(b"\n").unwrap();
        assert_eq!(stream.poll(&path).unwrap().len(), 1);
    }

    #[test]
    fn session_identity_comes_from_the_session_response_and_must_be_a_uuid() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("stdout.log");
        assert_eq!(read_session_identity(&path).unwrap(), None);
        std::fs::write(
            &path,
            b"{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{\"sessionId\":\"abc\"}}\n",
        )
        .unwrap();
        assert!(
            read_session_identity(&path).is_err(),
            "a prefix is not an identity"
        );
        let id = Uuid::new_v4().to_string();
        std::fs::write(
            &path,
            format!("{{\"jsonrpc\":\"2.0\",\"id\":2,\"result\":{{\"sessionId\":\"{id}\"}}}}\n"),
        )
        .unwrap();
        assert_eq!(read_session_identity(&path).unwrap(), Some(id));
    }

    // ---- A fake `agy acp` ------------------------------------------------

    /// A scripted ACP agent. The first prompt's text picks the scenario:
    /// `ok`, `child`, `hold:<name>`, `steer:<name>`, `cancel`, `stubborn`,
    /// `error`, `exit`. Every request it receives is appended to
    /// `requests.jsonl` in its working directory.
    #[cfg(unix)]
    fn write_fake_agy(root: &Path) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let bin = root.join("fake-agy");
        std::fs::write(
            &bin,
            r#"#!/usr/bin/env python3
import json, os, pathlib, queue, subprocess, sys, threading, time, uuid

cwd = pathlib.Path.cwd()
home = pathlib.Path.home()
incoming = queue.Queue()

def reader():
    for line in sys.stdin:
        line = line.strip()
        if line:
            incoming.put(json.loads(line))
    incoming.put(None)

threading.Thread(target=reader, daemon=True).start()

def out(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()

def log(message):
    with open(cwd / "requests.jsonl", "a") as handle:
        handle.write(json.dumps(message) + "\n")

def update(sid, body):
    out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": body}})

def transcript(sid, text):
    path = home / ".gemini" / "antigravity-cli" / "brain" / sid / ".system_generated" / "logs" / "transcript_full.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps({"step_index": path.stat().st_size if path.exists() else 0, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": "2026-10-12T00:00:00Z", "content": text}) + "\n")

def finish(mid, stop="end_turn", usage=True):
    result = {"stopReason": stop}
    if usage:
        result["usage"] = {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}
    out({"jsonrpc": "2.0", "id": mid, "result": result})

def chunk(sid, text):
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}})

def tool(sid, status="in_progress"):
    update(sid, {"sessionUpdate": "tool_call", "toolCallId": "call_1", "name": "run_command", "args": {"CommandLine": "sleep 1"}, "status": status})

def wait_for(name):
    while not (cwd / name).exists():
        time.sleep(0.01)

def prompt_text(message):
    return "".join(block.get("text", "") for block in message["params"]["prompt"] if block.get("type") == "text")

sid = None
while True:
    message = incoming.get()
    if message is None:
        break
    log(message)
    method, mid = message.get("method"), message.get("id")
    if method == "initialize":
        out({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"promptCapabilities": {"image": True}}}})
    elif method in ("session/new", "session/resume"):
        sid = message["params"].get("sessionId") or str(uuid.uuid4())
        transcript(sid, "seed")
        out({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}})
    elif method == "session/set_model":
        out({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/close":
        out({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/prompt":
        text = prompt_text(message)
        scenario, _, name = text.partition(":")
        if scenario == "child":
            child = subprocess.Popen(["sleep", "300"])
            (cwd / "child.pid").write_text(str(child.pid))
        if scenario in ("hold", "steer", "cancel", "stubborn"):
            tool(sid)
        if scenario == "hold":
            wait_for("release-" + name)
        elif scenario == "steer":
            follow = incoming.get()
            log(follow)
            finish(mid)  # the quirk: the first prompt returns at once
            wait_for("release-" + name)
            chunk(sid, "STEERED")
            transcript(sid, "STEERED")
            finish(follow["id"])
            continue
        elif scenario == "cancel":
            while True:
                follow = incoming.get()
                log(follow)
                if follow.get("method") == "session/cancel":
                    finish(mid, "cancelled")
                    break
            continue
        elif scenario == "stubborn":
            while True:
                follow = incoming.get()
                log(follow)
        elif scenario == "error":
            chunk(sid, "\n⚠ agent executor error: generating and executing: Error 429")
            finish(mid, usage=False)
            continue
        elif scenario == "exit":
            os._exit(3)
        chunk(sid, "DONE")
        transcript(sid, "DONE")
        finish(mid)
"#,
        )
        .unwrap();
        std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
        bin
    }

    #[cfg(unix)]
    fn acp_requests(root: &Path) -> Vec<Value> {
        std::fs::read_to_string(root.join("requests.jsonl"))
            .unwrap_or_default()
            .lines()
            .filter_map(|line| serde_json::from_str(line).ok())
            .collect()
    }

    #[cfg(unix)]
    fn requests_with_method(root: &Path, method: &str) -> Vec<Value> {
        acp_requests(root)
            .into_iter()
            .filter(|request| request["method"] == method)
            .collect()
    }

    #[cfg(unix)]
    async fn start_fake_turn(
        root: &Path,
        bin: &Path,
        prompt: &str,
        conversation_id: Option<String>,
        image_paths: Vec<PathBuf>,
        model: Option<&str>,
    ) -> Result<AntigravityAcpRunSummary> {
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let turn_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        let client_request_id = format!("acp-{run_id}");
        crate::turn_claims::default_registry()
            .unwrap()
            .claim(
                &run_id,
                &session_id,
                &thread_id,
                Some(&turn_id),
                Some(&client_request_id),
                "antigravity",
            )
            .unwrap();
        start_antigravity_acp_turn(AntigravityAcpRunConfig {
            session_id,
            thread_id,
            turn_id: Some(turn_id),
            run_id,
            client_request_id: Some(client_request_id),
            cwd: root.to_path_buf(),
            antigravity_bin: bin.to_string_lossy().into_owned(),
            prompt: prompt.to_string(),
            image_paths,
            model: model.map(str::to_string),
            conversation_id,
            machine_name: "antigravity-acp-test".to_string(),
            local_db_path: Some(root.join("state.db")),
        })
        .await
    }

    #[cfg(unix)]
    async fn wait_terminal(run_id: &str) -> crate::turn_claims::TurnClaim {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        loop {
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(run_id)
                .unwrap();
            if claim.state == "terminal" {
                return claim;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "run {run_id} did not settle"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    #[cfg(unix)]
    async fn wait_until(what: &str, mut done: impl FnMut() -> bool) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
        while !done() {
            assert!(
                tokio::time::Instant::now() < deadline,
                "timed out waiting for {what}"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    }

    #[cfg(unix)]
    async fn wait_group_exit(process_group_id: i32) {
        wait_until("the process group to exit", || {
            !crate::process_group::group_is_alive(process_group_id)
        })
        .await;
    }

    /// Run `body` with a scratch HOME and Longhouse home and a fake agent.
    #[cfg(unix)]
    fn with_fake_agy<F, Fut>(body: F)
    where
        F: FnOnce(PathBuf, PathBuf, PathBuf) -> Fut,
        Fut: std::future::Future<Output = ()>,
    {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::Builder::new()
            .prefix("agy-acp-")
            .tempdir_in("/tmp")
            .unwrap();
        let home = temp.path().join("home");
        let longhouse_home = temp.path().join("longhouse");
        std::fs::create_dir_all(&home).unwrap();
        let root = temp.path().join("work");
        std::fs::create_dir_all(&root).unwrap();
        let fake = write_fake_agy(temp.path());
        let db_path = temp.path().join("state.db");
        let _ = crate::state::db::open_db(Some(&db_path)).unwrap();
        temp_env::with_vars(
            [
                ("HOME", Some(home.as_os_str())),
                ("LONGHOUSE_HOME", Some(longhouse_home.as_os_str())),
            ],
            || runtime.block_on(body(root, db_path, fake)),
        );
    }

    #[cfg(unix)]
    #[test]
    fn a_new_console_turn_handshakes_completes_and_reaps_its_process_group() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(
                &root,
                &fake,
                "child",
                None,
                vec![],
                Some("gemini-4-hydrogen-low"),
            )
            .await
            .unwrap();
            let session = summary
                .provider_thread_id
                .clone()
                .expect("identity is known at launch");
            assert!(Uuid::parse_str(&session).is_ok());
            assert_eq!(
                summary.argv[1..],
                [
                    "acp".to_string(),
                    "--dangerously-skip-permissions".to_string()
                ]
            );
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(
                claim.result.as_ref().unwrap()["terminal_state"],
                "run_completed"
            );
            assert_eq!(claim.provider_thread_id.as_deref(), Some(session.as_str()));
            let methods: Vec<String> = acp_requests(&root)
                .iter()
                .filter_map(|request| request["method"].as_str().map(str::to_string))
                .collect();
            assert_eq!(
                methods,
                [
                    "initialize",
                    "session/new",
                    "session/set_model",
                    "session/prompt",
                    "session/close"
                ]
            );
            assert_eq!(
                requests_with_method(&root, "session/set_model")[0]["params"]["modelId"],
                "gemini-4-hydrogen-low"
            );
            wait_group_exit(summary.process_group_id).await;
            let child: i32 = std::fs::read_to_string(root.join("child.pid"))
                .unwrap()
                .parse()
                .unwrap();
            assert_eq!(
                unsafe { libc::kill(child, 0) },
                -1,
                "the tool child outlived the turn"
            );
            assert!(!fifo_of(Path::new(&summary.stdout_path)).exists());
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn resume_sends_the_full_conversation_id_and_refuses_a_prefix() {
        with_fake_agy(|root, _db, fake| async move {
            let id = Uuid::new_v4().to_string();
            let prefix = id[..8].to_string();
            let refused = start_fake_turn(&root, &fake, "ok", Some(prefix), vec![], None).await;
            assert!(refused.is_err(), "a short id must never reach agy");
            assert!(acp_requests(&root).is_empty());

            let summary = start_fake_turn(&root, &fake, "ok", Some(id.clone()), vec![], None)
                .await
                .unwrap();
            assert_eq!(summary.provider_thread_id.as_deref(), Some(id.as_str()));
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(
                claim.result.as_ref().unwrap()["terminal_state"],
                "run_completed"
            );
            let resumes = requests_with_method(&root, "session/resume");
            assert_eq!(resumes.len(), 1);
            assert_eq!(resumes[0]["params"]["sessionId"], id);
            assert!(requests_with_method(&root, "session/new").is_empty());
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn images_travel_as_native_image_prompt_parts() {
        with_fake_agy(|root, _db, fake| async move {
            let png = root.join("shot.png");
            std::fs::write(&png, b"\x89PNG fake").unwrap();
            let summary = start_fake_turn(&root, &fake, "ok", None, vec![png], None)
                .await
                .unwrap();
            wait_terminal(&summary.run_id).await;
            let prompt = &requests_with_method(&root, "session/prompt")[0]["params"]["prompt"];
            assert_eq!(prompt[0], json!({"type": "text", "text": "ok"}));
            assert_eq!(prompt[1]["type"], "image");
            assert_eq!(prompt[1]["mimeType"], "image/png");
            assert!(!prompt[1]["data"].as_str().unwrap().is_empty());
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn a_steer_with_an_image_is_a_second_prompt_and_the_early_end_turn_does_not_end_the_turn() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(&root, &fake, "steer:s1", None, vec![], None)
                .await
                .unwrap();
            wait_until("the first prompt", || {
                !requests_with_method(&root, "session/prompt").is_empty()
            })
            .await;
            let png = root.join("steer.png");
            std::fs::write(&png, b"\x89PNG steer").unwrap();
            steer_antigravity_acp_turn(
                &summary.run_id,
                &summary.session_id,
                "change course",
                &[png],
            )
            .await
            .unwrap();
            wait_until("the steer to arrive", || {
                requests_with_method(&root, "session/prompt").len() == 2
            })
            .await;
            let steer = &requests_with_method(&root, "session/prompt")[1]["params"]["prompt"];
            assert_eq!(steer[0]["text"], "change course");
            assert_eq!(steer[1]["type"], "image");
            // The first prompt has already answered end_turn; the turn is not over.
            tokio::time::sleep(Duration::from_millis(600)).await;
            assert_eq!(
                crate::turn_claims::default_registry()
                    .unwrap()
                    .read(&summary.run_id)
                    .unwrap()
                    .state,
                "spawned"
            );
            std::fs::write(root.join("release-s1"), b"go").unwrap();
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(
                claim.result.as_ref().unwrap()["terminal_state"],
                "run_completed"
            );
            wait_group_exit(summary.process_group_id).await;
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn a_steer_is_refused_once_the_turn_is_over_or_unknown() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(&root, &fake, "ok", None, vec![], None)
                .await
                .unwrap();
            wait_terminal(&summary.run_id).await;
            assert_eq!(
                steer_antigravity_acp_turn(&summary.run_id, &summary.session_id, "late", &[]).await,
                Err("turn_not_steerable".to_string())
            );
            assert_eq!(
                steer_antigravity_acp_turn(
                    &Uuid::new_v4().to_string(),
                    &summary.session_id,
                    "x",
                    &[]
                )
                .await,
                Err("turn_not_steerable".to_string())
            );
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn an_executor_error_chunk_settles_the_turn_as_failed() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(&root, &fake, "error", None, vec![], None)
                .await
                .unwrap();
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(claim.result.unwrap()["terminal_state"], "run_failed");
            let events =
                read_outbox_events(&crate::config::get_agent_runtime_events_outbox_dir().unwrap());
            let terminal = events
                .iter()
                .find(|event| {
                    event["run_id"] == summary.run_id && event["kind"] == "terminal_signal"
                })
                .expect("a terminal event");
            assert!(
                terminal["payload"]["stderr_tail"]
                    .as_str()
                    .unwrap()
                    .contains("Error 429"),
                "{terminal}"
            );
            wait_group_exit(summary.process_group_id).await;
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn a_provider_that_exits_mid_turn_fails_the_turn() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(&root, &fake, "exit", None, vec![], None)
                .await
                .unwrap();
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(claim.result.unwrap()["terminal_state"], "run_failed");
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn interrupt_sends_session_cancel_and_settles_as_cancelled() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(&root, &fake, "cancel", None, vec![], None)
                .await
                .unwrap();
            wait_until("the prompt", || {
                !requests_with_method(&root, "session/prompt").is_empty()
            })
            .await;
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&summary.run_id)
                .unwrap();
            interrupt_antigravity_acp_turn(
                &summary.run_id,
                &summary.session_id,
                &summary.thread_id,
                claim.turn_id.as_deref().unwrap(),
            )
            .await
            .unwrap();
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
            assert_eq!(requests_with_method(&root, "session/cancel").len(), 1);
            wait_group_exit(summary.process_group_id).await;
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn a_cancel_agy_ignores_is_enforced_by_killing_the_group_after_the_grace() {
        with_fake_agy(|root, _db, fake| async move {
            let summary = start_fake_turn(&root, &fake, "stubborn", None, vec![], None)
                .await
                .unwrap();
            wait_until("the prompt", || {
                !requests_with_method(&root, "session/prompt").is_empty()
            })
            .await;
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&summary.run_id)
                .unwrap();
            let started = std::time::Instant::now();
            interrupt_antigravity_acp_turn(
                &summary.run_id,
                &summary.session_id,
                &summary.thread_id,
                claim.turn_id.as_deref().unwrap(),
            )
            .await
            .unwrap();
            let claim = wait_terminal(&summary.run_id).await;
            assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
            assert!(
                started.elapsed() >= CANCEL_GRACE,
                "killed before the grace elapsed"
            );
            wait_group_exit(summary.process_group_id).await;
            crate::console_lifecycle::stop_test_monitor(&summary.launch_id).await;
        });
    }

    #[cfg(unix)]
    #[test]
    fn console_restart_contract() {
        with_fake_agy(|root, db_path, fake| async move {
            // A turn whose monitor dies with the engine is re-adopted and completes.
            let first = start_fake_turn(&root, &fake, "hold:live", None, vec![], None)
                .await
                .unwrap();
            wait_until("the first prompt", || {
                !requests_with_method(&root, "session/prompt").is_empty()
            })
            .await;
            crate::console_lifecycle::stop_test_monitor(&first.launch_id).await;
            assert!(crate::process_group::group_is_alive(first.process_group_id));
            assert_eq!(
                recover_antigravity_acp_turns("antigravity-acp-test", Some(db_path.clone()))
                    .await
                    .unwrap(),
                1
            );
            assert!(crate::process_group::group_is_alive(first.process_group_id));
            std::fs::write(root.join("release-live"), b"go").unwrap();
            let completed = wait_terminal(&first.run_id).await;
            assert_eq!(completed.result.unwrap()["terminal_state"], "run_completed");
            wait_group_exit(first.process_group_id).await;
            crate::console_lifecycle::stop_test_monitor(&first.launch_id).await;
            let events =
                read_outbox_events(&crate::config::get_agent_runtime_events_outbox_dir().unwrap());
            let binding_keys = events
                .iter()
                .filter(|event| {
                    event["run_id"] == first.run_id && event["kind"] == "binding_signal"
                })
                .filter_map(|event| event["dedupe_key"].as_str())
                .collect::<std::collections::HashSet<_>>();
            assert_eq!(
                binding_keys.len(),
                1,
                "recovery must preserve one logical binding"
            );
            assert_eq!(
                events
                    .iter()
                    .filter(|event| event["run_id"] == first.run_id
                        && event["kind"] == "terminal_signal")
                    .count(),
                1
            );

            // A recovered turn can still be stopped, and the stop is enforced.
            let stop = start_fake_turn(&root, &fake, "stubborn", None, vec![], None)
                .await
                .unwrap();
            wait_until("the stop prompt", || {
                requests_with_method(&root, "session/prompt").len() == 2
            })
            .await;
            crate::console_lifecycle::stop_test_monitor(&stop.launch_id).await;
            assert_eq!(
                recover_antigravity_acp_turns("antigravity-acp-test", Some(db_path.clone()))
                    .await
                    .unwrap(),
                1
            );
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&stop.run_id)
                .unwrap();
            interrupt_antigravity_acp_turn(
                &stop.run_id,
                &stop.session_id,
                &stop.thread_id,
                claim.turn_id.as_deref().unwrap(),
            )
            .await
            .unwrap();
            let stopped = wait_terminal(&stop.run_id).await;
            assert_eq!(stopped.result.unwrap()["terminal_state"], "run_cancelled");
            wait_group_exit(stop.process_group_id).await;
            crate::console_lifecycle::stop_test_monitor(&stop.launch_id).await;

            // A reused pid with a different start time is not adopted or signalled.
            let mismatch = start_fake_turn(&root, &fake, "stubborn", None, vec![], None)
                .await
                .unwrap();
            wait_until("the mismatch prompt", || {
                requests_with_method(&root, "session/prompt").len() == 3
            })
            .await;
            crate::console_lifecycle::stop_test_monitor(&mismatch.launch_id).await;
            let claims = crate::turn_claims::default_registry().unwrap();
            let claim = claims.read(&mismatch.run_id).unwrap();
            claims
                .mark_spawned_invocation(
                    &claim.run_id,
                    claim.pid.unwrap(),
                    claim.process_group_id.unwrap(),
                    Some("reused-pid-with-a-different-start-time".to_string()),
                    ANTIGRAVITY_ACP_ADAPTER,
                    &mismatch.launch_id,
                    claim.provider_thread_id.as_deref(),
                    claim.stdout_path.as_deref().unwrap(),
                    claim.stderr_path.as_deref().unwrap(),
                    claim.result.clone().unwrap(),
                )
                .unwrap();
            assert_eq!(
                recover_antigravity_acp_turns("antigravity-acp-test", Some(db_path.clone()))
                    .await
                    .unwrap(),
                0
            );
            assert!(crate::process_group::group_is_alive(
                mismatch.process_group_id
            ));
            assert_eq!(claims.read(&mismatch.run_id).unwrap().state, "spawned");
            assert!(crate::process_group::shutdown_group(
                mismatch.process_group_id,
                crate::process_group::DEFAULT_GRACE,
            )
            .await
            .is_gone());
        });
    }

    #[test]
    fn live_events_use_the_codex_live_text_envelope_under_the_antigravity_source() {
        let sink = AntigravityAcpSink {
            run: ConsoleRun {
                provider: &ANTIGRAVITY_CONSOLE,
                session_id: Uuid::new_v4().to_string(),
                thread_id: Uuid::new_v4().to_string(),
                turn_id: Some("turn-1".to_string()),
                run_id: "run".to_string(),
                client_request_id: Some("req-1".to_string()),
                launch_id: Uuid::new_v4().to_string(),
                process_group_id: None,
                machine_name: "test".to_string(),
                local_db_path: None,
                runtime_events_outbox_dir: PathBuf::from("/nonexistent"),
            },
            stdout_path: PathBuf::from("/nonexistent"),
        };
        let text = sink.live_event(&LiveSignal::Text {
            item_id: "run:message:1".to_string(),
            seq: 7,
            delta: "b".to_string(),
            text: "ab".to_string(),
            completed: false,
        });
        assert_eq!(text["provider"], "antigravity");
        assert_eq!(text["source"], ANTIGRAVITY_ACP_ADAPTER);
        assert_eq!(text["kind"], "progress_signal");
        assert!(text["dedupe_key"].as_str().unwrap().ends_with(":live:7"));
        assert_eq!(
            text["payload"]["progress_kind"],
            "bridge_live_transcript_delta"
        );
        assert_eq!(text["payload"]["live_text"], "ab");
        assert_eq!(text["payload"]["item_id"], "run:message:1");
        assert_eq!(text["payload"]["turn_id"], "turn-1");
        assert_eq!(text["payload"]["seq"], 7);
        let tool = sink.live_event(&LiveSignal::Tool {
            item_id: "run:step:2".to_string(),
            seq: 8,
            command: "ls".to_string(),
            output: "out".to_string(),
            completed: true,
        });
        assert_eq!(tool["payload"]["progress_kind"], "console_live_tool_item");
        assert_eq!(tool["payload"]["status"], "completed");
        assert_eq!(tool["payload"]["output"], "out");
    }

    #[test]
    fn failed_terminal_handoff_keeps_status_until_durable() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                let agent_dir = crate::config::get_agent_dir().unwrap();
                let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
                std::fs::create_dir_all(outbox.parent().unwrap()).unwrap();
                std::fs::write(&outbox, b"outbox path is a file").unwrap();
                let registry = crate::turn_claims::default_registry().unwrap();
                registry
                    .claim(&run_id, &session_id, &thread_id, None, None, "antigravity")
                    .unwrap();
                let sink = AntigravityAcpSink {
                    run: ConsoleRun {
                        provider: &ANTIGRAVITY_CONSOLE,
                        session_id: session_id.clone(),
                        thread_id,
                        turn_id: None,
                        run_id: run_id.clone(),
                        client_request_id: None,
                        launch_id: Uuid::new_v4().to_string(),
                        process_group_id: None,
                        machine_name: "test".to_string(),
                        local_db_path: None,
                        runtime_events_outbox_dir: outbox.clone(),
                    },
                    stdout_path: temp.path().join("stdout.jsonl"),
                };

                sink.post_phase("thinking", None).await;
                sink.post_terminal("run_completed", Some(0), None).await;
                let status_dir = crate::status_slot::status_slot_dir(&agent_dir);
                assert!(crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id && slot.run_id == run_id));
                assert_eq!(registry.read(&run_id).unwrap().state, "terminal");
                assert!(crate::outbox::collect_runtime_event_outbox(&outbox).is_empty());

                std::fs::remove_file(&outbox).unwrap();
                sink.post_terminal("run_completed", Some(0), None).await;
                assert_eq!(
                    crate::outbox::collect_runtime_event_outbox(&outbox).len(),
                    1
                );
                assert!(!crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id));
            });
        });
    }

    // ---- Live receipt against a real agy ---------------------------------

    /// One-pixel PNG, enough for agy to forward as an image part.
    #[cfg(unix)]
    const LIVE_PNG: &[u8] = &[
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f,
        0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0xf8,
        0xff, 0xff, 0x3f, 0x00, 0x05, 0xfe, 0x02, 0xfe, 0xdc, 0xcc, 0x59, 0xe7, 0x00, 0x00, 0x00,
        0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ];

    #[cfg(unix)]
    fn live_stdout_has(summary: &AntigravityAcpRunSummary, needle: &str) -> bool {
        std::fs::read_to_string(&summary.stdout_path)
            .map(|raw| raw.contains(needle))
            .unwrap_or(false)
    }

    #[cfg(unix)]
    async fn live_terminal(run_id: &str, budget: Duration) -> crate::turn_claims::TurnClaim {
        let deadline = tokio::time::Instant::now() + budget;
        loop {
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(run_id)
                .unwrap();
            if claim.state == "terminal" {
                return claim;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "live run {run_id} did not settle"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    /// Receipt driver, run by `scripts/qa/agy-acp-live-receipt.sh` on a bench
    /// host: the real `agy` (LONGHOUSE_ANTIGRAVITY_BIN) behind the credential-free
    /// scripted Gemini server, in a scratch HOME. It drives the adapter's own
    /// entry points: a new turn, resume by full id, a steer with an image, and
    /// an interrupt, and writes what it saw to `LIVE_RECEIPT_FILE`.
    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    #[ignore = "needs a real agy and the scripted model endpoint; run scripts/qa/agy-acp-live-receipt.sh on a bench host"]
    async fn live_agy_console_receipt() {
        let _guard = crate::console_adapter::longhouse_home_test_guard();
        let bin = std::env::var("LONGHOUSE_ANTIGRAVITY_BIN").expect("LONGHOUSE_ANTIGRAVITY_BIN");
        let receipt_path =
            PathBuf::from(std::env::var("LIVE_RECEIPT_FILE").expect("LIVE_RECEIPT_FILE"));
        let cwd = PathBuf::from(std::env::var("LIVE_CWD").expect("LIVE_CWD"));
        std::fs::create_dir_all(&cwd).unwrap();
        let db_path = cwd.join("state.db");
        let _ = crate::state::db::open_db(Some(&db_path)).unwrap();
        let png = cwd.join("steer.png");
        std::fs::write(&png, LIVE_PNG).unwrap();
        let budget = Duration::from_secs(180);
        let mut receipt = json!({"agy_bin": bin});
        let save = |receipt: &Value| {
            std::fs::write(&receipt_path, serde_json::to_vec_pretty(receipt).unwrap()).unwrap()
        };
        let start = |prompt: &str, resume: Option<(String, String)>, images: Vec<PathBuf>| {
            let bin = bin.clone();
            let cwd = cwd.clone();
            let db_path = db_path.clone();
            let prompt = prompt.to_string();
            async move {
                let (session_id, conversation_id) = match resume {
                    Some((lh, native)) => (lh, Some(native)),
                    None => (Uuid::new_v4().to_string(), None),
                };
                let thread_id = Uuid::new_v4().to_string();
                let turn_id = Uuid::new_v4().to_string();
                let run_id = Uuid::new_v4().to_string();
                crate::turn_claims::default_registry()
                    .unwrap()
                    .claim(
                        &run_id,
                        &session_id,
                        &thread_id,
                        Some(&turn_id),
                        None,
                        "antigravity",
                    )
                    .unwrap();
                start_antigravity_acp_turn(AntigravityAcpRunConfig {
                    session_id,
                    thread_id,
                    turn_id: Some(turn_id),
                    run_id,
                    client_request_id: None,
                    cwd,
                    antigravity_bin: bin,
                    prompt,
                    image_paths: images,
                    model: None,
                    conversation_id,
                    machine_name: "agy-live-receipt".to_string(),
                    local_db_path: Some(db_path.clone()),
                })
                .await
                .expect("the live turn starts")
            }
        };
        let terminal_state = |claim: &crate::turn_claims::TurnClaim| {
            claim
                .result
                .as_ref()
                .map(|r| r["terminal_state"].clone())
                .unwrap_or(Value::Null)
        };
        let transcript_text = |session: &str| {
            let path = conversation_transcript_path(&antigravity_brain_root().unwrap(), session);
            std::fs::read_to_string(path).unwrap_or_default()
        };
        let mut groups: Vec<i32> = Vec::new();

        // 1. A new Console turn.
        let began = std::time::Instant::now();
        let first = start("LIVE_PING please answer", None, vec![]).await;
        groups.push(first.process_group_id);
        let claim = live_terminal(&first.run_id, budget).await;
        let session = first.provider_thread_id.clone().unwrap();
        receipt["new_turn"] = json!({
            "terminal_state": terminal_state(&claim),
            "session_id": session,
            "elapsed_ms": began.elapsed().as_millis() as u64,
            "transcript_has_reply": transcript_text(&session).contains("LIVE_PONG"),
            "argv": first.argv,
        });
        save(&receipt);

        // 2. Resume the same conversation by its full id.
        let resumed = start(
            "LIVE_RESUME please answer",
            Some((first.session_id.clone(), session.clone())),
            vec![],
        )
        .await;
        groups.push(resumed.process_group_id);
        let claim = live_terminal(&resumed.run_id, budget).await;
        let transcript = transcript_text(&session);
        receipt["resume"] = json!({
            "terminal_state": terminal_state(&claim),
            "same_session": resumed.provider_thread_id.as_deref() == Some(session.as_str()),
            "transcript_has_both_turns": transcript.contains("LIVE_PONG") && transcript.contains("RESUMED_OK"),
        });
        save(&receipt);

        // 3. Steer a running turn with an image.
        let steered = start("LIVE_STEER_START go", None, vec![]).await;
        groups.push(steered.process_group_id);
        let until = tokio::time::Instant::now() + budget;
        while !live_stdout_has(&steered, "\"tool_call\"") {
            assert!(
                tokio::time::Instant::now() < until,
                "the steer target never ran a tool"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let steer_result = steer_antigravity_acp_turn(
            &steered.run_id,
            &steered.session_id,
            "LIVE_STEER_NOW change course",
            &[png.clone()],
        )
        .await;
        let claim = live_terminal(&steered.run_id, budget).await;
        let stdout = std::fs::read_to_string(&steered.stdout_path).unwrap_or_default();
        let steer_session = steered.provider_thread_id.clone().unwrap();
        receipt["steer_with_image"] = json!({
            "steer_accepted": steer_result.is_ok(),
            "terminal_state": terminal_state(&claim),
            "end_turn_replies": stdout.matches("\"stopReason\":\"end_turn\"").count(),
            "transcript_has_steer_reply": transcript_text(&steer_session).contains("STEER_ACK"),
        });
        save(&receipt);

        // 4. Interrupt a running turn.
        let cancelled = start("LIVE_CANCEL_START go", None, vec![]).await;
        groups.push(cancelled.process_group_id);
        let until = tokio::time::Instant::now() + budget;
        while !live_stdout_has(&cancelled, "\"tool_call\"") {
            assert!(
                tokio::time::Instant::now() < until,
                "the cancel target never ran a tool"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        let claim = crate::turn_claims::default_registry()
            .unwrap()
            .read(&cancelled.run_id)
            .unwrap();
        let asked = std::time::Instant::now();
        interrupt_antigravity_acp_turn(
            &cancelled.run_id,
            &cancelled.session_id,
            &cancelled.thread_id,
            claim.turn_id.as_deref().unwrap(),
        )
        .await
        .expect("interrupt is accepted");
        let claim = live_terminal(&cancelled.run_id, budget).await;
        let elapsed = asked.elapsed();
        let stdout = std::fs::read_to_string(&cancelled.stdout_path).unwrap_or_default();
        receipt["interrupt"] = json!({
            "terminal_state": terminal_state(&claim),
            "elapsed_ms": elapsed.as_millis() as u64,
            "grace_ms": CANCEL_GRACE.as_millis() as u64,
            "provider_resolved_the_cancel": stdout.contains("\"stopReason\":\"cancelled\""),
        });
        save(&receipt);

        // 5. Nothing left behind.
        tokio::time::sleep(Duration::from_millis(500)).await;
        let alive: Vec<i32> = groups
            .iter()
            .copied()
            .filter(|group| crate::process_group::group_is_alive(*group))
            .collect();
        receipt["groups_still_alive"] = json!(alive);
        save(&receipt);
        assert!(
            alive.is_empty(),
            "process groups outlived their turns: {alive:?}"
        );
        for key in ["new_turn", "resume", "steer_with_image", "interrupt"] {
            let expected = if key == "interrupt" {
                "run_cancelled"
            } else {
                "run_completed"
            };
            assert_eq!(receipt[key]["terminal_state"], expected, "{key}: {receipt}");
        }
    }
}
