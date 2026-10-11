//! Owner input for Claude Helm through Longhouse's Claude Code mod.
//!
//! Claude Code labels every channel message "NOT from your user", so a message
//! the owner sent from the Longhouse app reached Claude as untrusted outside
//! input. Since Claude Code 2.1.287 a mod (a plugin whose JavaScript runs inside
//! Claude Code) can submit a prompt as the user's own words with
//! `$.prompt.submit({ text, asUser: true })`. `longhouse claude` loads
//! Longhouse's mod (`engine/assets/claude-mod`) with `--plugin-dir`; the mod
//! runs `longhouse-engine claude-owner-input watch`, which streams this
//! session's owner inputs, and reports each step with `... ack`
//! (control-plane `docs/specs/claude-owner-input-delivery.md`, option e).
//!
//! The engine owns delivery, in one directory per session,
//! `<state_root>/sessions/<session>.owner-input/`:
//!
//! - `route.json`: the launch's decision, mod or channel, and why.
//! - `watch.lock`: held by the live watcher, so a free lock means no mod is
//!   attached.
//! - `pending/<seq>-<id>.json`: enqueued and not yet taken by the mod. Only
//!   pending inputs are offered as new, so a mod reload never submits twice.
//! - `claimed/<seq>-<id>.json`: the mod took it and submits it.
//! - `ledger.jsonl`: every step, appended: `claimed`, `submitted` (Claude Code
//!   queued the prompt), `delivered` (its turn started), `failed`, `aborted`,
//!   `no_turn`.
//!
//! Only owner input takes this path. Peer messages carry the Runtime Host's
//! `[Longhouse directed input]` envelope and stay on the labelled channel; the
//! channel control is the one place that decides, and it never enqueues an
//! envelope here.

use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::json;

/// The owner's input goes through the mod as the user's own words.
pub const PATH_MOD: &str = "claude_mod";
/// The owner's input goes through the channel, labelled as outside input.
pub const PATH_CHANNEL: &str = "channel";

/// The first Claude Code with `$.prompt.submit({ asUser: true })`.
pub const MIN_CLAUDE_CODE_VERSION: (u64, u64, u64) = (2, 1, 287);

/// The mod's name. Claude Code shows "Prompt from the longhouse plugin" above
/// each message it submits, so the person at the terminal sees where it came
/// from; the model reads the owner's text alone.
pub const MOD_NAME: &str = "longhouse";

/// Environment the launcher sets for the mod: the engine it runs.
pub const ENGINE_ENV: &str = "LONGHOUSE_OWNER_INPUT_ENGINE";

const ROUTE_FILE: &str = "route.json";
const LOCK_FILE: &str = "watch.lock";
const LEDGER_FILE: &str = "ledger.jsonl";
const PENDING_DIR: &str = "pending";
const CLAIMED_DIR: &str = "claimed";

/// Ledger states that end an input. A claimed file is removed once one lands.
const TERMINAL_STATES: &[&str] = &["delivered", "failed", "aborted", "no_turn"];
/// States the mod may record with `ack`. `claimed` goes through [`claim`].
pub const ACK_STATES: &[&str] = &["submitted", "delivered", "failed", "aborted", "no_turn"];

const POLL_INTERVAL: Duration = Duration::from_millis(50);
/// After the mod claimed an input it submits it at once; this bounds how long
/// the engine waits for that report before calling the outcome unknown.
const CLAIMED_GRACE: Duration = Duration::from_secs(5);

// --- The launch decision ----------------------------------------------------

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct OwnerInputRoute {
    /// [`PATH_MOD`] or [`PATH_CHANNEL`].
    pub path: String,
    /// Why the channel, when it is the channel.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claude_code_version: Option<String>,
    pub decided_at: String,
}

impl OwnerInputRoute {
    pub fn uses_mod(&self) -> bool {
        self.path == PATH_MOD
    }
}

/// `claude --version` prints `2.1.296 (Claude Code)`.
pub fn parse_claude_code_version(output: &str) -> Option<(u64, u64, u64)> {
    let token = output.split_whitespace().next()?;
    let mut parts = token.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts
        .next()?
        .split(|c: char| !c.is_ascii_digit())
        .next()?
        .parse()
        .ok()?;
    Some((major, minor, patch))
}

