#![allow(dead_code)]

use std::cell::RefCell;

thread_local! {
    static SHORTCUT_HANDLER: RefCell<Option<wasm_bindgen::closure::Closure<dyn FnMut(web_sys::KeyboardEvent)>>> = RefCell::new(None);
}

/// Register the global keyboard shortcut handler if not already.
pub fn register_global_shortcuts(document: &web_sys::Document) {
    use wasm_bindgen::closure::Closure;
    // If already registered, do nothing
    SHORTCUT_HANDLER.with(|cell| {
        if cell.borrow().is_some() { return; }
        let keydown_cb = Closure::wrap(Box::new(move |event: web_sys::KeyboardEvent| {
            // Prevent global shortcuts from firing inside input fields or editable areas.
            if let Some(active_el) = web_sys::window()
                .and_then(|w| w.document())
                .and_then(|d| d.active_element())
            {
                let tag = active_el.node_name();
                let is_input = tag == "INPUT" || tag == "TEXTAREA";
                let is_editable = active_el.dyn_ref::<web_sys::HtmlElement>().map_or(false, |el| el.is_content_editable());
                if is_input || is_editable {
                    // User is typing in input or editable, skip shortcuts
                    return;
                }
            }
            let key = event.key();
            // ? with no modifiers → show shortcut help
            if key == "?" && !event.ctrl_key() && !event.meta_key() {
                event.prevent_default();
                crate::toast::info("Shortcuts:\nN – New agent\nR – Run focused agent\n↑/↓ – Move row focus\nEnter – Expand row\n? – Show this help");
                return;
            }
            // Global dashboard-only shortcuts without modifiers --------------
            use crate::storage::ActiveView;
            let active_view = crate::state::APP_STATE.with(|s| s.borrow().active_view.clone());
            if active_view == ActiveView::Dashboard && !event.ctrl_key() && !event.meta_key() {
                match key.as_str() {
                    "n" | "N" => {
                        event.prevent_default();
                        let agent_name = format!(
                            "{} {}",
                            crate::constants::DEFAULT_AGENT_NAME,
                            (js_sys::Math::random() * 100.0).round()
                        );
                        crate::state::dispatch_global_message(crate::messages::Message::RequestCreateAgent {
                            name: agent_name,
                            system_instructions: crate::constants::DEFAULT_SYSTEM_INSTRUCTIONS.to_string(),
                            task_instructions: crate::constants::DEFAULT_TASK_INSTRUCTIONS.to_string(),
                        });
                    }
                    "r" | "R" => {
                        event.prevent_default();
                        if let Some(active_el) = web_sys::window().and_then(|w| w.document()).and_then(|d| d.active_element()) {
                            if let Some(agent_id_attr) = active_el.get_attribute("data-agent-id") {
                                if let Ok(agent_id) = agent_id_attr.parse::<u32>() {
                                    wasm_bindgen_futures::spawn_local(async move {
                                        match crate::network::api_client::ApiClient::run_agent(agent_id).await {
                                            Ok(_) => crate::toast::success("Agent queued to run"),
                                            Err(e) => crate::toast::error(&format!("Failed to run agent: {:?}", e)),
                                        }
                                    });
                                }
                            }
                        }
                    }
                    _ => {}
                }
            }
        }) as Box<dyn FnMut(_)>);
        let target: web_sys::EventTarget = document.clone().into();
        let _ = target.add_event_listener_with_callback("keydown", keydown_cb.as_ref().unchecked_ref());
        cell.replace(Some(keydown_cb));
    });
}

/// Remove global keyboard shortcuts handler (power mode off)
pub fn remove_global_shortcuts(document: &web_sys::Document) {
    use web_sys::EventTarget;
    SHORTCUT_HANDLER.with(|cell| {
        if let Some(cb) = cell.borrow_mut().take() {
            let target: EventTarget = document.clone().into();
            let _ =
                target.remove_event_listener_with_callback("keydown", cb.as_ref().unchecked_ref());
        }
    });
}
//---------------------------------------------------------------------------
// Temporary lint relaxations ------------------------------------------------
//---------------------------------------------------------------------------

