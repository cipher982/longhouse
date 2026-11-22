# Architecture

How Longhouse fits together, what its nouns mean, and where code lives. The
product thesis and invariants are in [`VISION.md`](VISION.md).

## Components

| Component | What it is | Runs on |
|---|---|---|
| Machine Agent | Rust engine (`longhouse-engine`). Drains the hook output provider CLIs write, ships session events to the Runtime Host with durable retry, sends heartbeats | each machine where you work |
| Runtime Host | `longhouse-server serve`: FastAPI API, bundled web UI, SQLite state | an always-on box (VPS, homelab, Mac mini), or hosted |

On a laptop both run together as a trial, and stop when the laptop sleeps.
Durability comes from where you put the Runtime Host.

```
  dev laptop ─┐
              ├─ Machine Agent ◀─events / commands─▶ Runtime Host ◀─▶ web / CLI / iOS
  dev box ────┘                                  (SQLite, durable)
       │
       └─ installed provider clients own execution, auth, tools, and billing
```

## Design rules

These add to the product invariants in `VISION.md`.

- **`/api/agents/*` is the machine surface.** Browser, CLI, MCP and iOS sit on
  the same primitives; none is a separate source of truth.
- **Normalize control, not provider execution.** Longhouse exposes one session
  and capability model. The installed provider client keeps its agent loop,
  authentication, billing and tools.
- **Rely on `session.capabilities`, not a session type.** Some sessions have
  live control, some need host reattach, some are search-only.
- **Two lanes.** The live lane answers what is happening now and must feel
  terminal-fast. The durable lane answers what provably happened and must be
  ordered and replayable.
- **Observations never compete with control.** Runtime events and presence have
  a per-device quota. Launch and input use control admission. Heartbeats and
  machine presence use liveness admission. Batches carrying bindings,
  interactions, wakes or execution ends use lifecycle admission with cooldowns.
  A malformed runtime body spends observation quota, never lifecycle quota.
- **OMP coordination belongs to the main managed session.** Native subagents
  report blockers and results through OMP's parent channel and cannot send as
  their parent. Child lifecycle events leave the parent's turn identity
  unchanged, and child switch or branch operations stay provider-owned.
- **iOS opens a session in two lanes** over the same server facts. The primary
  lane fetches identity and native controls so the title, runtime state and
  composer do not wait for the transcript. The transcript lane hydrates the
  recent tail and renders it in WebKit. A cached snapshot is instant paint
  only; the first accepted server tail is authoritative.

## Session modes

Every session has exactly one mode. This section is the canonical definition;
other docs link here and do not redefine it.

- **Shadow** - unmanaged, observe-only. The Machine Agent found a session it
  did not launch (for example a bare `claude` run). It is searchable and
  sometimes partly live, but Longhouse never owned its control path.
- **Helm** - managed, interactive, remote-steerable. A human starts it from a
  physical terminal with `longhouse claude`, `longhouse codex`, or another
  provider wrapper. Longhouse keeps the provider's normal TUI and owns its
  control path. Once the terminal-originated session exists, web and iOS may
  steer it; they never originate a headless Helm session. The provider process
  may persist while the TUI stays open.
- **Console** - managed, headless, UI-dispatched. Web or iOS sends one-shot work
  through the Machine Agent. The provider process runs one turn and exits, and
  state persists to disk. Longhouse picks the provider's flags here, so every
  Console provider runs in its most permissive permission mode with no
  per-session choice (`engine/src/console_permission.rs` is the one table).
  Helm adds no permission flag of its own: it passes through the user's own
  flags and settings. Shadow only observes.

"Unmanaged" means Shadow and "managed" means Helm or Console. The two
vocabularies answer different questions: can Longhouse steer this at all, and
exactly how.

`schemas/managed_providers.yml` is the authority for what each provider
supports. The modes map to its fields as follows.

| Mode | Manifest and code fields |
| --- | --- |
| Shadow | discovered or unmanaged import; no `launch_*` capability |
| Helm | `launch_local`, plus adapter-proven live controls such as `steer_active_turn` and `send_input` |
| Console | `turn_start`; `run_once` (legacy, being retired) |

