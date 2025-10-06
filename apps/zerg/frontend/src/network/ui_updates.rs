use js_sys::Array;
use std::cell::RefCell;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;

// Track packet counter for activity indicator
thread_local! {
    static PACKET_COUNTER: RefCell<u32> = RefCell::new(0);
}

pub fn update_connection_status(status: &str, color: &str) {
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            if let Some(status_element) = document.get_element_by_id("global-status") {
                status_element.set_class_name(color);
                status_element.set_inner_html(&format!("Status: {}", status));
            }

            // Update the coloured badge if present.  We set the *inline*
            // background colour so the indicator works without relying on a
            // global stylesheet (useful for our wasm-pack tests).
            if let Some(badge_el) = document.get_element_by_id("ws-badge") {
                // Map semantic colour names to HEX codes (fallback to the
                // provided string verbatim so callers can pass CSS colours).
                let hex = match color {
                    "green" => "#2ecc71",
                    "yellow" => "#f1c40f",
                    "red" => "#e74c3c",
                    other => other,
                };

                // Only attempt to set style when element is HtmlElement.
                if let Some(html_el) = badge_el.dyn_ref::<web_sys::HtmlElement>() {
                    let _ = html_el.style().set_property("background", hex);
                }
            }
        }
    }
}

pub fn flash_activity() {
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            if let Some(status_element) = document.get_element_by_id("global-api-status") {
                // Update packet counter
                PACKET_COUNTER.with(|counter| {
                    let count = *counter.borrow();
                    *counter.borrow_mut() = count.wrapping_add(1);
                    status_element.set_inner_html(&format!("PKT: {:08X}", count));
                });

                // Flash the LED
                status_element.set_class_name("packet-counter flash");

                // Remove flash after 200ms
                let status_clone = status_element.clone();
                let clear_callback = Closure::wrap(Box::new(move || {
                    status_clone.set_class_name("packet-counter");
                }) as Box<dyn FnMut()>);

                window
                    .set_timeout_with_callback_and_timeout_and_arguments(
                        clear_callback.as_ref().unchecked_ref(),
                        200, // Longer flash for better visibility
                        &Array::new(),
                    )
                    .expect("Failed to set timeout");

                clear_callback.forget();
            }
        }
    }
}

/// Update the *layout* persistence status area at the bottom bar.
/// `color` should be a CSS class such as "red", "yellow", "green" that the
/// stylesheet already defines.  The function is a no-op when the DOM element
/// does not exist yet (e.g. very early during bootstrap).
pub fn update_layout_status(msg: &str, color: &str) {
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            if let Some(el) = document.get_element_by_id("global-layout-status") {
                // Remove any previously set colour classes before adding the
                // new one to avoid class-name accumulation or stale styles.
                let class_list = el.class_list();
                for c in ["red", "yellow", "green"] {
                    let _ = class_list.remove_1(c);
                }
                let _ = class_list.add_1(color);
                el.set_text_content(Some(msg));
            }
        }
    }
}

/// Display a red, dismissible banner at the top of the page when an
/// authentication error occurs (e.g. WebSocket close code **4401**).  The
/// banner is inserted only once – subsequent calls simply make it visible
/// again.
pub fn show_auth_error_banner() {
    // Attempt to obtain window + document – bail quietly on failure so we do
    // not panic inside error-handling paths.
    let window = match web_sys::window() {
        Some(w) => w,
        None => return,
    };
    let document = match window.document() {
        Some(d) => d,
        None => return,
    };

    // Either fetch the existing element or create a new one.
    let banner_el = if let Some(el) = document.get_element_by_id("auth-error-banner") {
        el
    } else {
        // Create a new banner element.
        let el = match document.create_element("div") {
            Ok(e) => e,
            Err(_) => return,
        };
        el.set_id("auth-error-banner");
        el.set_class_name("error-banner");
        el.set_inner_html("Authentication required – please sign in again.");

        // Inline minimal styling so the banner is visible even without
        // external CSS bundles.
        let style = "position:fixed;top:0;left:0;width:100%;padding:6px 10px;\
                     background:#c00;color:#fff;font-weight:600;z-index:9999;\
                     text-align:center;box-shadow:0 2px 4px rgba(0,0,0,0.2);";
        let _ = el.set_attribute("style", style);

        // Insert right after <body> start so it spans full width.
        if let Some(body) = document.body() {
            // Insert as first child so it sits above any other content.
            let first_child = body.first_child();
            let _ = match first_child {
                Some(ref node) => body.insert_before(&el, Some(node)),
                None => body.append_child(&el),
            };
        }

        el
    };

    // Ensure the banner is visible (in case a previous call hid it).
    if let Some(mut existing) = banner_el.get_attribute("style") {
        if !existing.contains("display") {
            existing.push_str("display:block;");
            let _ = banner_el.set_attribute("style", &existing);
        }
    }
}
