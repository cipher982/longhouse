//! Bounded reusable pool of shipper SQLite connections.
//!
//! Per-job `open_db()` was a measurable hot-path cost (P95 ~190ms outliers
//! under contention) because cold open re-runs schema bootstrap, several
//! `PRAGMA table_info` introspections, a `DELETE … GROUP BY` on `spool_queue`,
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

struct Inner {
    db_path: PathBuf,
    capacity: usize,
    idle: Mutex<Vec<Connection>>,
}

pub struct PooledConnection {
    conn: Option<Connection>,
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

    /// Lease a connection from the pool, opening a new one if none is idle.
    ///
    /// Cheap when an idle connection is parked (no SQLite work). On a miss it
    /// runs `open_connection` (just `Connection::open` + two PRAGMAs).
    pub fn get(&self) -> Result<PooledConnection> {
        let conn = {
            let mut idle = self.inner.idle.lock().expect("connection pool poisoned");
            idle.pop()
        };
        let conn = match conn {
            Some(c) => c,
            None => open_connection(&self.inner.db_path)?,
        };
        Ok(PooledConnection {
            conn: Some(conn),
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
                idle.push(conn);
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
}
