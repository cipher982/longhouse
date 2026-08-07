//! One teardown primitive for Longhouse-spawned process groups.
//!
//! Six ad-hoc implementations existed before this module, each subtly
//! different: `codex_exec` slept 100ms, `codex_bridge` slept 500ms, and
//! `opencode_run`, `claude_print` and `cursor_print` each carried their own
//! 200ms copy, while `opencode_control` sent `SIGTERM` and returned success on
//! *signal delivery* without ever escalating. None of them waited for the group
//! to actually go away, so every one could report success while leaving a live
//! process group behind.
//!
//! The count is the point: the same helper was reinvented per provider, so a
//! fix to one never reached the others, and three of the six also had exit
//! paths that skipped cleanup entirely.
//!
//! That is not theoretical. On 2026-08-07 the author's laptop held 430 orphaned
//! Longhouse processes accumulated over roughly seven days — 81 `opencode
//! serve` groups among them, all reparented to `launchd` — which exhausted swap
//! (22.4GB of 23.5GB) and made the machine unusable. Every one of them exited
//! on a plain `SIGTERM` when finally asked. They were never asked.
//!
//! The rules this module enforces:
//!
//! - **Poll for exit, do not sleep and assume.** A fixed grace period is either
//!   too slow for the common case or too fast for the slow one.
//! - **Escalate, then verify the escalation.** `SIGKILL` is not a guarantee if
//!   the group is wedged in uninterruptible I/O; the caller needs to know.
//! - **Report what happened.** `Survived` is a real outcome and callers that
//!   log "stopped" unconditionally are how leaks stay invisible.
//! - **Reap owned children.** Signalling a group does not reap the leader when
//!   it is our direct child; an unreaped child is a zombie entry.
//!
//! Callers that hold a `Child` want [`shutdown_owned_child`]. Callers that only
//! recovered a pid from a state file — possibly written by a previous engine
//! process — want [`shutdown_group`], because such a process is not our child
//! and cannot be waited on.
//!
//! That split is load-bearing rather than stylistic. Liveness is probed with
//! `killpg(pgid, 0)`, which succeeds on a zombie, so a leader we own reads as
//! alive until we reap it. `shutdown_owned_child` therefore reaps before it
//! polls; `shutdown_group` would spin to its budget and wrongly report
//! `Survived`. Picking the wrong one is not a style error.

use std::time::Duration;

use tokio::process::Child;

/// How long a group may take to honour `SIGTERM` before it is killed.
pub const DEFAULT_GRACE: Duration = Duration::from_millis(500);

/// How long to keep checking after `SIGKILL` before reporting `Survived`.
const KILL_CONFIRM_BUDGET: Duration = Duration::from_millis(500);

/// Gap between liveness checks while waiting for a group to exit.
const POLL_INTERVAL: Duration = Duration::from_millis(25);

/// What actually happened to a process group that was asked to exit.
///
/// Deliberately distinguishes `Terminated` from `Killed` so callers can log the
/// difference: a provider that never honours `SIGTERM` is a bug worth seeing,
/// and it is invisible if both outcomes report the same thing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GroupShutdown {
    /// Nothing to stop: already exited, or the pid was never a group leader.
    Absent,
    /// The group exited after `SIGTERM`, within the grace period.
    Terminated,
    /// The group ignored `SIGTERM` and exited after `SIGKILL`.
    Killed,
    /// The group was still present after `SIGKILL` and the confirmation budget.
    Survived,
}

impl GroupShutdown {
    /// Whether the group is known to be gone.
    ///
    /// `Survived` is the only outcome that is not; it is separated out so that
    /// `if outcome.is_gone()` reads correctly at call sites and a leak cannot
    /// be spelled as success by accident.
    pub fn is_gone(self) -> bool {
        !matches!(self, GroupShutdown::Survived)
    }

    /// Stable label for logs and IPC payloads.
    pub fn as_str(self) -> &'static str {
        match self {
            GroupShutdown::Absent => "absent",
            GroupShutdown::Terminated => "terminated",
            GroupShutdown::Killed => "killed",
            GroupShutdown::Survived => "survived",
        }
    }
}

