# AGENTS.md

## What This Is

**Zerg** is an AI agent orchestration platform. **Jarvis** is a voice/text UI that connects to Zerg. Together they form a "swarm" system where Jarvis handles user interaction and Zerg manages multi-agent workflows.

## Architecture Philosophy (v2.2)

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees.

Key principles:
- **No keyword routing** — Jarvis decides when to delegate, not keyword matching
- **No specialized workers** — Workers are general-purpose agents with SSH access
- **Session Durability** — Runs survive disconnects and timeouts; work continues in background
- **Lazy tool loading** — 65+ tools available via `search_tools()`, ~12 core tools pre-bound
- **Progressive disclosure** — Large outputs stored by reference, evidence fetched on-demand
- **Event-driven** — Workers notify when done, no polling loops

Full spec: `docs/specs/durable-runs-v2.2.md`
Architecture overview (v2.1): `docs/specs/jarvis-supervisor-unification-v2.1.md`
Historical (v2.0): `docs/archive/super-siri-architecture.md`

## For AI Agents

**Assume `make dev` is running** — during coding sessions, the dev stack is typically already up.

When running, you have live access to:
| URL | What |
|-----|------|
| http://localhost:30080 | Main entry → redirects to /dashboard (dev mode) |
| http://localhost:30080/dashboard | Dashboard UI |
| http://localhost:30080/chat | Jarvis chat |
| http://localhost:30080/landing | Landing page preview (dev only) |
| http://localhost:30080/api/* | Backend API |

**Note:** In dev mode (auth disabled), `/` redirects to `/dashboard`. To preview the landing page, use `/landing`.

**Debug with:**
- **Playwright MCP** — take screenshots, click elements, run browser code
- **API calls** — `curl localhost:30080/api/health` or WebFetch tool
- **Logs** — `make logs` to tail all services
- **Supervisor/Worker Debug Pipeline** — see dedicated section below

## Supervisor/Worker Debug Pipeline

Three-layer debugging infrastructure for investigating LLM behavior in supervisor/worker runs.

**Note**: The supervisor/worker path is now **LangGraph-free by default** (as of 2026-01-13). The ReAct loop runs in `supervisor_react_engine.py` without LangGraph checkpointing. LangGraph is still used for the workflow engine.

### Quick Reference

| Question | Tool | Command |
|----------|------|---------|
| "Debug this trace end-to-end" | **Trace Debugger** | `make debug-trace TRACE=<uuid>` |
| "What recent traces exist?" | Trace Debugger | `make debug-trace RECENT=1` |
| "What's the trace coverage?" | Trace Debugger | `make trace-coverage` |
| "What messages are in the thread?" | Thread Inspector | `make debug-thread THREAD_ID=1` |
| "Are there duplicate messages?" | Validator | `make debug-validate THREAD_ID=1` |
| "What's the workflow LangGraph checkpoint state?" | Inspector | `make debug-inspect THREAD_ID=1` |
| "What did the LLM see/respond?" | Audit Log | `uv run python scripts/debug_run_audit.py --run-id 82` |
| "Can I replay with different prompts?" | Replay Harness | `uv run python scripts/replay_run.py <run_id>` |
| "List or trigger scheduled jobs" | Jobs API | `curl localhost:30080/api/jobs/` |

### Trace-Centric Debugging (Recommended)

**One ID to rule them all**: Every supervisor run gets a `trace_id` (UUID) that propagates through workers and LLM audit logs. Copy from UI, debug with agents.

**Copy from UI**: In dev mode, the trace_id appears in the bottom-right corner of the chat UI. Click to copy.

**Debug any trace**:
```bash
# Show unified timeline for a trace
make debug-trace TRACE=abc-123-def

# Full details (LLM messages, tool calls)
make debug-trace TRACE=abc-123-def LEVEL=full

# Just errors and anomalies
make debug-trace TRACE=abc-123-def LEVEL=errors

# List recent traces
make debug-trace RECENT=1

# Check trace completion coverage
make trace-coverage
```

**What it shows**:
- Unified timeline across supervisor runs, workers, and LLM calls
- Duration and token usage per phase
- Anomaly detection (failed workers, slow LLM calls, stuck workers)
- JSON output with `--json` flag for AI agents

**MCP Tool for AI Agents**:

AI agents (Claude Code, Cursor) can debug traces programmatically via the `debug-trace` MCP server:

```json
// Cursor: .cursor/mcp.json (already configured)
// Claude Code: Add to your MCP config
{
  "debug-trace": {
    "command": "uv",
    "args": ["run", "python", "scripts/mcp_debug_trace/server.py"],
    "cwd": "/path/to/zerg",
    "transport": "stdio"
  }
}
```

Available tools:
- `debug_trace(trace_id, level)` - Get full trace timeline (level: summary/full/errors)
- `list_recent_traces(limit)` - List recent traces for discovery

Workflow:
1. User copies trace_id from UI footer
2. User asks: "debug trace abc-123"
3. AI calls MCP tool → gets full context
4. AI explains what happened

### Layer 1: Thread Inspector (`debug_langgraph.py`)

Inspect DB state (ThreadMessage table) and workflow LangGraph checkpoints.

