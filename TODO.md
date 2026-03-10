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

## [Product][Tech Debt] Refactor session detail into a pane-based workspace (size: 4)

Status (2026-03-10): Done. Session detail now renders as a pane-based workspace with a left context rail, center timeline navigator, right inspector, and a docked continuation surface that uses width on desktop instead of wasting it.

**Goal:** Turn timeline session detail into a real workspace with reusable panes and first-class event selection, while preserving current continuation/thread capabilities.

- [x] Extract session detail data/state into a headless workspace hook
- [x] Introduce a workspace shell with left context, center timeline, and right inspector panes
- [x] Move event detail out of inline expansion and into inspector-driven components
- [x] Keep continuation/thread behavior working during the layout transition

Notes:
- 2026-03-10: The route currently mixes fetching, derived timeline shaping, deep-link handling, continuation logic, and rendering in one file. Refactor first; visual polish can follow once pane responsibilities feel right.
- 2026-03-10: There is no existing resizable pane primitive in the frontend. Start with fixed panes and clean component seams first.
- 2026-03-10: Live local capture confirmed the new layout uses the width much better than the old centered transcript. The next iteration, if needed, is resizable panes or a collapsible continuation dock, not another full route rewrite.

## [Product] Proactive Oikos operator mode (size: 5)

Status (2026-03-10): In progress. The immediate goal is to define the product principles and dogfood shape before building triggers, policies, or background loops that could lock us into the wrong architecture.

**Goal:** Make Oikos feel like a proactive technical deputy that can notice meaningful session state changes, decide what to inspect next, and take bounded actions without collapsing into a giant brittle automation engine.

- [x] Write a principles-first spec for proactive Oikos that stays future-friendly and avoids premature schema/runtime lock-in
- [ ] Dogfood a tiny wakeup set around coding-session transitions plus a periodic sweep fallback
- [ ] Add the thinnest possible Oikos-owned state for trigger history / policies without duplicating session logs
- [ ] Ship one bounded autonomy slice that can inspect a session, continue it, or escalate back to the user

Notes:
- 2026-03-10: The repo already has the durable coding-agent transcript/archive layer; the new problem is mostly Oikos-owned wakeups, policies, and decision history, not rebuilding another session store.
- 2026-03-10: "Session Shepherd" was only a working nickname. The actual product direction is broader: proactive Oikos / operator mode / Jarvis-like deputy behavior.
- 2026-03-10: Start simple and dogfood. Favor principles, thin triggers, and bounded actions over an elaborate orchestration framework.
- 2026-03-10: Foundation harness slice landed: deterministic shadow journeys with durable artifacts, fixture-backed coding-session wakeups, and `make test-autonomy-journeys`.
- 2026-03-10: First runtime-adjacent follow-up should fix the `invoke_oikos()` transport seam so proactive wakeups and non-web surfaces can reuse the same execution entrypoint without hardcoding `WebSurfaceAdapter`.
- 2026-03-10: Landed the first transport seam fix: `invoke_oikos()` now accepts an explicit surface adapter and adapter-specific raw payload, so operator-mode or non-web callers can reuse the same entrypoint without pretending everything is browser chat.
- Spec: `docs/specs/oikos-proactive-operator.md`.
- Roadmap: `docs/plans/oikos-autonomy-roadmap.md`.

## [Infra][QA/Test] Longhouse engine shipper byte batching + exact replay (size: 4)

Status (2026-03-10): In progress. The correctness floor is now in place; the remaining work is to make oversized session deltas actually make forward progress by splitting them into exact byte-range batches and replaying only the spooled range.

**Goal:** Make the Rust engine shipper eventually deliver large session files without data loss, over-send, or infinite 413 retries.

**Done when:**
- Fresh shipping splits a file delta into sequential byte-range batches capped by `max_batch_bytes`
- Replay rebuilds and ships only the exact spooled range, never newer bytes that appeared later in the file
- Successful multi-batch shipping advances `acked_offset`/`queued_offset` monotonically batch-by-batch with no gaps or overlap
- A single oversize source range fails loudly and deterministically instead of looping forever
- Regression coverage proves planner invariants, partial failure recovery, exact-range replay, and the supported CLI/E2E paths

- [x] Add a pure range batch planner with deterministic contiguous-range invariants
- [x] Wire `max_batch_bytes` into fresh shipping and one-shot `ship`
- [x] Make spool replay range-exact instead of reparsing to EOF and only capping the ack
- [x] Define and test explicit handling for a single oversize source range
- [x] Re-run supported shipper verification targets (`make test-engine-fast`, `make test-shipper-e2e`)

