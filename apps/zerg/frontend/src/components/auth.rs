//! Google Sign-In overlay (Stage 4).
//!
//! For the very first increment we only expose **minimal** functionality so that
//! other parts of the codebase can call [`init_google_signin`] at startup.  The
//! function injects a small JS glue-code snippet that initialises the Google
//! Identity library once it has loaded and forwards the *ID token* to the
//! Rust side where we call `ApiClient::google_auth_login()`.

use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;
use wasm_bindgen::JsCast;
use web_sys::{Document, Element, HtmlElement};

use std::cell::RefCell;
use std::rc::Rc;

use crate::messages::Message;
use crate::network::api_client::ApiClient;
use crate::state::dispatch_global_message;

/// Called by JS when `google.accounts.id.initialize()` fires the *credential*
/// callback.
#[wasm_bindgen]
pub async fn google_credential_received(id_token: String) {
    // Add `loading` class while we perform the network request so the CSS
    // spinner (see styles.css) is visible.
    let overlay_el_opt = web_sys::window()
        .and_then(|w| w.document())
        .and_then(|doc| doc.get_element_by_id("global-login-overlay"));

    if let Some(ref el) = overlay_el_opt {
        let mut cls = el.class_name();
        if !cls.contains("loading") {
            cls.push_str(" loading");
            el.set_class_name(&cls.trim());
        }
    }

    // Send id_token to backend → persist JWT → update logged_in flag.
    if let Err(e) = ApiClient::google_auth_login(&id_token).await {
        web_sys::console::error_1(&e);
        if let Some(el) = overlay_el_opt {
            el.set_class_name("login-overlay"); // remove loading state
        }
        return;
    }

    // ---------------------------------------------------------------------
    // Step 2: Fetch the user profile & update global state
    // ---------------------------------------------------------------------
    let profile_json = match ApiClient::fetch_current_user().await {
        Ok(j) => j,
        Err(e) => {
            web_sys::console::error_1(&JsValue::from(e));
            "{}".to_string()
        }
    };

    if let Ok(user) = serde_json::from_str::<crate::models::CurrentUser>(&profile_json) {
        dispatch_global_message(Message::CurrentUserLoaded(user));
    }

    // Hide login overlay if present.
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            if let Some(el) = document.get_element_by_id("global-login-overlay") {
                el.set_class_name("hidden");
            }
        }
    }

    // Continue normal application bootstrap now that we are authenticated.
    if let Some(window) = web_sys::window() {
        if let Some(document) = window.document() {
            // Ignore errors – if bootstrap was already run nothing happens.
            let _ = crate::bootstrap_app_after_login(&document);
        }
    }
}

/// Injects a simple *Sign-in with Google* button overlay.  Meant to be called
/// exactly once during application startup **if** the user is not yet logged
/// in.
pub fn mount_login_overlay(document: &Document, client_id: &str) {
    // Prevent duplicate overlays (shouldn’t really happen but stay safe).
    if document.get_element_by_id("global-login-overlay").is_some() {
        return;
    }

    let body = document.body().expect("<body> element missing");

    // Root overlay element -------------------------------------------------
    let overlay: HtmlElement = document
        .create_element("div")
        .expect("create overlay div")
        .dyn_into::<HtmlElement>()
        .unwrap();
    overlay.set_id("global-login-overlay");
    overlay.set_class_name("login-overlay");
    
    // Add inline styles to center the overlay with subtle backdrop
    overlay.set_attribute("style", 
        "position: fixed; \
         top: 0; left: 0; right: 0; bottom: 0; \
         background: rgba(255, 255, 255, 0.1); \
         backdrop-filter: blur(4px); \
         display: flex; \
         align-items: center; \
         justify-content: center; \
         z-index: 1000;"
    ).unwrap();

    // Container for Google button ------------------------------------------
    let btn_holder: Element = document.create_element("div").expect("create btn holder");
    btn_holder.set_attribute("id", "google-btn-holder").unwrap();
    overlay.append_child(&btn_holder).unwrap();

    body.append_child(&overlay).unwrap();

    // Attempt to initialise Google Identity library.  If the external script
    // has not yet loaded we install a small polling interval that retries
    // every ~250 ms until the global `google.accounts` namespace becomes
    // available.

    if attempt_google_init(document, client_id) {
        return; // success → nothing else to do.
    }

    // Google script not ready yet – set up retry interval.
    let win = match web_sys::window() {
        Some(w) => w,
        None => return,
    };

    // We need to be able to clear the interval once init succeeds; store the
    // interval ID inside an Rc<RefCell<…>> so the closure can access it.
    let interval_handle: Rc<RefCell<Option<i32>>> = Rc::new(RefCell::new(None));
    let interval_handle_clone = Rc::clone(&interval_handle);

    // Clone values moved into closure.
    let doc_clone = document.clone();
    let client_id_owned = client_id.to_string();

    let closure = Closure::wrap(Box::new(move || {
        if attempt_google_init(&doc_clone, &client_id_owned) {
            if let Some(id) = *interval_handle_clone.borrow() {
                let _ = web_sys::window()
                    .expect("window")
                    .clear_interval_with_handle(id);
            }
        }
    }) as Box<dyn FnMut()>);

    // Start polling every 250 ms.
    let int_id = win
        .set_interval_with_callback_and_timeout_and_arguments_0(
            closure.as_ref().unchecked_ref(),
            250,
        )
        .expect("setInterval failed");
    *interval_handle.borrow_mut() = Some(int_id);

    // Prevent closure from being dropped.
    closure.forget();
}

