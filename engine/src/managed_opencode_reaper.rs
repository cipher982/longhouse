//! Crash backstop for terminal-owned managed-local OpenCode servers.
//!
//! `longhouse opencode` (launch_mode `attached_tui`) stops its backing
//! `opencode serve` process when the attach TUI exits, and installs SIGHUP/
//! SIGTERM cleanup for a closed terminal. But an ungraceful wrapper death —
//! `kill -9`, SSH drop, force-quit — leaves the server running under
//! `setsid()` with no one to reap it. This module is that backstop.
//!
//! The rule is deliberately narrow: only `attached_tui` servers are reapable,
//! and only once their recorded owning wrapper PID is provably gone. We never
//! touch `keep_server` (intentional persistence) or `detached` servers, and we
//! never touch legacy state that lacks the owner-identity fields, because we
//! cannot prove ownership there. PID reuse is defended by comparing the live
//! process start time (`ps -o lstart=`) against the value the wrapper recorded.
//!
//! `decide` is pure. `tick` tracks a grace timer per session and spawns at most
//! one stop task per session, which re-verifies identity immediately before
//! signaling to close the scan→stop TOCTOU window.

use std::collections::HashMap;
use std::collections::HashSet;
use std::time::Duration;

use tokio::time::Instant;

use crate::managed_opencode_scan::OpenCodeServerObservation;
use crate::managed_reaper_core::{ReaperCore, ReaperCoreDecision};
use crate::process_identity::{collect_process_facts_by_pid, lstart_matches_recorded, ProcessFact};

pub const DEFAULT_OPENCODE_REAP_GRACE_SECS: u64 = 120;
const OPENCODE_LAUNCH_MODE_ATTACHED_TUI: &str = "attached_tui";

/// Outcome of the reap decision for a single OpenCode server observation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReapDecision {
    /// Not a candidate. Clear any grace tracking for this session.
    Skip,
    /// Candidate for reap. Start the grace timer if new, or wait.
    Track,
    /// Live attached_tui server whose owning wrapper is gone, past grace.
    Reap,
}

/// Is the owning wrapper for this server still alive AND the same process we
/// recorded? Returns false (wrapper gone) when the PID is absent, or present
/// but with a different start time (PID reuse).
pub fn wrapper_alive(obs: &OpenCodeServerObservation, facts: &HashMap<u32, ProcessFact>) -> bool {
    let Some(pid) = obs.owner_wrapper_pid else {
        return false;
    };
    if pid == 0 {
        return false;
    }
    let Some(fact) = facts.get(&pid) else {
        return false;
    };
    let recorded = obs.owner_wrapper_start_time.trim();
    // If we have a recorded start time, it must match the live process exactly;
    // otherwise the PID has been reused and our wrapper is gone.
    if !lstart_matches_recorded(fact, recorded) {
        return false;
    }
    true
}

/// Pure decision function. No fs/ps side effects.
pub fn decide(
    obs: &OpenCodeServerObservation,
    wrapper_is_alive: bool,
    first_orphaned_at: Option<Instant>,
    now: Instant,
    grace: Duration,
) -> ReapDecision {
    // Only live servers are reapable; dead ones are already omitted from the
    // heartbeat snapshot and need no action.
    if !obs.server_alive {
        return ReapDecision::Skip;
    }
    // Only terminal-owned servers. keep_server/detached are intentional
    // persistence; empty launch_mode is legacy state we cannot prove ownership
    // for. Never reap those.
    if obs.launch_mode.trim() != OPENCODE_LAUNCH_MODE_ATTACHED_TUI {
        return ReapDecision::Skip;
    }
    // We must be able to identify the owning wrapper to prove it died.
    if obs.owner_wrapper_pid.unwrap_or(0) == 0 || obs.owner_wrapper_start_time.trim().is_empty() {
        return ReapDecision::Skip;
    }
    if wrapper_is_alive {
        return ReapDecision::Skip;
    }
    match first_orphaned_at {
        None => ReapDecision::Track,
        Some(first) if now.saturating_duration_since(first) >= grace => ReapDecision::Reap,
        Some(_) => ReapDecision::Track,
    }
}

/// Grace tracking + in-flight guard shared across ticks.
pub struct ManagedOpenCodeReaper {
    grace: Duration,
    core: ReaperCore<()>,
}

impl ManagedOpenCodeReaper {
    pub fn new(grace: Duration) -> Self {
        Self {
            grace,
            core: ReaperCore::new(),
        }
    }

    pub fn from_env() -> Self {
        let secs = std::env::var("LONGHOUSE_MANAGED_OPENCODE_REAP_GRACE_SECS")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(DEFAULT_OPENCODE_REAP_GRACE_SECS);
        Self::new(Duration::from_secs(secs))
    }