```bash
cd apps/zerg/backend

# View messages in a thread (compact JSON)
make debug-thread THREAD_ID=1

# Validate message integrity (duplicates, ordering, tool response counts)
make debug-validate THREAD_ID=1

# Inspect workflow LangGraph checkpoint state
make debug-inspect THREAD_ID=1

# Batch queries (minimal tokens for AI agents)
echo '{"queries":[{"op":"thread","thread_id":1,"limit":5},{"op":"validate","thread_id":"1"}]}' | make debug-batch
```

**Validation rules:**
- No duplicate messages (same role + content)
- Messages ordered by sent_at
- Each AIMessage tool_call has exactly one ToolMessage response
- No duplicate tool response content

### Layer 2: LLM Audit Log (`llm_audit_log` table)

Every LLM request/response is stored in the database for postmortem debugging.

```bash
cd apps/zerg/backend

# View LLM interactions for a run
uv run python scripts/debug_run_audit.py --run-id 82

# Include full message arrays
uv run python scripts/debug_run_audit.py --run-id 82 --show-messages
```

**What's captured:**
- Full messages array sent to LLM (serialized)
- Response content and tool_calls
- Token counts (input, output, reasoning)
- Duration, phase, model
- Correlation to run_id, worker_id, thread_id

**Query directly:**
```sql
SELECT phase, model, message_count, duration_ms,
       LEFT(response_content, 100) as response_preview
FROM llm_audit_log
WHERE run_id = 82
ORDER BY created_at;
```

### Layer 3: Replay Harness (`replay_run.py`)

Re-run a supervisor with mocked tool results to test prompt changes.

```bash
cd apps/zerg/backend

# List recent runs
uv run python scripts/replay_run.py --list-recent 20

# Dry run (preview what would happen)
uv run python scripts/replay_run.py <run_id> --dry-run

# Full replay (real LLM, mocked spawn_worker)
uv run python scripts/replay_run.py <run_id>

# With options
uv run python scripts/replay_run.py <run_id> --match-threshold 0.8 --max-context-messages 50
```

**What's mocked:** `spawn_worker` returns cached results from original run
**What's real:** LLM calls (that's what you're testing)
**Safe by default:** Unsafe tools (send_email, http_request) are blocked unless `--allow-all-tools`

### Debugging Workflow

1. **User reports issue** → Get the run_id from logs or dashboard
2. **Check thread state** → `make debug-validate THREAD_ID=<id>` — any integrity issues?
3. **View LLM interactions** → `uv run python scripts/debug_run_audit.py --run-id <id>` — what did LLM see?
4. **Reproduce locally** → `uv run python scripts/replay_run.py <id>` — can you trigger the bug?
5. **Fix prompt** → Edit supervisor/worker prompt, replay again to verify

**Playwright MCP Best Practices (Token-Optimized):**

The Playwright MCP plugin is configured with `--snapshot-mode none --console-level error` to prevent context blowups. This means:

| Tool | Use Case | Notes |
|------|----------|-------|
| `browser_navigate` | Go to a URL | Returns minimal info |
| `browser_take_screenshot` | Visual verification | Preferred over snapshots |
| `browser_run_code` | Multi-step interactions | **Best for complex ops** - one round trip |
| `browser_click` | Single clicks | Use sparingly on complex pages |
| `browser_snapshot` | Accessibility tree | **Disabled by default** - use screenshot instead |

**For complex pages (React Flow canvas, dashboards):**
- Prefer `browser_run_code` to batch operations
- Use `browser_take_screenshot` for verification instead of `browser_snapshot`
- Console warnings are suppressed; only errors show

**Config backup:** `docs/playwright-mcp-config.json` (restore to `~/.claude/plugins/cache/claude-plugins-official/playwright/*/` if plugin updates overwrite)

**Frontend Logging Modes:**

Control console log verbosity via URL parameter `?log=<level>`:

| Mode | URL | Behavior |
|------|-----|----------|
| **minimal** | `?log=minimal` | Errors/warnings only |
| **normal** | `?log=normal` (default) | Errors/warnings + key events (start, complete) |
| **verbose** | `?log=verbose` | Everything (EventBus events, SSE stream details, all debug logs) |
| **timeline** | `?log=timeline` | Timeline output only (minimal noise + performance timing) |

**Examples:**
- Chat with minimal noise: `http://localhost:30080/chat?log=minimal`
- Chat with timeline profiling: `http://localhost:30080/chat?log=timeline`
- Full debug mode: `http://localhost:30080/chat?log=verbose`

**Implementation:**
- Logger config: `apps/zerg/frontend-web/src/jarvis/core/logger.ts`
- Timeline logger: `apps/zerg/frontend-web/src/jarvis/lib/timeline-logger.ts`
- Phase 5 reduced console noise by 70%+ for normal/timeline modes (chat-observability-eval spec)

**If not running:** `make dev-bg` (background, non-blocking) — wait for containers to become healthy.

## Architecture (Unified SPA)

