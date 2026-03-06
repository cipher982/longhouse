# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

Classification tags: [Launch], [Product], [Infra], [QA/Test], [Docs/Drift], [Tech Debt]

---

## What's Next (Priority Order)

## [Docs/Drift] Docs retention prune (size: 3)

Status (2026-03-06): In progress.

**Goal:** Cut the repo doc surface by about 80% so only canonical docs remain in-tree.

- [ ] Commit a retention policy spec with explicit keep/delete rules
- [ ] Delete transient and historical docs (handoffs, plans, reports, completed specs)
- [ ] Delete duplicated operator docs after folding any tiny must-keep guidance into canonical docs
- [ ] Verify the final markdown set is roughly 8-10 files and references are updated

Notes:
- 2026-03-06: Source of truth after this prune should be README/VISION/AGENTS/TODO plus a tiny set of app-contract docs. Git history is the archive.

## [Tech Debt] Provision live script helper dedupe (size: 1)

Status (2026-03-06): Done.

**Goal:** Make `scripts/provision-e2e-live.sh` use the same hosted helper contract as smoke/E2E instead of rebuilding control-plane and SSO logic again.

- [x] Source `scripts/lib/hosted-instance.sh` in the live provisioning script and normalize admin-token/control-plane URL setup
- [x] Stop deriving the instance URL from `ROOT_DOMAIN` when the control-plane create response already returns a canonical `url`
- [x] Reuse the hosted helper for login-token and deprovision actions

Notes:
- 2026-03-06: This is the last obvious hosted-ops drift script after smoke, prod E2E, CI env export, and qa-live were unified.
- 2026-03-06: `provision-e2e-live.sh` now reads the canonical instance URL from the create response, normalizes `CONTROL_PLANE_ADMIN_TOKEN`, uses the hosted helper for login-token + accept-token + deprovision, and passed a full live create/health/SSO/cleanup run against production control plane.

## [QA/Test] Runner detail metadata typing cleanup (size: 1)

Status (2026-03-06): Done.

**Goal:** Stop treating `runner_metadata` as a typed object in the UI when the OpenAPI contract exposes it as `Record<string, unknown>`.

- [x] Add one local metadata normalizer in `RunnerDetailPage` instead of indexing `unknown` fields directly
- [x] Re-run frontend type validation after the generated OpenAPI regen

Notes:
- 2026-03-06: This surfaced while regenerating OpenAPI types for the hosted alias cleanup. It is a real UI type hole, not fallout from the docstring-only changes.
- 2026-03-06: Added a local `normalizeRunnerMetadata()` helper in `RunnerDetailPage` so the UI stops indexing `Record<string, unknown>` as if it were a typed object. `bun run validate:types` then passed.

## [Docs/Drift] Hosted alias example cleanup (size: 1)

Status (2026-03-06): Done.

**Goal:** Stop teaching dead `david.longhouse.ai` examples in operator-facing code paths and generated API docs.

- [x] Update live CLI/docstring examples to use `api.longhouse.ai` or `{subdomain}.longhouse.ai`, whichever actually matches the surface
- [x] Regenerate frontend OpenAPI types after the backend docstring/source changes

Notes:
- 2026-03-06: This is not a historical-spec cleanup. The target is live help text, docstrings, and generated types that people actually read while operating the system.
- 2026-03-06: Updated live examples in `mcp_serve.py`, `shipper/hooks.py`, and auth route docstrings, fixed the broken `frontend-web` `generate:api` script by switching it to `bunx openapi-typescript`, and regenerated `src/generated/openapi-types.ts`.

## [Tech Debt] qa-live wrapper cleanup (size: 1)

Status (2026-03-06): Done.

**Goal:** Keep `qa-live.sh` as a thin alias to the prod E2E runner instead of a second command-line surface to maintain.

- [x] Drop the bespoke `qa-live.sh` flag parser and banner
- [x] Keep the useful env overrides (`QA_INSTANCE_SUBDOMAIN`, `QA_INSTANCE_URL`) and validate the default + direct-URL paths

Notes:
- 2026-03-06: `qa-live.sh` already delegates to `run-prod-e2e.sh`, so the extra parsing/printing layer was just more wrapper drift.
- 2026-03-06: `scripts/qa-live.sh` now stays a 32-line env shim over `run-prod-e2e.sh`. Validation passed with `make qa-live` (8/8) and a direct-URL override run using `SMOKE_LOGIN_TOKEN`.

## [Tech Debt] Hosted test target helper dedupe (size: 1)

Status (2026-03-06): Done.

**Goal:** Stop duplicating hosted target resolution and login-token setup across smoke, prod E2E, and CI helper scripts.

- [x] Move the shared control-plane URL + target resolution + login-token bootstrap into `scripts/lib/hosted-instance.sh`
- [x] Make `smoke-prod.sh`, `run-prod-e2e.sh`, and `export-hosted-instance-env.sh` use the shared helper

Notes:
- 2026-03-06: The same default-subdomain and URL/bootstrap logic had drifted across three scripts, which makes future auth changes land in too many places.
- 2026-03-06: `lh_hosted_prepare_target()` and `lh_hosted_resolved_login_token()` now own that bootstrap path. Validation passed through `./scripts/ci/export-hosted-instance-env.sh david010`, `make qa-live`, and `make verify-prod`.

## [QA/Test] Prod verify harness cleanup (size: 1)

Status (2026-03-06): Done.

**Goal:** Keep `make verify-prod` honest without tripping over local-only UUID casing or brittle text selectors.

- [x] Canonicalize smoke-test UUID generation so macOS `uuidgen` matches API UUID serialization
- [x] Tighten the live settings contract test selector so it asserts the heading instead of any matching text blob

Notes:
- 2026-03-06: `make verify-prod` passed smoke but the live browser phase failed on a strict-mode selector collision, not an actual API contract break.
- 2026-03-06: Fixed by centralizing lowercase UUID generation in `scripts/smoke-prod.sh` and switching the settings-page contract check to a role-based heading locator. `make verify-prod` then passed end to end (36 smoke + 21 live browser checks).

## [Tech Debt] Hosted follow-up simplifications (size: 4)

Status (2026-03-06): Done.

**Goal:** Keep deleting hosted-runtime drift now that auth and storage are standardized. Focus on removing one-off auth paths, fragile ops entrypoints, and repeated control-plane state shaping.

- [x] Make `qa-live` use hosted login tokens instead of scraping `LONGHOUSE_PASSWORD` from the live container
- [x] Make tenant GUID repair/admin tooling runnable without fake app env bootstrapping or hidden `/app/.venv` knowledge
- [x] Flatten remaining repeated `InstanceOut` / control-plane response shaping in `routers/instances.py`
- [x] Decide whether `Instance.data_path` should be derived or centrally wrapped instead of read ad hoc
- [x] Remove preview-env drift for infra apps so stale preview-only vars cannot quietly override prod assumptions
- [x] Consider moving the control-plane SQLite DB under `/var/app-data` too so mutable app state follows one rule
- [x] Add a repo-level way to assert/fix the control-plane Coolify env + storage contract without hand-editing Coolify internals

Notes:
- 2026-03-06: `scripts/control-plane-coolify-contract.sh` now asserts/fixes the live Coolify env + storage contract, migrated the control-plane SQLite DB to `/var/app-data/longhouse-control-plane`, and redeployed `longhouse-control-plane` successfully.
- 2026-03-06: Coolify recreates preview env rows for this app during deploy, so the invariant is now consistency rather than absence: preview values for the contract keys must match prod.
- 2026-03-06: Decision: keep persisted `Instance.data_path` for migration and host-move flexibility, but route fallback logic through `resolve_instance_data_path()` so router/deployer/provisioner stop open-coding it.
- 2026-03-06: `routers/instances.py` now builds `InstanceOut` through one `_instance_out()` helper instead of repeating the same response payload shape across the admin and self-service routes.
- 2026-03-06: `tenant_db_guid_repair.py` is now stdlib-only with an explicit GUID-column map, the CLI runs under plain `python3`, and a stripped-env subprocess regression test locks that in.
- 2026-03-06: `scripts/qa-live.sh` now delegates to `scripts/run-prod-e2e.sh`, uses the control-plane login-token flow, and live `make qa-live` passed 8/8 against `david010`.
- 2026-03-06: Immediate priority order is `qa-live` auth, maintenance-tool bootstrap simplification, then control-plane response/helper dedupe.
- 2026-03-06: Stale preview-only `CONTROL_PLANE_INSTANCE_DATA_ROOT` drift was already found and deleted live while landing the tenant-data-root cleanup, which is a signal to keep reducing hidden Coolify state.

