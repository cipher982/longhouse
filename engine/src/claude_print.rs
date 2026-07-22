//! Claude Console turns through stock `claude --print`.
//!
//! Turn-scoped adapter: one `claude --print --output-format stream-json`
//! invocation per Console turn. The first turn mints the provider session
//! UUID via `--session-id`; later turns resume it natively via `--resume`.
//! Unlike Cursor, Claude's native resume restores full model context, so no
//! synthetic continuation prompt is needed.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{json, Value};
use tokio::process::{Child, Command};
use uuid::Uuid;

pub const CLAUDE_PRINT_ADAPTER: &str = "claude_print";
pub const DEFAULT_CLAUDE_BIN: &str = "claude";
const STDERR_TAIL_LINES: usize = 40;

#[derive(Clone, Debug)]
pub struct ClaudePrintRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub claude_bin: String,
    pub prompt: String,
    pub resume_provider_thread_id: Option<String>,
    pub model: Option<String>,
    pub permission_mode: String,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct ClaudePrintRunSummary {
    pub session_id: String,
    pub thread_id: String,
    pub run_id: String,
    pub provider_thread_id: String,
    pub launch_id: String,
    pub pid: u32,
    pub process_group_id: i32,
    pub stdout_path: String,
    pub stderr_path: String,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct ClaudePrintSink {
    session_id: String,
    thread_id: String,
    turn_id: Option<String>,
    run_id: String,
    client_request_id: Option<String>,
    provider_thread_id: String,
    launch_id: String,
    process_group_id: Option<i32>,
    machine_name: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

pub async fn start_claude_print_turn(
    config: ClaudePrintRunConfig,
) -> Result<ClaudePrintRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = normalized_optional(&config.turn_id) {
        validate_uuid(&turn_id, "turn_id")?;
    }
    if config.permission_mode != "bypass" {
        anyhow::bail!("Claude Console currently supports bypass permission mode only");
    }
    let (provider_thread_id, is_resume) =
        match normalized_optional(&config.resume_provider_thread_id) {
            Some(value) => {
                validate_uuid(&value, "resume_provider_thread_id")?;
                (value, true)
            }
            None => (Uuid::new_v4().to_string(), false),
        };
    let launch_id = Uuid::new_v4().to_string();
    require_claude_lifecycle_hook()?;
    let lock = acquire_conversation_lock(&claude_managed_root()?, &provider_thread_id)?;

    let run_dir = crate::config::get_agent_dir()?
        .join("claude-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    let stdout_path = run_dir.join("stdout.jsonl");
    let stderr_path = run_dir.join("stderr.log");
    let stdout_file = private_output_file(&stdout_path)?;
    let stderr_file = private_output_file(&stderr_path)?;

    // `--verbose` is required by the Claude CLI when combining `--print`
    // with `--output-format stream-json`.
    let (args, recorded_args) = build_claude_args(
        &provider_thread_id,
        is_resume,
        config.model.as_deref(),
        &config.prompt,
    );
    let argv = std::iter::once(config.claude_bin.clone())
        .chain(recorded_args)
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.claude_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .env("LONGHOUSE_RUN_ID", &config.run_id)
        .env_remove("LONGHOUSE_SESSION_ID")
        .env_remove("LONGHOUSE_CHANNEL_SESSION_ID")
        .env_remove("LONGHOUSE_PROVIDER_SESSION_ID")
        .env_remove("LONGHOUSE_CHANNEL_CWD")
        .env_remove("LONGHOUSE_PERMISSION_HOOK_ENABLED")
        .env_remove("LONGHOUSE_HOOK_URL")
        .env_remove("LONGHOUSE_HOOK_TOKEN")
        .env_remove("LONGHOUSE_LAUNCH_ACTOR")
        .env_remove("LONGHOUSE_LAUNCH_SURFACE");
    #[cfg(unix)]
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Err(error).with_context(|| format!("spawning `{}` --print", config.claude_bin))
        }
    };
    let pid = child.id().context("claude --print returned no pid")?;
    let process_group_id = i32::try_from(pid).context("Claude pid exceeds process-group range")?;
    let sink = ClaudePrintSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        provider_thread_id: provider_thread_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        machine_name: config.machine_name.clone(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
    };
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "claude",
        "transport": CLAUDE_PRINT_ADAPTER,
        "provider_thread_id": provider_thread_id,
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
        CLAUDE_PRINT_ADAPTER,
        &launch_id,
        Some(&provider_thread_id),
        &stdout_path.to_string_lossy(),
        &stderr_path.to_string_lossy(),
        result,
    ) {
        cleanup_process_group(Some(process_group_id)).await;
        let _ = child.kill().await;
        return Err(error).context("persisting Claude Console spawn identity");
    }
    let monitor_path = stdout_path.clone();
    let monitor_stderr = stderr_path.clone();
    tokio::spawn(async move {
        monitor_claude_print(&mut child, &monitor_path, &monitor_stderr, sink, lock).await;
    });

    Ok(ClaudePrintRunSummary {
        session_id: config.session_id,
        thread_id: config.thread_id,
        run_id: config.run_id,
        provider_thread_id,
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        argv,
    })
}