```
User → http://localhost:30080 (nginx)
  /            → Redirects to /dashboard (dev) or Landing page (prod)
  /dashboard   → Zerg dashboard (React SPA)
  /landing     → Landing page preview (dev only)
  /chat        → Jarvis chat UI (part of Zerg SPA)
  /api/*       → FastAPI backend (includes Jarvis BFF at /api/jarvis/*)
  /ws/*        → SSE/WS

Internal service ports (dev):
  Zerg backend       47300
  Zerg frontend      47200
```

**Note**: Jarvis chat UI is now integrated into the Zerg frontend SPA at `src/jarvis/`.
There is no separate jarvis-web service.

## Package Managers

- **JavaScript**: Bun only.
  - Monorepo lockfile: `bun.lock` (repo root) — run `bun install` from repo root.
- **Python**: uv only. Run `cd apps/zerg/backend && uv sync`.

Do not use npm, yarn, pip, or poetry.

## Key Commands

```bash
# Start everything (Docker + nginx + services)
make dev           # Full platform with nginx proxy (profile: dev)
                   # ⚠️ Interactive - tails logs until Ctrl+C
                   # ⚠️ Auto-rebuilds images (--build flag included)
make dev-bg        # Same but background (for CI/automation)

# Environment validation
make env-check     # Validate required env vars before starting

# Stop everything
make stop

# ⚠️  CRITICAL: NEVER run pytest/bunx/playwright directly - ALWAYS use make targets
# Direct commands miss env vars, wrong CWD, and cause false failures
make test          # Unit tests only (backend + frontend)
make test-e2e-core # Core E2E (critical path, no retries)
make test-e2e      # Playwright E2E (full suite minus core)
make test-all      # Unit + Playwright E2E (full minus core)
make test-chat-e2e # Chat page E2E (unified SPA)

# Targeted E2E testing
make test-e2e-single TEST=tests/unified-frontend.spec.ts # Or any Playwright args
make test-e2e-grep GREP="test name"
make test-e2e-ui   # Interactive mode

# Regenerate generated code
make generate-sdk  # OpenAPI types
make regen-ws      # WebSocket contract code
make regen-sse     # SSE event contract code

# Workflow LangGraph Debugging (see "LangGraph Debug Pipeline" section for full docs)
make debug-thread THREAD_ID=1      # View thread messages
make debug-validate THREAD_ID=1   # Check message integrity
cd apps/zerg/backend && uv run python scripts/debug_run_audit.py --run-id <id>  # LLM audit trail
cd apps/zerg/backend && uv run python scripts/replay_run.py <run_id>            # Replay with mocked tools

# Seeding (local dev data)
make seed-agents       # Seed Jarvis agents
make seed-credentials  # Seed personal tool credentials (Traccar, WHOOP, Obsidian)
make seed-marketing    # Seed marketing demo data (workflows, agents, chat)

# Marketing Screenshots (manifest-driven, URL-addressable)
make marketing-capture            # Capture 3 product screenshots (chat, dashboard, canvas)
make marketing-single NAME=chat-preview  # Capture one screenshot
make marketing-list               # List available screenshots
make marketing-validate           # Check outputs exist

# Video Generation (Audio-First Pipeline)
make video-audio       # Generate voiceover audio (RUN FIRST)
make video-record      # Record scenes (headless, requires audio + dev stack)
make video-process     # Post-process (combine, add audio, compress)
make video-all         # Full pipeline (audio -> record -> process)

# Validation (CI checks these)
make validate      # All validation

# Performance profiling (GPU, macOS only)
make perf-gpu           # Landing page GPU measurement
make perf-gpu-dashboard # Dashboard GPU measurement

# Trace Debugging & Jobs
make debug-trace TRACE=uuid  # Debug a trace end-to-end
make trace-coverage          # Trace coverage report
curl localhost:30080/api/jobs/    # List scheduled jobs (admin only)
curl -X POST localhost:30080/api/jobs/disk_health/run  # Trigger job
```

## Deploying + Coolify Debugging

**When a Coolify deployment fails, ALWAYS follow this sequence:**

### 1. Get Full Logs First (Required)
```bash
./scripts/get-coolify-logs.sh 1 > /tmp/coolify-error.log
```
- Coolify UI truncates; this gives complete output
- Searches in `/tmp/coolify-error.log` for actual errors
- Never guess or patch without seeing the full error

### 2. Verify Repo State (Most Common Issues)
```bash
# Check if files are actually in git
git ls-files <failing-path>

# Check if .gitignore is excluding source code
git check-ignore -v <failing-path>

# Compare local vs what Coolify clones
git show HEAD:<path>
```

**Common footgun:** `.gitignore` patterns like `data/` or `dist/` excluding source code.
- Use `/data/` for root-level only
- Use `!apps/<workspace>/dist/` (or similar) for exceptions

### 3. Test Build Locally (Same Context as Coolify)
```bash
docker build -f apps/zerg/frontend-web/Dockerfile --target production .
docker compose -f docker/docker-compose.prod.yml build
```

### 4. Categorize the Failure

