//! Native-pair update check and policy.
//!
//! Longhouse's primary install is the **native pair** — the `longhouse` facade
//! and `longhouse-engine` binaries delivered by `scripts/install.sh` — plus
//! `Longhouse.app` on macOS. Before this module the only update checker in the
//! product watched the Python `longhouse` uv package (`update_manager.py`),
//! which most users do not have as their primary install, and nothing anywhere
//! applied an update. A fix could ship as a release and never reach the machine
//! that needed it.
//!
//! ## What this module owns
//!
//! Resolving the latest published native release, comparing it to the running
//! build, and recording the answer where health surfaces and the Runtime Host
//! can see it. Applying an update is deliberately a separate decision governed
//! by [`UpdatePolicy`].
//!
//! ## Why the daemon and not the CLI
//!
//! The engine is the always-on component. A check that only runs when a human
//! types a command is worth little to a machine whose owner has stopped looking
//! at it, which is the exact population this exists to serve.
//!
//! ## Deliberate limits
//!
//! - A non-`release` build never auto-applies. `make dogfood-refresh` installs
//!   a dev build into `~/.local/bin`; applying a published release over it
//!   would silently destroy local iteration. Dev builds check and report only.
//! - The check never executes downloaded code. It resolves a version string
//!   from a redirect and nothing more.
//! - A failed check is not an assertion that no update exists. `last_error` is
//!   recorded and `update_available` stays `None`, because reporting "current"
//!   from a failed lookup is the false-green this product forbids.
//! - **This path updates the native pair only**, so on a machine with
//!   `Longhouse.app` installed it declines to apply at all rather than produce
//!   a half-upgraded install. The installer versions the app and the pair
//!   together (`scripts/install.sh:390-414`) and `scripts/ops/release.sh` bumps
//!   both to one shared version, so they are lockstep by design. Replacing a
//!   running GUI app from a background daemon — `rm -rf /Applications/…` under
//!   a user who may have it open — is a worse failure than being one release
//!   behind. The shell installer moves both and stays the complete path.
//! - **Self-restart is gated on the installed service definition**, read from
//!   disk rather than assumed. An engine that exits cleanly under an older unit
//!   carrying `Restart=on-failure` would never come back, and a machine with no
//!   Machine Agent is worse than a stale one.

use anyhow::Context;
use anyhow::Result;
use serde::Deserialize;
use serde::Serialize;
use std::time::Duration;

use crate::build_identity;

/// Where published native releases live.
const RELEASES_LATEST_URL: &str = "https://github.com/cipher982/longhouse/releases/latest";

/// How often the daemon re-checks. Releases are a human-paced event; a tighter
/// interval would spend requests to learn nothing.
pub const UPDATE_CHECK_INTERVAL: Duration = Duration::from_secs(6 * 60 * 60);

/// Bound on the redirect lookup. A slow or unreachable GitHub must not stall
/// the tick that calls this.
const CHECK_TIMEOUT: Duration = Duration::from_secs(10);

/// Bound on each asset transfer. Every network call in this module carries a
/// deadline; an unbounded one can hold the update lock indefinitely.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(120);

/// Ceiling on a single release asset. The engine binary is ~17MB today, so
/// this is generous while still refusing to buffer something absurd.
const MAX_ASSET_BYTES: u64 = 256 * 1024 * 1024;

/// What the machine is permitted to do about an available update.
///
/// The vocabulary is taken verbatim from the runner's existing
/// `auto_update_policy` (`server/zerg/schemas/runner_schemas.py`) rather than
/// invented, so one word means one thing across the product.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum UpdatePolicy {
    /// Do not check and do not report.
    Off,
    /// Check and report; never apply. The default.
    Notify,
    /// Check, report, and apply when the running build is a release build.
    Apply,
}

impl Default for UpdatePolicy {
    fn default() -> Self {
        // Notify, not Apply. Replacing a user's binary without being asked is a
        // decision they should make once, explicitly, rather than inherit.
        Self::Notify
    }
}

impl UpdatePolicy {
    pub fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_lowercase().as_str() {
            "off" | "disabled" | "never" => Some(Self::Off),
            "notify" | "check" => Some(Self::Notify),
            "apply" | "auto" => Some(Self::Apply),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Off => "off",
            Self::Notify => "notify",
            Self::Apply => "apply",
        }
    }

    pub fn checks_enabled(self) -> bool {
        !matches!(self, Self::Off)
    }
}

/// Operator override read from `~/.longhouse/agent/update-control.json`.
///
/// Modelled on `ArchiveRepairControl` in `daemon.rs`, including its expiry
/// rule: a directive that carries `expires_at` stops applying once that time
/// passes, so a control record left behind by a one-off intervention cannot
/// silently govern the machine forever. `off` is sticky without an expiry,
/// because "stop touching my binaries" should not lapse on its own.
#[derive(Debug, Clone, Default, Deserialize)]
pub struct UpdateControl {
    pub policy: Option<String>,
    pub expires_at: Option<String>,
    /// Pin to an exact release version instead of latest. Used by rollback and
    /// by staged rollout cohorts.
    pub pin_version: Option<String>,
    pub actor: Option<String>,
    pub reason: Option<String>,
    pub updated_at: Option<String>,
}

impl UpdateControl {
    /// True when this record still governs. Mirrors
    /// `ArchiveRepairControl::active_override`.
    fn active_override(&self) -> bool {
        // A standing user preference does not lapse. Expiry exists for
        // temporary operator or remote directives — a cohort pin, a pause
        // during an incident — not for a choice the machine's owner made about
        // their own binaries. `off` and `apply` are both such choices.
        if matches!(
            self.policy
                .as_deref()
                .unwrap_or("")
                .trim()
                .to_ascii_lowercase()
                .as_str(),
            "off" | "disabled" | "never" | "apply" | "auto"
        ) && self.expires_at.is_none()
        {
            return true;
        }
        let Some(expires_at) = self.expires_at.as_deref() else {
            return false;
        };
        chrono::DateTime::parse_from_rfc3339(expires_at)
            .map(|value| value.with_timezone(&chrono::Utc) > chrono::Utc::now())
            .unwrap_or(false)
    }

    pub fn effective_policy(&self, default_policy: UpdatePolicy) -> UpdatePolicy {
        if !self.active_override() {
            return default_policy;
        }
        self.policy
            .as_deref()
            .and_then(UpdatePolicy::parse)
            .unwrap_or(default_policy)
    }

    pub fn effective_pin(&self) -> Option<String> {
        if !self.active_override() {
            return None;
        }
        self.pin_version
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(normalize_version)
    }
}

/// Read the control file, treating every failure as "no override".
///
/// An unreadable or malformed control file must not change update behaviour;
/// the compiled default is the safe answer and the parse error is logged.
pub fn read_update_control() -> UpdateControl {
    let Ok(path) = crate::config::get_agent_update_control_path() else {
        return UpdateControl::default();
    };
    let Ok(bytes) = std::fs::read(&path) else {
        return UpdateControl::default();
    };
    match serde_json::from_slice::<UpdateControl>(&bytes) {
        Ok(control) => control,
        Err(error) => {
            tracing::warn!(
                path = %path.display(),
                %error,
                "ignoring malformed update control file"
            );
            UpdateControl::default()
        }
    }
}

