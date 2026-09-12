//! Antigravity Console turns through stock `agy --print`.
//!
//! Turn-scoped one-shot adapter: one bounded `agy --print <prompt>` invocation
//! per Console turn. Unlike the Helm path, this does not depend on hooks --
//! which matters, because agy loads its hooks and never fires them when the
//! user authenticated with `GEMINI_API_KEY`, so hook-delivered control is not
//! universally available. A one-shot print turn is.
//!
//! Stock `--output-format stream-json` reports native identity in its `init`
//! event, before the terminal `result`. Discovery reads that same durable stdout
//! independently of the monitor and holds unowned sources while a Console
//! launch is still waiting for identity, rather than guessing from a workspace.

use std::fs::{File, OpenOptions};
use std::io::{BufReader, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::process::{Child, Command};

use crate::console_adapter::{claim_process_liveness, stderr_tail, ClaimLiveness};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use uuid::Uuid;

pub const ANTIGRAVITY_PRINT_ADAPTER: &str = "antigravity_print";
pub const DEFAULT_ANTIGRAVITY_BIN: &str = "agy";
pub const DEFAULT_PRINT_TIMEOUT_SECS: u64 = 600;

#[derive(Clone, Debug)]
pub struct AntigravityPrintRunConfig {
    pub session_id: String,
    pub thread_id: String,
    pub turn_id: Option<String>,
    pub run_id: String,
    pub client_request_id: Option<String>,
    pub cwd: PathBuf,
    pub antigravity_bin: String,
    pub prompt: String,
    pub model: Option<String>,
    /// Continue an existing agy conversation instead of starting a new one.
    pub conversation_id: Option<String>,
    pub print_timeout_secs: Option<u64>,
    pub permission_mode: String,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct AntigravityPrintRunSummary {
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
struct AntigravityPrintSink {
    session_id: String,
    thread_id: String,
    turn_id: Option<String>,
    run_id: String,
    client_request_id: Option<String>,
    launch_id: String,
    process_group_id: Option<i32>,
    stdout_path: PathBuf,
    machine_name: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

pub async fn start_antigravity_print_turn(
    config: AntigravityPrintRunConfig,
) -> Result<AntigravityPrintRunSummary> {
    validate_uuid(&config.session_id, "session_id")?;
    validate_uuid(&config.thread_id, "thread_id")?;
    validate_uuid(&config.run_id, "run_id")?;
    if let Some(turn_id) = normalized_optional(&config.turn_id) {
        validate_uuid(&turn_id, "turn_id")?;
    }
    if config.permission_mode != "bypass" {
        anyhow::bail!("Antigravity Console currently supports bypass permission mode only");
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
    // Fail before launching if durable ownership cannot be recorded. Discovery
    // can also reconstruct this binding from the claim and structured stdout.
    let db_path = config
        .local_db_path
        .as_deref()
        .context("Antigravity Console requires a local source binding database")?;
    crate::state::db::open_client_connection(db_path, Duration::from_millis(500))?;
    if let Some(conversation_id) = normalized_optional(&config.conversation_id) {
        validate_uuid(&conversation_id, "conversation_id")?;
        persist_transcript_binding(
            db_path,
            &conversation_transcript_path(
                &antigravity_brain_root().context("HOME is unset")?,
                &conversation_id,
            ),
            &config.session_id,
            &conversation_id,
        )?;
        crate::turn_claims::default_registry()?.mark_provider_binding(
            &config.run_id,
            &conversation_id,
            None,
        )?;
    }

    let args = build_antigravity_args(
        &config.prompt,
        config.model.as_deref(),
        config.conversation_id.as_deref(),
        config
            .print_timeout_secs
            .unwrap_or(DEFAULT_PRINT_TIMEOUT_SECS),
    );
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
            return Err(error)
                .with_context(|| format!("spawning `{}` --print", config.antigravity_bin))
        }
    };
    let pid = child.id().context("agy --print returned no pid")?;
    let process_group_id =
        i32::try_from(pid).context("Antigravity pid exceeds process-group range")?;
    let sink = AntigravityPrintSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        stdout_path: stdout_path.clone(),
        machine_name: config.machine_name.clone(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
    };
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "antigravity",
        "transport": ANTIGRAVITY_PRINT_ADAPTER,
        "provider_thread_id": config.conversation_id,
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
        ANTIGRAVITY_PRINT_ADAPTER,
        &launch_id,
        config.conversation_id.as_deref(),
        &stdout_path.to_string_lossy(),
        &stderr_path.to_string_lossy(),
        result,
    ) {
        cleanup_process_group(Some(process_group_id)).await;
        let _ = child.kill().await;
        return Err(error).context("persisting Antigravity Console spawn identity");
    }
    let monitor_stderr = stderr_path.clone();
    tokio::spawn(async move {
        monitor_antigravity_print(&mut child, &monitor_stderr, sink).await;
    });

    Ok(AntigravityPrintRunSummary {
        session_id: config.session_id,
        thread_id: config.thread_id,
        run_id: config.run_id,
        provider_thread_id: config.conversation_id,
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        argv,
    })
}

/// Settle or re-adopt Console turns that outlived the engine that spawned them.
///
/// Without this an engine restart strands every in-flight claim non-terminal:
/// the session shows a turn that never ends, and the run is never reported
/// failed, cancelled or complete. Every other one-shot adapter does this at
/// startup; Antigravity needs it for the same reason.
pub async fn recover_antigravity_print_turns(
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
        if !unrecorded_spawn && (!is_antigravity_print_claim(&claim) || claim.state != "spawned") {
            continue;
        }
        let stdout_path = claim_stdout_path(&claim, &agent_dir);
        let stderr_path = claim
            .stderr_path
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| stdout_path.with_file_name("stderr.log"));
        let sink = AntigravityPrintSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            stdout_path,
            machine_name: machine_name.to_string(),
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        match recovered_claim_liveness(&claim, &sink.stdout_path, inventory.as_ref()).await {
            ClaimLiveness::Live => {
                tokio::spawn(async move {
                    monitor_recovered_claim(claim, stderr_path, sink).await;
                });
                recovered += 1;
            }
            ClaimLiveness::Gone => {
                settle_recovered_dead_claim(&claim, &stderr_path, &sink).await;
            }
            ClaimLiveness::Unknown => {
                tracing::warn!(
                    run_id = %claim.run_id,
                    "Execution evidence unavailable; retrying Antigravity Console recovery"
                );
                tokio::spawn(async move {
                    monitor_recovered_claim(claim, stderr_path, sink).await;
                });
                recovered += 1;
            }
        }
    }
    Ok(recovered)
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
    claimed_at: Option<DateTime<Utc>>,
    mut fresh_start_time: impl FnMut(u32) -> Option<DateTime<Utc>>,
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

async fn monitor_recovered_claim(
    claim: crate::turn_claims::TurnClaim,
    stderr_path: PathBuf,
    sink: AntigravityPrintSink,
) {
    let mut source_bound = false;
    loop {
        if !source_bound {
            source_bound = sink.read_and_bind_transcript(false).await
                .map(|output| output.conversation_id.is_some())
                .unwrap_or_else(|error| {
                    tracing::warn!(run_id = %sink.run_id, %error, "Antigravity source binding is pending");
                    false
                });
        }
        let liveness = if claim.state == "claimed" && claim.pid.is_none() {
            recovered_claim_liveness(&claim, &sink.stdout_path, None).await
        } else {
            claim_process_liveness(&claim)
        };
        if liveness == ClaimLiveness::Gone {
            let cancel_requested = crate::turn_claims::default_registry()
                .and_then(|registry| registry.read(&claim.run_id))
                .ok()
                .and_then(|current| current.cancel_requested_at)
                .is_some();
            // The exit status died with the engine that owned the child, so a
            // recovered turn is settled from its claim and its output rather
            // than from a code nobody can observe any more.
            settle_antigravity_claim(&sink, cancel_requested, None, &stderr_path).await;
            return;
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

async fn settle_recovered_dead_claim(
    claim: &crate::turn_claims::TurnClaim,
    stderr_path: &Path,
    sink: &AntigravityPrintSink,
) {
    if claim.process_group_is_from_this_boot() {
        cleanup_process_group(sink.process_group_id).await;
    }
    settle_antigravity_claim(sink, claim.cancel_requested_at.is_some(), None, stderr_path).await;
}

pub fn interrupt_antigravity_print_turn(
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
    if !is_antigravity_print_claim(&claim) || claim.state != "spawned" {
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
    // agy leaves run_command children behind when it is signalled, so the
    // group -- not the pid -- is the unit of termination.
    let result = unsafe { libc::killpg(pgid, libc::SIGINT) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting Antigravity Console process group");
        }
    }
    Ok(())
}

async fn monitor_antigravity_print(
    child: &mut Child,
    stderr_path: &Path,
    sink: AntigravityPrintSink,
) {
    sink.post_phase("thinking", None).await;
    let mut source_bound = false;
    let status = loop {
        if !source_bound {
            source_bound = sink.read_and_bind_transcript(false).await
                .map(|output| output.conversation_id.is_some())
                .unwrap_or_else(|error| {
                    tracing::warn!(run_id = %sink.run_id, %error, "Antigravity source binding is pending");
                    false
                });
        }
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(error) => {
                cleanup_process_group(sink.process_group_id).await;
                sink.post_terminal("run_failed", None, Some(error.to_string()))
                    .await;
                return;
            }
        }
    };
    // agy flushes its result object and the conversation transcript as it
    // exits; give the filesystem a beat before reading either.
    tokio::time::sleep(Duration::from_millis(150)).await;
    // An interrupted turn exits like any other, so the claim -- not the exit
    // code -- is what distinguishes cancellation from completion.
    let cancel_requested = crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(&sink.run_id))
        .ok()
        .and_then(|claim| claim.cancel_requested_at)
        .is_some();
    settle_antigravity_claim(&sink, cancel_requested, status.code(), stderr_path).await;
}

async fn settle_antigravity_claim(
    sink: &AntigravityPrintSink,
    cancel_requested: bool,
    exit_code: Option<i32>,
    stderr_path: &Path,
) {
    // Ownership is independent of turn success: cancellation and model errors
    // still leave a native transcript that belongs to this Console session.
    let output = sink.read_and_bind_transcript(true).await;
    if cancel_requested {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal("run_cancelled", exit_code, None).await;
        return;
    }
    // A non-zero exit is a failed turn even when agy left a readable result.
    if exit_code.is_some_and(|code| code != 0) {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal("run_failed", exit_code, stderr_tail(stderr_path))
            .await;
        return;
    }
    let output = match output {
        Ok(output) if output.finished => output,
        Ok(_) => {
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal(
                "run_failed",
                exit_code,
                Some(stderr_tail(stderr_path).unwrap_or_else(|| {
                    "agy exited without reporting a terminal result".to_string()
                })),
            )
            .await;
            return;
        }
        Err(error) => {
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal("run_failed", exit_code, Some(error.to_string()))
                .await;
            return;
        }
    };
    let conversation_id = output
        .conversation_id
        .expect("a result has a validated identity");
    let status = output.status;
    // A reported ERROR is a failed turn even though agy exits 0 for it.
    if status
        .as_deref()
        .is_some_and(|value| !value.eq_ignore_ascii_case("SUCCESS"))
    {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            exit_code,
            Some(
                stderr_tail(stderr_path).unwrap_or_else(|| {
                    format!("agy reported status {}", status.unwrap_or_default())
                }),
            ),
        )
        .await;
        return;
    }
    if locate_conversation_transcript(&conversation_id).is_none() {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            exit_code,
            Some(format!(
                "agy conversation {conversation_id} has no transcript on disk"
            )),
        )
        .await;
        return;
    }
    sink.post_terminal("run_completed", exit_code, None).await;
}

