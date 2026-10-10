//! Claude Console turns through a long-lived `claude --print` stream-json process.
//!
//! Each provider result settles one Longhouse turn. The process stays open
//! while Claude reports pending background work and resumes on user input or
//! a provider-originated wake.

use std::fs::{File, OpenOptions};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result};
use chrono::Utc;
use serde::Serialize;
use serde_json::{json, Value};
use tokio::io::AsyncWriteExt;
use tokio::process::{Child, ChildStdin, Command};

use crate::console_adapter::{read_growth, stderr_tail, ClaimLiveness};
use crate::console_lifecycle::{
    ConsoleInput, ConsoleInvocation, IdleOutcome, IdleSignal, InvocationCloseReason,
    InvocationState, PendingItem, TurnBinding, TurnOrigin,
};
use crate::console_sink::{ConsoleProvider, ConsoleRun};
use crate::managed_identity::ManagedIdentity;
use crate::managed_identity_contract::ManagedProvider;
use uuid::Uuid;

pub const CLAUDE_PRINT_ADAPTER: &str = "claude_print";
const CLAUDE_RUNTIME_SOURCE: &str = "claude_console";
#[cfg(test)]
pub const DEFAULT_CLAUDE_BIN: &str = "claude";

/// Claude can answer with a synthetic auth failure before reaching the model.
/// Replay that input on the same provider thread after one bounded retry.
#[cfg(not(test))]
const AUTH_PREFLIGHT_RETRY_DELAYS: [Duration; 2] = [Duration::from_secs(2), Duration::from_secs(5)];
#[cfg(test)]
const AUTH_PREFLIGHT_RETRY_DELAYS: [Duration; 2] =
    [Duration::from_millis(10), Duration::from_millis(10)];

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
    pub image_paths: Vec<PathBuf>,
    pub resume_provider_thread_id: Option<String>,
    pub model: Option<String>,
    pub permission_mode: String,
    pub origin: String,
    pub wake_id: Option<String>,
    pub invocation_id: Option<String>,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pid: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_group_id: Option<i32>,
    pub stdout_path: String,
    pub stderr_path: String,
    pub argv: Vec<String>,
}

#[derive(Clone)]
struct ClaudePrintSink {
    run: ConsoleRun,
    provider_thread_id: String,
}

static CLAUDE_CONSOLE: ConsoleProvider = ConsoleProvider {
    provider: "claude",
    adapter: CLAUDE_PRINT_ADAPTER,
    tag: "claude-print",
    lifetime: "persistent",
};

impl std::ops::Deref for ClaudePrintSink {
    type Target = ConsoleRun;
    fn deref(&self) -> &ConsoleRun {
        &self.run
    }
}

impl std::ops::DerefMut for ClaudePrintSink {
    fn deref_mut(&mut self) -> &mut ConsoleRun {
        &mut self.run
    }
}

struct RetryContext {
    config: ClaudePrintRunConfig,
    stdout_path: PathBuf,
    stderr_path: PathBuf,
}

#[derive(Default)]
struct StreamProgress {
    seq: u64,
    terminal_from_stream: Option<ProviderTerminalResult>,
    identity_confirmed: bool,
    auth_failure: Option<String>,
    made_progress: bool,
    held: Vec<(u64, Value)>,
}

impl StreamProgress {
    fn observe(&mut self, event: &Value) -> bool {
        if stream_session_identity(event).is_some() {
            self.identity_confirmed = true;
        }
        if let Some(terminal) = terminal_result_from_event(event) {
            self.terminal_from_stream = Some(terminal);
        }
        let is_auth_failure = if let Some(message) = auth_failure_message(event) {
            self.auth_failure = Some(message.to_string());
            true
        } else {
            false
        };
        let kind = event.get("type").and_then(Value::as_str);
        let is_result = kind == Some("result");
        if !is_auth_failure
            && !is_result
            && !matches!(
                kind,
                Some("system") | Some("rate_limit_event") | Some("user")
            )
        {
            self.made_progress = true;
        }
        is_auth_failure
    }

    fn wants_auth_retry(&self) -> bool {
        self.auth_failure.is_some() && !self.made_progress
    }

    fn begin_attempt(&mut self) {
        self.terminal_from_stream = None;
        self.auth_failure = None;
        self.made_progress = false;
        self.held.clear();
    }

    fn begin_turn(&mut self) {
        self.terminal_from_stream = None;
        self.auth_failure = None;
        self.made_progress = false;
        self.held.clear();
    }
}

struct ClaudeInput {
    stdin: tokio::sync::Mutex<Option<ChildStdin>>,
}

impl ClaudeInput {
    fn new(stdin: ChildStdin) -> Self {
        Self {
            stdin: tokio::sync::Mutex::new(Some(stdin)),
        }
    }

    async fn write_message(&self, text: &str) -> Result<()> {
        self.send_input(text, &[]).await
    }
}

