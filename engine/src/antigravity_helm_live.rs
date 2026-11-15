//! Live reply text for Antigravity Helm, read from the conversation database.
//!
//! Antigravity's TUI paints a reply token by token but writes its transcript
//! only when a model step ends, and its hooks carry no text. The conversation
//! database does carry it: while a model step streams, agy updates that step's
//! row in `conversations/<id>.db` (SQLite, WAL mode) on every token, about
//! 15 ms after painting it. This module follows that row read-only and states
//! the accumulated reply text on the same live-preview path Console uses, so a
//! client sees the reply as it streams instead of when the step ends.
//!
//! What the database holds (measured on agy 1.1.13 through 1.5.6, identical
//! across all seven versions tried; `user_version` is 1 throughout):
//!
//! - `steps(idx, step_type, status, ..., step_payload)`, one row per step.
//!   `step_type` 15 is a model response. `status` is 8 while it streams and 3
//!   once it is done.
//! - `step_payload` is protobuf without a published schema. Field 1 repeats
//!   `step_type`; field 20 is the model response. Inside it, field 1 is the
//!   reply text so far (plain UTF-8, cumulative, not a delta) and field 3 is
//!   the model's thinking so far. Thinking goes out as its own preview,
//!   labelled `thinking`, never as the reply.
//! - `step_type` 132 is a tool call. Its payload's field 5 holds, in field 4,
//!   the call id (1), the tool name (2) and the arguments as JSON text (3);
//!   field 4 of the payload is the step's status, 3 once the call is done.
//!
//! This is a private, undocumented surface, so the reader trusts none of it
//! until it checks it, and stands down for the rest of the session on any
//! surprise: an unrecognised `user_version`, a missing column, a payload that
//! does not parse as the shape above, or repeated open and read errors. Standing
//! down costs only the live text; the transcript still delivers each step when
//! it ends, exactly as before.
//!
//! The reader must never disturb agy. It opens its own connection read-only,
//! with `query_only` on and a 50 ms busy timeout, and holds no transaction
//! between polls (each poll is one autocommit statement, so WAL checkpoints
//! are never held back by it). It never writes, never opens a database whose
//! `-wal` and `-shm` files are not both already there (opening a WAL database
//! read-only without them would create them), and retries only on a backoff
//! that grows to two seconds, never in a tight loop.
//!
//! When it reads. Every write agy makes lands in the `-wal` file, so between
//! polls the reader only stats that file (size and modification time) and
//! reads the database when it changed, at most once per `POLL_INTERVAL`. The
//! hook state file says whether a turn is in progress, but the reader learns
//! that a second late (`SCAN_EVERY`), and until 2026-11-12 an idle session was
//! read once a second whatever agy wrote: the first text or tool call of each
//! turn in an open session reached Longhouse up to a second after the terminal
//! showed it (1.0 s measured for a turn that opens with a tool call).

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Value};

/// Master switch. The terminal-truth mutation test flips this to prove the
/// gate notices when the reader is gone.
fn reader_enabled() -> bool {
    !crate::qa_fault::mutant("antigravity-helm-reader")
}

pub const SOURCE: &str = "antigravity_helm_db";
pub const PROGRESS_KIND: &str = "antigravity_helm_stream";

/// The only `user_version` this reader understands.
const SUPPORTED_USER_VERSION: i64 = 1;
/// `steps.step_type` of a model response.
const STEP_TYPE_MODEL_RESPONSE: u64 = 15;
/// `steps.status` while a model step is streaming, and once it is done.
const STATUS_STREAMING: i64 = 8;
/// `steps.step_type` of a tool call.
const STEP_TYPE_TOOL: i64 = 132;
const STATUS_DONE: i64 = 3;

const POLL_INTERVAL: Duration = Duration::from_millis(100);
/// The thread wakes this often to see what is due and whether it should stop.
const LOOP_SLICE: Duration = Duration::from_millis(25);
const MAX_BACKOFF: Duration = Duration::from_secs(2);
/// Consecutive failures after which the reader stands down for good.
const MAX_CONSECUTIVE_ERRORS: u32 = 8;
const BUSY_TIMEOUT: Duration = Duration::from_millis(50);

/// Where the reader looks.
#[derive(Debug, Clone)]
pub struct HelmLiveConfig {
    /// Hook state files, one per managed Antigravity session
    /// (`managed-local/antigravity/sessions`). A file with a control identity
    /// is a Helm session; it names the conversation once agy reports it.
    pub state_dir: PathBuf,
    /// Agy's state directory (`~/.gemini/antigravity-cli`).
    pub store_root: PathBuf,
    /// The runtime-event outbox the Machine Agent drains.
    pub outbox_dir: PathBuf,
}

impl HelmLiveConfig {
    /// The locations on this machine, or `None` when they cannot be resolved.
    pub fn for_this_machine() -> Option<Self> {
        let home = std::env::var_os("HOME").map(PathBuf::from)?;
        Some(Self {
            state_dir: crate::managed_antigravity_scan::default_antigravity_state_dir()?,
            store_root: home.join(".gemini").join("antigravity-cli"),
            outbox_dir: crate::config::get_agent_runtime_events_outbox_dir().ok()?,
        })
    }
}

/// A running reader. Dropping it stops the thread.
pub struct HelmLive {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl HelmLive {
    pub fn stop(mut self) {
        self.shutdown();
    }

