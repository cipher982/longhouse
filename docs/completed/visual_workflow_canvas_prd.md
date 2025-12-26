# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# PRD: Visual Workflow Canvas - Tool & Trigger Nodes

_Status: 🟡 IN PROGRESS - Phase 1 Complete, Phase 2+ Active_
_Owner: Development Team_
_Last Updated: 2025-06-15_

---

## Implementation Status (Auto-Tracked)

### Phase 1: Foundation (Weeks 1-2)

- [x] Extend NodeType enum for Tool nodes
- [x] Tool node rendering with basic styling
- [x] Tool palette with MCP integration (basic, static for now)
- [x] Drag-drop from palette to canvas
- [x] Tool and trigger node rendering
- [x] Hybrid tool visibility (internal/optional/external)
- [x] Canvas rendering for new node types
- [x] State management for new node types
- [x] Rust move/borrow errors resolved
- [x] Build compiles successfully

### Phase 2: Execution Engine (Current Focus)

- [x] Tool configuration modal opens (modal-based, not sidebar)
- [x] Tool configuration modal: input mapping (static values), save/cancel, state update via message dispatch
- [x] Agent configuration modal is robust
- [x] Canvas drag/drop and rendering is stable
- [x] Workflow execution history drawer implemented (June 2025)
- [x] Workflow cancellation support added
- [ ] **IN PROGRESS**: Trigger configuration modal: modal UI, parameter mapping
- [ ] **IN PROGRESS**: Complete workflow execution engine with live feedback
- [ ] **NEXT**: Templates and advanced UX features

---

## Implementation Notes

- All code changes are directly aligned with the requirements and user stories in this PRD.
- The node palette, drag-and-drop, and rendering logic are implemented as described in Epics 1 and 2.
- The hybrid approach for tool visibility is in place, allowing both internal and explicit tool nodes.
- The codebase changes (Rust/WASM frontend, state management, rendering, and palette) are consistent with the technical architecture and implementation roadmap in this PRD.
- Compilation errors and warnings have been resolved; the system is ready for further development and testing.
- The team has standardized on modal dialogs for all node configuration, ensuring a consistent and accessible UX. The sidebar approach was evaluated and rejected due to UX and technical drawbacks.

---

## Executive Summary

**Problem**: Users see Zerg as "another ChatGPT" because they can only chat with agents. The powerful MCP tool integrations and trigger system are hidden behind configuration modals.

**Solution**: Make tools and triggers first-class visual nodes on the canvas, enabling users to build "Zapier + AI" workflows by dragging and connecting components.

**Success Metric**: 80% of new users create their first automated workflow within 10 minutes.

**Key Differentiator**: Transform from "chat with AI" to "visual AI workflow automation platform"

---

## 1. Problem Statement

### Current State Pain Points

- **Hidden Capabilities**: MCP tools configured in modals, invisible during workflow creation
- **No Visual Automation**: Users can't see or build automated workflows
- **ChatGPT Comparison**: "This is just ChatGPT with extra configuration steps"
- **Complex Mental Model**: Users must understand agents, tools, and triggers separately
- **No Immediate Value**: Setup-heavy with unclear payoff

### User Journey Problems

1. User configures MCP tools in agent modal
2. User chats with agent hoping it uses the right tools
3. User has no visibility into what tools are available or how they're used
4. User doesn't understand how to build automation

### Competitive Landscape

- **ChatGPT**: Single conversation, no automation
- **Zapier**: Visual workflows, no AI reasoning
- **Make.com**: Complex automation, steep learning curve
- **Zerg Opportunity**: Visual AI workflows with immediate feedback

---

## 2. Solution Overview

### Vision Statement

Transform Zerg into a visual workflow automation platform where users drag, connect, and run AI-powered workflows that integrate with real-world services.

### Core Principle: "What You Draw Is What Runs"

