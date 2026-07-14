//! Parser-independent raw + parser-versioned render shipping for storage-v2.

use std::collections::HashMap;
use std::path::Path;
use std::time::Duration;

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use chrono::{DateTime, Utc};
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::cursor_store;
use crate::opencode_db;
use crate::pipeline::parser::{
    self, ParseResult, ParsedEvent, ParsedMediaObject, Role, SessionMetadata,
};
use crate::raw_records::{
    read_next_raw_batch_with_limits, RawRecordBatch, RawSourceFraming, MAX_RAW_BATCH_BYTES,
};
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::{
    StorageV2Capabilities, StorageV2Envelope, StorageV2MediaRef, StorageV2Record,
};
use crate::shipping::storage_v2::{StorageV2Render, StorageV2RenderRecord, StorageV2SessionFacts};
use crate::state::cursor_store_records;
use crate::state::cursor_store_root;
use crate::state::file_identity::{cursor_fingerprint, identity_from_metadata};
use crate::state::file_state::FileState;
use crate::state::source_epoch::{self, SourceChangeHint, SourceEpochResolution, SourceLane};
use crate::storage_v2_contract::{self, EnvelopeIdentity, RangeKind};

pub(crate) const PARSER_REVISION: &str = "engine-parser-v2";
pub(crate) const ORDERING_REVISION: &str = "semantic-order-v2";
const OPENCODE_SESSION_PAGE_SIZE: usize = 64;

pub(crate) struct PreparedStorageV2Envelope {
    pub envelope: StorageV2Envelope,
    pub source_epoch: Uuid,
    pub range_start: u64,
    pub range_end: u64,
    pub event_count: usize,
    pub has_reply_evidence: bool,
    pub raw_bytes: u64,
    pub has_more: bool,
    pub media_objects: Vec<ParsedMediaObject>,
}

pub(crate) struct StorageV2ShipOutcome {
    pub bytes_shipped: u64,
    pub events_shipped: usize,
    pub has_more: bool,
}

pub(crate) fn prepare_next_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    session_id_override: Option<&str>,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let canonical_path = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let path_text = canonical_path.to_string_lossy();
    let durable_session_id = match session_id_override {
        Some(value) => Some(value.to_string()),
        None => crate::state::session_binding::SessionBinding::new(conn).get(&path_text)?,
    };
    let session_id_override = durable_session_id.as_deref();
    let opaque_source_id = opaque_source_id(&path_text);
    let legacy_offset = validated_legacy_offset(conn, &path_text, &canonical_path)?;
    let source_revision = if provider.eq_ignore_ascii_case("antigravity") {
        Some(hash_file(path)?)
    } else {
        None
    };
    let resolution = source_epoch::observe_file(
        conn,
        provider,
        &opaque_source_id,
        path,
        SourceLane::Durable,
        legacy_offset,
        source_revision.as_deref(),
        session_id_override,
        SourceChangeHint::None,
    )?;
    let position = source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    let source_len = std::fs::metadata(path)?.len();
    if position >= source_len {
        return Ok(None);
    }
    let framing = if provider.eq_ignore_ascii_case("antigravity") {
        RawSourceFraming::WholeDocument
    } else {
        RawSourceFraming::LfDelimited
    };
    let maximum_record_bytes = usize::try_from(capabilities.max_raw_record_bytes)
        .context("storage-v2 raw record limit exceeds usize")?;
    let Some(mut raw_batch) = read_next_raw_batch_with_limits(
        path,
        framing,
        position,
        MAX_RAW_BATCH_BYTES,
        maximum_record_bytes,
    )?
    else {
        return Ok(None);
    };
    let parse_result = parser::parse_session_file(path, position)?;
    if framing == RawSourceFraming::LfDelimited
        && raw_batch.range_end > parse_result.last_good_offset
    {
        raw_batch
            .records
            .retain(|record| record.range_end <= parse_result.last_good_offset);
        let Some(last) = raw_batch.records.last() else {
            return Ok(None);
        };
        raw_batch.range_end = last.range_end;
    }
    let session_id = resolve_session_id(
        provider,
        &parse_result,
        resolution
            .bound_session_id
            .as_deref()
            .or(session_id_override),
    );
    let session_uuid =
        Uuid::parse_str(&session_id).context("storage-v2 session id is not a UUID")?;
    crate::state::session_title::observe_parse_result(conn, &session_id, &parse_result)?;
    let raw_bytes: Vec<Vec<u8>> = raw_batch
        .records
        .iter()
        .map(|record| record.bytes.clone())
        .collect();
    let identity = EnvelopeIdentity {
        tenant_id: capabilities.tenant_id.clone(),
        machine_id: capabilities.machine_id.clone(),
        provider: provider.to_ascii_lowercase(),
        opaque_source_id: opaque_source_id.clone(),
        source_epoch: resolution.source_epoch,
        range_kind: RangeKind::ByteOffset,
        range_start: raw_batch.range_start,
        range_end: raw_batch.range_end,
        record_hashes: storage_v2_contract::hash_records(&raw_bytes),
    };
    let expected_envelope_id = hex_hash(storage_v2_contract::envelope_id(&identity)?);
    let render_records = render_records_for_batch(&parse_result, &raw_batch)?;
    let render_generation = render_generation_id(session_uuid);
    let session = session_facts(&parse_result.metadata, &render_records, &resolution)?;
    let media_objects = parse_result
        .media_objects
        .iter()
        .filter(|media| {
            media.source_offset >= raw_batch.range_start
                && media.source_offset < raw_batch.range_end
        })
        .cloned()
        .collect::<Vec<_>>();
    Ok(Some(PreparedStorageV2Envelope {
        envelope: StorageV2Envelope {
            protocol_version: 2,
            tenant_id: capabilities.tenant_id.clone(),
            machine_id: capabilities.machine_id.clone(),
            session_id,
            provider: provider.to_ascii_lowercase(),
            opaque_source_id,
            source_epoch: resolution.source_epoch.to_string(),
            predecessor_source_epoch: resolution.predecessor_epoch.map(|value| value.to_string()),
            epoch_opened_at: resolution.opened_at,
            range_kind: "byte_offset".to_string(),
            range_start: raw_batch.range_start,
            range_end: raw_batch.range_end,
            render: Some(StorageV2Render {
                generation_id: render_generation.to_string(),
                parser_revision: PARSER_REVISION.to_string(),
                ordering_revision: ORDERING_REVISION.to_string(),
                records: render_records,
            }),
            media: storage_v2_media_refs(&media_objects),
            session,
            records: raw_batch
                .records
                .into_iter()
                .map(|record| StorageV2Record {
                    source_position: record.range_start,
                    data_b64: BASE64_STANDARD.encode(record.bytes),
                })
                .collect(),
            expected_envelope_id,
        },
        source_epoch: resolution.source_epoch,
        range_start: raw_batch.range_start,
        range_end: raw_batch.range_end,
        event_count: parse_result
            .events
            .iter()
            .filter(|event| {
                event.source_offset >= raw_batch.range_start
                    && event.source_offset < raw_batch.range_end
            })
            .count(),
        has_reply_evidence: parse_result.events.iter().any(|event| {
            event.source_offset >= raw_batch.range_start
                && matches!(event.role, Role::Assistant | Role::Tool)
        }),
        raw_bytes: raw_batch.range_end - raw_batch.range_start,
        has_more: raw_batch.range_end < source_len,
        media_objects,
    }))
}

