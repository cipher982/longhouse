//! OpenCode Console turns through stock `opencode run --format json`.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
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

pub const OPENCODE_RUN_ADAPTER: &str = "opencode_run";
const STDERR_TAIL_LINES: usize = 40;

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
    cwd: String,
    local_db_path: Option<PathBuf>,
    runtime_events_outbox_dir: PathBuf,
}

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

    let run_dir = crate::config::get_agent_dir()?
        .join("opencode-console")
        .join(&config.session_id)
        .join(&config.run_id);
    std::fs::create_dir_all(&run_dir)?;
    set_private_dir(&run_dir)?;
    let stdout_path = run_dir.join("stdout.jsonl");
    let stderr_path = run_dir.join("stderr.log");
    let stdout_file = private_output_file(&stdout_path)?;
    let stderr_file = private_output_file(&stderr_path)?;

    let mut args = vec![
        "run".to_string(),
        "--format".to_string(),
        "json".to_string(),
        "--pure".to_string(),
        "--auto".to_string(),
        "--title".to_string(),
        "Longhouse Console".to_string(),
    ];
    if let Some(provider_thread_id) = resume_provider_thread_id.as_deref() {
        args.extend(["--session".to_string(), provider_thread_id.to_string()]);
    }
    if let Some(model) = normalized_optional(&config.model) {
        args.extend(["--model".to_string(), model]);
    }
    args.push(config.prompt.clone());
    validate_console_argv(&args)?;
    let argv = std::iter::once(config.opencode_bin.clone())
        .chain(args.iter().cloned())
        .collect::<Vec<_>>();

    let mut command = Command::new(&config.opencode_bin);
    command
        .args(&args)
        .current_dir(&config.cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .env_remove("OPENCODE_CONFIG")
        .env_remove("OPENCODE_CONFIG_CONTENT")
        .env_remove("OPENCODE_SERVER_PASSWORD")
        .env_remove("OPENCODE_SERVER_USERNAME")
        .env_remove("LONGHOUSE_SESSION_ID")
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
            rollback_binding(&state_root, &config.session_id, &launch_id);
            return Err(error).with_context(|| format!("spawning `{}` run", config.opencode_bin));
        }
    };
    let pid = child.id().context("opencode run returned no pid")?;
    let process_group_id =
        i32::try_from(pid).context("OpenCode pid exceeds process-group range")?;
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
        cwd: config.cwd.to_string_lossy().to_string(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
    };
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
    let monitor_path = stdout_path.clone();
    let monitor_stderr = stderr_path.clone();
    tokio::spawn(async move {
        monitor_opencode_run(&mut child, &monitor_path, &monitor_stderr, sink, lock).await;
    });

    Ok(OpenCodeRunSummary {
        session_id: config.session_id,
        thread_id: config.thread_id,
        run_id: config.run_id,
        provider_thread_id: resume_provider_thread_id,
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        argv,
    })
}

