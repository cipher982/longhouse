# Supervisor UI Specification

**Version:** 1.1
**Date:** December 2024
**Status:** Draft for Review
**Depends on:** super-siri-architecture.md

---

## 1. Design Philosophy

### 1.1 The Core Tension

The supervisor/worker system introduces a fundamental UX question:

> **How much of the orchestration machinery should users see?**

Two extremes exist:

- **Full transparency**: Show every worker, every tool call, every intermediate result
- **Complete abstraction**: Hide everything, just show final answers

Neither extreme serves users well. Full transparency overwhelms. Complete abstraction removes trust and debuggability.

### 1.2 Recommended Approach: "Narrative Transparency"

**Show the story, not the machinery.**

Users see:

- What the assistant is doing ("Checking your servers...")
- Progress through meaningful phases ("Found an issue, investigating...")
- The synthesized result with confidence
- Honest caveats when data is incomplete

Users don't see (unless they ask):

- Individual worker IDs
- Tool call payloads
- Token counts
- Raw SSH output

**Mental model**: Like a capable colleague who says "I looked into it - here's what I found" rather than dumping their entire investigation log.

### 1.3 Guiding Principles

| Principle                   | Implication                                                 |
| --------------------------- | ----------------------------------------------------------- |
| **One Brain**               | Never expose "workers" as separate entities to casual users |
| **Earned Complexity**       | Details available on demand, not by default                 |
| **Conversation First**      | Progress appears as natural dialogue, not dashboards        |
| **Trust Through Narrative** | Show reasoning path, not raw data                           |
| **Debug Escape Hatch**      | Power users can always drill down                           |

### 1.4 Design Invariants (Hard Rules)

These are non-negotiable rules that all implementations must follow:

1. **No Implementation Leakage**
   - Jarvis NEVER mentions "workers", "tools", or "agents" to end users
   - Only speak in terms of _actions_ ("checking", "analyzing") and _findings_

2. **Acknowledgment Guarantee**
   - Every supervisor run MUST acknowledge within 500ms
   - MUST surface at least one "what I'm doing" narrative within 5 seconds
   - MUST end in either a result OR a clear failure state (never hang silently)

3. **Actionable Results**
   - Any result with `status: 'problem'` or `status: 'warning'` MUST include:
     - At least one recommended action, OR
     - An explicit "no action required" statement
   - Never leave users with a problem and no next step

4. **Graceful Degradation**
   - If part of an investigation fails, show partial results with caveat
   - Never show raw errors, stack traces, or "undefined"

---

## 2. Interface Architecture

### 2.1 Two Interfaces, One Experience

```
┌─────────────────────────────────────────────────────────────────┐
│                         JARVIS                                   │
│                   (Primary Interface)                            │
│                                                                  │
│  User's daily driver. Voice or text. Simple, conversational.    │
│  Shows narrative progress. Hides implementation details.         │
│                                                                  │
│  "Check my servers" → "Looking into it..." → "All healthy."     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ User clicks "Show details" or
                              │ explicitly asks for debug view
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      ZERG DASHBOARD                              │
│                    (Debug Interface)                             │
│                                                                  │
│  Power user view. Full artifact browser. Worker inspection.      │
│  Thread history. Tool call logs. Cost analytics.                 │
│                                                                  │
│  Shows: worker_id, tool_calls/*.txt, thread.jsonl, metrics      │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Jarvis: The Primary Interface

Jarvis remains conversational. Supervisor integration should feel like talking to a more capable assistant, not operating a new system.

**Key UX decisions:**

1. **No mode switching UI** - User doesn't choose "quick" vs "supervisor"
2. **Seamless delegation** - Complex tasks just take longer, naturally
3. **Progress as dialogue** - Updates appear as assistant messages
4. **Results as conversation** - Findings spoken/displayed naturally

### 2.3 Explanatory Debugging (In-Jarvis)

Users can ask "How did you figure that out?" and get a short, human explanation without opening the dashboard.

Every supervisor result offers two transparency levels:

- **[How did you figure this out?]** → Jarvis explains reasoning in conversation
- **[Show details]** → Opens dashboard with full artifacts

This provides lightweight transparency for curious users without forcing them into the power-user interface.

### 2.4 Zerg Dashboard: The Debug Interface

The dashboard becomes the "view source" for power users:

1. **Supervisor Runs** - Browse all past investigations
2. **Worker Artifacts** - Inspect individual worker outputs
3. **Thread Inspector** - See full conversation history
4. **Analytics** - Cost, duration, success rates

---

## 3. Jarvis UX Flows

### 3.1 Quick Query (No Supervisor)

```
User: "What time is it?"