| Error Pattern | Root Cause | Fix Location |
|--------------|------------|--------------|
| "Cannot find module" in TypeScript | Missing files in git | `.gitignore` + `git add` |
| "Variable not set" warnings | Env vars not at build time | Coolify UI → "Available at Build Time" |
| "Command failed with no error output (255)" | Coolify SSH/exec bug | Check `docker logs coolify` on clifford |
| "exit code 137" | OOM killed | Upgrade server or reduce parallelism |
| "Workspace not found" | Bun monorepo resolution | Check Docker COPY includes all workspaces |

**Never add Docker workarounds before completing steps 1-3.**

**Full guide:** `docs/COOLIFY_DEBUGGING.md`
**Architecture:** `docs/DEPLOYMENT.md`
**Known tech debt:** `docs/DEPLOYMENT_CLEANUP_PLAN.md`

## Docker Compose Profiles

Dev compose file (profiles live here): `docker/docker-compose.dev.yml`

Other compose files:
- Standalone production compose: `docker/docker-compose.prod.yml`

| Compose | Services | Use Case |
|--------|----------|----------|
| `docker/docker-compose.dev.yml` (profile `dev`) | postgres, backend, frontend, reverse-proxy, dev-runner | Local development via nginx at 30080 |
| `docker/docker-compose.prod.yml` | postgres, backend, frontend, reverse-proxy | Production deployment (typically via Coolify) |

```bash
# Direct compose usage (prefer make targets)
docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml --profile dev up
```

## Testing Infrastructure

**CRITICAL:** ALWAYS use Make targets for testing. Never run pytest/bun commands directly (they miss env vars, wrong CWD, no test isolation).

### Test Commands

| Command | What It Runs | Notes |
|---------|--------------|-------|
| `make test` | Unit tests (backend + frontend) | ~50 lines output |
| `make test MINIMAL=1` | Unit tests (compact mode) | **Recommended for Agents** (~10 lines) |
| `make test-e2e-core` | Core E2E tests (`tests/core/**`) | **No retries**; must pass 100% |
| `make test-e2e` | Playwright E2E (full suite minus core) | **Minimal output** (~10 lines pass, ~30 lines fail) |
| `make test-all` | Unit + Playwright E2E (full minus core) | **Does not include core** |
| `make test-e2e-single TEST=<spec>` | Single E2E test file | Most useful for iteration |
| `make test-e2e-verbose` | E2E with full output | For debugging |
| `make test-e2e-errors` | Show last run's errors | `cat test-results/errors.txt` |
| `make test-e2e-query Q='...'` | Query results JSON | e.g., `Q='.counts'` or `Q='.failed[]'` |

### E2E Projects (Playwright)

- **Core**: `tests/core/**`, retries=0, critical path only. Run with `make test-e2e-core`.
- **Full**: everything else (core ignored), retries allowed. Run with `make test-e2e`.
- **Full E2E coverage** = run **both** `make test-e2e-core` and `make test-e2e`.

### E2E Output: Progressive Disclosure

E2E tests use a **minimal reporter** designed for AI agents. Output is ~10 lines for pass, ~30 for fail:

```
Setting up 16 workers... done
Running 332 tests...

✓ E2E: 332 passed (8m 32s)
```

On failure, shows first 10 failed tests with guidance:
```
✗ E2E: 45 passed, 245 failed (10m 49s)

  tests/chat.spec.ts:45 "sends message"
  tests/workflow.spec.ts:123 "executes workflow"
  ... and 243 more

→ Errors: cat test-results/errors.txt
→ Query:  jq '.failed[]' test-results/summary.json
```

**Files generated** (in `apps/zerg/e2e/test-results/`):
- `summary.json` — Machine-readable results, query with `jq`
- `errors.txt` — Human-readable error details
- `full-output.log` — All suppressed console.logs (debugging)

**For verbose output**: `make test-e2e-verbose` or `VERBOSE=1 make test-e2e`

### E2E Test Isolation
- Per-worker Postgres schemas (not SQLite) for true isolation
- **8 Playwright workers** (reduced from 16) + **8 uvicorn workers** (optimal balance)
- Database reset with retry logic + stagger delays to prevent lock contention
- Playwright artifacts in `apps/zerg/e2e/test-results/` and `playwright-report/`
- Override: `PLAYWRIGHT_WORKERS=N bunx playwright test ...`

### Debugging E2E Failures
1. Check summary: `make test-e2e-query Q='.failed[] | .file'`
2. Read errors: `make test-e2e-errors`
3. Re-run single spec: `make test-e2e-single TEST=tests/chat.spec.ts`
4. Full verbose: `make test-e2e-verbose`
5. Interactive UI: `make test-e2e-ui`

## Conventions

### Backend (Python)
- Location: `apps/zerg/backend/zerg/`
- Tests: `apps/zerg/backend/tests/` — run with `make test`
- Debug scripts: `scripts/debug_*.py` — helpers for workflow/execution debugging
- Uses FastAPI with Pydantic models
- Routers in `routers/`, services in `services/`, models in `models/`

