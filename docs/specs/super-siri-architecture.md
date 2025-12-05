# Super Siri Architecture Specification

**Version:** 1.0
**Date:** December 2024
**Status:** Draft for Review

---

## Executive Summary

Super Siri is a unified AI assistant that combines:
- **Jarvis**: Simple voice/text interface (the "face")
- **Zerg Supervisor**: Backend orchestration brain (the "brain")
- **Workers**: Disposable task executors (the "hands")
- **Zerg Dashboard**: Power user debug/config view

The architecture enables natural conversation for simple tasks while seamlessly delegating complex work to backend agents.

---

## Table of Contents

1. [Vision & Goals](#1-vision--goals)
2. [System Architecture](#2-system-architecture)
3. [Component Responsibilities](#3-component-responsibilities)
4. [User Experience Flows](#4-user-experience-flows)
5. [API Specifications](#5-api-specifications)
6. [Data Models](#6-data-models)
7. [Implementation Phases](#7-implementation-phases)
8. [Technical Considerations](#8-technical-considerations)
9. [Open Questions](#9-open-questions)

---

## 1. Vision & Goals

### 1.1 The "Intern" Mental Model

Super Siri behaves like a capable intern who:
- Handles simple requests directly ("What time is it?")
- Delegates complex investigations ("Check why the server is slow")
- Maintains context across all interactions ("Remember yesterday's backup failure?")
- Escalates appropriately (email for routine, SMS for urgent)
- Can receive direction ("Make that check weekly instead of daily")

### 1.2 Core Principles

| Principle | Description |
|-----------|-------------|
| **One Brain** | Single unified intelligence, not isolated agents |
| **Simple Interface** | Voice or text, no complex UI required |
| **Powerful Backend** | Full orchestration capabilities hidden from user |
| **Transparent Debugging** | Power users can inspect everything |
| **Context Preservation** | Nothing is lost, supervisor remembers all |

### 1.3 Success Metrics

- User can accomplish 80% of tasks through voice alone
- Complex tasks complete without user micromanagement
- Context maintained across sessions (days/weeks)
- Power users can debug any interaction

---

## 2. System Architecture

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              USER INTERFACES                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌────────────────────────┐         ┌────────────────────────────────────┐ │
│  │       JARVIS           │         │        ZERG DASHBOARD              │ │
│  │   (Primary Interface)  │         │      (Power User Debug)            │ │
│  │                        │         │                                    │ │
│  │  • Voice input/output  │         │  • Agent configuration             │ │
│  │  • Text chat fallback  │         │  • Worker artifact browser         │ │
│  │  • Progress indicators │         │  • Thread inspector                │ │
│  │  • Result display      │         │  • Cost/usage analytics            │ │
│  │                        │         │  • Schedule management             │ │
│  └──────────┬─────────────┘         └──────────────┬─────────────────────┘ │
│             │                                       │                       │
└─────────────┼───────────────────────────────────────┼───────────────────────┘
              │                                       │
              │  Simple: OpenAI Realtime              │  Direct DB/API
              │  Complex: Supervisor API              │
              │                                       │
┌─────────────┼───────────────────────────────────────┼───────────────────────┐
│             ▼                                       ▼                       │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         ZERG BACKEND                                 │   │
│  ├─────────────────────────────────────────────────────────────────────┤   │
│  │                                                                      │   │
│  │  ┌──────────────────┐    ┌──────────────────┐    ┌───────────────┐ │   │
│  │  │  Jarvis Router   │    │ Supervisor Agent │    │   Workers     │ │   │
│  │  │                  │    │                  │    │               │ │   │
│  │  │ /api/jarvis/*    │───▶│  • Delegation    │───▶│ • Disposable  │ │   │
│  │  │ • auth           │    │  • Synthesis     │    │ • SSH/HTTP    │ │   │
│  │  │ • supervisor     │◀───│  • Memory        │◀───│ • Persist all │ │   │
│  │  │ • events (SSE)   │    │  • Context       │    │ • Return text │ │   │
│  │  └──────────────────┘    └──────────────────┘    └───────────────┘ │   │
│  │                                   │                      │          │   │
│  │                                   ▼                      ▼          │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │                     PERSISTENCE LAYER                        │   │   │
│  │  │                                                              │   │   │
│  │  │  PostgreSQL              Filesystem                          │   │   │
│  │  │  • Agents                /data/swarmlet/workers/             │   │   │
│  │  │  • Threads               • Worker artifacts                  │   │   │
│  │  │  • Messages              • Tool outputs                      │   │   │
│  │  │  • Runs                  • Audit trail                       │   │   │
│  │  │  • Checkpoints                                               │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                                                      │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│                              ZERG BACKEND                                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 Request Flow Modes

#### Mode A: Quick Mode (Simple Tasks)
```
User → Jarvis → OpenAI Realtime → Response
       (< 2 seconds, voice-to-voice)
```

**Examples:**
- "What time is it?"
- "Tell me a joke"
- "What's on my calendar today?"

#### Mode B: Supervisor Mode (Complex Tasks)
```
User → Jarvis → Zerg Supervisor → Workers → Synthesis → Jarvis → User
       (5-60 seconds, with progress updates)
```

**Examples:**
- "Check my server health"
- "Research robot vacuums under $500"
- "Why did the backup fail last night?"

### 2.3 Intent Detection

Jarvis determines mode based on:

| Signal | Quick Mode | Supervisor Mode |
|--------|------------|-----------------|
| Keywords | "what", "when", "tell me" | "check", "investigate", "research", "why" |
| Complexity | Single fact/action | Multi-step process |
| Data needs | None or cached | Requires tool calls |
| Expected time | < 5 seconds | > 5 seconds |

**Implementation:** OpenAI Realtime function calling with `route_to_supervisor` tool.

---

## 3. Component Responsibilities

### 3.1 Jarvis (Frontend)

**Primary Role:** User-facing interface for voice and text interaction.

| Responsibility | Implementation |
|----------------|----------------|
| Voice I/O | OpenAI Realtime API (WebRTC) |
| Text fallback | Standard input field |
| Quick responses | Direct OpenAI Realtime |
| Complex delegation | POST to Zerg Supervisor API |
| Progress display | SSE subscription |
| Result rendering | Text + optional TTS |
| Session management | JWT from Zerg |

**Does NOT:**
- Store long-term conversation history (Zerg does this)
- Make orchestration decisions (Supervisor does this)
- Execute tools directly (Workers do this)

### 3.2 Zerg Supervisor Agent

**Primary Role:** Central intelligence that delegates and synthesizes.

| Responsibility | Implementation |
|----------------|----------------|
| Intent analysis | GPT-4o reasoning |
| Worker delegation | `spawn_worker` tool |
| Result synthesis | Combine worker outputs |
| Context maintenance | Thread + core_memory |
| Past work queries | `list_workers`, `grep_workers` |
| Error handling | Retry logic, fallbacks |

**Tools Available:**
```python
# Delegation
spawn_worker(task, model)      # Spawn worker agent
list_workers(limit, status)    # Query past workers (SUMMARIES ONLY)
read_worker_result(worker_id)  # Get full worker output
read_worker_file(id, path)     # Drill into artifacts
grep_workers(pattern)          # Search across workers
get_worker_metadata(worker_id) # Worker details

# Direct (simple tasks)
get_current_time()             # Timestamp
http_request(url, method)      # Simple HTTP
send_email(to, subject, body)  # Notifications
```

### 3.3 Workers

**Primary Role:** Disposable task executors that persist everything.

| Responsibility | Implementation |
|----------------|----------------|
| Execute single task | AgentRunner with task |
| Use domain tools | SSH, HTTP, Docker, etc. |
| Persist all outputs | WorkerArtifactStore |
| Return natural language | Final assistant message |
| Die after completion | No persistent state |

**Worker Lifecycle:**
```
1. Supervisor calls spawn_worker("Check disk on cube")
2. WorkerRunner creates worker directory
3. Worker agent executes with domain tools
4. All tool outputs saved to filesystem
5. Final result extracted and returned
6. Worker marked complete, context discarded
```

### 3.4 Zerg Dashboard

**Primary Role:** Power user interface for debugging and configuration.

| Feature | Purpose |
|---------|---------|
| Agent list | View/edit all agents including supervisor |
| Worker browser | Inspect `/data/swarmlet/workers/` |
| Thread inspector | View full conversation history |
| Tool outputs | Raw tool call results |
| Cost analytics | Token usage, costs per agent |
| Schedule manager | Cron jobs for background agents |
| Connector config | API keys, OAuth tokens |

### 3.5 Worker Tool Philosophy

**Principle: The terminal is the primitive, not a curated tool list.**

Workers are LLMs with shell access. They already know `df -h`, `docker ps`, `journalctl`,
`grep`, `jq`, etc. We don't model each command as a separate tool.

**Worker capabilities:**

| Capability | Tool | Purpose |
|------------|------|---------|
| Remote shell | `ssh_exec` | Execute commands on cube, clifford, zerg, slim |
| Local sandbox | `container_exec` | Sandboxed shell for safe experimentation |
| HTTP | `http_request` | curl-equivalent for APIs |
| Connectors | `send_email`, `send_slack`, etc. | Side effects shell can't do |

**What we DON'T do:**
- No tool profiles (INFRA, RESEARCH, COMMS)
- No auxiliary LLM "tool planner"
- No per-task tool restrictions

**Safety boundaries:**
- Host allowlist in `ssh_exec` (only known servers)
- Audit trail via `tool_calls/*.txt` artifacts
- Connector gating by owner_id if needed

---

## 4. User Experience Flows

### 4.1 Simple Question Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  USER: "Hey Jarvis, what time is it?"                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  JARVIS (OpenAI Realtime)                                       │
│  • Recognizes simple query                                      │
│  • Calls get_current_time tool                                  │
│  • Responds directly                                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  USER HEARS: "It's 3:47 PM"                                     │
│  Latency: ~1.5 seconds                                          │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Complex Task Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  USER: "Check my server health"                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  JARVIS (OpenAI Realtime)                                       │
│  • Recognizes complex task                                      │
│  • Responds: "I'll check your servers. One moment..."           │
│  • Calls route_to_supervisor(task="check server health")        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  JARVIS → ZERG                                                  │
│  POST /api/jarvis/supervisor                                    │
│  { "task": "Check my server health" }                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  ZERG SUPERVISOR                                                │
│  • Creates thread for this interaction                          │
│  • Analyzes: "server health = disk + docker + connectivity"     │
│  • spawn_worker("Check disk usage on cube and clifford")        │
│  • spawn_worker("Check docker containers on all servers")       │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│  WORKER 1               │     │  WORKER 2               │
│  • SSH to cube          │     │  • SSH to cube          │
│  • SSH to clifford      │     │  • SSH to clifford      │
│  • Run df -h            │     │  • Run docker ps        │
│  • Analyze results      │     │  • Check container      │
│  • Return summary       │     │    health               │
└──────────┬──────────────┘     └──────────┬──────────────┘
           │                               │
           └───────────────┬───────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  SUPERVISOR (Synthesis)                                         │
│  • Reads worker results                                         │
│  • Synthesizes: "All servers healthy. Cube at 78% disk,         │
│    clifford running 12 containers. No issues detected."         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (SSE)
┌─────────────────────────────────────────────────────────────────┐
│  JARVIS (Speaks Result)                                         │
│  "Your servers look good. Cube is at 78% disk usage.            │
│   Clifford has 12 containers running, all healthy."             │
│                                                                  │
│  Latency: ~15-30 seconds                                        │
└─────────────────────────────────────────────────────────────────┘
```

### 4.3 Follow-Up Query Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  USER: "What about the backup? Did it run last night?"          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  SUPERVISOR (Same Thread - Has Context)                         │
│  • Remembers: just checked servers                              │
│  • Checks: grep_workers("backup") - finds yesterday's worker    │
│  • Or: spawn_worker("Check Kopia backup status on cube")        │
│  • Synthesizes with context                                     │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  JARVIS: "The backup ran successfully at 3am. It took           │
│   17 seconds and backed up 157GB. No errors."                   │
└─────────────────────────────────────────────────────────────────┘
```

### 4.4 Background Task + Alert Flow

```
┌─────────────────────────────────────────────────────────────────┐
│  SCHEDULED: Daily 6am health check                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  SUPERVISOR (Background Mode)                                   │
│  • spawn_worker("Check all server health")                      │
│  • spawn_worker("Check backup status")                          │
│  • Synthesizes results                                          │
│  • Assesses severity                                            │
└─────────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
┌─────────────────────────┐     ┌─────────────────────────┐
│  Severity: INFO         │     │  Severity: WARNING      │
│  → Email digest         │     │  → Email + SMS          │
│  → Jarvis Task Inbox    │     │  → Jarvis Task Inbox    │
│                         │     │  → Push notification    │
└─────────────────────────┘     └─────────────────────────┘
```

---

## 5. API Specifications

### 5.1 Jarvis API Endpoints

#### POST /api/jarvis/supervisor

**Purpose:** Dispatch a task to the supervisor agent.

**Request:**
```json
{
  "task": "Check my server health",
  "context": {
    "conversation_id": "jarvis-session-123",
    "previous_messages": [
      {"role": "user", "content": "..."}
    ]
  },
  "preferences": {
    "verbosity": "concise",
    "notify_on_complete": false
  }
}
```

**Response:**
```json
{
  "run_id": 456,
  "thread_id": 789,
  "status": "running",
  "estimated_duration_seconds": 30,
  "stream_url": "/api/jarvis/events?run_id=456"
}
```

#### GET /api/jarvis/events

**Purpose:** SSE stream for supervisor progress.

**Events:**
```
event: supervisor_thinking
data: {"message": "Analyzing your request..."}

event: worker_spawned
data: {"worker_id": "2024-12-03T14-32-00_disk-check", "task": "Check disk usage"}

event: worker_complete
data: {"worker_id": "2024-12-03T14-32-00_disk-check", "status": "success"}

event: supervisor_complete
data: {"result": "Your servers look good...", "run_id": 456}

event: error
data: {"error": "Worker failed", "details": "SSH connection refused"}
```

#### GET /api/jarvis/runs/{run_id}

**Purpose:** Get details of a supervisor run.

**Response:**
```json
{
  "run_id": 456,
  "status": "success",
  "started_at": "2024-12-03T14:32:00Z",
  "completed_at": "2024-12-03T14:32:25Z",
  "duration_ms": 25000,
  "result": "Your servers look good...",
  "workers": [
    {
      "worker_id": "2024-12-03T14-32-00_disk-check",
      "task": "Check disk usage",
      "status": "success",
      "duration_ms": 12000
    }
  ],
  "thread_id": 789,
  "tokens_used": 4500,
  "cost_usd": 0.045
}
```

### 5.2 Internal Service APIs

#### WorkerRunner.run_worker()

```python
async def run_worker(
    db: Session,
    task: str,
    agent: AgentModel | None = None,
    agent_config: dict | None = None,
    timeout: int = 300,
) -> WorkerResult:
    """
    Execute a task as a disposable worker agent.

    Returns:
        WorkerResult with worker_id, status, result text, duration
    """
```

#### WorkerArtifactStore

```python
class WorkerArtifactStore:
    def create_worker(self, task: str, owner_id: int) -> str: ...
    def save_tool_output(self, worker_id: str, tool_name: str, output: str) -> None: ...
    def save_result(self, worker_id: str, result: str) -> None: ...
    def complete_worker(self, worker_id: str, status: str) -> None: ...
    def get_worker_result(self, worker_id: str, owner_id: int) -> str: ...
    def list_workers(self, owner_id: int, limit: int, status: str) -> list[dict]: ...
    def search_workers(self, owner_id: int, pattern: str) -> list[dict]: ...
```

---

## 6. Data Models

### 6.1 Supervisor Thread

```python
# Supervisor maintains a long-running thread for context
Thread:
  id: int
  agent_id: int  # Supervisor agent ID
  title: str  # "Jarvis Session 2024-12-03"
  thread_type: "supervisor"
  active: bool
  agent_state: JSON  # Core memory, facts
  created_at: datetime
  updated_at: datetime
```

### 6.2 Worker Artifact Structure

#### Canonical Artifact Principle

**Core Invariants:**
- `result.txt` is the canonical worker output (source of truth)
- `thread.jsonl` is the canonical execution trace
- `metadata.json` contains derived views (summary, future extractions)
- System behavior depends on canonical artifacts, NOT derived data
- Derived data (summary, extractions) is rebuildable and can fail safely

**Status vs Summary:**
- `status` field: System-determined from exit codes and exceptions
- `summary` field: LLM-generated description for human consumption
- **Always trust `status` for system decisions**
- Use `summary` for display and context compression only

#### Filesystem Structure

```
/data/swarmlet/workers/
├── index.json                         # Master index (owner-filtered)
└── {worker_id}/
    ├── metadata.json
    │   {
    │     "worker_id": "2024-12-03T14-32-00_disk-check",
    │     "owner_id": 1,
    │     "task": "Check disk usage on cube",
    │     "status": "success",           // ← From system
    │     "started_at": "2024-12-03T14:32:00Z",
    │     "completed_at": "2024-12-03T14:32:12Z",
    │     "duration_ms": 12000,
    │     "model": "gpt-4o-mini",
    │     "supervisor_run_id": 456,
    │
    │     // Summary for context compression (NEW)
    │     "summary": "Cube 78% full, healthy, ~2-3 months capacity",
    │     "summary_meta": {
    │       "version": 1,                // Prompt version
    │       "model": "gpt-4o-mini",      // Model used
    │       "generated_at": "2024-12-03T14:32:12Z",
    │       "error": null                 // If fallback used
    │     },
    │
    │     // Future: Rich metadata (not yet implemented)
    │     "extracted": null
    │   }
    ├── result.txt                     # Natural language result (CANONICAL)
    ├── thread.jsonl                   # Full conversation (CANONICAL)
    └── tool_calls/
        ├── 001_ssh_exec.txt           # Raw SSH output
        └── 002_ssh_exec.txt
```

### 6.3 Supervisor Configuration

```python
# Agent config for supervisor
{
  "name": "Supervisor",
  "model": "gpt-4o",
  "system_instructions": SUPERVISOR_PROMPT,
  "config": {
    "is_supervisor": True,
    "temperature": 0.7,
    "max_tokens": 2000
  },
  "allowed_tools": [
    "spawn_worker", "list_workers", "read_worker_result",
    "read_worker_file", "grep_workers", "get_worker_metadata",
    "get_current_time", "http_request", "send_email"
  ]
}
```

---

## 7. Implementation Phases

### Phase 1: Foundation (COMPLETE ✅)

- [x] PostgresSaver for durable checkpoints
- [x] WorkerArtifactStore (filesystem persistence)
- [x] WorkerRunner (worker execution)
- [x] Supervisor tools (spawn_worker, list_workers, etc.)
- [x] Supervisor agent configuration and prompt
- [x] Security: owner-based filtering

### Phase 2: Jarvis Integration (BACKEND COMPLETE ✅, Frontend TODO)

**Goal:** Connect Jarvis to Zerg supervisor

| Task | Description | Status |
|------|-------------|--------|
| Supervisor endpoint | `POST /api/jarvis/supervisor` | ✅ Done |
| SSE supervisor events | Stream progress to Jarvis | ✅ Done |
| Jarvis intent detection | Route simple vs complex | ⏳ TODO |
| Jarvis progress UI | Show "Investigating..." state | ⏳ TODO |
| Result rendering | Display supervisor findings | ⏳ TODO |

**Deliverable:** User can say "check my servers" and get synthesized result.

### Phase 2.5: Summary Extraction (COMPLETE ✅)

**Goal:** Enable context-efficient worker scanning

**Why now:** Without summaries, cannot scan 50+ workers without hitting context window limits.

| Task | Description | Status |
|------|-------------|--------|
| Summary extraction | Extract 150-char summary on worker completion | ✅ Done |
| Update metadata schema | Add summary + summary_meta fields | ✅ Done |
| Graceful degradation | Fallback to truncation if LLM fails | ✅ Done |
| Update list_workers() | Return summaries only, not full results | ✅ Done |
| Update tools docstring | Clarify list_workers contract | ✅ Done |
| Add tests | Test extraction, fallback, list behavior | ✅ Done |

**Deliverable:** Supervisor can scan 100+ workers (1500 tokens) vs 25,000 tokens without summaries.

**Key Design:**
- Summary = compression layer for context, NOT metadata
- Full result.txt remains canonical
- Summary can fail → fallback to truncation
- Cost: ~$0.00001 per worker, ~200ms latency

### Phase 3: Real Workers (NEXT ⏳)

**Goal:** Port existing crons as supervisor-callable workers

**Prerequisite:** Add `ssh_exec` tool for remote host access.

| Worker | What it does |
|--------|--------------|
| Backup monitor | Check Kopia snapshots, verify backup health |
| Disk health | Check disk usage, SMART status across servers |
| Docker health | Verify containers running, check for restarts |
| Infrastructure check | Connectivity, service health, certificate expiry |

Workers use shell-first approach: `ssh_exec` + connectors (email for alerts).
No per-worker tool profiles needed.

**Deliverable:** Supervisor can delegate real infrastructure checks.

### Phase 4: Memory & Context

**Goal:** Supervisor maintains long-term memory

| Task | Description | Effort |
|------|-------------|--------|
| Core memory | Facts stored in agent_state | 3 days |
| Summarization | Compress old conversations | 3 days |
| Cross-session context | Remember past interactions | 2 days |

**Deliverable:** "Did we check this before?" works correctly.

### Phase 5: Background & Alerts

**Goal:** Scheduled supervisor runs with notifications

| Task | Description | Effort |
|------|-------------|--------|
| Scheduled supervisor | Morning health check | 2 days |
| Severity assessment | Determine alert level | 2 days |
| Multi-channel notify | Email, SMS, push | 3 days |
| Jarvis Task Inbox | Show background results | 2 days |

**Deliverable:** Morning digest of overnight activity.

### Phase 6: Two-Way Alerts

**Goal:** User can reply to alerts

| Task | Description | Effort |
|------|-------------|--------|
| Reply parsing | Email/SMS reply detection | 3 days |
| Reply → trigger | Resume supervisor context | 2 days |
| Directive handling | "Make this weekly" | 2 days |

**Deliverable:** Full two-way conversation with background agents.

---

## 8. Technical Considerations

### 8.1 Latency Budget

| Component | Target | Notes |
|-----------|--------|-------|
| Quick mode (total) | < 2s | OpenAI Realtime direct |
| Supervisor mode (total) | < 60s | Complex tasks |
| Intent detection | < 500ms | Must feel instant |
| Supervisor startup | < 2s | Thread creation, context load |
| Worker execution | < 30s | Depends on task |
| Result synthesis | < 5s | GPT-4o response |

### 8.2 Cost Considerations

| Component | Model | Est. Cost/Interaction |
|-----------|-------|----------------------|
| Quick mode | Realtime model (voice) | ~$0.01 |
| Supervisor | High-intelligence model (configurable) | ~$0.02-0.05 |
| Worker | Efficient model (configurable) | ~$0.005 |
| Typical complex task | 1 supervisor + 2 workers | ~$0.03-0.06 |

*Models are env-configurable via `DEFAULT_MODEL_ID` and `DEFAULT_WORKER_MODEL_ID`.*

### 8.3 Security

| Concern | Mitigation |
|---------|------------|
| Cross-tenant data | Owner ID filtering on all queries |
| Worker artifacts | Filesystem permissions + owner check |
| Tool access | Allowed tools per agent |
| Jarvis auth | Device secret → JWT (7-day expiry) |

### 8.4 Error Handling

| Error | Response |
|-------|----------|
| Worker timeout | Supervisor reports partial results + retry option |
| Worker failure | Supervisor explains error, suggests next steps |
| Supervisor failure | Jarvis falls back to direct mode with apology |
| SSE disconnect | Auto-reconnect with 5s backoff |

---

## 9. Open Questions

### 9.1 UX Decisions Needed

1. **Progress granularity**: How much detail to show during supervisor execution?
   - Option A: Just "Working on it..."
   - Option B: "Checking disk... Checking docker... Done"
   - Option C: Real-time worker output streaming

2. **Voice during long tasks**: What does Jarvis say while waiting?
   - Option A: Silent with visual indicator
   - Option B: Periodic "Still working..."
   - Option C: Play ambient sound

3. **Drill-down access**: How does user access worker details?
   - Option A: "Show me the details" → Jarvis reads more
   - Option B: Link to Zerg dashboard
   - Option C: In-Jarvis artifact viewer

### 9.2 Technical Decisions Needed

1. **Supervisor thread lifecycle** (RECOMMENDED):
   - **One thread per user (long-lived)** ✅
   - Each user has a single supervisor thread that persists indefinitely
   - Each task creates a new Run attached to this thread
   - Context accumulates across all interactions (with summarization for older content)
   - Benefits: True "one brain", cross-domain synthesis, consistent personality

2. **Worker tool access** (DECIDED ✅):
   - **Shell-first philosophy**: Workers get terminal access as the baseline primitive
   - `ssh_exec` for remote hosts, `container_exec` for sandboxed local execution
   - Connectors (email, Slack, Jira, etc.) available for side effects shell can't do
   - No tool profiles or task-specific restrictions — trust the LLM to use what it needs
   - Safety via: host allowlists, audit trails (`tool_calls/*.txt`), owner gating on connectors

3. **OpenAI Realtime integration**:
   - Jarvis calls route_to_supervisor as function?
   - Jarvis detects intent and switches modes?
   - Hybrid with some tasks handled by Realtime directly?

### 9.3 Product Decisions Needed

1. **Free tier limits**: How many supervisor calls per day?
2. **Worker artifact retention**: How long to keep?
3. **Background task frequency**: User-configurable?

---

## Appendix A: Supervisor System Prompt

See `/apps/zerg/backend/zerg/prompts/supervisor_prompt.py` for the full prompt.

Key sections:
- Role definition ("one brain" coordinator)
- When to spawn workers vs handle directly
- Querying past work
- Communication style
- Error handling

---

## Appendix B: File Locations

| Component | Location |
|-----------|----------|
| Supervisor prompt | `zerg/prompts/supervisor_prompt.py` |
| Supervisor tools | `zerg/tools/builtin/supervisor_tools.py` |
| Worker artifact store | `zerg/services/worker_artifact_store.py` |
| Worker runner | `zerg/services/worker_runner.py` |
| Checkpointer | `zerg/services/checkpointer.py` |
| Jarvis router | `zerg/routers/jarvis.py` |
| Jarvis frontend | `apps/jarvis/apps/web/` |
| Research docs | `docs/research/` |

---

## Appendix C: Related Documents

- `docs/research/01-langgraph-supervisor.md` - Supervisor pattern research
- `docs/research/02-memgpt-memory-tiers.md` - Memory architecture
- `docs/research/03-letta-filesystem.md` - Filesystem storage patterns
- `docs/research/04-lsfs-semantic-search.md` - Future semantic search
- `docs/research/05-interrupt-resume-patterns.md` - Wake conditions

---

*End of Specification*

---

## Addendum (Dec 2025): Phase 2 Clarifications

These notes remove ambiguity for Jarvis → Supervisor integration and worker execution.

### A. Supervisor Endpoint Contract
- **Endpoint:** `POST /api/jarvis/supervisor`
- **Auth:** Jarvis session cookie (from `/api/jarvis/auth`); resolves to `owner_id`.
- **Request body:**
  ```json
  {
    "task": "Investigate slow API responses",
    "context": {
      "conversation_id": "jarvis-session-123",
      "previous_messages": [...]
    },
    "preferences": {
      "verbosity": "concise",
      "notify_on_complete": true
    }
  }
  ```
- **Behavior:** Idempotently find or create the long-lived supervisor thread for the user, attach a new Run, and stream progress over SSE.
- **Response:** `{ "run_id": int, "thread_id": int, "status": "running", "stream_url": "/api/jarvis/supervisor/events?run_id=..." }`

### B. SSE Events for Supervisor + Workers
Event names/payloads for `GET /api/jarvis/supervisor/events`:
- `supervisor_started`: `{run_id, thread_id, task}`
- `supervisor_thinking`: `{message}`
- `worker_spawned`: `{job_id, task, model}` — *Note: `worker_id` not yet assigned*
- `worker_started`: `{job_id, worker_id}` — *`worker_id` (filesystem artifact ID) assigned here*
- `worker_complete`: `{job_id, worker_id, status, duration_ms}` (status = success|failed|timeout)
- `worker_summary_ready`: `{job_id, worker_id, summary}`
- `supervisor_complete`: `{run_id, result}`
- `error`: `{run_id, message, details?}`
- `heartbeat`: `{timestamp}` every 30s

*`job_id` = DB integer (stable handle), `worker_id` = filesystem artifact ID (appears after execution starts).*

### C. Intent Routing Rules (Jarvis → quick vs supervisor)
- Inputs: user utterance, optional history, latency budget.
- Quick mode if: single-fact/question, no external tools needed, expected <5s.
- Supervisor mode if: investigation, multi-step, or tools/files/SSH/HTTP needed.
- Surface to user: “I’ll escalate this to the supervisor for a deeper check…”
- Fallback: if intent uncertain, default to supervisor (keeps context clean).

### D. Supervisor Thread Lifecycle (“One Brain”)
- Key: **one supervisor thread per user**, long-lived, never recreated unless missing.
- Idempotency: thread lookup keyed by owner_id; create if absent.
- Each `POST /api/jarvis/supervisor` → new Run attached to that thread.
- On restart: re-hydrate the thread from DB; do not create a new one.
- Summarize old content when needed (future Phase 4), but keep thread identity stable.

### E. Ownership / Tenancy Defaults
- `owner_id` **required** on worker creation; reject missing owner_id in production paths.
- `list_workers`, `read_worker_result`, `read_worker_file`, `search_workers` must default-filter by owner_id; no owner → no data.
- SSE streams emit only events scoped to the authenticated owner.

### F. Worker Job Processor
- Must start at app boot; poll interval 5s; max concurrency 5 (configurable).
- Emits worker events (`worker_spawned/started/complete/summary_ready/error`) onto the supervisor SSE stream when applicable.

### G. Timeouts and Error Semantics
- Supervisor run timeout default: 60s (configurable); emits `error` with partial results if any.
- Worker timeout default: 300s; statuses are system-determined (`success|failed|timeout`), never taken from LLM.
- On timeout/failure, supervisor may synthesize partial results and suggest next steps; summaries are best-effort and can fall back to truncation.
