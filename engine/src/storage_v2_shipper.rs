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

use crate::pipeline::parser::{self, ParseResult, ParsedEvent, Role, SessionMetadata};
use crate::raw_records::{read_next_raw_batch, RawRecordBatch, RawSourceFraming};
use crate::shipping::client::ShipperClient;
use crate::shipping::storage_v2::{StorageV2Capabilities, StorageV2Envelope, StorageV2Record};
use crate::shipping::storage_v2::{StorageV2Render, StorageV2RenderRecord, StorageV2SessionFacts};
use crate::state::file_state::FileState;
use crate::state::source_epoch::{self, SourceChangeHint, SourceEpochResolution, SourceLane};
use crate::storage_v2_contract::{self, EnvelopeIdentity, RangeKind};

pub(crate) const PARSER_REVISION: &str = "engine-parser-v2";
pub(crate) const ORDERING_REVISION: &str = "semantic-order-v2";

pub(crate) struct PreparedStorageV2Envelope {
    pub envelope: StorageV2Envelope,
    pub source_epoch: Uuid,
    pub range_start: u64,
    pub range_end: u64,
    pub event_count: usize,
    pub has_more: bool,
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
    let opaque_source_id = opaque_source_id(&path_text);
    let legacy_offset = FileState::new(conn).get_offset(&path_text)?;
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
        SourceChangeHint::None,
    )?;
    let position = source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    let framing = if provider.eq_ignore_ascii_case("antigravity") {
        RawSourceFraming::WholeDocument
    } else {
        RawSourceFraming::LfDelimited
    };
    let Some(mut raw_batch) = read_next_raw_batch(path, framing, position)? else {
        return Ok(None);
    };
    let parse_result = parser::parse_session_file(path, position)?;
    if framing == RawSourceFraming::LfDelimited && raw_batch.range_end > parse_result.last_good_offset {
        raw_batch.records.retain(|record| record.range_end <= parse_result.last_good_offset);
        let Some(last) = raw_batch.records.last() else {
            return Ok(None);
        };
        raw_batch.range_end = last.range_end;
    }
    let session_id = resolve_session_id(provider, &parse_result, session_id_override);
    let session_uuid = Uuid::parse_str(&session_id).context("storage-v2 session id is not a UUID")?;
    let raw_bytes: Vec<Vec<u8>> = raw_batch.records.iter().map(|record| record.bytes.clone()).collect();
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
    let source_len = std::fs::metadata(path)?.len();
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
            .filter(|event| event.source_offset >= raw_batch.range_start && event.source_offset < raw_batch.range_end)
            .count(),
        has_more: raw_batch.range_end < source_len,
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
    let Some(prepared) = prepare_next_envelope(
        conn,
        capabilities,
        path,
        provider,
        session_id_override,
    )? else {
        return Ok(None);
    };
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
        bytes_shipped: prepared.range_end - prepared.range_start,
        events_shipped: prepared.event_count,
        has_more: prepared.has_more,
    }))
}

fn render_records_for_batch(parse_result: &ParseResult, batch: &RawRecordBatch) -> Result<Vec<StorageV2RenderRecord>> {
    let mut subordinals: HashMap<u64, u32> = HashMap::new();
    let mut records = Vec::new();
    for event in parse_result
        .events
        .iter()
        .filter(|event| event.source_offset >= batch.range_start && event.source_offset < batch.range_end)
    {
        let subordinal = subordinals.entry(event.source_offset).or_default();
        let raw_record_ordinal = batch
            .records
            .iter()
            .position(|record| event.source_offset >= record.range_start && event.source_offset < record.range_end)
            .context("parsed event is not covered by its raw record")?;
        records.push(render_record(event, *subordinal, raw_record_ordinal)?);
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

fn render_record(event: &ParsedEvent, event_subordinal: u32, raw_record_ordinal: usize) -> Result<StorageV2RenderRecord> {
    let tool_input_json = event
        .tool_input_json
        .as_ref()
        .map(|value| serde_json::from_str(value.get()))
        .transpose()
        .context("parsed tool input is not JSON")?;
    Ok(StorageV2RenderRecord {
        event_id: event.uuid.clone(),
        order_time_us: event.timestamp.timestamp_micros(),
        source_position: event.source_offset,
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
        environment: metadata.environment.clone().unwrap_or_else(|| "local".to_string()),
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

fn resolve_session_id(provider: &str, parse_result: &ParseResult, override_id: Option<&str>) -> String {
    let parsed = parse_result.metadata.session_id.clone();
    let Some(override_id) = override_id else {
        return parsed;
    };
    if provider.eq_ignore_ascii_case("codex")
        && (parse_result.metadata.forked_from_session_id.is_some() || parse_result.metadata.is_sidechain)
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
        format!("longhouse-render-v2\0{session_id}\0{PARSER_REVISION}\0{ORDERING_REVISION}").as_bytes(),
    )
}

fn opaque_source_id(path: &str) -> String {
    format!("path-sha256:{}", hex_hash(Sha256::digest(path.as_bytes()).into()))
}

fn hash_file(path: &Path) -> Result<String> {
    let bytes = std::fs::read(path).with_context(|| format!("reading source revision: {}", path.display()))?;
    Ok(hex_hash(Sha256::digest(bytes).into()))
}

fn hex_hash(hash: [u8; 32]) -> String {
    hash.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use std::fs;

    use super::*;
    use crate::state::db::open_db;

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
            range_kinds: vec!["byte_offset".to_string(), "record_ordinal".to_string()],
            lanes: vec!["live".to_string(), "repair".to_string()],
            lane_header: "X-Longhouse-Storage-Lane".to_string(),
        }
    }

    #[test]
    fn prepares_exact_raw_bytes_and_versioned_render_without_advancing_cursor() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("018f0c3a-7b2d-7f10-8a11-123456789abc.jsonl");
        let bytes = b"{\"type\":\"user\",\"uuid\":\"u1\",\"timestamp\":\"2026-07-12T12:00:00Z\",\"message\":{\"content\":\"hello\"}}\n";
        fs::write(&path, bytes).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let prepared = prepare_next_envelope(&mut conn, &capabilities(), &path, "claude", None)
            .unwrap()
            .unwrap();
        assert_eq!(prepared.range_start, 0);
        assert_eq!(prepared.range_end, bytes.len() as u64);
        assert_eq!(prepared.envelope.records[0].data_b64, BASE64_STANDARD.encode(bytes));
        assert_eq!(prepared.envelope.render.as_ref().unwrap().records.len(), 1);
        assert_eq!(
            source_epoch::lane_position(&conn, prepared.source_epoch, SourceLane::Durable).unwrap(),
            0
        );
    }
}