/// The published state of native-pair updates, as this machine understands it.
///
/// Serialized into `engine-status.json` and the heartbeat so both the local
/// menu bar and the Runtime Host read the same facts.
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct UpdateStatus {
    /// Release version of the running binary.
    pub installed_version: String,
    /// Build channel of the running binary. `apply` is refused unless this is
    /// `release`; see the module docs.
    pub channel: String,
    /// Latest published release, when the last check succeeded.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub latest_version: Option<String>,
    /// `None` means unknown, not "up to date". A failed check must never read
    /// as current.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub update_available: Option<bool>,
    pub policy: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub pinned_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checked_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
    /// Why an available update was not applied, when one was available and
    /// policy allowed it. Distinguishes "did not try" from "tried and failed".
    #[serde(skip_serializing_if = "Option::is_none")]
    pub apply_blocked_reason: Option<String>,
    /// Set once new binaries are installed but this process still runs the old
    /// ones. The update is not finished until a restart; saying so is the
    /// "one specific, safe action" the health contract owes the user.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub restart_required: bool,
    /// Version installed on disk when `restart_required` is set.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub staged_version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_apply_error: Option<String>,
    /// Who set a non-default policy, and why, when an override is in force.
    ///
    /// Carried so a user can always answer "why is my machine on this policy
    /// and who decided that" without reading a JSON file, and so a remotely
    /// issued directive is attributable rather than anonymous.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub control_actor: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub control_reason: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub control_updated_at: Option<String>,
}

impl UpdateStatus {
    pub fn unchecked(policy: UpdatePolicy) -> Self {
        Self {
            installed_version: build_identity::VERSION.to_string(),
            channel: build_identity::CHANNEL.to_string(),
            latest_version: None,
            update_available: None,
            policy: policy.as_str().to_string(),
            pinned_version: None,
            checked_at: None,
            last_error: None,
            apply_blocked_reason: None,
            restart_required: false,
            staged_version: None,
            last_apply_error: None,
            control_actor: None,
            control_reason: None,
            control_updated_at: None,
        }
    }

    /// Attach the provenance of the override that produced this status.
    ///
    /// Only populated when an override is actually in force; a machine running
    /// the compiled default has no actor to name, and inventing one would
    /// imply a decision nobody made.
    fn with_control_provenance(mut self, control: &UpdateControl) -> Self {
        if control.active_override() {
            self.control_actor = control.actor.clone();
            self.control_reason = control.reason.clone();
            self.control_updated_at = control.updated_at.clone();
        }
        self
    }
}

/// Strip a leading `v` and surrounding whitespace from a release tag.
pub fn normalize_version(raw: &str) -> String {
    raw.trim().trim_start_matches('v').trim().to_string()
}

/// Parse a dotted release version into comparable components.
///
/// Returns `None` for anything that is not three numeric components, which is
/// deliberate: an unparseable version is not silently treated as older or newer
/// than the running build, it makes the comparison unknown.
fn version_tuple(value: &str) -> Option<(u64, u64, u64)> {
    let core = value.split(['-', '+']).next().unwrap_or(value);
    let mut parts = core.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts.next()?.parse().ok()?;
    if parts.next().is_some() {
        return None;
    }
    Some((major, minor, patch))
}

/// True when `latest` is strictly newer than `installed`.
///
/// `None` when either side is unparseable. Callers must treat `None` as
/// unknown rather than as "no update", so a malformed tag cannot mask an
/// available fix or trigger a pointless reinstall.
pub fn is_newer(installed: &str, latest: &str) -> Option<bool> {
    Some(version_tuple(latest)? > version_tuple(installed)?)
}

/// Resolve the latest published native release version.
///
/// GitHub redirects `/releases/latest` to `/releases/tag/v<version>`, so the
/// version can be read from the final URL without parsing an API response or
/// spending an unauthenticated API rate-limit slot. This is the same technique
/// `scripts/install.sh:249` uses, kept identical so the installer and the
/// engine can never disagree about what "latest" means.
pub async fn resolve_latest_version(client: &reqwest::Client) -> Result<String> {
    let response = client
        .get(RELEASES_LATEST_URL)
        .timeout(CHECK_TIMEOUT)
        .send()
        .await
        .context("resolve latest Longhouse native release")?;
    let final_url = response.url().as_str().to_string();
    let (_, tag) = final_url
        .rsplit_once("/releases/tag/")
        .with_context(|| format!("unexpected release redirect target: {final_url}"))?;
    let version = normalize_version(tag);
    if version.is_empty() {
        anyhow::bail!("resolved an empty release version from {final_url}");
    }
    Ok(version)
}

/// Run one check and produce the status to publish.
///
/// Never returns `Err`: a check failure is a recorded fact about this machine,
/// not a reason to fail the tick that called it.
pub async fn check(client: &reqwest::Client, policy: UpdatePolicy, pin: Option<String>) -> UpdateStatus {
    let mut status = UpdateStatus::unchecked(policy);
    status.pinned_version = pin.clone();
    if !policy.checks_enabled() {
        return status;
    }
    status.checked_at = Some(chrono::Utc::now().to_rfc3339());

    let target = match pin {
        Some(pinned) => pinned,
        None => match resolve_latest_version(client).await {
            Ok(version) => version,
            Err(error) => {
                status.last_error = Some(format!("{error:#}"));
                return status;
            }
        },
    };

    status.update_available = is_newer(&status.installed_version, &target);
    if status.update_available.is_none() {
        status.last_error = Some(format!(
            "cannot compare installed {} against {}",
            status.installed_version, target
        ));
    }
    status.latest_version = Some(target);

    // Computed whenever the target differs at all, not only when it is newer:
    // a pin holding a cohort on an older release still needs to say why it did
    // or did not act.
    if status.latest_version.as_deref() != Some(status.installed_version.as_str()) {
        status.apply_blocked_reason = apply_blocked_reason(policy);
    }
    status
}

/// Persist the last check result.
///
/// The status lives in a file rather than in daemon memory for the same reason
/// `archive-repair-control.json` does: the heartbeat builder can read it fresh
/// without threading state through the loop, and the answer survives a daemon
/// restart so a machine that has just come back does not report "never checked"
/// when it checked an hour ago.
pub fn write_status(status: &UpdateStatus) -> Result<()> {
    let path = crate::config::get_agent_update_status_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).ok();
    }
    let body = serde_json::to_vec_pretty(status).context("serialize update status")?;
    // Write-then-rename so a reader never observes a half-written file.
    let temporary = path.with_extension("json.tmp");
    std::fs::write(&temporary, &body)
        .with_context(|| format!("write {}", temporary.display()))?;
    std::fs::rename(&temporary, &path).with_context(|| format!("rename into {}", path.display()))?;
    Ok(())
}

/// Read the last check result, or `None` when no check has ever completed.
pub fn read_status() -> Option<UpdateStatus> {
    let path = crate::config::get_agent_update_status_path().ok()?;
    let bytes = std::fs::read(&path).ok()?;
    serde_json::from_slice(&bytes).ok()
}