## [Infra] Tenant data root cleanup + repair tooling (size: 3)

Status (2026-03-06): Done.

**Goal:** Make `/var/app-data/longhouse` the canonical hosted data root, ship an automated tenant GUID repair tool, remove the host compatibility bind mount, then use the cleanup to delete more drift.

- [x] Commit persistent spec for the cleanup sprint
- [x] Add one-shot tenant DB GUID scan/repair tooling
- [x] Canonicalize control-plane/runtime data root to `/var/app-data/longhouse`
- [x] Migrate persisted instance `data_path` rows and remove the host compatibility bind mount on `zerg`
- [x] Run full verification (`make test`, `make test-e2e`, deploy/reprovision, `make qa-live`)
- [x] Land three more simplifications focused on deleting drift/duplicate code

Notes:
- 2026-03-06: Live on `zerg`, `cp_instances.data_path` rows now point at `/var/app-data/longhouse/<subdomain>`, the control-plane Coolify app mounts `/var/app-data/longhouse` directly, and the old compatibility bind mount is gone.
- 2026-03-06: Verification passed end to end: `make test`, `make test-e2e`, live tenant-GUID scan, active-instance reprovision, post-unmount reprovision, and `make qa-live`.
- 2026-03-06: Additional simplifications landed: extracted shared control-plane recreate/deploy helpers, corrected the ship skill examples, and removed stale old-path references from docs/scripts.
- Spec: `docs/specs/tenant-data-root-cleanup.md`.

## [Tech Debt] Startup recovery should survive malformed legacy run UUIDs (size: 1)

Status (2026-03-06): Done.

**Goal:** Instance restart should recover orphaned runs even if legacy rows contain malformed UUID-like sentinel strings in unrelated columns.

- [x] Narrow startup run recovery to the fields it actually needs
- [x] Add a regression test for malformed `assistant_message_id` rows discovered on `david010`

Notes:
- 2026-03-06: Discovered during the zerg tenant-data bind-mount migration. `david010` had two legacy `runs.assistant_message_id` values (`live-voice-*`, `live-web-*`) that crashed startup recovery via eager ORM GUID parsing.
- 2026-03-06: Fixed in app code by querying only the scalar run fields needed for startup recovery, enforcing UUID-only `message_id` values before `run_oikos()` persists `assistant_message_id`, and updating the live voice SSE test to use a real UUID.


## [Infra] Hosted runtime simplification (control plane + auth + smoke) (size: 4)

Status (2026-03-06): Done.

**Goal:** Collapse hosted deploy/auth/smoke onto one control-plane-driven path for paid, dev, and CI instances while keeping OSS local simple and separate.

- [x] Finalize canonical hosted target model (`subdomain` + computed control-plane URL)
- [x] Switch hosted smoke auth to `login-token -> /api/auth/accept-token`
- [x] Replace hardcoded hosted URLs with control-plane target resolution
- [x] Unify deploy/verify helpers around reprovision + shared smoke entrypoint
- [x] Define rollout, cleanup, and acceptance criteria in a spec

Notes:
- 2026-03-06: Removed obsolete repo vars (`SMOKE_FRONTEND_URL`, `SMOKE_API_URL`) and deleted the legacy `SMOKE_TEST_SECRET` GitHub Actions secret after the new `smoke-after-deploy` workflow path passed live (run `22743293792`).
- Primary dev instance is `david010.longhouse.ai`; user instances are provisioner-managed, not Coolify-managed.
- 2026-03-05: Draft redesign spec landed in `docs/specs/hosted-runtime-simplification.md` with scope, non-goals, phased rollout, and acceptance criteria.
- 2026-03-05: Phase 1 landed: control-plane `InstanceOut` now includes canonical `url`, instance endpoints return it consistently, and `scripts/lib/hosted-instance.sh` centralizes hosted instance resolve/login-token/reprovision helpers.
- 2026-03-05: Phase 2 landed: `scripts/smoke-prod.sh` now resolves hosted targets via control-plane metadata when `INSTANCE_SUBDOMAIN` is set and authenticates via `login-token -> /api/auth/accept-token`; live login-token validation passed against `david010`, but the new control-plane `url` field still needs deployment before helper-based resolution works against prod.
- 2026-03-05: Phase 3 landed: `scripts/run-prod-e2e.sh` now mints/accepts hosted login tokens instead of requiring `SMOKE_TEST_SECRET`, the live Playwright fixtures exchange `SMOKE_LOGIN_TOKEN` through `/api/auth/accept-token`, and the hosted auth smoke spec passed live against `david010` (4/4).
- 2026-03-05: Phase 4 landed: CI now resolves hosted targets through `scripts/ci/export-hosted-instance-env.sh`, `smoke-after-deploy.yml` no longer depends on repo URL vars or `SMOKE_TEST_SECRET`, and `deploy-and-verify.yml` deploys marketing/control-plane via the real Coolify API token path before reprovisioning the hosted instance through the control plane.
- 2026-03-05: Phase 5 landed: `scripts/qa-live.sh` now targets hosted instances by subdomain instead of a fixed URL/container pair, and `scripts/migrate_from_lifehub.py` now derives its default Longhouse URL from `LONGHOUSE_SUBDOMAIN` instead of the dead `david.longhouse.ai` hostname.





## [Product] Oikos multi-surface messaging architecture (web + Telegram + future channels) (size: 5)

Status (2026-03-04): In progress (spec + rollout plan in progress).

**Goal:** Keep one canonical Oikos reasoning thread while giving each UI surface a clean, filtered conversation view and reliable cross-surface delivery semantics.

- [x] Finalize metadata contract on `ThreadMessage.message_metadata` (`origin`, `delivery`, `visibility`, idempotency key fields)
- [x] Implement surface-aware Oikos execution context (`source_surface_id`, `source_conversation_id`) from web + Telegram + voice callers
- [x] Add surface-aware history filtering (`/api/oikos/history`) with sane defaults (`web` only) and optional all-activity mode
- [x] Add inbound Telegram dedupe (idempotency on retried webhooks) before Oikos run spawn
- [x] Add per-user Oikos run serialization to avoid cross-surface races on the shared thread
- [x] Add browser UI surface badge + optional merged-view toggle
- [x] Harden control-plane reprovision so per-instance custom env vars persist (Telegram/OpenAI settings survive reprovision)

Notes:
- 2026-03-04: Existing Telegram bot bridge is live; current behavior still mixes Telegram + web turns in browser history due to missing surface filtering.
- 2026-03-04: Draft architecture spec landed in `docs/specs/oikos-multi-surface-messaging.md`.
- 2026-03-05: Phase A shipped: source-surface metadata persistence + `/api/oikos/history` surface filtering + web default (`surface_id=web`) with backend/frontend tests and full E2E pass.
- 2026-03-05: Phase B shipped: Telegram inbound idempotency-key dedupe (`telegram:{chat_id}:{update_id}` only, no message-id fallback), process-local per-owner run serialization in `OikosService.run_oikos()`, and fail-closed dedupe behavior on missing/invalid transport metadata.
- 2026-03-05: Phase C shipped: browser chat view toggle (`Web only` vs `All activity`) now reloads history with `view=all` when enabled and renders per-message surface badges (Telegram/Voice/System) from metadata.
- 2026-03-05: Control-plane env durability shipped: added `cp_instances.custom_env_json`, admin GET/PUT endpoints for per-instance env overrides, merge semantics in provision/reprovision/deploy flows (including null-to-unset support), and test coverage for persistence across reprovision + password regen.

