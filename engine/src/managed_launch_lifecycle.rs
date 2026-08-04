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
use std::io::Write;
use std::path::PathBuf;
use std::process::Command;
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
const OUTCOME_RETRY_DIRECTORY: &str = "managed-local/outcome-retries";
const RETRY_MAX_ATTEMPTS: u32 = 288;
const RETRY_TTL: ChronoDuration = ChronoDuration::hours(24);
const PRE_READY_TTL: ChronoDuration = ChronoDuration::minutes(10);
const EXHAUSTED_RETENTION: ChronoDuration = ChronoDuration::days(7);

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
    #[serde(default)]
    recovery_exhausted: bool,
    #[serde(default)]
    exhausted_at: Option<String>,
    #[serde(default)]
    provider_pid: Option<u32>,
    #[serde(default)]
    provider_process_start_time: Option<String>,
    #[serde(default)]
    launcher_pid: Option<u32>,
    #[serde(default)]
    launcher_process_start_time: Option<String>,
    #[serde(default)]
    provider_exited: bool,
}

/// A durable owner-local receipt for the second half of a managed launch.
/// Registration and outcome confirmation are separate network operations, so
/// a successful local provider start must retain the exact run identity until
/// the Runtime Host has recorded that the provider actually reached ready.
#[derive(Clone, Debug, Deserialize, Serialize)]
struct ManagedLaunchOutcomeRetryIntent {
    schema_version: u32,
    url: String,
    token: String,
    session_id: String,
    run_id: String,
    attempts: u32,
    next_attempt_at: Option<String>,
    last_error: Option<String>,
    created_at: String,
    #[serde(default)]
    recovery_exhausted: bool,
    #[serde(default)]
    exhausted_at: Option<String>,
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
    keep_intent: Arc<AtomicBool>,
    intent_path: PathBuf,
}

