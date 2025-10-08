// AUTO-GENERATED - DO NOT EDIT
// Generated from api-schema.yml

use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct WorkflowNode {
    pub id: String,
    #[serde(rename = "type")]
    pub type_: String,
    pub position: Position,
    pub config: Option<serde_json::Value>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Position {
    pub x: f64,
    pub y: f64,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct WorkflowEdge {
    pub from_node_id: String,
    pub to_node_id: String,
    pub config: Option<serde_json::Value>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct WorkflowCanvas {
    pub nodes: Vec<WorkflowNode>,
    pub edges: Vec<WorkflowEdge>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Workflow {
    pub id: i32,
    pub name: String,
    pub description: Option<String>,
    pub canvas_data: Option<WorkflowCanvas>,
    pub created_at: chrono::DateTime<chrono::Utc>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub enum AgentStatus {
    #[serde(rename = "idle")]
    Idle,
    #[serde(rename = "running")]
    Running,
    #[serde(rename = "error")]
    Error,
    #[serde(rename = "processing")]
    Processing,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Agent {
    pub id: i32,
    pub name: String,
    pub status: AgentStatus,
    pub system_instructions: String,
    pub task_instructions: Option<String>,
    pub model: Option<String>,
    pub created_at: Option<chrono::DateTime<chrono::Utc>>,
    pub updated_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Thread {
    pub id: i32,
    pub title: String,
    pub agent_id: i32,
    pub created_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct Message {
    pub id: i32,
    pub role: String,
    pub content: String,
    pub metadata: Option<serde_json::Value>,
    pub created_at: Option<chrono::DateTime<chrono::Utc>>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CreateWorkflowRequest {
    pub name: String,
    pub description: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct CreateAgentRequest {
    pub name: String,
    pub system_instructions: String,
    pub task_instructions: Option<String>,
    pub model: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ErrorResponse {
    pub error: String,
    pub message: String,
    pub details: Option<serde_json::Value>,
}
