//! Per-path shipping jobs: retries, ready-work pumping and `run_path_job`.

use super::*;

pub(super) fn queue_storage_v2_pending_retry_paths(
    scheduler: &mut PathScheduler,
    conn: &rusqlite::Connection,
    archive_repair_mode: ArchiveRepairMode,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
) -> Result<usize> {
    if read_archive_repair_control().is_paused(archive_repair_mode) {
        tracing::debug!("Immutable storage-v2 retry paused by local control file");
        return Ok(0);
    }
    let mut queued = 0usize;
    for pending in crate::state::pending_source_envelope::retry_paths(conn)? {
        let Some(provider) = discovery::canonical_provider_name(&pending.provider) else {
            tracing::warn!(
                provider = %pending.provider,
                path = %pending.source_path,
                "Skipping storage-v2 pending retry with unknown provider"
            );
            continue;
        };
        let path = PathBuf::from(pending.source_path);
        if retry_admission_open(&path, deferred_retries) {
            scheduler.enqueue_observed_with_estimated_bytes(
                path,
                provider,
                WorkPriority::Retry,
                STORAGE_V2_PENDING_RETRY_OBSERVATION_SOURCE,
                now_ms(),
                Some(pending.raw_bytes),
            );
            queued += 1;
        }
    }
    Ok(queued)
}

pub(super) fn queue_failed_shipment_retries_if_idle(
    scheduler: &mut PathScheduler,
    conn: &rusqlite::Connection,
    offline: bool,
    archive_repair_mode: ArchiveRepairMode,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
) -> Result<usize> {
    if offline || scheduler.has_pending_work() {
        return Ok(0);
    }
    queue_storage_v2_pending_retry_paths(scheduler, conn, archive_repair_mode, deferred_retries)
}

pub(super) fn work_context(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Live => "live_transcript",
        WorkPriority::Retry => FAILED_SHIPMENT_RETRY_CONTEXT,
        WorkPriority::Scan => "reconciliation_scan",
    }
}

pub(super) fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// Path preparation, SQLite, compression, and acknowledgements can block.
/// Construct and poll their non-Send futures away from the LocalSet that owns
/// live wake/control dispatch. Scheduler admission still bounds these workers.
pub(super) fn spawn_path_worker<T, Work, Fut>(
    tasks: &mut JoinSet<Option<T>>,
    mut shutdown: watch::Receiver<bool>,
    work: Work,
) where
    T: Send + 'static,
    Work: FnOnce() -> Fut + Send + 'static,
    Fut: Future<Output = T> + 'static,
{
    let runtime = tokio::runtime::Handle::current();
    tasks.spawn_blocking(move || {
        runtime.block_on(async move {
            let cancelled = *shutdown.borrow();
            if cancelled {
                return None;
            }
            tokio::select! {
                biased;
                _ = shutdown.changed() => None,
                value = work() => Some(value),
            }
        })
    });
}

pub(super) fn start_ready_jobs(
    scheduler: &mut PathScheduler,
    in_flight: &mut JoinSet<Option<PathTaskResult>>,
    task_context: &PathTaskContext,
    live_only: bool,
) {
    let mut next_job = if live_only {
        scheduler.pop_launchable_live()
    } else {
        scheduler.pop_launchable()
    };
    while let Some(job) = next_job {
        let task_context = task_context.clone();
        let shutdown = task_context.shutdown.subscribe();
        spawn_path_worker(in_flight, shutdown, move || run_path_job(job, task_context));
        next_job = if live_only {
            scheduler.pop_launchable_live()
        } else {
            scheduler.pop_launchable()
        };
    }
}

pub(super) fn pump_ready_local_work(
    scheduler: &mut PathScheduler,
    in_flight: &mut JoinSet<Option<PathTaskResult>>,
    task_context: &PathTaskContext,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
    shipping_progress: &mut heartbeat::ShippingProgressObservation,
    offline: bool,
    background_paused: bool,
) {
    if known_pending_local_work(scheduler, deferred_retries, background_paused) {
        shipping_progress.observe_pending_work(true, Instant::now());
    }
    drain_due_local_retries(scheduler, deferred_retries);
    start_ready_jobs(
        scheduler,
        in_flight,
        task_context,
        local_work_is_live_only(offline, background_paused),
    );
}

pub(super) fn known_pending_local_work(
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    background_paused: bool,
) -> bool {
    if background_paused {
        scheduler.has_pending_priority(WorkPriority::Live)
            || deferred_retries
                .values()
                .any(|retry| retry.priority == WorkPriority::Live)
    } else {
        scheduler.has_pending_work() || !deferred_retries.is_empty()
    }
}

