//! Sending prepared envelopes (file, OpenCode, Cursor store, Cursor ACP).

use super::*;

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
        live_catch_up_batch_bytes(live_lag_bytes(conn, provider, path))
    } else {
        BACKLOG_TARGET_BATCH_BYTES
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
    if pending.blocked_at.is_some() {
        // Every blocked source gets one host-truth re-examination when it is
        // due, whatever its provider. This branch used to be gated on
        // `provider == "cursor"`, so a blocked Claude, Codex, OpenCode or
        // Antigravity source returned `StorageV2SourceBlocked` below without
        // ever touching the wire again — recovery existed only for the
        // provider whose incident prompted it, and every other provider's
        // blocks were permanent by default.
        //
        // Re-examination starts from current host manifests. Admission
        // conflicts retry the exact frozen request to obtain typed evidence;
        // human-readable block detail is never parsed as control state.
        // Whether it is due yet is `reexamine_blocked_source`'s question.
        if reexamine_blocked_source(
            conn,
            client,
            capabilities,
            &pending,
            &prepared,
            lane,
            request_timeout,
        )
        .await?
        {
            return Ok(StorageV2ShipOutcome {
                bytes_shipped: 0,
                events_shipped: 0,
                has_more: true,
            });
        }
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
            wire_body(&pending, capabilities)?,
            capabilities.envelope_body_encoding(),
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
                    conflict,
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
            // A structurally invalid envelope is terminal. The stored request
            // body is what gets retried, byte for byte, so re-sending it cannot
            // change the verdict — it just retries until someone reads the log.
            // Quarantining makes it visible in health and clearable through
            // `longhouse shipping discard`, after which the source re-prepares
            // from current code.
            if let Some(rejected) =
                error.downcast_ref::<crate::shipping::client::StorageV2EnvelopeRejected>()
            {
                return block_source(
                    conn,
                    prepared.source_epoch,
                    "envelope_rejected",
                    &format!(
                        "Runtime Host rejected the envelope as invalid ({}: {}). Retrying the \
                         stored body cannot change this. Details: {}",
                        rejected.code, rejected.message, rejected.response_body
                    ),
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
            if let Some(pending) =
                pending_source_envelope::load_for_epoch(conn, prepared.source_epoch)?
            {
                if pending.blocked_at.is_some() {
                    return if reexamine_blocked_source(
                        conn,
                        client,
                        capabilities,
                        &pending,
                        &prepared,
                        lane,
                        request_timeout,
                    )
                    .await?
                    {
                        Ok(CursorStorageV2ShipResult::Continue)
                    } else {
                        Ok(CursorStorageV2ShipResult::Current)
                    };
                }
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