Notes:
- 2026-03-10: The current pointer spool stores `(file_path, start_offset, end_offset)` only. That is fine, but replay must rebuild the same range exactly or it can over-send newer bytes added after the original failure.
- 2026-03-10: The simplest correct design is to keep the existing monotonic `queued_offset`/`acked_offset` model and ship batches sequentially per file; a sparse per-range state machine is unnecessary for this slice.
- 2026-03-10: Use source-line byte ranges as the primary planning unit, then verify compressed size as needed. Event-count batching is the wrong abstraction here.
- 2026-03-10: Shipped the batching slice. Claude/Codex JSONL sessions now batch on exact source-line byte ranges for fresh shipping and replay; whole-document Gemini sessions fall back to a single payload and dead-letter deterministically if they exceed `max_batch_bytes`, because they do not expose line-addressable replay boundaries.
- 2026-03-10: Live verification in progress. Goal: install the new local engine build, restart the LaunchAgent, run hosted QA, and prove forced multi-batch shipping against the real `david010.longhouse.ai` instance using a temporary engine DB plus an existing large local session file.
- 2026-03-10: Live verification result: `make install-engine` succeeded and the local LaunchAgent restarted onto a new PID. Hosted API health and agents API were healthy, but `make qa-live` finished `7/8` with one unrelated prod UI failure: session detail for the latest live session stayed stuck on "Loading session..." even though the underlying session/event APIs were fast.
- 2026-03-10: Forced live batching succeeded against the real instance. A 51 MB Codex session proved the multi-batch path with 8 successful ~1 MiB POSTs before the manual interruption; the temp DB advanced `acked_offset`/`queued_offset` to `8302957`.
- 2026-03-10: Clean completion run used a 7.6 MB Codex session at `max_batch_bytes=1048576`. Result: `410` events shipped live, temp DB reached EOF (`8015424`), and exactly one dead-letter row was recorded for a single oversize source range `758780..2519193` (1.76 MB raw line > 1 MiB cap). That is expected under the current design.
- 2026-03-10: After returning to stable Wi-Fi, the real daemon spool backlog moved from `33` pending entries to `28` over one replay interval, so installed replay is making forward progress, but the backlog is not yet fully drained.
- 2026-03-10: Follow-up hardening slice shipped for the pointer spool path. Pending spool rows are now unique per `(provider, file_path, start_offset, end_offset)` via startup dedupe + a partial unique index, startup recovery is idempotent, and fresh shipping stops while `queued_offset > acked_offset` so the same gap cannot be re-enqueued from `acked_offset`.
- 2026-03-10: Follow-up verification passed with `make test-engine-fast`, `make test-shipper-e2e`, and a fresh `make qa-live` run (`8/8`). The earlier prod session-detail failure did not reproduce after the network stabilized.

## [Infra][QA/Test] Longhouse engine shipper correctness fixes (size: 3)

Status (2026-03-10): Done. The immediate correctness work to stop dropping data around partial EOF lines, make dry-run truly non-mutating, and align one-shot ship/replay behavior with the daemon path landed before byte-based batching.

**Goal:** Make the Rust engine shipper's current semantics safe and internally consistent before adding batching/integrity work.

- [x] Use parser `last_good_offset` for shipped/spooled/acked ranges instead of raw file size
- [x] Make `longhouse-engine ship --dry-run` and `ship --file --dry-run` non-mutating
- [x] Align one-shot `ship` and spool replay handling with daemon 413/backpressure semantics
- [x] Add regression coverage for partial-line EOF handling, dry-run non-mutation, and 413 replay behavior
- [x] Re-run supported shipper verification targets (`make test-engine-fast`, `make test-shipper-e2e`)

Notes:
- 2026-03-09: Review found the parser already tracks `last_good_offset`, but shipper paths currently commit `file_size`, which can permanently skip incomplete trailing JSONL lines.
- 2026-03-09: `main.rs` bulk ship path still special-cases neither 413 nor spool backpressure correctly, so it can regress the daemon fix.
- 2026-03-09: The current dry-run paths advance offsets, which is not acceptable for a debugging command.
- 2026-03-09: First slice shipped: buffered parser now leaves trailing partial EOF lines unacked, shipper offsets follow `last_good_offset`, `ship --dry-run` paths no longer mutate state, and `test-engine-fast` now includes engine unit tests so these regressions stay covered.
- 2026-03-09: Second slice shipped: bulk `ship` now reuses shared `ship_and_record()` semantics, replay keeps 413 payloads pending with backoff instead of killing them, and replay acks only complete bytes so partial trailing lines remain recoverable.

## [Launch][Product] Session continuation from timeline should feel native (size: 3)

Status (2026-03-08): UX shipped for the current Claude-backed continuation path. Opening a session now lands near the latest context, the transcript and composer live on the same page, and non-Claude sessions explicitly explain the remaining provider gap instead of failing silently.

**Goal:** Make opening a synced session from timeline/mobile feel like a natural continuation flow: land near the latest context, show a clear composer where users expect it, and make cloud resume obvious instead of hidden.

- [x] Replace the current split detail-vs-resume mode with a single transcript page that can open an inline continuation composer
- [x] Make timeline/live-session entrypoints guide users into continuation more clearly instead of burying it behind a small header button
- [x] Auto-position resumed sessions near the latest context/composer instead of always opening at the top of long transcripts
- [x] Add focused frontend regression coverage for the resumed-session UX
- [x] Capture the remaining provider-parity gap explicitly if Codex/Gemini still cannot resume synced sessions directly

Notes:
- 2026-03-08: `POST /sessions/{id}/chat` exists today, but only for Claude-backed sessions; the main product issue on web/mobile was UX, not total backend absence.
- 2026-03-08: Timeline cards and live rows now route Claude sessions through `?resume=1`, detail pages auto-jump to the latest continuation point, and the inline composer lives below the full transcript instead of replacing it.
- 2026-03-08: Codex/Gemini sessions now show an explicit “not resumable from the web yet” state. That is honest, but it is still a real product gap for launch.

---

## [Launch][Product] Make cross-device continuation branch-safe and explicit (size: 6)

Status (2026-03-08): Done. Continuation now models one logical thread with explicit child sessions, one writable head, and honest stale-branch behavior instead of pretending laptop + cloud are mutating the same transcript in place.

**Goal:** Make "continue from phone, then continue later on laptop" understandable and safe without pretending there is one magical transcript being kept in sync everywhere.

- [x] Add session-level continuation lineage metadata (for example `thread_root_session_id`, `continued_from_session_id`, `continuation_kind`, `origin_label`, and a branch-point event/offset) and keep it distinct from the existing rewind-oriented `session_branches` table
- [x] On the first cloud/web/mobile message from a synced session, create a new child continuation explicitly instead of presenting it as an in-place mutation of the original source session
- [x] Define writable-head semantics: the latest continuation is writable, older branches are historical/stale, and typing there should start a new continuation instead of silently mutating history
- [x] Detect later laptop shipping after a cloud branch and ingest it as a new local continuation child instead of appending to the pre-branch source session as if nothing changed
- [x] Update timeline/detail UX to show lineage clearly (`Started on Cinder` -> `Continued in Cloud`, latest head, stale branch warning, open latest) while keeping the main path fast for the current head
- [x] Add regression coverage for branch creation, stale-branch handling, later local divergence, and timeline grouping of continuations under one logical thread

