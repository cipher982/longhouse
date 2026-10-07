# Architecture

A short map of how Longhouse fits together and what its nouns mean. For the
product thesis and invariants, read [`VISION.md`](VISION.md).

## The two components

Longhouse is one product with two public components:

- **Machine Agent** — a Rust engine (`longhouse-engine`) that runs on each
  machine where you do work. It drains the hook output that provider CLIs
  write, ships session events to the Runtime Host with retry/spool, and emits
  heartbeats. This is the shipping path.
- **Runtime Host** — the backend product: a FastAPI API, the bundled web UI,
  and SQLite-backed state. It is what `longhouse-server serve` runs. It lives where
  durability should live.

On a laptop both run together so you can try it out, but the runtime stops when
the laptop sleeps. For durability you run the Runtime Host on an always-on box
(VPS, homelab, Mac mini) and point your dev machines' Machine Agents at it.

```
  dev laptop ─┐
              ├─ Machine Agent ◀─events / commands─▶ Runtime Host ◀─▶ web / CLI / iOS
  dev box ────┘                                  (SQLite, durable)
       │
       └─ installed provider clients own execution, auth, tools, and billing
```

## Core principles

- **`/api/agents/*` is the canonical machine surface.** The browser, CLI, MCP,
  and iOS all sit on top of the same primitives — none is a separate source of
  truth.
- **SQLite is the only core database requirement.** Hosted account, billing,
  and provisioning state lives outside this repo.
- **One session, one execution owner.** A session runs somewhere real;
  Longhouse observes or controls it but never silently moves it.
- **Normalize control, not provider execution.** Longhouse exposes a common
  session and capability model while the installed provider client retains its
  own agent loop, authentication, subscription or API billing, and tools.
- **Capability over type.** Every item in the timeline is a session. Some have
  live control, some need host reattach, some are search-only. Rely on
  `session.capabilities`, not a session "species".
- **Separate realtime truth from durable archive.** A live lane answers "what
  is happening right now" and must feel terminal-fast; a durable lane answers
  "what provably happened" and must be correct, ordered, and replayable.
- **Keep observations out of control admission.** Replaceable runtime events
  and presence have their own per-device quota. Launch and input use control
  admission; heartbeat and machine presence use independent liveness admission.
  Validated batches carrying bindings, interactions, wakes, or execution ends
  use lifecycle admission and cooldowns, so telemetry cannot delay them.
  Malformed runtime bodies spend observation quota, never lifecycle quota.

OMP's launch-scoped coordination authority belongs to the main managed session.
Native subagents cannot use it to send as their parent; they report blockers and
results through OMP's native parent channel. The extension checks the calling
agent context before making a coordination request.
Child lifecycle events leave the parent's turn identity and settlement unchanged.
Child switch/branch operations stay provider-owned and bypass the managed
parent's transition handshake.

## iOS session opening

The iOS session route schedules opening as two client lanes over the same
server-owned facts:

- **Primary lane** fetches compact session identity and native controls first,
  so the title, runtime state, and composer do not wait for transcript rendering.
- **Transcript lane** hydrates the recent tail, reconciles any cached snapshot,
  and renders the body in WebKit. A cached snapshot is instant paint only; the
  first accepted server tail remains authoritative.

The timeline warms an idle WebKit spare before navigation. Route transitions
keep the title and trailing action slot bounded while the transcript lane is
loading. This is scheduling for responsiveness, not a second source of truth:
server facts, transcript watermarks, and stale-result fences remain canonical.

## Session modes

Every session has exactly one mode. This vocabulary is canonical here and in
the workspace `AGENTS.md`; nowhere else in this repo should redefine it —
only link to this section.

- **Shadow** — unmanaged, observe-only. The Machine Agent discovered a
  session it did not launch (e.g. a bare `claude` run). Searchable and
  sometimes partially live, but not steerable — Longhouse never owned its
  control path.
- **Helm** — managed, interactive, remote-steerable. A human starts Helm from
  a physical terminal with `longhouse claude`, `longhouse codex`, or another
  supported provider wrapper. Longhouse preserves the provider's normal TUI
  and owns its control path. Once that terminal-originated session exists,
  web/iOS may steer it remotely; web/iOS never originates a headless Helm
  session. The provider process may persist while the interactive TUI remains
  open.