pub(crate) async fn ship_next_envelope(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    session_id_override: Option<&str>,
    lane: &str,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let Some(prepared) =
        prepare_next_envelope(conn, capabilities, path, provider, session_id_override)?
    else {
        return Ok(None);
    };
    ship_prepared_envelope(conn, client, capabilities, prepared, lane, request_timeout)
        .await
        .map(Some)
}

pub(crate) async fn ship_prepared_envelope(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    prepared: PreparedStorageV2Envelope,
    lane: &str,
    request_timeout: Duration,
) -> Result<StorageV2ShipOutcome> {
    crate::media_upload::ensure_storage_v2_media_uploaded(
        client,
        capabilities,
        &prepared.media_objects,
        lane,
        Some(request_timeout),
    )
    .await?;
    client
        .ship_storage_v2_envelope(
            &capabilities.ingest_path,
            lane,
            &prepared.envelope,
            Some(request_timeout),
        )
        .await?;
    source_epoch::acknowledge_position(
        conn,
        prepared.source_epoch,
        SourceLane::Durable,
        prepared.range_start,
        prepared.range_end,
    )?;
    Ok(StorageV2ShipOutcome {
        bytes_shipped: prepared.raw_bytes,
        events_shipped: prepared.event_count,
        has_more: prepared.has_more,
    })
}

pub(crate) fn prepare_next_opencode_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let canonical_path = std::fs::canonicalize(db_path).unwrap_or_else(|_| db_path.to_path_buf());
    let path_text = canonical_path.to_string_lossy();
    let mut page_offset = 0usize;
    loop {
        let candidates = opencode_db::list_opencode_sessions_page(
            db_path,
            OPENCODE_SESSION_PAGE_SIZE,
            page_offset,
        )?;
        if candidates.is_empty() {
            return Ok(None);
        }
        for (candidate_index, candidate) in candidates.iter().enumerate() {
            let snapshot =
                opencode_db::opencode_raw_snapshot(db_path, &candidate.provider_session_id)?;
            let logical_len = u64::try_from(snapshot.records.len())
                .context("OpenCode snapshot has too many records")?;
            let opaque_source_id = opaque_source_id(&format!(
                "{path_text}\0opencode-session\0{}",
                candidate.provider_session_id
            ));
            let resolution = source_epoch::observe_source(
                conn,
                "opencode",
                &opaque_source_id,
                "opencode-sqlite-session-v1",
                logical_len,
                SourceLane::Durable,
                0,
                Some(&snapshot.source_revision),
                None,
                SourceChangeHint::None,
            )?;
            let range_start =
                source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
            if range_start >= logical_len {
                continue;
            }
            let (range_end, raw_bytes) = bounded_record_ordinal_end(
                &snapshot.records,
                range_start,
                capabilities.max_records,
                capabilities.max_raw_record_bytes,
            )?;
            let parse_result =
                opencode_db::parse_opencode_session(db_path, &candidate.provider_session_id)?;
            let session_id = opencode_db::managed_longhouse_session_id_for_opencode(
                &candidate.provider_session_id,
            )
            .unwrap_or_else(|| parse_result.metadata.session_id.clone());
            let session_uuid = Uuid::parse_str(&session_id)
                .context("storage-v2 OpenCode session id is not a UUID")?;
            crate::state::session_title::observe_parse_result(conn, &session_id, &parse_result)?;
            let start =
                usize::try_from(range_start).context("OpenCode range start exceeds usize")?;
            let end = usize::try_from(range_end).context("OpenCode range end exceeds usize")?;
            let selected = &snapshot.records[start..end];
            let identity = EnvelopeIdentity {
                tenant_id: capabilities.tenant_id.clone(),
                machine_id: capabilities.machine_id.clone(),
                provider: "opencode".to_string(),
                opaque_source_id: opaque_source_id.clone(),
                source_epoch: resolution.source_epoch,
                range_kind: RangeKind::RecordOrdinal,
                range_start,
                range_end,
                record_hashes: storage_v2_contract::hash_records(selected),
            };
            let expected_envelope_id = hex_hash(storage_v2_contract::envelope_id(&identity)?);
            let render_records = opencode_render_records_for_range(
                &parse_result,
                snapshot.part_record_start,
                range_start,
                range_end,
            )?;
            let event_count = render_records.len();
            let render_generation = render_generation_id(session_uuid);
            let session = session_facts(&parse_result.metadata, &render_records, &resolution)?;
            let media_objects = opencode_media_objects_for_range(
                &parse_result,
                snapshot.part_record_start,
                range_start,
                range_end,
            )?;
            return Ok(Some(PreparedStorageV2Envelope {
                envelope: StorageV2Envelope {
                    protocol_version: 2,
                    tenant_id: capabilities.tenant_id.clone(),
                    machine_id: capabilities.machine_id.clone(),
                    session_id,
                    provider: "opencode".to_string(),
                    opaque_source_id,
                    source_epoch: resolution.source_epoch.to_string(),
                    predecessor_source_epoch: resolution
                        .predecessor_epoch
                        .map(|value| value.to_string()),
                    epoch_opened_at: resolution.opened_at,
                    range_kind: "record_ordinal".to_string(),
                    range_start,
                    range_end,
                    render: Some(StorageV2Render {
                        generation_id: render_generation.to_string(),
                        parser_revision: PARSER_REVISION.to_string(),
                        ordering_revision: ORDERING_REVISION.to_string(),
                        records: render_records,
                    }),
                    media: storage_v2_media_refs(&media_objects),
                    session,
                    records: selected
                        .iter()
                        .enumerate()
                        .map(|(offset, bytes)| StorageV2Record {
                            source_position: range_start + offset as u64,
                            data_b64: BASE64_STANDARD.encode(bytes),
                        })
                        .collect(),
                    expected_envelope_id,
                },
                source_epoch: resolution.source_epoch,
                range_start,
                range_end,
                event_count,
                has_reply_evidence: parse_result
                    .events
                    .iter()
                    .any(|event| matches!(event.role, Role::Assistant | Role::Tool)),
                raw_bytes,
                has_more: range_end < logical_len
                    || candidate_index + 1 < candidates.len()
                    || candidates.len() == OPENCODE_SESSION_PAGE_SIZE,
                media_objects,
            }));
        }
        page_offset = page_offset.saturating_add(candidates.len());
    }
}

