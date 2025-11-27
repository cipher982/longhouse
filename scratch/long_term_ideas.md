# Project Context & Roadmap: Zerg Automation Platform

Document Purpose:
Provides essential context, current status, roadmap, development guidelines, and future ideas
for the Zerg project. Written for both human developers and AI coding assistants.

Last Updated: 2025-06-05 (Updated to reflect major completed milestones: Authentication, 
MCP Integration, User Personalization, and advanced Trigger system.)

----------------------------------------------------------------------------------------------

## 1. Project Overview & Goals

    * **Core Purpose:**
      Zerg is an automation platform enabling users to connect event triggers (webhooks,
schedules, etc.) to AI agents and deterministic tools using a visual canvas interface. The
goal: easily build, execute, and monitor complex, event-driven AI-powered workflows.
    * **Technology Stack:**


        * **Backend:** Python, FastAPI, SQLAlchemy (synchronous), LangGraph, APScheduler

        * **Frontend:** Rust (WASM compiled, Yew Framework), WebSockets

        * **Key Libraries:** Pydantic v2, `serde`/`serde_wasm_bindgen` (Rust), `uv` (Python env
 management)
    * **High-Level Architecture:**


        * Central `EventBus` for loose-coupling between backend services.

        * WebSocket Hub for real-time push by topic to clients (Dashboard, Canvas, Chat).

        * `AgentService` executes agent logic via LangGraph (current: single-node, DAGs/flows
planned).

        * Cron scheduling (APScheduler), Webhook triggers, per-agent config.

        * Frontend: Elm-style global state/message pattern, canvas-based visual editor, live
chat/threads, and dashboards.

----------------------------------------------------------------------------------------------

## 2. Current System State & What’s Landed

### Legend

✅ = Shipped & Verified | 🟡 = Partially Done | ❌ = Not Started

### Backend

    * ✅ Agent/Thread/Message CRUD, robust test suite
    * ✅ LangGraph-based agent runtime (single-node; tool execution loop)
    * ✅ Streaming chat/events, APScheduler, real-time WebSockets/EventBus
    * ✅ Webhook triggers (full backend CRUD, event, HMAC verification logic)
    * ✅ Agent Debug Endpoint & Modal (overview + raw JSON)
    * 🟡 Workflow DAG runtime (multi-node execution): not started, foundational code ready
    * 🟡 Tool runtime: core flows live, easy to expand tool set
    * 🟡 Multi-agent orchestration: only primitives exist (no exposed flows yet)

### Frontend

    * ✅ Core dashboard, chat, canvas (basic agent node support)
    * ✅ Networking: full REST and topic-based WebSocket
    * ✅ Scheduling: cron badge, display/edit, enable/disable, real-time
    * ✅ Agent Table, Debug Modal (overview, raw JSON); Live chat threads
    * 🟡 Trigger UI: List/copy works; missing create/delete and modal CRUD
    * 🟡 Thread surfacing: All runs tracked, but history/tab/presentation not unified
    * ✅ Tools/ToolMessages show with collapsible panels in Chat UI
    * 🟡 Canvas node variety: Only AgentIdentity nodes editable/visible; no Tool, Trigger,
Condition, Input/Output nodes yet
    * ❌ Canvas export/import, runtime highlight, workflow execution visualization
    * ❌ 'Result Panel', structured output, export to JSON/CSV, automatic summaries

### Cross-Cutting / Ops

    * ✅ Multi-tenancy/Auth (JWT, Google Sign-In, user management, WebSocket auth)
    * ✅ MCP Integration (Model Context Protocol server management, tool integration)
    * ✅ User Personalization (avatars, profiles, preferences, Gmail integration)
    * ❌ Usage analytics/cost metering
    * ❌ Observability/admin dashboards/metrics

----------------------------------------------------------------------------------------------

## 3. Roadmap & Milestones (2025+)

### Primary Objective

Allow users to visually assemble, run, and manage event-driven workflows combining AI agents
and deterministic tools.

