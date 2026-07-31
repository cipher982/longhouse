//! Who legitimately owns a managed Codex bridge, and when it becomes debris.
//!
//! The bridge is a `setsid` daemon: PPID 1, its own process group, no
//! controlling TTY. Nothing in the OS can reap it. Until this module existed
//! the only exit path was an explicit IPC stop sent by the launching CLI from
//! a `finally` block, so any `SIGKILL`, closed terminal pane, dead terminal
//! app, or wrapper crash leaked a bridge — and its `codex app-server` child —
//! permanently. Observed on a dogfood laptop: nine live bridges, the oldest ten
//! days old, accruing about two per day.
//!
//! ## The ownership rule
//!
//! A `tui` bridge is owned by its **launching wrapper** whenever no terminal is
//! attached, and by the **terminal** itself once one is. It is debris only when
//! neither exists.
//!
//! Both halves are load-bearing:
//!
//! - Binding to the wrapper alone is wrong. The wrapper spawns the TUI in its
//!   own foreground process group, so killing the wrapper can leave a live
//!   terminal behind; exiting then would kill a session the user is sitting in.
//! - Binding to the terminal alone is wrong. It would exit during the
//!   documented TUI-crash reattach window, where the wrapper deliberately
//!   outlives a dead TUI to reconnect it.
//!
//! `detached_ui` bridges are headless by design, have no wrapper waiting on
//! them and never a terminal, so they are always owned and end only on an
//! explicit stop.
//!
//! ## Deliberately conservative cases
//!
//! A bridge with no recorded owner identity — started by a CLI predating this
//! flag — is treated as owned forever. We cannot prove such a wrapper died, and
//! killing a session on missing evidence is worse than leaking one. Those are
//! reaped by hand once; every bridge started after this change records an
//! owner.
//!
//! Loss of ownership must also be observed on consecutive checks before acting.
//! A single missed process scan is a transient, not a death, and the penalty
//! for acting on it is terminating live work.

use std::time::Duration;

/// Why a bridge is still legitimately running.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BridgeOwner {
    /// Headless (`detached_ui`) launch: no terminal is expected, ever.
    Headless,
    /// A provider TUI is attached to this bridge right now.
    Terminal,
    /// The launching wrapper is alive and supervising, with no terminal yet.
    Wrapper,
    /// No owner identity was ever recorded; we cannot prove death.
    Unprovable,
}

/// Everything the ownership decision depends on, gathered by the caller.
#[derive(Debug, Clone, Copy)]
pub struct OwnershipInputs {
    /// True when `launch_mode == "detached_ui"`.
    pub headless_launch: bool,
    /// True when a provider TUI holds this bridge's websocket.
    pub terminal_attached: bool,
    /// True when an owner pid was recorded at launch.
    pub owner_recorded: bool,
    /// True when the recorded owner is still running with a matching start
    /// time. Meaningless when `owner_recorded` is false.
    pub owner_alive: bool,
}

/// Decide who owns this bridge, or `None` when it is debris.
pub fn evaluate(inputs: OwnershipInputs) -> Option<BridgeOwner> {
    if inputs.headless_launch {
        return Some(BridgeOwner::Headless);
    }
    if inputs.terminal_attached {
        return Some(BridgeOwner::Terminal);
    }
    if !inputs.owner_recorded {
        return Some(BridgeOwner::Unprovable);
    }
    if inputs.owner_alive {
        return Some(BridgeOwner::Wrapper);
    }
    None
}

/// How often ownership is re-checked.
///
/// Matches the engine's managed-observation cadence so bridge self-exit and
/// the daemon's own view of attachment converge on the same clock.
pub const OWNERSHIP_CHECK_INTERVAL: Duration = Duration::from_secs(5);

/// Consecutive unowned observations required before committing terminal state.
///
/// Three checks at the interval above is roughly fifteen seconds of continuous
/// absence. Long enough that a single failed process scan or a terminal
/// reconnect cannot trigger it, short enough that a closed terminal leaves the
/// timeline promptly.
pub const UNOWNED_OBSERVATIONS_BEFORE_EXIT: u32 = 3;

