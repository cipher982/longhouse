//! Runtime Host side of the managed Helm launch transaction.
//!
//! Registration creates a durable `pending` launch. The provider launcher must
//! then either confirm that its provider/control driver reached ready, or abort
//! with the startup error. A provider exit after confirmation is ordinary Helm
//! lifecycle, not a launch abort.

use anyhow::Context;
use chrono::{DateTime, Duration as ChronoDuration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;

#[allow(dead_code)] // Used by the provider facade binary.
const REGISTRATION_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

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
    /// Build the local side of a degraded Helm launch. The provider owns the
    /// first process locally; registration can converge later using the same
    /// client-minted identities.
    pub fn degraded_from_payload(
        payload: &Value,
        provider_name: &str,
        expected_transport: &str,
    ) -> anyhow::Result<Self> {
        let session_id = payload
            .get("session_id")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .with_context(|| format!("degraded {provider_name} launch has no session identity"))?;
        let run_id = uuid::Uuid::new_v5(
            &uuid::Uuid::NAMESPACE_URL,
            format!("longhouse:managed-local-run:{session_id}").as_bytes(),
        )
        .to_string();
        Ok(Self {
            session_id: session_id.to_owned(),
            run_id,
            provider_session_id: payload
                .get("provider_session_id")
                .and_then(Value::as_str)
                .map(str::to_owned),
            permission_mode: payload
                .get("permission_mode")
                .and_then(Value::as_str)
                .map(str::to_owned),
            hook_token: None,
            managed_transport: Some(expected_transport.to_owned()),
            coordination_token: None,
        })
    }

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

const RETRY_SCHEMA_VERSION: u32 = 1;
const RETRY_DIRECTORY: &str = "managed-local/registration-retries";

/// A durable owner-local recovery intent for an initially unreachable Runtime
/// Host. It never owns the provider process and only confirms the exact
/// client-minted session after the launcher records local provider readiness.
/// The device token is kept in a 0600 file because a detached launch may outlive
/// the process that originally received an explicit token override.
#[derive(Clone, Debug, Deserialize, Serialize)]
struct ManagedLaunchRetryIntent {
    schema_version: u32,
    provider_name: String,
    url: String,
    token: String,
    payload: Value,
    expected_session_id: String,
    expected_transport: String,
    provider_ready: bool,
    attempts: u32,
    next_attempt_at: Option<String>,
    last_error: Option<String>,
    created_at: String,
}

/// A durable owner-local recovery handle for a managed launch. The in-memory
/// flag remains useful while a foreground launcher is alive, while the intent
/// file lets the daemon continue the same retry after that launcher exits.
#[derive(Clone)]
pub struct ManagedLaunchRegistrationRetry {
    state: Arc<ManagedLaunchRetryState>,
}

struct ManagedLaunchRetryState {
    provider_alive: Arc<AtomicBool>,
    intent_path: PathBuf,
}

impl Drop for ManagedLaunchRegistrationRetry {
    fn drop(&mut self) {
        // Claude clones this handle into the post-fork readiness callback. Do
        // not let that short-lived clone delete the durable intent while the
        // foreground owner still has a chance to mark the provider ready.
        if Arc::strong_count(&self.state) == 1 && !self.state.provider_alive.load(Ordering::Acquire)
        {
            let _ = fs::remove_file(&self.state.intent_path);
        }
    }
}

impl ManagedLaunchRegistrationRetry {
    pub fn mark_provider_ready(&self) {
        self.state.provider_alive.store(true, Ordering::Release);
        if let Err(error) = update_retry_intent(&self.state.intent_path, |intent| {
            intent.provider_ready = true;
            intent.next_attempt_at = None;
        }) {
            eprintln!("Longhouse warning: could not persist managed launch readiness: {error:#}");
        }
    }

    pub fn mark_provider_failed(&self) {
        self.state.provider_alive.store(false, Ordering::Release);
        let _ = fs::remove_file(&self.state.intent_path);
    }
}

pub fn spawn_managed_launch_registration_retry(
    url: &str,
    token: &str,
    provider_name: &str,
    payload: Value,
    expected_session_id: &str,
    expected_transport: &str,
) -> anyhow::Result<ManagedLaunchRegistrationRetry> {
    let intent = ManagedLaunchRetryIntent {
        schema_version: RETRY_SCHEMA_VERSION,
        provider_name: provider_name.to_owned(),
        url: url.to_owned(),
        token: token.to_owned(),
        payload,
        expected_session_id: expected_session_id.to_owned(),
        expected_transport: expected_transport.to_owned(),
        provider_ready: false,
        attempts: 0,
        next_attempt_at: None,
        last_error: None,
        created_at: Utc::now().to_rfc3339(),
    };
    let intent_path = persist_retry_intent(&intent)?;
    let provider_alive = Arc::new(AtomicBool::new(false));
    Ok(ManagedLaunchRegistrationRetry {
        state: Arc::new(ManagedLaunchRetryState {
            provider_alive,
            intent_path,
        }),
    })
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

    /// Confirm without making provider terminal ownership wait on the
    /// Runtime Host. The launch is already locally usable; the receipt is a
    /// remote convergence detail and is retried in a detached worker.
    pub fn confirm_in_background(&mut self) {
        if self.confirmed {
            return;
        }
        self.confirmed = true;
        let url = self.url.to_owned();
        let device_token = self.device_token.to_owned();
        let session_id = self.session_id.to_owned();
        let run_id = self.run_id.to_owned();
        thread::spawn(move || {
            let mut delay = std::time::Duration::from_millis(250);
            for attempt in 0..4 {
                let Ok(runtime) = tokio::runtime::Runtime::new() else {
                    return;
                };
                match runtime.block_on(report_launch_outcome(
                    &url,
                    &device_token,
                    &session_id,
                    &run_id,
                    LaunchOutcome::Confirmed,
                    None,
                )) {
                    Ok(()) => return,
                    Err(error) if attempt < 3 => {
                        thread::sleep(delay);
                        delay = std::cmp::min(
                            delay.saturating_mul(2),
                            std::time::Duration::from_secs(2),
                        );
                        let _ = error;
                    }
                    Err(error) => {
                        eprintln!(
                            "Longhouse warning: managed launch receipt could not be confirmed after local startup: {error:#}"
                        );
                    }
                }
            }
        });
    }
}

/// Register one managed launch with the Runtime Host.
///
/// Every provider gets the same deadline, status/body error, response decode,
/// and launch-identity validation. Provider drivers validate only their own
/// transport-specific fields after this returns.
#[allow(dead_code)] // Used by the provider facade binary.
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
    runtime.block_on(register_managed_launch_async(
        url,
        device_token,
        provider_name,
        payload,
        expected_session_id,
        timeout,
    ))
}

