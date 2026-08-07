//! Runtime Host side of the managed Helm launch transaction.
//!
//! Registration creates a durable `pending` launch. The provider launcher must
//! then either confirm that its provider/control driver reached ready, or abort
//! with the startup error. A provider exit after confirmation is ordinary Helm
//! lifecycle, not a launch abort.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use anyhow::Context;
use serde::Deserialize;
use serde_json::{json, Value};

// Facade-only: the engine binary reaches the Runtime Host through the bounded
// entry point, so these are dead there but live in `longhouse`.
#[allow(dead_code)]
const REGISTRATION_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// Directory scanned by native local health (`device::collect_managed_launch_recovery`)
/// to decide whether a managed launch is still recovering its registration.
/// A degraded launch writes a receipt here so the menu bar and `longhouse
/// doctor` can see the degradation from another process; process liveness
/// alone must never be read as proof that control exists.
/// `agent_dir` is the caller's `<longhouse home>/agent`. It is passed in rather
/// than resolved here because the engine and the facade binary each own their
/// own home resolution and this module is shared by both.
fn retry_receipt_path(
    agent_dir: &std::path::Path,
    kind: &str,
    session_id: &str,
) -> anyhow::Result<std::path::PathBuf> {
    let directory = agent_dir.join("managed-local").join(kind);
    std::fs::create_dir_all(&directory).context("create managed launch recovery directory")?;
    Ok(directory.join(format!("{session_id}.json")))
}

fn registration_retry_path(
    agent_dir: &std::path::Path,
    session_id: &str,
) -> anyhow::Result<std::path::PathBuf> {
    retry_receipt_path(agent_dir, "registration-retries", session_id)
}

/// Record that a launch started without Runtime Host registration and is
/// retrying. `exhausted` flips the receipt to a terminal state so health can
/// distinguish "recovering" from "gave up".
pub fn record_registration_retry(
    agent_dir: &std::path::Path,
    session_id: &str,
    provider: &str,
    exhausted: bool,
) -> anyhow::Result<()> {
    let path = registration_retry_path(agent_dir, session_id)?;
    let payload = json!({
        "schema_version": 1,
        "session_id": session_id,
        "provider": provider,
        "recovery_exhausted": exhausted,
        // Coordination authority is minted by the Runtime Host at registration.
        // A degraded launch never had one, and this receipt does not imply the
        // running provider acquires it later.
        "coordination_state": "unavailable",
        "created_at": chrono::Utc::now().to_rfc3339(),
    });
    std::fs::write(&path, serde_json::to_vec_pretty(&payload)?)
        .with_context(|| format!("write managed launch recovery receipt {}", path.display()))
}

/// Drop the receipt once registration succeeds, so health stops reporting
/// active recovery.
pub fn clear_registration_retry(agent_dir: &std::path::Path, session_id: &str) {
    if let Ok(path) = registration_retry_path(agent_dir, session_id) {
        let _ = std::fs::remove_file(path);
    }
}

/// Record that a provider is running with an unsettled durable launch outcome.
///
/// Registration already succeeded here, so coordination authority exists and
/// the session is genuinely controllable; only the confirmation is unrecorded.
/// That is a weaker degradation than a failed registration and health reads it
/// from its own directory, where a fresh receipt is ordinary convergence and an
/// aged or exhausted one is real degradation.
pub fn record_outcome_retry(
    agent_dir: &std::path::Path,
    session_id: &str,
    provider: &str,
    exhausted: bool,
) -> anyhow::Result<()> {
    let path = retry_receipt_path(agent_dir, "outcome-retries", session_id)?;
    let payload = json!({
        "schema_version": 1,
        "session_id": session_id,
        "provider": provider,
        "recovery_exhausted": exhausted,
        "stage": "confirmation",
        "coordination_state": "available",
        "created_at": chrono::Utc::now().to_rfc3339(),
    });
    std::fs::write(&path, serde_json::to_vec_pretty(&payload)?)
        .with_context(|| format!("write managed launch outcome receipt {}", path.display()))
}

/// Drop the receipt once the outcome is durably recorded.
pub fn clear_outcome_retry(agent_dir: &std::path::Path, session_id: &str) {
    if let Ok(path) = retry_receipt_path(agent_dir, "outcome-retries", session_id) {
        let _ = std::fs::remove_file(path);
    }
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)] // Fields are split across the facade and engine binary consumers.
pub struct ManagedLaunchResponse {
    pub session_id: String,
    pub run_id: String,
    pub provider_session_id: Option<String>,
    pub permission_mode: Option<String>,
    pub hook_token: Option<String>,
    pub managed_transport: Option<String>,
    pub coordination_token: Option<String>,
}

