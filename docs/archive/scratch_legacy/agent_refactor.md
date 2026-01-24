# Refactoring the Zerg LangGraph Agent Manager

**Document Purpose:** To summarize the challenges encountered with the current `AgentManager` implementation, outline the lessons learned, and detail the proposed refactoring plan for improved clarity, maintainability, and robustness.

**Authors:** [Your Name/Team], [Date]

**Context:**

The Zerg Agent Platform utilizes LangGraph to orchestrate the behavior of AI agents. The `AgentManager` class is currently the central component responsible for handling agent interactions, managing conversational state (threads and messages) via database persistence, and processing agent runs based on user input or scheduled tasks. While functional, the current implementation has accumulated significant complexity, hindering further development and maintenance. This document outlines a plan to refactor it based on recent insights and best practices.

---

## 1. Problem Statement: Challenges with the Current `AgentManager`

Through recent development and analysis, several key challenges have been identified in the existing `AgentManager` implementation:

1.  **`StateGraph` Over-Complexity for ReAct:** The use of LangGraph's `StateGraph` API, while powerful for complex state machines, resulted in a relatively verbose and less intuitive implementation for the common ReAct (Reason-Act) agent pattern. Defining nodes (`_chatbot_node`, `_call_tool_node`) and conditional edges (`_decide_next_step`) added boilerplate that obscured the fundamentally linear flow of the ReAct loop (LLM -> Tool -> LLM -> ...).
2.  **Monolithic `process_thread` Function:** The core `process_thread` method became responsible for too many distinct concerns:
    *   Loading thread state (messages) from the database.
    *   Executing the LangGraph agent logic.
    *   Manually accumulating partial results (`assistant_content`, `tool_output_map`) specifically for persistence *during* streaming.
    *   Handling confusing dual streaming modes (`stream` vs. `token_stream`).
    *   Interleaving database persistence logic (saving messages) with the agent execution flow.
    *   Marking messages as processed.
3.  **Fragile and Implicit State Management:**
    *   The `get_or_create_thread` function indicated uncertainty about thread existence. Implicitly creating threads when retrieval failed masked potential upstream issues or incorrect `thread_id` usage.
    *   The `_add_system_message_if_missing` function acted as a defensive patch, suggesting that the core thread creation process might not reliably include the essential system prompt, leading to potential inconsistencies. Correct state should be guaranteed, not patched after the fact.
4.  **Streaming Logic Burden:** Token-level streaming, primarily a user interface enhancement for perceived responsiveness, significantly complicated the backend agent execution logic. Handling low-level `astream_events` (like `on_chat_model_stream`) within the main `process_thread` loop added noise and coupled the core agent execution tightly to presentation concerns.
5.  **Violation of Single Responsibility Principle (SRP):** The single `agent_manager.py` file accumulated responsibilities for:
    *   **Agent Definition:** Defining the agent's tools, LLM, and core reasoning logic (graph nodes/edges or tasks/entrypoint).
    *   **Persistence Logic:** Handling database interactions (CRUD operations for threads/messages, data format conversions).
    *   **Execution Orchestration:** Running the agent, managing its lifecycle for a given request, and handling side effects like streaming callbacks.

---

## 2. Lessons Learned

Our analysis and experimentation highlighted several key principles for building more robust and maintainable agent systems:

1.  **Choose the Right API for the Job:** For linear or logic-driven agent flows like ReAct, LangGraph's **Functional API** (`@task`, `@entrypoint`) offers better readability and simpler control flow compared to the `StateGraph` API.
2.  **Prioritize Explicitness in State Management:** Thread creation and retrieval should be deterministic and explicit. Avoid functions that guess or implicitly fix state. Ensure foundational elements (like system messages) are part of the atomic creation process.
3.  **Decouple Presentation Concerns:** UI features like token streaming should not dictate the core backend agent's structure. The agent logic should ideally operate on complete steps/results, while a separate orchestration layer handles mapping execution events to UI streams.
4.  **Separate Persistence from Execution:** Database operations (loading initial state, saving final state) should generally bookend the core agent execution logic, rather than being interwoven within it. This promotes clearer logic and easier transaction management.
5.  **Embrace Separation of Concerns (Modularity):** Breaking down functionality into distinct modules based on responsibility (Agent Definition, Persistence, Orchestration) is crucial for managing complexity, improving testability, and facilitating parallel development.

---

## 3. Proposed Architecture & Refactoring Plan

We propose refactoring the `AgentManager` functionality into a more modular and explicit architecture, guided by the lessons learned.

