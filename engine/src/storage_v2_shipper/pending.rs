//! Durable pending envelopes: persist, reload and validate against a fresh
//! preparation.

use super::*;

pub(super) fn persist_prepared(
    conn: &mut Connection,
    source_path: &str,
    mut prepared: PreparedStorageV2Envelope,
) -> Result<PreparedStorageV2Envelope> {
    qa_fault_redact_ingest_marker(&mut prepared);
    let candidate = pending_candidate(source_path, &prepared)?;
    let persisted = pending_source_envelope::persist_or_load(conn, &candidate)?;
    pending_to_prepared(persisted)
}

/// Negative control for the transcript-search producer. Every provider's
/// render records funnel through `persist_prepared`, so blanking the marker
/// here loses it for Claude, Codex, Cursor, OpenCode, Pi and OMP alike.
pub(super) fn qa_fault_redact_ingest_marker(prepared: &mut PreparedStorageV2Envelope) {
    let Some(marker) = crate::qa_fault::ingest_redact_marker() else {
        return;
    };
    let Some(render) = prepared.envelope.render.as_mut() else {
        return;
    };
    let blank = "x".repeat(marker.len());
    let mut redacted = 0usize;
    for record in &mut render.records {
        for text in [&mut record.content_text, &mut record.tool_output_text]
            .into_iter()
            .flatten()
        {
            if text.contains(&marker) {
                redacted += text.matches(&marker).count();
                *text = text.replace(&marker, &blank);
            }
        }
    }
    if redacted > 0 {
        crate::qa_fault::record_fired_named(
            "IngestRedactMarker",
            &prepared.envelope.session_id,
            serde_json::json!({ "redacted_occurrences": redacted }),
        );
    }
}

pub(super) fn pending_candidate(
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

pub(super) fn load_pending_for_source(
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
pub(crate) fn stable_source_path(path: &Path) -> PathBuf {
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
    let absolute = lexical_normalize_absolute(absolute);
    match (absolute.parent(), absolute.file_name()) {
        (Some(parent), Some(file_name)) => std::fs::canonicalize(parent)
            .map(|canonical_parent| canonical_parent.join(file_name))
            .unwrap_or(absolute),
        _ => absolute,
    }
}

pub(super) fn lexical_normalize_absolute(path: PathBuf) -> PathBuf {
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                normalized.pop();
            }
            other => normalized.push(other.as_os_str()),
        }
    }
    normalized
}

pub(super) fn pending_to_prepared(
    pending: PendingSourceEnvelope,
) -> Result<PreparedStorageV2Envelope> {
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

pub(super) fn validate_pending_matches_prepared(
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
