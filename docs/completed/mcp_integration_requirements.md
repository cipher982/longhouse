# MCP Integration Requirements - Single Source of Truth

_Status: ✅ COMPLETED_ · _Completed: May 2025_ · _Moved to completed: June 15, 2025_

---

## Executive Summary

We will integrate MCP (Model Context Protocol) into our platform using a **hybrid approach** that preserves our built-in tool system while enabling customers to connect to ANY MCP server they choose. This gives customers immediate access to 50+ enterprise integrations while maintaining our architectural advantages.

---

## Core Principles

1. **Customer Freedom**: Customers can add ANY MCP server URL they find or build
2. **Convenience**: Provide one-click presets for popular services
3. **Reliability**: Keep critical tools as built-in for performance and availability
4. **Transparency**: Make it clear which tools are external dependencies

---

## Technical Architecture

### 1. Unified Tool Registry

All tools (built-in and MCP) appear in the same registry:

```python
# Tools are registered with prefixes to indicate source
- get_current_time           # Built-in tool
- math_eval                  # Built-in tool
- mcp_github_create_issue    # From GitHub MCP server
- mcp_linear_search_tasks    # From Linear MCP server
- mcp_custom_internal_tool   # From customer's MCP server
```

### 2. MCP Client Adapter

```python
class MCPManager:
    """Manages connections to multiple MCP servers."""

    def add_server(self, url: str, name: str, auth_token: str = None) -> None:
        """Add any MCP server - customer's primary use case."""
        client = MCPClient(url, auth_token)
        tools = client.discover_tools()
        for tool in tools:
            self.registry.register_mcp_tool(f"mcp_{name}_{tool.name}", tool)

    def add_preset(self, preset_name: str, auth_token: str) -> None:
        """Convenience method for popular services."""
        config = PRESET_CONFIGS[preset_name]
        self.add_server(config.url, preset_name, auth_token)
```

### 3. Agent Configuration Schema

```python
# backend/zerg/models/models.py
class Agent(Base):
    # ... existing fields ...

    # Option 1: Store MCP config in existing JSON config field
    config = Column(JSON, nullable=True)
    # config = {
    #     "mcp_servers": [
    #         {"url": "https://example.com/mcp", "name": "custom", "auth_token": "xxx"},
    #         {"preset": "github", "auth_token": "ghp_xxx"}
    #     ]
    # }
```

### 4. Error Handling & Resilience

```python
async def execute_mcp_tool(server_url: str, tool_name: str, args: dict):
    try:
        async with timeout(30):  # 30 second timeout
            return await mcp_client.call_tool(tool_name, args)
    except TimeoutError:
        logger.error(f"MCP tool {tool_name} timed out")
        return {"error": "Tool execution timed out", "status": "timeout"}
    except MCPServerError as e:
        logger.error(f"MCP server {server_url} error: {e}")
        return {"error": "MCP server unavailable", "status": "server_error"}
```

---

## UI/UX Requirements

### Agent Configuration Interface

```
┌─────────────────────────────────────────────────┐
│ 🛠️  Tool Configuration                          │
├─────────────────────────────────────────────────┤
│ Built-in Tools ✓                                │
│ • Date/Time (get_current_time)                  │
│ • Math (math_eval)                              │
│ • HTTP Requests (http_request)                      │
│                                                 │
│ ─────────────────────────────────────           │
│                                                 │
│ 🚀 Quick Connect (Popular Services)             │
│                                                 │
│ [🔗 Connect GitHub]  [📋 Connect Linear]        │
│ [💬 Connect Slack]   [📝 Connect Notion]        │
│                                                 │
│ ─────────────────────────────────────           │
│                                                 │
│ ➕ Add Custom MCP Server                        │
│                                                 │
│ Server URL: [________________________]         │
│ Name: [_____________]                           │
│ Auth Token: [_____________________] 🔒          │
│                                                 │
│ [Test Connection]  [Add Server]                 │
│                                                 │
│ ─────────────────────────────────────           │
│                                                 │
│ 📊 Connected MCP Servers                        │
│                                                 │
│ • github (3 tools)          ✅ Online  [Remove] │
│ • internal-crm (5 tools)    ✅ Online  [Remove] │
│ • weather-api (2 tools)     ⚠️ Slow    [Remove] │
└─────────────────────────────────────────────────┘
```

