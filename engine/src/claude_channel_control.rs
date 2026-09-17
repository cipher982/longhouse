use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crate::process_identity::{
    collect_process_facts_by_pid, command_contains_basename, parse_rfc3339,
    started_before_or_near_recorded, ProcessFact,
};
use chrono::DateTime;
use chrono::Utc;
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::json;
use thiserror::Error;
use tokio::time::sleep;
use uuid::Uuid;

const DEFAULT_READY_WAIT: Duration = Duration::from_secs(10);
const DEFAULT_POLL_INTERVAL: Duration = Duration::from_millis(100);
const DEFAULT_HTTP_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Error)]
pub enum ClaudeChannelControlError {
    #[error("Claude channel is not attached for session {session_id}: {message}")]
    SessionNotAttached { session_id: String, message: String },
    #[error("Claude channel command failed: {0}")]
    CommandFailed(String),
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelSendConfig {
    pub session_id: String,
    pub text: String,
    pub meta: Vec<(String, String)>,
    pub state_root: Option<PathBuf>,
    pub wait_timeout: Option<Duration>,
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelInterruptConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub wait_timeout: Option<Duration>,
}

#[derive(Clone, Debug)]
pub struct ClaudeChannelInspectConfig {
    pub session_id: String,
    pub state_root: Option<PathBuf>,
    pub wait_timeout: Option<Duration>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeChannelSendSummary {
    pub provider_session_id: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeChannelInterruptSummary {
    pub pid: i32,
    pub tool_process_group: Option<i32>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ClaudeChannelTerminateSummary {
    pub pid: i32,
    pub forced: bool,
}

const TERMINATE_GRACE: Duration = Duration::from_secs(10);

#[derive(Deserialize)]
struct ClaudeChannelState {
    provider_session_id: Option<String>,
    auth_token: Option<String>,
    port: Option<u16>,
    claude_pid: Option<i32>,
    ready: Option<bool>,
    started_at: Option<String>,
}

pub async fn send_text(
    config: ClaudeChannelSendConfig,
) -> Result<ClaudeChannelSendSummary, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let state = wait_for_ready_state(
        &config.session_id,
        config.state_root.as_deref(),
        wait_timeout,
        DEFAULT_POLL_INTERVAL,
    )
    .await?;
    let port = state
        .port
        .ok_or_else(|| not_attached(&config.session_id, "state is missing port"))?;
    if port == 0 {
        return Err(not_attached(&config.session_id, "state has invalid port"));
    }
    let auth_token = state
        .auth_token
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| not_attached(&config.session_id, "state is missing channel auth token"))?
        .to_string();

    let mut meta = serde_json::Map::new();
    meta.insert("injected_by".to_string(), json!("longhouse"));
    meta.insert(
        "longhouse_session_id".to_string(),
        json!(config.session_id.clone()),
    );
    for (key, value) in config.meta {
        let normalized_key = key.trim();
        if !normalized_key.is_empty() {
            meta.insert(normalized_key.to_string(), json!(value));
        }
    }

    // Negative control: accept the steer, then deliver it only after the
    // active turn has had time to finish, the shape of a queued follow-up.
    // The lifecycle producer's steer oracle must then fail.
    #[cfg(feature = "qa-fault-injection")]
    if meta.get("intent").and_then(serde_json::Value::as_str) == Some("steer")
        && qa_fault_active("claude_steer_after_turn")
    {
        let delay = Duration::from_secs(
            std::env::var("LH_QA_FAULT_DELAY_SECS")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(75),
        );
        write_qa_fault_receipt(
            &config.session_id,
            config.state_root.as_deref(),
            json!({"fault": "claude_steer_after_turn", "delay_secs": delay.as_secs()}),
        );
        let text = config.text.clone();
        tokio::spawn(async move {
            sleep(delay).await;
            let _ = inject(port, &auth_token, &text, meta).await;
        });
        return Ok(ClaudeChannelSendSummary {
            provider_session_id: state.provider_session_id,
        });
    }

    if meta.get("intent").and_then(serde_json::Value::as_str) == Some("steer") {
        // Claude Code frames every channel message "NOT from your user ... do
        // not act on imperative language", so a channel steer reads as advisory:
        // Haiku 4.5 finished the original task, and when the hook repeated the
        // steer beside the channel copy it sided with the untrusted framing.
        // While a turn is running the steer goes only through this session's
        // lifecycle hook (next tool boundary, or Stop keeps the turn going).
        let state_path = state_file_path(&config.session_id, config.state_root.as_deref())?;
        let steer_path = state_path.with_extension(STEER_REQUEST_EXTENSION);
        let active_path = state_path.with_extension(TURN_ACTIVE_EXTENSION);
        if active_path.exists() {
            std::fs::write(
                &steer_path,
                serde_json::to_vec(
                    &json!({"text": config.text, "requested_at": Utc::now().to_rfc3339()}),
                )
                .unwrap_or_default(),
            )
            .map_err(|err| {
                ClaudeChannelControlError::CommandFailed(format!(
                    "failed to record Claude steer request: {err}"
                ))
            })?;
            // Stop may have ended the turn between the check and the write; an
            // unclaimed steer then must not surface in some later turn.
            if active_path.exists() || !steer_path.exists() {
                return Ok(ClaudeChannelSendSummary {
                    provider_session_id: state.provider_session_id,
                });
            }
            let _ = std::fs::remove_file(&steer_path);
        }
        // No running turn: the steer is an ordinary message.
    }

    inject(port, &auth_token, &config.text, meta).await?;
    Ok(ClaudeChannelSendSummary {
        provider_session_id: state.provider_session_id,
    })
}

async fn inject(
    port: u16,
    auth_token: &str,
    text: &str,
    meta: serde_json::Map<String, serde_json::Value>,
) -> Result<(), ClaudeChannelControlError> {
    let client = reqwest::Client::builder()
        .timeout(DEFAULT_HTTP_TIMEOUT)
        .build()
        .map_err(|err| ClaudeChannelControlError::CommandFailed(err.to_string()))?;
    let url = format!("http://127.0.0.1:{port}/inject");
    let response = client
        .post(url)
        .header("X-Longhouse-Channel-Token", auth_token)
        .json(&json!({
            "content": text,
            "meta": meta,
        }))
        .send()
        .await
        .map_err(|err| {
            ClaudeChannelControlError::CommandFailed(format!(
                "bridge injection request failed: {err}"
            ))
        })?;
    if response.status() != StatusCode::NO_CONTENT {
        return Err(ClaudeChannelControlError::CommandFailed(format!(
            "bridge injection returned HTTP {}",
            response.status().as_u16()
        )));
    }
    Ok(())
}

/// Stop Claude's active turn without ending the session.
///
/// Claude Code treats SIGINT as a shutdown signal, so signaling the process
/// ends the whole session. A turn stop instead goes through Claude's own hook
/// contract: this records an interrupt request that the lifecycle hook turns
/// into `{"continue": false}` at the next tool boundary, and it terminates the
/// foreground Bash tool that is running now, so that boundary arrives at once.
/// A turn that is only generating text stops at its next tool call or ends on
/// its own.
pub async fn interrupt(
    config: ClaudeChannelInterruptConfig,
) -> Result<ClaudeChannelInterruptSummary, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let state = wait_for_ready_state(
        &config.session_id,
        config.state_root.as_deref(),
        wait_timeout,
        DEFAULT_POLL_INTERVAL,
    )
    .await?;
    let pid = state
        .claude_pid
        .filter(|pid| *pid > 0)
        .ok_or_else(|| not_attached(&config.session_id, "state is missing claude_pid"))?;
    verify_claude_interrupt_target(
        &config.session_id,
        pid,
        state.started_at.as_deref().and_then(parse_rfc3339),
    )?;
    // Negative control: report a delivered interrupt without stopping anything.
    #[cfg(feature = "qa-fault-injection")]
    if qa_fault_active("claude_interrupt_noop") {
        write_qa_fault_receipt(
            &config.session_id,
            config.state_root.as_deref(),
            json!({"fault": "claude_interrupt_noop", "pid": pid}),
        );
        return Ok(ClaudeChannelInterruptSummary {
            pid,
            tool_process_group: None,
        });
    }
    let state_path = state_file_path(&config.session_id, config.state_root.as_deref())?;
    std::fs::write(
        state_path.with_extension(INTERRUPT_REQUEST_EXTENSION),
        serde_json::to_vec(&json!({"requested_at": Utc::now().to_rfc3339()})).unwrap_or_default(),
    )
    .map_err(|err| {
        ClaudeChannelControlError::CommandFailed(format!(
            "failed to record Claude interrupt request: {err}"
        ))
    })?;
    let tool_process_group = std::fs::read(state_path.with_extension(FOREGROUND_TOOL_EXTENSION))
        .ok()
        .and_then(|raw| serde_json::from_slice::<serde_json::Value>(&raw).ok())
        .and_then(|tool| {
            tool.get("command")
                .and_then(serde_json::Value::as_str)
                .map(str::to_owned)
        })
        .and_then(|command| terminate_foreground_tool(pid, &command));
    Ok(ClaudeChannelInterruptSummary {
        pid,
        tool_process_group,
    })
}

/// Claude runs each Bash tool call in its own session whose shell `eval`s the
/// command. Match that shell among Claude's direct children by the exact
/// command the PreToolUse hook recorded, and terminate its process group.
#[cfg(unix)]
fn terminate_foreground_tool(claude_pid: i32, command: &str) -> Option<i32> {
    let wanted = shell_words_key(command);
    if wanted.is_empty() {
        return None;
    }
    let output = std::process::Command::new("ps")
        .args(["-A", "-o", "pid=,ppid=,pgid=,args="])
        .output()
        .ok()?;
    let listing = String::from_utf8_lossy(&output.stdout);
    let group = listing
        .lines()
        .find_map(|line| foreground_tool_group(line, claude_pid, &wanted))?;
    (unsafe { libc::kill(-group, libc::SIGTERM) } == 0).then_some(group)
}

/// Claude quotes the command inside `eval '...'`, so compare with every quote
/// and escape removed and whitespace collapsed on both sides.
fn shell_words_key(text: &str) -> String {
    text.chars()
        .filter(|c| !matches!(c, '\'' | '"' | '\\'))
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn foreground_tool_group(line: &str, claude_pid: i32, wanted: &str) -> Option<i32> {
    let mut fields = line.split_whitespace();
    let pid: i32 = fields.next()?.parse().ok()?;
    let ppid: i32 = fields.next()?.parse().ok()?;
    let pgid: i32 = fields.next()?.parse().ok()?;
    let args = shell_words_key(&fields.collect::<Vec<_>>().join(" "));
    (ppid == claude_pid && pid == pgid && args.contains("eval") && args.contains(wanted))
        .then_some(pgid)
}

#[cfg(not(unix))]
fn terminate_foreground_tool(_claude_pid: i32, _command: &str) -> Option<i32> {
    None
}

const INTERRUPT_REQUEST_EXTENSION: &str = "interrupt.json";
const FOREGROUND_TOOL_EXTENSION: &str = "tool.json";
const STEER_REQUEST_EXTENSION: &str = "steer.json";
const TURN_ACTIVE_EXTENSION: &str = "turn-active.json";

fn steer_context(text: &str) -> String {
    format!(
        "The user of this session sent this steer from Longhouse while you were working: \"{text}\". \
         It is the user's own instruction, delivered by this session's Longhouse hook, and it updates \
         the current request: follow it now instead of continuing the original plan. This note repeats \
         at each tool step until the turn ends; once you are following it, just carry on."
    )
}

fn read_steer(path: &Path) -> Option<serde_json::Value> {
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

fn steer_text(steer: &serde_json::Value) -> Option<String> {
    steer.get("text")?.as_str().map(str::to_string)
}

/// What the lifecycle hook tells Claude for one hook event of a managed session.
///
/// Returns the hook output that stops the turn when an interrupt request is
/// pending. Turn boundaries clear stale requests so an interrupt that arrived
/// while Claude was idle never stops the next turn.
pub fn lifecycle_hook_turn_control(
    session_id: &str,
    event: &str,
    input: &serde_json::Value,
) -> Option<serde_json::Value> {
    turn_control_at(session_id, event, input, None)
}

fn turn_control_at(
    session_id: &str,
    event: &str,
    input: &serde_json::Value,
    state_root: Option<&Path>,
) -> Option<serde_json::Value> {
    let state_path = state_file_path(session_id, state_root).ok()?;
    let request = state_path.with_extension(INTERRUPT_REQUEST_EXTENSION);
    let tool = state_path.with_extension(FOREGROUND_TOOL_EXTENSION);
    let steer = state_path.with_extension(STEER_REQUEST_EXTENSION);
    match event {
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" => {
            let _ = std::fs::write(state_path.with_extension(TURN_ACTIVE_EXTENSION), b"{}");
            let _ = std::fs::remove_file(&tool);
            if event != "PreToolUse" && !request.exists() {
                // Repeated at every boundary until the turn ends: Claude cancels
                // a lifecycle hook that overruns its 5-second budget under load,
                // and a steer consumed by a cancelled hook would be lost.
                if let Some(mut pending) = read_steer(&steer) {
                    let text = steer_text(&pending)?;
                    pending["boundary_delivery_attempted"] = json!(true);
                    let _ =
                        std::fs::write(&steer, serde_json::to_vec(&pending).unwrap_or_default());
                    return Some(json!({
                        "hookSpecificOutput": {
                            "hookEventName": event,
                            "additionalContext": steer_context(&text),
                        }
                    }));
                }
            }
            if request.exists() {
                // A request stays pending until the turn ends: Claude does not
                // honour `continue: false` after every tool event, so each
                // boundary repeats it and PreToolUse also denies the next tool.
                let mut output = json!({
                    "continue": false,
                    "stopReason": "Interrupted from Longhouse",
                });
                if event == "PreToolUse" {
                    output["hookSpecificOutput"] = json!({
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "Interrupted from Longhouse",
                    });
                }
                return Some(output);
            }
            let foreground_bash = event == "PreToolUse"
                && input.get("tool_name").and_then(serde_json::Value::as_str) == Some("Bash")
                && input
                    .pointer("/tool_input/run_in_background")
                    .and_then(serde_json::Value::as_bool)
                    != Some(true);
            if foreground_bash {
                if let Some(command) = input
                    .pointer("/tool_input/command")
                    .and_then(serde_json::Value::as_str)
                {
                    let _ = std::fs::write(
                        &tool,
                        serde_json::to_vec(&json!({"command": command})).unwrap_or_default(),
                    );
                }
            }
            None
        }
        "Stop"
            if !request.exists()
                && read_steer(&steer).is_some_and(|pending| {
                    pending.get("boundary_delivery_attempted") != Some(&json!(true))
                }) =>
        {
            let _ = std::fs::remove_file(&tool);
            // The steer reached no tool boundary: keep the turn going with it.
            // The turn stays active, and the marker keeps Stop from blocking twice.
            let mut pending = read_steer(&steer)?;
            let text = steer_text(&pending)?;
            pending["boundary_delivery_attempted"] = json!(true);
            let _ = std::fs::write(&steer, serde_json::to_vec(&pending).unwrap_or_default());
            Some(json!({"decision": "block", "reason": steer_context(&text)}))
        }
        "SessionStart" | "UserPromptSubmit" | "Stop" => {
            let _ = std::fs::remove_file(&request);
            let _ = std::fs::remove_file(&tool);
            let _ = std::fs::remove_file(&steer);
            let active = state_path.with_extension(TURN_ACTIVE_EXTENSION);
            if event == "UserPromptSubmit" {
                let _ = std::fs::write(active, b"{}");
            } else {
                let _ = std::fs::remove_file(active);
            }
            None
        }
        _ => None,
    }
}

/// Stop the recorded Claude process: SIGTERM, then SIGKILL after a grace period.
///
/// Only the recorded Claude pid is signaled, for the same reason as interrupt:
/// the channel bridge can share its process group. Claude's own exit tears
/// down the bridge and the `longhouse claude` launcher.
pub async fn terminate(
    config: ClaudeChannelInterruptConfig,
) -> Result<ClaudeChannelTerminateSummary, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let state = wait_for_ready_state(
        &config.session_id,
        config.state_root.as_deref(),
        wait_timeout,
        DEFAULT_POLL_INTERVAL,
    )
    .await?;
    let pid = state
        .claude_pid
        .filter(|pid| *pid > 0)
        .ok_or_else(|| not_attached(&config.session_id, "state is missing claude_pid"))?;
    verify_claude_interrupt_target(
        &config.session_id,
        pid,
        state.started_at.as_deref().and_then(parse_rfc3339),
    )?;
    let forced = stop_process(pid, TERMINATE_GRACE).await.map_err(|err| {
        ClaudeChannelControlError::CommandFailed(format!(
            "failed to terminate Claude process {pid}: {err}"
        ))
    })?;
    Ok(ClaudeChannelTerminateSummary { pid, forced })
}

#[cfg(unix)]
async fn stop_process(pid: i32, grace: Duration) -> std::io::Result<bool> {
    if unsafe { libc::kill(pid, libc::SIGTERM) } != 0 {
        let error = std::io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            return Ok(false);
        }
        return Err(error);
    }
    let deadline = Instant::now() + grace;
    while Instant::now() < deadline {
        if !process_alive(pid) {
            return Ok(false);
        }
        sleep(DEFAULT_POLL_INTERVAL).await;
    }
    unsafe {
        libc::kill(pid, libc::SIGKILL);
    }
    Ok(true)
}

