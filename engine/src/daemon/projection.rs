//! The local status projection behind engine-status.json and the heartbeat.

use super::*;

/// Open a debounce window for a phase-ledger write.
///
/// Returns true when the caller should arm the timer, false when a window is
/// already open and this write coalesces into it.
///
/// The window is fixed from the first write, not sliding. A sliding window
/// would be pushed out by every subsequent phase, and providers are chatty
/// enough — the Codex bridge posts a phase per item start, completion, and
/// thread-status change — that a busy turn could starve the rebuild
/// indefinitely, which is the failure this whole change exists to remove.
pub(super) fn arm_phase_projection(pending: &mut bool) -> bool {
    if *pending {
        return false;
    }
    *pending = true;
    true
}

/// Highest accepted phase-ledger revision, or None if the ledger is empty.
///
/// Cheap enough for the 100ms outbox tick: `session_phase_state` holds one row
/// per session. Reading a watermark rather than subscribing to a signal is what
/// makes this cover out-of-process writers — the Codex bridge, Console
/// adapters, and OpenCode all record phases from their own processes.
pub(super) fn latest_phase_watermark(conn: &rusqlite::Connection) -> Option<i64> {
    conn.query_row("SELECT MAX(revision) FROM session_phase_state", [], |row| {
        row.get::<_, Option<i64>>(0)
    })
    .ok()
    .flatten()
}

pub(super) fn maybe_start_projection_build(
    tasks: &mut JoinSet<ProjectionBuildResult>,
    input: ProjectionBuildInput,
) -> bool {
    if !tasks.is_empty() {
        return false;
    }
    let ProjectionBuildInput {
        generation,
        managed_observation_generation,
        managed_scan_partial,
        managed_snapshot_complete,
        managed_captured_at,
        unmanaged_snapshot_complete,
        db_path,
        parse_tracker,
        ship_stats,
        is_offline,
        last_ship_at,
        machine_id,
        managed,
        unmanaged,
        continuation,
        limiter,
        scheduler,
        archive_repair_mode,
        last_full_reconciled_at,
        mut session_snapshot_state,
    } = input;
    // Unobserved continuation evidence is not an observed empty set. The
    // periodic observer retries a full pass until the first cache exists.
    let Some(continuation) = continuation else {
        return false;
    };
    tasks.spawn_blocking(move || {
        let started = Instant::now();
        let result = crate::state::db::open_connection(&db_path)
            .map_err(|error| error.to_string())
            .map(|conn| {
                let mut projection = build_local_status_projection_with_omp(
                    &conn,
                    &parse_tracker,
                    &ship_stats,
                    is_offline,
                    &last_ship_at,
                    &machine_id,
                    &managed.codex,
                    &managed.antigravity,
                    &managed.claude,
                    &managed.opencode,
                    &managed.cursor,
                    &managed.pi,
                    &managed.omp,
                    &unmanaged,
                    &continuation,
                    managed_snapshot_complete,
                    &managed_captured_at,
                    unmanaged_snapshot_complete,
                    Some(limiter),
                    Some(scheduler),
                    archive_repair_mode,
                    &mut session_snapshot_state,
                );
                projection.payload.managed_sessions.sort_by(|left, right| {
                    left.provider
                        .cmp(&right.provider)
                        .then_with(|| left.session_id.cmp(&right.session_id))
                });
                projection.set_last_reconciled_at(last_full_reconciled_at);
                (projection, session_snapshot_state)
            });
        ProjectionBuildResult {
            generation,
            managed_observation_generation,
            managed_scan_partial,
            managed_snapshot_complete,
            unmanaged_snapshot_complete,
            result,
            elapsed_ms: started.elapsed().as_millis() as u64,
        }
    });
    true
}

