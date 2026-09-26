//! Provider sign-in relay: run a provider's own login CLI on this machine and
//! hand its verification URL (and device code) to the Runtime Host, so a user
//! can sign a remote machine in from their phone.
//!
//! Longhouse never sees or stores the credential. The provider CLI completes
//! its own OAuth/device flow and writes the token to its own store on this
//! machine; the engine only relays the URL/code out and, for paste-back flows,
//! the user's code in. Only argv declared in the managed provider manifest
//! (`sign_in`) is ever run.

use std::collections::HashMap;
use std::ffi::OsString;
use std::process::Stdio;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{mpsc, Notify};

/// How long a provider's login may wait for the user before it is abandoned.
const ATTEMPT_TTL: Duration = Duration::from_secs(15 * 60);
/// How long to wait for the CLI to print its URL (and code) after starting.
const PROMPT_TIMEOUT: Duration = Duration::from_secs(20);

pub struct SignInError {
    pub code: &'static str,
    pub message: String,
}

fn err(code: &'static str, message: impl Into<String>) -> SignInError {
    SignInError {
        code,
        message: message.into(),
    }
}

struct Attempt {
    provider: String,
    stdin: Option<ChildStdin>,
    // Woken by cancel()/cancel_provider() so the watcher below kills the
    // login process immediately instead of leaving it running until it
    // exits on its own or the 15-minute ATTEMPT_TTL elapses. A device-code
    // flow never reads stdin, so dropping it (the old cancel path) never
    // signalled a still-polling `codex login --device-auth`.
    cancel: Arc<Notify>,
}

fn attempts() -> &'static Mutex<HashMap<String, Attempt>> {
    static ATTEMPTS: OnceLock<Mutex<HashMap<String, Attempt>>> = OnceLock::new();
    ATTEMPTS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Woken when a login process exits, so readiness is re-probed at once rather
/// than on the next periodic refresh.
pub fn readiness_refresh_now() -> &'static Notify {
    static NOTIFY: OnceLock<Notify> = OnceLock::new();
    NOTIFY.get_or_init(Notify::new)
}

/// The declared sign-in block for a provider, when it is runnable.
pub fn declared_sign_in(contract: &Value) -> Option<&Value> {
    let block = contract.get("sign_in")?;
    (block.get("disposition").and_then(Value::as_str) == Some("implemented")).then_some(block)
}

pub async fn start(contract: &Value, binary: OsString) -> Result<Value, SignInError> {
    let provider = contract
        .get("provider")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let block = declared_sign_in(contract).ok_or_else(|| {
        err(
            "sign_in_unsupported",
            format!("{provider} has no sign-in relay"),
        )
    })?;
    let argv: Vec<String> = block
        .get("argv")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    let flow = block
        .get("flow")
        .and_then(Value::as_str)
        .unwrap_or("device_code")
        .to_string();

    // One login per provider: a second start replaces the first.
    cancel_provider(&provider);

    // no managed identity: this runs the provider's own login command, not a
    // session. It produces no transcript for Longhouse to attribute and ends
    // when the provider has stored its credential.

    let mut child: Child = Command::new(&binary)
        .args(&argv)
        .env("NO_COLOR", "1")
        .env("TERM", "dumb")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|error| {
            err(
                "sign_in_spawn_failed",
                format!("could not start {provider} login: {error}"),
            )
        })?;

    let (line_tx, mut line_rx) = mpsc::unbounded_channel::<String>();
    for reader in [
        child
            .stdout
            .take()
            .map(|s| Box::new(s) as Box<dyn tokio::io::AsyncRead + Unpin + Send>),
        child
            .stderr
            .take()
            .map(|s| Box::new(s) as Box<dyn tokio::io::AsyncRead + Unpin + Send>),
    ]
    .into_iter()
    .flatten()
    {
        let tx = line_tx.clone();
        tokio::spawn(async move {
            let mut lines = BufReader::new(reader).lines();
            while let Ok(Some(line)) = lines.next_line().await {
                if tx.send(line).is_err() {
                    break;
                }
            }
        });
    }
    drop(line_tx);

    let mut prompt = SignInPrompt::default();
    let wait = tokio::time::timeout(PROMPT_TIMEOUT, async {
        while let Some(line) = line_rx.recv().await {
            prompt.observe(&strip_ansi(&line));
            if prompt.is_complete(&flow) {
                break;
            }
        }
    })
    .await;
    if wait.is_err() || !prompt.is_complete(&flow) {
        let _ = child.kill().await;
        return Err(err(
            "sign_in_prompt_missing",
            format!(
                "{provider} login did not print a sign-in URL{}",
                if flow == "device_code" {
                    " and code"
                } else {
                    ""
                }
            ),
        ));
    }

    let attempt_id = uuid::Uuid::new_v4().to_string();
    let stdin = child.stdin.take();
    let cancel_notify = Arc::new(Notify::new());
    attempts().lock().expect("sign-in attempts lock").insert(
        attempt_id.clone(),
        Attempt {
            provider: provider.clone(),
            stdin,
            cancel: cancel_notify.clone(),
        },
    );

    let waiter_id = attempt_id.clone();
    tokio::spawn(async move {
        // Keep draining output so the CLI never blocks on a full pipe.
        let drain = async { while line_rx.recv().await.is_some() {} };
        tokio::select! {
            _ = async { tokio::join!(child.wait(), drain); } => {}
            _ = tokio::time::sleep(ATTEMPT_TTL) => {}
            // An explicit cancel wakes this immediately rather than waiting
            // on the CLI to exit by itself or the TTL to elapse.
            _ = cancel_notify.notified() => {}
        }
        let _ = child.kill().await;
        attempts()
            .lock()
            .expect("sign-in attempts lock")
            .remove(&waiter_id);
        readiness_refresh_now().notify_one();
    });

    Ok(json!({
        "attempt_id": attempt_id,
        "provider": provider,
        "flow": flow,
        "verification_url": prompt.url,
        "user_code": prompt.code,
        "prerequisite": block.get("prerequisite").cloned().unwrap_or(Value::Null),
        "expires_in_secs": ATTEMPT_TTL.as_secs(),
    }))
}

