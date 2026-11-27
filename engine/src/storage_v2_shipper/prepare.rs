//! Preparing the next storage-v2 envelope for a file source: batch sizing by
//! lane, Claude startup-metadata handling and blocked-source retirement.

use super::*;

/// Open a replacement epoch for a file source so its next ship re-parses it
/// whole.
///
/// Rewinding the local lane alone is not enough: the host tracks
/// `accepted_through` per source epoch and ignores a range it has already
/// accepted, so a rewound client re-sends bytes that land nowhere. Minting a
/// replacement epoch is what the Cursor path does when its render revision
/// changes, and it is what makes the host treat the same bytes as new
/// material. Events still deduplicate by hash, so history is refreshed rather
/// than duplicated.
///
/// Returns the new epoch, or `None` when the source was never shipped and a
/// normal ship will pick it up anyway.
pub(crate) fn replay_file_source(
    conn: &mut Connection,
    path: &Path,
    provider: &str,
) -> Result<Option<Uuid>> {
    let canonical_path = stable_source_path(path);
    let path_text = canonical_path.to_string_lossy();
    let opaque_source_id = opaque_source_id(&path_text);
    if source_epoch::active_source_epoch(conn, provider, &opaque_source_id)?.is_none() {
        return Ok(None);
    }
    let resolution = source_epoch::observe_file(
        conn,
        provider,
        &opaque_source_id,
        path,
        SourceLane::Durable,
        0,
        None,
        None,
        SourceChangeHint::Rewrite,
    )?;
    Ok(Some(resolution.source_epoch))
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

pub(super) fn prepare_next_envelope_with_limit(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    session_id_override: Option<&str>,
    maximum_batch_bytes: usize,
) -> Result<Option<PreparedStorageV2Envelope>> {
    prepare_next_envelope_or_hold(
        conn,
        capabilities,
        path,
        provider,
        session_id_override,
        maximum_batch_bytes,
        &mut None,
    )
}

/// How long a brand-new, unbound Antigravity transcript is held so its
/// launcher's binding can claim it first (see the hold below).
const ANTIGRAVITY_CLAIM_WINDOW: Duration = Duration::from_secs(5);

/// [`prepare_next_envelope_with_limit`], also saying when it held the source
/// back for a bounded claim window rather than finding nothing to ship. A held
/// source sets `hold` to how long until the window closes, so the caller can
/// come back then: nothing else is due to wake it if the file goes quiet.
pub(super) fn prepare_next_envelope_or_hold(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    provider: &str,
    session_id_override: Option<&str>,
    maximum_batch_bytes: usize,
    hold: &mut Option<Duration>,
) -> Result<Option<PreparedStorageV2Envelope>> {
    if provider.eq_ignore_ascii_case("codex") {
        if crate::compressed_source::is_compressed_transcript(path) {
            return prepare_compressed_codex_envelope(
                conn,
                capabilities,
                path,
                session_id_override,
                maximum_batch_bytes,
            );
        }
        // Codex compresses a rollout by writing `<path>.zst` and then removing
        // the plain file. The `.zst` event came while the plain file still owned
        // the source, so the removal is the moment its tail becomes readable.
        let compressed = compressed_sibling(path);
        if !path.exists() && compressed.exists() {
            return prepare_compressed_codex_envelope(
                conn,
                capabilities,
                &compressed,
                session_id_override,
                maximum_batch_bytes,
            );
        }
    }
    prepare_file_envelope(
        conn,
        capabilities,
        &FileSourceView::plain(path),
        provider,
        session_id_override,
        maximum_batch_bytes,
        hold,
    )
}

/// Where a file source's bytes are read from, and which source they are. They
/// differ only for a compressed Codex rollout, whose bytes come from a
/// decompressed copy while its identity stays the plain rollout it replaced.
pub(super) struct FileSourceView<'a> {
    read_path: &'a Path,
    identity_path: &'a Path,
    /// The incarnation to record instead of the read file's own: a fresh
    /// decompressed copy has a new inode but continues the same source.
    incarnation: Option<String>,
}