A persistent no-terminal process is not a fourth mode and not a valid Helm launch.

## Glossary

- **Provider CLI** - an upstream binary you install yourself (`claude`, `codex`,
  `opencode`, Antigravity). It keeps its authentication, account entitlement,
  agent loop and tools. Longhouse launches it through a control path and does
  not vendor, pin, proxy or update it.
- **Wall** - live overview of current sessions across machines. It is a query
  (`GET /api/agents/sessions/wall`) and the browser view over it, not a CLI command.
- **Recall** - semantic and full-text retrieval over past sessions.
- **Tail** - stream a session's recent events.
- **Peers** - other machines and agents reporting into the same Runtime Host.
- **Runner** - optional WebSocket command executor on a user-owned machine.
- **Provider fact** - something the provider's own terminal shows that sits on a
  transcript line the transcript view never renders: turn duration, away recap,
  the provider's session title, model and context usage. The Machine Agent
  parses these beside the transcript and ships them with the bytes that carry
  them. The Runtime Host keeps one row per source line and projects them as
  `turn_end`, `last_turn`, `recap` and `usage_latest`. Each provider's signals
  are declared under `transcript_signals` in `schemas/managed_providers.yml`.
  `schemas/transcript_shapes/` is the append-only catalog of every transcript
  line shape seen per provider version, and `scripts/qa/transcript_census.py`
  fails on a shape nobody has classified.

## Code map

Every backticked path in this section must exist; `make validate` checks them
(`scripts/ci/check-codemap-paths.py`). Update the map when you move code.

### Top level

| Path | What |
| --- | --- |
| `server/` | Python Runtime Host: FastAPI API, `longhouse-server` CLI, SQLite state, QA producers |
| `web/` | React/TypeScript UI bundled into the Runtime Host, plus the iOS transcript document source |
| `engine/` | Rust Machine Agent (`longhouse-engine`) and the native `longhouse` CLI (`engine/src/longhouse.rs`) |
| `ios/` | SwiftUI iOS app, widget and Live Activity (read/steer client) |
| `desktop/` | macOS Desktop App menu bar (Swift package `desktop/LonghouseMenuBarHarness/`) |
| `runner/` | TypeScript optional WebSocket command executor |
| `schemas/` | Source-of-truth contracts: `schemas/managed_providers.yml`, `schemas/session_state_contract.yml`, WS protocol, transcript shapes |
| `config/` | Checked-in config: `config/models.json`, `config/tool-tiers.json`, `config/provider-brands.json`, fixtures |
| `tests/fixtures/` | Cross-client fixtures that server, web and iOS tests all read |
| `e2e/` | Playwright end-to-end suites and probes |
| `scripts/` | Make-target entrypoints: build, ci, dev, generate, ops, qa, release, ui |
| `docs/` | Public docs, runbooks, contracts, `docs/generated/` artifacts |
| `docker/` | Runtime and test images, local observability stack |
| `video/` | Remotion product videos |
| `eval/` | Recall evaluation harness |
| `labs/` | Opt-in experiments (see `labs/README.md`) |
| `blog/` | Static blog pages |
| `.agents/skills/` | Repo agent skills |
| `.github/` | Workflows, path filters, composite actions |

### Web (`web/src/`)

Features import across folders with `@/`; within a folder, relative.
`web/scripts/check-layout.mjs` keeps file-type roots from coming back.

