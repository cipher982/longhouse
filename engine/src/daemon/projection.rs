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