┌──────────────────────────────────────┐
│  🎤 "It's 3:47 PM"                   │
│                                      │
│  [instant response, no delegation]   │
└──────────────────────────────────────┘
```

No change from current behavior. OpenAI Realtime handles directly.

### 3.2 Supervisor Query (Investigation)

```
User: "Check if my backups are healthy"

┌──────────────────────────────────────┐
│  🔍 Looking into your backups...     │  ← Phase 1: Acknowledgment (< 500ms)
│     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
└──────────────────────────────────────┘
         │
         │ [< 5 seconds]
         ▼
┌──────────────────────────────────────┐
│  🔍 Gathering data...                │  ← Phase 2: Activity (< 5s)
│     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│                                      │
│  📋 Checking cube backup status...   │
└──────────────────────────────────────┘
         │
         │ [several seconds]
         ▼
┌──────────────────────────────────────┐
│  🔍 Analyzing...                     │  ← Phase 3: Analysis
│     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│                                      │
│  📋 Checking cube backup status...   │
│  📋 Checking clifford snapshots...   │
└──────────────────────────────────────┘
         │
         │ [completion]
         ▼
┌──────────────────────────────────────┐
│  ✅ Your backups look healthy.       │  ← Phase 4: Result
│                                      │
│  All 4 servers have recent Kopia     │
│  snapshots. Cube backed up 2 hours   │
│  ago, clifford 4 hours ago.          │
│                                      │
│  [How did you figure this out?]      │  ← In-Jarvis explanation
│  [Show details]                      │  ← Dashboard deep link
└──────────────────────────────────────┘
```

### 3.3 Supervisor Query (Problem Found)

```
User: "Why is clifford slow?"

┌──────────────────────────────────────┐
│  🔍 Investigating clifford...        │
│     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│                                      │
│  📋 Checking system resources...     │
│  📋 Reviewing container health...    │
│  ⚠️  Found something...              │  ← Narrative tension
└──────────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────┐
│  ⚠️  Found the issue.                │  ← Clear problem statement
│                                      │
│  The postgres container is using     │
│  94% of available memory. This is    │
│  causing swap thrashing.             │
│                                      │
│  💡 Recommendation: Increase the     │  ← REQUIRED for warnings/problems
│  memory limit or investigate which   │
│  queries are consuming memory.       │
│                                      │
│  [How did you figure this out?]      │
│  [Show details] [Take action]        │
└──────────────────────────────────────┘
```

### 3.4 Supervisor Query (Partial Failure)

When some checks succeed but others fail:

```
┌──────────────────────────────────────┐
│  ⚠️  Partial results available.      │
│                                      │
│  I checked cube and zerg - both      │
│  look healthy. However, I couldn't   │  ← Honest caveat
│  reach clifford, so this picture     │
│  may be incomplete.                  │
│                                      │
│  💡 You may want to check clifford   │
│  manually or try again later.        │
│                                      │
│  [How did you figure this out?]      │
│  [Show details] [Try again]          │
└──────────────────────────────────────┘
```

### 3.5 Long-Running Investigation

For tasks > 30 seconds:

```
┌──────────────────────────────────────┐
│  🔍 Deep investigation in progress   │
│     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│                                      │
│  This is taking a while. I'll        │
│  notify you when I have findings.    │
│                                      │
│  [Continue in background] [Cancel]   │  ← User can dismiss OR cancel
└──────────────────────────────────────┘
```

Result delivered to **Task Inbox** when ready.

### 3.6 Cancellation

Users can cancel investigations at any time:

**Voice**: "Cancel that", "Stop", "Never mind", "Forget it"
**UI**: Click `[Cancel]` or `✕` button

Cancellation response:

```
┌──────────────────────────────────────┐
│  ✓ Stopped the investigation.        │
│                                      │
│  [Start over]                        │
└──────────────────────────────────────┘
```

Backend: `POST /api/jarvis/supervisor/{run_id}/cancel`

---

## 4. Progress Indicator Design

### 4.1 Phase Labels

Map backend events to three user-understandable phases:

| Phase | Label                        | When                                      |
| ----- | ---------------------------- | ----------------------------------------- |
| 1     | "Gathering data..."          | Workers spawning/executing                |
| 2     | "Analyzing..."               | Workers completing, supervisor processing |
| 3     | "Writing up what I found..." | Supervisor generating final response      |

### 4.2 Activity Narrative

Instead of showing "Worker 1", "Worker 2", show what's happening:

| Backend Event                                | User-Facing Narrative                           |
| -------------------------------------------- | ----------------------------------------------- |
| `worker_spawned: "Check disk usage on cube"` | "Checking disk space..."                        |
| `worker_spawned: "Review docker containers"` | "Reviewing containers..."                       |
| `worker_complete: success`                   | [narrative disappears, next one shows]          |
| `worker_complete: failed`                    | "Had trouble checking X, trying another way..." |

### 4.3 Progress Bar Philosophy

The progress bar is **indeterminate** (pulsing/animated) not percentage-based.

Why: We can't predict how long investigations take. A percentage that stalls at 80% for 30 seconds destroys trust. A pulsing indicator says "working on it" without false precision.

### 4.4 Timing SLAs

| Milestone         | Target  | Fallback                                    |
| ----------------- | ------- | ------------------------------------------- |
| Acknowledgment    | < 500ms | Show "Processing..." immediately            |
| First narrative   | < 5s    | Show phase label even without worker detail |
| Background prompt | > 30s   | Offer "Continue in background"              |

### 4.5 Narrative Generation

Backend SSE events carry task descriptions. Frontend transforms them:

```javascript
// Backend event
{ "event_type": "worker_spawned", "task": "ssh cube df -h" }

