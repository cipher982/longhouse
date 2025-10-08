// frontend/src/messages.rs
//
// The events that can occur in your UI. Expand as needed.
//
use crate::generated::ws_messages::AgentEventData;
use crate::models::ApiWorkflow;
use crate::models::{ApiAgent, ApiAgentDetails, ApiThread, ApiThreadMessage, NodeType};
use crate::storage::ActiveView;
use std::collections::{HashMap, HashSet};

use crate::models::ToolConfig;

#[derive(Debug, Clone)]
#[allow(dead_code)] // Suppress warnings about unused variants
pub enum Message {
    SaveToolConfig {
        node_id: String,
        config: ToolConfig,
    },
    /// Enable or disable global keyboard shortcuts (“Power Mode”)
    SetPowerMode(bool),
    // View switching
    ToggleView(ActiveView), // Switch between Dashboard and Canvas

    // Agent management
    CreateAgent(String), // Create a new agent with a given name
    CreateAgentWithDetails {
        // Enhanced version with complete details
        name: String,
        agent_id: u32, // API-provided ID
        system_instructions: String,
        task_instructions: String,
    },
    EditAgent(u32),
    RequestAgentDeletion {
        agent_id: u32,
    },
    AgentDeletionSuccess {
        agent_id: u32,
    },
    AgentDeletionFailure {
        agent_id: u32,
        error: String,
    },

    // Canvas generation from agents
    GenerateCanvasFromAgents, // Create nodes for agents that don't have one

    // Node manipulation
    UpdateNodePosition {
        node_id: String,
        x: f64,
        y: f64,
    },
    AddNode {
        text: String,
        x: f64,
        y: f64,
        node_type: NodeType,
    },
    AddResponseNode {
        parent_id: String,
        response_text: String,
    },

    // Canvas node creation (may reference an agent)
    AddCanvasNode {
        agent_id: Option<u32>,
        x: f64,
        y: f64,
        node_type: NodeType,
        text: String,
    },
    DeleteNode {
        node_id: String,
    },

    // Canvas interaction – a node was clicked (no drag)
    CanvasNodeClicked {
        node_id: String,
    },

    ShowToolConfigModal {
        node_id: String,
    },
    /// Open the trigger config modal for a trigger node
    ShowTriggerConfigModal {
        node_id: String,
    },
    /// Update the config for a trigger node on the canvas
    UpdateTriggerNodeConfig {
        node_id: String,
        params: serde_json::Value,
    },

    // Workflow management
    CreateWorkflow {
        name: String,
    },
    SelectWorkflow {
        workflow_id: u32,
    },
    AddEdge {
        from_node_id: String,
        to_node_id: String,
        label: Option<String>,
    },

    // Connection creation UI
    ToggleConnectionMode, // Toggle connection creation mode
    SelectNodeForConnection {
        // Select a node for connection
        node_id: String,
    },
    CreateConnectionFromSelected {
        // Create connection from selected node to target
        target_node_id: String,
    },
    ClearNodeSelection, // Clear current node selection

    // Connection handle dragging
    StartConnectionDrag {
        // Start dragging from a connection handle
        node_id: String,
        handle_position: String,
        start_x: f64,
        start_y: f64,
    },
    UpdateConnectionDrag {
        // Update connection drag position
        current_x: f64,
        current_y: f64,
    },
    EndConnectionDrag {
        // End connection drag
        end_x: f64,
        end_y: f64,
    },

    // Workflow rename/delete actions coming from UI
    RenameWorkflow {
        workflow_id: u32,
        name: String,
        description: String,
    },
    DeleteWorkflow {
        workflow_id: u32,
    },

    // Loading state messages for workflow operations
    WorkflowCreationStarted,
    WorkflowDeletionStarted {
        workflow_id: u32,
    },
    WorkflowUpdateStarted {
        workflow_id: u32,
    },

    // Workflow scheduling actions
    ScheduleWorkflow {
        workflow_id: u32,
        cron_expression: String,
    },
    UnscheduleWorkflow {
        workflow_id: u32,
    },
    CheckWorkflowSchedule {
        workflow_id: u32,
    },

