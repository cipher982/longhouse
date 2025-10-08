use crate::messages::{Command, Message};
use crate::models::ApiAgentDetails;
use crate::models::ApiWorkflow;
use crate::models::{ApiAgent, ApiThread, ApiThreadMessage};
use crate::network::api_client::ApiClient;
use crate::state::{dispatch_global_message, APP_STATE};
use crate::debug_log;
use std::cell::RefCell;
use std::rc::Rc;

pub fn execute_fetch_command(cmd: Command) {
    match cmd {
        Command::FetchThreads(agent_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_threads(Some(agent_id)).await {
                    Ok(response) => match serde_json::from_str::<Vec<ApiThread>>(&response) {
                        Ok(threads) => dispatch_global_message(Message::AgentThreadsLoaded {
                            agent_id,
                            threads,
                        }),
                        Err(e) => web_sys::console::error_1(
                            &format!("Failed to parse threads: {:?}", e).into(),
                        ),
                    },
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch threads: {:?}", e).into(),
                    ),
                }
            });
        }
        Command::FetchThreadMessages(thread_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_thread_messages(thread_id, 0, 100).await {
                    Ok(messages) => {
                        match serde_json::from_str::<Vec<ApiThreadMessage>>(&messages) {
                            Ok(messages) => dispatch_global_message(Message::ThreadMessagesLoaded(
                                thread_id, messages,
                            )),
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse messages: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch messages: {:?}", e).into(),
                    ),
                }
            });
        }
        Command::LoadAgentInfo(agent_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_agent(agent_id).await {
                    Ok(response) => match serde_json::from_str::<ApiAgent>(&response) {
                        Ok(agent) => {
                            dispatch_global_message(Message::AgentInfoLoaded(Box::new(agent)))
                        }
                        Err(e) => web_sys::console::error_1(
                            &format!("Failed to parse agent: {:?}", e).into(),
                        ),
                    },
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to fetch agent: {:?}", e).into())
                    }
                }
            });
        }
        Command::FetchAgents => {
            // Set loading state before starting fetch - direct mutation since we're in command executor
            APP_STATE.with(|state_ref| {
                let mut state = state_ref.borrow_mut();
                state.is_loading = true;
            });

            // Trigger UI refresh to show loading state
            if let Err(e) = crate::state::AppState::refresh_ui_after_state_change() {
                web_sys::console::error_1(
                    &format!("Failed to refresh UI for loading state: {:?}", e).into(),
                );
            }

            // Determine current dashboard scope before starting the async
            // task so we don’t hold a RefCell borrow across await points.
            let scope_str = crate::state::APP_STATE.with(|state_ref| {
                let state = state_ref.borrow();
                state.dashboard_scope.as_str().to_string()
            });

            debug_log!("Executing FetchAgents command (scope={})", scope_str);
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_agents_scoped(&scope_str).await {
                    Ok(agents_json) => match serde_json::from_str::<Vec<ApiAgent>>(&agents_json) {
                        Ok(agents) => {
                            debug_log!("Fetched {} agents from API", agents.len());
                            // Clear loading state before dispatching agents - direct mutation in command executor
                            APP_STATE.with(|state_ref| {
                                let mut state = state_ref.borrow_mut();
                                state.is_loading = false;
                            });
                            dispatch_global_message(Message::AgentsRefreshed(agents))
                        }
                        Err(e) => {
                            // Clear loading state on error - direct mutation in command executor
                            APP_STATE.with(|state_ref| {
                                let mut state = state_ref.borrow_mut();
                                state.is_loading = false;
                            });
                            web_sys::console::error_1(
                                &format!("Failed to parse agents: {:?}", e).into(),
                            );
                        }
                    },
                    Err(e) => {
                        // Clear loading state on error - direct mutation in command executor
                        APP_STATE.with(|state_ref| {
                            let mut state = state_ref.borrow_mut();
                            state.is_loading = false;
                        });
                        web_sys::console::error_1(
                            &format!("Failed to fetch agents: {:?}", e).into(),
                        );
                    }
                }
            });
        }

        // -----------------------------------------------------------
        // Workflow helpers (new)
        // -----------------------------------------------------------
        Command::FetchWorkflows => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_workflows().await {
                    Ok(json_str) => {
                        match serde_json::from_str::<Vec<crate::models::ApiWorkflow>>(&json_str) {
                            Ok(api_wfs) => {
                                let workflows: Vec<ApiWorkflow> = api_wfs;
                                dispatch_global_message(Message::WorkflowsLoaded(workflows));
                            }
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse workflows: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch workflows: {:?}", e).into(),
                    ),
                }
            });
        }

        Command::FetchCurrentWorkflow => {
            // Increment request token to identify and drop stale responses - direct mutation in command executor
            let req_id = APP_STATE.with(|state_ref| {
                let mut st = state_ref.borrow_mut();
                st.workflow_fetch_seq = st.workflow_fetch_seq.wrapping_add(1);
                st.workflow_fetch_seq
            });

            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_current_workflow().await {
                    Ok(json_str) => {
                        debug_log!("🔍 Raw workflow JSON from backend: {}", json_str);
                        match serde_json::from_str::<crate::models::ApiWorkflow>(&json_str) {
                            Ok(api_wf) => {
                                let node_count = api_wf
                                    .canvas
                                    .as_object()
                                    .and_then(|obj| obj.get("nodes"))
                                    .and_then(|nodes| nodes.as_array())
                                    .map_or(0, |arr| arr.len());
                                let edge_count = api_wf
                                    .canvas
                                    .as_object()
                                    .and_then(|obj| obj.get("edges"))
                                    .and_then(|edges| edges.as_array())
                                    .map_or(0, |arr| arr.len());
                                debug_log!(
                                    "🔍 Parsed workflow: {} nodes, {} edges",
                                    node_count, edge_count
                                );
                                let workflow: ApiWorkflow = api_wf;
                                // Only dispatch if this response matches the latest request token
                                let should_apply = APP_STATE.with(|state_ref| {
                                    let st = state_ref.borrow();
                                    st.workflow_fetch_seq == req_id
                                });
                                if should_apply {
                                    dispatch_global_message(Message::CurrentWorkflowLoaded(workflow));
                                } else {
                                    debug_log!(
                                        "⚠️ Dropping stale workflow response (newer request in flight)"
                                    );
                                }
                            }
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse current workflow: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch current workflow: {:?}", e).into(),
                    ),
                }
            });
        }

        // ---------------- Workflow CRUD commands -------------------
        Command::CreateWorkflowApi { name } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::create_workflow(&name).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<crate::models::ApiWorkflow>(&json_str) {
                            Ok(api_wf) => {
                                let wf: ApiWorkflow = api_wf;
                                dispatch_global_message(Message::WorkflowCreated(wf));
                                crate::toast::success("Workflow created successfully!");
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to parse created workflow: {:?}", e).into(),
                                );
                                crate::toast::error("Failed to process workflow creation response");
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }
        Command::DeleteWorkflowApi { workflow_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::delete_workflow(workflow_id).await {
                    Ok(()) => {
                        dispatch_global_message(Message::WorkflowDeleted { workflow_id });
                        crate::toast::success("Workflow deleted successfully");
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }
        Command::RenameWorkflowApi {
            workflow_id,
            name,
            description,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::rename_workflow(workflow_id, &name, &description).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<crate::models::ApiWorkflow>(&json_str) {
                            Ok(api_wf) => {
                                let wf: ApiWorkflow = api_wf;
                                dispatch_global_message(Message::WorkflowUpdated(wf));
                                crate::toast::success("Workflow updated successfully!");
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to parse renamed workflow: {:?}", e).into(),
                                );
                                crate::toast::error("Failed to process workflow update response");
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        // -----------------------------------------------------------
        // Start workflow execution – call backend and then subscribe
        // -----------------------------------------------------------
        Command::StartWorkflowExecutionApi { workflow_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::start_workflow_execution(workflow_id).await {
                    Ok(json_str) => {
                        // Expected response: { "execution_id": 123, "status": "running" }
                        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&json_str) {
                            if let Some(exec_id_val) = val.get("execution_id") {
                                if let Some(exec_id_u64) = exec_id_val.as_u64() {
                                    let exec_id = exec_id_u64 as u32;
                                    crate::state::dispatch_global_message(
                                        crate::messages::Message::SubscribeWorkflowExecution {
                                            execution_id: exec_id,
                                        },
                                    );
                                }
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        // -----------------------------------------------------------
        // Reserve workflow execution – get execution ID and subscribe before starting
        // -----------------------------------------------------------
        Command::ReserveWorkflowExecutionApi { workflow_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::reserve_workflow_execution(workflow_id).await {
                    Ok(json_str) => {
                        // Expected response: { "execution_id": 123, "status": "reserved" }
                        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&json_str) {
                            if let Some(exec_id_val) = val.get("execution_id") {
                                if let Some(exec_id_u64) = exec_id_val.as_u64() {
                                    let exec_id = exec_id_u64 as u32;
                                    // Subscribe to the reserved execution first
                                    crate::state::dispatch_global_message(
                                        crate::messages::Message::SubscribeWorkflowExecution {
                                            execution_id: exec_id,
                                        },
                                    );
                                    // Then start the execution
                                    crate::state::dispatch_global_message(
                                        crate::messages::Message::StartReservedExecution {
                                            execution_id: exec_id,
                                        },
                                    );
                                }
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        // -----------------------------------------------------------
        // Start reserved execution
        // -----------------------------------------------------------
        Command::StartReservedExecutionApi { execution_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::start_reserved_execution(execution_id).await {
                    Ok(_json_str) => {
                        // Execution started successfully
                        debug_log!("✅ Reserved execution {} started", execution_id);
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        // -----------------------------------------------------------
        // Workflow Scheduling commands
        // -----------------------------------------------------------
        Command::ScheduleWorkflowApi {
            workflow_id,
            cron_expression,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::schedule_workflow(workflow_id, &cron_expression).await {
                    Ok(json_str) => {
                        debug_log!(
                            "Workflow {} scheduled successfully: {}",
                            workflow_id, json_str
                        );
                        // Could dispatch success message / toast
                        crate::toast::success(&format!(
                            "Workflow scheduled with cron: {}",
                            cron_expression
                        ));
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to schedule workflow: {:?}", e).into(),
                        );
                        crate::toast::error("Failed to schedule workflow");
                    }
                }
            });
        }

        Command::UnscheduleWorkflowApi { workflow_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::unschedule_workflow(workflow_id).await {
                    Ok(_) => {
                        debug_log!(
                            "Workflow {} unscheduled successfully",
                            workflow_id
                        );
                        crate::toast::success("Workflow unscheduled successfully");
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to unschedule workflow: {:?}", e).into(),
                        );
                        crate::toast::error("Failed to unschedule workflow");
                    }
                }
            });
        }

        Command::CheckWorkflowScheduleApi { workflow_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_workflow_schedule(workflow_id).await {
                    Ok(json_str) => {
                        if let Ok(val) = serde_json::from_str::<serde_json::Value>(&json_str) {
                            if let Some(scheduled) = val.get("scheduled").and_then(|v| v.as_bool())
                            {
                                if scheduled {
                                    if let Some(next_run) =
                                        val.get("next_run_time").and_then(|v| v.as_str())
                                    {
                                        debug_log!(
                                            "Workflow {} is scheduled, next run: {}",
                                            workflow_id, next_run
                                        );
                                    }
                                } else {
                                    debug_log!(
                                        "Workflow {} is not scheduled",
                                        workflow_id
                                    );
                                }
                            }
                        }
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to check workflow schedule: {:?}", e).into(),
                        );
                    }
                }
            });
        }

        Command::FetchAgentDetails(agent_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_agent_details(agent_id).await {
                    Ok(json_str) => match serde_json::from_str::<ApiAgentDetails>(&json_str) {
                        Ok(details) => {
                            dispatch_global_message(Message::ReceiveAgentDetails(details))
                        }
                        Err(e) => web_sys::console::error_1(
                            &format!("Failed to parse agent details: {:?}", e).into(),
                        ),
                    },
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to fetch agent details: {:?}", e).into(),
                        );
                        // Could choose to dispatch HideAgentDebugModal or error state
                    }
                }
            });
        }
        Command::FetchAgentRuns(agent_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_agent_runs(agent_id, 20).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<Vec<crate::models::ApiAgentRun>>(&json_str) {
                            Ok(runs) => dispatch_global_message(Message::ReceiveAgentRuns {
                                agent_id,
                                runs,
                            }),
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse agent runs: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch agent runs: {:?}", e).into(),
                    ),
                }
            });
        }

        // -----------------------------------------------------------
        // Trigger helpers
        // -----------------------------------------------------------
        Command::FetchTriggers(agent_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_triggers(agent_id).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<Vec<crate::models::Trigger>>(&json_str) {
                            Ok(triggers) => {
                                dispatch_global_message(crate::messages::Message::TriggersLoaded {
                                    agent_id,
                                    triggers,
                                })
                            }
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse triggers: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch triggers: {:?}", e).into(),
                    ),
                }
            });
        }

        Command::CreateTrigger { payload_json } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::create_trigger(&payload_json).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<crate::models::Trigger>(&json_str) {
                            Ok(trigger) => {
                                let agent_id = trigger.agent_id;
                                dispatch_global_message(crate::messages::Message::TriggerCreated {
                                    agent_id,
                                    trigger,
                                });
                            }
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse new trigger: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to create trigger: {:?}", e).into(),
                    ),
                }
            });
        }

        Command::DeleteTrigger(trigger_id) => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::delete_trigger(trigger_id).await {
                    Ok(()) => dispatch_global_message(crate::messages::Message::TriggerDeleted {
                        agent_id: 0,
                        trigger_id,
                    }),
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to delete trigger: {:?}", e).into(),
                    ),
                }
            });
        }

        // Template Gallery Commands
        Command::LoadTemplatesApi {
            category,
            my_templates,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_templates(category.as_deref(), my_templates).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<Vec<crate::models::WorkflowTemplate>>(
                            &json_str,
                        ) {
                            Ok(templates) => {
                                dispatch_global_message(Message::TemplatesLoaded(templates))
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to parse templates: {:?}", e).into(),
                                );
                                crate::toast::error("Failed to load templates");
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        Command::LoadTemplateCategoriesApi => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_template_categories().await {
                    Ok(json_str) => match serde_json::from_str::<Vec<String>>(&json_str) {
                        Ok(categories) => {
                            dispatch_global_message(Message::TemplateCategoriesLoaded(categories))
                        }
                        Err(e) => {
                            web_sys::console::error_1(
                                &format!("Failed to parse template categories: {:?}", e).into(),
                            );
                            crate::toast::error("Failed to load template categories");
                        }
                    },
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        Command::DeployTemplateApi {
            template_id,
            name,
            description,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::deploy_template(
                    template_id,
                    name.as_deref(),
                    description.as_deref(),
                )
                .await
                {
                    Ok(json_str) => {
                        match serde_json::from_str::<crate::models::ApiWorkflow>(&json_str) {
                            Ok(api_wf) => {
                                let workflow: ApiWorkflow = api_wf;
                                dispatch_global_message(Message::TemplateDeployed(workflow));
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to parse deployed workflow: {:?}", e).into(),
                                );
                                crate::toast::error("Failed to process template deployment");
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }

        _ => web_sys::console::warn_1(&"Unexpected command type in execute_fetch_command".into()),
    }
}

pub fn execute_thread_command(cmd: Command) {
    match cmd {
        Command::CreateThread { agent_id, title } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::create_thread(agent_id, &title).await {
                    Ok(response) => match serde_json::from_str::<ApiThread>(&response) {
                        Ok(thread) => dispatch_global_message(Message::ThreadCreated(thread)),
                        Err(e) => web_sys::console::error_1(
                            &format!("Failed to parse created thread: {:?}", e).into(),
                        ),
                    },
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to create thread: {:?}", e).into(),
                    ),
                }
            });
        }
        Command::SendThreadMessage {
            thread_id,
            content,
            client_id,
        } => {
            debug_log!(
                "Executor: Handling Command::SendThreadMessage for thread {}: '{}'",
                thread_id, content
            );

            let _client_id_str = client_id.map(|id| id.to_string()).unwrap_or_default();

            // Minimal required flow: (1) create message, (2) trigger processing
            wasm_bindgen_futures::spawn_local(async move {
                // Step 1 – create the user message
                if let Err(e) = ApiClient::create_thread_message(thread_id, &content).await {
                    web_sys::console::error_1(
                        &format!(
                            "Executor: Failed to create message for thread {}: {:?}",
                            thread_id, e
                        )
                        .into(),
                    );
                    // Show error toast directly instead of using deprecated message
                    crate::toast::error("Failed to send message. Please try again.");
                    return;
                }

                // Step 2 – run the thread (process unprocessed messages)
                if let Err(e) = ApiClient::run_thread(thread_id).await {
                    web_sys::console::error_1(
                        &format!("Executor: Failed to run thread {}: {:?}", thread_id, e).into(),
                    );
                    // Show error toast directly instead of using deprecated message
                    crate::toast::error("Failed to process message. Please try again.");
                } else {
                    debug_log!("Executor: Processing started for thread {}", thread_id);
                }
            });
        }
        Command::RunThread(thread_id) => {
            // This command might now be redundant if SendThreadMessage handles everything.
            // If kept, it should ideally not be called right after SendThreadMessage.
            // Consider removing or repurposing this command.
            web_sys::console::warn_1(
                &format!(
                    "Executor: Handling potentially redundant Command::RunThread for thread {}",
                    thread_id
                )
                .into(),
            );
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::run_thread(thread_id).await {
                    Ok(response) => {
                        debug_log!(
                            "Executor: run_thread (manual) POST succeeded for thread {}: {}",
                            thread_id, response
                        );
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!(
                                "Executor: Failed to run thread {} (manual): {:?}",
                                thread_id, e
                            )
                            .into(),
                        );
                    }
                }
            });
        }
        Command::UpdateThreadTitle { thread_id, title } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::update_thread(thread_id, &title).await {
                    Ok(_) => debug_log!(
                        "Successfully updated thread title for {}",
                        thread_id
                    ),
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to update thread title: {:?}", e).into(),
                    ),
                }
            });
        }
        _ => web_sys::console::warn_1(&"Unexpected command type in execute_thread_command".into()),
    }
}