| Path | What |
| --- | --- |
| `web/src/app/` | Entry (`main.tsx`), routes, `Layout.tsx`, error boundary, global styles and tokens |
| `web/src/features/timeline/` | Sessions page, rows (`SessionRow.tsx`), inbox model, recall panel, timeline stream hook |
| `web/src/features/session/` | Session detail page, `TimelinePane.tsx`, runtime strip, header state; the composer is `web/src/features/session/chat/SessionChat.tsx` |
| `web/src/features/launch/` | New-session pane (`/timeline/new`) and its one launch hook, model picker, provider sign-in |
| `web/src/features/machines/` | Machines page and a machine's page (`MachinesPage.tsx`, `MachineDetailPage.tsx`, status words in `machinePresentation.ts`, served by `/api/timeline/machines/summary` from `server/zerg/services/machines_summary.py`), Devices page, connect-machine flow |
| `web/src/features/runners/` | Runner detail page (optional shell-command tooling, shown under its machine) and the connect/add-runner modal |
| `web/src/features/auth/` | Login, profile, settings, auth and token refresh |
| `web/src/features/marketing/` | Landing, hero demo, remote scene, blog, docs, legal, share |
| `web/scripts/prerender.mjs` | Build step after `vite build`: renders every sitemap route (`web/src/app/prerender.tsx`) to static HTML with its own title and meta tags; the browser hydrates it (`web/src/app/main.tsx`). `server/zerg/frontend_pages.py` serves those pages on the public site only |
| `web/src/shared/api/` | HTTP client and typed endpoints (`agents.ts`, `useAgentSessions.ts`) |
| `web/src/shared/session/` | Session facts used by several features: status (`sessionStatus.ts`), activity freshness, labels; `web/src/shared/session/model/` is the transcript model |
| `web/src/shared/instruments/` | Status lamp, sparkline, Nixie, Hearth |
| `web/src/shared/ui/` | Buttons, cards, dialogs and other primitives |
| `web/src/shared/hooks/` | Generic React hooks |
| `web/src/shared/lib/` | Config, dates, logging, provider metadata |
| `web/src/shared/test/` | Vitest setup and fact builders |
| `web/src/embeds/ios-transcript/` | Source of the iOS transcript document (TypeScript and CSS) |
| `web/src/generated/` | Generated types (see below) |
| `web/src/dev/` | Live-status lab (`web/live-status-lab.html`) |

### iOS (`ios/`)

| Path | What |
| --- | --- |
| `ios/Sources/LonghouseApp/App/` | App entry, root view, push notifications, Live Activity manager |
| `ios/Sources/LonghouseApp/Inbox/` | Timeline list, rows, connectivity, `TimelineViewModel.swift` |
| `ios/Sources/LonghouseApp/Session/` | `SessionView.swift`, `SessionViewModel.swift`; subfolders Transcript, Runtime (dock, signal field), Composer |
| `ios/Sources/LonghouseApp/Launch/` | Launch sheet and its subviews |
| `ios/Sources/LonghouseApp/Auth/` | Login |
| `ios/Sources/LonghouseApp/Settings/` | Settings |
| `ios/Sources/LonghouseApp/Diagnostics/` | Bug reports, client diagnostics, stall monitor |
| `ios/Sources/LonghouseApp/Previews/` | SwiftUI previews rendered by `make ios-previews` |
| `ios/Sources/LonghouseApp/Fixtures/` | UI-test fixture screens, including the transcript benchmark run |
| `ios/Sources/LonghouseApp/DesignSystem/` | Ember chrome |
| `ios/Sources/Shared/API/` | `LonghouseAPI.swift` and DTO adapters |
| `ios/Sources/Shared/Models/` | Session DTOs, one file per family (`SessionStateFacts.swift`, `SessionSummary.swift`, `SessionDetail.swift`, `SessionEvent.swift`, `SessionInputModels.swift`, ...), `TimelineSignal.swift`, runtime copy, subagents |
| `ios/Sources/Shared/Streams/` | SSE reader, timeline and workspace streams |
| `ios/Sources/Shared/Transcript/` | Lite-body state and date parsing. Transcript rows are not built in Swift: the WebKit document runs the shared `presentTranscript` over the served events |
| `ios/Sources/Shared/Auth/` | Hosted auth flow, keychain, shared auth store |
| `ios/Sources/Shared/Persistence/` | Timeline cache, transcript snapshots, pending inputs |
| `ios/Sources/Shared/LiveActivity/` | Live Activity and widget models |
| `ios/Sources/Shared/Design/` | Ember palette, provider glyphs |
| `ios/Sources/Shared/Support/` | Build identity, image compression, test hooks |
| `ios/Sources/Shared/Generated/` | Generated Swift (see below) |
| `ios/Sources/LonghouseWidget/` | Home-screen widget |
| `ios/Resources/Transcript/` | Built transcript document (generated) |
| `ios/Tests/LonghouseIOSTests/` | Unit tests, mirroring the source folders |
| `ios/Tests/LonghouseIOSUITests/` | UI tests |
| `ios/Tests/LonghouseChatStressUITests/` | Transcript stress and renderer benchmark |
| `ios/XcodeHarness/` | Xcode project spec (`project.yml`), plist, entitlements, signing configs |
| `ios/testflight/` | Tester-facing TestFlight text and review notes (`beta.toml`); shipped by `.github/workflows/ios-testflight.yml` via `ios/scripts/testflight-build.sh` and `scripts/ops/testflight.py` |