Every visual element on the canvas maps 1-to-1 to runtime execution:

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│   Gmail     │ →  │ Summarize   │ →  │   Slack     │
│  Trigger    │    │   Agent     │    │   Tool      │
└─────────────┘    └─────────────┘    └─────────────┘
```

### Key Value Propositions

1. **Immediate Visual Feedback**: See all capabilities at a glance
2. **Real-world Integration**: Connect to actual services, not just chat
3. **Progressive Complexity**: Start simple, build sophisticated workflows
4. **Live Execution**: Watch workflows run in real-time
5. **Template Library**: Pre-built workflows for common use cases

---

## 3. User Stories & Acceptance Criteria

### Epic 1: Tool Nodes on Canvas

**As a user, I want to see available tools as draggable nodes so I can build workflows visually.**

#### Story 1.1: Tool Palette

```
GIVEN I'm on the canvas editor
WHEN I open the node palette
THEN I see organized tool categories:
- 🤖 Agents (existing)
- 🔧 Tools (NEW)
  - Built-in Tools (HTTP, Math, DateTime)
  - GitHub Tools (if MCP connected)
  - Slack Tools (if MCP connected)
  - Linear Tools (if MCP connected)
  - Custom MCP Tools
- ⚡ Triggers (NEW)
- 🔀 Logic (future: conditions, loops)
```

**Acceptance Criteria:**

- [ ] Tool palette shows all available tools from MCP registry
- [ ] Tools grouped by source with clear visual hierarchy
- [ ] Unavailable tools grayed out with "Connect [Service]" hint
- [ ] Search/filter functionality for large tool sets
- [ ] Drag-drop from palette to canvas creates tool node

#### Story 1.2: Tool Node Rendering

```
GIVEN I drag "GitHub Create Issue" tool to canvas
WHEN the node is created
THEN I see:
- Distinctive tool icon (🔧) and readable name
- Source badge ("GitHub" with service color)
- Connection status (✅ Connected / ❌ Disconnected / ⚠️ Slow)
- Input ports (title, body, labels)
- Output ports (issue_url, issue_number)
```

**Acceptance Criteria:**

- [ ] Tool nodes visually distinct from Agent nodes
- [ ] Connection status always visible and accurate
- [ ] Hover tooltip shows tool description and requirements
- [ ] Input/output ports clearly labeled
- [ ] Node resizes based on port count

#### Story 1.3: Tool Configuration Panel

```
GIVEN I click on a Tool node
WHEN the configuration panel opens
THEN I can:
- See tool description and documentation link
- Configure required inputs (static values or node connections)
- Map inputs to outputs from previous nodes
- Set optional parameters
- Test tool execution with sample data
- View execution history for this node
```

**Acceptance Criteria:**

- [x] Modal opens for tool nodes (not sidebar)
- [x] Minimal input mapping UI (static values, placeholder for node outputs)
- [x] Save/cancel logic, state update via message dispatch
- [ ] Advanced input mapping to outputs from previous nodes (future)
- [ ] Test tool execution with sample data (future)
- [ ] Execution history for this node (future)
- [ ] Form validation with clear error messages (future)
- [ ] Unsaved changes warning (future)

### Epic 2: Trigger Nodes on Canvas

**As a user, I want to create triggers visually so I can start automated workflows.**

#### Story 2.1: Webhook Trigger Node

```
GIVEN I drag "Webhook Trigger" to canvas
WHEN I configure it
THEN I get:
- Auto-generated unique webhook URL (copyable)
- HMAC secret for security (auto-generated, copyable)
- Event filtering options (JSON path filters)
- Payload transformation settings
- Test webhook functionality
```

**Acceptance Criteria:**

- [ ] Webhook URL generated immediately and copyable
- [ ] HMAC secret shown with copy button and regenerate option
- [ ] JSON payload preview and filtering UI
- [ ] "Send Test Webhook" button for validation
- [ ] Webhook activity log (last 10 requests)

#### Story 2.2: Schedule Trigger Node

```
GIVEN I drag "Schedule Trigger" to canvas
WHEN I configure it
THEN I can:
- Set cron expression with visual builder
- See next 5 execution times preview
- Enable/disable schedule with toggle
- Set timezone for execution
- Manually trigger for testing
```

**Acceptance Criteria:**

- [ ] Visual cron builder with common presets (hourly, daily, weekly)
- [ ] Next execution times preview updates in real-time
- [ ] Timezone selector with user's timezone as default
- [ ] "Run Now" button for manual testing
- [ ] Schedule history (last 10 executions)

#### Story 2.3: Email Trigger Node (Gmail Integration)

```
GIVEN I have Gmail connected via MCP
WHEN I drag "Gmail Trigger" to canvas
THEN I can:
- Set email filters (sender, subject, labels)
- Choose trigger frequency (real-time vs polling)
- Preview matching emails
- Test trigger with recent emails
```

**Acceptance Criteria:**

- [ ] Gmail connection status indicator
- [ ] Email filter builder with AND/OR logic
- [ ] Preview of emails matching current filters
- [ ] "Test with Recent Email" functionality
- [ ] Trigger activity log

### Epic 3: Workflow Execution & Feedback

**As a user, I want to see my workflow running in real-time so I understand what's happening.**

#### Story 3.1: Live Execution Visualization

```
GIVEN I have a connected workflow (Trigger → Agent → Tool)
WHEN the workflow executes
THEN I see:
- Current executing node highlighted with animation
- Data flowing along connection lines
- Progress indicators on each node (queued/running/complete/failed)
- Real-time execution log in side panel
- Success/failure states with error details
```

**Acceptance Criteria:**

- [ ] Active node has animated border/glow effect
- [ ] Connection lines show data flow animation
- [ ] Node states: idle/queued/running/success/failed with icons
- [ ] Execution log panel with timestamps and details
- [ ] Failed nodes show error message on hover
- [ ] Execution completes with summary notification

#### Story 3.2: Execution History & Debugging

```
GIVEN my workflow has run multiple times
WHEN I click "Execution History"
THEN I see:
- Chronological list of all executions
- Success/failure status with duration
- Trigger source and input data
- Step-by-step execution details
- Input/output data for each node
- Error logs and stack traces
```

**Acceptance Criteria:**

- [ ] Execution history panel with pagination
- [ ] Filter by date range, status, trigger type
- [ ] Click execution to see detailed step-by-step flow
- [ ] Export execution data as JSON/CSV
- [ ] Search functionality across execution logs

#### Story 3.3: Workflow Templates & Examples

```
GIVEN I'm a new user
WHEN I visit the canvas
THEN I see:
- Template gallery with common workflows
- One-click template deployment
- Customization guidance for templates
- Community-shared workflows (future)
```

**Acceptance Criteria:**

- [ ] Template gallery with categories (productivity, development, marketing)
- [ ] Template preview with description and required connections
- [ ] "Use Template" button creates workflow copy
- [ ] Template customization wizard for required settings
- [ ] Template rating and usage statistics

---

## Progress Summary (2025-06-06)

- Tool configuration modal is now robust, minimal, and fully integrated with the state management system.
- All state changes are handled via message dispatching, with no direct state mutation in UI code.
- The modal is ready for incremental improvements (advanced mapping, validation, test, etc.).
- Trigger modal, workflow execution, and templates remain as next major milestones.

## 4. Technical Architecture

### Frontend (Rust/WASM) Changes

#### Enhanced Node System

```rust
// Extend existing node types
#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum NodeType {
    Agent { agent_id: u32 },
    Tool {
        tool_name: String,
        server_name: String,
        config: ToolConfig
    },
    Trigger {
        // Removed legacy: trigger_type. Use typed config.trigger in NodeConfig.
        config: TriggerConfig
    },
    // Future: Condition, Loop, etc.
}

