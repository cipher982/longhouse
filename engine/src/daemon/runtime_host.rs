//! Daemon loop handlers that talk to the Runtime Host: heartbeats, machine
//! presence, the host link and the offline health check.

use super::*;

impl DaemonState {
    pub(super) fn on_heartbeat_post_done(
        &mut self,
        heartbeat_post_result: Option<Result<HeartbeatPostResult, tokio::task::JoinError>>,
    ) {
        match heartbeat_post_result {
            Some(Ok(result)) => {
                self.heartbeat_transport.record_send_metrics(
                    result.reason,
                    result.metrics.raw_bytes,
                    result.metrics.wire_bytes,
                    result.metrics.latency_ms,
                );
                let local_join_delay_ms = result
                    .join_elapsed_ms
                    .saturating_sub(result.task_elapsed_ms);
                if result.task_elapsed_ms > 1_000 || local_join_delay_ms > 1_000 {
                    if self.host_link.is_updating() {
                        tracing::debug!(
                            reason = result.reason,
                            task_elapsed_ms = result.task_elapsed_ms,
                            join_elapsed_ms = result.join_elapsed_ms,
                            local_join_delay_ms,
                            "Heartbeat POST was delayed during Runtime Host update"
                        );
                    } else {
                        tracing::warn!(
                            reason = result.reason,
                            task_elapsed_ms = result.task_elapsed_ms,
                            join_elapsed_ms = result.join_elapsed_ms,
                            local_join_delay_ms,
                            "Heartbeat POST was slow"
                        );
                    }
                }
                self.acknowledged_machine_evidence
                    .record_send_result(&result.result, &result.sent_evidence_identities);
                if let Some(evidence) = self
                    .last_status_projection
                    .as_ref()
                    .and_then(|projection| projection.payload.machine_evidence.as_ref())
                {
                    self.acknowledged_machine_evidence
                        .prune_to_current(&evidence.candidate_identities);
                } else {
                    self.acknowledged_machine_evidence.prune_to_current(&[]);
                }
                match result.result {
                    Ok(ack) => {
                        let evidence_was_refused = self.heartbeat_transport.evidence_refused();
                        let evidence_changed = self
                            .heartbeat_transport
                            .record_evidence_ack(ack.evidence_ack.as_deref());
                        let evidence_refused = self.heartbeat_transport.evidence_refused();
                        let recovered = self
                            .heartbeat_transport
                            .record_success(chrono::Utc::now().to_rfc3339());
                        tracing::debug!(
                            reason = result.reason,
                            task_elapsed_ms = result.task_elapsed_ms,
                            join_elapsed_ms = result.join_elapsed_ms,
                            evidence_state = %self.heartbeat_transport.evidence_state,
                            "Runtime truth snapshot sent after local process/control change"
                        );
                        if evidence_changed && evidence_refused {
                            tracing::warn!(
                                reason = result.reason,
                                evidence_state = %self.heartbeat_transport.evidence_state,
                                "Heartbeat machine evidence was refused"
                            );
                        } else if evidence_was_refused
                            && self.heartbeat_transport.evidence_state == "applied"
                        {
                            tracing::info!("Heartbeat machine evidence recovered");
                        }
                        if recovered {
                            tracing::info!("Heartbeat POST recovered");
                        }
                        self.last_runtime_truth_signature = Some(result.signature.clone());
                        if self
                            .pending_truth_heartbeat
                            .as_ref()
                            .is_some_and(|pending| pending.signature == result.signature)
                        {
                            self.pending_truth_heartbeat = None;
                        }
                    }
                    Err(err) => {
                        let error = heartbeat::bounded_heartbeat_error(&err);
                        let transitioned = self
                            .heartbeat_transport
                            .record_failure(chrono::Utc::now().to_rfc3339(), &error);
                        if self
                            .pending_truth_heartbeat
                            .as_ref()
                            .is_some_and(|pending| pending.signature == result.signature)
                        {
                            self.pending_truth_heartbeat = None;
                        }
                        // The failed send did not deliver this truth: forget it, so the
                        // next projection retries it (after the 1 s window) instead of
                        // waiting for the 60 s periodic heartbeat.
                        if self.last_runtime_truth_signature.as_deref()
                            == Some(result.signature.as_str())
                        {
                            self.last_runtime_truth_signature = None;
                        }
                        if self.host_link.explains_failure(&error) {
                            tracing::debug!(
                                reason = result.reason,
                                error = %error,
                                retry_after_secs = result.metrics.retry_after
                                    .map(|delay| delay.as_secs_f64()),
                                "Heartbeat POST deferred during Runtime Host update"
                            );
                        } else if transitioned {
                            tracing::warn!(
                                reason = result.reason,
                                error = %error,
                                "Heartbeat POST failed"
                            );
                        } else {
                            tracing::debug!(
                                reason = result.reason,
                                error = %error,
                                "Heartbeat POST remains degraded"
                            );
                        }
                    }
                }
                publish_heartbeat_transport_status(
                    &self.heartbeat_transport,
                    &mut self.last_status_projection,
                    serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.host_link,
                    &self.status_path,
                );
            }
            Some(Err(err)) => {
                let error = heartbeat::bounded_heartbeat_error(&err.to_string());
                let transitioned = self
                    .heartbeat_transport
                    .record_failure(chrono::Utc::now().to_rfc3339(), &error);
                if self.host_link.explains_failure(&error) {
                    tracing::debug!(error = %error, "Heartbeat POST task deferred during Runtime Host update");
                } else if transitioned {
                    tracing::warn!(error = %error, "Heartbeat POST task failed");
                } else {
                    tracing::debug!(error = %error, "Heartbeat POST task remains degraded");
                }
                publish_heartbeat_transport_status(
                    &self.heartbeat_transport,
                    &mut self.last_status_projection,
                    serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.host_link,
                    &self.status_path,
                );
            }
            None => {}
        }
    }

