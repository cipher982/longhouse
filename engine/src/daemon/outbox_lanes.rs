//! Daemon loop handlers for the local outbox lanes: collecting, sweeping and
//! posting runtime events and archive envelopes.

use super::*;

/// Compact directory metadata stamp used to skip spawning blocking outbox/status
/// directory scans when a directory was empty on the previous pass and its
/// metadata has not moved.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct DirActivityStamp {
    inode: u64,
    modified_nanos: i128,
    ctime_nanos: i128,
}

pub(super) fn dir_activity_stamp(dir: &Path) -> Option<DirActivityStamp> {
    let meta = std::fs::metadata(dir).ok()?;
    let modified_nanos = meta
        .modified()
        .ok()
        .and_then(|m| m.duration_since(SystemTime::UNIX_EPOCH).ok())
        .map(|d| d.as_nanos() as i128)
        .unwrap_or(-1);
    #[cfg(unix)]
    let (inode, ctime_nanos) = {
        use std::os::unix::fs::MetadataExt;
        (
            meta.ino(),
            (meta.ctime() as i128)
                .saturating_mul(1_000_000_000)
                .saturating_add(meta.ctime_nsec() as i128),
        )
    };
    #[cfg(not(unix))]
    let (inode, ctime_nanos) = (0, -1);
    Some(DirActivityStamp {
        inode,
        modified_nanos,
        ctime_nanos,
    })
}

#[derive(Default)]
pub(super) struct OutboxIdleStamps {
    pub(super) outbox_empty: Option<DirActivityStamp>,
    pub(super) status_empty: Option<DirActivityStamp>,
    pub(super) runtime_empty: Option<DirActivityStamp>,
    /// Set when an FSEvent forced a scan while that lane's collect/post task
    /// was already in flight, so the completing pass cannot mark the dir empty
    /// and the next tick re-scans immediately.
    pub(super) pending_force_outbox: bool,
    pub(super) pending_force_status: bool,
    pub(super) pending_force_runtime: bool,
}

impl OutboxIdleStamps {
    pub(super) fn is_all_idle(&self) -> bool {
        self.outbox_empty.is_some()
            && self.status_empty.is_some()
            && self.runtime_empty.is_some()
            && !self.pending_force_outbox
            && !self.pending_force_status
            && !self.pending_force_runtime
    }
}

impl DaemonState {
    pub(super) async fn on_outbox_collect_done(
        &mut self,
        mut phase_projection_timer: std::pin::Pin<&mut tokio::time::Sleep>,
        outbox_collect_result: Option<Result<OutboxCollectResult, tokio::task::JoinError>>,
    ) {
        match outbox_collect_result {
            Some(Ok(result)) => {
                if result.presence.signals.is_empty()
                    && result.presence.posts.is_empty()
                    && !self.outbox_idle_stamps.pending_force_outbox
                {
                    self.outbox_idle_stamps.outbox_empty = result.dir_stamp_before;
                } else {
                    self.outbox_idle_stamps.outbox_empty = None;
                }
                if result.elapsed_ms > 100 {
                    tracing::warn!(
                        elapsed_ms = result.elapsed_ms,
                        presence_posts = result.presence.posts.len(),
                        "Outbox collection was slow"
                    );
                }
                if !result.presence.signals.is_empty() {
                    tracing::debug!(
                        signal_count = result.presence.signals.len(),
                        "Ignoring hook outbox transcript catch-up signals"
                    );
                    // The phase ledger just moved, which is the only
                    // evidence a turn boundary produces. Arm the
                    // debounce rather than projecting per signal.
                    if arm_phase_projection(&mut self.phase_projection_pending) {
                        phase_projection_timer.as_mut().reset(
                            super::projection::phase_projection_deadline(
                                tokio::time::Instant::now(),
                                self.last_phase_projection_at,
                            ),
                        );
                    }
                }
                if !result.presence.posts.is_empty() {
                    if self.outbox_post_tasks.is_empty() {
                        let client = self.client.clone();
                        let posts = result.presence.posts;
                        let post_count = posts.len();
                        // spawn, not spawn_local: the wrapper only awaits
                        // the worker task, and a local task is polled on
                        // the LocalSet's driving thread. Multi-second work
                        // there (a full reconciliation pass is 10-20s) then
                        // holds this gate closed while the network is idle.
                        self.outbox_post_tasks.spawn(async move {
                            let join_started = Instant::now();
                            let post_task = tokio::spawn(async move {
                                let task_started = Instant::now();
                                let (sent, kept) =
                                    outbox::post_pending_presence_files(&client, posts).await;
                                (sent, kept, task_started.elapsed().as_millis() as u64)
                            });
                            match post_task.await {
                                Ok((sent, kept, task_elapsed_ms)) => (
                                    sent,
                                    kept,
                                    join_started.elapsed().as_millis() as u64,
                                    task_elapsed_ms,
                                ),
                                Err(err) => {
                                    tracing::warn!(
                                        post_count,
                                        "Outbox presence POST worker task failed: {}",
                                        err
                                    );
                                    (
                                        0,
                                        post_count,
                                        join_started.elapsed().as_millis() as u64,
                                        join_started.elapsed().as_millis() as u64,
                                    )
                                }
                            }
                        });
                    } else {
                        tracing::debug!(
                            pending_posts = result.presence.posts.len(),
                            "Skipping outbox presence POST while previous POST is still in flight"
                        );
                    }
                }
            }
            Some(Err(err)) => {
                tracing::warn!("Outbox collection task failed: {}", err);
            }
            None => {}
        }
    }