#[derive(Debug, Clone)]
pub struct ToolConfig {
    pub inputs: HashMap<String, InputValue>,
    pub static_params: HashMap<String, serde_json::Value>,
}

#[derive(Debug, Clone)]
pub enum InputValue {
    Static(serde_json::Value),
    FromNode { node_id: String, output_key: String },
}
```

#### Node Palette Component

```rust
pub struct NodePalette {
    pub categories: Vec<PaletteCategory>,
    pub search_query: String,
    pub is_open: bool,
}

pub struct PaletteCategory {
    pub name: String,
    pub icon: String,
    pub nodes: Vec<PaletteNode>,
    pub is_expanded: bool,
}

impl NodePalette {
    pub fn render(&self, document: &Document) -> Result<(), JsValue>;
    pub fn handle_drag_start(&mut self, node: &PaletteNode);
    pub fn filter_by_search(&mut self, query: &str);
}
```

#### Workflow Execution State

```rust
#[derive(Debug, Clone)]
pub struct WorkflowExecution {
    pub id: String,
    pub status: ExecutionStatus,
    pub current_node: Option<String>,
    pub node_states: HashMap<String, NodeExecutionState>,
    pub execution_log: Vec<ExecutionLogEntry>,
    pub started_at: Option<u64>,
    pub completed_at: Option<u64>,
}