// Continue the incremental clean-up – we silence *dead_code* warnings
// so the CI remains green while the incremental refactor is ongoing.

//---------------------------------------------------------------------------
// Temporary lint relaxations ------------------------------------------------
//---------------------------------------------------------------------------
//
// The CI pipeline now enforces `cargo clippy -D warnings`.  A handful of
// modules still trigger non-critical lints (redundant imports, dead
// code in stubs, etc.).  We allow those at the crate root so that the build
// remains green while we refactor incrementally.  **Do not** add new allows
// without a ticket – instead, fix the underlying issue.
// #![allow(
//     clippy::redundant_field_names,
//     clippy::single_component_path_imports,
//     clippy::collapsible_else_if,
//     clippy::crate_in_macro_def,
//     clippy::for_kv_map,
//     clippy::needless_borrow,
//     clippy::unnecessary_cast,
//     clippy::type_complexity,
//     clippy::unnecessary_map_or,
//     clippy::needless_return,
//     clippy::missing_const_for_thread_local,
//     clippy::bind_instead_of_map,

//     // -----------------------------------------------------------------
//     // Transitional: allow the remainder of Clippy lints so the strict
//     // `-D warnings` gate does not block CI while legacy code is still being
//     // refactored.  Remove this once we reach zero-warning baseline.
//     clippy::all,
//     deprecated,
//     dead_code,
//     unused_mut,
// )]

use crate::components::auth as components_auth;
use crate::constants::{
    ATTR_TYPE, BUTTON_TYPE_BUTTON, CSS_TAB_BUTTON, CSS_TAB_BUTTON_ACTIVE, ID_GLOBAL_CANVAS_TAB,
    ID_GLOBAL_DASHBOARD_TAB,
};
use crate::state::dispatch_global_message;
use network::ui_updates;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use wasm_bindgen_futures::spawn_local;
use web_sys::{window, Document};

// Import modules
mod canvas;
mod command_executors;
mod components;
mod constants;
pub mod generated;
mod messages;
pub mod models;
mod models_config;
mod node_builder;
pub mod network;
mod pages;
mod scheduling;
mod schema_validation;
mod state;
mod storage;
// mod thread_handlers; // REMOVED: Replaced by agent-scoped chat handling
mod toast;
mod ui;
mod update;
mod utils;
mod views;

// Tests are organized as integration tests via wasm-pack; no internal
// `mod tests` inclusion here to avoid path resolution issues.

// Dedicated unit tests for trigger invariants (kept internal to avoid path issues)
#[cfg(test)]
mod trigger_invariants_tests;

// ---------------------------------------------------------------------------
// Global keyboard shortcuts
// ---------------------------------------------------------------------------

