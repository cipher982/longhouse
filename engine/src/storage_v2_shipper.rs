//! Parser-independent raw + parser-versioned render shipping for storage-v2.

use std::collections::HashMap;
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use chrono::{DateTime, Utc};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use serde_json::Value;
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
    StorageV2SourceManifest,
};
use crate::shipping::storage_v2::{StorageV2Render, StorageV2RenderRecord, StorageV2SessionFacts};
use crate::state::cursor_store_records;
use crate::state::cursor_store_root;
use crate::state::file_identity::{cursor_fingerprint, identity_from_metadata};
use crate::state::file_state::FileState;
use crate::state::pending_source_envelope::{self, PendingSourceEnvelope};
use crate::state::source_epoch::{self, SourceChangeHint, SourceEpochResolution, SourceLane};
use crate::storage_v2_contract::{self, EnvelopeIdentity, RangeKind};

pub(crate) const PARSER_REVISION: &str = "engine-parser-v2";
pub(crate) const ORDERING_REVISION: &str = "semantic-order-v2";
const OPENCODE_SESSION_PAGE_SIZE: usize = 64;
const CURSOR_PARSER_REVISION: &str = "cursor-store-render-v2";
const LIVE_TARGET_BATCH_BYTES: usize = 64 * 1024;

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

#[derive(Debug)]
pub(crate) struct StorageV2ShipOutcome {
    pub bytes_shipped: u64,
    pub events_shipped: usize,
    pub has_more: bool,
}

#[derive(Debug, Serialize, Deserialize)]
struct PersistedMediaObject {
    source_offset: u64,
    sha256: String,
    mime_type: String,
    byte_size: usize,
    original_chars: usize,
    original_line_sha256: String,
    data_b64: String,
}

#[derive(Debug, thiserror::Error)]
#[error("storage-v2 source preparation failed: {source:#}")]
pub(crate) struct StorageV2PreparationError {
    #[source]
    source: anyhow::Error,
}

#[derive(Debug, thiserror::Error)]
#[error("storage-v2 source {source_epoch} blocked ({kind}): {detail}")]
pub(crate) struct StorageV2SourceBlocked {
    pub source_epoch: Uuid,
    pub kind: String,
    pub detail: String,
    pub newly_blocked: bool,
}

fn preparation_result<T>(result: Result<T>) -> Result<T> {
    result.map_err(|source| StorageV2PreparationError { source }.into())
}

pub(crate) fn prepare_next_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    session_id_override: Option<&str>,
) -> Result<Option<PreparedStorageV2Envelope>> {
    prepare_next_envelope_with_limit(
        conn,
        capabilities,
        path,
        provider,
        session_id_override,
        MAX_RAW_BATCH_BYTES,
    )
}

fn prepare_next_envelope_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    session_id_override: Option<&str>,
    maximum_batch_bytes: usize,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let canonical_path = stable_source_path(path);
    let path_text = canonical_path.to_string_lossy();
    let opaque_source_id = opaque_source_id(&path_text);
    if let Some(pending) =
        pending_source_envelope::load_for_source(conn, provider, &opaque_source_id)?
    {
        let oversized_unattempted = pending.raw_bytes > maximum_batch_bytes as u64
            && pending.attempt_count == 0
            && maximum_batch_bytes < MAX_RAW_BATCH_BYTES;
        if !oversized_unattempted
            || !pending_source_envelope::discard_unattempted(
                conn,
                pending.source_epoch,
                &pending.envelope_id,
            )?
        {
            return pending_to_prepared(pending).map(Some);
        }
    }
    let durable_session_id = match session_id_override {
        Some(value) => Some(value.to_string()),
        None => crate::state::session_binding::SessionBinding::new(conn).get(&path_text)?,
    };
    let session_id_override = durable_session_id.as_deref();
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
        maximum_batch_bytes,
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
    if let Err(error) =
        crate::state::session_title::observe_parse_result(conn, &session_id, &parse_result)
    {
        tracing::warn!(session_id, error = %error, "Unable to persist local prompt title");
    }
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
    let prepared = PreparedStorageV2Envelope {
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
    };
    persist_prepared(conn, &path_text, prepared).map(Some)
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
    let maximum_batch_bytes = if lane == "live" {
        LIVE_TARGET_BATCH_BYTES
    } else {
        MAX_RAW_BATCH_BYTES
    };
    let Some(prepared) = preparation_result(prepare_next_envelope_with_limit(
        conn,
        capabilities,
        path,
        provider,
        session_id_override,
        maximum_batch_bytes,
    ))?
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
    let pending = pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
        .context("prepared storage-v2 envelope is not durable")?;
    validate_pending_matches_prepared(&pending, &prepared)?;
    if let Some(blocked_at) = pending.blocked_at.as_deref() {
        return Err(StorageV2SourceBlocked {
            source_epoch: prepared.source_epoch,
            kind: pending
                .block_kind
                .clone()
                .unwrap_or_else(|| "source_blocked".to_string()),
            detail: pending
                .block_detail
                .clone()
                .unwrap_or_else(|| format!("blocked at {blocked_at}")),
            newly_blocked: false,
        }
        .into());
    }
    if prepared.envelope.tenant_id != capabilities.tenant_id
        || prepared.envelope.machine_id != capabilities.machine_id
    {
        return block_source(
            conn,
            prepared.source_epoch,
            "storage_target_changed",
            "durable envelope tenant or machine does not match current Runtime Host capabilities",
        );
    }
    let was_retry = pending.attempt_count > 0;
    pending_source_envelope::mark_attempt(conn, prepared.source_epoch)?;
    crate::media_upload::ensure_storage_v2_media_uploaded(
        client,
        capabilities,
        &prepared.media_objects,
        lane,
        Some(request_timeout),
    )
    .await?;
    let receipt = match client
        .ship_storage_v2_body(
            &capabilities.ingest_path,
            lane,
            decode_zstd(&pending.request_body_zstd, "storage-v2 request body")?,
            &pending.envelope_id,
            Some(request_timeout),
        )
        .await
    {
        Ok(receipt) => receipt,
        Err(error) => {
            if let Some(conflict) =
                error.downcast_ref::<crate::shipping::client::StorageV2Conflict>()
            {
                if let Some(outcome) = reconcile_storage_v2_conflict(
                    conn,
                    client,
                    &pending,
                    &prepared,
                    request_timeout,
                )
                .await?
                {
                    return Ok(outcome);
                }
                return block_source(
                    conn,
                    prepared.source_epoch,
                    &conflict.code,
                    &conflict.response_body,
                );
            }
            return Err(error);
        }
    };
    pending_source_envelope::acknowledge_and_delete(
        conn,
        prepared.source_epoch,
        &receipt.envelope_id,
        prepared.range_start,
        prepared.range_end,
    )?;
    Ok(StorageV2ShipOutcome {
        bytes_shipped: prepared.raw_bytes,
        events_shipped: prepared.event_count,
        has_more: prepared.has_more || was_retry,
    })
}

