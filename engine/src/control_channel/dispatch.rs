//! Command frames: dedupe and receipts around `execute_command`, plus the
//! invocation-close, archive backlog and provider sign-in commands.

use super::*;

pub(super) async fn handle_command_frame(
    frame: Value,
    completed_commands: &mut CompletedCommandCache,
    config: &ShipperConfig,
) -> Value {
    let command_id = frame
        .get("command_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if command_id.is_empty() {
        return command_error("", "invalid_command", "command_id is required");
    }

    let restart_fenced = command_requires_restart_fence(&frame);
    let receipt_identity = restart_fenced.then(|| command_receipt_identity(&frame, &command_id));
    if let (Some(store), Some(identity)) = (&completed_commands.durable_receipts, &receipt_identity)
    {
        match store.claim(identity) {
            Ok(DurableCommandReceiptOutcome::Terminal(result)) => return result,
            Ok(DurableCommandReceiptOutcome::Accepted) => {
                return command_error(
                    &command_id,
                    "command_indeterminate",
                    "Machine Agent accepted this command before its outcome was recorded; it was not replayed",
                )
            }
            Ok(DurableCommandReceiptOutcome::IdentityConflict) => {
                return command_error(
                    &command_id,
                    "command_identity_conflict",
                    "command_id was reused with a different provider, session, or lease generation",
                )
            }
            Ok(DurableCommandReceiptOutcome::Claimed) => {}
            Err(error) => {
                tracing::error!(command_id = %command_id, error = %error, "Cannot fence managed control command");
                return command_error(
                    &command_id,
                    "command_receipt_unavailable",
                    "Machine Agent could not durably record the managed control command before execution",
                );
            }
        }
    }

    if let Some(result) = completed_commands.get(&command_id) {
        return result;
    }

    let result = execute_command(&frame, config).await;
    let response = match result {
        Ok(result) => json!({
            "type": "command_result",
            "command_id": &command_id,
            "ok": true,
            "result": result,
        }),
        Err(CommandError { code, message }) => {
            tracing::warn!(
                command_id = %command_id,
                error_code = %code,
                error_message = %message,
                "Machine control command failed"
            );
            command_error(&command_id, &code, &message)
        }
    };
    if let (Some(store), Some(identity)) = (&completed_commands.durable_receipts, &receipt_identity)
    {
        match store.complete(identity, &response) {
            Ok(Some(recorded)) => return recorded,
            Ok(None) => {}
            Err(error) => {
                tracing::error!(command_id = %command_id, error = %error, "Cannot record managed control result");
                return command_error(
                    &command_id,
                    "command_indeterminate",
                    "Managed control ran, but its result could not be durably recorded; it was not replayed",
                );
            }
        }
    }
    completed_commands.insert(command_id, response.clone());
    response
}

/// Steer text into a live session's current turn from inside the engine, over
/// the same per-provider dispatch a server command uses. The disk guard uses
/// this to reach sessions that are writing without a server round trip.
pub(crate) async fn steer_local_session(
    config: &ShipperConfig,
    provider: &str,
    session_id: &str,
    text: &str,
) -> std::result::Result<(), String> {
    let frame = json!({
        "command_type": COMMAND_STEER_TEXT,
        "session_id": session_id,
        "payload": {"provider": provider, "text": text},
    });
    execute_command(&frame, config)
        .await
        .map(|_| ())
        .map_err(|error| format!("{}: {}", error.code, error.message))
}

