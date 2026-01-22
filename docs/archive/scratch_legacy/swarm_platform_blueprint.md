• Swarm Platform Blueprint

Purpose
Deliver a unified, self-hosted agent platform where Jarvis—a realtime voice/text assistant—serves as
the human interface and Zerg powers scheduling, workflows, and tool execution. This document captures
every relevant decision, asset, and task so work can resume from scratch without additional context.

———

### 1. Vision & Principles

- Jarvis as the face: A ChatGPT-style PWA (voice + text) on phone/desktop with instantaneous
  responses, context memory, and a Task Inbox summarizing all automated activity.
- Zerg as the hive: FastAPI + LangGraph backend running durable workflows, cron schedules, tool
  integrations (MCP + custom), history, and audit logs.
- Unified control: Jarvis issues commands, Zerg executes and reports back; no manual terminals
  required to interact with the swarm.
- Self-hosted autonomy: All code lives in one monorepo; you own runtime, data, private tool access,
  and can iterate without vendor locks.
- Inspirations, not dependencies: Use OpenAI AgentKit features (typed edges, previews, evals) as
  inspiration but replicate locally.

———

### 2. Current Assets Overview

Jarvis repository (/apps/jarvis future path)

- PWA built with Vite + TypeScript (apps/web/main.ts).
- Session management, IndexedDB persistence (packages/core/src/session-manager.ts, packages/data/
  local).
- Context loader (personal/work) with tool configs (apps/web/contexts).
- UI components (ConversationRenderer, radial visualizer).
- Node/express server bridging MCP tools and Realtime API (apps/server/server.js).
- Monorepo-ready packages: packages/core, packages/data/local.

Zerg repository (/apps/zerg future path)

- FastAPI backend (Python, uv) with LangGraph orchestration.
- SchedulerService using APScheduler (backend/zerg/services/scheduler_service.py).
- TaskRunner executing agent runs with locking, event bus, run history (backend/zerg/services/
  task_runner.py).