async fn reconcile_storage_v2_conflict(
    conn: &mut Connection,
    client: &ShipperClient,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let manifest = client
        .storage_v2_source_manifest(
            &prepared.source_epoch.to_string(),
            prepared.range_start,
            Some(request_timeout),
        )
        .await?;
    let Some(proven_through) = proven_manifest_prefix(prepared, &manifest)? else {
        return Ok(None);
    };
    let prefix_events = prepared
        .envelope
        .render
        .as_ref()
        .map(|render| {
            render
                .records
                .iter()
                .filter(|record| record.source_position < proven_through)
                .count()
        })
        .unwrap_or(0);
    let replacement_prepared = if proven_through < prepared.range_end {
        Some(split_prepared_suffix(prepared, proven_through)?)
    } else {
        None
    };
    let replacement = replacement_prepared
        .as_ref()
        .map(|suffix| pending_candidate(&pending.source_path, suffix))
        .transpose()?;
    pending_source_envelope::reconcile_proven_prefix(
        conn,
        prepared.source_epoch,
        &pending.envelope_id,
        prepared.range_start,
        proven_through,
        replacement.as_ref(),
    )?;
    Ok(Some(StorageV2ShipOutcome {
        bytes_shipped: proven_through - prepared.range_start,
        events_shipped: prefix_events,
        has_more: replacement.is_some() || prepared.has_more,
    }))
}

fn proven_manifest_prefix(
    prepared: &PreparedStorageV2Envelope,
    manifest: &StorageV2SourceManifest,
) -> Result<Option<u64>> {
    let envelope = &prepared.envelope;
    let epoch = &manifest.source_epoch;
    if manifest.v != 2
        || manifest.commit_seq.parse::<u64>().is_err()
        || epoch.source_epoch != envelope.source_epoch
        || epoch.tenant_id != envelope.tenant_id
        || epoch.machine_id != envelope.machine_id
        || epoch.provider != envelope.provider
        || epoch.opaque_source_id != envelope.opaque_source_id
        || epoch.range_kind != envelope.range_kind
    {
        return Ok(None);
    }
    let mut proven_through = prepared.range_start;
    for object in &manifest.objects {
        if object.source_epoch != envelope.source_epoch
            || object.tenant_id != envelope.tenant_id
            || object.machine_id != envelope.machine_id
            || object.provider != envelope.provider
            || object.opaque_source_id != envelope.opaque_source_id
            || object.range_kind != envelope.range_kind
            || object.retired_at.is_some()
        {
            if proven_through > prepared.range_start {
                break;
            }
            return Ok(None);
        }
        let Ok(range_start) = object.range_start.parse::<u64>() else {
            if proven_through > prepared.range_start {
                break;
            }
            return Ok(None);
        };
        let Ok(range_end) = object.range_end.parse::<u64>() else {
            if proven_through > prepared.range_start {
                break;
            }
            return Ok(None);
        };
        if range_start != proven_through
            || range_end <= range_start
            || range_end > prepared.range_end
        {
            break;
        }
        let Ok(computed_envelope_id) = envelope_id_for_subrange(envelope, range_start, range_end)
        else {
            if proven_through > prepared.range_start {
                break;
            }
            return Ok(None);
        };
        if computed_envelope_id != object.envelope_id {
            if proven_through > prepared.range_start {
                break;
            }
            return Ok(None);
        }
        proven_through = range_end;
        if proven_through == prepared.range_end {
            break;
        }
    }
    Ok((proven_through > prepared.range_start).then_some(proven_through))
}

fn split_prepared_suffix(
    prepared: &PreparedStorageV2Envelope,
    range_start: u64,
) -> Result<PreparedStorageV2Envelope> {
    if range_start <= prepared.range_start || range_start >= prepared.range_end {
        anyhow::bail!("storage-v2 suffix split is outside the pending range");
    }
    let removed_records = prepared
        .envelope
        .records
        .iter()
        .filter(|record| record.source_position < range_start)
        .count();
    let mut envelope = prepared.envelope.clone();
    envelope.range_start = range_start;
    envelope
        .records
        .retain(|record| record.source_position >= range_start);
    if envelope.records.is_empty() {
        anyhow::bail!("storage-v2 reconciled suffix has no raw records");
    }
    if let Some(render) = envelope.render.as_mut() {
        render
            .records
            .retain(|record| record.source_position >= range_start);
        for record in &mut render.records {
            record.raw_record_ordinal = record
                .raw_record_ordinal
                .checked_sub(removed_records)
                .context("storage-v2 render ordinal precedes reconciled suffix")?;
        }
    }
    envelope
        .media
        .retain(|media| media.source_position >= range_start);
    envelope.expected_envelope_id =
        envelope_id_for_subrange(&envelope, range_start, prepared.range_end)?;
    let decoded_bytes = decode_envelope_record_bytes(&envelope.records)?;
    let raw_bytes = if envelope.range_kind == "byte_offset" {
        prepared.range_end - range_start
    } else {
        decoded_bytes.iter().try_fold(0u64, |total, bytes| {
            total
                .checked_add(u64::try_from(bytes.len()).context("raw record exceeds u64")?)
                .context("storage-v2 suffix raw byte count overflow")
        })?
    };
    let event_count = envelope
        .render
        .as_ref()
        .map(|render| render.records.len())
        .unwrap_or(0);
    let has_reply_evidence = envelope.render.as_ref().is_some_and(|render| {
        render
            .records
            .iter()
            .any(|record| matches!(record.role.as_str(), "assistant" | "tool"))
    });
    Ok(PreparedStorageV2Envelope {
        envelope,
        source_epoch: prepared.source_epoch,
        range_start,
        range_end: prepared.range_end,
        event_count,
        has_reply_evidence,
        raw_bytes,
        has_more: prepared.has_more,
        media_objects: prepared
            .media_objects
            .iter()
            .filter(|media| media.source_offset >= range_start)
            .cloned()
            .collect(),
    })
}

