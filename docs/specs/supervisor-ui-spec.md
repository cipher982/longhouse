# Supervisor UI Specification

**Version:** 2.0
**Date:** December 2025
**Status:** Implementation Complete
**Philosophy:** Show progress, hide machinery. Simple UX principles, not invented protocols.

---

## Design Philosophy

### Core Principle

**Show what's happening, not how it's implemented.**

Users care about:

- ✅ "Is something happening?" (show progress)
- ✅ "What did you find?" (show results)
- ✅ "Can I see more?" (offer drill-down)

Users don't care about:

- ❌ Worker IDs, tool names, token counts
- ❌ "Phase labels" (Gathering → Analyzing → Writing)
- ❌ Implementation details unless they ask

### The Paid Intern Analogy

**When you ask an intern to investigate something:**

Good response:

> "Looking into it... Found the issue - postgres is using 94% memory. You should increase the limit or investigate slow queries."

Bad response:

> "Entered investigation mode. Spawned worker #47382. Tool call ssh_exec commenced. Status: gathering. Now analyzing. Result synthesis phase initiated..."

**Apply the same standard to Jarvis.**

---

## UI Components

### 1. Floating Progress Toast

**When supervisor tasks run:**

```
┌─────────────────────────────────────┐
│ 🔍 Investigating...                 │
│                                     │
│ ⚙️ Checking servers...              │
│ ├─ ssh_exec "df -h" ✓ (1.8s)       │
│ └─ ssh_exec "docker ps" ⏳ 2s...    │
└─────────────────────────────────────┘
```

**What's shown:**

- Status ("Investigating...")
- Current task description
- Tool calls with status icons (⏳ running, ✓ done, ✗ failed)
- Duration counters

**What's hidden:**