async fn register_managed_launch_async(
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
    let response = serde_json::from_str::<ManagedLaunchResponse>(&body)
        .with_context(|| format!("decode managed {provider_name} registration response"))?;
    validate_launch_identity(response, provider_name, expected_session_id)
}

fn retry_directory() -> anyhow::Result<PathBuf> {
    Ok(longhouse_home()?.join("agent").join(RETRY_DIRECTORY))
}

fn longhouse_home() -> anyhow::Result<PathBuf> {
    if let Some(home) = std::env::var_os("LONGHOUSE_HOME") {
        return Ok(PathBuf::from(home));
    }
    Ok(PathBuf::from(std::env::var("HOME").context("HOME not set")?).join(".longhouse"))
}

fn retry_intent_path(session_id: &str) -> anyhow::Result<PathBuf> {
    let safe_session_id = session_id
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
                character
            } else {
                '_'
            }
        })
        .collect::<String>();
    if safe_session_id.is_empty() {
        anyhow::bail!("managed launch retry has no usable session identity");
    }
    Ok(retry_directory()?.join(format!("{}.json", safe_session_id)))
}

fn persist_retry_intent(intent: &ManagedLaunchRetryIntent) -> anyhow::Result<PathBuf> {
    let path = retry_intent_path(&intent.expected_session_id)?;
    persist_retry_intent_at(&path, intent)
}