fn envelope_id_for_subrange(
    envelope: &StorageV2Envelope,
    range_start: u64,
    range_end: u64,
) -> Result<String> {
    let records = envelope
        .records
        .iter()
        .filter(|record| {
            record.source_position >= range_start && record.source_position < range_end
        })
        .cloned()
        .collect::<Vec<_>>();
    let raw_bytes = decode_envelope_record_bytes(&records)?;
    match envelope.range_kind.as_str() {
        "byte_offset" => {
            let mut expected = range_start;
            for (record, bytes) in records.iter().zip(&raw_bytes) {
                if record.source_position != expected {
                    anyhow::bail!("storage-v2 byte range is not contiguous");
                }
                expected = expected
                    .checked_add(u64::try_from(bytes.len()).context("raw record exceeds u64")?)
                    .context("storage-v2 byte range overflow")?;
            }
            if expected != range_end {
                anyhow::bail!("storage-v2 byte range does not end at manifest boundary");
            }
        }
        "record_ordinal" => {
            if records.len() != usize::try_from(range_end - range_start)?
                || records
                    .iter()
                    .enumerate()
                    .any(|(index, record)| record.source_position != range_start + index as u64)
            {
                anyhow::bail!("storage-v2 ordinal range is not contiguous");
            }
        }
        _ => anyhow::bail!("storage-v2 pending envelope has an unknown range kind"),
    }
    let identity = EnvelopeIdentity {
        tenant_id: envelope.tenant_id.clone(),
        machine_id: envelope.machine_id.clone(),
        provider: envelope.provider.clone(),
        opaque_source_id: envelope.opaque_source_id.clone(),
        source_epoch: Uuid::parse_str(&envelope.source_epoch)
            .context("storage-v2 pending source epoch is invalid")?,
        range_kind: if envelope.range_kind == "byte_offset" {
            RangeKind::ByteOffset
        } else {
            RangeKind::RecordOrdinal
        },
        range_start,
        range_end,
        record_hashes: storage_v2_contract::hash_records(&raw_bytes),
    };
    Ok(hex_hash(storage_v2_contract::envelope_id(&identity)?))
}

fn decode_envelope_record_bytes(records: &[StorageV2Record]) -> Result<Vec<Vec<u8>>> {
    records
        .iter()
        .map(|record| {
            BASE64_STANDARD
                .decode(&record.data_b64)
                .context("decoding persisted storage-v2 raw record")
        })
        .collect()
}

fn block_source<T>(conn: &Connection, source_epoch: Uuid, kind: &str, detail: &str) -> Result<T> {
    let newly_blocked = pending_source_envelope::quarantine(conn, source_epoch, kind, detail)?;
    Err(StorageV2SourceBlocked {
        source_epoch,
        kind: kind.to_string(),
        detail: detail.to_string(),
        newly_blocked,
    }
    .into())
}

pub(crate) fn prepare_next_opencode_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let canonical_path = stable_source_path(db_path);
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
            let opaque_source_id = opaque_source_id(&format!(
                "{path_text}\0opencode-session\0{}",
                candidate.provider_session_id
            ));
            if pending_source_envelope::source_is_blocked(conn, "opencode", &opaque_source_id)? {
                continue;
            }
            if let Some(pending) = load_pending_for_source(conn, "opencode", &opaque_source_id)? {
                return Ok(Some(pending));
            }
            let snapshot =
                opencode_db::opencode_raw_snapshot(db_path, &candidate.provider_session_id)?;
            let logical_len = u64::try_from(snapshot.records.len())
                .context("OpenCode snapshot has too many records")?;
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
            if let Err(error) =
                crate::state::session_title::observe_parse_result(conn, &session_id, &parse_result)
            {
                tracing::warn!(session_id, error = %error, "Unable to persist local OpenCode prompt title");
            }
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
            let prepared = PreparedStorageV2Envelope {
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
            };
            return persist_prepared(conn, &path_text, prepared).map(Some);
        }
        page_offset = page_offset.saturating_add(candidates.len());
    }
}

