/*
 * Models for the frontend application.
 *
 * ARCHITECTURE OVERVIEW
 * ---------------------
 * This file defines the data structures for our application with a clear separation of concerns:
 *
 * 1. Agent Domain Models:
 *    - ApiAgent, ApiAgentCreate, ApiAgentUpdate - these represent the business logic of agents
 *    - They are API models that store only agent functionality data (no visual properties)
 *    - Stored in the backend database and cached in the frontend state.agents HashMap
 *
 * 2. Canvas/Visualization Models:
 *    - Node - purely frontend visual representation with x, y coordinates
 *    - References an Agent by ID but doesn't embed all agent properties
 *    - Stored in the frontend state.workflow_nodes HashMap
 *
 * 3. Workflow Models:
 *    - Workflow - collection of Nodes and Edges forming a user-defined workflow
 *    - Edge - represents connections between nodes in a workflow
 *    - Stored in frontend state.workflows HashMap
 *
 * BACKEND INTEGRATION
 * ------------------
 * The frontend models align with the backend FastAPI models as follows:
 *
 * 1. Backend Agent Model:
 *    - Stored in the database with id, name, status, instructions, etc.
 *    - No visual/coordinate properties (x, y)
 *    - Accessed via /api/agents/ endpoints
 *
 * 2. Frontend Agent Representation:
 *    - ApiAgent instances are loaded from the backend and stored in state.agents
 *    - Node instances reference agents by ID and add visual properties
 *    - Changes to ApiAgent instances are synced with the backend
 *
 * 3. Frontend-only Models (for now):
 *    - Workflow and Edge are currently frontend-only and stored in localStorage
 *    - Can be extended to backend persistence in the future
 */

use serde::{Deserialize, Serialize};

// Needed for AgentDebug modal (Phase 1)
use serde_json::Value;

// -----------------------------------------------------------------------------
//  Trigger model – NEW (Phase A of Triggers front-end work)
// -----------------------------------------------------------------------------

/// Represents a **Trigger** row returned by the backend `/api/triggers` routes.
///
/// The struct mirrors the Pydantic `Trigger` schema (id, agent_id, type,
/// secret, config, created_at).  Additional backend fields will be ignored at
/// deserialisation time so we don’t need to bump the version when new
/// attributes land.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Trigger {
    pub id: u32,
    pub agent_id: u32,

    /// Either `"webhook"` or `"email"` (with provider encoded in `config`).
    pub r#type: String,

    /// Webhook secret token (hex).  Present even for non-webhook triggers so
    /// that future generic `/events` integrations can authenticate.
    pub secret: String,

    /// Arbitrary JSON blob – for email triggers holds `provider`, `filters`,
    /// etc.  Use `serde_json::Value` so we stay schema-less on the client.
    #[serde(default)]
    pub config: Option<Value>,

    pub created_at: String,
}

/// Type of node (e.g., AgentIdentity, UserInput, ResponseOutput)
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum NodeType {
    AgentIdentity,
    UserInput,
    ResponseOutput,
    GenericNode,
    Tool {
        tool_name: String,
        server_name: String,
        config: ToolConfig,
        visibility: ToolVisibility,
    },
    Trigger {
        trigger_type: TriggerType,
        config: TriggerConfig,
    },
}

/// Configuration for tool nodes
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolConfig {
    /// Static input parameters set by user
    pub static_params: std::collections::HashMap<String, serde_json::Value>,
    /// Input mappings from other nodes
    pub input_mappings: std::collections::HashMap<String, InputMapping>,
    /// Whether this tool should be executed automatically or require manual trigger
    pub auto_execute: bool,
}

/// Represents how an input parameter gets its value
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum InputMapping {
    /// Static value set by user
    Static(serde_json::Value),
    /// Value comes from output of another node
    FromNode { node_id: String, output_key: String },
}

/// Tool visibility classification for hybrid approach
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub enum ToolVisibility {
    /// Always appears as external I/O node (email readers, API calls, etc.)
    AlwaysExternal,
    /// User can choose to expose as node or keep internal to agent
    OptionalExternal,
    /// Always stays internal to agent (simple utilities)
    AlwaysInternal,
}

/// Types of triggers that can start workflows
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TriggerType {
    Webhook,
    Schedule,
    Email,
    Manual,
}

/// Configuration for trigger nodes
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggerConfig {
    /// Trigger-specific configuration (webhook URL, cron expression, etc.)
    pub params: std::collections::HashMap<String, serde_json::Value>,
    /// Whether this trigger is currently active
    pub enabled: bool,
    /// Filter conditions for the trigger
    pub filters: Vec<TriggerFilter>,
}

/// Typed trigger metadata stored in NodeConfig (single source of truth).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggerMeta {
    #[serde(rename = "type")]
    pub trigger_type: TriggerType,
    pub config: TriggerConfig,
}

