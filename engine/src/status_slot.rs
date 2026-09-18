//! One current status per managed session.
//!
//! A phase or a live preview is a statement about *now*: the next one replaces
//! it. Publishing each as a durable queue entry made queue length a function of
//! how chatty a provider is rather than how many sessions exist, and on
//! 2026-09-17 that reached 551,920 files and froze every session's served
//! status for 1h45m.
//!
//! A slot is one file per session, overwritten in place. It is written with a
//! temp file and a rename so a reader never sees half a value, but it is not
//! fsynced: losing the newest status to a crash costs nothing, because the
//! session will restate it within a keepalive. Durable records — a run ending,
//! an identity binding, an interaction — keep their own queue, where losing one
//! would be a real loss.
//!
//! The slot is also what makes delivery need no queue of its own. The daemon
//! sends from the slot and advances only on success, so a failed send simply
//! sends the newer value next tick. A daemon restart re-reads the slots and is
//! immediately current.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::Value;

#[cfg(unix)]
use std::os::unix::fs::PermissionsExt;

pub const STATUS_SLOT_SCHEMA: u32 = 1;

/// The live preview carried alongside a phase.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct StatusPreview {
    pub turn_id: String,
    pub seq: u64,
    pub live_text: String,
    pub turn_completed: bool,
    #[serde(default)]
    pub progress_kind: String,
    #[serde(default)]
    pub provider_session_id: Option<String>,
}

/// Everything the Machine Agent needs to state a session's current status.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StatusSlot {
    pub schema: u32,
    pub session_id: String,
    pub provider: String,
    pub runtime_key: String,
    pub run_id: String,
    pub source: String,
    pub phase: String,
    #[serde(default)]
    pub tool_name: Option<String>,
    pub observed_at: String,
    #[serde(default)]
    pub payload: Value,
    #[serde(default)]
    pub preview: Option<StatusPreview>,
    /// Identifies the writing process. A relaunch starts a new epoch, so the
    /// daemon never treats a fresh session's first status as old.
    pub producer_epoch: String,
    /// Increments on every write within an epoch.
    pub seq: u64,
}

impl StatusSlot {
    /// What the daemon compares to decide whether it has already sent this.
    pub fn version(&self) -> (String, u64) {
        (self.producer_epoch.clone(), self.seq)
    }
}

/// The runtime events a slot states: the phase always, and the live preview
/// when the session has one.
///
/// Status is delivered straight from the slot, with no queue of its own: the
/// slot is the durable copy, so a failed send simply sends the newer value on
/// the next tick, and a daemon restart is current as soon as it reads them.
pub fn runtime_events(slot: &StatusSlot) -> Vec<Value> {
    let mut events = Vec::new();
    // A producer without a run identity says so by omission rather than by
    // sending an empty string. The Codex bridge has none: its activity has
    // never bound to a run, and inventing one here to satisfy the shape would
    // bind it to a run that does not exist.
    let run_id: Value = if slot.run_id.trim().is_empty() {
        Value::Null
    } else {
        Value::String(slot.run_id.clone())
    };
    // Some phases are local-health vocabulary the Runtime Host does not accept
    // — `finished` is the one a Console turn ends on. The durable enqueue path
    // refused those at the producer; the slot path has to refuse them here, or
    // the daemon posts a 422 every tick until the slot is retired.
    if crate::managed_phase_contract::is_wire_phase(&slot.phase) {
        events.push(serde_json::json!({
        "runtime_key": slot.runtime_key,
        "session_id": slot.session_id,
        "provider": slot.provider,
        "run_id": run_id,
        "source": slot.source,
        "kind": "phase_signal",
        "phase": slot.phase,
        "tool_name": slot.tool_name,
        "occurred_at": slot.observed_at,
        "dedupe_key": format!(
            "{}-phase:{}:{}:{}:{}",
            slot.provider, slot.session_id, slot.run_id, slot.phase, slot.observed_at
        ),
        "payload": slot.payload,
        }));
    }
    if let Some(preview) = slot.preview.as_ref() {
        events.push(serde_json::json!({
            "runtime_key": slot.runtime_key,
            "session_id": slot.session_id,
            "provider": slot.provider,
            "run_id": run_id,
            "source": slot.source,
            "kind": "progress_signal",
            "occurred_at": slot.observed_at,
            "dedupe_key": format!(
                "{}-progress:{}:{}:{}:{}",
                slot.provider, slot.session_id, slot.run_id, preview.turn_id, preview.seq
            ),
            "payload": {
                "progress_kind": preview.progress_kind,
                "turn_id": preview.turn_id,
                "seq": preview.seq,
                "run_id": slot.run_id,
                "live_text": preview.live_text,
                "turn_completed": preview.turn_completed,
                "managed_transport": slot.source,
                "execution_lifetime": "interactive",
                "provider_session_id": preview.provider_session_id,
            },
        }));
    }
    events
}

