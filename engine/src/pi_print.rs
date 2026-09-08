//! Pi Console turns through stock `pi -p`.
//!
//! Turn-scoped one-shot adapter: one bounded stock `pi -p --mode json`
//! invocation per Console turn. The JSON event stream is projected live while
//! Pi's native JSONL remains the only durable transcript. Native identity is
//! reserved before spawn and exact session files are verified for every resume.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{json, Value};
use tokio::process::{Child, Command};

use crate::console_adapter::{claim_process_liveness, stderr_tail, ClaimLiveness};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use crate::pi_session::prepare_session;
use uuid::Uuid;

pub const PI_PRINT_ADAPTER: &str = "pi_print";
pub const DEFAULT_PI_BIN: &str = "pi";

#[derive(Clone, Debug)]
pub struct PiPrintRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub pi_bin: String,
    pub prompt: String,
    /// Pi's upstream provider id (e.g. `openrouter`), passed through only when
    /// the caller explicitly selected one.
    pub provider: Option<String>,
    pub model: Option<String>,
    /// Where Pi writes its native session JSONL. When absent, Pi's native
    /// `PI_CODING_AGENT_DIR`/default root is used by `prepare_session`.
    pub session_dir: Option<PathBuf>,
    /// Existing native identity to resume. The exact file is required when the
    /// caller has one; otherwise `prepare_session` performs an exact UUID scan.
    pub resume_thread_id: Option<String>,
    pub resume_session_file: Option<PathBuf>,
    pub permission_mode: String,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct PiPrintRunSummary {
    pub session_id: String,
    pub thread_id: String,
    pub run_id: String,
    pub provider_thread_id: String,
    pub launch_id: String,
    pub pid: u32,
    pub process_group_id: i32,
    pub stdout_path: String,
    pub stderr_path: String,
    pub session_dir: String,
    pub session_file: Option<String>,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct PiPrintSink {
    session_id: String,
    thread_id: String,
    turn_id: Option<String>,
    run_id: String,
    client_request_id: Option<String>,
    launch_id: String,
    process_group_id: Option<i32>,
    stdout_path: PathBuf,
    session_dir: PathBuf,
    provider_thread_id: String,
    session_file: Option<PathBuf>,
    binding_emitted: bool,
    machine_name: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

pub async fn start_pi_print_turn(config: PiPrintRunConfig) -> Result<PiPrintRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = normalized_optional(&config.turn_id) {
        validate_uuid(&turn_id, "turn_id")?;
    }
    if config.permission_mode != "provider_local" {
        anyhow::bail!("Pi Console currently supports provider_local permission mode only");
    }
    let launch_id = Uuid::new_v4().to_string();
    let target = prepare_session(
        &config.cwd,
        config.session_dir.as_deref(),
        config.resume_thread_id.as_deref(),
        config.resume_session_file.as_deref(),
    )?;
    let provider_thread_id = target.provider_thread_id.clone();
    let exact_session_file = target.session_file.clone();
    let session_dir = target.session_dir.clone();
    std::fs::create_dir_all(&session_dir)?;
    set_private_dir(&session_dir)?;

    // Reserve the native identity before Pi can create or touch a session
    // file. This is the fence against watcher discovery winning the initial
    // write race and creating an unrelated Shadow session.
    if let Some(path) = exact_session_file.as_deref() {
        persist_transcript_binding(
            config.local_db_path.as_deref(),
            path,
            &config.session_id,
            &provider_thread_id,
        )?;
    }
    crate::turn_claims::default_registry()?.mark_provider_binding(
        &config.run_id,
        &provider_thread_id,
        exact_session_file
            .as_deref()
            .map(|path| path.to_string_lossy().to_string())
            .as_deref(),
    )?;

    let run_dir = crate::config::get_agent_dir()?
        .join("pi-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    let stdout_path = run_dir.join("stdout.log");
    let stderr_path = run_dir.join("stderr.log");
    let stdout_file = private_output_file(&stdout_path)?;
    let stderr_file = private_output_file(&stderr_path)?;
    let runtime_events_outbox_dir = crate::config::get_agent_runtime_events_outbox_dir()?;

    let args = build_pi_args(
        &config.prompt,
        config.provider.as_deref(),
        config.model.as_deref(),
        &target,
    );
    let argv = std::iter::once(config.pi_bin.clone())
        .chain(args.iter().cloned())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.pi_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file));
    ManagedIdentity::new(ManagedProvider::Pi, &config.session_id)
        .with_run_id(&config.run_id)
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
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return Err(error).with_context(|| format!("spawning `{}` -p", config.pi_bin))
        }
    };
    let pid = match child.id() {
        Some(pid) => pid,
        None => {
            let _ = child.kill().await;
            return Err(anyhow::anyhow!("pi -p returned no pid"));
        }
    };
    let process_group_id = i32::try_from(pid).context("Pi pid exceeds process-group range")?;
    let sink = PiPrintSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        stdout_path: stdout_path.clone(),
        session_dir: session_dir.clone(),
        provider_thread_id: provider_thread_id.clone(),
        session_file: exact_session_file.clone(),
        binding_emitted: false,
        machine_name: config.machine_name.clone(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir,
    };
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "pi",
        "transport": PI_PRINT_ADAPTER,
        "provider_thread_id": provider_thread_id,
        "launch_id": launch_id,
        "pid": pid,
        "process_group_id": process_group_id,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "session_dir": session_dir,
        "cwd": config.cwd,
        "machine_name": config.machine_name,
        "argv": argv,
    });
    if let Err(error) = crate::turn_claims::default_registry()?.mark_spawned_invocation(
        &config.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        PI_PRINT_ADAPTER,
        &launch_id,
        Some(&target.provider_thread_id),
        &stdout_path.to_string_lossy(),
        &stderr_path.to_string_lossy(),
        result,
    ) {
        cleanup_process_group(Some(process_group_id)).await;
        let _ = child.kill().await;
        return Err(error).context("persisting Pi Console spawn identity");
    }
    let monitor_stderr = stderr_path.clone();
    tokio::spawn(async move {
        monitor_pi_print(&mut child, &monitor_stderr, sink).await;
    });

    Ok(PiPrintRunSummary {
        session_id: config.session_id,
        thread_id: config.thread_id,
        run_id: config.run_id,
        provider_thread_id: target.provider_thread_id,
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        session_dir: session_dir.to_string_lossy().to_string(),
        session_file: exact_session_file.map(|path| path.to_string_lossy().to_string()),
        argv,
    })
}

