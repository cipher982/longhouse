// frontend/src/pages/canvas.rs
//
// This file contains the Canvas page component, responsible for
// mounting and unmounting the canvas view with its sidebar and toolbar.
//
// Trigger creation is orchestrated in update.rs; no direct use here.
use wasm_bindgen::JsCast;
use wasm_bindgen::JsValue;
use web_sys::{Document, Element};
use crate::debug_log;

/// Mount the canvas view by creating necessary DOM elements
/// This function is called when switching to the canvas view
pub fn mount_canvas(document: &Document) -> Result<(), JsValue> {
    debug_log!("CANVAS: Starting mount");

    // Get app container for proper layout
    let app_container = document
        .get_element_by_id("app-container")
        .ok_or(JsValue::from_str("Could not find app-container"))?;

    // Add canvas-view class for flexbox layout
    debug_log!("CANVAS: Setting app-container to canvas-view class");
    app_container.set_class_name("canvas-view");

    // First create/ensure agent shelf exists (sidebar)
    debug_log!("CANVAS: Creating/refreshing agent shelf");

    // Agent shelf state is managed by workflow loading - no manual sync needed

    crate::components::agent_shelf::refresh_agent_shelf(document)?;

    // Ensure agent shelf is a direct child of app_container
    let agent_shelf = document
        .get_element_by_id("agent-shelf")
        .ok_or(JsValue::from_str("Agent shelf not found after refresh"))?;

    // If agent shelf is not already a child of app_container, append it
    if agent_shelf.parent_node().map_or(true, |p| {
        p.node_name() != "DIV"
            || p.dyn_ref::<Element>()
                .map_or(true, |e| e.id() != "app-container")
    }) {
        debug_log!("CANVAS: Appending agent shelf to app container");
        app_container.append_child(&agent_shelf)?;
    }

    // Create main content area if it doesn't exist
    debug_log!("CANVAS: Creating/finding main content area");
    let main_content = if let Some(content) = document.get_element_by_id("main-content-area") {
        content
    } else {
        let content = document.create_element("div")?;
        content.set_id("main-content-area");
        content.set_class_name("main-content-area");
        app_container.append_child(&content)?;
        content
    };

    // Removed: input panel (toolbar) creation and mounting; controls are now in workflow bar

    // Create canvas container if needed
    debug_log!("CANVAS: Creating/finding canvas container");
    let canvas_container = if let Some(container) = document.get_element_by_id("canvas-container") {
        container
    } else {
        let container = document.create_element("div")?;
        container.set_id("canvas-container");
        container.set_class_name("canvas-container");
        // Stable hook for E2E tests and tools
        let _ = container.set_attribute("data-testid", "canvas-container");

        // Create canvas element
        let canvas = document.create_element("canvas")?;
        canvas.set_id("node-canvas");
        container.append_child(&canvas)?;

        // Add container to main content
        main_content.append_child(&container)?;

        // Setup canvas once it's created
        crate::components::canvas_editor::setup_canvas(document)?;

        container
    };

    // Ensure canvas container is visible
    crate::dom_utils::show(&canvas_container);

    // Initialize/refresh the workflow switcher bar for this canvas view
    debug_log!("CANVAS: Initializing workflow switcher");
    if let Err(e) = crate::components::workflow_switcher::init(document) {
        web_sys::console::error_1(
            &format!("Failed to initialize workflow switcher: {:?}", e).into(),
        );
    }

    // Initialize the execution results panel
    debug_log!("CANVAS: Initializing execution results panel");
    // Removed execution results panel initialization.

    // Initialize the particle system for the canvas background via message dispatch
    crate::state::dispatch_global_message(crate::messages::Message::InitializeParticleSystem {
        width: crate::state::APP_STATE.with(|state| state.borrow().canvas_width),
        height: crate::state::APP_STATE.with(|state| state.borrow().canvas_height),
    });

    debug_log!("CANVAS: Setup canvas drawing (no state borrowed)");

    // NOTE: Trigger-node auto-creation has been centralized to the
    // CurrentWorkflowLoaded handler in update.rs to avoid races between
    // mount, view-switch and backend workflow loading.
    // Set up canvas resizing and drawing (without borrowing APP_STATE)
    if let Some(canvas_elem) = document.get_element_by_id("node-canvas") {
        if let Ok(canvas) = canvas_elem.dyn_into::<web_sys::HtmlCanvasElement>() {
            // Resize canvas explicitly without borrowing the app state
            if let Err(e) = crate::components::canvas_editor::resize_canvas(
                &canvas,
                crate::components::canvas_editor::AppStateRef::None,
            ) {
                web_sys::console::error_1(&format!("Failed to resize canvas: {:?}", e).into());
            }
        }
    }

    debug_log!("CANVAS: Mount complete");
    Ok(())
}

// Legacy ensure_trigger_node_exists removed – trigger creation is centralized
// during workflow load/selection in reducers to avoid race conditions.

/// Unmount the canvas view by removing it from the DOM
/// This function is called when switching away from the canvas view
#[allow(dead_code)]
pub fn unmount_canvas(document: &Document) -> Result<(), JsValue> {
    debug_log!("CANVAS: Starting unmount");

    // Remove workflow bar
    if let Some(workflow_bar) = document.get_element_by_id("workflow-bar") {
        debug_log!("CANVAS: Removing workflow bar");
        if let Some(parent) = workflow_bar.parent_node() {
            parent.remove_child(&workflow_bar)?;
        }
    }

    // Remove input panel
    if let Some(panel) = document.get_element_by_id("canvas-input-panel") {
        debug_log!("CANVAS: Removing input panel");
        if let Some(parent) = panel.parent_node() {
            parent.remove_child(&panel)?;
        }
    }

    // Remove canvas container
    if let Some(container) = document.get_element_by_id("canvas-container") {
        debug_log!("CANVAS: Removing canvas container");
        if let Some(parent) = container.parent_node() {
            parent.remove_child(&container)?;
        }
    }

    // Remove main content area
    if let Some(content) = document.get_element_by_id("main-content-area") {
        debug_log!("CANVAS: Removing main content area");
        if let Some(parent) = content.parent_node() {
            parent.remove_child(&content)?;
        }
    }

    // Remove agent shelf - critical to ensure it doesn't appear in dashboard view
    if let Some(shelf) = document.get_element_by_id("agent-shelf") {
        debug_log!("CANVAS: Removing agent shelf");
        if let Some(parent) = shelf.parent_node() {
            parent.remove_child(&shelf)?;
        }
    }

    // Reset app-container class back to default (remove canvas-view)
    if let Some(app_container) = document.get_element_by_id("app-container") {
        debug_log!("CANVAS: Resetting app container class");
        app_container.set_class_name("");
    }

    // Clear the particle system when unmounting the canvas via message dispatch
    crate::state::dispatch_global_message(crate::messages::Message::ClearParticleSystem);

    debug_log!("CANVAS: Unmount complete");
    Ok(())
}

/// Get a reference to the canvas container element if it exists
#[allow(dead_code)]
pub fn get_canvas_container(document: &Document) -> Option<Element> {
    document.get_element_by_id("canvas-container")
}

/// Check if the canvas is currently mounted
pub fn is_canvas_mounted(document: &Document) -> bool {
    document.get_element_by_id("canvas-container").is_some()
}
