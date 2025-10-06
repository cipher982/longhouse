//! Utility helpers shared across the WASM frontend.

use wasm_bindgen::{prelude::wasm_bindgen, JsValue};
use wasm_bindgen::JsCast;
use js_sys::{Function, Reflect};

/// Format a duration given in **milliseconds** into a short human-readable
/// string such as `"1 m 23 s"` or `"12 s"`.
///
/// The goal is to keep the output compact (fits into table cell) while still
/// being understandable.
pub fn format_duration_ms(ms: u64) -> String {
    let secs_total = ms / 1000;
    let minutes = secs_total / 60;
    let seconds = secs_total % 60;

    if minutes > 0 {
        format!("{} m {:02} s", minutes, seconds)
    } else {
        format!("{} s", seconds)
    }
}

/// Return the current timestamp in **milliseconds** since UNIX epoch.
///
/// We use JS Date here because it is available in browser/WASM without adding
/// heavy chrono dependencies.
pub fn now_ms() -> u64 {
    // `js_sys::Date::now()` returns f64 representing milliseconds since epoch.
    js_sys::Date::now() as u64
}

/// Helper for pretty-printing a floating USD cost value.
///
/// Rounds to 3–4 decimals depending on magnitude so small values like 0.0007
/// are not rendered as 0.00.
pub fn format_cost_usd(cost: f64) -> String {
    if cost >= 0.1 {
        format!("${:.2}", cost)
    } else if cost >= 0.01 {
        format!("${:.3}", cost)
    } else {
        format!("${:.4}", cost)
    }
}

/// Parse an ISO-8601 datetime string and return milliseconds since epoch.
/// Returns None when the string cannot be parsed.
pub fn parse_iso_ms(iso: &str) -> Option<u64> {
    let date = js_sys::Date::new(&JsValue::from_str(iso));
    let ms = date.get_time();
    if ms.is_nan() {
        None
    } else {
        Some(ms as u64)
    }
}

/// Capitalise the first letter of a &str.
pub fn capitalise_first(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        None => String::new(),
        Some(f) => f.to_uppercase().collect::<String>() + c.as_str(),
    }
}

// ---------------------------------------------------------------------------
// Authentication helpers
// ---------------------------------------------------------------------------

/// Remove the persisted JWT from localStorage.
pub fn clear_stored_jwt() {
    if let Some(window) = web_sys::window() {
        if let Ok(Some(storage)) = window.local_storage() {
            let _ = storage.remove_item("zerg_jwt");
        }
    }
}

/// Returns the JWT stored in localStorage, if any.
pub fn current_jwt() -> Option<String> {
    if let Some(window) = web_sys::window() {
        if let Ok(Some(storage)) = window.local_storage() {
            return storage.get_item("zerg_jwt").ok().flatten();
        }
    }
    None
}

/// Convert base64url string to standard base64 (adds padding as needed)
fn base64url_to_base64(input: &str) -> String {
    let mut s = input.replace('-', "+").replace('_', "/");
    let rem = s.len() % 4;
    if rem != 0 {
        s.push_str(&"=".repeat(4 - rem));
    }
    s
}

/// Decode a JWT payload and return it as a JSON value.
fn decode_jwt_payload(token: &str) -> Option<serde_json::Value> {
    let parts: Vec<&str> = token.split('.').collect();
    if parts.len() != 3 { return None; }
    let payload_b64 = base64url_to_base64(parts[1]);

    // Use window.atob to decode base64 to a (latin1) string; JWT payload is ASCII JSON
    let window = web_sys::window()?;
    let atob = Reflect::get(&window, &JsValue::from_str("atob")).ok()?;
    let atob_fn: Function = atob.dyn_into().ok()?;
    let decoded = atob_fn.call1(&window, &JsValue::from_str(&payload_b64)).ok()?.as_string()?;

    serde_json::from_str::<serde_json::Value>(&decoded).ok()
}