    fn shutdown(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

impl Drop for HelmLive {
    fn drop(&mut self) {
        self.shutdown();
    }
}

/// Start following every Helm session on this machine. One thread serves them
/// all. `None` when disabled or when the thread cannot start.
pub fn spawn(config: HelmLiveConfig) -> Option<HelmLive> {
    if !reader_enabled() {
        return None;
    }
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    let thread = std::thread::Builder::new()
        .name("antigravity-helm-live".into())
        .spawn(move || run(config, flag))
        .ok()?;
    Some(HelmLive {
        stop,
        thread: Some(thread),
    })
}

// --- protobuf (just enough, and strict) -----------------------------------

/// Why a payload is not the shape this reader knows.
#[derive(Debug, PartialEq, Eq)]
pub struct Surprise(pub String);

fn surprise<T>(message: impl Into<String>) -> Result<T, Surprise> {
    Err(Surprise(message.into()))
}

fn read_varint(bytes: &[u8], at: &mut usize) -> Result<u64, Surprise> {
    let mut value = 0u64;
    let mut shift = 0u32;
    loop {
        let Some(byte) = bytes.get(*at) else {
            return surprise("truncated varint");
        };
        *at += 1;
        if shift >= 64 {
            return surprise("overlong varint");
        }
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
        shift += 7;
    }
}

enum Wire<'a> {
    Varint(u64),
    Bytes(&'a [u8]),
    Other,
}

/// Walk the top-level fields of one message, in order.
fn fields(bytes: &[u8]) -> Result<Vec<(u64, Wire<'_>)>, Surprise> {
    let mut out = Vec::new();
    let mut at = 0usize;
    while at < bytes.len() {
        let key = read_varint(bytes, &mut at)?;
        let number = key >> 3;
        if number == 0 {
            return surprise("field number 0");
        }
        match key & 7 {
            0 => out.push((number, Wire::Varint(read_varint(bytes, &mut at)?))),
            1 => {
                if at + 8 > bytes.len() {
                    return surprise("truncated fixed64");
                }
                at += 8;
                out.push((number, Wire::Other));
            }
            2 => {
                let length = read_varint(bytes, &mut at)? as usize;
                let end = at.checked_add(length).filter(|end| *end <= bytes.len());
                let Some(end) = end else {
                    return surprise("truncated length-delimited field");
                };
                out.push((number, Wire::Bytes(&bytes[at..end])));
                at = end;
            }
            5 => {
                if at + 4 > bytes.len() {
                    return surprise("truncated fixed32");
                }
                at += 4;
                out.push((number, Wire::Other));
            }
            other => return surprise(format!("unsupported wire type {other}")),
        }
    }
    Ok(out)
}

/// The reply text and the thinking of a model-response step payload, each
/// `None` while it has produced none (thinking only, or no response yet).
fn response_texts(payload: &[u8]) -> Result<(Option<String>, Option<String>), Surprise> {
    let mut step_type = None;
    let mut response: Option<&[u8]> = None;
    for (number, value) in fields(payload)? {
        match (number, value) {
            (1, Wire::Varint(value)) => step_type = Some(value),
            (20, Wire::Bytes(bytes)) => response = Some(bytes),
            _ => {}
        }
    }
    if step_type != Some(STEP_TYPE_MODEL_RESPONSE) {
        return surprise(format!(
            "model-response payload declares step type {step_type:?}"
        ));
    }
    let Some(response) = response else {
        return Ok((None, None));
    };
    let (mut reply, mut thinking) = (None, None);
    for (number, value) in fields(response)? {
        if let (1 | 3, Wire::Bytes(bytes)) = (number, value) {
            let text = std::str::from_utf8(bytes)
                .map_err(|_| Surprise("response text is not UTF-8".into()))?
                .to_string();
            if number == 1 {
                reply = Some(text);
            } else {
                thinking = Some(text);
            }
        }
    }
    Ok((
        reply.filter(|text| !text.is_empty()),
        thinking.filter(|text| !text.is_empty()),
    ))
}

/// The reply text of a model-response step payload. `Ok(None)` while the
/// response has produced no reply text yet (thinking only).
pub fn reply_text(payload: &[u8]) -> Result<Option<String>, Surprise> {
    response_texts(payload).map(|(reply, _)| reply)
}

/// The thinking so far of a model-response step payload.
pub fn thinking_text(payload: &[u8]) -> Result<Option<String>, Surprise> {
    response_texts(payload).map(|(_, thinking)| thinking)
}

/// A tool call as a tool step records it.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub input: Option<Value>,
}

/// The call a tool step's payload names, or `None` when it names none. Never a
/// surprise: a tool step that does not read is simply not announced.
pub fn tool_call(payload: &[u8]) -> Option<ToolCall> {
    let top = fields(payload).ok()?;
    let info = top
        .iter()
        .find_map(|(number, value)| match (number, value) {
            (5, Wire::Bytes(bytes)) => Some(*bytes),
            _ => None,
        })?;
    let call =
        fields(info)
            .ok()?
            .into_iter()
            .find_map(|(number, value)| match (number, value) {
                (4, Wire::Bytes(bytes)) => Some(bytes),
                _ => None,
            })?;
    let (mut id, mut name, mut arguments) = (None, None, None);
    for (number, value) in fields(call).ok()? {
        if let Wire::Bytes(bytes) = value {
            let text = std::str::from_utf8(bytes).ok()?.to_string();
            match number {
                1 => id = Some(text),
                2 => name = Some(text),
                3 => arguments = Some(text),
                _ => {}
            }
        }
    }
    let name = name.filter(|name| !name.is_empty())?;
    Some(ToolCall {
        id: id.unwrap_or_default(),
        name,
        input: arguments
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .filter(Value::is_object),
    })
}

// --- what to say, given what the database shows ---------------------------

/// One statement of a step's reply text.
#[derive(Debug, Clone, PartialEq)]
pub struct Preview {
    pub idx: i64,
    pub seq: u64,
    pub text: String,
    pub completed: bool,
    /// `thinking`, `tool` or `subagent`; `None` is reply text.
    pub kind: Option<&'static str>,
    pub tool: Option<ToolCall>,
    pub tool_state: Option<&'static str>,
}

/// Follows the newest model step and decides when it has something to say.
pub struct StepWatch {
    first_poll: bool,
    current: Option<Current>,
}

struct Current {
    idx: i64,
    text: String,
    seq: u64,
    /// Seen at the first poll: already in the database when this reader
    /// arrived, so it says nothing until the step changes or a newer one starts.
    inherited: bool,
    completed: bool,
}

impl StepWatch {
    pub fn new() -> Self {
        Self {
            first_poll: true,
            current: None,
        }
    }

