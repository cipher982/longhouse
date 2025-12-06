# LangGraph Supervisor Pattern Research

**Date**: 2025-12-03
**Author**: Claude (Sonnet 4.5)
**Codebase**: Zerg Multi-Agent Platform

## Summary

The LangGraph supervisor pattern implements hierarchical multi-agent coordination where a supervisor agent delegates tasks to specialized worker agents. LangGraph provides `create_supervisor()` and `create_react_agent()` utilities for building this pattern. Zerg already implements similar concepts through its workflow engine with node executors, but uses a different architecture: LangGraph uses agent-to-agent handoffs while Zerg uses a centralized state graph with typed nodes. The supervisor pattern could enhance Zerg's agent coordination capabilities, but adoption would require significant architectural changes.

---

## What LangGraph Supervisor Provides

### Core Components

1. **`create_supervisor()` Function**
   - Creates a supervisor agent that routes tasks to worker agents
   - Takes a list of agent functions and a routing prompt
   - Handles task delegation based on LLM decision-making
   - Supports "handoff" messages for agent-to-agent communication
   - Output modes: `full_history` (complete conversation) or `final` (last result only)

2. **Agent Graph Construction**
   - Uses `StateGraph` to connect supervisor and worker nodes
   - Supervisor acts as a router with destinations list
   - Workers report back to supervisor after task completion
   - Supports both sequential and parallel execution patterns

3. **State Management**
   - Messages list tracks full conversation history
   - Workers inherit context from supervisor's routing decision
   - State updates flow bidirectionally (supervisor ↔ workers)

### Key Features

- **Dynamic Task Routing**: Supervisor uses LLM to decide which agent handles each subtask
- **Specialized Worker Agents**: Each worker focuses on a specific capability (research, math, etc.)
- **Conversation Context**: Full message history maintained across agent handoffs
- **Flexible Topologies**: Supports hierarchical teams (supervisor → workers → sub-workers)
- **ReAct Pattern**: Workers use tool-calling agents built with `create_react_agent()`

### Example Architecture Pattern

```python
from langgraph import StateGraph, create_react_agent
from langgraph_supervisor import create_supervisor
from langchain.chat_models import init_chat_model

# Define worker agents
def research_agent(state):
    # Research implementation
    return {"needs_search": True}

def math_agent(state):
    # Math implementation
    return {"needs_calculation": True}

# Create supervisor
supervisor_model = init_chat_model("openai:gpt-4.1")
supervisor = create_supervisor(
    model=supervisor_model,
    agents=[research_agent, math_agent],
    prompt=(
        "You are a supervisor managing two agents:\n"
        "- a research agent. Assign research-related tasks\n"
        "- a math agent. Assign math-related tasks\n"
        "Assign work to one agent at a time.\n"
    ),
    add_handoff_back_messages=True,
    output_mode="full_history",
).compile()

# Build graph
supervisor_graph = (
    StateGraph()
    .add_node(supervisor, destinations=("research_agent", "math_agent"))
    .add_node(research_agent)
    .add_node(math_agent)
    .add_edge("START", "supervisor")
    .add_edge("research_agent", "supervisor")  # Workers report back
    .add_edge("math_agent", "supervisor")
    .compile()
)

# Execute
for chunk in supervisor_graph.stream(user_query):
    process(chunk)
```

---

## Current Zerg Architecture

### Agent System (`/apps/zerg/backend/zerg/`)

#### 1. **Agent Definition** (`agents_def/zerg_react_agent.py`)

- **Pattern**: Single-agent ReAct loop (not multi-agent coordination)
- **Key Features**:
  - Functional API using `@entrypoint` decorator
  - Tool-calling with parallel execution (`asyncio.gather`)
  - Dynamic tool loading from MCP servers
  - Streaming token support via callbacks
  - Checkpointing with `MemorySaver`
- **Architecture**: Standalone agent executor, no supervisor/worker model

**Core Loop**:

```python
async def _agent_executor_async(messages, previous, enable_token_stream):
    current_messages = messages or previous or []
    llm_response = await _call_model_async(current_messages, enable_token_stream)

    # ReAct loop: call tools until model stops
    while isinstance(llm_response, AIMessage) and llm_response.tool_calls:
        coro_list = [_call_tool_async(tc) for tc in llm_response.tool_calls]
        tool_results = await asyncio.gather(*coro_list)
        current_messages = add_messages(current_messages, [llm_response] + tool_results)
        llm_response = await _call_model_async(current_messages, enable_token_stream)

    return add_messages(current_messages, [llm_response])
```

#### 2. **Agent Execution** (`managers/agent_runner.py`)

