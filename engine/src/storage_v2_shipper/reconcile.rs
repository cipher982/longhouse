//! Reconciling a source with the host after a conflict: lineage, epochs,
//! predecessors, manifests and blocked sources.

use super::*;

pub(super) fn reconcile_cursor_render_generation_conflict(
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

pub(super) async fn reconcile_storage_v2_conflict(
    conn: &mut Connection,
    client: &ShipperClient,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    conflict: &crate::shipping::client::StorageV2Conflict,
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
            // Record the *admission* refusal, not just the manifest probe that
            // followed it. This branch used to store only the 404, which says
            // the epoch does not exist — true but useless, since the host
            // creates epochs lazily on first commit and never created this one.
            // The 409 that caused all of it was discarded, which is why the
            // 2026-08-04 incident could not be root-caused after the fact.
            let block_detail = format!(
                "Runtime Host refused admission ({}: {}) and has no manifest for the source epoch. \
                 Admission response: {}. Manifest response: {}",
                conflict.code,
                conflict.message,
                conflict.response_body,
                error
                    .downcast_ref::<crate::shipping::client::StorageV2SourceNotFound>()
                    .expect("typed source-not-found checked above")
                    .response_body
            );
            let newly_blocked = pending_source_envelope::quarantine(
                conn,
                prepared.source_epoch,
                "source_epoch_conflict_unresolved",
                &block_detail,
            )?;
            let blocked = pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
                .context("newly blocked source disappeared before host-authority recovery")?;
            if reconcile_cross_provider_session_binding(conn, &blocked, prepared, conflict)? {
                return Ok(Some(StorageV2ShipOutcome {
                    bytes_shipped: 0,
                    events_shipped: 0,
                    has_more: true,
                }));
            }
            if reconcile_lost_local_epoch(
                conn,
                client,
                &blocked,
                prepared,
                host_open_epoch_from_conflict(conflict),
                request_timeout,
            )
            .await?
            {
                return Ok(Some(StorageV2ShipOutcome {
                    bytes_shipped: 0,
                    events_shipped: 0,
                    has_more: true,
                }));
            }
            if reconcile_replaced_host_predecessor(
                conn,
                client,
                &blocked,
                prepared,
                host_closed_predecessor_from_conflict(conflict),
                request_timeout,
            )
            .await?
            {
                return Ok(Some(StorageV2ShipOutcome {
                    bytes_shipped: 0,
                    events_shipped: 0,
                    has_more: true,
                }));
            }
            return Err(StorageV2SourceBlocked {
                source_epoch: prepared.source_epoch,
                kind: "source_epoch_conflict_unresolved".to_string(),
                detail: block_detail,
                newly_blocked,
            }
            .into());
        }
        Err(error) => return Err(error),
    };
    // The host may be *behind* us rather than ahead. `proven_manifest_prefix`
    // walks forward from `range_start` and so can only answer "how much of what
    // I am sending do you already hold" — it has no way to express a gap below
    // that point, and every such conflict fell through to quarantine.
    //
    // A lower host watermark conflicts with the retained local evidence. Check
    // it before the proven-prefix path so the conflict is recorded explicitly
    // instead of allowing that path to treat stale host state as permission to
    // discard the pending envelope.
    if let Some(outcome) = resync_behind_host(conn, prepared, &manifest)? {
        return Ok(Some(outcome));
    }
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