Notes:
- 2026-03-08: We deliberately did not build fake bidirectional transcript sync. Longhouse now treats cloud/local continuation as explicit branching with one latest writable head.
- 2026-03-08: `POST /sessions/{id}/chat` now creates explicit cloud child continuations when needed, scopes locks at the thread root, and ships resumed cloud sessions back with lineage metadata instead of accidental sibling rows.
- 2026-03-08: Session ingest now turns later laptop shipping after a cloud branch into a local child continuation (and reuses the latest same-origin child) instead of mutating the pre-branch source session.
- 2026-03-08: Timeline is now thread-centric: one card per logical task, latest head by default, stale-branch banner on older continuations, lineage rail in detail, and `Branch from Here` copy for historical branches.
- 2026-03-08: Regression coverage exists at three levels: backend lineage tests, core browser E2E in `apps/zerg/e2e/tests/core/sessions.spec.ts`, and live hosted proof in `apps/zerg/e2e/tests/live/session-continuation-lineage.spec.ts`.
- 2026-03-08: Fixed a hosted first-message regression in the continuation route: `prepare_session_for_resume()` had been self-fetching `/api/agents/sessions/{id}/export` over `LONGHOUSE_API_URL`, which failed inside the instance container. Resume prep now uses `AgentsStore.export_session_jsonl()` directly when a DB session is already in-process, and `tests_lite/test_session_resume_prep.py` covers both direct resume prep and the real `POST /api/sessions/{id}/chat` path so UI-only coverage cannot miss this again.
- 2026-03-09: Core browser E2E now covers the literal first cloud continuation send from the timeline detail page (`apps/zerg/e2e/tests/core/sessions.spec.ts`), using a deterministic fake Claude stream only when `TESTING=1` so CI verifies the real UI -> route -> branch creation path without depending on external Claude execution.
- Spec: `docs/specs/session-continuation-lineage.md`.

---

## [QA/Test] Provider-backed continuation smoke (Anthropic CI key) (size: 1)

Status (2026-03-09): Done.

**Goal:** Add one optional manual/nightly smoke that exercises the real continuation send path against a real Claude backend without depending on expiring Bedrock SSO or ambient personal laptop auth.

- [x] Keep per-push continuation E2E deterministic (`TESTING=1` + fake stream) so core CI stays fast and reliable
- [x] Make the real continuation route backend-configurable for `ambient`, `zai`, `bedrock`, or `anthropic` while preserving the current production default
- [x] Store a dedicated CI-only Anthropic key outside the repo and inject it into GitHub Actions as `LONGHOUSE_CI_ANTHROPIC_API_KEY`
- [x] Add a manual/nightly workflow that runs the single continuation-send browser smoke with `SESSION_CHAT_BACKEND=anthropic`
- [x] Verify the real browser path locally with proof (`make test-e2e-continuation-provider`)

Notes:
- 2026-03-09: The real provider smoke now lives in `apps/zerg/e2e/scripts/provider-continuation-smoke.mjs` instead of the normal Playwright suite. That keeps the core browser E2E deterministic and avoids runner/global-setup noise while still proving the literal user action with a real Claude session.
- 2026-03-09: The Anthropic key is CI-only. Do not export it globally on developer laptops or switch normal Claude coding flows away from Bedrock SSO.

---

## [Launch][Product] Codex/Gemini cloud continuation parity (size: 5)

Status (2026-03-08): Not started. Longhouse can reconstruct/resume Claude sessions today, but Codex/Gemini direct continuation is still missing even though the current local `codex` CLI exposes `codex exec resume ... --json`.

**Goal:** Make the core “pick up any synced session from the cloud” promise true across the main providers users will actually run.

- [ ] Extend the headless executor/Hatch path so provider-specific resume commands can be invoked for Codex (and verify Gemini’s equivalent contract)
- [ ] Teach Longhouse how to reconstruct provider-local session state for Codex/Gemini the way `session_continuity.py` already does for Claude
- [ ] Generalize `POST /sessions/{id}/chat` beyond Claude-only backend assumptions
- [ ] Add regression coverage for Codex web continuation once the backend path exists
- [ ] Revisit any remaining UI copy once the provider gap is actually closed

Notes:
- 2026-03-08: Local `codex` CLI supports `codex exec resume [SESSION_ID] [PROMPT] --json`; the missing layer is Longhouse/Hatch/provider-state reconstruction, not the existence of a Codex resume command.
- 2026-03-08: `session_continuity.py` and session export are currently Claude-specific, and `cloud_executor.py` only forwards `resume_session_id` for Claude backends.

---

## [Launch] Runner onboarding hardening to 100 (size: 4)

Status (2026-03-08): Standard CI is green, `cinder` + `clifford` installs are complete, and the disposable `cube` VM now proves Linux `server` install -> reboot -> re-enroll -> `exec.full`; remaining work is explicit manual persistence/device proof plus a small amount of polish.

**Goal:** Turn the runner onboarding slice from "credible pre-launch" into something David can trust on launch day across fresh clones, hosted CI, and real machines.

- [x] Fix `tests/onboarding/runner_install_modes.spec.ts` in hosted CI across Chromium, Firefox, WebKit, and mobile emulation
- [x] Make the main CI `oss-qa--fresh-clone--sqlite--demo-serve` job green again
- [ ] Run `workflow_dispatch` coverage for hosted extended (`ubuntu-24.04-arm`, `macos-latest`) and self-hosted (`cube`, `clifford`, macOS) jobs
- [x] Add a disposable Linux VM canary on `cube` that proves `server` install -> reboot -> Oikos reconnect without touching shared hosts
- [ ] Finish literal persistence proof on the real machines: logout/restart on `cinder`, and either explicit reboot proof on `clifford` or an intentional decision that the `cube` reboot canary is sufficient for Linux
- [ ] Verify Telegram can run `hostname` on the newly installed runners (`Oikos` already proved `cinder`, `clifford`, and the `cube` canary)
- [x] Make the disposable `cube` VM canary prove `exec.full` by promoting capabilities and running a real bash command through Oikos
- [ ] Do final iPhone Safari + Android Chrome spot checks (or BrowserStack/AWS Device Farm equivalent)
- [ ] Triage remaining clean-clone polish warnings: oversized bundle warning and the non-fatal startup `pip install failed` log