    /// Called each local-status tick with fresh observations. Emits reap tasks
    /// onto the tokio runtime; never blocks the caller on stop IPC.
    pub fn tick(&mut self, observations: &[OpenCodeServerObservation]) {
        let now = Instant::now();
        let facts = collect_process_facts_by_pid();
        let seen: Vec<String> = observations.iter().map(|o| o.session_id.clone()).collect();
        self.core.retain_seen(&seen);

        for obs in observations {
            let first = self.core.first_seen(&obs.session_id);
            let alive = wrapper_alive(obs, &facts);
            let decision = match decide(obs, alive, first, now, self.grace) {
                ReapDecision::Skip => ReaperCoreDecision::Skip,
                ReapDecision::Track => ReaperCoreDecision::Track,
                ReapDecision::Reap => ReaperCoreDecision::Act(()),
            };
            if self.core.apply(&obs.session_id, decision, now).is_some() {
                spawn_reap(self.core.in_flight(), obs.clone());
            }
        }
    }

    #[cfg(test)]
    pub fn tracked_count(&self) -> usize {
        self.core.tracked_count()
    }
}

fn spawn_reap(
    in_flight: std::sync::Arc<tokio::sync::Mutex<HashSet<String>>>,
    obs: OpenCodeServerObservation,
) {
    tokio::spawn(async move {
        let session_id = obs.session_id.clone();
        {
            let mut guard = in_flight.lock().await;
            if !guard.insert(session_id.clone()) {
                return; // already in flight
            }
        }
        tracing::info!(
            session_id = %session_id,
            cwd = ?obs.cwd,
            "reaping orphaned opencode server (attached_tui wrapper gone)"
        );
        if preflight_aborts_reap(&obs) {
            tracing::info!(
                session_id = %session_id,
                "preflight aborted opencode reap; conditions changed since scan"
            );
        } else {
            reap_server_pid(&obs);
        }
        let mut guard = in_flight.lock().await;
        guard.remove(&session_id);
    });
}

/// Re-verify immediately before signaling, closing the scan→stop TOCTOU window:
/// abort if the wrapper came back, or if the server PID is no longer the
/// `opencode serve` process we recorded (gone or reused).
fn preflight_aborts_reap(obs: &OpenCodeServerObservation) -> bool {
    let facts = collect_process_facts_by_pid();
    if wrapper_alive(obs, &facts) {
        return true;
    }
    !server_pid_identity_matches(obs, &facts)
}

/// True iff the recorded server PID is live, is an `opencode serve` process, and
/// (when a start time was recorded) matches it — proving it is the process we
/// launched rather than a recycled PID.
fn server_pid_identity_matches(
    obs: &OpenCodeServerObservation,
    facts: &HashMap<u32, ProcessFact>,
) -> bool {
    let Some(pid) = obs.pid else {
        return false;
    };
    let Some(fact) = facts.get(&pid) else {
        return false;
    };
    if !(fact.command.contains("opencode") && fact.command.contains(" serve")) {
        return false;
    }
    let recorded = obs.process_start_time.trim();
    if !lstart_matches_recorded(fact, recorded) {
        return false;
    }
    true
}

#[cfg(unix)]
fn reap_server_pid(obs: &OpenCodeServerObservation) {
    let Some(pid) = obs.pid else {
        return;
    };
    let pid_i = match i32::try_from(pid) {
        Ok(value) if value > 0 => value,
        _ => return,
    };
    // The server is launched with start_new_session=True, so it is its own
    // process-group leader (pgid == pid). Verify that invariant immediately
    // before signaling: if getpgid(pid) != pid, the pid is no longer our
    // session leader (exited and reused, or never a group leader) — skip rather
    // than risk SIGTERM-ing an unrelated process group. No bare-pid fallback.
    unsafe {
        let pgid = libc::getpgid(pid_i);
        if pgid == pid_i {
            libc::killpg(pid_i, libc::SIGTERM);
        }
    }
}

