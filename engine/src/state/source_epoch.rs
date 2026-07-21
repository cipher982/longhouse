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
    Durable,
}

impl SourceLane {
    fn as_str(self) -> &'static str {
        match self {
            Self::Durable => "durable",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EpochStartReason {
    Initial,
    Replacement,
    Truncation,
    RevisionChange,
    Rewrite,
    Rewind,
    SessionRebind,
    HostAuthorityReconciled,
}

impl EpochStartReason {
    fn as_str(self) -> &'static str {
        match self {
            Self::Initial => "initial",
            Self::Replacement => "replacement",
            Self::Truncation => "truncation",
            Self::RevisionChange => "revision_change",
            Self::Rewrite => "rewrite",
            Self::Rewind => "rewind",
            Self::SessionRebind => "session_rebind",
            Self::HostAuthorityReconciled => "host_authority_reconciled",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SourceChangeHint {
    None,
    Rewrite,
    Rewind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SourceEpochResolution {
    pub source_epoch: Uuid,
    pub predecessor_epoch: Option<Uuid>,
    pub created: bool,
    pub start_reason: EpochStartReason,
    pub opened_at: String,
    pub bound_session_id: Option<String>,
}

#[derive(Debug, Clone)]
struct ActiveEpoch {
    source_epoch: Uuid,
    file_incarnation: String,
    predecessor_epoch: Option<Uuid>,
    start_reason: EpochStartReason,
    max_observed_len: u64,
    source_revision: Option<String>,
    bound_session_id: Option<String>,
    opened_at: String,
}

/// Observe a real file and resolve its shared durable source epoch.
pub fn observe_file(
    conn: &mut Connection,
    provider: &str,
    opaque_source_id: &str,
    path: &Path,
    lane: SourceLane,
    position: u64,
    source_revision: Option<&str>,
    bound_session_id: Option<&str>,
    change_hint: SourceChangeHint,
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
        source_revision,
        bound_session_id,
        change_hint,
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
    source_revision: Option<&str>,
    bound_session_id: Option<&str>,
    change_hint: SourceChangeHint,
) -> Result<SourceEpochResolution> {
    if provider.is_empty() || opaque_source_id.is_empty() || file_incarnation.is_empty() {
        bail!("source epoch identity fields must be non-empty");
    }
    let observed_source_len = source_len;
    let observed_position = position;
    let source_revision = source_revision
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let bound_session_id = bound_session_id
        .map(str::trim)
        .filter(|value| !value.is_empty());
    let source_len = i64::try_from(source_len).context("source length exceeds SQLite INTEGER")?;
    let position = i64::try_from(position).context("source position exceeds SQLite INTEGER")?;
    let now = Utc::now().to_rfc3339();
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;

    let active = load_active_epoch(&tx, provider, opaque_source_id)?;
    let active_lane_position = match active.as_ref() {
        Some(active) => load_lane_position(&tx, active.source_epoch, lane)?.unwrap_or(0),
        None => 0,
    };
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
        } else if active.source_revision.as_deref().is_some()
            && source_revision.is_some()
            && active.source_revision.as_deref() != source_revision
        {
            Some(EpochStartReason::RevisionChange)
        } else if bound_session_id.is_some()
            && active.bound_session_id.as_deref() != bound_session_id
            && (active.bound_session_id.is_some() || active_lane_position > 0)
        {
            Some(EpochStartReason::SessionRebind)
        } else {
            match change_hint {
                SourceChangeHint::None => None,
                SourceChangeHint::Rewrite => Some(EpochStartReason::Rewrite),
                SourceChangeHint::Rewind => Some(EpochStartReason::Rewind),
            }
        }
    } else {
        None
    };

    let resolved = match (active, rotate_reason) {
        (Some(active), None) => {
            tx.execute(
                "UPDATE source_epoch_registry
                 SET max_observed_len = MAX(max_observed_len, ?1),
                     source_revision = COALESCE(?2, source_revision),
                     bound_session_id = COALESCE(?3, bound_session_id),
                     updated_at = ?4
                 WHERE source_epoch = ?5",
                params![
                    source_len,
                    source_revision,
                    bound_session_id,
                    now,
                    active.source_epoch.to_string()
                ],
            )?;
            let resolution = SourceEpochResolution {
                source_epoch: active.source_epoch,
                predecessor_epoch: active.predecessor_epoch,
                created: false,
                start_reason: active.start_reason,
                opened_at: active.opened_at,
                bound_session_id: active
                    .bound_session_id
                    .or_else(|| bound_session_id.map(str::to_string)),
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
                source_revision,
                bound_session_id,
                &now,
            )?;
            SourceEpochResolution {
                source_epoch: next,
                predecessor_epoch: Some(active.source_epoch),
                created: true,
                start_reason: reason,
                opened_at: now.clone(),
                bound_session_id: bound_session_id.map(str::to_string),
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
                source_revision,
                bound_session_id,
                &now,
            )?;
            SourceEpochResolution {
                source_epoch: epoch,
                predecessor_epoch: None,
                created: true,
                start_reason: EpochStartReason::Initial,
                opened_at: now.clone(),
                bound_session_id: bound_session_id.map(str::to_string),
            }
        }
    };

    let initial_position = if resolved.created && resolved.start_reason != EpochStartReason::Initial
    {
        0
    } else {
        position.min(source_len)
    };
    tx.execute(
        "INSERT INTO source_epoch_lane_state (source_epoch, lane, last_position, updated_at)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(source_epoch, lane) DO NOTHING",
        params![
            resolved.source_epoch.to_string(),
            lane.as_str(),
            initial_position,
            now
        ],
    )?;
    tx.commit()?;
    Ok(resolved)
}

pub fn lane_position(conn: &Connection, source_epoch: Uuid, lane: SourceLane) -> Result<u64> {
    load_lane_position(conn, source_epoch, lane)?
        .ok_or_else(|| anyhow::anyhow!("source epoch lane is not registered"))
}

pub fn active_source_incarnation(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<Option<String>> {
    Ok(load_active_epoch(conn, provider, opaque_source_id)?.map(|epoch| epoch.file_incarnation))
}

fn load_lane_position(
    conn: &Connection,
    source_epoch: Uuid,
    lane: SourceLane,
) -> Result<Option<u64>> {
    conn.query_row(
        "SELECT last_position FROM source_epoch_lane_state WHERE source_epoch = ?1 AND lane = ?2",
        params![source_epoch.to_string(), lane.as_str()],
        |row| row.get::<_, i64>(0),
    )
    .optional()?
    .map(|value| u64::try_from(value).context("source epoch lane position is negative"))
    .transpose()
}

pub fn acknowledge_position(
    conn: &mut Connection,
    source_epoch: Uuid,
    lane: SourceLane,
    expected_start: u64,
    acknowledged_through: u64,
) -> Result<()> {
    if acknowledged_through < expected_start {
        bail!("source epoch acknowledgement cannot move backward");
    }
    let expected_start =
        i64::try_from(expected_start).context("source position exceeds SQLite INTEGER")?;
    let acknowledged_through =
        i64::try_from(acknowledged_through).context("source position exceeds SQLite INTEGER")?;
    let changed = conn.execute(
        "UPDATE source_epoch_lane_state
         SET last_position = ?1, updated_at = ?2
         WHERE source_epoch = ?3 AND lane = ?4 AND last_position = ?5",
        params![
            acknowledged_through,
            Utc::now().to_rfc3339(),
            source_epoch.to_string(),
            lane.as_str(),
            expected_start
        ],
    )?;
    if changed != 1 {
        bail!("source epoch lane cursor changed before acknowledgement");
    }
    Ok(())
}

fn load_active_epoch(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<Option<ActiveEpoch>> {
    conn.query_row(
        "SELECT source_epoch, file_incarnation, predecessor_epoch, start_reason,
                max_observed_len, source_revision, bound_session_id, created_at
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
                row.get::<_, Option<String>>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, String>(7)?,
            ))
        },
    )
    .optional()?
    .map(
        |(
            epoch,
            incarnation,
            predecessor,
            reason,
            max_len,
            source_revision,
            bound_session_id,
            opened_at,
        )| {
            Ok(ActiveEpoch {
                source_epoch: Uuid::parse_str(&epoch)?,
                file_incarnation: incarnation,
                predecessor_epoch: predecessor
                    .map(|value| Uuid::parse_str(&value))
                    .transpose()?,
                start_reason: parse_reason(&reason)?,
                max_observed_len: u64::try_from(max_len).context("negative source length")?,
                source_revision,
                bound_session_id,
                opened_at,
            })
        },
    )
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
    source_revision: Option<&str>,
    bound_session_id: Option<&str>,
    now: &str,
) -> Result<()> {
    conn.execute(
        "INSERT INTO source_epoch_registry (
             source_epoch, provider, opaque_source_id, file_incarnation,
             predecessor_epoch, start_reason, max_observed_len, source_revision,
             bound_session_id, created_at, updated_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?10)",
        params![
            epoch.to_string(),
            provider,
            opaque_source_id,
            file_incarnation,
            predecessor.map(|value| value.to_string()),
            reason.as_str(),
            source_len,
            source_revision,
            bound_session_id,
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
        "revision_change" => Ok(EpochStartReason::RevisionChange),
        "rewrite" => Ok(EpochStartReason::Rewrite),
        "rewind" => Ok(EpochStartReason::Rewind),
        "session_rebind" => Ok(EpochStartReason::SessionRebind),
        "host_authority_reconciled" => Ok(EpochStartReason::HostAuthorityReconciled),
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
            SourceLane::Durable,
            4,
            Some("provider-revision-1"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        let live = observe_file(
            &mut conn,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            8,
            Some("provider-revision-1"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        assert_eq!(archive.source_epoch, live.source_epoch);
        assert_eq!(
            lane_position(&conn, archive.source_epoch, SourceLane::Durable).unwrap(),
            4
        );
        assert!(!archive.opened_at.is_empty());
        drop(conn);

        let mut reopened = crate::state::db::open_db(Some(&db_path)).unwrap();
        let after_restart = observe_file(
            &mut reopened,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            2,
            Some("provider-revision-1"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        assert_eq!(archive.source_epoch, after_restart.source_epoch);
        assert!(!after_restart.created);
        assert_eq!(after_restart.opened_at, archive.opened_at);
        assert_eq!(
            lane_position(&reopened, archive.source_epoch, SourceLane::Durable).unwrap(),
            4
        );
        acknowledge_position(
            &mut reopened,
            archive.source_epoch,
            SourceLane::Durable,
            4,
            8,
        )
        .unwrap();
        assert_eq!(
            lane_position(&reopened, archive.source_epoch, SourceLane::Durable).unwrap(),
            8
        );
    }

    #[test]
    fn host_authority_reconciled_epoch_remains_readable() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("history.jsonl");
        fs::write(&source, b"one\ntwo\n").unwrap();
        let mut conn = crate::state::db::open_db(None).unwrap();

        let initial = observe_file(
            &mut conn,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            None,
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        conn.execute(
            "UPDATE source_epoch_registry
             SET start_reason = 'host_authority_reconciled'
             WHERE source_epoch = ?1",
            [initial.source_epoch.to_string()],
        )
        .unwrap();

        let reconciled = observe_file(
            &mut conn,
            "claude",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            None,
            None,
            SourceChangeHint::None,
        )
        .unwrap();

        assert_eq!(reconciled.source_epoch, initial.source_epoch);
        assert_eq!(
            reconciled.start_reason,
            EpochStartReason::HostAuthorityReconciled
        );
        assert!(!reconciled.created);
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
            SourceLane::Durable,
            8,
            Some("revision-1"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();

        fs::rename(&source, dir.path().join("history.old")).unwrap();
        fs::write(&source, b"new\nrecord\n").unwrap();
        let replacement = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            Some("revision-2"),
            None,
            SourceChangeHint::None,
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
            SourceLane::Durable,
            8,
            Some("revision-2"),
            None,
            SourceChangeHint::None,
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
            SourceLane::Durable,
            8,
            Some("revision-2"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        let ordinary_retry = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Durable,
            2,
            Some("revision-2"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        assert_eq!(ordinary_retry.source_epoch, truncation.source_epoch);
        assert!(!ordinary_retry.created);

        let rewind = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Durable,
            2,
            Some("revision-2"),
            None,
            SourceChangeHint::Rewind,
        )
        .unwrap();
        assert_eq!(rewind.start_reason, EpochStartReason::Rewind);
        assert_eq!(rewind.predecessor_epoch, Some(truncation.source_epoch));

        fs::write(&source, b"ABCDEFGHIJKLM").unwrap();
        let revision_change = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            Some("revision-3"),
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        assert_eq!(
            revision_change.start_reason,
            EpochStartReason::RevisionChange
        );
        assert_eq!(revision_change.predecessor_epoch, Some(rewind.source_epoch));

        let explicit_rewrite = observe_file(
            &mut conn,
            "codex",
            "/stable/history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            Some("revision-3"),
            None,
            SourceChangeHint::Rewrite,
        )
        .unwrap();
        assert_eq!(explicit_rewrite.start_reason, EpochStartReason::Rewrite);
        assert_eq!(
            explicit_rewrite.predecessor_epoch,
            Some(revision_change.source_epoch)
        );

        let retained: i64 = conn
            .query_row("SELECT COUNT(*) FROM source_epoch_registry", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(retained, 6);
    }

    #[test]
    fn late_managed_binding_rotates_once_and_survives_hintless_repair() {
        let dir = tempfile::tempdir().unwrap();
        let source = dir.path().join("history.jsonl");
        fs::write(&source, b"one\ntwo\n").unwrap();
        let mut conn = crate::state::db::open_db(None).unwrap();

        let parsed = observe_file(
            &mut conn,
            "codex",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            None,
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        acknowledge_position(&mut conn, parsed.source_epoch, SourceLane::Durable, 0, 4).unwrap();
        let managed = observe_file(
            &mut conn,
            "codex",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            None,
            Some("managed-session"),
            SourceChangeHint::None,
        )
        .unwrap();

        assert_eq!(managed.start_reason, EpochStartReason::SessionRebind);
        assert_eq!(managed.predecessor_epoch, Some(parsed.source_epoch));
        assert_eq!(managed.bound_session_id.as_deref(), Some("managed-session"));
        assert_eq!(
            lane_position(&conn, managed.source_epoch, SourceLane::Durable).unwrap(),
            0
        );

        let repair = observe_file(
            &mut conn,
            "codex",
            "history.jsonl",
            &source,
            SourceLane::Durable,
            0,
            None,
            None,
            SourceChangeHint::None,
        )
        .unwrap();
        assert_eq!(repair.source_epoch, managed.source_epoch);
        assert_eq!(repair.bound_session_id.as_deref(), Some("managed-session"));
        assert!(!repair.created);
    }
}