/// Terminal reason recorded when a bridge exits because nothing owns it.
pub const TERMINAL_REASON_OWNER_GONE: &str = "owner_gone";

/// Debounces ownership loss across consecutive checks.
#[derive(Debug, Default)]
pub struct OwnershipWatch {
    consecutive_unowned: u32,
}

impl OwnershipWatch {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record one observation. Returns true when the bridge should commit
    /// terminal state and exit.
    pub fn observe(&mut self, inputs: OwnershipInputs) -> bool {
        match evaluate(inputs) {
            Some(_) => {
                self.consecutive_unowned = 0;
                false
            }
            None => {
                self.consecutive_unowned = self.consecutive_unowned.saturating_add(1);
                self.consecutive_unowned >= UNOWNED_OBSERVATIONS_BEFORE_EXIT
            }
        }
    }

    #[cfg(test)]
    fn consecutive_unowned(&self) -> u32 {
        self.consecutive_unowned
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tui(terminal_attached: bool, owner_alive: bool) -> OwnershipInputs {
        OwnershipInputs {
            headless_launch: false,
            terminal_attached,
            owner_recorded: true,
            owner_alive,
        }
    }

    #[test]
    fn headless_launch_is_always_owned() {
        let inputs = OwnershipInputs {
            headless_launch: true,
            terminal_attached: false,
            owner_recorded: false,
            owner_alive: false,
        };
        assert_eq!(evaluate(inputs), Some(BridgeOwner::Headless));
    }

    #[test]
    fn attached_terminal_owns_the_bridge_even_without_a_wrapper() {
        // The wrapper spawns the TUI in its own process group, so a killed
        // wrapper can leave a live terminal. That terminal is the session.
        assert_eq!(evaluate(tui(true, false)), Some(BridgeOwner::Terminal));
    }

    #[test]
    fn live_wrapper_owns_the_bridge_across_the_tui_crash_gap() {
        // _run_native_codex_tui_with_recovery deliberately outlives a dead TUI.
        assert_eq!(evaluate(tui(false, true)), Some(BridgeOwner::Wrapper));
    }

    #[test]
    fn no_terminal_and_no_wrapper_is_debris() {
        assert_eq!(evaluate(tui(false, false)), None);
    }

    #[test]
    fn unrecorded_owner_is_never_declared_debris() {
        let inputs = OwnershipInputs {
            headless_launch: false,
            terminal_attached: false,
            owner_recorded: false,
            owner_alive: false,
        };
        assert_eq!(evaluate(inputs), Some(BridgeOwner::Unprovable));
    }

    #[test]
    fn exit_requires_consecutive_unowned_observations() {
        let mut watch = OwnershipWatch::new();
        for _ in 1..UNOWNED_OBSERVATIONS_BEFORE_EXIT {
            assert!(!watch.observe(tui(false, false)));
        }
        assert!(watch.observe(tui(false, false)));
    }

    #[test]
    fn a_single_reappearance_resets_the_debounce() {
        let mut watch = OwnershipWatch::new();
        assert!(!watch.observe(tui(false, false)));
        assert!(!watch.observe(tui(true, false)));
        assert_eq!(watch.consecutive_unowned(), 0);
        // Must start the countdown over rather than exiting on the next tick.
        for _ in 1..UNOWNED_OBSERVATIONS_BEFORE_EXIT {
            assert!(!watch.observe(tui(false, false)));
        }
        assert!(watch.observe(tui(false, false)));
    }

    #[test]
    fn headless_bridges_never_trip_the_watch() {
        let mut watch = OwnershipWatch::new();
        let inputs = OwnershipInputs {
            headless_launch: true,
            terminal_attached: false,
            owner_recorded: true,
            owner_alive: false,
        };
        for _ in 0..(UNOWNED_OBSERVATIONS_BEFORE_EXIT * 3) {
            assert!(!watch.observe(inputs));
        }
    }
}