pub(super) async fn execute_command(
    frame: &Value,
    config: &ShipperConfig,
) -> std::result::Result<Value, CommandError> {
    let command_type = required_string(frame, "command_type")?;
    let payload = frame.get("payload").cloned().unwrap_or_else(|| json!({}));

    if command_type == COMMAND_PROVIDER_SIGN_IN_START
        || command_type == COMMAND_PROVIDER_SIGN_IN_CODE
        || command_type == COMMAND_PROVIDER_SIGN_IN_CANCEL
    {
        return run_provider_sign_in_command(&command_type, &payload).await;
    }
    if command_type == COMMAND_ARCHIVE_BACKLOG_CONTROL
        || command_type == COMMAND_ARCHIVE_BACKLOG_CONTROL_V2
    {
        return run_archive_backlog_control_command(&payload).await;
    }

    let session_id = required_string(frame, "session_id")?;
    let durable_command_id = frame.get("command_id").and_then(Value::as_str);

    match command_type.as_str() {
        COMMAND_INVOCATION_CLOSE => {
            execute_invocation_close(frame, &payload, &session_id, config).await
        }
        COMMAND_TURN_START => execute_turn_start(frame, &payload, &session_id, config).await,
        COMMAND_TURN_STEER => {
            let run_id = payload_required_string(&payload, "run_id")?;
            let provider = payload_required_string(&payload, "provider")?;
            let text = payload_required_string(&payload, "text")?;
            if crate::qa_fault::console_steer_noop() {
                crate::qa_fault::record_fired_named(
                    "console_steer_noop",
                    &session_id,
                    json!({"run_id": run_id, "provider": provider}),
                );
                return Ok(json!({
                    "provider": provider,
                    "transport": "qa_fault_noop",
                    "run_id": run_id,
                    "steered": true,
                }));
            }
            let (transport, outcome) = match provider.as_str() {
                "codex" => {
                    crate::codex_exec::steer_codex_console_turn(&run_id, &text)
                        .await
                        .map_err(|reason| CommandError {
                            code: reason.clone(),
                            message: format!(
                                "Codex Console turn {run_id} did not take the steer: {reason}"
                            ),
                        })?;
                    (CODEX_EXEC_ADAPTER, ConsoleSteerOutcome::Steered)
                }
                "claude" => {
                    crate::claude_print::steer_claude_print_turn(&run_id, &session_id, &text)
                        .map_err(|reason| CommandError {
                            code: if reason == "turn_not_steerable" {
                                reason.clone()
                            } else {
                                "steer_failed".to_string()
                            },
                            message: format!(
                                "Claude Console turn {run_id} did not take the steer: {reason}"
                            ),
                        })?;
                    (CLAUDE_PRINT_ADAPTER, ConsoleSteerOutcome::Steered)
                }
                "pi" => {
                    crate::pi_print::steer_pi_print_turn(&run_id, &session_id, &text)
                        .await
                        .map_err(|reason| CommandError {
                            code: reason.clone(),
                            message: format!(
                                "Pi Console turn {run_id} did not take the steer: {reason}"
                            ),
                        })?;
                    (PI_PRINT_ADAPTER, ConsoleSteerOutcome::Steered)
                }
                "omp" => {
                    crate::omp_print::steer_omp_print_turn(&run_id, &session_id, &text)
                        .await
                        .map_err(|reason| CommandError {
                            code: reason.clone(),
                            message: format!(
                                "OMP Console turn {run_id} did not take the steer: {reason}"
                            ),
                        })?;
                    (OMP_PRINT_ADAPTER, ConsoleSteerOutcome::Steered)
                }
                "opencode" => {
                    let outcome = crate::opencode_run::steer_opencode_console_turn(
                        &run_id,
                        &session_id,
                        &text,
                    )
                    .await
                    .map_err(|reason| CommandError {
                        code: reason.clone(),
                        message: format!(
                            "OpenCode Console turn {run_id} did not take the steer: {reason}"
                        ),
                    })?;
                    (OPENCODE_RUN_ADAPTER, outcome)
                }
                _ => {
                    return Err(CommandError {
                        code: "provider_unsupported".to_string(),
                        message: format!("provider={provider} has no Console steer adapter"),
                    });
                }
            };
            // `steered` is false when the text started a new turn instead of
            // entering the running one; `outcome` says which.
            Ok(json!({
                "provider": provider,
                "transport": transport,
                "run_id": run_id,
                "steered": outcome == ConsoleSteerOutcome::Steered,
                "outcome": outcome.as_str(),
            }))
        }
        COMMAND_TURN_INTERRUPT => {
            let run_id = payload_required_string(&payload, "run_id")?;
            let provider = payload_required_string(&payload, "provider")?;
            let turn_id = payload_required_string(&payload, "turn_id")?;
            let thread_id = payload_required_string(&payload, "thread_id")?;
            let transport = match provider.as_str() {
                "codex" => {
                    crate::codex_exec::interrupt_codex_console_turn(&run_id)
                        .await
                        .map_err(|reason| CommandError {
                            code: reason.clone(),
                            message: format!(
                                "Codex Console turn {run_id} was not interrupted: {reason}"
                            ),
                        })?;
                    CODEX_EXEC_ADAPTER
                }
                "cursor" => {
                    crate::cursor_print::interrupt_cursor_print_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .map_err(CommandError::command_failed)?;
                    CURSOR_PRINT_ADAPTER
                }
                "opencode" => {
                    crate::opencode_run::interrupt_opencode_run_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .await
                    .map_err(CommandError::command_failed)?;
                    OPENCODE_RUN_ADAPTER
                }
                "claude" => {
                    crate::claude_print::interrupt_claude_print_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .map_err(CommandError::command_failed)?;
                    CLAUDE_PRINT_ADAPTER
                }
                "pi" => {
                    crate::pi_print::interrupt_pi_print_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .await
                    .map_err(CommandError::command_failed)?;
                    PI_PRINT_ADAPTER
                }
                "omp" => {
                    crate::omp_print::interrupt_omp_print_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .await
                    .map_err(CommandError::command_failed)?;
                    OMP_PRINT_ADAPTER
                }
                "antigravity" => {
                    crate::antigravity_print::interrupt_antigravity_print_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .map_err(CommandError::command_failed)?;
                    ANTIGRAVITY_PRINT_ADAPTER
                }
                _ => {
                    return Err(CommandError {
                        code: "provider_unsupported".to_string(),
                        message: format!(
                            "provider={provider} has no turn-scoped interrupt adapter"
                        ),
                    });
                }
            };
            Ok(json!({
                "provider": provider,
                "transport": transport,
                "run_id": run_id,
                "interrupt_requested": true,
            }))
        }
        COMMAND_RUN_ONCE => {
            let provider = payload_required_string(&payload, "provider")?;
            if provider != "codex" {
                return Err(CommandError {
                    code: "provider_unsupported".to_string(),
                    message: format!("provider={provider} is not supported for session.run_once"),
                });
            }
            let cwd_raw = payload_required_string(&payload, "cwd")?;
            let cwd = PathBuf::from(&cwd_raw);
            if !cwd.is_absolute() {
                return Err(CommandError {
                    code: "cwd_not_allowed".to_string(),
                    message: "cwd must be absolute".to_string(),
                });
            }
            if !cwd.is_dir() {
                return Err(CommandError {
                    code: "cwd_not_found".to_string(),
                    message: format!("cwd does not exist: {}", cwd.display()),
                });
            }
            let initial_prompt = payload_required_string(&payload, "initial_prompt")?;
            let run_id = payload_required_string(&payload, "run_id")?;
            let resume_target = payload_resume_target(&payload)?;
            let launch_actor = payload_optional_string(&payload, "launch_actor");
            let launch_surface = payload_optional_string(&payload, "launch_surface");
            let provider_prompt = initial_prompt;
            let local_db_path = config
                .db_path
                .clone()
                .or_else(|| crate::config::get_agent_db_path().ok());

            let api_token = config.api_token.clone().ok_or_else(|| CommandError {
                code: "provider_launch_failed".to_string(),
                message: "Machine Agent has no device token configured".to_string(),
            })?;

            let summary = start_codex_exec_once(CodexExecRunConfig {
                session_id: session_id.clone(),
                run_id: run_id.clone(),
                thread_id: None,
                turn_id: None,
                client_request_id: None,
                origin: "user".to_string(),
                wake_id: None,
                invocation_id: None,
                cwd,
                api_url: config.api_url.clone(),
                api_token,
                codex_bin: DEFAULT_CODEX_BIN.to_string(),
                approval_policy: Some(REMOTE_CODEX_EXEC_APPROVAL_POLICY.to_string()),
                sandbox: Some(REMOTE_CODEX_EXEC_SANDBOX.to_string()),
                model: payload_optional_string(&payload, "model"),
                prompt: provider_prompt,
                image_paths: Vec::new(),
                launch_actor,
                launch_surface,
                resume_thread_id: resume_target
                    .as_ref()
                    .map(|target| target.thread_id.clone()),
                // run_once has no branch path; only served Console turns fork.
                fork_thread_id: None,
                machine_name: config.machine_name.clone(),
                local_db_path,
                transcript_wake_socket: None,
            })
            .await
            .map_err(|err| CommandError {
                code: "provider_launch_failed".to_string(),
                message: err.to_string(),
            })?;

            Ok(json!({
                "session_id": summary.session_id,
                "run_id": summary.run_id,
                "provider": "codex",
                "transport": "codex_app_server",
                "pid": summary.pid,
                "process_group_id": summary.process_group_id,
                "argv": summary.argv,
            }))
        }
        COMMAND_SEND_TEXT => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| DEFAULT_COMMAND_PROVIDER.to_string());
            let attachments = crate::codex_attachments::parse_attachments(&payload)
                .map_err(CommandError::command_failed)?;
            let text =
                crate::input_attachments::text_or_attachments(&payload, "text", &attachments)
                    .map_err(|error| CommandError {
                        code: "invalid_command".to_string(),
                        message: error.to_string(),
                    })?;
            let staged = stage_helm_attachments(
                config,
                &session_id,
                &provider,
                durable_command_id,
                &attachments,
            )
            .await?;
            let path_text = crate::input_attachments::prompt_with_attachments(&text, &staged);
            if provider == "claude" {
                let summary = claude_channel_send_text(ClaudeChannelSendConfig {
                    session_id: session_id.clone(),
                    text: path_text,
                    meta: Vec::new(),
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok(claude_channel_control_result(summary.provider_session_id));
            }
            if provider == "opencode" {
                let summary = crate::opencode_control::send_text(&session_id, &text, &staged)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "antigravity" {
                // stage_helm_attachments already refused attachments here.
                let outcome = crate::antigravity_channel_control::send_text(&session_id, &text)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "antigravity",
                    "transport": crate::antigravity_channel_control::ANTIGRAVITY_HOOK_INBOX_TRANSPORT,
                    "message_id": outcome.message_id,
                    "claimed_at": outcome.claimed_at,
                }));
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::send_text(&session_id, &path_text, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
            }
            if provider == "pi" {
                let summary = crate::pi_helm_control::dispatch_with_attachments(
                    &session_id,
                    crate::pi_helm_control::CommandKind::Send,
                    Some(&text),
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                    &staged,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "pi",
                    "transport": crate::pi_helm_control::PI_HELM_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                    "status": summary.status,
                }));
            }
            if provider == "omp" {
                let summary = crate::omp_helm_control::dispatch_with_attachments(
                    &session_id,
                    crate::omp_helm_control::CommandKind::Send,
                    Some(&text),
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                    durable_command_id,
                    &staged,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(
                    json!({"exit_code":0,"stdout":"","stderr":"","provider":"omp","transport":crate::omp_helm_control::OMP_HELM_TRANSPORT,"provider_session_id":summary.native_session_id,"status":summary.status}),
                );
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            let summary = cmd_codex_bridge_send(BridgeSendConfig {
                session_id: session_id.clone(),
                text,
                state_root: None,
                allow_direct_ws_fallback: false,
                attachments,
            })
            .await
            .map_err(|err| CommandError::command_failed(err))?;
            Ok(json!({
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
                "thread_id": summary.thread_id,
                "turn_id": summary.turn_id,
                "turn_status": summary.turn_status,
            }))
        }
        COMMAND_INTERRUPT => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| DEFAULT_COMMAND_PROVIDER.to_string());
            if provider == "claude" {
                claude_channel_interrupt(ClaudeChannelInterruptConfig {
                    session_id,
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok(claude_channel_control_result(None));
            }
            if provider == "opencode" {
                let summary = crate::opencode_control::interrupt(&session_id)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "antigravity" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: "Antigravity hook inbox does not support remote interrupts"
                        .to_string(),
                });
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::interrupt(&session_id, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
            }
            if provider == "pi" {
                let summary = crate::pi_helm_control::dispatch(
                    &session_id,
                    crate::pi_helm_control::CommandKind::Abort,
                    None,
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "pi",
                    "transport": crate::pi_helm_control::PI_HELM_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "omp" {
                let summary = crate::omp_helm_control::dispatch(
                    &session_id,
                    crate::omp_helm_control::CommandKind::Abort,
                    None,
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                    durable_command_id,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(
                    json!({"exit_code":0,"stdout":"","stderr":"","provider":"omp","transport":crate::omp_helm_control::OMP_HELM_TRANSPORT,"provider_session_id":summary.native_session_id}),
                );
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            cmd_codex_bridge_interrupt(BridgeInterruptConfig {
                session_id,
                state_root: None,
            })
            .await
            .map_err(|err| CommandError::command_failed(err))?;
            Ok(json!({
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
            }))
        }
        COMMAND_TERMINATE => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| DEFAULT_COMMAND_PROVIDER.to_string());
            if provider == "claude" {
                let summary = claude_channel_terminate(ClaudeChannelInterruptConfig {
                    session_id,
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                let mut result = claude_channel_control_result(None);
                result["pid"] = json!(summary.pid);
                result["forced"] = json!(summary.forced);
                return Ok(result);
            }
            if provider == "opencode" {
                let summary = crate::opencode_control::stop_server_bridge(&session_id)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "pid": summary.pid,
                    "stopped": summary.stopped,
                }));
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::terminate(&session_id, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
            }
            if provider == "pi" {
                let summary = crate::pi_helm_control::dispatch(
                    &session_id,
                    crate::pi_helm_control::CommandKind::Terminate,
                    None,
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "pi",
                    "transport": crate::pi_helm_control::PI_HELM_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "omp" {
                let summary = crate::omp_helm_control::dispatch(
                    &session_id,
                    crate::omp_helm_control::CommandKind::Terminate,
                    None,
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                    durable_command_id,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(
                    json!({"exit_code":0,"stdout":"","stderr":"","provider":"omp","transport":crate::omp_helm_control::OMP_HELM_TRANSPORT,"provider_session_id":summary.native_session_id}),
                );
            }
            Err(CommandError {
                code: "unsupported_command".to_string(),
                message: format!("{provider} terminate is not supported by this Machine Agent"),
            })
        }
        COMMAND_STEER_TEXT => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| DEFAULT_COMMAND_PROVIDER.to_string());
            let attachments = crate::codex_attachments::parse_attachments(&payload)
                .map_err(CommandError::command_failed)?;
            let text =
                crate::input_attachments::text_or_attachments(&payload, "text", &attachments)
                    .map_err(|error| CommandError {
                        code: "invalid_command".to_string(),
                        message: error.to_string(),
                    })?;
            let staged = stage_helm_attachments(
                config,
                &session_id,
                &provider,
                durable_command_id,
                &attachments,
            )
            .await?;
            let path_text = crate::input_attachments::prompt_with_attachments(&text, &staged);
            if provider == "claude" {
                let summary = claude_channel_send_text(ClaudeChannelSendConfig {
                    session_id: session_id.clone(),
                    text: path_text,
                    meta: vec![("intent".to_string(), "steer".to_string())],
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok(claude_channel_control_result(summary.provider_session_id));
            }
            if provider == "opencode" {
                // Same delivery as send: OpenCode picks a prompt posted into a
                // running turn up at that turn's next step boundary.
                let summary = crate::opencode_control::steer_text(&session_id, &text, &staged)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "cursor" {
                let summary = crate::cursor_helm_control::steer(&session_id, &path_text, None)
                    .await
                    .map_err(|error| CommandError {
                        code: error.code().to_string(),
                        message: error.message().to_string(),
                    })?;
                return Ok(json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                }));
            }
            if provider == "pi" {
                let summary = crate::pi_helm_control::dispatch_with_attachments(
                    &session_id,
                    crate::pi_helm_control::CommandKind::Steer,
                    Some(&text),
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                    &staged,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "pi",
                    "transport": crate::pi_helm_control::PI_HELM_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                }));
            }
            if provider == "omp" {
                let summary = crate::omp_helm_control::dispatch_with_attachments(
                    &session_id,
                    crate::omp_helm_control::CommandKind::Steer,
                    Some(&text),
                    None,
                    Some(
                        payload
                            .get("longhouse_control_grant")
                            .unwrap_or(&Value::Null),
                    ),
                    durable_command_id,
                    &staged,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message().to_string(),
                })?;
                return Ok(
                    json!({"exit_code":0,"stdout":"","stderr":"","provider":"omp","transport":crate::omp_helm_control::OMP_HELM_TRANSPORT,"provider_session_id":summary.native_session_id}),
                );
            }
            if provider == "antigravity" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: "Antigravity hook inbox does not support active-turn steer"
                        .to_string(),
                });
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            match cmd_codex_bridge_steer(BridgeSteerConfig {
                session_id,
                text,
                state_root: None,
                attachments,
            })
            .await
            {
                Ok(()) => Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "codex",
                    "transport": "codex_app_server",
                })),
                Err(BridgeSteerError::NoActiveTurn) => Err(CommandError::turn_ended(
                    "bridge state does not have an active turn to steer",
                )),
                Err(BridgeSteerError::TurnEnded(message)) => Err(CommandError::turn_ended(message)),
                Err(err) => Err(CommandError::command_failed(err)),
            }
        }
        COMMAND_ANSWER_PAUSE => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| DEFAULT_COMMAND_PROVIDER.to_string());
            if provider == "opencode" {
                let request_id = payload_required_string(&payload, "provider_request_id")?;
                let decision = payload_optional_string(&payload, "decision")
                    .unwrap_or_else(|| "answer".to_string());
                let reply = match decision.as_str() {
                    "answer" => "once",
                    "reject" | "cancel" => "reject",
                    _ => {
                        return Err(CommandError {
                            code: "invalid_pause_decision".to_string(),
                            message: "OpenCode pause decision must be answer, reject, or cancel"
                                .to_string(),
                        });
                    }
                };
                let message = payload_optional_string(&payload, "message");
                let summary = crate::opencode_control::permission_reply(
                    &session_id,
                    &request_id,
                    reply,
                    message.as_deref(),
                )
                .await
                .map_err(CommandError::command_failed)?;
                let status = if reply == "reject" {
                    "rejected"
                } else {
                    "resolved"
                };
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                    "pause_response": {
                        "status": status,
                        "response_text": message,
                        "response_payload": {"reply": reply},
                    },
                }));
            }
            if provider == "claude" {
                let request_key = payload_required_string(&payload, "request_key")?;
                let decision = payload_optional_string(&payload, "decision")
                    .unwrap_or_else(|| "answer".to_string());
                let text = claude_pause_response_text(&payload)?;
                let response_text = text.clone();
                let summary = claude_channel_send_text(ClaudeChannelSendConfig {
                    session_id: session_id.clone(),
                    text,
                    meta: vec![
                        ("intent".to_string(), "pause_response".to_string()),
                        ("request_key".to_string(), request_key.clone()),
                        ("decision".to_string(), decision.clone()),
                    ],
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                return Ok({
                    let mut result = claude_channel_control_result(summary.provider_session_id);
                    if let Some(obj) = result.as_object_mut() {
                        obj.insert(
                            "pause_response".to_string(),
                            json!({
                                "status": "resolved",
                                "response_text": response_text,
                                "response_payload": {
                                    "decision": decision,
                                    "answers": payload.get("answers").cloned(),
                                    "content": payload.get("content").cloned(),
                                    "message": payload_optional_string(&payload, "message"),
                                }
                            }),
                        );
                    }
                    result
                });
            }
            if provider != "codex" {
                return Err(CommandError {
                    code: "unsupported_command".to_string(),
                    message: format!("{provider} does not support remote pause responses yet"),
                });
            }
            let request_key = payload_required_string(&payload, "request_key")?;
            let decision = payload_optional_string(&payload, "decision")
                .unwrap_or_else(|| "answer".to_string());
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            let response = cmd_codex_bridge_pause_response(BridgePauseResponseConfig {
                session_id,
                state_root: None,
                request_key,
                decision,
                answers: payload.get("answers").cloned(),
                content: payload.get("content").cloned(),
                message: payload_optional_string(&payload, "message"),
            })
            .await
            .map_err(CommandError::command_failed)?;
            Ok(json!({
                "exit_code": 0,
                "stdout": serde_json::to_string(&response).unwrap_or_default(),
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
                "pause_response": response,
            }))
        }
        other => Err(CommandError {
            code: "unsupported_command".to_string(),
            message: format!("Unsupported command_type={other}"),
        }),
    }
}

