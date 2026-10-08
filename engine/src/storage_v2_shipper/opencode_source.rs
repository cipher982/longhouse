//! The OpenCode database source: settle detection, the session walk and
//! per-range provider facts and media.

use super::*;

/// OpenCode databases this process last found with nothing left to ship, and
/// what it depended on then (see `opencode_rest_key`), by path, plus when a row
/// still being written will be given up on: a walk that held one back is not at
/// rest past that moment, whatever the file did. Kept in memory: after a
/// restart the first pass reads the database once, as it always did.
pub(super) static OPENCODE_AT_REST: std::sync::LazyLock<
    std::sync::Mutex<HashMap<PathBuf, (String, Option<i64>)>>,
> = std::sync::LazyLock::new(|| std::sync::Mutex::new(HashMap::new()));

/// What a walk over an OpenCode database depended on outside the database's own
/// records: the file's stamp (any change to a session, however silent, changes
/// the file), the managed-state files that bind a session to a Longhouse one,
/// and the build; and, from the shipper database rather than the file, how far
/// the host has received each session (a lane rewound with no write to the
/// OpenCode file). `None` when something was written too recently to vouch for.
pub(super) fn opencode_rest_key(conn: &Connection, db_path: &Path) -> Result<Option<String>> {
    let (Some(stamp), Some(managed)) = (
        wal_database_stamp(db_path),
        opencode_db::managed_state_signature(),
    ) else {
        return Ok(None);
    };
    // The import scope decides which sessions a walk may read, so widening it
    // must end the rest: a database that was at rest under the old scope has
    // sessions the new one wants.
    Ok(Some(format!(
        "{stamp}|{managed}|{}|{}|{}",
        source_epoch::active_lane_fingerprint(conn, "opencode")?,
        crate::build_identity::COMMIT,
        crate::config::import_scope().signature()
    )))
}

/// Whether the walk that last found nothing left to ship saw this same database
/// and binding evidence, and nothing has been queued since, blocked or not.
pub(super) fn opencode_database_is_settled(
    conn: &Connection,
    db_path: &Path,
    rest_key: Option<&str>,
) -> Result<bool> {
    let Some(rest_key) = rest_key else {
        return Ok(false);
    };
    let now_ms = chrono::Utc::now().timestamp_millis();
    let at_rest = OPENCODE_AT_REST.lock().is_ok_and(|rests| {
        rests.get(db_path).is_some_and(|(known, wake_ms)| {
            known == rest_key && wake_ms.is_none_or(|wake_ms| now_ms < wake_ms)
        })
    });
    Ok(at_rest && !pending_source_envelope::exists_for_provider(conn, "opencode")?)
}

/// Walking an OpenCode database reads every session's every message and part.
/// The scan does that to learn that nothing moved, so a database that has not
/// moved since a walk found nothing to ship is not walked again.
pub(crate) fn prepare_next_opencode_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
) -> Result<Option<PreparedStorageV2Envelope>> {
    // Stamped before anything is read, so a write that lands while the walk
    // runs is seen as a change by the next one.
    let rest_key = opencode_rest_key(conn, db_path)?;
    if opencode_database_is_settled(conn, db_path, rest_key.as_deref())? {
        return Ok(None);
    }
    let mut waited = false;
    let mut wake_ms = None;
    let unread_before = crate::dir_cache::unread_files();
    let prepared = walk_opencode_database(conn, capabilities, db_path, &mut waited, &mut wake_ms)?;
    // A session held back for its managed binding is waiting on the clock, not
    // on the database, so it is not a walk that found nothing to ship. Nor is
    // one that could not read a managed-state file: the key vouches for what a
    // stat can see, and a file made readable again (a `chmod`) changes none of
    // it, so a walk that missed the file must be repeated until it does not.
    let saw_every_binding = crate::dir_cache::unread_files() == unread_before;
    if let (None, false, true, Some(rest_key)) = (&prepared, waited, saw_every_binding, rest_key) {
        if let Ok(mut rests) = OPENCODE_AT_REST.lock() {
            rests.insert(db_path.to_path_buf(), (rest_key, wake_ms));
        }
    }
    Ok(prepared)
}

