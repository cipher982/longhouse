//! `longhouse uninstall` and the device-token revocation `longhouse auth --clear`
//! shares with it.
//!
//! Removing Longhouse from a computer means more than deleting binaries: the
//! Machine Agent service would keep restarting a missing executable, provider
//! hooks would keep calling it, and the device token would stay valid on the
//! Runtime Host. Each step below undoes one thing the installer or `longhouse
//! claude configure` did, and reports what it did or why it did not.
//!
//! What already reached the Runtime Host is not touched: sessions uploaded
//! earlier stay in that archive until deleted there.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use anyhow::{bail, Context};
use clap::Args;
use serde_json::Value;

const ENGINE_LAUNCHD_LABEL: &str = "com.longhouse.shipper";
const APP_LAUNCHD_LABEL: &str = "ai.longhouse.app";
const APP_BUNDLE_ID: &str = "ai.longhouse.app";
const SYSTEMD_UNIT: &str = "longhouse-shipper.service";

#[derive(Args)]
pub struct UninstallArgs {
    /// Do not ask for confirmation.
    #[arg(long, short = 'y')]
    pub yes: bool,
    /// Show what would be removed without changing anything.
    #[arg(long)]
    pub dry_run: bool,
    /// Also delete local Longhouse state: the upload spool, logs, machine
    /// identity and import scope (everything under ~/.longhouse).
    #[arg(long)]
    pub purge: bool,
    /// Do not revoke the device token on the Runtime Host. It stays valid there
    /// until you revoke it from Settings, Devices.
    #[arg(long)]
    pub local_only: bool,
    /// Leave Longhouse.app in place (macOS).
    #[arg(long)]
    pub keep_app: bool,
}

// ---------------------------------------------------------------------------
// Revocation
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RevokeOutcome {
    /// No device token is stored here.
    NoCredentials,
    /// The Runtime Host revoked it now.
    Revoked,
    /// The Runtime Host no longer accepts it (revoked earlier, or the host was
    /// wiped): nothing left to revoke.
    AlreadyInvalid,
    /// An older Runtime Host without the self-revoke route.
    Unsupported,
    /// The host could not be reached.
    Unreachable(String),
    /// The rule for plain http (`plaintext_http`) forbids sending the token to
    /// the stored address, so nothing was sent.
    Blocked(String),
    /// The host answered with something else.
    Refused(String),
}

impl RevokeOutcome {
    /// Whether the credential is now useless, so deleting the local copy loses
    /// nothing that still needed revoking.
    pub fn token_is_dead(&self) -> bool {
        matches!(
            self,
            Self::NoCredentials | Self::Revoked | Self::AlreadyInvalid
        )
    }

    pub fn describe(&self, base: &str) -> String {
        match self {
            Self::NoCredentials => "no device token is stored on this machine".to_string(),
            Self::Revoked => format!("revoked this machine's device token on {base}"),
            Self::AlreadyInvalid => {
                format!("{base} no longer accepts this machine's device token")
            }
            Self::Unsupported => format!(
                "{base} is too old to revoke a device token from the machine; revoke it at {base}/settings/devices"
            ),
            Self::Unreachable(error) => format!("could not reach {base} to revoke the token ({error})"),
            Self::Blocked(message) => format!("did not send the token to {base}: {message}"),
            Self::Refused(detail) => format!("{base} refused to revoke the token ({detail})"),
        }
    }
}

/// Ask the Runtime Host to revoke exactly the device token this machine holds.
pub fn revoke_device_token(base: &str, token: &str) -> RevokeOutcome {
    let base = base.trim_end_matches('/');
    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(error) => return RevokeOutcome::Unreachable(error.to_string()),
    };
    runtime.block_on(async {
        let response = reqwest::Client::new()
            .delete(format!("{base}/api/agents/device-token"))
            .header("X-Agents-Token", token)
            .timeout(Duration::from_secs(30))
            .send()
            .await;
        match response {
            Err(error) => RevokeOutcome::Unreachable(error.to_string()),
            Ok(response) => match response.status().as_u16() {
                200 | 204 => RevokeOutcome::Revoked,
                401 => RevokeOutcome::AlreadyInvalid,
                404 | 405 => RevokeOutcome::Unsupported,
                status => RevokeOutcome::Refused(format!("HTTP {status}")),
            },
        }
    })
}

/// The device token stored on this machine, if any.
pub fn stored_token(machine_dir: &Path) -> Option<String> {
    let token = std::fs::read_to_string(machine_dir.join("device-token")).ok()?;
    let token = token.trim().to_string();
    (!token.is_empty()).then_some(token)
}

/// The Runtime Host address stored on this machine, if any.
pub fn stored_runtime_url(machine_dir: &Path) -> Option<String> {
    let state: Value =
        serde_json::from_slice(&std::fs::read(machine_dir.join("state.json")).ok()?).ok()?;
    let url = state.get("runtime_url")?.as_str()?.trim().to_string();
    (url.starts_with("http://") || url.starts_with("https://")).then_some(url)
}