pub(super) async fn execute_invocation_close(
    frame: &Value,
    payload: &Value,
    session_id: &str,
    config: &ShipperConfig,
) -> std::result::Result<Value, CommandError> {
    let command_id = required_string(frame, "command_id")?;
    let run_id = payload_required_string(payload, "run_id")?;
    let thread_id = payload_required_string(payload, "thread_id")?;
    let provider = payload_required_string(payload, "provider")?;
    let reason = payload_required_string(payload, "reason")?;
    if command_id != format!("{run_id}:close") || reason != "user_stop" {
        return Err(CommandError {
            code: "invalid_command".to_string(),
            message:
                "session.invocation.close requires command_id <run_id>:close and reason user_stop"
                    .to_string(),
        });
    }

    // Only the adapters that park work have an invocation to close; any other
    // provider is refused outright rather than answered with a no-op.
    let expected_adapter = match provider.as_str() {
        "claude" => CLAUDE_PRINT_ADAPTER,
        "codex" => CODEX_EXEC_ADAPTER,
        "omp" => OMP_PRINT_ADAPTER,
        _ => {
            return Err(CommandError {
                code: "provider_unsupported".to_string(),
                message: format!("provider={provider} has no parked Console invocation to close"),
            });
        }
    };

    let registry = default_turn_claim_registry().map_err(CommandError::command_failed)?;
    let claim = match registry.read(&run_id) {
        Ok(claim) => claim,
        Err(_) => {
            return Ok(json!({
                "closed": false,
                "invocation_id": Value::Null,
                "stopped": [],
            }));
        }
    };
    let invocation_id = claim.launch_id.clone();
    if claim.session_id != session_id
        || claim.thread_id != thread_id
        || claim.provider != provider
        || claim.adapter.as_deref() != Some(expected_adapter)
        || claim.state != "terminal"
        || claim.invocation_state.as_deref() != Some("parked")
        || claim.pending_count == 0
    {
        return Ok(json!({
            "closed": false,
            "invocation_id": invocation_id,
            "stopped": [],
        }));
    }
    let Some(invocation_id) = invocation_id else {
        return Ok(json!({
            "closed": false,
            "invocation_id": Value::Null,
            "stopped": [],
        }));
    };
    let Some(invocation) = crate::console_lifecycle::lookup_launch(&invocation_id) else {
        return Ok(json!({
            "closed": false,
            "invocation_id": invocation_id,
            "stopped": [],
        }));
    };
    if invocation.provider != provider || invocation.latest_turn().run_id != run_id {
        return Ok(json!({
            "closed": false,
            "invocation_id": invocation_id,
            "stopped": [],
        }));
    }

    let outcome = match provider.as_str() {
        "claude" => {
            crate::claude_print::close_parked_claude_invocation(
                &claim,
                invocation,
                &config.machine_name,
            )
            .await
        }
        "codex" => {
            crate::codex_exec::close_parked_codex_invocation(
                &claim,
                invocation,
                &config.machine_name,
            )
            .await
        }
        "omp" => {
            crate::omp_print::close_parked_omp_invocation(&claim, invocation, &config.machine_name)
                .await
        }
        _ => unreachable!("provider was admitted above"),
    }
    .map_err(CommandError::command_failed)?;
    let Some(outcome) = outcome else {
        return Ok(json!({
            "closed": false,
            "invocation_id": invocation_id,
            "stopped": [],
        }));
    };
    let stopped = outcome
        .stopped
        .iter()
        .map(|item| {
            json!({
                "id": item.id,
                "kind": item.kind,
                "description": item.description,
            })
        })
        .collect::<Vec<_>>();
    let mut response = json!({
        "closed": true,
        "invocation_id": outcome.invocation_id,
        "stopped": stopped,
        "cleanup": outcome.cleanup.as_str(),
    });
    if let Some(error_note) = outcome.error_note {
        response["error_note"] = Value::String(error_note);
    }
    Ok(response)
}