#[allow(clippy::too_many_arguments)]
#[cfg(test)]
pub(super) fn build_local_status_projection(
    conn: &rusqlite::Connection,
    parse_tracker: &RecentIssueTracker,
    ship_stats: &RecentShipStatsTracker,
    is_offline: bool,
    last_ship_at: &Option<String>,
    machine_id: &str,
    observations: &[managed_bridge_scan::CodexBridgeObservation],
    antigravity_observations: &[managed_antigravity_scan::AntigravityHookObservation],
    claude_observations: &[managed_claude_scan::ClaudeChannelObservation],
    opencode_observations: &[managed_opencode_scan::OpenCodeServerObservation],
    cursor_observations: &[managed_cursor_helm_scan::CursorHelmObservation],
    pi_observations: &[managed_pi_helm_scan::PiHelmObservation],
    unmanaged_session_bindings: &[heartbeat::UnmanagedSessionBinding],
    managed_snapshot_complete: bool,
    managed_captured_at: &str,
    unmanaged_snapshot_complete: bool,
    limiter_snapshot: Option<crate::scheduler::LimiterSnapshot>,
    scheduler_snapshot: Option<crate::scheduler::SchedulerSnapshot>,
    archive_repair_mode: ArchiveRepairMode,
    session_snapshot_state: &mut SessionSnapshotState,
) -> heartbeat::StatusFileProjection {
    build_local_status_projection_with_omp(
        conn,
        parse_tracker,
        ship_stats,
        is_offline,
        last_ship_at,
        machine_id,
        observations,
        antigravity_observations,
        claude_observations,
        opencode_observations,
        cursor_observations,
        pi_observations,
        &[],
        unmanaged_session_bindings,
        &[],
        managed_snapshot_complete,
        managed_captured_at,
        unmanaged_snapshot_complete,
        limiter_snapshot,
        scheduler_snapshot,
        archive_repair_mode,
        session_snapshot_state,
    )
}