pub(crate) fn prepare_next_cursor_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let snapshot = cursor_store::cursor_store_raw_snapshot(db_path)?;
    let claimed_session_id = crate::cursor_launch_binding::managed_session_id_for_conversation(
        &snapshot.conversation_uuid,
    )?;
    let opaque_source_id = cursor_store::cursor_opaque_source_id(&snapshot.conversation_uuid);
    let root_relation = match snapshot.root_blob_id.as_deref() {
        Some(root_blob_id) => cursor_store_root::observe_cursor_root(
            conn,
            &snapshot.conversation_uuid,
            root_blob_id,
            &snapshot.root_message_blob_ids,
        )?,
        None => cursor_store_root::CursorRootOrderRelation::Inconclusive,
    };
    let incarnation = snapshot.store_incarnation.clone();
    let existing_len =
        cursor_store_records::active_cursor_record_count(conn, "cursor", &opaque_source_id)?;
    let active_incarnation =
        source_epoch::active_source_incarnation(conn, "cursor", &opaque_source_id)?;
    let source_len_before_capture = if root_relation
        == cursor_store_root::CursorRootOrderRelation::Rewrite
        || active_incarnation.as_deref() != Some(incarnation.as_str())
    {
        0
    } else {
        existing_len
    };
    let resolution = source_epoch::observe_source(
        conn,
        "cursor",
        &opaque_source_id,
        &incarnation,
        source_len_before_capture,
        SourceLane::Durable,
        0,
        None,
        claimed_session_id.as_deref(),
        root_relation.source_change_hint(),
    )?;
    cursor_store_records::append_unseen_cursor_records(
        conn,
        resolution.source_epoch,
        &snapshot.records,
    )?;
    let logical_len = cursor_store_records::cursor_record_count(conn, resolution.source_epoch)?;
    // Refresh max_observed_len after adding local records. `None` revision is
    // intentional: an append changes Cursor's root blob every turn but must
    // not rotate the epoch.
    let resolution = source_epoch::observe_source(
        conn,
        "cursor",
        &opaque_source_id,
        &incarnation,
        logical_len,
        SourceLane::Durable,
        source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?,
        None,
        None,
        SourceChangeHint::None,
    )?;
    let range_start =
        source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    if range_start >= logical_len {
        return Ok(None);
    }
    let selected = cursor_store_records::cursor_records_from(
        conn,
        resolution.source_epoch,
        range_start,
        capabilities.max_records,
        capabilities.max_raw_record_bytes,
    )?;
    let Some(last) = selected.last() else {
        return Ok(None);
    };
    let range_end = last
        .source_position
        .checked_add(1)
        .context("Cursor source position overflow")?;
    let raw_bytes = selected.iter().try_fold(0u64, |total, record| {
        total
            .checked_add(
                u64::try_from(record.bytes.len())
                    .context("Cursor raw record length exceeds u64")?,
            )
            .context("Cursor raw bytes overflow")
    })?;
    let identity = EnvelopeIdentity {
        tenant_id: capabilities.tenant_id.clone(),
        machine_id: capabilities.machine_id.clone(),
        provider: "cursor".to_string(),
        opaque_source_id: opaque_source_id.clone(),
        source_epoch: resolution.source_epoch,
        range_kind: RangeKind::RecordOrdinal,
        range_start,
        range_end,
        record_hashes: storage_v2_contract::hash_records(
            &selected
                .iter()
                .map(|record| record.bytes.clone())
                .collect::<Vec<_>>(),
        ),
    };
    // A normal Cursor store is durable but not watchable as a managed Helm
    // session.  A verified probe binding is persisted by source_epoch so that
    // expiry cannot split an already-bound conversation mid-archive.
    let managed_session_id = resolution.bound_session_id.clone();
    let session_id = managed_session_id.clone().unwrap_or_else(|| {
        cursor_store::longhouse_session_id_for_cursor(&snapshot.conversation_uuid)
    });
    let started_at = snapshot
        .created_at_ms
        .and_then(DateTime::from_timestamp_millis)
        .unwrap_or_else(|| {
            DateTime::parse_from_rfc3339(&resolution.opened_at)
                .expect("source epoch opened_at is generated internally")
                .with_timezone(&Utc)
        });
    let observed_at = DateTime::parse_from_rfc3339(&resolution.opened_at)
        .expect("source epoch opened_at is generated internally")
        .with_timezone(&Utc);
    Ok(Some(PreparedStorageV2Envelope {
        envelope: StorageV2Envelope {
            protocol_version: 2,
            tenant_id: capabilities.tenant_id.clone(),
            machine_id: capabilities.machine_id.clone(),
            session_id,
            provider: "cursor".to_string(),
            opaque_source_id,
            source_epoch: resolution.source_epoch.to_string(),
            predecessor_source_epoch: resolution.predecessor_epoch.map(|value| value.to_string()),
            epoch_opened_at: resolution.opened_at,
            range_kind: "record_ordinal".to_string(),
            range_start,
            range_end,
            render: None,
            media: Vec::new(),
            session: StorageV2SessionFacts {
                environment: "local".to_string(),
                project: None,
                cwd: None,
                git_repo: None,
                git_branch: None,
                started_at: started_at.to_rfc3339(),
                last_activity_at: observed_at.max(started_at).to_rfc3339(),
                ended_at: None,
                origin_kind: Some("cursor_store".to_string()),
                hidden_from_default_timeline: managed_session_id.is_none(),
                launch_actor: None,
                launch_surface: None,
            },
            records: selected
                .into_iter()
                .map(|record| StorageV2Record {
                    source_position: record.source_position,
                    data_b64: BASE64_STANDARD.encode(record.bytes),
                })
                .collect(),
            expected_envelope_id: hex_hash(storage_v2_contract::envelope_id(&identity)?),
        },
        source_epoch: resolution.source_epoch,
        range_start,
        range_end,
        event_count: 0,
        has_reply_evidence: false,
        raw_bytes,
        has_more: range_end < logical_len,
        media_objects: Vec::new(),
    }))
}