### Milestone Breakdown

┌────────────────────────┬─────────────┬───────────────────────────────────────────────────────
────────────────────────────────────────┐
│ Milestone              │ Status      │ Key Notes / Gaps                                      
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M0: UX polish          │ ✅ Complete │ Scheduling, cron, badges, thread creation, display
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M1: Triggers v1        │ 🟡 Nearly   │ Backend HMAC/event/CRUD/Gmail complete. UI list/copy 
                         │   Complete  │ done. Missing: frontend create/delete modal only.
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M2: Workflow Engine    │ ❌          │ Canvas DAG → LangGraph, export/import, exec highlight
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M3a: Tool Runtime      │ ✅          │ Server-side tool exec + UI display/panels proven
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M3b: Toolbox UI/Nodes  │ 🟡 Partial  │ MCP tool integration working. Missing: canvas nodes
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M4: Debugging UX       │ ✅ v2       │ Multi-tab debug modal, run history, agent telemetry
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M5: Multi-tenancy/Auth │ ✅ Complete │ Google Sign-In, JWT, WebSocket auth, user management
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M6: Observability/Cost │ ❌          │ Usage stats, metrics, admin dashboards
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M7: MCP Integration    │ ✅ Complete │ Server management, tool integration, frontend UI
                                        │
├────────────────────────┼─────────────┼───────────────────────────────────────────────────────
────────────────────────────────────────┤
│ M8: User Features      │ ✅ Complete │ Avatars, profiles, preferences, Gmail integration
                                        │
└────────────────────────┴─────────────┴───────────────────────────────────────────────────────
────────────────────────────────────────┘

----------------------------------------------------------------------------------------------

## 4. Current Epic Focus

    * **Workflow Engine (M2):** Canvas DAG → LangGraph execution, export/import, runtime highlighting.
    * **Canvas Nodes:** Complete Tool, Trigger, and Condition node placement and config UI.
    * **Result Panel:** Structured output display, export capabilities, automatic summaries.
    * **Usage Analytics:** Cost metering, observability dashboards, admin tools.

----------------------------------------------------------------------------------------------

### Immediate Priority Tasks (as of June 2025)

    * [ ]  **Complete Workflow Engine (M2):** Canvas DAG → LangGraph execution pipeline.
    * [ ]  **Finish Trigger UI:** Complete frontend create/delete modal (backend done).
    * [ ]  **Add Canvas Node Palette:** Tool, Trigger, Condition nodes with config UI.
    * [ ]  **Implement Canvas JSON export/import** and backend workflow DAG support.
    * [ ]  **Add "Result Panel"** for structured outputs/export and summary to chat/canvas UI.
    * [ ]  **Begin usage metering/analytics** – cost tracking, observability dashboards.

----------------------------------------------------------------------------------------------

## 5. Development Practices & Guidelines

    * **Always run backend tests via `./backend/run_backend_tests.sh`** (never direct
`pytest`).
    * **Rust/wasm/Frontend:** Use message-passing and state reducer conventions. Never mutate
global state except in the top-level reducer.
    * **Backend event lifecycle:** Use `@publish_event()` decorators for API state changes;
subscribe to and publish via EventBus for real-time/client sync.
    * **Cron/scheduler:** Agents are considered scheduled if `schedule` is set. No separate
boolean.
    * **New features:** Mirror backend/REST representation closely in frontend models; follow
Elm/message-update conventions.

----------------------------------------------------------------------------------------------

## 6. Terminology & Concept Cheatsheet

    * **Agent:** a configured LLM “worker” node; has system prompt, threads, persistent config.
    * **Tool:** deterministic function/orchestratable workflow step.
    * **Trigger:** event (schedule, webhook, manual) that launches workflow.
    * **Thread:** execution history for agent, including every inbound/outbound message.
    * **CanvasNode:** placeable element (Agent, Tool, Trigger, Condition, etc.) in the graph
editor/canvas.

----------------------------------------------------------------------------------------------

## 7. Future Ideas & Long-Term Backlog