/// Run one check under current policy and persist the result.
///
/// Called from the daemon's update tick. Errors are logged rather than
/// propagated: an update check must never be able to fail a heartbeat.
pub async fn run_check_tick(client: &reqwest::Client) {
    // Held for the whole read/check/apply/write sequence, not just the swap.
    //
    // Two concurrent checks — the daemon tick and a hand-run `update check` —
    // could otherwise both read status, one install and record a pending
    // restart, and the loser then overwrite that record with its own older
    // view. The debt would vanish and the same release would install again on
    // the next tick, forever. Bailing rather than waiting is right for periodic
    // work: the other actor is already doing this.
    let _lock = match UpdateLock::acquire() {
        Ok(lock) => lock,
        Err(error) => {
            tracing::debug!(error = %format!("{error:#}"), "skipping update tick; lock held");
            return;
        }
    };
    let control = read_update_control();
    let policy = control.effective_policy(UpdatePolicy::default());
    let previous = read_status();

    if !policy.checks_enabled() {
        // `off` must actually stop reporting. Returning early used to leave the
        // last `notify`/`apply` status on disk, so heartbeats kept publishing a
        // policy the machine was no longer running.
        let mut status = UpdateStatus::unchecked(policy).with_control_provenance(&control);
        carry_pending_restart(&mut status, previous.as_ref());
        if let Err(error) = write_status(&status) {
            tracing::warn!(error = %format!("{error:#}"), "could not persist update status");
        }
        return;
    }

    let mut status = check(client, policy, control.effective_pin())
        .await
        .with_control_provenance(&control);
    carry_pending_restart(&mut status, previous.as_ref());

    // A restart already owed means the new binaries are on disk and this
    // process is simply still running the old ones. Re-applying would
    // re-download and re-activate the same release on every tick, forever,
    // because the running version never changes until a restart.
    if status.restart_required {
        tracing::debug!(
            staged = status.staged_version.as_deref().unwrap_or("unknown"),
            "skipping apply; a staged update is already awaiting restart"
        );
    } else if should_apply(&status) {
        if let Some(version) = status.latest_version.clone() {
            apply_staged(client, &version, &mut status).await;
        }
    }
    if let Some(error) = status.last_error.as_deref() {
        tracing::warn!(%error, "native update check did not complete");
    } else if status.update_available == Some(true) {
        tracing::info!(
            installed = %status.installed_version,
            latest = status.latest_version.as_deref().unwrap_or("unknown"),
            blocked = status.apply_blocked_reason.as_deref().unwrap_or("none"),
            "native update available"
        );
    }
    if let Err(error) = write_status(&status) {
        tracing::warn!(error = %format!("{error:#}"), "could not persist update status");
    }
}

/// Download, verify, and install a release, then record that a restart is owed.
///
/// ## Restarting, and why it is conditional
///
/// A swap on disk that never runs has not delivered the fix, so this exits so
/// the supervisor brings the process back on the new binary — but only when
/// [`supervisor_restarts_clean_exit`] proves it will.
///
/// The two supervisors genuinely disagree. launchd runs this job with a bare
/// `KeepAlive=true`, so any exit restarts. The generated systemd unit now uses
/// `Restart=always` for exactly this reason; it previously used
/// `Restart=on-failure`, under which a clean exit stops the service
/// permanently. Machines installed before that change still carry the old unit,
/// which is why the policy is read from the file on disk rather than assumed
/// from this binary's own generator. Being a release behind is recoverable;
/// having no Machine Agent is not.
///
/// When the restart cannot be proven safe the swap still completes and
/// `restart_required` records the debt, with `apply_blocked_reason` naming what
/// to do about it. That is the difference between a pending update and the
/// half-applied limbo this work exists to remove.
async fn apply_staged(client: &reqwest::Client, version: &str, status: &mut UpdateStatus) {
    // No lock acquired here: `run_check_tick` holds it across the whole
    // read/check/apply/write sequence, which is what keeps a concurrent actor
    // from overwriting the restart debt this function records.
    debug_assert!(
        UpdateLock::acquire().is_err(),
        "apply_staged must run under the caller's update lock"
    );
    let release_dir = match stage_release(client, version).await {
        Ok(dir) => dir,
        Err(error) => {
            let message = format!("{error:#}");
            tracing::warn!(%version, error = %message, "staging native update failed");
            status.last_apply_error = Some(message);
            return;
        }
    };
    if let Err(error) = activate_release(&release_dir) {
        let message = format!("{error:#}");
        tracing::warn!(%version, error = %message, "installing native update failed");
        status.last_apply_error = Some(message);
        return;
    }
    status.restart_required = true;
    status.staged_version = Some(version.to_string());

    // Record the obligation before acting on it. If the exit below happens, this
    // process does not get another chance to write anything, and a restart that
    // is owed but unrecorded is the half-applied limbo this work removes.
    if let Err(error) = write_status(status) {
        tracing::warn!(error = %format!("{error:#}"), "could not persist update status");
    }

    match supervisor_restarts_clean_exit() {
        Some(true) => {
            tracing::info!(
                %version,
                "installed native update; exiting so the supervisor restarts on the new binary"
            );
            // The binaries are already swapped and this process holds the old
            // inode, so only a restart can run the fix. Safe here because the
            // *installed* definition was read and proves we come back.
            std::process::exit(0);
        }
        Some(false) => {
            tracing::warn!(
                %version,
                "installed native update, but the service definition would not restart a clean \
                 exit; run `longhouse machine repair --repair-service` to refresh it"
            );
            status.apply_blocked_reason = Some("supervisor_would_not_restart".to_string());
        }
        None => {
            tracing::warn!(
                %version,
                "installed native update; could not read the service definition, so not exiting"
            );
            status.apply_blocked_reason = Some("supervisor_policy_unknown".to_string());
        }
    }
}

/// Carry a pending restart obligation across ticks, or retire it once served.
///
/// Each tick builds a fresh status, so without this an owed restart is
/// forgotten on the next pass and the same release installs again every
/// interval. The obligation ends only when the running build actually matches
/// what was staged, which is the one observation that proves the restart
/// happened.
fn carry_pending_restart(status: &mut UpdateStatus, previous: Option<&UpdateStatus>) {
    let Some(previous) = previous else {
        return;
    };
    if !previous.restart_required {
        return;
    }
    let staged = previous.staged_version.as_deref();
    if staged == Some(status.installed_version.as_str()) {
        // Running what was staged: the restart happened, the debt is settled.
        return;
    }
    status.restart_required = true;
    status.staged_version = previous.staged_version.clone();
}

/// Whether an available target should be installed now.
///
/// A pin is an instruction to run an exact version, so it applies in either
/// direction. Without this a pin could only ever move a machine forward, which
/// contradicts its documented use for holding a cohort on a known-good release.
fn should_apply(status: &UpdateStatus) -> bool {
    if status.apply_blocked_reason.is_some() {
        return false;
    }
    let Some(target) = status.latest_version.as_deref() else {
        return false;
    };
    if status.pinned_version.is_some() {
        return target != status.installed_version;
    }
    status.update_available == Some(true)
}

/// Whether the installed supervisor will restart this process after a clean exit.
///
/// The whole safety of self-restart rests on this being read from the service
/// definition **actually installed on the machine**, not from what the current
/// engine version would generate. An engine that upgraded itself while running
/// under an older unit — one still carrying `Restart=on-failure` — would exit
/// cleanly and never come back, leaving that machine with no Machine Agent.
/// Being one release behind is recoverable; being absent is not.
///
/// Returns `None` when the definition cannot be read, which is treated as "do
/// not exit". Absence of evidence is not evidence that a restart will happen.
pub fn supervisor_restarts_clean_exit() -> Option<bool> {
    let home = native_home().ok()?;
    let path = crate::device::installed_service_definition_path(&home)?;
    let body = std::fs::read_to_string(&path).ok()?;
    Some(definition_restarts_clean_exit(&body))
}

/// Does this service definition bring the process back after `exit(0)`?
fn definition_restarts_clean_exit(body: &str) -> bool {
    if body.contains("<key>KeepAlive</key>") {
        // launchd restarts unconditionally only for a bare `<true/>`. The
        // dictionary form — `{SuccessfulExit: false}`, which the Desktop app
        // uses so Quit means quit — deliberately does *not* restart a clean
        // exit, and reading it as if it did is the exact mistake this guard
        // exists to prevent.
        return body
            .split("<key>KeepAlive</key>")
            .nth(1)
            .map(|rest| rest.trim_start().starts_with("<true/>"))
            .unwrap_or(false);
    }
    body.lines().any(|line| {
        matches!(line.trim(), "Restart=always" | "Restart=on-success")
    })
}

