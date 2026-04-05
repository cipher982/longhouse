# Longhouse (codename: Zerg)

AI agent orchestration platform. Oikos = voice/text UI. Longhouse = product name.

**Owner**: david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## Philosophy

- **Read VISION.md first** — It's the guiding light. Understand the strategic direction before diving into implementation.
- **SQLite-only core** — SQLite pivot is complete. Don't invest in Postgres-specific infrastructure. See VISION.md.
- **Progressive disclosure** — AGENTS.md should point to deeper docs/runbooks so agents know what they don't know.
- **Design for cold restarts** — Assume future agents will re-understand the system from code and local docs only, with no durable human memory carrying context forward.
- **Keep behavior explicit** — Prefer explicit intent, explicit modes, and capability objects over inference from scattered raw fields.
- **No hidden fallbacks** — If behavior changes materially, make it an explicit product/API choice instead of a silent backend switch.
- **One source of truth per capability** — Do not make the frontend, Oikos, and backend separately re-derive the same behavior from different heuristics.
- **Prefer obvious seams over clever reuse** — When two flows differ in behavior or transport, split the route/service/contract instead of hiding both behind one overloaded surface.
- Always commit changes as you go (no lingering uncommitted work). In swarm mode, **lead commits after each teammate's verified work** — don't batch everything into one mega-commit at the end.
- **Execute autonomously** — When given a plan or task, implement → test → commit → deploy → verify without pausing for approval at each step. Only stop to ask if something is ambiguous or blocked. Don't narrate your progress — deliver results.

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized commiss.

**Current Direction:** SQLite-only for OSS (complete). Postgres is control-plane only. See VISION.md § "No Postgres in core."

## Communication

- Treat David as the PM and speak like the lead dev. Be direct, decisive, and product-aware.
- Default to **outcome → implication → next move**. Skip implementation play-by-play unless asked.
- Do **not** dump file paths, command transcripts, or atomic edit inventories in normal replies. Include them only when explicitly requested or when they are necessary to debug a blocker.
- Do **not** enumerate every test run by default. Mention tests when they failed, were skipped, materially change confidence, or the user asked for validation detail.
- Prefer short prose over changelog bullets. Use bullets only for real issues, choices, or risks.
- After a big round of tool calls, first consolidate privately into a short internal summary, then answer with the high-signal conclusion. The user should see the decision, not the scratchpad.

## Task Tracking

- **Do not create repo-local task files, backlogs, or TODO trackers** in this repo.
- If work needs local context, write a normal doc/spec/note with a real subject-driven name.
- Delete temporary planning artifacts when they stop being useful. Git history is the record.

## Quick Reference

**Do not Assume `make dev` is running** during coding sessions.

| URL | What |
|-----|------|
| localhost:47200 | Frontend (dev) |
| localhost:47300 | Backend API (dev) |
| localhost:47300/api/health | Health check |

## Essential Commands

```bash
make dev              # Start SQLite dev (backend + frontend, no Docker)
make stop             # Stop dev services
make test             # Python backend tests (tests_lite/, ~10s)
make test-e2e         # Playwright core + a11y (~2min)
make test-ci          # Simulate push/PR CI locally (~3min) — run before pushing
make test-full        # All tiers: backend + engine + frontend + e2e + visual (~8min)
make dev-docker       # Legacy: Docker + Postgres (CI/testing only)
```

## Testing

### Tier Guide

Run the tier that matches your change. Don't over-test.

| Target | What runs | Changed what? | Time |
|--------|-----------|---------------|------|
| `make test` | Python backend (`tests_lite/`, 174 files, SQLite in-memory) | `server/zerg/`, `server/tests_lite/` | ~10s |
| `make test-engine` | Rust engine unit + golden + adversarial parser tests | `engine/` | ~20s |
| `make test-frontend` | Frontend unit tests + TypeScript type-check | `web/` | ~15s |
| `make test-control-plane` | Control plane unit tests | `control-plane/` | ~10s |
| `make test-runner` | Runner unit tests (Bun) | `runner/` | ~5s |
| `make test-e2e` | Playwright core + a11y — **must pass 100%** | UI changes, before PR | ~2min |
| `make test-ci` | Simulate push/PR CI (validate + all unit tiers) | **Before pushing** | ~3min |
| `make test-full` | All tiers + shipper E2E + visual baselines | Pre-deploy, full verify | ~8min |
| `make qa-live` | Smoke against hosted instance | **After deploy** | ~60s |