// Frontend narrative (simple transform)
"Checking disk space on cube..."

// Or with LLM transform for complex tasks
{ "task": "Check kopia backup status and verify last 3 snapshots" }
→ "Verifying recent backups..."
```

Keep narratives short (< 40 chars), action-oriented, jargon-free.

---

## 5. Result Rendering

### 5.1 Result Structure

Supervisor results should have consistent structure:

```typescript
interface SupervisorResult {
  // Required fields
  runId: number; // For follow-ups and deep links
  status: "healthy" | "warning" | "problem" | "error";
  summary: string; // 1-2 sentence TL;DR
  debugUrl: string; // Dashboard deep link

  // Optional fields
  details?: string; // Expanded explanation
  recommendations?: string[]; // Required if status is warning/problem
  affectedSystems?: string[];

  // Transparency fields
  confidence?: "high" | "medium" | "low";
  caveats?: string[]; // e.g. ["Couldn't reach clifford"]

  // For "How did you figure this out?"
  reasoning?: string; // Human-readable explanation of approach
}
```

### 5.2 Visual Hierarchy

```
┌──────────────────────────────────────┐
│  [Status Icon]  [Summary - Bold]     │  ← Scannable at a glance
│                                      │
│  [Caveat if any - italic/muted]      │  ← Honest limitations
│                                      │
│  [Details paragraph if relevant]     │  ← Read if interested
│                                      │
│  💡 [Recommendations if any]         │  ← Actionable next steps
│                                      │
│  [How did you figure this out?]      │  ← Lightweight transparency
│  [Show details]                      │  ← Full debug view
└──────────────────────────────────────┘
```

### 5.3 Voice Rendering

For voice output, read only:

1. Status + Summary
2. Caveat (if any)
3. First recommendation (if any)
4. Offer to show details on screen

Example: "Your backups are healthy. All servers have recent snapshots. I couldn't reach clifford though, so you may want to check that one manually. Want me to show the details?"

---

## 6. Task Inbox

### 6.1 Purpose

Background tasks need a landing place. The **Task Inbox** is a "While you were away..." view in Jarvis.

### 6.2 Contents

```
┌──────────────────────────────────────┐
│  📥 Task Inbox (2 new)               │
│                                      │
│  ┌────────────────────────────────┐  │
│  │ ✅ Backup check complete        │  │
│  │    2 hours ago                  │  │
│  │    All servers healthy          │  │
│  └────────────────────────────────┘  │
│                                      │
│  ┌────────────────────────────────┐  │
│  │ ⚠️  Disk space warning          │  │
│  │    1 hour ago                   │  │
│  │    clifford at 85% capacity     │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
```

### 6.3 Notification Routing

| Result Status | Notification                                           |
| ------------- | ------------------------------------------------------ |
| `healthy`     | Task Inbox only                                        |
| `warning`     | Task Inbox + optional email                            |
| `problem`     | Task Inbox + email                                     |
| `error`       | Task Inbox + email + consider SMS for critical systems |

---

## 7. Dashboard Integration

### 7.1 New Dashboard Views

Add to Zerg Dashboard (not Jarvis):

**1. Supervisor Runs Page** (`/supervisor`)

- List of all supervisor runs with status, duration, timestamp
- Filter by: Status (✅/⚠️/❌), Time (1h/24h/7d/All)
- Free-text search across task and summary
- Click to expand → show workers, result, thread

**2. Worker Artifacts Browser** (`/supervisor/[runId]`)

- Tree view of worker directories
- View result.txt, metadata.json, tool_calls/\*
- Syntax highlighting for logs/JSON

**3. Thread Inspector** (`/supervisor/[runId]/thread`)

- Full conversation history
- Message-by-message view
- Token counts per message

### 7.2 "Show Details" Deep Link

When user clicks "Show details" in Jarvis, open:

```
https://zerg.yourdomain.com/supervisor/{run_id}
```

This provides the escape hatch without cluttering Jarvis.

---

## 8. Intent Routing (Jarvis-Side)

### 8.1 Decision Point

Jarvis must decide: handle directly (quick) or delegate (supervisor)?

**Chosen approach: OpenAI Realtime Tool**

Let the Realtime LLM decide. It already has context and can make nuanced decisions. The `route_to_supervisor` tool becomes the trigger:

```
User: "Check my servers"
LLM thinks: This needs investigation → calls route_to_supervisor tool
Jarvis: Receives tool call → dispatches to POST /api/jarvis/supervisor
```

This keeps Jarvis simple (just respond to tool calls) while leveraging LLM intelligence.

### 8.2 Fallback Behavior

If supervisor endpoint is unavailable or times out:

```
Jarvis: "I couldn't run a full investigation right now, but based on
        what I already know: [lighter-weight answer from LLM context]"