pub(super) fn local_work_is_live_only(offline: bool, background_paused: bool) -> bool {
    offline || background_paused
}

pub(super) fn drain_due_local_retries(
    scheduler: &mut PathScheduler,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
) {
    let now = Instant::now();
    let ready_paths: Vec<_> = deferred_retries
        .iter()
        .filter_map(|(path, retry)| (retry.due_at <= now).then_some(path.clone()))
        .collect();

    for path in ready_paths {
        if let Some(retry) = deferred_retries.remove(&path) {
            scheduler.enqueue_observation(path, retry.provider, retry.priority, retry.observation);
        }
    }
}

#[cfg(test)]
pub(super) fn ignore_transcript_shipping_for_signals(
    _conn: &rusqlite::Connection,
    signals: Vec<outbox::DrainedPresenceSignal>,
) {
    if !signals.is_empty() {
        tracing::debug!(
            signal_count = signals.len(),
            "Hook outbox signals do not schedule transcript shipping"
        );
    }
}

#[cfg(test)]
pub(super) fn filter_new_outbox_signals(
    signals: Vec<outbox::DrainedPresenceSignal>,
    seen: &mut HashSet<String>,
) -> Vec<outbox::DrainedPresenceSignal> {
    if seen.len() > 4096 {
        seen.clear();
    }

    signals
        .into_iter()
        .filter(|signal| seen.insert(outbox_signal_mark(signal)))
        .collect()
}

#[cfg(test)]
pub(super) fn outbox_signal_mark(signal: &outbox::DrainedPresenceSignal) -> String {
    format!(
        "{}|{}|{}|{}|{}",
        signal.provider,
        signal.session_id,
        signal.phase,
        signal.observed_at.timestamp_millis(),
        signal
            .transcript_path
            .as_ref()
            .map(|path| path.to_string_lossy())
            .unwrap_or_default(),
    )
}

