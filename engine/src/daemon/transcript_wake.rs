//! Transcript wake signals: file events, wake hints and binding liveness.

use super::*;

#[allow(clippy::too_many_arguments)]
pub(super) async fn handle_live_transcript_file_events(
    watcher: &mut SessionWatcher,
    first_event: WatcherEvent,
    providers: &[ProviderConfig],
    managed_state_dirs: &[PathBuf],
    conn: &rusqlite::Connection,
    transcript_wake_rx: &mut mpsc::UnboundedReceiver<TranscriptWakeSignal>,
    scheduler: &mut PathScheduler,
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
    in_flight: &mut JoinSet<Option<PathTaskResult>>,
    task_context: &PathTaskContext,
    shipping_progress: &mut heartbeat::ShippingProgressObservation,
    offline: bool,
    background_paused: bool,
) -> Vec<PathBuf> {
    // Keep the coalescing wait cancellable by transcript wakes. The wake socket
    // is the managed-session completion lane, so it should not sit behind
    // filesystem batching.
    let flush = tokio::time::sleep(WATCHER_FLUSH_INTERVAL);
    tokio::pin!(flush);
    loop {
        tokio::select! {
            biased;
            Some(signal) = transcript_wake_rx.recv() => {
                if enqueue_transcript_wake_signal(
                    conn,
                    scheduler,
                    latest_transcript_wake_observed,
                    deferred_retries,
                    signal,
                ).is_some() {
                    pump_ready_local_work(
                        scheduler,
                        in_flight,
                        task_context,
                        deferred_retries,
                        shipping_progress,
                        offline,
                        background_paused,
                    );
                }
            }
            _ = &mut flush => {
                break;
            }
        }
    }

    let events = watcher.collect_ready_batch(first_event);
    let (managed_state_changes, transcript_events) =
        partition_managed_state_events(events, managed_state_dirs);
    // Read once per batch: the scope is a file a person can change while the
    // daemon runs, and one read is cheaper than a stat per event.
    let import_scope = crate::config::import_scope();
    for event in transcript_events {
        let Some((session_path, provider)) =
            discovery::session_path_for_watcher_event(&event.path, providers)
        else {
            tracing::debug!(
                "Skipping file outside known providers: {}",
                event.path.display()
            );
            continue;
        };
        // An old session someone resumed writes to a file that was never in
        // scope; shipping its new lines would ship the session it belongs to.
        if !discovery::source_in_import_scope(&import_scope, provider, &session_path) {
            tracing::trace!(
                provider,
                path = %session_path.display(),
                "Skipping live event for a source outside the import scope"
            );
            continue;
        }

        let session_event = WatcherEvent {
            path: session_path,
            observed_at_ms: event.observed_at_ms,
            latest_observed_at_ms: event.latest_observed_at_ms,
        };
        if should_defer_fsevent_for_managed_wake(
            latest_transcript_wake_observed,
            &session_event,
            provider,
        ) {
            let observation = ObservationTrace {
                source: "fsevent",
                observed_at_ms: session_event.observed_at_ms,
                latest_observed_at_ms: Some(
                    session_event
                        .latest_observed_at_ms
                        .max(session_event.observed_at_ms),
                ),
                wake_received_at_ms: None,
                enqueued_at_ms: now_ms(),
                session_id: None,
                turn_id: None,
                wake_reason: None,
                file_len_hint: None,
            };
            deferred_retries.insert(
                session_event.path.clone(),
                DeferredRetry {
                    due_at: Instant::now() + MANAGED_WAKE_FSEVENT_FALLBACK_DELAY,
                    provider,
                    priority: WorkPriority::Live,
                    observation,
                },
            );
            tracing::debug!(
                provider,
                path = %session_event.path.display(),
                observed_at_ms = session_event.observed_at_ms,
                latest_observed_at_ms = session_event.latest_observed_at_ms,
                delay_ms = MANAGED_WAKE_FSEVENT_FALLBACK_DELAY.as_millis(),
                "Deferring filesystem live ship because managed wake socket owns this turn"
            );
            continue;
        }

        if retry_admission_open(&session_event.path, deferred_retries) {
            scheduler.enqueue_observed_window(
                session_event.path,
                provider,
                WorkPriority::Live,
                "fsevent",
                session_event.observed_at_ms,
                session_event.latest_observed_at_ms,
            );
        }
    }
    managed_state_changes
}

