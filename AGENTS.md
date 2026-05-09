# Longhouse (codename: Zerg)

Longhouse is mission control for CLI agent sessions running on machines the user owns.

The launch product is:

- session sync and memory
- remote control over real sessions running on user-owned machines

Keep the repo aligned to that story.

**Owner:** david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

Repo-local skills live in `.agents/skills/`, but Claude Code only discovers project skills through `.claude/skills/`. Keep the repo-level symlink `.claude/skills -> .agents/skills` in place or the skills will not load.

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
- **iOS App**: native SwiftUI client for viewing and replying to sessions plus a home-screen widget. Read/steer-only client — does not host a Runtime or run an engine. Always connects to a hosted instance.
- **Runner**: optional WebSocket command executor for remote execution on user-owned machines.
- **Provider CLI**: an upstream executable the user installs separately, such as `claude`, `codex`, or `gemini`. Longhouse may launch it through a managed control path, but Longhouse does not vendor, pin, or update provider CLIs.
- **CLI / Package Layer**: the current delivery and orchestration layer. PyPI `longhouse` installs the CLI plus Runtime Host entrypoint and manages Machine Agent/Desktop App installation. It installs Longhouse pieces, not provider CLIs. Treat this as first-class for agents and power users, but not the desired human product boundary.

### Things we built
- website landing page, website admin control plane, website 'main app you do everything', macos menu bar, ios app with widget, rust daemon to ship sessions

## Install Topology

The Machine Agent runs where work happens. The Runtime Host runs where durability should live.

- **Trying it out**: Machine Agent + Runtime Host both on a laptop. Works, but stops when the laptop sleeps. Good for first use and demos.
- **Self-hosted durable**: Machine Agent on dev machines. Runtime Host on an always-on box (VPS, homelab, Mac mini). The recommended setup for power users.
- **Hosted paid plan**: Machine Agent on dev machines. We run Control Plane + Runtime Host.

Install channels:

- **Mac human happy path**: `Longhouse.app`.
- **iOS**: built from `ios/` via Xcode; no public distribution yet (personal install via Xcode build-and-run).
- **Agent/headless/power-user**: `uv tool install longhouse`, PyPI, or `curl | bash`.
- **Canonical configured-machine repair seam**: `longhouse machine repair`. Use `longhouse connect --install` for first install or force-reinstall.

Boundary rules:

- The Machine Agent is always on the user's dev machine(s).
- The Runtime Host is on the user's always-on box (self-hosted) or our infra (hosted). On a laptop it is trial-mode only.
- The Desktop App is the macOS status surface. It must never become a dead wrapper around hidden CLI assumptions.
- The iOS App is a pure client. It never ships sessions, never hosts a runtime, and never runs an engine — it talks to an existing hosted instance (today: david010).

## Communication

- Treat David as the PM and speak like the lead dev.
- Default to **outcome -> implication -> next move**.
- Prefer short prose over changelog-style responses.
- Do not dump file inventories or command transcripts unless they are needed to explain a blocker.
- Mention tests when they fail, were skipped, or materially change confidence.

## Shared Worktree Reality

- **Four or five agents may be working in this exact directory on this laptop right now.** The filesystem, index, and `main` are shared. Plan for interference, do not assume exclusivity.
- "Latest" is not evidence. Branch-latest workflow lists, generic tails, and recent logs often belong to another agent or another deploy.
- Any CI or deploy claim must be anchored to an exact commit SHA, workflow run id, session id, or container/service name.
- If deployment matters, verify live surface SHAs too; demo, control plane, and canary can be on different commits during rollout.
- **Always `git status` before committing.** The index is shared. Plain `git add <file> && git commit` can sweep another agent's staged files into your commit. Prefer `git commit <paths>` / `git commit -o <path>` to commit only the files you touched.
- **Never reset or force-push a commit that already landed on `main`.** If you accidentally bundled another agent's work into your commit, roll forward — the mess is cheaper than a force-push race. "Atomic commits" is a preference; not losing pushed work is a hard rule.
- **A failing CI check on your commit isn't automatically yours.** Read the failing job first — path, test name, whether the regression predates your SHA. Parallel agents deploy concurrently; a hosted-chat smoke failure on a backend-only commit is usually someone else's in-flight fix.
- **Pre-commit stashes unstaged files and restores them after hooks run.** If files seem to appear or disappear mid-commit, that's the stash dance, not corruption.

