# Longhouse Vision (2026)

Longhouse turns CLI agent sessions into durable objects you can find, address, message, and continue. Every session is live, resumable, and can talk to every other session.

Longhouse is a session-control kernel with a bundled human view. Timeline, search, Oikos, managed-local control, and related pieces can ship together, but the important ones must also be usable as standalone public interfaces from the terminal or over HTTP.

The product must feel instant and magical. Works on your laptop. Shines on a machine that stays on.

This is a living vision doc. It captures both the direction and the reasoning that got us here, so we can make fast decisions without re-litigating the fundamentals.

## How to Read This Doc

- Unless explicitly marked otherwise, statements in this document describe the **target architecture**.
- Sections labeled **Current State (as of YYYY-MM-DD)** are point-in-time implementation snapshots.
- This document is not an execution tracker. Repo docs should hold design and product artifacts, not task queues.

## Read Next

- **SQLite-only OSS plan:** this doc (see "SQLite-only OSS Pivot (Consolidated)" below)
- **OSS onboarding plan:** this doc (see "Onboarding UX" below)
- **Machine-facing canon:** `docs/specs/agents-machine-surface.md`

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
- Deployable units are top-level directories: `server/`, `engine/`, `web/`, etc.
- Some env vars / schema names may still use `ZERG_` during transition

---

## North Star

1. **The session is the durable object**: Sessions are not dead transcripts. A session is the live, addressable endpoint with transcript, presence, workspace context, and control path.
2. **Agents talk to each other through sessions**: Agent A does not scrape Agent B's logs; it addresses Agent B's session and gets a real answer in context.
3. **CLI/API-first public primitives**: The core surfaces must work from terminal and HTTP first. MCP, web UI, and chat channels are adapters on top, not the foundation.
4. **Unified timeline across providers**: Claude Code, Codex, Gemini sessions in one searchable archive. The shipper is the onramp — local sessions appear in Longhouse automatically.
5. **Works on your laptop, shines on a machine that stays on**: The product is useful the moment you install it. A durable box (VPS, homelab, Mac mini) unlocks always-on sessions. Hosted is the convenience path — we run that box for you.
6. Fast iteration as a solo founder: avoid multi-tenant security complexity unless required.

**Endgame aspiration:** The laptop becomes the terminal to the mainframe. All agent processes live on infrastructure — close the lid, they keep going. Longhouse gives you session-level control instead of SSH+tmux terminal forwarding. But this is where the product pulls you, not where it demands you start.

---

## User Value Proposition

Three promises, in order of importance:

1. **Your sessions, durable and addressable** — Every CLI session becomes a live object you can find, message, and continue. On a durable box, they survive lid-close and are reachable from any device. Hosted means we run that box for you.

2. **Every session is interactive** — Click any session in the Timeline, send a message, get a response. Sessions are live endpoints, not static logs. Resume a session from last week with full context intact.

3. **Your agents talk to each other** — Agent A working on auth can ask Agent B (which fixed OAuth last week) a direct question and get a real answer in context. Not log search — an actual conversation between sessions. The more sessions in Longhouse, the more powerful every new one becomes.

**Supporting value (the onramp):**
- **Never lose a session** — All your Claude Code, Codex, and Gemini sessions in one searchable timeline. This is what gets users in the door.
- **Find where you solved it** — Full-text search, semantic search, recall. Sub-10ms.

**Guiding principle: Fast to Fun.** Time from install to "oh cool" should be under 2 minutes.

## Product Journey

Users move through three phases. The product is useful at every phase — each one compounds.

1. **Findable (hook)**: Install Longhouse → local sessions appear in Timeline → "wow, I can see all my sessions with summaries and search." This is genuinely better than `/.resume` in Claude Code.
2. **Controllable (the product)**: User starts Longhouse sessions and discovers they can message, continue, and coordinate them after launch. The session becomes a durable object, not a terminal pane.
3. **Always-on (the pull)**: User moves Longhouse to a durable box (VPS, homelab, Mac mini, or hosted). Sessions survive lid-close. Agents cross-reference and talk to each other. The laptop becomes the control surface, not the engine.

The shipper is one-directional and that's fine — it's the gateway, not the product. Once sessions live on Longhouse, there's no sync problem because there's only one source of truth.