#[cfg(not(unix))]
async fn stop_process(_pid: i32, _grace: Duration) -> std::io::Result<bool> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "Claude terminate is unsupported on this platform",
    ))
}

/// Alive and not a zombie awaiting its parent's reap.
#[cfg(unix)]
fn process_alive(pid: i32) -> bool {
    if unsafe { libc::kill(pid, 0) } != 0 {
        return false;
    }
    let Ok(pid) = u32::try_from(pid) else {
        return false;
    };
    collect_process_facts_by_pid()
        .get(&pid)
        .is_some_and(|fact| !fact.stat.starts_with('Z'))
}

#[cfg(feature = "qa-fault-injection")]
fn qa_fault_active(fault: &str) -> bool {
    std::env::var("LH_QA_FAULT").as_deref() == Ok(fault)
}

#[cfg(feature = "qa-fault-injection")]
fn write_qa_fault_receipt(session_id: &str, state_root: Option<&Path>, receipt: serde_json::Value) {
    if let Ok(path) = state_file_path(session_id, state_root) {
        let _ = std::fs::write(
            path.with_extension("qa-fault.json"),
            serde_json::to_vec(&receipt).unwrap_or_default(),
        );
    }
}

pub async fn inspect_state(
    config: ClaudeChannelInspectConfig,
) -> Result<serde_json::Value, ClaudeChannelControlError> {
    let wait_timeout = config.wait_timeout.unwrap_or(DEFAULT_READY_WAIT);
    let path = state_file_path(&config.session_id, config.state_root.as_deref())?;
    let deadline = Instant::now() + wait_timeout;
    loop {
        match read_state_value(&path) {
            Ok(mut value) => {
                if let Some(object) = value.as_object_mut() {
                    if object.contains_key("auth_token") {
                        object.insert("auth_token".to_string(), json!("<redacted>"));
                    }
                }
                return Ok(value);
            }
            Err(StateReadError::Missing) => {}
            Err(StateReadError::Invalid(message)) => {
                return Err(not_attached(&config.session_id, &message));
            }
        }
        if Instant::now() >= deadline {
            return Err(not_attached(
                &config.session_id,
                &format!("state did not appear at {}", path.display()),
            ));
        }
        sleep(DEFAULT_POLL_INTERVAL).await;
    }
}