Notes:
- 2026-03-08: Current `main` at `1f01a3dd` has standard CI green: `push-pr-ci`, `Test Installer`, `Web Quality`, and `Provisioning E2E`.
- 2026-03-08: `Runner Onboarding Validation Ring` is green on `9794072b`; the later `cube` canary follow-up only touched the disposable VM path and standard CI stayed green around it.
- 2026-03-08: Fixed the re-enroll capability regression: installers now persist `RUNNER_CAPABILITIES`, and `cinder` + `clifford` retained `exec.full` after migration.
- 2026-03-08: Hosted Oikos successfully ran `hostname -s` on both newly migrated runners before and after service restarts, returning `cinder` and `clifford` respectively.
- 2026-03-08: I did not run a literal logout/reboot on `cinder` or `clifford` yet; `cinder` needs an interactive local logout/restart, and rebooting `clifford` is still a deliberate production-impacting choice.
- 2026-03-08: Live probing showed `cube` is `x86_64`, not ARM, so the disposable canary uses Ubuntu `amd64` cloud images there.
- 2026-03-08: `cube` mounts both `/tmp` and `/var/tmp` as 2 GiB tmpfs, so `uvtool` image sync must use a disk-backed temp dir; the host harness now uses `/var/lib/longhouse-vm/tmp`.
- 2026-03-08: Disposable `cube` canary now proves the full hosted contract: Ubuntu `noble` VM -> `RUNNER_INSTALL_MODE=server` install -> reboot -> Oikos `hostname -s` -> promote runner to `exec.full` -> re-enroll -> reboot -> Oikos `bash -lc 'hostname -s'` -> revoke -> destroy.

## [Infra] Honest degraded job status for scheduled jobs (size: 2)

Status (2026-03-08): Done.

**Goal:** Make scheduled jobs report `degraded` honestly when they complete with non-fatal partial failures, and show that clearly in downstream reporting.

- [x] Derive `degraded` / reported `failure` from returned job payloads and pipeline summaries in direct + queued execution paths
- [x] Keep queue semantics sane: degraded completes, reported failures still retry/reschedule
- [x] Update downstream reporting so degraded runs are visible instead of silently counted as green

Notes:
- 2026-03-08: Current ai-tools editorial loop returns successful process exit with pipeline summary `status=error` for partial failures; scheduler ignores that and stores the top-level run as success.
- 2026-03-08: Shipped scheduler + queue status promotion, Jobs UI degraded badges, and ai-tools digest degraded counts so partial-failure runs stop showing up as green.
- 2026-03-08: Direct `job_registry.run_job()` now also emits `JobRun` rows with degraded/failure derivation, so manual triggers and non-queue APScheduler jobs show up in the same downstream reporting path as queued jobs.

## [Infra] Infisical-first personal ops secrets without OSS lock-in (size: 3)

Status (2026-03-08): Done.

**Goal:** Make Infisical the real source of truth for David's personal/private ops secrets without baking it into Longhouse's public repo/runtime contract.

- [x] Add a script-safe Infisical secret helper with exact-key lookup and loud failure on missing/empty values
- [x] Keep Longhouse hosted-control-plane helpers env-driven so self-hosters can use any secret manager they want
- [x] Migrate the live control-plane admin token into Infisical `ops-infra` and validate private helper reads + hosted instance resolution
- [x] Update global agent/docs guidance so future agents treat Keychain as legacy-only and keep public repos provider-agnostic

Notes:
- 2026-03-08: `~/.zshrc` already uses `infisical export` for common shell keys, but `CONTROL_PLANE_ADMIN_TOKEN` is not globally exported and long-lived admin tokens should stay fetched on demand.
- 2026-03-08: `infisical secrets get` can exit successfully with empty output for missing secrets, so private scripts now use the stricter shared helper `~/git/me/scripts/infisical-get.py`.
- 2026-03-08: Course-corrected after review: Longhouse repo scripts no longer auto-load control-plane auth from Infisical; repo code expects normal env vars and leaves secret sourcing to the operator.

## [QA/Test] Full verification sweep and CI follow-through (size: 2)

Status (2026-03-08): Done for the current repo state.

**Goal:** Run the full local verification surface plus the matching GitHub Actions workflows, then fix any real failures instead of carrying speculative cleanup debt.

- [x] Run the main local verification gates (`make test`, `make test-e2e`, `bun run validate:all`)
- [x] Kick off the matching GitHub Actions CI workflows and watch them to completion
- [x] Fix any concrete failures found and re-run only the affected surfaces until clean

Notes:
- 2026-03-07: This sweep intentionally used the real local gates and the live GitHub workflows rather than reasoning from repo structure alone.
- 2026-03-08: Current `main` at `1f01a3dd` has green `push-pr-ci`, `Test Installer`, `Web Quality`, and `Provisioning E2E`.
- 2026-03-08: Exact local gates also passed on this checkout: `make test`, `make test-e2e`, and `bun run validate:all`.
- 2026-03-08: Any remaining warnings are polish items now tracked under runner onboarding hardening, not active CI/test breakage.


## [Tech Debt] Remove secret-shaped dev bootstrap literals (size: 1)

Status (2026-03-08): Done.

**Goal:** Stop GitGuardian noise from committed dummy auth/encryption values while keeping OpenAPI/typegen bootstrap self-contained.

- [x] Replace committed Fernet-format bootstrap values with runtime-generated ephemeral secrets
- [x] Route OpenAPI export through one backend script instead of duplicating inline bootstrap env logic
- [x] Remove Fernet-looking placeholders from example/test files that could retrigger incidents on future edits

