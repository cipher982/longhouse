//! The control WebSocket: connect, reconnect with backoff, heartbeat and
//! readiness frames, and the per-connection receive loop.

use super::*;

pub fn spawn_control_channel(
    config: ShipperConfig,
    status: ControlChannelStatus,
    host_link: crate::host_link::HostLink,
) -> Option<JoinHandle<()>> {
    if config.api_token.as_deref().unwrap_or("").trim().is_empty() {
        status.set_disabled();
        tracing::debug!("Machine control channel disabled because no device token is configured");
        return None;
    }
    status.set_disconnected(None, None, None, None);

    Some(tokio::spawn(async move {
        match crate::codex_exec::recover_codex_exec_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered Codex Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => tracing::warn!(%error, "Failed to reconcile Codex Console turn claims"),
        }
        match crate::cursor_print::recover_cursor_print_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered Cursor Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => tracing::warn!(%error, "Failed to reconcile Cursor Console turn claims"),
        }
        match crate::opencode_run::recover_opencode_run_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered OpenCode Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => {
                tracing::warn!(%error, "Failed to reconcile OpenCode Console turn claims")
            }
        }
        match crate::claude_print::recover_claude_print_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered Claude Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => tracing::warn!(%error, "Failed to reconcile Claude Console turn claims"),
        }
        match crate::pi_print::recover_pi_print_turns(&config.machine_name, config.db_path.clone())
            .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered Pi Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => tracing::warn!(%error, "Failed to reconcile Pi Console turn claims"),
        }
        match crate::omp_print::recover_omp_print_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered OMP Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => tracing::warn!(%error, "Failed to reconcile OMP Console turn claims"),
        }
        match crate::antigravity_print::recover_antigravity_print_turns(
            &config.machine_name,
            config.db_path.clone(),
        )
        .await
        {
            Ok(count) if count > 0 => {
                tracing::info!(count, "Recovered Antigravity Console turn monitors")
            }
            Ok(_) => {}
            Err(error) => {
                tracing::warn!(%error, "Failed to reconcile Antigravity Console turn claims")
            }
        }
        run_reconnect_loop(config, status, host_link).await;
    }))
}

pub(super) async fn run_reconnect_loop(
    config: ShipperConfig,
    status: ControlChannelStatus,
    host_link: crate::host_link::HostLink,
) {
    let mut backoff = Duration::from_secs(1);
    let mut last_error: Option<String> = None;
    let mut outage_started: Option<Instant> = None;
    let Some(receipt_db_path) = config
        .db_path
        .clone()
        .or_else(|| crate::config::get_agent_db_path().ok())
    else {
        tracing::error!("Managed control cannot start without a durable command receipt path");
        return;
    };
    let receipt_store = Some(Arc::new(DurableCommandReceiptStore::for_db_path(
        &receipt_db_path,
    )));
    let mut completed_commands = CompletedCommandCache::new(
        COMPLETED_COMMAND_CACHE_CAPACITY,
        Duration::from_secs(COMPLETED_COMMAND_CACHE_TTL_SECS),
    )
    .with_durable_receipts(receipt_store);
    let mut host_link_changed = host_link.subscribe();
    loop {
        let _ = host_link_changed.borrow_and_update().clone();
        let serving_generation_before = host_link.serving_generation();
        let runtime_epoch_before = host_link.snapshot().runtime_epoch;
        let connected_before = status.snapshot().last_connected_at;
        let result = run_once(&config, &mut completed_commands, &status, &host_link).await;
        let connected_during_attempt = status.snapshot().last_connected_at != connected_before;
        let runtime_epoch_changed = host_link.snapshot().runtime_epoch != runtime_epoch_before;
        if runtime_epoch_changed {
            backoff = Duration::from_secs(1);
            outage_started = None;
        }
        let serving_evidence = host_link.serving_generation() > serving_generation_before;
        let reconnect_delay = if serving_evidence {
            outage_started = None;
            Duration::ZERO
        } else if host_link.is_updating() {
            Duration::from_millis(
                CONTROL_UPDATE_RETRY_MILLIS.saturating_sub(100) + rand::rng().random_range(0..=200),
            )
        } else if connected_during_attempt {
            outage_started = Some(Instant::now());
            backoff = Duration::from_secs(1);
            backoff
        } else {
            outage_started.get_or_insert_with(Instant::now);
            backoff
        };

        match result {
            Ok(()) => {
                tracing::info!("Machine control channel disconnected");
                status.set_disconnected(None, None, None, Some(reconnect_delay.as_secs()));
                last_error = None;
            }
            Err(err) => {
                let error_chain = format_error_chain(&err);
                status.set_disconnected(
                    None,
                    Some("connect_failed"),
                    Some(error_chain.as_str()),
                    Some(reconnect_delay.as_secs()),
                );
                if host_link.is_updating() || last_error.as_deref() == Some(error_chain.as_str()) {
                    tracing::debug!(error = %error_chain, "Machine control channel connection failed");
                } else {
                    tracing::warn!(error = %error_chain, "Machine control channel connection failed");
                    last_error = Some(error_chain);
                }
            }
        }

        tokio::select! {
            _ = tokio::time::sleep(reconnect_delay) => {}
            _ = async {
                loop {
                    if host_link.serving_generation() > serving_generation_before {
                        break;
                    }
                    if host_link_changed.changed().await.is_err() {
                        break;
                    }
                }
            } => {
                backoff = Duration::from_secs(1);
                outage_started = None;
                continue;
            }
        }
        let outage_elapsed = outage_started
            .map(|started| started.elapsed())
            .unwrap_or(Duration::ZERO);
        backoff = if serving_evidence {
            Duration::from_secs(1)
        } else {
            next_reconnect_backoff(reconnect_delay, outage_elapsed)
        };
    }
}

