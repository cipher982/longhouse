//! UI setup helpers that are **not** specific to any modal component.
//! This module currently exposes a single public function `create_base_ui`
//! which inserts the static header & status-bar at the top of the document.

use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlElement, KeyboardEvent};

/// Build the basic, always-visible chrome (header and status bar).
/// The function is idempotent – repeated invocations early-return once
/// the `header` element already exists in the DOM.
pub fn create_base_ui(document: &Document) -> Result<(), JsValue> {
    // Bail if a header already exists – assumes full base UI created earlier.
    if document.get_element_by_id("header").is_some() {
        return Ok(());
    }

    // Header
    let header = document.create_element("div")?;
    header.set_class_name("header");
    header.set_attribute("id", "header")?;

    let title = document.create_element("h1")?;
    title.set_attribute("id", "header-title")?;
    title.set_inner_html("AI Agent Platform");
    header.append_child(&title)?;

    // Mobile shelf toggle button in header (menu → close)
    let toggle_btn = document.create_element("button")?;
    toggle_btn.set_attribute("id", "shelf-toggle-btn")?;
    toggle_btn.set_attribute("aria-label", "Open agent panel")?;
    toggle_btn.set_attribute("aria-controls", "agent-shelf")?;
    toggle_btn.set_attribute("aria-expanded", "false")?;

    // Feather icon placeholder – auto-replaced by feather-init.js
    let icon = document.create_element("i")?;
    icon.set_attribute("data-feather", "menu")?;
    toggle_btn.append_child(&icon)?;

    // Insert before the title so it sits on the left
    header.insert_before(&toggle_btn, Some(&title))?;

    // Status bar – connection + API status
    let status_bar = document.create_element("div")?;
    status_bar.set_class_name("status-bar");

    let status = document.create_element("div")?;
    status.set_id("global-status");
    status.set_class_name("yellow"); // initial colour
    status.set_inner_html("Status: Connecting");

    status_bar.append_child(&status)?;

    // -------------------------------------------------------------------
    // WebSocket connection badge (small coloured circle)
    // -------------------------------------------------------------------
    // A minimal visual indicator that changes colour based on the current
    // WebSocket connection state – green (connected), yellow (connecting),
    // red (disconnected / error).  Inline styles are used so the badge works
    // even without the full CSS bundle during local-dev or unit tests.

    let ws_badge = document.create_element("span")?;
    ws_badge.set_id("ws-badge");
    ws_badge.set_attribute(
        "style",
        "display:inline-block;width:10px;height:10px;border-radius:50%;\
         background:#f1c40f;margin-right:6px;vertical-align:middle;",
    )?;

    // Prepend badge before the textual status so they appear side-by-side.
    status_bar.insert_before(&ws_badge, Some(&status))?;

    // API packet counter (hidden LED / counter that flashes on WS packets).
    let api_status = document.create_element("div")?;
    api_status.set_id("global-api-status");
    api_status.set_class_name("packet-counter");
    api_status.set_inner_html("PKT: 00000000");
    status_bar.append_child(&api_status)?;

    // Add an (initially empty) layout status span that will be aligned to the
    // right by the flex layout (`justify-content: space-between`).  We *do
    // not* insert any text here so the bar looks identical to previous builds
    // until an error/warning is displayed.

    let layout_status = document.create_element("div")?;
    layout_status.set_id("global-layout-status");
    layout_status.set_class_name("");
    layout_status.set_inner_html("");

    status_bar.append_child(&layout_status)?;

    // Inject into DOM
    let body = document
        .body()
        .ok_or(JsValue::from_str("No <body> element found"))?;
    body.append_child(&header)?;
    // Add a scrim element for mobile drawer
    let scrim = document.create_element("div")?;
    scrim.set_id("shelf-scrim");
    scrim.set_class_name("shelf-scrim");
    body.append_child(&scrim)?;
    body.append_child(&status_bar)?;

    // With grid layout, header/tabs/status are in normal flow – no offset math needed.

    // -------------------------------------------------------------------
    // Reliability fix – ensure any *pending* canvas layout changes are
    // flushed to the backend when the user navigates away (tab close,
    // refresh, browser back gesture, etc.).  We hook into the `pagehide`
    // event which fires reliably across modern browsers whenever the page
    // is being unloaded *or* moved into the back/forward cache.  Using
    // `visibilitychange` alone is insufficient because pages kept alive in
    // bfcache do not trigger a regular unload.
    // -------------------------------------------------------------------

    if let Some(window) = web_sys::window() {
        // Closure is intentionally leaked (`forget`) because it must live
        // for the entire page lifetime – there is no corresponding remove
        // listener during normal operation.
        let on_pagehide = Closure::<dyn FnMut(web_sys::Event)>::new(move |_| {
            crate::state::APP_STATE.with(|s| {
                // Acquire a *unique* mutable borrow – panic if another borrow
                // is still active so the issue is caught during development.
                let mut st = s.borrow_mut();

                // Force-clear dragging flags so persistence logic emits the
                // final PATCH even if the user closes the tab mid-drag.
                st.canvas_dragging = false;

                // Force a persistence attempt even if `state_modified` was
                // already reset by the most recent animation tick.
                st.state_modified = true;

                let _ = st.save_if_modified();
            });
        });

        // SAFETY: `.unchecked_ref()` is safe because the closure type
        // matches the required `&Function` signature for addEventListener.
        let _ = window
            .add_event_listener_with_callback("pagehide", on_pagehide.as_ref().unchecked_ref());

        // Leak – see comment above.
        on_pagehide.forget();
    }

    Ok(())
}