**Supervisor/Worker Architecture** (key files):
| File | Purpose |
|------|---------|
| `services/supervisor_react_engine.py` | Core ReAct loop (LangGraph-free) |
| `managers/agent_runner.py` | `run_thread()` and `run_continuation()` entry points |
| `services/supervisor_service.py` | Orchestrates runs, handles `AgentInterrupted` |
| `services/worker_runner.py` | Executes worker jobs, triggers supervisor resume |
| `services/worker_resume.py` | Resumes supervisor after worker completion |
| `tools/builtin/supervisor_tools.py` | `spawn_worker`, `get_worker_evidence`, `get_tool_output` tools |
| `services/evidence_compiler.py` | Compiles worker artifacts within byte budgets |
| `services/tool_output_store.py` | Stores large tool outputs by reference |
| `tools/tool_search.py` | Semantic search with OpenAI embeddings |
| `tools/catalog.py` | Tool catalog with CORE_TOOLS list |
| `tools/lazy_binder.py` | Lazy binding with allowlist support |
| `tools/builtin/tool_discovery.py` | `search_tools` and `list_tools` definitions |

**Flow**: User message → `SupervisorService` → `AgentRunner.run_thread()` → `supervisor_react_engine` → (if spawn_worker) `AgentInterrupted` → WAITING → worker runs → `worker_resume` → `AgentRunner.run_continuation()` → final response

**Lazy Tool Loading** (Claude Code pattern):

Agents have access to 65+ tools but only ~12 "core tools" are pre-bound to the LLM. Non-core tools are discovered via `search_tools()`:

```
User: "Where am I?"
       ↓
LLM → search_tools("location")
       ↓
Returns: [{name: "get_current_location", ...}]
       ↓
_maybe_rebind_after_tool_search() rebinds LLM
       ↓
LLM → get_current_location
       ↓
✅ Works!
```

Core tools (always loaded): `spawn_worker`, `list_workers`, `read_worker_result`, `get_worker_evidence`, `get_tool_output`, `grep_workers`, `get_worker_metadata`, `contact_user`, `search_tools`, `list_tools`, `web_search`, `http_request`

Embeddings cached to: `apps/zerg/backend/data/tool_embeddings.npz`

### Frontend (TypeScript/React)
- Zerg dashboard: `apps/zerg/frontend-web/`
- Uses React Query for data fetching
- Generated OpenAPI types in `src/generated/openapi-types.ts` — do not edit
- Run `bun run generate:api` to regenerate types from backend schema

### Frontend Styling Conventions (2025 Refactor)
- **Scope Styles**: CSS must be scoped to a container class (e.g., `.dashboard-container`, `.chat-view-container`) to prevent global collisions.
- **Layers**: Use `@layer` to control specificity (`tokens`, `base`, `legacy`, `components`, `pages`).
- **Tokens**: Use `--color-*` and `--space-*` variables from `src/styles/tokens.css`.
- **No Global Classes**: Avoid generic class names like `.back-button` or `.empty-state` at the root level.
- **Tools**: Tokens are edited directly (no build step); `bun run build` catches invalid CSS nesting in the Vite build pipeline.

### Jarvis (TypeScript - integrated into Zerg frontend)
- Chat UI: `apps/zerg/frontend-web/src/jarvis/` (integrated into Zerg SPA)
- Entry point: `src/pages/JarvisChatPage.tsx` (renders at `/chat`)
- BFF endpoints: Served by zerg-backend at `/api/jarvis/*`

The Jarvis code lives in `src/jarvis/` with this structure:
- `app/` — React components, hooks
- `contexts/` — React contexts
- `lib/` — Utilities (voice, session, audio controllers)
- `core/` — Core utilities (logger, API client)
- `data/` — IndexedDB storage
- `styles/` — Jarvis-specific CSS

**Reasoning Effort Feature:**
- Chat UI includes reasoning effort selector (none/low/medium/high)
- Controls OpenAI reasoning intensity for gpt-5.2, o1, o3 models
- Displays reasoning tokens badge in UI when >0
- Implementation uses raw AsyncOpenAI (now upgraded to LangChain `usage_metadata` v1.2.5+)

## Generated Code — Do Not Edit

- `apps/zerg/frontend-web/src/generated/` — OpenAPI types
- `apps/zerg/backend/zerg/generated/ws_messages.py` — WebSocket messages (generated)
- `apps/zerg/frontend-web/src/generated/ws-messages.ts` — WebSocket messages (generated)
- `apps/zerg/backend/zerg/generated/sse_events.py` — SSE events (generated)
- `apps/zerg/frontend-web/src/generated/sse-events.ts` — SSE events (generated)
- `apps/zerg/backend/zerg/tools/generated/tool_definitions.py` — Tool registry types (from `schemas/tools.yml`)

Run `make regen-ws` if WebSocket schema changes. Run `make regen-sse` if SSE schema changes. Run `make generate-sdk` if API changes.

## Pre-commit (AI Agent Safety)

Pre-commit hooks run automatically on every commit. Agents should be aware of what will block commits.

### What Runs

| Hook | Behavior | Blocks? |
|------|----------|---------|
| ruff lint | Auto-fixes Python issues | No (auto-fix) |
| ruff format | Auto-fixes formatting | No (auto-fix) |
| TypeScript types | `tsc --noEmit` | **Yes** |
| ESLint | Frontend lint | Warns only (100 max) |
| WS drift | Contract sync check | **Yes** |
| SSE drift | Contract sync check | **Yes** |
| AsyncAPI | Schema validation | **Yes** |