### Server (`server/zerg/`)

Python modules stay flat by design; services group by file prefix.

| Path | What |
| --- | --- |
| `server/zerg/routers/` | Every `/api` route (mounted on `api_app`) |
| `server/zerg/services/session_*.py` | Session state, listing, inputs, launch lifecycle, titles, turns, resume, observations |
| `server/zerg/services/provider_*.py` | Provider capability proofs, projections, sign-in, support state |
| `server/zerg/services/managed_*.py` | Managed (Helm/Console) local control, launcher, transport, contracts |
| `server/zerg/services/live_*.py` | Live catalog projections, session dispatch and inputs, launch readiness |
| `server/zerg/services/runner_*.py` | Runner connections, dispatch, health |
| `server/zerg/services/archive_*.py` | Transcript archive store, backlog, verifiers |
| `server/zerg/services/storage_v2_*.py` | Storage-v2 export, semantics, workspace |
| `server/zerg/services/machine_*.py`, `server/zerg/services/cursor_*.py` | Machine control channel and identity; Cursor hooks and transcript |
| `server/zerg/services/local_health/` | Local-health classifier, per-provider liveness, engine status |
| `server/zerg/services/agents/` | Agent kernel writes, backfills, identity resolution |
| `server/zerg/services/session_processing/` | Pure event processing: summaries, tokens, embeddings |
| `server/zerg/services/shipper/` | Python JSONL parser and hook helpers beside the Rust shipper |
| `server/zerg/catalogd/` | Single-writer catalog daemon; served session facts come from its snapshots |
| `server/zerg/catalogd/store_mixins/` | `CatalogStore` methods by responsibility (accounts, machines, interactions, launch, inputs, sessions, ingest, storage, projectors, legacy); helpers stay in `server/zerg/catalogd/store.py`, the only import path |
| `server/zerg/searchd/` | Search daemon |
| `server/zerg/storage_v2/` | Raw and render object store |
| `server/zerg/models/` | SQLAlchemy models (`AgentsBase` for agent infrastructure) |
| `server/zerg/schemas/` | Pydantic request/response models |
| `server/zerg/cli/` | `longhouse-server` commands |
| `server/zerg/qa/` | Live QA producers and oracles; every producer is built in `producer_shell.py` (the command line, the result envelope, the typed failure), and what lifecycle producers share is in `live_kit.py` |
| `server/zerg/auth/`, `server/zerg/middleware/`, `server/zerg/websocket/` | Auth, middleware, WebSocket |
| `server/tests_lite/` | Backend tests (`make test`) |

### Ship and release (`scripts/`)

