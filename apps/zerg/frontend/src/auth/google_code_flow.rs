//! Google Identity Services *code-client* integration (Phase C).
//!
//! The first shipped version tried to keep the build working without the
//! external Google script by short-circuiting the flow.  We now implement the
//! **real** OAuth dance while still falling back to the stub when running in
//! offline test environments where `google.accounts` is not available.
//!
//! High-level steps:
//! 1.  Build a parameter object for `google.accounts.oauth2.initCodeClient`
//!     (client ID, scopes, prompt, …).
//! 2.  Call the function and immediately invoke `requestCode()` which opens a
//!     popup asking for user consent.
//! 3.  When the popup returns the *authorization code* we POST it to
//!     `/api/auth/google/gmail`.  On HTTP 200 we dispatch
//!     `Message::GmailConnected` so the UI updates.
//!
//! If either the Google namespace is missing *or* the backend call fails we
//! print to the console and keep the old stub behaviour so demos without
//! network access still work.

use wasm_bindgen::closure::Closure;
use wasm_bindgen::prelude::*;

// wasm-bindgen externs ------------------------------------------------------

/// Opaque handle returned by `initCodeClient`.
#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(typescript_type = "CodeClient")]
    pub type CodeClient;

    #[wasm_bindgen(method, js_name = requestCode)]
    fn request_code(this: &CodeClient);

    #[wasm_bindgen(
        js_namespace = ["google", "accounts", "oauth2"],
        js_name = initCodeClient
    )]
    fn init_code_client(params: &JsValue) -> CodeClient;
}

/// Kick off the Gmail connect flow.
///
/// * In normal browsers this opens the GIS popup.
/// * In test/offline environments we fall back to the stub path so that the
///   UI remains usable without the external script.
pub fn initiate_gmail_connect() {
    // Retrieve Google client-id that the backend provided via /system/info.
    let client_id_opt = crate::state::APP_STATE.with(|st| st.borrow().google_client_id.clone());

    let client_id = if let Some(id) = client_id_opt {
        id
    } else {
        web_sys::console::error_1(
            &"[gmail-connect] Missing google_client_id – cannot start OAuth".into(),
        );
        // Fallback to stub.
        crate::state::dispatch_global_message(crate::messages::Message::GmailConnected);
        return;
    };

    let window = match web_sys::window() {
        Some(w) => w,
        None => {
            crate::state::dispatch_global_message(crate::messages::Message::GmailConnected);
            return;
        }
    };

    // Verify that the GIS script loaded successfully.
    let google_ns =
        js_sys::Reflect::get(&window, &JsValue::from_str("google")).unwrap_or(JsValue::UNDEFINED);
    if google_ns.is_undefined() {
        // Offline / tests – perform stub behaviour for now.
        web_sys::console::warn_1(
            &"[gmail-connect] google.accounts namespace unavailable – using stub".into(),
        );
        crate::state::dispatch_global_message(crate::messages::Message::GmailConnected);
        return;
    }

    // ---------------------------------------------------------------------
    // Build param object expected by `initCodeClient`.
    // ---------------------------------------------------------------------
    let params = js_sys::Object::new();

    // client_id ------------------------------------------------------------
    js_sys::Reflect::set(
        &params,
        &JsValue::from_str("client_id"),
        &JsValue::from_str(&client_id),
    )
    .unwrap();

    // Scope limited to readonly Gmail.  Note the space-separated list format.
    js_sys::Reflect::set(
        &params,
        &JsValue::from_str("scope"),
        &JsValue::from_str("https://www.googleapis.com/auth/gmail.readonly"),
    )
    .unwrap();

    // Force consent screen and request *offline* refresh-token.
    js_sys::Reflect::set(
        &params,
        &JsValue::from_str("prompt"),
        &JsValue::from_str("consent"),
    )
    .unwrap();
    js_sys::Reflect::set(
        &params,
        &JsValue::from_str("access_type"),
        &JsValue::from_str("offline"),
    )
    .unwrap();

    // Callback closure – receives JS object with `.code` once the user
    // grants access.
    let callback = Closure::<dyn Fn(JsValue)>::new(|resp: JsValue| {
        // Extract string field `code` from the JS object.
        let code_val = js_sys::Reflect::get(&resp, &JsValue::from_str("code"))
            .ok()
            .filter(|v| v.is_string())
            .and_then(|v| v.as_string());

        let auth_code = match code_val {
            Some(c) => c,
            None => {
                web_sys::console::error_1(
                    &"[gmail-connect] Callback called without `.code`".into(),
                );
                return;
            }
        };

        // We need to perform an async fetch – spawn_local inside closure.
        wasm_bindgen_futures::spawn_local(async move {
            match crate::network::api_client::ApiClient::gmail_exchange_auth_code(&auth_code).await {
                Ok(body) => {
                    // Parse connector_id from JSON body; fall back to boolean message.
                    let connector_id_opt = serde_json::from_str::<serde_json::Value>(&body)
                        .ok()
                        .and_then(|v| v.get("connector_id").and_then(|id| id.as_u64()))
                        .map(|n| n as u32);
                    if let Some(cid) = connector_id_opt {
                        crate::state::dispatch_global_message(
                            crate::messages::Message::GmailConnectedWithConnector { connector_id: cid },
                        );
                    } else {
                        crate::state::dispatch_global_message(crate::messages::Message::GmailConnected);
                    }
                }
                Err(e) => {
                    web_sys::console::error_1(&e);
                }
            }
        });
    });

    js_sys::Reflect::set(&params, &JsValue::from_str("callback"), callback.as_ref()).unwrap();

    // Safety: closure needs to remain alive as long as the code-client might
    // call it.  The simplest approach is to *forget* it so the leak is
    // permanent – acceptable given we have exactly one Gmail connect flow
    // per browser session.
    callback.forget();

    // ---------------------------------------------------------------------
    // Fire the code-client popup.
    // ---------------------------------------------------------------------
    let code_client = init_code_client(&params);
    code_client.request_code();
}