    /// Feed the newest model step. Returns what to publish, if anything.
    pub fn observe(&mut self, idx: i64, status: i64, text: Option<String>) -> Option<Preview> {
        let first = std::mem::replace(&mut self.first_poll, false);
        let text = text.unwrap_or_default();
        let step_changed = self
            .current
            .as_ref()
            .is_none_or(|current| current.idx != idx);
        if step_changed {
            if self
                .current
                .as_ref()
                .is_some_and(|current| idx < current.idx)
            {
                return None;
            }
            self.current = Some(Current {
                idx,
                text: String::new(),
                seq: 0,
                inherited: first,
                completed: false,
            });
            if first {
                let current = self.current.as_mut().expect("just set");
                current.text = text;
                current.completed = status != STATUS_STREAMING;
                return None;
            }
        }
        let current = self.current.as_mut().expect("set above");
        let grew = !text.is_empty() && text != current.text;
        if status == STATUS_STREAMING {
            if !grew {
                return None;
            }
            current.text = text;
            current.seq += 1;
            // A step that grows after this reader arrived is being streamed
            // now, whatever it looked like when first seen.
            current.inherited = false;
            return Some(Preview {
                idx,
                seq: current.seq,
                text: current.text.clone(),
                completed: false,
                kind: None,
                tool: None,
                tool_state: None,
            });
        }
        if status == STATUS_DONE && !current.completed {
            if grew {
                current.text = text;
            }
            if current.text.is_empty() || (current.inherited && !grew) {
                current.completed = true;
                return None;
            }
            current.completed = true;
            current.seq += 1;
            return Some(Preview {
                idx,
                seq: current.seq,
                text: current.text.clone(),
                completed: true,
                kind: None,
                tool: None,
                tool_state: None,
            });
        }
        None
    }
}

/// Follows the newest tool call. Like `StepWatch`, it says nothing about a call
/// already in the database when the reader arrived.
pub struct ToolWatch {
    first_poll: bool,
    current: Option<(i64, bool, u64)>,
}

impl ToolWatch {
    pub fn new() -> Self {
        Self {
            first_poll: true,
            current: None,
        }
    }

    /// A poll that found no tool step: whatever appears next is news.
    pub fn nothing_yet(&mut self) {
        self.first_poll = false;
    }

    /// Feed the newest tool step: its index, status and the call it names.
    pub fn observe(&mut self, idx: i64, status: i64, call: Option<ToolCall>) -> Option<Preview> {
        let first = std::mem::replace(&mut self.first_poll, false);
        let done = status == STATUS_DONE;
        match self.current {
            Some((current, _, _)) if idx < current => return None,
            Some((current, was_done, seq)) if current == idx => {
                if !done || was_done {
                    return None;
                }
                self.current = Some((idx, true, seq + 1));
                return Self::preview(idx, seq + 1, call?, "completed");
            }
            _ => {}
        }
        // A new call. One already finished or already there when the reader
        // arrived is history, not news.
        self.current = Some((idx, done, 1));
        if first {
            return None;
        }
        let state = if done { "completed" } else { "running" };
        Self::preview(idx, 1, call?, state)
    }

    fn preview(idx: i64, seq: u64, call: ToolCall, state: &'static str) -> Option<Preview> {
        let text = match call.input.as_ref() {
            Some(input) => crate::status_slot::tool_arguments_summary(&call.name, input),
            None => call.name.clone(),
        };
        Some(Preview {
            idx,
            seq,
            text,
            completed: false,
            kind: Some(crate::status_slot::tool_preview_kind(&call.name)),
            tool: Some(call),
            tool_state: Some(state),
        })
    }
}

// --- the database ---------------------------------------------------------

/// Open the conversation database read-only, or say why not yet.
fn open_read_only(db: &Path) -> Result<Option<Connection>, String> {
    let wal = sidecar(db, "-wal");
    let shm = sidecar(db, "-shm");
    // A read-only open of a WAL database whose sidecars are missing would
    // create them. Agy has them while it runs; without them it is not running
    // this conversation, so there is nothing to follow.
    if !db.is_file() || !wal.is_file() || !shm.is_file() {
        return Ok(None);
    }
    let conn = Connection::open_with_flags(
        db,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|error| format!("open: {error}"))?;
    conn.busy_timeout(BUSY_TIMEOUT)
        .map_err(|error| format!("busy_timeout: {error}"))?;
    conn.pragma_update(None, "query_only", true)
        .map_err(|error| format!("query_only: {error}"))?;
    Ok(Some(conn))
}

fn sidecar(db: &Path, suffix: &str) -> PathBuf {
    let mut name = db.as_os_str().to_os_string();
    name.push(suffix);
    PathBuf::from(name)
}

/// Size and modification time of a `-wal` file; `None` while it does not exist.
type WalMark = (u64, std::time::SystemTime);

/// One `stat`, no read: SQLite appends every committed change to the WAL (or
/// rewrites it from the start after a checkpoint), so either the size or the
/// modification time differs after any write. A reader never changes it.
fn wal_mark(wal: &Path) -> Option<WalMark> {
    let meta = std::fs::metadata(wal).ok()?;
    Some((meta.len(), meta.modified().ok()?))
}

/// Fail unless the database is the schema this reader was written against.
pub fn check_schema(conn: &Connection) -> Result<(), Surprise> {
    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .map_err(|error| Surprise(format!("user_version: {error}")))?;
    if version != SUPPORTED_USER_VERSION {
        return surprise(format!(
            "user_version {version}, expected {SUPPORTED_USER_VERSION}"
        ));
    }
    let mut statement = conn
        .prepare("PRAGMA table_info(steps)")
        .map_err(|error| Surprise(format!("table_info: {error}")))?;
    let columns: Vec<String> = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|error| Surprise(format!("table_info: {error}")))?
        .filter_map(Result::ok)
        .collect();
    for needed in ["idx", "step_type", "status", "step_payload"] {
        if !columns.iter().any(|column| column == needed) {
            return surprise(format!("steps has no column {needed}"));
        }
    }
    Ok(())
}

/// The newest model-response step: (idx, status, payload).
fn newest_model_step(conn: &Connection) -> rusqlite::Result<Option<(i64, i64, Vec<u8>)>> {
    let mut statement = conn.prepare(
        "SELECT idx, status, step_payload FROM steps WHERE step_type = ?1 ORDER BY idx DESC LIMIT 1",
    )?;
    let mut rows = statement.query([STEP_TYPE_MODEL_RESPONSE as i64])?;
    match rows.next()? {
        Some(row) => Ok(Some((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, Option<Vec<u8>>>(2)?.unwrap_or_default(),
        ))),
        None => Ok(None),
    }
}

/// The newest tool step: (idx, status, payload).
fn newest_tool_step(conn: &Connection) -> rusqlite::Result<Option<(i64, i64, Vec<u8>)>> {
    let mut statement = conn.prepare(
        "SELECT idx, status, step_payload FROM steps WHERE step_type = ?1 ORDER BY idx DESC LIMIT 1",
    )?;
    let mut rows = statement.query([STEP_TYPE_TOOL])?;
    match rows.next()? {
        Some(row) => Ok(Some((
            row.get::<_, i64>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, Option<Vec<u8>>>(2)?.unwrap_or_default(),
        ))),
        None => Ok(None),
    }
}

// --- the loop -------------------------------------------------------------

/// How often the hook state directory is rescanned for sessions.
const SCAN_EVERY: Duration = Duration::from_secs(1);
/// A state file untouched for this long belongs to no live session.
const STATE_FILE_RECENT: Duration = Duration::from_secs(30 * 60);
/// A "thinking" or "running" state older than this is a crashed turn, not a
/// stream, so the session drops back to the idle cadence.
const ACTIVE_STATE_MAX_AGE: Duration = Duration::from_secs(10 * 60);
/// Poll cadence while a turn is in progress, and while the session is idle.
const IDLE_POLL_INTERVAL: Duration = Duration::from_secs(1);

struct Following {
    conn: Connection,
    watch: StepWatch,
    thinking: StepWatch,
    tools: ToolWatch,
}

/// One Helm session being followed.
struct Session {
    session_id: String,
    run_id: String,
    conversation: String,
    active: bool,
    following: Option<Following>,
    errors: u32,
    backoff: Duration,
    next_poll: Instant,
    /// A write to the database may bring the next poll forward to here, never
    /// earlier: the floor that keeps a busy stream at one read per
    /// `POLL_INTERVAL` and an error backoff a backoff.
    earliest_poll: Instant,
    /// The `-wal` file as it was just before the last poll read the database.
    wal_seen: Option<WalMark>,
    /// Set once the conversation's database proved unreadable or unknown. The
    /// reader never touches that conversation again.
    stood_down: bool,
}

/// What one poll of a session found.
enum Tick {
    /// Nothing to follow yet.
    Wait,
    /// A transient problem; back off.
    Retry(String),
    /// The database is not what this reader knows; never read it again.
    StandDown(String),
}

impl Session {
    fn new(session_id: String, run_id: String, conversation: String) -> Self {
        Self {
            session_id,
            run_id,
            conversation,
            active: false,
            following: None,
            errors: 0,
            backoff: POLL_INTERVAL,
            next_poll: Instant::now(),
            earliest_poll: Instant::now(),
            wal_seen: None,
            stood_down: false,
        }
    }