/// Version of an installed `Longhouse.app`, when one is present.
///
/// `scripts/ops/release.sh` bumps the app, the CLI, the engine and the iOS
/// project to one shared release version, so they are lockstep by design.
pub fn installed_macos_app_version() -> Option<String> {
    if !cfg!(target_os = "macos") {
        return None;
    }
    let plist = std::path::Path::new("/Applications/Longhouse.app/Contents/Info.plist");
    let body = std::fs::read_to_string(plist).ok()?;
    let rest = body.split("<key>CFBundleShortVersionString</key>").nth(1)?;
    let value = rest.split("<string>").nth(1)?.split("</string>").next()?;
    let value = normalize_version(value);
    (!value.is_empty()).then_some(value)
}

/// Why an available update will not be applied, or `None` when it would be.
pub fn apply_blocked_reason(policy: UpdatePolicy) -> Option<String> {
    match policy {
        UpdatePolicy::Off => Some("policy_off".to_string()),
        UpdatePolicy::Notify => Some("policy_notify".to_string()),
        UpdatePolicy::Apply => {
            if build_identity::CHANNEL != "release" {
                // A dogfood build in ~/.local/bin is uncommitted work in
                // binary form. Overwriting it with a published release would
                // discard exactly what the developer is testing.
                return Some("non_release_build".to_string());
            }
            // Do not create a half-upgraded macOS install.
            //
            // The installer versions `Longhouse.app` and the native pair
            // together (`install.sh:390-414`), and this path can only move the
            // pair. Replacing a running GUI app from a background daemon —
            // `rm -rf /Applications/Longhouse.app` under a user who may have it
            // open — is a worse failure than being a release behind, so the
            // engine declines rather than doing half the job silently. The
            // shell installer upgrades both and remains the complete path.
            if installed_macos_app_version().is_some() {
                return Some("macos_app_installed".to_string());
            }
            None
        }
    }
}

/// One-line human rendering of a status.
///
/// Every branch says what the machine knows and what, if anything, the reader
/// should do. "Unknown" is stated rather than rounded to "up to date".
pub fn describe_status(status: &UpdateStatus) -> String {
    if status.restart_required {
        let staged = status.staged_version.as_deref().unwrap_or("a new version");
        let mut message = format!(
            "Installed {staged}; restart the Longhouse engine service to run it (still running {}).",
            status.installed_version
        );
        if let Some(app_version) = installed_macos_app_version() {
            if Some(app_version.as_str()) != status.staged_version.as_deref() {
                // Only says this when the versions actually differ, so it stops
                // being noise the moment the installer has brought both across.
                message.push_str(&format!(
                    " Longhouse.app is at {app_version}; re-run the installer to match it."
                ));
            }
        }
        return message;
    }
    if let Some(error) = status.last_apply_error.as_deref() {
        return format!("Update {} could not be installed: {error}", status.installed_version);
    }
    match status.update_available {
        Some(true) => {
            let latest = status.latest_version.as_deref().unwrap_or("a newer version");
            match status.apply_blocked_reason.as_deref() {
                Some("non_release_build") => format!(
                    "{latest} is available; this is a {} build, so it will not be replaced automatically.",
                    status.channel
                ),
                Some("macos_app_installed") => format!(
                    "{latest} is available. Longhouse.app is installed, and this path upgrades \
                     only the command line, so it will not half-upgrade you — run the installer \
                     to move both: curl -fsSL https://get.longhouse.ai/install.sh | bash"
                ),
                Some("supervisor_would_not_restart") => format!(
                    "Installed {latest}, but this machine's service would not restart on a clean \
                     exit. Run: longhouse machine repair --repair-service"
                ),
                Some("supervisor_policy_unknown") => format!(
                    "Installed {latest}, but the service definition could not be read, so the \
                     engine did not restart itself. Restart it to finish."
                ),
                Some("policy_off") => format!("{latest} is available; update policy is off."),
                Some(_) => format!(
                    "{latest} is available (installed {}). Run the installer to upgrade.",
                    status.installed_version
                ),
                None => format!("{latest} is available and will be installed."),
            }
        }
        Some(false) => format!("Up to date ({}).", status.installed_version),
        None => {
            let reason = status.last_error.as_deref().unwrap_or("no check has completed");
            format!(
                "Update state unknown for {}: {reason}",
                status.installed_version
            )
        }
    }
}

/// The release-asset naming this build expects.
///
/// Mirrors `native_target()` in `scripts/install.sh`. Kept as an explicit match
/// rather than a format string so an unsupported platform is a compile-visible
/// gap rather than a 404 at download time.
pub fn native_target() -> Option<&'static str> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Some("darwin-arm64"),
        ("linux", "x86_64") => Some("linux-x64"),
        ("linux", "aarch64") => Some("linux-arm64"),
        _ => None,
    }
}

/// Parse a `sha256  filename` checksums file into the digest for one asset.
///
/// Accepts the `*name` binary-mode marker that `sha256sum` emits, matching
/// `verify_release_checksum` in the installer.
fn expected_digest<'a>(checksums: &'a str, asset: &str) -> Option<&'a str> {
    checksums.lines().find_map(|line| {
        let mut parts = line.split_whitespace();
        let digest = parts.next()?;
        let name = parts.next()?;
        let name = name.strip_prefix('*').unwrap_or(name);
        (name == asset).then_some(digest)
    })
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::Digest;
    let mut hasher = sha2::Sha256::new();
    hasher.update(bytes);
    hasher
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// Filesystem roots, overridable for hermetic tests.
///
/// Tests must never be able to touch a real installation, and reading `HOME`
/// directly made the first version of these tests non-hermetic.
fn native_home() -> Result<std::path::PathBuf> {
    if let Ok(root) = std::env::var("LONGHOUSE_NATIVE_ROOT_OVERRIDE") {
        let trimmed = root.trim();
        if !trimmed.is_empty() {
            return Ok(std::path::PathBuf::from(trimmed));
        }
    }
    let home = std::env::var("HOME").context("HOME not set")?;
    Ok(std::path::PathBuf::from(home))
}

/// Root holding versioned native releases, matching the installer's layout.
fn native_root() -> Result<std::path::PathBuf> {
    Ok(native_home()?
        .join(".local")
        .join("share")
        .join("longhouse"))
}

fn native_release_root() -> Result<std::path::PathBuf> {
    Ok(native_root()?.join("releases"))
}

/// The `current` symlink every installed binary resolves through.
fn current_link_path() -> Result<std::path::PathBuf> {
    Ok(native_root()?.join("current"))
}

fn native_bin_dir() -> Result<std::path::PathBuf> {
    Ok(native_home()?.join(".local").join("bin"))
}

/// Exclusive lock over every mutation in this module.
///
/// The daemon tick and a hand-run `longhouse-engine update check` can otherwise
/// stage into the same directory, publish the same release, and rewrite the
/// same status file concurrently. Held for the whole stage-and-activate
/// sequence, not just the swap, because a half-staged directory published by
/// one actor is exactly what the other must not activate.
struct UpdateLock {
    path: std::path::PathBuf,
}

impl UpdateLock {
    /// Fails rather than waits. An update is periodic work; a second actor can
    /// simply come back on the next tick, and blocking here would be one more
    /// way to stall the daemon.
    fn acquire() -> Result<Self> {
        let path = crate::config::get_agent_dir()?.join("update.lock");
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).ok();
        }
        // A lock left behind by a killed process must not wedge updates
        // forever — the failure this whole effort exists to remove.
        if let Ok(metadata) = std::fs::metadata(&path) {
            let stale = metadata
                .modified()
                .ok()
                .and_then(|modified| modified.elapsed().ok())
                .is_some_and(|age| age > LOCK_STALE_AFTER);
            if stale {
                tracing::warn!(path = %path.display(), "clearing stale update lock");
                std::fs::remove_file(&path).ok();
            }
        }
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(_) => Ok(Self { path }),
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                anyhow::bail!("another update operation is in progress")
            }
            Err(error) => Err(error).with_context(|| format!("create {}", path.display())),
        }
    }
}