## [Product] First-class Oikos surface adapter interface extraction (size: 4)

Status (2026-03-05): Done.

**Goal:** Define one modular adapter contract for inbound/outbound Oikos surfaces (web, telegram, voice, future channels) so new platform integrations plug in without touching core Oikos orchestration.

- [x] Finalize `SurfaceAdapter` contract (normalized ingress, owner resolution, idempotency contract, delivery contract, capabilities)
- [x] Introduce central surface orchestrator/gateway that owns dedupe + run-serialization + `run_oikos()` invocation
- [x] Move Telegram-specific Oikos bridging onto the shared surface adapter contract (keep transport plugin isolated)
- [x] Add adapter contract test harness (shared tests every adapter must pass)
- [x] Add end-to-end multi-surface behavior tests on orchestrator path (web + telegram + voice stubs)

Notes:
- 2026-03-05: Current code has transport plugin architecture (`zerg/channels/*`) plus a Telegram-specific Oikos bridge (`services/telegram_bridge.py`) and separate web/voice Oikos entrypoints.
- 2026-03-05: New spec target will consolidate Oikos-facing integration behind one surface adapter layer while reusing existing channel plugins for transport mechanics.
- 2026-03-05: Draft implementation spec landed at `docs/specs/oikos-surface-adapter-interface.md` (first-principles invariants, adapter contract, orchestrator contract, dedupe ledger, rollout, and test matrix).
- 2026-03-05: Phase 1 foundation landed: added `zerg/surfaces` core modules (contract, registry, idempotency store, orchestrator), introduced `surface_ingress_claims` model + unique key semantics, and added backend contract tests (`test_surface_idempotency.py`, `test_surface_orchestrator.py`).
- 2026-03-05: Phase 2 cutover landed: TelegramBridge now delegates inbound message handling to `SurfaceOrchestrator` + `TelegramSurfaceAdapter`, duplicate/run orchestration logic removed from bridge, fail-closed adapter exception handling added, and coverage expanded in adapter/bridge/orchestrator tests.
- 2026-03-05: Phase 3 test matrix landed: added multi-surface orchestrator integration coverage (`test_surface_orchestrator_multi_surface.py`) validating end-to-end web/voice/telegram ingress behavior, surface-specific routing metadata, push-delivery behavior, and surface-scoped idempotency semantics.
- 2026-03-05: Phase 4 cutover landed: web chat (`/api/oikos/chat`) and turn-based voice now route through first-class `WebSurfaceAdapter`/`VoiceSurfaceAdapter` + `SurfaceOrchestrator`, and voice turns now require explicit `message_id` (fail-closed idempotency contract, no server-side fallback IDs).

## [QA/Test] Verify landing-page provider claims (Claude/Codex/Gemini) (size: 2)

Status (2026-03-04): Done.

**Goal:** Validate that "Syncing now" claims map to tested, working ingestion paths (and identify any gaps or overclaims).

- [x] Audit landing copy vs actual supported providers in ingest pipeline
- [x] Run current test gates (`make test`, `make test-e2e`) and provider-focused ingestion checks
- [x] Confirm whether provider coverage is real end-to-end, fixture-based, or mocked at each layer
- [x] Propose minimal follow-up tests/wording changes if claims exceed verification evidence

Notes:
- Investigation requested due to uncertainty about current Gemini support quality and E2E realism.
- 2026-03-04: Verification run complete. `make test`, `make test-e2e`, and `make test-shipper-e2e` passed.
- 2026-03-04: Added/updated landing E2E coverage to match current UX and claims (`landing-links.spec.ts`, `landing-integrations.spec.ts`), validated with `22 passed`.
- 2026-03-04: Confirmed E2E harness intentionally uses deterministic test doubles for agent execution (`gpt-scripted`, mock hatch, tool stubs), while shipper/provider ingest pipeline tests run real API + SQLite + engine binary on fixture transcripts.

## [QA/Test] Deep Gemini end-to-end verification (size: 3)

Status (2026-03-04): Done.

**Goal:** Validate Gemini "Syncing now" with highest-confidence evidence from parser through ingest (including real local transcripts), and quantify remaining uncertainty.

- [x] Re-audit Gemini parser against latest upstream Gemini CLI session schema
- [x] Replay local real Gemini sessions and measure parse/ingest coverage (turn/tool fidelity)
- [x] Add targeted parser/integration tests for uncovered Gemini edge-cases
- [x] Run `make test-shipper-e2e`, Gemini-focused engine tests, and `make test-e2e`
- [x] Publish confidence matrix (what is truly e2e vs fixture/mock vs unverified)

Notes:
- 2026-03-04: Prior provider-claims check passed, but this follow-up focuses specifically on Gemini depth and real-world transcript fidelity.
- 2026-03-04: Added Gemini tool-result ingest support in Rust parser (`gemini_tool_result` / `role=tool`) with `tool_call_id` pairing.
- 2026-03-04: Added fixture-backed integration coverage for Gemini tool call + result payloads (`gemini_tool_results.json`) and shipper e2e assertions.
- 2026-03-04: Added parser fallback repair for invalid escaped surrogate pairs in Gemini JSON payloads; this recovers otherwise-dropped sessions.
- 2026-03-04: Real local replay against `~/.gemini/tmp/**/chats/*.json` now shows full coverage for observed tool results: `7,721 raw results -> 7,721 parsed tool-result events` across 324 files.
- 2026-03-04: Verification gates passed after changes: `make test-engine-fast`, `make test-shipper-e2e` (25 tests), and `make test-e2e` (`70 core + 4 a11y`).

## [Infra] Migration Hardening (startup-safe + preflight + ledger) (size: 3)

Status (2026-03-04): Done.

**Goal:** Prevent long startup stalls/timeouts from heavy SQLite rewrites while keeping legacy instance upgrades deterministic.

- [x] Move heavy legacy rewrites out of `initialize_database()` startup path
- [x] Add explicit migration ledger + idempotent runner (`longhouse migrate`)
- [x] Run migration preflight before control-plane reprovision
- [x] Add tests for pending/ran migration planning and reprovision preflight behavior
- [x] Validate with `make test` and `make test-e2e`

Notes:
- Heavy operations include global `events.branch_id` backfill and `source_lines` table rebuild.
- Startup should run lightweight schema/index guards only and report pending heavy migrations instead of executing them inline.
- Reprovision preflight should apply heavy migrations against instance data before container boot.
- 2026-03-04: Added explicit heavy migration runner + ledger in `zerg/db_migrations.py` and new CLI entrypoint `longhouse migrate` (plan/apply mode).
- 2026-03-04: `initialize_database()` now stays startup-safe and warns on pending heavy migrations instead of running them inline.
- 2026-03-04: Control-plane `POST /api/instances/{id}/reprovision` now runs migration preflight before deprovision/provision and aborts safely on preflight failure.
- 2026-03-04: Coverage added in `tests_lite/test_db_migrations.py` and control-plane reprovision/provisioner tests; full verification passed with `make test` and `make test-e2e`.

## [Product] Compaction Fidelity + Active Context Semantics (size: 4)

Status (2026-03-04): In progress (slice 1+2 landed: metadata ingest + events context mode).

**Goal:** Preserve full transcript fidelity while accurately modeling what Claude can still "remember" after `/compact`.

**First-principles invariants:**
- [x] Never lose bytes: source transcript archive must remain append-only and lossless
- [x] Facts are immutable; UI/search contexts are derived views
- [x] "Forensic history" and "active model context" are different and must both be queryable

**Implementation spec:**
- [x] Persist compaction metadata as first-class events (do not drop `type=summary` / compaction-adjacent records at parse time)
  - [x] Parse and ingest `summary`, `file-history-snapshot`, and `system` `{subtype: compact_boundary|microcompact_boundary}` as `role=system` events (Rust engine + Python parser)
  - [ ] Parse high-volume `progress` records as first-class events (deferred until default timeline/query mode can hide noise)
