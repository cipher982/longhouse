//! Antigravity Helm terminal owner: agy under a PTY the launcher also types into.
//!
//! agy's hook inbox is read only when agy makes a model invocation, and an idle
//! TUI makes none, so a remote send to an idle Helm session was claimed by nobody
//! and timed out (the engine waits its claim window, then deletes the message).
//! The hooks cannot wake an idle agy, and nothing outside the process can type
//! into its terminal, so the launcher owns the terminal the way Cursor Helm does
//! (`pty_relay`): agy runs under `forkpty`, the user's keystrokes and agy's
//! output are relayed, and an idle `send` is typed into the same terminal as the
//! user's own words.
//!
//! The wake is one more claimer of the same inbox, not a new channel. The send
//! path is unchanged: the engine writes `msg-<id>.json` and waits for the claim
//! receipt `claimed/claimed-msg-<id>.json`. While agy is mid-turn its hook claims
//! the message at the next invocation boundary, as before. While the hook state
//! says agy is idle (a `Stop` with `fullyIdle`, settled for `IDLE_SETTLE`), the
//! launcher claims the message itself, in the receipt format the hook writes
//! (`claimed_by: longhouse-antigravity-launcher`, `hook_event: IdleWake`), and
//! types it. Both claimers move the file with an atomic rename, so exactly one
//! wins and a message is never delivered twice.
//!
//! The launcher also owns the exit: when agy's PTY closes it knows the exit
//! status, and queues the terminal event that ends the served run (a terminate
//! kills the process group, which closes the PTY, so a remote terminate now ends
//! the run as it does for the other Helm providers).

use std::ffi::{CString, OsStr, OsString};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Context;
use chrono::{DateTime, Utc};
use serde_json::{json, Value};

use crate::pty_relay::{self, PtyCommand, PtyWriter};

/// How long the hook's idle state must have stood before the launcher may claim
/// a message. A `Stop` that finds a message pending answers `continue` and the
/// hook claims it at the next `PreInvocation`; this keeps the launcher from
/// racing that continuation.
const IDLE_SETTLE: Duration = Duration::from_millis(800);
/// Settle window on a freshly spawned session before any hook has fired, giving
/// `agy` time to draw its initial prompt before keystrokes are written.
const FRESH_LAUNCH_SETTLE: Duration = Duration::from_millis(1500);
/// After typing a message the launcher waits for the hook to report the turn
/// starting (any new hook event) before it types another, up to this long.
const TURN_START_PATIENCE: Duration = Duration::from_secs(20);
const POLL_INTERVAL: Duration = Duration::from_millis(100);
/// Pause between the typed text and the submit key, so the TUI has taken the text.
const SUBMIT_DELAY: Duration = Duration::from_millis(250);

/// `<longhouse home>/managed-local/antigravity`, where the hook script and the
/// engine's inbox writer keep the state and inbox directories.
fn antigravity_dir() -> anyhow::Result<PathBuf> {
    Ok(crate::longhouse_home()?
        .join("managed-local")
        .join("antigravity"))
}

fn session_state_path(session_id: &str) -> anyhow::Result<PathBuf> {
    Ok(antigravity_dir()?
        .join("sessions")
        .join(format!("{session_id}.json")))
}

fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}

fn ensure_private_dir(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    std::fs::create_dir_all(path)?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
}

/// What the launcher decided to do with an idle session's inbox.
#[derive(Debug, PartialEq, Eq)]
pub struct Typed {
    pub message_id: String,
    pub claimed_at: String,
}

/// State carried between polls: the hook mark seen when a message was last typed.
#[derive(Default)]
pub struct WakeGate {
    typed_at: Option<(Instant, String)>,
}

fn parse_utc(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|parsed| parsed.with_timezone(&Utc))
}

