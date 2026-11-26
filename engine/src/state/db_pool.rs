//! Bounded reusable pool of shipper SQLite connections.
//!
//! Per-job `open_db()` was a measurable hot-path cost (P95 ~190ms outliers
//! under contention) because cold open re-runs schema bootstrap, several
//! `PRAGMA table_info` introspections, dedupe `DELETE`s,
//! and a handful of `CREATE INDEX IF NOT EXISTS` statements. Schema work only
//! needs to happen once per process; PRAGMAs are the only per-connection
//! setup.
//!
//! After startup, `prepare_file_for_job` and `run_path_job` lease a
//! connection from this pool instead. The pool grows lazily up to a cap and
//! returns idle connections via `Drop`.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use anyhow::Result;
use rusqlite::Connection;

use super::db::{open_connection, resolve_db_path};

/// Reusable pool of fast-path SQLite connections.
///
/// Schema bootstrap must have run via `open_db` before connections are leased.
#[derive(Clone)]
pub struct ConnectionPool {
    inner: Arc<Inner>,
}

#[cfg(unix)]
type DbFileId = (u64, u64);

#[cfg(not(unix))]
type DbFileId = ();

fn current_db_file_id(path: &Path) -> Option<DbFileId> {
    let meta = std::fs::metadata(path).ok()?;
    if !meta.is_file() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Some((meta.dev(), meta.ino()))
    }
    #[cfg(not(unix))]
    {
        Some(())
    }
}

struct IdleConnection {
    conn: Connection,
    file_id: Option<DbFileId>,
}

struct Inner {
    db_path: PathBuf,
    capacity: usize,
    idle: Mutex<Vec<IdleConnection>>,
}

pub struct PooledConnection {
    conn: Option<Connection>,
    file_id: Option<DbFileId>,
    pool: Arc<Inner>,
}

impl ConnectionPool {
    /// Create a pool that connects to `db_path` (or the default path) with
    /// the given soft `capacity`. Capacity caps the *idle* pool size; live
    /// concurrent borrows are unlimited (over-cap connections are dropped on
    /// return rather than parked).
    pub fn new(db_path: Option<&Path>, capacity: usize) -> Result<Self> {
        let resolved = resolve_db_path(db_path)?;
        Ok(Self {
            inner: Arc::new(Inner {
                db_path: resolved,
                capacity: capacity.max(1),
                idle: Mutex::new(Vec::new()),
            }),
        })
    }

    /// Lease a connection from the pool if one is already idle and the
    /// underlying database file on disk still matches its `(dev, ino)`.
    /// Returns `None` on miss or file replacement so callers fall back to `get`.
    pub fn try_get_idle(&self) -> Option<PooledConnection> {
        let live_id = current_db_file_id(&self.inner.db_path)?;
        let entry = {
            let mut idle = self.inner.idle.lock().ok()?;
            let mut chosen = None;
            while let Some(candidate) = idle.pop() {
                if candidate.file_id == Some(live_id) {
                    chosen = Some(candidate);
                    break;
                }
            }
            chosen?
        };
        Some(PooledConnection {
            conn: Some(entry.conn),
            file_id: entry.file_id,
            pool: Arc::clone(&self.inner),
        })
    }

    /// Lease a connection from the pool, opening a new one if none is idle or
    /// if the underlying database file on disk was replaced or became unusable.
    ///
    /// Cheap when an idle connection is parked and `(dev, ino)` still matches
    /// (one `stat()`, no SQLite work). On a miss or path replacement it runs
    /// `open_connection` (just `Connection::open` + two PRAGMAs).
    pub fn get(&self) -> Result<PooledConnection> {
        let live_id = current_db_file_id(&self.inner.db_path);
        let reused = {
            let mut idle = self.inner.idle.lock().expect("connection pool poisoned");
            match live_id {
                Some(id) => {
                    let mut chosen = None;
                    while let Some(entry) = idle.pop() {
                        if entry.file_id == Some(id) {
                            chosen = Some(entry);
                            break;
                        }
                    }
                    chosen
                }
                None => {
                    // DB path was removed, replaced with a non-file, or became
                    // unreadable; drop stale descriptors so open_connection
                    // surfaces the current filesystem state.
                    idle.clear();
                    None
                }
            }
        };
        let (conn, file_id) = match reused {
            Some(entry) => (entry.conn, entry.file_id),
            None => {
                let conn = open_connection(&self.inner.db_path)?;
                let opened_id = current_db_file_id(&self.inner.db_path);
                (conn, opened_id)
            }
        };
        Ok(PooledConnection {
            conn: Some(conn),
            file_id,
            pool: Arc::clone(&self.inner),
        })
    }
}