fn init_global_shortcuts(document: &Document) {
    use wasm_bindgen::closure::Closure;
    use web_sys::EventTarget;

    let keydown_cb = Closure::wrap(Box::new(move |event: web_sys::KeyboardEvent| {
        // Prevent global shortcuts from firing inside input fields or editable areas.
        if let Some(active_el) = web_sys::window()
            .and_then(|w| w.document())
            .and_then(|d| d.active_element())
        {
            let tag = active_el.node_name();
            let is_input = tag == "INPUT" || tag == "TEXTAREA";
            let is_editable = active_el
                .dyn_ref::<web_sys::HtmlElement>()
                .map_or(false, |el| el.is_content_editable());
            if is_input || is_editable {
                // User is typing in input or editable, skip shortcuts
                return;
            }
        }

        let key = event.key();

        // ? with no modifiers → show shortcut help
        if key == "?" && !event.ctrl_key() && !event.meta_key() {
            event.prevent_default();
            crate::toast::info("Shortcuts:\nN – New agent\nR – Run focused agent\n↑/↓ – Move row focus\nEnter – Expand row\n? – Show this help");
            return;
        }

        // Global dashboard-only shortcuts without modifiers --------------
        use crate::storage::ActiveView;
        let active_view = crate::state::APP_STATE.with(|s| s.borrow().active_view.clone());

        if active_view == ActiveView::Dashboard && !event.ctrl_key() && !event.meta_key() {
            match key.as_str() {
                "n" | "N" => {
                    event.prevent_default();
                    // Generate random name similar to UI button behaviour
                    let agent_name = format!(
                        "{} {}",
                        crate::constants::DEFAULT_AGENT_NAME,
                        (js_sys::Math::random() * 100.0).round()
                    );
                    crate::state::dispatch_global_message(
                        crate::messages::Message::RequestCreateAgent {
                            name: agent_name,
                            system_instructions: crate::constants::DEFAULT_SYSTEM_INSTRUCTIONS
                                .to_string(),
                            task_instructions: crate::constants::DEFAULT_TASK_INSTRUCTIONS
                                .to_string(),
                        },
                    );
                }
                "r" | "R" => {
                    event.prevent_default();
                    // If focused row has data-agent-id, trigger run.
                    if let Some(active_el) = web_sys::window()
                        .and_then(|w| w.document())
                        .and_then(|d| d.active_element())
                    {
                        if let Some(agent_id_attr) = active_el.get_attribute("data-agent-id") {
                            if let Ok(agent_id) = agent_id_attr.parse::<u32>() {
                                // Reuse ApiClient::run_agent logic (without spinner)
                                wasm_bindgen_futures::spawn_local(async move {
                                    match crate::network::api_client::ApiClient::run_agent(agent_id)
                                        .await
                                    {
                                        Ok(_) => crate::toast::success("Agent queued to run"),
                                        Err(e) => crate::toast::error(&format!(
                                            "Failed to run agent: {:?}",
                                            e
                                        )),
                                    }
                                });
                            }
                        }
                    }
                }
                _ => {}
            }
        }
    }) as Box<dyn FnMut(_)>);

    let target: EventTarget = document.clone().into();
    let _ = target.add_event_listener_with_callback("keydown", keydown_cb.as_ref().unchecked_ref());
    keydown_cb.forget();
}
mod auth;
mod dom_utils;
pub mod reducers;
mod ui_components;

// Export convenience macros crate-wide
#[macro_use]
mod macros;

/// Basic hash router for a handful of top-level pages.
fn route_hash(hash: &str) {
    use crate::messages::Message;
    use crate::storage::ActiveView;
    match hash {
        "#/profile" => {
            crate::state::dispatch_global_message(Message::ToggleView(ActiveView::Profile))
        }
        "#/admin/ops" | "#/ops" => {
            crate::state::dispatch_global_message(Message::ToggleView(ActiveView::AdminOps))
        }
        "#/canvas" => {
            crate::state::dispatch_global_message(Message::ToggleView(ActiveView::Canvas))
        }
        "#/dashboard" | "" | "#/" => {
            crate::state::dispatch_global_message(Message::ToggleView(ActiveView::Dashboard))
        }
        _ => {}
    }
}

