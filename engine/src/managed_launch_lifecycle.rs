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

/// What registering a launch is actually allowed to cost
/// (`server/zerg/catalogd/client.py::MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS`).
///
/// This is the budget that decides the outcome. catalogd abandons the call at
/// this bound, so the route fails regardless of what the HTTP layer permits and
/// a client deadline beyond it only waits longer for a failure that already
/// happened. The 45s in `request_timeout.py` is a runaway-request ceiling, not
/// a statement about how long this work takes.
///
/// It is emphatically not the expected latency either. The product budget for
/// this database is 250ms p95 with a 1s alert threshold
/// (`control-plane/docs/specs/speed-of-light-database.md`), and the 10s here
/// exists to cover cold schema and first-write costs after a Runtime Host
/// restart. Steady-state seconds on this route are a defect to investigate, not
/// a number to design deadlines around.
pub const MANAGED_LAUNCH_CATALOG_BUDGET: Duration = Duration::from_secs(10);

/// Margin over the catalogd budget for transport and the host's own overhead.
/// Small on purpose: it covers the hop, not a slow database.
const REGISTRATION_TRANSPORT_MARGIN: Duration = Duration::from_secs(2);

/// Deadline for the attempt the user waits on before the provider TUI starts.
///
/// Short because a human is watching, but abandoning is not free: the Runtime
/// Host cannot cancel a synchronous catalogd call when its client walks away
/// (see `catalogd/server.py`, which documents the same trap for reads), so a
/// timed-out attempt keeps occupying the single writer. Shortening this does
/// not reduce load, it converts one slow launch into queued work that the next
/// attempt has to wait behind. Degrading is safe only because recovery
/// converges the same client-minted identity afterwards.
pub const FOREGROUND_REGISTRATION_TIMEOUT: Duration = Duration::from_secs(2);

/// Deadline for background recovery attempts.
///
/// Nothing waits on this thread, so it can afford the operation's full budget —
/// and no more. Waiting past `MANAGED_LAUNCH_CATALOG_BUDGET` cannot succeed,
/// because catalogd has already given up by then; it would only leave another
/// abandoned write on the queue.
pub const RECOVERY_REGISTRATION_TIMEOUT: Duration =
    MANAGED_LAUNCH_CATALOG_BUDGET.saturating_add(REGISTRATION_TRANSPORT_MARGIN);

/// Longest gap between recovery attempts. Recovery runs for the life of the
/// provider, so it backs off to a slow steady poll rather than giving up.
const RECOVERY_MAX_BACKOFF: Duration = Duration::from_secs(60);

/// How long a recovered registration waits for the provider to reach ready
/// before treating the launch as one that never started. Bounded only so a
/// leaked handle cannot wait forever — `cancel` is the signal that actually
/// ends this wait, and a bridge that is merely slow to come up must not be
/// mistaken for one that failed.
const PROVIDER_READY_WAIT_CAP: Duration = Duration::from_secs(300);

/// Safety stop for a recovery thread whose cancel signal never arrives. At the
/// backoff cap this is several hours, far beyond any real launch; it exists so a
/// leaked thread cannot poll forever, not as a recovery policy.
const RECOVERY_MAX_ATTEMPTS: u32 = 240;

/// Gap before retrying after `attempt` consecutive failures.
///
/// Doubling, then flat at the cap. Pure so the policy can be asserted directly:
/// the failure mode being guarded against is a schedule that looks like a retry
/// loop but expires inside the first slow write on this route.
fn recovery_backoff(attempt: u32) -> Duration {
    Duration::from_secs(1u64 << attempt.min(6)).min(RECOVERY_MAX_BACKOFF)
}

/// Age past which a settled recovery receipt describes a launch nobody can act
/// on. Local health already ignores receipts for sessions this machine no longer
/// sees, so these are pure litter; pruning keeps the directory readable when
/// someone is debugging a live degradation.
const RETRY_RECEIPT_RETENTION: chrono::Duration = chrono::Duration::days(14);

