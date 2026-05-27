//! Orphan-bridge reaper for managed-local Codex sessions.
//!
//! The `longhouse codex` wrapper is the only thing that stops a bridge
//! on clean TUI exit. When the wrapper dies ungracefully — SSH drop,
//! Termius backgrounded, kill -9, force-quit — the bridge runs under
//! `setsid()` and survives with no one to reap it. Observed case:
//! bridge alive 13h after SSH-tunneled TUI died, idle, PPID=1.
//!
//! This module owns the backstop. Each local-status tick the daemon
//! calls `ManagedBridgeReaper::tick` with a fresh batch of
//! `CodexBridgeObservation` records. The reaper:
//!
//! 1. Classifies each observation against `decide`:
//!    - `Skip` — bridge is still in use or never used.
//!    - `Track` — candidate for reap; start/keep grace timer.
//!    - `Reap` — Class A, live bridge idle + unattached past grace.
//!    - `StopOrphanAppServer` — Class B, bridge daemon died,
//!      app-server child still listening.
//! 2. Spawns at most one stop task per session (tracked via
//!    `in_flight`), which re-verifies the state immediately before
//!    signaling to close the scan→stop TOCTOU window.
//!
//! Only intra-machine (TUI + bridge on same host) is in scope.
//! Split-machine detection of client presence will be a follow-up; the
//! decision function is expressed in terms of `has_tui_attachment`
//! abstractly so the signal can widen without changing the rule.

use std::collections::HashMap;
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;
use tokio::time::Instant;

use crate::codex_bridge::{self, BridgeStopConfig};
use crate::managed_bridge_scan::{
    self, codex_tui_process_attached, collect_process_commands, pid_alive, CodexBridgeObservation,
};

pub const DEFAULT_REAP_GRACE_SECS: u64 = 120;

/// Outcome of the reap decision for a single observation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReapDecision {
    /// Not a candidate. Clear any grace tracking for this session.
    Skip,
    /// Candidate for reap. Start the grace timer if new, or wait.
    Track,
    /// Class A: live bridge past grace with no TUI and no turn.
    Reap,
    /// Class B: bridge daemon is dead but app-server child is alive.
    StopOrphanAppServer,
}

/// Pure decision function. No fs/ps side effects.
pub fn decide(
    obs: &CodexBridgeObservation,
    first_unattached_at: Option<Instant>,
    now: Instant,
    grace: Duration,
) -> ReapDecision {
    // Invariant applied to both classes: never touch a bridge that was never
    // used. `thread_id` becomes `Some` after TUI attach or detached-UI
    // `thread/start`. Protects no-attach startup races.
    if obs.thread_id.is_none() {
        return ReapDecision::Skip;
    }

    // Class B — dead bridge daemon, orphaned app-server child.
    // No grace: if the bridge daemon exited and no TUI is attached,
    // the app-server is strictly orphaned. This intentionally runs
    // before schema/launch-mode gating: with a dead bridge daemon,
    // there is no live bridge control path to preserve.
    // Process-identity check runs again in the executor right before signaling.
    if !obs.bridge_alive && obs.app_server_alive && !obs.has_tui_attachment {
        return ReapDecision::StopOrphanAppServer;
    }

    if !obs.has_tui_attachment {
        match live_bridge_reap_safety(obs) {
            LiveBridgeReapSafety::TreatAsTuiAttached => {}
            LiveBridgeReapSafety::SkipDetachedUi | LiveBridgeReapSafety::SkipUnknown => {
                return ReapDecision::Skip;
            }
        }
    }

    // Class A — live bridge, unattached, idle.
    let class_a_eligible = obs.bridge_alive
        && !obs.has_tui_attachment
        && obs.active_turn_id.is_none()
        && obs.last_turn_status.as_deref() != Some("inProgress");

    if !class_a_eligible {
        return ReapDecision::Skip;
    }

    match first_unattached_at {
        None => ReapDecision::Track,
        Some(first) if now.saturating_duration_since(first) >= grace => ReapDecision::Reap,
        Some(_) => ReapDecision::Track,
    }
}