#[cfg(unix)]
pub(super) fn spawn_transcript_wake_listener(
    tx: mpsc::UnboundedSender<TranscriptWakeSignal>,
) -> Result<Option<tokio::task::JoinHandle<()>>> {
    let socket_path = config::get_agent_transcript_wake_socket_path()?;
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let _ = std::fs::remove_file(&socket_path);
    let listener = tokio::net::UnixListener::bind(&socket_path)?;
    tracing::debug!(
        path = %socket_path.display(),
        "Transcript wake listener started"
    );
    Ok(Some(tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            let tx = tx.clone();
            tokio::spawn(async move {
                let mut buf = Vec::with_capacity(1024);
                if stream.read_to_end(&mut buf).await.is_err() {
                    return;
                }
                let Ok(mut signal) = serde_json::from_slice::<TranscriptWakeSignal>(&buf) else {
                    return;
                };
                signal.received_at_ms = Some(now_ms());
                let _ = tx.send(signal);
            });
        }
    })))
}

#[cfg(not(unix))]
pub(super) fn spawn_transcript_wake_listener(
    _tx: mpsc::UnboundedSender<TranscriptWakeSignal>,
) -> Result<Option<tokio::task::JoinHandle<()>>> {
    Ok(None)
}

/// Whether a managed transcript wake is enough evidence to schedule a ship.
///
/// Most managed providers publish a completion wake, and waiting for it
/// coalesces the many filesystem events a single turn produces. OMP and Pi
/// never publish one: their managed channel reports ``binding``, ``phase``,
/// and ``progress``. Requiring completion therefore left their transcripts to
/// the filesystem watcher alone, and that watcher is bounded with
/// drop-on-full — which left a live managed session 17 minutes and 650 KB
/// behind its own terminal while its events were lost in the channel.
///
/// A provider with no completion lane ships on every wake instead. The
/// shipper is a no-op when the lane is already current, so a redundant wake
/// costs a scheduling pass, not a duplicate envelope.
pub(super) fn transcript_wake_ships(provider: &str, wake_reason: Option<&str>) -> bool {
    wake_reason == Some("turn_completed") || matches!(provider, "omp" | "pi")
}

pub(super) fn record_transcript_wake_hint(
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    mut signal: TranscriptWakeSignal,
) -> Option<(PathBuf, &'static str, ObservationTrace)> {
    let Some(provider) = discovery::canonical_provider_name(&signal.provider) else {
        tracing::debug!(
            provider = %signal.provider,
            "Skipping transcript wake for unknown provider"
        );
        return None;
    };
    if let std::borrow::Cow::Owned(path) =
        discovery::canonical_transcript_hint(provider, &signal.path)
    {
        signal.file_len_hint = path.metadata().ok().map(|metadata| metadata.len());
        signal.path = path;
    }
    if !signal.path.exists() {
        tracing::debug!(
            provider = %signal.provider,
            path = %signal.path.display(),
            "Skipping transcript wake for missing path"
        );
        return None;
    }
    if !remember_transcript_wake_observation(
        latest_transcript_wake_observed,
        &signal.path,
        signal.observed_at_ms,
    ) {
        tracing::debug!(
            provider = %signal.provider,
            path = %signal.path.display(),
            phase = %signal.phase,
            wake_reason = signal.wake_reason.as_deref().unwrap_or("unknown"),
            observed_at_ms = signal.observed_at_ms,
            "Skipping stale transcript wake"
        );
        return None;
    }
    let should_ship = transcript_wake_ships(provider, signal.wake_reason.as_deref());
    tracing::debug!(
        provider,
        path = %signal.path.display(),
        phase = %signal.phase,
        wake_reason = signal.wake_reason.as_deref().unwrap_or("unknown"),
        observed_at_ms = signal.observed_at_ms,
        received_at_ms = signal.received_at_ms.unwrap_or(0),
        session_id = signal.session_id.as_deref().unwrap_or("unknown"),
        turn_id = signal.turn_id.as_deref().unwrap_or("unknown"),
        file_len_hint = signal.file_len_hint.unwrap_or(0),
        should_ship,
        "Transcript wake recorded"
    );
    if !should_ship {
        return None;
    }
    Some((
        signal.path,
        provider,
        ObservationTrace {
            source: "wake_socket",
            observed_at_ms: signal.observed_at_ms,
            latest_observed_at_ms: None,
            wake_received_at_ms: signal.received_at_ms,
            enqueued_at_ms: now_ms(),
            session_id: signal.session_id,
            turn_id: signal.turn_id,
            wake_reason: signal.wake_reason,
            file_len_hint: signal.file_len_hint,
        },
    ))
}

