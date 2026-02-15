# Longhouse Vision (2026)

Longhouse is an AI agent orchestration platform where AI does the work and humans manage the system. The product must feel instant, always-on, and magical: your Claude Code sessions appear as a clean, queryable timeline inside Longhouse with zero friction (Codex and Gemini shipping; Cursor in progress).

This is a living vision doc. It captures both the direction and the reasoning that got us here, so we can make fast decisions without re-litigating the fundamentals.

## How to Read This Doc

- Unless explicitly marked otherwise, statements in this document describe the **target architecture**.
- Sections labeled **Current State (as of YYYY-MM-DD)** are point-in-time implementation snapshots.
- For operational truth and execution status, use `TODO.md` and `AGENTS.md`.

## Read Next

- **SQLite-only OSS plan:** this doc (see "SQLite-only OSS Pivot (Consolidated)" below)
- **OSS onboarding plan:** this doc (see "Onboarding UX" below)

## Naming (2026-02)

- **Longhouse** = public product + brand
- **Oikos** = assistant UI inside Longhouse
- **Zerg** = internal codename/repo (transitional; user-facing docs should say Longhouse)

### Branding Usage Rules

**Do:**
- Use **Longhouse** in marketing, UI, and docs
- Use **Oikos** only for the assistant feature
- Keep CLI verbs neutral (`longhouse serve`, `longhouse onboard`, `longhouse connect`)

**Don't:**
- Mix Longhouse and Zerg in user-facing copy
- Use Zerg/StarCraft theming in user-facing docs
- Apply themed verbs to APIs/CLI commands

**Transition notes:**
- Repo paths still live under `apps/zerg/` until the code rename lands
- Some env vars / schema names may still use `ZERG_` during transition

---

## North Star

1. Zero-friction onboarding for hosted + self-hosted: hosted beta signup plus **install.sh** for OSS. `pip install longhouse` remains the alternate. SQLite only.
2. Always-on agents: background work continues even when the user is away.
3. Unified, queryable agent sessions across providers (Claude, Codex, Gemini, Cursor, Oikos).
4. A hosted option that feels like "I pay $5 and never think about it."
5. Fast iteration as a solo founder: avoid multi-tenant security complexity unless required.

---

## User Value Proposition

Three promises to users:

1. **Never lose a conversation** — Claude Code, Codex, and Gemini sessions appear in one timeline today; Cursor is in progress. No more grepping JSONL.

2. **Find where you solved it** — Search by keyword, project, date. Instant results. FTS5-backed for sub-10ms discovery; Oikos handles deeper queries.

3. **Resume from anywhere** — Hosted makes sessions resumable across devices. Self-hosted is local by default.

**Guiding principle: Fast to Fun.** Time from install to "oh cool" should be under 2 minutes.

---

## Product Surface (2026-02 Decision)

**Primary UX: Web timeline + Oikos chat.** The web UI is the product. Timeline is the archive and control surface; Oikos is the built-in coordinator that has visibility into all agent work (commis sessions, shipped sessions, run status).

**Oikos role:** Personal assistant for your agent team. On the web, Oikos is a full chat interface with tools. Think of it as the manager who can peek into any Claude Code / commis session, surface insights, and help coordinate. On mobile, the responsive web UI covers 80% of use cases. Messaging channels (Telegram, etc.) are a secondary lightweight interface for on-the-go check-ins — "how's the team doing," approve/reject, read summaries.

**Chat channels (Telegram, etc.) are secondary.** They provide a thin interface to the same Oikos — useful for quick coordination while away from desk, not the primary product experience. The web UI can do everything chat can do, plus timeline browsing, search, session detail, and visual controls.

**What this means for execution:**
- Timeline + Oikos web UX is the critical path
- Slim Oikos dispatch contract is higher priority than channel wiring
- Channel integration layers on top of a working web Oikos, not the other way around
- Mobile story is "responsive web first, chat channels second"

---

## Principles & Constraints

- **Always-on beats cold start** for paid users. Background agents are core; sleeping instances break the product.
- **Lossless logs are sacred.** The agent session archive is not disposable.
- **Dual-path story**: hosted and self-hosted are equal in positioning and CTA.
- **Progressive disclosure**: keep primary docs short and link to deeper runbooks; AGENTS.md must point to what else to read.
- **Single-tenant core (enforced)**: build fast, keep code simple, avoid multi-tenant security tax. Agents APIs reject instances with >1 user.
- **Hosted = convenience**: premium support and "don't think about it" operations.
- **Users bring their own API keys**. Longhouse is orchestration + UI + data, not LLM compute billing.
- **No Postgres in core**. SQLite is the only DB requirement for OSS and hosted runtime instances.
- **Hosted architecture = control plane + isolated runtimes**. Control plane is multi-tenant; Longhouse app stays single-tenant.

---

## What Changed (Reality Check)

- Longhouse started as a hand-written ReAct system. It has evolved into an orchestration layer around Claude Code and other CLIs.
- The "real" session log is the provider JSONL stream. Longhouse's internal threads are operational state, not the canonical archive.
- Life Hub currently owns the agents schema; Longhouse should own it so OSS users are self-sufficient.

---

## The Trigger (and Why It Matters)

Oikos session picker threw:
```
relation "agents.events" does not exist
```

Cause: Longhouse was querying `agents.sessions` and `agents.events` in Life Hub's database. Those tables do not exist in Longhouse's DB.

This revealed the deeper issue: Longhouse was not standalone. OSS users who install Longhouse would hit Life Hub errors. That is a dead end for adoption.

---

## The Core Shift

**Target direction:** Longhouse is primarily an orchestration layer around CLI agents.

- Commis runs are Claude Code sessions (workspace mode via `hatch` subprocess). There is no separate in-process commis harness.
- The archive of truth is the provider session log, not Longhouse's internal thread state.
- The "magic" is taking obscure JSONL logs and turning them into a searchable, unified timeline.

This is the product. Everything else supports it.

### No Custom Agent Harness (2026-02 Decision)

**Target:** Longhouse does not build its own LLM execution loop. Claude Code, Codex CLI, and Gemini CLI are agent harnesses built by teams of hundreds. Longhouse should not compete with them.

**Current State (as of 2026-02-11):**
- Commis uses CLI subprocess execution (workspace mode) and ingests resulting sessions into timeline storage.
- Slim Oikos (Phase 3) complete: loop simplified, tools flattened, services decoupled, memory consolidated, skills progressive disclosure, MCP server, quality gates, multi-provider research.
- Oikos in-process loop (`fiche_runner` + `oikos_react_engine`) still runs but is significantly slimmed; deferred items (dispatch contract, compaction API) tracked in TODO.

**Target end-state:**

- **Commis = CLI agent subprocess.** Every commis spawns a real CLI agent (Claude Code via `hatch`) in an isolated workspace. The user gets the exact same agent they use in terminal — same tools, same context management, same capabilities.
- **Standard mode (in-process ReAct loop) is deprecated.** The custom harness infrastructure (fiche_runner, message assembly, tool registry, skills system, ReAct engine — ~15K LOC) is legacy from pre-pivot. It will be removed incrementally. The ~60 builtin tools themselves are kept as a modular toolbox.
- **Oikos is a thin coordinator with configurable tools.** Oikos uses a simple LLM API loop (not a custom ReAct engine) with a configured subset of the toolbox. It delegates complex multi-step work to commis but can perform quick actions directly (send email, post to Slack, search sessions, etc.).
- **Commis sessions appear in the timeline.** When a commis finishes, its session JSONL is ingested through the same `/api/agents/ingest` path as shipped terminal sessions. All sessions are unified in one archive.

### Oikos Dispatch Contract (Target)

Oikos is a coordinator, so every turn should follow a simple dispatch decision:

1. **Direct response** (no tool call)
2. **Quick tool action** (search/memory/web/messaging)
3. **CLI delegation** (spawn commis with explicit backend + workspace mode)

Dispatch should honor user intent for backend selection:
- "use Claude/Claude Code" -> Claude backend
- "use Codex" -> Codex backend
- "use Gemini" -> Gemini backend
- no explicit preference -> configured default backend

Delegation modes should be explicit:
- **Repo mode:** git repo provided, clone/branch/diff flow
- **Scratch mode:** no repo, ephemeral workspace for analysis/research/ops-style tasks

**Current State (as of 2026-02-10):**
- Oikos still uses legacy prompt/tool guidance that is partly ops-era and not fully aligned with workspace-only delegation semantics.
- Backend selection for commis is mostly implicit (model mapping) rather than first-class user intent.
- See `apps/zerg/backend/docs/specs/unified-memory-bridge.md` (Phase 3) for the implementation plan.

**What Longhouse owns:** orchestration, job queue, workspace isolation, timeline, search, resume, always-on infrastructure, runner coordination, modular toolbox (integrations, memory, communication).

**What CLI agents own:** the agent loop, tool execution, file editing, bash, MCP servers, context management, streaming.

### Longhouse MCP Server (CLI Agent Integration)