/// Filter conditions for triggers
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggerFilter {
    pub field: String,
    pub operator: FilterOperator,
    pub value: serde_json::Value,
}

/// Filter operators for trigger conditions
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum FilterOperator {
    Equals,
    Contains,
    StartsWith,
    EndsWith,
    GreaterThan,
    LessThan,
    Regex,
}

/// Message represents a thread entry between user and agent
#[derive(Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: String,    // "user" or "assistant"
    pub content: String, // The actual message text
    pub timestamp: u64,  // Unix timestamp
}

pub use crate::generated::{WorkflowCanvas, WorkflowEdge, WorkflowNode};

// Add type aliases for commonly used types
pub type WorkflowNodeType = NodeType;

// Constants for config keys to avoid magic strings and typos
/// Typed configuration for workflow nodes - replaces serde_json::Map
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct NodeConfig {
    // Position and dimensions
    #[serde(default)]
    pub x: f64,
    #[serde(default)]
    pub y: f64,
    #[serde(default = "default_width")]
    pub width: f64,
    #[serde(default = "default_height")]
    pub height: f64,

    // Display properties
    #[serde(default = "default_color")]
    pub color: String,
    #[serde(default = "default_text")]
    pub text: String,

    // Node-specific properties
    #[serde(skip_serializing_if = "Option::is_none")]
    pub agent_id: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub server_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tool_config: Option<serde_json::Value>,

    /// Typed trigger semantics; prefer this over legacy `dynamic_props` keys.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trigger: Option<TriggerMeta>,

    // Dynamic properties for extensibility (trigger params, etc.)
    #[serde(flatten)]
    pub dynamic_props: std::collections::HashMap<String, serde_json::Value>,
}

// Default value functions for serde
fn default_width() -> f64 {
    200.0
}
fn default_height() -> f64 {
    80.0
}
fn default_color() -> String {
    "#3b82f6".to_string()
}
fn default_text() -> String {
    "Node".to_string()
}

impl Default for NodeConfig {
    fn default() -> Self {
        Self {
            x: 0.0,
            y: 0.0,
            width: default_width(),
            height: default_height(),
            color: default_color(),
            text: default_text(),
            agent_id: None,
            parent_id: None,
            tool_name: None,
            server_name: None,
            tool_config: None,
            trigger: None,
            dynamic_props: std::collections::HashMap::new(),
        }
    }
}

impl NodeConfig {
    /// Create a new NodeConfig with position
    pub fn new(x: f64, y: f64) -> Self {
        Self {
            x,
            y,
            ..Default::default()
        }
    }

    /// Create NodeConfig for an agent
    pub fn agent(x: f64, y: f64, agent_id: u32, text: String) -> Self {
        Self {
            x,
            y,
            agent_id: Some(agent_id),
            text,
            color: "#2ecc71".to_string(),
            ..Default::default()
        }
    }

    /// Create NodeConfig for a trigger
    pub fn trigger(x: f64, y: f64, text: String) -> Self {
        Self {
            x,
            y,
            text,
            color: "#10b981".to_string(),
            ..Default::default()
        }
    }

    /// Update visual-only fields in-place (safe against dropping semantics).
    pub fn apply_visual(&mut self, x: f64, y: f64, width: f64, height: f64, color: &str, text: &str) {
        self.x = if x.is_finite() { x.clamp(-10000.0, 10000.0) } else { 0.0 };
        self.y = if y.is_finite() { y.clamp(-10000.0, 10000.0) } else { 0.0 };
        self.width = if width.is_finite() { width.clamp(1.0, 2000.0) } else { default_width() };
        self.height = if height.is_finite() { height.clamp(1.0, 2000.0) } else { default_height() };
        self.color = color.to_string();
        self.text = text.to_string();
    }
}

// Helper methods for WorkflowNode property access
impl WorkflowNode {
    pub fn get_x(&self) -> f64 {
        self.config.x
    }

    pub fn get_y(&self) -> f64 {
        self.config.y
    }

    pub fn get_width(&self) -> f64 {
        self.config.width
    }

    pub fn get_height(&self) -> f64 {
        self.config.height
    }

    pub fn get_color(&self) -> String {
        self.config.color.clone()
    }

    pub fn get_text(&self) -> String {
        self.config.text.clone()
    }

    pub fn set_x(&mut self, x: f64) {
        // Clamp to reasonable bounds and handle NaN/infinity
        self.config.x = if x.is_finite() {
            x.clamp(-10000.0, 10000.0)
        } else {
            0.0
        };
    }

    pub fn set_y(&mut self, y: f64) {
        self.config.y = if y.is_finite() {
            y.clamp(-10000.0, 10000.0)
        } else {
            0.0
        };
    }

    pub fn set_width(&mut self, width: f64) {
        self.config.width = if width.is_finite() {
            width.clamp(1.0, 2000.0)
        } else {
            200.0
        };
    }

