//! Status slots: phase recording, owner evidence and posting to the host.

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
/// Record each slot's phase in the local ledger. The daemon is the ledger's
/// single writer, which is why a provider callback no longer needs to hand it
/// a file per frame.
pub(super) fn record_status_slot_phases(
    db_path: Option<&Path>,
    slots: &[crate::status_slot::StatusSlot],
    already_recorded: &HashMap<String, (String, u64)>,
) -> Vec<(String, (String, u64))> {
    let pending: Vec<&crate::status_slot::StatusSlot> = slots
        .iter()
        .filter(|slot| already_recorded.get(&slot.session_id) != Some(&slot.version()))
        .collect();
    if pending.is_empty() {
        return Vec::new();
    }
    let Ok(path) = crate::state::db::resolve_db_path(db_path) else {
        return Vec::new();
    };
    let Ok(conn) = crate::state::db::open_connection(&path) else {
        return Vec::new();
    };
    let store = crate::state::session_phase::SessionPhaseStore::new(&conn);
    let mut recorded = Vec::new();
    for slot in pending {
        let Ok(observed_at) = chrono::DateTime::parse_from_rfc3339(&slot.observed_at) else {
            continue;
        };
        let signal = crate::state::session_phase::SessionPhaseSignal {
            session_id: slot.session_id.clone(),
            provider: slot.provider.clone(),
            phase: slot.phase.clone(),
            tool_name: slot.tool_name.clone(),
            source: slot.source.clone(),
            observed_at: observed_at.with_timezone(&chrono::Utc),
            // The slot already carries the run the launcher was started into, so
            // the phase row keeps its own identity rather than being attributed
            // by time later.
            run_id: (!slot.run_id.trim().is_empty()).then(|| slot.run_id.clone()),
        };
        match store.record(&signal) {
            Ok(_) => recorded.push((slot.session_id.clone(), slot.version())),
            Err(error) => {
                tracing::warn!(
                    session_id = %slot.session_id,
                    error = %error,
                    "Recording a status slot phase failed"
                );
            }
        }
    }
    recorded
}

/// Select the current slot observation for delivery without confusing a
/// permanent rejection with an accepted watermark. Rejected versions retry at
/// the assertion cadence; a newer version always replaces the marker.
pub(super) fn status_slot_pending(
    slot: &crate::status_slot::StatusSlot,
    sent: Option<&((String, u64), Instant)>,
    rejected: Option<&RejectedStatusSlot>,
    now: Instant,
) -> Option<bool> {
    let version = slot.version();
    if let Some(rejected) = rejected.filter(|rejected| rejected.version == version) {
        return (rejected.retry_at <= now).then_some(rejected.changed);
    }
    match sent {
        None => Some(true),
        Some((sent_version, _)) if sent_version != &version => Some(true),
        Some((_, sent_at))
            if sent_at.elapsed() >= crate::status_slot::STATUS_ASSERTION_INTERVAL =>
        {
            Some(false)
        }
        Some(_) => None,
    }
}

pub(super) fn status_owner_key(
    provider: &str,
    session_id: &str,
    run_id: &str,
) -> Option<StatusOwnerKey> {
    (!provider.trim().is_empty() && !session_id.trim().is_empty() && !run_id.trim().is_empty())
        .then(|| StatusOwnerKey {
            provider: provider.to_string(),
            session_id: session_id.to_string(),
            run_id: run_id.to_string(),
        })
}
pub(super) fn status_owner_snapshot_is_fresh(observed_at: Option<Instant>) -> bool {
    observed_at.is_some_and(|observed_at| observed_at.elapsed() <= STATUS_OWNER_SNAPSHOT_MAX_AGE)
}
pub(super) fn status_slot_uses_turn_claim(slot: &crate::status_slot::StatusSlot) -> bool {
    matches!(
        slot.payload
            .get("execution_lifetime")
            .and_then(serde_json::Value::as_str),
        Some("one_shot" | "persistent")
    )
}