pub fn execute_network_command(cmd: Command) {
    match cmd {
        Command::NetworkCall {
            endpoint,
            method,
            body,
            on_success,
            on_error,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                // Ensure endpoint is absolute
                let endpoint_abs = if endpoint.starts_with("http") {
                    endpoint
                } else {
                    match crate::network::get_api_base_url() {
                        Ok(base) => format!("{}{}", base, endpoint),
                        Err(e) => {
                            web_sys::console::error_1(&format!("API base URL error: {}", e).into());
                            dispatch_global_message(*on_error);
                            return;
                        }
                    }
                };
                let result = match method.as_str() {
                    "GET" => ApiClient::fetch_json(&endpoint_abs, "GET", None).await,
                    "POST" => {
                        if let Some(data) = body {
                            ApiClient::fetch_json(&endpoint_abs, "POST", Some(&data)).await
                        } else {
                            Err("POST request requires body data".into())
                        }
                    }
                    "PUT" => {
                        if let Some(data) = body {
                            ApiClient::fetch_json(&endpoint_abs, "PUT", Some(&data)).await
                        } else {
                            Err("PUT request requires body data".into())
                        }
                    }
                    "DELETE" => ApiClient::fetch_json(&endpoint_abs, "DELETE", None).await,
                    _ => Err(format!("Unsupported HTTP method: {}", method).into()),
                };

                match result {
                    Ok(_) => dispatch_global_message(*on_success),
                    Err(e) => {
                        web_sys::console::error_1(&format!("Network call failed: {:?}", e).into());
                        dispatch_global_message(*on_error)
                    }
                }
            });
        }
        Command::UpdateAgent {
            agent_id,
            payload,
            on_success,
            on_error,
        } => {
            debug_log!(
                "Executor: Updating agent {} with payload: {}",
                agent_id, payload
            );

            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::update_agent(agent_id, &payload).await {
                    Ok(response) => {
                        debug_log!("Agent update successful: {}", response);

                        // 1. Notify the state layer that the update succeeded so it
                        //    can refresh the agent list (or optimistic cache).
                        dispatch_global_message(*on_success);

                        // 2. Close the configuration modal – this ensures the UI
                        //    only disappears **after** the server responded, fixing
                        //    race-conditions seen in Playwright tests and giving
                        //    users clear confirmation the save completed.
                        dispatch_global_message(Message::CloseAgentModal);
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to update agent {}: {:?}", agent_id, e).into(),
                        );
                        dispatch_global_message(*on_error);

                        // Re-enable the Save button so the user can retry.
                        if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                            if let Some(btn) = doc.get_element_by_id("save-agent") {
                                btn.set_inner_html("Save");
                                let _ = btn.remove_attribute("disabled");
                            }
                        }
                    }
                }
            });
        }
        Command::DeleteAgentApi { agent_id } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::delete_agent(agent_id).await {
                    Ok(_) => dispatch_global_message(Message::AgentDeletionSuccess { agent_id }),
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Delete agent API call failed: {:?}", e).into(),
                        );
                        dispatch_global_message(Message::AgentDeletionFailure {
                            agent_id,
                            error: format!("Failed to delete agent: {:?}", e),
                        })
                    }
                }
            });
        }
        _ => web_sys::console::warn_1(&"Unexpected command type in execute_network_command".into()),
    }
}

