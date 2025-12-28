# Supervisor Tool Visibility Specification

**Version:** 1.0
**Date:** December 2025
**Status:** Partially Implemented (SSE + UI). Persistence pending.
**Parent Spec:** [supervisor-ui-spec.md](./supervisor-ui-spec.md)

---

## Problem Statement

When the supervisor calls tools directly (not via workers), users see **nothing**. The supervisor appears to "think" silently, then suddenly produces a response. This creates:

1. **Poor UX during execution** - No feedback while tools run (e.g., `get_current_location`, `web_search`)
2. **No transparency** - Users can't see what the agent actually did
3. **No debugging info** - When things fail, there's no trace of what was attempted
4. **Inconsistency** - Worker tools show progress, supervisor tools don't

### Example: Before This Feature

```
User: "where am I?"

[2 seconds of nothing...]

Jarvis: "You're at Central Park West, near 81st Street..."
```

User has no idea that `get_current_location` was called, what data was returned, or how long it took.

---

## Solution: Tool Calls as Conversation Artifacts

**Core Principle:** Every tool call (supervisor or worker) is a **conversation artifact** rendered inline before the assistant's response.

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Placement** | Inline, before assistant response | Clear causality (user message → tool → response) |
| **Persistence** | Session-scoped today; DB persistence planned | History rehydration is a separate phase |
| **Treatment** | Uniform for all tools | No per-tool UI rules or categories |
| **Disclosure** | Progressive (collapsed → expanded → raw) | Information density for power users |
| **Live Updates** | Real-time streaming | "Productive theater" - keeps users engaged |

### Example: After This Feature

```
User: "where am I?"

┌─────────────────────────────────────────────────────────────┐
│ 📍 get_current_location                              ✓ 0.2s │
└─────────────────────────────────────────────────────────────┘

Jarvis: "You're at Central Park West, near 81st Street..."
```

Click card to expand:

```
┌─────────────────────────────────────────────────────────────┐
│ 📍 get_current_location                              ✓ 0.2s │
├─────────────────────────────────────────────────────────────┤
│ › Input: {"device_id": "1"}                                 │
│ ‹ Result: 40.7857, -73.9683 (battery: 45%)                 │
│                                                             │
│ [▶ Show Raw]                                                │
└─────────────────────────────────────────────────────────────┘
```

---

## Architecture

### SSE Event Flow

```
Backend (tool execution)
    │
    ├── supervisor_tool_started   → tool_name, tool_call_id, args_preview
    ├── supervisor_tool_progress  → message, level (streaming logs)
    ├── supervisor_tool_completed → duration_ms, result_preview
    └── supervisor_tool_failed    → duration_ms, error
    │
    ▼
Frontend (SSE handler)
    │
    ├── EventBus emit → supervisor:tool_*
    │
    ▼
SupervisorToolStore
    │
    ├── Track tool state (Map<toolCallId, ToolCall>)
    ├── Live duration ticker (500ms)
    │
    ▼
ActivityStream + ToolCard (React)
    │
    └── Render inline in ChatContainer
```

### Data Model

```typescript
interface SupervisorToolCall {
  toolCallId: string;        // Stable ID linking all events
  toolName: string;
  status: 'running' | 'completed' | 'failed';
  runId: number;             // Supervisor run for correlation

  // Timing
  startedAt: number;
  completedAt?: number;
  durationMs?: number;

  // Progressive disclosure data
  argsPreview?: string;      // Collapsed view
  args?: object;             // Raw view
  resultPreview?: string;    // Expanded view
  result?: object;           // Raw view
  error?: string;
  errorDetails?: object;

  // Streaming logs
  logs: ToolLogEntry[];
}
```

---

## Implementation Status

### Phase 1: Backend Events ✅

| Component | Status | Location |
|-----------|--------|----------|
| SSE schema | ✅ Done | `schemas/sse-events.asyncapi.yml` |
| Generated types | ✅ Done | `apps/zerg/backend/zerg/generated/sse_events.py` |
| Supervisor context | ✅ Done | `apps/zerg/backend/zerg/services/supervisor_context.py` |
| Event emission | ✅ Done | `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py:682-845` |

**New SSE Events:**
- `supervisor_tool_started` - Emitted when supervisor calls a tool
- `supervisor_tool_progress` - Streaming progress/logs (future)
- `supervisor_tool_completed` - Tool finished successfully
- `supervisor_tool_failed` - Tool failed with error

### Phase 2: Frontend UI ✅