/// Revoke this machine's stored device token on its Runtime Host. Also returns
/// the address it asked, for messages.
pub fn revoke_stored_token(machine_dir: &Path) -> (RevokeOutcome, Option<String>) {
    let Some(token) = stored_token(machine_dir) else {
        return (RevokeOutcome::NoCredentials, None);
    };
    // A token with no address cannot be revoked from here, and pretending it
    // was would leave a live credential behind with nothing pointing at it.
    let Some(url) = stored_runtime_url(machine_dir) else {
        return (
            RevokeOutcome::Unreachable(
                "no Runtime Host address is stored with this token".to_string(),
            ),
            None,
        );
    };
    // The token rides this request as a header, so the stored address has to
    // pass the same plain-http rule it did when it was stored.
    if let Err(blocked) = crate::plaintext_http::enforce_for_machine(machine_dir, &url) {
        return (RevokeOutcome::Blocked(blocked.to_string()), Some(url));
    }
    (revoke_device_token(&url, &token), Some(url))
}

// ---------------------------------------------------------------------------
// Layout and effects
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Macos,
    Linux,
    Other,
}

impl Platform {
    fn current() -> Self {
        if cfg!(target_os = "macos") {
            Self::Macos
        } else if cfg!(target_os = "linux") {
            Self::Linux
        } else {
            Self::Other
        }
    }
}

/// Every path an install touches, resolved once so tests can point them at a
/// scratch directory.
#[derive(Debug, Clone)]
pub struct Layout {
    pub platform: Platform,
    pub longhouse_home: PathBuf,
    pub native_bin_dir: PathBuf,
    pub native_root: PathBuf,
    pub launch_agents_dir: PathBuf,
    pub systemd_user_dir: PathBuf,
    pub apps_dir: PathBuf,
    pub claude_dir: PathBuf,
    pub cursor_dir: PathBuf,
    pub gemini_hooks: PathBuf,
}

impl Layout {
    pub fn from_env() -> anyhow::Result<Self> {
        let home = PathBuf::from(std::env::var_os("HOME").context("HOME is not set")?);
        Ok(Self {
            platform: Platform::current(),
            longhouse_home: crate::longhouse_home()?,
            native_bin_dir: home.join(".local").join("bin"),
            native_root: home.join(".local").join("share").join("longhouse"),
            launch_agents_dir: home.join("Library").join("LaunchAgents"),
            systemd_user_dir: home.join(".config").join("systemd").join("user"),
            apps_dir: std::env::var_os("LONGHOUSE_MACOS_APP_INSTALL_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/Applications")),
            claude_dir: std::env::var_os("CLAUDE_CONFIG_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| home.join(".claude")),
            cursor_dir: home.join(".cursor"),
            gemini_hooks: home.join(".gemini").join("config").join("hooks.json"),
        })
    }

    fn machine_dir(&self) -> PathBuf {
        self.longhouse_home.join("machine")
    }
}

/// What uninstall does to the world outside the files it owns, so tests can
/// watch it instead of stopping a real service.
pub trait Effects {
    fn revoke(&mut self, machine_dir: &Path) -> (RevokeOutcome, Option<String>);
    /// Run a service-manager command; whether it succeeded.
    fn run(&mut self, program: &str, args: &[&str]) -> bool;
    fn app_bundle_id(&mut self, app: &Path) -> Option<String>;
}

struct RealEffects;

impl Effects for RealEffects {
    fn revoke(&mut self, machine_dir: &Path) -> (RevokeOutcome, Option<String>) {
        revoke_stored_token(machine_dir)
    }

    fn run(&mut self, program: &str, args: &[&str]) -> bool {
        Command::new(program)
            .args(args)
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status()
            .is_ok_and(|status| status.success())
    }

    fn app_bundle_id(&mut self, app: &Path) -> Option<String> {
        let output = Command::new("/usr/libexec/PlistBuddy")
            .args(["-c", "Print :CFBundleIdentifier"])
            .arg(app.join("Contents").join("Info.plist"))
            .output()
            .ok()?;
        output
            .status
            .success()
            .then(|| String::from_utf8_lossy(&output.stdout).trim().to_string())
    }
}

// ---------------------------------------------------------------------------
// The uninstall itself
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    Done(String),
    /// Nothing to do.
    Absent(String),
    /// Would be done (dry run).
    Planned(String),
    /// Left alone on purpose.
    Kept(String),
    Failed(String),
}

impl Outcome {
    fn line(&self) -> String {
        match self {
            Self::Done(text) => format!("  removed   {text}"),
            Self::Absent(text) => format!("  not found {text}"),
            Self::Planned(text) => format!("  would     {text}"),
            Self::Kept(text) => format!("  kept      {text}"),
            Self::Failed(text) => format!("  FAILED    {text}"),
        }
    }
}

#[derive(Debug, Clone)]
pub struct Options {
    pub dry_run: bool,
    pub purge: bool,
    pub local_only: bool,
    pub keep_app: bool,
}

pub struct Report {
    pub outcomes: Vec<Outcome>,
    /// Set when the device token could not be revoked and was therefore kept.
    pub aborted: Option<String>,
}

impl Report {
    pub fn failed(&self) -> bool {
        self.aborted.is_some()
            || self
                .outcomes
                .iter()
                .any(|outcome| matches!(outcome, Outcome::Failed(_)))
    }
}