pub async fn recover_opencode_run_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let mut recovered = 0;
    for claim in registry.list_nonterminal()? {
        if claim.adapter.as_deref() != Some(OPENCODE_RUN_ADAPTER) || claim.state != "spawned" {
            continue;
        }
        let Some(stdout_path) = claim.stdout_path.as_deref().map(PathBuf::from) else {
            let _ = registry.mark_terminal(
                &claim.run_id,
                "run_failed",
                Some("OpenCode Console claim has no stdout path".to_string()),
            );
            continue;
        };
        let stderr_path = claim
            .stderr_path
            .as_deref()
            .map(PathBuf::from)
            .unwrap_or_else(|| stdout_path.with_file_name("stderr.log"));
        let result = claim.result.as_ref().and_then(Value::as_object);
        let cwd = result
            .and_then(|value| value.get("cwd"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
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
            cwd,
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        if claim_process_is_live(&claim) {
            let lock_key = claim
                .provider_thread_id
                .as_deref()
                .unwrap_or(&claim.session_id);
            let lock = acquire_turn_lock(&opencode_console_root()?, lock_key)?;
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

pub fn interrupt_opencode_run_turn(run_id: &str, session_id: &str) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id || claim.provider != "opencode" {
        anyhow::bail!("OpenCode Console turn claim does not match the requested session");
    }
    if claim.adapter.as_deref() != Some(OPENCODE_RUN_ADAPTER) || claim.state != "spawned" {
        anyhow::bail!("OpenCode Console turn is not active");
    }
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
    registry.mark_cancel_requested(run_id)?;
    let result = unsafe { libc::killpg(pgid, libc::SIGINT) };
    if result != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() != Some(libc::ESRCH) {
            return Err(error).context("interrupting OpenCode Console process group");
        }
    }
    Ok(())
}

async fn monitor_opencode_run(
    child: &mut Child,
    stdout_path: &Path,
    stderr_path: &Path,
    sink: OpenCodeRunSink,
    _lock: File,
) {
    sink.post_phase("thinking", None).await;
    let mut offset = 0_u64;
    let mut pending = Vec::new();
    let mut seq = 0_u64;
    let mut provider_thread_id = sink.expected_provider_thread_id.clone();
    loop {
        match project_growth(
            stdout_path,
            &mut offset,
            &mut pending,
            &mut seq,
            &mut provider_thread_id,
            &sink,
        )
        .await
        {
            Ok(()) => {}
            Err(error) => {
                sink.post_terminal(
                    "run_failed",
                    None,
                    Some(error.to_string()),
                    provider_thread_id.as_deref(),
                )
                .await;
                return;
            }
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                tokio::time::sleep(Duration::from_millis(150)).await;
                let projection = project_growth(
                    stdout_path,
                    &mut offset,
                    &mut pending,
                    &mut seq,
                    &mut provider_thread_id,
                    &sink,
                )
                .await;
                let claim = crate::turn_claims::default_registry()
                    .and_then(|registry| registry.read(&sink.run_id))
                    .ok();
                let cancel_requested = claim
                    .as_ref()
                    .and_then(|item| item.cancel_requested_at.as_ref())
                    .is_some();
                let (terminal, error) = if let Err(error) = projection {
                    ("run_failed", Some(error.to_string()))
                } else if cancel_requested {
                    ("run_cancelled", None)
                } else if !status.success() {
                    ("run_failed", stderr_tail(stderr_path))
                } else if provider_thread_id.is_none() {
                    (
                        "run_failed",
                        Some("OpenCode Console completed without a native sessionID".to_string()),
                    )
                } else {
                    ("run_completed", None)
                };
                if terminal != "run_completed" {
                    cleanup_process_group(sink.process_group_id).await;
                }
                sink.post_terminal(
                    terminal,
                    status.code(),
                    error,
                    provider_thread_id.as_deref(),
                )
                .await;
                return;
            }
            Ok(None) => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(error) => {
                sink.post_terminal(
                    "run_failed",
                    None,
                    Some(error.to_string()),
                    provider_thread_id.as_deref(),
                )
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
    sink: OpenCodeRunSink,
    _lock: File,
) {
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let mut provider_thread_id = claim.provider_thread_id.clone();
    loop {
        let projection = project_growth(
            &stdout_path,
            &mut offset,
            &mut pending,
            &mut seq,
            &mut provider_thread_id,
            &sink,
        )
        .await;
        if projection.is_err() || !claim_process_is_live(&claim) {
            let current = crate::turn_claims::default_registry()
                .and_then(|registry| registry.read(&claim.run_id))
                .ok();
            let cancelled = current
                .as_ref()
                .and_then(|item| item.cancel_requested_at.as_ref())
                .is_some();
            let (terminal, error) = if let Err(error) = projection {
                ("run_failed", Some(error.to_string()))
            } else if cancelled {
                ("run_cancelled", None)
            } else if provider_thread_id.is_some() && stream_has_successful_finish(&stdout_path) {
                ("run_completed", None)
            } else {
                (
                    "run_failed",
                    Some(
                        "OpenCode Console process exited without observed terminal status"
                            .to_string(),
                    ),
                )
            };
            cleanup_process_group(sink.process_group_id).await;
            sink.post_terminal(
                terminal,
                None,
                error.or_else(|| stderr_tail(&stderr_path)),
                provider_thread_id.as_deref(),
            )
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
    sink: &OpenCodeRunSink,
) {
    let mut offset = claim.projected_stdout_offset;
    let mut pending = Vec::new();
    let mut seq = claim.projected_seq;
    let mut provider_thread_id = claim.provider_thread_id.clone();
    let projection = project_growth(
        stdout_path,
        &mut offset,
        &mut pending,
        &mut seq,
        &mut provider_thread_id,
        sink,
    )
    .await;
    let cancelled = claim.cancel_requested_at.is_some();
    let terminal = if cancelled {
        "run_cancelled"
    } else if provider_thread_id.is_some() && stream_has_successful_finish(stdout_path) {
        "run_completed"
    } else {
        "run_failed"
    };
    cleanup_process_group(sink.process_group_id).await;
    sink.post_terminal(
        terminal,
        None,
        projection
            .err()
            .map(|error| error.to_string())
            .or_else(|| stderr_tail(stderr_path)),
        provider_thread_id.as_deref(),
    )
    .await;
}

async fn project_growth(
    path: &Path,
    offset: &mut u64,
    pending: &mut Vec<u8>,
    seq: &mut u64,
    provider_thread_id: &mut Option<String>,
    sink: &OpenCodeRunSink,
) -> Result<()> {
    let starting_offset = *offset;
    let starting_seq = *seq;
    for bytes in read_growth(path, offset, pending)? {
        *seq += 1;
        match serde_json::from_slice::<Value>(&bytes) {
            Ok(event) => {
                if let Some(observed) = event_provider_thread_id(&event) {
                    validate_provider_thread_id(&observed)?;
                    if let Some(expected) = provider_thread_id.as_deref() {
                        if expected != observed {
                            anyhow::bail!(
                                "OpenCode stream sessionID {observed} did not match expected {expected}"
                            );
                        }
                    } else {
                        promote_binding(sink, &observed)?;
                        *provider_thread_id = Some(observed.clone());
                        let registry = crate::turn_claims::default_registry()?;
                        registry.mark_provider_binding(&sink.run_id, &observed, None)?;
                        sink.post_binding(&observed).await;
                    }
                }
                sink.post_stream_event(*seq, event, provider_thread_id.as_deref())
                    .await;
            }
            Err(error) => sink.post_decode_gap(*seq, &error.to_string()).await,
        }
    }
    if *offset != starting_offset || *seq != starting_seq {
        let complete_offset = offset.saturating_sub(pending.len() as u64);
        crate::turn_claims::default_registry()?.mark_projection_checkpoint(
            &sink.run_id,
            complete_offset,
            *seq,
        )?;
    }
    Ok(())
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

    async fn post_decode_gap(&self, seq: u64, error: &str) {
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
            "dedupe_key": format!("opencode-run:{}:{}:decode-gap:{seq}", self.session_id, self.run_id),
            "payload": {"progress_kind": "opencode_run_decode_gap", "seq": seq, "error": error}
        })]).await;
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
        let Ok(conn) = crate::state::db::open_db(Some(db_path)) else {
            return;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "opencode".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: OPENCODE_RUN_ADAPTER.to_string(),
            observed_at,
        };
        let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
        let managed = crate::state::managed_session_state::ManagedSessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "opencode".to_string(),
            workspace_path: Some(self.cwd.clone()),
            phase_kind: phase.to_string(),
            tool_name,
            phase_source: OPENCODE_RUN_ADAPTER.to_string(),
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
                eprintln!("[opencode-run] runtime outbox write failed: {error}");
            }
        }
    }
}