- **Console** — managed, headless, UI-dispatched. Web/iOS sends one-shot work
  through the Machine Agent; the provider process runs one turn and exits,
  state persists to disk, and nothing stays resident between turns.

`Managed session` / `Unmanaged session` (below) remain valid at the
control-ownership level: Unmanaged == Shadow; Managed == Helm or Console.
Both vocabularies are correct — they answer different questions (can
Longhouse steer this at all, vs. exactly how).

### Mapping to code

Read `schemas/managed_providers.yml` for the current, authoritative provider
support declarations — they change independently of this document and this
document does not restate them. The product terms above and the
manifest/engine field names below refer to the same three modes:

| Product term | Manifest/code fields |
| --- | --- |
| Shadow | discovered/unmanaged import; no `launch_*` capability involved |
| Helm | `launch_local`, plus adapter-proven live controls such as `steer_active_turn` and `send_input` |
| Console | `turn_start` (current); `run_once` (legacy, being retired) |

`launch_remote` / provider-facing `session.launch` was removed. Persistent
no-terminal processes are not a fourth mode or a valid Helm launch surface.

## Glossary

The project uses some shorthand nouns. The important ones:

- **Provider CLI** — an upstream binary you install yourself (`claude`, `codex`,
  Antigravity, `opencode`). It retains its native authentication, account
  entitlement, agent loop, and tools. Longhouse launches it through a control
  path but does not vendor, pin, proxy, or update it.
- **Wall** — a live overview of current sessions across your machines. It is a
  query (`GET /api/agents/sessions/wall`) and the browser view over it, not a
  CLI command.
- **Recall** — semantic/full-text retrieval over past session history.
- **Tail** — stream the recent events of a session.
- **Peers** — other machines/agents reporting into the same Runtime Host.
- **Runner** — an optional WebSocket command executor for remote execution on
  a user-owned machine.
- **Provider fact** — something the provider's own terminal shows that lives on
  a transcript line the transcript view never renders: a turn's duration, an
  away recap, the provider's own session title, model and context usage. The
  Machine Agent parses these beside the transcript and ships them with the
  bytes that carry them; the Runtime Host keeps one row per source line and
  projects them as `turn_end`, `last_turn`, `recap`, and `usage_latest`. Which
  signals each provider writes, and how proven each is, is declared per
  provider under `transcript_signals` in `schemas/managed_providers.yml`;
  `schemas/transcript_shapes/` is the append-only catalog of every transcript
  line shape seen per provider version, and `scripts/qa/transcript_census.py`
  fails on a shape nobody has classified.

## Code map

Where things live. Every backticked path in this section must exist;
`make validate` checks them (`scripts/ci/check-codemap-paths.py`).

### Top level

| Path | What |
| --- | --- |
| `server/` | Python Runtime Host: FastAPI API, `longhouse-server` CLI, SQLite state, QA producers |
| `web/` | React/TypeScript UI bundled into the Runtime Host, plus the iOS transcript document source |
| `engine/` | Rust Machine Agent (`longhouse-engine`) and the native `longhouse` CLI (`engine/src/longhouse.rs`) |
| `ios/` | SwiftUI iOS app, widget and Live Activity (read/steer client) |
| `desktop/` | macOS Desktop App menu bar (Swift package `desktop/LonghouseMenuBarHarness/`) |
| `runner/` | TypeScript optional WebSocket command executor |
| `schemas/` | Source-of-truth contracts: `schemas/managed_providers.yml`, `schemas/session_state_contract.yml`, WS protocol, transcript shapes; `schemas/factory_producers.yml` mirrors the private provider factory's producer registry, and `make validate-factory-registration` fails a release-gate row it has no producer for |
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
| `web/scripts/prerender.mjs` | Build step after `vite build`: renders every sitemap route (`web/src/app/prerender.tsx`) to static HTML under `_prerender` in the build output, each with its own title, description, canonical and OpenGraph tags, so crawlers and link unfurlers get content; the browser hydrates it (`web/src/app/main.tsx`). A lazy route's chunk and CSS (the docs, `web/src/features/marketing/docs/DocsRoutes.tsx`) are linked into that route's page from Vite's build manifest and loaded before hydrating. `server/zerg/frontend_pages.py` serves those pages on the public (demo) site only |
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
| `ios/Sources/Shared/Transcript/` | `TimelineBuilder.swift` (turns events into transcript rows), final answer, edit summary |
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
| `server/zerg/searchd/` | Search daemon |
| `server/zerg/storage_v2/` | Raw and render object store |
| `server/zerg/models/` | SQLAlchemy models (`AgentsBase` for agent infrastructure) |
| `server/zerg/schemas/` | Pydantic request/response models |
| `server/zerg/cli/` | `longhouse-server` commands |
| `server/zerg/mcp_server/` | MCP tools (search, recall) for agents over stdio |
| `server/zerg/qa/` | Live QA producers and oracles |
| `server/zerg/auth/`, `server/zerg/middleware/`, `server/zerg/websocket/` | Auth, middleware, WebSocket |
| `server/tests_lite/` | Backend tests (`make test`) |