async fn wait_for_ready_state(
    session_id: &str,
    state_root: Option<&Path>,
    timeout: Duration,
    poll_interval: Duration,
) -> Result<ClaudeChannelState, ClaudeChannelControlError> {
    let path = state_file_path(session_id, state_root)?;
    let deadline = Instant::now() + timeout;
    let mut last_not_ready = false;
    loop {
        match read_state_file(&path) {
            Ok(state) => {
                if state.ready.unwrap_or(false) {
                    return Ok(state);
                }
                last_not_ready = true;
            }
            Err(StateReadError::Missing) => {}
            Err(StateReadError::Invalid(message)) => {
                return Err(not_attached(session_id, &message));
            }
        }
        if Instant::now() >= deadline {
            let message = if last_not_ready {
                format!("state at {} did not become ready", path.display())
            } else {
                format!("state did not appear at {}", path.display())
            };
            return Err(not_attached(session_id, &message));
        }
        sleep(poll_interval).await;
    }
}

fn state_file_path(
    session_id: &str,
    state_root: Option<&Path>,
) -> Result<PathBuf, ClaudeChannelControlError> {
    let normalized = Uuid::parse_str(session_id)
        .map_err(|_| not_attached(session_id, "session id is not a UUID"))?;
    let root = match state_root {
        Some(path) => path.to_path_buf(),
        None => default_state_root().map_err(|message| not_attached(session_id, &message))?,
    };
    Ok(root.join("sessions").join(format!("{normalized}.json")))
}