/// How often a live slot restates that the session is still running.
///
/// The lease is short by design — freshness is about whether the machine is
/// reporting, not about how long a phase ought to last — and a hook provider
/// says nothing between its own events. This is the Machine Agent saying so
/// anyway, which is what keeps a ten-minute tool call current without inventing
/// a phase: the assertion carries the phase and its observation time unchanged.
pub const STATUS_ASSERTION_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5);

/// The assertion a live slot states: the same status, restated, with no phase.
///
/// Deliberately not a `phase_signal`. Shipping the same phase again is a change
/// the host would apply — bumping the runtime revision and re-anchoring the
/// phase — for a statement that says nothing new about the provider. This says
/// only what the phase cannot: the machine is still here and still willing to
/// report. Its identity is the assertion time, so a replay restates one the host
/// has already accepted rather than renewing anything.
pub fn assertion_runtime_event(slot: &StatusSlot, asserted_at: chrono::DateTime<chrono::Utc>) -> Value {
    let run_id: Value = if slot.run_id.trim().is_empty() {
        Value::Null
    } else {
        Value::String(slot.run_id.clone())
    };
    let asserted = asserted_at.to_rfc3339();
    serde_json::json!({
        "runtime_key": slot.runtime_key,
        "session_id": slot.session_id,
        "provider": slot.provider,
        "run_id": run_id,
        "source": slot.source,
        "kind": "status_assertion",
        "phase": Value::Null,
        "tool_name": slot.tool_name,
        "occurred_at": asserted,
        "dedupe_key": format!("{}-assert:{}:{}:{}", slot.provider, slot.session_id, slot.run_id, asserted),
        "payload": {
            // What the assertion is about, so a reader can tell the machine's
            // freshness from the provider's without reading the phase back out.
            "asserted_at": asserted,
            "observed_at": slot.observed_at,
        },
    })
}

pub fn status_slot_dir(agent_dir: &Path) -> PathBuf {
    agent_dir.join("status")
}

pub fn slot_path(dir: &Path, session_id: &str) -> PathBuf {
    dir.join(format!("{session_id}.json"))
}

/// Overwrite this session's slot. Atomic for readers, not fsynced.
pub fn publish(dir: &Path, slot: &StatusSlot) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    #[cfg(unix)]
    std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o700))?;

    let ready = slot_path(dir, &slot.session_id);
    // A unique temp name per write, created exclusively: two writers sharing
    // one temp path can interleave their bytes and publish the mixture.
    let temporary = dir.join(format!(
        ".{}.{}.{}.tmp",
        slot.session_id,
        std::process::id(),
        uuid::Uuid::new_v4()
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    #[cfg(unix)]
    file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    let bytes = serde_json::to_vec(slot).map_err(std::io::Error::other)?;
    file.write_all(&bytes)?;
    drop(file);
    if let Err(error) = std::fs::rename(&temporary, &ready) {
        let _ = std::fs::remove_file(&temporary);
        return Err(error);
    }
    Ok(())
}

/// Publish only if this observation is newer than the slot already holds.
///
/// A launcher owns its slot and publishes in order, so it needs no guard. A
/// hook does not: it is a short-lived process, several can run at once, and
/// two firing close together would otherwise race to overwrite one slot with
/// nothing to arbitrate between them — the older observation could win purely
/// by finishing last. The lock makes the read-compare-write one step across
/// processes, and the comparison is observation time, which is what the
/// Runtime Host compares too.
///
/// Returns whether the slot was written.
pub fn publish_if_newer(dir: &Path, slot: &StatusSlot) -> std::io::Result<bool> {
    std::fs::create_dir_all(dir)?;
    let _guard = lock_session(dir, &slot.session_id)?;
    if let Some(current) = read_slot(&slot_path(dir, &slot.session_id)) {
        let (Some(existing), Some(incoming)) = (
            parse_observed_at(&current.observed_at),
            parse_observed_at(&slot.observed_at),
        ) else {
            // An unreadable timestamp on either side is not evidence that this
            // observation is older, so publishing is the safe direction.
            publish(dir, slot)?;
            return Ok(true);
        };
        if existing > incoming {
            return Ok(false);
        }
    }
    publish(dir, slot)?;
    Ok(true)
}

fn parse_observed_at(value: &str) -> Option<chrono::DateTime<chrono::Utc>> {
    chrono::DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|value| value.with_timezone(&chrono::Utc))
}

