//! What a Runtime Host builds from what the Machine Agent shipped.
//!
//! The host holds, per source epoch, the raw records of every accepted
//! envelope and each envelope's `capture.witness` fact. Rebuilding an
//! envelope's render parses the stored bytes with the captured witness and no
//! access to the laptop: no filesystem, git, clock or local state. The Phase 2
//! check of `host-normalized-transcripts.md` holds this equal to the render
//! the laptop shipped.

use super::render::{
    cursor_render_generation_id, render_generation_id, render_records_for_batch, session_facts,
    storage_v2_media_refs,
};
use super::*;
use crate::pipeline::source::{CapturedWitness, MemorySource, CAPTURE_WITNESS_FACT_KIND};
use crate::raw_records::RawRecord;

/// One source epoch's stored raw stream: accepted envelopes' records laid
/// end to end from the first shipped position.
pub(crate) struct StoredSource {
    base: u64,
    bytes: Vec<u8>,
}

impl StoredSource {
    /// Lay out byte-offset envelopes of one epoch, in range order. Refuses a
    /// gap or overlap: a host missing bytes cannot claim to rebuild.
    pub(crate) fn from_envelopes<'a>(
        envelopes: impl IntoIterator<Item = &'a StorageV2Envelope>,
    ) -> Result<Self> {
        let mut source: Option<Self> = None;
        for envelope in envelopes {
            let stored = source.get_or_insert_with(|| Self {
                base: envelope.range_start,
                bytes: Vec::new(),
            });
            let end = stored.base + stored.bytes.len() as u64;
            anyhow::ensure!(
                envelope.range_start == end,
                "stored raw is not contiguous: envelope starts at {} but stored bytes end at {end}",
                envelope.range_start
            );
            for record in &envelope.records {
                anyhow::ensure!(
                    record.source_position == stored.base + stored.bytes.len() as u64,
                    "raw record at {} is not contiguous",
                    record.source_position
                );
                stored.bytes.extend(record.bytes()?);
            }
            anyhow::ensure!(
                stored.base + stored.bytes.len() as u64 == envelope.range_end,
                "envelope records do not cover {}..{}",
                envelope.range_start,
                envelope.range_end
            );
        }
        source.context("no stored envelopes")
    }
}

/// The captured witness an envelope shipped, with blob bytes from the host's
/// content-addressed media store.
pub(crate) fn envelope_witness(
    envelope: &StorageV2Envelope,
    media: impl Fn(&str) -> Option<Vec<u8>>,
) -> Result<CapturedWitness> {
    let fact = envelope
        .facts
        .iter()
        .find(|fact| fact.kind == CAPTURE_WITNESS_FACT_KIND)
        .with_context(|| {
            format!(
                "envelope {}..{} carries no {CAPTURE_WITNESS_FACT_KIND} fact",
                envelope.range_start, envelope.range_end
            )
        })?;
    let mut witness: CapturedWitness = serde_json::from_value(fact.payload.clone())
        .context("decoding the capture witness fact")?;
    for reference in &envelope.media {
        if let Some(bytes) = media(&reference.sha256) {
            witness.media.insert(reference.sha256.clone(), bytes);
        }
    }
    Ok(witness)
}

#[derive(Debug, Serialize)]
pub(crate) struct RebuiltRender {
    /// The laptop shipped no render for this envelope, and neither does the
    /// host; `records` is then empty.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub withheld: bool,
    pub records: Vec<StorageV2RenderRecord>,
    pub session: StorageV2SessionFacts,
    pub media: Vec<StorageV2MediaRef>,
    /// Provider facts in range, without the capture witness itself.
    pub facts: Vec<StorageV2ProviderFact>,
}

/// Rebuild one byte-offset envelope's render and session facts from stored
/// raw plus its captured witness.
pub(crate) fn rebuild_envelope(
    stored: &StoredSource,
    envelope: &StorageV2Envelope,
    witness: &CapturedWitness,
) -> Result<RebuiltRender> {
    let source = MemorySource::at(
        witness.identity.clone(),
        stored.base,
        stored.bytes.as_slice(),
    );
    let mut parse_result = parser::parse_source(
        &source,
        witness,
        envelope.range_start,
        Some(envelope.range_end),
        Some(&envelope.provider),
    )?;
    parse_result.metadata.managed_binding_is_exact = witness.managed_binding_is_exact;
    let mut records = Vec::with_capacity(envelope.records.len());
    for record in &envelope.records {
        let bytes = record.bytes()?;
        records.push(RawRecord {
            range_start: record.source_position,
            range_end: record.source_position + bytes.len() as u64,
            bytes,
        });
    }
    let batch = RawRecordBatch {
        range_start: envelope.range_start,
        range_end: envelope.range_end,
        records,
    };
    let mut render_records = render_records_for_batch(&parse_result, &batch)?;
    if let Some(previous) = witness.previous_provider_session_id.as_deref() {
        let current = parse_result
            .metadata
            .provider_session_id
            .as_deref()
            .unwrap_or(&parse_result.metadata.session_id)
            .trim()
            .to_string();
        super::render::insert_conversation_reset_boundary(
            &mut render_records,
            Uuid::parse_str(&envelope.source_epoch).context("source epoch is not a UUID")?,
            envelope.range_start,
            &envelope.epoch_opened_at,
            &envelope.session_id,
            previous,
            &current,
        )?;
    }
    let session = session_facts(
        &envelope.provider,
        &parse_result.metadata,
        &render_records,
        &envelope.epoch_opened_at,
    )?;
    let media_objects = parse_result
        .media_objects
        .iter()
        .filter(|media| {
            media.source_offset >= envelope.range_start && media.source_offset < envelope.range_end
        })
        .cloned()
        .collect::<Vec<_>>();
    let facts = parse_result
        .provider_facts
        .iter()
        .filter(|fact| {
            fact.source_offset >= envelope.range_start && fact.source_offset < envelope.range_end
        })
        .map(|fact| StorageV2ProviderFact {
            kind: fact.kind.clone(),
            at: fact.at.to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            source_position: fact.source_offset,
            payload: fact.payload.clone(),
        })
        .collect();
    Ok(RebuiltRender {
        withheld: witness.render_withheld,
        records: if witness.render_withheld {
            Vec::new()
        } else {
            render_records
        },
        session,
        media: storage_v2_media_refs(&media_objects),
        facts,
    })
}

