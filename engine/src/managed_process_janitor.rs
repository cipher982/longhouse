//! Detect managed provider processes whose session no longer exists.
//!
//! [`crate::managed_contract_janitor`] removes the *files* an early teardown
//! leaves behind. This names the surviving *processes*, which is the failure
//! that actually costs the user their machine: on 2026-08-07 the author's laptop
//! held 126 Codex `app-server` processes and 81 `opencode serve` groups, some
//! three days old, because nothing reaps a managed provider whose owner died.
//! Four of the Codex ones were listening with zero established connections and
//! roughly 45 seconds of CPU across those three days. Swap was exhausted
//! (22.4GB of 23.5GB) and the machine became unusable.
//!
//! Provider-neutral by construction. Every managed provider is launched with
//! `LONGHOUSE_MANAGED_SESSION_ID` on its command line, so a single scan covers
//! Codex, OpenCode and anything added later without each provider needing to
//! share a teardown model.
//!
//! **This module reports; it does not kill.** An earlier revision did, and
//! review found four independent ways it could destroy live work:
//!
//! 1. A provider scan that fails resolves to an empty observation vector via
//!    `unwrap_or_default` (`daemon.rs:3350-3363`, `:3444-3460`). `ps` succeeding
//!    proves the process inventory is valid; it proves nothing about whether
//!    the five provider scanners ran. Every live session of a provider whose
//!    scan failed would read as unretained.
//! 2. Resume contracts exist for four providers (`managed_resume_scan.rs:29-54`)
//!    and only Claude's were folded into the retained set, so a live Codex,
//!    Cursor or OpenCode session known solely through its contract looked
//!    orphaned.
//! 3. A legitimately detached Console process has no controlling terminal, so
//!    the "not in a terminal foreground group" test does not protect it
//!    (`claude_print.rs:122-148` spawns exactly that shape).
//! 4. Classification happens in the blocking scan and the kill happened later
//!    in the async consumer. Re-checking only `getpgid(pid) == pid` in between
//!    means pid reuse could send `SIGKILL` to an unrelated new group.
//!
//! Making it safe needs a process-group-aware `ps` format carrying `ppid`,
//! which ripples through all five scanners, plus real scan-completeness
//! plumbing. That is worth doing only if orphans still accumulate now that
//! teardown is fixed at every exit path (`process_group`, the OpenCode monitor
//! branches, and the e2e fixtures). This module exists to answer that question
//! with evidence instead of assumption: it names what it *would* have reaped,
//! and a log line costs nothing if it is wrong.
//!
//! Detection criteria, deliberately narrow even for reporting:
//!
//! - the command names a managed session that is **not** retained;
//! - the session id is a syntactically valid UUID;
//! - the executable is a recognized provider, not merely a command line that
//!   happens to contain the environment variable;
//! - it is older than a grace period, so a launch race is never a candidate;
//! - it is not in a terminal foreground group.
//!
//! The caller must additionally gate on a valid process inventory. Without one
//! every session reads as dead — the same trap the contract sweep documents.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use chrono::{DateTime, Utc};

use crate::process_identity::ProcessFact;

/// Minimum age before a managed process with no retained session is an orphan.
///
/// Matches the contract sweep's grace. A process is launched before its bridge
/// registers, so a young process with no observation yet is normal rather than
/// garbage.
pub const ORPHAN_GRACE: Duration = Duration::from_secs(3600);

/// A managed provider process whose session is gone.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrphanProcess {
    pub pid: u32,
    pub session_id: String,
    /// Truncated for logging; the full command line can be long.
    pub command: String,
}

/// Extract the managed session id a process was launched for.
///
/// Handles both `KEY="value"` (how providers receive it through `-c` config
/// arguments) and bare `KEY=value` (how it appears in a plain environment).
pub fn managed_session_id_of(command: &str) -> Option<String> {
    const KEY: &str = "LONGHOUSE_MANAGED_SESSION_ID=";
    let start = command.find(KEY)? + KEY.len();
    let rest = &command[start..];
    let value = if let Some(stripped) = rest.strip_prefix('"') {
        stripped.split('"').next()?
    } else {
        rest.split(|c: char| c.is_whitespace()).next()?
    };
    let value = value.trim();
    if value.is_empty() {
        return None;
    }
    Some(value.to_string())
}

/// Managed processes whose session is gone.
///
/// Pure, so the policy is testable without spawning anything — and so that if
/// this ever does gate a kill, the predicate is the part under test.
pub fn find_orphan_processes(
    facts: &HashMap<u32, ProcessFact>,
    retained_session_ids: &HashSet<String>,
    now: DateTime<Utc>,
    grace: Duration,
) -> Vec<OrphanProcess> {
    let Ok(grace) = chrono::Duration::from_std(grace) else {
        return Vec::new();
    };
    let mut orphans: Vec<OrphanProcess> = facts
        .values()
        .filter(|fact| {
            // Never touch something in a terminal's foreground group: the user
            // is looking at it, whatever our bookkeeping believes.
            !fact.stat.contains('+')
        })
        .filter_map(|fact| {
            let session_id = managed_session_id_of(&fact.command)?;
            // A command line merely containing the variable is not evidence of
            // a managed provider. Require both a real session id and a
            // recognized provider executable, or a user's own shell that
            // exported the variable would qualify.
            if uuid::Uuid::parse_str(&session_id).is_err() {
                return None;
            }
            if crate::unmanaged_bindings::is_provider_process(&fact.command).is_none() {
                return None;
            }
            if retained_session_ids.contains(&session_id) {
                return None;
            }
            // Unknown start time means we cannot prove it survived the grace
            // period, so leave it alone.
            let started = fact.start_time?;
            if now.signed_duration_since(started) < grace {
                return None;
            }
            Some(OrphanProcess {
                pid: fact.pid,
                session_id,
                command: fact.command.chars().take(120).collect(),
            })
        })
        .collect();
    // Deterministic order so logs and tests do not depend on HashMap iteration.
    orphans.sort_by_key(|orphan| orphan.pid);
    orphans
}