/// Return JWT expiration as milliseconds since epoch, if present.
pub fn jwt_expiration_ms(token: &str) -> Option<u64> {
    let payload = decode_jwt_payload(token)?;
    let exp_secs = payload.get("exp")?.as_u64()?;
    Some(exp_secs * 1000)
}

/// Quick check: return true if token is expired (with skew seconds).
pub fn is_jwt_expired(token: &str, skew_seconds: u64) -> bool {
    if let Some(exp_ms) = jwt_expiration_ms(token) {
        let now_ms = now_ms();
        let skew_ms = skew_seconds * 1000;
        return now_ms + skew_ms >= exp_ms;
    }
    // If we cannot parse exp, be conservative and consider it invalid
    true
}

/// Fully logs the user out: clears JWT, closes WebSocket, shows login overlay.
#[wasm_bindgen]
pub fn logout() -> Result<(), JsValue> {
    // If already logged out, do nothing (idempotent).
    let mut was_logged_in = false;

    crate::state::APP_STATE.with(|st| {
        let mut s = st.borrow_mut();
        was_logged_in = s.logged_in;
        if !was_logged_in {
            return; // early exit – avoids duplicate overlay creation
        }

        s.logged_in = false;

        // Close any active websocket connection
        let _ = s.ws_client.borrow_mut().close();
    });

    if !was_logged_in {
        return Ok(());
    }

    clear_stored_jwt();

    // Show overlay again
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            // If overlay not present create it.
            if document.get_element_by_id("global-login-overlay").is_none() {
                let client_id: String = crate::state::APP_STATE
                    .with(|st| st.borrow().google_client_id.clone().unwrap_or_default());
                crate::components::auth::mount_login_overlay(&document, &client_id);
            } else if let Some(el) = document.get_element_by_id("global-login-overlay") {
                el.set_class_name("login-overlay");
            }
        }
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Debug overlay & logging (compiled only in debug builds)
// ---------------------------------------------------------------------------

#[cfg(debug_assertions)]
pub mod debug {
    use std::collections::VecDeque;
    use std::cell::RefCell;
    use web_sys::CanvasRenderingContext2d;

    /// Maximum ring buffer capacity.
    #[allow(dead_code)] // Referenced only when `debug_log!` is invoked in dev builds
    pub const RING_CAP: usize = 200;

    // Independent debug ring buffer - completely separate from APP_STATE
    thread_local! {
        static DEBUG_RING: RefCell<VecDeque<String>> = RefCell::new(VecDeque::new());
    }

    /// Add a message to the debug ring buffer (independent of APP_STATE)
    pub fn add_to_debug_ring(msg: String) {
        DEBUG_RING.with(|ring| {
            let mut ring = ring.borrow_mut();
            if ring.len() >= RING_CAP {
                ring.pop_front();
            }
            ring.push_back(msg);
        });
    }

    /// Get a copy of the debug ring for display
    pub fn get_debug_ring() -> VecDeque<String> {
        DEBUG_RING.with(|ring| ring.borrow().clone())
    }

    /// Draw the translucent overlay that shows the last few debug lines.
    pub fn draw_overlay(ctx: &CanvasRenderingContext2d, ring: &VecDeque<String>) {
        if ring.is_empty() {
            return;
        }

        const PADDING: f64 = 4.0;
        const LINE_HEIGHT: f64 = 14.0;
        const MAX_LINES: usize = 10;

        let lines: Vec<&String> = ring.iter().rev().take(MAX_LINES).collect();
        let height = (lines.len() as f64) * LINE_HEIGHT + PADDING * 2.0;
        let width = 400.0;

        ctx.save();
        // `set_fill_style` is deprecated in `web_sys` 0.3.77.
        // Use the string-specific setter instead to silence the warning.
        ctx.set_fill_style_str("rgba(0,0,0,0.6)");
        ctx.fill_rect(0.0, 0.0, width, height);
        ctx.set_font("12px monospace");
        ctx.set_fill_style_str("#8aff8a");

        for (idx, line) in lines.iter().enumerate() {
            let y = PADDING + LINE_HEIGHT * (idx as f64 + 1.0) - 3.0;
            let _ = ctx.fill_text(line, PADDING, y);
        }

        ctx.restore();
    }
}