pub(super) async fn run_archive_backlog_control_command(
    payload: &Value,
) -> std::result::Result<Value, CommandError> {
    let mode = payload_required_string(payload, "mode")?;
    let normalized_mode = match mode.trim().to_ascii_lowercase().as_str() {
        "paused" | "pause" => "paused",
        "trickle" | "resume" => "trickle",
        "drain" | "drain-now" => "drain",
        other => {
            return Err(CommandError {
                code: "archive_control_invalid_mode".to_string(),
                message: format!("unsupported archive repair mode {other}"),
            });
        }
    };
    let mut control = json!({
        "mode": normalized_mode,
        "updated_at": timestamp_now(),
        "actor": payload.get("actor").and_then(Value::as_str).unwrap_or("machine_api"),
    });
    if let Some(reason) = payload
        .get("reason")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|reason| !reason.is_empty())
    {
        control["reason"] = json!(reason);
    }
    if normalized_mode != "paused" {
        let lease_seconds = payload
            .get("lease_seconds")
            .and_then(Value::as_u64)
            .unwrap_or(3600)
            .clamp(60, 86_400);
        control["expires_at"] = json!((chrono::Utc::now()
            + chrono::Duration::seconds(lease_seconds as i64))
        .to_rfc3339());
    }

    let path =
        crate::config::get_agent_archive_repair_control_path().map_err(|err| CommandError {
            code: "archive_control_write_failed".to_string(),
            message: err.to_string(),
        })?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| CommandError {
            code: "archive_control_write_failed".to_string(),
            message: err.to_string(),
        })?;
    }
    let bytes = serde_json::to_vec_pretty(&control).map_err(CommandError::command_failed)?;
    std::fs::write(&path, bytes).map_err(|err| CommandError {
        code: "archive_control_write_failed".to_string(),
        message: err.to_string(),
    })?;

    Ok(json!({
        "mode": normalized_mode,
        "path": path.to_string_lossy(),
    }))
}