fn read_slot(path: &Path) -> Option<StatusSlot> {
    let bytes = std::fs::read(path).ok()?;
    serde_json::from_slice::<StatusSlot>(&bytes)
        .ok()
        .filter(|slot| slot.schema == STATUS_SLOT_SCHEMA)
}

/// Hold an exclusive lock for one session's slot. The lock file is separate
/// from the slot so the atomic rename that publishes it stays untouched.
fn lock_session(dir: &Path, session_id: &str) -> std::io::Result<std::fs::File> {
    let lock_path = dir.join(format!(".{session_id}.lock"));
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options.open(&lock_path)?;
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(file)
}

/// Read every current slot. Bounded by the number of live sessions.
pub fn read_all(dir: &Path) -> Vec<StatusSlot> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut slots = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let is_slot = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".json") && !name.starts_with('.'));
        if !is_slot {
            continue;
        }
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        match serde_json::from_slice::<StatusSlot>(&bytes) {
            Ok(slot) if slot.schema == STATUS_SLOT_SCHEMA => slots.push(slot),
            // A slot from a newer or malformed writer is left alone rather than
            // guessed at: it is current truth for someone.
            _ => continue,
        }
    }
    slots
}

/// Drop a session's slot. The producer does this when the session ends.
pub fn retire(dir: &Path, session_id: &str) {
    let _ = std::fs::remove_file(slot_path(dir, session_id));
}

/// Age after which a slot no producer is maintaining is removed. A crashed
/// launcher cannot retire its own slot, and nothing else knows it is gone.
pub const STATUS_SLOT_ABANDONED_AFTER: std::time::Duration =
    std::time::Duration::from_secs(24 * 60 * 60);

/// Remove slots nothing has touched for a day. Bounded work: the directory
/// holds one file per session.
pub fn sweep_abandoned(dir: &Path, older_than: std::time::Duration) -> usize {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return 0;
    };
    let now = std::time::SystemTime::now();
    let mut removed = 0usize;
    for entry in entries.flatten() {
        let path = entry.path();
        let is_slot = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| name.ends_with(".json") && !name.starts_with('.'));
        if !is_slot {
            continue;
        }
        let abandoned = entry
            .metadata()
            .and_then(|meta| meta.modified())
            .ok()
            .and_then(|modified| now.duration_since(modified).ok())
            .is_some_and(|age| age > older_than);
        if abandoned && std::fs::remove_file(&path).is_ok() {
            removed += 1;
        }
    }
    removed
}

/// Writes one session's status slot.
///
/// Every managed provider needs the same thing: state the current phase, carry
/// the newest preview, publish a transition immediately, and coalesce a stream
/// that restates itself every few milliseconds. Keeping one implementation is
/// the point — six copies of this would be six places to get the coalescing
/// window or the retirement rule subtly wrong.
/// One statement of a session's current status.
pub struct StatusUpdate<'a> {
    pub session_id: &'a str,
    pub run_id: &'a str,
    pub observed_at: &'a str,
    pub phase: &'a str,
    pub tool: Option<&'a str>,
    pub preview: Option<StatusPreview>,
    /// Merged into the published payload, for the fields only this provider
    /// knows about.
    pub extra_payload: Option<Value>,
}