// ---------------------------------------------------------------------------
// Save-state executor – called by debounced SaveState command
// ---------------------------------------------------------------------------

pub fn execute_save_command() {
    // Borrow AppState immutably to serialise current state.  Persistence
    // helpers only need read-access.
    crate::state::APP_STATE.with(|state_ref| {
        let state = state_ref.borrow();
        // Persist viewport & node positions (localStorage for now)
        crate::storage::save_state_to_api(&state);
    });
}

pub fn execute_websocket_command(cmd: Command) {
    match cmd {
        Command::WebSocketAction {
            action,
            topic,
            data: _,
        } => {
            match action.as_str() {
                "subscribe" => {
                    if let Some(topic_str) = topic {
                        APP_STATE.with(|state| {
                            let state = state.borrow();
                            let topic_manager = state.topic_manager.clone();

                            // Clone topic_str before moving into closure
                            let topic_for_handler = topic_str.clone();

                            // Clone topic_str for potential error logging
                            let topic_for_error_log = topic_str.clone();

                            wasm_bindgen_futures::spawn_local(async move {
                                // Create a simple handler that logs the received messages
                                let handler =
                                    Rc::new(RefCell::new(move |json_value: serde_json::Value| {
                                        debug_log!(
                                            "WS: Received message for topic {}: {:?}",
                                            topic_for_handler, json_value
                                        );
                                    }));

                                // Subscribe using the topic manager
                                if let Err(e) =
                                    topic_manager.borrow_mut().subscribe(topic_str, handler)
                                {
                                    web_sys::console::error_1(
                                        &format!(
                                            "Failed to subscribe to topic {}: {:?}",
                                            topic_for_error_log, e
                                        )
                                        .into(),
                                    );
                                } else {
                                    // No need to clone here, topic_str wasn't moved in this path if subscribe succeeded
                                    debug_log!(
                                        "Successfully subscribed to topic {}",
                                        topic_for_error_log
                                    );
                                }
                            });
                        });
                    } else {
                        web_sys::console::error_1(&"Cannot subscribe without a topic".into());
                    }
                }
                _ => web_sys::console::warn_1(
                    &format!("Unsupported WebSocket action: {}", action).into(),
                ),
            }
        }
        _ => {
            web_sys::console::warn_1(&"Unexpected command type in execute_websocket_command".into())
        }
    }
}