pub(super) fn claude_pause_response_text(
    payload: &Value,
) -> std::result::Result<String, CommandError> {
    let decision = payload_optional_string(payload, "decision")
        .unwrap_or_else(|| "answer".to_string())
        .to_ascii_lowercase();
    if let Some(message) = payload_optional_string(payload, "message") {
        if !message.trim().is_empty() {
            return Ok(message);
        }
    }
    if let Some(content) = payload.get("content") {
        if let Some(text) = content.as_str() {
            if !text.trim().is_empty() {
                return Ok(text.trim().to_string());
            }
        } else if !content.is_null() {
            let text = content.to_string();
            if !text.trim().is_empty() {
                return Ok(text.trim().to_string());
            }
        }
    }
    if let Some(answers) = payload.get("answers").and_then(Value::as_object) {
        let mut entries: Vec<_> = answers.iter().collect();
        entries.sort_by(|(left, _), (right, _)| left.cmp(right));
        let parts: Vec<String> = entries
            .into_iter()
            .filter_map(|(key, value)| {
                let label = key.trim();
                if label.is_empty() {
                    return None;
                }
                let values = claude_pause_answer_values(value);
                if values.is_empty() {
                    return None;
                }
                Some(format!("{label}: {}", values.join(", ")))
            })
            .collect();
        if !parts.is_empty() {
            return Ok(parts.join("; "));
        }
    }
    if decision == "cancel" || decision == "reject" {
        return Ok("Cancelled in Longhouse.".to_string());
    }
    Err(CommandError {
        code: "invalid_command".to_string(),
        message: "Claude pause responses require a non-empty answer message".to_string(),
    })
}