    // Workflows fetched from backend
    WorkflowsLoaded(Vec<ApiWorkflow>),
    /// Workflow successfully created via backend
    WorkflowCreated(ApiWorkflow),
    CurrentWorkflowLoaded(ApiWorkflow),
    /// Workflow was deleted (soft delete)
    WorkflowDeleted {
        workflow_id: u32,
    },
    /// Workflow renamed / updated
    WorkflowUpdated(ApiWorkflow),

    // Canvas view controls
    ToggleAutoFit, // Toggle auto-fit functionality
    CenterView,    // Center the canvas view
    ResetView,     // Reset zoom =100% and pan to origin
    ClearCanvas,   // Clear all nodes from the canvas

    // Canvas zooming
    ZoomCanvas {
        new_zoom: f64,
        viewport_x: f64,
        viewport_y: f64,
    },

    // -------------------------------------------------------------------
    // Node ↔ Agent *display* sync helpers
    // -------------------------------------------------------------------
    /// Refresh the `.text` labels of all CanvasNodes that reference an agent
    /// so they reflect the **current** `agent.name`.  Used after agent rename
    /// operations or bulk agent refresh from the backend.  One-way sync only
    /// (agents are the source-of-truth).
    RefreshCanvasLabels,

    // Input handling
    UpdateInputText(String), // Update the input text field

    // Mark canvas as needing a redraw (set `dirty = true`).  Should not be
    // produced by the renderer itself – only helpers that run outside the
    // normal reducer flow and cannot obtain a mutable AppState borrow.
    MarkCanvasDirty,

    // Dragging state
    StartDragging {
        node_id: String,
        offset_x: f64,
        offset_y: f64,
        start_x: f64,
        start_y: f64,
        is_agent: bool,
    },
    StopDragging,

    // Canvas dragging
    StartCanvasDrag {
        start_x: f64,
        start_y: f64,
    },
    UpdateCanvasDrag {
        current_x: f64,
        current_y: f64,
    },
    StopCanvasDrag,

    // Other actions can be added as needed

    // Modal operations
    SaveAgentDetails {
        name: String,
        system_instructions: String,
        task_instructions: String,
        model: String,
        schedule: Option<String>,
    },

    CloseAgentModal,

    // Task operations
    SendTaskToAgent,

    // Tab switching in modal – use the unified variant below.
    /// Unified variant – switch to the given tab.
    SetAgentTab(crate::state::AgentConfigTab),

    // Auto-save operations
    UpdateSystemInstructions(String),
    UpdateAgentName(String),

    // WebSocket related messages
    UpdateNodeText {
        node_id: String,
        text: String,
        is_first_chunk: bool,
    },

    CompleteNodeResponse {
        node_id: String,
        final_text: String,
    },

    UpdateNodeStatus {
        node_id: String,
        phase: String,
        result: Option<String>,
    },

    /// Update connection animation state based on node execution
    UpdateConnectionAnimation {
        from_node_id: String,
        to_node_id: String,
        is_executing: bool,
    },

    /// Workflow execution finished (success or failed)
    ExecutionFinished {
        execution_id: u32,
        phase: String,
        result: String,
        error: Option<String>,
    },

    /// Append a single stdout/stderr line to the log drawer
    AppendExecutionLog {
        execution_id: u32,
        node_id: String,
        stream: String,
        text: String,
    },

    // ------------------------------------------------------------------
    // Execution history / right-hand sidebar
    // ------------------------------------------------------------------
    /// Toggle the execution history drawer
    ToggleExecutionHistory,

    /// Load execution history for current workflow (async command will follow)
    LoadExecutionHistory {
        workflow_id: u32,
    },

    /// Execution history loaded from backend
    ExecutionHistoryLoaded(Vec<crate::models::ExecutionSummary>),

    /// User selected an execution from the list – triggers replay
    SelectExecution {
        execution_id: u32,
    },

    /// Toggle the collapsible execution log drawer
    ToggleLogDrawer,

    /// User clicked Run – initiate backend execution and subscribe to progress.
    StartWorkflowExecution {
        workflow_id: u32,
    },

    /// Reserve an execution ID and subscribe to its WebSocket topic
    ReserveWorkflowExecution {
        workflow_id: u32,
    },

    /// Start a previously reserved execution
    StartReservedExecution {
        execution_id: u32,
    },

    // -------------------------------------------------------------------
    // Workflow execution streaming
    // -------------------------------------------------------------------
    /// Subscribe to execution progress for the given execution_id.
    SubscribeWorkflowExecution {
        execution_id: u32,
    },