CLI agents (Claude Code, Codex, Gemini) can call back into Longhouse's toolbox via MCP. This is the standard industry pattern — teams expose internal tooling as MCP servers so agents can access shared context mid-task.

**Longhouse exposes as MCP tools:**
- `search_sessions` — find past solutions in the session archive
- `get_session_detail` — retrieve specific session content/events
- `memory_read` / `memory_write` — persistent memory across commis runs
- `notify_oikos` — commis reports status back to Oikos coordinator

**How it works:**
- Longhouse runs an MCP server (stdio transport for local, streamable HTTP for remote)
- `longhouse connect --install` registers the MCP server in Claude Code's `.claude/settings.json`
- Commis spawned via `hatch` automatically get the Longhouse MCP server configured
- A hatch-spawned agent can search "how did we implement retry logic?" against the Longhouse archive mid-task

**Current State (as of 2026-02-10):** MCP server implemented with stdio and HTTP transport. 5 tools exposed: `search_sessions`, `get_session_detail`, `memory_read`, `memory_write`, `notify_oikos`. Auto-registered via `longhouse connect --install`. Auto-configured for commis workspaces (injected into `.claude/settings.json` at spawn time). Codex `config.toml` MCP registration supported. Quality gates (verify hooks) injected into commis workspaces.

### Multi-Provider Backend Integration

Each CLI agent backend has different integration depths:

| Backend | Commis Execution | Event Streaming | Hooks Support | MCP Support |
|---------|-----------------|----------------|---------------|-------------|
| Claude Code | `hatch` subprocess | JSONL file parse | Yes (Stop, SessionStart, etc.) | Yes (native) |
| Codex | `hatch -b codex` | JSONL + App Server protocol (JSON-RPC) | No (rules system instead) | Yes (config.toml) |
| Gemini | `hatch -b gemini` | JSONL file parse | No | Yes |

**Codex App Server protocol:** Codex exposes a stable harness API (bidirectional JSON-RPC over stdio) with Thread/Turn/Item primitives and approval requests. For Codex-backend commis, this gives structured event streaming and approval routing through Oikos. Evaluate as alternative to raw JSONL parsing.

---

## The Canonical Idea (Unified Sessions)

Agent sessions are unified into a single, lossless, queryable database:

- **sessions**: one row per provider session (metadata, device, project, timestamps)
- **events**: append-only rows for each message/tool call (raw text + parsed fields)

This schema is already proven in Life Hub. We are moving it to Longhouse and making Longhouse the source of truth.

---

## Session Discovery

Two tiers optimized for different needs:

**Timeline Search Bar (fast, 80% of lookups)**
- Indexed search over session events (`content_text`, `tool_name`, project, etc.)
- Instant results
- Keyword matching, project/date filters
- User types → results appear immediately
- (Launch requirement: SQLite FTS5 for sub-10ms full-text search)

**Oikos Discovery (agentic, complex queries)**
- Multi-tool reasoning for vague or complex lookups
- Tools: `search_sessions`, `grep_sessions`, `filter_sessions`, `get_session_detail`
- Semantic search (embeddings) for approximate matching
- Example: "Find where I implemented retry logic last month" → Oikos searches, filters, cross-references

**The split:**
- Search bar handles "I know roughly what I'm looking for"
- Oikos handles "I vaguely remember something..."

This keeps the UI snappy while preserving power for complex discovery.

---

## Domain Model (Fix the Abstraction)

"An agent session is an agent session" is true only if we preserve structure. We model explicitly:

- **conversation**: user-facing thread (Oikos thread)
- **run**: orchestration execution (Oikos run / commis job)
- **session**: provider log stream (Claude/Codex/Gemini/Cursor)
- **event**: message/tool call within a session

Relations:
- A conversation can spawn multiple runs.
- A run can spawn one or more provider sessions.
- A session emits many events.

This prevents flattening lifecycles and keeps UI semantics intact.

---

## Current Session Streams (Today)

Two streams were conflated:

**Stream 1: Laptop -> Life Hub**
```
Claude Code on laptop
  -> shipper daemon (watches ~/.claude/projects/)
  -> Life Hub /ingest/agents/events
  -> agents.sessions + agents.events
```

**Stream 2: Longhouse commis -> Life Hub**
```
Longhouse spawns commis
  -> Claude Code runs in container
  -> commis_job_processor ships to Life Hub
  -> same Life Hub tables
```

Both end up in Life Hub, so Longhouse depends on Life Hub. We are reversing that: Longhouse becomes the canonical home for agent sessions, and Life Hub becomes a reader.

---

## Product Paths

### OSS Local (default path)
```bash
pip install longhouse
longhouse serve
```
- Local web UI on port 8080
- Local agents DB (SQLite)
- Shipper requires `longhouse connect` to start
- Full end-to-end flow is visible locally

(Homebrew formula planned for future.)

**Local path diagram:**
```
Laptop
  ├─ Longhouse (UI + API)
  ├─ SQLite only (default and core)
  └─ Shipper watches ~/.claude/, ~/.codex/, ~/.gemini/...
```

### Hosted (paid, always-on)
```
Sign in with Google -> provision isolated instance -> always-on
```
- One container stack per user (shared node, strict limits)
- Always-on background agents
- Users bring their own API keys
- Premium support + no-ops maintenance

**Hosted path diagram:**
```
User -> Control Plane -> Provision Longhouse instance (per-user)
     -> user.longhouse.ai -> Longhouse (UI+API) + DB
```

### Free Trial
- Optional: provisioned instance for a short trial
- Can hibernate after trial, but **paid instances stay hot**

---

## Onboarding UX

The first 2 minutes determine adoption. Onboarding must be zero-friction and demonstrate value before asking for configuration.

**Timeline-first:**
- Timeline (`/timeline`) is the default route for authenticated users
- The session archive IS the product - not a feature buried in nav
- New users land on Timeline immediately, not a dashboard or settings page

**Zero-key demo:**
- Demo sessions auto-seed on first run (when sessions table is empty); `SKIP_DEMO_SEED=1` to disable
- Users see the product working before any configuration
- Chat/LLM features prompt for keys only when actually needed

**Guided empty state:**
When Timeline is empty, show a 3-step path:
1. Connect shipper (optional) - for real session sync
2. Load demo - instant gratification, no keys
3. Explore timeline - filters, search, detail views

This is not a modal or tour - it's inline content that disappears once sessions exist.

**Current State (as of 2026-02-11):** Auto-seed on first run and guided empty state with "Load demo sessions" CTA are implemented. `longhouse serve --demo` / `--demo-fresh` also supported. Multi-CLI detection (Claude Code, Codex CLI, Gemini CLI) in onboard wizard with guidance when no CLI is found. Install guide docs at `docs/install-guide.md`.

**Docs-as-source validation:**
README contains an `onboarding-contract` JSON block that CI executes:
- Steps to run (`pip install longhouse`, `longhouse serve`, health check)
- Cleanup commands
- CTA selectors to verify (e.g., `[data-testid='demo-cta']`)

If the README drifts from reality, CI fails. No hidden env flags - everything declared in the contract.

**OSS install + onboarding (canonical):**
- **One-liner install is the primary path:** `curl -fsSL https://get.longhouse.ai/install.sh | bash`
- `pip install` / `brew install` remain supported but are not the primary path
- The installer must:
  - Install the `longhouse` CLI
  - Install a Claude shim (PATH-based) so sessions show up without user retraining
  - Verify the shim in a fresh shell and **report if it failed** with an exact fix line (implemented)
- **Interactive wizard:** `longhouse onboard`
  - QuickStart by default; Manual for power users
  - Multi-CLI detection: discovers Claude Code, Codex CLI, and Gemini CLI; shows guidance with alternative CLI links when none found
  - No 200-line `.env` edits
  - Graceful degradation: UI works without API keys; chat unlocks later
- **Install guide:** `docs/install-guide.md` — canonical install path, onboarding wizard steps, connect flow, troubleshooting, manual install
- **Goal:** time-to-value < 2 minutes and a visible session in the Timeline

---

## Mental Model (Core vs Scheduler vs Jobs)

Longhouse is the product. Sauron is the scheduler service. Jobs are the thing it runs.

- **Longhouse Core**: UI + API + agents. Runs standalone (no scheduler required).
- **Sauron**: cron/scheduler service. Optional for Longhouse overall, but its whole purpose is to run jobs (think “cron for Longhouse jobs/commis/fiches”).
- **Jobs Pack**: the job source Sauron needs. Options: a local template for zero-config OSS, or a private repo for real workloads.

This framing keeps OSS onboarding simple while preserving the “power user” path.

---

## Hosting Architecture (Indie-Scale)

We do not do "one VM per user." We do:

- **Control plane (tiny)**: signup -> payment -> provision -> route
- **Runtime**: one container stack per user (Longhouse + SQLite) on shared nodes
- **Routing**: wildcard DNS + reverse proxy to per-user container
- **Always-on**: paid instances never sleep

**Current plan:** keep control plane + instances co-located on the zerg host for simplicity. Split later only if scale demands it.

This preserves instant agents while keeping $5-10/month viable.