    pub(super) fn on_machine_presence_post_done(
        &mut self,
        machine_presence_post_result: Option<
            Result<MachinePresencePostResult, tokio::task::JoinError>,
        >,
    ) {
        match machine_presence_post_result {
            Some(Ok(result)) => match result.result {
                Ok(true) => {
                    tracing::debug!(
                        task_elapsed_ms = result.task_elapsed_ms,
                        "Machine presence POST sent"
                    );
                }
                Ok(false) => {
                    tracing::debug!(
                        task_elapsed_ms = result.task_elapsed_ms,
                        "Machine presence collection disabled"
                    );
                }
                Err(err) => {
                    tracing::debug!("Machine presence POST failed: {}", err);
                }
            },
            Some(Err(err)) => {
                tracing::warn!("Machine presence POST task failed: {}", err);
            }
            None => {}
        }
    }

    pub(super) fn on_host_link_poll_done(
        &mut self,
        host_link_poll_result: Option<Result<Result<()>, tokio::task::JoinError>>,
    ) {
        match host_link_poll_result {
            Some(Ok(Ok(()))) | None => {}
            Some(Ok(Err(error))) => {
                tracing::debug!(%error, "Runtime Host admission poll failed");
            }
            Some(Err(error)) => {
                tracing::debug!(%error, "Runtime Host admission poll task failed");
            }
        }
    }

    pub(super) fn on_host_link_changed(
        &mut self,
        host_link_event: Result<(), watch::error::RecvError>,
    ) {
        if host_link_event.is_ok() {
            let serving_generation = self.host_link.serving_generation();
            if serving_generation > self.last_serving_generation {
                self.last_serving_generation = serving_generation;
                let now = Instant::now();
                for retry in self.deferred_retries.values_mut() {
                    retry.due_at = now;
                }
                self.runtime_outbox_consecutive_failures = 0;
                self.runtime_outbox_retry_after = None;
                self.adaptive_limiter.reset_backpressure_cooldown();
                self.outbox_timer.reset_immediately();
                self.failed_ship_retry_timer.reset_immediately();
                self.heartbeat_timer.reset_immediately();
                self.pending_truth_heartbeat = None;
            }
        }
    }

