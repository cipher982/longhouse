//! Managed-session observation scans and the snapshot the projection reads.

use super::*;

/// What one managed scan pass may claim to the Runtime Host.
///
/// `partial` is diagnostic; `complete` is the certificate. A pass certifies only
/// what it actually enumerated in this generation -- pass kind is irrelevant,
/// and an unresolved provider directory or an entry carried forward unaccounted
/// for is a failure to enumerate, not a quiet empty result.
pub(super) fn managed_scan_certificate(result: &ManagedObservationScanResult) -> (bool, bool) {
    (result.retained_stale_rows > 0, result.enumeration_complete)
}

#[derive(Clone, Default)]
pub(super) struct ManagedObservationScanResult {
    pub(super) reason: &'static str,
    pub(super) full_reconciliation: bool,
    /// Whether this pass enumerated every managed provider state directory and
    /// accounted for every entry in it. This -- not the pass kind -- is what the
    /// Runtime Host needs before it may act on absence: a certificate is a claim
    /// about a moment, not a standing property of the machine.
    pub(super) enumeration_complete: bool,
    /// Wall clock of the enumeration itself, carried into the evidence scope.
    pub(super) captured_at: String,
    pub(super) process_inventory_valid: bool,
    pub(super) process_inventory: Vec<unmanaged_bindings::ProcessInfo>,
    pub(super) codex_observations: Vec<managed_bridge_scan::CodexBridgeObservation>,
    pub(super) antigravity_observations: Vec<managed_antigravity_scan::AntigravityHookObservation>,
    pub(super) claude_observations: Vec<managed_claude_scan::ClaudeChannelObservation>,
    pub(super) opencode_observations: Vec<managed_opencode_scan::OpenCodeServerObservation>,
    pub(super) cursor_observations: Vec<managed_cursor_helm_scan::CursorHelmObservation>,
    pub(super) pi_observations: Vec<managed_pi_helm_scan::PiHelmObservation>,
    pub(super) omp_observations: Vec<managed_omp_helm_scan::OmpHelmObservation>,
    pub(super) continuation: Option<Arc<[managed_resume_scan::ResumeContractObservation]>>,
    /// Managed provider processes whose session is gone. Identified in the
    /// blocking scan, reaped by the async consumer.
    pub(super) orphan_processes: Vec<crate::managed_process_janitor::OrphanProcess>,
    pub(super) process_inventory_ms: u64,
    pub(super) codex_elapsed_ms: u64,
    pub(super) antigravity_elapsed_ms: u64,
    pub(super) claude_elapsed_ms: u64,
    pub(super) opencode_elapsed_ms: u64,
    pub(super) cursor_elapsed_ms: u64,
    pub(super) pi_elapsed_ms: u64,
    pub(super) omp_elapsed_ms: u64,
    pub(super) retained_stale_rows: usize,
    pub(super) elapsed_ms: u64,
    pub(super) status_owners: StatusOwnerEvidence,
    pub(super) status_owner_snapshot_at: Option<Instant>,
}

#[derive(Clone, Default, PartialEq, Eq)]
pub(super) struct ManagedObservationSnapshot {
    pub(super) codex: Vec<managed_bridge_scan::CodexBridgeObservation>,
    pub(super) antigravity: Vec<managed_antigravity_scan::AntigravityHookObservation>,
    pub(super) claude: Vec<managed_claude_scan::ClaudeChannelObservation>,
    pub(super) opencode: Vec<managed_opencode_scan::OpenCodeServerObservation>,
    pub(super) cursor: Vec<managed_cursor_helm_scan::CursorHelmObservation>,
    pub(super) pi: Vec<managed_pi_helm_scan::PiHelmObservation>,
    pub(super) omp: Vec<managed_omp_helm_scan::OmpHelmObservation>,
}

pub(super) struct ProjectionBuildInput {
    pub(super) generation: u64,
    // Independent from the Shadow refresh generation: a slow optional refresh
    // must not invalidate fresh managed observations, while an older build
    // must not publish over a newer managed scan.
    pub(super) managed_observation_generation: u64,
    pub(super) managed_scan_partial: bool,
    pub(super) managed_snapshot_complete: bool,
    /// Wall clock of the enumeration the managed observations came from. The
    /// evidence scope carries it so the Runtime Host can tell how old the claim
    /// is: a certificate says "as of this moment", not "always".
    pub(super) managed_captured_at: String,
    pub(super) unmanaged_snapshot_complete: bool,
    pub(super) db_path: PathBuf,
    pub(super) parse_tracker: RecentIssueTracker,
    pub(super) ship_stats: RecentShipStatsTracker,
    pub(super) is_offline: bool,
    pub(super) last_ship_at: Option<String>,
    pub(super) machine_id: String,
    pub(super) managed: ManagedObservationSnapshot,
    pub(super) unmanaged: Vec<heartbeat::UnmanagedSessionBinding>,
    pub(super) continuation: Option<Arc<[managed_resume_scan::ResumeContractObservation]>>,
    pub(super) limiter: crate::scheduler::LimiterSnapshot,
    pub(super) scheduler: crate::scheduler::SchedulerSnapshot,
    pub(super) archive_repair_mode: ArchiveRepairMode,
    pub(super) last_full_reconciled_at: Option<String>,
    pub(super) session_snapshot_state: SessionSnapshotState,
}

pub(super) struct ProjectionBuildResult {
    pub(super) generation: u64,
    pub(super) managed_observation_generation: u64,
    pub(super) managed_scan_partial: bool,
    pub(super) managed_snapshot_complete: bool,
    pub(super) unmanaged_snapshot_complete: bool,
    pub(super) result: Result<(heartbeat::StatusFileProjection, SessionSnapshotState), String>,
    pub(super) elapsed_ms: u64,
}

impl ManagedObservationSnapshot {
    pub(super) fn from_result(result: &ManagedObservationScanResult) -> Self {
        Self {
            codex: result.codex_observations.clone(),
            antigravity: result.antigravity_observations.clone(),
            claude: result.claude_observations.clone(),
            opencode: result.opencode_observations.clone(),
            cursor: result.cursor_observations.clone(),
            pi: result.pi_observations.clone(),
            omp: result.omp_observations.clone(),
        }
    }

