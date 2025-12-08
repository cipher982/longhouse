# Supervisor UI Specification

**Version:** 2.0
**Date:** December 2025
**Status:** Simplified Reference
**Parent Spec:** [super-siri-architecture.md](./super-siri-architecture.md)

---

## Overview

This document covers UI components for supervisor tasks. For full philosophy, see [super-siri-architecture.md](./super-siri-architecture.md).

**Core Principle:** Show what's happening, not how it's implemented.

---

## UI Components

### 1. Floating Progress Toast

```
┌─────────────────────────────────────┐
│ 🔍 Investigating...                 │
│                                     │
│ ⚙️ Checking servers...              │
│ ├─ ssh_exec "df -h" ✓ (1.8s)       │
│ └─ ssh_exec "docker ps" ⏳ 2s...    │
└─────────────────────────────────────┘
```

**Shows:** Status, task description, tool calls with status icons (⏳ running, ✓ done, ✗ failed), duration.

**Hides:** Worker IDs, job IDs, phase labels.

**Location:** `apps/jarvis/apps/web/lib/supervisor-progress.ts`

### 2. Result Display

Standard chat message in conversation. Supervisor's natural language response.

### 3. Error Display

```
I couldn't connect to cube via SSH. This could mean the server
is down or SSH credentials aren't properly configured.
```

Supervisor LLM interprets raw errors and explains them. No error classification middleware.

---

## State Management

```typescript
interface SupervisorState {
  isActive: boolean; // Is a supervisor task running?
  currentRunId: number | null; // Which run?
  workers: Map<number, WorkerInfo>; // Active workers
}
```

That's it. 3 fields, not 9.

---

## SSE Event → UI Mapping

| SSE Event               | UI Update                              |
| ----------------------- | -------------------------------------- |
| `supervisor:started`    | Show toast: "Investigating..."         |
| `worker:tool_started`   | Add tool to progress list with spinner |
| `worker:tool_completed` | Update tool with checkmark + duration  |
| `worker:tool_failed`    | Update tool with X + error             |
| `supervisor:complete`   | Hide toast, display result as message  |
| `supervisor:error`      | Hide toast, display error message      |

```typescript
eventBus.on("supervisor:started", () => showToast("Investigating..."));
eventBus.on("worker:tool_started", (d) =>
  addToolToProgress(d.toolName, "running"),
);
eventBus.on("worker:tool_completed", (d) =>
  updateTool(d.toolCallId, "done", d.durationMs),
);
eventBus.on("supervisor:complete", (d) => {
  hideToast();
  displayResult(d.result);
});
```

No phase mapping, no narrative transformation.

---

## What NOT to Build

| Concept                                        | Why Skip                           |
| ---------------------------------------------- | ---------------------------------- |
| Phase labels (Gathering → Analyzing → Writing) | Doesn't match LLM execution model  |
| "Narrative Transparency Protocol"              | Just "show progress" - standard UX |
| Activity narrative transformation              | Use task descriptions directly     |
| Complex state machine                          | 3 fields is enough                 |

---

## UX Flows

### Simple Request (No Supervision)

```
User: "What time is it?"
Jarvis: "It's 3:47 PM"
(No progress indicator - direct response)
```

### Delegated Task

```
User: "Check my servers"
Jarvis: "Let me check your servers." ← Acknowledgment
[Toast appears with live progress]
Jarvis: "Your servers are healthy..." ← Result
[Toast auto-hides]
```

### Follow-Up

```
User: "What about backups specifically?"
Jarvis: [Has context from previous turn]
Jarvis: "The backup ran at 3am successfully."
(May not need new worker if info already gathered)
```

---

## Current Implementation

Already correct in `apps/jarvis/apps/web/lib/supervisor-progress.ts`:

- Shows task descriptions directly
- Displays tool calls with simple status icons
- Uses floating toast (always visible)
- No phase labels or invented terminology

**No changes needed.**

---

_For full architecture, see [super-siri-architecture.md](./super-siri-architecture.md)._
