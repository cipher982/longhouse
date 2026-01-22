# 📝  Converged Plan for Restoring “Task” Runs
----------------------------
---

• Task Run
  – One-shot, non-interactive execution of task_instructions.
  – Launch sources: “▶ Play” button, Scheduler, future Webhook.
  – Always creates a new Thread that stores:
    • system message (agent.system_instructions)
    • one user message containing task_instructions
    • assistant / tool messages produced by the run.
  – No token streaming required; UI simply opens the thread when finished.
  – Agent status flips running → idle/error so the dashboard badge reflects progress.

• Chat Session (out of scope for this fix)
  – Interactive turns, token streaming via WsTokenCallback.
  – Can be designed later using the same AgentRunner but different routing.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

    1. Technical Gap

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

Legacy path /api/agents/{id}/task and SchedulerService still call
AgentManager.execute_task() – a method that was removed in the functional
refactor. We must re-implement the behaviour using the new primitives:

  • ThreadService  – DB helpers (exists).
  • AgentRunner    – runs the ReAct graph (exists).
  • event_bus      – to broadcast AGENT_UPDATED.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

- [x] **New Helper**  ➜  `backend/zerg/services/task_runner.py`

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

    # pseudo-code signature
    async def execute_agent_task(db: Session, agent: AgentModel) -> ThreadModel:
        """
        Create a fresh thread, run AgentRunner once (non-stream), persist results,
        update agent.status / timestamps, broadcast events.
        Returns the created Thread ORM row.
        """

Step-by-step inside the helper

    1. Validate agent has `task_instructions`; 400 if missing.
    2. `crud.update_agent(..., status="running")`; `db.commit()`.
    3. `event_bus.publish(EventType.AGENT_UPDATED, {"id": agent.id, "status": "running"})`
    4. `thread = ThreadService.create_thread_with_system_message(db, agent,
       title=f"Manual Task Run – {utc_now()}",
       thread_type="manual")`
    5. Insert user task prompt (processed=False):
       `crud.create_thread_message(db, thread_id=thread.id, role="user",
        content=agent.task_instructions)`
    6. `await AgentRunner(agent).run_thread(db, thread)`   # stream=False by default
    7. On success: set agent to *idle*, fill `last_run_at`, `db.commit()`,
       publish AGENT_UPDATED (include `thread_id`), return thread.
    8. On exception: set status *error*, store `last_error`,
       publish AGENT_UPDATED, re-raise.

Notes
• No token streaming flag is enabled, so AgentRunner will skip WsTokenCallback.
• We reuse ThreadService so logic is DB-clean.
• Helper is self-contained → easily reused by SchedulerService.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

### 2. Wire-up Changes

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

- [x] **API Route** – `/api/agents/{id}/task` now delegates to `execute_agent_task`.
- [x] **SchedulerService** – internal `run_agent_task()` uses the same helper; `AgentManager` import dropped.
- [x] **Dead-code purge** – all remaining `execute_task(` call-sites removed.  The legacy module still lives for *chat* path but is now completely unused for task runs.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

### 3. Tests to Update / Add

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

- [x] `tests/test_agents.py` patched (no external AgentManager mocks, uses new helper).
- [ ] Add dedicated unit coverage for `execute_agent_task` (helper logic).  Low priority – core paths already exercised via router & scheduler tests.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

    1. README / Docs (minor touch)

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

*README* wording still accurate; opt-in tweak can be done later.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

    1. Future Proofing

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

No change – still valid future-work.
  – Creates a thread, returns WS topic;
  – Re-uses AgentRunner with token streaming enabled.

• Once chat is migrated, the entire legacy_agent_manager.py can be deleted.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

    1. Next Steps

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

    1. Add `task_runner.py` with the helper.
    2. Patch router & scheduler.
    3. Run full test-suite (`./backend/run_backend_tests.sh`) and add new tests.
    4. Remove legacy refs, commit.