/// Whether the session state says agy is idle and has settled.
fn idle_mark(state: &Value, now: DateTime<Utc>) -> Option<String> {
    if state.get("state").and_then(Value::as_str) != Some("idle") {
        return None;
    }
    if let Some(observed) = state
        .get("last_hook_observed_at")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
    {
        let age = now.signed_duration_since(parse_utc(observed)?);
        return (age >= chrono::Duration::from_std(IDLE_SETTLE).ok()?)
            .then(|| observed.to_string());
    }
    // A freshly launched Helm session seeds `state: "idle"` and `updated_at`
    // before its first turn runs, when no hook has fired yet.
    let seeded = state
        .get("updated_at")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())?;
    let age = now.signed_duration_since(parse_utc(seeded)?);
    (age >= chrono::Duration::from_std(FRESH_LAUNCH_SETTLE).ok()?)
        .then(|| format!("seeded:{seeded}"))
}

fn is_private_regular_file(path: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    std::fs::metadata(path).is_ok_and(|meta| {
        meta.is_file() && meta.uid() == unsafe { libc::geteuid() } && meta.mode() & 0o077 == 0
    })
}

struct ReservedSend {
    message_id: String,
    text: String,
    payload: Value,
    original_path: PathBuf,
    claim_path: PathBuf,
}

/// Reserve the oldest live `send` in the inbox by atomic rename into `claimed/`.
/// `claimed_at` is written only after the PTY write succeeds, so a failed PTY
/// write never produces a false delivery receipt.
fn reserve_oldest_send(inbox: &Path, now: DateTime<Utc>) -> Option<ReservedSend> {
    let mut names: Vec<_> = std::fs::read_dir(inbox)
        .ok()?
        .filter_map(|entry| entry.ok())
        .map(|entry| entry.file_name().to_string_lossy().into_owned())
        .filter(|name| name.starts_with("msg-") && name.ends_with(".json"))
        .collect();
    names.sort();
    for name in names {
        let path = inbox.join(&name);
        if !is_private_regular_file(&path) {
            continue;
        }
        let Some(payload) = std::fs::read(&path)
            .ok()
            .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            .filter(Value::is_object)
        else {
            continue;
        };
        // Only a send is typed. A steer needs a running turn and an interrupt
        // needs an invocation boundary; both stay for the hook (or time out).
        if payload
            .get("intent")
            .and_then(Value::as_str)
            .unwrap_or("send")
            != "send"
        {
            continue;
        }
        if payload
            .get("expires_at")
            .and_then(Value::as_str)
            .and_then(parse_utc)
            .is_some_and(|expires| expires < now)
        {
            let _ = std::fs::remove_file(&path);
            continue;
        }
        let text = payload.get("text").and_then(Value::as_str).unwrap_or("");
        // A leading slash would run an agy command rather than say something to
        // the model, and the owner's words must reach the model as typed.
        if text.trim().is_empty() || text.trim_start().starts_with('/') {
            continue;
        }
        let text = text.to_string();
        let message_id = payload
            .get("id")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| {
                name.trim_start_matches("msg-")
                    .trim_end_matches(".json")
                    .to_string()
            });
        let claim_path = inbox.join("claimed").join(format!("claimed-{name}"));
        if ensure_private_dir(&inbox.join("claimed")).is_err()
            || std::fs::rename(&path, &claim_path).is_err()
        {
            // The hook or engine withdrawal took it first.
            continue;
        }
        return Some(ReservedSend {
            message_id,
            text,
            payload,
            original_path: path,
            claim_path,
        });
    }
    None
}

enum TypeError {
    NothingWritten(std::io::Error),
    PartialWrite(std::io::Error),
}

/// Type `text` into the TUI as the user would: the text, then Enter. Multi-line
/// text goes in as a bracketed paste so its newlines do not submit.
fn type_message(writer: &PtyWriter, text: &str) -> Result<(), TypeError> {
    {
        let guard = writer.lock();
        if text.contains('\n') {
            guard
                .write(pty_relay::PASTE_START)
                .map_err(TypeError::NothingWritten)?;
            guard
                .write(text.as_bytes())
                .map_err(TypeError::PartialWrite)?;
            guard
                .write(pty_relay::PASTE_END)
                .map_err(TypeError::PartialWrite)?;
        } else {
            guard
                .write(text.as_bytes())
                .map_err(TypeError::NothingWritten)?;
        }
    }
    std::thread::sleep(SUBMIT_DELAY);
    writer.lock().write(b"\r").map_err(TypeError::PartialWrite)
}