fn event_provider_thread_id(event: &Value) -> Option<String> {
    let candidates = [
        event.get("sessionID"),
        event.get("session_id"),
        event.get("part").and_then(|part| part.get("sessionID")),
        event
            .get("properties")
            .and_then(|props| props.get("sessionID")),
    ];
    candidates
        .into_iter()
        .flatten()
        .find_map(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn stream_has_successful_finish(path: &Path) -> bool {
    let Ok(text) = std::fs::read_to_string(path) else {
        return false;
    };
    text.lines().any(|line| {
        serde_json::from_str::<Value>(line).is_ok_and(|event| {
            event.get("type").and_then(Value::as_str) == Some("step_finish")
                && event
                    .get("part")
                    .and_then(|part| part.get("reason"))
                    .and_then(Value::as_str)
                    == Some("stop")
        })
    })
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

fn validate_console_argv(args: &[String]) -> Result<()> {
    if !args.iter().any(|arg| arg == "--auto") {
        anyhow::bail!("OpenCode Console argv must include --auto");
    }
    for forbidden in ["--continue", "--attach", "--dangerously-skip-permissions"] {
        if args.iter().any(|arg| arg == forbidden) {
            anyhow::bail!("OpenCode Console argv must not include {forbidden}");
        }
    }
    Ok(())
}

fn validate_provider_thread_id(value: &str) -> Result<()> {
    if !value.starts_with("ses_") || value.len() <= 4 {
        anyhow::bail!("OpenCode provider session id must be an opaque ses_... identity");
    }
    Ok(())
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
    fn opencode_console_argv_requires_current_permission_and_explicit_resume_contract() {
        let valid = vec![
            "run".to_string(),
            "--format".to_string(),
            "json".to_string(),
            "--auto".to_string(),
            "--session".to_string(),
            "ses_native".to_string(),
        ];
        validate_console_argv(&valid).unwrap();
        for forbidden in ["--continue", "--attach", "--dangerously-skip-permissions"] {
            let mut invalid = valid.clone();
            invalid.push(forbidden.to_string());
            assert!(validate_console_argv(&invalid).is_err());
        }
    }

    #[test]
    fn native_session_ids_are_opaque_not_uuids() {
        validate_provider_thread_id("ses_01JZZTEST").unwrap();
        assert!(validate_provider_thread_id("550e8400-e29b-41d4-a716-446655440000").is_err());
        assert!(validate_provider_thread_id("ses_").is_err());
    }

    #[test]
    fn event_session_identity_accepts_stock_jsonl_shapes() {
        assert_eq!(
            event_provider_thread_id(&json!({"type":"text","sessionID":"ses_top"})).as_deref(),
            Some("ses_top")
        );
        assert_eq!(
            event_provider_thread_id(&json!({"type":"tool_use","part":{"sessionID":"ses_part"}}))
                .as_deref(),
            Some("ses_part")
        );
    }

    #[test]
    fn stock_1_17_20_jsonl_fixture_keeps_native_identity_and_tool_shape() {
        let events = include_str!("../tests/fixtures/opencode/run-1.17.20.jsonl")
            .lines()
            .map(|line| serde_json::from_str::<Value>(line).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(events.len(), 4);
        assert!(events.iter().all(|event| {
            event_provider_thread_id(event).as_deref() == Some("ses_fixture_11720")
        }));
        assert_eq!(events[1]["part"]["callID"], "call_fixture");
        assert_eq!(events[2]["part"]["state"]["status"], "completed");
        assert_eq!(events[2]["part"]["state"]["output"], "fixture");
        assert_eq!(events[3]["part"]["reason"], "stop");
    }

    #[test]
    fn recovered_stream_requires_explicit_success_finish_evidence() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("stdout.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"text\",\"sessionID\":\"ses_test\",\"part\":{\"type\":\"text\",\"text\":\"done\"}}\n",
        )
        .unwrap();
        assert!(!stream_has_successful_finish(&path));
        std::fs::write(
            &path,
            "{\"type\":\"step_finish\",\"sessionID\":\"ses_test\",\"part\":{\"reason\":\"stop\"}}\n",
        )
        .unwrap();
        assert!(stream_has_successful_finish(&path));
    }

    #[tokio::test]
    #[ignore = "requires an authenticated stock opencode and spends provider tokens"]
    async fn installed_opencode_completes_and_resumes_through_production_console_adapter() {
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
        }
        let opencode_bin =
            std::env::var("LONGHOUSE_OPENCODE_BIN").unwrap_or_else(|_| "opencode".to_string());
        let help = Command::new(&opencode_bin)
            .args(["run", "--help"])
            .output()
            .await
            .unwrap();
        let help_text = format!(
            "{}{}",
            String::from_utf8_lossy(&help.stdout),
            String::from_utf8_lossy(&help.stderr)
        );
        assert!(help.status.success() && help_text.contains("--auto"));
        assert!(!help_text.contains("--dangerously-skip-permissions"));
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
                        "stdout={}\nstderr={}",
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
        assert!(second
            .argv
            .windows(2)
            .any(|pair| pair == ["--session", provider_thread_id.as_str()]));

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
            turn_id: Some(interrupt_turn_id),
            run_id: interrupt_run_id.clone(),
            client_request_id: Some("canary-interrupt".to_string()),
            cwd: temp.path().to_path_buf(),
            opencode_bin: opencode_bin.clone(),
            prompt: "Use the bash tool to run exactly: sleep 30. Do not finish before the command finishes."
                .to_string(),
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
            let stdout = std::fs::read_to_string(&interrupted.stdout_path).unwrap_or_default();
            if stdout.contains("\"type\":\"tool_use\"") || stdout.contains("\"type\": \"tool_use\"")
            {
                break;
            }
            assert!(
                tokio::time::Instant::now() < tool_deadline,
                "OpenCode did not begin the interrupt canary tool"
            );
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
        interrupt_opencode_run_turn(&interrupt_run_id, &session_id).unwrap();
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
