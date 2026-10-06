//! OMP Console turns through the stock `omp -p --mode json` surface.
//!
//! OMP is Pi-shaped, but its native archive and identity rules are separate.
//! This adapter therefore owns its exact reserved/resumed source path and uses
//! the OMP native session header as the only provider identity authority.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, Instant};

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
const TERMINAL_DRAIN_GRACE: Duration = Duration::from_secs(5);

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
    /// Staged image files, passed as `@<path>` messages after `--` so OMP
    /// attaches them as image content natively.
    pub image_paths: Vec<PathBuf>,
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
    crate::omp_session::ensure_session_dir_is_disjoint_from_pi(&config.cwd, &session_dir)?;
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
    // The Console path claims its source locally, exactly as Helm does: the
    // daemon projects the claim into the binding discovery reads, so a busy or
    // unreadable archive cannot fail a launch here either. `local_db_path` stays
    // for the phase outbox, which is not on the identity path.
    crate::managed_source_claim::reserve(
        &config.session_id,
        "omp",
        &session_file,
        &config.cwd,
        None,
        None,
    )
    .with_context(|| format!("claiming the OMP console source {}", session_file.display()))?;

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
        config.model.as_deref(),
        config.profile.as_deref(),
        &session_dir,
        &session_file,
        omp_supports_no_ui(&config.omp_bin),
    );
    // RPC stdin: see `console_rpc`. The prompt and a later steer are written
    // there; stdout still goes to the file the monitors tail.
    let rpc_stdin = run_dir.join(crate::console_rpc::RPC_STDIN);
    crate::console_rpc::create_fifo(&rpc_stdin)?;
    let prompt_command = crate::console_rpc::prompt_command(&config.prompt, &config.image_paths)?;
    let rpc_stdin_c = std::ffi::CString::new(rpc_stdin.as_os_str().as_bytes())?;
    let argv = std::iter::once(config.omp_bin.clone())
        .chain(args.iter().cloned())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.omp_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file));
    ManagedIdentity::new(ManagedProvider::Omp, &config.session_id)
        .with_run_id(&config.run_id)
        .apply(&mut command, &[]);
    #[cfg(unix)]
    unsafe {
        command.pre_exec(move || {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            crate::console_rpc::adopt_fifo_as_stdin(&rpc_stdin_c)
        });
    }
    let mut child = command
        .spawn()
        .with_context(|| format!("spawning `{}` --mode rpc", config.omp_bin))?;
    let pid = child.id().context("omp --mode rpc returned no pid")?;
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
    refresh_owned_processes(&config.run_id);

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
    // Nothing is written until OMP says `ready`: see `stdout_has_ready`.
    if let Err(error) = wait_for_rpc_ready(&mut child, &stdout_path, &stderr_path).await {
        let _ = cleanup_owned_child(&mut child, &config.run_id).await;
        let _ =
            crate::turn_claims::default_registry()?.mark_failed(&config.run_id, &error.to_string());
        return Err(error).context("waiting for OMP to become ready");
    }
    for command in [
        json!({"id": crate::console_rpc::RPC_IDENTITY_ID, "type": "get_state"}),
        prompt_command,
    ] {
        if let Err(error) = crate::console_rpc::write_command(&rpc_stdin, &command).await {
            let _ = cleanup_owned_child(&mut child, &config.run_id).await;
            let _ = crate::turn_claims::default_registry()?
                .mark_failed(&config.run_id, &error.to_string());
            return Err(error).context("sending the OMP Console prompt");
        }
    }
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
        let cleanup_verified = cleanup_live_claim(&config.run_id).await;
        let mut error = if cleanup_verified {
            "OMP native session identity was not confirmed before launch acknowledgment".to_string()
        } else {
            "OMP native session identity was not confirmed before launch acknowledgment; owned process-group cleanup was not verified".to_string()
        };
        // The identity timeout is only the symptom. When OMP died at once the
        // cause is in its stderr (on a fresh Linux machine it was
        // "/usr/bin/env: bun: No such file or directory"), so say it here
        // instead of leaving it in a file nobody opens.
        if let Some(cause) = stderr_tail(&stderr_path) {
            error.push_str(&format!("; omp stderr: {cause}"));
        }
        let _ = crate::turn_claims::default_registry()?.mark_failed(&config.run_id, &error);
        anyhow::bail!(error);
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