/// One poll: if agy is idle and a send is waiting, claim and type it.
pub fn wake_once(
    gate: &mut WakeGate,
    writer: &PtyWriter,
    session_id: &str,
    now: DateTime<Utc>,
) -> Option<Typed> {
    let state: Value = std::fs::read(session_state_path(session_id).ok()?)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())?;
    let mark = idle_mark(&state, now)?;
    if let Some((typed, seen_mark)) = &gate.typed_at {
        if *seen_mark == mark && typed.elapsed() < TURN_START_PATIENCE {
            return None;
        }
    }
    // The user is actively typing: leave the message for the next poll.
    if writer.local_draft_pending() {
        return None;
    }
    let inbox = antigravity_dir().ok()?.join("inbox").join(session_id);
    let mut reserved = reserve_oldest_send(&inbox, now)?;
    match type_message(writer, &reserved.text) {
        Ok(()) => {
            let claimed_at = now_iso();
            if let Some(object) = reserved.payload.as_object_mut() {
                object.insert("id".into(), json!(reserved.message_id));
                object.insert("session_id".into(), json!(session_id));
                object.insert("claimed_at".into(), json!(claimed_at));
                object.insert("claimed_by".into(), json!("longhouse-antigravity-launcher"));
                object.insert("hook_event".into(), json!("IdleWake"));
            }
            let _ = crate::write_private_json(&reserved.claim_path, &reserved.payload);
            gate.typed_at = Some((Instant::now(), mark));
            Some(Typed {
                message_id: reserved.message_id,
                claimed_at,
            })
        }
        Err(TypeError::NothingWritten(error)) => {
            tracing::warn!(session_id, %error, "could not write Antigravity send to PTY; returning to inbox");
            let _ = std::fs::rename(&reserved.claim_path, &reserved.original_path);
            None
        }
        Err(TypeError::PartialWrite(error)) => {
            tracing::warn!(session_id, %error, "partial PTY write on Antigravity send; leaving claim unfinalized");
            gate.typed_at = Some((Instant::now(), mark));
            None
        }
    }
}

fn pump(session_id: String, writer: PtyWriter, stop: Arc<AtomicBool>) {
    let mut gate = WakeGate::default();
    while !stop.load(Ordering::Relaxed) {
        wake_once(&mut gate, &writer, &session_id, Utc::now());
        std::thread::sleep(POLL_INTERVAL);
    }
}

fn resolve_program(program: &OsStr) -> PathBuf {
    let candidate = PathBuf::from(program);
    if candidate.components().count() > 1 {
        return candidate;
    }
    std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default())
        .map(|dir| dir.join(&candidate))
        .find(|path| path.is_file())
        .unwrap_or(candidate)
}

/// The child's environment: ours, with the command's overrides applied, and a
/// usable TERM and size hints (agy sizes its first frame from them).
fn child_env(command: &Command, size: &libc::winsize) -> anyhow::Result<Vec<CString>> {
    let mut pairs: Vec<(OsString, OsString)> = std::env::vars_os().collect();
    for (key, value) in command.get_envs() {
        pairs.retain(|(existing, _)| existing != key);
        if let Some(value) = value {
            pairs.push((key.to_os_string(), value.to_os_string()));
        }
    }
    pairs.retain(|(key, _)| key != "LINES" && key != "COLUMNS");
    pairs.push(("LINES".into(), size.ws_row.to_string().into()));
    pairs.push(("COLUMNS".into(), size.ws_col.to_string().into()));
    let term_usable = pairs
        .iter()
        .any(|(key, value)| key == "TERM" && !value.is_empty() && value != "dumb");
    if !term_usable {
        pairs.retain(|(key, _)| key != "TERM");
        pairs.push(("TERM".into(), "xterm-256color".into()));
    }
    pairs
        .into_iter()
        .map(|(key, value)| {
            let mut pair = key.into_vec();
            pair.push(b'=');
            pair.extend(value.into_vec());
            CString::new(pair).context("Antigravity environment cannot contain NUL")
        })
        .collect()
}

