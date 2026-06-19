# Provider Release Proof Roadmap

**Status:** Active roadmap
**Last updated:** 2026-06-19
**Current grand-epic score:** 76/100

This roadmap tracks the migration from one-off provider canaries and release
emails to a full end-to-end release regression CI. The design target is
`docs/specs/universal-agent-harness.md`, but the score here is the larger
release-proofing product, not just the harness plumbing.

## Axes

Do not quote a score without naming the axis.

| Axis | Meaning | Current read |
| --- | --- | ---: |
| Existing Longhouse CI/test maturity | Internal Longhouse confidence before this release-proof epic: parser tests, bridge tests, shipper tests, backend/engine/frontend tests | 45/100 |
| Release-watch proofing before recent work | Sauron release emails plus limited/fake provider checks | 20/100 |
| Release-watch proofing after recent work | Longhouse proof lanes, coverage matrix, baseline tooling, Sauron invocation, universal harness attachment, concrete provider adapter action rows, first real OpenCode no-token e2e lane, OpenCode `interrupt_cancel`, `resume_reattach`, `tool_call_result`, and `live_token_streaming` lanes, Claude provider-live no-token contract plus channel interrupt/steer lanes, Codex managed-session e2e adapter lane, first universal `interrupt_cancel` control lane, first universal `steer_active_turn` lane, first universal `tool_call_result` lane, executable all-provider `pause_request_detect`, `tail_output`, `runtime_phase`, `transcript_binding`, `multi_turn_continuity`, and `crash_timeout_cleanup` scenarios, executable answer-pause Longhouse service proof with live-provider delivery gap, opt-in `terminate_cleanup` scenario with Antigravity unsupported gap, Antigravity `external_event_channel` scenario backed by hook/inbox proof, all-provider explicit `permission_prompt` blocked gap, all-provider `live_token_streaming` adapter lanes, Antigravity hook/inbox e2e adapter lane, universal action/control-surface/session/timeline projection artifacts, action/control baseline diff, executable universal old/new proof-artifact diff, all-provider fake/no-token CLI e2e, hermetic DB ingest proof, provider-live/control DB round-trips, computed maturity rollups from coverage/baseline/universal artifacts, and Sauron baseline-guard consumption of those maturity rollups | 76/100 |
| Universal harness plumbing only | Adapter protocol, concrete provider adapter classes, runner, evidence package, action matrix, control-surface scenario, session/timeline projection scenarios, action/control/explicit-old-new baseline diff, executable `old_new_release_diff` scenario, all-provider fake/no-token CLI e2e, DB ingest scenario, all-provider pause/request, observation, multi-turn, crash-cleanup, and permission-gap scenarios, Antigravity `external_event_channel` scenario, opt-in `terminate_cleanup` scenario, Claude/Codex/OpenCode/Antigravity managed-session e2e promotion, Claude/Codex/OpenCode `interrupt_cancel` scenarios, Claude `steer_active_turn` scenario, Codex/OpenCode `tool_call_result` and `live_token_streaming` scenarios, OpenCode `resume_reattach` scenario, Claude and Antigravity `live_token_streaming` scenarios, proof-artifact attachment, and computed maturity rollups, excluding all-provider-live/staged-old-new/Sauron completion | 97/100 |

The apparent drop from 45 to 25/35 was a denominator change: internal CI
maturity was being compared with the larger release-proofing product. The fair
movement for this epic is release-watch proofing before/after: roughly 20 -> 76.

## Ownership Boundary

Longhouse is the public proof framework. Sauron is David's private release ops
runner.

| System | Owns |
| --- | --- |
| Longhouse | OSS-safe proof engine, universal scenarios, adapter interface, generic adapters, evidence schema, parser/ingest/timeline assertions, baseline accept/status/diff semantics, green/yellow/red classification, public runbook. |
| Sauron | Upstream release watching, private source-review context, private tokens/accounts/machines, token-spending schedules, private evidence/baseline storage, digest routing, email labels, alert policy. |

Sauron may invoke Longhouse and report its artifacts. Sauron should not define
provider compatibility.

