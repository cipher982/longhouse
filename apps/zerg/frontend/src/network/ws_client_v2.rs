use js_sys::Array;
use js_sys::Date;
use serde_json::Value;
use std::any::Any;
use std::cell::RefCell;
use std::rc::Rc;
use wasm_bindgen::prelude::*;
use web_sys::{MessageEvent, WebSocket};
use crate::{debug_log, warn_log};

use super::messages::builders;

/// Trait defining the WebSocket client interface
#[allow(dead_code)]
pub trait IWsClient: Any {
    fn connect(&mut self) -> Result<(), JsValue>;
    fn send_serialized_message(&self, message_json: &str) -> Result<(), JsValue>;
    fn connection_state(&self) -> ConnectionState;
    fn close(&mut self) -> Result<(), JsValue>;
    fn set_on_connect(&mut self, callback: Box<dyn FnMut() + 'static>);
    fn set_on_message(&mut self, callback: Box<dyn FnMut(Value) + 'static>);
    fn set_on_disconnect(&mut self, callback: Box<dyn FnMut() + 'static>);
    fn as_any(&self) -> &dyn Any;
}

/// Represents the current state of the WebSocket connection
#[derive(Debug, Clone, PartialEq)]
pub enum ConnectionState {
    Disconnected,
    Connecting,
    Connected,
    #[allow(dead_code)]
    Error(String),
}

impl std::fmt::Display for ConnectionState {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConnectionState::Disconnected => write!(f, "Disconnected"),
            ConnectionState::Connecting => write!(f, "Connecting"),
            ConnectionState::Connected => write!(f, "Connected"),
            ConnectionState::Error(msg) => write!(f, "Error: {}", msg),
        }
    }
}

/// Configuration for the WebSocket client
#[derive(Debug, Clone)]
pub struct WsConfig {
    /// Base URL for the WebSocket connection
    pub url: String,
    /// Maximum number of reconnection attempts (None for infinite)
    pub max_reconnect_attempts: Option<u32>,
    /// Initial backoff delay in milliseconds
    pub initial_backoff_ms: u32,
    /// Maximum backoff delay in milliseconds
    pub max_backoff_ms: u32,
    /// Ping interval in milliseconds when *client*-initiated heart-beat is
    /// required.  Our server now sends the periodic `ping`, so by default we
    /// turn the client-side timer **off**.
    pub ping_interval_ms: Option<u32>,

    /// Watch-dog interval (ms).  If **no** frame has been received within
    /// this threshold the client assumes the connection is stale and forces
    /// a reconnect.  Defaults to twice the expected server-side ping
    /// cadence (≈60 s).
    pub watchdog_ms: u32,
}

impl Default for WsConfig {
    fn default() -> Self {
        let url = super::get_ws_url().unwrap_or_else(|_| {
            // Provide a sane fallback during unit tests where API config may
            // not be initialised.  At runtime `init_api_config()` should have
            // been called long before we reach this code.
            "ws://localhost/placeholder".to_string()
        });

        Self {
            url,
            max_reconnect_attempts: None,
            initial_backoff_ms: 1000,
            max_backoff_ms: 30000,
            ping_interval_ms: None, // server → ping, client replies with pong
            watchdog_ms: 65_000,    // 65 s gives generous slack above 60 s drop rule
        }
    }
}

/// Type for the on_connect callback
type OnConnectCallback = Rc<RefCell<dyn FnMut()>>;
/// Type for the on_message callback (receives parsed JSON value)
type OnMessageCallback = Rc<RefCell<dyn FnMut(Value)>>;
/// Type for the on_disconnect callback
type OnDisconnectCallback = Rc<RefCell<dyn FnMut()>>;

/// Core WebSocket client implementation
pub struct WsClientV2 {
    config: WsConfig,
    websocket: Option<WebSocket>,
    state: Rc<RefCell<ConnectionState>>,
    reconnect_attempt: Rc<RefCell<u32>>,
    ping_interval: Option<i32>, // Store interval ID for cleanup
    reconnect_timeout: Rc<RefCell<Option<i32>>>, // Store timeout ID so we can cancel on success