**Core Principles:**

*   **Adopt LangGraph Functional API:** Redefine the agent logic using `@task` and `@entrypoint`.
*   **Strict Separation of Concerns:** Implement distinct modules for Agent Definition, Persistence Services, and Execution Orchestration.
*   **Decoupled Streaming:** Isolate token streaming logic within the Execution Orchestrator, invoked via callbacks.
*   **Explicit State Management:** Implement reliable, separate functions for thread creation (guaranteeing system message) and retrieval (validating existence/ownership).

**New Module Structure:**

1.  **Agent Definition (`agents/`)**: Contains the "pure" agent logic.
2.  **Persistence Service (`services/`)**: Handles all database interactions for threads/messages.
3.  **Execution Runner/Orchestrator (`managers/`)**: Coordinates the agent run, connecting definition with persistence and handling runtime concerns like streaming.

---

## 4. Detailed Component Breakdown

Here's a breakdown of the responsibilities for each proposed component:

**A. Agent Definition Module (e.g., `agents/zerg_react_agent.py`)**

*   **Primary Responsibility:** Define the cognitive logic of the agent.
*   **Key Contents:**
    *   `@tool` definitions (e.g., `get_current_time`).
    *   LLM instance configuration (`ChatOpenAI`).
    *   `@task` functions for distinct agent actions (e.g., `call_model_task`, `call_tool_task`).
    *   `@entrypoint` function defining the core agent loop (e.g., `react_agent_entrypoint`).
    *   A factory function/method to return the compiled, runnable agent (e.g., `get_agent_runnable()`).
*   **Goal:** Encapsulate *how the agent thinks*. It should be testable in isolation with mock inputs/outputs and have no direct knowledge of the database or UI streaming.
*   **Dependencies:** LangChain Core, LangGraph, LLM provider library (e.g., `langchain-openai`).

**B. Persistence Service Module (e.g., `services/thread_service.py`)**

*   **Primary Responsibility:** Abstract database operations related to conversation state.
*   **Key Contents:**
    *   Functions wrapping `crud` operations:
        *   `create_thread_with_system_message(db, agent_model, title, type)`
        *   `get_valid_thread_for_agent(db, thread_id, agent_id)`
        *   `get_thread_messages_as_langchain(db, thread_id)`
        *   `save_new_messages(db, thread_id, messages: List[BaseMessage])`
        *   `mark_messages_processed(db, message_ids: List[int])`
        *   `update_thread_timestamp(db, thread_id)`
    *   Message conversion utilities (`_db_to_langchain_message`, `_langchain_to_db_kwargs`).
*   **Goal:** Provide a clean API for managing thread/message state persistence. Isolates database schema details and `crud` interactions.
*   **Dependencies:** Database models (`zerg.models`), CRUD layer (`zerg.crud`), SQLAlchemy Session, LangChain Core Messages.

**C. Execution Runner Module (e.g., `managers/agent_runner.py`)**

*   **Primary Responsibility:** Orchestrate the end-to-end execution of an agent run for a specific thread/request.
*   **Key Contents:**
    *   Class `AgentRunner` (or similar structure).
    *   Methods like `process_thread(db, thread, stream_callback=None)` and `execute_task(...)`.
    *   Initialization takes dependencies (e.g., agent runnable, thread service).
    *   Logic to:
        *   Use `ThreadService` to load initial state (messages).
        *   Invoke the `agent_runnable.astream_events(...)`.
        *   Process events from `astream_events`.
        *   If a `stream_callback` is provided, identify token stream events (`on_chat_model_stream`) and invoke the callback.
        *   Capture the final state/message list from the agent run.
        *   Use `ThreadService` to persist new messages and update thread status *after* the run completes.
*   **Goal:** Act as the "glue" connecting the agent's brain (definition) with its memory (persistence) and handling runtime execution details, including optional UI streaming.
*   **Dependencies:** Agent Definition module, Persistence Service module, LangGraph, SQLAlchemy Session, `asyncio`.

---

## 5. Example Workflow (Processing a User Message)

1.  **Request:** An API endpoint receives a user message for agent `X` and thread `Y`.
2.  **Runner Invocation:** The endpoint instantiates `AgentRunner` (injecting dependencies) and calls `runner.process_thread(db, thread_id=Y, agent_id=X, stream_callback=websocket_sender)`.
3.  **Load State:** `AgentRunner` calls `thread_service.get_valid_thread_for_agent(db, Y, X)` and `thread_service.get_thread_messages_as_langchain(db, Y)`.
4.  **Execute Agent:** `AgentRunner` invokes `agent_runnable.astream_events(initial_messages, config=...)`.
5.  **Stream Handling:** As `astream_events` yields events:
    *   If an `on_chat_model_stream` event occurs, `AgentRunner` extracts the token and calls `await websocket_sender(token)`.
    *   Other events (tool calls/results) might be logged or sent via the callback for UI progress indication.
