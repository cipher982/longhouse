//! dom_utils.rs – thin helper layer for repetitive DOM operations.
//!
//! Inspired by the checklist in `ui_robustness_task.md` this module exposes
//! small, **ergonomic** wrappers for common show / hide / activate patterns
//! without sprinkling `set_attribute("style", …)` calls across the code-base.

use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlElement, HtmlInputElement};

/// Remove the `hidden` attribute so the element becomes visible.
pub fn show(el: &Element) {
    let _ = el.class_list().remove_1("hidden");
    let _ = el.class_list().add_1("visible");
    let _ = el.remove_attribute("hidden");
}

/// Hide the element by toggling CSS classes and setting [hidden] attr for test/a11y.
pub fn hide(el: &Element) {
    let _ = el.class_list().remove_1("visible");
    let _ = el.class_list().add_1("hidden");
    let _ = el.set_attribute("hidden", "");
}

/// Mark a tab button as the active one (adds "tab-button active" class).
pub fn set_active(btn: &Element) {
    btn.set_class_name("tab-button active");
}

/// Remove the `active` modifier from a tab button.
pub fn set_inactive(btn: &Element) {
    btn.set_class_name("tab-button");
}

/// Fetch an `<input>` element by id and cast it to `HtmlInputElement`.
///
/// Panics when the element is missing or is of a different type.  Intended
/// for *fixed* DOM fragments that should always exist.
#[allow(dead_code)] // helper for vanilla DOM experiments
pub fn html_input(id: &str) -> HtmlInputElement {
    web_sys::window()
        .and_then(|w| w.document())
        .and_then(|d| d.get_element_by_id(id))
        .and_then(|e| e.dyn_into::<HtmlInputElement>().ok())
        .expect(&format!("<input id='{}'> not found or wrong type", id))
}

// ---------------------------------------------------------------------------
// Focus Management Utilities
// ---------------------------------------------------------------------------

use std::cell::RefCell;

// Thread-local storage for the previously focused element
thread_local! {
    static PREVIOUS_FOCUS: RefCell<Option<HtmlElement>> = RefCell::new(None);
}

/// Focus the first interactive element within the given container.
/// Returns true if an element was focused, false otherwise.
pub fn focus_first_interactive(container: &Element) -> bool {
    let focusable_selectors = [
        "input:not([disabled]):not([type='hidden'])",
        "button:not([disabled])",
        "textarea:not([disabled])",
        "select:not([disabled])",
        "a[href]",
        "[tabindex]:not([tabindex='-1'])",
    ];

    for selector in &focusable_selectors {
        if let Ok(Some(element)) = container.query_selector(selector) {
            if let Ok(html_element) = element.dyn_into::<HtmlElement>() {
                let _ = html_element.focus();
                return true;
            }
        }
    }
    false
}

/// Get all focusable elements within a container.
/// Returns a vector of all focusable elements found.
/// Note: This finds the first element of each type due to web-sys API limitations.
pub fn get_focusable_elements(container: &Element) -> Vec<HtmlElement> {
    let mut focusable = Vec::new();

    // Since Element doesn't have query_selector_all, we'll iterate through each selector type
    let selectors = [
        "input:not([disabled]):not([type='hidden'])",
        "button:not([disabled])",
        "textarea:not([disabled])",
        "select:not([disabled])",
        "a[href]",
        "[tabindex]:not([tabindex='-1'])",
    ];

    for selector in &selectors {
        if let Ok(Some(element)) = container.query_selector(selector) {
            if let Ok(html_element) = element.dyn_into::<HtmlElement>() {
                focusable.push(html_element);
            }
        }
    }

    focusable
}

/// Store the currently focused element for later restoration.
pub fn store_active_element(document: &Document) -> Option<HtmlElement> {
    let active = document
        .active_element()
        .and_then(|el| el.dyn_into::<HtmlElement>().ok());

    // Store in thread-local storage
    PREVIOUS_FOCUS.with(|prev| {
        *prev.borrow_mut() = active.clone();
    });

    active
}

/// Restore focus to a previously stored element.
pub fn restore_focus(element: Option<HtmlElement>) {
    if let Some(el) = element {
        let _ = el.focus();
    }
}

/// Restore focus to the element stored in thread-local storage.
pub fn restore_previous_focus() {
    PREVIOUS_FOCUS.with(|prev| {
        if let Some(el) = prev.borrow().as_ref() {
            let _ = el.focus();
        }
    });
}

/// Clear the stored previous focus element.
pub fn clear_previous_focus() {
    PREVIOUS_FOCUS.with(|prev| {
        *prev.borrow_mut() = None;
    });
}

/// Check if an element is focusable.
pub fn is_focusable(element: &Element) -> bool {
    if let Ok(_html_element) = element.clone().dyn_into::<HtmlElement>() {
        // Check if element is disabled
        if element.has_attribute("disabled") {
            return false;
        }

        // Check if element has negative tabindex
        if let Some(tabindex) = element.get_attribute("tabindex") {
            if let Ok(index) = tabindex.parse::<i32>() {
                if index < 0 {
                    return false;
                }
            }
        }

        // Check if element is naturally focusable
        let tag_name = element.tag_name().to_lowercase();
        matches!(
            tag_name.as_str(),
            "input" | "button" | "textarea" | "select" | "a"
        ) || element.has_attribute("tabindex")
    } else {
        false
    }
}

// ---------------------------------------------------------------------------
// Overlay portal helper
// ---------------------------------------------------------------------------

/// Append the given element to the global `#overlay-root` portal.  Falls back
/// to `<body>` when the element is absent (e.g. dev HTML not yet updated).
pub fn mount_in_overlay(el: &Element) {
    let window = web_sys::window().expect("no window");
    if let Some(doc) = window.document() {
        if let Some(portal) = doc.get_element_by_id("overlay-root") {
            let _ = portal.append_child(el);
            return;
        }
        // Fallback – shouldn’t happen but keeps behaviour sane in older HTML builds
        let _ = doc.body().unwrap().append_child(el);
    }
}

// ---------------------------------------------------------------------------
// Unit tests (run with `cargo test --lib` in the frontend crate)
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {

    // wasm-bindgen unit tests have to run in a wasm-bindgen test
    // environment.  Here we only test the **string** manipulation helpers so
    // we guard with `#[cfg(not(target_arch = "wasm32"))]` to skip when
    // compiled for the browser.

    #[cfg(not(target_arch = "wasm32"))]
    #[test]
    fn class_helpers() {
        // Simple smoke test that the helpers compile on non-wasm targets.
        // Real DOM tests run in the Playwright suite.
        fn dummy(el: &mut web_sys::Element) {
            super::set_active(el);
            super::set_inactive(el);
        }

        // The dummy function is never called but ensures the generic code
        // path type-checks on non-wasm builds.
        let _ = dummy;
    }
}