    fn wal_path(&self, config: &HelmLiveConfig) -> PathBuf {
        sidecar(
            &config
                .store_root
                .join("conversations")
                .join(format!("{}.db", self.conversation)),
            "-wal",
        )
    }

    /// Whether to read the database at `now`: the cadence says so, or agy has
    /// written to it since the last read and the floor has passed.
    fn due(&self, config: &HelmLiveConfig, now: Instant) -> bool {
        now >= self.next_poll
            || (now >= self.earliest_poll && wal_mark(&self.wal_path(config)) != self.wal_seen)
    }

    fn poll(&mut self, config: &HelmLiveConfig) {
        let now = Instant::now();
        let cadence = if self.active {
            POLL_INTERVAL
        } else {
            IDLE_POLL_INTERVAL
        };
        // Taken before the read: a write that lands during it leaves the file
        // different from this mark, so the next slice reads again.
        self.wal_seen = wal_mark(&self.wal_path(config));
        self.earliest_poll = now + POLL_INTERVAL;
        match self.tick(config) {
            Ok(()) => {
                self.errors = 0;
                self.backoff = POLL_INTERVAL;
                self.next_poll = now + cadence;
            }
            Err(Tick::Wait) => self.next_poll = now + cadence,
            Err(Tick::Retry(message)) => {
                self.errors += 1;
                self.following = None;
                if self.errors >= MAX_CONSECUTIVE_ERRORS {
                    tracing::warn!(
                        session_id = %self.session_id,
                        error = %message,
                        "Antigravity live reply text stands down after repeated errors; the transcript still delivers each step"
                    );
                    self.stood_down = true;
                    return;
                }
                self.backoff = (self.backoff * 2).min(MAX_BACKOFF);
                self.next_poll = now + self.backoff;
                self.earliest_poll = self.next_poll;
            }
            Err(Tick::StandDown(message)) => {
                tracing::warn!(
                    session_id = %self.session_id,
                    reason = %message,
                    "Antigravity live reply text stands down: unrecognised conversation database; the transcript still delivers each step"
                );
                self.following = None;
                self.stood_down = true;
            }
        }
    }