impl ConsoleInput for ClaudeInput {
    fn send_input<'a>(
        &'a self,
        text: &'a str,
        _images: &'a [PathBuf],
    ) -> crate::console_lifecycle::InputFuture<'a> {
        Box::pin(async move {
            let mut stdin = self.stdin.lock().await;
            let writer = stdin.as_mut().context("Claude stdin is closed")?;
            let message = json!({
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text}]
                }
            });
            writer.write_all(&serde_json::to_vec(&message)?).await?;
            writer.write_all(b"\n").await?;
            writer.flush().await?;
            Ok(())
        })
    }

    fn close_input(&self) -> crate::console_lifecycle::InputFuture<'_> {
        Box::pin(async move {
            self.stdin.lock().await.take();
            Ok(())
        })
    }

    fn echoes_consumed_input(&self) -> bool {
        true
    }
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
    require_claude_lifecycle_hook()?;

    if config.origin == "wake" {
        let invocation_id = config.invocation_id.as_deref().unwrap_or_default();
        let wake_id = config.wake_id.as_deref().unwrap_or_default();
        if let Some(invocation) = crate::console_lifecycle::lookup_launch(invocation_id) {
            match bind_wake_turn(&config, invocation, wake_id, TurnOrigin::Wake).await {
                Ok(summary) => return Ok(summary),
                Err(error) if error.is::<crate::console_lifecycle::WakeTargetGone>() => {}
                Err(error) => return Err(error).context("binding Claude Console wake"),
            }
        }
        if let Some(invocation) =
            crate::console_lifecycle::lookup_retained_wake(invocation_id, wake_id)
        {
            match bind_retained_wake_turn(&config, invocation, wake_id).await {
                Ok(summary) => return Ok(summary),
                Err(error) if error.is::<crate::console_lifecycle::WakeTargetGone>() => {}
                Err(error) => return Err(error).context("binding retained Claude Console wake"),
            }
        }
        return cancel_missing_wake(&config).await;
    }

    let resume_provider_thread_id = normalized_optional(&config.resume_provider_thread_id);
    if let Some(provider_thread_id) = resume_provider_thread_id.as_deref() {
        validate_uuid(provider_thread_id, "resume_provider_thread_id")?;
        crate::console_lifecycle::discard_retained_wakes("claude", provider_thread_id);
        if let Some(invocation) = crate::console_lifecycle::lookup("claude", provider_thread_id) {
            match invocation.state() {
                InvocationState::Parked => {
                    return adopt_parked_turn(&config, invocation).await;
                }
                InvocationState::Closed => {
                    invocation.wait_stopped().await;
                    crate::console_lifecycle::unregister(&invocation.launch_id);
                }
                InvocationState::Responding => {
                    if let Some(wake_id) = invocation.pending_wake_id() {
                        match bind_wake_turn(
                            &config,
                            invocation.clone(),
                            &wake_id,
                            TurnOrigin::User,
                        )
                        .await
                        {
                            Ok(summary) => return Ok(summary),
                            Err(error)
                                if error.is::<crate::console_lifecycle::WakeTargetGone>() =>
                            {
                                match invocation.state() {
                                    InvocationState::Parked => {
                                        return adopt_parked_turn(&config, invocation).await;
                                    }
                                    InvocationState::Closed => {
                                        invocation.wait_stopped().await;
                                        crate::console_lifecycle::unregister(&invocation.launch_id);
                                    }
                                    InvocationState::Responding => {
                                        return Err(error).context(
                                            "binding user turn to Claude Console response",
                                        );
                                    }
                                }
                            }
                            Err(error) => {
                                return Err(error)
                                    .context("binding user turn to Claude Console response");
                            }
                        }
                    }
                }
            }
        }
    }

    let (provider_thread_id, is_resume) = match resume_provider_thread_id {
        Some(value) => (value, true),
        None => (Uuid::new_v4().to_string(), false),
    };
    let launch_id = Uuid::new_v4().to_string();
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
    let (args, recorded_args) =
        build_claude_args(&provider_thread_id, is_resume, config.model.as_deref());
    let argv = std::iter::once(config.claude_bin.clone())
        .chain(recorded_args)
        .collect::<Vec<_>>();
    let registry = crate::turn_claims::default_registry()?;
    let mut sink = make_sink(&config, &provider_thread_id, &launch_id, None)?;
    let mut child = spawn_claude(&config, &args, stdout_file, stderr_file)
        .with_context(|| format!("spawning `{}` --print", config.claude_bin))?;
    let pid = child.id().context("claude --print returned no pid")?;
    let process_group_id = i32::try_from(pid).context("Claude pid exceeds process-group range")?;
    let input = Arc::new(ClaudeInput::new(
        child
            .stdin
            .take()
            .context("Claude stdin pipe was not created")?,
    ));
    sink.process_group_id = Some(process_group_id);
    let result = spawn_record(
        &config,
        &provider_thread_id,
        &launch_id,
        Some(pid),
        Some(process_group_id),
        &stdout_path,
        &stderr_path,
        &argv,
    );
    if let Err(error) = registry.mark_spawned_invocation(
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
        crate::process_group::shutdown_owned_child(
            &mut child,
            Some(process_group_id),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        return Err(error).context("persisting Claude Console spawn identity");
    }
    if let Err(error) =
        registry.record_invocation_turn(&config.run_id, TurnOrigin::User.as_str(), false)
    {
        crate::process_group::shutdown_owned_child(
            &mut child,
            Some(process_group_id),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        return Err(error).context("recording Claude Console turn origin");
    }
    let binding = turn_binding(&config, TurnOrigin::User);
    let invocation = Arc::new(ConsoleInvocation::new(
        "claude",
        provider_thread_id.clone(),
        launch_id.clone(),
        pid,
        process_group_id,
        binding,
        input.clone(),
    ));
    if let Err(error) = crate::console_lifecycle::register(invocation.clone()) {
        input.close_input().await.ok();
        crate::process_group::shutdown_owned_child(
            &mut child,
            Some(process_group_id),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        let _ = registry.mark_failed(&config.run_id, &error.to_string());
        return Err(error);
    }
    sink.post_phase("thinking", None).await;
    invocation.expect_input_ack();
    if let Err(error) = input.write_message(&config.prompt).await {
        invocation.take_active_turn();
        let _ = registry.record_invocation_state(&config.run_id, "closed", 0);
        invocation.close_input().await.ok();
        let shutdown = crate::process_group::shutdown_owned_child(
            &mut child,
            Some(process_group_id),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        if !shutdown.is_gone() {
            let _ = registry.record_shutdown_survived(&config.run_id, invocation.pending_count());
        }
        drop(lock);
        invocation.process_exited();
        crate::console_lifecycle::unregister(&launch_id);
        return Err(error).context("writing Claude Console input");
    }
    let monitor = crate::turn_claims::register_monitor(&config.run_id);
    let retry = RetryContext {
        config: config.clone(),
        stdout_path: stdout_path.clone(),
        stderr_path: stderr_path.clone(),
    };
    let summary = ClaudePrintRunSummary {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        run_id: config.run_id.clone(),
        provider_thread_id,
        launch_id: launch_id.clone(),
        pid: Some(pid),
        process_group_id: Some(process_group_id),
        stdout_path: stdout_path.to_string_lossy().to_string(),
        stderr_path: stderr_path.to_string_lossy().to_string(),
        argv,
    };
    tokio::spawn(async move {
        monitor_claude_print(
            child,
            stdout_path,
            stderr_path,
            sink,
            retry,
            invocation,
            lock,
        )
        .await;
        drop(monitor);
    });
    Ok(summary)
}
fn turn_binding(config: &ClaudePrintRunConfig, origin: TurnOrigin) -> TurnBinding {
    TurnBinding {
        run_id: config.run_id.clone(),
        turn_id: config.turn_id.clone(),
        client_request_id: config.client_request_id.clone(),
        origin,
    }
}

fn make_sink(
    config: &ClaudePrintRunConfig,
    provider_thread_id: &str,
    launch_id: &str,
    process_group_id: Option<i32>,
) -> Result<ClaudePrintSink> {
    Ok(ClaudePrintSink {
        run: ConsoleRun {
            provider: &CLAUDE_CONSOLE,
            session_id: config.session_id.clone(),
            thread_id: config.thread_id.clone(),
            turn_id: config.turn_id.clone(),
            run_id: config.run_id.clone(),
            client_request_id: config.client_request_id.clone(),
            launch_id: launch_id.to_string(),
            process_group_id,
            machine_name: config.machine_name.clone(),
            local_db_path: config.local_db_path.clone(),
            runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir()?,
        },
        provider_thread_id: provider_thread_id.to_string(),
    })
}

async fn adopt_parked_turn(
    config: &ClaudePrintRunConfig,
    invocation: Arc<ConsoleInvocation>,
) -> Result<ClaudePrintRunSummary> {
    let registry = crate::turn_claims::default_registry()?;
    let previous = registry.read(&invocation.latest_turn().run_id)?;
    let stdout_path = previous
        .stdout_path
        .as_deref()
        .context("parked Claude invocation has no stdout path")?;
    let stderr_path = previous
        .stderr_path
        .as_deref()
        .context("parked Claude invocation has no stderr path")?;
    let (pid, process_group_id) = invocation.process_identity();
    let argv = previous
        .result
        .as_ref()
        .and_then(|result| result.get("argv"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect::<Vec<_>>();
    let sink = make_sink(
        config,
        &invocation.provider_thread_id,
        &invocation.launch_id,
        Some(process_group_id),
    )?;
    let result = spawn_record(
        config,
        &invocation.provider_thread_id,
        &invocation.launch_id,
        Some(pid),
        Some(process_group_id),
        Path::new(stdout_path),
        Path::new(stderr_path),
        &argv,
    );
    registry.mark_spawned_invocation(
        &config.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        CLAUDE_PRINT_ADAPTER,
        &invocation.launch_id,
        Some(&invocation.provider_thread_id),
        stdout_path,
        stderr_path,
        result,
    )?;
    registry.record_invocation_turn(&config.run_id, TurnOrigin::User.as_str(), true)?;
    registry.mark_provider_binding(&config.run_id, &invocation.provider_thread_id, None)?;
    sink.post_phase("thinking", None).await;
    if let Err(error) = invocation
        .send_user_input(
            turn_binding(config, TurnOrigin::User),
            &config.prompt,
            &config.image_paths,
        )
        .await
    {
        invocation.close_input().await.ok();
        return Err(error).context("writing user input to parked Claude invocation");
    }
    let monitor = crate::turn_claims::register_monitor(&config.run_id);
    let monitored_invocation = invocation.clone();
    tokio::spawn(async move {
        while monitored_invocation.state() != InvocationState::Closed {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        drop(monitor);
    });
    Ok(ClaudePrintRunSummary {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        run_id: config.run_id.clone(),
        provider_thread_id: invocation.provider_thread_id.clone(),
        launch_id: invocation.launch_id.clone(),
        pid: Some(pid),
        process_group_id: Some(process_group_id),
        stdout_path: stdout_path.to_string(),
        stderr_path: stderr_path.to_string(),
        argv,
    })
}

async fn bind_wake_turn(
    config: &ClaudePrintRunConfig,
    invocation: Arc<ConsoleInvocation>,
    wake_id: &str,
    origin: TurnOrigin,
) -> Result<ClaudePrintRunSummary> {
    let latest_run = invocation.latest_turn().run_id;
    let claims = crate::turn_claims::default_registry()?;
    let previous = claims.read(&latest_run)?;
    let stdout_path = previous
        .stdout_path
        .as_deref()
        .context("parked Claude invocation has no stdout path")?;
    let stderr_path = previous
        .stderr_path
        .as_deref()
        .context("parked Claude invocation has no stderr path")?;
    let (pid, process_group_id) = invocation.process_identity();
    let argv = previous
        .result
        .as_ref()
        .and_then(|result| result.get("argv"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect::<Vec<_>>();
    let sink = make_sink(
        config,
        &invocation.provider_thread_id,
        &invocation.launch_id,
        Some(process_group_id),
    )?;
    let result = spawn_record(
        config,
        &invocation.provider_thread_id,
        &invocation.launch_id,
        Some(pid),
        Some(process_group_id),
        Path::new(stdout_path),
        Path::new(stderr_path),
        &argv,
    );
    let binding = turn_binding(config, origin.clone());
    let is_user_turn = origin == TurnOrigin::User;
    let wake = invocation.bind_wake(&invocation.launch_id, wake_id, binding, || {
        claims.mark_spawned_invocation(
            &config.run_id,
            pid,
            process_group_id,
            crate::turn_claims::process_start_time_for_pid(Some(pid)),
            CLAUDE_PRINT_ADAPTER,
            &invocation.launch_id,
            Some(&invocation.provider_thread_id),
            stdout_path,
            stderr_path,
            result,
        )?;
        claims.record_invocation_turn(&config.run_id, origin.as_str(), true)?;
        claims.mark_provider_binding(&config.run_id, &invocation.provider_thread_id, None)?;
        Ok(())
    })?;
    sink.post_phase("thinking", None).await;
    for event in wake.buffered_events {
        sink.post_stream_event(event.sequence, event.value).await;
    }
    if let Some((_, snapshot)) = invocation.delegation_snapshot() {
        sink.post_delegation_snapshot(snapshot).await;
    }
    if is_user_turn {
        if let Err(error) = invocation
            .write_input(&config.prompt, &config.image_paths)
            .await
        {
            invocation.close_input().await.ok();
            return Err(error).context("writing user input to Claude Console response");
        }
        if let Some(outcome) = invocation.finish_pending_user_input() {
            let closed = complete_idle(&invocation, &sink, outcome).await;
            if closed {
                invocation.close_input().await.ok();
            }
        }
    } else if let Some(outcome) = wake.deferred_idle {
        let closed = complete_idle(&invocation, &sink, outcome).await;
        if closed {
            invocation.close_input().await.ok();
        }
    }
    let monitor = crate::turn_claims::register_monitor(&config.run_id);
    let monitored_invocation = invocation.clone();
    tokio::spawn(async move {
        while monitored_invocation.state() != InvocationState::Closed {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        drop(monitor);
    });
    Ok(ClaudePrintRunSummary {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        run_id: config.run_id.clone(),
        provider_thread_id: invocation.provider_thread_id.clone(),
        launch_id: invocation.launch_id.clone(),
        pid: Some(pid),
        process_group_id: Some(process_group_id),
        stdout_path: stdout_path.to_string(),
        stderr_path: stderr_path.to_string(),
        argv,
    })
}

async fn bind_retained_wake_turn(
    config: &ClaudePrintRunConfig,
    invocation: Arc<ConsoleInvocation>,
    wake_id: &str,
) -> Result<ClaudePrintRunSummary> {
    let latest_run = invocation.latest_turn().run_id;
    let claims = crate::turn_claims::default_registry()?;
    let previous = claims.read(&latest_run)?;
    let stdout_path = previous
        .stdout_path
        .as_deref()
        .context("retained Claude wake has no stdout path")?;
    let stderr_path = previous
        .stderr_path
        .as_deref()
        .context("retained Claude wake has no stderr path")?;
    let argv = previous
        .result
        .as_ref()
        .and_then(|result| result.get("argv"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_string)
        .collect::<Vec<_>>();
    let (pid, process_group_id) = invocation.process_identity();
    let sink = make_sink(
        config,
        &invocation.provider_thread_id,
        &invocation.launch_id,
        None,
    )?;
    let binding = turn_binding(config, TurnOrigin::Wake);
    let wake =
        invocation.bind_retained_wake(&invocation.launch_id, wake_id, binding, |source_state| {
            if source_state == InvocationState::Parked {
                let result = spawn_record(
                    config,
                    &invocation.provider_thread_id,
                    &invocation.launch_id,
                    Some(pid),
                    Some(process_group_id),
                    Path::new(stdout_path),
                    Path::new(stderr_path),
                    &argv,
                );
                claims.mark_spawned_invocation(
                    &config.run_id,
                    pid,
                    process_group_id,
                    crate::turn_claims::process_start_time_for_pid(Some(pid)),
                    CLAUDE_PRINT_ADAPTER,
                    &invocation.launch_id,
                    Some(&invocation.provider_thread_id),
                    stdout_path,
                    stderr_path,
                    result,
                )?;
            } else {
                let result = spawn_record(
                    config,
                    &invocation.provider_thread_id,
                    &invocation.launch_id,
                    None,
                    None,
                    Path::new(stdout_path),
                    Path::new(stderr_path),
                    &argv,
                );
                claims.mark_spawned(
                    &config.run_id,
                    None,
                    None,
                    None,
                    CLAUDE_PRINT_ADAPTER,
                    result,
                )?;
            }
            claims.record_invocation_turn(&config.run_id, TurnOrigin::Wake.as_str(), true)?;
            claims.mark_provider_binding(&config.run_id, &invocation.provider_thread_id, None)?;
            Ok(())
        })?;
    sink.post_phase("thinking", None).await;
    for event in wake.buffered_events {
        sink.post_stream_event(event.sequence, event.value).await;
    }
    if let Some((_, snapshot)) = invocation.delegation_snapshot() {
        sink.post_delegation_snapshot(snapshot).await;
    }
    if let Some(outcome) = wake.deferred_idle {
        let closed = complete_idle(&invocation, &sink, outcome).await;
        if closed {
            invocation.close_input().await.ok();
        }
    }
    let monitor = crate::turn_claims::register_monitor(&config.run_id);
    let monitored_invocation = invocation.clone();
    tokio::spawn(async move {
        while monitored_invocation.state() != InvocationState::Closed {
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        drop(monitor);
    });
    let process_active = invocation.state() == InvocationState::Parked;
    Ok(ClaudePrintRunSummary {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        run_id: config.run_id.clone(),
        provider_thread_id: invocation.provider_thread_id.clone(),
        launch_id: invocation.launch_id.clone(),
        pid: process_active.then_some(pid),
        process_group_id: process_active.then_some(process_group_id),
        stdout_path: stdout_path.to_string(),
        stderr_path: stderr_path.to_string(),
        argv,
    })
}

async fn cancel_missing_wake(config: &ClaudePrintRunConfig) -> Result<ClaudePrintRunSummary> {
    let launch_id = config.invocation_id.as_deref().unwrap_or_default();
    let provider_thread_id =
        normalized_optional(&config.resume_provider_thread_id).unwrap_or_default();
    let sink = make_sink(config, &provider_thread_id, launch_id, None)?;
    sink.post_terminal_with_lifecycle(
        "run_cancelled",
        None,
        Some("wake target is gone or wake_id is unknown".to_string()),
        Some("closed"),
        Some(0),
        Some("wake_target_gone"),
    )
    .await;
    Ok(ClaudePrintRunSummary {
        session_id: config.session_id.clone(),
        thread_id: config.thread_id.clone(),
        run_id: config.run_id.clone(),
        provider_thread_id,
        launch_id: launch_id.to_string(),
        pid: None,
        process_group_id: None,
        stdout_path: String::new(),
        stderr_path: String::new(),
        argv: Vec::new(),
    })
}

async fn complete_idle(
    invocation: &ConsoleInvocation,
    sink: &ClaudePrintSink,
    outcome: IdleOutcome,
) -> bool {
    let _ = invocation.persist_pending_claim();
    let turn_sink = sink.for_binding(&outcome.binding);
    if outcome.has_active_turn {
        turn_sink
            .post_terminal_with_lifecycle(
                &outcome.signal.terminal_state,
                outcome.signal.exit_code,
                outcome.signal.stderr,
                Some(outcome.invocation_state.as_str()),
                Some(outcome.pending_count),
                None,
            )
            .await;
    } else if let Ok(claims) = crate::turn_claims::default_registry() {
        let _ = claims.record_invocation_state(
            &outcome.binding.run_id,
            outcome.invocation_state.as_str(),
            outcome.pending_count,
        );
    }
    if outcome.invocation_state == InvocationState::Parked {
        if let Some((binding, snapshot)) = invocation.delegation_snapshot() {
            sink.for_binding(&binding)
                .post_delegation_snapshot(snapshot)
                .await;
        }
    }
    if outcome.invocation_state == InvocationState::Closed {
        let _ = invocation.close_input().await;
        true
    } else {
        false
    }
}

pub async fn close_parked_claude_invocation(
    claim: &crate::turn_claims::TurnClaim,
    invocation: Arc<ConsoleInvocation>,
    machine_name: &str,
) -> Result<Option<crate::console_lifecycle::InvocationCloseOutcome>> {
    crate::console_lifecycle::close_parked_invocation(
        &invocation,
        claim,
        machine_name,
        CLAUDE_RUNTIME_SOURCE,
        crate::config::get_agent_runtime_events_outbox_dir(),
    )
    .await
}

pub async fn recover_claude_print_turns(
    machine_name: &str,
    _local_db_path: Option<PathBuf>,
) -> Result<usize> {
    let registry = crate::turn_claims::default_registry()?;
    let outbox_dir = crate::config::get_agent_runtime_events_outbox_dir()?;
    let Some(inventory) = crate::process_identity::try_collect_process_facts_by_pid() else {
        tracing::warn!("Process inventory unavailable; leaving Claude Console claims untouched");
        return Ok(0);
    };
    let claims = registry.list_all()?;
    let mut seen_launches = std::collections::HashSet::new();
    let mut recovered = 0;
    for claim in claims.into_iter().rev() {
        if claim.adapter.as_deref() != Some(CLAUDE_PRINT_ADAPTER) {
            continue;
        }
        let Some(launch_id) = claim.launch_id.as_deref() else {
            continue;
        };
        if !seen_launches.insert(launch_id.to_string())
            || crate::console_lifecycle::lookup_launch(launch_id).is_some()
        {
            continue;
        }
        let parked = claim.invocation_state.as_deref() == Some("parked");
        let active = claim.state == "spawned";
        if !parked && !active {
            continue;
        }
        let process_gone = match crate::console_adapter::claim_liveness(&claim, Some(&inventory)) {
            ClaimLiveness::Unknown => {
                tracing::warn!(
                    run_id = %claim.run_id,
                    "Could not prove the Claude process identity during recovery"
                );
                continue;
            }
            ClaimLiveness::Live => false,
            ClaimLiveness::Gone => true,
        };
        let running_group = match claim.process_group_id {
            Some(pgid) if crate::process_group::running_member_off_runtime(pgid).await => {
                Some(pgid)
            }
            _ => None,
        };
        if let Some(pgid) = running_group {
            if !claim.process_group_is_from_this_boot()
                || !claim.has_live_group_identity(&inventory)
            {
                tracing::warn!(
                    run_id = %claim.run_id,
                    process_group_id = pgid,
                    "Claude process group is live but its exact identity cannot be verified"
                );
                if claim.pending_count > 0 || !process_gone {
                    continue;
                }
            } else {
                let outcome =
                    crate::process_group::shutdown_group(pgid, crate::process_group::DEFAULT_GRACE)
                        .await;
                if !outcome.is_gone() {
                    tracing::error!(
                        run_id = %claim.run_id,
                        process_group_id = pgid,
                        "Claude process group survived Machine Agent recovery shutdown"
                    );
                    continue;
                }
            }
        }
        if active {
            let stdout_path = claim.stdout_path.as_deref().map(PathBuf::from);
            let stderr_path = claim.stderr_path.as_deref().map(PathBuf::from).or_else(|| {
                stdout_path
                    .as_ref()
                    .map(|path| path.with_file_name("stderr.log"))
            });
            let sink = ClaudePrintSink {
                run: ConsoleRun {
                    provider: &CLAUDE_CONSOLE,
                    session_id: claim.session_id.clone(),
                    thread_id: claim.thread_id.clone(),
                    turn_id: claim.turn_id.clone(),
                    run_id: claim.run_id.clone(),
                    client_request_id: claim.client_request_id.clone(),
                    launch_id: launch_id.to_string(),
                    process_group_id: claim.process_group_id,
                    machine_name: machine_name.to_string(),
                    local_db_path: _local_db_path.clone(),
                    runtime_events_outbox_dir: crate::config::get_agent_runtime_events_outbox_dir(
                    )?,
                },
                provider_thread_id: claim.provider_thread_id.clone().unwrap_or_default(),
            };
            sink.post_terminal_with_lifecycle(
                "run_cancelled",
                None,
                stderr_path.as_deref().and_then(stderr_tail),
                Some("closed"),
                Some(claim.pending_count),
                Some("machine_agent_restart"),
            )
            .await;
        }
        if parked && claim.pending_count > 0 {
            let stopped = crate::console_lifecycle::stopped_items_for_claim(&claim);
            if let Err(error) = crate::console_lifecycle::publish_invocation_closed(
                &registry,
                Ok(&outbox_dir),
                &claim,
                machine_name,
                CLAUDE_RUNTIME_SOURCE,
                InvocationCloseReason::MachineAgentRestart,
                crate::console_lifecycle::recovered_invocation_cleanup(&claim),
                &stopped,
            ) {
                tracing::warn!(
                    %error,
                    run_id = %claim.run_id,
                    "Failed to publish recovered Claude Console close"
                );
                continue;
            }
        }
        let _ = registry.record_invocation_state(&claim.run_id, "closed", claim.pending_count);
        recovered += 1;
    }
    Ok(recovered)
}

/// Enter a running Claude Console turn with `text` (see
/// `claude_channel_control::record_console_steer`). `Err("turn_not_steerable")`
/// unless this run's recorded Claude process is still the live one.
pub fn steer_claude_print_turn(
    run_id: &str,
    session_id: &str,
    text: &str,
) -> std::result::Result<(), String> {
    let not_steerable = || "turn_not_steerable".to_string();
    let registry = crate::turn_claims::default_registry().map_err(|err| err.to_string())?;
    let claim = registry.read(run_id).map_err(|_| not_steerable())?;
    if claim.session_id != session_id
        || claim.provider != "claude"
        || claim.adapter.as_deref() != Some(CLAUDE_PRINT_ADAPTER)
        || claim.state != "spawned"
    {
        return Err(not_steerable());
    }
    let (Some(pid), Some(expected_start)) = (claim.pid, claim.process_start_time.as_deref()) else {
        return Err(not_steerable());
    };
    let alive = crate::process_identity::collect_process_facts_by_pid()
        .get(&pid)
        .is_some_and(|facts| facts.lstart == expected_start);
    if !alive {
        return Err(not_steerable());
    }
    crate::claude_channel_control::record_console_steer(session_id, text)
        .map_err(|err| err.to_string())
}

pub fn interrupt_claude_print_turn(
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
        || claim.provider != "claude"
    {
        anyhow::bail!(
            "Claude Console turn claim does not match the requested session, thread, or turn"
        );
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
    let actual_pgid = unsafe { libc::getpgid(pid as libc::pid_t) };
    if actual_pgid != pgid || crate::process_group::leader_group_for(pid) != Some(pgid) {
        anyhow::bail!("Claude Console provider process-group identity changed");
    }
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
    mut child: Child,
    stdout_path: PathBuf,
    stderr_path: PathBuf,
    mut sink: ClaudePrintSink,
    retry: RetryContext,
    invocation: Arc<ConsoleInvocation>,
    lock: File,
) {
    let mut conversation_lock = Some(lock);
    let mut offset = 0_u64;
    let mut pending_bytes = Vec::new();
    let mut stream = StreamProgress::default();
    let mut retries_used = 0;
    let mut last_trigger = json!({"kind": "unknown", "task_ids": [], "summary": ""});
    let mut deferred_completion: Option<RegistryUpdate> = None;
    let mut observed_run = invocation.latest_turn().run_id;

    loop {
        if invocation.state() == InvocationState::Closed {
            close_invocation(&mut child, &invocation, &mut conversation_lock).await;
            return;
        }
        // Observe exit before reading stdout: a child that has exited has
        // written all its output, so this read sees its final lines (an auth
        // failure, the last `result`) before the exit is handled. Checking
        // after the read dropped whatever it wrote in between.
        let exit_status = child.try_wait();
        let lines = match read_growth(&stdout_path, &mut offset, &mut pending_bytes) {
            Ok(lines) => lines,
            Err(error) => {
                fail_active_turn(
                    &mut child,
                    &invocation,
                    &sink,
                    &stderr_path,
                    error.to_string(),
                    &mut conversation_lock,
                )
                .await;
                return;
            }
        };
        let had_lines = !lines.is_empty();
        let mut retry_now = false;
        for bytes in lines {
            stream.seq += 1;
            let sequence = stream.seq;
            let event = match serde_json::from_slice::<Value>(&bytes) {
                Ok(event) => event,
                Err(error) => {
                    let binding = invocation.latest_turn();
                    sink.for_binding(&binding)
                        .post_decode_gap(sequence, &error.to_string(), &bytes)
                        .await;
                    continue;
                }
            };
            if let Err(error) = validate_stream_identity(&event, &invocation.provider_thread_id) {
                fail_active_turn(
                    &mut child,
                    &invocation,
                    &sink,
                    &stderr_path,
                    error.to_string(),
                    &mut conversation_lock,
                )
                .await;
                return;
            }
            if invocation.latest_turn().run_id != observed_run {
                observed_run = invocation.latest_turn().run_id;
                stream.begin_turn();
            }
            let is_auth_failure = stream.observe(&event);
            let is_result = event.get("type").and_then(Value::as_str) == Some("result");
            let may_hold = retries_used < AUTH_PREFLIGHT_RETRY_DELAYS.len();
            if may_hold && stream.wants_auth_retry() && (is_auth_failure || is_result) {
                stream.held.push((sequence, event));
                retry_now = is_result;
                if retry_now {
                    break;
                }
                continue;
            }

            if is_response_start(&event) && invocation.state() == InvocationState::Parked {
                stream.begin_turn();
                if let Some(wake) = invocation.response_started(last_trigger.clone()) {
                    let wake_sink = sink.for_binding(&invocation.latest_turn());
                    wake_sink.post_wake_signal(&wake).await;
                    if let Some(update) = deferred_completion.take() {
                        if apply_registry_update(&invocation, &sink, update).await {
                            close_invocation(&mut child, &invocation, &mut conversation_lock).await;
                            return;
                        }
                    }
                }
            }
            if is_replayed_user_input(&event) {
                invocation.input_acknowledged();
            }
            if let Some(trigger) = task_trigger(&event) {
                last_trigger = trigger;
            }
            if let Some(update) = registry_update(&event) {
                let defer_completion = invocation.state() == InvocationState::Parked
                    && match &update {
                        RegistryUpdate::Item(_, pending) => {
                            !*pending && is_task_completion_event(&event)
                        }
                        RegistryUpdate::Snapshot(items, _) => {
                            items.is_empty() && deferred_completion.is_some()
                        }
                    };
                if defer_completion {
                    deferred_completion = Some(update);
                } else if apply_registry_update(&invocation, &sink, update).await {
                    close_invocation(&mut child, &invocation, &mut conversation_lock).await;
                    return;
                }
            }

            if let Some((binding, event)) = invocation.route_stream_event(sequence, event) {
                let event_sink = sink.for_binding(&binding);
                event_sink.post_stream_event(sequence, event).await;
            }

            if is_result && invocation.state() == InvocationState::Parked {
                if let Some(update) = deferred_completion.take() {
                    if apply_registry_update(&invocation, &sink, update).await {
                        close_invocation(&mut child, &invocation, &mut conversation_lock).await;
                        return;
                    }
                }
                if invocation.state() == InvocationState::Parked {
                    continue;
                }
            }
            if is_result {
                let claim = crate::turn_claims::default_registry()
                    .and_then(|claims| claims.read(&invocation.latest_turn().run_id))
                    .ok();
                let cancelled = claim
                    .as_ref()
                    .is_some_and(|claim| claim.cancel_requested_at.is_some());
                let terminal = settle_terminal_state(
                    cancelled,
                    true,
                    stream.identity_confirmed,
                    stream.terminal_from_stream,
                );
                let stderr = stderr_tail(&stderr_path).or(stream.auth_failure.clone());
                if let Some(outcome) = invocation.idle(IdleSignal {
                    terminal_state: terminal,
                    exit_code: None,
                    stderr,
                }) {
                    let closed = complete_idle(&invocation, &sink, outcome).await;
                    stream.begin_turn();
                    if closed {
                        close_invocation(&mut child, &invocation, &mut conversation_lock).await;
                        return;
                    }
                }
            }
        }

        if retry_now {
            if retries_used < AUTH_PREFLIGHT_RETRY_DELAYS.len() {
                let delay = AUTH_PREFLIGHT_RETRY_DELAYS[retries_used];
                retries_used += 1;
                let (_, process_group_id) = invocation.process_identity();
                let _ = invocation.close_input().await;
                crate::process_group::shutdown_owned_child(
                    &mut child,
                    Some(process_group_id),
                    crate::process_group::DEFAULT_GRACE,
                )
                .await;
                tokio::time::sleep(delay).await;
                match respawn_claude(&retry, &sink, &invocation).await {
                    Ok((next, next_sink)) => {
                        child = next;
                        sink = next_sink;
                        stream.begin_attempt();
                        continue;
                    }
                    Err(error) => {
                        let failed_sink = sink.for_binding(&invocation.latest_turn());
                        for (sequence, event) in std::mem::take(&mut stream.held) {
                            failed_sink.post_stream_event(sequence, event).await;
                        }
                        fail_active_turn(
                            &mut child,
                            &invocation,
                            &sink,
                            &stderr_path,
                            stream.auth_failure.clone().unwrap_or_else(|| {
                                format!("relaunching Claude after an auth failure: {error:#}")
                            }),
                            &mut conversation_lock,
                        )
                        .await;
                        return;
                    }
                }
            }
            let failed_sink = sink.for_binding(&invocation.latest_turn());
            for (sequence, event) in std::mem::take(&mut stream.held) {
                failed_sink.post_stream_event(sequence, event).await;
            }
            fail_active_turn(
                &mut child,
                &invocation,
                &sink,
                &stderr_path,
                stream.auth_failure.clone().unwrap_or_else(|| {
                    "Claude authentication failed before the model ran".to_string()
                }),
                &mut conversation_lock,
            )
            .await;
            return;
        }

        match exit_status {
            Ok(Some(status)) => {
                let active = invocation.take_active_turn();
                if let Some(binding) = active {
                    let terminal = if crate::turn_claims::default_registry()
                        .and_then(|claims| claims.read(&binding.run_id))
                        .ok()
                        .is_some_and(|claim| claim.cancel_requested_at.is_some())
                    {
                        "run_cancelled"
                    } else {
                        "run_failed"
                    };
                    sink.for_binding(&binding)
                        .post_terminal_with_lifecycle(
                            terminal,
                            status.code(),
                            stderr_tail(&stderr_path),
                            Some("closed"),
                            Some(invocation.pending_count()),
                            None,
                        )
                        .await;
                }
                let outcome = crate::process_group::shutdown_owned_child(
                    &mut child,
                    Some(invocation.process_identity().1),
                    crate::process_group::DEFAULT_GRACE,
                )
                .await;
                if !outcome.is_gone() {
                    tracing::error!(
                        process_group_id = invocation.process_identity().1,
                        "Claude process group survived child-exit cleanup"
                    );
                    if let Ok(claims) = crate::turn_claims::default_registry() {
                        let binding = invocation.latest_turn();
                        let _ = claims
                            .record_shutdown_survived(&binding.run_id, invocation.pending_count());
                    }
                    return;
                }
                drop(conversation_lock.take());
                invocation.mark_stopped();
                record_closed_invocation(&invocation);
                crate::console_lifecycle::unregister(&invocation.launch_id);
                return;
            }
            Ok(None) => {}
            Err(error) => {
                fail_active_turn(
                    &mut child,
                    &invocation,
                    &sink,
                    &stderr_path,
                    error.to_string(),
                    &mut conversation_lock,
                )
                .await;
                return;
            }
        }
        if had_lines && stream.terminal_from_stream.is_none() {
            let run_id = invocation.latest_turn().run_id;
            persist_projection_checkpoint(&run_id, offset, pending_bytes.len(), stream.seq);
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

async fn fail_active_turn(
    child: &mut Child,
    invocation: &ConsoleInvocation,
    sink: &ClaudePrintSink,
    stderr_path: &Path,
    error: String,
    lock: &mut Option<File>,
) {
    let stderr = stderr_tail(stderr_path).unwrap_or(error);
    let pending_count = invocation.pending_count();
    let active = invocation.take_active_turn();
    if let Some(binding) = active {
        sink.for_binding(&binding)
            .post_terminal_with_lifecycle(
                "run_failed",
                None,
                Some(stderr),
                Some("closed"),
                Some(pending_count),
                None,
            )
            .await;
    }
    close_invocation(child, invocation, lock).await;
}

async fn close_invocation(
    child: &mut Child,
    invocation: &ConsoleInvocation,
    lock: &mut Option<File>,
) {
    let _ = invocation.close_input().await;
    let process_group_id = invocation.process_identity().1;
    let outcome = crate::process_group::shutdown_owned_child(
        child,
        Some(process_group_id),
        crate::process_group::DEFAULT_GRACE,
    )
    .await;
    if !outcome.is_gone() {
        eprintln!("[claude-print] process group {process_group_id} survived shutdown");
        if let Ok(claims) = crate::turn_claims::default_registry() {
            let binding = invocation.latest_turn();
            let _ = claims.record_shutdown_survived(&binding.run_id, invocation.pending_count());
        }
        return;
    }
    drop(lock.take());
    invocation.process_exited();
    record_closed_invocation(invocation);
    crate::console_lifecycle::unregister(&invocation.launch_id);
}

fn record_closed_invocation(invocation: &ConsoleInvocation) {
    if let Ok(claims) = crate::turn_claims::default_registry() {
        let binding = invocation.latest_turn();
        let _ =
            claims.record_invocation_state(&binding.run_id, "closed", invocation.pending_count());
    }
}

async fn respawn_claude(
    retry: &RetryContext,
    sink: &ClaudePrintSink,
    invocation: &ConsoleInvocation,
) -> Result<(Child, ClaudePrintSink)> {
    let (args, recorded_args) = build_claude_args(
        &sink.provider_thread_id,
        true,
        retry.config.model.as_deref(),
    );
    let argv = std::iter::once(retry.config.claude_bin.clone())
        .chain(recorded_args)
        .collect::<Vec<_>>();
    let mut child = spawn_claude(
        &retry.config,
        &args,
        append_output_file(&retry.stdout_path)?,
        append_output_file(&retry.stderr_path)?,
    )
    .with_context(|| format!("spawning `{}` --print", retry.config.claude_bin))?;
    let pid = child.id().context("relaunched Claude returned no pid")?;
    let process_group_id = i32::try_from(pid).context("Claude pid exceeds process-group range")?;
    let input = Arc::new(ClaudeInput::new(
        child
            .stdin
            .take()
            .context("Claude stdin pipe was not created")?,
    ));
    let current = invocation.latest_turn();
    let config = ClaudePrintRunConfig {
        run_id: current.run_id.clone(),
        turn_id: current.turn_id.clone(),
        client_request_id: current.client_request_id.clone(),
        ..retry.config.clone()
    };
    let record = spawn_record(
        &config,
        &sink.provider_thread_id,
        &sink.launch_id,
        Some(pid),
        Some(process_group_id),
        &retry.stdout_path,
        &retry.stderr_path,
        &argv,
    );
    let claims = crate::turn_claims::default_registry()?;
    if let Err(error) = claims.mark_spawned_invocation(
        &current.run_id,
        pid,
        process_group_id,
        crate::turn_claims::process_start_time_for_pid(Some(pid)),
        CLAUDE_PRINT_ADAPTER,
        &sink.launch_id,
        Some(&sink.provider_thread_id),
        &retry.stdout_path.to_string_lossy(),
        &retry.stderr_path.to_string_lossy(),
        record,
    ) {
        input.close_input().await.ok();
        crate::process_group::shutdown_owned_child(
            &mut child,
            Some(process_group_id),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        return Err(error);
    }
    invocation.expect_input_ack();
    if let Err(error) = input.write_message(&retry.config.prompt).await {
        input.close_input().await.ok();
        crate::process_group::shutdown_owned_child(
            &mut child,
            Some(process_group_id),
            crate::process_group::DEFAULT_GRACE,
        )
        .await;
        return Err(error).context("writing retried Claude Console input");
    }
    invocation.replace_process(pid, process_group_id);
    invocation.replace_input(input);
    let mut next_sink = sink.clone();
    next_sink.run_id = current.run_id;
    next_sink.turn_id = current.turn_id;
    next_sink.client_request_id = current.client_request_id;
    next_sink.process_group_id = Some(process_group_id);
    Ok((child, next_sink))
}

fn spawn_claude(
    config: &ClaudePrintRunConfig,
    args: &[String],
    stdout: File,
    stderr: File,
) -> std::io::Result<Child> {
    let mut command = Command::new(&config.claude_bin);
    command
        .args(args)
        .current_dir(&config.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    ManagedIdentity::new(ManagedProvider::Claude, &config.session_id)
        .with_run_id(&config.run_id)
        .apply(&mut command, &[]);
    #[cfg(unix)]
    crate::console_sink::own_process_group(&mut command, || Ok(()));
    command.spawn()
}

#[allow(clippy::too_many_arguments)]
fn spawn_record(
    config: &ClaudePrintRunConfig,
    provider_thread_id: &str,
    launch_id: &str,
    pid: Option<u32>,
    process_group_id: Option<i32>,
    stdout_path: &Path,
    stderr_path: &Path,
    argv: &[String],
) -> Value {
    json!({
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
    })
}

/// The text of Claude's "I never reached the model" message: a synthetic
/// assistant event flagged as an API error with `error: authentication_failed`
/// (`Not logged in · Please run /login`).
fn auth_failure_message(event: &Value) -> Option<&str> {
    if event.get("type").and_then(Value::as_str) != Some("assistant")
        || event.get("error").and_then(Value::as_str) != Some("authentication_failed")
        || event.get("is_api_error_message").and_then(Value::as_bool) != Some(true)
    {
        return None;
    }
    Some(
        event
            .pointer("/message/content/0/text")
            .and_then(Value::as_str)
            .unwrap_or("Claude authentication failed"),
    )
}

fn persist_projection_checkpoint(run_id: &str, read_offset: u64, pending_len: usize, seq: u64) {
    let complete_offset = read_offset.saturating_sub(pending_len as u64);
    if let Ok(registry) = crate::turn_claims::default_registry() {
        let _ = registry.mark_projection_checkpoint(run_id, complete_offset, seq);
    }
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

fn terminal_reason<'a>(terminal_state: &'a str, stderr: Option<&str>) -> &'a str {
    if terminal_state == "run_failed"
        && stderr.is_some_and(|text| {
            let lower = text.to_ascii_lowercase();
            (lower.contains("not logged in") && lower.contains("login"))
                || lower.contains("invalid authentication credentials")
                || lower.contains("authentication_error")
        })
    {
        "provider_auth_required"
    } else {
        terminal_state
    }
}

impl ClaudePrintSink {
    fn for_binding(&self, binding: &TurnBinding) -> Self {
        Self {
            run: self.run.for_binding(binding),
            provider_thread_id: self.provider_thread_id.clone(),
        }
    }

    async fn post_binding(&self) {
        self.post_event(&self.binding_event(json!({
            "provider_session_id": self.provider_thread_id,
        })));
    }

    async fn post_phase(&self, phase: &str, tool_name: Option<String>) {
        self.publish_phase(phase, tool_name.as_deref(), None);
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
        self.post_event(&self.run_event(
            "progress_signal",
            &format!("stdout:{seq}"),
            self.with_transport(json!({
                "progress_kind": "claude_print_stream",
                "seq": seq,
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "client_request_id": self.client_request_id,
                "provider_thread_id": self.provider_thread_id,
                "event": event,
            })),
        ));
    }

    async fn post_decode_gap(&self, seq: u64, error: &str, raw_line: &[u8]) {
        self.post_event(&self.run_event(
            "progress_signal",
            &format!("decode-gap:{seq}"),
            json!({
                "progress_kind": "claude_print_decode_gap",
                "seq": seq,
                "error": error,
                "raw_line": String::from_utf8_lossy(raw_line)
            }),
        ));
    }

    async fn post_delegation_snapshot(&self, snapshot: Value) {
        self.post_event(&self.delegation_event(CLAUDE_RUNTIME_SOURCE, "claude-console", snapshot));
    }

    async fn post_wake_signal(&self, wake: &crate::console_lifecycle::WakeRequest) {
        self.post_event(&self.wake_event(CLAUDE_RUNTIME_SOURCE, wake));
    }

    async fn post_terminal_with_lifecycle(
        &self,
        terminal_state: &str,
        exit_code: Option<i32>,
        stderr: Option<String>,
        invocation_state: Option<&str>,
        pending_count: Option<usize>,
        reason: Option<&str>,
    ) {
        self.persist_local_phase("finished", None, Utc::now());
        let mut payload = self.terminal_payload(
            terminal_state,
            reason.unwrap_or_else(|| terminal_reason(terminal_state, stderr.as_deref())),
            exit_code,
            stderr.as_deref(),
        );
        payload["provider_thread_id"] = json!(self.provider_thread_id);
        let payload = self.with_invocation(payload, invocation_state, pending_count);
        let terminal_event = self.run_event("terminal_signal", "terminal", payload);
        let terminal_error = (terminal_state == "run_failed")
            .then(|| stderr.clone())
            .flatten();
        self.hand_off_terminal(terminal_state, terminal_error, terminal_event);
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

enum RegistryUpdate {
    Snapshot(Vec<PendingItem>, Vec<PendingItem>),
    Item(PendingItem, bool),
}

fn is_response_start(event: &Value) -> bool {
    matches!(
        (
            event.get("type").and_then(Value::as_str),
            event.get("subtype").and_then(Value::as_str)
        ),
        (Some("system"), Some("init")) | (Some("assistant"), _)
    )
}

/// `--replay-user-messages` echoes every user message Claude consumes. Our
/// stdin inputs echo with no `origin`; Claude's own injected messages (task
/// notifications, monitor events) echo with an `origin`, and a resume-time
/// orphan notice may run without any echo at all.
fn is_replayed_user_input(event: &Value) -> bool {
    event.get("type").and_then(Value::as_str) == Some("user")
        && event.get("isReplay").and_then(Value::as_bool) == Some(true)
        && event.get("origin").is_none_or(Value::is_null)
}

fn is_task_completion_event(event: &Value) -> bool {
    event
        .get("subtype")
        .and_then(Value::as_str)
        .or_else(|| event.get("type").and_then(Value::as_str))
        .is_some_and(|kind| matches!(kind, "task_notification" | "task_updated"))
}

async fn apply_registry_update(
    invocation: &ConsoleInvocation,
    sink: &ClaudePrintSink,
    update: RegistryUpdate,
) -> bool {
    let (changed, close) = match update {
        RegistryUpdate::Snapshot(items, recent) => invocation.replace_pending(items, recent),
        RegistryUpdate::Item(item, pending) => invocation.update_pending_item(item, pending),
    };
    if changed || close {
        let _ = invocation.persist_pending_claim();
        if let Some((binding, snapshot)) = invocation.delegation_snapshot() {
            sink.for_binding(&binding)
                .post_delegation_snapshot(snapshot)
                .await;
        }
    }
    close
}
fn registry_update(event: &Value) -> Option<RegistryUpdate> {
    let kind = event.get("type").and_then(Value::as_str);
    let subtype = event.get("subtype").and_then(Value::as_str);
    if kind == Some("system") && subtype == Some("background_tasks_changed") {
        let tasks = event
            .get("tasks")
            .or_else(|| event.pointer("/data/tasks"))?
            .as_array()?;
        let mut pending = Vec::new();
        let mut recent = Vec::new();
        for task in tasks {
            let item = pending_item(task, None)?;
            if is_pending_status(&item.status) {
                pending.push(item);
            } else {
                recent.push(item);
            }
        }
        return Some(RegistryUpdate::Snapshot(pending, recent));
    }
    let task_event = subtype
        .or(kind)
        .filter(|kind| matches!(*kind, "task_started" | "task_updated" | "task_notification"))?;
    let task = event.get("task").unwrap_or(event);
    let fallback = match task_event {
        "task_started" => Some("running"),
        "task_notification" => Some("completed"),
        _ => None,
    };
    let item = pending_item(task, fallback)?;
    Some(RegistryUpdate::Item(
        item.clone(),
        is_pending_status(&item.status),
    ))
}

fn pending_item(task: &Value, fallback_status: Option<&str>) -> Option<PendingItem> {
    let id = task
        .get("id")
        .or_else(|| task.get("task_id"))
        .or_else(|| task.get("tool_use_id"))
        .and_then(Value::as_str)?
        .to_string();
    let status = task
        .get("status")
        .and_then(Value::as_str)
        .or(fallback_status)
        .unwrap_or("running")
        .to_ascii_lowercase();
    let kind = normalize_task_kind(
        task.get("kind")
            .or_else(|| task.get("type"))
            .or_else(|| task.get("task_type"))
            .and_then(Value::as_str)
            .unwrap_or("other"),
    );
    let description = task
        .get("description")
        .or_else(|| task.get("summary"))
        .or_else(|| task.get("subject"))
        .or_else(|| task.get("name"))
        .and_then(Value::as_str)
        .map(str::to_string);
    Some(PendingItem {
        id,
        kind,
        status,
        description,
    })
}

fn is_pending_status(status: &str) -> bool {
    matches!(
        status,
        "running" | "queued" | "pending" | "in_progress" | "working" | "started"
    )
}

fn normalize_task_kind(kind: &str) -> String {
    match kind.to_ascii_lowercase().as_str() {
        "monitor" | "watch" => "monitor".to_string(),
        "bash" | "shell" | "command" => "shell".to_string(),
        "agent" | "subagent" | "task" => "subagent".to_string(),
        "cron" | "schedule" | "scheduled" => "scheduled".to_string(),
        _ => "other".to_string(),
    }
}

fn task_trigger(event: &Value) -> Option<Value> {
    let subtype = event
        .get("subtype")
        .and_then(Value::as_str)
        .or_else(|| event.get("type").and_then(Value::as_str))?;
    if !matches!(subtype, "task_notification" | "task_updated") {
        return None;
    }
    let task = event.get("task").unwrap_or(event);
    let kind = normalize_task_kind(
        task.get("kind")
            .or_else(|| task.get("type"))
            .or_else(|| task.get("task_type"))
            .and_then(Value::as_str)
            .unwrap_or("other"),
    );
    let trigger_kind = match kind.as_str() {
        "monitor" => "monitor_event",
        "subagent" => "subagent_result",
        "scheduled" => "scheduled",
        _ => "task_completed",
    };
    let task_ids = task
        .get("id")
        .or_else(|| task.get("task_id"))
        .and_then(Value::as_str)
        .map(|id| vec![id.to_string()])
        .unwrap_or_default();
    let summary = task
        .get("description")
        .or_else(|| task.get("summary"))
        .or_else(|| task.get("message"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .chars()
        .take(180)
        .collect::<String>();
    Some(json!({
        "kind": trigger_kind,
        "task_ids": task_ids,
        "summary": summary
    }))
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

pub(crate) fn require_claude_lifecycle_hook() -> Result<()> {
    require_claude_lifecycle_hook_at(&claude_provider_home()?)
}

fn require_claude_lifecycle_hook_at(provider_home: &Path) -> Result<()> {
    let settings_path = provider_home.join("settings.json");
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
                .any(|command| {
                    command.contains("longhouse-hook.sh")
                        || command.contains("claude-lifecycle-hook")
                })
        })
    });
    if !registered {
        anyhow::bail!(
            "Claude Console requires the Longhouse SessionStart hook in {}; run `longhouse claude configure`",
            settings_path.display()
        );
    }
    Ok(())
}

fn build_claude_args(
    provider_thread_id: &str,
    is_resume: bool,
    model: Option<&str>,
) -> (Vec<String>, Vec<String>) {
    let mut args = vec![
        "--print".to_string(),
        "--output-format".to_string(),
        "stream-json".to_string(),
        "--verbose".to_string(),
        "--dangerously-skip-permissions".to_string(),
        "--input-format".to_string(),
        "stream-json".to_string(),
        "--replay-user-messages".to_string(),
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
    let recorded = args.clone();
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

fn append_output_file(path: &Path) -> Result<File> {
    Ok(OpenOptions::new()
        .append(true)
        .create(true)
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

    fn golden_claude_sink(home: &crate::console_sink_golden::GoldenHome) -> ClaudePrintSink {
        use crate::console_sink_golden::*;
        ClaudePrintSink {
            run: ConsoleRun {
                provider: &CLAUDE_CONSOLE,
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
            provider_thread_id: PROVIDER_THREAD.to_string(),
        }
    }

    #[test]
    fn console_sink_golden_claude() {
        use crate::console_sink_golden::*;
        let home = GoldenHome::new("claude");
        let sink = golden_claude_sink(&home);
        let phases = std::cell::RefCell::new(Vec::new());
        let captured = home.run(async {
            sink.post_phase("running", Some("Bash".to_string())).await;
            sink.post_stream_event(
                1,
                json!({"type": "system", "subtype": "init", "session_id": PROVIDER_THREAD}),
            )
            .await;
            sink.post_stream_event(
                2,
                json!({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}),
            )
            .await;
            sink.post_stream_event(3, json!({"type": "user"})).await;
            sink.post_decode_gap(4, "bad json", b"{oops").await;
            sink.post_delegation_snapshot(json!({"observed_at": "2026-10-07T00:00:00Z", "agents": []}))
                .await;
            sink.post_wake_signal(&crate::console_lifecycle::WakeRequest {
                invocation_id: LAUNCH.to_string(),
                wake_id: "golden-wake".to_string(),
                provider_thread_id: PROVIDER_THREAD.to_string(),
                trigger: json!({"kind": "task_notification"}),
            })
            .await;
            *phases.borrow_mut() = home.status_rows();
            sink.post_terminal_with_lifecycle(
                "run_failed",
                Some(1),
                Some("Not logged in · Please run /login".to_string()),
                Some("parked"),
                Some(2),
                None,
            )
            .await;
        });
        let mut captured = captured;
        captured["status_before_terminal"] = json!(phases.into_inner());
        assert_golden("claude", &captured);
    }
    use crate::console_lifecycle::conformance::{
        self, LifecycleScenario, ScenarioFuture, ScenarioOutcome, ScenarioRunner,
    };

    #[test]
    fn fresh_and_resume_argv_use_stream_json_stdin_without_embedding_prompts() {
        let provider_id = Uuid::new_v4().to_string();
        let (fresh, recorded) = build_claude_args(&provider_id, false, Some("claude-sonnet-4-5"));
        assert_eq!(
            fresh,
            vec![
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
                "--input-format",
                "stream-json",
                "--replay-user-messages",
                "--session-id",
                &provider_id,
                "--model",
                "claude-sonnet-4-5",
            ]
        );
        assert_eq!(recorded, fresh);
        assert!(!fresh.contains(&"secret prompt".to_string()));

        let (resume, _) = build_claude_args(&provider_id, true, None);
        assert_eq!(resume[8], "--resume");
        assert_eq!(resume[9], provider_id);
        assert!(!resume.iter().any(|arg| arg == "--"));
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
    fn provider_auth_failures_have_an_actionable_terminal_reason() {
        assert_eq!(
            terminal_reason(
                "run_failed",
                Some("Not logged in · Please run /login from the Claude CLI"),
            ),
            "provider_auth_required"
        );
        assert_eq!(
            terminal_reason("run_failed", Some("network connection reset")),
            "run_failed"
        );
        assert_eq!(
            terminal_reason("run_completed", Some("Not logged in · Please run /login")),
            "run_completed"
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
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
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
                image_paths: Vec::new(),
                resume_provider_thread_id: resume,
                model: None,
                permission_mode: "bypass".to_string(),
                origin: "user".to_string(),
                wake_id: None,
                invocation_id: None,
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

    /// The exact event Claude 2.1.284 wrote when it found no credential
    /// (recorded from a real failed Console turn, session id elided).
    fn recorded_auth_failure() -> Value {
        json!({
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "role": "assistant",
                "content": [{"type": "text", "text": "Not logged in \u{b7} Please run /login"}]
            },
            "error": "authentication_failed",
            "is_api_error_message": true
        })
    }

    #[test]
    fn auth_failure_is_recognised_only_from_the_flagged_synthetic_message() {
        assert_eq!(
            auth_failure_message(&recorded_auth_failure()),
            Some("Not logged in \u{b7} Please run /login")
        );
        // Ordinary assistant text that merely says the words is not the signal.
        let quoted = json!({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Not logged in \u{b7} Please run /login"}]}
        });
        assert_eq!(auth_failure_message(&quoted), None);
        // Nor is another API error.
        let mut other = recorded_auth_failure();
        other["error"] = json!("rate_limit");
        assert_eq!(auth_failure_message(&other), None);
        // Nor a non-assistant event carrying the same fields.
        let mut wrong_type = recorded_auth_failure();
        wrong_type["type"] = json!("result");
        assert_eq!(auth_failure_message(&wrong_type), None);
    }

    /// A Python fake Claude CLI that speaks stream-json on stdout and stdin.
    struct FakeClaude {
        dir: tempfile::TempDir,
        bin: PathBuf,
    }

    impl FakeClaude {
        fn new(failures: usize) -> Self {
            use std::os::unix::fs::PermissionsExt;
            let dir = tempfile::tempdir().unwrap();
            let bin = dir.path().join("claude");
            std::fs::write(dir.path().join("failures"), failures.to_string()).unwrap();
            std::fs::write(
                &bin,
                r#"#!/usr/bin/env python3
import json
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(__file__).parent
count_file = root / "count"
count = int(count_file.read_text() if count_file.exists() else "0") + 1
count_file.write_text(str(count))
args = sys.argv[1:]
with (root / "argv.log").open("a") as output:
    output.write(json.dumps(args) + "\n")
with (root / "pid.log").open("a") as output:
    output.write(str(__import__("os").getpid()) + "\n")
session_id = ""
for index, arg in enumerate(args[:-1]):
    if arg in ("--session-id", "--resume"):
        session_id = args[index + 1]

def emit(value):
    print(json.dumps(value), flush=True)

def assistant(text):
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})

task = {"id": "task-1", "kind": "monitor", "status": "running", "description": "watch project files"}
emit({"type": "system", "subtype": "init", "session_id": session_id})
raw = sys.stdin.readline()
if not raw:
    sys.exit(2)
request = json.loads(raw)
message = request["message"]
prompt = next((part.get("text", "") for part in message.get("content", []) if part.get("type") == "text"), "")
if prompt.startswith("scenario=orphan"):
    # Claude's resume-time orphan check: it runs its own notice, which ends
    # in a result with no model turn, before it consumes the queued input.
    emit({"type": "system", "subtype": "task_notification", "task_id": "orphan-1", "status": "stopped", "summary": "Background shell command didn't finish before the previous session ended"})
    emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "result": ""})
    time.sleep(0.6)
emit({"type": "user", "message": message, "isReplay": True})
if count <= int((root / "failures").read_text()):
    emit({"type": "assistant", "error": "authentication_failed", "is_api_error_message": True, "message": {"content": [{"type": "text", "text": "Not logged in - Please run /login"}]}})
    emit({"type": "result", "subtype": "success", "is_error": True})
    sys.exit(1)

if prompt.startswith("scenario=background") or prompt.startswith("scenario=park") or prompt.startswith("scenario=restart") or prompt.startswith("scenario=wake") or prompt.startswith("scenario=drain") or prompt.startswith("scenario=stop"):
    if prompt.startswith("scenario=stop"):
        background = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (root / "task.pid").write_text(str(background.pid))
    emit({"type": "system", "subtype": "background_tasks_changed", "tasks": [task]})
    assistant("waiting for background work")
    emit({"type": "result", "subtype": "success", "is_error": False})
    if prompt.startswith("scenario=wake_user"):
        time.sleep(0.15)
        emit({"type": "system", "subtype": "task_notification", "task": dict(task, status="completed")})
        assistant("background task completed")
        time.sleep(0.5)
        emit({"type": "system", "subtype": "background_tasks_changed", "tasks": [task]})
        for next_raw in sys.stdin:
            next_request = json.loads(next_raw)
            emit({"type": "user", "message": next_request["message"], "isReplay": True})
            assistant("user message handled")
            emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
            emit({"type": "result", "subtype": "success", "is_error": False})
            break
    elif prompt.startswith("scenario=wake_immediate"):
        assistant("background task completed")
        emit({"type": "result", "subtype": "success", "is_error": False})
    elif prompt.startswith("scenario=wake") or prompt.startswith("scenario=drain"):
        time.sleep(0.15)
        emit({"type": "system", "subtype": "task_notification", "task": dict(task, status="completed")})
        assistant("background task completed")
        time.sleep(0.5)
        next_tasks = [] if prompt.startswith("scenario=drain") else [task]
        emit({"type": "system", "subtype": "background_tasks_changed", "tasks": next_tasks})
        emit({"type": "result", "subtype": "success", "is_error": False})
        for next_raw in sys.stdin:
            next_request = json.loads(next_raw)
            emit({"type": "user", "message": next_request["message"], "isReplay": True})
            assistant("user message handled")
            emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
            emit({"type": "result", "subtype": "success", "is_error": False})
            break
else:
    assistant("fake answer")
    emit({"type": "result", "subtype": "success", "is_error": False})
for next_raw in sys.stdin:
    next_request = json.loads(next_raw)
    emit({"type": "user", "message": next_request["message"], "isReplay": True})
    assistant("user message handled")
    emit({"type": "system", "subtype": "background_tasks_changed", "tasks": []})
    emit({"type": "result", "subtype": "success", "is_error": False})
    break
for _ in sys.stdin:
    pass
"#,
            )
            .unwrap();
            std::fs::set_permissions(&bin, std::fs::Permissions::from_mode(0o755)).unwrap();
            Self { dir, bin }
        }

        fn launches(&self) -> Vec<String> {
            std::fs::read_to_string(self.dir.path().join("argv.log"))
                .unwrap_or_default()
                .lines()
                .map(str::to_string)
                .collect()
        }

        fn pids(&self) -> Vec<u32> {
            std::fs::read_to_string(self.dir.path().join("pid.log"))
                .unwrap_or_default()
                .lines()
                .filter_map(|line| line.parse().ok())
                .collect()
        }
    }
    struct FakeHome {
        _guard: std::sync::MutexGuard<'static, ()>,
        root: tempfile::TempDir,
        previous_home: Option<std::ffi::OsString>,
        previous_config: Option<std::ffi::OsString>,
    }

    impl FakeHome {
        fn new() -> Self {
            let guard = crate::console_adapter::longhouse_home_test_guard();
            let root = tempfile::tempdir().unwrap();
            let previous_home = std::env::var_os("LONGHOUSE_HOME");
            let previous_config = std::env::var_os("CLAUDE_CONFIG_DIR");
            let provider_home = root.path().join("claude-home");
            std::fs::create_dir_all(&provider_home).unwrap();
            std::fs::write(
                provider_home.join("settings.json"),
                serde_json::to_vec(&json!({
                    "hooks": {"SessionStart": [{"hooks": [{"command": "/x/longhouse-hook.sh"}]}]}
                }))
                .unwrap(),
            )
            .unwrap();
            unsafe {
                std::env::set_var("LONGHOUSE_HOME", root.path().join("longhouse"));
                std::env::set_var("CLAUDE_CONFIG_DIR", provider_home);
            }
            Self {
                _guard: guard,
                root,
                previous_home,
                previous_config,
            }
        }

        fn cwd(&self) -> &Path {
            self.root.path()
        }

        fn events(&self) -> Vec<Value> {
            let Ok(outbox) = crate::config::get_agent_runtime_events_outbox_dir() else {
                return Vec::new();
            };
            std::fs::read_dir(outbox)
                .into_iter()
                .flatten()
                .filter_map(|entry| std::fs::read(entry.ok()?.path()).ok())
                .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .collect()
        }
    }

    impl Drop for FakeHome {
        fn drop(&mut self) {
            for (key, previous) in [
                ("LONGHOUSE_HOME", self.previous_home.take()),
                ("CLAUDE_CONFIG_DIR", self.previous_config.take()),
            ] {
                match previous {
                    Some(value) => unsafe { std::env::set_var(key, value) },
                    None => unsafe { std::env::remove_var(key) },
                }
            }
        }
    }

    async fn start_fake_turn(
        home: &FakeHome,
        fake: &FakeClaude,
        session_id: &str,
        thread_id: &str,
        prompt: &str,
        resume_provider_thread_id: Option<String>,
        origin: &str,
        wake_id: Option<String>,
        invocation_id: Option<String>,
    ) -> ClaudePrintRunSummary {
        let run_id = Uuid::new_v4().to_string();
        let turn_id = Uuid::new_v4().to_string();
        let client_request_id = format!("fake-{run_id}");
        let registry = crate::turn_claims::default_registry().unwrap();
        assert!(matches!(
            registry
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
        start_claude_print_turn(ClaudePrintRunConfig {
            session_id: session_id.to_string(),
            thread_id: thread_id.to_string(),
            turn_id: Some(turn_id),
            run_id,
            client_request_id: Some(client_request_id),
            cwd: home.cwd().to_path_buf(),
            claude_bin: fake.bin.to_string_lossy().to_string(),
            prompt: prompt.to_string(),
            image_paths: Vec::new(),
            resume_provider_thread_id,
            model: None,
            permission_mode: "bypass".to_string(),
            origin: origin.to_string(),
            wake_id,
            invocation_id,
            machine_name: "fake-box".to_string(),
            local_db_path: None,
        })
        .await
        .unwrap()
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
                "Claude run {run_id} did not settle: {claim:?}"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    async fn wait_for_event(home: &FakeHome, session_id: &str, kind: &str) -> Value {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
        loop {
            if let Some(event) = home
                .events()
                .into_iter()
                .find(|event| event["session_id"] == session_id && event["kind"] == kind)
            {
                return event;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "Claude did not post {kind} for session {session_id}"
            );
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    }

    fn terminal_event<'a>(events: &'a [Value], session_id: &str, run_id: &str) -> &'a Value {
        events
            .iter()
            .find(|event| {
                event["session_id"] == session_id
                    && event["run_id"] == run_id
                    && event["kind"] == "terminal_signal"
            })
            .expect("terminal runtime event")
    }

    async fn assert_fake_groups_gone(fake: &FakeClaude) {
        let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
        loop {
            let live_pid = fake
                .pids()
                .into_iter()
                .find(|pid| crate::process_group::group_is_alive(*pid as i32));
            if let Some(pid) = live_pid {
                assert!(
                    tokio::time::Instant::now() < deadline,
                    "fake Claude process group {pid} was left running"
                );
                tokio::time::sleep(Duration::from_millis(10)).await;
            } else {
                return;
            }
        }
    }

    fn run_claude_scenario(scenario: LifecycleScenario) -> ScenarioFuture {
        Box::pin(async move {
            run_claude_scenario_inner(scenario).await;
            ScenarioOutcome::Passed
        })
    }

    async fn run_claude_scenario_inner(scenario: LifecycleScenario) {
        let home = FakeHome::new();
        let fake = FakeClaude::new(0);
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let prompt = match scenario {
            LifecycleScenario::Plain => "plain message",
            LifecycleScenario::Background | LifecycleScenario::UserSend => "scenario=park",
            LifecycleScenario::WakePending => "scenario=wake",
            LifecycleScenario::WakeDrained | LifecycleScenario::WakeUnboundDrained => {
                "scenario=drain"
            }
            LifecycleScenario::WakeUserSend => "scenario=wake_user",
            LifecycleScenario::WakeImmediateUnbound => "scenario=wake_immediate",
            LifecycleScenario::Restart => "scenario=restart",
            LifecycleScenario::StopWhileParked => "scenario=stop",
        };
        let first = start_fake_turn(
            &home,
            &fake,
            &session_id,
            &thread_id,
            prompt,
            None,
            "user",
            None,
            None,
        )
        .await;
        let first_claim = wait_for_terminal(&first.run_id).await;
        let events = home.events();

        match scenario {
            LifecycleScenario::Plain => {
                assert_eq!(
                    first_claim.result.as_ref().unwrap()["terminal_state"],
                    "run_completed"
                );
                assert_eq!(first_claim.invocation_state.as_deref(), Some("closed"));
                assert_eq!(
                    terminal_event(&events, &session_id, &first.run_id)["payload"]["invocation"]
                        ["state"],
                    "closed"
                );
                assert_eq!(fake.pids().len(), 1);
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::Background | LifecycleScenario::UserSend => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                let delegation = events
                    .iter()
                    .find(|event| {
                        event["session_id"] == session_id
                            && event["run_id"] == first.run_id
                            && event["kind"] == "delegation_signal"
                    })
                    .expect("pending task snapshot");
                assert_eq!(delegation["provider"], "claude");
                assert_eq!(delegation["source"], CLAUDE_RUNTIME_SOURCE);
                assert_eq!(delegation["payload"]["delegation"]["count"], 1);
                assert!(crate::process_group::group_is_alive(
                    first.process_group_id.unwrap()
                ));
                let prompt = "continue with this exact text";
                let second = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    prompt,
                    Some(first.provider_thread_id.clone()),
                    "user",
                    None,
                    None,
                )
                .await;
                assert_eq!(second.pid, first.pid);
                assert_eq!(second.launch_id, first.launch_id);
                let second_claim = wait_for_terminal(&second.run_id).await;
                assert_eq!(second_claim.origin.as_deref(), Some("user"));
                assert!(second_claim.adopted_parked_invocation);
                assert_eq!(second_claim.invocation_state.as_deref(), Some("closed"));
                let events = home.events();
                if matches!(scenario, LifecycleScenario::UserSend) {
                    assert!(events.iter().any(|event| {
                        event["run_id"] == second.run_id
                            && event["kind"] == "progress_signal"
                            && event["payload"]["event"]["type"] == "user"
                            && event["payload"]["event"]["message"]["content"][0]["text"] == prompt
                    }));
                }
                assert_eq!(fake.pids().len(), 1);
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::StopWhileParked => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                assert_eq!(first_claim.pending_count, 1);
                let delegation = wait_for_event(&home, &session_id, "delegation_signal").await;
                assert_eq!(
                    delegation["payload"]["delegation"]["items"][0]["id"],
                    "task-1"
                );
                let process_group_id = first.process_group_id.unwrap();
                let background_pid: u32 = std::fs::read_to_string(fake.dir.path().join("task.pid"))
                    .unwrap()
                    .parse()
                    .unwrap();
                assert!(
                    crate::process_identity::try_collect_process_fact(background_pid).is_some()
                );
                let invocation = crate::console_lifecycle::lookup_launch(&first.launch_id).unwrap();
                let started = std::time::Instant::now();
                let stopped = close_parked_claude_invocation(&first_claim, invocation, "fake-box")
                    .await
                    .unwrap()
                    .expect("parked Claude invocation should close");
                assert!(started.elapsed() >= crate::console_lifecycle::USER_STOP_INPUT_GRACE);
                assert_eq!(stopped.stopped.len(), 1);
                assert_eq!(stopped.stopped[0].id, "task-1");
                assert!(!crate::process_group::group_is_alive(process_group_id));
                assert!(
                    crate::process_identity::try_collect_process_fact(background_pid).is_none()
                );
                let events = home.events();
                let closed = events
                    .iter()
                    .find(|event| event["kind"] == "invocation_closed")
                    .expect("user-stop close event");
                assert_eq!(closed["run_id"], first.run_id);
                assert_eq!(closed["provider"], "claude");
                assert_eq!(closed["source"], CLAUDE_RUNTIME_SOURCE);
                assert_eq!(closed["payload"]["invocation_id"], first.launch_id);
                assert_eq!(closed["payload"]["reason"], "user_stop");
                assert_eq!(
                    closed["payload"]["stopped"][0]["description"],
                    "watch project files"
                );
                let empty = events
                    .iter()
                    .find(|event| {
                        event["kind"] == "delegation_signal"
                            && event["dedupe_key"]
                                == format!("close:{}:delegation", first.launch_id)
                    })
                    .expect("empty delegation snapshot after close");
                assert_eq!(empty["payload"]["delegation"]["count"], 0);

                let resumed = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "fresh turn after stop",
                    Some(first.provider_thread_id.clone()),
                    "user",
                    None,
                    None,
                )
                .await;
                assert_ne!(resumed.pid, first.pid);
                assert_ne!(resumed.launch_id, first.launch_id);
                assert_eq!(resumed.provider_thread_id, first.provider_thread_id);
                assert_eq!(
                    wait_for_terminal(&resumed.run_id)
                        .await
                        .invocation_state
                        .as_deref(),
                    Some("closed")
                );
                assert_eq!(fake.pids().len(), 2);
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::WakeUserSend => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                let wake = wait_for_event(&home, &session_id, "wake_signal").await;
                let prompt = "user input joins the unbound wake response";
                let user_turn = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    prompt,
                    Some(first.provider_thread_id.clone()),
                    "user",
                    None,
                    None,
                )
                .await;
                assert_eq!(user_turn.pid, first.pid);
                assert_eq!(user_turn.launch_id, first.launch_id);
                let user_claim = wait_for_terminal(&user_turn.run_id).await;
                assert_eq!(user_claim.origin.as_deref(), Some("user"));
                assert!(user_claim.adopted_parked_invocation);
                assert_eq!(user_claim.invocation_state.as_deref(), Some("closed"));
                let events = home.events();
                assert!(events.iter().any(|event| {
                    event["run_id"] == user_turn.run_id
                        && event["kind"] == "progress_signal"
                        && event["payload"]["event"]["type"] == "assistant"
                        && event["payload"]["event"]["message"]["content"][0]["text"]
                            == "background task completed"
                }));
                assert!(events.iter().any(|event| {
                    event["run_id"] == user_turn.run_id
                        && event["kind"] == "progress_signal"
                        && event["payload"]["event"]["type"] == "user"
                        && event["payload"]["event"]["message"]["content"][0]["text"] == prompt
                }));
                assert!(events.iter().any(|event| {
                    event["kind"] == "wake_signal"
                        && event["payload"]["wake_id"] == wake["payload"]["wake_id"]
                }));
                assert_eq!(fake.pids().len(), 1);
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::WakePending | LifecycleScenario::WakeDrained => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                let wake = wait_for_event(&home, &session_id, "wake_signal").await;
                assert_eq!(wake["source"], CLAUDE_RUNTIME_SOURCE);
                assert_eq!(wake["payload"]["invocation_id"], first.launch_id);
                assert_eq!(
                    wake["payload"]["provider_thread_id"],
                    first.provider_thread_id
                );
                assert_eq!(wake["payload"]["trigger"]["task_ids"][0], "task-1");
                let wake_run = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "",
                    Some(first.provider_thread_id.clone()),
                    "wake",
                    wake["payload"]["wake_id"].as_str().map(str::to_string),
                    wake["payload"]["invocation_id"]
                        .as_str()
                        .map(str::to_string),
                )
                .await;
                assert_eq!(wake_run.pid, first.pid);
                let wake_claim = wait_for_terminal(&wake_run.run_id).await;
                assert_eq!(wake_claim.origin.as_deref(), Some("wake"));
                assert!(wake_claim.adopted_parked_invocation);
                let expected_state = if matches!(scenario, LifecycleScenario::WakeDrained) {
                    "closed"
                } else {
                    "parked"
                };
                assert_eq!(wake_claim.invocation_state.as_deref(), Some(expected_state));
                let events = home.events();
                assert!(events.iter().any(|event| {
                    event["run_id"] == wake_run.run_id
                        && event["kind"] == "progress_signal"
                        && event["payload"]["event"]["type"] == "assistant"
                }));
                assert_eq!(fake.pids().len(), 1);
                if matches!(scenario, LifecycleScenario::WakeDrained) {
                    assert!(events.iter().any(|event| {
                        event["run_id"] == wake_run.run_id
                            && event["kind"] == "delegation_signal"
                            && event["payload"]["delegation"]["count"] == 0
                    }));
                }
                if matches!(scenario, LifecycleScenario::WakePending) {
                    let cleanup = start_fake_turn(
                        &home,
                        &fake,
                        &session_id,
                        &thread_id,
                        "finish pending work",
                        Some(first.provider_thread_id.clone()),
                        "user",
                        None,
                        None,
                    )
                    .await;
                    wait_for_terminal(&cleanup.run_id).await;
                } else {
                    let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
                    while crate::console_lifecycle::lookup_launch(&first.launch_id).is_some()
                        || crate::process_group::group_is_alive(first.process_group_id.unwrap())
                    {
                        assert!(tokio::time::Instant::now() < deadline);
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                    tokio::time::sleep(Duration::from_millis(50)).await;
                    let resumed = start_fake_turn(
                        &home,
                        &fake,
                        &session_id,
                        &thread_id,
                        "resume after background work drained",
                        Some(first.provider_thread_id.clone()),
                        "user",
                        None,
                        None,
                    )
                    .await;
                    assert_ne!(resumed.pid, first.pid);
                    assert_ne!(resumed.launch_id, first.launch_id);
                    assert_eq!(resumed.provider_thread_id, first.provider_thread_id);
                    let resumed_claim = wait_for_terminal(&resumed.run_id).await;
                    assert_eq!(resumed_claim.invocation_state.as_deref(), Some("closed"));
                    assert_eq!(fake.pids().len(), 2);
                }
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::WakeUnboundDrained => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                let invocation = crate::console_lifecycle::lookup_launch(&first.launch_id).unwrap();
                let wake = wait_for_event(&home, &session_id, "wake_signal").await;
                let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
                while invocation.state() != InvocationState::Closed {
                    assert!(tokio::time::Instant::now() < deadline);
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                while crate::console_lifecycle::lookup_launch(&first.launch_id).is_some()
                    || crate::process_group::group_is_alive(first.process_group_id.unwrap())
                {
                    assert!(tokio::time::Instant::now() < deadline);
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                assert!(crate::console_lifecycle::lookup_retained_wake(
                    &first.launch_id,
                    wake["payload"]["wake_id"].as_str().unwrap(),
                )
                .is_some());

                let events = home.events();
                assert!(!events.iter().any(|event| {
                    event["kind"] == "progress_signal"
                        && event["payload"]["event"]["message"]["content"][0]["text"]
                            == "background task completed"
                }));
                let wake_run = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "",
                    Some(first.provider_thread_id.clone()),
                    "wake",
                    wake["payload"]["wake_id"].as_str().map(str::to_string),
                    wake["payload"]["invocation_id"]
                        .as_str()
                        .map(str::to_string),
                )
                .await;
                assert_eq!(wake_run.pid, None);
                assert_eq!(wake_run.process_group_id, None);
                let wake_claim = wait_for_terminal(&wake_run.run_id).await;
                assert_eq!(wake_claim.origin.as_deref(), Some("wake"));
                assert!(wake_claim.adopted_parked_invocation);
                assert_eq!(wake_claim.invocation_state.as_deref(), Some("closed"));
                assert_eq!(
                    wake_claim.result.as_ref().unwrap()["terminal_state"],
                    "run_completed"
                );
                let events = home.events();
                assert!(events.iter().any(|event| {
                    event["run_id"] == wake_run.run_id
                        && event["kind"] == "progress_signal"
                        && event["payload"]["event"]["type"] == "assistant"
                        && event["payload"]["event"]["message"]["content"][0]["text"]
                            == "background task completed"
                }));
                assert_eq!(
                    terminal_event(&events, &session_id, &wake_run.run_id)["payload"]
                        ["terminal_state"],
                    "run_completed"
                );

                let stale_wake = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "",
                    Some(first.provider_thread_id.clone()),
                    "wake",
                    wake["payload"]["wake_id"].as_str().map(str::to_string),
                    wake["payload"]["invocation_id"]
                        .as_str()
                        .map(str::to_string),
                )
                .await;
                let stale_claim = wait_for_terminal(&stale_wake.run_id).await;
                assert_eq!(stale_claim.state, "terminal");
                let events = home.events();
                let stale_terminal = terminal_event(&events, &session_id, &stale_wake.run_id);
                assert_eq!(
                    stale_terminal["payload"]["terminal_reason"],
                    "wake_target_gone"
                );
                assert_eq!(stale_terminal["payload"]["terminal_state"], "run_cancelled");

                let resumed = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "resume after the unbound wake closed",
                    Some(first.provider_thread_id.clone()),
                    "user",
                    None,
                    None,
                )
                .await;
                assert_ne!(resumed.pid, first.pid);
                assert_ne!(resumed.launch_id, first.launch_id);
                assert_eq!(resumed.provider_thread_id, first.provider_thread_id);
                let resumed_claim = wait_for_terminal(&resumed.run_id).await;
                assert_eq!(resumed_claim.invocation_state.as_deref(), Some("closed"));
                assert_eq!(fake.pids().len(), 2);
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::WakeImmediateUnbound => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                let invocation = crate::console_lifecycle::lookup_launch(&first.launch_id).unwrap();
                let wake = wait_for_event(&home, &session_id, "wake_signal").await;
                let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
                while invocation.state() == InvocationState::Responding
                    || invocation.pending_wake_id().is_some()
                {
                    assert!(tokio::time::Instant::now() < deadline);
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                assert_eq!(invocation.state(), InvocationState::Parked);
                let wake_id = wake["payload"]["wake_id"].as_str().unwrap();
                assert!(
                    crate::console_lifecycle::lookup_retained_wake(&first.launch_id, wake_id)
                        .is_some()
                );

                let wake_run = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "",
                    Some(first.provider_thread_id.clone()),
                    "wake",
                    Some(wake_id.to_string()),
                    wake["payload"]["invocation_id"]
                        .as_str()
                        .map(str::to_string),
                )
                .await;
                assert_eq!(wake_run.pid, first.pid);
                assert_eq!(wake_run.process_group_id, first.process_group_id);
                let wake_claim = wait_for_terminal(&wake_run.run_id).await;
                assert_eq!(wake_claim.origin.as_deref(), Some("wake"));
                assert!(wake_claim.adopted_parked_invocation);
                assert_eq!(wake_claim.invocation_state.as_deref(), Some("parked"));
                assert_eq!(
                    wake_claim.result.as_ref().unwrap()["terminal_state"],
                    "run_completed"
                );
                let events = home.events();
                assert!(events.iter().any(|event| {
                    event["run_id"] == wake_run.run_id
                        && event["kind"] == "progress_signal"
                        && event["payload"]["event"]["type"] == "assistant"
                        && event["payload"]["event"]["message"]["content"][0]["text"]
                            == "background task completed"
                }));
                assert_eq!(
                    terminal_event(&events, &session_id, &wake_run.run_id)["payload"]
                        ["terminal_state"],
                    "run_completed"
                );
                assert!(
                    crate::console_lifecycle::lookup_retained_wake(&first.launch_id, wake_id)
                        .is_none()
                );

                let cleanup = start_fake_turn(
                    &home,
                    &fake,
                    &session_id,
                    &thread_id,
                    "finish pending work",
                    Some(first.provider_thread_id.clone()),
                    "user",
                    None,
                    None,
                )
                .await;
                assert_eq!(cleanup.pid, first.pid);
                let cleanup_claim = wait_for_terminal(&cleanup.run_id).await;
                assert_eq!(cleanup_claim.invocation_state.as_deref(), Some("closed"));
                assert_fake_groups_gone(&fake).await;
            }
            LifecycleScenario::Restart => {
                assert_eq!(first_claim.invocation_state.as_deref(), Some("parked"));
                crate::console_lifecycle::unregister(&first.launch_id);
                assert_eq!(
                    recover_claude_print_turns("fake-box", None).await.unwrap(),
                    1
                );
                let deadline = tokio::time::Instant::now() + Duration::from_secs(3);
                while crate::process_group::group_is_alive(first.process_group_id.unwrap())
                    && tokio::time::Instant::now() < deadline
                {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                let recovered = crate::turn_claims::default_registry()
                    .unwrap()
                    .read(&first.run_id)
                    .unwrap();
                assert_eq!(recovered.invocation_state.as_deref(), Some("closed"));
                let recovered_events = home.events();
                let closed = recovered_events
                    .iter()
                    .find(|event| event["kind"] == "invocation_closed")
                    .expect("restart close event");
                assert_eq!(closed["run_id"], first.run_id);
                assert_eq!(closed["provider"], "claude");
                assert_eq!(closed["source"], CLAUDE_RUNTIME_SOURCE);
                assert_eq!(closed["payload"]["reason"], "machine_agent_restart");
                assert_eq!(closed["payload"]["stopped"][0]["id"], "task-1");
                let empty = recovered_events
                    .iter()
                    .find(|event| {
                        event["kind"] == "delegation_signal"
                            && event["dedupe_key"]
                                == format!("close:{}:delegation", first.launch_id)
                    })
                    .expect("empty delegation snapshot after restart close");
                assert_eq!(empty["payload"]["delegation"]["count"], 0);
                assert_fake_groups_gone(&fake).await;
            }
        }
    }

    #[tokio::test]
    async fn console_lifecycle_conformance_runs_phase_one_scenarios() {
        let adapters: [(&str, ScenarioRunner); 1] = [("claude", run_claude_scenario)];
        conformance::run_phase_one(&adapters).await;
        for scenario in [
            LifecycleScenario::WakeUserSend,
            LifecycleScenario::WakeUnboundDrained,
            LifecycleScenario::WakeImmediateUnbound,
        ] {
            assert_eq!(run_claude_scenario(scenario).await, ScenarioOutcome::Passed);
        }
    }

    /// 2026-10-07, session ca6e9d23: on `--resume` Claude ran its own orphan
    /// notice and emitted a `result` before consuming the user's queued input.
    /// That result must not end the user turn or close the invocation.
    #[tokio::test]
    async fn a_provider_result_before_the_user_input_is_consumed_does_not_end_the_turn() {
        let home = FakeHome::new();
        let fake = FakeClaude::new(0);
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let prompt = "scenario=orphan keep pushing forward with tests";
        let run = start_fake_turn(
            &home,
            &fake,
            &session_id,
            &thread_id,
            prompt,
            None,
            "user",
            None,
            None,
        )
        .await;
        let claim = wait_for_terminal(&run.run_id).await;
        assert_eq!(
            claim.result.as_ref().unwrap()["terminal_state"],
            "run_completed"
        );
        assert_eq!(claim.invocation_state.as_deref(), Some("closed"));
        let events = home.events();
        let mut run_events = events
            .iter()
            .filter(|event| event["run_id"] == run.run_id && event["kind"] == "progress_signal")
            .collect::<Vec<_>>();
        run_events.sort_by_key(|event| event["payload"]["seq"].as_u64());
        let run_events = run_events
            .into_iter()
            .map(|event| &event["payload"]["event"])
            .collect::<Vec<_>>();
        let replay = run_events
            .iter()
            .position(|event| {
                event["type"] == "user" && event["message"]["content"][0]["text"] == prompt
            })
            .expect("the user input reached Claude");
        let answer = run_events
            .iter()
            .position(|event| {
                event["type"] == "assistant"
                    && event["message"]["content"][0]["text"] == "fake answer"
            })
            .expect("Claude answered the user input inside the user turn");
        assert!(replay < answer);
        assert_eq!(fake.pids().len(), 1);
        assert_fake_groups_gone(&fake).await;
    }

    /// Run one Console turn through the production adapter against `fake`,
    /// returning the terminal claim result and every runtime event it posted.
    async fn run_fake_turn(fake: &FakeClaude) -> (Value, Vec<Value>) {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        let previous_config = std::env::var_os("CLAUDE_CONFIG_DIR");
        let provider_home = temp.path().join("claude-home");
        std::fs::create_dir_all(&provider_home).unwrap();
        std::fs::write(
            provider_home.join("settings.json"),
            serde_json::to_vec(&json!({
                "hooks": {"SessionStart": [{"hooks": [{"command": "/x/longhouse-hook.sh"}]}]}
            }))
            .unwrap(),
        )
        .unwrap();
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", temp.path().join("longhouse"));
            std::env::set_var("CLAUDE_CONFIG_DIR", &provider_home);
        }

        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        let turn_id = Uuid::new_v4().to_string();
        let registry = crate::turn_claims::default_registry().unwrap();
        assert!(matches!(
            registry
                .claim(
                    &run_id,
                    &session_id,
                    &thread_id,
                    Some(&turn_id),
                    None,
                    "claude"
                )
                .unwrap(),
            crate::turn_claims::ClaimOutcome::Acquired
        ));
        let summary = start_claude_print_turn(ClaudePrintRunConfig {
            session_id,
            thread_id,
            turn_id: Some(turn_id),
            run_id: run_id.clone(),
            client_request_id: None,
            cwd: temp.path().to_path_buf(),
            claude_bin: fake.bin.to_string_lossy().to_string(),
            prompt: "hello".to_string(),
            image_paths: Vec::new(),
            resume_provider_thread_id: None,
            model: None,
            permission_mode: "bypass".to_string(),
            origin: "user".to_string(),
            wake_id: None,
            invocation_id: None,
            machine_name: "fake-box".to_string(),
            local_db_path: None,
        })
        .await
        .unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(30);
        let claim = loop {
            let claim = registry.read(&summary.run_id).unwrap();
            if claim.state == "terminal" {
                break claim;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "fake Claude turn never settled"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
        let events = std::fs::read_dir(&outbox)
            .map(|entries| {
                entries
                    .filter_map(|entry| std::fs::read(entry.ok()?.path()).ok())
                    .filter_map(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                    .collect()
            })
            .unwrap_or_default();
        for (key, previous) in [
            ("LONGHOUSE_HOME", previous_home),
            ("CLAUDE_CONFIG_DIR", previous_config),
        ] {
            match previous {
                Some(value) => unsafe { std::env::set_var(key, value) },
                None => unsafe { std::env::remove_var(key) },
            }
        }
        (claim.result.unwrap_or(Value::Null), events)
    }

    #[tokio::test]
    async fn a_preflight_auth_failure_is_relaunched_and_never_reaches_the_timeline() {
        let fake = FakeClaude::new(1);
        let (result, events) = run_fake_turn(&fake).await;

        assert_eq!(result["terminal_state"], "run_completed");
        let launches = fake.launches();
        assert_eq!(
            launches.len(),
            2,
            "one failed launch, one retry: {launches:?}"
        );
        assert!(launches[0].contains("--session-id"), "{launches:?}");
        assert!(
            launches[1].contains("--resume") && !launches[1].contains("--session-id"),
            "the retry must resume the session the failed launch created: {launches:?}"
        );
        let posted = serde_json::to_string(&events).unwrap();
        assert!(
            !posted.contains("authentication_failed") && !posted.contains("Not logged in"),
            "the failed attempt leaked into the timeline: {posted}"
        );
        assert!(posted.contains("fake answer"), "{posted}");
        assert_fake_groups_gone(&fake).await;
    }

    #[tokio::test]
    async fn a_persistent_auth_failure_is_surfaced_after_bounded_retries() {
        let fake = FakeClaude::new(1000);
        let (result, events) = run_fake_turn(&fake).await;

        assert_eq!(result["terminal_state"], "run_failed");
        assert_eq!(
            fake.launches().len(),
            1 + AUTH_PREFLIGHT_RETRY_DELAYS.len(),
            "retries must be bounded"
        );
        let terminal = events
            .iter()
            .find(|event| event["kind"] == "terminal_signal")
            .expect("terminal signal posted");
        assert_eq!(
            terminal["payload"]["terminal_reason"],
            "provider_auth_required"
        );
        // Giving up releases the withheld failure so the user sees why.
        let posted = serde_json::to_string(&events).unwrap();
        assert!(posted.contains("authentication_failed"), "{posted}");
        assert_fake_groups_gone(&fake).await;
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
    #[test]
    fn persist_local_phase_survives_locked_sqlite_db_without_stalling() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("agent/longhouse-shipper.db");

        let blocker_conn =
            crate::state::db::open_client_connection(&db_path, Duration::from_millis(500)).unwrap();
        blocker_conn.execute("BEGIN EXCLUSIVE", []).unwrap();

        let sink = ClaudePrintSink {
            run: ConsoleRun {
                provider: &CLAUDE_CONSOLE,
                session_id: "claude-lock-test".to_string(),
                thread_id: "thread-1".to_string(),
                turn_id: None,
                run_id: "run-1".to_string(),
                client_request_id: None,
                launch_id: "launch-1".to_string(),
                process_group_id: None,
                machine_name: "test-box".to_string(),
                local_db_path: Some(db_path),
                runtime_events_outbox_dir: temp.path().join("outbox"),
            },
            provider_thread_id: "p-thread-1".to_string(),
        };

        let started = std::time::Instant::now();
        sink.persist_local_phase("running", Some("Bash"), Utc::now());
        let elapsed = started.elapsed();

        assert!(
            elapsed < Duration::from_millis(1500),
            "persist_local_phase took {:?}, expected < 1500ms even under lock contention",
            elapsed
        );

        blocker_conn.execute("ROLLBACK", []).unwrap();
    }
    #[test]
    fn failed_terminal_handoff_keeps_status_until_durable_and_preserves_invocation() {
        let _home_guard = crate::console_adapter::longhouse_home_test_guard();
        let temp = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let session_id = Uuid::new_v4().to_string();
        let thread_id = Uuid::new_v4().to_string();
        let run_id = Uuid::new_v4().to_string();
        let successor_run_id = Uuid::new_v4().to_string();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            runtime.block_on(async {
                let agent_dir = crate::config::get_agent_dir().unwrap();
                let outbox = crate::config::get_agent_runtime_events_outbox_dir().unwrap();
                std::fs::create_dir_all(outbox.parent().unwrap()).unwrap();
                std::fs::write(&outbox, b"outbox path is a file").unwrap();
                let registry = crate::turn_claims::default_registry().unwrap();
                registry
                    .claim(&run_id, &session_id, &thread_id, None, None, "claude")
                    .unwrap();
                let sink = ClaudePrintSink {
                    run: ConsoleRun {
                        provider: &CLAUDE_CONSOLE,
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
                    provider_thread_id: Uuid::new_v4().to_string(),
                };

                sink.post_phase("thinking", None).await;
                sink.post_terminal_with_lifecycle(
                    "run_completed",
                    Some(0),
                    None,
                    Some("parked"),
                    Some(2),
                    None,
                )
                .await;
                let status_dir = crate::status_slot::status_slot_dir(&agent_dir);
                assert!(crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id && slot.run_id == run_id));
                let claim = registry.read(&run_id).unwrap();
                assert_eq!(claim.state, "terminal");
                assert_eq!(claim.invocation_state.as_deref(), Some("parked"));
                assert_eq!(claim.pending_count, 2);
                let retained_event = claim.terminal_event.clone().expect("retained terminal");
                assert_eq!(retained_event["run_id"], run_id);
                assert_eq!(retained_event["payload"]["invocation"]["pending_count"], 2);
                assert!(!claim.terminal_event_handed_off);
                let reopened =
                    crate::turn_claims::TurnClaimRegistry::new(agent_dir.join("turn-claims"));
                assert_eq!(
                    reopened.pending_terminal_event(&run_id).unwrap(),
                    Some(retained_event.clone())
                );
                assert!(crate::outbox::collect_runtime_event_outbox(&outbox).is_empty());

                std::fs::remove_file(&outbox).unwrap();
                assert!(
                    crate::outbox::retry_retained_terminal_event(&reopened, &outbox, &run_id)
                        .unwrap()
                );
                let recovered = reopened.read(&run_id).unwrap();
                assert_eq!(recovered.terminal_event, Some(retained_event.clone()));
                assert!(recovered.terminal_event_handed_off);
                sink.post_terminal_with_lifecycle(
                    "run_completed",
                    Some(0),
                    None,
                    Some("parked"),
                    Some(2),
                    None,
                )
                .await;
                assert_eq!(
                    crate::outbox::collect_runtime_event_outbox(&outbox).len(),
                    1
                );
                assert!(!crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id));

                crate::status_slot::publish_console_phase(
                    "claude",
                    CLAUDE_PRINT_ADAPTER,
                    &session_id,
                    &successor_run_id,
                    &Utc::now().to_rfc3339(),
                    "thinking",
                    None,
                    json!({}),
                );
                sink.post_terminal_with_lifecycle(
                    "run_completed",
                    Some(0),
                    None,
                    Some("parked"),
                    Some(2),
                    None,
                )
                .await;
                assert!(crate::status_slot::read_all(&status_dir)
                    .iter()
                    .any(|slot| slot.session_id == session_id && slot.run_id == successor_run_id));
            });
        });
    }
}
