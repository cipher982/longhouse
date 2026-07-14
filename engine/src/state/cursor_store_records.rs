//! Durable local spool for source-faithful Cursor raw records.
//!
//! The current Cursor SQLite snapshot can change before a Runtime Host receipt
//! arrives. Keeping exact wrapper bytes locally means a rejected envelope or a
//! machine restart retries the same evidence, even if Cursor has since written
//! a newer root or metadata value.

use anyhow::{Context, Result};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CursorRawRecord {
    pub source_position: u64,
    pub bytes: Vec<u8>,
}

/// Add unseen raw records to an epoch's durable local spool. A byte-identical
/// record is stored once per epoch; source positions are monotonically
/// assigned and never reused.
pub fn append_unseen_cursor_records(
    conn: &mut Connection,
    source_epoch: Uuid,
    records: &[Vec<u8>],
) -> Result<u64> {
    let epoch = source_epoch.to_string();
    let transaction = conn.transaction()?;
    let next = next_position(&transaction, &epoch)?;
    let mut next = next;
    for bytes in records {
        let hash = hex_hash(bytes);
        let inserted = transaction.execute(
            "INSERT INTO cursor_store_raw_record (
                 source_epoch, record_hash, source_position, record_bytes, created_at
             ) VALUES (?1, ?2, ?3, ?4, ?5)
             ON CONFLICT(source_epoch, record_hash) DO NOTHING",
            params![epoch, hash, next, bytes, Utc::now().to_rfc3339()],
        )?;
        if inserted == 1 {
            next = next
                .checked_add(1)
                .context("Cursor source position overflow")?;
        }
    }
    transaction.commit()?;
    Ok(next)
}

pub fn cursor_record_count(conn: &Connection, source_epoch: Uuid) -> Result<u64> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM cursor_store_raw_record WHERE source_epoch = ?1",
        [source_epoch.to_string()],
        |row| row.get(0),
    )?;
    u64::try_from(count).context("Cursor record count is negative")
}

pub fn active_cursor_record_count(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<u64> {
    let epoch: Option<String> = conn
        .query_row(
            "SELECT source_epoch
             FROM source_epoch_registry
             WHERE provider = ?1 AND opaque_source_id = ?2 AND ended_at IS NULL",
            params![provider, opaque_source_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(epoch) = epoch else {
        return Ok(0);
    };
    let epoch = Uuid::parse_str(&epoch).context("active Cursor source epoch is not a UUID")?;
    cursor_record_count(conn, epoch)
}

pub fn cursor_records_from(
    conn: &Connection,
    source_epoch: Uuid,
    start: u64,
    max_records: u64,
    max_bytes: u64,
) -> Result<Vec<CursorRawRecord>> {
    let start = i64::try_from(start).context("Cursor source position exceeds SQLite INTEGER")?;
    let max_records = i64::try_from(max_records).context("record limit exceeds SQLite INTEGER")?;
    let mut statement = conn.prepare(
        "SELECT source_position, record_bytes
         FROM cursor_store_raw_record
         WHERE source_epoch = ?1 AND source_position >= ?2
         ORDER BY source_position ASC
         LIMIT ?3",
    )?;
    let rows = statement.query_map(
        params![source_epoch.to_string(), start, max_records],
        |row| {
            let source_position: i64 = row.get(0)?;
            let bytes: Vec<u8> = row.get(1)?;
            Ok((source_position, bytes))
        },
    )?;
    let mut result = Vec::new();
    let mut total_bytes = 0u64;
    for row in rows {
        let (source_position, bytes) = row?;
        let source_position =
            u64::try_from(source_position).context("negative Cursor source position")?;
        let byte_len = u64::try_from(bytes.len()).context("Cursor record length exceeds u64")?;
        if byte_len > max_bytes {
            anyhow::bail!("one Cursor raw record exceeds the negotiated storage-v2 object bound");
        }
        if total_bytes
            .checked_add(byte_len)
            .context("Cursor raw byte count overflow")?
            > max_bytes
        {
            break;
        }
        total_bytes += byte_len;
        result.push(CursorRawRecord {
            source_position,
            bytes,
        });
    }
    Ok(result)
}

fn next_position(conn: &Connection, source_epoch: &str) -> Result<u64> {
    let highest: Option<i64> = conn.query_row(
        "SELECT MAX(source_position) FROM cursor_store_raw_record WHERE source_epoch = ?1",
        [source_epoch],
        |row| row.get(0),
    )?;
    match highest {
        Some(value) => u64::try_from(value)
            .context("negative Cursor source position")?
            .checked_add(1)
            .context("Cursor source position overflow"),
        None => Ok(0),
    }
}

fn hex_hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;

    #[test]
    fn exact_records_are_spooled_once_and_positions_are_contiguous() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(temp.path())).unwrap();
        let epoch = Uuid::new_v4();
        assert_eq!(
            append_unseen_cursor_records(
                &mut conn,
                epoch,
                &[b"meta-v1".to_vec(), b"blob-a".to_vec(), b"blob-a".to_vec()],
            )
            .unwrap(),
            2
        );
        assert_eq!(
            append_unseen_cursor_records(
                &mut conn,
                epoch,
                &[b"blob-a".to_vec(), b"root-b".to_vec()]
            )
            .unwrap(),
            3
        );
        assert_eq!(cursor_record_count(&conn, epoch).unwrap(), 3);
        assert_eq!(
            cursor_records_from(&conn, epoch, 0, 10, 1024).unwrap(),
            vec![
                CursorRawRecord {
                    source_position: 0,
                    bytes: b"meta-v1".to_vec()
                },
                CursorRawRecord {
                    source_position: 1,
                    bytes: b"blob-a".to_vec()
                },
                CursorRawRecord {
                    source_position: 2,
                    bytes: b"root-b".to_vec()
                },
            ]
        );
    }

    #[test]
    fn failed_receipt_retries_the_same_bounded_record_range() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(temp.path())).unwrap();
        let epoch = Uuid::new_v4();
        append_unseen_cursor_records(
            &mut conn,
            epoch,
            &[b"first".to_vec(), b"second".to_vec(), b"third".to_vec()],
        )
        .unwrap();
        let first = cursor_records_from(&conn, epoch, 0, 2, 1024).unwrap();
        let retry = cursor_records_from(&conn, epoch, 0, 2, 1024).unwrap();
        assert_eq!(first, retry);
        assert_eq!(
            cursor_records_from(&conn, epoch, 1, 10, 1024).unwrap(),
            vec![
                CursorRawRecord {
                    source_position: 1,
                    bytes: b"second".to_vec()
                },
                CursorRawRecord {
                    source_position: 2,
                    bytes: b"third".to_vec()
                },
            ]
        );
    }
}