### Tool Selection Interface

When selecting allowed tools for an agent:

```
Available Tools
├── 🛠️ Built-in (Always Available)
│   ├── ☑️ get_current_time
│   ├── ☑️ math_eval
│   └── ☑️ http_request
└── 🌐 From MCP Servers
    ├── github
    │   ├── ☐ create_issue
    │   ├── ☐ search_repositories
    │   └── ☐ get_pull_request
    └── internal-crm
        ├── ☐ lookup_customer
        └── ☐ create_ticket
```

---

## Implementation Plan

### Week 1: Core MCP Support ✅ COMPLETED

1. **Update MCP adapter** (`backend/zerg/tools/mcp_adapter.py`)
   - ✅ Removed hardcoded servers - moved to `mcp_presets.py`
   - ✅ Implemented dynamic server addition via `MCPManager`
   - ✅ Added comprehensive error handling with custom exceptions
   - ✅ Input validation using JSON schemas
   - ✅ Connection pooling and retry logic with exponential backoff
   - ✅ Health checks before tool registration
   - ✅ Dedicated event loop for async operations

2. **Registry improvements** (`backend/zerg/tools/registry.py`)
   - ✅ Added override mechanism for testing (no more monkey patching!)
   - ✅ Support for temporary tool overrides
   - ✅ Proper handling of overrides in all registry methods

3. **Integration with agent factory**
   - ✅ Implemented in `zerg_react_agent.py`
   - ✅ Clean integration without monkey patching

   ```python
   # In get_runnable()
   if agent.config and 'mcp_servers' in agent.config:
       load_mcp_tools_sync(agent.config['mcp_servers'])
   ```

4. **Configuration schema** (`backend/zerg/tools/mcp_config_schema.py`)
   - ✅ Clear type discrimination between preset and custom configs
   - ✅ Validation and normalization functions
   - ✅ TypedDict definitions for type safety
   - ✅ Support for legacy configuration format

5. **Error handling** (`backend/zerg/tools/mcp_exceptions.py`)
   - ✅ Custom exception hierarchy for different failure modes
   - ✅ `MCPConnectionError` for network issues
   - ✅ `MCPAuthenticationError` for auth failures
   - ✅ `MCPToolExecutionError` for tool runtime errors
   - ✅ `MCPValidationError` for input validation failures
   - ✅ `MCPConfigurationError` for config issues

### Database Updates ✅ COMPLETED

- ✅ Store MCP server configs in agent.config JSON field
- ✅ Add API endpoints for MCP server management:
  - `GET /api/agents/{agent_id}/mcp-servers/` - List configured servers
  - `POST /api/agents/{agent_id}/mcp-servers/` - Add new server
  - `DELETE /api/agents/{agent_id}/mcp-servers/{server_name}` - Remove server
  - `POST /api/agents/{agent_id}/mcp-servers/test` - Test connection
  - `GET /api/agents/{agent_id}/mcp-servers/available-tools` - List all tools
- ✅ Created comprehensive test suite (`backend/tests/test_mcp_servers.py`)
- ✅ Registered MCP router in main FastAPI app

### Week 2: Presets & OAuth ✅ COMPLETED

1. **Popular service presets** ✅ COMPLETED
   - ✅ GitHub - with 5 tools configured
   - ✅ Linear - with 5 tools configured
   - ✅ Slack - with 4 tools configured
   - ✅ Notion - with 7 tools configured
   - ✅ Asana - with 7 tools configured
   - All presets available in `backend/zerg/tools/mcp_presets.py`

2. **OAuth token management** ✅ COMPLETED
   - ✅ Secure token storage (encrypted using AES-GCM via Fernet)
   - ✅ Automatic encryption/decryption of auth tokens
   - ✅ HTTPS URL validation for security
   - ✅ Integrated with existing crypto utilities
   - Token refresh logic (deferred - specific to OAuth2 providers)
   - OAuth flow helpers for presets (deferred - requires UI integration)

### Week 3: UI Implementation

**Progress as of 2025-05-27:**

#### ✅ MCP UI Core Components Implemented

