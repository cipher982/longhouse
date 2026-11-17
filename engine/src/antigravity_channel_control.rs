//! Antigravity Helm remote send: hook-inbox message writer.
//!
//! Longhouse used to shell out from the Machine Agent to a
//! `longhouse antigravity-channel send --session-id <id> --text <text>`
//! subcommand. That Python CLI command was retired in commit bf1cf146c
//! ("Retire Python provider control canaries") and the Rust `longhouse`
//! binary (`src/longhouse.rs`) never grew a replacement, so remote Helm send
//! for Antigravity always failed with "unrecognized subcommand". See docket
//! `no-date--longhouse-antigravity-helm-send-missing-subcommand.md`.
//!
//! The fix is to stop subprocessing entirely and write the hook-inbox
//! message file directly from the engine, in the exact format, location, and
//! atomic-write style that `server/zerg/provider_contract/antigravity_hook_inbox.py`'s
//! `enqueue_antigravity_message` writes and the shipped Antigravity hook
//! script's `claim_inbox_messages` reads and claims:
//!
//! - path: `<longhouse_home>/managed-local/antigravity/inbox/<session_id>/msg-<id>.json`
//! - payload: `{id, session_id, text, intent, created_at, expires_at}`
//! - claim receipt (written by the hook, read here):
//!   `.../inbox/<session_id>/claimed/claimed-msg-<id>.json`, carrying a
//!   `claimed_at` field once the hook has claimed the message.
//!
//! `send_text` mirrors the retired CLI's `send` command: enqueue, then poll
//! for the hook's claim receipt for up to `wait_claimed_secs` before giving
//! up and deleting the unclaimed message.

use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use chrono::{Duration as ChronoDuration, SecondsFormat, Utc};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::config::get_longhouse_home;

pub const ANTIGRAVITY_HOOK_INBOX_TRANSPORT: &str = "antigravity_hook_inbox";

/// Matches `_MESSAGE_TTL` in `antigravity_hook_inbox.py`.
const MESSAGE_TTL_SECS: i64 = 5 * 60;
/// Matches `_MAX_MESSAGE_BYTES` in `antigravity_hook_inbox.py`.
const MAX_MESSAGE_BYTES: usize = 64 * 1024;
/// Matches the retired CLI's `--wait-claimed-secs` default for legacy callers.
const DEFAULT_WAIT_CLAIMED_SECS: f64 = 15.0;
/// Default wait budget for mid-turn steers (bounded by the server's shielded task).
const DEFAULT_STEER_WAIT_CLAIMED_SECS: f64 = 30.0;
/// Grace window after losing a withdraw rename race for the claimer to finish
/// writing `claimed_at` into `claimed/claimed-msg-<id>.json`.
const CLAIM_FINALIZE_GRACE_SECS: f64 = 0.5;
/// Matches the hook script's claim-poll interval.
const CLAIM_POLL_INTERVAL: Duration = Duration::from_millis(50);

#[derive(Debug)]
pub enum AntigravityControlError {
    /// The message was atomically withdrawn before any hook or launcher claimed
    /// it, so nothing reached the provider and the durable queue may retry.
    InputNotClaimed(String),
    /// A claimer moved the inbox file before withdraw, so the message may have
    /// been injected even though no finalized claim receipt appeared in time.
    DeliveryUnknown(String),
    /// Definite failure or non-retryable control error.
    CommandFailed(anyhow::Error),
}

impl AntigravityControlError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::InputNotClaimed(_) => "input_not_claimed",
            Self::DeliveryUnknown(_) => "delivery_unknown",
            Self::CommandFailed(_) => "command_failed",
        }
    }

    pub fn message(&self) -> String {
        match self {
            Self::InputNotClaimed(message) | Self::DeliveryUnknown(message) => message.clone(),
            Self::CommandFailed(err) => err.to_string(),
        }
    }
}

impl std::fmt::Display for AntigravityControlError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message())
    }
}

impl std::error::Error for AntigravityControlError {}