pub(super) fn claim_has_current_process_identity(
    claim: &crate::turn_claims::TurnClaim,
    current_boot_id: Option<&str>,
) -> bool {
    matches!(
        (claim.boot_id.as_deref(), current_boot_id),
        (Some(recorded), Some(current)) if recorded == current
    ) && claim.pid.is_some_and(|pid| pid > 1)
        && claim
            .process_start_time
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty())
}
pub(super) fn managed_status_state_file(slot: &crate::status_slot::StatusSlot) -> Option<PathBuf> {
    let session_path = Path::new(&slot.session_id);
    if session_path.file_name().and_then(|name| name.to_str()) != Some(slot.session_id.as_str())
        || session_path.components().count() != 1
        || !matches!(
            session_path.components().next(),
            Some(std::path::Component::Normal(_))
        )
    {
        return None;
    }
    let state_dir = match slot.provider.as_str() {
        "codex" => managed_bridge_scan::default_codex_bridge_state_dir(),
        "claude" => managed_claude_scan::default_claude_channel_state_dir(),
        "opencode" => managed_opencode_scan::default_opencode_server_state_dir(),
        "cursor" => managed_cursor_helm_scan::default_cursor_helm_state_dir(),
        "pi" => managed_pi_helm_scan::default_pi_helm_state_dir(),
        "omp" => managed_omp_helm_scan::default_omp_helm_state_dir(),
        _ => None,
    }?;
    Some(state_dir.join(format!("{}.json", slot.session_id)))
}

pub(super) fn managed_status_observation_matches_slot(
    slot: &crate::status_slot::StatusSlot,
    provider: &str,
    session_id: &str,
    run_id: Option<&str>,
) -> bool {
    slot.provider == provider
        && slot.session_id == session_id
        && run_id == Some(slot.run_id.as_str())
}

pub(super) fn managed_status_observations_for_slots(
    slots: &[crate::status_slot::StatusSlot],
) -> ManagedObservationScanResult {
    let mut scan = ManagedObservationScanResult::default();
    let no_processes: HashMap<u32, crate::process_identity::ProcessFact> = HashMap::new();
    for slot in slots {
        let Some(path) = managed_status_state_file(slot) else {
            continue;
        };
        let paths = [path];
        match slot.provider.as_str() {
            "codex" => scan.codex_observations.extend(
                managed_bridge_scan::collect_observations_from_paths(&paths, &no_processes)
                    .into_iter()
                    .filter(|observation| {
                        managed_status_observation_matches_slot(
                            slot,
                            "codex",
                            &observation.session_id,
                            observation.run_id.as_deref(),
                        )
                    }),
            ),
            "claude" => scan.claude_observations.extend(
                managed_claude_scan::collect_observations_from_paths(&paths, &no_processes)
                    .into_iter()
                    .filter(|observation| {
                        managed_status_observation_matches_slot(
                            slot,
                            "claude",
                            &observation.session_id,
                            observation.run_id.as_deref(),
                        )
                    }),
            ),
            "opencode" => scan.opencode_observations.extend(
                managed_opencode_scan::collect_observations_from_paths(&paths, &no_processes)
                    .into_iter()
                    .filter(|observation| {
                        managed_status_observation_matches_slot(
                            slot,
                            "opencode",
                            &observation.session_id,
                            observation.run_id.as_deref(),
                        )
                    }),
            ),
            "cursor" => scan.cursor_observations.extend(
                managed_cursor_helm_scan::collect_observations_from_paths(&paths, &no_processes)
                    .into_iter()
                    .filter(|observation| {
                        managed_status_observation_matches_slot(
                            slot,
                            "cursor",
                            &observation.session_id,
                            observation.run_id.as_deref(),
                        )
                    }),
            ),
            "pi" => scan.pi_observations.extend(
                managed_pi_helm_scan::collect_observations_from_paths(&paths, &no_processes)
                    .into_iter()
                    .filter(|observation| {
                        managed_status_observation_matches_slot(
                            slot,
                            "pi",
                            &observation.session_id,
                            observation.run_id.as_deref(),
                        )
                    }),
            ),
            "omp" => scan.omp_observations.extend(
                managed_omp_helm_scan::collect_observations_from_paths(&paths, &no_processes)
                    .into_iter()
                    .filter(|observation| {
                        managed_status_observation_matches_slot(
                            slot,
                            "omp",
                            &observation.session_id,
                            observation.run_id.as_deref(),
                        )
                    }),
            ),
            _ => {}
        }
    }
    scan
}

