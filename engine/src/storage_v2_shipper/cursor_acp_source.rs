//! The Cursor ACP transcript source.

use super::*;

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
                provider_session_id: None,
                environment: "local".to_string(),
                project: None,
                cwd: None,
                git_repo: None,
                git_branch: None,
                provider_version: None,
                started_at: observed_at.to_rfc3339(),
                last_activity_at: observed_at.to_rfc3339(),
                ended_at: None,
                origin_kind: Some("cursor_acp".to_string()),
                hidden_from_default_timeline: false,
                launch_actor: None,
                launch_surface: None,
                // Cursor stores no subagent lineage.
                is_subagent: false,
                parent_provider_session_id: None,
                parent_tool_call_id: None,
                workflow_run_id: None,
            },
            records: batch
                .records
                .into_iter()
                .map(|record| StorageV2Record {
                    source_position: record.range_start,
                    data_b64: BASE64_STANDARD.encode(record.bytes),
                })
                .collect(),
            facts: Vec::new(),
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