    pub(super) async fn on_runtime_collect_done(
        &mut self,
        runtime_collect_result: Option<Result<RuntimeCollectResult, tokio::task::JoinError>>,
    ) {
        match runtime_collect_result {
            Some(Ok(result)) => {
                if result.posts.is_empty()
                    && result.measurement.pending_count == 0
                    && !self.outbox_idle_stamps.pending_force_runtime
                {
                    self.outbox_idle_stamps.runtime_empty = result.dir_stamp_before;
                } else {
                    self.outbox_idle_stamps.runtime_empty = None;
                }
                self.latest_runtime_event_outbox = result.measurement;
                if let Some(projection) = self.last_status_projection.as_mut() {
                    projection.set_runtime_event_outbox(self.latest_runtime_event_outbox.clone());
                }
                // A saturated pass means the directory holds more than
                // one pass can inspect, so the newest observation is
                // not reliably in it. Reduce it to current status in a
                // task of its own: the live lane must keep collecting
                // and posting while that runs.
                if result.saturated
                    && self.runtime_sweep_tasks.is_empty()
                    && self
                        .runtime_sweep_quiet_until
                        .is_none_or(|until| Instant::now() >= until)
                {
                    let runtime_events_outbox_dir = self.runtime_events_outbox_dir.clone();
                    self.runtime_sweep_tasks.spawn_blocking(move || {
                        outbox::sweep_runtime_event_outbox(
                            &runtime_events_outbox_dir,
                            outbox::RUNTIME_EVENT_SWEEP_LIMIT,
                        )
                    });
                }
                if result.elapsed_ms > 100 {
                    tracing::warn!(
                        elapsed_ms = result.elapsed_ms,
                        runtime_posts = result.posts.len(),
                        "Runtime-event collection was slow"
                    );
                }
                if !result.posts.is_empty() {
                    let retry_due = self
                        .runtime_outbox_retry_after
                        .map(|retry_at| Instant::now() >= retry_at)
                        .unwrap_or(true);
                    if self.runtime_outbox_post_tasks.is_empty() && retry_due {
                        let client = self.client.clone();
                        let runtime_posts = result.posts;
                        let post_count = runtime_posts.len();
                        // spawn, not spawn_local: see the presence path
                        // above. This is the live-transcript lane, so a
                        // starved wrapper here shows up as minutes-stale
                        // events on every client.
                        self.runtime_outbox_post_tasks.spawn(async move {
                            let join_started = Instant::now();
                            let post_task = tokio::spawn(async move {
                                let task_started = Instant::now();
                                let (sent, kept) = outbox::post_pending_runtime_event_files(
                                    &client,
                                    runtime_posts,
                                )
                                .await;
                                (sent, kept, task_started.elapsed().as_millis() as u64)
                            });
                            match post_task.await {
                                Ok((sent, kept, task_elapsed_ms)) => (
                                    sent,
                                    kept,
                                    join_started.elapsed().as_millis() as u64,
                                    task_elapsed_ms,
                                ),
                                Err(err) => {
                                    tracing::warn!(
                                        post_count,
                                        "Outbox runtime-event POST worker task failed: {}",
                                        err
                                    );
                                    (
                                        0,
                                        post_count,
                                        join_started.elapsed().as_millis() as u64,
                                        join_started.elapsed().as_millis() as u64,
                                    )
                                }
                            }
                        });
                    } else {
                        tracing::debug!(
                                    pending_posts = result.posts.len(),
                                    "Skipping outbox runtime-event POST while previous POST is still in flight"
                                );
                    }
                }
            }
            Some(Err(err)) => {
                tracing::warn!("Runtime-event collection task failed: {}", err);
            }
            None => {}
        }
    }

