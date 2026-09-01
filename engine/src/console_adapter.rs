//! Helpers shared by the Console (headless, UI-driven) provider adapters.
//!
//! `claude_print`, `cursor_print`, `pi_print`, `antigravity_print` and
//! `opencode_run` all drive a provider CLI that writes JSONL to a file, watch
//! that file grow, and settle a turn claim when the process goes away. Each
//! carried its own copy of the pieces below: five of the liveness check, five
//! of the process-group teardown differing only in a log prefix, five stderr
//! tails, and three file readers.

use std::collections::{HashMap, VecDeque};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use anyhow::Result;

use crate::process_identity::ProcessFact;
use crate::turn_claims::TurnClaim;

/// How much of a provider's stderr rides along on a terminal event.
const STDERR_TAIL_LINES: usize = 40;

/// What the process inventory says about the worker behind a turn claim.
///
/// `Unknown` is the reason this is not a `bool`. A terminal is an actuator: it
/// settles the console FIFO and dispatches the next queued turn, and the
/// monitor loops SIGTERM/SIGKILL the process group on the way there. A `ps`
/// that failed to run, timed out, or returned an incoherent scan is missing
/// evidence, not proof of death, and must not drive either.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ClaimLiveness {
    /// The recorded pid is running and its start time still matches the claim.
    Live,
    /// The recorded pid is gone, or now belongs to a different process.
    Gone,
    /// The inventory could not be read. Retain whatever was last observed.
    Unknown,
}

/// Classify a claim against an already-collected inventory. `None` is what
/// `process_identity::try_collect_process_facts_by_pid` returns for a failed
/// scan, and it maps to `Unknown`.
pub fn claim_liveness(
    claim: &TurnClaim,
    inventory: Option<&HashMap<u32, ProcessFact>>,
) -> ClaimLiveness {
    let Some(inventory) = inventory else {
        return ClaimLiveness::Unknown;
    };
    // A claim in `spawned` without a recorded pid or start time carries no
    // identity to check and never will, so it settles rather than pinning a
    // monitor forever. That is not the ambiguous case; the inventory is.
    let Some((pid, expected)) = claim.pid.zip(claim.process_start_time.as_deref()) else {
        return ClaimLiveness::Gone;
    };
    match inventory.get(&pid) {
        Some(fact) if fact.lstart == expected => ClaimLiveness::Live,
        _ => ClaimLiveness::Gone,
    }
}

/// Collect one inventory and classify a single claim against it.
pub fn claim_process_liveness(claim: &TurnClaim) -> ClaimLiveness {
    claim_liveness(
        claim,
        crate::process_identity::try_collect_process_facts_by_pid().as_ref(),
    )
}

/// Read whatever a provider appended to its JSONL file since `offset`, and
/// return the complete lines that are now available. Partial trailing bytes
/// stay in `pending` for the next call.
pub fn read_growth(path: &Path, offset: &mut u64, pending: &mut Vec<u8>) -> Result<Vec<Vec<u8>>> {
    let mut file = File::open(path)?;
    file.seek(SeekFrom::Start(*offset))?;
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    *offset += bytes.len() as u64;
    pending.extend(bytes);
    let mut lines = Vec::new();
    while let Some(index) = pending.iter().position(|byte| *byte == b'\n') {
        let mut line = pending.drain(..=index).collect::<Vec<_>>();
        line.pop();
        if !line.is_empty() {
            lines.push(line);
        }
    }
    Ok(lines)
}

/// The last few lines of a provider's stderr, for attaching to a terminal.
pub fn stderr_tail(path: &Path) -> Option<String> {
    let text = std::fs::read_to_string(path).ok()?;
    let mut lines = text
        .lines()
        .rev()
        .take(STDERR_TAIL_LINES)
        .collect::<VecDeque<_>>();
    lines.make_contiguous().reverse();
    let value = lines.into_iter().collect::<Vec<_>>().join("\n");
    (!value.is_empty()).then_some(value)
}