/// Rebuild one OpenCode envelope from the epoch's stored records through it
/// and the envelope's capture witness.
pub(crate) fn rebuild_opencode_envelope(
    stored: &[&StorageV2Envelope],
    envelope: &StorageV2Envelope,
) -> Result<RebuiltRender> {
    use super::opencode_source::{
        opencode_media_objects_for_range, opencode_provider_facts_for_range,
    };
    let fact = envelope
        .facts
        .iter()
        .find(|fact| fact.kind == CAPTURE_WITNESS_FACT_KIND)
        .context("OpenCode envelope carries no capture witness")?;
    let witness: crate::opencode_db::OpenCodeWitness = serde_json::from_value(
        fact.payload
            .get("opencode")
            .cloned()
            .context("capture witness is not an OpenCode witness")?,
    )
    .context("decoding the OpenCode capture witness")?;
    let mut records = Vec::new();
    for stored_envelope in stored {
        for record in &stored_envelope.records {
            records.push((record.source_position, record.bytes()?));
        }
    }
    let previous_provider_session_id = witness.previous_provider_session_id.clone();
    let provider_session_id = witness.provider_session_id.clone();
    let parse = crate::opencode_db::parse_opencode_records(
        &records,
        envelope.range_start,
        envelope.range_end,
        witness,
    )?;
    let source_ordinals: Vec<(u64, u64)> = parse
        .result
        .source_lines
        .iter()
        .zip(&parse.line_ordinals)
        .map(|(line, ordinal)| (line.source_offset, *ordinal))
        .collect();
    let mut render_records = super::render::opencode_render_records_for_range(
        &parse.result,
        &source_ordinals,
        envelope.range_start,
        envelope.range_end,
    )?;
    if let Some(previous) = previous_provider_session_id.as_deref() {
        super::render::insert_conversation_reset_boundary(
            &mut render_records,
            Uuid::parse_str(&envelope.source_epoch).context("source epoch is not a UUID")?,
            envelope.range_start,
            &envelope.epoch_opened_at,
            &envelope.session_id,
            previous,
            &provider_session_id,
        )?;
    }
    let session = session_facts(
        "opencode",
        &parse.result.metadata,
        &render_records,
        &envelope.epoch_opened_at,
    )?;
    let media_objects = opencode_media_objects_for_range(
        &parse.result,
        &source_ordinals,
        envelope.range_start,
        envelope.range_end,
    )?;
    let facts = opencode_provider_facts_for_range(
        &parse.result,
        &source_ordinals,
        envelope.range_start,
        envelope.range_end,
    )?;
    Ok(RebuiltRender {
        withheld: false,
        records: render_records,
        session,
        media: storage_v2_media_refs(&media_objects),
        facts,
    })
}

