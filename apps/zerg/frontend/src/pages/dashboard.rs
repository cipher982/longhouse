// frontend/src/pages/dashboard.rs
//
// This file contains the Dashboard page component, responsible for
// mounting and unmounting the dashboard view.
//
use wasm_bindgen::JsValue;
use web_sys::{Document, Element};

/// Mount the dashboard view by creating necessary DOM elements
/// This function is called when switching to the dashboard view
pub fn mount_dashboard(document: &Document) -> Result<(), JsValue> {
    if let Some(window) = web_sys::window() {
        if let Ok(Some(storage)) = window.local_storage() {
            if let Ok(flag) = storage.get_item("zerg_use_react_dashboard") {
                if flag.as_deref() == Some("1") {
                    if let Ok(Some(target_url)) = storage.get_item("zerg_react_dashboard_url") {
                        let _ = window.location().set_href(&target_url);
                        return Ok(());
                    } else {
                        web_sys::console::warn_1(&"React dashboard flag enabled but no target URL configured".into());
                    }
                }
            }
        }
    }

    // Get app container for adding dashboard
    let app_container = document
        .get_element_by_id("app-container")
        .ok_or(JsValue::from_str("Could not find app-container"))?;

    // Ensure we're using the default layout (not canvas-view)
    app_container.set_class_name("");

    // Create dashboard container if needed
    let dashboard_container =
        if let Some(container) = document.get_element_by_id("dashboard-container") {
            container
        } else {
            // Create the container
            let container = document.create_element("div")?;
            container.set_id("dashboard-container");
            container.set_class_name("dashboard-container");

            // Create dashboard element
            let dashboard = document.create_element("div")?;
            dashboard.set_id("dashboard");
            dashboard.set_class_name("dashboard");
            container.append_child(&dashboard)?;

            app_container.append_child(&container)?;
            container
        };

    // Ensure the dashboard is visible with proper styling
    crate::dom_utils::show(&dashboard_container);

    // Verify the dashboard inner element exists
    if document.get_element_by_id("dashboard").is_none() {
        let dashboard = document.create_element("div")?;
        dashboard.set_id("dashboard");
        dashboard.set_class_name("dashboard");
        dashboard_container.append_child(&dashboard)?;
    }

    // Update tab styling
    if let Some(dashboard_tab) = document.get_element_by_id("global-dashboard-tab") {
        dashboard_tab.set_class_name("tab-button active");
    }

    if let Some(canvas_tab) = document.get_element_by_id("global-canvas-tab") {
        canvas_tab.set_class_name("tab-button");
    }

    // Initialize or refresh the dashboard content
    crate::components::dashboard::refresh_dashboard(document)?;

    Ok(())
}

/// Unmount the dashboard view by removing it from the DOM
/// This function is called when switching away from the dashboard view
#[allow(dead_code)]
pub fn unmount_dashboard(document: &Document) -> Result<(), JsValue> {
    if let Some(container) = document.get_element_by_id("dashboard-container") {
        // Option 1: Remove container completely from DOM
        if let Some(parent) = container.parent_node() {
            parent.remove_child(&container)?;
        }
    }

    Ok(())
}

/// Get a reference to the dashboard container element if it exists
#[allow(dead_code)]
pub fn get_dashboard_container(document: &Document) -> Option<Element> {
    document.get_element_by_id("dashboard-container")
}

/// Check if the dashboard is currently mounted
pub fn is_dashboard_mounted(document: &Document) -> bool {
    document.get_element_by_id("dashboard-container").is_some()
}
