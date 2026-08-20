//! Antigravity Console turns through stock `agy --print`.
//!
//! Turn-scoped one-shot adapter: one bounded `agy --print <prompt>` invocation
//! per Console turn. Unlike the Helm path, this does not depend on hooks --
//! which matters, because agy loads its hooks and never fires them when the
//! user authenticated with `GEMINI_API_KEY`, so hook-delivered control is not
//! universally available. A one-shot print turn is.
//!
//! The conversation identity comes back on stdout rather than from a directory
//! scan: `--output-format json` emits a single object carrying
//! `conversation_id`, and agy writes that conversation's append-only transcript
//! to `~/.gemini/antigravity-cli/brain/<conversation_id>/.system_generated/
//! logs/transcript_full.jsonl`. The engine binds that transcript to the
//! Longhouse session and wakes the daemon so it gets shipped, mirroring the
//! other one-shot console adapters.

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

pub const ANTIGRAVITY_PRINT_ADAPTER: &str = "antigravity_print";
pub const DEFAULT_ANTIGRAVITY_BIN: &str = "agy";
const STDERR_TAIL_LINES: usize = 40;
const DEFAULT_PRINT_TIMEOUT_SECS: u64 = 600;

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

    let args = build_antigravity_args(
        &config.prompt,
        config.model.as_deref(),
        config.conversation_id.as_deref(),
        config.print_timeout_secs.unwrap_or(DEFAULT_PRINT_TIMEOUT_SECS),
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
        .stderr(Stdio::from(stderr_file))
        .env("LONGHOUSE_MANAGED_SESSION_ID", &config.session_id)
        .env("LONGHOUSE_MANAGED_PROVIDER", "antigravity")
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
        None,
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

pub fn interrupt_antigravity_print_turn(run_id: &str, session_id: &str) -> Result<()> {
    let registry = crate::turn_claims::default_registry()?;
    let claim = registry.read(run_id)?;
    if claim.session_id != session_id || claim.provider != "antigravity" {
        anyhow::bail!("Antigravity Console turn claim does not match the requested session");
    }
    if claim.adapter.as_deref() != Some(ANTIGRAVITY_PRINT_ADAPTER) || claim.state != "spawned" {
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
    loop {
        match child.try_wait() {
            Ok(Some(_status)) => break,
            Ok(None) => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(error) => {
                cleanup_process_group(sink.process_group_id).await;
                sink.post_terminal("run_failed", None, Some(error.to_string()))
                    .await;
                return;
            }
        }
    }
    // agy flushes its result object and the conversation transcript as it
    // exits; give the filesystem a beat before reading either.
    tokio::time::sleep(Duration::from_millis(150)).await;
    settle_antigravity_claim(&sink, false, stderr_path).await;
}

async fn settle_antigravity_claim(
    sink: &AntigravityPrintSink,
    cancel_requested: bool,
    stderr_path: &Path,
) {
    if cancel_requested {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal("run_cancelled", None, None).await;
        return;
    }
    let Some(conversation_id) = read_conversation_id(&sink.stdout_path) else {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            None,
            Some(stderr_tail(stderr_path).unwrap_or_else(|| {
                "agy exited without reporting a conversation id".to_string()
            })),
        )
        .await;
        return;
    };
    let Some(transcript) = locate_conversation_transcript(&conversation_id) else {
        cleanup_process_group(sink.process_group_id).await;
        sink.post_terminal(
            "run_failed",
            None,
            Some(format!(
                "agy conversation {conversation_id} has no transcript on disk"
            )),
        )
        .await;
        return;
    };
    sink.bind_transcript(&transcript, &conversation_id).await;
    sink.post_binding(&conversation_id, &transcript).await;
    sink.wake_transcript_shipper(&transcript, &conversation_id)
        .await;
    sink.post_terminal("run_completed", None, None).await;
}

/// Build a bounded one-shot argv.
///
/// `--print` consumes the next argument as its prompt, so it must come last.
/// `--print --print-timeout 60s <prompt>` makes agy treat the literal string
/// `--print-timeout` as the prompt and answer a question about its own flag,
/// leaving the real prompt as a stray positional. The ordering here is
/// load-bearing, not cosmetic.
fn build_antigravity_args(
    prompt: &str,
    model: Option<&str>,
    conversation_id: Option<&str>,
    print_timeout_secs: u64,
) -> Vec<String> {
    let mut args = vec![
        "--dangerously-skip-permissions".to_string(),
        "--output-format".to_string(),
        "json".to_string(),
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

/// Read `conversation_id` out of agy's `--output-format json` result object.
fn read_conversation_id(stdout_path: &Path) -> Option<String> {
    let bytes = std::fs::read(stdout_path).ok()?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    value
        .get("conversation_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

/// agy writes one append-only transcript per conversation under its brain dir.
fn locate_conversation_transcript(conversation_id: &str) -> Option<PathBuf> {
    let path = antigravity_brain_root()?
        .join(conversation_id)
        .join(".system_generated")
        .join("logs")
        .join("transcript_full.jsonl");
    path.is_file().then_some(path)
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

impl AntigravityPrintSink {
    async fn bind_transcript(&self, transcript: &Path, provider_session_id: &str) {
        if let Some(db_path) = self.local_db_path.as_deref() {
            match crate::state::db::open_db(Some(db_path)) {
                Ok(conn) => {
                    let binding = crate::state::session_binding::SessionBinding::new(&conn);
                    if let Err(err) = binding.bind(
                        &transcript.to_string_lossy(),
                        &self.session_id,
                        "antigravity",
                    ) {
                        eprintln!("[antigravity-print] persist transcript binding failed: {err}");
                    }
                }
                Err(err) => {
                    eprintln!("[antigravity-print] open transcript binding DB failed: {err}")
                }
            }
        }
        if let Ok(registry) = crate::turn_claims::default_registry() {
            let source = transcript.to_string_lossy().to_string();
            let _ =
                registry.mark_provider_binding(&self.run_id, provider_session_id, Some(&source));
        }
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
        let Ok(conn) = crate::state::db::open_db(Some(db_path)) else {
            return;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: self.session_id.clone(),
            provider: "antigravity".to_string(),
            phase: phase.to_string(),
            tool_name: tool_name.clone(),
            source: ANTIGRAVITY_PRINT_ADAPTER.to_string(),
            observed_at,
        };
        let _ = crate::state::session_phase::SessionPhaseStore::new(&conn).record(&signal);
    }

    #[cfg(unix)]
    async fn wake_transcript_shipper(&self, source_path: &Path, provider_session_id: &str) {
        let Some(socket_path) = crate::config::get_agent_transcript_wake_socket_path().ok() else {
            return;
        };
        if !socket_path.exists() {
            return;
        }
        let payload = json!({
            "provider": "antigravity",
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
    async fn wake_transcript_shipper(&self, _source_path: &Path, _provider_session_id: &str) {}

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

async fn cleanup_process_group(process_group_id: Option<i32>) {
    let Some(pgid) = process_group_id else {
        return;
    };
    #[cfg(unix)]
    unsafe {
        // agy's run_command children survive a bare SIGINT to the leader, so
        // terminate the whole group and then reap what ignored it.
        libc::killpg(pgid, libc::SIGTERM);
    }
    tokio::time::sleep(Duration::from_millis(200)).await;
    #[cfg(unix)]
    unsafe {
        libc::killpg(pgid, libc::SIGKILL);
    }
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

fn stderr_tail(path: &Path) -> Option<String> {
    let content = std::fs::read_to_string(path).ok()?;
    let trimmed = content.trim_end();
    if trimmed.is_empty() {
        return None;
    }
    let tail = trimmed
        .lines()
        .rev()
        .take(STDERR_TAIL_LINES)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n");
    Some(tail)
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
    fn print_flag_is_last_so_it_takes_the_prompt_as_its_value() {
        // The regression this guards is real and was observed against agy
        // 1.1.16: with `--print` ahead of `--print-timeout`, agy answered a
        // question about its own flag instead of running the prompt.
        let args = build_antigravity_args("do the thing", None, None, 600);
        let print_index = args.iter().position(|arg| arg == "--print").unwrap();
        assert_eq!(args[print_index + 1], "do the thing");
        assert_eq!(print_index, args.len() - 2);
        assert!(args.iter().any(|arg| arg == "--print-timeout"));
        assert!(args
            .iter()
            .position(|arg| arg == "--print-timeout")
            .unwrap()
            < print_index);
    }

    #[test]
    fn argv_is_a_bounded_one_shot_with_structured_output() {
        let args = build_antigravity_args("hello", None, None, 120);
        assert!(args.contains(&"--dangerously-skip-permissions".to_string()));
        assert!(args.contains(&"--output-format".to_string()));
        assert!(args.contains(&"json".to_string()));
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
    fn conversation_id_is_read_from_the_result_object() {
        let dir = std::env::temp_dir().join(format!("agy-print-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("stdout.log");
        std::fs::write(
            &path,
            br#"{"conversation_id":"5f62a636-1412-4afe-9cfd-a5079e0a0366","status":"SUCCESS","response":"ok\n"}"#,
        )
        .unwrap();
        assert_eq!(
            read_conversation_id(&path).as_deref(),
            Some("5f62a636-1412-4afe-9cfd-a5079e0a0366")
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_or_unparsable_result_yields_no_conversation_id() {
        let dir = std::env::temp_dir().join(format!("agy-print-{}", Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("stdout.log");
        std::fs::write(&path, b"not json at all").unwrap();
        assert_eq!(read_conversation_id(&path), None);
        std::fs::write(&path, br#"{"status":"ERROR"}"#).unwrap();
        assert_eq!(read_conversation_id(&path), None);
        std::fs::write(&path, br#"{"conversation_id":"   "}"#).unwrap();
        assert_eq!(read_conversation_id(&path), None);
        assert_eq!(read_conversation_id(&dir.join("absent.log")), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn transcript_is_resolved_under_the_conversation_brain_dir() {
        let home = std::env::temp_dir().join(format!("agy-home-{}", Uuid::new_v4()));
        let conversation = "abc-123";
        let logs = home
            .join(".gemini")
            .join("antigravity-cli")
            .join("brain")
            .join(conversation)
            .join(".system_generated")
            .join("logs");
        std::fs::create_dir_all(&logs).unwrap();
        let transcript = logs.join("transcript_full.jsonl");
        std::fs::write(&transcript, b"{}\n").unwrap();

        let previous = std::env::var_os("HOME");
        std::env::set_var("HOME", &home);
        let located = locate_conversation_transcript(conversation);
        assert_eq!(located.as_deref(), Some(transcript.as_path()));
        assert_eq!(locate_conversation_transcript("missing-conversation"), None);
        match previous {
            Some(value) => std::env::set_var("HOME", value),
            None => std::env::remove_var("HOME"),
        }
        let _ = std::fs::remove_dir_all(&home);
    }
}