### Output & Results

    * Structure agent/tool outputs (tables, JSON, HTML, charts) in UI (“Result Panel”).
    * Export thread logs/results (CSV/JSON).
    * Summarize long threads automatically (dedicated node or chat button).

### Tooling & Orchestration

    * UX/UI for Notification tools, and for chaining/running sub-workflows (multi-agent
orchestration).
    * Support for users specifying fine-tuned model IDs per agent/system.

### Canvas & Workflow

    * Full node variety (Tool, Trigger, Condition, Input/Output, etc.).
    * Drag-and-drop palette for placing canvas nodes.
    * DAG export/import to backend (and vice versa).
    * Visual highlight of currently active node during execution.

### Analytics/Auth

    * Multi-tenancy, JWT/Google auth, workspace-permission enforcement.
    * Detailed cost and usage metering; per-run stats and error reporting.
    * Admin/observability dashboards.

----------------------------------------------------------------------------------------------

## 8. Status History & Changelog

    * **2025-04:** Cron/scheduling UI complete. Thread persistence unified.
    * **2025-05:**
        * Webhook event backend + HMAC shipped. Frontend trigger list/copy live.
        * Debug modal, decoupled node/agent logic completed.
        * Workflow engine & advanced canvas DAG plumbing not started.
        * Remaining critical: canvas node variety, CRUD for triggers, workflow
persistence/export/import, groundwork for authentication and analytics.
    * **2025-06:** **MAJOR MILESTONE COMPLETIONS:**
        * ✅ **Authentication System (M5):** Google Sign-In, JWT tokens, WebSocket auth, user management fully implemented.
        * ✅ **MCP Integration (M7):** Complete Model Context Protocol server management, tool integration, frontend UI.
        * ✅ **User Personalization (M8):** Avatars, profiles, preferences, Gmail integration shipped.
        * ✅ **Enhanced Debugging (M4 v2):** Multi-tab debug modal, run history, agent telemetry.
        * 🟡 **Advanced Triggers (M1):** Backend completely done including Gmail integration. Frontend missing only create/delete modal.
        * **Focus shifted to:** Workflow Engine (M2), Canvas export/import, Result Panel, usage analytics.

----------------------------------------------------------------------------------------------

## 9. Risks & Considerations

┌───────────────────────────────────────────────────────┬────────────┬──────────┬──────────────
──────────────────────────────────────────┐
│ Risk                                                  │ Likelihood │ Impact   │ Mitigation   
                                          │
├───────────────────────────────────────────────────────┼────────────┼──────────┼──────────────
──────────────────────────────────────────┤
│ APScheduler single-process bottleneck                 │ Medium     │ High     │ Prototype
Celery or redis offload in future            │
├───────────────────────────────────────────────────────┼────────────┼──────────┼──────────────
──────────────────────────────────────────┤
│ Webhook secret previously only checked via body param │ High       │ High     │ Now HMAC
header, but add UI for secret visibility      │
├───────────────────────────────────────────────────────┼────────────┼──────────┼──────────────
──────────────────────────────────────────┤
│ Lack of auth on public deployments                    │ ✅ RESOLVED │ Critical │ Authentication
system fully implemented and production-ready │
├───────────────────────────────────────────────────────┼────────────┼──────────┼──────────────
──────────────────────────────────────────┤
│ LangGraph API surface changes (still very new)        │ Medium     │ Medium   │ Lock
versions, integration tests, thin adaption layers │
├───────────────────────────────────────────────────────┼────────────┼──────────┼──────────────
──────────────────────────────────────────┤
│ Token/cost overrun from tool streaming                │ Low        │ Medium   │ Add per-run
token/time budgets                         │
└───────────────────────────────────────────────────────┴────────────┴──────────┴──────────────
──────────────────────────────────────────┘

----------------------------------------------------------------------------------------------

### End of Document

(Any contributor should review [sections 2–4] before updating priorities, and always re-confirm
 “real” status via repo/tests before marking tasks complete.)