/// Give a blocked source one provider-neutral re-examination against host truth.
///
/// Returns true when something changed and the caller should come back around.
///
/// The order matters. The host-behind check runs first so a stale manifest is
/// recorded as an authority conflict without mutating retained evidence. The
/// Cursor-specific lineage and replacement repairs remain for the epoch-identity
/// failures only they understand; they are reached through this one door rather
/// than gating whether the door opens.
///
/// A refusal is a verdict, not a transient failure: it stands until its backoff
/// elapses or something wakes the row (an engine restart, a repaired request
/// body). This is the single door to the wire for a blocked row -- the generic
/// shipper and the Cursor store lane both come through it -- so the backoff is
/// checked here, where the network attempt is made. It used to be read only by
/// `retry_paths`, and a blocked source that stayed behind its file was re-asked
/// at every other caller's cadence (the one-second bound-source reconciler,
/// watcher events, wakes, hook ships): ~7 refused requests a second from thirty
/// sources on 2026-09-28. Returns false, having done nothing, while the row is
/// inside its backoff.
#[allow(clippy::too_many_arguments)]
pub(super) async fn reexamine_blocked_source(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    lane: &str,
    request_timeout: Duration,
) -> Result<bool> {
    if pending_source_envelope::reexamination_is_deferred(conn, prepared.source_epoch)? {
        return Ok(false);
    }
    match examine_blocked_source(
        conn,
        client,
        capabilities,
        pending,
        prepared,
        lane,
        request_timeout,
    )
    .await
    {
        // Some looks end in "still blocked" through an error return (the host
        // is behind local evidence, for one). That is the same nothing-changed
        // answer, so it earns the same backoff; without it the row stays due
        // and is re-asked immediately.
        Err(error) if error.downcast_ref::<StorageV2SourceBlocked>().is_some() => {
            pending_source_envelope::defer_reexamination(conn, prepared.source_epoch)?;
            Err(error)
        }
        other => other,
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn examine_blocked_source(
    conn: &mut Connection,
    client: &ShipperClient,
    capabilities: &StorageV2Capabilities,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    lane: &str,
    request_timeout: Duration,
) -> Result<bool> {
    let manifest = match client
        .storage_v2_source_manifest(&prepared.source_epoch.to_string(), 0, Some(request_timeout))
        .await
    {
        Ok(manifest) => Some(manifest),
        Err(error)
            if error
                .downcast_ref::<crate::shipping::client::StorageV2SourceNotFound>()
                .is_some() =>
        {
            None
        }
        Err(error) => return Err(error),
    };
    if let Some(manifest) = manifest.as_ref() {
        if resync_behind_host(conn, prepared, manifest)?.is_some() {
            return Ok(true);
        }
        if reconcile_admitted_epoch_predecessor(
            conn,
            client,
            pending,
            prepared,
            manifest,
            request_timeout,
        )
        .await?
        {
            return Ok(true);
        }
    }
    if (prepared.envelope.provider == "cursor"
        && reconcile_blocked_cursor_replacement(conn, client, prepared, request_timeout).await?)
        || reconcile_blocked_lineage(conn, client, prepared, request_timeout).await?
    {
        return Ok(true);
    }
    // Retry the frozen request to obtain current, typed admission evidence.
    // Human-readable block detail remains diagnostic output, never control
    // state that must be parsed back into a conflict.
    if pending.block_kind.as_deref() == Some("source_epoch_conflict_unresolved") {
        match client
            .ship_storage_v2_body(
                &capabilities.ingest_path,
                lane,
                wire_body(&pending, capabilities)?,
                capabilities.envelope_body_encoding(),
                &pending.envelope_id,
                Some(request_timeout),
            )
            .await
        {
            Ok(receipt) => {
                pending_source_envelope::acknowledge_and_delete(
                    conn,
                    prepared.source_epoch,
                    &receipt.envelope_id,
                    prepared.range_start,
                    prepared.range_end,
                )?;
                return Ok(true);
            }
            Err(error) => {
                let Some(conflict) =
                    error.downcast_ref::<crate::shipping::client::StorageV2Conflict>()
                else {
                    // A structurally invalid envelope is as deterministic as a
                    // conflict: fall through to the backoff instead of handing
                    // the daemon an error it retries every half second.
                    if error
                        .downcast_ref::<crate::shipping::client::StorageV2EnvelopeRejected>()
                        .is_some()
                    {
                        pending_source_envelope::defer_reexamination(conn, prepared.source_epoch)?;
                        return Ok(false);
                    }
                    return Err(error);
                };
                if reconcile_cross_provider_session_binding(conn, pending, prepared, conflict)?
                    || reconcile_lost_local_epoch(
                        conn,
                        client,
                        pending,
                        prepared,
                        host_open_epoch_from_conflict(conflict),
                        request_timeout,
                    )
                    .await?
                    || reconcile_replaced_host_predecessor(
                        conn,
                        client,
                        pending,
                        prepared,
                        host_closed_predecessor_from_conflict(conflict),
                        request_timeout,
                    )
                    .await?
                {
                    return Ok(true);
                }
            }
        }
    }
    // An envelope the host called invalid gets its POST retried, not just a
    // manifest fetch. "Invalid" is the host's opinion at one moment: a schema
    // rollout or a partially deployed validator can refuse a body it will
    // accept minutes later. Quarantining without ever re-POSTing would make
    // that verdict permanent — the absorbing state this whole area exists to
    // avoid. Re-POSTing costs one request per backoff interval, which the
    // unresolved floor already spaces to six hours.
    if pending.block_kind.as_deref() == Some("envelope_rejected") {
        match client
            .ship_storage_v2_body(
                &capabilities.ingest_path,
                lane,
                wire_body(&pending, capabilities)?,
                capabilities.envelope_body_encoding(),
                &pending.envelope_id,
                Some(request_timeout),
            )
            .await
        {
            Ok(receipt) => {
                pending_source_envelope::acknowledge_and_delete(
                    conn,
                    prepared.source_epoch,
                    &receipt.envelope_id,
                    prepared.range_start,
                    prepared.range_end,
                )?;
                return Ok(true);
            }
            Err(_) => {
                // Still refused. Fall through to the backoff below rather than
                // surfacing a second identical block.
            }
        }
    }
    // Nothing to do this time. Push the next look further out rather than
    // letting the row come straight back: a re-examination that changes nothing
    // still costs a manifest fetch, and "always schedulable" without a growing
    // interval is how the fix for an absorbing state turns into a load problem.
    pending_source_envelope::defer_reexamination(conn, prepared.source_epoch)?;
    Ok(false)
}

/// The single open host epoch a lineage conflict discloses, when adopting it is
/// the recovery.
///
/// Two refusals mean "my registry is not the host's registry": an initial epoch
/// arriving while the host already has one open, and a predecessor the host has
/// never seen. Both come from a local registry that was lost, salvaged or
/// rebuilt, and the host's open epoch is the lineage to adopt in either.
pub(super) fn host_open_epoch_from_conflict(
    conflict: &crate::shipping::client::StorageV2Conflict,
) -> Option<Uuid> {
    let reason = conflict
        .details
        .get("reason")
        .and_then(serde_json::Value::as_str);
    let lineage_is_foreign = match reason {
        Some("another_epoch_is_already_open_for_this_source") => true,
        Some("predecessor_not_open_for_this_identity") => {
            conflict
                .details
                .get("predecessor_exists")
                .and_then(serde_json::Value::as_bool)
                == Some(false)
        }
        _ => false,
    };
    if !lineage_is_foreign {
        return None;
    }
    let epochs = conflict.details.get("open_source_epochs")?.as_array()?;
    if epochs.len() != 1 {
        return None;
    }
    Uuid::parse_str(epochs[0].as_str()?).ok()
}

pub(super) fn host_closed_predecessor_from_conflict(
    conflict: &crate::shipping::client::StorageV2Conflict,
) -> Option<Uuid> {
    if conflict
        .details
        .get("reason")
        .and_then(serde_json::Value::as_str)
        != Some("predecessor_not_open_for_this_identity")
        || conflict
            .details
            .get("predecessor_state")
            .and_then(serde_json::Value::as_str)
            != Some("closed")
    {
        return None;
    }
    Uuid::parse_str(conflict.details.get("expected_predecessor")?.as_str()?).ok()
}

pub(super) async fn reconcile_admitted_epoch_predecessor(
    conn: &mut Connection,
    client: &ShipperClient,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    manifest: &StorageV2SourceManifest,
    request_timeout: Duration,
) -> Result<bool> {
    let envelope = &prepared.envelope;
    let hosted = &manifest.source_epoch;
    let Some(host_predecessor) = hosted
        .predecessor_source_epoch
        .as_deref()
        .and_then(|value| Uuid::parse_str(value).ok())
    else {
        return Ok(false);
    };
    let Ok(accepted_through) = hosted.accepted_through.parse::<u64>() else {
        return Ok(false);
    };
    if pending.block_kind.as_deref() != Some("source_epoch_conflict")
        || envelope.predecessor_source_epoch.is_some()
        || manifest.v != 2
        || hosted.source_epoch != envelope.source_epoch
        || hosted.tenant_id != envelope.tenant_id
        || hosted.machine_id != envelope.machine_id
        || hosted.provider != envelope.provider
        || hosted.opaque_source_id != envelope.opaque_source_id
        || hosted.range_kind != envelope.range_kind
        || !timestamps_match_at_host_precision(&hosted.opened_at, &envelope.epoch_opened_at)
        || hosted.state != "open"
        || hosted.replaced_by_source_epoch.is_some()
        || accepted_through == 0
        || accepted_through != prepared.range_start
    {
        return Ok(false);
    }
    if !manifest_proves_tail(
        client,
        manifest,
        envelope,
        &envelope.source_epoch,
        accepted_through,
        Some(&envelope.session_id),
        true,
        request_timeout,
    )
    .await?
    {
        return Ok(false);
    }

    let predecessor = client
        .storage_v2_source_manifest(&host_predecessor.to_string(), 0, Some(request_timeout))
        .await?;
    let predecessor_epoch = &predecessor.source_epoch;
    let Ok(predecessor_accepted) = predecessor_epoch.accepted_through.parse::<u64>() else {
        return Ok(false);
    };
    if predecessor.v != 2
        || predecessor_epoch.source_epoch != host_predecessor.to_string()
        || predecessor_epoch.tenant_id != envelope.tenant_id
        || predecessor_epoch.machine_id != envelope.machine_id
        || predecessor_epoch.provider != envelope.provider
        || predecessor_epoch.opaque_source_id != envelope.opaque_source_id
        || predecessor_epoch.range_kind != envelope.range_kind
        || predecessor_epoch.state != "closed"
        || predecessor_epoch.replaced_by_source_epoch.as_deref()
            != Some(envelope.source_epoch.as_str())
        || predecessor_accepted == 0
    {
        return Ok(false);
    }
    if !manifest_proves_tail(
        client,
        &predecessor,
        envelope,
        &host_predecessor.to_string(),
        predecessor_accepted,
        None,
        false,
        request_timeout,
    )
    .await?
    {
        return Ok(false);
    }

    let mut replacement = envelope.clone();
    replacement.predecessor_source_epoch = Some(host_predecessor.to_string());
    let replacement_body =
        serde_json::to_vec(&replacement).context("serializing host-admitted predecessor repair")?;
    let replacement_body_zstd =
        encode_zstd(&replacement_body, "host-admitted predecessor repair body")?;
    let proof_json = serde_json::json!({
        "v": 1,
        "source_epoch": envelope.source_epoch,
        "host_predecessor": host_predecessor.to_string(),
        "host_accepted_through": hosted.accepted_through,
        "predecessor_accepted_through": predecessor_epoch.accepted_through,
        "host_commit_seq": manifest.commit_seq,
        "predecessor_commit_seq": predecessor.commit_seq,
    })
    .to_string();
    pending_source_envelope::align_admitted_epoch_predecessor(
        conn,
        prepared.source_epoch,
        host_predecessor,
        predecessor_epoch
            .predecessor_source_epoch
            .as_deref()
            .map(Uuid::parse_str)
            .transpose()?,
        accepted_through,
        predecessor_accepted,
        &pending.envelope_id,
        &pending.request_body_zstd,
        &replacement_body_zstd,
        &proof_json,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        host_predecessor = %host_predecessor,
        accepted_through,
        "Restored salvaged epoch predecessor from Runtime Host proof"
    );
    Ok(true)
}

pub(super) fn timestamps_match_at_host_precision(left: &str, right: &str) -> bool {
    let Ok(left) = DateTime::parse_from_rfc3339(left) else {
        return false;
    };
    let Ok(right) = DateTime::parse_from_rfc3339(right) else {
        return false;
    };
    left.timestamp_micros() == right.timestamp_micros()
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn manifest_proves_tail(
    client: &ShipperClient,
    initial: &StorageV2SourceManifest,
    envelope: &StorageV2Envelope,
    source_epoch: &str,
    accepted_through: u64,
    session_id: Option<&str>,
    require_active: bool,
    request_timeout: Duration,
) -> Result<bool> {
    let mut page = initial.clone();
    loop {
        if page.objects.iter().any(|object| {
            object.tenant_id == envelope.tenant_id
                && session_id.is_none_or(|expected| object.session_id == expected)
                && object.machine_id == envelope.machine_id
                && object.provider == envelope.provider
                && object.opaque_source_id == envelope.opaque_source_id
                && object.source_epoch == source_epoch
                && object.range_kind == envelope.range_kind
                && (!require_active || object.retired_at.is_none())
                && object.range_end.parse::<u64>() == Ok(accepted_through)
        }) {
            return Ok(true);
        }
        if page.objects.len() < 1_000 {
            return Ok(false);
        }
        let Some(next_position) = page
            .objects
            .iter()
            .filter_map(|object| object.range_end.parse::<u64>().ok())
            .max()
        else {
            return Ok(false);
        };
        if next_position == 0 || next_position >= accepted_through {
            return Ok(false);
        }
        page = client
            .storage_v2_source_manifest(source_epoch, next_position, Some(request_timeout))
            .await?;
        if page.v != 2 || page.source_epoch.source_epoch != source_epoch {
            return Ok(false);
        }
    }
}

pub(super) fn reconcile_cross_provider_session_binding(
    conn: &mut Connection,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    conflict: &crate::shipping::client::StorageV2Conflict,
) -> Result<bool> {
    if conflict
        .details
        .get("reason")
        .and_then(serde_json::Value::as_str)
        != Some("session_identity_conflict")
        || pending.block_kind.as_deref() != Some("source_epoch_conflict_unresolved")
        || prepared.range_start != 0
    {
        return Ok(false);
    }
    let envelope = &prepared.envelope;
    let Some(existing_provider) = conflict
        .details
        .get("existing_provider")
        .and_then(serde_json::Value::as_str)
    else {
        return Ok(false);
    };
    if conflict
        .details
        .get("requested_provider")
        .and_then(serde_json::Value::as_str)
        != Some(envelope.provider.as_str())
        || existing_provider.eq_ignore_ascii_case(&envelope.provider)
    {
        return Ok(false);
    }
    let parsed = parser::parse_session_file_with_provider(
        Path::new(&pending.source_path),
        0,
        Some(&envelope.provider),
    )?;
    let corrected_session_id = canonical_session_id(parsed.metadata.session_id.clone());
    let corrected_uuid = Uuid::parse_str(&corrected_session_id)
        .context("provider transcript session id is not a UUID")?;
    if corrected_session_id == envelope.session_id {
        return Ok(false);
    }
    let provider_session_id = parsed
        .metadata
        .provider_session_id
        .as_deref()
        .unwrap_or(&parsed.metadata.session_id)
        .trim();
    if provider_session_id.is_empty() {
        return Ok(false);
    }

    let mut replacement = envelope.clone();
    replacement.session_id = corrected_session_id.clone();
    if let Some(render) = replacement.render.as_mut() {
        render.generation_id = render_generation_id(corrected_uuid).to_string();
    }
    let replacement_body =
        serde_json::to_vec(&replacement).context("serializing cross-provider binding repair")?;
    let replacement_body_zstd =
        encode_zstd(&replacement_body, "cross-provider binding repair body")?;
    let proof_json = serde_json::json!({
        "v": 1,
        "source_epoch": envelope.source_epoch,
        "host_source_epoch_absent": true,
        "stale_session_id": envelope.session_id,
        "stale_binding_provider": existing_provider,
        "requested_provider": envelope.provider,
        "provider_session_id": provider_session_id,
        "corrected_session_id": corrected_session_id,
    })
    .to_string();
    pending_source_envelope::repair_cross_provider_session_binding(
        conn,
        prepared.source_epoch,
        &pending.source_path,
        &envelope.provider,
        existing_provider,
        &envelope.session_id,
        provider_session_id,
        &pending.envelope_id,
        &pending.request_body_zstd,
        &replacement_body_zstd,
        &proof_json,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        stale_session_id = envelope.session_id,
        corrected_session_id,
        stale_provider = existing_provider,
        provider = envelope.provider,
        "Removed cross-provider transcript binding before host admission"
    );
    Ok(true)
}

/// Recover from a salvaged local shipper database that no longer contains the
/// epoch the Runtime Host already accepted for this source.
///
/// The host-proven open epoch becomes the wire predecessor of the retained
/// local initial epoch. That preserves every local byte and lets normal epoch
/// replacement close the old hosted epoch, instead of either replaying an
/// overlapping "initial" history forever or jumping a local cursor forward.
pub(super) async fn reconcile_lost_local_epoch(
    conn: &mut Connection,
    client: &ShipperClient,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    host_epoch: Option<Uuid>,
    request_timeout: Duration,
) -> Result<bool> {
    let Some(host_epoch) = host_epoch else {
        return Ok(false);
    };
    if pending.block_kind.as_deref() != Some("source_epoch_conflict_unresolved") {
        return Ok(false);
    }
    let manifest = client
        .storage_v2_source_manifest(&host_epoch.to_string(), 0, Some(request_timeout))
        .await?;
    let hosted = &manifest.source_epoch;
    let envelope = &prepared.envelope;
    let Ok(accepted_through) = hosted.accepted_through.parse::<u64>() else {
        return Ok(false);
    };
    if manifest.v != 2
        || hosted.source_epoch != host_epoch.to_string()
        || hosted.tenant_id != envelope.tenant_id
        || hosted.machine_id != envelope.machine_id
        || hosted.provider != envelope.provider
        || hosted.opaque_source_id != envelope.opaque_source_id
        || hosted.range_kind != envelope.range_kind
        || hosted.state != "open"
        || hosted.replaced_by_source_epoch.is_some()
        || accepted_through == 0
        || manifest.objects.is_empty()
    {
        return Ok(false);
    }

    let local_position =
        source_epoch::lane_position(conn, prepared.source_epoch, SourceLane::Durable)?;
    if local_position > 0 {
        // This epoch is absent on the host, so its authoritative host
        // watermark is zero even if salvaged local state remembers an older
        // receipt. Byte-offset sources can safely replay from disk as a fresh
        // replacement; record-ordinal stores cannot reconstruct old rows and
        // must remain quarantined.
        if envelope.range_kind != "byte_offset"
            || prepared.range_start != local_position
            || pending.range_start != local_position
        {
            return Ok(false);
        }
        let metadata = match std::fs::metadata(&pending.source_path) {
            Ok(metadata) if metadata.len() >= prepared.range_end => metadata,
            _ => return Ok(false),
        };
        let current_incarnation = crate::state::file_identity::identity_from_metadata(&metadata);
        let registered_incarnation = crate::state::source_epoch::active_source_incarnation(
            conn,
            &envelope.provider,
            &envelope.opaque_source_id,
        )?;
        if !crate::state::file_identity::file_identities_match(
            registered_incarnation.as_deref(),
            current_incarnation.as_deref(),
        ) {
            return Ok(false);
        }
        let transaction = conn
            .transaction()
            .context("open transaction for lost host epoch replay")?;
        crate::state::source_epoch::resync_to_host_watermark(
            &transaction,
            prepared.source_epoch,
            SourceLane::Durable,
            0,
        )?;
        if !pending_source_envelope::discard_after_cursor_resync(
            &transaction,
            prepared.source_epoch,
            &pending.envelope_id,
        )? {
            anyhow::bail!("lost host epoch envelope changed before replay rewind");
        }
        transaction.commit()?;
        tracing::warn!(
            source_epoch = %prepared.source_epoch,
            previous_local_position = local_position,
            provider = %envelope.provider,
            "Rewound host-absent byte-offset epoch for full replacement replay"
        );
        return Ok(true);
    }

    let mut replacement = envelope.clone();
    replacement.predecessor_source_epoch = Some(host_epoch.to_string());
    let replacement_body =
        serde_json::to_vec(&replacement).context("serializing host-authority epoch recovery")?;
    let replacement_body_zstd =
        encode_zstd(&replacement_body, "host-authority epoch recovery body")?;
    let proof_json = serde_json::json!({
        "v": 1,
        "requested_source_epoch": prepared.source_epoch.to_string(),
        "requested_epoch_absent_remotely": true,
        "host_open_source_epoch": host_epoch.to_string(),
        "host_state": hosted.state,
        "host_accepted_through": hosted.accepted_through,
        "host_commit_seq": manifest.commit_seq,
        "identity": {
            "tenant_id": hosted.tenant_id,
            "machine_id": hosted.machine_id,
            "provider": hosted.provider,
            "opaque_source_id": hosted.opaque_source_id,
            "range_kind": hosted.range_kind,
        },
    })
    .to_string();
    pending_source_envelope::attach_host_authority_predecessor(
        conn,
        prepared.source_epoch,
        host_epoch,
        accepted_through,
        &pending.envelope_id,
        &pending.request_body_zstd,
        &replacement_body_zstd,
        &proof_json,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        host_predecessor = %host_epoch,
        accepted_through,
        provider = %envelope.provider,
        "Recovered missing local epoch state from Runtime Host authority"
    );
    Ok(true)
}

/// Advance once when a recovered host predecessor has been replaced.
///
/// Only the single replacement shape supported by the local marker transaction
/// is accepted. Deeper history stays blocked instead of triggering an
/// unbounded manifest walk.
pub(super) async fn reconcile_replaced_host_predecessor(
    conn: &mut Connection,
    client: &ShipperClient,
    pending: &PendingSourceEnvelope,
    prepared: &PreparedStorageV2Envelope,
    closed_predecessor: Option<Uuid>,
    request_timeout: Duration,
) -> Result<bool> {
    let Some(closed_predecessor) = closed_predecessor else {
        return Ok(false);
    };
    if pending.block_kind.as_deref() != Some("source_epoch_conflict_unresolved")
        || prepared.envelope.predecessor_source_epoch.as_deref()
            != Some(closed_predecessor.to_string().as_str())
    {
        return Ok(false);
    }

    let envelope = &prepared.envelope;
    let closed_manifest = client
        .storage_v2_source_manifest(&closed_predecessor.to_string(), 0, Some(request_timeout))
        .await?;
    let closed = &closed_manifest.source_epoch;
    let Some(open_predecessor) = closed
        .replaced_by_source_epoch
        .as_deref()
        .and_then(|value| Uuid::parse_str(value).ok())
    else {
        return Ok(false);
    };
    if closed_manifest.v != 2
        || closed.source_epoch != closed_predecessor.to_string()
        || closed.tenant_id != envelope.tenant_id
        || closed.machine_id != envelope.machine_id
        || closed.provider != envelope.provider
        || closed.opaque_source_id != envelope.opaque_source_id
        || closed.range_kind != envelope.range_kind
        || closed.state != "closed"
    {
        return Ok(false);
    }
    let open_manifest = client
        .storage_v2_source_manifest(&open_predecessor.to_string(), 0, Some(request_timeout))
        .await?;
    let hosted = &open_manifest.source_epoch;
    if open_manifest.v != 2
        || hosted.source_epoch != open_predecessor.to_string()
        || hosted.tenant_id != envelope.tenant_id
        || hosted.machine_id != envelope.machine_id
        || hosted.provider != envelope.provider
        || hosted.opaque_source_id != envelope.opaque_source_id
        || hosted.range_kind != envelope.range_kind
        || hosted
            .predecessor_source_epoch
            .as_deref()
            .and_then(|value| Uuid::parse_str(value).ok())
            != Some(closed_predecessor)
        || hosted.state != "open"
        || hosted.replaced_by_source_epoch.is_some()
        || open_manifest.objects.is_empty()
    {
        return Ok(false);
    }
    let Ok(accepted_through) = hosted.accepted_through.parse::<u64>() else {
        return Ok(false);
    };
    if accepted_through == 0 {
        return Ok(false);
    }

    let mut replacement = envelope.clone();
    replacement.predecessor_source_epoch = Some(open_predecessor.to_string());
    let replacement_body = serde_json::to_vec(&replacement)
        .context("serializing replaced host-authority predecessor recovery")?;
    let replacement_body_zstd = encode_zstd(
        &replacement_body,
        "replaced host-authority predecessor recovery body",
    )?;
    let proof_json = serde_json::json!({
        "v": 1,
        "requested_source_epoch": prepared.source_epoch.to_string(),
        "stale_host_predecessor": closed_predecessor.to_string(),
        "host_open_source_epoch": open_predecessor.to_string(),
        "host_accepted_through": hosted.accepted_through,
        "closed_host_commit_seq": closed_manifest.commit_seq,
        "host_commit_seq": open_manifest.commit_seq,
        "identity": {
            "tenant_id": hosted.tenant_id,
            "machine_id": hosted.machine_id,
            "provider": hosted.provider,
            "opaque_source_id": hosted.opaque_source_id,
            "range_kind": hosted.range_kind,
        },
    })
    .to_string();
    pending_source_envelope::retarget_host_authority_predecessor(
        conn,
        prepared.source_epoch,
        closed_predecessor,
        open_predecessor,
        accepted_through,
        &pending.envelope_id,
        &pending.request_body_zstd,
        &replacement_body_zstd,
        &proof_json,
    )?;
    tracing::warn!(
        source_epoch = %prepared.source_epoch,
        stale_predecessor = %closed_predecessor,
        open_predecessor = %open_predecessor,
        accepted_through,
        "Advanced recovered predecessor to its Runtime Host replacement"
    );
    Ok(true)
}

/// Refuse a Runtime Host watermark that falls behind retained local evidence.
///
/// A stale or conflicting host watermark is an authority conflict, not proof
/// that the immutable pending envelope can be discarded. The existing
/// re-examination path can retry the same envelope after the host catches up.
///
/// Restricted to `range_kind == "byte_offset"` — Claude, Codex, Antigravity.
/// Cursor and OpenCode ship record ordinals over a mutable SQLite database
/// where an earlier ordinal may no longer be reconstructable, so their
/// bookkeeping is load-bearing and must not be re-derived this way.
///
/// Returns `None` when the host is not behind, leaving the existing
/// proven-prefix and lineage paths to reconcile authoritative host progress.
pub(super) fn resync_behind_host(
    conn: &mut Connection,
    prepared: &PreparedStorageV2Envelope,
    manifest: &StorageV2SourceManifest,
) -> Result<Option<StorageV2ShipOutcome>> {
    let epoch = &manifest.source_epoch;
    let envelope = &prepared.envelope;
    // Identity must match exactly before any host number is trusted. A manifest
    // for a different tenant, machine, provider, source, or range unit says
    // nothing about this cursor.
    if manifest.v != 2
        || epoch.source_epoch != envelope.source_epoch
        || epoch.tenant_id != envelope.tenant_id
        || epoch.machine_id != envelope.machine_id
        || epoch.provider != envelope.provider
        || epoch.opaque_source_id != envelope.opaque_source_id
        || epoch.range_kind != envelope.range_kind
        || epoch.range_kind != "byte_offset"
        || epoch.predecessor_source_epoch.as_deref() != envelope.predecessor_source_epoch.as_deref()
        || epoch.state != "open"
        || epoch.replaced_by_source_epoch.is_some()
    {
        return Ok(None);
    }
    let Ok(accepted_through) = epoch.accepted_through.parse::<u64>() else {
        return Ok(None);
    };
    // Only a gap below where we are about to send. Anything else is either
    // agreement or an overlap the proven-prefix path owns.
    if accepted_through >= prepared.range_start {
        return Ok(None);
    }
    block_source(
        conn,
        prepared.source_epoch,
        "source_epoch_conflict_unresolved",
        &format!(
            "Runtime Host is behind local durable evidence at {accepted_through}, below retained range start {}; preserved the immutable envelope and refused to rewind the epoch",
            prepared.range_start
        ),
    )
}

pub(super) async fn reconcile_blocked_lineage(
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

    // Local proof establishes no receipt-gated durable progress and no
    // pending request for every skipped epoch (plus no captured rows for the
    // SQLite store lane). Require matching host absence before changing the
    // rejected request's wire predecessor.
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
                || admitted.source_epoch.provider != prepared.envelope.provider
                || admitted.source_epoch.opaque_source_id != prepared.envelope.opaque_source_id
                || admitted.source_epoch.range_kind != prepared.envelope.range_kind
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
        "Repaired blocked lineage from Runtime Host manifest proof"
    );
    Ok(true)
}

pub(super) async fn reconcile_blocked_cursor_replacement(
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
        pending.range_start,
        pending.range_end,
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

pub(super) fn proven_manifest_prefix(
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

pub(super) fn split_prepared_suffix(
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

pub(super) fn envelope_id_for_subrange(
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

pub(super) fn decode_envelope_record_bytes(records: &[StorageV2Record]) -> Result<Vec<Vec<u8>>> {
    records
        .iter()
        .map(|record| {
            BASE64_STANDARD
                .decode(&record.data_b64)
                .context("decoding persisted storage-v2 raw record")
        })
        .collect()
}

pub(super) fn block_source<T>(
    conn: &Connection,
    source_epoch: Uuid,
    kind: &str,
    detail: &str,
) -> Result<T> {
    let newly_blocked = pending_source_envelope::quarantine(conn, source_epoch, kind, detail)?;
    Err(StorageV2SourceBlocked {
        source_epoch,
        kind: kind.to_string(),
        detail: detail.to_string(),
        newly_blocked,
    }
    .into())
}