/// The route a launch takes: the mod when this Claude Code can submit as the
/// user and nothing turns mods off, otherwise the channel with the reason.
pub fn decide_route(version_output: Option<&str>, blocked_by: Option<&str>) -> OwnerInputRoute {
    let version = version_output
        .and_then(|output| output.split_whitespace().next())
        .map(str::to_owned);
    let parsed = version_output.and_then(parse_claude_code_version);
    let reason = match (parsed, blocked_by) {
        (None, _) => Some("claude_code_version_unknown".to_string()),
        (Some(found), _) if found < MIN_CLAUDE_CODE_VERSION => {
            Some("claude_code_predates_mods".to_string())
        }
        (Some(_), Some(setting)) => Some(format!("mods_disabled_by_settings:{setting}")),
        (Some(_), None) => None,
    };
    OwnerInputRoute {
        path: if reason.is_none() {
            PATH_MOD
        } else {
            PATH_CHANNEL
        }
        .to_string(),
        reason,
        claude_code_version: version,
        decided_at: chrono::Utc::now().to_rfc3339(),
    }
}

/// The settings that stop a `--plugin-dir` mod from running. Managed settings
/// can reject the flag outright (`disableSideloadFlags`), and then Claude does
/// not start at all, so they are read before the flag is passed. The user's own
/// `disableAllHooks` also turns installed mods off.
const MOD_BLOCKING_MANAGED_KEYS: &[&str] = &[
    "disableSideloadFlags",
    "allowManagedModsOnly",
    "allowManagedHooksOnly",
    "disableAllHooks",
];

pub fn managed_settings_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    if cfg!(target_os = "macos") {
        paths.push(PathBuf::from(
            "/Library/Application Support/ClaudeCode/managed-settings.json",
        ));
    } else {
        paths.push(PathBuf::from("/etc/claude-code/managed-settings.json"));
    }
    paths
}

/// The first setting, in managed settings or the user's own, that keeps the
/// mod from running; `None` when nothing does.
pub fn mod_blocking_setting(managed: &[PathBuf], user_settings: Option<&Path>) -> Option<String> {
    for path in managed {
        let Some(value) = read_json(path) else {
            continue;
        };
        for key in MOD_BLOCKING_MANAGED_KEYS {
            if json_flag_set(&value, key) {
                return Some((*key).to_string());
            }
        }
    }
    let user = user_settings.and_then(read_json)?;
    (user.get("disableAllHooks") == Some(&json!(true))).then(|| "disableAllHooks".to_string())
}

fn read_json(path: &Path) -> Option<serde_json::Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}

/// `allowManagedModsOnly` is an option on the built-in guard, so it can sit
/// below the top level; any `true` under that name counts.
fn json_flag_set(value: &serde_json::Value, key: &str) -> bool {
    match value {
        serde_json::Value::Object(map) => map.iter().any(|(name, inner)| {
            (name == key && inner == &json!(true)) || json_flag_set(inner, key)
        }),
        serde_json::Value::Array(items) => items.iter().any(|inner| json_flag_set(inner, key)),
        _ => false,
    }
}

// --- The spool --------------------------------------------------------------

/// `<state_root>/sessions/<session>.owner-input`, beside the channel state file.
pub fn spool_dir(state_root: &Path, session_id: &str) -> PathBuf {
    state_root
        .join("sessions")
        .join(format!("{session_id}.owner-input"))
}

pub fn write_route(dir: &Path, route: &OwnerInputRoute) -> io::Result<()> {
    fs::create_dir_all(dir)?;
    write_atomic(&dir.join(ROUTE_FILE), &serde_json::to_vec_pretty(route)?)
}

pub fn read_route(dir: &Path) -> Option<OwnerInputRoute> {
    serde_json::from_slice(&fs::read(dir.join(ROUTE_FILE)).ok()?).ok()
}

/// Whether a watcher holds the lock, which is whether the mod is attached.
#[cfg(unix)]
pub fn watcher_attached(dir: &Path) -> bool {
    use std::os::fd::AsRawFd;
    let Ok(file) = File::open(dir.join(LOCK_FILE)) else {
        return false;
    };
    // Taking the lock means nobody holds it; drop releases it again.
    unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) != 0 }
}