    // Track last *any* frame timestamp (ms since epoch) so the watchdog can
    // detect silent socket hangs where *onclose* never fires (e.g. router
    // drops outgoing but not incoming packets).
    last_activity_ms: Rc<RefCell<f64>>,
    activity_interval: Rc<RefCell<Option<i32>>>,

    // --- NEW Callbacks ---
    on_connect_callback: Option<OnConnectCallback>,
    on_message_callback: Option<OnMessageCallback>,
    on_disconnect_callback: Option<OnDisconnectCallback>,
}

impl WsClientV2 {
    /// Redact sensitive query parameters (e.g. JWT token) in URLs for logging
    fn redact_url(url: &str) -> String {
        if let Some(pos) = url.find("token=") {
            let start = pos + "token=".len();
            let end = url[start..]
                .find('&')
                .map(|i| start + i)
                .unwrap_or_else(|| url.len());
            let mut redacted = url.to_string();
            redacted.replace_range(start..end, "REDACTED");
            redacted
        } else {
            url.to_string()
        }
    }
    /// Create a new WebSocket client with the given configuration
    pub fn new(config: WsConfig) -> Self {
        Self {
            config,
            websocket: None,
            state: Rc::new(RefCell::new(ConnectionState::Disconnected)),
            reconnect_attempt: Rc::new(RefCell::new(0)),
            ping_interval: None,
            reconnect_timeout: Rc::new(RefCell::new(None)),

            last_activity_ms: Rc::new(RefCell::new(js_sys::Date::now())),
            activity_interval: Rc::new(RefCell::new(None)),
            // Initialize callbacks as None
            on_connect_callback: None,
            on_message_callback: None,
            on_disconnect_callback: None,
        }
    }

    /// Create a new WebSocket client with default configuration
    pub fn new_default() -> Self {
        Self::new(WsConfig::default())
    }

    /// Set a handler to be called upon successful connection/reconnection.
    pub fn set_on_connect<F>(&mut self, callback: F)
    where
        F: FnMut() + 'static,
    {
        self.on_connect_callback = Some(Rc::new(RefCell::new(Box::new(callback))));
    }

    /// Set a handler for incoming messages.
    /// The handler receives the parsed `serde_json::Value`.
    pub fn set_on_message<F>(&mut self, callback: F)
    where
        F: FnMut(Value) + 'static,
    {
        self.on_message_callback = Some(Rc::new(RefCell::new(Box::new(callback))));
    }

    /// Set a handler to be called when the connection is closed or fails to reconnect.
    pub fn set_on_disconnect<F>(&mut self, callback: F)
    where
        F: FnMut() + 'static,
    {
        self.on_disconnect_callback = Some(Rc::new(RefCell::new(Box::new(callback))));
    }

    /// Get the current connection state
    pub fn connection_state(&self) -> ConnectionState {
        self.state.borrow().clone()
    }

    /// Calculate the backoff delay for reconnection attempts
    fn get_backoff_ms(&self) -> u32 {
        let attempt = *self.reconnect_attempt.borrow();
        let base_delay = self.config.initial_backoff_ms;
        let max_delay = self.config.max_backoff_ms;
        let delay = base_delay * (2_u32.pow(attempt.min(10))); // Prevent overflow with min(10)
        delay.min(max_delay)
    }

    /// Set up ping interval
    fn setup_ping_interval(&mut self) {
        if let Some(interval_ms) = self.config.ping_interval_ms {
            let window = web_sys::window().expect("no global window exists");
            let ws_clone = self.websocket.clone();

            let ping_callback = Closure::wrap(Box::new(move || {
                if let Some(ws) = &ws_clone {
                    let ping = builders::create_ping();
                    if let Ok(json) = serde_json::to_string(&ping) {
                        if let Err(e) = ws.send_with_str(&json) {
                            web_sys::console::error_1(
                                &format!("Failed to send ping: {:?}", e).into(),
                            );
                        }
                    }
                }
            }) as Box<dyn FnMut()>);

            let interval_id = window
                .set_interval_with_callback_and_timeout_and_arguments(
                    ping_callback.as_ref().unchecked_ref(),
                    interval_ms as i32,
                    &Array::new(),
                )
                .expect("Failed to set ping interval");

            ping_callback.forget();
            self.ping_interval = Some(interval_id);
        }
    }