    fn tick(&mut self, config: &HelmLiveConfig) -> Result<(), Tick> {
        if self.following.is_none() {
            let db = config
                .store_root
                .join("conversations")
                .join(format!("{}.db", self.conversation));
            let Some(conn) = open_read_only(&db).map_err(Tick::Retry)? else {
                return Err(Tick::Wait);
            };
            check_schema(&conn).map_err(|Surprise(message)| Tick::StandDown(message))?;
            self.following = Some(Following {
                conn,
                watch: StepWatch::new(),
                thinking: StepWatch::new(),
                tools: ToolWatch::new(),
            });
        }
        let current = self.following.as_mut().expect("opened above");
        let row = newest_model_step(&current.conn)
            .map_err(|error| Tick::Retry(format!("read: {error}")))?;
        let tool_row = newest_tool_step(&current.conn)
            .map_err(|error| Tick::Retry(format!("read: {error}")))?;
        let mut previews = Vec::new();
        if let Some((idx, status, payload)) = row {
            let (reply, thinking) =
                response_texts(&payload).map_err(|Surprise(message)| Tick::StandDown(message))?;
            // Thinking first: within a step it comes before the reply.
            if let Some(mut preview) = current.thinking.observe(
                idx,
                status,
                thinking.filter(|_| !crate::qa_fault::mutant("antigravity-helm-thinking-forward")),
            ) {
                preview.kind = if crate::qa_fault::mutant("antigravity-helm-thinking-as-reply") {
                    None
                } else {
                    Some("thinking")
                };
                preview.completed = false;
                previews.push(preview);
            }
            previews.extend(current.watch.observe(idx, status, reply));
        }
        match tool_row {
            Some((idx, status, payload)) => {
                previews.extend(
                    current.tools.observe(
                        idx,
                        status,
                        tool_call(&payload)
                            .filter(|_| !crate::qa_fault::mutant("antigravity-helm-tool-forward")),
                    ),
                );
            }
            None => current.tools.nothing_yet(),
        }
        for preview in previews {
            publish(config, self, &preview)
                .map_err(|error| Tick::Retry(format!("enqueue: {error}")))?;
        }
        Ok(())
    }
}

/// The Helm sessions with a recent state file, as (session, run, conversation,
/// active). A session whose hook has not yet named a conversation has nothing
/// to follow and is left out.
fn helm_sessions(config: &HelmLiveConfig) -> Vec<(String, String, String, bool)> {
    let now = std::time::SystemTime::now();
    let paths: Vec<PathBuf> = crate::managed_scan::state_file_paths(&config.state_dir)
        .into_iter()
        .filter(|path| {
            std::fs::metadata(path)
                .and_then(|meta| meta.modified())
                .ok()
                .and_then(|modified| now.duration_since(modified).ok())
                .is_some_and(|age| age <= STATE_FILE_RECENT)
        })
        .collect();
    crate::managed_antigravity_scan::collect_observations_from_paths(&paths)
        .into_iter()
        .filter(|observation| observation.connection_id.is_some())
        .filter_map(|observation| {
            let conversation = observation
                .provider_session_id
                .filter(|id| !id.contains(['/', '\\']) && id != "." && id != "..")?;
            let run_id = observation.run_id?;
            let fresh = chrono::DateTime::parse_from_rfc3339(&observation.updated_at)
                .ok()
                .is_some_and(|updated| {
                    chrono::Utc::now()
                        .signed_duration_since(updated)
                        .to_std()
                        .is_ok_and(|age| age <= ACTIVE_STATE_MAX_AGE)
                });
            let streaming = matches!(observation.state.as_deref(), Some("thinking" | "running"));
            Some((
                observation.session_id,
                run_id,
                conversation,
                streaming && fresh,
            ))
        })
        .collect()
}

fn rescan(config: &HelmLiveConfig, sessions: &mut HashMap<String, Session>) {
    let found = helm_sessions(config);
    sessions.retain(|id, _| found.iter().any(|(found_id, ..)| found_id == id));
    for (session_id, run_id, conversation, active) in found {
        let session = sessions
            .entry(session_id.clone())
            .or_insert_with(|| Session::new(session_id, run_id.clone(), conversation.clone()));
        if session.conversation != conversation || session.run_id != run_id {
            // agy moved to another conversation (or the run changed): a new
            // database, and a new chance to recognise it.
            *session = Session::new(session.session_id.clone(), run_id, conversation);
        }
        session.active = active;
    }
}

fn run(config: HelmLiveConfig, stop: Arc<AtomicBool>) {
    let mut sessions: HashMap<String, Session> = HashMap::new();
    let mut next_scan = Instant::now();
    while !stop.load(Ordering::SeqCst) {
        let now = Instant::now();
        if now >= next_scan {
            rescan(&config, &mut sessions);
            next_scan = now + SCAN_EVERY;
        }
        for session in sessions.values_mut() {
            if !session.stood_down && session.due(&config, now) {
                session.poll(&config);
            }
        }
        std::thread::sleep(LOOP_SLICE);
    }
}

/// The runtime event that states a preview. Shaped like the other Helm
/// providers' previews; the Runtime Host allowlists the source.
fn preview_event(session: &Session, preview: &Preview, occurred_at: &str) -> Value {
    let conversation = session.conversation.as_str();
    let turn_id = match (preview.kind, preview.tool.as_ref()) {
        (Some("thinking"), _) => format!("{conversation}:{}:thinking", preview.idx),
        (Some(_), Some(call)) => format!("{conversation}:{}:tool:{}", preview.idx, call.id),
        _ => format!("{conversation}:{}", preview.idx),
    };
    let mut text = String::new();
    crate::status_slot::append_live_text(&mut text, &preview.text);
    let mut event = json!({
        "runtime_key": format!("antigravity:{}", session.session_id),
        "session_id": session.session_id,
        "provider": "antigravity",
        "run_id": session.run_id,
        "source": SOURCE,
        "kind": "progress_signal",
        "occurred_at": occurred_at,
        "dedupe_key": format!("antigravity-helm-progress:{}:{turn_id}:{}", session.session_id, preview.seq),
        "payload": {
            "progress_kind": PROGRESS_KIND,
            "turn_id": turn_id,
            "seq": preview.seq,
            "run_id": session.run_id,
            "live_text": text,
            "turn_completed": preview.completed,
            "managed_transport": SOURCE,
            "execution_lifetime": "interactive",
            "provider_session_id": conversation,
        },
    });
    // Reply text carries none of these, so its payload is what it was.
    if let Some(payload) = event["payload"].as_object_mut() {
        if let Some(kind) = preview.kind {
            payload.insert("preview_kind".into(), json!(kind));
        }
        if let Some(call) = preview.tool.as_ref() {
            payload.insert("tool_name".into(), json!(call.name));
            payload.insert("tool_call_id".into(), json!(call.id));
            if let Some(input) = call.input.as_ref() {
                payload.insert("tool_input".into(), input.clone());
            }
        }
        if let Some(state) = preview.tool_state {
            payload.insert("tool_state".into(), json!(state));
        }
    }
    event
}

fn publish(config: &HelmLiveConfig, session: &Session, preview: &Preview) -> anyhow::Result<()> {
    let event = preview_event(session, preview, &chrono::Utc::now().to_rfc3339());
    crate::outbox::enqueue_runtime_event(&config.outbox_dir, &event)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn varint(mut value: u64) -> Vec<u8> {
        let mut out = Vec::new();
        loop {
            let byte = (value & 0x7f) as u8;
            value >>= 7;
            if value == 0 {
                out.push(byte);
                return out;
            }
            out.push(byte | 0x80);
        }
    }

    fn field_bytes(number: u64, bytes: &[u8]) -> Vec<u8> {
        let mut out = varint(number << 3 | 2);
        out.extend(varint(bytes.len() as u64));
        out.extend(bytes);
        out
    }

    fn field_varint(number: u64, value: u64) -> Vec<u8> {
        let mut out = varint(number << 3);
        out.extend(varint(value));
        out
    }

    /// A model-response payload shaped like the ones agy writes.
    fn payload(reply: Option<&str>, thinking: Option<&str>) -> Vec<u8> {
        let mut response = Vec::new();
        if let Some(reply) = reply {
            response.extend(field_bytes(1, reply.as_bytes()));
        }
        if let Some(thinking) = thinking {
            response.extend(field_bytes(3, thinking.as_bytes()));
        }
        response.extend(field_varint(12, 2));
        let mut out = field_varint(1, 15);
        out.extend(field_varint(4, 8));
        out.extend(field_bytes(5, &field_bytes(12, b"meta")));
        out.extend(field_bytes(20, &response));
        out
    }

    #[test]
    fn reply_text_is_field_one_and_thinking_is_never_returned() {
        let bytes = payload(Some("EXPM0000 birch"), Some("THINKING000 THINKING001"));
        assert_eq!(
            reply_text(&bytes).unwrap().as_deref(),
            Some("EXPM0000 birch")
        );
        let thinking_only = payload(None, Some("THINKING000"));
        assert_eq!(reply_text(&thinking_only).unwrap(), None);
    }

    #[test]
    fn a_payload_that_is_not_the_known_shape_is_a_surprise() {
        assert!(reply_text(&[0xff]).is_err(), "truncated varint");
        let mut wrong_type = field_varint(1, 14);
        wrong_type.extend(field_bytes(20, &field_bytes(1, b"hi")));
        assert!(
            reply_text(&wrong_type).is_err(),
            "step type 14 is not a model response"
        );
        let mut bad_utf8 = field_varint(1, 15);
        bad_utf8.extend(field_bytes(20, &field_bytes(1, &[0xff, 0xfe])));
        assert!(reply_text(&bad_utf8).is_err());
        let mut truncated = field_varint(1, 15);
        truncated.extend(varint(20 << 3 | 2));
        truncated.extend(varint(50));
        assert!(reply_text(&truncated).is_err(), "length runs past the end");
    }

    #[test]
    fn a_streaming_step_publishes_growth_then_completes_once() {
        let mut watch = StepWatch::new();
        assert_eq!(
            watch.observe(1, STATUS_STREAMING, None),
            None,
            "thinking only says nothing"
        );
        let first = watch
            .observe(1, STATUS_STREAMING, Some("EXPM0000".into()))
            .unwrap();
        assert_eq!(
            (first.seq, first.completed, first.text.as_str()),
            (1, false, "EXPM0000")
        );
        assert_eq!(
            watch.observe(1, STATUS_STREAMING, Some("EXPM0000".into())),
            None
        );
        let second = watch
            .observe(1, STATUS_STREAMING, Some("EXPM0000 birch".into()))
            .unwrap();
        assert_eq!(second.seq, 2);
        let done = watch
            .observe(1, STATUS_DONE, Some("EXPM0000 birch".into()))
            .unwrap();
        assert!(done.completed);
        assert_eq!((done.seq, done.text.as_str()), (3, "EXPM0000 birch"));
        assert_eq!(
            watch.observe(1, STATUS_DONE, Some("EXPM0000 birch".into())),
            None
        );
    }

    #[test]
    fn a_step_that_finished_between_polls_still_completes() {
        let mut watch = StepWatch::new();
        assert_eq!(
            watch.observe(1, STATUS_DONE, Some("old".into())),
            None,
            "inherited"
        );
        let done = watch
            .observe(3, STATUS_DONE, Some("whole reply".into()))
            .unwrap();
        assert!(done.completed);
        assert_eq!(done.text, "whole reply");
    }

    #[test]
    fn a_step_already_there_when_the_reader_arrives_says_nothing_until_it_changes() {
        let mut watch = StepWatch::new();
        assert_eq!(
            watch.observe(5, STATUS_STREAMING, Some("stale partial".into())),
            None
        );
        assert_eq!(
            watch.observe(5, STATUS_STREAMING, Some("stale partial".into())),
            None
        );
        let grown = watch
            .observe(5, STATUS_STREAMING, Some("stale partial more".into()))
            .unwrap();
        assert_eq!(grown.text, "stale partial more");
    }

    #[test]
    fn a_new_step_starts_a_new_message() {
        let mut watch = StepWatch::new();
        watch.observe(1, STATUS_STREAMING, None);
        watch
            .observe(1, STATUS_STREAMING, Some("one".into()))
            .unwrap();
        watch.observe(1, STATUS_DONE, Some("one".into())).unwrap();
        let next = watch
            .observe(3, STATUS_STREAMING, Some("two".into()))
            .unwrap();
        assert_eq!((next.idx, next.seq, next.text.as_str()), (3, 1, "two"));
    }

    fn config(dir: &Path) -> HelmLiveConfig {
        HelmLiveConfig {
            state_dir: dir.join("sessions"),
            store_root: dir.join("store"),
            outbox_dir: dir.join("outbox"),
        }
    }

    /// A Helm session's hook state file, as the launcher and hook leave it.
    fn write_state(config: &HelmLiveConfig, conversation: &str, state: &str) {
        std::fs::create_dir_all(&config.state_dir).unwrap();
        let path = config.state_dir.join("sess.json");
        std::fs::write(
            &path,
            json!({
                "schema_version": 3,
                "session_id": "sess",
                "connection_id": "conn",
                "lease_generation": "lease",
                "run_id": "run",
                "provider_session_id": conversation,
                "state": state,
                "updated_at": chrono::Utc::now().to_rfc3339(),
            })
            .to_string(),
        )
        .unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    }

    fn make_db(path: &Path, user_version: i64, columns: &str) -> Connection {
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        let conn = Connection::open(path).unwrap();
        conn.pragma_update(None, "journal_mode", "WAL").unwrap();
        conn.execute_batch(&format!(
            "CREATE TABLE steps ({columns}); PRAGMA user_version = {user_version};"
        ))
        .unwrap();
        conn
    }

    const GOOD_COLUMNS: &str =
        "idx integer PRIMARY KEY, step_type integer NOT NULL DEFAULT 0, status integer NOT NULL DEFAULT 0, step_payload blob";

    fn db_path(config: &HelmLiveConfig, conversation: &str) -> PathBuf {
        config
            .store_root
            .join("conversations")
            .join(format!("{conversation}.db"))
    }

    fn outbox_events(config: &HelmLiveConfig) -> Vec<Value> {
        let Ok(entries) = std::fs::read_dir(&config.outbox_dir) else {
            return Vec::new();
        };
        let mut events: Vec<Value> = entries
            .map(|entry| {
                serde_json::from_slice(&std::fs::read(entry.unwrap().path()).unwrap()).unwrap()
            })
            .collect();
        events.sort_by_key(|event| event["payload"]["seq"].as_u64());
        events
    }

    #[test]
    fn the_schema_gate_accepts_the_known_shape_and_refuses_everything_else() {
        let dir = tempfile::tempdir().unwrap();
        let good = make_db(&dir.path().join("good.db"), 1, GOOD_COLUMNS);
        assert!(check_schema(&good).is_ok());
        let newer = make_db(&dir.path().join("newer.db"), 2, GOOD_COLUMNS);
        assert!(check_schema(&newer)
            .unwrap_err()
            .0
            .contains("user_version 2"));
        let renamed = make_db(
            &dir.path().join("renamed.db"),
            1,
            "idx integer, step_type integer, status integer, payload blob",
        );
        assert!(check_schema(&renamed)
            .unwrap_err()
            .0
            .contains("step_payload"));
    }

    #[test]
    fn an_unknown_schema_stands_the_session_down_and_posts_nothing() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "thinking");
        let writer = make_db(&db_path(&config, "conv-1"), 7, GOOD_COLUMNS);
        writer
            .execute(
                "INSERT INTO steps VALUES (1, 15, 8, ?1)",
                [payload(Some("must not be read"), None)],
            )
            .unwrap();
        assert!(sidecar(&db_path(&config, "conv-1"), "-wal").is_file());