---

## Product Surface (2026-04 Direction)

**Primary human UX: Timeline + session interaction.** The web UI is the main integrated product. Timeline is the live dashboard showing all agent sessions. Click any session to view it, send messages, resume it, and coordinate work.

**Primary machine UX: CLI + API.** Nearly everything important should be operable from the terminal or over HTTP. Agents, scripts, CI, and background automations should not need a browser and should not depend on MCP to get real work done.

**Bundled product: Longhouse.** Longhouse bundles the human view and the support layers around the session-control kernel: timeline, session interaction, search, continuity, Oikos, managed-local control, and future TUI. The kernel is the center; the bundle makes it easier to use.

**Public primitives inside the suite:**
- **Session kernel**: sessions, events, presence, addressing, message delivery
- **Coordination**: wall, tail, peers, messages
- **Managed-local control**: launch, resume, and safely inject work into local/remote CLI sessions
- **Continuity**: search, recall, session detail, insights
- **Engine / shipper**: get local sessions into the unified archive
- **Runner**: remote command execution on user infrastructure

These pieces are designed to compose cleanly, but they are not equal. The session-control kernel is the product center; the rest either expose it or support it. Some may later split into their own distribution wedges, but they should not define the story now.

**Secondary: Oikos.** A lightweight assistant / receptionist, not a middleman in the session flow. Oikos handles quick questions, spawns sessions, and surfaces insights. It does not sit between the user and their sessions.

**Future: TUI.** `longhouse attach session-123` should feel like tmux into a remote Claude Code or Codex session. Same backend, different frontend.

**MCP and chat channels are adapters.** They are useful integration surfaces, but not the canonical contract. The system should still make sense if a user never configures MCP.

**What this means for execution:**
- Timeline + session interaction is the critical path for the bundled human surface
- Control of real sessions after launch is the core wedge; archive/search supports it
- CLI/API contracts come before MCP wrappers
- Oikos is a convenience layer, not a dependency for the core flow
- Separate by capability now; decide branding later, only after real pull exists
- Build one kernel and expose it through multiple interfaces rather than building separate silos

---

## Prelaunch MVP (2026-04)

The launch cut should be narrower than the full vision. Ship the session kernel first.

**What the MVP must prove:**

1. Longhouse can ingest CLI sessions and turn them into durable, addressable objects.
2. A user can recover context fast: search for a prior solution, inspect the raw session, and resume work without hunting through provider logs.
3. A user can coordinate work through the kernel itself: inspect the wall, tail a session, send a directed message, and continue a session from terminal/API or the bundled web UI.
4. First value happens free and locally (or via demo data) before hosted billing, account, or provisioning friction.
5. Hosted is the monetization and convenience layer, not the only way to understand the product.

**Launch-critical surfaces:**

- free local install + demo path
- real session ingest for Claude Code, Codex CLI, and Gemini CLI
- canonical `/api/agents/*` machine surface plus CLI parity for `wall`, `peers`, `tail`, `message`, `continue`, and inbox/session inspection
- one honest continuation path that is strong enough to demo today (Claude-first is acceptable)
- timeline, search, and session detail in the web UI as the integrated human surface

**Not launch blockers:**

- proactive Oikos operator mode
- Gmail / inbox / conversations as a primary wedge
- broad runner or jobs positioning
- full continuation parity across every provider
- perfect hosted self-serve onboarding

**Proof-of-value demo journey:**

1. Install Longhouse free locally and see demo sessions or real shipped sessions immediately.
2. Find a prior session where auth / retry / refactor logic was solved.
3. Open the raw session detail and recover the exact context that matters.
4. Use the kernel primitives to inspect the wall, tail a session, or send a directed session message.
5. Continue the current session from Longhouse and keep going from the recovered context.
6. Optional final beat: show the same session reachable from another device or hosted canary to prove the endgame without making hosted signup the first gate.

---

## Principles & Constraints

