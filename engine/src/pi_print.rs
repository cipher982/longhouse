//! Pi Console turns through stock `pi -p`.
//!
//! Turn-scoped one-shot adapter: one bounded `pi -p <prompt> --provider
//! <provider> --model <model> --session-dir <dir>` invocation per Console
//! turn. Pi (npm `@earendil-works/pi-coding-agent`) runs a single turn and
//! exits, writing its append-only session JSONL into the `--session-dir` at
//! completion. The produced `<ts>_<uuidv7>.jsonl` header pins the provider
//! session id; the engine binds that transcript to the Longhouse session and
//! wakes the daemon so the pi JSONL gets shipped, mirroring the other one-shot
//! console adapters.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::Write;
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

pub const PI_PRINT_ADAPTER: &str = "pi_print";
pub const DEFAULT_PI_BIN: &str = "pi";
const STDERR_TAIL_LINES: usize = 40;

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
    /// Pi's upstream provider id (e.g. `openrouter`), passed through `--provider`.
    pub provider: Option<String>,
    pub model: Option<String>,
    /// Where pi writes its session JSONL. Defaults to
    /// `<agent-dir>/pi-console/<session_id>/sessions` when absent so discovery
    /// can route the produced transcripts.
    pub session_dir: Option<PathBuf>,
    pub permission_mode: String,
    pub machine_name: String,
    pub local_db_path: Option<PathBuf>,
}

#[derive(Debug, Serialize)]
pub struct PiPrintRunSummary {
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
    session_dir: PathBuf,
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
    if config.permission_mode != "bypass" {
        anyhow::bail!("Pi Console currently supports bypass permission mode only");
    }
    let launch_id = Uuid::new_v4().to_string();
    let session_dir = config
        .session_dir
        .clone()
        .unwrap_or_else(|| pi_console_session_root(&config.session_id));
    std::fs::create_dir_all(&session_dir)?;
    set_private_dir(&session_dir)?;

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

    let args = build_pi_args(
        &config.prompt,
        config.provider.as_deref(),
        config.model.as_deref(),
        &session_dir,
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
        .stderr(Stdio::from(stderr_file))
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .env("LONGHOUSE_MANAGED_PROVIDER", "pi")
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
            return Err(error).with_context(|| format!("spawning `{}` -p", config.pi_bin))
        }
    };
    let pid = child.id().context("pi -p returned no pid")?;
    let process_group_id = i32::try_from(pid).context("Pi pid exceeds process-group range")?;
    let sink = PiPrintSink {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        turn_id: config.turn_id.clone(),
        run_id: config.run_id.clone(),
        client_request_id: config.client_request_id.clone(),
        launch_id: launch_id.clone(),
        process_group_id: Some(process_group_id),
        session_dir: session_dir.clone(),
        machine_name: config.machine_name.clone(),
        local_db_path: config.local_db_path.clone(),
        runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
    };
    let result = json!({
        "session_id": config.session_id,
        "thread_id": config.thread_id,
        "run_id": config.run_id,
        "provider": "pi",
        "transport": PI_PRINT_ADAPTER,
        "provider_thread_id": Value::Null,
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
        None,
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
        provider_thread_id: None,
        launch_id,
        pid,
        process_group_id,
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        session_dir: session_dir.to_string_lossy().to_string(),
        argv,
    })
}