fn persist_retry_intent_at(
    path: &std::path::Path,
    intent: &ManagedLaunchRetryIntent,
) -> anyhow::Result<PathBuf> {
    let directory = path
        .parent()
        .context("managed launch retry path has no parent")?;
    fs::create_dir_all(directory).with_context(|| {
        format!(
            "create managed launch retry directory {}",
            directory.display()
        )
    })?;
    let bytes = serde_json::to_vec_pretty(intent).context("encode managed launch retry intent")?;
    let temporary = directory.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    fs::write(&temporary, bytes)
        .with_context(|| format!("write managed launch retry intent {}", temporary.display()))?;
    set_private_file_permissions(&temporary)?;
    fs::rename(&temporary, &path)
        .with_context(|| format!("publish managed launch retry intent {}", path.display()))?;
    set_private_file_permissions(&path)?;
    Ok(path.to_path_buf())
}

fn set_private_file_permissions(path: &std::path::Path) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
    }
    Ok(())
}

fn update_retry_intent<F>(path: &std::path::Path, update: F) -> anyhow::Result<()>
where
    F: FnOnce(&mut ManagedLaunchRetryIntent),
{
    let bytes = fs::read(path).with_context(|| format!("read {}", path.display()))?;
    let mut intent: ManagedLaunchRetryIntent =
        serde_json::from_slice(&bytes).context("decode managed launch retry intent")?;
    update(&mut intent);
    let published = persist_retry_intent_at(path, &intent)?;
    if published != path {
        anyhow::bail!("managed launch retry intent path changed unexpectedly");
    }
    Ok(())
}

#[allow(dead_code)] // Used by the Machine Agent daemon binary.
fn retry_due(intent: &ManagedLaunchRetryIntent, now: DateTime<Utc>) -> bool {
    intent
        .next_attempt_at
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Utc) <= now)
        .unwrap_or(true)
}

#[allow(dead_code)] // Used by the Machine Agent daemon binary.
fn retry_delay(attempts: u32) -> ChronoDuration {
    let seconds = 2_i64.saturating_pow(attempts.min(5)).min(60);
    ChronoDuration::seconds(seconds)
}

