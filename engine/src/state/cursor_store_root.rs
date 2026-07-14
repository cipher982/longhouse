//! Logical Cursor root ordering evidence.
//!
//! Cursor's SQLite root changes as a conversation grows. File size, WAL
//! churn, and root hash changes alone do not prove a new transcript epoch. The
//! only positive append proof is that the current ordered field-1 message IDs
//! extend the previously observed order by prefix. Unknown root encodings are
//! explicitly inconclusive rather than guessed as rewrites.

use anyhow::{Context, Result};
use chrono::Utc;
use rusqlite::{params, Connection, OptionalExtension};

use crate::cursor_store::RootMessageBlobIds;
use crate::state::source_epoch::SourceChangeHint;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CursorRootOrderRelation {
    Initial,
    PrefixExtension,
    Rewrite,
    Inconclusive,
}

impl CursorRootOrderRelation {
    pub fn source_change_hint(self) -> SourceChangeHint {
        match self {
            Self::Rewrite => SourceChangeHint::Rewrite,
            Self::Initial | Self::PrefixExtension | Self::Inconclusive => SourceChangeHint::None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct StoredRootState {
    message_blob_ids: Option<Vec<String>>,
}

/// Record the latest parseable Cursor root ordering for one provider-native
/// conversation and classify its relationship to prior evidence.
pub fn observe_cursor_root(
    conn: &Connection,
    conversation_uuid: &str,
    root_blob_id: &str,
    ordering: &RootMessageBlobIds,
) -> Result<CursorRootOrderRelation> {
    let conversation_uuid = require_nonempty(conversation_uuid, "conversation_uuid")?;
    let root_blob_id = require_nonempty(root_blob_id, "root_blob_id")?;
    let previous = load_root_state(conn, conversation_uuid)?;
    let RootMessageBlobIds::Parsed(current_ids) = ordering else {
        // Do not erase the last proven sequence: a later readable root can
        // still be compared to it. Recording the observed root id keeps local
        // diagnostics current without pretending to know its ordering.
        upsert_root_state(
            conn,
            conversation_uuid,
            root_blob_id,
            previous.and_then(|state| state.message_blob_ids),
        )?;
        return Ok(CursorRootOrderRelation::Inconclusive);
    };

    let relation = match previous
        .as_ref()
        .and_then(|state| state.message_blob_ids.as_ref())
    {
        None if previous.is_none() => CursorRootOrderRelation::Initial,
        None => CursorRootOrderRelation::Inconclusive,
        Some(previous_ids) if current_ids.starts_with(previous_ids) => {
            CursorRootOrderRelation::PrefixExtension
        }
        Some(_) => CursorRootOrderRelation::Rewrite,
    };
    upsert_root_state(
        conn,
        conversation_uuid,
        root_blob_id,
        Some(current_ids.clone()),
    )?;
    Ok(relation)
}

fn load_root_state(conn: &Connection, conversation_uuid: &str) -> Result<Option<StoredRootState>> {
    conn.query_row(
        "SELECT message_blob_ids_json
         FROM cursor_store_root_state
         WHERE conversation_uuid = ?1",
        [conversation_uuid],
        |row| {
            let raw_ids: Option<String> = row.get(0)?;
            let message_blob_ids = raw_ids
                .as_deref()
                .map(serde_json::from_str)
                .transpose()
                .map_err(|error| {
                    rusqlite::Error::FromSqlConversionFailure(
                        0,
                        rusqlite::types::Type::Text,
                        error.into(),
                    )
                })?;
            Ok(StoredRootState { message_blob_ids })
        },
    )
    .optional()
    .context("loading Cursor root ordering state")
}

fn upsert_root_state(
    conn: &Connection,
    conversation_uuid: &str,
    root_blob_id: &str,
    message_blob_ids: Option<Vec<String>>,
) -> Result<()> {
    let message_blob_ids_json = message_blob_ids
        .as_ref()
        .map(serde_json::to_string)
        .transpose()
        .context("encoding Cursor root message IDs")?;
    conn.execute(
        "INSERT INTO cursor_store_root_state (
             conversation_uuid, root_blob_id, message_blob_ids_json, updated_at
         ) VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(conversation_uuid) DO UPDATE SET
             root_blob_id = excluded.root_blob_id,
             message_blob_ids_json = excluded.message_blob_ids_json,
             updated_at = excluded.updated_at",
        params![
            conversation_uuid,
            root_blob_id,
            message_blob_ids_json,
            Utc::now().to_rfc3339(),
        ],
    )?;
    Ok(())
}

fn require_nonempty<'a>(value: &'a str, field: &str) -> Result<&'a str> {
    let value = value.trim();
    if value.is_empty() {
        anyhow::bail!("{field} must be non-empty");
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::db::open_db;

    fn parsed(ids: &[&str]) -> RootMessageBlobIds {
        RootMessageBlobIds::Parsed(ids.iter().map(|id| (*id).to_string()).collect())
    }

    #[test]
    fn root_prefix_extension_stays_in_the_epoch_and_rewrite_rotates() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(temp.path())).unwrap();
        assert_eq!(
            observe_cursor_root(&conn, "conversation", "root-a", &parsed(&["a", "b"])).unwrap(),
            CursorRootOrderRelation::Initial
        );
        assert_eq!(
            observe_cursor_root(&conn, "conversation", "root-b", &parsed(&["a", "b", "c"]))
                .unwrap(),
            CursorRootOrderRelation::PrefixExtension
        );
        assert_eq!(
            observe_cursor_root(&conn, "conversation", "root-c", &parsed(&["a", "x"])).unwrap(),
            CursorRootOrderRelation::Rewrite
        );
        assert_eq!(
            CursorRootOrderRelation::Rewrite.source_change_hint(),
            SourceChangeHint::Rewrite
        );
    }

    #[test]
    fn unknown_root_does_not_destroy_prior_prefix_proof() {
        let temp = tempfile::NamedTempFile::new().unwrap();
        let conn = open_db(Some(temp.path())).unwrap();
        observe_cursor_root(&conn, "conversation", "root-a", &parsed(&["a", "b"])).unwrap();
        assert_eq!(
            observe_cursor_root(
                &conn,
                "conversation",
                "unknown-root",
                &RootMessageBlobIds::Unavailable {
                    reason: "new Cursor wire type".to_string(),
                },
            )
            .unwrap(),
            CursorRootOrderRelation::Inconclusive
        );
        assert_eq!(
            observe_cursor_root(&conn, "conversation", "root-c", &parsed(&["a", "b", "c"]))
                .unwrap(),
            CursorRootOrderRelation::PrefixExtension
        );
    }
}
