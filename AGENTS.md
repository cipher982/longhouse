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

## System Map

Use these nouns consistently. Do not say just "the daemon" without clarifying which one.

- **Control Plane**: hosted-only signup, billing, and provisioning. Never part of the self-hosted local install.
- **Runtime Host**: the main Longhouse backend product runtime: FastAPI API, bundled web UI, and database-backed state. In self-host mode this is what `longhouse serve` runs. In hosted mode we run it for the user.
- **Machine Agent**: the Rust engine (`longhouse-engine`). It drains local hook outbox files, ships events, retries spool, writes `~/.claude/engine-status.json`, and emits heartbeats. It is the shipping path.
- **Desktop App**: macOS `Longhouse.app`. Native local status/setup/repair/menu bar surface. It is not the Runtime Host and not the Machine Agent.
- **Runner**: optional WebSocket command executor for remote execution on user-owned machines.
- **CLI / Package Layer**: the current delivery and orchestration layer. PyPI `longhouse` installs the CLI plus Runtime Host entrypoint and manages Machine Agent/Desktop App installation. Treat this as first-class for agents and power users, but not the desired human product boundary.

## Install Topology

The Machine Agent runs where work happens. The Runtime Host runs where durability should live.

- **Trying it out**: Machine Agent + Runtime Host both on a laptop. Works, but stops when the laptop sleeps. Good for first use and demos.
- **Self-hosted durable**: Machine Agent on dev machines. Runtime Host on an always-on box (VPS, homelab, Mac mini). The recommended setup for power users.
- **Hosted paid plan**: Machine Agent on dev machines. We run Control Plane + Runtime Host.

Install channels:

- **Mac human happy path**: `Longhouse.app`.
- **Agent/headless/power-user**: `uv tool install longhouse`, PyPI, or `curl | bash`.
- **Canonical repair seam**: `longhouse connect --install`.

Boundary rules:

- The Machine Agent is always on the user's dev machine(s).
- The Runtime Host is on the user's always-on box (self-hosted) or our infra (hosted). On a laptop it is trial-mode only.
- The Desktop App is the macOS status surface. It must never become a dead wrapper around hidden CLI assumptions.

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
- `docs/specs/macos-launch-product-shape.md`
- `docs/specs/distribution-update-loop.md`
- `docs/specs/agents-machine-surface.md`
- `README.md`
- `runner/README.md`
- `control-plane/README.md`
- `server/README.md`

## Quick Commands

```bash
make dev
make stop
make dogfood-refresh
make dogfood-check
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

## Remote CI

- Prefer GitHub Actions on cube ARC for heavy validation, browser jobs, and anything that can wait for remote feedback.
- ARC lanes for this repo are `cube-fast`, `cube-browser`, `cube-deploy`, and `cube-maint`.
- ARC is only used by GitHub Actions for `cipher982/longhouse`. Local `make` commands and unpushed branches run on the current machine.
- Reserve local `make test-ci`, `make test-e2e`, `make test-full`, and long Rust builds for targeted debugging, remote CI outages, or when David explicitly wants a local run.

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
- Oikos only where it helps users drive that same loop faster

Treat these as support tier (functional but not launch-critical):

- runner-backed execution on user-owned machines
- Oikos proactive operator behavior

Treat these as frozen for launch:

- cloud-branch / cloud-takeover product stories (capability gate is off)
- loop inbox, turn reviews, and email surfaces (hidden from nav)
- jobs as a user-facing product surface
- briefings and insights as standalone pages

If you touch a secondary area, either simplify it toward the core story or explain why it still survives launch.

## High-Signal Gotchas

- `make dev` is interactive.
- After local runtime changes (engine, hooks, `connect`, desktop app/menu bar), run `make dogfood-refresh` to reinstall the real local runtime from current repo source. DMG drag-install is release transport, not the daily dogfood loop.
- `make test-ci`, `make test-e2e`, `make test-full`, and long `cargo` builds run locally and can saturate the laptop. Prefer the repo's GitHub ARC workflows on cube when possible.
- `scripts/ci/installer-first-run.sh` stays light locally by default; use `make test-install-first-run-fresh` only when you need a rebuilt frontend, and treat `--menubar` / `make test-install-macos-ambient` as the heavy macOS path. Prefer GitHub Actions unless menu bar install debugging is the point.
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
- `/api/timeline/sessions` caps `limit` at 100. Frontend URL parsing needs to clamp to that or the timeline can self-422 on oversized `limit` params.

## Pushing Changes

**`git push` is the full deploy action in 95% of cases.** GitHub Actions detects which paths changed and fires the right lane automatically. Agents do not need to figure out what to build — just push.

Two completely separate user populations update differently:

**Hosted users** (demo + paid tenants on your infra) — update on every push via CI:

| Changed paths | Lane | Extra step? |
|---|---|---|
| `server/`, `web/`, `engine/`, `config/`, `docker/runtime.dockerfile` | Rebuilds runtime image, redeploys demo, reprovisions canary | Only if DB migration added → `make reprovision` |
| `control-plane/` | Control-plane deploy only | No |
| `runner/` | Runner lane | No |
| Docs/scripts/tests | CI only, no deploy | No |

**Self-installed users** (CLI via PyPI, `Longhouse.app` download) — completely decoupled from push:
- Only get an update when you publish a GitHub release (`vX.Y.Z` tag → triggers PyPI publish)
- Must manually run `longhouse upgrade` or `uv tool upgrade longhouse` — no silent auto-update
- `longhouse version --check` tells them if they're behind
- After CLI upgrade, they may also need `longhouse connect --install` to rewire hooks/engine service

**Mac app binary:** every push runs a packaging smoke test (builds + signs `.app` to catch breakage early) but does NOT distribute. Real signed + notarized release only fires on a published `vX.Y.Z` GitHub release.

**Provisioning rule:** if your change adds a new DB column, new required env var, or touches schema — flag it explicitly and run `make reprovision` after CI passes. Otherwise skip it.

**Machine agent (Rust engine):** hosted tenants get the new binary automatically via the runtime image. Users running the engine locally need `make install-engine`. Mention this when shipping engine changes.

Default path:

```bash
git push
gh run list -R cipher982/longhouse --limit 3  # confirm lanes fired
```

Local fallback when remote CI is unavailable or David explicitly wants a local run:

```bash
make test-ci
make test-e2e
```

Manual deploy fallback:

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
- macOS menu bar surfaces should open from cached/loading state and refresh off the main thread; SwiftUI menu hosts are prone to size and open-latency regressions, so prefer explicit AppKit popover control for fixed panels.
- Public macOS onboarding should talk about `Longhouse.app` / the desktop app only; keep `local-health` as internal diagnostic terminology, not public copy or artifact naming.
- For macOS menu bar UI changes, PNG harness renders are required QA; inspect the literal full-frame images before reinstalling `Longhouse.app`.
- The tight master logo SVG is not enough proof for the macOS status item; the generated menu bar glyph still needs menu-bar-specific optical framing and a live installed-app capture check before ship.
- Timeline card `mouseenter` prefetch can turn scrolling under a stationary cursor into accidental workspace-request churn; treat hover prefetch as scroll-sensitive, not as free.
- Timeline card `transition: all` hover animations can explode software-raster cost when cards slide under a stationary cursor during scroll; active scrolling should suppress card hover transitions.
- Decorative shell animations can also steal software-render budget from timeline scroll. On the timeline route, pause header glow animations during active scrolling instead of only tuning the cards.
- `/api/threads/{id}/runs` needs direct backend coverage. Dead runtime imports can survive broad suites and only surface in hosted chat smoke.