#[derive(Debug, Clone)]
pub enum NodeExecutionState {
    Idle,
    Queued,
    Running { progress: f32 },
    Success { output: serde_json::Value },
    Failed { error: String },
}
```

### Backend (Python/FastAPI) Changes

#### Workflow Engine

```python
class WorkflowEngine:
    """Orchestrates execution of visual workflows"""

    async def execute_workflow(
        self,
        workflow: Workflow,
        trigger_data: dict,
        execution_id: str
    ) -> WorkflowExecution:
        """Execute complete workflow from trigger through all nodes"""

    async def execute_node(
        self,
        node: WorkflowNode,
        input_data: dict,
        execution_context: ExecutionContext
    ) -> NodeResult:
        """Execute single node with proper error handling and logging"""

    async def resolve_node_inputs(
        self,
        node: WorkflowNode,
        execution_context: ExecutionContext
    ) -> dict:
        """Resolve input mappings from previous node outputs"""
```

#### Enhanced Tool Integration

```python
class VisualToolExecutor:
    """Executes tools from visual workflow nodes"""

    async def execute_tool_node(
        self,
        tool_name: str,
        server_name: str,
        inputs: dict,
        execution_context: ExecutionContext
    ) -> ToolResult:
        # Leverage existing MCP integration
        # Add visual workflow context
        # Enhanced error handling and logging
        # Execution time tracking
```

#### Workflow Storage Schema

```python
class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    canvas_data = Column(JSON)  # Nodes, connections, layout
    owner_id = Column(Integer, ForeignKey("users.id"))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=func.now())

class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"

    id = Column(String, primary_key=True)  # UUID
    workflow_id = Column(Integer, ForeignKey("workflows.id"))
    # Removed legacy flattened columns. Triggers are encoded in workflow.canvas JSON
    # as typed config.trigger for trigger nodes.
    status = Column(Enum(ExecutionStatus))
    node_states = Column(JSON)
    execution_log = Column(JSON)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    error_message = Column(Text)

class WorkflowNode(Base):
    __tablename__ = "workflow_nodes"

    id = Column(String, primary_key=True)  # UUID
    workflow_id = Column(Integer, ForeignKey("workflows.id"))
    node_type = Column(String)  # 'agent', 'tool', 'trigger'
    config = Column(JSON)
    position_x = Column(Float)
    position_y = Column(Float)