fn cursor_render_records(
    snapshot: &cursor_store::CursorStoreSnapshot,
    selected: &[cursor_store_records::CursorRawRecord],
    started_at_us: i64,
) -> Result<Vec<StorageV2RenderRecord>> {
    let cursor_store::RootMessageBlobIds::Parsed(root_ids) = &snapshot.root_message_blob_ids else {
        return Ok(Vec::new());
    };
    let mut selected_blobs: HashMap<String, (u64, usize, Vec<u8>)> = HashMap::new();
    for (raw_record_ordinal, record) in selected.iter().enumerate() {
        let Ok(wrapper) = serde_json::from_slice::<Value>(&record.bytes) else {
            continue;
        };
        if wrapper.get("kind").and_then(Value::as_str) != Some("blob") {
            continue;
        }
        let Some(blob_id) = wrapper.get("blob_id").and_then(Value::as_str) else {
            continue;
        };
        let Some(encoded) = wrapper.get("blob_bytes_b64").and_then(Value::as_str) else {
            continue;
        };
        let Ok(bytes) = BASE64_STANDARD.decode(encoded) else {
            continue;
        };
        selected_blobs.insert(
            blob_id.to_string(),
            (record.source_position, raw_record_ordinal, bytes),
        );
    }
    let mut records = Vec::new();
    for (message_order, blob_id) in root_ids.iter().enumerate() {
        let Some((source_position, raw_record_ordinal, blob_bytes)) = selected_blobs.get(blob_id)
        else {
            continue;
        };
        let message: Value = match serde_json::from_slice(blob_bytes) {
            Ok(value) => value,
            Err(_) => continue,
        };
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("assistant");
        let blocks: Vec<Value> = match message.get("content") {
            Some(Value::Array(values)) => values.clone(),
            Some(Value::String(text)) => vec![serde_json::json!({"type":"text","text":text})],
            _ => Vec::new(),
        };
        for (subordinal, block) in blocks.iter().enumerate() {
            let kind = block
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("unknown");
            let (
                event_role,
                content_text,
                tool_name,
                tool_input_json,
                tool_output_text,
                tool_call_id,
            ) = match kind {
                "text" | "reasoning" => {
                    let text = block
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or_default();
                    let (effective_role, effective_text) = if kind == "reasoning" {
                        ("assistant".to_string(), text.to_string())
                    } else {
                        classify_cursor_text(role, text)
                    };
                    (effective_role, Some(effective_text), None, None, None, None)
                }
                "tool-call" => (
                    "assistant".to_string(),
                    None,
                    block
                        .get("toolName")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    block.get("args").or_else(|| block.get("input")).cloned(),
                    None,
                    block
                        .get("toolCallId")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                ),
                "tool-result" => (
                    "tool".to_string(),
                    None,
                    block
                        .get("toolName")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                    None,
                    block.get("result").map(|value| match value {
                        Value::String(text) => text.clone(),
                        other => other.to_string(),
                    }),
                    block
                        .get("toolCallId")
                        .and_then(Value::as_str)
                        .map(str::to_owned),
                ),
                _ => (
                    role.to_string(),
                    Some(String::new()),
                    None,
                    None,
                    None,
                    None,
                ),
            };
            records.push(StorageV2RenderRecord {
                event_id: Uuid::new_v5(
                    &Uuid::NAMESPACE_URL,
                    format!("cursor:{blob_id}:{subordinal}").as_bytes(),
                )
                .to_string(),
                order_time_us: started_at_us + message_order as i64 * 1_000 + subordinal as i64,
                source_position: *source_position,
                event_subordinal: subordinal as u32,
                role: event_role,
                content_text,
                tool_name,
                tool_input_json,
                tool_output_text,
                tool_call_id,
                thread_id: None,
                branch_kind: (kind == "reasoning").then(|| "reasoning".to_string()),
                raw_record_ordinal: *raw_record_ordinal,
            });
        }
    }
    Ok(records)
}

fn classify_cursor_text(role: &str, text: &str) -> (String, String) {
    if role != "user" {
        return (role.to_string(), text.to_string());
    }
    if let Some(query_start) = text.find("<user_query>") {
        let inner_start = query_start + "<user_query>".len();
        if let Some(query_end) = text[inner_start..].find("</user_query>") {
            return (
                "user".to_string(),
                text[inner_start..inner_start + query_end]
                    .trim()
                    .to_string(),
            );
        }
    }
    const INJECTION_MARKERS: [&str; 6] = [
        "<user_info>",
        "<agent_transcripts>",
        "<rules>",
        "<system_reminder>",
        "<attached_files>",
        "<system_notification>",
    ];
    if INJECTION_MARKERS.iter().any(|marker| text.contains(marker)) {
        return ("system".to_string(), text.to_string());
    }
    ("user".to_string(), text.to_string())
}

pub(crate) fn prepare_next_cursor_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    prepare_next_cursor_envelope_with_limit(
        conn,
        capabilities,
        db_path,
        capabilities.max_raw_record_bytes,
    )
}

fn prepare_next_cursor_envelope_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    maximum_batch_bytes: u64,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let canonical_path = stable_source_path(db_path);
    let path_text = canonical_path.to_string_lossy();
    if let Some(pending) = pending_source_envelope::load_for_path(conn, &path_text)? {
        let oversized_unattempted = pending.raw_bytes > maximum_batch_bytes
            && pending.range_end.saturating_sub(pending.range_start) > 1
            && pending.attempt_count == 0
            && maximum_batch_bytes < capabilities.max_raw_record_bytes;
        if !oversized_unattempted
            || !pending_source_envelope::discard_unattempted(
                conn,
                pending.source_epoch,
                &pending.envelope_id,
            )?
        {
            return pending_to_prepared(pending).map(Some);
        }
    }
    let store_snapshot = cursor_store::read_cursor_store(db_path)?;
    let store_incarnation = identity_from_metadata(
        &db_path
            .metadata()
            .with_context(|| format!("reading Cursor store metadata {}", db_path.display()))?,
    )
    .context("Cursor store has no stable file incarnation")?;
    let snapshot =
        cursor_store::cursor_store_raw_snapshot_from(&store_snapshot, store_incarnation)?;
    let claimed_session_id = crate::cursor_launch_binding::managed_session_id_for_conversation(
        &snapshot.conversation_uuid,
    )?;
    if claimed_session_id.is_none()
        && crate::cursor_launch_binding::pending_claim_for_conversation(
            &snapshot.conversation_uuid,
        )?
    {
        return Ok(None);
    }
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
    let mut selected_bytes = 0u64;
    let selected = selected
        .into_iter()
        .take_while(|record| {
            let record_bytes = record.bytes.len() as u64;
            if selected_bytes > 0
                && selected_bytes.saturating_add(record_bytes) > maximum_batch_bytes
            {
                return false;
            }
            // One source record is atomic. It may exceed the live target but
            // never the negotiated per-record capability enforced above.
            selected_bytes = selected_bytes.saturating_add(record_bytes);
            true
        })
        .collect::<Vec<_>>();
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
    let render_records =
        cursor_render_records(&store_snapshot, &selected, started_at.timestamp_micros())?;
    let render_generation = cursor_render_generation_id(&session_id);
    let has_reply_evidence = render_records
        .iter()
        .any(|record| record.role == "assistant" || record.role == "tool");
    let event_count = render_records.len();
    let render = (!render_records.is_empty()).then(|| StorageV2Render {
        generation_id: render_generation.to_string(),
        parser_revision: CURSOR_PARSER_REVISION.to_string(),
        ordering_revision: "cursor-root-order-v1".to_string(),
        records: render_records,
    });
    let render_ready = render.is_some();
    let prepared = PreparedStorageV2Envelope {
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
            render,
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
                hidden_from_default_timeline: managed_session_id.is_none() || !render_ready,
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
        event_count,
        has_reply_evidence,
        raw_bytes,
        has_more: range_end < logical_len,
        media_objects: Vec::new(),
    };
    persist_prepared(conn, &path_text, prepared).map(Some)
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
    let canonical_path = stable_source_path(path);
    let path_text = canonical_path.to_string_lossy();
    if let Some(pending) = load_pending_for_source(conn, "cursor", &opaque_source_id)? {
        return Ok(Some(pending));
    }
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
    let prepared = PreparedStorageV2Envelope {
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
    };
    persist_prepared(conn, &path_text, prepared).map(Some)
}