## Scorecard

| Area | Points | Current | State |
| --- | ---: | ---: | --- |
| Scope, ownership, and provider set | 10 | 7 | Longhouse/Sauron boundary is documented; providers are Claude Code, Codex/OpenAI, OpenCode, Antigravity |
| Coverage inventory | 10 | 10 | 52 provider/surface rows tracked, computed universal action rows in harness artifacts, and `provider-release-proof-maturity.py` emits coverage/provider/baseline/action-matrix rollups |
| Universal harness architecture | 15 | 15 | Shared runner, concrete provider adapter classes, adapter-owned action rows, evidence packages, universal action/control-surface/session/timeline projection scenarios, DB ingest scenario, pause/question service scenarios, observation/cleanup scenarios, remaining spec-surface scenarios, and proof attachment exist; one real OpenCode e2e lane exists |
| Longhouse proof artifact/core commands | 15 | 13 | Proof artifacts, normalized contracts, action-matrix/control-surface/DB-ingest/maturity artifacts, all-provider fake/no-token CLI e2e, accept/status/diff/maturity commands exist; OpenCode e2e DB artifacts flow through release proof; universal artifacts are comparable but not yet full CI gates |
| Baselines and differential confidence | 15 | 6 | Accepted baseline machinery exists and now compares universal action/control artifacts plus explicit old/new proof artifacts through both the baseline CLI and universal harness; durable/auditable old/new release source of truth is unsettled |
| Sauron private runner/reporting | 10 | 3 | Sauron can call Longhouse lanes and the daily baseline guard consumes Longhouse maturity rollups; private alert/noise policy is not migrated to universal artifacts |
| Provider real e2e migration | 25 | 22 | OpenCode has first real no-token universal e2e lane with provider-live evidence fed through Longhouse DB ingest; OpenCode `interrupt_cancel` now routes to the session.abort canary and DB-ingests abort evidence; OpenCode `resume_reattach` now routes to the process-restart reattach canary and DB-ingests reattach evidence; OpenCode `tool_call_result` now routes to the real-tool canary and DB-ingests tool call/result linkage; OpenCode `live_token_streaming` now routes to a real-print `opencode run --format json` marker canary and DB-ingests marker evidence; Claude provider-live no-token command/channel/PTY contract now DB-ingests through universal `managed_session_e2e`; Claude `interrupt_cancel` now routes to the channel-control canary, proves send/meta steer/SIGINT against an owned fake provider process, and DB-ingests control rows; Claude `steer_active_turn` now routes to the same channel-control canary but evaluates steer evidence directly; Claude `live_token_streaming` now routes to real-print one-shot live-token proof and DB-ingests marker evidence; Codex managed-session e2e calls the existing Codex canary and reports Runtime Host credential gaps explicitly; Codex `interrupt_cancel` now routes to the managed-live-interrupt canary and reports credentials gaps explicitly; Codex `tool_call_result` now routes to the real-tool canary and DB-ingests tool call/result linkage; Codex `live_token_streaming` now routes to managed live-send and reports credential gaps explicitly; Antigravity hook/inbox e2e calls provider-control and DB-ingests external-event evidence; Antigravity `live_token_streaming` now routes to real-agy hook-inbox injection and DB-ingests marker evidence; cross-provider managed live send/steer and live answer-pause delivery remain incomplete |
| **Total** | **100** | **76** |  |

## Provider-agnostic Phases

### Phase 1: Universal Harness Design

Status: in progress.

Deliverables:

- Longhouse/Sauron boundary documented.
- Universal agent harness contract documented.
- Scenario statuses, evidence package, baseline/diff rules, and scoring model
  documented.
- Current one-off tests crosswalked to universal scenarios.

Done when a fresh agent can read the spec and know what Phase 2 must build
without inventing provider-specific deliverables.

### Phase 2: Runner Skeleton And First Scenarios

Status: MVP implemented.

Implemented:

- Adapter protocol/data classes.
- Concrete provider adapter classes for Claude Code, Codex/OpenAI, OpenCode,
  and Antigravity.