    pub(super) async fn on_health_tick(&mut self) {
        match self.client.health_check().await {
            Ok(true) => {
                if let Some(duration) = self.offline.mark_online() {
                    self.shipping_progress.reset_after_sleep(Instant::now());
                    self.last_runtime_truth_signature = None;
                    tracing::info!(
                        "Back online after {:.0}s — resuming shipping",
                        duration.as_secs_f64()
                    );
                }
            }
            _ => {
                tracing::debug!("Still offline (health check failed)");
            }
        }
    }

    pub(super) fn on_machine_presence_tick(&mut self) {
        if !self.offline.is_offline {
            if self.machine_presence_post_tasks.is_empty() {
                spawn_machine_presence_post(
                    &mut self.machine_presence_post_tasks,
                    self.client.clone(),
                );
            } else {
                tracing::debug!(
                    "Skipping machine presence POST while previous POST is still in flight"
                );
            }
        }
    }

    pub(super) fn on_truth_heartbeat_due(&mut self) {
        if let Some(pending) = self.pending_truth_heartbeat.take() {
            if !self.offline.is_offline
                && runtime_truth_changed(
                    self.last_runtime_truth_signature.as_deref(),
                    &pending.signature,
                )
            {
                let now = Instant::now();
                self.heartbeat_transport
                    .record_attempt(chrono::Utc::now().to_rfc3339());
                publish_heartbeat_transport_status(
                    &self.heartbeat_transport,
                    &mut self.last_status_projection,
                    serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                    &self.managed_reconciliation,
                    &mut self.shipping_progress,
                    self.offline.is_offline,
                    &self.host_link,
                    &self.status_path,
                );
                spawn_heartbeat_post(
                    &mut self.heartbeat_post_tasks,
                    self.client.clone(),
                    pending.payload,
                    pending.signature.clone(),
                    "runtime_truth_change",
                    &mut self.acknowledged_machine_evidence,
                );
                self.last_truth_heartbeat_at = Some(now);
                self.last_runtime_truth_signature = Some(pending.signature);
            }
        }
    }

    pub(super) fn on_heartbeat_tick(&mut self) {
        if let Some(projection) = self.last_status_projection.as_mut() {
            projection.set_heartbeat_transport(self.heartbeat_transport.clone());
            projection.set_host_link(self.host_link.snapshot());
            heartbeat::write_status_file(
                projection,
                serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                &self.managed_reconciliation,
                &mut self.shipping_progress,
                self.offline.is_offline,
                &self.status_path,
            );
            if !self.offline.is_offline {
                if self.heartbeat_post_tasks.is_empty() {
                    self.heartbeat_transport
                        .record_attempt(chrono::Utc::now().to_rfc3339());
                    projection.set_heartbeat_transport(self.heartbeat_transport.clone());
                    projection.set_host_link(self.host_link.snapshot());
                    heartbeat::write_status_file(
                        projection,
                        serde_json::to_value(self.control_channel_status.snapshot()).ok(),
                        &self.managed_reconciliation,
                        &mut self.shipping_progress,
                        self.offline.is_offline,
                        &self.status_path,
                    );
                    let payload = projection.payload.clone();
                    let signature = runtime_truth_signature(&payload);
                    self.last_runtime_truth_signature = Some(signature.clone());
                    self.pending_truth_heartbeat = None;
                    spawn_heartbeat_post(
                        &mut self.heartbeat_post_tasks,
                        self.client.clone(),
                        payload,
                        signature,
                        "periodic_heartbeat",
                        &mut self.acknowledged_machine_evidence,
                    );
                } else {
                    tracing::debug!(
                        "Skipping periodic heartbeat while a heartbeat POST is still in flight"
                    );
                }
            }
        } else {
            tracing::debug!(
                "Skipping periodic heartbeat until the startup managed observation scan completes"
            );
        }
    }
}