    // Toggle / change dashboard sort
    UpdateDashboardSort(crate::state::DashboardSortKey),

    // Database management
    ResetDatabase, // Clear all agent data from database

    // Reload agents from the API to refresh state
    RefreshAgentsFromAPI,
    AgentsRefreshed(Vec<ApiAgent>), // Agents have been refreshed from API

    // Animation related
    AnimationTick,

    // Thread-related messages
    LoadThreads(u32),              // Load threads for an agent
    ThreadsLoaded(Vec<ApiThread>), // DEPRECATED: Use AgentThreadsLoaded instead
    CreateThread(u32, String),     // Create a new thread for an agent
    ThreadCreated(ApiThread),      // Changed String to ApiThread for direct use
    SelectThread(u32),             // Select a thread

    // NEW: Agent-Scoped Thread Messages
    /// Load threads for a specific agent (agent-scoped)
    LoadAgentThreads(u32),
    /// Threads loaded for a specific agent (agent-scoped)
    AgentThreadsLoaded {
        agent_id: u32,
        threads: Vec<ApiThread>,
    },
    /// Select a thread within the current agent context
    SelectAgentThread {
        agent_id: u32,
        thread_id: u32,
    },
    /// Navigate to agent chat view with clean state management
    NavigateToAgentChat(u32),
    LoadThreadMessages(u32), // Load messages for a thread
    ThreadMessagesLoaded(u32, Vec<ApiThreadMessage>), // Changed String to Vec<ApiThreadMessage> + thread_id
    SendThreadMessage(u32, String),                   // Send a message to a thread
    ThreadMessageSent(String, String),                // DEPRECATED: Remove after migration complete
    ThreadMessageFailed(u32, String),                 // DEPRECATED: Remove after migration complete
    UpdateThreadTitle(u32, String),                   // Update thread title (request)
    DeleteThread(u32),                                // Delete thread

    // --- NEW WebSocket Received Messages ---
    ReceiveNewMessage(ApiThreadMessage), // New message received via WebSocket
    ReceiveThreadUpdate {
        // Thread metadata updated via WebSocket
        thread_id: u32,
        title: Option<String>,
    },

    // ---------------- Run History (AgentRun) ----------------
    /// Request to load the latest runs for an agent (REST call)
    LoadAgentRuns(u32), // agent_id
    /// Response containing latest runs list
    ReceiveAgentRuns {
        agent_id: u32,
        runs: Vec<crate::models::ApiAgentRun>,
    },
    /// Real-time update for a single run via WebSocket
    ReceiveRunUpdate {
        agent_id: u32,
        run: crate::models::ApiAgentRun,
    },

    // -------------------------------------------------------------------
    // Trigger management (Phase A)
    // -------------------------------------------------------------------
    /// Request the list of triggers for an agent (REST call)
    LoadTriggers(u32), // agent_id
    /// Response containing current triggers
    TriggersLoaded {
        agent_id: u32,
        triggers: Vec<crate::models::Trigger>,
    },
    /// A trigger was created via the modal wizard
    TriggerCreated {
        agent_id: u32,
        trigger: crate::models::Trigger,
    },
    /// A trigger was deleted (list should refresh)
    TriggerDeleted {
        agent_id: u32,
        trigger_id: u32,
    },

    /// User requested to create a new trigger (UI ➜ update ➜ REST)
    RequestCreateTrigger {
        payload_json: String,
    },

    /// User clicked delete on a trigger row.
    RequestDeleteTrigger {
        trigger_id: u32,
    },

    // -------------------------------------------------------------------
    // Gmail OAuth flow (Phase C)
    // -------------------------------------------------------------------
    /// OAuth flow succeeded – the backend confirmed refresh-token storage.
    GmailConnected,
    /// OAuth flow succeeded and returned a connector id
    GmailConnectedWithConnector { connector_id: u32 },

    /// Toggle between compact (first 5) and full run list for an agent row
    ToggleRunHistory {
        agent_id: u32,
    },

