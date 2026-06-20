# Provider Release Proof Roadmap

**Status:** Active roadmap
**Last updated:** 2026-06-19
**Current grand-epic score:** 88/100

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
| Release-watch proofing after recent work | Longhouse proof lanes, coverage matrix, baseline tooling, Sauron invocation, universal harness attachment, adapter-conformance proof, concrete provider adapter action rows, all-provider support-matrix and execution-coverage artifacts, first real OpenCode no-token e2e lane, OpenCode `interrupt_cancel`, `resume_reattach`, `tool_call_result`, `permission_prompt`, and `live_token_streaming` lanes, Claude provider-live no-token managed e2e/launch contract plus channel interrupt/steer lanes and resume command-shape proof, Codex managed-session e2e, `resume_reattach` command-shape, and interrupt dispatch adapter lanes, first universal `interrupt_cancel` control lane, first universal `steer_active_turn` lane, first universal `tool_call_result` lane, executable all-provider `pause_request_detect`, Claude/Codex answer-pause service plus managed dispatch proof, `tail_output`, `runtime_phase`, `transcript_binding`, `multi_turn_continuity`, `tool_call_result_projection`, and `crash_timeout_cleanup` scenarios, opt-in `terminate_cleanup` scenario with Antigravity unsupported gap, Antigravity `external_event_channel` scenario backed by hook/inbox proof, explicit `permission_prompt` states with Claude/Codex blocked and Antigravity unsupported, all-provider `live_token_streaming` adapter lanes, Antigravity hook/inbox e2e adapter lane, universal action/control-surface/session/timeline projection artifacts, portable `full_action_suite` aggregate coverage artifact, action/control baseline diff, executable universal old/new proof-artifact diff, provider-scoped synthetic old/new diff coverage in the default all-provider smoke, staged-binary old/new proof runner, Sauron release-envelope preference for that staged-old-new runner, broad all-provider fake/no-token CLI smoke across implemented and explicit-gap scenarios, broader default release-proof universal profile that now runs provider-specific `managed_session_e2e` lanes, hermetic DB ingest proof, provider-live/control DB round-trips, computed maturity rollups from coverage/baseline/universal support and execution artifacts, Sauron baseline-guard generation and consumption of those universal smoke and maturity artifacts, and a Longhouse CI validation gate for the default all-provider fake/no-token universal smoke | 88/100 |
| Universal harness plumbing only | Adapter protocol, concrete provider adapter classes, adapter-conformance scenario, runner, evidence package, action matrix, all-provider support-matrix and execution-coverage artifacts, control-surface scenario, portable full-action-suite aggregate with executable tool-call/result projection, session/timeline projection scenarios, action/control/explicit-old-new baseline diff, executable `old_new_release_diff` scenario, all-provider fake/no-token CLI e2e, DB ingest scenario, all-provider pause/request, observation, multi-turn, crash-cleanup, and permission-gap scenarios, Antigravity `external_event_channel` scenario, opt-in `terminate_cleanup` scenario, Claude/Codex/OpenCode/Antigravity managed-session e2e promotion, Claude/Codex/OpenCode `interrupt_cancel` scenarios, Claude `steer_active_turn` scenario, Codex/OpenCode `tool_call_result` and `live_token_streaming` scenarios, OpenCode `resume_reattach` scenario, Claude resume command-shape proof, Claude and Antigravity `live_token_streaming` scenarios, proof-artifact attachment, broader default release-proof universal profile with managed-session e2e coverage, and computed maturity rollups, excluding all-provider-live/staged-old-new/Sauron completion | 100/100 |