Notes:
- 2026-03-08: Commit `09586ae` introduced a Fernet-shaped dummy value for OpenAPI export; this cleanup removes that pattern entirely instead of adding dashboard ignores.
- 2026-03-08: Canonical OpenAPI export entrypoint is now `apps/zerg/backend/scripts/export_openapi.py`.

## [Infra] Restore Oikos direct runner terminal access (size: 2)

Status (2026-03-07): Done.

**Goal:** Let the `david010` instance use connected runners directly from Oikos/Telegram for bash terminal access, while keeping commis delegation for heavier work.

- [x] Let Oikos call `runner_exec` with normal authenticated user context (not only commis context)
- [x] Update the Oikos prompt/allowlist so it knows direct runner commands are available for lightweight shell work
- [x] Verify the live `david010` runners (`cube`, `clifford`, `cinder`) are configured for `exec.full` and only one daemon instance each

Notes:
- 2026-03-07: Live inspection found all three runners online, but every runner is still `exec.readonly`, which blocks bash access.
- 2026-03-07: `cube`, `clifford`, and `cinder` each currently have duplicate runner daemon instances, causing repeated websocket replacement churn.
- 2026-03-07: Shipped `Restore Oikos runner terminal access` to `main`, passed `make test` + `make qa-live`, redeployed runtime image, and reprovisioned `david010`.
- 2026-03-07: Live Oikos smoke checks succeeded with `runner_exec` on `clifford`, `cube`, and `cinder` using `bash -lc hostname`.
- 2026-03-07: Follow-up smoke still showed Oikos sometimes *claiming* `cube` was offline without calling tools; live DB showed it online, so prompt guidance now explicitly requires `runner_list` verification before any offline claim.

## [Infra] Runner connectivity v1 (size: 4)

Status (2026-03-08): Phase 1 shipped to main; launch-hardening follow-through remains.

## [Launch] Runner doctor + repair UX (size: 3)

**Status (2026-03-08): Core v1 is shipped, deployed, and live-validated. The reconnect follow-up is also fixed and shipped in `runner-v0.1.3`.**

**Goal:** Make runner failures obvious and fixable without teaching users service-manager trivia.

- [x] Write first-principles spec for `doctor` + repair UX
- [x] Add per-runner doctor API with reason codes and recommended repair action
- [x] Add `Run Doctor` UI on runner detail with generated repair command
- [x] Add local `longhouse-runner doctor` command for machine-side checks
- [x] Validate the v1 on `david010`, `cinder`, and the disposable `cube` VM canary

Notes:
- 2026-03-08: Keep v1 diagnose-first. Avoid hidden self-healing or a large fleet-management surface.
- 2026-03-08: Prefer one repair path: regenerate the correct reinstall/re-enroll command for the existing runner name and install mode.
- 2026-03-08: Shipped `GET /api/runners/{id}/doctor`, `Run Doctor` on the runner detail page, and `longhouse-runner doctor --json` in the runner binary.
- 2026-03-08: Repair command generation reuses `POST /api/runners/enroll-token` plus the existing runner name; no bespoke repair mutation API was added in v1.
- 2026-03-08: Local validation passed via backend tests, runner Bun tests, frontend typecheck, frontend vitest, and a real CLI smoke with `longhouse-runner doctor --json`.
- 2026-03-08: Important shipping quirk: `bun run src/index.ts doctor` works now, but already-installed compiled runner binaries will not expose `doctor` until the next runner release is built and users reinstall/update the binary.
- 2026-03-08: Live validation passed on `david010`: `clifford` defaults repair to `server`, `cinder` defaults repair to `desktop`, and both generated commands now preserve `RUNNER_NAME`.
- 2026-03-08: Fresh `cube` VM validation confirmed `longhouse-runner doctor --json` is healthy on runner `v0.1.3` with `installMode=server` after install.
- 2026-03-08: Shipped `runner-v0.1.3` with an explicit websocket connect watchdog in the runner client so boot-time/re-enroll handshakes cannot hang forever before `hello`.
- 2026-03-08: Re-ran the disposable `cube` exec.full canary after the `runner-v0.1.3` deploy; it passed end-to-end (install -> reboot -> re-enroll -> reboot -> Oikos `bash -lc`).
- 2026-03-08: Simplified the runner websocket close path with a best-effort close helper; early disconnects before `hello` now log cleanly without noisy double-close errors.
- 2026-03-08: One canary rerun failed earlier due to `uvt-simplestreams-libvirt sync` timing out on `cube`; rerunning succeeded, so that was infra flake, not a product regression.

**Goal:** Make runner installs reliable across laptops and always-on Linux machines while keeping Longhouse runner-first and SSH optional for power users.

- [x] Write the connectivity/design spec and keep it updated with implementation progress
- [x] Add Linux install modes (`desktop`, `server`) to the live install script
- [x] Add backend tests for the served install script contract
- [x] Surface the right install commands in the UX without requiring users to understand `loginctl`

Notes:
- 2026-03-07: Research decision is runner-first, SSH-optional. The immediate reliability gap is Linux always-on installs using `systemd --user`.
- 2026-03-07: The live install script is `apps/zerg/backend/zerg/routers/templates/install.sh`; `apps/runner/scripts/install.sh` is a sibling copy and should stay aligned.
- 2026-03-07: Shipped `RUNNER_INSTALL_MODE=desktop|server`, added install-script tests, and updated Add Runner / chat runner setup UI to expose the machine type choice directly.
- 2026-03-07: Validation passed with `make test`, `uv run pytest tests_lite/test_runner_install_script.py -q`, `bash -n` on both installer scripts, and `bun run validate:types`. Frontend lint still reports unrelated pre-existing warnings elsewhere in the app.
- 2026-03-07: Removed the stale `apps/runner/scripts/install-linux.sh` helper because it was unused and still advertised the old linger-based Linux flow.

