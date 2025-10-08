// frontend/src/update.rs
//
use crate::components::chat_view::update_thread_list_ui;
use crate::dom_utils::{hide, set_active, set_inactive, show};
use crate::messages::{Command, Message};
use crate::models::{ApiThread, ApiThreadMessage};
use crate::state::AgentConfigTab;
use crate::state::{dispatch_global_message, AppState, APP_STATE};

// Conversion from generated UserUpdateData to CurrentUser
impl From<crate::generated::ws_messages::UserUpdateData> for crate::models::CurrentUser {
    fn from(data: crate::generated::ws_messages::UserUpdateData) -> Self {
        crate::models::CurrentUser {
            id: data.id,
            email: data.email.unwrap_or_default(),
            display_name: data.display_name,
            avatar_url: data.avatar_url,
            prefs: None,            // Not provided in WebSocket update
            gmail_connected: false, // Not provided in WebSocket update
        }
    }
}
use std::collections::HashMap;
use wasm_bindgen::JsValue;
use web_sys::Document;
use crate::debug_log;

// ---------------------------------------------------------------------------
// Internal helper – encapsulates all DOM + side-effects when the user switches
// between tabs inside the Agent Configuration modal.  Used by the unified
// `Message::SetAgentTab` handler.
// ---------------------------------------------------------------------------

pub fn handle_agent_tab_switch(
    state: &mut AppState,
    commands: &mut Vec<Command>,
    tab: AgentConfigTab,
) {
    state.agent_modal_tab = tab;

    let window = match web_sys::window() {
        Some(w) => w,
        None => return,
    };
    let document = match window.document() {
        Some(d) => d,
        None => return,
    };

    let by_id = |id: &str| document.get_element_by_id(id);

    // Content sections
    let main_c = by_id("agent-main-content");
    let hist_c = by_id("agent-history-content");
    let trg_c = by_id("agent-triggers-content");

    // Tab buttons
    let main_t = by_id("agent-main-tab");
    let hist_t = by_id("agent-history-tab");
    let trg_t = by_id("agent-triggers-tab");

    // Reset all visibility / active state first
    if let Some(el) = &main_c {
        hide(el);
    }
    if let Some(el) = &hist_c {
        hide(el);
    }
    if let Some(el) = &trg_c {
        hide(el);
    }

    if let Some(btn) = &main_t {
        set_inactive(btn);
    }
    if let Some(btn) = &hist_t {
        set_inactive(btn);
    }
    if let Some(btn) = &trg_t {
        set_inactive(btn);
    }

    // Content sections
    let tools_c = by_id("agent-tools-content");

    // Tab buttons
    let tools_t = by_id("agent-tools-tab");

    // Reset all visibility / active state first
    if let Some(el) = &tools_c {
        hide(el);
    }
    if let Some(btn) = &tools_t {
        set_inactive(btn);
    }

    // Activate selected tab
    match tab {
        AgentConfigTab::Main => {
            if let Some(el) = &main_c {
                show(el);
            }
            if let Some(btn) = &main_t {
                set_active(btn);
            }
        }

        AgentConfigTab::History => {
            if let Some(el) = &hist_c {
                show(el);
            }
            if let Some(btn) = &hist_t {
                set_active(btn);
            }
        }
        AgentConfigTab::Triggers => {
            if let Some(el) = &trg_c {
                show(el);
            }
            if let Some(btn) = &trg_t {
                set_active(btn);
            }
        }
        AgentConfigTab::ToolsIntegrations => {
            if let Some(el) = &tools_c {
                show(el);
            }
            if let Some(btn) = &tools_t {
                set_active(btn);
            }
        }
    }

    // When switching to Triggers we may need to (lazy) fetch triggers.
    if tab == AgentConfigTab::Triggers {
        let agent_id_opt = document
            .get_element_by_id("agent-modal")
            .and_then(|m| m.get_attribute("data-agent-id"))
            .and_then(|s| s.parse::<u32>().ok());

        if let Some(agent_id) = agent_id_opt {
            if !state.triggers.contains_key(&agent_id) {
                commands.push(Command::FetchTriggers(agent_id));
            }

            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::agent_config_modal::render_triggers_list(
                            &doc, agent_id,
                        );
                    }
                }
            })));
        }
    }

    // When switching to Tools tab, load MCP servers
    if tab == AgentConfigTab::ToolsIntegrations {
        let agent_id_opt = document
            .get_element_by_id("agent-modal")
            .and_then(|m| m.get_attribute("data-agent-id"))
            .and_then(|s| s.parse::<u32>().ok());

        if let Some(agent_id) = agent_id_opt {
            // Load MCP tools and servers for this agent
            commands.push(Command::SendMessage(Message::LoadMcpTools(agent_id)));

            // Render the MCP server manager UI
            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        if let Some(container) = doc.get_element_by_id("agent-tools-content") {
                            let mcp_manager =
                                crate::components::mcp_server_manager::MCPServerManager::new(
                                    agent_id,
                                );
                            let _ = mcp_manager.build_ui(&doc, &container);
                        }
                    }
                }
            })));
        }
    }
}