/// Wait for OMP's `ready` frame (see `console_rpc::stdout_has_ready`), failing
/// early when the process exits first so the stderr tail names the cause.
async fn wait_for_rpc_ready(
    child: &mut Child,
    stdout_path: &Path,
    stderr_path: &Path,
) -> Result<()> {
    let deadline = Instant::now() + crate::console_rpc::RPC_READY_DEADLINE;
    loop {
        if crate::console_rpc::stdout_has_ready(stdout_path) {
            return Ok(());
        }
        if let Some(status) = child.try_wait()? {
            let cause = stderr_tail(stderr_path).map(|tail| format!("; omp stderr: {tail}"));
            anyhow::bail!(
                "OMP exited ({status}) before reporting ready{}",
                cause.unwrap_or_default()
            );
        }
        if Instant::now() >= deadline {
            anyhow::bail!(
                "OMP did not report ready within {}s",
                crate::console_rpc::RPC_READY_DEADLINE.as_secs()
            );
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

/// Enter the running OMP Console turn with an RPC `steer`. OMP delivers it at
/// the next message boundary; a long-running command is moved to the
/// background rather than cancelled, and its result is collected later.
pub async fn steer_omp_print_turn(
    run_id: &str,
    session_id: &str,
    text: &str,
) -> std::result::Result<(), String> {
    let not_steerable = || "turn_not_steerable".to_string();
    let registry = crate::turn_claims::default_registry().map_err(|err| err.to_string())?;
    let claim = registry.read(run_id).map_err(|_| not_steerable())?;
    if claim.session_id != session_id
        || claim.provider != "omp"
        || claim.adapter.as_deref() != Some(OMP_PRINT_ADAPTER)
        || claim.state != "spawned"
        || crate::console_adapter::claim_process_liveness(&claim)
            != crate::console_adapter::ClaimLiveness::Live
    {
        return Err(not_steerable());
    }
    let stdout_path = PathBuf::from(claim.stdout_path.clone().ok_or_else(not_steerable)?);
    let fifo = stdout_path.with_file_name(crate::console_rpc::RPC_STDIN);
    crate::console_rpc::steer(&fifo, &stdout_path, text).await
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

pub async fn interrupt_omp_print_turn(
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
        || claim.provider != "omp"
    {
        anyhow::bail!(
            "OMP Console turn claim does not match the requested session, thread, or turn"
        );
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
    if actual_pgid != pgid || crate::process_group::leader_group_for(pid) != Some(pgid) {
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
    if !cleanup_live_claim(run_id).await {
        anyhow::bail!("OMP Console process-group cleanup was not verified");
    }
    Ok(())
}

async fn monitor_omp_print(child: &mut Child, stderr_path: &Path, mut sink: OmpPrintSink) {
    let mut projection = OmpStreamProjection::default();
    let mut offset = 0_u64;
    let mut pending = Vec::new();
    let mut seq = 0_u64;
    let mut terminal_drain_deadline = None;
    // An RPC process stays up after the run settles; Longhouse ends it, and
    // that exit is the run's successful end rather than a failure.
    let mut settled_shutdown = false;
    let prompt_written_at = Instant::now();
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
            let cleanup_verified = cleanup_owned_child(child, &sink.run_id).await;
            let reason = if cleanup_verified {
                error.to_string()
            } else {
                format!("OMP owned process-group cleanup was not verified: {error}")
            };
            sink.post_terminal("run_failed", None, Some(reason)).await;
            return;
        }
        if projection.turn_settled {
            terminal_drain_deadline.get_or_insert_with(|| Instant::now() + TERMINAL_DRAIN_GRACE);
        } else {
            terminal_drain_deadline = None;
        }
        if !settled_shutdown && (projection.turn_settled || projection.rpc_rejected) {
            settled_shutdown = projection.turn_settled;
            crate::console_adapter::cleanup_process_group("omp-print", sink.process_group_id).await;
        }
        refresh_owned_processes(&sink.run_id);
        if crate::console_rpc::prompt_ack_overdue(
            projection.prompt_acknowledged,
            projection.rpc_rejected,
            prompt_written_at.elapsed(),
        ) {
            // The process is alive and idle and nothing will follow. Say so now
            // instead of presenting a turn nobody is running as Thinking.
            let cleanup_verified = cleanup_owned_child(child, &sink.run_id).await;
            let reason = crate::console_rpc::prompt_ack_overdue_reason(prompt_written_at.elapsed());
            let reason = if cleanup_verified {
                reason
            } else {
                format!("{reason}; owned process-group cleanup was not verified")
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
                let cleanup_verified = cleanup_live_claim(&sink.run_id).await;
                let (terminal_state, reason) = if cleanup_verified {
                    terminal_state_for_projection(
                        &projection,
                        Some(status.success() || settled_shutdown),
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
            Ok(None) => {
                if terminal_drain_deadline.is_some_and(|deadline| Instant::now() >= deadline) {
                    let cleanup_verified = cleanup_owned_child(child, &sink.run_id).await;
                    let reason = if cleanup_verified {
                        format!(
                            "OMP terminal event did not release the provider process within {}s",
                            TERMINAL_DRAIN_GRACE.as_secs()
                        )
                    } else {
                        "OMP terminal drain expired and owned process-group cleanup was not verified"
                            .to_string()
                    };
                    sink.post_terminal("run_failed", None, Some(reason)).await;
                    return;
                }
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
            Err(error) => {
                let cleanup_verified = cleanup_owned_child(child, &sink.run_id).await;
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
    let mut terminal_drain_deadline = None;
    // The prompt was handed over when the claim was made, so an unacknowledged
    // one is as old as the claim. `thinking` is posted only for a turn the
    // provider accepted, or one still inside the time it has to accept: a
    // restart used to re-assert Thinking for a run that never began, for ever.
    // A claim time that cannot be read is unknown evidence, not a fresh claim:
    // it counts as overdue, so an unacknowledged prompt fails instead of reading
    // Thinking for ever. The engine writes the field itself, as RFC 3339.
    let claimed_age = || {
        crate::console_rpc::age_since_rfc3339(&claim.claimed_at)
            .unwrap_or(crate::console_rpc::PROMPT_ACK_DEADLINE)
    };
    let mut thinking_posted = false;
    if projection.prompt_acknowledged
        || !crate::console_rpc::prompt_ack_overdue(false, false, claimed_age())
    {
        sink.post_phase("thinking", None, seq).await;
        thinking_posted = true;
    }
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
                cleanup_recovered_process_group(&claim.run_id, &claim, sink.process_group_id).await;
            let reason = if cleanup_verified {
                error.to_string()
            } else {
                format!("OMP recovered process-group cleanup was not verified: {error}")
            };
            sink.post_terminal("run_failed", None, Some(reason)).await;
            return;
        }
        if !thinking_posted && projection.prompt_acknowledged {
            sink.post_phase("thinking", None, seq).await;
            thinking_posted = true;
        }
        if crate::console_rpc::prompt_ack_overdue(
            projection.prompt_acknowledged,
            projection.rpc_rejected,
            claimed_age(),
        ) {
            let cleanup_verified =
                cleanup_recovered_process_group(&claim.run_id, &claim, sink.process_group_id).await;
            let reason = crate::console_rpc::prompt_ack_overdue_reason(claimed_age());
            let reason = if cleanup_verified {
                reason
            } else {
                format!("{reason}; recovered process-group cleanup was not verified")
            };
            sink.post_terminal("run_failed", None, Some(reason)).await;
            return;
        }
        if (projection.turn_settled || projection.rpc_rejected)
            && claim.process_group_is_from_this_boot()
            && crate::console_adapter::claim_process_liveness(&claim)
                == crate::console_adapter::ClaimLiveness::Live
        {
            crate::console_adapter::cleanup_process_group("omp-print", sink.process_group_id).await;
        }
        if projection.turn_settled {
            terminal_drain_deadline.get_or_insert_with(|| Instant::now() + TERMINAL_DRAIN_GRACE);
        } else {
            terminal_drain_deadline = None;
        }
        refresh_owned_processes(&claim.run_id);
        let liveness = crate::turn_claims::default_registry()
            .and_then(|registry| registry.read(&claim.run_id))
            .map(|current| claim_process_liveness(&current))
            .unwrap_or(ClaimLiveness::Unknown);
        if liveness == ClaimLiveness::Gone {
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
                cleanup_recovered_process_group(&claim.run_id, &claim, sink.process_group_id).await;
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
        if liveness == ClaimLiveness::Live
            && terminal_drain_deadline.is_some_and(|deadline| Instant::now() >= deadline)
        {
            let cleanup_verified =
                cleanup_recovered_process_group(&claim.run_id, &claim, sink.process_group_id).await;
            let reason = if cleanup_verified {
                format!(
                    "OMP recovered terminal event did not release the provider process within {}s",
                    TERMINAL_DRAIN_GRACE.as_secs()
                )
            } else {
                "OMP recovered terminal drain expired and owned process-group cleanup was not verified"
                    .to_string()
            };
            sink.post_terminal("run_failed", None, Some(reason)).await;
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
    let cleanup_verified =
        cleanup_recovered_process_group(&claim.run_id, claim, sink.process_group_id).await;
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

/// `--no-ui` (headless extensions under `--mode rpc`) is newer than `--mode rpc`
/// itself: OMP 18.2.9 has the mode and rejects the flag ("unknown flag") before
/// it reads a prompt, so every Console turn on such a build died in seconds.
/// Ask the installed binary instead of assuming, and keep the answer for a few
/// minutes so a turn does not pay for a second process start. A binary that
/// cannot be asked is assumed current: the launch then fails loudly on its own.
fn omp_supports_no_ui(omp_bin: &str) -> bool {
    // no managed identity: this only runs `omp --help` to read the flag list. It
    // starts no agent and no session, so there is nothing for the overlay to claim.
    const TTL: Duration = Duration::from_secs(600);
    static CACHE: std::sync::OnceLock<
        std::sync::Mutex<std::collections::HashMap<String, (Instant, bool)>>,
    > = std::sync::OnceLock::new();
    let cache = CACHE.get_or_init(|| std::sync::Mutex::new(std::collections::HashMap::new()));
    if let Some((asked_at, supported)) = cache.lock().ok().and_then(|c| c.get(omp_bin).copied()) {
        if asked_at.elapsed() < TTL {
            return supported;
        }
    }
    let supported = std::process::Command::new(omp_bin)
        .arg("--help")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| help_advertises_no_ui(&String::from_utf8_lossy(&output.stdout)))
        .unwrap_or(true);
    if let Ok(mut cache) = cache.lock() {
        cache.insert(omp_bin.to_string(), (Instant::now(), supported));
    }
    supported
}

fn help_advertises_no_ui(help: &str) -> bool {
    help.lines()
        .any(|line| line.trim_start().starts_with("--no-ui"))
}

pub fn build_omp_args(
    model: Option<&str>,
    profile: Option<&str>,
    session_dir: &Path,
    session_file: &Path,
    headless_extensions: bool,
) -> Vec<String> {
    let has_existing_session = std::fs::metadata(session_file)
        .map(|metadata| metadata.len() > 0)
        .unwrap_or(false);
    let mut args: Vec<String> = vec!["--mode".into(), "rpc".into()];
    if headless_extensions {
        // Extensions run headless: no extension_ui_request dialogs for a host.
        args.push("--no-ui".into());
    }
    args.extend([
        "--session-dir".into(),
        session_dir.to_string_lossy().into_owned(),
        "--resume".into(),
        session_file.to_string_lossy().into_owned(),
    ]);
    if has_existing_session {
        args.push("--continue".into());
    }
    if let Some(profile) = profile.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--profile".into(), profile.into()]);
    }
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--model".into(), model.into()]);
    }
    // The prompt and its images (one user message) go over stdin as an RPC
    // `prompt` command; see `console_rpc`.
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
    /// OMP refused the prompt before accepting it: no run will follow.
    rpc_rejected: bool,
    /// OMP accepted the prompt (its `response`, or the run it started). A
    /// prompt that is written and never acknowledged is not a turn: nothing
    /// will follow, whatever the provider process is doing.
    prompt_acknowledged: bool,
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
                self.prompt_acknowledged = true;
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
                    // A retry after a refused attempt is a new attempt; a
                    // refusal that holds is reported again by its message_end.
                    self.native_error = None;
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
                    // A provider refusal (a 401, a quota) ends the assistant
                    // message with no content and its reason here.
                    if self.final_stop_reason.as_deref() == Some("error") {
                        if let Some(error) = message
                            .get("errorMessage")
                            .and_then(Value::as_str)
                            .filter(|text| !text.trim().is_empty())
                        {
                            self.native_error = Some(error.to_string());
                        }
                    }
                }
            }
            Some("prompt_result") => {
                if event.get("status").and_then(Value::as_str) == Some("error") {
                    if let Some(error) = event
                        .pointer("/error/message")
                        .and_then(Value::as_str)
                        .filter(|text| !text.trim().is_empty())
                    {
                        self.native_error = Some(error.to_string());
                    }
                }
            }
            Some("response") => {
                let ok = event.get("success").and_then(Value::as_bool) == Some(true);
                if event.get("id").and_then(Value::as_str)
                    == Some(crate::console_rpc::RPC_IDENTITY_ID)
                    && ok
                {
                    let observed = event
                        .pointer("/data/sessionId")
                        .and_then(Value::as_str)
                        .context("OMP get_state reported no sessionId")?;
                    anyhow::ensure!(
                        expected_provider_thread_id.is_none_or(|expected| expected == observed),
                        "OMP RPC session id {observed} does not match the exact resume identity"
                    );
                    self.provider_thread_id = Some(observed.to_string());
                    self.identity_confirmed = true;
                } else if event.get("command").and_then(Value::as_str) == Some("prompt") && ok {
                    self.prompt_acknowledged = true;
                } else if event.get("command").and_then(Value::as_str) == Some("prompt") && !ok {
                    self.rpc_rejected = true;
                    self.native_error = Some(
                        event
                            .get("error")
                            .and_then(Value::as_str)
                            .unwrap_or("OMP refused the prompt")
                            .to_string(),
                    );
                }
            }
            // RPC mode's end of a whole prompt, after any steer it absorbed.
            Some("session_settled") => self.turn_settled = true,
            Some("agent_settled" | "session_stop" | "turn_end") => {}
            Some("agent_end") => {
                let is_terminal = is_terminal_agent_end(event);
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

fn terminal_reason_code(terminal_state: &str, detail: Option<&str>) -> &'static str {
    let auth_refused = terminal_state == "run_failed"
        && detail.is_some_and(|text| {
            let lower = text.to_ascii_lowercase();
            lower.starts_with("401 ")
                || lower.starts_with("403 ")
                || lower.contains("incorrect api key")
                || lower.contains("invalid api key")
                || lower.contains("invalid_api_key")
                || lower.contains("authentication_error")
                || lower.contains("unauthorized")
        });
    match (terminal_state, auth_refused) {
        (_, true) => "provider_auth_required",
        ("run_completed", _) => "run_completed",
        ("run_cancelled", _) => "run_cancelled",
        _ => "run_failed",
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

fn is_terminal_agent_end(event: &Value) -> bool {
    for field in ["isTerminal", "willContinue"] {
        if event.get(field).is_some_and(|value| !value.is_boolean()) {
            return false;
        }
    }
    event
        .get("isTerminal")
        .and_then(Value::as_bool)
        .or_else(|| {
            event
                .get("willContinue")
                .and_then(Value::as_bool)
                .map(|value| !value)
        })
        .unwrap_or(true)
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
    if let Some(error) = stream_error.or(projection.native_error.as_deref()) {
        return ("run_failed", Some(error.into()));
    }
    if process_succeeded == Some(false) {
        return (
            "run_failed",
            Some("OMP process exited unsuccessfully".into()),
        );
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
        Some("agent_end") if is_terminal_agent_end(event) => Some(("idle", None)),
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
        crate::managed_source_claim::confirm_identity(
            &self.session_id,
            "omp",
            &self.session_file,
            &header.native_id,
            None,
            None,
        )
        .with_context(|| {
            format!(
                "binding the OMP console source {} to {}",
                self.session_file.display(),
                header.native_id
            )
        })?;
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
        // One slot per session: the daemon records the local ledger from it and
        // sends it. Only records no later event can restate — binding,
        // terminal — stay on the durable queue.
        let observed_at = Utc::now();
        crate::status_slot::publish_console_phase(
            "omp",
            OMP_PRINT_ADAPTER,
            &self.session_id,
            &self.run_id,
            &observed_at.to_rfc3339(),
            phase,
            tool_name.as_deref(),
            json!({
                "execution_lifetime": "one_shot",
                "thread_id": self.thread_id,
                "device_id": self.machine_name,
                "activity_seq": activity_seq,
            }),
        );
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
                "event": crate::console_rpc::runtime_stream_event(event),
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
        reason: Option<String>,
    ) {
        self.persist_local_phase("finished", None, Utc::now());
        // The reason is a code the served state keys on; the sentence is the
        // detail. A sentence in this field reads as no known reason: "Ended".
        let terminal_reason = terminal_reason_code(terminal_state, reason.as_deref());
        self.post_events(vec![json!({"runtime_key": format!("omp:{}", self.session_id), "session_id": self.session_id, "thread_id": self.thread_id, "run_id": self.run_id, "provider": "omp", "device_id": self.machine_name, "source": OMP_PRINT_ADAPTER, "kind": "terminal_signal", "occurred_at": Utc::now().to_rfc3339(), "dedupe_key": format!("omp-print:{}:{}:terminal", self.session_id, self.run_id), "payload": {"managed_transport": OMP_PRINT_ADAPTER, "execution_lifetime": "one_shot", "terminal_state": terminal_state, "terminal_reason": terminal_reason, "terminal_source": OMP_PRINT_ADAPTER, "exit_code": exit_code, "stderr_tail": reason, "provider_thread_id": self.provider_thread_id, "source_path": self.session_file.to_string_lossy(), "turn_id": self.turn_id, "client_request_id": self.client_request_id}})]).await;
        crate::turn_claims::mark_terminal(
            &self.run_id,
            terminal_state,
            (terminal_state == "run_failed")
                .then_some(reason.clone())
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
        if let Err(err) = crate::hook_outbox::enqueue_local_phase(
            db_path,
            &self.session_id,
            "omp",
            phase,
            tool_name.as_deref(),
            OMP_PRINT_ADAPTER,
            &observed_at.to_rfc3339(),
            Some(self.run_id.as_str()),
        ) {
            eprintln!(
                "[omp-print] enqueue local phase failed for {}: {err}",
                self.session_id
            );
        }
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

fn live_process_group_is_safe(claim: &crate::turn_claims::TurnClaim, pgid: i32) -> bool {
    if !claim.process_group_is_from_this_boot()
        || claim.process_group_id != Some(pgid)
        || !claim
            .pid
            .zip(claim.process_start_time.as_deref())
            .is_some_and(|(pid, expected_start)| {
                crate::process_identity::try_collect_process_fact(pid)
                    .is_some_and(|fact| fact.lstart == expected_start)
            })
    {
        return false;
    }
    claim.pid.and_then(crate::process_group::leader_group_for) == Some(pgid)
}

async fn cleanup_live_claim(run_id: &str) -> bool {
    let Ok(registry) = crate::turn_claims::default_registry() else {
        return false;
    };
    let Ok(claim) = registry.read(run_id) else {
        return false;
    };
    let Some(pgid) = claim.process_group_id else {
        let _ = cleanup_recorded_processes(&claim.owned_processes).await;
        return false;
    };
    let group_cleanup_verified = if live_process_group_is_safe(&claim, pgid) {
        cleanup_process_group(Some(pgid)).await
    } else {
        // Never signal a group after its recorded leader/birth identity stops
        // proving ownership. Recorded PIDs are still cleaned up individually.
        !crate::process_group::group_is_alive(pgid)
    };
    let recorded_cleanup_verified = cleanup_recorded_processes(&claim.owned_processes).await;
    group_cleanup_verified
        && recorded_cleanup_verified
        && !crate::process_group::group_is_alive(pgid)
}
async fn cleanup_owned_child(child: &mut Child, run_id: &str) -> bool {
    let _ = cleanup_live_claim(run_id).await;
    let reaped = tokio::time::timeout(Duration::from_secs(2), child.wait())
        .await
        .is_ok_and(|result| result.is_ok());
    if !reaped {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    cleanup_live_claim(run_id).await
}

fn refresh_owned_processes(run_id: &str) {
    let Ok(registry) = crate::turn_claims::default_registry() else {
        return;
    };
    let Ok(claim) = registry.read(run_id) else {
        return;
    };
    let (Some(pid), Some(process_group_id)) = (claim.pid, claim.process_group_id) else {
        return;
    };
    let Some(lineage) = crate::process_identity::try_collect_process_lineage() else {
        return;
    };
    let Some(facts) = crate::process_identity::try_collect_process_facts_by_pid() else {
        return;
    };
    let Some(root_fact) = facts.get(&pid) else {
        return;
    };
    if claim
        .process_start_time
        .as_deref()
        .is_some_and(|expected| root_fact.lstart != expected)
    {
        return;
    }
    let mut observed = vec![crate::turn_claims::OwnedProcessIdentity {
        pid,
        process_group_id,
        process_start_time: Some(root_fact.lstart.clone()),
    }];
    observed.extend(
        crate::process_identity::owned_processes(&lineage, pid, Some(process_group_id))
            .into_iter()
            .filter_map(|(entry, _)| {
                facts
                    .get(&entry.pid)
                    .map(|fact| crate::turn_claims::OwnedProcessIdentity {
                        pid: entry.pid,
                        process_group_id: entry.pgid,
                        process_start_time: Some(fact.lstart.clone()),
                    })
            }),
    );
    let _ = registry.record_owned_processes(run_id, observed);
}

fn recorded_process_matches(
    identity: &crate::turn_claims::OwnedProcessIdentity,
) -> Result<Option<bool>> {
    let Some(expected_start) = identity.process_start_time.as_deref() else {
        return Ok(Some(false));
    };
    match crate::process_identity::inspect_process_fact(identity.pid) {
        crate::process_identity::ProcessFactLookup::Present(fact) => {
            Ok(Some(fact.lstart == expected_start))
        }
        crate::process_identity::ProcessFactLookup::Absent => Ok(Some(false)),
        crate::process_identity::ProcessFactLookup::Unavailable => {
            Err(anyhow::anyhow!("process identity probe unavailable"))
        }
    }
}

async fn cleanup_recorded_processes(
    owned_processes: &[crate::turn_claims::OwnedProcessIdentity],
) -> bool {
    let mut live = Vec::new();
    let mut identity_failure = false;
    for identity in owned_processes {
        match recorded_process_matches(identity) {
            Ok(Some(true)) => live.push(identity.clone()),
            Ok(Some(false)) => {}
            Ok(None) => {}
            Err(_) => identity_failure = true,
        }
    }
    for identity in &live {
        unsafe {
            libc::kill(identity.pid as libc::pid_t, libc::SIGTERM);
        }
    }
    let deadline = Instant::now() + crate::process_group::DEFAULT_GRACE;
    loop {
        live.retain(|identity| match recorded_process_matches(identity) {
            Ok(Some(matches)) => matches,
            Ok(None) => false,
            Err(_) => {
                identity_failure = true;
                false
            }
        });
        if live.is_empty() {
            return !identity_failure;
        }
        if Instant::now() >= deadline {
            break;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
    for identity in &live {
        match recorded_process_matches(identity) {
            Ok(Some(true)) => unsafe {
                libc::kill(identity.pid as libc::pid_t, libc::SIGKILL);
            },
            Ok(Some(false)) | Ok(None) => {}
            Err(_) => identity_failure = true,
        }
    }
    let deadline = Instant::now() + crate::process_group::KILL_CONFIRM_BUDGET;
    loop {
        live.retain(|identity| match recorded_process_matches(identity) {
            Ok(Some(matches)) => matches,
            Ok(None) => false,
            Err(_) => {
                identity_failure = true;
                false
            }
        });
        if live.is_empty() {
            return !identity_failure;
        }
        if Instant::now() >= deadline {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

fn recovered_process_group_is_safe(claim: &crate::turn_claims::TurnClaim, pgid: i32) -> bool {
    if !claim.process_group_is_from_this_boot() {
        return false;
    }
    if claim.process_group_id != Some(pgid) {
        return false;
    }
    let (Some(pid), Some(expected_start)) = (claim.pid, claim.process_start_time.as_deref()) else {
        return false;
    };
    crate::process_identity::try_collect_process_fact(pid)
        .is_some_and(|fact| fact.lstart == expected_start)
        && crate::process_group::leader_group_for(pid) == Some(pgid)
}

async fn cleanup_recovered_process_group(
    run_id: &str,
    fallback_claim: &crate::turn_claims::TurnClaim,
    process_group_id: Option<i32>,
) -> bool {
    // Descendant refreshes and terminal projection can race teardown. Always
    // reload the claim so cleanup consumes the final owned-process inventory.
    let claim = crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(run_id))
        .unwrap_or_else(|_| fallback_claim.clone());
    let Some(pgid) = claim.process_group_id.or(process_group_id) else {
        return false;
    };
    let group_cleanup_verified = if recovered_process_group_is_safe(&claim, pgid) {
        cleanup_process_group(Some(pgid)).await
    } else {
        !crate::process_group::group_is_alive(pgid)
    };
    // A dead leader or surviving old group must not short-circuit the exact
    // PID/birth-identity cleanup for descendants that changed process group.
    let recorded_cleanup_verified = cleanup_recorded_processes(&claim.owned_processes).await;
    group_cleanup_verified
        && recorded_cleanup_verified
        && !crate::process_group::group_is_alive(pgid)
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
    fn stock_omp_console_runs_in_rpc_mode_and_binds_exact_resume() {
        let args = build_omp_args(
            Some("gpt-5.2"),
            Some("work"),
            Path::new("/sessions"),
            Path::new("/sessions/exact.jsonl"),
            true,
        );
        assert_eq!(
            args,
            vec![
                "--mode",
                "rpc",
                "--no-ui",
                "--session-dir",
                "/sessions",
                "--resume",
                "/sessions/exact.jsonl",
                "--profile",
                "work",
                "--model",
                "gpt-5.2",
            ]
        );
        // The prompt (and any image) is a stdin command, never argv.
        assert!(!args
            .iter()
            .any(|arg| arg == "-p" || arg == "--" || arg.starts_with('@')));
        assert!(!args.iter().any(|arg| matches!(
            arg.as_str(),
            "--no-tools" | "--no-extensions" | "--no-skills"
        )));
    }

    #[test]
    fn existing_omp_session_args_request_native_continuation() {
        let temp = tempfile::tempdir().unwrap();
        let session_file = temp.path().join("session.jsonl");
        std::fs::write(&session_file, b"{\"type\":\"session\"}\n").unwrap();
        let args = build_omp_args(None, None, temp.path(), &session_file, true);
        assert!(args.iter().any(|arg| arg == "--continue"));
    }

    #[test]
    fn omp_without_no_ui_still_gets_rpc_mode_but_not_the_flag() {
        let args = build_omp_args(
            None,
            None,
            Path::new("/sessions"),
            Path::new("/sessions/exact.jsonl"),
            false,
        );
        assert_eq!(args[..2], ["--mode", "rpc"]);
        assert!(!args.iter().any(|arg| arg == "--no-ui"));
        assert!(args.iter().any(|arg| arg == "--resume"));
    }

    #[test]
    fn help_text_decides_whether_no_ui_is_advertised() {
        let current = "      --mode=<value>   Output mode: text, json, rpc, or rpc-ui\n      --no-ui          With --mode rpc: run extensions headless\n";
        let old = "      --mode=<value>   Output mode: text, json, rpc, or rpc-ui\n      --no-tools       Disable all built-in tools\n";
        assert!(help_advertises_no_ui(current));
        assert!(!help_advertises_no_ui(old));
        // A mention in prose is not the flag.
        assert!(!help_advertises_no_ui("use --no-ui to hide dialogs"));
    }

    /// The stream of a console run whose OpenAI key was refused (recorded
    /// 2026-10-06): an empty assistant message that ended in `error`, then a
    /// settled session. The run failed because of the key, and says so.
    #[test]
    fn a_provider_refusal_is_the_run_failure_and_an_auth_reason() {
        let mut projection = OmpStreamProjection::default();
        let refusal = "401 Incorrect API key provided: sk-proj-****q1AA. (type=invalid_request_error param=invalid_api_key)";
        for event in [
            json!({"type":"agent_start"}),
            json!({"type":"message_start","message":{"role":"assistant","content":[]}}),
            json!({"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":refusal}}),
            json!({"type":"agent_end","messages":[]}),
            json!({"type":"prompt_result","id":"longhouse-prompt","status":"error","sessionSettled":true,"error":{"message":refusal}}),
            json!({"type":"session_settled"}),
        ] {
            projection.apply(None, &event).unwrap();
        }
        // Even when the native session file did not drain, the provider's own
        // refusal is the reason, not the drain.
        let (state, detail) =
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, false);
        assert_eq!(state, "run_failed");
        assert_eq!(detail.as_deref(), Some(refusal));
        assert_eq!(
            terminal_reason_code(state, detail.as_deref()),
            "provider_auth_required"
        );
        assert_eq!(
            terminal_reason_code("run_failed", Some("OMP process exited unsuccessfully")),
            "run_failed"
        );
        assert_eq!(terminal_reason_code("run_completed", None), "run_completed");
        // A bad exit after a refusal is still the refusal.
        let (_, detail) =
            terminal_state_for_projection(&projection, Some(false), false, None, true, true, true);
        assert_eq!(detail.as_deref(), Some(refusal));
    }

    #[test]
    fn a_retry_that_succeeds_after_a_refused_attempt_is_not_a_failure() {
        let mut projection = OmpStreamProjection::default();
        projection.identity_confirmed = true;
        for event in [
            json!({"type":"agent_start"}),
            json!({"type":"message_start","message":{"role":"assistant","content":[]}}),
            json!({"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"429 slow down"}}),
            json!({"type":"message_start","message":{"role":"assistant","content":[]}}),
            json!({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}],"stopReason":"stop"}}),
            json!({"type":"agent_end"}),
            json!({"type":"session_settled"}),
        ] {
            projection.apply(None, &event).unwrap();
        }
        assert_eq!(projection.native_error, None);
        assert_eq!(
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, true).0,
            "run_completed"
        );
    }

    #[test]
    fn rpc_identity_is_get_state_and_session_settled_ends_the_run() {
        let mut projection = OmpStreamProjection::default();
        projection
            .apply(
                Some("01a0e975-6956-7480-b292-42c7635c1b83"),
                &json!({"id": crate::console_rpc::RPC_IDENTITY_ID, "type": "response", "command": "get_state", "success": true, "data": {"sessionId": "01a0e975-6956-7480-b292-42c7635c1b83"}}),
            )
            .unwrap();
        assert!(projection.identity_confirmed);
        assert!(OmpStreamProjection::default()
            .apply(
                Some("other"),
                &json!({"id": crate::console_rpc::RPC_IDENTITY_ID, "type": "response", "command": "get_state", "success": true, "data": {"sessionId": "01a0e975-6956-7480-b292-42c7635c1b83"}}),
            )
            .is_err());
        projection
            .apply(None, &json!({"type": "agent_start"}))
            .unwrap();
        assert!(!projection.turn_settled);
        projection
            .apply(None, &json!({"type": "session_settled"}))
            .unwrap();
        assert!(projection.turn_settled);
        let mut refused = OmpStreamProjection::default();
        refused
            .apply(None, &json!({"id": "longhouse-prompt", "type": "response", "command": "prompt", "success": false, "error": "no model"}))
            .unwrap();
        assert!(refused.rpc_rejected);
        assert_eq!(refused.native_error.as_deref(), Some("no model"));
    }

    #[test]
    fn ordinary_agent_end_is_terminal_but_explicit_continuation_is_not() {
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
            .apply(None, &json!({"type":"agent_end","willContinue":true}))
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
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, false)
                .0,
            "run_failed"
        );
        projection
            .apply(None, &json!({"type":"agent_end"}))
            .unwrap();
        assert_eq!(
            terminal_state_for_projection(&projection, Some(true), false, None, true, true, true).0,
            "run_completed"
        );
    }

    #[test]
    fn malformed_agent_end_lifecycle_fields_are_not_terminal() {
        assert!(!is_terminal_agent_end(&json!({
            "type": "agent_end",
            "isTerminal": "true"
        })));
        assert!(!is_terminal_agent_end(&json!({
            "type": "agent_end",
            "willContinue": "false"
        })));
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
native_id = "01a08857-826d-72f6-b816-672b54116504"
header = {
    "type": "session",
    "version": 3,
    "id": native_id,
    "timestamp": "2026-09-09T22:43:51.533Z",
    "cwd": os.getcwd(),
}
def out(event):
    print(json.dumps(event, separators=(",", ":")), flush=True)
out({"type":"ready"})
# RPC mode never exits on its own: Longhouse ends it after session_settled.
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    if kind == "get_state":
        out({"id":command.get("id"),"type":"response","command":"get_state","success":True,"data":{"sessionId":native_id}})
    elif kind == "steer":
        with open(source + ".steer.log", "a", encoding="utf-8") as stream:
            stream.write(command["message"])
        out({"id":command.get("id"),"type":"response","command":"steer","success":True})
    elif kind == "prompt":
        prompt = command["message"]
        out({"id":command.get("id"),"type":"response","command":"prompt","success":True})
        if os.path.getsize(source) == 0:
            with open(source, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(header, separators=(",", ":")) + "\n")
        with open(source, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type":"message","id":str(uuid.uuid4()),"message":{"role":"user","content":[{"type":"text","text":prompt}]}}) + "\n")
            stream.write(json.dumps({"type":"message","id":str(uuid.uuid4()),"message":{"role":"assistant","content":[{"type":"text","text":prompt}],"stopReason":"stop"}}) + "\n")
        for event in [
            {"type":"agent_start"},
            {"type":"message_start","message":{"role":"assistant","content":[]}},
            {"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":prompt}},
            {"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":prompt}],"stopReason":"stop"}},
            {"type":"agent_end","isTerminal":True,"willContinue":False},
            {"type":"session_settled"},
        ]:
            out(event)
"##,
        )
        .unwrap();
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions).unwrap();
    }

    #[test]
    fn recovered_cleanup_rejects_reused_pid_identity_without_signalling() {
        let pid = std::process::id();
        let process_group_id = unsafe { libc::getpgid(pid as libc::pid_t) };
        let identity = crate::turn_claims::OwnedProcessIdentity {
            pid,
            process_group_id,
            process_start_time: Some("not-the-current-process".into()),
        };
        assert_eq!(recorded_process_matches(&identity).unwrap(), Some(false));
    }

    #[tokio::test]
    async fn recovered_cleanup_signals_matching_pid_after_process_group_change() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        use std::process::Command as StdCommand;

        let mut child = StdCommand::new("sleep").arg("30").spawn().unwrap();
        let pid = child.id();
        let fact = crate::process_identity::try_collect_process_fact(pid).unwrap();
        let actual_group = unsafe { libc::getpgid(pid as libc::pid_t) };
        let recorded = crate::turn_claims::OwnedProcessIdentity {
            pid,
            process_group_id: if actual_group == 1 { 2 } else { 1 },
            process_start_time: Some(fact.lstart),
        };
        let reused_pid = crate::turn_claims::OwnedProcessIdentity {
            pid: std::process::id(),
            process_group_id: actual_group,
            process_start_time: Some("not-the-current-process".into()),
        };
        let waiter = std::thread::spawn(move || child.wait().unwrap());

        assert!(cleanup_recorded_processes(&[reused_pid, recorded]).await);
        assert!(!waiter.join().unwrap().success());
    }

    #[tokio::test]
    async fn recovered_cleanup_still_consumes_owned_pids_when_group_is_untrusted() {
        // Spawns a subprocess or reads the process table: hold the shared
        // agent-state lock, so a concurrent test cannot empty PATH or move a
        // global tree under it.
        let _guard = crate::console_adapter::agent_state_guard();
        use std::process::Command as StdCommand;

        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let run_id = Uuid::new_v4().to_string();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        let current_pid = std::process::id();
        let current_pgid = unsafe { libc::getpgid(current_pid as libc::pid_t) };
        let mut child = StdCommand::new("sleep").arg("30").spawn().unwrap();
        let child_pid = child.id();
        let child_fact = crate::process_identity::try_collect_process_fact(child_pid).unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                current_pid,
                current_pgid,
                Some("not-the-current-process".into()),
                OMP_PRINT_ADAPTER,
                "launch",
                None,
                "/tmp/stdout.log",
                "/tmp/stderr.log",
                json!({}),
            )
            .unwrap();
        let claim = registry
            .record_owned_processes(
                &run_id,
                vec![crate::turn_claims::OwnedProcessIdentity {
                    pid: child_pid,
                    process_group_id: current_pgid,
                    process_start_time: Some(child_fact.lstart),
                }],
            )
            .unwrap();
        let waiter = std::thread::spawn(move || child.wait().unwrap());

        assert!(!cleanup_recovered_process_group(&run_id, &claim, Some(current_pgid)).await);
        assert!(!waiter.join().unwrap().success());
    }

    #[tokio::test]
    async fn live_cleanup_consumes_owned_pids_after_leader_identity_is_lost() {
        use std::process::Command as StdCommand;

        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", &longhouse_home);
        }
        let registry = crate::turn_claims::default_registry().unwrap();
        let run_id = Uuid::new_v4().to_string();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        let current_pid = std::process::id();
        let current_pgid = unsafe { libc::getpgid(current_pid as libc::pid_t) };
        let mut child = StdCommand::new("sleep").arg("30").spawn().unwrap();
        let child_pid = child.id();
        let child_fact = crate::process_identity::try_collect_process_fact(child_pid).unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                current_pid,
                current_pgid,
                Some("not-the-current-process".into()),
                OMP_PRINT_ADAPTER,
                "launch",
                None,
                "/tmp/stdout.log",
                "/tmp/stderr.log",
                json!({}),
            )
            .unwrap();
        registry
            .record_owned_processes(
                &run_id,
                vec![crate::turn_claims::OwnedProcessIdentity {
                    pid: child_pid,
                    process_group_id: current_pgid,
                    process_start_time: Some(child_fact.lstart),
                }],
            )
            .unwrap();
        let waiter = std::thread::spawn(move || child.wait().unwrap());

        assert!(!cleanup_live_claim(&run_id).await);
        assert!(!waiter.join().unwrap().success());
        match previous_home {
            Some(home) => unsafe { std::env::set_var("LONGHOUSE_HOME", home) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }
    }
    #[tokio::test]
    async fn live_cleanup_reaps_owned_child_before_verifying_group_death() {
        use std::os::unix::process::CommandExt;

        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse");
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", &longhouse_home);
        }
        let registry = crate::turn_claims::default_registry().unwrap();
        let run_id = Uuid::new_v4().to_string();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        let mut child = Command::new("sleep");
        child.arg("30");
        unsafe {
            child.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = child.spawn().unwrap();
        let child_pid = child.id().unwrap();
        let process_group_id = i32::try_from(child_pid).unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                child_pid,
                process_group_id,
                crate::turn_claims::process_start_time_for_pid(Some(child_pid)),
                OMP_PRINT_ADAPTER,
                "launch",
                None,
                "/tmp/stdout.log",
                "/tmp/stderr.log",
                json!({}),
            )
            .unwrap();
        refresh_owned_processes(&run_id);

        assert!(cleanup_owned_child(&mut child, &run_id).await);
        assert!(child.try_wait().unwrap().is_some());

        match previous_home {
            Some(home) => unsafe { std::env::set_var("LONGHOUSE_HOME", home) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }
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
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
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
            image_paths: Vec::new(),
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
        // The console turn owns a local claim, which is what discovery reads
        // once the daemon projects it; the archive database is no longer written
        // on this path.
        let claim = crate::managed_source_claim::read_claim(&first.session_id)
            .expect("read claim")
            .expect("a console turn must claim its source");
        assert_eq!(claim.state, crate::managed_source_claim::ClaimState::Bound);
        assert_eq!(claim.native_session_id.as_deref(), Some(native_id.as_str()));
        // RPC mode: the prompt (even one that looks like a flag) is a stdin
        // command, never argv.
        assert!(first.argv.windows(2).any(|pair| pair == ["--mode", "rpc"]));
        assert!(!first.argv.iter().any(|arg| arg == "--OMP_FIRST"));
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
            image_paths: Vec::new(),
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

    /// A stock OMP whose startup takes a moment, with the two behaviours seen on
    /// 2026-10-01. `mode` is one of:
    /// - `wedge_if_early`: bytes already pending on stdin when it starts make it
    ///   answer `get_state` and then never read stdin again (what stock OMP 18.4.5
    ///   does on macOS); commands written after `ready` work.
    /// - `never_acknowledge`: it answers `get_state` and reads the prompt but
    ///   never responds to it and never starts a run.
    fn write_stalling_fake_omp(path: &Path, mode: &str) {
        std::fs::write(
            path,
            format!(
                r##"#!/usr/bin/env python3
import json
import os
import select
import sys
import time

MODE = "{mode}"
args = sys.argv[1:]
source = args[args.index("--resume") + 1]
native_id = "01a08857-826d-72f6-b816-672b54116504"
def out(event):
    print(json.dumps(event, separators=(",", ":")), flush=True)
time.sleep(1.0)
early = bool(select.select([sys.stdin], [], [], 0)[0])
# Stock OMP creates its native session file (header first) while starting.
if not os.path.exists(source) or os.path.getsize(source) == 0:
    os.makedirs(os.path.dirname(source), exist_ok=True)
    with open(source, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({{"type":"session","version":3,"id":native_id,"timestamp":"2026-09-09T22:43:51.533Z","cwd":os.getcwd()}}) + "\n")
out({{"type":"ready"}})
for line in sys.stdin:
    command = json.loads(line)
    kind = command["type"]
    if kind == "get_state":
        out({{"id":command.get("id"),"type":"response","command":"get_state","success":True,"data":{{"sessionId":native_id}}}})
        if MODE == "wedge_if_early" and early:
            time.sleep(3600)
    elif kind == "prompt":
        if MODE == "never_acknowledge":
            time.sleep(3600)
        prompt = command["message"]
        out({{"id":command.get("id"),"type":"response","command":"prompt","success":True}})
        with open(source, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({{"type":"message","id":"u1","message":{{"role":"user","content":[{{"type":"text","text":prompt}}]}}}}) + "\n")
            stream.write(json.dumps({{"type":"message","id":"a1","message":{{"role":"assistant","content":[{{"type":"text","text":"ok"}}],"stopReason":"stop"}}}}) + "\n")
        for event in [
            {{"type":"agent_start"}},
            {{"type":"message_start","message":{{"role":"assistant","content":[]}}}},
            {{"type":"message_end","message":{{"role":"assistant","content":[{{"type":"text","text":"ok"}}],"stopReason":"stop"}}}},
            {{"type":"agent_end","isTerminal":True,"willContinue":False}},
            {{"type":"session_settled"}},
        ]:
            out(event)
"##
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(path).unwrap().permissions();
        permissions.set_mode(0o755);
        std::fs::set_permissions(path, permissions).unwrap();
    }

    fn stalling_fake_config(
        temp: &Path,
        mode: &str,
        prompt: String,
    ) -> (OmpPrintRunConfig, String) {
        let fake_omp = temp.join("omp");
        write_stalling_fake_omp(&fake_omp, mode);
        let run_id = Uuid::new_v4().to_string();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        crate::turn_claims::default_registry()
            .unwrap()
            .claim(&run_id, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        (
            OmpPrintRunConfig {
                session_id,
                thread_id,
                turn_id: None,
                run_id: run_id.clone(),
                client_request_id: Some("omp-stall".into()),
                cwd: PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .unwrap()
                    .to_path_buf(),
                omp_bin: fake_omp.to_string_lossy().into_owned(),
                prompt,
                image_paths: Vec::new(),
                model: None,
                profile: None,
                session_dir: Some(temp.join("omp-sessions")),
                resume_provider_thread_id: None,
                resume_session_file: None,
                permission_mode: "provider_local".into(),
                machine_name: "omp-test".into(),
                local_db_path: Some(temp.join("agent.db")),
            },
            run_id,
        )
    }

    fn set_test_longhouse_home(path: PathBuf) -> Option<std::ffi::OsString> {
        let previous = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", path);
        }
        previous
    }

    fn restore_test_longhouse_home(previous: Option<std::ffi::OsString>) {
        match previous {
            Some(home) => unsafe { std::env::set_var("LONGHOUSE_HOME", home) },
            None => unsafe { std::env::remove_var("LONGHOUSE_HOME") },
        }
    }

    #[tokio::test]
    async fn a_large_prompt_is_not_written_until_omp_reports_ready() {
        // The 2026-10-01 incident: a 617 KB image prompt written at spawn wedged a
        // stock OMP's stdin for good (reproduced on the bench, 450 KB, OMP 18.4.5).
        // The fake wedges exactly when bytes are pending before `ready`, so this only
        // completes if the adapter holds every command until the `ready` frame.
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let previous = set_test_longhouse_home(temp.path().join("longhouse"));
        let (config, run_id) =
            stalling_fake_config(temp.path(), "wedge_if_early", "x".repeat(300_000));

        let started = start_omp_print_turn(config).await.unwrap();
        let claim = wait_for_terminal(&run_id).await;

        assert_eq!(
            claim.result.as_ref().unwrap()["terminal_state"],
            "run_completed",
            "{:?}",
            claim.error
        );
        assert_eq!(
            started.provider_thread_id.as_deref(),
            Some("01a08857-826d-72f6-b816-672b54116504")
        );
        restore_test_longhouse_home(previous);
    }

    #[tokio::test]
    async fn a_prompt_omp_never_acknowledges_fails_the_turn_instead_of_thinking_forever() {
        // The same incident, other half: the provider stayed alive and idle, no
        // response to the prompt ever came, and the run read Thinking for 1h47m.
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let previous = set_test_longhouse_home(temp.path().join("longhouse"));
        let (config, run_id) =
            stalling_fake_config(temp.path(), "never_acknowledge", "hello".into());

        let started = start_omp_print_turn(config).await.unwrap();
        let claim = wait_for_terminal(&run_id).await;

        assert_eq!(
            claim.result.as_ref().unwrap()["terminal_state"],
            "run_failed"
        );
        assert!(
            claim
                .error
                .as_deref()
                .unwrap_or_default()
                .contains("did not acknowledge the prompt"),
            "{claim:?}"
        );
        // The idle provider is not left running behind a failed turn.
        assert_ne!(unsafe { libc::killpg(started.process_group_id, 0) }, 0);
        restore_test_longhouse_home(previous);
    }

    #[test]
    fn prompt_acknowledgement_is_the_response_or_the_run_it_started() {
        let mut by_response = OmpStreamProjection::default();
        assert!(!by_response.prompt_acknowledged);
        by_response
            .apply(None, &json!({"id": "longhouse-prompt", "type": "response", "command": "prompt", "success": true}))
            .unwrap();
        assert!(by_response.prompt_acknowledged);
        assert!(!by_response.rpc_rejected);

        let mut by_run = OmpStreamProjection::default();
        by_run.apply(None, &json!({"type": "agent_start"})).unwrap();
        assert!(by_run.prompt_acknowledged);

        // The identity response is not the prompt's.
        let mut identity_only = OmpStreamProjection::default();
        identity_only
            .apply(None, &json!({"id": crate::console_rpc::RPC_IDENTITY_ID, "type": "response", "command": "get_state", "success": true, "data": {"sessionId": "s"}}))
            .unwrap();
        assert!(!identity_only.prompt_acknowledged);
    }

    #[test]
    fn an_unacknowledged_prompt_is_overdue_only_past_its_deadline_and_only_unsettled() {
        use crate::console_rpc::{prompt_ack_overdue, PROMPT_ACK_DEADLINE};
        let past = PROMPT_ACK_DEADLINE + Duration::from_secs(1);
        assert!(prompt_ack_overdue(false, false, past));
        assert!(!prompt_ack_overdue(false, false, Duration::ZERO));
        assert!(!prompt_ack_overdue(true, false, past), "acknowledged");
        assert!(
            !prompt_ack_overdue(false, true, past),
            "rejected, already terminal"
        );
    }

    #[tokio::test]
    async fn a_recovered_turn_whose_prompt_was_never_acknowledged_is_failed_not_re_asserted() {
        // A Machine Agent restart found the wedged run's provider alive and
        // re-posted `thinking` for it every time, which the host kept vouching for.
        settle_recovered_unacknowledged_claim(
            (Utc::now() - chrono::Duration::hours(2)).to_rfc3339(),
        )
        .await;
    }

    #[tokio::test]
    async fn a_recovered_claim_with_an_unreadable_time_is_not_trusted_to_be_fresh() {
        settle_recovered_unacknowledged_claim("not a time".to_string()).await;
    }

    async fn settle_recovered_unacknowledged_claim(claimed_at: String) {
        use std::os::unix::process::CommandExt;

        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let previous = set_test_longhouse_home(temp.path().join("longhouse"));
        let registry = crate::turn_claims::default_registry().unwrap();
        let run_id = Uuid::new_v4().to_string();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        registry
            .claim(&run_id, &session_id, &thread_id, None, None, "omp")
            .unwrap();
        let stdout_path = temp.path().join("stdout.log");
        std::fs::write(
            &stdout_path,
            "{\"type\":\"ready\"}\n{\"id\":\"longhouse-identity\",\"type\":\"response\",\"command\":\"get_state\",\"success\":true,\"data\":{\"sessionId\":\"s\"}}\n",
        )
        .unwrap();
        let mut provider = Command::new("sleep");
        provider.arg("30");
        unsafe {
            provider.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut provider = provider.spawn().unwrap();
        let pid = provider.id().unwrap();
        let process_group_id = i32::try_from(pid).unwrap();
        registry
            .mark_spawned_invocation(
                &run_id,
                pid,
                process_group_id,
                crate::turn_claims::process_start_time_for_pid(Some(pid)),
                OMP_PRINT_ADAPTER,
                "launch",
                None,
                &stdout_path.to_string_lossy(),
                "/tmp/stderr.log",
                json!({}),
            )
            .unwrap();
        refresh_owned_processes(&run_id);
        let mut claim = registry.read(&run_id).unwrap();
        claim.claimed_at = claimed_at;
        let sink = OmpPrintSink {
            session_id,
            thread_id,
            turn_id: None,
            run_id: run_id.clone(),
            client_request_id: None,
            launch_id: "launch".into(),
            process_group_id: Some(process_group_id),
            stdout_path,
            session_dir: temp.path().to_path_buf(),
            session_file: temp.path().join("session.jsonl"),
            provider_thread_id: None,
            source_start_len: 0,
            binding_emitted: true,
            machine_name: "omp-test".into(),
            local_db_path: Some(temp.path().join("agent.db")),
            runtime_events_outbox_dir: temp.path().join("runtime-events"),
        };
        std::fs::create_dir_all(&sink.runtime_events_outbox_dir).unwrap();

        monitor_recovered_omp_claim(claim, temp.path().join("stderr.log"), sink).await;

        let settled = registry.read(&run_id).unwrap();
        assert_eq!(settled.state, "terminal");
        assert_eq!(
            settled.result.as_ref().unwrap()["terminal_state"],
            "run_failed"
        );
        assert!(settled
            .error
            .as_deref()
            .unwrap_or_default()
            .contains("did not acknowledge the prompt"));
        let _ = provider.wait().await;
        restore_test_longhouse_home(previous);
    }
}