impl Drop for ManagedLaunchRegistrationRetry {
    fn drop(&mut self) {
        // Claude clones this handle into the post-fork readiness callback. Do
        // not let that short-lived clone delete the durable intent while the
        // foreground owner still has a chance to mark the provider ready.
        if Arc::strong_count(&self.state) == 1
            && !self.state.provider_alive.load(Ordering::Acquire)
            && !self.state.keep_intent.load(Ordering::Acquire)
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
            intent.provider_exited = false;
            intent.next_attempt_at = None;
        }) {
            eprintln!("Longhouse warning: could not persist managed launch readiness: {error:#}");
        }
    }

    /// Mark a launch that failed before readiness. If an owner was already
    /// recorded, retain a post-hoc registration so a host outage cannot hide
    /// the launch; otherwise remove the unready intent immediately.
    pub fn mark_provider_failed(&self) {
        if self.state.provider_alive.load(Ordering::Acquire) {
            self.mark_provider_exited();
            return;
        }
        self.state.keep_intent.store(false, Ordering::Release);
        let _ = fs::remove_file(&self.state.intent_path);
    }

    /// Retain a ready launch after the foreground wrapper exits. The provider
    /// may have completed before the Runtime Host returned; post-hoc
    /// registration still preserves the session's durable identity and raw
    /// evidence. The retry lane expires rather than resurrecting it forever.
    pub fn mark_provider_exited(&self) {
        if !self.state.provider_alive.swap(false, Ordering::AcqRel) {
            self.state.keep_intent.store(false, Ordering::Release);
            let _ = fs::remove_file(&self.state.intent_path);
            return;
        }
        self.state.keep_intent.store(true, Ordering::Release);
        if let Err(error) = update_retry_intent(&self.state.intent_path, |intent| {
            intent.provider_ready = true;
            intent.provider_exited = true;
            intent.next_attempt_at = None;
        }) {
            eprintln!("Longhouse warning: could not retain managed launch registration: {error:#}");
        }
    }

    /// Record the long-lived provider-owned process for detached Helm. The
    /// daemon can then reject a retry after PID reuse or provider exit.
    pub fn record_provider_owner(&self, pid: u32, process_start_time: Option<String>) {
        if let Err(error) = update_retry_intent(&self.state.intent_path, |intent| {
            intent.provider_pid = Some(pid);
            intent.provider_process_start_time = process_start_time;
        }) {
            eprintln!("Longhouse warning: could not persist managed provider ownership: {error:#}");
        }
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
        recovery_exhausted: false,
        exhausted_at: None,
        provider_pid: None,
        provider_process_start_time: None,
        launcher_pid: Some(std::process::id()),
        launcher_process_start_time: process_start_identity(std::process::id()),
        provider_exited: false,
    };
    let intent_path = persist_retry_intent(&intent)?;
    let provider_alive = Arc::new(AtomicBool::new(false));
    Ok(ManagedLaunchRegistrationRetry {
        state: Arc::new(ManagedLaunchRetryState {
            provider_alive,
            keep_intent: Arc::new(AtomicBool::new(false)),
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
        let url = self.url.to_owned();
        let device_token = self.device_token.to_owned();
        let session_id = self.session_id.to_owned();
        let run_id = self.run_id.to_owned();
        let path = match persist_outcome_retry_intent(&ManagedLaunchOutcomeRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            url,
            token: device_token,
            session_id,
            run_id,
            attempts: 0,
            next_attempt_at: None,
            last_error: None,
            created_at: Utc::now().to_rfc3339(),
            recovery_exhausted: false,
            exhausted_at: None,
        }) {
            Ok(path) => path,
            Err(error) => {
                eprintln!(
                    "Longhouse warning: could not persist managed launch outcome receipt: {error:#}"
                );
                return;
            }
        };
        // The Drop implementation is the synchronous abort fallback. Keep
        // the transaction unconfirmed until the owner-local retry obligation
        // is durable; otherwise a persistence failure can silently leave the
        // Runtime Host launch pending with no recovery evidence.
        self.confirmed = true;
        thread::spawn(move || {
            let Ok(runtime) = tokio::runtime::Runtime::new() else {
                return;
            };
            if let Err(error) = runtime.block_on(reconcile_managed_launch_outcome_retry_at(&path)) {
                eprintln!(
                    "Longhouse warning: managed launch outcome receipt could not be confirmed: {error:#}"
                );
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

fn outcome_retry_directory() -> anyhow::Result<PathBuf> {
    Ok(longhouse_home()?
        .join("agent")
        .join(OUTCOME_RETRY_DIRECTORY))
}

fn longhouse_home() -> anyhow::Result<PathBuf> {
    if let Some(home) = std::env::var_os("LONGHOUSE_HOME") {
        return Ok(PathBuf::from(home));
    }
    Ok(PathBuf::from(std::env::var("HOME").context("HOME not set")?).join(".longhouse"))
}

fn retry_intent_path(session_id: &str) -> anyhow::Result<PathBuf> {
    if session_id.trim().is_empty()
        || !session_id
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
    {
        anyhow::bail!("managed launch retry has an unsafe session identity");
    }
    Ok(retry_directory()?.join(format!("{session_id}.json")))
}

fn persist_retry_intent(intent: &ManagedLaunchRetryIntent) -> anyhow::Result<PathBuf> {
    let path = retry_intent_path(&intent.expected_session_id)?;
    persist_retry_intent_at(&path, intent)
}

fn outcome_retry_intent_path(session_id: &str, run_id: &str) -> anyhow::Result<PathBuf> {
    for (label, value) in [("session", session_id), ("run", run_id)] {
        if value.trim().is_empty()
            || !value.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '-' | '_')
            })
        {
            anyhow::bail!("managed launch outcome has an unsafe {label} identity");
        }
    }
    Ok(outcome_retry_directory()?.join(format!("{session_id}-{run_id}.json")))
}

fn persist_outcome_retry_intent(
    intent: &ManagedLaunchOutcomeRetryIntent,
) -> anyhow::Result<PathBuf> {
    let path = outcome_retry_intent_path(&intent.session_id, &intent.run_id)?;
    persist_outcome_retry_intent_at(&path, intent)
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
    set_private_directory_permissions(directory)?;
    let bytes = serde_json::to_vec_pretty(intent).context("encode managed launch retry intent")?;
    let temporary = directory.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    write_private_file(&temporary, &bytes)
        .with_context(|| format!("write managed launch retry intent {}", temporary.display()))?;
    fs::rename(&temporary, &path)
        .with_context(|| format!("publish managed launch retry intent {}", path.display()))?;
    set_private_file_permissions(&path)?;
    Ok(path.to_path_buf())
}

fn persist_outcome_retry_intent_at(
    path: &std::path::Path,
    intent: &ManagedLaunchOutcomeRetryIntent,
) -> anyhow::Result<PathBuf> {
    let directory = path
        .parent()
        .context("managed launch outcome path has no parent")?;
    fs::create_dir_all(directory).with_context(|| {
        format!(
            "create managed launch outcome retry directory {}",
            directory.display()
        )
    })?;
    set_private_directory_permissions(directory)?;
    let bytes = serde_json::to_vec_pretty(intent).context("encode managed launch outcome")?;
    let temporary = directory.join(format!(".{}.tmp", uuid::Uuid::new_v4()));
    write_private_file(&temporary, &bytes).with_context(|| {
        format!(
            "write managed launch outcome intent {}",
            temporary.display()
        )
    })?;
    fs::rename(&temporary, path)
        .with_context(|| format!("publish managed launch outcome intent {}", path.display()))?;
    set_private_file_permissions(path)?;
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

fn set_private_directory_permissions(path: &std::path::Path) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

fn write_private_file(path: &std::path::Path, bytes: &[u8]) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)?;
        file.write_all(bytes)?;
        file.sync_all()?;
    }
    #[cfg(not(unix))]
    {
        fs::write(path, bytes)?;
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

fn merge_current_retry_owner_state(
    path: &std::path::Path,
    intent: &mut ManagedLaunchRetryIntent,
) -> anyhow::Result<()> {
    let bytes = fs::read(path).with_context(|| format!("read {}", path.display()))?;
    let current: ManagedLaunchRetryIntent =
        serde_json::from_slice(&bytes).context("decode current managed launch retry intent")?;
    // Foreground provider callbacks may update these fields while the daemon
    // is awaiting Runtime Host registration. They are monotonic owner facts;
    // merge them into the daemon's snapshot before it can report an outcome,
    // rather than publishing a stale copy over a provider-exit update.
    intent.provider_ready |= current.provider_ready;
    intent.provider_exited |= current.provider_exited;
    if current.provider_pid.is_some() {
        intent.provider_pid = current.provider_pid;
        intent.provider_process_start_time = current.provider_process_start_time;
    }
    if current.launcher_pid.is_some() {
        intent.launcher_pid = current.launcher_pid;
        intent.launcher_process_start_time = current.launcher_process_start_time;
    }
    Ok(())
}

fn persist_retry_intent_with_owner_merge(
    path: &std::path::Path,
    intent: &mut ManagedLaunchRetryIntent,
) -> anyhow::Result<()> {
    merge_current_retry_owner_state(path, intent)?;
    let published = persist_retry_intent_at(path, intent)?;
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
fn retry_delay(attempts: u32, jitter_key: &str) -> ChronoDuration {
    let base_seconds = 2_i64.saturating_pow(attempts.min(8)).min(300);
    let mut hash = 0xcbf29ce484222325_u64;
    for byte in jitter_key.as_bytes() {
        hash = hash.wrapping_mul(0x100000001b3_u64);
        hash ^= u64::from(*byte);
    }
    let jitter_window = (base_seconds / 4).max(1);
    let jitter = (hash % (jitter_window as u64 + 1)) as i64;
    ChronoDuration::seconds(base_seconds + jitter)
}

fn retry_exhausted(intent: &ManagedLaunchRetryIntent, now: DateTime<Utc>) -> bool {
    if intent.recovery_exhausted || intent.attempts >= RETRY_MAX_ATTEMPTS {
        return true;
    }
    DateTime::parse_from_rfc3339(&intent.created_at)
        .map(|created| created.with_timezone(&Utc) + RETRY_TTL <= now)
        .unwrap_or(true)
}

pub fn process_start_identity(pid: u32) -> Option<String> {
    let output = Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "lstart="])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!value.is_empty()).then_some(value)
}

fn process_exists(pid: u32) -> Option<bool> {
    let pid = i32::try_from(pid).ok().filter(|pid| *pid > 0)?;
    let result = unsafe { libc::kill(pid, 0) };
    if result == 0 {
        return Some(true);
    }
    match std::io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Some(false),
        // Permission to inspect a process is not required to establish that
        // the PID is occupied. Start-time comparison below still decides
        // whether this is the recorded owner.
        Some(libc::EPERM) => Some(true),
        _ => None,
    }
}