pub async fn recover_pi_print_turns(
    machine_name: &str,
    local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
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
            .unwrap_or_else(|| pi_console_session_root(&claim.session_id));
        let sink = PiPrintSink {
            session_id: claim.session_id.clone(),
            thread_id: claim.thread_id.clone(),
            turn_id: claim.turn_id.clone(),
            run_id: claim.run_id.clone(),
            client_request_id: claim.client_request_id.clone(),
            launch_id: claim.launch_id.clone().unwrap_or_default(),
            process_group_id: claim.process_group_id,
            session_dir,
            machine_name: machine_name.to_string(),
            local_db_path: local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        };
        if claim_process_is_live(&claim) {
            tokio::spawn(async move {
                monitor_recovered_claim(claim, stdout_path, stderr_path, sink).await;
            });
            recovered += 1;
        } else {
            settle_recovered_dead_claim(&claim, &stderr_path, &sink).await;
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
    let pid = claim
        .pid
        .context("Pi Console turn has no provider pid")?;
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

async fn monitor_pi_print(child: &mut Child, stderr_path: &Path, sink: PiPrintSink) {
    sink.post_phase("thinking", None).await;
    let status = loop {
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
    // Pi writes its session JSONL at turn completion; give the filesystem a
    // beat to settle the rename before locating it.
    tokio::time::sleep(Duration::from_millis(150)).await;
    let claim = crate::turn_claims::default_registry()
        .and_then(|registry| registry.read(&sink.run_id))
        .ok();
    let cancel_requested = claim
        .as_ref()
        .and_then(|item| item.cancel_requested_at.as_ref())
        .is_some();
    if cancel_requested {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal("run_cancelled", status.code(), None).await;
        return;
    }
    if !status.success() {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal("run_failed", status.code(), stderr_tail(stderr_path))
            .await;
        return;
    }
    let Some(transcript) = locate_session_transcript(&sink.session_dir) else {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            status.code(),
            Some("Pi completed without writing a session transcript".to_string()),
        )
        .await;
        return;
    };
    let Some(provider_session_id) = read_session_header_id(&transcript) else {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            status.code(),
            Some("Pi session transcript has no header session id".to_string()),
        )
        .await;
        return;
    };
    sink.bind_transcript(&transcript, &provider_session_id).await;
    sink.post_binding(&provider_session_id, &transcript).await;
    sink.wake_transcript_shipper(&transcript, &provider_session_id).await;
    sink.post_terminal("run_completed", status.code(), None).await;
}

async fn monitor_recovered_claim(
    claim: crate::turn_claims::TurnClaim,
    _stdout_path: PathBuf,
    stderr_path: PathBuf,
    sink: PiPrintSink,
) {
    loop {
        if !claim_process_is_live(&claim) {
            let cancel_requested = crate::turn_claims::default_registry()
                .and_then(|registry| registry.read(&claim.run_id))
                .ok()
                .and_then(|current| current.cancel_requested_at)
                .is_some();
            settle_pi_claim(&sink, cancel_requested, &stderr_path).await;
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
    if claim.process_group_is_from_this_boot() {
        cleanup_process_group(sink.process_group_id).await;
    }
    settle_pi_claim(sink, claim.cancel_requested_at.is_some(), stderr_path).await;
}

async fn settle_pi_claim(sink: &PiPrintSink, cancel_requested: bool, stderr_path: &Path) {
    if cancel_requested {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal("run_cancelled", None, None).await;
        return;
    }
    let Some(transcript) = locate_session_transcript(&sink.session_dir) else {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            None,
            Some(
                stderr_tail(stderr_path).unwrap_or_else(|| {
                    "Pi process exited without writing a session transcript".to_string()
                }),
            ),
        )
        .await;
        return;
    };
    let Some(provider_session_id) = read_session_header_id(&transcript) else {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            None,
            Some("Pi session transcript has no header session id".to_string()),
        )
        .await;
        return;
    };
    sink.bind_transcript(&transcript, &provider_session_id).await;
    sink.post_binding(&provider_session_id, &transcript).await;
    sink.wake_transcript_shipper(&transcript, &provider_session_id).await;
    sink.post_terminal("run_completed", None, None).await;
}

fn build_pi_args(
    prompt: &str,
    provider: Option<&str>,
    model: Option<&str>,
    session_dir: &Path,
) -> Vec<String> {
    let mut args = vec![
        "-p".to_string(),
        prompt.to_string(),
        "--provider".to_string(),
        provider
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or("openrouter")
            .to_string(),
    ];
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        args.extend(["--model".to_string(), model.to_string()]);
    }
    args.extend([
        "--session-dir".to_string(),
        session_dir.to_string_lossy().to_string(),
        "--no-context-files".to_string(),
        "--no-tools".to_string(),
    ]);
    args
}