- **Pattern**: Single-threaded execution wrapper
- **Responsibilities**:
  - Persists agent turns to database
  - Manages context (credentials, streaming)
  - Injects connector status into system prompt
  - Handles usage tracking and cost calculation
- **Limitation**: One agent per thread, no multi-agent coordination

**Key Flow**:

```python
async def run_thread(self, db, thread):
    # Inject system context (connectors, protocols)
    original_msgs = get_thread_messages_as_langchain(db, thread.id)
    context_text = build_agent_context(db, owner_id, agent_id)

    # Execute agent runnable
    updated_messages = await self._runnable.ainvoke(original_msgs, config)

    # Persist new messages
    new_messages = updated_messages[messages_with_context:]
    save_new_messages(db, thread.id, new_messages)
```

#### 3. **Task Runner** (`services/task_runner.py`)

- **Pattern**: Single-agent task execution
- **Purpose**: Non-interactive "Play" button runs and scheduled tasks
- **Process**:
  1. Create fresh thread with system prompt
  2. Insert user message with `agent.task_instructions`
  3. Delegate to `AgentRunner`
  4. Update agent status and broadcast events
- **Limitation**: No multi-step orchestration or agent collaboration

#### 4. **Workflow Engine** (`services/workflow_engine.py`)

- **Pattern**: LangGraph StateGraph with typed node executors
- **Architecture**:
  - Centralized workflow orchestration (not agent-to-agent)
  - Node types: `agent`, `tool`, `trigger`, `conditional`
  - Each node executor is independent, no supervisor coordination
  - State flows through graph edges, not agent handoffs

**Key Differences from Supervisor Pattern**:

```python
class WorkflowState(TypedDict):
    node_outputs: Annotated[Dict[str, Any], merge_dicts]
    completed_nodes: Annotated[List[str], operator.add]
    error: Annotated[Union[str, None], first_error]
```

- **Zerg**: Central state dictionary shared across all nodes
- **LangGraph Supervisor**: Message-based agent-to-agent communication

**Workflow Execution**:

```python
def _build_langgraph(self, workflow_data, execution_id):
    graph = StateGraph(WorkflowState)

    # Add typed node executors (not agents)
    for node in workflow_data.nodes:
        executor = create_node_executor(node, self._publish_node_event)
        graph.add_node(node.id, executor.execute)

    # Connect nodes with edges (not agent routing)
    for edge in workflow_data.edges:
        graph.add_edge(edge.from_node_id, edge.to_node_id)
```

#### 5. **Node Executors** (`services/node_executors.py`)

- **Pattern**: Typed executor classes for each node type
- **Node Types**:
  - `AgentNodeExecutor`: Runs an agent via `AgentRunner`
  - `ToolNodeExecutor`: Executes a single tool
  - `ConditionalNodeExecutor`: Evaluates expressions for branching
  - `TriggerNodeExecutor`: Workflow initiation nodes
- **State Machine**: Each executor uses `ExecutionStateMachine` for phase transitions
- **Output Format**: Envelope pattern `{"value": ..., "meta": {...}}`

**Agent Node Example**:

```python
async def _execute_node_logic(self, db, state, execution_id):
    agent_id = resolved_config.get("agent_id")
    message = resolved_config.get("message", "Execute this task")

    # Create thread for agent execution
    thread = crud.create_thread(db, agent_id, title)
    crud.create_thread_message(db, thread.id, "user", message)

    # Execute via AgentRunner (not supervisor handoff)
    runner = AgentRunner(agent)
    created_messages = await runner.run_thread(db, thread)

    return create_agent_envelope(value={"messages": created_messages}, ...)
```

### Key Architectural Patterns

| Aspect                  | Zerg Current               | LangGraph Supervisor     |
| ----------------------- | -------------------------- | ------------------------ |
| **Coordination**        | Central workflow state     | Agent-to-agent handoffs  |
| **State Management**    | Typed `WorkflowState` dict | Message history list     |
| **Task Assignment**     | Pre-defined graph edges    | Dynamic LLM routing      |
| **Agent Communication** | Shared node_outputs        | Handoff messages         |
| **Execution Model**     | Node executor pattern      | ReAct agent pattern      |
| **Persistence**         | Database-backed threads    | In-memory checkpoints    |
| **Streaming**           | WebSocket events per node  | Stream chunks from graph |

---

## Gap Analysis

### What Zerg Has (Strengths)

1. **Rich Type System**
   - Strongly-typed node executors with envelope outputs
   - Database-backed execution state with phase/result tracking
   - Comprehensive error handling via `ExecutionStateMachine`

2. **Production-Ready Persistence**
   - All agent turns saved to PostgreSQL
   - Thread/message history for audit trail
   - Usage tracking and cost calculation