pub(super) fn claude_channel_control_result(provider_session_id: Option<String>) -> Value {
    let mut result = json!({
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "provider": "claude",
        "transport": "claude_channel_bridge",
    });
    if let Some(provider_session_id) = provider_session_id {
        if !provider_session_id.trim().is_empty() {
            if let Some(obj) = result.as_object_mut() {
                obj.insert(
                    "provider_session_id".to_string(),
                    json!(provider_session_id),
                );
            }
        }
    }
    result
}

pub(super) fn claude_channel_error_to_command_error(
    err: ClaudeChannelControlError,
) -> CommandError {
    match err {
        ClaudeChannelControlError::SessionNotAttached { message, .. } => {
            CommandError::session_not_attached(anyhow!(message))
        }
        ClaudeChannelControlError::CommandFailed(message) => {
            CommandError::command_failed(anyhow!(message))
        }
    }
}

pub(super) fn claude_pause_answer_values(value: &Value) -> Vec<String> {
    match value {
        Value::Null => Vec::new(),
        Value::Array(items) => items
            .iter()
            .filter_map(|item| {
                let text = match item {
                    Value::Null => return None,
                    Value::String(text) => text.trim().to_string(),
                    other => other.to_string(),
                };
                if text.trim().is_empty() {
                    None
                } else {
                    Some(text)
                }
            })
            .collect(),
        Value::String(text) => {
            let text = text.trim();
            if text.is_empty() {
                Vec::new()
            } else {
                vec![text.to_string()]
            }
        }
        other => vec![other.to_string()],
    }
}