**Decision:** the control plane is the *only* multi-tenant system. It provisions per-user Longhouse instances. The app remains single-tenant.

---

## Control Plane Details

The control plane is minimal infrastructure that provisions and routes to user instances.
It is multi-tenant by necessity, but it stores only account + provisioning metadata (no agent data).

**Signup flow:**
```
1. User visits longhouse.ai
2. "Sign in with Google" -> OAuth
3. User record created in control plane DB
4. Stripe checkout for $5/month (or free trial)
5. On payment success: provision instance
6. Redirect to alice.longhouse.ai
```

**Provisioning (via Docker API on zerg server):**

We do NOT use Coolify for dynamic provisioning (API can't create apps). Instead, the control plane runs on zerg and talks directly to the local Docker socket.

**Docker API access (security):**
- Control plane runs on the zerg host and uses the local Docker unix socket
- If remote access is required, use SSH + `docker context` (no open TCP socket)
- Lock down the control plane service user to Docker-only permissions

```bash
# Control plane calls Docker API directly (Caddy labels for coolify-proxy)
docker run -d \
  --name longhouse-alice \
  --network coolify \
  --label caddy=alice.longhouse.ai \
  --label "caddy.reverse_proxy={{upstreams 8000}}" \
  -v /data/longhouse-alice:/data \
  -e INSTANCE_ID=alice \
  -e SINGLE_TENANT=1 \
  -e APP_PUBLIC_URL=https://alice.longhouse.ai \
  -e PUBLIC_SITE_URL=https://longhouse.ai \
  -e DATABASE_URL=sqlite:////data/longhouse.db \
  ghcr.io/cipher982/longhouse-runtime:latest
```

**Control plane stack:**
```
apps/control-plane/           # NEW - tiny FastAPI app
├── main.py                   # App startup + router wiring
├── config.py                 # Settings (Stripe keys, Docker host, JWT)
├── models.py                 # User, Instance, Subscription
├── routers/
│   ├── auth.py               # Google OAuth
│   ├── billing.py            # Stripe checkout + portal + webhooks
│   └── instances.py          # Provision/deprovision/status
└── services/
    ├── provisioner.py        # Docker API client
    └── stripe_service.py     # Stripe helpers
```

**Routing:**
- Wildcard DNS: `*.longhouse.ai -> zerg server IP` (✅ configured 2026-02-05, proxied through Cloudflare)
- Caddy Docker Proxy on zerg (existing coolify-proxy) routes by caddy-docker-proxy labels
- Each container gets unique subdomain automatically

**Provisioning guarantees:**
- Provision is idempotent (retry-safe) by instance_id
- Control plane waits for `/api/health` before redirect
- Deprovision archives volume before delete (or marks for retention)

**What control plane stores (Postgres, separate from instances):**
- User email, Stripe customer ID, subscription status
- Instance ID, container name, provisioned timestamp, state
- NOT user data (that's in their isolated SQLite instance)

**What user instances store (SQLite, isolated):**
- Agent sessions, events, threads
- User preferences, API keys (encrypted)
- Everything in the OSS schema

**Control plane endpoints:**
| Endpoint | Purpose |
|----------|---------|
| `POST /signup` | Google OAuth → create user record |
| `POST /checkout` | Create Stripe checkout session |
| `POST /webhooks/stripe` | Handle payment → trigger provision |
| `GET /instance/{user}` | Check instance status |
| `POST /provision/{user}` | Manual trigger (admin) |
| `DELETE /instance/{user}` | Deprovision (cancel) |

---

## Runner Architecture (User-Owned Compute)

Runners are user-owned daemons that execute commands on infrastructure the user controls.

**What a runner is:**
- Bun-compiled daemon installed on user's laptop/server
- Connects **outbound** to Longhouse (no firewall holes needed)
- Executes shell commands when Longhouse requests (`runner_exec` tool)
- Example: run tests, git operations, deploy scripts

**How runners fit with isolated hosting:**
```
User's hosted Longhouse instance (always-on)
         ↕ WebSocket
User's laptop runner daemon
         ↓
Local command execution (npm test, etc.)
```

Each user's Longhouse instance only sees their own runners. Isolation is natural.

**Runner registration:**
- Runner installer (`install-runner.sh`) registers via `/api/runners/register` endpoint
- Runner connects with enrollment token
- Longhouse validates runner belongs to the user

---

## Commis Execution Model

Commis are background agent jobs. **All commis run as CLI agent subprocesses** via `hatch`.

**How it works:**
- Spawns `hatch` CLI (wraps Claude Code / Codex / Gemini CLI) as subprocess
- Uses explicit delegation mode:
  - repo workspace (git clone/branch/diff)
  - scratch workspace (no repo clone)
- Long-running tasks (up to 1 hour)
- Captures artifacts (and diff when repo mode is used)
- Session JSONL ingested into agent timeline on completion

**Current State (as of 2026-02-10):**
- Repo workspace mode is implemented end-to-end.
- Scratch-mode delegation is a target contract but not fully standardized in tool/docs yet.
- Legacy `spawn_commis` naming/semantics still exist for compatibility and are being simplified.

**Standard mode (in-process) is deprecated.** The legacy in-process ReAct loop with custom tools is being removed. See "No Custom Agent Harness" above.

**What's containerized vs not:**
- ✅ Containerized: Longhouse backend (per-user isolation in hosted mode)
- ❌ Not containerized: Commis execution (runs as subprocess with workspace isolation)
- ❌ Not containerized: Runner commands (run on user's own machine)

This keeps the execution model simple. No Docker dependency for OSS users.

---

## Commis Workspace Isolation

Workspace mode provides directory-based isolation so multiple commis can work on the same codebase simultaneously without conflicts:

```
✓ Git clone isolation (own directory per commis)
✓ Git branch isolation (oikos/{run_id})
✓ Process group isolation (killable on timeout)
✓ Artifact capture (diff, logs accessible to host)
```

**How it works:**

1. `WorkspaceManager` clones repo to `~/.longhouse/workspaces/{commis_id}/`
2. Creates working branch `oikos/{commis_id}`
3. Commis executes via `hatch` subprocess
4. Changes captured as git diff
5. Artifacts stored for Oikos to reference
6. Workspace cleaned up (or retained for debugging)

**Current State (as of 2026-02-10):**
- Runtime default path is `/var/oikos/workspaces` unless `OIKOS_WORKSPACE_PATH` is set.
- `~/.longhouse/workspaces` remains the preferred OSS target path but is not yet the universal runtime default.

**Directory structure:**
```
~/.longhouse/
├── workspaces/
│   ├── ws-123-abc/      # Commis 1's isolated clone
│   └── ws-456-def/      # Commis 2's isolated clone
└── artifacts/
    ├── ws-123-abc/
    │   └── diff.patch   # Captured changes
    └── ws-456-def/
        └── diff.patch
```

This enables the "multiple agents adding features to the same codebase" pattern. Each commis gets a clean working directory, can create branches and push changes without stepping on other agents' work.

**No Docker required** — it's just directories and git branches. Fast startup, simple debugging, artifacts accessible to host.

**Security note:** Workspace isolation is about parallel work, not sandboxing untrusted code. Commis run with full host access (same as running `hatch` manually). For OSS users on their own machine, this is the expected trust model

---

## Shipper (Real-Time Sync)

**Target:** real-time sync is default and feels instant.

### Two shipping paths (preferred → fallback)

**Path 1: Hook-based push (preferred for Claude Code)**

Claude Code hooks fire on lifecycle events (`Stop`, `SessionStart`, etc.). A `Stop` hook can push the session to Longhouse immediately when Claude Code finishes — no daemon, no file-watching, zero latency:

```json
// .claude/settings.json (injected by `longhouse connect --install`)
{"hooks": {"Stop": [{"command": "longhouse ship --session $SESSION_ID"}]}}
```

Benefits:
- Zero-config after install (no background service to manage)
- Instant push (faster than file-watching debounce)
- Works on any OS without launchd/systemd
- `longhouse connect --install` auto-injects the hook + verifies it works

Limitations:
- Only works for providers with hook support (Claude Code today)
- Requires Claude Code settings access

**Path 2: Watcher daemon (fallback / multi-provider)**

The shipper daemon for providers without hook support (Codex, Gemini, Cursor):

- Watches local provider session files via OS file watching (FSEvents/inotify)
- Debounces rapid writes (Claude streams to file)
- Sends incremental events to Longhouse in batches
- Runs as a background service (launchd/systemd)
- Falls back to periodic scan to catch missed events/rotations
- Spools locally when offline, syncs on reconnect

**Current State (as of 2026-02-10):**
- `longhouse connect` runs in foreground (watch mode by default; polling with `--poll` or custom `--interval`).
- `longhouse connect --install` installs/starts the background service + Claude Code hooks + MCP server.
- `longhouse auth` handles device-token setup; `connect` then uses the stored token.
- Hook-based push implemented: Stop hook ships session on Claude Code response completion; SessionStart hook shows recent sessions.
- Watcher daemon watches Claude, Codex, and Gemini session directories for real-time sync.

**Testing:** Shipper smoke test (`make test-shipper-smoke`) validates the end-to-end ingest path: file parse → API post → session appears in timeline. Added 2026-02-11.

**Magic moment:** user types in Claude Code -> hook fires on stop -> session appears in Longhouse before they switch tabs.

---

## Ingest Protocol

The shipper-to-Longhouse ingest must be robust:

**Target batching:**
- Collect events for up to 1 second or 100 events, whichever first
- Gzip compress payload
- Single HTTP POST per batch

**Current State (as of 2026-02-10):**
- Shipper posts per parsed file chunk (with gzip support) rather than a strict 1s/100-events window.
- Offline spool replay relies on DB dedupe (`source_path`, `source_offset`, `event_hash`) rather than explicit idempotency headers.

**Offline resilience target:**
- Local SQLite spool when Longhouse unreachable
- Replay on reconnect with idempotency keys
- Dedup by (session_id, source_path, source_offset, event_hash)

**Authentication (current):**
- Per-device token is created via auth/UI flow, then used by `longhouse connect`/`longhouse ship`
- Token scoped to user's instance
- Revocable if device compromised

**Rate limits:**
- 1000 events/minute per device (soft cap)
- Backpressure via HTTP 429

---

## Timeline Session Resume (Design)

Transform Timeline from passive session visualization into an interactive session multiplexer.
Click an agent session (Claude/Codex/Gemini) and resume that session in real time.

**Goal:** Resume a provider session turn-by-turn without breaking the canonical archive in Longhouse.

### Turn-by-Turn Resume Pattern

Each user message spawns a fresh provider process with context restoration:

```
User Message
    ↓
Backend: POST /api/sessions/{id}/chat
    ↓
1. Validate session ownership & provider
2. Acquire per-session lock (or 409)
3. Resolve workspace (local or temp clone)
4. Prepare session file from Longhouse archive
    ↓
5. Spawn: claude --resume {id} -p "message" --output-format stream-json
    ↓
6. Stream SSE events to frontend
    ↓
7. On complete: ingest session updates back into Longhouse
```

### Key Components

#### Backend

**`routers/session_chat.py`**
- `POST /sessions/{session_id}/chat` - SSE streaming endpoint
- `GET /sessions/{session_id}/lock` - Check lock status
- `DELETE /sessions/{session_id}/lock` - Force release (admin)

**`services/session_continuity.py`**
- `SessionLockManager` - In-memory async locks with TTL
- `WorkspaceResolver` - Clone repo to temp if workspace unavailable
- `prepare_session_for_resume()` - Build provider session file from Longhouse events
- `ingest_session_updates()` - Ingest new events into Longhouse

#### Frontend

**`components/SessionChat.tsx`**
- Message list with streaming assistant response
- Cancel button (AbortController)
- Lock status indicators

**`pages/ForumPage.tsx`** (may rename to `TimelinePage.tsx`)
- Resume mode toggle when session selected
- SessionChat replaces metadata panel in resume mode

### SSE Event Types

| Event | Data | Description |
|-------|------|-------------|
| `system` | `{type, session_id, workspace}` | Session info, status updates |
| `assistant_delta` | `{text, accumulated}` | Streaming text chunks |
| `tool_use` | `{name, id}` | Tool call notification |
| `tool_result` | `{result}` | Tool execution result |
| `error` | `{error, details?}` | Error message |
| `done` | `{exit_code, total_text_length}` | Completion signal |

### Security Considerations

**Path traversal prevention**
- Workspace path derived server-side from session metadata
- Client never provides workspace path
- Session IDs validated with strict pattern

**Concurrent access**
- Per-session async locks prevent simultaneous resumes
- 409 response when session locked, with fork option (future)
- TTL-based expiration (5 min default) for crash recovery

**Process management**
- Process terminated on client disconnect
- AbortController propagates cancellation
- Cleanup of temp workspaces on completion/error

### Workspace Resolution

Priority order:
1. **Original path exists locally** → Use directly
2. **Git repo in session metadata** → Clone to temp dir
3. **Neither available** → Error (chat-only future option)

Temp workspaces:
- Location: `~/.longhouse/workspaces/session-{id[:12]}`
- Shallow clone (`--depth=1`) for speed
- Cleaned up after chat completion

### Performance Characteristics

Based on lab testing:
- TTFT: ~8-12 seconds (context reload + first token)
- Context growth: ~1.3 KB per turn
- Session file updated and re-ingested after each turn

### Future Enhancements

1. **Fork sessions** - Create new session from locked session's state
2. **Chat-only mode** - Allow conversation without workspace (no tools)
3. **Tool execution warnings** - Confirm before destructive operations
4. **Multi-session view** - Chat with multiple sessions in tabs

---

## Agents Schema (Source of Truth)

Adopt the Life Hub schema as Longhouse's canonical agent archive, implemented to run on SQLite by default:

- Lossless storage: raw text + raw JSON
- Queryable: extracted fields for search
- Append-only: events never updated
- Dedup: hash + source offset
- Optional Postgres/TimescaleDB for scale and advanced search

Longhouse owns this data. Life Hub becomes a reader.

---

## Security Model (Minimal, Practical)

We avoid most multi-tenant risk by isolating users. Remaining risks:

- **Ingest endpoint**: must authenticate and protect against replay/injection.
- **Device identity**: issue per-device tokens via auth flow and require them for shipper ingest.
- **Rate limits**: basic caps per device to prevent abuse.
- **Data leakage**: isolated instance prevents cross-user leaks by default.

Containerization (Docker/containerd/k3s) protects execution, **not** data isolation. It does not replace tenant safety if we ever go multi-tenant.

---

## Authentication (Two Paths)

Auth differs between self-hosted and hosted. The Longhouse app codebase is identical; auth method is configured via environment.

### Self-Hosted Auth

OSS users should NOT need Google Console setup. Options:

| Method | Env Var | Use Case |
|--------|---------|----------|
| **Password** | `LONGHOUSE_PASSWORD=xxx` | Remote access, simple shared secret |
| **Disabled** | `AUTH_DISABLED=1` | Local-only, trusted network |
| **Google OAuth** | `GOOGLE_CLIENT_ID=xxx` | Power users who want OAuth |

**Default behavior:**
- If `LONGHOUSE_PASSWORD` is set → password login enabled
- If localhost + no password → auto-authenticate (dev convenience)
- If remote + no password + no OAuth → require one to be configured

**Password endpoint:**
```
POST /api/auth/password
{ "password": "xxx" }
→ Sets session cookie, returns user info
```

### Hosted Auth

Hosted users authenticate via control plane, then get redirected to their instance.

**Flow:**
```
1. User visits longhouse.ai
2. Clicks "Sign In with Google"
3. Control plane handles OAuth, creates/finds user record
4. If no instance: redirect to checkout
5. If instance exists: redirect to {user}.longhouse.ai?auth_token=xxx
6. User instance validates token, sets session cookie
```

**Cross-subdomain token:**
- Control plane issues short-lived JWT (5 min)
- User instance validates at `/api/auth/accept-token`
- Token contains user_id, email, instance_id
- After validation, instance sets its own session cookie

**Token trust:**
- Control plane signs JWTs with a private key
- Instances validate via shared secret or JWKS URL
- Tokens are one-time use (nonce stored server-side)

**Current State (as of 2026-02-10):** `POST /api/auth/accept-token` validates the JWT and sets a session cookie but does not enforce one-time use (no nonce/server-side tracking). One-time enforcement is a target improvement.

**Instance auth state:**
- Instance trusts tokens signed by control plane
- Instance maintains its own session (httpOnly cookie)
- No ongoing dependency on control plane for auth

**Hosted auth alternatives:**
- Default: Google OAuth via control plane
- Optional: per-instance password (`LONGHOUSE_PASSWORD`) for users who refuse OAuth
- Future: magic link or passkeys (not required for launch)

---

## API Key Management

Users bring their own LLM API keys. Longhouse stores and uses them securely.

**Onboarding modal:**
```
"Choose your AI provider"
  [ ] OpenAI      [paste key]
  [ ] Anthropic   [paste key]
  [ ] Google AI   [paste key]
  [ ] Bedrock     [configure AWS]
```

**Storage:**
- Keys stored in `account_connector_credentials` table
- Encrypted at rest with Fernet (AES-128-CBC)
- Decrypted only when making API calls
- Never logged, never sent to control plane

**Per-provider config:**
- Model selection (gpt-4o, claude-sonnet, etc.)
- Default reasoning effort
- Rate limit preferences

**Key rotation:**
- User can update keys anytime via settings
- Old key immediately invalidated in memory

---

## OSS Packaging

`pip install longhouse` must "just work" for the 90% case. (Homebrew formula planned for future.)

**What's in the package:**
- `longhouse` CLI (Python, via pip)
- Embeds: FastAPI backend, React frontend (built), shipper
- Default: SQLite for local DB (zero-config). Postgres is not part of core/runtime.

**Commands:**
```bash
longhouse serve           # Start local server (SQLite, port 8080)
longhouse connect --url <url>   # Run shipper in foreground (watch mode by default)
longhouse connect --url <url> --install   # Install/start shipper service
longhouse ship            # One-time manual sync
longhouse status          # Show current configuration
```

**Docker alternative:**
```bash
docker compose -f docker/docker-compose.dev.yml up    # Full stack with Postgres
```

**DB note (self-contained):**
- Should work out of the box with a local SQLite file.
- Scheduler/queue needs ops/agents schemas to bootstrap automatically.

**What's NOT in the package:**
- Node.js runner daemon (separate install if needed)
- Postgres (optional, user provides)
- LLM API keys (user provides)

**Homebrew formula (planned):**
```ruby
# Not yet published to Homebrew
class Longhouse < Formula
  desc "AI agent orchestration platform"
  homepage "https://longhouse.ai"
  url "https://github.com/cipher982/longhouse/releases/..."

  depends_on "python@3.12"

  def install
    virtualenv_install_with_resources
  end
end
```

---

## Economics (Founder's Reality)

We only cover hosting; users bring their own API keys. This keeps margins healthy.

For hosted:
- **Cost per user** ~= `server_cost / (usable_ram / per_user_ram)`
- Measure per-user RAM in steady state and reserve headroom.
- If per-user infra cost stays under $1-2/month, $5-10 pricing is viable.

Always-on for paid is non-negotiable. If a free tier exists, it can hibernate.

Margins are sensitive to commis concurrency; cap concurrent commis per user (tiers) to keep costs predictable.

Compute strategy (later): fixed nodes (Hetzner) are best for always-on baseline. Serverless (Modal/etc) only wins for bursty, low-duty commis; treat it as optional overflow rather than primary hosting.

---

## Why Not Multi-Tenant (for now)

The current codebase is multi-user but enforced at the application layer (owner_id filters, no DB RLS). That is risky at scale and expensive to audit continuously.

Single-tenant core + isolated hosted instances gives:
- Simpler code
- Faster iteration
- Security by default
- Same OSS and hosted codebase

Multi-tenant **inside the Longhouse app** is a possible future, not a current requirement. Multi-tenant **in the control plane** is acceptable and minimal.

---

## Migration Path (From Today)

1. **Agents schema in Longhouse** (alembic migration from Life Hub SQL)
2. **Ingest API** (`POST /api/agents/ingest`)
3. **Query API** (`GET /api/agents/sessions`)
4. **Port shipper** into Longhouse CLI
5. **Update commis** to ingest locally
6. **Backfill** Life Hub history if desired
7. **Life Hub reads Longhouse** (dashboard only)

**Implementation note:** Schema and APIs are provider-agnostic (Claude, Codex, Gemini, Cursor, Oikos). Phase 1 validation focuses on Claude (session picker), but the ingest/query layer must not hardcode provider assumptions.

Optional safety step:
- Dual-write during migration (Life Hub + Longhouse) then reconcile counts/hashes.

---

## Alternatives Considered (and Rejected)

1. **Shared multi-tenant SaaS now**
   - Efficient but high security tax; RLS would be required immediately.

2. **Per-user VM**
   - Too expensive; kills $5-10 pricing.

3. **Scale-to-zero for paid**
   - Breaks always-on agents; bad UX.

4. **Separate "agent-sessions" service**
   - Adds infra complexity; Longhouse should own this data.

---

## Data Lifecycle

**Retention:**
- Agent sessions: indefinite by default (the archive is the product)
- User can configure retention per project (e.g., delete after 90 days)
- Hibernated free trials: auto-delete after 30 days with warning emails

**Deletion (GDPR-ready):**
- User requests account deletion via settings
- Triggers: delete all sessions, events, credentials, user record
- Hosted: container destroyed, subdomain freed
- Deletion is permanent and irreversible

**Schema versioning:**
- `events.schema_version` field (default: 1)
- Ingest always writes current version
- Query layer handles version differences
- Breaking changes: new version, migrate on read or background job

**Export:**
- User can export all data as JSON/JSONL
- Includes: sessions, events, credentials (encrypted), settings
- For migration to self-hosted or data portability

---

## Observability

**Per-instance health:**
- `/api/health` endpoint on each user instance
- Control plane pings periodically
- Alert if instance unreachable for >5 minutes

**Metrics (future):**
- Events ingested per day
- Commis jobs run
- API call counts (per provider)
- Exposed via `/metrics` (Prometheus format)

**Logging:**
- Structured JSON logs
- Shipped to central aggregator (Loki/CloudWatch)
- Retention: 7 days for debug, 90 days for audit

**Alerting:**
- Instance down
- Ingest errors spike
- Disk usage >80%

**User-facing status:**
- Simple status page: "Your instance is healthy"
- Last sync time from shipper
- Recent commis job outcomes

---

## Prompt Cache Optimization (LLM Cost/Latency)

> **Note:** `MessageArrayBuilder` and `prompt_context` are slated for removal in Phase 3 (Slim Oikos). The simple loop replacement will use provider-native prompt caching (stable system message + tool schemas as prefix). This section documents the legacy approach for reference.

**Current state:** `MessageArrayBuilder` already follows good cache patterns:
1) Static system content first
2) Conversation history next
3) Dynamic context last