### Ship, promotion and review (`scripts/`)

| Path | What |
| --- | --- |
| `scripts/ops/review_gate.py`, `scripts/ops/review-policy.toml`, `scripts/lib/review-gate.sh` | Review landing rule: a commit on the blocking list needs a completed `hatch review` receipt. Asked by `make check-push-readiness`, `make ship`, `release.sh`, the pre-push hook (`scripts/ops/install-push-gate.sh`) and both promotions |
| `scripts/ops/check-push-readiness.sh` | Pre-push check: stale duplicate commits on main, the review rule, the verifier-boundary ratchet |
| `scripts/ci/verifier_boundary.py`, `scripts/ci/verifier-boundary.allow` | Import boundary between the provider factory's verifier (`server/zerg/qa/`) and the code it judges; the allowlist only shrinks; computes the verifier digest |
| `scripts/ops/ship.sh`, `scripts/ops/ship-monitor.py` | `make ship` and `make ship-watch`: push an exact SHA, wait for its workflows |
| `scripts/ops/promote-dogfood.sh`, `scripts/ops/promote-production.sh`, `scripts/ops/promotion_gates.py` | Ring promotions; production promotes the image dogfood serves once its four gates pass and writes the receipt |
| `scripts/ops/release.sh`, `scripts/ops/release-artifacts.py`, `scripts/ops/launch-readiness.py` | `make release`: bump, validate, push, exact-SHA gates, GitHub release, notarization checks |
| `scripts/ops/crunch.sh`, `scripts/ops/bench.sh` | Remote executors: heavy Docker-dispatched goals on the crunch VM, iOS build and simulator work on the bench Mac |
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
| `engine/src/managed_phase_contract.rs`, `web/src/generated/presence-states.ts` | `server/zerg/config/managed_phase_contract.json` | `make generate-phase-contract` |
| `engine/src/managed_identity_contract.rs` | `schemas/managed_providers.yml` | `make generate-managed-identity` |
| `docs/generated/provider_census.json` | tracked source files | `make generate-provider-census` |
| `docs/generated/provider_factory_plan.json`, `docs/generated/provider_factory_status_tables.md` | provider schema and proofs | `make generate-provider-factory-plan` |
| `ios/Resources/Transcript/transcript.html` | `web/src/embeds/ios-transcript/` | `make generate-ios-transcript` (`web/scripts/build-ios-transcript.mjs`) |
| `web/src/features/marketing/remote-scene/generated/` | recorded scene | `web/scripts/generate-remote-scene.ts` |

`scripts/generate/generate_session_state_contract.py` generates nothing; it
checks that the session-state contract version is the same in the schema, the
Python projector, OpenAPI and both clients.

### Where to look for X