## [QA/Test] Solo-dev runner onboarding validation ring (size: 4)

Status (2026-03-08): Core hosted + fresh-clone coverage is green; `cinder`/`clifford` installs and the disposable `cube` Linux reboot canary are validated, and the remaining work is the explicit extended/manual proof ring.

**Goal:** Catch onboarding regressions across browser, OS, and hardware before beta users ever see them.

- [x] Add Playwright onboarding projects for Chromium, Firefox, WebKit, and mobile emulation with trace capture
- [x] Add GitHub Actions matrix jobs for hosted OS coverage and scheduled/manual synthetic onboarding runs
- [x] Add labeled self-hosted hardware smoke jobs for macOS arm64 and Linux x64, plus hosted Linux arm64 coverage
- [x] Add a tiny release-candidate real-device checklist for iPhone Safari and Android Chrome via a cloud device lab
- [x] Make hosted `onboarding--ubuntu-latest--all` green in GitHub Actions
- [x] Make the shared fresh-clone `oss-qa` onboarding UI path green in main CI
- [ ] Record first green `workflow_dispatch` runs for extended hosted + self-hosted coverage
- [ ] Finish the real install reality check on desktop + server hardware

Notes:
- 2026-03-07: Keep the matrix intentionally small and risk-based; cover one representative machine per failure class.
- 2026-03-07: Use cloud real-device sessions sparingly for pre-launch spot checks; rely on automation and David-owned canary hardware day-to-day.
- 2026-03-07: Keep macOS hosted coverage selective because GitHub-hosted macOS minutes cost much more than Linux in private repos.
- 2026-03-07: Implemented `make test-e2e-onboarding`, expanded the Playwright onboarding config, added `/runners` install-mode coverage, and added the workflow/checklist scaffolding for hosted + self-hosted validation.
- 2026-03-07: Fixed `make onboarding-funnel` so the README contract now runs the onboarding Playwright smoke instead of only checking `/api/health`.
- 2026-03-07: The first fresh-clone passes uncovered real bootstrap issues (`make`, `uv`, frontend build, `bun-types`, and longer startup budget); those are all now folded into the harness.
- 2026-03-08: Hosted browser ring and fresh-clone onboarding smoke are green in GitHub Actions.
- 2026-03-08: Root cause for the fresh-clone onboarding break was `POST /api/runners/enroll-token` deriving bad URLs when `APP_PUBLIC_URL` was unset; local/demo flows now derive `longhouse_url` from `request.base_url`, with regression coverage.
- 2026-03-08: `contract-first-ci` now pins `ONBOARDING_PLAYWRIGHT_PROJECT=onboarding-chromium` so its lightweight fresh-clone smoke matches the browsers it actually installs.
- 2026-03-08: Real-machine validation is partly done: `cinder` desktop and `clifford` server installs both succeeded with post-restart Oikos hostname checks. The remaining manual proof is literal logout/reboot behavior plus the first green extended `workflow_dispatch` runs.
- 2026-03-08: Disposable `cube` VM canary passed the full exec proof without touching `clifford`: install -> reboot -> Oikos hostname -> promote to `exec.full` -> re-enroll -> reboot -> Oikos `bash -lc` -> revoke -> destroy.

## [Docs/Drift] Docs retention prune (size: 3)

Status (2026-03-06): Done.

**Goal:** Cut the repo doc surface by about 80% so only canonical docs remain in-tree.

- [x] Commit a retention policy spec with explicit keep/delete rules
- [x] Delete transient and historical docs (handoffs, plans, reports, completed specs)
- [x] Delete duplicated operator docs after folding any tiny must-keep guidance into canonical docs
- [x] Verify the final markdown set is roughly 8-10 files and references are updated

Notes:
- 2026-03-06: Source of truth after this prune is `README.md`, `VISION.md`, `AGENTS.md`, `TODO.md`, `apps/control-plane/README.md`, `apps/control-plane/API.md`, `apps/runner/README.md`, and `apps/zerg/backend/README.md`.
- 2026-03-06: Bundled `SKILL.md` files under `apps/zerg/backend/zerg/skills/bundled/` were intentionally kept; they are runtime assets, not repository docs.
- 2026-03-06: Git history is the archive. Completed specs, plans, handoffs, and reports were removed instead of archived.

## [Docs/Drift] Remove Sauron README build dependency (size: 1)

Status (2026-03-07): Done.

**Goal:** Delete `apps/sauron/README.md` by removing the last packaging and Docker build-time references to it.

- [x] Remove `readme = "README.md"` from both Sauron pyproject files
- [x] Stop copying `apps/sauron/README.md` in the Dockerfile
- [x] Delete the file and verify local package + Docker builds still work

Notes:
- 2026-03-07: This was the last non-canonical repo doc kept only because the build expected it.
- 2026-03-07: Validation passed with `cd apps/sauron && uv build` and `docker build -f apps/sauron/Dockerfile -t sauron-readme-prune:test .`.

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

## [Tech Debt] Remove Sauron Docker pyproject duplication (size: 1)

Status (2026-03-07): Done.

**Goal:** Delete `apps/sauron/pyproject.docker.toml` and derive the Docker build manifest from the real `apps/sauron/pyproject.toml`.

- [x] Make the Docker build transform `apps/sauron/pyproject.toml` instead of copying a second manifest
- [x] Delete `apps/sauron/pyproject.docker.toml`
- [x] Verify the Docker build still succeeds

Notes:
- 2026-03-07: The only semantic difference was the dependency name swap from `longhouse` to the locally-built `zerg` package. The Dockerfile now rewrites that one dependency and drops the unused `tool.uv.sources` block at build time.
- 2026-03-07: Validation passed with `docker build -f apps/sauron/Dockerfile -t sauron-pyproject-dedupe:test .`.

## [Tech Debt] Delete dead frontend contract wrapper (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove `scripts/validate-frontend-contracts.sh` and keep `scripts/fast-contract-check.sh` as the single repo-level frontend contract entrypoint.

