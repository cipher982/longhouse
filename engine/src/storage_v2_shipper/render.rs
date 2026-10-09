//! Render records, session facts and render-generation identity.

use super::*;

pub(super) fn storage_v2_media_refs(media_objects: &[ParsedMediaObject]) -> Vec<StorageV2MediaRef> {
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

pub(super) fn bounded_record_ordinal_end(
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

pub(super) fn render_records_for_batch(
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

pub(super) fn opencode_render_records_for_range(
    parse_result: &ParseResult,
    source_ordinals: &[(u64, u64)],
    range_start: u64,
    range_end: u64,
) -> Result<Vec<StorageV2RenderRecord>> {
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

pub(super) fn render_record(
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
        parent_uuid: event.parent_uuid.clone(),
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
        interaction_kind: None,
        raw_record_ordinal,
    })
}

pub(super) fn insert_conversation_reset_boundary(
    records: &mut Vec<StorageV2RenderRecord>,
    source_epoch: Uuid,
    source_position: u64,
    opened_at: &str,
    session_id: &str,
    previous_provider_session_id: &str,
    provider_session_id: &str,
) -> Result<()> {
    for record in records
        .iter_mut()
        .filter(|record| record.source_position == source_position)
    {
        record.event_subordinal = record.event_subordinal.saturating_add(1);
    }
    let fallback_time = DateTime::parse_from_rfc3339(opened_at)
        .context("source epoch opened_at is invalid")?
        .with_timezone(&Utc)
        .timestamp_micros();
    let order_time_us = records
        .iter()
        .map(|record| record.order_time_us)
        .min()
        .map(|value| value.saturating_sub(1))
        .unwrap_or(fallback_time);
    let event_id = Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!(
            "longhouse:conversation-reset:{session_id}:{source_epoch}:{source_position}:{previous_provider_session_id}:{provider_session_id}"
        )
        .as_bytes(),
    );
    records.push(StorageV2RenderRecord {
        event_id: event_id.to_string(),
        order_time_us,
        source_position,
        event_subordinal: 0,
        role: "system".to_string(),
        content_text: Some("Conversation reset".to_string()),
        tool_name: None,
        // Both native ids ride as structured data so ingest can alias the new
        // id back to this session (the rotation used to be recoverable only by
        // reversing the hashed event_id, i.e. not at all). The event_id
        // derivation above must stay untouched: replay idempotency depends on
        // it remaining stable for identical transitions.
        tool_input_json: Some(serde_json::json!({
            "previous_provider_session_id": previous_provider_session_id,
            "provider_session_id": provider_session_id,
        })),
        tool_output_text: None,
        tool_call_id: None,
        parent_uuid: None,
        thread_id: None,
        branch_kind: Some("conversation_reset".to_string()),
        interaction_kind: None,
        raw_record_ordinal: 0,
    });
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
    Ok(())
}

/// Only Claude and Codex write their CLI release into the transcript. Other
/// providers' version fields describe a file format (OMP/Pi writes a numeric
/// schema version), so they ship no CLI version.
fn shipped_provider_version(provider: &str, metadata: &SessionMetadata) -> Option<String> {
    if provider.eq_ignore_ascii_case("claude") || provider.eq_ignore_ascii_case("codex") {
        metadata.version.clone()
    } else {
        None
    }
}

pub(super) fn session_facts(
    provider: &str,
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
        .chain(metadata.last_activity_at)
        .max()
        .or(metadata.ended_at)
        .unwrap_or(started_at);
    // Codex and OMP publish an explicit native child identity that the host
    // must alias before it can resolve a later child-of-child edge. Keep that
    // identity on the wire even when the transcript is not the provider head.
    // Claude's sidechain suppression is intentionally unchanged: its native
    // child identity is not established by this path.
    let preserve_native_identity =
        provider.eq_ignore_ascii_case("codex") || provider.eq_ignore_ascii_case("omp");
    let provider_session_id = if preserve_native_identity {
        metadata
            .provider_session_id
            .clone()
            .or_else(|| (!metadata.session_id.is_empty()).then(|| metadata.session_id.clone()))
    } else {
        routes_as_provider_head(metadata).then(|| {
            metadata
                .provider_session_id
                .clone()
                .unwrap_or_else(|| metadata.session_id.clone())
        })
    };
    let parent_provider_session_id = (metadata.is_sidechain || metadata.is_plain_fork())
        .then(|| {
            metadata
                .parent_provider_session_id
                .clone()
                .or_else(|| metadata.forked_from_session_id.clone())
        })
        .flatten();
    Ok(StorageV2SessionFacts {
        provider_session_id,
        environment: metadata
            .environment
            .clone()
            .unwrap_or_else(|| "local".to_string()),
        project: metadata.project.clone(),
        cwd: metadata.cwd.clone(),
        git_repo: metadata.git_repo.clone(),
        git_branch: metadata.git_branch.clone(),
        provider_version: shipped_provider_version(provider, metadata),
        started_at: started_at.to_rfc3339(),
        last_activity_at: last_activity_at.max(started_at).to_rfc3339(),
        ended_at: metadata.ended_at.map(|value| value.to_rfc3339()),
        origin_kind: metadata.origin_kind.clone(),
        hidden_from_default_timeline: metadata.is_hidden_child(),
        launch_actor: metadata.launch_actor.clone(),
        launch_surface: metadata.launch_surface.clone(),
        is_subagent: metadata.is_sidechain,
        // A plain fork keeps its parent pointer whether or not it is hidden, so
        // the timeline can show where it came from.
        // Provider identity, deliberately unresolved. `forked_from_session_id`
        // is the parent as the provider names it; mapping that to a Longhouse
        // session is the host's job, because a session id here may have come
        // from a managed binding override rather than from the transcript.
        parent_provider_session_id,
        parent_tool_call_id: metadata
            .is_sidechain
            .then(|| metadata.subagent_tool_use_id.clone())
            .flatten(),
        workflow_run_id: metadata
            .is_sidechain
            .then(|| metadata.workflow_run_id.clone())
            .flatten(),
    })
}

pub(super) fn routes_as_provider_head(metadata: &SessionMetadata) -> bool {
    !metadata.is_sidechain
        && metadata.subagent_id.is_none()
        && metadata.parent_provider_session_id.is_none()
        && metadata.forked_from_session_id.is_none()
}

pub(super) fn record_time(record: &StorageV2RenderRecord) -> Option<DateTime<Utc>> {
    DateTime::from_timestamp_micros(record.order_time_us)
}

/// Render a session id the way the Runtime Host defines canonical.
///
/// `_canonical_uuid` in `server/zerg/routers/agents_storage_v2.py:213-220`
/// rejects anything whose text differs from Python's `str(UUID(...))`, which is
/// lowercase. Claude names some transcripts with an uppercase UUID — e.g.
/// `8D188FD9-348C-4D1D-996B-86C8FCE02F04.jsonl` — and the unmanaged path takes
/// the session id straight from that file stem, so those envelopes were
/// rejected 422 forever. It went unnoticed because a managed binding normally
/// supplies a lowercase id that masks it; removing one such binding exposed a
/// file that had never been shippable.
///
/// Only ids that actually parse as a UUID are rewritten. Provider-native ids
/// are not all UUIDs (OpenCode uses `ses_…`), and lowercasing an opaque
/// identifier would silently change which session it names.
pub(super) fn canonical_session_id(session_id: String) -> String {
    match Uuid::parse_str(&session_id) {
        Ok(parsed) => parsed.to_string(),
        Err(_) => session_id,
    }
}

pub(super) fn resolve_session_id(
    provider: &str,
    parse_result: &ParseResult,
    override_id: Option<&str>,
) -> String {
    let parsed = parse_result.metadata.session_id.clone();
    let Some(override_id) = override_id else {
        return canonical_session_id(parsed);
    };
    let resolved = if provider.eq_ignore_ascii_case("codex")
        && !parse_result.metadata.honors_managed_binding()
        && override_id != parsed
    {
        parsed
    } else {
        override_id.to_string()
    };
    canonical_session_id(resolved)
}

pub(super) fn render_generation_id(session_id: Uuid) -> Uuid {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("longhouse-render-v2\0{session_id}\0{PARSER_REVISION}\0{ORDERING_REVISION}")
            .as_bytes(),
    )
}

pub(super) fn cursor_render_generation_id(session_id: &str) -> Uuid {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!(
            "longhouse-cursor-render-v2\0{session_id}\0{CURSOR_PARSER_REVISION}\0cursor-root-order-v1"
        )
        .as_bytes(),
    )
}
