# Provider Release Proof Roadmap

**Status:** Active roadmap
**Last updated:** 2026-06-19
**Current end-state score:** 48/100

This roadmap tracks the migration from one-off provider canaries to a universal
agent-harness proof system. The design target is
`docs/specs/universal-agent-harness.md`.

## Axes

Do not quote a score without naming the axis.

| Axis | Meaning | Current read |
| --- | --- | ---: |
| Existing Longhouse CI/test maturity | Internal Longhouse confidence before this release-proof epic: parser tests, bridge tests, shipper tests, backend/engine/frontend tests | 45/100 |
| Release-watch proofing before recent work | Sauron release emails plus limited/fake provider checks | 20/100 |
| Release-watch proofing after recent work | Longhouse proof lanes, coverage matrix, baseline tooling, Sauron invocation, several accepted real/no-token baselines | 48/100 |
| Universal harness end state | The full provider-agnostic roadmap below | 48/100 |

The apparent drop from 45 to 25/35 was a denominator change: internal CI
maturity was being compared with the larger release-proofing product. The fair
movement for this epic is release-watch proofing before/after: roughly 20 -> 48.

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
| Universal harness architecture | 15 | 5 | Design spec exists; adapter protocol and runner are not implemented |
| Longhouse proof artifact/core commands | 15 | 11 | Proof artifacts, normalized contracts, accept/status/diff commands exist; still wrap provider-specific canaries |
| Baselines and differential confidence | 15 | 5 | Accepted baseline machinery exists; durable/auditable baseline source of truth is unsettled |
| Sauron private runner/reporting | 10 | 5 | Sauron calls Longhouse lanes and baseline guard exists; alert/noise policy remains private-runner work |
| Provider adapter/scenario migration | 25 | 6 | Current behavior is mostly one-off provider scripts; crosswalk identifies migration candidates |
| **Total** | **100** | **48** |  |

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

Deliverables:

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
| Claude Code | 3/10 | No-token proof baseline and machine dispatch path exist; live-token machine proof currently times out with weak partial evidence | Move PTY/channel/live-token logic behind a Claude adapter and make failure artifacts bounded |
| Codex/OpenAI | 7/10 | Strongest lane: staged asset proof, managed live-send/interrupt, real-tool scenarios, accepted baselines | Use Codex as one of the first adapters for the runner skeleton |
| OpenCode | 6/10 | Good no-token server/control proof and real-tool baseline | Use OpenCode as the second adapter to keep the runner from becoming Codex-shaped |
| Antigravity | 4/10 | Canonical Google lane; no-token hook/plugin baseline and live-send baseline exist | Model hooks/inbox as `external_event_channel`; classify interrupt/reattach/tool gaps explicitly |

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
| H8 | Implement adapter protocol and scenario result schema | Not started | +6 | Future code/tests |
| H9 | Implement shared evidence package writer | Not started | +5 | Future code/tests |
| H10 | Implement first universal scenarios: `probe_identity`, `collect_raw_evidence`, fixture `parse_ingest_project` | Not started | +6 | Future runner artifacts |
| H11 | Wire first two adapters through the runner | Not started | +6 | Future Codex/OpenCode runner artifacts |
| H12 | Migrate managed launch/send/timeline scenarios | Not started | +8 | Future universal scenario artifacts |
| H13 | Migrate control/live-token/tool/resume scenarios | Not started | +8 | Future universal scenario artifacts |
| H14 | Decide durable accepted-baseline source of truth | Not started | +5 | Future documented store and reproducible status artifact |
| H15 | Update Sauron to invoke universal lanes and apply private alert policy | Not started | +5 | Future Sauron tests/artifacts |

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

The next implementation goal should be Phase 2, not another provider-specific
canary:

1. Add adapter protocol/data classes and scenario result schema.
2. Add the shared evidence package writer.
3. Implement `probe_identity`, `collect_raw_evidence`, and fixture
   `parse_ingest_project`.
4. Wire Codex and OpenCode first to validate the abstraction.
5. Keep provider-specific canaries as compatibility lanes until their behavior
   is migrated and baselined.