- Scenario result schema.
- Shared evidence package writer.
- Universal scenarios for `probe_identity`, `collect_raw_evidence`,
  `action_matrix`, `parse_ingest_project`, and typed-unsupported
  `run_prompt_once`.
- MVP adapters for Claude Code, Codex/OpenAI, OpenCode, and Antigravity.
- CLI entrypoint: `scripts/qa/universal-agent-harness.py`.

Remaining before this phase is production-grade:

- Replace provider-specific live/proof canaries scenario by scenario.
- Add computed maturity rollups from runner artifacts.

Original deliverables:

- Adapter protocol/data classes.
- Scenario result schema.
- Shared evidence package writer.
- Universal scenarios for `probe_identity`, `collect_raw_evidence`, and fixture
  `parse_ingest_project`.
- Two adapters wired first, preferably Codex and OpenCode, to prove the runner
  is not Claude-shaped.

Done when adding a provider means implementing adapter methods, not adding
provider branches to the scenario runner.

### Phase 3: Managed Session Scenarios

Status: first migration slice implemented.

Implemented:

- `provider-release-proof.py` can run `scripts/qa/universal-agent-harness.py`
  with `--run-universal-harness`.
- Release-proof artifacts include universal harness raw artifact paths,
  normalized universal summary/canaries, and prefixed universal operation
  evidence.
- `probe_identity`, `collect_raw_evidence`, `parse_ingest_project`,
  `action_matrix`, `control_surface`, `run_prompt_once`, `launch_managed_session`, and
  `send_receive` can be included in the release-proof output.
- `action_matrix` emits the same 23 Longhouse action ids for every provider:
  identity, launch, run-once, session identity, send, steer, pause
  detect/answer, interrupt/cancel, resume/reattach, terminate, tail/runtime,
  transcript/tool-result, raw capture, parse, DB ingest, projections, baseline
  compare, and old/new release diff.
- `control_surface` emits the same control/observation subset for every
  provider: launch, run, session identity, send, steer, pause detect/answer,
  interrupt, resume, terminate, tail, runtime phase, transcript binding, and
  tool-call/result rows.
- `provider-release-proof.py --run-universal-harness` now captures the action
  matrix and control surface in normalized artifacts and exposes status counts.
- `scripts/qa/universal-agent-harness.py` has an all-provider fake/no-token CLI
  e2e test that runs `action_matrix` plus `control_surface` for Claude Code,
  Codex/OpenAI, OpenCode, and Antigravity in one command and verifies
  comparable artifact paths and action ids.
- `provider-release-proof-baseline.py` compares stable `action_matrix` and
  `control_surface` rows, so baseline drift can catch universal action status
  regressions without diffing volatile evidence paths.
- `db_ingest_project` ingests canonical events into an isolated SQLite
  Longhouse DB through `AgentsStore.ingest_session` and verifies durable
  session events, counts, export JSONL, and timeline reads. This promotes the
  action-matrix `db_ingest` row to hermetic proof, not live provider-token
  proof.
- `session_projection` and `timeline_projection` are first-class universal
  scenarios for all four providers. They emit comparable canonical event,
  session-detail, timeline/card, and operation-evidence artifacts and are
  included in default `provider-release-proof.py --run-universal-harness`
  runs.
- Codex/OpenAI and OpenCode expose first no-token/session-safe
  `launch_managed_session` and `send_receive` projections through the universal
  runner.
- OpenCode exposes the first real no-token universal `managed_session_e2e`
  scenario, backed by the existing provider-live canary's server/session/schema,
  `prompt_async noReply`, reattach, transcript, and abort checks.
- The OpenCode `managed_session_e2e` lane feeds those provider-live raw rows
  through isolated Longhouse SQLite ingest, then verifies durable events,
  session counts, export JSONL, query lookup, timeline listing, and preserved
  provider-session binding.
