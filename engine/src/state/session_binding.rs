//! Managed session ID bindings for transcript files.
//!
//! Maps canonical transcript paths to Longhouse-managed session IDs.
//! Seeded by launchers/bridges BEFORE transcript activity starts.
//! The daemon reads bindings to pass `session_id_override` when shipping.

use anyhow::Result;
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension};

/// Whether a binding's owning session is still running.
///
/// A session that exits still owes whatever it wrote before it stopped, so the
/// binding outlives the owner: `exited` keeps the path in the reconciler's
/// working set until its records are shipped, rather than dropping the debt
/// along with the process.
///
/// The state is liveness evidence, and only liveness evidence writes it: the
/// managed scan's process observation of a run, a launch claim being projected
/// (`set_state_for_owner`) or released (`mark_exited`), or a new or changed
/// ownership assertion, which starts `active`. Finding the transcript file again is ownership evidence at
/// most, never liveness: a file still sitting on disk says nothing about whether
/// the run that wrote it is alive, so rediscovery neither revives an exited
/// binding nor rewrites an unchanged one (`bind_for_thread`).
pub const BINDING_STATE_ACTIVE: &str = "active";
pub const BINDING_STATE_EXITED: &str = "exited";

/// One durable path-to-session binding, as the reconciler needs to see it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceBinding {
    pub path: String,
    pub session_id: String,
    pub provider: String,
    pub provider_session_id: Option<String>,
    pub state: String,
    pub last_seen_at: Option<String>,
}

pub struct SessionBinding<'a> {
    conn: &'a Connection,
}

