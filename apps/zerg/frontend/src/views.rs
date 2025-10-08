// frontend/src/views.rs
//
// This file contains functions to render different parts of the UI
// based on the current application state.
//
use crate::components::agent_config_modal::AgentConfigModal;
use crate::dom_utils;
use crate::storage::ActiveView;
use wasm_bindgen::JsValue;
use web_sys::Document;
use crate::debug_log;

/// Helper: hide a DOM element (`display:none`) if it exists.
fn hide_by_id(document: &web_sys::Document, id: &str) {
    if let Some(elem) = document.get_element_by_id(id) {
        dom_utils::hide(&elem);
    }
}

/// Helper: show a DOM element (`display:block`) if it exists.
fn show_block_by_id(document: &web_sys::Document, id: &str) {
    if let Some(elem) = document.get_element_by_id(id) {
        // Use unified helper for visibility; rely on CSS for layout.
        crate::dom_utils::show(&elem);
    }
}

// Handle agent modal display
pub fn hide_agent_modal(document: &Document) -> Result<(), JsValue> {
    if let Some(modal) = document.get_element_by_id("agent-modal") {
        dom_utils::hide(&modal);
    }

    Ok(())
}

// Add a function to display the agent modal
pub fn show_agent_modal(agent_id: u32, document: &Document) -> Result<(), JsValue> {
    // Directly open the modal for the given agent
    AgentConfigModal::open(document, agent_id)?;
    Ok(())
}

// Render the appropriate view based on the explicit view type
// This avoids requiring a reference to AppState, preventing potential borrow issues
pub fn render_active_view_by_type(
    view_type: &ActiveView,
    document: &Document,
) -> Result<(), JsValue> {
    // First log which view we're switching to for debugging
    debug_log!("Switching to view: {:?}", view_type);

    // -------------------------------------------------------------
    // Step 1: Hide *all* view containers (if they exist).
    // -------------------------------------------------------------

    hide_by_id(document, "dashboard-container");
    hide_by_id(document, "canvas-container");
    hide_by_id(document, "main-content-area");
    hide_by_id(document, "canvas-input-panel");
    hide_by_id(document, "agent-shelf");
    hide_by_id(document, "workflow-bar");
    hide_by_id(document, "exec-sidebar");
    hide_by_id(document, "log-drawer");
    hide_by_id(document, "node-palette-shelf");
    hide_by_id(document, "profile-container");
    hide_by_id(document, "chat-view-container");
    hide_by_id(document, "ops-dashboard-container");

    // -------------------------------------------------------------
    // Step 2: Ensure requested view is mounted once, then show it.
    // -------------------------------------------------------------
    match view_type {
        ActiveView::Dashboard => {
            if !crate::pages::dashboard::is_dashboard_mounted(document) {
                crate::pages::dashboard::mount_dashboard(document)?;
            }
            show_block_by_id(document, "dashboard-container");

            // Ensure layout class on app-container is reset (dashboard needs default)
            if let Some(app_container) = document.get_element_by_id("app-container") {
                app_container.set_class_name("");
            }
        }
        ActiveView::Canvas => {
            if !crate::pages::canvas::is_canvas_mounted(document) {
                crate::pages::canvas::mount_canvas(document)?;
            }
            show_block_by_id(document, "canvas-container");
            show_block_by_id(document, "main-content-area");
            show_block_by_id(document, "canvas-input-panel");
            show_block_by_id(document, "workflow-bar");

            show_block_by_id(document, "agent-shelf");

            // Canvas view uses flex row layout via the .canvas-view class
            if let Some(app_container) = document.get_element_by_id("app-container") {
                app_container.set_class_name("canvas-view");
            }

            // Trigger-node creation is handled after backend workflow load
            // in update.rs::CurrentWorkflowLoaded to avoid race conditions.
        }
        ActiveView::Profile => {
            if !crate::pages::profile::is_profile_mounted(document) {
                crate::pages::profile::mount_profile(document)?;
            }
            show_block_by_id(document, "profile-container");

            if let Some(app_container) = document.get_element_by_id("app-container") {
                app_container.set_class_name("");
            }
        }
        ActiveView::ChatView => {
            // Setup chat view once
            if document.get_element_by_id("chat-view-container").is_none() {
                crate::components::chat_view::setup_chat_view(document)?;
            }
            // Show
            if let Some(chat_container) = document.get_element_by_id("chat-view-container") {
                crate::dom_utils::show(&chat_container);
            }

            if let Some(app_container) = document.get_element_by_id("app-container") {
                app_container.set_class_name("");
            }
        }
        ActiveView::AdminOps => {
            if !crate::pages::ops::is_ops_dashboard_mounted(document) {
                crate::pages::ops::mount_ops_dashboard(document)?;
            }
            show_block_by_id(document, "ops-dashboard-container");

            if let Some(app_container) = document.get_element_by_id("app-container") {
                app_container.set_class_name("");
            }
        }
    }

    // Update tab styling based on active view
    update_tab_styling(view_type, document)?;

    Ok(())
}

// Helper function to update tab styling based on active view
fn update_tab_styling(view_type: &ActiveView, document: &Document) -> Result<(), JsValue> {
    // Reset all tabs to inactive state first
    if let Some(dashboard_tab) = document.get_element_by_id("global-dashboard-tab") {
        dashboard_tab.set_class_name("tab-button");
    }

    if let Some(canvas_tab) = document.get_element_by_id("global-canvas-tab") {
        canvas_tab.set_class_name("tab-button");
    }

    // Now set the active tab based on the current view
    match view_type {
        ActiveView::Dashboard => {
            if let Some(dashboard_tab) = document.get_element_by_id("global-dashboard-tab") {
                dashboard_tab.set_class_name("tab-button active");
            }
        }
        ActiveView::Canvas => {
            if let Some(canvas_tab) = document.get_element_by_id("global-canvas-tab") {
                canvas_tab.set_class_name("tab-button active");
            }
        }
        ActiveView::Profile => {
            // Profile currently has no tab; ensure others are inactive.
        }
        ActiveView::ChatView => {
            // Chat doesn't have a tab currently, could add one in the future
        }
        ActiveView::AdminOps => {
            // Ops dashboard has no tab – keep both inactive
        }
    }

    Ok(())
}