/// Project what the managed scan observed about each run onto its binding.
///
/// This is the one place a running-or-not fact reaches `session_binding.state`:
/// the provider process evidence in the helm observation (`live`), not the
/// transcript file merely still existing. A launch that is no longer live
/// stopped owning its source, but ownership ending must not cancel replication
/// debt: the binding still says which session owns the file, so it stays in the
/// reconciler's working set until its tail ships. A live one is active again.
/// Only OMP and Pi runs are observed this way; the other providers' bindings
/// move on their launch claims (`managed_source_claim`).
///
/// The scan restates this every pass, so a binding already in the observed
/// state is left alone. Several state files can name one transcript (a resumed
/// run beside its retired predecessor); the file is live if any of them is.
pub(super) fn project_binding_liveness(
    conn: &rusqlite::Connection,
    observations: &ManagedObservationSnapshot,
) {
    let mut observed: std::collections::BTreeMap<(PathBuf, String), bool> = Default::default();
    let runs = observations
        .omp
        .iter()
        .map(|observation| {
            (
                &observation.session_file,
                &observation.session_id,
                observation.live,
            )
        })
        .chain(observations.pi.iter().map(|observation| {
            (
                &observation.session_file,
                &observation.session_id,
                observation.live,
            )
        }));
    for (session_file, session_id, live) in runs {
        let Some(path) = session_file else { continue };
        let canonical = std::fs::canonicalize(path).unwrap_or_else(|_| path.clone());
        *observed
            .entry((canonical, session_id.to_ascii_lowercase()))
            .or_default() |= live;
    }
    let bindings = crate::state::session_binding::SessionBinding::new(conn);
    for ((path, session_id), live) in observed {
        let state = if live {
            crate::state::session_binding::BINDING_STATE_ACTIVE
        } else {
            crate::state::session_binding::BINDING_STATE_EXITED
        };
        let _ = bindings.set_state_for_owner(&path.to_string_lossy(), &session_id, state);
    }
}

pub(super) fn enqueue_transcript_wake_signal(
    conn: &rusqlite::Connection,
    scheduler: &mut PathScheduler,
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    deferred_retries: &mut HashMap<PathBuf, DeferredRetry>,
    signal: TranscriptWakeSignal,
) -> Option<PathBuf> {
    if let Some((path, provider, observation)) =
        record_transcript_wake_hint(latest_transcript_wake_observed, signal)
    {
        persist_cursor_agent_transcript_binding(conn, &path, provider, &observation);
        let scheduled_path = path.clone();
        if retry_admission_open(&path, deferred_retries) {
            scheduler.enqueue_observation(path, provider, WorkPriority::Live, observation);
        }
        Some(scheduled_path)
    } else {
        None
    }
}

/// Preserve the managed session identity before a wake-triggered live job runs.
///
/// Cursor's agent-transcript JSONL is rewritten during a turn. A completion
/// wake carries the exact Longhouse session, but the provider can rewrite the
/// file again before the live worker gets its turn. If a forced `ship` scan
/// wins that race, it has no `ObservationTrace` override and would otherwise
/// create the replacement source epoch from Cursor's provider conversation ID.
/// The binding is only for this provider-owned projection and is deliberately
/// best-effort: the live job still carries the wake override, while a failed
/// SQLite write must not prevent the transcript from being scheduled.
pub(super) fn persist_cursor_agent_transcript_binding(
    conn: &rusqlite::Connection,
    path: &Path,
    provider: &str,
    observation: &ObservationTrace,
) {
    if provider != "cursor"
        || !path
            .components()
            .any(|component| component.as_os_str() == "agent-transcripts")
    {
        return;
    }
    let Some(session_id) = observation.session_id.as_deref() else {
        return;
    };
    let canonical = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    if let Err(error) = crate::state::session_binding::SessionBinding::new(conn).bind(
        &canonical.to_string_lossy(),
        session_id,
        provider,
    ) {
        tracing::warn!(
            path = %canonical.display(),
            session_id,
            error = %error,
            "Unable to persist Cursor agent-transcript wake binding"
        );
    }
}