fn get_shelf(document: &Document) -> Option<Element> {
    document.get_element_by_id("agent-shelf")
}

fn set_toggle_icon(document: &Document, open: bool) {
    if let Some(btn) = document.get_element_by_id("shelf-toggle-btn") {
        // Update aria
        let _ = btn.set_attribute("aria-expanded", if open { "true" } else { "false" });
        let _ = btn.set_attribute(
            "aria-label",
            if open { "Close agent panel" } else { "Open agent panel" },
        );

        // Swap icon placeholder; feather-init's MutationObserver will replace it
        let icon_name = if open { "x" } else { "menu" };
        if let Ok(icon) = document.create_element("i") {
            let _ = icon.set_attribute("data-feather", icon_name);
            // Clear previous icon
            while let Some(child) = btn.first_child() {
                let _ = btn.remove_child(&child);
            }
            let _ = btn.append_child(&icon);
        }
    }
}

fn persist_shelf_state(open: bool) {
    if let Some(win) = web_sys::window() {
        if let Ok(Some(storage)) = win.local_storage() {
            let _ = storage.set_item("agent_shelf_open", if open { "1" } else { "0" });
        }
    }
}

fn restore_persisted_shelf_state(document: &Document) -> bool {
    if let Some(win) = web_sys::window() {
        if let Ok(Some(storage)) = win.local_storage() {
            if let Ok(Some(val)) = storage.get_item("agent_shelf_open") {
                return val == "1";
            }
        }
    }
    false
}

fn open_shelf(document: &Document) {
    if let Some(shelf) = get_shelf(document) {
        let _ = shelf.class_list().add_1("open");
        if let Some(body) = document.body() {
            let _ = body.class_list().add_1("shelf-open");
        }
        // Ensure shelf can receive focus
        if let Some(el) = shelf.dyn_ref::<HtmlElement>() {
            let _ = el.set_attribute("tabindex", "-1");
            let _ = el.focus();
        }
        set_toggle_icon(document, true);
        persist_shelf_state(true);
    }
}

fn close_shelf(document: &Document) {
    if let Some(shelf) = get_shelf(document) {
        let _ = shelf.class_list().remove_1("open");
        if let Some(body) = document.body() {
            let _ = body.class_list().remove_1("shelf-open");
        }
        set_toggle_icon(document, false);
        persist_shelf_state(false);
        // Restore focus back to toggle button for accessibility
        if let Some(btn) = document.get_element_by_id("shelf-toggle-btn") {
            if let Some(btn_el) = btn.dyn_ref::<HtmlElement>() {
                let _ = btn_el.focus();
            }
        }
    }
}