impl Drop for UpdateLock {
    fn drop(&mut self) {
        std::fs::remove_file(&self.path).ok();
    }
}

/// How long before an abandoned lock is assumed dead.
const LOCK_STALE_AFTER: Duration = Duration::from_secs(30 * 60);

/// Download and verify one release into a versioned directory on disk.
///
/// Downloading to a content-addressed staging area before touching anything
/// executable means a failed or tampered download can never become the
/// installed binary. The engine deliberately does not fetch and execute
/// `install.sh`: a daemon running a shell script pulled from the network is a
/// remote-execution channel, and the product's own argument against pushing
/// logic over the wire applies to itself.
pub async fn stage_release(client: &reqwest::Client, version: &str) -> Result<std::path::PathBuf> {
    let target = native_target()
        .with_context(|| format!("no native release for {}", std::env::consts::OS))?;
    let facade_asset = format!("longhouse-{target}");
    let engine_asset = format!("longhouse-engine-{target}");
    let base = format!("https://github.com/cipher982/longhouse/releases/download/v{version}");

    let checksums = client
        .get(format!("{base}/local-runtime-checksums.txt"))
        .timeout(DOWNLOAD_TIMEOUT)
        .send()
        .await
        .and_then(reqwest::Response::error_for_status)
        .context("download release checksums")?
        .text()
        .await
        .context("read release checksums")?;

    let release_dir = native_release_root()?.join(version);
    let staging = release_dir.with_extension("staging");
    // A previous interrupted attempt must not contribute files to this one.
    std::fs::remove_dir_all(&staging).ok();
    std::fs::create_dir_all(&staging)
        .with_context(|| format!("create {}", staging.display()))?;

    for (asset, filename) in [(&facade_asset, "longhouse"), (&engine_asset, "longhouse-engine")] {
        let response = client
            .get(format!("{base}/{asset}"))
            .timeout(DOWNLOAD_TIMEOUT)
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
            .with_context(|| format!("download {asset}"))?;
        // Refuse an implausible payload before buffering it. Without this a
        // wrong or hostile URL could be read into memory unbounded.
        if let Some(length) = response.content_length() {
            if length > MAX_ASSET_BYTES {
                std::fs::remove_dir_all(&staging).ok();
                anyhow::bail!(
                    "{asset} advertises {length} bytes, above the {MAX_ASSET_BYTES} byte limit"
                );
            }
        }
        let bytes = response
            .bytes()
            .await
            .with_context(|| format!("read {asset}"))?;
        if bytes.len() as u64 > MAX_ASSET_BYTES {
            std::fs::remove_dir_all(&staging).ok();
            anyhow::bail!("{asset} exceeded the {MAX_ASSET_BYTES} byte limit");
        }
        let expected = expected_digest(&checksums, asset)
            .with_context(|| format!("no published checksum for {asset}"))?;
        let actual = sha256_hex(&bytes);
        if actual != expected {
            std::fs::remove_dir_all(&staging).ok();
            anyhow::bail!("checksum mismatch for {asset}: expected {expected}, got {actual}");
        }
        let path = staging.join(filename);
        std::fs::write(&path, &bytes).with_context(|| format!("write {}", path.display()))?;
        set_executable(&path)?;
    }

    // Rename into place only once every asset verified, so a release directory
    // that exists is always complete.
    std::fs::remove_dir_all(&release_dir).ok();
    std::fs::rename(&staging, &release_dir)
        .with_context(|| format!("publish {}", release_dir.display()))?;
    Ok(release_dir)
}

#[cfg(unix)]
fn set_executable(path: &std::path::Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755))
        .with_context(|| format!("chmod {}", path.display()))
}

#[cfg(not(unix))]
fn set_executable(_path: &std::path::Path) -> Result<()> {
    Ok(())
}

/// A release directory that is complete and whose pair verifies.
fn validate_release_dir(release_dir: &std::path::Path) -> Result<std::path::PathBuf> {
    let facade = release_dir.join("longhouse");
    let engine = release_dir.join("longhouse-engine");
    for path in [&facade, &engine] {
        if !path.is_file() {
            anyhow::bail!(
                "release directory is incomplete: {} is missing",
                path.display()
            );
        }
    }
    let status = std::process::Command::new(&facade)
        .arg("verify-pair")
        .status()
        .with_context(|| format!("run verify-pair from {}", facade.display()))?;
    if !status.success() {
        anyhow::bail!(
            "pair at {} failed verify-pair; refusing to activate",
            release_dir.display()
        );
    }
    Ok(facade)
}

/// Activate a staged release by repointing `current` at it.
///
/// ## Why a symlink swap and not a file copy
///
/// The installer makes `~/.local/bin/longhouse` a symlink to
/// `../share/longhouse/current/longhouse` (`scripts/install.sh:289-290`), so
/// `current` is the real switch and both binaries move together the instant it
/// changes. An earlier version of this function copied the two binaries into
/// `~/.local/bin` separately, which reintroduced a window where a crash between
/// the two renames left a new facade beside an old engine — a mismatched pair
/// is worse than an old one, and `verify-pair` afterwards could only report the
/// damage, not undo it.
///
/// `std::fs::rename` is better suited here than the installer's shell: rename(2)
/// operates on the symlink itself, so it replaces `current` atomically. The
/// installer has to `rm -f` first because `mv(1)` follows an existing directory
/// symlink, which leaves a brief window where `current` does not exist. This has
/// no such window.
fn activate_release(release_dir: &std::path::Path) -> Result<()> {
    validate_release_dir(release_dir)?;

    let root = native_root()?;
    std::fs::create_dir_all(&root).with_context(|| format!("create {}", root.display()))?;
    let current = current_link_path()?;

    // Relative target, matching the installer, so the tree stays relocatable.
    let target = std::path::Path::new("releases").join(
        release_dir
            .file_name()
            .context("release directory has no name")?,
    );

    if let Ok(metadata) = std::fs::symlink_metadata(&current) {
        if !metadata.file_type().is_symlink() {
            anyhow::bail!(
                "refusing to replace non-symlink {}; a real directory there is not ours to move",
                current.display()
            );
        }
    }

    let staging = root.join(format!(".current-incoming-{}", std::process::id()));
    std::fs::remove_file(&staging).ok();
    symlink(&target, &staging)
        .with_context(|| format!("stage {} -> {}", staging.display(), target.display()))?;
    // Atomic: readers see either the old release or the new one, never neither.
    std::fs::rename(&staging, &current).with_context(|| {
        let _ = std::fs::remove_file(&staging);
        format!("activate {}", current.display())
    })?;

    ensure_bin_symlinks()?;

    let installed = native_bin_dir()?.join("longhouse");
    let status = std::process::Command::new(&installed)
        .arg("verify-pair")
        .status()
        .with_context(|| format!("run verify-pair from {}", installed.display()))?;
    if !status.success() {
        anyhow::bail!(
            "activated pair failed verify-pair at {}",
            installed.display()
        );
    }
    Ok(())
}

/// Point `~/.local/bin` entries at `current`, matching the installer's layout.
///
/// Refuses to replace an existing binary that does not pass `verify-pair`,
/// exactly as `scripts/install.sh:297-301` does: something else owning that
/// name is not ours to overwrite.
fn ensure_bin_symlinks() -> Result<()> {
    let bin_dir = native_bin_dir()?;
    std::fs::create_dir_all(&bin_dir).with_context(|| format!("create {}", bin_dir.display()))?;
    for name in ["longhouse", "longhouse-engine"] {
        let destination = bin_dir.join(name);
        let target = std::path::Path::new("../share/longhouse/current").join(name);
        if let Ok(existing) = std::fs::read_link(&destination) {
            if existing == target {
                continue;
            }
        } else if destination.exists() {
            // A regular file here is a dogfood install (`dogfood-runtime.sh`
            // replaces these links with real binaries). Only take the name over
            // if what is there is genuinely ours.
            let ours = std::process::Command::new(&destination)
                .arg("verify-pair")
                .status()
                .map(|status| status.success())
                .unwrap_or(false);
            if !ours {
                anyhow::bail!(
                    "refusing to replace {}: it is not a Longhouse native binary",
                    destination.display()
                );
            }
        }
        let staging = bin_dir.join(format!(".{name}-incoming-{}", std::process::id()));
        std::fs::remove_file(&staging).ok();
        symlink(&target, &staging)
            .with_context(|| format!("stage {}", staging.display()))?;
        std::fs::rename(&staging, &destination).with_context(|| {
            let _ = std::fs::remove_file(&staging);
            format!("link {}", destination.display())
        })?;
    }
    Ok(())
}