/// Build a bounded one-shot argv.
///
/// `--print` consumes the next argument as its prompt, so it must come last.
/// `--print --print-timeout 60s <prompt>` makes agy treat the literal string
/// `--print-timeout` as the prompt and answer a question about its own flag,
/// leaving the real prompt as a stray positional. The ordering here is
/// load-bearing, not cosmetic.
/// The same provider argv is used by the Console launcher and release canary.
pub fn console_turn_argv(
    prompt: &str,
    model: Option<&str>,
    conversation_id: Option<&str>,
    print_timeout_secs: u64,
) -> Vec<String> {
    build_antigravity_args(prompt, model, conversation_id, print_timeout_secs)
}

fn build_antigravity_args(
    prompt: &str,
    model: Option<&str>,
    conversation_id: Option<&str>,
    print_timeout_secs: u64,
) -> Vec<String> {
    let mut args = vec![
        "--dangerously-skip-permissions".to_string(),
        "--output-format".to_string(),
        "stream-json".to_string(),
        "--print-timeout".to_string(),
        format!("{print_timeout_secs}s"),
    ];
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--model".to_string(), model.to_string()]);
    }
    if let Some(conversation) = conversation_id
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        args.extend(["--conversation".to_string(), conversation.to_string()]);
    }
    args.extend(["--print".to_string(), prompt.to_string()]);
    args
}

