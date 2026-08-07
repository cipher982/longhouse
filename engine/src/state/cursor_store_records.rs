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
    // A Cursor capture may already hold a wider preparation savepoint so root
    // ordering, epoch selection, raw records, and pending-envelope persistence
    // commit atomically. Nested savepoints work in both contexts.
    let transaction = conn.savepoint()?;
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

/// Select the oldest epoch in one Cursor source lineage whose frozen records
/// extend beyond its receipt-gated durable cursor.
pub fn oldest_undrained_epoch(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<Option<Uuid>> {
    let epoch: Option<String> = conn
        .query_row(
            "SELECT epoch.source_epoch
             FROM source_epoch_registry AS epoch
             WHERE epoch.provider = ?1 AND epoch.opaque_source_id = ?2
               AND (SELECT COUNT(*) FROM cursor_store_raw_record AS raw
                    WHERE raw.source_epoch = epoch.source_epoch)
                   > COALESCE((SELECT lane.last_position
                               FROM source_epoch_lane_state AS lane
                               WHERE lane.source_epoch = epoch.source_epoch
                                 AND lane.lane = 'durable'), 0)
             ORDER BY epoch.created_at, epoch.source_epoch
             LIMIT 1",
            params![provider, opaque_source_id],
            |row| row.get(0),
        )
        .optional()?;
    epoch
        .map(|value| Uuid::parse_str(&value).context("undrained Cursor epoch is not a UUID"))
        .transpose()
}

/// Delete copied Cursor bytes for epochs the Runtime Host has fully receipted.
///
/// Claude and Codex are re-read from their `.jsonl` files by byte offset, so
/// the engine keeps only a cursor for them. Cursor keeps chats in its own blob
/// database, so its records must be copied out and linearized here before they
/// can be shipped. That copy is necessary while an epoch is in flight and dead
/// weight afterwards — and nothing ever deleted it. On the machine that
/// motivated this, `cursor_store_raw_record` was 8.5GB of 9.7GB, with 501,511
/// rows (5.97GB) belonging to epochs that had been fully receipted and ended.
///
/// The predicate is keyed on **positions, not row counts**. `last_position` is
/// exclusive: envelopes cover `[range_start, range_end)` and a receipt advances
/// the cursor to `range_end` (`pending_source_envelope.rs:894-903`), so an epoch
/// is fully durable exactly when it retains no record at or above it. The
/// count-based comparison in [`oldest_undrained_epoch`] is only equivalent
/// while rows are dense and undeleted, which stops being true the first time
/// this runs.
///
/// Safe against the lineage walk: `wire_predecessor_proof_for_epoch` returns on
/// `durable_position > 0` before it ever consults the raw-record count
/// (`source_epoch.rs:443-451`), and every epoch drained here has shipped, so it
/// can never be mistaken for an empty ancestor.
///
/// Historical supersession rows deliberately do not block a drain: they are an
/// audit trail with no production reader, not an in-flight operation.
pub fn drain_receipted_cursor_records(conn: &Connection) -> Result<u64> {
    let deleted = conn.execute(
        "DELETE FROM cursor_store_raw_record
         WHERE source_epoch IN (
             SELECT epoch.source_epoch
             FROM source_epoch_registry AS epoch
             JOIN source_epoch_lane_state AS durable
               ON durable.source_epoch = epoch.source_epoch
              AND durable.lane = 'durable'
             WHERE epoch.ended_at IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM pending_source_envelope AS pending
                   WHERE pending.source_epoch = epoch.source_epoch
               )
               AND NOT EXISTS (
                   SELECT 1 FROM cursor_store_raw_record AS retained
                   WHERE retained.source_epoch = epoch.source_epoch
                     AND retained.source_position >= durable.last_position
               )
         )",
        [],
    )?;
    u64::try_from(deleted).context("drained Cursor record count is negative")
}

pub fn cursor_record_hash(bytes: &[u8]) -> String {
    hex_hash(bytes)
}

pub fn cursor_record_exists(
    conn: &Connection,
    source_epoch: Uuid,
    record_hash: &str,
) -> Result<bool> {
    let found: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM cursor_store_raw_record WHERE source_epoch = ?1 AND record_hash = ?2",
            params![source_epoch.to_string(), record_hash],
            |row| row.get(0),
        )
        .optional()?;
    Ok(found.is_some())
}

