use crate::models::ApiAgentRun;
use crate::models::{
    ApiAgent, ApiThread, ApiThreadMessage, ApiWorkflow, NodeType, Trigger, UiEdgeStateMap,
    UiNodeState, UiStateMap, WorkflowEdge, WorkflowNode, WorkflowNodeType,
};
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use web_sys::{CanvasRenderingContext2d, HtmlCanvasElement, WebSocket};
use crate::debug_log;

use crate::canvas::{background::ParticleSystem, renderer};
use crate::messages::{Command, Message};
use crate::models::ApiAgentDetails;
use crate::models::OpsSummary;
use crate::network::{TopicManager, WsClientV2};
use crate::storage::ActiveView;

// ---------------------------------------------------------------------------
//  Workflow execution helper structs (UI state only)
// ---------------------------------------------------------------------------

#[derive(Clone, PartialEq, Debug)]
pub enum ExecPhase {
    Starting,
    Running,
    Success,
    Failed,
}

#[derive(Clone)]
pub struct ExecutionStatus {
    pub execution_id: u32,
    pub status: ExecPhase,
}

#[derive(Clone)]
pub struct ExecutionLog {
    pub node_id: String,
    pub stream: String, // stdout | stderr
    pub text: String,
}
use crate::constants::{DEFAULT_AGENT_NODE_COLOR, DEFAULT_NODE_HEIGHT, DEFAULT_NODE_WIDTH};
use js_sys::Date;

use crate::update;
use wasm_bindgen::closure::Closure;
use wasm_bindgen::JsCast;
use wasm_bindgen::JsValue;

// ---------------------------------------------------------------------------
// Viewport constraints – keep values sane so we never generate Inf/NaN or
// absurd world-space coordinates.
// Default zoom == 1.0 so we allow ±50 %.
// ---------------------------------------------------------------------------

// Zoom is currently disabled – hard-lock to 100 %.
pub const MIN_ZOOM: f64 = 1.0;
pub const MAX_ZOOM: f64 = 1.0;
// (legacy CanvasNode helper trait removed; state uses modern workflow models)

// Lightweight ticker entry for Ops events rendered in the dashboard
#[derive(Clone, Debug)]
pub struct OpsTick {
    pub ts: u64,
    pub kind: String,
    pub text: String,
}

// ---------------------------------------------------------------------------
// Agent Debug Pane (read-only modal) – Phase 1
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DebugTab {
    Overview,
    RawJson,
}

// ---------------------------------------------------------------------------
// Agent Configuration Modal – currently three fixed tabs.
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AgentConfigTab {
    Main,
    #[allow(dead_code)] // planned UI tab – keep placeholder
    History,
    Triggers,
    ToolsIntegrations,
}

// ---------------------------------------------------------------------------
// Dashboard filter scope – either *my* (default) or *all* (admin)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DashboardScope {
    MyAgents,
    AllAgents,
}

impl DashboardScope {
    pub fn as_str(&self) -> &'static str {
        match self {
            DashboardScope::MyAgents => "my",
            DashboardScope::AllAgents => "all",
        }
    }

    pub fn from_str(s: &str) -> Self {
        match s {
            "all" => DashboardScope::AllAgents,
            _ => DashboardScope::MyAgents,
        }
    }
}

#[derive(Debug, Clone)]
pub struct AgentDebugPane {
    pub agent_id: u32,
    pub loading: bool,
    pub details: Option<ApiAgentDetails>,
    pub active_tab: DebugTab,
}


// ---------------------------------------------------------------------------
// MCP Integration State Structures
// ---------------------------------------------------------------------------

/// Configuration for MCP servers connected to an agent
#[derive(Debug, Clone)]
pub struct AgentMcpConfig {
    /// List of MCP servers configured for this agent
    pub servers: Vec<McpServerConfig>,
    /// Set of tool names that the agent is allowed to use
    pub allowed_tools: HashSet<String>,
}

use serde::Serialize;

/// Individual MCP server configuration
#[derive(Debug, Clone, Serialize)]
pub struct McpServerConfig {
    /// Display name for the server
    pub name: String,
    /// URL for custom MCP servers (None for presets)
    pub url: Option<String>,
    /// Preset name (e.g., "github", "linear") if using a preset
    pub preset: Option<String>,
    /// Authentication token (encrypted on backend)
    pub auth_token: Option<String>,
}

/// Information about a tool provided by an MCP server
#[derive(Debug, Clone)]
pub struct McpToolInfo {
    /// Tool name (e.g., "create_issue")
    pub name: String,
    /// Server that provides this tool
    pub server_name: String,
    /// Human-readable description
    pub description: Option<String>,
}

/// Connection status for an MCP server
#[derive(Debug, Clone)]
pub enum ConnectionStatus {
    /// Server is healthy and responding
    Healthy,
    /// Server is responding but slowly (with response time in ms)
    Slow(u64),
    /// Server connection failed with error message
    Failed(String),
    /// Currently checking connection status
    Checking,
}

// ---------------------------------------------------------------------------
// Agent-Centric State Management (NEW)
// ---------------------------------------------------------------------------

/// Complete state for a single agent, including all its threads and UI state
#[derive(Debug, Clone)]
pub struct AgentState {
    /// Agent metadata from API
    pub metadata: ApiAgent,
    /// All threads owned by this agent
    pub threads: HashMap<u32, ApiThread>,
    /// Currently selected thread for this agent
    pub current_thread_id: Option<u32>,
    /// Messages for each thread (thread_id -> messages)
    pub thread_messages: HashMap<u32, Vec<ApiThreadMessage>>,
    /// Whether this agent is currently streaming
    pub is_streaming: bool,
    /// Active stream tracking for this agent's threads
    pub active_streams: HashMap<u32, Option<u32>>,
    /// Threads in token streaming mode
    pub token_mode_threads: HashSet<u32>,
}

impl AgentState {
    /// Create a new AgentState for the given agent
    pub fn new(metadata: ApiAgent) -> Self {
        Self {
            metadata,
            threads: HashMap::new(),
            current_thread_id: None,
            thread_messages: HashMap::new(),
            is_streaming: false,
            active_streams: HashMap::new(),
            token_mode_threads: HashSet::new(),
        }
    }

    /// Get all threads for this agent as a sorted vector (newest first)
    pub fn get_threads_sorted(&self) -> Vec<&ApiThread> {
        let mut threads: Vec<&ApiThread> = self.threads.values().collect();
        threads.sort_by(|a, b| {
            let a_time = a
                .updated_at
                .as_ref()
                .or(a.created_at.as_ref())
                .map(|s| s.as_str())
                .unwrap_or("");
            let b_time = b
                .updated_at
                .as_ref()
                .or(b.created_at.as_ref())
                .map(|s| s.as_str())
                .unwrap_or("");
            b_time.cmp(a_time) // Newest first
        });
        threads
    }

    /// Get the currently selected thread
    pub fn current_thread(&self) -> Option<&ApiThread> {
        self.current_thread_id.and_then(|id| self.threads.get(&id))
    }

    /// Get messages for the current thread
    pub fn current_thread_messages(&self) -> Vec<&ApiThreadMessage> {
        if let Some(thread_id) = self.current_thread_id {
            self.thread_messages
                .get(&thread_id)
                .map(|msgs| msgs.iter().collect())
                .unwrap_or_default()
        } else {
            Vec::new()
        }
    }

    /// Clear all thread data (for clean navigation)
    pub fn clear_threads(&mut self) {
        self.threads.clear();
        self.thread_messages.clear();
        self.current_thread_id = None;
        self.active_streams.clear();
        self.token_mode_threads.clear();
        self.is_streaming = false;
    }
}

// Store global application state
pub struct AppState {
    /// If true, global keyboard shortcuts (power mode) are enabled
    pub power_mode: bool,
    // Particle system for animated background
    pub particle_system: Option<ParticleSystem>,
    // Agent domain data (business logic)
    pub agents: HashMap<u32, ApiAgent>, // Backend agent data
    pub agents_on_canvas: HashSet<u32>, // Track which agents are already placed on canvas

    // Canvas visualization data
    pub workflow_nodes: HashMap<String, WorkflowNode>,
    pub ui_state: UiStateMap,
    pub ui_edge_state: UiEdgeStateMap,
    pub workflows: HashMap<u32, ApiWorkflow>, // Workflows collection
    pub current_workflow_id: Option<u32>,     // Currently active workflow

    // Canvas and rendering related
    pub canvas: Option<HtmlCanvasElement>,
    pub context: Option<CanvasRenderingContext2d>,
    pub connection_animation_offset: f64,
    pub input_text: String,
    pub dragging: Option<String>,
    pub drag_offset_x: f64,
    pub drag_offset_y: f64,
    // New fields for canvas dragging
    pub canvas_dragging: bool,
    pub drag_start_x: f64,
    pub drag_start_y: f64,
    pub drag_last_x: f64,
    pub drag_last_y: f64,
    #[allow(dead_code)]
    pub websocket: Option<WebSocket>,
    // Canvas dimensions
    pub canvas_width: f64,
    pub canvas_height: f64,
    // Viewport tracking for zoom-to-fit functionality
    pub viewport_x: f64,
    pub viewport_y: f64,
    pub zoom_level: f64,
    pub auto_fit: bool,

    // ------------------------------------------------------------------
    // Workflow execution – live run status & logs
    // ------------------------------------------------------------------
    pub current_execution: Option<ExecutionStatus>,
    pub execution_logs: Vec<ExecutionLog>,