impl<'a> SessionBinding<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    /// Bind a transcript path to a managed session ID, recording which provider
    /// thread the binding was made for.
    ///
    /// A path-keyed binding cannot, on its own, distinguish one written
    /// deliberately for a forked child from one a managed parent left on the
    /// next file that appeared. Recording the thread id makes that decidable:
    /// a binding whose `provider_session_id` equals the transcript's own id was
    /// made *for this thread*, and nothing else was.
    ///
    /// Every scan pass re-asserts the ownership it rediscovers, so an assertion
    /// the row already records is not an event: it writes nothing and leaves
    /// `state` and the timestamps alone. Only new or changed ownership writes,
    /// and that starts `active`.
    pub fn bind_for_thread(
        &self,
        path: &str,
        session_id: &str,
        provider: &str,
        provider_session_id: Option<&str>,
    ) -> Result<()> {
        let recorded: Option<(String, String, Option<String>)> = self
            .conn
            .query_row(
                "SELECT session_id, provider, provider_session_id FROM session_binding
                 WHERE path = ?1",
                [path],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()?;
        if recorded.is_some_and(|(recorded_session, recorded_provider, recorded_thread)| {
            recorded_session.eq_ignore_ascii_case(session_id)
                && recorded_provider.eq_ignore_ascii_case(provider)
                && recorded_thread.as_deref() == provider_session_id
        }) {
            return Ok(());
        }
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO session_binding (path, session_id, provider, provider_session_id, updated_at, state, bound_at, last_seen_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?5, ?5)
             ON CONFLICT(path) DO UPDATE SET
                 session_id = ?2,
                 provider = ?3,
                 provider_session_id = ?4,
                 updated_at = ?5,
                 last_seen_at = ?5,
                 state = 'active',
                 bound_at = COALESCE(session_binding.bound_at, ?5)",
            rusqlite::params![path, session_id, provider, provider_session_id, now],
        )?;
        Ok(())
    }

    /// Look up a binding only when it belongs to the provider reading it.
    ///
    /// Transcript paths are not provider identities. A stale or misrouted
    /// binding must never let one provider publish into another provider's
    /// Longhouse session.
    pub fn get_with_thread_for_provider(
        &self,
        path: &str,
        provider: &str,
    ) -> Result<Option<(String, Option<String>)>> {
        let result = self.conn.query_row(
            "SELECT session_id, provider_session_id FROM session_binding
             WHERE path = ?1 AND lower(provider) = lower(?2)",
            rusqlite::params![path, provider],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?)),
        );
        match result {
            Ok(found) => Ok(Some(found)),
            Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    /// Bind a transcript path to a managed session ID.
    /// Upserts — later binds overwrite earlier ones.
    pub fn bind(&self, path: &str, session_id: &str, provider: &str) -> Result<()> {
        let now = Utc::now().to_rfc3339();
        self.conn.execute(
            "INSERT INTO session_binding (path, session_id, provider, updated_at, state, bound_at, last_seen_at)
             VALUES (?1, ?2, ?3, ?4, 'active', ?4, ?4)
             ON CONFLICT(path) DO UPDATE SET
                 session_id = ?2,
                 provider = ?3,
                 updated_at = ?4,
                 last_seen_at = ?4,
                 state = 'active',
                 bound_at = COALESCE(session_binding.bound_at, ?4)",
            rusqlite::params![path, session_id, provider, now],
        )?;
        Ok(())
    }

    /// Look up a managed session only for the provider that created it.
    pub fn get_for_provider(&self, path: &str, provider: &str) -> Result<Option<String>> {
        Ok(self
            .get_with_thread_for_provider(path, provider)?
            .map(|(session_id, _)| session_id))
    }

    /// Every durable binding, for the reconciler's working set.
    ///
    /// This is the enumeration the path-keyed table never had. The set is small
    /// by construction — it holds sessions a launcher deliberately bound, not
    /// every transcript on the machine — so callers may walk it on a tick.
    pub fn list_bindings(&self) -> Result<Vec<SourceBinding>> {
        let mut statement = self.conn.prepare(
            "SELECT path, session_id, provider, provider_session_id, state, last_seen_at
             FROM session_binding
             ORDER BY path",
        )?;
        let rows = statement.query_map([], |row| {
            Ok(SourceBinding {
                path: row.get(0)?,
                session_id: row.get(1)?,
                provider: row.get(2)?,
                provider_session_id: row.get(3)?,
                state: row.get(4)?,
                last_seen_at: row.get(5)?,
            })
        })?;
        Ok(rows.collect::<std::result::Result<Vec<_>, _>>()?)
    }

    /// Record that the owning session stopped, without dropping its debt.
    pub fn mark_exited(&self, path: &str) -> Result<()> {
        self.set_state(path, BINDING_STATE_EXITED)
    }

    /// Move a binding's lifecycle state.
    ///
    /// A binding already in that state is left alone: the managed scan restates
    /// what it observed on every pass, and an idle machine must not turn that
    /// into a write per binding.
    pub fn set_state(&self, path: &str, state: &str) -> Result<()> {
        if self
            .state_of(path, None)?
            .as_deref()
            .is_none_or(|current| current == state)
        {
            return Ok(());
        }
        self.conn.execute(
            "UPDATE session_binding SET state = ?2 WHERE path = ?1",
            rusqlite::params![path, state],
        )?;
        Ok(())
    }

    /// Move a binding's state on evidence about one session's run.
    ///
    /// A run's process observation speaks for the session that ran it, not for
    /// whoever owns the path now: a path reused by a later session keeps the
    /// state its own owner earned.
    pub fn set_state_for_owner(&self, path: &str, session_id: &str, state: &str) -> Result<()> {
        if self
            .state_of(path, Some(session_id))?
            .as_deref()
            .is_none_or(|current| current == state)
        {
            return Ok(());
        }
        self.conn.execute(
            "UPDATE session_binding SET state = ?3
             WHERE path = ?1 AND lower(session_id) = lower(?2)",
            rusqlite::params![path, session_id, state],
        )?;
        Ok(())
    }

    /// The recorded state, read without taking the writer lock. `owner`, when
    /// given, must match the binding's session or nothing is returned.
    fn state_of(&self, path: &str, owner: Option<&str>) -> Result<Option<String>> {
        Ok(self
            .conn
            .query_row(
                "SELECT state FROM session_binding
                 WHERE path = ?1 AND (?2 IS NULL OR lower(session_id) = lower(?2))",
                rusqlite::params![path, owner],
                |row| row.get(0),
            )
            .optional()?)
    }

    /// Remove binding for a transcript path.
    pub fn unbind(&self, path: &str) -> Result<()> {
        self.conn
            .execute("DELETE FROM session_binding WHERE path = ?", [path])?;
        Ok(())
    }

    /// Prune stale bindings where the transcript file no longer exists on disk
    /// and the binding is older than `days`.
    pub fn prune_stale(&self, days: u64) -> Result<usize> {
        let cutoff = (Utc::now() - chrono::Duration::days(days as i64)).to_rfc3339();
        let mut stmt = self
            .conn
            .prepare("SELECT path FROM session_binding WHERE updated_at < ?")?;
        let paths: Vec<String> = stmt
            .query_map([&cutoff], |row| row.get(0))?
            .filter_map(|r| r.ok())
            .collect();

        let mut pruned = 0usize;
        for path in &paths {
            if !std::path::Path::new(path).exists() {
                self.conn
                    .execute("DELETE FROM session_binding WHERE path = ?", [path])?;
                pruned += 1;
            }
        }
        Ok(pruned)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;

    fn setup() -> (tempfile::NamedTempFile, Connection) {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(tmp.path())).unwrap();
        (tmp, conn)
    }

    #[test]
    fn test_bind_and_get() {
        let (_tmp, conn) = setup();
        let sb = SessionBinding::new(&conn);

        assert!(sb
            .get_for_provider("/path/to/session.jsonl", "claude")
            .unwrap()
            .is_none());

        sb.bind("/path/to/session.jsonl", "managed-uuid-1", "claude")
            .unwrap();
        assert_eq!(
            sb.get_for_provider("/path/to/session.jsonl", "claude")
                .unwrap()
                .as_deref(),
            Some("managed-uuid-1")
        );
    }

    #[test]
    fn test_bind_upserts() {
        let (_tmp, conn) = setup();
        let sb = SessionBinding::new(&conn);

        sb.bind("/f.jsonl", "old-id", "claude").unwrap();
        sb.bind("/f.jsonl", "new-id", "claude").unwrap();
        assert_eq!(
            sb.get_for_provider("/f.jsonl", "claude")
                .unwrap()
                .as_deref(),
            Some("new-id")
        );
    }

    #[test]
    fn provider_scoped_lookup_rejects_cross_provider_binding() {
        let (_tmp, conn) = setup();
        let sb = SessionBinding::new(&conn);
        sb.bind_for_thread("/f.jsonl", "claude-id", "claude", Some("thread-1"))
            .unwrap();

        assert_eq!(
            sb.get_for_provider("/f.jsonl", "claude")
                .unwrap()
                .as_deref(),
            Some("claude-id")
        );
        assert!(sb.get_for_provider("/f.jsonl", "cursor").unwrap().is_none());
        assert!(sb
            .get_with_thread_for_provider("/f.jsonl", "cursor")
            .unwrap()
            .is_none());
    }

    #[test]
    fn test_unbind() {
        let (_tmp, conn) = setup();
        let sb = SessionBinding::new(&conn);

        sb.bind("/f.jsonl", "id-1", "claude").unwrap();
        sb.unbind("/f.jsonl").unwrap();
        assert!(sb.get_for_provider("/f.jsonl", "claude").unwrap().is_none());
    }

    #[test]
    fn test_prune_stale() {
        let (_tmp, conn) = setup();
        let sb = SessionBinding::new(&conn);

        // Insert a stale binding (35 days old, file doesn't exist)
        let old_date = (Utc::now() - chrono::Duration::days(35)).to_rfc3339();
        conn.execute(
            "INSERT INTO session_binding (path, session_id, provider, updated_at)
             VALUES ('/gone/old.jsonl', 'stale-id', 'claude', ?1)",
            [&old_date],
        )
        .unwrap();

        // Insert a recent binding (file doesn't exist but is recent)
        sb.bind("/gone/recent.jsonl", "fresh-id", "claude").unwrap();

        let pruned = sb.prune_stale(30).unwrap();
        assert_eq!(pruned, 1);
        assert!(sb
            .get_for_provider("/gone/old.jsonl", "claude")
            .unwrap()
            .is_none());
        assert!(sb
            .get_for_provider("/gone/recent.jsonl", "claude")
            .unwrap()
            .is_some());
    }

    #[test]
    fn bindings_are_enumerable_with_their_lifecycle_state() {
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();
        binding.bind("/tmp/b.jsonl", "session-b", "pi").unwrap();

        let listed = binding.list_bindings().unwrap();

        assert_eq!(listed.len(), 2);
        assert_eq!(listed[0].path, "/tmp/a.jsonl");
        assert_eq!(listed[0].session_id, "session-a");
        assert_eq!(listed[0].provider, "omp");
        assert_eq!(listed[0].state, BINDING_STATE_ACTIVE);
        assert_eq!(listed[1].session_id, "session-b");
    }

    #[test]
    fn an_exited_owner_stays_in_the_working_set() {
        // The replication debt outlives the process: a session that stopped
        // still owes whatever it wrote before it did.
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();

        binding.mark_exited("/tmp/a.jsonl").unwrap();

        let listed = binding.list_bindings().unwrap();
        assert_eq!(listed.len(), 1, "an exited owner must not vanish");
        assert_eq!(listed[0].state, BINDING_STATE_EXITED);
    }

    #[test]
    fn an_explicit_bind_reactivates_the_path() {
        // `bind` is a launcher or hook saying the session is running now; that
        // is liveness evidence. Rediscovery is `bind_for_thread`, tested below.
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();
        binding.mark_exited("/tmp/a.jsonl").unwrap();

        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();

        let listed = binding.list_bindings().unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].state, BINDING_STATE_ACTIVE);
    }

    #[test]
    fn a_reused_path_has_one_current_binding() {
        // A native switch or resume that reuses a path makes that file the new
        // session's transcript, so the current binding is the only one that
        // matters for shipping. History is not retained here on purpose.
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();
        binding.bind("/tmp/a.jsonl", "session-b", "omp").unwrap();

        let listed = binding.list_bindings().unwrap();

        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].session_id, "session-b");
        assert_eq!(listed[0].state, BINDING_STATE_ACTIVE);
    }

    #[test]
    fn rediscovering_recorded_ownership_writes_nothing_and_leaves_state_alone() {
        use crate::state::wal_window::WalWindow;
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding
            .bind_for_thread("/tmp/a.jsonl", "session-a", "omp", Some("native-a"))
            .unwrap();
        binding.mark_exited("/tmp/a.jsonl").unwrap();
        let before = binding.list_bindings().unwrap();
        let stamp = |conn: &Connection| -> String {
            conn.query_row(
                "SELECT updated_at || last_seen_at FROM session_binding WHERE path = '/tmp/a.jsonl'",
                [],
                |row| row.get(0),
            )
            .unwrap()
        };
        let stamped = stamp(&conn);

        // A scan pass re-derives the same ownership from the same evidence.
        let window = WalWindow::open(&conn);
        for _pass in 0..3 {
            binding
                .bind_for_thread("/tmp/a.jsonl", "session-a", "omp", Some("native-a"))
                .unwrap();
        }
        let cost = window.cost(&conn);

        assert!(cost.is_zero(), "rediscovery wrote: {cost:?}");
        assert_eq!(
            binding.list_bindings().unwrap(),
            before,
            "the file being on disk again is not evidence its run is alive"
        );
        assert_eq!(
            binding.list_bindings().unwrap()[0].state,
            BINDING_STATE_EXITED
        );
        assert_eq!(stamp(&conn), stamped);
    }

    #[test]
    fn changed_ownership_is_written_and_starts_active() {
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding
            .bind_for_thread("/tmp/a.jsonl", "session-a", "omp", Some("native-a"))
            .unwrap();
        binding.mark_exited("/tmp/a.jsonl").unwrap();

        // A later session took the path over, or the provider thread was learned.
        binding
            .bind_for_thread("/tmp/a.jsonl", "session-b", "omp", Some("native-a"))
            .unwrap();
        let listed = binding.list_bindings().unwrap();
        assert_eq!(listed[0].session_id, "session-b");
        assert_eq!(listed[0].state, BINDING_STATE_ACTIVE);

        binding.mark_exited("/tmp/a.jsonl").unwrap();
        binding
            .bind_for_thread("/tmp/a.jsonl", "session-b", "omp", Some("native-b"))
            .unwrap();
        let listed = binding.list_bindings().unwrap();
        assert_eq!(listed[0].provider_session_id.as_deref(), Some("native-b"));
        assert_eq!(listed[0].state, BINDING_STATE_ACTIVE);
    }

    #[test]
    fn a_state_the_binding_already_has_is_not_rewritten() {
        use crate::state::wal_window::WalWindow;
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();
        binding.mark_exited("/tmp/a.jsonl").unwrap();

        let window = WalWindow::open(&conn);
        for _pass in 0..3 {
            binding.mark_exited("/tmp/a.jsonl").unwrap();
            binding
                .set_state_for_owner("/tmp/a.jsonl", "SESSION-A", BINDING_STATE_EXITED)
                .unwrap();
            binding
                .set_state_for_owner("/tmp/gone.jsonl", "session-a", BINDING_STATE_ACTIVE)
                .unwrap();
        }
        let cost = window.cost(&conn);
        assert!(
            cost.is_zero(),
            "restating an observed state wrote: {cost:?}"
        );

        // New evidence moves it, and moves it back.
        binding
            .set_state_for_owner("/tmp/a.jsonl", "session-a", BINDING_STATE_ACTIVE)
            .unwrap();
        assert_eq!(
            binding.list_bindings().unwrap()[0].state,
            BINDING_STATE_ACTIVE
        );
        binding.mark_exited("/tmp/a.jsonl").unwrap();
        assert_eq!(
            binding.list_bindings().unwrap()[0].state,
            BINDING_STATE_EXITED
        );
    }

    #[test]
    fn a_runs_observation_does_not_move_a_binding_its_session_no_longer_owns() {
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-b", "omp").unwrap();

        // Session A's run ended; the path now belongs to session B.
        binding
            .set_state_for_owner("/tmp/a.jsonl", "session-a", BINDING_STATE_EXITED)
            .unwrap();

        assert_eq!(
            binding.list_bindings().unwrap()[0].state,
            BINDING_STATE_ACTIVE
        );
    }
}
