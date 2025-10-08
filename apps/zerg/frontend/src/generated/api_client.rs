
// GENERATED CODE - DO NOT EDIT
// Generated from api-schema.yml

use wasm_bindgen::JsValue;
use crate::network::api_client::ApiClient;

impl ApiClient {

    /// Reserve execution ID for workflow
    pub async fn reserve_execution_id_for_workflow(, workflow_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/by-workflow/{workflow_id}/reserve",
            Self::api_base_url(), workflow_id
        );
        Self::fetch_json(&url, "POST", None).await
    }

    /// Start new workflow execution
    pub async fn start_new_workflow_execution(, workflow_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/by-workflow/{workflow_id}/start",
            Self::api_base_url(), workflow_id
        );
        Self::fetch_json(&url, "POST", None).await
    }

    /// Start reserved execution
    pub async fn start_reserved_execution(, execution_id: u32) -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflow-executions/executions/{execution_id}/start",
            Self::api_base_url(), execution_id
        );
        Self::fetch_json(&url, "POST", None).await
    }

    /// List all workflows
    pub async fn list_all_workflows() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflows",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Create new workflow
    pub async fn create_new_workflow() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflows",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "POST", None).await
    }

    /// Get current workflow
    pub async fn get_current_workflow() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflows/current",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Update workflow canvas data
    pub async fn update_workflow_canvas_data() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/workflows/current/canvas",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "PATCH", None).await
    }

    /// List agents
    pub async fn list_agents() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Create agent
    pub async fn create_agent() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "POST", None).await
    }

    /// Get agent by ID
    pub async fn get_agent_by_id() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{agent_id}",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Update agent
    pub async fn update_agent() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{agent_id}",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "PUT", None).await
    }

    /// Delete agent
    pub async fn delete_agent() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/agents/{agent_id}",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "DELETE", None).await
    }

    /// List threads
    pub async fn list_threads() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/threads",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Get thread messages
    pub async fn get_thread_messages() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/threads/{thread_id}/messages",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "GET", None).await
    }

    /// Create thread message
    pub async fn create_thread_message() -> Result<String, JsValue> {
        let url = format!(
            "{}/api/threads/{thread_id}/messages",
            Self::api_base_url()
        );
        Self::fetch_json(&url, "POST", None).await
    }
}