- [x] Add `compaction_boundary` derivation during ingest/projection (boundary anchored to source offset + timestamp)
- [x] Add context modes in read/query APIs:
  - [x] `/api/agents/sessions/{id}/events` supports `context_mode=forensic|active_context`
  - [x] Extend `context_mode` semantics to search/recall/session-tool surfaces (MCP + API list/search endpoints)
  - [x] `active_context` projection should anchor by explicit boundary source offset/timestamp
- [x] Keep pre-compaction turns visible in timeline/search by default (no destructive pruning)
- [x] In UI, mark pre-compaction facts as "outside active model context" instead of hiding/deleting
- [x] Add retention/sync guardrails so source transcripts are archived before local cleanup windows can delete them (for example Claude `cleanupPeriodDays` default)

**Acceptance tests:**
- [x] Real Claude transcript with repeated summary lines still roundtrips byte-for-byte in source archive
- [x] Compaction-only append does not create fake conversational events
- [x] `forensic` query returns pre-compact fact; `active_context` query excludes it unless reintroduced later

Notes:
- 2026-03-04: Rust + Python parsers now emit compaction-adjacent records as `system` events; `progress` remains intentionally skipped for now to avoid timeline noise until query modes land.
- 2026-03-04: Added `context_mode` projection for session events API/store (`forensic` default, `active_context` from latest compact/summary boundary); this is projection-only and does not mutate stored history.
- 2026-03-04: Active-context boundary now derives from explicit compaction marker metadata (`source_path`, `source_offset`, `timestamp`) instead of latest boundary event id; stale same-path events before the boundary offset are excluded.
- 2026-03-04: Added `context_mode` support to search + recall + MCP session tools (`search_sessions`, `get_session_detail`, `get_session_events`, `recall`) and `/api/agents/sessions` + `/api/agents/sessions/semantic` + `/api/agents/recall`.
- 2026-03-04: Session detail timeline now marks forensic rows outside the active context boundary (compaction pre-history remains visible with explicit "outside active model context" badges).
- 2026-03-04: Added regression test proving compaction-only append rows (`summary` + `compact_boundary`) do not inflate user/assistant turn counts (`tests_lite/test_ingest_session_counts.py`).
- 2026-03-04: `longhouse doctor` now checks Claude retention/sync risk (`cleanupPeriodDays`, Stop-hook presence) and warns when retention is short/default or hooks are missing.
- 2026-03-04: Context-mode search regression is covered end-to-end (`tests_lite/test_sessions_search_context_mode.py`) proving forensic includes pre-compact facts while active_context excludes stale pre-boundary facts.
- 2026-03-04: Added real-session verifier `scripts/verify_real_claude_transcript_fidelity.py`; run against Claude session `bf3c1a89-...` (`summary_lines=26`, `compact_boundary_lines=1`) now roundtrips exactly (`expected_bytes == exported_bytes == 2,347,789`).

## [Product] Claude Rewind DAG Fidelity (size: 3)

Status (2026-03-04): Done.

**Goal:** Match real Claude `/rewind` behavior when branching is represented by `uuid`/`parentUuid` graph relationships (not only source-offset rewrites/truncation).

**Implementation spec:**
- [x] Persist raw event lineage IDs (`uuid`, `parentUuid`) on `events` for every ingested line when present
- [x] Add branch-scoped dedupe for lineage IDs so replay retries do not duplicate events while branch prefix copy remains valid
- [x] Detect branch forks from lineage graph divergence even when no same-offset rewrite/truncation is observed
- [x] Align head-branch selection with Claude `leafUuid`/DAG head semantics when available
- [x] Add real-transcript verification harness for high-rewind sessions (multiple parent fan-outs)

Notes:
- 2026-03-04: Real transcript analysis on local Claude session `bf3c1a89-...` showed `summary=26`, `compact_boundary=1`, and ~25 parent fan-out branch points in raw JSONL while offset-only rewind detection produced a single stored branch, confirming the remaining fidelity gap.
- 2026-03-04: Added `event_uuid` + `parent_event_uuid` columns/index/migration path and ingest extraction in `AgentsStore`; coverage in `tests_lite/test_event_lineage_ingest.py`.
- 2026-03-04: Rewind detection now forks on lineage divergence (`parentUuid` already has different child on head) even when incoming lines are append-only; regression in `tests_lite/test_rewind_branch_projection.py::test_lineage_divergence_forks_branch_without_offset_rewrite`.
- 2026-03-04: Added `is_branch_copy` provenance on `source_lines` so forensic export excludes branch-prefix copies; real transcript verification (`scripts/verify_real_claude_transcript_fidelity.py --strict-fanout`) now passes with 19 stored branches and exact byte roundtrip.
- 2026-03-04: Ingest now honors summary `leafUuid` hints to realign active head branch; regression in `tests_lite/test_rewind_branch_projection.py::test_leaf_uuid_realigns_head_branch`.

## [Product] Rewind Branch Semantics + Dangling State UX (size: 5)

Status (2026-03-04): Done.

**Goal:** Handle `/rewind` as intentional branch history, not duplicate/corrupt event accumulation.

**First-principles invariants:**
- [x] Rewind must never destroy previously shipped facts
- [x] Post-rewind "head" must be reconstructable deterministically
- [x] Abandoned branches remain auditable but do not pollute default active timeline

**Implementation spec:**
- [x] Make source-line storage append-only by revision (stop overwriting `(session_id, source_path, source_offset)` on conflict)
- [x] Detect rewrite-at-same-offset and file truncation as `rewind_candidate` signals
- [x] Introduce branch metadata for sessions (`branch_id`, `parent_branch_id`, `branched_at_offset`, `is_head`)
- [x] On rewind detection:
  - [x] Freeze prior head as abandoned branch
  - [x] Start new head branch from rewind point
- [x] Update event projection APIs:
  - [x] default timeline = head branch only
  - [x] optional "show abandoned" mode for forensic/debug
- [x] Add explicit UX language for "dangling state": events still exist, but are not on active branch

**Acceptance tests:**
- [x] Rewind replay with rewritten line at same offset creates new head branch (not duplicate rows in active projection)
- [x] Forensic mode returns both pre- and post-rewind branches
- [x] Default timeline excludes abandoned-branch continuation after rewind point
- [x] Export "head only" and "full forensic" both pass deterministic reconstruction tests

Notes:
- Real DB evidence already shows same `(session, path, offset)` with distinct content hashes; this task formalizes that into branch semantics.
- 2026-03-04: Added `session_branches` + per-event/per-source-line `branch_id` with rewind detection on rewrite/truncation and append-only source-line revisions.
- 2026-03-04: Events API now defaults to `branch_mode=head` and supports `branch_mode=all`; response includes `abandoned_events` and per-event `is_head_branch`.
- 2026-03-04: Session detail UI now surfaces dangling-state language and a forensic toggle (“Show abandoned branches”).
- 2026-03-04: Regression coverage added in `tests_lite/test_rewind_branch_projection.py` and `tests_lite/test_session_events_branch_mode.py`.

## [Tech Debt] Demo seed/reset reliability + session environment fidelity (size: 2)

Status (2026-03-03): Done.

**Goal:** Remove demo workflow friction and fix missing machine/environment metadata in timeline APIs.

- [x] Document dev SQLite path (`~/.longhouse/dev.db`) in AGENTS quick-reference
- [x] Make `DELETE /api/agents/demo` delete demo rows by `provider_session_id LIKE 'demo-%'` (not `device_id`)
- [x] Add per-session error logging in demo seed path so partial failures are observable
- [x] Fix `SessionResponse.environment` mapping so API responses no longer return `null` for valid rows
- [x] Add/adjust tests for reset filtering and environment serialization