pub fn uninstall(layout: &Layout, options: &Options, effects: &mut dyn Effects) -> Report {
    let mut outcomes = Vec::new();
    let apply = !options.dry_run;

    // 1. Revoke first. It needs the token this uninstall is about to delete,
    // and a host that cannot be reached must stop the uninstall before it
    // strips the credentials, not after.
    let machine_dir = layout.machine_dir();
    if options.local_only {
        outcomes.push(Outcome::Kept(
            "device token stays valid on the Runtime Host until revoked in Settings, Devices"
                .to_string(),
        ));
    } else if !apply {
        outcomes.push(match (stored_token(&machine_dir), stored_runtime_url(&machine_dir)) {
            (Some(_), Some(url)) => Outcome::Planned(format!("revoke this machine's device token on {url}")),
            (Some(_), None) => Outcome::Failed(
                "the stored device token has no Runtime Host address, so it cannot be revoked from here".to_string(),
            ),
            (None, _) => Outcome::Absent("device token".to_string()),
        });
    } else {
        let (outcome, url) = effects.revoke(&machine_dir);
        let base = url.as_deref().unwrap_or("the Runtime Host");
        if outcome.token_is_dead() {
            outcomes.push(match outcome {
                RevokeOutcome::NoCredentials => Outcome::Absent("device token".to_string()),
                other => Outcome::Done(other.describe(base)),
            });
        } else {
            return Report {
                outcomes,
                aborted: Some(format!(
                    "{}. Nothing was removed, so the token is still on this machine. Try again when it is reachable, \
or run `longhouse uninstall --local-only` to remove Longhouse and revoke the token yourself.",
                    outcome.describe(base)
                )),
            };
        }
    }

    // 2. Stop the Machine Agent before its files go.
    outcomes.extend(remove_services(layout, apply, effects));
    // 3. Provider hooks that call the binaries being removed.
    outcomes.extend(remove_provider_hooks(layout, apply));
    // 4. Binaries.
    outcomes.extend(remove_binaries(layout, apply));
    // 5. Longhouse.app.
    if layout.platform == Platform::Macos {
        outcomes.push(if options.keep_app {
            Outcome::Kept("Longhouse.app".to_string())
        } else {
            remove_app(layout, apply, effects)
        });
    }
    // 6. State.
    outcomes.extend(remove_state(layout, options, apply));
    Report {
        outcomes,
        aborted: None,
    }
}

fn remove_file_outcome(path: &Path, what: &str, apply: bool) -> Outcome {
    if !path.exists() && path.symlink_metadata().is_err() {
        return Outcome::Absent(format!("{what} ({})", path.display()));
    }
    if !apply {
        return Outcome::Planned(format!("delete {what} ({})", path.display()));
    }
    match std::fs::remove_file(path) {
        Ok(()) => Outcome::Done(format!("{what} ({})", path.display())),
        Err(error) => Outcome::Failed(format!("{what} ({}): {error}", path.display())),
    }
}

fn remove_services(layout: &Layout, apply: bool, effects: &mut dyn Effects) -> Vec<Outcome> {
    let mut outcomes = Vec::new();
    match layout.platform {
        Platform::Macos => {
            for label in [ENGINE_LAUNCHD_LABEL, APP_LAUNCHD_LABEL] {
                let plist = layout.launch_agents_dir.join(format!("{label}.plist"));
                if plist.symlink_metadata().is_err() {
                    outcomes.push(Outcome::Absent(format!("LaunchAgent {label}")));
                    continue;
                }
                if !apply {
                    outcomes.push(Outcome::Planned(format!(
                        "unload and delete LaunchAgent {label}"
                    )));
                    continue;
                }
                let path = plist.display().to_string();
                if !effects.run("launchctl", &["unload", &path]) {
                    // Not loaded is the common reason; the file still goes.
                    outcomes.push(Outcome::Kept(format!(
                        "launchctl did not unload {label} (was it running?)"
                    )));
                }
                outcomes.push(remove_file_outcome(
                    &plist,
                    &format!("LaunchAgent {label}"),
                    true,
                ));
            }
        }
        Platform::Linux => {
            let unit = layout.systemd_user_dir.join(SYSTEMD_UNIT);
            if unit.symlink_metadata().is_err() {
                outcomes.push(Outcome::Absent(format!("systemd user unit {SYSTEMD_UNIT}")));
            } else if !apply {
                outcomes.push(Outcome::Planned(format!(
                    "stop, disable and delete {SYSTEMD_UNIT}"
                )));
            } else {
                if !effects.run("systemctl", &["--user", "disable", "--now", SYSTEMD_UNIT]) {
                    outcomes.push(Outcome::Kept(format!(
                        "systemctl did not stop {SYSTEMD_UNIT} (was it running?)"
                    )));
                }
                outcomes.push(remove_file_outcome(
                    &unit,
                    &format!("systemd user unit {SYSTEMD_UNIT}"),
                    true,
                ));
                effects.run("systemctl", &["--user", "daemon-reload"]);
            }
        }
        Platform::Other => outcomes.push(Outcome::Kept(
            "no service manager known on this platform".to_string(),
        )),
    }
    outcomes
}

/// Provider config files Longhouse merged its hooks into, and what marks a
/// hook as ours. Entries are removed by that marker only; everything else in
/// the user's file stays.
fn hook_files(layout: &Layout) -> Vec<(PathBuf, &'static [&'static str])> {
    vec![
        (
            layout.claude_dir.join("settings.json"),
            &["claude-lifecycle-hook", "claude-permission-gate"][..],
        ),
        (
            layout.cursor_dir.join("hooks.json"),
            &["cursor-lifecycle-hook", "cursor-permission-hook"][..],
        ),
        (layout.gemini_hooks.clone(), &["longhouse-runtime"][..]),
    ]
}