- Codex exposes a `managed_session_e2e` adapter lane that calls the existing
  Codex provider-release canary for `managed_tui_attach` and `detached_ui`,
  projects those rows, and feeds them through isolated Longhouse SQLite ingest.
  Missing Runtime Host credentials remain a typed `unsupported_gap`.
- Antigravity exposes a `managed_session_e2e` adapter lane that calls the
  provider-control hook/inbox canary, projects external-event channel rows, and
  feeds them through isolated Longhouse SQLite ingest. This proves hook/inbox
  delivery, not interrupt/reattach/tool support.
- Claude exposes a provider-safe `managed_session_e2e` adapter lane that calls
  the provider-live no-token command/channel/PTY contract, projects those rows,
  and feeds them through isolated Longhouse SQLite ingest. Live send and steer
  remain explicit `blocked` operations that require the live-token contract lane.
- `interrupt_cancel` is now an executable universal scenario for Claude, Codex,
  and OpenCode. Claude routes it to the provider-control channel canary, proving
  normal send metadata, steer metadata, and SIGINT delivery against an owned
  fake provider process without model-token spend. Codex routes it to the
  existing managed-live-interrupt canary and DB-ingests interrupt evidence when
  configured; without Runtime Host credentials it returns an explicit
  `unsupported_gap`. OpenCode routes it to the provider-live session.abort
  canary and DB-ingests abort evidence. Antigravity still returns a typed
  adapter gap.
- `tool_call_result` is now an executable universal scenario. Codex routes it
  to the existing real-tool canary; OpenCode routes it to the existing
  provider-control real-tool canary. Both project command execution plus tool
  output and final assistant response rows, then DB-ingest the call/result
  linkage. Other providers still return typed adapter gaps.
- `live_token_streaming` is now an executable universal scenario for all four
  providers. Claude routes it to the real-print one-shot canary; Codex routes it
  to the existing managed-live-send canary; OpenCode routes it to a real-print
  `opencode run --format json` marker canary; Antigravity routes it to the
  real-agy hook-inbox injection canary. These paths project marker rows and
  DB-ingest live-token behavior evidence when their live lane is configured.
- `resume_reattach` is now an executable universal scenario. OpenCode routes it
  to the provider-live process-restart reattach canary and DB-ingests reattach
  evidence. Other providers still return typed adapter gaps.
- `pause_request_detect` is now an executable universal scenario for all four
  providers. It drives Longhouse runtime `needs_user` plus structured
  `pause_request` events through an isolated SQLite DB, verifies the active
  pause-request projection, and is included in default
  `provider-release-proof.py --run-universal-harness` runs.
- `answer_pause_request` is now an executable universal scenario. Claude and
  Codex prove the Longhouse answer/resolve service hermetically, then report a
  typed `blocked` live-provider delivery gap; OpenCode and Antigravity report
  typed `unsupported_gap` results until they expose answer-pause semantics.
- `tail_output`, `runtime_phase`, and `transcript_binding` are executable
  universal observation scenarios for all four providers. They are included in
  default release-proof universal runs and emit comparable raw/canonical
  event, session, timeline, and operation-evidence artifacts; `runtime_phase`
  also drives the Longhouse runtime reducer through isolated SQLite.
- `terminate_cleanup` is an executable universal scenario. Claude, Codex, and
  OpenCode project a provider-neutral cleanup/terminal event; Antigravity
  reports the contract-defined `unsupported_gap`.
- `multi_turn_continuity` and `crash_timeout_cleanup` are executable universal
  scenarios for all four providers. They emit comparable canonical projection
  artifacts; crash/timeout also writes diagnostics proving no owned process is
  left behind.
- `external_event_channel` is an executable universal scenario. Antigravity
  routes it to the existing provider-control hook/inbox canary and DB-ingests
  the resulting external-event evidence; other providers report typed
  `unsupported_gap` results.
- `permission_prompt` is an executable universal scenario that currently
  reports a typed `blocked` live canary gap for every provider. It is visible in
  the action matrix and selectable in release proof, but approve/deny delivery
  remains future work.
- Unsupported unsafe scenarios remain explicit `unsupported_gap` results for
  providers that do not yet have a safe universal adapter lane.