#[derive(Debug, Default, PartialEq, Eq)]
struct PrintOutput {
    conversation_id: Option<String>,
    status: Option<String>,
    finished: bool,
}

#[derive(Deserialize)]
struct PrintRecord {
    event: Option<String>,
    conversation_id: Option<String>,
    status: Option<String>,
    result: Option<PrintResult>,
}

#[derive(Deserialize)]
struct PrintResult {
    conversation_id: String,
    status: Option<String>,
}

/// Read only complete JSON values. A partially appended event is pending, not a
/// failed invocation. Unknown events (including step_update's nested identity)
/// cannot bind a source or complete a turn. Old single-JSON results remain
/// readable so already-deployed invocations survive an engine upgrade.
fn read_print_output(stdout_path: &Path, until_identity: bool) -> Result<PrintOutput> {
    let file = match File::open(stdout_path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok(PrintOutput::default())
        }
        Err(error) => return Err(error).context("reading Antigravity stdout"),
    };
    let records =
        serde_json::Deserializer::from_reader(BufReader::new(file)).into_iter::<PrintRecord>();
    let mut output = PrintOutput::default();
    for record in records {
        let record = match record {
            Ok(record) => record,
            Err(error) if error.is_eof() => break,
            Err(error) => return Err(error).context("parsing Antigravity stdout"),
        };
        let (id, status, finished) = match record.event.as_deref() {
            Some("init") => (
                record
                    .conversation_id
                    .context("Antigravity init has no conversation id")?,
                None,
                false,
            ),
            Some("result") => {
                let result = record
                    .result
                    .context("Antigravity result event has no result")?;
                (result.conversation_id, result.status, true)
            }
            None => (
                record
                    .conversation_id
                    .context("Antigravity legacy result has no conversation id")?,
                record.status,
                true,
            ),
            _ => continue,
        };
        validate_uuid(&id, "Antigravity conversation id")?;
        if let Some(existing) = output.conversation_id.as_ref() {
            anyhow::ensure!(
                existing == &id,
                "Antigravity stdout has conflicting conversation identities"
            );
        } else {
            output.conversation_id = Some(id);
        }
        if finished {
            output.finished = true;
            output.status = status;
        }
        if until_identity {
            break;
        }
    }
    Ok(output)
}