// Facade-only: the engine binary reaches the Runtime Host through the bounded
// entry point, so these are dead there but live in `longhouse`.
//
// Used by the blocking resume path, which has no degraded fallback: a failure
// here fails the user's resume outright, so it must be at least as patient as
// the host's own budget or it fails resumes the host would have served. The
// wait that patience buys is bounded elsewhere: `CONNECT_TIMEOUT` keeps a dead
// or partitioned host from consuming the full budget in silence.
#[allow(dead_code)]
const REGISTRATION_TIMEOUT: Duration = RECOVERY_REGISTRATION_TIMEOUT;

/// What the Runtime Host allows itself for `POST
/// /api/agents/sessions/{id}/launch-outcome`, which takes no override and so
/// gets `DEFAULT_TIMEOUT_SECONDS` (`request_timeout.py`).
///
/// Same lesson as registration, same single-writer route: a 5s client deadline
/// against a 15s server budget abandoned confirmations a healthy-but-queued
/// host was still committing, leaving a running session with an unrecorded
/// outcome.
const LAUNCH_OUTCOME_SERVER_BUDGET: Duration = Duration::from_secs(15);

/// How long to spend establishing a connection, independent of the overall
/// deadline.
///
/// The two answer different questions. A total deadline sized to the host's
/// work budget is right for a host that accepted the request and is queued
/// behind the single writer; it is wrong for a blackholed link, where without
/// this the blocking resume path would sit silent for the full budget. Connect
/// refused and DNS failures already return early — a partitioned network is the
/// case that needs its own bound.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Client used for every Runtime Host call in this module, so the connect bound
/// cannot be applied to some call sites and forgotten at others.
fn runtime_host_client() -> reqwest::Client {
    reqwest::Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .build()
        .unwrap_or_else(|_| reqwest::Client::new())
}

/// Why a registration attempt failed, in the terms the user needs.
///
/// "Unavailable" and "did not answer in time" are different facts about the
/// host and lead to different actions. Collapsing them let a queueing delay
/// report itself as an outage.
pub fn registration_failure_summary(error: &anyhow::Error, deadline: Duration) -> String {
    for cause in error.chain() {
        if let Some(request_error) = cause.downcast_ref::<reqwest::Error>() {
            if request_error.is_timeout() {
                return format!(
                    "Runtime Host did not answer within {}s",
                    deadline.as_secs()
                );
            }
            if request_error.is_connect() {
                return "Runtime Host is unreachable".to_string();
            }
        }
    }
    // The host can also time the request out on its own side and answer 503
    // with this detail (`server/zerg/middleware/request_timeout.py`). That is
    // the same fact as a client-side timeout — the host was up and working —
    // so it must not fall through to the generic arm and read as a rejection.
    let text = format!("{error:#}");
    if text.contains("Request timed out") {
        return "Runtime Host timed out its own request".to_string();
    }
    format!("registration failed ({text})")
}

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
    if let Some(directory) = path.parent() {
        prune_stale_retry_receipts(directory);
    }
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
    if let Some(directory) = path.parent() {
        prune_stale_retry_receipts(directory);
    }
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

