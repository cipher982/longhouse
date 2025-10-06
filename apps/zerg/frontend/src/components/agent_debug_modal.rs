//! Agent Debug Modal – phase 2 refactor.
//!
//! Rewritten to leverage the **shared** `components::modal` helper introduced
//! on 2025-05-11 so we no longer duplicate boiler-plate DOM creation and
//! visibility-handling across every modal implementation.
//!
//! Public surface:
//!   • `render_agent_debug_modal(state, document)` – (re)renders the modal
//!     based on `AppState.agent_debug_pane`.
//!   • `hide_agent_debug_modal(document)` – hides the backdrop (wrapper stays
//!     in the DOM for quick subsequent opens).

use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;

use web_sys::{Document, Element};

use crate::components::{modal as shared_modal, tab_bar};
use crate::messages::Message;
use crate::state::{dispatch_global_message, AppState, DebugTab};

/// (Re)render the Agent Debug Modal.
///
/// The modal is only visible when `state.agent_debug_pane` is `Some(_)` – we
/// early-return otherwise so callers can invoke this on every global render
/// pass without worrying about conditional checks.
pub fn render_agent_debug_modal(state: &AppState, document: &Document) -> Result<(), JsValue> {
    let Some(pane) = &state.agent_debug_pane else {
        // Hide backdrop if the pane was just cleared
        if let Some(elem) = document.get_element_by_id("agent-debug-modal") {
            shared_modal::hide(&elem);
        }
        return Ok(());
    };

    // ------------------------------------------------------------------
    // Backdrop & content wrappers – create once, then update inner markup.
    // ------------------------------------------------------------------
    let (backdrop, content) = shared_modal::ensure_modal(document, "agent-debug-modal")?;

    // Add data-testid for E2E testing
    let _ = backdrop.set_attribute("data-testid", "agent-debug-modal");

    // Give the content wrapper a stable ID so external helpers can target it.
    if content.id().is_empty() {
        content.set_id("agent-debug-content");
    }

    // Show backdrop (removes `hidden`).
    shared_modal::show(&backdrop);

    // Attach backdrop-click (close) & stop-propagation listeners **once**.
    if backdrop.get_attribute("data-listeners-attached").is_none() {
        // Close on background click
        let close_clone = backdrop.clone();
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_evt: web_sys::MouseEvent| {
            shared_modal::hide(&close_clone);
        }));
        backdrop.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();

        // Stop propagation so clicks inside `content` don’t bubble and close.
        let stop = Closure::<dyn FnMut(_)>::wrap(Box::new(|e: web_sys::MouseEvent| {
            e.stop_propagation();
        }));
        content.add_event_listener_with_callback("click", stop.as_ref().unchecked_ref())?;
        stop.forget();

        let _ = backdrop.set_attribute("data-listeners-attached", "true");
    }

    // ------------------------------------------------------------------
    // Inner markup – clear and rebuild according to current `pane` state.
    // ------------------------------------------------------------------
    content.set_inner_html("");

    // Header -------------------------------------------------------------
    let header_container = document.create_element("div")?;
    header_container.set_class_name("debug-header");

    let header = document.create_element("h2")?;
    header.set_inner_html(&format!("Agent #{} Debug", pane.agent_id));
    header_container.append_child(&header)?;

    content.append_child(&header_container)?;

    // Tabs – use shared TabBar helper then attach click handlers ------------
    let active_overview = matches!(pane.active_tab, DebugTab::Overview);
    let tab_container = tab_bar::build_tab_bar(
        document,
        &[
            ("Overview", active_overview),
            ("Raw JSON", !active_overview),
        ],
    )?;

    // Attach listeners to newly created buttons (child_nodes order matches
    // the slice order above).
    if let Some(first_btn) = tab_container.first_element_child() {
        attach_tab_click(&first_btn, DebugTab::Overview);
    }
    if let Some(second) = tab_container.last_element_child() {
        attach_tab_click(&second, DebugTab::RawJson);
    }

    content.append_child(&tab_container)?;

    // Body ---------------------------------------------------------------
    let body = document.create_element("div")?;
    body.set_class_name("tab-body");

    match pane.active_tab {
        DebugTab::Overview => {
            if pane.loading {
                body.set_inner_html("Loading …");
            } else if let Some(details) = &pane.details {
                let agent = &details.agent;
                let list = document.create_element("ul")?;
                list.set_class_name("overview-list");

                list.set_inner_html(&format!(
                    "<li><strong>Name:</strong> {}</li>
                     <li><strong>Status:</strong> {}</li>
                     <li><strong>Model:</strong> {}</li>
                     <li><strong>Schedule:</strong> {}</li>",
                    agent.name,
                    agent.status.clone().unwrap_or_else(|| "-".into()),
                    agent.model.clone().unwrap_or_else(|| "-".into()),
                    agent.schedule.clone().unwrap_or_else(|| "-".into()),
                ));
                body.append_child(&list)?;
            } else {
                body.set_inner_html("No data.");
            }
        }
        DebugTab::RawJson => {
            if pane.loading {
                body.set_inner_html("Loading …");
            } else if let Some(details) = &pane.details {
                let pre = document.create_element("pre")?;
                pre.set_class_name("raw-json");
                let json_str = serde_json::to_string_pretty(details).unwrap_or_default();
                pre.set_text_content(Some(&json_str));
                body.append_child(&pre)?;
            }
        }
    }

    content.append_child(&body)?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Shared attacher – avoids code duplication inside build logic
fn attach_tab_click(btn: &Element, tab: DebugTab) {
    let tab_clone = tab.clone();
    let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_evt: web_sys::MouseEvent| {
        dispatch_global_message(Message::SetAgentDebugTab(tab_clone.clone()));
    }));
    let _ = btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref());
    cb.forget();
}

/// Convenience wrapper around the shared helper so external call-sites remain
/// unchanged.
pub fn hide_agent_debug_modal(document: &Document) -> Result<(), JsValue> {
    if let Some(elem) = document.get_element_by_id("agent-debug-modal") {
        shared_modal::hide(&elem);
    }
    Ok(())
}