### Common Failures & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `bare except` (E722) | Using `except:` | Use `except Exception:` |
| Unused variable (F841) | Assigned but never used | Prefix with `_` or remove |
| Line too long (E501) | Ignored in this repo | N/A (disabled) |
| TypeScript type error | Actual type mismatch | Fix the types |
| WS drift | Schema out of sync | Run `make regen-ws` |
| SSE drift | Schema out of sync | Run `make regen-sse` |

### Ruff Configuration

Located in `apps/zerg/backend/pyproject.toml`:
- **Line length**: 140 (generous for descriptive code)
- **Ignored**: E501 (line length) - prompt templates are intentionally long
- **Scope**: Only `apps/zerg/backend/zerg/` (excludes alembic, scripts, tests from format enforcement)
- **Generated files excluded**: `apps/zerg/backend/zerg/generated/`

### Testing Pre-commit Locally

```bash
# Run all hooks on all files
pre-commit run --all-files

# Run specific hook
pre-commit run ruff --all-files

# Skip hooks (escape hatch for WIP commits)
git commit --no-verify -m "WIP: skip hooks"
```

### Generated Files — Never Edit

These are excluded from linting and regenerated from schemas:
- `apps/zerg/backend/zerg/generated/ws_messages.py`
- `apps/zerg/frontend-web/src/generated/ws-messages.ts`
- `apps/zerg/backend/zerg/generated/sse_events.py`
- `apps/zerg/frontend-web/src/generated/sse-events.ts`
- `apps/zerg/frontend-web/src/generated/openapi-types.ts`

If you edit these, your changes will be overwritten by `make regen-ws`, `make regen-sse`, or `make generate-sdk`.

## Gotchas

1. **Auth in dev vs prod**: unified compose sets `AUTH_DISABLED=1` backend and `VITE_AUTH_ENABLED=false` frontend for local. Re-enable auth in prod.

2. **CORS**: In prod, `ALLOWED_CORS_ORIGINS` takes priority if set (regardless of `AUTH_DISABLED`). In dev with no explicit origins, defaults to localhost only.

3. **WebSocket code must stay in sync**: run `make regen-ws` after touching `schemas/ws-protocol.schema.json`.

4. **Ports**: external entry 30080; service ports (47200/47300) are internal. Avoid binding conflicts if running pieces standalone.

5. **Python virtual env**: backend uses `apps/zerg/backend/.venv/`; use `uv run` or activate venv.

6. **E2E Test Isolation**: E2E tests run in a separate Docker project (`zerg-e2e`). If tests fail due to "database connection" issues, ensure no other containers are conflicting on the internal network and try `make test-e2e-reset`.

7. **Auth in Tests**: When `AUTH_DISABLED=1` (default for tests), the `/api/auth/verify` endpoint always returns 204. This allows the frontend to proceed without a real login.

8. **E2E runs on an insecure origin**: Playwright hits `http://reverse-proxy` in Docker (service name), which is **not** a secure context — `crypto.randomUUID()` may be unavailable. Use `src/jarvis/lib/uuid.ts` instead of calling `crypto.randomUUID()` directly.

9. **Prefer Make targets for E2E**: `make` loads/export vars from `.env`. Running raw `docker compose` commands directly can fail if environment variables aren't exported in your shell.

10. **Deterministic UI tests**: For progress-indicator/UI behavior tests, prefer emitting events via `window.__jarvis.eventBus` (DEV only) instead of relying on the LLM to spawn workers/tools.

11. **WebRTC tests in Docker**: When `SKIP_WEBRTC_TESTS=true`, WebRTC-based tests should be *describe-level* skipped so `beforeEach` doesn't fail in the Docker E2E environment.

12. **AGENTS vs CLAUDE**: `CLAUDE.md` is a symlink for tool compatibility. Treat `AGENTS.md` as canonical and only edit `AGENTS.md`.

13. **Generated tool types path**: `scripts/generate_tool_types.py` writes to `apps/zerg/backend/zerg/tools/generated/` relative to repo root (safe from any CWD). If you ever see a duplicate tree like `apps/zerg/backend/apps/...`, it's a stray artifact and can be deleted.

14. **CSS Collisions**: Global class names (e.g., `.back-button`, `.empty-state`) can leak across pages if not scoped. Always nest page-specific styles under a root container class (e.g., `.dashboard-container { ... }`).

15. **`make dev` is interactive**: It starts services, then tails logs forever (never exits). Run it in your terminal, not via automated tools expecting completion. To rebuild after Dockerfile changes, just `make stop && make dev` — the `--build` flag is already included.

16. **Never use raw `docker compose` for dev**: Always use Make targets (`make dev`, `make stop`, `make logs`). Raw `docker compose` commands use wrong project names, miss env vars from `.env`, and create containers on isolated networks that can't communicate with the rest of the stack. If you must, use the pattern from Makefile line 17: `docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml ...`