pub async fn recover_pi_print_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    // One coherent inventory for the pass; `None` means `ps` was unreadable,
    // which must leave claims alone rather than settle them.
    let inventory = crate::process_identity::try_collect_process_facts_by_pid();
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        if claim.adapter.as_deref() != Some(PI_PRINT_ADAPTER) || claim.state != "spawned" {
            continue;
        }
        let Some(stdout_path) = claim.stdout_path.as_deref().map(PathBuf::from) else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("Pi Console claim has no stdout path".to_string()),
            );
            continue;
        };
        let stderr_path = claim
            .stderr_path
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| stdout_path.with_file_name("stderr.log"));
        let session_dir = claim
            .result
            .as_ref()
            .and_then(|result| result.get("session_dir").and_then(Value::as_str))
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        let sink = PiPrintSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            stdout_path: stdout_path.clone(),
            session_dir,
            provider_thread_id: claim.provider_thread_id.clone().unwrap_or_default(),
            session_file: claim.source_path.as_deref().map(PathBuf::from),
            binding_emitted: false,
            machine_name: machine_name.to_string(),
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        if sink.provider_thread_id.is_empty() {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("Pi Console claim has no native provider identity".to_string()),
            );
            continue;
        }
        match crate::console_adapter::claim_liveness(&claim, inventory.as_ref()) {
            ClaimLiveness::Live => {
                tokio::spawn(async move {
                    monitor_recovered_claim(claim, stdout_path, stderr_path, sink).await;
                });
                recovered += 1;
            }
            ClaimLiveness::Gone => {
                settle_recovered_dead_claim(&claim, &stderr_path, &sink).await;
            }
            ClaimLiveness::Unknown => tracing::warn!(
                run_id = %claim.run_id,
                "Process inventory unavailable; leaving Pi Console turn claim for a later scan"
            ),
        }
    }
    Ok(recovered)
}

pub fn interrupt_pi_print_turn(run_id: &str, session_id: &str) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id || claim.provider != "pi" {
        anyhow::bail!("Pi Console turn claim does not match the requested session");
    }
    if claim.adapter.as_deref() != Some(PI_PRINT_ADAPTER) || claim.state != "spawned" {
        anyhow::bail!("Pi Console turn is not active");
    }
    let pid = claim.pid.context("Pi Console turn has no provider pid")?;
    let expected_start = claim
        .process_start_time
        .as_deref()
        .context("Pi Console turn has no process-start identity")?;
    let actual = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .cloned()
        .context("Pi Console provider process is gone")?;
    if actual.lstart != expected_start {
        anyhow::bail!("Pi Console provider pid identity changed");
    }
    let pgid = claim
        .process_group_id
        .context("Pi Console turn has no process-group identity")?;
    registry.mark_cancel_requested(run_id)?;
    let result = unsafe { libc::killpg(pgid, libc::SIGINT) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting Pi Console process group");
        }
    }
    Ok(())
}