// Legacy helper trait fully removed; update.rs is decoupled and works with generated models.

pub fn update(state: &mut AppState, msg: Message) -> Vec<Command> {
    let mut needs_refresh = true; // We'll still track this internally for now
    let mut commands = Vec::new(); // Collect commands to return

    // ---------------------------------------------------------------
    // Delegate to domain-specific reducers first.  When one of them
    // consumes the message we can bail out early.
    // ---------------------------------------------------------------

    if crate::reducers::chat::update(state, &msg, &mut commands) {
        return commands;
    }
    if crate::reducers::canvas::update(state, &msg, &mut commands) {
        return commands;
    }
    if crate::reducers::ops::update(state, &msg, &mut commands) {
        return commands;
    }
    if crate::reducers::agent::update(state, &msg, &mut commands) {
        return commands;
    }
    if crate::reducers::dashboard::update(state, &msg, &mut commands) {
        return commands;
    }
    if crate::reducers::mcp::update(state, &msg, &mut commands) {
        return commands;
    }
    if crate::reducers::triggers::update(state, &msg, &mut commands) {
        return commands;
    }

    match msg {
        Message::SetPowerMode(enabled) => {
            state.power_mode = enabled;
            if let Some(window) = web_sys::window() {
                if let Some(doc) = window.document() {
                    if enabled {
                        crate::register_global_shortcuts(&doc);
                        crate::toast::info("Power Mode enabled: Keyboard shortcuts are active. Press '?' for help.");
                    } else {
                        crate::remove_global_shortcuts(&doc);
                        crate::toast::info("Power Mode disabled: Keyboard shortcuts are off.");
                    }
                }
            }
            return vec![];
        }
        // ---------------------------------------------------------------
        // Auth / profile handling
        // ---------------------------------------------------------------
        Message::CurrentUserLoaded(user) => {
            // Store a *clone* inside the state so that we can still access the
            // original `user` value below without running into move issues.
            state.current_user = Some(user.clone());
            state.logged_in = true;

            // Persist Gmail connection status so the Triggers tab can enable
            // the e-mail trigger option without requiring another network
            // call.
            state.gmail_connected = user.gmail_connected;

            // Trigger admin status check asynchronously
            wasm_bindgen_futures::spawn_local(async move {
                use crate::network::ApiClient;
                match ApiClient::get_super_admin_status().await {
                    Ok(response_json) => {
                        match serde_json::from_str::<crate::models::SuperAdminStatus>(&response_json) {
                            Ok(status) => {
                                dispatch_global_message(Message::AdminStatusLoaded {
                                    is_super_admin: status.is_super_admin,
                                    requires_password: status.requires_password,
                                });
                            }
                            Err(e) => {
                                web_sys::console::warn_1(&format!("Failed to parse admin status: {:?}", e).into());
                            }
                        }
                    }
                    Err(_) => {
                        // Not an admin or error - keep defaults (false)
                    }
                }
            });

            let user_for_ui = user.clone();
            // Mount / refresh user menu asynchronously after borrow ends.
            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::user_menu::mount_user_menu(&doc);

                        // ------- Update header greeting ------------------
                        if let Some(title_el) = doc.get_element_by_id("header-title") {
                            let greeting = if let Some(name) =
                                user_for_ui.display_name.as_ref().filter(|s| !s.is_empty())
                            {
                                format!("Welcome, {}!", name)
                            } else {
                                format!("Welcome, {}!", user_for_ui.email)
                            };
                            title_el.set_inner_html(&greeting);
                        }
                    }
                }
            })));

            // Subscribe to user:{id} topic for live updates.
            if let Some(user_id) = state.current_user.as_ref().map(|u| u.id) {
                let topic = format!("user:{}", user_id);
                let topic_manager_rc = state.topic_manager.clone();

                commands.push(Command::UpdateUI(Box::new(move || {
                    {
                        let mut tm = topic_manager_rc.borrow_mut();
                        // Prepare handler closure
                        use std::cell::RefCell;
                        use std::rc::Rc;
                        let handler: crate::network::topic_manager::TopicHandler =
                            Rc::new(RefCell::new(move |payload: serde_json::Value| {
                                // Extract message type and data from envelope-style payload
                                let message_type = payload
                                    .get("type")
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("unknown");
                                let message_data = payload.get("data").unwrap_or(&payload);

                                if message_type == "user_update" {
                                    if let Ok(user_data) = serde_json::from_value::<
                                        crate::generated::ws_messages::UserUpdateData,
                                    >(
                                        message_data.clone()
                                    ) {
                                        let profile: crate::models::CurrentUser = user_data.into();
                                        crate::state::dispatch_global_message(
                                            Message::CurrentUserLoaded(profile),
                                        );
                                        return;
                                    }
                                }
                            }));

                        let _ = tm.subscribe(topic, handler);
                    }
                })));
            }
        }

        Message::AdminStatusLoaded { is_super_admin, requires_password } => {
            state.is_super_admin = is_super_admin;
            state.admin_requires_password = requires_password;

            // Refresh dashboard to show/hide reset button based on admin status
            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(window) = web_sys::window() {
                    if let Some(document) = window.document() {
                        let _ = crate::components::dashboard::refresh_dashboard(&document);
                    }
                }
            })));
        }

        Message::ToggleView(view) => {
            let view_clone = view.clone();
            let view_clone_for_shelf = view.clone();

            state.active_view = view;
            state.state_modified = true;

            // Use a command to update the UI after this function returns
            // This avoids borrowing issues by executing after the current state borrow is released
            commands.push(Command::UpdateUI(Box::new(move || {
                // Get window and document without borrowing state
                if let Some(window) = web_sys::window() {
                    // Keep location.hash in sync for basic routing
                    let _ = match view_clone {
                        crate::storage::ActiveView::Profile => {
                            window.location().set_hash("#/profile")
                        }
                        crate::storage::ActiveView::AdminOps => {
                            window.location().set_hash("#/admin/ops")
                        }
                        crate::storage::ActiveView::Dashboard => {
                            window.location().set_hash("#/dashboard")
                        }
                        crate::storage::ActiveView::Canvas => {
                            window.location().set_hash("#/canvas")
                        }
                        crate::storage::ActiveView::ChatView => {
                            window.location().set_hash("#/chat")
                        }
                    };

                    if let Some(document) = window.document() {
                        // Directly call render_active_view_by_type without using refresh_ui_after_state_change
                        if let Err(e) =
                            crate::views::render_active_view_by_type(&view_clone, &document)
                        {
                            web_sys::console::error_1(
                                &format!("Failed to switch view: {:?}", e).into(),
                            );
                        }
                    }
                }
            })));

            // If switching to Canvas, always fetch agents and current workflow
            if view_clone_for_shelf == crate::storage::ActiveView::Canvas {
                commands.push(Command::FetchAgents);
                commands.push(Command::FetchCurrentWorkflow);
            }
        }

        Message::ResetDatabase => {
            // The actual database reset happens via API call (already done in dashboard.rs)
            // We don't need to do anything here because:
            // 1. The page will be refreshed immediately after this (in dashboard.rs)
            // 2. On refresh, it will automatically load the fresh state from the backend
            debug_log!("Reset database message received - state will refresh");
        }

        // Thread-related messages

        // Navigation messages

        // Force re-render of Dashboard when active

        // --- NEW WebSocket Received Messages ---

        // -----------------------------------------------------------------
        // AssistantId – arrives once per *token-mode* stream right after the
        // backend persisted the assistant row.  We store the id so upcoming
        // tool_output chunks can link to this bubble.  Additionally we patch
        // the last assistant message (if id is still None) so future UI
        // operations have the correct PK.
        // -----------------------------------------------------------------

        // Toggle collapse/expand state for a tool call indicator

        // Toggle full vs truncated tool output view for a tool call

        // --- NEW WebSocket Event Handlers ---
        Message::ReceiveAgentUpdate(agent_data) => {
            debug_log!(
                "Update handler: Received agent update: {:?}",
                agent_data
            );
            // TODO: Update agent list/details in AppState if needed
            // state.agents.insert(agent_data.id as u32, agent_data.into()); // Example update
            needs_refresh = true; // Assume agent list UI might need refresh
        }

        Message::ReceiveAgentDelete(agent_id) => {
            debug_log!("Update handler: Received agent delete: {}", agent_id);
            // TODO: Remove agent from AppState if needed
            // state.agents.remove(&(agent_id as u32)); // Example removal
            needs_refresh = true; // Assume agent list UI might need refresh
        }

        Message::ReceiveThreadHistory(messages) => {
            let system_messages_count = messages.iter().filter(|msg| msg.role == "system").count();
            if system_messages_count > 0 {
                debug_log!(
                    "Thread history contains {} system messages which won't be displayed in the chat UI",
                    system_messages_count
                );
            }
            debug_log!(
                "Update handler: Received thread history ({} messages, {} displayable)",
                messages.len(),
                messages.len() - system_messages_count
            );

            // Use agent-scoped state for thread messages
            if let Some(agent_state) = state.current_agent_mut() {
                if let Some(active_thread_id) = agent_state.current_thread_id {
                    // Store the received history messages in the agent's thread_messages
                    // Clone messages here before the insert
                    let messages_clone_for_dispatch = messages.clone();
                    agent_state
                        .thread_messages
                        .insert(active_thread_id, messages);

                    // Dispatch a message to update the UI instead of calling render directly
                    // This keeps the update flow consistent
                    commands.push(Command::UpdateUI(Box::new(move || {
                        dispatch_global_message(Message::UpdateConversation(
                            messages_clone_for_dispatch,
                        ));
                    })));

                    needs_refresh = false; // UI update handled by UpdateConversation
                } else {
                    web_sys::console::warn_1(
                        &"Received thread history but agent has no current thread selected.".into(),
                    );
                }
            } else {
                web_sys::console::warn_1(
                    &"Received thread history but no current agent selected in state.".into(),
                );
                needs_refresh = false;
            }
        }

        // Agent Debug Modal messages
        Message::UpdateLoadingState(is_loading) => {
            state.is_chat_loading = is_loading;

            // Update the UI
            if let Some(document) = web_sys::window()
                .expect("no global window exists")
                .document()
            {
                let _ = crate::components::chat_view::update_loading_state(&document, is_loading);
            }
        }

        Message::RequestThreadTitleUpdate => {
            // Get the current thread title from agent state
            let current_title = state
                .current_agent()
                .and_then(|agent| agent.current_thread_id)
                .and_then(|thread_id| state.current_agent()?.threads.get(&thread_id))
                .map(|thread| thread.title.clone())
                .unwrap_or_default();

            // Store the title for later use
            let title_to_update = current_title.clone();

            // Schedule UI update for after this function completes
            commands.push(Command::UpdateUI(Box::new(move || {
                // Dispatch a message to update the thread title UI
                dispatch_global_message(Message::UpdateThreadTitleUI(title_to_update));
            })));
        }

        // Agent Debug Modal

        // Model management
        Message::SetAvailableModels {
            models,
            default_model_id,
        } => {
            state.available_models = models.clone();
            state.default_model_id = default_model_id.clone();
            state.state_modified = true;
            needs_refresh = false; // No UI refresh needed for model updates
        }

        // -----------------------------------------------------------------
        // Trigger management (Phase A minimal state sync)
        // -----------------------------------------------------------------

        // -----------------------------------------------------------
        // Gmail OAuth flow – Phase C (frontend-only stub)
        // -----------------------------------------------------------

        // Toggle compact/full run history view

        // -------------------------------------------------------------------
        // Dashboard scope toggle (My ⇄ All)
        // -------------------------------------------------------------------
        // -------------------------------------------------------------------
        // MCP Integration Messages
        // -------------------------------------------------------------------

        // --- MCP UI message handlers ---

        // Canvas-related messages are handled by the canvas reducer
        Message::UpdateNodePosition { .. }
        | Message::AddNode { .. }
        | Message::AddResponseNode { .. }
        | Message::ToggleAutoFit
        | Message::CenterView
        | Message::ResetView
        | Message::ClearCanvas
        | Message::CanvasNodeClicked { .. }
        | Message::MarkCanvasDirty
        | Message::StartDragging { .. }
        | Message::StopDragging
        | Message::StartCanvasDrag { .. }
        | Message::UpdateCanvasDrag { .. }
        | Message::StopCanvasDrag
        | Message::ZoomCanvas { .. }
        | Message::AddCanvasNode { .. }
        | Message::DeleteNode { .. }
        | Message::UpdateNodeText { .. }
        | Message::CompleteNodeResponse { .. }
        | Message::UpdateNodeStatus { .. } => {
            // These are handled by the canvas reducer which returns early
            unreachable!("Canvas messages should be handled by the canvas reducer")
        }
        // (handled later)
        Message::AnimationTick => {
            // Fire async command to backend
            // These are handled elsewhere or in canvas reducer. No-op here.
        }

        // -------------------------------------------------------------------
        // SubscribeWorkflowExecution – create WS topic subscription
        // -------------------------------------------------------------------
        Message::SubscribeWorkflowExecution { execution_id } => {
            // Update state.current_execution with real id
            state.current_execution = Some(crate::state::ExecutionStatus {
                execution_id,
                status: crate::state::ExecPhase::Running,
            });

            // Refresh results panel for execution start via command
            commands.push(Command::UpdateUI(Box::new(|| {
                // (results panel removed)
            })));

            let topic = format!("workflow_execution:{}", execution_id);
            let topic_manager_rc = state.topic_manager.clone();

            commands.push(Command::UpdateUI(Box::new(move || {
                use std::cell::RefCell;
                use std::rc::Rc;
                let mut tm = topic_manager_rc.borrow_mut();

                let handler: crate::network::topic_manager::TopicHandler =
                    Rc::new(RefCell::new(move |payload: serde_json::Value| {
                        // Extract message type and data from envelope-style payload
                        let message_type = payload
                            .get("type")
                            .and_then(|v| v.as_str())
                            .unwrap_or("unknown");
                        let message_data = payload.get("data").unwrap_or(&payload);

                        if message_type == "node_state" {
                            if let Ok(data) = serde_json::from_value::<
                                crate::generated::ws_messages::NodeStateData,
                            >(message_data.clone())
                            {
                                crate::state::dispatch_global_message(Message::UpdateNodeStatus {
                                    node_id: data.node_id.clone(),
                                    phase: data.phase.clone(),
                                    result: data.result.clone(),
                                });
                            }
                        } else if message_type == "execution_finished" {
                            if let Ok(data) = serde_json::from_value::<
                                crate::generated::ws_messages::ExecutionFinishedData,
                            >(message_data.clone())
                            {
                                crate::state::dispatch_global_message(Message::ExecutionFinished {
                                    execution_id: data.execution_id,
                                    phase: "finished".to_string(),
                                    result: data.result.clone(),
                                    error: data.error_message.clone(),
                                });
                            }
                        } else if message_type == "node_log" {
                            if let Ok(data) = serde_json::from_value::<
                                crate::generated::ws_messages::NodeLogData,
                            >(message_data.clone())
                            {
                                crate::state::dispatch_global_message(
                                    Message::AppendExecutionLog {
                                        execution_id: data.execution_id,
                                        node_id: data.node_id.clone(),
                                        stream: data.stream.clone(),
                                        text: data.text.clone(),
                                    },
                                );
                            }
                        }
                    }));

                let _ = tm.subscribe(topic.clone(), handler);
            })));

            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::update_run_button(&doc);
                }
            })));
        }

        // -------------------------------------------------------------------
        // Start workflow execution – triggers API call command
        // -------------------------------------------------------------------
        Message::StartWorkflowExecution { workflow_id } => {
            state.current_execution = Some(crate::state::ExecutionStatus {
                execution_id: 0,
                status: crate::state::ExecPhase::Starting,
            });
            state.execution_logs.clear();

            // Use the new reserve-first approach to avoid race conditions
            commands.push(Command::ReserveWorkflowExecutionApi { workflow_id });

            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::update_run_button(&doc);
                }
            })));

            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::log_drawer::refresh(&doc);
                }
            })));
        }

        // -------------------------------------------------------------------
        // Reserve workflow execution – get execution ID without starting
        // -------------------------------------------------------------------
        Message::ReserveWorkflowExecution { workflow_id } => {
            // This message is not used directly, as the reserve operation
            // is handled by the ReserveWorkflowExecutionApi command
            commands.push(Command::ReserveWorkflowExecutionApi { workflow_id });
        }

        // -------------------------------------------------------------------
        // Start reserved execution – begin execution of previously reserved ID
        // -------------------------------------------------------------------
        Message::StartReservedExecution { execution_id } => {
            // Update the execution status to running
            if let Some(exec) = &mut state.current_execution {
                exec.execution_id = execution_id;
                exec.status = crate::state::ExecPhase::Running;
            }

            commands.push(Command::StartReservedExecutionApi { execution_id });

            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::update_run_button(&doc);
                }
            })));
        }
        Message::CreateWorkflow { name } => {
            state.creating_workflow = true;
            commands.push(Command::CreateWorkflowApi { name: name.clone() });
        }

        Message::RenameWorkflow {
            workflow_id,
            name,
            description,
        } => {
            state.updating_workflow = Some(workflow_id);
            commands.push(Command::RenameWorkflowApi {
                workflow_id,
                name: name.clone(),
                description: description.clone(),
            });
        }

        Message::DeleteWorkflow { workflow_id } => {
            state.deleting_workflow = Some(workflow_id);
            commands.push(Command::DeleteWorkflowApi { workflow_id });
        }

        // Loading state handlers
        Message::WorkflowCreationStarted => {
            state.creating_workflow = true;
        }

        Message::WorkflowDeletionStarted { workflow_id } => {
            state.deleting_workflow = Some(workflow_id);
        }

        Message::WorkflowUpdateStarted { workflow_id } => {
            state.updating_workflow = Some(workflow_id);
        }

        // Workflow scheduling messages
        Message::ScheduleWorkflow {
            workflow_id,
            cron_expression,
        } => {
            commands.push(Command::ScheduleWorkflowApi {
                workflow_id,
                cron_expression,
            });
        }

        Message::UnscheduleWorkflow { workflow_id } => {
            commands.push(Command::UnscheduleWorkflowApi { workflow_id });
        }

        Message::CheckWorkflowSchedule { workflow_id } => {
            commands.push(Command::CheckWorkflowScheduleApi { workflow_id });
        }
        // Message::SelectWorkflow is handled in reducers/canvas.rs to keep
        // canvas/workflow state mutations in one place. UI updates are pushed
        // from there as needed.
        Message::WorkflowCreated(wf) => {
            // Remove any optimistic entry with same name or temp id (<=0)
            let wf_id = wf.id;
            state.workflows.retain(|_, v| v.id > 0 || v.name != wf.name);
            state.workflows.insert(wf_id, wf);
            state.current_workflow_id = Some(wf_id);
            state.creating_workflow = false; // Clear loading state
            needs_refresh = true;
            // UI update to refresh bar after new workflow added
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::refresh(&doc);
                }
            })));
        }

        Message::CurrentWorkflowLoaded(wf) => {
            let wf_id = wf.id;
            state.workflows.insert(wf_id, wf.clone());
            state.current_workflow_id = Some(wf_id);

            // CARMACK PRINCIPLE: Workflow IS the single source of truth
            // Canvas state is completely rebuilt from workflow - no merging, no confusion

            // 1. Clear canvas state - workflow will repopulate it
            state.workflow_nodes.clear();
            state.agents_on_canvas.clear();
            state.agent_id_to_node_id.clear();

            // 2. Rebuild canvas from workflow (single source of truth)
            //    Also deduplicate stray duplicate Manual triggers that may
            //    exist due to older saves.
            let mut seen_manual_trigger = false;
            let nodes = wf.get_nodes();
            for mut node in nodes {
                let is_dupe_manual = match node.get_semantic_type() {
                    crate::models::NodeType::Trigger { trigger_type, .. } => {
                        if matches!(trigger_type, crate::models::TriggerType::Manual) {
                            if seen_manual_trigger {
                                true
                            } else {
                                seen_manual_trigger = true;
                                false
                            }
                        } else {
                            false
                        }
                    }
                    _ => false,
                };

                if is_dupe_manual {
                    debug_log!(
                        "⚠️ Skipping duplicate Manual trigger node {} during rebuild",
                        node.node_id
                    );
                    continue;
                }

                state
                    .workflow_nodes
                    .insert(node.node_id.clone(), node.clone());

                // Rebuild derived state from workflow data
                if let Some(agent_id) = node.get_agent_id() {
                    if matches!(
                        node.get_semantic_type(),
                        crate::models::NodeType::AgentIdentity
                    ) {
                        state.agents_on_canvas.insert(agent_id);
                        state
                            .agent_id_to_node_id
                            .insert(agent_id, node.node_id.clone());
                    }
                }
            }

            // 3. Backend now provides complete workflows with triggers via templates
            // No client-side trigger creation needed - workflows come pre-populated

            // 4. Edges are already in workflow - no duplication needed
            // The renderer reads directly from workflow.edges

            needs_refresh = true;
            debug_log!(
                "🎨 Rebuilt canvas from workflow '{}': {} nodes + {} edges",
                wf.name,
                state.workflow_nodes.len(),
                wf.get_edges().len()
            );
        }

        // (ExecutionFinished & AppendExecutionLog handled in dedicated arms
        // below.  Any UI refresh logic is appended there to avoid duplicate
        // pattern matches that caused unreachable-pattern warnings.)
        Message::WorkflowDeleted { workflow_id } => {
            state.workflows.remove(&workflow_id);
            if state.current_workflow_id == Some(workflow_id) {
                state.current_workflow_id = state.workflows.keys().next().cloned();
            }
            state.deleting_workflow = None; // Clear loading state
            needs_refresh = true;
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::refresh(&doc);
                }
            })));
        }
        Message::WorkflowUpdated(wf) => {
            state.workflows.insert(wf.id, wf.clone());
            state.updating_workflow = None; // Clear loading state
            needs_refresh = true;
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::refresh(&doc);
                }
            })));
        }

        // -------------------------------------------------------------------
        // Live workflow execution updates
        // -------------------------------------------------------------------
        Message::ExecutionFinished {
            execution_id,
            phase: _,
            result,
            error,
        } => {
            // Show toast notification for completion
            let status_msg = if result == "success" {
                "Workflow completed successfully"
            } else {
                "Workflow execution failed"
            };

            if result == "success" {
                crate::toast::success(status_msg);
            } else {
                crate::toast::error(status_msg);
            }

            // Status feedback is handled purely by toast notifications
            // No UI elements are added/modified to avoid layout shifts

            // Update execution status for internal tracking, but don't affect run button
            if let Some(exec) = &mut state.current_execution {
                if exec.execution_id == 0 || exec.execution_id == execution_id {
                    exec.execution_id = execution_id;
                    exec.status = if result == "success" {
                        crate::state::ExecPhase::Success
                    } else {
                        crate::state::ExecPhase::Failed
                    };
                }
            }
            if let Some(err) = error {
                state.execution_logs.push(crate::state::ExecutionLog {
                    node_id: "execution".to_string(),
                    stream: "error".to_string(),
                    text: format!("Execution failed: {}", err),
                });
            }
            needs_refresh = true;
        }

        Message::AppendExecutionLog {
            execution_id,
            node_id,
            stream,
            text,
        } => {
            if let Some(exec) = &state.current_execution {
                if exec.execution_id == 0 || exec.execution_id == execution_id {
                    state.execution_logs.push(crate::state::ExecutionLog {
                        node_id: node_id.clone(),
                        stream: stream.clone(),
                        text: text.clone(),
                    });
                    needs_refresh = true;
                }
            }

            // Refresh results panel for log updates via command
            // Removed: let _ = crate::components::execution_results_panel::refresh_results_panel();

            // Live append means drawer needs repaint if open
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::log_drawer::refresh(&doc);
                }
            })));
        }

        // UI toggle for log drawer
        Message::ToggleLogDrawer => {
            state.logs_open = !state.logs_open;
            needs_refresh = true;
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::log_drawer::refresh(&doc);
                }
            })));
        }

        // -------------------------------------------------------------------
        // Template Gallery Messages
        // -------------------------------------------------------------------
        Message::LoadTemplates {
            category,
            my_templates,
        } => {
            state.templates_loading = true;
            state.selected_template_category = category.clone();
            state.show_my_templates_only = my_templates;
            commands.push(Command::LoadTemplatesApi {
                category: category.clone(),
                my_templates,
            });
        }

        Message::TemplatesLoaded(templates) => {
            state.templates = templates.clone();
            state.templates_loading = false;
            needs_refresh = true;
            // Refresh template gallery if it's open
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::template_gallery::refresh_templates_grid(&doc);
                }
            })));
        }

        Message::LoadTemplateCategories => {
            commands.push(Command::LoadTemplateCategoriesApi);
        }

        Message::TemplateCategoriesLoaded(categories) => {
            state.template_categories = categories.clone();
            needs_refresh = true;
            // Refresh template gallery if it's open
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::template_gallery::refresh_templates_grid(&doc);
                }
            })));
        }

        Message::SetTemplateCategory(category) => {
            state.selected_template_category = category.clone();
            // Reload templates with new category filter
            commands.push(Command::LoadTemplatesApi {
                category: category.clone(),
                my_templates: state.show_my_templates_only,
            });
        }

        Message::ToggleMyTemplatesOnly => {
            state.show_my_templates_only = !state.show_my_templates_only;
            // Reload templates with new filter
            commands.push(Command::LoadTemplatesApi {
                category: state.selected_template_category.clone(),
                my_templates: state.show_my_templates_only,
            });
        }

        Message::DeployTemplate {
            template_id,
            name,
            description,
        } => {
            commands.push(Command::DeployTemplateApi {
                template_id,
                name: Some(name.clone()),
                description: Some(description.clone()),
            });
        }

        Message::TemplateDeployed(workflow) => {
            // Add the new workflow to state
            state.workflows.insert(workflow.id, workflow.clone());
            state.current_workflow_id = Some(workflow.id);
            needs_refresh = true;
            // Show success toast and refresh UI
            crate::toast::success("Template deployed successfully!");
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::refresh(&doc);
                }
            })));
        }

        Message::ShowTemplateGallery => {
            // Load templates and categories when showing gallery
            commands.push(Command::LoadTemplatesApi {
                category: state.selected_template_category.clone(),
                my_templates: state.show_my_templates_only,
            });
            commands.push(Command::LoadTemplateCategoriesApi);
        }

        Message::HideTemplateGallery => {
            // No action needed - gallery will be hidden by UI component
        }
        Message::WorkflowsLoaded(workflows) => {
            state.workflows.clear();
            for wf in workflows {
                state.workflows.insert(wf.id, wf);
            }
            // Ensure current_workflow_id is set
            if state.current_workflow_id.is_none() {
                if let Some(first_id) = state.workflows.keys().next().cloned() {
                    state.current_workflow_id = Some(first_id);
                }
            }
            // Trigger UI refresh of workflow switcher
            commands.push(Command::UpdateUI(Box::new(|| {
                if let (Some(win),) = (web_sys::window(),) {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::workflow_switcher::refresh(&doc);
                    }
                }
            })));
        }
        Message::AddEdge { .. }
        | Message::ToggleConnectionMode
        | Message::SelectNodeForConnection { .. }
        | Message::CreateConnectionFromSelected { .. }
        | Message::ClearNodeSelection
        | Message::StartConnectionDrag { .. }
        | Message::UpdateConnectionDrag { .. }
        | Message::EndConnectionDrag { .. }
        | Message::GenerateCanvasFromAgents => {
            // These are handled by the canvas reducer which returns early
            unreachable!("Canvas messages should be handled by the canvas reducer")
        }

        Message::InitializeParticleSystem { width, height } => {
            state.particle_system = Some(crate::canvas::background::ParticleSystem::new(
                width, height, 50,
            ));
            needs_refresh = true; // Particle system initialized, likely needs a redraw
        }
        Message::ClearParticleSystem => {
            state.particle_system = None;
            needs_refresh = true; // Particle system cleared, likely needs a redraw
        }
        Message::UpdateViewport { x, y, zoom } => {
            state.viewport_x = x;
            state.viewport_y = y;
            state.zoom_level = zoom;
            needs_refresh = true; // Viewport changes require redraw
        }
        Message::UpdateWorkflowNodes(nodes) => {
            state.workflow_nodes = nodes;
            needs_refresh = true; // Node changes require redraw
        }
        Message::SetDataLoaded(loaded) => {
            state.data_loaded = loaded;
            needs_refresh = false; // Data loaded flag changes don't require UI refresh
        }
        Message::ClearAgents => {
            state.agents.clear();
            needs_refresh = true; // Agent changes may affect UI
        }
        Message::AddAgents(agents) => {
            for agent in agents {
                if let Some(id) = agent.id {
                    state.agents.insert(id, agent);
                }
            }
            needs_refresh = true; // Agent changes may affect UI
        }
        Message::SetGoogleClientId(client_id) => {
            state.google_client_id = client_id;
            needs_refresh = false; // Client ID changes don't require UI refresh
        }
        // Catch-all for any unhandled messages
        _ => {
            // Warn about unexpected messages so nothing silently fails
            web_sys::console::warn_1(
                &format!("[update.rs] Unexpected or unhandled message: {:?}", msg).into(),
            );
            needs_refresh = false;
        }
    }

    // -------------------------------------------------------------------
    // After handling the message: if any mutation occurred mark timestamp.
    // This centralised check guarantees `last_modified_ms` is always kept
    // in sync with the `state_modified` flag without sprinkling
    // `utils::now_ms()` across every reducer arm.
    // -------------------------------------------------------------------
    if state.state_modified {
        state.last_modified_ms = crate::utils::now_ms();
    }

    // If needs_refresh is true, update UI components
    if needs_refresh {
        commands.push(Command::UpdateUI(Box::new(|| {
            if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                let _ = crate::components::workflow_switcher::update_run_button(&doc);
            }
        })));
    }

    commands
}