fn default_state_root() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").ok_or_else(|| "HOME is not set".to_string())?;
    Ok(PathBuf::from(home).join(".claude/channels/longhouse"))
}

enum StateReadError {
    Missing,
    Invalid(String),
}

fn read_state_file(path: &Path) -> Result<ClaudeChannelState, StateReadError> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(StateReadError::Missing);
        }
        Err(err) => return Err(StateReadError::Invalid(err.to_string())),
    };
    serde_json::from_str(&raw)
        .map_err(|err| StateReadError::Invalid(format!("state is invalid JSON: {err}")))
}

fn read_state_value(path: &Path) -> Result<serde_json::Value, StateReadError> {
    let raw = match std::fs::read_to_string(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(StateReadError::Missing);
        }
        Err(err) => return Err(StateReadError::Invalid(err.to_string())),
    };
    serde_json::from_str(&raw)
        .map_err(|err| StateReadError::Invalid(format!("state is invalid JSON: {err}")))
}

fn verify_claude_interrupt_target(
    session_id: &str,
    pid: i32,
    recorded_start: Option<DateTime<Utc>>,
) -> Result<(), ClaudeChannelControlError> {
    let pid_u32 = u32::try_from(pid).map_err(|_| {
        not_attached(
            session_id,
            &format!("state has invalid claude_pid {pid} for interrupt"),
        )
    })?;
    let process_facts = collect_process_facts_by_pid();
    let Some(fact) = process_facts.get(&pid_u32) else {
        return Err(not_attached(
            session_id,
            &format!("recorded Claude process {pid} is not running"),
        ));
    };
    if !claude_interrupt_target_matches(fact, recorded_start) {
        return Err(not_attached(
            session_id,
            &format!("recorded Claude process {pid} no longer matches the channel state"),
        ));
    }
    Ok(())
}