/// Delete receipts old enough that no recovery could still act on them.
///
/// Only age decides. A receipt for a session that merely looks gone is not safe
/// to remove — the same trap `collect_managed_launch_recovery` documents, where
/// a failed provider scan makes every live session look absent — but a receipt
/// from two weeks ago describes a launch that ended regardless of what the
/// scanner can see today. Best effort throughout: losing this cleanup is
/// cosmetic, and a receipt we cannot parse is left for a human to look at.
fn prune_stale_retry_receipts(directory: &std::path::Path) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    let cutoff = chrono::Utc::now() - RETRY_RECEIPT_RETENTION;
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|value| value.to_str()) != Some("json") {
            continue;
        }
        let created_at = std::fs::read_to_string(&path)
            .ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .and_then(|payload| {
                payload
                    .get("created_at")
                    .and_then(Value::as_str)
                    .and_then(|value| chrono::DateTime::parse_from_rfc3339(value).ok())
            });
        if let Some(created_at) = created_at {
            if created_at.with_timezone(&chrono::Utc) < cutoff {
                let _ = std::fs::remove_file(&path);
            }
        }
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
        let response = runtime_host_client()
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
    let response = runtime_host_client()
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
        .timeout(LAUNCH_OUTCOME_SERVER_BUDGET)
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

    /// Pin the client deadline to the budget that actually decides the outcome.
    ///
    /// Bounded on both sides deliberately. Too short abandons work the host is
    /// still doing — and cannot cancel — which is how a queued write became a
    /// reported outage. Too long is the opposite failure and the more insidious
    /// one: it cannot succeed, since catalogd has already given up, and it lets
    /// a database regression hide behind a patient client instead of surfacing.
    #[test]
    fn registration_deadline_tracks_the_catalogd_launch_budget() {
        let source = std::fs::read_to_string(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../server/zerg/catalogd/client.py"),
        )
        .expect("read the catalogd client budgets");
        let declared = source
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once('=')?;
                (name.trim() == "MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS")
                    .then(|| value.trim().parse::<f64>().ok())
                    .flatten()
            })
            .expect("find MANAGED_LAUNCH_CATALOG_TIMEOUT_SECONDS");
        assert_eq!(
            MANAGED_LAUNCH_CATALOG_BUDGET.as_secs_f64(),
            declared,
            "engine budget drifted from what catalogd gives this call"
        );
        assert!(
            RECOVERY_REGISTRATION_TIMEOUT > MANAGED_LAUNCH_CATALOG_BUDGET,
            "the client must outlast catalogd or it abandons a decided call"
        );
        assert!(
            RECOVERY_REGISTRATION_TIMEOUT <= MANAGED_LAUNCH_CATALOG_BUDGET * 2,
            "a deadline far past catalogd's own bound absorbs a slow database \
             instead of reporting one; fix the write, do not widen the wait"
        );
        // The foreground attempt is the one a human is watching.
        assert!(FOREGROUND_REGISTRATION_TIMEOUT < RECOVERY_REGISTRATION_TIMEOUT);
    }

    /// Recovery has to outlive the thing it is recovering from. The schedule it
    /// replaced ran five attempts inside about fifteen seconds — less than a
    /// single slow write on this route — so any host that came back a minute
    /// later found nothing still trying.
    #[test]
    fn recovery_keeps_trying_for_the_life_of_a_session() {
        assert!(recovery_backoff(0) <= Duration::from_secs(1), "react fast to a blip");
        assert!(recovery_backoff(1) > recovery_backoff(0), "back off under sustained failure");
        assert_eq!(
            recovery_backoff(u32::MAX),
            RECOVERY_MAX_BACKOFF,
            "a long outage must settle into a slow poll, not an ever-growing gap"
        );

        let covered: Duration = (0..RECOVERY_MAX_ATTEMPTS).map(recovery_backoff).sum();
        assert!(
            covered >= Duration::from_secs(60 * 60),
            "recovery spans {covered:?}, far short of a working session"
        );
    }

    /// A recovered registration whose confirmation fails must degrade, not
    /// abort. Aborting posts `aborted` for a provider that is running right
    /// now — the launcher's own `confirm_or_degrade` exists precisely because
    /// an unrecorded outcome is a far weaker problem than a false abort.
    #[test]
    fn a_failed_confirmation_degrades_instead_of_aborting_a_live_provider() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let (sender, requests) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let mut buffer = [0_u8; 8192];
                let Ok(size) = stream.read(&mut buffer) else {
                    continue;
                };
                let request = String::from_utf8_lossy(&buffer[..size]).to_string();
                // Registration works; every launch-outcome POST is rejected.
                let (status, body) = if request.contains("managed-local/this-device") {
                    (
                        "200 OK",
                        json!({"session_id": "session-unconfirmed", "run_id": "run-unconfirmed"})
                            .to_string(),
                    )
                } else {
                    ("503 Service Unavailable", r#"{"detail":"writer busy"}"#.to_string())
                };
                let _ = sender.send(request);
                let _ = stream.write_all(
                    format!(
                        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    )
                    .as_bytes(),
                );
            }
        });

        let agent_dir = tempfile::tempdir().unwrap();
        let registration_receipt = agent_dir
            .path()
            .join("managed-local")
            .join("registration-retries")
            .join("session-unconfirmed.json");
        {
            let retry = spawn_managed_registration_retry(
                &format!("http://{address}"),
                "device-token",
                "Codex",
                json!({"session_id": "session-unconfirmed"}),
                "session-unconfirmed",
                DeferredNotices::default(),
                agent_dir.path().to_path_buf(),
            );
            retry.provider_alive.store(true, Ordering::Release);

            let deadline = std::time::Instant::now() + Duration::from_secs(20);
            while registration_receipt.exists() && std::time::Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(100));
            }
            assert!(
                !registration_receipt.exists(),
                "registration itself succeeded, so its receipt must be cleared"
            );
        }

        let mut outcomes = Vec::new();
        while let Ok(request) = requests.recv_timeout(Duration::from_millis(500)) {
            if request.contains("launch-outcome") {
                outcomes.push(request);
            }
        }
        assert!(!outcomes.is_empty(), "confirmation was never attempted");
        for outcome in &outcomes {
            assert!(
                !outcome.contains(r#""outcome":"aborted""#),
                "a running provider was reported as an aborted launch: {outcome}"
            );
        }
        assert!(
            outcome_receipt(agent_dir.path(), "session-unconfirmed").exists(),
            "an unconfirmed outcome must leave its own recovery receipt"
        );
    }

    /// A launcher that goes away mid-attempt must not leave a receipt claiming
    /// a recovery is still running. The thread is detached, so nothing else
    /// will ever correct it.
    #[test]
    fn dropping_the_handle_settles_a_recovery_still_in_flight() {
        // Nothing listens here, so every attempt fails and the thread stays in
        // its retry loop rather than reaching a terminal state on its own.
        let agent_dir = tempfile::tempdir().unwrap();
        let receipt = agent_dir
            .path()
            .join("managed-local")
            .join("registration-retries")
            .join("session-dropped.json");
        {
            let _retry = spawn_managed_registration_retry(
                "http://127.0.0.1:1",
                "device-token",
                "Codex",
                json!({"session_id": "session-dropped"}),
                "session-dropped",
                DeferredNotices::default(),
                agent_dir.path().to_path_buf(),
            );
            let payload: Value =
                serde_json::from_slice(&std::fs::read(&receipt).unwrap()).unwrap();
            assert_eq!(payload["recovery_exhausted"], json!(false));
        }

        let payload: Value = serde_json::from_slice(&std::fs::read(&receipt).unwrap()).unwrap();
        assert_eq!(
            payload["recovery_exhausted"],
            json!(true),
            "receipt still claims an active recovery after its handle went away"
        );
    }

    #[test]
    fn failure_summary_separates_a_slow_host_from_a_missing_one() {
        let runtime = tokio::runtime::Runtime::new().unwrap();
        // Nothing listens on this port, so the connection is refused outright.
        let unreachable = runtime
            .block_on(async {
                runtime_host_client()
                    .post("http://127.0.0.1:1/api/sessions/managed-local/this-device")
                    .send()
                    .await
            })
            .map_err(|error| anyhow::Error::new(error).context("register managed Codex launch"))
            .unwrap_err();
        assert_eq!(
            registration_failure_summary(&unreachable, Duration::from_secs(45)),
            "Runtime Host is unreachable"
        );

        let (url, _requests) = spawn_stalled_server();
        let slow = runtime
            .block_on(async {
                runtime_host_client()
                    .post(format!("{url}/api/sessions/managed-local/this-device"))
                    .timeout(Duration::from_millis(200))
                    .send()
                    .await
            })
            .map_err(|error| anyhow::Error::new(error).context("register managed Codex launch"))
            .unwrap_err();
        assert_eq!(
            registration_failure_summary(&slow, Duration::from_secs(45)),
            "Runtime Host did not answer within 45s",
            "a host that accepted the connection and kept working is not an outage"
        );
    }

    /// Accept connections and never answer, so the client hits its own deadline.
    fn spawn_stalled_server() -> (String, std::sync::mpsc::Sender<()>) {
        use std::net::TcpListener;
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let (sender, receiver) = std::sync::mpsc::channel::<()>();
        std::thread::spawn(move || {
            let mut held = Vec::new();
            for stream in listener.incoming() {
                let Ok(stream) = stream else { return };
                held.push(stream);
                if receiver.try_recv().is_ok() {
                    return;
                }
            }
        });
        (format!("http://{address}"), sender)
    }

    #[test]
    fn stale_receipts_are_pruned_and_recent_ones_survive() {
        let agent_dir = tempfile::tempdir().unwrap();
        let directory = agent_dir.path().join("managed-local").join("registration-retries");
        std::fs::create_dir_all(&directory).unwrap();

        let write = |name: &str, created_at: chrono::DateTime<chrono::Utc>| {
            std::fs::write(
                directory.join(format!("{name}.json")),
                serde_json::to_vec(&json!({
                    "schema_version": 1,
                    "session_id": name,
                    "created_at": created_at.to_rfc3339(),
                }))
                .unwrap(),
            )
            .unwrap();
        };
        let now = chrono::Utc::now();
        write("ancient", now - chrono::Duration::days(30));
        write("recent", now - chrono::Duration::hours(1));
        // A receipt we cannot date is left alone: deleting evidence we failed to
        // parse is how a debugging session loses the thing it needed.
        std::fs::write(directory.join("unparseable.json"), b"{not json").unwrap();

        prune_stale_retry_receipts(&directory);

        assert!(!directory.join("ancient.json").exists());
        assert!(directory.join("recent.json").exists());
        assert!(directory.join("unparseable.json").exists());
    }

    /// The regression this whole path exists for.
    ///
    /// The host is up and answers correctly — it is just slower than the old 2s
    /// client deadline and well inside its own 45s budget. That is the incident
    /// exactly: every attempt died on the client's own stopwatch, recovery
    /// declared the host unavailable, and the session ran its whole life
    /// unregistered. A test that merely returns a fast 503 does not reproduce
    /// it, because retrying past a fast rejection is something the old loop
    /// already did.
    #[test]
    fn recovery_converges_when_the_host_is_slower_than_the_foreground_deadline() {
        use std::io::{Read, Write};
        use std::net::TcpListener;

        // Comfortably past FOREGROUND_REGISTRATION_TIMEOUT (and the old 2s
        // recovery deadline), far inside the server budget recovery now honors.
        const HOST_THINKING_TIME: Duration = Duration::from_secs(4);

        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = listener.local_addr().unwrap();
        let (sender, requests) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for stream in listener.incoming() {
                let Ok(mut stream) = stream else { return };
                let sender = sender.clone();
                std::thread::spawn(move || {
                    let mut buffer = [0_u8; 8192];
                    let Ok(size) = stream.read(&mut buffer) else {
                        return;
                    };
                    let request = String::from_utf8_lossy(&buffer[..size]).to_string();
                    let (status, body) = if request.contains("managed-local/this-device") {
                        // Accept, then hold the request open the way a write
                        // queued behind the single writer does.
                        std::thread::sleep(HOST_THINKING_TIME);
                        (
                            "200 OK",
                            json!({"session_id": "session-late", "run_id": "run-late"}).to_string(),
                        )
                    } else {
                        ("200 OK", r#"{"recorded":true}"#.to_string())
                    };
                    let _ = sender.send(request);
                    let _ = stream.write_all(
                        format!(
                            "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        )
                        .as_bytes(),
                    );
                });
            }
        });
        let url = format!("http://{address}");

        let agent_dir = tempfile::tempdir().unwrap();
        let notices = DeferredNotices::default();
        let retry = spawn_managed_registration_retry(
            &url,
            "device-token",
            "Codex",
            json!({"session_id": "session-late"}),
            "session-late",
            notices,
            agent_dir.path().to_path_buf(),
        );
        // The provider is already running; recovery only confirms once it is.
        retry.provider_alive.store(true, Ordering::Release);

        let receipt = agent_dir
            .path()
            .join("managed-local")
            .join("registration-retries")
            .join("session-late.json");
        assert!(receipt.exists(), "degradation must be visible while it lasts");

        let deadline = std::time::Instant::now() + Duration::from_secs(20);
        while receipt.exists() && std::time::Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(100));
        }
        assert!(
            !receipt.exists(),
            "recovery abandoned a host that was working and answered inside its own budget"
        );

        let mut confirmed = false;
        while let Ok(request) = requests.recv_timeout(Duration::from_millis(500)) {
            if request.contains("launch-outcome") {
                confirmed = true;
            }
        }
        assert!(
            confirmed,
            "a recovered registration must still confirm its launch"
        );
        drop(retry);
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
    // Identify the receipt this handle is responsible for settling, from both
    // `abandon` (used by the facade's detached launch paths) and `Drop`.
    agent_dir: PathBuf,
    session_id: String,
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
        // The recovery thread is detached and only observes `cancel` between
        // attempts, so a process exiting mid-request kills it with the receipt
        // still reading "recovering" — a durable claim about a thread that no
        // longer exists, and a window this deadline change made longer.
        //
        // Settling here is safe because recovery cannot usefully finish once
        // the provider is gone: confirmation requires a live provider. Both
        // interleavings are benign — if the thread already cleared the receipt
        // there is nothing to settle, and if it is still running its own
        // terminal write supersedes this one.
        let already_settled = registration_retry_path(&self.agent_dir, &self.session_id)
            .map(|path| !path.is_file())
            .unwrap_or(true);
        if !already_settled {
            let _ =
                record_registration_retry(&self.agent_dir, &self.session_id, &self.provider, true);
        }
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
        let started_at = std::time::Instant::now();
        let mut reported_first_failure = false;
        // Recovery lasts as long as the provider does. The old loop gave up
        // after five attempts inside ~15 seconds, which is shorter than a single
        // slow write on this route — so a host that answered a minute later, or
        // a session that outlived a brief blip, was stranded unregistered for
        // its whole life with nothing left running to converge it.
        for attempt in 0..RECOVERY_MAX_ATTEMPTS {
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
                RECOVERY_REGISTRATION_TIMEOUT,
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
                        // Wait for the provider to reach ready. The old fixed
                        // 10s cutoff aborted launches whose bridge merely
                        // started slowly under load, which contradicts
                        // recovering for the life of the provider; `cancel` is
                        // the real end-of-life signal, and the cap below only
                        // exists so a leaked handle cannot wait forever.
                        let mut waited = Duration::ZERO;
                        while waited < PROVIDER_READY_WAIT_CAP {
                            if cancel_for_thread.load(Ordering::Acquire) {
                                settle_exhausted(&session_id, &provider);
                                return;
                            }
                            if provider_alive_for_thread.load(Ordering::Acquire) {
                                // Registration itself succeeded, so the
                                // registration receipt is finished either way.
                                // A failed confirmation is a weaker
                                // degradation with its own receipt and its own
                                // retry -- and must never abort a provider that
                                // is running, which is what dropping this
                                // transaction unconfirmed would have posted.
                                transaction.confirm_or_degrade(&provider, &agent_dir, &notices);
                                clear_registration_retry(&agent_dir, &session_id);
                                return;
                            }
                            std::thread::sleep(Duration::from_millis(100));
                            waited += Duration::from_millis(100);
                        }
                        notices.push(format!(
                            "Longhouse warning: {provider} never became ready after its registration recovered"
                        ));
                        settle_exhausted(&session_id, &provider);
                    }
                    return;
                }
                Err(error) if attempt + 1 < RECOVERY_MAX_ATTEMPTS => {
                    // Say what actually happened, once, before the first sleep.
                    // Repeating it every attempt would bury the launch output.
                    if !reported_first_failure {
                        reported_first_failure = true;
                        notices.push(format!(
                            "Longhouse warning: {}; {provider} is running locally and registration keeps retrying in the background",
                            registration_failure_summary(&error, RECOVERY_REGISTRATION_TIMEOUT)
                        ));
                    }
                    let backoff = recovery_backoff(attempt);
                    let mut slept = Duration::ZERO;
                    while slept < backoff {
                        if cancel_for_thread.load(Ordering::Acquire) {
                            // The provider exited mid-backoff. Settle the
                            // receipt: returning without it left health reading
                            // a recovery that no longer had a thread behind it.
                            settle_exhausted(&session_id, &provider);
                            return;
                        }
                        std::thread::sleep(Duration::from_millis(200));
                        slept += Duration::from_millis(200);
                    }
                }
                Err(error) => {
                    notices.push(format!(
                        "Longhouse warning: could not recover {provider} Helm registration after {} attempts over {}s: {error:#}",
                        attempt + 1,
                        started_at.elapsed().as_secs()
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