    pub fn set_height(&mut self, height: f64) {
        self.config.height = if height.is_finite() {
            height.clamp(1.0, 2000.0)
        } else {
            80.0
        };
    }

    pub fn set_text(&mut self, text: String) {
        self.config.text = text;
    }

    pub fn set_color(&mut self, color: String) {
        self.config.color = color;
    }

    /// Apply visual-only updates without replacing the whole NodeConfig.
    pub fn apply_visual(&mut self, x: f64, y: f64, width: f64, height: f64, color: &str, text: &str) {
        self.config.apply_visual(x, y, width, height, color, text);
    }

    pub fn get_agent_id(&self) -> Option<u32> {
        self.config.agent_id
    }

    pub fn set_agent_id(&mut self, agent_id: Option<u32>) {
        self.config.agent_id = agent_id;
    }

    pub fn get_parent_id(&self) -> Option<String> {
        self.config.parent_id.clone()
    }

    pub fn set_parent_id(&mut self, parent_id: Option<String>) {
        self.config.parent_id = parent_id;
    }

    /// Get layout as a single struct - optimized for performance
    pub fn get_layout(&self) -> NodeLayout {
        NodeLayout {
            x: self.get_x(),
            y: self.get_y(),
            width: self.get_width(),
            height: self.get_height(),
        }
    }

    /// Set layout from a single struct - more efficient than individual calls
    pub fn set_layout(&mut self, layout: &NodeLayout) {
        layout.apply_to_node(self);
    }

    /// Get the semantic node type from the generated NodeType
    pub fn get_semantic_type(&self) -> NodeType {
        match &self.node_type {
            crate::generated::workflow::NodeType::Variant0(type_str) => match type_str.as_str() {
                "UserInput" => NodeType::UserInput,
                "ResponseOutput" => NodeType::ResponseOutput,
                "AgentIdentity" => NodeType::AgentIdentity,
                "GenericNode" => NodeType::GenericNode,
                "Tool" => NodeType::Tool {
                    tool_name: self
                        .config
                        .tool_name
                        .clone()
                        .unwrap_or_else(|| "unknown".to_string()),
                    server_name: self
                        .config
                        .server_name
                        .clone()
                        .unwrap_or_else(|| "unknown".to_string()),
                    config: crate::models::ToolConfig {
                        static_params: std::collections::HashMap::new(),
                        input_mappings: std::collections::HashMap::new(),
                        auto_execute: false,
                    },
                    visibility: crate::models::ToolVisibility::AlwaysExternal,
                },
                "Trigger" => {
                    // Strict: typed trigger meta must be present
                    if let Some(meta) = &self.config.trigger {
                        return NodeType::Trigger {
                            trigger_type: meta.trigger_type.clone(),
                            config: meta.config.clone(),
                        };
                    }
                    // Fail-fast path (strict): treat as Generic for rendering; upstream validation should reject
                    crate::debug_log!("Trigger node missing config.trigger; treating as GenericNode");
                    NodeType::GenericNode
                }
                _ => NodeType::GenericNode,
            },
            crate::generated::workflow::NodeType::Variant1(_map) => {
                // If it's a complex type stored as a map, we'd need more sophisticated parsing
                NodeType::GenericNode
            }
        }
    }

    /// Set the node type from a semantic NodeType
    pub fn set_semantic_type(&mut self, node_type: &NodeType) {
        let type_str = match node_type {
            NodeType::UserInput => "UserInput",
            NodeType::ResponseOutput => "ResponseOutput",
            NodeType::AgentIdentity => "AgentIdentity",
            NodeType::GenericNode => "GenericNode",
            NodeType::Tool { .. } => "Tool",
            NodeType::Trigger { .. } => "Trigger",
        };
        self.node_type = crate::generated::workflow::NodeType::Variant0(type_str.to_string());

        // Set additional config for complex types
        match node_type {
            NodeType::Tool {
                tool_name,
                server_name,
                ..
            } => {
                self.config.tool_name = Some(tool_name.clone());
                self.config.server_name = Some(server_name.clone());
            }
            NodeType::Trigger { trigger_type, config } => {
                // Update typed trigger meta only (canonical). No legacy mirroring.
                self.config.trigger = Some(TriggerMeta {
                    trigger_type: trigger_type.clone(),
                    config: config.clone(),
                });
            }
            _ => {}
        }
    }

    /// Helper to create a WorkflowNode from semantic type
    pub fn new_with_type(node_id: String, node_type: &NodeType) -> Self {
        let mut node = WorkflowNode {
            node_id,
            node_type: crate::generated::workflow::NodeType::Variant0("GenericNode".to_string()),
            config: NodeConfig::default(),
            position: crate::network::generated_client::PositionContract::default(),
        };
        node.set_semantic_type(node_type);
        node
    }

