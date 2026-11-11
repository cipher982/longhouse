//! Discovery, inventory and reconciliation scans that find sources to ship.

use super::*;

pub(super) fn enqueue_discovered_files(
    scheduler: &mut PathScheduler,
    all_files: Vec<discovery::DiscoveredFile>,
    priority: WorkPriority,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
) -> usize {
    let source = discovery_observation_source(priority);
    let mut count = 0;
    for file in all_files {
        if retry_admission_open(&file.path, deferred_retries) {
            let observed_at_ms = if priority == WorkPriority::Scan {
                file.modified_at_ms
            } else {
                now_ms()
            };
            scheduler.enqueue_observed(file.path, file.provider, priority, source, observed_at_ms);
            count += 1;
        }
    }
    count
}

pub(super) fn retry_admission_open(
    path: &Path,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
) -> bool {
    let Some(retry) = deferred_retries.get(path) else {
        return true;
    };
    if retry.due_at > Instant::now() {
        return false;
    }
    deferred_retries.remove(path);
    true
}

pub(super) fn maybe_seal_history_reconciliation(
    conn: &mut rusqlite::Connection,
    open: &mut Option<OpenHistoryReconciliation>,
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    discovery_idle: bool,
) {
    let Some(attempt) = open.as_mut() else {
        return;
    };
    let history_retry_pending = deferred_retries
        .values()
        .any(|retry| retry.priority != WorkPriority::Live);
    if !attempt.remaining_paths.is_empty()
        || !discovery_idle
        || scheduler.has_pending_priority(WorkPriority::Scan)
        || scheduler.has_pending_priority(WorkPriority::Retry)
        || history_retry_pending
    {
        return;
    }
    if attempt
        .last_unsealed_check
        .is_some_and(|at| at.elapsed() < super::HISTORY_SEAL_RECHECK_INTERVAL)
    {
        return;
    }
    let attempt_id = attempt.attempt_id;
    match crate::state::source_inventory::try_seal_reconciliation(conn, attempt_id) {
        Ok(true) => {
            tracing::info!(
                attempt_id,
                "Sealed durable transcript reconciliation coverage"
            );
            *open = None;
        }
        Ok(false) => attempt.last_unsealed_check = Some(Instant::now()),
        Err(error) => tracing::warn!(
            attempt_id,
            error = %error,
            "Failed to seal durable transcript reconciliation coverage"
        ),
    }
}

/// Say how much local history the import scope is keeping out of a scan, so a
/// machine that ships less than its disk holds explains itself in the log. The
/// periodic scan repeats the same fact every few minutes, so only the scans
/// that mean something (startup, a scope change) say it at info.
pub(super) fn log_scope_exclusions(
    reason: &str,
    excluded: &std::collections::BTreeMap<&'static str, u64>,
) {
    let total: u64 = excluded.values().sum();
    if total == 0 {
        return;
    }
    if reason == "reconciliation scan" {
        tracing::debug!(
            excluded_sources = total,
            ?excluded,
            "Import scope excluded local sources"
        );
    } else {
        tracing::info!(
            reason,
            excluded_sources = total,
            ?excluded,
            "Import scope kept local sources out of this scan; widen it with `longhouse machine scope`"
        );
    }
}

pub(super) fn discovery_observation_source(priority: WorkPriority) -> &'static str {
    match priority {
        WorkPriority::Scan => "reconciliation_scan",
        _ => "discovery_scan",
    }
}

pub(super) fn start_discovery_task(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
    priority: WorkPriority,
    reason: &'static str,
) {
    let providers = providers.to_vec();
    discovery_tasks.spawn_blocking(move || {
        let scan = discovery::discover_all_files_with_inventory(&providers);
        log_scope_exclusions(reason, &scan.excluded_by_scope);
        DiscoveryTaskResult {
            files: scan.files,
            inventory: scan.inventory,
            enqueue_files: true,
            priority,
            reason,
        }
    });
}

pub(super) fn start_inventory_task(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
) {
    let providers = providers.to_vec();
    discovery_tasks.spawn_blocking(move || {
        let scan = discovery::discover_all_files_with_inventory(&providers);
        log_scope_exclusions("startup inventory", &scan.excluded_by_scope);
        DiscoveryTaskResult {
            files: Vec::new(),
            inventory: scan.inventory,
            enqueue_files: false,
            priority: WorkPriority::Scan,
            reason: "startup inventory",
        }
    });
}