// ---------------------------------------------------------------------------------
// Helper – tries to initialise google.accounts.id.  Returns `true` on success or
// `false` if the external Google Identity script has not yet loaded.
// ---------------------------------------------------------------------------------

fn attempt_google_init(document: &Document, client_id: &str) -> bool {
    use js_sys::{Function, Object, Reflect};

    let window = match web_sys::window() {
        Some(w) => w,
        None => return false,
    };

    // Access `google.accounts.id` hierarchy via JS reflection.
    let google_val = Reflect::get(&window, &"google".into()).unwrap_or(JsValue::UNDEFINED);
    if google_val.is_undefined() {
        return false; // Script not yet ready → caller will retry.
    }

    let accounts_val = Reflect::get(&google_val, &"accounts".into()).unwrap();
    let id_val = Reflect::get(&accounts_val, &"id".into()).unwrap();

    // ------------------------------------------------------------------
    // Build the credential callback → forwards the token to Rust async fn.
    // ------------------------------------------------------------------

    let credential_cb = Closure::wrap(Box::new(move |resp: JsValue| {
        if let Ok(token) = Reflect::get(&resp, &"credential".into()) {
            if let Some(token_str) = token.as_string() {
                wasm_bindgen_futures::spawn_local(async move {
                    crate::components::auth::google_credential_received(token_str).await;
                });
            }
        }
    }) as Box<dyn FnMut(JsValue)>);

    // ------------------------------------------------------------------
    // google.accounts.id.initialize({...})
    // ------------------------------------------------------------------

    let init_fn = match Reflect::get(&id_val, &"initialize".into()) {
        Ok(v) => v.dyn_into::<Function>().expect("initialize not a Function"),
        Err(_) => return false,
    };

    let init_opts = Object::new();
    let _ = Reflect::set(
        &init_opts,
        &"client_id".into(),
        &JsValue::from_str(client_id),
    );
    let _ = Reflect::set(&init_opts, &"callback".into(), credential_cb.as_ref());

    let _ = init_fn.call1(&id_val, &init_opts);

    // ------------------------------------------------------------------
    // google.accounts.id.renderButton(element, {theme: 'outline', size:'large'})
    // ------------------------------------------------------------------

    let render_fn = Reflect::get(&id_val, &"renderButton".into())
        .expect("renderButton missing")
        .dyn_into::<Function>()
        .expect("renderButton not a Function");

    let btn_holder_el = match document.get_element_by_id("google-btn-holder") {
        Some(el) => el,
        None => return false,
    };

    let render_opts = Object::new();
    let _ = Reflect::set(&render_opts, &"theme".into(), &"outline".into());
    let _ = Reflect::set(&render_opts, &"size".into(), &"large".into());

    let _ = render_fn.call2(&id_val, &btn_holder_el.into(), &render_opts);

    // Prevent the closure from being dropped (callback must stay alive while
    // the page is open).  We *leak* it intentionally – the OS will reclaim
    // memory when the tab is closed.
    credential_cb.forget();

    true
}