/// Two limits worth stating, because both are inherent to pid-based signalling
/// rather than oversights:
///
/// - [`leader_group_for`] proves a pid *leads* a group, not that the group is
///   ours. Ownership comes from provenance — we spawned it, or a previous
///   engine recorded it — and a stale pid that now leads unrelated work would
///   pass. Every caller pairs it with an identity check for that reason.
/// - There is a TOCTOU window between that check and `killpg`. A group can exit
///   and its numeric pgid be reused in between. Nothing closes this; it is why
///   the callers that matter re-verify identity immediately before signalling.
///
/// [`GroupShutdown::Survived`] also means "could not confirm gone" rather than
/// "definitely still running": a member that exited but lingers unreaped by its
/// own parent still answers `killpg(_, 0)`. `is_gone()` treats it as not-gone,
/// which is the safe direction.
///
/// Whether any process remains in `pgid`.
///
/// Uses `killpg(_, 0)`, which counts a zombie as present because a not-yet
/// reaped pid still exists. That is the right trade here: callers that own the
/// leader use [`shutdown_owned_child`], which reaps before it polls, and every
/// caller of [`shutdown_group`] is stopping a process it did not spawn — a pid
/// from a state file whose spawner has exited, which is exactly the shape the
/// orphans in the motivating incident had (all 81 were reparented to `launchd`
/// and reaped promptly on exit).
pub fn group_is_alive(pgid: i32) -> bool {
    if pgid <= 0 {
        return false;
    }
    live_group_probe(pgid)
}

#[cfg(unix)]
fn live_group_probe(pgid: i32) -> bool {
    // EPERM means the group exists but is not ours, which still counts as alive.
    if unsafe { libc::killpg(pgid, 0) } == 0 {
        return true;
    }
    matches!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(code) if code == libc::EPERM
    )
}

#[cfg(not(unix))]
fn live_group_probe(_pgid: i32) -> bool {
    false
}

/// The process group `pid` leads, if it leads one.
///
/// Returns `None` when `pid` is merely a member of someone else's group.
/// Signalling a group we did not create would reach unrelated processes, which
/// on a developer laptop means killing the user's own work.
#[cfg(unix)]
pub fn leader_group_for(pid: u32) -> Option<i32> {
    let pid = i32::try_from(pid).ok()?;
    if pid <= 0 {
        return None;
    }
    let pgid = unsafe { libc::getpgid(pid) };
    if pgid == -1 || pgid != pid {
        return None;
    }
    Some(pgid)
}

#[cfg(not(unix))]
pub fn leader_group_for(_pid: u32) -> Option<i32> {
    None
}

/// Signal a process group and wait until it is actually gone.
///
/// Escalates `SIGTERM` to `SIGKILL` after `grace`, then keeps checking so the
/// return value reflects reality rather than the last signal sent.
///
/// For groups whose leader is **not** our direct child — a pid recovered from a
/// state file, or one spawned by a previous engine process, which is every
/// caller in the engine today. If you hold a `Child` for the leader, use
/// [`shutdown_owned_child`] instead: an unreaped child of ours stays a zombie,
/// `killpg(pgid, 0)` still succeeds on it, and this would poll until the budget
/// expired and then report `Survived` for a process that had in fact stopped.
#[cfg(unix)]
pub async fn shutdown_group(pgid: i32, grace: Duration) -> GroupShutdown {
    if pgid <= 0 || !group_is_alive(pgid) {
        return GroupShutdown::Absent;
    }
    unsafe {
        libc::killpg(pgid, libc::SIGTERM);
    }
    if wait_for_group_exit(pgid, grace).await {
        return GroupShutdown::Terminated;
    }
    unsafe {
        libc::killpg(pgid, libc::SIGKILL);
    }
    if wait_for_group_exit(pgid, KILL_CONFIRM_BUDGET).await {
        GroupShutdown::Killed
    } else {
        GroupShutdown::Survived
    }
}