pub(super) fn managed_status_owner_pids(scan: &ManagedObservationScanResult) -> Vec<u32> {
    let mut pids = Vec::new();
    for observation in &scan.codex_observations {
        pids.push(observation.bridge_pid);
        pids.extend(observation.app_server_pid);
    }
    for observation in &scan.claude_observations {
        pids.extend(observation.claude_pid);
        pids.extend(observation.bridge_pid);
    }
    for observation in &scan.opencode_observations {
        pids.extend(observation.pid);
    }
    for observation in &scan.cursor_observations {
        pids.extend(observation.launcher_pid);
        pids.extend(observation.cursor_pid);
    }
    for observation in &scan.pi_observations {
        pids.extend(observation.launcher_pid);
        pids.extend(observation.provider_pid);
    }
    for observation in &scan.omp_observations {
        pids.extend(observation.launcher_pid);
        pids.extend(observation.provider_pid);
    }
    pids.retain(|pid| *pid > 1);
    pids.sort_unstable();
    pids.dedup();
    pids
}

pub(super) fn status_owner_evidence_for_slots(
    slots: &[crate::status_slot::StatusSlot],
    claims: &[crate::turn_claims::TurnClaim],
    previous: &StatusOwnerEvidence,
    previous_is_fresh: bool,
    current_boot_id: Option<&str>,
    refresh_cursor: usize,
    managed_refresh_cursor: usize,
) -> (StatusOwnerEvidence, usize, usize) {
    let mut evidence = StatusOwnerEvidence::default();
    let mut unresolved_claims = Vec::new();
    let mut unresolved_managed_slots = Vec::new();
    for slot in slots {
        let Some(owner) = status_owner_key(&slot.provider, &slot.session_id, &slot.run_id) else {
            continue;
        };
        if status_slot_uses_turn_claim(slot) {
            let Some(claim) = claims.iter().find(|claim| {
                claim.provider == slot.provider
                    && claim.session_id == slot.session_id
                    && claim.run_id == slot.run_id
            }) else {
                continue;
            };
            let identity = StatusOwnerClaimIdentity::from(claim);
            if turn_claim_has_ended_status(claim) {
                evidence.claim_identities.insert(owner.clone(), identity);
                if claim.has_pending_runtime_handoff() {
                    evidence.terminal_pending.insert(owner);
                } else {
                    evidence.ended.insert(owner);
                }
                continue;
            }
            if claim.state != "spawned" {
                continue;
            }
            if matches!(
                (claim.boot_id.as_deref(), current_boot_id),
                (Some(recorded), Some(current)) if recorded != current
            ) {
                evidence.claim_identities.insert(owner.clone(), identity);
                evidence.ended.insert(owner);
                continue;
            }
            if previous_is_fresh && previous.claim_identities.get(&owner) == Some(&identity) {
                if previous.terminal_pending.contains(&owner) {
                    evidence.terminal_pending.insert(owner);
                    continue;
                }
                if previous.ended.contains(&owner) {
                    evidence.claim_identities.insert(owner.clone(), identity);
                    evidence.ended.insert(owner);
                    continue;
                }
                if previous.active.contains(&owner) {
                    evidence.claim_identities.insert(owner.clone(), identity);
                    evidence.active.insert(owner);
                    continue;
                }
            }
            if claim_has_current_process_identity(claim, current_boot_id) {
                unresolved_claims.push(claim.clone());
            }
        } else if previous_is_fresh {
            if previous.terminal_pending.contains(&owner) {
                evidence.terminal_pending.insert(owner);
            } else if previous.ended.contains(&owner) {
                evidence.ended.insert(owner);
            } else if previous.active.contains(&owner) {
                evidence.active.insert(owner);
            } else if managed_status_state_file(slot).is_some_and(|path| path.is_file()) {
                unresolved_managed_slots.push(slot.clone());
            }
        } else if managed_status_state_file(slot).is_some_and(|path| path.is_file()) {
            unresolved_managed_slots.push(slot.clone());
        }
    }

    unresolved_claims.sort_unstable_by(|left, right| left.run_id.cmp(&right.run_id));
    let claim_start = if unresolved_claims.is_empty() {
        0
    } else {
        refresh_cursor % unresolved_claims.len()
    };
    let selected_claim_count = unresolved_claims.len().min(STATUS_OWNER_REFRESH_BATCH_SIZE);
    let selected_claims = (0..selected_claim_count)
        .map(|offset| unresolved_claims[(claim_start + offset) % unresolved_claims.len()].clone())
        .collect::<Vec<_>>();
    let next_claim_cursor = if selected_claim_count == unresolved_claims.len() {
        0
    } else {
        (claim_start + selected_claim_count) % unresolved_claims.len()
    };

    unresolved_managed_slots.sort_unstable_by(|left, right| {
        left.provider
            .cmp(&right.provider)
            .then_with(|| left.session_id.cmp(&right.session_id))
            .then_with(|| left.run_id.cmp(&right.run_id))
    });
    let managed_start = if unresolved_managed_slots.is_empty() {
        0
    } else {
        managed_refresh_cursor % unresolved_managed_slots.len()
    };
    let selected_managed_count = unresolved_managed_slots
        .len()
        .min(STATUS_MANAGED_OWNER_REFRESH_BATCH_SIZE);
    let selected_managed_slots = (0..selected_managed_count)
        .map(|offset| {
            unresolved_managed_slots[(managed_start + offset) % unresolved_managed_slots.len()]
                .clone()
        })
        .collect::<Vec<_>>();
    let next_managed_cursor = if selected_managed_count == unresolved_managed_slots.len() {
        0
    } else {
        (managed_start + selected_managed_count) % unresolved_managed_slots.len()
    };
    let mut managed_scan = managed_status_observations_for_slots(&selected_managed_slots);
    let mut pids = selected_claims
        .iter()
        .filter_map(|claim| claim.pid)
        .collect::<Vec<_>>();
    pids.extend(managed_status_owner_pids(&managed_scan));
    if let Some(process_facts) = crate::process_identity::try_collect_process_facts_for_pids(&pids)
    {
        evidence.merge(status_owner_evidence_from_claims(
            &selected_claims,
            Some(&process_facts),
            current_boot_id,
        ));
        managed_scan.process_inventory_valid = true;
        evidence.merge(status_owner_evidence_from_scan(
            &managed_scan,
            &process_facts,
        ));
    } else {
        evidence.merge(status_owner_evidence_from_scan(
            &managed_scan,
            &HashMap::new(),
        ));
    }
    (evidence, next_claim_cursor, next_managed_cursor)
}