    // No migration helpers – strict, typed-only semantics
}

/// Cached layout information for performance
#[derive(Clone, Debug, PartialEq)]
pub struct NodeLayout {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

impl Default for NodeLayout {
    fn default() -> Self {
        Self {
            x: 0.0,
            y: 0.0,
            width: 200.0,
            height: 80.0,
        }
    }
}

impl NodeLayout {
    pub fn from_node(node: &WorkflowNode) -> Self {
        Self {
            x: node.get_x(),
            y: node.get_y(),
            width: node.get_width(),
            height: node.get_height(),
        }
    }

    pub fn apply_to_node(&self, node: &mut WorkflowNode) {
        node.set_x(self.x);
        node.set_y(self.y);
        node.set_width(self.width);
        node.set_height(self.height);
    }
}

// Add UI-only state wrapper:
#[derive(Clone, Default)]
pub struct UiNodeState {
    pub is_selected: bool,
    pub is_dragging: bool,
    pub exec_status: Option<NodeExecStatus>,
    pub transition_animation: Option<TransitionAnimation>,
    /// Cached layout for performance - avoids HashMap lookups every frame
    pub cached_layout: Option<NodeLayout>,
    /// Cached semantic node type - avoids string->enum conversion
    pub cached_node_type: Option<NodeType>,
}

impl UiNodeState {
    /// Sync cached data from a WorkflowNode - call this when the node changes
    pub fn sync_from_node(&mut self, node: &WorkflowNode) {
        self.cached_layout = Some(NodeLayout::from_node(node));
        self.cached_node_type = Some(node.get_semantic_type());
    }

    /// Get cached layout, falling back to node if not cached
    pub fn get_layout(&self, node: &WorkflowNode) -> NodeLayout {
        self.cached_layout
            .clone()
            .unwrap_or_else(|| NodeLayout::from_node(node))
    }

    /// Get cached node type, falling back to node if not cached
    pub fn get_node_type(&self, node: &WorkflowNode) -> NodeType {
        self.cached_node_type
            .clone()
            .unwrap_or_else(|| node.get_semantic_type())
    }