// Install interactions after base UI is present
#[allow(clippy::too_many_lines)]
#[wasm_bindgen]
pub fn init_shelf_toggle_interactions(document: &Document) -> Result<(), JsValue> {
    // Toggle click
    if let Some(btn) = document.get_element_by_id("shelf-toggle-btn") {
        let doc = document.clone();
        let onclick = Closure::<dyn FnMut(_)>::new(move |_e: web_sys::Event| {
            let is_open = get_shelf(&doc)
                .map(|el| el.class_list().contains("open"))
                .unwrap_or(false);
            if is_open { close_shelf(&doc) } else { open_shelf(&doc) }
        });
        btn.add_event_listener_with_callback("click", onclick.as_ref().unchecked_ref())?;
        onclick.forget();
    }

    // Scrim click closes
    if let Some(scrim) = document.get_element_by_id("shelf-scrim") {
        let doc = document.clone();
        let on_scrim = Closure::<dyn FnMut(_)>::new(move |_e: web_sys::Event| {
            close_shelf(&doc);
        });
        scrim.add_event_listener_with_callback("click", on_scrim.as_ref().unchecked_ref())?;
        on_scrim.forget();
    }

    // ESC to close
    if let Some(win) = web_sys::window() {
        let doc = document.clone();
        let on_key = Closure::<dyn FnMut(KeyboardEvent)>::new(move |e: KeyboardEvent| {
            if e.key() == "Escape" {
                let is_open = get_shelf(&doc)
                    .map(|el| el.class_list().contains("open"))
                    .unwrap_or(false);
                if is_open {
                    e.prevent_default();
                    close_shelf(&doc);
                }
            }
        });
        win.add_event_listener_with_callback("keydown", on_key.as_ref().unchecked_ref())?;
        on_key.forget();
    }

    // Basic focus management: keep Tab within shelf when open
    if let Some(shelf) = document.get_element_by_id("agent-shelf") {
        let doc = document.clone();
        let on_keydown = Closure::<dyn FnMut(KeyboardEvent)>::new(move |e: KeyboardEvent| {
            if e.key() == "Tab" {
                // Only trap when drawer is open
                let is_open = get_shelf(&doc)
                    .map(|el| el.class_list().contains("open"))
                    .unwrap_or(false);
                if !is_open { return; }

                if let Some(shelf_el) = doc.get_element_by_id("agent-shelf") {
                    if let Some(container) = shelf_el.dyn_ref::<Element>() {
                        if let Ok(node_list) = container.query_selector_all(
                            "a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex='-1'])",
                        ) {
                            let len = node_list.length() as i32;
                            if len == 0 {
                                // Keep focus on shelf container
                                if let Some(he) = shelf_el.dyn_ref::<HtmlElement>() {
                                    let _ = he.focus();
                                }
                                e.prevent_default();
                                return;
                            }

                            // Find current index
                            let active = doc.active_element();
                            let mut idx: i32 = -1;
                            for i in 0..len {
                                if let Some(node) = node_list.item(i as u32) {
                                    if let Some(el) = node.dyn_ref::<Element>() {
                                        if Some(el.clone()) == active {
                                            idx = i;
                                            break;
                                        }
                                    }
                                }
                            }

                            // Compute next index
                            let next = if e.shift_key() {
                                if idx <= 0 { len - 1 } else { idx - 1 }
                            } else {
                                if idx < 0 || idx + 1 >= len { 0 } else { idx + 1 }
                            };

                            if let Some(node) = node_list.item(next as u32) {
                                if let Some(el) = node.dyn_ref::<HtmlElement>() {
                                    let _ = el.focus();
                                    e.prevent_default();
                                }
                            }
                        }
                    }
                }
            }
        });
        shelf.add_event_listener_with_callback("keydown", on_keydown.as_ref().unchecked_ref())?;
        on_keydown.forget();
    }

    // Restore persisted state once on init
    if restore_persisted_shelf_state(document) {
        open_shelf(document);
    } else {
        set_toggle_icon(document, false);
    }

    Ok(())
}

// No layout CSS var measurement required in grid-based layout