### What `make test-ci` runs

Mirrors exactly what `contract-first-ci.yml` runs on push/PR:
1. `make validate` — WS/SSE/Makefile contracts + lint patterns
2. `make test` — Python backend
3. `make test-control-plane` — control plane unit
4. `make test-frontend` — frontend unit + TypeScript types
5. `make test-runner` — runner unit (Bun)
6. `make test-engine` — Rust engine unit + golden + adversarial
7. `make test-shipper-e2e` — full shipping pipeline (builds release binary)

### Subsystem targets for targeted runs

```bash
make test-engine           # Rust engine — only when changing engine/
make test-frontend         # Frontend unit + type-check — only when changing web/
make test-control-plane    # Control plane — only when changing control-plane/
make test-runner           # Runner — only when changing runner/
make test-shipper-premerge # Engine + shipper E2E — before merging engine changes
```

### Visual comparison

```bash
make qa-visual-compare       # Hybrid pixelmatch + Gemini Flash triage (catches color/layout regressions)
make qa-visual-compare-fast  # Pixelmatch only (no LLM), faster
```

`scripts/visual-compare.ts`: Two modes: (1) `--baseline-dir`/`--current-dir` for CI, (2) single-pair manual debugging. Shared page definitions in `e2e/tests/helpers/page-list.ts`. Requires `GOOGLE_API_KEY` for LLM triage; falls back to hard threshold without it.

### `tests_lite/` convention

No shared conftest. Each test file creates its own DB via `_make_db(tmp_path)` using `make_engine("sqlite:///...")` + `AgentsBase.metadata.create_all()`. HTTP-level tests use `TestClient` with `dependency_overrides` on `api_app` (not `app`). See `test_job_preflight.py` for a clean example of both ORM-only and HTTP-level patterns.

## Architecture

**Stack:** FastAPI + SQLAlchemy + SQLite (dev/OSS) or Postgres (prod control-plane). React frontend. See `VISION.md` for details.

**API sub-app:** All `/api/*` routes live on `api_app` (mounted on `app` at `/api`). New routers go on `api_app`, not `app`. Test `dependency_overrides` must target `api_app`. Only `/metrics`, `/config.js`, static mounts, and the SPA catch-all stay on `app`.

**LLM model config:** `config/models.json` is the single source of truth — models, tiers, use cases, routing profiles, and embedding config. Set `MODELS_PROFILE` env var to select per-instance overrides (default `oss`). Each model can declare `apiKeyEnvVar` for its required API key. See `models_config.py:get_llm_client_for_use_case()` for LLM factory, `get_embedding_config()` for embeddings (gracefully returns None if no API key).

**MCP server:** `zerg/mcp_server/server.py:create_server()` exposes Longhouse continuity tools to managed CLI workspaces. Tools: session search, session detail/event drill-down, recall, insights, and `notify_oikos`. `longhouse connect --install` no longer registers this globally in normal local Claude/Codex configs; workspace provisioning injects it where needed. New tools go inside `create_server()`. API client at `mcp_server/api_client.py` (get/post methods).

## Features (What Exists)