/// Keep only statuses backed by the exact current run owner.
///
/// Unknown ownership never renews liveness. A terminal event whose durable
/// handoff is pending suppresses status but keeps both local records for retry.
pub(super) fn reconcile_status_slots(
    dir: &Path,
    slots: Vec<crate::status_slot::StatusSlot>,
    owners: &StatusOwnerEvidence,
) -> Vec<crate::status_slot::StatusSlot> {
    slots
        .into_iter()
        .filter_map(|slot| {
            let key = status_owner_key(&slot.provider, &slot.session_id, &slot.run_id);
            if key
                .as_ref()
                .is_some_and(|key| owners.terminal_pending.contains(key))
            {
                return None;
            }
            if key.as_ref().is_some_and(|key| owners.active.contains(key)) {
                return Some(slot);
            }
            if key.as_ref().is_some_and(|key| owners.ended.contains(key)) {
                if let Err(error) =
                    crate::status_slot::retire_if_run(dir, &slot.session_id, &slot.run_id)
                {
                    tracing::warn!(
                        session_id = %slot.session_id,
                        run_id = %slot.run_id,
                        %error,
                        "Could not retire an ended status slot"
                    );
                }
            } else if let Err(error) = crate::status_slot::discard_expired_observation(
                dir, &slot, chrono::Utc::now(),
            ) {
                tracing::warn!(session_id = %slot.session_id, %error, "Could not discard expired status observation");
            }
            None
        })
        .collect()
}
/// Deliver current status and report which versions the host accepted or
/// permanently rejected. Nothing is queued and nothing is deleted: the slot
/// is the durable copy, so a session whose send fails is simply sent again
/// with whatever it says by then.
#[allow(unused_imports)]
pub(super) async fn post_status_slots(
    client: &crate::shipping::client::ShipperClient,
    slots: Vec<PendingStatus>,
) -> StatusPostResult {
    use futures_util::StreamExt;
    // Sessions are independent, so one whose send keeps failing must not hold
    // up everyone else's current status.
    let outcomes = futures_util::stream::iter(slots.into_iter().map(
        |PendingStatus {
             slot,
             changed,
             stated_preview,
         }| async move {
            let events: Vec<outbox::PendingRuntimeEventPost> = if changed {
                crate::status_slot::runtime_events_since(&slot, stated_preview.as_ref())
            } else {
                // Unchanged: the machine is restating, not reporting. A phase
                // shipped again would bump the host's runtime revision and
                // re-anchor the phase for a statement that says nothing new about
                // the provider.
                vec![crate::status_slot::assertion_runtime_event(
                    &slot,
                    chrono::Utc::now(),
                )]
            }
            .into_iter()
            .map(outbox::PendingRuntimeEventPost::from_event)
            .collect();
            let expected = events.len();
            let version = slot.version();
            // What the host holds of this session's preview once the post
            // lands: the one this slot carries, sent now or already stated. An
            // assertion says nothing about the preview, so it leaves it be.
            let preview = changed.then(|| slot.preview_identity()).flatten();
            let outcome =
                outbox::post_pending_runtime_event_files_with_outcome(client, events).await;
            if outcome.sent == expected && outcome.kept == 0 {
                StatusPostResult {
                    previews: preview
                        .map(|preview| (slot.session_id.clone(), preview))
                        .into_iter()
                        .collect(),
                    accepted: vec![(slot.session_id, version)],
                    rejected: Vec::new(),
                }
            } else if outcome
                .permanent_rejections
                .iter()
                .any(|rejection| rejection.session_id == slot.session_id)
            {
                StatusPostResult {
                    accepted: Vec::new(),
                    rejected: vec![StatusPostRejection {
                        session_id: slot.session_id,
                        version,
                        changed,
                    }],
                    previews: Vec::new(),
                }
            } else {
                StatusPostResult::default()
            }
        },
    ))
    .buffer_unordered(STATUS_POST_CONCURRENCY)
    .collect::<Vec<_>>()
    .await;

    outcomes
        .into_iter()
        .fold(StatusPostResult::default(), |mut total, outcome| {
            total.accepted.extend(outcome.accepted);
            total.rejected.extend(outcome.rejected);
            total.previews.extend(outcome.previews);
            total
        })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum ProcessOwnerState {
    Live,
    Ended,
    Unknown,
}

/// Compare recorded PIDs with one already-collected, coherent process table.
/// A missing birth time is unknown while the PID is present; a failed scan is
/// handled by the caller and can never be interpreted as process death.
pub(super) fn recorded_process_owner_state(
    process_facts: &HashMap<u32, crate::process_identity::ProcessFact>,
    owners: &[(Option<u32>, Option<&str>)],
) -> ProcessOwnerState {
    let mut saw_owner = false;
    let mut unknown = false;
    for (pid, recorded_start) in owners {
        match (*pid, *recorded_start) {
            (None, None) => {}
            (Some(pid), recorded_start) if pid > 1 => {
                saw_owner = true;
                let Some(fact) = process_facts.get(&pid) else {
                    continue;
                };
                let Some(recorded_start) = recorded_start.filter(|value| !value.trim().is_empty())
                else {
                    unknown = true;
                    continue;
                };
                if fact.lstart.trim() == recorded_start.trim() {
                    return ProcessOwnerState::Live;
                }
            }
            _ => unknown = true,
        }
    }
    if unknown || !saw_owner {
        ProcessOwnerState::Unknown
    } else {
        ProcessOwnerState::Ended
    }
}

pub(super) fn claude_process_owner_state(
    observation: &managed_claude_scan::ClaudeChannelObservation,
    process_facts: &HashMap<u32, crate::process_identity::ProcessFact>,
) -> ProcessOwnerState {
    let recorded_start = crate::process_identity::parse_rfc3339(&observation.started_at);
    let mut saw_owner = false;
    let mut unknown = false;
    for (pid, is_bridge) in [
        (observation.claude_pid, false),
        (observation.bridge_pid, true),
    ] {
        let Some(pid) = pid else {
            continue;
        };
        if pid <= 1 {
            unknown = true;
            continue;
        }
        saw_owner = true;
        let Some(fact) = process_facts.get(&pid) else {
            continue;
        };
        let Some(recorded_start) = recorded_start.as_ref() else {
            unknown = true;
            continue;
        };
        let command_matches = if is_bridge {
            fact.command.contains("longhouse")
                && fact.command.contains("claude-channel")
                && fact.command.contains("serve")
        } else {
            crate::process_identity::command_contains_basename(&fact.command, "claude")
        };
        if fact.start_time.is_none() {
            unknown = true;
        } else if command_matches
            && crate::process_identity::started_before_or_near_recorded(
                fact,
                Some(recorded_start.clone()),
            )
        {
            return ProcessOwnerState::Live;
        }
    }
    if unknown || !saw_owner {
        ProcessOwnerState::Unknown
    } else {
        ProcessOwnerState::Ended
    }
}

pub(super) fn add_status_owner(
    evidence: &mut StatusOwnerEvidence,
    provider: &str,
    session_id: &str,
    run_id: Option<&str>,
    process_state: ProcessOwnerState,
    explicit_terminal: bool,
    process_inventory_valid: bool,
) {
    // Older Claude channel state may omit its optional run_id. Without that
    // exact generation it cannot authorize any current status slot.
    let Some(key) = run_id.and_then(|run_id| status_owner_key(provider, session_id, run_id)) else {
        return;
    };
    if explicit_terminal {
        evidence.ended.insert(key);
    } else if process_inventory_valid {
        match process_state {
            ProcessOwnerState::Live => {
                evidence.active.insert(key);
            }
            ProcessOwnerState::Ended => {
                evidence.ended.insert(key);
            }
            ProcessOwnerState::Unknown => {}
        }
    }
}

pub(super) fn turn_claim_process_owner_state(
    claim: &crate::turn_claims::TurnClaim,
    process_facts: &HashMap<u32, crate::process_identity::ProcessFact>,
    current_boot_id: Option<&str>,
) -> ProcessOwnerState {
    match (claim.boot_id.as_deref(), current_boot_id) {
        (Some(recorded), Some(current)) if recorded != current => ProcessOwnerState::Ended,
        (Some(_), Some(_)) => recorded_process_owner_state(
            process_facts,
            &[(claim.pid, claim.process_start_time.as_deref())],
        ),
        _ => ProcessOwnerState::Unknown,
    }
}

pub(super) fn turn_claim_has_ended_status(claim: &crate::turn_claims::TurnClaim) -> bool {
    matches!(claim.state.as_str(), "terminal" | "failed")
        || claim.invocation_state.as_deref() == Some("closed")
}

pub(super) fn status_owner_evidence_from_claims(
    claims: &[crate::turn_claims::TurnClaim],
    process_facts: Option<&HashMap<u32, crate::process_identity::ProcessFact>>,
    current_boot_id: Option<&str>,
) -> StatusOwnerEvidence {
    let mut evidence = StatusOwnerEvidence::default();
    for claim in claims {
        let Some(owner) = status_owner_key(&claim.provider, &claim.session_id, &claim.run_id)
        else {
            continue;
        };
        let identity = StatusOwnerClaimIdentity::from(claim);
        if turn_claim_has_ended_status(claim) {
            evidence.claim_identities.insert(owner.clone(), identity);
            if claim.has_pending_runtime_handoff() {
                evidence.terminal_pending.insert(owner);
            } else {
                evidence.ended.insert(owner);
            }
        } else if claim.state == "spawned" {
            let Some(process_facts) = process_facts else {
                continue;
            };
            match turn_claim_process_owner_state(claim, process_facts, current_boot_id) {
                ProcessOwnerState::Live => {
                    evidence.claim_identities.insert(owner.clone(), identity);
                    evidence.active.insert(owner);
                }
                ProcessOwnerState::Ended => {
                    evidence.claim_identities.insert(owner.clone(), identity);
                    evidence.ended.insert(owner);
                }
                ProcessOwnerState::Unknown => {}
            }
        }
    }
    evidence
}

pub(super) fn status_owner_evidence_from_scan(
    scan: &ManagedObservationScanResult,
    process_facts: &HashMap<u32, crate::process_identity::ProcessFact>,
) -> StatusOwnerEvidence {
    let mut evidence = StatusOwnerEvidence::default();

    for observation in &scan.codex_observations {
        let state = if scan.process_inventory_valid {
            recorded_process_owner_state(
                process_facts,
                &[
                    (
                        Some(observation.bridge_pid),
                        observation.bridge_process_start_time.as_deref(),
                    ),
                    (
                        observation.app_server_pid,
                        observation.app_server_process_start_time.as_deref(),
                    ),
                ],
            )
        } else {
            ProcessOwnerState::Unknown
        };
        add_status_owner(
            &mut evidence,
            "codex",
            &observation.session_id,
            observation.run_id.as_deref(),
            state,
            observation.status.eq_ignore_ascii_case("stopped")
                || observation
                    .terminal_state
                    .as_deref()
                    .is_some_and(|value| !value.trim().is_empty()),
            scan.process_inventory_valid,
        );
    }
    for observation in &scan.claude_observations {
        let state = if scan.process_inventory_valid {
            claude_process_owner_state(observation, process_facts)
        } else {
            ProcessOwnerState::Unknown
        };
        add_status_owner(
            &mut evidence,
            "claude",
            &observation.session_id,
            observation.run_id.as_deref(),
            state,
            false,
            scan.process_inventory_valid,
        );
    }
    for observation in &scan.opencode_observations {
        let state = if scan.process_inventory_valid {
            recorded_process_owner_state(
                process_facts,
                &[(
                    observation.pid,
                    Some(observation.process_start_time.as_str()),
                )],
            )
        } else {
            ProcessOwnerState::Unknown
        };
        add_status_owner(
            &mut evidence,
            "opencode",
            &observation.session_id,
            observation.run_id.as_deref(),
            state,
            false,
            scan.process_inventory_valid,
        );
    }
    for observation in &scan.cursor_observations {
        let state = if scan.process_inventory_valid {
            recorded_process_owner_state(
                process_facts,
                &[
                    (
                        observation.launcher_pid,
                        observation.launcher_process_start_time.as_deref(),
                    ),
                    (
                        observation.cursor_pid,
                        observation.cursor_process_start_time.as_deref(),
                    ),
                ],
            )
        } else {
            ProcessOwnerState::Unknown
        };
        add_status_owner(
            &mut evidence,
            "cursor",
            &observation.session_id,
            observation.run_id.as_deref(),
            state,
            false,
            scan.process_inventory_valid,
        );
    }
    for observation in &scan.pi_observations {
        let state = if scan.process_inventory_valid {
            recorded_process_owner_state(
                process_facts,
                &[
                    (
                        observation.launcher_pid,
                        observation.launcher_process_start_time.as_deref(),
                    ),
                    (
                        observation.provider_pid,
                        observation.provider_process_start_time.as_deref(),
                    ),
                ],
            )
        } else {
            ProcessOwnerState::Unknown
        };
        add_status_owner(
            &mut evidence,
            "pi",
            &observation.session_id,
            observation.run_id.as_deref(),
            state,
            observation.status.eq_ignore_ascii_case("stopped")
                || observation.status.eq_ignore_ascii_case("failed"),
            scan.process_inventory_valid,
        );
    }
    for observation in &scan.omp_observations {
        let state = if scan.process_inventory_valid {
            recorded_process_owner_state(
                process_facts,
                &[
                    (
                        observation.launcher_pid,
                        observation.launcher_process_start_time.as_deref(),
                    ),
                    (
                        observation.provider_pid,
                        observation.provider_process_start_time.as_deref(),
                    ),
                ],
            )
        } else {
            ProcessOwnerState::Unknown
        };
        add_status_owner(
            &mut evidence,
            "omp",
            &observation.session_id,
            observation.run_id.as_deref(),
            state,
            observation.status.eq_ignore_ascii_case("stopped"),
            scan.process_inventory_valid,
        );
    }

    let ended = &evidence.ended;
    evidence.active.retain(|owner| !ended.contains(owner));
    evidence
}

/// Read only claims named by current status slots, before the shared process
/// inventory. The one-second slot pass uses the same exact match for newly
/// spawned owners that were not in the last managed scan.
pub(super) fn read_status_slot_claims() -> Vec<crate::turn_claims::TurnClaim> {
    let Ok(agent_dir) = crate::config::get_agent_dir() else {
        return Vec::new();
    };
    let slots = crate::status_slot::read_all(&crate::status_slot::status_slot_dir(&agent_dir));
    read_status_slot_claims_for_slots(&slots)
}

pub(super) fn read_status_slot_claims_for_slots(
    slots: &[crate::status_slot::StatusSlot],
) -> Vec<crate::turn_claims::TurnClaim> {
    let Ok(registry) = crate::turn_claims::default_registry() else {
        return Vec::new();
    };
    slots
        .iter()
        .filter_map(|slot| {
            let claim = registry.read(&slot.run_id).ok()?;
            (claim.provider == slot.provider
                && claim.session_id == slot.session_id
                && claim.run_id == slot.run_id)
                .then_some(claim)
        })
        .collect()
}
pub(super) fn retry_pending_terminal_claim_handoffs() {
    let Ok(registry) = crate::turn_claims::default_registry() else {
        return;
    };
    let claims = match registry.list_all_shared() {
        Ok(claims) => claims,
        Err(error) => {
            tracing::warn!(%error, "Could not read terminal claims for status-event replay");
            return;
        }
    };
    let outbox_dir = match crate::config::get_agent_runtime_events_outbox_dir() {
        Ok(outbox_dir) => outbox_dir,
        Err(error) => {
            tracing::warn!(%error, "Could not resolve runtime-event outbox for terminal replay");
            return;
        }
    };
    let mut handed_off = 0;
    for claim in claims.iter().filter(|claim| {
        matches!(claim.state.as_str(), "terminal" | "failed") && claim.has_pending_runtime_handoff()
    }) {
        let replay = (|| {
            if claim.terminal_event.is_some()
                && !crate::outbox::retry_retained_terminal_event(
                    &registry,
                    &outbox_dir,
                    &claim.run_id,
                )?
            {
                return Ok(false);
            }
            if claim.invocation_close_event.is_some() {
                crate::outbox::retry_retained_invocation_close_event(
                    &registry,
                    &outbox_dir,
                    &claim.run_id,
                )
            } else {
                Ok(true)
            }
        })();
        match replay {
            Ok(true) => handed_off += 1,
            Ok(false) => {}
            Err(error) => tracing::warn!(
                run_id = %claim.run_id,
                %error,
                "Retained terminal event remains pending after daemon scan"
            ),
        }
    }
    if handed_off > 0 {
        tracing::info!(
            handed_off,
            "Replayed retained terminal events from turn claims"
        );
    }
}