impl<'a> StatusUpdate<'a> {
    pub fn phase(session_id: &'a str, run_id: &'a str, observed_at: &'a str, phase: &'a str) -> Self {
        Self {
            session_id,
            run_id,
            observed_at,
            phase,
            tool: None,
            preview: None,
            extra_payload: None,
        }
    }

    pub fn with_tool(mut self, tool: Option<&'a str>) -> Self {
        self.tool = tool;
        self
    }

    pub fn with_preview(mut self, preview: Option<StatusPreview>) -> Self {
        self.preview = preview;
        self
    }

    pub fn with_payload(mut self, extra: Value) -> Self {
        self.extra_payload = Some(extra);
        self
    }
}

/// State a Console turn's phase in the session's slot.
///
/// Console adapters are one-shot and headless, but their status is the same
/// kind of claim a Helm session makes: replaceable, restated often, and worth
/// exactly one file. What ends a Console turn is its terminal record, which
/// stays on the durable queue — so the slot carrying the last phase is not
/// load-bearing for knowing the run finished.
pub fn publish_console_phase(
    provider: &str,
    transport: &str,
    session_id: &str,
    run_id: &str,
    observed_at: &str,
    phase: &str,
    tool: Option<&str>,
    extra_payload: Value,
) {
    publisher_for(provider, transport, session_id).publish(
        StatusUpdate::phase(session_id, run_id, observed_at, phase)
            .with_tool(tool)
            .with_payload(extra_payload),
    );
}

/// Publishers are per session and per process: the epoch and sequence they
/// carry are how the daemon tells a fresh statement from one it already sent,
/// so a provider that publishes from free functions shares one here rather
/// than minting a new epoch per call.
static PUBLISHERS: std::sync::OnceLock<Mutex<HashMap<(String, String), Arc<StatusPublisher>>>> =
    std::sync::OnceLock::new();

pub fn publisher_for(provider: &str, transport: &str, session_id: &str) -> Arc<StatusPublisher> {
    let registry = PUBLISHERS.get_or_init(|| Mutex::new(HashMap::new()));
    let mut guard = registry.lock().expect("status publisher registry poisoned");
    guard
        .entry((provider.to_string(), session_id.to_string()))
        .or_insert_with(|| Arc::new(StatusPublisher::for_provider(provider, transport)))
        .clone()
}

pub struct StatusPublisher {
    dir: PathBuf,
    provider: String,
    transport: String,
    epoch: String,
    coalesce: Duration,
    state: Mutex<StatusPublisherState>,
}

#[derive(Default)]
struct StatusPublisherState {
    preview: Option<StatusPreview>,
    last_written: Option<Instant>,
    last_phase: Option<(String, Option<String>, Option<String>)>,
    seq: u64,
    /// A retired session has no current status. Nothing may recreate its slot,
    /// including a frame that was already in flight when the run ended.
    retired: bool,
}

/// How long a preview-only update waits before the slot is rewritten. A token
/// stream restates the preview every few milliseconds; the reader only ever
/// wants the newest one.
pub const STATUS_SLOT_COALESCE: Duration = Duration::from_millis(100);

impl StatusPublisher {
    pub fn new(dir: PathBuf, provider: &str, transport: &str) -> Self {
        Self {
            dir,
            provider: provider.to_string(),
            transport: transport.to_string(),
            epoch: uuid::Uuid::new_v4().to_string(),
            coalesce: STATUS_SLOT_COALESCE,
            state: Mutex::new(StatusPublisherState::default()),
        }
    }

    /// Where this machine's status slots live. A launcher that cannot resolve
    /// the agent directory writes nowhere rather than guessing at a path.
    pub fn for_provider(provider: &str, transport: &str) -> Self {
        let dir = crate::config::get_agent_dir()
            .map(|agent| status_slot_dir(&agent))
            .unwrap_or_else(|_| PathBuf::from("/dev/null/longhouse-status"));
        Self::new(dir, provider, transport)
    }

