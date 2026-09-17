//! Outbox drain for presence events.
//!
//! Hook integrations write small JSON files to `~/.longhouse/agent/outbox/` instead of
//! calling the API directly. This eliminates network I/O from the hook hot path,
//! allowing hooks to run as `async: false` without risking stalls.
//!
//! The daemon drains the outbox on a short tick: reads all ready files,
//! coalesces by session_id (latest state wins), returns local phase signals for
//! transcript catch-up, persists managed transcript bindings, POSTs to
//! `/api/agents/presence`, and deletes files on success. Files are kept on
//! failure and retried next tick. Durable local writes belong here rather than
//! in the provider hook process, where SQLite contention can block the provider.
//! Ordinary presence files older than `STALE_SECS` are deleted without posting;
//! managed transcript-binding intents are retained until the daemon persists them.

use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use chrono::{DateTime, Utc};
use futures_util::stream::{self, StreamExt};
use serde::Deserialize;
use serde_json::Value;
use tracing::warn;

use crate::shipping::client::{JsonPostError, ShipperClient};
use crate::state::session_phase::{PhaseSource, SessionPhaseSignal, SessionPhaseStore};
use crate::state::unmanaged_process_binding::{
    UnmanagedProcessBindingSignal, UnmanagedProcessBindingStore,
};

/// Maximum age for an outbox file before it is considered stale and deleted.
const STALE_SECS: u64 = 600; // 10 minutes
const PRESENCE_POST_TIMEOUT: Duration = Duration::from_secs(3);
const PRESENCE_POST_CONCURRENCY: usize = 8;
const RUNTIME_EVENT_POST_TIMEOUT: Duration = Duration::from_secs(20);
/// Matches RuntimeEventBatchIngest in server/zerg/services/session_runtime.py.
/// The route applies a 1024-event request in ordered 128-event catalogd
/// chunks, each with a two-second queue budget. Three seconds allowed the
/// server to commit successfully after the client had already retried the
/// same durable files, so keep the HTTP deadline above the worst-case
/// eight-apply path.
/// One POST is one catalogd apply. A larger request was split server-side into
/// up to eight serial applies, so the client could time out after the server
/// had already committed and then re-send the same durable files forever.
/// Fewer round trips stopped being the lever once replaceable status is
/// coalesced at collection: a healthy pass carries a handful of events.
const RUNTIME_EVENT_BATCH_LIMIT: usize = 128;
const RUNTIME_EVENT_DEAD_LETTER_DIR: &str = "dead-letter";
/// Upper bound on files inspected in one collection pass. Collection holds
/// every inspected event in memory, so an unbounded directory is an unbounded
/// allocation on a 100ms tick. Anything left over is collected next pass.
const RUNTIME_EVENT_COLLECT_LIMIT: usize = 8_192;
/// Above this many ready files the directory is moved aside and reduced in the
/// background instead of being drained in place. Enumerating it costs more than
/// the current truth it contains is worth.
pub const RUNTIME_EVENT_RECOVERY_THRESHOLD: usize = 5_000;

#[derive(Debug, Clone, Deserialize)]
struct PresenceOutboxPayload {
    session_id: String,
    state: String,
    #[serde(default)]
    tool_name: Option<String>,
    #[serde(default)]
    cwd: Option<String>,
    #[serde(default)]
    transcript_path: Option<String>,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    control_path: Option<String>,
    #[serde(default)]
    provider_pid: Option<u32>,
    #[serde(default)]
    occurred_at: Option<String>,
    /// Provider adapters use this for local-health-only phase evidence. It is
    /// persisted by the daemon but never POSTed as machine presence.
    #[serde(default)]
    local_only: bool,
    #[serde(default)]
    phase_source: Option<String>,
}

#[derive(Debug)]
struct PendingPresenceFile {
    path: PathBuf,
    bytes: Vec<u8>,
    payload: PresenceOutboxPayload,
    observed_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DrainedPresenceSignal {
    pub session_id: String,
    pub provider: String,
    pub phase: String,
    pub observed_at: DateTime<Utc>,
    pub transcript_path: Option<PathBuf>,
}

#[derive(Debug, Default)]
#[cfg_attr(not(test), allow(dead_code))]
pub struct OutboxDrainResult {
    pub sent: usize,
    pub kept: usize,
    pub signals: Vec<DrainedPresenceSignal>,
}

#[derive(Debug, Default)]
pub struct OutboxLocalDrainResult {
    pub signals: Vec<DrainedPresenceSignal>,
    pub posts: Vec<PendingPresencePost>,
}

#[derive(Debug)]
pub struct PendingPresencePost {
    path: PathBuf,
    bytes: Vec<u8>,
}

#[derive(Debug)]
pub struct PendingRuntimeEventPost {
    path: PathBuf,
    event: Value,
}

/// Is this event's phase one an adapter is allowed to ship?
///
/// Only `phase_signal` is constrained. A terminal or binding signal carries its
/// meaning in the payload and legitimately ships `finished`, which the contract
/// marks local-health-only.
fn runtime_event_phase_is_shippable(event: &Value) -> bool {
    if event.get("kind").and_then(Value::as_str) != Some("phase_signal") {
        return true;
    }
    match event.get("phase").and_then(Value::as_str) {
        None => true,
        Some(phase) => crate::managed_phase_contract::is_wire_phase(phase),
    }
}

/// Durably enqueue one runtime event for the daemon's shared retrying outbox.
/// Writers never POST directly: an atomic rename makes an event visible to the
/// drain loop only after its complete JSON payload reaches disk.
pub fn enqueue_runtime_event(dir: &Path, event: &Value) -> anyhow::Result<()> {
    // The spill path drops too, or a fault-injected terminal would simply take
    // the durable route and arrive anyway.
    if crate::fault_injection::should_drop_runtime_event(event) {
        return Ok(());
    }
    // Ingest rejects a phase_signal outside the contract, which dead-letters it.
    // Refuse at the producer instead, so the bug surfaces as a local error rather
    // than a 422 in production. This is deliberately an error and not a
    // debug_assert: the engine ships and tests in release, where debug assertions
    // are compiled out and the guard would never fire.
    if !runtime_event_phase_is_shippable(event) {
        anyhow::bail!("phase_signal carries a phase outside the managed phase contract: {event}");
    }
    std::fs::create_dir_all(dir)?;
    let bytes = serde_json::to_vec(event)?;
    let nonce = uuid::Uuid::new_v4();
    let temporary = dir.join(format!(".{nonce}.tmp"));
    let ready = dir.join(format!("{nonce}.json"));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(&temporary, &ready)?;
    sync_directory(dir)?;
    Ok(())
}

/// Drain all ready presence events from the outbox directory.
///
/// Returns `(sent, kept)`:
/// - `sent`: number of events successfully POSTed (files deleted)
/// - `kept`: number of files kept for retry (POST failed)
#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_outbox(dir: &Path, client: &ShipperClient) -> (usize, usize) {
    let result = drain_outbox_impl(dir, client, None, false).await;
    (result.sent, result.kept)
}

/// Same as `drain_outbox`, but also mirrors the latest coalesced phase into
/// the local agent DB so local-health can render accurate per-session phase.
#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_outbox_with_local_state(
    dir: &Path,
    client: &ShipperClient,
    db_path: Option<&Path>,
) -> (usize, usize) {
    let result = drain_outbox_impl(dir, client, db_path, true).await;
    (result.sent, result.kept)
}

/// Drain outbox files and return locally valid phase signals so the daemon can
/// schedule transcript catch-up work for the same sessions. Signals are returned
/// even when the presence POST fails because transcript shipping is local truth
/// and should not depend on the runtime accepting a presence update first.
#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_outbox_with_local_state_result(
    dir: &Path,
    client: &ShipperClient,
    db_path: Option<&Path>,
) -> OutboxDrainResult {
    drain_outbox_impl(dir, client, db_path, true).await
}

async fn drain_outbox_impl(
    dir: &Path,
    client: &ShipperClient,
    db_path: Option<&Path>,
    persist_local_state: bool,
) -> OutboxDrainResult {
    let local = collect_outbox_impl(dir, db_path, persist_local_state);
    let (sent, kept) = post_pending_presence_files(client, local.posts).await;
    OutboxDrainResult {
        sent,
        kept,
        signals: local.signals,
    }
}

pub fn collect_outbox_with_local_state_result(
    dir: &Path,
    db_path: Option<&Path>,
) -> OutboxLocalDrainResult {
    collect_outbox_impl(dir, db_path, true)
}