fn process_is_zombie(pid: u32) -> Option<bool> {
    let output = Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "stat="])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let state = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!state.is_empty()).then_some(state.starts_with('Z'))
}

fn provider_owner_alive(intent: &ManagedLaunchRetryIntent) -> Option<bool> {
    owner_identity_alive(
        intent.provider_pid,
        intent.provider_process_start_time.as_deref(),
    )
}

fn launcher_owner_alive(intent: &ManagedLaunchRetryIntent) -> Option<bool> {
    owner_identity_alive(
        intent.launcher_pid,
        intent.launcher_process_start_time.as_deref(),
    )
}

fn owner_identity_alive(pid: Option<u32>, recorded_start: Option<&str>) -> Option<bool> {
    let Some(pid) = pid else {
        // Missing owner identity is an unknown observation, not proof that
        // the provider exited. Treating it as dead would exhaust a launch
        // before the provider registration had a chance to record its PID.
        return None;
    };
    let Some(recorded_start) = recorded_start
        .map(str::trim)
        .filter(|value| !value.is_empty())
    else {
        return None;
    };
    match process_exists(pid) {
        Some(false) => return Some(false),
        Some(true) => {
            if process_is_zombie(pid) == Some(true) {
                return Some(false);
            }
        }
        None => return None,
    }
    let Some(current) = process_start_identity(pid) else {
        // The PID exists, but the platform identity probe failed. Do not
        // delete a retry intent on an unsupported or permission-limited ps.
        return None;
    };
    Some(current == recorded_start)
}