fn live_bridge_reap_safety(obs: &CodexBridgeObservation) -> LiveBridgeReapSafety {
    if obs.schema_version > codex_bridge::BRIDGE_STATE_SCHEMA_VERSION {
        return LiveBridgeReapSafety::SkipUnknown;
    }
    launch_mode_reap_safety(obs.launch_mode.as_deref())
}

fn launch_mode_reap_safety(mode: Option<&str>) -> LiveBridgeReapSafety {
    match mode {
        Some(value) if value.eq_ignore_ascii_case(codex_bridge::LAUNCH_MODE_TUI) => {
            LiveBridgeReapSafety::TreatAsTuiAttached
        }
        Some(value) if value.eq_ignore_ascii_case(codex_bridge::LAUNCH_MODE_DETACHED_UI) => {
            LiveBridgeReapSafety::SkipDetachedUi
        }
        Some(value) if value.eq_ignore_ascii_case(codex_bridge::LEGACY_LAUNCH_MODE_HEADLESS) => {
            LiveBridgeReapSafety::SkipDetachedUi
        }
        Some(_) => LiveBridgeReapSafety::SkipUnknown,
        None => LiveBridgeReapSafety::SkipUnknown,
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LiveBridgeReapSafety {
    TreatAsTuiAttached,
    SkipDetachedUi,
    SkipUnknown,
}

/// Grace tracking + in-flight guard shared across ticks.
pub struct ManagedBridgeReaper {
    grace: Duration,
    first_unattached_at: HashMap<String, Instant>,
    in_flight: Arc<Mutex<HashSet<String>>>,
}

impl ManagedBridgeReaper {
    pub fn new(grace: Duration) -> Self {
        Self {
            grace,
            first_unattached_at: HashMap::new(),
            in_flight: Arc::new(Mutex::new(HashSet::new())),
        }
    }

    pub fn from_env() -> Self {
        let secs = std::env::var("LONGHOUSE_MANAGED_REAP_GRACE_SECS")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(DEFAULT_REAP_GRACE_SECS);
        Self::new(Duration::from_secs(secs))
    }

    /// Called each local-status tick with fresh observations. Emits
    /// reap tasks onto the tokio runtime; never blocks the caller on
    /// stop IPC or signal grace.
    pub fn tick(&mut self, observations: &[CodexBridgeObservation]) {
        let now = Instant::now();
        let seen: HashSet<String> = observations.iter().map(|o| o.session_id.clone()).collect();

        // Drop grace entries for sessions that disappeared entirely
        // (state file gone — another cleanup path already handled it).
        self.first_unattached_at
            .retain(|session_id, _| seen.contains(session_id));

        for obs in observations {
            let first = self.first_unattached_at.get(&obs.session_id).copied();
            let decision = decide(obs, first, now, self.grace);
            match decision {
                ReapDecision::Skip => {
                    self.first_unattached_at.remove(&obs.session_id);
                }
                ReapDecision::Track => {
                    self.first_unattached_at
                        .entry(obs.session_id.clone())
                        .or_insert(now);
                }
                ReapDecision::Reap => {
                    self.first_unattached_at.remove(&obs.session_id);
                    spawn_reap(self.in_flight.clone(), obs.clone(), ReapClass::LiveBridge);
                }
                ReapDecision::StopOrphanAppServer => {
                    self.first_unattached_at.remove(&obs.session_id);
                    spawn_reap(
                        self.in_flight.clone(),
                        obs.clone(),
                        ReapClass::OrphanAppServer,
                    );
                }
            }
        }
    }

    #[cfg(test)]
    pub fn tracked_count(&self) -> usize {
        self.first_unattached_at.len()
    }
}

#[derive(Debug, Clone, Copy)]
enum ReapClass {
    LiveBridge,
    OrphanAppServer,
}

fn terminal_disconnected_bridge_stop_config(session_id: String) -> BridgeStopConfig {
    BridgeStopConfig {
        session_id,
        state_root: None,
        terminal_reason: Some("terminal_disconnected".to_string()),
    }
}

fn spawn_reap(
    in_flight: Arc<Mutex<HashSet<String>>>,
    obs: CodexBridgeObservation,
    class: ReapClass,
) {
    tokio::spawn(async move {
        let session_id = obs.session_id.clone();
        {
            let mut guard = in_flight.lock().await;
            if !guard.insert(session_id.clone()) {
                return; // already in flight
            }
        }
        let class_str = match class {
            ReapClass::LiveBridge => "A",
            ReapClass::OrphanAppServer => "B",
        };
        tracing::info!(
            session_id = %session_id,
            cwd = ?obs.cwd,
            class = class_str,
            "reaping orphaned codex bridge"
        );
        if preflight_aborts_reap(&obs, class) {
            tracing::info!(
                session_id = %session_id,
                class = class_str,
                "preflight aborted reap; conditions changed since scan"
            );
        } else {
            match class {
                ReapClass::LiveBridge => {
                    if let Err(err) = codex_bridge::cmd_codex_bridge_stop(
                        terminal_disconnected_bridge_stop_config(session_id.clone()),
                    )
                    .await
                    {
                        tracing::warn!(
                            session_id = %session_id,
                            error = %err,
                            "codex_bridge_stop failed during reap"
                        );
                    }
                }
                ReapClass::OrphanAppServer => {
                    stop_orphan_app_server(&obs).await;
                }
            }
        }
        let mut guard = in_flight.lock().await;
        guard.remove(&session_id);
    });
}

/// Re-verify the conditions that got us to `Reap` immediately before
/// signaling. Closes the scan→stop TOCTOU window.
fn preflight_aborts_reap(obs: &CodexBridgeObservation, class: ReapClass) -> bool {
    let process_commands = collect_process_commands();
    let has_tui = obs
        .ws_url
        .as_deref()
        .is_some_and(|ws| codex_tui_process_attached(&process_commands, ws));
    if has_tui {
        return true;
    }
    // Re-read state to catch late turn/started notifications.
    if let Ok(bytes) = std::fs::read(&obs.state_file) {
        if let Ok(state) = serde_json::from_slice::<crate::codex_bridge::BridgeStateFile>(&bytes) {
            if state.active_turn_id.is_some()
                || state.last_turn_status.as_deref() == Some("inProgress")
            {
                return true;
            }
            if matches!(class, ReapClass::LiveBridge) {
                let reap_safety =
                    if state.schema_version > codex_bridge::BRIDGE_STATE_SCHEMA_VERSION {
                        LiveBridgeReapSafety::SkipUnknown
                    } else {
                        launch_mode_reap_safety(state.launch_mode.as_deref())
                    };
                if reap_safety != LiveBridgeReapSafety::TreatAsTuiAttached {
                    return true;
                }
            }
            if matches!(class, ReapClass::OrphanAppServer) {
                // Bridge came back to life between scan and stop.
                if managed_bridge_scan::bridge_lock_is_held(&obs.state_file) {
                    return true;
                }
            }
        }
    }
    false
}

/// Class-B teardown: bridge daemon is dead, app-server child orphaned.
/// No IPC path; go straight to SIGTERM pgid → 500ms → SIGKILL.
/// Verifies process identity before signaling to defend against pgid
/// reuse by unrelated processes after the bridge died.
#[cfg(unix)]
async fn stop_orphan_app_server(obs: &CodexBridgeObservation) {
    let Some(pid) = obs.app_server_pid.and_then(|p| i32::try_from(p).ok()) else {
        cleanup_sidecars(&obs.state_file);
        return;
    };
    if !pid_alive(pid) {
        cleanup_sidecars(&obs.state_file);
        return;
    }
    if !process_identity_matches(pid) {
        tracing::warn!(
            session_id = %obs.session_id,
            pid,
            "skipping orphan app-server reap; pid no longer belongs to a codex app-server"
        );
        cleanup_sidecars(&obs.state_file);
        return;
    }

    let pgid = obs.app_server_pgid.filter(|p| *p > 0);
    // Only TERM/KILL the pgid if we actually confirmed pid's current
    // pgid matches the recorded one. Otherwise another unrelated
    // process group may have inherited that pgid number and signaling
    // it would take down unrelated work. Track this explicitly.
    // SAFETY: all libc calls below operate on the verified pid and
    // (when permitted) the verified pgid; no raw pointers touched.
    let termed_pgid = unsafe {
        if let Some(pgid) = pgid {
            let live_pgid = libc::getpgid(pid);
            if live_pgid == pgid {
                let _ = libc::killpg(pgid, libc::SIGTERM);
                true
            } else {
                let _ = libc::kill(pid, libc::SIGTERM);
                false
            }
        } else {
            let _ = libc::kill(pid, libc::SIGTERM);
            false
        }
    };
    tokio::time::sleep(Duration::from_millis(500)).await;
    unsafe {
        if termed_pgid {
            if let Some(pgid) = pgid {
                if libc::killpg(pgid, 0) == 0 {
                    let _ = libc::killpg(pgid, libc::SIGKILL);
                }
            }
        }
        if libc::kill(pid, 0) == 0 {
            let _ = libc::kill(pid, libc::SIGKILL);
        }
    }
    cleanup_sidecars(&obs.state_file);
}

#[cfg(not(unix))]
async fn stop_orphan_app_server(_obs: &CodexBridgeObservation) {}

#[cfg(unix)]
fn process_identity_matches(pid: i32) -> bool {
    let Ok(output) = std::process::Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "command="])
        .output()
    else {
        return false;
    };
    if !output.status.success() {
        return false;
    }
    let command = String::from_utf8_lossy(&output.stdout);
    let lower = command.to_ascii_lowercase();
    lower.contains("codex") && lower.contains("app-server")
}