This maximizes prefix cache hits for providers that support prompt caching.

### Cache-Busting Issues

1. **Timestamp precision** (high impact)
   - Current dynamic context uses full timestamps; changes every request.
   - **Fix:** reduce granularity (minute or date) or inject time separately.

2. **Memory context variance** (medium impact)
   - Memory search results vary per query and are embedded in the same dynamic block.
   - **Fix:** cache memory results per query or separate into its own message.

3. **Connector status JSON ordering** (low impact)
   - Unstable key order can bust cache.
   - **Fix:** `json.dumps(..., sort_keys=True)` for deterministic output.

### Recommended Quick Wins

- Sort connector status keys for deterministic output.
- Reduce timestamp granularity in dynamic context.
- Split dynamic context into separate SystemMessages (time, connector status, memory).

---

## Risks & Mitigations

- **Ship missed events** -> periodic scan fallback + dedup by (path, offset, hash).
- **Provider schema drift** -> raw_text/raw_json preserved; extracted fields are best-effort.
- **Archive loss** -> backups + stable DB (AGENTS_DATABASE_URL if needed).
- **Overlapping identities** -> explicit conversation/run/session links.
- **Scale shocks** -> per-user limits, rate caps, pre-warmed headroom.

---

## Life Hub Integration (David-specific)