- Worker IDs (users don't care)
- Job IDs (implementation detail)
- "Phase labels" (invented terminology)

**Implementation:** Already complete in `apps/jarvis/apps/web/lib/supervisor-progress.ts`

### 2. Result Display

**After supervisor completes:**

```
┌─────────────────────────────────────┐
│ Assistant (Jarvis)                  │
├─────────────────────────────────────┤
│ Your servers are healthy. Cube is   │
│ at 78% disk usage, clifford has 22  │
│ containers running. Backup completed │
│ 4 hours ago successfully.           │
│                                     │
│ [Ask follow-up] [Show details]      │
└─────────────────────────────────────┘
```

**Implementation:** Standard chat message in conversation renderer

### 3. Error Display

**When things fail:**

```
┌─────────────────────────────────────┐
│ Assistant (Jarvis)                  │
├─────────────────────────────────────┤
│ I couldn't connect to cube via SSH. │
│ This could mean the server is down  │
│ or SSH credentials aren't properly  │
│ configured.                         │
│                                     │
│ [Try again] [Show details]          │
└─────────────────────────────────────┘
```

**Key:** Show user-friendly interpretation (from supervisor's LLM reasoning), not raw exception.

### 4. Drill-Down to Dashboard

**Users can always access full details:**

```
[Show details] → Opens: http://localhost:30081/runs/{run_id}
```

Dashboard shows:

- Full worker list with IDs
- All tool calls with raw outputs
- Thread conversation history
- Token usage and costs
- Timing breakdown

---

## UX Flows

### Flow 1: Simple Request (No Supervision)

```
User: "What time is it?"
  ↓
Jarvis: "It's 3:47 PM"
  ↓
Done (1-2s latency)
```

No progress indicator needed. Direct response.

### Flow 2: Delegated Task (Success)

```
User: "Check my server health"
  ↓
Jarvis: "Let me check your servers."  ← Acknowledgment
  ↓
[Floating toast appears]
🔍 Investigating...
⚙️ Checking servers...
  ├─ ssh_exec ✓ (1.8s)
  └─ ssh_exec ✓ (2.1s)
  ↓
Jarvis: "Your servers are healthy. Cube at 78% disk..."  ← Result
  ↓
[Toast auto-hides after 2s]
```

### Flow 3: Delegated Task (Error)

```
User: "Check disk space on cube"
  ↓
Jarvis: "Let me check that."
  ↓
[Toast appears]
🔍 Investigating...
⚙️ Checking disk...
  └─ ssh_exec ✗ Connection refused
  ↓
Jarvis: "I couldn't connect to cube via SSH..."  ← Interpreted error
  ↓
[Toast shows failed status, auto-hides after 3s]
```

### Flow 4: Follow-Up Question

```
User: "Check my servers"
Jarvis: [delegates, returns: "All healthy"]
  ↓
User: "What about the backup specifically?"
Jarvis: [has context, knows backup was checked]
Jarvis: "The backup ran at 3am, 157GB backed up successfully."
```

**Key:** Jarvis conversation history (8 turns) provides context. User doesn't repeat themselves.

---

## Implementation Guidelines

### What to Implement

1. **Floating progress toast** ✅ (already done)
   - Fixed position, always visible
   - Shows current activity
   - Tool call list with status icons

2. **Error interpretation** ✅ (already works)
   - Supervisor receives raw errors
   - LLM explains them intelligently
   - User sees friendly message

3. **Drill-down links** ⏳ (optional)
   - "Show details" → opens dashboard
   - Links to specific run_id

### What NOT to Implement

1. ❌ **"Phase labels"** (Gathering → Analyzing → Writing)
   - Invented terminology
   - Doesn't match how LLMs actually work
   - Just show what's actually happening

2. ❌ **"Narrative Transparency Protocol"**
   - Overcomplicated name for "show progress"
   - No need for special protocol

3. ❌ **"Activity narrative transformation"**
   - Converting backend events to prose
   - Just display the task description supervisor provides

4. ❌ **Complex state management**
   - v1.0 spec defined 9-field JarvisState
   - Just track: is_running, current_run_id, last_result

---

## SSE Event Handling

**Events from backend:**

```typescript
// Worker lifecycle
eventBus.on("supervisor:started", (data) => {
  showToast("Investigating...");
});

eventBus.on("worker:tool_started", (data) => {
  addToolToProgressUI(data.toolName, "running");
});

eventBus.on("worker:tool_completed", (data) => {
  updateToolStatus(data.toolCallId, "completed", data.durationMs);
});

eventBus.on("supervisor:complete", (data) => {
  hideToast();
  displayResult(data.result); // Show as chat message
});

eventBus.on("supervisor:error", (data) => {
  hideToast();
  displayError(data.message); // Show error in chat
});
```

**That's it.** Simple event handlers. No phase mapping, no narrative transformation.

---

## Design Decisions

### Decision 1: Progress Granularity

**v1.0 spec asked:** "How much detail to show?"

- Option A: Just "Working on it..."
- Option B: "Checking disk... Checking docker... Done"
- Option C: Real-time worker output streaming

**v2.0 answer:** Option B, but naturally.

Show task description supervisor provides:

- "Checking disk..." (from worker task: "Check disk usage")
- "Analyzing backup status..." (from worker task: "Check backup")

Don't invent phase labels or transform prose. Use what the supervisor already gives us.

### Decision 2: Voice During Long Tasks

**v1.0 spec asked:** "What does Jarvis say while waiting?"

**v2.0 answer:** Already solved in implementation.

Jarvis acknowledges immediately:

> "Let me check that for you."

Then visual progress toast shows activity. When complete, Jarvis speaks result.

No need for "still working..." ambient sounds or periodic updates.

### Decision 3: Drill-Down Access

**v1.0 spec asked:** "How does user access details?"

**v2.0 answer:** Two levels.

**Level 1: In-conversation explanation** (for curious users)

```
User: "How did you figure that out?"
Jarvis: "I SSH'd to cube and checked disk with df -h, then verified
         docker containers were running. Everything looked healthy."
```

**Level 2: Dashboard** (for power users)

```
[Show details] button → Opens dashboard at /runs/{run_id}
```

---

## State Management (Simplified)

**All you need:**

```typescript
interface SupervisorState {
  isActive: boolean; // Is a supervisor task running?
  currentRunId: number | null; // Which run?
  workers: Map<number, WorkerInfo>; // Active workers
}
```

**v1.0 had 9 fields.** We need 3.

---

## Error Handling

### Principle: Trust the LLM

**When errors occur:**

```
Backend error: "ForeignKeyViolation: constraint violation on agent_runs"
  ↓
Passed to supervisor's context (raw string)
  ↓
Supervisor (GPT-4o) interprets:
  "This is a database schema issue. Foreign key violations indicate
   data relationships are inconsistent. You may need to run:
   alembic upgrade head"
  ↓
Jarvis displays supervisor's interpreted message
```

**No custom error classification needed.** LLM reads exception type and explains it.

### What Users See

```
✅ Good error message:
"I encountered a database issue - looks like a schema migration might be needed.
 The error suggests a foreign key constraint violation. You may need to run:
 alembic upgrade head"

❌ Bad error message:
"ForeignKeyViolation: update or delete on table agent_runs violates foreign
 key constraint worker_jobs_supervisor_run_id_fkey"
```

Supervisor handles the translation. No middleware needed.

---

## Visual Design (Current Implementation)

### Progress Toast Styling

**Already implemented** in `apps/jarvis/apps/web/styles/supervisor-progress.css`:

- Fixed position at bottom-right
- Uses design tokens (`--color-void-light`, `--glow-primary`)
- Mobile-responsive (full-width on small screens)
- Attention pulse on start
- Smooth entrance/exit animations

**No changes needed.** Current implementation follows v2.0 philosophy.

---

## Testing Strategy

### Test Behaviors, Not UI Details

```typescript
// ✅ Good test
test("shows progress when supervisor task starts", async () => {
  await jarvis.ask("Check my servers");

  // Verify progress indicator appears
  expect(screen.getByText(/investigating/i)).toBeVisible();
});

// ✅ Good test
test("displays result after supervisor completes", async () => {
  await jarvis.ask("Check disk space");
  await waitFor(() => screen.getByText(/78% disk/i));

  expect(screen.getByText(/healthy/i)).toBeInTheDocument();
});

// ❌ Bad test
test("progress goes through gathering → analyzing → writing phases", () => {
  // Testing invented phase labels, not actual behavior
  expect(progressIndicator.phase).toBe("gathering");
  tick(5000);
  expect(progressIndicator.phase).toBe("analyzing");
  // ...this doesn't test anything users care about
});
```

---

## Migration from v1.0

### Remove

1. **"Narrative Transparency" terminology**
   - It's just "show progress" - standard UX
   - No need for invented protocol name

2. **Design Invariants 1-4**
   - These are just good UX practices
   - Don't frame as special constraints

3. **Phase Labels (Gathering → Analyzing → Writing)**
   - Doesn't match LLM execution model
   - Just show what's actually happening

4. **Activity Narrative Transformation**
   - v1.0 spec wanted to transform events into prose
   - Just use task descriptions directly

5. **9-field JarvisState**
   - Overcomplicated state management
   - Use: isActive, currentRunId, workers (3 fields)

### Keep

1. **Floating toast for progress** ✅
   - Always visible
   - Shows tool activity
   - Auto-hides on completion

2. **Error interpretation** ✅
   - Supervisor LLM explains errors
   - User sees friendly messages

3. **Drill-down to dashboard** ✅
   - Power users can inspect everything
   - Link to full artifacts

---

## Appendix: Event → UI Mapping

**Simple mapping (no transformation needed):**

| SSE Event               | UI Update                              |
| ----------------------- | -------------------------------------- |
| `supervisor:started`    | Show toast: "Investigating..."         |
| `worker:tool_started`   | Add tool to progress list with spinner |
| `worker:tool_completed` | Update tool with checkmark + duration  |
| `worker:tool_failed`    | Update tool with X + error             |
| `supervisor:complete`   | Hide toast, display result as message  |
| `supervisor:error`      | Hide toast, display error message      |

**That's it.** No phase detection, no narrative transformation, no complex state machine.

---

## Current Implementation (Already Correct)

The current implementation in `apps/jarvis/apps/web/lib/supervisor-progress.ts` already follows v2.0 philosophy:

- ✅ Shows task descriptions directly
- ✅ Displays tool calls with simple status icons
- ✅ Uses floating toast (always visible)
- ✅ No phase labels or invented terminology
- ✅ Clean state management

**No changes needed.** The implementation is simpler than the v1.0 spec suggested.

---

_End of Specification v2.0_

**Summary:** Show progress, hide implementation details. That's it. No need for "Narrative Transparency Protocol" or phase labels. The current implementation already does this correctly.