pub(super) async fn run_provider_sign_in_command(
    command_type: &str,
    payload: &Value,
) -> std::result::Result<Value, CommandError> {
    let to_command_error = |error: crate::sign_in::SignInError| CommandError {
        code: error.code.to_string(),
        message: error.message,
    };
    match command_type {
        COMMAND_PROVIDER_SIGN_IN_START => {
            let provider = required_string(payload, "provider")?;
            let contract = managed_provider_contract_items()
                .iter()
                .find(|item| {
                    item.get("provider").and_then(Value::as_str) == Some(provider.as_str())
                })
                .ok_or_else(|| CommandError {
                    code: "sign_in_unsupported".to_string(),
                    message: format!("unknown provider {provider}"),
                })?;
            let path_value = std::env::var_os("PATH");
            let binary = provider_binary_value(contract, &|name| std::env::var_os(name))
                .filter(|binary| {
                    command_value_exists_in_path(binary.as_os_str(), path_value.as_deref())
                })
                .ok_or_else(|| CommandError {
                    code: "sign_in_cli_missing".to_string(),
                    message: format!("{provider} CLI is not installed on this machine"),
                })?;
            crate::sign_in::start(contract, binary)
                .await
                .map_err(to_command_error)
        }
        COMMAND_PROVIDER_SIGN_IN_CODE => {
            let attempt_id = required_string(payload, "attempt_id")?;
            let code = required_string(payload, "code")?;
            crate::sign_in::submit_code(&attempt_id, &code)
                .await
                .map_err(to_command_error)
        }
        _ => {
            let attempt_id = required_string(payload, "attempt_id")?;
            Ok(crate::sign_in::cancel(&attempt_id))
        }
    }
}