- **Always-on beats cold start** for paid users. Background agents are core; sleeping instances break the product.
- **Lossless logs are sacred.** The agent session history is not disposable.
- **The session is the system of record.** "Agent" is useful product language, but the durable object in the platform is the session.
- **Canonical interface order**: HTTP/SSE/WebSocket first, CLI second, MCP third, web UI for humans on top.
- **MCP is an adapter, not the boundary.** If a capability matters, it should exist without requiring a host-managed MCP install.
- **Free local wedge first, hosted convenience second**: launch onboarding must demonstrate value before billing or provisioning friction. Hosted remains the paid always-on path, but self-hosted/demo is the primary proof gate.
- **Progressive disclosure**: keep primary docs short and link to deeper runbooks; AGENTS.md must point to what else to read.
- **Single-tenant core (enforced)**: build fast, keep code simple, avoid multi-tenant security tax. Agents APIs reject instances with >1 user.
- **Hosted = convenience**: premium support and "don't think about it" operations.
- **Users bring their own API keys** for agent execution (cloud sessions use their Claude/OpenAI/etc. key). Longhouse provides a shared Groq pool for Oikos (the assistant) so it works out of the box. Longhouse is not an LLM billing intermediary.
- **No Postgres in core**. SQLite is the only DB requirement for OSS and hosted runtime instances.
- **Hosted architecture = control plane + isolated runtimes**. Control plane is multi-tenant; Longhouse app stays single-tenant.
- **Modules may stand alone later.** Keep capability boundaries clean enough that any strong primitive can be adopted or branded independently without a rewrite.

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

**Current State (as of 2026-03-12):**
- Commis uses CLI subprocess execution (workspace mode) and ingests resulting sessions into timeline storage.
- Slim Oikos (Phase 3) complete: loop simplified, tools flattened, services decoupled, overlapping memory removed, optional Memory Files retained, skills progressive disclosure, MCP server, quality gates, multi-provider research.
- Oikos in-process loop (`fiche_runner` + `oikos_react_engine`) still runs but is significantly slimmed; deferred items (dispatch contract, compaction API) are intentionally not tracked in repo docs until they become active implementation work again.

**Target end-state:**

- **Commis = CLI agent subprocess.** Every commis spawns a real CLI agent (Claude Code via `hatch`) in an isolated workspace. The user gets the exact same agent they use in terminal — same tools, same context management, same capabilities.
- **Standard mode (in-process ReAct loop) is deprecated.** The custom harness infrastructure (fiche_runner, message assembly, tool registry, skills system, ReAct engine — ~15K LOC) is legacy from pre-pivot. It will be removed incrementally. The ~60 builtin tools themselves are kept as a modular toolbox.
- **Oikos is a thin coordinator with configurable tools.** Oikos uses a simple LLM API loop (not a custom ReAct engine) with a configured subset of the toolbox. It delegates complex multi-step work to commis but can perform quick actions directly (send email, post to Slack, search sessions, etc.).
- **Commis sessions appear in the timeline.** When a commis finishes, its session JSONL is ingested through the same `/api/agents/ingest` path as shipped terminal sessions. All sessions are unified in one archive.

### Oikos Dispatch Contract (Target)

Oikos is a coordinator, so every turn should follow a simple dispatch decision:

1. **Direct response** (no tool call)
2. **Quick tool action** (search/recall/web/messaging, plus optional Memory Files when enabled)
3. **CLI delegation** (spawn commis with explicit backend + workspace mode)

Dispatch should honor user intent for backend selection:
- "use Claude/Claude Code" -> Claude backend
- "use Codex" -> Codex backend
- "use Gemini" -> Gemini backend
- no explicit preference -> configured default backend

Delegation modes should be explicit:
- **Repo mode:** git repo provided, clone/branch/diff flow
- **Scratch mode:** no repo, ephemeral workspace for analysis/research/ops-style tasks

**Current State (as of 2026-03-12):**
- Oikos still uses legacy prompt/tool guidance that is partly ops-era and not fully aligned with workspace-only delegation semantics.
- Backend selection for commis is mostly implicit (model mapping) rather than first-class user intent.
- Memory Files are the only surviving optional memory layer; the old Oikos note-memory stack and thread `memory_strategy` surface have been removed.

**What Longhouse owns:** orchestration, job queue, workspace isolation, timeline, search, resume, always-on infrastructure, runner coordination, and the continuity toolbox (session search, recall, insights, Oikos callbacks).

