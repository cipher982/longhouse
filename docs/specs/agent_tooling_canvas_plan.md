# Agent Tooling & Canvas – Simplified Design

**Version:** 2.0
**Date:** December 2025
**Status:** Foundation Complete, Canvas Optional
**Philosophy:** Agents are code. Visual tools are for humans, not architectural requirements.

---

## 1. Purpose

Enable users to create and manage AI agents that can execute tasks autonomously.

**v1.0 approach (DEPRECATED):**

- Visual canvas with Agent, Tool, Trigger, Condition node types
- ToolRegistry for centralized tool management
- Per-agent allowed_tools allowlists
- Complex node types for workflow composition

**v2.0 approach (SIMPLIFIED):**

- Agents are defined in code or via API
- Tools are capabilities (SSH access, HTTP, connectors)
- Canvas is optional visualization layer (not architectural requirement)
- Trust LLMs to use appropriate tools for the task

---

## 2. Core Architecture

### 2.1 What an Agent Is

```python
Agent:
  name: "Infrastructure Monitor"
  model: "gpt-4o"  # or gpt-4o-mini, claude-opus-4-5
  system_instructions: """
    You monitor infrastructure health.
    You have SSH access to: cube, clifford, zerg, slim
    Check disk, docker, backups, connectivity as needed.
  """
  # No allowed_tools list - agent has capabilities, not restrictions
```

**That's it.** No specialized classes, no tool allowlists, no pre-programmed behavior.

### 2.2 What Tools Are

**Tools are not abstractions - they're capabilities:**

| Tool                             | What It Does                             |
| -------------------------------- | ---------------------------------------- |
| `ssh_exec(host, command)`        | Execute shell commands on remote servers |
| `http_request(url, method, ...)` | Make HTTP calls to APIs                  |
| `send_email(to, subject, body)`  | Send notifications                       |
| `spawn_worker(task)`             | Delegate to disposable sub-agent         |

**Safety via capability boundaries:**

- `ssh_exec` validates host against allowlist (cube, clifford, zerg, slim)
- `send_email` validates from address (alerts@drose.io)
- File operations scoped to worker artifact directories

**No ToolRegistry needed.** Tools are imported code, not discovered services.

---

## 3. Implementation Status

### ✅ Phase A: Core Tools (COMPLETE)

| Tool               | Status |
| ------------------ | ------ |
| `get_current_time` | ✅     |
| `http_request`     | ✅     |
| `ssh_exec`         | ✅     |
| `spawn_worker`     | ✅     |
| `send_email`       | ✅     |

### ✅ Phase B: Reliability (COMPLETE)

| Feature             | Status                  |
| ------------------- | ----------------------- |
| Timeouts per tool   | ✅                      |
| Retry logic         | ✅                      |
| Error handling      | ✅                      |
| Tool execution logs | ✅ (saved to artifacts) |

### ✅ Phase C: MCP Integration (COMPLETE)

| Feature                                      | Status |
| -------------------------------------------- | ------ |
| MCP client adapter                           | ✅     |
| Custom MCP servers                           | ✅     |
| OAuth token management                       | ✅     |
| Preset servers (GitHub, Linear, Slack, etc.) | ✅     |

### 📋 Phase D: Canvas (OPTIONAL)

**v1.0 spec:** Complex multi-node system with Agent, Tool, Trigger, Condition node types

**v2.0 decision:** Canvas is optional visualization, not architectural requirement.

**If we build Canvas:**

- Single "Agent" node type
- Node shows: name, status, last run time
- Click to view: full thread history, tool calls, artifacts
- No separate Tool/Trigger/Condition nodes (unnecessary complexity)

**Why optional:**

- Agents work fine defined in code/API
- Canvas adds visual polish but not core capability
- Most users will use Jarvis (conversational), not Canvas (visual workflows)

---

## 4. What Changed from v1.0

### Removed Concepts

1. **ToolRegistry** ❌
   - v1.0: Central registry for discovering tools
   - v2.0: Tools are imported code, no discovery needed
   - Benefit: 200+ LOC simpler

2. **allowed_tools allowlist** ❌
   - v1.0: Per-agent tool whitelist (`allowed_tools: ["ssh", "http"]`)
   - v2.0: Agents have capabilities (which hosts, which APIs)
   - Benefit: Security via capability boundaries, not tool restrictions

3. **Multiple node types** ❌
   - v1.0: Agent node, Tool node, Trigger node, Condition node
   - v2.0: Just Agent node (if we build Canvas at all)
   - Benefit: Simpler implementation, clearer mental model

4. **"Progressive disclosure" nested canvas** ❌
   - v1.0: Click agent → see internal LLM/Tool timeline as nested graph
   - v2.0: Click agent → see thread history as chat transcript
   - Benefit: Users understand chat transcripts; graph adds complexity

### Kept Concepts

1. **Agent as autonomous unit** ✅
   - Agents execute independently
   - Have persistent threads for context
   - Make their own tool decisions

2. **Tool execution logging** ✅
   - All tool calls saved to disk artifacts
   - Full audit trail

3. **MCP integration** ✅
   - Connect to external tool providers
   - Standardized protocol

---

## 5. Current Usage Pattern

**How users create agents today:**