| Path | What |
| --- | --- |
| `scripts/ops/dogfood_sensitive_paths.py`, `scripts/ops/dogfood-sensitive-paths.toml` | Sensitive-path classifier: a range matching this path list takes the Hosted Live QA gate before dogfood; also names the live-preview paths that need a Terminal Truth verdict |
| `scripts/ops/check-push-readiness.sh` | Pre-push check: stale duplicate commits on main and the verifier-boundary ratchet |
| `scripts/ci/verifier_boundary.py`, `scripts/ci/verifier-boundary.allow` | Import boundary between the verifier (`server/zerg/qa/`) and the code it judges; the allowlist only shrinks; computes the verifier digest |
| `scripts/ops/ship.sh`, `scripts/ops/ship-monitor.py` | `make ship` and `make ship-watch`: push an exact SHA, wait for its workflows |
| `.github/workflows/release.yml`, `scripts/ops/release_plan.py` | The one deploy path: a green CI run on main is built once into the runtime image, archived, deployed to the canary and (sensitive ranges) Hosted Live QA, then to dogfood; `release_plan.py` holds the gates (green CI, never backwards, no-op when no runtime input changed, QA for sensitive paths, Terminal Truth for live-preview paths) |
| `scripts/ops/install-precommit-autofix.sh` | Installs the self-staging pre-commit shims and retires only its own obsolete pre-push hook |
| `scripts/ops/release.sh`, `scripts/ops/release-artifacts.py`, `scripts/ops/launch-readiness.py` | `make release`: bump, validate, push, exact-SHA gates, GitHub release, notarization checks |
| `scripts/ops/remote.sh`, `scripts/ops/bench.sh` | Remote executors: heavy Docker-dispatched goals on the `ci` VM, iOS build and simulator work on the bench Mac |
| `scripts/ci/perf-proof.py`, `scripts/ci/perf-soak.py`, `scripts/ci/perf_soak_fixtures.py`, `.github/workflows/provider-nightly.yml` (`perf` job, Tuesdays) | Weekly perf lane: throughput proof, then a steady-state soak of the engine; fails on a resource-budget breach or a regression against recent runs |
| `scripts/ops/testflight.py`, `scripts/ops/testflight-ship.sh` | `make testflight` and `make testflight-status` (workflow `.github/workflows/ios-testflight.yml`) |

### Generated files

Never edit these by hand. `make validate` fails when one is stale.

| Output | Source | Written by |
| --- | --- | --- |
| `web/src/generated/openapi-types.ts`, `ios/Sources/Shared/Generated/SessionAPI.generated.swift` | server routes and models | `make generate-sdk` (`server/scripts/export_openapi.py`, `scripts/generate/ios_api_models.py`) |
| `web/src/generated/ws-messages.ts`, `server/zerg/generated/ws_messages.py` | `schemas/ws-protocol-asyncapi.yml` | `make regen-ws` (`scripts/generate/generate-ws-types-modern.py`) |
| `web/src/generated/provider-brands.ts`, `server/zerg/generated/provider_brands.py`, `ios/Sources/Shared/Generated/ProviderBrands.generated.swift`, `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/ProviderBrands.generated.swift` | `config/provider-brands.json`, `schemas/managed_providers.yml` (managed provider ids) | `make generate-provider-brands` (`scripts/generate/provider_brands.py`) |
| `web/src/generated/provider-capabilities.ts` | `schemas/managed_providers.yml` | `make generate-provider-capabilities` (`scripts/generate/provider_capabilities_ts.py`) |
| `web/src/shared/session/model/toolTiers.generated.ts`, `ios/Sources/Shared/Generated/ToolTiers.generated.swift` | `config/tool-tiers.json` | `scripts/generate/tool_tiers.py` (`make validate-tool-tiers` checks) |
| `server/zerg/config/managed_provider_contracts.json` | `schemas/managed_providers.yml` | `scripts/generate/generate_managed_provider_contracts.py --write` |
| `engine/src/managed_provider_contracts.generated.json` (engine copy, no source digests) | `schemas/managed_providers.yml` | `scripts/generate/generate_managed_provider_contracts.py --write` |
| `engine/src/managed_phase_contract.rs`, `web/src/generated/presence-states.ts` | `server/zerg/config/managed_phase_contract.json` | `make generate-phase-contract` |
| `engine/src/managed_identity_contract.rs` | `schemas/managed_providers.yml` | `make generate-managed-identity` |
| `docs/generated/provider_census.json` | tracked source files | `make generate-provider-census` |
| `ios/Resources/Transcript/transcript.html` | `web/src/embeds/ios-transcript/` | `make generate-ios-transcript` (`web/scripts/build-ios-transcript.mjs`) |
| `web/src/features/marketing/remote-scene/generated/` | recorded scene | `web/scripts/generate-remote-scene.ts` |

`scripts/generate/generate_session_state_contract.py` generates nothing; it
checks that the session-state contract version is the same in the schema, the
Python projector, OpenAPI and both clients.

### Where to look for X