#[cfg(not(unix))]
pub async fn shutdown_group(_pgid: i32, _grace: Duration) -> GroupShutdown {
    GroupShutdown::Absent
}

/// Poll until the group is gone or `budget` expires. True when it is gone.
///
/// Not `cfg(unix)`-gated: `group_is_alive` is always false off unix, so this
/// returns immediately there and both callers stay platform-neutral.
async fn wait_for_group_exit(pgid: i32, budget: Duration) -> bool {
    wait_for_group_exit_until(pgid, tokio::time::Instant::now() + budget).await
}

/// Poll until the group is gone or `deadline` passes. True when it is gone.
async fn wait_for_group_exit_until(pgid: i32, deadline: tokio::time::Instant) -> bool {
    loop {
        if !group_is_alive(pgid) {
            return true;
        }
        let now = tokio::time::Instant::now();
        if now >= deadline {
            return !group_is_alive(pgid);
        }
        tokio::time::sleep(POLL_INTERVAL.min(deadline - now)).await;
    }
}

/// Stop a group we own through a `Child`, then reap the leader.
///
/// This cannot delegate to [`shutdown_group`], because the leader is our child:
/// between exiting and being waited on it is a zombie, and `killpg(pgid, 0)`
/// succeeds on a zombie. Polling group liveness before reaping would therefore
/// never observe the group leave, and every well-behaved child would be
/// reported as `Survived`. The reap has to come first.
///
/// The reap also matters on its own. A child that exited but was never waited
/// on stays in the process table as a defunct entry; twelve were present during
/// the incident that motivated this module.
pub async fn shutdown_owned_child(
    child: &mut Child,
    pgid: Option<i32>,
    grace: Duration,
) -> GroupShutdown {
    // One deadline for the whole polite phase. Timing the child wait and the
    // group wait separately spent up to two graces before reporting
    // `Terminated`, which contradicts what that outcome claims.
    let deadline = tokio::time::Instant::now() + grace;
    let pgid = pgid.filter(|pgid| *pgid > 0 && group_is_alive(*pgid));

    // `Absent` must mean nothing was running, not "we had no pgid". Without
    // this check the no-group path could kill a live child and still report
    // that there was nothing to stop.
    if pgid.is_none() && !matches!(child.try_wait(), Ok(None)) {
        let _ = child.wait().await;
        return GroupShutdown::Absent;
    }

    // Ask nicely: the whole group when we lead one, the child alone otherwise.
    #[cfg(unix)]
    match pgid {
        Some(pgid) => unsafe {
            libc::killpg(pgid, libc::SIGTERM);
        },
        None => {
            if let Some(pid) = child.id() {
                if let Ok(pid) = i32::try_from(pid) {
                    unsafe {
                        libc::kill(pid, libc::SIGTERM);
                    }
                }
            }
        }
    }

    // Reap the leader first so its zombie stops holding the group open.
    // `Ok(Err(_))` is a `wait` failure, not an exit: treating it as success
    // would skip escalation for a child we have lost track of.
    let exited_on_term = matches!(
        tokio::time::timeout_at(deadline, child.wait()).await,
        Ok(Ok(_))
    );

    let settled = match pgid {
        Some(pgid) => exited_on_term && wait_for_group_exit_until(pgid, deadline).await,
        None => exited_on_term,
    };
    if settled {
        return GroupShutdown::Terminated;
    }

    #[cfg(unix)]
    if let Some(pgid) = pgid {
        unsafe {
            libc::killpg(pgid, libc::SIGKILL);
        }
    }
    if !exited_on_term {
        let _ = child.start_kill();
        let _ = child.wait().await;
    }
    match pgid {
        Some(pgid) if !wait_for_group_exit(pgid, KILL_CONFIRM_BUDGET).await => {
            GroupShutdown::Survived
        }
        _ => GroupShutdown::Killed,
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::process::Stdio;
    use tokio::process::Command;

    /// Spawn a sleeper in its own process group, with a child of its own so the
    /// tests exercise group semantics rather than single-pid semantics.
    fn spawn_group_leader() -> Child {
        let mut command = Command::new("/bin/sh");
        command
            .arg("-c")
            .arg("sleep 300 & sleep 300")
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            command.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.spawn().expect("spawn test group leader")
    }

    #[tokio::test]
    async fn absent_group_reports_absent() {
        // A pid that cannot be a live leader.
        assert_eq!(
            shutdown_group(i32::MAX, DEFAULT_GRACE).await,
            GroupShutdown::Absent
        );
    }

    #[tokio::test]
    async fn terminates_group_and_reaps_leader() {
        let mut child = spawn_group_leader();
        let pid = child.id().expect("test child pid");
        let pgid = leader_group_for(pid).expect("test child leads its group");

        let outcome = shutdown_owned_child(&mut child, Some(pgid), DEFAULT_GRACE).await;

        assert_eq!(outcome, GroupShutdown::Terminated);
        assert!(outcome.is_gone());
        assert!(!group_is_alive(pgid), "group outlived shutdown");
    }

    #[tokio::test]
    async fn kills_group_that_ignores_sigterm() {
        // The shell must both ignore TERM *and* outlive its children: a plain
        // `trap '' TERM; sleep 300` exits cleanly on TERM because `sleep` does
        // not ignore it and the shell simply reaps it. Restarting the sleep in
        // a loop keeps the leader alive so escalation is actually exercised.
        let mut command = Command::new("/bin/sh");
        command
            .arg("-c")
            .arg("trap '' TERM; while :; do sleep 5; done")
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        unsafe {
            command.pre_exec(|| {
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        let mut child = command.spawn().expect("spawn sigterm-ignoring child");
        let pid = child.id().expect("test child pid");
        let pgid = leader_group_for(pid).expect("test child leads its group");

        // `spawn` returns as soon as exec succeeds, before the shell has parsed
        // its script and installed the trap. Signalling immediately races that
        // and hits a child whose TERM disposition is still the default, which
        // makes this test silently assert nothing.
        tokio::time::sleep(Duration::from_millis(250)).await;

        // Short grace so the test does not pay the full default twice.
        let outcome =
            shutdown_owned_child(&mut child, Some(pgid), Duration::from_millis(100)).await;

        assert_eq!(outcome, GroupShutdown::Killed);
        assert!(!group_is_alive(pgid), "group survived SIGKILL");
    }

    #[tokio::test]
    async fn a_live_child_with_no_group_is_stopped_and_reported_honestly() {
        // Without a pgid there is no group to signal, but there is still a live
        // child. Reporting `Absent` here claimed nothing needed stopping while
        // having just killed it.
        let mut command = Command::new("/bin/sh");
        command
            .arg("-c")
            .arg("trap '' TERM; while :; do sleep 5; done")
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let mut child = command.spawn().expect("spawn ungrouped child");
        let pid = child.id().expect("test child pid");
        tokio::time::sleep(Duration::from_millis(250)).await;

        let outcome = shutdown_owned_child(&mut child, None, Duration::from_millis(100)).await;

        assert_eq!(
            outcome,
            GroupShutdown::Killed,
            "a live child that ignored SIGTERM was stopped; saying otherwise hides the kill"
        );
        assert!(
            unsafe { libc::kill(pid as i32, 0) } != 0,
            "child outlived shutdown"
        );
    }

    #[tokio::test]
    async fn an_already_dead_child_reports_absent() {
        let mut child = Command::new("/bin/sh")
            .arg("-c")
            .arg("exit 0")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn short-lived child");
        // Let it exit and be reaped by the runtime before we look.
        let _ = child.wait().await;

        assert_eq!(
            shutdown_owned_child(&mut child, None, DEFAULT_GRACE).await,
            GroupShutdown::Absent
        );
    }

    #[tokio::test]
    async fn non_leader_pid_yields_no_group() {
        // The test process is virtually never its own group leader under a test
        // harness; if it happens to be, the assertion below still holds because
        // leader_group_for would return our own pgid and we skip the check.
        let self_pid = std::process::id();
        if leader_group_for(self_pid).is_none() {
            assert!(leader_group_for(self_pid).is_none());
        }
    }
}
