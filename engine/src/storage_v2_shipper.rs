//! Parser-independent raw + parser-versioned render shipping for storage-v2.

use std::collections::{HashMap, HashSet};
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
use crate::state::file_identity::{
    cursor_fingerprint, file_identities_match, identity_from_metadata,
};
use crate::state::file_state::FileState;
use crate::state::pending_source_envelope::{self, PendingSourceEnvelope};
use crate::state::source_epoch::{self, SourceChangeHint, SourceEpochResolution, SourceLane};
use crate::storage_v2_contract::{self, EnvelopeIdentity, RangeKind};

pub(crate) const PARSER_REVISION: &str = "engine-parser-v2";
pub(crate) const ORDERING_REVISION: &str = "semantic-order-v2";
const OPENCODE_SESSION_PAGE_SIZE: usize = 64;
const CURSOR_PARSER_REVISION: &str = "cursor-store-render-v5-receipt-lifecycle";
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

pub(crate) enum CursorPreparationOutcome {
    Envelope(PreparedStorageV2Envelope),
    Current,
    WaitingOnClaim,
    Continue,
}

#[derive(Debug)]
pub(crate) enum CursorStorageV2ShipResult {
    Shipped(StorageV2ShipOutcome),
    Current,
    WaitingOnClaim,
    Continue,
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

/// Prepare one durable storage-v2 envelope for a lane and return the exact
/// request body that must be POSTed (and optionally retried) without
/// reserialization.
pub(crate) fn prepare_next_envelope_body_for_lane(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    lane: &str,
) -> Result<Option<(Vec<u8>, PreparedStorageV2Envelope)>> {
    if lane != "live" && lane != "repair" {
        anyhow::bail!("storage-v2 lane must be live or repair");
    }
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
        None,
        maximum_batch_bytes,
    ))?
    else {
        return Ok(None);
    };
    let pending = pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
        .context("prepared storage-v2 envelope is not durable")?;
    validate_pending_matches_prepared(&pending, &prepared)?;
    let body = decode_zstd(&pending.request_body_zstd, "storage-v2 request body")?;
    Ok(Some((body, prepared)))
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
    if pending.blocked_at.is_some()
        && prepared.envelope.provider == "cursor"
        && (reconcile_blocked_cursor_replacement(conn, client, &prepared, request_timeout).await?
            || reconcile_blocked_cursor_lineage(conn, client, &prepared, request_timeout).await?)
    {
        return Ok(StorageV2ShipOutcome {
            bytes_shipped: 0,
            events_shipped: 0,
            has_more: true,
        });
    }
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
                if let Some(outcome) = reconcile_cursor_render_generation_conflict(
                    conn, &pending, &prepared, conflict,
                )? {
                    return Ok(outcome);
                }
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

fn reconcile_cursor_render_generation_conflict(
    conn: &Connection,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    conflict: &crate::shipping::client::StorageV2Conflict,
) -> Result<Option<StorageV2ShipOutcome>> {
    if prepared.envelope.provider != "cursor"
        || conflict
            .details
            .get("reason")
            .and_then(|value| value.as_str())
            != Some("render_generation_revision_conflict")
    {
        return Ok(None);
    }
    let Some(render) = prepared.envelope.render.as_ref() else {
        return Ok(None);
    };
    let Some(existing_generation_id) = conflict
        .details
        .get("existing_generation_id")
        .and_then(|value| value.as_str())
    else {
        return Ok(None);
    };
    let requested_generation_id = conflict
        .details
        .get("requested_generation_id")
        .and_then(|value| value.as_str());
    let parser_revision = conflict
        .details
        .get("parser_revision")
        .and_then(|value| value.as_str());
    let ordering_revision = conflict
        .details
        .get("ordering_revision")
        .and_then(|value| value.as_str());
    if requested_generation_id != Some(render.generation_id.as_str())
        || parser_revision != Some(render.parser_revision.as_str())
        || ordering_revision != Some(render.ordering_revision.as_str())
        || existing_generation_id == render.generation_id
        || Uuid::parse_str(existing_generation_id).is_err()
    {
        return Ok(None);
    }

    let mut replacement = prepared.envelope.clone();
    replacement
        .render
        .as_mut()
        .context("Cursor render-generation recovery lost its render payload")?
        .generation_id = existing_generation_id.to_string();
    let replacement_body = serde_json::to_vec(&replacement)
        .context("serializing reconciled Cursor render generation")?;
    let replacement_body_zstd = encode_zstd(
        &replacement_body,
        "reconciled Cursor storage-v2 request body",
    )?;
    pending_source_envelope::replace_request_body_after_render_conflict(
        conn,
        prepared.source_epoch,
        &pending.envelope_id,
        &pending.request_body_zstd,
        &replacement_body_zstd,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        session_id = prepared.envelope.session_id,
        parser_revision = render.parser_revision,
        old_generation_id = render.generation_id,
        new_generation_id = existing_generation_id,
        "Reconciled Cursor render generation with Runtime Host authority"
    );
    Ok(Some(StorageV2ShipOutcome {
        bytes_shipped: 0,
        events_shipped: 0,
        has_more: true,
    }))
}

