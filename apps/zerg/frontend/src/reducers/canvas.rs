//! Canvas/workflow reducer split out from `update.rs`.
//!
//! Handles all canvas, node, edge, drag, zoom, and workflow logic.

use crate::messages::{Command, Message};
use crate::models::{NodeType, UiNodeState};
use crate::state::AppState;
use crate::debug_log;

pub fn update(state: &mut AppState, msg: &Message, cmds: &mut Vec<Command>) -> bool {
    match msg {
        Message::SaveToolConfig { node_id, config } => {
            if let Some(node) = state.workflow_nodes.get_mut(node_id) {
                if matches!(
                    node.get_semantic_type(),
                    crate::models::NodeType::Tool { .. }
                ) {
                    // Store the tool config in the node's config field
                    node.config.tool_config =
                        Some(serde_json::to_value(config).unwrap_or_default());
                    state.state_modified = true;
                    state.mark_dirty();
                }
            }
            true
        }
        Message::UpdateTriggerNodeConfig { node_id, params } => {
            if let Some(node) = state.workflow_nodes.get_mut(node_id) {
                if let crate::models::NodeType::Trigger { trigger_type, config } =
                    node.get_semantic_type()
                {
                    // Update typed trigger params first
                    let mut new_cfg = config.clone();
                    if let serde_json::Value::Object(param_map) = params {
                        for (k, v) in param_map {
                            new_cfg.params.insert(k.clone(), v.clone());
                        }
                    }

                    // Re-set semantic type to persist typed meta (canonical only)
                    let new_semantic = crate::models::NodeType::Trigger {
                        trigger_type,
                        config: new_cfg,
                    };
                    node.set_semantic_type(&new_semantic);

                    state.state_modified = true;
                    state.mark_dirty();
                }
            }
            true
        }
        Message::UpdateNodePosition { node_id, x, y } => {
            state.update_node_position(node_id, *x, *y);
            // Always mark dirty for visual updates during dragging
            state.mark_dirty();
            // Only mark state as modified (triggering API saves) if not currently dragging
            // This prevents spam of PATCH requests during drag operations for all node types
            if state.ui_state.get(node_id).map_or(false, |s| s.is_dragging) {
                state.state_modified = true;
            }
            true
        }
        Message::AddNode {
            text,
            x,
            y,
            node_type,
        } => {
            // Enforce invariant: only one Manual trigger per workflow
            if let NodeType::Trigger { trigger_type, .. } = node_type {
                if matches!(trigger_type, crate::models::TriggerType::Manual) {
                    let already_has_manual = state
                        .workflow_nodes
                        .values()
                        .any(|n| matches!(n.get_semantic_type(), NodeType::Trigger { trigger_type: crate::models::TriggerType::Manual, .. }));
                    if already_has_manual {
                        crate::toast::error("Only one Manual trigger allowed per workflow");
                        return true;
                    }
                }
            }
            if *node_type == NodeType::AgentIdentity && *x == 0.0 && *y == 0.0 {
                let viewport_width = if state.canvas_width > 0.0 {
                    state.canvas_width
                } else {
                    800.0
                };
                let viewport_height = if state.canvas_height > 0.0 {
                    state.canvas_height
                } else {
                    600.0
                };
                let x = state.viewport_x + (viewport_width / state.zoom_level) / 2.0 - 75.0;
                let y = state.viewport_y + (viewport_height / state.zoom_level) / 2.0 - 50.0;
                let node_id = state.add_node(text.clone(), x, y, node_type.clone());
                debug_log!("Created visual node for agent: {}", node_id);
            } else {
                let _ = state.add_node(text.clone(), *x, *y, node_type.clone());
            }
            state.state_modified = true;
            true
        }
        Message::AddResponseNode {
            parent_id,
            response_text,
        } => {
            let _ = state.add_response_node(parent_id, response_text.clone());
            state.state_modified = true;
            true
        }
        Message::ToggleAutoFit => {
            state.toggle_auto_fit();
            state.state_modified = true;
            true
        }
        Message::CenterView => {
            state.center_view();
            state.state_modified = true;
            true
        }
        Message::ResetView => {
            state.reset_view();
            state.state_modified = true;
            true
        }
        Message::ClearCanvas => {
            state.workflow_nodes.clear();
            state.ui_state.clear();
            state.latest_user_input_id = None;
            state.message_id_to_node_id.clear();
            state.viewport_x = 0.0;
            state.viewport_y = 0.0;
            state.zoom_level = 1.0;
            state.auto_fit = true;
            state.state_modified = true;
            true
        }
        Message::ShowToolConfigModal { node_id } => {
            if let Some(node) = state.workflow_nodes.get(node_id) {
                if let NodeType::Tool {
                    tool_name,
                    server_name,
                    ..
                } = node.get_semantic_type()
                {
                    let _description = state
                        .available_mcp_tools
                        .values()
                        .flat_map(|tools| tools.iter())
                        .find(|t| t.name == tool_name && t.server_name == server_name)
                        .and_then(|t| t.description.clone())
                        .unwrap_or_else(|| "No description available.".to_string());

                    if let Some(window) = web_sys::window() {
                        if let Some(document) = window.document() {
                            let _ = crate::components::tool_config_modal::ToolConfigModal::open(
                                &document, node,
                            );
                        }
                    }
                }
            }
            true
        }
        Message::ShowTriggerConfigModal { node_id } => {
            if let Some(node) = state.workflow_nodes.get(node_id) {
                if matches!(node.get_semantic_type(), NodeType::Trigger { .. }) {
                    if let Some(window) = web_sys::window() {
                        if let Some(document) = window.document() {
                            let _ =
                                crate::components::trigger_config_modal::TriggerConfigModal::open(
                                    &document, node,
                                );
                        }
                    }
                }
            }
            true
        }
        Message::CanvasNodeClicked { node_id } => {
            if state.connection_mode {
                // Handle connection mode clicks
                if let Some(source_node_id) = &state.connection_source_node {
                    if source_node_id != node_id {
                        // Create connection from source to clicked node
                        cmds.push(Command::SendMessage(
                            Message::CreateConnectionFromSelected {
                                target_node_id: node_id.clone(),
                            },
                        ));
                    }
                } else {
                    // Select this node as connection source
                    cmds.push(Command::SendMessage(Message::SelectNodeForConnection {
                        node_id: node_id.clone(),
                    }));
                }
            } else {
                // Normal click behavior - open agent config
                if let Some(agent_id) = state
                    .workflow_nodes
                    .get(node_id)
                    .and_then(|n| n.config.agent_id)
                {
                    cmds.push(Command::SendMessage(Message::EditAgent(agent_id)));
                }
            }
            true
        }
        Message::MarkCanvasDirty => {
            state.mark_dirty();
            true
        }
        Message::StartDragging {
            node_id,
            offset_x,
            offset_y,
            start_x,
            start_y,
            is_agent: _,
        } => {
            if let Some(ui_node_state) = state.ui_state.get_mut(node_id) {
                ui_node_state.is_dragging = true;
            }
            state.dragging = Some(node_id.clone());
            state.drag_offset_x = *offset_x;
            state.drag_offset_y = *offset_y;
            state.drag_start_x = *start_x;
            state.drag_start_y = *start_y;
            true
        }
        Message::StopDragging => {
            if let Some(node_id) = state.dragging.take() {
                if let Some(ui_node_state) = state.ui_state.get_mut(&node_id) {
                    ui_node_state.is_dragging = false;
                }
            }
            state.state_modified = true;
            let _ = state.save_if_modified();
            true
        }
        Message::StartCanvasDrag { start_x, start_y } => {
            state.canvas_dragging = true;
            state.drag_start_x = *start_x;
            state.drag_start_y = *start_y;
            state.drag_last_x = *start_x;
            state.drag_last_y = *start_y;
            true
        }
        Message::UpdateCanvasDrag {
            current_x,
            current_y,
        } => {
            if state.canvas_dragging {
                let dx = *current_x - state.drag_last_x;
                let dy = *current_y - state.drag_last_y;
                let zoom = state.zoom_level;
                state.viewport_x -= dx / zoom;
                state.viewport_y -= dy / zoom;
                state.drag_last_x = *current_x;
                state.drag_last_y = *current_y;
                state.state_modified = true;
                state.mark_dirty();
            }
            true
        }
        Message::StopCanvasDrag => {
            state.canvas_dragging = false;
            state.state_modified = true;
            let _ = state.save_if_modified();
            true
        }
        Message::ZoomCanvas {
            new_zoom,
            viewport_x,
            viewport_y,
        } => {
            state.zoom_level = *new_zoom;
            state.clamp_zoom();
            state.viewport_x = *viewport_x;
            state.viewport_y = *viewport_y;
            state.state_modified = true;
            state.mark_dirty();
            true
        }
        Message::AddCanvasNode {
            agent_id,
            x,
            y,
            node_type,
            text,
        } => {
            let node_id =
                state.add_node_with_agent(*agent_id, *x, *y, node_type.clone(), text.clone());
            debug_log!("Created new node: {}", node_id);
            state.state_modified = true;

            true
        }
        Message::DeleteNode { node_id } => {
            // Clean up agent tracking before removing the node
            if let Some(node) = state.workflow_nodes.get(node_id) {
                let agent_id = node.config.agent_id;
                if let Some(agent_id) = agent_id {
                    state.agent_id_to_node_id.remove(&agent_id);
                    state.agents_on_canvas.remove(&agent_id);
                }
            }

            // Remove the node
            state.workflow_nodes.remove(node_id);
            state.ui_state.remove(node_id);

            // Also clean up from workflow
            if let Some(workflow_id) = state.current_workflow_id {
                if let Some(workflow) = state.workflows.get_mut(&workflow_id) {
                    workflow.remove_node(node_id);
                }
            }

            state.state_modified = true;
            state.mark_dirty();
            true
        }
        Message::UpdateNodeText {
            node_id,
            text,
            is_first_chunk,
        } => {
            if let Some(node) = state.workflow_nodes.get_mut(node_id) {
                let current_text = &node.config.text;
                let new_text = if *is_first_chunk {
                    text.clone()
                } else {
                    current_text.clone() + text
                };
                node.config.text = new_text;
                state.resize_node_for_content(node_id);
                state.state_modified = true;
            }
            true
        }
        Message::CompleteNodeResponse {
            node_id,
            final_text,
        } => {
            if let Some(node) = state.workflow_nodes.get_mut(node_id) {
                if !final_text.is_empty() {
                    node.config.text = final_text.clone();
                }
                node.config.color = "#c8e6c9".to_string();
                let agent_id = node.config.agent_id;
                if let Some(agent_id) = agent_id {
                    if let Some(agent) = state.agents.get_mut(&agent_id) {
                        agent.status = Some("idle".to_string());
                    }
                }
                let parent_id = node.config.parent_id.clone();
                state.state_modified = true;
                state.resize_node_for_content(node_id);
                if let Some(parent_id) = parent_id {
                    if let Some(parent) = state.workflow_nodes.get_mut(&parent_id) {
                        parent.config.color = "#ffecb3".to_string();
                        let parent_agent_id = parent.config.agent_id;
                        if let Some(agent_id) = parent_agent_id {
                            if let Some(agent) = state.agents.get_mut(&agent_id) {
                                agent.status = Some("idle".to_string());
                            }
                        }
                    }
                }
            }
            true
        }
        Message::UpdateNodeStatus { node_id, phase, result } => {
            if let Some(node) = state.workflow_nodes.get_mut(node_id) {
                use crate::models::{NodeExecStatus, Phase, ExecutionResult};

                // Convert phase/result to NodeExecStatus using the From implementation
                let phase_enum = match phase.as_str() {
                    "waiting" => Phase::Waiting,
                    "running" => Phase::Running,
                    "finished" => Phase::Finished,
                    _ => Phase::Waiting,
                };

                let result_enum = result.as_ref().and_then(|r| match r.as_str() {
                    "success" => Some(ExecutionResult::Success),
                    "failure" => Some(ExecutionResult::Failure),
                    "cancelled" => Some(ExecutionResult::Cancelled),
                    _ => None,
                });

                let exec_status: NodeExecStatus = (phase_enum, result_enum).into();

                let color = match exec_status {
                    NodeExecStatus::Waiting => "#e0e7ff",      // indigo-100
                    NodeExecStatus::Running => "#fcd34d",      // amber-300
                    NodeExecStatus::Completed => "#86efac",    // green-300
                    NodeExecStatus::Failed => "#fca5a5",       // red-300
                };

                node.config.color = color.to_string();
                if let Some(ui_node_state) = state.ui_state.get_mut(node_id) {
                    ui_node_state.exec_status = Some(exec_status);
                }

                // Start transition animations for success/error states
                match exec_status {
                    NodeExecStatus::Completed => {
                        let now = js_sys::Date::now();
                        if let Some(ui_node_state) = state.ui_state.get_mut(node_id) {
                            ui_node_state.transition_animation =
                                Some(crate::models::TransitionAnimation {
                                    animation_type: crate::models::TransitionType::SuccessFlash,
                                    start_time: now,
                                    duration: 1000.0, // 1 second flash
                                });
                        }
                    }
                    NodeExecStatus::Failed => {
                        let now = js_sys::Date::now();
                        if let Some(ui_node_state) = state.ui_state.get_mut(node_id) {
                            ui_node_state.transition_animation =
                                Some(crate::models::TransitionAnimation {
                                    animation_type: crate::models::TransitionType::ErrorShake,
                                    start_time: now,
                                    duration: 800.0, // 0.8 second shake
                                });
                        }
                    }
                    _ => {}
                }

                // Only update agent status for agent-backed nodes with valid workflow execution statuses
                let agent_id = node.config.agent_id;
                if let Some(agent_id) = agent_id {
                    if let Some(agent) = state.agents.get_mut(&agent_id) {
                        // Only update agent status for legitimate workflow execution status changes
                        // that come from the backend workflow engine, not UI-only updates
                        let should_update_agent_status = match phase.as_str() {
                            "running" => true, // Node is running
                            "finished" => true, // Node finished (check result for success/failure)
                            _ => false, // Skip waiting or other states
                        };

                        if should_update_agent_status {
                            // Map node execution phase/result to valid agent status
                            let agent_status = match phase.as_str() {
                                "running" => "running",
                                "finished" => {
                                    // Check the result to determine agent status
                                    match result.as_ref().map(|r| r.as_str()).unwrap_or("failure") {
                                        "success" => "idle", // Node completed successfully, agent goes back to idle
                                        _ => "error", // Failed or cancelled
                                    }
                                },
                                _ => return true, // Should not reach here due to filter above
                            };

                            debug_log!(
                                "Updating agent {} status from '{}' to '{}' due to node {} workflow execution",
                                agent_id,
                                agent.status.as_ref().unwrap_or(&"unknown".to_string()),
                                agent_status,
                                node_id
                            );

                            agent.status = Some(agent_status.to_string());
                            let update = crate::models::ApiAgentUpdate {
                                name: None,
                                status: Some(agent_status.to_string()),
                                system_instructions: None,
                                task_instructions: None,
                                model: None,
                                schedule: None,
                                config: None,
                                last_error: None,
                            };
                            let agent_id_clone = agent_id;
                            let update_clone = update.clone();
                            wasm_bindgen_futures::spawn_local(async move {
                                match serde_json::to_string(&update_clone) {
                                    Ok(json_str) => {
                                        if let Err(e) = crate::network::ApiClient::update_agent(
                                            agent_id_clone,
                                            &json_str,
                                        )
                                        .await
                                        {
                                            web_sys::console::error_1(
                                                &format!("Failed to update agent: {:?}", e).into(),
                                            );
                                        }
                                    }
                                    Err(e) => {
                                        web_sys::console::error_1(
                                            &format!("Failed to serialize agent update: {}", e)
                                                .into(),
                                        );
                                    }
                                }
                            });
                        }
                    }
                }

                // Update connection animations for all edges involving this node
                let is_running = exec_status == NodeExecStatus::Running;

                // Find edges where this node is the source or target
                if let Some(current_workflow_id) = state.current_workflow_id {
                    if let Some(workflow) = state.workflows.get(&current_workflow_id) {
                        for edge in &workflow.get_edges() {
                            if edge.from_node_id == *node_id || edge.to_node_id == *node_id {
                                cmds.push(Command::SendMessage(
                                    crate::messages::Message::UpdateConnectionAnimation {
                                        from_node_id: edge.from_node_id.clone(),
                                        to_node_id: edge.to_node_id.clone(),
                                        is_executing: is_running,
                                    },
                                ));
                            }
                        }
                    }
                }

                // Connection animation logic is handled outside the mutable borrow scope

                state.state_modified = true;

                // Refresh results panel for node status updates via command
                cmds.push(Command::UpdateUI(Box::new(|| {
                    // Removed: let _ = crate::components::execution_results_panel::refresh_results_panel();
                })));
            }

            // Handle connection animation updates outside the mutable borrow scope
            // Determine if the node is running based on phase
            let is_running = phase.as_str() == "running";

            // Find workflow edges involving this node
            if let Some(current_workflow_id) = state.current_workflow_id {
                if let Some(workflow) = state.workflows.get(&current_workflow_id) {
                    for edge in &workflow.get_edges() {
                        if edge.from_node_id == *node_id || edge.to_node_id == *node_id {
                            cmds.push(Command::SendMessage(
                                crate::messages::Message::UpdateConnectionAnimation {
                                    from_node_id: edge.from_node_id.clone(),
                                    to_node_id: edge.to_node_id.clone(),
                                    is_executing: is_running,
                                },
                            ));
                        }
                    }
                }
            }

            // Handle parent-child connections (visual grouping)
            // Find all child nodes of this node
            let child_connections: Vec<_> = state
                .workflow_nodes
                .iter()
                .filter_map(|(_, other_node)| {
                    if other_node.config.parent_id == Some(node_id.clone()) {
                        Some((node_id.clone(), other_node.node_id.clone()))
                    } else {
                        None
                    }
                })
                .collect();

            // Add messages for child connections
            for (from_id, to_id) in child_connections {
                cmds.push(Command::SendMessage(
                    crate::messages::Message::UpdateConnectionAnimation {
                        from_node_id: from_id,
                        to_node_id: to_id,
                        is_executing: is_running,
                    },
                ));
            }

            // Add message for parent connection if this node has a parent
            if let Some(node) = state.workflow_nodes.get(node_id) {
                if let Some(parent_id) = &node.config.parent_id {
                    cmds.push(Command::SendMessage(
                        crate::messages::Message::UpdateConnectionAnimation {
                            from_node_id: parent_id.clone(),
                            to_node_id: node_id.clone(),
                            is_executing: is_running,
                        },
                    ));
                }
            }

            true
        }

        Message::UpdateConnectionAnimation {
            from_node_id,
            to_node_id,
            is_executing,
        } => {
            // Create a unique edge key for this connection
            let edge_key = format!("{}:{}", from_node_id, to_node_id);

            // Update or create the edge state
            let edge_state = state.ui_edge_state.entry(edge_key).or_default();
            edge_state.is_executing = *is_executing;

            // Also update based on the individual node states if available
            let source_running = state
                .ui_state
                .get(from_node_id)
                .and_then(|ui_state| ui_state.exec_status)
                .map(|status| status == crate::models::NodeExecStatus::Running)
                .unwrap_or(false);

            let target_running = state
                .ui_state
                .get(to_node_id)
                .and_then(|ui_state| ui_state.exec_status)
                .map(|status| status == crate::models::NodeExecStatus::Running)
                .unwrap_or(false);

            edge_state.update_from_nodes(source_running, target_running);

            state.state_modified = true;
            true
        }

        Message::AnimationTick => {
            // Check if there are active animations or continuous background animations that need rendering
            let has_background_particles = state.particle_system.is_some();
            let has_connection_lines = state
                .current_workflow_id
                .and_then(|wf_id| state.workflows.get(&wf_id))
                .map(|wf| !wf.get_edges().is_empty())
                .unwrap_or(false)
                || state
                    .workflow_nodes
                    .values()
                    .any(|node| node.get_parent_id().is_some());

            // Clean up expired transition animations and check if any are still active
            let now = js_sys::Date::now();
            let mut has_transition_animations = false;
            for node_id in state.ui_state.keys().cloned().collect::<Vec<String>>() {
                if let Some(ui_node_state) = state.ui_state.get_mut(&node_id) {
                    if let Some(animation) = &ui_node_state.transition_animation {
                        let elapsed = now - animation.start_time;
                        if elapsed >= animation.duration {
                            ui_node_state.transition_animation = None;
                        } else {
                            has_transition_animations = true;
                        }
                    }
                }
            }

            let needs_animation = !state.running_runs.is_empty()
                || state.connection_drag_active
                || state.ui_state.values().any(|s| s.is_dragging)
                || state.canvas_dragging
                || has_background_particles
                || has_connection_lines
                || has_transition_animations;

            if needs_animation {
                state.mark_dirty();
            }

            if state.dirty {
                state.dirty = false;
                cmds.push(Command::UpdateUI(Box::new(|| {
                    crate::state::APP_STATE.with(|state_rc| {
                        let mut st = state_rc.borrow_mut();
                        crate::canvas::renderer::draw_nodes(&mut st);
                        #[cfg(debug_assertions)]
                        {
                            if let Some(ctx) = st.context.as_ref() {
                                let debug_ring = crate::utils::debug::get_debug_ring();
                                crate::utils::debug::draw_overlay(ctx, &debug_ring);
                            }
                        }
                    });
                })));
            }
            let mut duration_changed = false;
            let now_ms = crate::utils::now_ms();
            for run_id in state.running_runs.iter() {
                if let Some((_agent_id, run_list)) = state
                    .agent_runs
                    .iter_mut()
                    .find(|(_aid, list)| list.iter().any(|r| r.id == *run_id))
                {
                    if let Some(run) = run_list.iter_mut().find(|r| r.id == *run_id) {
                        if let Some(start_iso) = &run.started_at {
                            if let Some(start_ms) = crate::utils::parse_iso_ms(start_iso) {
                                let new_duration = now_ms.saturating_sub(start_ms as u64);
                                if run
                                    .duration_ms
                                    .map(|old| new_duration / 1000 != old / 1000)
                                    .unwrap_or(true)
                                {
                                    run.duration_ms = Some(new_duration);
                                    duration_changed = true;
                                }
                            }
                        }
                    }
                }
            }
            for (_id, node) in state.workflow_nodes.iter_mut() {
                let agent_id = node.config.agent_id;
                if let Some(agent_id) = agent_id {
                    if let Some(agent) = state.agents.get(&agent_id) {
                        if let Some(status_str) = &agent.status {
                            if status_str == "processing" {
                                // Placeholder for future animation effect
                            }
                        }
                    }
                }
            }
            if duration_changed && state.active_view == crate::storage::ActiveView::Dashboard {
                cmds.push(Command::UpdateUI(Box::new(|| {
                    if let Some(win) = web_sys::window() {
                        if let Some(doc) = win.document() {
                            let _ = crate::components::dashboard::refresh_dashboard(&doc);
                        }
                    }
                })));
            }
            if state.state_modified {
                let now = crate::utils::now_ms();
                if now.saturating_sub(state.last_modified_ms) > 400 {
                    cmds.push(Command::SaveState);
                    state.state_modified = false;
                }
            }
            true
        }
        Message::CreateWorkflow { name } => {
            // Don't create local workflow here - let update.rs handle the API call
            // and WorkflowCreated message will update the state properly
            state.creating_workflow = true;
            cmds.push(Command::CreateWorkflowApi { name: name.clone() });

            // Refresh tab bar UI
            cmds.push(Command::UpdateUI(Box::new(|| {
                if let (Some(win),) = (web_sys::window(),) {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::workflow_switcher::refresh(&doc);
                    }
                }
            })));

            true
        }
        Message::SelectWorkflow { workflow_id } => {
            state.current_workflow_id = Some(*workflow_id);
            debug_log!(
                "DEBUG: SelectWorkflow {} - clearing {} nodes",
                workflow_id,
                state.workflow_nodes.len()
            );
            for (id, node) in &state.workflow_nodes {
                debug_log!("DEBUG: Clearing node {} (type: {:?})", id, node.node_type);
            }
            state.workflow_nodes.clear();
            state.ui_state.clear();
            if let Some(workflow) = state.workflows.get(workflow_id) {
                let nodes = workflow.get_nodes();
                debug_log!(
                    "DEBUG: Repopulating with {} workflow nodes",
                    nodes.len()
                );
                for node in &nodes {
                    debug_log!(
                        "DEBUG: Restoring node {} (type: {:?})",
                        node.node_id, node.node_type
                    );
                    state
                        .workflow_nodes
                        .insert(node.node_id.clone(), node.clone());
                    state
                        .ui_state
                        .insert(node.node_id.clone(), UiNodeState::default());
                }

                // NOTE: Trigger creation is now handled exclusively by CurrentWorkflowLoaded 
                // to prevent race conditions and duplicate triggers
            }
            state.mark_dirty();
            state.state_modified = true;

            cmds.push(Command::UpdateUI(Box::new(|| {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::workflow_switcher::refresh(&doc);
                        let _ = crate::components::workflow_switcher::update_run_button(&doc);
                        let _ = crate::components::log_drawer::refresh(&doc);
                    }
                }
            })));

            true
        }
        Message::AddEdge {
            from_node_id,
            to_node_id,
            label,
        } => {
            let edge_id = state.add_edge(from_node_id.clone(), to_node_id.clone(), label.clone());
            debug_log!("Created new edge with ID: {}", edge_id);
            state.mark_dirty();
            state.state_modified = true;

            true
        }
        Message::GenerateCanvasFromAgents => {
            let mut nodes_created = 0;
            let agents_needing_nodes = state
                .agents
                .iter()
                .filter(|(id, _)| !state.agent_id_to_node_id.contains_key(id))
                .map(|(id, agent)| (*id, agent.name.clone()))
                .collect::<Vec<_>>();
            for (i, (agent_id, name)) in agents_needing_nodes.iter().enumerate() {
                let row = i / 3;
                let col = i % 3;
                let x = 100.0 + (col as f64 * 250.0);
                let y = 100.0 + (row as f64 * 150.0);
                state.add_agent_node(*agent_id, name.clone(), x, y);
                nodes_created += 1;
            }
            debug_log!(
                "Created {} nodes for agents without visual representation",
                nodes_created
            );
            if nodes_created > 0 {
                state.state_modified = true;
                state.mark_dirty();
            }
            true
        }

        // Connection mode handlers
        Message::ToggleConnectionMode => {
            state.connection_mode = !state.connection_mode;
            if !state.connection_mode {
                // Clear selection when exiting connection mode
                state.connection_source_node = None;
                state.selected_node_id = None;
            }
            debug_log!("Connection mode: {}", state.connection_mode);
            state.mark_dirty();

            // Update button appearance
            cmds.push(Command::UpdateUI(Box::new(|| {
                if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
                    let _ = crate::components::workflow_switcher::update_connection_button(&doc);
                }
            })));

            true
        }

        Message::SelectNodeForConnection { node_id } => {
            if state.connection_mode {
                state.connection_source_node = Some(node_id.clone());
                state.selected_node_id = Some(node_id.clone());
                debug_log!("Selected node {} as connection source", node_id);
                state.mark_dirty();
            }
            true
        }

        Message::CreateConnectionFromSelected { target_node_id } => {
            if state.connection_mode {
                if let Some(source_node_id) = state.connection_source_node.clone() {
                    if source_node_id != *target_node_id {
                        let edge_id =
                            state.add_edge(source_node_id.clone(), target_node_id.clone(), None);
                        debug_log!(
                            "Created connection from {} to {} (edge ID: {})",
                            source_node_id, target_node_id, edge_id
                        );
                        state.mark_dirty();
                        state.state_modified = true;

                        // Clear selection after creating connection
                        state.connection_source_node = None;
                        state.selected_node_id = None;
                    } else {
                        debug_log!("Cannot connect node to itself");
                    }
                } else {
                    debug_log!("No source node selected for connection");
                }
            }
            true
        }

        Message::ClearNodeSelection => {
            state.connection_source_node = None;
            state.selected_node_id = None;
            debug_log!("Cleared node selection");
            state.mark_dirty();
            true
        }

        // Connection handle dragging handlers
        Message::StartConnectionDrag {
            node_id,
            handle_position,
            start_x,
            start_y,
        } => {
            state.connection_drag_active = true;
            state.connection_drag_start = Some((node_id.clone(), handle_position.clone()));
            state.connection_drag_current = Some((*start_x, *start_y));
            debug_log!(
                "Started connection drag from {} handle of node {}",
                handle_position, node_id
            );
            state.mark_dirty();
            true
        }

        Message::UpdateConnectionDrag {
            current_x,
            current_y,
        } => {
            if state.connection_drag_active {
                state.connection_drag_current = Some((*current_x, *current_y));
                state.mark_dirty(); // Repaint to show preview line
            }
            true
        }

        Message::EndConnectionDrag { end_x, end_y } => {
            if state.connection_drag_active {
                // Check if we're dropping on another node handle with validation
                if let Some((source_node_id, source_handle)) = state.connection_drag_start.clone() {
                    // Check if dropped on a specific handle (precise targeting)
                    let mut connection_created = false;
                    for (target_node_id, _) in &state.workflow_nodes.clone() {
                        if target_node_id != &source_node_id {
                            if let Some(target_handle) =
                                state.get_handle_at_point(target_node_id, *end_x, *end_y)
                            {
                                // Validate the connection before creating
                                if state.is_valid_connection(
                                    &source_handle,
                                    &target_handle,
                                    &source_node_id,
                                    target_node_id,
                                ) {
                                    let edge_id = state.add_edge(
                                        source_node_id.clone(),
                                        target_node_id.clone(),
                                        None,
                                    );
                                    debug_log!(
                                        "Created valid connection from {} ({}) to {} ({}) (edge ID: {})",
                                        source_node_id, source_handle, target_node_id, target_handle, edge_id
                                    );
                                    state.state_modified = true;
                                    connection_created = true;
                                } else {
                                    debug_log!(
                                        "Invalid connection: {} ({}) to {} ({})",
                                        source_node_id,
                                        source_handle,
                                        target_node_id,
                                        target_handle
                                    );
                                }
                                break;
                            }
                        }
                    }

                    // Fallback: if not dropped on a handle, check if dropped on a node (auto-route to input)
                    if !connection_created {
                        if let Some((target_node_id, _, _)) =
                            state.find_node_at_position(*end_x, *end_y)
                        {
                            if target_node_id != source_node_id {
                                // Auto-route: output -> input only
                                if state.is_valid_connection(
                                    &source_handle,
                                    "input",
                                    &source_node_id,
                                    &target_node_id,
                                ) {
                                    let edge_id = state.add_edge(
                                        source_node_id.clone(),
                                        target_node_id.clone(),
                                        None,
                                    );
                                    debug_log!(
                                        "Created auto-routed connection from {} ({}) to {} (input) (edge ID: {})",
                                        source_node_id, source_handle, target_node_id, edge_id
                                    );
                                    state.state_modified = true;
                                } else {
                                    debug_log!(
                                        "Invalid auto-route connection: {} ({}) to {} (input)",
                                        source_node_id, source_handle, target_node_id
                                    );
                                }
                            } else {
                                debug_log!("Cannot connect node to itself");
                            }
                        }
                    }
                }

                // Clear drag state
                state.connection_drag_active = false;
                state.connection_drag_start = None;
                state.connection_drag_current = None;
            }
            state.mark_dirty();
            true
        }

        _ => false,
    }
}