/// Rebuild one Cursor store envelope from the epoch's stored envelopes up to
/// and including it, plus its capture witness.
///
/// The root order comes from the epoch's own `root_observation` record when
/// one precedes the envelope's end, else from the witness. Visibility, prompt
/// authorship and turn failure are the laptop's shipped answers: the hook
/// journal and `prompt_history.json` are not raw on the host, so a change to
/// those rules still needs the laptop.
pub(crate) fn rebuild_cursor_envelope(
    stored: &[&StorageV2Envelope],
    envelope: &StorageV2Envelope,
) -> Result<RebuiltRender> {
    use super::cursor_store_source::{
        apply_cursor_thread, cursor_session_facts, render_cursor_records, CursorStoreWitness,
    };
    let fact = envelope
        .facts
        .iter()
        .find(|fact| fact.kind == CAPTURE_WITNESS_FACT_KIND)
        .context("Cursor envelope carries no capture witness")?;
    let witness: CursorStoreWitness = serde_json::from_value(
        fact.payload
            .get("cursor")
            .cloned()
            .context("capture witness is not a Cursor witness")?,
    )
    .context("decoding the Cursor capture witness")?;
    let root_ids = if !witness.root_available {
        None
    } else if let Some(ids) = witness.root_message_blob_ids.clone() {
        Some(ids)
    } else {
        let root_blob_id = witness
            .root_blob_id
            .as_deref()
            .context("Cursor witness has a root order but no root blob id")?;
        let mut found = None;
        'search: for stored_envelope in stored {
            for record in &stored_envelope.records {
                if record.source_position >= envelope.range_end {
                    continue;
                }
                let value: Value = serde_json::from_slice(&record.bytes()?)?;
                if value.get("kind").and_then(Value::as_str) == Some("root_observation")
                    && value.get("root_blob_id").and_then(Value::as_str) == Some(root_blob_id)
                {
                    let encoded = value
                        .get("root_blob_bytes_b64")
                        .and_then(Value::as_str)
                        .context("root observation has no bytes")?;
                    found = Some(BASE64_STANDARD.decode(encoded)?);
                    break 'search;
                }
            }
        }
        let bytes = found.with_context(|| {
            format!(
                "no stored root observation for {root_blob_id} before {}",
                envelope.range_end
            )
        })?;
        Some(crate::cursor_store::parse_root_message_blob_ids(&bytes)?)
    };
    let selected = envelope
        .records
        .iter()
        .map(|record| {
            Ok(cursor_store_records::CursorRawRecord {
                source_position: record.source_position,
                bytes: record.bytes()?,
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let started_at_us = DateTime::parse_from_rfc3339(&witness.started_at)
        .context("Cursor witness started_at is not RFC 3339")?
        .timestamp_micros();
    let mut records = render_cursor_records(
        root_ids.as_deref(),
        &selected,
        started_at_us,
        &witness.render,
    )?;
    if let Some(previous) = witness.previous_provider_session_id.as_deref() {
        super::render::insert_conversation_reset_boundary(
            &mut records,
            Uuid::parse_str(&envelope.source_epoch).context("source epoch is not a UUID")?,
            envelope.range_start,
            &envelope.epoch_opened_at,
            &envelope.session_id,
            previous,
            &witness.conversation_uuid,
        )?;
    }
    apply_cursor_thread(&mut records, &witness);
    let session = cursor_session_facts(&witness, !records.is_empty())?;
    Ok(RebuiltRender {
        // The laptop ships a Cursor store render only when it has records.
        withheld: records.is_empty(),
        records,
        session,
        // Cursor ships no provider facts beyond its capture witness.
        media: Vec::new(),
        facts: Vec::new(),
    })
}

/// One source epoch as a Runtime Host stores it: its accepted envelopes in
/// range order, each with its raw records and its facts (the capture witness
/// among them), plus the media bytes those envelopes reference.
#[derive(Debug, Deserialize)]
pub(crate) struct EpochRequest {
    #[serde(default)]
    pub id: Value,
    pub provider: String,
    pub session_id: String,
    pub source_epoch: String,
    pub epoch_opened_at: String,
    pub range_kind: String,
    pub envelopes: Vec<StoredEnvelope>,
    /// Media bytes by sha256, base64.
    #[serde(default)]
    pub media: HashMap<String, String>,
}

#[derive(Debug, Deserialize)]
pub(crate) struct StoredEnvelope {
    pub range_start: u64,
    pub range_end: u64,
    pub records: Vec<StorageV2Record>,
    #[serde(default)]
    pub facts: Vec<StorageV2ProviderFact>,
    #[serde(default)]
    pub media: Vec<StorageV2MediaRef>,
}

/// The rebuild of each envelope of an epoch, or why one could not be rebuilt
/// (an envelope from an engine that shipped no capture witness, for one).
#[derive(Debug, Serialize)]
pub(crate) struct EpochRender {
    pub range_start: u64,
    pub range_end: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub render: Option<RebuiltRender>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

impl EpochRequest {
    fn envelopes(&self) -> Vec<StorageV2Envelope> {
        self.envelopes
            .iter()
            .map(|stored| StorageV2Envelope {
                protocol_version: 2,
                tenant_id: String::new(),
                machine_id: String::new(),
                session_id: self.session_id.clone(),
                provider: self.provider.clone(),
                opaque_source_id: String::new(),
                source_epoch: self.source_epoch.clone(),
                predecessor_source_epoch: None,
                epoch_opened_at: self.epoch_opened_at.clone(),
                range_kind: self.range_kind.clone(),
                range_start: stored.range_start,
                range_end: stored.range_end,
                render: None,
                media: stored.media.clone(),
                session: StorageV2SessionFacts {
                    provider_session_id: None,
                    environment: String::new(),
                    project: None,
                    cwd: None,
                    git_repo: None,
                    git_branch: None,
                    provider_version: None,
                    started_at: String::new(),
                    last_activity_at: String::new(),
                    ended_at: None,
                    origin_kind: None,
                    hidden_from_default_timeline: false,
                    launch_actor: None,
                    launch_surface: None,
                    is_subagent: false,
                    parent_provider_session_id: None,
                    parent_tool_call_id: None,
                    workflow_run_id: None,
                },
                records: stored.records.clone(),
                facts: stored.facts.clone(),
                expected_envelope_id: String::new(),
            })
            .collect()
    }
}

/// Rebuild every envelope of one stored epoch. The epoch's bytes are laid
/// out once; each envelope parses its own range of them.
pub(crate) fn rebuild_epoch(request: &EpochRequest) -> Result<Vec<EpochRender>> {
    let envelopes = request.envelopes();
    let media: HashMap<String, Vec<u8>> = request
        .media
        .iter()
        .filter_map(|(sha, encoded)| {
            BASE64_STANDARD
                .decode(encoded)
                .ok()
                .map(|bytes| (sha.clone(), bytes))
        })
        .collect();
    let provider = request.provider.to_ascii_lowercase();
    let byte_offset = request.range_kind == "byte_offset";
    let stored = if byte_offset {
        Some(StoredSource::from_envelopes(envelopes.iter())?)
    } else {
        None
    };
    let mut out = Vec::with_capacity(envelopes.len());
    for (index, envelope) in envelopes.iter().enumerate() {
        let rebuilt = (|| -> Result<RebuiltRender> {
            if let Some(stored) = stored.as_ref() {
                let witness = envelope_witness(envelope, |sha| media.get(sha).cloned())?;
                rebuild_envelope(stored, envelope, &witness)
            } else {
                let through: Vec<&StorageV2Envelope> = envelopes[..=index].iter().collect();
                match provider.as_str() {
                    "opencode" => rebuild_opencode_envelope(&through, envelope),
                    "cursor" => rebuild_cursor_envelope(&through, envelope),
                    other => anyhow::bail!("no record-ordinal rebuild for provider {other}"),
                }
            }
        })();
        out.push(match rebuilt {
            Ok(render) => EpochRender {
                range_start: envelope.range_start,
                range_end: envelope.range_end,
                render: Some(render),
                error: None,
            },
            Err(error) => EpochRender {
                range_start: envelope.range_start,
                range_end: envelope.range_end,
                render: None,
                error: Some(format!("{error:#}")),
            },
        });
    }
    Ok(out)
}

/// The render generation a host rebuild of this epoch belongs to: the same
/// identity the Machine Agent's shipper mints for this kind of source, so a
/// host render and a laptop render at one revision land in one generation.
fn epoch_generation(request: &EpochRequest) -> Option<Value> {
    let provider = request.provider.to_ascii_lowercase();
    if request.range_kind != "byte_offset" && provider == "cursor" {
        return Some(serde_json::json!({
            "generation_id": cursor_render_generation_id(&request.session_id).to_string(),
            "parser_revision": CURSOR_PARSER_REVISION,
            "ordering_revision": "cursor-root-order-v1",
        }));
    }
    let session = Uuid::parse_str(&request.session_id).ok()?;
    Some(serde_json::json!({
        "generation_id": render_generation_id(session).to_string(),
        "parser_revision": PARSER_REVISION,
        "ordering_revision": ORDERING_REVISION,
    }))
}

/// The Runtime Host's normalizer process: one JSON request per line on
/// stdin, one JSON response per line on stdout, until stdin closes. A request
/// that fails or panics answers with an error and the worker keeps serving.
pub(crate) fn serve_normalize_worker(
    input: impl std::io::BufRead,
    mut output: impl std::io::Write,
) -> Result<()> {
    for line in input.lines() {
        let line = line.context("reading a normalize request")?;
        if line.trim().is_empty() {
            continue;
        }
        let mut generation = Value::Null;
        let mut response = match serde_json::from_str::<EpochRequest>(&line) {
            Err(error) => {
                serde_json::json!({ "id": Value::Null, "error": format!("bad request: {error}") })
            }
            Ok(request) => {
                generation = epoch_generation(&request).unwrap_or(Value::Null);
                let started = std::time::Instant::now();
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    rebuild_epoch(&request)
                })) {
                    Ok(Ok(envelopes)) => serde_json::json!({
                        "id": request.id,
                        "envelopes": envelopes,
                        "elapsed_us": started.elapsed().as_micros() as u64,
                    }),
                    Ok(Err(error)) => {
                        serde_json::json!({ "id": request.id, "error": format!("{error:#}") })
                    }
                    Err(_) => {
                        serde_json::json!({ "id": request.id, "error": "normalizer panicked" })
                    }
                }
            }
        };
        // Every answer names the revision, so a caller learns it from a probe
        // before it has anything to rebuild.
        response["revision"] = Value::from(normalizer_revision());
        // And, for any request it could read, the generation a host render of
        // that source belongs to, even when there was nothing to rebuild.
        if !generation.is_null() {
            response["generation"] = generation;
        }
        serde_json::to_writer(&mut output, &response)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    //! Phase 2 check: for every transcript fixture, ship it through the real
    //! shipper in small envelopes, rebuild each envelope from only the stored
    //! raw records and its capture fact, and require the rebuild to equal what
    //! the laptop shipped.

    use super::*;
    use crate::state::db::open_db;
    use std::fs;

    fn fixtures_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures")
    }

    fn files_in(dir: &Path, out: &mut Vec<PathBuf>) {
        let Ok(entries) = fs::read_dir(dir) else {
            return;
        };
        let mut paths: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).collect();
        paths.sort();
        for path in paths {
            if path.is_dir() {
                files_in(&path, out);
            } else {
                out.push(path);
            }
        }
    }

    /// `(fixture tree, provider)`. A tree is copied whole so sidecars
    /// (subagent meta, workflow scripts, blobs) sit where the shipper looks.
    fn fixture_sets() -> Vec<(&'static str, &'static str)> {
        vec![
            ("golden/claude", "claude"),
            ("adversarial/claude", "claude"),
            ("workflows/claude", "claude"),
            ("golden/codex", "codex"),
            ("adversarial/codex", "codex"),
            ("codex", "codex"),
            ("golden/cursor", "cursor"),
            ("golden/antigravity", "antigravity"),
            ("golden/antigravity_legacy_json", "antigravity"),
            ("adversarial/antigravity_legacy_json", "antigravity"),
            ("golden/pi", "pi"),
            ("golden/omp", "omp"),
        ]
    }

    fn is_transcript(path: &Path) -> bool {
        let name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or_default();
        (name.ends_with(".jsonl") || name.ends_with(".json"))
            && !name.contains(".expected.")
            && !name.ends_with(".meta.json")
    }

    #[derive(Clone)]
    struct Shipped {
        envelopes: Vec<StorageV2Envelope>,
        media: HashMap<String, Vec<u8>>,
    }

    fn ship_all(path: &Path, provider: &str, state_dir: &Path, batch_bytes: usize) -> Shipped {
        let mut conn = open_db(Some(&state_dir.join("state.db"))).unwrap();
        let mut shipped = Shipped {
            envelopes: Vec::new(),
            media: HashMap::new(),
        };
        for _ in 0..10_000 {
            let Some(prepared) = prepare::prepare_next_envelope_with_limit(
                &mut conn,
                &crate::storage_v2_shipper::tests::capabilities(),
                path,
                provider,
                None,
                batch_bytes,
            )
            .unwrap() else {
                return shipped;
            };
            pending_source_envelope::acknowledge_and_delete(
                &mut conn,
                prepared.source_epoch,
                &prepared.envelope.expected_envelope_id,
                prepared.range_start,
                prepared.range_end,
            )
            .unwrap();
            for media in &prepared.media_objects {
                shipped
                    .media
                    .insert(media.sha256.clone(), media.bytes.clone());
            }
            shipped.envelopes.push(prepared.envelope);
        }
        panic!("{} never finished shipping", path.display());
    }

    fn rebuild_mismatches(shipped: &Shipped) -> Vec<String> {
        let mut problems = Vec::new();
        let mut by_epoch: Vec<(String, Vec<&StorageV2Envelope>)> = Vec::new();
        for envelope in &shipped.envelopes {
            match by_epoch
                .iter_mut()
                .find(|(epoch, _)| *epoch == envelope.source_epoch)
            {
                Some((_, list)) => list.push(envelope),
                None => by_epoch.push((envelope.source_epoch.clone(), vec![envelope])),
            }
        }
        for (_, envelopes) in by_epoch {
            for (index, envelope) in envelopes.iter().enumerate() {
                // The host rebuilds an envelope from everything stored up to it.
                let stored = match StoredSource::from_envelopes(envelopes[..=index].iter().copied())
                {
                    Ok(stored) => stored,
                    Err(error) => {
                        problems.push(format!("{}: {error:#}", envelope.range_start));
                        continue;
                    }
                };
                let witness =
                    match envelope_witness(envelope, |sha| shipped.media.get(sha).cloned()) {
                        Ok(witness) => witness,
                        Err(error) => {
                            problems.push(format!("{error:#}"));
                            continue;
                        }
                    };
                let rebuilt = match rebuild_envelope(&stored, envelope, &witness) {
                    Ok(rebuilt) => rebuilt,
                    Err(error) => {
                        problems.push(format!("{}: {error:#}", envelope.range_start));
                        continue;
                    }
                };
                problems.extend(compare_rebuilt(envelope, &rebuilt));
            }
        }
        problems
    }

    /// Every difference between what the laptop shipped and what the host
    /// rebuilt: render records, session facts, media refs and provider facts.
    fn compare_rebuilt(envelope: &StorageV2Envelope, rebuilt: &RebuiltRender) -> Vec<String> {
        let mut problems = Vec::new();
        let shipped_facts: Vec<&StorageV2ProviderFact> = envelope
            .facts
            .iter()
            .filter(|fact| fact.kind != CAPTURE_WITNESS_FACT_KIND)
            .collect();
        let pairs = [
            (
                "render",
                envelope
                    .render
                    .as_ref()
                    .map(|render| serde_json::to_value(&render.records).unwrap())
                    .unwrap_or(Value::Null),
                if rebuilt.withheld {
                    Value::Null
                } else {
                    serde_json::to_value(&rebuilt.records).unwrap()
                },
            ),
            (
                "session",
                serde_json::to_value(&envelope.session).unwrap(),
                serde_json::to_value(&rebuilt.session).unwrap(),
            ),
            (
                "media",
                serde_json::to_value(&envelope.media).unwrap(),
                serde_json::to_value(&rebuilt.media).unwrap(),
            ),
            (
                "facts",
                serde_json::to_value(&shipped_facts).unwrap(),
                serde_json::to_value(&rebuilt.facts).unwrap(),
            ),
        ];
        for (what, shipped, rebuilt) in pairs {
            if shipped != rebuilt {
                problems.push(format!(
                    "range {}..{} {what} differs:\n  shipped {shipped}\n  rebuilt {rebuilt}",
                    envelope.range_start, envelope.range_end
                ));
            }
        }
        problems
    }

    fn opencode_envelopes(
        state: &mut rusqlite::Connection,
        db_path: &Path,
    ) -> Vec<StorageV2Envelope> {
        let mut out = Vec::new();
        while let Some(prepared) = opencode_source::prepare_next_opencode_envelope(
            state,
            &crate::storage_v2_shipper::tests::capabilities(),
            db_path,
        )
        .unwrap()
        {
            pending_source_envelope::acknowledge_and_delete(
                state,
                prepared.source_epoch,
                &prepared.envelope.expected_envelope_id,
                prepared.range_start,
                prepared.range_end,
            )
            .unwrap();
            out.push(prepared.envelope);
        }
        out
    }

    fn opencode_mismatches(envelopes: &[StorageV2Envelope]) -> Vec<String> {
        let mut problems = Vec::new();
        for (index, envelope) in envelopes.iter().enumerate() {
            let stored: Vec<&StorageV2Envelope> = envelopes[..=index]
                .iter()
                .filter(|earlier| earlier.source_epoch == envelope.source_epoch)
                .collect();
            match rebuild_opencode_envelope(&stored, envelope) {
                Ok(rebuilt) => problems.extend(compare_rebuilt(envelope, &rebuilt)),
                Err(error) => problems.push(format!(
                    "range {}..{}: {error:#}",
                    envelope.range_start, envelope.range_end
                )),
            }
        }
        problems
    }

    #[test]
    fn host_rebuild_of_opencode_equals_laptop_render() {
        // Rows settle out of order: an assistant message is still being written
        // when its parts ship, so the witness carries it; parts created in the
        // same millisecond sort by load index, which only the laptop knows.
        use crate::storage_v2_shipper::tests::{oc_fixture, oc_now_ms, oc_turn};
        let _guard = crate::console_adapter::agent_state_guard();
        let mut fixture = oc_fixture("ses_rebuild");
        let base = oc_now_ms() - 60_000;
        let mut envelopes = Vec::new();
        for turn in 0..6 {
            let t = base + (turn as i64) * 100;
            let (provider, state, db_path) =
                (&fixture.provider, &mut fixture.state, &fixture.db_path);
            let mut mid = Vec::new();
            oc_turn(provider, "ses_rebuild", turn, t, 200, &mut || {
                mid.extend(opencode_envelopes(state, db_path));
            });
            envelopes.extend(mid);
            envelopes.extend(opencode_envelopes(state, db_path));
        }
        assert!(envelopes.len() >= 6, "{} envelopes", envelopes.len());
        let mut problems = opencode_mismatches(&envelopes);
        problems.extend(worker_mismatches(&envelopes, &HashMap::new()));
        assert!(problems.is_empty(), "{}", problems.join("\n"));
        let witness_bytes: usize = envelopes
            .iter()
            .flat_map(|envelope| &envelope.facts)
            .filter(|fact| fact.kind == CAPTURE_WITNESS_FACT_KIND)
            .map(|fact| fact.payload.to_string().len())
            .max()
            .unwrap();
        assert!(
            witness_bytes < 16 * 1024,
            "witness is {witness_bytes} bytes"
        );
        assert!(
            envelopes
                .iter()
                .flat_map(|envelope| &envelope.facts)
                .filter(|fact| fact.kind == CAPTURE_WITNESS_FACT_KIND)
                .any(|fact| fact.payload["opencode"]["messages"]
                    .as_array()
                    .is_some_and(|messages| !messages.is_empty())),
            "no envelope shipped a part ahead of its message; the case is not exercised"
        );

        // The witness is an input: without the part indexes or the unsettled
        // messages the host cannot reproduce the laptop.
        let mut stripped = envelopes.clone();
        for envelope in &mut stripped {
            for fact in &mut envelope.facts {
                if fact.kind == CAPTURE_WITNESS_FACT_KIND {
                    fact.payload["opencode"]["part_indexes"] = serde_json::json!([]);
                }
            }
        }
        assert!(!opencode_mismatches(&stripped).is_empty());
    }

    #[test]
    fn host_rebuild_reproduces_a_conversation_reset_boundary() {
        // A managed session that moves to a new provider session opens the new
        // source with a reset boundary. Which provider session came before is
        // laptop knowledge, so it rides the witness.
        let dir = tempfile::tempdir().unwrap();
        let old_provider_id = "018f0c3a-7b2d-7f10-8a11-000000000001";
        let new_provider_id = "018f0c3a-7b2d-7f10-8a11-000000000002";
        let managed_session_id = "018f0c3a-7b2d-7f10-8a11-000000000042";
        let path_for = |id: &str| dir.path().join(format!("{id}.jsonl"));
        for (id, uuid, text, minute) in [
            (old_provider_id, "old-user", "before", "00"),
            (new_provider_id, "new-user", "after", "01"),
        ] {
            fs::write(
                path_for(id),
                format!(
                    "{{\"type\":\"user\",\"sessionId\":\"{id}\",\"uuid\":\"{uuid}\",\"timestamp\":\"2026-07-12T12:{minute}:00Z\",\"message\":{{\"content\":\"{text}\"}}}}\n"
                ),
            )
            .unwrap();
        }
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        let binding = crate::state::session_binding::SessionBinding::new(&conn);
        for id in [old_provider_id, new_provider_id] {
            binding
                .bind(
                    &fs::canonicalize(path_for(id)).unwrap().to_string_lossy(),
                    managed_session_id,
                    "claude",
                )
                .unwrap();
        }
        let mut shipped = Shipped {
            envelopes: Vec::new(),
            media: HashMap::new(),
        };
        for id in [old_provider_id, new_provider_id] {
            let prepared = prepare::prepare_next_envelope_with_limit(
                &mut conn,
                &crate::storage_v2_shipper::tests::capabilities(),
                &path_for(id),
                "claude",
                None,
                usize::MAX / 2,
            )
            .unwrap()
            .unwrap();
            pending_source_envelope::acknowledge_and_delete(
                &mut conn,
                prepared.source_epoch,
                &prepared.envelope.expected_envelope_id,
                prepared.range_start,
                prepared.range_end,
            )
            .unwrap();
            shipped.envelopes.push(prepared.envelope);
        }
        let boundary_count = |envelope: &StorageV2Envelope| {
            envelope
                .render
                .as_ref()
                .map(|render| {
                    render
                        .records
                        .iter()
                        .filter(|record| {
                            record.branch_kind.as_deref() == Some("conversation_reset")
                        })
                        .count()
                })
                .unwrap_or(0)
        };
        assert_eq!(boundary_count(&shipped.envelopes[1]), 1);
        let problems = rebuild_mismatches(&shipped);
        assert!(problems.is_empty(), "{}", problems.join("\n"));

        let mut without = shipped.clone();
        for fact in &mut without.envelopes[1].facts {
            if fact.kind == CAPTURE_WITNESS_FACT_KIND {
                fact.payload
                    .as_object_mut()
                    .unwrap()
                    .remove("previous_provider_session_id");
            }
        }
        assert!(!rebuild_mismatches(&without).is_empty());
    }

    #[test]
    fn host_rebuild_keeps_a_longhouse_fork_visible() {
        // A fork Longhouse started is bound to its own thread on the laptop,
        // which keeps it in the default timeline. Only the laptop's binding
        // says so; the witness carries it.
        use crate::storage_v2_shipper::tests::{codex_forked_child_lines, FORK_CHILD_THREAD};
        let dir = tempfile::tempdir().unwrap();
        let path = dir
            .path()
            .join("rollout-2026-02-15T10-00-00-dddd1111.jsonl");
        fs::write(&path, codex_forked_child_lines()).unwrap();
        let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
        crate::state::session_binding::SessionBinding::new(&conn)
            .bind_for_thread(
                &stable_source_path(&path).to_string_lossy(),
                "019d2869-1111-7222-8333-aaaaaaaaaaaa",
                "codex",
                Some(FORK_CHILD_THREAD),
            )
            .unwrap();
        let prepared = prepare::prepare_next_envelope_with_limit(
            &mut conn,
            &crate::storage_v2_shipper::tests::capabilities(),
            &path,
            "codex",
            None,
            usize::MAX / 2,
        )
        .unwrap()
        .unwrap();
        assert!(!prepared.envelope.session.hidden_from_default_timeline);
        let shipped = Shipped {
            envelopes: vec![prepared.envelope],
            media: HashMap::new(),
        };
        let problems = rebuild_mismatches(&shipped);
        assert!(problems.is_empty(), "{}", problems.join("\n"));
        let mut without = shipped.clone();
        for fact in &mut without.envelopes[0].facts {
            if fact.kind == CAPTURE_WITNESS_FACT_KIND {
                fact.payload
                    .as_object_mut()
                    .unwrap()
                    .remove("managed_binding_is_exact");
            }
        }
        assert!(!rebuild_mismatches(&without).is_empty());
    }

    #[test]
    fn host_rebuild_withholds_the_render_the_laptop_withheld() {
        // A Cursor agent transcript with no clock and no events ships raw only;
        // the host must not mint a render for it either.
        let dir = tempfile::tempdir().unwrap();
        let transcripts = dir.path().join("agent-transcripts");
        fs::create_dir_all(&transcripts).unwrap();
        let path = transcripts.join("not-a-uuid.jsonl");
        fs::write(&path, "{}\n").unwrap();
        let shipped = ship_all(&path, "cursor", dir.path(), usize::MAX / 2);
        assert!(!shipped.envelopes.is_empty());
        assert!(shipped
            .envelopes
            .iter()
            .all(|envelope| envelope.render.is_none()));
        let problems = rebuild_mismatches(&shipped);
        assert!(problems.is_empty(), "{}", problems.join("\n"));
        let mut without = Shipped {
            envelopes: shipped.envelopes.clone(),
            media: HashMap::new(),
        };
        for envelope in &mut without.envelopes {
            for fact in &mut envelope.facts {
                if fact.kind == CAPTURE_WITNESS_FACT_KIND {
                    fact.payload
                        .as_object_mut()
                        .unwrap()
                        .remove("render_withheld");
                }
            }
        }
        assert!(!rebuild_mismatches(&without).is_empty());
    }

    #[test]
    fn render_does_not_depend_on_where_envelopes_were_cut() {
        // The served timeline must be a function of the transcript, not of how
        // the shipper happened to batch it: whole-file and 512-byte envelopes
        // render the same rows (apart from each row's index within its own
        // envelope).
        let _guard = crate::console_adapter::agent_state_guard();
        let scratch = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", scratch.path().join("longhouse"));
        }
        let rows = |shipped: &Shipped| -> Vec<Value> {
            let mut rows: Vec<Value> = shipped
                .envelopes
                .iter()
                .flat_map(|envelope| {
                    envelope
                        .render
                        .iter()
                        .flat_map(|render| render.records.iter())
                })
                .map(|record| {
                    let mut value = serde_json::to_value(record).unwrap();
                    value.as_object_mut().unwrap().remove("raw_record_ordinal");
                    value
                })
                .collect();
            rows.sort_by_key(|row| row.to_string());
            rows
        };
        let mut failures = Vec::new();
        for (tree, provider) in fixture_sets() {
            let source_root = fixtures_root().join(tree);
            let mut files = Vec::new();
            files_in(&source_root, &mut files);
            for fixture in files.into_iter().filter(|path| is_transcript(path)) {
                let relative = fixture.strip_prefix(&source_root).unwrap();
                let mut renders = Vec::new();
                for batch_bytes in [usize::MAX / 2, 512] {
                    let case = tempfile::tempdir().unwrap();
                    let copy_root = case.path().join("src");
                    let mut tree_files = Vec::new();
                    files_in(&source_root, &mut tree_files);
                    for file in tree_files {
                        let target = copy_root.join(file.strip_prefix(&source_root).unwrap());
                        fs::create_dir_all(target.parent().unwrap()).unwrap();
                        fs::copy(&file, &target).unwrap();
                    }
                    let path = copy_root.join(relative);
                    renders.push(rows(&ship_all(&path, provider, case.path(), batch_bytes)));
                }
                if renders[0] != renders[1] {
                    let whole: std::collections::HashSet<String> =
                        renders[0].iter().map(Value::to_string).collect();
                    let cut: std::collections::HashSet<String> =
                        renders[1].iter().map(Value::to_string).collect();
                    failures.push(format!(
                        "{tree}/{}: only whole {:?}\n  only cut {:?}",
                        relative.display(),
                        whole.difference(&cut).take(2).collect::<Vec<_>>(),
                        cut.difference(&whole).take(2).collect::<Vec<_>>()
                    ));
                }
            }
        }
        unsafe {
            match previous_home {
                Some(value) => std::env::set_var("LONGHOUSE_HOME", value),
                None => std::env::remove_var("LONGHOUSE_HOME"),
            }
        }
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    #[test]
    fn host_rebuild_resolves_provider_blobs_from_shipped_media() {
        // A pasted image lives in the provider's blob store, not the
        // transcript. The host has only the shipped media object, keyed by the
        // same digest, and must render the same row from it.
        use sha2::{Digest, Sha256};
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let blob_bytes = b"\x89PNG\r\nblob-backed image".to_vec();
        let digest = format!("{:x}", Sha256::digest(&blob_bytes));
        let blob_dir = dir.path().join("agent").join("blobs");
        fs::create_dir_all(&blob_dir).unwrap();
        fs::write(blob_dir.join(&digest), &blob_bytes).unwrap();
        let session_dir = dir.path().join("agent").join("sessions").join("cwd");
        fs::create_dir_all(&session_dir).unwrap();
        let path = session_dir.join("session.jsonl");
        let header = r#"{"type":"session","id":"omp-native","cwd":"/tmp/longhouse-test","timestamp":"2026-11-14T10:00:00Z"}"#;
        let line = format!(
            r#"{{"type":"message","id":"omp-user","timestamp":"2026-11-14T10:00:01Z","message":{{"role":"user","content":[{{"type":"text","text":"look at this"}},{{"type":"image","data":"blob:sha256:{digest}","mimeType":"image/png"}}]}}}}"#
        );
        fs::write(&path, format!("{header}\n{line}\n")).unwrap();

        let shipped = ship_all(&path, "omp", dir.path(), usize::MAX / 2);
        assert_eq!(shipped.media.get(&digest), Some(&blob_bytes));
        let problems = rebuild_mismatches(&shipped);
        assert!(problems.is_empty(), "{}", problems.join("\n"));

        // Without the media the host must not invent the row it cannot see.
        let without_media = Shipped {
            envelopes: shipped.envelopes.clone(),
            media: HashMap::new(),
        };
        assert!(
            !rebuild_mismatches(&without_media).is_empty(),
            "the blob is an input: dropping it must change the rebuild"
        );
    }

    /// Rebuild through the worker protocol, as the Runtime Host will: each
    /// epoch serialized to one request line, answers read back from stdout.
    fn worker_mismatches(
        envelopes: &[StorageV2Envelope],
        media: &HashMap<String, Vec<u8>>,
    ) -> Vec<String> {
        let mut epochs: Vec<Vec<&StorageV2Envelope>> = Vec::new();
        for envelope in envelopes {
            match epochs
                .iter_mut()
                .find(|epoch| epoch[0].source_epoch == envelope.source_epoch)
            {
                Some(epoch) => epoch.push(envelope),
                None => epochs.push(vec![envelope]),
            }
        }
        let mut input = String::new();
        for (index, epoch) in epochs.iter().enumerate() {
            let first = epoch[0];
            let request = serde_json::json!({
                "id": index,
                "provider": first.provider,
                "session_id": first.session_id,
                "source_epoch": first.source_epoch,
                "epoch_opened_at": first.epoch_opened_at,
                "range_kind": first.range_kind,
                "envelopes": epoch.iter().map(|envelope| serde_json::json!({
                    "range_start": envelope.range_start,
                    "range_end": envelope.range_end,
                    "records": envelope.records,
                    "facts": envelope.facts,
                    "media": envelope.media,
                })).collect::<Vec<_>>(),
                "media": media.iter().map(|(sha, bytes)| (sha.clone(), BASE64_STANDARD.encode(bytes))).collect::<HashMap<_, _>>(),
            });
            input.push_str(&serde_json::to_string(&request).unwrap());
            input.push('\n');
        }
        let mut output = Vec::new();
        serve_normalize_worker(input.as_bytes(), &mut output).unwrap();
        let mut problems = Vec::new();
        for (epoch, line) in epochs
            .iter()
            .zip(String::from_utf8(output).unwrap().lines())
        {
            let response: Value = serde_json::from_str(line).unwrap();
            if let Some(error) = response.get("error") {
                problems.push(format!("worker error: {error}"));
                continue;
            }
            for (envelope, answer) in epoch.iter().zip(response["envelopes"].as_array().unwrap()) {
                if let Some(error) = answer.get("error") {
                    problems.push(format!(
                        "range {}..{}: {error}",
                        envelope.range_start, envelope.range_end
                    ));
                    continue;
                }
                let render = &answer["render"];
                let shipped_facts: Vec<&StorageV2ProviderFact> = envelope
                    .facts
                    .iter()
                    .filter(|fact| fact.kind != CAPTURE_WITNESS_FACT_KIND)
                    .collect();
                let pairs = [
                    (
                        "render",
                        envelope
                            .render
                            .as_ref()
                            .map(|r| serde_json::to_value(&r.records).unwrap())
                            .unwrap_or(Value::Null),
                        if render["withheld"].as_bool() == Some(true) {
                            Value::Null
                        } else {
                            render["records"].clone()
                        },
                    ),
                    (
                        "session",
                        serde_json::to_value(&envelope.session).unwrap(),
                        render["session"].clone(),
                    ),
                    (
                        "media",
                        serde_json::to_value(&envelope.media).unwrap(),
                        render["media"].clone(),
                    ),
                    (
                        "facts",
                        serde_json::to_value(&shipped_facts).unwrap(),
                        render["facts"].clone(),
                    ),
                ];
                for (what, shipped, rebuilt) in pairs {
                    if shipped != rebuilt {
                        problems.push(format!(
                            "worker range {}..{} {what} differs:\n  shipped {shipped}\n  rebuilt {rebuilt}",
                            envelope.range_start, envelope.range_end
                        ));
                    }
                }
            }
        }
        problems
    }

    #[test]
    fn a_host_rebuild_names_the_generation_the_shipper_mints() {
        let session = Uuid::new_v4();
        let request = |provider: &str, range_kind: &str| EpochRequest {
            id: Value::Null,
            provider: provider.to_string(),
            session_id: session.to_string(),
            source_epoch: Uuid::new_v4().to_string(),
            epoch_opened_at: "2026-11-15T00:00:00Z".to_string(),
            range_kind: range_kind.to_string(),
            envelopes: Vec::new(),
            media: HashMap::new(),
        };
        let file = epoch_generation(&request("claude", "byte_offset")).unwrap();
        assert_eq!(file["generation_id"], Value::from(render_generation_id(session).to_string()));
        assert_eq!(file["parser_revision"], Value::from(PARSER_REVISION));
        assert_eq!(file["ordering_revision"], Value::from(ORDERING_REVISION));
        let cursor = epoch_generation(&request("cursor", "record_ordinal")).unwrap();
        assert_eq!(
            cursor["generation_id"],
            Value::from(cursor_render_generation_id(&session.to_string()).to_string())
        );
        assert_eq!(cursor["parser_revision"], Value::from(CURSOR_PARSER_REVISION));
        // A Cursor JSONL transcript goes through the file parser.
        let cursor_file = epoch_generation(&request("cursor", "byte_offset")).unwrap();
        assert_eq!(cursor_file["parser_revision"], Value::from(PARSER_REVISION));
    }

    #[test]
    fn normalize_worker_answers_errors_and_keeps_serving() {
        let input = "not json\n{\"provider\":\"claude\",\"session_id\":\"s\",\"source_epoch\":\"e\",\"epoch_opened_at\":\"2026-11-14T00:00:00Z\",\"range_kind\":\"byte_offset\",\"envelopes\":[]}\n";
        let mut output = Vec::new();
        serve_normalize_worker(input.as_bytes(), &mut output).unwrap();
        let lines: Vec<Value> = String::from_utf8(output)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(lines.len(), 2);
        assert!(lines[0]["error"]
            .as_str()
            .unwrap()
            .starts_with("bad request"));
        assert_eq!(lines[0]["revision"], Value::from(normalizer_revision()));
        assert!(lines[1]["error"]
            .as_str()
            .unwrap()
            .contains("no stored envelopes"));
        // Session "s" is not a UUID, so no file-parser generation is named.
        assert!(lines[1].get("generation").is_none());
    }

    #[test]
    fn host_rebuild_from_shipped_envelopes_equals_laptop_render() {
        let _guard = crate::console_adapter::agent_state_guard();
        let scratch = tempfile::tempdir().unwrap();
        let previous_home = std::env::var_os("LONGHOUSE_HOME");
        unsafe {
            std::env::set_var("LONGHOUSE_HOME", scratch.path().join("longhouse"));
        }
        let mut failures = Vec::new();
        let mut checked = 0usize;
        for (tree, provider) in fixture_sets() {
            let source_root = fixtures_root().join(tree);
            let mut files = Vec::new();
            files_in(&source_root, &mut files);
            for fixture in files.into_iter().filter(|path| is_transcript(path)) {
                let relative = fixture.strip_prefix(&source_root).unwrap();
                // Whole envelopes and many small ones: a resumed parse must
                // rebuild from stored raw exactly as the first one does.
                for batch_bytes in [usize::MAX / 2, 512] {
                    let case = tempfile::tempdir().unwrap();
                    let copy_root = case.path().join("src");
                    let mut tree_files = Vec::new();
                    files_in(&source_root, &mut tree_files);
                    for file in tree_files {
                        let target = copy_root.join(file.strip_prefix(&source_root).unwrap());
                        fs::create_dir_all(target.parent().unwrap()).unwrap();
                        fs::copy(&file, &target).unwrap();
                    }
                    let path = copy_root.join(relative);
                    let shipped = ship_all(&path, provider, case.path(), batch_bytes);
                    checked += shipped.envelopes.len();
                    // The host has none of the laptop's files: delete them
                    // before rebuilding, so a hidden disk read cannot pass.
                    drop(case);
                    assert!(!path.exists());
                    for problem in rebuild_mismatches(&shipped)
                        .into_iter()
                        .chain(worker_mismatches(&shipped.envelopes, &shipped.media))
                    {
                        failures.push(format!(
                            "{tree}/{} (batch {batch_bytes}): {problem}",
                            relative.display()
                        ));
                    }
                }
            }
        }
        unsafe {
            match previous_home {
                Some(value) => std::env::set_var("LONGHOUSE_HOME", value),
                None => std::env::remove_var("LONGHOUSE_HOME"),
            }
        }
        assert!(checked > 0, "no envelopes were shipped");
        assert!(
            failures.is_empty(),
            "{} of the rebuilt envelopes differ from what the laptop shipped:\n{}",
            failures.len(),
            failures.join("\n")
        );
    }

    // ---- Cursor store ----

    const CURSOR_CONVERSATION: &str = "7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f";
    const MANAGED_SESSION: &str = "018f0c3a-7b2d-7f10-8a11-1234567890ab";

    fn blob_id(byte: u8) -> String {
        format!("{byte:02x}").repeat(32)
    }

    fn hex_text(text: &str) -> String {
        text.bytes().map(|byte| format!("{byte:02x}")).collect()
    }

    /// A Cursor store: `meta['0']` names the root, the root lists messages.
    fn write_cursor_store(path: &Path, root_byte: u8, messages: &[(u8, serde_json::Value)]) {
        let store = rusqlite::Connection::open(path).unwrap();
        store
            .execute_batch(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB);
                 CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB);",
            )
            .unwrap();
        let root_id = blob_id(root_byte);
        let metadata = format!(
            r#"{{"agentId":"{CURSOR_CONVERSATION}","latestRootBlobId":"{root_id}","createdAt":1773403200000}}"#
        );
        store
            .execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('0', ?1)",
                [hex_text(&metadata)],
            )
            .unwrap();
        let mut root = Vec::new();
        for (byte, message) in messages {
            root.extend_from_slice(&[0x0a, 0x20]);
            root.extend_from_slice(&[*byte; 32]);
            store
                .execute(
                    "INSERT OR IGNORE INTO blobs (id, data) VALUES (?1, ?2)",
                    rusqlite::params![blob_id(*byte), serde_json::to_vec(message).unwrap()],
                )
                .unwrap();
        }
        store
            .execute(
                "INSERT OR IGNORE INTO blobs (id, data) VALUES (?1, ?2)",
                rusqlite::params![root_id, root],
            )
            .unwrap();
    }

    fn user(text: &str) -> serde_json::Value {
        serde_json::json!({"role":"user","content":[{"type":"text","text":format!("<user_query>{text}</user_query>")}]})
    }

    fn assistant(text: &str) -> serde_json::Value {
        serde_json::json!({"role":"assistant","content":[
            {"type":"reasoning","text":format!("thinking about {text}")},
            {"type":"text","text":text},
            {"type":"tool-call","toolName":"Shell","toolCallId":format!("call-{text}"),"args":{"command":"ls"}}
        ]})
    }

    fn first_turns() -> Vec<(u8, serde_json::Value)> {
        vec![
            (0x11, user("do work")),
            (0x12, assistant("first answer")),
            (0x13, user("Briefly inform the user about the task result")),
            (0x14, assistant("auto answer")),
        ]
    }

    fn later_turns() -> Vec<(u8, serde_json::Value)> {
        let mut turns = first_turns();
        turns.push((0x15, user("keep going")));
        turns.push((0x16, assistant("second answer")));
        turns
    }

    fn ship_cursor(
        conn: &mut rusqlite::Connection,
        path: &Path,
        batch: u64,
    ) -> Vec<StorageV2Envelope> {
        let mut shipped = Vec::new();
        for _ in 0..10_000 {
            match super::super::cursor_store_source::prepare_next_cursor_envelope_outcome_with_limit(
                conn,
                &crate::storage_v2_shipper::tests::capabilities(),
                path,
                batch,
            )
            .unwrap()
            {
                CursorPreparationOutcome::Envelope(prepared) => {
                    pending_source_envelope::acknowledge_and_delete(
                        conn,
                        prepared.source_epoch,
                        &prepared.envelope.expected_envelope_id,
                        prepared.range_start,
                        prepared.range_end,
                    )
                    .unwrap();
                    shipped.push(prepared.envelope);
                }
                CursorPreparationOutcome::Continue => continue,
                CursorPreparationOutcome::Current | CursorPreparationOutcome::WaitingOnClaim => {
                    return shipped;
                }
            }
        }
        panic!("Cursor store never finished shipping");
    }

    /// Rebuild every envelope from what the host holds and compare.
    fn cursor_rebuild_mismatches(envelopes: &[StorageV2Envelope]) -> Vec<String> {
        let mut problems = Vec::new();
        for (index, envelope) in envelopes.iter().enumerate() {
            let stored: Vec<&StorageV2Envelope> = envelopes[..=index]
                .iter()
                .filter(|earlier| earlier.source_epoch == envelope.source_epoch)
                .collect();
            let rebuilt = match rebuild_cursor_envelope(&stored, envelope) {
                Ok(rebuilt) => rebuilt,
                Err(error) => {
                    problems.push(format!("{}: {error:#}", envelope.range_start));
                    continue;
                }
            };
            problems.extend(compare_rebuilt(envelope, &rebuilt));
        }
        problems
    }

    struct CursorCase {
        envelopes: Vec<StorageV2Envelope>,
    }

    /// Ship a store, grow it by one turn, ship again. `managed` binds it to a
    /// launch with hook receipts whose last turn failed.
    fn run_cursor_case(batch: u64, prompt_history: bool, managed: bool) -> CursorCase {
        let _guard = crate::console_adapter::agent_state_guard();
        let dir = tempfile::tempdir().unwrap();
        let store_dir = dir
            .path()
            .join("cursor/chats/workspace")
            .join(CURSOR_CONVERSATION);
        fs::create_dir_all(&store_dir).unwrap();
        fs::write(
            store_dir.join("meta.json"),
            serde_json::to_vec(&serde_json::json!({
                "schemaVersion": 1,
                "createdAtMs": 1_773_403_200_000_i64,
                "updatedAtMs": 1_773_403_260_000_i64,
                "cwd": "/tmp/longhouse-test/cursor-project",
            }))
            .unwrap(),
        )
        .unwrap();
        if prompt_history {
            fs::write(
                store_dir.join("prompt_history.json"),
                serde_json::to_vec(&serde_json::json!(["do work", "keep going", "unrelated"]))
                    .unwrap(),
            )
            .unwrap();
        }
        let helm = dir.path().join("longhouse/managed-local/cursor-helm");
        if managed {
            fs::create_dir_all(helm.join("binding-probes")).unwrap();
            fs::create_dir_all(helm.join("hook-events")).unwrap();
            fs::write(
                helm.join("binding-probes")
                    .join(format!("{MANAGED_SESSION}.json")),
                serde_json::to_vec(&serde_json::json!({
                    "schema_version": 2,
                    "provider": "cursor",
                    "status": "observed",
                    "session_id": MANAGED_SESSION,
                    "thread_id": "thread-main",
                    "launch_id": "launch-1",
                    "conversation_uuid": CURSOR_CONVERSATION,
                    // The managed session ran this conversation before: its
                    // first envelope opens with a reset boundary.
                    "previous_conversation_uuids": ["5f0e1d2c-3b4a-4968-8776-655443322110"],
                    "hook_observed_at": "2026-07-17T00:00:00Z"
                }))
                .unwrap(),
            )
            .unwrap();
        }
        let write_receipts = |rows: &[serde_json::Value]| {
            if !managed {
                return;
            }
            let text = rows
                .iter()
                .map(|row| row.to_string())
                .collect::<Vec<_>>()
                .join("\n");
            fs::write(
                helm.join("hook-events")
                    .join(format!("{MANAGED_SESSION}.ndjson")),
                format!("{text}\n"),
            )
            .unwrap();
        };
        let receipt = |event: &str, generation: &str, payload: serde_json::Value| {
            let mut payload = payload;
            payload["generation_id"] = serde_json::json!(generation);
            serde_json::json!({
                "conversation_id": CURSOR_CONVERSATION,
                "event": event,
                "launch_id": "launch-1",
                "observed_at": "2026-07-17T00:01:00Z",
                "payload": payload,
            })
        };
        let first_receipts = vec![
            receipt(
                "beforeSubmitPrompt",
                "g1",
                serde_json::json!({"prompt":"do work"}),
            ),
            receipt(
                "afterAgentResponse",
                "g1",
                serde_json::json!({"text":"first answer"}),
            ),
            receipt("stop", "g1", serde_json::json!({"status":"completed"})),
        ];
        let mut later_receipts = first_receipts.clone();
        later_receipts.push(receipt(
            "beforeSubmitPrompt",
            "g2",
            serde_json::json!({"prompt":"keep going"}),
        ));
        later_receipts.push(receipt("stop", "g2", serde_json::json!({"status":"error"})));

        let path = store_dir.join("store.db");
        let envelopes = temp_env::with_vars(
            [
                (
                    "CURSOR_HOME",
                    Some(dir.path().join("cursor").into_os_string()),
                ),
                (
                    "XDG_CONFIG_HOME",
                    Some(dir.path().join("config").into_os_string()),
                ),
                (
                    "LONGHOUSE_HOME",
                    Some(dir.path().join("longhouse").into_os_string()),
                ),
            ],
            || {
                let mut conn = open_db(Some(&dir.path().join("state.db"))).unwrap();
                write_cursor_store(&path, 0xa1, &first_turns());
                write_receipts(&first_receipts);
                let mut envelopes = ship_cursor(&mut conn, &path, batch);
                write_cursor_store(&path, 0xa2, &later_turns());
                write_receipts(&later_receipts);
                envelopes.extend(ship_cursor(&mut conn, &path, batch));
                envelopes
            },
        );
        CursorCase { envelopes }
    }

    #[test]
    fn host_rebuilds_cursor_store_envelopes_from_stored_records_and_witness() {
        let mut checked = 0;
        let mut failures = Vec::new();
        for (batch, prompt_history, managed) in [
            (u64::MAX / 2, false, false),
            (1, false, false),
            (u64::MAX / 2, true, false),
            (u64::MAX / 2, true, true),
            (1, true, true),
        ] {
            let case = run_cursor_case(batch, prompt_history, managed);
            assert!(
                case.envelopes.iter().any(|envelope| envelope.render.is_some()),
                "case (batch {batch}, history {prompt_history}, managed {managed}) rendered nothing"
            );
            let witness_bytes = case
                .envelopes
                .iter()
                .map(|envelope| {
                    serde_json::to_vec(&envelope.facts[0].payload)
                        .unwrap()
                        .len()
                })
                .max()
                .unwrap();
            eprintln!(
                "cursor case batch={batch} history={prompt_history} managed={managed}: {} envelopes, largest witness {witness_bytes} bytes",
                case.envelopes.len()
            );
            checked += case.envelopes.len();
            for problem in cursor_rebuild_mismatches(&case.envelopes) {
                failures.push(format!(
                    "(batch {batch}, history {prompt_history}, managed {managed}) {problem}"
                ));
            }
        }
        assert!(checked > 0);
        assert!(failures.is_empty(), "{}", failures.join("\n"));
    }

    #[test]
    fn cursor_rebuild_depends_on_the_witness_answers() {
        // The managed case exercises suppression, the failure row, the thread
        // and the typed-prompt labels: blanking the witness answers must break
        // the rebuild, or the check above proves nothing.
        let case = run_cursor_case(u64::MAX / 2, true, true);
        assert!(
            case.envelopes
                .iter()
                .any(|envelope| envelope.facts[0].payload["cursor"]
                    .get("previous_provider_session_id")
                    .is_some()),
            "the managed case must ship a reset boundary"
        );
        assert!(cursor_rebuild_mismatches(&case.envelopes).is_empty());
        // Each answer on its own is load-bearing.
        for field in [
            "typed_prompts",
            "suppressed",
            "abandoned",
            "turn_failure",
            "thread_id",
            "previous_provider_session_id",
        ] {
            let mut stripped = case.envelopes.clone();
            for envelope in &mut stripped {
                envelope.facts[0].payload["cursor"]
                    .as_object_mut()
                    .unwrap()
                    .remove(field);
            }
            assert!(
                !cursor_rebuild_mismatches(&stripped).is_empty(),
                "stripping {field} from the witness did not change the rebuild"
            );
        }
    }
}