- Models for agents, threads, runs (backend/zerg/models/models.py).
- MCP tool adapter, presets (backend/zerg/tools/*).
- WebSocket/event infrastructure (backend/zerg/events).
- React/Rust front-ends (optional once Jarvis becomes main UI).

———

### 3. Monorepo Migration Plan

Structure

/apps
  /jarvis        # PWA + node server
  /zerg          # FastAPI backend, LangGraph workflows
/packages
  /contracts     # Generated OpenAPI/AsyncAPI clients (TS + Python)
/  tool-manifest # Shared MCP tool definitions output as TS + Python
/docs
  jarvis_integration.md
  architecture.md

Tooling

- Use pnpm workspaces (or npm) for TypeScript packages.
- Use uv + makefiles for Python dependencies.
- Top-level scripts:
    - make jarvis-dev – runs PWA + node bridge.
    - make zerg-dev – runs FastAPI backend.
    - make swarm-dev – optional concurrent start.
    - make generate-sdk – runs OpenAPI generation into /packages/contracts.
    - make seed-jarvis-agents – seeds baseline Zerg agents.

CI/CD

- Single pipeline: lint/test Zerg, lint/test Jarvis, run integration smoke test (start backend, run
  headless browser hitting Jarvis UI, dispatch a job, verify SSE updates).
- Artifacts: build PWA, containerize backend as optional.

Dev Environment

- Shared .env with namespaced values (JARVIS_*, ZERG_*), documented in /docs/environment.md.
- Provide script to copy sample env and remind developer to populate secrets.

———

### 4. System Architecture Overview

Jarvis Front Door

- Modes: voice (OpenAI Realtime WebRTC) + text (REST fallback).
- Conversation state stored locally and optionally synced to Zerg.
- Task Inbox showing run status via SSE feed.
- Context switcher for persona (personal/work).
- PWA features: offline shell, home-screen icon, push notifications (later).

Zerg Backend

- FastAPI routers for agents, runs, threads, jarvis dispatch.
- LangGraph workflows for complex tasks.
- APScheduler for cron (daily digests, health snapshots).
- Event bus broadcasting run updates.
- MCP aggregator for tools (WHOOP, Traccar, Gmail, Slack, etc).
- Historical storage and cost/usage metrics.

Integration Flow

1. User speaks or types to Jarvis.
2. Jarvis handles quick LLM replies locally.
3. When a task requires scheduling or multi-step action, Jarvis POST /api/jarvis/dispatch.
4. Zerg enqueues run via execute_agent_task (handles locking, run rows, events).
5. Event bus pushes updates; /api/jarvis/events SSE streams statuses.
6. Jarvis Task Inbox updates UI and optionally speaks the result.
7. Scheduled agents run autonomously; SSE stream updates Jarvis automatically.

———

### 5. Detailed Feature Targets

#### Jarvis Enhancements

- Text mode: Input field to send typed queries through same flow as voice. Extends apps/web/main.ts.
- Task Inbox: Side panel summarizing AgentRun entries (status, updated_at, summary). Data store keyed
  by run_id.
- Voice intents: Map recognized intents to dispatch payloads (Zod schema). Provide fallback prompts
  for ambiguous requests.
- Notifications: Initially SSE-based toasts/audio; later web push via service worker.
- Home screen polish: Manifest icons, share instructions for “Add to Home Screen”; optionally create
  Apple Shortcut to launch & auto-connect.
- Desktop parity: Document using PWA in macOS (or electron wrapper).
- Context integration: Import tool manifest from shared package; maintain persona instructions and
  default prompts.

#### Zerg Enhancements

- Jarvis Router (backend/zerg/routers/jarvis.py):
    - POST /api/jarvis/auth: issue short-lived JWT when provided device secret.
    - POST /api/jarvis/dispatch: call execute_agent_task, return run_id/thread_id.
    - GET /api/jarvis/agents: minimal agent list (id, name, schedule, status, next_run_at).
    - GET /api/jarvis/runs: recent runs filtered by user (status, summary).
    - GET /api/jarvis/events: SSE endpoint streaming run/agent updates.
- SSE Integration: Use FastAPI EventSourceResponse; subscribe to event bus (EventType.RUN_UPDATED,
  EventType.AGENT_UPDATED, EventType.RUN_CREATED). Ensure cleanup on disconnect.
- Run summary: Extend AgentRun model with summary column storing first assistant response or
  truncated output. Update crud.mark_finished.
- Cron templates: Add script to create default agents (e.g., morning_digest, health_watch,
  finance_snapshot). Each uses cron schedule and appropriate tool allowlists.
- Tool manifest generation: Source of truth in Python; script outputs TS file used by Jarvis
  contexts.
- LangGraph integration: Document how Jarvis dispatch integrates with existing workflows; optional
  bridging for customizing node canvases.

———

### 6. Implementation Roadmap

Phase 0 – Monorepo Migration (prep)

- Merge repositories under structure above.
- Adjust package.json, uv configs, Makefiles.
- Ensure existing tests run within new layout.
- Document new README with setup instructions.

Phase 1 – Control Plane

- Implement /api/jarvis/auth, /api/jarvis/agents, /api/jarvis/runs.
- Generate initial OpenAPI client and integrate into Jarvis UI (sidebar listing).
- Add documentation for endpoint usage.

Phase 2 – Dispatch & SSE

- Implement dispatch endpoint; handle errors (e.g., agent running).
- Build SSE channel; subscribe Jarvis to live updates.
- Update PWA to show Task Inbox and voice notifications when runs change.

Phase 3 – Text Mode & UX

- Add text input mode and conversation toggles; unify with voice pipeline.
- Polish PWA (manifest, icons, offline support).
- Document home-screen setup instructions.

Phase 4 – Cron Starter Pack

- Seed baseline scheduled agents with prompts and tool configs.
- Ensure SchedulerService recognizes them; test next_run_at updates.
- Show scheduled tasks in Jarvis Task Inbox.

Phase 5 – Tool Manifest & Sync

- Implement manifest generation package.
- Update both Jarvis contexts and Zerg MCP registry to import from shared manifest.
- Write tests ensuring parity.

Phase 6 – Notifications and Evals

- Prototype web push (if feasible).
- Add evaluation harness: Zerg run logs + Jarvis transcripts -> simple trace grader (inspired by
  AgentKit).
- Document manual eval process.

———

### 7. Technical References

Jarvis code anchors

- apps/web/main.ts – entry point, WebRTC session logic.
- apps/web/lib/conversation-renderer.ts – transcript UI.
- packages/core/src/session-manager.ts – conversation memory and sync.
- apps/web/contexts/context-loader.ts – context/theme loader.
- apps/server/server.js – token minting, MCP bridging.

Zerg code anchors

- backend/zerg/services/scheduler_service.py – cron scheduling.
- backend/zerg/services/task_runner.py – agent run execution & events.
- backend/zerg/models/models.py – agent/run/threads schema.
- backend/zerg/tools/mcp_adapter.py – MCP integration.
- backend/zerg/routers/agents.py – existing agent CRUD patterns.
- backend/zerg/events/event_bus.py – event broadcasting.

New modules

- backend/zerg/routers/jarvis.py – Jarvis-specific API layer.
- packages/tool-manifest – shared tool definitions for Jarvis + Zerg.
- docs/jarvis_integration.md – usage guide detailing flows.

———

### 8. Auth & Security Plan

- Jarvis supplies X-Device-Secret to /api/jarvis/auth. If valid, returns JWT with scope=jarvis.
- JWT stored securely in IndexedDB/localStorage; refresh before expiry.
- All /api/jarvis/* endpoints require this token. Depends(get_current_user) ensures token user
  matches agent owner.
- Document secret rotation process; optionally allow multiple device secrets for different devices.

———

### 9. Testing Strategy

- Unit: existing tests in both apps continue (pytest via uv, Vitest).
- API: add FastAPI tests for new Jarvis endpoints; simulate SSE client.
- UI: add Playwright test verifying 1) text command dispatch, 2) SSE updates display.
- Integration: CI script that
    - Starts Zerg backend.
    - Seeds mock agent & user.
    - Starts Jarvis server in headless mode.
    - Dispatches run via frontend automation.
    - Confirms SSE event received.
- Manual: run make seed-jarvis-agents, make swarm-dev, verify PWA on phone receives updates and
  scheduled jobs run.

———

### 10. Documentation Deliverables

- /docs/jarvis_integration.md: architecture, endpoints, SSE payload schema, dispatch flow, task inbox
  behavior.
- /docs/tool_manifest_workflow.md: how to add new tool, regenerate manifest, update contexts.
- /docs/cron_templates.md: default agents, prompts, schedules.
- /docs/mobile_setup.md: instructions for adding PWA to iPhone, microphone permissions, optional
  shortcuts.
- Update README.md at root summarizing project, dev setup, and mission statement.

———

### 11. Long-Term Enhancements

- Workflow builder parity: create a local visual canvas (React-based) mimicking AgentKit features
  (typed edges, preview runs). Optionally embed in Jarvis for advanced editing.
- Offline mode: allow Jarvis to queue dispatches offline and flush when reconnected (leverage
  SessionManager and new queue API).
- Push notifications: integrate with APNs/FCM or service worker push to alert even when PWA closed.
- Native wrappers: wrap PWA in Capacitor/Electron for deeper OS integration (hotkeys, background
  service).
- Model diversity: enable switching between OpenAI, Anthropic, local models via configuration; update
  TaskRunner to route accordingly.
- Analytics dashboard: optional UI to visualize run history, costs, success rates.

———

### 12. Glossary

- Jarvis: Realtime voice/text assistant, PWA interface.
- Zerg: Backend orchestrator, LangGraph workflows, scheduling.
- MCP: Model Context Protocol for exposing tools to LLMs.
- AgentRun: Persistent record of a workflow execution.
- SSE: Server-Sent Events streaming run updates to Jarvis.
- Task Inbox: Jarvis UI component summarizing active/completed runs.
- Dispatch: Jarvis-initiated request for Zerg to execute an agent.
- Cron agents: Zerg agents scheduled via APScheduler.

———

### 13. Immediate Action Checklist

1. Clone both repos and merge into monorepo structure.
2. Update package managers, Makefiles, and docs to reflect new layout.
3. Create /api/jarvis router skeleton (auth, agents, runs).
4. Generate initial OpenAPI client; expose minimal UI in Jarvis listing agents.
5. Implement SSE endpoint and front-end listener; scaffold Task Inbox UI.
6. Add text input to PWA with same dispatch path.
7. Seed one demo agent + cron schedule; verify end-to-end from phone.
8. Document flows in docs/jarvis_integration.md.
9. Iterate on polishing PWA (manifest, icons, instructions).
10. Plan next features (tool manifest sync, notifications) in backlog.

———

This document is designed as a single source to reboot development: it outlines architecture, tasks,
file locations, and goals without assuming prior knowledge or access to past discussions.
