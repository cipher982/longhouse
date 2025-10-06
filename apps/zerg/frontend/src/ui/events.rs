//! Generic (non-modal) UI event handlers.
//!
//! Modal-specific logic was migrated to `components::agent_config_modal` as
//! part of the modal refactor (see `modal_refactor.md`).  The helpers here are
//! limited to toolbar buttons and other global widgets.

use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Event, MouseEvent};

use crate::{messages::Message, state::dispatch_global_message};

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/// Entry point – call once after the base UI was rendered.  Attaches all
/// global (non-modal) event handlers.
pub fn setup_ui_event_handlers(document: &Document) -> Result<(), JsValue> {
    setup_auto_fit_button_handler(document)?;
    setup_center_view_handler(document)?;
    setup_clear_button_handler(document)?;
    setup_keyboard_shortcuts()?;
    Ok(())
}

// ---------------------------------------------------------------------------
// Individual handlers
// ---------------------------------------------------------------------------

/// <input type="checkbox" id="auto-fit-toggle">
fn setup_auto_fit_button_handler(document: &Document) -> Result<(), JsValue> {
    if let Some(toggle) = document.get_element_by_id("auto-fit-toggle") {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: Event| {
            dispatch_global_message(Message::ToggleAutoFit);
        }));
        toggle.add_event_listener_with_callback("change", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    Ok(())
}

/// Center view button
fn setup_center_view_handler(document: &Document) -> Result<(), JsValue> {
    if let Some(btn) = document.get_element_by_id("center-view") {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: MouseEvent| {
            dispatch_global_message(Message::CenterView);
        }));
        btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    Ok(())
}

/// Clear canvas button
fn setup_clear_button_handler(document: &Document) -> Result<(), JsValue> {
    if let Some(btn) = document.get_element_by_id("clear-button") {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: Event| {
            let window = web_sys::window().expect("no global window exists");
            let confirm = window
                .confirm_with_message(
                    "Are you sure you want to clear all nodes? This cannot be undone.",
                )
                .unwrap_or(false);
            if confirm {
                dispatch_global_message(Message::ClearCanvas);
                if let Err(err) = crate::storage::clear_storage() {
                    web_sys::console::error_1(
                        &format!("Failed to clear storage: {:?}", err).into(),
                    );
                }
            }
        }));
        btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    Ok(())
}

/// Global keyboard shortcuts (F = fit to content, 0 = reset view)
fn setup_keyboard_shortcuts() -> Result<(), JsValue> {
    use web_sys::KeyboardEvent;

    let window = web_sys::window().expect("no global window exists");

    let window_clone = window.clone();

    let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |e: KeyboardEvent| {
        // Ignore when typing in inputs
        if let Some(target) = e.target() {
            if target.dyn_ref::<web_sys::HtmlInputElement>().is_some()
                || target.dyn_ref::<web_sys::HtmlTextAreaElement>().is_some()
            {
                return;
            }
            if let Some(el) = target.dyn_ref::<web_sys::Element>() {
                if el.has_attribute("contenteditable") {
                    return;
                }
            }
        }

        match e.key().as_str() {
            " " => {
                if let Some(doc) = web_sys::window().unwrap().document() {
                    let _ = doc.body().unwrap().class_list().add_1("space-pan");
                }
            }
            "f" | "F" => {
                dispatch_global_message(Message::CenterView);
                // Prevent default browser find shortcut when Ctrl+F not pressed
                if !e.ctrl_key() && !e.meta_key() {
                    e.prevent_default();
                }
            }
            "0" => {
                dispatch_global_message(Message::ResetView);
                e.prevent_default();
            }
            "r" | "R" => {
                // Cmd/Ctrl + R triggers browser refresh; intercept only when
                // *not* pressed with meta/ctrl to avoid hijacking.
                if !e.meta_key() && !e.ctrl_key() {
                    if let Some(current_id) =
                        crate::state::APP_STATE.with(|st| st.borrow().current_workflow_id)
                    {
                        dispatch_global_message(Message::StartWorkflowExecution {
                            workflow_id: current_id,
                        });
                    }
                    e.prevent_default();
                }
            }
            _ => {}
        }
    }));

    window.add_event_listener_with_callback("keydown", cb.as_ref().unchecked_ref())?;
    cb.forget();

    // Keyup – exit pan mode etc.
    let cb_up = Closure::<dyn FnMut(_)>::wrap(Box::new(move |e: KeyboardEvent| {
        if e.key() == " " {
            if let Some(doc) = window_clone.document() {
                let _ = doc.body().unwrap().class_list().remove_1("space-pan");
            }
        }
    }));

    window.add_event_listener_with_callback("keyup", cb_up.as_ref().unchecked_ref())?;
    cb_up.forget();

    Ok(())
}

/// "Create Agent" button – spawns a brand-new agent node.
#[allow(dead_code)] // legacy UI button – kept for potential re-enablement
fn setup_create_agent_button_handler(document: &Document) -> Result<(), JsValue> {
    if let Some(btn) = document.get_element_by_id("create-agent-button") {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: MouseEvent| {
            let name = format!("Agent {}", (js_sys::Math::random() * 10000.0).round());
            dispatch_global_message(Message::RequestCreateAgent {
                name,
                system_instructions: "You are a helpful AI assistant.".into(),
                task_instructions: "Respond to user questions accurately and concisely.".into(),
            });
        }));
        btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    Ok(())
}

/// "Add Agent Node" button – lets the user quickly drop an existing agent onto
/// the canvas.  Minimal UX for early experiments: prompts for an Agent ID and
/// spawns a Node at a default location.
#[allow(dead_code)] // legacy UI button – kept for potential re-enablement
fn setup_add_agent_node_button_handler(document: &Document) -> Result<(), JsValue> {
    use crate::models::NodeType;

    if let Some(btn) = document.get_element_by_id("add-agent-node-button") {
        let cb = Closure::<dyn FnMut(_)>::wrap(Box::new(move |_e: MouseEvent| {
            let doc = web_sys::window().unwrap().document().unwrap();
            if let Some(sel_el) = doc.get_element_by_id("agent-select") {
                if let Ok(select) = sel_el.dyn_into::<web_sys::HtmlSelectElement>() {
                    let value = select.value();
                    if let Ok(agent_id_num) = value.parse::<u32>() {
                        dispatch_global_message(Message::AddCanvasNode {
                            agent_id: Some(agent_id_num),
                            x: 100.0,
                            y: 100.0,
                            node_type: NodeType::AgentIdentity,
                            text: format!("Agent {}", agent_id_num),
                        });
                    } else {
                        web_sys::console::warn_1(
                            &"Please select a valid agent before adding".into(),
                        );
                    }
                }
            }
        }));
        btn.add_event_listener_with_callback("click", cb.as_ref().unchecked_ref())?;
        cb.forget();
    }
    Ok(())
}