    /// Invalidate cache when node changes
    pub fn invalidate_cache(&mut self) {
        self.cached_layout = None;
        self.cached_node_type = None;
    }
}

pub type UiStateMap = std::collections::HashMap<String, UiNodeState>;

/// UI state for workflow edges (connections between nodes)
#[derive(Debug, Clone)]
pub struct UiEdgeState {
    pub is_executing: bool,
    pub source_node_running: bool,
    pub target_node_running: bool,
}

impl Default for UiEdgeState {
    fn default() -> Self {
        Self {
            is_executing: false,
            source_node_running: false,
            target_node_running: false,
        }
    }
}

impl UiEdgeState {
    /// Update execution state based on connected nodes
    pub fn update_from_nodes(&mut self, source_running: bool, target_running: bool) {
        self.source_node_running = source_running;
        self.target_node_running = target_running;
        self.is_executing = source_running || target_running;
    }
}

pub type UiEdgeStateMap = std::collections::HashMap<String, UiEdgeState>;

/// Phase/Result execution state architecture
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Phase {
    Waiting,
    Running,
    Finished,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExecutionResult {
    Success,
    Failure,
    Cancelled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum FailureKind {
    User,
    System,
    Timeout,
    External,
    Unknown,
}

/// Node execution status for UI display
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum NodeExecStatus {
    Waiting,
    Running,
    Completed,
    Failed,
}

impl From<(Phase, Option<ExecutionResult>)> for NodeExecStatus {
    fn from((phase, result): (Phase, Option<ExecutionResult>)) -> Self {
        match phase {
            Phase::Waiting => NodeExecStatus::Waiting,
            Phase::Running => NodeExecStatus::Running,
            Phase::Finished => match result {
                Some(ExecutionResult::Success) => NodeExecStatus::Completed,
                Some(ExecutionResult::Failure) | Some(ExecutionResult::Cancelled) => {
                    NodeExecStatus::Failed
                }
                None => NodeExecStatus::Failed, // Invalid state - shouldn't happen
            },
        }
    }
}

/// Transition animation state for visual effects on status changes
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TransitionAnimation {
    pub animation_type: TransitionType,
    pub start_time: f64,
    pub duration: f64,
}

/// Types of transition animations
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum TransitionType {
    SuccessFlash,
    ErrorShake,
}

// ---------------------------------------------------------------------------
//   Execution history (sidebar)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct ExecutionSummary {
    pub id: u32,
    pub status: String, // running | success | failed | cancelled

    #[serde(default)]
    pub started_at: Option<String>,

    #[serde(default)]
    pub finished_at: Option<String>,

    #[serde(default)]
    pub duration_ms: Option<u64>,

    #[serde(default)]
    pub error: Option<String>,
}

// -----------------------------------------------------------------------------
// Backend **Workflow** DTO (matches FastAPI schema) ---------------------------
// -----------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiWorkflow {
    pub id: u32,
    pub owner_id: u32,
    pub name: String,
    pub description: Option<String>,
    #[serde(default)]
    pub canvas: serde_json::Value,
    #[serde(default)]
    pub is_active: bool,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

impl From<ApiWorkflow> for WorkflowCanvas {
    fn from(api: ApiWorkflow) -> Self {
        serde_json::from_value(api.canvas).expect("backend guarantees canonical schema")
    }
}

impl ApiWorkflow {
    /// Get nodes from canvas
    pub fn get_nodes(&self) -> Vec<WorkflowNode> {
        self.canvas
            .get("nodes")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| serde_json::from_value(v.clone()).ok())
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Get edges from canvas
    pub fn get_edges(&self) -> Vec<WorkflowEdge> {
        self.canvas
            .get("edges")
            .and_then(|v| v.as_array())
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| {
                        let mut edge: WorkflowEdge = serde_json::from_value(v.clone()).ok()?;

                        // Fix double-nested config structure from backend
                        if let Some(config_obj) = edge.config.get("config") {
                            if let Some(inner_config) = config_obj.as_object() {
                                // Replace the double-nested config with the inner config
                                edge.config = inner_config.clone();
                            }
                        }

                        Some(edge)
                    })
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Get mutable access to nodes (for internal use)
    pub fn get_nodes_mut(&mut self) -> &mut serde_json::Value {
        if !self.canvas.is_object() {
            self.canvas = serde_json::json!({});
        }
        let canvas_obj = self.canvas.as_object_mut().unwrap();
        canvas_obj.entry("nodes").or_insert(serde_json::json!([]));
        &mut canvas_obj["nodes"]
    }

    /// Get mutable access to edges (for internal use)
    pub fn get_edges_mut(&mut self) -> &mut serde_json::Value {
        if !self.canvas.is_object() {
            self.canvas = serde_json::json!({});
        }
        let canvas_obj = self.canvas.as_object_mut().unwrap();
        canvas_obj.entry("edges").or_insert(serde_json::json!([]));
        &mut canvas_obj["edges"]
    }

    /// Add a node to the workflow
    pub fn add_node(&mut self, node: WorkflowNode) {
        let nodes_array = self.get_nodes_mut();
        if let Some(arr) = nodes_array.as_array_mut() {
            // Remove existing node with same ID
            arr.retain(|v| v.get("node_id").and_then(|id| id.as_str()) != Some(&node.node_id));
            // Add new node
            arr.push(serde_json::to_value(&node).unwrap());
        }
    }

    /// Add an edge to the workflow
    pub fn add_edge(&mut self, edge: WorkflowEdge) {
        let edges_array = self.get_edges_mut();
        if let Some(arr) = edges_array.as_array_mut() {
            arr.push(serde_json::to_value(&edge).unwrap());
        }
    }

    /// Remove a node and its associated edges
    pub fn remove_node(&mut self, node_id: &str) {
        // Remove node
        let nodes_array = self.get_nodes_mut();
        if let Some(arr) = nodes_array.as_array_mut() {
            arr.retain(|v| v.get("node_id").and_then(|id| id.as_str()) != Some(node_id));
        }

        // Remove associated edges
        let edges_array = self.get_edges_mut();
        if let Some(arr) = edges_array.as_array_mut() {
            arr.retain(|v| {
                let from_id = v.get("from_node_id").and_then(|id| id.as_str());
                let to_id = v.get("to_node_id").and_then(|id| id.as_str());
                from_id != Some(node_id) && to_id != Some(node_id)
            });
        }
    }
}

// API models that match the backend schema
// These are used for API requests and responses

/// ApiAgent represents an agent in the backend database
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiAgent {
    pub id: Option<u32>,
    pub name: String,
    pub status: Option<String>,
    pub system_instructions: Option<String>,
    pub task_instructions: Option<String>,
    pub model: Option<String>,
    pub temperature: Option<f64>,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
    // -------- Scheduling metadata (Milestone 0) --------
    /// Cron expression defining when the agent should run (None means not scheduled)
    pub schedule: Option<String>,
    /// ISO‑8601 timestamp of the next scheduled run (set by backend)
    pub next_run_at: Option<String>,
    /// ISO‑8601 timestamp when the agent last finished a run
    pub last_run_at: Option<String>,

    /// If the last run produced an error the backend stores the message here.
    /// `None` or empty string means the agent is healthy.
    #[serde(default)]
    pub last_error: Option<String>,

    // ---------------------------------------------------------------------
    // Ownership (User Personalisation feature)
    // ---------------------------------------------------------------------
    #[serde(default)]
    pub owner_id: Option<u32>,

    // Nested owner payload – present when the backend embeds `owner` in the
    // `Agent` schema (scope=all) so the dashboard can render the avatar +
    // name column.  The struct mirrors the subset of `UserOut` actually used
    // by the front-end; additional fields are ignored by `serde`.
    #[serde(default)]
    pub owner: Option<ApiUserPublic>,
}

// -----------------------------------------------------------------------------
//  Lightweight user representation for embedded `owner` objects
// -----------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ApiUserPublic {
    pub id: u32,
    pub email: String,

    #[serde(default)]
    pub display_name: Option<String>,

    #[serde(default)]
    pub avatar_url: Option<String>,
}

// -----------------------------------------------------------------------------
//  Run History – new API model
// -----------------------------------------------------------------------------

/// A single execution (run) of an agent task. Mirrors the backend `AgentRun` model.
///
/// Only the subset of fields required for the dashboard UI is included. Additional
/// telemetry (token_count, cost, etc.) can be added later without changing
/// existing code as Serde will simply ignore missing struct fields.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ApiAgentRun {
    pub id: u32,
    pub agent_id: u32,
    pub thread_id: Option<u32>,