    /// Clear ping interval
    fn clear_ping_interval(&mut self) {
        if let Some(interval_id) = self.ping_interval.take() {
            if let Some(window) = web_sys::window() {
                window.clear_interval_with_handle(interval_id);
            }
        }
    }

    /// Creates the actual WebSocket connection and attaches handlers.
    /// This is used by both `connect` and `schedule_reconnect`.
    fn establish_connection(&mut self) -> Result<WebSocket, JsValue> {
        // Create WebSocket connection
        let ws = WebSocket::new(&self.config.url)?;

        // Clone necessary parts for closures
        let state_clone = self.state.clone();
        let reconnect_attempt_clone = self.reconnect_attempt.clone();
        let on_connect_cb_clone = self.on_connect_callback.clone();
        let on_message_cb_clone = self.on_message_callback.clone();
        let on_disconnect_cb_clone = self.on_disconnect_callback.clone();
        let client_clone_for_reconnect = self.clone(); // Clone for schedule_reconnect call
        let config_clone = self.config.clone();

        // Set up open handler
        let onopen_closure = Closure::wrap(Box::new(move |_: web_sys::Event| {
            debug_log!("WebSocket connected");
            *state_clone.borrow_mut() = ConnectionState::Connected;
            *reconnect_attempt_clone.borrow_mut() = 0; // Reset counter

            // --- Call on_connect callback ---
            if let Some(callback_rc) = &on_connect_cb_clone {
                (callback_rc.borrow_mut())();
            }
        }) as Box<dyn FnMut(web_sys::Event)>);
        ws.set_onopen(Some(onopen_closure.as_ref().unchecked_ref()));
        onopen_closure.forget();

        // Set up error handler (simple logging for now)
        let onerror_closure = Closure::wrap(Box::new(move |e: web_sys::Event| {
            web_sys::console::error_1(&format!("WebSocket error: {:?}", e).into());
            // Error often leads to close, state change handled in onclose
        }) as Box<dyn FnMut(web_sys::Event)>);
        ws.set_onerror(Some(onerror_closure.as_ref().unchecked_ref()));
        onerror_closure.forget();

        // Set up close handler (handles disconnect and triggers reconnect)
        let state_clone = self.state.clone();
        let reconnect_attempt_clone = self.reconnect_attempt.clone();
        let on_disconnect_cb_clone_for_close = on_disconnect_cb_clone.clone(); // Clone again for this closure
        let onclose_closure = Closure::wrap(Box::new(move |evt: web_sys::Event| {
            // Attempt to downcast to CloseEvent so we can inspect the code.
            if let Ok(close_evt) = evt.dyn_into::<web_sys::CloseEvent>() {
                let code = close_evt.code();
                if code == 4401 || code == 4003 {
                    // 4401: unauthenticated, 4003: forbidden – logout and show banner.
                    crate::network::ui_updates::show_auth_error_banner();
                    let _ = crate::utils::logout();
                } else if code == 1002 {
                    crate::toast::error("Protocol error – socket closed (1002)");
                } else if code == 4408 {
                    crate::toast::error("Heartbeat timeout – reconnecting… (4408)");
                }
            }

            debug_log!("WebSocket closed");
            *state_clone.borrow_mut() = ConnectionState::Disconnected;

            // Cancel watchdog interval (if set) to avoid leaks when the
            // socket was closed intentionally or due to network issues.
            let interval_id = *client_clone_for_reconnect.activity_interval.borrow();
            if let Some(id) = interval_id {
                let _ = web_sys::window().unwrap().clear_interval_with_handle(id);
                *client_clone_for_reconnect.activity_interval.borrow_mut() = None;
            }

            // --- Call on_disconnect callback ---
            if let Some(callback_rc) = &on_disconnect_cb_clone_for_close {
                (callback_rc.borrow_mut())();
            }

            // Check if reconnection should be attempted
            let current_attempt = *reconnect_attempt_clone.borrow();
            if config_clone
                .max_reconnect_attempts
                .map_or(true, |max| current_attempt < max)
            {
                *reconnect_attempt_clone.borrow_mut() = current_attempt + 1;
                // Use the client clone to call schedule_reconnect
                client_clone_for_reconnect.schedule_reconnect();
            } else {
                warn_log!("Max reconnection attempts reached");
                // Potentially update state to a permanent error state if needed
            }
        }) as Box<dyn FnMut(web_sys::Event)>);
        ws.set_onclose(Some(onclose_closure.as_ref().unchecked_ref()));
        onclose_closure.forget();

        // Clone WebSocket so the closure that may call `close_with_code` does not
        // move ownership of the *original* socket (we still need it afterwards
        // to attach the handler and potentially other callbacks).
        let ws_for_close = ws.clone();

        // ----- Watchdog: share last_activity_ms between closure and interval ----
        let last_activity_ms_clone = self.last_activity_ms.clone();

        // Set up message handler: parse JSON once, validate envelope, then forward to topic manager
        let onmessage_closure = Closure::wrap(Box::new(move |event: MessageEvent| {
            // Only handle text messages
            if let Ok(text) = event.data().dyn_into::<js_sys::JsString>() {
                if let Some(msg_str) = text.as_string() {
                    if let Ok(parsed_value) = serde_json::from_str::<Value>(&msg_str) {
                        // ------------------------------------------------------------------
                        // Phase-2: Runtime schema validation (lightweight)
                        // ------------------------------------------------------------------
                        if let Ok(_) =
                            crate::generated::ws_messages::validate_envelope(&parsed_value)
                        {
                            // ------------------------------------------------------------------
                            // Automatic Pong reply – server-initiated heart-beat
                            // ------------------------------------------------------------------
                            if let Some(ty) = parsed_value.get("type").and_then(|v| v.as_str()) {
                                if ty.eq_ignore_ascii_case("PING") {
                                    // Extract ping_id if present under data.ping_id (enveloped) OR
                                    // at root for legacy format.
                                    let ping_id = parsed_value
                                        .get("data")
                                        .and_then(|d| d.get("ping_id"))
                                        .and_then(|v| v.as_str())
                                        .or_else(|| {
                                            parsed_value.get("ping_id").and_then(|v| v.as_str())
                                        });

                                    let mut pong_obj = serde_json::json!({"type": "pong"});
                                    if let Some(pid) = ping_id {
                                        pong_obj["ping_id"] =
                                            serde_json::Value::String(pid.to_string());
                                    }

                                    if let Ok(pong_str) = serde_json::to_string(&pong_obj) {
                                        let _ = ws_for_close.send_with_str(&pong_str);
                                    }
                                }
                            }

                            if let Some(callback_rc) = &on_message_cb_clone {
                                (callback_rc.borrow_mut())(parsed_value);
                            }

                            // Update last-activity timestamp for watchdog
                            *last_activity_ms_clone.borrow_mut() = Date::now();
                        } else {
                            web_sys::console::error_1(&"Protocol error – invalid envelope".into());
                            // Close to trigger reconnect (matches backend 1002 logic).  We ignore
                            // failures since the socket may already be closed by the server.
                            let _ = ws_for_close.close_with_code(1002);
                        }
                    } else {
                        web_sys::console::error_1(
                            &format!(
                                "Failed to parse incoming WebSocket message as JSON: {}",
                                msg_str
                            )
                            .into(),
                        );
                    }
                }
            } else {
                web_sys::console::warn_1(&"Received non-text WebSocket message".into());
            }
        }) as Box<dyn FnMut(MessageEvent)>);
        ws.set_onmessage(Some(onmessage_closure.as_ref().unchecked_ref()));
        onmessage_closure.forget();

        // ------------------------------------------------------------------
        // Watch-dog interval – runs every `watchdog_ms` to ensure at least
        // *some* traffic is flowing.  If the timer detects silence for more
        // than the threshold it forcibly closes the socket with 4408 which
        // flows into the existing onclose → reconnect logic.
        // ------------------------------------------------------------------

        let watchdog_last = self.last_activity_ms.clone();
        let watchdog_ws = ws.clone();
        let watchdog_ms = self.config.watchdog_ms as i32;

        // Store interval id so we can clear it on manual close / reconnect.
        let watchdog_closure = Closure::wrap(Box::new(move || {
            let now = Date::now();
            let last = *watchdog_last.borrow();
            if now - last > watchdog_ms as f64 {
                let _ = watchdog_ws.close_with_code(4408);
            }
        }) as Box<dyn FnMut()>);

        let interval_id = web_sys::window()
            .expect("window")
            .set_interval_with_callback_and_timeout_and_arguments_0(
                watchdog_closure.as_ref().unchecked_ref(),
                watchdog_ms,
            )?;

        watchdog_closure.forget();

        *self.activity_interval.borrow_mut() = Some(interval_id);

        Ok(ws)
    }