/// Resume durable managed-launch registration intents owned by the local
/// Machine Agent. A provider-ready intent is safe to retry because it carries
/// the original client-minted session identity; a response with another
/// provider identity or transport is rejected and retained for inspection.
#[allow(dead_code)] // The provider facade and Machine Agent are separate binaries.
pub async fn reconcile_managed_launch_retries() -> anyhow::Result<usize> {
    let directory = retry_directory()?;
    let entries = match fs::read_dir(&directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error).with_context(|| format!("read {}", directory.display())),
    };
    let mut resolved = 0usize;
    for entry in entries {
        let path = entry?.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let bytes = match fs::read(&path) {
            Ok(bytes) => bytes,
            Err(error) => {
                tracing::warn!(path = %path.display(), error = %error, "Could not read managed launch retry intent");
                continue;
            }
        };
        let mut intent: ManagedLaunchRetryIntent = match serde_json::from_slice::<
            ManagedLaunchRetryIntent,
        >(&bytes)
        {
            Ok(intent) if intent.schema_version == RETRY_SCHEMA_VERSION => intent,
            Ok(_) => {
                tracing::warn!(path = %path.display(), "Ignoring unsupported managed launch retry intent schema");
                continue;
            }
            Err(error) => {
                tracing::warn!(path = %path.display(), error = %error, "Could not decode managed launch retry intent");
                continue;
            }
        };
        if !intent.provider_ready || !retry_due(&intent, Utc::now()) {
            continue;
        }
        let response = match register_managed_launch_async(
            &intent.url,
            &intent.token,
            &intent.provider_name,
            &intent.payload,
            Some(&intent.expected_session_id),
            std::time::Duration::from_secs(2),
        )
        .await
        {
            Ok(response) => response,
            Err(error) => {
                intent.attempts = intent.attempts.saturating_add(1);
                intent.last_error = Some(truncate(&format!("{error:#}"), 1000));
                intent.next_attempt_at =
                    Some((Utc::now() + retry_delay(intent.attempts)).to_rfc3339());
                if let Err(update_error) = persist_retry_intent(&intent) {
                    tracing::warn!(path = %path.display(), error = %update_error, "Could not persist managed launch retry backoff");
                }
                continue;
            }
        };
        if let Err(error) =
            response.validate_transport(&intent.provider_name, &intent.expected_transport)
        {
            intent.last_error = Some(truncate(&format!("{error:#}"), 1000));
            intent.next_attempt_at = Some((Utc::now() + ChronoDuration::minutes(1)).to_rfc3339());
            let _ = persist_retry_intent(&intent);
            tracing::warn!(path = %path.display(), error = %error, "Managed launch retry returned an invalid transport");
            continue;
        }
        if let Some(expected_provider_session_id) = intent
            .payload
            .get("provider_session_id")
            .and_then(Value::as_str)
            .filter(|value: &&str| !value.trim().is_empty())
        {
            if response.provider_session_id.as_deref() != Some(expected_provider_session_id) {
                intent.last_error =
                    Some("Runtime Host returned a different provider identity".into());
                intent.next_attempt_at =
                    Some((Utc::now() + ChronoDuration::minutes(1)).to_rfc3339());
                let _ = persist_retry_intent(&intent);
                tracing::warn!(path = %path.display(), "Managed launch retry returned a different provider identity");
                continue;
            }
        }
        if let Err(error) = report_launch_outcome(
            &intent.url,
            &intent.token,
            &response.session_id,
            &response.run_id,
            LaunchOutcome::Confirmed,
            None,
        )
        .await
        {
            intent.attempts = intent.attempts.saturating_add(1);
            intent.last_error = Some(truncate(&format!("{error:#}"), 1000));
            intent.next_attempt_at = Some((Utc::now() + retry_delay(intent.attempts)).to_rfc3339());
            let _ = persist_retry_intent(&intent);
            continue;
        }
        fs::remove_file(&path)
            .with_context(|| format!("remove resolved managed launch retry {}", path.display()))?;
        resolved = resolved.saturating_add(1);
        tracing::info!(provider = %intent.provider_name, session_id = %intent.expected_session_id, "Recovered managed launch registration");
    }
    Ok(resolved)
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

    #[test]
    fn degraded_response_preserves_client_minted_identity() {
        let payload = serde_json::json!({
            "session_id": "11111111-1111-4111-8111-111111111111",
            "provider_session_id": "22222222-2222-4222-8222-222222222222",
            "permission_mode": "provider_local"
        });
        let response =
            ManagedLaunchResponse::degraded_from_payload(&payload, "Codex", "codex_app_server")
                .unwrap();
        assert_eq!(response.session_id, payload["session_id"]);
        assert_eq!(
            response.provider_session_id.as_deref(),
            payload["provider_session_id"].as_str()
        );
        assert_eq!(response.permission_mode.as_deref(), Some("provider_local"));
        assert_eq!(
            response.managed_transport.as_deref(),
            Some("codex_app_server")
        );
        assert!(!response.run_id.is_empty());
    }

    #[test]
    fn durable_retry_intent_preserves_readiness_and_private_permissions() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("managed-local").join("retry.json");
        let intent = ManagedLaunchRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            provider_name: "Codex".to_string(),
            url: "http://127.0.0.1:1".to_string(),
            token: "device-token".to_string(),
            payload: serde_json::json!({"session_id": "session-1"}),
            expected_session_id: "session-1".to_string(),
            expected_transport: "codex_app_server".to_string(),
            provider_ready: false,
            attempts: 0,
            next_attempt_at: None,
            last_error: None,
            created_at: "2026-08-03T00:00:00Z".to_string(),
        };

        assert_eq!(persist_retry_intent_at(&path, &intent).unwrap(), path);
        update_retry_intent(&path, |stored| stored.provider_ready = true).unwrap();

        let stored: ManagedLaunchRetryIntent =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert!(stored.provider_ready);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }
}