fn pre_ready_expired(intent: &ManagedLaunchRetryIntent, now: DateTime<Utc>) -> bool {
    DateTime::parse_from_rfc3339(&intent.created_at)
        .map(|created| created.with_timezone(&Utc) + PRE_READY_TTL <= now)
        .unwrap_or(true)
}

fn exhaust_retry_intent(intent: &mut ManagedLaunchRetryIntent, reason: impl Into<String>) {
    intent.recovery_exhausted = true;
    intent.exhausted_at = Some(Utc::now().to_rfc3339());
    intent.last_error = Some(reason.into());
    intent.next_attempt_at = None;
    intent.token.clear();
}

fn outcome_retry_exhausted(intent: &ManagedLaunchOutcomeRetryIntent, now: DateTime<Utc>) -> bool {
    if intent.recovery_exhausted || intent.attempts >= RETRY_MAX_ATTEMPTS {
        return true;
    }
    DateTime::parse_from_rfc3339(&intent.created_at)
        .map(|created| created.with_timezone(&Utc) + RETRY_TTL <= now)
        .unwrap_or(true)
}

fn exhaust_outcome_retry_intent(
    intent: &mut ManagedLaunchOutcomeRetryIntent,
    reason: impl Into<String>,
) {
    intent.recovery_exhausted = true;
    intent.exhausted_at = Some(Utc::now().to_rfc3339());
    intent.last_error = Some(reason.into());
    intent.next_attempt_at = None;
    intent.token.clear();
}

fn exhausted_retention_expired(exhausted_at: Option<&str>, now: DateTime<Utc>) -> bool {
    exhausted_at
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Utc) + EXHAUSTED_RETENTION <= now)
        .unwrap_or(false)
}

fn remove_if_present(path: &std::path::Path) -> anyhow::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error).with_context(|| format!("remove {}", path.display())),
    }
}

async fn reconcile_managed_launch_outcome_retry_at(path: &std::path::Path) -> anyhow::Result<bool> {
    let bytes = match fs::read(path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(false),
        Err(error) => return Err(error).with_context(|| format!("read {}", path.display())),
    };
    let mut intent: ManagedLaunchOutcomeRetryIntent =
        serde_json::from_slice(&bytes).context("decode managed launch outcome intent")?;
    if intent.schema_version != RETRY_SCHEMA_VERSION {
        anyhow::bail!("unsupported managed launch outcome intent schema");
    }
    let now = Utc::now();
    if intent.recovery_exhausted {
        if intent.exhausted_at.is_none() {
            intent.exhausted_at = Some(now.to_rfc3339());
            persist_outcome_retry_intent_at(path, &intent)?;
        } else if exhausted_retention_expired(intent.exhausted_at.as_deref(), now) {
            remove_if_present(path)?;
        }
        return Ok(false);
    }
    if outcome_retry_exhausted(&intent, now) {
        exhaust_outcome_retry_intent(
            &mut intent,
            "automatic managed launch outcome recovery exhausted; inspect the managed session",
        );
        persist_outcome_retry_intent_at(path, &intent)?;
        tracing::warn!(path = %path.display(), action_id = "inspect_managed_session", "Managed launch outcome recovery exhausted");
        return Ok(false);
    }
    if !retry_due_outcome(&intent, now) {
        return Ok(false);
    }
    match report_launch_outcome(
        &intent.url,
        &intent.token,
        &intent.session_id,
        &intent.run_id,
        LaunchOutcome::Confirmed,
        None,
    )
    .await
    {
        Ok(()) => {
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => {
                    return Err(error).with_context(|| {
                        format!("remove resolved managed launch outcome {}", path.display())
                    });
                }
            }
            Ok(true)
        }
        Err(error) => {
            intent.attempts = intent.attempts.saturating_add(1);
            intent.last_error = Some(truncate(&format!("{error:#}"), 1000));
            intent.next_attempt_at = Some(
                (Utc::now()
                    + retry_delay(
                        intent.attempts,
                        &format!("{}:{}", intent.session_id, intent.run_id),
                    ))
                .to_rfc3339(),
            );
            persist_outcome_retry_intent_at(path, &intent)?;
            Ok(false)
        }
    }
}