async fn monitor_pi_print(child: &mut Child, stderr_path: &Path, mut sink: PiPrintSink) {
    let mut projection = PiStreamProjection::default();
    sink.post_phase("thinking", None, 0).await;
    let mut offset = 0_u64;
    let mut pending = Vec::new();
    let mut seq = 0_u64;
    loop {
        if let Err(error) = publish_stdout_growth(
            &mut sink,
            &mut projection,
            &mut offset,
            &mut pending,
            &mut seq,
        )
        .await
        {
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal("run_failed", None, Some(error.to_string()))
                .await;
            return;
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                // Pi flushes the final JSON event and native file close during
                // shutdown. Drain after the child exits before deciding success.
                tokio::time::sleep(Duration::from_millis(150)).await;
                let drain_error = publish_stdout_growth(
                    &mut sink,
                    &mut projection,
                    &mut offset,
                    &mut pending,
                    &mut seq,
                )
                .await
                .err()
                .map(|error| error.to_string());
                let cancel_requested = crate::turn_claims::default_registry()
                    .and_then(|registry| registry.read(&sink.run_id))
                    .ok()
                    .and_then(|claim| claim.cancel_requested_at)
                    .is_some();
                let source_bound = sink.ensure_transcript_binding().await.unwrap_or(false);
                if source_bound {
                    if let Some(path) = sink.session_file.as_deref() {
                        sink.wake_transcript_shipper(path, &sink.provider_thread_id)
                            .await;
                    }
                }
                cleanup_process_group(sink.process_group_id).await;
                let (terminal_state, reason) = terminal_state_for_projection(
                    &projection,
                    if status.success() {
                        PiProcessExitEvidence::Succeeded
                    } else {
                        PiProcessExitEvidence::Failed
                    },
                    cancel_requested,
                    drain_error.as_deref(),
                    pending.is_empty(),
                    source_bound,
                );
                let terminal_reason = if terminal_state == "run_failed" {
                    reason.or_else(|| stderr_tail(stderr_path))
                } else {
                    reason
                };
                sink.post_terminal(terminal_state, status.code(), terminal_reason)
                    .await;
                return;
            }
            Ok(None) => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(error) => {
                cleanup_process_group(sink.process_group_id).await;
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
    mut sink: PiPrintSink,
) {
    let mut projection = replay_projection(
        &stdout_path,
        claim.projected_stdout_offset,
        &sink.provider_thread_id,
    );
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    sink.post_phase("thinking", None, seq).await;
    loop {
        if let Err(error) = publish_stdout_growth(
            &mut sink,
            &mut projection,
            &mut offset,
            &mut pending,
            &mut seq,
        )
        .await
        {
            if claim.process_group_is_from_this_boot() {
                cleanup_process_group(sink.process_group_id).await;
            }
            sink.post_terminal("run_failed", None, Some(error.to_string()))
                .await;
            return;
        }
        if claim_process_liveness(&claim) == ClaimLiveness::Gone {
            let cancel_requested = crate::turn_claims::default_registry()
                .and_then(|registry| registry.read(&claim.run_id))
                .ok()
                .and_then(|current| current.cancel_requested_at)
                .is_some();
            let source_bound = sink.ensure_transcript_binding().await.unwrap_or(false);
            if source_bound {
                if let Some(path) = sink.session_file.as_deref() {
                    sink.wake_transcript_shipper(path, &sink.provider_thread_id)
                        .await;
                }
            }
            if claim.process_group_is_from_this_boot() {
                cleanup_process_group(sink.process_group_id).await;
            }
            let (terminal_state, reason) = terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Unknown,
                cancel_requested,
                None,
                pending.is_empty(),
                source_bound,
            );
            let terminal_reason = if terminal_state == "run_failed" {
                reason.or_else(|| stderr_tail(&stderr_path))
            } else {
                reason
            };
            sink.post_terminal(terminal_state, None, terminal_reason)
                .await;
            return;
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

async fn settle_recovered_dead_claim(
    claim: &crate::turn_claims::TurnClaim,
    stderr_path: &Path,
    sink: &PiPrintSink,
) {
    let mut sink = sink.clone();
    let mut projection = replay_projection(
        &sink.stdout_path,
        claim.projected_stdout_offset,
        &sink.provider_thread_id,
    );
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let stream_error = publish_stdout_growth(
        &mut sink,
        &mut projection,
        &mut offset,
        &mut pending,
        &mut seq,
    )
    .await
    .err()
    .map(|error| error.to_string());
    let source_bound = sink.ensure_transcript_binding().await.unwrap_or(false);
    if source_bound {
        if let Some(path) = sink.session_file.as_deref() {
            sink.wake_transcript_shipper(path, &sink.provider_thread_id)
                .await;
        }
    }
    if claim.process_group_is_from_this_boot() {
        cleanup_process_group(sink.process_group_id).await;
    }
    let (terminal_state, reason) = terminal_state_for_projection(
        &projection,
        PiProcessExitEvidence::Unknown,
        claim.cancel_requested_at.is_some(),
        stream_error.as_deref(),
        pending.is_empty(),
        source_bound,
    );
    let terminal_reason = if terminal_state == "run_failed" {
        reason.or_else(|| stderr_tail(stderr_path))
    } else {
        reason
    };
    sink.post_terminal(terminal_state, None, terminal_reason)
        .await;
}

fn build_pi_args(
    prompt: &str,
    provider: Option<&str>,
    model: Option<&str>,
    target: &crate::pi_session::PiSessionTarget,
) -> Vec<String> {
    let mut args = vec![
        "-p".to_string(),
        prompt.to_string(),
        "--mode".to_string(),
        "json".to_string(),
    ];
    if let Some(provider) = provider.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--provider".to_string(), provider.to_string()]);
    }
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--model".to_string(), model.to_string()]);
    }
    args.extend([
        "--session-dir".to_string(),
        target.session_dir.to_string_lossy().to_string(),
    ]);
    if let Some(session_file) = target.session_file.as_ref() {
        args.extend([
            "--session".to_string(),
            session_file.to_string_lossy().to_string(),
        ]);
    } else {
        args.extend([
            "--session-id".to_string(),
            target.provider_thread_id.clone(),
        ]);
    }
    args
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct PiStreamProjection {
    identity_confirmed: bool,
    assistant_message_index: u64,
    current_assistant: Option<PiAssistantProjection>,
    final_stop_reason: Option<String>,
    agent_settled: bool,
    native_error: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct PiAssistantProjection {
    index: u64,
    live_text: String,
}

impl PiStreamProjection {
    fn apply(&mut self, expected_provider_thread_id: &str, event: &Value) -> Result<()> {
        match event.get("type").and_then(Value::as_str) {
            Some("session") => {
                let observed = event
                    .get("id")
                    .and_then(Value::as_str)
                    .context("Pi JSON stream session header has no id")?;
                anyhow::ensure!(
                    observed == expected_provider_thread_id,
                    "Pi JSON stream session id {observed} does not match reserved provider thread {expected_provider_thread_id}"
                );
                self.identity_confirmed = true;
            }
            Some("agent_start") => {
                self.agent_settled = false;
                self.final_stop_reason = None;
                self.native_error = None;
            }
            Some("message_start") => {
                let message = event
                    .get("message")
                    .context("Pi message_start has no message")?;
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    self.final_stop_reason = None;
                    let index = self.assistant_message_index;
                    self.assistant_message_index += 1;
                    self.current_assistant = Some(PiAssistantProjection {
                        index,
                        live_text: message_text(message),
                    });
                }
            }
            Some("message_update") => {
                if let Some(current) = self.current_assistant.as_mut() {
                    let update = event.get("assistantMessageEvent");
                    if update
                        .and_then(|value| value.get("type"))
                        .and_then(Value::as_str)
                        == Some("text_delta")
                    {
                        if let Some(delta) = update
                            .and_then(|value| value.get("delta"))
                            .and_then(Value::as_str)
                        {
                            current.live_text.push_str(delta);
                        }
                    }
                }
            }
            Some("message_end") => {
                let message = event
                    .get("message")
                    .context("Pi message_end has no message")?;
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    if self.current_assistant.is_none() {
                        let index = self.assistant_message_index;
                        self.assistant_message_index += 1;
                        self.current_assistant = Some(PiAssistantProjection {
                            index,
                            live_text: String::new(),
                        });
                    }
                    if let Some(current) = self.current_assistant.as_mut() {
                        // Pi's final message is authoritative after delta-only
                        // message_update events in current JSON mode.
                        current.live_text = message_text(message);
                    }
                    self.final_stop_reason = message
                        .get("stopReason")
                        .and_then(Value::as_str)
                        .map(str::to_string);
                }
            }
            Some("agent_settled") => self.agent_settled = true,
            Some("error") => {
                self.native_error = event
                    .get("message")
                    .and_then(Value::as_str)
                    .or_else(|| event.get("errorMessage").and_then(Value::as_str))
                    .map(str::to_string);
            }
            _ => {}
        }
        Ok(())
    }

    fn live_text(&self) -> Option<&str> {
        self.current_assistant
            .as_ref()
            .map(|assistant| assistant.live_text.as_str())
    }

    fn item_id(&self, run_id: &str) -> Option<String> {
        self.current_assistant
            .as_ref()
            .map(|assistant| format!("{run_id}:assistant:{}", assistant.index))
    }
}

fn message_text(message: &Value) -> String {
    match message.get("content") {
        Some(Value::String(text)) => text.to_string(),
        Some(Value::Array(content)) => content
            .iter()
            .filter(|block| block.get("type").and_then(Value::as_str) == Some("text"))
            .filter_map(|block| block.get("text").and_then(Value::as_str))
            .collect::<String>(),
        _ => String::new(),
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PiProcessExitEvidence {
    Succeeded,
    Failed,
    Unknown,
}

fn terminal_state_for_projection(
    projection: &PiStreamProjection,
    process_exit: PiProcessExitEvidence,
    cancel_requested: bool,
    stream_error: Option<&str>,
    output_drained: bool,
    source_bound: bool,
) -> (&'static str, Option<String>) {
    // As with the other Console adapters, an owned cancellation takes
    // precedence over the nonzero exit caused by our SIGINT.
    if cancel_requested {
        return ("run_cancelled", None);
    }
    if process_exit == PiProcessExitEvidence::Failed {
        return (
            "run_failed",
            Some("Pi process exited unsuccessfully".to_string()),
        );
    }
    if let Some(error) = stream_error.or(projection.native_error.as_deref()) {
        return ("run_failed", Some(error.to_string()));
    }
    if !output_drained {
        return (
            "run_failed",
            Some("Pi JSON stream ended with an incomplete event".to_string()),
        );
    }
    if !source_bound {
        return (
            "run_failed",
            Some("Pi completed without an exact native session file".to_string()),
        );
    }
    if !projection.identity_confirmed {
        return (
            "run_failed",
            Some("Pi JSON stream never confirmed its native session identity".to_string()),
        );
    }
    if !projection.agent_settled {
        return (
            "run_failed",
            Some("Pi exited before agent_settled".to_string()),
        );
    }
    let Some(stop_reason) = projection.final_stop_reason.as_deref() else {
        return (
            "run_failed",
            Some("Pi settled without a final assistant message".to_string()),
        );
    };
    if matches!(stop_reason, "error" | "aborted") {
        return (
            "run_failed",
            Some(format!("Pi assistant stopped with {stop_reason}")),
        );
    }
    if !matches!(stop_reason, "stop" | "length") {
        return (
            "run_failed",
            Some(format!("Pi reported unsupported stop reason {stop_reason}")),
        );
    }
    ("run_completed", None)
}

async fn publish_stdout_growth(
    sink: &mut PiPrintSink,
    projection: &mut PiStreamProjection,
    offset: &mut u64,
    pending: &mut Vec<u8>,
    seq: &mut u64,
) -> Result<()> {
    let lines = crate::console_adapter::read_growth(&sink.stdout_path, offset, pending)?;
    let had_lines = !lines.is_empty();
    for bytes in lines {
        *seq += 1;
        let event: Value = match serde_json::from_slice(&bytes) {
            Ok(event) => event,
            Err(error) => {
                sink.post_decode_gap(*seq, &error.to_string()).await;
                continue;
            }
        };
        projection.apply(&sink.provider_thread_id, &event)?;
        if let Some((phase, tool_name)) = pi_phase_from_event(&event) {
            sink.post_phase(phase, tool_name, *seq).await;
        }
        sink.post_stream_event(*seq, &event, projection).await;
    }
    let _ = sink.ensure_transcript_binding().await?;
    if had_lines {
        let complete_offset = offset.saturating_sub(pending.len() as u64);
        if let Ok(registry) = crate::turn_claims::default_registry() {
            let _ = registry.mark_projection_checkpoint(&sink.run_id, complete_offset, *seq);
        }
    }
    Ok(())
}

fn pi_phase_from_event(event: &Value) -> Option<(&'static str, Option<String>)> {
    match event.get("type").and_then(Value::as_str) {
        Some("tool_execution_start") => Some((
            "running",
            event
                .get("toolName")
                .and_then(Value::as_str)
                .map(str::to_string),
        )),
        Some("tool_execution_end") => Some(("thinking", None)),
        Some("agent_settled") => Some(("idle", None)),
        _ => None,
    }
}

fn replay_projection(
    path: &Path,
    limit: u64,
    expected_provider_thread_id: &str,
) -> PiStreamProjection {
    let mut projection = PiStreamProjection::default();
    let Ok(file) = File::open(path) else {
        return projection;
    };
    let mut bytes = Vec::new();
    if file.take(limit).read_to_end(&mut bytes).is_err() {
        return projection;
    }
    for line in bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
    {
        if let Ok(event) = serde_json::from_slice::<Value>(line) {
            let _ = projection.apply(expected_provider_thread_id, &event);
        }
    }
    projection
}

fn locate_exact_transcript(
    session_dir: &Path,
    provider_thread_id: &str,
) -> Result<Option<PathBuf>> {
    let mut matches = Vec::new();
    for entry in walkdir::WalkDir::new(session_dir)
        .follow_links(false)
        .max_depth(2)
        .into_iter()
        .filter_map(Result::ok)
    {
        let path = entry.path();
        if !entry.file_type().is_file()
            || path.extension().and_then(|value| value.to_str()) != Some("jsonl")
        {
            continue;
        }
        if crate::pi_session::read_session_header_id(path)
            .ok()
            .as_deref()
            == Some(provider_thread_id)
        {
            matches.push(path.canonicalize().unwrap_or_else(|_| path.to_path_buf()));
        }
    }
    matches.sort();
    matches.dedup();
    match matches.len() {
        0 => Ok(None),
        1 => Ok(matches.into_iter().next()),
        count => anyhow::bail!(
            "Pi provider thread {provider_thread_id} has {count} native session files"
        ),
    }
}

fn persist_transcript_binding(
    db_path: Option<&Path>,
    transcript: &Path,
    session_id: &str,
    provider_thread_id: &str,
) -> Result<()> {
    let Some(db_path) = db_path else {
        return Ok(());
    };
    let conn = crate::state::db::open_client_connection(db_path, Duration::from_millis(500))?;
    let stable_path = crate::storage_v2_shipper::stable_source_path(transcript);
    let binding = crate::state::session_binding::SessionBinding::new(&conn);
    if let Some((existing_session, existing_provider_thread)) =
        binding.get_with_thread_for_provider(&stable_path.to_string_lossy(), "pi")?
    {
        anyhow::ensure!(
            existing_session == session_id,
            "Pi native session file is already bound to another Longhouse session"
        );
        anyhow::ensure!(
            existing_provider_thread
                .as_deref()
                .map(|value| value == provider_thread_id)
                .unwrap_or(true),
            "Pi native session file is bound to another provider identity"
        );
    }
    binding.bind_for_thread(
        &stable_path.to_string_lossy(),
        session_id,
        "pi",
        Some(provider_thread_id),
    )?;
    Ok(())
}

impl PiPrintSink {
    async fn ensure_transcript_binding(&mut self) -> Result<bool> {
        if self.binding_emitted {
            return Ok(self.session_file.is_some());
        }
        if self.session_file.is_none() {
            self.session_file =
                locate_exact_transcript(&self.session_dir, &self.provider_thread_id)?;
        }
        let Some(transcript) = self.session_file.clone() else {
            return Ok(false);
        };
        let Ok(provider_session_id) = crate::pi_session::read_session_header_id(&transcript) else {
            return Ok(false);
        };
        anyhow::ensure!(
            provider_session_id == self.provider_thread_id,
            "Pi native session header changed identity after launch"
        );
        persist_transcript_binding(
            self.local_db_path.as_deref(),
            &transcript,
            &self.session_id,
            &self.provider_thread_id,
        )?;
        crate::turn_claims::default_registry()?.mark_provider_binding(
            &self.run_id,
            &self.provider_thread_id,
            Some(&transcript.to_string_lossy()),
        )?;
        if !self.binding_emitted {
            self.post_binding(&self.provider_thread_id, &transcript)
                .await;
            self.binding_emitted = true;
        }
        Ok(true)
    }

    async fn post_binding(&self, provider_session_id: &str, transcript: &Path) {
        self.post_events(vec![json!({
            "runtime_key": format!("pi:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "pi",
            "device_id": self.machine_name,
            "source": PI_PRINT_ADAPTER,
            "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("pi-print:{}:{}:binding", self.session_id, self.launch_id),
            "payload": {
                "provider_session_id": provider_session_id,
                "source_path": transcript.to_string_lossy(),
                "managed_transport": PI_PRINT_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>, activity_seq: u64) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("pi:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "pi",
            "device_id": self.machine_name,
            "source": PI_PRINT_ADAPTER,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!("pi-print:{}:{}:phase:{phase}:{activity_seq}", self.session_id, self.run_id),
            "payload": {"managed_transport": PI_PRINT_ADAPTER, "execution_lifetime": "one_shot"}
        })])
        .await;
    }

    async fn post_stream_event(&self, seq: u64, event: &Value, projection: &PiStreamProjection) {
        let mut payload = json!({
            "progress_kind": "pi_print_stream",
            "seq": seq,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "client_request_id": self.client_request_id,
            "provider_thread_id": self.provider_thread_id,
            "event": event,
            "managed_transport": PI_PRINT_ADAPTER,
            "execution_lifetime": "one_shot"
        });
        if let Some(item_id) = projection.item_id(&self.run_id) {
            payload["item_id"] = json!(item_id);
        }
        if let Some(live_text) = projection.live_text() {
            payload["live_text"] = json!(live_text);
        }
        if let Some(tool_call_id) = event
            .get("toolCallId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            payload["item_id"] = json!(tool_call_id);
            payload["toolCallId"] = json!(tool_call_id);
            for key in ["toolName", "args", "partialResult", "result", "isError"] {
                if let Some(value) = event.get(key) {
                    payload[key] = value.clone();
                }
            }
        }
        self.post_events(vec![json!({
            "runtime_key": format!("pi:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "pi",
            "device_id": self.machine_name,
            "source": PI_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("pi-print:{}:{}:stdout:{seq}", self.session_id, self.run_id),
            "payload": payload
        })])
        .await;
    }

    async fn post_decode_gap(&self, seq: u64, error: &str) {
        self.post_events(vec![json!({
            "runtime_key": format!("pi:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "pi",
            "device_id": self.machine_name,
            "source": PI_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("pi-print:{}:{}:decode-gap:{seq}", self.session_id, self.run_id),
            "payload": {
                "progress_kind": "pi_print_decode_gap",
                "seq": seq,
                "error": error,
                "managed_transport": PI_PRINT_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr: Option<String>,
    ) {
        self.persist_local_phase("finished", None, Utc::now());
        self.post_events(vec![json!({
            "runtime_key": format!("pi:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "pi",
            "device_id": self.machine_name,
            "source": PI_PRINT_ADAPTER,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("pi-print:{}:{}:terminal", self.session_id, self.run_id),
            "payload": {
                "managed_transport": PI_PRINT_ADAPTER,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": PI_PRINT_ADAPTER,
                "exit_code": exit_code,
                "stderr_tail": stderr,
                "provider_thread_id": self.provider_thread_id,
                "source_path": self.session_file.as_ref().map(|path| path.to_string_lossy()),
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id
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
        let conn = match crate::state::db::open_client_connection(
            Path::new(db_path),
            Duration::from_millis(250),
        ) {
            Ok(conn) => conn,
            Err(err) => {
                eprintln!("[pi-print] open local phase DB failed: {err}");
                return;
            }
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "pi".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: PI_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[pi-print] persist local phase failed for {}: {err}",
                self.session_id
            );
        }
    }

    #[cfg(unix)]
    async fn wake_transcript_shipper(&self, source_path: &Path, provider_session_id: &str) {
        let Some(socket_path) = crate::config::get_agent_transcript_wake_socket_path().ok() else {
            eprintln!(
                "[pi-print] latency stage=durable_wake_miss session={} run={} reason=socket_unresolved",
                self.session_id, self.run_id
            );
            return;
        };
        if !socket_path.exists() {
            eprintln!(
                "[pi-print] latency stage=durable_wake_miss session={} run={} reason=socket_missing socket={}",
                self.session_id,
                self.run_id,
                socket_path.display()
            );
            return;
        }
        let payload = json!({
            "provider": "pi",
            "path": source_path,
            "phase": "idle",
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "provider_turn_id": provider_session_id,
            "client_request_id": self.client_request_id,
            "wake_reason": "turn_completed",
            "observed_at_ms": Utc::now().timestamp_millis(),
            "file_len_hint": std::fs::metadata(source_path).ok().map(|metadata| metadata.len()),
        });
        let bytes = payload.to_string().into_bytes();
        let socket_display = socket_path.display().to_string();
        let write = tokio::task::spawn_blocking(move || -> std::io::Result<()> {
            let mut stream = std::os::unix::net::UnixStream::connect(socket_path)?;
            stream.set_write_timeout(Some(Duration::from_millis(50)))?;
            stream.write_all(&bytes)
        });
        match tokio::time::timeout(Duration::from_millis(75), write).await {
            Ok(Ok(Ok(()))) => eprintln!(
                "[pi-print] latency stage=durable_wake_sent session={} run={} turn={} provider_turn={} path={}",
                self.session_id,
                self.run_id,
                self.turn_id.as_deref().unwrap_or("unknown"),
                provider_session_id,
                source_path.display()
            ),
            Ok(Ok(Err(err))) => eprintln!(
                "[pi-print] latency stage=durable_wake_miss session={} run={} reason=connect_or_write_failed socket={} error={err}",
                self.session_id, self.run_id, socket_display
            ),
            Ok(Err(err)) => eprintln!(
                "[pi-print] latency stage=durable_wake_miss session={} run={} reason=join_failed error={err}",
                self.session_id, self.run_id
            ),
            Err(_) => eprintln!(
                "[pi-print] latency stage=durable_wake_miss session={} run={} reason=timeout socket={}",
                self.session_id, self.run_id, socket_display
            ),
        }
    }

    #[cfg(not(unix))]
    async fn wake_transcript_shipper(&self, _source_path: &Path, _provider_session_id: &str) {}

    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            if let Err(error) =
                crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event)
            {
                eprintln!("[pi-print] runtime outbox write failed: {error}");
            }
        }
    }
}

async fn cleanup_process_group(process_group_id: Option<i32>) {
    crate::console_adapter::cleanup_process_group("pi-print", process_group_id).await;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn session_header_id_is_read_from_the_session_jsonl_header() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp
            .path()
            .join("1723200000_019f6b93-edf6-7bd0-a757-b5195a61abdd.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"session\",\"version\":3,\"id\":\"019f6b93-edf6-7bd0-a757-b5195a61abdd\",\"cwd\":\"/tmp\",\"timestamp\":\"2026-08-10T00:00:00Z\"}\n{\"type\":\"message\",\"id\":\"msg1\",\"timestamp\":\"2026-08-10T00:00:01Z\"}\n",
        )
        .unwrap();
        assert_eq!(
            crate::pi_session::read_session_header_id(&path)
                .ok()
                .as_deref(),
            Some("019f6b93-edf6-7bd0-a757-b5195a61abdd")
        );

        std::fs::write(&path, "{\"type\":\"message\",\"id\":\"msg1\"}\n").unwrap();
        assert!(crate::pi_session::read_session_header_id(&path).is_err());
    }

    #[test]
    fn exact_native_transcript_is_located_by_header_identity() {
        let temp = tempfile::tempdir().unwrap();
        let first = temp
            .path()
            .join("1723100000_019f6b93-0000-7000-8000-000000000001.jsonl");
        let second = temp
            .path()
            .join("1723200000_019f6b93-0000-7000-8000-000000000002.jsonl");
        std::fs::write(
            &first,
            "{\"type\":\"session\",\"id\":\"019f6b93-0000-7000-8000-000000000001\"}\n",
        )
        .unwrap();
        std::fs::write(
            &second,
            "{\"type\":\"session\",\"id\":\"019f6b93-0000-7000-8000-000000000002\"}\n",
        )
        .unwrap();
        std::fs::write(temp.path().join("stderr.log"), "noise").unwrap();
        assert_eq!(
            locate_exact_transcript(temp.path(), "019f6b93-0000-7000-8000-000000000002").unwrap(),
            Some(second.canonicalize().unwrap())
        );
    }

    #[test]
    fn json_deltas_accumulate_until_message_end_replaces_with_authoritative_text() {
        let provider_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        let mut projection = PiStreamProjection::default();
        projection
            .apply(provider_id, &json!({"type":"session","id":provider_id}))
            .unwrap();
        projection
            .apply(
                provider_id,
                &json!({"type":"message_start","message":{"role":"assistant","content":[]}}),
            )
            .unwrap();
        projection
            .apply(
                provider_id,
                &json!({"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"stale"}}),
            )
            .unwrap();
        assert_eq!(projection.live_text(), Some("stale"));
        projection
            .apply(
                provider_id,
                &json!({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"authoritative"}],"stopReason":"stop"}}),
            )
            .unwrap();
        assert_eq!(projection.live_text(), Some("authoritative"));
    }

    #[test]
    fn settlement_requires_the_current_native_run_to_finish_successfully() {
        let provider_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
        let mut projection = PiStreamProjection::default();
        for event in [
            json!({"type":"session","id":provider_id}),
            json!({"type":"agent_start"}),
            json!({"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error"}}),
            json!({"type":"agent_settled"}),
        ] {
            projection.apply(provider_id, &event).unwrap();
        }
        assert_eq!(
            terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Succeeded,
                false,
                None,
                true,
                true,
            )
            .0,
            "run_failed"
        );
        projection
            .apply(provider_id, &json!({"type":"agent_start"}))
            .unwrap();
        projection.apply(provider_id, &json!({
            "type":"message_end",
            "message":{"role":"assistant","content":[{"type":"text","text":"recovered"}],"stopReason":"stop"}
        })).unwrap();
        assert_eq!(
            terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Succeeded,
                false,
                None,
                true,
                true,
            )
            .0,
            "run_failed"
        );
        projection
            .apply(provider_id, &json!({"type":"agent_settled"}))
            .unwrap();
        assert_eq!(
            terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Succeeded,
                false,
                None,
                true,
                true,
            )
            .0,
            "run_completed"
        );
        assert_eq!(
            terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Unknown,
                false,
                None,
                true,
                true,
            )
            .0,
            "run_completed"
        );
        assert_eq!(
            terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Failed,
                false,
                None,
                true,
                true,
            )
            .0,
            "run_failed"
        );
        projection
            .apply(provider_id, &json!({"type":"agent_start"}))
            .unwrap();
        assert_eq!(
            terminal_state_for_projection(
                &projection,
                PiProcessExitEvidence::Succeeded,
                false,
                None,
                true,
                true,
            )
            .0,
            "run_failed"
        );
    }

    fn write_fake_pi(path: &Path, sleep_secs: u32) {
        let sleep_line = if sleep_secs > 0 {
            format!("    time.sleep({sleep_secs})\n")
        } else {
            String::new()
        };
        std::fs::write(
            path,
            format!(
                r#"#!/usr/bin/env python3
import json, os, sys, time, uuid
if "--version" in sys.argv:
    print("0.84.1")
    sys.exit(0)
args = sys.argv[1:]
if "-p" in args:
    session_dir = args[args.index("--session-dir") + 1]
    os.makedirs(session_dir, exist_ok=True)
    sid = args[args.index("--session-id") + 1]
    path = os.path.join(session_dir, f"1723200000_{{sid}}.jsonl")
    header = {{"type": "session", "version": 3, "id": sid, "cwd": os.getcwd(), "timestamp": "2026-08-10T00:00:00Z"}}
    with open(path, "w") as f:
        f.write(json.dumps(header) + "\n")
        f.write(json.dumps({{"type": "message", "id": str(uuid.uuid4()), "parentId": None, "timestamp": "2026-08-10T00:00:01Z", "message": {{"role": "assistant", "content": [{{"type": "text", "text": "fake pi reply"}}], "stopReason": "stop"}}}}) + "\n")
    events = [
        header,
        {{"type": "message_start", "message": {{"role": "assistant", "content": []}}}},
        {{"type": "message_update", "assistantMessageEvent": {{"type": "text_delta", "delta": "fake pi reply"}}}},
        {{"type": "message_end", "message": {{"role": "assistant", "content": [{{"type": "text", "text": "fake pi reply"}}], "stopReason": "stop"}}}},
        {{"type": "agent_end", "messages": []}},
        {{"type": "agent_settled"}},
    ]
    for event in events:
        print(json.dumps(event), flush=True)
{sleep_line}sys.exit(0)
"#,
                sleep_line = sleep_line
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions).unwrap();
    }

    fn run_config(
        pi_bin: &str,
        session_id: &str,
        thread_id: &str,
        run_id: &str,
        cwd: &Path,
        prompt: &str,
    ) -> PiPrintRunConfig {
        PiPrintRunConfig {
            session_id: session_id.to_string(),
            thread_id: thread_id.to_string(),
            turn_id: None,
            run_id: run_id.to_string(),
            client_request_id: Some(format!("canary-{run_id}")),
            cwd: cwd.to_path_buf(),
            pi_bin: pi_bin.to_string(),
            prompt: prompt.to_string(),
            provider: Some("openrouter".to_string()),
            model: Some("deepseek/deepseek-v4-flash-latest".to_string()),
            session_dir: Some(cwd.join("pi-sessions")),
            resume_thread_id: None,
            resume_session_file: None,
            permission_mode: "provider_local".to_string(),
            machine_name: "pi-console-canary".to_string(),
            local_db_path: None,
        }
    }

    #[tokio::test]
    async fn fake_pi_completes_binds_and_wakes_daemon() {
        use tokio::io::AsyncReadExt;
        use tokio::net::UnixListener;

        let _home_guard = crate::console_adapter::longhouse_home_test_guard().await;
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let agent_dir = temp.path().join("longhouse").join("agent");
        std::fs::create_dir_all(&agent_dir).unwrap();
        let socket_path = agent_dir.join("transcript-wake.sock");
        let listener = UnixListener::bind(&socket_path).unwrap();

        let fake_pi = temp.path().join("pi");
        write_fake_pi(&fake_pi, 0);

        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        assert!(matches!(
            crate::turn_claims::default_registry()
                .unwrap()
                .claim(
                    &run_id,
                    &session_id,
                    &thread_id,
                    None,
                    Some(&format!("canary-{run_id}")),
                    "pi",
                )
                .unwrap(),
            crate::turn_claims::ClaimOutcome::Acquired
        ));
        let summary = start_pi_print_turn(run_config(
            fake_pi.to_str().unwrap(),
            &session_id,
            &thread_id,
            &run_id,
            temp.path(),
            "Do one bounded turn",
        ))
        .await
        .unwrap();

        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let claim = loop {
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&summary.run_id)
                .unwrap();
            if claim.state == "terminal" {
                break claim;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "Pi canary timed out; stderr={}",
                std::fs::read_to_string(&summary.stderr_path).unwrap_or_default()
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        };
        assert_eq!(
            claim.result.as_ref().unwrap()["terminal_state"],
            "run_completed",
            "stdout={}\nstderr={}",
            std::fs::read_to_string(&summary.stdout_path).unwrap_or_default(),
            std::fs::read_to_string(&summary.stderr_path).unwrap_or_default(),
        );
        let provider_session_id = claim.provider_thread_id.expect("provider binding recorded");
        assert!(std::fs::read_to_string(&summary.stderr_path)
            .unwrap()
            .is_empty());

        let (mut stream, _) = tokio::time::timeout(Duration::from_secs(1), listener.accept())
            .await
            .unwrap()
            .unwrap();
        let mut bytes = Vec::new();
        stream.read_to_end(&mut bytes).await.unwrap();
        let wake: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(wake["provider"], "pi");
        assert_eq!(wake["wake_reason"], "turn_completed");
        assert_eq!(wake["provider_turn_id"], provider_session_id);
        assert_eq!(wake["session_id"], session_id);

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

    #[tokio::test]
    async fn fake_pi_interrupt_settles_cancelled() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard().await;
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let agent_dir = temp.path().join("longhouse").join("agent");
        std::fs::create_dir_all(&agent_dir).unwrap();

        let fake_pi = temp.path().join("pi");
        write_fake_pi(&fake_pi, 60);

        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        assert!(matches!(
            crate::turn_claims::default_registry()
                .unwrap()
                .claim(
                    &run_id,
                    &session_id,
                    &thread_id,
                    None,
                    Some(&format!("canary-{run_id}")),
                    "pi",
                )
                .unwrap(),
            crate::turn_claims::ClaimOutcome::Acquired
        ));
        let summary = start_pi_print_turn(run_config(
            fake_pi.to_str().unwrap(),
            &session_id,
            &thread_id,
            &run_id,
            temp.path(),
            "Run long",
        ))
        .await
        .unwrap();
        interrupt_pi_print_turn(&run_id, &session_id).unwrap();

        let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
        loop {
            let claim = crate::turn_claims::default_registry()
                .unwrap()
                .read(&summary.run_id)
                .unwrap();
            if claim.state == "terminal" {
                assert_eq!(claim.result.unwrap()["terminal_state"], "run_cancelled");
                break;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "Pi interrupt did not settle"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        assert_ne!(unsafe { libc::killpg(summary.process_group_id, 0) }, 0);

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
