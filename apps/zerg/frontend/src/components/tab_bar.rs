//! Simple *TabBar* helper – central place to create the standard markup for
//! `.tab-container` + `.tab-button` elements so modal implementations don't
//! clone this snippet over and over.
//!
//! Phase-1 scope (May 2025): *pure markup factory*.
//!   • No internal state – the caller owns the "which tab is active" logic
//!     (usually inside `AppState`).
//!   • No default click-handlers – each consumer attaches its own listener so
//!     it can dispatch the appropriate `Message` variant.
//!
//! A later phase might add an enum-based constructor that auto-generates
//! handlers when given a closure factory, but that requires higher-order
//! generics that complicate wasm-bindgen lifetimes – out of scope for now.

use crate::constants::{ATTR_TYPE, BUTTON_TYPE_BUTTON, CSS_TAB_BUTTON, CSS_TAB_BUTTON_ACTIVE};
use std::cell::RefCell;
use wasm_bindgen::prelude::*;
use wasm_bindgen::{closure::Closure, JsCast};
use web_sys::{Document, HtmlElement, KeyboardEvent};

// Thread-local storage for keyboard event handlers to prevent memory leaks
thread_local! {
    static TAB_KEYBOARD_HANDLERS: RefCell<Vec<(String, Closure<dyn FnMut(KeyboardEvent)>)>> = RefCell::new(Vec::new());
}

/// Create a `<div class="tab-container">` with child `<button>`s – one per
/// entry in `tabs`.
///
/// * `tabs` – slice of `(label, active)` tuples.  The caller is responsible
/// for **unordered** uniqueness and for attaching click listeners after the
/// function returns.
///
/// Returns the container element which can be directly inserted into the DOM
/// hierarchy.
pub fn build_tab_bar(
    document: &Document,
    tabs: &[(&str, bool)],
) -> Result<web_sys::Element, JsValue> {
    let container = document.create_element("div")?;
    container.set_class_name("tab-container");

    for (label, active) in tabs {
        let btn = document.create_element("button")?;
        btn.set_attribute(ATTR_TYPE, BUTTON_TYPE_BUTTON)?;
        btn.set_inner_html(label);
        btn.set_class_name(if *active {
            CSS_TAB_BUTTON_ACTIVE
        } else {
            CSS_TAB_BUTTON
        });
        container.append_child(&btn)?;
    }

    Ok(container)
}

/// Add keyboard navigation to a tab container. This enables arrow key navigation
/// between tabs and Enter/Space to activate tabs.
///
/// * `container` - The tab container element returned by `build_tab_bar`
/// * `container_id` - Unique identifier for this tab container (for cleanup)
/// * `on_tab_change` - Callback function that takes the tab index when a tab is activated
pub fn add_keyboard_navigation<F>(
    container: &web_sys::Element,
    container_id: &str,
    on_tab_change: F,
) -> Result<(), JsValue>
where
    F: Fn(usize) + 'static,
{
    // Remove any existing handler for this container
    remove_keyboard_navigation(container_id);

    // Get all tab buttons by iterating through children
    let mut buttons = Vec::new();
    let children = container.children();
    for i in 0..children.length() {
        if let Some(child) = children.item(i) {
            if child.tag_name().to_lowercase() == "button" {
                buttons.push(child);
            }
        }
    }

    if buttons.is_empty() {
        return Ok(());
    }

    // Set up ARIA attributes for accessibility
    for (i, button) in buttons.iter().enumerate() {
        if let Ok(html_button) = button.clone().dyn_into::<HtmlElement>() {
            html_button.set_attribute("role", "tab")?;
            html_button.set_attribute("tabindex", if i == 0 { "0" } else { "-1" })?;
        }
    }

    // Set container role
    if let Some(html_container) = container.dyn_ref::<HtmlElement>() {
        html_container.set_attribute("role", "tablist")?;
    }

    // Create keyboard event handler
    let container_clone = container.clone();
    let on_tab_change = std::rc::Rc::new(on_tab_change);

    let keydown_handler = Closure::wrap(Box::new(move |event: KeyboardEvent| {
        let key = event.key();

        // Only handle arrow keys, Enter, and Space
        if !matches!(key.as_str(), "ArrowLeft" | "ArrowRight" | "Enter" | " ") {
            return;
        }

        // Prevent default behavior
        event.prevent_default();

        // Get current focused element
        if let Some(document) = web_sys::window().and_then(|w| w.document()) {
            if let Some(active_element) = document.active_element() {
                // Get all tab buttons by iterating through children
                let mut buttons = Vec::new();
                let children = container_clone.children();
                for i in 0..children.length() {
                    if let Some(child) = children.item(i) {
                        if child.tag_name().to_lowercase() == "button" {
                            buttons.push(child);
                        }
                    }
                }

                // Find current tab index
                let mut current_index = None;
                for (i, button) in buttons.iter().enumerate() {
                    if button.is_same_node(Some(&active_element)) {
                        current_index = Some(i);
                        break;
                    }
                }

                if let Some(current) = current_index {
                    match key.as_str() {
                        "ArrowLeft" => {
                            // Move to previous tab (wrap around)
                            let new_index = if current == 0 {
                                buttons.len() - 1
                            } else {
                                current - 1
                            };
                            focus_tab(&buttons, new_index);
                        }
                        "ArrowRight" => {
                            // Move to next tab (wrap around)
                            let new_index = (current + 1) % buttons.len();
                            focus_tab(&buttons, new_index);
                        }
                        "Enter" | " " => {
                            // Activate current tab
                            on_tab_change(current);
                        }
                        _ => {}
                    }
                }
            }
        }
    }) as Box<dyn FnMut(KeyboardEvent)>);

    // Add event listener to container
    container
        .add_event_listener_with_callback("keydown", keydown_handler.as_ref().unchecked_ref())?;

    // Store handler for cleanup
    TAB_KEYBOARD_HANDLERS.with(|handlers| {
        handlers
            .borrow_mut()
            .push((container_id.to_string(), keydown_handler));
    });

    Ok(())
}

/// Remove keyboard navigation for a specific tab container
pub fn remove_keyboard_navigation(container_id: &str) {
    TAB_KEYBOARD_HANDLERS.with(|handlers| {
        let mut handlers = handlers.borrow_mut();
        handlers.retain(|(id, _)| id != container_id);
    });
}

/// Helper function to focus a specific tab and update tabindex attributes
fn focus_tab(buttons: &[web_sys::Element], index: usize) {
    for (i, button) in buttons.iter().enumerate() {
        if let Ok(html_button) = button.clone().dyn_into::<HtmlElement>() {
            if i == index {
                // Focus this tab and make it tabbable
                let _ = html_button.set_attribute("tabindex", "0");
                let _ = html_button.focus();
            } else {
                // Make other tabs not tabbable
                let _ = html_button.set_attribute("tabindex", "-1");
            }
        }
    }
}