fn retry_due_outcome(intent: &ManagedLaunchOutcomeRetryIntent, now: DateTime<Utc>) -> bool {
    intent
        .next_attempt_at
        .as_deref()
        .and_then(|value| DateTime::parse_from_rfc3339(value).ok())
        .map(|value| value.with_timezone(&Utc) <= now)
        .unwrap_or(true)
}

/// Resume durable managed-launch outcome receipts owned by the local Machine
/// Agent. The result is the number of receipts confirmed and removed.
#[allow(dead_code)] // Used by the Machine Agent daemon binary.
pub async fn reconcile_managed_launch_outcome_retries() -> anyhow::Result<usize> {
    let directory = outcome_retry_directory()?;
    let entries = match fs::read_dir(&directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
        Err(error) => return Err(error).with_context(|| format!("read {}", directory.display())),
    };
    let mut resolved = 0usize;
    for entry in entries {
        let path = match entry {
            Ok(entry) => entry.path(),
            Err(error) => {
                tracing::warn!(error = %error, "Could not enumerate managed launch outcome retry intent");
                continue;
            }
        };
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        match reconcile_managed_launch_outcome_retry_at(&path).await {
            Ok(true) => resolved = resolved.saturating_add(1),
            Ok(false) => {}
            Err(error) => {
                tracing::warn!(path = %path.display(), error = %error, "Could not reconcile managed launch outcome retry intent");
            }
        }
    }
    Ok(resolved)
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
        let path = match entry {
            Ok(entry) => entry.path(),
            Err(error) => {
                tracing::warn!(error = %error, "Could not enumerate managed launch retry intent");
                continue;
            }
        };
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
        let now = Utc::now();
        if intent.recovery_exhausted {
            if intent.exhausted_at.is_none() {
                intent.exhausted_at = Some(now.to_rfc3339());
                if let Err(error) = persist_retry_intent_at(&path, &intent) {
                    tracing::warn!(path = %path.display(), error = %error, "Could not timestamp exhausted managed launch retry intent");
                }
            } else if exhausted_retention_expired(intent.exhausted_at.as_deref(), now) {
                if let Err(error) = remove_if_present(&path) {
                    tracing::warn!(path = %path.display(), error = %error, "Could not remove expired managed launch retry intent");
                }
            }
            continue;
        }
        if !intent.provider_ready {
            if let Err(error) = merge_current_retry_owner_state(&path, &mut intent) {
                tracing::warn!(path = %path.display(), error = %error, "Could not refresh managed launch owner state before pre-ready reconciliation");
                continue;
            }
            // A readiness callback may have landed after the initial read.
            // Re-enter the ready path on the next scan instead of expiring or
            // deleting the now-ready intent from the stale snapshot.
            if intent.provider_ready {
                continue;
            }
            match launcher_owner_alive(&intent) {
                Some(true) if pre_ready_expired(&intent, now) => {
                    exhaust_retry_intent(
                        &mut intent,
                        "managed launch did not become ready before its recovery window expired",
                    );
                    if let Err(error) = persist_retry_intent_with_owner_merge(&path, &mut intent) {
                        tracing::warn!(path = %path.display(), error = %error, "Could not persist expired pre-ready managed launch retry intent");
                    }
                    tracing::warn!(path = %path.display(), action_id = "inspect_managed_session", "Managed launch stayed pre-ready until its recovery window expired");
                    continue;
                }
                Some(true) => continue,
                Some(false) => {
                    tracing::warn!(path = %path.display(), action_id = "inspect_managed_session", "Removing managed launch intent whose launcher exited before readiness");
                    let _ = remove_if_present(&path);
                    continue;
                }
                None if pre_ready_expired(&intent, now) => {
                    tracing::warn!(path = %path.display(), action_id = "inspect_managed_session", "Removing stale managed launch intent with no readiness owner identity");
                    let _ = remove_if_present(&path);
                    continue;
                }
                None => continue,
            }
        }
        if retry_exhausted(&intent, now) {
            exhaust_retry_intent(
                &mut intent,
                "automatic managed launch registration recovery exhausted; inspect the managed session",
            );
            if let Err(error) = persist_retry_intent_with_owner_merge(&path, &mut intent) {
                tracing::warn!(path = %path.display(), error = %error, "Could not persist exhausted managed launch retry intent");
            }
            tracing::warn!(path = %path.display(), action_id = "inspect_managed_session", "Managed launch registration recovery exhausted");
            continue;
        }
        if !retry_due(&intent, now) {
            continue;
        }
        if !intent.provider_exited {
            match provider_owner_alive(&intent) {
                Some(true) => {}
                Some(false) => {
                    // The wrapper may have been killed after local readiness
                    // but before it could persist `provider_exited`. Register
                    // once and report an explicit post-hoc abort so the host
                    // does not turn this ordinary owner loss into a red,
                    // exhausted recovery incident.
                    if let Err(error) = merge_current_retry_owner_state(&path, &mut intent) {
                        tracing::warn!(path = %path.display(), error = %error, "Could not refresh managed launch owner state before post-hoc abort");
                        continue;
                    }
                    if intent.provider_exited {
                        continue;
                    }
                    intent.provider_exited = true;
                    intent.last_error = Some(
                        "managed provider owner is no longer live; recording post-hoc launch abort"
                            .into(),
                    );
                    let _ = persist_retry_intent_with_owner_merge(&path, &mut intent);
                    tracing::warn!(
                        path = %path.display(),
                        action_id = "inspect_managed_session",
                        "Managed provider owner exited before launch recovery completed; recording post-hoc abort"
                    );
                }
                None => {
                    // The owner identity is unverifiable, not evidence that
                    // the provider exited. Register once; later lifecycle
                    // evidence will settle the session without resurrecting
                    // an unknown process.
                }
            }
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
                intent.next_attempt_at = Some(
                    (Utc::now() + retry_delay(intent.attempts, &intent.expected_session_id))
                        .to_rfc3339(),
                );
                if let Err(update_error) = persist_retry_intent_with_owner_merge(&path, &mut intent)
                {
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
            let _ = persist_retry_intent_with_owner_merge(&path, &mut intent);
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
                let _ = persist_retry_intent_with_owner_merge(&path, &mut intent);
                tracing::warn!(path = %path.display(), "Managed launch retry returned a different provider identity");
                continue;
            }
        }
        if let Err(error) = merge_current_retry_owner_state(&path, &mut intent) {
            intent.attempts = intent.attempts.saturating_add(1);
            intent.last_error = Some(truncate(&format!("{error:#}"), 1000));
            intent.next_attempt_at = Some(
                (Utc::now() + retry_delay(intent.attempts, &intent.expected_session_id))
                    .to_rfc3339(),
            );
            let _ = persist_retry_intent_with_owner_merge(&path, &mut intent);
            tracing::warn!(path = %path.display(), error = %error, "Could not refresh managed launch owner state before outcome");
            continue;
        }
        let provider_exited_during_registration = if intent.provider_exited {
            false
        } else {
            match provider_owner_alive(&intent) {
                Some(true) => false,
                Some(false) => true,
                None => {
                    intent.attempts = intent.attempts.saturating_add(1);
                    intent.last_error = Some(
                        "managed provider owner identity is unavailable; launch outcome deferred"
                            .into(),
                    );
                    intent.next_attempt_at = Some(
                        (Utc::now() + retry_delay(intent.attempts, &intent.expected_session_id))
                            .to_rfc3339(),
                    );
                    let _ = persist_retry_intent_with_owner_merge(&path, &mut intent);
                    tracing::warn!(path = %path.display(), action_id = "inspect_managed_session", "Managed provider owner identity unavailable; deferring launch outcome");
                    continue;
                }
            }
        };
        let post_hoc_exit = intent.provider_exited || provider_exited_during_registration;
        let post_hoc_error = post_hoc_exit.then(|| {
            anyhow::anyhow!(
                "provider exited before Runtime Host confirmed managed launch readiness"
            )
        });
        if let Err(error) = report_launch_outcome(
            &intent.url,
            &intent.token,
            &response.session_id,
            &response.run_id,
            if post_hoc_exit {
                LaunchOutcome::Aborted
            } else {
                LaunchOutcome::Confirmed
            },
            post_hoc_error.as_ref(),
        )
        .await
        {
            intent.attempts = intent.attempts.saturating_add(1);
            intent.last_error = Some(truncate(&format!("{error:#}"), 1000));
            intent.next_attempt_at = Some(
                (Utc::now() + retry_delay(intent.attempts, &intent.expected_session_id))
                    .to_rfc3339(),
            );
            let _ = persist_retry_intent_with_owner_merge(&path, &mut intent);
            continue;
        }
        if let Err(error) = remove_if_present(&path) {
            tracing::warn!(path = %path.display(), error = %error, "Could not remove resolved managed launch retry intent");
            continue;
        }
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
        assert_eq!(
            response.run_id,
            uuid::Uuid::new_v5(
                &uuid::Uuid::NAMESPACE_URL,
                b"longhouse:managed-local-run:11111111-1111-4111-8111-111111111111",
            )
            .to_string()
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
            recovery_exhausted: false,
            exhausted_at: None,
            provider_pid: None,
            provider_process_start_time: None,
            launcher_pid: None,
            launcher_process_start_time: None,
            provider_exited: false,
        };

        assert_eq!(persist_retry_intent_at(&path, &intent).unwrap(), path);
        update_retry_intent(&path, |stored| stored.provider_ready = true).unwrap();

        let stored: ManagedLaunchRetryIntent =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert!(stored.provider_ready);
        update_retry_intent(&path, |stored| stored.provider_exited = true).unwrap();
        let mut stale = intent;
        merge_current_retry_owner_state(&path, &mut stale).unwrap();
        assert!(stale.provider_ready);
        assert!(stale.provider_exited);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn pre_ready_retry_expires_even_when_launcher_is_still_alive() {
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
            recovery_exhausted: false,
            exhausted_at: None,
            provider_pid: None,
            provider_process_start_time: None,
            provider_exited: false,
            launcher_pid: Some(std::process::id()),
            launcher_process_start_time: process_start_identity(std::process::id()),
        };

        assert_eq!(launcher_owner_alive(&intent), Some(true));
        assert!(pre_ready_expired(&intent, Utc::now()));
    }

    #[test]
    fn missing_launcher_process_is_dead_not_unknown() {
        let mut child = Command::new("true").spawn().unwrap();
        let pid = child.id();
        child.wait().unwrap();
        assert_eq!(
            owner_identity_alive(Some(pid), Some("missing-process")),
            Some(false),
        );
    }

    #[test]
    fn invalid_launcher_pid_is_unknown() {
        assert_eq!(
            owner_identity_alive(Some(u32::MAX), Some("invalid-pid")),
            None
        );
    }

    #[test]
    fn retry_intent_identity_does_not_sanitize_collisions() {
        assert!(retry_intent_path("session/a").is_err());
        assert!(retry_intent_path("session_a").is_ok());
    }

    #[test]
    fn retry_intent_expires_by_attempts_or_age() {
        let mut intent = ManagedLaunchRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            provider_name: "Codex".to_string(),
            url: "http://127.0.0.1:1".to_string(),
            token: "device-token".to_string(),
            payload: serde_json::json!({"session_id": "session-1"}),
            expected_session_id: "session-1".to_string(),
            expected_transport: "codex_app_server".to_string(),
            provider_ready: true,
            attempts: RETRY_MAX_ATTEMPTS,
            next_attempt_at: None,
            last_error: None,
            created_at: Utc::now().to_rfc3339(),
            recovery_exhausted: false,
            exhausted_at: None,
            provider_pid: None,
            provider_process_start_time: None,
            launcher_pid: None,
            launcher_process_start_time: None,
            provider_exited: false,
        };
        assert!(retry_exhausted(&intent, Utc::now()));
        intent.attempts = 0;
        intent.created_at = (Utc::now() - RETRY_TTL - ChronoDuration::seconds(1)).to_rfc3339();
        assert!(retry_exhausted(&intent, Utc::now()));
    }

    #[test]
    fn retry_delay_is_bounded_and_deterministically_jittered() {
        let first = retry_delay(8, "session-a");
        let repeat = retry_delay(8, "session-a");
        let other = retry_delay(8, "session-b");
        let minimum = ChronoDuration::seconds(256);
        let maximum = ChronoDuration::seconds(320);

        assert_eq!(first, repeat);
        assert!(first >= minimum);
        assert!(first <= maximum);
        assert!(other <= maximum);
        assert!(retry_delay(1000, "session-a") <= maximum);
    }

    #[test]
    fn exhausted_retry_retention_has_a_separate_cleanup_window() {
        let now = Utc::now();
        let retained = (now - EXHAUSTED_RETENTION + ChronoDuration::seconds(1)).to_rfc3339();
        let expired = (now - EXHAUSTED_RETENTION - ChronoDuration::seconds(1)).to_rfc3339();

        assert!(!exhausted_retention_expired(Some(&retained), now));
        assert!(exhausted_retention_expired(Some(&expired), now));
        assert!(!exhausted_retention_expired(None, now));
    }

    #[test]
    fn exhausted_intents_drop_device_tokens_before_retention() {
        let mut registration = ManagedLaunchRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            provider_name: "Codex".to_string(),
            url: "http://127.0.0.1:1".to_string(),
            token: "device-token".to_string(),
            payload: serde_json::json!({"session_id": "session-1"}),
            expected_session_id: "session-1".to_string(),
            expected_transport: "codex_app_server".to_string(),
            provider_ready: true,
            attempts: 0,
            next_attempt_at: None,
            last_error: None,
            created_at: Utc::now().to_rfc3339(),
            recovery_exhausted: false,
            exhausted_at: None,
            provider_pid: None,
            provider_process_start_time: None,
            launcher_pid: None,
            launcher_process_start_time: None,
            provider_exited: false,
        };
        exhaust_retry_intent(&mut registration, "test");
        assert!(registration.token.is_empty());

        let mut outcome = ManagedLaunchOutcomeRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            url: "http://127.0.0.1:1".to_string(),
            token: "device-token".to_string(),
            session_id: "session-1".to_string(),
            run_id: "run-1".to_string(),
            attempts: 0,
            next_attempt_at: None,
            last_error: None,
            created_at: Utc::now().to_rfc3339(),
            recovery_exhausted: false,
            exhausted_at: None,
        };
        exhaust_outcome_retry_intent(&mut outcome, "test");
        assert!(outcome.token.is_empty());
    }

    #[test]
    fn durable_outcome_intent_preserves_exact_run_identity_and_private_permissions() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("managed-local").join("outcome.json");
        let intent = ManagedLaunchOutcomeRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            url: "http://127.0.0.1:1".to_string(),
            token: "device-token".to_string(),
            session_id: "session-1".to_string(),
            run_id: "run-1".to_string(),
            attempts: 0,
            next_attempt_at: None,
            last_error: None,
            created_at: Utc::now().to_rfc3339(),
            recovery_exhausted: false,
            exhausted_at: None,
        };
        persist_outcome_retry_intent_at(&path, &intent).unwrap();
        let stored: ManagedLaunchOutcomeRetryIntent =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(stored.session_id, "session-1");
        assert_eq!(stored.run_id, "run-1");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                std::fs::metadata(path.parent().unwrap())
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
            assert_eq!(
                std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn outcome_retry_expires_by_attempts_or_age() {
        let mut intent = ManagedLaunchOutcomeRetryIntent {
            schema_version: RETRY_SCHEMA_VERSION,
            url: "http://127.0.0.1:1".to_string(),
            token: "device-token".to_string(),
            session_id: "session-1".to_string(),
            run_id: "run-1".to_string(),
            attempts: RETRY_MAX_ATTEMPTS,
            next_attempt_at: None,
            last_error: None,
            created_at: Utc::now().to_rfc3339(),
            recovery_exhausted: false,
            exhausted_at: None,
        };
        assert!(outcome_retry_exhausted(&intent, Utc::now()));
        intent.attempts = 0;
        intent.created_at = (Utc::now() - RETRY_TTL - ChronoDuration::seconds(1)).to_rfc3339();
        assert!(outcome_retry_exhausted(&intent, Utc::now()));
    }
}
