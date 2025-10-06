use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::JsFuture;
// Individual web-sys types are imported where required – RequestMode is still
// used in several helper methods so we keep the top-level import for
// convenience.
use super::ui_updates::flash_activity;
use crate::constants::{DEFAULT_NODE_HEIGHT, DEFAULT_NODE_WIDTH};
use serde::Deserialize;
use std::rc::Rc;
use web_sys::RequestMode;
use crate::debug_log;

#[derive(Deserialize)]
struct TokenOut {
    access_token: String,
    #[allow(dead_code)]
    expires_in: u32,
    #[allow(dead_code)]
    token_type: String,
}

// ----------------------------------------------------------------------------
// Helper – read the persisted JWT from localStorage
// ----------------------------------------------------------------------------

use crate::utils as auth_utils;

// REST API Client for Agent operations
pub struct ApiClient;

impl ApiClient {
    // Re-export to keep older imports working while we centralize contract glue
    #[allow(dead_code)]
    pub(crate) fn map_node_type(frontend_type: &str) -> &str {
        crate::network::generated_client::TypeSafeApiClient::map_node_type(frontend_type)
    }
    // Get the base URL for API calls
    fn api_base_url() -> String {
        // Default to same-origin (empty string → relative "/api" paths)
        super::get_api_base_url().unwrap_or_default()
    }

    /// Format HTTP errors with user-friendly messages and show appropriate toasts
    fn format_http_error(status: u16, status_text: &str, response_body: &str) -> String {
        match status {
            409 => {
                // Check if this is an agent run conflict
                if response_body.contains("already running") {
                    crate::toast::info("Agent is already running. Please wait for it to finish.");
                    "Agent already running".to_string()
                } else {
                    crate::toast::error(
                        "That name is already taken. Please choose a different name.",
                    );
                    "Conflict: Resource already exists".to_string()
                }
            }
            422 => {
                // Try to parse validation errors from response body
                if let Ok(error_data) = serde_json::from_str::<serde_json::Value>(response_body) {
                    if let Some(detail) = error_data.get("detail") {
                        if let Some(detail_str) = detail.as_str() {
                            crate::toast::error(&format!("Validation error: {}", detail_str));
                            return format!("Validation failed: {}", detail_str);
                        }
                    }
                }
                crate::toast::error("Invalid input. Please check your data and try again.");
                "Validation failed".to_string()
            }
            400 => {
                crate::toast::error("Bad request. Please check your input.");
                format!("Bad request: {}", status_text)
            }
            403 => {
                crate::toast::error("You don't have permission to perform this action.");
                "Permission denied".to_string()
            }
            404 => {
                crate::toast::error("The requested resource was not found.");
                "Resource not found".to_string()
            }
            500..=599 => {
                crate::toast::error("Server error. Please try again later.");
                format!("Server error: {} {}", status, status_text)
            }
            _ => {
                crate::toast::error("An unexpected error occurred. Please try again.");
                format!("API request failed: {} {}", status, status_text)
            }
        }
    }

    // -------------------------------------------------------------------
    // Ops Dashboard (admin-only)
    // -------------------------------------------------------------------

    /// GET /api/ops/summary – returns the daily ops summary object.
    pub async fn get_ops_summary() -> Result<String, JsValue> {
        let url = format!("{}/api/ops/summary", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    /// GET /api/ops/timeseries?metric=...&window=today – returns 24 points.
    pub async fn get_ops_timeseries(metric: &str, window: &str) -> Result<String, JsValue> {
        let base = Self::api_base_url();
        let url = format!("{}/api/ops/timeseries?metric={}&window={}", base, metric, window);
        Self::fetch_json(&url, "GET", None).await
    }

    /// GET /api/ops/top?kind=agents&window=today&limit=5 – top lists.
    pub async fn get_ops_top(kind: &str, window: &str, limit: u32) -> Result<String, JsValue> {
        let base = Self::api_base_url();
        let url = format!(
            "{}/api/ops/top?kind={}&window={}&limit={}",
            base, kind, window, limit
        );
        Self::fetch_json(&url, "GET", None).await
    }

    // -------------------------------------------------------------------
    // Workflow execution history
    // -------------------------------------------------------------------

    pub async fn get_execution_history(workflow_id: u32, limit: u32) -> Result<String, JsValue> {
        let base = Self::api_base_url();
        // Backend currently ignores limit param but we include for forward-compat
        let url = format!(
            "{}/api/workflow-executions/history/{}?limit={}",
            base, workflow_id, limit
        );
        Self::fetch_json(&url, "GET", None).await
    }

    // -------------------------------------------------------------------
    // Workflow Scheduling
    // -------------------------------------------------------------------

    /// Schedule a workflow to run on a cron schedule
    pub async fn schedule_workflow(
        workflow_id: u32,
        cron_expression: &str,
    ) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/{}/schedule",
            Self::api_base_url(),
            workflow_id
        );
        let body = format!(
            "{{\"cron_expression\": \"{}\", \"trigger_config\": {{}}}}",
            cron_expression
        );
        Self::fetch_json(&url, "POST", Some(&body)).await
    }