#[tracing::instrument(
    level = "info",
    name = "engine.path_job",
    skip(task_context),
    fields(
        longhouse.provider = %job.provider,
        longhouse.work_context = %work_context(job.priority),
    )
)]
pub(super) async fn run_path_job(job: PathJob, task_context: PathTaskContext) -> PathTaskResult {
    let task_started = Instant::now();
    let mut result = PathTaskResult {
        job,
        events_shipped: 0,
        bytes_shipped: 0,
        had_connect_error: false,
        rerun_priority: None,
        local_retry_after: None,
        local_retry_priority: None,
        reconciled_to_head: false,
        processing_elapsed: Duration::ZERO,
    };

    // Pool checkout may wait on SQLite contention. This task runs on the
    // LocalSet that owns wake/control dispatch, so a synchronous checkout here
    // can stall every managed session behind archive workers. Keep the wait on
    // the blocking pool just like parsing/preparation below.
    let db_pool = task_context.db_pool.clone();
    let mut conn = match tokio::task::spawn_blocking(move || db_pool.get()).await {
        Ok(Ok(conn)) => conn,
        Ok(Err(e)) => {
            if task_context.tracker.record_error() {
                tracing::warn!(
                    "Error opening shipper DB for {}: {}",
                    result.job.path.display(),
                    e
                );
            }
            result.local_retry_after = Some(local_retry_delay(result.job.priority));
            return finish_path_task(result, task_started);
        }
        Err(e) => {
            if task_context.tracker.record_error() {
                tracing::warn!(
                    "Shipper DB checkout task failed for {}: {}",
                    result.job.path.display(),
                    e
                );
            }
            result.local_retry_after = Some(local_retry_delay(result.job.priority));
            return finish_path_task(result, task_started);
        }
    };

    // Storage-v2 is the only transcript lane this engine has; `run` refuses
    // to start without it, so capabilities are always present here.
    let capabilities = task_context.storage_v2.as_ref();
    let lane = if result.job.priority == WorkPriority::Live {
        "live"
    } else {
        "repair"
    };
    let stats_lane = if result.job.priority == WorkPriority::Live {
        ShipLane::Live
    } else {
        ShipLane::Repair
    };
    let timeout = if lane == "live" {
        Duration::from_secs(20)
    } else {
        Duration::from_secs(75)
    };
    let ship_started = Instant::now();
    let ship_result = if is_opencode_database_job(&result.job) {
        crate::storage_v2_shipper::ship_next_opencode_envelope(
            &mut conn,
            &task_context.client,
            capabilities,
            &result.job.path,
            lane,
            timeout,
        )
        .await
        .map(|outcome| {
            outcome.map_or(
                PathStorageV2ShipResult::Current,
                PathStorageV2ShipResult::Shipped,
            )
        })
    } else if is_cursor_database_job(&result.job) {
        crate::storage_v2_shipper::ship_next_cursor_envelope(
            &mut conn,
            &task_context.client,
            capabilities,
            &result.job.path,
            lane,
            timeout,
        )
        .await
        .map(|outcome| match outcome {
            crate::storage_v2_shipper::CursorStorageV2ShipResult::Shipped(outcome) => {
                PathStorageV2ShipResult::Shipped(outcome)
            }
            crate::storage_v2_shipper::CursorStorageV2ShipResult::Current => {
                PathStorageV2ShipResult::Current
            }
            crate::storage_v2_shipper::CursorStorageV2ShipResult::WaitingOnClaim => {
                PathStorageV2ShipResult::WaitingOnClaim
            }
            crate::storage_v2_shipper::CursorStorageV2ShipResult::Continue => {
                PathStorageV2ShipResult::Continue
            }
        })
    } else if is_cursor_acp_source_job(&result.job) {
        crate::storage_v2_shipper::ship_next_cursor_acp_envelope(
            &mut conn,
            &task_context.client,
            capabilities,
            &result.job.path,
            lane,
            timeout,
        )
        .await
        .map(|outcome| {
            outcome.map_or(
                PathStorageV2ShipResult::Current,
                PathStorageV2ShipResult::Shipped,
            )
        })
    } else {
        crate::storage_v2_shipper::ship_next_envelope(
            &mut conn,
            &task_context.client,
            capabilities,
            &result.job.path,
            result.job.provider,
            result.job.observation.session_id.as_deref(),
            lane,
            timeout,
        )
        .await
        .map(|outcome| {
            outcome.map_or(
                PathStorageV2ShipResult::Current,
                PathStorageV2ShipResult::Shipped,
            )
        })
    };
    match ship_result {
        Ok(PathStorageV2ShipResult::Shipped(outcome)) => {
            let latency_ms = ship_started.elapsed().as_millis() as u64;
            task_context.ship_stats.record_with_lane_detail_and_stages(
                stats_lane,
                ShipAttemptOutcome::Ok,
                latency_ms,
                None,
                None,
                None,
                outcome.events_shipped as u32,
                outcome.bytes_shipped,
                false,
                None,
            );
            task_context.ship_stats.record_events_and_bytes_shipped(
                stats_lane,
                outcome.events_shipped as u32,
                outcome.bytes_shipped,
                latency_ms,
            );
            if let Some(failures) = task_context.tracker.record_success() {
                tracing::info!(
                    failures,
                    "Storage-v2 shipping recovered after consecutive failures"
                );
            }
            result.events_shipped = outcome.events_shipped;
            result.bytes_shipped = outcome.bytes_shipped;
            if outcome.has_more {
                result.rerun_priority = Some(result.job.priority);
            } else {
                result.reconciled_to_head = true;
            }
            tracing::info!(
                path = %result.job.path.display(),
                provider = result.job.provider,
                lane,
                bytes_shipped = outcome.bytes_shipped,
                events_shipped = outcome.events_shipped,
                "Shipped storage-v2 source envelope"
            );
        }
        Ok(PathStorageV2ShipResult::Current) => {
            result.reconciled_to_head = true;
        }
        Ok(PathStorageV2ShipResult::WaitingOnClaim) => {
            return finish_path_task(result, task_started);
        }
        Ok(PathStorageV2ShipResult::Continue) => {
            result.rerun_priority = Some(result.job.priority);
        }
        Err(error) => {
            if let Some(blocked) =
                error.downcast_ref::<crate::storage_v2_shipper::StorageV2SourceBlocked>()
            {
                if blocked.newly_blocked {
                    task_context.ship_stats.record_with_lane_detail_and_stages(
                        stats_lane,
                        ShipAttemptOutcome::PayloadRejected,
                        ship_started.elapsed().as_millis() as u64,
                        Some(409),
                        Some("storage_v2_source_blocked"),
                        Some(&blocked.to_string()),
                        0,
                        0,
                        false,
                        None,
                    );
                    tracing::warn!(
                        path = %result.job.path.display(),
                        provider = result.job.provider,
                        source_epoch = %blocked.source_epoch,
                        kind = blocked.kind,
                        detail = blocked.detail,
                        "Storage-v2 source quarantined; automatic retries stopped"
                    );
                } else {
                    tracing::debug!(
                        path = %result.job.path.display(),
                        provider = result.job.provider,
                        source_epoch = %blocked.source_epoch,
                        "Skipped already-quarantined storage-v2 source without a network attempt"
                    );
                }
                return finish_path_task(result, task_started);
            }
            if error
                .downcast_ref::<crate::storage_v2_shipper::StorageV2PreparationError>()
                .is_some()
            {
                tracing::warn!(
                    path = %result.job.path.display(),
                    provider = result.job.provider,
                    error = %error,
                    "Storage-v2 source preparation failed; retrying locally"
                );
                result.local_retry_after = Some(local_retry_delay(result.job.priority));
                return finish_path_task(result, task_started);
            }
            let backpressure =
                error.downcast_ref::<crate::shipping::client::StorageV2Backpressure>();
            task_context.ship_stats.record_with_lane_detail_and_stages(
                stats_lane,
                ShipAttemptOutcome::RetryableClientError,
                ship_started.elapsed().as_millis() as u64,
                backpressure.map(|_| 503),
                Some(if backpressure.is_some() {
                    "storage_lane_busy"
                } else {
                    "storage_v2_ship_failed"
                }),
                Some(&error.to_string()),
                0,
                0,
                backpressure.is_some(),
                None,
            );
            if let Some(backpressure) = backpressure {
                task_context
                    .limiter
                    .observe_backpressure(Some(backpressure.retry_after));
            }
            if crate::shipping::client::is_connect_error(&error) {
                result.had_connect_error = true;
            }
            if task_context
                .client
                .host_link()
                .explains_failure(&format!("{error:#}"))
            {
                tracing::debug!(
                    path = %result.job.path.display(),
                    provider = result.job.provider,
                    lane,
                    error = %error,
                    retry_after_secs = backpressure.map(|value| value.retry_after.as_secs_f64()),
                    "Storage-v2 POST deferred during Runtime Host update"
                );
            } else if task_context.tracker.record_error() {
                tracing::warn!(
                    path = %result.job.path.display(),
                    provider = result.job.provider,
                    lane,
                    error = %error,
                    "Storage-v2 ship failed; durable cursor remains unchanged"
                );
            }
            result.local_retry_after = Some(
                backpressure
                    .map(|value| {
                        storage_v2_backpressure_retry_delay(
                            result.job.priority,
                            value.retry_after,
                            task_context.client.host_link().is_updating(),
                        )
                    })
                    .unwrap_or_else(|| local_retry_delay(result.job.priority)),
            );
        }
    }
    finish_path_task(result, task_started)
}