    /// Schedule a reconnection attempt with exponential backoff
    fn schedule_reconnect(&self) {
        let window = web_sys::window().expect("no global window exists");
        let state_clone = self.state.clone();
        let delay = self.get_backoff_ms();
        let mut client_clone = self.clone(); // Clone self to move into the closure

        // Create reconnection callback
        let reconnect_callback = Closure::once(Box::new(move || {
            // Only attempt reconnection if we're still disconnected
            if *state_clone.borrow() == ConnectionState::Disconnected {
                debug_log!(
                    "Attempting reconnection (attempt {})",
                    *client_clone.reconnect_attempt.borrow()
                );

                // Update state to connecting before attempting
                *state_clone.borrow_mut() = ConnectionState::Connecting;

                // Attempt to establish connection
                match client_clone.establish_connection() {
                    Ok(ws) => {
                        // Connection initiated, store the WebSocket instance
                        client_clone.websocket = Some(ws);
                        // Setup ping interval for the new connection
                        client_clone.setup_ping_interval();
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to create WebSocket during reconnect: {:?}", e).into(),
                        );
                        // Connection failed, state might be reset by onclose, or manually trigger schedule_reconnect again if necessary
                        *state_clone.borrow_mut() = ConnectionState::Disconnected; // Reset state
                                                                                   // Schedule again immediately if creation failed (could lead to tight loop, consider delay)
                        client_clone.schedule_reconnect();
                    }
                }
            }
        }) as Box<dyn FnOnce()>);