// Main entry point for the WASM application
#[wasm_bindgen(start)]
pub fn start() -> Result<(), JsValue> {
    // Initialize better panic messages
    console_error_panic_hook::set_once();

    // Initialize API configuration before any network operations
    // This tries to use compile-time API_BASE_URL. If unavailable, we will
    // wait for runtime initialization from bootstrap.js to avoid races.
    if let Err(e) = network::init_api_config() {
        debug_log!(
            "API config not set at compile time ({}). Waiting for runtime config…",
            e
        );
    }

    // Model list fetch moved to after runtime config init to avoid using localhost fallback
    // Models will be fetched when the dropdown is first accessed
    // if let Some(doc) = web_sys::window().and_then(|w| w.document()) {
    //     let _ = crate::components::model_selector::fetch_models_from_backend(&doc);
    // }

    // Get the document
    let window = web_sys::window().expect("no global `window` exists");
    let document = window.document().expect("should have a document on window");

    // Power-mode keyboard shortcuts are now opt-in.  The handler will be
    // installed via `Message::SetPowerMode(true)` after the user toggles the
    // switch in the profile dropdown.

    // -------------------------------------------------------------------
    // NEW: Discover runtime flags from /api/system/info before deciding on
    //       the authentication flow.  This avoids the need to synchronise
    //       env vars between backend and frontend during local dev.
    // -------------------------------------------------------------------

    // Perform the network call in a *blocking* fashion for this early stage
    // by using `spawn_local` and returning early from `start()`.  The rest of
    // the bootstrap happens inside the async block.

    let doc_clone = document.clone();
    spawn_local(async move {
        fn show_fatal_config_error(doc: &Document, msg: &str) {
            web_sys::console::error_1(&format!("FATAL: {}", msg).into());
            let overlay = doc.create_element("div").expect("create overlay");
            overlay.set_id("fatal-config-error");
            overlay.set_attribute(
                "style",
                "position:fixed;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.6);z-index:99999;",
            )
            .ok();
            let panel = doc.create_element("div").expect("create panel");
            panel
                .set_attribute(
                    "style",
                    "max-width:720px;background:#fff;border-radius:8px;padding:24px;font-family:system-ui, sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.3);",
                )
                .ok();
            let h = doc.create_element("h2").unwrap();
            h.set_inner_html("Configuration Error");
            let p = doc.create_element("pre").unwrap();
            p.set_attribute(
                "style",
                "white-space:pre-wrap;word-break:break-word;font-size:14px;margin-top:8px;",
            )
            .ok();
            p.set_text_content(Some(msg));
            panel.append_child(&h).ok();
            panel.append_child(&p).ok();
            overlay.append_child(&panel).ok();
            doc.body().unwrap().append_child(&overlay).ok();
        }
        // Ensure API configuration is initialized before making any API calls.
        // This avoids a race where #[wasm_bindgen(start)] fires before
        // bootstrap.js calls init_api_config_js().
        async fn wait_for_api_config(timeout_ms: u32) -> bool {
            use gloo_timers::future::TimeoutFuture;
            let start = js_sys::Date::now();
            loop {
                // Accept either a configured base URL (dev runtime or compile-time)
                // or same-origin (empty) when not on localhost.
                let base = crate::network::get_api_base_url().unwrap_or_default();
                if !base.is_empty() { return true; }
                if let Some(win) = web_sys::window() {
                    if let Ok(hostname) = win.location().hostname() {
                        let hn = hostname.to_lowercase();
                        if hn != "localhost" && hn != "127.0.0.1" {
                            // In production/same-origin, proceed with empty base (relative /api)
                            return true;
                        }
                    }
                }
                let elapsed = js_sys::Date::now() - start;
                if elapsed >= timeout_ms as f64 {
                    return false;
                }
                TimeoutFuture::new(25).await;
            }
        }

        if !wait_for_api_config(3000).await {
            show_fatal_config_error(&doc_clone, "API base URL not configured. In development, ensure window.API_BASE_URL is set in config.js or set API_BASE_URL at build time. In production, the app will use same-origin '/api' endpoints by default.");
            return;
        }
        // Attempt to fetch the system info endpoint.  If it fails we fall
        // back to the previous compile-time logic so production builds that
        // are already configured continue to work.

        let sys_info_json = match network::api_client::ApiClient::fetch_system_info().await {
            Ok(j) => j,
            Err(e) => {
                show_fatal_config_error(&doc_clone, &format!(
                    "Failed to fetch /api/system/info from API base '{}'.\nError: {:?}",
                    crate::network::get_api_base_url().unwrap_or_default(),
                    e
                ));
                return;
            }
        };

        #[derive(serde::Deserialize)]
        struct SysInfo {
            #[serde(default)]
            auth_disabled: bool,
            #[serde(default)]
            google_client_id: Option<String>,
        }

        let info: SysInfo = serde_json::from_str(&sys_info_json).unwrap_or(SysInfo {
            auth_disabled: false,
            google_client_id: None,
        });

        // Store google_client_id in global state for later reuse (e.g. after
        // a manual logout when we need to recreate the overlay quickly).
        state::dispatch_global_message(messages::Message::SetGoogleClientId(info.google_client_id.clone()));

        if info.auth_disabled {
            // ─── Dev mode ───
            // State will be updated via CurrentUserLoaded message

            if let Err(e) = bootstrap_app_after_login(&doc_clone) {
                web_sys::console::error_1(&e);
            }

            let dummy_user = crate::models::CurrentUser {
                id: 0,
                email: "dev@local".to_string(),
                display_name: Some("Developer".to_string()),
                avatar_url: None,
                prefs: None,
                gmail_connected: false,
            };
            dispatch_global_message(crate::messages::Message::CurrentUserLoaded(dummy_user));
            return;
        }

        // ─── Auth enabled ───
        // If we believe we're logged in due to a cached JWT, validate it first
        // by calling `/api/users/me`. Only after a successful profile fetch do
        // we bootstrap the app to avoid any pre-auth API/WebSocket traffic.
        if state::APP_STATE.with(|s| s.borrow().logged_in) {
            // Pre-check: ignore obviously expired tokens to avoid a useless network call
            if let Some(tok) = crate::utils::current_jwt() {
                if crate::utils::is_jwt_expired(&tok, 60) {
                    let _ = crate::utils::logout();
                }
            }

            // Re-read the flag after potential logout above
            if !state::APP_STATE.with(|s| s.borrow().logged_in) {
                // Fall through to show the overlay below
            } else {
                match crate::network::api_client::ApiClient::fetch_current_user().await {
                    Ok(profile_json) => {
                        if let Ok(user) = serde_json::from_str::<crate::models::CurrentUser>(&profile_json) {
                            dispatch_global_message(crate::messages::Message::CurrentUserLoaded(user));
                            if let Err(e) = bootstrap_app_after_login(&doc_clone) {
                                web_sys::console::error_1(&e);
                            }
                            return;
                        } else {
                            // Malformed profile – treat as unauthenticated
                            let _ = crate::utils::logout();
                        }
                    }
                    Err(_e) => {
                        // 401 and other failures are handled in fetch_json (logout + toast)
                        // Fall through to show the login overlay below.
                    }
                }
            }
        }

        // Otherwise show Google Sign-In overlay (needs client_id).
        let client_id = info.google_client_id.as_deref().unwrap_or("");

        if client_id.is_empty() {
            show_fatal_config_error(&doc_clone, "Authentication is enabled but google_client_id is missing in /api/system/info.\nSet GOOGLE_CLIENT_ID in the backend environment or disable auth in dev (AUTH_DISABLED=1).\nCSP must allow https://accounts.google.com.");
            return;
        }

        components_auth::mount_login_overlay(&doc_clone, client_id);
    });

    // Nothing else to do synchronously – actual bootstrap continues in async
    // block above.
    Ok(())
}