fn remove_provider_hooks(layout: &Layout, apply: bool) -> Vec<Outcome> {
    let mut outcomes = Vec::new();
    for (path, needles) in hook_files(layout) {
        let Ok(raw) = std::fs::read(&path) else {
            outcomes.push(Outcome::Absent(format!(
                "Longhouse hooks in {}",
                path.display()
            )));
            continue;
        };
        let Ok(mut config) = serde_json::from_slice::<Value>(&raw) else {
            outcomes.push(Outcome::Kept(format!(
                "{} is not valid JSON; edit out Longhouse hooks by hand",
                path.display()
            )));
            continue;
        };
        let removed = prune_hooks(&mut config, needles);
        if removed == 0 {
            outcomes.push(Outcome::Absent(format!(
                "Longhouse hooks in {}",
                path.display()
            )));
        } else if !apply {
            outcomes.push(Outcome::Planned(format!(
                "remove {removed} Longhouse hook entries from {}",
                path.display()
            )));
        } else {
            let body = match serde_json::to_string_pretty(&config) {
                Ok(body) => format!("{body}\n"),
                Err(error) => {
                    outcomes.push(Outcome::Failed(format!("{}: {error}", path.display())));
                    continue;
                }
            };
            outcomes.push(match write_atomic(&path, body.as_bytes()) {
                Ok(()) => Outcome::Done(format!(
                    "{removed} Longhouse hook entries from {}",
                    path.display()
                )),
                Err(error) => Outcome::Failed(format!("{}: {error}", path.display())),
            });
        }
    }
    outcomes
}

fn write_atomic(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| std::io::Error::other("path has no parent"))?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("config"),
        std::process::id()
    ));
    std::fs::write(&temporary, bytes)?;
    // Keep the user's own file mode.
    if let Ok(meta) = std::fs::metadata(path) {
        let _ = std::fs::set_permissions(&temporary, meta.permissions());
    }
    std::fs::rename(&temporary, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&temporary);
    })
}

/// Remove hook objects that mention one of `needles`, then any entry or
/// container that was left empty by that removal. Returns how many hook objects
/// went. Anything the removal did not empty is left exactly as it was.
fn prune_hooks(value: &mut Value, needles: &[&str]) -> usize {
    fn is_ours(value: &Value, needles: &[&str]) -> bool {
        value.as_object().is_some_and(|map| {
            map.values().any(|field| {
                field
                    .as_str()
                    .is_some_and(|text| needles.iter().any(|needle| text.contains(needle)))
            })
        })
    }
    fn is_empty_container(value: &Value) -> bool {
        match value {
            Value::Array(items) => items.is_empty(),
            Value::Object(fields) => fields.is_empty(),
            _ => false,
        }
    }
    // An entry like {"matcher": "*"} whose hooks were all ours has nothing left
    // to run.
    fn has_content(value: &Value) -> bool {
        value.as_object().is_some_and(|map| {
            map.values().any(|field| {
                matches!(field, Value::Array(_) | Value::Object(_)) && !is_empty_container(field)
            })
        })
    }
    match value {
        Value::Array(items) => {
            let mut removed = 0;
            let mut kept = Vec::with_capacity(items.len());
            for mut item in items.drain(..) {
                if is_ours(&item, needles) {
                    removed += 1;
                    continue;
                }
                let inside = prune_hooks(&mut item, needles);
                removed += inside;
                if inside > 0 && !has_content(&item) {
                    continue;
                }
                kept.push(item);
            }
            *items = kept;
            removed
        }
        Value::Object(map) => {
            let mut removed = 0;
            let mut emptied = Vec::new();
            for (key, child) in map.iter_mut() {
                let inside = prune_hooks(child, needles);
                removed += inside;
                if inside > 0 && is_empty_container(child) {
                    emptied.push(key.clone());
                }
            }
            for key in emptied {
                map.remove(&key);
            }
            removed
        }
        _ => 0,
    }
}

fn remove_binaries(layout: &Layout, apply: bool) -> Vec<Outcome> {
    let mut outcomes = Vec::new();
    for name in ["longhouse", "longhouse-engine"] {
        let link = layout.native_bin_dir.join(name);
        match std::fs::read_link(&link) {
            Ok(target)
                if target.starts_with("../share/longhouse")
                    || link
                        .parent()
                        .map(|dir| dir.join(&target))
                        .is_some_and(|resolved| resolved.starts_with(&layout.native_root)) =>
            {
                outcomes.push(remove_file_outcome(&link, name, apply));
            }
            Ok(target) => outcomes.push(Outcome::Kept(format!(
                "{} points at {}, which Longhouse did not install",
                link.display(),
                target.display()
            ))),
            Err(_) if link.exists() => outcomes.push(Outcome::Kept(format!(
                "{} is a plain file, not a link the installer made",
                link.display()
            ))),
            Err(_) => outcomes.push(Outcome::Absent(name.to_string())),
        }
    }
    let looks_ours = layout
        .native_root
        .join("current")
        .symlink_metadata()
        .is_ok()
        || layout.native_root.join("releases").is_dir();
    if !layout.native_root.exists() {
        outcomes.push(Outcome::Absent(format!(
            "installed releases ({})",
            layout.native_root.display()
        )));
    } else if !looks_ours {
        outcomes.push(Outcome::Kept(format!(
            "{} does not look like a Longhouse install",
            layout.native_root.display()
        )));
    } else if !apply {
        outcomes.push(Outcome::Planned(format!(
            "delete installed releases ({})",
            layout.native_root.display()
        )));
    } else {
        outcomes.push(match std::fs::remove_dir_all(&layout.native_root) {
            Ok(()) => Outcome::Done(format!(
                "installed releases ({})",
                layout.native_root.display()
            )),
            Err(error) => Outcome::Failed(format!("{}: {error}", layout.native_root.display())),
        });
    }
    outcomes
}