#[cfg(not(unix))]
pub fn watcher_attached(_dir: &Path) -> bool {
    false
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct OwnerInputItem {
    pub id: String,
    /// `send` or `abort`.
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    pub enqueued_at: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LedgerEntry {
    pub id: String,
    pub state: String,
    pub at: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<String>,
}

/// File names sort in arrival order and carry the id, so the id alone finds an
/// input in either directory.
fn item_file_name(id: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_nanos())
        .unwrap_or_default();
    format!("{nanos:020}-{id}.json")
}

/// Ids name files, so only a plain token is accepted.
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 128
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

pub fn enqueue(dir: &Path, item: &OwnerInputItem) -> io::Result<PathBuf> {
    if !valid_id(&item.id) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("owner input id {:?} is not a plain token", item.id),
        ));
    }
    let pending = dir.join(PENDING_DIR);
    fs::create_dir_all(&pending)?;
    fs::create_dir_all(dir.join(CLAIMED_DIR))?;
    let path = pending.join(item_file_name(&item.id));
    write_atomic(&path, &serde_json::to_vec(item)?)?;
    Ok(path)
}

fn find_item(dir: &Path, sub: &str, id: &str) -> Option<PathBuf> {
    let suffix = format!("-{id}.json");
    fs::read_dir(dir.join(sub))
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .find(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.ends_with(&suffix))
        })
}

/// The mod takes an input before submitting it. Exactly one claim of a pending
/// input succeeds, and an input the engine withdrew can no longer be claimed,
/// so an input is never submitted after the engine gave up on it.
pub fn claim(dir: &Path, id: &str) -> io::Result<bool> {
    let Some(pending) = find_item(dir, PENDING_DIR, id) else {
        return Ok(false);
    };
    let Some(name) = pending.file_name() else {
        return Ok(false);
    };
    let claimed = dir.join(CLAIMED_DIR).join(name);
    match fs::rename(&pending, &claimed) {
        Ok(()) => {
            append_ledger(dir, id, "claimed", None, None)?;
            Ok(true)
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

/// Take back an input the mod has not claimed; false once it was claimed.
pub fn withdraw(pending: &Path) -> io::Result<bool> {
    match fs::remove_file(pending) {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

/// Record a step. A terminal state retires the claimed file.
pub fn append_ledger(
    dir: &Path,
    id: &str,
    state: &str,
    detail: Option<&str>,
    turn_id: Option<&str>,
) -> io::Result<()> {
    let entry = LedgerEntry {
        id: id.to_string(),
        state: state.to_string(),
        at: chrono::Utc::now().to_rfc3339(),
        detail: detail.map(str::to_owned),
        turn_id: turn_id.map(str::to_owned),
    };
    let mut line = serde_json::to_vec(&entry)?;
    line.push(b'\n');
    // One write of a short line under O_APPEND lands whole.
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join(LEDGER_FILE))?
        .write_all(&line)?;
    if TERMINAL_STATES.contains(&state) {
        if let Some(claimed) = find_item(dir, CLAIMED_DIR, id) {
            let _ = fs::remove_file(claimed);
        }
    }
    Ok(())
}

pub fn ledger(dir: &Path) -> Vec<LedgerEntry> {
    let Ok(file) = File::open(dir.join(LEDGER_FILE)) else {
        return Vec::new();
    };
    BufReader::new(file)
        .lines()
        .map_while(Result::ok)
        .filter_map(|line| serde_json::from_str(&line).ok())
        .collect()
}

fn states_for(dir: &Path, id: &str) -> Vec<LedgerEntry> {
    ledger(dir)
        .into_iter()
        .filter(|entry| entry.id == id)
        .collect()
}

// --- The watcher the mod runs -----------------------------------------------

/// One line the watcher writes for the mod.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct WatchLine {
    pub id: String,
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub text: Option<String>,
    /// `pending`: claim and submit it. `claimed`: an earlier copy of the mod
    /// took it; do not submit it again, only record when its turn starts.
    pub state: String,
}

/// Stream this session's owner inputs to `out` until `out` closes or `keep`
/// says stop. Holds the lock for its whole life: one watcher per session, and a
/// free lock tells the engine no mod is attached.
#[cfg(unix)]
pub fn watch(dir: &Path, out: &mut impl Write, keep: impl Fn() -> bool) -> io::Result<()> {
    use std::collections::HashSet;
    use std::os::fd::AsRawFd;

    fs::create_dir_all(dir.join(PENDING_DIR))?;
    fs::create_dir_all(dir.join(CLAIMED_DIR))?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join(LOCK_FILE))?;
    // A reloaded mod starts a new watcher while the old one may still be
    // exiting: wait for it rather than run two.
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX) } != 0 {
        return Err(io::Error::last_os_error());
    }

    let mut offered = HashSet::new();
    // Inputs an earlier copy of the mod claimed and has not finished: this copy
    // only watches for their turn.
    let finished: HashSet<String> = ledger(dir)
        .into_iter()
        .filter(|entry| TERMINAL_STATES.contains(&entry.state.as_str()))
        .map(|entry| entry.id)
        .collect();
    for item in items_in(dir, CLAIMED_DIR) {
        if item.kind == "send" && !finished.contains(&item.id) {
            emit(out, &item, "claimed")?;
        }
        offered.insert(item.id);
    }
    // Say hello so the mod knows the stream is up even with nothing queued.
    writeln!(out, "{}", json!({"kind": "ready"}))?;
    out.flush()?;
    while keep() {
        for item in items_in(dir, PENDING_DIR) {
            if offered.insert(item.id.clone()) {
                emit(out, &item, "pending")?;
            }
        }
        std::thread::sleep(POLL_INTERVAL);
    }
    drop(lock);
    Ok(())
}