    pub status: String,  // queued | running | success | failed
    pub trigger: String, // manual | schedule | api

    pub started_at: Option<String>,
    pub finished_at: Option<String>,
    pub duration_ms: Option<u64>,

    #[serde(default)]
    pub total_tokens: Option<u32>,
    #[serde(default)]
    pub total_cost_usd: Option<f64>,

    #[serde(default)]
    pub error: Option<String>,
}

// -----------------------------------------------------------------------------
//  User profile (CurrentUser)
// -----------------------------------------------------------------------------

/// Authenticated user profile returned from `/api/users/me`.
///
/// Only the subset of fields actually required by the frontend is modelled
/// here.  Additional backend attributes will be ignored during `serde`
/// deserialization so the backend can evolve freely.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CurrentUser {
    pub id: u32,
    pub email: String,

    #[serde(default)]
    pub display_name: Option<String>,

    #[serde(default)]
    pub avatar_url: Option<String>,

    #[serde(default)]
    pub prefs: Option<serde_json::Value>,

    // ---------------- Gmail integration (Phase-C) ----------------
    #[serde(default)]
    pub gmail_connected: bool,
}

// -----------------------------------------------------------------------------
// Convenience impls
// -----------------------------------------------------------------------------

impl ApiAgent {
    /// Returns `true` if the `schedule` field contains a non-empty cron string.
    ///
    /// The backend no longer stores a separate `run_on_schedule` flag – an
    /// agent is considered *scheduled* whenever the cron expression is set.
    pub fn is_scheduled(&self) -> bool {
        self.schedule
            .as_ref()
            .map(|s| !s.trim().is_empty())
            .unwrap_or(false)
    }
}

