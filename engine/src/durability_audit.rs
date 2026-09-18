//! Independent durability oracle for the storage-v2 boundary.
//!
//! Every expectation here is re-derived from the two things that are not the
//! engine's own claim about itself: the **source file** on disk, and the
//! **sealed payload files** under the payload root. The lane cursor and the
//! host's `accepted_through` are the claims under test, never the expectation.
//!
//! The spec's rule is the reason for that split. `green integrity_check`, zero
//! lock errors and healthy heartbeats disprove nothing about whether the bytes
//! that were supposed to be durable are the bytes that exist — so this audit
//! reads the source, re-frames it the way the shipping path frames it, re-hashes
//! the payload files from their own contents, and only then compares.
//!
//! Two things it cannot check, and says so instead of guessing:
//! - a source whose path was never recorded is `no_source_path`;
//! - an epoch with no host receipt is audited locally and marked
//!   `host_receipt_missing`, because a missing receipt is not a mismatch.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use rusqlite::Connection;

use crate::raw_records::{read_next_raw_batch_with_limits, RawSourceFraming, MAX_RAW_BATCH_BYTES};
use crate::state::payload_store;
use crate::storage_v2_contract::hash_record;

/// One epoch's audit. Serialized directly as the command's JSON.
#[derive(Debug, Clone, serde::Serialize)]
pub struct EpochAudit {
    pub source_epoch: String,
    pub provider: String,
    pub opaque_source_id: String,
    pub source_path: Option<String>,
    /// The claim: how far this machine believes it has durably shipped.
    pub lane_cursor: u64,
    /// The claim from the other side, when a receipt was supplied.
    pub host_accepted_through: Option<u64>,
    /// Re-derived from the file itself.
    pub source_len: Option<u64>,
    pub records_in_prefix: Option<u64>,
    /// SHA-256 over the framed record bytes of the audited prefix. Evidence, not
    /// a comparison: neither side stores a whole-prefix hash today.
    pub prefix_sha256: Option<String>,
    pub prefix_bytes_audited: Option<u64>,
    pub payload_files: usize,
    pub payload_bytes: u64,
    /// Evidence that could not be taken, with the reason. Not an alarm: an
    /// audit that cannot observe something says so rather than guessing.
    pub notes: Vec<String>,
    pub alarms: Vec<String>,
}

/// `clean` — every check ran and passed. `unverifiable` — a check could not be
/// taken (no recorded source path). `alarmed` — a check ran and failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EpochStatus {
    Clean,
    Unverifiable,
    Alarmed,
}

impl EpochAudit {
    fn alarm(&mut self, code: &str, detail: String) {
        self.alarms.push(format!("{code}: {detail}"));
    }

    fn note(&mut self, code: &str, detail: String) {
        self.notes.push(format!("{code}: {detail}"));
    }