- [x] Confirm all live refs already use `fast-contract-check.sh` or `bun run validate:contracts`
- [x] Delete the dead wrapper script
- [x] Keep any tiny durable guidance in canonical files only if needed

Notes:
- 2026-03-07: `validate-frontend-contracts.sh` had no live references in hooks, make, package scripts, or CI.
- 2026-03-07: Validation passed with `./scripts/fast-contract-check.sh` after deleting the wrapper.

## [Tech Debt] Delete dead Sauron compose file (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove `apps/sauron/docker-compose.yml`, which appears to be a stale standalone deployment path.

- [x] Confirm there are no repo references to `apps/sauron/docker-compose.yml`
- [x] Confirm there is no live `sauron` container still using this repo deployment path
- [x] Delete the file

Notes:
- 2026-03-07: Confirmed `apps/sauron/docker-compose.yml` was a dead path and safe to delete at the time.
- 2026-03-08: Architecture changed again during extraction: the active standalone runtime now lives in the `~/git/sauron/` workspace (runtime repo under `runtime/`) on `clifford`, and Longhouse is back to builtin jobs only.

## [Tech Debt] Delete stale helper wrappers (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove old helper scripts that just wrap `make` or encode retired setup/test flows.

- [x] Delete clearly stale wrapper scripts with no repo references
- [x] Keep only the canonical `make` or maintained script entrypoints
- [x] Verify the remaining canonical path still works

Notes:
- 2026-03-07: Deleted `scripts/run_all_tests.sh`, `scripts/run-smoke-tests.sh`, `scripts/validate-setup.sh`, and `scripts/design-verify.sh`. They were unreferenced and pointed at old flows or retired architecture.
- 2026-03-07: Validation passed with `make test`, which exercised the supported test entrypoint directly.

## [Tech Debt] Delete old schema-first API leftovers (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove the unused schema-first API generation path that no longer feeds the current OpenAPI-based frontend/backend contracts.

- [x] Delete the unused generators and generated file
- [x] Delete the now-unreferenced `schemas/api-schema.yml`
- [x] Verify no live repo refs remain

Notes:
- 2026-03-07: Deleted `scripts/generate-complete-contracts.py`, `scripts/generate-api-client.py`, `apps/zerg/backend/zerg/generated/api_models.py`, and `schemas/api-schema.yml`.
- 2026-03-07: Verification was a repo-wide ref search; the only remaining mentions after deletion were the TODO notes for this cleanup itself.

## [Tech Debt] Inline fast contract validation (size: 1)

Status (2026-03-07): Done.

**Goal:** Delete `scripts/fast-contract-check.sh` and call the real frontend contract validator directly from the remaining entrypoints.

- [x] Update `package.json` and `scripts/run-ci-tests.sh` to invoke the canonical validator directly
- [x] Delete `scripts/fast-contract-check.sh`
- [x] Verify both entrypoints still work

Notes:
- 2026-03-07: The wrapper only `cd`'d into `apps/zerg/frontend-web` and ran `bun run validate:contracts`.
- 2026-03-07: Validation passed with `cd apps/zerg/frontend-web && bun run validate:contracts` and `./scripts/run-ci-tests.sh`.
- 2026-03-07: `bun run validate:all` still fails earlier in the existing CSS-class audit (`validate:css`), which is unrelated to this wrapper removal.

## [Tech Debt] Delete broken CSS class validator (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove `scripts/validate-css-classes.js`, which is a regex-based JSX parser producing hundreds of false positives, and point `validate:all` at the real validation path.

- [x] Delete the CSS class validator and the `validate:css` package script
- [x] Make `validate:all` call the canonical validation path instead
- [x] Verify `make validate` and `bun run validate:all` pass

Notes:
- 2026-03-07: The old validator invented garbage class names like `.`, `===`, `&&`, and template fragments. `make validate` never used it; it only broke the root package script.
- 2026-03-07: While switching to the real validation path, `make validate` surfaced two legitimate issues: missing `.PHONY` entries in `Makefile` and `networkidle` waits in `apps/zerg/e2e/tests/live/frontend_api_contract.spec.ts`. Both are fixed now.

## [Tech Debt] Inline test-ci wrapper (size: 1)

Status (2026-03-07): Done.

**Goal:** Delete `scripts/run-ci-tests.sh` and keep `test-ci` in `Makefile` as the single supported entrypoint.

- [x] Inline the wrapper commands into `Makefile:test-ci`
- [x] Delete `scripts/run-ci-tests.sh`
- [x] Verify `make test-ci` still works

Notes:
- 2026-03-07: The script is only referenced by `Makefile:test-ci`; it is a wrapper, not a shared primitive.
- 2026-03-07: While inlining it, `test-ci` was corrected to run the real backend lite suite (`run_backend_tests_lite.sh`) instead of swallowing a call to the nonexistent `run_backend_tests.sh`.

## [Tech Debt] Hosted provision helper cleanup (size: 1)

Status (2026-03-07): Done.

**Goal:** Finish collapsing `scripts/provision-e2e-live.sh` onto `scripts/lib/hosted-instance.sh` so the live provision smoke does not carry its own JSON parsing and control-plane fetch logic.

- [x] Add helper support for create/get instance payloads
- [x] Remove local JSON parsing/helpers from `scripts/provision-e2e-live.sh`
- [x] Verify the live provision smoke still passes end to end

Notes:
- 2026-03-07: The script already uses the shared helper for login-token and deprovision, but it still open-codes create/get parsing with ad-hoc Python snippets.
- 2026-03-07: Added `lh_hosted_create_instance` and `lh_hosted_get_instance`, removed the local JSON/python helpers from `provision-e2e-live.sh`, and re-ran the live provision smoke successfully against `control.longhouse.ai`.

## [Tech Debt] Inline AsyncAPI regen wrappers (size: 1)

Status (2026-03-07): Done.