/// The newest `*.jsonl` in the pi session dir, which names the most recent run.
fn locate_session_transcript(session_dir: &Path) -> Option<PathBuf> {
    let entries = std::fs::read_dir(session_dir).ok()?;
    entries
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.path())
        .filter(|path| {
            path.is_file() && path.extension().and_then(|value| value.to_str()) == Some("jsonl")
        })
        .max_by_key(|path| {
            std::fs::metadata(path)
                .and_then(|metadata| metadata.modified())
                .unwrap_or(std::time::UNIX_EPOCH)
        })
}

/// Read the `{"type":"session",...,"id":<uuid>}` header id from a pi transcript.
fn read_session_header_id(path: &Path) -> Option<String> {
    let bytes = std::fs::read(path).ok()?;
    let header = bytes.split(|byte| *byte == b'\n').next()?;
    let value: Value = serde_json::from_slice(header).ok()?;
    if value.get("type").and_then(Value::as_str) != Some("session") {
        return None;
    }
    value
        .get("id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn pi_console_session_root(session_id: &str) -> PathBuf {
    match crate::config::get_agent_dir() {
        Ok(agent_dir) => agent_dir.join("pi-console").join(session_id).join("sessions"),
        Err(_) => PathBuf::from(".").join("pi-console").join(session_id).join("sessions"),
    }
}

impl PiPrintSink {
    async fn bind_transcript(&self, transcript: &Path, provider_session_id: &str) {
        if let Some(db_path) = self.local_db_path.as_deref() {
            match crate::state::db::open_db(Some(db_path)) {
                Ok(conn) => {
                    let binding = crate::state::session_binding::SessionBinding::new(&conn);
                    if let Err(err) =
                        binding.bind(&transcript.to_string_lossy(), &self.session_id, "pi")
                    {
                        eprintln!("[pi-print] persist transcript binding failed: {err}");
                    }
                }
                Err(err) => eprintln!("[pi-print] open transcript binding DB failed: {err}"),
            }
        }
        if let Ok(registry) = crate::turn_claims::default_registry() {
            let source = transcript.to_string_lossy().to_string();
            let _ = registry.mark_provider_binding(
                &self.run_id,
                provider_session_id,
                Some(&source),
            );
        }
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

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
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
            "dedupe_key": format!("pi-print:{}:{}:phase:{phase}", self.session_id, self.run_id),
            "payload": {"managed_transport": PI_PRINT_ADAPTER, "execution_lifetime": "one_shot"}
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
        let Ok(conn) = crate::state::db::open_db(Some(db_path)) else {
            return;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "pi".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: PI_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
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
    let Some(pgid) = process_group_id else {
        return;
    };
    let outcome =
        crate::process_group::shutdown_group(pgid, crate::process_group::DEFAULT_GRACE).await;
    if !outcome.is_gone() {
        eprintln!("[pi-print] process group {pgid} survived SIGKILL and was left running");
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
    use std::os::unix::fs::PermissionsExt;

    // The engine reads LONGHOUSE_HOME from the process env for the turn-claim
    // registry, the agent dir, and the transcript-wake socket. The two daemon
    // tests below point it at their own temp dirs, so they must not run
    // concurrently with each other (or the monitor task of one would resolve
    // the other's HOME).
    static LONGHOUSE_HOME_LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> =
    std::sync::OnceLock::new();

    #[test]
    fn pi_argv_is_bounded_one_shot_with_session_dir_and_no_tools() {
        let args = build_pi_args(
            "Do one bounded turn",
            Some("openrouter"),
            Some("deepseek/deepseek-v4-flash-latest"),
            Path::new("/tmp/pi-console/sess/sessions"),
        );
        assert_eq!(
            args,
            vec![
                "-p",
                "Do one bounded turn",
                "--provider",
                "openrouter",
                "--model",
                "deepseek/deepseek-v4-flash-latest",
                "--session-dir",
                "/tmp/pi-console/sess/sessions",
                "--no-context-files",
                "--no-tools",
            ]
        );
    }

    #[test]
    fn pi_argv_defaults_provider_and_omits_empty_model() {
        let args = build_pi_args("hi", None, None, Path::new("/tmp/sessions"));
        assert_eq!(
            args,
            vec![
                "-p",
                "hi",
                "--provider",
                "openrouter",
                "--session-dir",
                "/tmp/sessions",
                "--no-context-files",
                "--no-tools",
            ]
        );
    }

    #[test]
    fn session_header_id_is_read_from_the_session_jsonl_header() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("1723200000_019f6b93-edf6-7bd0-a757-b5195a61abdd.jsonl");
        std::fs::write(
            &path,
            "{\"type\":\"session\",\"version\":3,\"id\":\"019f6b93-edf6-7bd0-a757-b5195a61abdd\",\"cwd\":\"/tmp\",\"timestamp\":\"2026-08-10T00:00:00Z\"}\n{\"type\":\"message\",\"id\":\"msg1\",\"timestamp\":\"2026-08-10T00:00:01Z\"}\n",
        )
        .unwrap();
        assert_eq!(
            read_session_header_id(&path).as_deref(),
            Some("019f6b93-edf6-7bd0-a757-b5195a61abdd")
        );

        std::fs::write(&path, "{\"type\":\"message\",\"id\":\"msg1\"}\n").unwrap();
        assert_eq!(read_session_header_id(&path), None);
    }

    #[test]
    fn newest_jsonl_in_session_dir_is_located() {
        let temp = tempfile::tempdir().unwrap();
        let older = temp
            .path()
            .join("1723100000_019f6b93-0000-7000-8000-000000000001.jsonl");
        let newer = temp
            .path()
            .join("1723200000_019f6b93-0000-7000-8000-000000000002.jsonl");
        std::fs::write(&older, "{}\n").unwrap();
        std::fs::write(&newer, "{}\n").unwrap();
        std::fs::write(temp.path().join("stderr.log"), "noise").unwrap();
        assert_eq!(locate_session_transcript(temp.path()), Some(newer));
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
    sid = str(uuid.uuid4())
    with open(os.path.join(session_dir, f"1723200000_{{sid}}.jsonl"), "w") as f:
        f.write(json.dumps({{"type": "session", "version": 3, "id": sid, "cwd": os.getcwd(), "timestamp": "2026-08-10T00:00:00Z"}}) + "\n")
        f.write(json.dumps({{"type": "message", "id": str(uuid.uuid4()), "parentId": None, "timestamp": "2026-08-10T00:00:01Z", "message": {{"role": "assistant", "content": [{{"type": "text", "text": "fake pi reply"}}]}}}}) + "\n")
    print(sid, flush=True)
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
            session_dir: None,
            permission_mode: "bypass".to_string(),
            machine_name: "pi-console-canary".to_string(),
            local_db_path: None,
        }
    }

    #[test]
    fn fake_pi_version_is_bare_semver() {
        let temp = tempfile::tempdir().unwrap();
        let fake_pi = temp.path().join("pi");
        write_fake_pi(&fake_pi, 0);
        let output = std::process::Command::new(&fake_pi)
            .arg("--version")
            .output()
            .unwrap();
        assert!(output.status.success());
        let version = String::from_utf8_lossy(&output.stdout).trim().to_string();
        assert_eq!(version, "0.84.1");
        assert!(!version.contains('\n') && !version.contains(' '));
    }

    #[tokio::test]
    async fn fake_pi_completes_binds_and_wakes_daemon() {
        use tokio::io::AsyncReadExt;
        use tokio::net::UnixListener;

        let _home_guard = LONGHOUSE_HOME_LOCK
            .get_or_init(|| tokio::sync::Mutex::new(()))
            .lock()
            .await;
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
        let _home_guard = LONGHOUSE_HOME_LOCK
            .get_or_init(|| tokio::sync::Mutex::new(()))
            .lock()
            .await;
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