fn remove_app(layout: &Layout, apply: bool, effects: &mut dyn Effects) -> Outcome {
    let app = layout.apps_dir.join("Longhouse.app");
    if app.symlink_metadata().is_err() {
        return Outcome::Absent(format!("Longhouse.app ({})", app.display()));
    }
    // Never delete an application that only shares the name.
    if effects.app_bundle_id(&app).as_deref() != Some(APP_BUNDLE_ID) {
        return Outcome::Kept(format!("{} is not the Longhouse app", app.display()));
    }
    if !apply {
        return Outcome::Planned(format!("quit and delete Longhouse.app ({})", app.display()));
    }
    effects.run(
        "osascript",
        &[
            "-e",
            &format!("tell application id \"{APP_BUNDLE_ID}\" to quit"),
        ],
    );
    match std::fs::remove_dir_all(&app) {
        Ok(()) => Outcome::Done(format!("Longhouse.app ({})", app.display())),
        Err(error) => Outcome::Failed(format!("{}: {error}", app.display())),
    }
}

fn remove_state(layout: &Layout, options: &Options, apply: bool) -> Vec<Outcome> {
    let home = &layout.longhouse_home;
    if !options.purge {
        // The token is a credential; it goes even when the rest stays.
        let mut outcomes = vec![remove_file_outcome(
            &layout.machine_dir().join("device-token"),
            "device token",
            apply,
        )];
        outcomes.push(Outcome::Kept(format!(
            "local state in {} (upload spool, logs, import scope); `longhouse uninstall --purge` deletes it",
            home.display()
        )));
        return outcomes;
    }
    if !home.exists() {
        return vec![Outcome::Absent(format!("local state ({})", home.display()))];
    }
    // The state directory is only ever deleted when it is recognisably ours.
    let recognisable = home.join("machine").is_dir() || home.join("agent").is_dir();
    let dangerous = home.parent().is_none()
        || std::env::var_os("HOME").is_some_and(|value| Path::new(&value) == home.as_path());
    if !recognisable || dangerous {
        return vec![Outcome::Kept(format!(
            "{} does not look like a Longhouse state directory",
            home.display()
        ))];
    }
    if !apply {
        return vec![Outcome::Planned(format!(
            "delete local state ({})",
            home.display()
        ))];
    }
    vec![match std::fs::remove_dir_all(home) {
        Ok(()) => Outcome::Done(format!("local state ({})", home.display())),
        Err(error) => Outcome::Failed(format!("{}: {error}", home.display())),
    }]
}

// ---------------------------------------------------------------------------
// Command entry
// ---------------------------------------------------------------------------

