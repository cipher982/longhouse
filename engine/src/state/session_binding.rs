//! Managed session ID bindings for transcript files.
//!
//! Maps canonical transcript paths to Longhouse-managed session IDs.
//! Seeded by launchers/bridges BEFORE transcript activity starts.
//! The daemon reads bindings to pass `session_id_override` when shipping.

use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;

/// Whether a binding's owning session is still running.
///
/// A session that exits still owes whatever it wrote before it stopped, so the
/// binding outlives the owner: `exited` keeps the path in the reconciler's
/// working set until its records are shipped, rather than dropping the debt
/// along with the process.
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
    pub fn bind_for_thread(
        &self,
        path: &str,
        session_id: &str,
        provider: &str,
        provider_session_id: Option<&str>,
    ) -> Result<()> {
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

    /// Record that a path was observed again while its session is live.
    pub fn mark_seen(&self, path: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE session_binding SET last_seen_at = ?2 WHERE path = ?1",
            rusqlite::params![path, Utc::now().to_rfc3339()],
        )?;
        Ok(())
    }

    /// Move a binding's lifecycle state.
    pub fn set_state(&self, path: &str, state: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE session_binding SET state = ?2 WHERE path = ?1",
            rusqlite::params![path, state],
        )?;
        Ok(())
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
        binding
            .bind("/tmp/a.jsonl", "session-a", "omp")
            .unwrap();
        binding
            .bind("/tmp/b.jsonl", "session-b", "pi")
            .unwrap();

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
    fn rebinding_a_path_reactivates_it() {
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
    fn marking_seen_advances_without_changing_ownership() {
        let (_tmp, conn) = setup();
        let binding = SessionBinding::new(&conn);
        binding.bind("/tmp/a.jsonl", "session-a", "omp").unwrap();
        let before = binding.list_bindings().unwrap()[0].last_seen_at.clone();

        std::thread::sleep(std::time::Duration::from_millis(5));
        binding.mark_seen("/tmp/a.jsonl").unwrap();

        let listed = binding.list_bindings().unwrap();
        let after = listed.first().expect("binding survives mark_seen");
        assert_eq!(after.session_id, "session-a");
        assert_ne!(after.last_seen_at, before);
    }
}