pub(super) fn should_defer_fsevent_for_managed_wake(
    latest_transcript_wake_observed: &HashMap<PathBuf, i64>,
    event: &WatcherEvent,
    provider: &str,
) -> bool {
    if provider != "codex" {
        return false;
    }
    let Some(last_wake_observed_at_ms) = latest_transcript_wake_observed.get(&event.path) else {
        return false;
    };
    let suppress_ms = MANAGED_WAKE_FSEVENT_DEFER_WINDOW.as_millis() as i64;
    now_ms().saturating_sub(*last_wake_observed_at_ms) <= suppress_ms
}

pub(super) fn remember_transcript_wake_observation(
    latest_transcript_wake_observed: &mut HashMap<PathBuf, i64>,
    path: &Path,
    observed_at_ms: i64,
) -> bool {
    // Bridge wakes can arrive out of order; keep the newest wake per transcript
    // path so a late binding wake cannot resurrect an already-completed turn.
    if let Some(latest_observed_at_ms) = latest_transcript_wake_observed.get(path) {
        if observed_at_ms <= *latest_observed_at_ms {
            return false;
        }
    }

    if !latest_transcript_wake_observed.contains_key(path)
        && latest_transcript_wake_observed.len() >= MAX_TRANSCRIPT_WAKE_TRACKED_PATHS
    {
        if let Some(oldest_path) = latest_transcript_wake_observed
            .iter()
            .min_by_key(|(_, observed)| *observed)
            .map(|(oldest_path, _)| oldest_path.clone())
        {
            latest_transcript_wake_observed.remove(&oldest_path);
        }
    }

    latest_transcript_wake_observed.insert(path.to_path_buf(), observed_at_ms);
    true
}

#[cfg(test)]
pub(super) fn ignore_transcript_shipping_for_codex_observations(
    observations: &[managed_bridge_scan::CodexBridgeObservation],
) {
    if !observations.is_empty() {
        let transcript_path_hints = observations
            .iter()
            .filter(|observation| observation.thread_path.is_some())
            .count();
        tracing::debug!(
            observation_count = observations.len(),
            transcript_path_hints,
            "Codex bridge observations do not schedule transcript shipping"
        );
    }
}

#[cfg(test)]
pub(super) fn resolve_transcript_path_for_session(
    conn: &rusqlite::Connection,
    session_id: &str,
    provider: &str,
) -> Option<PathBuf> {
    find_transcript_path(conn, "file_state", session_id, provider)
        .or_else(|| find_transcript_path(conn, "session_binding", session_id, provider))
}

#[cfg(test)]
pub(super) fn find_transcript_path(
    conn: &rusqlite::Connection,
    table: &str,
    session_id: &str,
    provider: &str,
) -> Option<PathBuf> {
    let order_column = match table {
        "file_state" => "last_updated",
        "session_binding" => "updated_at",
        _ => return None,
    };
    let sql = format!(
        "SELECT path FROM {table} WHERE session_id = ?1 AND provider = ?2 ORDER BY {order_column} DESC LIMIT 1",
    );
    let result = conn.query_row(&sql, rusqlite::params![session_id, provider], |row| {
        row.get::<_, String>(0)
    });
    match result {
        Ok(path) if Path::new(&path).exists() => Some(PathBuf::from(path)),
        Ok(_) | Err(rusqlite::Error::QueryReturnedNoRows) => None,
        Err(err) => {
            tracing::debug!(
                error = %err,
                table,
                session_id,
                provider,
                "Failed to resolve transcript path"
            );
            None
        }
    }
}

impl DaemonState {
    pub(super) fn on_transcript_wake(
        &mut self,
        config: &ConnectConfig,
        signal: TranscriptWakeSignal,
    ) {
        if enqueue_transcript_wake_signal(
            &self.conn,
            &mut self.scheduler,
            &mut self.latest_transcript_wake_observed,
            &mut self.deferred_retries,
            signal,
        )
        .is_some()
        {
            pump_ready_local_work(
                &mut self.scheduler,
                &mut self.in_flight,
                &self.task_context,
                &mut self.deferred_retries,
                &mut self.shipping_progress,
                self.offline.is_offline,
                archive_repair_is_paused(config.archive_repair_mode),
            );
        }
    }
}