impl PooledConnection {
    pub fn as_conn(&self) -> &Connection {
        self.conn.as_ref().expect("connection already returned")
    }

    pub fn as_conn_mut(&mut self) -> &mut Connection {
        self.conn.as_mut().expect("connection already returned")
    }
}

impl std::ops::Deref for PooledConnection {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.as_conn()
    }
}

impl std::ops::DerefMut for PooledConnection {
    fn deref_mut(&mut self) -> &mut Connection {
        self.as_conn_mut()
    }
}

impl Drop for PooledConnection {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            let mut idle = match self.pool.idle.lock() {
                Ok(guard) => guard,
                Err(_) => return,
            };
            if idle.len() < self.pool.capacity {
                idle.push(IdleConnection {
                    conn,
                    file_id: self.file_id,
                });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;

    #[test]
    fn pool_reuses_connections() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        // Bootstrap schema once.
        drop(open_db(Some(tmp.path())).unwrap());

        let pool = ConnectionPool::new(Some(tmp.path()), 2).unwrap();

        // After a sequential borrow + drop the pool should hold one idle conn.
        {
            let _lease = pool.get().unwrap();
        }
        assert_eq!(
            pool.inner.idle.lock().unwrap().len(),
            1,
            "returning the only lease should park it as idle"
        );

        // The next lease should drain that idle slot rather than open a new
        // connection — the idle pool transiently empties during the borrow.
        {
            let _lease = pool.get().unwrap();
            assert_eq!(
                pool.inner.idle.lock().unwrap().len(),
                0,
                "active borrow should leave the idle pool empty"
            );
        }
        assert_eq!(
            pool.inner.idle.lock().unwrap().len(),
            1,
            "drop should park the connection again"
        );
    }

    #[test]
    fn pool_caps_idle_size() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        drop(open_db(Some(tmp.path())).unwrap());

        let pool = ConnectionPool::new(Some(tmp.path()), 1).unwrap();
        // Hold three concurrent leases; pool capacity is 1 so two will be
        // discarded on return.
        let l1 = pool.get().unwrap();
        let l2 = pool.get().unwrap();
        let l3 = pool.get().unwrap();
        drop(l1);
        drop(l2);
        drop(l3);
        assert_eq!(pool.inner.idle.lock().unwrap().len(), 1);
    }

    #[test]
    fn pool_connections_can_query() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        drop(open_db(Some(tmp.path())).unwrap());

        let pool = ConnectionPool::new(Some(tmp.path()), 2).unwrap();
        let lease = pool.get().unwrap();
        let count: i64 = lease
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='file_state'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn pool_invalidates_idle_connections_when_db_file_is_replaced_or_unusable() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("shipper.db");
        let backup_path = dir.path().join("shipper.db.bak");
        drop(open_db(Some(&db_path)).unwrap());

        let pool = ConnectionPool::new(Some(&db_path), 2).unwrap();
        {
            let _lease = pool.get().unwrap();
        }
        assert_eq!(pool.inner.idle.lock().unwrap().len(), 1);

        // Replace the DB path with a directory: pool must drop stale idle
        // descriptors and fail open instead of silently reading the old inode.
        std::fs::rename(&db_path, &backup_path).unwrap();
        std::fs::create_dir(&db_path).unwrap();
        assert!(pool.get().is_err());
        assert_eq!(pool.inner.idle.lock().unwrap().len(), 0);

        // Restoring the file lets the pool reopen and recover cleanly.
        std::fs::remove_dir(&db_path).unwrap();
        std::fs::rename(&backup_path, &db_path).unwrap();
        let lease = pool.get().unwrap();
        let count: i64 = lease
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='file_state'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}