fn claude_interrupt_target_matches(
    fact: &ProcessFact,
    recorded_start: Option<DateTime<Utc>>,
) -> bool {
    command_contains_basename(&fact.command, "claude")
        && started_before_or_near_recorded(fact, recorded_start)
}

fn not_attached(session_id: &str, message: &str) -> ClaudeChannelControlError {
    ClaudeChannelControlError::SessionNotAttached {
        session_id: session_id.to_string(),
        message: message.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::oneshot;

    const SESSION_ID: &str = "11111111-1111-4111-8111-111111111111";

    struct RecordedRequest {
        headers: String,
        body: Value,
    }

    async fn spawn_inject_server(
        status: &'static str,
    ) -> (u16, oneshot::Receiver<RecordedRequest>) {
        let listener = TcpListener::bind(("127.0.0.1", 0)).await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let (tx, rx) = oneshot::channel();
        tokio::spawn(async move {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut raw = Vec::new();
            let mut buffer = [0_u8; 4096];
            loop {
                let n = stream.read(&mut buffer).await.unwrap();
                if n == 0 {
                    break;
                }
                raw.extend_from_slice(&buffer[..n]);
                if let Some(header_end) = find_header_end(&raw) {
                    let headers = String::from_utf8_lossy(&raw[..header_end]).to_string();
                    let content_length = headers
                        .lines()
                        .find_map(|line| {
                            line.strip_prefix("content-length:")
                                .or_else(|| line.strip_prefix("Content-Length:"))
                        })
                        .and_then(|value| value.trim().parse::<usize>().ok())
                        .unwrap_or(0);
                    let body_start = header_end + 4;
                    if raw.len() >= body_start + content_length {
                        let body =
                            serde_json::from_slice(&raw[body_start..body_start + content_length])
                                .unwrap();
                        let _ = tx.send(RecordedRequest { headers, body });
                        let response = format!(
                            "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                        );
                        stream.write_all(response.as_bytes()).await.unwrap();
                        break;
                    }
                }
            }
        });
        (port, rx)
    }

    fn find_header_end(raw: &[u8]) -> Option<usize> {
        raw.windows(4).position(|window| window == b"\r\n\r\n")
    }

    fn write_state(root: &Path, session_id: &str, payload: Value) {
        let path = state_file_path(session_id, Some(root)).unwrap();
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, serde_json::to_vec(&payload).unwrap()).unwrap();
    }

    fn process_fact(command: &str, started_at: Option<&str>) -> ProcessFact {
        ProcessFact {
            pid: 101,
            tty: "??".to_string(),
            stat: "Ss".to_string(),
            lstart: "".to_string(),
            command: command.to_string(),
            start_time: started_at.and_then(parse_rfc3339),
        }
    }

    #[tokio::test]
    async fn send_text_injects_payload_and_default_meta() {
        let temp = tempfile::tempdir().unwrap();
        let (port, rx) = spawn_inject_server("204 No Content").await;
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "session_id": SESSION_ID,
                "provider_session_id": "claude-provider-1",
                "auth_token": "secret-token",
                "port": port,
                "ready": true,
            }),
        );

        let summary = send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "hello".to_string(),
            meta: vec![],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap();

        assert_eq!(
            summary.provider_session_id.as_deref(),
            Some("claude-provider-1")
        );
        let request = rx.await.unwrap();
        assert!(request
            .headers
            .to_ascii_lowercase()
            .contains("x-longhouse-channel-token: secret-token"));
        assert_eq!(request.body["content"], "hello");
        assert_eq!(request.body["meta"]["injected_by"], "longhouse");
        assert_eq!(request.body["meta"]["longhouse_session_id"], SESSION_ID);
    }

    #[tokio::test]
    async fn send_text_preserves_control_meta() {
        let temp = tempfile::tempdir().unwrap();
        let (port, rx) = spawn_inject_server("204 No Content").await;
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "auth_token": "secret-token",
                "port": port,
                "ready": true,
            }),
        );

        send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "course correct".to_string(),
            meta: vec![("intent".to_string(), "steer".to_string())],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap();

        let request = rx.await.unwrap();
        assert_eq!(request.body["content"], "course correct");
        assert_eq!(request.body["meta"]["intent"], "steer");
    }

    #[tokio::test]
    async fn bridge_failure_does_not_leak_auth_token() {
        let temp = tempfile::tempdir().unwrap();
        let (port, _rx) = spawn_inject_server("403 Forbidden").await;
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "auth_token": "very-secret-token",
                "port": port,
                "ready": true,
            }),
        );

        let err = send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "hello".to_string(),
            meta: vec![],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap_err();

        let message = err.to_string();
        assert!(message.contains("HTTP 403"));
        assert!(!message.contains("very-secret-token"));
    }

    #[tokio::test]
    async fn missing_state_is_session_not_attached() {
        let temp = tempfile::tempdir().unwrap();

        let err = send_text(ClaudeChannelSendConfig {
            session_id: SESSION_ID.to_string(),
            text: "hello".to_string(),
            meta: vec![],
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: Some(Duration::from_millis(10)),
        })
        .await
        .unwrap_err();

        assert!(matches!(
            err,
            ClaudeChannelControlError::SessionNotAttached { .. }
        ));
    }

    #[tokio::test]
    async fn inspect_state_redacts_auth_token() {
        let temp = tempfile::tempdir().unwrap();
        write_state(
            temp.path(),
            SESSION_ID,
            json!({
                "session_id": SESSION_ID,
                "provider_session_id": "claude-provider-1",
                "auth_token": "very-secret-token",
                "port": 4242,
                "ready": true,
            }),
        );

        let state = inspect_state(ClaudeChannelInspectConfig {
            session_id: SESSION_ID.to_string(),
            state_root: Some(temp.path().to_path_buf()),
            wait_timeout: None,
        })
        .await
        .unwrap();

        assert_eq!(state["session_id"], SESSION_ID);
        assert_eq!(state["auth_token"], "<redacted>");
        assert!(!serde_json::to_string(&state)
            .unwrap()
            .contains("very-secret-token"));
    }

    #[test]
    fn claude_interrupt_target_requires_claude_command() {
        let fact = process_fact(
            "/System/Library/PrivateFrameworks/CascadeSets.framework/SetStoreUpdateService",
            None,
        );

        assert!(!claude_interrupt_target_matches(&fact, None));
    }

    #[test]
    fn claude_interrupt_target_rejects_reused_claude_pid_by_start_time() {
        let fact = process_fact(
            "/Users/test/.local/bin/claude --resume 11111111-1111-4111-8111-111111111111",
            Some("2026-05-28T20:40:28Z"),
        );
        let recorded_start = parse_rfc3339("2026-04-07T19:38:09Z");

        assert!(!claude_interrupt_target_matches(&fact, recorded_start));
    }

    #[test]
    fn claude_interrupt_target_accepts_matching_recorded_claude_process() {
        let fact = process_fact(
            "/Users/test/.local/bin/claude --resume 11111111-1111-4111-8111-111111111111",
            Some("2026-04-07T19:38:08Z"),
        );
        let recorded_start = parse_rfc3339("2026-04-07T19:38:09Z");

        assert!(claude_interrupt_target_matches(&fact, recorded_start));
    }

    #[test]
    fn pending_interrupt_stops_every_tool_boundary_until_the_turn_ends() {
        let temp = tempfile::tempdir().unwrap();
        let state = state_file_path(SESSION_ID, Some(temp.path())).unwrap();
        std::fs::create_dir_all(state.parent().unwrap()).unwrap();
        let bash = json!({"tool_name": "Bash", "tool_input": {"command": "sleep 30"}});

        assert_eq!(
            turn_control_at(SESSION_ID, "PreToolUse", &bash, Some(temp.path())),
            None
        );
        let tool = state.with_extension(FOREGROUND_TOOL_EXTENSION);
        assert!(tool.exists());

        std::fs::write(state.with_extension(INTERRUPT_REQUEST_EXTENSION), b"{}").unwrap();
        let stop =
            turn_control_at(SESSION_ID, "PostToolUseFailure", &bash, Some(temp.path())).unwrap();
        assert_eq!(stop["continue"], false);
        assert!(!tool.exists());
        let deny = turn_control_at(SESSION_ID, "PreToolUse", &bash, Some(temp.path())).unwrap();
        assert_eq!(deny["hookSpecificOutput"]["permissionDecision"], "deny");
        assert_eq!(
            turn_control_at(SESSION_ID, "Stop", &json!({}), Some(temp.path())),
            None
        );
        assert_eq!(
            turn_control_at(SESSION_ID, "PreToolUse", &bash, Some(temp.path())),
            None
        );
    }

    #[test]
    fn steer_repeats_at_tool_boundaries_and_blocks_stop_only_without_one() {
        let temp = tempfile::tempdir().unwrap();
        let state = state_file_path(SESSION_ID, Some(temp.path())).unwrap();
        std::fs::create_dir_all(state.parent().unwrap()).unwrap();
        let bash = json!({"tool_name": "Bash", "tool_input": {"command": "sleep 5"}});
        let steer = state.with_extension(STEER_REQUEST_EXTENSION);
        let write_steer = || std::fs::write(&steer, br#"{"text":"stop now"}"#).unwrap();

        // Delivered at every PostToolUse (a cancelled hook must not lose it),
        // never at PreToolUse, and Stop then ends the turn normally.
        write_steer();
        assert_eq!(
            turn_control_at(SESSION_ID, "PreToolUse", &bash, Some(temp.path())),
            None
        );
        for _ in 0..2 {
            let output =
                turn_control_at(SESSION_ID, "PostToolUse", &bash, Some(temp.path())).unwrap();
            let context = output["hookSpecificOutput"]["additionalContext"]
                .as_str()
                .unwrap();
            assert!(context.contains("stop now"));
            assert_eq!(output["hookSpecificOutput"]["hookEventName"], "PostToolUse");
        }
        assert_eq!(
            turn_control_at(SESSION_ID, "Stop", &json!({}), Some(temp.path())),
            None
        );
        assert!(!steer.exists());
        assert!(!state.with_extension(TURN_ACTIVE_EXTENSION).exists());

        // No tool boundary: Stop blocks once with the steer, then lets the turn end.
        write_steer();
        let block = turn_control_at(SESSION_ID, "Stop", &json!({}), Some(temp.path())).unwrap();
        assert_eq!(block["decision"], "block");
        assert!(block["reason"].as_str().unwrap().contains("stop now"));
        assert_eq!(
            turn_control_at(SESSION_ID, "Stop", &json!({}), Some(temp.path())),
            None
        );
        assert!(!steer.exists());
    }

    #[test]
    fn steer_while_idle_is_cleared_by_the_next_prompt() {
        let temp = tempfile::tempdir().unwrap();
        let state = state_file_path(SESSION_ID, Some(temp.path())).unwrap();
        std::fs::create_dir_all(state.parent().unwrap()).unwrap();
        let steer = state.with_extension(STEER_REQUEST_EXTENSION);
        std::fs::write(&steer, br#"{"text":"stale"}"#).unwrap();

        assert_eq!(
            turn_control_at(
                SESSION_ID,
                "UserPromptSubmit",
                &json!({}),
                Some(temp.path())
            ),
            None
        );
        assert!(!steer.exists());
        assert!(state.with_extension(TURN_ACTIVE_EXTENSION).exists());
    }

    #[test]
    fn interrupt_requested_while_idle_never_stops_the_next_turn() {
        let temp = tempfile::tempdir().unwrap();
        let state = state_file_path(SESSION_ID, Some(temp.path())).unwrap();
        std::fs::create_dir_all(state.parent().unwrap()).unwrap();
        std::fs::write(state.with_extension(INTERRUPT_REQUEST_EXTENSION), b"{}").unwrap();

        assert_eq!(
            turn_control_at(
                SESSION_ID,
                "UserPromptSubmit",
                &json!({}),
                Some(temp.path())
            ),
            None
        );
        let background = json!({"tool_name": "Bash", "tool_input": {"command": "make dev", "run_in_background": true}});
        assert_eq!(
            turn_control_at(SESSION_ID, "PreToolUse", &background, Some(temp.path())),
            None
        );
        assert!(!state.with_extension(FOREGROUND_TOOL_EXTENSION).exists());
    }

    #[test]
    fn foreground_tool_matches_claude_eval_quoting() {
        let command = "python3 -c \"import select; select.select([], [], [], 45); print('lh_x')\"";
        let line = "  5690  5641  5690 /bin/bash -c source snap.sh && eval 'python3 -c \"import select; select.select([], [], [], 45); print('\\''lh_x'\\'')\"' < /dev/null && pwd -P";
        let wanted = shell_words_key(command);
        assert_eq!(foreground_tool_group(line, 5641, &wanted), Some(5690));
        assert_eq!(foreground_tool_group(line, 1, &wanted), None);
    }
}