The apparent drop from 45 to 25/35 was a denominator change: internal CI
maturity was being compared with the larger release-proofing product. The fair
movement for this epic is release-watch proofing before/after: roughly 20 -> 88.

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
| Longhouse proof artifact/core commands | 15 | 15 | Proof artifacts, normalized contracts, action-matrix/control-surface/DB-ingest/maturity artifacts, broad all-provider fake/no-token CLI e2e, accept/status/diff/maturity commands exist; OpenCode e2e DB artifacts flow through release proof; universal default smoke now includes continuity, crash/timeout cleanup, and provider-specific managed-session e2e lanes |
| Baselines and differential confidence | 15 | 11 | Accepted baseline machinery exists and now compares universal action/control artifacts plus explicit old/new proof artifacts through both the baseline CLI and universal harness; staged-binary old/new runner can produce both sides and diff them; default all-provider fake/no-token universal smoke is CI-gated, generates provider-scoped synthetic old/new proof pairs, and verifies `old_new_release_diff` coverage for every provider; durable/auditable provider-version install source of truth is still unsettled |
| Sauron private runner/reporting | 10 | 6 | Sauron can call Longhouse lanes, prefers the staged old/new proof runner for default differentials when both binaries are staged, and the daily baseline guard now generates Longhouse fake/no-token universal smoke when needed before consuming maturity rollups; private alert/noise policy is not migrated to universal artifacts |
| Provider real e2e migration | 25 | 24 | OpenCode has first real no-token universal e2e lane with provider-live evidence fed through Longhouse DB ingest; OpenCode `interrupt_cancel` now routes to the session.abort canary and DB-ingests abort evidence; OpenCode `resume_reattach` now routes to the process-restart reattach canary and DB-ingests reattach evidence; OpenCode `tool_call_result` now routes to the real-tool canary and DB-ingests tool call/result linkage; OpenCode `live_token_streaming` now routes to a real-print `opencode run --format json` marker canary and DB-ingests marker evidence; Claude provider-live no-token command/channel/PTY contract now DB-ingests through universal `managed_session_e2e` and `launch_managed_session`; Claude `interrupt_cancel` now routes to the channel-control canary, proves send/meta steer/SIGINT against an owned fake provider process, and DB-ingests control rows; Claude `steer_active_turn` now routes to the same channel-control canary but evaluates steer evidence directly; Claude `resume_reattach` now proves the channel resume command shape hermetically through `build_claude_channel_exec_command`; Claude/Codex `answer_pause_request` now proves Longhouse resolution plus managed-local `session.answer_pause` dispatch hermetically while live provider-held delivery remains blocked; Claude `live_token_streaming` now routes to real-print one-shot live-token proof and DB-ingests marker evidence; Codex managed-session e2e and live `resume_reattach` call the existing Codex canary and DB-ingest launch/reattach evidence when Runtime Host credentials are present; Codex `resume_reattach` now falls back to a hermetic `build_managed_local_attach_command` proof in no-token smoke; Codex `interrupt_cancel` now proves managed-local interrupt dispatch hermetically in no-token smoke and still records the live managed-interrupt credentials gate; Codex `steer_active_turn` now proves Longhouse managed-local steer dispatch hermetically while live active-turn provider behavior remains future; Codex `tool_call_result` now routes to the real-tool canary and DB-ingests call/result linkage; Codex `live_token_streaming` now routes to managed live-send and reports credential gaps explicitly; Antigravity hook/inbox e2e calls provider-control and DB-ingests external-event evidence; Antigravity `live_token_streaming` now routes to real-agy hook-inbox injection and DB-ingests marker evidence; cross-provider managed live send/steer and live answer-pause delivery remain incomplete |
| **Total** | **100** | **88** |  |

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
- Claude `launch_managed_session` now uses the same provider-live no-token
  command/channel/PTY contract, requires passing `launch_local` evidence, and
  feeds the resulting rows through isolated Longhouse SQLite ingest. Claude
  send/receive remains an explicit no-token gap.
- `send_message` execution coverage now uses any mapped executable scenario
  that carries passing `send_input` operation evidence. Claude's
  channel-control `interrupt_cancel` canary and Antigravity's hook/inbox
  `managed_session_e2e` proof can cover abstract send, while unsupported
  response-binding gaps remain visible in scenario status/failure metadata.
- `session_identity` execution coverage now also uses any mapped executable
  scenario that carries a provider session id. A passing managed launch or
  managed-session e2e can cover provider/Longhouse session identity while
  `resume_reattach` remains a separate explicit gap when it is unsupported or
  credential-gated.
- OpenCode `permission_prompt` now has a hermetic bridge-transport proof. The
  universal scenario writes an OpenCode bridge state file, sends
  `permission-reply` through Longhouse's bridge command to a fake held
  permission request, and records forwarded decision/auth/path evidence. Live
  provider-held permission prompts remain a stronger future gate.