- `old_new_release_diff` is an executable universal scenario. It is blocked
  without explicit proof artifacts, compares old/new proof artifacts through
  `provider-release-proof-baseline.py old-new`, and can be attached to
  `provider-release-proof.py --run-universal-harness`.
- Automatic staged old/new provider install and live-token behavior remain
  explicit future gates.

Deliverables:

- Universal `run_prompt_once`, `launch_managed_session`, `send_receive`, and
  `timeline_projection` scenarios.
- Existing one-off launch/send/session tests migrate behind adapters.
- Provider-specific mechanics stay behind adapters: PTY, app-server, server
  schema, hooks/inbox.

Done when the same scenario ids produce comparable artifacts for every provider
that declares the required capabilities.

### Phase 4: Control And Live-token Scenarios

Implemented:

- Universal `interrupt_cancel`, `resume_reattach`, `multi_turn_continuity`,
  `tool_call_result`, `live_token_streaming`, `external_event_channel`, and
  `crash_timeout_cleanup` scenarios exist as selectable universal harness
  lanes with pass/fail/unsupported/blocked statuses.

Remaining:

- Claude machine-live diagnostics are fixed as part of the Claude adapter lane,
  not as a separate product shape.
- Permission-prompt approve/deny delivery still needs provider-held canaries.

Done when every provider/capability row is either pass, fail, unsupported gap,
not applicable, blocked, flaky, or xfail with expiry.

### Phase 5: Durable Baselines And Old/New Diff

Deliverables:

- Durable accepted-baseline source of truth selected.
- `baseline-status-all` reproducible from that source.
- Baselines scoped by provider, version/channel, adapter version, scenario id,
  profile, platform, and fixture hash.
- Old/new staged release diff consumes universal scenario artifacts.

Done when a release candidate can be compared against an accepted baseline
without relying on host-local tribal knowledge.

### Phase 6: Sauron Private Runner And Alert Policy

Status: started.

Implemented:

- Sauron's `agent-release-baseline-guard` attaches Longhouse's
  `provider_release_proof_maturity_rollup` beside accepted-baseline status, so
  private release ops can report coverage/completion evidence without owning
  the scoring logic.

Deliverables:

- Sauron stages releases and invokes Longhouse universal proof lanes.
- Sauron stores private artifacts/baselines where needed.
- Digest separates actionable red, concrete yellow, infra blocked, known
  unsupported gaps, and green no-action results.
- Release notes are attached only as explanation.

Done when release emails are low-noise evidence reports rather than AI-written
release-note summaries.

## Adapter Progress

Provider lane scores describe migration state, not separate deliverables. The
deliverable is the same for every provider: implement the universal adapter
contract for the provider's declared profile and run the same scenario corpus.

| Provider | Adapter migration score | Current state | Next migration gate |
| --- | ---: | --- | --- |
| Claude Code | 8/10 | MVP adapter runs safe universal scenarios; universal `managed_session_e2e` now calls provider-live no-token command/channel/PTY checks and DB-ingests them; universal `interrupt_cancel` calls the channel-control canary and DB-ingests no-token send/steer/SIGINT rows; universal `steer_active_turn` evaluates that steer evidence directly; universal `live_token_streaming` calls the real-print one-shot live-token canary and DB-ingests marker evidence; managed live-token send still needs promotion | Promote managed Claude live-token send into universal scenarios with bounded failure artifacts |
| Codex/OpenAI | 9/10 | MVP adapter runs safe universal scenarios; strongest existing lane has staged asset proof, managed live-send/interrupt, real-tool scenarios, accepted baselines; universal `managed_session_e2e` now calls the Codex provider-release canary and DB-ingests launch/reattach evidence when Runtime Host credentials are present; universal `interrupt_cancel` calls the managed-live-interrupt canary and reports credentials gaps explicitly; universal `tool_call_result` calls the real-tool canary and DB-ingests call/result linkage; universal `live_token_streaming` calls managed live-send and reports Runtime Host credential gaps explicitly | Replace remaining Codex one-off release-watch invocations with universal scenarios and add staged old/new install proof |
| OpenCode | 10/10 | First real no-token universal e2e lane calls the provider-live server/session/schema/prompt_async/reattach/abort canary, projects canonical evidence, and round-trips it through Longhouse DB ingest; universal `interrupt_cancel` now calls the session.abort canary directly; universal `resume_reattach` now calls the process-restart reattach canary directly; universal `tool_call_result` now calls provider-control real-tool and DB-ingests call/result linkage; universal `live_token_streaming` now calls provider-control real-print and DB-ingests marker rows | Promote staged old/new release diff for OpenCode in the release runner |
| Antigravity | 7/10 | MVP adapter runs safe universal scenarios; no-token hook/plugin baseline and live-send baseline exist; universal `managed_session_e2e` now calls provider-control hook/inbox and DB-ingests external-event evidence; universal `live_token_streaming` now calls real-agy hook-inbox injection and DB-ingests marker evidence | Keep interrupt/reattach/tool gaps explicit and add staged old/new release proof |