pub async fn submit_code(attempt_id: &str, code: &str) -> Result<Value, SignInError> {
    let code = code.trim();
    if code.is_empty() || code.contains(['\n', '\r']) {
        return Err(err(
            "sign_in_code_invalid",
            "code must be one non-empty line",
        ));
    }
    let stdin = attempts()
        .lock()
        .expect("sign-in attempts lock")
        .get_mut(attempt_id)
        .and_then(|attempt| attempt.stdin.take());
    let Some(mut stdin) = stdin else {
        return Err(err(
            "sign_in_attempt_missing",
            "this sign-in has finished or expired; start again",
        ));
    };
    stdin
        .write_all(format!("{code}\n").as_bytes())
        .await
        .map_err(|error| err("sign_in_code_write_failed", error.to_string()))?;
    let _ = stdin.flush().await;
    Ok(json!({"attempt_id": attempt_id, "accepted": true}))
}

pub fn cancel(attempt_id: &str) -> Value {
    // Removing the entry also closes stdin, which ends a paste-back flow
    // waiting on it; the cancel notify is what stops a device-code flow that
    // never reads stdin and would otherwise keep polling until it exits on
    // its own or the 15-minute ATTEMPT_TTL elapses.
    let notify = attempts()
        .lock()
        .expect("sign-in attempts lock")
        .remove(attempt_id)
        .map(|attempt| attempt.cancel);
    let cancelled = notify.is_some();
    if let Some(notify) = notify {
        notify.notify_one();
    }
    json!({"attempt_id": attempt_id, "cancelled": cancelled})
}

fn cancel_provider(provider: &str) {
    let mut guard = attempts().lock().expect("sign-in attempts lock");
    let mut superseded = Vec::new();
    guard.retain(|_, attempt| {
        if attempt.provider == provider {
            superseded.push(attempt.cancel.clone());
            false
        } else {
            true
        }
    });
    drop(guard);
    // A second start for the same provider must not orphan the first
    // attempt's still-running login process.
    for notify in superseded {
        notify.notify_one();
    }
}

#[derive(Default, Debug, PartialEq)]
struct SignInPrompt {
    url: Option<String>,
    code: Option<String>,
}

impl SignInPrompt {
    fn observe(&mut self, line: &str) {
        for token in line.split_whitespace() {
            let token =
                token.trim_matches(|c: char| matches!(c, '(' | ')' | '<' | '>' | ',' | '"' | '\''));
            if self.url.is_none() && token.starts_with("https://") {
                self.url = Some(token.trim_end_matches(['.', ':']).to_string());
            } else if self.code.is_none() && looks_like_device_code(token) {
                self.code = Some(token.to_string());
            }
        }
    }

    fn is_complete(&self, flow: &str) -> bool {
        self.url.is_some() && (flow != "device_code" || self.code.is_some())
    }
}