| Topic | Server | Web | iOS |
| --- | --- | --- | --- |
| Status wording ("Thinking", "Using Bash") | `_primary` in `server/zerg/services/session_state_contract.py` authors it; `server/zerg/services/session_state_facts_projector.py` serves it | `web/src/shared/session/sessionStatus.ts` (freshness gate, served label) | `ios/Sources/Shared/Models/TimelineSignal.swift`, `ios/Sources/Shared/Models/SessionRuntimeCopy.swift`, `ios/Sources/LonghouseApp/Session/Runtime/SessionRuntimeDock.swift`, `ios/Sources/Shared/LiveActivity/` |
| Transcript rendering | `server/zerg/services/transcript_content.py`, `server/zerg/services/tool_presentation.py` | `web/src/features/session/TimelinePane.tsx` over `web/src/shared/session/model/timelineModel.ts` | `ios/Sources/LonghouseApp/Session/Transcript/WebTranscriptView.swift` sends served events (payload in `WebTranscriptPayloadBuilder.swift`, bridge and scroll in `WebTranscriptCoordinator.swift`) to the document from `web/src/embeds/ios-transcript/`, which groups them with `web/src/shared/session/model/presentTranscript.ts` |
| Session titles | `server/zerg/services/title_generator.py`, `server/zerg/provider_contract/session_title.py`, `server/zerg/services/storage_session_titles.py`; engine `engine/src/state/session_title.rs` | `web/src/shared/session/sessionLabels.ts` | `ios/Sources/Shared/Models/SessionSummary.swift` (`driftTitle`), `ios/Sources/Shared/Models/SessionDetail.swift` (`displayTitle`) |
| Model/context line ("opus 5 · high · 501k ctx") | `usage_label` in `server/zerg/services/session_provider_facts.py` authors `usage_latest.label` from the newest `turn.usage` fact; clients render it verbatim | header identity line in `web/src/features/session/SessionDetailPage.tsx`; `ReadoutRail.tsx` draws the fill bar only when a window is reported | `SessionUsageChip` in `ios/Sources/LonghouseApp/Session/SessionView.swift` |
| Timeline stream | `server/zerg/services/timeline_session_stream.py`, `server/zerg/routers/timeline.py` | `web/src/features/timeline/useTimelineSessionStream.ts` | `ios/Sources/Shared/Streams/TimelineSessionsStream.swift` |
| Launch | `server/zerg/services/session_launch_lifecycle.py`, `server/zerg/services/managed_local_launcher.py`; engine `engine/src/managed_launch_lifecycle.rs` | `web/src/features/launch/` | `ios/Sources/LonghouseApp/Launch/` |
| Hearth (row flame) | none: reads served facts | `web/src/shared/instruments/hearth/` (`signals.ts` maps facts to flame) | `ios/Sources/LonghouseApp/DesignSystem/EmberChrome.swift` (background light only) |
| Provider support | `schemas/managed_providers.yml` | `web/src/generated/provider-capabilities.ts` | none |
| Plaintext-http rule (which Runtime Host addresses native clients may reach without https: loopback, Tailscale, an opted-in LAN address) | `server/zerg/services/plaintext_http.py`; engine `engine/src/plaintext_http.rs`; Desktop `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/PlaintextHTTP.swift` (byte-identical to the iOS copy); shared cases `schemas/plaintext-http-vectors.json` | none | `ios/Sources/Shared/Auth/PlaintextHTTP.swift` |
| Import scope (which local history a Machine Agent may ship) and uninstall | `server/zerg/cli/onboard.py` asks what to import; `server/zerg/routers/device_tokens.py` revokes a device token; engine `engine/src/import_scope.rs` and `engine/src/machine_scope.rs` enforce the scope, `engine/src/machine_uninstall.rs` implements `longhouse uninstall` | `web/src/features/machines/` (revoke a machine) | none |

## Read next

- [`VISION.md`](VISION.md): product thesis and invariants
- [`docs/README.md`](docs/README.md): public docs and runbooks
- [`docs/contracts/truth-plane.md`](docs/contracts/truth-plane.md): truth-plane contract
- [`server/README.md`](server/README.md), [`engine/README.md`](engine/README.md), [`runner/README.md`](runner/README.md): component detail