    // -------------------------------------------------------------------
    // Dashboard filtering (My vs All agents)
    // -------------------------------------------------------------------
    ToggleDashboardScope(crate::state::DashboardScope),
    ReceiveStreamStart(u32), // Start of streaming response for thread_id
    ReceiveStreamChunk {
        // Chunk of streaming response
        thread_id: u32,
        content: String,
        chunk_type: Option<String>, // "tool_output" or "assistant_message"
        tool_name: Option<String>,
        tool_call_id: Option<String>,
        message_id: Option<String>,
    },
    ReceiveStreamEnd(u32), // End of streaming response for thread_id
    /// The backend sent the id of the assistant bubble currently being
    /// streamed (token mode).  Enables correct parent-linking of tool_output
    /// messages.
    ReceiveAssistantId {
        thread_id: u32,
        message_id: u32,
    },
    // Using UpdateConversation for thread history
    // --- END NEW WebSocket Received Messages ---

    // Navigation messages
    NavigateToChatView(u32),   // DEPRECATED: Use NavigateToAgentChat instead
    NavigateToThreadView(u32), // Navigate to specific thread
    NavigateToDashboard,       // Back to dashboard

    // Chat view messages
    LoadAgentInfo(u32),               // Request to load agent info
    AgentInfoLoaded(Box<ApiAgent>),   // Changed String to Box<ApiAgent>
    RequestNewThread,                 // Request to create new thread
    RequestSendMessage(String),       // Request to send message
    RequestUpdateThreadTitle(String), // Request to update thread title
    RequestThreadTitleUpdate,         // Request to refresh thread title UI
    RequestThreadListUpdate(u32),     // Request to update thread list for agent
    UpdateThreadList(
        Vec<ApiThread>,
        Option<u32>,
        HashMap<u32, Vec<ApiThreadMessage>>,
    ), // Update thread list UI
    UpdateConversation(Vec<ApiThreadMessage>), // Update conversation UI
    UpdateThreadTitleUI(String),      // Update thread title UI with provided title
    UpdateLoadingState(bool),         // Update loading state in UI

    // Force a redraw of the Dashboard table after agents have been reloaded
    RefreshDashboard,

    ReceiveAgentUpdate(AgentEventData),
    ReceiveAgentDelete(i32),
    ReceiveThreadHistory(Vec<ApiThreadMessage>),

    // -------------------------------------------------------------------
    // Agent Debug / Info Modal (Phase 1)
    // -------------------------------------------------------------------
    /// Open the read-only debug modal for the specified agent.
    ShowAgentDebugModal {
        agent_id: u32,
    },
    /// Close/hide the debug modal.
    HideAgentDebugModal,
    /// Backend payload with full AgentDetails has arrived.
    ReceiveAgentDetails(ApiAgentDetails),

    // Switch active tab in Agent Debug Modal
    SetAgentDebugTab(crate::state::DebugTab),

    // Add the DeleteAgentApi command inside the enum
    DeleteAgentApi {
        agent_id: u32,
    }, // Command to execute the API call for deletion

    // -------------------------------------------------------------------
    // Authentication / User profile
    // -------------------------------------------------------------------
    /// The authenticated user's profile has been fetched from `/api/users/me`.
    CurrentUserLoaded(crate::models::CurrentUser),

    /// Admin status has been checked for the current user
    AdminStatusLoaded {
        is_super_admin: bool,
        requires_password: bool,
    },

    // Model management
    SetAvailableModels {
        models: Vec<(String, String)>,
        default_model_id: String,
    },

    // Agent creation message
    RequestCreateAgent {
        name: String,
        system_instructions: String,
        task_instructions: String,
    },

    // Dashboard-specific actions for error rows
    RetryAgentTask {
        agent_id: u32,
    },
    DismissAgentError {
        agent_id: u32,
    },

    // -------------------------------------------------------------------
    // MCP Integration Messages
    // -------------------------------------------------------------------
    /// Load available MCP tools for an agent
    LoadMcpTools(u32), // agent_id

    /// Received available MCP tools
    McpToolsLoaded {
        agent_id: u32,
        builtin_tools: Vec<String>,
        mcp_tools: HashMap<String, Vec<crate::state::McpToolInfo>>,
    },

    /// Add an MCP server to an agent
    AddMcpServer {
        agent_id: u32,
        server_config: crate::state::McpServerConfig,
    },

    /// Remove an MCP server from an agent
    RemoveMcpServer {
        agent_id: u32,
        server_name: String,
    },

    /// Test MCP server connection
    TestMcpConnection {
        agent_id: u32,
        server_config: crate::state::McpServerConfig,
    },

