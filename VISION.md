# Zerg Vision (2026)

Zerg is an AI agent orchestration platform where AI does the work and humans manage the system. The product must feel instant, always-on, and magical: your local Claude/Codex/Gemini sessions appear as a clean, queryable timeline inside Zerg with zero friction.

This is a living vision doc. It captures both the direction and the reasoning that got us here, so we can make fast decisions without re-litigating the fundamentals.

---

## North Star

1. Zero-friction onboarding for OSS builders: `brew install zerg` or `docker compose up`.
2. Always-on agents: background work continues even when the user is away.
3. Unified, queryable agent sessions across providers (Claude, Codex, Gemini, Cursor, Oikos).
4. A hosted option that feels like "I pay $5 and never think about it."
5. Fast iteration as a solo founder: avoid multi-tenant security complexity unless required.

---

## Principles & Constraints

- **Always-on beats cold start** for paid users. Background agents are core; sleeping instances break the product.
- **Lossless logs are sacred.** The agent session archive is not disposable.
- **OSS-first story**: easy to explain on HN/Twitter without enterprise jargon.
- **Single-tenant core**: build fast, keep code simple, avoid multi-tenant security tax.
- **Hosted = convenience**: premium support and "don't think about it" operations.
- **Users bring their own API keys**. Zerg is orchestration + UI + data, not LLM compute billing.

---

## What Changed (Reality Check)

- Zerg started as a hand-written ReAct system. It has evolved into an orchestration layer around Claude Code and other CLIs.
- The "real" session log is the provider JSONL stream. Zerg's internal threads are operational state, not the canonical archive.
- Life Hub currently owns the agents schema; Zerg should own it so OSS users are self-sufficient.

---

## The Trigger (and Why It Matters)

Oikos session picker threw:
```
relation "agents.events" does not exist
```

Cause: Zerg was querying `agents.sessions` and `agents.events` in Life Hub's database. Those tables do not exist in Zerg's DB.

This revealed the deeper issue: Zerg was not standalone. OSS users who `brew install zerg` would hit Life Hub errors. That is a dead end for adoption.

---

## The Core Shift

**Zerg is now primarily an orchestration layer around CLI agents.**

- Commis runs are mostly Claude Code sessions.
- The archive of truth is the provider session log, not Zerg's internal thread state.
- The "magic" is taking obscure JSONL logs and turning them into a searchable, unified timeline.

This is the product. Everything else supports it.

---

## The Canonical Idea (Unified Sessions)

Agent sessions are unified into a single, lossless, queryable database:

- **sessions**: one row per provider session (metadata, device, project, timestamps)
- **events**: append-only rows for each message/tool call (raw text + parsed fields)

This schema is already proven in Life Hub. We are moving it to Zerg and making Zerg the source of truth.

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

**Stream 2: Zerg commis -> Life Hub**
```
Zerg spawns commis
  -> Claude Code runs in container
  -> commis_job_processor ships to Life Hub
  -> same Life Hub tables
```

Both end up in Life Hub, so Zerg depends on Life Hub. We are reversing that: Zerg becomes the canonical home for agent sessions, and Life Hub becomes a reader.

---

## Product Paths

### OSS Local (default path)
```
brew install zerg
zerg up
```
- Local web UI
- Local agents DB
- Shipper runs on the same machine (zero config)
- Full end-to-end flow is visible locally

### Hosted (paid, always-on)
```
Sign in with Google -> provision isolated instance -> always-on
```
- One container stack per user (shared node, strict limits)
- Always-on background agents
- Users bring their own API keys
- Premium support + no-ops maintenance

### Free Trial
- Optional: provisioned instance for a short trial
- Can hibernate after trial, but **paid instances stay hot**

---

## Mental Model (Core vs Scheduler vs Jobs)

Zerg is the product. Sauron is the scheduler service. Jobs are the thing it runs.

- **Zerg Core**: UI + API + agents. Runs standalone (no scheduler required).
- **Sauron**: cron/scheduler service. Optional for Zerg overall, but its whole purpose is to run jobs (think “cron for Zerg jobs/commis/fiches”).
- **Jobs Pack**: the job source Sauron needs. Options: a local template for zero-config OSS, or a private repo for real workloads.

This framing keeps OSS onboarding simple while preserving the “power user” path.

---

## Hosting Architecture (Indie-Scale)

We do not do "one VM per user." We do:

- **Control plane (tiny)**: signup -> payment -> provision -> route
- **Runtime**: one container stack per user (Zerg + DB) on shared nodes
- **Routing**: wildcard DNS + reverse proxy to per-user container
- **Always-on**: paid instances never sleep

This preserves instant agents while keeping $5-10/month viable.

**The control plane is not a second product**; it is a provisioning layer. The app remains single-tenant.

---

## Control Plane Details

The control plane is minimal infrastructure that provisions and routes to user instances.

**Signup flow:**
```
1. User visits swarmlet.com
2. "Sign in with Google" -> OAuth
3. User record created in control plane DB
4. Stripe checkout for $5/month (or free trial)
5. On payment success: provision instance
6. Redirect to alice.swarmlet.com
```