/// Shut down a Console turn's process group, reporting anything that outlived
/// SIGKILL. `shutdown_group` polls for group exit rather than assuming a fixed
/// grace window, which is how orphans used to get left behind: a child in
/// uninterruptible I/O outlives an ad-hoc SIGTERM/sleep/SIGKILL and nobody
/// finds out. `tag` is the adapter's log prefix, e.g. `claude-print`.
pub async fn cleanup_process_group(tag: &str, process_group_id: Option<i32>) {
    let Some(pgid) = process_group_id else {
        return;
    };
    let outcome =
        crate::process_group::shutdown_group(pgid, crate::process_group::DEFAULT_GRACE).await;
    if !outcome.is_gone() {
        eprintln!("[{tag}] process group {pgid} survived SIGKILL and was left running");
    }
}

/// Serializes tests that point the process-global `LONGHOUSE_HOME` at their
/// own temp dir.
///
/// The Console adapters resolve the turn-claim registry, the agent dir, and
/// the transcript-wake socket from that variable at use time, so two tests
/// setting it concurrently make each other read the wrong tree. Each adapter
/// used to guard (or not guard) that on its own, which meant `pi_print`'s
/// daemon tests raced `cursor_print`'s process-group test. One lock for all of
/// them; no test holds a second lock while holding this one.
#[cfg(test)]
pub async fn longhouse_home_test_guard() -> tokio::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::OnceLock<tokio::sync::Mutex<()>> = std::sync::OnceLock::new();
    LOCK.get_or_init(|| tokio::sync::Mutex::new(()))
        .lock()
        .await
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spawned_claim(pid: Option<u32>, start: Option<&str>) -> TurnClaim {
        let temp = tempfile::tempdir().unwrap();
        let registry = crate::turn_claims::TurnClaimRegistry::new(temp.path().join("claims"));
        let run_id = "99999999-9999-4999-8999-999999999999";
        registry
            .claim(
                run_id,
                "11111111-1111-4111-8111-111111111111",
                "44444444-4444-4444-8444-444444444444",
                Some("55555555-5555-4555-8555-555555555555"),
                None,
                "claude",
            )
            .unwrap();
        registry
            .mark_spawned(
                run_id,
                pid,
                Some(4242),
                start.map(str::to_string),
                "claude_print",
                serde_json::json!({}),
            )
            .unwrap();
        registry.read(run_id).unwrap()
    }

    fn fact(lstart: &str) -> ProcessFact {
        ProcessFact {
            pid: 4242,
            tty: "??".to_string(),
            stat: "S".to_string(),
            lstart: lstart.to_string(),
            command: "claude".to_string(),
            start_time: None,
        }
    }

    /// The regression this enum exists for: an unreadable `ps` used to come
    /// back as `false` from a boolean helper, which the monitor loops read as
    /// "the provider died" and answered with SIGKILL plus a terminal event.
    #[test]
    fn an_unreadable_inventory_is_unknown_not_gone() {
        let claim = spawned_claim(Some(4242), Some("Mon Jan  1 00:00:00 2024"));
        assert_eq!(claim_liveness(&claim, None), ClaimLiveness::Unknown);
    }

    #[test]
    fn matching_start_time_is_live_and_a_changed_one_is_gone() {
        let claim = spawned_claim(Some(4242), Some("Mon Jan  1 00:00:00 2024"));
        let mut inventory = HashMap::new();
        inventory.insert(4242, fact("Mon Jan  1 00:00:00 2024"));
        assert_eq!(
            claim_liveness(&claim, Some(&inventory)),
            ClaimLiveness::Live
        );

        inventory.insert(4242, fact("Tue Jan  2 00:00:00 2024"));
        assert_eq!(
            claim_liveness(&claim, Some(&inventory)),
            ClaimLiveness::Gone
        );

        inventory.remove(&4242);
        assert_eq!(
            claim_liveness(&claim, Some(&inventory)),
            ClaimLiveness::Gone
        );
    }

    #[test]
    fn a_claim_without_process_identity_is_gone_against_a_readable_inventory() {
        let claim = spawned_claim(None, None);
        let inventory = HashMap::new();
        assert_eq!(
            claim_liveness(&claim, Some(&inventory)),
            ClaimLiveness::Gone
        );
        // ...but still unknown when there is no inventory to check it against.
        assert_eq!(claim_liveness(&claim, None), ClaimLiveness::Unknown);
    }
}