// -----------------------------------------------------------------------------
//  Ops Dashboard Models
// -----------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OpsBudget {
    pub limit_cents: Option<u32>,
    pub used_usd: Option<f64>,
    #[serde(default)]
    pub percent: Option<f64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OpsTopAgent {
    pub agent_id: u32,
    pub name: String,
    pub owner_email: Option<String>,
    pub runs: u32,
    pub cost_usd: Option<f64>,
    pub p95_ms: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OpsLatencyMs {
    pub p50: Option<u32>,
    pub p95: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OpsSummary {
    pub runs_today: u32,
    pub cost_today_usd: Option<f64>,
    pub budget_user: Option<OpsBudget>,
    pub budget_global: Option<OpsBudget>,
    pub active_users_24h: u32,
    pub agents_total: u32,
    pub agents_scheduled: u32,
    pub latency_ms: Option<OpsLatencyMs>,
    pub errors_last_hour: u32,
    #[serde(default)]
    pub top_agents_today: Vec<OpsTopAgent>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OpsSeriesPoint {
    pub hour_iso: String,
    pub value: f64,
}

// -----------------------------------------------------------------------------
// Tests
// -----------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use wasm_bindgen_test::*;

    wasm_bindgen_test_configure!(run_in_browser);

    #[wasm_bindgen_test]
    fn test_is_scheduled() {
        // Case 1: None -> not scheduled
        let agent_none = ApiAgent {
            id: Some(1),
            name: "A".to_string(),
            status: None,
            system_instructions: None,
            task_instructions: None,
            model: None,
            temperature: None,
            created_at: None,
            updated_at: None,
            schedule: None,
            next_run_at: None,
            last_run_at: None,
            last_error: None,

            owner_id: None,
            owner: None,
        };
        assert!(!agent_none.is_scheduled());

        // Case 2: Empty string -> not scheduled
        let agent_empty = ApiAgent {
            schedule: Some("   ".to_string()),
            ..agent_none.clone()
        };
        assert!(!agent_empty.is_scheduled());

        // Case 3: Valid cron -> scheduled
        let agent_cron = ApiAgent {
            schedule: Some("0 * * * *".to_string()),
            ..agent_empty.clone()
        };
        assert!(agent_cron.is_scheduled());
    }

    // ---------------------------------------------------------------------
    // New test – ApiAgent deserialisation with `owner_id` and nested owner
    // ---------------------------------------------------------------------
    #[wasm_bindgen_test]
    fn test_api_agent_with_owner() {
        let json = r#"{
            "id": 42,
            "name": "Test agent",
            "status": "idle",
            "owner_id": 7,
            "owner": {
                "id": 7,
                "email": "bob@example.com",
                "display_name": "Bob",
                "avatar_url": null
            }
        }"#;

        let agent: ApiAgent = serde_json::from_str(json).expect("ApiAgent with owner json");
        assert_eq!(agent.owner_id, Some(7));
        assert!(agent.owner.is_some());
        let owner = agent.owner.unwrap();
        assert_eq!(owner.display_name.unwrap(), "Bob");
    }

    #[wasm_bindgen_test]
    fn test_current_user_deserialize() {
        let json = r#"{
            "id": 1,
            "email": "alice@example.com",
            "display_name": "Alice",
            "avatar_url": null,
            "prefs": null,
            "gmail_connected": false
        }"#;

        let user: CurrentUser = serde_json::from_str(json).expect("CurrentUser JSON");
        assert_eq!(user.email, "alice@example.com");
    }

    #[wasm_bindgen_test]
    fn test_workflow_node_serialization_roundtrip() {
        let mut node =
            WorkflowNode::new_with_type("test-node".to_string(), &NodeType::AgentIdentity);
        node.set_x(100.0);
        node.set_y(200.0);
        node.set_width(300.0);
        node.set_height(80.0);
        node.set_text("Test Agent".to_string());
        node.set_color("#2ecc71".to_string());
        node.set_agent_id(Some(42));

        // Serialize to JSON
        let json = serde_json::to_string(&node).expect("Serialization failed");

        // Deserialize back
        let deserialized: WorkflowNode =
            serde_json::from_str(&json).expect("Deserialization failed");

        // Verify the round-trip
        assert_eq!(node.node_id, deserialized.node_id);
        assert_eq!(node.get_x(), deserialized.get_x());
        assert_eq!(node.get_y(), deserialized.get_y());
        assert_eq!(node.get_width(), deserialized.get_width());
        assert_eq!(node.get_height(), deserialized.get_height());
        assert_eq!(node.get_text(), deserialized.get_text());
        assert_eq!(node.get_color(), deserialized.get_color());
        assert_eq!(node.get_agent_id(), deserialized.get_agent_id());

        // Verify semantic type conversion works
        assert!(matches!(
            deserialized.get_semantic_type(),
            NodeType::AgentIdentity
        ));
    }

    #[wasm_bindgen_test]
    fn test_node_layout_cache_performance() {
        let mut node = WorkflowNode::new_with_type("test".to_string(), &NodeType::GenericNode);
        node.set_x(10.0);
        node.set_y(20.0);
        node.set_width(100.0);
        node.set_height(50.0);

        let mut ui_state = UiNodeState::default();
        ui_state.sync_from_node(&node);

        // Test cached access
        let cached_layout = ui_state.get_layout(&node);
        assert_eq!(cached_layout.x, 10.0);
        assert_eq!(cached_layout.y, 20.0);
        assert_eq!(cached_layout.width, 100.0);
        assert_eq!(cached_layout.height, 50.0);

        // Test cached type access
        let cached_type = ui_state.get_node_type(&node);
        assert!(matches!(cached_type, NodeType::GenericNode));
    }

    #[wasm_bindgen_test]
    fn test_safe_property_setters() {
        let mut node = WorkflowNode::new_with_type("test".to_string(), &NodeType::GenericNode);

        // Test NaN/infinity handling
        node.set_x(f64::NAN);
        assert_eq!(node.get_x(), 0.0); // Should fallback to 0.0

        node.set_x(f64::INFINITY);
        assert_eq!(node.get_x(), 0.0); // Should fallback to 0.0

        // Test clamping
        node.set_x(-20000.0);
        assert_eq!(node.get_x(), -10000.0); // Should clamp to min

        node.set_width(-10.0);
        assert_eq!(node.get_width(), 1.0); // Should clamp to min

        node.set_width(5000.0);
        assert_eq!(node.get_width(), 2000.0); // Should clamp to max
    }

    #[wasm_bindgen_test]
    fn test_workflow_canvas_roundtrip() {
        let mut canvas = WorkflowCanvas::default();

        // Add a node
        let node = WorkflowNode::new_with_type("test-node".to_string(), &NodeType::UserInput);
        canvas.nodes.push(node);

        // Add an edge
        let edge = WorkflowEdge {
            from_node_id: "test-node".to_string(),
            to_node_id: "test-node-2".to_string(),
            config: serde_json::Map::new(),
        };
        canvas.edges.push(edge);

        // Serialize to JSON
        let json = serde_json::to_string(&canvas).expect("Canvas serialization failed");

        // Deserialize back
        let deserialized: WorkflowCanvas =
            serde_json::from_str(&json).expect("Canvas deserialization failed");

        // Verify the round-trip
        assert_eq!(canvas.nodes.len(), deserialized.nodes.len());
        assert_eq!(canvas.edges.len(), deserialized.edges.len());
        assert_eq!(canvas.nodes[0].node_id, deserialized.nodes[0].node_id);
        assert_eq!(
            canvas.edges[0].from_node_id,
            deserialized.edges[0].from_node_id
        );
    }
}

