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
//! - the since-removed v1 spool's `dead` rows had no transition back to
//!   `pending`, and every query that selected work asked for `pending`;
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
    use rusqlite::params;
    use uuid::Uuid;

    fn temp_db() -> (tempfile::TempDir, rusqlite::Connection) {
        let dir = tempfile::tempdir().unwrap();
        let conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        (dir, conn)
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

    #[test]
    fn no_launcher_opens_the_archive_database() {
        // A launcher that opens the shipper database puts a cold process with a
        // fresh busy timeout in front of one WAL writer. On 2026-09-17 that made
        // a required identity bind lose the lock and left sessions degraded for
        // good. Launch authority is a local claim now; opening the database
        // again from any of these files is how the incident comes back.
        for (name, source) in [
            (
                "omp_helm_launcher.rs",
                include_str!("../omp_helm_launcher.rs"),
            ),
            ("omp_print.rs", include_str!("../omp_print.rs")),
            ("codex_exec.rs", include_str!("../codex_exec.rs")),
            ("pi_print.rs", include_str!("../pi_print.rs")),
            (
                "pi_helm_launcher.rs",
                include_str!("../pi_helm_launcher.rs"),
            ),
            (
                "antigravity_print.rs",
                include_str!("../antigravity_print.rs"),
            ),
            (
                "cursor_helm_launcher.rs",
                include_str!("../cursor_helm_launcher.rs"),
            ),
            ("cursor_print.rs", include_str!("../cursor_print.rs")),
        ] {
            // Only production code counts: a fixture may hold the database to
            // prove that a locked archive no longer blocks a launch.
            let production = source.split("#[cfg(test)]").next().unwrap_or_default();
            assert!(
                !production.contains("open_client_connection"),
                "{name} opens the archive database from a launcher path; launch authority is a \
                 claim (managed_source_claim), not a row"
            );
        }
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
        // A recovery path that nothing schedules is indistinguishable from one
        // that does not exist. The producer may live outside the daemon, so the
        // invariant is asserted in two parts: the producer exists where it is
        // claimed to, and the daemon names the entry point that schedules it.
        let daemon = concat!(
            include_str!("../daemon/mod.rs"),
            include_str!("../daemon/archive_repair.rs"),
            include_str!("../daemon/discovery_scans.rs"),
            include_str!("../daemon/managed_observation.rs"),
            include_str!("../daemon/path_jobs.rs"),
            include_str!("../daemon/projection.rs"),
            include_str!("../daemon/startup.rs"),
            include_str!("../daemon/status_slots.rs"),
            include_str!("../daemon/transcript_wake.rs"),
        );
        for (producer, producer_source, scheduled_entry, why) in [
            (
                "run_check_tick",
                daemon,
                "run_check_tick",
                "the machine would never learn it is running a stale binary",
            ),
            (
                "wake_blocked_for_new_engine",
                include_str!("pending_source_envelope.rs"),
                "wake_blocked_for_new_engine",
                "a fixed engine would wait out the old engine's backoff before re-judging blocked work",
            ),
        ] {
            assert!(
                producer_source.contains(producer),
                "{producer} is not found in the source this test claims holds it"
            );
            assert!(
                daemon.contains(scheduled_entry),
                "the daemon does not schedule {scheduled_entry}, so {why}. A recovery path that \
                 nothing schedules is indistinguishable from one that does not exist."
            );
        }
    }
}
