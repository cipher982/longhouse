//! Cursor Console turns through stock `cursor-agent --print`.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
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

pub const CURSOR_PRINT_ADAPTER: &str = "cursor_print";
const STDERR_TAIL_LINES: usize = 40;

#[derive(Clone, Debug)]
pub struct CursorPrintRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub cursor_bin: String,
    pub prompt: String,
    pub resume_provider_thread_id: Option<String>,
    pub model: Option<String>,
    pub permission_mode: String,
    pub api_url: String,
    pub api_token: Option<String>,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct CursorPrintRunSummary {
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
struct CursorPrintSink {
    session_id: String,
    thread_id: String,
    turn_id: Option<String>,
    run_id: String,
    client_request_id: Option<String>,
    provider_thread_id: String,
    launch_id: String,
    process_group_id: Option<i32>,
    machine_name: String,
    cwd: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

pub async fn start_cursor_print_turn(
    config: CursorPrintRunConfig,
) -> Result<CursorPrintRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = normalized_optional(&config.turn_id) {
        validate_uuid(&turn_id, "turn_id")?;
    }
    let provider_thread_id = match normalized_optional(&config.resume_provider_thread_id) {
        Some(value) => {
            validate_uuid(&value, "resume_provider_thread_id")?;
            value
        }
        None => create_chat(&config.cursor_bin, &config.cwd).await?,
    };
    let launch_id = Uuid::new_v4().to_string();
    let state_root = cursor_managed_root()?;
    let lock = acquire_conversation_lock(&state_root, &provider_thread_id)?;
    reserve_binding(
        &state_root,
        &config.session_id,
        &config.thread_id,
        config.turn_id.as_deref(),
        &config.run_id,
        config.client_request_id.as_deref(),
        &provider_thread_id,
        &launch_id,
    )?;

    let run_dir = crate::config::get_agent_dir()?
        .join("cursor-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    let stdout_path = run_dir.join("stdout.jsonl");
    let stderr_path = run_dir.join("stderr.log");
    let stdout_file = private_output_file(&stdout_path)?;
    let stderr_file = private_output_file(&stderr_path)?;

    let mut args = vec![
        "--print".to_string(),
        "--output-format".to_string(),
        "stream-json".to_string(),
        "--trust".to_string(),
        "--workspace".to_string(),
        config.cwd.to_string_lossy().to_string(),
        "--resume".to_string(),
        provider_thread_id.clone(),
    ];
    if let Some(model) = normalized_optional(&config.model) {
        args.extend(["--model".to_string(), model]);
    }
    if config.permission_mode == "bypass" {
        args.push("--force".to_string());
    }
    args.push(config.prompt.clone());
    let argv = std::iter::once(config.cursor_bin.clone())
        .chain(args.iter().cloned())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.cursor_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .env_remove("LONGHOUSE_SESSION_ID")
        .env_remove("LONGHOUSE_CURSOR_LAUNCH_ID")
        .env_remove("LONGHOUSE_CURSOR_REGISTRATION_READY")
        .env_remove("LONGHOUSE_CURSOR_PRINT_MODE")
        .env_remove("LONGHOUSE_LAUNCH_ACTOR")
        .env_remove("LONGHOUSE_LAUNCH_SURFACE");
    if config.permission_mode == "remote_approve" {
        let token = normalized_optional(&config.api_token)
            .context("Cursor Console remote approval requires a Machine Agent token")?;
        command
            .env("LONGHOUSE_PERMISSION_HOOK_ENABLED", "1")
            .env("LONGHOUSE_HOOK_URL", config.api_url.trim_end_matches('/'))
            .env("LONGHOUSE_HOOK_TOKEN", token)
            .env("LONGHOUSE_PERMISSION_HOOK_TIMEOUT_S", "20");
    } else {
        command.env_remove("LONGHOUSE_PERMISSION_HOOK_ENABLED");
        command.env_remove("LONGHOUSE_HOOK_URL");
        command.env_remove("LONGHOUSE_HOOK_TOKEN");
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
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            rollback_binding(&state_root, &config.session_id, &launch_id);
            return Err(error).with_context(|| format!("spawning `{}` --print", config.cursor_bin));
        }
    };
    let pid = child.id().context("cursor-agent --print returned no pid")?;
    let process_group_id = i32::try_from(pid).context("Cursor pid exceeds process-group range")?;
    let sink = CursorPrintSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        provider_thread_id: provider_thread_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        machine_name: config.machine_name.clone(),
        cwd: config.cwd.to_string_lossy().to_string(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
    };
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "cursor",
        "transport": CURSOR_PRINT_ADAPTER,
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
    crate::turn_claims::default_registry()?.mark_spawned_invocation(
        &config.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        CURSOR_PRINT_ADAPTER,
        &launch_id,
        Some(&provider_thread_id),
        &stdout_path.to_string_lossy(),
        &stderr_path.to_string_lossy(),
        result,
    )?;
    let monitor_path = stdout_path.clone();
    let monitor_stderr = stderr_path.clone();
    tokio::spawn(async move {
        monitor_cursor_print(&mut child, &monitor_path, &monitor_stderr, sink, lock).await;
    });

    Ok(CursorPrintRunSummary {
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

pub async fn recover_cursor_print_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        if claim.adapter.as_deref() != Some(CURSOR_PRINT_ADAPTER) || claim.state != "spawned" {
            continue;
        }
        let Some(stdout_path) = claim.stdout_path.as_deref().map(PathBuf::from) else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("Cursor Console claim has no stdout path".to_string()),
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
                Some("Cursor Console claim has no provider identity".to_string()),
            );
            continue;
        }
        let result = claim.result.as_ref().and_then(Value::as_object);
        let cwd = result
            .and_then(|value| value.get("cwd"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let sink = CursorPrintSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            provider_thread_id: provider_thread_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            machine_name: machine_name.to_string(),
            cwd,
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        if claim_process_is_live(&claim) {
            let lock = acquire_conversation_lock(&cursor_managed_root()?, &provider_thread_id)?;
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

pub fn interrupt_cursor_print_turn(run_id: &str, session_id: &str) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id || claim.provider != "cursor" {
        anyhow::bail!("Cursor Console turn claim does not match the requested session");
    }
    if claim.adapter.as_deref() != Some(CURSOR_PRINT_ADAPTER) || claim.state != "spawned" {
        anyhow::bail!("Cursor Console turn is not active");
    }
    let pid = claim
        .pid
        .context("Cursor Console turn has no provider pid")?;
    let expected_start = claim
        .process_start_time
        .as_deref()
        .context("Cursor Console turn has no process-start identity")?;
    let actual = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .cloned()
        .context("Cursor Console provider process is gone")?;
    if actual.lstart != expected_start {
        anyhow::bail!("Cursor Console provider pid identity changed");
    }
    let pgid = claim
        .process_group_id
        .context("Cursor Console turn has no process-group identity")?;
    registry.mark_cancel_requested(run_id)?;
    let result = unsafe { libc::killpg(pgid, libc::SIGINT) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting Cursor Console process group");
        }
    }
    Ok(())
}

async fn create_chat(binary: &str, cwd: &Path) -> Result<String> {
    let output = Command::new(binary)
        .arg("create-chat")
        .current_dir(cwd)
        .output()
        .await
        .with_context(|| format!("running `{binary} create-chat`"))?;
    if !output.status.success() {
        anyhow::bail!(
            "cursor-agent create-chat failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let value = String::from_utf8(output.stdout)?.trim().to_string();
    validate_uuid(&value, "cursor-agent create-chat result")?;
    Ok(value)
}

async fn monitor_cursor_print(
    child: &mut Child,
    stdout_path: &Path,
    stderr_path: &Path,
    sink: CursorPrintSink,
    _lock: File,
) {
    sink.post_phase("thinking", None).await;
    let mut offset = 0_u64;
    let mut pending = Vec::new();
    let mut seq = 0_u64;
    let mut terminal_from_stream: Option<String> = None;
    loop {
        match read_growth(stdout_path, &mut offset, &mut pending) {
            Ok(lines) => {
                let had_lines = !lines.is_empty();
                for bytes in lines {
                    seq += 1;
                    match serde_json::from_slice::<Value>(&bytes) {
                        Ok(event) => {
                            if let Some(terminal) = terminal_state_from_event(&event) {
                                terminal_from_stream = Some(terminal);
                            }
                            sink.post_stream_event(seq, event).await;
                        }
                        Err(error) => sink.post_decode_gap(seq, &error.to_string()).await,
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
                        if let Ok(event) = serde_json::from_slice::<Value>(&bytes) {
                            if let Some(terminal) = terminal_state_from_event(&event) {
                                terminal_from_stream = Some(terminal);
                            }
                            sink.post_stream_event(seq, event).await;
                        }
                    }
                    if had_lines {
                        persist_projection_checkpoint(&sink.run_id, offset, pending.len(), seq);
                    }
                }
                let claim = crate::turn_claims::default_registry()
                    .and_then(|registry| registry.read(&sink.run_id))
                    .ok();
                let terminal = terminal_from_stream.unwrap_or_else(|| {
                    if claim
                        .as_ref()
                        .and_then(|item| item.cancel_requested_at.as_ref())
                        .is_some()
                    {
                        "run_cancelled".to_string()
                    } else if status.success() {
                        "run_completed".to_string()
                    } else {
                        "run_failed".to_string()
                    }
                });
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
    sink: CursorPrintSink,
    _lock: File,
) {
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let mut terminal_from_stream = None;
    loop {
        if let Ok(lines) = read_growth(&stdout_path, &mut offset, &mut pending) {
            let had_lines = !lines.is_empty();
            for bytes in lines {
                seq += 1;
                if let Ok(event) = serde_json::from_slice::<Value>(&bytes) {
                    if let Some(terminal) = terminal_state_from_event(&event) {
                        terminal_from_stream = Some(terminal);
                    }
                    sink.post_stream_event(seq, event).await;
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
            let terminal = terminal_from_stream.unwrap_or_else(|| {
                if cancel_requested {
                    "run_cancelled".to_string()
                } else {
                    "run_failed".to_string()
                }
            });
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
    sink: &CursorPrintSink,
) {
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let mut terminal = None;
    if let Ok(lines) = read_growth(stdout_path, &mut offset, &mut pending) {
        let had_lines = !lines.is_empty();
        for bytes in lines {
            seq += 1;
            if let Ok(event) = serde_json::from_slice::<Value>(&bytes) {
                terminal = terminal_state_from_event(&event).or(terminal);
                sink.post_stream_event(seq, event).await;
            }
        }
        if had_lines {
            persist_projection_checkpoint(&sink.run_id, offset, pending.len(), seq);
        }
    }
    let terminal = terminal.unwrap_or_else(|| {
        if claim.cancel_requested_at.is_some() {
            "run_cancelled".to_string()
        } else {
            "run_failed".to_string()
        }
    });
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

fn terminal_state_from_event(event: &Value) -> Option<String> {
    (event.get("type").and_then(Value::as_str) == Some("result")).then(|| {
        if event.get("subtype").and_then(Value::as_str) == Some("success")
            && event.get("is_error").and_then(Value::as_bool) != Some(true)
        {
            "run_completed".to_string()
        } else {
            "run_failed".to_string()
        }
    })
}

impl CursorPrintSink {
    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_PRINT_ADAPTER,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("cursor-print:{}:{}:phase:{phase}", self.session_id, self.run_id),
            "payload": {"managed_transport": CURSOR_PRINT_ADAPTER, "execution_lifetime": "one_shot"}
        })])
        .await;
    }

    async fn post_stream_event(&self, seq: u64, event: Value) {
        if event.get("type").and_then(Value::as_str) == Some("system") {
            let observed = event
                .get("session_id")
                .and_then(Value::as_str)
                .unwrap_or_default();
            if observed == self.provider_thread_id {
                if let Ok(root) = cursor_managed_root() {
                    let _ = promote_binding(
                        &root,
                        &self.session_id,
                        &self.thread_id,
                        self.turn_id.as_deref(),
                        &self.run_id,
                        self.client_request_id.as_deref(),
                        observed,
                        &self.launch_id,
                    );
                }
                if let Ok(registry) = crate::turn_claims::default_registry() {
                    let _ = registry.mark_provider_binding(&self.run_id, observed, None);
                }
            }
        }
        let phase = match (
            event.get("type").and_then(Value::as_str),
            event.get("subtype").and_then(Value::as_str),
        ) {
            (Some("tool_call"), Some("started")) => Some("running"),
            (Some("tool_call"), Some("completed")) => Some("thinking"),
            _ => None,
        };
        if let Some(phase) = phase {
            self.post_phase(phase, cursor_tool_name(&event)).await;
        }
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("cursor-print:{}:{}:stdout:{seq}", self.session_id, self.run_id),
            "payload": {
                "progress_kind": "cursor_print_stream",
                "seq": seq,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": self.provider_thread_id,
                "event": event,
                "managed_transport": CURSOR_PRINT_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_decode_gap(&self, seq: u64, error: &str) {
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("cursor-print:{}:{}:decode-gap:{seq}", self.session_id, self.run_id),
            "payload": {"progress_kind": "cursor_print_decode_gap", "seq": seq, "error": error}
        })]).await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr: Option<String>,
    ) {
        crate::turn_claims::mark_terminal(
            &self.run_id,
            terminal_state,
            (terminal_state == "run_failed")
                .then(|| stderr.clone())
                .flatten(),
        );
        self.persist_local_phase("finished", None, Utc::now());
        self.post_events(vec![json!({
            "runtime_key": format!("cursor:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "cursor",
            "device_id": self.machine_name,
            "source": CURSOR_PRINT_ADAPTER,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("cursor-print:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": CURSOR_PRINT_ADAPTER,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": CURSOR_PRINT_ADAPTER,
                "exit_code": exit_code,
                "stderr_tail": stderr,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": self.provider_thread_id
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
        let Ok(conn) = crate::state::db::open_db(Some(db_path)) else {
            return;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "cursor".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: CURSOR_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
        let managed = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "cursor".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name,
            phase_source: CURSOR_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        let _ = crate::state::managed_session_state::ManagedSessionStateStore::new(&conn)
            .record_phase(&managed);
    }

    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            if let Err(error) =
                crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event)
            {
                eprintln!("[cursor-print] runtime outbox write failed: {error}");
            }
        }
    }
}

fn cursor_tool_name(event: &Value) -> Option<String> {
    let call = event.get("tool_call")?;
    for key in [
        "shellToolCall",
        "mcpToolCall",
        "readToolCall",
        "writeToolCall",
    ] {
        if call.get(key).is_some() {
            return Some(key.trim_end_matches("ToolCall").to_string());
        }
    }
    None
}

fn cursor_managed_root() -> Result<PathBuf> {
    Ok(crate::config::get_longhouse_home()?
        .join("managed-local")
        .join("cursor-helm"))
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
        anyhow::bail!("Cursor conversation {provider_thread_id} already has an execution owner");
    }
    Ok(file)
}

#[allow(clippy::too_many_arguments)]
fn reserve_binding(
    root: &Path,
    session_id: &str,
    thread_id: &str,
    turn_id: Option<&str>,
    run_id: &str,
    client_request_id: Option<&str>,
    provider_thread_id: &str,
    launch_id: &str,
) -> Result<()> {
    let claims = root.join("binding-probes");
    std::fs::create_dir_all(&claims)?;
    set_private_dir(&claims)?;
    let target = claims.join(format!("{session_id}.json"));
    let backup = claims.join(format!("{session_id}.observed-backup.json"));
    if let Ok(bytes) = std::fs::read(&target) {
        if serde_json::from_slice::<Value>(&bytes)
            .ok()
            .and_then(|value| {
                value
                    .get("status")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            })
            .as_deref()
            == Some("observed")
        {
            atomic_write(&backup, &bytes)?;
        }
    }
    let payload = json!({
        "schema_version": 2,
        "provider": "cursor",
        "adapter": CURSOR_PRINT_ADAPTER,
        "status": "pending",
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "client_request_id": client_request_id,
        "conversation_uuid": provider_thread_id,
        "launch_id": launch_id,
        "expires_at": (Utc::now() + chrono::Duration::minutes(10)).to_rfc3339(),
    });
    atomic_write(&target, &serde_json::to_vec(&payload)?)
}

#[allow(clippy::too_many_arguments)]
fn promote_binding(
    root: &Path,
    session_id: &str,
    thread_id: &str,
    turn_id: Option<&str>,
    run_id: &str,
    client_request_id: Option<&str>,
    provider_thread_id: &str,
    launch_id: &str,
) -> Result<()> {
    let target = root
        .join("binding-probes")
        .join(format!("{session_id}.json"));
    let existing: Value = serde_json::from_slice(&std::fs::read(&target)?)?;
    if existing.get("status").and_then(Value::as_str) != Some("pending")
        || existing.get("session_id").and_then(Value::as_str) != Some(session_id)
        || existing.get("conversation_uuid").and_then(Value::as_str) != Some(provider_thread_id)
        || existing.get("launch_id").and_then(Value::as_str) != Some(launch_id)
    {
        anyhow::bail!("Cursor stream identity does not match its pending binding reservation");
    }
    atomic_write(
        &target,
        &serde_json::to_vec(&json!({
            "schema_version": 2,
            "provider": "cursor",
            "status": "observed",
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "run_id": run_id,
            "client_request_id": client_request_id,
            "conversation_uuid": provider_thread_id,
            "launch_id": launch_id,
            "hook_observed_at": Utc::now().to_rfc3339(),
        }))?,
    )
}

fn rollback_binding(root: &Path, session_id: &str, launch_id: &str) {
    let claims = root.join("binding-probes");
    let target = claims.join(format!("{session_id}.json"));
    let backup = claims.join(format!("{session_id}.observed-backup.json"));
    let pending_matches = std::fs::read(&target)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
        .is_some_and(|value| {
            value.get("status").and_then(Value::as_str) == Some("pending")
                && value.get("launch_id").and_then(Value::as_str) == Some(launch_id)
        });
    if !pending_matches {
        return;
    }
    if backup.exists() {
        let _ = std::fs::rename(&backup, &target);
    } else {
        let _ = std::fs::remove_file(&target);
    }
}

fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().context("state path has no parent")?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name().and_then(|v| v.to_str()).unwrap_or("state"),
        Uuid::new_v4()
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    std::fs::rename(&temporary, path)?;
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
    fn terminal_result_requires_success_shape() {
        assert_eq!(
            terminal_state_from_event(
                &json!({"type":"result","subtype":"success","is_error":false})
            )
            .as_deref(),
            Some("run_completed")
        );
        assert_eq!(
            terminal_state_from_event(&json!({"type":"result","subtype":"error","is_error":true}))
                .as_deref(),
            Some("run_failed")
        );
        assert!(terminal_state_from_event(&json!({"type":"assistant"})).is_none());
    }

    #[test]
    fn file_growth_emits_only_complete_lines() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("stdout.jsonl");
        std::fs::write(&path, b"one\ntw").unwrap();
        let mut offset = 0;
        let mut pending = Vec::new();
        assert_eq!(
            read_growth(&path, &mut offset, &mut pending).unwrap(),
            vec![b"one".to_vec()]
        );
        std::fs::write(&path, b"one\ntwo\n").unwrap();
        assert_eq!(
            read_growth(&path, &mut offset, &mut pending).unwrap(),
            vec![b"two".to_vec()]
        );
    }

    #[test]
    fn system_init_promotes_only_the_exact_pending_binding() {
        let temp = tempfile::tempdir().unwrap();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let turn_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        let provider_thread_id = Uuid::new_v4().to_string();
        let launch_id = Uuid::new_v4().to_string();
        reserve_binding(
            temp.path(),
            &session_id,
            &thread_id,
            Some(&turn_id),
            &run_id,
            Some("request-1"),
            &provider_thread_id,
            &launch_id,
        )
        .unwrap();
        assert!(promote_binding(
            temp.path(),
            &session_id,
            &thread_id,
            Some(&turn_id),
            &run_id,
            Some("request-1"),
            "wrong-provider-thread",
            &launch_id,
        )
        .is_err());
        promote_binding(
            temp.path(),
            &session_id,
            &thread_id,
            Some(&turn_id),
            &run_id,
            Some("request-1"),
            &provider_thread_id,
            &launch_id,
        )
        .unwrap();
        let claim: Value = serde_json::from_slice(
            &std::fs::read(
                temp.path()
                    .join("binding-probes")
                    .join(format!("{session_id}.json")),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(claim["status"], "observed");
        assert_eq!(claim["thread_id"], thread_id);
        assert_eq!(claim["run_id"], run_id);
    }

    #[tokio::test]
    #[ignore = "requires an authenticated stock cursor-agent and spends provider tokens"]
    async fn installed_cursor_completes_and_resumes_through_production_console_adapter() {
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let cursor_bin =
            std::env::var("LONGHOUSE_CURSOR_BIN").unwrap_or_else(|_| "cursor-agent".to_string());
        let marker = format!("LH_CURSOR_CONSOLE_CANARY_{}", Uuid::new_v4().simple());

        async fn run_turn(
            cursor_bin: &str,
            cwd: &Path,
            prompt: String,
            resume: Option<String>,
            longhouse_identity: Option<(String, String)>,
        ) -> CursorPrintRunSummary {
            let (session_id, thread_id) = longhouse_identity
                .unwrap_or_else(|| (Uuid::new_v4().to_string(), Uuid::new_v4().to_string()));
            let turn_id = Uuid::new_v4().to_string();
            let run_id = Uuid::new_v4().to_string();
            let client_request_id = format!("canary-{}", Uuid::new_v4());
            assert!(matches!(
                crate::turn_claims::default_registry()
                    .unwrap()
                    .claim(
                        &run_id,
                        &session_id,
                        &thread_id,
                        Some(&turn_id),
                        Some(&client_request_id),
                        "cursor",
                    )
                    .unwrap(),
                crate::turn_claims::ClaimOutcome::Acquired
            ));
            let summary = start_cursor_print_turn(CursorPrintRunConfig {
                session_id,
                thread_id,
                turn_id: Some(turn_id),
                run_id,
                client_request_id: Some(client_request_id),
                cwd: cwd.to_path_buf(),
                cursor_bin: cursor_bin.to_string(),
                prompt,
                resume_provider_thread_id: resume,
                model: Some("gpt-5.3-codex-low".to_string()),
                permission_mode: "bypass".to_string(),
                api_url: "http://127.0.0.1:1".to_string(),
                api_token: None,
                machine_name: "cursor-console-canary".to_string(),
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
                    let result = claim.result.unwrap();
                    assert_eq!(
                        result["terminal_state"],
                        "run_completed",
                        "stdout={}\nstderr={}",
                        std::fs::read_to_string(&summary.stdout_path).unwrap_or_default(),
                        std::fs::read_to_string(&summary.stderr_path).unwrap_or_default(),
                    );
                    return summary;
                }
                assert!(
                    tokio::time::Instant::now() < deadline,
                    "Cursor Console canary timed out"
                );
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }

        let first = run_turn(
            &cursor_bin,
            temp.path(),
            format!("Reply with exactly {marker} and nothing else. Do not use tools."),
            None,
            None,
        )
        .await;
        let first_output = std::fs::read_to_string(&first.stdout_path).unwrap();
        assert!(first_output.contains(&marker));
        assert!(first_output.contains(&first.provider_thread_id));
        let binding: Value = serde_json::from_slice(
            &std::fs::read(
                temp.path()
                    .join("longhouse/managed-local/cursor-helm/binding-probes")
                    .join(format!("{}.json", first.session_id)),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(binding["status"], "observed");
        assert_eq!(binding["conversation_uuid"], first.provider_thread_id);
        assert_eq!(binding["thread_id"], first.thread_id);

        // Cursor closes the local process before its remote resume checkpoint
        // is immediately reusable. Real Console dispatch naturally crosses
        // the runtime-event/catalog round trip; keep the direct canary honest
        // to that boundary instead of manufacturing a zero-gap second turn.
        tokio::time::sleep(Duration::from_secs(3)).await;
        let second_marker = format!("{marker}_RESUMED");
        let second = run_turn(
            &cursor_bin,
            temp.path(),
            format!("Reply with exactly {second_marker} and nothing else. Do not use tools."),
            Some(first.provider_thread_id.clone()),
            Some((first.session_id.clone(), first.thread_id.clone())),
        )
        .await;
        assert_eq!(second.provider_thread_id, first.provider_thread_id);
        assert!(std::fs::read_to_string(&second.stdout_path)
            .unwrap()
            .contains(&second_marker));

        let interrupt_turn_id = Uuid::new_v4().to_string();
        let interrupt_run_id = Uuid::new_v4().to_string();
        assert!(matches!(
            crate::turn_claims::default_registry()
                .unwrap()
                .claim(
                    &interrupt_run_id,
                    &first.session_id,
                    &first.thread_id,
                    Some(&interrupt_turn_id),
                    Some("cursor-canary-interrupt"),
                    "cursor",
                )
                .unwrap(),
            crate::turn_claims::ClaimOutcome::Acquired
        ));
        let interrupted = start_cursor_print_turn(CursorPrintRunConfig {
            session_id: first.session_id.clone(),
            thread_id: first.thread_id.clone(),
            turn_id: Some(interrupt_turn_id),
            run_id: interrupt_run_id.clone(),
            client_request_id: Some("cursor-canary-interrupt".to_string()),
            cwd: temp.path().to_path_buf(),
            cursor_bin: cursor_bin.clone(),
            prompt: "Use the shell tool to run exactly: sleep 30. Do not finish before the command finishes."
                .to_string(),
            resume_provider_thread_id: Some(first.provider_thread_id.clone()),
            model: Some("gpt-5.3-codex-low".to_string()),
            permission_mode: "bypass".to_string(),
            api_url: "http://127.0.0.1:1".to_string(),
            api_token: None,
            machine_name: "cursor-console-canary".to_string(),
            local_db_path: None,
        })
        .await
        .unwrap();
        let tool_deadline = tokio::time::Instant::now() + Duration::from_secs(90);
        loop {
            let stdout = std::fs::read_to_string(&interrupted.stdout_path).unwrap_or_default();
            if stdout.contains("\"type\":\"tool_call\"")
                || stdout.contains("\"type\": \"tool_call\"")
            {
                break;
            }
            assert!(
                tokio::time::Instant::now() < tool_deadline,
                "Cursor did not begin the interrupt canary tool"
            );
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        interrupt_cursor_print_turn(&interrupt_run_id, &first.session_id).unwrap();
        let cancel_deadline = tokio::time::Instant::now() + Duration::from_secs(15);
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
                "Cursor interrupt did not settle"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        assert_ne!(unsafe { libc::killpg(interrupted.process_group_id, 0) }, 0);

        tokio::time::sleep(Duration::from_secs(3)).await;
        let post_cancel_marker = format!("{marker}_AFTER_CANCEL");
        let post_cancel = run_turn(
            &cursor_bin,
            temp.path(),
            format!("Reply with exactly {post_cancel_marker} and nothing else. Do not use tools."),
            Some(first.provider_thread_id.clone()),
            Some((first.session_id.clone(), first.thread_id.clone())),
        )
        .await;
        assert!(std::fs::read_to_string(&post_cancel.stdout_path)
            .unwrap()
            .contains(&post_cancel_marker));

        match previous_home {
            Some(value) => unsafe { std::env::set_var("LONGHOUSE_HOME", value) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }
    }
}
