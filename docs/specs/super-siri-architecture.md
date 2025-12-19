# Super Siri Architecture Specification

**Version:** 2.0
**Date:** December 2025
**Status:** Archived (superseded by v2.1)
**Philosophy:** Trust the AI. Remove scaffolding that limits capability.

> **Archived:** This doc describes the v2.0 design where Jarvis (OpenAI Realtime) is a decision-making agent that can call `route_to_supervisor`.
> We are moving to a stricter “one brain” architecture (Supervisor SSOT + all tools; Realtime optional I/O-only) in:
> `docs/specs/jarvis-supervisor-unification-v2.1.md`

---

## Executive Summary

Super Siri is a unified AI assistant that trusts modern LLM capabilities:

- **Jarvis**: Voice/text interface powered by OpenAI Realtime
- **Zerg Supervisor**: Backend intelligence that delegates when needed
- **Workers**: Autonomous agents with terminal access
- **Zerg Dashboard**: Power user inspection tools

**Core insight:** GPT-4o and Claude Opus are smart enough to figure things out. Give them context and autonomy, not rigid decision trees and pre-programmed behavior.

---

## Table of Contents

1. [Vision & Philosophy](#1-vision--philosophy)
2. [System Architecture](#2-system-architecture)
3. [Component Responsibilities](#3-component-responsibilities)
4. [User Experience Flows](#4-user-experience-flows)
5. [API Specifications](#5-api-specifications)
6. [Data Models](#6-data-models)
7. [Implementation Status](#7-implementation-status)
8. [Technical Considerations](#8-technical-considerations)

---

## 1. Vision & Philosophy

### 1.1 The "Paid Intern Test"

Super Siri behaves like a capable paid intern:

- **Given context and access** - Not pre-programmed responses
- **Trusted to make decisions** - Not keyword-routed to different modes
- **Has terminal access** - Can SSH to servers and figure out what to check
- **Asks when uncertain** - Not forced to guess from rigid rules
- **Learns from experience** - Reviews past work before duplicating effort

**What we DON'T do:**

- Pre-program decision rules ("if keyword 'check' then supervisor mode")
- Create specialized worker classes (BackupMonitorWorker, DiskHealthWorker)
- Build API wrappers for every system tool (give SSH access instead)
- Poll workers every 5 seconds asking "are you done yet?"
- Restrict tools via allowlists (use capability boundaries instead)

### 1.2 Core Principles

| Principle                        | Description                                      |
| -------------------------------- | ------------------------------------------------ |
| **Trust the LLM**                | GPT-4o/Claude can reason autonomously - let them |
| **Terminal is primitive**        | SSH access + context > curated tool lists        |
| **Behavior over implementation** | Test "can it answer this?" not "does it call X?" |
| **Event-driven**                 | Workers notify when done, don't poll them        |
| **Capability-based security**    | Scope by access (which hosts?), not tools        |

### 1.3 Success Metrics

- User can accomplish 80% of tasks through natural language alone
- Complex tasks complete without user micromanagement
- Context maintained across sessions (days/weeks)
- Agent explains errors intelligently without custom classification logic
- Power users can inspect every decision and artifact

---

## 2. System Architecture

### 2.1 Unified Request Flow

**All requests follow the same path:**

```
User → Jarvis (OpenAI Realtime) → Decides autonomously
                                    ↓
                        Need external access?
                     /                      \
                   NO                       YES
                    ↓                        ↓
            Answer directly          route_to_supervisor
            (get_current_time,       (delegates to backend)
             simple facts)                   ↓
                                    Zerg Supervisor
                                    (decides what's needed)
                                            ↓
                                    Spawn workers if complex
                                    OR handle directly if simple
                                            ↓
                                    Workers (autonomous agents)
                                    • SSH to servers
                                    • Run commands
                                    • Figure out what to check
                                    • Return natural language
                                            ↓
                                    Supervisor synthesizes
                                            ↓
                                    SSE → Jarvis → User
```

**Key change from v1.0:** No mode detection, no keyword routing. Jarvis (OpenAI Realtime) has `route_to_supervisor` as a tool. It decides when to call it based on the request.

**Examples:**

- "What time is it?" → Jarvis calls `get_current_time` directly
- "Check disk space on cube" → Jarvis calls `route_to_supervisor`, supervisor spawns worker
- "Is my backup working?" → Jarvis routes to supervisor, supervisor decides to spawn worker or check past workers

The LLM makes these decisions naturally - we don't pre-program them.

### 2.2 Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                         USER INTERFACES                           │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌──────────────────────┐         ┌─────────────────────────┐   │
│  │      JARVIS          │         │    ZERG DASHBOARD       │   │
│  │  (Unified Agent)     │         │   (Debug & Config)      │   │
│  │                      │         │                         │   │
│  │  • Voice/text I/O    │         │  • Worker artifacts     │   │
│  │  • OpenAI Realtime   │         │  • Thread inspector     │   │
│  │  • Decides: answer   │         │  • Cost analytics       │   │
│  │    directly OR       │         │  • Schedule manager     │   │
│  │    delegate          │         │  • Connector config     │   │
│  └──────────┬───────────┘         └──────────────┬──────────┘   │
│             │                                     │              │
└─────────────┼─────────────────────────────────────┼──────────────┘
              │                                     │
              │ route_to_supervisor                 │ Direct API
              │ (when needed)                       │
┌─────────────┼─────────────────────────────────────┼──────────────┐
│             ▼                                     ▼              │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    ZERG BACKEND                           │   │
│  ├──────────────────────────────────────────────────────────┤   │
│  │                                                           │   │
│  │  ┌────────────────┐    ┌──────────────┐    ┌──────────┐ │   │
│  │  │ Jarvis Router  │───▶│  Supervisor  │───▶│ Workers  │ │   │
│  │  │                │    │              │    │          │ │   │
│  │  │ • /supervisor  │◀───│ Autonomous   │◀───│ Terminal │ │   │
│  │  │ • /events (SSE)│    │ Delegation   │    │ Access   │ │   │
│  │  └────────────────┘    │              │    │          │ │   │
│  │                        │ Decides:     │    │ ssh_exec │ │   │
│  │                        │ - Spawn?     │    │ Figures  │ │   │
│  │                        │ - How many?  │    │ it out   │ │   │
│  │                        │ - What info? │    │          │ │   │
│  │                        └──────────────┘    └──────────┘ │   │
│  │                                                           │   │
│  │  ┌──────────────────────────────────────────────────┐    │   │
│  │  │              PERSISTENCE                          │    │   │
│  │  │  • PostgreSQL (threads, runs, messages)           │    │   │
│  │  │  • Filesystem (worker artifacts, audit trail)     │    │   │
│  │  └──────────────────────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Responsibilities

### 3.1 Jarvis (Frontend)

**Primary Role:** Unified AI interface that decides when to delegate.

| Responsibility       | Implementation                              |
| -------------------- | ------------------------------------------- |
| Voice I/O            | OpenAI Realtime (WebRTC)                    |
| Text fallback        | Standard input field                        |
| Decision-making      | Jarvis decides: answer OR delegate          |
| Progress display     | SSE subscription for delegated tasks        |
| Result rendering     | Text + optional TTS                         |
| Conversation history | Hydrates last 8 turns into Realtime session |

**Key difference from v1.0:** Jarvis doesn't route between "modes" - it's a single agent that calls `route_to_supervisor` tool when it determines delegation is needed.

### 3.2 Zerg Supervisor Agent

**Primary Role:** Autonomous orchestrator that figures out what's needed.

**What it does:**

- Receives tasks from Jarvis (or scheduled triggers)
- **Decides autonomously:** Do I need workers? How many? What should they do?
- Spawns workers as needed (not pre-programmed)
- Synthesizes results from multiple sources
- Maintains long-term memory in persistent thread
- Queries past work to avoid duplication

**What it does NOT do:**

- Follow keyword-based routing rules (it reasons about the request)
- Poll workers asking "are you done?" (workers notify when complete)
- Use pre-programmed decision trees (it makes judgment calls)

**Tools Available:**

```python
# Worker Delegation
spawn_worker(task, model)      # Autonomous: supervisor decides task description
list_workers(limit, status)    # Query past work (summaries only)
read_worker_result(worker_id)  # Drill into full result
read_worker_file(id, path)     # Access specific artifacts
grep_workers(pattern)          # Search across history
get_worker_metadata(worker_id) # Worker details

# Direct Execution
get_current_time()             # Current timestamp
http_request(url, method)      # Simple HTTP calls
send_email(to, subject, body)  # Notifications
```

### 3.3 Workers

**Primary Role:** Autonomous agents with terminal access.

**Philosophy:** Workers are paid interns with SSH access. They figure out what commands to run.

**What they have:**

- Terminal access via `ssh_exec(host, command)`
- Knowledge of available hosts: cube, clifford, zerg, slim
- Context about what they're checking
- Autonomy to decide: "I should run df -h, then docker ps, then check logs"

**What they DON'T have:**

- Pre-programmed command lists
- Specialized roles (no "BackupMonitorWorker" class)
- Rigid tool restrictions (no "allowed_tools: [ssh, curl]" allowlists)

**Worker Lifecycle:**

```
1. Supervisor: spawn_worker("Check server health")
2. Worker created with context:
   "You're checking server health.
    Available hosts: cube (GPU/home), clifford (prod VPS), zerg (projects)
    Figure out what to check: disk, docker, connectivity, backups, etc."
3. Worker reasons: "I should run df -h, docker ps, check kopia status"
4. Worker executes via ssh_exec
5. Worker interprets outputs and synthesizes answer
6. Returns: "All servers healthy. Cube 78% disk, clifford 12 containers running."
7. Artifacts persist, worker context discarded
```

### 3.4 Zerg Dashboard

**Primary Role:** Human inspection and configuration.

| Feature          | Purpose                                     |
| ---------------- | ------------------------------------------- |
| Agent list       | View/edit agents including supervisor       |
| Worker browser   | Inspect `/data/swarmlet/workers/` artifacts |
| Thread inspector | View full conversation history              |
| Tool outputs     | Raw SSH/HTTP outputs                        |
| Cost analytics   | Token usage, costs per run                  |
| Schedule manager | Cron jobs for background tasks              |

---

## 4. User Experience Flows

### 4.1 Simple Request

```
USER: "Hey Jarvis, what time is it?"
  ↓
JARVIS (OpenAI Realtime):
  • Reasons: "This is simple, I have get_current_time tool"
  • Calls: get_current_time()
  • Responds: "It's 3:47 PM"
  ↓
USER HEARS: "It's 3:47 PM"
Latency: ~1.5 seconds
```

### 4.2 Infrastructure Check

```
USER: "Check my server health"
  ↓
JARVIS (OpenAI Realtime):
  • Reasons: "This needs SSH access to check servers"
  • Responds: "Let me check your servers."
  • Calls: route_to_supervisor("Check my server health")
  ↓
POST /api/jarvis/supervisor {"task": "Check my server health"}
  ↓
SUPERVISOR (Zerg Backend):
  • Reasons: "Server health = disk + docker + connectivity + backups"
  • Decides: "I'll spawn one worker to check everything"
  • Calls: spawn_worker("Check health of cube, clifford, zerg: disk, docker, backups")
  ↓
WORKER (Autonomous Agent):
  • Reasons: "I should check each server with df -h, docker ps, kopia status"
  • ssh_exec('cube', 'df -h && docker ps && kopia snapshot list')
  • ssh_exec('clifford', 'df -h && docker ps')
  • ssh_exec('zerg', 'df -h && docker ps')
  • Interprets outputs
  • Returns: "All healthy. Cube 78% disk (safe), 15 containers running.
             Clifford 45% disk, 22 containers. Zerg 30% disk, 8 containers.
             Backup successful 4 hours ago (157GB)."
  ↓
SUPERVISOR:
  • Reads worker result
  • Synthesizes: "Your servers are healthy. Cube is at 78% disk capacity with
                  15 containers running. Backup completed 4 hours ago successfully."
  ↓ (via SSE)
JARVIS speaks result
  ↓
USER HEARS synthesized answer
Latency: ~15-30 seconds
```

**Key difference from v1.0:** No "quick mode" vs "supervisor mode" routing. Jarvis decides when to delegate. Supervisor decides whether to spawn workers. Workers decide what commands to run.

### 4.3 Follow-Up Query

```
USER: "What about backups specifically?"
  ↓
JARVIS:
  • Has conversation history (last 8 turns hydrated)
  • Reasons: "User just asked about servers, now narrowing to backups"
  • Calls: route_to_supervisor("What about backups specifically?")
  ↓
SUPERVISOR (Same Thread):
  • Has full context from previous request
  • Sees worker just checked backups 30 seconds ago
  • Reads: grep_workers("kopia") or read_worker_result(previous_worker_id)
  • Responds: "Backup ran successfully at 3am, 157GB backed up, no errors"
  ↓
USER HEARS detailed backup info without spawning new worker
```

### 4.4 Error Interpretation

```
USER: "Check disk space on cube"
  ↓
JARVIS → SUPERVISOR → WORKER
  ↓
WORKER fails with: ForeignKeyViolation
  ↓
SUPERVISOR receives:
  "Worker job 123 failed.
   Error: (psycopg2.errors.ForeignKeyViolation) update or delete on table..."
  ↓
SUPERVISOR (GPT-4o interprets):
  • Recognizes database integrity error
  • Reasons: "This is a schema issue, not infrastructure problem"
  • Responds: "I encountered a database issue - looks like a schema migration
              might be needed. The error suggests a foreign key constraint
              violation. You may need to run: alembic upgrade head"
  ↓
USER HEARS intelligent error explanation (no custom ErrorContext code needed)
```

**Key difference from v1.0:** No error classification middleware. LLM reads raw error and explains it.

---

## 5. API Specifications

### 5.1 POST /api/jarvis/supervisor

**Purpose:** Delegate a task to the supervisor agent.

**Request:**

```json
{
  "task": "Check my server health",
  "context": {
    "conversation_id": "jarvis-session-123",
    "previous_messages": [] // Optional: if Jarvis needs to pass context
  }
}
```

**Response:**

```json
{
  "run_id": 456,
  "thread_id": 789,
  "status": "running",
  "stream_url": "/api/jarvis/events?run_id=456"
}
```

### 5.2 GET /api/jarvis/events

**Purpose:** SSE stream for real-time progress.

**Events:**

```
event: supervisor_started
data: {"run_id": 456, "task": "Check my server health"}

event: worker_spawned
data: {"job_id": 1, "task": "Check health of all servers"}

event: worker_started
data: {"job_id": 1, "worker_id": "2025-12-07T20-33-15_..."}

event: worker_complete
data: {"job_id": 1, "status": "success", "duration_ms": 16547}

event: worker_summary_ready
data: {"job_id": 1, "summary": "All servers healthy..."}

event: supervisor_complete
data: {"run_id": 456, "result": "Your servers are healthy..."}

event: error
data: {"message": "Worker timeout", "run_id": 456}
```

### 5.3 Worker Tool Events (UI Only)

**Optional events for UI activity ticker:**

```
event: worker_tool_started
data: {"worker_id": "...", "tool_name": "ssh_exec", "args_preview": "cube: df -h"}

event: worker_tool_completed
data: {"worker_id": "...", "tool_name": "ssh_exec", "duration_ms": 2500}
```

**Note:** These are for UI polish (showing live progress), NOT for supervisor decision-making. Supervisor doesn't poll worker status - workers complete and notify.

---

## 6. Data Models

### 6.1 Supervisor Thread (Long-Lived)

```python
Thread:
  id: int
  agent_id: int              # Supervisor agent ID
  title: str                 # "Jarvis Session 2025-12-07"
  thread_type: "supervisor"
  active: bool
  agent_state: JSON          # Core memory, learned facts
  created_at: datetime
  updated_at: datetime
```

**Key principle:** One thread per user, lives forever. Each task is a new Run attached to this thread.

### 6.2 Worker Artifacts (Immutable Audit Trail)

```
/data/swarmlet/workers/{worker_id}/
├── metadata.json
│   {
│     "worker_id": "2025-12-07T20-33-15_check-health",
│     "owner_id": 1,
│     "task": "Check health of all servers",
│     "status": "success",           // System-determined
│     "summary": "All healthy...",   // LLM-generated (best-effort)
│     "started_at": "2025-12-07T20:33:15Z",
│     "completed_at": "2025-12-07T20:33:32Z",
│     "duration_ms": 16547,
│     "model": "gpt-4o-mini",
│     "supervisor_run_id": 456
│   }
├── result.txt              # Canonical worker output
├── thread.jsonl            # Canonical execution trace
└── tool_calls/
    ├── 001_ssh_exec.txt    # Raw outputs
    └── 002_ssh_exec.txt
```

**Canonical artifacts:**

- `result.txt` = source of truth (worker's natural language answer)
- `thread.jsonl` = full execution trace
- `metadata.json` = derived views (summary is LLM-generated, can fail)

**Philosophy:** Artifacts are immutable audit trail. Everything persists. Nothing is lost.

### 6.3 Supervisor Configuration

```python
{
  "name": "Supervisor",
  "model": "gpt-4o",  # or claude-opus-4-5
  "system_instructions": SUPERVISOR_PROMPT,
  "config": {
    "is_supervisor": True,
    "temperature": 0.7,
    "max_tokens": 2000
  }
}
```

**Note:** No `allowed_tools` allowlist. Supervisor has access to all delegation + direct tools. Safety is via capability boundaries (which hosts can SSH to, which emails can send from), not tool restrictions.

---

## 7. Implementation Status

### ✅ Phase 1: Foundation (COMPLETE)

- PostgreSQL persistence with LangGraph checkpointing
- Worker artifact store (filesystem)
- Worker execution runtime
- Supervisor tools (spawn, list, read, grep, metadata)
- Owner-based security filtering

### ✅ Phase 2: Jarvis Integration (COMPLETE)

| Component                                    | Status |
| -------------------------------------------- | ------ |
| `POST /api/jarvis/supervisor` endpoint       | ✅     |
| SSE event streaming                          | ✅     |
| `route_to_supervisor` tool in Jarvis         | ✅     |
| Floating progress UI                         | ✅     |
| Tool acknowledgment prompting                | ✅     |
| Conversation history hydration               | ✅     |
| ForeignKeyViolation fix (ON DELETE SET NULL) | ✅     |
| UI retry bug fix                             | ✅     |

**Deliverable achieved:** User can say "check my servers" and Jarvis autonomously delegates to supervisor, shows progress, returns synthesized result.

### ✅ Phase 2.5: Summary Extraction (COMPLETE)

- Workers generate 150-char summaries on completion
- `list_workers()` returns summaries only (not full results)
- Supervisor can scan 100+ workers without context overflow
- Graceful fallback if summary generation fails

### 📋 Phase 3: Infrastructure Intelligence (NEXT)

**OLD APPROACH (v1.0):**

> Create specialized workers: BackupMonitorWorker, DiskHealthWorker, DockerHealthWorker

**NEW APPROACH (v2.0):**

> Write behavior-focused E2E tests. Trust workers to figure out what to check.

**What to build:**

| Goal           | Test Scenario             | Expected Behavior                       |
| -------------- | ------------------------- | --------------------------------------- |
| Backup health  | "Is my backup working?"   | Worker checks kopia status, interprets  |
| Disk capacity  | "How's my disk space?"    | Worker runs df -h, analyzes usage       |
| Docker status  | "Are containers healthy?" | Worker checks docker ps, restart counts |
| General health | "Check my servers"        | Worker decides what to check            |

**Implementation:**

```typescript
// E2E test with mocked SSH responses
test("can answer: is backup working?", async () => {
  mockSSH("cube", /kopia snapshot list/, MOCK_KOPIA_SUCCESS);

  const answer = await ask("Is my backup working?");

  expect(answer).toContain("successful");
  expect(answer).toContain("last ran");
});
```

**Deliverable:** Supervisor can answer real infrastructure questions. Workers autonomously figure out what commands to run.

### 📋 Phase 4: Memory & Context

**Goal:** Supervisor learns and remembers.

- Core memory (facts stored in agent_state)
- Conversation summarization (compress old turns)
- Cross-session context ("Did we check this before?")

**Deliverable:** Supervisor remembers infrastructure state, past issues, user preferences.

### 📋 Phase 5: Background & Alerts

**Goal:** Scheduled checks with intelligent notifications.

- Scheduled supervisor runs (morning health check)
- **Autonomous severity assessment** (LLM decides if it's urgent, not hardcoded thresholds)
- Multi-channel notifications (email for routine, SMS for critical)

**Example:**

```
6am: Supervisor runs scheduled health check
  ↓
Spawns worker: "Check all infrastructure"
  ↓
Worker finds: Disk 95% full on cube
  ↓
Supervisor reads result, reasons: "95% is critical - notify immediately"
  ↓
Sends: Email + SMS alert
```

**Key difference:** LLM decides urgency (not hardcoded "if > 80% then alert").

---

## 8. Technical Considerations

### 8.1 Security Model

**Capability-based, not restriction-based:**

| Capability      | Boundary                                    | Enforcement               |
| --------------- | ------------------------------------------- | ------------------------- |
| SSH access      | Host allowlist (cube, clifford, zerg, slim) | `ssh_exec` validates host |
| Email sending   | From address scope (alerts@drose.io)        | SMTP configuration        |
| Database access | Read-only for workers                       | Postgres role permissions |
| File access     | Worker artifact directories only            | Filesystem permissions    |

**What we DON'T do:**

- Model allowlists (anyone can use any model, token-limited)
- Tool allowlists (workers have capabilities, not tool restrictions)
- Pre-programmed decision rules (LLMs decide)

**What we DO:**

- Output token caps (hard limit on generation)
- Per-user quota (rate limiting)
- Audit everything (tool_calls/ artifacts)
- Owner-based filtering (cross-tenant isolation)

### 8.2 Cost Management

| Control         | Mechanism                                       |
| --------------- | ----------------------------------------------- |
| Token limit     | max_tokens per run                              |
| Rate limit      | N runs per day per user                         |
| Model selection | Configurable (gpt-4o, gpt-4o-mini, claude-opus) |
| Usage tracking  | Token counts in run metadata                    |

**Philosophy:** Control costs via token limits and rate limiting, not by restricting which models agents can use.

### 8.3 Error Handling

**Autonomous error interpretation:**

```
Worker fails with: "ForeignKeyViolation: constraint violation"
  ↓
Supervisor receives raw error string
  ↓
Supervisor (LLM) interprets:
  "This is a database schema issue. Foreign key violations mean
   data relationships are inconsistent. You may need to run migrations."
  ↓
User sees intelligent explanation (no ErrorContext classification needed)
```

**What we DON'T do:**

- Pre-classify errors into categories (DATABASE_CONSTRAINT, NETWORK_TIMEOUT, etc.)
- Build error transformation pipelines
- Create ErrorContext dataclass with suggested_action fields

**What we DO:**

- Pass raw errors to supervisor's context
- Trust LLM to read exception type and message
- Let LLM suggest remediation based on error content

### 8.4 Latency Budget

| Component             | Target  | Notes                    |
| --------------------- | ------- | ------------------------ |
| Jarvis response       | < 2s    | Direct tool calls        |
| Supervisor delegation | < 60s   | Complex multi-step tasks |
| Worker execution      | < 300s  | Timeout after 5 minutes  |
| SSE event latency     | < 500ms | Near real-time updates   |

### 8.5 Guardrails (Not Heuristics)

**Critical distinction:** Guardrails are NOT heuristics. They don't tell the LLM what to decide - they're hard limits that prevent runaway resources.

| Guardrail                      | Limit | Purpose                 | Enforcement          |
| ------------------------------ | ----- | ----------------------- | -------------------- |
| Worker timeout                 | 300s  | Prevent runaway workers | System kills process |
| SSH command timeout            | 60s   | Protect servers         | asyncio.timeout      |
| Max workers per supervisor run | 5     | Cost control            | spawn_worker fails   |
| Output tokens per run          | 4000  | Cost control            | LLM truncates        |
| Max SSH commands per worker    | 20    | Prevent loops           | Tool raises error    |

**What makes these NOT heuristics:**

```python
# HEURISTIC (bad) - pre-programs LLM decisions:
if elapsed > 60:
    return "CANCEL"  # LLM should decide this

# GUARDRAIL (good) - enforces hard limit:
async with asyncio.timeout(300):
    await worker.execute()  # System enforces, not LLM
```

**The key test:** Does this rule tell the LLM WHAT TO DECIDE, or does it enforce a RESOURCE BOUNDARY?

- "If disk > 80%, alert user" → Heuristic (LLM should judge severity)
- "Workers timeout after 300s" → Guardrail (resource limit, not judgment)

### 8.6 Command Safety Layer

**Dangerous command patterns require explicit override:**

```python
DANGEROUS_PATTERNS = [
    r'rm\s+-rf',              # Recursive delete
    r'>\s*/dev/sd',           # Write to block device
    r'DROP\s+(TABLE|DATABASE)', # SQL destruction
    r'docker\s+rm\s+-f',      # Force remove containers
    r'systemctl\s+(stop|disable)', # Service disruption
    r'chmod\s+777',           # Security risk
]

async def ssh_exec(host, command, allow_destructive=False):
    if not allow_destructive and matches_dangerous_pattern(command):
        raise DangerousCommandError(
            f"Command matches dangerous pattern. "
            f"Use allow_destructive=True if intentional."
        )
    # ... execute command
```

**Why this is safe:**

- LLM can still run destructive commands if needed (explicit flag)
- Prevents accidental destruction from prompt injection
- All attempts logged to audit trail
- Human review for patterns that trigger this

---

## 9. Implementation Principles

### 9.1 The Paid Intern Test

**For every design decision, ask:**

> "Would a paid intern need this scaffolding, or could they figure it out?"

**Examples:**

| Decision              | Paid Intern                              | AI Agent                       |
| --------------------- | ---------------------------------------- | ------------------------------ |
| "Check server health" | Figures out: df, docker ps, logs         | ✅ Worker figures it out       |
| Intent detection      | Understands complex vs simple            | ✅ Jarvis decides autonomously |
| Error interpretation  | Reads "ForeignKeyViolation" and explains | ✅ Supervisor interprets       |
| When to exit early    | Judges "I have enough info"              | ✅ Supervisor decides          |
| What to check first   | Prioritizes: disk > docker > backups     | ✅ Worker prioritizes          |

**Anti-patterns:**

```python
# WRONG: Pre-program decisions
if 'check' in keywords:
    route_to_supervisor()

# RIGHT: Let LLM decide
jarvis.call_tool_if_needed(route_to_supervisor)

# WRONG: Specialized workers
class BackupMonitorWorker:
    def run(self):
        return check_kopia()

# RIGHT: General workers
spawn_worker("Check if backup is healthy")
  # Worker figures out: "I should run kopia snapshot list"

# WRONG: Hardcoded thresholds
if disk_usage > 80%:
    severity = "WARNING"

# RIGHT: LLM judgment
supervisor_sees = "Disk is at 78%"
supervisor_decides = "That's fine, plenty of capacity"
```

### 9.2 Architectural Simplifications

**Removed from v1.0:**

- ❌ Intent detection keyword routing
- ❌ Three-mode system (quick/supervisor/background)
- ❌ Hardcoded decision heuristics
- ❌ Tool allowlists (`allowed_tools: [...]`)
- ❌ Worker polling loops (roundabout monitoring)
- ❌ Error classification middleware
- ❌ Specialized worker classes

**Kept from v1.0:**

- ✅ Supervisor/worker delegation pattern
- ✅ Worker artifact persistence
- ✅ One supervisor thread per user
- ✅ SSE event streaming
- ✅ Terminal-first tool philosophy
- ✅ Owner-based security

### 9.3 What Changed and Why

| v1.0 Concept              | v2.0 Change              | Reason                           |
| ------------------------- | ------------------------ | -------------------------------- |
| Quick vs Supervisor modes | Unified agent            | LLM decides when to delegate     |
| Intent detection keywords | Removed                  | Trust LLM to read request        |
| Roundabout polling loop   | Event-driven             | Workers notify, don't poll       |
| Specialized workers       | General workers          | Trust LLM to figure out commands |
| Tool allowlists           | Capability boundaries    | Security by access, not tools    |
| Error classification      | Raw error interpretation | LLM can read exception types     |

---

## Appendix A: Supervisor System Prompt

See `/apps/zerg/backend/zerg/prompts/supervisor_prompt.py` for full prompt.

**Key guidance:**

- You are the central brain coordinating work
- Spawn workers for complex/multi-step tasks
- Handle simple tasks directly
- Query past workers before duplicating work
- **Decide autonomously:** No rules tell you when to spawn workers
- **Interpret errors:** Read raw exceptions and explain to users
- **Exit early if you have enough info:** You judge, no heuristics force you

### Appendix B: Worker Context Template

When supervisor spawns a worker:

```
Task: Check server health

Context:
You have SSH access to these servers:
- cube (100.70.237.79) - Home GPU server, runs AI workloads, cameras
- clifford (5.161.97.53) - Production VPS, most web apps
- zerg (5.161.92.127) - Project server, dedicated workloads
- slim (135.181.204.0) - EU VPS, cost-effective workloads

Figure out what to check for "server health". Common things to look at:
- Disk usage (df -h)
- Docker containers (docker ps, check for restarts)
- Memory/CPU if relevant (free -h, top)
- Backups if applicable (kopia snapshot list)
- Connectivity (can you reach the server?)

Use your judgment. You're a capable intern with terminal access. Return a natural
language summary of what you found.
```

**Philosophy:** Give context, trust the worker to figure out the details.

---

## Appendix C: File Locations

| Component             | Location                                     |
| --------------------- | -------------------------------------------- |
| Supervisor prompt     | `zerg/prompts/supervisor_prompt.py`          |
| Supervisor tools      | `zerg/tools/builtin/supervisor_tools.py`     |
| Worker artifact store | `zerg/services/worker_artifact_store.py`     |
| Worker runner         | `zerg/services/worker_runner.py`             |
| Jarvis router         | `zerg/routers/jarvis.py`                     |
| Jarvis frontend       | `apps/jarvis/apps/web/`                      |
| History hydration     | `apps/jarvis/apps/web/lib/history-mapper.ts` |

---

## Appendix D: Migration from v1.0

**If you have v1.0 code:**

1. **Remove intent detection:**

   ```typescript
   // DELETE: Keyword routing logic
   if (keywords.includes('check', 'investigate'))...

   // KEEP: route_to_supervisor as a tool
   // Jarvis decides when to call it
   ```

2. **Remove roundabout monitoring:**

   ```python
   # DELETE: RoundaboutMonitor class and polling loop

   # KEEP: Event-driven notifications
   # Workers emit events, supervisor reacts
   ```

3. **Remove specialized workers:**

   ```python
   # DELETE: BackupMonitorWorker, DiskHealthWorker classes

   # KEEP: Generic spawn_worker()
   spawn_worker("Check if backup is healthy")
   ```

4. **Remove tool allowlists:**

   ```python
   # DELETE: allowed_tools: ["ssh_exec", "http_request"]

   # KEEP: Capability boundaries in tool implementations
   # ssh_exec validates host against allowlist
   ```

5. **Trust error interpretation:**

   ```python
   # DELETE: error_classifier.py, ErrorContext dataclass

   # KEEP: Raw error strings in supervisor context
   # Supervisor LLM interprets and explains
   ```

---

_End of Specification v2.0_

**Summary of v2.0 philosophy:**

> Modern LLMs are autonomous reasoners. Give them context, access, and trust. Remove the scaffolding that makes them less capable than they are. Test behaviors, not implementations. If a paid intern can figure it out, so can the AI.