```

---

## 5. User Experience Design

### Visual Design Principles

#### Node Visual Hierarchy

```
🤖 Agent Nodes: Rounded rectangles, blue gradient, brain icon
🔧 Tool Nodes: Sharp rectangles, service-color accent, tool icon
⚡ Trigger Nodes: Diamond shape, green gradient, lightning icon
🔀 Logic Nodes: Hexagon, purple gradient, logic icon (future)
```

#### Connection Lines

- **Data Flow**: Animated dots moving along connections during execution
- **Connection Types**: Different line styles for data vs control flow
- **Status Colors**: Green (success), red (error), yellow (warning), gray (idle)

#### Execution Feedback

- **Node Glow**: Animated border during execution
- **Progress Bars**: For long-running operations
- **Status Icons**: ✅ ❌ ⚠️ ⏳ overlaid on nodes
- **Execution Path**: Highlight the path taken through the workflow

### Responsive Design

- **Desktop**: Full canvas with side panels
- **Tablet**: Collapsible panels, touch-friendly nodes
- **Mobile**: Simplified view, execution monitoring only

### Accessibility

- **Keyboard Navigation**: Tab through nodes, arrow keys to navigate canvas
- **Screen Reader**: Proper ARIA labels for all interactive elements
- **High Contrast**: Alternative color scheme for visual impairments
- **Focus Indicators**: Clear focus rings on all interactive elements

---

## 6. Success Metrics & KPIs

### Primary Success Metrics

#### User Adoption

- **Workflow Creation Rate**: 80% of users create first workflow within 10 minutes
- **Tool Usage**: 60% of workflows use 2+ different tools/services
- **Template Adoption**: 40% of new workflows start from templates
- **User Retention**: 70% of workflow creators return within 7 days

#### Product Engagement

- **Workflow Complexity**: Average 3.5 nodes per workflow
- **Execution Success**: 90% of workflows execute without errors
- **Daily Active Workflows**: 50% of created workflows run at least daily
- **Service Integrations**: Average 2.3 MCP services connected per user

### Secondary Metrics

#### User Experience

- **Time to First Success**: <5 minutes from signup to working workflow
- **Support Ticket Reduction**: 80% fewer "how do I use tools" questions
- **User Satisfaction**: >4.5/5 rating on workflow building experience
- **Demo Conversion**: Prospects understand value in <3 minutes

#### Technical Performance

- **Execution Latency**: <2 seconds for simple workflows
- **System Reliability**: 99.5% uptime for workflow execution
- **Error Recovery**: 95% of failed workflows auto-retry successfully
- **Canvas Performance**: Smooth interaction with 50+ nodes

### Qualitative Success Indicators

- User feedback: "Now I see how this is different from ChatGPT"
- Sales feedback: "Demos are much more compelling"
- Support feedback: "Users understand the platform immediately"
- Community: Users sharing workflow templates and use cases

---

## 7. Implementation Roadmap

### Phase 1: Foundation (Weeks 1-2)

**Goal**: Basic tool and trigger nodes on canvas

#### Week 1: Tool Nodes

- [x] Extend NodeType enum for Tool nodes
- [x] Tool node rendering with basic styling
- [x] Tool palette with MCP integration
- [x] Drag-drop from palette to canvas
- [x] Tool configuration modal (basic, modal-based)

#### Week 2: Trigger Nodes

- [ ] Complete trigger frontend modal (existing M1 milestone)
- [ ] Trigger node rendering and configuration
- [ ] Webhook trigger with URL generation
- [ ] Schedule trigger with cron builder
- [ ] Basic trigger testing functionality

**Success Criteria**: Users can drag tools and triggers onto canvas and configure them

### Phase 2: Workflow Execution (Weeks 3-4)

**Goal**: Workflows actually run and show live feedback

#### Week 3: Execution Engine

- [ ] Workflow execution backend service
- [ ] Node input/output resolution
- [ ] Tool node execution integration
- [ ] Basic error handling and logging
- [ ] WebSocket execution status updates

#### Week 4: Visual Feedback

- [ ] Live execution status on canvas nodes
- [ ] Execution flow animation along connections
- [ ] Execution history panel
- [ ] Error display and debugging tools
- [ ] Execution log with detailed information

**Success Criteria**: Users can run workflows and see real-time execution feedback

### Phase 3: Polish & Templates (Weeks 5-6)

**Goal**: Production-ready experience with templates

#### Week 5: User Experience Polish

- [ ] Node palette search and filtering
- [ ] Improved tool configuration UX
- [ ] Workflow save/load functionality
- [ ] Canvas performance optimization
- [ ] Mobile-responsive design

#### Week 6: Templates & Examples

- [ ] Template system architecture
- [ ] 5 pre-built workflow templates
- [ ] Template gallery UI
- [ ] Template customization wizard
- [ ] Documentation and tutorials

**Success Criteria**: New users can successfully create workflows using templates

### Phase 4: Advanced Features (Weeks 7-8)

**Goal**: Power user features and scalability

#### Week 7: Advanced Workflow Features

- [ ] Workflow versioning and history
- [ ] Bulk operations and batch processing
- [ ] Conditional logic nodes (basic)
- [ ] Workflow sharing and collaboration
- [ ] Advanced debugging tools

#### Week 8: Performance & Scale

- [ ] Canvas optimization for large workflows
- [ ] Execution engine performance tuning
- [ ] Advanced error recovery
- [ ] Monitoring and observability
- [ ] Load testing and optimization

**Success Criteria**: Platform handles complex workflows and multiple concurrent users

---

## 8. Risk Assessment & Mitigation

### Technical Risks

#### Canvas Performance with Many Nodes

- **Risk**: Canvas becomes slow with 50+ nodes
- **Likelihood**: Medium
- **Impact**: High (user experience degradation)
- **Mitigation**:
  - Implement virtual scrolling for large canvases
  - Optimize rendering with canvas-based approach
  - Add node grouping/folding functionality
  - Performance testing with large workflows

#### Workflow Execution Reliability

- **Risk**: Complex workflows fail unpredictably
- **Likelihood**: Medium
- **Impact**: High (user trust and adoption)
- **Mitigation**:
  - Comprehensive error handling and retry logic
  - Workflow validation before execution
  - Execution sandboxing and timeouts
  - Detailed logging and debugging tools

#### MCP Integration Stability

- **Risk**: External MCP servers cause workflow failures
- **Likelihood**: High
- **Impact**: Medium (specific tool failures)
- **Mitigation**:
  - Graceful degradation for failed tools
  - Health monitoring for MCP connections
  - Fallback mechanisms and error recovery
  - Clear user communication about external dependencies

### User Experience Risks

#### Learning Curve Too Steep

- **Risk**: Users find visual workflow building complex
- **Likelihood**: Medium
- **Impact**: High (adoption failure)
- **Mitigation**:
  - Progressive disclosure of advanced features
  - Comprehensive template library
  - Interactive onboarding tutorial
  - Clear visual feedback and error messages

#### Feature Discoverability

- **Risk**: Users don't discover powerful features
- **Likelihood**: Medium
- **Impact**: Medium (underutilization)
- **Mitigation**:
  - Guided tours and feature highlights
  - Contextual help and tooltips
  - Template gallery showcasing capabilities
  - In-app feature announcements

### Business Risks

#### Competitive Response

- **Risk**: Competitors copy visual workflow approach
- **Likelihood**: High
- **Impact**: Medium (market differentiation)
- **Mitigation**:
  - Focus on execution quality and user experience
  - Build strong ecosystem of integrations
  - Continuous innovation and feature development
  - Strong brand and community building

#### Support and Documentation Load

- **Risk**: Complex features increase support burden
- **Likelihood**: Medium
- **Impact**: Medium (operational costs)
- **Mitigation**:
  - Comprehensive self-service documentation
  - In-app help and guidance
  - Community forums and user-generated content
  - Proactive user education and onboarding

---

## 9. Future Enhancements

### Phase 2 Features (Months 2-3)

#### Advanced Logic Nodes

- **Condition Nodes**: If/then/else branching logic
- **Loop Nodes**: Iterate over arrays and datasets
- **Switch Nodes**: Multi-way branching based on values
- **Merge Nodes**: Combine multiple workflow paths

#### Workflow Collaboration

- **Shared Workflows**: Team access and permissions
- **Version Control**: Track changes and rollback capability
- **Comments and Annotations**: Collaborative workflow documentation
- **Approval Workflows**: Require approval before execution

#### Advanced Integrations

- **Database Nodes**: Direct SQL query execution
- **API Builder**: Custom REST API endpoint creation
- **File Processing**: Upload, transform, and download files
- **Notification Channels**: SMS, push notifications, webhooks

### Phase 3 Features (Months 4-6)

#### Workflow Marketplace

- **Community Templates**: User-contributed workflows
- **Template Ratings**: Community-driven quality assessment
- **Template Categories**: Organized by use case and industry
- **Premium Templates**: Professional workflow templates

#### Advanced Analytics

- **Workflow Performance**: Execution time and success rate analytics
- **Cost Tracking**: Monitor API usage and costs per workflow
- **Usage Insights**: Identify most valuable workflows and tools
- **Optimization Suggestions**: AI-powered workflow improvement recommendations

#### Enterprise Features

- **Single Sign-On**: SAML/OIDC integration
- **Audit Logging**: Comprehensive activity tracking
- **Role-Based Access**: Granular permissions and access control
- **On-Premise Deployment**: Self-hosted option for enterprise customers

---

## 10. Appendix

### Competitive Analysis

#### Zapier

- **Strengths**: Large integration ecosystem, simple trigger-action model
- **Weaknesses**: No AI reasoning, limited data transformation
- **Zerg Advantage**: AI agents provide intelligent decision-making and data processing

#### Make.com (formerly Integromat)

- **Strengths**: Visual workflow builder, advanced logic capabilities
- **Weaknesses**: Complex interface, steep learning curve, no AI
- **Zerg Advantage**: AI-powered nodes simplify complex logic, better user experience

#### Microsoft Power Automate

- **Strengths**: Deep Microsoft ecosystem integration, enterprise features
- **Weaknesses**: Limited to Microsoft ecosystem, complex pricing
- **Zerg Advantage**: Open ecosystem, AI-first approach, simpler pricing

#### n8n

- **Strengths**: Open source, self-hosted option, developer-friendly
- **Weaknesses**: Technical setup required, limited AI capabilities
- **Zerg Advantage**: Hosted solution, AI-native design, visual simplicity

### Technical Specifications

#### Canvas Rendering Performance

- **Target**: 60 FPS interaction with 100+ nodes
- **Approach**: Canvas-based rendering with viewport culling
- **Optimization**: Only render visible nodes, use efficient hit detection

#### Workflow Execution Scalability

- **Target**: 1000+ concurrent workflow executions
- **Approach**: Async execution with proper resource management
- **Monitoring**: Execution queue depth, memory usage, response times

#### Data Flow Architecture

```
Trigger Event → Workflow Engine → Node Executor → Tool/Agent → Result Storage
     ↓              ↓                ↓              ↓           ↓