impl ManagedLaunchResponse {
    pub fn validate_transport(
        &self,
        provider_name: &str,
        expected_transport: &str,
    ) -> anyhow::Result<()> {
        if self.managed_transport.as_deref() != Some(expected_transport) {
            anyhow::bail!(
                "Runtime Host returned an unsupported managed-local transport for {provider_name} (expected {expected_transport}, got {})",
                self.managed_transport.as_deref().unwrap_or("missing")
            );
        }
        Ok(())
    }

    pub fn coordination_token(&self) -> Option<&str> {
        self.coordination_token
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
    }

    pub fn require_authority(
        &self,
        provider_name: &str,
        expected_transport: &str,
    ) -> anyhow::Result<&str> {
        self.validate_transport(provider_name, expected_transport)?;
        self.coordination_token()
            .context("Longhouse did not issue coordination authority for this session")
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LaunchOutcome {
    Confirmed,
    Aborted,
}

pub struct ManagedLaunchTransaction<'a> {
    runtime: &'a tokio::runtime::Runtime,
    url: &'a str,
    device_token: &'a str,
    session_id: &'a str,
    run_id: &'a str,
    confirmed: bool,
}

impl<'a> ManagedLaunchTransaction<'a> {
    pub fn new(
        runtime: &'a tokio::runtime::Runtime,
        url: &'a str,
        device_token: &'a str,
        session_id: &'a str,
        run_id: &'a str,
    ) -> Self {
        Self {
            runtime,
            url,
            device_token,
            session_id,
            run_id,
            confirmed: false,
        }
    }

    pub fn confirm(&mut self) -> anyhow::Result<()> {
        self.runtime.block_on(report_launch_outcome(
            self.url,
            self.device_token,
            self.session_id,
            self.run_id,
            LaunchOutcome::Confirmed,
            None,
        ))?;
        self.confirmed = true;
        Ok(())
    }