Life Hub is David's personal data platform (health, finance, etc.). It is NOT part of Longhouse and Longhouse has zero concept of Life Hub in its codebase.

Life Hub becomes a dashboard consumer, not the data owner.

**Migration plan (David-specific):**
1. Longhouse already ingests Claude Code sessions via shipper — same raw data Life Hub has.
2. Add semantic search to Longhouse (embeddings on ingest) to replace Life Hub MCP `recall`/`search_agent_logs`.
3. One-time backfill: script pulls historical sessions from Life Hub API → pushes to Longhouse `/api/agents/ingest`.
4. Once Longhouse search is sufficient, stop using Life Hub for agent memory. Life Hub reads from Longhouse if it wants agent data.

**No bridge adapter, no `AGENTS_BACKEND` config, no Life Hub code inside Longhouse.**

**API contract (future):**
- Life Hub calls Longhouse's `/api/agents/sessions` endpoint
- Authenticated via service token (not user OAuth)
- Read-only access to session metadata and events

**Configuration:**
```env
# In Life Hub (not in Longhouse)
LONGHOUSE_API_URL=https://david.longhouse.ai/api
LONGHOUSE_SERVICE_TOKEN=xxx
```

This is David's personal integration. OSS users don't need Life Hub at all.

---

## Open Questions

1. ~~TimescaleDB support in Longhouse deployments?~~ → Fallback to vanilla Postgres with time-based partitioning.
2. ~~Session resume: store raw JSONL alongside events or reconstruct on demand?~~ → Reconstruct from events (implemented).
3. Backfill tooling: how to avoid duplicates and ensure fidelity?
4. How should Oikos conversations map into sessions (provider="oikos")?
5. Artifact storage: should file diffs, screenshots, patches be stored alongside events or separate?
6. ~~Runner daemon packaging: separate install or bundle with `longhouse` CLI?~~ → Separate install (Bun binary). Shipper is bundled with CLI; runner is separate per-machine daemon.
7. ~~Secrets for jobs: job-scoped encrypted bundles (age) vs sops vs external secrets manager?~~ → **Resolved.** JobSecret table with Fernet encryption (per-user, per-key). Platform provides defaults (control plane injects SES creds as env vars during provisioning); users can override via Settings UI (`EmailConfigCard`). Resolution chain: DB first → env var fallback.
8. ~~Jobs pack UX: local template by default vs required private repo from day one?~~ → **Partially resolved.** Builtin jobs work without a private repo. Platform-provided email means jobs can send notifications out of the box. Sauron-jobs repo remains the power-user path for custom jobs.
9. ~~Session discovery: semantic search (embeddings) priority vs FTS5-only for MVP?~~ → FTS5 for MVP (done), embeddings as Phase 4 enhancement. Memory system already has OpenAI embeddings infra.