#[allow(clippy::too_many_arguments)]
pub(super) fn build_local_status_projection_with_omp(
    conn: &rusqlite::Connection,
    parse_tracker: &RecentIssueTracker,
    ship_stats: &RecentShipStatsTracker,
    is_offline: bool,
    last_ship_at: &Option<String>,
    machine_id: &str,
    observations: &[managed_bridge_scan::CodexBridgeObservation],
    antigravity_observations: &[managed_antigravity_scan::AntigravityHookObservation],
    claude_observations: &[managed_claude_scan::ClaudeChannelObservation],
    opencode_observations: &[managed_opencode_scan::OpenCodeServerObservation],
    cursor_observations: &[managed_cursor_helm_scan::CursorHelmObservation],
    pi_observations: &[managed_pi_helm_scan::PiHelmObservation],
    omp_observations: &[managed_omp_helm_scan::OmpHelmObservation],
    unmanaged_session_bindings: &[heartbeat::UnmanagedSessionBinding],
    continuation: &[managed_resume_scan::ResumeContractObservation],
    managed_snapshot_complete: bool,
    managed_captured_at: &str,
    unmanaged_snapshot_complete: bool,
    limiter_snapshot: Option<crate::scheduler::LimiterSnapshot>,
    scheduler_snapshot: Option<crate::scheduler::SchedulerSnapshot>,
    archive_repair_mode: ArchiveRepairMode,
    session_snapshot_state: &mut SessionSnapshotState,
) -> heartbeat::StatusFileProjection {
    let stats = heartbeat::HeartbeatStats {
        conn,
        parse_tracker,
        ship_stats,
        is_offline,
        last_ship_at: last_ship_at.clone(),
    };
    let mut payload = heartbeat::HeartbeatPayload::build(&stats);
    // Read fresh rather than threading through the loop, same as the archive
    // control below. Absent until the first check completes.
    payload.update = crate::update::read_status();
    let archive_control = read_archive_repair_control();
    payload.adaptive_backlog_limiter = limiter_snapshot;
    payload.ship_scheduler = scheduler_snapshot;
    apply_archive_repair_control(&mut payload, &archive_control, archive_repair_mode);
    let sealed_current = payload.history_import.state == "current";
    let background_active = history_runtime_work_active(
        sealed_current,
        payload.archive_backlog.pending_ranges,
        payload.ship_scheduler.as_ref(),
    );
    payload.history_import.apply_runtime_state(
        is_offline,
        payload.archive_backlog.mode == "paused",
        background_active,
    );
    let now = chrono::Utc::now();
    payload.managed_sessions = heartbeat::leases_from_observations(machine_id, observations, now);
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_claude_channel_observations(
            machine_id,
            claude_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_opencode_server_observations(
            machine_id,
            opencode_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_cursor_helm_observations(
            machine_id,
            cursor_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_antigravity_observations(
            machine_id,
            antigravity_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_pi_helm_observations(
            machine_id,
            pi_observations,
            now,
        ));
    payload
        .managed_sessions
        .extend(heartbeat::leases_from_omp_helm_observations(
            machine_id,
            omp_observations,
            now,
        ));
    payload.managed_sessions.sort_by(|a, b| {
        a.provider
            .cmp(&b.provider)
            .then_with(|| a.session_id.cmp(&b.session_id))
    });
    payload.unmanaged_session_bindings =
        heartbeat::filter_unmanaged_bindings_owned_by_managed_observations_with_omp(
            unmanaged_session_bindings.to_vec(),
            observations,
            claude_observations,
            opencode_observations,
            cursor_observations,
            pi_observations,
            omp_observations,
        );
    // Keep cached Shadow bindings as timestamped raw evidence, but do not put
    // them in the canonical current-session view until that process scope has
    // been freshly observed. The incomplete scope remains fail-open for server
    // reconciliation and prevents old process identity from becoming liveness.
    let resolved_unmanaged_bindings: &[heartbeat::UnmanagedSessionBinding] =
        if unmanaged_snapshot_complete {
            &payload.unmanaged_session_bindings
        } else {
            &[]
        };
    // Compute the fresh activity ledger once and feed the raw rows into the
    // typed evidence envelope. Activity facts remain independent of control
    // leases and of the resolved presentation projection below.
    let (phase_ledger, ledger_status) =
        match crate::state::session_phase::SessionPhaseStore::new(conn)
            .fresh_rows(chrono::Utc::now())
        {
            Ok(rows) => (rows, heartbeat::PhaseLedgerStatus::Ok),
            Err(err) => {
                tracing::warn!(
                    error = %err,
                    "failed to read fresh phase_ledger rows for engine-status.json"
                );
                (
                    Vec::new(),
                    heartbeat::PhaseLedgerStatus::ReadFailed(err.to_string()),
                )
            }
        };
    payload.machine_evidence = Some(heartbeat::machine_evidence_from_observations_with_omp(
        machine_id,
        observations,
        antigravity_observations,
        claude_observations,
        opencode_observations,
        cursor_observations,
        pi_observations,
        omp_observations,
        unmanaged_session_bindings,
        &phase_ledger,
        managed_snapshot_complete,
        unmanaged_snapshot_complete,
        now,
        Some(continuation),
        current_evidence_rotation(),
    ));
    if let Some(evidence) = payload.machine_evidence.as_mut() {
        // The scope says *when* it was enumerated; the envelope only says when
        // it was sent.
        heartbeat::stamp_scope_capture_time(evidence, &managed_captured_at);
    }
    payload.sessions = heartbeat::resolved_sessions_from_observations_with_omp(
        &payload.managed_sessions,
        resolved_unmanaged_bindings,
        observations,
        claude_observations,
        opencode_observations,
        cursor_observations,
        pi_observations,
        omp_observations,
    );
    heartbeat::apply_machine_boot_identity(&mut payload.sessions);
    heartbeat::apply_local_titles(conn, &mut payload.sessions);
    session_snapshot_state.annotate(&mut payload);
    heartbeat::build_status_file_projection(payload, phase_ledger, ledger_status)
}

pub(super) fn history_runtime_work_active(
    sealed_current: bool,
    archive_pending_ranges: usize,
    scheduler: Option<&crate::scheduler::SchedulerSnapshot>,
) -> bool {
    (!sealed_current && archive_pending_ranges > 0)
        || scheduler.is_some_and(|scheduler| {
            let live_or_retry = scheduler.ready_live > 0
                || scheduler.in_flight_live > 0
                || scheduler.ready_retry > 0
                || scheduler.in_flight_retry > 0;
            live_or_retry
                || (!sealed_current && (scheduler.ready_scan > 0 || scheduler.in_flight_scan > 0))
        })
}

impl DaemonState {
    pub(super) fn on_projection_build_done(
        &mut self,
        config: &ConnectConfig,
        projection_build_result: Option<Result<ProjectionBuildResult, tokio::task::JoinError>>,
    ) {
        match projection_build_result {
            Some(Ok(result)) => {
                let is_current = self.managed_observation_valid
                    && result.generation == self.projection_generation
                    && result.managed_observation_generation == self.managed_observation_generation
                    && result.managed_scan_partial == self.last_projected_managed_scan_partial
                    && result.managed_snapshot_complete
                        == self.last_projected_managed_snapshot_complete
                    && result.unmanaged_snapshot_complete
                        == self.last_projected_unmanaged_snapshot_complete;
                match result.result {
                    Ok((mut projection, next_snapshot_state)) => {
                        if result.elapsed_ms > LOCAL_STATUS_BUDGET_MS {
                            self.projection_over_budget_ticks =
                                self.projection_over_budget_ticks.saturating_add(1);
                            self.projection_worst_elapsed_ms =
                                self.projection_worst_elapsed_ms.max(result.elapsed_ms);
                            let due = self.projection_budget_reported_at.is_none_or(|at| {
                                at.elapsed() >= LOCAL_STATUS_BUDGET_REPORT_INTERVAL
                            });
                            if due {
                                tracing::warn!(
                                    over_budget_ticks = self.projection_over_budget_ticks,
                                    worst_elapsed_ms = self.projection_worst_elapsed_ms,
                                    budget_ms = LOCAL_STATUS_BUDGET_MS,
                                    "Local status projection exceeded background budget"
                                );
                                self.projection_over_budget_ticks = 0;
                                self.projection_worst_elapsed_ms = 0;
                                self.projection_budget_reported_at = Some(Instant::now());
                            }
                        }
                        if !is_current {
                            tracing::debug!(
                                generation = result.generation,
                                latest_generation = self.projection_generation,
                                managed_scan_partial = result.managed_scan_partial,
                                latest_managed_scan_partial =
                                    self.last_projected_managed_scan_partial,
                                unmanaged_snapshot_complete = result.unmanaged_snapshot_complete,
                                latest_unmanaged_snapshot_complete =
                                    self.last_projected_unmanaged_snapshot_complete,
                                "Discarded stale local status projection"
                            );
                        } else {
                            self.session_snapshot_state = next_snapshot_state;
                            if result.managed_scan_partial {
                                self.managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::failed(
                                        "provider_state_partial",
                                    );
                            } else if result.managed_snapshot_complete
                                && result.unmanaged_snapshot_complete
                                && self.managed_observation_scan_tasks.is_empty()
                                && self.unmanaged_binding_refresh_tasks.is_empty()
                                && !self.unmanaged_binding_refresh_failed
                            {
                                // Only a complete paired observation clears a failure.
                                // A cached/managed-only projection is not recovery.
                                self.managed_reconciliation =
                                    heartbeat::ProjectionReconciliation::idle();
                            }
                            self.shipping_progress.observe_pending_work(
                                heartbeat::payload_has_pending_work(&projection.payload)
                                    || known_pending_local_work(
                                        &self.scheduler,
                                        &self.deferred_retries,
                                        archive_repair_is_paused(config.archive_repair_mode),
                                    ),
                                Instant::now(),
                            );
                            projection.set_heartbeat_transport(self.heartbeat_transport.clone());
                            projection.set_host_link(self.host_link.snapshot());
                            projection
                                .set_runtime_event_outbox(self.latest_runtime_event_outbox.clone());
                            if let Some(evidence) = projection.payload.machine_evidence.as_ref() {
                                self.acknowledged_machine_evidence
                                    .prune_to_current(&evidence.candidate_identities);
                            } else {
                                self.acknowledged_machine_evidence.prune_to_current(&[]);
                            }
                            heartbeat::write_status_file(
                                &mut projection,
                                serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                                &self.managed_reconciliation,
                                &mut self.shipping_progress,
                                self.offline.is_offline,
                                &self.status_path,
                            );
                            let payload = projection.payload.clone();
                            self.last_status_projection = Some(projection);
                            let signature = runtime_truth_signature(&payload);
                            if !self.offline.is_offline {
                                if runtime_truth_changed(
                                    self.last_runtime_truth_signature.as_deref(),
                                    &signature,
                                ) {
                                    let now = Instant::now();
                                    let due =
                                        truth_heartbeat_due_at(self.last_truth_heartbeat_at, now);
                                    if self.heartbeat_post_tasks.is_empty() && due <= now {
                                        self.heartbeat_transport
                                            .record_attempt(chrono::Utc::now().to_rfc3339());
                                        publish_heartbeat_transport_status(
                                            &self.heartbeat_transport,
                                            &mut self.last_status_projection,
                                            serde_json::to_value(
                                                self.control_channel_status.snapshot(),
                                            )
                                            .ok(),
                                            &self.managed_reconciliation,
                                            &mut self.shipping_progress,
                                            self.offline.is_offline,
                                            &self.host_link,
                                            &self.status_path,
                                        );
                                        spawn_heartbeat_post(
                                            &mut self.heartbeat_post_tasks,
                                            self.client.clone(),
                                            payload,
                                            signature.clone(),
                                            "runtime_truth_change",
                                            &mut self.acknowledged_machine_evidence,
                                        );
                                        self.last_truth_heartbeat_at = Some(now);
                                        self.last_runtime_truth_signature = Some(signature);
                                        self.pending_truth_heartbeat = None;
                                    } else {
                                        self.pending_truth_heartbeat =
                                            Some(PendingTruthHeartbeat { payload, signature });
                                    }
                                } else {
                                    self.pending_truth_heartbeat = None;
                                }
                            }
                        }
                    }
                    Err(error) => {
                        tracing::warn!(error = %error, "Local status projection build failed");
                        if is_current {
                            self.managed_reconciliation =
                                heartbeat::ProjectionReconciliation::failed("projection_build");
                        }
                    }
                }
            }
            Some(Err(error)) => {
                tracing::warn!(error = %error, "Local status projection task failed");
                if !self.projection_build_pending {
                    self.managed_reconciliation =
                        heartbeat::ProjectionReconciliation::failed("projection_build");
                }
            }
            None => {}
        }

        if self.projection_build_pending && self.managed_observation_valid {
            self.projection_build_pending = false;
            let input = ProjectionBuildInput {
                generation: self.projection_generation,
                managed_observation_generation: self.managed_observation_generation,
                managed_scan_partial: self.last_projected_managed_scan_partial,
                managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                managed_captured_at: self.last_managed_captured_at.clone(),
                unmanaged_snapshot_complete: self.last_projected_unmanaged_snapshot_complete,
                db_path: self.projection_db_path.clone(),
                parse_tracker: self.parse_tracker.clone(),
                ship_stats: self.ship_stats.clone(),
                is_offline: self.offline.is_offline,
                last_ship_at: self.last_ship_at.clone(),
                machine_id: config.shipper_config.machine_name.clone(),
                managed: self.last_projected_managed_observations.clone(),
                unmanaged: self
                    .last_unmanaged_session_bindings
                    .clone()
                    .unwrap_or_default(),
                limiter: self.adaptive_limiter.snapshot(),
                scheduler: self.scheduler.snapshot(),
                archive_repair_mode: config.archive_repair_mode,
                last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                continuation: self.last_resume_contracts.clone(),
                session_snapshot_state: self.session_snapshot_state.clone(),
            };
            let _ = maybe_start_projection_build(&mut self.projection_build_tasks, input);
        }
    }

    pub(super) fn on_phase_projection_due(&mut self, config: &ConnectConfig) {
        self.phase_projection_pending = false;
        // Only after the first full projection: before that the
        // managed snapshot is empty, and projecting would ship a
        // sessionless digest that the first real scan immediately
        // replaces.
        if self.last_status_projection.is_some() && self.managed_observation_valid {
            let input = ProjectionBuildInput {
                generation: self.projection_generation,
                managed_observation_generation: self.managed_observation_generation,
                managed_scan_partial: self.last_projected_managed_scan_partial,
                managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                managed_captured_at: self.last_managed_captured_at.clone(),
                unmanaged_snapshot_complete: self.last_projected_unmanaged_snapshot_complete,
                db_path: self.projection_db_path.clone(),
                parse_tracker: self.parse_tracker.clone(),
                ship_stats: self.ship_stats.clone(),
                is_offline: self.offline.is_offline,
                last_ship_at: self.last_ship_at.clone(),
                machine_id: config.shipper_config.machine_name.clone(),
                managed: self.last_projected_managed_observations.clone(),
                unmanaged: self
                    .last_unmanaged_session_bindings
                    .clone()
                    .unwrap_or_default(),
                limiter: self.adaptive_limiter.snapshot(),
                scheduler: self.scheduler.snapshot(),
                archive_repair_mode: config.archive_repair_mode,
                last_full_reconciled_at: self.last_full_reconciled_at.clone(),
                continuation: self.last_resume_contracts.clone(),
                session_snapshot_state: self.session_snapshot_state.clone(),
            };
            if !maybe_start_projection_build(&mut self.projection_build_tasks, input) {
                self.projection_build_pending = true;
            }
        }
    }

    pub(super) async fn on_local_status_tick(&mut self) {
        if let Some(gap) = self
            .wake_gap_detector
            .observe(SystemTime::now(), Instant::now())
        {
            self.shipping_progress.reset_after_sleep(Instant::now());
            tracing::info!(
                wake_gap_ms = gap.as_millis() as u64,
                "Detected system wake gap"
            );
            if maybe_start_managed_observation_scan(
                self.projection_db_path.clone(),
                &mut self.managed_observation_scan_tasks,
                "wake",
                true,
                &self.last_managed_observations,
            ) {
                self.managed_full_reconciliation_not_before =
                    Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                self.managed_reconciliation
                    .start("wake", chrono::Utc::now().to_rfc3339());
            } else {
                self.pending_wake_reconciliation = true;
                self.managed_reconciliation
                    .start("wake", chrono::Utc::now().to_rfc3339());
            }
        }
        let host_link_status = self.host_link.snapshot();
        if matches!(host_link_status.state.as_str(), "updating" | "slow_update")
            && self.host_link_poll_tasks.len() < 3
        {
            let client = self.client.clone();
            self.host_link_poll_tasks
                .spawn_local(async move { client.poll_runtime_admission().await });
        }
        if let Some(projection) = self.last_status_projection.as_mut() {
            projection.set_heartbeat_transport(self.heartbeat_transport.clone());
            projection.set_host_link(host_link_status.clone());
            heartbeat::write_status_file(
                projection,
                serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                &self.managed_reconciliation,
                &mut self.shipping_progress,
                self.offline.is_offline,
                &self.status_path,
            );
        } else {
            heartbeat::refresh_existing_status_pulse(
                &self.managed_reconciliation,
                &mut self.shipping_progress,
                self.offline.is_offline,
                &self.status_path,
                &self.heartbeat_transport,
                Some(&host_link_status),
            );
        }
    }
}