**What CLI agents own:** the agent loop, tool execution, file editing, bash, MCP servers, context management, streaming.

### Longhouse MCP Server (CLI Agent Integration)

MCP is useful, but it is not the platform boundary.

Managed CLI workspaces (Claude Code, Codex, Gemini) can call back into Longhouse via MCP so agents can access shared context mid-task. That is a strong integration pattern, especially inside Longhouse-managed workspaces. But the important boundary is that MCP wraps canonical Longhouse primitives; it does not define them.

**Canonical contract order:**
- HTTP / SSE / WebSocket for remote access
- CLI for terminal-native access
- MCP as an optional integration veneer

Any important capability exposed via MCP should also exist as an HTTP API and, when practical, a CLI command. A background agent should be able to `curl` an endpoint or run `longhouse ...` without depending on host-managed MCP configuration.

**Longhouse exposes as MCP tools:**
- `search_sessions` — find past solutions in the session archive
- `get_session_detail` — retrieve specific session content/events
- `get_session_events` — surgical event search within a known session
- `recall` — chunk-level semantic recall with event window retrieval
- `log_insight` / `query_insights` — write/read insights
- `notify_oikos` — commis reports status back to Oikos coordinator (currently logs)

**How it works:**
- Longhouse runs an MCP server (stdio transport for workspace/manual use, streamable HTTP for remote)
- `longhouse connect --install` sets up shipping hooks/service only; it does not modify the user's normal global Claude/Codex MCP config
- Commis spawned via `hatch` automatically get the Longhouse MCP server configured in their workspace-local Claude/Codex settings
- A hatch-spawned agent can search "how did we implement retry logic?" against the Longhouse archive mid-task

**Current State (as of 2026-03-12):** MCP server implemented with stdio and HTTP transport. Default toolset is continuity-focused (search/detail/event drill-down, recall, insights, notify). Longhouse no longer auto-registers this MCP server into normal local Claude/Codex installs; it is auto-configured for commis workspaces only (injected into workspace-local `.claude/settings.json` / `.codex/config.toml`). Quality gates (verify hooks) are also injected into commis workspaces. `notify_oikos` still logs (WebSocket delivery pending).

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

The durable object in Longhouse is the **session**, not an abstract agent persona.

We model explicitly:

- **conversation**: user-facing thread (Oikos thread)
- **run**: orchestration execution (Oikos run / commis job)
- **session**: durable provider-backed endpoint with transcript, presence, workspace context, and control path
- **event**: append-only message/tool call within a session
- **agent**: ephemeral execution wrapper around a unit of work (provider process, workspace, tools, policies, runtime metadata)

Relations:
- A conversation can spawn multiple runs.
- A run can spawn or resume one or more sessions.
- A session emits many events.
- An agent may create, resume, or operate on sessions, but it is not the canonical system of record.

Addressing rules:
- Sessions are addressed by `session_id`.
- Human-facing hints such as device name, title, repo, provider, and presence help discovery, but they are not the primary key.
- If we later add persistent agent identity, it must layer on top of sessions rather than replacing them.

This keeps the platform aligned with how real CLI agents behave: the wrapper is ephemeral, the session is what compounds.

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

Three deployment modes, same product loop. The session kernel, coordination primitives, and machine surface work identically everywhere. The only variable is where Longhouse runs and who keeps it running.

### Hosted (convenience path)
```
Sign in with Google -> provision isolated instance -> always-on
```
- One container stack per user (shared node, strict limits)
- Always-on — agents run 24/7, survive laptop close
- Users bring their own API keys for agent execution
- Shared Groq pool for Oikos (works out of the box)
- Access from any device (browser, future TUI)
- Premium support + no-ops maintenance

**Hosted path diagram:**
```
User signs up → Instance provisioned → Ship existing sessions (findable)
  → Start Longhouse sessions (controllable)
  → Sessions always-on, laptop is the window
```

### Self-hosted (the default)
```bash
curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse serve
```
- Local web UI on port 8080
- Local agents DB (SQLite)
- Shipper watches ~/.claude/, ~/.codex/, ~/.gemini/
- Full timeline + search visible locally
- Natural upgrade path to hosted when user wants always-on