pub(crate) fn prepare_next_cursor_acp_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let session_id = path
        .parent()
        .and_then(Path::file_name)
        .and_then(|v| v.to_str())
        .context("Cursor ACP source path has no managed session directory")?;
    Uuid::parse_str(session_id).context("Cursor ACP source session id is not a UUID")?;
    let run_id = path
        .file_stem()
        .and_then(|v| v.to_str())
        .context("Cursor ACP source path has no run id")?;
    let opaque_source_id = format!("cursor-acp-v1:{session_id}:{run_id}");
    let resolution = source_epoch::observe_file(
        conn,
        "cursor",
        &opaque_source_id,
        path,
        SourceLane::Durable,
        0,
        None,
        Some(session_id),
        SourceChangeHint::None,
    )?;
    let position = source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    let source_len = std::fs::metadata(path)?.len();
    if position >= source_len {
        return Ok(None);
    }
    let maximum_record_bytes = usize::try_from(capabilities.max_raw_record_bytes)
        .context("storage-v2 raw record limit exceeds usize")?;
    let Some(batch) = read_next_raw_batch_with_limits(
        path,
        RawSourceFraming::LfDelimited,
        position,
        MAX_RAW_BATCH_BYTES,
        maximum_record_bytes,
    )?
    else {
        return Ok(None);
    };
    let raw_bytes: Vec<Vec<u8>> = batch
        .records
        .iter()
        .map(|record| record.bytes.clone())
        .collect();
    let identity = EnvelopeIdentity {
        tenant_id: capabilities.tenant_id.clone(),
        machine_id: capabilities.machine_id.clone(),
        provider: "cursor".to_string(),
        opaque_source_id: opaque_source_id.clone(),
        source_epoch: resolution.source_epoch,
        range_kind: RangeKind::ByteOffset,
        range_start: batch.range_start,
        range_end: batch.range_end,
        record_hashes: storage_v2_contract::hash_records(&raw_bytes),
    };
    let observed_at = DateTime::parse_from_rfc3339(&resolution.opened_at)
        .expect("source epoch opened_at is generated internally")
        .with_timezone(&Utc);
    Ok(Some(PreparedStorageV2Envelope {
        envelope: StorageV2Envelope {
            protocol_version: 2,
            tenant_id: capabilities.tenant_id.clone(),
            machine_id: capabilities.machine_id.clone(),
            session_id: session_id.to_string(),
            provider: "cursor".to_string(),
            opaque_source_id,
            source_epoch: resolution.source_epoch.to_string(),
            predecessor_source_epoch: resolution.predecessor_epoch.map(|v| v.to_string()),
            epoch_opened_at: resolution.opened_at,
            range_kind: "byte_offset".to_string(),
            range_start: batch.range_start,
            range_end: batch.range_end,
            render: None,
            media: Vec::new(),
            session: StorageV2SessionFacts {
                environment: "local".to_string(),
                project: None,
                cwd: None,
                git_repo: None,
                git_branch: None,
                started_at: observed_at.to_rfc3339(),
                last_activity_at: observed_at.to_rfc3339(),
                ended_at: None,
                origin_kind: Some("cursor_acp".to_string()),
                hidden_from_default_timeline: false,
                launch_actor: None,
                launch_surface: None,
            },
            records: batch
                .records
                .into_iter()
                .map(|record| StorageV2Record {
                    source_position: record.range_start,
                    data_b64: BASE64_STANDARD.encode(record.bytes),
                })
                .collect(),
            expected_envelope_id: hex_hash(storage_v2_contract::envelope_id(&identity)?),
        },
        source_epoch: resolution.source_epoch,
        range_start: batch.range_start,
        range_end: batch.range_end,
        event_count: 0,
        has_reply_evidence: false,
        raw_bytes: batch.range_end - batch.range_start,
        has_more: batch.range_end < source_len,
        media_objects: Vec::new(),
    }))
}

fn validated_legacy_offset(conn: &Connection, path_text: &str, path: &Path) -> Result<u64> {
    let file_state = FileState::new(conn);
    let offset = file_state.get_offset(path_text)?;
    if offset == 0 {
        return Ok(0);
    }
    let metadata = path
        .metadata()
        .with_context(|| format!("reading source metadata: {}", path.display()))?;
    let stored_identity = file_state.get_file_identity(path_text)?;
    let current_identity = identity_from_metadata(&metadata);
    let stored_fingerprint = file_state.get_acked_cursor_fingerprint(path_text)?;
    let current_fingerprint = cursor_fingerprint(path, offset);
    if stored_identity == current_identity
        && stored_identity.is_some()
        && stored_fingerprint == current_fingerprint
        && stored_fingerprint.is_some()
    {
        return Ok(offset);
    }
    tracing::warn!(
        path = %path.display(),
        offset,
        stored_identity = ?stored_identity,
        current_identity = ?current_identity,
        "Legacy cursor lacks matching file identity and boundary proof; replaying storage-v2 source from zero"
    );
    Ok(0)
}

pub(crate) async fn ship_next_opencode_envelope(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    lane: &str,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let Some(prepared) = prepare_next_opencode_envelope(conn, capabilities, db_path)? else {
        return Ok(None);
    };
    crate::media_upload::ensure_storage_v2_media_uploaded(
        client,
        capabilities,
        &prepared.media_objects,
        lane,
        Some(request_timeout),
    )
    .await?;
    client
        .ship_storage_v2_envelope(
            &capabilities.ingest_path,
            lane,
            &prepared.envelope,
            Some(request_timeout),
        )
        .await?;
    source_epoch::acknowledge_position(
        conn,
        prepared.source_epoch,
        SourceLane::Durable,
        prepared.range_start,
        prepared.range_end,
    )?;
    Ok(Some(StorageV2ShipOutcome {
        bytes_shipped: prepared.raw_bytes,
        events_shipped: prepared.event_count,
        has_more: prepared.has_more,
    }))
}

pub(crate) async fn ship_next_cursor_envelope(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    lane: &str,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let Some(prepared) = prepare_next_cursor_envelope(conn, capabilities, db_path)? else {
        return Ok(None);
    };
    ship_prepared_envelope(conn, client, capabilities, prepared, lane, request_timeout)
        .await
        .map(Some)
}