        // Schedule the reconnection attempt and **store** the timeout ID so
        // that we can cancel it when a manual connect() succeeds before the
        // timer fires (fixes the memory leak mentioned in the refactor doc).

        let timeout_id = window
            .set_timeout_with_callback_and_timeout_and_arguments_0(
                reconnect_callback.as_ref().unchecked_ref(),
                delay as i32,
            )
            .expect("Failed to schedule reconnection");

        *self.reconnect_timeout.borrow_mut() = Some(timeout_id);

        // Closure::once requires forget
        reconnect_callback.forget();
    }

    /// Connect to the WebSocket server for the first time.
    pub fn connect(&mut self) -> Result<(), JsValue> {
        // Recompute WS URL from current API config to avoid stale defaults
        if let Ok(ws_url) = super::get_ws_url() {
            if self.config.url != ws_url {
                let before = Self::redact_url(&self.config.url);
                let after = Self::redact_url(&ws_url);
                debug_log!("Updating WebSocket URL: {} -> {}", before, after);
                self.config.url = ws_url;
            }
        }
        debug_log!("Initiating WebSocket connection...");
        // Reset reconnection counter when manually connecting
        *self.reconnect_attempt.borrow_mut() = 0;

        // Clear any previous interval
        self.clear_ping_interval();

        // Update state to connecting
        *self.state.borrow_mut() = ConnectionState::Connecting;

        // Establish the connection and set up handlers
        let ws = self.establish_connection()?;

        // Clear any pending reconnect timeout – we successfully connected
        if let Some(timeout_id) = self.reconnect_timeout.borrow_mut().take() {
            if let Some(window) = web_sys::window() {
                window.clear_timeout_with_handle(timeout_id);
            }
        }

        // Store WebSocket instance
        self.websocket = Some(ws);

        // Set up ping interval for the new connection
        self.setup_ping_interval();

        Ok(())
    }