/// A reconciliation scan deferred by Live work runs anyway once this many
/// fallback intervals have passed since the last one started.
pub(super) const SCAN_OVERDUE_INTERVALS: u32 = 3;

pub(super) fn maybe_start_reconciliation_scan(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    archive_repair_mode: ArchiveRepairMode,
    reason: &'static str,
) {
    maybe_start_reconciliation_scan_unless_live(
        discovery_tasks,
        providers,
        scheduler,
        deferred_retries,
        archive_repair_mode,
        reason,
        false,
    );
}

/// Start a reconciliation scan unless one is running or pending, archive
/// repair is paused, or Live work is pending and the scan is not `overdue`.
/// Returns whether one started.
pub(super) fn maybe_start_reconciliation_scan_unless_live(
    discovery_tasks: &mut JoinSet<DiscoveryTaskResult>,
    providers: &[ProviderConfig],
    scheduler: &PathScheduler,
    deferred_retries: &HashMap<PathBuf, DeferredRetry>,
    archive_repair_mode: ArchiveRepairMode,
    reason: &'static str,
    overdue: bool,
) -> bool {
    if archive_repair_is_paused(archive_repair_mode) {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because archive repair is paused"
        );
        return false;
    }

    if !discovery_tasks.is_empty() {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because discovery is still running"
        );
        return false;
    }

    if scheduler.has_pending_priority(WorkPriority::Scan) {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan because one is already pending"
        );
        return false;
    }

    let live_retry_pending = deferred_retries
        .values()
        .any(|retry| retry.priority == WorkPriority::Live);
    if (scheduler.has_pending_priority(WorkPriority::Live) || live_retry_pending) && !overdue {
        tracing::debug!(
            reason,
            "Skipping reconciliation scan while live local work is pending"
        );
        return false;
    }
    if overdue && scheduler.has_pending_priority(WorkPriority::Live) {
        tracing::info!(
            reason,
            "Starting an overdue reconciliation scan alongside pending live work"
        );
    }

    tracing::debug!(reason, "Starting reconciliation scan in background");
    start_discovery_task(discovery_tasks, providers, WorkPriority::Scan, reason);
    true
}

/// How often the bound working set is reconciled.
pub(super) const RECONCILE_INTERVAL: Duration = Duration::from_secs(1);

/// One source the reconciler wants scheduled.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct ReconcileTarget {
    pub(super) path: PathBuf,
    pub(super) provider: String,
    pub(super) lag_bytes: u64,
    pub(super) never_shipped: bool,
}

pub(super) struct ReconcileScanResult {
    pub(super) targets: Vec<ReconcileTarget>,
    pub(super) scanned: usize,
    pub(super) elapsed_ms: u64,
    pub(super) error: Option<String>,
}

/// Decide whether a bound source needs shipping, from durable state alone.
///
/// The working set is the binding table, not the live observations: an owner
/// that exited still owes its tail, and a source bound before its first
/// shipment has no lane to compare against, so both would be invisible to a
/// live-observation scan.
pub(super) fn reconcile_target_for(
    conn: &rusqlite::Connection,
    binding: &crate::state::session_binding::SourceBinding,
) -> Option<ReconcileTarget> {
    let path = PathBuf::from(&binding.path);
    // A source we cannot stat is unknown, not current: say nothing rather than
    // claim it is up to date.
    let metadata = std::fs::metadata(&path).ok()?;
    if metadata.len() == 0 {
        return None;
    }
    let provider = binding.provider.as_str();
    // A source the host refused stays behind its file by construction: the
    // frozen envelope cannot ship, so the lane never advances. Scheduling it
    // every tick re-loads its whole request body to be told "blocked" (and,
    // before the shipper honoured the backoff, re-asked the host). It is due
    // again when its backoff elapses or a restart wakes it.
    if crate::state::pending_source_envelope::source_reexamination_is_deferred(
        conn,
        provider,
        &crate::storage_v2_shipper::opaque_source_id(&binding.path),
    )
    .unwrap_or(false)
    {
        return None;
    }
    let (position, never_shipped) =
        match crate::storage_v2_shipper::durable_lane_position(conn, provider, &binding.path) {
            Ok(Some(position)) => (position, false),
            Ok(None) => (0, true),
            Err(_) => return None,
        };
    let lag_bytes = metadata.len().saturating_sub(position);
    let lane_is_stale = lag_bytes > 0
        && crate::storage_v2_shipper::durable_lane_age_seconds(conn, provider, &binding.path)
            .is_some_and(|age| age >= STARVED_LIVE_TRANSCRIPT_STALE_SECONDS);
    if !never_shipped && lag_bytes < STARVED_LIVE_TRANSCRIPT_BYTES && !lane_is_stale {
        return None;
    }
    Some(ReconcileTarget {
        path,
        provider: binding.provider.clone(),
        lag_bytes,
        never_shipped,
    })
}