pub(super) fn finish_path_task(mut result: PathTaskResult, started: Instant) -> PathTaskResult {
    result.processing_elapsed = started.elapsed();
    result
}

impl DaemonState {
    pub(super) fn on_path_task_done(
        &mut self,
        task_result: Option<Result<Option<PathTaskResult>, tokio::task::JoinError>>,
    ) -> Result<()> {
        {
            match task_result {
                Some(Ok(Some(result))) => {
                    let retry_path = result.job.path.clone();
                    let retry_provider = result.job.provider;
                    let reconciled_to_head = result.reconciled_to_head
                        && result.rerun_priority.is_none()
                        && result.local_retry_after.is_none();
                    if result.local_retry_after.is_some() {
                        self.scheduler.complete_without_rerun(&retry_path);
                    } else {
                        self.scheduler.complete(&retry_path, result.rerun_priority);
                    }
                    if let Some(delay) = result.local_retry_after {
                        let priority = result.local_retry_priority.unwrap_or(result.job.priority);
                        self.deferred_retries.insert(
                            retry_path.clone(),
                            DeferredRetry {
                                due_at: Instant::now() + delay,
                                provider: retry_provider,
                                priority,
                                observation: result.job.observation.clone(),
                            },
                        );
                    }
                    // Checking an unchanged source head advances reconciliation
                    // without creating a new upload receipt.
                    if reconciled_to_head || result.events_shipped > 0 || result.bytes_shipped > 0 {
                        self.shipping_progress.record_progress(Instant::now());
                    }
                    if result.had_connect_error {
                        if self.offline.record_connect_error() {
                            self.shipping_progress.reset_after_sleep(Instant::now());
                            tracing::warn!(
                                    threshold = OFFLINE_CONNECT_FAILURE_THRESHOLD,
                                    "Connection error threshold reached while processing {} — entering offline mode",
                                    result.job.path.display()
                                );
                        } else {
                            tracing::warn!(
                                    consecutive_connect_errors = self.offline.consecutive_connect_failures,
                                    threshold = OFFLINE_CONNECT_FAILURE_THRESHOLD,
                                    "Connection error while processing {}; keeping local shipping active",
                                    result.job.path.display()
                                );
                        }
                    } else if result.events_shipped > 0 || result.bytes_shipped > 0 {
                        self.last_ship_at = Some(chrono::Utc::now().to_rfc3339());
                        if let Some(duration) = self.offline.mark_online() {
                            self.last_runtime_truth_signature = None;
                            tracing::info!(
                                "Back online after {:.0}s — resuming shipping",
                                duration.as_secs_f64()
                            );
                        }
                    }
                    if reconciled_to_head
                        && !self.scheduler.has_path(&retry_path)
                        && !self.deferred_retries.contains_key(&retry_path)
                    {
                        if let Some(open) = self.open_history_reconciliation.as_mut() {
                            open.remaining_paths.remove(&retry_path);
                        }
                    }
                    maybe_seal_history_reconciliation(
                        &mut self.conn,
                        &mut self.open_history_reconciliation,
                        &self.scheduler,
                        &self.deferred_retries,
                        self.discovery_tasks.is_empty(),
                    );
                }
                Some(Err(e)) => {
                    return Err(anyhow::anyhow!("path task failed: {}", e));
                }
                None => {}
                Some(Ok(None)) => {}
            }
        }
        Ok(())
    }