    /// Publish the current phase, and the newest preview if there is one.
    ///
    /// A phase or tool change publishes immediately, and so does a completed
    /// turn: its final preview is what a reader keeps until the next turn
    /// starts, and coalescing it away leaves a truncated answer behind.
    pub fn publish(&self, update: StatusUpdate<'_>) {
        let StatusUpdate {
            session_id,
            run_id,
            observed_at,
            phase,
            tool,
            preview,
            extra_payload,
        } = update;
        // The lock is held across the write. Dropping it first let two frames
        // race and land out of order, so the slot could end up holding the
        // older of two states under the newer sequence.
        let mut guard = self.state.lock().expect("status publisher mutex poisoned");
        if guard.retired {
            return;
        }
        let completes_turn = preview.as_ref().is_some_and(|preview| preview.turn_completed);
        if let Some(preview) = preview {
            guard.preview = Some(preview);
        }
        // The payload is part of the statement, not decoration. Codex carries
        // `pause_request_still_pending` and stall evidence there, and the
        // Runtime Host takes a different branch on each — coalescing a payload
        // change away because the phase name held still would drop a
        // transition that resolves a pending question.
        let phase_key = (
            phase.to_string(),
            tool.map(str::to_string),
            extra_payload.as_ref().map(|value| value.to_string()),
        );
        let transition = guard.last_phase.as_ref() != Some(&phase_key);
        let due = guard
            .last_written
            .map(|written| written.elapsed() >= self.coalesce)
            .unwrap_or(true);
        if !transition && !due && !completes_turn {
            return;
        }
        guard.last_phase = Some(phase_key);
        guard.last_written = Some(Instant::now());
        guard.seq += 1;
        let slot = StatusSlot {
            schema: STATUS_SLOT_SCHEMA,
            session_id: session_id.to_string(),
            provider: self.provider.clone(),
            runtime_key: format!("{}:{session_id}", self.provider),
            run_id: run_id.to_string(),
            source: self.transport.clone(),
            phase: phase.to_string(),
            tool_name: tool.map(str::to_string),
            observed_at: observed_at.to_string(),
            payload: {
                let mut payload = serde_json::json!({
                    "managed_transport": self.transport,
                    "execution_lifetime": "interactive",
                    "structured_remote_approval": false,
                });
                // A provider may have to say something only it knows, such as
                // the native session id its own events are keyed by.
                if let (Some(Value::Object(extra)), Some(target)) =
                    (extra_payload, payload.as_object_mut())
                {
                    for (key, value) in extra {
                        target.insert(key, value);
                    }
                }
                payload
            },
            preview: guard.preview.clone(),
            producer_epoch: self.epoch.clone(),
            seq: guard.seq,
        };
        if let Err(error) = publish(&self.dir, &slot) {
            eprintln!(
                "[{}-helm] status slot publish failed for {}: {error}",
                self.provider, slot.session_id
            );
        }
    }

    /// A new turn starts with no preview. Without this the next phase snapshot
    /// carries the previous turn's text.
    pub fn clear_preview(&self) {
        let mut guard = self.state.lock().expect("status publisher mutex poisoned");
        guard.preview = None;
    }

    /// The run is over: there is no current status to state. Callers retire
    /// only once the terminal record is durable, so a failure in between
    /// cannot lose both the status and the evidence that the run ended.
    pub fn retire(&self, session_id: &str) {
        let mut guard = self.state.lock().expect("status publisher mutex poisoned");
        guard.retired = true;
        guard.preview = None;
        retire(&self.dir, session_id);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    fn slot(session: &str, phase: &str, seq: u64) -> StatusSlot {
        StatusSlot {
            schema: STATUS_SLOT_SCHEMA,
            session_id: session.into(),
            provider: "omp".into(),
            runtime_key: format!("omp:{session}"),
            run_id: "run-1".into(),
            source: "omp_helm_channel".into(),
            phase: phase.into(),
            tool_name: None,
            observed_at: "2026-09-17T15:00:00Z".into(),
            payload: json!({"managed_transport": "omp_helm_channel"}),
            preview: None,
            producer_epoch: "epoch-1".into(),
            seq,
        }
    }

    #[test]
    fn an_assertion_restates_the_status_without_stating_a_phase() {
        let live = slot("s1", "running", 3);
        let asserted = chrono::DateTime::from_timestamp(1_758_120_000, 0).expect("ts");
        let event = assertion_runtime_event(&live, asserted);

        // Phase-less on purpose: the host applies a phase, and applying this one
        // again would bump its runtime revision and re-anchor the phase for a
        // statement that says nothing new about the provider.
        assert_eq!(event["kind"], "status_assertion");
        assert_eq!(event["phase"], Value::Null);
        // What the assertion is about, kept separate from when it was made.
        assert_eq!(event["payload"]["observed_at"], live.observed_at);
        assert_eq!(event["payload"]["asserted_at"], asserted.to_rfc3339());
        assert_eq!(event["occurred_at"], asserted.to_rfc3339());
        // Identity is the assertion itself, so a replay restates one the host
        // has already accepted instead of renewing anything.
        assert_eq!(
            event["dedupe_key"],
            format!("omp-assert:s1:run-1:{}", asserted.to_rfc3339())
        );
        let later = assertion_runtime_event(&live, asserted + chrono::Duration::seconds(5));
        assert_ne!(later["dedupe_key"], event["dedupe_key"]);
    }

    #[test]
    fn one_file_per_session_no_matter_how_often_it_is_written() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        for seq in 0..500 {
            publish(&dir, &slot("s1", "thinking", seq)).expect("publish");
        }
        publish(&dir, &slot("s2", "running", 0)).expect("publish");

        let ready = std::fs::read_dir(&dir)
            .expect("read_dir")
            .flatten()
            .filter(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .is_some_and(|name| name.ends_with(".json"))
            })
            .count();
        assert_eq!(ready, 2, "one slot per session, not one per write");