fn collect_outbox_impl(
    dir: &Path,
    db_path: Option<&Path>,
    persist_local_state: bool,
) -> OutboxLocalDrainResult {
    // Nothing to do if outbox doesn't exist yet.
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return OutboxLocalDrainResult::default(),
    };

    let now = SystemTime::now();
    // session_id → latest ordinary presence observation — newest state wins
    let mut by_session: HashMap<String, PendingPresenceFile> = HashMap::new();
    // Local phase observations are durable evidence for the engine's own
    // health projection. Keep them separate: they must reach SQLite but never
    // become hosted presence traffic.
    let mut local_phase_by_session: HashMap<String, PendingPresenceFile> = HashMap::new();
    // Binding intents are durable until the daemon has persisted them. Do not
    // let presence coalescing discard an older managed observation whose
    // transcript path is the only durable identity we have.
    let mut managed_binding_paths = HashSet::new();
    let mut managed_binding_payloads = Vec::new();

    for entry in entries.flatten() {
        let path = entry.path();

        // Skip non-JSON and in-progress tmp files (start with '.')
        let file_name = match path.file_name().and_then(|n| n.to_str()) {
            Some(n) => n.to_owned(),
            None => continue,
        };
        if !file_name.ends_with(".json") || file_name.starts_with('.') {
            // Prune stale dot-files (orphaned atomic-write temps) — they will never
            // be renamed to prs.*.json and would otherwise accumulate forever.
            if file_name.starts_with('.') {
                if let Ok(meta) = entry.metadata() {
                    if let Ok(modified) = meta.modified() {
                        if let Ok(age) = now.duration_since(modified) {
                            if age > Duration::from_secs(STALE_SECS) {
                                let _ = std::fs::remove_file(&path);
                            }
                        }
                    }
                }
            }
            continue;
        }

        // Read and validate JSON.
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue, // file disappeared between read_dir and read
        };
        let payload: PresenceOutboxPayload = match serde_json::from_slice(&bytes) {
            Ok(v) => v,
            Err(_) => {
                // Malformed JSON — delete to avoid indefinite retry.
                let _ = std::fs::remove_file(&path);
                continue;
            }
        };

        // Must have a non-empty session_id.
        let sid = payload.session_id.trim().to_string();
        if sid.is_empty() {
            let _ = std::fs::remove_file(&path);
            continue;
        }
        let state = payload.state.trim();
        if state.is_empty() {
            let _ = std::fs::remove_file(&path);
            continue;
        }

        let managed_binding_required = is_managed_binding_payload(&payload);
        // Presence is ephemeral, but a managed transcript binding is a
        // durable identity claim. Keep that intent through daemon outages so
        // the first healthy collector can persist it before posting/deleting.
        if let Ok(meta) = entry.metadata() {
            if let Ok(modified) = meta.modified() {
                if let Ok(age) = now.duration_since(modified) {
                    if age > Duration::from_secs(STALE_SECS) && !managed_binding_required {
                        let _ = std::fs::remove_file(&path);
                        continue;
                    }
                }
            }
        }

        if managed_binding_required {
            managed_binding_paths.insert(path.clone());
            managed_binding_payloads.push((path.clone(), payload.clone()));
        }

        let observed_at = observed_at_for_payload(&payload, &entry, now);
        let next_file = PendingPresenceFile {
            path: path.clone(),
            bytes,
            payload,
            observed_at,
        };

        if next_file.payload.local_only {
            match local_phase_by_session.get(&sid) {
                Some(existing) => {
                    if next_file.observed_at > existing.observed_at {
                        let _ = std::fs::remove_file(&existing.path);
                        local_phase_by_session.insert(sid, next_file);
                    } else {
                        let _ = std::fs::remove_file(&path);
                    }
                }
                None => {
                    local_phase_by_session.insert(sid, next_file);
                }
            }
            continue;
        }

        match by_session.get(&sid) {
            Some(existing) => {
                if next_file.observed_at > existing.observed_at {
                    if !managed_binding_paths.contains(&existing.path) {
                        let _ = std::fs::remove_file(&existing.path);
                    }
                    by_session.insert(sid, next_file);
                } else {
                    if !managed_binding_paths.contains(&path) {
                        let _ = std::fs::remove_file(&path);
                    }
                }
            }
            None => {
                by_session.insert(sid, next_file);
            }
        }
    }

    let mut result = OutboxLocalDrainResult::default();
    let local_phase_conn =
        if persist_local_state && (!by_session.is_empty() || !local_phase_by_session.is_empty()) {
            match crate::state::db::resolve_db_path(db_path)
                .and_then(|path| crate::state::db::open_connection(&path))
            {
                Ok(conn) => Some(conn),
                Err(err) => {
                    warn!("opening local session phase DB failed: {err}");
                    None
                }
            }
        } else {
            None
        };

    let mut persisted_binding_paths = HashSet::new();
    if let Some(conn) = local_phase_conn.as_ref() {
        for (path, payload) in &managed_binding_payloads {
            let provider = normalize_provider(payload.provider.as_deref());
            let session_id = payload.session_id.trim();
            if persist_managed_binding_for_payload(payload, provider, session_id, conn) {
                persisted_binding_paths.insert(path.clone());
            }
        }
    }
    let mut persisted_local_phase_paths = HashSet::new();
    if let Some(conn) = local_phase_conn.as_ref() {
        for pending in local_phase_by_session.values() {
            let payload = &pending.payload;
            let provider = normalize_provider(payload.provider.as_deref());
            let source = payload
                .phase_source
                .as_deref()
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| PhaseSource::for_hook_provider(provider).as_str());
            let signal = SessionPhaseSignal {
                session_id: payload.session_id.trim().to_string(),
                provider: provider.to_string(),
                phase: payload.state.trim().to_string(),
                tool_name: payload.tool_name.clone(),
                source: source.to_string(),
                observed_at: pending.observed_at,
            };
            match SessionPhaseStore::new(conn).record(&signal) {
                Ok(_) => {
                    persisted_local_phase_paths.insert(pending.path.clone());
                }
                Err(err) => {
                    warn!(
                        "persisting local-only phase failed for session {}: {err}",
                        signal.session_id
                    );
                }
            }
        }
    }
    for path in persisted_local_phase_paths {
        let _ = std::fs::remove_file(path);
    }

    let selected_paths: HashSet<PathBuf> = by_session
        .values()
        .map(|pending| pending.path.clone())
        .collect();
    for path in persisted_binding_paths.difference(&selected_paths) {
        let _ = std::fs::remove_file(path);
    }

    for pending in by_session.into_values() {
        let PendingPresenceFile {
            path,
            bytes,
            payload,
            observed_at,
        } = pending;
        let provider = normalize_provider(payload.provider.as_deref()).to_string();
        let session_id = payload.session_id.trim().to_string();
        let phase = payload.state.trim().to_string();
        let transcript_path = normalize_transcript_path(payload.transcript_path.as_deref());

        let managed_binding_required = is_managed_binding_payload(&payload);
        let binding_persisted = persisted_binding_paths.contains(&path);
        if managed_binding_required && !binding_persisted {
            warn!(
                session_id,
                provider, "deferring managed transcript binding until local state DB is available"
            );
            continue;
        }

        result.signals.push(DrainedPresenceSignal {
            session_id: session_id.clone(),
            provider: provider.clone(),
            phase: phase.clone(),
            observed_at: observed_at.clone(),
            transcript_path,
        });
        result.posts.push(PendingPresencePost { path, bytes });

        if let Some(conn) = local_phase_conn.as_ref() {
            let signal = SessionPhaseSignal {
                session_id: session_id.clone(),
                provider: provider.clone(),
                phase: phase.clone(),
                tool_name: payload.tool_name.clone(),
                source: PhaseSource::for_hook_provider(&provider)
                    .as_str()
                    .to_string(),
                observed_at,
            };
            if let Err(err) = SessionPhaseStore::new(conn).record(&signal) {
                warn!(
                    "persisting local phase failed for session {}: {err}",
                    signal.session_id
                );
            }
            if let Some(binding_signal) =
                unmanaged_binding_signal_for_payload(&payload, &provider, &session_id, observed_at)
            {
                if let Err(err) = UnmanagedProcessBindingStore::new(conn).record(&binding_signal) {
                    warn!(
                        "persisting unmanaged process binding failed for session {}: {err}",
                        binding_signal.provider_session_id
                    );
                }
            }
        }
    }

    result
}

pub async fn post_pending_presence_files(
    client: &ShipperClient,
    posts: Vec<PendingPresencePost>,
) -> (usize, usize) {
    post_pending_presence_files_with_timeout(client, posts, PRESENCE_POST_TIMEOUT).await
}