6.  **Capture Final State:** `AgentRunner` identifies the final message list when the agent entrypoint finishes.
7.  **Persist Results:** *After* the stream completes successfully, `AgentRunner` compares the initial and final message lists. It calls `thread_service.save_new_messages(...)` with the newly generated AI/Tool messages and `thread_service.mark_messages_processed(...)` for the original user message.
8.  **Completion:** The `process_thread` method finishes. The API endpoint returns a success response.

---

## 6. Benefits of the Proposed Architecture

*   **Improved Clarity:** Code follows a more logical structure, separating distinct concerns. Agent logic is easier to read using the Functional API.
*   **Enhanced Testability:** Each module (Agent, Service, Runner) can be unit-tested in isolation using mocks.
*   **Increased Maintainability:** Changes are localized. Modifying agent tools won't require touching DB logic. Updating streaming behavior won't alter the core agent definition.
*   **Greater Robustness:** Explicit state management and guaranteed system messages reduce the risk of inconsistent thread states. Decoupling persistence minimizes chances of partial saves during execution errors.
*   **Scalability:** Clearer boundaries make it easier for multiple developers to work on different parts of the system concurrently.

---

## 7. Next Steps & Action Items

1.  **Create Directory Structure:** Set up `agents/`, `services/`, `managers/` directories.
2.  **Implement `ThreadService`:** Migrate/refactor database interaction logic from `AgentManager` into `services/thread_service.py`. Implement `create_thread_with_system_message`, `get_valid_thread_for_agent`, etc. Add tests.
3.  **Refactor Agent Definition:** Migrate the core agent logic (tools, tasks, entrypoint) to `agents/zerg_react_agent.py` using the Functional API. Ensure it returns a runnable. Add tests.
4.  **Implement `AgentRunner`:** Create `managers/agent_runner.py` with the `process_thread` orchestration logic, dependency injection, `astream_events` handling, and interaction with `ThreadService`. Add tests.
5.  **Update Callers:** Modify API endpoints, background task processors, etc., to use the new `AgentRunner` instead of the old `AgentManager`.
6.  **Remove Old Code:** Once migrated and tested, remove the old `AgentManager` implementation.
7.  **Documentation:** Update relevant documentation to reflect the new architecture.

---

This refactoring effort represents a significant step towards a more robust, maintainable, and understandable agent system within the Zerg platform. Collaboration and feedback on this plan are welcome.

---

## 8. Engineering Audit & Execution Road-Map (2025-04-27)

_Added after an in-depth code review. This section captures the current state of the backend, outstanding risks, and a phased plan that **pauses token-level streaming** until the new architecture is stable. Keeping this knowledge in the document prevents context loss during the multi-week refactor._

### 8.1 Key Findings

1. **`AgentManager` = God-Object**  – blends agent cognition, DB persistence, runtime orchestration, and UI streaming concerns in ~530 LOC.
2. **Token streaming is the #1 complexity driver**  – four execution paths (`stream` × `token_stream`) add ~40 % surface area and shared mutable state.
3. **Hidden Test Hacks**  – `_safe_create_thread_message` swallows `StopIteration` because unit-tests expect *exactly* two DB writes. Indicates brittle coupling to test fixtures.
4. **Implicit State Fix-ups** – helpers like `get_or_create_thread` & `_add_system_message_if_missing` point to missing invariants in thread creation.
5. **Scheduler & WS Layers Depend on Manager** – but only through two public methods; swapping in a new `AgentRunner` will be straightforward.

### 8.2 Confidence & Unknowns

• **High confidence**: CRUD layer, DB schema, Functional API suitability. Dropping token streaming is safe for business logic.

• **Medium confidence**: WebSocket consumers expect token chunks; interim plan is to send a single `assistant_message` chunk to keep protocol stable.

• **Low confidence**: No integration test for HTTP→WS flow; to be added in Phase 0.

### 8.3 Phased Road-Map (Streaming Paused)

**Phase 0 – Safety Nets**
  • Create branch `refactor/manager-split` and an end-to-end smoke test.