/// Device codes are two short uppercase alphanumeric groups: `5DD9-4F49`,
/// `VWSN-8F9KZ`.
fn looks_like_device_code(token: &str) -> bool {
    let Some((left, right)) = token.split_once('-') else {
        return false;
    };
    let group = |part: &str| {
        (4..=6).contains(&part.len())
            && part
                .chars()
                .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit())
    };
    group(left) && group(right) && !right.contains('-')
}

fn strip_ansi(line: &str) -> String {
    let mut out = String::with_capacity(line.len());
    let mut chars = line.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            if chars.peek() == Some(&'[') {
                chars.next();
                for next in chars.by_ref() {
                    if next.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
            continue;
        }
        out.push(c);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_codex_device_prompt() {
        let mut prompt = SignInPrompt::default();
        for line in [
            "1. Open this link in your browser and sign in to your account",
            "   \u{1b}[94mhttps://auth.openai.com/codex/device\u{1b}[0m",
            "2. Enter this one-time code (expires in 15 minutes)",
            "   \u{1b}[94mVWSN-8F9KZ\u{1b}[0m",
        ] {
            prompt.observe(&strip_ansi(line));
        }
        assert_eq!(
            prompt.url.as_deref(),
            Some("https://auth.openai.com/codex/device")
        );
        assert_eq!(prompt.code.as_deref(), Some("VWSN-8F9KZ"));
        assert!(prompt.is_complete("device_code"));
    }

    #[test]
    fn parses_claude_paste_code_prompt_without_a_device_code() {
        let mut prompt = SignInPrompt::default();
        prompt.observe("Opening browser to sign in…");
        prompt.observe("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y");
        assert_eq!(
            prompt.url.as_deref(),
            Some("https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=y")
        );
        assert_eq!(prompt.code, None);
        assert!(prompt.is_complete("paste_code"));
        assert!(!prompt.is_complete("device_code"));
    }

    #[test]
    fn device_code_shape_rejects_ordinary_words() {
        assert!(looks_like_device_code("5DD9-4F49"));
        assert!(!looks_like_device_code("one-time"));
        assert!(!looks_like_device_code("2026-09-24"));
        assert!(!looks_like_device_code("AB-CD"));
    }

    #[tokio::test]
    async fn submitting_to_an_unknown_attempt_is_a_typed_error() {
        let error = submit_code("missing", "abc").await.err().expect("error");
        assert_eq!(error.code, "sign_in_attempt_missing");
    }

    fn process_alive(pid: i32) -> bool {
        std::process::Command::new("kill")
            .arg("-0")
            .arg(pid.to_string())
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }

    /// A device-code login (codex) never reads stdin after printing its
    /// prompt, so the old cancel path -- which only dropped the stdin handle
    /// -- never signalled it; the process kept polling until it exited on
    /// its own or the 15-minute ATTEMPT_TTL elapsed. Live-proved on
    /// 2026-09-26: `codex login --device-auth` was still running two minutes
    /// after a real `cancel` call. cancel() must kill the process directly.
    #[tokio::test]
    async fn cancel_kills_a_login_process_that_never_reads_stdin() {
        let pid_file = std::env::temp_dir().join(format!(
            "longhouse-sign-in-cancel-test-{}-{}.pid",
            std::process::id(),
            uuid::Uuid::new_v4()
        ));
        let contract = json!({
            "provider": "cancel-test",
            "sign_in": {
                "disposition": "implemented",
                "flow": "device_code",
                "argv": [
                    "-c",
                    format!(
                        "echo $$ > {path}; echo https://example.com/device; echo AAAA-BBBB; sleep 30",
                        path = pid_file.display(),
                    ),
                ],
            },
        });

        let result = match start(&contract, OsString::from("/bin/sh")).await {
            Ok(result) => result,
            Err(error) => panic!(
                "start should observe the printed prompt: {} {}",
                error.code, error.message
            ),
        };
        let attempt_id = result["attempt_id"]
            .as_str()
            .expect("attempt_id")
            .to_string();

        let deadline = std::time::Instant::now() + Duration::from_secs(5);
        let pid: i32 = loop {
            if let Ok(text) = std::fs::read_to_string(&pid_file) {
                if let Ok(pid) = text.trim().parse() {
                    break pid;
                }
            }
            assert!(
                std::time::Instant::now() < deadline,
                "child never wrote its pid"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        };
        assert!(process_alive(pid), "child should be running before cancel");

        let response = cancel(&attempt_id);
        assert_eq!(response["cancelled"], true);

        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while process_alive(pid) {
            assert!(
                std::time::Instant::now() < deadline,
                "cancel should kill a device-code login promptly instead of leaving it to the 15-minute TTL"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }

        let _ = std::fs::remove_file(&pid_file);
    }
}