fn emit(out: &mut impl Write, item: &OwnerInputItem, state: &str) -> io::Result<()> {
    let line = WatchLine {
        id: item.id.clone(),
        kind: item.kind.clone(),
        text: item.text.clone(),
        state: state.to_string(),
    };
    writeln!(out, "{}", serde_json::to_string(&line)?)?;
    out.flush()
}

fn items_in(dir: &Path, sub: &str) -> Vec<OwnerInputItem> {
    let Ok(entries) = fs::read_dir(dir.join(sub)) else {
        return Vec::new();
    };
    let mut paths: Vec<PathBuf> = entries
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().and_then(|ext| ext.to_str()) == Some("json"))
        .collect();
    paths.sort();
    paths
        .into_iter()
        .filter_map(|path| serde_json::from_slice(&fs::read(path).ok()?).ok())
        .collect()
}

// --- What the engine's control asks for -------------------------------------

#[derive(Debug, PartialEq, Eq)]
pub enum DeliveryError {
    /// Nothing took the input, and it was withdrawn: it will never be
    /// submitted, so another path may carry it.
    NotPickedUp(&'static str),
    /// The mod reported a failure, with its reason.
    Failed(String),
    /// The mod claimed the input and never said what happened.
    Unknown(String),
    Io(String),
}

impl std::fmt::Display for DeliveryError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotPickedUp(reason) => {
                write!(f, "the Longhouse mod did not take the input ({reason})")
            }
            Self::Failed(detail) => {
                write!(f, "the Longhouse mod could not deliver the input: {detail}")
            }
            Self::Unknown(detail) => write!(f, "{detail}"),
            Self::Io(detail) => write!(f, "owner input spool: {detail}"),
        }
    }
}