- The default all-provider fake/no-token smoke now includes
  `managed_session_e2e`. In routine CI/Sauron smoke artifacts, Claude,
  OpenCode, and Antigravity must pass their provider-specific managed-session
  lanes, while Codex may report the typed Runtime Host credentials gap.
- `interrupt_cancel` is now an executable universal scenario for Claude, Codex,
  and OpenCode. Claude routes it to the provider-control channel canary, proving
  normal send metadata, steer metadata, and SIGINT delivery against an owned
  fake provider process without model-token spend. Codex routes to the existing
  managed-live-interrupt canary and DB-ingests interrupt evidence when
  configured; without Runtime Host credentials, it falls back to a hermetic
  `interrupt_managed_local_session` dispatch proof and records the live canary
  credentials gate separately. OpenCode routes it to the provider-live
  session.abort canary and DB-ingests abort evidence. Antigravity still returns
  a typed adapter gap.
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
  evidence. Codex routes it to the existing provider-release canary and
  DB-ingests reattach evidence when Runtime Host credentials are present;
  without those credentials it proves the managed-local attach command shape
  hermetically and records the live reattach canary gate separately. Claude
  proves the channel resume command shape hermetically, including `--resume`,
  Longhouse/provider session env, development-channel loading, and workspace
  selection. Antigravity still returns a typed adapter gap.
- `pause_request_detect` is now an executable universal scenario for all four
  providers. It drives Longhouse runtime `needs_user` plus structured
  `pause_request` events through an isolated SQLite DB, verifies the active
  pause-request projection, and is included in default
  `provider-release-proof.py --run-universal-harness` runs.
- `answer_pause_request` is now an executable universal scenario. Claude and
  Codex prove the Longhouse answer/resolve service plus managed-local
  `session.answer_pause` dispatch hermetically, while recording live
  provider-held answer delivery as the stronger blocked gate. OpenCode and
  Antigravity report typed `unsupported_gap` results until they expose
  answer-pause semantics.
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
  artifacts and are included in default release-proof universal runs;
  crash/timeout also writes diagnostics proving no owned process is left
  behind.
- `external_event_channel` is an executable universal scenario. Antigravity
  routes it to the existing provider-control hook/inbox canary and DB-ingests
  the resulting external-event evidence; other providers report typed
  `unsupported_gap` results.
- `permission_prompt` is an executable universal scenario. OpenCode proves the
  bridge reply transport hermetically. Claude and Codex report typed `blocked`
  live held-prompt canary gaps. Antigravity reports a typed `unsupported_gap`
  until it exposes stable provider permission-prompt semantics.
- Unsupported unsafe scenarios remain explicit `unsupported_gap` results for
  providers that do not yet have a safe universal adapter lane.
- `old_new_release_diff` is an executable universal scenario. It is blocked
  without explicit proof artifacts, compares old/new proof artifacts through
  `provider-release-proof-baseline.py old-new`, and can be attached to
  `provider-release-proof.py --run-universal-harness`.
- The default all-provider fake/no-token smoke now generates provider-scoped
  synthetic old/new proof artifacts, passes them through top-level
  `old_new_release_diff` and nested `full_action_suite`, and requires the
  execution coverage matrix to report `old_new_release_diff=pass` for all four
  providers. This proves diff mechanics in CI without claiming real provider
  version staging.