/// Performs the main UI + networking bootstrap sequence.  Called once on page
/// load *if* a JWT is already present, or after the Google sign-in flow
/// completes successfully.
pub(crate) fn bootstrap_app_after_login(document: &Document) -> Result<(), JsValue> {
    // Create base UI elements (header and status bar)
    ui::setup::create_base_ui(document)?;
    // Wire up shelf toggle, scrim and state persistence
    ui::setup::init_shelf_toggle_interactions(document)?;

    // Mount compact Ops HUD (admin-gated via REST on first fetch)
    let _ = crate::components::ops::mount_ops_hud(document);

    // --- Initialize and Connect WebSocket v2 ---
    // Access the globally initialized WS client and Topic Manager
    let (ws_client_rc, topic_manager_rc) = state::APP_STATE.with(|state_ref| {
        let state = state_ref.borrow();
        (state.ws_client.clone(), state.topic_manager.clone())
    });

    // Setup callbacks for WebSocket
    {
        let topic_manager_clone = topic_manager_rc.clone();
        let ws_client_clone = ws_client_rc.clone();

        // Borrow mutably to set callbacks
        let mut ws_client = ws_client_clone.borrow_mut();

        // Set callbacks for websocket events
        setup_websocket_callbacks(&mut ws_client, topic_manager_clone.clone())?;

        // Set initial status before attempting connection
        ui_updates::update_connection_status("Connecting", "yellow");

        // Initiate the connection
        if let Err(e) = ws_client.connect() {
            web_sys::console::error_1(&format!("Initial WebSocket connect failed: {:?}", e).into());
            // Update UI status on initial connection error
            ui_updates::update_connection_status("Error", "red");
        }
    } // Mutable borrow of ws_client ends here

    // Create the tab navigation
    create_tab_navigation(&document)?;

    // -------------------------------------------------------------------
    // Simple hash-based routing for the Profile page (and a few others)
    // -------------------------------------------------------------------

    {
        let hash_now = web_sys::window()
            .and_then(|w| w.location().hash().ok())
            .unwrap_or_default();

        // Dispatch once for the current hash on load
        route_hash(&hash_now);

        // Install listener for subsequent changes
        if let Some(win) = web_sys::window() {
            let closure =
                wasm_bindgen::closure::Closure::wrap(Box::new(move |_e: web_sys::Event| {
                    if let Ok(hash) = web_sys::window().unwrap().location().hash() {
                        route_hash(&hash);
                    }
                }) as Box<dyn FnMut(_)>);

            let _ = win
                .add_event_listener_with_callback("hashchange", closure.as_ref().unchecked_ref());
            closure.forget();
        }
    }

    // Set up shared UI components
    ui::main::setup_ui(&document)?;

    // Show initial loading spinner
    if let Some(loading_spinner) = document.get_element_by_id("loading-spinner") {
        if let Some(spinner_element) = loading_spinner.dyn_ref::<web_sys::HtmlElement>() {
            spinner_element.set_class_name("active");
        }
    }

    // Initialize data loading
    initialize_data_loading();

    // -------------------------------------------------------------------
    // If a JWT is present fetch the current user profile.  This also covers
    // the *page refresh* scenario where Google login overlay is skipped.
    // -------------------------------------------------------------------

    wasm_bindgen_futures::spawn_local(async move {
        if crate::state::APP_STATE.with(|s| s.borrow().logged_in) {
            if let Ok(profile_json) =
                crate::network::api_client::ApiClient::fetch_current_user().await
            {
                if let Ok(user) = serde_json::from_str::<crate::models::CurrentUser>(&profile_json)
                {
                    dispatch_global_message(crate::messages::Message::CurrentUserLoaded(user));
                }
            }
        }
    });

    // By default, start with dashboard view
    debug_log!("Setting initial view to Dashboard");

    // Set initial view to Dashboard via message dispatch (uses update+commands)
    dispatch_global_message(crate::messages::Message::ToggleView(
        storage::ActiveView::Dashboard,
    ));

    // Signal to automation/tests that the app finished its initial bootstrap.
    if let Some(win) = web_sys::window() {
        let key = js_sys::JsString::from("__APP_READY__");
        let _ = js_sys::Reflect::set(&win, &key, &JsValue::from_bool(true));
    }

    Ok(())
}

