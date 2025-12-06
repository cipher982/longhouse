# MCP Frontend Implementation Plan

_Status: ✅ COMPLETED_ · _Completed: May 2025_ · _Moved to completed: June 15, 2025_

## Overview

This document outlined the frontend implementation plan for integrating MCP (Model Context Protocol) servers into the Zerg platform. All phases have been successfully completed and the MCP integration is live in production.

## Architecture Overview

### Frontend Stack

- **Language**: Rust compiled to WebAssembly
- **DOM Manipulation**: web-sys
- **State Management**: Global APP_STATE with message-based updates
- **Styling**: CSS with design system variables

### Key Components to Modify/Create

1. **Agent Configuration Modal** (`frontend/src/components/agent_config_modal.rs`)
   - Add new "Tools & Integrations" tab
   - Implement three sections: Built-in Tools, Quick Connect, Custom Servers

2. **State Management** (`frontend/src/state.rs`)
   - Add MCP-related state fields
   - Handle MCP server configurations per agent

3. **Network Layer** (`frontend/src/network/`)
   - Add API calls for MCP server management
   - Handle WebSocket updates for tool execution

4. **Chat View** (`frontend/src/components/chat_view.rs`)
   - Enhance tool message rendering with MCP server info
   - Add visual indicators for tool source

## Phase 1: Core UI Structure (Week 1)

### 1.1 Add "Tools & Integrations" Tab ✅ COMPLETED

**Files Modified**:

- `frontend/src/state.rs`: Added `ToolsIntegrations` to `AgentConfigTab` enum, and new MCP-related state structures (`AgentMcpConfig`, `McpServerConfig`, `McpToolInfo`, `ConnectionStatus`) and fields to `AppState`.
- `frontend/src/messages.rs`: Added new message types for MCP operations (e.g., `LoadMcpTools`, `AddMcpServer`, `TestMcpConnection`).
- `frontend/src/update.rs`: Updated match statements to handle `AgentConfigTab::ToolsIntegrations` and all new MCP-related messages.

**Implementation Steps**:

1. Update `build_dom()` to include new tab button
2. Create content container for tools section
3. Add tab switching logic

### 1.2 Create Tool Sections Layout

**Structure**:

```
<div id="agent-tools-content" class="tab-content">
    <div id="builtin-tools-section" class="tools-section">
        <!-- Built-in tools list -->
    </div>

    <div id="quick-connect-section" class="tools-section">
        <!-- Preset buttons -->
    </div>

    <div id="custom-servers-section" class="tools-section">
        <!-- Custom MCP servers -->
    </div>
</div>
```

### 1.3 State Management Updates

**File**: `frontend/src/state.rs`

```rust
// Add to AppState
pub struct AppState {
    // ... existing fields ...

    // MCP-related state
    pub agent_mcp_configs: HashMap<u32, AgentMcpConfig>,
    pub available_mcp_tools: HashMap<u32, Vec<McpToolInfo>>,
    pub mcp_connection_status: HashMap<String, ConnectionStatus>,
}

pub struct AgentMcpConfig {
    pub servers: Vec<McpServerConfig>,
    pub allowed_tools: HashSet<String>,
}

pub struct McpServerConfig {
    pub name: String,
    pub url: Option<String>,
    pub preset: Option<String>,
    pub auth_token: Option<String>,
}

pub struct McpToolInfo {
    pub name: String,
    pub server_name: String,
    pub description: Option<String>,
}

pub enum ConnectionStatus {
    Healthy,
    Slow(u64), // Response time in ms
    Failed(String), // Error message
    Checking,
}
```

### 1.4 CSS Styling

**File**: `frontend/www/styles.css`

