//! OMP Console turns through the stock `omp -p --mode json` surface.
//!
//! OMP is Pi-shaped, but its native archive and identity rules are separate.
//! This adapter therefore owns its exact reserved/resumed source path and uses
//! the OMP native session header as the only provider identity authority.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::Serialize;
use serde_json::{json, Value};
use tokio::process::{Child, Command};
use uuid::Uuid;

use crate::console_adapter::{claim_process_liveness, stderr_tail, ClaimLiveness};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;

pub const OMP_PRINT_ADAPTER: &str = "omp_print";
pub const DEFAULT_OMP_BIN: &str = "omp";

#[derive(Clone, Debug)]
pub struct OmpPrintRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub omp_bin: String,
    pub prompt: String,
    pub model: Option<String>,
    pub profile: Option<String>,
    pub session_dir: Option<PathBuf>,
    pub resume_provider_thread_id: Option<String>,
    pub resume_session_file: Option<PathBuf>,
    pub permission_mode: String,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct OmpPrintRunSummary {
    pub session_id: String,
    pub thread_id: String,
    pub run_id: String,
    pub provider_thread_id: Option<String>,
    pub launch_id: String,
    pub pid: u32,
    pub process_group_id: i32,
    pub stdout_path: String,
    pub stderr_path: String,
    pub session_dir: String,
    pub session_file: String,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct OmpPrintSink {
    session_id: String,
    thread_id: String,
    turn_id: Option<String>,
    run_id: String,
    client_request_id: Option<String>,
    launch_id: String,
    process_group_id: Option<i32>,
    stdout_path: PathBuf,
    session_dir: PathBuf,
    session_file: PathBuf,
    provider_thread_id: Option<String>,
    source_start_len: u64,
    binding_emitted: bool,
    machine_name: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

pub async fn start_omp_print_turn(config: OmpPrintRunConfig) -> Result<OmpPrintRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = config.turn_id.as_deref() {
        validate_uuid(turn_id, "turn_id")?;
    }
    if config.permission_mode != "provider_local" {
        anyhow::bail!("OMP Console currently supports provider_local permission mode only");
    }

    let session_dir = config
        .session_dir
        .clone()
        .or_else(|| {
            crate::omp_session::session_dir_for_launch(&config.cwd, config.profile.as_deref()).ok()
        })
        .context("OMP has no session directory")?;
    std::fs::create_dir_all(&session_dir)?;
    set_private_dir(&session_dir)?;

    let expected_provider_thread_id = config
        .resume_provider_thread_id
        .clone()
        .filter(|value| !value.trim().is_empty());
    let session_file = if let Some(path) = config.resume_session_file.clone() {
        let expected = expected_provider_thread_id
            .as_deref()
            .context("OMP exact resume requires a native session id")?;
        crate::omp_session::verify_exact_session_file(
            &path,
            expected,
            Some(&config.cwd.display().to_string()),
        )?;
        path
    } else {
        anyhow::ensure!(
            expected_provider_thread_id.is_none(),
            "OMP native resume id has no exact session file"
        );
        crate::omp_session::reserve_session_path(&session_dir)?
    };
    let source_start_len = std::fs::metadata(&session_file)
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    let local_db_path = config
        .local_db_path
        .clone()
        .or_else(|| crate::config::get_agent_db_path().ok());
    if let Some(db_path) = local_db_path.as_deref() {
        let conn = crate::state::db::open_client_connection(db_path, Duration::from_millis(500))?;
        crate::omp_session::reserve_source_for_thread(
            &conn,
            &session_file,
            &config.session_id,
            expected_provider_thread_id.as_deref(),
        )?;
    }

    let launch_id = Uuid::new_v4().to_string();
    let run_dir = crate::config::get_agent_dir()?
        .join("omp-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    let stdout_path = run_dir.join("stdout.log");
    let stderr_path = run_dir.join("stderr.log");
    let stdout_file = private_output_file(&stdout_path)?;
    let stderr_file = private_output_file(&stderr_path)?;
    let runtime_events_outbox_dir = crate::config::get_agent_runtime_events_outbox_dir()?;
    let args = build_omp_args(
        &config.prompt,
        config.model.as_deref(),
        config.profile.as_deref(),
        &session_dir,
        &session_file,
    );
    let argv = std::iter::once(config.omp_bin.clone())
        .chain(args.iter().cloned())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.omp_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .env_remove("PI_CONFIG_DIR")
        .env_remove("PI_CODING_AGENT_DIR")
        .env_remove("PI_CODING_AGENT_SESSION_DIR")
        .env_remove("PI_PROFILE");
    ManagedIdentity::new(ManagedProvider::Omp, &config.session_id)
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
    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{}` -p", config.omp_bin))?;
    let pid = child.id().context("omp -p returned no pid")?;
    let process_group_id = i32::try_from(pid).context("OMP pid exceeds process-group range")?;
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "omp",
        "transport": OMP_PRINT_ADAPTER,
        "provider_thread_id": expected_provider_thread_id,
        "source_start_len": source_start_len,
        "launch_id": launch_id,
        "pid": pid,
        "process_group_id": process_group_id,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "session_dir": session_dir,
        "session_file": session_file,
        "cwd": config.cwd,
        "machine_name": config.machine_name,
        "argv": argv,
    });
    if let Err(error) = crate::turn_claims::default_registry()?.mark_spawned_invocation(
        &config.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        OMP_PRINT_ADAPTER,
        &launch_id,
        expected_provider_thread_id.as_deref(),
        &stdout_path.to_string_lossy(),
        &stderr_path.to_string_lossy(),
        result,
    ) {
        cleanup_process_group(Some(process_group_id)).await;
        let _ = child.kill().await;
        return Err(error).context("persisting OMP Console spawn identity");
    }

    let sink = OmpPrintSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        stdout_path: stdout_path.clone(),
        session_dir: session_dir.clone(),
        session_file: session_file.clone(),
        provider_thread_id: expected_provider_thread_id.clone(),
        source_start_len,
        binding_emitted: false,
        machine_name: config.machine_name.clone(),
        local_db_path,
        runtime_events_outbox_dir,
    };
    let monitor_stderr_path = stderr_path.clone();
    tokio::spawn(async move {
        monitor_omp_print(&mut child, &monitor_stderr_path, sink).await;
    });
    let provider_thread_id = {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
        loop {
            let claim = crate::turn_claims::default_registry()?.read(&config.run_id)?;
            if claim.provider_identity_confirmed {
                break claim.provider_thread_id;
            }
            if claim.state == "terminal" || tokio::time::Instant::now() >= deadline {
                break None;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
    };
    let Some(provider_thread_id) = provider_thread_id else {
        cleanup_process_group(Some(process_group_id)).await;
        let _ = crate::turn_claims::default_registry()?.mark_failed(
            &config.run_id,
            "OMP native session identity was not confirmed before launch acknowledgment",
        );
        anyhow::bail!("OMP native session identity was not confirmed before launch acknowledgment");
    };

    Ok(OmpPrintRunSummary {
        session_id: config.session_id,
        thread_id: config.thread_id,
        run_id: config.run_id,
        provider_thread_id: Some(provider_thread_id),
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        session_dir: session_dir.to_string_lossy().to_string(),
        session_file: session_file.to_string_lossy().to_string(),
        argv,
    })
}

pub async fn recover_omp_print_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let inventory = crate::process_identity::try_collect_process_facts_by_pid();
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        if claim.adapter.as_deref() != Some(OMP_PRINT_ADAPTER) || claim.state != "spawned" {
            continue;
        }
        let Some(stdout_path) = claim.stdout_path.as_deref().map(PathBuf::from) else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("OMP Console claim has no stdout path".into()),
            );
            continue;
        };
        let stderr_path = claim
            .stderr_path
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| stdout_path.with_file_name("stderr.log"));
        let session_file = claim
            .result
            .as_ref()
            .and_then(|result| result.get("session_file"))
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .or_else(|| claim.source_path.as_deref().map(PathBuf::from));
        let Some(session_file) = session_file else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("OMP Console claim has no exact session file".into()),
            );
            continue;
        };
        let provider_thread_id = claim.provider_thread_id.clone().or_else(|| {
            crate::omp_session::read_session_header(&session_file)
                .ok()
                .map(|header| header.native_id)
        });
        let session_dir = claim
            .result
            .as_ref()
            .and_then(|result| result.get("session_dir").and_then(Value::as_str))
            .map(PathBuf::from)
            .or_else(|| session_file.parent().map(Path::to_path_buf))
            .unwrap_or_else(|| PathBuf::from("."));
        let source_start_len = claim
            .result
            .as_ref()
            .and_then(|result| result.get("source_start_len"))
            .and_then(Value::as_u64)
            .unwrap_or(0);
        let sink = OmpPrintSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            stdout_path: stdout_path.clone(),
            session_dir,
            session_file,
            provider_thread_id,
            source_start_len,
            binding_emitted: false,
            machine_name: machine_name.to_string(),
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        match crate::console_adapter::claim_liveness(&claim, inventory.as_ref()) {
            ClaimLiveness::Live => {
                tokio::spawn(
                    async move { monitor_recovered_omp_claim(claim, stderr_path, sink).await },
                );
                recovered += 1;
            }
            ClaimLiveness::Gone => settle_recovered_dead_claim(&claim, &stderr_path, &sink).await,
            ClaimLiveness::Unknown => {
                tracing::warn!(run_id = %claim.run_id, "Process inventory unavailable; leaving OMP Console turn claim for a later scan")
            }
        }
    }
    Ok(recovered)
}

pub async fn interrupt_omp_print_turn(run_id: &str, session_id: &str) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id || claim.provider != "omp" {
        anyhow::bail!("OMP Console turn claim does not match the requested session");
    }
    if claim.adapter.as_deref() != Some(OMP_PRINT_ADAPTER) || claim.state != "spawned" {
        anyhow::bail!("OMP Console turn is not active");
    }
    let pid = claim.pid.context("OMP Console turn has no provider pid")?;
    let expected_start = claim
        .process_start_time
        .as_deref()
        .context("OMP Console turn has no process-start identity")?;
    let actual = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .cloned()
        .context("OMP Console provider process is gone")?;
    if actual.lstart != expected_start {
        anyhow::bail!("OMP Console provider pid identity changed");
    }
    let pgid = claim
        .process_group_id
        .context("OMP Console turn has no process-group identity")?;
    let actual_pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
    if actual_pgid != pgid {
        anyhow::bail!("OMP Console provider process-group identity changed");
    }
    registry.mark_cancel_requested(run_id)?;
    let result = unsafe { libc::killpg(pgid, libc::SIGINT) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting OMP Console process group");
        }
    }
    tokio::time::sleep(Duration::from_millis(750)).await;
    if !cleanup_process_group(Some(pgid)).await {
        anyhow::bail!("OMP Console process-group cleanup was not verified");
    }
    Ok(())
}

async fn monitor_omp_print(child: &mut Child, stderr_path: &Path, mut sink: OmpPrintSink) {
    let mut projection = OmpStreamProjection::default();
    let mut offset = 0_u64;
    let mut pending = Vec::new();
    let mut seq = 0_u64;
    sink.post_phase("thinking", None, 0).await;
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
            let cleanup_verified = cleanup_process_group(sink.process_group_id).await;
            let reason = if cleanup_verified {
                error.to_string()
            } else {
                format!("OMP owned process-group cleanup was not verified: {error}")
            };
            sink.post_terminal("run_failed", None, Some(reason)).await;
            return;
        }
        match child.try_wait() {
            Ok(Some(status)) => {
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
                let source_drained = sink.source_is_drained(&projection).unwrap_or(false);
                if source_bound {
                    sink.wake_transcript_shipper().await;
                }
                let cleanup_verified = cleanup_process_group(sink.process_group_id).await;
                let (terminal_state, reason) = if cleanup_verified {
                    terminal_state_for_projection(
                        &projection,
                        Some(status.success()),
                        cancel_requested,
                        drain_error.as_deref(),
                        pending.is_empty(),
                        source_bound,
                        source_drained,
                    )
                } else {
                    (
                        "run_failed",
                        Some("OMP owned process-group cleanup was not verified".to_string()),
                    )
                };
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
                let cleanup_verified = cleanup_process_group(sink.process_group_id).await;
                let reason = if cleanup_verified {
                    error.to_string()
                } else {
                    format!("OMP owned process-group cleanup was not verified: {error}")
                };
                sink.post_terminal("run_failed", None, Some(reason)).await;
                return;
            }
        }
    }
}

async fn monitor_recovered_omp_claim(
    claim: crate::turn_claims::TurnClaim,
    stderr_path: PathBuf,
    mut sink: OmpPrintSink,
) {
    let mut projection = replay_projection(
        &sink.stdout_path,
        claim.projected_stdout_offset,
        sink.provider_thread_id.as_deref(),
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
            let cleanup_verified =
                cleanup_recovered_process_group(&claim, sink.process_group_id).await;
            let reason = if cleanup_verified {
                error.to_string()
            } else {
                format!("OMP recovered process-group cleanup was not verified: {error}")
            };
            sink.post_terminal("run_failed", None, Some(reason)).await;
            return;
        }
        if claim_process_liveness(&claim) == ClaimLiveness::Gone {
            let cancel_requested = crate::turn_claims::default_registry()
                .and_then(|registry| registry.read(&claim.run_id))
                .ok()
                .and_then(|current| current.cancel_requested_at)
                .is_some();
            let source_bound = sink.ensure_transcript_binding().await.unwrap_or(false);
            let source_drained = sink.source_is_drained(&projection).unwrap_or(false);
            if source_bound {
                sink.wake_transcript_shipper().await;
            }
            let cleanup_verified =
                cleanup_recovered_process_group(&claim, sink.process_group_id).await;
            let (terminal_state, reason) = if cleanup_verified {
                terminal_state_for_projection(
                    &projection,
                    None,
                    cancel_requested,
                    None,
                    pending.is_empty(),
                    source_bound,
                    source_drained,
                )
            } else {
                (
                    "run_failed",
                    Some("OMP recovered process-group cleanup was not verified".to_string()),
                )
            };
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
    sink: &OmpPrintSink,
) {
    let mut sink = sink.clone();
    let mut projection = replay_projection(
        &sink.stdout_path,
        claim.projected_stdout_offset,
        sink.provider_thread_id.as_deref(),
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
    let source_drained = sink.source_is_drained(&projection).unwrap_or(false);
    if source_bound {
        sink.wake_transcript_shipper().await;
    }
    let cleanup_verified = cleanup_recovered_process_group(claim, sink.process_group_id).await;
    let cancel_requested = crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(&claim.run_id))
        .ok()
        .and_then(|current| current.cancel_requested_at)
        .or(claim.cancel_requested_at.clone())
        .is_some();
    let (terminal_state, reason) = if cleanup_verified {
        terminal_state_for_projection(
            &projection,
            None,
            cancel_requested,
            stream_error.as_deref(),
            pending.is_empty(),
            source_bound,
            source_drained,
        )
    } else {
        (
            "run_failed",
            Some("OMP recovered process-group cleanup was not verified".to_string()),
        )
    };
    let terminal_reason = if terminal_state == "run_failed" {
        reason.or_else(|| stderr_tail(stderr_path))
    } else {
        reason
    };
    sink.post_terminal(terminal_state, None, terminal_reason)
        .await;
}

pub fn build_omp_args(
    prompt: &str,
    model: Option<&str>,
    profile: Option<&str>,
    session_dir: &Path,
    session_file: &Path,
) -> Vec<String> {
    let mut args = vec![
        "--mode".into(),
        "json".into(),
        "--session-dir".into(),
        session_dir.to_string_lossy().into_owned(),
        "--resume".into(),
        session_file.to_string_lossy().into_owned(),
    ];
    if let Some(profile) = profile.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--profile".into(), profile.into()]);
    }
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--model".into(), model.into()]);
    }
    args.extend(["-p".into(), "--".into(), prompt.into()]);
    args
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct OmpStreamProjection {
    identity_confirmed: bool,
    provider_thread_id: Option<String>,
    assistant_message_index: u64,
    final_assistant_id: Option<String>,
    current_assistant: Option<String>,
    final_stop_reason: Option<String>,
    turn_settled: bool,
    native_error: Option<String>,
}

impl OmpStreamProjection {
    fn apply(&mut self, expected_provider_thread_id: Option<&str>, event: &Value) -> Result<()> {
        match event.get("type").and_then(Value::as_str) {
            Some("session") => {
                let observed = event
                    .get("id")
                    .and_then(Value::as_str)
                    .context("OMP JSON stream session header has no id")?;
                anyhow::ensure!(expected_provider_thread_id.is_none_or(|expected| expected == observed), "OMP JSON stream session id {observed} does not match the exact resume identity");
                self.provider_thread_id = Some(observed.to_string());
                self.identity_confirmed = true;
            }
            Some("agent_start") => {
                self.turn_settled = false;
                self.final_assistant_id = None;
                self.final_stop_reason = None;
                self.native_error = None;
            }
            Some("message_start") => {
                if event
                    .get("message")
                    .and_then(|message| message.get("role"))
                    .and_then(Value::as_str)
                    == Some("assistant")
                {
                    self.current_assistant =
                        Some(message_text(event.get("message").unwrap_or(&Value::Null)));
                    self.assistant_message_index += 1;
                    self.final_assistant_id = None;
                    self.final_stop_reason = None;
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
                            current.push_str(delta);
                        }
                    }
                }
            }
            Some("message_end") => {
                let message = event
                    .get("message")
                    .context("OMP message_end has no message")?;
                if message.get("role").and_then(Value::as_str) == Some("assistant") {
                    self.current_assistant = Some(message_text(message));
                    self.final_assistant_id =
                        message.get("id").and_then(Value::as_str).map(str::to_owned);
                    self.final_stop_reason = message
                        .get("stopReason")
                        .and_then(Value::as_str)
                        .map(str::to_string);
                }
            }
            Some("agent_settled" | "session_stop" | "turn_end") => {}
            Some("agent_end") => {
                let is_terminal = event
                    .get("isTerminal")
                    .and_then(Value::as_bool)
                    .unwrap_or_else(|| {
                        event
                            .get("willContinue")
                            .and_then(Value::as_bool)
                            .map(|value| !value)
                            .unwrap_or(false)
                    });
                if is_terminal {
                    self.turn_settled = true;
                }
            }
            Some("error") => {
                self.native_error = event
                    .get("message")
                    .and_then(Value::as_str)
                    .or_else(|| event.get("errorMessage").and_then(Value::as_str))
                    .map(str::to_string)
            }
            _ => {}
        }
        Ok(())
    }
    fn live_text(&self) -> Option<&str> {
        self.current_assistant.as_deref()
    }
}

fn message_text(message: &Value) -> String {
    match message.get("content") {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Array(content)) => content
            .iter()
            .filter(|block| block.get("type").and_then(Value::as_str) == Some("text"))
            .filter_map(|block| block.get("text").and_then(Value::as_str))
            .collect(),
        _ => String::new(),
    }
}

fn terminal_state_for_projection(
    projection: &OmpStreamProjection,
    process_succeeded: Option<bool>,
    cancel_requested: bool,
    stream_error: Option<&str>,
    output_drained: bool,
    source_bound: bool,
    source_drained: bool,
) -> (&'static str, Option<String>) {
    if cancel_requested {
        return ("run_cancelled", None);
    }
    if process_succeeded == Some(false) {
        return (
            "run_failed",
            Some("OMP process exited unsuccessfully".into()),
        );
    }
    if let Some(error) = stream_error.or(projection.native_error.as_deref()) {
        return ("run_failed", Some(error.into()));
    }
    if !output_drained {
        return (
            "run_failed",
            Some("OMP JSON stream ended with an incomplete event".into()),
        );
    }
    if !source_drained {
        return (
            "run_failed",
            Some("OMP native session source did not drain completely".into()),
        );
    }
    if !source_bound {
        return (
            "run_failed",
            Some("OMP completed without an exact native session file".into()),
        );
    }
    if !projection.identity_confirmed {
        return (
            "run_failed",
            Some("OMP JSON stream never confirmed its native session identity".into()),
        );
    }
    if !projection.turn_settled {
        return (
            "run_failed",
            Some("OMP exited before its native turn settled".into()),
        );
    }
    if projection
        .current_assistant
        .as_deref()
        .map(str::trim)
        .unwrap_or_default()
        .is_empty()
    {
        return (
            "run_failed",
            Some("OMP settled without a final assistant message".into()),
        );
    }
    if !matches!(
        projection.final_stop_reason.as_deref(),
        Some("stop" | "length")
    ) {
        return (
            "run_failed",
            Some("OMP settled without a successful assistant stop reason".into()),
        );
    }
    ("run_completed", None)
}

async fn publish_stdout_growth(
    sink: &mut OmpPrintSink,
    projection: &mut OmpStreamProjection,
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
                anyhow::bail!("OMP JSON stream record {seq} is malformed: {error}");
            }
        };
        projection.apply(sink.provider_thread_id.as_deref(), &event)?;
        if sink.provider_thread_id.is_none() {
            sink.provider_thread_id = projection.provider_thread_id.clone();
        }
        if let Some((phase, tool)) = omp_phase_from_event(&event) {
            sink.post_phase(phase, tool, *seq).await;
        }
        sink.post_stream_event(*seq, &event, projection).await;
    }
    let _ = sink.ensure_transcript_binding().await?;
    if had_lines {
        if let Ok(registry) = crate::turn_claims::default_registry() {
            let _ = registry.mark_projection_checkpoint(
                &sink.run_id,
                offset.saturating_sub(pending.len() as u64),
                *seq,
            );
        }
    }
    Ok(())
}

fn omp_phase_from_event(event: &Value) -> Option<(&'static str, Option<String>)> {
    match event.get("type").and_then(Value::as_str) {
        Some("tool_execution_start") => Some((
            "running",
            event
                .get("toolName")
                .and_then(Value::as_str)
                .map(str::to_string),
        )),
        Some("tool_execution_end") => Some(("thinking", None)),
        Some("agent_end")
            if event
                .get("isTerminal")
                .and_then(Value::as_bool)
                .unwrap_or_else(|| {
                    event
                        .get("willContinue")
                        .and_then(Value::as_bool)
                        .map(|value| !value)
                        .unwrap_or(false)
                }) =>
        {
            Some(("idle", None))
        }
        _ => None,
    }
}

fn replay_projection(path: &Path, limit: u64, expected: Option<&str>) -> OmpStreamProjection {
    let mut projection = OmpStreamProjection::default();
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
            let _ = projection.apply(expected, &event);
        }
    }
    projection
}

impl OmpPrintSink {
    async fn ensure_transcript_binding(&mut self) -> Result<bool> {
        if self.binding_emitted {
            return Ok(self.provider_thread_id.is_some());
        }
        let header = match crate::omp_session::read_session_header(&self.session_file) {
            Ok(header) => header,
            Err(_) => return Ok(false),
        };
        if let Some(expected) = self.provider_thread_id.as_deref() {
            anyhow::ensure!(
                header.native_id == expected,
                "OMP native session header changed identity after launch"
            );
        }
        if let Some(stream_id) = self.provider_thread_id.as_deref() {
            anyhow::ensure!(
                stream_id == header.native_id,
                "OMP stream and native source identities disagree"
            );
        }
        self.provider_thread_id = Some(header.native_id.clone());
        if let Some(db_path) = self.local_db_path.as_deref() {
            let conn =
                crate::state::db::open_client_connection(db_path, Duration::from_millis(500))?;
            crate::omp_session::bind_source_for_thread(
                &conn,
                &self.session_file,
                &self.session_id,
                &header.native_id,
            )?;
        }
        crate::turn_claims::default_registry()?.mark_provider_binding(
            &self.run_id,
            &header.native_id,
            Some(&self.session_file.to_string_lossy()),
        )?;
        self.post_binding(&header.native_id).await;
        self.binding_emitted = true;
        Ok(true)
    }

    fn source_is_drained(&self, projection: &OmpStreamProjection) -> Result<bool> {
        let bytes = std::fs::read(&self.session_file)?;
        if bytes.is_empty()
            || !bytes.ends_with(b"\n")
            || bytes.len() as u64 <= self.source_start_len
        {
            return Ok(false);
        }
        for line in bytes
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
        {
            serde_json::from_slice::<Value>(line)
                .context("OMP native source contains an incomplete record")?;
        }
        let header = crate::omp_session::read_session_header(&self.session_file)?;
        if self.provider_thread_id.as_deref() != Some(header.native_id.as_str()) {
            return Ok(false);
        }
        let start = (self.source_start_len as usize).min(bytes.len());
        let mut last_assistant: Option<Value> = None;
        for line in bytes[start..]
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
        {
            let record: Value = serde_json::from_slice(line)?;
            if record.get("type").and_then(Value::as_str) != Some("message")
                || record.pointer("/message/role").and_then(Value::as_str) != Some("assistant")
            {
                continue;
            }
            last_assistant = Some(record);
        }
        let Some(last_assistant) = last_assistant else {
            return Ok(false);
        };
        let native_id = last_assistant.get("id").and_then(Value::as_str);
        let native_text = last_assistant
            .pointer("/message")
            .map(message_text)
            .unwrap_or_default();
        let native_stop_reason = last_assistant
            .pointer("/message/stopReason")
            .and_then(Value::as_str);
        let id_matches = projection
            .final_assistant_id
            .as_deref()
            .is_none_or(|expected| native_id == Some(expected));
        Ok(id_matches
            && !native_text.trim().is_empty()
            && projection.current_assistant.as_deref() == Some(native_text.as_str())
            && matches!(native_stop_reason, Some("stop" | "length")))
    }

    async fn post_binding(&self, provider_thread_id: &str) {
        self.post_events(vec![json!({"runtime_key": format!("omp:{}", self.session_id), "session_id": self.session_id, "thread_id": self.thread_id, "run_id": self.run_id, "provider": "omp", "device_id": self.machine_name, "source": OMP_PRINT_ADAPTER, "kind": "binding_signal", "occurred_at": Utc::now().to_rfc3339(), "dedupe_key": format!("omp-print:{}:{}:binding", self.session_id, self.launch_id), "payload": {"provider_session_id": provider_thread_id, "source_path": self.session_file.to_string_lossy(), "managed_transport": OMP_PRINT_ADAPTER, "execution_lifetime": "one_shot"}})]).await;
    }
    async fn post_phase(&self, phase: &str, tool_name: Option<String>, activity_seq: u64) {
        self.persist_local_phase(phase, tool_name.clone(), Utc::now());
        self.post_events(vec![json!({"runtime_key": format!("omp:{}", self.session_id), "session_id": self.session_id, "thread_id": self.thread_id, "run_id": self.run_id, "provider": "omp", "device_id": self.machine_name, "source": OMP_PRINT_ADAPTER, "kind": "phase_signal", "phase": phase, "tool_name": tool_name, "occurred_at": Utc::now().to_rfc3339(), "dedupe_key": format!("omp-print:{}:{}:phase:{phase}:{activity_seq}", self.session_id, self.run_id), "payload": {"managed_transport": OMP_PRINT_ADAPTER, "execution_lifetime": "one_shot"}})]).await;
    }
    async fn post_stream_event(&self, seq: u64, event: &Value, projection: &OmpStreamProjection) {
        self.post_events(vec![json!({
            "runtime_key": format!("omp:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "omp",
            "device_id": self.machine_name,
            "source": OMP_PRINT_ADAPTER,
            "kind": "progress_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!("omp-print:{}:{}:stdout:{seq}", self.session_id, self.run_id),
            "payload": {
                "progress_kind": "omp_print_stream",
                "seq": seq,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": self.provider_thread_id,
                "assistant_message_index": projection.assistant_message_index,
                "event": event,
                "live_text": projection.live_text(),
                "managed_transport": OMP_PRINT_ADAPTER,
                "execution_lifetime": "one_shot",
            },
        })])
        .await;
    }
    async fn post_decode_gap(&self, seq: u64, error: &str) {
        self.post_events(vec![json!({"runtime_key": format!("omp:{}", self.session_id), "session_id": self.session_id, "thread_id": self.thread_id, "run_id": self.run_id, "provider": "omp", "device_id": self.machine_name, "source": OMP_PRINT_ADAPTER, "kind": "progress_signal", "occurred_at": Utc::now().to_rfc3339(), "dedupe_key": format!("omp-print:{}:{}:decode-gap:{seq}", self.session_id, self.run_id), "payload": {"progress_kind": "omp_print_decode_gap", "seq": seq, "error": error, "managed_transport": OMP_PRINT_ADAPTER, "execution_lifetime": "one_shot"}})]).await;
    }
    async fn post_terminal(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr: Option<String>,
    ) {
        self.persist_local_phase("finished", None, Utc::now());
        self.post_events(vec![json!({"runtime_key": format!("omp:{}", self.session_id), "session_id": self.session_id, "thread_id": self.thread_id, "run_id": self.run_id, "provider": "omp", "device_id": self.machine_name, "source": OMP_PRINT_ADAPTER, "kind": "terminal_signal", "occurred_at": Utc::now().to_rfc3339(), "dedupe_key": format!("omp-print:{}:{}:terminal", self.session_id, self.run_id), "payload": {"managed_transport": OMP_PRINT_ADAPTER, "execution_lifetime": "one_shot", "terminal_state": terminal_state, "terminal_reason": terminal_state, "terminal_source": OMP_PRINT_ADAPTER, "exit_code": exit_code, "stderr_tail": stderr, "provider_thread_id": self.provider_thread_id, "source_path": self.session_file.to_string_lossy(), "turn_id": self.turn_id, "client_request_id": self.client_request_id}})]).await;
        crate::turn_claims::mark_terminal(
            &self.run_id,
            terminal_state,
            (terminal_state == "run_failed").then_some(stderr).flatten(),
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
        let Ok(conn) =
            crate::state::db::open_client_connection(db_path, Duration::from_millis(250))
        else {
            return;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "omp".into(),
            phase: phase.into(),
            tool_name,
            source: OMP_PRINT_ADAPTER.into(),
            observed_at,
        };
        let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
    }
    async fn wake_transcript_shipper(&self) {
        let Ok(socket_path) = crate::config::get_agent_transcript_wake_socket_path() else {
            return;
        };
        if !socket_path.exists() {
            return;
        }
        let payload = json!({"provider":"omp","path":self.session_file,"phase":"idle","session_id":self.session_id,"run_id":self.run_id,"turn_id":self.turn_id,"provider_turn_id":self.provider_thread_id,"client_request_id":self.client_request_id,"wake_reason":"turn_completed","observed_at_ms":Utc::now().timestamp_millis(),"file_len_hint":std::fs::metadata(&self.session_file).ok().map(|metadata| metadata.len())});
        let bytes = payload.to_string().into_bytes();
        let _ = tokio::task::spawn_blocking(move || {
            let mut stream = std::os::unix::net::UnixStream::connect(socket_path)?;
            stream.set_write_timeout(Some(Duration::from_millis(50)))?;
            stream.write_all(&bytes)
        })
        .await;
    }
    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            let _ = crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event);
        }
    }
}

async fn cleanup_process_group(process_group_id: Option<i32>) -> bool {
    let Some(pgid) = process_group_id else {
        return false;
    };
    crate::console_adapter::cleanup_process_group("omp-print", Some(pgid)).await;
    !crate::process_group::group_is_alive(pgid)
}

fn recovered_process_group_is_safe(claim: &crate::turn_claims::TurnClaim) -> bool {
    if !claim.process_group_is_from_this_boot() {
        return false;
    }
    let (Some(pid), Some(expected_start)) = (claim.pid, claim.process_start_time.as_deref()) else {
        return false;
    };
    match crate::process_identity::try_collect_process_fact(pid) {
        Some(fact) => fact.lstart == expected_start,
        None => false,
    }
}

async fn cleanup_recovered_process_group(
    claim: &crate::turn_claims::TurnClaim,
    process_group_id: Option<i32>,
) -> bool {
    let Some(pgid) = process_group_id else {
        return false;
    };
    if !crate::process_group::group_is_alive(pgid) {
        return true;
    }
    if !recovered_process_group_is_safe(claim) {
        return false;
    }
    cleanup_process_group(Some(pgid)).await
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
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))?;
    Ok(())
}
fn validate_uuid(value: &str, label: &str) -> Result<()> {
    Uuid::parse_str(value).with_context(|| format!("{label} must be a UUID"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stock_omp_args_keep_native_defaults_and_bind_exact_resume() {
        let args = build_omp_args(
            "reply",
            Some("gpt-5.2"),
            Some("work"),
            Path::new("/sessions"),
            Path::new("/sessions/exact.jsonl"),
        );
        assert_eq!(
            args,
            vec![
                "--mode",
                "json",
                "--session-dir",
                "/sessions",
                "--resume",
                "/sessions/exact.jsonl",
                "--profile",
                "work",
                "--model",
                "gpt-5.2",
                "-p",
                "--",
                "reply"
            ]
        );
        assert!(!args.iter().any(|arg| matches!(
            arg.as_str(),
            "--no-tools" | "--no-extensions" | "--no-skills"
        )));
    }

    #[test]
    fn leading_dash_console_prompt_is_after_literal_separator() {
        let args = build_omp_args(
            "--looks-like-an-option",
            None,
            None,
            Path::new("/sessions"),
            Path::new("/sessions/exact.jsonl"),
        );
        assert_eq!(args[args.len() - 2], "--");
        assert_eq!(
            args.last().map(String::as_str),
            Some("--looks-like-an-option")
        );
    }

    #[test]
    fn terminal_requires_native_agent_end_and_source_drain() {
        let mut projection = OmpStreamProjection::default();
        projection
            .apply(
                None,
                &json!({"type":"session","id":"01a08857-826d-72f6-b816-672b54116504"}),
            )
            .unwrap();
        projection
            .apply(None, &json!({"type":"agent_start"}))
            .unwrap();
        projection.apply(None, &json!({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"stopReason":"stop"}})).unwrap();
        projection
            .apply(None, &json!({"type":"agent_settled"}))
            .unwrap();
        projection
            .apply(None, &json!({"type":"session_stop"}))
            .unwrap();
        assert_eq!(
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, true).0,
            "run_failed"
        );
        projection
            .apply(
                None,
                &json!({"type":"agent_end","isTerminal":false,"willContinue":false}),
            )
            .unwrap();
        assert_eq!(
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, true).0,
            "run_failed"
        );
        projection
            .apply(
                None,
                &json!({"type":"agent_end","isTerminal":true,"willContinue":false}),
            )
            .unwrap();
        assert_eq!(
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, false)
                .0,
            "run_failed"
        );
        assert_eq!(
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, true).0,
            "run_completed"
        );
    }

    fn write_fake_omp(path: &Path) {
        std::fs::write(
            path,
            r##"#!/usr/bin/env python3
import json
import os
import sys
import uuid

args = sys.argv[1:]
source = args[args.index("--resume") + 1]
prompt = args[-1]
native_id = "01a08857-826d-72f6-b816-672b54116504"
header = {
    "type": "session",
    "version": 3,
    "id": native_id,
    "timestamp": "2026-09-09T22:43:51.533Z",
    "cwd": "/Users/davidrose/git/zerg/longhouse",
}
if os.path.getsize(source) == 0:
    with open(source, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(header, separators=(",", ":")) + "\n")
with open(source, "a", encoding="utf-8") as stream:
    stream.write(json.dumps({"type":"message","id":str(uuid.uuid4()),"message":{"role":"user","content":[{"type":"text","text":prompt}]}}) + "\n")
    stream.write(json.dumps({"type":"message","id":str(uuid.uuid4()),"message":{"role":"assistant","content":[{"type":"text","text":prompt}],"stopReason":"stop"}}) + "\n")
events = [
    header,
    {"type":"agent_start"},
    {"type":"message_start","message":{"role":"assistant","content":[]}},
    {"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":prompt}},
    {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":prompt}],"stopReason":"stop"}},
    {"type":"agent_end","isTerminal":True,"willContinue":False},
]
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
"##,
        )
        .unwrap();
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions).unwrap();
    }

    async fn wait_for_terminal(run_id: &str) -> crate::turn_claims::TurnClaim {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
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
                "OMP Console fake turn did not settle"
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }

    #[tokio::test]
    async fn fake_stock_omp_completes_and_continues_through_exact_native_file() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard().await;
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let fake_omp = temp.path().join("omp");
        write_fake_omp(&fake_omp);
        let session_dir = temp.path().join("omp-sessions");
        let local_db_path = temp.path().join("agent.db");
        let cwd = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let first_run = Uuid::new_v4().to_string();
        let second_run = Uuid::new_v4().to_string();
        let registry = crate::turn_claims::default_registry().unwrap();
        registry
            .claim(&first_run, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        let first = start_omp_print_turn(OmpPrintRunConfig {
            session_id: session_id.clone(),
            thread_id: thread_id.clone(),
            turn_id: None,
            run_id: first_run.clone(),
            client_request_id: Some("omp-first".into()),
            cwd: cwd.clone(),
            omp_bin: fake_omp.to_string_lossy().into_owned(),
            prompt: "--OMP_FIRST".into(),
            model: Some("gpt-5.2".into()),
            profile: Some("work".into()),
            session_dir: Some(session_dir.clone()),
            resume_provider_thread_id: None,
            resume_session_file: None,
            permission_mode: "provider_local".into(),
            machine_name: "omp-test".into(),
            local_db_path: Some(local_db_path.clone()),
        })
        .await
        .unwrap();
        let first_claim = wait_for_terminal(&first_run).await;
        assert_eq!(
            first_claim.result.as_ref().unwrap()["terminal_state"],
            "run_completed"
        );
        let native_id = first_claim.provider_thread_id.clone().unwrap();
        assert_eq!(native_id, "01a08857-826d-72f6-b816-672b54116504");
        let conn =
            crate::state::db::open_client_connection(&local_db_path, Duration::from_millis(500))
                .unwrap();
        let binding = crate::state::session_binding::SessionBinding::new(&conn)
            .get_with_thread_for_provider(
                &crate::storage_v2_shipper::stable_source_path(Path::new(&first.session_file))
                    .display()
                    .to_string(),
                "omp",
            )
            .unwrap();
        assert_eq!(binding.map(|(session, _)| session), Some(first.session_id));
        assert!(first
            .argv
            .windows(2)
            .any(|pair| pair == ["--", "--OMP_FIRST"]));
        assert_ne!(unsafe { libc::killpg(first.process_group_id, 0) }, 0);

        registry
            .claim(&second_run, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        let second = start_omp_print_turn(OmpPrintRunConfig {
            session_id,
            thread_id,
            turn_id: None,
            run_id: second_run.clone(),
            client_request_id: Some("omp-second".into()),
            cwd,
            omp_bin: fake_omp.to_string_lossy().into_owned(),
            prompt: "OMP_SECOND".into(),
            model: Some("gpt-5.2".into()),
            profile: Some("work".into()),
            session_dir: Some(session_dir.clone()),
            resume_provider_thread_id: Some(native_id),
            resume_session_file: Some(PathBuf::from(&first.session_file)),
            permission_mode: "provider_local".into(),
            machine_name: "omp-test".into(),
            local_db_path: Some(local_db_path),
        })
        .await
        .unwrap();
        let second_claim = wait_for_terminal(&second_run).await;
        assert_eq!(
            second_claim.result.as_ref().unwrap()["terminal_state"],
            "run_completed"
        );
        assert_eq!(
            second.provider_thread_id.as_deref(),
            Some("01a08857-826d-72f6-b816-672b54116504")
        );
        assert!(second
            .argv
            .windows(2)
            .any(|pair| pair[0] == "--resume" && pair[1] == first.session_file));
        let source = std::fs::read_to_string(&first.session_file).unwrap();
        assert_eq!(source.matches("OMP_FIRST").count(), 2);
        assert_eq!(source.matches("OMP_SECOND").count(), 2);

        if let Some(home) = previous_home {
            unsafe {
                std::env::set_var("LONGHOUSE_HOME", home);
            }
        } else {
            unsafe {
                std::env::remove_var("LONGHOUSE_HOME");
            }
        }
    }
}