// Helper function to setup WebSocket callbacks
fn setup_websocket_callbacks(
    ws_client: &mut network::WsClientV2,
    topic_manager: std::rc::Rc<std::cell::RefCell<network::TopicManager>>,
) -> Result<(), JsValue> {
    // Set on_connect: Call topic_manager.resubscribe_all_topics
    let tm_on_connect = topic_manager.clone();
    ws_client.set_on_connect(move || {
        debug_log!("on_connect callback triggered");
        if let Err(e) = tm_on_connect.borrow().resubscribe_all_topics() {
            web_sys::console::error_1(&format!("Failed to resubscribe topics: {:?}", e).into());
        }
        // Update UI status
        ui_updates::update_connection_status("Connected", "green");
    });

    // Set on_message: Call topic_manager.route_incoming_message
    let tm_on_message = topic_manager.clone();
    ws_client.set_on_message(move |json_value| {
        // Route the message using the topic manager
        tm_on_message.borrow().route_incoming_message(json_value);
    });

    // Set on_disconnect
    ws_client.set_on_disconnect(|| {
        web_sys::console::warn_1(&"WebSocket disconnected (on_disconnect callback)".into());
        // Update UI status
        ui_updates::update_connection_status("Disconnected", "red");
    });

    Ok(())
}

// Helper function to initialize data loading
fn initialize_data_loading() {
    spawn_local(async {
        // Fetch available models from API
        let models_json = match network::api_client::ApiClient::fetch_available_models().await {
            Ok(json) => json,
            Err(e) => {
                web_sys::console::error_1(&format!("Failed to fetch models: {:?}", e).into());
                return;
            }
        };
        let models: Vec<crate::models_config::ModelConfig> =
            match serde_json::from_str(&models_json) {
                Ok(m) => m,
                Err(e) => {
                    web_sys::console::error_1(&format!("Failed to parse models: {:?}", e).into());
                    return;
                }
            };
        let default_model = models.iter().find(|m| m.is_default);
        if let Some(default) = default_model {
            let model_pairs = models
                .iter()
                .map(|m| (m.id.clone(), m.display_name.clone()))
                .collect();
            dispatch_global_message(crate::messages::Message::SetAvailableModels {
                models: model_pairs,
                default_model_id: default.id.clone(),
            });
        } else {
            web_sys::console::error_1(&"No default model found in backend response".into());
            return;
        }

        // Load the state (this will also try to load workflows and agents)
        state::APP_STATE.with(|s| {
            let mut app_state = s.borrow_mut();
            if let Err(e) = storage::load_state(&mut app_state) {
                web_sys::console::error_1(&format!("Error loading state: {:?}", e).into());
            }
        }); // The borrow is dropped when this block ends

        // Canvas workflow will be loaded when user switches to Canvas tab
        state::APP_STATE.with(|state| {
            let state = state.borrow();
            // No need for default workflow creation - backend handles this

            // If we have an agent selected in chat view, load their threads
            if state.active_view == storage::ActiveView::ChatView {
                if let Some(agent_id) = state.agents.keys().next().cloned() {
                    crate::state::dispatch_global_message(crate::messages::Message::LoadThreads(
                        agent_id,
                    ));
                }
            }
        });

        // Set up auto-save timer
        if let Err(e) = setup_auto_save_timer(30000) {
            // 30 seconds
            web_sys::console::error_1(&format!("Failed to setup auto-save: {:?}", e).into());
        }
    });
}