17. **⚠️ NEVER run tests directly**: Always use `make test` / `make test-e2e` instead of direct `pytest` / `bunx playwright` / `bun test` commands. Running tests directly WILL cause false failures (missing env vars, wrong working directory, no test isolation). Make targets ensure:
   - Correct working directory
   - Environment variables loaded from `.env`
   - Parallel execution enabled (pytest uses `-n auto`, Playwright uses 4 workers)
   - Proper test isolation (separate DB per worker)
   - Comparable results locally and in CI

18. **UI effects toggle**: Dashboard/app pages use static backgrounds + glass panels by default. Use `?effects=off` URL param or `VITE_UI_EFFECTS=off` to disable ambient visuals entirely. Landing page has its own animated effects (hero, particles) controlled separately via `?fx=none`.

19. **Marketing mode**: Use `?marketing=true` URL param to enable vivid styling for screenshots. Works with URL-addressable pages: `/canvas?workflow=health&marketing=true`, `/chat?thread=marketing&marketing=true`, `/dashboard?marketing=true`. Pages emit `data-ready="true"` on body when loaded for automation.

20. **Feature flags are import-time**: Environment variables read via `os.getenv()` at module level are evaluated once at import. Tests must patch the module variable directly, not `os.environ`.

21. **Exception classes must be single-sourced**: Custom exceptions like `AgentInterrupted` must be defined in one place and imported everywhere. Defining a local class with the same name causes `except` blocks to miss it silently.

22. **Pre-commit + unstaged files**: If pre-commit hooks auto-fix files while you have unstaged changes, you get conflicts. Stage everything first (`git add -A`) before committing.

23. **Split deployment API URLs**: Frontend uses `window.API_BASE_URL` from `/config.js` for API origin. Key files: `src/jarvis/lib/config.ts` (`toAbsoluteUrl()`), `public/funnel.js`.

24. **Coolify domain port syntax**: Use `https://api.example.com:8000` to route to container port 8000. The `:8000` is for caddy-docker-proxy, not public.

25. **Coolify env var changes need redeploy**: Restart does NOT pick up new env vars. Must redeploy via git push or Coolify UI.

## Environment Setup

Copy `.env.example` to `.env` and fill in:
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` — Required for Docker
- `OPENAI_API_KEY` — Required for Jarvis and LLM features

Run `make env-check` to validate your environment before starting.

Dev auth defaults (`AUTH_DISABLED=1`, `VITE_AUTH_ENABLED=false`) are set in compose. For production, set `AUTH_DISABLED=0` and configure Google OAuth credentials.

### Supervisor Tool Output Storage (Optional)

These env vars control progressive disclosure for large tool outputs:

| Env Var | Default | Purpose |
|---------|---------|---------|
| `SUPERVISOR_TOOL_OUTPUT_MAX_CHARS` | 8000 | Tool outputs larger than this are stored by reference |
| `SUPERVISOR_TOOL_OUTPUT_PREVIEW_CHARS` | 1200 | Preview size shown when output is stored |

When tool outputs exceed `SUPERVISOR_TOOL_OUTPUT_MAX_CHARS`, they're stored on disk and replaced with a marker like `[TOOL_OUTPUT:artifact_id=...,tool=...,bytes=...]`. The LLM can fetch full content via `get_tool_output(artifact_id, max_bytes)` (default 32KB truncation, pass `max_bytes=0` for full content).

## Auto-Seeding (User Context & Credentials)

On startup, the backend automatically seeds user context and credentials from local config files. This is **idempotent** (safe to run repeatedly).

### Config File Locations (checked in order)
| File | Dev Path (relative to backend) | Prod Path |
|------|----------|-----------|
| User context | `scripts/user_context.local.json` | `~/.config/zerg/user_context.json` |
| Credentials | `scripts/personal_credentials.local.json` | `~/.config/zerg/personal_credentials.json` |

### Setup (First Time)
```bash
# Copy examples and edit with your details
cd apps/zerg/backend
cp scripts/user_context.example.json \
   scripts/user_context.local.json

cp scripts/personal_credentials.example.json \
   scripts/personal_credentials.local.json

# Edit the files with your servers, integrations, and credentials
# Then restart the backend (or it will seed on next `make dev`)
```

The `.local.json` files are git-ignored. User context includes:
- **servers**: Names, IPs, purposes (injected into supervisor/worker prompts)
- **integrations**: Health trackers, notes apps, backup systems
- **custom_instructions**: Personal preferences for AI responses

## Personal Tools & Credentials (v2.1 Phase 4)

Jarvis supports personal integrations for location, health, and notes. These are **Supervisor-owned tools** that use per-user encrypted credentials.

### Available Personal Tools

| Tool | Integration | Purpose |
|------|-------------|---------|
| `get_current_location` | Traccar | GPS location from tracking server |
| `get_whoop_data` | WHOOP | Health metrics (recovery, HRV, sleep, strain) |
| `search_notes` | Obsidian | Search personal notes via Runner |

### Local Development Setup

**1. Create credentials file:**
```bash
cp apps/zerg/backend/scripts/personal_credentials.example.json \
   apps/zerg/backend/scripts/personal_credentials.local.json
