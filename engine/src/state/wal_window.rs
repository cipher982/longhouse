//! Test support: what a stretch of work cost the shipper database in writes.
//!
//! Read from the SQLite WAL itself, so it counts what reached disk rather than
//! what the code claims to have done. A scan pass over sources that did not move
//! must cost nothing here: no commit, no frame, no changed row.

use std::fs;
use std::path::PathBuf;

use rusqlite::Connection;

pub struct WalWindow {
    wal_path: PathBuf,
    start_len: u64,
    start_changes: u64,
}

#[derive(Debug, Default, Clone, Copy)]
pub struct WriteCost {
    /// Write transactions that dirtied a page (SQLite writes a commit frame).
    pub commits: usize,
    pub frames: usize,
    pub wal_bytes: u64,
    /// Rows inserted, updated or deleted through this connection.
    pub rows_changed: u64,
}

impl WriteCost {
    pub fn is_zero(&self) -> bool {
        self.commits == 0 && self.frames == 0 && self.rows_changed == 0
    }
}

impl WalWindow {
    /// Start measuring. Empties the WAL and disables auto-checkpoint on `conn`,
    /// so nothing can restart the WAL (and overwrite the frames being counted)
    /// while the window is open.
    pub fn open(conn: &Connection) -> Self {
        conn.execute_batch("PRAGMA wal_autocheckpoint=0;").unwrap();
        let busy: i64 = conn
            .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |row| row.get(0))
            .unwrap();
        assert_eq!(busy, 0, "another reader kept the WAL from being emptied");
        let db_path: String = conn
            .query_row(
                "SELECT file FROM pragma_database_list WHERE name = 'main'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let wal_path = PathBuf::from(format!("{db_path}-wal"));
        Self {
            start_len: fs::metadata(&wal_path).map(|meta| meta.len()).unwrap_or(0),
            wal_path,
            start_changes: conn.total_changes(),
        }
    }

    pub fn cost(&self, conn: &Connection) -> WriteCost {
        let mut cost = WriteCost {
            rows_changed: conn.total_changes() - self.start_changes,
            ..WriteCost::default()
        };
        let bytes = fs::read(&self.wal_path).unwrap_or_default();
        if bytes.len() < 32 {
            return cost;
        }
        let page_size = u32::from_be_bytes(bytes[8..12].try_into().unwrap()) as usize;
        let frame_len = 24 + page_size;
        let mut offset = 32 + (self.start_len.saturating_sub(32) as usize / frame_len) * frame_len;
        while offset + frame_len <= bytes.len() {
            cost.frames += 1;
            // A commit frame records the database size in pages; others hold 0.
            if u32::from_be_bytes(bytes[offset + 4..offset + 8].try_into().unwrap()) != 0 {
                cost.commits += 1;
            }
            offset += frame_len;
        }
        cost.wal_bytes = (cost.frames * frame_len) as u64;
        cost
    }
}