    pub(super) fn on_runtime_sweep_done(
        &mut self,
        runtime_sweep_result: Option<Result<outbox::RuntimeOutboxSweep, tokio::task::JoinError>>,
    ) {
        match runtime_sweep_result {
            Some(Ok(sweep)) => {
                tracing::warn!(
                    inspected = sweep.inspected,
                    discarded = sweep.discarded,
                    more = sweep.more,
                    "Swept superseded runtime status out of a flooded outbox"
                );
                // More work does not mean another pass right now. The
                // next saturated collection arms the next sweep on the
                // ordinary tick; chaining blocking passes back to back
                // would starve every other lane on this loop.
                self.runtime_sweep_quiet_until = (sweep.discarded == 0)
                    .then(|| Instant::now() + RUNTIME_SWEEP_QUIET_AFTER_NOTHING);
            }
            Some(Err(err)) => {
                tracing::warn!("Runtime-event outbox sweep task failed: {}", err);
            }
            None => {}
        }
    }

    pub(super) fn on_outbox_post_done(
        &mut self,
        outbox_post_result: Option<Result<(usize, usize, u64, u64), tokio::task::JoinError>>,
    ) {
        match outbox_post_result {
            Some(Ok((sent, kept, join_elapsed_ms, task_elapsed_ms))) => {
                let local_join_delay_ms = join_elapsed_ms.saturating_sub(task_elapsed_ms);
                if kept > 0 {
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST deferred during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST kept files for retry"
                        );
                    }
                } else if join_elapsed_ms > 1_000 {
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            sent,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST was delayed during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            sent,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            "Outbox presence POST was slow"
                        );
                    }
                } else if sent > 0 {
                    tracing::debug!(
                        sent,
                        task_elapsed_ms,
                        join_elapsed_ms,
                        "Outbox presence POST sent files"
                    );
                }
            }
            Some(Err(err)) => {
                if self.host_link.explains_failure(&err.to_string()) {
                    tracing::debug!(
                        "Outbox presence POST task deferred during Runtime Host update: {}",
                        err
                    );
                } else {
                    tracing::warn!("Outbox presence POST task failed: {}", err);
                }
            }
            None => {}
        }
    }

    pub(super) fn on_runtime_outbox_post_done(
        &mut self,
        runtime_outbox_post_result: Option<
            Result<(usize, usize, u64, u64), tokio::task::JoinError>,
        >,
    ) {
        match runtime_outbox_post_result {
            Some(Ok((sent, kept, join_elapsed_ms, task_elapsed_ms))) => {
                let local_join_delay_ms = join_elapsed_ms.saturating_sub(task_elapsed_ms);
                // A mixed result proves the Runtime Host is accepting
                // work. Do not let one session's retained event throttle
                // every other session; back off only when this pass made
                // no progress at all.
                if kept > 0 && sent == 0 {
                    self.runtime_outbox_consecutive_failures =
                        self.runtime_outbox_consecutive_failures.saturating_add(1);
                    let backoff_multiplier =
                        1u64 << self.runtime_outbox_consecutive_failures.min(4);
                    let delay = LIVE_LOCAL_RETRY_DELAY
                        .saturating_mul(backoff_multiplier as u32)
                        .min(Duration::from_secs(LOCAL_RETRY_DELAY_SECS));
                    self.runtime_outbox_retry_after = Some(Instant::now() + delay);
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            retry_delay_ms = delay.as_millis() as u64,
                            "Runtime-event outbox deferred during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            sent,
                            kept,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            local_join_delay_ms,
                            retry_delay_ms = delay.as_millis() as u64,
                            "Outbox runtime-event POST kept all files for retry"
                        );
                    }
                } else {
                    self.runtime_outbox_consecutive_failures = 0;
                    self.runtime_outbox_retry_after = None;
                    // Delivery made progress, so more may be waiting:
                    // collect again now rather than on the next tick.
                    // An empty outbox ends this, because a pass that
                    // finds nothing starts no POST.
                    if sent > 0 {
                        self.outbox_timer.reset_immediately();
                    }
                    if kept > 0 {
                        tracing::warn!(
                                    sent,
                                    kept,
                                    task_elapsed_ms,
                                    join_elapsed_ms,
                                    local_join_delay_ms,
                                    "Outbox runtime-event POST kept some files while other files progressed"
                                );
                    } else if join_elapsed_ms > 1_000 {
                        if self.host_link.is_updating() {
                            tracing::debug!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Runtime-event outbox POST was delayed during Runtime Host update"
                            );
                        } else {
                            tracing::warn!(
                                sent,
                                task_elapsed_ms,
                                join_elapsed_ms,
                                local_join_delay_ms,
                                "Outbox runtime-event POST was slow"
                            );
                        }
                    } else if sent > 0 {
                        tracing::debug!(
                            sent,
                            task_elapsed_ms,
                            join_elapsed_ms,
                            "Outbox runtime-event POST sent files"
                        );
                    }
                }
            }
            Some(Err(err)) => {
                self.runtime_outbox_consecutive_failures =
                    self.runtime_outbox_consecutive_failures.saturating_add(1);
                self.runtime_outbox_retry_after = Some(Instant::now() + LIVE_LOCAL_RETRY_DELAY);
                if self.host_link.explains_failure(&err.to_string()) {
                    tracing::debug!(
                        "Runtime-event outbox task deferred during Runtime Host update: {}",
                        err
                    );
                } else {
                    tracing::warn!("Outbox runtime-event POST task failed: {}", err);
                }
            }
            None => {}
        }
    }

    pub(super) fn on_outbox_tick(
        &mut self,
        config: &ConnectConfig,
        mut phase_projection_timer: std::pin::Pin<&mut tokio::time::Sleep>,
        force_scan: bool,
    ) {
        // The hook outbox is only one of five phase-ledger writers. The
        // Codex bridge, both Console adapters, and OpenCode all run as
        // separate processes and write the shared ledger directly, so
        // the daemon never sees their phases through the outbox at all
        // — those transitions fell through to the 60s reconciliation
        // and produced the long tail that survived the debounce.
        //
        // Watching the ledger watermark covers every producer at once,
        // including any added later, without each one having to signal
        // the daemon. One MAX() over a table with one row per session.
        if let Some(watermark) = latest_phase_watermark(&self.conn) {
            if self.last_phase_watermark != Some(watermark) {
                self.last_phase_watermark = Some(watermark);
                if arm_phase_projection(&mut self.phase_projection_pending) {
                    phase_projection_timer.as_mut().reset(
                        super::projection::phase_projection_deadline(
                            tokio::time::Instant::now(),
                            self.last_phase_projection_at,
                        ),
                    );
                }
            }
        }
        // Files remain durable while a POST is in flight. Do not scan,
        // parse, and persist them again until that attempt has either removed
        // them or made them eligible for retry. When a directory was empty on
        // the previous pass and its metadata stamp has not changed (and no
        // FSEvent forced a scan), skip spawning the blocking task altogether.
        let force_outbox = force_scan || self.outbox_idle_stamps.pending_force_outbox;
        let outbox_unchanged_empty = !force_outbox
            && self.outbox_idle_stamps.outbox_empty.is_some()
            && self.outbox_idle_stamps.outbox_empty == dir_activity_stamp(&self.outbox_dir);
        if !outbox_unchanged_empty {
            if self.outbox_collect_tasks.is_empty() && self.outbox_post_tasks.is_empty() {
                self.outbox_idle_stamps.outbox_empty = None;
                self.outbox_idle_stamps.pending_force_outbox = false;
                let outbox_dir = self.outbox_dir.clone();
                let db_path = config.shipper_config.db_path.clone();
                self.outbox_collect_tasks.spawn_blocking(move || {
                    let started = Instant::now();
                    let dir_stamp_before = dir_activity_stamp(&outbox_dir);
                    let presence = outbox::collect_outbox_with_local_state_result(
                        &outbox_dir,
                        db_path.as_deref(),
                    );
                    OutboxCollectResult {
                        presence,
                        dir_stamp_before,
                        elapsed_ms: started.elapsed().as_millis() as u64,
                    }
                });
            } else if force_scan {
                self.outbox_idle_stamps.outbox_empty = None;
                self.outbox_idle_stamps.pending_force_outbox = true;
            }
        }
        let status_dir = self
            .runtime_events_outbox_dir
            .parent()
            .map(crate::status_slot::status_slot_dir);
        let force_status = force_scan || self.outbox_idle_stamps.pending_force_status;
        let status_unchanged_empty = !force_status
            && self.status_recorded.is_empty()
            && self.outbox_idle_stamps.status_empty.is_some()
            && status_dir
                .as_deref()
                .and_then(dir_activity_stamp)
                .is_some_and(|stamp| Some(stamp) == self.outbox_idle_stamps.status_empty);
        if !status_unchanged_empty {
            if self.status_slot_tasks.is_empty() {
                self.outbox_idle_stamps.status_empty = None;
                self.outbox_idle_stamps.pending_force_status = false;
                let agent_dir = self
                    .runtime_events_outbox_dir
                    .parent()
                    .map(Path::to_path_buf)
                    .unwrap_or_else(|| self.runtime_events_outbox_dir.clone());
                let db_path = config.shipper_config.db_path.clone();
                let already_recorded = self.status_recorded.clone();
                let owner_evidence = self.last_status_owners.clone();
                let owner_evidence_fresh =
                    status_owner_snapshot_is_fresh(self.last_status_owners_at);
                let owner_refresh_cursor = self.status_owner_refresh_cursor;
                let managed_refresh_cursor = self.managed_owner_refresh_cursor;
                self.status_slot_tasks.spawn_blocking(move || {
                    let started = Instant::now();
                    let dir = crate::status_slot::status_slot_dir(&agent_dir);
                    let dir_stamp_before = dir_activity_stamp(&dir);
                    let slots = crate::status_slot::read_all(&dir);
                    let claims = read_status_slot_claims_for_slots(&slots);
                    let (owners, owner_refresh_cursor, next_managed_refresh_cursor) =
                        status_owner_evidence_for_slots(
                            &slots,
                            &claims,
                            owner_evidence.as_ref(),
                            owner_evidence_fresh,
                            crate::heartbeat::machine_boot_id().as_deref(),
                            owner_refresh_cursor,
                            managed_refresh_cursor,
                        );
                    let slots = reconcile_status_slots(&dir, slots, &owners);
                    // The phase ledger is local truth, and the daemon is
                    // its single writer. Recording here is what lets a
                    // provider callback stop writing a file per frame.
                    let recorded =
                        record_status_slot_phases(db_path.as_deref(), &slots, &already_recorded);
                    StatusSlotResult {
                        slots,
                        recorded,
                        owner_refresh_cursor,
                        managed_owner_refresh_cursor: next_managed_refresh_cursor,
                        dir_stamp_before,
                        elapsed_ms: started.elapsed().as_millis() as u64,
                    }
                });
            } else if force_scan {
                self.outbox_idle_stamps.status_empty = None;
                self.outbox_idle_stamps.pending_force_status = true;
            }
        }
        let force_runtime = force_scan || self.outbox_idle_stamps.pending_force_runtime;
        let runtime_unchanged_empty = !force_runtime
            && self.outbox_idle_stamps.runtime_empty.is_some()
            && self.outbox_idle_stamps.runtime_empty
                == dir_activity_stamp(&self.runtime_events_outbox_dir);
        if !runtime_unchanged_empty {
            if self.runtime_collect_tasks.is_empty()
                && self.runtime_outbox_post_tasks.is_empty()
                && self
                    .runtime_outbox_retry_after
                    .map(|retry_at| Instant::now() >= retry_at)
                    .unwrap_or(true)
            {
                self.outbox_idle_stamps.runtime_empty = None;
                self.outbox_idle_stamps.pending_force_runtime = false;
                let runtime_events_outbox_dir = self.runtime_events_outbox_dir.clone();
                self.runtime_collect_tasks.spawn_blocking(move || {
                    let started = Instant::now();
                    let dir_stamp_before = dir_activity_stamp(&runtime_events_outbox_dir);
                    let pass =
                        outbox::collect_runtime_event_outbox_pass(&runtime_events_outbox_dir);
                    let measurement = heartbeat::RuntimeEventOutboxSnapshot {
                        pending_count: pass.pending_count,
                        pending_count_is_lower_bound: pass.pending_count_is_lower_bound,
                        inspected_count: pass.inspected_count,
                        saturated: pass.saturated,
                        oldest_pending_at: pass.oldest_pending_at.map(|at| at.to_rfc3339()),
                        observed_at: pass.observed_at.map(|at| at.to_rfc3339()),
                    };
                    RuntimeCollectResult {
                        posts: pass.posts,
                        measurement,
                        dir_stamp_before,
                        elapsed_ms: started.elapsed().as_millis() as u64,
                        saturated: pass.saturated,
                    }
                });
            } else if force_scan {
                self.outbox_idle_stamps.runtime_empty = None;
                self.outbox_idle_stamps.pending_force_runtime = true;
            }
        }
    }
}