fn persist_prepared(
    conn: &mut Connection,
    source_path: &str,
    prepared: PreparedStorageV2Envelope,
) -> Result<PreparedStorageV2Envelope> {
    let candidate = pending_candidate(source_path, &prepared)?;
    let persisted = pending_source_envelope::persist_or_load(conn, &candidate)?;
    pending_to_prepared(persisted)
}

fn pending_candidate(
    source_path: &str,
    prepared: &PreparedStorageV2Envelope,
) -> Result<PendingSourceEnvelope> {
    let request_body = serde_json::to_vec(&prepared.envelope)
        .context("serializing storage-v2 envelope before durable prepare")?;
    let media_objects = prepared
        .media_objects
        .iter()
        .map(PersistedMediaObject::from)
        .collect::<Vec<_>>();
    let media_json = serde_json::to_vec(&media_objects)
        .context("serializing storage-v2 media before durable prepare")?;
    Ok(PendingSourceEnvelope::new(
        prepared.source_epoch,
        source_path.to_string(),
        prepared.range_start,
        prepared.range_end,
        prepared.envelope.expected_envelope_id.clone(),
        encode_zstd(&request_body, "storage-v2 request body")?,
        encode_zstd(&media_json, "storage-v2 media")?,
        prepared.raw_bytes,
        prepared.event_count,
        prepared.has_reply_evidence,
        prepared.has_more,
    ))
}

fn load_pending_for_source(
    conn: &Connection,
    provider: &str,
    opaque_source_id: &str,
) -> Result<Option<PreparedStorageV2Envelope>> {
    pending_source_envelope::load_for_source(conn, provider, opaque_source_id)?
        .map(pending_to_prepared)
        .transpose()
}

/// Keep a stable lookup key after the source itself is unlinked. On macOS,
/// canonicalizing `/var/.../file` while it exists yields `/private/var/...`,
/// so falling back to the original path after deletion would orphan pending
/// work. The parent remains canonicalizable in that crash/retry window.
fn stable_source_path(path: &Path) -> PathBuf {
    if let Ok(canonical) = std::fs::canonicalize(path) {
        return canonical;
    }
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    };
    match (absolute.parent(), absolute.file_name()) {
        (Some(parent), Some(file_name)) => std::fs::canonicalize(parent)
            .map(|canonical_parent| canonical_parent.join(file_name))
            .unwrap_or(absolute),
        _ => absolute,
    }
}

fn pending_to_prepared(pending: PendingSourceEnvelope) -> Result<PreparedStorageV2Envelope> {
    let request_body = decode_zstd(&pending.request_body_zstd, "storage-v2 request body")?;
    let envelope: StorageV2Envelope = serde_json::from_slice(&request_body)
        .context("decoding durable storage-v2 request body")?;
    let media_json = decode_zstd(&pending.media_objects_zstd, "storage-v2 media")?;
    let media_objects = serde_json::from_slice::<Vec<PersistedMediaObject>>(&media_json)
        .context("decoding durable storage-v2 media")?
        .into_iter()
        .map(ParsedMediaObject::try_from)
        .collect::<Result<Vec<_>>>()?;
    let prepared = PreparedStorageV2Envelope {
        envelope,
        source_epoch: pending.source_epoch,
        range_start: pending.range_start,
        range_end: pending.range_end,
        event_count: pending.event_count,
        has_reply_evidence: pending.has_reply_evidence,
        raw_bytes: pending.raw_bytes,
        has_more: pending.has_more,
        media_objects,
    };
    validate_pending_matches_prepared(&pending, &prepared)?;
    Ok(prepared)
}

fn validate_pending_matches_prepared(
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
) -> Result<()> {
    if prepared.source_epoch != pending.source_epoch
        || prepared.envelope.source_epoch != pending.source_epoch.to_string()
        || prepared.range_start != pending.range_start
        || prepared.range_end != pending.range_end
        || prepared.envelope.range_start != pending.range_start
        || prepared.envelope.range_end != pending.range_end
        || prepared.envelope.expected_envelope_id != pending.envelope_id
    {
        anyhow::bail!("durable storage-v2 envelope metadata does not match its request body");
    }
    Ok(())
}

fn encode_zstd(bytes: &[u8], label: &str) -> Result<Vec<u8>> {
    zstd::stream::encode_all(Cursor::new(bytes), 1)
        .with_context(|| format!("compressing durable {label}"))
}

fn decode_zstd(bytes: &[u8], label: &str) -> Result<Vec<u8>> {
    zstd::stream::decode_all(Cursor::new(bytes))
        .with_context(|| format!("decompressing durable {label}"))
}

impl From<&ParsedMediaObject> for PersistedMediaObject {
    fn from(media: &ParsedMediaObject) -> Self {
        Self {
            source_offset: media.source_offset,
            sha256: media.sha256.clone(),
            mime_type: media.mime_type.clone(),
            byte_size: media.byte_size,
            original_chars: media.original_chars,
            original_line_sha256: media.original_line_sha256.clone(),
            data_b64: BASE64_STANDARD.encode(&media.bytes),
        }
    }
}

impl TryFrom<PersistedMediaObject> for ParsedMediaObject {
    type Error = anyhow::Error;

    fn try_from(media: PersistedMediaObject) -> Result<Self> {
        let bytes = BASE64_STANDARD
            .decode(&media.data_b64)
            .context("decoding durable storage-v2 media bytes")?;
        if bytes.len() != media.byte_size {
            anyhow::bail!("durable storage-v2 media byte size changed");
        }
        Ok(Self {
            source_offset: media.source_offset,
            sha256: media.sha256,
            mime_type: media.mime_type,
            byte_size: media.byte_size,
            original_chars: media.original_chars,
            original_line_sha256: media.original_line_sha256,
            bytes,
        })
    }
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
    let Some(prepared) =
        preparation_result(prepare_next_opencode_envelope(conn, capabilities, db_path))?
    else {
        return Ok(None);
    };
    ship_prepared_envelope(conn, client, capabilities, prepared, lane, request_timeout)
        .await
        .map(Some)
}