Notes:
- Triggered by 2026-03-03 timeline polish retro; most lost time came from silent seed failures + stale demo reset semantics.
- 2026-03-03: Added shared demo seeding helper (`seed_missing_demo_sessions`) to top-up missing demo sessions and emit per-session failure logs; wired into startup auto-seed and `POST /api/agents/demo`.
- 2026-03-03: `DELETE /api/agents/demo` now keys off `provider_session_id LIKE 'demo-%'`, decoupled from `device_id`.
- 2026-03-03: Restored `environment` in all session API response mappers (`/agents/sessions`, hybrid/semantic, and `/agents/sessions/{id}`).
- 2026-03-03: Added `POST /api/agents/demo?replace=true` (dev-only) for stale hot-reload recovery; response now includes `sessions_failed` + `sessions_deleted`.
- Validation: `make test` ✅ (519 lite backend + 96 control-plane + 9 engine tests).

## [Tech Debt] Longhouse simplification wave (Commis/Oikos/Forum) (size: 8)

Status (2026-03-04): Done.

**Goal:** Reduce conceptual and code complexity now that product direction is clear (timeline-first, Oikos coordinator, commis as CLI jobs).

- [x] Remove dual commis semantics (`standard`/legacy compatibility paths); keep workspace/scratch-only execution
- [x] Split monolith modules by responsibility (`oikos_tools.py`, `commis_resume.py`, `oikos_service.py`)
- [x] Quarantine Forum code as legacy while route stays disabled
- [x] Remove naming drift (`Forum live mode` → `Live sessions`) in comments/docs/API descriptions
- [x] Add guardrail tests to prevent regressions into deprecated flow

Notes:
- Keep the current soft-disable behavior for `/forum` while this cleanup is underway.
- 2026-03-03: `spawn_commis` runtime now only executes workspace path; legacy `standard` branch removed from job processor.
- 2026-03-03: Forum implementation moved to `frontend-web/src/legacy/forum`; timeline live panel is list-only (2D map removed from active timeline UX).
- 2026-03-03: Added guardrails for deprecated flow drift:
  - frontend route helper test locks `/forum` -> `/timeline` redirect semantics
  - backend tests assert `spawn_commis`/`spawn_workspace_commis` always persist `execution_mode=workspace`
- 2026-03-03: Began monolith split (Phase 1) by extracting shared pending-commis stream lifecycle helper to `services/oikos_run_lifecycle.py` and wiring `oikos_service.py` + `commis_resume.py` to use it.
- 2026-03-03: Extended Phase 1 lifecycle extraction with shared helpers for `oikos_waiting + run_updated(waiting)` and standardized failed `run_updated` payloads; both `oikos_service.py` and `commis_resume.py` now call the shared helpers.
- 2026-03-03: Added shared error lifecycle helper (`error` event + `stream_control:close(error)`) and replaced duplicate error-emission blocks in both oikos and commis resume paths.
- 2026-03-03: Removed remaining backend “Forum UI” naming in active-session and session-action docs/comments (`agents.py`, `session_chat.py`) to align on “live sessions”.
- 2026-03-03: Added shared helpers for successful/cancelled `run_updated` emission and replaced duplicate success/cancel payload blocks across `oikos_service.py` + `commis_resume.py`.
- 2026-03-03: Added shared helper for successful `oikos_complete` emission (with optional `trace_id`/`batch_size`) and replaced duplicate completion payload blocks in `oikos_service.py` + `commis_resume.py`.
- 2026-03-03: Began structural `commis_resume.py` split by extracting commis update content/queue helpers into `services/commis_updates.py` with new focused tests.
- 2026-03-03: Quarantined forum UI surface further by moving `pages/ForumPage.tsx` into `legacy/forum/ForumPage.tsx`; route remains redirect-only and tests now import from legacy path.
- 2026-03-03: Removed dead `frontend-web/src/styles/forum-map.css` placeholder (unused import surface).
- 2026-03-03: Continued `commis_resume.py` split by extracting inbox follow-up polling/scheduling into `services/commis_inbox_followup.py` and wiring callbacks from resume path with dedicated unit tests.
- 2026-03-03: Extracted inbox synthetic-task prompt builder to `services/commis_inbox_prompt.py` and replaced inline branch logic in `trigger_commis_inbox_run` with a shared helper + tests.
- 2026-03-03: Continued `commis_resume.py` split by extracting barrier coordination logic to `services/commis_barrier.py`; `commis_resume.py` now orchestrates while barrier semantics are covered by dedicated unit tests.
- 2026-03-03: Moved `ForumPage` tests from `pages/__tests__` into `legacy/forum/__tests__` to keep active page test surface aligned with forum quarantine.
- 2026-03-03: Removed `services/commis_resume.py` compatibility facade entirely; callers now import concrete modules directly (`commis_single_resume.py`, `commis_batch_resume.py`, `commis_inbox_trigger.py`).
- 2026-03-03: Removed compatibility wrappers from `tools/builtin/oikos_tools.py` for commis lifecycle/artifact tools; the tools registry now references concrete implementations imported directly from `tools/builtin/oikos_commis_artifact_tools.py`.
- 2026-03-03: Removed `spawn_commis` alias from active contracts: Oikos tool registry/runtime now uses `spawn_workspace_commis` only, tool schema/types were regenerated, and frontend Oikos timeline/tool-store logic now keys off `spawn_workspace_commis`.
- 2026-03-03: Cleaned spec drift in `docs/specs/unified-memory-bridge.md` + `docs/specs/3a-deferred-research.md` to remove legacy `spawn_commis` contract language and added a guardrail test asserting `"spawn_commis" not in OIKOS_TOOL_NAMES`.
- 2026-03-03: Extracted commis job-management implementations from `tools/builtin/oikos_tools.py` into `tools/builtin/oikos_commis_job_tools.py`; `oikos_tools.py` now owns tool registration + allowlist contracts.
- 2026-03-03: Extracted Oikos commis inbox-context lifecycle into `services/oikos_commis_context.py` and reduced `oikos_service.py` helpers to delegating wrappers.
- 2026-03-03: Added focused unit coverage for the extracted inbox-context helpers in `tests_lite/test_oikos_commis_context.py`.

## [Infra] Control-plane provisioning status reconciliation (size: 1)

Status (2026-03-04): Done.

**Goal:** Stop admin/API status drift where reprovisioned instances remain `provisioning` even after containers are healthy.

- [x] Add shared health-probe helper in `routers/instances.py`
- [x] Reconcile `provisioning` instances to `active` during admin list/get reads
- [x] Clear stale `last_health_at` timestamps on reprovision/password-regeneration paths
- [x] Add control-plane tests covering reconciliation and timestamp reset behavior

Notes (2026-03-04):
- `list_instances` and `get_instance` now opportunistically probe `/api/health` for `provisioning` rows and persist `active + last_health_at` on success.
- `reprovision` and `regenerate-password` now reset `last_health_at=None` when re-entering `provisioning`, preventing stale health metadata.

## [Product] Admin Operations Dashboard semantics + UX overhaul (size: 3)

Status (2026-03-03): Done.

**Problem:** Time-window selection in Admin does not affect the summary API call. UI says "Last 30 Days" while cards still show "Runs Today"/"Cost Today", which is misleading. Styling is also degraded due to malformed CSS blocks and inline style drift.

- [x] Write first-principles improvement spec (metrics semantics, labels, layout, accessibility, mobile behavior)
- [x] Add window-aware ops summary API (`today|7d|30d`) and align response naming with selected window
- [x] Rework Admin operations section to clearly separate window-scoped metrics from fixed-window metrics (for example `errors_last_hour`)
- [x] Replace broken admin ops styling with valid, scoped, responsive CSS (remove inline style overrides)
- [x] Update frontend/backend tests for the new contract and behavior
- [x] Run `make test` and `make test-e2e`, then ship + post-deploy `make qa-live`

Notes (2026-03-03):
- Spec: `docs/specs/admin-operations-dashboard-revamp.md`
- Backend: `/api/ops/summary` now accepts `window=today|7d|30d`, returns canonical window-scoped fields (`window`, `window_label`, `runs`, `cost_usd`, `top_fiches`) and backward-compatible aliases.
- Frontend: Admin page now binds window selector to query key/API call, updates KPI/top-fiche labels by selected window, and explicitly labels fixed realtime metrics.
- Styling: moved admin operations polish into scoped stylesheet `apps/zerg/frontend-web/src/styles/admin-ops.css`; removed inline demo card/button styles.
- Validation:
  - `cd apps/zerg/backend && ./run_backend_tests_lite.sh tests_lite/test_ops_summary_window.py` ✅
  - `cd apps/zerg/frontend-web && bunx vitest run src/pages/__tests__/AdminPage.test.tsx` ✅
  - `make test` ✅
  - `make test-e2e` ✅
  - `make qa-live` after deploy ✅ (8/8 passed against `https://david010.longhouse.ai`)