---

## SQLite-only OSS Pivot (Consolidated)

_This section consolidates the former standalone SQLite pivot doc so VISION is the single source of truth._


**Status:** Active
**Goal:** `pip install longhouse && longhouse serve` — cloud agent ops center in under 5 minutes (SQLite only)
**Reality check:** Postgres remains for legacy/dev paths and control plane; OSS/runtime is SQLite-only.
**Naming note:** Public brand is Longhouse; repo paths still use `apps/zerg/` until the code rename lands.

---

### Current State Findings (2026-01-31)

These are the concrete mismatches between today’s codebase and the SQLite-only target. This section is here so we can plan from reality instead of aspiration.

✅ **Phases 1–3 complete:**
- SQLite URLs allowed in `make_engine`; startup no longer blocks SQLite.
- Schema handling is conditional (`DB_SCHEMA`/`AGENTS_SCHEMA` become `None` on SQLite) with schema translate map; `_apply_search_path()` is a no-op on SQLite.
- SQLite pragmas configured (WAL, busy_timeout, foreign_keys, etc).
- UUID/JSONB compatibility via GUID TypeDecorator + JSON.with_variant; Python-side defaults replace `gen_random_uuid()` on SQLite.
- Partial indexes include `sqlite_where`.
- Agents API now SQLite-safe: dialect-agnostic upsert, dedupe with `on_conflict_do_nothing()`, and `require_postgres()` removed.
- `lite_mode` detection handles quoted DATABASE_URLs via a shared `db_utils.is_sqlite_url()` helper.
- **SQLite minimum version enforced at startup: 3.35+** (RETURNING support).
- Lite test suite expanded (SQLite boot, agents ingest/models, GUID round-trips, db_is_sqlite detection, version check).

✅ **Phases 4–7 complete (2026-02-01):**
- Job claiming uses SQLite-specific `commis_job_queue.py`: `UPDATE ... RETURNING` with atomic claiming (Postgres `FOR UPDATE SKIP LOCKED` path was removed during SQLite-only pivot).
- Heartbeat + stale job reclaim implemented for both dialects.
- Checkpoints are durable on SQLite via `langgraph-checkpoint-sqlite` (`SqliteSaver`).
- CLI has `longhouse serve` command (`cli/serve.py`) with lite mode defaults.
- README defaults to SQLite for OSS quick start.

**Status: SQLite pivot is complete.** The `pip install longhouse && longhouse serve` flow is functional.

---

### Decisions to Lock (before implementation)

1. **SQLite minimum version**: **require 3.35+** (RETURNING support). Enforced at startup; fail fast.
2. **SQLite schema strategy**: recommended = **flat tables, no schemas** (there are no name collisions with agents tables). Postgres keeps schemas.
3. **Durable job queue**: **SQLite-backed `zerg.jobs.queue`** for OSS/Sauron. `ops.job_queue` (Postgres) is not required in lite.
4. **Durable checkpoints**: recommended = **use `langgraph-checkpoint-sqlite`** for SQLite so resumes survive restarts.
5. **Static frontend packaging**: recommended = **bundle `apps/zerg/frontend-web/dist` in the python package** and mount via FastAPI.

---

### Detailed Execution Plan (SQLite Lite Mode)

#### Phase 0 — Preflight Decisions + Flags

**Goal:** Establish SQLite vs Postgres mode cleanly so the rest of the system can branch safely.

- Add a computed `lite_mode` (or `db_is_sqlite`) flag in `zerg.config.get_settings()` based on `database_url` scheme (handles quoted URLs).
- Enforce SQLite >= 3.35 at startup (RETURNING support).
- Decide schema strategy (flat tables) and write it down in this doc.
- Decide job queue scope (disable ops job queue in lite).
- Decide durable checkpoints (sqlite checkpointer).

#### Phase 1 — Core DB Boot on SQLite

**Goal:** `longhouse serve` boots on SQLite without crashing. **Status: ✅ complete**

- **database.py**
  - Allow sqlite URLs (remove hard error).
  - Skip `_apply_search_path()` for sqlite.
  - Make `DB_SCHEMA` and `AGENTS_SCHEMA` conditional; use `None` for sqlite.
  - Enforce SQLite >= 3.35 at startup (fail fast).
  - Set SQLite pragmas on connect: `journal_mode=WAL`, `busy_timeout`, `foreign_keys=ON`.
- **main.py**
  - Remove PostgreSQL-only guard in `lifespan()`. Replace with a warning if SQLite (locks/features are degraded).
- **initialize_database()**
  - Skip schema creation for sqlite and avoid schema-qualified introspection.

**Test:** `DATABASE_URL=sqlite:///~/.longhouse/longhouse.db longhouse serve` starts and `/api/health` works.

#### Phase 2 — Model Compatibility (Core + Agents)

**Goal:** All tables can be created on SQLite. **Status: ✅ complete**

- Replace `UUID` columns with `String(36)` (or `String`) + `uuid4()` defaults.
- Replace `JSONB` with `JSON().with_variant(JSONB, "postgresql")` or plain `JSON`.
- Replace `gen_random_uuid()` defaults with Python-side defaults on sqlite.
- Update partial indexes to include `sqlite_where` or drop them if not supported.
- Make `agents` metadata schema conditional (None for sqlite).

**Files:** `models/agents.py`, `models/device_token.py`, `models/llm_audit.py`, `models/run.py`, `models/models.py`

**Test:** `initialize_database()` succeeds on SQLite; `sqlite3 ~/.longhouse/longhouse.db .tables` shows all tables.

#### Phase 3 — Agents API + Ingest

**Goal:** Shipper ingestion + Timeline endpoints work on SQLite. **Status: ✅ complete**

- Replace `postgresql.insert` with dialect-agnostic upsert:
  - If sqlite: use `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_nothing()` or catch `IntegrityError`.
  - If postgres: keep current `on_conflict_do_nothing` with partial index.
- Remove `require_postgres()` guard; keep `require_single_tenant()` if needed.
- Ensure dedupe index works without schema-qualified names.

**Files:** `services/agents_store.py`, `routers/agents.py`, `models/agents.py`, `alembic/versions/0002_agents_schema.py` (+ follow-on migrations)

**Test:** Shipper syncs session; sessions appear in Timeline UI on SQLite.

#### Phase 4 — Job Queue + Concurrency (SQLite-safe)

**Goal:** Multiple commis can run concurrently without PG locks.

- Replace `FOR UPDATE SKIP LOCKED` with `BEGIN IMMEDIATE` + atomic `UPDATE ... RETURNING`.
- Add heartbeat fields + reclaim logic for stale jobs.
- Replace advisory locks with file locks or status-guarded updates.
- For SQLite: disable `ops.job_queue` paths (or gate behind `job_queue_enabled && not lite_mode`).

**Files:** `services/commis_job_processor.py`, `jobs/queue.py`, `services/commis_resume.py`, `services/single_tenant.py`, `services/fiche_locks.py`, `tools/builtin/email_tools.py`, `tools/builtin/sms_tools.py`

**Test:** Spawn 3 commis jobs, kill server, restart, jobs resume.

#### Phase 5 — Durable Checkpoints (SQLite)

**Goal:** Interrupt/resume survives process restart in lite mode.

- Replace MemorySaver for sqlite with `langgraph-checkpoint-sqlite` backed by the same `~/.longhouse/longhouse.db`.
- Ensure migrations/setup are idempotent for sqlite.

**Files:** `services/checkpointer.py`

**Test:** Interrupt a run, restart server, resume continues correctly.

#### Phase 6 — CLI + Frontend Bundle

**Goal:** `pip install longhouse && longhouse serve` is real.

- Add `longhouse serve` command (typer) that runs uvicorn with sane defaults (`0.0.0.0:8080`).
- Bundle frontend `dist` into the python package (hatch config).
- Update FastAPI static mount to use packaged assets when available.

**Files:** `cli/main.py`, `pyproject.toml`, `main.py`, `apps/zerg/frontend-web/dist`

**Test:** fresh venv → `pip install longhouse` → `longhouse serve` → open `/dashboard` and `/chat`.

#### Phase 7 — Onboarding Smoke + Docs

**Goal:** Validate the full OSS onboarding flow.

- Add/extend `make onboarding-smoke` to run SQLite boot + basic API checks.
- Update README quick-start to default to SQLite.

**Test:** `make onboarding-smoke` passes on a clean machine.

---

### The Vision

**The Problem:**
- You have 5-6 Claude Code terminals open
- Context switching is exhausting
- Close laptop = agents pause
- Can't check progress from phone
- Sessions lost if you restart

**The Solution:** Longhouse — your always-on agent operations center
**Alignment:** SQLite is the core and only runtime DB; Postgres is control-plane only (if used).