(Homebrew formula planned for future.)

**Hosted provisioning architecture:**
```
User -> Control Plane -> Provision Longhouse instance (per-user)
     -> user.longhouse.ai -> Longhouse (UI+API) + DB
```

### Free Trial
- Optional: provisioned instance for a short trial
- Not required for the first proof of value
- Can hibernate after trial, but **paid instances stay hot**

---

## Onboarding UX

The first 2 minutes determine adoption. Onboarding must be zero-friction and demonstrate value before asking for configuration.

**Timeline-first:**
- Timeline (`/timeline`) is the default route for authenticated users
- The live session timeline (status + resume) IS the product - not a feature buried in nav
- New users land on Timeline immediately, not a dashboard or settings page

**Zero-key demo:**
- Demo sessions auto-seed on first run (when sessions table is empty); `SKIP_DEMO_SEED=1` to disable
- Users see the product working before any configuration
- Chat/LLM features prompt for keys only when actually needed

**Agent-native evaluation requirements (target):**
- The first successful task should happen before credit card entry
- The free path should not require email verification before the user can run a meaningful local or demo flow
- Programmatic use must exist at the free tier through the CLI and `/api/agents/*`
- If/when hosted trials exist, their limits must be generous enough to complete one real inspect / coordinate / continue loop

**Guided empty state:**
When Timeline is empty, show a 3-step path:
1. Connect shipper (optional) - for real session sync
2. Load demo - instant gratification, no keys
3. Explore timeline - filters, search, detail views

This is not a modal or tour - it's inline content that disappears once sessions exist.

**Current State (as of 2026-02-11):** Auto-seed on first run and guided empty state with "Load demo sessions" CTA are implemented. `longhouse serve --demo` / `--demo-fresh` also supported. Multi-CLI detection (Claude Code, Codex CLI, Gemini CLI) in onboard wizard with guidance when no CLI is found. README now carries the canonical install and onboarding guidance.

**Docs-as-source validation (target, not implemented yet):**
README will contain an `onboarding-contract` JSON block that CI executes:
- Steps to run (`pip install longhouse`, `longhouse serve`, health check)
- Cleanup commands
- CTA selectors to verify (e.g., `[data-testid='demo-cta']`)

If the README drifts from reality, CI should fail. No hidden env flags — everything declared in the contract.

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
- **README:** canonical install path, onboarding wizard flow, troubleshooting, and manual install entry points
- **Goal:** time-to-value < 2 minutes and a visible session in the Timeline

---

## Mental Model (Core vs Scheduler vs Jobs)

Longhouse is the product. Sauron is the standalone scheduler service. Jobs are the thing the scheduler runs.

- **Longhouse Core**: UI + API + agents. Runs standalone, with a small builtin jobs framework for product maintenance.
- **Sauron**: separate cron/scheduler service for broader automation workloads. David's private jobs pack runs here today, outside the Longhouse product runtime.
- **Jobs Pack**: the job source a scheduler needs. Options: a local template for zero-config OSS, or a private repo for real workloads.