/// Replaceable status coordinate for one runtime event.
///
/// A phase or progress signal is an observation of the session's *current*
/// state: the newest one subsumes every earlier one, exactly as the catalog's
/// latest-wins reducer treats them on arrival. Everything else — terminal,
/// binding, interaction — carries meaning that no later event can restate, so
/// it is never merged.
fn replaceable_status_key(event: &Value) -> Option<(String, String, String)> {
    let kind = event.get("kind").and_then(Value::as_str)?;
    let session = event
        .get("session_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let run = event
        .get("run_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    match kind {
        "phase_signal" => Some((session, run, "phase".to_string())),
        "progress_signal" => {
            // Preview text is cumulative (`live_text`), so only the newest
            // sequence in a turn is needed to render it.
            let turn = event
                .get("payload")
                .and_then(|payload| payload.get("turn_id"))
                .and_then(Value::as_str)
                .unwrap_or_default();
            Some((session, run, format!("progress:{turn}")))
        }
        _ => None,
    }
}

/// Order within one replaceable coordinate: explicit sequence first, then the
/// producer's observation time.
fn replaceable_order(event: &Value) -> (u64, String) {
    let seq = event
        .get("payload")
        .and_then(|payload| payload.get("seq"))
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let occurred_at = event
        .get("occurred_at")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    (seq, occurred_at)
}

fn occurred_at_sort_key(event: &Value) -> String {
    event
        .get("occurred_at")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

pub fn collect_runtime_event_outbox(dir: &Path) -> Vec<PendingRuntimeEventPost> {
    collect_runtime_event_outbox_bounded(dir, RUNTIME_EVENT_COLLECT_LIMIT).posts
}

/// One collection pass: what to post now, and whether the directory still held
/// more than one pass could inspect.
pub struct RuntimeEventCollection {
    pub posts: Vec<PendingRuntimeEventPost>,
    pub saturated: bool,
}

pub fn collect_runtime_event_outbox_pass(dir: &Path) -> RuntimeEventCollection {
    collect_runtime_event_outbox_bounded(dir, RUNTIME_EVENT_COLLECT_LIMIT)
}

/// Drain ready runtime events, keeping only the newest observation per
/// replaceable coordinate and every durable record.
///
/// Superseded files are deleted here rather than posted. Sending an
/// already-overwritten phase cannot improve the served truth, and during the
/// 2026-09-17 reconnect flood it was the entire reason current truth never
/// arrived: 250k queued observations at ~66 events/s is hours of transmission
/// for state the next tick replaces.
fn collect_runtime_event_outbox_bounded(dir: &Path, limit: usize) -> RuntimeEventCollection {
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => {
            return RuntimeEventCollection {
                posts: Vec::new(),
                saturated: false,
            }
        }
    };

    let now = SystemTime::now();
    let mut durable: Vec<PendingRuntimeEventPost> = Vec::new();
    let mut replaceable: HashMap<(String, String, String), (( u64, String), PendingRuntimeEventPost)> =
        HashMap::new();
    let mut inspected = 0usize;
    let mut saturated = false;

    for entry in entries.flatten() {
        if inspected >= limit {
            saturated = true;
            break;
        }
        let path = entry.path();
        let file_name = match path.file_name().and_then(|n| n.to_str()) {
            Some(n) => n.to_owned(),
            None => continue,
        };
        if !file_name.ends_with(".json") || file_name.starts_with('.') {
            // The dead-letter directory lives here too. Pruning targets stale
            // temp files, and remove_file on a directory merely fails, but say
            // so rather than relying on that.
            if !entry.file_type().map(|kind| kind.is_dir()).unwrap_or(false) {
                prune_stale_dot_file(&entry, &path, now);
            }
            continue;
        }
        inspected += 1;

        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let event: Value = match serde_json::from_slice::<Value>(&bytes) {
            Ok(value) if value.is_object() => value,
            Ok(_) | Err(_) => {
                let _ = std::fs::remove_file(&path);
                continue;
            }
        };

        let Some(key) = replaceable_status_key(&event) else {
            durable.push(PendingRuntimeEventPost { path, event });
            continue;
        };
        let order = replaceable_order(&event);
        let candidate = PendingRuntimeEventPost { path, event };
        match replaceable.get(&key) {
            Some((existing_order, existing)) if *existing_order >= order => {
                let _ = std::fs::remove_file(&candidate.path);
            }
            Some((_, existing)) => {
                let _ = std::fs::remove_file(&existing.path);
                replaceable.insert(key, (order, candidate));
            }
            None => {
                replaceable.insert(key, (order, candidate));
            }
        }
    }

    let mut posts = durable;
    posts.extend(replaceable.into_values().map(|(_, post)| post));
    // A session's own order must hold across the batch boundary, and file
    // enumeration is uuid order, which is no order at all.
    posts.sort_by(|left, right| {
        occurred_at_sort_key(&left.event).cmp(&occurred_at_sort_key(&right.event))
    });
    RuntimeEventCollection { posts, saturated }
}

/// Outcome of reducing a moved-aside runtime outbox.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct RuntimeOutboxRecovery {
    pub inspected: usize,
    pub kept: usize,
    pub discarded: usize,
}

/// Does this directory hold more ready files than `threshold`?
///
/// Stops as soon as the answer is known: the whole point is to decide without
/// paying for a full enumeration of a directory that may hold 250k entries.
pub fn runtime_event_outbox_exceeds(dir: &Path, threshold: usize) -> bool {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return false;
    };
    let mut seen = 0usize;
    for entry in entries.flatten() {
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if !name.ends_with(".json") || name.starts_with('.') {
            continue;
        }
        seen += 1;
        if seen > threshold {
            return true;
        }
    }
    false
}

/// Move a flooded runtime outbox aside so live producers get an empty
/// directory immediately, then reduce the aside copy to current truth.
///
/// Draining a flood in place cannot restore liveness: every pass pays for the
/// whole directory before the newest observation can be posted. Renaming is
/// atomic and producers call `create_dir_all` on every enqueue, so the live
/// path is restored in one syscall while recovery runs in the background.
pub fn recover_runtime_event_outbox(dir: &Path, threshold: usize) -> Option<RuntimeOutboxRecovery> {
    if !runtime_event_outbox_exceeds(dir, threshold) {
        return None;
    }
    let file_name = dir.file_name()?.to_str()?.to_owned();
    let stamp = Utc::now().format("%Y%m%dT%H%M%S%3fZ");
    let aside = dir.with_file_name(format!("{file_name}.recovering-{stamp}"));
    if let Err(err) = std::fs::rename(dir, &aside) {
        warn!("moving flooded runtime outbox aside failed: {err}");
        return None;
    }
    if let Err(err) = std::fs::create_dir_all(dir) {
        warn!("recreating runtime outbox failed: {err}");
    }
    // Dead-lettered evidence is retained, not reduced.
    let aside_dead_letter = aside.join(RUNTIME_EVENT_DEAD_LETTER_DIR);
    if aside_dead_letter.is_dir() {
        let _ = std::fs::rename(&aside_dead_letter, dir.join(RUNTIME_EVENT_DEAD_LETTER_DIR));
    }
    Some(reduce_runtime_event_outbox_dir(&aside, dir))
}

/// Reduce an aside directory into `live_dir`: every durable record and the
/// newest observation per replaceable coordinate are moved back, everything
/// else is deleted, and the directory is removed.
fn reduce_runtime_event_outbox_dir(aside: &Path, live_dir: &Path) -> RuntimeOutboxRecovery {
    let mut stats = RuntimeOutboxRecovery::default();
    let Ok(entries) = std::fs::read_dir(aside) else {
        return stats;
    };
    // Bounded by replaceable coordinates (sessions x turns), not by file count.
    let mut newest: HashMap<(String, String, String), ((u64, String), PathBuf)> = HashMap::new();
    let mut durable_seen: HashSet<String> = HashSet::new();

    for entry in entries.flatten() {
        let path = entry.path();
        let Some(name) = path.file_name().and_then(|n| n.to_str()).map(str::to_owned) else {
            continue;
        };
        if !name.ends_with(".json") || name.starts_with('.') {
            continue;
        }
        stats.inspected += 1;
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        let event: Value = match serde_json::from_slice::<Value>(&bytes) {
            Ok(value) if value.is_object() => value,
            Ok(_) | Err(_) => {
                let _ = std::fs::remove_file(&path);
                stats.discarded += 1;
                continue;
            }
        };
        let Some(key) = replaceable_status_key(&event) else {
            // A durable record repeated under one stable dedupe key is one
            // record; the server applies it idempotently either way.
            let dedupe = event
                .get("dedupe_key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            if !dedupe.is_empty() && !durable_seen.insert(dedupe) {
                let _ = std::fs::remove_file(&path);
                stats.discarded += 1;
                continue;
            }
            if move_into(&path, live_dir, &name) {
                stats.kept += 1;
            }
            continue;
        };
        let order = replaceable_order(&event);
        match newest.get(&key) {
            Some((existing_order, existing_path)) if *existing_order >= order => {
                let _ = std::fs::remove_file(&path);
                stats.discarded += 1;
                let _ = existing_path;
            }
            Some((_, existing_path)) => {
                let _ = std::fs::remove_file(existing_path);
                stats.discarded += 1;
                newest.insert(key, (order, path));
            }
            None => {
                newest.insert(key, (order, path));
            }
        }
    }

    for (_, (_, path)) in newest {
        let Some(name) = path.file_name().and_then(|n| n.to_str()).map(str::to_owned) else {
            continue;
        };
        if move_into(&path, live_dir, &name) {
            stats.kept += 1;
        }
    }
    let _ = std::fs::remove_dir_all(aside);
    stats
}

fn move_into(path: &Path, live_dir: &Path, name: &str) -> bool {
    match std::fs::rename(path, live_dir.join(name)) {
        Ok(()) => true,
        Err(err) => {
            warn!("returning runtime event to the live outbox failed: {err}");
            false
        }
    }
}

pub async fn post_pending_runtime_event_files(
    client: &ShipperClient,
    posts: Vec<PendingRuntimeEventPost>,
) -> (usize, usize) {
    let mut sent = 0usize;
    let mut kept = 0usize;
    for chunk in posts.chunks(RUNTIME_EVENT_BATCH_LIMIT) {
        let events: Vec<Value> = chunk.iter().map(|post| post.event.clone()).collect();
        let body = match serde_json::to_vec(&serde_json::json!({ "events": events })) {
            Ok(value) => value,
            Err(_) => {
                kept += chunk.len();
                continue;
            }
        };
        match client
            .post_json_with_timeout_classified(
                "/api/agents/runtime/events/batch",
                body,
                Some(RUNTIME_EVENT_POST_TIMEOUT),
            )
            .await
        {
            Ok(_) => {
                for post in chunk {
                    let _ = std::fs::remove_file(&post.path);
                }
                sent += chunk.len();
            }
            Err(error) if error.permanent_status_code().is_some() => {
                let (chunk_sent, chunk_kept) =
                    isolate_permanent_runtime_event_rejection(client, chunk).await;
                sent += chunk_sent;
                kept += chunk_kept;
            }
            Err(error) => {
                tracing::warn!(error = %error, event_count = chunk.len(), "Runtime event batch kept for retry");
                kept += chunk.len();
            }
        }
    }
    (sent, kept)
}