    /// MCP connection test result
    McpConnectionTested {
        agent_id: u32,
        server_name: String,
        status: crate::state::ConnectionStatus,
    },

    /// Update allowed tools for an agent
    UpdateAllowedTools {
        agent_id: u32,
        allowed_tools: HashSet<String>,
    },

    /// MCP server was successfully added
    McpServerAdded {
        agent_id: u32,
        server_name: String,
    },

    /// MCP server was successfully removed
    McpServerRemoved {
        agent_id: u32,
        server_name: String,
    },

    /// Error occurred with MCP operation
    McpError {
        agent_id: u32,
        error: String,
    },

    // Additional MCP UI messages
    /// Set active MCP tab for an agent
    SetMCPTab {
        agent_id: u32,
        tab: crate::components::mcp_server_manager::MCPTab,
    },

    /// Connect to an MCP preset (GitHub, Linear, etc.)
    ConnectMCPPreset {
        agent_id: u32,
        preset_id: String,
    },

    /// Add MCP server (for component interface)
    AddMCPServer {
        agent_id: u32,
        url: Option<String>,
        name: String,
        preset: Option<String>,
        auth_token: String,
    },

    /// Remove MCP server (for component interface)
    RemoveMCPServer {
        agent_id: u32,
        server_name: String,
    },

    /// Test MCP connection (for component interface)
    TestMCPConnection {
        agent_id: u32,
        url: String,
        name: String,
        auth_token: String,
    },

    // Particle System Messages
    InitializeParticleSystem {
        width: f64,
        height: f64,
    },
    ClearParticleSystem,

    // Template Gallery Messages
    ShowTemplateGallery,
    HideTemplateGallery,
    LoadTemplates {
        category: Option<String>,
        my_templates: bool,
    },
    TemplatesLoaded(Vec<crate::models::WorkflowTemplate>),
    LoadTemplateCategories,
    TemplateCategoriesLoaded(Vec<String>),
    SetTemplateCategory(Option<String>),
    ToggleMyTemplatesOnly,
    DeployTemplate {
        template_id: u32,
        name: String,
        description: String,
    },
    TemplateDeployed(ApiWorkflow),
    
    // Storage system state updates - avoid direct mutations
    UpdateViewport { x: f64, y: f64, zoom: f64 },
    UpdateWorkflowNodes(std::collections::HashMap<String, crate::models::WorkflowNode>),
    SetDataLoaded(bool),
    ClearAgents,
    AddAgents(Vec<crate::models::ApiAgent>),
    
    // Initialization state updates - avoid direct mutations
    SetGoogleClientId(Option<String>),

    // -------------------------------------------------------------------
    // Ops Dashboard / HUD
    // -------------------------------------------------------------------
    OpsSummaryLoaded(crate::models::OpsSummary),
    OpsAppendEvent { ts: u64, kind: String, text: String },
}

/// Commands represent side effects that should be executed after state updates.
/// This separates pure state changes from effects like UI updates, API calls, etc.
pub enum Command {
    /// Chain another message to be processed
    SendMessage(Message),

