//! Shared modal helper used by Agent Debug & Config modals.
//!
//! Keeps creation / show / hide logic in one place so feature modals don't
//! duplicate the same boilerplate.

use std::cell::RefCell;
use wasm_bindgen::{closure::Closure, JsCast};
use web_sys::{Document, Element, EventTarget, HtmlElement, KeyboardEvent};

use crate::dom_utils;

/// Ensure a `<div id="{id}" class="modal">…` exists in the DOM and return it.
/// The returned element is the **backdrop** container.  A child `<div
/// class="modal-content">` is created (and returned) if missing so callers
/// can append their specific inner markup.
///
/// Returns `(backdrop, content)`.
pub fn ensure_modal(
    document: &Document,
    id: &str,
) -> Result<(Element, Element), wasm_bindgen::JsValue> {
    // Backdrop first ------------------------------------------------------
    let backdrop = if let Some(el) = document.get_element_by_id(id) {
        el
    } else {
        let el = document.create_element("div")?;
        el.set_id(id);
        el.set_class_name("modal");
        // Add ARIA attributes for accessibility
        el.set_attribute("role", "dialog")?;
        el.set_attribute("aria-modal", "true")?;
        dom_utils::hide(&el);
        document.body().unwrap().append_child(&el)?;
        el
    };

    // Content wrapper inside backdrop ------------------------------------
    let content = if let Some(el) = backdrop.query_selector(".modal-content")? {
        el
    } else {
        let el = document.create_element("div")?;
        el.set_class_name("modal-content");
        backdrop.append_child(&el)?;
        el
    };

    Ok((backdrop, content))
}

// Thread-local storage for modal event listeners
thread_local! {
    static MODAL_LISTENERS: RefCell<Vec<(String, Closure<dyn FnMut(KeyboardEvent)>)>> = RefCell::new(Vec::new());
}

/// Show the modal with focus management and keyboard trap.
/// - Stores the currently focused element for later restoration
/// - Shows the modal
/// - Focuses the first interactive element within the modal
/// - Sets up keyboard event handlers for focus trap and Escape key
pub fn show(modal_backdrop: &Element) {
    if let Some(document) = web_sys::window().and_then(|w| w.document()) {
        // Store the currently focused element
        let _ = dom_utils::store_active_element(&document);

        // Show the modal
        dom_utils::show(modal_backdrop);

        // Focus first interactive element
        let _ = dom_utils::focus_first_interactive(modal_backdrop);

        // Set up keyboard event handlers
        setup_keyboard_handlers(modal_backdrop);
    }
}

/// Hide the modal with focus restoration.
/// - Hides the modal
/// - Removes keyboard event handlers
/// - Restores focus to the previously focused element
pub fn hide(modal_backdrop: &Element) {
    // Remove keyboard event handlers
    cleanup_keyboard_handlers(modal_backdrop);

    // Hide the modal
    dom_utils::hide(modal_backdrop);

    // Restore focus to the previously focused element
    dom_utils::restore_previous_focus();
}

/// Show the modal with explicit focus management.
/// This version allows the caller to specify what element should receive focus.
pub fn show_with_focus(modal_backdrop: &Element, focus_selector: Option<&str>) {
    if let Some(document) = web_sys::window().and_then(|w| w.document()) {
        // Store the currently focused element
        let _ = dom_utils::store_active_element(&document);

        // Show the modal
        dom_utils::show(modal_backdrop);

        // Focus specific element or first interactive element
        if let Some(selector) = focus_selector {
            if let Ok(Some(element)) = modal_backdrop.query_selector(selector) {
                if let Ok(html_element) = element.dyn_into::<HtmlElement>() {
                    let _ = html_element.focus();
                } else {
                    // Fallback to first interactive element
                    let _ = dom_utils::focus_first_interactive(modal_backdrop);
                }
            } else {
                // Fallback to first interactive element
                let _ = dom_utils::focus_first_interactive(modal_backdrop);
            }
        } else {
            // Fallback to first interactive element
            let _ = dom_utils::focus_first_interactive(modal_backdrop);
        }

        // Set up keyboard event handlers
        setup_keyboard_handlers(modal_backdrop);
    }
}

/// Set up keyboard event handlers for focus trap and Escape key.
fn setup_keyboard_handlers(modal_backdrop: &Element) {
    let modal_id = modal_backdrop.id();

    // Create keydown handler
    let modal_backdrop_clone = modal_backdrop.clone();
    let keydown_handler = Closure::wrap(Box::new(move |event: KeyboardEvent| {
        let key = event.key();

        match key.as_str() {
            "Escape" => {
                // Close modal on Escape key
                event.prevent_default();
                hide(&modal_backdrop_clone);
            }
            "Tab" => {
                // Implement focus trap
                let focusable_elements = dom_utils::get_focusable_elements(&modal_backdrop_clone);
                if !focusable_elements.is_empty() {
                    if let Some(target) = event.target() {
                        if let Ok(current_element) = target.dyn_into::<HtmlElement>() {
                            let first_element = focusable_elements.first();
                            let last_element = focusable_elements.last();

                            if event.shift_key() {
                                // Shift+Tab: moving backwards
                                if let Some(first) = first_element {
                                    if current_element == *first {
                                        // We're at the first element, wrap to last
                                        event.prevent_default();
                                        if let Some(last) = last_element {
                                            let _ = last.focus();
                                        }
                                    }
                                }
                            } else {
                                // Tab: moving forwards
                                if let Some(last) = last_element {
                                    if current_element == *last {
                                        // We're at the last element, wrap to first
                                        event.prevent_default();
                                        if let Some(first) = first_element {
                                            let _ = first.focus();
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }) as Box<dyn FnMut(KeyboardEvent)>);

    // Add event listener to the modal
    if let Ok(event_target) = modal_backdrop.clone().dyn_into::<EventTarget>() {
        let _ = event_target.add_event_listener_with_callback(
            "keydown",
            keydown_handler.as_ref().dyn_ref().unwrap(),
        );

        // Store the handler so it doesn't get dropped
        MODAL_LISTENERS.with(|listeners| {
            listeners.borrow_mut().push((modal_id, keydown_handler));
        });
    }
}

/// Clean up keyboard event handlers when modal is closed.
fn cleanup_keyboard_handlers(modal_backdrop: &Element) {
    let modal_id = modal_backdrop.id();

    MODAL_LISTENERS.with(|listeners| {
        let mut listeners_mut = listeners.borrow_mut();
        listeners_mut.retain(|(id, handler)| {
            if id == &modal_id {
                // Remove the event listener
                if let Ok(event_target) = modal_backdrop.clone().dyn_into::<EventTarget>() {
                    let _ = event_target.remove_event_listener_with_callback(
                        "keydown",
                        handler.as_ref().dyn_ref().unwrap(),
                    );
                }
                false // Remove from vector
            } else {
                true // Keep in vector
            }
        });
    });
}