    pub(super) fn contains_state_file(&self, path: &Path) -> bool {
        self.codex.iter().any(|row| row.state_file == path)
            || self.antigravity.iter().any(|row| row.state_file == path)
            || self.claude.iter().any(|row| row.state_file == path)
            || self.opencode.iter().any(|row| row.state_file == path)
            || self.cursor.iter().any(|row| row.state_file == path)
            || self.pi.iter().any(|row| row.state_file == path)
            || self.omp.iter().any(|row| row.state_file == path)
    }

    pub(super) fn projection_equivalent(&self, other: &Self) -> bool {
        let mut left = self.clone();
        let mut right = other.clone();
        for snapshot in [&mut left, &mut right] {
            for row in &mut snapshot.codex {
                row.updated_at.clear();
                row.active_turn_id = None;
                row.last_turn_status = None;
            }
            for row in &mut snapshot.antigravity {
                row.updated_at.clear();
            }
            for row in &mut snapshot.claude {
                row.updated_at.clear();
            }
            for row in &mut snapshot.opencode {
                row.updated_at.clear();
            }
            for row in &mut snapshot.cursor {
                row.updated_at.clear();
            }
            for row in &mut snapshot.pi {
                row.updated_at.clear();
            }
            for row in &mut snapshot.omp {
                row.updated_at.clear();
            }
        }
        left == right
    }

    pub(super) fn current_only(&self) -> Self {
        Self {
            // Every provider below keeps a dead row when it still names a run,
            // for the reason spelled out for Cursor: an abrupt owner loss makes
            // the observation non-live, and dropping the row here would also drop
            // the only exact pid/start-time evidence that can close the run on
            // the next complete process snapshot. Without it nothing retires the
            // run, `live_session_runs.ended_at IS NULL` keeps reading as
            // "executing", and resume refuses the session its own recovery path
            // (5,472 such runs on the dogfood catalog on 2026-09-25).
            //
            // Retention is evidence-only. Lease projection filters liveness, so
            // a retained row cannot make a dead session's control visible, and
            // `resolved_sessions_from_observations` is built from those leases.
            codex: self
                .codex
                .iter()
                .filter(|row| {
                    row.bridge_alive
                        || row.app_server_alive
                        || row.has_tui_attachment
                        || row.run_id.is_some()
                })
                .cloned()
                .collect(),
            antigravity: self.antigravity.clone(),
            claude: self
                .claude
                .iter()
                .filter(|row| row.claude_alive || row.bridge_alive || row.run_id.is_some())
                .cloned()
                .collect(),
            opencode: self
                .opencode
                .iter()
                .filter(|row| row.server_alive || row.has_tui_attachment || row.run_id.is_some())
                .cloned()
                .collect(),
            cursor: self
                .cursor
                .iter()
                .filter(|row| row.live || row.run_id.is_some())
                .cloned()
                .collect(),
            pi: self
                .pi
                .iter()
                .filter(|row| row.live || row.run_id.is_some())
                .cloned()
                .collect(),
            omp: self
                .omp
                .iter()
                .filter(|row| row.live || row.run_id.is_some())
                .cloned()
                .collect(),
        }
    }
}

pub(super) fn partition_managed_state_events(
    events: Vec<WatcherEvent>,
    managed_state_dirs: &[PathBuf],
) -> (Vec<PathBuf>, Vec<WatcherEvent>) {
    let mut managed = Vec::new();
    let mut transcripts = Vec::new();
    for event in events {
        if managed_state_dirs
            .iter()
            .any(|state_dir| event.path.starts_with(state_dir))
        {
            managed.push(event.path);
        } else {
            transcripts.push(event);
        }
    }
    (managed, transcripts)
}

pub(super) fn managed_state_changes_require_full_reconciliation(
    observations: &ManagedObservationSnapshot,
    paths: &[PathBuf],
) -> bool {
    paths
        .iter()
        .any(|path| !observations.contains_state_file(path))
}

/// Whether the certificate the next beat would carry is about to age out.
///
/// A certificate is per-generation, and the Runtime Host enforces its own age
/// bound, so an idle machine must still re-enumerate inside that bound or its
/// beats stop carrying absence authority.
pub(super) fn certificate_needs_refresh(certified_at: Option<Instant>, now: Instant) -> bool {
    match certified_at {
        None => true,
        Some(certified_at) => {
            now.saturating_duration_since(certified_at)
                > Duration::from_secs(MANAGED_CERTIFICATE_KEEPALIVE_SECS)
        }
    }
}

pub(super) fn managed_full_reconciliation_ready(
    pending: bool,
    scan_idle: bool,
    now: Instant,
    not_before: Instant,
) -> bool {
    pending && scan_idle && now >= not_before
}

pub(super) fn managed_process_pids_from_observations(
    codex: &[managed_bridge_scan::CodexBridgeObservation],
    claude: &[managed_claude_scan::ClaudeChannelObservation],
    opencode: &[managed_opencode_scan::OpenCodeServerObservation],
    cursor: &[managed_cursor_helm_scan::CursorHelmObservation],
    pi: &[managed_pi_helm_scan::PiHelmObservation],
    omp: &[managed_omp_helm_scan::OmpHelmObservation],
) -> HashSet<u32> {
    let mut pids = HashSet::new();
    for observation in codex {
        if observation.bridge_alive {
            pids.insert(observation.bridge_pid);
        }
        if observation.app_server_alive {
            pids.extend(observation.app_server_pid);
        }
    }
    for observation in claude {
        if observation.claude_alive {
            pids.extend(observation.claude_pid);
        }
        if observation.bridge_alive {
            pids.extend(observation.bridge_pid);
        }
    }
    for observation in opencode {
        if observation.server_alive {
            pids.extend(observation.pid);
        }
    }
    for observation in cursor {
        if observation.live {
            pids.extend(observation.launcher_pid);
            pids.extend(observation.cursor_pid);
        }
    }
    for observation in pi {
        if observation.launcher_alive {
            pids.extend(observation.launcher_pid);
        }
        if observation.provider_alive {
            pids.extend(observation.provider_pid);
        }
    }
    for observation in omp {
        if observation.launcher_alive {
            pids.extend(observation.launcher_pid);
        }
        if observation.provider_alive {
            pids.extend(observation.provider_pid);
        }
    }
    pids
}

