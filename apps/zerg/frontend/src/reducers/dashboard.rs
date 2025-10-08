//! Dashboard domain reducer: handles dashboard navigation, UI refresh, scope toggling, database reset, run history toggling.

use crate::messages::{Command, Message};
use crate::state::AppState;
use crate::debug_log;

/// Handles dashboard-related messages. Returns true if the message was handled.
pub fn update(state: &mut AppState, msg: &Message, commands: &mut Vec<Command>) -> bool {
    match msg {
        Message::NavigateToDashboard => {
            // Set the active view to Dashboard
            state.active_view = crate::storage::ActiveView::Dashboard;

            // Trigger agent refresh when navigating to dashboard
            // This ensures agents are always up-to-date when dashboard loads
            commands.push(Command::FetchAgents);

            commands.push(Command::UpdateUI(Box::new(move || {
                if let Some(document) = web_sys::window().unwrap().document() {
                    // First hide the chat view container
                    if let Some(chat_container) = document.get_element_by_id("chat-view-container")
                    {
                        crate::dom_utils::hide(&chat_container);
                    }

                    if let Err(e) = crate::views::render_active_view_by_type(
                        &crate::storage::ActiveView::Dashboard,
                        &document,
                    ) {
                        web_sys::console::error_1(
                            &format!("Failed to render dashboard: {:?}", e).into(),
                        );
                    }
                }
            })));
            true
        }
        Message::RefreshDashboard => {
            // Trigger agent fetch to ensure data is fresh
            commands.push(Command::FetchAgents);

            // Schedule UI update outside the current mutable borrow scope
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(window) = web_sys::window() {
                    if let Some(document) = window.document() {
                        // Step 1: read active view with an immutable borrow and drop it.
                        let is_dashboard = crate::state::APP_STATE.with(|state_cell| {
                            state_cell.borrow().active_view == crate::storage::ActiveView::Dashboard
                        });

                        if is_dashboard {
                            // Refresh only the dashboard to avoid borrowing state mutably while inside refresh.
                            if let Err(e) =
                                crate::components::dashboard::refresh_dashboard(&document)
                            {
                                web_sys::console::error_1(
                                    &format!("Failed to refresh dashboard: {:?}", e).into(),
                                );
                            }
                        }
                    }
                }
            })));
            true
        }
        Message::ToggleDashboardScope(new_scope) => {
            if state.dashboard_scope != *new_scope {
                state.dashboard_scope = new_scope.clone();

                // Persist to localStorage
                if let Some(window) = web_sys::window() {
                    if let Ok(Some(storage)) = window.local_storage() {
                        let _ = storage.set_item("dashboard_scope", new_scope.as_str());
                    }
                }

                // Trigger agent list reload
                commands.push(Command::FetchAgents);

                // Force dashboard re-render
                commands.push(Command::UpdateUI(Box::new(|| {
                    if let Some(window) = web_sys::window() {
                        if let Some(document) = window.document() {
                            let _ = crate::components::dashboard::refresh_dashboard(&document);
                        }
                    }
                })));
            }
            true
        }
        Message::ResetDatabase => {
            // The actual database reset happens via API call (already done in dashboard.rs)
            // We don't need to do anything here because:
            // 1. The page will be refreshed immediately after this (in dashboard.rs)
            // 2. On refresh, it will automatically load the fresh state from the backend
            debug_log!("Reset database message received - state will refresh");
            true
        }
        Message::ToggleRunHistory { agent_id } => {
            if state.run_history_expanded.contains(agent_id) {
                state.run_history_expanded.remove(agent_id);
            } else {
                state.run_history_expanded.insert(*agent_id);
            }

            // Refresh dashboard UI
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(window) = web_sys::window() {
                    if let Some(document) = window.document() {
                        let _ = crate::components::dashboard::refresh_dashboard(&document);
                    }
                }
            })));
            true
        }
        Message::RequestCreateAgent {
            name,
            system_instructions,
            task_instructions,
        } => {
            debug_log!("Dashboard: Creating agent with name: {}", name);

            // Use the default model from state - no fallbacks, backend should always provide this
            let model = &state.default_model_id;

            // Create the agent via API using NetworkCall command
            let payload = serde_json::json!({
                "name": name,
                "system_instructions": system_instructions,
                "task_instructions": task_instructions,
                "model": model
            })
            .to_string();

            commands.push(Command::NetworkCall {
                endpoint: "/api/agents".to_string(),
                method: "POST".to_string(),
                body: Some(payload),
                on_success: Box::new(Message::RefreshAgentsFromAPI),
                on_error: Box::new(Message::RefreshAgentsFromAPI),
            });
            true
        }

        // -------------------------------------------------------------
        // Sort toggling
        // -------------------------------------------------------------
        Message::UpdateDashboardSort(key) => {
            let mut asc = true;
            if state.dashboard_sort.key == *key {
                // Same column: toggle direction
                asc = !state.dashboard_sort.ascending;
            }

            state.dashboard_sort = crate::state::DashboardSort {
                key: *key,
                ascending: asc,
            };

            // Persist to storage
            if let Some(window) = web_sys::window() {
                if let Ok(Some(storage)) = window.local_storage() {
                    let key_str = match key {
                        crate::state::DashboardSortKey::Name => "name",
                        crate::state::DashboardSortKey::Status => "status",
                        crate::state::DashboardSortKey::LastRun => "last_run",
                        crate::state::DashboardSortKey::NextRun => "next_run",
                        crate::state::DashboardSortKey::SuccessRate => "success",
                    };
                    let _ = storage.set_item("dashboard_sort_key", key_str);
                    let _ = storage.set_item("dashboard_sort_asc", if asc { "1" } else { "0" });
                }
            }

            // Refresh UI
            commands.push(Command::UpdateUI(Box::new(|| {
                if let Some(win) = web_sys::window() {
                    if let Some(doc) = win.document() {
                        let _ = crate::components::dashboard::refresh_dashboard(&doc);
                    }
                }
            })));
            true
        }
        _ => false,
    }
}