async fn isolate_permanent_runtime_event_rejection(
    client: &ShipperClient,
    chunk: &[PendingRuntimeEventPost],
) -> (usize, usize) {
    let mut sent = 0usize;
    let mut kept = 0usize;
    for post in chunk {
        let body = match serde_json::to_vec(&serde_json::json!({
            "events": [post.event.clone()],
        })) {
            Ok(body) => body,
            Err(error) => {
                tracing::warn!(
                    path = %post.path.display(),
                    error = %error,
                    "Runtime event could not be serialized for rejection isolation"
                );
                kept += 1;
                continue;
            }
        };

        match client
            .post_json_with_timeout_classified(
                "/api/agents/runtime/events/batch",
                body,
                Some(RUNTIME_EVENT_POST_TIMEOUT),
            )
            .await
        {
            Ok(()) => {
                let _ = std::fs::remove_file(&post.path);
                sent += 1;
            }
            Err(error) if error.permanent_status_code().is_some() => {
                let status = error
                    .permanent_status_code()
                    .expect("permanent JSON POST errors have a status code");
                let response_body = error.response_body().unwrap_or_default();
                match dead_letter_runtime_event(post, status, response_body, &error) {
                    Ok(path) => {
                        tracing::error!(
                            source = %post.path.display(),
                            dead_letter = %path.display(),
                            status,
                            "Runtime event permanently rejected and dead-lettered"
                        );
                    }
                    Err(dead_letter_error) => {
                        tracing::warn!(
                            source = %post.path.display(),
                            error = %dead_letter_error,
                            "Runtime event rejection could not be dead-lettered; keeping for retry"
                        );
                        kept += 1;
                    }
                }
            }
            Err(error) => {
                tracing::warn!(
                    path = %post.path.display(),
                    error = %error,
                    "Runtime event rejection isolation hit a transient failure"
                );
                kept += 1;
            }
        }
    }
    (sent, kept)
}

fn dead_letter_runtime_event(
    post: &PendingRuntimeEventPost,
    status: u16,
    response_body: &str,
    error: &JsonPostError,
) -> anyhow::Result<PathBuf> {
    let parent = post
        .path
        .parent()
        .ok_or_else(|| anyhow::anyhow!("runtime event path has no parent"))?;
    let dead_letter_dir = parent.join(RUNTIME_EVENT_DEAD_LETTER_DIR);
    std::fs::create_dir_all(&dead_letter_dir)?;
    let nonce = uuid::Uuid::new_v4();
    let temporary = dead_letter_dir.join(format!(".{nonce}.tmp"));
    let ready = dead_letter_dir.join(format!("{nonce}.json"));
    let evidence = serde_json::json!({
        "schema": "runtime_event_dead_letter.v1",
        "dead_lettered_at": Utc::now().to_rfc3339(),
        "source_file": post.path,
        "status_code": status,
        "error": error.to_string(),
        "response_body": response_body,
        "event": post.event,
    });
    let bytes = serde_json::to_vec_pretty(&evidence)?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)?;
    file.write_all(&bytes)?;
    file.sync_all()?;
    drop(file);
    std::fs::rename(&temporary, &ready)?;
    sync_directory(&dead_letter_dir)?;
    std::fs::remove_file(&post.path)?;
    sync_directory(parent)?;
    Ok(ready)
}

fn sync_directory(dir: &Path) -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        OpenOptions::new().read(true).open(dir)?.sync_all()?;
    }
    Ok(())
}

#[cfg_attr(not(test), allow(dead_code))]
pub async fn drain_runtime_event_outbox(dir: &Path, client: &ShipperClient) -> (usize, usize) {
    let posts = collect_runtime_event_outbox(dir);
    post_pending_runtime_event_files(client, posts).await
}

async fn post_pending_presence_files_with_timeout(
    client: &ShipperClient,
    posts: Vec<PendingPresencePost>,
    request_timeout: Duration,
) -> (usize, usize) {
    // A disconnected Runtime Host used to turn N queued signals into N * 3s
    // of serial timeout. During that window the 100ms collector kept rescanning
    // and reparsing the same files. Presence is independent per session, so a
    // small fixed fan-out bounds recovery latency without creating an
    // unbounded request burst.
    let outcomes = stream::iter(posts.into_iter().map(|post| async move {
        match client
            .post_json_with_timeout("/api/agents/presence", post.bytes, Some(request_timeout))
            .await
        {
            Ok(_) => {
                let _ = std::fs::remove_file(&post.path);
                true
            }
            Err(_) => false,
        }
    }))
    .buffer_unordered(PRESENCE_POST_CONCURRENCY)
    .collect::<Vec<_>>()
    .await;
    let sent = outcomes.iter().filter(|sent| **sent).count();
    let kept = outcomes.len().saturating_sub(sent);
    (sent, kept)
}

fn observed_at_for_payload(
    payload: &PresenceOutboxPayload,
    entry: &std::fs::DirEntry,
    now: SystemTime,
) -> DateTime<Utc> {
    if let Some(parsed) = payload.occurred_at.as_deref().and_then(parse_rfc3339_utc) {
        return parsed;
    }

    if let Ok(metadata) = entry.metadata() {
        if let Ok(modified) = metadata.modified() {
            return DateTime::<Utc>::from(modified);
        }
    }

    DateTime::<Utc>::from(now)
}

fn prune_stale_dot_file(entry: &std::fs::DirEntry, path: &Path, now: SystemTime) {
    if let Ok(meta) = entry.metadata() {
        if let Ok(modified) = meta.modified() {
            if let Ok(age) = now.duration_since(modified) {
                if age > Duration::from_secs(STALE_SECS) {
                    let _ = std::fs::remove_file(path);
                }
            }
        }
    }
}

fn parse_rfc3339_utc(raw: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(raw)
        .ok()
        .map(|value| value.with_timezone(&Utc))
}

fn normalize_provider(provider: Option<&str>) -> &str {
    provider
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("claude")
}