// Create tab navigation for switching between dashboard and canvas
fn create_tab_navigation(document: &Document) -> Result<(), JsValue> {
    debug_log!("create_tab_navigation called");
    // Create a tabs container
    let tabs_container = document.create_element("div")?;
    tabs_container.set_id("global-tabs-container");
    tabs_container.set_class_name("tabs-container");

    // Create dashboard tab
    let dashboard_tab = document.create_element("button")?;
    dashboard_tab.set_attribute(ATTR_TYPE, BUTTON_TYPE_BUTTON)?;
    dashboard_tab.set_id(ID_GLOBAL_DASHBOARD_TAB);
    dashboard_tab.set_attribute("data-testid", "global-dashboard-tab")?;
    dashboard_tab.set_class_name(CSS_TAB_BUTTON_ACTIVE);
    dashboard_tab.set_inner_html("Agent Dashboard");

    // Create canvas tab
    let canvas_tab = document.create_element("button")?;
    canvas_tab.set_attribute(ATTR_TYPE, BUTTON_TYPE_BUTTON)?;
    canvas_tab.set_id(ID_GLOBAL_CANVAS_TAB);
    // Expose for E2E tests – keeps parity with dashboard tab
    canvas_tab.set_attribute("data-testid", "global-canvas-tab")?;
    canvas_tab.set_class_name(CSS_TAB_BUTTON);
    canvas_tab.set_inner_html("Canvas Editor");

    // Add tabs to container
    tabs_container.append_child(&dashboard_tab)?;
    tabs_container.append_child(&canvas_tab)?;

    // Set up dashboard tab click handler
    {
        let dashboard_click = Closure::wrap(Box::new(move |_: web_sys::MouseEvent| {
            debug_log!("Dashboard tab clicked");
            dispatch_global_message(messages::Message::ToggleView(
                storage::ActiveView::Dashboard,
            ));
        }) as Box<dyn FnMut(_)>);

        dashboard_tab
            .add_event_listener_with_callback("click", dashboard_click.as_ref().unchecked_ref())?;
        dashboard_click.forget();
    }

    // Set up canvas tab click handler
    {
        let canvas_click = Closure::wrap(Box::new(move |_: web_sys::MouseEvent| {
            debug_log!("Canvas tab clicked");
            dispatch_global_message(messages::Message::ToggleView(storage::ActiveView::Canvas));
        }) as Box<dyn FnMut(_)>);

        canvas_tab
            .add_event_listener_with_callback("click", canvas_click.as_ref().unchecked_ref())?;
        canvas_click.forget();
    }

    // Add the tabs container to the document
    let header = document
        .get_element_by_id("header")
        .ok_or_else(|| JsValue::from_str("Header not found"))?;
    header.append_child(&tabs_container)?;
    debug_log!("Tabs added to DOM");

    Ok(())
}