    pub(super) async fn on_watcher_event(
        &mut self,
        config: &ConnectConfig,
        first_event: WatcherEvent,
    ) {
        let managed_state_changes = handle_live_transcript_file_events(
            &mut self.watcher,
            first_event,
            &self.providers,
            &self.managed_state_dirs,
            &self.conn,
            &mut self.transcript_wake_rx,
            &mut self.scheduler,
            &mut self.latest_transcript_wake_observed,
            &mut self.deferred_retries,
            &mut self.in_flight,
            &self.task_context,
            &mut self.shipping_progress,
            self.offline.is_offline,
            archive_repair_is_paused(config.archive_repair_mode),
        )
        .await;
        if !managed_state_changes.is_empty() {
            let requires_discovery = managed_state_changes_require_full_reconciliation(
                &self.last_managed_observations,
                &managed_state_changes,
            );
            if requires_discovery {
                // A watcher burst can contain many transient paths while
                // a provider is writing one session. Queue one bounded
                // full walk; the 5s observation tick starts it after the
                // burst instead of starting one walk per event batch.
                self.projection_generation = self.projection_generation.saturating_add(1);
                self.pending_full_reconciliation = true;
                tracing::debug!(
                    event_count = managed_state_changes.len(),
                    "Queued managed state discovery for coalesced observation"
                );
            } else {
                tracing::debug!(
                    event_count = managed_state_changes.len(),
                    "Known managed state changed; bounded periodic observation owns refresh"
                );
            }
        }
    }

    pub(super) fn on_failed_ship_retry_tick(&mut self, config: &ConnectConfig) {
        let mut queued_retries = 0usize;
        match queue_storage_v2_pending_retry_paths(
            &mut self.scheduler,
            &self.conn,
            config.archive_repair_mode,
            &mut self.deferred_retries,
        ) {
            Ok(queued) => queued_retries = queued_retries.saturating_add(queued),
            Err(error) => tracing::warn!(
                error = %error,
                "Immutable storage-v2 retry error"
            ),
        }
        // Copied Cursor bytes are only needed until the host receipts
        // them; nothing deleted them before, so they grew to 8.5GB of
        // a 9.7GB local database. Run one bounded batch in the existing
        // single-flight maintenance worker, after retry queueing above
        // makes owed envelopes visible to the safety predicate. Never
        // put this writer on the event-loop connection: even a bounded
        // batch must not pause live transcript scheduling.
        if self.storage_maintenance_tasks.is_empty() {
            let db_path = self.projection_db_path.clone();
            self.storage_maintenance_tasks.spawn_blocking(move || {
                let result = crate::state::db::open_connection(&db_path).and_then(|conn| {
                    crate::state::cursor_store_records::drain_receipted_cursor_records(&conn)
                });
                match result {
                    Ok(0) => {}
                    Ok(drained) => {
                        tracing::info!(drained, "drained bounded batch of receipted Cursor records")
                    }
                    Err(error) => tracing::warn!(
                        error = %error,
                        "Cursor record drain error"
                    ),
                }
            });
        }
        if queued_retries > 0 {
            tracing::debug!(
                queued_retries,
                "Queued durable retry paths on periodic refill"
            );
        }
    }
}