pub async fn recover_claude_print_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        if claim.adapter.as_deref() != Some(CLAUDE_PRINT_ADAPTER) || claim.state != "spawned" {
            continue;
        }
        let Some(stdout_path) = claim.stdout_path.as_deref().map(PathBuf::from) else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("Claude Console claim has no stdout path".to_string()),
            );
            continue;
        };
        let stderr_path = claim
            .stderr_path
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| stdout_path.with_file_name("stderr.log"));
        let provider_thread_id = claim.provider_thread_id.clone().unwrap_or_default();
        if provider_thread_id.is_empty() {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("Claude Console claim has no provider identity".to_string()),
            );
            continue;
        }
        let sink = ClaudePrintSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            provider_thread_id: provider_thread_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            machine_name: machine_name.to_string(),
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        if claim_process_is_live(&claim) {
            let lock = acquire_conversation_lock(&claude_managed_root()?, &provider_thread_id)?;
            tokio::spawn(async move {
                monitor_recovered_claim(claim, stdout_path, stderr_path, sink, lock).await;
            });
            recovered += 1;
        } else {
            settle_recovered_dead_claim(&claim, &stdout_path, &stderr_path, &sink).await;
        }
    }
    Ok(recovered)
}

pub fn interrupt_claude_print_turn(run_id: &str, session_id: &str) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id || claim.provider != "claude" {
        anyhow::bail!("Claude Console turn claim does not match the requested session");
    }
    if claim.adapter.as_deref() != Some(CLAUDE_PRINT_ADAPTER) || claim.state != "spawned" {
        anyhow::bail!("Claude Console turn is not active");
    }
    let pid = claim
        .pid
        .context("Claude Console turn has no provider pid")?;
    let expected_start = claim
        .process_start_time
        .as_deref()
        .context("Claude Console turn has no process-start identity")?;
    let actual = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .cloned()
        .context("Claude Console provider process is gone")?;
    if actual.lstart != expected_start {
        anyhow::bail!("Claude Console provider pid identity changed");
    }
    let pgid = claim
        .process_group_id
        .context("Claude Console turn has no process-group identity")?;
    registry.mark_cancel_requested(run_id)?;
    let result = unsafe { libc::killpg(pgid, libc::SIGINT) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting Claude Console process group");
        }
    }
    Ok(())
}