pub(super) fn walk_opencode_database(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    db_path: &Path,
    waited: &mut bool,
    wake_ms: &mut Option<i64>,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let canonical_path = stable_source_path(db_path);
    let path_text = canonical_path.to_string_lossy();
    // One database holds every OpenCode session the machine ever ran, so the
    // machine's import scope is applied here, per session, rather than at
    // discovery. A session outside it is skipped before any of its records are
    // read or its source epoch opened.
    let import_scope = crate::config::import_scope();
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
            if !candidate.in_import_scope(&import_scope) {
                continue;
            }
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
            let stream = opencode_db::opencode_session_stream(
                db_path,
                &candidate.provider_session_id,
                chrono::Utc::now().timestamp_millis(),
            )?;
            if let Some(stream_wake_ms) = stream.next_wake_ms() {
                *wake_ms = Some(wake_ms.map_or(stream_wake_ms, |known| known.min(stream_wake_ms)));
            }
            let logical_len = u64::try_from(stream.records.len())
                .context("OpenCode stream has too many records")?;
            let managed_session_id = opencode_db::managed_longhouse_session_id_for_opencode(
                &candidate.provider_session_id,
            );
            // A session that only grew keeps its epoch: the stream's revision
            // chain still vouches for every record already shipped. The epoch
            // then vouches for the new tail too. Only a settled row that
            // changed, moved or vanished ends the epoch.
            let stored_revision =
                source_epoch::active_source_revision(conn, "opencode", &opaque_source_id)?;
            let revision = stream.continuing_revision(stored_revision.as_deref());
            // An epoch that never recorded a revision cannot vouch for where
            // its records sit, so it is replaced like one whose chain broke.
            let unvouched = stored_revision.is_none()
                && source_epoch::active_source_epoch(conn, "opencode", &opaque_source_id)?
                    .is_some();
            let resolution = source_epoch::observe_source(
                conn,
                "opencode",
                &opaque_source_id,
                "opencode-sqlite-session-v1",
                logical_len,
                SourceLane::Durable,
                0,
                Some(&revision),
                managed_session_id.as_deref(),
                if unvouched {
                    SourceChangeHint::Rewrite
                } else {
                    SourceChangeHint::None
                },
            )?;
            let revision = {
                let current = stream.revision();
                if revision != current {
                    source_epoch::set_source_revision(conn, resolution.source_epoch, &current)?;
                }
                current
            };
            let range_start =
                source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
            if range_start >= logical_len {
                continue;
            }
            let (range_end, raw_bytes) = bounded_record_ordinal_end(
                &stream.records,
                range_start,
                capabilities.max_records,
                capabilities.max_raw_record_bytes,
            )?;
            let opencode_db::OpenCodeParse {
                result: parse_result,
                line_ordinals,
            } = opencode_db::parse_opencode_stream(db_path, &stream)?;
            let source_ordinals: Vec<(u64, u64)> = parse_result
                .source_lines
                .iter()
                .zip(&line_ordinals)
                .map(|(line, ordinal)| (line.source_offset, *ordinal))
                .collect();
            let managed_session_id = managed_session_id.or_else(|| {
                opencode_db::managed_longhouse_session_id_for_opencode(
                    &candidate.provider_session_id,
                )
            });
            if managed_session_id.is_none()
                && opencode_db::managed_binding_may_be_pending(&parse_result.metadata)
            {
                tracing::debug!(
                    provider_session_id = candidate.provider_session_id,
                    "Waiting for OpenCode managed-session rollover binding"
                );
                *waited = true;
                continue;
            }
            let resolution = if managed_session_id.is_some()
                && resolution.bound_session_id.as_deref() != managed_session_id.as_deref()
            {
                source_epoch::observe_source(
                    conn,
                    "opencode",
                    &opaque_source_id,
                    "opencode-sqlite-session-v1",
                    logical_len,
                    SourceLane::Durable,
                    range_start,
                    Some(&revision),
                    managed_session_id.as_deref(),
                    SourceChangeHint::None,
                )?
            } else {
                resolution
            };
            let session_id = managed_session_id
                .clone()
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
            let selected = &stream.records[start..end];
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
            let previous_provider_session_id = if range_start == 0 {
                if let Some(managed_session_id) = managed_session_id.as_deref() {
                    source_epoch::previous_provider_session_id(
                        conn,
                        resolution.source_epoch,
                        "opencode",
                        managed_session_id,
                    )?
                    .or(FileState::new(conn).previous_provider_session_id(
                        &candidate.source_key,
                        managed_session_id,
                        "opencode",
                    )?)
                } else {
                    None
                }
            } else {
                None
            };
            source_epoch::record_provider_session_id(
                conn,
                resolution.source_epoch,
                &candidate.provider_session_id,
            )?;
            let mut render_records = opencode_render_records_for_range(
                &parse_result,
                &source_ordinals,
                range_start,
                range_end,
            )?;
            if previous_provider_session_id
                .as_deref()
                .is_some_and(|previous| previous != candidate.provider_session_id.as_str())
            {
                insert_conversation_reset_boundary(
                    &mut render_records,
                    resolution.source_epoch,
                    range_start,
                    &resolution.opened_at,
                    &session_id,
                    previous_provider_session_id.as_deref().unwrap_or_default(),
                    &candidate.provider_session_id,
                )?;
            }
            let event_count = render_records.len();
            let render_generation = render_generation_id(session_uuid);
            let session = session_facts(
                "opencode",
                &parse_result.metadata,
                &render_records,
                &resolution,
            )?;
            let media_objects = opencode_media_objects_for_range(
                &parse_result,
                &source_ordinals,
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
                    facts: opencode_provider_facts_for_range(
                        &parse_result,
                        &source_ordinals,
                        range_start,
                        range_end,
                    )?,
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

/// `source_ordinals` pairs each part's source offset with the ordinal of its
/// raw record, in offset order.
pub(super) fn opencode_provider_facts_for_range(
    parse_result: &ParseResult,
    source_ordinals: &[(u64, u64)],
    range_start: u64,
    range_end: u64,
) -> Result<Vec<StorageV2ProviderFact>> {
    let source_ordinals: HashMap<u64, u64> = source_ordinals.iter().copied().collect();
    let mut facts = Vec::new();
    for fact in &parse_result.provider_facts {
        let ordinal = if fact.kind == "delegation.metadata" && fact.source_offset == 0 {
            // Session metadata facts are anchored to the synthetic session
            // record; part facts with offset zero still use the source map.
            0
        } else {
            *source_ordinals
                .get(&fact.source_offset)
                .context("OpenCode provider fact is not covered by a raw part record")?
        };
        if ordinal < range_start
            || ordinal >= range_end
            || facts.iter().any(|existing: &StorageV2ProviderFact| {
                existing.kind == fact.kind && existing.source_position == ordinal
            })
        {
            continue;
        }
        facts.push(StorageV2ProviderFact {
            kind: fact.kind.clone(),
            at: fact.at.to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            source_position: ordinal,
            payload: fact.payload.clone(),
        });
    }
    Ok(facts)
}

pub(super) fn opencode_media_objects_for_range(
    parse_result: &ParseResult,
    source_ordinals: &[(u64, u64)],
    range_start: u64,
    range_end: u64,
) -> Result<Vec<ParsedMediaObject>> {
    let source_ordinals: HashMap<u64, u64> = source_ordinals.iter().copied().collect();
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