pub(super) fn next_reconnect_backoff(current: Duration, outage_elapsed: Duration) -> Duration {
    let max_backoff = if outage_elapsed < Duration::from_secs(CONTROL_RECONNECT_SHORT_WINDOW_SECS) {
        Duration::from_secs(CONTROL_RECONNECT_SHORT_MAX_BACKOFF_SECS)
    } else {
        Duration::from_secs(CONTROL_RECONNECT_SUSTAINED_MAX_BACKOFF_SECS)
    };
    (current * 2).min(max_backoff)
}

pub(super) fn format_error_chain(err: &anyhow::Error) -> String {
    err.chain()
        .map(ToString::to_string)
        .collect::<Vec<_>>()
        .join(": ")
}

pub(super) async fn run_once(
    config: &ShipperConfig,
    completed_commands: &mut CompletedCommandCache,
    status: &ControlChannelStatus,
    host_link: &crate::host_link::HostLink,
) -> Result<()> {
    let ws_url = control_ws_url(
        &config.api_url,
        crate::plaintext_http::opt_in_enabled(&crate::config::get_machine_dir()?, &config.api_url),
    )?;
    status.set_disconnected(Some(&ws_url), None, None, None);
    let mut request = ws_url
        .as_str()
        .into_client_request()
        .context("building control websocket request")?;
    if let Some(token) = config.api_token.as_deref() {
        request.headers_mut().insert(
            "X-Agents-Token",
            HeaderValue::from_str(token).context("invalid X-Agents-Token header")?,
        );
    }

    let (mut stream, _) = tokio::time::timeout(
        Duration::from_secs(if host_link.is_updating() {
            CONTROL_UPDATE_CONNECT_TIMEOUT_SECS
        } else {
            CONTROL_CONNECT_TIMEOUT_SECS
        }),
        connect_async(request),
    )
    .await
    .map_err(|_| anyhow!("timed out connecting machine control websocket {ws_url}"))?
    .with_context(|| format!("connecting machine control websocket {ws_url}"))?;
    let mut last_capabilities = capabilities_snapshot().await;
    let hello = json!({
        "type": "hello",
        "schema_version": 1,
        "device_id": config.machine_name,
        "machine_name": config.machine_name,
        "engine_build": build_identity::COMMIT_SHORT,
        // supports[] says what this engine can drive. Readiness says whether
        // the machine can actually do it right now, and why not when it
        // cannot -- the reason the browser previously never received.
        "supports": last_capabilities["supports"].clone(),
        "provider_readiness": last_capabilities["provider_readiness"].clone(),
    });
    send_control_message(
        &mut stream,
        Message::Text(hello.to_string().into()),
        "machine control hello",
        status,
    )
    .await?;
    status.set_connected(&ws_url);
    tracing::info!("Machine control channel connected to {ws_url}");

    let heartbeat_interval = Duration::from_secs(HEARTBEAT_INTERVAL_SECS);
    let mut heartbeat = tokio::time::interval(Duration::from_secs(HEARTBEAT_INTERVAL_SECS));
    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Delay);
    heartbeat.tick().await;
    let mut next_heartbeat_due = Instant::now() + heartbeat_interval;
    let (command_result_tx, mut command_result_rx) = mpsc::unbounded_channel::<(String, Value)>();
    let mut in_flight_commands: HashSet<String> = HashSet::new();
    // Hello is a snapshot; without a refresh, signing in to a provider or
    // installing its CLI after connect stayed invisible until a reconnect.
    let mut readiness_refresh = tokio::time::interval(Duration::from_secs(READINESS_REFRESH_SECS));
    readiness_refresh.set_missed_tick_behavior(MissedTickBehavior::Delay);
    readiness_refresh.tick().await;
    let (capabilities_tx, mut capabilities_rx) = mpsc::unbounded_channel::<Value>();
    let mut capabilities_probe_in_flight = false;
    let mut host_link_changed = host_link.subscribe();
    let mut last_serving_generation = host_link.serving_generation();

    loop {
        tokio::select! {
            _ = async {
                tokio::select! {
                    _ = readiness_refresh.tick() => {}
                    _ = crate::sign_in::readiness_refresh_now().notified() => {}
                }
            }, if !capabilities_probe_in_flight => {
                capabilities_probe_in_flight = true;
                let tx = capabilities_tx.clone();
                tokio::spawn(async move {
                    let _ = tx.send(capabilities_snapshot().await);
                });
            }
            Some(capabilities) = capabilities_rx.recv() => {
                capabilities_probe_in_flight = false;
                if let Some(frame) = readiness_update_frame(&last_capabilities, &capabilities) {
                    send_control_message(
                        &mut stream,
                        Message::Text(frame.to_string().into()),
                        "machine control readiness update",
                        status,
                    )
                    .await?;
                    last_capabilities = capabilities;
                }
            }
            Some((command_id, result)) = command_result_rx.recv() => {
                in_flight_commands.remove(&command_id);
                completed_commands.insert(command_id, result.clone());
                send_control_message(
                    &mut stream,
                    Message::Text(result.to_string().into()),
                    "machine control command result",
                    status,
                )
                .await?;
            }
            _ = heartbeat.tick() => {
                let now = Instant::now();
                let lateness = now.saturating_duration_since(next_heartbeat_due);
                status.record_heartbeat_lateness(lateness);
                if lateness.as_millis() > CONTROL_HEARTBEAT_LATE_WARN_MS {
                    tracing::warn!(
                        lateness_ms = duration_millis_u64(lateness),
                        heartbeat_interval_secs = HEARTBEAT_INTERVAL_SECS,
                        "Machine control heartbeat delayed; executor stall suspected"
                    );
                }
                next_heartbeat_due = now + heartbeat_interval;
                send_control_message(
                    &mut stream,
                    Message::Text(heartbeat_frame().to_string().into()),
                    "machine control heartbeat",
                    status,
                )
                .await?;
            }
            changed = host_link_changed.changed() => {
                if changed.is_err() {
                    break;
                }
                let serving_generation = host_link.serving_generation();
                if serving_generation > last_serving_generation {
                    break;
                }
            }
            message = stream.next() => {
                let Some(message) = message else {
                    break;
                };
                let message = message.context("reading machine control websocket message")?;
                let text = match message {
                    Message::Text(text) => text,
                    Message::Close(frame) => {
                        if frame.as_ref().is_some_and(|frame| frame.code == CloseCode::Restart)
                            && !host_link.has_valid_claim()
                        {
                            host_link.observe_default_restart_claim();
                        }
                        tracing::info!(?frame, "Machine control channel received close frame");
                        break;
                    }
                    Message::Ping(payload) => {
                        send_control_message(
                            &mut stream,
                            Message::Pong(payload),
                            "machine control pong",
                            status,
                        )
                        .await?;
                        continue;
                    }
                    _ => {
                        continue;
                    }
                };
                let frame: Value = serde_json::from_str(&text).context("parsing machine control frame")?;
                match frame.get("type").and_then(Value::as_str) {
                    Some("host.lifecycle") => {
                        host_link.observe_lifecycle_value(&frame);
                        continue;
                    }
                    Some("command") => {}
                    _ => {
                        tracing::debug!(
                            "Ignoring machine control frame type={:?}",
                            frame.get("type")
                        );
                        continue;
                    }
                }
                let command_id = frame
                    .get("command_id")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string();
                if command_id.is_empty() {
                    let result = command_error("", "invalid_command", "command_id is required");
                    send_control_message(
                        &mut stream,
                        Message::Text(result.to_string().into()),
                        "machine control command result",
                        status,
                    )
                    .await?;
                    continue;
                }
                if !command_requires_restart_fence(&frame) {
                    if let Some(result) = completed_commands.get(&command_id) {
                        send_control_message(
                            &mut stream,
                            Message::Text(result.to_string().into()),
                            "machine control command result",
                            status,
                        )
                        .await?;
                        continue;
                    }
                }
                if in_flight_commands.contains(&command_id) {
                    tracing::debug!(command_id, "Ignoring duplicate in-flight machine control command");
                    continue;
                }

                in_flight_commands.insert(command_id.clone());
                let tx = command_result_tx.clone();
                let config = config.clone();
                let durable_receipts = completed_commands.durable_receipts.clone();
                tokio::spawn(async move {
                    let mut task_cache = CompletedCommandCache::new(0, Duration::ZERO)
                        .with_durable_receipts(durable_receipts);
                    let result = handle_command_frame(frame, &mut task_cache, &config).await;
                    let _ = tx.send((command_id, result));
                });
            }
        }
    }

    Ok(())
}