async fn monitor_claude_print(
    child: &mut Child,
    stdout_path: &Path,
    stderr_path: &Path,
    sink: ClaudePrintSink,
    _lock: File,
) {
    sink.post_phase("thinking", None).await;
    let mut offset = 0_u64;
    let mut pending = Vec::new();
    let mut seq = 0_u64;
    let mut terminal_from_stream = None;
    let mut identity_confirmed = false;
    loop {
        match read_growth(stdout_path, &mut offset, &mut pending) {
            Ok(lines) => {
                let had_lines = !lines.is_empty();
                for bytes in lines {
                    seq += 1;
                    match serde_json::from_slice::<Value>(&bytes) {
                        Ok(event) => {
                            if let Err(error) =
                                validate_stream_identity(&event, &sink.provider_thread_id)
                            {
                                cleanup_process_group(sink.process_group_id).await;
                                sink.post_terminal("run_failed", None, Some(error.to_string()))
                                    .await;
                                return;
                            }
                            if stream_session_identity(&event).is_some() {
                                identity_confirmed = true;
                            }
                            if let Some(terminal) = terminal_result_from_event(&event) {
                                terminal_from_stream = Some(terminal);
                            }
                            sink.post_stream_event(seq, event).await;
                        }
                        Err(error) => sink.post_decode_gap(seq, &error.to_string(), &bytes).await,
                    }
                }
                if had_lines {
                    persist_projection_checkpoint(&sink.run_id, offset, pending.len(), seq);
                }
            }
            Err(error) => {
                sink.post_terminal("run_failed", None, Some(error.to_string()))
                    .await;
                return;
            }
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                tokio::time::sleep(Duration::from_millis(150)).await;
                if let Ok(lines) = read_growth(stdout_path, &mut offset, &mut pending) {
                    let had_lines = !lines.is_empty();
                    for bytes in lines {
                        seq += 1;
                        match serde_json::from_slice::<Value>(&bytes) {
                            Ok(event) => {
                                if let Err(error) =
                                    validate_stream_identity(&event, &sink.provider_thread_id)
                                {
                                    cleanup_process_group(sink.process_group_id).await;
                                    sink.post_terminal(
                                        "run_failed",
                                        status.code(),
                                        Some(error.to_string()),
                                    )
                                    .await;
                                    return;
                                }
                                if stream_session_identity(&event).is_some() {
                                    identity_confirmed = true;
                                }
                                if let Some(terminal) = terminal_result_from_event(&event) {
                                    terminal_from_stream = Some(terminal);
                                }
                                sink.post_stream_event(seq, event).await;
                            }
                            Err(error) => {
                                sink.post_decode_gap(seq, &error.to_string(), &bytes).await
                            }
                        }
                    }
                    if had_lines {
                        persist_projection_checkpoint(&sink.run_id, offset, pending.len(), seq);
                    }
                }
                let claim = crate::turn_claims::default_registry()
                    .and_then(|registry| registry.read(&sink.run_id))
                    .ok();
                let cancel_requested = claim
                    .as_ref()
                    .and_then(|item| item.cancel_requested_at.as_ref())
                    .is_some();
                let terminal = settle_terminal_state(
                    cancel_requested,
                    status.success(),
                    identity_confirmed,
                    terminal_from_stream,
                );
                if terminal != "run_completed" {
                    cleanup_process_group(sink.process_group_id).await;
                }
                sink.post_terminal(&terminal, status.code(), stderr_tail(stderr_path))
                    .await;
                return;
            }
            Ok(None) => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(error) => {
                sink.post_terminal("run_failed", None, Some(error.to_string()))
                    .await;
                return;
            }
        }
    }
}