async fn reconcile_storage_v2_conflict(
    conn: &mut Connection,
    client: &ShipperClient,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    request_timeout: Duration,
) -> Result<Option<StorageV2ShipOutcome>> {
    let manifest = match client
        .storage_v2_source_manifest(
            &prepared.source_epoch.to_string(),
            prepared.range_start,
            Some(request_timeout),
        )
        .await
    {
        Ok(manifest) => manifest,
        Err(error)
            if error
                .downcast_ref::<crate::shipping::client::StorageV2SourceNotFound>()
                .is_some() =>
        {
            return block_source(
                conn,
                prepared.source_epoch,
                "source_epoch_conflict_unresolved",
                &format!(
                    "Runtime Host rejected the exact envelope and has no manifest for its source epoch: {}",
                    error
                        .downcast_ref::<crate::shipping::client::StorageV2SourceNotFound>()
                        .expect("typed source-not-found checked above")
                        .response_body
                ),
            );
        }
        Err(error) => return Err(error),
    };
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

async fn reconcile_blocked_cursor_lineage(
    conn: &mut Connection,
    client: &ShipperClient,
    prepared: &PreparedStorageV2Envelope,
    request_timeout: Duration,
) -> Result<bool> {
    let Some(pending) = pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
    else {
        return Ok(false);
    };
    if pending.block_kind.as_deref() != Some("source_epoch_conflict_unresolved")
        || !pending
            .block_detail
            .as_deref()
            .is_some_and(|detail| detail.contains("source_epoch_not_found"))
    {
        return Ok(false);
    }
    let requested_predecessor = prepared
        .envelope
        .predecessor_source_epoch
        .as_deref()
        .map(Uuid::parse_str)
        .transpose()?;
    let Some(requested_predecessor) = requested_predecessor else {
        return Ok(false);
    };
    let lineage_proof =
        source_epoch::wire_predecessor_proof_for_epoch(conn, prepared.source_epoch)?;
    if lineage_proof.wire_predecessor == Some(requested_predecessor) {
        return Ok(false);
    }
    if !lineage_proof
        .skipped_empty_epochs
        .contains(&requested_predecessor)
    {
        return Ok(false);
    }

    // An admitted target epoch makes its frozen body immutable forever.
    match client
        .storage_v2_source_manifest(&prepared.source_epoch.to_string(), 0, Some(request_timeout))
        .await
    {
        Err(error)
            if error
                .downcast_ref::<crate::shipping::client::StorageV2SourceNotFound>()
                .is_some() => {}
        Ok(_) => return Ok(false),
        Err(error) => return Err(error),
    }

    // Local proof establishes zero records, receipt-gated durable progress,
    // and pending request for every skipped epoch. Require matching host
    // absence before changing the rejected request's wire predecessor.
    for skipped_epoch in &lineage_proof.skipped_empty_epochs {
        match client
            .storage_v2_source_manifest(&skipped_epoch.to_string(), 0, Some(request_timeout))
            .await
        {
            Err(error)
                if error
                    .downcast_ref::<crate::shipping::client::StorageV2SourceNotFound>()
                    .is_some() => {}
            Ok(_) => return Ok(false),
            Err(error) => return Err(error),
        }
    }

    let (host_state, host_accepted_through, durable_position) =
        if let Some(wire_predecessor) = lineage_proof.wire_predecessor {
            let admitted = client
                .storage_v2_source_manifest(&wire_predecessor.to_string(), 0, Some(request_timeout))
                .await?;
            let durable_position =
                source_epoch::lane_position(conn, wire_predecessor, SourceLane::Durable)?;
            let host_accepted_through = admitted
                .source_epoch
                .accepted_through
                .parse::<u64>()
                .context("Runtime Host Cursor predecessor accepted_through is invalid")?;
            if admitted.v != 2
                || admitted.source_epoch.source_epoch != wire_predecessor.to_string()
                || admitted.source_epoch.tenant_id != prepared.envelope.tenant_id
                || admitted.source_epoch.machine_id != prepared.envelope.machine_id
                || admitted.source_epoch.provider != "cursor"
                || admitted.source_epoch.opaque_source_id != prepared.envelope.opaque_source_id
                || admitted.source_epoch.range_kind != "record_ordinal"
                || admitted.source_epoch.state != "open"
                || admitted.source_epoch.replaced_by_source_epoch.is_some()
                || durable_position == 0
                || host_accepted_through != durable_position
            {
                return Ok(false);
            }
            (
                Some(admitted.source_epoch.state),
                Some(host_accepted_through),
                Some(durable_position),
            )
        } else {
            (None, None, None)
        };

    let mut replacement = prepared.envelope.clone();
    replacement.predecessor_source_epoch = lineage_proof
        .wire_predecessor
        .map(|epoch| epoch.to_string());
    let replacement_body = serde_json::to_vec(&replacement)
        .context("serializing host-proven Cursor lineage repair")?;
    let replacement_body_zstd =
        encode_zstd(&replacement_body, "host-proven Cursor lineage repair body")?;
    let proof_json = serde_json::json!({
        "v": 1,
        "requested_source_epoch": prepared.source_epoch.to_string(),
        "requested_epoch_absent_remotely": true,
        "skipped_empty_epochs": lineage_proof
            .skipped_empty_epochs
            .iter()
            .map(Uuid::to_string)
            .collect::<Vec<_>>(),
        "skipped_epochs_absent_remotely": true,
        "wire_predecessor": lineage_proof.wire_predecessor.map(|epoch| epoch.to_string()),
        "host_state": host_state,
        "host_accepted_through": host_accepted_through.map(|position| position.to_string()),
        "local_durable_position": durable_position.map(|position| position.to_string()),
    })
    .to_string();
    pending_source_envelope::replace_request_body_after_lineage_repair(
        conn,
        pending.source_epoch,
        &pending.envelope_id,
        &pending.request_body_zstd,
        &replacement_body_zstd,
        "Runtime Host proved requested predecessor chain absent and nearest admitted local ancestor valid",
        &proof_json,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        old_predecessor = %requested_predecessor,
        new_predecessor = ?lineage_proof.wire_predecessor,
        ?host_accepted_through,
        "Repaired blocked Cursor lineage from Runtime Host manifest proof"
    );
    Ok(true)
}

async fn reconcile_blocked_cursor_replacement(
    conn: &mut Connection,
    client: &ShipperClient,
    prepared: &PreparedStorageV2Envelope,
    request_timeout: Duration,
) -> Result<bool> {
    let Some(pending) = pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
    else {
        return Ok(false);
    };
    if pending.block_kind.as_deref() != Some("source_epoch_conflict") {
        return Ok(false);
    }
    let closed = client
        .storage_v2_source_manifest(&prepared.source_epoch.to_string(), 0, Some(request_timeout))
        .await?;
    let epoch = &closed.source_epoch;
    let Some(replacement_epoch) = epoch
        .replaced_by_source_epoch
        .as_deref()
        .map(Uuid::parse_str)
        .transpose()?
    else {
        return Ok(false);
    };
    if closed.v != 2
        || epoch.source_epoch != prepared.source_epoch.to_string()
        || epoch.tenant_id != prepared.envelope.tenant_id
        || epoch.machine_id != prepared.envelope.machine_id
        || epoch.provider != "cursor"
        || epoch.opaque_source_id != prepared.envelope.opaque_source_id
        || epoch.range_kind != "record_ordinal"
        || epoch.state != "closed"
    {
        return Ok(false);
    }

    let replacement = client
        .storage_v2_source_manifest(&replacement_epoch.to_string(), 0, Some(request_timeout))
        .await?;
    let replacement_durable =
        source_epoch::lane_position(conn, replacement_epoch, SourceLane::Durable)?;
    let replacement_accepted = replacement
        .source_epoch
        .accepted_through
        .parse::<u64>()
        .context("Runtime Host Cursor replacement accepted_through is invalid")?;
    if replacement.v != 2
        || replacement.source_epoch.source_epoch != replacement_epoch.to_string()
        || replacement.source_epoch.tenant_id != prepared.envelope.tenant_id
        || replacement.source_epoch.machine_id != prepared.envelope.machine_id
        || replacement.source_epoch.provider != "cursor"
        || replacement.source_epoch.opaque_source_id != prepared.envelope.opaque_source_id
        || replacement.source_epoch.range_kind != "record_ordinal"
        || replacement.source_epoch.predecessor_source_epoch.as_deref()
            != Some(epoch.source_epoch.as_str())
        || !matches!(replacement.source_epoch.state.as_str(), "open" | "closed")
        || replacement_durable == 0
        || replacement_accepted != replacement_durable
    {
        return Ok(false);
    }

    let proof_json = serde_json::json!({
        "v": 1,
        "retired_source_epoch": prepared.source_epoch.to_string(),
        "host_state": epoch.state,
        "host_replaced_by_source_epoch": replacement_epoch.to_string(),
        "replacement_host_state": replacement.source_epoch.state,
        "replacement_host_accepted_through": replacement_accepted.to_string(),
        "replacement_local_durable_position": replacement_durable.to_string(),
    })
    .to_string();
    pending_source_envelope::retire_after_host_replacement(
        conn,
        pending.source_epoch,
        &pending.envelope_id,
        &pending.request_body_zstd,
        "Runtime Host proved the blocked Cursor epoch was superseded by a locally durable replacement",
        &proof_json,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        replacement_epoch = %replacement_epoch,
        replacement_accepted,
        "Retired blocked Cursor envelope after Runtime Host replacement proof"
    );
    Ok(true)
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

#[derive(Debug, Default)]
struct CursorTextProjection {
    suppressed: HashSet<(String, usize)>,
}

#[derive(Debug)]
struct CursorStoreTurn {
    prompt: String,
    suppressible_blocks: Vec<(String, usize)>,
    text_blocks: Vec<(String, usize, String)>,
}

fn cursor_message_blocks(message: &Value) -> Vec<Value> {
    match message.get("content") {
        Some(Value::Array(values)) => values.clone(),
        Some(Value::String(text)) => vec![serde_json::json!({"type":"text","text":text})],
        _ => Vec::new(),
    }
}

fn cursor_text_projection(
    snapshot: &cursor_store::CursorStoreSnapshot,
    evidence: Option<&crate::cursor_visibility::CursorVisibilityEvidence>,
) -> CursorTextProjection {
    let Some(evidence) = evidence else {
        return CursorTextProjection::default();
    };
    let cursor_store::RootMessageBlobIds::Parsed(root_ids) = &snapshot.root_message_blob_ids else {
        return CursorTextProjection::default();
    };
    let blobs = snapshot
        .blob_rows
        .iter()
        .map(|row| (row.id.as_str(), row.data_bytes.as_slice()))
        .collect::<HashMap<_, _>>();
    let mut store_turns = Vec::<CursorStoreTurn>::new();
    let mut current_turn: Option<CursorStoreTurn> = None;
    for blob_id in root_ids {
        let Some(bytes) = blobs.get(blob_id.as_str()) else {
            continue;
        };
        let Ok(message) = serde_json::from_slice::<Value>(bytes) else {
            continue;
        };
        let role = message
            .get("role")
            .and_then(Value::as_str)
            .unwrap_or("assistant");
        let blocks = cursor_message_blocks(&message);
        if role == "user" {
            if let Some(turn) = current_turn.take() {
                store_turns.push(turn);
            }
            let prompt = blocks
                .iter()
                .filter_map(|block| block.get("text").and_then(Value::as_str))
                .find_map(|text| {
                    let (effective_role, effective_text) = classify_cursor_text(role, text);
                    (effective_role == "user").then_some(effective_text)
                })
                .unwrap_or_default();
            current_turn = Some(CursorStoreTurn {
                prompt,
                suppressible_blocks: Vec::new(),
                text_blocks: Vec::new(),
            });
            continue;
        }
        let Some(turn) = current_turn.as_mut() else {
            continue;
        };
        for (subordinal, block) in blocks.iter().enumerate() {
            let kind = block.get("type").and_then(Value::as_str);
            if matches!(kind, Some("text" | "reasoning")) {
                turn.suppressible_blocks.push((blob_id.clone(), subordinal));
            }
            if kind == Some("text") && block.get("text").and_then(Value::as_str).is_some() {
                turn.text_blocks.push((
                    blob_id.clone(),
                    subordinal,
                    block
                        .get("text")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string(),
                ));
            }
        }
    }
    if let Some(turn) = current_turn {
        store_turns.push(turn);
    }

    let mut projection = CursorTextProjection::default();
    for store_turn in &store_turns {
        projection.suppressed.extend(
            store_turn
                .suppressible_blocks
                .iter()
                .map(|(blob_id, subordinal)| (blob_id.clone(), *subordinal)),
        );
    }
    if evidence.ambiguous {
        tracing::warn!(
            reason = "conflicting_hook_evidence",
            store_turn_count = store_turns.len(),
            hook_turn_count = evidence.turns.len(),
            "Suppressing managed Cursor assistant text"
        );
        return projection;
    }
    let Some(alignment) = unique_cursor_turn_alignment(&store_turns, &evidence.turns) else {
        tracing::warn!(
            reason = "ambiguous_turn_alignment",
            store_turn_count = store_turns.len(),
            hook_turn_count = evidence.turns.len(),
            "Suppressing managed Cursor assistant text"
        );
        return projection;
    };
    for (store_index, evidence_index) in alignment {
        let store_turn = &store_turns[store_index];
        let hook_turn = &evidence.turns[evidence_index];
        if let Some(response_text) = hook_turn.response_text.as_ref() {
            if let Some(indices) =
                unique_cursor_receipt_path(&store_turn.text_blocks, response_text)
            {
                for index in indices {
                    let (blob_id, subordinal, _) = &store_turn.text_blocks[index];
                    projection
                        .suppressed
                        .remove(&(blob_id.clone(), *subordinal));
                }
            } else {
                tracing::warn!(
                    reason = "ambiguous_receipt_binding",
                    generation_id = hook_turn.generation_id,
                    text_block_count = store_turn.text_blocks.len(),
                    "Suppressing managed Cursor assistant text"
                );
            }
        }
    }
    projection
}

fn unique_cursor_turn_alignment(
    store_turns: &[CursorStoreTurn],
    hook_turns: &[crate::cursor_visibility::CursorHookTurn],
) -> Option<Vec<(usize, usize)>> {
    if hook_turns.is_empty() {
        return Some(Vec::new());
    }
    let mut states = HashMap::<usize, (u8, Vec<usize>)>::new();
    for (store_index, store_turn) in store_turns.iter().enumerate() {
        if store_turn.prompt.trim() == hook_turns[0].prompt.trim() {
            states.insert(store_index, (1, vec![store_index]));
        }
    }
    for hook_turn in hook_turns.iter().skip(1) {
        let mut next = HashMap::<usize, (u8, Vec<usize>)>::new();
        for (previous_index, (path_count, path)) in &states {
            for (store_index, store_turn) in
                store_turns.iter().enumerate().skip(*previous_index + 1)
            {
                if store_turn.prompt.trim() != hook_turn.prompt.trim() {
                    continue;
                }
                let entry = next.entry(store_index).or_insert_with(|| {
                    let mut candidate = path.clone();
                    candidate.push(store_index);
                    (0, candidate)
                });
                entry.0 = entry.0.saturating_add(*path_count).min(2);
            }
        }
        states = next;
    }
    let path_count = states
        .values()
        .fold(0u8, |total, (count, _)| total.saturating_add(*count).min(2));
    if path_count != 1 {
        return None;
    }
    let (_, path) = states.into_values().find(|(count, _)| *count == 1)?;
    Some(path.into_iter().enumerate().map(|(e, s)| (s, e)).collect())
}

fn unique_cursor_receipt_path(
    text_blocks: &[(String, usize, String)],
    response_text: &str,
) -> Option<Vec<usize>> {
    let mut paths = HashMap::<usize, Vec<Vec<usize>>>::from([(0, vec![Vec::new()])]);
    for (index, (_, _, text)) in text_blocks.iter().enumerate() {
        if text.is_empty() {
            continue;
        }
        let previous = paths.clone();
        for (offset, candidates) in previous {
            let Some(suffix) = response_text.get(offset..) else {
                continue;
            };
            if !suffix.starts_with(text) {
                continue;
            }
            let next_offset = offset + text.len();
            let next_paths = paths.entry(next_offset).or_default();
            for candidate in candidates {
                if next_paths.len() >= 2 {
                    break;
                }
                let mut next = candidate;
                next.push(index);
                if !next_paths.contains(&next) {
                    next_paths.push(next);
                }
            }
        }
    }
    let paths = paths.remove(&response_text.len())?;
    (paths.len() == 1).then(|| paths.into_iter().next().expect("one receipt path exists"))
}

fn cursor_render_records(
    snapshot: &cursor_store::CursorStoreSnapshot,
    selected: &[cursor_store_records::CursorRawRecord],
    started_at_us: i64,
    visibility_evidence: Option<&crate::cursor_visibility::CursorVisibilityEvidence>,
) -> Result<Vec<StorageV2RenderRecord>> {
    let cursor_store::RootMessageBlobIds::Parsed(root_ids) = &snapshot.root_message_blob_ids else {
        return Ok(Vec::new());
    };
    let mut selected_blobs: HashMap<String, (u64, usize, Vec<u8>)> = HashMap::new();
    for (raw_record_ordinal, record) in selected.iter().enumerate() {
        let Ok(wrapper) = serde_json::from_slice::<Value>(&record.bytes) else {
            continue;
        };
        if !matches!(
            wrapper.get("kind").and_then(Value::as_str),
            Some("blob" | "root_reference")
        ) {
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
    let projection = cursor_text_projection(snapshot, visibility_evidence);
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
        let blocks = cursor_message_blocks(&message);
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
                        .unwrap_or_default()
                        .to_string();
                    if matches!(kind, "text" | "reasoning") {
                        let key = (blob_id.clone(), subordinal);
                        if projection.suppressed.contains(&key) {
                            continue;
                        }
                    }
                    let (effective_role, effective_text) = if kind == "reasoning" {
                        ("assistant".to_string(), text)
                    } else {
                        classify_cursor_text(role, &text)
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
            let event_id_material = format!("cursor:{blob_id}:{subordinal}");
            records.push(StorageV2RenderRecord {
                event_id: Uuid::new_v5(&Uuid::NAMESPACE_URL, event_id_material.as_bytes())
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

#[cfg(test)]
pub(crate) fn prepare_next_cursor_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    Ok(
        match prepare_next_cursor_envelope_outcome(conn, capabilities, db_path)? {
            CursorPreparationOutcome::Envelope(prepared) => Some(prepared),
            CursorPreparationOutcome::Current
            | CursorPreparationOutcome::WaitingOnClaim
            | CursorPreparationOutcome::Continue => None,
        },
    )
}

pub(crate) fn prepare_next_cursor_envelope_outcome(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<CursorPreparationOutcome> {
    prepare_next_cursor_envelope_outcome_with_limit(
        conn,
        capabilities,
        db_path,
        capabilities.max_raw_record_bytes,
    )
}

#[cfg(test)]
fn prepare_next_cursor_envelope_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    maximum_batch_bytes: u64,
) -> Result<Option<PreparedStorageV2Envelope>> {
    Ok(
        match prepare_next_cursor_envelope_outcome_with_limit(
            conn,
            capabilities,
            db_path,
            maximum_batch_bytes,
        )? {
            CursorPreparationOutcome::Envelope(prepared) => Some(prepared),
            CursorPreparationOutcome::Current
            | CursorPreparationOutcome::WaitingOnClaim
            | CursorPreparationOutcome::Continue => None,
        },
    )
}

fn prepare_next_cursor_envelope_outcome_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    maximum_batch_bytes: u64,
) -> Result<CursorPreparationOutcome> {
    let canonical_path = stable_source_path(db_path);
    let path_text = canonical_path.to_string_lossy();
    if let Some(pending) = pending_source_envelope::load_for_path(conn, &path_text)? {
        let prepared = pending_to_prepared(pending.clone())?;
        if pending.blocked_at.is_some() {
            tracing::debug!(
                source_epoch = %pending.source_epoch,
                "Deferring blocked Cursor envelope to lineage selection"
            );
        } else {
            let oversized_unattempted = pending.raw_bytes > maximum_batch_bytes
                && pending.range_end.saturating_sub(pending.range_start) > 1
                && pending.attempt_count == 0
                && maximum_batch_bytes < capabilities.max_raw_record_bytes;
            let obsolete_unattempted_render = pending.attempt_count == 0
                && prepared
                    .envelope
                    .render
                    .as_ref()
                    .is_none_or(|render| render.parser_revision != CURSOR_PARSER_REVISION);
            if !(oversized_unattempted || obsolete_unattempted_render)
                || !pending_source_envelope::discard_unattempted(
                    conn,
                    pending.source_epoch,
                    &pending.envelope_id,
                )?
            {
                return Ok(CursorPreparationOutcome::Envelope(prepared));
            }
        }
    }
    let metadata_before = db_path
        .metadata()
        .with_context(|| format!("reading Cursor store metadata {}", db_path.display()))?;
    let store_incarnation = identity_from_metadata(&metadata_before)
        .context("Cursor store has no stable file incarnation")?;
    let mut store_snapshot = cursor_store::read_cursor_render_snapshot(db_path)?;
    let identity_after_render = identity_from_metadata(
        &db_path
            .metadata()
            .with_context(|| format!("rechecking Cursor store metadata {}", db_path.display()))?,
    );
    if !file_identities_match(
        Some(store_incarnation.as_str()),
        identity_after_render.as_deref(),
    ) {
        anyhow::bail!("Cursor store file changed identity during root capture");
    }
    let snapshot =
        cursor_store::cursor_store_raw_snapshot_from(&store_snapshot, store_incarnation.clone())?;
    let claimed_binding = match crate::cursor_launch_binding::launch_binding_state_for_conversation(
        &snapshot.conversation_uuid,
    )? {
        crate::cursor_launch_binding::CursorLaunchBindingState::Managed(binding) => Some(binding),
        crate::cursor_launch_binding::CursorLaunchBindingState::Pending => {
            return Ok(CursorPreparationOutcome::WaitingOnClaim);
        }
        crate::cursor_launch_binding::CursorLaunchBindingState::Unclaimed => None,
    };
    let claimed_session_id = claimed_binding
        .as_ref()
        .map(|binding| binding.session_id.clone());
    let visibility_evidence = claimed_binding
        .as_ref()
        .map(|binding| {
            crate::cursor_visibility::load_cursor_visibility_evidence(
                &binding.session_id,
                &snapshot.conversation_uuid,
            )
            .map(|evidence| {
                if evidence.is_none() {
                    tracing::warn!(
                        session_id = binding.session_id,
                        conversation_id = snapshot.conversation_uuid,
                        reason = "missing_hook_evidence",
                        "Suppressing managed Cursor assistant text"
                    );
                }
                evidence.unwrap_or_default()
            })
        })
        .transpose()?;
    // Do not consume raw records while the provider's turn receipt is still
    // racing the stop hook.  Both terminal hooks wake the shipper, so the
    // settled turn will be captured without permanently losing its render.
    if let Some(wait) = visibility_evidence
        .as_ref()
        .and_then(|evidence| evidence.unsettled_reason())
    {
        tracing::warn!(
            session_id = claimed_session_id.as_deref().unwrap_or_default(),
            conversation_id = snapshot.conversation_uuid,
            reason = wait.as_str(),
            "Waiting for managed Cursor visibility evidence"
        );
        return Ok(CursorPreparationOutcome::Current);
    }
    if visibility_evidence.is_some() {
        if let cursor_store::RootMessageBlobIds::Parsed(root_ids) =
            &store_snapshot.root_message_blob_ids
        {
            for ids in root_ids.chunks(256) {
                store_snapshot
                    .blob_rows
                    .extend(cursor_store::read_cursor_blob_rows(db_path, ids)?);
            }
        }
    }
    let opaque_source_id = cursor_store::cursor_opaque_source_id(&snapshot.conversation_uuid);
    let previous_root_ids =
        cursor_store_root::previous_message_blob_ids(conn, &snapshot.conversation_uuid)?;
    let root_relation = match snapshot.root_blob_id.as_deref() {
        Some(_) => cursor_store_root::classify_cursor_root(
            conn,
            &snapshot.conversation_uuid,
            &snapshot.root_message_blob_ids,
        )?,
        None => cursor_store_root::CursorRootOrderRelation::Inconclusive,
    };
    let incarnation = snapshot.store_incarnation.clone();
    let existing_len =
        cursor_store_records::active_cursor_record_count(conn, "cursor", &opaque_source_id)?;
    let active_incarnation =
        source_epoch::active_source_incarnation(conn, "cursor", &opaque_source_id)?;
    let active_render_revision =
        source_epoch::active_source_revision(conn, "cursor", &opaque_source_id)?;
    let parser_replay_required =
        existing_len > 0 && active_render_revision.as_deref() != Some(CURSOR_PARSER_REVISION);
    let source_len_before_capture = if parser_replay_required
        || root_relation == cursor_store_root::CursorRootOrderRelation::Rewrite
        || !file_identities_match(active_incarnation.as_deref(), Some(incarnation.as_str()))
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
        Some(CURSOR_PARSER_REVISION),
        claimed_session_id.as_deref(),
        if parser_replay_required {
            SourceChangeHint::Rewrite
        } else {
            root_relation.source_change_hint()
        },
    )?;
    let newly_referenced_ids = match (&previous_root_ids, &snapshot.root_message_blob_ids) {
        (Some(previous), cursor_store::RootMessageBlobIds::Parsed(current))
            if current.starts_with(previous) =>
        {
            current[previous.len()..].to_vec()
        }
        // Initial capture streams every blob through the bounded page walker;
        // explicit reference records are only needed for already-spooled
        // orphan blobs that a later root extension makes visible.
        (None, cursor_store::RootMessageBlobIds::Parsed(_)) => Vec::new(),
        _ => Vec::new(),
    };
    store_snapshot
        .blob_rows
        .extend(cursor_store::read_cursor_blob_rows(
            db_path,
            &newly_referenced_ids,
        )?);
    let mut capture_records = snapshot.records.clone();
    capture_records.extend(cursor_store::root_reference_records(
        &store_snapshot,
        &snapshot.store_incarnation,
        &newly_referenced_ids,
    )?);
    cursor_store_records::append_unseen_cursor_records(
        conn,
        resolution.source_epoch,
        &capture_records,
    )?;
    // Commit root ordering only after every reference needed to render this
    // transition is durable. A crash before here safely replays and dedupes.
    if let Some(root_blob_id) = snapshot.root_blob_id.as_deref() {
        cursor_store_root::record_cursor_root(
            conn,
            &snapshot.conversation_uuid,
            root_blob_id,
            &snapshot.root_message_blob_ids,
        )?;
    }
    let mut streamed_records = Vec::new();
    let mut streamed_bytes = 0usize;
    let capture_cursor = cursor_store_records::capture_cursor(conn, resolution.source_epoch)?;
    let blob_visit = cursor_store::visit_cursor_blob_records(
        db_path,
        &snapshot.conversation_uuid,
        &snapshot.store_incarnation,
        capture_cursor.as_deref(),
        256,
        |record| {
            let record_hash = cursor_store_records::cursor_record_hash(&record);
            if cursor_store_records::cursor_record_exists(
                conn,
                resolution.source_epoch,
                &record_hash,
            )? {
                return Ok(true);
            }
            if record.len() as u64 > capabilities.max_raw_record_bytes {
                anyhow::bail!(
                    "one Cursor raw record exceeds the negotiated storage-v2 object bound"
                );
            }
            if !streamed_records.is_empty()
                && streamed_bytes.saturating_add(record.len()) > MAX_RAW_BATCH_BYTES
            {
                return Ok(false);
            }
            streamed_bytes = streamed_bytes.saturating_add(record.len());
            streamed_records.push(record);
            Ok(true)
        },
    )?;
    let identity_after_blobs = identity_from_metadata(
        &db_path
            .metadata()
            .with_context(|| format!("rechecking Cursor store metadata {}", db_path.display()))?,
    );
    if !file_identities_match(
        Some(store_incarnation.as_str()),
        identity_after_blobs.as_deref(),
    ) {
        anyhow::bail!("Cursor store file changed identity during blob capture");
    }
    cursor_store_records::append_unseen_cursor_records(
        conn,
        resolution.source_epoch,
        &streamed_records,
    )?;
    // The last visited ID is a durable high-water mark, including at the
    // current head. Clearing it at the head makes the next reconciliation
    // indistinguishable from an initial capture and rescans every blob.
    cursor_store_records::store_capture_cursor(
        conn,
        resolution.source_epoch,
        blob_visit.last_blob_id.as_deref(),
    )?;
    let source_capture_has_more = blob_visit.has_more;
    let captured_logical_len =
        cursor_store_records::cursor_record_count(conn, resolution.source_epoch)?;
    // Refresh max_observed_len after adding local records.  The renderer
    // revision is stable across ordinary root appends and deliberately rotates
    // the epoch when replay is required by a parser upgrade.
    let resolution = source_epoch::observe_source(
        conn,
        "cursor",
        &opaque_source_id,
        &incarnation,
        captured_logical_len,
        SourceLane::Durable,
        source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?,
        Some(CURSOR_PARSER_REVISION),
        None,
        SourceChangeHint::None,
    )?;
    let active_source_epoch = resolution.source_epoch;
    let target_source_epoch =
        cursor_store_records::oldest_undrained_epoch(conn, "cursor", &opaque_source_id)?
            .unwrap_or(active_source_epoch);
    let resolution = source_epoch::resolution_for_epoch(conn, target_source_epoch)?;
    let logical_len = cursor_store_records::cursor_record_count(conn, target_source_epoch)?;
    let wire_predecessor = source_epoch::wire_predecessor_for_epoch(conn, target_source_epoch)?;

    if let Some(blocked) = pending_source_envelope::load_for_epoch(conn, target_source_epoch)? {
        if blocked.blocked_at.is_some() {
            return pending_to_prepared(blocked).map(CursorPreparationOutcome::Envelope);
        }
    }
    let range_start =
        source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    if range_start >= logical_len {
        return Ok(
            if source_capture_has_more && target_source_epoch == active_source_epoch {
                CursorPreparationOutcome::Continue
            } else {
                CursorPreparationOutcome::Current
            },
        );
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
        return Ok(CursorPreparationOutcome::Continue);
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
    let mut render_records = cursor_render_records(
        &store_snapshot,
        &selected,
        started_at.timestamp_micros(),
        visibility_evidence.as_ref(),
    )?;
    if let Some(thread_id) = claimed_binding
        .as_ref()
        .and_then(|binding| binding.thread_id.as_ref())
    {
        for record in &mut render_records {
            record.thread_id = Some(thread_id.clone());
        }
    }
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
            predecessor_source_epoch: wire_predecessor.map(|value| value.to_string()),
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
        has_more: range_end < logical_len
            || source_capture_has_more
            || target_source_epoch != active_source_epoch,
        media_objects: Vec::new(),
    };
    persist_prepared(conn, &path_text, prepared).map(CursorPreparationOutcome::Envelope)
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
    if file_identities_match(stored_identity.as_deref(), current_identity.as_deref())
        && stored_fingerprint == current_fingerprint
        && stored_fingerprint.is_some()
    {
        file_state.record_continuous_file_identity(path_text, current_identity.as_deref())?;
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
) -> Result<CursorStorageV2ShipResult> {
    let maximum_batch_bytes = if lane == "live" {
        LIVE_TARGET_BATCH_BYTES as u64
    } else {
        capabilities.max_raw_record_bytes
    };
    let prepared = preparation_result(prepare_next_cursor_envelope_outcome_with_limit(
        conn,
        capabilities,
        db_path,
        maximum_batch_bytes,
    ))?;
    match prepared {
        CursorPreparationOutcome::Envelope(prepared) => {
            let source_is_blocked =
                pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
                    .is_some_and(|pending| pending.blocked_at.is_some());
            if source_is_blocked {
                return if reconcile_blocked_cursor_replacement(
                    conn,
                    client,
                    &prepared,
                    request_timeout,
                )
                .await?
                    || reconcile_blocked_cursor_lineage(conn, client, &prepared, request_timeout)
                        .await?
                {
                    Ok(CursorStorageV2ShipResult::Continue)
                } else {
                    Ok(CursorStorageV2ShipResult::Current)
                };
            }
            ship_prepared_envelope(conn, client, capabilities, prepared, lane, request_timeout)
                .await
                .map(CursorStorageV2ShipResult::Shipped)
        }
        CursorPreparationOutcome::Current => Ok(CursorStorageV2ShipResult::Current),
        CursorPreparationOutcome::WaitingOnClaim => Ok(CursorStorageV2ShipResult::WaitingOnClaim),
        CursorPreparationOutcome::Continue => Ok(CursorStorageV2ShipResult::Continue),
    }
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
    fn cursor_renders_blob_first_referenced_after_its_raw_receipt() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        let orphan_id = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    orphan_id,
                    br#"{"role":"assistant","content":[{"type":"text","text":"referenced later"}]}"#
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        acknowledge_prepared(&mut conn, &first);

        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0xee; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        let extension = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let rendered = extension.envelope.render.unwrap().records;
        assert!(rendered
            .iter()
            .any(|record| record.content_text.as_deref() == Some("referenced later")));
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

    fn cursor_visibility_fixture(
        messages: Vec<(&str, Value)>,
    ) -> (
        cursor_store::CursorStoreSnapshot,
        Vec<cursor_store_records::CursorRawRecord>,
    ) {
        let mut blob_rows = Vec::new();
        let mut selected = Vec::new();
        let mut root_ids = Vec::new();
        for (source_position, (blob_id, message)) in messages.into_iter().enumerate() {
            let bytes = serde_json::to_vec(&message).unwrap();
            root_ids.push(blob_id.to_string());
            blob_rows.push(cursor_store::CursorStoreBlobRow {
                id: blob_id.to_string(),
                data_bytes: bytes.clone(),
                data_storage_class: cursor_store::SqliteStorageClass::Blob,
            });
            selected.push(cursor_store_records::CursorRawRecord {
                source_position: source_position as u64,
                bytes: serde_json::to_vec(&serde_json::json!({
                    "kind": "blob",
                    "blob_id": blob_id,
                    "blob_bytes_b64": BASE64_STANDARD.encode(bytes),
                }))
                .unwrap(),
            });
        }
        (
            cursor_store::CursorStoreSnapshot {
                conversation_uuid: CURSOR_CONVERSATION_ID.to_string(),
                root_blob_id: None,
                created_at_ms: Some(1_773_403_200_000),
                meta_rows: Vec::new(),
                blob_rows,
                root_message_blob_ids: cursor_store::RootMessageBlobIds::Parsed(root_ids),
            },
            selected,
        )
    }

    #[test]
    fn cursor_failed_retry_artifacts_remain_raw_but_do_not_render_as_replies() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let attempt_one = "2222222222222222222222222222222222222222222222222222222222222222";
        let attempt_two = "3333333333333333333333333333333333333333333333333333333333333333";
        let attempt_three = "4444444444444444444444444444444444444444444444444444444444444444";
        let attempt_four = "5555555555555555555555555555555555555555555555555555555555555555";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>hello test 1</user_query>"}]}),
            ),
            (
                attempt_one,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought one"},{"type":"text","text":"reply one"}]}),
            ),
            (
                attempt_two,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought two"},{"type":"text","text":"reply two"}]}),
            ),
            (
                attempt_three,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought three"},{"type":"text","text":"reply three"}]}),
            ),
            (
                attempt_four,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"retry thought four"},{"type":"text","text":"reply four"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorHookTurn {
                generation_id: "generation-1".to_string(),
                prompt: "hello test 1".to_string(),
                response_text: None,
                stop_status: Some("error".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[0].content_text.as_deref(), Some("hello test 1"));
        assert_eq!(
            selected.len(),
            5,
            "all retry artifacts remain in the raw envelope"
        );
    }

    #[test]
    fn cursor_completed_turn_renders_exact_provider_receipt_once() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let progress = "2222222222222222222222222222222222222222222222222222222222222222";
        let tool = "3333333333333333333333333333333333333333333333333333333333333333";
        let final_text = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>do work</user_query>"}]}),
            ),
            (
                progress,
                serde_json::json!({"role":"assistant","content":[{"type":"reasoning","text":"internal progress"},{"type":"text","text":"progress"}]}),
            ),
            (
                tool,
                serde_json::json!({"role":"assistant","content":[{"type":"tool-call","toolName":"Shell","toolCallId":"call-1","args":{"command":"pwd"}}]}),
            ),
            (
                final_text,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"done"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorHookTurn {
                generation_id: "generation-2".to_string(),
                prompt: "do work".to_string(),
                response_text: Some("progressdone".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 4);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[1].role, "assistant");
        assert_eq!(rendered[1].content_text.as_deref(), Some("progress"));
        assert_eq!(rendered[2].tool_call_id.as_deref(), Some("call-1"));
        assert_eq!(rendered[3].content_text.as_deref(), Some("done"));
    }

    #[test]
    fn cursor_ambiguous_retry_receipt_does_not_choose_first_or_last_artifact() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let attempt_one = "2222222222222222222222222222222222222222222222222222222222222222";
        let attempt_two = "3333333333333333333333333333333333333333333333333333333333333333";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>repeat</user_query>"}]}),
            ),
            (
                attempt_one,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same answer"}]}),
            ),
            (
                attempt_two,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same answer"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorHookTurn {
                generation_id: "generation-ambiguous".to_string(),
                prompt: "repeat".to_string(),
                response_text: Some("same answer".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
    }

    #[test]
    fn cursor_conflicting_hook_receipts_fail_closed() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let reply = "2222222222222222222222222222222222222222222222222222222222222222";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>hello</user_query>"}]}),
            ),
            (
                reply,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"world"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorHookTurn {
                generation_id: "generation-conflict".to_string(),
                prompt: "hello".to_string(),
                response_text: Some("world".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ambiguous: true,
            ..Default::default()
        };

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
    }

    #[test]
    fn cursor_failed_turn_keeps_executed_tool_evidence_but_suppresses_prose() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let tool = "2222222222222222222222222222222222222222222222222222222222222222";
        let prose = "3333333333333333333333333333333333333333333333333333333333333333";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>run it</user_query>"}]}),
            ),
            (
                tool,
                serde_json::json!({"role":"assistant","content":[{"type":"tool-call","toolName":"Shell","toolCallId":"call-1","args":{"command":"pwd"}}]}),
            ),
            (
                prose,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"uncommitted prose"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorHookTurn {
                generation_id: "generation-failed-tool".to_string(),
                prompt: "run it".to_string(),
                response_text: None,
                stop_status: Some("error".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 2);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(rendered[1].tool_call_id.as_deref(), Some("call-1"));
    }

    #[test]
    fn cursor_managed_turn_without_matching_hook_evidence_is_raw_only() {
        let user = "1111111111111111111111111111111111111111111111111111111111111111";
        let attempt = "2222222222222222222222222222222222222222222222222222222222222222";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>missing hook</user_query>"}]}),
            ),
            (
                attempt,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"unverified artifact"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence::default();

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 1);
        assert_eq!(rendered[0].role, "user");
        assert_eq!(selected.len(), 2, "unverified text remains in raw storage");
    }

    #[test]
    fn cursor_repeated_prompt_without_unique_turn_alignment_is_raw_only() {
        let user_one = "1111111111111111111111111111111111111111111111111111111111111111";
        let reply_one = "2222222222222222222222222222222222222222222222222222222222222222";
        let user_two = "3333333333333333333333333333333333333333333333333333333333333333";
        let reply_two = "4444444444444444444444444444444444444444444444444444444444444444";
        let (snapshot, selected) = cursor_visibility_fixture(vec![
            (
                user_one,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>same prompt</user_query>"}]}),
            ),
            (
                reply_one,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same reply"}]}),
            ),
            (
                user_two,
                serde_json::json!({"role":"user","content":[{"type":"text","text":"<user_query>same prompt</user_query>"}]}),
            ),
            (
                reply_two,
                serde_json::json!({"role":"assistant","content":[{"type":"text","text":"same reply"}]}),
            ),
        ]);
        let evidence = crate::cursor_visibility::CursorVisibilityEvidence {
            turns: vec![crate::cursor_visibility::CursorHookTurn {
                generation_id: "generation-repeated".to_string(),
                prompt: "same prompt".to_string(),
                response_text: Some("same reply".to_string()),
                stop_status: Some("completed".to_string()),
                stop_observed_at: None,
            }],
            ..Default::default()
        };

        let rendered = cursor_render_records(&snapshot, &selected, 0, Some(&evidence)).unwrap();
        assert_eq!(rendered.len(), 2);
        assert!(rendered.iter().all(|record| record.role == "user"));
    }

    #[test]
    fn cursor_parser_revision_upgrade_replays_from_a_replacement_epoch() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![
                    br#"{"role":"assistant","content":[{"type":"text","text":"legacy render"}]}"#,
                    CURSOR_MESSAGE_A,
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let first_epoch = first.source_epoch;
        acknowledge_prepared(&mut conn, &first);
        conn.execute(
            "UPDATE source_epoch_registry
             SET source_revision = NULL, max_observed_len = 132
             WHERE source_epoch = ?1",
            [first_epoch.to_string()],
        )
        .unwrap();

        let replay = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_ne!(replay.source_epoch, first_epoch);
        assert_eq!(
            replay.envelope.predecessor_source_epoch,
            Some(first_epoch.to_string())
        );
        assert_eq!(replay.range_start, 0);
        assert_eq!(
            replay.envelope.render.as_ref().unwrap().parser_revision,
            CURSOR_PARSER_REVISION
        );
        let epoch_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM source_epoch_registry", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(
            epoch_count, 2,
            "parser replay must not look like a truncation"
        );
    }

    #[tokio::test]
    async fn cursor_lineage_repair_requires_host_proof_and_collapses_empty_epochs() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let _store = make_cursor_store(&path);
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let first_epoch = first.source_epoch;
        acknowledge_prepared(&mut conn, &first);
        let durable_position =
            source_epoch::lane_position(&conn, first_epoch, SourceLane::Durable).unwrap();
        let source_id = first.envelope.opaque_source_id.clone();
        let incarnation = source_epoch::active_source_incarnation(&conn, "cursor", &source_id)
            .unwrap()
            .unwrap();
        let mut predecessor = first_epoch;
        for revision in ["fixture-empty-1", "fixture-empty-2", CURSOR_PARSER_REVISION] {
            let next = source_epoch::observe_source(
                &mut conn,
                "cursor",
                &source_id,
                &incarnation,
                0,
                SourceLane::Durable,
                0,
                Some(revision),
                None,
                SourceChangeHint::None,
            )
            .unwrap();
            assert_eq!(next.predecessor_epoch, Some(predecessor));
            predecessor = next.source_epoch;
        }
        cursor_store_records::append_unseen_cursor_records(
            &mut conn,
            predecessor,
            &[b"descendant-record".to_vec()],
        )
        .unwrap();

        let fresh = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_eq!(fresh.source_epoch, predecessor);
        assert_eq!(
            fresh.envelope.predecessor_source_epoch,
            Some(first_epoch.to_string())
        );
        let pending = pending_source_envelope::load_for_epoch(&conn, predecessor)
            .unwrap()
            .unwrap();
        let mut poisoned = fresh.envelope.clone();
        poisoned.predecessor_source_epoch = Some(
            source_epoch::resolution_for_epoch(&conn, predecessor)
                .unwrap()
                .predecessor_epoch
                .unwrap()
                .to_string(),
        );
        let poisoned_zstd = encode_zstd(
            &serde_json::to_vec(&poisoned).unwrap(),
            "poisoned fixture body",
        )
        .unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            predecessor,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &poisoned_zstd,
        )
        .unwrap();
        pending_source_envelope::quarantine(
            &mut conn,
            predecessor,
            "source_epoch_conflict_unresolved",
            "source_epoch_not_found: fixture predecessor absent",
        )
        .unwrap();
        let poisoned_prepared = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, predecessor)
                .unwrap()
                .unwrap(),
        )
        .unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let host_manifest = serde_json::json!({
            "v": 2,
            "source_epoch": {
                "source_epoch": first_epoch.to_string(),
                "tenant_id": fresh.envelope.tenant_id,
                "machine_id": fresh.envelope.machine_id,
                "provider": "cursor",
                "opaque_source_id": source_id,
                "range_kind": "record_ordinal",
                "state": "open",
                "predecessor_source_epoch": null,
                "replaced_by_source_epoch": null,
                "accepted_through": durable_position.to_string()
            },
            "objects": [],
            "commit_seq": "42",
            "observed_at": "2026-07-22T12:00:00Z"
        })
        .to_string();
        let server = tokio::spawn(async move {
            for (status, body) in [
                ("200 OK", host_manifest.clone()),
                (
                    "404 Not Found",
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string(),
                ),
                (
                    "404 Not Found",
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string(),
                ),
                (
                    "404 Not Found",
                    r#"{"detail":{"code":"source_epoch_not_found","message":"missing","details":{}}}"#.to_string(),
                ),
                ("200 OK", host_manifest),
            ] {
                let (mut socket, _) = listener.accept().await.unwrap();
                let _ = read_http_request(&mut socket).await;
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(), body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let client = ShipperClient::with_compression(
            &ShipperConfig {
                api_url: format!("http://{address}"),
                timeout_seconds: 5,
                ..ShipperConfig::default()
            },
            CompressionAlgo::Gzip,
        )
        .unwrap();

        assert!(!reconcile_blocked_cursor_lineage(
            &mut conn,
            &client,
            &poisoned_prepared,
            Duration::from_secs(5),
        )
        .await
        .unwrap());
        assert!(
            pending_source_envelope::load_for_epoch(&conn, predecessor)
                .unwrap()
                .unwrap()
                .blocked_at
                .is_some(),
            "a hosted manifest for the requested epoch must keep the body quarantined"
        );
        assert!(reconcile_blocked_cursor_lineage(
            &mut conn,
            &client,
            &poisoned_prepared,
            Duration::from_secs(5),
        )
        .await
        .unwrap());
        server.await.unwrap();
        let repaired = pending_source_envelope::load_for_epoch(&conn, predecessor)
            .unwrap()
            .unwrap();
        assert!(repaired.blocked_at.is_none());
        assert_eq!(
            pending_to_prepared(repaired)
                .unwrap()
                .envelope
                .predecessor_source_epoch,
            Some(first_epoch.to_string())
        );
    }

    #[test]
    fn cursor_unattempted_obsolete_pending_render_is_rebuilt_before_shipping() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![
                    br#"{"role":"assistant","content":[{"type":"text","text":"legacy render"}]}"#,
                    CURSOR_MESSAGE_A,
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let first = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let pending = pending_source_envelope::load_for_epoch(&conn, first.source_epoch)
            .unwrap()
            .unwrap();
        let mut obsolete = first.envelope.clone();
        obsolete.render.as_mut().unwrap().parser_revision =
            "cursor-store-render-v3-receipts".to_string();
        let obsolete_body = serde_json::to_vec(&obsolete).unwrap();
        let obsolete_body_zstd = encode_zstd(&obsolete_body, "obsolete Cursor request").unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            first.source_epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &obsolete_body_zstd,
        )
        .unwrap();
        conn.execute(
            "UPDATE source_epoch_registry SET source_revision = NULL WHERE source_epoch = ?1",
            [first.source_epoch.to_string()],
        )
        .unwrap();

        let rebuilt = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        assert_ne!(rebuilt.source_epoch, first.source_epoch);
        assert_eq!(
            rebuilt.envelope.render.as_ref().unwrap().parser_revision,
            CURSOR_PARSER_REVISION
        );
    }

    #[tokio::test]
    async fn cursor_generation_conflict_adopts_hosted_revision_without_changing_raw_identity() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        store
            .execute(
                "UPDATE blobs SET data = ?1 WHERE id = ?2",
                params![
                    br#"{"role":"assistant","content":[{"type":"text","text":"hello"}]}"#,
                    CURSOR_MESSAGE_A,
                ],
            )
            .unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let stable_generation_id = prepared
            .envelope
            .render
            .as_ref()
            .unwrap()
            .generation_id
            .clone();
        let obsolete_generation_id = Uuid::new_v4().to_string();
        let mut obsolete_envelope = prepared.envelope.clone();
        obsolete_envelope.render.as_mut().unwrap().generation_id = obsolete_generation_id.clone();
        let pending = pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
            .unwrap()
            .unwrap();
        let obsolete_body = serde_json::to_vec(&obsolete_envelope).unwrap();
        let obsolete_body_zstd = encode_zstd(&obsolete_body, "obsolete Cursor request").unwrap();
        pending_source_envelope::replace_request_body_after_render_conflict(
            &conn,
            prepared.source_epoch,
            &pending.envelope_id,
            &pending.request_body_zstd,
            &obsolete_body_zstd,
        )
        .unwrap();
        let obsolete_prepared = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .unwrap(),
        )
        .unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let expected_envelope_id = prepared.envelope.expected_envelope_id.clone();
        let hosted_generation_id = stable_generation_id.clone();
        let requested_generation_id = obsolete_generation_id.clone();
        let server = tokio::spawn(async move {
            for request_index in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let (request_line, body) = read_http_request(&mut socket).await;
                assert!(request_line.starts_with("POST /api/agents/storage/v2/envelopes "));
                let envelope: StorageV2Envelope = serde_json::from_slice(&body).unwrap();
                assert_eq!(envelope.expected_envelope_id, expected_envelope_id);
                let render = envelope.render.as_ref().unwrap();
                let response_body = if request_index == 0 {
                    assert_eq!(render.generation_id, requested_generation_id);
                    serde_json::json!({
                        "detail": {
                            "code": "source_epoch_conflict",
                            "message": "render generation drift",
                            "details": {
                                "reason": "render_generation_revision_conflict",
                                "existing_generation_id": hosted_generation_id,
                                "requested_generation_id": requested_generation_id,
                                "parser_revision": render.parser_revision,
                                "ordering_revision": render.ordering_revision,
                            }
                        }
                    })
                    .to_string()
                } else {
                    assert_eq!(render.generation_id, hosted_generation_id);
                    serde_json::json!({
                        "v": 2,
                        "envelope_id": envelope.expected_envelope_id,
                        "object_hash": "b".repeat(64),
                        "commit_seq": "42",
                        "raw_state": "durable",
                        "render_state": "ready",
                        "media_state": "complete",
                        "missing_media_hashes": [],
                    })
                    .to_string()
                };
                let status = if request_index == 0 {
                    "409 Conflict"
                } else {
                    "200 OK"
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    response_body.len(),
                    response_body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let config = ShipperConfig {
            api_url: format!("http://{address}"),
            timeout_seconds: 5,
            ..ShipperConfig::default()
        };
        let client = ShipperClient::with_compression(&config, CompressionAlgo::Gzip).unwrap();

        let reconciled = ship_prepared_envelope(
            &mut conn,
            &client,
            &capabilities(),
            obsolete_prepared,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap();
        assert_eq!(reconciled.bytes_shipped, 0);
        assert!(reconciled.has_more);
        let repaired = pending_to_prepared(
            pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            repaired.envelope.render.as_ref().unwrap().generation_id,
            stable_generation_id
        );
        let shipped = ship_prepared_envelope(
            &mut conn,
            &client,
            &capabilities(),
            repaired,
            "live",
            Duration::from_secs(5),
        )
        .await
        .unwrap();
        assert!(shipped.bytes_shipped > 0);
        assert!(
            pending_source_envelope::load_for_epoch(&conn, prepared.source_epoch)
                .unwrap()
                .is_none()
        );
        server.await.unwrap();
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

    #[test]
    fn cursor_preparation_reports_current_only_after_acknowledged_head() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..300 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("page-{index:03}"), vec![index as u8]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let mut shipped = 0;
        let mut source_epoch = None;
        let mut reached_current = false;

        for _ in 0..16 {
            match prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap()
            {
                CursorPreparationOutcome::Envelope(prepared) => {
                    source_epoch = Some(prepared.source_epoch);
                    acknowledge_prepared(&mut conn, &prepared);
                    shipped += 1;
                }
                CursorPreparationOutcome::Continue => continue,
                CursorPreparationOutcome::Current => {
                    assert!(shipped > 0);
                    reached_current = true;
                    break;
                }
                CursorPreparationOutcome::WaitingOnClaim => {
                    panic!("fixture must not be held by a managed Cursor claim")
                }
            }
        }
        assert!(reached_current, "Cursor source did not reach current");
        let source_epoch = source_epoch.expect("fixture must prepare at least one envelope");
        assert_eq!(
            cursor_store_records::capture_cursor(&conn, source_epoch)
                .unwrap()
                .as_deref(),
            Some("page-299")
        );
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));

        let lower_message_id = "1111111111111111111111111111111111111111111111111111111111111111";
        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                params![
                    lower_message_id,
                    br#"{"role":"assistant","content":[{"type":"text","text":"lower hash extension"}]}"#
                ],
            )
            .unwrap();
        let mut extended_root = vec![0xbb; 32];
        extended_root.extend_from_slice(&[0x11; 32]);
        set_cursor_root(&store, CURSOR_ROOT_B, &extended_root);
        let lower_extension = match prepare_next_cursor_envelope_outcome_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap()
        {
            CursorPreparationOutcome::Envelope(prepared) => prepared,
            _ => panic!("a lower-ID blob referenced by a root extension must be captured"),
        };
        assert_eq!(lower_extension.source_epoch, source_epoch);
        assert!(lower_extension.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "root_reference" && raw["blob_id"] == lower_message_id
        }));
        assert!(lower_extension
            .envelope
            .render
            .as_ref()
            .unwrap()
            .records
            .iter()
            .any(|record| record.content_text.as_deref() == Some("lower hash extension")));
        acknowledge_prepared(&mut conn, &lower_extension);
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));

        store
            .execute(
                "INSERT INTO blobs (id, data) VALUES ('zzzz-tail', X'CAFE')",
                [],
            )
            .unwrap();
        let tail = match prepare_next_cursor_envelope_outcome_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap()
        {
            CursorPreparationOutcome::Envelope(prepared) => prepared,
            _ => panic!("a new blob beyond the high-water mark must be captured"),
        };
        assert!(tail.envelope.records.iter().any(|record| {
            let bytes = BASE64_STANDARD.decode(&record.data_b64).unwrap();
            let raw: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
            raw["kind"] == "blob" && raw["blob_id"] == "zzzz-tail"
        }));
        acknowledge_prepared(&mut conn, &tail);
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));
    }

    #[test]
    fn cursor_exact_blob_page_retains_high_water_after_empty_follow_up() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        // The fixture contributes two blobs, making this exactly one 256-row
        // capture page. The visitor conservatively reports has_more until the
        // next empty page confirms EOF.
        for index in 0..254 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("page-{index:03}"), vec![index as u8]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let first = match prepare_next_cursor_envelope_outcome_with_limit(
            &mut conn,
            &capabilities(),
            &path,
            LIVE_TARGET_BATCH_BYTES as u64,
        )
        .unwrap()
        {
            CursorPreparationOutcome::Envelope(prepared) => prepared,
            _ => panic!("the initial exact page must prepare an envelope"),
        };
        acknowledge_prepared(&mut conn, &first);
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));
        assert_eq!(
            cursor_store_records::capture_cursor(&conn, first.source_epoch)
                .unwrap()
                .as_deref(),
            Some("page-253")
        );
        assert!(matches!(
            prepare_next_cursor_envelope_outcome_with_limit(
                &mut conn,
                &capabilities(),
                &path,
                LIVE_TARGET_BATCH_BYTES as u64,
            )
            .unwrap(),
            CursorPreparationOutcome::Current
        ));
    }

    #[test]
    fn cursor_capture_pages_large_unreferenced_blob_tables() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("store.db");
        let store = make_cursor_store(&path);
        for index in 0..20 {
            store
                .execute(
                    "INSERT INTO blobs (id, data) VALUES (?1, ?2)",
                    params![format!("bulk-{index:03}"), vec![b'x'; 1024 * 1024]],
                )
                .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();

        let prepared = prepare_next_cursor_envelope(&mut conn, &capabilities(), &path)
            .unwrap()
            .unwrap();
        let captured_bytes: i64 = conn
            .query_row(
                "SELECT COALESCE(SUM(length(record_bytes)), 0) FROM cursor_store_raw_record WHERE source_epoch = ?1",
                [prepared.source_epoch.to_string()],
                |row| row.get(0),
            )
            .unwrap();

        assert!(captured_bytes <= (MAX_RAW_BATCH_BYTES + 2 * 1024 * 1024) as i64);
        assert!(prepared.has_more);
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
    async fn missing_epoch_after_conflict_quarantines_exact_intent() {
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
        let blocked = error.downcast_ref::<StorageV2SourceBlocked>().unwrap();
        assert_eq!(blocked.kind, "source_epoch_conflict_unresolved");
        assert!(blocked.newly_blocked);
        server.await.unwrap();

        let pending = pending_source_envelope::load_for_path(
            &conn,
            &stable_source_path(&path).to_string_lossy(),
        )
        .unwrap()
        .unwrap();
        assert_eq!(pending.attempt_count, 1);
        assert!(pending.blocked_at.is_some());
        let snapshot = pending_source_envelope::snapshot(&conn).unwrap();
        assert_eq!(snapshot.pending_count, 0);
        assert_eq!(snapshot.blocked_source_count, 1);
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
        assert_eq!(
            FileState::new(&conn).get_file_identity(&path_text).unwrap(),
            identity_from_metadata(&path.metadata().unwrap())
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn initial_v2_epoch_adopts_proven_cursor_across_macos_device_remap() {
        use std::os::unix::fs::MetadataExt;

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
        let inode = path.metadata().unwrap().ino();
        conn.execute(
            "UPDATE file_state SET file_identity = ?1 WHERE path = ?2",
            params![format!("unix:16777230:{inode}"), path_text.as_ref()],
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
                predecessor_source_epoch: None,
                replaced_by_source_epoch: None,
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