pub(crate) async fn ship_next_cursor_acp_envelope(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    lane: &str,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let Some(prepared) = prepare_next_cursor_acp_envelope(conn, capabilities, path)? else {
        return Ok(None);
    };
    ship_prepared_envelope(conn, client, capabilities, prepared, lane, request_timeout)
        .await
        .map(Some)
}

fn storage_v2_media_refs(media_objects: &[ParsedMediaObject]) -> Vec<StorageV2MediaRef> {
    media_objects
        .iter()
        .enumerate()
        .map(|(index, media)| StorageV2MediaRef {
            sha256: media.sha256.clone(),
            source_position: media.source_offset,
            ref_key: format!(
                "inline_data_url:{}:{}:{index}",
                media.source_offset, media.original_line_sha256
            ),
            availability: "available".to_string(),
        })
        .collect()
}

fn opencode_media_objects_for_range(
    parse_result: &ParseResult,
    part_record_start: u64,
    range_start: u64,
    range_end: u64,
) -> Result<Vec<ParsedMediaObject>> {
    let source_ordinals: HashMap<u64, u64> = parse_result
        .source_lines
        .iter()
        .enumerate()
        .map(|(index, line)| (line.source_offset, part_record_start + index as u64))
        .collect();
    let mut result = Vec::new();
    for media in &parse_result.media_objects {
        let ordinal = source_ordinals
            .get(&media.source_offset)
            .copied()
            .context("OpenCode media is not covered by a raw part record")?;
        if ordinal >= range_start && ordinal < range_end {
            let mut mapped = media.clone();
            mapped.source_offset = ordinal;
            result.push(mapped);
        }
    }
    Ok(result)
}

fn bounded_record_ordinal_end(
    records: &[Vec<u8>],
    range_start: u64,
    max_records: u64,
    max_bytes: u64,
) -> Result<(u64, u64)> {
    let start = usize::try_from(range_start).context("record-ordinal start exceeds usize")?;
    let record_limit = usize::try_from(max_records).unwrap_or(usize::MAX);
    let mut end = start;
    let mut bytes = 0u64;
    for record in records.iter().skip(start).take(record_limit) {
        let record_bytes = u64::try_from(record.len()).context("raw record length exceeds u64")?;
        if record_bytes > max_bytes {
            anyhow::bail!("one OpenCode raw record exceeds the negotiated storage-v2 object bound");
        }
        if bytes + record_bytes > max_bytes {
            break;
        }
        bytes += record_bytes;
        end += 1;
    }
    if end == start {
        anyhow::bail!("storage-v2 record-ordinal batch made no progress");
    }
    Ok((
        u64::try_from(end).context("record-ordinal end exceeds u64")?,
        bytes,
    ))
}

fn render_records_for_batch(
    parse_result: &ParseResult,
    batch: &RawRecordBatch,
) -> Result<Vec<StorageV2RenderRecord>> {
    let mut subordinals: HashMap<u64, u32> = HashMap::new();
    let mut records = Vec::new();
    for event in parse_result.events.iter().filter(|event| {
        event.source_offset >= batch.range_start && event.source_offset < batch.range_end
    }) {
        let subordinal = subordinals.entry(event.source_offset).or_default();
        let raw_record_ordinal = batch
            .records
            .iter()
            .position(|record| {
                event.source_offset >= record.range_start && event.source_offset < record.range_end
            })
            .context("parsed event is not covered by its raw record")?;
        records.push(render_record(
            event,
            event.source_offset,
            *subordinal,
            raw_record_ordinal,
        )?);
        *subordinal += 1;
    }
    records.sort_by(|left, right| {
        (
            left.order_time_us,
            left.source_position,
            left.event_subordinal,
            &left.event_id,
        )
            .cmp(&(
                right.order_time_us,
                right.source_position,
                right.event_subordinal,
                &right.event_id,
            ))
    });
    Ok(records)
}

fn opencode_render_records_for_range(
    parse_result: &ParseResult,
    part_record_start: u64,
    range_start: u64,
    range_end: u64,
) -> Result<Vec<StorageV2RenderRecord>> {
    let source_ordinals: Vec<(u64, u64)> = parse_result
        .source_lines
        .iter()
        .enumerate()
        .map(|(index, line)| (line.source_offset, part_record_start + index as u64))
        .collect();
    let mut subordinals: HashMap<u64, u32> = HashMap::new();
    let mut records = Vec::new();
    for event in &parse_result.events {
        let source_position = source_ordinals
            .iter()
            .rev()
            .find(|(source_offset, _)| *source_offset <= event.source_offset)
            .map(|(_, ordinal)| *ordinal)
            .context("OpenCode parsed event has no source record")?;
        if source_position < range_start || source_position >= range_end {
            continue;
        }
        let subordinal = subordinals.entry(source_position).or_default();
        let raw_record_ordinal = usize::try_from(source_position - range_start)
            .context("OpenCode raw record ordinal exceeds usize")?;
        records.push(render_record(
            event,
            source_position,
            *subordinal,
            raw_record_ordinal,
        )?);
        *subordinal += 1;
    }
    records.sort_by(|left, right| {
        (
            left.order_time_us,
            left.source_position,
            left.event_subordinal,
            &left.event_id,
        )
            .cmp(&(
                right.order_time_us,
                right.source_position,
                right.event_subordinal,
                &right.event_id,
            ))
    });
    Ok(records)
}

fn render_record(
    event: &ParsedEvent,
    source_position: u64,
    event_subordinal: u32,
    raw_record_ordinal: usize,
) -> Result<StorageV2RenderRecord> {
    let tool_input_json = event
        .tool_input_json
        .as_ref()
        .map(|value| serde_json::from_str(value.get()))
        .transpose()
        .context("parsed tool input is not JSON")?;
    Ok(StorageV2RenderRecord {
        event_id: event.uuid.clone(),
        order_time_us: event.timestamp.timestamp_micros(),
        source_position,
        event_subordinal,
        role: match event.role {
            Role::User => "user",
            Role::Assistant => "assistant",
            Role::Tool => "tool",
            Role::System => "system",
        }
        .to_string(),
        content_text: event.content_text.clone(),
        tool_name: event.tool_name.clone(),
        tool_input_json,
        tool_output_text: event.tool_output_text.clone(),
        tool_call_id: event.tool_call_id.clone(),
        thread_id: None,
        branch_kind: None,
        raw_record_ordinal,
    })
}