    /// Settle a launch whose provider process already exists.
    ///
    /// Confirmation is bookkeeping about a process that is already spawned, so
    /// a Runtime Host outage must never unspawn it. Every caller previously
    /// treated a failed confirm as a fatal launch error and killed the
    /// provider, which turned one lost HTTP response into "Longhouse will not
    /// let you open your agent" — including the common case where the outcome
    /// committed durably and only the response was lost.
    ///
    /// Degrading marks the transaction settled, because its `Drop` would
    /// otherwise report a launch abort for a provider that really did start,
    /// and hands the same idempotent outcome to a bounded background retry.
    pub fn confirm_or_degrade(
        &mut self,
        provider: &str,
        agent_dir: &std::path::Path,
        notices: &DeferredNotices,
    ) {
        let error = match self.confirm() {
            Ok(()) => return,
            Err(error) => error,
        };
        self.confirmed = true;
        notices.push(format!(
            "Longhouse warning: {provider} started but Longhouse could not record the launch outcome; retrying in the background: {error:#}"
        ));
        if let Err(error) = record_outcome_retry(agent_dir, self.session_id, provider, false) {
            notices.push(format!(
                "Longhouse warning: could not record {provider} launch outcome recovery state: {error:#}"
            ));
        }
        spawn_launch_outcome_retry(
            self.url,
            self.device_token,
            self.session_id,
            self.run_id,
            provider,
            agent_dir.to_path_buf(),
            notices.clone(),
        );
    }
}

/// Replay one unrecorded launch confirmation until the Runtime Host accepts it.
///
/// The outcome is idempotent on (session, run): the Runtime Host accepts an
/// exact replay of an already-committed confirmation instead of treating it as
/// a conflict, so retrying a lost response cannot corrupt durable state.
fn spawn_launch_outcome_retry(
    url: &str,
    device_token: &str,
    session_id: &str,
    run_id: &str,
    provider: &str,
    agent_dir: PathBuf,
    notices: DeferredNotices,
) {
    let url = url.to_string();
    let device_token = device_token.to_string();
    let session_id = session_id.to_string();
    let run_id = run_id.to_string();
    let provider = provider.to_string();
    std::thread::spawn(move || {
        let Ok(runtime) = tokio::runtime::Runtime::new() else {
            let _ = record_outcome_retry(&agent_dir, &session_id, &provider, true);
            return;
        };
        let mut last_error = None;
        for attempt in 0..5_u32 {
            // Back off between attempts, never before the first one. A detached
            // launch (`--no-attach`) returns within milliseconds and takes this
            // thread with it, so a sleep-first loop never reached the Runtime
            // Host at all and left the launch permanently unrecorded.
            if attempt > 0 {
                std::thread::sleep(Duration::from_secs(2_u64.pow(attempt - 1)));
            }
            match runtime.block_on(report_launch_outcome(
                &url,
                &device_token,
                &session_id,
                &run_id,
                LaunchOutcome::Confirmed,
                None,
            )) {
                Ok(()) => {
                    clear_outcome_retry(&agent_dir, &session_id);
                    return;
                }
                Err(error) => last_error = Some(error),
            }
        }
        if let Some(error) = last_error {
            notices.push(format!(
                "Longhouse warning: {provider} is running but its launch outcome was never recorded: {error:#}"
            ));
        }
        let _ = record_outcome_retry(&agent_dir, &session_id, &provider, true);
    });
}

/// Register one managed launch with the Runtime Host.
///
/// Every provider gets the same deadline, status/body error, response decode,
/// and launch-identity validation. Provider drivers validate only their own
/// transport-specific fields after this returns.
#[allow(dead_code)]
pub fn register_managed_launch(
    runtime: &tokio::runtime::Runtime,
    url: &str,
    device_token: &str,
    provider_name: &str,
    payload: &Value,
    expected_session_id: Option<&str>,
) -> anyhow::Result<ManagedLaunchResponse> {
    register_managed_launch_with_timeout(
        runtime,
        url,
        device_token,
        provider_name,
        payload,
        expected_session_id,
        REGISTRATION_TIMEOUT,
    )
}

/// Register with a caller-selected bound. Degraded Helm uses a short first
/// attempt so a Runtime Host outage cannot gate the provider TUI, then retries
/// the same client-minted identity in the background.
pub fn register_managed_launch_with_timeout(
    runtime: &tokio::runtime::Runtime,
    url: &str,
    device_token: &str,
    provider_name: &str,
    payload: &Value,
    expected_session_id: Option<&str>,
    timeout: std::time::Duration,
) -> anyhow::Result<ManagedLaunchResponse> {
    let endpoint = format!(
        "{}/api/sessions/managed-local/this-device",
        url.trim_end_matches('/')
    );
    let response = runtime.block_on(async {
        let response = reqwest::Client::new()
            .post(endpoint)
            .header("X-Agents-Token", device_token)
            .json(payload)
            .timeout(timeout)
            .send()
            .await
            .with_context(|| format!("register managed {provider_name} launch"))?;
        let status = response.status();
        let body = response
            .text()
            .await
            .with_context(|| format!("read managed {provider_name} registration response"))?;
        if !status.is_success() {
            anyhow::bail!(
                "managed {provider_name} launch failed ({status}): {}",
                truncate(body.trim(), 500)
            );
        }
        serde_json::from_str::<ManagedLaunchResponse>(&body)
            .with_context(|| format!("decode managed {provider_name} registration response"))
    })?;
    validate_launch_identity(response, provider_name, expected_session_id)
}

fn validate_launch_identity(
    response: ManagedLaunchResponse,
    provider_name: &str,
    expected_session_id: Option<&str>,
) -> anyhow::Result<ManagedLaunchResponse> {
    if response.session_id.trim().is_empty() {
        anyhow::bail!("Runtime Host returned no {provider_name} session identity");
    }
    if response.run_id.trim().is_empty() {
        anyhow::bail!("Runtime Host returned no {provider_name} run identity");
    }
    if expected_session_id.is_some_and(|expected| response.session_id != expected) {
        anyhow::bail!("Runtime Host returned a mismatched {provider_name} session identity");
    }
    Ok(response)
}

impl Drop for ManagedLaunchTransaction<'_> {
    fn drop(&mut self) {
        if self.confirmed {
            return;
        }
        let launch_error = anyhow::anyhow!("provider launcher exited before readiness");
        if let Err(error) = self.runtime.block_on(report_launch_outcome(
            self.url,
            self.device_token,
            self.session_id,
            self.run_id,
            LaunchOutcome::Aborted,
            Some(&launch_error),
        )) {
            eprintln!(
                "Longhouse warning: launch failed and its abort could not be recorded: {error:#}"
            );
        }
    }
}

impl LaunchOutcome {
    fn as_wire(self) -> &'static str {
        match self {
            Self::Confirmed => "confirmed",
            Self::Aborted => "aborted",
        }
    }
}