impl From<io::Error> for DeliveryError {
    fn from(error: io::Error) -> Self {
        Self::Io(error.to_string())
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct SendReceipt {
    /// The turn it opened had already started when the engine answered.
    pub delivered: bool,
}

/// Hand an owner message to the mod and wait until Claude Code has queued it
/// as the user's prompt: at once when idle, after the running turn otherwise.
pub fn deliver_send(
    dir: &Path,
    id: &str,
    text: &str,
    budget: Duration,
) -> Result<SendReceipt, DeliveryError> {
    let pending = enqueue(
        dir,
        &OwnerInputItem {
            id: id.to_string(),
            kind: "send".to_string(),
            text: Some(text.to_string()),
            enqueued_at: chrono::Utc::now().to_rfc3339(),
        },
    )?;
    let entries = await_states(
        dir,
        id,
        &pending,
        budget,
        &["submitted", "delivered", "failed"],
    )?;
    if let Some(failed) = entries.iter().find(|entry| entry.state == "failed") {
        return Err(DeliveryError::Failed(
            failed
                .detail
                .clone()
                .unwrap_or_else(|| "no reason given".to_string()),
        ));
    }
    Ok(SendReceipt {
        delivered: entries.iter().any(|entry| entry.state == "delivered"),
    })
}

#[derive(Debug, PartialEq, Eq)]
pub enum AbortReceipt {
    Aborted {
        turn_id: Option<String>,
    },
    /// The mod knew of no running turn.
    NoTurn,
}

/// Ask the mod to end the running turn with `$.turn.abort`.
pub fn deliver_abort(
    dir: &Path,
    id: &str,
    budget: Duration,
) -> Result<AbortReceipt, DeliveryError> {
    let pending = enqueue(
        dir,
        &OwnerInputItem {
            id: id.to_string(),
            kind: "abort".to_string(),
            text: None,
            enqueued_at: chrono::Utc::now().to_rfc3339(),
        },
    )?;
    let entries = await_states(dir, id, &pending, budget, &["aborted", "no_turn", "failed"])?;
    if let Some(entry) = entries.iter().find(|entry| entry.state == "aborted") {
        return Ok(AbortReceipt::Aborted {
            turn_id: entry.turn_id.clone(),
        });
    }
    if entries.iter().any(|entry| entry.state == "no_turn") {
        return Ok(AbortReceipt::NoTurn);
    }
    let detail = entries
        .iter()
        .find(|entry| entry.state == "failed")
        .and_then(|entry| entry.detail.clone())
        .unwrap_or_else(|| "no reason given".to_string());
    Err(DeliveryError::Failed(detail))
}

/// Wait for one of `wanted` in the ledger. Until the mod claims the input it
/// can be withdrawn, and is: when the watcher detaches or `budget` runs out.
/// Once claimed it is the mod's, and the engine waits [`CLAIMED_GRACE`] more.
fn await_states(
    dir: &Path,
    id: &str,
    pending: &Path,
    budget: Duration,
    wanted: &[&str],
) -> Result<Vec<LedgerEntry>, DeliveryError> {
    let started = Instant::now();
    let mut claimed_deadline: Option<Instant> = None;
    loop {
        let entries = states_for(dir, id);
        if entries
            .iter()
            .any(|entry| wanted.contains(&entry.state.as_str()))
        {
            return Ok(entries);
        }
        let claimed = entries.iter().any(|entry| entry.state == "claimed");
        if claimed {
            let deadline = *claimed_deadline.get_or_insert_with(|| Instant::now() + CLAIMED_GRACE);
            if Instant::now() >= deadline {
                return Err(DeliveryError::Unknown(format!(
                    "the Longhouse mod took owner input {id} and did not report submitting it; it may still reach Claude"
                )));
            }
        } else if !watcher_attached(dir) {
            if withdraw(pending)? {
                return Err(DeliveryError::NotPickedUp("mod_not_attached"));
            }
        } else if started.elapsed() >= budget && withdraw(pending)? {
            return Err(DeliveryError::NotPickedUp("mod_did_not_take_it"));
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

fn write_atomic(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let temporary = path.with_extension(format!("tmp.{}", std::process::id()));
    fs::write(&temporary, bytes)?;
    fs::rename(&temporary, path)
}

/// A stand-in for the mod in tests: runs the real watcher and answers every
/// pending input it streams with `outcome` (`submitted` for a send,
/// `aborted`/`no_turn` for an abort), until the returned guard drops.
#[cfg(all(test, unix))]
pub(crate) fn spawn_fake_mod(dir: PathBuf, outcome: &'static str) -> FakeMod {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    struct Lines(std::sync::mpsc::Sender<String>, Vec<u8>);
    impl Write for Lines {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.1.extend_from_slice(bytes);
            while let Some(end) = self.1.iter().position(|byte| *byte == b'\n') {
                let line: Vec<u8> = self.1.drain(..=end).collect();
                let _ = self
                    .0
                    .send(String::from_utf8_lossy(&line).trim().to_string());
            }
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    let running = Arc::new(AtomicBool::new(true));
    let (lines_tx, lines_rx) = std::sync::mpsc::channel::<String>();
    let watch_running = Arc::clone(&running);
    let watch_dir = dir.clone();
    let watcher = std::thread::spawn(move || {
        let mut out = Lines(lines_tx, Vec::new());
        let _ = watch(&watch_dir, &mut out, || {
            watch_running.load(Ordering::SeqCst)
        });
    });
    let answer_running = Arc::clone(&running);
    let answerer = std::thread::spawn(move || {
        while answer_running.load(Ordering::SeqCst) {
            let Ok(line) = lines_rx.recv_timeout(Duration::from_millis(20)) else {
                continue;
            };
            let Ok(input) = serde_json::from_str::<WatchLine>(&line) else {
                continue;
            };
            if input.state == "pending" && claim(&dir, &input.id).unwrap_or(false) {
                let turn = (outcome == "aborted").then_some("turn-1");
                let _ = append_ledger(&dir, &input.id, outcome, None, turn);
            }
        }
    });
    FakeMod {
        running,
        threads: vec![watcher, answerer],
    }
}

#[cfg(all(test, unix))]
pub(crate) struct FakeMod {
    running: std::sync::Arc<std::sync::atomic::AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}

#[cfg(all(test, unix))]
impl FakeMod {
    /// Wait until the watcher holds its lock.
    pub(crate) fn attached(self, dir: &Path) -> Self {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !watcher_attached(dir) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        self
    }
}

#[cfg(all(test, unix))]
impl Drop for FakeMod {
    fn drop(&mut self) {
        self.running
            .store(false, std::sync::atomic::Ordering::SeqCst);
        for thread in self.threads.drain(..) {
            let _ = thread.join();
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::fd::AsRawFd;

    fn hold_watch_lock(dir: &Path) -> File {
        fs::create_dir_all(dir).unwrap();
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(dir.join(LOCK_FILE))
            .unwrap();
        assert_eq!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) }, 0);
        file
    }

    #[test]
    fn version_parse_reads_claude_code_output() {
        assert_eq!(
            parse_claude_code_version("2.1.296 (Claude Code)\n"),
            Some((2, 1, 296))
        );
        assert_eq!(
            parse_claude_code_version("2.1.287-beta.1"),
            Some((2, 1, 287))
        );
        assert_eq!(parse_claude_code_version("Claude Code"), None);
        assert_eq!(parse_claude_code_version(""), None);
    }

    #[test]
    fn route_uses_the_mod_only_when_claude_code_can_submit_as_the_user() {
        let current = decide_route(Some("2.1.296 (Claude Code)"), None);
        assert!(current.uses_mod());
        assert_eq!(current.reason, None);
        assert_eq!(current.claude_code_version.as_deref(), Some("2.1.296"));

        let first = decide_route(Some("2.1.287 (Claude Code)"), None);
        assert!(first.uses_mod());

        let old = decide_route(Some("2.1.286 (Claude Code)"), None);
        assert_eq!(old.path, PATH_CHANNEL);
        assert_eq!(old.reason.as_deref(), Some("claude_code_predates_mods"));

        let unknown = decide_route(None, None);
        assert_eq!(unknown.path, PATH_CHANNEL);
        assert_eq!(
            unknown.reason.as_deref(),
            Some("claude_code_version_unknown")
        );

        let blocked = decide_route(Some("2.1.296 (Claude Code)"), Some("disableSideloadFlags"));
        assert_eq!(blocked.path, PATH_CHANNEL);
        assert_eq!(
            blocked.reason.as_deref(),
            Some("mods_disabled_by_settings:disableSideloadFlags")
        );
    }

    #[test]
    fn settings_that_turn_mods_off_are_named() {
        let root = tempfile::tempdir().unwrap();
        let managed = root.path().join("managed-settings.json");
        let user = root.path().join("settings.json");

        assert_eq!(mod_blocking_setting(&[managed.clone()], Some(&user)), None);

        fs::write(
            &managed,
            r#"{"permissions": {}, "disableSideloadFlags": false}"#,
        )
        .unwrap();
        assert_eq!(mod_blocking_setting(&[managed.clone()], Some(&user)), None);

        fs::write(
            &managed,
            r#"{"pluginConfigs": {"sec-default@builtin": {"options": {"allowManagedModsOnly": true}}}}"#,
        )
        .unwrap();
        assert_eq!(
            mod_blocking_setting(&[managed.clone()], Some(&user)).as_deref(),
            Some("allowManagedModsOnly")
        );

        fs::write(&managed, "{}").unwrap();
        fs::write(&user, r#"{"disableAllHooks": true}"#).unwrap();
        assert_eq!(
            mod_blocking_setting(&[managed], Some(&user)).as_deref(),
            Some("disableAllHooks")
        );
    }

    #[test]
    fn watcher_attachment_is_the_lock() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        assert!(!watcher_attached(&dir));
        let lock = hold_watch_lock(&dir);
        assert!(watcher_attached(&dir));
        drop(lock);
        // A process another test forks inherits the locked descriptor until it
        // execs, so allow the lock a moment to come free.
        assert!(detaches(&dir));
    }

    fn detaches(dir: &Path) -> bool {
        let deadline = Instant::now() + Duration::from_secs(2);
        while watcher_attached(dir) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        !watcher_attached(dir)
    }

    #[test]
    fn one_claim_wins_and_a_withdrawn_input_cannot_be_claimed() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        let item = OwnerInputItem {
            id: "cmd-1".into(),
            kind: "send".into(),
            text: Some("hello".into()),
            enqueued_at: "t".into(),
        };
        enqueue(&dir, &item).unwrap();
        assert!(claim(&dir, "cmd-1").unwrap());
        assert!(!claim(&dir, "cmd-1").unwrap());
        assert_eq!(states_for(&dir, "cmd-1")[0].state, "claimed");

        let second = enqueue(
            &dir,
            &OwnerInputItem {
                id: "cmd-2".into(),
                ..item
            },
        )
        .unwrap();
        assert!(withdraw(&second).unwrap());
        assert!(!claim(&dir, "cmd-2").unwrap());
        assert!(!withdraw(&second).unwrap());
    }

    #[test]
    fn ids_that_could_name_another_path_are_refused() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        for id in ["", "../escape", "a/b", "with space"] {
            let item = OwnerInputItem {
                id: id.into(),
                kind: "send".into(),
                text: Some("x".into()),
                enqueued_at: "t".into(),
            };
            assert!(enqueue(&dir, &item).is_err(), "{id:?} was accepted");
        }
    }

    #[test]
    fn a_terminal_state_retires_the_claimed_input() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        enqueue(
            &dir,
            &OwnerInputItem {
                id: "cmd-1".into(),
                kind: "send".into(),
                text: Some("hello".into()),
                enqueued_at: "t".into(),
            },
        )
        .unwrap();
        claim(&dir, "cmd-1").unwrap();
        append_ledger(&dir, "cmd-1", "submitted", None, None).unwrap();
        assert!(find_item(&dir, CLAIMED_DIR, "cmd-1").is_some());
        append_ledger(&dir, "cmd-1", "delivered", None, Some("turn-9")).unwrap();
        assert!(find_item(&dir, CLAIMED_DIR, "cmd-1").is_none());
        let states: Vec<String> = states_for(&dir, "cmd-1")
            .into_iter()
            .map(|e| e.state)
            .collect();
        assert_eq!(states, ["claimed", "submitted", "delivered"]);
    }

    /// A fake mod: claims each pending input and reports `outcome` for it.
    fn fake_mod(dir: PathBuf, outcome: &'static str) -> std::thread::JoinHandle<()> {
        std::thread::spawn(move || {
            let lock = hold_watch_lock(&dir);
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline {
                for item in items_in(&dir, PENDING_DIR) {
                    if claim(&dir, &item.id).unwrap() {
                        let (state, turn) = match outcome {
                            "aborted" => ("aborted", Some("turn-1")),
                            other => (other, None),
                        };
                        append_ledger(&dir, &item.id, state, Some("because"), turn).unwrap();
                        drop(lock);
                        return;
                    }
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        })
    }

    #[test]
    fn send_returns_once_the_mod_submitted_it() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        fs::create_dir_all(&dir).unwrap();
        let mod_thread = fake_mod(dir.clone(), "submitted");
        // Give the fake mod its lock before the send looks for it.
        let deadline = Instant::now() + Duration::from_secs(2);
        while !watcher_attached(&dir) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let receipt = deliver_send(&dir, "cmd-1", "hello", Duration::from_secs(5)).unwrap();
        assert_eq!(receipt, SendReceipt { delivered: false });
        mod_thread.join().unwrap();
    }

    #[test]
    fn send_reports_the_mods_failure() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        fs::create_dir_all(&dir).unwrap();
        let mod_thread = fake_mod(dir.clone(), "failed");
        let deadline = Instant::now() + Duration::from_secs(2);
        while !watcher_attached(&dir) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let error = deliver_send(&dir, "cmd-1", "hello", Duration::from_secs(5)).unwrap_err();
        assert_eq!(error, DeliveryError::Failed("because".into()));
        mod_thread.join().unwrap();
    }

    #[test]
    fn send_without_a_mod_is_withdrawn_and_never_offered() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        let error = deliver_send(&dir, "cmd-1", "hello", Duration::from_secs(5)).unwrap_err();
        assert_eq!(error, DeliveryError::NotPickedUp("mod_not_attached"));
        assert!(items_in(&dir, PENDING_DIR).is_empty());
    }

    #[test]
    fn send_to_a_mod_that_never_claims_is_withdrawn_after_the_budget() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        let _lock = hold_watch_lock(&dir);
        let error = deliver_send(&dir, "cmd-1", "hello", Duration::from_millis(200)).unwrap_err();
        assert_eq!(error, DeliveryError::NotPickedUp("mod_did_not_take_it"));
        assert!(!claim(&dir, "cmd-1").unwrap());
    }