---

## [Infra] Zerg backup + restore drill via unified zerg-ops (size: 3)

Status (2026-03-03): Done.

**Goal:** Replace ad-hoc snapshot cleanup with a single, testable backup/restore flow that protects every Longhouse instance DB on zerg and keeps disk growth bounded.

- [x] Write first-principles spec (data scope, retention, remote sync contract, restore drill)
- [x] Implement unified `zerg-ops` script in repo (cleanup + backup + verify + prune + report)
- [x] Add automated local E2E test for backup -> restore byte/hash match across multiple instance dirs
- [x] Roll out updated script/config to zerg host and run a real backup + verify cycle
- [x] Update docs/runbook with operational commands and failure triage

Notes:
- Must stay schema-agnostic: backup logic must copy full SQLite bytes so future columns/events are automatically covered.
- Offsite sync should be optional config (Synology-ready) without coupling core local backups to network health.

Notes (2026-03-03):
- Added first-principles spec: `docs/specs/zerg-ops-backup.md`.
- Added unified script + local contract test:
  - `scripts/zerg-ops.sh`
  - `scripts/test-zerg-ops.sh`
  - `make test-zerg-ops-backup`
- Simplified implementation after complexity review:
  - Removed env-file driven config surface (`/etc/zerg-ops.env` is no longer part of the contract).
  - Removed personal offsite host/path details from repo code.
  - Offsite sync now uses neutral SSH alias `longhouse-offsite`; alias mapping lives only in host ssh config.
  - Scoped execution now uses CLI `--instance` instead of env overrides.
- Dead-man switch remains via `zerg-ops monitor` and systemd monitor timer.
- 2026-03-05: Tightened local retention to 5 snapshots, added backup-volume usage warnings at 80%, and auto-pruned stale unmanaged raw `longhouse*.db` dumps after 2 days so manual prod backups cannot quietly fill `/var/app-data`.
- 2026-03-06: Moved live Longhouse tenant data off root on zerg onto `/var/app-data/longhouse`; root usage dropped from 69% to 23%, app-data now carries the mutable instance state, and the temporary compatibility bind mount has since been removed.
- 2026-03-06: Restart during the storage migration exposed two legacy `runs.assistant_message_id` sentinel strings in `david010` (`live-voice-*`, `live-web-*`); repaired rows 25 and 26 in place on the host, then fixed app code so startup recovery no longer ORM-loads malformed GUID columns and Oikos now rejects non-UUID assistant message IDs at ingress.

---

## [QA/Test] Ship→Unship raw_json roundtrip contract (size: 1)

Status (2026-03-02): Done.

**Goal:** Add automated tests that validate ship→unship can reconstruct the full log byte-for-byte for both Claude and Codex fixtures.

- [x] Add Claude fixture roundtrip assertion
- [x] Add Codex fixture roundtrip assertion
- [x] Assert reconstructed lines from payload archive exactly match source lines for all source offsets

Notes (2026-03-02):
- Added engine payload contract tests in `apps/engine/src/pipeline/compressor.rs`:
  - `test_ship_unship_roundtrip_claude_fixture`
  - `test_ship_unship_roundtrip_codex_fixture`
- Contract path: parse fixture → build+gzip payload → decompress payload → reconstruct log lines from shipped `raw_json` + `source_offset` → assert byte-for-byte equality with source file lines at those offsets.
- Clarification: this now validates exact roundtrip for **all source lines** via `source_lines`, including metadata/unknown-schema lines.

---

## [Infra] Parser fidelity: remove raw_json 32KB cap (size: 4)

Status (2026-03-03): Done.

**Problem:** `raw_json` in the events table is supposed to be the complete original JSONL source line — the authoritative cloud copy of the user's session. It's currently hard-capped at 32KB (`MAX_RAW_LINE_BYTES` in `apps/engine/src/pipeline/compressor.rs`). A single image-containing Codex message can be 1–10MB of base64. Those lines get truncated, which means Longhouse is NOT a complete copy of the session. This violates the core product promise.

**Root cause confirmed:** In a real session (`8c81e236` on david010 instance), a user message with an attached screenshot was ~826KB. The 32KB cap truncated the raw_json. Combined with the parser bug that emitted zero events for image-only lines (fixed separately), the entire user turn was lost from the cloud record.

**What needs to change:**
- [x] Remove `MAX_RAW_LINE_BYTES` constant and the truncation logic in `compressor.rs` (lines ~38-41, ~132-140)
- The `raw_json` column in SQLite (`TEXT` type) handles arbitrary size; no schema change needed
- The HTTP ingest payload (`EventIngest.raw_json`) is just a JSON string field — no API change needed
- The gzip-compressed ingest payload will still compress well (base64 at 4:3 ratio, gzip recovers much of it)

**Scope / considerations:**
- DB will grow. A session with 50 screenshots could add 50–500MB to the user's DB. That's correct and expected — you need 5GB of images if the user has 5GB of images.
- The ingest HTTP request payload will be larger for image-heavy sessions. May need to bump any request size limits on the backend (check `MAX_CONTENT_LENGTH` / nginx/caddy config on the server).
- [x] Check `apps/zerg/backend/zerg/routers/agents.py` ingest endpoint for any body size limits. (No explicit body clamp found; gzip/zstd paths support streaming decode.)
- [x] Check the Coolify/nginx proxy config for upload limits on the zerg server. (`coolify-proxy` Caddy config has no request-body clamp directives for `david010.longhouse.ai`.)
- [x] After removing the cap, run `make test` — existing test `test_raw_line_cap_truncates` in `compressor.rs` updated to assert full preservation.
- [x] Run `make test-e2e`.

**Files to touch:**
- `apps/engine/src/pipeline/compressor.rs` — remove cap (marked with TODO comment)
- `apps/engine/src/pipeline/compressor.rs` test `test_raw_line_preserves_full_content` — validates no truncation
- Backend proxy/nginx config — check upload size limits
- Possibly `apps/zerg/backend/zerg/routers/agents.py` — check `max_content_length`

**Validation:** After the change, ingest a real Codex session with an image attachment and confirm `length(raw_json)` in the DB equals the full source line length from the JSONL file.

Notes (2026-03-02):
- Removed raw_json truncation in `apps/engine/src/pipeline/compressor.rs`; `raw_json` now forwards the full source line unchanged.
- Replaced truncation test with `test_raw_line_preserves_full_content`.
- Fixed Codex incremental session-id drift when parsing from non-zero offsets by scanning the file header for `session_meta.payload.id` before parse dispatch; added regression tests for both buffered and mmap paths.
- Validation run:
  - `cargo test -p longhouse-engine pipeline::compressor::tests` → 4 passed
  - `cargo test -p longhouse-engine pipeline::parser::tests` → 33 passed
  - `make test` → 444 backend tests passed, 96 control-plane tests passed, 9 engine parser tests passed
  - `make test-e2e` → 59 core E2E + 4 a11y passed
  - Live ingest check (synthetic image-like payload): posted a 1,200,093-byte `raw_json` line to `https://david010.longhouse.ai/api/agents/ingest`; DB `LENGTH(raw_json)` matched exactly (`1200093`)

Notes (2026-03-03):
- Fixed a second Codex session-id drift path: removed stale `file_state`/spool `session_id` overrides in `apps/engine/src/shipper.rs` and `apps/engine/src/main.rs` so parser-resolved canonical IDs always win on incremental parses.
- Added `test_stale_stored_codex_session_id_is_not_reused` regression coverage.
- Backfilled live instance (`david010`) orphan split sessions again after the fix; current DB check shows `provider='codex' AND project IS NULL` count is `0`.
- Hardened `scripts/fix_codex_orphan_sessions.py` for dedup-safe reparenting and WAL-safe prod snapshot/restore via SQLite backup API.