    /// Remove the schedule for a workflow
    pub async fn unschedule_workflow(workflow_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/{}/schedule",
            Self::api_base_url(),
            workflow_id
        );
        Self::fetch_json(&url, "DELETE", None).await
    }

    /// Get the current schedule status for a workflow
    pub async fn get_workflow_schedule(workflow_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/{}/schedule",
            Self::api_base_url(),
            workflow_id
        );
        Self::fetch_json(&url, "GET", None).await
    }

    // ---------------- Agent Runs ----------------

    /// Fetch most recent runs for an agent (limit parameter default 20)
    pub async fn get_agent_runs(agent_id: u32, limit: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{}/runs?limit={}",
            Self::api_base_url(),
            agent_id,
            limit
        );
        Self::fetch_json(&url, "GET", None).await
    }

    // Get available models
    pub async fn fetch_available_models() -> Result<String, JsValue> {
        // Use trailing slash to avoid framework redirects (which can break under misconfigured proxies)
        let url = format!("{}/api/models/", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    // -------------------------------------------------------------------
    // Workflow CRUD – new backend integration (July 2025)
    // -------------------------------------------------------------------

    /// Fetch all workflows for the **current** user (active only).
    pub async fn get_workflows() -> Result<String, JsValue> {
        // Use trailing slash to avoid framework redirects (which can break under misconfigured proxies)
        let url = format!("{}/api/workflows/", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    /// Create a new workflow – returns the JSON representation from backend.
    /// Uses "minimal" template by default to ensure workflows start with a trigger.
    pub async fn create_workflow(name: &str) -> Result<String, JsValue> {
        // Request minimal template by default - backend will provide complete workflow with trigger
        let body = format!(
            "{{\"name\": \"{}\", \"description\": \"\", \"template_name\": \"minimal\"}}",
            name
        );
        let url = format!("{}/api/workflows/", Self::api_base_url());
        Self::fetch_json(&url, "POST", Some(&body)).await
    }

    /// Soft-delete a workflow.
    pub async fn delete_workflow(workflow_id: u32) -> Result<(), JsValue> {
        let url = format!("{}/api/workflows/{}", Self::api_base_url(), workflow_id);
        Self::fetch_json(&url, "DELETE", None).await.map(|_| ())
    }

    /// Rename / update a workflow (PATCH).
    pub async fn rename_workflow(
        workflow_id: u32,
        name: &str,
        description: &str,
    ) -> Result<String, JsValue> {
        let url = format!("{}/api/workflows/{}", Self::api_base_url(), workflow_id);
        let body = format!(
            "{{\"name\": \"{}\", \"description\": \"{}\", \"canvas\": {{\"nodes\": [], \"edges\": []}}}}",
            name, description
        );
        Self::fetch_json(&url, "PATCH", Some(&body)).await
    }

    // -------------------------------------------------------------------
    // Workflow execution – run & status
    // -------------------------------------------------------------------

    /// Get the user's current working workflow. Creates one if none exists.
    pub async fn get_current_workflow() -> Result<String, JsValue> {
        let url = format!("{}/api/workflows/current", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    /// Start an execution for the given workflow ID.  Returns the JSON
    /// response from the backend which contains at least
    /// `{ "execution_id": <u32>, "status": "running" }`.
    pub async fn start_workflow_execution(workflow_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/by-workflow/{}/start",
            Self::api_base_url(),
            workflow_id
        );

        Self::fetch_json(&url, "POST", None).await
    }

    /// Reserve an execution ID for a workflow without starting execution.
    /// This allows the frontend to subscribe to WebSocket messages before execution starts.
    pub async fn reserve_workflow_execution(workflow_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/by-workflow/{}/reserve",
            Self::api_base_url(),
            workflow_id
        );

        Self::fetch_json(&url, "POST", None).await
    }

    /// Start a previously reserved execution.
    pub async fn start_reserved_execution(execution_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/executions/{}/start",
            Self::api_base_url(),
            execution_id
        );

        Self::fetch_json(&url, "POST", None).await
    }

    // Get all agents
    pub async fn get_agents_scoped(scope: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/agents?scope={}", Self::api_base_url(), scope);
        let result = Self::fetch_json(&url, "GET", None).await;
        match &result {
            Ok(json) => debug_log!("API_CLIENT: GET /api/agents returned: {}", json),
            Err(e) => web_sys::console::error_1(
                &format!("API_CLIENT: GET /api/agents error: {:?}", e).into(),
            ),
        }
        result
    }

    // Get a specific agent by ID
    pub async fn get_agent(agent_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/agents/{}", Self::api_base_url(), agent_id);
        Self::fetch_json(&url, "GET", None).await
    }

    // Create a new agent
    #[allow(dead_code)]
    pub async fn create_agent(agent_data: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/agents", Self::api_base_url());
        Self::fetch_json(&url, "POST", Some(agent_data)).await
    }

    // Update an existing agent
    pub async fn update_agent(agent_id: u32, agent_data: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/agents/{}", Self::api_base_url(), agent_id);
        Self::fetch_json(&url, "PUT", Some(agent_data)).await
    }

    // Delete an agent
    pub async fn delete_agent(agent_id: u32) -> Result<(), JsValue> {
        let url = format!("{}/api/agents/{}", Self::api_base_url(), agent_id);
        let _ = Self::fetch_json(&url, "DELETE", None).await?;
        Ok(())
    }

    // Get messages for a specific agent
    #[allow(dead_code)]
    pub async fn get_agent_messages(agent_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/agents/{}/messages", Self::api_base_url(), agent_id);
        Self::fetch_json(&url, "GET", None).await
    }

    // Add a message to an agent
    pub async fn create_agent_message(
        agent_id: u32,
        message_data: &str,
    ) -> Result<String, JsValue> {
        let url = format!("{}/api/agents/{}/messages", Self::api_base_url(), agent_id);
        Self::fetch_json(&url, "POST", Some(message_data)).await
    }

    // Trigger an agent to run
    pub async fn run_agent(agent_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/agents/{}/task", Self::api_base_url(), agent_id);
        Self::fetch_json(&url, "POST", None).await
    }

    // -------------------------------------------------------------------
    // Agent *details* endpoint (debug modal)
    // -------------------------------------------------------------------

    pub async fn get_agent_details(agent_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/agents/{}/details", Self::api_base_url(), agent_id);
        Self::fetch_json(&url, "GET", None).await
    }

    // Check super admin status for reset button
    pub async fn get_super_admin_status() -> Result<String, JsValue> {
        let url = format!("{}/api/admin/super-admin-status", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    // Clear user data (keeps users logged in)
    pub async fn clear_user_data() -> Result<String, JsValue> {
        let url = format!("{}/api/admin/reset-database", Self::api_base_url());
        let body = r#"{"confirmation_password": null, "reset_type": "clear_data"}"#;
        Self::fetch_json(&url, "POST", Some(body)).await
    }

    // Clear user data with password confirmation
    pub async fn clear_user_data_with_password(password: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/admin/reset-database", Self::api_base_url());
        let body = format!(r#"{{"confirmation_password": "{}", "reset_type": "clear_data"}}"#, password);
        Self::fetch_json(&url, "POST", Some(&body)).await
    }

    // Full schema rebuild (logs out all users)
    pub async fn reset_database_full() -> Result<String, JsValue> {
        let url = format!("{}/api/admin/reset-database", Self::api_base_url());
        let body = r#"{"confirmation_password": null, "reset_type": "full_rebuild"}"#;
        Self::fetch_json(&url, "POST", Some(body)).await
    }

    // Full schema rebuild with password confirmation
    pub async fn reset_database_full_with_password(password: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/admin/reset-database", Self::api_base_url());
        let body = format!(r#"{{"confirmation_password": "{}", "reset_type": "full_rebuild"}}"#, password);
        Self::fetch_json(&url, "POST", Some(&body)).await
    }

    // -------------------------------------------------------------------
    // Canvas layout (Phase-B)
    // -------------------------------------------------------------------

    /// Retrieve the persisted canvas layout for the authenticated user.
    /// Returns an **empty string** if the backend responded with 204.
    pub async fn get_layout(workflow_id: Option<u32>) -> Result<String, JsValue> {
        let base = Self::api_base_url();
        let url = if let Some(id) = workflow_id {
            format!("{}/api/graph/layout?workflow_id={}", base, id)
        } else {
            format!("{}/api/graph/layout", base)
        };
        Self::fetch_json(&url, "GET", None).await
    }

    // Thread management
    pub async fn get_threads(agent_id: Option<u32>) -> Result<String, JsValue> {
        let url = if let Some(id) = agent_id {
            format!("{}/api/threads?agent_id={}", Self::api_base_url(), id)
        } else {
            format!("{}/api/threads", Self::api_base_url())
        };
        Self::fetch_json(&url, "GET", None).await
    }

    #[allow(dead_code)]
    pub async fn get_thread(thread_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/threads/{}", Self::api_base_url(), thread_id);
        Self::fetch_json(&url, "GET", None).await
    }

    pub async fn create_thread(agent_id: u32, title: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/threads", Self::api_base_url());
        let thread_data = format!(
            "{{\"agent_id\": {}, \"title\": \"{}\", \"active\": true}}",
            agent_id, title
        );
        Self::fetch_json(&url, "POST", Some(&thread_data)).await
    }

    pub async fn update_thread(thread_id: u32, title: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/threads/{}", Self::api_base_url(), thread_id);
        let thread_data = format!("{{\"title\": \"{}\"}}", title);
        Self::fetch_json(&url, "PUT", Some(&thread_data)).await
    }

    pub async fn delete_thread(thread_id: u32) -> Result<(), JsValue> {
        let url = format!("{}/api/threads/{}", Self::api_base_url(), thread_id);
        let _ = Self::fetch_json(&url, "DELETE", None).await?;
        Ok(())
    }

    // Thread messages
    pub async fn get_thread_messages(
        thread_id: u32,
        skip: u32,
        limit: u32,
    ) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/threads/{}/messages?skip={}&limit={}",
            Self::api_base_url(),
            thread_id,
            skip,
            limit
        );
        Self::fetch_json(&url, "GET", None).await
    }

    pub async fn create_thread_message(thread_id: u32, content: &str) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/threads/{}/messages",
            Self::api_base_url(),
            thread_id
        );
        let message_data = format!("{{\"role\": \"user\", \"content\": \"{}\"}}", content);
        Self::fetch_json(&url, "POST", Some(&message_data)).await
    }

    // Run a thread – the backend expects NO body, simply triggers processing
    pub async fn run_thread(thread_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/threads/{}/run", Self::api_base_url(), thread_id);
        // We deliberately pass None for the body – backend ignores payload.
        Self::fetch_json(&url, "POST", None).await
    }

    // -------------------------------------------------------------------
    // Trigger management (Phase A)
    // -------------------------------------------------------------------

    /// Retrieve all triggers for a given agent.  The backend does not yet
    /// expose `/agents/{id}/triggers`, therefore we hit generic collection
    /// route with a query param.  Once the backend ships the nested route we
    /// can simply change the URL – call-sites remain unchanged.
    #[allow(dead_code)]
    pub async fn get_triggers(agent_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/triggers?agent_id={}",
            Self::api_base_url(),
            agent_id
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Create a new trigger.  `trigger_json` must be a valid JSON body that
    /// matches the backend `TriggerCreate` schema (agent_id, type, config).
    #[allow(dead_code)]
    pub async fn create_trigger(trigger_json: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/triggers", Self::api_base_url());
        Self::fetch_json(&url, "POST", Some(trigger_json)).await
    }

    /// Delete an existing trigger by id.
    #[allow(dead_code)]
    pub async fn delete_trigger(trigger_id: u32) -> Result<(), JsValue> {
        let url = format!("{}/api/triggers/{}", Self::api_base_url(), trigger_id);
        let _ = Self::fetch_json(&url, "DELETE", None).await?;
        Ok(())
    }

    // -------------------------------------------------------------------
    // Gmail OAuth exchange (Phase C)
    // -------------------------------------------------------------------

    /// Exchange the *authorization code* for a backend-stored refresh-token.
    /// Returns the backend response body so callers can capture `connector_id`.
    pub async fn gmail_exchange_auth_code(auth_code: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/auth/google/gmail", Self::api_base_url());
        let payload = format!("{{\"auth_code\": \"{}\"}}", auth_code);
        Self::fetch_json(&url, "POST", Some(&payload)).await
    }

    // -------------------------------------------------------------------
    // Authentication & User profile
    // -------------------------------------------------------------------

    // -------------------------------------------------------------------
    // System information (public)
    // -------------------------------------------------------------------

    /// Fetch `/api/system/info` which returns runtime feature flags needed
    /// by the SPA before login has been determined.
    pub async fn fetch_system_info() -> Result<String, JsValue> {
        use web_sys::{Headers, Request, RequestInit, Response};

        let url = format!("{}/api/system/info", Self::api_base_url());

        let opts = RequestInit::new();
        // `web_sys::RequestInit` exposes interior-mutable setters, therefore
        // the instance itself does **not** have to be declared `mut`.
        // Removing the `mut` keyword eliminates an `unused_mut` compiler
        // warning without changing behaviour.
        opts.set_method("GET");
        opts.set_mode(RequestMode::Cors);

        // No Authorization header – endpoint is public.
        let headers = Headers::new()?;
        opts.set_headers(&headers);

        let request = Request::new_with_str_and_init(&url, &opts)?;
        let window = web_sys::window().expect("window");
        let resp_value = JsFuture::from(window.fetch_with_request(&request)).await?;
        let resp: Response = resp_value.dyn_into()?;

        if !resp.ok() {
            let status = resp.status();
            let status_text = resp.status_text();
            return Err(JsValue::from_str(&format!(
                "system-info request failed: {} {}",
                status, status_text
            )));
        }

        let text = JsFuture::from(resp.text()?).await?;
        Ok(text.as_string().unwrap_or_default())
    }

    /// Fetch the authenticated user's profile (`/api/users/me`).  Caller is
    /// responsible for ensuring that a valid JWT is already present in
    /// localStorage – otherwise the request will fail with 401.
    pub async fn fetch_current_user() -> Result<String, JsValue> {
        let url = format!("{}/api/users/me", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    /// Update (PATCH) the current user's profile via `/api/users/me`.
    /// Expects `patch_json` to be a JSON string matching the backend
    /// `UserUpdate` schema, e.g. `{ "display_name": "Alice" }`.
    pub async fn update_current_user(patch_json: &str) -> Result<String, JsValue> {
        let url = format!("{}/api/users/me", Self::api_base_url());
        Self::fetch_json(&url, "PUT", Some(patch_json)).await
    }

    // -------------------------------------------------------------------
    // Canvas layout persistence (graph editor)
    // -------------------------------------------------------------------

    /// Persist batched node positions + viewport to the backend.
    ///
    /// The backend acknowledges with HTTP 204 (no-content).  We therefore
    /// convert the successful `fetch_json` result (an empty string) into `()`
    /// so callers can use the function in a boolean-style `match` without
    /// caring about a body.
    pub async fn patch_layout(payload_json: &str, workflow_id: Option<u32>) -> Result<(), JsValue> {
        let base = Self::api_base_url();
        let url = if let Some(id) = workflow_id {
            format!("{}/api/graph/layout?workflow_id={}", base, id)
        } else {
            format!("{}/api/graph/layout", base)
        };
        Self::fetch_json(&url, "PATCH", Some(payload_json))
            .await
            .map(|_| ())
    }

    /// Update canvas data (nodes and edges) for current workflow
    pub async fn patch_workflow_canvas_data(payload_json: &str) -> Result<String, JsValue> {
        let base = Self::api_base_url();
        let url = format!("{}/api/workflows/current/canvas", base);
        Self::fetch_json(&url, "PATCH", Some(payload_json)).await
    }

    // Helper function to make fetch requests
    pub async fn fetch_json(
        url: &str,
        method: &str,
        body: Option<&str>,
    ) -> Result<String, JsValue> {
        use web_sys::{Headers, Request, RequestInit, RequestMode, Response};

        // If the page is served over HTTPS but the URL is HTTP, upgrade it to HTTPS.
        // This prevents mixed-content / CSP violations in production while keeping
        // localhost development (served via HTTP) working as-is.
        let mut effective_url = url.to_string();
        if let Some(win) = web_sys::window() {
            if let Ok(protocol) = win.location().protocol() {
                if protocol == "https:" && effective_url.starts_with("http://") {
                    // Only rewrite the scheme; host and path remain unchanged.
                    effective_url = effective_url.replacen("http://", "https://", 1);
                }
            }
        }

        let opts = RequestInit::new();
        // As above, `RequestInit` methods mutate internal JS fields via
        // interior mutability, so a `mut` binding is unnecessary.
        opts.set_method(method);
        opts.set_mode(RequestMode::Cors);

        // ----------------------------------------------------------------
        // Headers
        // ----------------------------------------------------------------
        let headers = Headers::new()?;

        // Always attempt to attach Authorization header if token present.
        if let Some(jwt) = auth_utils::current_jwt() {
            headers.append("Authorization", &format!("Bearer {}", jwt))?;
        }

        // Add Content-Type & body if provided
        if let Some(data) = body {
            let js_body = JsValue::from_str(data);
            opts.set_body(&js_body);
            headers.append("Content-Type", "application/json")?;
        }

        opts.set_headers(&headers);

        let request = Request::new_with_str_and_init(&effective_url, &opts)?;

        let window = web_sys::window().expect("no global window exists");
        let resp_value = JsFuture::from(window.fetch_with_request(&request)).await?;
        let resp: Response = resp_value.dyn_into()?;

        // Check HTTP status – handle authentication expiry gracefully.
        if !resp.ok() {
            let status = resp.status();

            // 401 → token expired or invalid → logout & show error.
            if status == 401 {
                // Attempt to log out; ignore errors (e.g. during unit tests)
                let _ = crate::utils::logout();
                crate::toast::error("Session expired. Please sign in again.");
                return Err(JsValue::from_str("Authentication failed"));
            }

            // Get response body for detailed error messages
            let error_text = if let Ok(text_future) = resp.text() {
                if let Ok(text_js) = JsFuture::from(text_future).await {
                    text_js.as_string().unwrap_or_default()
                } else {
                    String::new()
                }
            } else {
                String::new()
            };

            let status_text = resp.status_text();
            let error_message = Self::format_http_error(status, &status_text, &error_text);
            return Err(JsValue::from_str(&error_message));
        }

        // Parse body as text – caller can decode JSON.
        let text = JsFuture::from(resp.text()?).await?;
        Ok(text.as_string().unwrap_or_default())
    }

    // -------------------------------------------------------------------
    // MCP Server Management
    // -------------------------------------------------------------------

    /// List MCP servers configured for an agent
    pub async fn list_mcp_servers(agent_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{}/mcp-servers/",
            Self::api_base_url(),
            agent_id
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Add a new MCP server to an agent
    pub async fn add_mcp_server(agent_id: u32, server_config: &str) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{}/mcp-servers/",
            Self::api_base_url(),
            agent_id
        );
        Self::fetch_json(&url, "POST", Some(server_config)).await
    }

    /// Remove an MCP server from an agent
    pub async fn remove_mcp_server(agent_id: u32, server_name: &str) -> Result<(), JsValue> {
        let url = format!(
            "{}/api/agents/{}/mcp-servers/{}",
            Self::api_base_url(),
            agent_id,
            server_name
        );
        let _ = Self::fetch_json(&url, "DELETE", None).await?;
        Ok(())
    }

    /// Test connection to an MCP server
    pub async fn test_mcp_connection(agent_id: u32, test_config: &str) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{}/mcp-servers/test",
            Self::api_base_url(),
            agent_id
        );
        Self::fetch_json(&url, "POST", Some(test_config)).await
    }

    /// Get available tools from all MCP servers for an agent
    pub async fn get_mcp_available_tools(agent_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{}/mcp-servers/available-tools",
            Self::api_base_url(),
            agent_id
        );
        Self::fetch_json(&url, "GET", None).await
    }

    // -------------------------------------------------------------------
    // Authentication – Google Sign-In
    // -------------------------------------------------------------------

    /// Exchange a Google ID token for a platform JWT and persist it.
    pub async fn google_auth_login(id_token: &str) -> Result<(), JsValue> {
        let url = format!("{}/api/auth/google", Self::api_base_url());
        let payload = format!("{{\"id_token\": \"{}\"}}", id_token);

        let resp_json = Self::fetch_json(&url, "POST", Some(&payload)).await?;

        let token_out: TokenOut = serde_json::from_str(&resp_json)
            .map_err(|e| JsValue::from_str(&format!("Failed to parse token response: {:?}", e)))?;

        // Store JWT in localStorage so future fetches/websocket connections are authenticated
        if let Some(window) = web_sys::window() {
            if let Ok(Some(storage)) = window.local_storage() {
                let _ = storage.set_item("zerg_jwt", &token_out.access_token);
            }
        }

        Ok(())
    }

    // -------------------------------------------------------------------
    // Template Gallery API methods
    // -------------------------------------------------------------------

    /// List workflow templates
    pub async fn get_templates(
        category: Option<&str>,
        my_templates: bool,
    ) -> Result<String, JsValue> {
        let base = Self::api_base_url();
        let mut url = format!("{}/api/templates", base);

        let mut params = Vec::new();
        if let Some(cat) = category {
            params.push(format!("category={}", cat));
        }
        if my_templates {
            params.push("my_templates=true".to_string());
        }

        if !params.is_empty() {
            url.push('?');
            url.push_str(&params.join("&"));
        }

        Self::fetch_json(&url, "GET", None).await
    }

    /// Get template categories
    pub async fn get_template_categories() -> Result<String, JsValue> {
        let url = format!("{}/api/templates/categories", Self::api_base_url());
        Self::fetch_json(&url, "GET", None).await
    }

    /// Get a specific template by ID
    pub async fn get_template(template_id: u32) -> Result<String, JsValue> {
        let url = format!("{}/api/templates/{}", Self::api_base_url(), template_id);
        Self::fetch_json(&url, "GET", None).await
    }

    /// Deploy a template as a new workflow
    pub async fn deploy_template(
        template_id: u32,
        name: Option<&str>,
        description: Option<&str>,
    ) -> Result<String, JsValue> {
        let url = format!("{}/api/templates/deploy", Self::api_base_url());

        let mut deploy_request = format!(r#"{{"template_id": {}}}"#, template_id);

        if name.is_some() || description.is_some() {
            let mut fields = vec![format!(r#""template_id": {}"#, template_id)];
            if let Some(n) = name {
                fields.push(format!(r#""name": "{}""#, n.replace('"', r#"\""#)));
            }
            if let Some(d) = description {
                fields.push(format!(r#""description": "{}""#, d.replace('"', r#"\""#)));
            }
            deploy_request = format!("{{{}}}", fields.join(", "));
        }

        Self::fetch_json(&url, "POST", Some(&deploy_request)).await
    }

    /// Create a new template
    pub async fn create_template(
        name: &str,
        description: Option<&str>,
        category: &str,
        canvas: &str,
        tags: Option<&[&str]>,
        preview_image_url: Option<&str>,
    ) -> Result<String, JsValue> {
        let url = format!("{}/api/templates", Self::api_base_url());

        let mut fields = vec![
            format!(r#""name": "{}""#, name.replace('"', r#"\""#)),
            format!(r#""category": "{}""#, category.replace('"', r#"\""#)),
            format!(r#""canvas": {}"#, canvas),
        ];

        if let Some(desc) = description {
            fields.push(format!(
                r#""description": "{}""#,
                desc.replace('"', r#"\""#)
            ));
        }

        if let Some(tag_list) = tags {
            let tags_json = tag_list
                .iter()
                .map(|t| format!(r#""{}""#, t.replace('"', r#"\""#)))
                .collect::<Vec<_>>()
                .join(", ");
            fields.push(format!(r#""tags": [{}]"#, tags_json));
        }

        if let Some(img_url) = preview_image_url {
            fields.push(format!(
                r#""preview_image_url": "{}""#,
                img_url.replace('"', r#"\""#)
            ));
        }

        let payload = format!("{{{}}}", fields.join(", "));
        Self::fetch_json(&url, "POST", Some(&payload)).await
    }
}

// Load agents from API and update state.agents
pub fn load_agents() {
    flash_activity(); // Flash on API call

    let api_base_url = format!("{}/api/agents", ApiClient::api_base_url());

    // Call the API and update state
    wasm_bindgen_futures::spawn_local(async move {
        let window = web_sys::window().expect("no global window exists");
        let opts = web_sys::RequestInit::new();
        opts.set_method("GET");
        opts.set_mode(RequestMode::Cors);

        // Create new request
        match web_sys::Request::new_with_str_and_init(&api_base_url, &opts) {
            Ok(request) => {
                let promise = window.fetch_with_request(&request);

                match wasm_bindgen_futures::JsFuture::from(promise).await {
                    Ok(resp_value) => {
                        let response: web_sys::Response = resp_value.dyn_into().unwrap();

                        if response.ok() {
                            match response.json() {
                                Ok(json_promise) => {
                                    match wasm_bindgen_futures::JsFuture::from(json_promise).await {
                                        Ok(json_value) => {
                                            let agents_data = json_value;
                                            match serde_wasm_bindgen::from_value::<
                                                Vec<crate::models::ApiAgent>,
                                            >(
                                                agents_data
                                            ) {
                                                Ok(agents) => {
                                                    debug_log!(
                                                        "Loaded {} agents from API",
                                                        agents.len()
                                                    );

                                                    // Update the agents HashMap in AppState
                                                    crate::state::APP_STATE.with(|state| {
                                                        let mut state = state.borrow_mut();
                                                        state.agents.clear();

                                                        // Add each agent to the HashMap
                                                        for agent in agents {
                                                            if let Some(id) = agent.id {
                                                                state.agents.insert(id, agent);
                                                            }
                                                        }
                                                    // Subscribe dashboard WS manager (if already initialised)
                                                    let topic_manager_rc = state.topic_manager.clone();
                                                    let handler_opt = crate::components::dashboard::ws_manager::DASHBOARD_WS.with(|cell| {
                                                        cell.borrow().as_ref().and_then(|mgr| mgr.agent_subscription_handler.clone())
                                                    });

                                                    if let Some(handler) = handler_opt {
                                                        {
                                                            // Fail loudly if a borrow conflict exists – indicates logical error.
                                                            let mut tm = topic_manager_rc.borrow_mut();
                                                            for id in state.agents.keys() {
                                                                let topic = format!("agent:{}", id);
                                                                let _ = tm.subscribe(topic.clone(), Rc::clone(&handler));
                                                            }
                                                        }
                                                    }

                                                        // DO NOT automatically create canvas nodes for agents
                                                        // Agents should only appear on canvas when explicitly dragged from shelf
                                                        // create_nodes_for_agents(&mut state); // DISABLED - causes auto-canvas bug

                                                        state.data_loaded = true;
                                                        state.api_load_attempted = true;
                                                        state.is_loading = false;
                                                    });

                                                    // Update the UI
                                                    if let Err(e) = crate::state::AppState::refresh_ui_after_state_change() {
                                                        web_sys::console::error_1(&format!("Error refreshing UI: {:?}", e).into());
                                                    }

                                                    // Notify the application that the dashboard data changed
                                                    crate::state::dispatch_global_message(
                                                        crate::messages::Message::RefreshDashboard,
                                                    );
                                                }
                                                Err(e) => {
                                                    web_sys::console::error_1(
                                                        &format!(
                                                            "Failed to deserialize agents: {:?}",
                                                            e
                                                        )
                                                        .into(),
                                                    );
                                                    mark_load_attempted();
                                                }
                                            }
                                        }
                                        Err(e) => {
                                            web_sys::console::error_1(
                                                &format!("Failed to parse response: {:?}", e)
                                                    .into(),
                                            );
                                            mark_load_attempted();
                                        }
                                    }
                                }
                                Err(e) => {
                                    web_sys::console::error_1(
                                        &format!("Failed to call json(): {:?}", e).into(),
                                    );
                                    mark_load_attempted();
                                }
                            }
                        } else {
                            web_sys::console::error_1(
                                &format!("API request failed with status: {}", response.status())
                                    .into(),
                            );
                            mark_load_attempted();
                        }
                    }
                    Err(e) => {
                        web_sys::console::error_1(
                            &format!("Failed to fetch agents: {:?}", e).into(),
                        );
                        mark_load_attempted();
                    }
                }
            }
            Err(e) => {
                web_sys::console::error_1(&format!("Failed to create request: {:?}", e).into());
                mark_load_attempted();
            }
        }
    });
}

// Helper to mark API load as attempted but failed
fn mark_load_attempted() {
    crate::state::APP_STATE.with(|state| {
        let mut state = state.borrow_mut();
        state.api_load_attempted = true;
        state.is_loading = false;
    });
}

// Reload a specific agent by ID
pub fn reload_agent(agent_id: u32) {
    let api_url = ApiClient::api_base_url();
    let url = format!("{}/api/agents/{}", api_url, agent_id);

    wasm_bindgen_futures::spawn_local(async move {
        let window = web_sys::window().expect("no global window exists");
        let opts = web_sys::RequestInit::new();
        opts.set_method("GET");
        opts.set_mode(web_sys::RequestMode::Cors);

        match web_sys::Request::new_with_str_and_init(&url, &opts) {
            Ok(request) => {
                match JsFuture::from(window.fetch_with_request(&request)).await {
                    Ok(resp_value) => {
                        let response: web_sys::Response = resp_value.dyn_into().unwrap();

                        if response.ok() {
                            match response.json() {
                                Ok(json_promise) => {
                                    match JsFuture::from(json_promise).await {
                                        Ok(json_value) => {
                                            let agent_data = json_value;
                                            match serde_wasm_bindgen::from_value::<
                                                crate::models::ApiAgent,
                                            >(
                                                agent_data
                                            ) {
                                                Ok(agent) => {
                                                    // Update the agent in the agents HashMap
                                                    // First mutate state data
                                                    crate::state::APP_STATE.with(|state| {
                                                        let mut state = state.borrow_mut();
                                                        if let Some(id) = agent.id {
                                                            state.agents.insert(id, agent.clone());
                                                            for (_, node) in
                                                                state.workflow_nodes.iter_mut()
                                                            {
                                                                if node.get_agent_id() == Some(id) {
                                                                    node.set_text(
                                                                        agent.name.clone(),
                                                                    );
                                                                }
                                                            }
                                                        }
                                                    });

                                                    // After the previous borrow ends, mark canvas dirty via queued Message
                                                    crate::state::dispatch_global_message(
                                                        crate::messages::Message::MarkCanvasDirty,
                                                    );

                                                    // Update the UI
                                                    if let Err(e) = crate::state::AppState::refresh_ui_after_state_change() {
                                                        web_sys::console::error_1(&format!("Error refreshing UI: {:?}", e).into());
                                                    }
                                                }
                                                Err(e) => {
                                                    web_sys::console::error_1(
                                                        &format!(
                                                            "Failed to deserialize agent: {:?}",
                                                            e
                                                        )
                                                        .into(),
                                                    );
                                                }
                                            }
                                        }
                                        Err(e) => {
                                            web_sys::console::error_1(
                                                &format!("Failed to parse json: {:?}", e).into(),
                                            );
                                        }
                                    }
                                }
                                Err(e) => {
                                    web_sys::console::error_1(
                                        &format!("Failed to call json(): {:?}", e).into(),
                                    );
                                }
                            }
                        }
                    }
                    Err(e) => {
                        web_sys::console::error_1(&format!("Failed to fetch: {:?}", e).into());
                    }
                }
            }
            Err(e) => {
                web_sys::console::error_1(&format!("Failed to create request: {:?}", e).into());
            }
        }
    });
}

// Create nodes for agents that don't already have one
fn create_nodes_for_agents(state: &mut crate::state::AppState) {
    // First, collect all the information we need without holding the borrow
    let agents_to_add: Vec<(u32, String)> = state
        .agents
        .iter()
        .filter(|(agent_id, _)| {
            // Check if there's already a node for this agent
            !state
                .workflow_nodes
                .iter()
                .any(|(_, node)| node.get_agent_id() == Some(**agent_id))
        })
        .map(|(agent_id, agent)| (*agent_id, agent.name.clone()))
        .collect();

    // Calculate grid layout
    let grid_size = (agents_to_add.len() as f64).sqrt().ceil() as usize;

    // Now add the nodes without conflicting borrows
    for (i, (agent_id, name)) in agents_to_add.into_iter().enumerate() {
        // Calculate a grid-like position for the new node
        let row = i / grid_size;
        let col = i % grid_size;

        let x = 100.0 + (col as f64 * (DEFAULT_NODE_WIDTH + 50.0));
        let y = 100.0 + (row as f64 * (DEFAULT_NODE_HEIGHT + 70.0));

        let node_id = state.add_node_with_agent(
            Some(agent_id),
            x,
            y,
            crate::models::NodeType::AgentIdentity,
            name,
        );

        debug_log!(
            "Created visual node with ID: {} for agent {}",
            node_id, agent_id
        );
    }
}
