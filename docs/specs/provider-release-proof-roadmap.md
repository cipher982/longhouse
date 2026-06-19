# Provider Release Proof Roadmap

**Status:** Active roadmap
**Last updated:** 2026-06-19
**Current end-state score:** 69/100

This roadmap tracks the migration from one-off provider canaries to a universal
agent-harness proof system. The design target is
`docs/specs/universal-agent-harness.md`.

## Axes

Do not quote a score without naming the axis.

| Axis | Meaning | Current read |
| --- | --- | ---: |
| Existing Longhouse CI/test maturity | Internal Longhouse confidence before this release-proof epic: parser tests, bridge tests, shipper tests, backend/engine/frontend tests | 45/100 |
| Release-watch proofing before recent work | Sauron release emails plus limited/fake provider checks | 20/100 |
| Release-watch proofing after recent work | Longhouse proof lanes, coverage matrix, baseline tooling, Sauron invocation, universal harness attachment, several accepted real/no-token baselines | 55/100 |
| Universal harness end state | The full provider-agnostic roadmap below | 69/100 |

The apparent drop from 45 to 25/35 was a denominator change: internal CI
maturity was being compared with the larger release-proofing product. The fair
movement for this epic is release-watch proofing before/after: roughly 20 -> 55.

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
| Scope, ownership, and provider set | 10 | 8 | Longhouse/Sauron boundary is documented; providers are Claude Code, Codex/OpenAI, OpenCode, Antigravity |
| Coverage inventory | 10 | 8 | 52 provider/surface rows tracked; needs computed rollups generated from scenario state |
| Universal harness architecture | 15 | 13 | Shared runner now covers identity, evidence collection, fixture replay, prompt projection, and first managed/session projections |
| Longhouse proof artifact/core commands | 15 | 13 | Proof artifacts, normalized contracts, accept/status/diff commands exist; release-proof can attach universal harness artifacts |
| Baselines and differential confidence | 15 | 5 | Accepted baseline machinery exists; durable/auditable baseline source of truth is unsettled |
| Sauron private runner/reporting | 10 | 5 | Sauron calls Longhouse lanes and baseline guard exists; alert/noise policy remains private-runner work |
| Provider adapter/scenario migration | 25 | 17 | All four providers have MVP adapters; Codex/OpenCode have first universal managed/session-safe projections; control/live-token scenarios remain one-off lanes |
| **Total** | **100** | **69** |  |

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
- Scenario result schema.
- Shared evidence package writer.
- Universal scenarios for `probe_identity`, `collect_raw_evidence`,
  `parse_ingest_project`, and typed-unsupported `run_prompt_once`.
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
  `run_prompt_once`, `launch_managed_session`, and `send_receive` can be
  included in the release-proof output.
- Codex/OpenAI and OpenCode expose first no-token/session-safe
  `launch_managed_session` and `send_receive` projections through the universal
  runner.
- Unsupported unsafe scenarios remain explicit `unsupported_gap` results for
  providers that do not yet have a safe universal adapter lane.

Deliverables:

- Universal `run_prompt_once`, `launch_managed_session`, `send_receive`, and
  `timeline_projection` scenarios.
- Existing one-off launch/send/session tests migrate behind adapters.
- Provider-specific mechanics stay behind adapters: PTY, app-server, server
  schema, hooks/inbox.

Done when the same scenario ids produce comparable artifacts for every provider
that declares the required capabilities.

### Phase 4: Control And Live-token Scenarios

Deliverables:

- Universal `interrupt_cancel`, `resume_reattach`, `multi_turn_continuity`,
  `tool_call_result`, `live_token_streaming`, `external_event_channel`, and
  `crash_timeout_cleanup` scenarios.
- Unsupported required capabilities surface as `unsupported_gap`, not skip.
- Claude machine-live diagnostics are fixed as part of the Claude adapter lane,
  not as a separate product shape.

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
| Claude Code | 4/10 | MVP adapter runs safe universal scenarios; no-token proof baseline and machine dispatch path exist; live-token machine proof still times out with weak partial evidence | Move PTY/channel/live-token logic behind the Claude adapter and make failure artifacts bounded |
| Codex/OpenAI | 8/10 | MVP adapter runs safe universal scenarios; strongest existing lane has staged asset proof, managed live-send/interrupt, real-tool scenarios, accepted baselines | Migrate Codex managed launch/send/control canaries into universal scenarios |
| OpenCode | 7/10 | MVP adapter runs safe universal scenarios; good no-token server/control proof and real-tool baseline | Migrate server/session/schema canaries into universal scenarios |
| Antigravity | 5/10 | MVP adapter runs safe universal scenarios; no-token hook/plugin baseline and live-send baseline exist | Model hooks/inbox as `external_event_channel`; classify interrupt/reattach/tool gaps explicitly |

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
| H7 | Add computed maturity/score rollups from coverage + scenario/baseline state | Not started | +4 | Future status command/output |
| H8 | Implement adapter protocol and scenario result schema | Done | +6 | `server/zerg/qa/universal_agent_harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H9 | Implement shared evidence package writer | Done | +5 | `server/zerg/qa/universal_agent_harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H10 | Implement first universal scenarios: `probe_identity`, `collect_raw_evidence`, fixture `parse_ingest_project` | Done | +6 | `scripts/qa/universal-agent-harness.py`, `server/tests_lite/test_universal_agent_harness.py` |
| H11 | Wire first two adapters through the runner | Done | +6 | MVP adapters now cover all four providers in `server/zerg/qa/universal_agent_harness.py` |
| H12 | Migrate managed launch/send/timeline scenarios | Partial | +4 | Codex/OpenCode universal `launch_managed_session` and `send_receive` artifacts in release-proof output |
| H13 | Migrate control/live-token/tool/resume scenarios | Not started | +8 | Future universal scenario artifacts |
| H14 | Decide durable accepted-baseline source of truth | Not started | +5 | Future documented store and reproducible status artifact |
| H15 | Update Sauron to invoke universal lanes and apply private alert policy | Not started | +5 | Future Sauron tests/artifacts |
| H16 | Feed universal runner output into `provider-release-proof.py` | Done | +4 | `scripts/qa/provider-release-proof.py`, `scripts/tests/provider-release-proof.test.py` |

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

1. Replace the current Codex/OpenCode projection-only session lane with real
   adapter calls to the existing no-token canary mechanics where possible.
2. Bring Claude PTY/channel and Antigravity hook/inbox mechanics behind
   adapters instead of only compatibility canaries.
3. Migrate `interrupt_cancel`, `resume_reattach`, `tool_call_result`, and
   control-plane send/interrupt evidence behind universal scenarios.
4. Add computed maturity rollups from universal scenario artifacts.
5. Migrate `run_prompt_once`, `launch_managed_session`, `send_receive`, and
   `timeline_projection` behind adapters.
6. Keep provider-specific canaries as compatibility lanes until their behavior
   is migrated and baselined.