### Option 1: API (Primary Method)

```typescript
POST /api/agents
{
  "name": "Infrastructure Monitor",
  "model": "gpt-4o",
  "system_instructions": "You monitor infrastructure health...",
  "trigger": {
    "type": "schedule",
    "cron": "0 6 * * *"  // Daily at 6am
  }
}
```

### Option 2: Jarvis (Voice)

```
User: "Create a daily health check that runs every morning"
Jarvis → Supervisor → Creates agent via API
```

### Option 3: Dashboard (UI)

```
Zerg Dashboard → Agents → [+ Create Agent]
  Fill form: name, model, instructions, schedule
  Save
```

**Canvas (visual workflow builder) is optional future enhancement, not required for functionality.**

---

## 6. Testing Strategy

### Test Behaviors, Not Architecture

```python
# ✅ Good test: Can agent accomplish task?
async def test_can_check_server_health():
    """Can agent check server health when asked?"""

    mock_ssh('cube', /df -h/, "500G 78% used")
    mock_ssh('cube', /docker ps/, "12 containers running")

    result = await agent.execute("Check server health")

    assert "78%" in result
    assert "healthy" in result or "12 containers" in result

# ❌ Bad test: Does it use specific tools?
async def test_uses_ssh_exec_tool():
    """Tests implementation detail, not behavior."""

    result = await agent.execute("Check server health")

    # Who cares which tool it used? Test the outcome!
    assert agent.tool_calls[0].name == "ssh_exec"
```

---

## 7. Migration from v1.0

### Code to Remove

1. **ToolRegistry:**

   ```python
   # DELETE: zerg/tools/registry.py
   # DELETE: register_tool() decorator
   # DELETE: Tool discovery logic
   ```

2. **allowed_tools column:**

   ```python
   # DELETE: agents.allowed_tools column from schema
   # DELETE: Tool allowlist validation in AgentRunner
   ```

3. **Tool/Trigger/Condition node types:**
   ```typescript
   // DELETE: Canvas node types besides Agent
   // DELETE: Edge validation for different port types
   ```

### Code to Keep

1. **Tool implementations:**

   ```python
   # KEEP: ssh_exec, http_request, send_email
   # These are capabilities, not registry entries
   ```

2. **MCP adapter:**

   ```python
   # KEEP: MCP client integration
   # MCP tools available to agents naturally
   ```

3. **Tool execution logging:**
   ```python
   # KEEP: tool_calls/*.txt artifact saving
   # Audit trail is valuable
   ```

---

## 8. Canvas: If We Build It

**Decision:** Canvas is optional visualization layer, not core architecture.

**If we decide to build it:**

### Single Node Type Approach

```
┌────────────────────────┐
│  Infrastructure Check  │  ← Agent node
│  ⚙️ gpt-4o             │
│                        │
│  Status: ✓ Healthy     │
│  Last run: 2h ago      │
│  Cost: $0.03           │
│                        │
│  [View Thread]         │  ← Opens chat transcript
│  [Edit] [Run Now]      │
└────────────────────────┘
```

**What it shows:**

- Agent name
- Model in use
- Last run status
- Timing/cost

**What it does:**

- Click [View Thread] → See full conversation history
- Click [Edit] → Update system instructions
- Click [Run Now] → Trigger immediate execution

**No nested graph.** Thread history is shown as chat transcript (humans understand this).

### No Complex Workflow Composition

**v1.0 vision:**

```
Trigger → Agent A → Tool (format) → Agent B → Tool (email)
```

**v2.0 reality:**
Agents can do this themselves:

```
Agent with instructions:
  "Check infrastructure daily.
   If you find issues, format them clearly and send email to alerts@drose.io"

Agent autonomously:
  1. Spawns worker to check infrastructure
  2. Reads result
  3. Decides if it's an issue
  4. Formats email naturally
  5. Calls send_email
```

**No workflow composition UI needed.** LLM handles orchestration.

---

## 9. Design Principles

### Principle 1: Agents Are Code

Visual canvas is helpful for:

- ✅ Seeing what agents exist
- ✅ Checking their status
- ✅ Viewing past runs

Visual canvas is NOT needed for:

- ❌ Defining agent behavior (system instructions work fine)
- ❌ Composing workflows (LLMs orchestrate naturally)
- ❌ Tool selection (LLMs pick appropriate tools)

### Principle 2: Tools Are Capabilities

```python
# WRONG: Tool as abstraction
class DiskCheckTool:
    name = "check_disk"
    description = "Check disk usage"
    def execute(host):
        return ssh_exec(host, "df -h")

# RIGHT: Tool as capability
ssh_exec(host, command)  # General purpose, LLM decides command
```

### Principle 3: Canvas Is Visualization, Not Definition

```
Definition lives in: Database (agent records) or code (agent configs)
Canvas displays: Current state, past runs, artifacts
Canvas does NOT: Define behavior, enforce tool restrictions, manage workflows
```

---

_End of Specification v2.0_

**Summary:** Agents are code with system instructions. Tools are capabilities bounded by access control. Canvas (if built) is optional visualization layer showing agent status and history. ToolRegistry, allowed_tools allowlists, and complex node types removed. ~500+ LOC simpler, equally capable.