```
┌─────────────────────────────────────────────────────────────┐
│  LONGHOUSE (runs 24/7 on VPS / homelab / Mac mini)               │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Oikos (main chat)           Commis Pool (background)       │
│  ┌─────────────────┐         ┌────────────────────────┐     │
│  │ "Convert that   │────────▶│ Commis 1: cloning...   │     │
│  │  repo to Rust"  │         │ Commis 2: writing tests│     │
│  │                 │         │ Commis 3: reviewing PR │     │
│  │ "Status on the  │◀────────│ Commis 4: (idle)       │     │
│  │  PR from earlier"│        │ Commis 5: deploying... │     │
│  └─────────────────┘         └────────────────────────┘     │
│         ▲                            ▲                      │
│    [Phone/Web]                  [Sauron crons]              │
│                                                             │
│  Timeline: searchable archive of all sessions               │
│  Resume: continue any session from any device               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**Key insight:** Users migrate local Claude sessions → cloud commis. Close laptop, agents keep working. Check from phone. Wake up to completed work.

---

### Target Experience

```bash
### On your VPS / homelab / always-on Mac
pip install longhouse
longhouse serve --host 0.0.0.0 --port 8080

### That's it. Access from anywhere.
### SQLite at ~/.longhouse/longhouse.db (default)
### No Postgres in core/runtime; no Docker or external services required
```

| Metric | Target |
|--------|--------|
| Time to deploy | < 5 minutes |
| Idle RAM | < 200 MB |
| With 5 active commis | < 500 MB |
| External dependencies | Zero |

---

### What We're Building

| Feature | Description |
|---------|-------------|
| **Timeline** | Searchable archive of all sessions — the core product |
| **Search** | Instant discovery (FTS5 required for launch) + Oikos for complex queries |
| **Resume** | Continue any session from any device (spawns commis) |
| **Commis Pool** | Background agents (headless Claude Code) working in parallel |
| **Oikos** | Chat interface for discovery and direct AI interaction |
| **Mobile Access** | Check on agents, resume sessions from phone |
| **Sauron Crons** | Scheduled background jobs |

### What "Lightweight" Means

- **Easy deploy** — pip install, not k8s
- **Low resources** — runs on $5 VPS
- **Zero external deps** — no Postgres server, no Redis, no Docker
- **Simple config** — works out of the box

**NOT:**
- Single-user viewer
- Desktop-only tool
- Toy without real concurrency

---

### SQLite + Concurrent Agents

#### Why SQLite Works

**The fear:** "SQLite can't handle concurrent agents"

**The reality:** Your agents spend 99% of time waiting on LLM APIs, not writing to DB.

```
Agent 1: [======LLM call (10s)======] [write 5ms] [======LLM call======]
Agent 2: [======LLM call (8s)======] [write 5ms] [======LLM call======]
Agent 3: [======LLM call (12s)======] [write 5ms] [======LLM call======]
```

SQLite with WAL mode handles this trivially. Writes serialize but they're milliseconds.

#### Scale Reality Check

| Metric | Enterprise SaaS | Your Use Case |
|--------|-----------------|---------------|
| Concurrent agents | 1000s | 5-10 |
| Writes/second | 10,000s | ~10 |
| Users | 10,000s | 1 |

SQLite is overkill for this, not underkill.

#### Postgres Features → SQLite Alternatives

| Postgres Feature | What It Does | SQLite Alternative |
|------------------|--------------|-------------------|
| `FOR UPDATE SKIP LOCKED` | Atomic job claim | `BEGIN IMMEDIATE` + atomic UPDATE |
| Advisory locks | Cross-process coordination | Status column + heartbeat |
| UUID columns | Convenience | String(36) |
| JSONB | Indexed JSON | JSON1 extension |
| Separate schemas | Isolation | Separate .db files or prefixes |

#### Job Queue Implementation

```python
### Postgres way
SELECT * FROM jobs WHERE status='pending' FOR UPDATE SKIP LOCKED LIMIT 1

### SQLite way
def claim_job(db, worker_id):
    with db.begin_immediate():  # Acquire write lock
        job = db.execute("""
            UPDATE jobs
            SET status='running', worker_id=?, started_at=NOW()
            WHERE id = (
                SELECT id FROM jobs
                WHERE status='pending'
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
            )
            RETURNING *
        """, [worker_id]).fetchone()
    return job
```

Multiple workers, SQLite, works fine. Celery does this.

#### Coordination Without Advisory Locks

```python
### Option A: Status + heartbeat
### Job has: status, worker_id, last_heartbeat
### Worker updates heartbeat every 30s
### Stale jobs (no heartbeat for 2min) get reclaimed

### Option B: File locks for critical sections
import fcntl
with open(f"~/.longhouse/locks/{resource}.lock", 'w') as f:
    fcntl.flock(f, fcntl.LOCK_EX)
    # ... exclusive access ...