WebSocket ← Status Updates ← Execution Log ← Progress ← Output Data
```

### User Research Insights

#### Target User Personas

**"Automation Andy" - Operations Manager**

- Wants to automate repetitive business processes
- Comfortable with tools like Zapier but wants AI capabilities
- Values reliability and clear error reporting
- Success metric: Hours saved per week through automation

**"Developer Dana" - Technical Product Manager**

- Builds workflows for team productivity
- Wants to integrate custom APIs and internal tools
- Values flexibility and extensibility
- Success metric: Team velocity improvement

**"Marketing Mary" - Digital Marketing Manager**

- Automates content creation and distribution workflows
- Wants AI to help with content optimization
- Values templates and ease of use
- Success metric: Campaign performance improvement

#### Key User Feedback Themes

1. **"Show me what's possible"** - Users want to see capabilities upfront
2. **"Make it work reliably"** - Reliability more important than features
3. **"Help me get started"** - Templates and examples are crucial
4. **"Let me customize everything"** - Power users want full control

---

## Conclusion

This PRD transforms Zerg from a "ChatGPT alternative" into a unique "Visual AI Workflow Platform" that combines the best of automation tools (Zapier) with AI reasoning capabilities.

The visual canvas approach makes AI capabilities immediately discoverable and usable, while the workflow execution engine enables real-world automation that goes far beyond simple chat interactions.

Success depends on excellent execution of the visual experience, reliable workflow execution, and comprehensive templates that demonstrate the platform's unique value proposition.

**Next Steps**: Review and approve this PRD, then begin Phase 1 implementation with tool nodes on the canvas.