impl<'a> FileSourceView<'a> {
    pub(super) fn plain(path: &'a Path) -> Self {
        Self {
            read_path: path,
            identity_path: path,
            incarnation: None,
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn observe_view(
    conn: &mut Connection,
    provider: &str,
    opaque_source_id: &str,
    view: &FileSourceView<'_>,
    position: u64,
    source_revision: Option<&str>,
    bound_session_id: Option<&str>,
    change_hint: SourceChangeHint,
) -> Result<source_epoch::SourceEpochResolution> {
    match view.incarnation.as_deref() {
        None => source_epoch::observe_file(
            conn,
            provider,
            opaque_source_id,
            view.read_path,
            SourceLane::Durable,
            position,
            source_revision,
            bound_session_id,
            change_hint,
        ),
        Some(incarnation) => source_epoch::observe_source(
            conn,
            provider,
            opaque_source_id,
            incarnation,
            std::fs::metadata(view.read_path)?.len(),
            SourceLane::Durable,
            position,
            source_revision,
            bound_session_id,
            change_hint,
        ),
    }
}

/// A Codex rollout Codex compressed in place. Already shipped (the usual case:
/// live capture reads the plain file before Codex compresses it) is decided
/// from the zstd header without decompressing; otherwise the rollout is
/// decompressed into a cache and its unshipped bytes go out under the plain
/// path's source id, continuing that source's epoch. Task 1's content anchor
/// still guards the join.
fn prepare_compressed_codex_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    path: &Path,
    session_id_override: Option<&str>,
    maximum_batch_bytes: usize,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let identity_path = crate::compressed_source::uncompressed_path(path);
    if identity_path.exists() {
        // Mid-compression both exist; the plain file is the source until it goes.
        return Ok(None);
    }
    let canonical_identity = stable_source_path(&identity_path);
    let opaque_source_id = opaque_source_id(&canonical_identity.to_string_lossy());
    if let Some(pending) =
        pending_source_envelope::load_for_source(conn, "codex", &opaque_source_id)?
    {
        return pending_to_prepared(pending).map(Some);
    }
    let cache_dir = compressed_source_cache_dir()?;
    let active = load_active_codex_epoch(conn, &opaque_source_id)?;
    if let Some((_, position, _)) = &active {
        if crate::compressed_source::fully_shipped(path, *position)? {
            crate::compressed_source::evict(&canonical_identity, &cache_dir);
            return Ok(None);
        }
    }
    let copy = crate::compressed_source::materialize(path, &canonical_identity, &cache_dir)?;
    let view = FileSourceView {
        read_path: &copy,
        identity_path: &identity_path,
        incarnation: active.map(|(_, _, incarnation)| incarnation),
    };
    prepare_file_envelope(
        conn,
        capabilities,
        &view,
        "codex",
        session_id_override,
        maximum_batch_bytes,
        &mut None,
    )
}

fn compressed_sibling(path: &Path) -> PathBuf {
    let mut name = path.as_os_str().to_os_string();
    name.push(".zst");
    PathBuf::from(name)
}

/// Where decompressed copies of compressed rollouts are kept while their tail
/// ships; each is evicted once its source is fully shipped.
pub(super) fn compressed_source_cache_dir() -> Result<PathBuf> {
    Ok(crate::config::get_agent_dir()?.join("compressed-source-cache"))
}

fn load_active_codex_epoch(
    conn: &Connection,
    opaque_source_id: &str,
) -> Result<Option<(Uuid, u64, String)>> {
    let Some(epoch) = source_epoch::active_source_epoch(conn, "codex", opaque_source_id)? else {
        return Ok(None);
    };
    let Some(incarnation) =
        source_epoch::active_source_incarnation(conn, "codex", opaque_source_id)?
    else {
        return Ok(None);
    };
    let position = source_epoch::lane_position(conn, epoch, SourceLane::Durable)?;
    Ok(Some((epoch, position, incarnation)))
}

fn prepare_file_envelope(
    conn: &mut Connection,
    capabilities: &StorageV2Capabilities,
    view: &FileSourceView<'_>,
    provider: &str,
    session_id_override: Option<&str>,
    maximum_batch_bytes: usize,
    hold: &mut Option<Duration>,
) -> Result<Option<PreparedStorageV2Envelope>> {
    let path = view.read_path;
    let canonical_path = stable_source_path(view.identity_path);
    let path_text = canonical_path.to_string_lossy();
    let opaque_source_id = opaque_source_id(&path_text);
    let mut durable_session_id = session_id_override.map(str::to_string);
    if provider.eq_ignore_ascii_case("pi") {
        let claims = crate::turn_claims::default_registry()?.list_all_shared()?;
        match crate::pi_session::bind_discovered_source(conn, &canonical_path, &claims)? {
            crate::pi_session::SourceOwnership::Managed(session_id) => {
                if durable_session_id
                    .as_deref()
                    .is_some_and(|override_id| override_id != session_id)
                {
                    anyhow::bail!(
                        "Pi source ownership conflicts with the managed session override"
                    );
                }
                durable_session_id = Some(session_id);
            }
            crate::pi_session::SourceOwnership::Pending => {
                if let Some(session_id) = durable_session_id.as_deref() {
                    let Ok(provider_thread_id) =
                        crate::pi_session::read_session_header_id(&canonical_path)
                    else {
                        return Ok(None);
                    };
                    crate::pi_session::bind_source_for_thread(
                        conn,
                        &canonical_path,
                        session_id,
                        &provider_thread_id,
                    )?;
                } else {
                    return Ok(None);
                }
            }
            crate::pi_session::SourceOwnership::Unclaimed => {
                if let Some(session_id) = durable_session_id.as_deref() {
                    let Ok(provider_thread_id) =
                        crate::pi_session::read_session_header_id(&canonical_path)
                    else {
                        return Ok(None);
                    };
                    crate::pi_session::bind_source_for_thread(
                        conn,
                        &canonical_path,
                        session_id,
                        &provider_thread_id,
                    )?;
                }
            }
        }
    }
    if provider.eq_ignore_ascii_case("omp") {
        match crate::omp_session::bind_discovered_source(conn, &canonical_path)? {
            crate::omp_session::SourceOwnership::Managed(session_id) => {
                if durable_session_id
                    .as_deref()
                    .is_some_and(|override_id| override_id != session_id)
                {
                    anyhow::bail!(
                        "OMP source ownership conflicts with the managed session override"
                    );
                }
                durable_session_id = Some(session_id);
            }
            crate::omp_session::SourceOwnership::Pending => {
                // A managed wake's session override is not native identity
                // confirmation. Keep the source held until its provider header
                // has been verified against the exact reported binding.
                return Ok(None);
            }
            crate::omp_session::SourceOwnership::Unclaimed => {
                if let Some(session_id) = durable_session_id.as_deref() {
                    let Ok(native_id) = crate::omp_session::read_session_header(&canonical_path)
                    else {
                        return Ok(None);
                    };
                    crate::omp_session::bind_source_for_thread(
                        conn,
                        &canonical_path,
                        session_id,
                        &native_id.native_id,
                    )?;
                }
            }
        }
    }
    // Read the binding together with the thread it was made for. A binding that
    // names this transcript's own thread was written deliberately for it; one
    // that names something else was inherited, and cannot be trusted to say who
    // owns this file.
    let binding = crate::state::session_binding::SessionBinding::new(conn)
        .get_with_thread_for_provider(&path_text, provider)?;
    let binding_thread = binding.as_ref().and_then(|(_, thread)| thread.clone());
    if durable_session_id.is_none() {
        durable_session_id = binding.map(|(session_id, _)| session_id);
    }
    if let Some(pending) =
        pending_source_envelope::load_for_source(conn, provider, &opaque_source_id)?
    {
        if !retire_empty_blocked_source_if_safe(conn, provider, &pending)? {
            let oversized_render_rejected = pending.raw_bytes > maximum_batch_bytes as u64
                && pending.block_kind.as_deref() == Some("envelope_rejected")
                && pending
                    .block_detail
                    .as_deref()
                    .is_some_and(|d| d.contains("render object exceeds"));
            let oversized_unattempted = pending.raw_bytes > maximum_batch_bytes as u64
                && pending.attempt_count == 0
                && maximum_batch_bytes < MAX_RAW_BATCH_BYTES;
            // An envelope prepared before this binary decided the Cursor
            // projection is raw-only still carries a render, and shipping it
            // would take the current generation back from the store. Drop the
            // unattempted one and re-prepare from the same bytes.
            let obsolete_unattempted_cursor_render = pending.attempt_count == 0
                && cursor_projection_is_raw_only(provider, path)
                && pending_carries_render(&pending)?;
            if oversized_render_rejected {
                if !pending_source_envelope::discard_after_cursor_resync(
                    conn,
                    pending.source_epoch,
                    &pending.envelope_id,
                )? {
                    return pending_to_prepared(pending).map(Some);
                }
            } else if !(oversized_unattempted || obsolete_unattempted_cursor_render)
                || !pending_source_envelope::discard_unattempted(
                    conn,
                    pending.source_epoch,
                    &pending.envelope_id,
                )?
            {
                return pending_to_prepared(pending).map(Some);
            }
        }
    }
    if durable_session_id.is_none() {
        if let Some(conversation_uuid) = cursor_agent_transcript_conversation_id(provider, path) {
            let source_was_seen =
                source_epoch::active_source_incarnation(conn, "cursor", &opaque_source_id)?
                    .is_some();
            let source_is_fresh = path
                .metadata()
                .and_then(|metadata| metadata.modified())
                .ok()
                .and_then(|modified| modified.elapsed().ok())
                .is_some_and(|age| age <= Duration::from_secs(5));
            let launch_reservation_may_be_pending =
                crate::cursor_launch_binding::launch_reservation_may_be_pending()?;
            match crate::cursor_launch_binding::launch_binding_state_for_conversation(
                &conversation_uuid,
            )? {
                crate::cursor_launch_binding::CursorLaunchBindingState::Managed(binding) => {
                    durable_session_id = Some(binding.session_id);
                }
                crate::cursor_launch_binding::CursorLaunchBindingState::Pending => {
                    return Ok(None);
                }
                crate::cursor_launch_binding::CursorLaunchBindingState::Unclaimed
                    if should_wait_for_unclaimed_cursor_source(
                        source_was_seen,
                        launch_reservation_may_be_pending,
                        source_is_fresh
                            && crate::cursor_launch_binding::reset_binding_may_be_pending(
                                &conversation_uuid,
                            )?,
                    ) =>
                {
                    // Cursor writes the agent-transcript projection before its
                    // launch probe or completion wake can persist the managed
                    // owner. Hold only that bounded claim window so the first
                    // envelope cannot freeze a duplicate Shadow identity.
                    return Ok(None);
                }
                crate::cursor_launch_binding::CursorLaunchBindingState::Unclaimed => {}
            }
        }
    }
    // A conversation another Antigravity conversation recorded spawning is a
    // worker, never the conversation a Longhouse launch is waiting to bind, so
    // neither the Console claim nor the claim window below applies to it. The
    // parent's record lands before the child's transcript exists.
    let antigravity_unclaimed_root = provider.eq_ignore_ascii_case("antigravity")
        && durable_session_id.is_none()
        && crate::antigravity_subagents::child_of(&canonical_path).is_none();
    if antigravity_unclaimed_root {
        match crate::antigravity_acp::bind_discovered_source(
            conn,
            &canonical_path,
            &crate::turn_claims::default_registry()?.list_all_shared()?,
            &crate::config::get_agent_dir()?,
        )? {
            crate::antigravity_acp::SourceOwnership::Managed(session_id) => {
                durable_session_id = Some(session_id);
            }
            crate::antigravity_acp::SourceOwnership::Pending => return Ok(None),
            crate::antigravity_acp::SourceOwnership::Unclaimed => {}
        }
    }
    if antigravity_unclaimed_root && durable_session_id.is_none() {
        // Antigravity creates the transcript immediately before its first
        // synchronous hook records the managed binding. Avoid freezing a new
        // unmanaged envelope in that short discovery race. Only a source that
        // has never shipped can be in that race: once its first envelope is
        // out, its identity is settled, and holding every later write back
        // only delayed live work until the file went quiet (when no watcher
        // event is left to wake it). The caller retries when the window
        // closes. Exact pending retries above remain authoritative.
        let never_shipped =
            source_epoch::active_source_epoch(conn, provider, &opaque_source_id)?.is_none();
        let remaining = path
            .metadata()
            .and_then(|metadata| metadata.modified())
            .ok()
            .and_then(|modified| modified.elapsed().ok())
            .and_then(|age| ANTIGRAVITY_CLAIM_WINDOW.checked_sub(age));
        if let (true, Some(remaining)) = (never_shipped, remaining) {
            *hold = Some(remaining + Duration::from_millis(250));
            return Ok(None);
        }
    }
    let session_id_override = durable_session_id.as_deref();
    let legacy_offset = legacy_offset_for_adoption(
        conn,
        provider,
        &opaque_source_id,
        &path_text,
        &canonical_path,
    )?;
    if !path.exists() {
        return Ok(None);
    }
    let framing = if provider.eq_ignore_ascii_case("antigravity")
        && path
            .extension()
            .and_then(|ext| ext.to_str())
            .map_or(false, |ext| ext.eq_ignore_ascii_case("json"))
    {
        RawSourceFraming::WholeDocument
    } else {
        RawSourceFraming::LfDelimited
    };
    let source_revision =
        if provider.eq_ignore_ascii_case("omp") || provider.eq_ignore_ascii_case("pi") {
            pi_lineage_source_revision(path)?
        } else if is_cursor_agent_transcript_path(provider, path) {
            cursor_agent_transcript_source_revision(path)?
        } else if provider.eq_ignore_ascii_case("claude") {
            claude_subagent_sidecar_revision(path)?
        } else if provider.eq_ignore_ascii_case("antigravity")
            && framing == RawSourceFraming::WholeDocument
        {
            Some(hash_file(path)?)
        } else {
            None
        };
    let resolution = observe_view(
        conn,
        provider,
        &opaque_source_id,
        view,
        legacy_offset,
        source_revision.as_deref(),
        session_id_override,
        SourceChangeHint::None,
    )?;
    let position = source_epoch::lane_position(conn, resolution.source_epoch, SourceLane::Durable)?;
    // Identity and length catch a replaced or shortened file. A file rewritten
    // in place to the same or a greater length passes both, and the cursor
    // would go on reading new bytes as if they followed the old ones. The
    // anchor recorded when the range ending here was prepared says what the
    // bytes before the cursor were; if they changed, ship the source again
    // from the start under a new epoch. Checked before the "nothing new" exit
    // so a same-length rewrite is seen without waiting for an append.
    if framing == RawSourceFraming::LfDelimited
        && position > 0
        && rewritten_under_cursor(conn, path, resolution.source_epoch, position)?
    {
        tracing::warn!(
            path = %path.display(),
            provider,
            source_epoch = %resolution.source_epoch,
            position,
            "Source was rewritten under its cursor; shipping it again under a new epoch"
        );
        observe_view(
            conn,
            provider,
            &opaque_source_id,
            view,
            0,
            source_revision.as_deref(),
            session_id_override,
            SourceChangeHint::Rewrite,
        )?;
        return prepare_file_envelope(
            conn,
            capabilities,
            view,
            provider,
            session_id_override,
            maximum_batch_bytes,
            hold,
        );
    }
    let source_len = std::fs::metadata(path)?.len();
    if position >= source_len {
        return Ok(None);
    }
    // The raw batch and the parsed events are separate reads of a file the
    // provider may rewrite between them, which would publish raw bytes from one
    // version with rendered events from another. Stamp the source here and
    // re-check it after the parse; a change means this batch describes no single
    // version, so decline it and let the next tick read a consistent one.
    let source_stamp_before = source_stamp(path)?;
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
    // Parse exactly the captured range. Reading the whole remaining file made
    // the raw batch and the rendered events two independent reads of a mutable
    // file: unbounded work proportional to the file, and a rewrite between them
    // could publish bytes from one version with events from another.
    // Parse with a witness that records every answer it gives, dated by the
    // epoch's opening time, and ship those answers beside the bytes: a host
    // holding only this envelope then parses identically.
    let epoch_opened_at = DateTime::parse_from_rfc3339(&resolution.opened_at)
        .context("source epoch opened_at is not RFC 3339")?
        .with_timezone(&Utc);
    let file_source = crate::pipeline::source::FileSource::named(path, view.identity_path);
    // A record with no clock of its own takes the last clock before it. The
    // range ending here recorded where its parse ended; the first range
    // starts from the epoch's opening.
    let starting_clock = if position > 0 {
        source_epoch::last_clock_at(conn, resolution.source_epoch, position)?
    } else {
        None
    }
    .unwrap_or(epoch_opened_at);
    let capturing =
        crate::pipeline::source::CapturingWitness::new(view.identity_path, starting_clock);
    let mut parse_result = parser::parse_source(
        &file_source,
        &capturing,
        position,
        Some(raw_batch.range_end),
        Some(provider),
    )?;
    let mut captured_witness =
        capturing.finish(crate::pipeline::source::SourceBytes::identity(&file_source).clone());
    if is_cursor_agent_transcript_path(provider, path)
        && parse_result.metadata.started_at.is_none()
        && parse_result.events.is_empty()
    {
        tracing::warn!(
            path = %path.display(),
            "Cursor transcript has no source clock; archiving raw-only with the stable source epoch clock"
        );
    }
    // A binding is "exact" when it names the very thread this transcript
    // records. That is what separates a fork Longhouse started — it bound the
    // child's path to the child's session at fork time — from one a managed
    // parent left on the next file that appeared.
    parse_result.metadata.managed_binding_is_exact = binding_thread
        .as_deref()
        .is_some_and(|thread| thread == parse_result.metadata.session_id);
    captured_witness.managed_binding_is_exact = parse_result.metadata.managed_binding_is_exact;
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
    if source_stamp(path)? != source_stamp_before {
        tracing::info!(
            path = %path.display(),
            "Source changed between the raw read and the parse; declining this batch"
        );
        return Ok(None);
    }
    if framing == RawSourceFraming::LfDelimited {
        // Anchor the end of this range from the bytes being shipped, not a later
        // read of the file: a rewrite before the receipt lands must still be
        // seen as one. The anchor is consulted only once the lane reaches
        // exactly this position, so a batch that is declined or replaced leaves
        // nothing wrong behind.
        source_epoch::record_content_anchor(
            conn,
            resolution.source_epoch,
            &source_epoch::ContentAnchor::new(raw_batch.range_end, &batch_tail(&raw_batch)),
        )?;
        source_epoch::record_last_clock(
            conn,
            resolution.source_epoch,
            raw_batch.range_end,
            parse_result.last_clock.unwrap_or(starting_clock),
        )?;
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
    // The hook is the authority on interactions and it can miss. The transcript
    // carries the same outcome, so the wait a question opened is closed here too
    // — a backstop for an Esc, a dropped write, a killed process, or a machine
    // that never registered the hook. Resolution only: nothing here opens a wait.
    if provider.eq_ignore_ascii_case("claude") {
        for event in crate::claude_lifecycle_hook::transcript_resolutions_for_events(
            &session_id,
            &parse_result.events,
        ) {
            match crate::config::get_agent_runtime_events_outbox_dir() {
                Ok(dir) => {
                    if let Err(error) = crate::outbox::enqueue_runtime_event(&dir, &event) {
                        tracing::warn!(
                            session_id,
                            error = %error,
                            "Unable to queue a transcript-derived interaction resolution"
                        );
                    }
                }
                Err(error) => tracing::warn!(
                    session_id,
                    error = %error,
                    "No runtime-events outbox for a transcript-derived interaction resolution"
                ),
            }
        }
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
    let provider_session_id = parse_result
        .metadata
        .provider_session_id
        .as_deref()
        .unwrap_or(&parse_result.metadata.session_id)
        .trim()
        .to_string();
    let routes_as_provider_head = routes_as_provider_head(&parse_result.metadata);
    let previous_provider_session_id = if position == 0 && routes_as_provider_head {
        if let Some(managed_session_id) = durable_session_id.as_deref() {
            source_epoch::previous_provider_session_id(
                conn,
                resolution.source_epoch,
                provider,
                managed_session_id,
            )?
            .or(FileState::new(conn).previous_provider_session_id(
                &path_text,
                managed_session_id,
                provider,
            )?)
        } else {
            None
        }
    } else {
        None
    };
    if routes_as_provider_head {
        source_epoch::record_provider_session_id(
            conn,
            resolution.source_epoch,
            &provider_session_id,
        )?;
    }
    let conversation_reset = previous_provider_session_id
        .as_deref()
        .is_some_and(|previous| previous != provider_session_id.as_str());
    let mut render_records = render_records_for_batch(&parse_result, &raw_batch)?;
    if conversation_reset {
        captured_witness.previous_provider_session_id = previous_provider_session_id.clone();
        insert_conversation_reset_boundary(
            &mut render_records,
            resolution.source_epoch,
            raw_batch.range_start,
            &resolution.opened_at,
            &session_id,
            previous_provider_session_id.as_deref().unwrap_or_default(),
            &provider_session_id,
        )?;
    }
    let render_generation = render_generation_id(session_uuid);
    let session = session_facts(
        provider,
        &parse_result.metadata,
        &render_records,
        &resolution.opened_at,
    )?;
    let media_objects = parse_result
        .media_objects
        .iter()
        .filter(|media| {
            media.source_offset >= raw_batch.range_start
                && media.source_offset < raw_batch.range_end
        })
        .cloned()
        .collect::<Vec<_>>();
    let mut facts = parse_result
        .provider_facts
        .iter()
        .filter(|fact| {
            fact.source_offset >= raw_batch.range_start && fact.source_offset < raw_batch.range_end
        })
        .map(|fact| StorageV2ProviderFact {
            kind: fact.kind.clone(),
            at: fact.at.to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            source_position: fact.source_offset,
            payload: fact.payload.clone(),
        })
        .collect::<Vec<_>>();
    // Cursor's agent-transcripts JSONL is raw evidence only (see the render
    // field below); the host must withhold its render exactly when the laptop
    // does.
    let render_shipped = !cursor_projection_is_raw_only(provider, path)
        && (!is_cursor_agent_transcript_path(provider, path)
            || parse_result.metadata.started_at.is_some()
            || !render_records.is_empty());
    captured_witness.render_withheld = !render_shipped;
    // First, so a host that truncates an oversized fact list keeps it.
    facts.insert(
        0,
        capture_witness_fact(&captured_witness, raw_batch.range_start)?,
    );
    if position == 0
        && render_records.is_empty()
        && media_objects.is_empty()
        && provider.eq_ignore_ascii_case("claude")
        && raw_batch_is_claude_startup_metadata(&raw_batch)
    {
        // Startup metadata may be skipped only while establishing the first
        // host coordinate. Once an epoch has shipped data, advancing the
        // local cursor over new metadata would create a host-side range gap:
        // the next event envelope would start after bytes Runtime Host has
        // never accepted. Preserve those bytes in a zero-event envelope so
        // the durable raw range remains contiguous.
        source_epoch::acknowledge_position(
            conn,
            resolution.source_epoch,
            SourceLane::Durable,
            position,
            raw_batch.range_end,
        )?;
        if raw_batch.range_end < source_len {
            return prepare_file_envelope(
                conn,
                capabilities,
                view,
                provider,
                session_id_override,
                maximum_batch_bytes,
                hold,
            );
        }
        return Ok(None);
    }
    let batch_event_count = parse_result
        .events
        .iter()
        .filter(|event| {
            event.source_offset >= raw_batch.range_start
                && event.source_offset < raw_batch.range_end
        })
        .count();
    let wire_predecessor = if is_cursor_agent_transcript_path(provider, path)
        || provider.eq_ignore_ascii_case("antigravity")
    {
        // Cursor rewrites this JSONL projection repeatedly while a turn is in
        // flight, and Antigravity can rotate revisions before an earlier epoch
        // is admitted to the host. Revisions with no durable bytes on the host
        // must see the nearest admitted ancestor rather than an empty un-admitted revision.
        source_epoch::wire_predecessor_for_epoch(conn, resolution.source_epoch)?
    } else {
        resolution.predecessor_epoch
    };
    let prepared = PreparedStorageV2Envelope {
        envelope: StorageV2Envelope {
            protocol_version: 2,
            tenant_id: capabilities.tenant_id.clone(),
            machine_id: capabilities.machine_id.clone(),
            session_id,
            provider: provider.to_ascii_lowercase(),
            opaque_source_id,
            source_epoch: resolution.source_epoch.to_string(),
            predecessor_source_epoch: wire_predecessor.map(|value| value.to_string()),
            epoch_opened_at: resolution.opened_at,
            range_kind: "byte_offset".to_string(),
            range_start: raw_batch.range_start,
            range_end: raw_batch.range_end,
            // Cursor's agent-transcripts JSONL is raw evidence only. It has
            // no tool results, no tool_call_ids, no per-event clock and no
            // reasoning blocks, and it inlines thought summaries into the
            // committed answer; store.db carries all of it. Both sources bind
            // one session, and whichever publishes a render last becomes the
            // current generation, so attaching a render here is what let the
            // lossy projection overwrite the authoritative one. Keep shipping
            // its bytes; never let it claim render authority.
            // Suppressed outright when store.db owns this conversation's render;
            // otherwise the original rule stands, because a Cursor snapshot
            // without a clock cannot certify an empty transcript.
            render: render_shipped.then(|| StorageV2Render {
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
                .map(|record| StorageV2Record::from_bytes(record.range_start, record.bytes))
                .collect(),
            facts,
            expected_envelope_id,
        },
        source_epoch: resolution.source_epoch,
        range_start: raw_batch.range_start,
        range_end: raw_batch.range_end,
        event_count: batch_event_count,
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

pub(super) fn retire_empty_blocked_source_if_safe(
    conn: &mut Connection,
    provider: &str,
    pending: &PendingSourceEnvelope,
) -> Result<bool> {
    if !provider.eq_ignore_ascii_case("claude")
        || pending.block_kind.as_deref() != Some("source_epoch_conflict_unresolved")
        || !pending
            .block_detail
            .as_deref()
            .is_some_and(|detail| detail.contains("source_epoch_not_found"))
        || pending.range_end <= pending.range_start
    {
        return Ok(false);
    }
    let path = Path::new(&pending.source_path);
    if !path.is_file() {
        return Ok(false);
    }
    let prepared = pending_to_prepared(pending.clone())?;
    let render_is_metadata_only = prepared.envelope.render.as_ref().is_some_and(|render| {
        render
            .records
            .iter()
            .all(|record| record.branch_kind.as_deref() == Some("conversation_reset"))
    });
    if !render_is_metadata_only
        || !prepared.media_objects.is_empty()
        || !raw_records_are_claude_startup_metadata(&prepared.envelope.records)
    {
        return Ok(false);
    }
    let proof_json = serde_json::json!({
        "v": 1,
        "provider": provider,
        "source_path": pending.source_path,
        "range_start": pending.range_start.to_string(),
        "range_end": pending.range_end.to_string(),
        "event_count": 0,
        "media_count": 0,
        "host_manifest_absent": true,
    })
    .to_string();
    pending_source_envelope::retire_empty_source(
        conn,
        pending.source_epoch,
        &pending.envelope_id,
        pending.range_start,
        pending.range_end,
        &pending.request_body_zstd,
        "Retired host-absent source epoch after proving its range contained no canonical events or media",
        &proof_json,
    )?;
    tracing::info!(
        source_epoch = %pending.source_epoch,
        range_start = pending.range_start,
        range_end = pending.range_end,
        "Retired blocked empty source range"
    );
    Ok(true)
}

pub(super) fn raw_batch_is_claude_startup_metadata(batch: &RawRecordBatch) -> bool {
    !batch.records.is_empty()
        && batch
            .records
            .iter()
            .all(|record| is_claude_startup_metadata_bytes(&record.bytes))
}

pub(super) fn raw_records_are_claude_startup_metadata(records: &[StorageV2Record]) -> bool {
    !records.is_empty()
        && records.iter().all(|record| {
            record
                .bytes()
                .ok()
                .is_some_and(|bytes| is_claude_startup_metadata_bytes(&bytes))
        })
}

pub(super) fn is_claude_startup_metadata_bytes(bytes: &[u8]) -> bool {
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(bytes) else {
        return false;
    };
    matches!(
        value.get("type").and_then(serde_json::Value::as_str),
        Some(
            "last-prompt"
                | "mode"
                | "permission-mode"
                | "bridge-session"
                | "attachment"
                | "file-history-snapshot"
        )
    )
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
        live_catch_up_batch_bytes(live_lag_bytes(conn, provider, path))
    } else {
        BACKLOG_TARGET_BATCH_BYTES
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
    let body = wire_body(&pending, capabilities)?;
    Ok(Some((body, prepared)))
}

/// Live shipping is tuned for latency: a small batch keeps the newest content
/// close to the client. That tuning becomes a trap once a path has fallen
/// behind, because every scheduled turn then moves at most one live batch. On
/// 2026-09-13 a live managed transcript drained at one 64 KiB batch per
/// scheduled turn while its lane sat 117 minutes behind its own file, on an
/// idle uplink.
///
/// A live lane that is behind may therefore use the backlog batch size and
/// catch up in one pass.
pub(super) fn live_catch_up_batch_bytes(lag_bytes: u64) -> usize {
    if lag_bytes > LIVE_TARGET_BATCH_BYTES as u64 {
        BACKLOG_TARGET_BATCH_BYTES
    } else {
        LIVE_TARGET_BATCH_BYTES
    }
}

/// Bytes between a source's durable lane and the end of its file.
///
/// Zero when the source has no active epoch yet, or when its file is unreadable.
pub(super) fn live_lag_bytes(conn: &Connection, provider: &str, path: &Path) -> u64 {
    let Ok(metadata) = std::fs::metadata(path) else {
        return 0;
    };
    let canonical = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    let Ok(Some(position)) = durable_lane_position(conn, provider, &canonical.to_string_lossy())
    else {
        return 0;
    };
    metadata.len().saturating_sub(position)
}

/// How long a source's durable lane has been sitting still, in seconds.
///
/// A byte threshold alone cannot serve a slow producer: a session writing a few
/// bytes per second reaches any fixed threshold only after hours, so its
/// unshipped records sit for exactly as long. Callers use this to re-drive on
/// staleness as well as on size.
pub(crate) fn durable_lane_age_seconds(
    conn: &Connection,
    provider: &str,
    canonical_path: &str,
) -> Option<i64> {
    let epoch = crate::state::source_epoch::active_source_epoch(
        conn,
        provider,
        &opaque_source_id(canonical_path),
    )
    .ok()
    .flatten()?;
    let updated_at: String = conn
        .query_row(
            "SELECT updated_at FROM source_epoch_lane_state WHERE source_epoch = ?1 AND lane = 'durable'",
            rusqlite::params![epoch.to_string()],
            |row| row.get(0),
        )
        .ok()?;
    let parsed = chrono::DateTime::parse_from_rfc3339(&updated_at).ok()?;
    let age = chrono::Utc::now().signed_duration_since(parsed.with_timezone(&chrono::Utc));
    Some(age.num_seconds().max(0))
}

/// Whether the bytes before `position` differ from the ones anchored when the
/// range ending there was prepared. No anchor at exactly this position (a
/// source shipped before anchors existed, or a lane moved by a host resync)
/// means there is nothing to compare, not a rewrite.
pub(super) fn rewritten_under_cursor(
    conn: &Connection,
    path: &Path,
    source_epoch: Uuid,
    position: u64,
) -> Result<bool> {
    let Some(anchor) = source_epoch::content_anchor(conn, source_epoch)? else {
        return Ok(false);
    };
    if anchor.position != position {
        return Ok(false);
    }
    if std::fs::metadata(path)?.len() < position {
        // A shorter file is a truncation, which the epoch registry already ends.
        return Ok(false);
    }
    Ok(!source_epoch::file_content_anchor_holds(path, &anchor)?)
}

/// The last bytes of a contiguous raw batch, up to the anchor size, taken
/// across record boundaries.
pub(super) fn batch_tail(batch: &RawRecordBatch) -> Vec<u8> {
    let mut wanted = source_epoch::CONTENT_ANCHOR_BYTES;
    let mut pieces = Vec::new();
    for record in batch.records.iter().rev() {
        if wanted == 0 {
            break;
        }
        let take = record.bytes.len().min(wanted);
        pieces.push(&record.bytes[record.bytes.len() - take..]);
        wanted -= take;
    }
    pieces.into_iter().rev().flatten().copied().collect()
}

/// The witness a parse used, as the fact that travels with its envelope.
pub(super) fn capture_witness_fact(
    witness: &crate::pipeline::source::CapturedWitness,
    range_start: u64,
) -> Result<StorageV2ProviderFact> {
    Ok(witness_fact(
        witness
            .fallback_time
            .to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
        range_start,
        serde_json::to_value(witness).context("capture witness is not JSON")?,
    ))
}

/// The Runtime Host keeps a capture fact up to this size and drops a larger
/// one, leaving that envelope impossible to rebuild from raw.
pub(super) const MAX_CAPTURE_WITNESS_BYTES: usize = 64 * 1024;

pub(super) fn witness_fact(at: String, range_start: u64, payload: Value) -> StorageV2ProviderFact {
    let size = payload.to_string().len();
    if size > MAX_CAPTURE_WITNESS_BYTES {
        tracing::warn!(
            size,
            range_start,
            "Capture witness exceeds the host's limit; this envelope will not be rebuildable from raw"
        );
    }
    StorageV2ProviderFact {
        kind: crate::pipeline::source::CAPTURE_WITNESS_FACT_KIND.to_string(),
        at,
        source_position: range_start,
        payload,
    }
}