#[cfg(not(unix))]
fn reap_server_pid(_obs: &OpenCodeServerObservation) {}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process_identity::parse_process_fact;
    use std::path::PathBuf;

    fn obs(
        launch_mode: &str,
        wrapper_pid: Option<u32>,
        wrapper_start: &str,
    ) -> OpenCodeServerObservation {
        OpenCodeServerObservation {
            session_id: "sess".to_string(),
            provider_session_id: "provider-sess".to_string(),
            state_file: PathBuf::from("/tmp/sess.json"),
            cwd: Some("/Users/test/git/acme".to_string()),
            server_url: Some("http://127.0.0.1:12345".to_string()),
            pid: Some(4242),
            started_at: "2026-05-05T11:59:00Z".to_string(),
            updated_at: "2026-05-05T12:00:00Z".to_string(),
            server_alive: true,
            launch_mode: launch_mode.to_string(),
            owner_wrapper_pid: wrapper_pid,
            owner_wrapper_start_time: wrapper_start.to_string(),
            process_start_time: "Mon May  5 11:59:00 2026".to_string(),
        }
    }

    fn facts_with_wrapper(pid: u32, lstart: &str) -> HashMap<u32, ProcessFact> {
        let mut m = HashMap::new();
        m.insert(
            pid,
            ProcessFact {
                pid,
                lstart: lstart.to_string(),
                command: "longhouse opencode".to_string(),
                start_time: None,
            },
        );
        m
    }

    #[test]
    fn keep_server_is_never_reaped() {
        let o = obs("keep_server", Some(9000), "Mon May  5 11:58:00 2026");
        // Even with no wrapper alive and grace elapsed.
        let d = decide(
            &o,
            false,
            Some(Instant::now() - Duration::from_secs(999)),
            Instant::now(),
            Duration::from_secs(120),
        );
        assert_eq!(d, ReapDecision::Skip);
    }

    #[test]
    fn detached_is_never_reaped() {
        let o = obs("detached", Some(9000), "Mon May  5 11:58:00 2026");
        let d = decide(&o, false, None, Instant::now(), Duration::from_secs(120));
        assert_eq!(d, ReapDecision::Skip);
    }

    #[test]
    fn legacy_state_without_owner_identity_is_never_reaped() {
        // attached_tui but no recorded owner pid / start -> cannot prove
        // ownership, so skip.
        let no_pid = obs("attached_tui", None, "");
        assert_eq!(
            decide(
                &no_pid,
                false,
                None,
                Instant::now(),
                Duration::from_secs(120)
            ),
            ReapDecision::Skip
        );
        let no_start = obs("attached_tui", Some(9000), "");
        assert_eq!(
            decide(
                &no_start,
                false,
                None,
                Instant::now(),
                Duration::from_secs(120)
            ),
            ReapDecision::Skip
        );
    }

    #[test]
    fn empty_launch_mode_is_never_reaped() {
        let o = obs("", Some(9000), "Mon May  5 11:58:00 2026");
        assert_eq!(
            decide(&o, false, None, Instant::now(), Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn live_wrapper_is_skipped() {
        let o = obs("attached_tui", Some(9000), "Mon May  5 11:58:00 2026");
        assert_eq!(
            decide(&o, true, None, Instant::now(), Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn dead_wrapper_tracks_then_reaps_after_grace() {
        let o = obs("attached_tui", Some(9000), "Mon May  5 11:58:00 2026");
        let now = Instant::now();
        let grace = Duration::from_secs(120);
        // First sighting: track, do not reap.
        assert_eq!(decide(&o, false, None, now, grace), ReapDecision::Track);
        // Within grace: still track.
        assert_eq!(
            decide(&o, false, Some(now), now, grace),
            ReapDecision::Track
        );
        // Past grace: reap.
        let earlier = now - Duration::from_secs(121);
        assert_eq!(
            decide(&o, false, Some(earlier), now, grace),
            ReapDecision::Reap
        );
    }

    #[test]
    fn dead_server_is_skipped() {
        let mut o = obs("attached_tui", Some(9000), "Mon May  5 11:58:00 2026");
        o.server_alive = false;
        assert_eq!(
            decide(&o, false, None, Instant::now(), Duration::from_secs(120)),
            ReapDecision::Skip
        );
    }

    #[test]
    fn wrapper_alive_matches_recorded_start() {
        let o = obs("attached_tui", Some(9000), "Mon May  5 11:58:00 2026");
        let facts = facts_with_wrapper(9000, "Mon May  5 11:58:00 2026");
        assert!(wrapper_alive(&o, &facts));
    }

    #[test]
    fn wrapper_dead_when_pid_absent() {
        let o = obs("attached_tui", Some(9000), "Mon May  5 11:58:00 2026");
        let facts = HashMap::new();
        assert!(!wrapper_alive(&o, &facts));
    }

    #[test]
    fn wrapper_dead_when_pid_reused_with_different_start() {
        // PID present but a different start time -> reused -> wrapper gone.
        let o = obs("attached_tui", Some(9000), "Mon May  5 11:58:00 2026");
        let facts = facts_with_wrapper(9000, "Tue Jun  2 09:00:00 2026");
        assert!(!wrapper_alive(&o, &facts));
    }

    #[test]
    fn parse_process_fact_extracts_pid_lstart_command() {
        let line = "  4242 Mon May  5 11:58:00 2026 opencode serve --hostname 127.0.0.1";
        let (pid, fact) = parse_process_fact(line).unwrap();
        assert_eq!(pid, 4242);
        assert_eq!(fact.lstart, "Mon May  5 11:58:00 2026");
        assert!(fact.command.starts_with("opencode serve"));
    }

    #[test]
    fn tick_tracks_dead_wrapper_without_reaping_immediately() {
        let mut reaper = ManagedOpenCodeReaper::new(Duration::from_secs(120));
        // A server whose wrapper pid is absent from this machine's ps output
        // (pid 1 is launchd / init; command won't match "longhouse opencode").
        let mut o = obs("attached_tui", Some(2), "Definitely Not A Real Lstart 2026");
        o.session_id = "track-me".to_string();
        reaper.tick(std::slice::from_ref(&o));
        // First tick should track (grace not elapsed), not reap.
        assert_eq!(reaper.tracked_count(), 1);
    }
}