pub(crate) async fn ship_next_cursor_envelope(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    lane: &str,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let maximum_batch_bytes = if lane == "live" {
        LIVE_TARGET_BATCH_BYTES as u64
    } else {
        capabilities.max_raw_record_bytes
    };
    let Some(prepared) = preparation_result(prepare_next_cursor_envelope_with_limit(
        conn,
        capabilities,
        db_path,
        maximum_batch_bytes,
    ))?
    else {
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
    let Some(prepared) =
        preparation_result(prepare_next_cursor_acp_envelope(conn, capabilities, path))?
    else {
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

fn cursor_render_generation_id(session_id: &str) -> Uuid {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!(
            "longhouse-cursor-render-v2\0{session_id}\0{CURSOR_PARSER_REVISION}\0cursor-root-order-v1"
        )
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

    #[test]
    fn preparation_errors_are_distinct_from_transport_failures() {
        let error =
            preparation_result::<()>(Err(anyhow::anyhow!("unsupported local shape"))).unwrap_err();
        assert!(error.downcast_ref::<StorageV2PreparationError>().is_some());
    }

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

    fn acknowledge_prepared(conn: &mut Connection, prepared: &PreparedStorageV2Envelope) {
        pending_source_envelope::acknowledge_and_delete(
            conn,
            prepared.source_epoch,
            &prepared.envelope.expected_envelope_id,
            prepared.range_start,
            prepared.range_end,
        )
        .unwrap();
    }

    async fn read_http_request(socket: &mut tokio::net::TcpStream) -> (String, Vec<u8>) {
        let mut bytes = Vec::new();
        let mut buffer = [0_u8; 4096];
        let header_end = loop {
            let read = socket.read(&mut buffer).await.unwrap();
            assert!(read > 0, "request closed before headers completed");
            bytes.extend_from_slice(&buffer[..read]);
            if let Some(offset) = bytes.windows(4).position(|window| window == b"\r\n\r\n") {
                break offset + 4;
            }
        };
        let headers = String::from_utf8_lossy(&bytes[..header_end]).into_owned();
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
            assert!(read > 0, "request closed before body completed");
            bytes.extend_from_slice(&buffer[..read]);
        }
        (
            headers.lines().next().unwrap_or_default().to_string(),
            bytes[header_end..header_end + content_length].to_vec(),
        )
    }

    async fn read_http_body(socket: &mut tokio::net::TcpStream) -> Vec<u8> {
        read_http_request(socket).await.1
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
        acknowledge_prepared(&mut conn, &first);
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
        acknowledge_prepared(&mut conn, &first);

        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xdd; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    CURSOR_MESSAGE_B,
                    br#"{"role":"assistant","content":[{"type":"text","text":"second turn"}]}"#
                ],
            )
            .unwrap();
        let extension = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(extension.source_epoch, first.source_epoch);
        assert_eq!(
            extension.envelope.render.as_ref().unwrap().generation_id,
            cursor_render_generation_id(&extension.envelope.session_id).to_string()
        );
        let extension_render = extension.envelope.render.as_ref().unwrap();
        assert_eq!(extension_render.records.len(), 1);
        assert_eq!(
            extension_render.records[0].content_text.as_deref(),
            Some("second turn")
        );
        acknowledge_prepared(&mut conn, &extension);

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
    fn cursor_store_emits_readable_text_and_tool_render_records() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let message = serde_json::json!({
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello from Cursor"},
                {"type": "tool-call", "toolName": "Shell", "toolCallId": "call-1", "args": {"command": "pwd"}},
                {"type": "tool-result", "toolName": "Shell", "toolCallId": "call-1", "result": "/tmp"}
            ]
        });
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![serde_json::to_vec(&message).unwrap(), CURSOR_MESSAGE_A],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let render = prepared.envelope.render.unwrap();
        assert_eq!(render.parser_revision, CURSOR_PARSER_REVISION);
        assert_eq!(render.records.len(), 3);
        assert_eq!(
            render.records[0].content_text.as_deref(),
            Some("hello from Cursor")
        );
        assert_eq!(render.records[1].tool_call_id.as_deref(), Some("call-1"));
        assert_eq!(render.records[2].role, "tool");
        assert_eq!(render.records[2].tool_output_text.as_deref(), Some("/tmp"));
    }

    #[test]
    fn cursor_render_hides_injected_context_and_unwraps_real_user_query() {
        assert_eq!(
            classify_cursor_text(
                "user",
                "<user_info>darwin</user_info><rules>workspace</rules>"
            ),
            (
                "system".to_string(),
                "<user_info>darwin</user_info><rules>workspace</rules>".to_string()
            )
        );
        assert_eq!(
            classify_cursor_text(
                "user",
                "<user_info>darwin</user_info><user_query>  ship it  </user_query>"
            ),
            ("user".to_string(), "ship it".to_string())
        );
        assert_eq!(
            classify_cursor_text("user", "plain follow-up"),
            ("user".to_string(), "plain follow-up".to_string())
        );
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
    fn durable_prepare_survives_source_growth_restart_and_deletion() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let db_path = dir.path().join("state.db");
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second_line = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"later\"}}\n";
        fs::write(&path, first_line).unwrap();
        let mut conn = open_db(Some(&db_path)).unwrap();

        let first = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        let persisted_first = pending_source_envelope::load_for_epoch(&conn, first.source_epoch)
            .unwrap()
            .unwrap();
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);

        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();
        drop(conn);
        let mut conn = open_db(Some(&db_path)).unwrap();
        let after_growth = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        let persisted_after_growth =
            pending_source_envelope::load_for_epoch(&conn, first.source_epoch)
                .unwrap()
                .unwrap();
        assert_eq!(after_growth.source_epoch, first.source_epoch);
        assert_eq!(after_growth.range_end, first_line.len() as u64);
        assert_eq!(
            after_growth.envelope.expected_envelope_id,
            first.envelope.expected_envelope_id
        );
        assert_eq!(
            persisted_after_growth.request_body_zstd,
            persisted_first.request_body_zstd
        );

        fs::remove_file(&path).unwrap();
        drop(conn);
        let mut conn = open_db(Some(&db_path)).unwrap();
        let after_deletion =
            prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
                .unwrap()
                .unwrap();
        assert_eq!(
            after_deletion.envelope.expected_envelope_id,
            first.envelope.expected_envelope_id
        );
        assert_eq!(
            serde_json::to_vec(&after_deletion.envelope.records).unwrap(),
            serde_json::to_vec(&first.envelope.records).unwrap()
        );
    }

    #[test]
    fn receipt_acknowledgement_updates_cursor_and_deletes_intent_atomically() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();

        let error = pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            prepared.source_epoch,
            &"f".repeat(64),
            prepared.range_start,
            prepared.range_end,
        )
        .unwrap_err();
        assert!(error
            .to_string()
            .contains("does not match the durable pending envelope"));
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 1);

        acknowledge_prepared(&mut conn, &prepared);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_end
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
        pending_source_envelope::acknowledge_and_delete(
            &mut conn,
            prepared.source_epoch,
            &prepared.envelope.expected_envelope_id,
            prepared.range_start,
            prepared.range_end,
        )
        .unwrap();
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            prepared.range_end,
            "a concurrent exact-replay receipt must be an idempotent local success"
        );
    }

    #[test]
    fn unsent_product_gate_can_discard_and_refreeze_after_reply_arrives() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let user = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let assistant = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"hi\"}}\n";
        fs::write(&path, user).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let user_only = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(!user_only.has_reply_evidence);
        assert!(pending_source_envelope::discard_unattempted(
            &conn,
            user_only.source_epoch,
            &user_only.envelope.expected_envelope_id,
        )
        .unwrap());

        fs::write(&path, [user.as_slice(), assistant.as_slice()].concat()).unwrap();
        let with_reply = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(with_reply.has_reply_evidence);
        assert_eq!(with_reply.range_end, (user.len() + assistant.len()) as u64);
    }

    #[test]
    fn live_prepare_refreezes_unattempted_backlog_to_live_budget() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let content = "x".repeat(40 * 1024);
        let first = format!(
            "{{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{{\"content\":{}}}}}\n",
            serde_json::to_string(&content).unwrap()
        );
        let second = format!(
            "{{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{{\"content\":{}}}}}\n",
            serde_json::to_string(&content).unwrap()
        );
        fs::write(&path, format!("{first}{second}")).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let backlog = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert!(backlog.raw_bytes > LIVE_TARGET_BATCH_BYTES as u64);

        let live = prepare_next_envelope_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            "claude",
            None,
            LIVE_TARGET_BATCH_BYTES,
        )
        .unwrap()
        .unwrap();
        assert_ne!(
            live.envelope.expected_envelope_id,
            backlog.envelope.expected_envelope_id
        );
        assert!(live.raw_bytes <= LIVE_TARGET_BATCH_BYTES as u64);
        assert!(live.has_more);
    }

    #[test]
    fn cursor_live_prepare_refreezes_record_backlog_to_live_budget() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..4 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("large-{index}"), vec![b'x'; 40 * 1024]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let backlog = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert!(backlog.raw_bytes > LIVE_TARGET_BATCH_BYTES as u64);

        let live = prepare_next_cursor_envelope_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap()
        .unwrap();
        assert_ne!(
            live.envelope.expected_envelope_id,
            backlog.envelope.expected_envelope_id
        );
        assert!(live.raw_bytes <= LIVE_TARGET_BATCH_BYTES as u64);
        assert!(live.has_more);
    }

    #[tokio::test]
    async fn lost_receipt_retries_identical_body_after_source_growth() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let bodies = Arc::new(Mutex::new(Vec::<Vec<u8>>::new()));
        let server_bodies = bodies.clone();
        let server = tokio::spawn(async move {
            for attempt in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let body = read_http_body(&mut socket).await;
                server_bodies.lock().unwrap().push(body.clone());
                if attempt == 0 {
                    drop(socket);
                    continue;
                }
                let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                let response_body = serde_json::json!({
                    "v": 2,
                    "envelope_id": envelope.expected_envelope_id,
                    "object_hash": "b".repeat(64),
                    "commit_seq": "42",
                    "raw_state": "durable",
                    "render_state": "ready",
                    "media_state": "complete",
                    "missing_media_hashes": [],
                })
                .to_string();
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        let second_line = b"{\"type\":\"user\",\"uuid\":\"u2\",\"timestamp\":\"2026-07-12T12:01:00Z\",\"message\":{\"content\":\"later\"}}\n";
        fs::write(&path, first_line).unwrap();
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
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await;
        assert!(first.is_err());
        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();

        let second = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(
            second.has_more,
            "a successful retry must rescan source growth"
        );
        server.await.unwrap();

        let observed = bodies.lock().unwrap();
        assert_eq!(observed.len(), 2);
        assert_eq!(observed[0], observed[1]);
        let envelope: StorageV2Envelope = serde_json::from_slice(&observed[1]).unwrap();
        assert_eq!(envelope.range_start, 0);
        assert_eq!(envelope.range_end, first_line.len() as u64);
        assert_eq!(
            source_epoch::lane_position(
                &conn,
                Uuid::parse_str(&envelope.source_epoch).unwrap(),
                SourceLane::Durable,
            )
            .unwrap(),
            first_line.len() as u64
        );
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
    }

    #[tokio::test]
    async fn manifest_unavailable_after_conflict_keeps_exact_intent_retryable() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let mut envelope = None;
            for request_index in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (_request_line, body) = read_http_request(&mut socket).await;
                let response_body = if request_index == 0 {
                    envelope = Some(serde_json::from_slice::<StorageV2Envelope>(&body).unwrap());
                    r#"{"detail":{"code":"source_epoch_conflict","message":"range overlap","details":{}}}"#.to_string()
                } else {
                    assert!(envelope.is_some());
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string()
                };
                let status = if request_index == 0 {
                    "409 Conflict"
                } else {
                    "404 Not Found"
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n",
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let error = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("manifest returned 404"));
        server.await.unwrap();

        let pending = pending_source_envelope::load_for_path(
            &conn,
            &stable_source_path(&path).to_string_lossy(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending.attempt_count, 1);
        assert!(pending.blocked_at.is_none());
        let snapshot = pending_source_envelope::snapshot(&conn).unwrap();
        assert_eq!(snapshot.pending_count, 1);
        assert_eq!(snapshot.blocked_source_count, 0);
    }

    #[tokio::test]
    async fn legacy_conflict_proves_hosted_prefix_and_ships_only_the_suffix() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let first_line = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n".to_vec();
        let second_line = b"{\"type\":\"assistant\",\"uuid\":\"a1\",\"timestamp\":\"2026-07-12T12:00:01Z\",\"message\":{\"content\":\"world\"}}\n".to_vec();
        let prefix_end = first_line.len() as u64;
        let server = tokio::spawn(async move {
            let mut original: Option<StorageV2Envelope> = None;
            for request_index in 0..3 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                if request_index == 0 {
                    assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes "));
                    let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                    assert_eq!(envelope.range_start, 0);
                    assert!(envelope.range_end > prefix_end);
                    original = Some(envelope);
                    let response_body = r#"{"detail":{"code":"source_epoch_conflict","message":"range overlap","details":{"reason":"range_overlap"}}}"#;
                    let response = format!(
                        "HTTP/1.1 409 Conflict\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                } else if request_index == 1 {
                    assert!(request_line.starts_with("GET /api/agents/storage/v2/source-epochs/"));
                    let envelope = original.as_ref().unwrap();
                    let prefix_id = envelope_id_for_subrange(envelope, 0, prefix_end).unwrap();
                    let response_body = serde_json::json!({
                        "v": 2,
                        "source_epoch": {
                            "source_epoch": envelope.source_epoch,
                            "tenant_id": envelope.tenant_id,
                            "machine_id": envelope.machine_id,
                            "provider": envelope.provider,
                            "opaque_source_id": envelope.opaque_source_id,
                            "range_kind": envelope.range_kind,
                            "state": "open",
                            "accepted_through": prefix_end.to_string(),
                        },
                        "objects": [{
                            "envelope_id": prefix_id,
                            "tenant_id": envelope.tenant_id,
                            "machine_id": envelope.machine_id,
                            "provider": envelope.provider,
                            "opaque_source_id": envelope.opaque_source_id,
                            "source_epoch": envelope.source_epoch,
                            "range_kind": envelope.range_kind,
                            "range_start": "0",
                            "range_end": prefix_end.to_string(),
                            "retired_at": null,
                        }],
                        "commit_seq": "41",
                        "observed_at": "2026-07-15T00:00:00Z",
                    })
                    .to_string();
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                } else {
                    assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes "));
                    let suffix: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                    assert_eq!(suffix.range_start, prefix_end);
                    assert_eq!(suffix.records.len(), 1);
                    let response_body = serde_json::json!({
                        "v": 2,
                        "envelope_id": suffix.expected_envelope_id,
                        "object_hash": "b".repeat(64),
                        "commit_seq": "42",
                        "raw_state": "durable",
                        "render_state": "ready",
                        "media_state": "complete",
                        "missing_media_hashes": [],
                    })
                    .to_string();
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        response_body.len(),
                        response_body
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                }
            }
        });

        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        fs::write(
            &path,
            [first_line.as_slice(), second_line.as_slice()].concat(),
        )
        .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let reconciled = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert_eq!(reconciled.bytes_shipped, prefix_end);
        assert!(reconciled.has_more);
        let pending = pending_source_envelope::load_for_path(
            &conn,
            &stable_source_path(&path).to_string_lossy(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending.range_start, prefix_end);

        let suffix = ship_next_envelope(
            &mut conn,
            &client,
            &capabilities(),
            &path,
            "claude",
            None,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap()
        .unwrap();
        assert!(!suffix.has_more);
        assert_eq!(pending_source_envelope::count(&conn).unwrap(), 0);
        server.await.unwrap();
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
    fn record_ordinal_reconciliation_rebases_render_ordinals_without_source_read() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("opencode.db");
        create_opencode_db(&db_path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_opencode_envelope(&mut conn, &capabilities(), &db_path)
            .unwrap()
            .unwrap();
        assert_eq!((prepared.range_start, prepared.range_end), (0, 3));
        let prefix_end = 2;
        let prefix_id = envelope_id_for_subrange(&prepared.envelope, 0, prefix_end).unwrap();
        let manifest = StorageV2SourceManifest {
            v: 2,
            source_epoch: crate::shipping::storage_v2::StorageV2SourceEpoch {
                source_epoch: prepared.envelope.source_epoch.clone(),
                tenant_id: prepared.envelope.tenant_id.clone(),
                machine_id: prepared.envelope.machine_id.clone(),
                provider: prepared.envelope.provider.clone(),
                opaque_source_id: prepared.envelope.opaque_source_id.clone(),
                range_kind: prepared.envelope.range_kind.clone(),
                state: "open".to_string(),
                accepted_through: prefix_end.to_string(),
            },
            objects: vec![crate::shipping::storage_v2::StorageV2SourceObject {
                envelope_id: prefix_id,
                tenant_id: prepared.envelope.tenant_id.clone(),
                machine_id: prepared.envelope.machine_id.clone(),
                provider: prepared.envelope.provider.clone(),
                opaque_source_id: prepared.envelope.opaque_source_id.clone(),
                source_epoch: prepared.envelope.source_epoch.clone(),
                range_kind: prepared.envelope.range_kind.clone(),
                range_start: "0".to_string(),
                range_end: prefix_end.to_string(),
                retired_at: None,
            }],
            commit_seq: "7".to_string(),
            observed_at: "2026-07-15T00:00:00Z".to_string(),
        };

        assert_eq!(
            proven_manifest_prefix(&prepared, &manifest).unwrap(),
            Some(prefix_end)
        );
        let suffix = split_prepared_suffix(&prepared, prefix_end).unwrap();
        assert_eq!((suffix.range_start, suffix.range_end), (2, 3));
        assert_eq!(suffix.envelope.records[0].source_position, 2);
        let render = suffix.envelope.render.as_ref().unwrap();
        assert_eq!(render.records[0].source_position, 2);
        assert_eq!(render.records[0].raw_record_ordinal, 0);
        assert_eq!(
            suffix.envelope.expected_envelope_id,
            envelope_id_for_subrange(&suffix.envelope, 2, 3).unwrap()
        );
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
        acknowledge_prepared(&mut conn, &first);

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
            acknowledge_prepared(&mut conn, &prepared);
        }
        assert_eq!(shipped_sources.len(), 65);
    }
}