This framing keeps OSS onboarding simple while preserving the power-user path without blurring Longhouse and Sauron together.

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
control-plane/           # NEW - tiny FastAPI app
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
{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "/abs/path/to/longhouse-engine ship --file \"$TRANSCRIPT\"", "async": false, "timeout": 30}]}]}}
```

Benefits:
- Instant push on Stop (no debounce delay)
- Works on any OS where hooks are supported
- `longhouse connect --install` auto-injects and verifies hook registration

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

**Current State (as of 2026-02-20):**
- **Rust engine daemon** (`engine/`) is the only shipping path. Python watcher/shipper deleted (2026-02-20). Resource profile: 27 MB RSS idle (vs 835 MB Python), 0% CPU idle, 3 threads, <1s wake-to-ship latency. Uses FSEvents (macOS) / inotify (Linux) via `notify` crate, tokio single-threaded runtime, zstd compression (12x faster than gzip).
- `longhouse-engine connect --flush-ms 500 --fallback-scan-secs 300 --compression zstd --log-dir ~/.claude/logs` is the daemon command. Managed by `longhouse connect --install` (launchd/systemd). Watches Claude, Codex, and Gemini directories.
- **Python `longhouse connect`** manages service lifecycle only (`--install`, `--uninstall`, `--status`) plus shipping hooks. No Python shipping code remains.
- **Stop hook calls `longhouse-engine ship --file "$TRANSCRIPT"` directly** (not via Python wrapper). Absolute path baked at install time via `get_engine_executable()`. `exec` replaces the shell — zero Python overhead. Hook registration is currently `async: false` (sync).
- **Presence hooks (2026-02-20):** Four lifecycle events now emit real-time state to `POST /api/agents/presence`: `UserPromptSubmit→thinking`, `PreToolUse→running` (with tool name), `PostToolUse→thinking`, `Stop→idle`. Stored in `session_presence` table (one row per session, stale after 10 min). Active sessions endpoint joins presence for real status vs heuristic fallback.
- **Hardened (2026-02-18):** Rate-limited warn! via `error_tracker`. Daily rolling log files to `~/.claude/logs/`. Watcher channel bounded. `raw_line` capped at 32KB. Offline mode, spool dead/prune, 429 backoff with jitter, heartbeat every 5 min to `POST /api/agents/heartbeat`.

**Testing:** `make qa-live` — 5 Playwright tests against live instance (~5s). Auth + timeline, forum (session rows), session detail, health, agents API. Exit 0=pass. Rust engine: 45+ unit tests. Python: heartbeat + stale agent tests in `tests_lite/`.

**Magic moment:** user sends a message in Claude Code → `UserPromptSubmit` hook fires → "Thinking..." appears in Forum → tool runs → "Running bash" → response completes → session ships → all within the same page refresh cycle.

---

## Ingest Protocol

The shipper-to-Longhouse ingest must be robust:

**Target batching:**
- Collect events for up to 1 second or 100 events, whichever first
- Gzip compress payload
- Single HTTP POST per batch

**Current State (as of 2026-02-18):**
- Rust engine streams JSON directly into compressor (gzip or zstd) — full JSON never materialized in memory. Posts per parsed file chunk. `raw_line` capped at 32KB to prevent single-line bloat.
- Offline spool replay relies on DB dedupe (`source_path`, `source_offset`, `event_hash`) rather than explicit idempotency headers.
- Offline mode active: when `ConnectError` is detected, engine skips all parse/compress/ship and runs a connectivity health-check every 60s instead of hammering a downed server.

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

## Inter-Agent Communication (The Moat)

The unique capability Longhouse enables: **sessions are live, addressable endpoints, not dead transcripts.** One session can message another session and get a real answer in context.

### Why This Matters

Current state of the art for agent-to-agent knowledge sharing:
- **Copy-paste**: User manually copies context between terminal sessions. Tedious, error-prone.
- **Log search / RAG**: Agents read other agents' logs via MCP search tools. Better, but read-only — you get stale text, not a live conversation.
- **Longhouse**: Session A addresses Session B, Longhouse delivers the message at a safe boundary, and Session B responds with full context of its original work. No human in the loop.

### How It Works

Built on top of session-native addressing and delivery:

```
Agent A (working on auth)
  → addresses Session B: "How did you handle token refresh in that OAuth session?"
  → Longhouse resolves the target session and delivers immediately or queues until a safe boundary
  → Session B responds with full context of its original work
  → Response fed back to Agent A
  → Agent A continues with real, contextual knowledge