fn pty_command(command: &Command, size: &libc::winsize) -> anyhow::Result<PtyCommand> {
    // no managed identity: this only translates the Command it is handed. The
    // launcher applied the identity overlay to that Command before calling.
    let program = resolve_program(command.get_program());
    let mut argv = vec![CString::new(program.as_os_str().as_bytes())
        .context("Antigravity binary path cannot contain NUL")?];
    for arg in command.get_args() {
        argv.push(
            CString::new(arg.as_bytes()).context("Antigravity arguments cannot contain NUL")?,
        );
    }
    let cwd = match command.get_current_dir() {
        Some(dir) => dir.to_path_buf(),
        None => std::env::current_dir()?,
    };
    Ok(PtyCommand {
        argv,
        env: child_env(command, size)?,
        cwd: CString::new(cwd.as_os_str().as_bytes())
            .context("Antigravity working directory cannot contain NUL")?,
    })
}

/// Run `command` (agy) under a PTY until it exits and return its exit code.
/// `after_spawn` runs once the provider exists, with its pid (also its process
/// group: `forkpty` makes it a session leader), to record it and confirm the
/// launch; an error there ends the provider.
pub fn run(
    command: &Command,
    session_id: &str,
    after_spawn: impl FnOnce(libc::pid_t) -> anyhow::Result<()>,
) -> anyhow::Result<i32> {
    let mut size = pty_relay::terminal_winsize(1);
    let child = pty_relay::spawn(&pty_command(command, &size)?, &mut size)
        .context("start agy under a PTY")?;
    if let Err(error) = after_spawn(child.pid) {
        child.abort();
        return Err(error);
    }
    let stop = Arc::new(AtomicBool::new(false));
    let resized = Arc::new(AtomicBool::new(true));
    let setup = (|| -> anyhow::Result<pty_relay::RawTerminal> {
        let terminal = pty_relay::raw_mode(0)?;
        signal_hook::flag::register(libc::SIGWINCH, resized.clone())?;
        signal_hook::flag::register(libc::SIGTERM, stop.clone())?;
        signal_hook::flag::register(libc::SIGHUP, stop.clone())?;
        Ok(terminal)
    })();
    let terminal = match setup {
        Ok(terminal) => terminal,
        Err(error) => {
            child.abort();
            return Err(error);
        }
    };
    pty_relay::sync_winsize(child.master);
    let writer = PtyWriter::new(child.master);
    let pump_stop = Arc::new(AtomicBool::new(false));
    let pump_thread = {
        let (session_id, writer, stop) =
            (session_id.to_string(), writer.clone(), pump_stop.clone());
        std::thread::spawn(move || pump(session_id, writer, stop))
    };
    let reaped_status = pty_relay::relay(&child, &writer, 0, 1, &stop, &resized);
    let requested_stop = stop.load(Ordering::Relaxed);
    pump_stop.store(true, Ordering::Relaxed);
    let _ = pump_thread.join();
    drop(terminal);
    child.release_slave_hold();
    let exit_code = pty_relay::reap(&child, reaped_status, requested_stop);
    // agy's tool children can outlive it; the group is ours, so end it.
    unsafe {
        libc::kill(-child.pid, libc::SIGKILL);
        libc::close(child.master);
    }
    Ok(exit_code)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::fd::RawFd;
    use std::os::unix::fs::PermissionsExt;

    static HOME_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn pipe() -> (RawFd, RawFd) {
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        (fds[0], fds[1])
    }

    fn read_available(fd: RawFd) -> Vec<u8> {
        let mut seen = Vec::new();
        let mut buffer = [0u8; 4096];
        loop {
            let mut pfd = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            if unsafe { libc::poll(&mut pfd, 1, 100) } <= 0 {
                return seen;
            }
            let count = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
            if count <= 0 {
                return seen;
            }
            seen.extend_from_slice(&buffer[..count as usize]);
        }
    }

    fn write_state(home: &Path, session: &str, state: &str, observed: DateTime<Utc>) {
        let path = home
            .join("managed-local/antigravity/sessions")
            .join(format!("{session}.json"));
        crate::write_private_json(
            &path,
            &json!({
                "session_id": session,
                "state": state,
                "last_hook_observed_at": observed.to_rfc3339(),
            }),
        )
        .unwrap();
    }

    fn write_message(home: &Path, session: &str, id: &str, text: &str, intent: &str) -> PathBuf {
        let path = home
            .join("managed-local/antigravity/inbox")
            .join(session)
            .join(format!("msg-{id}.json"));
        crate::write_private_json(
            &path,
            &json!({
                "id": id, "session_id": session, "text": text, "intent": intent,
                "created_at": now_iso(),
                "expires_at": (Utc::now() + chrono::Duration::minutes(5)).to_rfc3339(),
            }),
        )
        .unwrap();
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        path
    }

    fn settled() -> DateTime<Utc> {
        Utc::now() - chrono::Duration::seconds(5)
    }

    #[test]
    fn an_idle_send_is_claimed_with_a_receipt_and_typed_with_enter() {
        let _guard = HOME_LOCK
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            write_state(temp.path(), "s1", "idle", settled());
            let message = write_message(temp.path(), "s1", "m1", "hello agy", "send");
            let (read, write) = pipe();
            let writer = PtyWriter::new(write);
            let mut gate = WakeGate::default();
            let typed = wake_once(&mut gate, &writer, "s1", Utc::now()).expect("typed");
            assert_eq!(typed.message_id, "m1");
            assert!(!message.exists(), "the message moved to claimed/");
            let receipt: Value = serde_json::from_slice(
                &std::fs::read(
                    message
                        .parent()
                        .unwrap()
                        .join("claimed/claimed-msg-m1.json"),
                )
                .unwrap(),
            )
            .unwrap();
            assert_eq!(receipt["claimed_at"], typed.claimed_at);
            assert_eq!(receipt["hook_event"], "IdleWake");
            assert_eq!(receipt["intent"], "send");
            assert_eq!(read_available(read), b"hello agy\r".to_vec());
            unsafe {
                libc::close(read);
                libc::close(write);
            }
        });
    }

    #[test]
    fn multiline_text_goes_in_as_one_bracketed_paste() {
        let (read, write) = pipe();
        assert!(type_message(&PtyWriter::new(write), "one\ntwo").is_ok());
        assert_eq!(
            read_available(read),
            b"\x1b[200~one\ntwo\x1b[201~\r".to_vec()
        );
        unsafe {
            libc::close(read);
            libc::close(write);
        }
    }

    #[test]
    fn nothing_is_claimed_while_a_turn_runs_or_just_ended() {
        let _guard = HOME_LOCK
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            let message = write_message(temp.path(), "s2", "m2", "later", "send");
            let (read, write) = pipe();
            let writer = PtyWriter::new(write);
            let mut gate = WakeGate::default();
            // The hook owns a running turn.
            write_state(temp.path(), "s2", "thinking", settled());
            assert!(wake_once(&mut gate, &writer, "s2", Utc::now()).is_none());
            // A Stop that just fired may still continue into a pending message.
            write_state(temp.path(), "s2", "idle", Utc::now());
            assert!(wake_once(&mut gate, &writer, "s2", Utc::now()).is_none());
            assert!(message.exists());
            assert!(read_available(read).is_empty());
            unsafe {
                libc::close(read);
                libc::close(write);
            }
        });
    }

    #[test]
    fn steers_interrupts_expired_and_slash_messages_are_left_alone() {
        let _guard = HOME_LOCK
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            write_state(temp.path(), "s3", "idle", settled());
            let steer = write_message(temp.path(), "s3", "a-steer", "steer me", "steer");
            let interrupt = write_message(
                temp.path(),
                "s3",
                "b-int",
                "Longhouse interrupt",
                "interrupt",
            );
            let slash = write_message(temp.path(), "s3", "c-slash", "/quit", "send");
            let (read, write) = pipe();
            let writer = PtyWriter::new(write);
            let mut gate = WakeGate::default();
            assert!(wake_once(&mut gate, &writer, "s3", Utc::now()).is_none());
            assert!(steer.exists() && interrupt.exists() && slash.exists());
            assert!(read_available(read).is_empty());
            unsafe {
                libc::close(read);
                libc::close(write);
            }
        });
    }

    #[test]
    fn a_user_draft_holds_the_message_and_a_second_send_waits_for_the_turn_to_start() {
        let _guard = HOME_LOCK
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            write_state(temp.path(), "s4", "idle", settled());
            let first = write_message(temp.path(), "s4", "m-1", "first", "send");
            let second = write_message(temp.path(), "s4", "m-2", "second", "send");
            let (read, write) = pipe();
            let writer = PtyWriter::new(write);
            writer.relay_local_input(b"half").unwrap();
            let mut gate = WakeGate::default();
            assert!(wake_once(&mut gate, &writer, "s4", Utc::now()).is_none());
            assert!(first.exists());
            writer.relay_local_input(b"\r").unwrap();
            let _ = read_available(read);
            assert_eq!(
                wake_once(&mut gate, &writer, "s4", Utc::now())
                    .unwrap()
                    .message_id,
                "m-1"
            );
            // The hook has not reported the turn starting: do not stack another.
            assert!(wake_once(&mut gate, &writer, "s4", Utc::now()).is_none());
            assert!(second.exists());
            // A new hook event clears the gate.
            write_state(
                temp.path(),
                "s4",
                "idle",
                Utc::now() - chrono::Duration::seconds(2),
            );
            assert_eq!(
                wake_once(&mut gate, &writer, "s4", Utc::now())
                    .unwrap()
                    .message_id,
                "m-2"
            );
            unsafe {
                libc::close(read);
                libc::close(write);
            }
        });
    }

    #[test]
    fn a_fresh_session_seeded_idle_before_any_hook_wakes_after_launch_settle() {
        let _guard = HOME_LOCK
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        let temp = tempfile::tempdir().unwrap();
        temp_env::with_var("LONGHOUSE_HOME", Some(temp.path()), || {
            let path = temp
                .path()
                .join("managed-local/antigravity/sessions/fresh.json");
            let seeded_at = Utc::now() - chrono::Duration::seconds(3);
            crate::write_private_json(
                &path,
                &json!({
                    "schema_version": 3,
                    "session_id": "fresh",
                    "state": "idle",
                    "updated_at": seeded_at.to_rfc3339(),
                }),
            )
            .unwrap();
            let message = write_message(temp.path(), "fresh", "m-fresh", "first prompt", "send");
            let (read, write) = pipe();
            let writer = PtyWriter::new(write);
            // Terminal startup probes arrive on stdin before any user keystroke.
            writer
                .relay_local_input(b"\x1b]11;rgb:0000/0000/0000\x1b\\\x1bP>|term\x1b\\\x1b[?1u")
                .unwrap();
            let _ = read_available(read);

            let mut gate = WakeGate::default();
            let typed =
                wake_once(&mut gate, &writer, "fresh", Utc::now()).expect("woke fresh session");
            assert_eq!(typed.message_id, "m-fresh");
            assert!(!message.exists());
            assert_eq!(read_available(read), b"first prompt\r".to_vec());
            unsafe {
                libc::close(read);
                libc::close(write);
            }
        });
    }

    #[test]
    fn the_pty_command_carries_the_binary_and_overridden_environment() {
        // `forkpty` needs no controlling terminal of ours, but raw mode on fd 0
        // does, so this covers only the exec data and environment.
        let mut command = Command::new("sh");
        command.arg("-c").arg("exit 3").env("LONGHOUSE_PROOF", "1");
        let size = pty_relay::terminal_winsize(-1);
        let built = pty_command(&command, &size).unwrap();
        assert!(built.argv[0].to_bytes().ends_with(b"/sh"));
        assert!(built
            .env
            .iter()
            .any(|pair| pair.to_bytes() == b"LONGHOUSE_PROOF=1"));
        assert!(built
            .env
            .iter()
            .any(|pair| pair.to_bytes().starts_with(b"TERM=")));
    }
}