| Feature | API | Frontend | MCP | Notes |
|---------|-----|----------|-----|-------|
| Session timeline | `GET /api/agents/sessions` | SessionsPage | `search_sessions` | FTS search, filters by project/provider/days |
| Session detail | `GET /api/agents/sessions/:id` | SessionDetailPage | — | Full event transcript |
| **Semantic search** | `GET /api/agents/sessions/semantic?query=` | SessionsPage (AI search / hybrid mode) | `search_sessions` | Requires embeddings (OpenAI text-embedding-3-small, 256d) |
| **Recall** | `GET /api/agents/recall?query=` | SessionsPage (Recall panel) | `recall` | Turn-level knowledge retrieval with context windows |
| Embeddings | `POST /api/agents/backfill-embeddings` | — | — | Auto-generated on ingest; backfill for existing sessions |
| Oikos (chat) | WebSocket `/ws/oikos` | OikosPage | — | Voice/text AI assistant with tool use |
| Insights | `GET /api/insights`, `POST /api/insights/:id/archive`, `POST /api/insights/:id/unarchive` (browser); `GET /api/agents/insights` + `POST /api/insights` (machine) | InsightsPage | `log_insight` | Curated continuity memory; browser users can archive/restore rows and machine reads stay separate |
| Reflection / briefings | `POST /api/agents/reflect`, `GET /api/agents/reflections`, `GET /api/timeline/briefing` | BriefingsPage | — | Briefings are the user-facing continuity surface; reflection remains optional admin tooling and the cron job is paused by default |
| Jobs/Scheduler | `GET /api/jobs` | JobsPage | — | Cron jobs with enable/disable, secrets |
| Runner daemon | WebSocket from runner binary | — | — | Remote command execution on user infra |
| **Session presence** | `POST /api/agents/presence` + `GET /api/agents/sessions/active` | ForumPage | — | Real-time state (thinking/running/idle) via Claude/Codex hooks; managed-local Codex launch also seeds an initial idle row because bare Codex TUI emits no hook until the first submitted prompt; `session_presence` table, stale after 10min |
| **Forum / live view** | `GET /api/agents/sessions/active` | ForumPage | — | Active session map; active rows glow green, canvas entities pulse; polls at 2s |

## Conventions