    // Execution history sidebar
    pub exec_history_open: bool,
    pub executions: Vec<crate::models::ExecutionSummary>,
    pub logs_open: bool,

    // (duplicate fields removed)
    // Track the latest user input node ID
    pub latest_user_input_id: Option<String>,
    // Track message IDs and their corresponding node IDs
    pub message_id_to_node_id: HashMap<String, String>,
    // Selected AI model
    pub selected_model: String,
    // Available AI models
    pub available_models: Vec<(String, String)>,
    // Default AI model ID
    pub default_model_id: String,
    // Whether state has been modified since last save
    pub state_modified: bool,

    /// Timestamp (ms) of the **last modification** that set `state_modified`
    /// to true.  Used by the debounced persistence logic inside the
    /// AnimationTick handler.
    pub last_modified_ms: u64,
    // Currently selected node ID
    pub selected_node_id: Option<String>,
    // Track node ID on mousedown to detect clicks on mouseup
    pub clicked_node_id: Option<String>,
    // Connection creation mode
    pub connection_mode: bool,
    pub connection_source_node: Option<String>,
    // Connection handle dragging
    pub connection_drag_active: bool,
    pub connection_drag_start: Option<(String, String)>, // (node_id, handle_position)
    pub connection_drag_current: Option<(f64, f64)>,     // Current mouse position
    // Mouse tracking for hover effects
    pub mouse_x: f64,
    pub mouse_y: f64,
    pub hovered_handle: Option<(String, String)>, // (node_id, handle_position)
    // Track the active view (Dashboard, Canvas, or ChatView)
    pub active_view: ActiveView,

    /// Current filter applied to the dashboard (persisted in localStorage).
    pub dashboard_scope: DashboardScope,

    /// Current sort settings for the dashboard table
    pub dashboard_sort: DashboardSort,
    // Pending network call data to avoid nested borrows
    pub pending_network_call: Option<(String, String)>,
    // Loading state flags
    pub is_loading: bool,
    pub data_loaded: bool,
    pub api_load_attempted: bool,
    /// Monotonic sequence to discard stale FetchCurrentWorkflow responses
    pub workflow_fetch_seq: u64,
    /// Monotonic counter to generate unique node identifiers
    pub next_node_seq: u64,

    // Workflow operation loading states
    pub creating_workflow: bool,
    pub deleting_workflow: Option<u32>, // workflow_id being deleted
    pub updating_workflow: Option<u32>, // workflow_id being updated
    // Agent-Centric State Management
    /// Map of agent_id -> AgentState for clean data isolation
    pub agent_states: HashMap<u32, AgentState>,
    /// Currently active agent for chat interface
    pub current_agent_id: Option<u32>,
    /// Loading state for chat interface
    pub is_chat_loading: bool,

    // New field for handling streaming responses
    /// Tracks the **current assistant message** id for every thread that is
    /// actively streaming.  `None` means we have not yet received the
    /// `assistant_id` frame (token-stream mode) or the first
    /// `assistant_message` chunk (non token mode).  Once the id is known we
    /// replace the entry with `Some(id)` so tool_output bubbles can link to
    /// their parent.
    pub active_streams: HashMap<u32, Option<u32>>,
    // --- WebSocket v2 and Topic Manager ---
    pub ws_client: Rc<RefCell<WsClientV2>>,
    pub topic_manager: Rc<RefCell<TopicManager>>,
    pub streaming_threads: HashSet<u32>,

    /// Threads for which the server is sending *token level* chunks.  This is
    /// detected lazily when the first `assistant_token` chunk is observed so
    /// we can adapt placeholder/bubble logic accordingly.
    pub token_mode_threads: HashSet<u32>,

    /// Currently selected tab inside the *Agent Configuration* modal.  Kept
    /// here so business logic can switch tabs without directly touching the
    /// DOM (single-source-of-truth).  Defaults to `Main` when the modal is
    /// first opened.
    pub agent_modal_tab: AgentConfigTab,

    // Track which agent rows are expanded in the dashboard UI so we can
    // preserve open/closed state across re‑renders.
    pub expanded_agent_rows: HashSet<u32>,

    // Map `agent_id -> recent runs (ordered newest-first, max 20)`
    pub agent_runs: HashMap<u32, Vec<ApiAgentRun>>,

    // ---------------------------------------------------------------
    // Trigger management (NEW – Phase A)
    // ---------------------------------------------------------------
    /// All triggers grouped by their owning agent.  Populated lazily when a
    /// user opens the *Triggers* tab in the agent modal.
    pub triggers: HashMap<u32, Vec<Trigger>>, // agent_id → triggers

    // Agent Debug modal (None when hidden)
    pub agent_debug_pane: Option<AgentDebugPane>,

    // -------------------------------------------------------------------
    // Runtime configuration flags fetched from `/api/system/info`
    // -------------------------------------------------------------------
    /// Google OAuth client ID returned by the backend.  *None* until the
    /// initial system-info request completes.  Stored so that subsequent
    /// calls (e.g. after a manual logout) can recreate the login overlay
    /// without requiring another network round-trip.
    pub google_client_id: Option<String>,

    // Track which agents have their full run history expanded (>5 rows)
    pub run_history_expanded: HashSet<u32>,

    // -------------------------------------------------------------------
    // Gmail integration status (Phase C)
    // -------------------------------------------------------------------
    /// True once the user connected Gmail via OAuth.  Controls whether the
    /// “Email (Gmail)” option in the *Add Trigger* wizard is clickable.
    pub gmail_connected: bool,
    /// Connector id for the active Gmail connection (if known).
    pub gmail_connector_id: Option<u32>,

    // Debug overlay moved to separate module to avoid circular dependencies

    /// Runs currently in `running` status for which the dashboard should show
    /// a ticking duration value.  Stores *run_id* (primary key) – agent_id can
    /// be looked up via `agent_runs` map if required.
    pub running_runs: HashSet<u32>,

    // -------------------------------------------------------------------
    // NEW: Explicit mapping from backend `agent_id` → canvas `node_id`.
    // -------------------------------------------------------------------
    /// Once an AgentIdentity node is placed on the canvas we store a *single*
    /// mapping entry so the UI can instantly look up the visual node given an
    /// `agent_id` without resorting to brittle string parsing or HashMap
    /// scans.  The invariant we uphold is **at most one** AgentIdentity node
    /// per agent per-workflow.  When a node is deleted (feature TBD) the
    /// entry must be removed as well.
    pub agent_id_to_node_id: HashMap<u32, String>,

    // -------------------------------------------------------------------
    // Rendering: marks whether a repaint is required on the next
    // requestAnimationFrame.  Keep this separate from `state_modified`
    // which is about *persistence*, not visual output.
    pub dirty: bool,

    // -------------------------------------------------------------------
    // Authentication
    // -------------------------------------------------------------------
    /// Whether the current browser session is authenticated.  Determined at
    /// startup from `localStorage["zerg_jwt"]` and updated once the Google
    /// login flow succeeds.
    pub logged_in: bool,

    /// Authenticated user profile loaded from `/api/users/me`.  `None` until
    /// the profile fetch succeeds (or the session is unauthenticated).
    pub current_user: Option<crate::models::CurrentUser>,

    /// Admin status loaded when user profile is fetched
    pub is_super_admin: bool,
    pub admin_requires_password: bool,

    /// Track whether agents have been loaded from the API at least once
    pub agents_loaded: bool,

    // -------------------------------------------------------------------
    // MCP Integration State
    // -------------------------------------------------------------------
    /// MCP server configurations per agent
    pub agent_mcp_configs: HashMap<u32, AgentMcpConfig>,

    /// Available MCP tools per agent (populated when tools are fetched)
    pub available_mcp_tools: HashMap<u32, Vec<McpToolInfo>>,

    /// Connection status for each MCP server (key: "agent_id:server_name")
    pub mcp_connection_status: HashMap<String, ConnectionStatus>,

    // -------------------------------------------------------------------
    // Template Gallery State
    // -------------------------------------------------------------------
    /// Available workflow templates
    pub templates: Vec<crate::models::WorkflowTemplate>,

    /// Template categories
    pub template_categories: Vec<String>,

    /// Currently selected template category filter
    pub selected_template_category: Option<String>,

    /// Whether to show only user's own templates
    pub show_my_templates_only: bool,

    /// Template gallery loading state
    pub templates_loading: bool,

    /// Whether the template gallery modal is currently shown
    pub show_template_gallery: bool,

    // -------------------------------------------------------------------
    // Ops Dashboard / HUD state
    // -------------------------------------------------------------------
    /// Latest ops summary (polled or after page load)
    pub ops_summary: Option<OpsSummary>,
    /// Rolling ticker buffer (newest first), capped at N=200
    pub ops_ticker: Vec<OpsTick>,
    /// Whether we've subscribed to `ops:events` on this session
    pub ops_ws_subscribed: bool,
}