**Provisioning (via Coolify API):**
```
POST /api/applications
{
  "name": "zerg-alice",
  "image": "ghcr.io/cipher982/zerg:latest",
  "env": {
    "DATABASE_URL": "postgres://...",
    "USER_EMAIL": "alice@example.com",
    "INSTANCE_ID": "alice"
  }
}
```

**Routing:**
- Wildcard DNS: `*.swarmlet.com -> load balancer IP`
- Traefik/Caddy routes by subdomain to correct container
- Each container exposes on unique internal port

**What control plane stores:**
- User email, Stripe customer ID, subscription status
- Instance ID, provisioned timestamp, current state
- NOT user data (that's in their isolated instance)

---

## Runner Architecture (User-Owned Compute)

Runners are user-owned daemons that execute commands on infrastructure the user controls.

**What a runner is:**
- Node.js daemon installed on user's laptop/server
- Connects **outbound** to Zerg (no firewall holes needed)
- Executes shell commands when Zerg requests (`runner_exec` tool)
- Example: run tests, git operations, deploy scripts

**How runners fit with isolated hosting:**
```
User's hosted Zerg instance (always-on)
         ↕ WebSocket
User's laptop runner daemon
         ↓
Local command execution (npm test, etc.)
```

Each user's Zerg instance only sees their own runners. Isolation is natural.

**Runner registration:**
- `zerg runner register` generates credentials
- Runner connects with those credentials
- Zerg validates runner belongs to the user

---

## Commis Execution Model

Commis (background agent jobs) execute in two modes:

**Standard mode (in-process):**
- Runs in the Zerg FastAPI process
- Direct LLM API calls using user's configured keys
- Fast, suitable for short tasks (<5 min)
- No container overhead

**Workspace mode (subprocess):**
- Spawns `hatch` CLI as subprocess
- Isolated git workspace per job
- Long-running tasks (up to 1 hour)
- Changes captured as git diff

**What's containerized vs not:**
- ✅ Containerized: Zerg backend + Postgres (per-user isolation)
- ❌ Not containerized: Commis execution (runs in Zerg process or as subprocess)
- ❌ Not containerized: Runner commands (run on user's own machine)

This keeps the execution model simple while still isolating user data.

---

## Commis Execution Isolation

Workspace mode provides git isolation but NOT process isolation. Current state:

```
✓ Git clone isolation (own directory)
✓ Git branch isolation (oikos/{run_id})
✓ Process group isolation (killable on timeout)
✗ Filesystem isolation (can read /etc, ~/, escape to other repos)
✗ Network isolation (can exfiltrate data)
✗ Resource limits (can OOM host)
```

For autonomous/untrusted agents, this is a blocker. Commis needs two trust levels:

**Trusted (interactive, from laptop):**
- User-initiated via Oikos
- Runs in subprocess, no container
- Fast startup, full access to workspace
- User is accountable for the prompt

**Untrusted (scheduled, autonomous):**
- Patrol scans, cron jobs, external triggers
- Runs in container sandbox:
  - Read-only filesystem (except /repo workspace)
  - No network by default
  - Resource limits (CPU, memory)
  - User namespace isolation
- Container overhead (~5-15s) is acceptable for background work

**Implementation:** CloudExecutor gets `sandbox: bool` flag.
- `sandbox=False`: current subprocess behavior (default)
- `sandbox=True`: docker/podman with hardened config

This unlocks:
- Patrol (autonomous code analysis) as a commis consumer
- Safe 24/7 background agents
- Clear trust boundary for OSS users running untrusted prompts

---

## Shipper (Real-Time Sync)

We need real-time sync (not polling). The shipper:

- Watches local provider session files via OS file watching (FSEvents/inotify)
- Debounces rapid writes (Claude streams to file)
- Sends incremental events to Zerg in batches
- Runs as a background service (launchd/systemd)
- Falls back to periodic scan to catch missed events/rotations
- Spools locally when offline, syncs on reconnect

Commands:
- `zerg connect <url>` installs and starts the shipper for remote hosted instances.
- Local installs auto-detect and run inline.

**Magic moment:** user types in Claude Code -> shipper fires -> session appears in Zerg before they switch tabs.

---

## Ingest Protocol

The shipper-to-Zerg ingest must be robust:

**Batching:**
- Collect events for up to 1 second or 100 events, whichever first
- Gzip compress payload
- Single HTTP POST per batch

**Offline resilience:**
- Local SQLite spool when Zerg unreachable
- Replay on reconnect with idempotency keys
- Dedup by (session_id, source_path, source_offset, event_hash)

**Authentication:**
- Per-device token issued during `zerg connect`
- Token scoped to user's instance
- Revocable if device compromised

**Rate limits:**
- 1000 events/minute per device (soft cap)
- Backpressure via HTTP 429

---

## Agents Schema (Source of Truth)

Adopt the Life Hub schema as Zerg's canonical agent archive:

- Lossless storage: raw text + raw JSON
- Queryable: extracted fields for search
- Append-only: events never updated
- Dedup: hash + source offset
- Optional TimescaleDB (fallback to vanilla Postgres if missing)

Zerg owns this data. Life Hub becomes a reader.

---

## Security Model (Minimal, Practical)

We avoid most multi-tenant risk by isolating users. Remaining risks:

- **Ingest endpoint**: must authenticate and protect against replay/injection.
- **Device identity**: issue per-device tokens during `zerg connect`.
- **Rate limits**: basic caps per device to prevent abuse.
- **Data leakage**: isolated instance prevents cross-user leaks by default.

Containerization (Docker/containerd/k3s) protects execution, **not** data isolation. It does not replace tenant safety if we ever go multi-tenant.

---

## API Key Management

Users bring their own LLM API keys. Zerg stores and uses them securely.

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

`brew install zerg` must "just work" for the 90% case.

**What's in the package:**
- `zerg` CLI (Python, via pipx or standalone binary)
- Embeds: FastAPI backend, React frontend (built), shipper
- Default: SQLite for local DB (zero-config). Full agents/search features require Postgres.
- Optional: Postgres for production use

**Commands:**
```bash
zerg up              # Start local server (SQLite, port 30080)
zerg up --postgres   # Use Postgres (reads DATABASE_URL)
zerg connect <url>   # Connect shipper to remote instance
zerg ship            # One-time manual sync
```

**Docker alternative:**
```bash
docker compose up    # Full stack with Postgres
```

**DB note (self-contained):**
- Should work with a single local Postgres spawned by docker compose.
- Also support pointing to an existing Postgres (if users already have one).
- Scheduler/queue needs ops/agents schemas to bootstrap automatically.

**What's NOT in the package:**
- Node.js runner daemon (separate install if needed)
- Postgres (optional, user provides)
- LLM API keys (user provides)

**Homebrew formula sketch:**
```ruby
class Zerg < Formula
  desc "AI agent orchestration platform"
  homepage "https://swarmlet.com"
  url "https://github.com/cipher982/zerg/releases/..."

  depends_on "python@3.11"

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

Multi-tenant is a possible future, not a current requirement.

---

## Migration Path (From Today)

1. **Agents schema in Zerg** (alembic migration from Life Hub SQL)
2. **Ingest API** (`POST /api/agents/ingest`)
3. **Query API** (`GET /api/agents/sessions`)
4. **Port shipper** into Zerg CLI
5. **Update commis** to ingest locally
6. **Backfill** Life Hub history if desired
7. **Life Hub reads Zerg** (dashboard only)

Optional safety step:
- Dual-write during migration (Life Hub + Zerg) then reconcile counts/hashes.

---

## Alternatives Considered (and Rejected)

1. **Shared multi-tenant SaaS now**
   - Efficient but high security tax; RLS would be required immediately.

2. **Per-user VM**
   - Too expensive; kills $5-10 pricing.

3. **Scale-to-zero for paid**
   - Breaks always-on agents; bad UX.

4. **Separate "agent-sessions" service**
   - Adds infra complexity; Zerg should own this data.

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
- `/health` endpoint on each user instance
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

## Risks & Mitigations

- **Ship missed events** -> periodic scan fallback + dedup by (path, offset, hash).
- **Provider schema drift** -> raw_text/raw_json preserved; extracted fields are best-effort.
- **Archive loss** -> backups + stable DB (AGENTS_DATABASE_URL if needed).
- **Overlapping identities** -> explicit conversation/run/session links.
- **Scale shocks** -> per-user limits, rate caps, pre-warmed headroom.

---

## Life Hub Integration (David-specific)

Life Hub becomes a dashboard consumer, not the data owner.

**API contract:**
- Life Hub calls Zerg's `/api/agents/sessions` endpoint
- Authenticated via service token (not user OAuth)
- Read-only access to session metadata and events

**What Life Hub displays:**
- Agent session timeline alongside health, finance, etc.
- Cross-project analytics (sessions per day, tool usage)
- Does NOT modify Zerg data

**Configuration:**
```env
# In Life Hub
ZERG_API_URL=https://swarmlet.com/api
ZERG_SERVICE_TOKEN=xxx
```

This is David's personal integration. OSS users don't need Life Hub at all.

---

## Open Questions

1. ~~TimescaleDB support in Zerg deployments?~~ → Fallback to vanilla Postgres with time-based partitioning.
2. Session resume: store raw JSONL alongside events or reconstruct on demand?
3. Backfill tooling: how to avoid duplicates and ensure fidelity?
4. How should Oikos conversations map into sessions (provider="oikos")?
5. Artifact storage: should file diffs, screenshots, patches be stored alongside events or separate?
6. Runner daemon packaging: separate install or bundle with `zerg` CLI?
7. Secrets for jobs: job-scoped encrypted bundles (age) vs sops vs external secrets manager?
8. Jobs pack UX: local template by default vs required private repo from day one?

---

## Summary

Zerg becomes the canonical home for agent sessions. Life Hub becomes a dashboard consumer. The core product is single-tenant and OSS-friendly, while hosted is per-user, always-on, and simple to explain. This unlocks a clean story and fast iteration without betting the company on multi-tenant security.
