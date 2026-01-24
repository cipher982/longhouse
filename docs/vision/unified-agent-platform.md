# Unified Agent Platform Vision

**Status:** Living document - refining ideas
**Started:** 2026-01-22
**Contributors:** David, Claude (Opus), Codex (GPT-5.2)

---

## The Problem

Current workflow:
```
cd ~/git/zerg/
claude-code
"fix the auth bug"
... watch it work ...
... babysit ...
... close terminal ...
... forget where you were ...
```

The human is:
- **Router** - deciding which repo/directory
- **Scheduler** - deciding what to work on when
- **Babysitter** - watching agents work
- **Memory** - remembering context across sessions

This doesn't scale. You want your time back.

---

## The Vision (One Sentence)

**Delegate to agents, get notified when done, review results.**

Not "where should I cd" but "do this thing" and walk away.

---

## What Already Exists

| Component | Status | Location |
|-----------|--------|----------|
| Supervisor/Worker ReAct loop | ✅ Built | `supervisor_react_engine.py` |
| Durable runs (interrupt/resume) | ✅ Built | `durable-runs-v2.2.md` spec |
| Runner infrastructure (WebSocket) | ✅ Built | `runner_connection_manager.py` |
| Job dispatch to runners | ✅ Built | `runner_job_dispatcher.py` |
| Session history to postgres | ✅ Built | `agents.sessions/events` (Life Hub) |
| Task tracking | ✅ Built | `work.tasks` (Life Hub) |
| Hindsight analysis | ❌ Removed | Was `hindsight_service.py` |
| Async job execution | ❌ Missing | Runner blocks, 30s default timeout |
| CLI agent spawning | ❌ Missing | No `run_cli_agent` tool |
| Unified work view | ❌ Missing | Data exists, no joined UI |

---

## Key Architectural Decisions

### 1. Cloud-First, Laptop as Resource

```
OLD: Laptop is primary, cloud is "remote execution option"
NEW: Cloud is primary (24/7), laptop is a tool cloud can call when needed
```

**Why:** You're tired of walking around with a charger, keeping screen open, never leaving wifi. The goal is a 24/7 worker that doesn't depend on your laptop being alive.

**What this means:**
- Agent runs on zerg-vps by default (always on)
- Repos cloned to cloud, work happens there
- Laptop runner (existing WebSocket infra) is called only when agent needs local resources
- If laptop offline, agent works with what cloud has or waits gracefully

**What needs laptop vs. what doesn't:**

| Need | Laptop Required? | Why |
|------|------------------|-----|
| Git repos | ❌ No | Clone to cloud |
| Claude session logs | ❌ No | Already in Life Hub postgres |
| Public APIs | ❌ No | Cloud has internet |
| API keys | ❌ No | Copy to cloud .env once |
| Local docker/services | ✅ Yes | Only on laptop |
| Keychain secrets | ✅ Yes | macOS-specific |
| Local-only files | ✅ Yes | Not in git |

**80% of work runs on cloud. Laptop is the 20% fallback.**

### 2. Single Database = Full Truth

Life Hub postgres holds everything:
- `agents.sessions/events` - CLI session logs
- `work.tasks` - Task tracking
- `zerg.*` - Runtime state
- `ops.runs` - Job history

Query instead of organize. The UI is a view on this truth.

### 3. Two Modes of Work

1. **Hands-on** - Back-and-forth collaboration (like today, but in UI)
2. **Delegated** - Fire and forget, notified when done

Both are threads. Both persist. Both resumable.

---

## Codex's Perspective (GPT-5.2, 2026-01-22)

### Core Reframe

> "Centralize *truth* and *policy*, decentralize *execution*."

### Industry Direction

- Platform war moving from model APIs to **orchestration**: durable runs, auditability, policy gating
- Context routing will be data-driven (telemetry + semantic indexing), not static repo selection
- Agents become "workflow primitives": long-running tasks, resumable state, human checkpoints

### Failure Modes of Centralization

| Risk | Impact |
|------|--------|
| Control-plane outage | Everything halts |
| Silent data drift | Wrong context propagates everywhere |
| Schema coupling | Choke point for experimentation |
| Security blast radius | Compromised hub = compromised fleet |

### Oversight Without Bottlenecks

**"Human in the policy"** not "human in the loop":
- Pre-defined guardrails (budgets, tool allowlists, scope boundaries)
- Declarative approvals ("auto-approve if < $X, diff < N lines")
- Trust tiers (new tasks need oversight, repeated patterns auto-approve)
- Fast aborts (stop/rollback any run in seconds)

### End-State Timeline

**2028:** Agents as semi-autonomous services. Context routing mostly automatic. Central DB becomes semantic event log.

**2031:** Distributed mesh of supervisors. Work is a graph of jobs, not chat threads. Human oversight = portfolio management.