// Update thread list UI
#[allow(dead_code)]
pub fn update_thread_list(document: &Document) -> Result<(), JsValue> {
    APP_STATE.with(|state| {
        let state = state.borrow();
        if let Some(agent_state) = state.current_agent() {
            let threads: Vec<ApiThread> = agent_state.threads.values().cloned().collect();
            let current_thread_id = agent_state.current_thread_id;
            let thread_messages = agent_state.thread_messages.clone();
            update_thread_list_ui(document, &threads, current_thread_id, &thread_messages)
        } else {
            // No current agent, show empty thread list
            update_thread_list_ui(document, &[], None, &HashMap::new())
        }
    })
}

// A version of update_thread_list that accepts data directly instead of accessing APP_STATE
#[allow(dead_code)]
pub fn update_thread_list_with_data(
    document: &Document,
    threads: &[ApiThread],
    current_thread_id: Option<u32>,
    thread_messages: &HashMap<u32, Vec<ApiThreadMessage>>,
) -> Result<(), JsValue> {
    update_thread_list_ui(document, threads, current_thread_id, thread_messages)
}

// Update conversation UI
#[allow(dead_code)]
pub fn update_conversation(document: &Document) -> Result<(), JsValue> {
    APP_STATE.with(|state| {
        let state = state.borrow();
        if let Some(agent_state) = state.current_agent() {
            if let Some(thread_id) = agent_state.current_thread_id {
                if let Some(messages) = agent_state.thread_messages.get(&thread_id) {
                    let current_user_opt = state.current_user.clone();
                    return crate::components::chat_view::update_conversation_ui(
                        document,
                        messages,
                        current_user_opt.as_ref(),
                    );
                }
            }
        }

        // (Removed misplaced match arm – correct implementation lives inside
        // the main `update()` reducer near other dashboard-related logic.)
        Ok(())
    })
}