## Cowbell

- `cowbell` is the PM trigger for the full ship cycle. David should only need to say `cowbell`.
- Use `.agents/skills/zerg-ship/SKILL.md` for any full ship/deploy work in this repo. That skill owns task-commit resolution, exact-SHA monitoring, command flow, and final success/failure reporting.

## Task Tracking

- Do not create repo-local TODO lists, backlog files, or handoff trackers.
- If repo-local context is needed, write a short subject-driven spec or design note.
- Session notes and handoffs belong in `~/git/obsidian_vault/AI-Sessions/`, not this repo.

## Read Next

Start with `VISION.md`, `README.md`, `docs/specs/agents-machine-surface.md`, and `docs/specs/distribution-update-loop.md`.

Then read the component README you are touching: `server/README.md`, `control-plane/README.md`, or `runner/README.md`.

## Quick Commands

```bash
make dev
make stop
make ui-capture PAGE=timeline SCENE=timeline-card-stress VIEWPORT=mobile
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

For timeline/mobile UI work, read `.agents/skills/zerg-ui/SKILL.md` first and prefer the fixture-backed `ui-capture` loop before asking for manual live review.

Do not assume `make dev` is already running.

## Testing

Run the tier that matches the change. Do not over-test.

- `make test` for backend Python changes in `server/zerg/` and `server/tests_lite/`
- `make test-frontend` for `web/`
- `make test-engine` for `engine/`
- `make test-control-plane` for `control-plane/`
- `make test-runner` for `runner/`
- For `ios/` changes: run the Xcode `Longhouse` scheme tests via `xcodebuild ... test` (or the in-editor test action). There is no `make` target for iOS.
- `make test-e2e` for UI changes and before push when runtime behavior changed
- `make test-ci` before push
- `make test-full` only for broad pre-deploy verification

`tests_lite/` convention:

- no shared conftest
- use per-test SQLite DBs
- HTTP-level tests override dependencies on `api_app`, not `app`

## Remote CI

- Prefer GitHub Actions on cube ARC for heavy validation, browser jobs, and anything that can wait for remote feedback.
- The self-hosted ARC label for this repo is `cube`.
- ARC is only used by GitHub Actions for `cipher982/longhouse`. Local `make` commands and unpushed branches run on the current machine.
- Reserve local `make test-ci`, `make test-e2e`, `make test-full`, and long Rust builds for targeted debugging, remote CI outages, or when David explicitly wants a local run.

## Architecture Essentials

- **API sub-app:** all `/api/*` routes live on `api_app`, not `app`.
- **Models config:** `config/models.json` is the single source of truth for models and embeddings.
- **Generated code:** do not edit `server/zerg/generated/`, `server/zerg/tools/generated/`, or `web/src/generated/` directly.
- **Tool contracts:** edit `schemas/tools.yml`, then run `scripts/generate_tool_types.py`.
- **Backend tests:** add new backend tests in `server/tests_lite/`.
- **Agent tools:** registration is centralized in `server/zerg/tools/builtin/__init__.py`; generated contracts come from `schemas/tools.yml`.
- **Machine surface:** if a capability matters to agents or CLI workflows, it should exist on `/api/agents/*`.
- **Agent history ownership:** Longhouse owns raw session history. Other systems should call `/api/agents/*` or store narrow references, not build second ingest/query stacks.
- **High-frequency writes:** presence, heartbeat, ingest, runtime, and runner WebSocket writes must go through `WriteSerializer`.
- **Rust engine owns shipping:** the old Python shipper is gone. After engine changes, run `make install-engine`.

## Product Focus

Treat these as the launch-critical surfaces:

- session ingest and archive quality
- timeline, session detail, search, and recall
- machine-facing coordination primitives
- managed-local remote execution and control
- assistant surfaces only where they help users drive that same loop faster

Treat these as support tier (functional but not launch-critical):

- runner-backed execution on user-owned machines
- proactive operator behavior

Treat these as frozen or removed for launch:

- cloud-branch / cloud-takeover product stories (capability gate is off)
- loop inbox and turn reviews (removed); email surfaces (hidden from nav)
- jobs as a user-facing product surface
- briefings and insights as standalone pages

If you touch a secondary area, either simplify it toward the core story or explain why it still survives launch.

## Managed vs Unmanaged Sessions

- **Managed** means Longhouse owns the control path for the session, not necessarily the provider binary. Today that usually means the session was launched with `longhouse claude`, `longhouse codex`, or another Longhouse wrapper, and it is reflected by `session.capabilities.live_control_available` or `host_reattach_available`.
- A `this-device` managed launch without a remote command Runner row is managed but observe-only from browser/iOS until a Runner-backed send path exists; `host_reattach_available` may still be true.
- **Unmanaged** means Longhouse imported or discovered the session but does not own its live control path. Bare provider CLI runs are unmanaged compatibility ingest: searchable, sometimes partially live, but not steerable from the browser.
- Do not use "started in Longhouse" in user-facing copy. It sounds like the website. Prefer `managed` / `unmanaged`, then separately describe whether the session is steerable, observe-only, or only has transcript-level liveness.
- The timeline, wall, and workspace should all tell you whether the session you are looking at is managed before you try to drive it. Always rely on `session.capabilities` so you do not assume bare CLI sessions can behave like managed ones.

## Provider CLI Control

- Longhouse manages session control, not provider binary distribution. Managed wrappers should be explainable as: user-owned provider CLI plus Longhouse-owned control path plus Runtime Host/Machine Agent state.
- Use `.agents/skills/managed-provider-cli/SKILL.md` for Codex/Claude/Gemini managed-session work, provider binary drift, bridge/relay/hooks/liveness, or changes to this terminology.
- Codex's hard contract: stock upstream `codex` from PATH by default; `--codex-bin` / `LONGHOUSE_CODEX_BIN` are explicit debug overrides; no `longhouse-codex`, no `~/.longhouse/runtimes/codex`, no release-asset or custom-fork lane.

## High-Signal Gotchas

- `make dev` is interactive.
- After local runtime changes (engine, hooks, `connect`, desktop app/menu bar), run `make dogfood-refresh` to reinstall the real local runtime from current repo source. DMG drag-install is release transport, not the daily dogfood loop.
- Python-CLI-only changes under `server/zerg/cli/` skip the Rust rebuild — `cd server && uv tool install -e .` is ~5s vs ~60s for `make dogfood-refresh`. This is a narrow shortcut; it does not apply to engine, hooks, connect, desktop app, or iOS.
- Warp's Codex sidebar/rich status comes from OSC 777 `notify;warp://cli-agent` terminal markers written to `/dev/tty`; command regexes and foreground process groups alone are not enough for managed `longhouse codex`.
- **iOS app changes** (`ios/`) are pure Swift — no `make` target, no dogfood-refresh, no install step. An Xcode rebuild/run is the whole loop. The iOS app has no runtime or engine to reinstall.
- Heavy local validation can saturate the laptop. Prefer GitHub ARC for `make test-ci`, `make test-e2e`, `make test-full`, long `cargo` builds, and heavy installer/menu bar paths unless local debugging is the point.
- Local SQLite dev DB is `~/.longhouse/dev.db` unless `DATABASE_URL` overrides it.
- `AGENTS.md` is canonical. `CLAUDE.md` is a symlink.
- Auth is disabled in dev by default (`AUTH_DISABLED=1`).
- Dirty trees are normal. Stage only your changes.
- Do not commit valid secret-shaped dummy values. Generate ephemeral secrets in tests/dev code.
- Do not add `extra_body={\"metadata\": ...}` to provider LLM calls. Providers reject it.
- `get_llm_client_with_db_fallback()` checks DB provider config before env vars. Stale DB rows can silently override env keys.
- `~/.claude/longhouse-machine-name` is read at engine startup, not live.
- Use `scripts/ops/hosted-session-debug.sh --subdomain <subdomain> --session <session-id>` before improvising hosted session/runtime debugging.
- If a tool or workflow already provides a completion signal, do not turn it into a polling loop.
- `/api/timeline/sessions` caps `limit` at 100. Frontend URL parsing needs to clamp to that or the timeline can self-422 on oversized `limit` params.
- Warp-style CLI agent detection appears to key off the final spawned executable basename in the PTY. For managed Codex, debug the attached stock `codex --enable tui_app_server --remote ...` process, not just the shell wrapper or alias that started it.
- For `longhouse-engine codex-bridge start` repros, use `--isolation-root <tmp-dir>` so bridge files and Longhouse DB/session_binding state are isolated together.
- Managed Codex propagation profiler attaches must mirror the launch-provided attach command, including `LONGHOUSE_MANAGED_SESSION_ID` and `-c check_for_update_on_startup=false`; dropping either creates misleading probe failures.

## Pushing Changes

**`git push` is the deploy trigger in 95% of cases.** Hosted surfaces update from push-triggered CI; self-installed CLI/app users do not.

Hosted push lanes:

- Runtime paths (`server/`, `web/`, `engine/`, `config/`, `docker/runtime.dockerfile`) rebuild the runtime image and roll demo + canary.
- `control-plane/` changes deploy the control plane.
- `runner/` changes use the runner lane.
- Docs/scripts/tests may run CI only.

Separate release paths:

- Self-installed CLI and `Longhouse.app` users only update on a published `vX.Y.Z` release.
- `longhouse upgrade` / `uv tool upgrade longhouse` updates the CLI; some upgrades still need `longhouse machine repair` afterward.
- macOS app packaging smoke runs on push, but real app distribution is release-only.
- **iOS app**: no push-triggered release. `git push` does not deploy iOS. Personal install today is Xcode build-and-run; there is no TestFlight or App Store distribution yet.

Extra rules:

- If you add a DB column, new required env var, or touch schema, call it out and run `make reprovision` after CI.
- Hosted tenants get engine changes through the runtime image; users running the engine locally still need `make install-engine`.

### David's laptop is always dogfooding

David is the primary dogfood user. `make ship` updates hosted surfaces
but does **not** touch his local `longhouse` CLI, `longhouse-engine`
daemon, or `Longhouse.app` menu bar. Those are installed into his
system and go stale silently.

**After every successful `make ship`**, run:

```bash
make dogfood-refresh
launchctl kickstart -k gui/$(id -u)/ai.longhouse.app
```

No conditionals. Not "if the runtime changed." Always. The menu bar
will show "restart pending" otherwise, and David is stuck running old
code while hosted is already on the new SHA.

**iOS:** if the change touched `ios/`, call it out explicitly — David
has to plug his phone in and Xcode-build, because there is no
TestFlight or App Store distribution yet. Do not mark iOS changes
"shipped" without this prompt.

## Jobs and External Automation

Longhouse core should keep builtin product jobs only.

If asked about Sauron, private cron packs, or job failures outside the core product:

- read `~/git/sauron-jobs/AGENTS.md` first
- assume the standalone Sauron runtime is authoritative, not this repo

## Learnings (High-Signal Only)

- New columns on existing models must also be added to `_migrate_agents_columns()` in `server/zerg/database.py`.
- Agent infra models live on `AgentsBase`, not `Base`.
- Browser auth and machine auth are different systems. Browser pages use the `longhouse_session` cookie; `/api/agents/*` uses `X-Agents-Token`.
- Demo session reset/reseed uses `provider_session_id LIKE 'demo-%'`, not `device_id`.
- Control-plane admin calls are user-agent sensitive. Use the existing scripts and curl patterns before improvising custom Python clients.
- Hosted tenant data on `zerg` lives under `/var/app-data/longhouse/<subdomain>` and mounts to `/data` in the container.
- If `longhouse.ai` suddenly serves an old landing bundle after a green deploy, check whether the demo runtime was redeployed with `ghcr.io/cipher982/longhouse-runtime:latest`; if `latest` stopped being refreshed, non-runtime deploys can silently roll the public demo back to stale frontend assets.
- Deploy, hosted QA, and post-deploy smoke jobs should target the warm `cube-deploy` ARC lane, not the legacy `cube` label; `cube` can make checkout/setup look like deploy regressions.
- Managed Codex TUI exits with `Connection reset without closing handshake` are not necessarily bridge crashes. Check `~/.codex/log/codex-tui.log`, the bridge `.json` state, rollout JSONL, and app-server `readyz` before assuming root cause; the relay should prevent the backpressure-induced variant of this, so if you see one, look at whether the relay spawned (`engine/src/codex_bridge.rs` logs the relay URL) and whether the connection is actually routing through it.
- If a managed Codex session has no bridge `.sock`/`.json` but its `codex app-server` process still listens on the old port, the bridge parent died and left a provider child orphaned; treat it as a cleanup/control-path failure, not proof that the model turn was lost.
- Managed Codex bridge state can be stale after startup `thread/resume failed: no rollout found`; if that happens, trust the thread rollout JSONL plus `~/.codex/log/codex-tui.log` over bridge `last_turn_status` for whether the turn actually completed.
- Managed Codex hook-review prompts are provider preconditions, not propagation failures. The bridge/app-server log may not show them; check the attached TUI log for `hooks need review` and classify profiler runs as blocked until hooks are reviewed.
- Managed Codex reaper grace is awake-time on macOS: it uses Rust/Tokio `Instant`, backed by `CLOCK_UPTIME_RAW`, so laptop sleep does not consume the 120s orphan grace; SSH disconnects on an awake remote host still do.
- For slow or inconsistent managed sessions, use `.agents/skills/managed-session-debug/SKILL.md`; compare local transcript timing, local-health, hosted runtime state, and WriteSerializer pressure before blaming telemetry.
- macOS menu bar work is latency-sensitive: open from cached/loading state, refresh off the main thread, keep `local-health` internal-only, and require PNG harness plus live installed-app capture QA before ship.
- Longhouse icon source assets are zero-padding contracts unless the file is explicitly a padded surface asset. Add inset per derivative/surface, and verify exported PNG alpha bounds instead of trusting SVG viewBox geometry.
- For menu bar pill regressions, prefer fixed-time `SnapshotRenderer` PNG baselines over macOS XCUITest accessibility assertions; the window-host AX tree can omit chip text even when the pill is visibly rendered.
- Menu bar process/session truth should be modeled explicitly, not inferred from `recent activity`; fixture-first states like `attached`, `detached`, `degraded`, and `orphan bridge` are the right harness loop before wiring live data.
- Managed session phase in `local_health` now comes from the machine-local `session_phase_state` ledger plus any newer hook outbox file. Process/bridge scans answer liveness only; `phase=null` / “Phase unknown” is intentional when an already-running session has not emitted a local phase signal since the current runtime was installed.
- Managed Codex live transcript sends use a separate realtime cursor; do not advance archival `file_state` from EventOnly live sends or full raw source context can be skipped forever.
- Managed Codex progress wakes carry session identity into the immediate live lane; turn-start wakes should only arm active polling because useful rollout content may not exist yet.
- In local-health JSON, `control_path` (managed/unmanaged), `liveness_model` (bridge/process scan), and `state` (attached/detached/degraded) are separate axes. Do not infer one from another.
- Complete engine process snapshots close absent stale unbound Claude/Codex/Gemini sessions as `process_gone`, gated by phase freshness plus the 90s unbound close grace; failed or partial scan states need a separate payload flag before widening this further.
- Timeline scroll performance is fragile: hover prefetch, card hover transitions, and decorative shell animations can steal raster budget during active scroll and should be suppressed when scrolling.
- Timeline stream pubsub is process-local; keep its fallback wait low enough that missed cross-worker publishes cannot leave managed lifecycle cards stale for seconds.
- `/api/threads/{id}/runs` needs direct backend coverage. Dead runtime imports can survive broad suites and only surface in hosted chat smoke.
- iOS auth boundary fixes cannot rely on `WKNavigationDelegate` alone; React/SPA transitions to `/login` can bypass network navigation, so the shell must also observe in-page history changes before web login leaks into the app.
- Replaying an exported session through `longhouse-engine ship --file /tmp/...` rewrites `source_path` to the temp path; UUID-backed events dedupe cleanly, but non-UUID events and `source_lines` do not. For hosted repairs, preserve the original source path in the ingest payload or normalize the temp-path rows afterward.
- Claude `tool_result` payloads are not guaranteed to be text. Image blocks and `tool_reference` arrays still need a synthetic `role=tool` event during ingest or the timeline/iOS surfaces will show phantom running tool calls.
- iOS timeline pairing mirrors the web pairing logic (`ios/Sources/Shared/TimelineBuilder.swift` ↔ `web/.../timelineModel.ts`): assistant-with-tool_name + role=tool paired on `tool_call_id`. If you change pairing rules on one side, change the other — otherwise mobile and web disagree on what "one row" means.
- iOS treats any tool call older than 1hr or inside an ended session as "dropped" rather than "running", because real ingest still occasionally loses `role=tool` events (Bedrock orphan rate ~9% when last checked). Don't rip this client-side guardrail out until the ingest side is fixed.
- iOS `SessionView` loads session detail and tail events in parallel; a timeline events-route failure surfaces as "Couldn't load session" even when `/timeline/sessions/{id}` succeeds. Keep browser-cookie timeline wrappers parameter-compatible with their `/agents/sessions/*` delegates.
- Server-side runtime truth lives only in `session_runtime_state`. The old `session_presence` TTL cache and its `presence_cache` service are gone — `/api/agents/presence` now emits `RuntimeEventIngest` rows through the reducer. Do not re-introduce a parallel presence cache; if you need richer overlay semantics, extend `build_runtime_view` / `resolve_runtime_overlay`.
- Timeline empty-state E2E is not stable after first load because `SessionsPage` auto-seeds demo sessions when the timeline is empty. For no-runner browser tests, assert that `Machines` stays reachable via either the empty-state CTA or the global nav, not that the blank state persists.
- Build identity and release version are different nouns. Release version is a single lockstep semver for all five manifests, bumped only via `scripts/ops/release.sh` (which delegates to `bump-my-version` per `.bumpversion.toml`). Build identity is per-binary (`.build/build-identity.json`, regenerated by `scripts/build/generate_build_identity.py`) and is what CLI/health/engine/menu bar/iOS should display. Never hand-edit component manifests to bump versions, and never display release version where build identity is meaningful — two different commits can share a release version but never a build identity. The control-plane FastAPI does not pin `version=`: the service runs as a source checkout (not an installed distribution) so `importlib.metadata.version(...)` fails, and its OpenAPI `info.version` is low-signal. Leave it unset; do not "fix" it by re-introducing a hardcoded release version.
- `needs_user` is often the provider's normal idle prompt after an assistant response, not durable unfinished work. When an explicit machine snapshot says a previously observed unmanaged process binding disappeared, mark that binding stale so lifecycle closes as `process_gone`; do not shorten `needs_user` freshness to hide stale attention.
