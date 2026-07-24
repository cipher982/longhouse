//! Durable terminal publication for managed Codex teardown.
//!
//! Hosted only ends a run when it receives a `terminal_signal` runtime event
//! (`server/zerg/services/session_runtime.py` `_apply_run_terminal_event`).
//! Managed-lease withdrawal reports that *control* disappeared, not that the
//! run ended or why — the heartbeat path deliberately synthesizes no runtime
//! events. So teardown cannot simply drop the terminal publication and let
//! lease omission speak for it.
//!
//! It also must not block the user's exit on the network, which is what the
//! old synchronous post did: a 5s-per-attempt POST behind a retry ladder
//! totalling ~254s, sitting in front of an IPC reply the facade only waited 2s
//! for. A slow server was reported to the user as a local teardown failure.
//!
//! The split here: the bridge state file is the durable commit point and
//! carries the fully-formed terminal event; the outbox carries delivery; the
//! daemon reconciles any state that committed but never published. Teardown
//! performs no network I/O at all.

use std::path::Path;

use anyhow::{Context, Result};
use serde_json::{json, Value};

use crate::codex_bridge::BridgeStateFile;

/// Terminal identity is derived from session and run rather than minted fresh
/// per call, so the original enqueue, a reconciliation republish, and a
/// concurrent second teardown all collapse to one event on the server's
/// existing `dedupe_key` contract.
pub fn terminal_dedupe_key(session_id: &str, run_id: Option<&str>) -> String {
    match run_id {
        Some(run) if !run.trim().is_empty() => format!("bridge:terminal:{session_id}:{run}"),
        _ => format!("bridge:terminal:{session_id}"),
    }
}

/// Build the terminal runtime event. Shape matches what the bridge runtime
/// sink posted previously; only the dedupe key changed from a random UUID to
/// a stable identity.
pub fn build_terminal_event(
    session_id: &str,
    run_id: Option<&str>,
    thread_id: Option<&str>,
    device_id: Option<&str>,
    terminal_state: &str,
    terminal_reason: &str,
    occurred_at: &str,
    source: &str,
) -> Value {
    json!({
        "runtime_key": format!("codex:{session_id}"),
        "session_id": session_id,
        "provider": "codex",
        "device_id": device_id,
        "source": source,
        "kind": "terminal_signal",
        "phase": Value::Null,
        "tool_name": Value::Null,
        "occurred_at": occurred_at,
        "dedupe_key": terminal_dedupe_key(session_id, run_id),
        "payload": {
            "managed_transport": "codex_app_server",
            "thread_id": thread_id,
            "terminal_state": terminal_state,
            "terminal_reason": terminal_reason,
            "terminal_source": source,
        }
    })
}

/// Stamp the terminal facts onto the state so the caller can commit them.
///
/// Deliberately does not write: the caller owns the single durable write, so
/// there is exactly one commit point rather than a sequence of partial ones.
pub fn stamp_terminal_commit(
    state: &mut BridgeStateFile,
    device_id: Option<&str>,
    terminal_state: &str,
    terminal_reason: &str,
    source: &str,
) {
    let occurred_at = chrono::Utc::now().to_rfc3339();
    let event = build_terminal_event(
        &state.session_id,
        state.run_id.as_deref(),
        state.thread_id.as_deref(),
        device_id,
        terminal_state,
        terminal_reason,
        &occurred_at,
        source,
    );
    state.status = "stopped".to_string();
    state.active_turn_id = None;
    state.last_error = None;
    state.terminal_state = Some(terminal_state.to_string());
    state.terminal_reason = Some(terminal_reason.to_string());
    state.stopped_at = Some(occurred_at);
    state.terminal_dedupe_key = Some(terminal_dedupe_key(
        &state.session_id,
        state.run_id.as_deref(),
    ));
    state.terminal_event = Some(event);
    state.terminal_published = false;
}

/// Hand the committed terminal event to the durable outbox.
pub fn publish_terminal_event(outbox_dir: &Path, state: &BridgeStateFile) -> Result<()> {
    let event = state
        .terminal_event
        .as_ref()
        .context("state has no committed terminal event to publish")?;
    crate::outbox::enqueue_runtime_event(outbox_dir, event).context("enqueue codex terminal event")
}

/// True when a state file committed a terminal outcome that was never handed
/// to the outbox. This is the crash window between the state rename and the
/// enqueue, plus any enqueue failure.
pub fn needs_terminal_reconciliation(state: &BridgeStateFile) -> bool {
    state.status.trim().eq_ignore_ascii_case("stopped")
        && !state.terminal_published
        && state.terminal_event.is_some()
}