/// Record what a reaper would have stopped, without stopping anything.
///
/// Deliberately not a kill. See the module docs: the retained set cannot
/// currently be trusted to mean "no live session", because a provider scan that
/// fails is indistinguishable from a provider with no sessions. Reporting turns
/// that uncertainty into evidence; killing would turn it into data loss.
///
/// If these lines appear on a machine where teardown is working, that is the
/// signal to build the real reaper — and the log says exactly which processes
/// and which sessions to build it against.
pub fn report_orphan_processes(orphans: &[OrphanProcess]) {
    for orphan in orphans {
        let leads_group = crate::process_group::leader_group_for(orphan.pid).is_some();
        tracing::warn!(
            pid = orphan.pid,
            session_id = %orphan.session_id,
            leads_own_group = leads_group,
            command = %orphan.command,
            "managed provider process outlived its session; not reaped \
             (detection only — see managed_process_janitor docs)"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fact(pid: u32, command: &str, stat: &str, age: chrono::Duration) -> ProcessFact {
        ProcessFact {
            pid,
            tty: "??".to_string(),
            stat: stat.to_string(),
            lstart: String::new(),
            command: command.to_string(),
            start_time: Some(Utc::now() - age),
        }
    }

    fn inventory(facts: Vec<ProcessFact>) -> HashMap<u32, ProcessFact> {
        facts.into_iter().map(|fact| (fact.pid, fact)).collect()
    }

    #[test]
    fn parses_quoted_and_bare_session_ids() {
        assert_eq!(
            managed_session_id_of(
                r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="abc-123" app-server"#
            ),
            Some("abc-123".to_string())
        );
        assert_eq!(
            managed_session_id_of("LONGHOUSE_MANAGED_SESSION_ID=abc-123 opencode serve"),
            Some("abc-123".to_string())
        );
        assert_eq!(managed_session_id_of("opencode serve"), None);
        assert_eq!(managed_session_id_of("LONGHOUSE_MANAGED_SESSION_ID="), None);
    }

    const DEAD: &str = "11111111-1111-4111-8111-111111111111";
    const LIVE: &str = "22222222-2222-4222-8222-222222222222";
    const YOUNG: &str = "33333333-3333-4333-8333-333333333333";

    fn codex(session_id: &str) -> String {
        format!(r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="{session_id}" app-server"#)
    }

    #[test]
    fn finds_only_unretained_processes_past_the_grace_period() {
        let facts = inventory(vec![
            // Orphan: old, unretained, detached.
            fact(101, &codex(DEAD), "S", chrono::Duration::hours(3)),
            // Retained session — still live.
            fact(102, &codex(LIVE), "S", chrono::Duration::hours(3)),
            // Too young: still inside the launch race.
            fact(103, &codex(YOUNG), "S", chrono::Duration::minutes(2)),
            // Not a managed process at all.
            fact(104, "vim notes.md", "S", chrono::Duration::hours(3)),
        ]);
        let retained = HashSet::from([LIVE.to_string()]);

        let orphans = find_orphan_processes(&facts, &retained, Utc::now(), ORPHAN_GRACE);

        assert_eq!(orphans.len(), 1, "unexpected orphans: {orphans:?}");
        assert_eq!(orphans[0].pid, 101);
        assert_eq!(orphans[0].session_id, DEAD);
    }

    #[test]
    fn a_command_line_mentioning_the_variable_is_not_enough() {
        // Two ways a non-provider can carry the string: a user's own shell that
        // exported it, and a provider-shaped command whose id is not a session.
        // Neither is evidence that Longhouse launched anything.
        let facts = inventory(vec![
            fact(
                201,
                &format!(r#"zsh -lc export LONGHOUSE_MANAGED_SESSION_ID={DEAD}"#),
                "S",
                chrono::Duration::hours(9),
            ),
            fact(
                202,
                r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="not-a-uuid" app-server"#,
                "S",
                chrono::Duration::hours(9),
            ),
        ]);

        let orphans = find_orphan_processes(&facts, &HashSet::new(), Utc::now(), ORPHAN_GRACE);

        assert!(
            orphans.is_empty(),
            "matched something that is not a managed provider: {orphans:?}"
        );
    }

    #[test]
    fn never_reaps_a_foreground_terminal_session() {
        // `+` means the process sits in its terminal's foreground group: the
        // user is interacting with it right now. Bookkeeping that says the
        // session is dead is the thing that is wrong, not the process.
        let facts = inventory(vec![fact(
            201,
            &codex(DEAD),
            "S+",
            chrono::Duration::hours(5),
        )]);

        let orphans = find_orphan_processes(&facts, &HashSet::new(), Utc::now(), ORPHAN_GRACE);

        assert!(
            orphans.is_empty(),
            "reaped a foreground session: {orphans:?}"
        );
    }

    #[test]
    fn skips_processes_with_unknown_start_time() {
        let mut stale = fact(301, &codex(DEAD), "S", chrono::Duration::hours(5));
        stale.start_time = None;
        let facts = inventory(vec![stale]);

        let orphans = find_orphan_processes(&facts, &HashSet::new(), Utc::now(), ORPHAN_GRACE);

        assert!(
            orphans.is_empty(),
            "reaped without proving age: {orphans:?}"
        );
    }
}
