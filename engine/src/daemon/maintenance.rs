//! Daemon loop handlers for periodic upkeep: pruning, update checks, the disk
//! guard, flight samples and OpenCode title refresh.

use super::*;

impl DaemonState {
    pub(super) fn on_opencode_title_refresh_done(
        &mut self,
        opencode_title_refresh_result: Option<Result<Result<()>, tokio::task::JoinError>>,
    ) {
        match opencode_title_refresh_result {
            Some(Ok(Ok(()))) | None => {}
            Some(Ok(Err(err))) => tracing::warn!(error = %err, "OpenCode title refresh failed"),
            Some(Err(err)) => tracing::warn!(error = %err, "OpenCode title refresh task failed"),
        }
    }

    pub(super) fn on_flight_sample_tick(&mut self, config: &ConnectConfig) {
        if let Some(recorder) = self.flight_recorder.as_ref() {
            record_flight_sample(
                recorder,
                &self.outbox_dir,
                &self.runtime_events_outbox_dir,
                &self.control_channel_status,
                &self.ship_stats,
                &config.shipper_config.machine_name,
                self.in_flight.len(),
                self.scheduler.has_pending_work(),
                self.deferred_retries.len(),
                self.offline.is_offline,
            );
        }
    }

    pub(super) async fn on_disk_guard_tick(&mut self, config: &ConnectConfig) {
        if let Some(guard) = self.disk_guard.as_mut() {
            let outcome = guard.tick(&disk_guard_sessions(&self.last_managed_observations));
            if let Some((title, body)) = outcome.notify.as_ref() {
                crate::disk_guard::notify_desktop(title, body);
            }
            for steer in outcome.steers {
                let shipper_config = config.shipper_config.clone();
                tokio::spawn(async move {
                    if let Err(error) = crate::control_channel::steer_local_session(
                        &shipper_config,
                        &steer.provider,
                        &steer.session_id,
                        &steer.text,
                    )
                    .await
                    {
                        tracing::info!(
                            session_id = %steer.session_id,
                            provider = %steer.provider,
                            %error,
                            "Disk guard could not steer session"
                        );
                    }
                });
            }
        }
    }

    pub(super) async fn on_update_check_tick(&mut self) {
        // Spawned, not awaited. A download runs for as long as the
        // transfer takes, and awaiting it here would stop shipping,
        // heartbeat, and control work for that whole window.
        //
        // Runtime Host reachability is deliberately not consulted:
        // `offline` describes the Longhouse transport, which says
        // nothing about whether GitHub is reachable, and gating on it
        // would hide a stale machine whose own Runtime Host is down.
        let client = self.update_http_client.clone();
        tokio::task::spawn(async move {
            crate::update::run_check_tick(&client).await;
        });
    }

    pub(super) fn on_prune_tick(&mut self) {
        let fs = FileState::new(&self.conn);
        match fs.prune_stale(30) {
            Ok(n) if n > 0 => tracing::info!("Daily prune: removed {} stale file_state entries", n),
            Ok(_) => {}
            Err(e) => tracing::warn!("Daily prune error: {}", e),
        }
        let sb = crate::state::session_binding::SessionBinding::new(&self.conn);
        match sb.prune_stale(30) {
            Ok(n) if n > 0 => {
                tracing::info!("Daily prune: removed {} stale session_binding entries", n)
            }
            Ok(_) => {}
            Err(e) => tracing::warn!("Session binding prune error: {}", e),
        }
        if self.daily_maintenance_tasks.is_empty() {
            let db_path = self.projection_db_path.clone();
            self.daily_maintenance_tasks.spawn_blocking(move || {
                crate::state::recover::run_daily_storage_maintenance(&db_path);
            });
        }
    }
}