### Principles to Avoid Lock-In

- Protocol over platform (clean contracts for runners, tools, runs)
- Event-sourced core (append-only events, derived views)
- Pluggable routing (service, not hard-coded algorithm)
- Model-agnostic (capabilities, not model-specific hacks)
- Auditability by default (provenance on every action)

### Direct Challenges

> "Single DB is full truth" is useful but dangerous as single failure domain. Make it canonical ledger with local caches and eventual consistency.

> "Jarvis in cloud, laptop is tool" works until offline/latency kills productivity. Want hybrid: local-first execution, cloud-first oversight.

---

## Claude's Perspective (Opus, 2026-01-22)

### Gap Analysis

The vision doc undersells:
1. Life Hub integration was **actively removed** (commit 18b27a0), not just incomplete
2. Runner async execution is a **blocking architectural change**, not a parameter tweak

And oversells:
- Context routing is prompt engineering, not a separate system

### What's Actually Missing

1. **Runner is synchronous** - `runner_exec()` blocks worker thread
2. **No streaming** - Output buffered, not pushed to UI
3. **No job queueing** - Busy runner rejects, doesn't queue
4. **No AGENTS.md injection** - Supervisor gets no repo-specific context
5. **Hindsight removed** - Webhook handler deleted, analysis pipeline gone

### Recommendation

Restore hindsight webhook, add async job mode, build unified view. 80% of value with 20% of work.

---

## David's Pushback (2026-01-22)

### On "Single Point of Failure" Concerns

> "My laptop IS the single point of execution and control right now. How is that not a point of failure? If we're worrying about how many 9s of uptime for a psql db for my personal coding platform, I worry we're too far into enterprise land."

**Valid.** The failure mode framing is wrong for personal tooling:

| Concern | Enterprise | Personal |
|---------|------------|----------|
| Uptime SLA | 99.99% | "Is it up when I need it?" |
| Failure recovery | Automated failover | SSH in and restart |
| Data durability | Multi-region replication | Daily backups |

The real insight isn't reliability—it's **leverage**:
```
Today:    1 human + 1 laptop = 1 stream of work
Future:   1 human + N agents = N streams of work
```

### The Real Question

Not "should we build this" but **"what's the MVP to validate the idea?"**

Current process: `cd to repo → claude-code → type task → watch`

What's the 80/20 that proves delegation works?

---

## MVP Design (Refined 2026-01-22)

**Core insight:** Cloud is always on. Laptop is optional. Prove delegation works with the simplest possible change.

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  ZERG-VPS (always on)                                   │
│                                                         │
│  ┌─────────────┐    ┌─────────────┐    ┌────────────┐  │
│  │ Zerg Backend│───▶│ Agent runs  │───▶│ Git repos  │  │
│  │ (Jarvis)    │    │ HERE        │    │ (cloned)   │  │
│  └─────────────┘    └──────┬──────┘    └────────────┘  │
│                            │                            │
│                            │ needs local resource?      │
│                            ▼                            │
│                     ┌──────────────┐                    │
│                     │ Laptop Runner│◀─── WebSocket      │
│                     │ (if online)  │     (existing!)    │
│                     └──────────────┘                    │
└─────────────────────────────────────────────────────────┘
```

### What Stays As-Is

- Supervisor/worker ReAct loop, durable runs
- Session history + task tracking in Postgres
- Jarvis UI entrypoint
- Laptop runner + WebSocket (now used as "reach out to laptop" not "dispatch work to laptop")

### True MVP (3 Things)

| Change | Purpose |
|--------|---------|
| **Local agent execution** | `subprocess.Popen("agent-run ...")` directly on zerg-vps. No WebSocket to self. No 30s timeout. |
| **Workspace management** | Clone repo, create `jarvis/<run_id>` branch, capture `git diff` on completion. |
| **Notification** | Webhook or email when done. Simple POST with status + link. |

**That's it.** Laptop runner already exists for the "reach out" case. Review UI can use existing run detail page initially.

> **What is `agent-run`?** Headless CLI wrapper (lives in `~/bin/agent-run`) that runs Claude/Codex/Gemini without TTY. Supports `agent-run -m bedrock/claude-sonnet "prompt"` for non-interactive execution. See global CLAUDE.md for full usage.

### MVP Flow

```
1. User: "fix the auth bug in zerg"
2. Jarvis creates work.task, spawns cloud worker
3. Cloud worker (on zerg-vps):
   - git fetch origin && git checkout -b jarvis/<run_id>
   - subprocess: agent-run -m bedrock/claude-sonnet "<instruction>"
   - On completion: git diff > artifact, summarize
   - If needs laptop resource: runner_exec() to laptop (if online)