```

**2. Fill in your credentials** (file is git-ignored):
```json
{
  "traccar": {
    "url": "http://5.161.97.53:5055",
    "username": "admin",
    "password": "your-password",
    "device_id": "1"
  },
  "whoop": {
    "client_id": "your-oauth-app-client-id",
    "client_secret": "your-oauth-app-client-secret",
    "access_token": "from-oauth-flow",
    "refresh_token": "from-oauth-flow"
  },
  "obsidian": {
    "vault_path": "~/git/obsidian_vault",
    "runner_name": "laptop"
  }
}
```

**3. Start dev** (auto-seeds on startup):
```bash
make dev
```

Or seed manually:
```bash
make seed-credentials
```

### Production Setup

On production server, seed credentials for your user:
```bash
# Via SSH
python scripts/seed_personal_credentials.py --email your@email.com

# Or create ~/.config/zerg/personal_credentials.json
# Then run: python scripts/seed_personal_credentials.py
```

### Avoid compats / legacy / shims
- this app has no users, never launched, and only has one dev. Keep the code simple. If you see anything that is slightly too complex, recommend a complete refactor to start fresh.

### Credential Security

- ✅ **Local config** (`*.local.json`) is git-ignored
- ✅ **Database storage** is Fernet-encrypted (AES-GCM)
- ✅ **No secrets in code** - all credentials come from config files
- ✅ **Per-user** - stored in `account_connector_credentials` table
- ⚠️ **Change defaults** - Traccar default password is `admin`

### Seeding Commands

```bash
make seed-agents       # Seed Jarvis agents (Morning Digest, Health Watch, etc)
make seed-credentials  # Seed personal tool credentials (Traccar, WHOOP, Obsidian)
```

Both are **idempotent** (safe to run multiple times). Use `--force` to overwrite:
```bash
make seed-credentials ARGS="--force"
```

### For More Details

- **Traccar Setup**: See `TRACCAR_QUICKSTART.md` and `apps/zerg/backend/scripts/TRACCAR_SETUP.md`
- **Test Connection**: `uv run scripts/test_traccar.py`
- **WHOOP OAuth**: Register app at https://developer.whoop.com
- **Obsidian**: Requires a Runner on the machine with your vault

## Production Deployment

**Product Name**: Swarmlet (rebranded from Zerg)

| Service | URL | Server |
|---------|-----|--------|
| Frontend (Dashboard + Landing) | https://swarmlet.com | zerg (Hetzner VPS) |
| Backend API | https://api.swarmlet.com | zerg (Hetzner VPS) |
| Jarvis Chat | https://swarmlet.com/chat | zerg (Hetzner VPS) |

### Coolify Apps
| App | UUID | Coolify ID |
|-----|------|------------|
| zerg-api | `a08o0s4k0s4kwswswg0008o0` | 48 |
| zerg-web | `awkk0k084ssgw00sg44k4so4` | 51 |

**Deploy branch**: `onboard-sauron` (not `main` yet). Push triggers auto-deploy.

### Infrastructure Architecture
```
clifford (Coolify master)      zerg (build/runtime)
├─ Coolify PHP + DB            ├─ Docker containers
├─ API at localhost:8000       ├─ Build artifacts
└─ SSH → zerg for deploys      └─ App runtime
```

- **Coolify runs on clifford** - orchestrates deploys, stores logs in `coolify-db`
- **Containers run on zerg** - target server for Swarmlet
- **Auto-deploy**: Push to `main` → Coolify detects → SSH to zerg → rebuild

### Deployment Commands (Agent-Friendly)
```bash
git push origin onboard-sauron          # Triggers auto-deploy
./scripts/get-coolify-logs.sh 1         # Check status
./scripts/smoke-prod.sh                 # Validate endpoints (16 checks)
./scripts/smoke-prod.sh --quick         # Health check only
```

**Smoke test covers**: health, CORS, auth (401s), Jarvis API, frontend pages, runtime config, Caddy errors.
**Limitation**: Can't test authenticated chat flow (no service account yet).

### Server Access
- **ssh zerg** - Container logs, runtime state
- **ssh clifford** - Coolify DB, deployment logs, API

See `docs/COOLIFY_DEBUGGING.md` for full debugging workflow.

### Vite Environment Variables

**CRITICAL:** `VITE_*` variables are BUILD-TIME, not runtime:
- Must be present during `bun run build` (baked into JS bundle)
- Changing env vars in Coolify requires REBUILD, not just restart
- Use Coolify API `/deploy` endpoint, not `/restart` endpoint

Example:
```bash
# After adding VITE_UMAMI_WEBSITE_ID to Coolify:
# WRONG: /restart (uses old build)
# RIGHT: /deploy?uuid=mosksc0ogk0cssokckw0c8sc&force=true (rebuilds with new vars)
```

## Documentation

| Doc | Purpose |
|-----|---------|
| `docs/DEVELOPMENT.md` | Local dev setup, troubleshooting |
| `docs/DEPLOYMENT.md` | Production deployment |
| `docs/specs/durable-runs-v2.2.md` | **Main architecture spec** (v2.2) |
| `docs/specs/jarvis-supervisor-unification-v2.1.md` | Architecture overview (v2.1) |
| `docs/archive/super-siri-architecture.md` | Historical architecture (v2.0) |
| `docs/work/` | Active PRDs (temporary, delete when done) |