---

## [Product] Oikos Thread Context Window Bug (size: 3)

Status (2026-03-02): Done.

**Problem:** Oikos uses a long-lived thread, but current history loading appears to cap to an oldest-message window rather than a latest-message sliding window. On long threads, this can drop recent context and degrade decisions.

- [x] Reproduce with a long oikos thread and confirm current message window behavior end-to-end
- [x] Fix thread message retrieval to feed the most recent window to the LLM while preserving chronological order
- [x] Add regression coverage in `tests_lite/` for long-thread context selection
- [x] Run targeted backend tests and update this task with pass/fail evidence

Notes (2026-03-02):
- Implemented `get_recent_thread_messages()` and switched `ThreadService.get_thread_messages_as_langchain()` to recent-window retrieval.
- Added `tests_lite/test_thread_context_window.py` (latest-100 and custom-limit window assertions).
- Added runner-path verification (`MessageArrayBuilder.with_conversation`) so the same latest-window behavior is validated through message assembly used by execution.
- Targeted test run: `./run_backend_tests_lite.sh tests_lite/test_thread_context_window.py` → 3 passed.

---

## [Product] Oikos Prompt/Tool Contract Alignment (size: 2)

Status (2026-03-02): Done.

**Problem:** Oikos prompt guidance still emphasizes deprecated `spawn_commis` patterns while runtime is workspace-first and `spawn_workspace_commis` is the canonical path.

- [x] Update Oikos base prompt examples/instructions to reflect workspace-first delegation
- [x] Remove/replace deprecated `spawn_commis` guidance from user-facing prompt text
- [x] Keep compatibility alias behavior in code, but stop teaching legacy semantics
- [x] Add/adjust tests that assert prompt/tool contract consistency

Notes (2026-03-02):
- Updated `BASE_OIKOS_PROMPT` to make `spawn_workspace_commis` primary, mark `spawn_commis` as deprecated, and replace stale `wait` parameter guidance with explicit `wait_for_commis(job_id)` usage.
- Added `tests_lite/test_oikos_prompt_contract.py` guardrails for workspace-first wording, removal of `wait=True/False` prompt guidance, and alignment with tool descriptions.
- Targeted test run: `./run_backend_tests_lite.sh tests_lite/test_oikos_prompt_contract.py tests_lite/test_thread_context_window.py` → 5 passed.

---

## [Frontend] Forum/Session Status Normalization (size: 2)

Status (2026-03-02): Done.

**Problem:** Frontend active-session handling has edge-case mismatches between `status` and `presence_state`, and session detail can miss deep anchors on long sessions due to a hard event cap.

- [x] Normalize active/inactive status mapping across Forum + session mapper
- [x] Add unknown/unsupported presence fallback handling for future hook states
- [x] Add session-detail pagination (or equivalent fetch strategy) so deep links beyond first 1000 events resolve reliably

Notes (2026-03-02):
- Added shared session-state helpers in `frontend-web/src/forum/session-status.ts` and wired Forum list/canvas mapping to a single normalization path.
- Presence badges now safely handle unsupported states (e.g. future hook values) without breaking UI state mapping.
- Session detail now uses paginated event loading (`useAgentSessionEventsInfinite`) with auto-fetch for deep-link anchors and manual "Load older events" pagination.
- Frontend verification:
  - `bunx vitest run src/forum/__tests__/session-status.test.ts src/pages/__tests__/ForumPage.test.tsx` → 8 passed
  - `bun run validate:types` → passed

---

## [QA/Test] High-Risk Guardrails (size: 2)

Status (2026-03-02): Done.

- [x] Add presence ingest tests for invalid state no-op and `tool_name` clearing on non-running states
- [x] Add migration guard test that checks SQLite table columns against agents model columns
- [x] Add deterministic dispatch tests (direct response vs quick tool vs commis delegation) in default test suite

Notes (2026-03-02):
- Extended `tests_lite/test_forum_filtering.py` with presence no-op coverage for invalid states and `tool_name` clearing when transitioning away from `running`.
- Added `tests_lite/test_sqlite_migration_guard.py` to simulate legacy SQLite tables and assert `_migrate_agents_columns()` backfills all current model columns for `sessions`, `events`, and `job_runs`.
- Added `tests_lite/test_oikos_dispatch_contract.py` for deterministic dispatch categories:
  - direct response (no tool call),
  - quick utility tool call,
  - commis delegation.
- Targeted test run: `./run_backend_tests_lite.sh tests_lite/test_forum_filtering.py tests_lite/test_sqlite_migration_guard.py tests_lite/test_oikos_dispatch_contract.py` → 15 passed.

---

## [Product] Landing Screenshot Frame Contrast Variants (size: 1)

Status (2026-02-27): Done.

**Goal:** Add two visual frame variants for landing screenshots so warm-page screenshots still pop and can be compared quickly in-browser.

- [x] Add a second screenshot frame theme with cooler/high-contrast treatment
- [x] Wire screenshot frame theme through landing hero + product showcase
- [x] Add an in-browser toggle for quick visual comparison in marketing mode

## [QA/Test] Verify Session Resume End-to-End (size: 4)

Status (2026-02-26): Core verification shipped.

**Goal:** Validate the core promise ("resume from any device") with deterministic E2E coverage and fix any behavioral gaps discovered.

- [x] Run targeted resume E2E coverage and capture current failures
- [x] Fix resume path regressions (backend and/or frontend) found by E2E (none found in current flow)
- [x] Add/adjust assertions so resume guarantees are explicit (not implied by generic session continuity)
- [x] Ship with passing `make test-e2e-single TEST=tests/core/session-continuity.spec.ts` and `make test-e2e`

Notes (2026-02-26):
- Added user-facing resume coverage in `apps/zerg/e2e/tests/core/sessions.spec.ts`:
  - Claude sessions show `Resume Session` and open the chat overlay
  - Non-Claude sessions hide `Resume Session`

---

## [Docs/Drift] Resolve VISION Contradictions (size: 1)

Status (2026-02-26): Done.

- [x] Updated Principles section to cloud-first CTA (hosted primary; self-hosted supported)
- [x] Replaced old "Oikos main chat" ASCII diagram with timeline/session-first product diagram

---

## [Product] Quota Error UX in Oikos Chat (size: 2)

Status (2026-02-26): Done (first-principles UI + behavior).

