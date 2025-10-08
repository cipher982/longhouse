// Re-export network modules
pub mod api_client;
pub mod config;
pub mod contract_glue;
pub mod generated_client;
pub mod messages;
pub mod topic_manager;
pub mod ui_updates;
pub mod ws_client_v2;

// Re-export commonly used items
pub use api_client::ApiClient;
pub use topic_manager::TopicManager;
pub use ws_client_v2::WsClientV2;

use config::ApiConfig;
use lazy_static::lazy_static;
use std::sync::RwLock;
use wasm_bindgen::prelude::*;

lazy_static! {
    static ref API_CONFIG: RwLock<Option<ApiConfig>> = RwLock::new(None);
}

/// Initialize the API configuration. Must be called before any network operations.
pub fn init_api_config() -> Result<(), &'static str> {
    let config = ApiConfig::new()?;
    *API_CONFIG.write().unwrap() = Some(config);
    Ok(())
}

/// Initialize the API configuration from a JS-provided URL.
/// This allows runtime configuration of the API endpoints.
#[wasm_bindgen]
pub fn init_api_config_js(api_base_url: &str) -> Result<(), JsValue> {
    crate::debug_log!("Initializing API config from JS: {}", api_base_url);
    let config = ApiConfig::from_url(api_base_url);
    *API_CONFIG.write().unwrap() = Some(config);
    Ok(())
}

/// Get the API configuration
#[allow(dead_code)]
fn get_api_config() -> Result<std::sync::RwLockReadGuard<'static, Option<ApiConfig>>, &'static str>
{
    Ok(API_CONFIG.read().unwrap())
}

/// Get the WebSocket URL
pub(crate) fn get_ws_url() -> Result<String, &'static str> {
    let base_url = {
        let guard = API_CONFIG.read().unwrap();
        if let Some(cfg) = guard.as_ref() {
            cfg.ws_url()
        } else {
            // Fallback to same-origin WebSocket URL
            let win = web_sys::window().ok_or("window unavailable")?;
            let loc = win.location();
            let host = loc.host().map_err(|_| "host unavailable")?;
            let proto = loc.protocol().map_err(|_| "protocol unavailable")?;
            let ws_scheme = if proto == "https:" { "wss" } else { "ws" };
            format!("{}://{}/api/ws", ws_scheme, host)
        }
    };

    // If a JWT is present in localStorage append it as query parameter so the
    // backend can authenticate the WebSocket upgrade request.
    let token_opt = crate::utils::current_jwt();

    if let Some(tok) = token_opt {
        Ok(format!("{}?token={}", base_url, tok))
    } else {
        Ok(base_url)
    }
}

/// Get the base URL for API calls
pub(crate) fn get_api_base_url() -> Result<String, &'static str> {
    // Missing config → default to same-origin (empty string) so callers build
    // relative URLs like "/api/...".
    let guard = API_CONFIG.read().unwrap();
    Ok(guard
        .as_ref()
        .map(|cfg| cfg.base_url().to_string())
        .unwrap_or_default())
}