/// Bound sources found fully shipped (lane at the file's length), by
/// database and path, with the stat they were judged on.
///
/// The binding table keeps every session a launcher ever bound (1,472 rows on
/// cinder, 2026-10-11), and each pass did a stat plus three SQLite lookups per
/// row every second. A source whose lane had reached its length and whose
/// file has not changed cannot have become a target: with no lag it is
/// neither starved nor stale, whatever the clock says. So only the stat is
/// repeated for it, and a full pass every [`RECONCILE_FULL_PASS_INTERVAL`]
/// re-reads everything in case durable state moved without the file moving.
static RECONCILED_CURRENT: std::sync::LazyLock<std::sync::Mutex<ReconciledCurrent>> =
    std::sync::LazyLock::new(Default::default);

#[derive(Default)]
struct ReconciledCurrent {
    full_pass_at: HashMap<PathBuf, Instant>,
    sources: HashMap<(PathBuf, String), SourceStat>,
}

/// How long a "fully shipped and unchanged" verdict is trusted.
const RECONCILE_FULL_PASS_INTERVAL: Duration = Duration::from_secs(60);

#[derive(Clone, Copy, PartialEq, Eq)]
struct SourceStat {
    len: u64,
    modified_nanos: i128,
    inode: u64,
}

fn source_stat(metadata: &std::fs::Metadata) -> SourceStat {
    #[cfg(unix)]
    let inode = std::os::unix::fs::MetadataExt::ino(metadata);
    #[cfg(not(unix))]
    let inode = 0;
    SourceStat {
        len: metadata.len(),
        modified_nanos: metadata
            .modified()
            .ok()
            .and_then(|modified| modified.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|elapsed| elapsed.as_nanos() as i128)
            .unwrap_or(-1),
        inode,
    }
}

/// Walk the bound working set off the event loop.
///
/// What it must not do is run *on* the loop: the reviewers' objection to a
/// timer inside a biased `select!` is that sustained events still starve it.
pub(super) fn maybe_start_reconcile_scan(
    tasks: &mut JoinSet<ReconcileScanResult>,
    db_path: Option<PathBuf>,
) -> bool {
    if !tasks.is_empty() {
        return false;
    }
    tasks.spawn_blocking(move || {
        let started = Instant::now();
        let mut result = ReconcileScanResult {
            targets: Vec::new(),
            scanned: 0,
            elapsed_ms: 0,
            error: None,
        };
        match crate::state::db::resolve_db_path(db_path.as_deref())
            .and_then(|path| crate::state::db::open_connection(&path).map(|conn| (path, conn)))
        {
            Ok((path, conn)) => {
                match crate::state::session_binding::SessionBinding::new(&conn).list_bindings() {
                    Ok(bindings) => {
                        let mut current = RECONCILED_CURRENT
                            .lock()
                            .unwrap_or_else(|poisoned| poisoned.into_inner());
                        let full_pass = current
                            .full_pass_at
                            .get(&path)
                            .is_none_or(|at| at.elapsed() >= RECONCILE_FULL_PASS_INTERVAL);
                        if full_pass {
                            current.sources.retain(|(db, _), _| db != &path);
                            current.full_pass_at.insert(path.clone(), Instant::now());
                        }
                        for binding in bindings {
                            result.scanned += 1;
                            let key = (path.clone(), binding.path.clone());
                            let stat = std::fs::metadata(&binding.path)
                                .ok()
                                .map(|metadata| source_stat(&metadata));
                            if stat.is_some() && current.sources.get(&key) == stat.as_ref() {
                                continue;
                            }
                            current.sources.remove(&key);
                            match reconcile_target_for(&conn, &binding) {
                                Some(target) => result.targets.push(target),
                                None => {
                                    if let Some(stat) = stat.filter(|stat| {
                                        stat.len > 0
                                            && crate::storage_v2_shipper::durable_lane_position(
                                                &conn,
                                                &binding.provider,
                                                &binding.path,
                                            )
                                            .ok()
                                            .flatten()
                                                == Some(stat.len)
                                    }) {
                                        current.sources.insert(key, stat);
                                    }
                                }
                            }
                        }
                    }
                    Err(error) => result.error = Some(error.to_string()),
                }
            }
            Err(error) => result.error = Some(error.to_string()),
        }
        result.elapsed_ms = started.elapsed().as_millis() as u64;
        result
    });
    true
}

