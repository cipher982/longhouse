# Longhouse (codename: Zerg)

Longhouse is mission control for CLI agent sessions running on machines the user owns.

The launch product is:

- session sync and memory
- remote control over real sessions running on user-owned machines

Keep the repo aligned to that story.

**Owner:** david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## First Principles

- **Read `VISION.md` first.** It is the product and architecture north star.
- **Optimize for the launch loop.** Import or start sessions, find them fast, steer them later.
- **One session, one execution owner.** Do not reintroduce magical local-to-cloud takeover as an implicit behavior.
- **Self-hosted and user-owned machines are the default truth.** Hosted is convenience, not the core product contract.
- **SQLite-only core.** Postgres is control-plane only.
- **`/api/agents/*` is the canonical machine surface.** The browser and MCP sit on top of it.
- **Keep behavior explicit.** No hidden fallbacks, silent mode switches, or duplicated capability logic.
- **Prefer obvious seams over clever reuse.** If two flows behave differently, split them.
- **Prefer deletion or freezing over half-supported surfaces.** Before launch, ambiguity is more expensive than missing features.
- **Design for cold restarts.** Durable state and reconstructable context beat giant hot threads.
- **Work on `main`.** Make atomic commits. Do not stash unrelated work.

## Communication

- Treat David as the PM and speak like the lead dev.
- Default to **outcome -> implication -> next move**.
- Prefer short prose over changelog-style responses.
- Do not dump file inventories or command transcripts unless they are needed to explain a blocker.
- Mention tests when they fail, were skipped, or materially change confidence.

## Task Tracking

- Do not create repo-local TODO lists, backlog files, or handoff trackers.
- If repo-local context is needed, write a short subject-driven spec or design note.
- Session notes and handoffs belong in `~/git/obsidian_vault/AI-Sessions/`, not this repo.

## Read Next

- `VISION.md`
- `docs/specs/agents-machine-surface.md`
- `docs/specs/prelaunch-simplification-cut-plan.md`
- `README.md`
- `runner/README.md`
- `control-plane/README.md`
- `server/README.md`

## Quick Commands

```bash
make dev
make stop
make test
make test-frontend
make test-engine
make test-control-plane
make test-runner
make test-e2e
make test-ci
make test-full
make qa-live
```

Do not assume `make dev` is already running.

## Testing

Run the tier that matches the change. Do not over-test.

- `make test` for backend Python changes in `server/zerg/` and `server/tests_lite/`
- `make test-frontend` for `web/`
- `make test-engine` for `engine/`
- `make test-control-plane` for `control-plane/`
- `make test-runner` for `runner/`
- `make test-e2e` for UI changes and before push when runtime behavior changed
- `make test-ci` before push
- `make test-full` only for broad pre-deploy verification

`tests_lite/` convention:

- no shared conftest
- use per-test SQLite DBs
- HTTP-level tests override dependencies on `api_app`, not `app`

## Architecture Essentials

- **API sub-app:** all `/api/*` routes live on `api_app`, not `app`.
- **Models config:** `config/models.json` is the single source of truth for models and embeddings.
- **Generated code:** do not edit `server/zerg/generated/`, `server/zerg/tools/generated/`, or `web/src/generated/` directly.
- **Tool contracts:** edit `schemas/tools.yml`, then run `scripts/generate_tool_types.py`.
- **Backend tests:** add new backend tests in `server/tests_lite/`.
- **Oikos tools:** registration is centralized in `server/zerg/tools/builtin/oikos_tools.py`.
- **Machine surface:** if a capability matters to agents or CLI workflows, it should exist on `/api/agents/*`.
- **High-frequency writes:** presence, heartbeat, ingest, runtime, and runner WebSocket writes must go through `WriteSerializer`.
- **Rust engine owns shipping:** the old Python shipper is gone. After engine changes, run `make install-engine`.

## Product Focus

Treat these as the launch-critical surfaces:

- session ingest and archive quality
- timeline, session detail, search, and recall
- machine-facing coordination primitives
- managed-local remote execution and control
- runner-backed execution on user-owned machines
- Oikos only where it helps users drive that same loop faster

Treat these as secondary or speculative unless the user explicitly asks:

- seamless cloud takeover or cloud-branch product stories
- proactive operator mode
- inbox, email, and conversation surfaces
- jobs as a user-facing product surface
- duplicate browser-only views that do not strengthen the core session loop

If you touch a secondary area, either simplify it toward the core story or explain why it still survives launch.

## High-Signal Gotchas

- `make dev` is interactive.
- Local SQLite dev DB is `~/.longhouse/dev.db` unless `DATABASE_URL` overrides it.
- `AGENTS.md` is canonical. `CLAUDE.md` is a symlink.
- Auth is disabled in dev by default (`AUTH_DISABLED=1`).
- Do not edit generated files directly.
- Dirty trees are normal. Stage only your changes.
- Do not commit valid secret-shaped dummy values. Generate ephemeral secrets in tests/dev code.
- Do not add `extra_body={\"metadata\": ...}` to provider LLM calls. Providers reject it.
- `get_llm_client_with_db_fallback()` checks DB provider config before env vars. Stale DB rows can silently override env keys.
- `~/.claude/longhouse-machine-name` is read at engine startup, not live.
- Use `scripts/hosted-loop-debug.sh <subdomain>` before improvising hosted loop debugging.
- If a tool or workflow already provides a completion signal, do not turn it into a polling loop.

## Pushing Changes

Before push:

```bash
make test-ci
make test-e2e
```

Deploy lanes:

- **Runtime lane:** changes under `server/**`, `web/**`, `engine/**`, `config/**`, or `docker/runtime.dockerfile` build the shared runtime image, deploy the public demo runtime, and reprovision the hosted canary.
- **Control-plane lane:** changes under `control-plane/**` deploy only the control plane.

Prefer the GitHub automation chain over manual ship steps.

Manual fallback:

```bash
./scripts/ops/coolify-deploy.sh longhouse-demo --timeout 900
./scripts/ops/coolify-deploy.sh longhouse-control-plane --timeout 900
make reprovision
make qa-live
```

## Jobs and External Automation

Longhouse core should keep builtin product jobs only.

If asked about Sauron, private cron packs, or job failures outside the core product:

- read `~/git/sauron-jobs/AGENTS.md` first
- assume the standalone Sauron runtime is authoritative, not this repo

## Learnings (High-Signal Only)

- New columns on existing models must also be added to `_migrate_agents_columns()` in `server/zerg/database.py`.
- Agent infra models live on `AgentsBase`, not `Base`.
- Browser auth and machine auth are different systems. Browser pages use the `longhouse_session` cookie; `/api/agents/*` uses `X-Agents-Token`.
- The Rust engine is the shipping path. `longhouse connect` manages the service; it does not run a Python shipper.
- Demo session reset/reseed uses `provider_session_id LIKE 'demo-%'`, not `device_id`.
- Control-plane admin calls are user-agent sensitive. Use the existing scripts and curl patterns before improvising custom Python clients.
- Hosted tenant data on `zerg` lives under `/var/app-data/longhouse/<subdomain>` and mounts to `/data` in the container.
