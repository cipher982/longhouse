//! Runtime Host side of the managed Helm launch transaction.
//!
//! Registration creates a durable `pending` launch. The provider launcher must
//! then either confirm that its provider/control driver reached ready, or abort
//! with the startup error. A provider exit after confirmation is ordinary Helm
//! lifecycle, not a launch abort.

use anyhow::Context;
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;

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

/// A bounded owner-local recovery loop for an initially unreachable Runtime
/// Host. It never owns the provider process and is cancelled when the launch
/// wrapper exits. Successful registration confirms the same run identity; it
/// does not silently create a second session.
pub struct ManagedLaunchRegistrationRetry {
    pub provider_alive: Arc<AtomicBool>,
    cancel: Arc<AtomicBool>,
}

impl Drop for ManagedLaunchRegistrationRetry {
    fn drop(&mut self) {
        self.cancel.store(true, Ordering::Release);
    }
}

pub fn spawn_managed_launch_registration_retry(
    url: &str,
    token: &str,
    provider_name: &str,
    payload: Value,
    expected_session_id: &str,
    expected_transport: &str,
) -> ManagedLaunchRegistrationRetry {
    let url = url.to_owned();
    let token = token.to_owned();
    let provider_name = provider_name.to_owned();
    let expected_session_id = expected_session_id.to_owned();
    let expected_transport = expected_transport.to_owned();
    let provider_alive = Arc::new(AtomicBool::new(false));
    let cancel = Arc::new(AtomicBool::new(false));
    let provider_alive_for_thread = Arc::clone(&provider_alive);
    let cancel_for_thread = Arc::clone(&cancel);
    thread::spawn(move || {
        let mut delay = std::time::Duration::from_secs(1);
        for attempt in 0..16 {
            if cancel_for_thread.load(Ordering::Acquire) {
                return;
            }
            let Ok(runtime) = tokio::runtime::Runtime::new() else {
                return;
            };
            match register_managed_launch_with_timeout(
                &runtime,
                &url,
                &token,
                &provider_name,
                &payload,
                Some(&expected_session_id),
                std::time::Duration::from_secs(2),
            ) {
                Ok(response) => {
                    if let Err(error) =
                        response.validate_transport(&provider_name, &expected_transport)
                    {
                        eprintln!(
                            "Longhouse warning: recovered {provider_name} registration returned an invalid transport: {error:#}"
                        );
                        return;
                    }
                    if let Some(expected_provider_session_id) = payload
                        .get("provider_session_id")
                        .and_then(Value::as_str)
                        .filter(|value| !value.trim().is_empty())
                    {
                        if response.provider_session_id.as_deref()
                            != Some(expected_provider_session_id)
                        {
                            eprintln!(
                                "Longhouse warning: recovered {provider_name} registration returned a different provider identity"
                            );
                            return;
                        }
                    }
                    let mut transaction = ManagedLaunchTransaction::new(
                        &runtime,
                        &url,
                        &token,
                        &response.session_id,
                        &response.run_id,
                    );
                    while !cancel_for_thread.load(Ordering::Acquire)
                        && !provider_alive_for_thread.load(Ordering::Acquire)
                    {
                        thread::sleep(std::time::Duration::from_millis(100));
                    }
                    if cancel_for_thread.load(Ordering::Acquire) {
                        return;
                    }
                    if let Err(error) = transaction.confirm() {
                        eprintln!(
                            "Longhouse warning: recovered {provider_name} registration could not be confirmed: {error:#}"
                        );
                    }
                    return;
                }
                Err(error) if attempt < 15 => {
                    if attempt == 0 {
                        eprintln!(
                            "Longhouse warning: Runtime Host is still unavailable; {provider_name} is running locally and registration will retry"
                        );
                    }
                    let deadline = std::time::Instant::now() + delay;
                    while !cancel_for_thread.load(Ordering::Acquire)
                        && std::time::Instant::now() < deadline
                    {
                        thread::sleep(std::time::Duration::from_millis(100));
                    }
                    delay =
                        std::cmp::min(delay.saturating_mul(2), std::time::Duration::from_secs(60));
                    let _ = error;
                }
                Err(error) => {
                    eprintln!(
                        "Longhouse warning: could not recover {provider_name} Helm registration within the retry budget: {error:#}"
                    );
                }
            }
        }
    });
    ManagedLaunchRegistrationRetry {
        provider_alive,
        cancel,
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
        let response = ManagedLaunchResponse::degraded_from_payload(
            &payload,
            "Codex",
            "codex_app_server",
        )
        .unwrap();
        assert_eq!(response.session_id, payload["session_id"]);
        assert_eq!(
            response.provider_session_id.as_deref(),
            payload["provider_session_id"].as_str()
        );
        assert_eq!(response.permission_mode.as_deref(), Some("provider_local"));
        assert_eq!(response.managed_transport.as_deref(), Some("codex_app_server"));
        assert!(!response.run_id.is_empty());
    }
}
