use std::path::PathBuf;

use anyhow::Result;
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnmanagedProcessBindingSignal {
    pub provider: String,
    pub provider_session_id: String,
    pub source_path: Option<PathBuf>,
    pub pid: u32,
    pub process_start_time: DateTime<Utc>,
    pub process_start_time_key: String,
    pub cwd: Option<String>,
    pub observed_at: DateTime<Utc>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnmanagedProcessBindingRow {
    pub provider: String,
    pub provider_session_id: String,
    pub source_path: Option<PathBuf>,
    pub pid: u32,
    pub process_start_time: DateTime<Utc>,
    pub process_start_time_key: String,
    pub cwd: Option<String>,
    pub observed_at: DateTime<Utc>,
}

pub struct UnmanagedProcessBindingStore<'a> {
    conn: &'a Connection,
}

impl<'a> UnmanagedProcessBindingStore<'a> {
    pub fn new(conn: &'a Connection) -> Self {
        Self { conn }
    }

    pub fn record(&self, signal: &UnmanagedProcessBindingSignal) -> Result<()> {
        self.conn.execute(
            "INSERT INTO unmanaged_process_binding_state (
                provider, provider_session_id, source_path, pid, process_start_time,
                process_start_time_key, cwd, observed_at, updated_at
             )
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)
             ON CONFLICT(provider, provider_session_id) DO UPDATE SET
                source_path = excluded.source_path,
                pid = excluded.pid,
                process_start_time = excluded.process_start_time,
                process_start_time_key = excluded.process_start_time_key,
                cwd = excluded.cwd,
                observed_at = excluded.observed_at,
                updated_at = excluded.updated_at
             WHERE excluded.observed_at >= unmanaged_process_binding_state.observed_at",
            params![
                signal.provider,
                signal.provider_session_id,
                signal
                    .source_path
                    .as_ref()
                    .map(|path| path.to_string_lossy().to_string()),
                i64::from(signal.pid),
                signal.process_start_time.to_rfc3339(),
                signal.process_start_time_key,
                signal.cwd.as_deref(),
                signal.observed_at.to_rfc3339(),
            ],
        )?;
        Ok(())
    }

    pub fn prune_older_than(&self, cutoff: DateTime<Utc>) -> Result<usize> {
        let deleted = self.conn.execute(
            "DELETE FROM unmanaged_process_binding_state WHERE observed_at < ?1",
            params![cutoff.to_rfc3339()],
        )?;
        Ok(deleted)
    }

    pub fn load_all(&self) -> Result<Vec<UnmanagedProcessBindingRow>> {
        let mut stmt = self.conn.prepare(
            "SELECT provider, provider_session_id, source_path, pid,
                    process_start_time, process_start_time_key, cwd, observed_at
             FROM unmanaged_process_binding_state
             ORDER BY observed_at DESC",
        )?;
        let rows = stmt.query_map([], |row| {
            let process_start_time: String = row.get(4)?;
            let observed_at: String = row.get(7)?;
            let pid_i64: i64 = row.get(3)?;
            Ok(UnmanagedProcessBindingRow {
                provider: row.get(0)?,
                provider_session_id: row.get(1)?,
                source_path: row.get::<_, Option<String>>(2)?.map(PathBuf::from),
                pid: pid_i64.max(0) as u32,
                process_start_time: DateTime::parse_from_rfc3339(&process_start_time)
                    .map(|dt| dt.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now()),
                process_start_time_key: row.get(5)?,
                cwd: row.get(6)?,
                observed_at: DateTime::parse_from_rfc3339(&observed_at)
                    .map(|dt| dt.with_timezone(&Utc))
                    .unwrap_or_else(|_| Utc::now()),
            })
        })?;

        let mut out = Vec::new();
        for row in rows {
            out.push(row?);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn t(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn record_keeps_newest_binding_per_provider_session() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = UnmanagedProcessBindingStore::new(&conn);

        store
            .record(&UnmanagedProcessBindingSignal {
                provider: "claude".to_string(),
                provider_session_id: "sess-1".to_string(),
                source_path: Some(PathBuf::from("/tmp/old.jsonl")),
                pid: 10,
                process_start_time: t("2026-05-05T12:00:00Z"),
                process_start_time_key: "Tue May  5 12:00:00 2026".to_string(),
                cwd: Some("/tmp".to_string()),
                observed_at: t("2026-05-05T12:01:00Z"),
            })
            .unwrap();
        store
            .record(&UnmanagedProcessBindingSignal {
                provider: "claude".to_string(),
                provider_session_id: "sess-1".to_string(),
                source_path: Some(PathBuf::from("/tmp/new.jsonl")),
                pid: 11,
                process_start_time: t("2026-05-05T12:05:00Z"),
                process_start_time_key: "Tue May  5 12:05:00 2026".to_string(),
                cwd: Some("/tmp/new".to_string()),
                observed_at: t("2026-05-05T12:06:00Z"),
            })
            .unwrap();

        let rows = store.load_all().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].pid, 11);
        assert_eq!(rows[0].source_path, Some(PathBuf::from("/tmp/new.jsonl")));
    }

    #[test]
    fn prune_older_than_deletes_old_rows() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let conn = crate::state::db::open_db(Some(tmp.path())).unwrap();
        let store = UnmanagedProcessBindingStore::new(&conn);

        store
            .record(&UnmanagedProcessBindingSignal {
                provider: "claude".to_string(),
                provider_session_id: "old".to_string(),
                source_path: None,
                pid: 10,
                process_start_time: t("2026-05-05T12:00:00Z"),
                process_start_time_key: "Tue May  5 12:00:00 2026".to_string(),
                cwd: None,
                observed_at: t("2026-05-05T12:00:00Z"),
            })
            .unwrap();
        store
            .record(&UnmanagedProcessBindingSignal {
                provider: "claude".to_string(),
                provider_session_id: "new".to_string(),
                source_path: None,
                pid: 11,
                process_start_time: t("2026-05-06T12:00:00Z"),
                process_start_time_key: "Wed May  6 12:00:00 2026".to_string(),
                cwd: None,
                observed_at: t("2026-05-06T12:00:00Z"),
            })
            .unwrap();

        assert_eq!(
            store.prune_older_than(t("2026-05-06T00:00:00Z")).unwrap(),
            1
        );
        let rows = store.load_all().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].provider_session_id, "new");
    }
}