```css
/* Tools & Integrations Tab Styles */
.tools-section {
  margin-bottom: 24px;
  padding: 16px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background-color: var(--bg-secondary);
}

.tools-section h3 {
  margin: 0 0 12px 0;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}

.tool-item {
  display: flex;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid var(--border-light);
}

.tool-item:last-child {
  border-bottom: none;
}

.tool-checkbox {
  margin-right: 12px;
}

.tool-name {
  flex: 1;
  font-size: 14px;
}

.tool-source {
  font-size: 12px;
  color: var(--text-secondary);
  background-color: var(--bg-tertiary);
  padding: 2px 8px;
  border-radius: 12px;
}

/* Quick Connect Buttons */
.quick-connect-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
  margin-top: 12px;
}

.preset-button {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  background-color: var(--bg-primary);
  cursor: pointer;
  transition: all 0.2s;
}

.preset-button:hover {
  border-color: var(--primary-color);
  transform: translateY(-2px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.preset-button .icon {
  font-size: 24px;
  margin-bottom: 4px;
}

/* Connection Status Indicators */
.connection-status {
  display: inline-flex;
  align-items: center;
  font-size: 12px;
  margin-left: 8px;
}

.status-healthy {
  color: var(--success-color);
}

.status-slow {
  color: var(--warning-color);
}

.status-failed {
  color: var(--error-color);
}

.status-checking {
  color: var(--text-secondary);
}

/* MCP Server Card */
.mcp-server-card {
  padding: 12px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  margin-bottom: 8px;
  background-color: var(--bg-primary);
}

.server-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.server-name {
  font-weight: 600;
  font-size: 14px;
}

.server-actions {
  display: flex;
  gap: 8px;
}

.server-details {
  font-size: 12px;
  color: var(--text-secondary);
}
```

## Phase 2: Connection Management (Week 2)

### 2.1 API Integration

**File**: `frontend/src/network/api.rs` (create if doesn't exist)

```rust
// API calls for MCP server management
pub async fn add_mcp_server(
    agent_id: u32,
    server_config: McpServerConfig,
) -> Result<McpServerInfo, JsValue> {
    // POST /api/agents/{agent_id}/mcp-servers/
}

pub async fn remove_mcp_server(
    agent_id: u32,
    server_name: String,
) -> Result<(), JsValue> {
    // DELETE /api/agents/{agent_id}/mcp-servers/{server_name}
}

pub async fn test_mcp_connection(
    agent_id: u32,
    server_config: McpServerConfig,
) -> Result<ConnectionTestResult, JsValue> {
    // POST /api/agents/{agent_id}/mcp-servers/test
}

pub async fn get_available_tools(
    agent_id: u32,
) -> Result<ToolsResponse, JsValue> {
    // GET /api/agents/{agent_id}/mcp-servers/available-tools
}
```

### 2.2 Connection Modal Component

**File**: `frontend/src/components/mcp_connection_modal.rs` (new file)

```rust
pub struct McpConnectionModal;

impl McpConnectionModal {
    pub fn show_preset(preset_name: &str) -> Result<(), JsValue> {
        // Show modal for connecting to a preset service
    }

    pub fn show_custom() -> Result<(), JsValue> {
        // Show modal for adding custom MCP server
    }
}
```

### 2.3 Real-time Status Updates

- WebSocket messages for connection status changes
- Periodic health checks (every 30 seconds)
- Visual feedback during connection testing

## Phase 3: Tool Selection (Week 3)

### 3.1 Tool Selection UI

**Features**:

- Collapsible sections by server
- Checkbox for each tool
- Bulk selection options
- Search/filter functionality

### 3.2 Tool Persistence

- Save allowed tools in agent config
- Update backend when tools are selected/deselected
- Sync state across browser tabs

## Phase 4: Polish & Optimization (Week 4)

### 4.1 Loading States

- Skeleton loaders while fetching data
- Progress indicators for long operations
- Optimistic updates for better UX

### 4.2 Error Handling

- User-friendly error messages
- Retry mechanisms for failed connections
- Graceful degradation when MCP servers are unavailable

### 4.3 Accessibility

- Proper ARIA labels
- Keyboard navigation support
- Screen reader compatibility

## Testing Strategy

### Unit Tests

- State management logic
- API request/response handling
- UI component behavior

### Integration Tests

- End-to-end connection flow
- Tool selection persistence
- Error scenarios

### E2E Tests

- Complete user journey
- Cross-browser compatibility
- Performance under load

## Migration Strategy

1. **Feature Flag**: Initially hide behind a feature flag
2. **Beta Testing**: Roll out to select users first
3. **Gradual Rollout**: Enable for all users after stability confirmed
4. **Backwards Compatibility**: Ensure existing agents work unchanged

## Success Metrics

- **Connection Success Rate**: >95% for preset services
- **Tool Discovery Time**: <2 seconds
- **User Adoption**: 50% of agents using MCP tools within 30 days
- **Error Rate**: <1% for MCP-related operations

## Next Steps

1. Implement Phase 1 UI structure
2. Create mock data for development
3. Set up API integration tests
4. Begin Phase 2 implementation

---

This plan provides a comprehensive roadmap for implementing MCP integration in the frontend while maintaining a great user experience and system stability.