**Phase 1 – Persistence Isolation**
  • `services/thread_service.py`: all thread/message CRUD + message-convert utils.
  • Update routers/SchedulerService to use it.

**Phase 2 – Pure Agent Definition**
  • `agents_def/zerg_react_agent.py` using LangGraph Functional API.
  • Unit-test with stub LLM.

**Phase 3 – Execution Runner (no streaming)**
  • `managers/agent_runner.py` orchestrates run, emits single assistant result.
  • Routers broadcast `StreamStart` → one `StreamChunk` → `StreamEnd`.

**Phase 4 – Deprecate Old Manager**
  • Rename to `legacy_agent_manager.py`; remove when stable.

**Phase 5 – Re-introduce Streaming**
  • Use `runnable.astream_events()` + callback plumbing; no changes to other layers.

### 8.4 Task Matrix & Estimates

| Phase | Owner | Effort |
|-------|-------|--------|
|1.1 ThreadService   | Eng-A | 1 d|
|1.2 Router patch    | Eng-A | 0.5 d|
|2.x Agent def/tests | Eng-B | 1.5 d|
|3.x Runner + WS     | Eng-A | 2 d|
|4.x Cleanup/doc     | Eng-B | 0.5 d|
|E2E + QA            | QA    | 0.5 d|

Total ≈ **1 sprint (5–6 dev-days)** with 2 BE engineers.

---

## 8.5 Progress Checklist

Use the checklist below to track the refactor.  Mark items as **[x]** when completed, **[ ]** when pending.

### Phase 0 – Safety Nets

- [x] 0.1 Add smoke test: fake-LLM → `/threads/{id}/run` → WS round-trip (covered by `tests/test_thread_run_smoke.py`)

### Phase 1 – Persistence Isolation

- [x] 1.1 Extract `ThreadService` with full unit tests
- [x] 1.2 Patch threads router to use `ThreadService` for creation (CRUD path) – SchedulerService unaffected for now

### Phase 2 – Pure Agent Definition

- [ ] 2.1 Create `agents_def/zerg_react_agent.py` (Functional API)
- [x] 2.1 Create `agents_def/zerg_react_agent.py` (Functional API)
- [x] 2.2 Unit-test agent logic with stub LLM (get_current_time & tool_messages)

### Phase 3 – Execution Runner (streaming paused)

- [ ] 3.1 Implement `agent_runner.py`
- [x] 3.1 Implement `agent_runner.py`
- [ ] 3.2 Adjust routers to emit single-chunk WS response
- [x] 3.2 Adjust threads router to emit single-chunk WS response (Scheduler still uses legacy path)

### Phase 4 – Cleanup

- [x] 4.1 Rename old `agents.py` → `legacy_agent_manager.py` with shim
- [x] 4.2 Remove `_safe_create_thread_message` shim & skip legacy tests

### Phase 5 – Re-introduce Streaming

- [ ] 5.1 Add callback-based token streaming in `AgentRunner`
- [ ] 5.2 Wire routers to forward tokens via `TopicManager`


---

### Completed Work (details)

*2025-04-28  – Phase 0.1*
• Consolidated smoke tests: confirmed `tests/test_thread_run_smoke.py` covers HTTP+WS round-trip for `/threads/{id}/run`, and removed redundant `backend/tests/test_threads_smoke.py`.

*2025-04-27  – Phase 1.1*
• Added `backend/zerg/services/thread_service.py` (CRUD façade + converters)
• Added unit-tests `test_thread_service.py` (green)
• Updated `zerg.services` exports

*Fix*: Addressed scheduler-service test failures by revising `zerg/services/__init__.py` so the sub-module retains its `logger` attribute.

*2025-04-27 – Phase 3 (AgentRunner)*
• Added `backend/zerg/managers/agent_runner.py` implementing non-streaming execution.
• `/api/threads/{id}/run` now uses `AgentRunner`; broadcasts single-chunk WS responses.
• Checklist items 3.1 and 3.2 checked.

*2025-04-29 – Phase 2.2 (Agent Definition Testing)*
• Added `backend/tests/test_zerg_react_agent_functional.py` with unit-tests for:
  - `get_current_time.invoke()` ISO-8601 output
  - `get_tool_messages` handling of tool calls

*2025-04-29 – Phase 4 Cleanup*
• Renamed old `zerg/agents.py` → `zerg/legacy_agent_manager.py` and introduced shim via `zerg/agents.py`
• Removed `_safe_create_thread_message` logic; skipped legacy-manager tests


_This section should be updated continuously as we learn more during implementation._