pub(super) fn codex_contract_must_be_retained(
    observation: &managed_bridge_scan::CodexBridgeObservation,
) -> bool {
    observation.bridge_alive
        || observation.app_server_alive
        || observation.has_tui_attachment
        || (observation.status.eq_ignore_ascii_case("stopped")
            && observation.stopped_at.is_some()
            && matches!(
                observation.terminal_state.as_deref(),
                Some("session_ended" | "process_gone")
            )
            && matches!(
                observation.terminal_reason.as_deref(),
                Some(
                    "user_closed"
                        | "bridge_stop"
                        | "owner_gone"
                        | "provider_exit"
                        | "process_gone"
                        | "provider_signal"
                )
            )
            && observation
                .thread_id
                .as_deref()
                .is_some_and(|thread| !thread.trim().is_empty())
            && observation
                .thread_path
                .as_deref()
                .is_some_and(|path| Path::new(path).is_file())
            && observation
                .cwd
                .as_deref()
                .is_some_and(|cwd| Path::new(cwd).is_dir()))
}

pub(super) fn maybe_start_managed_observation_scan(
    db_path: PathBuf,
    scan_tasks: &mut JoinSet<ManagedObservationScanResult>,
    reason: &'static str,
    full_reconciliation: bool,
    previous: &ManagedObservationSnapshot,
) -> bool {
    if !scan_tasks.is_empty() {
        return false;
    }

    let previous = previous.clone();
    scan_tasks.spawn_blocking(move || {
        // Claims are projected **before** any provider source is enumerated: a
        // discovery pass that ran first would mint a Shadow session for a path a
        // live managed session is about to own. This is the only place the
        // ordering is guaranteed, so it happens here rather than after the scan.
        match crate::managed_source_claim::project_claims(&db_path) {
            Ok(report) if report.applied > 0 => tracing::info!(
                applied = report.applied,
                failed = report.failed,
                "Projected managed source claims before discovery"
            ),
            Ok(report) if report.failed > 0 => tracing::warn!(
                failed = report.failed,
                "Some managed source claims could not be projected"
            ),
            Ok(_) => {}
            Err(error) => tracing::warn!(
                error = %format!("{error:#}"),
                "Managed source claim projection failed; discovery runs without it"
            ),
        }
        let previous = if full_reconciliation {
            previous
        } else {
            previous.current_only()
        };
        // The enumeration's own clock. The evidence envelope is stamped when it
        // is built, which can be later than the scan it describes, and a stale
        // claim that reads as fresh is worse than no claim.
        let captured_at = chrono::Utc::now().to_rfc3339();
        let mut unresolved_state_dirs = 0_usize;
        let started = Instant::now();
        let status_claims = read_status_slot_claims();
        retry_pending_terminal_claim_handoffs();
        let process_started = Instant::now();
        let process_inventory = crate::process_identity::try_collect_process_facts_by_pid();
        let process_inventory_valid = process_inventory.is_some();
        let process_inventory_snapshot_at = Instant::now();
        let process_facts = process_inventory.unwrap_or_default();
        let unmanaged_process_inventory = process_facts
            .values()
            .filter_map(|fact| {
                Some(unmanaged_bindings::ProcessInfo {
                    pid: fact.pid,
                    start_time: fact.start_time?,
                    start_time_key: fact.lstart.clone(),
                    command: fact.command.clone(),
                })
            })
            .collect();
        let process_inventory_ms = process_started.elapsed().as_millis() as u64;
        let codex_started = Instant::now();
        let (mut codex_observations, unresolved) =
            match managed_bridge_scan::default_codex_bridge_state_dir() {
                Some(state_dir) => (
                    managed_bridge_scan::collect_observations_from(&state_dir, &process_facts),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let codex_elapsed_ms = codex_started.elapsed().as_millis() as u64;

        // Republish terminal events that committed durably but never reached
        // the outbox — the crash window between the bridge's state rename and
        // its enqueue, plus any enqueue failure. Idempotent: the dedupe key is
        // persisted with the stopped fact, so a republish that races the
        // original collapses server-side.
        //
        // Full-reconciliation ticks only. This recovers from failures, not
        // steady state, and the incremental tick sees only previously-known
        // paths anyway.
        if full_reconciliation {
            if let Ok(outbox_dir) = crate::config::get_agent_runtime_events_outbox_dir() {
                let stopped_state_files = codex_observations
                    .iter()
                    .filter(|observation| observation.status.trim().eq_ignore_ascii_case("stopped"))
                    .map(|observation| observation.state_file.clone())
                    .collect::<Vec<_>>();
                let republished = crate::codex_teardown::reconcile_terminal_event_paths(
                    &stopped_state_files,
                    &outbox_dir,
                );
                if republished > 0 {
                    tracing::info!(
                        republished,
                        "republished committed-but-unpublished codex terminal events"
                    );
                }
            }
        }

        let antigravity_started = Instant::now();
        let (mut antigravity_observations, unresolved) =
            match managed_antigravity_scan::default_antigravity_state_dir() {
                Some(state_dir) => (
                    managed_antigravity_scan::collect_observations_from(&state_dir),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let retained_antigravity = retain_existing_observations(
            &mut antigravity_observations,
            &previous.antigravity,
            |observation| &observation.state_file,
        );
        let antigravity_elapsed_ms = antigravity_started.elapsed().as_millis() as u64;

        let claude_started = Instant::now();
        let (mut claude_observations, unresolved) =
            match managed_claude_scan::default_claude_channel_state_dir() {
                Some(state_dir) => (
                    managed_claude_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    ),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let retained_codex =
            retain_existing_observations(&mut codex_observations, &previous.codex, |observation| {
                &observation.state_file
            });
        let retained_claude = retain_existing_observations(
            &mut claude_observations,
            &previous.claude,
            |observation| &observation.state_file,
        );
        let claude_elapsed_ms = claude_started.elapsed().as_millis() as u64;

        let opencode_started = Instant::now();
        let (mut opencode_observations, unresolved) =
            match managed_opencode_scan::default_opencode_server_state_dir() {
                Some(state_dir) => (
                    managed_opencode_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    ),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let opencode_elapsed_ms = opencode_started.elapsed().as_millis() as u64;

        let cursor_started = Instant::now();
        let (mut cursor_observations, unresolved) =
            match managed_cursor_helm_scan::default_cursor_helm_state_dir() {
                Some(state_dir) => (
                    managed_cursor_helm_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    ),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let retained_opencode = retain_existing_observations(
            &mut opencode_observations,
            &previous.opencode,
            |observation| &observation.state_file,
        );
        let retained_cursor = retain_existing_observations(
            &mut cursor_observations,
            &previous.cursor,
            |observation| &observation.state_file,
        );
        let cursor_elapsed_ms = cursor_started.elapsed().as_millis() as u64;
        let pi_started = Instant::now();
        let (mut pi_observations, unresolved) =
            match managed_pi_helm_scan::default_pi_helm_state_dir() {
                Some(state_dir) => (
                    managed_pi_helm_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    ),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let pi_elapsed_ms = pi_started.elapsed().as_millis() as u64;
        let retained_pi =
            retain_existing_observations(&mut pi_observations, &previous.pi, |observation| {
                &observation.state_file
            });
        let omp_started = Instant::now();
        let (mut omp_observations, unresolved) =
            match managed_omp_helm_scan::default_omp_helm_state_dir() {
                Some(state_dir) => (
                    managed_omp_helm_scan::collect_observations_from_processes(
                        &state_dir,
                        &process_facts,
                    ),
                    false,
                ),
                None => (Vec::new(), true),
            };
        unresolved_state_dirs += usize::from(unresolved);
        let retained_omp =
            retain_existing_observations(&mut omp_observations, &previous.omp, |observation| {
                &observation.state_file
            });
        let omp_elapsed_ms = omp_started.elapsed().as_millis() as u64;
        // Sweep contracts left behind by teardown paths that exited early or
        // by abrupt process death. Provider-neutral: Codex and Claude leak
        // these for different reasons.
        //
        // Gated on a valid process inventory. Without one every observation
        // reads as dead, and the sweep would delete the launch provenance of
        // every running session older than the grace period.
        let mut orphan_processes = Vec::new();
        // Retained contracts are discovered once per full pass, not by every
        // phase-ledger projection or once per janitor consumer.
        let continuation: Option<Arc<[managed_resume_scan::ResumeContractObservation]>> =
            if full_reconciliation && process_inventory_valid {
                crate::config::get_longhouse_home().ok().map(|home| {
                    managed_resume_scan::scan_resume_contracts_with_process_facts(
                        &home,
                        chrono::Utc::now(),
                        Some(&process_facts),
                    )
                    .into()
                })
            } else {
                None
            };
        if full_reconciliation && process_inventory_valid {
            if let Ok(home) = crate::config::get_longhouse_home() {
                // Codex launch provenance remains useful after a run ends: a
                // stopped session with a durable thread can be resumed later.
                // Live processes and resumable stopped state therefore both
                // protect the contract from the orphan sweep.
                let retained_codex = codex_observations
                    .iter()
                    .filter(|observation| codex_contract_must_be_retained(observation))
                    .map(|observation| observation.session_id.clone())
                    .collect::<std::collections::HashSet<_>>();
                let mut retained_claude = claude_observations
                    .iter()
                    .filter(|observation| observation.claude_alive || observation.bridge_alive)
                    .map(|observation| observation.session_id.clone())
                    .collect::<std::collections::HashSet<_>>();
                retained_claude.extend(
                    continuation
                        .as_deref()
                        .unwrap_or(&[])
                        .iter()
                        .filter(|observation| {
                            observation.provider == "claude"
                                && observation.contract_state == "valid"
                        })
                        .map(|observation| observation.session_id.clone()),
                );
                let now = std::time::SystemTime::now();
                let swept = crate::managed_contract_janitor::sweep_orphan_contracts(
                    &home.join("managed-local/contracts/codex"),
                    &retained_codex,
                    now,
                ) + crate::managed_contract_janitor::sweep_orphan_contracts(
                    &home.join("managed-local/contracts/claude"),
                    &retained_claude,
                    now,
                );
                if swept > 0 {
                    tracing::info!(swept, "removed orphaned managed-session contracts");
                }

                // Sweep the processes themselves. A managed provider whose
                // owner died keeps running forever otherwise: 126 Codex
                // app-servers and 81 OpenCode servers, up to three days old,
                // exhausted swap on the author's laptop before anything
                // noticed.
                //
                // Retained is every session any provider still observes at
                // all, not just the live ones. An orphan is a process whose
                // session Longhouse has lost track of completely — which is
                // what these were, since they pointed at temp and worktree
                // homes this daemon never reads.
                let mut observed_sessions = std::collections::HashSet::new();
                observed_sessions.extend(codex_observations.iter().map(|o| o.session_id.clone()));
                observed_sessions.extend(claude_observations.iter().map(|o| o.session_id.clone()));
                observed_sessions
                    .extend(opencode_observations.iter().map(|o| o.session_id.clone()));
                observed_sessions.extend(cursor_observations.iter().map(|o| o.session_id.clone()));
                observed_sessions.extend(pi_observations.iter().map(|o| o.session_id.clone()));
                observed_sessions.extend(omp_observations.iter().map(|o| o.session_id.clone()));
                observed_sessions.extend(
                    antigravity_observations
                        .iter()
                        .map(|o| o.session_id.clone()),
                );
                let mut retained_sessions = observed_sessions.clone();
                retained_sessions.extend(retained_codex.iter().cloned());
                retained_sessions.extend(retained_claude.iter().cloned());
                // Retained contracts protect ended, resumable sessions from
                // orphan classification even without an active observation.
                retained_sessions.extend(
                    continuation
                        .as_deref()
                        .unwrap_or(&[])
                        .iter()
                        .filter(|observation| observation.contract_state == "valid")
                        .map(|observation| observation.session_id.clone()),
                );

                // Only *identify* here. This closure runs under
                // `spawn_blocking`, and stopping a process group means waiting
                // on it; the async consumer of this result does the reaping so
                // the inventory scan stays quick.
                orphan_processes = crate::managed_process_janitor::find_orphan_processes(
                    &process_facts,
                    &retained_sessions,
                    chrono::Utc::now(),
                    crate::managed_process_janitor::ORPHAN_GRACE,
                );
            }
        }

        let mut result = ManagedObservationScanResult {
            reason,
            full_reconciliation,
            process_inventory_valid,
            process_inventory: unmanaged_process_inventory,
            codex_observations,
            antigravity_observations,
            claude_observations,
            orphan_processes,
            opencode_observations,
            cursor_observations,
            pi_observations,
            omp_observations,
            continuation,
            process_inventory_ms,
            codex_elapsed_ms,
            antigravity_elapsed_ms,
            claude_elapsed_ms,
            opencode_elapsed_ms,
            cursor_elapsed_ms,
            pi_elapsed_ms,
            omp_elapsed_ms,
            retained_stale_rows: retained_codex.len()
                + retained_antigravity.len()
                + retained_claude.len()
                + retained_opencode.len()
                + retained_cursor.len()
                + retained_pi.len()
                + retained_omp.len(),
            // Every pass enumerates the provider directories now, so a complete
            // certificate means: the process inventory was valid (identity
            // checks are meaningless without it), every provider directory
            // resolved, and no entry was carried forward unaccounted for -- a
            // state file that vanished mid-pass or failed to parse.
            enumeration_complete: process_inventory_valid
                && unresolved_state_dirs == 0
                && retained_codex.is_empty()
                && retained_antigravity.is_empty()
                && retained_claude.is_empty()
                && retained_opencode.is_empty()
                && retained_cursor.is_empty()
                && retained_pi.is_empty()
                && retained_omp.is_empty(),
            captured_at,
            elapsed_ms: started.elapsed().as_millis() as u64,
            status_owners: StatusOwnerEvidence::default(),
            status_owner_snapshot_at: process_inventory_valid
                .then_some(process_inventory_snapshot_at),
        };
        result.status_owners = status_owner_evidence_from_scan(&result, &process_facts);
        result
            .status_owners
            .merge(status_owner_evidence_from_claims(
                &status_claims,
                process_inventory_valid.then_some(&process_facts),
                crate::heartbeat::machine_boot_id().as_deref(),
            ));
        result
    });
    true
}

pub(super) fn retain_existing_observations<T: Clone>(
    current: &mut Vec<T>,
    previous: &[T],
    state_file: impl Fn(&T) -> &Path,
) -> HashSet<PathBuf> {
    let current_paths = current
        .iter()
        .map(|observation| state_file(observation).to_path_buf())
        .collect::<HashSet<_>>();
    let retained = previous
        .iter()
        .filter_map(|observation| {
            let path = state_file(observation);
            (path.exists() && !current_paths.contains(path)).then(|| observation.clone())
        })
        .collect::<Vec<_>>();
    let retained_paths = retained
        .iter()
        .map(|observation| state_file(observation).to_path_buf())
        .collect();
    current.extend(retained);
    retained_paths
}

pub(super) fn defer_managed_pair_retry(projection_generation: &mut u64, pending_full: &mut bool) {
    // The in-flight unmanaged refresh was paired with older managed truth.
    // Invalidate it now and retry both halves together once that lane is idle.
    *projection_generation = projection_generation.saturating_add(1);
    *pending_full = true;
}

impl DaemonState {
    pub(super) fn on_unmanaged_binding_refresh_done(
        &mut self,
        config: &ConnectConfig,
        unmanaged_binding_refresh_result: Option<
            Result<UnmanagedBindingRefreshResult, tokio::task::JoinError>,
        >,
    ) {
        let refresh_generation = self.unmanaged_binding_refresh_generation.take();
        match unmanaged_binding_refresh_result {
            Some(Ok(result)) => {
                let stale = result.generation != self.projection_generation;
                let managed_observation_current =
                    result.managed_observation_generation == self.managed_observation_generation;
                if stale {
                    tracing::debug!(
                        generation = result.generation,
                        latest_generation = self.projection_generation,
                        "Discarded stale unmanaged reconciliation result"
                    );
                } else if !managed_observation_current {
                    tracing::debug!(
                        result_managed_observation_generation =
                            result.managed_observation_generation,
                        latest_managed_observation_generation = self.managed_observation_generation,
                        "Applying unmanaged result without replacing newer managed observations"
                    );
                }
                if !stale && self.managed_observation_valid {
                    match result.result {
                        Ok(bindings) => {
                            if result.elapsed_ms > 1_000 {
                                tracing::warn!(
                                    reason = result.reason,
                                    binding_count = bindings.len(),
                                    elapsed_ms = result.elapsed_ms,
                                    "Unmanaged binding refresh was slow"
                                );
                            } else {
                                tracing::debug!(
                                    reason = result.reason,
                                    binding_count = bindings.len(),
                                    elapsed_ms = result.elapsed_ms,
                                    "Unmanaged binding refresh completed"
                                );
                            }
                            if managed_observation_current {
                                self.last_projected_managed_observations = result.managed;
                                self.last_projected_managed_scan_partial =
                                    result.managed_scan_partial;
                                self.last_projected_managed_snapshot_complete =
                                    result.full_reconciliation_candidate;
                                self.last_projected_unmanaged_snapshot_complete =
                                    result.full_reconciliation_candidate;
                                self.unmanaged_binding_refresh_failed = false;
                                if result.full_reconciliation_candidate {
                                    self.last_full_reconciled_at =
                                        Some(chrono::Utc::now().to_rfc3339());
                                }
                            } else {
                                self.last_projected_unmanaged_snapshot_complete = false;
                            }
                            self.last_unmanaged_session_bindings = Some(bindings);
                        }
                        Err(err) if !managed_observation_current => {
                            tracing::debug!(
                                reason = result.reason,
                                "Discarded stale unmanaged binding refresh failure: {}",
                                err
                            );
                        }
                        Err(err) => {
                            // Managed state files are authoritative for Helm ownership.
                            // Optional Shadow process discovery must not suppress a newly
                            // observed managed run. Publish that managed truth with the
                            // last-known unmanaged bindings, but mark only the unmanaged
                            // scope incomplete so the Runtime Host cannot close missing
                            // Shadow sessions from this partial observation.
                            if managed_observation_current {
                                self.last_projected_managed_observations = result.managed;
                                self.last_projected_managed_scan_partial =
                                    result.managed_scan_partial;
                                self.last_projected_managed_snapshot_complete =
                                    result.full_reconciliation_candidate;
                            }
                            self.last_projected_unmanaged_snapshot_complete = false;
                            // Shadow discovery is optional. Do not turn a
                            // per-pid lsof failure into an immediate full
                            // managed scan, which would repeatedly advance
                            // projection generations and starve managed
                            // truth. The scheduled full observation is the
                            // retry path; keep this projection incomplete
                            // so missing Shadow sessions remain unknown.
                            self.pending_full_reconciliation = false;
                            self.unmanaged_binding_refresh_failed = true;
                            // Shadow discovery is optional evidence. Keep the
                            // retained managed projection usable and mark the
                            // retry as in progress; a single lsof failure
                            // must not turn local health into a failed
                            // reconciliation or erase current sessions.
                            self.managed_reconciliation
                                .start("unmanaged_binding", chrono::Utc::now().to_rfc3339());
                            tracing::warn!(
                                reason = result.reason,
                                elapsed_ms = result.elapsed_ms,
                                "Unmanaged binding refresh failed: {}",
                                err
                            );
                        }
                    }
                    let input = ProjectionBuildInput {
                        generation: self.projection_generation,
                        managed_observation_generation: self.managed_observation_generation,
                        managed_scan_partial: self.last_projected_managed_scan_partial,
                        managed_snapshot_complete: self.last_projected_managed_snapshot_complete,
                        managed_captured_at: self.last_managed_captured_at.clone(),
                        unmanaged_snapshot_complete: self
                            .last_projected_unmanaged_snapshot_complete,
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
            Some(Err(err)) => {
                let refresh_is_current = refresh_generation
                    == Some((
                        self.projection_generation,
                        self.managed_observation_generation,
                    ));
                if refresh_is_current {
                    self.unmanaged_binding_refresh_failed = true;
                    // This task only refreshes optional Shadow bindings.
                    // Preserve the last coherent managed projection and
                    // expose the retry as reconciling rather than making
                    // the whole local session inventory failed.
                    self.managed_reconciliation
                        .start("unmanaged_binding", chrono::Utc::now().to_rfc3339());
                    heartbeat::refresh_existing_status_pulse(
                        &self.managed_reconciliation,
                        &mut self.shipping_progress,
                        self.offline.is_offline,
                        &self.status_path,
                        &self.heartbeat_transport,
                        Some(&self.host_link.snapshot()),
                    );
                    tracing::warn!("Unmanaged binding refresh task failed: {}", err);
                } else {
                    tracing::debug!(
                        refresh_generation = ?refresh_generation,
                        latest_generation = self.projection_generation,
                        latest_managed_observation_generation =
                            self.managed_observation_generation,
                        "Discarded stale unmanaged binding task failure: {}",
                        err
                    );
                }
            }
            None => {}
        }
        if self.unmanaged_binding_refresh_tasks.is_empty()
            && self.managed_observation_scan_tasks.is_empty()
        {
            if self.pending_wake_reconciliation
                && maybe_start_managed_observation_scan(
                    self.projection_db_path.clone(),
                    &mut self.managed_observation_scan_tasks,
                    "wake",
                    true,
                    &self.last_managed_observations,
                )
            {
                self.pending_wake_reconciliation = false;
                self.managed_full_reconciliation_not_before =
                    Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                self.managed_reconciliation
                    .start("wake", chrono::Utc::now().to_rfc3339());
            } else if managed_full_reconciliation_ready(
                self.pending_full_reconciliation
                    || certificate_needs_refresh(self.last_certified_at, Instant::now()),
                self.managed_observation_scan_tasks.is_empty(),
                Instant::now(),
                self.managed_full_reconciliation_not_before,
            ) && maybe_start_managed_observation_scan(
                self.projection_db_path.clone(),
                &mut self.managed_observation_scan_tasks,
                "full_reconciliation",
                true,
                &self.last_managed_observations,
            ) {
                self.pending_full_reconciliation = false;
                self.managed_full_reconciliation_not_before =
                    Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                self.managed_reconciliation
                    .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
            }
        }
    }

    pub(super) fn on_managed_observation_scan_done(
        &mut self,
        config: &ConnectConfig,
        managed_observation_scan_result: Option<
            Result<ManagedObservationScanResult, tokio::task::JoinError>,
        >,
    ) {
        match managed_observation_scan_result {
            Some(Ok(mut result)) => {
                self.last_status_owners_at = result.status_owner_snapshot_at;
                self.last_status_owners = Arc::new(std::mem::take(&mut result.status_owners));
                if result.elapsed_ms > 250 {
                    tracing::warn!(
                        reason = result.reason,
                        full_reconciliation = result.full_reconciliation,
                        process_inventory_valid = result.process_inventory_valid,
                        codex_count = result.codex_observations.len(),
                        antigravity_count = result.antigravity_observations.len(),
                        claude_count = result.claude_observations.len(),
                        opencode_count = result.opencode_observations.len(),
                        cursor_count = result.cursor_observations.len(),
                        pi_count = result.pi_observations.len(),
                        omp_count = result.omp_observations.len(),
                        omp_live_count = result
                            .omp_observations
                            .iter()
                            .filter(|observation| observation.live)
                            .count(),
                        process_inventory_ms = result.process_inventory_ms,
                        codex_elapsed_ms = result.codex_elapsed_ms,
                        antigravity_elapsed_ms = result.antigravity_elapsed_ms,
                        claude_elapsed_ms = result.claude_elapsed_ms,
                        opencode_elapsed_ms = result.opencode_elapsed_ms,
                        cursor_elapsed_ms = result.cursor_elapsed_ms,
                        pi_elapsed_ms = result.pi_elapsed_ms,
                        omp_elapsed_ms = result.omp_elapsed_ms,
                        retained_stale_rows = result.retained_stale_rows,
                        elapsed_ms = result.elapsed_ms,
                        "Managed observation scan was slow"
                    );
                } else {
                    tracing::debug!(
                        reason = result.reason,
                        full_reconciliation = result.full_reconciliation,
                        process_inventory_valid = result.process_inventory_valid,
                        codex_count = result.codex_observations.len(),
                        antigravity_count = result.antigravity_observations.len(),
                        claude_count = result.claude_observations.len(),
                        opencode_count = result.opencode_observations.len(),
                        cursor_count = result.cursor_observations.len(),
                        pi_count = result.pi_observations.len(),
                        omp_count = result.omp_observations.len(),
                        omp_live_count = result
                            .omp_observations
                            .iter()
                            .filter(|observation| observation.live)
                            .count(),
                        process_inventory_ms = result.process_inventory_ms,
                        codex_elapsed_ms = result.codex_elapsed_ms,
                        antigravity_elapsed_ms = result.antigravity_elapsed_ms,
                        claude_elapsed_ms = result.claude_elapsed_ms,
                        opencode_elapsed_ms = result.opencode_elapsed_ms,
                        cursor_elapsed_ms = result.cursor_elapsed_ms,
                        pi_elapsed_ms = result.pi_elapsed_ms,
                        omp_elapsed_ms = result.omp_elapsed_ms,
                        retained_stale_rows = result.retained_stale_rows,
                        elapsed_ms = result.elapsed_ms,
                        "Managed observation scan completed"
                    );
                }
                // Report only. `process_inventory_valid` proves `ps`
                // ran; it does not prove the five provider scanners
                // did, and a scanner that fails is indistinguishable
                // from a provider with no sessions. Killing on that
                // basis would destroy live work.
                if !result.orphan_processes.is_empty() {
                    crate::managed_process_janitor::report_orphan_processes(
                        &result.orphan_processes,
                    );
                }
                if !result.process_inventory_valid {
                    self.managed_observation_valid = false;
                    self.projection_generation = self.projection_generation.saturating_add(1);
                    tracing::warn!(
                                reason = result.reason,
                                "Managed observation scan retained prior truth because process inventory failed"
                            );
                    self.managed_reconciliation =
                        heartbeat::ProjectionReconciliation::failed("process_inventory");
                    self.managed_reconciliation
                        .start("process_inventory", chrono::Utc::now().to_rfc3339());
                    heartbeat::refresh_existing_status_pulse(
                        &self.managed_reconciliation,
                        &mut self.shipping_progress,
                        self.offline.is_offline,
                        &self.status_path,
                        &self.heartbeat_transport,
                        Some(&self.host_link.snapshot()),
                    );
                    if self.pending_wake_reconciliation {
                        if maybe_start_managed_observation_scan(
                            self.projection_db_path.clone(),
                            &mut self.managed_observation_scan_tasks,
                            "wake",
                            true,
                            &self.last_managed_observations,
                        ) {
                            self.pending_wake_reconciliation = false;
                        }
                    } else if managed_full_reconciliation_ready(
                        self.pending_full_reconciliation
                            || certificate_needs_refresh(self.last_certified_at, Instant::now()),
                        self.managed_observation_scan_tasks.is_empty(),
                        Instant::now(),
                        self.managed_full_reconciliation_not_before,
                    ) && maybe_start_managed_observation_scan(
                        self.projection_db_path.clone(),
                        &mut self.managed_observation_scan_tasks,
                        "full_reconciliation",
                        true,
                        &self.last_managed_observations,
                    ) {
                        self.pending_full_reconciliation = false;
                        self.managed_full_reconciliation_not_before =
                            Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                    }
                    return;
                }
                self.managed_observation_valid = true;
                if result.full_reconciliation {
                    // Start-time gating does not prevent a long scan
                    // from immediately retriggering on events observed
                    // during its own walk. Hold the next full pass for
                    // one observation interval after completion.
                    self.managed_full_reconciliation_not_before =
                        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                }
                if let Some(continuation) = &result.continuation {
                    self.last_resume_contracts = Some(continuation.clone());
                }
                let next_managed_observations =
                    ManagedObservationSnapshot::from_result(&result).current_only();
                let managed_observations_changed = !next_managed_observations
                    .projection_equivalent(&self.last_managed_observations);
                let (managed_scan_partial, managed_snapshot_complete) =
                    managed_scan_certificate(&result);
                if managed_snapshot_complete {
                    self.last_certified_at = Some(Instant::now());
                    self.certificate_keepalive_warned = false;
                } else if let Some(refreshed_at) = self.last_certified_at {
                    // Keep the last claim only while it is still inside
                    // the keepalive; past that the beat ships without
                    // absence authority, which fails closed.
                    if refreshed_at.elapsed()
                        > Duration::from_secs(MANAGED_CERTIFICATE_KEEPALIVE_SECS)
                        && !self.certificate_keepalive_warned
                    {
                        self.certificate_keepalive_warned = true;
                        tracing::warn!(
                                    reason = result.reason,
                                    retained_stale_rows = result.retained_stale_rows,
                                    "Managed enumeration has not certified within the keepalive window; beats ship without absence authority"
                                );
                    }
                }
                let managed_evidence_changed = managed_observations_changed
                    || result.full_reconciliation
                    || managed_scan_partial != self.last_projected_managed_scan_partial;
                if managed_evidence_changed {
                    self.managed_observation_generation =
                        self.managed_observation_generation.saturating_add(1);
                }
                self.last_managed_observations = next_managed_observations;
                project_binding_liveness(&self.conn, &self.last_managed_observations);
                pump_ready_local_work(
                    &mut self.scheduler,
                    &mut self.in_flight,
                    &self.task_context,
                    &mut self.deferred_retries,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    archive_repair_is_paused(config.archive_repair_mode),
                );
                let managed_process_pids = managed_process_pids_from_observations(
                    &result.codex_observations,
                    &result.claude_observations,
                    &result.opencode_observations,
                    &result.cursor_observations,
                    &result.pi_observations,
                    &result.omp_observations,
                );
                let should_refresh_unmanaged =
                    result.full_reconciliation || managed_observations_changed;
                let paired_generation = self.projection_generation.saturating_add(1);
                let paired_refresh_started = should_refresh_unmanaged
                    && maybe_start_unmanaged_binding_refresh(
                        &mut self.unmanaged_binding_refresh_tasks,
                        config.shipper_config.db_path.clone(),
                        config.shipper_config.machine_name.clone(),
                        managed_process_pids.clone(),
                        result.process_inventory.clone(),
                        result.reason,
                        paired_generation,
                        self.managed_observation_generation,
                        self.last_managed_observations.clone(),
                        managed_scan_partial,
                        managed_scan_certificate(&result).1,
                    );
                if paired_refresh_started {
                    self.projection_generation = paired_generation;
                    self.unmanaged_binding_refresh_generation = Some((
                        self.projection_generation,
                        self.managed_observation_generation,
                    ));
                } else if result.full_reconciliation {
                    if result.reason == "wake" {
                        self.pending_wake_reconciliation = true;
                    } else {
                        self.pending_full_reconciliation = true;
                    }
                } else if managed_observations_changed {
                    defer_managed_pair_retry(
                        &mut self.projection_generation,
                        &mut self.pending_full_reconciliation,
                    );
                }

                // A valid managed scan is fresh evidence even when the
                // optional unmanaged/Shadow refresh is still running.
                // Publish it with the cached Shadow rows only as
                // incomplete evidence; the refresh result can replace
                // this projection later without blocking managed truth.
                self.last_projected_managed_observations = self.last_managed_observations.clone();
                self.last_managed_captured_at = result.captured_at.clone();
                self.last_projected_managed_scan_partial = managed_scan_partial;
                self.last_projected_managed_snapshot_complete = managed_snapshot_complete;
                self.last_projected_unmanaged_snapshot_complete = false;
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
                maybe_start_opencode_title_refresh(
                    &mut self.opencode_title_refresh_tasks,
                    config.shipper_config.db_path.clone(),
                    result.opencode_observations.clone(),
                );
                if self.pending_wake_reconciliation
                    && self.unmanaged_binding_refresh_tasks.is_empty()
                    && maybe_start_managed_observation_scan(
                        self.projection_db_path.clone(),
                        &mut self.managed_observation_scan_tasks,
                        "wake",
                        true,
                        &self.last_managed_observations,
                    )
                {
                    self.pending_wake_reconciliation = false;
                    self.managed_full_reconciliation_not_before =
                        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                    self.managed_reconciliation
                        .start("wake", chrono::Utc::now().to_rfc3339());
                } else if managed_full_reconciliation_ready(
                    self.pending_full_reconciliation,
                    self.unmanaged_binding_refresh_tasks.is_empty()
                        && self.managed_observation_scan_tasks.is_empty(),
                    Instant::now(),
                    self.managed_full_reconciliation_not_before,
                ) && maybe_start_managed_observation_scan(
                    self.projection_db_path.clone(),
                    &mut self.managed_observation_scan_tasks,
                    "full_reconciliation",
                    true,
                    &self.last_managed_observations,
                ) {
                    self.pending_full_reconciliation = false;
                    self.managed_full_reconciliation_not_before =
                        Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
                    self.managed_reconciliation
                        .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
                }
            }
            Some(Err(err)) => {
                self.managed_observation_valid = false;
                self.projection_generation = self.projection_generation.saturating_add(1);
                tracing::warn!("Managed observation scan task failed: {}", err);
                self.managed_reconciliation = heartbeat::ProjectionReconciliation::failed(
                    self.managed_reconciliation
                        .reason
                        .clone()
                        .unwrap_or_else(|| "managed_observation".to_string()),
                );
                heartbeat::refresh_existing_status_pulse(
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.status_path,
                    &self.heartbeat_transport,
                    Some(&self.host_link.snapshot()),
                );
            }
            None => {}
        }
    }

    pub(super) fn on_managed_full_reconciliation_tick(&mut self) {
        if managed_full_reconciliation_ready(
            !self.pending_wake_reconciliation,
            self.managed_observation_scan_tasks.is_empty(),
            Instant::now(),
            self.managed_full_reconciliation_not_before,
        ) && maybe_start_managed_observation_scan(
            self.projection_db_path.clone(),
            &mut self.managed_observation_scan_tasks,
            "full_reconciliation",
            true,
            &self.last_managed_observations,
        ) {
            self.managed_full_reconciliation_not_before =
                Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
            self.managed_reconciliation
                .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
        } else {
            self.pending_full_reconciliation = true;
            self.managed_reconciliation
                .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
        }
    }

    pub(super) fn on_managed_observation_tick(&mut self) {
        // Coalesce a periodic tick and watcher burst while a full scan
        // is running. A queued full request wins on the next tick, but
        // the cooldown prevents a completion/event feedback loop.
        if managed_full_reconciliation_ready(
            self.pending_full_reconciliation,
            self.managed_observation_scan_tasks.is_empty(),
            Instant::now(),
            self.managed_full_reconciliation_not_before,
        ) && maybe_start_managed_observation_scan(
            self.projection_db_path.clone(),
            &mut self.managed_observation_scan_tasks,
            "full_reconciliation",
            true,
            &self.last_managed_observations,
        ) {
            self.pending_full_reconciliation = false;
            self.managed_full_reconciliation_not_before =
                Instant::now() + Duration::from_secs(MANAGED_OBSERVATION_INTERVAL_SECS);
            self.managed_reconciliation
                .start("full_reconciliation", chrono::Utc::now().to_rfc3339());
        } else if !self.pending_full_reconciliation && !self.pending_wake_reconciliation {
            maybe_start_managed_observation_scan(
                self.projection_db_path.clone(),
                &mut self.managed_observation_scan_tasks,
                "periodic",
                self.last_resume_contracts.is_none(),
                &self.last_managed_observations,
            );
        }
    }
}