#[cfg(unix)]
fn cleanup_sidecars(state_file: &std::path::Path) {
    for suffix in ["json", "json.tmp", "lock", "sock"] {
        let candidate = if suffix == "json.tmp" {
            state_file.with_extension("json.tmp")
        } else {
            state_file.with_extension(suffix)
        };
        let _ = std::fs::remove_file(candidate);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn base_obs() -> CodexBridgeObservation {
        CodexBridgeObservation {
            session_id: "session-A".to_string(),
            state_file: PathBuf::from("/tmp/session-A.json"),
            schema_version: codex_bridge::BRIDGE_STATE_SCHEMA_VERSION,
            cwd: Some("/tmp".to_string()),
            launch_mode: Some(codex_bridge::LAUNCH_MODE_TUI.to_string()),
            ws_url: Some("ws://127.0.0.1:1111".to_string()),
            status: "ready".to_string(),
            thread_id: Some("thread-1".to_string()),
            thread_path: None,
            active_turn_id: None,
            last_turn_status: Some("completed".to_string()),
            last_error: None,
            thread_subscription_status: Some("subscribed".to_string()),
            bridge_pid: 4241,
            app_server_pid: Some(4242),
            app_server_pgid: Some(4242),
            updated_at: "2026-05-01T00:00:00Z".to_string(),
            bridge_alive: true,
            has_tui_attachment: false,
            app_server_alive: true,
        }
    }

    #[test]
    fn skip_when_thread_id_absent_even_if_idle() {
        let mut obs = base_obs();
        obs.thread_id = None;
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::Skip);
    }

    #[test]
    fn track_then_reap_after_grace() {
        let obs = base_obs();
        let now = Instant::now();
        let grace = Duration::from_secs(120);
        assert_eq!(decide(&obs, None, now, grace), ReapDecision::Track);
        let first = now - Duration::from_secs(30);
        assert_eq!(decide(&obs, Some(first), now, grace), ReapDecision::Track);
        let first = now - Duration::from_secs(130);
        assert_eq!(decide(&obs, Some(first), now, grace), ReapDecision::Reap);
    }

    #[test]
    fn prestarted_tui_bridge_without_attachment_uses_grace_window() {
        let mut obs = base_obs();
        obs.launch_mode = Some(codex_bridge::LAUNCH_MODE_TUI.to_string());
        obs.thread_id = Some("prestarted-thread".to_string());
        obs.has_tui_attachment = false;
        let now = Instant::now();
        let grace = Duration::from_secs(120);

        assert_eq!(decide(&obs, None, now, grace), ReapDecision::Track);
        assert_eq!(
            decide(&obs, Some(now - Duration::from_secs(130)), now, grace),
            ReapDecision::Reap
        );
    }

    #[test]
    fn skip_detached_ui_launch_without_tui_even_after_grace() {
        let mut obs = base_obs();
        obs.launch_mode = Some(codex_bridge::LAUNCH_MODE_DETACHED_UI.to_string());
        let now = Instant::now();
        let first = now - Duration::from_secs(130);

        assert_eq!(
            decide(&obs, Some(first), now, Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn skip_legacy_headless_launch_without_tui_even_after_grace() {
        let mut obs = base_obs();
        obs.launch_mode = Some(codex_bridge::LEGACY_LAUNCH_MODE_HEADLESS.to_string());
        let now = Instant::now();
        let first = now - Duration::from_secs(130);

        assert_eq!(
            decide(&obs, Some(first), now, Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn skip_unknown_launch_mode_without_tui_even_after_grace() {
        let mut obs = base_obs();
        obs.launch_mode = Some("future_detached_ui_v2".to_string());
        let now = Instant::now();
        let first = now - Duration::from_secs(130);

        assert_eq!(
            decide(&obs, Some(first), now, Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn skip_future_schema_without_tui_even_after_grace() {
        let mut obs = base_obs();
        obs.schema_version = codex_bridge::BRIDGE_STATE_SCHEMA_VERSION + 1;
        let now = Instant::now();
        let first = now - Duration::from_secs(130);

        assert_eq!(
            decide(&obs, Some(first), now, Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn skip_missing_launch_mode_without_tui_even_after_grace() {
        let mut obs = base_obs();
        obs.schema_version = 0;
        obs.launch_mode = None;
        let now = Instant::now();
        let first = now - Duration::from_secs(130);

        assert_eq!(
            decide(&obs, Some(first), now, Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn skip_when_tui_attached() {
        let mut obs = base_obs();
        obs.has_tui_attachment = true;
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::Skip);
    }

    #[test]
    fn skip_when_turn_in_progress() {
        let mut obs = base_obs();
        obs.active_turn_id = Some("turn-1".to_string());
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::Skip);

        let mut obs = base_obs();
        obs.last_turn_status = Some("inProgress".to_string());
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::Skip);
    }

    #[test]
    fn class_b_when_bridge_dead_and_app_server_alive() {
        let mut obs = base_obs();
        obs.bridge_alive = false;
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::StopOrphanAppServer);
    }

    #[test]
    fn class_b_skipped_when_never_attached() {
        let mut obs = base_obs();
        obs.bridge_alive = false;
        obs.thread_id = None; // never-attached invariant
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::Skip);
    }

    #[test]
    fn class_b_skipped_when_tui_still_attached() {
        // Shouldn't really happen (dead bridge with TUI) but be safe.
        let mut obs = base_obs();
        obs.bridge_alive = false;
        obs.has_tui_attachment = true;
        let decision = decide(&obs, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(decision, ReapDecision::Skip);
    }

    #[test]
    fn live_bridge_reap_stop_config_uses_terminal_disconnected() {
        let config = terminal_disconnected_bridge_stop_config("session-A".to_string());

        assert_eq!(config.session_id, "session-A");
        assert_eq!(config.state_root, None);
        assert_eq!(
            config.terminal_reason.as_deref(),
            Some("terminal_disconnected")
        );
    }

    #[test]
    fn reaper_tick_tracks_across_calls() {
        let mut reaper = ManagedBridgeReaper::new(Duration::from_secs(120));
        let obs = base_obs();
        reaper.tick(&[obs.clone()]);
        assert_eq!(reaper.tracked_count(), 1);
        // Next tick with TUI back → tracking drops.
        let mut obs2 = obs.clone();
        obs2.has_tui_attachment = true;
        reaper.tick(&[obs2]);
        assert_eq!(reaper.tracked_count(), 0);
    }

    #[test]
    fn reaper_drops_stale_entries_when_session_disappears() {
        let mut reaper = ManagedBridgeReaper::new(Duration::from_secs(120));
        reaper.tick(&[base_obs()]);
        assert_eq!(reaper.tracked_count(), 1);
        reaper.tick(&[]);
        assert_eq!(reaper.tracked_count(), 0);
    }
}