```

The key point is that **session identity and delivery semantics belong to Longhouse**. The sender may be a user, an agent, or an automation, but the addressable unit is the session.

**Addressing:**
- Canonical target = `session_id`
- Human helpers = device name, repo, title, provider, presence
- Discovery surfaces = wall for rich visibility, peers for a tight active list

### Network Effect

This creates a compounding advantage:
- Every session shipped to Longhouse becomes a queryable, resumable knowledge node
- New agents start with access to every previous session's expertise
- The value of Longhouse grows with usage — this is the moat
- Isolated runs on competitors don't compound; Longhouse sessions do

### What Needs to Be Built

- Session-native message queue and delivery state machine
- Safe-boundary delivery for live managed sessions, plus `stored_only` fallback for unmanaged ones
- Discovery and addressing primitives (`wall`, `tail`, `peers`, `message_session`)
- Acknowledgement and audit semantics distinct from fetch and delivery
- Permissions / scoping for who can address which sessions (default: same instance)
- Optional future protocol adapters (A2A, etc.) layered on top of Longhouse addressing rather than replacing it

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
5. If instance exists: redirect through control-plane open-instance flow to
   {user}.longhouse.ai/api/auth/accept-token?token=xxx
6. User instance validates token, sets session cookie, redirects to `/timeline`
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

The monorepo ships one integrated product, but the package should expose public primitives cleanly enough that users can adopt narrow slices without buying into all of Longhouse.

**What's in the package:**
- `longhouse` CLI (Python, via pip)
- Embeds: FastAPI backend, React frontend (built), engine/shipper, coordination surfaces, continuity APIs
- Default: SQLite for local DB (zero-config). Postgres is not part of core/runtime.
- MCP remains optional; the core product must still be useful over CLI and HTTP alone.

**Core commands:**
```bash
longhouse serve           # Start local server (SQLite, port 8080)
longhouse connect --url <url>   # Run engine daemon in foreground (watch + fallback scan)
longhouse connect --url <url> --install   # Install/start managed engine service + hooks
longhouse ship            # One-time manual sync
longhouse status          # Show current configuration
```

**Target public primitives over time:**
```bash
longhouse wall            # Raw session wall / coordination metadata
longhouse peers           # Tight active-session discovery
longhouse tail <session>  # Session tail / recent activity
longhouse message <session> --from <session> "..."   # Session-to-session messaging
longhouse search "..."    # Continuity/search entry point
```

These commands are not just convenience wrappers. They define the agent-native contract for terminal use and should map cleanly onto public HTTP APIs.

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


**Status:** Complete
**Goal:** `pip install longhouse && longhouse serve` — session kernel in under 5 minutes (SQLite only)
**Reality check:** Postgres remains for control plane only; OSS/runtime is SQLite-only.
**Naming note:** Public brand is Longhouse; Python package is still `zerg` internally.

---

**Implementation (2026-02-01):** All phases complete. SQLite pivot is done — flat tables (no schemas), WAL mode, 3.35+ enforced, RETURNING-based job claiming, durable checkpoints via `langgraph-checkpoint-sqlite`, bundled frontend. Key decisions locked: SQLite minimum 3.35+, flat tables, SQLite-backed job queue, bundled `web/dist`. See git history for phase-by-phase details.

---

_Detailed execution plan (Phases 0–7) and day-by-day implementation plan completed 2026-02-01. See git history for step-by-step details._

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
│  Timeline (primary surface)      Agent Sessions (always-on) │
│  ┌─────────────────────────┐     ┌────────────────────────┐  │
│  │ Search all work         │     │ Claude/Codex/Gemini    │  │
│  │ See what's running now  │────▶│ keep running in cloud  │  │
│  │ Resume from any device  │◀────│ and talk to each other │  │
│  └─────────────────────────┘     └────────────────────────┘  │
│            ▲                              ▲                  │
│       [Web + Phone]             Oikos (receptionist layer)   │
│                                                             │
│  Oikos is optional UX glue, not the core product surface    │
│  The moat is resumable, communicating, always-on sessions   │
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

### Frontend Bundle Size Baseline (2026-02-11)

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

### SQLite Prior Art

Key tools: [Litestream](https://litestream.io/) (streaming replication), [LiteFS](https://fly.io/docs/litefs/) (distributed FUSE), [rqlite](https://rqlite.io/) (Raft replication), [Datasette + sqlite-utils](https://datasette.io/) (inspection). Key SQLite facts: WAL is single-writer, `busy_timeout` is required, `BEGIN IMMEDIATE` for atomic claims, 3.35+ for RETURNING.

---

## Summary

Longhouse turns CLI agent sessions into durable, controllable objects and bundles the human view around that kernel. Life Hub becomes a dashboard consumer. The durable kernel is the session. The important primitives must work over CLI and HTTP first, with MCP as an optional adapter. The core product remains single-tenant and OSS-friendly. Self-hosted is the default path; hosted is convenience. This gives Longhouse a clean story today and preserves the option for breakout standalone surfaces later without rewriting the platform.