- **MCP API Client**: Added all required async methods for MCP server management and tool discovery in `frontend/src/network/api_client.rs`.
- **MCP Server Manager Component**: Created `frontend/src/components/mcp_server_manager.rs` with:
  - Quick Connect tab for presets (GitHub, Linear, Slack, Notion)
  - Custom Server tab for arbitrary MCP URLs
  - Built-in tools section
  - Connected servers list with status and removal
  - Connection testing and error handling
- **Agent Configuration Modal**:
  - Added a third "Tools" tab (now `ToolsIntegrations`) to the modal in `frontend/src/components/agent_config_modal.rs`
  - Tab switching logic and content containers for all three tabs
  - Tools tab renders the MCP Server Manager UI
- **Message System**:
  - All MCP UI messages added to `frontend/src/messages.rs`
  - Update logic in `frontend/src/update.rs` now handles all MCP UI messages (stubs for future logic, no more non-exhaustive match error)

#### 🛠️ Build/Compile Status

- All enum variants and message arms are now handled; the project compiles and runs.
- No more non-exhaustive match errors.
- UI structure and message wiring are in place for further MCP integration.

#### 🟢 Progress as of 2025-05-27

- **MCP UI message handling and backend integration is complete:**
  - All MCP UI events (`SetMCPTab`, `ConnectMCPPreset`, `AddMCPServer`, `RemoveMCPServer`, `TestMCPConnection`) are now fully wired to backend API calls and state updates in `frontend/src/update.rs`.
  - State synchronization is in place: MCP server and tool state is loaded and updated in the UI after add/remove/test actions.
  - The codebase now uses only the canonical method name (`get_mcp_available_tools`) for MCP tool fetching—no legacy or compatibility code remains.

---

#### 🟡 Next Steps

1. **UI Polish & Error Handling**
   - Display connection errors and tool status in the UI
   - Add loading indicators and feedback for async actions

2. **Testing**
   - Manual and automated tests for all MCP UI flows
   - Edge cases: duplicate server names, invalid URLs, token errors

3. **Documentation**
   - Update user and developer docs for MCP UI usage and extension

---

#### Updated Implementation Plan

- **Week 3: UI Implementation**
  - ✅ MCP API client and UI components created and integrated
  - ✅ Agent configuration modal updated with Tools tab
  - ✅ Message system and update logic extended for MCP UI
  - ✅ Full UI-to-backend wiring and state sync for MCP servers and tools
  - 🟡 Next: UI polish, error display, and comprehensive testing

---

_This section will be updated as further MCP UI logic and integration are completed._

## Security Considerations

1. **Token Storage**: Encrypt all auth tokens at rest
2. **URL Validation**: Only allow HTTPS MCP server URLs
3. **Timeout Protection**: 30-second timeout on all MCP calls
4. **Rate Limiting**: Implement per-server rate limits
5. **Audit Logging**: Log all MCP tool executions

---

## Success Metrics

- **Week 1**: Successfully connect to any MCP server URL
- **Week 2**: 5 preset integrations working with OAuth
- **Week 3**: UI complete, 10 beta customers testing
- **Month 2**: 100+ agents using MCP tools in production

---

## API Examples

### Add MCP Server

```http
POST /api/agents/{agent_id}/mcp-servers
{
  "url": "https://example.com/mcp/sse",
  "name": "example",
  "auth_token": "bearer_xxx"
}
```

### Add Preset

```http
POST /api/agents/{agent_id}/mcp-servers
{
  "preset": "github",
  "auth_token": "ghp_xxx"
}
```

### List Available Tools

```http
GET /api/agents/{agent_id}/available-tools
{
  "builtin": ["get_current_time", "math_eval", "http_request"],
  "mcp": {
    "github": ["create_issue", "search_repositories"],
    "custom": ["internal_tool_1", "internal_tool_2"]
  }
}
```

---

## Key Decisions

1. **Both presets AND custom URLs**: Not either/or
2. **Unified tool registry**: All tools appear together
3. **Customer owns risk**: They choose which MCP servers to trust
4. **Clear UI separation**: Built-in vs external tools
5. **Graceful degradation**: Handle MCP failures without breaking agents

---

## Next Steps

1. Review and approve this requirements document
2. Create implementation tickets for Week 1 tasks
3. Begin with MCP adapter updates
4. Set up testing environment with real MCP servers

---

_This document supersedes all previous MCP-related documentation and represents our final architectural decision._