        let mut sessions = HashMap::new();
        rescan(&config, &mut sessions);
        let session = sessions.get_mut("sess").expect("a Helm session is found");
        assert!(session.active);
        session.poll(&config);
        assert!(
            session.stood_down,
            "an unknown user_version stands the reader down"
        );
        assert!(session.following.is_none(), "the connection is closed");
        assert!(outbox_events(&config).is_empty(), "nothing was posted");
        // It stays down: a rescan of the same conversation does not reset it.
        rescan(&config, &mut sessions);
        assert!(sessions["sess"].stood_down);
    }

    #[test]
    fn a_payload_that_does_not_parse_stands_the_session_down() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "thinking");
        let writer = make_db(&db_path(&config, "conv-1"), 1, GOOD_COLUMNS);
        writer
            .execute(
                "INSERT INTO steps VALUES (1, 15, 8, ?1)",
                [vec![0xffu8, 0xff]],
            )
            .unwrap();
        let mut sessions = HashMap::new();
        rescan(&config, &mut sessions);
        let session = sessions.get_mut("sess").unwrap();
        session.poll(&config);
        assert!(session.stood_down);
        assert!(outbox_events(&config).is_empty());
    }

    #[test]
    fn a_known_schema_streams_reply_text_and_labelled_thinking_to_the_outbox() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "thinking");
        let writer = make_db(&db_path(&config, "conv-1"), 1, GOOD_COLUMNS);
        writer
            .execute(
                "INSERT INTO steps VALUES (0, 14, 3, ?1)",
                [field_varint(1, 14)],
            )
            .unwrap();
        writer
            .execute(
                "INSERT INTO steps VALUES (1, 15, 8, ?1)",
                [payload(None, Some("THINKING000"))],
            )
            .unwrap();
        let mut sessions = HashMap::new();
        rescan(&config, &mut sessions);
        let session = sessions.get_mut("sess").unwrap();
        session.poll(&config);
        for (status, reply) in [
            (8, "EXPM0000"),
            (8, "EXPM0000 birch"),
            (3, "EXPM0000 birch"),
        ] {
            writer
                .execute(
                    "UPDATE steps SET status = ?1, step_payload = ?2 WHERE idx = 1",
                    rusqlite::params![
                        status,
                        payload(Some(reply), Some("THINKING000 THINKING001"))
                    ],
                )
                .unwrap();
            session.poll(&config);
        }
        assert!(!session.stood_down);
        let events = outbox_events(&config);
        let is_thinking = |event: &&Value| event["payload"]["preview_kind"] == "thinking";
        let thinking: Vec<_> = events.iter().filter(is_thinking).cloned().collect();
        let events: Vec<_> = events.iter().filter(|e| !is_thinking(e)).cloned().collect();
        let texts: Vec<_> = events
            .iter()
            .map(|event| event["payload"]["live_text"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(texts, ["EXPM0000", "EXPM0000 birch", "EXPM0000 birch"]);
        // Reply events never carry thinking text or a kind label.
        assert!(events
            .iter()
            .all(|event| !event.to_string().contains("THINKING")
                && event["payload"].get("preview_kind").is_none()));
        // Thinking is its own labelled item, never completed, never reply text.
        assert!(!thinking.is_empty());
        for event in &thinking {
            assert_eq!(event["payload"]["turn_id"], "conv-1:1:thinking");
            assert_eq!(event["payload"]["turn_completed"], false);
            assert!(!event.to_string().contains("EXPM"));
        }
        assert_eq!(
            thinking[0]["payload"]["live_text"],
            "THINKING000 THINKING001"
        );
        assert_eq!(events[2]["payload"]["turn_completed"], true);
        assert_eq!(events[0]["source"], SOURCE);
        assert_eq!(events[0]["session_id"], "sess");
        assert_eq!(events[0]["run_id"], "run");
        assert_eq!(events[0]["payload"]["progress_kind"], PROGRESS_KIND);
        assert_eq!(events[0]["payload"]["turn_id"], "conv-1:1");
        assert_eq!(events[0]["payload"]["provider_session_id"], "conv-1");
    }

    /// A tool step, as agy writes it: field 5 holds, in field 4, the call id, the
    /// tool name and the arguments as JSON text.
    fn tool_payload(id: &str, name: &str, arguments: &str) -> Vec<u8> {
        let mut call = field_bytes(1, id.as_bytes());
        call.extend(field_bytes(2, name.as_bytes()));
        call.extend(field_bytes(3, arguments.as_bytes()));
        let mut info = field_bytes(4, &call);
        info.extend(field_varint(3, 2));
        let mut out = field_varint(1, STEP_TYPE_TOOL as u64);
        out.extend(field_bytes(5, &info));
        out
    }

    #[test]
    fn a_tool_step_names_its_call_and_a_bad_one_names_nothing() {
        let call = tool_call(&tool_payload(
            "call_1",
            "run_command",
            r#"{"CommandLine":"ls"}"#,
        ))
        .expect("a call");
        assert_eq!(
            (call.id.as_str(), call.name.as_str()),
            ("call_1", "run_command")
        );
        assert_eq!(call.input.unwrap()["CommandLine"], "ls");
        assert_eq!(tool_call(&field_varint(1, 132)), None);
        assert_eq!(tool_call(&[0xff, 0xff]), None);
    }

    #[test]
    fn a_tool_call_is_announced_running_then_completed_and_history_is_not() {
        let call = |name: &str| tool_call(&tool_payload("c", name, r#"{"CommandLine":"ls -la"}"#));
        let mut watch = ToolWatch::new();
        // Already there when the reader arrived: silent.
        assert_eq!(watch.observe(4, 3, call("run_command")), None);
        let started = watch.observe(6, 2, call("run_command")).expect("running");
        assert_eq!(started.kind, Some("tool"));
        assert_eq!(started.tool_state, Some("running"));
        assert_eq!(started.text, "ls -la");
        assert_eq!(watch.observe(6, 2, call("run_command")), None, "unchanged");
        let done = watch.observe(6, 3, call("run_command")).expect("completed");
        assert_eq!(done.tool_state, Some("completed"));
        assert!(done.seq > started.seq);
        assert_eq!(watch.observe(6, 3, call("run_command")), None, "once");
        let agent = watch
            .observe(8, 2, call("invoke_subagent"))
            .expect("running");
        assert_eq!(agent.kind, Some("subagent"));
    }

    #[test]
    fn a_tool_step_in_the_database_reaches_the_outbox_labelled_with_its_name() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "running");
        let writer = make_db(&db_path(&config, "conv-1"), 1, GOOD_COLUMNS);
        writer
            .execute(
                "INSERT INTO steps VALUES (0, 14, 3, ?1)",
                [field_varint(1, 14)],
            )
            .unwrap();
        let mut sessions = HashMap::new();
        rescan(&config, &mut sessions);
        let session = sessions.get_mut("sess").unwrap();
        session.poll(&config);
        writer
            .execute(
                "INSERT INTO steps VALUES (1, 132, 2, ?1)",
                [tool_payload(
                    "call_9",
                    "run_command",
                    r#"{"CommandLine":": TTMARK0000"}"#,
                )],
            )
            .unwrap();
        session.poll(&config);
        let events = outbox_events(&config);
        assert_eq!(events.len(), 1);
        let payload = &events[0]["payload"];
        assert_eq!(payload["preview_kind"], "tool");
        assert_eq!(payload["tool_name"], "run_command");
        assert_eq!(payload["tool_call_id"], "call_9");
        assert_eq!(payload["tool_state"], "running");
        assert_eq!(payload["live_text"], ": TTMARK0000");
        assert_eq!(payload["tool_input"]["CommandLine"], ": TTMARK0000");
    }

    #[test]
    fn the_thread_follows_a_session_and_stops_promptly() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "thinking");
        let writer = make_db(&db_path(&config, "conv-1"), 1, GOOD_COLUMNS);
        writer
            .execute(
                "INSERT INTO steps VALUES (1, 15, 8, ?1)",
                [payload(None, None)],
            )
            .unwrap();
        let live = spawn(config.clone()).expect("enabled");
        std::thread::sleep(Duration::from_millis(400));
        writer
            .execute(
                "UPDATE steps SET step_payload = ?1 WHERE idx = 1",
                [payload(Some("EXPM0000"), None)],
            )
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while outbox_events(&config).is_empty() {
            assert!(Instant::now() < deadline, "no preview arrived");
            std::thread::sleep(Duration::from_millis(20));
        }
        let stopping = Instant::now();
        live.stop();
        assert!(stopping.elapsed() < Duration::from_secs(1));
        assert_eq!(
            outbox_events(&config)[0]["payload"]["live_text"],
            "EXPM0000"
        );
    }

    #[test]
    fn an_idle_session_is_read_when_agy_writes_not_a_second_later() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "idle");
        let writer = make_db(&db_path(&config, "conv-1"), 1, GOOD_COLUMNS);
        writer
            .execute(
                "INSERT INTO steps VALUES (1, 15, 3, ?1)",
                [payload(Some("the previous turn"), None)],
            )
            .unwrap();
        let mut session = Session::new("sess".into(), "run".into(), "conv-1".into());
        session.poll(&config);
        assert!(session.following.is_some());
        assert_eq!(
            session.next_poll - session.earliest_poll,
            IDLE_POLL_INTERVAL - POLL_INTERVAL,
            "an idle session is on the slow cadence"
        );
        let just_before_the_slow_poll = session.next_poll - Duration::from_millis(1);
        assert!(
            !session.due(&config, just_before_the_slow_poll),
            "nothing was written, so nothing is read before the slow poll"
        );

        // The next turn opens with a tool call. The hook state still says idle.
        writer
            .execute(
                "INSERT INTO steps VALUES (2, 132, 8, ?1)",
                [tool_payload(
                    "call_1",
                    "run_command",
                    r#"{"CommandLine":"ls"}"#,
                )],
            )
            .unwrap();
        assert!(
            !session.due(&config, session.earliest_poll - Duration::from_millis(1)),
            "a write never brings a read closer than one poll interval"
        );
        assert!(
            session.due(&config, session.earliest_poll),
            "the write is read at the floor, not at the slow poll"
        );
        session.poll(&config);
        let events = outbox_events(&config);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["payload"]["tool_name"], "run_command");
        assert!(
            !session.due(&config, session.next_poll - Duration::from_millis(1)),
            "the reader's own read does not look like a write"
        );
    }

    #[test]
    fn a_read_error_backoff_is_not_shortened_by_a_write() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        let writer = make_db(&db_path(&config, "conv-1"), 1, GOOD_COLUMNS);
        let mut session = Session::new("sess".into(), "run".into(), "conv-1".into());
        session.poll(&config);
        assert!(session.following.is_some());
        // The table goes away under the reader: every read now fails.
        writer.execute_batch("DROP TABLE steps;").unwrap();
        session.poll(&config);
        assert_eq!(session.errors, 1);
        writer
            .execute_batch("CREATE TABLE other (x integer);")
            .unwrap();
        assert!(!session.due(&config, session.next_poll - Duration::from_millis(1)));
        assert!(session.due(&config, session.next_poll));
    }

    #[test]
    fn a_shadow_session_and_an_idle_one_are_not_followed_actively() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "conv-1", "idle");
        let found = helm_sessions(&config);
        assert_eq!(found.len(), 1);
        assert!(!found[0].3, "an idle session is on the slow cadence");
        // A state file with no control identity is a Shadow session.
        let path = config.state_dir.join("sess.json");
        let mut state: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        state.as_object_mut().unwrap().remove("connection_id");
        std::fs::write(&path, state.to_string()).unwrap();
        assert!(helm_sessions(&config).is_empty());
    }

    #[test]
    fn a_database_without_its_wal_sidecars_is_not_opened() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("c.db");
        {
            let conn = make_db(&db, 1, GOOD_COLUMNS);
            conn.pragma_update(None, "journal_mode", "DELETE").unwrap();
        }
        assert!(!sidecar(&db, "-wal").exists());
        assert!(open_read_only(&db).unwrap().is_none());
        assert!(!sidecar(&db, "-wal").exists() && !sidecar(&db, "-shm").exists());
    }

    #[test]
    fn a_conversation_id_cannot_leave_the_conversations_directory() {
        let dir = tempfile::tempdir().unwrap();
        let config = config(dir.path());
        write_state(&config, "../../etc/passwd", "thinking");
        assert!(helm_sessions(&config).is_empty());
    }
}
