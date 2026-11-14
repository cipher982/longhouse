//! What a Runtime Host builds from what the Machine Agent shipped.
//!
//! The host holds, per source epoch, the raw records of every accepted
//! envelope and each envelope's `capture.witness` fact. Rebuilding an
//! envelope's render parses the stored bytes with the captured witness and no
//! access to the laptop: no filesystem, git, clock or local state. The Phase 2
//! check of `host-normalized-transcripts.md` holds this equal to the render
//! the laptop shipped.

use super::render::{render_records_for_batch, session_facts, storage_v2_media_refs};
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

pub(crate) struct RebuiltRender {
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
    let source = MemorySource::at(witness.identity.clone(), stored.base, stored.bytes.clone());
    let parse_result = parser::parse_source(
        &source,
        witness,
        envelope.range_start,
        Some(envelope.range_end),
        Some(&envelope.provider),
    )?;
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
        records: render_records,
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
        records: render_records,
        session,
        media: storage_v2_media_refs(&media_objects),
        facts,
    })
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
                    .unwrap_or(serde_json::json!([])),
                serde_json::to_value(&rebuilt.records).unwrap(),
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
        let problems = opencode_mismatches(&envelopes);
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
                    for problem in rebuild_mismatches(&shipped) {
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
}