// Set up a timer to auto-save state periodically
fn setup_auto_save_timer(interval_ms: i32) -> Result<(), JsValue> {
    let window = window().expect("no global window exists");

    // Create a closure that will be called by setInterval
    let auto_save_callback = Closure::wrap(Box::new(move || {
        state::APP_STATE.with(|state| {
            let mut state = state.borrow_mut();
            if let Err(e) = state.save_if_modified() {
                web_sys::console::warn_1(&format!("Auto-save failed: {:?}", e).into());
            }
        });
    }) as Box<dyn FnMut()>);

    // Create interval timer
    let _ = window.set_interval_with_callback_and_timeout_and_arguments(
        auto_save_callback.as_ref().unchecked_ref(),
        interval_ms,
        &js_sys::Array::new(),
    )?;

    // Forget the closure to prevent it from being dropped
    auto_save_callback.forget();

    Ok(())
}

// Debug helpers for e2e tests - expose app state info

#[cfg(debug_assertions)]
#[wasm_bindgen]
pub fn debug_get_node_count() -> usize {
    crate::state::APP_STATE.with(|state| {
        let state_ref = state.borrow();
        let count = state_ref.workflow_nodes.len();
        debug_log!("debug_get_node_count: found {} nodes", count);

        // Log all node IDs for debugging
        let node_ids: Vec<String> = state_ref.workflow_nodes.keys().cloned().collect();
        debug_log!("debug_get_node_count: node IDs = {:?}", node_ids);

        count
    })
}

#[cfg(debug_assertions)]
#[wasm_bindgen]
pub fn debug_has_trigger_node() -> bool {
    crate::state::APP_STATE.with(|state| {
        state.borrow().workflow_nodes.values().any(|node| {
            matches!(
                node.node_type,
                crate::generated::workflow::NodeType::Variant0(_)
                    | crate::generated::workflow::NodeType::Variant1(_)
            )
        })
    })
}

#[cfg(debug_assertions)]
#[wasm_bindgen]
pub fn debug_get_trigger_node_info() -> String {
    crate::state::APP_STATE.with(|state| {
        let state = state.borrow();
        if let Some(node) = state.workflow_nodes.values().next() {
            // Use helper methods for property access
            return format!(
                "{{\"id\":\"{}\",\"text\":\"{}\",\"x\":{},\"y\":{}}}",
                node.node_id,
                node.get_text(),
                node.get_x(),
                node.get_y()
            );
        }
        "null".to_string()
    })
}