| Topic | Server | Web | iOS |
| --- | --- | --- | --- |
| Status wording ("Thinking", "Using Bash") | `_primary` in `server/zerg/services/session_state_contract.py` authors it; `server/zerg/services/session_state_facts_projector.py` serves it | `web/src/shared/session/sessionStatus.ts` (freshness gate, served label) | `ios/Sources/Shared/Models/TimelineSignal.swift`, `ios/Sources/Shared/Models/SessionRuntimeCopy.swift`, `ios/Sources/LonghouseApp/Session/Runtime/SessionRuntimeDock.swift`, `ios/Sources/Shared/LiveActivity/` |
| Transcript rendering | `server/zerg/services/transcript_content.py`, `server/zerg/services/tool_presentation.py` | `web/src/features/session/TimelinePane.tsx` over `web/src/shared/session/model/timelineModel.ts` | `ios/Sources/Shared/Transcript/TimelineBuilder.swift` builds the payload; `ios/Sources/LonghouseApp/Session/Transcript/WebTranscriptView.swift` renders it (payload in `WebTranscriptPayloadBuilder.swift`, bridge and scroll in `WebTranscriptCoordinator.swift`) in the document from `web/src/embeds/ios-transcript/` |
| Session titles | `server/zerg/services/title_generator.py`, `server/zerg/services/session_title.py`, `server/zerg/services/storage_session_titles.py`; engine `engine/src/state/session_title.rs` | `web/src/shared/session/sessionLabels.ts` | `ios/Sources/Shared/Models/SessionSummary.swift` (`driftTitle`), `ios/Sources/Shared/Models/SessionDetail.swift` (`displayTitle`) |
| Model/context line ("opus 5 · high · 501k ctx") | `usage_label` in `server/zerg/services/session_provider_facts.py` authors `usage_latest.label` from the newest `turn.usage` fact; clients render it verbatim | header identity line in `web/src/features/session/SessionDetailPage.tsx`; `ReadoutRail.tsx` draws the fill bar only when a window is reported | `SessionUsageChip` in `ios/Sources/LonghouseApp/Session/SessionView.swift` |
| Timeline stream | `server/zerg/services/timeline_session_stream.py`, `server/zerg/routers/timeline.py` | `web/src/features/timeline/useTimelineSessionStream.ts` | `ios/Sources/Shared/Streams/TimelineSessionsStream.swift` |
| Launch | `server/zerg/services/session_launch_lifecycle.py`, `server/zerg/services/managed_local_launcher.py`; engine `engine/src/managed_launch_lifecycle.rs` | `web/src/features/launch/` | `ios/Sources/LonghouseApp/Launch/` |
| Hearth (row flame) | none: reads served facts | `web/src/shared/instruments/hearth/` (`signals.ts` maps facts to flame) | `ios/Sources/LonghouseApp/DesignSystem/EmberChrome.swift` (background light only) |
| Provider support | `schemas/managed_providers.yml` | `web/src/generated/provider-capabilities.ts` | none |
| Plaintext-http rule (which Runtime Host addresses native clients may reach without https: loopback, Tailscale, an opted-in LAN address) | `server/zerg/services/plaintext_http.py`; engine `engine/src/plaintext_http.rs`; Desktop `desktop/LonghouseMenuBarHarness/Sources/LonghouseMenuBarCore/PlaintextHTTP.swift` (byte-identical to the iOS copy); shared cases `schemas/plaintext-http-vectors.json` | none | `ios/Sources/Shared/Auth/PlaintextHTTP.swift` |
| Import scope (which local history a Machine Agent may ship) and uninstall | `server/zerg/cli/onboard.py` asks what to import; `server/zerg/routers/device_tokens.py` revokes a device token; engine `engine/src/import_scope.rs` and `engine/src/machine_scope.rs` enforce the scope, `engine/src/machine_uninstall.rs` implements `longhouse uninstall` | `web/src/features/machines/` (revoke a machine) | none |

## Where to read next

- [`VISION.md`](VISION.md) — product thesis and invariants (start here)
- [`docs/README.md`](docs/README.md) — public documentation and runbooks
- [`docs/contracts/truth-plane.md`](docs/contracts/truth-plane.md) — the public truth-plane contract
- [`server/README.md`](server/README.md) / [`runner/README.md`](runner/README.md) — component detail