3. **Visual Workflow Editor**
   - Canvas-based workflow design (`WorkflowData` schema)
   - Conditional branching with expression evaluator
   - Variable resolution across nodes (`resolve_variables`)

4. **Real-Time Updates**
   - WebSocket event streaming for UI
   - Granular node state transitions
   - Event bus with pub/sub pattern

5. **Tool Integration**
   - Unified tool resolver with MCP support
   - Dynamic tool loading from external servers
   - Connector credentials management

### What Zerg Lacks (Gaps)

1. **Dynamic Task Routing**
   - Zerg: Pre-defined graph edges (static workflow)
   - Supervisor: LLM decides which agent handles each subtask
   - **Impact**: Can't adapt workflow based on task requirements

2. **Agent-to-Agent Communication**
   - Zerg: Agents don't communicate directly (share state dict)
   - Supervisor: Agents handoff tasks with context messages
   - **Impact**: Limited collaborative problem-solving

3. **Hierarchical Teams**
   - Zerg: Flat workflow graph (all nodes equal)
   - Supervisor: Nested supervisor → workers → sub-workers
   - **Impact**: Can't scale to complex multi-level orchestration

4. **Conversational Context**
   - Zerg: Each agent node creates isolated thread
   - Supervisor: Shared message history across agents
   - **Impact**: Agents can't build on each other's work conversationally

5. **Supervisor Decision-Making**
   - Zerg: Workflow designer hardcodes routing logic
   - Supervisor: LLM supervisor reasons about task delegation
   - **Impact**: Less flexible for open-ended problems

### Concrete Examples

**Scenario**: Research question requiring web search + data analysis + report writing

**Zerg Current Approach**:

```
[Trigger] → [Agent1: Research] → [Agent2: Analyze] → [Agent3: Write]
            (3 separate threads, no shared context)
```

**LangGraph Supervisor Approach**:

```
User Query → [Supervisor LLM]
                ↓
            "Research agent, find data"
                ↓
            [Research Agent] → Results
                ↓
            [Supervisor LLM]
                ↓
            "Analysis agent, process this: {results}"
                ↓
            [Analysis Agent] → Insights
                ↓
            [Supervisor LLM]
                ↓
            "Writer agent, create report: {insights}"
                ↓
            [Writer Agent] → Report
```

**Key Difference**: Supervisor maintains conversation context and adapts routing based on intermediate results.

---

## Recommended Actions

### Option 1: Hybrid Approach (Recommended)

**Integrate supervisor pattern into existing workflow nodes without replacing core architecture.**

**Implementation**:

1. Create new `SupervisorNodeExecutor` class
2. Node config specifies available worker agents
3. Supervisor delegates to workers via internal message passing
4. Workers are standard Zerg agents (use `AgentRunner`)
5. Output envelope contains routing decisions + results

**Benefits**:

- Preserves existing workflow engine and persistence
- Adds dynamic routing where needed
- Minimal breaking changes
- Gradual adoption path

**Pseudocode**:

```python
class SupervisorNodeExecutor(BaseNodeExecutor):
    async def _execute_node_logic(self, db, state, execution_id):
        # Get supervisor config
        worker_agent_ids = self.node.config.get("worker_agents")
        task_description = resolve_variables(self.node.config.get("task"))

        # Create supervisor prompt
        workers_desc = self._build_worker_descriptions(db, worker_agent_ids)
        supervisor_prompt = f"""
        You are coordinating these agents:
        {workers_desc}

        Task: {task_description}

        Decide which agent(s) to use and in what order.
        """

        # Execute supervisor loop
        messages = [HumanMessage(content=supervisor_prompt)]
        while True:
            # Supervisor decides next action
            decision = await self._call_supervisor_llm(messages)

            if decision.is_complete:
                break

            # Execute chosen worker agent
            worker_result = await self._execute_worker(
                db, decision.agent_id, decision.task
            )

            # Add result to conversation
            messages.append(AIMessage(content=decision.task))
            messages.append(HumanMessage(content=worker_result))

        return create_supervisor_envelope(
            value={
                "routing_decisions": self._extract_decisions(),
                "final_result": messages[-1].content
            },
            workers_used=[...],
            coordination_steps=len(messages)
        )
```

### Option 2: Parallel Implementation

**Build separate supervisor-based workflows alongside existing system.**

**Implementation**:

1. Create `SupervisorWorkflow` model separate from `Workflow`
2. New execution engine using LangGraph supervisor pattern
3. Different UI for supervisor workflows vs. canvas workflows
4. Both systems coexist, users choose appropriate tool