```

Never fail silently. Always provide some response.

### 8.3 Duplicate Prevention

If the same query is routed while a previous run is still active:

- Don't start a new run
- Instead: "I'm still working on that. Here's what I've found so far..."

---

## 9. State Management

### 9.1 Jarvis State

Minimal state in Jarvis:

```typescript
interface JarvisState {
  // Current supervisor run (if any)
  activeRun: {
    runId: number;
    threadId: number;
    status: "running" | "complete" | "error" | "cancelled";
    phase: "gathering" | "analyzing" | "writing";
    narratives: string[]; // Current activity narratives
    lastSeq: number; // For event deduplication
  } | null;

  // SSE connection
  eventSource: EventSource | null;

  // Task inbox
  pendingResults: SupervisorResult[];
}
```

### 9.2 SSE Event Handling

```typescript
const handleSupervisorEvent = (event: SupervisorEvent) => {
  // Dedupe via sequence number
  if (event.seq <= state.activeRun?.lastSeq) return;
  updateLastSeq(event.seq);

  switch (event.type) {
    case "supervisor_thinking":
      setPhase("gathering");
      setNarratives(["Starting investigation..."]);
      break;

    case "worker_spawned":
      addNarrative(humanizeTask(event.task));
      break;

    case "worker_complete":
      removeNarrative(event.taskId);
      if (allWorkersComplete()) setPhase("analyzing");
      break;

    case "supervisor_complete":
      setPhase("writing");
      setResult(event.result);
      closeEventSource();
      break;

    case "error":
      setStatus("error");
      setErrorMessage(event.message);
      closeEventSource();
      break;
  }
};
```

### 9.3 Event Idempotence

SSE connections can drop and reconnect. To handle this:

- Each event includes `run_id` and `seq` (monotonically increasing sequence number)
- Client dedupes via `(run_id, seq)` - ignore if already seen
- Client ignores out-of-order events

---

## 10. Error Handling

### 10.1 User-Facing Errors

| Error Type     | User Message                                                                |
| -------------- | --------------------------------------------------------------------------- |
| Timeout        | "This is taking longer than expected. I'll keep trying in the background."  |
| Worker failure | "Had trouble with part of the investigation. Here's what I found so far..." |
| Total failure  | "Sorry, I couldn't complete that investigation. [Try again] [Show details]" |
| Cancelled      | "Stopped the investigation. [Start over]"                                   |

### 10.2 Never Show to Users

- Stack traces
- Worker IDs
- Raw error messages
- "undefined" or null states

Always have a human-readable fallback.

---

## 11. Concurrent Investigations

### 11.1 Policy

Allow multiple concurrent runs per user, but:

- Main chat shows only **one** active investigation (most recent)
- Additional concurrent runs appear as small "pill" indicators
- Or in a "Recent investigations" sidebar/dropdown

### 11.2 UI Treatment

```
┌──────────────────────────────────────┐
│  [Main investigation UI...]          │
│                                      │
│  ┌──────────────────────────────┐   │
│  │ 🔄 Server check (running)    │   │  ← Pills for other active runs
│  │ 🔄 Backup verify (running)   │   │
│  └──────────────────────────────┘   │
└──────────────────────────────────────┘
```

---

## 12. Multi-Turn Conversations

### 12.1 Follow-Up Behavior

When user asks a follow-up like "Tell me more about that disk issue":

- **Same thread** (`thread_id`) - supervisor context preserved
- **New run** (`run_id`) - fresh investigation entry
- Supervisor can reference previous findings from thread history

### 12.2 Reference by Run

User can say "Run that check again" and Jarvis understands from context which `runId` to reference.

---

## 13. Action Buttons

### 13.1 Philosophy

"Take action" buttons are just **pre-filled follow-up requests**, not direct imperatives.

When user clicks `[Restart postgres]`:

- Jarvis sends: "Restart the postgres container on clifford"
- This triggers a new supervisor run with that task
- User sees progress and confirmation

### 13.2 Why Not Direct Execution?

- Maintains conversational model
- User sees what's happening
- Can be cancelled
- Audit trail preserved

---

## 14. Implementation Phases

### Phase 1: Foundation (1-2 days)

- [ ] `useSupervisorDispatch` hook - call supervisor endpoint
- [ ] `useSupervisorEvents` hook - consume SSE stream with deduplication
- [ ] Basic state management for active run
- [ ] Cancel endpoint and UI

### Phase 2: Progress UI (1-2 days)

- [ ] Progress indicator component (pulsing bar)
- [ ] Phase labels (Gathering → Analyzing → Writing)
- [ ] Activity narrative display
- [ ] Task-to-narrative transformation

### Phase 3: Result Rendering (1-2 days)

- [ ] Result display component with full schema
- [ ] Status icons (healthy/warning/problem)
- [ ] Confidence/caveats display
- [ ] "How did you figure this out?" handler
- [ ] "Show details" link generation

### Phase 4: Task Inbox (1 day)

- [ ] Inbox component
- [ ] Background result delivery
- [ ] Notification routing (email for warnings/problems)

### Phase 5: Dashboard Views (2-3 days)

- [ ] Supervisor runs list page with filters
- [ ] Worker artifacts browser
- [ ] Thread inspector

### Phase 6: Polish (1-2 days)

- [ ] Error handling refinement
- [ ] Voice output optimization
- [ ] Animation/transitions
- [ ] Mobile responsiveness
- [ ] Concurrent run pills

---

## 15. Success Criteria

The UI redesign succeeds if:

1. **Zero confusion** - Users never wonder "what's happening?"
2. **Earned trust** - Progress narrative builds confidence
3. **Honest caveats** - Limitations surfaced without alarming
4. **Clean escalation** - Details available but not intrusive
5. **Voice-first viable** - Full experience works without screen
6. **Debug capable** - Power users can inspect everything
7. **Cancellable** - Users can always stop and redirect

---

## Appendix A: Event Type Reference

| SSE Event             | Trigger          | Payload                                     |
| --------------------- | ---------------- | ------------------------------------------- |
| `supervisor_started`  | Run begins       | `{ run_id, thread_id, task, seq }`          |
| `supervisor_thinking` | LLM processing   | `{ message, seq }`                          |
| `worker_spawned`      | Worker queued    | `{ job_id, task, seq }`                     |
| `worker_started`      | Worker executing | `{ job_id, worker_id, seq }`                |
| `worker_complete`     | Worker finished  | `{ job_id, status, duration_ms, seq }`      |
| `supervisor_complete` | Final result     | `{ run_id, result: SupervisorResult, seq }` |
| `error`               | Failure          | `{ message, details, seq }`                 |

All events include `seq` for idempotent handling on reconnect.

---

## Appendix B: Narrative Examples

| Worker Task                           | User Narrative                   |
| ------------------------------------- | -------------------------------- |
| `ssh cube docker ps`                  | "Checking containers on cube..." |
| `ssh clifford df -h`                  | "Checking disk space..."         |
| `Check kopia snapshot status`         | "Verifying backups..."           |
| `Review nginx access logs for errors` | "Scanning recent logs..."        |
| `Query prometheus for memory usage`   | "Analyzing memory usage..."      |

Keep narratives: action verbs, present continuous, no jargon, under 40 chars.

---

## Appendix C: Confidence/Caveat Examples

| Situation              | Confidence | Caveats                                                  |
| ---------------------- | ---------- | -------------------------------------------------------- |
| All checks succeeded   | `high`     | (none)                                                   |
| One server unreachable | `medium`   | `["Couldn't reach clifford"]`                            |
| Using cached data      | `medium`   | `["Based on data from 2 hours ago"]`                     |
| Partial timeout        | `low`      | `["Some checks timed out", "Results may be incomplete"]` |

Voice rendering: "I'm reasonably confident, though I couldn't reach clifford so this is based only on cube and zerg."