**Goal:** Delete `scripts/regen-ws-code.sh` and `scripts/regen-sse-code.sh`; keep the real generator invocation in `Makefile` so there is one less shell layer to maintain.

- [x] Inline the WebSocket regen command into `Makefile`
- [x] Inline the SSE regen command into `Makefile`
- [x] Update workflow path filters and verify the regen/validate targets still work

Notes:
- 2026-03-07: These scripts are only thin wrappers around `uv run python ...` in the backend env and are only called from `Makefile` plus one workflow path filter.
- 2026-03-07: Deleted both wrappers, updated `ws-code-drift.yml` to watch the real generator inputs, and re-ran `make regen-ws`, `make validate-ws`, `make regen-sse`, and `make validate-sse` successfully.

## [Tech Debt] Delete legacy marketing screenshot entrypoint (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove the old TypeScript marketing screenshot script and keep the Python/Makefile capture flow as the only supported path.

- [x] Delete `apps/zerg/e2e/scripts/capture-marketing-screenshots.ts`
- [x] Remove the root `capture:screenshots` package script
- [x] Verify `make marketing-list` and `make marketing-validate` still work

Notes:
- 2026-03-07: The TS script is only referenced by the root package script; the maintained flow is `scripts/capture_marketing.py` plus the `marketing-*` Make targets.
- 2026-03-07: Deleted the TS entrypoint, removed the package script, and re-ran `make marketing-list` + `make marketing-validate` successfully.

## [Tech Debt] Delete dead WS validation scripts (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove the old WebSocket/AsyncAPI validation helper scripts that are no longer wired into `Makefile`, CI, or package scripts.

- [x] Delete `scripts/validate-asyncapi.sh`
- [x] Delete `scripts/check_ws_drift.sh`
- [x] Verify no live repo refs remain

Notes:
- 2026-03-07: `make validate-ws` already owns the real drift check; these two scripts are now dead side paths.
- 2026-03-07: Verified with a repo-wide ref search and a passing `make validate-ws` after deletion.

## [Tech Debt] Delete dead empty-schema checker (size: 1)

Status (2026-03-07): Done.

**Goal:** Remove `scripts/check-empty-schemas.sh`, which is dead and misleading, without pretending it is an active CI gate.

- [x] Delete `scripts/check-empty-schemas.sh`
- [x] Remove the redundant skipped empty-schema test block
- [x] Validate the real active contract path still passes

Notes:
- 2026-03-07: The shell script was unreferenced, threshold-based, and stale; the maintained path is `bun run validate:contracts`.
- 2026-03-07: Research result: do **not** wire the global empty-schema check into the active validator yet. `bun run validate:all` immediately found 45 empty-schema endpoints, which proves this was not a real enforced standard.
- 2026-03-07: Lead-dev call: keep the active validator focused on critical frontend contracts, delete the dead side path, and only add repo-wide OpenAPI linting later if we want to pay down those 45 endpoints with a real standards pass.

## [Tech Debt] Prune broken/redundant root package scripts (size: 1)

Status (2026-03-07): Done.

**Goal:** Keep the root `package.json` scripts limited to real, maintained entrypoints instead of stale aliases.

- [x] Remove the broken `zerg` script
- [x] Remove the redundant `verify:react` script
- [x] Verify no live repo refs remain

Notes:
- 2026-03-07: `zerg` points at a nonexistent `make zerg` target, and `verify-single-react.mjs` is invoked directly by the frontend vitest runner, not via the root package script.
- 2026-03-07: Verified there are no remaining repo refs to `bun run zerg`, `bun run verify:react`, or those root package-script keys after the cleanup.

## [Tech Debt] Route CI provision E2E through hosted helper (size: 1)

Status (2026-03-07): Done.

**Goal:** Stop open-coding control-plane instance create/deprovision parsing in `scripts/ci/provision-e2e.sh`; use `scripts/lib/hosted-instance.sh` for the instance lifecycle there too.

- [x] Source `hosted-instance.sh` in the CI provision E2E script
- [x] Replace manual instance create/deprovision API handling with helper calls
- [x] Re-run the provision E2E gate

Notes:
- 2026-03-07: This is the main remaining control-plane instance lifecycle path in `scripts/` that still hand-rolls JSON parsing instead of using the shared helper.
- 2026-03-07: The local CI gate intentionally keeps its fixed `http://127.0.0.1:8000` instance URL; the helper is used for lifecycle actions, but the control plane still returns the canonical hosted URL (`https://ci.longhouse.ai`) even in local publish-port mode.
- 2026-03-07: Validation passed with `make test-provision-e2e` after the helper integration.

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
- 2026-03-08: Simplified follow-up: normalized the handful of stale internal `cp_instances.data_path` rows directly in the live control-plane DB and kept runtime path resolution simple instead of carrying a compatibility shim in product code.
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

Status (2026-03-08): Done.

**Goal:** Make session metadata resilient when multiple shipper paths race, so a generic `production` row can be corrected by a later machine-labeled ingest.

- [x] Update duplicate-ingest handling so existing sessions can self-heal `environment` from generic labels to machine labels
- [x] Fix any remaining ingest payload path that omits `environment`
- [x] Add regression coverage for generic->machine-label correction without regressing the reverse case
- [x] Re-verify live local shipping after disabling obsolete local shipper paths

Notes:
- 2026-03-08: Live evidence showed the active engine eventually re-ingests the same session files with machine labels, but current store behavior leaves the first-created generic `production` session row unchanged.
- 2026-03-08: Added backend self-heal so repeat ingest upgrades generic labels like `production` to machine labels like `cinder`, patched `session_continuity.py` to always send `environment`, disabled the obsolete `io.drose.agent-shipper` LaunchAgent locally, re-signed + restarted `com.longhouse.shipper`, and backfilled the 6 remaining hosted `production` rows after taking a fresh backup at `/data/longhouse.db.pre-cinder-backfill-20260308T171538Z`.

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