**Benefits**:

- No risk to existing workflows
- Experiment with supervisor pattern safely
- Learn from usage before deeper integration

**Drawbacks**:

- Maintenance burden of two systems
- User confusion about which to use
- Code duplication

### Option 3: Full Migration (Not Recommended)

**Replace workflow engine with LangGraph supervisor architecture.**

**Why Not**:

- Breaks all existing workflows (100+ hours of user-built automation)
- Loses database persistence and audit trail
- Loses visual workflow editor
- Supervisor pattern doesn't fit all use cases (scheduled tasks, webhooks)

### Short-Term Actions

1. **Prototype Supervisor Node** (1-2 days)
   - Implement basic `SupervisorNodeExecutor`
   - Hardcode 2-3 test worker agents
   - Validate routing decisions work correctly

2. **Study LangGraph Internals** (1 day)
   - Review `create_supervisor()` source code
   - Understand handoff message format
   - Test integration with Zerg's `AgentRunner`

3. **Design Node Config Schema** (1 day)
   - Define JSON schema for supervisor node config
   - Specify worker agent selection interface
   - Design routing strategy options (sequential, parallel, adaptive)

4. **Update Frontend Canvas** (2-3 days)
   - Add "Supervisor" node type to visual editor
   - UI for selecting worker agents
   - Display routing decisions in execution logs

### Long-Term Considerations

1. **Performance**: Supervisor adds LLM calls for routing decisions (latency + cost)
2. **Reliability**: Supervisor can make poor routing choices (needs safeguards)
3. **Observability**: Need detailed logging of routing decisions for debugging
4. **Testing**: Complex interaction patterns harder to unit test
5. **Documentation**: Users need guidance on when to use supervisor vs. static workflow

---

## Code Examples

### Zerg: Current Agent Execution

```python
# Single agent, no coordination
agent = crud.get_agent(db, agent_id=1)
runner = AgentRunner(agent)
messages = await runner.run_thread(db, thread)
```

### LangGraph: Supervisor Pattern

```python
# Multiple agents with supervisor coordination
supervisor = create_supervisor(
    model=llm,
    agents=[research_agent, analysis_agent, writer_agent],
    prompt="Coordinate these agents to answer user questions"
)

graph = StateGraph()
    .add_node(supervisor, destinations=("research", "analysis", "writer"))
    .add_node(research_agent)
    .add_node(analysis_agent)
    .add_node(writer_agent)
    .add_edge("START", "supervisor")
    .add_edge("research", "supervisor")
    .add_edge("analysis", "supervisor")
    .add_edge("writer", "supervisor")
    .compile()

for chunk in graph.stream({"messages": [user_query]}):
    # Process supervisor routing decisions and agent outputs
    handle_chunk(chunk)
```

### Proposed Zerg + Supervisor Hybrid

```python
# Workflow node that internally uses supervisor pattern
workflow = {
    "nodes": [
        {
            "id": "supervisor_1",
            "type": "supervisor",
            "config": {
                "worker_agents": [agent_1.id, agent_2.id, agent_3.id],
                "task": "Answer this research question: ${trigger.user_query}",
                "routing_strategy": "adaptive",  # or "sequential", "parallel"
                "max_steps": 10
            }
        }
    ],
    "edges": [
        {"from_node_id": "trigger", "to_node_id": "supervisor_1"}
    ]
}

# Execution uses existing workflow engine
execution_id = await workflow_engine.execute_workflow(workflow.id)

# SupervisorNodeExecutor handles internal agent coordination
# Output envelope contains routing decisions + final result
```

---

## References

- **LangGraph Documentation**: `https://langchain-ai.github.io/langgraph/` (redirects to docs.langchain.com)
- **Multi-Agent Tutorial**: Hierarchical Agent Teams pattern
- **Zerg Codebase**: `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/`
- **Key Files**:
  - `agents_def/zerg_react_agent.py` - ReAct agent implementation
  - `managers/agent_runner.py` - Single-agent execution wrapper
  - `services/workflow_engine.py` - LangGraph StateGraph orchestration
  - `services/node_executors.py` - Typed node execution handlers
  - `services/task_runner.py` - Non-interactive task execution

---

## Conclusion

The LangGraph supervisor pattern offers powerful dynamic task routing and agent collaboration capabilities that Zerg currently lacks. However, Zerg's existing workflow architecture provides superior persistence, observability, and production-readiness. **The hybrid approach (Option 1) is recommended**: implement supervisor pattern as a new node type within Zerg's existing workflow engine. This preserves current strengths while adding adaptive multi-agent coordination where it provides value. Start with a prototype supervisor node to validate the concept before broader integration.