        let slots = read_all(&dir);
        assert_eq!(slots.len(), 2);
        let s1 = slots
            .iter()
            .find(|slot| slot.session_id == "s1")
            .expect("s1");
        assert_eq!(s1.seq, 499, "the slot holds the newest write");
    }

    #[test]
    fn a_slot_states_its_phase_and_its_preview() {
        let mut value = slot("s1", "running", 7);
        value.tool_name = Some("bash".into());
        value.preview = Some(StatusPreview {
            turn_id: "turn-9".into(),
            seq: 42,
            live_text: "partial answer".into(),
            turn_completed: false,
            progress_kind: "omp_helm_stream".into(),
            provider_session_id: Some("native-1".into()),
        });

        let events = runtime_events(&value);

        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["kind"], "phase_signal");
        assert_eq!(events[0]["phase"], "running");
        assert_eq!(events[0]["tool_name"], "bash");
        assert_eq!(events[1]["kind"], "progress_signal");
        assert_eq!(events[1]["payload"]["live_text"], "partial answer");
        assert_eq!(events[1]["payload"]["seq"], 42);
        // Cumulative text carries everything the deltas said, so no delta is
        // transported at all.
        assert!(events[1]["payload"].get("delta").is_none());
    }

    /// `finished` is local-health vocabulary, not a wire phase. The durable
    /// enqueue path refused it at the producer; the slot path has to refuse it
    /// in the projection, or the daemon posts a rejected event every tick
    /// until the slot is retired.
    #[test]
    fn a_local_health_phase_is_never_projected_onto_the_wire() {
        let mut finished = slot("s1", "finished", 1);
        finished.preview = Some(StatusPreview {
            turn_id: "turn-1".into(),
            seq: 1,
            live_text: "the answer".into(),
            turn_completed: true,
            progress_kind: "cursor_print_stream".into(),
            provider_session_id: None,
        });

        let events = runtime_events(&finished);

        assert!(
            events.iter().all(|event| event["kind"] != "phase_signal"),
            "a phase the host rejects is never sent"
        );
        assert_eq!(events.len(), 1, "the preview still ships");
        assert_eq!(events[0]["kind"], "progress_signal");
    }

    /// A producer with no run identity says so by omission. Shipping an empty
    /// string would bind activity to a run that does not exist; the Codex
    /// bridge is the live example, and its events carry no run id today.
    #[test]
    fn a_slot_without_a_run_omits_it_rather_than_sending_an_empty_one() {
        let mut runless = slot("s1", "running", 1);
        runless.run_id = String::new();

        let events = runtime_events(&runless);

        assert_eq!(events[0]["run_id"], Value::Null);
        assert_eq!(events[0]["session_id"], "s1");
    }

    #[test]
    fn a_slot_without_a_preview_states_only_its_phase() {
        let events = runtime_events(&slot("s1", "idle", 1));
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["kind"], "phase_signal");
    }

    /// Several short-lived writers, out of order on purpose: the slot must end
    /// up holding the newest observation, not whichever process finished last.
    #[test]
    fn an_older_observation_never_overwrites_a_newer_one() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());

        let mut newest = slot("s1", "running", 2);
        newest.observed_at = "2026-09-17T15:00:10Z".into();
        assert!(publish_if_newer(&dir, &newest).expect("publish"));

        let mut older = slot("s1", "idle", 3);
        older.observed_at = "2026-09-17T15:00:05Z".into();
        assert!(
            !publish_if_newer(&dir, &older).expect("publish"),
            "the older observation is refused"
        );
        assert_eq!(read_all(&dir).pop().expect("slot").phase, "running");

        let mut later = slot("s1", "idle", 4);
        later.observed_at = "2026-09-17T15:00:11Z".into();
        assert!(publish_if_newer(&dir, &later).expect("publish"));
        assert_eq!(read_all(&dir).pop().expect("slot").phase, "idle");
    }

    #[test]
    fn concurrent_hook_style_writers_leave_the_newest_observation() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());

        std::thread::scope(|scope| {
            for worker in 0..8 {
                let dir = dir.clone();
                scope.spawn(move || {
                    for step in 0..40 {
                        let mut value = slot("s1", "running", 1);
                        // Interleaved times across workers, so finishing order
                        // and observation order deliberately disagree.
                        value.observed_at =
                            format!("2026-09-17T15:00:{:02}Z", (step * 8 + worker) % 60);
                        let _ = publish_if_newer(&dir, &value);
                    }
                });
            }
        });

        let slots = read_all(&dir);
        assert_eq!(slots.len(), 1);
        assert_eq!(
            slots[0].observed_at, "2026-09-17T15:00:59Z",
            "the newest observation survived every race"
        );
    }

    #[test]
    fn a_retired_session_leaves_nothing_behind() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        publish(&dir, &slot("s1", "idle", 1)).expect("publish");
        retire(&dir, "s1");
        assert!(read_all(&dir).is_empty());
    }

    #[test]
    fn a_slot_from_an_unknown_schema_is_left_alone() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        std::fs::create_dir_all(&dir).expect("create");
        let mut future = serde_json::to_value(slot("s1", "idle", 1)).expect("value");
        future["schema"] = json!(STATUS_SLOT_SCHEMA + 1);
        std::fs::write(
            slot_path(&dir, "s1"),
            serde_json::to_vec(&future).expect("bytes"),
        )
        .expect("write");

        assert!(read_all(&dir).is_empty());
        assert!(slot_path(&dir, "s1").exists(), "and it is not deleted");
    }

    /// Every provider shares one publisher, so the contract is proven once:
    /// a transition publishes immediately, a restatement inside the window
    /// does not, and the wire events carry that provider's own identity.
    #[test]
    fn the_publisher_is_provider_generic() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        let publisher = StatusPublisher::new(dir.clone(), "pi", "pi_helm_channel");

        publisher.publish(StatusUpdate::phase("s1", "run-1", "2026-09-17T15:00:01Z", "running").with_tool(Some("bash")));
        let first = read_all(&dir).pop().expect("slot");
        assert_eq!(first.provider, "pi");
        assert_eq!(first.runtime_key, "pi:s1");
        assert_eq!(first.source, "pi_helm_channel");
        assert_eq!(first.phase, "running");

        // The same statement again, inside the coalesce window.
        publisher.publish(StatusUpdate::phase("s1", "run-1", "2026-09-17T15:00:02Z", "running").with_tool(Some("bash")));
        assert_eq!(read_all(&dir).pop().expect("slot").seq, first.seq);

        // A transition is never coalesced.
        publisher.publish(StatusUpdate::phase("s1", "run-1", "2026-09-17T15:00:03Z", "idle"));
        let idle = read_all(&dir).pop().expect("slot");
        assert_eq!(idle.phase, "idle");
        assert!(idle.seq > first.seq);

        let events = runtime_events(&idle);
        assert_eq!(events[0]["provider"], "pi");
        assert!(
            events[0]["dedupe_key"].as_str().expect("key").starts_with("pi-phase:"),
            "each provider's events carry its own identity"
        );

        publisher.retire("s1");
        assert!(read_all(&dir).is_empty());
        publisher.publish(StatusUpdate::phase("s1", "run-1", "2026-09-17T15:00:04Z", "running"));
        assert!(read_all(&dir).is_empty(), "a retired session states nothing further");
    }

    /// Codex carries `pause_request_still_pending` and stall evidence in its
    /// payload, and the Runtime Host takes a different branch on each. A
    /// payload change is therefore a transition, even when the phase name
    /// holds still: coalescing it away would drop the statement that resolves
    /// a pending question.
    #[test]
    fn a_payload_change_publishes_even_inside_the_coalesce_window() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        let publisher = StatusPublisher::new(dir.clone(), "codex", "codex_app_server");

        publisher.publish(
            StatusUpdate::phase("s1", "run-1", "2026-09-17T15:00:01Z", "running")
                .with_payload(json!({"pause_request_still_pending": true})),
        );
        let pending = read_all(&dir).pop().expect("slot");
        assert_eq!(pending.payload["pause_request_still_pending"], true);

        // Same phase, same tool, immediately after — but it now says the
        // question is no longer pending.
        publisher.publish(
            StatusUpdate::phase("s1", "run-1", "2026-09-17T15:00:01Z", "running")
                .with_payload(json!({"pause_request_still_pending": false})),
        );
        let resolved = read_all(&dir).pop().expect("slot");
        assert_eq!(resolved.payload["pause_request_still_pending"], false);
        assert!(resolved.seq > pending.seq, "the change was published, not coalesced");
    }

    #[test]
    fn abandoned_slots_are_swept_and_live_ones_are_not() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        publish(&dir, &slot("s1", "thinking", 1)).expect("publish");

        assert_eq!(sweep_abandoned(&dir, std::time::Duration::from_secs(3600)), 0);
        assert_eq!(read_all(&dir).len(), 1, "a fresh slot is current truth");

        // A launcher that crashed cannot retire its own slot, and nothing else
        // knows it is gone.
        assert_eq!(sweep_abandoned(&dir, std::time::Duration::ZERO), 1);
        assert!(read_all(&dir).is_empty());
    }

    /// Two threads publishing the same session raced through one shared temp
    /// path before this: each write now creates its own.
    #[test]
    fn concurrent_publishes_never_interleave_their_bytes() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        std::thread::scope(|scope| {
            for worker in 0..8 {
                let dir = dir.clone();
                scope.spawn(move || {
                    for seq in 0..200 {
                        let mut value = slot("s1", "thinking", worker * 1000 + seq);
                        value.preview = Some(StatusPreview {
                            turn_id: "turn-1".into(),
                            seq,
                            live_text: "y".repeat(2048),
                            turn_completed: false,
                            progress_kind: "omp_helm_stream".into(),
                            provider_session_id: None,
                        });
                        publish(&dir, &value).expect("publish");
                    }
                });
            }
        });

        let slots = read_all(&dir);
        assert_eq!(slots.len(), 1, "still one slot");
        assert_eq!(slots[0].preview.as_ref().expect("preview").live_text.len(), 2048);
        let leftovers = std::fs::read_dir(&dir)
            .expect("read_dir")
            .flatten()
            .filter(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .is_some_and(|name| name.ends_with(".tmp"))
            })
            .count();
        assert_eq!(leftovers, 0, "no temp file is left behind");
    }

    #[test]
    fn a_reader_never_sees_a_partial_value() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = status_slot_dir(tmp.path());
        let writer_dir = dir.clone();
        let writer = std::thread::spawn(move || {
            for seq in 0..2_000 {
                let mut value = slot("s1", "thinking", seq);
                value.preview = Some(StatusPreview {
                    turn_id: "turn-1".into(),
                    seq,
                    live_text: "x".repeat(4096),
                    turn_completed: false,
                    progress_kind: "omp_helm_stream".into(),
                    provider_session_id: None,
                });
                publish(&writer_dir, &value).expect("publish");
            }
        });
        for _ in 0..2_000 {
            for slot in read_all(&dir) {
                assert_eq!(slot.session_id, "s1");
                assert_eq!(slot.schema, STATUS_SLOT_SCHEMA);
            }
        }
        writer.join().expect("writer thread");
    }
}