pub async fn report_launch_outcome(
    url: &str,
    device_token: &str,
    session_id: &str,
    run_id: &str,
    outcome: LaunchOutcome,
    error: Option<&anyhow::Error>,
) -> anyhow::Result<()> {
    let (error_code, error_message) = match outcome {
        LaunchOutcome::Confirmed => (None, None),
        LaunchOutcome::Aborted => (
            Some("provider_launch_failed"),
            error.map(|value| truncate(&format!("{value:#}"), 2000)),
        ),
    };
    let response = reqwest::Client::new()
        .post(format!(
            "{}/api/agents/sessions/{session_id}/launch-outcome",
            url.trim_end_matches('/')
        ))
        .header("X-Agents-Token", device_token)
        .json(&json!({
            "run_id": run_id,
            "outcome": outcome.as_wire(),
            "error_code": error_code,
            "error_message": error_message,
        }))
        .timeout(std::time::Duration::from_secs(5))
        .send()
        .await
        .context("send managed launch outcome")?;
    if !response.status().is_success() {
        let status = response.status();
        let detail = response.text().await.unwrap_or_default();
        anyhow::bail!(
            "Runtime Host rejected managed launch {} for {session_id} ({status}): {}",
            outcome.as_wire(),
            truncate(detail.trim(), 500)
        );
    }
    let payload: Value = response
        .json()
        .await
        .context("decode managed launch outcome response")?;
    if payload.get("recorded").and_then(Value::as_bool) != Some(true) {
        anyhow::bail!("Runtime Host did not record managed launch outcome for {session_id}");
    }
    Ok(())
}

