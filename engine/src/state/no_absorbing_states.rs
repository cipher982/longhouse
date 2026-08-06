//! One place that asserts durable work can always be reached again.
//!
//! Longhouse spent a day recovering from a class of bug rather than a bug: a
//! durable row entered a state that no code path would ever select again, so
//! the work was retained, displayed, and never retried. Three independent
//! instances shipped —
//!
//! - `pending_source_envelope.blocked_at` was set on conflict and
//!   `retry_paths` filtered `blocked_at IS NULL`, so quarantine was terminal by
//!   scheduling as well as by policy;
//! - `spool_queue.status = 'dead'` had no transition back to `pending`, and
//!   every query that selects work asks for `pending`;
//! - a Codex bridge with no recorded owner was owned forever by rule.
//!
//! Each was found by reading code and asking "what selects this again?". That
//! audit does not scale and does not repeat. This module turns the question
//! into a test.
//!
//! # The invariant
//!
//! **For every durable table that holds outstanding work, a row in any state
//! the production code can write must still be reachable — either by the
//! ordinary scheduler, or by a named recovery path that returns it there.**
//!
//! Adding a new terminal-looking state without a way back now fails a test
//! rather than waiting to be discovered on someone's machine. The cases below
//! are enumerated by hand on purpose: the point is that adding a state forces
//! an edit here, where the question "what brings this back?" is unavoidable.

#[cfg(test)]
mod tests {
    use crate::state::db::open_db;
    use crate::state::pending_source_envelope;
    use crate::state::spool::Spool;
    use rusqlite::params;
    use uuid::Uuid;

    fn temp_db() -> (tempfile::TempDir, rusqlite::Connection) {
        let dir = tempfile::tempdir().unwrap();
        let conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        (dir, conn)
    }

    /// `spool_queue.status` — every value production code writes.
    ///
    /// Grep for `SET status =` in `state/spool.rs` before trusting this list.
    const SPOOL_STATUSES: &[&str] = &["pending", "dead"];

    #[test]
    fn every_spool_status_can_be_worked_again() {
        for status in SPOOL_STATUSES {
            let (dir, conn) = temp_db();
            let spool = Spool::new(&conn);
            // A real file, because the recovery paths deliberately refuse rows
            // whose source can no longer be read.
            let source = dir.path().join("transcript.jsonl");
            std::fs::write(&source, b"{}\n").unwrap();
            spool
                .enqueue("claude", source.to_str().unwrap(), 0, 1, None)
                .unwrap();
            conn.execute(
                "UPDATE spool_queue SET status = ?1",
                params![status],
            )
            .unwrap();

            let reachable = match *status {
                // Selected directly by the shipper.
                "pending" => spool.pending_count().unwrap() > 0,
                // Not selected directly; must have a documented way back.
                "dead" => {
                    spool.revive_dead_with_readable_sources(10).unwrap() > 0
                        && spool.pending_count().unwrap() > 0
                }
                other => panic!(
                    "spool status {other:?} has no reachability case here. Add one, or explain \
                     in this test why work in that state is genuinely finished."
                ),
            };
            assert!(
                reachable,
                "spool_queue rows in status {status:?} can never be worked again"
            );
        }
    }

    #[test]
    fn a_blocked_source_envelope_is_still_scheduled() {
        // The bug: `retry_paths` filtered `blocked_at IS NULL`, so a quarantined
        // source was skipped by restart recovery as well as by the live path.
        // Nothing in the process ever looked at the row again, which meant no
        // repair could run even once one existed.
        let (_dir, mut conn) = temp_db();
        let epoch = Uuid::new_v4();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation,
                 start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, 'claude', 'src', 'fixture', 'initial', 1, ?2, ?2)",
            params![epoch.to_string(), "2026-07-15T00:00:00Z"],
        )
        .unwrap();
        let candidate = pending_source_envelope::PendingSourceEnvelope::new(
            epoch,
            "/tmp/blocked.jsonl".to_string(),
            0,
            1,
            "a".repeat(64),
            vec![1],
            vec![2],
            1,
            1,
            true,
            false,
        );
        pending_source_envelope::persist_or_load(&mut conn, &candidate).unwrap();
        pending_source_envelope::quarantine(&mut conn, epoch, "fixture", "blocked").unwrap();

        // The invariant is *reachable*, not *immediately due*. Quarantine now
        // postpones by setting a future `wake_at`, so asserting the row appears
        // right away tested a stricter property than the invariant — and one
        // that backoff correctly violates. Advancing the clock is the honest
        // form: the row must come back on its own, without anything else
        // intervening.
        assert!(
            !pending_source_envelope::retry_paths(&conn)
                .unwrap()
                .iter()
                .any(|p| p.source_path == "/tmp/blocked.jsonl"),
            "a freshly blocked source is postponed rather than spun on"
        );
        conn.execute(
            "UPDATE pending_source_envelope SET wake_at = '1970-01-01T00:00:00+00:00'",
            [],
        )
        .unwrap();
        let paths = pending_source_envelope::retry_paths(&conn).unwrap();
        assert!(
            paths.iter().any(|p| p.source_path == "/tmp/blocked.jsonl"),
            "once due, a quarantined source must be scheduled like any other row; it is \
             re-examined against host truth when it runs, and stays blocked only if there \
             is genuinely nothing to do"
        );
    }

    /// A recovery function nothing calls is the bug, not the fix.
    ///
    /// The other tests here prove the recovery *functions* behave. None of them
    /// can observe whether the daemon still invokes them: delete the tick
    /// wiring and they all stay green. That is precisely the producer/consumer
    /// failure this module exists to catch — `--owner-pid` was defined, parsed
    /// and forwarded while no caller passed it, and the orphan-bridge reaper
    /// exists only in comments.
    ///
    /// Asserting on source text is crude, and it is the cheapest thing that
    /// actually fails when the call site disappears. A stronger version would
    /// drive the daemon loop directly; until that exists, this is the guard.
    #[test]
    fn every_recovery_producer_is_wired_into_the_daemon() {
        let daemon = include_str!("../daemon.rs");
        for (function, why) in [
            (
                "revive_dead_with_readable_sources",
                "dead spool ranges would never return to pending",
            ),
            (
                "run_check_tick",
                "the machine would never learn it is running a stale binary",
            ),
        ] {
            assert!(
                daemon.contains(function),
                "{function} is not called from daemon.rs, so {why}. A recovery path that \
                 nothing schedules is indistinguishable from one that does not exist."
            );
        }
    }

    #[test]
    fn a_dead_range_whose_source_survives_is_never_deleted() {
        // The bug: cleanup() hard-deleted dead rows after 30 days. A spool row
        // is a pointer into the user's own transcript, not a copy, so deleting
        // one destroyed no data — it destroyed the record that the bytes were
        // never shipped, dropping dead_ranges to zero and turning the status
        // surface green with the debt unpaid.
        let (dir, conn) = temp_db();
        let spool = Spool::new(&conn);
        let source = dir.path().join("transcript.jsonl");
        std::fs::write(&source, b"{}\n").unwrap();
        spool
            .enqueue("claude", source.to_str().unwrap(), 0, 1, None)
            .unwrap();
        conn.execute(
            "UPDATE spool_queue SET status = 'dead', created_at = '2020-01-01T00:00:00Z'",
            [],
        )
        .unwrap();

        spool.cleanup().unwrap();

        assert_eq!(
            spool.dead_count().unwrap(),
            1,
            "an unshipped range whose source still exists must keep its pointer; forgetting it \
             reports a debt as paid"
        );
    }
}