// ---------------------------------------------------------------------------
// Dashboard sort state – column + ascending flag
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DashboardSortKey {
    Name,
    Status,
    LastRun,
    NextRun,
    SuccessRate,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DashboardSort {
    pub key: DashboardSortKey,
    pub ascending: bool,
}

impl AppState {
    pub fn new() -> Self {
        // --- Initialize WsClientV2 and TopicManager ---
        // Create the WebSocket client first
        let ws_client_rc = Rc::new(RefCell::new(WsClientV2::new_default()));
        // Create the TopicManager, giving it a reference to the client
        let topic_manager_rc = Rc::new(RefCell::new(TopicManager::new(ws_client_rc.clone())));

        Self {
            agents: HashMap::new(),
            agents_on_canvas: HashSet::new(),
            workflow_nodes: HashMap::new(),
            ui_state: HashMap::new(),
            ui_edge_state: HashMap::new(),
            workflows: HashMap::new(),
            current_workflow_id: None,
            canvas: None,
            context: None,
            connection_animation_offset: 0.0,
            input_text: String::new(),
            dragging: None,
            drag_offset_x: 0.0,
            drag_offset_y: 0.0,
            canvas_dragging: false,
            drag_start_x: 0.0,
            drag_start_y: 0.0,
            drag_last_x: 0.0,
            drag_last_y: 0.0,
            websocket: None,
            canvas_width: 800.0,
            canvas_height: 600.0,
            viewport_x: 0.0,
            viewport_y: 0.0,
            zoom_level: 1.0,
            // Auto-fit disabled by default – users can still trigger a one-off
            // "Find everything" action via the toolbar button.
            auto_fit: false,
            latest_user_input_id: None,
            message_id_to_node_id: HashMap::new(),
            selected_model: String::new(),
            available_models: Vec::new(),
            default_model_id: String::new(),
            state_modified: false,

            last_modified_ms: 0,
            selected_node_id: None,
            clicked_node_id: None,
            connection_mode: false,
            connection_source_node: None,
            connection_drag_active: false,
            connection_drag_start: None,
            connection_drag_current: None,
            mouse_x: 0.0,
            mouse_y: 0.0,
            hovered_handle: None,
            active_view: ActiveView::ChatView,

            dashboard_scope: {
                // Read persisted dashboard scope from localStorage (if any)
                let window = web_sys::window();
                if let Some(w) = window {
                    if let Ok(Some(storage)) = w.local_storage() {
                        if let Ok(Some(scope_str)) = storage.get_item("dashboard_scope") {
                            DashboardScope::from_str(&scope_str)
                        } else {
                            DashboardScope::MyAgents
                        }
                    } else {
                        DashboardScope::MyAgents
                    }
                } else {
                    DashboardScope::MyAgents
                }
            },

            // Default sort: Name ascending
            dashboard_sort: {
                // Try restoring from localStorage
                let window = web_sys::window();
                if let Some(w) = window {
                    if let Ok(Some(storage)) = w.local_storage() {
                        if let Ok(Some(key)) = storage.get_item("dashboard_sort_key") {
                            let key_enum = match key.as_str() {
                                "status" => DashboardSortKey::Status,
                                "last_run" => DashboardSortKey::LastRun,
                                "next_run" => DashboardSortKey::NextRun,
                                "success" => DashboardSortKey::SuccessRate,
                                _ => DashboardSortKey::Name,
                            };
                            let asc = storage
                                .get_item("dashboard_sort_asc")
                                .ok()
                                .flatten()
                                .map(|v| v != "0")
                                .unwrap_or(true);
                            DashboardSort {
                                key: key_enum,
                                ascending: asc,
                            }
                        } else {
                            DashboardSort {
                                key: DashboardSortKey::Name,
                                ascending: true,
                            }
                        }
                    } else {
                        DashboardSort {
                            key: DashboardSortKey::Name,
                            ascending: true,
                        }
                    }
                } else {
                    DashboardSort {
                        key: DashboardSortKey::Name,
                        ascending: true,
                    }
                }
            },
            pending_network_call: None,
            is_loading: true,
            data_loaded: false,
            api_load_attempted: false,
            workflow_fetch_seq: 0,
            next_node_seq: 0,

            // Initialize workflow operation loading states
            creating_workflow: false,
            deleting_workflow: None,
            updating_workflow: None,

            // Agent-Centric State
            agent_states: HashMap::new(),
            current_agent_id: None,
            is_chat_loading: false,

            active_streams: HashMap::new(),
            ws_client: ws_client_rc,
            topic_manager: topic_manager_rc,
            streaming_threads: HashSet::new(),

            token_mode_threads: HashSet::new(),

            agent_modal_tab: AgentConfigTab::Main,

            expanded_agent_rows: HashSet::new(),

            agent_runs: HashMap::new(),

            // Trigger map starts empty – filled on demand.
            triggers: HashMap::new(),

            agent_debug_pane: None,

            run_history_expanded: HashSet::new(),

            // Gmail yet to be connected.
            gmail_connected: false,
            gmail_connector_id: None,

            running_runs: HashSet::new(),

            agent_id_to_node_id: HashMap::new(),

            // The very first frame must draw the freshly created canvas.
            dirty: true,

            // -------------------------------------------------------------------
            // Authentication state – look for a persisted JWT.
            // -------------------------------------------------------------------
            logged_in: {
                let window = web_sys::window();
                if let Some(w) = window {
                    if let Ok(Some(storage)) = w.local_storage() {
                        storage.get_item("zerg_jwt").ok().flatten().is_some()
                    } else {
                        false
                    }
                } else {
                    false
                }
            },

            // Initially no profile is loaded.  Will be set once the frontend
            // successfully calls `/api/users/me` after a login or page
            // refresh.
            current_user: None,

            // Admin status - initially false until checked
            is_super_admin: false,
            admin_requires_password: false,

            google_client_id: None,

            // Start with agents not loaded
            agents_loaded: false,

            // MCP Integration state
            agent_mcp_configs: HashMap::new(),
            available_mcp_tools: HashMap::new(),
            mcp_connection_status: HashMap::new(),
            power_mode: false,
            particle_system: None,

            // Template Gallery state
            templates: Vec::new(),
            template_categories: Vec::new(),
            selected_template_category: None,
            show_my_templates_only: false,
            templates_loading: false,
            show_template_gallery: false,

            // Ops defaults
            ops_summary: None,
            ops_ticker: Vec::new(),
            ops_ws_subscribed: false,

            current_execution: None,
            execution_logs: Vec::new(),
            exec_history_open: false,
            executions: Vec::new(),
            logs_open: false,
        }
    }

    // -------------------------------------------------------------------
    // Rendering helpers
    // -------------------------------------------------------------------

    /// Mark the canvas as needing a repaint on the next animation frame.
    pub fn mark_dirty(&mut self) {
        self.dirty = true;
    }

    /// Check if a point (x, y) is on a connection handle of the given node
    /// Returns Some(handle_position) if hit, None otherwise
    pub fn get_handle_at_point(&self, node_id: &str, x: f64, y: f64) -> Option<String> {
        if let Some(node) = self.workflow_nodes.get(node_id) {
            let handle_radius = 6.0;
            let handles = [
                (node.get_x() + node.get_width() / 2.0, node.get_y(), "input"), // Top = Input
                (
                    node.get_x() + node.get_width() / 2.0,
                    node.get_y() + node.get_height(),
                    "output",
                ), // Bottom = Output
            ];

            for (hx, hy, position) in handles.iter() {
                let dx = x - hx;
                let dy = y - hy;
                let distance = (dx * dx + dy * dy).sqrt();
                if distance <= handle_radius {
                    return Some(position.to_string());
                }
            }
        }
        None
    }

    /// Validate if a connection from source handle to target handle is allowed
    pub fn is_valid_connection(
        &self,
        from_handle: &str,
        to_handle: &str,
        from_node_id: &str,
        to_node_id: &str,
    ) -> bool {
        // Prevent self-connections
        if from_node_id == to_node_id {
            return false;
        }

        // Only allow output -> input connections
        match (from_handle, to_handle) {
            ("output", "input") => true,
            _ => false,
        }
    }

    /// Update mouse position and check for handle hover states
    pub fn update_mouse_position(&mut self, x: f64, y: f64) {
        self.mouse_x = x;
        self.mouse_y = y;

        // Check if we're hovering over any handle
        let mut found_hover = None;
        for (node_id, _) in &self.workflow_nodes {
            if let Some(handle_pos) = self.get_handle_at_point(node_id, x, y) {
                found_hover = Some((node_id.clone(), handle_pos));
                break;
            }
        }

        // Update hover state if it changed
        if self.hovered_handle != found_hover {
            self.hovered_handle = found_hover;
            self.mark_dirty(); // Trigger redraw for hover effect
        }
    }

    /// Return the assistant message id that is **currently being streamed**
    /// for the given `thread_id`, if known.
    pub fn current_assistant_id(&self, thread_id: u32) -> Option<u32> {
        // First check agent-scoped state
        if let Some(agent_state) = self.current_agent() {
            if let Some(assistant_id) = agent_state
                .active_streams
                .get(&thread_id)
                .and_then(|opt| *opt)
            {
                return Some(assistant_id);
            }
        }
        // Fallback to global active_streams for compatibility with existing WebSocket code
        self.active_streams.get(&thread_id).and_then(|opt| *opt)
    }

    // ---------------------------------------------------------------------------
    // NEW: Agent-Centric Accessor Methods
    // ---------------------------------------------------------------------------

    /// Get the currently active agent state
    pub fn current_agent(&self) -> Option<&AgentState> {
        self.current_agent_id
            .and_then(|id| self.agent_states.get(&id))
    }

    /// Get the currently active agent state (mutable)
    pub fn current_agent_mut(&mut self) -> Option<&mut AgentState> {
        self.current_agent_id
            .and_then(|id| self.agent_states.get_mut(&id))
    }

    /// Get agent state by id
    pub fn get_agent_state(&self, agent_id: u32) -> Option<&AgentState> {
        self.agent_states.get(&agent_id)
    }

    /// Get agent state by id (mutable)
    pub fn get_agent_state_mut(&mut self, agent_id: u32) -> Option<&mut AgentState> {
        self.agent_states.get_mut(&agent_id)
    }

    /// Get threads for the current agent
    pub fn current_agent_threads(&self) -> Vec<&ApiThread> {
        self.current_agent()
            .map(|agent| agent.get_threads_sorted())
            .unwrap_or_default()
    }

    /// Get the current thread for the current agent
    pub fn current_agent_current_thread(&self) -> Option<&ApiThread> {
        self.current_agent()
            .and_then(|agent| agent.current_thread())
    }

    /// Get messages for the current thread of the current agent
    pub fn current_agent_current_thread_messages(&self) -> Vec<&ApiThreadMessage> {
        self.current_agent()
            .map(|agent| agent.current_thread_messages())
            .unwrap_or_default()
    }

    /// Initialize or get agent state for a given agent
    pub fn ensure_agent_state(&mut self, agent_id: u32) -> &mut AgentState {
        if !self.agent_states.contains_key(&agent_id) {
            // Get agent metadata from existing agents map
            if let Some(agent_metadata) = self.agents.get(&agent_id).cloned() {
                let agent_state = AgentState::new(agent_metadata);
                self.agent_states.insert(agent_id, agent_state);
            } else {
                // Create minimal agent state if metadata not available
                let minimal_agent = ApiAgent {
                    id: Some(agent_id),
                    name: format!("Agent {}", agent_id),
                    status: Some("unknown".to_string()),
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
                let agent_state = AgentState::new(minimal_agent);
                self.agent_states.insert(agent_id, agent_state);
            }
        }
        self.agent_states.get_mut(&agent_id).unwrap()
    }

    /// Set the current agent and ensure its state is initialized
    pub fn set_current_agent(&mut self, agent_id: u32) {
        self.current_agent_id = Some(agent_id);
        self.ensure_agent_state(agent_id);
    }

    /// Clear current agent selection
    pub fn clear_current_agent(&mut self) {
        self.current_agent_id = None;
    }

    pub fn add_node(
        &mut self,
        text: String,
        x: f64,
        y: f64,
        node_type: WorkflowNodeType,
    ) -> String {
        // Generate a unique, monotonic node id (prefix avoids legacy "node_" collisions)
        self.next_node_seq = self.next_node_seq.wrapping_add(1);
        let id = format!("node-{}-{}", js_sys::Date::now() as u64, self.next_node_seq);
        debug_log!(
            "Creating node: id={}, type={:?}, text={}",
            id, node_type, text
        );

        // Determine color based on node type
        let color = match &node_type {
            NodeType::UserInput => "#3498db".to_string(), // Blue
            NodeType::ResponseOutput => "#9b59b6".to_string(), // Purple
            NodeType::AgentIdentity => "#2ecc71".to_string(), // Green
            NodeType::GenericNode => "#95a5a6".to_string(), // Gray
            NodeType::Tool { .. } => "#f59e0b".to_string(), // Orange
            NodeType::Trigger { .. } => "#10b981".to_string(), // Green
        };

        // Calculate approximate node size based on text content
        let chars_per_line = 25; // Approximate chars per line
        let lines = (text.len() as f64 / chars_per_line as f64).ceil() as usize;

        // Set minimum sizes but allow for growth
        let width = f64::max(200.0, chars_per_line as f64 * 8.0); // Estimate width based on chars
        let height = f64::max(80.0, lines as f64 * 20.0 + 40.0); // Base height + lines

        let mut node = WorkflowNode::new_with_type(id.clone(), &node_type);
        node.set_x(x);
        node.set_y(y);
        node.set_width(width);
        node.set_height(height);
        node.set_color(color);
        node.set_text(text);
        node.set_parent_id(None);

        debug_log!(
            "Node created with dimensions: {}x{} at position ({}, {})",
            width, height, x, y
        );

        self.workflow_nodes.insert(id.clone(), node.clone());
        self.ui_state.insert(id.clone(), UiNodeState::default());
        debug_log!(
            "DEBUG: Added node {} to nodes map (type: {:?})",
            id, node.node_type
        );

        // Add node to current workflow structure
        self.add_node_to_current_workflow(node);

        self.state_modified = true; // Mark state as modified

        // If this is a user input node, update the latest_user_input_id
        if matches!(node_type, NodeType::UserInput) {
            self.latest_user_input_id = Some(id.clone());
        }

        // Auto-fit all nodes if enabled
        if self.auto_fit && self.workflow_nodes.len() > 1 {
            debug_log!("Auto-fitting nodes to view");
            self.fit_nodes_to_view();
        }

        debug_log!("Successfully added node {}", id);
        id
    }

    // -------------------------------------------------------------------
    // NODE CREATION HELPERS – AGENT-AWARE
    // -------------------------------------------------------------------
    /// Add a `NodeType::AgentIdentity` node that visualises the given
    /// `agent_id`.  The function ensures the `agent_id_to_node_id` mapping is
    /// updated so other parts of the frontend can perform O(1) look-ups.
    pub fn add_agent_node(&mut self, agent_id: u32, text: String, x: f64, y: f64) -> String {
        // Create the node manually to avoid double workflow updates
        self.next_node_seq = self.next_node_seq.wrapping_add(1);
        let node_id = format!("node-{}-{}", js_sys::Date::now() as u64, self.next_node_seq);
        debug_log!(
            "Creating agent node: id={}, agent_id={}, text={}",
            node_id, agent_id, text
        );

        let mut node = WorkflowNode::new_with_type(node_id.clone(), &NodeType::AgentIdentity);
        node.set_x(x);
        node.set_y(y);
        node.set_width(200.0);
        node.set_height(80.0);
        node.set_agent_id(Some(agent_id));
        node.set_color("#2ecc71".to_string());
        node.set_text(text);
        node.set_parent_id(None);

        self.workflow_nodes.insert(node_id.clone(), node.clone());
        self.ui_state
            .insert(node_id.clone(), UiNodeState::default());
        debug_log!(
            "DEBUG: Added agent node {} to nodes map (agent_id: {:?}, type: {:?})",
            node_id, agent_id, node.node_type
        );

        // Add node to current workflow structure with correct agent_id
        self.add_node_to_current_workflow(node);

        self.agent_id_to_node_id.insert(agent_id, node_id.clone());
        self.state_modified = true;

        node_id
    }

    /// Remove a node from the canvas and clean up any reverse mapping if the
    /// node belonged to an agent.
    #[allow(dead_code)]
    pub fn remove_node(&mut self, node_id: &str) {
        if let Some(node) = self.workflow_nodes.remove(node_id) {
            self.ui_state.remove(node_id);
            if let Some(agent_id) = node.get_agent_id() {
                self.agent_id_to_node_id.remove(&agent_id);
                self.agents_on_canvas.remove(&agent_id);
            }
            self.state_modified = true;
            self.mark_dirty();
        }
    }

    pub fn add_response_node(&mut self, parent_id: &str, response_text: String) -> String {
        let response_id = format!("resp-{}", self.generate_message_id());
        let parent = self.workflow_nodes.get(parent_id);

        // Default position for response node is below parent
        let (mut x, mut y) = (100.0, 100.0);

        if let Some(parent_node) = parent {
            x = parent_node.get_x();
            y = parent_node.get_y() + parent_node.get_height() + 30.0;
        }

        let mut node = WorkflowNode::new_with_type(response_id.clone(), &NodeType::ResponseOutput);
        node.set_x(x);
        node.set_y(y);
        node.set_width(300.0);
        node.set_height(100.0);
        node.set_color("#d5f5e3".to_string());
        node.set_text(response_text.clone());
        node.set_parent_id(Some(parent_id.to_string()));

        self.workflow_nodes.insert(response_id.clone(), node);
        self.ui_state
            .insert(response_id.clone(), UiNodeState::default());
        self.state_modified = true;

        // If the parent is an agent node, add this message to its history
        if let Some(parent_node) = self.workflow_nodes.get_mut(parent_id) {
            if matches!(
                parent_node.get_semantic_type(),
                crate::models::NodeType::AgentIdentity
            ) {
                let message = crate::models::Message {
                    role: "assistant".to_string(),
                    content: response_text,
                    timestamp: js_sys::Date::now() as u64,
                };

                // Instead of directly accessing history, use agent_id to add the message
                if let Some(agent_id) = parent_node.get_agent_id() {
                    crate::state::APP_STATE.with(|state| {
                        let mut state = state.borrow_mut();
                        if let Some(_agent) = state.agents.get_mut(&agent_id) {
                            // Store the message with the agent (actual implementation would depend on your API structure)
                        }
                    });
                }

                // Still save the message to API
                crate::storage::save_agent_messages_to_api(parent_id, &[message]);
            }
        }

        response_id
    }

    pub fn draw_nodes(&self) {
        if let (Some(context), Some(canvas)) = (&self.context, &self.canvas) {
            // Clear the canvas with our themed background color
            context.set_fill_style_str(crate::constants::CANVAS_BACKGROUND_COLOR);
            context.fill_rect(0.0, 0.0, canvas.width() as f64, canvas.height() as f64);

            // No viewport transformations needed since viewport is fixed at (0,0) and zoom is 1.0

            // Draw canvas nodes (new structure)
            for (_, node) in &self.workflow_nodes {
                let is_reachable = self.is_node_reachable_from_trigger(&node.node_id);
                let default_ui_state = crate::models::UiNodeState::default();
                let ui_state = self
                    .ui_state
                    .get(&node.node_id)
                    .unwrap_or(&default_ui_state);
                renderer::draw_node(
                    &context,
                    node,
                    &self.agents,
                    &self.selected_node_id,
                    &self.connection_source_node,
                    self.connection_mode,
                    &self.hovered_handle,
                    is_reachable,
                    ui_state,
                );
            }
        }
    }

    pub fn update_node_position(&mut self, node_id: &str, x: f64, y: f64) {
        // Track if any updates were made
        let mut updated = false;

        // First, try to update in nodes (new structure)
        if let Some(node) = self.workflow_nodes.get_mut(node_id) {
            node.set_x(x);
            node.set_y(y);
            updated = true;
        }

        // Only proceed if an update was made
        if updated {
            self.state_modified = true; // Mark state as modified

            // Auto-fit all nodes if enabled
            if self.auto_fit {
                self.fit_nodes_to_view();
            } else {
                self.mark_dirty();
            }
        }
    }

    pub fn find_node_at_position(&self, x: f64, y: f64) -> Option<(String, f64, f64)> {
        // No viewport transformation needed since viewport is fixed at (0,0) and zoom is 1.0

        // Check in nodes
        for (id, node) in &self.workflow_nodes {
            if x >= node.get_x()
                && x <= node.get_x() + node.get_width()
                && y >= node.get_y()
                && y <= node.get_y() + node.get_height()
            {
                return Some((id.clone(), x - node.get_x(), y - node.get_y()));
            }
        }

        None
    }

    // Apply transform to ensure all nodes are visible
    pub fn fit_nodes_to_view(&mut self) {
        // If there are no nodes at all, nothing to fit
        if self.workflow_nodes.is_empty() {
            return;
        }

        // Find bounding box of all nodes
        let mut min_x = f64::MAX;
        let mut min_y = f64::MAX;
        let mut max_x = f64::MIN;
        let mut max_y = f64::MIN;

        // Check nodes (new structure)
        for (_, node) in &self.workflow_nodes {
            min_x = f64::min(min_x, node.get_x());
            min_y = f64::min(min_y, node.get_y());
            max_x = f64::max(max_x, node.get_x() + node.get_width());
            max_y = f64::max(max_y, node.get_y() + node.get_height());
        }

        // Calculate the bounding box dimensions
        let box_width = max_x - min_x;
        let box_height = max_y - min_y;

        // Ensure canvas exists and has valid dimensions
        let (canvas_width, canvas_height) = match &self.canvas {
            Some(canvas) => {
                let dpr = web_sys::window().unwrap().device_pixel_ratio();
                let w = canvas.width() as f64 / dpr;
                let h = canvas.height() as f64 / dpr;
                if w < 1.0 || h < 1.0 {
                    web_sys::console::error_1(&"fit_nodes_to_view: canvas not yet sized".into());
                    return;
                }
                (w, h)
            }
            None => {
                web_sys::console::error_1(&"fit_nodes_to_view: no canvas available".into());
                return;
            }
        };

        // Calculate zoom level to fit all nodes with padding
        let padding = 50.0; // Padding around the bounding box
        let _zoom_x = canvas_width / (box_width + padding * 2.0);
        let _zoom_y = canvas_height / (box_height + padding * 2.0);

        // Take the smaller of the two to ensure all nodes fit
        if let Some(canvas) = &self.canvas {
            // Find bounding box of all nodes
            let mut min_x = f64::MAX;
            let mut min_y = f64::MAX;
            let mut max_x = f64::MIN;
            let mut max_y = f64::MIN;

            for (_, node) in &self.workflow_nodes {
                min_x = f64::min(min_x, node.get_x());
                min_y = f64::min(min_y, node.get_y());
                max_x = f64::max(max_x, node.get_x() + node.get_width());
                max_y = f64::max(max_y, node.get_y() + node.get_height());
            }

            // Get canvas dimensions
            let canvas_width = canvas.width() as f64;
            let canvas_height = canvas.height() as f64;

            // Get the device pixel ratio to adjust calculations
            let window = web_sys::window().expect("no global window exists");
            let dpr = window.device_pixel_ratio();

            // Adjust canvas dimensions by DPR
            let canvas_width = canvas_width / dpr;
            let canvas_height = canvas_height / dpr;

            // Calculate required width and height with padding
            let padding = 80.0;
            let required_width = max_x - min_x + padding;
            let required_height = max_y - min_y + padding;

            // Set minimum view area to prevent excessive zooming on small node counts
            // This ensures we don't zoom in too far when there are only a few nodes
            let min_view_width = 800.0; // Minimum width to display
            let min_view_height = 600.0; // Minimum height to display

            // Use the larger of required size or minimum size
            let effective_width = f64::max(required_width, min_view_width);
            let effective_height = f64::max(required_height, min_view_height);

            // Calculate zoom level needed
            let width_ratio = canvas_width / effective_width;
            let height_ratio = canvas_height / effective_height;

            // Use the smaller ratio to ensure everything fits, but clamp to a
            // sensible range so we don't zoom to nearly-zero which would make
            // subsequent coordinate conversions explode.
            let mut new_zoom = f64::min(width_ratio, height_ratio);
            if new_zoom > MAX_ZOOM {
                new_zoom = MAX_ZOOM;
            } else if new_zoom < MIN_ZOOM {
                new_zoom = MIN_ZOOM;
            }

            // Calculate the center of the nodes
            let center_x = min_x + (max_x - min_x) / 2.0;
            let center_y = min_y + (max_y - min_y) / 2.0;

            // Calculate viewport position to center the content
            let new_viewport_x = center_x - (canvas_width / (2.0 * new_zoom));
            let new_viewport_y = center_y - (canvas_height / (2.0 * new_zoom));

            // Update state
            self.zoom_level = new_zoom;
            self.clamp_zoom();
            self.viewport_x = new_viewport_x;
            self.viewport_y = new_viewport_y;
            self.clamp_viewport();

            // Schedule a redraw via RAF
            self.mark_dirty();
        }
    }

    // Toggle auto-fit functionality
    pub fn toggle_auto_fit(&mut self) {
        self.auto_fit = !self.auto_fit;
        if self.auto_fit {
            self.fit_nodes_to_view();
        }
    }

    // Center the viewport on all nodes without changing auto-fit setting
    pub fn center_view(&mut self) {
        // Calculate bounding box of all nodes first. If there are none, bail.
        if self.workflow_nodes.is_empty() {
            return;
        }

        let mut min_x = f64::MAX;
        let mut min_y = f64::MAX;
        let mut max_x = f64::MIN;
        let mut max_y = f64::MIN;

        for (_, node) in &self.workflow_nodes {
            min_x = f64::min(min_x, node.get_x());
            min_y = f64::min(min_y, node.get_y());
            max_x = f64::max(max_x, node.get_x() + node.get_width());
            max_y = f64::max(max_y, node.get_y() + node.get_height());
        }

        // Canvas dimensions must be ready; otherwise bail loudly.
        let (canvas_w, canvas_h) = match &self.canvas {
            Some(canvas) => {
                let dpr = web_sys::window().unwrap().device_pixel_ratio();
                let w = canvas.width() as f64 / dpr;
                let h = canvas.height() as f64 / dpr;
                if w < 1.0 || h < 1.0 {
                    web_sys::console::error_1(&"center_view: canvas not yet sized".into());
                    return;
                }
                (w, h)
            }
            None => {
                web_sys::console::error_1(&"center_view: no canvas available".into());
                return;
            }
        };

        let padding = 80.0;
        let required_w = (max_x - min_x) + padding;
        let required_h = (max_y - min_y) + padding;

        let width_ratio = canvas_w / required_w;
        let height_ratio = canvas_h / required_h;
        let mut target_zoom = f64::min(width_ratio, height_ratio);

        // Clamp zoom to a sensible range so that extremely small bounding
        // boxes (or an un-initialised <canvas>) cannot drive the zoom towards
        // zero which then explodes world-space coordinates on the next drag.
        if target_zoom > MAX_ZOOM {
            target_zoom = MAX_ZOOM;
        } else if target_zoom < MIN_ZOOM {
            target_zoom = MIN_ZOOM;
        }

        // Desired centre point of nodes
        let centre_x = min_x + (max_x - min_x) / 2.0;
        let centre_y = min_y + (max_y - min_y) / 2.0;

        let target_viewport_x = centre_x - (canvas_w / (2.0 * target_zoom));
        let target_viewport_y = centre_y - (canvas_h / (2.0 * target_zoom));

        // ------------------------------------------------------------------
        // Apply viewport change immediately.
        // ------------------------------------------------------------------
        // The earlier animated version left `zoom_level` unchanged until the
        // next RAF callback, which meant any user interaction that happened
        // in that single frame (e.g. dropping a new node) still used the old
        // (possibly tiny) zoom value and generated astronomically large
        // coordinates.  We switch to an immediate update – the user barely
        // notices the 250 ms animation anyway, but the correctness win is
        // massive.

        self.zoom_level = target_zoom;
        self.clamp_zoom();
        self.viewport_x = target_viewport_x;
        self.viewport_y = target_viewport_y;

        self.mark_dirty();
    }

    /// Reset viewport to origin & 100% zoom with animation
    pub fn reset_view(&mut self) {
        // Immediate reset – avoid stale zoom during animation window.
        self.zoom_level = 1.0;
        self.clamp_zoom();
        self.viewport_x = 0.0;
        self.viewport_y = 0.0;
        self.mark_dirty();
    }

    #[allow(dead_code)]
    fn animate_viewport(
        &mut self,
        start_x: f64,
        start_y: f64,
        start_zoom: f64,
        target_x: f64,
        target_y: f64,
        target_zoom: f64,
    ) {
        let window = web_sys::window().expect("no global window exists");

        // Animation parameters
        let duration = 250.0; // Animation duration in ms (fast but visible)
        let start_time = js_sys::Date::now();

        // Define the type for our self-referential closure
        type AnimationClosure = Closure<dyn FnMut(f64)>;

        // Need to use this approach for self-referential closure
        let f = Rc::new(RefCell::new(None::<AnimationClosure>));
        let g = f.clone();

        // Clone window for use outside the closure
        let window_for_start = window.clone();

        // Create a function to perform the animation
        *g.borrow_mut() = Some(Closure::new(Box::new(move |time: f64| {
            // Clone window reference for use in the closure
            let window_ref = web_sys::window().expect("no global window exists");

            APP_STATE.with(|state| {
                let mut state = state.borrow_mut();
                let elapsed = time - start_time;
                let progress = (elapsed / duration).min(1.0);

                // Ease function (smooth start and end)
                let ease = |t: f64| -> f64 {
                    if t < 0.5 {
                        4.0 * t * t * t
                    } else {
                        1.0 - (-2.0 * t + 2.0).powi(3) / 2.0
                    }
                };

                let eased_progress = ease(progress);

                // Interpolate viewport position and zoom
                state.viewport_x = start_x + (target_x - start_x) * eased_progress;
                state.viewport_y = start_y + (target_y - start_y) * eased_progress;
                state.zoom_level = start_zoom + (target_zoom - start_zoom) * eased_progress;

                // Flag a repaint for this frame
                state.mark_dirty();

                // Continue animation if not finished
                if progress < 1.0 {
                    let _ = window_ref.request_animation_frame(
                        f.borrow().as_ref().unwrap().as_ref().unchecked_ref(),
                    );
                }
            });
        })));

        // Start the animation
        let _ = window_for_start
            .request_animation_frame(g.borrow().as_ref().unwrap().as_ref().unchecked_ref());
    }

    // Generate a unique message ID
    pub fn generate_message_id(&self) -> String {
        format!("msg_{}", Date::now())
    }

    // Track a message ID and its corresponding node ID
    pub fn track_message(&mut self, message_id: String, node_id: String) {
        self.message_id_to_node_id.insert(message_id, node_id);
    }

    // Function to get node ID for a message ID
    pub fn _get_node_id_for_message(&self, message_id: &str) -> Option<String> {
        self.message_id_to_node_id.get(message_id).cloned()
    }

    // Enforce boundaries for viewport to prevent going out of range
    pub fn _enforce_viewport_boundaries(&mut self) {
        if self.workflow_nodes.is_empty() {
            return;
        }

        // Find bounding box of all nodes
        let mut min_x = f64::MAX;
        let mut min_y = f64::MAX;
        let mut max_x = f64::MIN;
        let mut max_y = f64::MIN;

        for (_, node) in &self.workflow_nodes {
            min_x = f64::min(min_x, node.get_x());
            min_y = f64::min(min_y, node.get_y());
            max_x = f64::max(max_x, node.get_x() + node.get_width());
            max_y = f64::max(max_y, node.get_y() + node.get_height());
        }

        // Calculate content dimensions
        let content_width = max_x - min_x;
        let content_height = max_y - min_y;

        // Get canvas dimensions if available
        let (canvas_width, canvas_height) = if let Some(canvas) = &self.canvas {
            let window = web_sys::window().expect("no global window exists");
            let dpr = window.device_pixel_ratio();
            let canvas_width = canvas.width() as f64 / dpr;
            let canvas_height = canvas.height() as f64 / dpr;
            (canvas_width, canvas_height)
        } else {
            (800.0, 600.0) // Fallback values if canvas not available
        };

        // Calculate the viewport's visible width and height in world coordinates
        let viewport_width = canvas_width / self.zoom_level;
        let viewport_height = canvas_height / self.zoom_level;

        // Calculate expanded content bounds with generous padding
        // Allow the center of content to be positioned anywhere in the viewport
        let padding = f64::max(content_width, content_height); // Use content size as padding
        let expanded_min_x = min_x - padding;
        let expanded_min_y = min_y - padding;
        let expanded_max_x = max_x + padding;
        let expanded_max_y = max_y + padding;

        // Limit viewport to expanded bounds
        // This ensures nodes can be centered in the viewport and won't disappear
        self.viewport_x = self.viewport_x.clamp(
            expanded_min_x - viewport_width / 2.0,
            expanded_max_x - viewport_width / 2.0,
        );

        self.viewport_y = self.viewport_y.clamp(
            expanded_min_y - viewport_height / 2.0,
            expanded_max_y - viewport_height / 2.0,
        );
    }

    // Save state if modified
    pub fn save_if_modified(&mut self) -> Result<(), JsValue> {
        if self.state_modified {
            // Persist to localStorage and schedule the *single* API PATCH
            // call via ``save_state``.  We removed the second direct call to
            // `save_state_to_api` to avoid duplicate network requests for the
            // same state update.
            let result = crate::storage::save_state(self);

            // Sync agent messages – removed as part of node/agent decoupling

            self.state_modified = false;
            result
        } else {
            Ok(())
        }
    }

    // Separate method to refresh UI after state changes
    pub fn refresh_ui_after_state_change() -> Result<(), JsValue> {
        // Refresh both canvas and dashboard views to ensure all UI elements are in sync
        let window = web_sys::window().ok_or(JsValue::from_str("No window"))?;
        let document = window.document().ok_or(JsValue::from_str("No document"))?;

        // Get the active view once to avoid multiple borrows
        let active_view = APP_STATE.with(|state| {
            let state = state.borrow();
            state.active_view.clone() // Clone to avoid borrowing issues
        });

        debug_log!("Refreshing UI for active view: {:?}", active_view);

        // First render the active view to ensure proper display of containers
        crate::views::render_active_view_by_type(&active_view, &document)?;

        // Only refresh components relevant to the current view
        match active_view {
            crate::storage::ActiveView::Dashboard => {
                // For Dashboard view, only refresh the dashboard component
                debug_log!("Refreshing dashboard components");
                crate::components::dashboard::refresh_dashboard(&document)?;
            }
            crate::storage::ActiveView::Canvas => {
                // For Canvas view, refresh the canvas and agent shelf
                debug_log!("Refreshing canvas components");

                // Check if we need to refresh canvas in a separate borrow scope
                let has_canvas = APP_STATE.with(|state| {
                    let state = state.borrow();
                    state.canvas.is_some() && state.context.is_some()
                });

                if has_canvas {
                    // Refresh canvas in a separate borrow scope
                    APP_STATE.with(|state| {
                        let mut state = state.borrow_mut();
                        state.mark_dirty();
                    });
                }

                // Only refresh the agent shelf for Canvas view
                let _ = crate::components::agent_shelf::refresh_agent_shelf(&document);
            }
            crate::storage::ActiveView::ChatView => {
                // For Chat view, refresh chat components
                debug_log!("Refreshing chat components");
                // Chat view refreshes are handled by its own code
            }
            crate::storage::ActiveView::Profile => {
                // Profile page currently doesn't need dynamic refresh logic
                debug_log!("Refreshing profile components");
            }
            crate::storage::ActiveView::AdminOps => {
                debug_log!("Refreshing Ops dashboard components");
                crate::components::ops::render_ops_dashboard(&document)?;
            }
        }

        Ok(())
    }

    // -------------------------------------------------------------------
    // SANITY HELPERS – must be called after *any* change to zoom/viewport.
    // -------------------------------------------------------------------

    /// Clamp `zoom_level` to the hard limits.
    pub fn clamp_zoom(&mut self) {
        self.zoom_level = self.zoom_level.clamp(MIN_ZOOM, MAX_ZOOM);
    }

    /// Clamp `viewport_x` / `viewport_y` so that the centre of the viewport
    /// cannot move farther than half a canvas from the origin.  This keeps
    /// panning within ±50 % of the default view.
    pub fn clamp_viewport(&mut self) {
        // Determine canvas dimensions (world units depend on zoom).
        let (canvas_w, canvas_h) = if let Some(canvas) = &self.canvas {
            let window = web_sys::window().unwrap();
            let dpr = window.device_pixel_ratio();
            (canvas.width() as f64 / dpr, canvas.height() as f64 / dpr)
        } else {
            (self.canvas_width.max(1.0), self.canvas_height.max(1.0))
        };

        // Half of the viewport size in world units.
        let half_w = (canvas_w / self.zoom_level) * 0.5;
        let half_h = (canvas_h / self.zoom_level) * 0.5;

        self.viewport_x = self.viewport_x.clamp(-half_w, half_w);
        self.viewport_y = self.viewport_y.clamp(-half_h, half_h);
    }

    pub fn resize_node_for_content(&mut self, node_id: &str) {
        if let Some(node) = self.workflow_nodes.get_mut(node_id) {
            // Calculate approximate node size based on text content
            let chars_per_line = 25; // Approximate chars per line
            let lines = (node.get_text().len() as f64 / chars_per_line as f64).ceil() as usize;

            // Set minimum sizes but allow for growth
            node.set_width(f64::max(200.0, chars_per_line as f64 * 8.0)); // Estimate width based on chars
            node.set_height(f64::max(80.0, lines as f64 * 20.0 + 40.0)); // Base height + lines

            // Mark state as modified
            self.state_modified = true;
        }
    }

    /// Returns the saved task-instructions for the given node / agent.
    ///
    /// * `Ok(String)` – instructions found.
    /// * `Err(&str)`  – agent resolved but has no instructions, or could not
    ///                  resolve an agent for the provided `node_id`.
    pub fn get_task_instructions(&self, node_id: &str) -> Result<String, &'static str> {
        let agent_id_opt = self
            .workflow_nodes
            .get(node_id)
            .and_then(|n| n.get_agent_id());

        let aid = agent_id_opt.ok_or("Could not resolve agent_id from node_id")?;

        let agent = self.agents.get(&aid).ok_or("Agent not found in state")?;

        agent
            .task_instructions
            .clone()
            .ok_or("Agent has no task instructions set")
    }

    // New dispatch method to handle messages
    pub fn dispatch(&mut self, msg: Message) -> Vec<Command> {
        // Call the update function and return its commands
        update::update(self, msg)
    }

    // Update to set the selected node ID and load messages if it's an agent
    pub fn _select_node(&mut self, node_id: Option<String>) {
        // First unselect any currently selected node
        if let Some(current_id) = &self.selected_node_id {
            if let Some(ui_node_state) = self.ui_state.get_mut(current_id) {
                ui_node_state.is_selected = false;
            }
        }

        // Set the new selected node
        self.selected_node_id = node_id.clone();

        // Mark the new node as selected
        if let Some(id) = &node_id {
            if let Some(ui_node_state) = self.ui_state.get_mut(id) {
                ui_node_state.is_selected = true;
            }
        }

        // Flag for redraw
        self.mark_dirty();
    }

    /// Creates a new node linked to an optional agent
    pub fn add_node_with_agent(
        &mut self,
        agent_id: Option<u32>,
        x: f64,
        y: f64,
        node_type: WorkflowNodeType,
        text: String,
    ) -> String {
        // Generate a unique ID for the node
        if node_type == NodeType::AgentIdentity {
            // Delegate to the dedicated helper so we keep the mapping in sync.
            if let Some(aid) = agent_id {
                return self.add_agent_node(aid, text, x, y);
            }
        }

        // Fallback – generic node (or agent node without backend id yet).
        let node_id = format!("node-{}", js_sys::Date::now() as u32);

        let color = match node_type {
            NodeType::UserInput => "#3498db".to_string(), // Blue
            NodeType::ResponseOutput => "#9b59b6".to_string(), // Purple
            NodeType::AgentIdentity => DEFAULT_AGENT_NODE_COLOR.to_string(),
            NodeType::GenericNode => "#95a5a6".to_string(), // Gray
            NodeType::Tool { .. } => "#f59e0b".to_string(), // Orange
            NodeType::Trigger { .. } => "#10b981".to_string(), // Green
        };

        let mut node = WorkflowNode::new_with_type(node_id.clone(), &node_type);
        node.set_x(x);
        node.set_y(y);
        node.set_width(DEFAULT_NODE_WIDTH);
        node.set_height(DEFAULT_NODE_HEIGHT);
        node.set_color(color);
        node.set_text(text);
        if let Some(aid) = agent_id {
            node.set_agent_id(Some(aid));
        }
        node.set_parent_id(None);

        self.workflow_nodes.insert(node_id.clone(), node.clone());
        self.ui_state
            .insert(node_id.clone(), UiNodeState::default());

        // Add node to current workflow structure
        self.add_node_to_current_workflow(node);

        self.state_modified = true;

        node_id
    }

    /// Check if a node is connected to other nodes (has incoming or outgoing edges)
    pub fn is_node_connected(&self, node_id: &str) -> bool {
        if let Some(workflow_id) = self.current_workflow_id {
            if let Some(workflow) = self.workflows.get(&workflow_id) {
                // Check if the node has any incoming or outgoing edges
                return workflow
                    .get_edges()
                    .iter()
                    .any(|edge| edge.from_node_id == node_id || edge.to_node_id == node_id);
            }
        }
        false
    }

    /// Check if a node is reachable from a trigger node (part of execution path)
    pub fn is_node_reachable_from_trigger(&self, node_id: &str) -> bool {
        if let Some(workflow_id) = self.current_workflow_id {
            if let Some(workflow) = self.workflows.get(&workflow_id) {
                // Find all trigger nodes
                let nodes = workflow.get_nodes();
                let trigger_nodes: Vec<&str> = nodes
                    .iter()
                    .filter(|node| {
                        matches!(
                            node.get_semantic_type(),
                            crate::models::NodeType::Trigger { .. }
                        )
                    })
                    .map(|node| node.node_id.as_str())
                    .collect();

                // For now, simple check: is node connected to anything OR is a trigger itself
                let is_trigger = trigger_nodes.contains(&node_id);
                let is_connected = self.is_node_connected(node_id);

                return is_trigger || is_connected;
            }
        }
        false
    }

    /// Adds a node to the current workflow's structure (for backend sync)
    pub fn add_node_to_current_workflow(&mut self, node: WorkflowNode) {
        if let Some(workflow_id) = self.current_workflow_id {
            if let Some(workflow) = self.workflows.get_mut(&workflow_id) {
                // Add the node using the helper method
                let node_id_for_log = node.node_id.clone();
                workflow.add_node(node);

                debug_log!(
                    "📋 Added node {} to workflow (total: {} nodes, {} edges)",
                    node_id_for_log,
                    workflow.get_nodes().len(),
                    workflow.get_edges().len()
                );
                debug_log!(
                    "🔍 Workflow structure: nodes={:?}",
                    workflow
                        .get_nodes()
                        .iter()
                        .map(|n| &n.node_id)
                        .collect::<Vec<_>>()
                );
            } else {
                debug_log!(
                    "⚠️ Current workflow not found, creating default workflow for node"
                );
                // Create a default workflow if it doesn't exist
                let default_workflow = ApiWorkflow {
                    id: workflow_id,
                    owner_id: 0, // Default owner for now
                    name: "My Canvas Workflow".to_string(),
                    description: Some("Auto-created workflow".to_string()),
                    canvas: serde_json::json!({
                        "nodes": vec![node],
                        "edges": Vec::<WorkflowEdge>::new()
                    }),
                    is_active: true,
                    created_at: None,
                    updated_at: None,
                };
                self.workflows.insert(workflow_id, default_workflow);
            }
        } else {
            debug_log!("📋 No current workflow, creating new workflow for node");
            // Create a new workflow for this node
            let new_workflow_id = self.create_workflow("My Canvas Workflow".to_string());
            if let Some(workflow) = self.workflows.get_mut(&new_workflow_id) {
                workflow.add_node(node);
            }
        }
    }

    /// Creates a new workflow
    pub fn create_workflow(&mut self, name: String) -> u32 {
        // Generate a new workflow ID (simply use the current timestamp for now)
        let workflow_id = (Date::now() / 1000.0) as u32;

        // Create the new workflow
        let workflow = ApiWorkflow {
            id: workflow_id,
            owner_id: 0, // Default owner for now
            name,
            description: Some("Canvas workflow".to_string()),
            canvas: serde_json::json!({
                "nodes": Vec::<WorkflowNode>::new(),
                "edges": Vec::<WorkflowEdge>::new()
            }),
            is_active: true,
            created_at: None,
            updated_at: None,
        };

        // Add the workflow to our collection
        self.workflows.insert(workflow_id, workflow);

        // Set this as the current workflow
        self.current_workflow_id = Some(workflow_id);

        self.state_modified = true;

        // Return the new workflow's ID
        workflow_id
    }

    /// Creates an edge between two canvas nodes
    pub fn add_edge(
        &mut self,
        from_node_id: String,
        to_node_id: String,
        label: Option<String>,
    ) -> String {
        // Generate a unique ID for the edge
        let edge_id = format!("edge-{}", Date::now() as u32);

        // Create the new edge
        let edge = WorkflowEdge {
            from_node_id: from_node_id.clone(),
            to_node_id: to_node_id.clone(),
            config: {
                let mut config = serde_json::Map::new();
                if let Some(label) = label {
                    config.insert("label".to_string(), serde_json::Value::String(label));
                }
                config.insert("id".to_string(), serde_json::Value::String(edge_id.clone()));
                config
            },
        };

        debug_log!("🔗 Connecting {} → {}", from_node_id, to_node_id);

        // If we have a current workflow, add this edge to it
        if let Some(workflow_id) = self.current_workflow_id {
            if let Some(workflow) = self.workflows.get_mut(&workflow_id) {
                workflow.add_edge(edge);
                debug_log!(
                    "✅ Connection saved! ({} total)",
                    workflow.get_edges().len()
                );

                // TODO: Trigger immediate graph rebuild in backend
                // self.trigger_graph_rebuild();
            } else {
                debug_log!(
                    "📋 Auto-creating workflow for your canvas connections...",
                );
                // Create a default workflow if it doesn't exist
                let default_workflow = ApiWorkflow {
                    id: workflow_id,
                    owner_id: 0, // Default owner for now
                    name: "My Canvas Workflow".to_string(),
                    description: Some("Auto-created workflow".to_string()),
                    canvas: serde_json::json!({
                        "nodes": Vec::<WorkflowNode>::new(),
                        "edges": vec![edge]
                    }),
                    is_active: true,
                    created_at: None,
                    updated_at: None,
                };
                self.workflows.insert(workflow_id, default_workflow);
                debug_log!(
                    "✅ Created workflow '{}' - your connections will be saved here!",
                    "My Canvas Workflow"
                );

                // TODO: Trigger immediate graph rebuild in backend
                // self.trigger_graph_rebuild();
            }
        } else {
            debug_log!("📋 Creating your first canvas workflow...");
            // Create a new default workflow
            let new_workflow_id = self.create_workflow("My Canvas Workflow".to_string());
            if let Some(workflow) = self.workflows.get_mut(&new_workflow_id) {
                workflow.add_edge(edge);
                debug_log!(
                    "✅ Created '{}' - start building your workflow!",
                    "My Canvas Workflow"
                );
            }
        }

        self.state_modified = true;

        // Return the new edge's ID
        edge_id
    }

    /// Trigger immediate graph rebuild in backend by sending canvas data
    #[allow(dead_code)]
    fn trigger_graph_rebuild(&self) {
        debug_log!("🔄 Triggering graph rebuild in backend...");

        // TODO: Implement graph rebuild trigger
        // Need to add proper message type and canvas data structure
    }

    // sync_agents_on_canvas removed - no longer needed with single source of truth architecture
    // agents_on_canvas is now rebuilt directly from workflow data in CurrentWorkflowLoaded
}

// We use thread_local to store our app state
thread_local! {
    pub static APP_STATE: RefCell<AppState> = RefCell::new(AppState::new());
}

// Add a public function to update the app state with data from the API
#[allow(dead_code)]
pub fn update_app_state_from_api(nodes: HashMap<String, WorkflowNode>) -> Result<(), JsValue> {
    // Get access to the global APP_STATE
    APP_STATE.with(|app_state_ref| {
        let mut app_state = app_state_ref.borrow_mut();

        // Update the nodes with those loaded from the API
        for (node_id, node) in nodes {
            app_state.workflow_nodes.insert(node_id, node);
        }

        // Flag that the state has been modified
        app_state.state_modified = true;

        // Refresh the UI
        if let Some(_canvas) = &app_state.canvas {
            if let Some(_context) = &app_state.context {
                // Use the correct draw_nodes function instead of render_canvas
                crate::canvas::renderer::draw_nodes(&mut *app_state);
            }
        }
    });

    Ok(())
}

// Helper function to update node IDs after API creation
#[allow(dead_code)]
pub fn update_node_id(old_id: &str, new_id: &str) {
    APP_STATE.with(|state_ref| {
        let mut state = state_ref.borrow_mut();

        // If the node exists with the old ID
        if let Some(node) = state.workflow_nodes.remove(old_id) {
            // Insert it with the new ID
            let mut updated_node = node.clone();
            // Directly change the `node_id` field – no helper needed.
            updated_node.node_id = new_id.to_string();
            state
                .workflow_nodes
                .insert(new_id.to_string(), updated_node);

            debug_log!("Updated node ID from {} to {}", old_id, new_id);

            // Also update any relationships like parent IDs
            for (_, child_node) in state.workflow_nodes.iter_mut() {
                if let Some(parent_id) = child_node.get_parent_id() {
                    if parent_id == old_id {
                        child_node.set_parent_id(Some(new_id.to_string()));
                    }
                }
            }

            // Update selected node if necessary
            if let Some(selected_id) = &state.selected_node_id {
                if selected_id == old_id {
                    state.selected_node_id = Some(new_id.to_string());
                }
            }

            // Mark state as modified to ensure it gets saved
            state.state_modified = true;

            // Update the UI to reflect the changes
            if let Err(e) = AppState::refresh_ui_after_state_change() {
                web_sys::console::error_1(
                    &format!("Error refreshing UI after node ID update: {:?}", e).into(),
                );
            }
        }
    });
}

// Global helper function for dispatching messages with proper UI refresh handling
pub fn dispatch_global_message(msg: crate::messages::Message) {
    // 1. Perform state updates and collect commands
    let (commands, network_data) = APP_STATE.with(|state| {
        let mut state = state.borrow_mut();
        let commands = state.dispatch(msg);

        // Take ownership of pending network data
        // We'll gradually migrate these to commands
        let network = state.pending_network_call.take();

        (commands, network)
    });

    // 2. Execute commands after state borrow is dropped
    for cmd in commands {
        match cmd {
            Command::SendMessage(msg) => dispatch_global_message(msg),
            // UI update closures – comment out verbose log that fired on
            // every incremental render.
            Command::UpdateUI(ui_fn) => {
                ui_fn();
            }

            // Group commands by type and delegate to appropriate executor
            cmd @ Command::FetchThreads(_)
            | cmd @ Command::FetchThreadMessages(_)
            | cmd @ Command::LoadAgentInfo(_)
            | cmd @ Command::FetchAgents
            | cmd @ Command::FetchAgentRuns(_)
            | cmd @ Command::FetchAgentDetails(_) => {
                crate::command_executors::execute_fetch_command(cmd)
            }
            cmd @ Command::FetchWorkflows | cmd @ Command::FetchCurrentWorkflow => {
                crate::command_executors::execute_fetch_command(cmd)
            }
            cmd @ Command::FetchExecutionHistory { .. } => {
                crate::command_executors::execute_fetch_command(cmd)
            }
            cmd @ Command::CreateWorkflowApi { .. }
            | cmd @ Command::DeleteWorkflowApi { .. }
            | cmd @ Command::RenameWorkflowApi { .. }
            | cmd @ Command::StartWorkflowExecutionApi { .. }
            | cmd @ Command::ReserveWorkflowExecutionApi { .. }
            | cmd @ Command::StartReservedExecutionApi { .. }
            | cmd @ Command::ScheduleWorkflowApi { .. }
            | cmd @ Command::UnscheduleWorkflowApi { .. }
            | cmd @ Command::CheckWorkflowScheduleApi { .. } => {
                crate::command_executors::execute_fetch_command(cmd)
            }
            cmd @ Command::FetchTriggers(_)
            | cmd @ Command::CreateTrigger { .. }
            | cmd @ Command::DeleteTrigger(_) => {
                crate::command_executors::execute_fetch_command(cmd)
            }

            // Template Gallery Commands - API calls
            cmd @ Command::LoadTemplatesApi { .. }
            | cmd @ Command::LoadTemplateCategoriesApi
            | cmd @ Command::DeployTemplateApi { .. } => {
                crate::command_executors::execute_template_command(cmd)
            }

            cmd @ Command::CreateThread { .. }
            | cmd @ Command::SendThreadMessage { .. }
            | cmd @ Command::UpdateThreadTitle { .. }
            | cmd @ Command::RunThread(_) => crate::command_executors::execute_thread_command(cmd),

            // Group network calls together with consistent cmd binding
            cmd @ Command::NetworkCall { .. }
            | cmd @ Command::UpdateAgent { .. }
            | cmd @ Command::DeleteAgentApi { .. } => {
                crate::command_executors::execute_network_command(cmd)
            }

            cmd @ Command::WebSocketAction { .. } => {
                crate::command_executors::execute_websocket_command(cmd)
            }

            // Persist debounced state saves
            Command::SaveState => crate::command_executors::execute_save_command(),

            // Template state commands handled directly here
            Command::TemplatesLoaded(templates) => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.templates = templates;
                    state.templates_loading = false;
                });
            }
            Command::TemplateCategoriesLoaded(categories) => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.template_categories = categories;
                });
            }
            Command::SetTemplateCategory(category) => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.selected_template_category = category;
                });
            }
            Command::ToggleMyTemplatesOnly => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.show_my_templates_only = !state.show_my_templates_only;
                });
            }
            Command::ShowTemplateGallery => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.show_template_gallery = true;
                });
            }
            Command::HideTemplateGallery => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.show_template_gallery = false;
                });
            }
            Command::TemplateDeployed(workflow) => {
                APP_STATE.with(|st| {
                    let mut state = st.borrow_mut();
                    state.workflows.insert(workflow.id, workflow);
                });
            }

            // Handle the non-API template commands by converting to API calls
            Command::LoadTemplates {
                category,
                my_templates,
            } => {
                dispatch_global_message(Message::LoadTemplates {
                    category,
                    my_templates,
                });
            }
            Command::LoadTemplateCategories => {
                dispatch_global_message(Message::LoadTemplateCategories);
            }
            Command::DeployTemplate {
                template_id,
                name,
                description,
            } => {
                let name_str = name.unwrap_or("Untitled Template".to_string());
                let desc_str = description.unwrap_or("Template deployment".to_string());
                dispatch_global_message(Message::DeployTemplate {
                    template_id,
                    name: name_str,
                    description: desc_str,
                });
            }

            Command::NoOp => {}
        }
    }

    // 3. Process legacy network calls (these will be migrated to commands)
    if let Some((text, message_id)) = network_data {
        // This only handles thread messages now
        if let Ok(thread_id) = message_id.parse::<u32>() {
            // If message_id is a thread ID, it's a thread message
            let command = Command::SendThreadMessage {
                thread_id,
                content: text,
                client_id: None,
            };
            crate::command_executors::execute_thread_command(command);
        } else {
            web_sys::console::warn_1(
                &format!("Unhandled pending network call: {}", message_id).into(),
            );
        }
    }
}