fn session_facts(
    metadata: &SessionMetadata,
    records: &[StorageV2RenderRecord],
    resolution: &SourceEpochResolution,
) -> Result<StorageV2SessionFacts> {
    let fallback = DateTime::parse_from_rfc3339(&resolution.opened_at)
        .context("source epoch opened_at is invalid")?
        .with_timezone(&Utc);
    let started_at = metadata
        .started_at
        .or_else(|| records.first().and_then(record_time))
        .unwrap_or(fallback);
    let last_activity_at = records
        .iter()
        .filter_map(record_time)
        .max()
        .or(metadata.ended_at)
        .unwrap_or(started_at);
    Ok(StorageV2SessionFacts {
        environment: metadata
            .environment
            .clone()
            .unwrap_or_else(|| "local".to_string()),
        project: metadata.project.clone(),
        cwd: metadata.cwd.clone(),
        git_repo: metadata.git_repo.clone(),
        git_branch: metadata.git_branch.clone(),
        started_at: started_at.to_rfc3339(),
        last_activity_at: last_activity_at.max(started_at).to_rfc3339(),
        ended_at: metadata.ended_at.map(|value| value.to_rfc3339()),
        origin_kind: metadata.origin_kind.clone(),
        hidden_from_default_timeline: metadata.is_sidechain,
        launch_actor: metadata.launch_actor.clone(),
        launch_surface: metadata.launch_surface.clone(),
    })
}

fn record_time(record: &StorageV2RenderRecord) -> Option<DateTime<Utc>> {
    DateTime::from_timestamp_micros(record.order_time_us)
}

fn resolve_session_id(
    provider: &str,
    parse_result: &ParseResult,
    override_id: Option<&str>,
) -> String {
    let parsed = parse_result.metadata.session_id.clone();
    let Some(override_id) = override_id else {
        return parsed;
    };
    if provider.eq_ignore_ascii_case("codex")
        && (parse_result.metadata.forked_from_session_id.is_some()
            || parse_result.metadata.is_sidechain)
        && override_id != parsed
    {
        parsed
    } else {
        override_id.to_string()
    }
}

fn render_generation_id(session_id: Uuid) -> Uuid {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("longhouse-render-v2\0{session_id}\0{PARSER_REVISION}\0{ORDERING_REVISION}")
            .as_bytes(),
    )
}

fn opaque_source_id(path: &str) -> String {
    format!(
        "path-sha256:{}",
        hex_hash(Sha256::digest(path.as_bytes()).into())
    )
}

fn hash_file(path: &Path) -> Result<String> {
    let bytes = std::fs::read(path)
        .with_context(|| format!("reading source revision: {}", path.display()))?;
    Ok(hex_hash(Sha256::digest(bytes).into()))
}