4. Update run status → POST notification
5. User gets notified, opens run page, reviews diff
```

### What This Doesn't Solve (Yet)

- Context routing (user specifies repo for now)
- AGENTS.md injection (agent gets repo but not custom context)
- Multi-repo support (start with zerg repo only)
- Trust tiers / policy automation

These are Phase 2+. MVP proves: **delegate task → close laptop → get notified → review result.**

---

## Open Questions

1. ~~What's the smallest change that enables "delegate and walk away"?~~ → **Answered: 3 things (local exec, workspace mgmt, notification)**
2. ~~Where does the remote runner live?~~ → **Answered: No remote runner. Direct subprocess on zerg-vps.**
3. **Git auth on server** - SSH key on zerg-vps that can pull your repos. One-time setup.
4. **Workspace location** - `/var/jarvis/workspaces/<run_id>/`? Cleanup policy?
5. **Which repos to start?** - MVP: just `zerg` repo. Multi-repo is Phase 2.
6. **Notification mechanism** - Discord webhook? Email? Both?

---

## Next Steps

1. ✅ Vision aligned - cloud-first, laptop as resource
2. ✅ Exact files identified (see Implementation Plan below)
3. **One-time setup**: SSH key, workspace dir, agent-run on server
4. **Build vertical slice**: single repo (zerg), single task type, prove it works

---

## Implementation Plan

### Files to Modify

| File | Change | Location |
|------|--------|----------|
| `services/worker_runner.py` | Add cloud execution mode branch | Line ~232 |
| `services/supervisor_service.py` | Add notification webhook call | Lines ~911, ~1014 |
| `models/` | Add `notification_webhook` to user/agent | New column |

### Files to Create

| File | Purpose |
|------|---------|
| `services/workspace_manager.py` | Git clone, branch creation, diff capture |

### Key Integration Points

**Cloud Execution** - `worker_runner.py:232`
```python
if config.get("execution_mode") == "cloud":
    created_messages = await self._run_cloud_execution(task, workspace_path, config)
else:
    created_messages = await asyncio.wait_for(runner.run_thread(...), timeout=timeout)
```

**Workspace Setup** - Before worker execution
```python
if config.get("git_repo"):
    workspace = await WorkspaceManager.setup(repo=config["git_repo"], run_id=worker_id)
    # Agent runs in workspace.path
```

**Notification** - `supervisor_service.py:911` (after success) and `:1014` (after failure)
```python
if notification_webhook := getattr(run.agent.owner, 'notification_webhook', None):
    await send_webhook(url=notification_webhook, payload={...})
```

### Server Setup (One-Time)

```bash
# On zerg-vps
mkdir -p /var/jarvis/workspaces
chmod 755 /var/jarvis/workspaces

# SSH key for git access (use existing or generate)
ssh-keygen -t ed25519 -f ~/.ssh/jarvis_deploy -N ""
# Add public key to GitHub as deploy key

# Install agent-run
scp ~/bin/agent-run zerg:/usr/local/bin/
```

### Database Migration

```sql
ALTER TABLE users ADD COLUMN notification_webhook TEXT;
-- Or add to agent_configs JSONB if preferred
```

---

## Session Log

### 2026-01-22 Evening - Initial Vision

- Established "Full Truth" principle: query instead of organize
- Codex provided long-term vision (2028: semi-autonomous, 2031: distributed mesh)
- David pushed back on enterprise concerns - this is personal tooling
- Landed on MVP: detached execution + notification + review

### 2026-01-22 Night - Cloud-First Reframe

**Key clarification from David:**
> "I want something that can 'always run' - a safe robust cloud VM environment. It can 'reach out' to my laptop when needed. The goal is a 24/7 worker that no longer relies on me walking around with a laptop charger."

This flipped the model:
- **Before:** "Add remote execution option to laptop-centric system"
- **After:** "Cloud is primary, laptop is optional resource"

**Implications:**
- Most work (80%) doesn't need laptop at all
- Laptop runner becomes "reach out when needed" not "dispatch work to"
- MVP simplified to 3 things: local exec on cloud, workspace mgmt, notification

### Correlation Gap (still relevant)

When cloud agent spawns `claude-code`, link back via:
- Pass `ZERG_RUN_ID`, `WORK_TASK_ID` as env vars
- Agent-shipper captures in `agents.sessions.metadata`
- Enables joins: Zerg run → CLI session → work.task

### Phase Progression

| Phase | Goal | Key Change |
|-------|------|------------|
| **MVP** | Prove delegation works | Cloud exec + workspace + notification |
| **Phase 2** | Multi-repo + unified view | Dashboard, more repos, AGENTS.md injection |
| **Phase 3** | Smart routing | Auto-detect repo from task description |
| **Phase 4** | Policy automation | Trust tiers, auto-approve patterns |

---

*Last updated: 2026-01-22 (night session)*