pub fn capture_cursor(conn: &Connection, source_epoch: Uuid) -> Result<Option<String>> {
    let value: Option<Option<String>> = conn
        .query_row(
            "SELECT last_blob_id FROM cursor_store_capture_cursor WHERE source_epoch = ?1",
            [source_epoch.to_string()],
            |row| row.get(0),
        )
        .optional()
        .context("reading Cursor blob capture cursor")?;
    Ok(value.flatten())
}

pub fn store_capture_cursor(
    conn: &Connection,
    source_epoch: Uuid,
    last_blob_id: Option<&str>,
) -> Result<()> {
    conn.execute(
        "INSERT INTO cursor_store_capture_cursor (source_epoch, last_blob_id, updated_at)
         VALUES (?1, ?2, ?3)
         ON CONFLICT(source_epoch) DO UPDATE SET
             last_blob_id = excluded.last_blob_id,
             updated_at = excluded.updated_at",
        params![
            source_epoch.to_string(),
            last_blob_id,
            Utc::now().to_rfc3339()
        ],
    )?;
    Ok(())
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

    fn seed_epoch(conn: &Connection, epoch: Uuid) {
        let now = Utc::now().to_rfc3339();
        conn.execute(
            "INSERT INTO source_epoch_registry (
                source_epoch, provider, opaque_source_id, file_incarnation,
                start_reason, max_observed_len, created_at, updated_at
             ) VALUES (?1, 'cursor', ?2, 'fixture', 'fixture', 0, ?3, ?3)",
            params![epoch.to_string(), format!("fixture:{epoch}"), now],
        )
        .unwrap();
    }

    /// Set the durable cursor for an epoch. Exclusive: the next position that
    /// is *not* yet durable.
    fn set_durable_cursor(conn: &Connection, epoch: Uuid, last_position: u64) {
        conn.execute(
            "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
             VALUES (?1, 'durable', ?2, ?3)
             ON CONFLICT(source_epoch, lane) DO UPDATE SET last_position = ?2",
            params![
                epoch.to_string(),
                last_position as i64,
                Utc::now().to_rfc3339()
            ],
        )
        .unwrap();
    }

    fn end_epoch(conn: &Connection, epoch: Uuid) {
        conn.execute(
            "UPDATE source_epoch_registry SET ended_at = ?2 WHERE source_epoch = ?1",
            params![epoch.to_string(), Utc::now().to_rfc3339()],
        )
        .unwrap();
    }

    #[test]
    fn the_drain_removes_only_fully_receipted_ended_epochs() {
        // The whole risk of this feature is deleting evidence that was never
        // shipped, so every retention reason gets its own epoch here.
        let temp = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(temp.path())).unwrap();

        // (a) ended and fully receipted — the only one that should go.
        let drained = Uuid::new_v4();
        seed_epoch(&conn, drained);
        append_unseen_cursor_records(&mut conn, drained, &[b"a0".to_vec(), b"a1".to_vec()]).unwrap();
        set_durable_cursor(&conn, drained, 2);
        end_epoch(&conn, drained);

        // (b) ended, but one record sits at the cursor — never shipped.
        let partial = Uuid::new_v4();
        seed_epoch(&conn, partial);
        append_unseen_cursor_records(&mut conn, partial, &[b"b0".to_vec(), b"b1".to_vec()]).unwrap();
        set_durable_cursor(&conn, partial, 1);
        end_epoch(&conn, partial);

        // (c) ended and receipted, but an envelope is still pending.
        let pending = Uuid::new_v4();
        seed_epoch(&conn, pending);
        append_unseen_cursor_records(&mut conn, pending, &[b"c0".to_vec()]).unwrap();
        set_durable_cursor(&conn, pending, 1);
        end_epoch(&conn, pending);
        conn.execute(
            "INSERT INTO pending_source_envelope (
                 source_epoch, source_path, range_start, range_end, envelope_id,
                 request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                 has_reply_evidence, has_more, created_at
             ) VALUES (?1, '/tmp/c', 0, 1, 'env-c', x'00', x'00', 1, 1, 0, 0, ?2)",
            params![pending.to_string(), Utc::now().to_rfc3339()],
        )
        .unwrap();

        // (d) fully receipted but still open — the session may yet write more.
        let open = Uuid::new_v4();
        seed_epoch(&conn, open);
        append_unseen_cursor_records(&mut conn, open, &[b"d0".to_vec()]).unwrap();
        set_durable_cursor(&conn, open, 1);

        let deleted = drain_receipted_cursor_records(&conn).unwrap();

        assert_eq!(deleted, 2, "expected only the fully receipted ended epoch");
        assert_eq!(cursor_record_count(&conn, drained).unwrap(), 0);
        assert_eq!(cursor_record_count(&conn, partial).unwrap(), 2, "unshipped");
        assert_eq!(cursor_record_count(&conn, pending).unwrap(), 1, "pending");
        assert_eq!(cursor_record_count(&conn, open).unwrap(), 1, "still open");

        // Retained evidence must still be shippable byte-for-byte, not merely
        // present: a drain that corrupted the read path would pass a row count.
        assert_eq!(
            oldest_undrained_epoch(&conn, "cursor", &format!("fixture:{partial}")).unwrap(),
            Some(partial)
        );
        assert_eq!(
            cursor_records_from(&conn, partial, 1, 10, 1024).unwrap(),
            vec![CursorRawRecord {
                source_position: 1,
                bytes: b"b1".to_vec()
            }]
        );

        // Idempotent: the predicate is position-based, so a second pass over an
        // already-drained epoch finds nothing rather than mis-reading counts.
        assert_eq!(drain_receipted_cursor_records(&conn).unwrap(), 0);
    }

    #[test]
    fn exact_records_are_spooled_once_and_positions_are_contiguous() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(temp.path())).unwrap();
        let epoch = Uuid::new_v4();
        seed_epoch(&conn, epoch);
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
        seed_epoch(&conn, epoch);
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

    #[test]
    fn completed_blob_capture_cursor_round_trips_as_none() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(temp.path())).unwrap();
        let epoch = Uuid::new_v4();
        seed_epoch(&conn, epoch);

        store_capture_cursor(&conn, epoch, Some("blob-123")).unwrap();
        assert_eq!(
            capture_cursor(&conn, epoch).unwrap().as_deref(),
            Some("blob-123")
        );
        store_capture_cursor(&conn, epoch, None).unwrap();
        assert_eq!(capture_cursor(&conn, epoch).unwrap(), None);
    }

    #[test]
    fn oldest_undrained_epoch_preserves_lineage_order() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let mut conn = open_db(Some(temp.path())).unwrap();
        let first = Uuid::new_v4();
        let second = Uuid::new_v4();
        let now = Utc::now().to_rfc3339();
        for (epoch, created_at, ended_at) in [
            (
                first,
                "2026-07-21T00:00:00Z",
                Some("2026-07-21T00:00:30Z"),
            ),
            (second, "2026-07-21T00:01:00Z", None),
        ] {
            conn.execute(
                "INSERT INTO source_epoch_registry (
                    source_epoch, provider, opaque_source_id, file_incarnation,
                    start_reason, max_observed_len, created_at, updated_at, ended_at
                 ) VALUES (?1, 'cursor', 'shared-source', 'fixture', 'initial', 1, ?2, ?3, ?4)",
                params![epoch.to_string(), created_at, now, ended_at],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
                 VALUES (?1, 'durable', 0, ?2)",
                params![epoch.to_string(), now],
            )
            .unwrap();
            append_unseen_cursor_records(&mut conn, epoch, &[epoch.to_string().into_bytes()])
                .unwrap();
        }

        assert_eq!(
            oldest_undrained_epoch(&conn, "cursor", "shared-source").unwrap(),
            Some(first)
        );
        crate::state::source_epoch::acknowledge_position(
            &mut conn,
            first,
            crate::state::source_epoch::SourceLane::Durable,
            0,
            1,
        )
        .unwrap();
        assert_eq!(
            oldest_undrained_epoch(&conn, "cursor", "shared-source").unwrap(),
            Some(second)
        );
    }
}