fn normalize_transcript_path(path: Option<&str>) -> Option<PathBuf> {
    path.map(str::trim)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn is_managed_binding_payload(payload: &PresenceOutboxPayload) -> bool {
    payload.control_path.as_deref().map(str::trim) == Some("managed")
        && normalize_transcript_path(payload.transcript_path.as_deref()).is_some()
}

fn unmanaged_binding_signal_for_payload(
    payload: &PresenceOutboxPayload,
    provider: &str,
    session_id: &str,
    observed_at: DateTime<Utc>,
) -> Option<UnmanagedProcessBindingSignal> {
    if payload.control_path.as_deref().map(str::trim) != Some("unmanaged") {
        return None;
    }
    let pid = payload.provider_pid?;
    let process = crate::unmanaged_bindings::process_info_for_pid(pid, provider)?;
    let source_path = normalize_transcript_path(payload.transcript_path.as_deref());

    Some(UnmanagedProcessBindingSignal {
        provider: provider.to_string(),
        provider_session_id: session_id.to_string(),
        source_path,
        pid,
        process_start_time: process.start_time,
        process_start_time_key: process.start_time_key,
        cwd: payload.cwd.clone(),
        observed_at,
    })
}

/// Persist the managed transcript identity after the hook has handed the event
/// to the daemon. This is deliberately best-effort: the outbox file remains
/// available for the next daemon tick if SQLite is contended, and the provider
/// hook is never made to wait for this write.
fn persist_managed_binding_for_payload(
    payload: &PresenceOutboxPayload,
    provider: &str,
    session_id: &str,
    conn: &rusqlite::Connection,
) -> bool {
    if payload.control_path.as_deref().map(str::trim) != Some("managed") {
        return true;
    }
    let Some(transcript_path) = normalize_transcript_path(payload.transcript_path.as_deref())
    else {
        return true;
    };
    let transcript_path = crate::discovery::canonical_transcript_hint(provider, &transcript_path);
    let canonical = crate::storage_v2_shipper::stable_source_path(&transcript_path);
    if let Err(err) = crate::state::session_binding::SessionBinding::new(conn).bind(
        &canonical.to_string_lossy(),
        session_id,
        provider,
    ) {
        warn!(
            path = %canonical.display(),
            session_id,
            provider,
            error = %err,
            "persisting managed transcript binding failed"
        );
        return false;
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;
    use tempfile::TempDir;

    fn make_outbox() -> TempDir {
        tempfile::tempdir().expect("tempdir")
    }

    #[test]
    fn runtime_event_post_deadline_covers_ordered_server_apply_budget() {
        // The server accepts 1024 observations but applies them as eight
        // ordered 128-event catalogd calls, each with a two-second queue
        // budget. A shorter client deadline causes successful server commits
        // to be replayed from the durable outbox.
        assert!(RUNTIME_EVENT_POST_TIMEOUT >= Duration::from_secs(16));
    }

    #[test]
    fn enqueue_runtime_event_is_atomically_visible_to_the_shared_drain() {
        let dir = make_outbox();
        let event = serde_json::json!({"session_id":"cursor-session","kind":"progress_signal"});

        enqueue_runtime_event(dir.path(), &event).unwrap();

        let posts = collect_runtime_event_outbox(dir.path());
        assert_eq!(posts.len(), 1);
        assert_eq!(posts[0].event, event);
        assert!(fs::read_dir(dir.path()).unwrap().all(|entry| !entry
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with('.')));
    }

    fn write_presence(dir: &Path, name: &str, session_id: &str, state: &str) -> PathBuf {
        let path = dir.join(name);
        let json = serde_json::json!({
            "session_id": session_id,
            "state": state,
            "tool_name": "",
            "cwd": "/tmp",
            "transcript_path": "/tmp/transcript.jsonl"
        });
        fs::write(&path, serde_json::to_vec(&json).unwrap()).unwrap();
        path
    }

    fn write_runtime_event(dir: &Path, name: &str, session_id: &str) -> PathBuf {
        write_runtime_event_with_tool_name(dir, name, session_id, None).0
    }

    fn write_runtime_event_with_tool_name(
        dir: &Path,
        name: &str,
        session_id: &str,
        tool_name: Option<&str>,
    ) -> (PathBuf, Value) {
        let path = dir.join(name);
        let mut json = serde_json::json!({
            "runtime_key": format!("claude:{}", session_id),
            "session_id": session_id,
            "provider": "claude",
            "device_id": "work-laptop",
            "source": "claude_channel_wrapper",
            "kind": "terminal_signal",
            "occurred_at": "2026-05-20T21:06:20Z",
            "dedupe_key": format!("claude-terminal:{}:0:2026-05-20T21:06:20Z", session_id),
            "payload": {
                "terminal_state": "session_ended",
                "terminal_reason": "provider_exit",
                "terminal_source": "claude_channel_wrapper",
                "provider_session_id": session_id,
                "exit_code": 0
            }
        });
        if let Some(tool_name) = tool_name {
            json["tool_name"] = Value::String(tool_name.to_string());
        }
        fs::write(&path, serde_json::to_vec(&json).unwrap()).unwrap();
        (path, json)
    }

    // ShipperClient can't be easily constructed without a real config, so
    // for unit tests we verify file-level behavior only (no HTTP).
    // HTTP behavior (delete-on-success, keep-on-failure) is validated in E2E.

    fn filter_ready(dir: &std::path::Path) -> Vec<std::path::PathBuf> {
        fs::read_dir(dir)
            .unwrap()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
                name.ends_with(".json") && !name.starts_with('.')
            })
            .collect()
    }

    #[test]
    fn test_skips_tmp_files() {
        let dir = make_outbox();
        // In-progress atomic write — starts with '.'
        let tmp = dir.path().join(".tmp.ABC123");
        fs::write(&tmp, b"{}").unwrap();
        // Also the old-style final name that was the bug: .tmp.ABC123.json
        let old_bad = dir.path().join(".tmp.ABC123.json");
        fs::write(&old_bad, b"{}").unwrap();
        // Correct final name from hook: prs.ABC123.json (no leading dot)
        write_presence(dir.path(), "prs.ABC123.json", "sess-1", "thinking");

        let ready = filter_ready(dir.path());
        assert_eq!(
            ready.len(),
            1,
            "only prs.*.json should be ready, not .tmp.* files"
        );
        assert_eq!(
            ready[0].file_name().unwrap().to_str().unwrap(),
            "prs.ABC123.json"
        );
    }

    #[test]
    fn test_hook_filename_pattern_is_picked_up() {
        // Verify the exact rename pattern the hook uses:
        //   mv "$TMPFILE" "${TMPFILE/\/.tmp\./\/prs.}.json"
        // which turns .tmp.XXXXXX → prs.XXXXXX.json
        let dir = make_outbox();
        let tmp_name = ".tmp.Zakvof";
        // Simulate the bash rename: replace /.tmp. with /prs. then append .json
        let final_name = tmp_name.replace(".tmp.", "prs.").to_owned() + ".json";
        assert_eq!(final_name, "prs.Zakvof.json");
        assert!(
            !final_name.starts_with('.'),
            "final name must not start with dot"
        );

        write_presence(dir.path(), &final_name, "sess-hook", "idle");
        let ready = filter_ready(dir.path());
        assert_eq!(
            ready.len(),
            1,
            "hook-produced filename must be picked up by drain"
        );
    }

    #[test]
    fn test_skips_nonexistent_dir() {
        // drain_outbox returns (0,0) when dir doesn't exist — no panic.
        let entries = std::fs::read_dir("/nonexistent/outbox/path/xyz");
        assert!(entries.is_err(), "nonexistent dir returns error");
        // drain_outbox handles this gracefully (returns (0,0))
    }

    // -----------------------------------------------------------------------
    // Integration tests — use a real inline HTTP server via tokio
    // -----------------------------------------------------------------------

    /// Write a presence file using the EXACT atomic rename pattern the hook uses:
    ///   mktemp .tmp.XXXXXX  →  mv to prs.XXXXXX.json
    /// This is the producer-consumer contract. If the naming convention drifts
    /// on either side, this test catches it.
    fn write_hook_style(
        dir: &std::path::Path,
        suffix: &str,
        session_id: &str,
        state: &str,
    ) -> std::path::PathBuf {
        let tmp = dir.join(format!(".tmp.{}", suffix));
        let final_path = dir.join(format!("prs.{}.json", suffix));
        let json = serde_json::json!({
            "session_id": session_id,
            "state": state,
            "tool_name": "",
            "cwd": "/tmp",
            "transcript_path": "/tmp/transcript.jsonl"
        });
        fs::write(&tmp, serde_json::to_vec(&json).unwrap()).unwrap();
        fs::rename(&tmp, &final_path).unwrap();
        final_path
    }

    /// Spawn a minimal HTTP server that returns `status` for every request.
    /// Returns the bound address, shared request log, and a task handle (call .abort() when done).
    /// Uses Arc<Mutex> for paths so the test can inspect them without awaiting the server task.
    async fn spawn_http_server(
        status: u16,
    ) -> (
        std::net::SocketAddr,
        std::sync::Arc<std::sync::Mutex<Vec<String>>>,
        tokio::task::JoinHandle<()>,
    ) {
        use std::sync::{Arc, Mutex};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let paths: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let paths_clone = paths.clone();

        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };

                // Read until end of HTTP headers
                let mut buf = vec![0u8; 4096];
                let mut total = 0;
                loop {
                    let n = socket.read(&mut buf[total..]).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    total += n;
                    if buf[..total].windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }

                // Extract path from request line
                let head = String::from_utf8_lossy(&buf[..total]).into_owned();
                let path = head
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().nth(1))
                    .unwrap_or("/")
                    .to_string();
                paths_clone.lock().unwrap().push(path);

                // Drain body so reqwest doesn't hang waiting for it to be consumed
                let content_len = head
                    .lines()
                    .find(|l| l.to_ascii_lowercase().starts_with("content-length:"))
                    .and_then(|l| l.split(':').nth(1))
                    .and_then(|v| v.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                let header_end = buf[..total]
                    .windows(4)
                    .position(|w| w == b"\r\n\r\n")
                    .unwrap()
                    + 4;
                let mut body_read = total - header_end;
                while body_read < content_len {
                    let n = socket.read(&mut buf).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    body_read += n;
                }

                let resp = format!(
                    "HTTP/1.1 {}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                    status
                );
                let _ = socket.write_all(resp.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });

        (addr, paths, handle)
    }

    /// Simulate Runtime Host validation: a batch containing an over-length
    /// tool_name is rejected with 422, while a valid batch is accepted.
    async fn spawn_runtime_validation_server() -> (
        std::net::SocketAddr,
        std::sync::Arc<std::sync::Mutex<Vec<(u16, Value)>>>,
        tokio::task::JoinHandle<()>,
    ) {
        use std::sync::{Arc, Mutex};
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let requests: Arc<Mutex<Vec<(u16, Value)>>> = Arc::new(Mutex::new(Vec::new()));
        let requests_clone = requests.clone();

        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };

                let mut request = Vec::with_capacity(4096);
                let header_end = loop {
                    let mut buf = [0u8; 4096];
                    let n = socket.read(&mut buf).await.unwrap_or(0);
                    if n == 0 {
                        break None;
                    }
                    request.extend_from_slice(&buf[..n]);
                    if let Some(position) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                        break Some(position + 4);
                    }
                };
                let Some(header_end) = header_end else {
                    continue;
                };
                let headers = String::from_utf8_lossy(&request[..header_end]);
                let content_len = headers
                    .lines()
                    .find(|line| line.to_ascii_lowercase().starts_with("content-length:"))
                    .and_then(|line| line.split(':').nth(1))
                    .and_then(|value| value.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                while request.len().saturating_sub(header_end) < content_len {
                    let mut buf = [0u8; 4096];
                    let n = socket.read(&mut buf).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    request.extend_from_slice(&buf[..n]);
                }

                let body_end = header_end.saturating_add(content_len).min(request.len());
                let body = serde_json::from_slice::<Value>(&request[header_end..body_end])
                    .unwrap_or_else(|_| serde_json::json!({}));
                let rejects = body
                    .get("events")
                    .and_then(Value::as_array)
                    .is_some_and(|events| {
                        events.iter().any(|event| {
                            event
                                .get("tool_name")
                                .and_then(Value::as_str)
                                .is_some_and(|tool_name| tool_name.chars().count() > 128)
                        })
                    });
                let status = if rejects { 422 } else { 204 };
                requests_clone.lock().unwrap().push((status, body));
                let response_body = if rejects {
                    r#"{"detail":[{"loc":["body","events",0,"tool_name"],"msg":"String should have at most 128 characters","type":"string_too_long"}]}"#
                } else {
                    ""
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response_body}",
                    response_body.len()
                );
                let _ = socket.write_all(response.as_bytes()).await;
                let _ = socket.shutdown().await;
            }
        });

        (addr, requests, handle)
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_success_deletes_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Write one file using the exact hook rename pattern
        let f = write_hook_style(dir.path(), "OK1234", "sess-ok", "thinking");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1, "one event should be sent");
        assert_eq!(kept, 0);
        assert!(!f.exists(), "file must be deleted after successful POST");

        // Verify the server received exactly 1 POST to the presence endpoint
        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1);
        assert_eq!(logged[0], "/api/agents/presence");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_runtime_event_outbox_success_deletes_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();
        let f = write_runtime_event(dir.path(), "rte.ok.json", "sess-runtime-ok");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_runtime_event_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1);
        assert_eq!(kept, 0);
        assert!(
            !f.exists(),
            "runtime event file must be deleted after successful POST"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1);
        assert_eq!(logged[0], "/api/agents/runtime/events/batch");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_runtime_event_outbox_failure_keeps_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(503).await;
        let dir = tempfile::tempdir().unwrap();
        let f = write_runtime_event(dir.path(), "rte.retry.json", "sess-runtime-retry");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_runtime_event_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 1);
        assert!(f.exists(), "runtime event file must remain for retry");

        server.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_runtime_event_poison_pill_does_not_wedge_valid_terminal() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, requests, server) = spawn_runtime_validation_server().await;
        let dir = tempfile::tempdir().unwrap();
        let (poison, poison_event) = write_runtime_event_with_tool_name(
            dir.path(),
            "rte.poison.json",
            "sess-poison",
            Some(&"x".repeat(129)),
        );
        let (terminal, terminal_event) = write_runtime_event_with_tool_name(
            dir.path(),
            "rte.terminal.json",
            "sess-terminal",
            None,
        );

        let url = format!("http://{addr}");
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_runtime_event_outbox(dir.path(), &client).await;
        server.abort();
        let requests = requests.lock().unwrap().clone();

        assert_eq!(
            sent,
            1,
            "reproduction: permanently rejected event wedged valid event (sent={sent}, kept={kept}, requests={requests:?})"
        );
        assert_eq!(kept, 0);
        assert!(
            !terminal.exists(),
            "valid terminal must be removed after delivery"
        );
        assert!(!poison.exists(), "poison event must leave the ready queue");
        let dead_letters = fs::read_dir(dir.path().join(RUNTIME_EVENT_DEAD_LETTER_DIR))
            .unwrap()
            .flatten()
            .map(|entry| entry.path())
            .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
            .collect::<Vec<_>>();
        assert_eq!(dead_letters.len(), 1, "poison event must be dead-lettered");
        let evidence: Value = serde_json::from_slice(&fs::read(&dead_letters[0]).unwrap()).unwrap();
        assert_eq!(evidence["event"], poison_event);
        assert_eq!(evidence["status_code"], 422);
        assert_eq!(
            evidence["response_body"],
            r#"{"detail":[{"loc":["body","events",0,"tool_name"],"msg":"String should have at most 128 characters","type":"string_too_long"}]}"#
        );
        assert!(
            requests.iter().any(|(status, body)| {
                *status == 204
                    && body
                        .get("events")
                        .and_then(Value::as_array)
                        .is_some_and(|events| events == std::slice::from_ref(&terminal_event))
            }),
            "the valid terminal must reach the server in its own accepted request"
        );
        assert!(
            requests.iter().any(|(status, body)| {
                *status == 422
                    && body
                        .get("events")
                        .and_then(Value::as_array)
                        .is_some_and(|events| events.contains(&poison_event))
            }),
            "the server must observe the actual validation rejection"
        );
    }

    #[test]
    fn a_phase_signal_outside_the_contract_never_reaches_the_outbox() {
        // `tool` is the phase codex_exec actually shipped. Ingest now rejects it,
        // which dead-letters the event, so the producer must fail here first.
        let dir = tempfile::tempdir().unwrap();
        let event = json!({"kind": "phase_signal", "phase": "tool", "session_id": "sess"});
        let outcome = enqueue_runtime_event(dir.path(), &event);
        assert!(
            outcome.is_err(),
            "a phase outside the contract must be refused at the producer"
        );
        let queued = fs::read_dir(dir.path()).unwrap().flatten().count();
        assert_eq!(queued, 0, "a refused event must not reach the queue");
    }

    #[test]
    fn contract_phases_and_state_bearing_kinds_still_enqueue() {
        let dir = tempfile::tempdir().unwrap();
        for phase in crate::managed_phase_contract::WIRE_PHASES {
            let event = json!({"kind": "phase_signal", "phase": phase, "session_id": "sess"});
            enqueue_runtime_event(dir.path(), &event).unwrap();
        }
        // A terminal legitimately ships `finished`, which is local-health-only.
        let terminal =
            json!({"kind": "terminal_signal", "phase": "finished", "session_id": "sess"});
        enqueue_runtime_event(dir.path(), &terminal).unwrap();
        // And a terminal is never gated on vocabulary at all.
        let odd = json!({"kind": "terminal_signal", "phase": "tool", "session_id": "sess"});
        enqueue_runtime_event(dir.path(), &odd).unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn rate_limited_runtime_events_are_retried_not_dead_lettered() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // 429 is a 4xx, but a later attempt succeeds. Dead-lettering it would
        // lose terminal signals exactly when the outbox is busiest -- the same
        // class of loss that poison isolation exists to prevent.
        let (addr, _paths, server) = spawn_http_server(429).await;
        let dir = tempfile::tempdir().unwrap();
        let event = write_runtime_event(dir.path(), "rte.rate-limited.json", "sess-throttled");

        let url = format!("http://{addr}");
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_runtime_event_outbox(dir.path(), &client).await;
        server.abort();

        assert_eq!(sent, 0);
        assert_eq!(kept, 1, "a rate-limited event must stay queued for retry");
        assert!(event.exists(), "rate-limited event must remain on disk");

        let dead_letter_dir = dir.path().join(RUNTIME_EVENT_DEAD_LETTER_DIR);
        let dead_letters = fs::read_dir(&dead_letter_dir)
            .map(|entries| {
                entries
                    .flatten()
                    .map(|entry| entry.path())
                    .filter(|path| {
                        path.extension().and_then(|value| value.to_str()) == Some("json")
                    })
                    .count()
            })
            .unwrap_or(0);
        assert_eq!(
            dead_letters, 0,
            "a rate limit must never dead-letter an event"
        );
    }

    #[test]
    fn test_collect_runtime_event_outbox_deletes_malformed_files() {
        let dir = tempfile::tempdir().unwrap();
        let bad = dir.path().join("rte.bad.json");
        let scalar = dir.path().join("rte.scalar.json");
        fs::write(&bad, b"not valid json").unwrap();
        fs::write(&scalar, b"[]").unwrap();

        let posts = collect_runtime_event_outbox(dir.path());

        assert!(posts.is_empty());
        assert!(
            !bad.exists(),
            "malformed runtime event file must be deleted"
        );
        assert!(
            !scalar.exists(),
            "non-object runtime event file must be deleted"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_result_returns_sent_phase_signal() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        write_hook_style(dir.path(), "SIG123", "sess-signal", "needs_user");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(
            Some(&url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let result =
            drain_outbox_with_local_state_result(dir.path(), &client, Some(db.path())).await;

        assert_eq!(result.sent, 1);
        assert_eq!(result.kept, 0);
        assert_eq!(result.signals.len(), 1);
        assert_eq!(result.signals[0].session_id, "sess-signal");
        assert_eq!(result.signals[0].provider, "claude");
        assert_eq!(result.signals[0].phase, "needs_user");
        assert_eq!(
            result.signals[0].transcript_path,
            Some(PathBuf::from("/tmp/transcript.jsonl"))
        );

        server.abort();
    }

    #[test]
    fn test_collect_outbox_result_returns_signal_before_post() {
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        let f = write_hook_style(dir.path(), "FAST123", "sess-fast", "thinking");

        let result = collect_outbox_with_local_state_result(dir.path(), Some(db.path()));

        assert_eq!(result.signals.len(), 1);
        assert_eq!(result.signals[0].session_id, "sess-fast");
        assert_eq!(result.signals[0].phase, "thinking");
        assert_eq!(result.posts.len(), 1);
        assert!(
            f.exists(),
            "presence file should remain until the POST/delete phase completes"
        );
    }

    #[test]
    fn test_collect_outbox_persists_managed_transcript_binding_before_post() {
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        drop(crate::state::db::open_db(Some(db.path())).unwrap());
        let transcript = tempfile::NamedTempFile::new().unwrap();
        let path = dir.path().join("prs.MANAGED.json");
        let payload = serde_json::json!({
            "session_id": "managed-session",
            "state": "thinking",
            "provider": "claude",
            "control_path": "managed",
            "transcript_path": transcript.path(),
        });
        fs::write(&path, serde_json::to_vec(&payload).unwrap()).unwrap();
        std::process::Command::new("touch")
            .args(["-t", "197001020000", path.to_str().unwrap()])
            .status()
            .expect("touch failed");

        let result = collect_outbox_with_local_state_result(dir.path(), Some(db.path()));

        assert_eq!(result.posts.len(), 1);
        let conn = crate::state::db::open_db(Some(db.path())).unwrap();
        let row: (String, String, String) = conn
            .query_row(
                "SELECT path, session_id, provider FROM session_binding",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(
            PathBuf::from(row.0),
            fs::canonicalize(transcript.path()).unwrap()
        );
        assert_eq!(row.1, "managed-session");
        assert_eq!(row.2, "claude");
    }

    #[test]
    fn test_collect_outbox_persists_antigravity_managed_binding_intent() {
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        drop(crate::state::db::open_db(Some(db.path())).unwrap());
        let transcript = dir
            .path()
            .join("brain/conversation/.system_generated/logs/transcript_full.jsonl");
        fs::create_dir_all(transcript.parent().unwrap()).unwrap();
        fs::write(&transcript, b"canonical snapshot\n").unwrap();
        let mirror = transcript.with_file_name("transcript.jsonl");
        fs::write(&mirror, b"truncated summary\n").unwrap();
        let path = dir.path().join("prs.ANTIGRAVITY.json");
        let payload = serde_json::json!({
            "session_id": "antigravity-session",
            "state": "idle",
            "provider": "antigravity",
            "control_path": "managed",
            "transcript_path": mirror,
        });
        fs::write(&path, serde_json::to_vec(&payload).unwrap()).unwrap();

        let result = collect_outbox_with_local_state_result(dir.path(), Some(db.path()));

        assert_eq!(result.posts.len(), 1);
        let conn = crate::state::db::open_db(Some(db.path())).unwrap();
        let row: (String, String, String) = conn
            .query_row(
                "SELECT path, session_id, provider FROM session_binding",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(PathBuf::from(row.0), fs::canonicalize(&transcript).unwrap());
        assert_eq!(row.1, "antigravity-session");
        assert_eq!(row.2, "antigravity");
    }

    #[test]
    fn test_empty_outbox_does_not_open_local_phase_db() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("engine.db");

        let result = collect_outbox_with_local_state_result(dir.path(), Some(&db_path));

        assert!(result.signals.is_empty());
        assert!(result.posts.is_empty());
        assert!(
            !db_path.exists(),
            "empty outbox ticks should not touch SQLite on the daemon hot loop"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_network_error_keeps_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        use tokio::net::TcpListener;

        // Server that accepts then immediately drops the socket — reqwest gets
        // a connection-closed error without a response, which makes post_json return Err.
        // This is faster and more reliable than pointing at a closed port (avoids timeout).
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            while let Ok((socket, _)) = listener.accept().await {
                drop(socket); // close immediately, no response written
            }
        });

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let dir = tempfile::tempdir().unwrap();
        let f = write_hook_style(dir.path(), "ERR123", "sess-err", "running");

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 1, "file must be kept when POST fails");
        assert!(f.exists(), "file must not be deleted on network error");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_presence_post_timeout_keeps_file() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;
        use tokio::io::AsyncReadExt;
        use tokio::net::TcpListener;

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            while let Ok((mut socket, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut buf = [0u8; 512];
                    let _ = socket.read(&mut buf).await;
                    tokio::time::sleep(Duration::from_secs(2)).await;
                });
            }
        });

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let dir = tempfile::tempdir().unwrap();
        let files = (0..8)
            .map(|index| {
                write_hook_style(
                    dir.path(),
                    &format!("SLOW{index}"),
                    &format!("sess-slow-{index}"),
                    "thinking",
                )
            })
            .collect::<Vec<_>>();
        let db_path = dir.path().join("state.db");
        let posts = collect_outbox_with_local_state_result(dir.path(), Some(&db_path)).posts;

        let started = std::time::Instant::now();
        let (sent, kept) =
            post_pending_presence_files_with_timeout(&client, posts, Duration::from_millis(50))
                .await;

        assert_eq!(sent, 0);
        assert_eq!(kept, 8, "slow presence POSTs should be retried later");
        assert!(
            started.elapsed() < Duration::from_millis(300),
            "timeouts must be concurrent rather than queued file by file"
        );
        assert!(
            files.iter().all(|file| file.exists()),
            "files must remain after a presence POST timeout"
        );

        server.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_result_returns_phase_signal_when_post_fails() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(503).await;
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();

        let f = write_hook_style(dir.path(), "LOCAL123", "sess-local", "thinking");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(
            Some(&url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let result =
            drain_outbox_with_local_state_result(dir.path(), &client, Some(db.path())).await;

        assert_eq!(result.sent, 0);
        assert_eq!(result.kept, 1);
        assert_eq!(result.signals.len(), 1);
        assert_eq!(result.signals[0].session_id, "sess-local");
        assert_eq!(result.signals[0].phase, "thinking");
        assert!(
            f.exists(),
            "file must still be retried for presence delivery"
        );

        server.abort();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_dot_files_never_posted() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // Server that accepts exactly 1 request (the real prs.* file)
        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Dot-prefixed files — must be skipped by drain
        let tmp_in_progress = dir.path().join(".tmp.ABC123");
        let old_bad_pattern = dir.path().join(".tmp.ABC123.json"); // the bug we fixed
        fs::write(&tmp_in_progress, b"{}").unwrap();
        fs::write(
            &old_bad_pattern,
            serde_json::to_vec(&serde_json::json!({
                "session_id": "sess-dot", "state": "thinking"
            }))
            .unwrap(),
        )
        .unwrap();

        // One real file using correct hook pattern
        let real = write_hook_style(dir.path(), "REAL01", "sess-real", "idle");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 1, "only the real file should be sent");
        assert_eq!(kept, 0);
        assert!(!real.exists(), "real file deleted");
        assert!(tmp_in_progress.exists(), ".tmp file must not be touched");
        assert!(
            old_bad_pattern.exists(),
            "old .tmp.*.json pattern must be skipped (the bug we fixed)"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(
            logged.len(),
            1,
            "only 1 POST — dot files must not be POSTed"
        );
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_coalesces_same_session() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Three files for the same session — only latest should be POSTed
        write_hook_style(dir.path(), "S1A", "sess-multi", "thinking");
        std::thread::sleep(Duration::from_millis(10));
        write_hook_style(dir.path(), "S1B", "sess-multi", "running");
        std::thread::sleep(Duration::from_millis(10));
        let latest = write_hook_style(dir.path(), "S1C", "sess-multi", "idle");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        let older_a = dir.path().join("prs.S1A.json");
        let older_b = dir.path().join("prs.S1B.json");

        assert_eq!(sent, 1, "3 files for same session → 1 POST");
        assert_eq!(kept, 0);
        assert!(!latest.exists(), "latest file deleted after send");
        assert!(
            !older_a.exists(),
            "older file S1A deleted during coalescing"
        );
        assert!(
            !older_b.exists(),
            "older file S1B deleted during coalescing"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 1, "only 1 POST despite 3 files");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_deletes_invalid_json() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // Server that should NOT be called — malformed files get deleted, not POSTed.
        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Write a malformed JSON file using the prs.* naming (would be picked up)
        let bad = dir.path().join("prs.bad.json");
        fs::write(&bad, b"not valid json!!!").unwrap();

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0, "malformed file must not be POSTed");
        assert_eq!(kept, 0, "malformed file must not be kept for retry");
        assert!(!bad.exists(), "malformed file must be deleted");

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 0, "no POSTs for malformed file");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_deletes_stale_tmp_files() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        // Server that must NOT be called — stale dot-files get deleted, never POSTed.
        let (addr, paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();

        // Two stale dot-file variants the hook can produce:
        //   1. .tmp.XXXXXX          — orphaned before mv (killed mid-write)
        //   2. .tmp.XXXXXX.json     — old buggy hook pattern
        let stale_no_ext = dir.path().join(".tmp.STALE1");
        let stale_with_ext = dir.path().join(".tmp.STALE2.json");
        fs::write(&stale_no_ext, b"{}").unwrap();
        fs::write(&stale_with_ext, b"{}").unwrap();

        // Backdate both past STALE_SECS (600s) using touch(1).
        // Using a timestamp clearly in the past (1970-01-02 = +86400s epoch).
        for path in [&stale_no_ext, &stale_with_ext] {
            std::process::Command::new("touch")
                .args(["-t", "197001020000", path.to_str().unwrap()])
                .status()
                .expect("touch failed");
        }

        // One fresh in-progress temp — must NOT be touched (age ~0).
        let fresh_tmp = dir.path().join(".tmp.FRESH1");
        fs::write(&fresh_tmp, b"{}").unwrap();

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(Some(&url), None, None, None, None, None);
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) = drain_outbox(dir.path(), &client).await;

        assert_eq!(sent, 0, "stale dot-files must not be POSTed");
        assert_eq!(kept, 0, "stale dot-files must not be kept for retry");
        assert!(!stale_no_ext.exists(), ".tmp.STALE1 must be deleted");
        assert!(!stale_with_ext.exists(), ".tmp.STALE2.json must be deleted");
        assert!(
            fresh_tmp.exists(),
            "fresh in-progress .tmp must be left alone"
        );

        server.abort();
        let logged = paths.lock().unwrap().clone();
        assert_eq!(logged.len(), 0, "no POSTs — stale dot-files never sent");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn test_drain_outbox_with_local_state_persists_latest_phase() {
        use crate::config::ShipperConfig;
        use crate::pipeline::compressor::CompressionAlgo;
        use crate::shipping::client::ShipperClient;

        let (addr, _paths, server) = spawn_http_server(204).await;
        let dir = tempfile::tempdir().unwrap();
        let db = tempfile::NamedTempFile::new().unwrap();
        // The daemon bootstraps the schema before maintenance connections are opened.
        drop(crate::state::db::open_db(Some(db.path())).unwrap());

        write_hook_style(dir.path(), "PHASE1", "sess-phase", "thinking");
        std::thread::sleep(Duration::from_millis(10));
        write_hook_style(dir.path(), "PHASE2", "sess-phase", "running");

        let url = format!("http://{}", addr);
        let cfg = ShipperConfig::default().with_overrides(
            Some(&url),
            None,
            Some(db.path()),
            None,
            None,
            None,
        );
        let client = ShipperClient::with_compression(&cfg, CompressionAlgo::Gzip).unwrap();

        let (sent, kept) =
            drain_outbox_with_local_state(dir.path(), &client, Some(db.path())).await;

        assert_eq!(sent, 1);
        assert_eq!(kept, 0);

        let conn = crate::state::db::open_db(Some(db.path())).unwrap();
        let row: (String, Option<String>, String) = conn
            .query_row(
                "SELECT phase, tool_name, source
                 FROM session_phase_state
                 WHERE session_id = 'sess-phase'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(row.0, "running");
        assert!(row.1.is_none());
        assert_eq!(row.2, "claude_hook");

        assert!(conn.prepare("SELECT 1 FROM managed_session_state").is_err());

        server.abort();
    }
    #[test]
    fn local_only_phase_is_persisted_without_presence_post() {
        let home = tempfile::tempdir().unwrap();
        let agent_dir = home.path().join("agent");
        let db_path = agent_dir.join("longhouse-shipper.db");
        drop(crate::state::db::open_db(Some(&db_path)).unwrap());

        crate::hook_outbox::enqueue_local_phase(
            &db_path,
            "sess-local-only",
            "codex",
            "finished",
            None,
            "codex_exec",
            "2026-04-19T00:00:00+00:00",
        )
        .unwrap();

        let result =
            collect_outbox_with_local_state_result(&agent_dir.join("outbox"), Some(&db_path));
        assert!(result.posts.is_empty());
        assert!(result.signals.is_empty());

        let conn = crate::state::db::open_db(Some(&db_path)).unwrap();
        let row: (String, String, String) = conn
            .query_row(
                "SELECT phase, provider, source
                 FROM session_phase_state
                 WHERE session_id = 'sess-local-only'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(
            row,
            ("finished".into(), "codex".into(), "codex_exec".into())
        );
        assert!(std::fs::read_dir(agent_dir.join("outbox"))
            .unwrap()
            .next()
            .is_none());
    }
}

#[cfg(test)]
mod runtime_status_collection_tests {
    use super::*;
    use serde_json::json;
    use tempfile::TempDir;

    fn phase_event(session: &str, run: &str, phase: &str, occurred_at: &str) -> Value {
        json!({
            "runtime_key": format!("omp:{session}"),
            "session_id": session,
            "provider": "omp",
            "run_id": run,
            "source": "omp_helm_channel",
            "kind": "phase_signal",
            "phase": phase,
            "occurred_at": occurred_at,
            "dedupe_key": format!("omp-phase:{session}:{run}:{phase}:{occurred_at}"),
        })
    }

    fn progress_event(session: &str, run: &str, turn: &str, seq: u64, occurred_at: &str) -> Value {
        json!({
            "runtime_key": format!("omp:{session}"),
            "session_id": session,
            "provider": "omp",
            "run_id": run,
            "source": "omp_helm_channel",
            "kind": "progress_signal",
            "occurred_at": occurred_at,
            "dedupe_key": format!("omp-progress:{session}:{run}:{turn}:{seq}"),
            "payload": {"turn_id": turn, "seq": seq, "live_text": "hello"},
        })
    }

    fn terminal_event(session: &str, run: &str, occurred_at: &str) -> Value {
        json!({
            "runtime_key": format!("omp:{session}"),
            "session_id": session,
            "provider": "omp",
            "run_id": run,
            "kind": "managed_terminal",
            "occurred_at": occurred_at,
            "dedupe_key": format!("omp-helm-terminal:{session}:{run}"),
        })
    }

    fn write(dir: &Path, event: &Value) {
        enqueue_runtime_event(dir, event).expect("enqueue");
    }

    fn write_plain(dir: &Path, event: &Value) {
        let path = dir.join(format!("{}.json", uuid::Uuid::new_v4()));
        std::fs::write(path, serde_json::to_vec(event).expect("serialize")).expect("write");
    }

    fn ready_files(dir: &Path) -> usize {
        std::fs::read_dir(dir)
            .expect("read_dir")
            .flatten()
            .filter(|entry| {
                entry
                    .file_name()
                    .to_str()
                    .is_some_and(|name| name.ends_with(".json") && !name.starts_with('.'))
            })
            .count()
    }

    #[test]
    fn keeps_only_the_newest_observation_per_replaceable_coordinate() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path();
        for tick in 0..50 {
            write(
                dir,
                &phase_event("s1", "r1", "thinking", &format!("2026-09-17T15:00:{tick:02}Z")),
            );
        }
        write(dir, &phase_event("s2", "r2", "running", "2026-09-17T15:00:10Z"));

        let posts = collect_runtime_event_outbox(dir);

        assert_eq!(posts.len(), 2, "one current phase per session");
        let s1 = posts
            .iter()
            .find(|post| post.event["session_id"] == "s1")
            .expect("s1 phase");
        assert_eq!(s1.event["occurred_at"], "2026-09-17T15:00:49Z");
        assert_eq!(ready_files(dir), 2, "superseded observations are deleted");
    }

    #[test]
    fn coalesces_progress_by_turn_and_sequence() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path();
        for seq in 0..20 {
            write(
                dir,
                &progress_event("s1", "r1", "turn-1", seq, "2026-09-17T15:00:00Z"),
            );
        }
        write(
            dir,
            &progress_event("s1", "r1", "turn-2", 0, "2026-09-17T15:00:01Z"),
        );

        let posts = collect_runtime_event_outbox(dir);

        assert_eq!(posts.len(), 2, "one preview per turn");
        let turn_one = posts
            .iter()
            .find(|post| post.event["payload"]["turn_id"] == "turn-1")
            .expect("turn-1 preview");
        assert_eq!(turn_one.event["payload"]["seq"], 19);
    }

    #[test]
    fn never_merges_durable_records_and_orders_by_observation() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path();
        write(dir, &phase_event("s1", "r1", "running", "2026-09-17T15:00:01Z"));
        write(dir, &terminal_event("s1", "r1", "2026-09-17T15:00:02Z"));
        write(dir, &terminal_event("s2", "r2", "2026-09-17T15:00:03Z"));

        let posts = collect_runtime_event_outbox(dir);

        assert_eq!(posts.len(), 3);
        let order: Vec<&str> = posts
            .iter()
            .map(|post| post.event["occurred_at"].as_str().expect("occurred_at"))
            .collect();
        assert_eq!(
            order,
            vec![
                "2026-09-17T15:00:01Z",
                "2026-09-17T15:00:02Z",
                "2026-09-17T15:00:03Z"
            ],
            "a session's own order must hold across the batch boundary"
        );
    }

    #[test]
    fn collection_is_bounded_per_pass() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path();
        for index in 0..40 {
            write(dir, &terminal_event(&format!("s{index}"), "r1", "2026-09-17T15:00:00Z"));
        }

        let pass = collect_runtime_event_outbox_bounded(dir, 10);
        let posts = pass.posts;

        assert!(pass.saturated, "a full pass reports more work waiting");
        assert_eq!(posts.len(), 10, "inspection stops at the cap");
        assert_eq!(ready_files(dir), 40, "unread durable records stay queued");
    }

    #[test]
    fn recovery_reduces_a_flooded_outbox_without_losing_durable_records() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path().join("runtime-events-outbox");
        std::fs::create_dir_all(&dir).expect("create outbox");
        for tick in 0..200 {
            write(
                &dir,
                &phase_event("s1", "r1", "thinking", &format!("2026-09-17T15:{:02}:00Z", tick % 60)),
            );
        }
        write(&dir, &terminal_event("s2", "r2", "2026-09-17T15:59:00Z"));

        let recovery = recover_runtime_event_outbox(&dir, 10).expect("recovery runs above threshold");

        assert_eq!(recovery.inspected, 201);
        assert_eq!(recovery.kept, 2, "one current phase plus the terminal record");
        assert_eq!(ready_files(&dir), 2);
        let posts = collect_runtime_event_outbox(&dir);
        assert!(
            posts
                .iter()
                .any(|post| post.event["kind"] == "managed_terminal"),
            "a terminal record is never reduced away"
        );
        assert!(
            tmp.path()
                .read_dir()
                .expect("read_dir")
                .flatten()
                .all(|entry| entry.file_name() == "runtime-events-outbox"),
            "the aside directory is removed once reduced"
        );
    }

    /// Incident scale, scaled down for a unit test: the 2026-09-17 flood left
    /// 250k files, five live sessions, and one terminal record that still had
    /// to arrive. Recovery must be bounded by coordinates, not by file count.
    #[test]
    fn recovery_holds_at_incident_shape() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path().join("runtime-events-outbox");
        std::fs::create_dir_all(&dir).expect("create outbox");
        // Written without the producer's fsync: this measures recovery, and
        // 20k durable enqueues cost minutes on their own — which is its own
        // evidence about publishing status per token.
        for tick in 0..4_000 {
            for session in 0..5 {
                write_plain(
                    &dir,
                    &phase_event(
                        &format!("s{session}"),
                        "r1",
                        "thinking",
                        &format!("2026-09-17T15:00:{:02}.{:03}Z", tick % 60, tick % 1000),
                    ),
                );
            }
        }
        write_plain(&dir, &terminal_event("s3", "r1", "2026-09-17T15:59:59Z"));

        let recovery =
            recover_runtime_event_outbox(&dir, RUNTIME_EVENT_RECOVERY_THRESHOLD).expect("recovery");

        assert_eq!(recovery.inspected, 20_001);
        assert_eq!(recovery.kept, 6, "five current phases plus one terminal record");
        let posts = collect_runtime_event_outbox(&dir);
        assert_eq!(posts.len(), 6);
        assert!(!collect_runtime_event_outbox_pass(&dir).saturated);
    }

    #[test]
    fn recovery_is_skipped_below_the_threshold() {
        let tmp = TempDir::new().expect("tempdir");
        let dir = tmp.path().join("runtime-events-outbox");
        std::fs::create_dir_all(&dir).expect("create outbox");
        write(&dir, &phase_event("s1", "r1", "thinking", "2026-09-17T15:00:00Z"));

        assert!(recover_runtime_event_outbox(&dir, 10).is_none());
        assert_eq!(ready_files(&dir), 1);
    }
}