## Active Task List

Update this list after each substantial slice. A task is done only when the
evidence path is recorded and the relevant doc, test, or proof command exists.

| ID | Task | Status | Score impact | Evidence |
| --- | --- | --- | ---: | --- |
| H1 | Keep provider scope post-Gemini: Claude Code, Codex/OpenAI, OpenCode, Antigravity | Done | +2 | `provider-release-proof-coverage.json`, runbook provider list |
| H2 | Maintain honest 52-row provider/surface coverage matrix | Done | +8 | `docs/specs/provider-release-proof-coverage.json`, coverage tests |
| H3 | Document Longhouse public proof framework vs Sauron private release runner boundary | Done | +3 | `docs/specs/universal-agent-harness.md` |
| H4 | Define Universal Agent Harness adapter contract, capabilities, profiles, statuses, evidence package, baseline/diff, scoring | Done | +5 | `docs/specs/universal-agent-harness.md` |
| H5 | Crosswalk current one-off tests/canaries to universal scenarios | Done | +3 | `docs/specs/universal-agent-harness.md` |
| H6 | Replace roadmap provider-specific phases with provider-agnostic phases | Done | +2 | This roadmap |
| H7 | Add computed maturity/score rollups from coverage + scenario/baseline state | Done | +4 | `scripts/qa/provider-release-proof-maturity.py`, `make provider-release-proof-maturity`, and maturity tests emit coverage, baseline, and universal action-matrix ratios |
| H8 | Implement adapter protocol and scenario result schema | Done | +6 | `server/zerg/qa/universal_agent_harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H9 | Implement shared evidence package writer | Done | +5 | `server/zerg/qa/universal_agent_harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H10 | Implement first universal scenarios: `probe_identity`, `collect_raw_evidence`, fixture `parse_ingest_project` | Done | +6 | `scripts/qa/universal-agent-harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H11 | Wire first two adapters through the runner | Done | +6 | MVP adapters now cover all four providers in `server/zerg/qa/universal_agent_harness.py` |
| H12 | Migrate managed launch/send/timeline scenarios | Partial | +5 | Session/timeline projection scenarios now run for all four providers and attach to release-proof output; Codex/OpenCode universal `launch_managed_session` and `send_receive` artifacts remain the managed-send slice |
| H13 | Migrate control/live-token/tool/resume scenarios | Partial | +10 | `control_surface` exposes comparable control/observe action rows; all-provider live-token lanes, Codex/OpenCode tool lanes, Claude/Codex/OpenCode interrupt, and OpenCode resume are promoted; remaining providers/actions stay explicit gaps |
| H14 | Decide durable accepted-baseline source of truth | Not started | +5 | Future documented store and reproducible status artifact |
| H15 | Update Sauron to invoke universal lanes and apply private alert policy | Partial | +1 | Sauron baseline guard now consumes Longhouse maturity rollups; digest thresholds and alert/noise policy remain future |
| H16 | Feed universal runner output into `provider-release-proof.py` | Done | +4 | `scripts/qa/provider-release-proof.py`, `scripts/tests/provider-release-proof.test.py` |
| H17 | Add first real provider-safe universal e2e lane | Done | +4 | OpenCode `managed_session_e2e` in `server/zerg/qa/universal_agent_harness.py`, tested through `provider-release-proof.py` |
| H18 | Define and emit the full universal Longhouse action matrix for every provider | Done | +4 | `action_matrix` scenario in `server/zerg/qa/universal_agent_harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H19 | Attach action-matrix output to provider release-proof artifacts | Done | +2 | `scripts/qa/provider-release-proof.py`, `scripts/tests/provider-release-proof.test.py` |
| H20 | Promote action-matrix blocked rows into real DB ingest, baseline compare, and old/new release diff lanes | Partial | +6 | DB ingest, baseline compare, and explicit old/new proof-artifact diff are promoted; automatic staged old/new provider install remains future work |
| H21 | Add hermetic DB ingest/product-surface proof behind the universal harness | Done | +3 | `db_ingest_project` scenario and release-proof wrapper test |
| H22 | Feed OpenCode provider-live managed-session evidence through Longhouse DB ingest | Done | +2 | `managed_session_e2e` now writes `longhouse/db-ingest-result.json` and release-proof asserts `universal_db_ingest=pass` |
| H23 | Add provider-agnostic control-surface artifact to release proofs | Done | +1 | `control_surface` scenario and normalized `control_surface.json` release-proof artifact |
| H24 | Add all-provider fake/no-token action/control CLI e2e | Done | +1 | `test_script_entrypoint_runs_all_provider_action_e2e` drives `scripts/qa/universal-agent-harness.py` across all four providers |
| H25 | Compare universal action/control rows in release-proof baselines | Done | +1 | `provider-release-proof-baseline.py` includes `action_matrix` and `control_surface` comparable artifacts |
| H26 | Add explicit old/new proof-artifact diff lane | Done | +1 | `provider-release-proof-baseline.py old-new`, `make provider-release-proof-old-new`, and baseline tests compare explicit old/new artifacts |
| H27 | Move universal action rows behind concrete provider adapter classes | Done | +1 | `ClaudeCodeHarnessAdapter`, `CodexOpenAIHarnessAdapter`, `OpenCodeHarnessAdapter`, `AntigravityHarnessAdapter`, and `action_result` row tests |
| H28 | Promote Codex managed-session e2e behind the universal adapter | Done | +1 | Codex `managed_session_e2e` calls `run_codex_provider_release_canary`, projects canary rows, DB-ingests them, and reports credential gaps as `unsupported_gap` |
| H29 | Promote Antigravity hook/inbox e2e behind the universal adapter | Done | +1 | Antigravity `managed_session_e2e` calls provider-control hook/inbox, projects external-event rows, DB-ingests them, and keeps unsupported control gaps explicit |
| H30 | Promote Claude provider-live no-token contract behind the universal adapter | Done | +1 | Claude `managed_session_e2e` calls provider-live command/channel/PTY checks, projects contract rows, DB-ingests them, and marks live send/steer as blocked live-token work |
| H31 | Add universal Codex interrupt/cancel scenario | Done | +1 | `interrupt_cancel` is accepted by provider-release-proof, routes Codex to `managed_live_interrupt`, DB-ingests pass evidence, and reports missing Runtime Host credentials as `unsupported_gap` |
| H32 | Add universal Codex tool call/result scenario | Done | +1 | `tool_call_result` is accepted by provider-release-proof, routes Codex to `codex_real_tool_result_shape`, DB-ingests tool call/result rows, and exposes `universal_tool_call_result` evidence |
| H33 | Add universal OpenCode resume/reattach scenario | Done | +1 | `resume_reattach` is accepted by provider-release-proof, routes OpenCode to `process_restart_reattach_contract`, DB-ingests reattach rows, and exposes `universal_reattach` evidence |
| H34 | Add universal Codex live-token streaming/send scenario | Done | +1 | `live_token_streaming` is accepted by provider-release-proof, routes Codex to `managed_live_send`, DB-ingests marker rows when Runtime Host credentials are present, and reports missing credentials as `unsupported_gap` |
| H35 | Add universal OpenCode tool call/result scenario | Done | +1 | `tool_call_result` routes OpenCode to `opencode_real_tool_result_shape`, DB-ingests linked call/result rows, and exposes `universal_tool_call_result` evidence through release proof |
| H36 | Add universal Antigravity live-token streaming/send scenario | Done | +1 | `live_token_streaming` routes Antigravity to `antigravity_real_agy_send`, DB-ingests hook-injected marker rows, and exposes `universal_live_token_streaming` evidence through release proof |
| H37 | Add universal Claude one-shot live-token scenario | Done | +1 | `live_token_streaming` routes Claude to `claude_real_print`, DB-ingests prompt/result marker rows, and exposes `universal_live_token_streaming` evidence through release proof without claiming managed steer |
| H38 | Add universal OpenCode interrupt/cancel scenario | Done | +1 | `interrupt_cancel` routes OpenCode to provider-live `session_abort`, DB-ingests abort/control rows, and exposes `universal_interrupt_cancel` evidence through release proof |
| H39 | Add universal Claude channel interrupt/cancel scenario | Done | +1 | `interrupt_cancel` routes Claude to provider-control channel send/steer/SIGINT, DB-ingests no-token control rows, and exposes `universal_interrupt_cancel` evidence through release proof without claiming managed live-token steer |
| H40 | Add universal OpenCode one-shot live-token scenario | Done | +1 | `live_token_streaming` routes OpenCode to provider-control `opencode_real_print`, DB-ingests prompt/result marker rows, and exposes `universal_live_token_streaming` evidence through release proof |
| H41 | Promote session/timeline projection to first-class universal scenarios | Done | +1 | `session_projection` and `timeline_projection` run for all four providers, emit comparable projection artifacts, and are included in default release-proof universal harness output |
| H42 | Promote old/new proof-artifact diff to executable universal scenario | Done | +1 | `old_new_release_diff` accepts explicit proof artifacts, emits pass/fail/blocked operation evidence, and is auto-attached by `provider-release-proof.py` when old/new artifacts are supplied |
| H43 | Promote Claude active-turn steer to executable universal scenario | Done | +1 | `steer_active_turn` routes Claude to provider-control channel steer evidence, DB-ingests no-token control rows, and reports Codex/OpenCode/Antigravity steer gaps explicitly |

