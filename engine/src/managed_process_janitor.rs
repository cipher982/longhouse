//! Sweep managed provider processes whose session no longer exists.
//!
//! [`crate::managed_contract_janitor`] removes the *files* an early teardown
//! leaves behind. This removes the *processes*, which is the failure that
//! actually costs the user their machine: on 2026-08-07 the author's laptop
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
//! Killing processes is less reversible than deleting files, so the criteria
//! here are deliberately narrower than the contract sweep's:
//!
//! - the process names a managed session that is **not** retained;
//! - it is older than a grace period, so a launch race is never a candidate;
//! - it is not attached to a terminal foreground group, so a session the user
//!   is sitting in front of is never a candidate even if bookkeeping says it is
//!   dead;
//! - it leads its own process group, so the signal cannot reach work we do not
//!   own.
//!
//! The caller must additionally gate on a valid process inventory. Without one
//! every session reads as dead and this would reap the entire running estate —
//! the same trap the contract sweep documents.

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

/// Managed processes eligible to be reaped.
///
/// Pure so the policy can be tested without spawning anything; the caller does
/// the killing.
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

/// Stop orphaned managed processes. Returns how many are confirmed gone.
///
/// Only group leaders are signalled. A managed provider that is not its own
/// group leader was started in a way this module does not own, and reaching
/// into another group could kill the user's unrelated work.
pub async fn reap_orphan_processes(orphans: &[OrphanProcess]) -> usize {
    let mut reaped = 0usize;
    for orphan in orphans {
        let Some(pgid) = crate::process_group::leader_group_for(orphan.pid) else {
            tracing::debug!(
                pid = orphan.pid,
                session_id = %orphan.session_id,
                "skipped orphaned managed process that does not lead its group"
            );
            continue;
        };
        let outcome =
            crate::process_group::shutdown_group(pgid, crate::process_group::DEFAULT_GRACE).await;
        if outcome.is_gone() {
            reaped += 1;
            tracing::info!(
                pid = orphan.pid,
                session_id = %orphan.session_id,
                outcome = outcome.as_str(),
                command = %orphan.command,
                "reaped orphaned managed provider process"
            );
        } else {
            tracing::warn!(
                pid = orphan.pid,
                session_id = %orphan.session_id,
                "orphaned managed provider process survived SIGKILL"
            );
        }
    }
    reaped
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

    #[test]
    fn finds_only_unretained_processes_past_the_grace_period() {
        let facts = inventory(vec![
            // Orphan: old, unretained, detached.
            fact(
                101,
                r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="dead" app-server"#,
                "S",
                chrono::Duration::hours(3),
            ),
            // Retained session — still live.
            fact(
                102,
                r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="live" app-server"#,
                "S",
                chrono::Duration::hours(3),
            ),
            // Too young: still inside the launch race.
            fact(
                103,
                r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="young" app-server"#,
                "S",
                chrono::Duration::minutes(2),
            ),
            // Not a managed process at all.
            fact(104, "vim notes.md", "S", chrono::Duration::hours(3)),
        ]);
        let retained = HashSet::from(["live".to_string()]);

        let orphans = find_orphan_processes(&facts, &retained, Utc::now(), ORPHAN_GRACE);

        assert_eq!(orphans.len(), 1, "unexpected orphans: {orphans:?}");
        assert_eq!(orphans[0].pid, 101);
        assert_eq!(orphans[0].session_id, "dead");
    }

    #[test]
    fn never_reaps_a_foreground_terminal_session() {
        // `+` means the process sits in its terminal's foreground group: the
        // user is interacting with it right now. Bookkeeping that says the
        // session is dead is the thing that is wrong, not the process.
        let facts = inventory(vec![fact(
            201,
            r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="dead" app-server"#,
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
        let mut stale = fact(
            301,
            r#"codex -c env.LONGHOUSE_MANAGED_SESSION_ID="dead" app-server"#,
            "S",
            chrono::Duration::hours(5),
        );
        stale.start_time = None;
        let facts = inventory(vec![stale]);

        let orphans = find_orphan_processes(&facts, &HashSet::new(), Utc::now(), ORPHAN_GRACE);

        assert!(
            orphans.is_empty(),
            "reaped without proving age: {orphans:?}"
        );
    }
}