- [x] Parse 429 JSON detail in Oikos chat requests (don't discard backend detail)
- [x] Convert run-cap/budget errors into clear reset-time messaging
- [x] Show quota context in-chat for the active assistant bubble instead of raw generic failure
- [x] Add always-visible quota panel in Oikos header (health/warning/blocked, progress, remaining, runs)
- [x] Block new input only on true quota exhaustion (keep normal post-run unblock behavior intact)

## [Tech Debt] Commis model selection — remove implicit defaults (size: 3)

Status (2026-02-26): Done (shipped in `c0872450`).

**Problem:** `DEFAULT_COMMIS_MODEL_ID` resolves via `models.json` tiers to `gpt-5.2` (OpenAI). This gets stored on `CommisJob.model` and passed as `--model gpt-5.2` to hatch, which passes it to Claude Code CLI — nonsensical for non-OpenAI backends. Two spawn paths are both broken:
- `oikos_tools.py:103` — falls back to `DEFAULT_COMMIS_MODEL_ID`
- `oikos_react_engine.py:615` — hardcoded fallback to `"gpt-5-mini"`

`CloudExecutor` always passes `--model`, so hatch backend defaults never kick in.

**Fix:**
Make `model` an explicit override only, not a forced default. New execution logic in `cloud_executor.py`:
- `backend + model` both provided → pass both (`-b <backend> --model <model>`)
- `backend` only → pass `-b` only, let hatch pick the backend's default model
- `model` only → infer backend via compat mapping (see below)
- neither → full hatch defaults (zai + glm-5)

**Backend ↔ model compat mapping** (for backward compat when bare model ID is passed):
- `gpt-*`, `o1-*`, `o3-*`, `o4-*` → `codex` backend
- `claude-*`, `us.anthropic.*` → `bedrock` backend (or direct anthropic if key available)
- `glm-*` → `zai` backend
- `gemini-*` → `gemini` backend
- anything else → error loudly, don't silently use wrong backend

**Files to change:**
- `apps/zerg/backend/zerg/tools/builtin/oikos_tools.py:103` — remove `DEFAULT_COMMIS_MODEL_ID` fallback
- `apps/zerg/backend/zerg/services/oikos_react_engine.py:615,704` — remove `"gpt-5-mini"` hardcode
- `apps/zerg/backend/zerg/services/cloud_executor.py:75,203` — implement new logic (backend-only path)
- `apps/zerg/backend/zerg/models_config.py:182` — remove or deprecate `DEFAULT_COMMIS_MODEL_ID`
- `apps/zerg/backend/zerg/models/models.py:220` — keep `CommisJob.model` nullable (don't rebuild SQLite table; just allow None)

**Don't:** make `CommisJob.model` NOT NULL migration on SQLite — table rebuild is risky. Just allow None and skip `--model` flag when absent.

**Tests to write first** (`tests_lite/`): no backend tests exist for the commis→cloud_executor→hatch path. Add at least: model+backend passed → correct hatch args; backend-only → no --model flag; unknown model → error.

**Follow-up (2026-03-02):**
- [x] Scenario seeding no longer forces `"gpt-5-mini"` when `commis_jobs[].model` is omitted (`scenarios/seed.py` + regression test).

---

~~**[Tech Debt] Reconcile historical Codex orphan sessions (size: 3)**~~ Done (2026-02-25). 129 orphans merged (7,446 events re-parented) via `scripts/fix_codex_orphan_sessions.py`. Used `source_path` filename to extract canonical UUID — no time-window ambiguity. Prod DB updated, 0 null-project Codex sessions remain.


1. ~~**[Launch] Move Settings into user menu** — remove it from any primary nav surface; add to header user dropdown + mobile menu.~~ Done (2026-02-25).
2. ~~**[Launch] Briefings as Timeline secondary action** — add a Timeline header action to open `/briefings` instead of a primary nav tab.~~ Done (2026-02-25).
1. ~~**[QA/Test] Stabilize session-continuity E2E polling** — tolerate transient `/api/oikos/runs/:id/events` socket hangups.~~ Done (2026-02-25).
2. ~~**[Launch] Simplify primary navigation** — Timeline as the core surface. Hide Forum for now, move Settings out of top-level nav, and decide whether Briefings is a primary tab or secondary surface (e.g., under More or Settings).~~ Done (2026-02-24).
3. ~~**[Launch] Forum as Timeline subpane** — consider migrating the live overview into the Timeline UI if it still earns a place.~~ Done (2026-02-24).
4. **Extended hook states** (`needs_user`, `blocked`) — blocked on Claude Code hook support. Defer.
5. **Oikos Dispatch Contract** — defer until usage demands it.
6. ~~**PyPI publish**~~ — shipped as v0.1.3 (2026-02-24).

---

## [Product] Search + Discovery

**Status (2026-02-23):** Core shipped. Timeline search is fully functional.

- [x] Keyword search (FTS) — sessions page, instant
- [x] Semantic search — AI toggle (✨ icon), hybrid RRF mode, sort by relevance/recency
- [x] Recall panel — turn-level semantic search, `?event_id=X` anchoring to matched turn
- [x] Session titles — LLM summary_title preferred, cwd/project/date fallbacks
- [x] Smart search fallback — keyword→semantic auto-fallback when no keyword results
- [x] Semantic result display polish — score badge ("87% match") shown on AI search results when score ≥ 0.5.

---

## [Product] Forum + Presence

**Status (2026-02-23):** Fully working end-to-end on prod.

- [x] Presence ingestion — `session_presence` table, outbox pattern (no network in hooks)
- [x] Real-time state — thinking/running/idle via Claude Code hooks → daemon → API
- [x] Forum UI — active rows glow, canvas entities pulse, presence priority over ended_at
- [x] Bucket actions — Park/Snooze/Archive/Resume
- [x] Extended hook states (`needs_user`, `blocked`) — shipped 2026-03-03 via Notification/PermissionRequest hooks

---

## [Product] Oikos Fiche Surface

**Status (2026-03-02):** Fixed.

- [x] `/api/oikos/fiches` now returns persisted `fiche.next_run_at` (was always `null` even when scheduler populated it).
- [x] Added API-lite regression test coverage for owner scoping + `next_run_at` serialization.

---

## [Product] Briefings + AI Features

**Status (2026-02-23):** Core wired. Depends on LLM summarization running.

- [x] Briefings page (`/briefings`) — project selector, session summaries + insights + proposals
- [x] Reflection briefing endpoint — `GET /api/agents/briefing`
- [x] Summarization coverage gap — fixed: `enqueue_ingest_tasks` now called inside `AgentsStore.ingest_session()` so all paths (demo seeds, commis_job_processor, CLI, router) enqueue summary + embedding tasks.

---

## [Product] Harness — Oikos Dispatch Contract (Deferred)

- [x] Oikos dispatch contract: direct vs quick-tool vs CLI delegation
- [ ] Claude Compaction API for infinite thread context management

Research doc: `apps/zerg/backend/docs/specs/3a-deferred-research.md`

Notes (2026-03-04):
- Added explicit dispatch-lane contract to `BASE_OIKOS_PROMPT` (direct, quick-tool, CLI delegation) with escalation rules.
- Added backend-intent mapping guidance in prompt (`zai|codex|gemini|bedrock|anthropic`) for `spawn_workspace_commis`.
- Added runtime dispatch normalization in `oikos_react_engine.py` to infer requested backend from latest user prompt and inject it into `spawn_workspace_commis` when missing.
- Added dispatch guardrail tests for lane classification and backend normalization.

---

## [Tech Debt] Schema Migration

**Resolved (2026-02-22):** `_migrate_agents_columns()` in `database.py` now covers all
current columns. **Rule:** every new `Column` on an agents model must get a corresponding
`ALTER TABLE` entry in that function — SQLite ignores new columns on existing tables.

- [x] `last_summarized_event_id`, `user_state`, `user_state_at` — added to migration
- [ ] No current gaps known — watch for new columns added without migration entries

---

## [Docs/Drift] Open Items

- ~~`docs/install-guide.md` still references `longhouse connect --poll` behavior that no longer matches runtime engine behavior.~~ Fixed in docs + CLI help sync (2026-03-02).
- ~~Keep VISION "Current State" sections in sync with hook installation/runtime details whenever hook registration behavior changes.~~ Synced shipper/hook examples + command docs (2026-03-02).
- ~~PyPI `0.1.1` lags repo `0.1.2`.~~ Published as v0.1.3 (2026-02-24).

---

## [Tech Debt] CSS Legacy Patterns

- Legacy modal pattern CSS — `.modal-*` classes still exist across multiple components/stylesheets and both modal systems remain loaded.
- Legacy token aliases — present in `styles/tokens.css`, actively referenced in component CSS.

---

## [Tech Debt] Newly Verified Follow-ups (2026-03-02)

- [x] Surface `register_all_jobs()` import/registration failures as actionable health status (exposed via `registration_warnings` on `GET /api/jobs` and shown in Jobs UI) (2026-03-02).
- [x] Added frontend coverage for JobsPage registration warning rendering (`pages/__tests__/JobsPage.test.tsx`).
- [x] Eliminated control-plane test deprecation warnings by migrating to `SettingsConfigDict` and FastAPI lifespan startup hook (2026-03-02).
- [x] Added semantic-search regression tests to enforce `hide_autonomous` filtering of sidechain and zero-user sessions.
- [x] Added SSRF regression coverage for LLM provider base URLs (metadata hostname + DNS-rebinding-to-private-IP cases).