### Auto-released on close/crash
```

---

### Architecture

```
┌─────────────────────────────────────────────────────────┐
│  longhouse serve                                             │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  FastAPI (async)                                        │
│  ├── /api/* — REST endpoints                            │
│  ├── /ws/* — WebSocket for real-time                    │
│  └── /* — Static frontend                               │
│                                                         │
│  Commis Pool (concurrent async tasks)                   │
│  ├── Commis 1 ──▶ LLM ──▶ Tools ──▶ DB                  │
│  ├── Commis 2 ──▶ LLM ──▶ Tools ──▶ DB                  │
│  └── Commis N ──▶ LLM ──▶ Tools ──▶ DB                  │
│                                                         │
│  Job Queue (SQLite-backed)                              │
│  └── Durable, survives restarts                         │
│                                                         │
│  SQLite (WAL mode)                                      │
│  └── ~/.longhouse/longhouse.db                                    │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Single process. Multiple concurrent agents. SQLite. Durable jobs.**

---

### Implementation Phases

#### Phase 1: SQLite Backend (Day 1-2)

**Goal:** Accept SQLite URLs, prove it works

**Files:**
- `database.py` — Remove Postgres-only guard, accept `sqlite:///`
- `main.py` — Remove startup check
- `config/` — Add `lite_mode` detection (auto from URL scheme)

**Test:**
```bash
DATABASE_URL=sqlite:///~/.longhouse/longhouse.db longhouse serve
### Server starts, basic endpoints work
```

#### Phase 2: Agents Models (Day 2-3)

**Goal:** Claude session sync works with SQLite

**Changes:**
```python
### models/agents.py
### Before
id = Column(postgresql.UUID(as_uuid=True), primary_key=True)
raw_json = Column(postgresql.JSONB)

### After
id = Column(String(36), primary_key=True, default=lambda: str(uuid4()))
raw_json = Column(JSON)  # SQLite JSON1
```

**Files:**
- `models/agents.py` — UUID→String, JSONB→JSON, drop schema
- `services/agents_store.py` — Dialect-agnostic upsert
- `routers/agents.py` — Remove `require_postgres()` guard

**Test:** Shipper syncs session → appears in Timeline

#### Phase 3: Job Queue (Day 3-4)

**Goal:** Durable job queue with SQLite

**Changes:**
- Replace `FOR UPDATE SKIP LOCKED` with `BEGIN IMMEDIATE` pattern
- Add heartbeat column for stale job detection
- File locks for critical sections (optional)

**Files:**
- `jobs/queue.py` — SQLite-compatible claim logic
- `services/commis_job_processor.py` — Heartbeat updates

**Test:**
```bash
### Start server, spawn 3 commis, kill server, restart
### Jobs resume from where they left off
```

#### Phase 4: Single-Process Concurrency (Day 4-5)

**Goal:** Multiple commis in one process

**Architecture:**
```python
### Commis pool as async tasks
class CommisPool:
    def __init__(self, max_workers=10):
        self.semaphore = asyncio.Semaphore(max_workers)

    async def spawn(self, job):
        async with self.semaphore:
            await run_commis(job)
```

**Test:** Spawn 5 concurrent commis, all make progress

#### Phase 5: CLI + Frontend Bundle (Day 5-6)

**Goal:** `pip install longhouse && longhouse serve` works

**CLI:**
```bash
longhouse serve              # Start server (default: 127.0.0.1:8080)
longhouse serve --port 8080  # Custom port
longhouse status             # Show current configuration
# Note: `longhouse logs` command planned but not yet implemented
```

**Frontend:** Pre-built React app served from FastAPI static mount

**Test:** Fresh virtualenv, pip install, longhouse serve, open browser, see UI

#### Phase 6: PyPI Publishing (Day 6-7)

**Goal:** Available on PyPI

```bash
pip install longhouse
```

---

### File Structure (After)

```
~/.longhouse/
├── longhouse.db              # SQLite database (WAL mode)
├── config.toml          # Optional config overrides
├── locks/               # File locks for coordination
└── logs/                # Job logs

$ longhouse serve
→ http://127.0.0.1:8080
→ SQLite: ~/.longhouse/longhouse.db
```

Logs are written to `~/.longhouse/server.log` (server) and `~/.claude/shipper.log` (shipper).

---

### Feature Matrix

| Feature | Lite (SQLite) | Full (Postgres) |
|---------|:-------------:|:---------------:|
| Timeline UI | ✅ | ✅ |
| Session sync (Shipper) | ✅ | ✅ |
| Oikos chat | ✅ | ✅ |
| Concurrent commis | ✅ | ✅ |
| Durable job queue | ✅ | ✅ |
| Job queue (multi-node) | ❌ | ✅ |
| Full-text search | ✅ FTS5 | ✅ FTS5 |
| Multi-user | ❌ | ✅ |

**Lite = single node, full features. Postgres = horizontal scale.**

---

### Config

```toml
### ~/.longhouse/config.toml (optional — sensible defaults work)

[server]
host = "0.0.0.0"
port = 8080

[commis]
max_concurrent = 5      # How many agents can run at once
heartbeat_interval = 30 # Seconds between heartbeats
stale_threshold = 120   # Reclaim jobs with no heartbeat after this

[database]
### Default: sqlite:///~/.longhouse/longhouse.db
### For scale: postgresql://user:pass@host/db
url = "sqlite:///~/.longhouse/longhouse.db"

[llm]
anthropic_api_key = "sk-ant-..."
openai_api_key = "sk-..."
```

---

### Success Criteria

1. **Deploy:** `pip install longhouse && longhouse serve` on fresh VPS
2. **Concurrent:** 5 commis running simultaneously
3. **Durable:** Kill process, restart, jobs resume
4. **Mobile:** Access from phone, see agent progress
5. **Resources:** <500MB RAM with 5 active agents

---

### Open Questions

- [x] Package name: `longhouse` available on PyPI? → **Yes, published as `longhouse` v0.1.1**
- [x] Frontend bundle size? → **Measured 2026-02-11. See below.**

#### Frontend Bundle Size Baseline (2026-02-11)

Measured via `bun run build` (Vite 5.4, production mode, gzip sizes reported by Vite).

| Chunk | Raw | Gzipped | Notes |
|-------|-----|---------|-------|
| `index.js` (main) | 581 KB | 163 KB | React, router, query, shared UI |
| `OikosChatPage.js` | 287 KB | 81 KB | Oikos chat (markdown, syntax highlight) |
| `ChatPage.js` | 256 KB | 78 KB | Chat page (marked, DOMPurify) |
| `ForumPage.js` | 15 KB | 5 KB | Forum |
| `SwarmOpsPage.js` | 10 KB | 3 KB | Swarm ops |
| `index.css` (main) | 305 KB | 45 KB | Design tokens + all component styles |
| Route CSS (3 files) | 36 KB | 7 KB | Page-specific styles |
| **JS total** | **1,152 KB** | **332 KB** | |
| **CSS total** | **341 KB** | **53 KB** | |
| **Grand total** | **1,493 KB** | **385 KB** | |

**Target budget:**
- JS gzipped: **<400 KB** (current: 332 KB -- 17% headroom)
- CSS gzipped: **<60 KB** (current: 53 KB -- 12% headroom)
- Total gzipped: **<500 KB** (current: 385 KB -- 23% headroom)
- Largest single JS chunk: **<200 KB gzipped** (current: 163 KB)

**Improvement opportunities (if budget pressure increases):**
- `react-syntax-highlighter` ships all Prism languages; lazy-load or switch to a lighter highlighter
- `marked` + `react-markdown` are both bundled; consolidate to one markdown renderer
- Consider extracting `react-dom` into a shared vendor chunk for better caching
- [x] Shipper: bundled (single `pip install longhouse` ships sessions out-of-the-box)
- [x] Auth for remote access: auto-token flow in `longhouse connect` (commits `a7c11f96`, `0435639d`)
- [x] HTTPS: no built-in; recommend Caddy/nginx reverse proxy

---

### The Pitch

**Before:**
```
5 Claude terminals → context switching hell → close laptop = pause → no mobile
```

**After:**
```
pip install longhouse
longhouse serve

### Spawn agents from phone
### Close laptop, they keep working
### Wake up to completed PRs
```

Your personal cloud agent team. Always on. SQLite simple. Actually works.

---

### Prior Art & SQLite Best Practices (Sources)

Curated sources we can lean on when pushing SQLite to its limits, plus the concrete behaviors that matter for Longhouse’s design.

#### Key Learnings (What we take into the plan)

- **WAL is concurrent but single-writer.** Readers + one writer can coexist; multiple writers serialize. WAL needs shared memory and is unsafe on network filesystems.
- **Checkpoint starvation is a real operational risk.** Long-lived readers can prevent WAL checkpoints; WAL can grow unbounded without active checkpointing.
- **BEGIN IMMEDIATE is the right pattern for atomic job claims.** It grabs the write lock up front and fails fast if another writer is active.
- **busy_timeout is required for reliability under contention.** Set it on every connection so writes wait instead of throwing SQLITE_BUSY.
- **Minimum SQLite version: 3.35+.** We require RETURNING for SQLite-safe job claiming.
- **UPSERT + RETURNING are available.** Use ON CONFLICT for dedupe and RETURNING for atomic claim patterns.
- **Durability is a tradeoff.** `PRAGMA synchronous` and WAL checkpoint policy directly affect durability vs speed.
- **JSON1 is good enough for our needs.** JSON operators and functions exist, and JSON5 inputs are supported on newer SQLite builds.

#### Concurrency & Locking Reality

- **WAL improves read/write concurrency, but still single-writer.** Readers and writers can run concurrently, but only one writer at a time. WAL also requires shared memory and does not work over network filesystems.
  https://sqlite.org/wal.html
  https://www.sqlite.org/isolation.html
- **Checkpoint starvation is real.** Long-lived readers can prevent WAL checkpoint completion, letting the WAL grow without bound.
  https://sqlite.org/wal.html
  https://wchargin.com/better-sqlite3/performance.html
- **BEGIN IMMEDIATE grabs the write lock up-front.** It can return `SQLITE_BUSY` if another writer is active; DEFERRED upgrades on first write and can also hit `SQLITE_BUSY`.
  https://www.sqlite.org/lang_transaction.html
- **Timeouts matter under contention.** `PRAGMA busy_timeout` / `sqlite3_busy_timeout()` make writes wait instead of failing; high concurrency often needs longer timeouts than you expect.
  https://www.sqlite.org/c3ref/busy_timeout.html
  https://blog.skypilot.co/abusing-sqlite-to-handle-concurrency/

#### DML Features We Can Rely On

- **UPSERT (ON CONFLICT)** is supported and designed for unique constraints (SQLite 3.24+).
  https://www.sqlite.org/lang_upsert.html
- **RETURNING** is supported (SQLite 3.35+), but output order is unspecified.
  https://www.sqlite.org/lang_returning.html

#### Durability Tuning in WAL

- **`PRAGMA synchronous` tradeoffs**: `FULL` adds durability; `NORMAL` is faster but can reduce durability in WAL mode.
  https://www.sqlite.org/pragma.html
- **Auto-checkpoint defaults**: WAL checkpoints trigger at ~1000 pages by default; disabling checkpoints can let WAL grow unbounded.
  https://sqlite.org/wal.html

#### JSON Support

- **JSON1 functions and operators** (`json_*`, `->`, `->>`) exist; JSON5 extensions are supported in newer SQLite builds.
  https://www.sqlite.org/json1.html

#### Tooling / Prior Art

- **Litestream** — streaming replication of SQLite (WAL-aware) to object storage for backups/DR.
  https://litestream.io/how-it-works/
  https://litestream.io/reference/replicate/
- **LiteFS** — distributed SQLite via a FUSE filesystem + single-writer leases; production caveats are documented.
  https://fly.io/docs/litefs/
  https://fly.io/blog/introducing-litefs/
- **rqlite** — Raft-based replication of SQLite commands; single leader handles writes.
  https://rqlite.io/docs/design/
- **dqlite** — Canonical’s Raft-based HA SQLite (used in LXD).
  https://canonical.com/dqlite
  https://documentation.ubuntu.com/lxd/latest/reference/dqlite-internals/
- **Datasette + sqlite-utils** — ecosystem for creating/inspecting SQLite DBs; `sqlite-utils` CLI is great for migration/debugging.
  https://datasette.io/tools/sqlite-utils
  https://docs.datasette.io/en/0.56/ecosystem.html

---

### References

- [OpenClaw](https://github.com/moltbot/moltbot) — Lightweight agent platform
- [Datasette](https://datasette.io/) — SQLite-powered data tool
- [Litestream](https://litestream.io/) — SQLite replication (future?)
- [SQLite WAL mode](https://www.sqlite.org/wal.html) — Concurrent reads

---

### Changelog

- **2026-01-30:** Initial draft
- **2026-01-30:** Pivoted from "viewer" to "cloud agent ops center"
- **2026-01-30:** Proved SQLite + concurrent agents works — durable queue stays
- **2026-02-02:** Added User Value Proposition, Session Discovery architecture, "Fast to Fun" principle
- **2026-02-02:** Renamed Forum → Timeline throughout; elevated Resume as key feature

## Summary

Longhouse becomes the canonical home for agent sessions. Life Hub becomes a dashboard consumer. The core product is single-tenant and OSS-friendly, while hosted is per-user, always-on, and simple to explain. This unlocks a clean story and fast iteration without betting the company on multi-tenant security.