fn truncate(value: &str, max_chars: usize) -> String {
    value.chars().take(max_chars).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn response(session_id: &str, run_id: &str) -> ManagedLaunchResponse {
        ManagedLaunchResponse {
            session_id: session_id.to_string(),
            run_id: run_id.to_string(),
            provider_session_id: None,
            permission_mode: None,
            hook_token: None,
            managed_transport: None,
            coordination_token: None,
        }
    }

    #[test]
    fn launch_outcome_wire_names_are_stable() {
        assert_eq!(LaunchOutcome::Confirmed.as_wire(), "confirmed");
        assert_eq!(LaunchOutcome::Aborted.as_wire(), "aborted");
    }

    #[test]
    fn error_text_is_bounded_by_characters() {
        assert_eq!(truncate("aéb", 2), "aé");
    }

    #[test]
    fn registration_requires_shared_launch_identities() {
        assert!(validate_launch_identity(response("session", ""), "Codex", None).is_err());
        assert!(validate_launch_identity(response("", "run"), "Codex", None).is_err());
        assert!(validate_launch_identity(
            response("unexpected", "run"),
            "Cursor",
            Some("expected")
        )
        .is_err());
        assert!(
            validate_launch_identity(response("expected", "run"), "Cursor", Some("expected"))
                .is_ok()
        );
    }

    /// Serve every launch-outcome POST with `status`, reporting each request body.
    fn spawn_outcome_server(status: &'static str) -> (String, std::sync::mpsc::Receiver<String>) {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let (sender, receiver) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let mut buffer = [0_u8; 8192];
                let Ok(size) = stream.read(&mut buffer) else {
                    continue;
                };
                if sender
                    .send(String::from_utf8_lossy(&buffer[..size]).to_string())
                    .is_err()
                {
                    return;
                }
                let body = r#"{"recorded":true,"detail":"Launch outcomes require catalogd"}"#;
                let _ = stream.write_all(
                    format!(
                        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    )
                    .as_bytes(),
                );
            }
        });
        (format!("http://{address}"), receiver)
    }

    fn outcome_receipt(agent_dir: &std::path::Path, session_id: &str) -> std::path::PathBuf {
        agent_dir
            .join("managed-local")
            .join("outcome-retries")
            .join(format!("{session_id}.json"))
    }

    #[test]
    fn failed_confirmation_degrades_instead_of_unspawning_a_running_provider() {
        let (url, requests) = spawn_outcome_server("503 Service Unavailable");
        let agent_dir = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let notices = DeferredNotices::default();
        {
            let mut transaction =
                ManagedLaunchTransaction::new(&runtime, &url, "device-token", "session-1", "run-1");
            transaction.confirm_or_degrade("Claude", agent_dir.path(), &notices);
        }

        let confirm = requests.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(confirm.contains(r#""outcome":"confirmed""#));
        // The provider really started, so dropping an unconfirmed transaction
        // must never report a launch abort. The background retry replays the
        // same confirmation immediately -- a detached launch exits too fast for
        // a delayed first attempt to reach anything -- so assert on what those
        // replays SAY rather than on their absence.
        while let Ok(replay) = requests.recv_timeout(Duration::from_millis(300)) {
            assert!(
                replay.contains(r#""outcome":"confirmed""#),
                "degraded launch replayed something other than its confirmation: {replay}"
            );
        }

        let payload: Value = serde_json::from_slice(
            &std::fs::read(outcome_receipt(agent_dir.path(), "session-1")).unwrap(),
        )
        .unwrap();
        assert_eq!(payload["recovery_exhausted"], json!(false));
        assert_eq!(payload["stage"], json!("confirmation"));
        assert_eq!(payload["coordination_state"], json!("available"));
        assert!(notices
            .drain()
            .iter()
            .any(|message| message.contains("could not record the launch outcome")));
    }

    #[test]
    fn recorded_confirmation_leaves_no_degradation_receipt() {
        let (url, requests) = spawn_outcome_server("200 OK");
        let agent_dir = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let notices = DeferredNotices::default();
        {
            let mut transaction =
                ManagedLaunchTransaction::new(&runtime, &url, "device-token", "session-2", "run-2");
            transaction.confirm_or_degrade("Claude", agent_dir.path(), &notices);
        }

        assert!(requests
            .recv_timeout(Duration::from_secs(5))
            .unwrap()
            .contains(r#""outcome":"confirmed""#));
        assert!(requests.recv_timeout(Duration::from_millis(250)).is_err());
        assert!(!outcome_receipt(agent_dir.path(), "session-2").exists());
        assert!(notices.drain().is_empty());
    }

    #[test]
    fn authority_requires_expected_transport_and_nonempty_token() {
        let mut response = response("session", "run");
        response.managed_transport = Some("codex_app_server".to_string());
        response.coordination_token = Some("  secret  ".to_string());
        assert_eq!(
            response
                .require_authority("Codex", "codex_app_server")
                .unwrap(),
            "secret"
        );
        assert!(response
            .require_authority("Codex", "claude_channel_bridge")
            .is_err());
        response.coordination_token = Some("  ".to_string());
        assert!(response
            .require_authority("Codex", "codex_app_server")
            .is_err());
    }
}

/// Buffered user-facing notices produced by a background thread while a
/// managed provider TUI owns the shared terminal. Drained and printed only
/// after the child exits and the terminal is restored, so raw text never lands
/// inside the provider's alternate screen.
#[derive(Clone, Default)]
pub struct DeferredNotices(Arc<Mutex<Vec<String>>>);

impl DeferredNotices {
    pub fn push(&self, message: String) {
        if let Ok(mut guard) = self.0.lock() {
            guard.push(message);
        }
    }

    pub fn drain(&self) -> Vec<String> {
        if let Ok(mut guard) = self.0.lock() {
            std::mem::take(&mut *guard)
        } else {
            Vec::new()
        }
    }
}

pub struct ManagedRegistrationRetry {
    pub provider_alive: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
    // Read only by `abandon`, which the detached launch paths in the facade
    // binary use. Cursor's launcher lives in the engine binary and always has
    // an attached terminal, so it never abandons.
    #[allow(dead_code)]
    agent_dir: PathBuf,
    #[allow(dead_code)]
    session_id: String,
    #[allow(dead_code)]
    provider: String,
}

impl ManagedRegistrationRetry {
    /// Give up on recovery from the launcher side. A detached launch returns
    /// immediately and takes the retry thread with it, so the receipt must be
    /// settled here rather than left reporting a recovery that is not running.
    #[allow(dead_code)]
    pub fn abandon(&self) {
        self.cancel.store(true, Ordering::Release);
        let _ = record_registration_retry(
            &self.agent_dir,
            &self.session_id,
            &self.provider,
            true,
        );
    }
}

impl Drop for ManagedRegistrationRetry {
    fn drop(&mut self) {
        self.cancel.store(true, Ordering::Release);
    }
}

/// Recover a managed launch whose first bounded registration attempt failed.
///
/// The provider is already running locally and owns the terminal, so this must
/// never block the caller and never print directly: notices are buffered until
/// the provider exits. `provider` is the user-facing label used in those
/// notices, so every managed provider reports its own name.
pub fn spawn_managed_registration_retry(
    url: &str,
    token: &str,
    provider: &str,
    payload: serde_json::Value,
    session_id: &str,
    notices: DeferredNotices,
    agent_dir: PathBuf,
) -> ManagedRegistrationRetry {
    let url = url.to_string();
    let token = token.to_string();
    let provider = provider.to_string();
    let session_id = session_id.to_string();
    let provider_alive = Arc::new(AtomicBool::new(false));
    let cancel = Arc::new(AtomicBool::new(false));
    let provider_alive_for_thread = Arc::clone(&provider_alive);
    let cancel_for_thread = Arc::clone(&cancel);
    let retry_agent_dir = agent_dir.clone();
    let retry_session_id = session_id.clone();
    let retry_provider = provider.clone();
    // Publish the degraded state before the provider takes over the terminal.
    // Local health runs in a different process, so it can only see this launch
    // as degraded through a durable receipt; process liveness is not proof that
    // control exists.
    if let Err(error) =
        record_registration_retry(
            &agent_dir,
            &session_id,
            &provider,
            false,
        )
    {
        notices.push(format!(
            "Longhouse warning: could not record {provider} launch recovery state: {error:#}"
        ));
    }
    std::thread::spawn(move || {
        // Every terminal path below must settle the receipt: cleared when
        // control is genuinely established, exhausted otherwise. Leaving it
        // active would report recovery that is no longer running.
        let settle_exhausted = |session_id: &str, provider: &str| {
            let _ = record_registration_retry(
                &agent_dir, session_id, provider, true,
            );
        };
        for attempt in 0..5 {
            if cancel_for_thread.load(Ordering::Acquire) {
                settle_exhausted(&session_id, &provider);
                return;
            }
            let Ok(runtime) = tokio::runtime::Runtime::new() else {
                settle_exhausted(&session_id, &provider);
                return;
            };
            match register_managed_launch_with_timeout(
                &runtime,
                &url,
                &token,
                &format!("{provider} degraded"),
                &payload,
                Some(&session_id),
                Duration::from_secs(2),
            ) {
                Ok(response) => {
                    if response.provider_session_id.as_deref()
                        != payload.get("provider_session_id").and_then(|value| value.as_str())
                    {
                        notices.push(format!(
                            "Longhouse warning: degraded {provider} registration returned a different provider identity"
                        ));
                        let _transaction = ManagedLaunchTransaction::new(
                            &runtime,
                            &url,
                            &token,
                            &response.session_id,
                            &response.run_id,
                        );
                        settle_exhausted(&session_id, &provider);
                    } else {
                        let mut transaction = ManagedLaunchTransaction::new(
                            &runtime,
                            &url,
                            &token,
                            &response.session_id,
                            &response.run_id,
                        );
                        for _ in 0..100 {
                            if cancel_for_thread.load(Ordering::Acquire) {
                                settle_exhausted(&session_id, &provider);
                                return;
                            }
                            if provider_alive_for_thread.load(Ordering::Acquire) {
                                match transaction.confirm() {
                                    Ok(_) => clear_registration_retry(&agent_dir, &session_id),
                                    Err(error) => {
                                        notices.push(format!(
                                            "Longhouse warning: recovered {provider} registration could not be confirmed: {error:#}"
                                        ));
                                        settle_exhausted(&session_id, &provider);
                                    }
                                }
                                return;
                            }
                            std::thread::sleep(Duration::from_millis(100));
                        }
                        notices.push(format!(
                            "Longhouse warning: {provider} exited before recovered registration became ready"
                        ));
                        settle_exhausted(&session_id, &provider);
                    }
                    return;
                }
                Err(error) if attempt < 4 => {
                    // Surface the outage before sleeping so the transient notice
                    // reads in order. The final attempt's error is the most
                    // recent and is reported by the failure arm below.
                    if attempt == 0 {
                        notices.push(format!(
                            "Longhouse warning: Runtime Host is still unavailable; {provider} is running locally and registration will retry"
                        ));
                    }
                    let _ = error;
                    for _ in 0..2_u64.pow(attempt) {
                        if cancel_for_thread.load(Ordering::Acquire) {
                            return;
                        }
                        std::thread::sleep(Duration::from_secs(1));
                    }
                }
                Err(error) => {
                    notices.push(format!(
                        "Longhouse warning: could not recover {provider} Helm registration: {error:#}"
                    ));
                    settle_exhausted(&session_id, &provider);
                }
            }
        }
    });
    ManagedRegistrationRetry {
        provider_alive,
        cancel,
        agent_dir: retry_agent_dir,
        session_id: retry_session_id,
        provider: retry_provider,
    }
}