| Component | Status | Location |
|-----------|--------|----------|
| EventBus events | ✅ Done | `apps/zerg/frontend-web/src/jarvis/lib/event-bus.ts` |
| SSE handlers | ✅ Done | `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts:687-749` |
| Tool store | ✅ Done | `apps/zerg/frontend-web/src/jarvis/lib/supervisor-tool-store.ts` |
| ToolCard component | ✅ Done | `apps/zerg/frontend-web/src/jarvis/app/components/ToolCard.tsx` |
| ActivityStream | ✅ Done | `apps/zerg/frontend-web/src/jarvis/app/components/ActivityStream.tsx` |
| Chat integration | ✅ Done | `apps/zerg/frontend-web/src/jarvis/app/components/ChatContainer.tsx` |

### Phase 3: Persistence (Pending)

| Component | Status | Notes |
|-----------|--------|-------|
| DB schema | ⏳ Pending | `tool_calls` and `tool_events` tables |
| Store on emit | ⏳ Pending | Persist alongside AgentRunEvent |
| Rehydrate on load | ⏳ Pending | Load tools when fetching thread history |

### Phase 4: Progress Streaming (Future)

| Component | Status | Notes |
|-----------|--------|-------|
| Progress helper | ⏳ Pending | `emit_tool_progress()` utility for tools |
| Tool integration | ⏳ Pending | Add progress to web_search, http_request, etc. |

---

## UI Components

### ToolCard

Three-level progressive disclosure:

**Collapsed (default):**
```
┌───────────────────────────────────────────────────────────┐
│ 📍 get_current_location                            ✓ 0.2s │
└───────────────────────────────────────────────────────────┘
```

**Expanded (click):**
```
┌───────────────────────────────────────────────────────────┐
│ 📍 get_current_location                            ✓ 0.2s │
├───────────────────────────────────────────────────────────┤
│ › Input: {"device_id": "1"}                               │
│ ‹ Result: lat=40.7857, lon=-73.9683, battery=45%         │
│                                                           │
│ [▶ Show Raw]                                              │
└───────────────────────────────────────────────────────────┘
```

**Raw (click toggle):**
```
┌───────────────────────────────────────────────────────────┐
│ Input                                                     │
│ ┌─────────────────────────────────────────────────────┐   │
│ │ {                                                   │   │
│ │   "device_id": "1"                                  │   │
│ │ }                                                   │   │
│ └─────────────────────────────────────────────────────┘   │
│ Output                                                    │
│ ┌─────────────────────────────────────────────────────┐   │
│ │ {                                                   │   │
│ │   "lat": 40.785738,                                 │   │
│ │   "lon": -73.968335,                                │   │
│ │   "battery": 45,                                    │   │
│ │   "updated_at": "2025-12-27T20:22:12Z"             │   │
│ │ }                                                   │   │
│ └─────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────┘
```

### Status Indicators

| Status | Icon | Color | Animation |
|--------|------|-------|-----------|
| Running | ⏳ | Blue | Pulse |
| Completed | ✓ | Green | None |
| Failed | ✗ | Red | None |

### Tool Icons

| Tool | Icon |
|------|------|
| get_current_location | 📍 |
| get_whoop_data | 💓 |
| search_notes | 📝 |
| web_search | 🌐 |
| web_fetch | 🔗 |
| http_request | 📡 |
| spawn_worker | 🤖 |
| send_email | 📧 |
| get_current_time | ⏰ |
| Default | 🔧 |

---

## Testing

### Manual Test Cases

1. **Basic tool display**
   - Send "where am I?" to Jarvis
   - Verify ToolCard appears with `get_current_location`
   - Verify duration updates live while running
   - Verify checkmark appears on completion

2. **Progressive disclosure**
   - Click card to expand
   - Verify args and result preview show
   - Click "Show Raw" toggle
   - Verify full JSON displayed

3. **Multiple tools**
   - Send query that triggers multiple tools (e.g., "what's the weather and where am I?")
   - Verify multiple cards stack vertically
   - Verify each updates independently

4. **Error handling**
   - Trigger a tool failure (e.g., invalid credentials)
   - Verify card shows error state (red, ✗)
   - Verify error message displayed in expanded view

### E2E Tests

E2E spec exists at `apps/zerg/e2e/tests/supervisor-tool-visibility.spec.ts`, but is currently skipped because it relies on dev-only event injection (`window.__jarvis.eventBus`). Unit tests cover the store and UI components.

---

## Security Considerations

1. **Credential redaction** - Tool args are passed through `redact_sensitive_args()` before emission
2. **Run isolation** - Events include `run_id` to prevent cross-run leakage
3. **Owner filtering** - SSE stream filters by owner_id

---

## References

- [ChatGPT Inline Surfaces](https://developers.openai.com/apps-sdk/concepts/ui-guidelines/) - "inline surfaces appear before the generated model response"
- [LangGraph Studio](https://blog.langchain.dev/langgraph-studio-the-first-agent-ide/) - Real-time agent execution visibility
- [Vercel Build Logs](https://vercel.com/docs/deployments/logs) - Streaming log UI patterns

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2025-12-27 | Initial spec, Phase 1-2 implemented |