    #[test]
    fn abort_reports_the_turn_it_ended_or_that_there_was_none() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        fs::create_dir_all(&dir).unwrap();
        for (outcome, expected) in [
            (
                "aborted",
                AbortReceipt::Aborted {
                    turn_id: Some("turn-1".into()),
                },
            ),
            ("no_turn", AbortReceipt::NoTurn),
        ] {
            let mod_thread = fake_mod(dir.clone(), outcome);
            let deadline = Instant::now() + Duration::from_secs(2);
            while !watcher_attached(&dir) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(5));
            }
            let id = format!("abort-{outcome}");
            assert_eq!(
                deliver_abort(&dir, &id, Duration::from_secs(5)).unwrap(),
                expected
            );
            mod_thread.join().unwrap();
        }
    }

    #[test]
    fn watch_offers_each_pending_input_once_and_never_resubmits_a_claimed_one() {
        let root = tempfile::tempdir().unwrap();
        let dir = spool_dir(root.path(), "s1");
        let base = OwnerInputItem {
            id: "old".into(),
            kind: "send".into(),
            text: Some("claimed earlier".into()),
            enqueued_at: "t".into(),
        };
        enqueue(&dir, &base).unwrap();
        claim(&dir, "old").unwrap();
        enqueue(
            &dir,
            &OwnerInputItem {
                id: "new".into(),
                text: Some("fresh".into()),
                ..base.clone()
            },
        )
        .unwrap();
        enqueue(
            &dir,
            &OwnerInputItem {
                id: "done".into(),
                text: Some("finished".into()),
                ..base
            },
        )
        .unwrap();
        claim(&dir, "done").unwrap();
        append_ledger(&dir, "done", "delivered", None, None).unwrap();

        let passes = std::sync::atomic::AtomicUsize::new(0);
        let mut out = Vec::new();
        watch(&dir, &mut out, || {
            passes.fetch_add(1, std::sync::atomic::Ordering::SeqCst) < 3
        })
        .unwrap();
        let lines: Vec<serde_json::Value> = String::from_utf8(out)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(
            lines,
            vec![
                json!({"id": "old", "kind": "send", "text": "claimed earlier", "state": "claimed"}),
                json!({"kind": "ready"}),
                json!({"id": "new", "kind": "send", "text": "fresh", "state": "pending"}),
            ]
        );
        assert!(detaches(&dir));
    }
}