/// Wrapper returned by `/api/agents/{id}/details`
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiAgentDetails {
    pub agent: ApiAgent,

    // Optional heavy payloads – currently None / empty in Phase 1
    #[serde(default)]
    pub threads: Option<Vec<ApiThread>>,

    #[serde(default)]
    pub runs: Option<Vec<Value>>,

    #[serde(default)]
    pub stats: Option<Value>,
}

/// ApiAgentCreate is used when creating a new agent
#[derive(Clone, Serialize, Deserialize)]
pub struct ApiAgentCreate {
    pub name: String,
    pub system_instructions: String,
    pub task_instructions: String,
    pub model: Option<String>,
    pub schedule: Option<String>,
    pub config: Option<serde_json::Value>,
}

/// ApiAgentUpdate is used when updating an existing agent
#[derive(Clone, Serialize, Deserialize)]
pub struct ApiAgentUpdate {
    pub name: Option<String>,
    pub status: Option<String>,
    pub system_instructions: Option<String>,
    pub task_instructions: Option<String>,
    pub model: Option<String>,
    pub schedule: Option<String>,
    pub config: Option<serde_json::Value>,

    /// Optional error string – only present if we need to update/clear it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub last_error: Option<String>,
}

/// ApiMessage represents a message in the agent thread
#[derive(Clone, Serialize, Deserialize)]
pub struct ApiMessage {
    pub id: Option<u32>,
    pub agent_id: u32,
    pub role: String,
    pub content: String,
    pub timestamp: Option<String>,
}

/// ApiMessageCreate is used when creating a new message
#[derive(Clone, Serialize, Deserialize)]
pub struct ApiMessageCreate {
    pub role: String,
    pub content: String,
}

/// Thread model from the API
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiThread {
    pub id: Option<u32>,
    pub agent_id: u32,
    pub title: String,
    pub active: bool,
    pub created_at: Option<String>,
    pub updated_at: Option<String>,
    /// Optional eagerly-loaded messages list returned by the `/api/threads` endpoint.
    ///
    /// For large installations this field may be truncated or omitted by the
    /// backend; therefore we mark it with `#[serde(default)]` so deserialising
    /// older responses (or those with pagination) continues to work.
    #[serde(default)]
    pub messages: Vec<ApiThreadMessage>,
}

/// Create a new thread
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiThreadCreate {
    pub agent_id: u32,
    pub title: String,
    pub active: bool,
}

/// Update a thread
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiThreadUpdate {
    pub title: Option<String>,
    pub active: Option<bool>,
}

/// ApiThreadMessage represents a message in a thread
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiThreadMessage {
    pub id: Option<u32>,
    pub thread_id: u32,
    pub role: String,
    pub content: String,
    pub timestamp: Option<String>,
    // New fields for tool message display
    #[serde(default)]
    pub message_type: Option<String>, // "tool_output" or "assistant_message"
    #[serde(default)]
    pub tool_name: Option<String>,
    #[serde(default)]
    pub tool_call_id: Option<String>,
    /// Optional serialized inputs for the tool call (if provided by backend)
    #[serde(default)]
    pub tool_input: Option<String>,
    #[serde(default)]
    pub parent_id: Option<u32>,
}

/// Create a new thread message
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApiThreadMessageCreate {
    pub role: String,
    pub content: String,
}

// -----------------------------------------------------------------------------
// Template Gallery Models
// -----------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WorkflowTemplate {
    pub id: u32,
    pub created_by: u32,
    pub name: String,
    pub description: Option<String>,
    pub category: String,
    pub canvas: serde_json::Value,
    pub tags: Vec<String>,
    pub preview_image_url: Option<String>,
    pub is_public: bool,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TemplateCategory {
    pub name: String,
    pub count: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TemplateDeployRequest {
    pub template_id: u32,
    pub name: Option<String>,
    pub description: Option<String>,
}

/// Response model for super admin status check
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SuperAdminStatus {
    pub is_super_admin: bool,
    pub requires_password: bool,
}