- Provider-version install/fetch policy and live-token behavior remain explicit
  future gates; Longhouse can already diff staged old/new binaries once a
  private runner supplies their paths.

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
| Claude Code | 8/10 | MVP adapter runs safe universal scenarios; universal `managed_session_e2e` and `launch_managed_session` now call provider-live no-token command/channel/PTY checks and DB-ingest them; universal `interrupt_cancel` calls the channel-control canary and DB-ingests no-token send/steer/SIGINT rows; universal `steer_active_turn` evaluates that steer evidence directly; universal `resume_reattach` now proves the channel resume command shape hermetically; universal `live_token_streaming` calls the real-print one-shot live-token canary and DB-ingests marker evidence; managed live-token send still needs promotion | Promote managed Claude live-token send and live resume-same-session proof into universal scenarios with bounded failure artifacts |
| Codex/OpenAI | 9/10 | MVP adapter runs safe universal scenarios; strongest existing lane has staged asset proof, managed live-send/interrupt, real-tool scenarios, accepted baselines; universal `managed_session_e2e` and live `resume_reattach` call the Codex provider-release canary and DB-ingest launch/reattach evidence when Runtime Host credentials are present; universal `resume_reattach` falls back to a hermetic managed-local attach-command proof in no-token smoke; universal `interrupt_cancel` proves managed-local interrupt dispatch hermetically in no-token smoke and calls the managed-live-interrupt canary when credentials are present; universal `steer_active_turn` now proves Longhouse managed-local steer dispatch hermetically; universal `tool_call_result` calls the real-tool canary and DB-ingests call/result linkage; universal `live_token_streaming` calls managed live-send and reports Runtime Host credential gaps explicitly | Promote live active-turn Codex steer behavior and add staged old/new install proof |
| OpenCode | 10/10 | First real no-token universal e2e lane calls the provider-live server/session/schema/prompt_async/reattach/abort canary, projects canonical evidence, and round-trips it through Longhouse DB ingest; universal `interrupt_cancel` now calls the session.abort canary directly; universal `resume_reattach` now calls the process-restart reattach canary directly; universal `tool_call_result` now calls provider-control real-tool and DB-ingests call/result linkage; universal `live_token_streaming` now calls provider-control real-print and DB-ingests marker rows | Promote staged old/new release diff for OpenCode in the release runner |
| Antigravity | 8/10 | MVP adapter runs safe universal scenarios; universal `launch_managed_session` now calls provider-live no-token binary/help/plugin/global-hook checks and DB-ingests `launch_local` evidence; universal `managed_session_e2e` now calls provider-control hook/inbox, DB-ingests external-event evidence, and counts toward abstract `send_message` plus `session_identity` only because it carries passing `send_input` evidence and a provider session id; universal `live_token_streaming` now calls real-agy hook-inbox injection and DB-ingests marker evidence | Keep interrupt/reattach/tool gaps explicit and add staged old/new release proof |

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
| H15 | Update Sauron to invoke universal lanes and apply private alert policy | Partial | +3 | Sauron baseline guard consumes Longhouse maturity rollups, and release-envelope differentials now prefer Longhouse's staged old/new runner for default profiles; digest thresholds and alert/noise policy remain future |
| H16 | Feed universal runner output into `provider-release-proof.py` | Done | +4 | `scripts/qa/provider-release-proof.py`, `scripts/tests/provider-release-proof.test.py` |
| H17 | Add first real provider-safe universal e2e lane | Done | +4 | OpenCode `managed_session_e2e` in `server/zerg/qa/universal_agent_harness.py`, tested through `provider-release-proof.py` |
| H18 | Define and emit the full universal Longhouse action matrix for every provider | Done | +4 | `action_matrix` scenario in `server/zerg/qa/universal_agent_harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H19 | Attach action-matrix output to provider release-proof artifacts | Done | +2 | `scripts/qa/provider-release-proof.py`, `scripts/tests/provider-release-proof.test.py` |
| H20 | Promote action-matrix blocked rows into real DB ingest, baseline compare, and old/new release diff lanes | Partial | +6 | DB ingest, executable baseline compare, explicit old/new proof-artifact diff, and staged-binary old/new runner are promoted; provider-version install/fetch policy remains future work |
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
| H31 | Add universal Codex interrupt/cancel scenario | Done | +1 | `interrupt_cancel` is accepted by provider-release-proof, routes Codex to `managed_live_interrupt` when configured, and now falls back to hermetic dispatch proof when Runtime Host credentials are missing |
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
| H44 | Add broad all-provider fake/no-token CLI smoke | Done | +1 | `test_script_entrypoint_runs_all_provider_fake_no_token_release_surface` drives 13 scenarios across Claude Code, Codex/OpenAI, OpenCode, and Antigravity in one CLI invocation, proving pass artifacts and operation-level unsupported gaps |
| H45 | Promote continuity and crash cleanup into default release-proof profile | Done | +1 | `DEFAULT_UNIVERSAL_SCENARIOS` now includes `multi_turn_continuity` and `crash_timeout_cleanup`; the all-provider release-proof wrapper test requires their merged canaries and operation evidence |
| H46 | Add staged-binary old/new proof runner | Done | +2 | `provider-release-proof-old-new.py` runs old and new staged provider binaries through `provider-release-proof.py`, then delegates to `provider-release-proof-baseline.py old-new`; Make and tests cover the new entrypoint |
| H47 | Feed staged old/new runner from Sauron release envelopes | Done | +2 | Sauron `run_provider_differential` prefers Longhouse `provider-release-proof-old-new.py` for default profiles when previous/current binaries are staged, and keeps envelope fallback for profile-specific live/tool lanes |
| H48 | Add portable full-action-suite aggregate | Done | +0 | `full_action_suite` runs the action matrix plus safe no-token action scenarios, writes per-action coverage, proves no abstract action is missing, and attaches through provider-release-proof; it closes a harness enforcement gap but does not advance private live/staged/Sauron gates |
| H49 | Add all-provider support-matrix artifact | Done | +0 | `provider-support-matrix.json` transposes action-matrix rows into one provider-by-action grid and `provider-release-proof.py` attaches the current provider slice as `provider_support_matrix`; this improves auditability without changing the grand score |
| H50 | Add CI-friendly universal fake/no-token smoke command | Done | +0 | `provider-release-proof-universal-smoke.py` and `make provider-release-proof-universal-smoke` generate disposable fake provider binaries, run the all-provider universal harness, and emit support/execution coverage matrices; this makes the existing smoke easy to schedule without changing live/staged coverage |
| H51 | Gate provider validation on the all-provider universal smoke | Done | +1 | `make validate-provider-cli-canaries` now runs the default `provider-release-proof-universal-smoke` target, and the Make target uses the `server` uv environment so `contract-first-ci` exercises the shared provider action surface without secrets |
| H52 | Preserve universal smoke artifacts from CI | Done | +0 | `contract-first-ci` uploads `.build/canaries/provider-release-proof-universal-smoke/**` after provider validation so yellow/red evidence can be inspected from GitHub Actions; this improves debuggability without adding release-sensitive coverage |
| H53 | Promote baseline compare to an executable universal scenario | Done | +0 | `baseline_compare` now generates synthetic provider-release-proof baseline/candidate envelopes, calls `provider-release-proof-baseline.py diff`, runs for all providers, and is included in default smoke/full-action-suite coverage; this closes a matrix-only proof gap without changing staged/live release coverage |
| H54 | Add all-provider execution coverage matrix to smoke artifacts | Done | +0 | `provider-execution-coverage-matrix.json` transposes `full_action_suite` rows by provider/action and the default universal smoke now runs `full_action_suite`, making matrix-only versus executable scenario coverage visible in CI artifacts without adding new live/staged release coverage |
| H55 | Consume execution coverage in release-proof and maturity artifacts | Done | +0 | `provider-release-proof.py` now attaches a provider-specific `provider_execution_coverage_matrix` when `full_action_suite` runs, and `provider-release-proof-maturity.py` reports execution pass/executable-scenario/matrix-contract ratios from universal artifacts; this improves reporting without adding new live/staged coverage |
| H56 | Make Sauron generate default universal smoke and close portable tool projection coverage | Done | +1 | Sauron's baseline guard now runs Longhouse fake/no-token universal smoke when no artifact exists, and Longhouse `full_action_suite` maps `tool_call_result` to a portable DB-ingested projection scenario while preserving stronger live real-tool canaries as separate opt-in lanes |
| H57 | Close portable remote-launch execution coverage | Done | +0 | `launch_remote_projection` exercises Longhouse's canonical remote-launch lifecycle projection for supported providers and keeps Antigravity as an explicit unsupported gap; real Machine Agent remote dispatch remains a stronger live lane |
| H58 | Run managed-session e2e in the default universal smoke | Done | +1 | `provider-release-proof-universal-smoke.py` now includes `managed_session_e2e` by default, its fake Claude binary supports provider-live command/channel contract checks, and the Make smoke test asserts Claude/OpenCode/Antigravity pass while Codex reports the typed Runtime Host credentials gap |
| H59 | Promote Codex resume/reattach behind the universal adapter | Done | +1 | `resume_reattach` now routes Codex through the existing provider-release canary, DB-ingests reattach evidence when Runtime Host credentials are present, and reports `codex_managed_bridge_credentials_missing` instead of an adapter-missing gap in fake/no-token smoke |
| H60 | Promote Claude launch behind the universal adapter | Done | +1 | `launch_managed_session` now routes Claude through the provider-live no-token command/channel/PTY contract canary, requires `launch_local` evidence, DB-ingests the rows, and keeps Claude send/receive as an explicit no-token gap |
| H61 | Count mapped send-input evidence without hiding response gaps | Done | +0 | `send_message` execution coverage now accepts any mapped executable scenario that proves send input, so Claude's channel-control `interrupt_cancel` evidence can cover the abstract send action while `send_receive_not_safe_no_token` remains recorded in scenario metadata |
| H62 | Make default smoke prove provider-scoped old/new diff coverage | Done | +1 | `full_action_suite` now receives old/new proof artifacts, `HarnessOptions` accepts provider-scoped proof paths, and the default universal smoke generates synthetic old/new proof pairs for Claude, Codex, OpenCode, and Antigravity so the execution coverage matrix records `old_new_release_diff=pass` for every provider |
| H63 | Count mapped session identity evidence without hiding reattach gaps | Done | +0 | `session_identity` execution coverage now accepts any mapped executable scenario that proves provider/Longhouse session identity, so Claude and Codex managed-launch evidence can cover the abstract identity action while `resume_reattach` still records adapter-missing or credential-gated gaps |
| H64 | Add OpenCode permission-prompt bridge transport proof | Done | +0 | `permission_prompt` now passes for OpenCode by writing an isolated bridge state file and sending `permission-reply` through Longhouse's OpenCode bridge command to a fake held permission request; Claude/Codex still report live provider-held permission prompt gaps |
| H65 | Count Antigravity managed-session proof without over-crediting launch-only scenarios | Done | +0 | `full_action_suite` now runs `managed_session_e2e`, `send_message` coverage requires mapped `send_input=pass`, and `session_identity` coverage requires a provider session id; Antigravity hook/inbox e2e now counts for abstract send/session identity while response, interrupt, and reattach gaps stay visible |
| H66 | Add Codex steer dispatch transport proof | Done | +0 | `steer_active_turn` now passes for Codex by seeding a managed-local Codex session, dispatching through `steer_text_to_managed_local_session`, and asserting the `session.steer_text` command/payload/transport; live provider active-turn behavior remains a future gate |
| H67 | Add Claude resume command-shape proof | Done | +0 | `resume_reattach` now passes for Claude by building the channel resume command through `build_claude_channel_exec_command` and asserting `--resume`, session env, channel loading, and workspace selection; live process restart plus same-session send remains the future gate |
| H68 | Add Codex interrupt dispatch transport proof | Done | +0 | `interrupt_cancel` now falls back to a hermetic managed-local Codex `session.interrupt` dispatch proof when Runtime Host credentials are absent, while preserving the managed-live-interrupt canary as a blocked stronger gate |
| H69 | Add Codex reattach command-shape proof | Done | +0 | `resume_reattach` now falls back to a hermetic managed-local Codex attach-command proof when Runtime Host credentials are absent, while preserving managed process-restart/same-thread reattach as the stronger gate |
| H70 | Add managed answer-pause dispatch proof | Done | +0 | `answer_pause_request` now proves Longhouse pause resolution plus Claude/Codex managed-local `session.answer_pause` dispatch, while preserving live provider-held answer delivery as a blocked stronger gate |
| H71 | Mark Antigravity permission prompts unsupported | Done | +0 | `permission_prompt` now reports Antigravity as a typed unsupported gap instead of a missing live canary, matching its contract surface |
| H72 | Promote Antigravity launch behind the universal adapter | Done | +0 | `launch_managed_session` now routes Antigravity through the provider-live no-token binary/help/plugin/global-hook canary, requires `launch_local` evidence, DB-ingests the rows, and keeps real `agy` loop send/interrupt/reattach gaps separate |
| H73 | Replace Antigravity adapter-missing control gaps with contract gaps | Done | +0 | `interrupt_cancel` and `resume_reattach` now report typed Antigravity `unsupported_gap` results from the managed-provider contract instead of looking like missing universal adapter plumbing |

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

1. Add private/Sauron provider-version install staging that feeds
   `provider-release-proof-staged-old-new` with audited old/new binary paths.
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