pub fn execute_template_command(cmd: Command) {
    match cmd {
        Command::LoadTemplatesApi {
            category,
            my_templates,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_templates(category.as_deref(), my_templates).await {
                    Ok(json_str) => {
                        match serde_json::from_str::<Vec<crate::models::WorkflowTemplate>>(
                            &json_str,
                        ) {
                            Ok(templates) => {
                                dispatch_global_message(Message::TemplatesLoaded(templates))
                            }
                            Err(e) => web_sys::console::error_1(
                                &format!("Failed to parse templates: {:?}", e).into(),
                            ),
                        }
                    }
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch templates: {:?}", e).into(),
                    ),
                }
            });
        }
        Command::LoadTemplateCategoriesApi => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::get_template_categories().await {
                    Ok(json_str) => match serde_json::from_str::<Vec<String>>(&json_str) {
                        Ok(categories) => {
                            dispatch_global_message(Message::TemplateCategoriesLoaded(categories))
                        }
                        Err(e) => web_sys::console::error_1(
                            &format!("Failed to parse template categories: {:?}", e).into(),
                        ),
                    },
                    Err(e) => web_sys::console::error_1(
                        &format!("Failed to fetch template categories: {:?}", e).into(),
                    ),
                }
            });
        }
        Command::DeployTemplateApi {
            template_id,
            name,
            description,
        } => {
            wasm_bindgen_futures::spawn_local(async move {
                match ApiClient::deploy_template(
                    template_id,
                    name.as_deref(),
                    description.as_deref(),
                )
                .await
                {
                    Ok(json_str) => {
                        match serde_json::from_str::<crate::models::ApiWorkflow>(&json_str) {
                            Ok(api_wf) => {
                                let workflow: ApiWorkflow = api_wf;
                                dispatch_global_message(Message::TemplateDeployed(workflow));
                            }
                            Err(e) => {
                                web_sys::console::error_1(
                                    &format!("Failed to parse deployed workflow: {:?}", e).into(),
                                );
                                crate::toast::error("Failed to process template deployment");
                            }
                        }
                    }
                    Err(_) => {
                        // Error toast already shown by ApiClient::format_http_error
                    }
                }
            });
        }
        _ => {
            web_sys::console::warn_1(&"Unexpected command type in execute_template_command".into())
        }
    }
}
