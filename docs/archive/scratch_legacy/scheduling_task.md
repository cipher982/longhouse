Okay, here is a summary report of our discussion on implementing autonomous agents:

## Report: Enabling Autonomous Agent Execution

**1. Goal:**

To evolve the current AI Agent Platform beyond manual triggers (chat interactions or "play" button clicks) towards truly autonomous agents capable of proactive, scheduled, and potentially stateful execution of tasks.

**2. Problem Statement:**

Currently, agents are primarily reactive. They respond to user input in a chat thread or execute a single, predefined task when manually triggered via the dashboard's "play" button. There is no built-in mechanism for agents to initiate actions based on time, events, or persistent goals without direct user interaction for each run.

**3. Existing Context:**

*   **Architecture:** The platform consists of a Rust/WASM frontend (Dashboard & Canvas Editor views) and a Python/FastAPI backend.
*   **Agent Model (`zerg/app/models/models.py::Agent`):** Agents store `system_instructions`, `task_instructions`, status, and importantly, already have a `schedule` field (CRON string). A new `run_on_schedule` boolean field has been added.
*   **Thread Model (`zerg/app/models/models.py::Thread`):** Threads store conversation history (`ThreadMessage`) and include an `agent_state` JSON field, suitable for persisting arbitrary data across runs.
*   **Execution Logic (`zerg/app/agents.py::AgentManager`):** Uses LangGraph to manage agent execution state (`AgentManagerState`), process message history, interact with the LLM, and potentially use tools. It loads messages from the `ThreadMessage` table for context.

**4. Discussion & Brainstorming Summary:**

*   **Defining "Autonomous":** We considered features like proactive execution (scheduling/triggers), goal persistence, environmental interaction (tools/APIs), and planning/decision-making.
*   **Initial Focus:** We decided to prioritize **proactive execution via scheduling** as the most logical first step, building upon the existing `schedule` field and the new `run_on_schedule` field.
*   **Run Modes:** Distinguished between the current "Manual One-Off" run and the desired "Scheduled Run".
*   **Context/Memory:** Explored how agents maintain context between autonomous runs, settling on using the agent's primary thread or creating one if needed, leveraging `Thread.agent_state` for long-term state persistence.

**5. Implementation Progress:**

*   **Backend Tasks:**
    *   [x] Add `APScheduler` dependency via `uv` and update `pyproject.toml`.
    *   [x] Add `run_on_schedule: bool` field to the `Agent` model (`zerg/app/models/models.py`).
    *   [x] Update Pydantic schemas (`zerg/app/schemas/schemas.py`) to include `run_on_schedule`.
    *   [x] Update CRUD functions (`create_agent`, `update_agent` in `zerg/app/crud/crud.py`) to handle `run_on_schedule`.
    *   [x] Update API endpoints (`/api/agents/` POST and PUT in `zerg/app/routers/agents.py`) to accept and return `run_on_schedule`.
    *   [x] Create `services/` directory (`backend/zerg/app/services/`).
    *   [x] Implement `SchedulerService` (`backend/zerg/app/services/scheduler_service.py`):
        *   [x] Initialize `APScheduler` (using `AsyncIOScheduler`).
        *   [x] Implement function to load agents from DB (`run_on_schedule=True`, valid `schedule`).
        *   [x] Implement function (`run_agent_task`) to be called by the scheduler job:
            *   [x] Get DB session
            *   [x] Retrieve the `Agent` model
            *   [x] Get or create the agent's persistent `Thread`
            *   [x] Instantiate `AgentManager`
            *   [x] Call `agent_manager.process_message(db, thread, content=agent.task_instructions, stream=False)`
            *   [x] Include error handling (logging, updating agent status if needed)
        *   [x] Add logic to add/update/remove jobs in `APScheduler` based on agents found in the DB
        *   [x] Integrate scheduler startup/shutdown with FastAPI lifecycle events (`@app.on_event("startup")`, `@app.on_event("shutdown")`) in `zerg/main.py`.

*   **Database:**
    *   [x] Schema updated (manually, by deleting and recreating DB, as migrations were skipped).

**6. Key Implementation Details:**

1. **Scheduler Service Design:**
   - Uses `AsyncIOScheduler` for async compatibility with FastAPI
   - Global singleton instance for app-wide access
   - Clean startup/shutdown hooks in FastAPI lifecycle
   - Proper DB session management in all operations

2. **Thread Management:**
   - Creates dedicated threads for scheduled runs
   - Maintains system instructions and context
   - Disables streaming for scheduled runs (no active user to stream to)

3. **Error Handling & Logging:**
   - Comprehensive error handling at all levels
   - Detailed logging for debugging and monitoring
   - Graceful failure handling to prevent cascade failures

**7. Next Steps:**

1. **Testing:**
   - Write unit tests for the scheduler service
   - Test edge cases (invalid CRON expressions, DB failures)
   - Integration tests with the full agent execution flow

2. **Monitoring & Observability:**
   - Add metrics for scheduled job success/failure
   - Track execution times and resource usage
   - Consider adding alerts for repeated failures

3. **Future Enhancements:**
   - Add API endpoints to manually trigger/pause scheduled jobs
   - Implement retry logic for failed scheduled runs
   - Consider adding job history and execution logs
   - Add support for more complex scheduling patterns (intervals, one-time schedules)

Would you like to proceed with any of these next steps?