/// agy writes one append-only transcript per conversation under its brain dir.
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

fn is_antigravity_print_claim(claim: &crate::turn_claims::TurnClaim) -> bool {
    claim.provider == "antigravity"
        && (claim.adapter.as_deref() == Some(ANTIGRAVITY_PRINT_ADAPTER)
            || claim
                .result
                .as_ref()
                .and_then(|result| result.get("transport"))
                .and_then(Value::as_str)
                == Some(ANTIGRAVITY_PRINT_ADAPTER))
}

fn persist_transcript_binding(
    db_path: &Path,
    transcript: &Path,
    session_id: &str,
    provider_session_id: &str,
) -> Result<()> {
    let conn = crate::state::db::open_client_connection(db_path, Duration::from_millis(500))?;
    bind_source_owner(&conn, transcript, session_id, provider_session_id)
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
    claims: &[crate::turn_claims::TurnClaim],
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
        .filter(|claim| claim.provider == "antigravity")
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
            match read_print_output(&stdout, true) {
                Ok(output) => output.conversation_id,
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

impl AntigravityPrintSink {
    async fn read_and_bind_transcript(&self, turn_ended: bool) -> Result<PrintOutput> {
        let output = read_print_output(&self.stdout_path, !turn_ended)?;
        let Some(provider_session_id) = output.conversation_id.as_deref() else {
            return Ok(output);
        };
        let transcript = conversation_transcript_path(
            &antigravity_brain_root().context("HOME is unset")?,
            provider_session_id,
        );
        let db_path = self
            .local_db_path
            .as_deref()
            .context("Antigravity Console has no source binding database")?;
        persist_transcript_binding(db_path, &transcript, &self.session_id, provider_session_id)?;
        crate::turn_claims::default_registry()?.mark_provider_binding(
            &self.run_id,
            provider_session_id,
            Some(&transcript.to_string_lossy()),
        )?;
        self.post_binding(provider_session_id, &transcript).await;
        if transcript.is_file() {
            self.wake_transcript_shipper(&transcript, provider_session_id, turn_ended)
                .await;
        }
        Ok(output)
    }

    async fn post_binding(&self, provider_session_id: &str, transcript: &Path) {
        self.post_events(vec![json!({
            "runtime_key": format!("antigravity:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "antigravity",
            "device_id": self.machine_name,
            "source": ANTIGRAVITY_PRINT_ADAPTER,
            "kind": "binding_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!(
                "antigravity-print:{}:{}:binding",
                self.session_id, self.launch_id
            ),
            "payload": {
                "provider_session_id": provider_session_id,
                "source_path": transcript.to_string_lossy(),
                "managed_transport": ANTIGRAVITY_PRINT_ADAPTER,
                "execution_lifetime": "one_shot"
            }
        })])
        .await;
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        let observed_at = Utc::now();
        self.persist_local_phase(phase, tool_name.clone(), observed_at);
        self.post_events(vec![json!({
            "runtime_key": format!("antigravity:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "antigravity",
            "device_id": self.machine_name,
            "source": ANTIGRAVITY_PRINT_ADAPTER,
            "kind": "phase_signal",
            "phase": phase,
            "tool_name": tool_name,
            "occurred_at": observed_at.to_rfc3339(),
            "dedupe_key": format!(
                "antigravity-print:{}:{}:phase:{phase}",
                self.session_id, self.run_id
            ),
            "payload": {
                "managed_transport": ANTIGRAVITY_PRINT_ADAPTER,
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
            "runtime_key": format!("antigravity:{}", self.session_id),
            "session_id": self.session_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "provider": "antigravity",
            "device_id": self.machine_name,
            "source": ANTIGRAVITY_PRINT_ADAPTER,
            "kind": "terminal_signal",
            "occurred_at": Utc::now().to_rfc3339(),
            "dedupe_key": format!(
                "antigravity-print:{}:{}:terminal",
                self.session_id, self.run_id
            ),
            "payload": {
                "managed_transport": ANTIGRAVITY_PRINT_ADAPTER,
                "execution_lifetime": "one_shot",
                "terminal_state": terminal_state,
                "terminal_reason": terminal_state,
                "terminal_source": ANTIGRAVITY_PRINT_ADAPTER,
                "exit_code": exit_code,
                "stderr_tail": stderr,
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
                eprintln!("[antigravity-print] open local phase DB failed: {err}");
                return;
            }
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "antigravity".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: ANTIGRAVITY_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        if let Err(err) = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal)
        {
            eprintln!(
                "[antigravity-print] persist local phase failed for {}: {err}",
                self.session_id
            );
        }
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

    async fn post_events(&self, events: Vec<Value>) {
        for event in events {
            if let Err(error) =
                crate::outbox::enqueue_runtime_event(&self.runtime_events_outbox_dir, &event)
            {
                eprintln!("[antigravity-print] runtime outbox write failed: {error}");
            }
        }
    }
}

/// agy leaks `run_command` children on signal, so this provider leans on the
/// shared helper's "what survived SIGKILL" reporting more than most.
async fn cleanup_process_group(process_group_id: Option<i32>) {
    crate::console_adapter::cleanup_process_group("antigravity-print", process_group_id).await;
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
        let _guard = runtime.block_on(crate::console_adapter::longhouse_home_test_guard());
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
                // before output setup, before exec, mid-init, and after init.
                for output in [
                    None,
                    Some(String::new()),
                    Some("{\"event\":\"init\",\"conversation_id\":\"".to_string()),
                    Some(format!("{{\"event\":\"init\",\"conversation_id\":\"{native_id}\"}}\n")),
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
                    let has_identity = read_print_output(&stdout, true).unwrap().conversation_id.is_some();
                    assert_eq!(
                        bind_discovered_source(&conn, &shadow, &registry.list_all().unwrap(), &agent_dir).unwrap(),
                        if has_identity { SourceOwnership::Unclaimed } else { SourceOwnership::Pending },
                    );
                    assert_eq!(recover_antigravity_print_turns("test", Some(db_path.clone())).await.unwrap(), 0);
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
        let _guard = runtime.block_on(crate::console_adapter::longhouse_home_test_guard());
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
                // The child owns the durable output, but the engine died
                // before persisting any PID, adapter or provider identity.
                let mut child = Command::new("/bin/sh")
                    .arg("-c")
                    .arg(format!(
                        "read first; printf '%s\\n' '{{\"event\":\"init\",\"conversation_id\":\"{native_id}\"}}'; read second; printf '%s\\n' '{{\"event\":\"result\",\"result\":{{\"conversation_id\":\"{native_id}\",\"status\":\"SUCCESS\"}}}}'"
                    ))
                    .stdin(Stdio::piped())
                    .stdout(Stdio::from(private_output_file(&stdout).unwrap()))
                    .kill_on_drop(true)
                    .spawn().unwrap();
                assert_eq!(recover_antigravity_print_turns("test", Some(db_path.clone())).await.unwrap(), 1);
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
        let _guard = runtime.block_on(crate::console_adapter::longhouse_home_test_guard());
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
                AntigravityPrintSink {
                    session_id: session_id.clone(),
                    thread_id,
                    turn_id: None,
                    run_id,
                    client_request_id: None,
                    launch_id: Uuid::new_v4().to_string(),
                    process_group_id: None,
                    stdout_path: run_dir.join("stdout.log"),
                    machine_name: "test".to_string(),
                    local_db_path: Some(db_path.clone()),
                    runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir().unwrap(),
                }
            };
            let sink = make_sink();
            std::fs::write(&sink.stdout_path, format!("{{\"event\":\"init\",\"conversation_id\":\"{provider_id}\"}}\n")).unwrap();
            let early = sink.read_and_bind_transcript(false).await.unwrap();
            assert!(!early.finished);
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
            let result = format!("{{\"event\":\"result\",\"result\":{{\"conversation_id\":\"{provider_id}\",\"status\":\"SUCCESS\",\"response\":\"done\"}}}}\n");
            OpenOptions::new().append(true).open(&sink.stdout_path).unwrap().write_all(result.as_bytes()).unwrap();
            settle_antigravity_claim(&sink, false, Some(0), &sink.stdout_path.with_file_name("stderr.log")).await;
            let wake = receive_wake(&listener).await;
            assert_eq!(wake["phase"], "idle");
            assert_eq!(wake["wake_reason"], "turn_completed");
            assert_eq!(registry.read(&sink.run_id).unwrap().state, "terminal");
            let terminal = read_outbox_events(&sink.runtime_events_outbox_dir).into_iter()
                .find(|event| event["run_id"] == sink.run_id && event["kind"] == "terminal_signal").unwrap();
            assert_eq!(terminal["payload"]["terminal_state"], "run_completed");

            let legacy = make_sink();
            registry.mark_provider_binding(&legacy.run_id, &provider_id, Some(&mirror.to_string_lossy())).unwrap();
            std::fs::write(&legacy.stdout_path, format!("{{\"conversation_id\":\"{provider_id}\",\"status\":\"SUCCESS\",\"response\":\"done\"}}")).unwrap();
            settle_antigravity_claim(&legacy, false, Some(0), &legacy.stdout_path.with_file_name("stderr.log")).await;
            receive_wake(&listener).await;
            let terminal = read_outbox_events(&legacy.runtime_events_outbox_dir).into_iter()
                .find(|event| event["run_id"] == legacy.run_id && event["kind"] == "terminal_signal").unwrap();
            assert_eq!(terminal["payload"]["terminal_state"], "run_completed");
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

            // A launch that terminates without init fails rather than claiming
            // success, and its pending hold must not outlive the terminal claim.
            let missing = make_sink();
            let unrelated = conversation_transcript_path(&antigravity_brain_root().unwrap(), &Uuid::new_v4().to_string());
            assert_eq!(bind_discovered_source(&conn, &unrelated, &registry.list_all().unwrap(), &agent_dir).unwrap(), SourceOwnership::Pending);
            settle_antigravity_claim(&missing, false, Some(0), &missing.stdout_path.with_file_name("stderr.log")).await;
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

    #[tokio::test]
    async fn a_cancelled_turn_settles_as_cancelled_not_completed() {
        // Regression: the first cut of this adapter never read the claim, so an
        // interrupted turn reported run_completed and the caller saw its own
        // cancel succeed as a normal answer.
        let dir = std::env::temp_dir().join(format!("agy-cancel-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let stdout = dir.join("stdout.log");
        std::fs::write(&stdout, br#"{"conversation_id":"c-1","status":"SUCCESS"}"#).unwrap();
        let stderr = dir.join("stderr.log");
        std::fs::write(&stderr, b"").unwrap();
        let sink = AntigravityPrintSink {
            session_id: Uuid::new_v4().to_string(),
            thread_id: Uuid::new_v4().to_string(),
            turn_id: None,
            run_id: Uuid::new_v4().to_string(),
            client_request_id: None,
            launch_id: Uuid::new_v4().to_string(),
            process_group_id: None,
            stdout_path: stdout,
            machine_name: "test".to_string(),
            local_db_path: None,
            runtime_events_outbox_dir: dir.join("outbox"),
        };
        settle_antigravity_claim(&sink, true, Some(0), &stderr).await;
        let events = read_outbox_events(&dir.join("outbox"));
        let terminal = events
            .iter()
            .find(|event| event["kind"] == "terminal_signal")
            .unwrap();
        assert_eq!(terminal["payload"]["terminal_state"], "run_cancelled");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[tokio::test]
    async fn a_nonzero_exit_fails_the_turn_even_with_a_readable_result() {
        let dir = std::env::temp_dir().join(format!("agy-exit-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let stdout = dir.join("stdout.log");
        std::fs::write(&stdout, br#"{"conversation_id":"c-1","status":"ERROR"}"#).unwrap();
        let stderr = dir.join("stderr.log");
        std::fs::write(&stderr, b"agy: model call failed\n").unwrap();
        let sink = AntigravityPrintSink {
            session_id: Uuid::new_v4().to_string(),
            thread_id: Uuid::new_v4().to_string(),
            turn_id: None,
            run_id: Uuid::new_v4().to_string(),
            client_request_id: None,
            launch_id: Uuid::new_v4().to_string(),
            process_group_id: None,
            stdout_path: stdout,
            machine_name: "test".to_string(),
            local_db_path: None,
            runtime_events_outbox_dir: dir.join("outbox"),
        };
        settle_antigravity_claim(&sink, false, Some(1), &stderr).await;
        let events = read_outbox_events(&dir.join("outbox"));
        let terminal = events
            .iter()
            .find(|event| event["kind"] == "terminal_signal")
            .unwrap();
        assert_eq!(terminal["payload"]["terminal_state"], "run_failed");
        assert_eq!(terminal["payload"]["exit_code"], 1);
        let _ = std::fs::remove_dir_all(&dir);
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

    #[test]
    fn print_flag_is_last_so_it_takes_the_prompt_as_its_value() {
        // The regression this guards is real and was observed against agy
        // 1.1.16: with `--print` ahead of `--print-timeout`, agy answered a
        // question about its own flag instead of running the prompt.
        let args = build_antigravity_args("do the thing", None, None, 600);
        let print_index = args.iter().position(|arg| arg == "--print").unwrap();
        assert_eq!(args[print_index + 1], "do the thing");
        assert_eq!(print_index, args.len() - 2);
        assert!(args.iter().any(|arg| arg == "--print-timeout"));
        assert!(
            args.iter()
                .position(|arg| arg == "--print-timeout")
                .unwrap()
                < print_index
        );
    }

    #[test]
    fn argv_is_a_bounded_one_shot_with_structured_output() {
        let args = build_antigravity_args("hello", None, None, 120);
        assert!(args.contains(&"--dangerously-skip-permissions".to_string()));
        assert!(args.contains(&"--output-format".to_string()));
        assert!(args.contains(&"stream-json".to_string()));
        assert!(args.contains(&"120s".to_string()));
        assert!(!args.contains(&"--input-format".to_string()));
    }

    #[test]
    fn continuing_a_conversation_passes_the_provider_thread_id() {
        let args = build_antigravity_args("next", None, Some("conv-1"), 600);
        let index = args.iter().position(|arg| arg == "--conversation").unwrap();
        assert_eq!(args[index + 1], "conv-1");
    }

    #[test]
    fn blank_model_and_conversation_are_omitted_rather_than_passed_empty() {
        let args = build_antigravity_args("x", Some("   "), Some(""), 600);
        assert!(!args.contains(&"--model".to_string()));
        assert!(!args.contains(&"--conversation".to_string()));
    }

    #[test]
    fn structured_init_binds_before_result_and_legacy_results_remain_readable() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("stdout.log");
        let id = "5f62a636-1412-4afe-9cfd-a5079e0a0366";
        let init = format!(
            "{{\"event\":\"init\",\"conversation_id\":\"{id}\",\"init\":{{\"cwd\":\"/tmp\"}}}}\n"
        );
        std::fs::write(&path, &init).unwrap();
        let early = read_print_output(&path, true).unwrap();
        assert_eq!(early.conversation_id.as_deref(), Some(id));
        assert!(!early.finished);
        assert!(!read_print_output(&path, false).unwrap().finished);

        let result = format!("{{\"event\":\"result\",\"result\":{{\"conversation_id\":\"{id}\",\"status\":\"SUCCESS\",\"response\":\"done\"}}}}\n");
        std::fs::write(&path, format!("{init}{result}")).unwrap();
        let current = read_print_output(&path, false).unwrap();
        assert_eq!(current.conversation_id.as_deref(), Some(id));
        assert!(current.finished);
        assert_eq!(current.status.as_deref(), Some("SUCCESS"));

        std::fs::write(
            &path,
            format!(
                "{{\"conversation_id\":\"{id}\",\"status\":\"SUCCESS\",\"response\":\"done\"}}"
            ),
        )
        .unwrap();
        assert_eq!(read_print_output(&path, false).unwrap(), current);
    }

    #[test]
    fn partial_stdout_waits_but_invalid_or_conflicting_identity_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("stdout.log");
        assert_eq!(
            read_print_output(&path, false).unwrap(),
            PrintOutput::default()
        );
        std::fs::write(&path, b"{\"event\":\"init\",\"conversation_id\":\"").unwrap();
        assert_eq!(
            read_print_output(&path, false).unwrap(),
            PrintOutput::default()
        );
        std::fs::write(&path, b"{\"event\":\"step_update\",\"step_update\":{\"conversation_id\":\"5f62a636-1412-4afe-9cfd-a5079e0a0366\"}}\n").unwrap();
        assert_eq!(
            read_print_output(&path, false).unwrap(),
            PrintOutput::default()
        );
        std::fs::write(
            &path,
            b"{\"event\":\"init\",\"conversation_id\":\"invalid\"}\n",
        )
        .unwrap();
        assert!(read_print_output(&path, false).is_err());
        std::fs::write(&path, concat!(
            "{\"event\":\"init\",\"conversation_id\":\"5f62a636-1412-4afe-9cfd-a5079e0a0366\"}\n",
            "{\"event\":\"result\",\"result\":{\"conversation_id\":\"ad8d7e16-26e0-4add-b11d-a7841890718d\",\"status\":\"SUCCESS\"}}\n"
        )).unwrap();
        assert!(read_print_output(&path, false).is_err());
    }
}