pub(super) async fn send_control_message<S>(
    stream: &mut S,
    message: Message,
    context: &'static str,
    status: &ControlChannelStatus,
) -> Result<()>
where
    S: Sink<Message, Error = tokio_tungstenite::tungstenite::Error> + Unpin,
{
    let started = Instant::now();
    tokio::time::timeout(
        Duration::from_secs(CONTROL_WRITE_TIMEOUT_SECS),
        stream.send(message),
    )
    .await
    .map_err(|_| anyhow!("timed out sending {context}"))?
    .with_context(|| format!("sending {context}"))?;
    let elapsed = started.elapsed();
    status.record_write_elapsed(elapsed);
    if elapsed > Duration::from_secs(1) {
        tracing::warn!(
            context,
            elapsed_ms = duration_millis_u64(elapsed),
            "Machine control websocket send was slow"
        );
    }
    Ok(())
}

pub(super) fn heartbeat_frame() -> Value {
    json!({"type": "heartbeat"})
}

pub(super) async fn capabilities_snapshot() -> Value {
    json!({
        "supports": control_supports(),
        "provider_readiness": provider_readiness_snapshot().await,
    })
}

/// The frame to send when supports or readiness changed since the last one
/// the Runtime Host saw; `None` when nothing changed.
pub(super) fn readiness_update_frame(previous: &Value, current: &Value) -> Option<Value> {
    if previous == current {
        return None;
    }
    Some(json!({
        "type": "readiness_update",
        "supports": current["supports"].clone(),
        "provider_readiness": current["provider_readiness"].clone(),
    }))
}
