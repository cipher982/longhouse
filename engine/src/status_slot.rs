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
    let mut events = vec![serde_json::json!({
        "runtime_key": slot.runtime_key,
        "session_id": slot.session_id,
        "provider": slot.provider,
        "run_id": slot.run_id,
        "source": slot.source,
        "kind": "phase_signal",
        "phase": slot.phase,
        "tool_name": slot.tool_name,
        "occurred_at": slot.observed_at,
        "dedupe_key": format!(
            "omp-phase:{}:{}:{}:{}",
            slot.session_id, slot.run_id, slot.phase, slot.observed_at
        ),
        "payload": slot.payload,
    })];
    if let Some(preview) = slot.preview.as_ref() {
        events.push(serde_json::json!({
            "runtime_key": slot.runtime_key,
            "session_id": slot.session_id,
            "provider": slot.provider,
            "run_id": slot.run_id,
            "source": slot.source,
            "kind": "progress_signal",
            "occurred_at": slot.observed_at,
            "dedupe_key": format!(
                "omp-progress:{}:{}:{}:{}",
                slot.session_id, slot.run_id, preview.turn_id, preview.seq
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

    #[test]
    fn a_slot_without_a_preview_states_only_its_phase() {
        let events = runtime_events(&slot("s1", "idle", 1));
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["kind"], "phase_signal");
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
