/// API route configuration
pub struct ApiConfig {
    // When empty, the SPA assumes same-origin and builds relative REST URLs
    // ("/api/...") and computes WebSocket URL from `window.location`.
    base_url: String,
}

impl Default for ApiConfig {
    /// Provides a *minimal* default configuration that points to the local
    /// development backend.  The values returned here are **only** meant for
    /// unit-tests (which run in a headless browser without the normal
    /// bootstrap sequence) or very early start-up phases before
    /// `init_api_config()` is executed.  Production code **must** still call
    /// `init_api_config()` so that the real URL – injected at build-time via
    /// the `API_BASE_URL` environment variable or at runtime via
    /// `init_api_config_js()` – is stored inside the global `API_CONFIG`.
    ///
    /// Keeping the default lightweight avoids having to litter the rest of
    /// the code base with `Option` checks while still preventing hard panics
    /// during tests.
    fn default() -> Self { Self { base_url: String::new() } }
}

impl ApiConfig {
    /// Create a new ApiConfig from the API_BASE_URL environment variable
    pub fn new() -> Result<Self, &'static str> {
        // Env var is optional – fall back to same-origin when missing.
        let base = option_env!("API_BASE_URL").unwrap_or("");
        Ok(Self {
            base_url: base.trim_end_matches('/').to_string(),
        })
    }

    /// Create a new ApiConfig from a URL string
    pub fn from_url(url: &str) -> Self {
        Self {
            base_url: url.trim_end_matches('/').to_string(),
        }
    }

    /// Get the base URL for all API calls
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    /// Get the WebSocket URL
    pub fn ws_url(&self) -> String {
        // When base_url is empty, compute absolute WS URL from the current page
        if self.base_url.is_empty() {
            if let Some(win) = web_sys::window() {
                let loc = win.location();
                let host = loc.host().unwrap_or_else(|_| "localhost".into());
                let proto = loc.protocol().unwrap_or_else(|_| "http:".into());
                let ws_scheme = if proto == "https:" { "wss" } else { "ws" };
                return format!("{}://{}/api/ws", ws_scheme, host);
            }
            // Fallback for tests
            return "ws://localhost/api/ws".to_string();
        }

        // Configured cross-origin base → convert scheme and append /api/ws
        let ws_base = if self.base_url.starts_with("https://") {
            self.base_url.replacen("https://", "wss://", 1)
        } else {
            self.base_url.replacen("http://", "ws://", 1)
        };
        format!("{}/api/ws", ws_base)
    }

    /// Get a full API URL for a given path
    #[allow(dead_code)]
    pub fn url(&self, path: &str) -> String {
        format!("{}/api{}", self.base_url, path)
    }
}