    pub fn status(&self) -> EpochStatus {
        if !self.alarms.is_empty() {
            EpochStatus::Alarmed
        } else if self.source_path.is_none() {
            EpochStatus::Unverifiable
        } else {
            EpochStatus::Clean
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct DurabilityAuditReport {
    pub database: String,
    pub payload_root: String,
    pub audited_epochs: usize,
    pub clean_epochs: usize,
    pub unverifiable_epochs: usize,
    pub alarmed_epochs: usize,
    pub epochs: Vec<EpochAudit>,
}

impl DurabilityAuditReport {
    /// Only a check that ran and failed makes the audit fail. An epoch with no
    /// recorded source path is counted and named, not treated as a defect.
    pub fn is_clean(&self) -> bool {
        self.alarmed_epochs == 0
    }
}

/// Audit every epoch in the ledger, with optional host receipts keyed by
/// `source_epoch`, and an optional cap on how many prefix bytes to re-frame.
pub fn audit(
    db_path: &Path,
    receipts: &HashMap<String, u64>,
    sample_bytes: Option<u64>,
    limit: Option<usize>,
) -> Result<DurabilityAuditReport> {
    let conn = Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("opening {} for the durability audit", db_path.display()))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    let payload_root = payload_store::root_for_db(db_path);

    let mut epochs = load_epochs(&conn)?;
    epochs.reverse();
    if let Some(limit) = limit {
        epochs.truncate(limit);
    }
    let mut audits = Vec::with_capacity(epochs.len());
    for epoch in epochs {
        audits.push(audit_epoch(
            &conn,
            &payload_root,
            &epoch,
            receipts.get(&epoch.source_epoch).copied(),
            sample_bytes,
        ));
    }
    let clean_epochs = audits
        .iter()
        .filter(|audit| audit.status() == EpochStatus::Clean)
        .count();
    let unverifiable_epochs = audits
        .iter()
        .filter(|audit| audit.status() == EpochStatus::Unverifiable)
        .count();
    let alarmed_epochs = audits
        .iter()
        .filter(|audit| audit.status() == EpochStatus::Alarmed)
        .count();
    Ok(DurabilityAuditReport {
        database: db_path.display().to_string(),
        payload_root: payload_root.display().to_string(),
        audited_epochs: audits.len(),
        clean_epochs,
        unverifiable_epochs,
        alarmed_epochs,
        epochs: audits,
    })
}

struct EpochRow {
    source_epoch: String,
    provider: String,
    opaque_source_id: String,
    max_observed_len: i64,
    bound_session_id: Option<String>,
}

fn load_epochs(conn: &Connection) -> Result<Vec<EpochRow>> {
    let mut stmt = conn.prepare(
        "SELECT source_epoch, provider, opaque_source_id, max_observed_len, bound_session_id
         FROM source_epoch_registry
         ORDER BY created_at",
    )?;
    let rows = stmt
        .query_map([], |row| {
            Ok(EpochRow {
                source_epoch: row.get(0)?,
                provider: row.get(1)?,
                opaque_source_id: row.get(2)?,
                max_observed_len: row.get(3)?,
                bound_session_id: row.get(4)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    Ok(rows)
}

fn audit_epoch(
    conn: &Connection,
    payload_root: &Path,
    epoch: &EpochRow,
    host_accepted_through: Option<u64>,
    sample_bytes: Option<u64>,
) -> EpochAudit {
    let lane_cursor = conn
        .query_row(
            "SELECT last_position FROM source_epoch_lane_state
             WHERE source_epoch = ?1 AND lane = 'durable'",
            [epoch.source_epoch.as_str()],
            |row| row.get::<_, i64>(0),
        )
        .unwrap_or(0)
        .max(0) as u64;

    let source_path = recorded_source_path(conn, epoch);
    let mut audit = EpochAudit {
        source_epoch: epoch.source_epoch.clone(),
        provider: epoch.provider.clone(),
        opaque_source_id: epoch.opaque_source_id.clone(),
        source_path: source_path
            .as_ref()
            .map(|path| path.display().to_string()),
        lane_cursor,
        host_accepted_through,
        source_len: None,
        records_in_prefix: None,
        prefix_sha256: None,
        prefix_bytes_audited: None,
        payload_files: 0,
        payload_bytes: 0,
        notes: Vec::new(),
        alarms: Vec::new(),
    };

    let Some(source_path) = source_path else {
        audit.note(
            "no_source_path",
            "neither a retained envelope nor a session binding names the source, so the \
             cursor cannot be checked against the file it describes"
                .to_string(),
        );
        return audit;
    };

    match std::fs::metadata(&source_path) {
        Ok(meta) => {
            let len = meta.len();
            audit.source_len = Some(len);
            if lane_cursor > len {
                audit.alarm(
                    "cursor_past_source_end",
                    format!("lane cursor {lane_cursor} exceeds source length {len}"),
                );
            }
            if len < epoch.max_observed_len.max(0) as u64 {
                audit.alarm(
                    "source_shrank_below_max_observed",
                    format!(
                        "source is {len} bytes but was observed at {} — the file was replaced \
                         or truncated under a live epoch",
                        epoch.max_observed_len
                    ),
                );
            }
            let limit = lane_cursor.min(len);
            let prefix_limit = sample_bytes.map_or(limit, |sample| limit.min(sample));
            match digest_prefix(&source_path, prefix_limit) {
                Ok((records, digest, bytes)) => {
                    audit.records_in_prefix = Some(records);
                    audit.prefix_sha256 = Some(digest);
                    audit.prefix_bytes_audited = Some(bytes);
                }
                Err(err) => audit.alarm("prefix_unreadable", format!("{err:#}")),
            }
        }
        Err(err) => audit.alarm(
            "source_missing",
            format!("{} cannot be read: {err}", source_path.display()),
        ),
    }

    if let Some(accepted) = host_accepted_through {
        if accepted > audit.source_len.unwrap_or(u64::MAX) {
            audit.alarm(
                "host_past_source_end",
                format!(
                    "host accepted_through {accepted} exceeds source length {}",
                    audit.source_len.unwrap_or(0)
                ),
            );
        }
        if lane_cursor > accepted {
            audit.alarm(
                "local_cursor_ahead_of_host",
                format!("lane cursor {lane_cursor} is ahead of accepted_through {accepted}"),
            );
        }
    } else {
        audit.note(
            "host_receipt_missing",
            "no host receipt supplied for this epoch; local evidence only".to_string(),
        );
    }

    audit_payloads(conn, payload_root, epoch, &mut audit);
    audit
}

/// The source a cursor describes: the retained envelope names it while work is
/// pending, and the binding names it once the work is done.
fn recorded_source_path(conn: &Connection, epoch: &EpochRow) -> Option<PathBuf> {
    let pending = conn
        .query_row(
            "SELECT source_path FROM pending_source_envelope WHERE source_epoch = ?1",
            [epoch.source_epoch.as_str()],
            |row| row.get::<_, String>(0),
        )
        .ok();
    if let Some(path) = pending {
        return Some(PathBuf::from(path));
    }
    let session_id = epoch.bound_session_id.as_deref()?;
    let path = conn
        .query_row(
            "SELECT path FROM session_binding WHERE session_id = ?1 AND provider = ?2
             ORDER BY updated_at DESC LIMIT 1",
            rusqlite::params![session_id, epoch.provider],
            |row| row.get::<_, String>(0),
        )
        .ok()?;
    Some(PathBuf::from(path))
}

/// Re-frame the source prefix exactly the way the shipping path frames it, and
/// return the record count plus a digest over the framed record bytes.
fn digest_prefix(path: &Path, limit: u64) -> Result<(u64, String, u64)> {
    let mut offset = 0_u64;
    let mut records = 0_u64;
    let mut hasher = sha2::Sha256::new();
    use sha2::Digest;
    while offset < limit {
        let Some(batch) = read_next_raw_batch_with_limits(
            path,
            RawSourceFraming::LfDelimited,
            offset,
            MAX_RAW_BATCH_BYTES,
            MAX_RAW_BATCH_BYTES,
        )?
        else {
            break;
        };
        if batch.range_end <= offset {
            break;
        }
        for record in &batch.records {
            if record.range_start >= limit {
                break;
            }
            records += 1;
            hasher.update(hash_record(&record.bytes));
        }
        offset = batch.range_end.min(limit);
    }
    Ok((records, format!("{:x}", hasher.finalize()), offset))
}

/// Re-read every payload file this epoch still references and re-hash it from
/// its own bytes. A missing file or a hash that does not match what the row
/// recorded is the alarm; the row's recorded hash is the claim.
fn audit_payloads(
    conn: &Connection,
    payload_root: &Path,
    epoch: &EpochRow,
    audit: &mut EpochAudit,
) {
    let rows = conn
        .prepare(
            "SELECT request_body_path, request_body_sha256, media_objects_path, media_objects_sha256
             FROM pending_source_envelope WHERE source_epoch = ?1",
        )
        .and_then(|mut stmt| {
            stmt.query_map([epoch.source_epoch.as_str()], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()
        });
    let Ok(rows) = rows else {
        audit.alarm(
            "payload_rows_unreadable",
            "the retained envelope row could not be read".to_string(),
        );
        return;
    };
    for (body_path, body_sha, media_path, media_sha) in rows {
        for (label, relative, expected) in [
            ("request_body", body_path, body_sha),
            ("media_objects", media_path, media_sha),
        ] {
            let (Some(relative), Some(expected)) = (relative, expected) else {
                continue;
            };
            match payload_store::read(payload_root, &relative, &expected) {
                Ok(bytes) => {
                    audit.payload_files += 1;
                    audit.payload_bytes += bytes.len() as u64;
                }
                Err(err) => audit.alarm(
                    "payload_unverifiable",
                    format!("{label} {relative}: {err:#}"),
                ),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;

    fn seed_epoch(conn: &Connection, epoch: &str, source: &Path, cursor: u64, observed_len: u64) {
        conn.execute(
            "INSERT INTO source_epoch_registry (
                 source_epoch, provider, opaque_source_id, file_incarnation, start_reason,
                 max_observed_len, created_at, updated_at
             ) VALUES (?1, 'claude', 'opaque-1', 'incarnation', 'initial', ?2, ?3, ?3)",
            rusqlite::params![epoch, observed_len as i64, "2026-09-18T00:00:00Z"],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
             VALUES (?1, 'durable', ?2, '2026-09-18T00:00:00Z')",
            rusqlite::params![epoch, cursor as i64],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO pending_source_envelope (
                 source_epoch, source_path, range_start, range_end, envelope_id,
                 request_body_zstd, media_objects_zstd, raw_bytes, event_count,
                 has_reply_evidence, has_more, created_at
             ) VALUES (?1, ?2, 0, ?3, 'env-1', X'', X'', ?3, 1, 0, 0, ?4)",
            rusqlite::params![
                epoch,
                source.display().to_string(),
                cursor as i64,
                "2026-09-18T00:00:00Z"
            ],
        )
        .unwrap();
    }

    #[test]
    fn a_cursor_within_the_source_is_clean() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("state.db");
        let conn = open_db(Some(&db)).unwrap();
        let source = dir.path().join("session.jsonl");
        std::fs::write(&source, b"one\ntwo\nthree\n").unwrap();
        seed_epoch(&conn, "epoch-1", &source, 8, 14);
        drop(conn);

        let report = audit(&db, &HashMap::from([("epoch-1".to_string(), 8)]), None, None).unwrap();
        let epoch = &report.epochs[0];
        assert_eq!(epoch.status(), EpochStatus::Clean, "{:?}", epoch.alarms);
        assert_eq!(epoch.records_in_prefix, Some(2), "the prefix is two records");
        assert_eq!(epoch.source_len, Some(14));
    }

    #[test]
    fn a_cursor_past_the_source_is_an_alarm() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("state.db");
        let conn = open_db(Some(&db)).unwrap();
        let source = dir.path().join("session.jsonl");
        std::fs::write(&source, b"one\n").unwrap();
        seed_epoch(&conn, "epoch-1", &source, 400, 4);
        drop(conn);

        let report = audit(&db, &HashMap::new(), None, None).unwrap();
        let alarms = &report.epochs[0].alarms;
        assert!(
            alarms.iter().any(|alarm| alarm.starts_with("cursor_past_source_end")),
            "{alarms:?}"
        );
        assert!(!report.is_clean());
    }

    #[test]
    fn a_host_receipt_behind_the_local_cursor_is_an_alarm() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("state.db");
        let conn = open_db(Some(&db)).unwrap();
        let source = dir.path().join("session.jsonl");
        std::fs::write(&source, b"one\ntwo\n").unwrap();
        seed_epoch(&conn, "epoch-1", &source, 8, 8);
        drop(conn);

        let report = audit(&db, &HashMap::from([("epoch-1".to_string(), 4)]), None, None).unwrap();
        assert!(report.epochs[0]
            .alarms
            .iter()
            .any(|alarm| alarm.starts_with("local_cursor_ahead_of_host")));
    }

    #[test]
    fn a_payload_file_that_does_not_match_its_hash_is_an_alarm() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("state.db");
        let conn = open_db(Some(&db)).unwrap();
        let source = dir.path().join("session.jsonl");
        std::fs::write(&source, b"one\n").unwrap();
        seed_epoch(&conn, "epoch-1", &source, 4, 4);
        let root = payload_store::root_for_db(&db);
        let sealed = payload_store::seal(&root, b"{\"body\":true}").unwrap();
        conn.execute(
            "UPDATE pending_source_envelope
             SET request_body_path = ?1, request_body_sha256 = ?2 WHERE source_epoch = 'epoch-1'",
            rusqlite::params![sealed.relative_path, sealed.sha256],
        )
        .unwrap();
        drop(conn);

        // A payload that matches is counted.
        let report = audit(&db, &HashMap::from([("epoch-1".to_string(), 4)]), None, None).unwrap();
        assert_eq!(report.epochs[0].payload_files, 1, "{:?}", report.epochs[0].alarms);

        // Rewriting the file under its recorded hash is the alarm.
        std::fs::write(root.join(&sealed.relative_path), b"{\"body\":false}").unwrap();
        let report = audit(&db, &HashMap::from([("epoch-1".to_string(), 4)]), None, None).unwrap();
        assert!(
            report.epochs[0]
                .alarms
                .iter()
                .any(|alarm| alarm.starts_with("payload_unverifiable")),
            "{:?}",
            report.epochs[0].alarms
        );
    }
}