#[cfg(unix)]
fn symlink(target: &std::path::Path, link: &std::path::Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(not(unix))]
fn symlink(_target: &std::path::Path, _link: &std::path::Path) -> std::io::Result<()> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "native update activation requires a Unix filesystem",
    ))
}

/// Releases present on disk as `(version, directory)`, newest first.
///
/// Directory naming is not a single convention and must not be assumed to be.
/// `scripts/install.sh:281` builds `release_id="${version:-local}-${tmp_dir##*/}"`,
/// so an installer-created directory looks like `0.1.33-tmp.Qbb0vJCYL2`, while
/// [`stage_release`] writes a bare `0.1.33`. Matching on the *parsed* version
/// rather than the literal directory name is what lets rollback reach a release
/// this engine did not install — which is the common case, since the installer
/// put the running binaries there.
///
/// Entries whose name carries no parseable version (the installer's `local-…`
/// dev installs) are skipped: they are not a version anyone can ask for.
pub fn local_releases() -> Vec<(String, std::path::PathBuf)> {
    let Ok(root) = native_release_root() else {
        return Vec::new();
    };
    let Ok(entries) = std::fs::read_dir(&root) else {
        return Vec::new();
    };
    let mut releases: Vec<(String, std::path::PathBuf)> = entries
        .filter_map(|entry| entry.ok())
        .filter(|entry| entry.path().is_dir())
        .filter_map(|entry| {
            let name = entry.file_name().into_string().ok()?;
            let version = release_version_from_dir_name(&name)?;
            let path = entry.path();
            // An interrupted install can leave a directory holding one binary.
            // Offering it would let an incomplete copy mask a complete one.
            (path.join("longhouse").is_file() && path.join("longhouse-engine").is_file())
                .then_some((version, path))
        })
        .collect();
    // Newest first, and numerically: a lexical sort would offer 0.1.9 as newer
    // than 0.1.10 and silently roll a user backwards. Ties break on directory
    // name so the choice among several directories for one version is stable
    // across runs rather than left to readdir order.
    releases.sort_by(|(left_version, left_path), (right_version, right_path)| {
        version_tuple(right_version)
            .cmp(&version_tuple(left_version))
            .then_with(|| right_path.file_name().cmp(&left_path.file_name()))
    });
    releases
}

/// The release version a directory name encodes, if any.
fn release_version_from_dir_name(name: &str) -> Option<String> {
    let (major, minor, patch) = version_tuple(name)?;
    Some(format!("{major}.{minor}.{patch}"))
}

/// Release versions already present on disk, newest first.
///
/// The installer never prunes these, so previous binaries remain available as
/// a rollback source.
pub fn local_release_versions() -> Vec<String> {
    let mut versions: Vec<String> = local_releases().into_iter().map(|(v, _)| v).collect();
    versions.dedup();
    versions
}