pub fn run(args: UninstallArgs) -> anyhow::Result<()> {
    let layout = Layout::from_env()?;
    let options = Options {
        dry_run: args.dry_run,
        purge: args.purge,
        local_only: args.local_only,
        keep_app: args.keep_app,
    };
    if !args.dry_run && !args.yes {
        if !crate::interactive_stdio() {
            bail!("uninstall changes this machine; pass --yes to confirm, or --dry-run to preview");
        }
        let plan = uninstall(
            &layout,
            &Options {
                dry_run: true,
                ..options.clone()
            },
            &mut RealEffects,
        );
        println!("Longhouse will be removed from this computer:");
        for outcome in &plan.outcomes {
            println!("{}", outcome.line());
        }
        print!("Continue? [y/N] ");
        std::io::Write::flush(&mut std::io::stdout())?;
        let mut answer = String::new();
        std::io::stdin().read_line(&mut answer)?;
        if !matches!(answer.trim().to_ascii_lowercase().as_str(), "y" | "yes") {
            println!("Nothing changed.");
            return Ok(());
        }
    }
    let report = uninstall(&layout, &options, &mut RealEffects);
    if args.dry_run {
        println!("Dry run: nothing was changed. This would happen:");
    }
    for outcome in &report.outcomes {
        println!("{}", outcome.line());
    }
    if let Some(reason) = &report.aborted {
        bail!("{reason}");
    }
    if report.failed() {
        bail!("some steps failed; see above");
    }
    if !args.dry_run {
        println!();
        println!("Longhouse is removed from this computer.");
        println!("Sessions already uploaded stay in your Runtime Host's archive until you delete them there.");
        println!("The PATH line the installer added to your shell profile is left alone; it is harmless.");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;

    #[test]
    fn revoke_never_sends_the_token_to_an_address_the_plain_http_rule_forbids() {
        for stored in ["http://demo.longhouse.ai", "http://192.168.1.20:8080"] {
            let dir = tempfile::tempdir().unwrap();
            fs::write(dir.path().join("device-token"), "zdt_abc").unwrap();
            fs::write(
                dir.path().join("state.json"),
                serde_json::json!({"runtime_url": stored}).to_string(),
            )
            .unwrap();
            temp_env::with_var_unset(crate::plaintext_http::OPT_IN_ENV, || {
                let (outcome, url) = revoke_stored_token(dir.path());
                assert!(
                    matches!(&outcome, RevokeOutcome::Blocked(message) if message.contains("Refusing plaintext")),
                    "{stored}: {outcome:?}"
                );
                assert!(!outcome.token_is_dead());
                assert_eq!(url.as_deref(), Some(stored));
            });
        }
    }

    #[derive(Default)]
    struct FakeEffects {
        revoke_outcome: Option<RevokeOutcome>,
        commands: Vec<String>,
        bundle_id: Option<String>,
        revoked_from: Vec<PathBuf>,
    }

    impl Effects for FakeEffects {
        fn revoke(&mut self, machine_dir: &Path) -> (RevokeOutcome, Option<String>) {
            self.revoked_from.push(machine_dir.to_path_buf());
            let outcome = self
                .revoke_outcome
                .clone()
                .unwrap_or(RevokeOutcome::Revoked);
            (outcome, Some("https://you.longhouse.ai".to_string()))
        }
        fn run(&mut self, program: &str, args: &[&str]) -> bool {
            self.commands.push(format!("{program} {}", args.join(" ")));
            true
        }
        fn app_bundle_id(&mut self, _app: &Path) -> Option<String> {
            self.bundle_id.clone()
        }
    }

    fn layout(root: &Path, platform: Platform) -> Layout {
        Layout {
            platform,
            longhouse_home: root.join(".longhouse"),
            native_bin_dir: root.join(".local/bin"),
            native_root: root.join(".local/share/longhouse"),
            launch_agents_dir: root.join("Library/LaunchAgents"),
            systemd_user_dir: root.join(".config/systemd/user"),
            apps_dir: root.join("Applications"),
            claude_dir: root.join(".claude"),
            cursor_dir: root.join(".cursor"),
            gemini_hooks: root.join(".gemini/config/hooks.json"),
        }
    }

    /// A machine as the installer and `longhouse claude configure` leave it.
    fn installed_machine(root: &Path, platform: Platform) -> Layout {
        let layout = layout(root, platform);
        let write = |path: &Path, body: &str| {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, body).unwrap();
        };
        write(
            &layout.longhouse_home.join("machine/device-token"),
            "zdt_secret\n",
        );
        write(
            &layout.longhouse_home.join("machine/state.json"),
            "{\"runtime_url\":\"https://you.longhouse.ai\"}",
        );
        write(
            &layout.longhouse_home.join("agent/longhouse-shipper.db"),
            "db",
        );
        write(
            &layout.native_root.join("releases/0.1.60-x/longhouse"),
            "bin",
        );
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink("releases/0.1.60-x", layout.native_root.join("current"))
                .unwrap();
            fs::create_dir_all(&layout.native_bin_dir).unwrap();
            for name in ["longhouse", "longhouse-engine"] {
                std::os::unix::fs::symlink(
                    format!("../share/longhouse/current/{name}"),
                    layout.native_bin_dir.join(name),
                )
                .unwrap();
            }
        }
        write(
            &layout.launch_agents_dir.join("com.longhouse.shipper.plist"),
            "<plist/>",
        );
        write(
            &layout.launch_agents_dir.join("ai.longhouse.app.plist"),
            "<plist/>",
        );
        write(
            &layout.systemd_user_dir.join("longhouse-shipper.service"),
            "[Unit]",
        );
        write(
            &layout.claude_dir.join("settings.json"),
            &serde_json::json!({
                "model": "opus",
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "my-own-hook.sh"}]},
                        {"hooks": [{"type": "command", "command": "'/x/longhouse-engine' claude-lifecycle-hook"}]}
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "'/x/longhouse-engine' claude-lifecycle-hook"}]}
                    ],
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "'/x/longhouse-engine' claude-permission-gate"}]}
                    ]
                }
            })
            .to_string(),
        );
        fs::create_dir_all(layout.apps_dir.join("Longhouse.app/Contents")).unwrap();
        layout
    }

    fn options() -> Options {
        Options {
            dry_run: false,
            purge: false,
            local_only: false,
            keep_app: false,
        }
    }

    #[test]
    fn uninstall_revokes_first_then_removes_service_hooks_binaries_and_the_token() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Macos);
        let mut effects = FakeEffects {
            bundle_id: Some(APP_BUNDLE_ID.to_string()),
            ..Default::default()
        };
        let report = uninstall(&layout, &options(), &mut effects);
        assert!(!report.failed(), "{:#?}", report.outcomes);

        assert_eq!(
            effects.revoked_from,
            vec![layout.machine_dir()],
            "revoked before anything was deleted"
        );
        assert_eq!(
            effects.commands,
            vec![
                format!(
                    "launchctl unload {}",
                    layout
                        .launch_agents_dir
                        .join("com.longhouse.shipper.plist")
                        .display()
                ),
                format!(
                    "launchctl unload {}",
                    layout
                        .launch_agents_dir
                        .join("ai.longhouse.app.plist")
                        .display()
                ),
                format!("osascript -e tell application id \"{APP_BUNDLE_ID}\" to quit"),
            ]
        );
        assert!(!layout
            .launch_agents_dir
            .join("com.longhouse.shipper.plist")
            .exists());
        assert!(!layout
            .launch_agents_dir
            .join("ai.longhouse.app.plist")
            .exists());
        assert!(layout
            .native_bin_dir
            .join("longhouse")
            .symlink_metadata()
            .is_err());
        assert!(layout
            .native_bin_dir
            .join("longhouse-engine")
            .symlink_metadata()
            .is_err());
        assert!(!layout.native_root.exists());
        assert!(!layout.apps_dir.join("Longhouse.app").exists());
        assert!(
            !layout.machine_dir().join("device-token").exists(),
            "the credential never outlives the uninstall"
        );
        assert!(
            layout.machine_dir().join("state.json").exists(),
            "local state stays without --purge"
        );
        assert!(layout
            .longhouse_home
            .join("agent/longhouse-shipper.db")
            .exists());

        // The user's own hook and settings survive; ours are gone.
        let settings: Value =
            serde_json::from_slice(&fs::read(layout.claude_dir.join("settings.json")).unwrap())
                .unwrap();
        assert_eq!(settings["model"], "opus");
        assert_eq!(
            settings["hooks"],
            serde_json::json!({"SessionStart": [{"hooks": [{"type": "command", "command": "my-own-hook.sh"}]}]})
        );
    }

    #[test]
    fn linux_stops_and_removes_the_systemd_unit() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Linux);
        let mut effects = FakeEffects::default();
        let report = uninstall(&layout, &options(), &mut effects);
        assert!(!report.failed(), "{:#?}", report.outcomes);
        assert_eq!(
            effects.commands,
            vec![
                "systemctl --user disable --now longhouse-shipper.service".to_string(),
                "systemctl --user daemon-reload".to_string()
            ]
        );
        assert!(!layout
            .systemd_user_dir
            .join("longhouse-shipper.service")
            .exists());
        // No macOS artifacts are touched on Linux.
        assert!(layout
            .launch_agents_dir
            .join("com.longhouse.shipper.plist")
            .exists());
        assert!(layout.apps_dir.join("Longhouse.app").exists());
    }

    #[test]
    fn an_unreachable_host_stops_the_uninstall_before_anything_is_removed() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Macos);
        let mut effects = FakeEffects {
            revoke_outcome: Some(RevokeOutcome::Unreachable("connection refused".into())),
            ..Default::default()
        };
        let report = uninstall(&layout, &options(), &mut effects);
        assert!(report
            .aborted
            .as_deref()
            .unwrap()
            .contains("connection refused"));
        assert!(effects.commands.is_empty(), "no service was touched");
        assert!(
            layout.machine_dir().join("device-token").exists(),
            "token kept so it can be revoked later"
        );
        assert!(layout.native_root.exists());
    }

    #[test]
    fn local_only_skips_revocation_and_says_the_token_stays_valid() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Macos);
        let mut effects = FakeEffects::default();
        let report = uninstall(
            &layout,
            &Options {
                local_only: true,
                ..options()
            },
            &mut effects,
        );
        assert!(effects.revoked_from.is_empty());
        assert!(report
            .outcomes
            .iter()
            .any(|outcome| matches!(outcome, Outcome::Kept(text) if text.contains("stays valid"))));
        assert!(!layout.machine_dir().join("device-token").exists());
    }

    #[test]
    fn a_token_the_host_already_rejects_does_not_block_the_uninstall() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Linux);
        let mut effects = FakeEffects {
            revoke_outcome: Some(RevokeOutcome::AlreadyInvalid),
            ..Default::default()
        };
        assert!(!uninstall(&layout, &options(), &mut effects).failed());
        assert!(!layout.native_root.exists());
    }

    #[test]
    fn dry_run_changes_nothing_and_lists_what_would_happen() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Macos);
        let mut effects = FakeEffects {
            bundle_id: Some(APP_BUNDLE_ID.to_string()),
            ..Default::default()
        };
        let report = uninstall(
            &layout,
            &Options {
                dry_run: true,
                purge: true,
                ..options()
            },
            &mut effects,
        );
        assert!(effects.commands.is_empty() && effects.revoked_from.is_empty());
        assert!(layout.machine_dir().join("device-token").exists());
        assert!(layout.native_root.exists());
        assert!(layout.claude_dir.join("settings.json").exists());
        assert!(
            report
                .outcomes
                .iter()
                .all(|outcome| !matches!(outcome, Outcome::Done(_))),
            "{:#?}",
            report.outcomes
        );
        assert!(report
            .outcomes
            .iter()
            .any(|outcome| matches!(outcome, Outcome::Planned(text) if text.contains("revoke"))));
        let settings = fs::read_to_string(layout.claude_dir.join("settings.json")).unwrap();
        assert!(settings.contains("claude-lifecycle-hook"));
    }

    #[test]
    fn purge_deletes_local_state_but_only_when_it_is_recognisably_ours() {
        let root = tempfile::tempdir().unwrap();
        let layout = installed_machine(root.path(), Platform::Linux);
        let mut effects = FakeEffects::default();
        let report = uninstall(
            &layout,
            &Options {
                purge: true,
                ..options()
            },
            &mut effects,
        );
        assert!(!report.failed(), "{:#?}", report.outcomes);
        assert!(!layout.longhouse_home.exists());

        // A directory that merely has the right name is left alone.
        let stray = tempfile::tempdir().unwrap();
        let mut layout = installed_machine(stray.path(), Platform::Linux);
        layout.longhouse_home = stray.path().join("documents");
        fs::create_dir_all(&layout.longhouse_home).unwrap();
        fs::write(layout.longhouse_home.join("thesis.txt"), "keep me").unwrap();
        let report = uninstall(
            &layout,
            &Options {
                purge: true,
                ..options()
            },
            &mut FakeEffects::default(),
        );
        assert!(layout.longhouse_home.join("thesis.txt").exists());
        assert!(report
            .outcomes
            .iter()
            .any(|outcome| matches!(outcome, Outcome::Kept(text) if text.contains("does not look like a Longhouse state directory"))));
    }

    #[test]
    fn binaries_and_apps_that_are_not_ours_are_left_in_place() {
        let root = tempfile::tempdir().unwrap();
        let layout = layout(root.path(), Platform::Macos);
        fs::create_dir_all(&layout.native_bin_dir).unwrap();
        fs::write(
            layout.native_bin_dir.join("longhouse"),
            "someone else's script",
        )
        .unwrap();
        #[cfg(unix)]
        std::os::unix::fs::symlink(
            "/opt/tool/longhouse-engine",
            layout.native_bin_dir.join("longhouse-engine"),
        )
        .unwrap();
        fs::create_dir_all(layout.apps_dir.join("Longhouse.app")).unwrap();
        let mut effects = FakeEffects {
            bundle_id: Some("com.example.other".into()),
            ..Default::default()
        };
        let report = uninstall(&layout, &options(), &mut effects);
        assert!(layout.native_bin_dir.join("longhouse").exists());
        assert!(layout
            .native_bin_dir
            .join("longhouse-engine")
            .symlink_metadata()
            .is_ok());
        assert!(layout.apps_dir.join("Longhouse.app").exists());
        assert!(!effects
            .commands
            .iter()
            .any(|command| command.starts_with("osascript")));
        assert!(!report.failed(), "{:#?}", report.outcomes);
    }

    #[test]
    fn pruning_hooks_keeps_everything_that_is_not_ours() {
        let mut config = serde_json::json!({
            "version": 1,
            "hooks": {
                "stop": [
                    {"command": "'/x/longhouse-engine' cursor-lifecycle-hook stop", "timeout": 5},
                    {"command": "my-formatter"}
                ],
                "beforeShellExecution": [
                    {"command": "'/x/longhouse-engine' cursor-permission-hook beforeShellExecution"}
                ]
            }
        });
        let removed = prune_hooks(
            &mut config,
            &["cursor-lifecycle-hook", "cursor-permission-hook"],
        );
        assert_eq!(removed, 2);
        assert_eq!(
            config,
            serde_json::json!({"version": 1, "hooks": {"stop": [{"command": "my-formatter"}]}})
        );
        // Nothing of ours in the file: nothing changes, not even an empty container.
        let mut untouched = serde_json::json!({"hooks": {}, "other": []});
        assert_eq!(prune_hooks(&mut untouched, &["cursor-lifecycle-hook"]), 0);
        assert_eq!(untouched, serde_json::json!({"hooks": {}, "other": []}));
    }

    /// Serve one canned response and report the request line and token header.
    fn one_shot_host(
        status_line: &'static str,
    ) -> (String, std::thread::JoinHandle<(String, Option<String>)>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buffer = [0_u8; 4096];
            let read = stream.read(&mut buffer).unwrap();
            let request = String::from_utf8_lossy(&buffer[..read]).to_string();
            let request_line = request.lines().next().unwrap_or_default().to_string();
            let token = request.lines().find_map(|line| {
                line.to_ascii_lowercase()
                    .starts_with("x-agents-token:")
                    .then(|| line.split_once(':').unwrap().1.trim().to_string())
            });
            stream
                .write_all(
                    format!(
                        "HTTP/1.1 {status_line}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                    )
                    .as_bytes(),
                )
                .unwrap();
            (request_line, token)
        });
        (base, handle)
    }

    #[test]
    fn revocation_sends_the_stored_token_to_the_self_revoke_route_and_classifies_the_answer() {
        for (status, expected) in [
            ("204 No Content", RevokeOutcome::Revoked),
            ("401 Unauthorized", RevokeOutcome::AlreadyInvalid),
            ("404 Not Found", RevokeOutcome::Unsupported),
            (
                "500 Internal Server Error",
                RevokeOutcome::Refused("HTTP 500".into()),
            ),
        ] {
            let (base, host) = one_shot_host(status);
            assert_eq!(revoke_device_token(&base, "zdt_abc"), expected, "{status}");
            let (request_line, token) = host.join().unwrap();
            assert_eq!(request_line, "DELETE /api/agents/device-token HTTP/1.1");
            assert_eq!(token.as_deref(), Some("zdt_abc"));
        }
        assert!(matches!(
            revoke_device_token("http://127.0.0.1:1", "zdt_abc"),
            RevokeOutcome::Unreachable(_)
        ));
    }

    #[test]
    fn a_stored_token_without_an_address_cannot_be_revoked_and_is_not_called_absent() {
        let dir = tempfile::tempdir().unwrap();
        let machine = dir.path().join("machine");
        assert_eq!(
            revoke_stored_token(&machine).0,
            RevokeOutcome::NoCredentials
        );
        fs::create_dir_all(&machine).unwrap();
        fs::write(machine.join("device-token"), "zdt_abc\n").unwrap();
        assert!(matches!(
            revoke_stored_token(&machine).0,
            RevokeOutcome::Unreachable(_)
        ));
        fs::write(machine.join("state.json"), "{\"runtime_url\":null}").unwrap();
        assert!(matches!(
            revoke_stored_token(&machine).0,
            RevokeOutcome::Unreachable(_)
        ));
        fs::write(
            machine.join("state.json"),
            "{\"runtime_url\":\"https://you.longhouse.ai/\"}",
        )
        .unwrap();
        assert_eq!(
            stored_runtime_url(&machine).as_deref(),
            Some("https://you.longhouse.ai/")
        );
        assert_eq!(stored_token(&machine).as_deref(), Some("zdt_abc"));
    }
}