// ---------------------------------------------------------------------------
// debug_log! macro (noop in release)
// ---------------------------------------------------------------------------

#[macro_export]
macro_rules! debug_log {
    ($($t:tt)*) => {{
        #[cfg(debug_assertions)]
        {
            let msg = format!($($t)*);
            web_sys::console::log_1(&msg.clone().into());
            
            // Debug ring is separate from application state to avoid circular dependencies
            // The debug overlay will read from this separate ring buffer
            $crate::utils::debug::add_to_debug_ring(msg);
        }
    }};
}

// ---------------------------------------------------------------------------
// info_log!/warn_log!/error_log! helpers
// ---------------------------------------------------------------------------

#[macro_export]
macro_rules! info_log {
    ($($t:tt)*) => {{
        #[cfg(debug_assertions)]
        web_sys::console::log_1(&format!($($t)*).into());
    }};
}

#[macro_export]
macro_rules! warn_log {
    ($($t:tt)*) => {{
        web_sys::console::warn_1(&format!($($t)*).into());
    }};
}

#[macro_export]
macro_rules! error_log {
    ($($t:tt)*) => {{
        web_sys::console::error_1(&format!($($t)*).into());
    }};
}

// wasm-bindgen tests ----------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_bindgen_test::*;

    wasm_bindgen_test_configure!(run_in_browser);

    #[wasm_bindgen_test]
    fn test_format_duration_ms() {
        assert_eq!(format_duration_ms(1_500), "1 s");
        assert_eq!(format_duration_ms(12_000), "12 s");
        assert_eq!(format_duration_ms(65_000), "1 m 05 s");
    }

    // ------------------------------------------------------------------
    // Authentication helper tests
    // ------------------------------------------------------------------

    #[wasm_bindgen_test]
    fn test_jwt_storage_helpers() {
        // Ensure clean slate
        clear_stored_jwt();
        assert!(current_jwt().is_none());

        // Store a token via JS localStorage
        let window = web_sys::window().unwrap();
        let storage = window.local_storage().unwrap().unwrap();
        storage.set_item("zerg_jwt", "testtoken").unwrap();

        assert_eq!(current_jwt().as_deref(), Some("testtoken"));

        clear_stored_jwt();
        assert!(current_jwt().is_none());
    }

    #[wasm_bindgen_test]
    fn test_logout_clears_token_and_overlay() {
        // Prepare environment ------------------------------------------------
        let window = web_sys::window().unwrap();
        let document = window.document().unwrap();

        // Inject fake GOOGLE_CLIENT_ID so overlay creation works.
        js_sys::Reflect::set(
            &window,
            &"GOOGLE_CLIENT_ID".into(),
            &"dummy_client_id".into(),
        )
        .unwrap();

        // Ensure no overlay initially
        if let Some(el) = document.get_element_by_id("global-login-overlay") {
            el.parent_node().unwrap().remove_child(&el).unwrap();
        }

        // Store a JWT
        let storage = window.local_storage().unwrap().unwrap();
        storage.set_item("zerg_jwt", "dummyjwt").unwrap();

        // Manually mark app as logged-in
        crate::state::APP_STATE.with(|s| {
            s.borrow_mut().logged_in = true;
        });

        // Call logout()
        logout().unwrap();

        // Assertions ---------------------------------------------------------
        assert!(current_jwt().is_none());

        crate::state::APP_STATE.with(|s| {
            assert_eq!(s.borrow().logged_in, false);
        });

        assert!(document.get_element_by_id("global-login-overlay").is_some());
    }
}