/// A live transcript this far behind its own file is treated as starved.
pub(super) const STARVED_LIVE_TRANSCRIPT_BYTES: u64 = 256 * 1024;
/// A live transcript whose lane has not moved for this long is starved too.
///
/// The byte threshold alone cannot serve a slow producer: a session writing a
/// few bytes per second crosses any byte bound only after hours. Staleness
/// catches that case at the timescale a user would notice.
pub(super) const STARVED_LIVE_TRANSCRIPT_STALE_SECONDS: i64 = 120;

impl DaemonState {
    pub(super) fn on_discovery_done(
        &mut self,
        config: &ConnectConfig,
        discovery_result: Option<Result<DiscoveryTaskResult, tokio::task::JoinError>>,
    ) {
        match discovery_result {
            Some(Ok(result)) => {
                let reconciliation_paths = result
                    .enqueue_files
                    .then(|| result.files.iter().map(|file| file.path.clone()).collect());
                let previous_inventory_generation =
                    crate::state::source_inventory::load_inventory(&self.conn)
                        .ok()
                        .flatten()
                        .map(|inventory| inventory.generation);
                let inventory =
                    crate::state::source_inventory::persist_inventory(&self.conn, result.inventory);
                if result.enqueue_files {
                    // Seed the re-check with what this scan found recently
                    // written; the scan already knows each file's mtime, so
                    // older sources cost nothing here.
                    let now = std::time::SystemTime::now();
                    let hot_after_ms =
                        now_ms().saturating_sub(super::hot_sources::HOT_WINDOW.as_millis() as i64);
                    for file in result
                        .files
                        .iter()
                        .filter(|file| file.modified_at_ms >= hot_after_ms)
                    {
                        self.hot_sources.note(file.path.clone(), file.provider, now);
                    }
                    let queued = enqueue_discovered_files(
                        &mut self.scheduler,
                        result.files,
                        result.priority,
                        &mut self.deferred_retries,
                    );
                    tracing::debug!("Queued {} paths for {}", queued, result.reason);
                }
                match inventory {
                    Ok(snapshot) => {
                        tracing::info!(
                            generation = snapshot.generation,
                            source_count = snapshot.source_count,
                            footprint_bytes = snapshot.footprint_bytes,
                            scan_error_count = snapshot.scan_error_count,
                            "Updated durable transcript source inventory"
                        );
                        if let Some(remaining_paths) = reconciliation_paths {
                            match crate::state::source_inventory::begin_reconciliation(
                                &mut self.conn,
                                &snapshot,
                            ) {
                                Ok(Some(attempt)) => {
                                    self.open_history_reconciliation =
                                        Some(OpenHistoryReconciliation {
                                            attempt_id: attempt.attempt_id,
                                            remaining_paths,
                                            last_unsealed_check: None,
                                        });
                                }
                                Ok(None) => self.open_history_reconciliation = None,
                                Err(error) => {
                                    self.open_history_reconciliation = None;
                                    tracing::warn!(
                                        error = %error,
                                        "Failed to begin transcript reconciliation seal"
                                    );
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
                        if inventory_change_requires_projection(
                            previous_inventory_generation,
                            snapshot.generation,
                            self.last_unmanaged_session_bindings.is_some(),
                        ) && self.managed_observation_valid
                        {
                            self.projection_generation =
                                self.projection_generation.saturating_add(1);
                            let input = ProjectionBuildInput {
                                generation: self.projection_generation,
                                managed_observation_generation: self.managed_observation_generation,
                                managed_scan_partial: self.last_projected_managed_scan_partial,
                                managed_snapshot_complete: self
                                    .last_projected_managed_snapshot_complete,
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
                            if !maybe_start_projection_build(
                                &mut self.projection_build_tasks,
                                input,
                            ) {
                                self.projection_build_pending = true;
                            }
                        }
                    }
                    Err(error) => tracing::warn!(
                        error = %error,
                        "Failed to persist transcript source inventory"
                    ),
                }
            }
            Some(Err(e)) => {
                tracing::warn!("Background discovery task failed: {}", e);
            }
            None => {}
        }
    }

    pub(super) fn on_startup_reconciliation_due(&mut self, config: &ConnectConfig) {
        self.startup_reconciliation_pending = false;
        maybe_start_reconciliation_scan(
            &mut self.discovery_tasks,
            &self.providers,
            &self.scheduler,
            &self.deferred_retries,
            config.archive_repair_mode,
            "startup reconciliation",
        );
    }

    pub(super) fn on_scope_tick(&mut self) {
        let current = crate::import_scope::fingerprint(&self.scope_dir);
        if current != self.last_scope_fingerprint {
            self.last_scope_fingerprint = current;
            if current.is_none() {
                // The file was deleted under a running daemon. What it
                // enforces did not change (it keeps the scope it last
                // knew), so put the file back rather than rescan.
                if crate::config::restore_lost_scope_file(&self.scope_dir) {
                    tracing::warn!(
                        "The import scope file was deleted; restored it from the running scope"
                    );
                }
            } else {
                let scope = crate::config::import_scope();
                crate::config::record_import_scope(&self.conn, &scope);
                tracing::info!("Import scope: {}", scope.describe());
                start_discovery_task(
                    &mut self.discovery_tasks,
                    &self.providers,
                    WorkPriority::Scan,
                    "import scope changed",
                );
            }
        }
    }

    pub(super) fn on_provider_roots_tick(&mut self) {
        if self
            .watcher
            .refresh_provider_roots(&mut self.providers, &mut self.pending_provider_roots)
        {
            start_discovery_task(
                &mut self.discovery_tasks,
                &self.providers,
                WorkPriority::Scan,
                "new provider transcript root",
            );
        }
    }

    pub(super) fn on_fallback_tick(&mut self, config: &ConnectConfig) {
        // Live work defers a scan, but never indefinitely: a source whose
        // event was lost and which is no longer hot is found only by a scan.
        let fallback_interval = Duration::from_secs(config.fallback_scan_secs.max(10));
        let overdue = self.last_reconciliation_scan_at.elapsed()
            >= fallback_interval * SCAN_OVERDUE_INTERVALS;
        if maybe_start_reconciliation_scan_unless_live(
            &mut self.discovery_tasks,
            &self.providers,
            &self.scheduler,
            &self.deferred_retries,
            config.archive_repair_mode,
            "reconciliation scan",
            overdue,
        ) {
            self.last_reconciliation_scan_at = Instant::now();
        }
    }

    /// Enqueue the hot sources whose stat moved since they were last seen.
    pub(super) fn on_hot_recheck_tick(&mut self) {
        if crate::qa_fault::hot_recheck_off() {
            return;
        }
        let moved = self.hot_sources.recheck(std::time::SystemTime::now());
        if moved.is_empty() {
            return;
        }
        let import_scope = crate::config::import_scope();
        let now = now_ms();
        for (path, provider) in moved {
            if !discovery::source_in_import_scope(&import_scope, provider, &path)
                || !retry_admission_open(&path, &mut self.deferred_retries)
            {
                continue;
            }
            tracing::debug!(
                provider,
                path = %path.display(),
                "Hot source moved without a file event reaching it first; scheduling a shipment"
            );
            self.scheduler.enqueue_observed_window(
                path,
                provider,
                WorkPriority::Live,
                "hot_recheck",
                now,
                now,
            );
        }
    }
}
