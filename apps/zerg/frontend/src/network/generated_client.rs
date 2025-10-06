use serde::{Deserialize, Serialize};
use crate::debug_log;
use wasm_bindgen_futures::spawn_local;

/// Contract-enforced models matching backend Pydantic schemas exactly
#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct WorkflowEdgeContract {
    pub from_node_id: String, // ✅ Enforces consistent naming from Phase 1
    pub to_node_id: String,   // ✅ Enforces consistent naming from Phase 1
    #[serde(default)]
    pub config: serde_json::Value,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct WorkflowNodeContract {
    pub id: String,
    #[serde(rename = "type")]
    pub node_type: String,
    pub position: PositionContract,
    #[serde(default)]
    pub config: serde_json::Value,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct PositionContract {
    pub x: f64,
    pub y: f64,
}

impl Default for PositionContract {
    fn default() -> Self {
        Self { x: 0.0, y: 0.0 }
    }
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct WorkflowDataContract {
    pub edges: Vec<WorkflowEdgeContract>,
    pub nodes: Vec<WorkflowNodeContract>,
}

/// Type-safe API client with contract validation
pub struct TypeSafeApiClient;

impl TypeSafeApiClient {
    /// Maps frontend semantic types to backend contract types
    pub(crate) fn map_node_type(frontend_type: &str) -> &str {
        match frontend_type {
            "AgentIdentity" => "agent",
            "Tool" => "tool",
            "Trigger" => "trigger",
            // All others map to conditional
            "UserInput" | "ResponseOutput" | "GenericNode" | _ => "conditional",
        }
    }

    /// Validates canvas data against contract before API call
    pub async fn update_workflow_canvas_data(canvas_data: serde_json::Value) -> Result<(), String> {
        // Normalize (map types, flatten trigger meta, remove typed field) and validate
        let workflow_data: WorkflowDataContract = crate::network::contract_glue::normalize_canvas_for_api(canvas_data)?;

        // Validate edge fields use consistent naming
        for edge in &workflow_data.edges {
            if edge.from_node_id.is_empty() || edge.to_node_id.is_empty() {
                return Err("Edge missing required from_node_id or to_node_id fields".to_string());
            }
        }

        // Use existing WASM-compatible API client with validated data
        let payload = serde_json::json!({ "canvas": serde_json::to_value(&workflow_data).unwrap_or_default() });
        let payload_str = payload.to_string();

        crate::network::ApiClient::patch_workflow_canvas_data(&payload_str)
            .await
            .map(|_| ())
            .map_err(|e| format!("API call failed: {:?}", e))
    }
}

/// Convenience function for storage layer
pub fn save_canvas_data_typed(canvas_data: serde_json::Value) {
    debug_log!("🚀 Sending canvas data via type-safe generated client");

    spawn_local(async move {
        match TypeSafeApiClient::update_workflow_canvas_data(canvas_data).await {
            Ok(_) => {
                debug_log!("✅ Canvas data saved via type-safe client");
            }
            Err(e) => {
                // Surface a user-facing toast for preflight/normalization errors to keep UX consistent
                let msg = if e.contains("Only one Manual trigger allowed per workflow") {
                    "Only one Manual trigger allowed per workflow".to_string()
                } else {
                    format!("Canvas validation failed: {}", e)
                };
                crate::toast::error(&msg);
                web_sys::console::error_1(&format!("❌ Type-safe API call failed: {}", msg).into());
            }
        }
    });
}