- **Backend**: FastAPI + Pydantic, `server/zerg/`
- **Frontend**: React + React Query, `web/`
- **Frontend effects**: `useEffect` is for external synchronization only, never for syncing React state to React state or routine fetch bookkeeping.
- **Package managers**: Bun (JS), uv (Python) — never npm/pip
- **Generated code** (don't edit): `server/zerg/generated/`, `server/zerg/tools/generated/`, `web/src/generated/`
- **Tests**: Always use `make test*` targets, never direct pytest/playwright. New backend tests go in `tests_lite/`.
- **Tool contracts**: Edit `schemas/tools.yml`, then run `scripts/generate_tool_types.py` — never edit generated files directly
- **Oikos tools**: Registration is centralized in `oikos_tools.py`; `OIKOS_TOOL_NAMES` + `OIKOS_UTILITY_TOOLS` define the tool subset; `get_oikos_allowed_tools()` is the single source of truth
- **Git policy**: Work only on `main`, no worktrees; confirm `git status -sb` before changes; no stashing unless explicitly requested
- **DB writes in endpoints**: High-frequency endpoints (presence, heartbeat, ingest, runtime, runner WebSocket) MUST route writes through `WriteSerializer` (`services/write_serializer.py`). Low-frequency interactive endpoints (admin, settings, auth) may use direct `db.commit()`. When in doubt, use the serializer — the pre-commit hook will warn on new `db.commit()` calls in routers.
- **Concurrent edits**: Dirty trees are normal; work around existing diffs and avoid overlapping lines. Only pause to coordinate if you must edit an already-modified file.

## UI Components

Import from `../components/ui`. **Check here before building custom UI.**

| Component | Variants/Sizes | Usage |
|-----------|---------------|-------|
| Button | primary, secondary, tertiary, ghost, danger, success / sm, md, lg | `<Button variant="primary" size="md">` |
| Badge | neutral, success, warning, error | `<Badge variant="success">Active</Badge>` |
| Card | glass (default), default | `<Card><Card.Header>...</Card.Header><Card.Body>...</Card.Body></Card>` |
| EmptyState | default, error | `<EmptyState title="..." description="..." action={<Button>}/>` |
| Input | - | `<Input placeholder="..." />` |
| Spinner | sm, md, lg | `<Spinner size="lg" />` |
| Table | - | `<Table><Table.Header>...<Table.Body><Table.Row>...` |
| SectionHeader | - | `<SectionHeader title="..." description="..." actions={...} />` |
| PageShell | narrow, normal, wide, full | `<PageShell size="wide">...</PageShell>` |
| IconButton | - | `<IconButton aria-label="..."><Icon /></IconButton>` |

## Gotchas

1. **`make dev` is interactive** — Use `run_in_background=true` in Bash tool.
2. **Dev DB location + overrides** — SQLite dev DB is `~/.longhouse/dev.db` (check `/api/health` → `checks.database.url`). `DATABASE_URL` in `.env` overrides this and can silently point you elsewhere.
3. **AGENTS.md is canonical** — `CLAUDE.md` is a symlink, edit AGENTS.md only.
4. **Auth disabled in dev** — `AUTH_DISABLED=1` set by dev.sh.
5. **Coolify env var changes need redeploy** — restart doesn't pick up new vars.
6. **Task tracking:** See § Task Tracking above. Delete task/spec files when work ships.
7. **Backend README required** — pyproject.toml needs it; don't delete `server/README.md`.
8. **Stripe key rotation** — Use `~/git/me/mytech/scripts/update-stripe-key.sh sk_live_...`. It validates against Stripe before touching anything, then updates Coolify and redeploys.
9. **Coolify container names are random hashes** — Don't `docker ps --filter name=X` to find Coolify apps. Use `docker ps` and check labels: `coolify.serviceName` has the logical name (e.g., `longhouse-control-plane`). Or use `coolify app status <name>`.
10. **Pre-commit hooks** — ruff, ruff-format, vulture (dead code), TS type-check, frontend lint. Vulture whitelist: `server/vulture-whitelist.py`. New `TYPE_CHECKING` imports need whitelisting or vulture will block commit.
11. **Runtime deploys and control-plane deploys are separate lanes** — runtime-path pushes build the GHCR runtime image, then deploy the public demo runtime from that image and reprovision the hosted canary. Superseded branch runtime-image runs now cancel instead of burning time publishing stale branch commits. `control-plane/**` pushes deploy only the control plane. The control plane is still its own Coolify build.
12. **Do not commit valid secret-shaped dummy values** — GitGuardian will flag Fernet-format placeholders even when they are fake. For dev/test/bootstrap paths, generate ephemeral secrets at runtime instead of checking in realistic-looking literals.
13. **Stage only your changes** — Dirty trees are normal (other agents' WIP). When committing, `git add` specific files — never `git add -A`. If new code depends on unstaged changes from other files, include those files or the deploy will break.
14. **Do NOT add `extra_body={"metadata": ...}` to LLM calls** — Instance calls go directly to providers (Groq, OpenAI, z.ai), NOT through the LiteLLM proxy. Groq rejects `metadata` with 400. The proxy at `llm.drose.io` is personal-dev only, not used by user instances. New models must be added to `~/git/litellm-proxy/config.yaml` AND `hooks/model_hints.py` (for personal-dev proxy use).
15. **Secret manager choice is operator-side, not repo-side** — Keep Longhouse app code, scripts, READMEs, and deployment contracts provider-agnostic (`ENV_VAR` inputs, standard secret injection). Personal tools may use Infisical/Keychain/1Password externally, but do not make the OSS repo depend on one of them by default.
16. **DB provider config overrides env vars** — `get_llm_client_with_db_fallback()` checks `LlmProviderConfig` table first. Stale rows with wrong keys cause silent 401s. Check DB before debugging API auth failures.
17. **Zerg host backups/cleanup are unified under `zerg-ops`** — source of truth is `scripts/zerg-ops.sh`, deployed to `/usr/local/bin/zerg-ops`. It is intentionally code-configured (no `/etc/zerg-ops.env` contract). For scoped checks use CLI `--instance`, and offsite uses SSH alias `longhouse-offsite` configured on the host.
18. **Demo seed/reset identity + repair path** — demo rows are identified by `provider_session_id LIKE 'demo-%'` (not `device_id`). In dev (`AUTH_DISABLED=1`), run `POST /api/agents/demo?replace=true` to wipe/reseed stale rows. Response includes `sessions_failed` and `sessions_deleted`.
19. **`~/.claude/longhouse-machine-name` is read at engine startup** — changing the file does nothing for the already-running `com.longhouse.shipper` daemon. Restart/reinstall the LaunchAgent (and re-sign via `make install-engine` first if launchd reports a codesigning exit) or new sessions may keep the stale label.
20. **Hosted Gmail now depends on control-plane Pub/Sub config** — hosted Gmail connect runs through `control.longhouse.ai`, and the control plane now creates or repairs per-instance `gmail-push-<subdomain>` subscriptions before handoff. The required operator-side config is `CONTROL_PLANE_INSTANCE_GMAIL_PUBSUB_TOPIC`, `CONTROL_PLANE_INSTANCE_PUBSUB_SA_EMAIL`, and working GCP credentials on the control plane (ADC or `CONTROL_PLANE_GOOGLE_CLOUD_CREDENTIALS_JSON`). Existing hosted instances may still need reprovisioning once to pick up the injected runtime env.
21. **Do not write handoff notes into this repo** — session handoffs belong in `~/git/obsidian_vault/AI-Sessions/`, not under `docs/` or other repo-local paths.
22. **Hosted Longhouse data path is an explicit exception on `zerg`** — do not assume the generic VPS `/var/lib/docker/data/...` layout here. Hosted tenant data lives at `/var/app-data/longhouse/<subdomain>` on the host, mounts to `/data` in the tenant container, and the live DB is `/data/longhouse.db`. Use `scripts/hosted-loop-debug.sh <subdomain>` before improvising nested `ssh` + SQLite commands.
23. **Claude Code local MCP config lives in `~/.claude.json`, not `.claude/settings.json`** — private per-project MCP servers live under `projects[canonical-workspace-path].mcpServers`; on macOS, canonicalize workspace paths (`/private/tmp/...` vs `/tmp/...`) before wiring native Claude channels.
24. **Background waits should not turn into polling loops** — if a tool, workflow, or wrapper already gives completion notification or a blocking wait primitive, use that and do other useful work. Do not burn tool calls on `pgrep`, repeated health curls, or status checks unless there is no completion signal available.

## Pushing Changes

**There are three deploy surfaces on `zerg`:**

| What | URL | How |
|------|-----|-----|
| Public demo runtime | https://longhouse.ai | Coolify app `longhouse-demo` |
| Control plane | https://control.longhouse.ai | Coolify app `longhouse-control-plane` |
| Hosted tenant runtimes | https://david010.longhouse.ai | Control plane reprovision (pulls latest GHCR runtime image) |

`longhouse.ai` is not a static landing page. It is a Longhouse demo runtime in demo mode with its own API and SQLite DB.

**Hosted tenant runtimes** are provisioned and managed by the control plane. There is no docker-compose path — all instances are containers created by the provisioner. To update an instance to the latest runtime image, reprovision it via the admin API.

**Dev instances:**
- **david010.longhouse.ai** — primary dev instance (david010@gmail.com, persistent data). Use for feature dev and prod debugging.
- **david@drose.io** — ephemeral instance for testing signup flow. Can be nuked/reprovisioned freely.

### Before Push
```bash
make test-ci           # Simulate push/PR CI (validate + all unit tiers) — required
make test-e2e          # Playwright core + a11y — must pass 100%
```

### After Push
Treat deploys as **lanes**, not one linear checklist:

- **Runtime lane** (`server/**`, `web/**`, `engine/**`, `config/**`, `docker/runtime.dockerfile`): push triggers `runtime-image.yml`, then `deploy-and-verify.yml`, then `hosted-live-qa.yml`. That path deploys the **public demo runtime** and reprovisions the hosted canary tenant (`david010`).
- **Control-plane lane** (`control-plane/**`): push triggers `deploy-control-plane.yml`. That path deploys **only** the control plane and runs smoke/credential checks.
- **Manual ship** is fallback/recovery, not the default path.

For runtime pushes, `deploy-and-verify.yml` now waits for the matching `contract-first-ci.yml` run for the same SHA to finish green before any remote deploy action. Automatic deploy/live-QA runs also collapse superseded `main` commits; manual dispatch stays isolated so operators can still force a recovery run.

Runtime lane builds `ghcr.io/cipher982/longhouse-runtime` with `:latest` and an immutable full-commit tag. Superseded branch image builds cancel automatically; manual dispatch and release tags stay isolated. The public demo runtime and hosted tenants now consume that shared runtime image; the control plane remains a separate Coolify build from this repo.

If you manually ship, launch independent waits once and move on. Example: start a GHCR watch or a Coolify deploy wait, then do the next independent step. Do **not** sit in `pgrep`/health-check polling loops when a notification or blocking wait already exists.

### Manual Runtime Lane

```bash
gh run watch $(gh run list --workflow runtime-image.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
IMAGE_REF="ghcr.io/cipher982/longhouse-runtime:<full-commit-sha>"
./scripts/ops/coolify-deploy.sh longhouse-demo --docker-image "${IMAGE_REF%:*}" --docker-tag "${IMAGE_REF##*:}" --timeout 900
make reprovision IMAGE="$IMAGE_REF" # david010 by default
make qa-live
make qa-live-conversations
```

### Manual Control-Plane Lane

```bash
./scripts/ops/coolify-deploy.sh longhouse-control-plane --timeout 900
./scripts/qa/smoke-prod.sh --no-llm
./scripts/ops/check-cp-credentials.sh
```

### Manual Canary Reprovision

```bash
make reprovision                    # david010 (default) — auto-fetches admin token from zerg
make reprovision SUBDOMAIN=other
make reprovision IMAGE="ghcr.io/cipher982/longhouse-runtime:<tag>"
```
Data is safe — SQLite lives on a host bind mount at `/var/app-data/longhouse/<subdomain>`. Waits 15s and prints health status. Admin token is auto-fetched from the control-plane container on zerg; set `CONTROL_PLANE_ADMIN_TOKEN` explicitly in CI.

### Verify Deploy
```bash
make verify-prod                                       # API + browser validation
curl -s https://longhouse.ai/api/health                # Public demo runtime health
curl -s https://control.longhouse.ai/health            # Control plane health
curl -s https://david010.longhouse.ai/api/health       # User instance health
```

### Automation Note
Pushes to `main` that touch runtime paths trigger the runtime build and follow-on runtime deploy workflows automatically. Pushes that touch `control-plane/**` trigger the dedicated control-plane deploy workflow. Treat those as the primary path for normal app deploys, and keep the manual commands above as fallback/recovery.

### If Something Breaks
```bash
ssh zerg 'docker logs longhouse-david010 --tail 50'    # User instance
coolify app logs longhouse-demo                        # Public demo runtime
coolify app logs longhouse-control-plane               # Control plane
```

For hosted loop / turn-review debugging on a live tenant, start with `scripts/hosted-loop-debug.sh <subdomain>`. It resolves the instance through the control plane, authenticates a browser cookie, hits `/api/oikos/loop-inbox` + `/api/oikos/turn-reviews`, and only then falls back to a container-side SQLite probe.

### Checklist for Agents
1. `make test-ci` pass locally (validate + all unit tiers + engine + shipper).
2. `make test-e2e` pass locally (Playwright core + a11y).
3. Pick the lane that matches the change: runtime, control-plane, or both.
4. Prefer the GitHub automation chain over manual operator steps.
5. If you launch background waits, do other useful work and only block at the real dependency edge.
6. Verify the affected surface after deploy: public demo runtime, control plane, hosted canary, or all three.
7. Brief summary only at end (what shipped, what to manually verify if needed).

## Sauron / External Jobs

Sauron is separate again. Longhouse core only runs builtin product jobs; David's private cron/workload pack now runs via the standalone `~/git/sauron` service on `clifford`.

**If asked about jobs, sauron, or job failures: read `~/git/sauron-jobs/AGENTS.md` first.** Key facts:
- Longhouse user instances on `zerg` should stay builtin-only unless an explicit external jobs repo is configured
- The standalone `sauron` container on `clifford` is the authoritative runtime for `~/git/sauron-jobs`
- Job-specific secrets for the standalone runtime live in `/var/lib/docker/data/sauron/data/secrets.env` on `clifford`
- `host.docker.internal` still matters for the standalone scheduler because jobs ProxyJump through the host into other servers
- There must only ever be ONE active standalone Sauron scheduler for the private jobs pack; duplicate runtimes = duplicate scheduled jobs

## runner - Native Runner Daemon

Bun-compiled binary for command execution on user infrastructure. Connects via WebSocket, validates commands client-side (defense-in-depth), streams output. Install via `ENROLL_TOKEN=xxx curl .../install.sh | bash` → launchd (macOS) or systemd (Linux).

**Entrypoints:** `runner/src/index.ts` (daemon), `runner/src/executor.ts` (command exec), `zerg/services/runner_job_dispatcher.py` (backend dispatch). Release workflow: `.github/workflows/runner-release.yml`.

## Product Surface (Canonical)

Source of truth for product surface and priorities: `VISION.md` section **"Product Surface (2026-02 Decision)"**.

Do not maintain a second feature catalog in this file. Keep AGENTS focused on execution rules and link to canonical docs:
- Oikos tool contract lives in code under `server/zerg/tools/builtin/oikos_tools.py` and `server/zerg/services/`
- Harness/runtime contracts live in code under `server/zerg/services/` and `server/zerg/tools/`
- Runner daemon docs: `runner/README.md`
- Control plane docs: `control-plane/README.md`
- Shipper internals: `server/zerg/services/shipper/` (Python — hooks, install, auth)
- Rust engine (high-perf shipper daemon): `engine/` — `longhouse-engine connect` replaces Python watcher daemon; 27 MB RSS idle vs 835 MB Python
- Session processing: `server/zerg/services/session_processing/` (summarize, embeddings, content, tokens)
- Embedding cache: `server/zerg/services/embedding_cache.py`
- Video production: `video/` (Remotion studio — canonical video pipeline)
- Marketing screenshots: `scripts/capture_marketing.py`, `scripts/screenshots.yaml`

## Jobs: Builtin vs External

Jobs can live in two places:
- **Builtin** (`zerg/jobs/`): Product functionality OSS users need.
- **External** (`sauron-jobs/`): David-specific automation/integrations.

Rule of thumb: if an OSS user wouldn't need it, put it in `sauron-jobs/`.

## Demo & Seed Data

Two separate things exist — don't conflate or rebuild:
- **Fast to Fun** (onboarding): `services/demo_sessions.py` — seeds timeline demo sessions (currently 7) via empty-state CTA / `POST /api/agents/demo`
- **Showcase** (demos/videos): `scripts/build_demo_db.py` — builds full DB with scenarios

## Misc
- GH actions use runners on Cube
- if you are manually patching things for coolify you likely did something wrong. Remember infra-as-code.

## CI Test Runner
- Run: `scripts/ci/run-on-ci.sh <suite> [ref] [--test <path>]` and use `--help` for the allowlisted suites.

## Learnings (High-Signal Only)

<!-- Agents: keep this tight (<=10). Keep durable invariants only. If a learning is code-fixable confusion, track it outside the repo and remove it after the fix lands. -->
- (2026-02-05) [db] Alembic migrations are deprecated for core app work; `server/alembic/versions` is intentionally empty. New models use `AgentsBase.metadata.create_all()` auto-creation. **New columns on existing models must also be added to `_migrate_agents_columns()` in `database.py`** — SQLite ignores new columns on existing tables and will 500 without the ALTER.
- (2026-02-05) [security] Never store admin/device tokens in AI session notes; rotate immediately if exposed.
- (2026-02-12) [arch] Agent infra models use `AgentsBase` (not `Base`), live in `models/agents.py` and `models/work.py`. Schema `agents.` gets translate-mapped to `None` for SQLite.
- (2026-02-12) [frontend] Frontend API errors: `ApiError` class has `status`, `url`, `body` (already-parsed object, not string). FastAPI wraps HTTPException detail in `{detail: ...}`, so structured error data is at `body.detail.field`.
- (2026-02-14) [ops] **Reprovisioning an instance** = stop+remove container, then re-create with current env vars. Data is safe — SQLite lives on a host bind mount (`/var/app-data/longhouse/<subdomain>`), not inside the container. Use the admin API: `POST /api/instances/{id}/reprovision`. If secrets change on the control plane, instances must be reprovisioned to pick them up.
- (2026-03-08) [ops] Control-plane reprovision can briefly return `500` with a Docker name-conflict (`409`) even when the replacement eventually succeeds. After a failed reprovision, verify the instance `StartedAt`, `/api/health`, and startup logs before retrying or assuming it stayed on the old image.
- (2026-02-26) [ops] Instance LLM calls go directly to providers (Groq, OpenAI, z.ai) — NOT through `llm.drose.io`. Do NOT add `extra_body={"metadata": ...}` to any app code; Groq rejects it with 400. The LiteLLM proxy is personal-dev only.
- (2026-02-16) [ops] `get_llm_client_with_db_fallback()` checks DB `LlmProviderConfig` table before env vars. Stale DB rows with wrong API keys silently override env config → 401s. Delete stale rows: `db.query(LlmProviderConfig).filter(...).delete()`.
- (2026-02-20) [arch] **Python shipper is deleted.** Rust engine (`longhouse-engine`) owns all session shipping. `longhouse connect` manages the service only. Stop hook: `exec /abs/path/to/longhouse-engine ship --file "$TRANSCRIPT"` — absolute path baked at install time, no Python overhead. After any engine source change run `make install-engine` (build + codesign). `~/.local/bin/longhouse-engine` is a symlink to `target/release/` — never copy it.
- (2026-02-20) [auth] **Two auth systems — don't mix.** Browser pages: password-login JWT → `longhouse_session` cookie. `/api/agents/*` endpoints: device token → `X-Agents-Token` header (normally from `~/.claude/longhouse-device-token`; hosted live QA can mint a short-lived one via control-plane admin auth). Using JWT Bearer on agents endpoints gets 403.
- (2026-02-20) [frontend] **ForumCanvas viewport snaps on poll** if ResizeObserver effect depends on `state.layout` (object ref). Must depend on `state.layout.grid.cols/rows` (primitives) — object ref changes every 2s poll causing viewport re-center while user pans.
- (2026-03-09) [continuation] Keep the live `/api/sessions/{id}/chat` SSE route on direct `claude` invocation. Real provider-backed continuation smoke lives at `make test-e2e-continuation-provider` / `.github/workflows/continuation-provider-smoke.yml` with the CI-only `LONGHOUSE_CI_ANTHROPIC_API_KEY`; do not rely on ambient laptop Claude auth there.
- (2026-03-17) [ops] **Control-plane admin calls are user-agent sensitive.** `curl` and `scripts/lib/hosted-instance.sh` work; raw Python `urllib` defaults can trigger Cloudflare `1010` on `control.longhouse.ai` even with a valid `X-Admin-Token`.
- (2026-03-29) [db] `raw_json` compression backfill completion must count compressible legacy rows, not `raw_json_codec = 0` alone. Some `events` rows legitimately have `raw_json IS NULL`, so they remain codec 0 and are intentionally skipped.
