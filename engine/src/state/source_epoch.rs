//! Durable parser-independent source epochs shared by live and archive lanes.
//!
//! A path or provider source id is not an epoch: files can be replaced in
//! place, truncated, or replayed from an earlier cursor. This registry gives
//! both shipping lanes the same durable UUID while retaining every superseded
//! epoch as a predecessor chain.

#![allow(dead_code)] // Foundation is wired into shipping only at the v2 cutover.

use std::path::Path;

use anyhow::{bail, Context, Result};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use uuid::Uuid;

use super::file_identity::identity_from_metadata;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceLane {
    Live,
    Archive,
}

impl SourceLane {
    fn as_str(self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::Archive => "archive",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EpochStartReason {
    Initial,
    Replacement,
    Truncation,
    Rewind,
}

impl EpochStartReason {
    fn as_str(self) -> &'static str {
        match self {
            Self::Initial => "initial",
            Self::Replacement => "replacement",
            Self::Truncation => "truncation",
            Self::Rewind => "rewind",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceEpochResolution {
    pub source_epoch: Uuid,
    pub predecessor_epoch: Option<Uuid>,
    pub created: bool,
    pub start_reason: EpochStartReason,
}

#[derive(Debug, Clone)]
struct ActiveEpoch {
    source_epoch: Uuid,
    file_incarnation: String,
    predecessor_epoch: Option<Uuid>,
    start_reason: EpochStartReason,
    max_observed_len: u64,
}

/// Observe a real file and resolve its shared durable source epoch.
pub fn observe_file(
    conn: &mut Connection,
    provider: &str,
    opaque_source_id: &str,
    path: &Path,
    lane: SourceLane,
    position: u64,
) -> Result<SourceEpochResolution> {
    let metadata = path
        .metadata()
        .with_context(|| format!("reading source metadata: {}", path.display()))?;
    let incarnation = identity_from_metadata(&metadata).ok_or_else(|| {
        anyhow::anyhow!("source file has no stable incarnation: {}", path.display())
    })?;
    observe_source(
        conn,
        provider,
        opaque_source_id,
        &incarnation,
        metadata.len(),
        lane,
        position,
    )
}

/// Resolve an epoch from an already-captured file observation.
pub fn observe_source(
    conn: &mut Connection,
    provider: &str,
    opaque_source_id: &str,
    file_incarnation: &str,
    source_len: u64,
    lane: SourceLane,
    position: u64,
) -> Result<SourceEpochResolution> {
    if provider.is_empty() || opaque_source_id.is_empty() || file_incarnation.is_empty() {
        bail!("source epoch identity fields must be non-empty");
    }
    let observed_source_len = source_len;
    let observed_position = position;
    let source_len = i64::try_from(source_len).context("source length exceeds SQLite INTEGER")?;
    let position = i64::try_from(position).context("source position exceeds SQLite INTEGER")?;
    let now = Utc::now().to_rfc3339();
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;

    let active = load_active_epoch(&tx, provider, opaque_source_id)?;
    if active.is_none() && observed_position > observed_source_len {
        bail!(
            "source position {observed_position} exceeds new source length {observed_source_len}"
        );
    }
    let rotate_reason = if let Some(active) = &active {
        if active.file_incarnation != file_incarnation {
            Some(EpochStartReason::Replacement)
        } else if source_len < active.max_observed_len as i64 || position > source_len {
            Some(EpochStartReason::Truncation)
        } else {
            let prior_lane_position: Option<i64> = tx
                .query_row(
                    "SELECT last_position FROM source_epoch_lane_state
                     WHERE source_epoch = ?1 AND lane = ?2",
                    params![active.source_epoch.to_string(), lane.as_str()],
                    |row| row.get(0),
                )
                .optional()?;
            prior_lane_position
                .filter(|prior| position < *prior)
                .map(|_| EpochStartReason::Rewind)
        }
    } else {
        None
    };

    let resolved = match (active, rotate_reason) {
        (Some(active), None) => {
            tx.execute(
                "UPDATE source_epoch_registry
                 SET max_observed_len = MAX(max_observed_len, ?1), updated_at = ?2
                 WHERE source_epoch = ?3",
                params![source_len, now, active.source_epoch.to_string()],
            )?;
            let resolution = SourceEpochResolution {
                source_epoch: active.source_epoch,
                predecessor_epoch: active.predecessor_epoch,
                created: false,
                start_reason: active.start_reason,
            };
            resolution
        }
        (Some(active), Some(reason)) => {
            tx.execute(
                "UPDATE source_epoch_registry
                 SET ended_at = ?1, end_reason = ?2, updated_at = ?1
                 WHERE source_epoch = ?3",
                params![now, reason.as_str(), active.source_epoch.to_string()],
            )?;
            let next = Uuid::new_v4();
            insert_epoch(
                &tx,
                next,
                provider,
                opaque_source_id,
                file_incarnation,
                Some(active.source_epoch),
                reason,
                source_len,
                &now,
            )?;
            SourceEpochResolution {
                source_epoch: next,
                predecessor_epoch: Some(active.source_epoch),
                created: true,
                start_reason: reason,
            }
        }
        (None, _) => {
            let epoch = Uuid::new_v4();
            insert_epoch(
                &tx,
                epoch,
                provider,
                opaque_source_id,
                file_incarnation,
                None,
                EpochStartReason::Initial,
                source_len,
                &now,
            )?;
            SourceEpochResolution {
                source_epoch: epoch,
                predecessor_epoch: None,
                created: true,
                start_reason: EpochStartReason::Initial,
            }
        }
    };

    tx.execute(
        "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(source_epoch, lane) DO UPDATE SET
             last_position = excluded.last_position,
             updated_at = excluded.updated_at",
        params![
            resolved.source_epoch.to_string(),
            lane.as_str(),
            position.min(source_len),
            now
        ],
    )?;
    tx.commit()?;
    Ok(resolved)
}

fn load_active_epoch(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<Option<ActiveEpoch>> {
    conn.query_row(
        "SELECT source_epoch, file_incarnation, predecessor_epoch, start_reason, max_observed_len
         FROM source_epoch_registry
         WHERE provider = ?1 AND opaque_source_id = ?2 AND ended_at IS NULL",
        params![provider, opaque_source_id],
        |row| {
            let epoch: String = row.get(0)?;
            let predecessor: Option<String> = row.get(2)?;
            let reason: String = row.get(3)?;
            Ok((
                epoch,
                row.get(1)?,
                predecessor,
                reason,
                row.get::<_, i64>(4)?,
            ))
        },
    )
    .optional()?
    .map(|(epoch, incarnation, predecessor, reason, max_len)| {
        Ok(ActiveEpoch {
            source_epoch: Uuid::parse_str(&epoch)?,
            file_incarnation: incarnation,
            predecessor_epoch: predecessor
                .map(|value| Uuid::parse_str(&value))
                .transpose()?,
            start_reason: parse_reason(&reason)?,
            max_observed_len: u64::try_from(max_len).context("negative source length")?,
        })
    })
    .transpose()
}

#[allow(clippy::too_many_arguments)]
fn insert_epoch(
    conn: &Connection,
    epoch: Uuid,
    provider: &str,
    opaque_source_id: &str,
    file_incarnation: &str,
    predecessor: Option<Uuid>,
    reason: EpochStartReason,
    source_len: i64,
    now: &str,
) -> Result<()> {
    conn.execute(
        "INSERT INTO source_epoch_registry (
             source_epoch, provider, opaque_source_id, file_incarnation,
             predecessor_epoch, start_reason, max_observed_len, created_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)",
        params![
            epoch.to_string(),
            provider,
            opaque_source_id,
            file_incarnation,
            predecessor.map(|value| value.to_string()),
            reason.as_str(),
            source_len,
            now
        ],
    )?;
    Ok(())
}

fn parse_reason(value: &str) -> Result<EpochStartReason> {
    match value {
        "initial" => Ok(EpochStartReason::Initial),
        "replacement" => Ok(EpochStartReason::Replacement),
        "truncation" => Ok(EpochStartReason::Truncation),
        "rewind" => Ok(EpochStartReason::Rewind),
        other => bail!("invalid source epoch reason {other:?}"),
    }
}

#[cfg(test)]
mod tests {
    use std::fs::{self, OpenOptions};
    use std::io::Write;

    use super::*;

    #[test]
    fn epoch_survives_restart_and_is_shared_across_lanes() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let source = dir.path().join("history.jsonl");
        fs::write(&source, b"one\ntwo\n").unwrap();
        let mut conn = crate::state::db::open_db(Some(&db_path)).unwrap();

        let archive = observe_file(
            &mut conn,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Archive,
            4,
        )
        .unwrap();
        let live = observe_file(
            &mut conn,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Live,
            8,
        )
        .unwrap();
        assert_eq!(archive.source_epoch, live.source_epoch);
        drop(conn);

        let mut reopened = crate::state::db::open_db(Some(&db_path)).unwrap();
        let after_restart = observe_file(
            &mut reopened,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Archive,
            8,
        )
        .unwrap();
        assert_eq!(archive.source_epoch, after_restart.source_epoch);
        assert!(!after_restart.created);
    }

    #[test]
    fn replacement_truncation_and_lane_rewind_create_predecessor_chain() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("state.db");
        let source = dir.path().join("history.jsonl");
        fs::write(&source, b"one\ntwo\n").unwrap();
        let mut conn = crate::state::db::open_db(Some(&db_path)).unwrap();

        let first = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Archive,
            8,
        )
        .unwrap();

        fs::rename(&source, dir.path().join("history.old")).unwrap();
        fs::write(&source, b"new\nrecord\n").unwrap();
        let replacement = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Archive,
            0,
        )
        .unwrap();
        assert_eq!(replacement.start_reason, EpochStartReason::Replacement);
        assert_eq!(replacement.predecessor_epoch, Some(first.source_epoch));

        OpenOptions::new()
            .write(true)
            .open(&source)
            .unwrap()
            .set_len(3)
            .unwrap();
        let truncation = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Archive,
            8,
        )
        .unwrap();
        assert_eq!(truncation.start_reason, EpochStartReason::Truncation);
        assert_eq!(truncation.predecessor_epoch, Some(replacement.source_epoch));

        let mut file = OpenOptions::new().append(true).open(&source).unwrap();
        file.write_all(b"more bytes").unwrap();
        observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Live,
            8,
        )
        .unwrap();
        let rewind = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Live,
            2,
        )
        .unwrap();
        assert_eq!(rewind.start_reason, EpochStartReason::Rewind);
        assert_eq!(rewind.predecessor_epoch, Some(truncation.source_epoch));

        let retained: i64 = conn
            .query_row("SELECT COUNT(*) FROM source_epoch_registry", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(retained, 4);
    }
}