## Score Update Rules

When updating this roadmap:

1. Change a score only when the task's evidence exists.
2. Do not give release-proof credit for fixture/hermetic evidence unless the
   scenario/profile explicitly says that evidence protects the upstream
   release-sensitive behavior.
3. Do not give baseline credit for a green proof until it has been manually
   accepted into the chosen baseline store.
4. Red or yellow proof results can increase the score if they make the system
   more diagnostic and actionable.
5. Unsupported provider behavior should be scored as a documented decision, not
   as an open bug, once the coverage matrix and Sauron digest classify it that
   way.
6. Averages must not hide P0 failures. Broken ingest/projection is red even if
   the provider lane has many passing lower-priority scenarios.

## Next Gates

The next implementation goal should finish Phase 3's real adapter migration,
then move into Phase 4 control/live-token scenarios:

1. Add sandboxed provider-version staging/install that generates the old and
   new proof artifacts automatically for every provider lane.
2. Migrate Codex live send/control canaries into universal scenarios instead of
   only release-proof profile flags.
3. Bring Claude live-token send mechanics behind adapters instead of only
   machine-live proof profiles.
5. Migrate remaining control-plane send/interrupt evidence behind universal
   scenarios.
6. Use Sauron's maturity rollup fields in digest wording and alert thresholds.
7. Migrate `run_prompt_once`, `launch_managed_session`, and `send_receive`
   behind stronger provider-backed adapters where they are still hermetic only.
8. Keep provider-specific canaries as compatibility lanes until their behavior
   is migrated and baselined.