fn hex_hash(hash: [u8; 32]) -> String {
    hash.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use std::fs;
    use std::sync::{Arc, Mutex};

    use rusqlite::params;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    use super::*;
    use crate::config::ShipperConfig;
    use crate::pipeline::compressor::CompressionAlgo;
    use crate::shipping::client::ShipperClient;
    use crate::state::db::open_db;
    use crate::state::file_state::FileState;

    const CURSOR_CONVERSATION_ID: &str = "60bf2c11-01da-456e-8216-c5dbd2fa52b4";
    const CURSOR_ROOT_A: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    const CURSOR_ROOT_B: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
    const CURSOR_ROOT_C: &str = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
    const CURSOR_MESSAGE_A: &str =
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
    const CURSOR_MESSAGE_B: &str =
        "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
    const CURSOR_MESSAGE_C: &str =
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff";

    fn capabilities() -> StorageV2Capabilities {
        StorageV2Capabilities {
            protocol_version: 2,
            cutover: true,
            tenant_id: "tenant-a".to_string(),
            machine_id: "cinder".to_string(),
            ingest_path: "/api/agents/storage/v2/envelopes".to_string(),
            max_wire_body_bytes: 12 * 1024 * 1024,
            max_raw_record_bytes: 4 * 1024 * 1024,
            max_records: 10_000,
            media_claim_path: "/api/agents/storage/v2/media/claims".to_string(),
            media_upload_path_template: "/api/agents/storage/v2/media/{sha256}".to_string(),
            max_media_bytes: 32 * 1024 * 1024,
            max_media_claims: 512,
            range_kinds: vec!["byte_offset".to_string(), "record_ordinal".to_string()],
            lanes: vec!["live".to_string(), "repair".to_string()],
            lane_header: "X-Longhouse-Storage-Lane".to_string(),
        }
    }

    fn cursor_root(ids: &[u8]) -> Vec<u8> {
        let mut root = Vec::new();
        for id in ids.chunks_exact(32) {
            root.extend_from_slice(&[0x0a, 0x20]);
            root.extend_from_slice(id);
        }
        root
    }

    fn cursor_metadata(root_blob_id: &str) -> String {
        let json = format!(
            r#"{{"agentId":"{CURSOR_CONVERSATION_ID}","latestRootBlobId":"{root_blob_id}","createdAt":1773403200000}}"#
        );
        json.as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect()
    }

    fn make_cursor_store(path: &Path) -> Connection {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB);
             CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('0', ?1)",
            [cursor_metadata(CURSOR_ROOT_A)],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('unknown', X'00FF')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
            params![CURSOR_ROOT_A, cursor_root(&[0xbb; 32])],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, X'0102')",
            [CURSOR_MESSAGE_A],
        )
        .unwrap();
        conn
    }

    fn set_cursor_root(conn: &Connection, root_id: &str, message_ids: &[u8]) {
        conn.execute(
            "UPDATE meta SET value = ?1 WHERE key = '0'",
            [cursor_metadata(root_id)],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
            params![root_id, cursor_root(message_ids)],
        )
        .unwrap();
    }

    #[test]
    fn cursor_acp_source_preserves_exact_notifications_and_needs_a_receipt_to_advance() {
        let dir = tempfile::tempdir().unwrap();
        let session_id = "019c638d-0000-0000-0000-000000000099";
        let path = dir.path().join(session_id).join("run-1.jsonl");
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let raw = b" {\"jsonrpc\":\"2.0\",\"method\":\"session/update\"}\n";
        fs::write(&path, raw).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_acp_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(first.envelope.session_id, session_id);
        assert_eq!(first.envelope.provider, "cursor");
        assert!(first.envelope.render.is_none());
        assert_eq!(
            BASE64_STANDARD
                .decode(&first.envelope.records[0].data_b64)
                .unwrap(),
            raw
        );
        assert_eq!(
            source_epoch::lane_position(&conn, first.source_epoch, SourceLane::Durable).unwrap(),
            0,
        );
        source_epoch::acknowledge_position(
            &mut conn,
            first.source_epoch,
            SourceLane::Durable,
            first.range_start,
            first.range_end,
        )
        .unwrap();
        assert!(
            prepare_next_cursor_acp_envelope(&mut conn, &capabilities(), &path)
                .unwrap()
                .is_none()
        );
    }

    #[test]
    fn cursor_prepares_source_faithful_raw_records_and_rotates_only_on_root_rewrite() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(first.envelope.provider, "cursor");
        assert_eq!(
            first.envelope.opaque_source_id,
            format!("cursor-store-v1:{CURSOR_CONVERSATION_ID}")
        );
        assert!(first.envelope.render.is_none());
        assert!(first.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "meta" && raw["meta_key"] == "unknown"
        }));
        source_epoch::acknowledge_position(
            &mut conn,
            first.source_epoch,
            SourceLane::Durable,
            first.range_start,
            first.range_end,
        )
        .unwrap();

        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xdd; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, X'0304')",
                [CURSOR_MESSAGE_B],
            )
            .unwrap();
        let extension = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(extension.source_epoch, first.source_epoch);
        source_epoch::acknowledge_position(
            &mut conn,
            extension.source_epoch,
            SourceLane::Durable,
            extension.range_start,
            extension.range_end,
        )
        .unwrap();

        set_cursor_root(&store, CURSOR_ROOT_C, &[0xff; 32]);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, X'0506')",
                [CURSOR_MESSAGE_C],
            )
            .unwrap();
        let rewrite = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_ne!(rewrite.source_epoch, first.source_epoch);
        assert_eq!(
            rewrite.envelope.predecessor_source_epoch,
            Some(first.source_epoch.to_string())
        );
        assert_eq!(rewrite.range_start, 0);
    }

    #[test]
    fn cursor_prepares_raw_records_without_a_root_pointer() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let metadata =
            format!(r#"{{"agentId":"{CURSOR_CONVERSATION_ID}","createdAt":1773403200000}}"#);
        let encoded: String = metadata
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        store
            .execute("UPDATE meta SET value = ?1 WHERE key = '0'", [encoded])
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.envelope.provider, "cursor");
        assert!(prepared.envelope.records.iter().all(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] != "root_observation"
        }));
    }

    #[test]
    fn prepares_exact_raw_bytes_and_versioned_render_without_advancing_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let bytes = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        fs::write(&path, bytes).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, 0);
        assert_eq!(prepared.range_end, bytes.len() as u64);
        assert_eq!(
            prepared.envelope.records[0].data_b64,
            BASE64_STANDARD.encode(bytes)
        );
        assert_eq!(prepared.envelope.render.as_ref().unwrap().records.len(), 1);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
    }

    #[test]
    fn prepare_reuses_durable_managed_binding_without_a_wake_hint() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-000000000042";
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind(&canonical.to_string_lossy(), managed_session_id, "claude")
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();

        assert_eq!(prepared.envelope.session_id, managed_session_id);
    }

    #[test]
    fn initial_v2_epoch_adopts_only_a_proven_legacy_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let path_text = canonical.to_string_lossy();
        FileState::new(&conn)
            .set_offset(
                &path_text,
                first.len() as u64,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, first.len() as u64);
    }

    #[test]
    fn initial_v2_epoch_replays_after_same_inode_truncate_and_regrow() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let replacement = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"jello\"}}\n";
        let second = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"world\"}}\n";
        fs::write(&path, [first.as_slice(), second.as_slice()].concat()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let canonical = fs::canonicalize(&path).unwrap();
        let path_text = canonical.to_string_lossy();
        let file_state = FileState::new(&conn);
        file_state
            .set_offset(
                &path_text,
                first.len() as u64,
                "018f0c3a-7b2d-7f10-8a11-123456789abc",
                "provider-session",
                "claude",
            )
            .unwrap();
        let stored_identity = file_state.get_file_identity(&path_text).unwrap();

        fs::write(&path, [replacement.as_slice(), second.as_slice()].concat()).unwrap();
        assert_eq!(
            stored_identity,
            identity_from_metadata(&path.metadata().unwrap()),
            "the regression must exercise truncate/regrow of the same file identity"
        );

        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, 0);
    }

    #[test]
    fn media_is_declared_without_changing_exact_provider_bytes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("019c638d-0000-0000-0000-000000000012.jsonl");
        let bytes = br#"{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":"data:image/png;base64,AAAA"}]}}