async fn monitor_recovered_claim(
    claim: crate::turn_claims::TurnClaim,
    stdout_path: PathBuf,
    stderr_path: PathBuf,
    sink: ClaudePrintSink,
    _lock: File,
) {
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let mut terminal_from_stream = None;
    let mut identity_confirmed = claim.provider_identity_confirmed;
    loop {
        if let Ok(lines) = read_growth(&stdout_path, &mut offset, &mut pending) {
            let had_lines = !lines.is_empty();
            for bytes in lines {
                seq += 1;
                match serde_json::from_slice::<Value>(&bytes) {
                    Ok(event) => {
                        if let Err(error) =
                            validate_stream_identity(&event, &sink.provider_thread_id)
                        {
                            cleanup_process_group(sink.process_group_id).await;
                            sink.post_terminal("run_failed", None, Some(error.to_string()))
                                .await;
                            return;
                        }
                        if stream_session_identity(&event).is_some() {
                            identity_confirmed = true;
                        }
                        if let Some(terminal) = terminal_result_from_event(&event) {
                            terminal_from_stream = Some(terminal);
                        }
                        sink.post_stream_event(seq, event).await;
                    }
                    Err(error) => sink.post_decode_gap(seq, &error.to_string(), &bytes).await,
                }
            }
            if had_lines {
                persist_projection_checkpoint(&sink.run_id, offset, pending.len(), seq);
            }
        }
        if !claim_process_is_live(&claim) {
            let cancel_requested = crate::turn_claims::default_registry()
                .and_then(|registry| registry.read(&claim.run_id))
                .ok()
                .and_then(|current| current.cancel_requested_at)
                .is_some();
            let terminal = settle_recovered_terminal_state(
                cancel_requested,
                identity_confirmed,
                terminal_from_stream,
            );
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal(&terminal, None, stderr_tail(&stderr_path))
                .await;
            return;
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

async fn settle_recovered_dead_claim(
    claim: &crate::turn_claims::TurnClaim,
    stdout_path: &Path,
    stderr_path: &Path,
    sink: &ClaudePrintSink,
) {
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let mut terminal = None;
    let mut identity_confirmed = claim.provider_identity_confirmed;
    if let Ok(lines) = read_growth(stdout_path, &mut offset, &mut pending) {
        let had_lines = !lines.is_empty();
        for bytes in lines {
            seq += 1;
            match serde_json::from_slice::<Value>(&bytes) {
                Ok(event) => {
                    if let Err(error) = validate_stream_identity(&event, &sink.provider_thread_id) {
                        cleanup_process_group(sink.process_group_id).await;
                        sink.post_terminal("run_failed", None, Some(error.to_string()))
                            .await;
                        return;
                    }
                    if stream_session_identity(&event).is_some() {
                        identity_confirmed = true;
                    }
                    terminal = terminal_result_from_event(&event).or(terminal);
                    sink.post_stream_event(seq, event).await;
                }
                Err(error) => sink.post_decode_gap(seq, &error.to_string(), &bytes).await,
            }
        }
        if had_lines {
            persist_projection_checkpoint(&sink.run_id, offset, pending.len(), seq);
        }
    }
    let terminal = settle_recovered_terminal_state(
        claim.cancel_requested_at.is_some(),
        identity_confirmed,
        terminal,
    );
    cleanup_process_group(sink.process_group_id).await;
    sink.post_terminal(&terminal, None, stderr_tail(stderr_path))
        .await;
}

fn persist_projection_checkpoint(run_id: &str, read_offset: u64, pending_len: usize, seq: u64) {
    let complete_offset = read_offset.saturating_sub(pending_len as u64);
    if let Ok(registry) = crate::turn_claims::default_registry() {
        let _ = registry.mark_projection_checkpoint(run_id, complete_offset, seq);
    }
}

async fn cleanup_process_group(process_group_id: Option<i32>) {
    let Some(pgid) = process_group_id else {
        return;
    };
    if unsafe { libc::killpg(pgid, 0) } != 0 {
        return;
    }
    unsafe { libc::killpg(pgid, libc::SIGTERM) };
    tokio::time::sleep(Duration::from_millis(200)).await;
    if unsafe { libc::killpg(pgid, 0) } == 0 {
        unsafe { libc::killpg(pgid, libc::SIGKILL) };
    }
}

fn claim_process_is_live(claim: &crate::turn_claims::TurnClaim) -> bool {
    claim
        .pid
        .zip(claim.process_start_time.as_deref())
        .and_then(|(pid, expected)| {
            crate::process_identity::collect_process_facts_by_pid()
                .get(&pid)
                .map(|fact| fact.lstart == expected)
        })
        .unwrap_or(false)
}

fn read_growth(path: &Path, offset: &mut u64, pending: &mut Vec<u8>) -> Result<Vec<Vec<u8>>> {
    let mut file = File::open(path)?;
    file.seek(SeekFrom::Start(*offset))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    *offset += bytes.len() as u64;
    pending.extend(bytes);
    let mut lines = Vec::new();
    while let Some(index) = pending.iter().position(|byte| *byte == b'\n') {
        let mut line = pending.drain(..=index).collect::<Vec<_>>();
        line.pop();
        if !line.is_empty() {
            lines.push(line);
        }
    }
    Ok(lines)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProviderTerminalResult {
    Success,
    Error,
}

fn terminal_result_from_event(event: &Value) -> Option<ProviderTerminalResult> {
    (event.get("type").and_then(Value::as_str) == Some("result")).then(|| {
        if event.get("subtype").and_then(Value::as_str) == Some("success")
            && event.get("is_error").and_then(Value::as_bool) != Some(true)
        {
            ProviderTerminalResult::Success
        } else {
            ProviderTerminalResult::Error
        }
    })
}

fn stream_session_identity(event: &Value) -> Option<&str> {
    (event.get("type").and_then(Value::as_str) == Some("system")
        && event.get("subtype").and_then(Value::as_str) == Some("init"))
    .then(|| event.get("session_id").and_then(Value::as_str))
    .flatten()
}

fn validate_stream_identity(event: &Value, expected: &str) -> Result<()> {
    if let Some(observed) = stream_session_identity(event) {
        if observed != expected {
            anyhow::bail!(
                "Claude stream session identity {observed} does not match requested {expected}"
            );
        }
    }
    Ok(())
}

fn settle_terminal_state(
    cancel_requested: bool,
    exit_success: bool,
    identity_confirmed: bool,
    provider_result: Option<ProviderTerminalResult>,
) -> String {
    if cancel_requested {
        return "run_cancelled".to_string();
    }
    if exit_success
        && identity_confirmed
        && provider_result == Some(ProviderTerminalResult::Success)
    {
        "run_completed".to_string()
    } else {
        "run_failed".to_string()
    }
}

fn settle_recovered_terminal_state(
    cancel_requested: bool,
    identity_confirmed: bool,
    provider_result: Option<ProviderTerminalResult>,
) -> String {
    if cancel_requested {
        "run_cancelled".to_string()
    } else if identity_confirmed && provider_result == Some(ProviderTerminalResult::Success) {
        "run_completed".to_string()
    } else {
        "run_failed".to_string()
    }
}

impl ClaudePrintSink {
    async fn post_binding(&self) {
        self.post_events(vec![json!({
            "runtime_key": format!("claude:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "claude",
            "device_id": self.machine_name,
            "source": CLAUDE_PRINT_ADAPTER,
            "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("claude-print:{}:{}:binding", self.session_id, self.launch_id),
            "payload": {
                "provider_session_id": self.provider_thread_id,
                "managed_transport": CLAUDE_PRINT_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("claude:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "claude",
            "device_id": self.machine_name,
            "source": CLAUDE_PRINT_ADAPTER,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("claude-print:{}:{}:phase:{phase}", self.session_id, self.run_id),
            "payload": {"managed_transport": CLAUDE_PRINT_ADAPTER, "execution_lifetime": "one_shot"}
        })])
        .await;
    }

    async fn post_stream_event(&self, seq: u64, event: Value) {
        if event.get("type").and_then(Value::as_str) == Some("system")
            && event.get("subtype").and_then(Value::as_str) == Some("init")
        {
            let observed = event
                .get("session_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if observed == self.provider_thread_id {
                if let Ok(registry) = crate::turn_claims::default_registry() {
                    let _ = registry.mark_provider_binding(&self.run_id, observed, None);
                }
                self.post_binding().await;
            }
        }
        if let Some((phase, tool_name)) = claude_phase_from_event(&event) {
            self.post_phase(phase, tool_name).await;
        }
        self.post_events(vec![json!({
            "runtime_key": format!("claude:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "claude",
            "device_id": self.machine_name,
            "source": CLAUDE_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("claude-print:{}:{}:stdout:{seq}", self.session_id, self.run_id),
            "payload": {
                "progress_kind": "claude_print_stream",
                "seq": seq,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": self.provider_thread_id,
                "event": event,
                "managed_transport": CLAUDE_PRINT_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_decode_gap(&self, seq: u64, error: &str, raw_line: &[u8]) {
        self.post_events(vec![json!({
            "runtime_key": format!("claude:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "claude",
            "device_id": self.machine_name,
            "source": CLAUDE_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("claude-print:{}:{}:decode-gap:{seq}", self.session_id, self.run_id),
            "payload": {
                "progress_kind": "claude_print_decode_gap",
                "seq": seq,
                "error": error,
                "raw_line": String::from_utf8_lossy(raw_line)
            }
        })]).await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr: Option<String>,
    ) {
        self.persist_local_phase("finished", None, Utc::now());
        self.post_events(vec![json!({
            "runtime_key": format!("claude:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "claude",
            "device_id": self.machine_name,
            "source": CLAUDE_PRINT_ADAPTER,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("claude-print:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": CLAUDE_PRINT_ADAPTER,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CLAUDE_PRINT_ADAPTER,
                "exit_code": exit_code,
                "stderr_tail": stderr,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": self.provider_thread_id
            }
        })])
        .await;
        crate::turn_claims::mark_terminal(
            &self.run_id,
            terminal_state,
            (terminal_state == "run_failed")
                .then(|| stderr.clone())
                .flatten(),
        );
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
        let Ok(conn) = crate::state::db::open_db(Some(db_path)) else {
            return;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "claude".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: CLAUDE_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
    }

    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            if let Err(error) =
                crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event)
            {
                eprintln!("[claude-print] runtime outbox write failed: {error}");
            }
        }
    }
}

/// Map a Claude stream-json event to a runtime phase.
///
/// Assistant messages carrying `tool_use` blocks mean the provider is
/// executing a tool; a `user` event in print mode is a tool result coming
/// back, after which the model is thinking again.
fn claude_phase_from_event(event: &Value) -> Option<(&'static str, Option<String>)> {
    match event.get("type").and_then(Value::as_str) {
        Some("assistant") => {
            let tool = event
                .get("message")
                .and_then(|message| message.get("content"))
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .find(|block| block.get("type").and_then(Value::as_str) == Some("tool_use"))
                .and_then(|block| block.get("name").and_then(Value::as_str))
                .map(str::to_string);
            match tool {
                Some(name) => Some(("running", Some(name))),
                None => None,
            }
        }
        Some("user") => Some(("thinking", None)),
        _ => None,
    }
}

fn claude_managed_root() -> Result<PathBuf> {
    Ok(crate::config::get_longhouse_home()?
        .join("managed-local")
        .join("claude-print"))
}

fn claude_provider_home() -> Result<PathBuf> {
    if let Some(value) = std::env::var_os("CLAUDE_CONFIG_DIR") {
        return Ok(PathBuf::from(value));
    }
    Ok(PathBuf::from(std::env::var("HOME").context("HOME not set")?).join(".claude"))
}

fn require_claude_lifecycle_hook() -> Result<()> {
    require_claude_lifecycle_hook_at(&claude_provider_home()?)
}

fn require_claude_lifecycle_hook_at(provider_home: &Path) -> Result<()> {
    let hook_path = provider_home.join("hooks").join("longhouse-hook.sh");
    let settings_path = provider_home.join("settings.json");
    if !hook_path.is_file() {
        anyhow::bail!(
            "Claude Console requires the Longhouse lifecycle hook at {}; run `longhouse machine repair`",
            hook_path.display()
        );
    }
    let settings: Value = serde_json::from_slice(
        &std::fs::read(&settings_path)
            .with_context(|| format!("reading Claude settings {}", settings_path.display()))?,
    )
    .with_context(|| format!("parsing Claude settings {}", settings_path.display()))?;
    let session_start = settings
        .get("hooks")
        .and_then(|value| value.get("SessionStart"))
        .and_then(Value::as_array);
    let registered = session_start.is_some_and(|entries| {
        entries.iter().any(|entry| {
            entry
                .get("hooks")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|hook| hook.get("command").and_then(Value::as_str))
                .any(|command| command.contains("longhouse-hook.sh"))
        })
    });
    if !registered {
        anyhow::bail!(
            "Claude Console requires the Longhouse SessionStart hook in {}; run `longhouse machine repair`",
            settings_path.display()
        );
    }
    Ok(())
}

fn build_claude_args(
    provider_thread_id: &str,
    is_resume: bool,
    model: Option<&str>,
    prompt: &str,
) -> (Vec<String>, Vec<String>) {
    let mut args = vec![
        "--print".to_string(),
        "--output-format".to_string(),
        "stream-json".to_string(),
        "--verbose".to_string(),
        "--dangerously-skip-permissions".to_string(),
    ];
    args.extend([
        if is_resume {
            "--resume"
        } else {
            "--session-id"
        }
        .to_string(),
        provider_thread_id.to_string(),
    ]);
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--model".to_string(), model.to_string()]);
    }
    let mut recorded = args.clone();
    args.push(prompt.to_string());
    recorded.push("[prompt omitted]".to_string());
    (args, recorded)
}

fn acquire_conversation_lock(root: &Path, provider_thread_id: &str) -> Result<File> {
    let locks = root.join("conversation-locks");
    std::fs::create_dir_all(&locks)?;
    set_private_dir(&locks)?;
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .mode(0o600)
        .open(locks.join(format!("{provider_thread_id}.lock")))?;
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result != 0 {
        anyhow::bail!("Claude conversation {provider_thread_id} already has an execution owner");
    }
    Ok(file)
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

fn stderr_tail(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut lines = text
        .lines()
        .rev()
        .take(STDERR_TAIL_LINES)
        .collect::<VecDeque<_>>();
    lines.make_contiguous().reverse();
    let value = lines.into_iter().collect::<Vec<_>>().join("\n");
    (!value.is_empty()).then_some(value)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_and_resume_argv_are_exact_and_claims_redact_prompt() {
        let provider_id = Uuid::new_v4().to_string();
        let (fresh, fresh_recorded) = build_claude_args(
            &provider_id,
            false,
            Some("claude-sonnet-4-5"),
            "secret prompt",
        );
        assert_eq!(
            fresh,
            vec![
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--session-id",
                &provider_id,
                "--model",
                "claude-sonnet-4-5",
                "secret prompt",
            ]
        );
        assert_eq!(
            fresh_recorded.last().map(String::as_str),
            Some("[prompt omitted]")
        );
        assert!(!fresh_recorded.iter().any(|value| value == "secret prompt"));

        let (resume, _) = build_claude_args(&provider_id, true, None, "next");
        assert_eq!(resume[5], "--resume");
        assert_eq!(resume[6], provider_id);
    }

    #[test]
    fn terminal_result_requires_success_record_matching_identity_and_zero_exit() {
        let success = json!({"type": "result", "subtype": "success", "is_error": false});
        assert_eq!(
            terminal_result_from_event(&success),
            Some(ProviderTerminalResult::Success)
        );
        let error = json!({"type": "result", "subtype": "error_during_execution"});
        assert_eq!(
            terminal_result_from_event(&error),
            Some(ProviderTerminalResult::Error)
        );
        let flagged = json!({"type": "result", "subtype": "success", "is_error": true});
        assert_eq!(
            terminal_result_from_event(&flagged),
            Some(ProviderTerminalResult::Error)
        );
        assert_eq!(
            settle_terminal_state(false, true, true, Some(ProviderTerminalResult::Success)),
            "run_completed"
        );
        for (exit_success, identity_confirmed, result) in [
            (true, false, Some(ProviderTerminalResult::Success)),
            (true, true, None),
            (true, true, Some(ProviderTerminalResult::Error)),
            (false, true, Some(ProviderTerminalResult::Success)),
        ] {
            assert_eq!(
                settle_terminal_state(false, exit_success, identity_confirmed, result),
                "run_failed"
            );
        }
        assert_eq!(
            settle_terminal_state(true, false, false, None),
            "run_cancelled"
        );
    }

    #[test]
    fn stream_init_identity_must_match_requested_provider_thread() {
        let expected = Uuid::new_v4().to_string();
        assert!(validate_stream_identity(
            &json!({"type": "system", "subtype": "init", "session_id": expected}),
            &expected,
        )
        .is_ok());
        assert!(validate_stream_identity(
            &json!({"type": "system", "subtype": "init", "session_id": Uuid::new_v4().to_string()}),
            &expected,
        )
        .is_err());
    }

    #[test]
    fn lifecycle_hook_preflight_fails_closed() {
        let temp = tempfile::tempdir().unwrap();
        assert!(require_claude_lifecycle_hook_at(temp.path()).is_err());

        let hook_dir = temp.path().join("hooks");
        std::fs::create_dir_all(&hook_dir).unwrap();
        std::fs::write(hook_dir.join("longhouse-hook.sh"), "#!/bin/sh\n").unwrap();
        std::fs::write(temp.path().join("settings.json"), "{}").unwrap();
        assert!(require_claude_lifecycle_hook_at(temp.path()).is_err());

        std::fs::write(
            temp.path().join("settings.json"),
            serde_json::to_vec(&json!({
                "hooks": {"SessionStart": [{"hooks": [{"command": hook_dir.join("longhouse-hook.sh")}]}]}
            }))
            .unwrap(),
        )
        .unwrap();
        assert!(require_claude_lifecycle_hook_at(temp.path()).is_ok());
    }

    #[tokio::test]
    #[ignore = "requires an authenticated stock claude and spends provider tokens"]
    async fn installed_claude_completes_and_resumes_through_production_console_adapter() {
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let claude_bin = std::env::var("LONGHOUSE_CLAUDE_BIN")
            .unwrap_or_else(|_| DEFAULT_CLAUDE_BIN.to_string());
        let marker = format!("LH_CLAUDE_CONSOLE_{}", Uuid::new_v4().simple());
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();

        async fn run_turn(
            claude_bin: &str,
            cwd: &Path,
            session_id: &str,
            thread_id: &str,
            prompt: String,
            resume: Option<String>,
        ) -> ClaudePrintRunSummary {
            let turn_id = Uuid::new_v4().to_string();
            let run_id = Uuid::new_v4().to_string();
            let client_request_id = format!("canary-{run_id}");
            assert!(matches!(
                crate::turn_claims::default_registry()
                    .unwrap()
                    .claim(
                        &run_id,
                        session_id,
                        thread_id,
                        Some(&turn_id),
                        Some(&client_request_id),
                        "claude",
                    )
                    .unwrap(),
                crate::turn_claims::ClaimOutcome::Acquired
            ));
            let summary = start_claude_print_turn(ClaudePrintRunConfig {
                session_id: session_id.to_string(),
                thread_id: thread_id.to_string(),
                turn_id: Some(turn_id),
                run_id,
                client_request_id: Some(client_request_id),
                cwd: cwd.to_path_buf(),
                claude_bin: claude_bin.to_string(),
                prompt,
                resume_provider_thread_id: resume,
                model: None,
                permission_mode: "bypass".to_string(),
                machine_name: "claude-console-canary".to_string(),
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
                    assert!(claim.provider_identity_confirmed);
                    assert_eq!(
                        claim.result.as_ref().unwrap()["terminal_state"],
                        "run_completed",
                        "stdout={}\nstderr={}",
                        std::fs::read_to_string(&summary.stdout_path).unwrap_or_default(),
                        std::fs::read_to_string(&summary.stderr_path).unwrap_or_default(),
                    );
                    return summary;
                }
                assert!(
                    tokio::time::Instant::now() < deadline,
                    "Claude Console canary timed out"
                );
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }

        let first = run_turn(
            &claude_bin,
            temp.path(),
            &session_id,
            &thread_id,
            format!("Remember {marker}. Reply with exactly {marker} and nothing else. Do not use tools."),
            None,
        )
        .await;
        assert!(std::fs::read_to_string(&first.stdout_path)
            .unwrap()
            .contains(&marker));

        let second = run_turn(
            &claude_bin,
            temp.path(),
            &session_id,
            &thread_id,
            "Reply with exactly the marker from the previous turn and nothing else. Do not use tools."
                .to_string(),
            Some(first.provider_thread_id.clone()),
        )
        .await;
        assert_eq!(second.provider_thread_id, first.provider_thread_id);
        assert!(std::fs::read_to_string(&second.stdout_path)
            .unwrap()
            .contains(&marker));

        match previous_home {
            Some(value) => unsafe { std::env::set_var("LONGHOUSE_HOME", value) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }
    }

    #[test]
    fn phase_mapping_reads_tool_use_and_tool_results() {
        let tool_call = json!({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "name": "Bash", "input": {}}
            ]}
        });
        assert_eq!(
            claude_phase_from_event(&tool_call),
            Some(("running", Some("Bash".to_string())))
        );
        let text_only = json!({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "answer"}]}
        });
        assert_eq!(claude_phase_from_event(&text_only), None);
        let tool_result = json!({"type": "user", "message": {"content": []}});
        assert_eq!(
            claude_phase_from_event(&tool_result),
            Some(("thinking", None))
        );
        let system = json!({"type": "system", "subtype": "init"});
        assert_eq!(claude_phase_from_event(&system), None);
    }
}