impl From<anyhow::Error> for AntigravityControlError {
    fn from(error: anyhow::Error) -> Self {
        Self::CommandFailed(error)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AntigravitySendOutcome {
    pub message_id: String,
    pub claimed_at: String,
}

struct EnqueuedMessage {
    message_id: String,
    inbox_dir: PathBuf,
    message_path: PathBuf,
    claim_path: PathBuf,
}

/// `~/.longhouse/managed-local/antigravity`
/// (`get_managed_local_dir("antigravity")` in `longhouse_paths.py`).
fn antigravity_managed_local_dir() -> Result<PathBuf> {
    Ok(get_longhouse_home()?
        .join("managed-local")
        .join("antigravity"))
}

/// `antigravity_inbox_dir` in `antigravity_hook_inbox.py`.
fn antigravity_inbox_dir(session_id: &str) -> Result<PathBuf> {
    Ok(antigravity_managed_local_dir()?
        .join("inbox")
        .join(session_id))
}

fn ensure_private_dir(path: &Path) -> Result<()> {
    std::fs::create_dir_all(path).with_context(|| format!("create {}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700));
    }
    Ok(())
}

/// Atomic write matching `_write_private_json` in `antigravity_hook_inbox.py`:
/// a temp file in the same directory, chmod 0600, then rename over the target.
fn write_private_json(path: &Path, payload: &Value) -> Result<()> {
    let parent = path.parent().context("message path has no parent")?;
    ensure_private_dir(parent)?;
    let mut contents = serde_json::to_vec_pretty(payload)?;
    contents.push(b'\n');
    let tmp_path = parent.join(format!(".tmp.{}", Uuid::new_v4().simple()));
    {
        use std::io::Write as _;
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp_path)
            .with_context(|| format!("create {}", tmp_path.display()))?;
        file.write_all(&contents)?;
        file.sync_all()?;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp_path, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(&tmp_path, path)
        .with_context(|| format!("rename {} -> {}", tmp_path.display(), path.display()))?;
    Ok(())
}

fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Micros, true)
}

fn read_claimed_at(claim_path: &Path) -> Option<String> {
    let bytes = std::fs::read(claim_path).ok()?;
    let value: Value = serde_json::from_slice(&bytes).ok()?;
    value
        .get("claimed_at")
        .and_then(Value::as_str)
        .map(str::to_string)
}

fn enqueue(session_id: &str, text: &str, intent: &str) -> Result<EnqueuedMessage> {
    let session_id = session_id.trim();
    if session_id.is_empty() {
        return Err(anyhow!("session_id is required"));
    }
    if text.trim().is_empty() {
        return Err(anyhow!("text is required"));
    }
    if text.len() > MAX_MESSAGE_BYTES {
        return Err(anyhow!("text exceeds {MAX_MESSAGE_BYTES} bytes"));
    }

    let message_id = Uuid::new_v4().simple().to_string();
    let created_at = now_iso();
    let expires_at = (Utc::now() + ChronoDuration::seconds(MESSAGE_TTL_SECS))
        .to_rfc3339_opts(SecondsFormat::Micros, true);
    let payload = json!({
        "id": message_id,
        "session_id": session_id,
        "text": text,
        "intent": intent,
        "created_at": created_at,
        "expires_at": expires_at,
    });

    let managed_local_dir = antigravity_managed_local_dir()?;
    ensure_private_dir(&managed_local_dir)?;
    ensure_private_dir(&managed_local_dir.join("inbox"))?;
    let inbox_dir = antigravity_inbox_dir(session_id)?;
    let message_path = inbox_dir.join(format!("msg-{message_id}.json"));
    write_private_json(&message_path, &payload)?;

    let claim_path = inbox_dir
        .join("claimed")
        .join(format!("claimed-msg-{message_id}.json"));
    Ok(EnqueuedMessage {
        message_id,
        inbox_dir,
        message_path,
        claim_path,
    })
}

/// Poll `claim_path` for up to `wait_claimed_secs`, matching
/// `wait_for_antigravity_message_claim` in `antigravity_hook_inbox.py`
/// (check, sleep 50ms, repeat; one final check after the deadline).
async fn wait_for_claim(claim_path: &Path, wait_claimed_secs: f64) -> Option<String> {
    let deadline =
        tokio::time::Instant::now() + Duration::from_secs_f64(wait_claimed_secs.max(0.0));
    loop {
        if let Some(claimed_at) = read_claimed_at(claim_path) {
            return Some(claimed_at);
        }
        if tokio::time::Instant::now() >= deadline {
            return read_claimed_at(claim_path);
        }
        tokio::time::sleep(CLAIM_POLL_INTERVAL).await;
    }
}

/// Enqueue `text` into the Antigravity hook inbox for `session_id` and wait
/// up to `wait_claimed_secs` for the shipped hook or PTY launcher to claim it.
/// Withdraws the message atomically via `rename` when unclaimed.
#[allow(dead_code)]
pub async fn send_text(
    session_id: &str,
    text: &str,
) -> std::result::Result<AntigravitySendOutcome, AntigravityControlError> {
    send_text_with_wait(session_id, text, None).await
}

pub async fn send_text_with_wait(
    session_id: &str,
    text: &str,
    wait_claimed_secs: Option<f64>,
) -> std::result::Result<AntigravitySendOutcome, AntigravityControlError> {
    deliver(
        session_id,
        text,
        "send",
        wait_claimed_secs,
        DEFAULT_WAIT_CLAIMED_SECS,
    )
    .await
}

/// Steer a running turn. Same inbox and same claim as `send_text`: agy's
/// PreInvocation hook returns the message as `injectSteps`, so it lands after
/// the tool call in flight and before the next model call. That is bounded,
/// not immediate: one long tool call delays it.
#[allow(dead_code)]
pub async fn steer_text(
    session_id: &str,
    text: &str,
) -> std::result::Result<AntigravitySendOutcome, AntigravityControlError> {
    steer_text_with_wait(session_id, text, None).await
}

pub async fn steer_text_with_wait(
    session_id: &str,
    text: &str,
    wait_claimed_secs: Option<f64>,
) -> std::result::Result<AntigravitySendOutcome, AntigravityControlError> {
    deliver(
        session_id,
        text,
        "steer",
        wait_claimed_secs,
        DEFAULT_STEER_WAIT_CLAIMED_SECS,
    )
    .await
}

/// Text of an interrupt message. The hook never injects it: it only needs a
/// non-empty body to claim, and answers with `terminationBehavior` instead.
const INTERRUPT_TEXT: &str = "Longhouse interrupt";

/// Interrupt a running Helm turn. The hook claims an `interrupt` message at the
/// next PreInvocation or PostInvocation and answers `terminationBehavior:
/// terminate`, so agy finishes the tool call in flight and starts no further
/// model call. A message not claimed in time is deleted rather than left
/// queued: a stale interrupt must never end a later turn. The TUI is the
/// user's own terminal, so kill-and-resume is not an option here (it would
/// close their terminal session).
pub async fn interrupt(session_id: &str) -> Result<AntigravitySendOutcome> {
    deliver(
        session_id,
        INTERRUPT_TEXT,
        "interrupt",
        None,
        DEFAULT_WAIT_CLAIMED_SECS,
    )
    .await
    .map_err(|err| anyhow!("{}", err.message()))
}

/// Where the Helm launcher records the provider process it spawned.
fn session_state_path(session_id: &str) -> Result<PathBuf> {
    Ok(antigravity_managed_local_dir()?
        .join("sessions")
        .join(format!("{session_id}.json")))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AntigravityTerminateOutcome {
    pub pid: u32,
    pub process_group_id: i32,
    pub shutdown: &'static str,
}

/// Grace between SIGTERM and SIGKILL for a terminated Helm group.
const TERMINATE_GRACE: Duration = Duration::from_secs(3);

/// Terminate a Helm session: stop the whole process group the launcher
/// recorded (agy's tool children outlive a plain kill of agy, confirmed on
/// 1.5.9). The recorded pid is verified against its start time first, so a
/// recycled pid is never signalled.
pub async fn terminate(session_id: &str) -> Result<AntigravityTerminateOutcome> {
    let state: Value = serde_json::from_slice(
        &std::fs::read(session_state_path(session_id)?)
            .context("Antigravity session has no state file")?,
    )
    .context("Antigravity session state is not valid JSON")?;
    let pid = state
        .get("provider_pid")
        .and_then(Value::as_u64)
        .and_then(|pid| u32::try_from(pid).ok())
        .context("Antigravity launcher recorded no provider pid")?;
    let pgid = state
        .get("provider_process_group_id")
        .and_then(Value::as_i64)
        .and_then(|pgid| i32::try_from(pgid).ok())
        .context("Antigravity launcher recorded no process group")?;
    let expected_start = state
        .get("provider_process_start_time")
        .and_then(Value::as_str)
        .context("Antigravity launcher recorded no process start time")?;
    match crate::process_identity::try_collect_process_fact(pid) {
        None => {
            return Ok(AntigravityTerminateOutcome {
                pid,
                process_group_id: pgid,
                shutdown: crate::process_group::GroupShutdown::Absent.as_str(),
            })
        }
        Some(fact) if !crate::process_identity::lstart_matches_recorded(&fact, expected_start) => {
            return Err(anyhow!(
            "Antigravity provider pid {pid} now belongs to another process; refusing to signal it"
        ))
        }
        Some(_) => {}
    }
    if crate::process_group::leader_group_for(pid) != Some(pgid) {
        return Err(anyhow!(
            "Antigravity provider process-group identity changed; refusing to signal it"
        ));
    }
    let outcome = crate::process_group::shutdown_group(pgid, TERMINATE_GRACE).await;
    if !outcome.is_gone() {
        return Err(anyhow!("Antigravity process group {pgid} survived SIGKILL"));
    }
    Ok(AntigravityTerminateOutcome {
        pid,
        process_group_id: pgid,
        shutdown: outcome.as_str(),
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum WithdrawOutcome {
    /// `rename` moved `msg-<id>.json` to `.withdrawn-<id>` and unlinked it,
    /// proving no claimer ever took the message.
    Withdrawn,
    /// `msg-<id>.json` was already gone: a claimer won the atomic rename.
    ClaimedByPeer,
}

fn withdraw_unclaimed(enqueued: &EnqueuedMessage) -> std::io::Result<WithdrawOutcome> {
    let withdrawn_path = enqueued
        .inbox_dir
        .join(format!(".withdrawn-{}", enqueued.message_id));
    match std::fs::rename(&enqueued.message_path, &withdrawn_path) {
        Ok(()) => {
            let _ = std::fs::remove_file(&withdrawn_path);
            Ok(WithdrawOutcome::Withdrawn)
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            Ok(WithdrawOutcome::ClaimedByPeer)
        }
        Err(err) => Err(err),
    }
}

async fn deliver(
    session_id: &str,
    text: &str,
    intent: &str,
    requested_wait_secs: Option<f64>,
    default_wait_secs: f64,
) -> std::result::Result<AntigravitySendOutcome, AntigravityControlError> {
    let base_secs = requested_wait_secs
        .filter(|secs| secs.is_finite() && *secs > 0.0)
        .map(|secs| secs.clamp(0.05, 60.0))
        .unwrap_or(default_wait_secs);
    let wait = crate::control_wait::control_wait(Duration::from_secs_f64(base_secs));
    send_text_with_timeout(session_id, text, intent, wait.as_secs_f64()).await
}

async fn send_text_with_timeout(
    session_id: &str,
    text: &str,
    intent: &str,
    wait_claimed_secs: f64,
) -> std::result::Result<AntigravitySendOutcome, AntigravityControlError> {
    let enqueued = enqueue(session_id, text, intent)?;
    if let Some(claimed_at) = wait_for_claim(&enqueued.claim_path, wait_claimed_secs).await {
        return Ok(AntigravitySendOutcome {
            message_id: enqueued.message_id,
            claimed_at,
        });
    }
    match withdraw_unclaimed(&enqueued) {
        Ok(WithdrawOutcome::Withdrawn) => {
            let detail = format!(
                "Antigravity hook did not claim queued input before timeout ({wait_claimed_secs}s)"
            );
            if intent == "send" {
                Err(AntigravityControlError::InputNotClaimed(detail))
            } else {
                Err(AntigravityControlError::CommandFailed(anyhow!("{detail}")))
            }
        }
        Ok(WithdrawOutcome::ClaimedByPeer) => {
            // A claimer won the rename just as our wait expired and may still be
            // writing `claimed_at`. Give it a short grace window before reporting
            // an indeterminate handoff (never `input_not_claimed`, which would
            // requeue and double-deliver).
            if let Some(claimed_at) =
                wait_for_claim(&enqueued.claim_path, CLAIM_FINALIZE_GRACE_SECS).await
            {
                return Ok(AntigravitySendOutcome {
                    message_id: enqueued.message_id,
                    claimed_at,
                });
            }
            Err(AntigravityControlError::DeliveryUnknown(format!(
                "Antigravity inbox message {} was claimed before withdrawal, but no claim receipt was finalized",
                enqueued.message_id
            )))
        }
        Err(err) => Err(AntigravityControlError::DeliveryUnknown(format!(
            "Could not withdraw unclaimed Antigravity inbox message {}: {err}",
            enqueued.message_id
        ))),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runtime() -> tokio::runtime::Runtime {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap()
    }

    #[test]
    fn enqueue_writes_the_exact_hook_inbox_message_shape_and_location() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            let enqueued = enqueue("session-abc", "hello there", "send").unwrap();

            let expected_dir = temp
                .path()
                .join("managed-local")
                .join("antigravity")
                .join("inbox")
                .join("session-abc");
            assert_eq!(enqueued.message_path.parent().unwrap(), expected_dir);
            assert_eq!(
                enqueued.message_path.file_name().unwrap().to_str().unwrap(),
                format!("msg-{}.json", enqueued.message_id)
            );
            assert_eq!(
                enqueued.claim_path,
                expected_dir
                    .join("claimed")
                    .join(format!("claimed-msg-{}.json", enqueued.message_id))
            );

            let bytes = std::fs::read(&enqueued.message_path).unwrap();
            let payload: Value = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(payload["id"], enqueued.message_id);
            assert_eq!(payload["session_id"], "session-abc");
            assert_eq!(payload["text"], "hello there");
            assert_eq!(payload["intent"], "send");
            assert!(payload["created_at"].as_str().unwrap().ends_with('Z'));
            assert!(payload["expires_at"].as_str().unwrap().ends_with('Z'));

            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let mode = std::fs::metadata(&enqueued.message_path)
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777;
                assert_eq!(mode, 0o600, "message file must be private");
            }

            // No stray temp files left behind after the atomic rename.
            let leftovers: Vec<_> = std::fs::read_dir(&expected_dir)
                .unwrap()
                .filter_map(|entry| entry.ok())
                .filter(|entry| entry.file_name().to_string_lossy().starts_with(".tmp."))
                .collect();
            assert!(leftovers.is_empty(), "{leftovers:?}");
        });
    }

    #[test]
    fn steer_writes_the_same_inbox_message_with_a_steer_intent() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            let enqueued = enqueue("session-steer", "change course", "steer").unwrap();
            let payload: Value =
                serde_json::from_slice(&std::fs::read(&enqueued.message_path).unwrap()).unwrap();
            assert_eq!(payload["intent"], "steer");
            assert_eq!(payload["text"], "change course");
            assert_eq!(
                enqueued.message_path.parent().unwrap(),
                temp.path()
                    .join("managed-local/antigravity/inbox/session-steer")
            );
        });
    }

    #[test]
    fn interrupt_writes_a_claimable_interrupt_message() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                // Nothing claims it: the message must be deleted, never left
                // behind to end a later turn.
                let result =
                    send_text_with_timeout("session-int", INTERRUPT_TEXT, "interrupt", 0.05).await;
                assert!(result.is_err());
                let inbox = temp
                    .path()
                    .join("managed-local/antigravity/inbox/session-int");
                let leftovers = std::fs::read_dir(&inbox)
                    .map(|dir| dir.filter_map(|e| e.ok()).count())
                    .unwrap_or(0);
                assert_eq!(leftovers, 0, "an unclaimed interrupt must not stay queued");
            });
        });
    }

    fn write_state(home: &Path, session_id: &str, state: Value) {
        let path = home
            .join("managed-local/antigravity/sessions")
            .join(format!("{session_id}.json"));
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, serde_json::to_vec(&state).unwrap()).unwrap();
    }

    #[test]
    fn terminate_reaps_the_whole_recorded_process_group_including_tool_children() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                // A stand-in for agy: a group leader whose tool child would
                // outlive a plain kill of the leader.
                let child_pid_file = temp.path().join("tool.pid");
                let mut leader = tokio::process::Command::new("sh")
                    .arg("-c")
                    .arg(format!(
                        "sleep 60 & echo $! > {}; wait",
                        child_pid_file.display()
                    ))
                    .process_group(0)
                    .spawn()
                    .unwrap();
                let pid = leader.id().unwrap();
                let fact = loop {
                    if let Some(fact) = crate::process_identity::try_collect_process_fact(pid) {
                        break fact;
                    }
                    tokio::time::sleep(Duration::from_millis(20)).await;
                };
                while !child_pid_file.exists()
                    || std::fs::read_to_string(&child_pid_file)
                        .unwrap()
                        .trim()
                        .is_empty()
                {
                    tokio::time::sleep(Duration::from_millis(20)).await;
                }
                let tool_pid: i32 = std::fs::read_to_string(&child_pid_file)
                    .unwrap()
                    .trim()
                    .parse()
                    .unwrap();
                write_state(
                    temp.path(),
                    "session-term",
                    json!({
                        "provider_pid": pid,
                        "provider_process_group_id": pid,
                        "provider_process_start_time": fact.lstart,
                    }),
                );
                let reaper = tokio::spawn(async move { leader.wait().await });
                let outcome = terminate("session-term").await.unwrap();
                reaper.await.unwrap().unwrap();
                assert!(
                    matches!(outcome.shutdown, "terminated" | "killed"),
                    "{outcome:?}"
                );
                crate::process_group::assert_group_gone(
                    pid as i32,
                    "the process group must be gone",
                )
                .await;
                assert_eq!(
                    unsafe { libc::kill(tool_pid, 0) },
                    -1,
                    "the tool child must not outlive terminate"
                );
            });
        });
    }

    #[test]
    fn terminate_refuses_a_pid_whose_start_time_changed() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                let mut bystander = tokio::process::Command::new("sleep")
                    .arg("30")
                    .process_group(0)
                    .kill_on_drop(true)
                    .spawn()
                    .unwrap();
                let pid = bystander.id().unwrap();
                write_state(
                    temp.path(),
                    "session-recycled",
                    json!({
                        "provider_pid": pid,
                        "provider_process_group_id": pid,
                        "provider_process_start_time": "Mon Jan  1 00:00:00 2001",
                    }),
                );
                let error = terminate("session-recycled").await.unwrap_err();
                assert!(error.to_string().contains("refusing to signal"), "{error}");
                assert!(crate::process_group::group_is_alive(pid as i32));
                let _ = bystander.kill().await;
            });
        });
    }

    #[test]
    fn terminate_without_a_recorded_process_is_an_error_not_a_guess() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                write_state(
                    temp.path(),
                    "session-none",
                    json!({"session_id": "session-none"}),
                );
                assert!(terminate("session-none").await.is_err());
            });
        });
    }

    #[test]
    fn send_text_returns_claimed_at_once_the_hook_claims_the_message() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                let enqueued = enqueue("session-claim", "queued text", "send").unwrap();
                std::fs::create_dir_all(enqueued.claim_path.parent().unwrap()).unwrap();
                std::fs::write(
                    &enqueued.claim_path,
                    serde_json::to_vec(&json!({
                        "id": enqueued.message_id,
                        "claimed_at": "2026-01-01T00:00:00.000000Z",
                    }))
                    .unwrap(),
                )
                .unwrap();

                let claimed_at = wait_for_claim(&enqueued.claim_path, 0.0).await.unwrap();
                assert_eq!(claimed_at, "2026-01-01T00:00:00.000000Z");
            });
        });
    }

    #[test]
    fn send_text_withdraws_atomically_with_input_not_claimed_code() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                let err = send_text_with_timeout("session-timeout", "never claimed", "send", 0.05)
                    .await
                    .unwrap_err();
                assert_eq!(err.code(), "input_not_claimed");

                let inbox_dir = temp
                    .path()
                    .join("managed-local")
                    .join("antigravity")
                    .join("inbox")
                    .join("session-timeout");
                let remaining: Vec<_> = std::fs::read_dir(&inbox_dir)
                    .unwrap()
                    .filter_map(|entry| entry.ok())
                    .collect();
                assert!(
                    remaining.is_empty(),
                    "unclaimed message and withdrawn scratch file must be gone: {remaining:?}"
                );
            });
        });
    }

    #[test]
    fn send_text_reports_delivery_unknown_when_peer_claims_without_finalizing_receipt() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                let enqueued = enqueue("session-half-claimed", "ambiguous", "send").unwrap();
                std::fs::create_dir_all(enqueued.claim_path.parent().unwrap()).unwrap();
                // Simulate a claimer renaming `msg-<id>.json` -> `claimed/` and
                // crashing before writing `claimed_at`.
                std::fs::rename(&enqueued.message_path, &enqueued.claim_path).unwrap();

                let outcome = withdraw_unclaimed(&enqueued).unwrap();
                assert_eq!(outcome, WithdrawOutcome::ClaimedByPeer);
                assert!(wait_for_claim(&enqueued.claim_path, 0.02).await.is_none());
            });
        });
    }

    #[test]
    fn claim_and_withdraw_race_1000_iterations_has_exact_exclusive_winner() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            for i in 0..1000 {
                let session = format!("race-{i}");
                let enqueued = enqueue(&session, "race payload", "send").unwrap();
                let claim_dir = enqueued.claim_path.parent().unwrap().to_path_buf();
                ensure_private_dir(&claim_dir).unwrap();

                let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
                let msg_for_claimer = enqueued.message_path.clone();
                let claim_for_claimer = enqueued.claim_path.clone();
                let b_claim = barrier.clone();
                let claimer = std::thread::spawn(move || {
                    b_claim.wait();
                    std::fs::rename(&msg_for_claimer, &claim_for_claimer).is_ok()
                });
                barrier.wait();
                let withdrawn = withdraw_unclaimed(&enqueued).unwrap();
                let claimer_won = claimer.join().unwrap();

                match (claimer_won, withdrawn) {
                    (true, WithdrawOutcome::ClaimedByPeer) => {
                        assert!(enqueued.claim_path.exists());
                        assert!(!enqueued.message_path.exists());
                    }
                    (false, WithdrawOutcome::Withdrawn) => {
                        assert!(!enqueued.claim_path.exists());
                        assert!(!enqueued.message_path.exists());
                    }
                    other => panic!("iteration {i} broke mutual exclusion: {other:?}"),
                }
            }
        });
    }

    #[test]
    fn enqueue_rejects_blank_session_id_and_text() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            assert!(enqueue("", "text", "send").is_err());
            assert!(enqueue("session", "   ", "send").is_err());
        });
    }

    /// Manual live proof, not run in CI: stage the *real* shipped Antigravity
    /// hook script with unmodified production Python
    /// (`_ensure_antigravity_runtime_plugin`), then invoke it as a real `agy`
    /// PreInvocation hook would, and confirm it claims and injects a message
    /// this module actually wrote via `enqueue`. This is the exact contract
    /// boundary this fix depends on -- it does not exercise a live `agy`
    /// turn (a live Helm launch is a separate, heavier proof).
    ///
    /// Run with: cargo test --bin longhouse-engine \
    ///   antigravity_channel_control::tests::live_proof_real_hook_script_claims_engine_written_message \
    ///   -- --ignored --nocapture
    #[ignore]
    #[tokio::test]
    async fn live_proof_real_hook_script_claims_engine_written_message() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let longhouse_home = temp.path().join("longhouse-home");
        let repo_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();

        let stage_script = format!(
            "import json\n\
             from pathlib import Path\n\
             from zerg.services.antigravity_hook_inbox import _ensure_antigravity_runtime_plugin, _ANTIGRAVITY_HOOK_SCRIPT_NAME\n\
             plugin_root = _ensure_antigravity_runtime_plugin(\n\
             \tconfig_dir=Path({home:?}),\n\
             \tantigravity_cli_root=Path({cli:?}),\n\
             \tglobal_hooks_path=Path({hooks:?}),\n\
             )\n\
             print(json.dumps({{'hook_script': str(plugin_root / _ANTIGRAVITY_HOOK_SCRIPT_NAME)}}))\n",
            home = longhouse_home.display(),
            cli = temp.path().join("antigravity-cli").display(),
            hooks = temp.path().join("hooks.json").display(),
        );
        let stage_output = std::process::Command::new("uv")
            .args(["run", "python", "-c", &stage_script])
            .current_dir(repo_root.join("server"))
            .output()
            .expect("uv run python for real hook staging");
        assert!(
            stage_output.status.success(),
            "staging the real hook script failed: {}",
            String::from_utf8_lossy(&stage_output.stderr)
        );
        let staged: Value = serde_json::from_slice(&stage_output.stdout)
            .expect("staging script printed valid JSON");
        let hook_script = PathBuf::from(staged["hook_script"].as_str().unwrap());
        assert!(
            hook_script.is_file(),
            "hook script was not staged at {}",
            hook_script.display()
        );

        let old_home = std::env::var_os("LONGHOUSE_HOME");
        std::env::set_var("LONGHOUSE_HOME", &longhouse_home);

        let session_id = format!("antigravity-live-proof-{}", Uuid::new_v4().simple());
        let text = format!("LIVE_PROOF_{}", Uuid::new_v4().simple());

        // This is the exact production function `session.send_text` calls
        // for provider "antigravity" in control_channel/dispatch.rs.
        let enqueued = enqueue(&session_id, &text, "send").unwrap();

        let hook_payload = json!({
            "conversationId": "unused-conversation-id",
            "workspacePaths": [temp.path().join("workspace").to_string_lossy()],
            "transcriptPath": temp.path().join("transcript.jsonl").to_string_lossy(),
            "stepIdx": 1,
        })
        .to_string();

        let output = (|| -> std::io::Result<std::process::Output> {
            use std::io::Write as _;
            let mut command = std::process::Command::new(&hook_script);
            command
                .arg("PreInvocation")
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped());
            // The hook resolves its inbox from the same identity a real launch
            // carries, so claim it the way every launch site does.
            crate::managed_identity::ManagedIdentity::new(
                crate::managed_identity_contract::ManagedProvider::Antigravity,
                &session_id,
            )
            .apply(&mut command, &[]);
            let mut child = command.spawn()?;
            child
                .stdin
                .take()
                .unwrap()
                .write_all(hook_payload.as_bytes())?;
            child.wait_with_output()
        })()
        .expect("invoke the real Antigravity hook script");

        if let Some(value) = old_home {
            std::env::set_var("LONGHOUSE_HOME", value);
        } else {
            std::env::remove_var("LONGHOUSE_HOME");
        }

        assert!(
            output.status.success(),
            "the real hook script exited non-zero: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        let hook_response: Value =
            serde_json::from_slice(&output.stdout).expect("hook script printed valid JSON");
        assert_eq!(
            hook_response,
            json!({"injectSteps": [{"userMessage": text}]}),
            "the real Antigravity hook script did not inject the engine-written message; stderr={}",
            String::from_utf8_lossy(&output.stderr)
        );

        let claimed_at = wait_for_claim(&enqueued.claim_path, 0.0)
            .await
            .expect("hook script left no claim receipt after a successful claim");
        assert!(!claimed_at.is_empty());
    }
    // ---- live receipt (real agy behind the scripted model; run on a bench host) ----

    /// A 1x1 PNG, enough for a real staged image file.
    const LIVE_PNG: &[u8] = &[
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1f,
        0x15, 0xc4, 0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0xf8,
        0xcf, 0xc0, 0xf0, 0x1f, 0x00, 0x05, 0x00, 0x01, 0xff, 0x89, 0x99, 0x3d, 0x1d, 0x00, 0x00,
        0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ];

    /// Run agy the way a managed Helm session runs it for the hook's purposes
    /// (managed identity env, own process group); the harness stands in for the
    /// user by passing the permission flag a user could type. Print mode stands
    /// in for the TUI: the hook inbox does not care which one called it.
    fn live_spawn(bin: &str, cwd: &Path, session_id: &str, prompt: &str) -> tokio::process::Child {
        let mut command = tokio::process::Command::new(bin);
        command
            .args([
                "--dangerously-skip-permissions",
                "--output-format",
                "stream-json",
                "--print-timeout",
                "0s",
                "--print",
                prompt,
            ])
            .current_dir(cwd)
            .env("LONGHOUSE_HOOK_PYTHON", "python3")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .process_group(0);
        crate::managed_identity::ManagedIdentity::new(
            crate::managed_identity_contract::ManagedProvider::Antigravity,
            session_id,
        )
        .apply(&mut command, &[]);
        command.spawn().expect("spawn agy")
    }

    async fn live_wait_for_hook(home: &Path, session_id: &str, event: &str) {
        let path = home
            .join("managed-local/antigravity/sessions")
            .join(format!("{session_id}.json"));
        let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
        loop {
            let seen = std::fs::read(&path)
                .ok()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
                .and_then(|state| {
                    state
                        .get("last_hook_event")
                        .and_then(Value::as_str)
                        .map(str::to_string)
                });
            if seen.as_deref() == Some(event) {
                return;
            }
            assert!(
                tokio::time::Instant::now() < deadline,
                "hook event {event} never observed"
            );
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    fn live_record_process(home: &Path, session_id: &str, child: &tokio::process::Child) {
        let pid = child.id().unwrap();
        let fact =
            crate::process_identity::try_collect_process_fact(pid).expect("agy process fact");
        let path = home
            .join("managed-local/antigravity/sessions")
            .join(format!("{session_id}.json"));
        let mut state = std::fs::read(&path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .and_then(|value| value.as_object().cloned())
            .unwrap_or_default();
        state.insert("provider_pid".into(), json!(pid));
        state.insert("provider_process_group_id".into(), json!(pid));
        state.insert("provider_process_start_time".into(), json!(fact.lstart));
        std::fs::write(&path, serde_json::to_vec(&Value::Object(state)).unwrap()).unwrap();
    }

    #[cfg(unix)]
    #[tokio::test(flavor = "current_thread")]
    #[ignore = "needs a real agy and the scripted model endpoint; run scripts/qa/agy-helm-live-receipt.sh on a bench host"]
    async fn live_agy_helm_receipt() {
        let _guard = crate::console_adapter::agent_state_guard();
        let bin = std::env::var("LONGHOUSE_ANTIGRAVITY_BIN").expect("LONGHOUSE_ANTIGRAVITY_BIN");
        let receipt_path =
            PathBuf::from(std::env::var("LIVE_RECEIPT_FILE").expect("LIVE_RECEIPT_FILE"));
        let cwd = PathBuf::from(std::env::var("LIVE_CWD").expect("LIVE_CWD"));
        let home = crate::config::get_longhouse_home().unwrap();
        std::fs::create_dir_all(&cwd).unwrap();
        let mut receipt = json!({"agy_bin": bin});
        let save = |receipt: &Value| {
            std::fs::write(&receipt_path, serde_json::to_vec_pretty(receipt).unwrap()).unwrap()
        };

        // 1. Steer with an image path through the hook inbox.
        let sid = format!("live-steer-{}", Uuid::new_v4().simple());
        let png = cwd.join("steer.png");
        std::fs::write(&png, LIVE_PNG).unwrap();
        let staged = vec![crate::input_attachments::StagedAttachment {
            path: png.clone(),
            mime_type: "image/png".to_string(),
        }];
        let started = std::time::Instant::now();
        let child = live_spawn(&bin, &cwd, &sid, "HELM_STEER_START");
        live_wait_for_hook(&home, &sid, "PreToolUse").await;
        let message = crate::input_attachments::prompt_with_attachments(
            "HELM_STEER_NOW change course",
            &staged,
        );
        let outcome = steer_text(&sid, &message).await;
        let output = tokio::time::timeout(Duration::from_secs(120), child.wait_with_output())
            .await
            .expect("steer turn ended")
            .unwrap();
        receipt["steer"] = json!({
            "claimed": outcome.as_ref().ok().map(|o| o.claimed_at.clone()),
            "error": outcome.as_ref().err().map(|e| e.to_string()),
            "image_path": png,
            "exit_code": output.status.code(),
            "elapsed_secs": started.elapsed().as_secs_f64(),
            "stdout_tail": String::from_utf8_lossy(&output.stdout).chars().rev().take(1200).collect::<String>().chars().rev().collect::<String>(),
        });
        save(&receipt);

        // 2. Interrupt: ends the turn at the next invocation boundary.
        let sid = format!("live-int-{}", Uuid::new_v4().simple());
        let started = std::time::Instant::now();
        let child = live_spawn(&bin, &cwd, &sid, "HELM_INT_START");
        live_wait_for_hook(&home, &sid, "PreToolUse").await;
        let outcome = interrupt(&sid).await;
        let output = tokio::time::timeout(Duration::from_secs(120), child.wait_with_output())
            .await
            .expect("interrupted turn ended")
            .unwrap();
        receipt["interrupt"] = json!({
            "claimed": outcome.as_ref().ok().map(|o| o.claimed_at.clone()),
            "error": outcome.as_ref().err().map(|e| e.to_string()),
            "exit_code": output.status.code(),
            "elapsed_secs": started.elapsed().as_secs_f64(),
            "stdout_tail": String::from_utf8_lossy(&output.stdout).chars().rev().take(1200).collect::<String>().chars().rev().collect::<String>(),
        });
        save(&receipt);

        // 3. Terminate: the whole process group, tool child included.
        let sid = format!("live-term-{}", Uuid::new_v4().simple());
        let mut child = live_spawn(&bin, &cwd, &sid, "HELM_TERM_START");
        live_wait_for_hook(&home, &sid, "PreToolUse").await;
        live_record_process(&home, &sid, &child);
        let pid = child.id().unwrap();
        let reaper = tokio::spawn(async move {
            let status = child.wait().await;
            status.ok().and_then(|s| s.code())
        });
        let started = std::time::Instant::now();
        let outcome = terminate(&sid).await;
        let exit = reaper.await.unwrap();
        receipt["terminate"] = json!({
            "shutdown": outcome.as_ref().ok().map(|o| o.shutdown),
            "error": outcome.as_ref().err().map(|e| e.to_string()),
            "exit_code": exit,
            "group_alive_after": crate::process_group::group_is_alive(pid as i32),
            "elapsed_secs": started.elapsed().as_secs_f64(),
        });
        save(&receipt);
    }
}