    /// Send a pre-serialized message to the WebSocket server
    pub fn send_serialized_message(&self, message_json: &str) -> Result<(), JsValue> {
        if let Some(ws) = &self.websocket {
            if *self.state.borrow() == ConnectionState::Connected {
                ws.send_with_str(message_json)?;
                Ok(())
            } else {
                web_sys::console::warn_1(
                    &"Attempted to send message while WebSocket is not connected".into(),
                );
                Err(JsValue::from_str("WebSocket is not connected"))
            }
        } else {
            Err(JsValue::from_str("WebSocket is not initialized"))
        }
    }

    /// Close the WebSocket connection gracefully.
    pub async fn close(&mut self) -> Result<(), JsValue> {
        debug_log!("Closing WebSocket connection...");
        // Clear ping interval
        self.clear_ping_interval();

        // Set state to disconnected *before* closing
        *self.state.borrow_mut() = ConnectionState::Disconnected;

        if let Some(ws) = self.websocket.take() {
            // Use take to remove it
            match ws.close_with_code(1000) {
                Ok(_) => debug_log!("WebSocket close command sent."),
                Err(e) => web_sys::console::error_1(
                    &format!("Error sending close command: {:?}", e).into(),
                ),
            }
        }
        // Note: The onclose handler will likely fire after this, potentially calling on_disconnect again.
        Ok(())
    }
}

/// Minimal validation that an incoming frame matches the *Envelope* shape.
/// This is **not** a full JSON-Schema check (that will be generated once the
/// AsyncAPI toolchain works in CI).  It simply guards against blatantly
/// malformed payloads so we can fail-fast and close the connection –
/// mirroring the backend 1002 close code behaviour.
// The inline `validate_envelope()` helper has been superseded by the
// full JSON-Schema based check in `crate::schema_validation`.
// (Function removed.)

// Implement IWsClient trait for WsClientV2
impl IWsClient for WsClientV2 {
    fn connect(&mut self) -> Result<(), JsValue> {
        self.connect()
    }

    fn send_serialized_message(&self, message_json: &str) -> Result<(), JsValue> {
        self.send_serialized_message(message_json)
    }

    fn connection_state(&self) -> ConnectionState {
        self.connection_state()
    }

    fn close(&mut self) -> Result<(), JsValue> {
        debug_log!("Closing WebSocket connection...");
        self.clear_ping_interval();
        *self.state.borrow_mut() = ConnectionState::Disconnected;
        if let Some(ws) = self.websocket.take() {
            match ws.close_with_code(1000) {
                Ok(_) => debug_log!("WebSocket close command sent."),
                Err(e) => web_sys::console::error_1(
                    &format!("Error sending close command: {:?}", e).into(),
                ),
            }
        }
        Ok(())
    }

    fn set_on_connect(&mut self, callback: Box<dyn FnMut() + 'static>) {
        self.set_on_connect(callback);
    }

    fn set_on_message(&mut self, callback: Box<dyn FnMut(Value) + 'static>) {
        self.set_on_message(callback);
    }

    fn set_on_disconnect(&mut self, callback: Box<dyn FnMut() + 'static>) {
        self.set_on_disconnect(callback);
    }

    fn as_any(&self) -> &dyn Any {
        self
    }
}

// --- Clone Implementation (Needs update) ---

