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
//! atomic-write style that `server/zerg/services/antigravity_hook_inbox.py`'s
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
/// Matches the retired CLI's `--wait-claimed-secs` default.
const DEFAULT_WAIT_CLAIMED_SECS: f64 = 15.0;
/// Matches the hook script's claim-poll interval.
const CLAIM_POLL_INTERVAL: Duration = Duration::from_millis(50);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AntigravitySendOutcome {
    pub message_id: String,
    pub claimed_at: String,
}

struct EnqueuedMessage {
    message_id: String,
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

fn enqueue(session_id: &str, text: &str) -> Result<EnqueuedMessage> {
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
        "intent": "send",
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
/// up to `wait_claimed_secs` for the shipped hook to claim it. Deletes the
/// message and fails if no claim shows up in time, matching the retired
/// `longhouse antigravity-channel send` CLI's behavior.
pub async fn send_text(session_id: &str, text: &str) -> Result<AntigravitySendOutcome> {
    send_text_with_timeout(session_id, text, DEFAULT_WAIT_CLAIMED_SECS).await
}

async fn send_text_with_timeout(
    session_id: &str,
    text: &str,
    wait_claimed_secs: f64,
) -> Result<AntigravitySendOutcome> {
    let enqueued = enqueue(session_id, text)?;
    if let Some(claimed_at) = wait_for_claim(&enqueued.claim_path, wait_claimed_secs).await {
        return Ok(AntigravitySendOutcome {
            message_id: enqueued.message_id,
            claimed_at,
        });
    }
    let _ = std::fs::remove_file(&enqueued.message_path);
    Err(anyhow!(
        "Antigravity hook did not claim queued input before timeout ({wait_claimed_secs}s)"
    ))
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
            let enqueued = enqueue("session-abc", "hello there").unwrap();

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
    fn send_text_returns_claimed_at_once_the_hook_claims_the_message() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                let enqueued = enqueue("session-claim", "queued text").unwrap();
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
    fn send_text_deletes_the_message_and_fails_when_never_claimed() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        let rt = runtime();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            rt.block_on(async {
                let result = send_text_with_timeout("session-timeout", "never claimed", 0.05).await;
                assert!(result.is_err(), "{result:?}");

                let inbox_dir = temp
                    .path()
                    .join("managed-local")
                    .join("antigravity")
                    .join("inbox")
                    .join("session-timeout");
                let remaining: Vec<_> = std::fs::read_dir(&inbox_dir)
                    .unwrap()
                    .filter_map(|entry| entry.ok())
                    .filter(|entry| entry.file_name().to_string_lossy().starts_with("msg-"))
                    .collect();
                assert!(
                    remaining.is_empty(),
                    "unclaimed message must be deleted: {remaining:?}"
                );
            });
        });
    }

    #[test]
    fn enqueue_rejects_blank_session_id_and_text() {
        let _guard = crate::console_adapter::agent_state_guard();
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            assert!(enqueue("", "text").is_err());
            assert!(enqueue("session", "   ").is_err());
        });
    }

    /// Manual live proof, not run in CI: stage the *real* shipped Antigravity
    /// hook script with unmodified production Python
    /// (`_ensure_antigravity_runtime_plugin`), then invoke it as a real `agy`
    /// PreInvocation hook would, and confirm it claims and injects a message
    /// this module actually wrote via `enqueue`. This is the exact contract
    /// boundary this fix depends on -- it does not exercise a live `agy`
    /// turn (hooks don't fire under `GEMINI_API_KEY` auth, and a signed-in
    /// profile plus a Helm launch is a separate, heavier proof).
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
        // for provider "antigravity" in control_channel.rs.
        let enqueued = enqueue(&session_id, &text).unwrap();

        let hook_payload = json!({
            "conversationId": "unused-conversation-id",
            "workspacePaths": [temp.path().join("workspace").to_string_lossy()],
            "transcriptPath": temp.path().join("transcript.jsonl").to_string_lossy(),
            "stepIdx": 1,
        })
        .to_string();

        let output = (|| -> std::io::Result<std::process::Output> {
            use std::io::Write as _;
            let mut child = std::process::Command::new(&hook_script)
                .arg("PreInvocation")
                .env("LONGHOUSE_MANAGED_SESSION_ID", &session_id)
                .env("LONGHOUSE_MANAGED_PROVIDER", "antigravity")
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .stderr(std::process::Stdio::piped())
                .spawn()?;
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
}