    /// Execute a UI update function after state changes
    UpdateUI(Box<dyn FnOnce() + 'static>),

    /// Fetch threads for an agent
    FetchThreads(u32), // agent_id

    /// Fetch messages for a thread
    FetchThreadMessages(u32), // thread_id

    /// Fetch workflows (visual canvas definitions)
    FetchWorkflows,

    /// Fetch current working workflow (creates if none exists)
    FetchCurrentWorkflow,

    /// Fetch execution history for workflow
    FetchExecutionHistory {
        workflow_id: u32,
    },

    // Workflow CRUD actions
    CreateWorkflowApi {
        name: String,
    },
    DeleteWorkflowApi {
        workflow_id: u32,
    },
    RenameWorkflowApi {
        workflow_id: u32,
        name: String,
        description: String,
    },

    // ---------------- Workflow execution -----------------------------
    /// POST /workflow-executions/{workflow_id}/start
    StartWorkflowExecutionApi {
        workflow_id: u32,
    },

    /// POST /workflow-executions/{workflow_id}/reserve
    ReserveWorkflowExecutionApi {
        workflow_id: u32,
    },

    /// POST /workflow-executions/executions/{execution_id}/start
    StartReservedExecutionApi {
        execution_id: u32,
    },

    // ---------------- Workflow scheduling -----------------------------
    /// POST /workflow-executions/{workflow_id}/schedule
    ScheduleWorkflowApi {
        workflow_id: u32,
        cron_expression: String,
    },
    /// DELETE /workflow-executions/{workflow_id}/schedule
    UnscheduleWorkflowApi {
        workflow_id: u32,
    },
    /// GET /workflow-executions/{workflow_id}/schedule
    CheckWorkflowScheduleApi {
        workflow_id: u32,
    },

    // ---------------------------------------------------------------
    // Trigger-related side-effect commands (Phase A wiring only)
    // ---------------------------------------------------------------
    /// Fetch triggers for the specified agent id.
    FetchTriggers(u32),
    /// Perform POST /triggers with given JSON payload.
    CreateTrigger {
        payload_json: String,
    },
    /// DELETE /triggers/{id}
    DeleteTrigger(u32), // trigger_id

    /// Create a new thread
    CreateThread {
        agent_id: u32,
        title: String,
    },

    /// Send a message to a thread
    SendThreadMessage {
        thread_id: u32,
        content: String,
        client_id: Option<u32>,
    },

    /// Run a thread to process messages
    #[allow(dead_code)]
    RunThread(u32), // thread_id

    /// Update thread title
    UpdateThreadTitle {
        thread_id: u32,
        title: String,
    },

    /// Update an agent
    UpdateAgent {
        agent_id: u32,
        payload: String,
        on_success: Box<Message>,
        on_error: Box<Message>,
    },

    /// Load agent info
    #[allow(dead_code)]
    LoadAgentInfo(u32),

    /// Generic network call
    NetworkCall {
        endpoint: String,
        method: String,
        body: Option<String>,
        on_success: Box<Message>,
        on_error: Box<Message>,
    },

    /// WebSocket operation
    #[allow(dead_code)]
    WebSocketAction {
        action: String,
        topic: Option<String>,
        data: Option<String>,
    },

    /// Represents no side effect
    NoOp,

    // Template Gallery Messages
    LoadTemplates {
        category: Option<String>,
        my_templates: bool,
    },
    TemplatesLoaded(Vec<crate::models::WorkflowTemplate>),
    LoadTemplateCategories,
    TemplateCategoriesLoaded(Vec<String>),
    SetTemplateCategory(Option<String>),
    ToggleMyTemplatesOnly,
    ShowTemplateGallery,
    HideTemplateGallery,
    DeployTemplate {
        template_id: u32,
        name: Option<String>,
        description: Option<String>,
    },
    TemplateDeployed(ApiWorkflow),

    // Add the DeleteAgentApi command inside the enum
    DeleteAgentApi {
        agent_id: u32,
    }, // Command to execute the API call for deletion

    FetchAgents, // Command to fetch agents from API

    /// Fetch visual workflows from backend
    /// Fetch detailed debug info for an agent
    FetchAgentDetails(u32), // agent_id

    /// Fetch latest runs for an agent
    FetchAgentRuns(u32), // agent_id

    // Template Gallery Commands
    LoadTemplatesApi {
        category: Option<String>,
        my_templates: bool,
    },
    LoadTemplateCategoriesApi,
    DeployTemplateApi {
        template_id: u32,
        name: Option<String>,
        description: Option<String>,
    },

    /// Persist current canvas/layout state (debounced in AnimationTick)
    SaveState,
}

impl Command {
    /// Helper to create a SendMessage command
    #[allow(dead_code)]
    pub fn send(msg: Message) -> Self {
        Command::SendMessage(msg)
    }

    /// Helper to create a NoOp command
    #[allow(dead_code)]
    pub fn none() -> Self {
        Command::NoOp
    }

    /// Helper to create an UpdateUI command
    #[allow(dead_code)]
    pub fn update_ui<F>(f: F) -> Self
    where
        F: FnOnce() + 'static,
    {
        Command::UpdateUI(Box::new(f))
    }

    /// Helper to create a NetworkCall command
    #[allow(dead_code)]
    pub fn network_call(
        endpoint: String,
        method: String,
        body: Option<String>,
        on_success: Message,
        on_error: Message,
    ) -> Self {
        Command::NetworkCall {
            endpoint,
            method,
            body,
            on_success: Box::new(on_success),
            on_error: Box::new(on_error),
        }
    }
}