impl Clone for WsClientV2 {
    fn clone(&self) -> Self {
        // Cloning is tricky with callbacks and WebSocket instance.
        // Typically used for passing into closures for reconnect logic.
        // We clone the Rc's for state/config but NOT the WebSocket instance or interval ID.
        // Callbacks are also NOT cloned here; they are captured by the closures where needed.
        Self {
            config: self.config.clone(),
            websocket: None, // Never clone the actual WebSocket
            state: self.state.clone(),
            reconnect_attempt: self.reconnect_attempt.clone(),
            ping_interval: None, // Don't clone interval ID
            reconnect_timeout: self.reconnect_timeout.clone(),
            // Callbacks are specifically NOT cloned here. They are managed by Rc.
            on_connect_callback: self.on_connect_callback.clone(),
            on_message_callback: self.on_message_callback.clone(),
            on_disconnect_callback: self.on_disconnect_callback.clone(),

            last_activity_ms: self.last_activity_ms.clone(),
            activity_interval: self.activity_interval.clone(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_bindgen_test::*;

    wasm_bindgen_test_configure!(run_in_browser);

    #[wasm_bindgen_test]
    fn test_client_creation() {
        let client = WsClientV2::new_default();
        assert_eq!(client.connection_state(), ConnectionState::Disconnected);
    }

    #[wasm_bindgen_test]
    fn test_backoff_calculation() {
        let client = WsClientV2::new_default();

        // First attempt should be initial_backoff_ms
        assert_eq!(*client.reconnect_attempt.borrow(), 0);
        assert_eq!(client.get_backoff_ms(), 1000);

        // Second attempt should be 2 * initial_backoff_ms
        *client.reconnect_attempt.borrow_mut() = 1;
        assert_eq!(client.get_backoff_ms(), 2000);

        // Should not exceed max_backoff_ms
        *client.reconnect_attempt.borrow_mut() = 10;
        assert_eq!(client.get_backoff_ms(), 30000);
    }

    #[wasm_bindgen_test]
    fn test_message_handler() {
        let mut client = WsClientV2::new_default();
        let received_messages = Rc::new(RefCell::new(Vec::new()));
        let received_clone = received_messages.clone();

        client.set_on_message(move |msg| {
            // Store the message type from the JSON if it exists
            if let Some(msg_type) = msg.get("type").and_then(|t| t.as_str()) {
                received_clone.borrow_mut().push(msg_type.to_string());
            }
        });

        // TODO: Add more message handler tests when we implement the full
        // message processing system
    }
}

// Helper function to send a message to a thread via WebSocket
#[allow(dead_code)]
pub fn send_thread_message(text: &str, message_id: String) {
    debug_log!(
        "Network: Sending thread message: '{}', message_id: {}",
        text, message_id
    );

    super::ui_updates::flash_activity(); // Flash on send

    // Check if we have a selected agent and a websocket connection
    let has_agent_and_websocket = crate::state::APP_STATE.with(|state| {
        let state = state.borrow();
        state.selected_node_id.is_some() && state.websocket.is_some()
    });

    if has_agent_and_websocket {
        // Use WebSocket for agent communication
        crate::state::APP_STATE.with(|state| {
            let state = state.borrow();
            if let Some(ws) = &state.websocket {
                if ws.ready_state() == 1 {
                    // OPEN
                    // Get current thread ID from agent state
                    let thread_id = state
                        .current_agent()
                        .and_then(|agent| agent.current_thread_id)
                        .unwrap_or(1);

                    // Create message body with correct format
                    let body_obj = js_sys::Object::new();
                    js_sys::Reflect::set(&body_obj, &"type".into(), &"send_message".into())
                        .unwrap();
                    js_sys::Reflect::set(&body_obj, &"thread_id".into(), &thread_id.into())
                        .unwrap();
                    js_sys::Reflect::set(&body_obj, &"content".into(), &text.into()).unwrap();
                    js_sys::Reflect::set(&body_obj, &"message_id".into(), &message_id.into())
                        .unwrap();

                    // Get selected model if any
                    if !state.selected_model.is_empty() {
                        js_sys::Reflect::set(
                            &body_obj,
                            &"model".into(),
                            &state.selected_model.clone().into(),
                        )
                        .unwrap();
                    }

                    let body_string = js_sys::JSON::stringify(&body_obj).unwrap();

                    // Convert JsString to String before sending
                    if let Some(string_data) = body_string.as_string() {
                        // Send through WebSocket
                        debug_log!("Sending message: {}", string_data);
                        let _ = ws.send_with_str(&string_data);
                    } else {
                        web_sys::console::error_1(&"Failed to convert JSON to string".into());
                    }
                } else {
                    // WebSocket not connected, try to reconnect
                    web_sys::console::warn_1(&"WebSocket not connected, reconnecting...".into());
                    // Create a new client and connect
                    let mut client = WsClientV2::new_default();
                    let _ = client.connect();
                }
            }
        });
    } else {
        web_sys::console::error_1(
            &"Cannot send message: No websocket connection or no agent selected".into(),
        );
    }
}