/// Roll back to a release already on disk, without touching the network.
///
/// This is the path a badly wedged machine needs, so it must not depend on
/// anything that machine may have lost: no download, no Runtime Host, no
/// GitHub. Only directories the installer already left behind, activated by
/// the same atomic swap [`activate_release`] uses for a forward update.
pub fn rollback_to_local(version: &str) -> Result<()> {
    let _lock = UpdateLock::acquire()?;
    let target = normalize_version(version);
    let releases = local_releases();
    let Some((_, release_dir)) = releases.iter().find(|(candidate, _)| candidate == &target) else {
        let available = local_release_versions().join(", ");
        anyhow::bail!(
            "no complete local release {target} under {}; available: [{available}]",
            native_release_root()?.display()
        );
    };
    activate_release(release_dir)?;
    // The rollback is the new intent for this machine. Leaving the previous
    // status in place would keep advertising a staged upgrade the operator has
    // just deliberately backed out of.
    if let Some(mut status) = read_status() {
        status.restart_required = true;
        status.staged_version = Some(target.clone());
        status.last_apply_error = None;
        status.update_available = None;
        write_status(&status).ok();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Serializes the tests that set process-global environment variables.
    ///
    /// The root override is per-process, so two of these running concurrently
    /// would point at each other's temp directories. Relying on
    /// `--test-threads=1` instead would work locally and fail in CI, which
    /// runs the suite with default parallelism.
    static ENV_GUARD: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// Tests that touch the filesystem run against an overridden root so they
    /// can never modify a real installation. `HOME` was used directly at first,
    /// which made the rollback test non-hermetic.
    struct FakeInstall {
        _guard: std::sync::MutexGuard<'static, ()>,
        _dir: tempfile::TempDir,
        root: std::path::PathBuf,
    }

    impl FakeInstall {
        fn new() -> Self {
            // A panicking test poisons the mutex; recovering keeps one failure
            // from cascading into every other test in the module.
            let guard = ENV_GUARD.lock().unwrap_or_else(|error| error.into_inner());
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path().to_path_buf();
            std::env::set_var("LONGHOUSE_NATIVE_ROOT_OVERRIDE", &root);
            std::fs::create_dir_all(root.join(".local/share/longhouse/releases")).unwrap();
            std::fs::create_dir_all(root.join(".local/bin")).unwrap();
            Self {
                _guard: guard,
                _dir: dir,
                root,
            }
        }

        /// A release directory whose "binaries" are stubs that pass or fail
        /// `verify-pair` on demand, so the swap boundaries can be exercised
        /// without real 17MB artifacts.
        fn release(&self, name: &str, complete: bool, verifies: bool) -> std::path::PathBuf {
            let dir = self
                .root
                .join(".local/share/longhouse/releases")
                .join(name);
            std::fs::create_dir_all(&dir).unwrap();
            let script = if verifies {
                "#!/bin/sh\nexit 0\n"
            } else {
                "#!/bin/sh\nexit 1\n"
            };
            std::fs::write(dir.join("longhouse"), script).unwrap();
            set_executable(&dir.join("longhouse")).unwrap();
            if complete {
                std::fs::write(dir.join("longhouse-engine"), script).unwrap();
                set_executable(&dir.join("longhouse-engine")).unwrap();
            }
            dir
        }

        fn current(&self) -> std::path::PathBuf {
            self.root.join(".local/share/longhouse/current")
        }
    }

    impl Drop for FakeInstall {
        fn drop(&mut self) {
            std::env::remove_var("LONGHOUSE_NATIVE_ROOT_OVERRIDE");
        }
    }

    #[test]
    fn activation_repoints_current_atomically() {
        let install = FakeInstall::new();
        let release = install.release("0.1.34", true, true);
        activate_release(&release).unwrap();
        assert_eq!(
            std::fs::read_link(install.current()).unwrap(),
            std::path::Path::new("releases/0.1.34")
        );
        // Both binaries move together because only the one link changed.
        let bin = install.root.join(".local/bin/longhouse");
        assert_eq!(
            std::fs::read_link(&bin).unwrap(),
            std::path::Path::new("../share/longhouse/current/longhouse")
        );
    }

    #[test]
    fn an_incomplete_release_never_becomes_current() {
        let install = FakeInstall::new();
        let good = install.release("0.1.33", true, true);
        activate_release(&good).unwrap();
        // Missing longhouse-engine: an interrupted download or install.
        let broken = install.release("0.1.34", false, true);
        assert!(activate_release(&broken).is_err());
        // The previous release is still live; a failed activation must not
        // leave the machine pointing at a half-release.
        assert_eq!(
            std::fs::read_link(install.current()).unwrap(),
            std::path::Path::new("releases/0.1.33")
        );
    }

    #[test]
    fn a_release_failing_verify_pair_never_becomes_current() {
        let install = FakeInstall::new();
        let good = install.release("0.1.33", true, true);
        activate_release(&good).unwrap();
        let mismatched = install.release("0.1.34", true, false);
        assert!(activate_release(&mismatched).is_err());
        assert_eq!(
            std::fs::read_link(install.current()).unwrap(),
            std::path::Path::new("releases/0.1.33")
        );
    }

    #[test]
    fn a_real_directory_at_current_is_never_replaced() {
        let install = FakeInstall::new();
        std::fs::create_dir_all(install.current()).unwrap();
        let release = install.release("0.1.34", true, true);
        let error = activate_release(&release).unwrap_err();
        assert!(format!("{error:#}").contains("non-symlink"));
        assert!(install.current().is_dir());
    }

    #[test]
    fn rollback_prefers_a_complete_directory_over_an_incomplete_one() {
        let install = FakeInstall::new();
        // Same version, two directories, as an interrupted installer leaves.
        install.release("0.1.33-tmp.aaa", false, true);
        install.release("0.1.33-tmp.bbb", true, true);
        let found = local_releases();
        assert_eq!(found.len(), 1, "incomplete directories must not be offered");
        assert!(found[0].1.ends_with("0.1.33-tmp.bbb"));
    }

    #[test]
    fn release_listing_is_deterministic_across_runs() {
        let install = FakeInstall::new();
        install.release("0.1.33-tmp.aaa", true, true);
        install.release("0.1.33-tmp.bbb", true, true);
        let first: Vec<_> = local_releases().into_iter().map(|(_, p)| p).collect();
        let second: Vec<_> = local_releases().into_iter().map(|(_, p)| p).collect();
        assert_eq!(first, second, "readdir order must not decide the winner");
    }

    #[test]
    fn a_second_update_operation_is_refused_while_one_holds_the_lock() {
        let _install = FakeInstall::new();
        let temp = tempfile::tempdir().unwrap();
        std::env::set_var("LONGHOUSE_HOME", temp.path());
        let held = UpdateLock::acquire().unwrap();
        assert!(UpdateLock::acquire().is_err());
        drop(held);
        // Released on drop, so a crash mid-update does not wedge the next one
        // beyond the staleness window.
        assert!(UpdateLock::acquire().is_ok());
        std::env::remove_var("LONGHOUSE_HOME");
    }

    #[test]
    fn a_pending_restart_survives_the_next_tick() {
        // Without this the same release reinstalls on every tick forever,
        // because the running version never changes until a restart.
        let previous = UpdateStatus {
            restart_required: true,
            staged_version: Some("0.1.34".to_string()),
            ..UpdateStatus::unchecked(UpdatePolicy::Apply)
        };
        let mut next = UpdateStatus::unchecked(UpdatePolicy::Apply);
        next.installed_version = "0.1.33".to_string();
        carry_pending_restart(&mut next, Some(&previous));
        assert!(next.restart_required);
        assert_eq!(next.staged_version.as_deref(), Some("0.1.34"));
    }

    #[test]
    fn a_pending_restart_retires_once_the_staged_version_is_running() {
        let previous = UpdateStatus {
            restart_required: true,
            staged_version: Some("0.1.34".to_string()),
            ..UpdateStatus::unchecked(UpdatePolicy::Apply)
        };
        let mut next = UpdateStatus::unchecked(UpdatePolicy::Apply);
        next.installed_version = "0.1.34".to_string();
        carry_pending_restart(&mut next, Some(&previous));
        assert!(!next.restart_required);
    }

    #[test]
    fn a_pin_applies_downwards_as_well_as_upwards() {
        // A pin means "run exactly this", which is what makes it usable to hold
        // a cohort on a known-good release after a bad one shipped.
        let mut status = UpdateStatus::unchecked(UpdatePolicy::Apply);
        status.installed_version = "0.1.34".to_string();
        status.pinned_version = Some("0.1.32".to_string());
        status.latest_version = Some("0.1.32".to_string());
        status.update_available = Some(false);
        status.apply_blocked_reason = None;
        assert!(should_apply(&status));
    }

    #[test]
    fn a_pin_at_the_running_version_is_a_no_op() {
        let mut status = UpdateStatus::unchecked(UpdatePolicy::Apply);
        status.installed_version = "0.1.32".to_string();
        status.pinned_version = Some("0.1.32".to_string());
        status.latest_version = Some("0.1.32".to_string());
        assert!(!should_apply(&status));
    }

    #[test]
    fn a_blocked_reason_always_wins_over_a_pin() {
        let mut status = UpdateStatus::unchecked(UpdatePolicy::Apply);
        status.installed_version = "0.1.34".to_string();
        status.pinned_version = Some("0.1.32".to_string());
        status.latest_version = Some("0.1.32".to_string());
        status.apply_blocked_reason = Some("non_release_build".to_string());
        assert!(!should_apply(&status));
    }

    #[test]
    fn an_explicit_apply_preference_does_not_lapse() {
        // Standing user choices are not temporary directives.
        let control = UpdateControl {
            policy: Some("apply".to_string()),
            ..Default::default()
        };
        assert_eq!(
            control.effective_policy(UpdatePolicy::Notify),
            UpdatePolicy::Apply
        );
    }

    #[test]
    fn a_temporary_off_directive_does_lapse() {
        // An operator pause carrying an expiry is exactly what should expire.
        let control = UpdateControl {
            policy: Some("off".to_string()),
            expires_at: Some("2020-01-01T00:00:00Z".to_string()),
            ..Default::default()
        };
        assert_eq!(
            control.effective_policy(UpdatePolicy::Notify),
            UpdatePolicy::Notify
        );
    }

    #[test]
    fn version_comparison_is_numeric_not_lexical() {
        // "0.1.9" > "0.1.10" under string ordering; the whole point of parsing.
        assert_eq!(is_newer("0.1.9", "0.1.10"), Some(true));
        assert_eq!(is_newer("0.1.10", "0.1.9"), Some(false));
        assert_eq!(is_newer("0.1.33", "0.1.33"), Some(false));
        assert_eq!(is_newer("0.1.33", "0.2.0"), Some(true));
    }

    #[test]
    fn unparseable_versions_compare_as_unknown_not_as_current() {
        assert_eq!(is_newer("0.1.33", "nightly"), None);
        assert_eq!(is_newer("", "0.1.34"), None);
        assert_eq!(is_newer("0.1", "0.1.34"), None);
        assert_eq!(is_newer("0.1.33.1", "0.1.34"), None);
    }

    #[test]
    fn prerelease_and_build_metadata_compare_on_the_release_core() {
        assert_eq!(is_newer("0.1.33-dev+abc", "0.1.34"), Some(true));
        assert_eq!(is_newer("0.1.34", "0.1.34-dev+abc"), Some(false));
    }

    #[test]
    fn version_normalization_strips_tag_prefix() {
        assert_eq!(normalize_version("v0.1.34"), "0.1.34");
        assert_eq!(normalize_version("  v0.1.34 "), "0.1.34");
        assert_eq!(normalize_version("0.1.34"), "0.1.34");
    }

    #[test]
    fn policy_parses_documented_vocabulary() {
        assert_eq!(UpdatePolicy::parse("off"), Some(UpdatePolicy::Off));
        assert_eq!(UpdatePolicy::parse("NOTIFY"), Some(UpdatePolicy::Notify));
        assert_eq!(UpdatePolicy::parse(" apply "), Some(UpdatePolicy::Apply));
        assert_eq!(UpdatePolicy::parse("sometimes"), None);
    }

    #[test]
    fn default_policy_never_applies_unasked() {
        assert_eq!(UpdatePolicy::default(), UpdatePolicy::Notify);
    }

    #[test]
    fn expired_control_stops_governing() {
        let control = UpdateControl {
            policy: Some("apply".to_string()),
            expires_at: Some("2020-01-01T00:00:00Z".to_string()),
            ..Default::default()
        };
        assert_eq!(
            control.effective_policy(UpdatePolicy::Notify),
            UpdatePolicy::Notify
        );
    }

    #[test]
    fn unexpired_control_governs() {
        let control = UpdateControl {
            policy: Some("apply".to_string()),
            expires_at: Some("2999-01-01T00:00:00Z".to_string()),
            ..Default::default()
        };
        assert_eq!(
            control.effective_policy(UpdatePolicy::Notify),
            UpdatePolicy::Apply
        );
    }

    #[test]
    fn off_is_sticky_without_an_expiry() {
        // "stop touching my binaries" must not lapse on its own.
        let control = UpdateControl {
            policy: Some("off".to_string()),
            ..Default::default()
        };
        assert_eq!(
            control.effective_policy(UpdatePolicy::Notify),
            UpdatePolicy::Off
        );
    }

    #[test]
    fn launchd_keepalive_true_restarts_a_clean_exit() {
        let plist = "<key>KeepAlive</key>\n\t<true/>\n";
        assert!(definition_restarts_clean_exit(plist));
    }

    #[test]
    fn launchd_keepalive_dictionary_does_not_restart_a_clean_exit() {
        // The Desktop app uses {SuccessfulExit: false} so Quit means quit.
        // Reading that as unconditional would strand a user with no agent.
        let plist = "<key>KeepAlive</key>\n\t<dict>\n\t\t<key>SuccessfulExit</key>\n\t\t<false/>\n\t</dict>\n";
        assert!(!definition_restarts_clean_exit(plist));
    }

    #[test]
    fn systemd_restart_always_restarts_a_clean_exit() {
        assert!(definition_restarts_clean_exit(
            "[Service]\nType=simple\nRestart=always\nRestartSec=10\n"
        ));
    }

    #[test]
    fn systemd_restart_on_failure_does_not_restart_a_clean_exit() {
        // The old generated unit. An engine self-exiting under it never returns,
        // which is the whole reason self-restart is gated on the installed file.
        assert!(!definition_restarts_clean_exit(
            "[Service]\nType=simple\nRestart=on-failure\nRestartSec=10\n"
        ));
    }

    #[test]
    fn a_definition_naming_restart_only_in_a_comment_does_not_count() {
        assert!(!definition_restarts_clean_exit(
            "[Service]\n# Restart=always would be wrong here\nRestart=on-failure\n"
        ));
    }

    #[test]
    fn apply_is_refused_on_non_release_builds() {
        // Guards `make dogfood-refresh` output from being overwritten.
        let reason = apply_blocked_reason(UpdatePolicy::Apply);
        if build_identity::CHANNEL == "release" {
            assert_eq!(reason, None);
        } else {
            assert_eq!(reason.as_deref(), Some("non_release_build"));
        }
    }

    #[test]
    fn notify_policy_reports_why_it_did_not_apply() {
        assert_eq!(
            apply_blocked_reason(UpdatePolicy::Notify).as_deref(),
            Some("policy_notify")
        );
    }

    #[test]
    fn checksum_lookup_matches_installer_parsing() {
        let file = "abc123  longhouse-darwin-arm64\ndef456 *longhouse-engine-darwin-arm64\n";
        assert_eq!(expected_digest(file, "longhouse-darwin-arm64"), Some("abc123"));
        // The `*` binary-mode marker sha256sum emits must not defeat the match.
        assert_eq!(
            expected_digest(file, "longhouse-engine-darwin-arm64"),
            Some("def456")
        );
        assert_eq!(expected_digest(file, "longhouse-linux-x64"), None);
    }

    #[test]
    fn checksum_lookup_does_not_match_on_prefix() {
        let file = "abc123  longhouse-darwin-arm64-extra\n";
        assert_eq!(expected_digest(file, "longhouse-darwin-arm64"), None);
    }

    #[test]
    fn sha256_matches_known_vector() {
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn installer_directory_names_resolve_to_their_version() {
        // scripts/install.sh:281 names directories `<version>-<tmp suffix>`.
        // Rollback must reach those, not only the bare names stage_release
        // writes, or it can never restore a release the installer put there.
        assert_eq!(
            release_version_from_dir_name("0.1.33-tmp.Qbb0vJCYL2").as_deref(),
            Some("0.1.33")
        );
        assert_eq!(
            release_version_from_dir_name("0.1.33").as_deref(),
            Some("0.1.33")
        );
        // The installer's dev-install form carries no version to offer.
        assert_eq!(release_version_from_dir_name("local-tmp.Qbb0vJCYL2"), None);
        assert_eq!(release_version_from_dir_name("releases"), None);
    }

    #[test]
    fn local_release_versions_sort_numerically_newest_first() {
        // Guards the same lexical-vs-numeric trap as version comparison: a
        // rollback offering "0.1.9" as newer than "0.1.10" would downgrade.
        let mut versions = vec!["0.1.9".to_string(), "0.1.10".to_string(), "0.2.0".to_string()];
        versions.sort_by_key(|value| version_tuple(value));
        versions.reverse();
        assert_eq!(versions, vec!["0.2.0", "0.1.10", "0.1.9"]);
    }

    #[test]
    fn native_target_matches_installer_vocabulary() {
        // Names must equal scripts/install.sh native_target(), or the engine
        // downloads assets that were never published under those names.
        if let Some(target) = native_target() {
            assert!(matches!(target, "darwin-arm64" | "linux-x64" | "linux-arm64"));
        }
    }

    #[test]
    fn unchecked_status_does_not_claim_currency() {
        let status = UpdateStatus::unchecked(UpdatePolicy::Notify);
        assert_eq!(status.update_available, None);
        assert_eq!(status.latest_version, None);
        assert!(!status.restart_required);
    }

    #[test]
    fn a_fresh_status_owes_no_restart_and_serializes_without_noise() {
        // restart_required is skipped when false so the common case does not
        // carry a field that reads as an outstanding obligation.
        let status = UpdateStatus::unchecked(UpdatePolicy::Notify);
        let json = serde_json::to_string(&status).unwrap();
        assert!(!json.contains("restart_required"), "{json}");
        assert!(!json.contains("update_available"), "{json}");
    }

    #[test]
    fn restart_required_survives_a_round_trip() {
        // The daemon restarts before the user acts on it; the obligation has to
        // outlive the process that recorded it.
        let mut status = UpdateStatus::unchecked(UpdatePolicy::Apply);
        status.restart_required = true;
        status.staged_version = Some("0.1.34".to_string());
        let json = serde_json::to_string(&status).unwrap();
        let parsed: UpdateStatus = serde_json::from_str(&json).unwrap();
        assert!(parsed.restart_required);
        assert_eq!(parsed.staged_version.as_deref(), Some("0.1.34"));
    }

    #[test]
    fn rollback_names_available_versions_when_the_target_is_absent() {
        // A rollback that fails must say what it could have rolled back to;
        // "not found" alone leaves the operator with no next move.
        let error = rollback_to_local("99.99.99").unwrap_err();
        let message = format!("{error:#}");
        assert!(message.contains("99.99.99"), "{message}");
        assert!(message.contains("available:"), "{message}");
    }
}
