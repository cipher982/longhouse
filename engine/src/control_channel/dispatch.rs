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
            let attachments = crate::codex_attachments::parse_attachments(&payload)
                .map_err(CommandError::command_failed)?;
            let text =
                crate::input_attachments::text_or_attachments(&payload, "text", &attachments)
                    .map_err(|error| CommandError {
                        code: "invalid_command".to_string(),
                        message: error.to_string(),
                    })?;
            if let Some(command) = crate::slash_input::command(&text) {
                return Err(CommandError {
                    code: "turn_active".to_string(),
                    message: format!(
                        "/{} runs when the current turn ends; it was not sent into the running turn",
                        command.name
                    ),
                });
            }
            let text = crate::slash_input::message_text(&text);
            let delivery = crate::input_attachments::attachment_delivery(&provider, "console");
            if !attachments.is_empty() && delivery.is_none() {
                return Err(CommandError {
                    code: "attachments_unsupported".to_string(),
                    message: format!(
                        "provider={provider} does not accept Console steer image attachments"
                    ),
                });
            }
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
            // Stage before steering, the way Console turn start does. The files
            // live until the run settles, because the provider may read them
            // after the steer is accepted.
            let (staging_dir, staged) = turn_start::stage_console_attachments(
                config,
                &session_id,
                Path::new(""),
                &uuid::Uuid::new_v4().to_string(),
                &attachments,
            )
            .await?;
            let (text, image_paths) = console_steer_input(text, delivery, &staged);
            let steered = async {
                let (transport, outcome) = match provider.as_str() {
                    "codex" => {
                        crate::codex_exec::steer_codex_console_turn(&run_id, &text, &image_paths)
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
                        crate::pi_print::steer_pi_print_turn(
                            &run_id,
                            &session_id,
                            &text,
                            &image_paths,
                        )
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
                        crate::omp_print::steer_omp_print_turn(
                            &run_id,
                            &session_id,
                            &text,
                            &image_paths,
                        )
                        .await
                        .map_err(|reason| CommandError {
                            code: reason.clone(),
                            message: format!(
                                "OMP Console turn {run_id} did not take the steer: {reason}"
                            ),
                        })?;
                        (OMP_PRINT_ADAPTER, ConsoleSteerOutcome::Steered)
                    }
                    "antigravity" => {
                        crate::antigravity_acp::steer_antigravity_acp_turn(
                            &run_id,
                            &session_id,
                            &text,
                            &image_paths,
                        )
                        .await
                        .map_err(|reason| CommandError {
                            code: reason.clone(),
                            message: format!(
                                "Antigravity Console turn {run_id} did not take the steer: {reason}"
                            ),
                        })?;
                        (ANTIGRAVITY_ACP_ADAPTER, ConsoleSteerOutcome::Steered)
                    }
                    "opencode" => {
                        let outcome = crate::opencode_run::steer_opencode_console_turn(
                            &run_id,
                            &session_id,
                            &text,
                            &image_paths,
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
                Ok::<_, CommandError>((transport, outcome))
            }
            .await;
            // Cleanup waits for the run to settle in every case: an unknown
            // outcome may still have delivered the images.
            if let Some(dir) = staging_dir {
                crate::input_attachments::schedule_console_cleanup(dir, run_id.clone());
            }
            let (transport, outcome) = steered?;
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
                    crate::antigravity_acp::interrupt_antigravity_acp_turn(
                        &run_id,
                        &session_id,
                        &thread_id,
                        &turn_id,
                    )
                    .await
                    .map_err(CommandError::command_failed)?;
                    ANTIGRAVITY_ACP_ADAPTER
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
            // A slash command runs as if typed; a bare picker, or a mode that
            // cannot run commands, is refused before any provider sees it.
            let text = admit_owner_text(&provider, "helm", &text)?;
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
                    input_id: durable_command_id.map(str::to_owned),
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                let mut result = claude_channel_control_result(summary.provider_session_id);
                if let Some(owner_input) = summary.owner_input {
                    result["owner_input_path"] = json!(owner_input.path);
                    result["owner_input_reason"] = json!(owner_input.reason);
                    result["owner_input_turn_started"] = json!(owner_input.delivered);
                    if let Some(command) = crate::slash_input::command(&text) {
                        crate::slash_input::mark_result(
                            &mut result,
                            &command,
                            !owner_input.command_ran,
                        );
                    }
                }
                return Ok(result);
            }
            if provider == "opencode" {
                let summary = crate::opencode_control::send_text(&session_id, &text, &staged)
                    .await
                    .map_err(CommandError::command_failed)?;
                let mut result = json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "opencode",
                    "transport": crate::opencode_control::OPENCODE_SERVER_BRIDGE_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                });
                if let (Some(command), Some(turn_started)) = (
                    crate::slash_input::command(&text),
                    summary.command_turn_started,
                ) {
                    crate::slash_input::mark_result(&mut result, &command, turn_started);
                }
                return Ok(result);
            }
            if provider == "antigravity" {
                // Images arrive as staged file paths inside the injected
                // message (the model opens them with read_file).
                let wait_claimed_secs = payload.get("wait_claimed_secs").and_then(Value::as_f64);
                let outcome = crate::antigravity_channel_control::send_text_with_wait(
                    &session_id,
                    &path_text,
                    wait_claimed_secs,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message(),
                })?;
                let mut result = json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "antigravity",
                    "transport": crate::antigravity_channel_control::ANTIGRAVITY_HOOK_INBOX_TRANSPORT,
                    "message_id": outcome.message_id,
                    "claimed_at": outcome.claimed_at,
                });
                if let Some(command) = crate::slash_input::command(&text) {
                    crate::slash_input::mark_command(&mut result, &command);
                }
                return Ok(result);
            }
            if provider == "cursor" {
                // Cursor's terminal closes the session on a bare `quit` or
                // `exit` typed as a message; from the app that would end the
                // session on words meant for the model.
                if matches!(text.trim().to_ascii_lowercase().as_str(), "quit" | "exit") {
                    return Err(CommandError {
                        code: crate::slash_input::REFUSED_CODE.to_string(),
                        message: format!(
                            "\"{}\" was not sent: Cursor's terminal reads it as closing the session. Send /exit to end the session, or add more words.",
                            text.trim()
                        ),
                    });
                }
                let summary = crate::cursor_helm_control::send_text(&session_id, &path_text, None)
                    .await
                    .map_err(|err| CommandError {
                        code: err.code().to_string(),
                        message: err.message().to_string(),
                    })?;
                let mut result = json!({
                    "exit_code": summary.exit_code,
                    "stdout": summary.stdout,
                    "stderr": summary.stderr,
                    "provider": "cursor",
                    "transport": crate::cursor_helm_control::CURSOR_HELM_TRANSPORT,
                });
                if let Some(command) = crate::slash_input::command(&text) {
                    crate::slash_input::mark_command(&mut result, &command);
                }
                return Ok(result);
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
                let mut result = json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "pi",
                    "transport": crate::pi_helm_control::PI_HELM_TRANSPORT,
                    "provider_session_id": summary.provider_session_id,
                    "status": summary.status,
                });
                if let Some(command) = crate::slash_input::command(&text) {
                    mark_helm_command(&mut result, &command, summary.turn_started);
                }
                return Ok(result);
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
                let mut result = json!({"exit_code":0,"stdout":"","stderr":"","provider":"omp","transport":crate::omp_helm_control::OMP_HELM_TRANSPORT,"provider_session_id":summary.native_session_id,"status":summary.status});
                if let Some(command) = crate::slash_input::command(&text) {
                    mark_helm_command(&mut result, &command, summary.turn_started);
                }
                return Ok(result);
            }
            validate_codex_bridge_attached(&session_id, None)
                .map_err(CommandError::session_not_attached)?;
            // Codex runs slash commands only in its TUI: the ones with an
            // app-server request go to the bridge, the rest are refused here.
            let command = crate::slash_input::command(&text);
            let text = match &command {
                Some(command) => match crate::codex_slash::route("", command) {
                    crate::codex_slash::CodexSlash::Refuse(reason) => {
                        return Err(CommandError {
                            code: crate::slash_input::REFUSED_CODE.to_string(),
                            message: format!("/{} was not sent: {reason}.", command.name),
                        })
                    }
                    _ => text,
                },
                None => crate::slash_input::message_text(&text),
            };
            let summary = cmd_codex_bridge_send(BridgeSendConfig {
                session_id: session_id.clone(),
                text,
                state_root: None,
                allow_direct_ws_fallback: false,
                attachments,
            })
            .await
            .map_err(|err| CommandError::command_failed(err))?;
            let mut result = json!({
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "provider": "codex",
                "transport": "codex_app_server",
                "thread_id": summary.thread_id,
                "turn_id": summary.turn_id,
                "turn_status": summary.turn_status,
            });
            if let (Some(name), Some(command)) = (summary.command.as_ref(), command.as_ref()) {
                debug_assert_eq!(name, &command.name);
                match summary.command_turn_started {
                    Some(turn) => crate::slash_input::mark_result(&mut result, command, turn),
                    None => crate::slash_input::mark_command(&mut result, command),
                }
            }
            Ok(result)
        }
        COMMAND_INTERRUPT => {
            let provider = payload_optional_string(&payload, "provider")
                .unwrap_or_else(|| DEFAULT_COMMAND_PROVIDER.to_string());
            if provider == "claude" {
                let summary = claude_channel_interrupt(ClaudeChannelInterruptConfig {
                    session_id,
                    state_root: None,
                    wait_timeout: None,
                })
                .await
                .map_err(claude_channel_error_to_command_error)?;
                let mut result = claude_channel_control_result(None);
                result["abort_path"] = json!(summary.path);
                result["tool_process_group"] = json!(summary.tool_process_group);
                return Ok(result);
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
                let outcome = crate::antigravity_channel_control::interrupt(&session_id)
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
            if provider == "antigravity" {
                let outcome = crate::antigravity_channel_control::terminate(&session_id)
                    .await
                    .map_err(CommandError::command_failed)?;
                return Ok(json!({
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "provider": "antigravity",
                    "transport": crate::antigravity_channel_control::ANTIGRAVITY_HOOK_INBOX_TRANSPORT,
                    "pid": outcome.pid,
                    "process_group_id": outcome.process_group_id,
                    "shutdown": outcome.shutdown,
                }));
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
            // A command never enters a running turn: the server holds it until
            // the session is idle and sends it then.
            if let Some(command) = crate::slash_input::command(&text) {
                return Err(CommandError {
                    code: "turn_active".to_string(),
                    message: format!(
                        "/{} runs when the current turn ends; it was not sent into the running turn",
                        command.name
                    ),
                });
            }
            let text = crate::slash_input::message_text(&text);
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
                    // A replayed steer that answers an open question is
                    // recognised by its command id, like a send.
                    input_id: durable_command_id.map(str::to_owned),
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
                let wait_claimed_secs = payload.get("wait_claimed_secs").and_then(Value::as_f64);
                let outcome = crate::antigravity_channel_control::steer_text_with_wait(
                    &session_id,
                    &path_text,
                    wait_claimed_secs,
                )
                .await
                .map_err(|error| CommandError {
                    code: error.code().to_string(),
                    message: error.message(),
                })?;
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
                let open = crate::claude_channel_control::open_question(&session_id, None)
                    .map_err(claude_channel_error_to_command_error)?
                    .ok_or_else(|| CommandError {
                        code: "question_not_answerable".to_string(),
                        message: "Claude has no question open that Longhouse can answer; \
                                  answer it in the terminal."
                            .to_string(),
                    })?;
                if let Some(asked) = payload_optional_string(&payload, "provider_request_id") {
                    if asked != open.tool_use_id {
                        return Err(CommandError {
                            code: "question_not_answerable".to_string(),
                            message: "Claude is no longer asking that question.".to_string(),
                        });
                    }
                }
                let answer = claude_question_answer(&payload, &open)?;
                crate::claude_channel_control::answer_question(
                    &session_id,
                    None,
                    &answer,
                    frame.get("command_id").and_then(Value::as_str),
                    crate::control_wait::control_wait(CLAUDE_ANSWER_WAIT),
                )
                .await
                .map_err(claude_channel_error_to_command_error)?;
                let response_text = claude_answer_text(&answer);
                let status = if answer.declined {
                    "rejected"
                } else {
                    "resolved"
                };
                return Ok({
                    let mut result = claude_channel_control_result(None);
                    if let Some(obj) = result.as_object_mut() {
                        obj.insert("transport".to_string(), json!("claude_mod"));
                        obj.insert(
                            "pause_response".to_string(),
                            json!({
                                "status": status,
                                "response_text": response_text,
                                "response_payload": {
                                    "answers": answer.answers,
                                    "response": answer.response,
                                    "declined": answer.declined,
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

/// How long a Longhouse answer waits for the mod to resolve Claude's dialog.
const CLAUDE_ANSWER_WAIT: std::time::Duration = std::time::Duration::from_secs(10);

/// Turn a Longhouse pause answer into AskUserQuestion's answer. The app keys
/// answers by the projection's question id (`question_<n>`, 1-based) or by the
/// question text; Claude keys them by the question text, the chosen labels
/// comma-joined. Free text with no structured answer is Claude's `response`.
pub(super) fn claude_question_answer(
    payload: &Value,
    open: &crate::claude_owner_input::OpenQuestion,
) -> std::result::Result<crate::claude_owner_input::QuestionAnswer, CommandError> {
    let decision = payload_optional_string(payload, "decision")
        .unwrap_or_else(|| "answer".to_string())
        .to_ascii_lowercase();
    let mut answer = crate::claude_owner_input::QuestionAnswer {
        tool_use_id: open.tool_use_id.clone(),
        ..Default::default()
    };
    if decision == "reject" || decision == "cancel" {
        answer.declined = true;
        return Ok(answer);
    }
    let texts: Vec<String> = open
        .questions
        .as_array()
        .map(|items| {
            items
                .iter()
                .map(|item| {
                    item.get("question")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string()
                })
                .collect()
        })
        .unwrap_or_default();
    if let Some(answers) = payload.get("answers").and_then(Value::as_object) {
        for (key, value) in answers {
            let key = key.trim();
            let question = key
                .strip_prefix("question_")
                .and_then(|n| n.parse::<usize>().ok())
                .and_then(|n| n.checked_sub(1))
                .and_then(|index| texts.get(index))
                .filter(|text| !text.is_empty())
                .cloned()
                .or_else(|| texts.iter().find(|text| text.as_str() == key).cloned());
            let Some(question) = question else {
                return Err(CommandError {
                    code: "invalid_command".to_string(),
                    message: format!("answer key {key:?} names none of Claude's questions"),
                });
            };
            let values = claude_pause_answer_values(value);
            if !values.is_empty() {
                answer.answers.insert(question, values.join(", "));
            }
        }
    }
    if answer.answers.is_empty() {
        let text = payload_optional_string(payload, "message")
            .or_else(|| match payload.get("content") {
                Some(Value::String(text)) => Some(text.clone()),
                Some(Value::Null) | None => None,
                Some(other) => Some(other.to_string()),
            })
            .map(|text| text.trim().to_string())
            .filter(|text| !text.is_empty());
        let Some(text) = text else {
            return Err(CommandError {
                code: "invalid_command".to_string(),
                message: "Claude pause responses require an answer".to_string(),
            });
        };
        answer.response = Some(text);
    }
    Ok(answer)
}

/// The answer as one line, for the pause request's response text.
pub(super) fn claude_answer_text(answer: &crate::claude_owner_input::QuestionAnswer) -> String {
    if answer.declined {
        return "Declined in Longhouse.".to_string();
    }
    if let Some(response) = &answer.response {
        return response.clone();
    }
    answer
        .answers
        .iter()
        .map(|(question, label)| format!("{question} {label}"))
        .collect::<Vec<_>>()
        .join("; ")
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

/// Mark a Pi or OMP Helm send of a slash command. Their extension submits
/// the command through the editor and says whether a model turn began; an
/// older extension says nothing, and the turn is left unknown.
fn mark_helm_command(
    result: &mut Value,
    command: &crate::slash_input::SlashCommand,
    turn_started: Option<bool>,
) {
    match turn_started {
        Some(started) => crate::slash_input::mark_result(result, command, started),
        None => crate::slash_input::mark_command(result, command),
    }
}

/// The owner's text as the provider should receive it in `mode`, or the
/// refusal the app shows (see [`crate::slash_input`]).
pub(super) fn admit_owner_text(
    provider: &str,
    mode: &str,
    text: &str,
) -> Result<String, CommandError> {
    let policy = super::capabilities::slash_policy(provider);
    crate::slash_input::admit(provider, mode, &policy, text).map_err(|refusal| CommandError {
        code: refusal.code.to_string(),
        message: refusal.message,
    })
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
        ClaudeChannelControlError::TurnEnded(message) => CommandError::turn_ended(message),
        ClaudeChannelControlError::Coded { code, message } => CommandError {
            code: code.to_string(),
            message,
        },
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

/// What a Console steer hands the provider. Path-delivery providers (Claude)
/// get the staged files named in the text; native providers (Codex, OpenCode,
/// Pi, OMP) get the image paths beside the text, never both.
fn console_steer_input(
    text: String,
    delivery: Option<&str>,
    staged: &[crate::input_attachments::StagedAttachment],
) -> (String, Vec<PathBuf>) {
    if delivery == Some("path") {
        (
            crate::input_attachments::prompt_with_attachments(&text, staged),
            Vec::new(),
        )
    } else {
        (text, staged.iter().map(|item| item.path.clone()).collect())
    }
}

#[cfg(test)]
mod steer_input_tests {
    use super::*;
    use crate::input_attachments::StagedAttachment;

    fn staged() -> Vec<StagedAttachment> {
        vec![StagedAttachment {
            path: PathBuf::from("/tmp/lh-attach/console/x/a.png"),
            mime_type: "image/png".to_string(),
        }]
    }

    #[test]
    fn claude_steer_names_the_staged_files_in_the_text() {
        let (text, images) = console_steer_input("look".to_string(), Some("path"), &staged());
        assert!(
            text.starts_with("look\n\n[Longhouse attachments]"),
            "{text}"
        );
        assert!(text.contains("/tmp/lh-attach/console/x/a.png"), "{text}");
        assert!(images.is_empty());
    }

    #[test]
    fn native_providers_get_image_paths_beside_untouched_text() {
        let (text, images) = console_steer_input("look".to_string(), Some("native"), &staged());
        assert_eq!(text, "look");
        assert_eq!(
            images,
            vec![PathBuf::from("/tmp/lh-attach/console/x/a.png")]
        );
    }

    /// The factory's Console steer-drop control runs against Antigravity too: the fault fires ahead of the
    /// provider match, so an agy steer is acknowledged and no `session/prompt` reaches the ACP process. The
    /// run id names no live turn, so without the fault the same command is refused.
    #[cfg(feature = "qa-fault-injection")]
    #[tokio::test]
    async fn console_steer_noop_drops_an_antigravity_acp_steer() {
        let receipt = tempfile::NamedTempFile::new().unwrap();
        let frame = json!({
            "command_type": COMMAND_TURN_STEER,
            "session_id": "agy-session",
            "payload": {"provider": "antigravity", "run_id": "no-such-run", "text": "steer"},
        });
        let config = ShipperConfig::default();
        std::env::remove_var("LONGHOUSE_QA_FAULT");
        let refused = execute_command(&frame, &config).await;
        assert!(refused.is_err(), "no live turn: {refused:?}");

        std::env::set_var("LONGHOUSE_QA_FAULT", "console_steer_noop");
        std::env::set_var("LONGHOUSE_QA_FAULT_RECEIPT", receipt.path());
        let dropped = execute_command(&frame, &config).await;
        std::env::remove_var("LONGHOUSE_QA_FAULT");
        std::env::remove_var("LONGHOUSE_QA_FAULT_RECEIPT");
        let dropped = dropped.expect("the fault acknowledges the steer");
        assert_eq!(dropped["transport"], "qa_fault_noop");
        assert_eq!(dropped["steered"], true);
        let fired = std::fs::read_to_string(receipt.path()).unwrap();
        assert!(
            fired.contains("console_steer_noop") && fired.contains("antigravity"),
            "{fired}"
        );
    }
}

#[cfg(test)]
mod helm_command_tests {
    use super::*;

    #[test]
    fn a_helm_command_reports_the_turn_the_extension_saw() {
        let command = crate::slash_input::command("/name proof").unwrap();
        let mut ran = json!({});
        mark_helm_command(&mut ran, &command, Some(false));
        assert_eq!(ran["turn_started"], false);
        let mut turn = json!({});
        mark_helm_command(&mut turn, &command, Some(true));
        assert_eq!(turn["turn_started"], true);
        let mut unknown = json!({});
        mark_helm_command(&mut unknown, &command, None);
        assert_eq!(unknown["command"], "name");
        assert!(unknown.get("turn_started").is_none());
    }
}