/// Republish committed-but-unpublished terminal events found under
/// `state_dir`. Idempotent: the persisted dedupe key means a republish that
/// races the original enqueue collapses server-side.
///
/// Returns the number of events republished.
#[cfg_attr(not(test), allow(dead_code))]
pub fn reconcile_terminal_events(state_dir: &Path, outbox_dir: &Path) -> usize {
    let entries = match std::fs::read_dir(state_dir) {
        Ok(entries) => entries,
        Err(_) => return 0,
    };
    let paths = entries
        .flatten()
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.ends_with(".json") && !name.starts_with('.'))
        })
        .collect::<Vec<_>>();
    reconcile_terminal_event_paths(&paths, outbox_dir)
}

/// Reconcile a known set of state files. The daemon uses this with the paths
/// its scan already identified as `stopped`, avoiding a second full read of a
/// state directory that accumulates thousands of historical sessions.
pub fn reconcile_terminal_event_paths(paths: &[std::path::PathBuf], outbox_dir: &Path) -> usize {
    let mut republished = 0usize;
    for path in paths {
        let path = path.as_path();
        let bytes = match std::fs::read(path) {
            Ok(bytes) => bytes,
            Err(_) => continue,
        };
        let mut state: BridgeStateFile = match serde_json::from_slice(&bytes) {
            Ok(state) => state,
            Err(_) => continue,
        };
        if !needs_terminal_reconciliation(&state) {
            continue;
        }
        if publish_terminal_event(outbox_dir, &state).is_err() {
            // Leave the state untouched so the next tick retries. Delivery is
            // the daemon's job and a failed enqueue is not a lifecycle change.
            continue;
        }
        state.terminal_published = true;
        if let Err(error) = crate::codex_bridge::write_bridge_state_file(path, &state) {
            // The event is already durably queued, so a failed bookkeeping
            // write costs one duplicate republish next tick, which the dedupe
            // key absorbs.
            tracing::warn!(
                state_file = %path.display(),
                error = %error,
                "codex terminal reconciliation could not mark state published"
            );
        }
        republished += 1;
    }
    republished
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stopped_state(session_id: &str, run_id: Option<&str>) -> BridgeStateFile {
        let mut state = BridgeStateFile {
            session_id: session_id.to_string(),
            run_id: run_id.map(str::to_string),
            status: "running".to_string(),
            ..Default::default()
        };
        stamp_terminal_commit(
            &mut state,
            Some("cinder"),
            "session_ended",
            "clean_tui_exit",
            "codex_bridge_ws",
        );
        state
    }

    #[test]
    fn dedupe_key_is_stable_across_calls() {
        let first = stopped_state("s1", Some("r1"));
        let second = stopped_state("s1", Some("r1"));
        assert_eq!(first.terminal_dedupe_key, second.terminal_dedupe_key);
        assert_eq!(
            first.terminal_event.as_ref().unwrap()["dedupe_key"],
            second.terminal_event.as_ref().unwrap()["dedupe_key"],
        );
    }

    #[test]
    fn dedupe_key_separates_sessions() {
        assert_ne!(
            stopped_state("s1", Some("r1")).terminal_dedupe_key,
            stopped_state("s2", Some("r1")).terminal_dedupe_key,
        );
    }

    #[test]
    fn commit_marks_unpublished_and_stopped() {
        let state = stopped_state("s1", Some("r1"));
        assert_eq!(state.status, "stopped");
        assert!(!state.terminal_published);
        assert!(needs_terminal_reconciliation(&state));
    }

    #[test]
    fn published_state_needs_no_reconciliation() {
        let mut state = stopped_state("s1", Some("r1"));
        state.terminal_published = true;
        assert!(!needs_terminal_reconciliation(&state));
    }

    #[test]
    fn running_state_needs_no_reconciliation() {
        let state = BridgeStateFile {
            session_id: "s1".to_string(),
            status: "running".to_string(),
            ..Default::default()
        };
        assert!(!needs_terminal_reconciliation(&state));
    }

    /// Commit a stopped state to disk the way the bridge does, so
    /// reconciliation is exercised against real files rather than a fixture
    /// that only resembles them.
    fn commit_to_disk(state_dir: &Path, session_id: &str, run_id: &str) -> std::path::PathBuf {
        std::fs::create_dir_all(state_dir).unwrap();
        let mut state = BridgeStateFile {
            session_id: session_id.to_string(),
            run_id: Some(run_id.to_string()),
            status: "running".to_string(),
            ..Default::default()
        };
        stamp_terminal_commit(
            &mut state,
            Some("cinder"),
            "session_ended",
            "clean_tui_exit",
            "codex_bridge_ws",
        );
        let path = state_dir.join(format!("{session_id}.json"));
        crate::codex_bridge::write_bridge_state_file(&path, &state).unwrap();
        path
    }

    fn outbox_event_count(outbox_dir: &Path) -> usize {
        std::fs::read_dir(outbox_dir)
            .map(|entries| {
                entries
                    .flatten()
                    .filter(|entry| {
                        entry
                            .file_name()
                            .to_str()
                            .is_some_and(|name| name.ends_with(".json") && !name.starts_with('.'))
                    })
                    .count()
            })
            .unwrap_or(0)
    }

    fn read_state(path: &Path) -> BridgeStateFile {
        serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
    }

    /// The bridge committed the stopped fact and died before enqueuing.
    /// Without reconciliation the hosted run would stay open forever.
    #[test]
    fn crash_between_commit_and_enqueue_is_reconciled() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let outbox_dir = home.path().join("outbox");
        let path = commit_to_disk(&state_dir, "session-a", "run-a");

        assert_eq!(outbox_event_count(&outbox_dir), 0);
        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 1);
        assert_eq!(outbox_event_count(&outbox_dir), 1);
        assert!(read_state(&path).terminal_published);
    }

    /// The normal path already published; reconciliation must not re-emit, or
    /// every stopped session would republish on every tick.
    #[test]
    fn published_terminal_event_is_not_republished() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let outbox_dir = home.path().join("outbox");
        let path = commit_to_disk(&state_dir, "session-b", "run-b");
        let mut state = read_state(&path);
        state.terminal_published = true;
        crate::codex_bridge::write_bridge_state_file(&path, &state).unwrap();

        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 0);
        assert_eq!(outbox_event_count(&outbox_dir), 0);
    }

    #[test]
    fn reconciliation_is_idempotent_across_ticks() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let outbox_dir = home.path().join("outbox");
        commit_to_disk(&state_dir, "session-c", "run-c");

        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 1);
        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 0);
        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 0);
        assert_eq!(outbox_event_count(&outbox_dir), 1);
    }

    /// A republish racing the original enqueue, or a concurrent second
    /// teardown, must collapse server-side. That is what the persisted dedupe
    /// key buys — the old code minted a fresh UUID per post, so duplicates
    /// could not collapse.
    #[test]
    fn duplicate_publication_shares_one_dedupe_key() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let outbox_dir = home.path().join("outbox");
        let path = commit_to_disk(&state_dir, "session-d", "run-d");
        let state = read_state(&path);

        // The original enqueue, then a reconciliation republish.
        publish_terminal_event(&outbox_dir, &state).unwrap();
        reconcile_terminal_events(&state_dir, &outbox_dir);

        assert_eq!(outbox_event_count(&outbox_dir), 2, "both copies on disk");
        let keys = std::fs::read_dir(&outbox_dir)
            .unwrap()
            .flatten()
            .map(|entry| {
                let value: Value =
                    serde_json::from_slice(&std::fs::read(entry.path()).unwrap()).unwrap();
                value["dedupe_key"].as_str().unwrap().to_string()
            })
            .collect::<std::collections::HashSet<_>>();
        assert_eq!(keys.len(), 1, "duplicates must share one dedupe key");
        assert!(keys.contains("bridge:terminal:session-d:run-d"));
    }

    /// A running session must never be treated as terminal.
    #[test]
    fn running_session_is_never_reconciled() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let outbox_dir = home.path().join("outbox");
        std::fs::create_dir_all(&state_dir).unwrap();
        let state = BridgeStateFile {
            session_id: "session-e".to_string(),
            status: "ready".to_string(),
            ..Default::default()
        };
        crate::codex_bridge::write_bridge_state_file(&state_dir.join("session-e.json"), &state)
            .unwrap();

        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 0);
        assert_eq!(outbox_event_count(&outbox_dir), 0);
    }

    /// The daemon was down at teardown. The event waits on disk instead of
    /// being lost, which is what the old in-process queue did.
    #[test]
    fn event_survives_until_a_daemon_tick_happens() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let outbox_dir = home.path().join("outbox");
        let path = commit_to_disk(&state_dir, "session-f", "run-f");

        let state = read_state(&path);
        assert_eq!(state.status, "stopped");
        assert!(!state.terminal_published);
        assert!(state.terminal_event.is_some());

        assert_eq!(reconcile_terminal_events(&state_dir, &outbox_dir), 1);
        assert_eq!(outbox_event_count(&outbox_dir), 1);
    }

    /// The committed event must survive a serialization round-trip through the
    /// state file unchanged, since reconciliation republishes it verbatim.
    #[test]
    fn committed_event_round_trips_through_the_state_file() {
        let home = tempfile::tempdir().unwrap();
        let state_dir = home.path().join("state");
        let path = commit_to_disk(&state_dir, "session-g", "run-g");
        let before = read_state(&path).terminal_event.unwrap();
        let after = read_state(&path).terminal_event.unwrap();
        assert_eq!(before, after);
        assert_eq!(before["payload"]["terminal_state"], "session_ended");
    }

    #[test]
    fn terminal_event_carries_reason_and_state() {
        let state = stopped_state("s1", Some("r1"));
        let event = state.terminal_event.unwrap();
        assert_eq!(event["kind"], "terminal_signal");
        assert_eq!(event["payload"]["terminal_state"], "session_ended");
        assert_eq!(event["payload"]["terminal_reason"], "clean_tui_exit");
        assert_eq!(event["session_id"], "s1");
    }
}