"#;
        fs::write(&path, bytes).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();

        assert_eq!(
            prepared.envelope.records[0].data_b64,
            BASE64_STANDARD.encode(bytes)
        );
        assert_eq!(prepared.media_objects.len(), 1);
        assert_eq!(prepared.media_objects[0].bytes, vec![0, 0, 0]);
        assert_eq!(prepared.envelope.media.len(), 1);
        assert_eq!(
            prepared.envelope.media[0].sha256,
            prepared.media_objects[0].sha256
        );
        assert_eq!(prepared.envelope.media[0].availability, "available");
        assert_eq!(prepared.envelope.media[0].source_position, 0);
    }

    #[tokio::test]
    async fn media_upload_precedes_envelope_and_failed_upload_keeps_cursor_for_exact_retry() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(Vec::<String>::new()));
        let server_requests = requests.clone();
        let server = tokio::spawn(async move {
            for request_index in 0..5 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut bytes = Vec::new();
                let mut buffer = [0_u8; 4096];
                let header_end = loop {
                    let read = socket.read(&mut buffer).await.unwrap();
                    assert!(read > 0);
                    bytes.extend_from_slice(&buffer[..read]);
                    if let Some(offset) = bytes.windows(4).position(|window| window == b"\r\n\r\n")
                    {
                        break offset + 4;
                    }
                };
                let headers = String::from_utf8_lossy(&bytes[..header_end]);
                let request_line = headers.lines().next().unwrap().to_string();
                let content_length = headers
                    .lines()
                    .find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        name.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap_or(0);
                while bytes.len() - header_end < content_length {
                    let read = socket.read(&mut buffer).await.unwrap();
                    assert!(read > 0);
                    bytes.extend_from_slice(&buffer[..read]);
                }
                server_requests.lock().unwrap().push(request_line.clone());
                let (status, body) = match request_index {
                    0 | 2 => {
                        let claim: serde_json::Value =
                            serde_json::from_slice(&bytes[header_end..]).unwrap();
                        let hash = claim["items"][0]["sha256"].as_str().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({"needed":[hash],"present":[],"rejected":[]})
                                .to_string(),
                        )
                    }
                    1 => ("503 Service Unavailable", "{}".to_string()),
                    3 => ("200 OK", "{}".to_string()),
                    4 => {
                        let envelope: serde_json::Value =
                            serde_json::from_slice(&bytes[header_end..]).unwrap();
                        let envelope_id = envelope["expected_envelope_id"].as_str().unwrap();
                        (
                            "200 OK",
                            serde_json::json!({
                                "v":2,
                                "envelope_id":envelope_id,
                                "object_hash":"b".repeat(64),
                                "commit_seq":"9",
                                "raw_state":"durable",
                                "render_state":"ready",
                                "media_state":"complete",
                                "missing_media_hashes":[]
                            })
                            .to_string(),
                        )
                    }
                    _ => unreachable!(),
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("019c638d-0000-0000-0000-000000000013.jsonl");
        let line = concat!(
            r#"{"type":"response_item","timestamp":"2026-03-01T10:00:00Z","payload":{"type":"message","role":"user","content":[{"type":"input_image","image_url":"data:image/png;base64,AAAA"}]}}"#,
            "\n"
        );
        fs::write(&path, line).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let first = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "codex",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await;
        assert!(first.is_err());
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "codex", None)
            .unwrap()
            .unwrap();
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );

        let second = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "codex",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(second.events_shipped, 1);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_end
        );
        server.await.unwrap();
        let observed = requests.lock().unwrap().clone();
        assert!(observed[0].starts_with("POST /api/agents/storage/v2/media/claims "));
        assert!(observed[1].starts_with("PUT /api/agents/storage/v2/media/"));
        assert!(observed[2].starts_with("POST /api/agents/storage/v2/media/claims "));
        assert!(observed[3].starts_with("PUT /api/agents/storage/v2/media/"));
        assert!(observed[4].starts_with("POST /api/agents/storage/v2/envelopes "));
    }

    fn create_opencode_db(path: &Path) {
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE project (id text PRIMARY KEY, worktree text NOT NULL, name text);
            CREATE TABLE session (
                id text PRIMARY KEY, project_id text NOT NULL, parent_id text,
                directory text, path text, title text, version text,
                time_created integer NOT NULL, time_updated integer NOT NULL
            );
            CREATE TABLE message (
                id text PRIMARY KEY, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
            );
            CREATE TABLE part (
                id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
                time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
            );
            INSERT INTO project VALUES ('project-1', '/tmp/longhouse', 'longhouse');
            INSERT INTO session VALUES (
                'session-1', 'project-1', NULL, '/tmp/longhouse', '/tmp/longhouse',
                'OpenCode test', '1', 1779000000000, 1779000000100
            );
            INSERT INTO message VALUES (
                'message-1', 'session-1', 1779000000010, 1779000000020, '{"role":"user"}'
            );
            INSERT INTO part VALUES (
                'part-1', 'message-1', 'session-1', 1779000000011, 1779000000011,
                '{"type":"text","text":"hello"}'
            );
            "#,
        )
        .unwrap();
    }

    #[test]
    fn opencode_uses_record_ordinals_and_keeps_one_parser_generation_across_source_revisions() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        assert_eq!(first.envelope.range_kind, "record_ordinal");
        assert_eq!((first.range_start, first.range_end), (0, 3));
        assert_eq!(first.envelope.records[0].source_position, 0);
        let first_render = first.envelope.render.as_ref().unwrap();
        assert_eq!(first_render.records[0].source_position, 2);
        assert_eq!(first_render.records[0].raw_record_ordinal, 2);
        assert_eq!(
            source_epoch::lane_position(&conn, first.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
        source_epoch::acknowledge_position(
            &mut conn,
            first.source_epoch,
            SourceLane::Durable,
            first.range_start,
            first.range_end,
        )
        .unwrap();

        let provider = Connection::open(&db_path).unwrap();
        provider
            .execute(
                "UPDATE part SET data = ?1, time_updated = ?2 WHERE id = 'part-1'",
                params![r#"{"type":"text","text":"hello again"}"#, 1779000000200_i64],
            )
            .unwrap();
        let second = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        assert_ne!(second.source_epoch, first.source_epoch);
        assert_eq!(second.range_start, 0);
        let first_epoch = first.source_epoch.to_string();
        assert_eq!(
            second.envelope.predecessor_source_epoch.as_deref(),
            Some(first_epoch.as_str())
        );
        assert_eq!(
            second.envelope.render.as_ref().unwrap().generation_id,
            first_render.generation_id
        );
    }

    #[test]
    fn opencode_storage_v2_exhausts_sessions_older_than_the_newest_64() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let provider = Connection::open(&db_path).unwrap();
        for index in 2..=65 {
            let session_id = format!("session-{index}");
            let message_id = format!("message-{index}");
            let part_id = format!("part-{index}");
            let timestamp = 1_779_000_000_000_i64 + index;
            provider
                .execute(
                    "INSERT INTO session VALUES (?1, 'project-1', NULL, '/tmp/longhouse', '/tmp/longhouse', 'OpenCode test', '1', ?2, ?2)",
                    params![session_id, timestamp],
                )
                .unwrap();
            provider
                .execute(
                    "INSERT INTO message VALUES (?1, ?2, ?3, ?3, '{\"role\":\"user\"}')",
                    params![message_id, session_id, timestamp + 1],
                )
                .unwrap();
            provider
                .execute(
                    "INSERT INTO part VALUES (?1, ?2, ?3, ?4, ?4, '{\"type\":\"text\",\"text\":\"hello\"}')",
                    params![part_id, message_id, session_id, timestamp + 2],
                )
                .unwrap();
        }
        drop(provider);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let mut shipped_sources = std::collections::HashSet::new();
        while let Some(prepared) =
            prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path).unwrap()
        {
            shipped_sources.insert(prepared.envelope.opaque_source_id.clone());
            source_epoch::acknowledge_position(
                &mut conn,
                prepared.source_epoch,
                SourceLane::Durable,
                prepared.range_start,
                prepared.range_end,
            )
            .unwrap();
        }
        assert_eq!(shipped_sources.len(), 65);
    }
}
