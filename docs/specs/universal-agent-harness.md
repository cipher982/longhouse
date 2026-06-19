# Universal Agent Harness

**Status:** Design target for provider release proofing
**Owner:** Longhouse
**Last updated:** 2026-06-19

MVP implementation:

- `server/zerg/qa/universal_agent_harness.py`
- `scripts/qa/universal-agent-harness.py`
- `server/tests_lite/test_universal_agent_harness.py`

Longhouse provider release proofing should not grow as four separate test
families for Claude Code, Codex/OpenAI, OpenCode, and Antigravity. The target is
one universal agent-harness contract, one scenario runner, and provider
adapters that translate each upstream CLI's mechanics into that contract.

The core pipeline is:

```text
provider release + provider adapter + universal scenario corpus
  -> immutable evidence package
  -> deterministic assertions + accepted-baseline diff
  -> Longhouse verdict
  -> Sauron private release report
```

## Boundary

Longhouse owns the OSS-safe proof framework:

- universal harness contract and capability vocabulary
- provider adapter interfaces and generic provider adapters
- universal scenario corpus and scenario runner
- evidence package schema
- raw-to-canonical parser and Longhouse ingest/projection assertions
- baseline accept/status/diff semantics
- green/yellow/red classification
- public docs and local proof commands

Sauron owns David's private release operations:

- upstream release watching and private source-review context
- private accounts, tokens, machines, schedules, and email policy
- token-spending proof configuration
- private evidence/baseline storage when artifacts contain sensitive data
- digest routing, labels, inbox policy, and artifact links

Sauron may stage a candidate version and invoke Longhouse, but Sauron must not
define provider compatibility. Release notes can explain a verdict; they should
not be the verdict.

## Conceptual Model

The provider is fungible only at the observable session/capability layer:

- start or run an agent
- send input
- observe output
- interrupt or steer work
- resume or reattach where supported
- collect raw provider evidence
- decode provider evidence into canonical Longhouse events
- ingest and project those events into Longhouse sessions/timelines

Provider quirks are adapter internals. Claude PTY/channel behavior, Codex
app-server/bridge behavior, OpenCode server/schema behavior, and Antigravity
hook/inbox behavior should be expressed through the same harness contract rather
than becoming separate top-level deliverables.

## Adapter Contract

A provider adapter should be concrete and operational. It should not abstract
agent intelligence or model quality. It should expose only what Longhouse must
control and observe.

Required adapter responsibilities:

| Method family | Responsibility |
| --- | --- |
| `prepare` | Create an isolated workspace, resolve/stage the provider binary or version, write provider config, and record environment metadata. |
| `probe` | Report provider name, binary path, version, channel, adapter version, platform, declared capabilities, and auth/account status where available. |
| `adapter_conformance` | Prove the concrete adapter class implements the universal method set, action ids, scenario runner map, and action-to-scenario map. |
| `run_prompt` | Run a one-shot prompt and capture raw stdout/stderr/provider exports. |
| `start_session` | Start a managed interactive session and return a stable session handle with provider ids and evidence paths. |
| `send_input` | Deliver text or structured input to an active managed session. |
| `observe` | Collect output, provider events, terminal/server logs, status, errors, and timing. |
| `interrupt` | Cancel or interrupt an active turn and report the resulting session state. |
| `steer` | Send active-turn steering input when the provider supports it. |
| `resume` | Resume or reattach a prior session and prove it is the same provider session/thread when the provider supports it. |
| `stop` | Stop gracefully, force cleanup if needed, and detect orphaned processes. |
| `collect_evidence` | Persist raw logs, transcripts, provider exports, process/server logs, workspace diffs, and adapter diagnostics. |
| `decode_normalize` | Convert provider raw evidence to canonical Longhouse events while preserving unknown provider fields/events. |

Adapters may declare unsupported capabilities. A claimed capability that fails
its scenario is a contract failure, not an unsupported gap.

## Universal Action Matrix

The harness now has an `action_matrix` scenario. It emits the same Longhouse
action ids for every provider, even when a provider cannot support an action.
This is the bridge between "agent harnesses are fungible" and "provider
mechanics are not."

When an all-provider run includes `action_matrix`, the harness also writes
`provider-support-matrix.json`. That artifact transposes the per-provider
action rows into one provider-by-action grid, preserving each provider's
status, support reason, evidence level, proof scope, canary, failure code, and
next promotion gate. Release-proof attaches the current provider's slice as
`provider_support_matrix` so Sauron and baseline tooling can report the shared
surface without re-parsing every provider subrun.

Current action ids:

```text
provider_identity
launch_local
launch_remote
run_once
session_identity
send_message
steer_active_turn
pause_request_detect
answer_pause_request
interrupt_cancel
resume_reattach
terminate_cleanup
tail_output
runtime_phase
transcript_binding
tool_call_result
raw_evidence_capture
parse_normalize
db_ingest
session_projection
timeline_projection
baseline_compare
old_new_release_diff
```

Each row includes:

| Field | Meaning |
| --- | --- |
| `support` / `support_reason` | Whether the provider can support the action and which contract/capability says so. |
| `status` | Current proof state: `pass`, `fail`, `unsupported_gap`, `blocked`, etc. |
| `adapter_class` / `adapter_method` | The concrete provider adapter class and method that emitted the row. |
| `implementation_kind` | Whether the row is backed by a provider probe, managed-provider contract, derived Longhouse surface, DB ingest, release diff, typed unsupported gap, or typed blocked gap. |
| `required_evidence` | The minimum evidence level this action should eventually have. |
| `evidence_level` | The strongest recorded proof level for the action today, when present. |
| `proof_scope` | Where the current proof comes from: version command, managed-provider contract, parser tests, DB lane, release diff runner, etc. |
| `contract_evidence` | The existing managed-provider contract evidence, when the action maps to a contract operation. |
| `next` | The next promotion gate when a row is unsupported or blocked. |

`pass` in the matrix means Longhouse has a named proof source for that
provider/action at the recorded evidence level. It does not automatically mean
the current invocation spent tokens or drove a live model turn. The row's
`evidence_level` and `proof_scope` are the important qualifiers.

`old_new_release_diff` is now a first-class executable artifact-diff scenario:
it is `blocked` without explicit proof artifacts and `pass` or `fail` when the
release-proof baseline tool compares old/new proof artifacts. Run it directly
with `scripts/qa/universal-agent-harness.py --scenario old_new_release_diff
--old-proof-artifact OLD --new-proof-artifact NEW`, or attach it to
`provider-release-proof.py --run-universal-harness` with
`--universal-old-proof-artifact` and `--universal-new-proof-artifact`.
All-provider callers can also pass provider-scoped proof paths through
`HarnessOptions.old_proof_paths` and `HarnessOptions.new_proof_paths`; the
single old/new proof path fields remain the backward-compatible fallback.
It is not yet automatic provider-version staging/install; that belongs to the
release runner that produces the proof artifacts.

The registry uses concrete provider adapter classes:
`ClaudeCodeHarnessAdapter`, `CodexOpenAIHarnessAdapter`,
`OpenCodeHarnessAdapter`, and `AntigravityHarnessAdapter`. They share the same
abstract `action_result` method, so every provider emits a result for every
action id instead of silently skipping unsupported or unimplemented behavior.

`managed_session_e2e` is adapter-specific today. OpenCode calls the provider-live
server/session canary and DB-ingests the resulting rows. Claude calls the
provider-live no-token command/channel/PTY contract, projects those rows, and
DB-ingests them; Claude `launch_managed_session` now uses the same provider-live
contract and requires passing `launch_local` evidence. Live send and steer
remain explicit blocked operations until the live-token Claude contract is
promoted. Codex calls the provider-release canary for `managed_tui_attach` and
`detached_ui`, then DB-ingests those
launch/reattach rows when Runtime Host credentials are available; without those
credentials it returns a typed `unsupported_gap`. Codex `resume_reattach` uses
the same provider-release canary and requires a passing reattach row, so the
dedicated action now reports evidence when Runtime Host credentials are present
and the same typed credentials gap otherwise. Antigravity calls the
provider-control hook/inbox canary, projects external-event channel rows, and
DB-ingests them. The default all-provider fake/no-token release smoke includes
this scenario, so routine CI/Sauron smoke artifacts must show Claude, OpenCode,
and Antigravity passing their provider-specific managed-session lanes while
Codex reports the typed Runtime Host credentials gap when credentials are not
configured. That proves hook/inbox input delivery and Stop/force-continue
behavior for Antigravity; it does not prove interrupt, reattach, or tool-result
semantics.

`interrupt_cancel` is a dedicated universal control scenario. Claude routes it
to the provider-control channel canary, proves normal send metadata, steer
metadata, and SIGINT delivery against an owned fake provider process, then
DB-ingests the resulting no-token control rows. Codex routes it to the existing
managed-live-interrupt canary and DB-ingests interrupt evidence when Runtime
Host credentials are present. Without those credentials it returns a typed
`unsupported_gap`. OpenCode routes it to the provider-live session.abort canary
and DB-ingests abort evidence. Antigravity currently returns a typed adapter gap
for this scenario.

`tool_call_result_projection` is the portable no-token executable scenario for
the abstract `tool_call_result` action. It emits a paired tool call/result
fixture for every provider, DB-ingests it, and proves Longhouse can preserve
call ids, names, inputs, outputs, and the session/timeline projection. The
stronger `tool_call_result` live scenario still exists separately: Codex routes
it to the existing Codex real-tool canary, and OpenCode routes it to the
provider-control real-tool canary. Those live lanes project a tool call row, a
linked tool result row, and the final assistant response row, then DB-ingest the
linkage. Other providers currently return typed adapter gaps for the stronger
live scenario.

`resume_reattach` is an executable universal scenario for OpenCode. It calls the
provider-live process-restart reattach canary, projects the recovered session
and marker transcript rows, and DB-ingests the reattach evidence. Other providers
currently return typed adapter gaps for this scenario.

`live_token_streaming` is an executable universal scenario for Claude, Codex,
OpenCode, and Antigravity. Claude calls the real-print one-shot canary. Codex
calls the existing managed-live-send canary. OpenCode calls the real-print
`opencode run --format json` marker canary. Antigravity calls the real-agy
hook-inbox injection canary. These paths project user and assistant marker rows
and DB-ingest the live-token evidence when their live lane is configured.

`session_projection` and `timeline_projection` are now first-class universal
scenarios for all four providers. They are hermetic projection proofs: each
adapter emits the same canonical provider events, Longhouse session projection,
timeline projection, and operation evidence. They prove the shared projection
surface is wired through the harness and release proof; provider-live lanes add
stronger raw evidence when they run.

`launch_remote_projection` is the portable no-token executable scenario for the
abstract `launch_remote` action. It exercises Longhouse's canonical
remote-launch lifecycle projection for dispatched, adopted/live, and failed
launch attempts, records provider machine-control support metadata, and leaves
providers without remote-launch support as explicit `unsupported_gap` rows. It
does not dispatch a real Machine Agent command; real Runtime Host remote launch
remains a stronger provider/live lane.

`full_action_suite` is an opt-in portable aggregate scenario. It runs the
action matrix plus the safe no-token control/observe scenarios, writes a single
coverage artifact, and verifies that every abstract action id is either covered
by an executable scenario result or by an explicit matrix/contract row. It now
executes `baseline_compare` through the same provider-release-proof baseline
diff CLI used by release watch, and executes `tool_call_result` through the
portable `tool_call_result_projection` DB-ingest lane. It also executes
`launch_remote` through the portable `launch_remote_projection` lifecycle lane,
and forwards explicit old/new proof artifacts into nested
`old_new_release_diff`.
It keeps real Machine Agent remote dispatch, live-token provider tool
execution, and staged old/new prerequisites out of the portable bundle; those
remain stronger opt-in lanes. A blocked suite is expected while permission
prompts, live answer-pause delivery, and some provider-specific control lanes
are still explicit gaps.

When an all-provider run includes `full_action_suite`, the harness also writes
`provider-execution-coverage-matrix.json`. This is different from
`provider-support-matrix.json`: the support matrix transposes provider contract
rows from `action_matrix`, while the execution coverage matrix transposes
`full_action_suite` rows and shows whether each provider/action was backed by
an executable scenario (`coverage_kind=executable_scenario`) or only by the
explicit matrix contract (`coverage_kind=matrix_contract`). This is the artifact
to inspect when deciding whether a release smoke actually exercised send,
steer, pause, cancel, ingest, projection, baseline compare, or old/new diff
behavior.

## Capabilities And Profiles

Capabilities are the vocabulary scenarios use to decide what is required:

- `identity`
- `auth_status`
- `one_shot_prompt`
- `managed_launch`
- `session_identity`
- `raw_evidence`
- `canonical_parse`
- `longhouse_ingest`
- `timeline_projection`
- `send_input`
- `interrupt`
- `steer`
- `resume`
- `tool_call_result`
- `live_token`
- `streaming_output`
- `external_event_channel`
- `permission_prompt`
- `cleanup`

Profiles define what a lane expects:

| Profile | Purpose |
| --- | --- |
| `fixture_replay` | Re-run raw captured evidence through parser/ingest/projection without launching a provider. |
| `live_no_token` | Exercise real provider binary/control surfaces without model-visible token spend. |
| `live_token_smoke` | Spend the minimum token budget needed to prove model-visible send/receive behavior. |
| `managed_control` | Prove Longhouse can launch, send, interrupt/steer, observe, and stop a managed session. |
| `full_release_gate` | Run all required P0/P1 scenarios for a provider release and diff against accepted baselines. |

Unsupported provider capabilities must be explicit. If a provider's target
profile requires `resume` and the adapter cannot support it, the result is an
unsupported gap, not a hidden skip.

## Scenario Model

A scenario is provider-agnostic. It names required capabilities and assertions;
it does not branch on provider names.

Scenario fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable scenario id used for baselines and Sauron reports. |
| `profile` | Required profile such as `live_no_token` or `full_release_gate`. |
| `required_capabilities` | Capabilities the adapter must declare and empirically prove. |
| `fixture` | Optional workspace or raw evidence fixture. |
| `stimulus` | Prompt/input/control action/external event. |
| `expected_observations` | Structural expectations, not exact prose. |
| `longhouse_assertions` | Parse, ingest, session, timeline, and tool-result assertions. |
| `artifact_requirements` | Raw and normalized evidence files that must exist even on failure. |
| `baseline_comparator` | Stable normalized fields to diff against the accepted baseline. |
| `severity` | `P0`, `P1`, or `P2`. |

Universal scenarios:

| Scenario | Severity | Required proof |
| --- | --- | --- |
| `probe_identity` | P0 | Version, binary path, adapter version, platform, declared/observed capabilities. |
| `adapter_conformance` | P0 | Concrete provider adapter class, method table, action ids, scenario ids, and action-to-scenario mapping match the universal contract. |
| `action_matrix` | P0 | Every provider emits the same Longhouse action ids with explicit pass/fail/unsupported/blocked status and proof source. |
| `control_surface` | P0 | Every provider emits the same control/observation action subset with concrete pass/fail/unsupported/blocked evidence rows. |
| `run_prompt_once` | P0 | One-shot prompt exits cleanly, emits evidence, and produces a model or fixture response. |
| `launch_managed_session` | P0 | Managed session starts, exposes a session handle, and has raw evidence. |
| `send_receive` | P0 | Input reaches the correct active session and a response is observed. |
| `managed_session_e2e` | P0 | Real provider-safe managed/session mechanics run, raw provider/control evidence is captured, and canonical session/timeline projection is produced. |
| `tail_output` | P0 | Fresh provider output/tail events project to canonical session and timeline artifacts. |
| `runtime_phase` | P0 | Runtime phase events reduce into Longhouse runtime state and project to canonical artifacts. |
| `transcript_binding` | P0 | Raw provider transcript events bind to a stable provider session id and canonical Longhouse events. |
| `collect_raw_evidence` | P0 | stdout/stderr/provider logs/transcripts are persisted on success and failure. |
| `parse_ingest_project` | P0 | Raw evidence becomes canonical events, ingests into Longhouse, and projects a session/timeline. |
| `db_ingest_project` | P0 | Canonical events ingest through `AgentsStore` into an isolated SQLite DB, then session events/counts/export/timeline reads prove durable Longhouse projection. |
| `session_projection` | P0 | Canonical events project into the stable session-detail shape for every provider. |
| `timeline_projection` | P0 | Canonical events project into the stable timeline/card shape for every provider. |
| `tool_call_result` | P1 | Tool call/result events are paired and attributed; workspace side effects match the fixture. |
| `steer_active_turn` | P1 | Active-turn steering reaches the provider control lane or reports an explicit unsupported/blocked gap. |
| `pause_request_detect` | P1 | Runtime `needs_user` plus provider question evidence projects to a pending Longhouse pause request. |
| `answer_pause_request` | P1 | Longhouse answer/resolve service works, or provider-held live answer delivery reports an explicit blocked/unsupported gap. |
| `interrupt_cancel` | P1 | A long active turn can be interrupted without corrupting session evidence. |
| `resume_reattach` | P1 | A prior session can be resumed or explicitly reports an unsupported gap. |
| `terminate_cleanup` | P1 | Termination/cleanup projects owned-resource cleanup or reports an explicit unsupported gap. |
| `multi_turn_continuity` | P1 | Follow-up input depends on prior turn state and stays in the same session. |
| `live_token_streaming` | P1 | Model-visible behavior works; streaming is verified only when declared. |
| `permission_prompt` | P2 | Permission approve/deny paths are observable where supported; OpenCode currently proves the bridge reply transport hermetically, while live held-provider prompts remain a stronger gate. |
| `external_event_channel` | P2 | Hook/inbox/external input reaches the session where supported. |
| `crash_timeout_cleanup` | P2 | Timeouts/crashes leave diagnosable artifacts and no orphaned managed process. |

## Scenario Statuses

| Status | Meaning |
| --- | --- |
| `pass` | Required behavior was observed and required artifacts exist. |
| `fail` | Required behavior was absent, malformed, or contradicted. |
| `unsupported_gap` | Capability is required by the target profile but the provider does not support it. |
| `not_applicable` | Capability is outside the provider's declared target profile. |
| `blocked` | Infrastructure, credentials, staging, or private machine state prevented a valid measurement. |
| `flaky` | Repeated runs disagree beyond the scenario's accepted retry policy. |
| `xfail_with_expiry` | Known temporary failure with owner, reason, and expiry. |

Skipped work is not a status. It must become `not_applicable`,
`unsupported_gap`, `blocked`, or `xfail_with_expiry`.

## Evidence Package

Every scenario run must produce an immutable evidence package. Baselines compare
normalized proof, but raw evidence must survive so parser and provider failures
remain diagnosable.

Recommended shape:

```text
manifest.json
raw/
  stdout.log
  stderr.log
  terminal.log
  provider-transcript.*
  provider-events.jsonl
  process-or-server.log
input/
  prompt.txt
  control-events.jsonl
  permission-decisions.jsonl
workspace/
  fixture-manifest.json
  final-manifest.json
  diff.patch
events/
  provider-raw-events.jsonl
  canonical-longhouse-events.jsonl
  unknown-provider-events.jsonl
  parser-diagnostics.json
longhouse/
  ingest-result.json
  session-projection.json
  timeline-projection.json
  tool-call-results.json
assertions/
  results.json
  timing.json
diff/
  baseline-ref.json
  normalized-diff.json
  semantic-diff.json
redaction/
  policy.json
  secret-scan.json
```

Evidence rules:

1. Raw provider evidence is preserved before normalization.
2. Unknown provider fields/events are preserved and surfaced as yellow review
   items unless they break a required contract.
3. Live-provider failures and Longhouse parser/ingest failures are separated in
   assertion output.
4. Failure artifacts must be written before outer orchestration timeouts expire.
5. Baselines are scoped by provider, provider version/channel, adapter version,
   scenario id/version, profile, platform, and fixture hash.

## Baselines And Diffing

Universal assertions are primary; baselines catch drift. A candidate proof can
be accepted only when:

- all required P0 scenarios pass
- required P1 scenarios pass or are explicitly approved non-applicable gaps
- evidence packages are complete
- no severe unreviewed baseline diff exists
- raw evidence has been reviewed for secrets and diagnosability

Diffs should compare stable structural fields:

- capability declarations and observed support
- event type/field presence
- session ids and continuity semantics
- tool call/result pairing
- Longhouse ingest/projection shape
- universal `action_matrix`/`control_surface` row status, support, evidence
  level, proof scope, canary, and failure code
- failure codes and severity
- timing only through bounded thresholds, not exact durations

Do not baseline exact assistant prose except for tiny sentinel markers in smoke
scenarios. Provider quality is not the release-proof target; Longhouse
compatibility is.

## Scoring

Scores should be computed from scenario and baseline state, not guessed.

Per provider/profile:

- capability coverage: required capabilities proved / required capabilities
- scenario conformance: weighted pass rate over applicable P0/P1/P2 scenarios
- evidence completeness: required evidence files present
- Longhouse integrity: parse, ingest, session projection, timeline projection,
  and tool pairing success
- baseline coverage: applicable scenarios with accepted baselines
- regression severity: candidate vs accepted baseline
- flake rate: disagreement across retries

Release verdict:

| Verdict | Rule |
| --- | --- |
| `green` | All P0 pass, required P1 pass or approved not-applicable, evidence complete, no severe diff. |
| `yellow` | P0 pass but there is a P1 gap, unsupported required capability, new unknown provider event, incomplete-but-diagnosable evidence, flake, or missing baseline. |
| `red` | Any P0 failure, claimed capability failure, ingest/projection failure, missing raw evidence on failure, lost tool result, corrupted resume/interrupt, crash/hang without diagnostics, or secret leakage. |

Roadmap completion should separately report:

- adapter conformance
- scenario migration from one-off tests
- provider branching remaining in the runner
- evidence completeness
- baseline coverage
- Longhouse ingest coverage
- Sauron invocation/reporting integration
- flake governance

## Current One-Off Crosswalk

This crosswalk records how today's provider-specific work should migrate into
the universal harness. "Reusable" means the behavior can become a universal
scenario assertion. "Adapter internal" means the code remains provider-specific
behind the adapter. "Migration candidate" means the current test should be
rewritten to call the shared scenario runner.

| Current work | Future scenario | Role |
| --- | --- | --- |
| `server/zerg/services/managed_provider_contracts.py` and `server/zerg/config/managed_provider_contracts.json` | Capability/profile declaration | Reusable vocabulary; extend rather than replace. |
| `scripts/qa/provider-release-proof.py` | Proof wrapper, normalization, baseline artifact generation | Reusable shell; should eventually call universal runner instead of provider-specific canary scripts. |
| `server/zerg/qa/provider_live_canary.py` Claude binary/channel/PTY checks | `probe_identity`, `launch_managed_session`, `collect_raw_evidence` | Migrated into Claude `managed_session_e2e`; live-token send/steer still need promotion. |
| `server/zerg/qa/managed_claude_live.py` | `send_receive`, `live_token_streaming`, `interrupt_cancel`, `multi_turn_continuity` | Migration candidate; PTY loop and channel readiness are Claude adapter internals. |
| `server/zerg/qa/codex_provider_release_canary.py` | `probe_identity`, `run_prompt_once`, `launch_managed_session`, `resume_reattach`, `send_receive`, `interrupt_cancel`, `tool_call_result`, `live_token_streaming` | Partly migrated: Codex `managed_session_e2e`, `interrupt_cancel`, `tool_call_result`, and `live_token_streaming` now call this canary; live active-turn steer behavior still needs promotion. |
| `server/zerg/qa/provider_live_canary.py` OpenCode server/schema/session checks | `launch_managed_session`, `send_receive`, `resume_reattach`, `interrupt_cancel`, `parse_ingest_project` | Partly migrated: OpenCode `managed_session_e2e`, `interrupt_cancel`, and `resume_reattach` now call this canary; remaining live-token scenarios still need promotion. |
| `server/zerg/qa/provider_live_canary.py` Antigravity plugin/global hook checks | `probe_identity`, `external_event_channel`, `send_receive` | Migration candidate; hook/inbox setup is Antigravity adapter internal. |
| `scripts/qa/provider-control-e2e-canary.py` | `send_receive`, `interrupt_cancel`, `tool_call_result`, `external_event_channel`, `live_token_streaming` | Partly migrated: Claude `interrupt_cancel` and `live_token_streaming`, OpenCode `tool_call_result` and `live_token_streaming`, plus Antigravity `live_token_streaming`, now call this canary; keep provider-specific fakes as adapter test fixtures. |
| Engine parser golden/adversarial tests | `parse_ingest_project` fixture replay | Reusable as `fixture_replay` scenarios. |
| Shipper/ingest/session projection tests | `parse_ingest_project`, `timeline_projection` | Reusable Longhouse assertions under the runner. |
| Sauron release-envelope/provider-status jobs | Private invocation and reporting | Sauron-owned runner/reporting; should consume universal artifacts, not provider-specific semantics. |

## Implementation Status

The first implementation slices exist. They intentionally avoid token-spending
provider calls, while proving the shared runner and release-proof attachment
shape:

1. Adapter protocol/data classes and scenario result schema exist.
2. MVP adapters exist for Claude Code, Codex/OpenAI, OpenCode, and
   Antigravity.
3. `probe_identity`, `adapter_conformance`, `collect_raw_evidence`, and fixture
   `parse_ingest_project` run through shared scenario code.
4. `provider-release-proof.py --run-universal-harness` attaches the universal
   run artifact, normalized universal canaries, and prefixed universal
   operation evidence.
   `provider-release-proof-maturity.py` can then roll coverage inventory,
   accepted-baseline status, and optional universal harness action-matrix
   artifacts into a machine-readable maturity report.
5. `control_surface` emits the same launch/send/steer/pause/interrupt/resume/
   terminate/tail/runtime/transcript/tool rows for every provider and is
   captured as a normalized release-proof artifact.
6. `session_projection` and `timeline_projection` are first-class universal
   scenarios for every provider and are included in default release-proof
   universal harness runs.
7. `run_prompt_once` has a safe Codex/OpenAI projection and typed
   `unsupported_gap` results for unsafe providers.
8. Codex/OpenAI and OpenCode have first no-token/session-safe
   `launch_managed_session` and `send_receive` projections behind the universal
   runner.
9. The CLI entrypoint has a broad all-provider fake/no-token smoke that runs
   identity, evidence capture, projections, run/send/session, pause detection,
   tail/runtime/transcript, multi-turn continuity, crash cleanup, and
   provider-specific `managed_session_e2e` lanes in one command. It also
   generates provider-scoped synthetic old/new proof pairs and proves
   `old_new_release_diff` both as a top-level scenario and inside the
   `full_action_suite` execution matrix. Implemented scenarios must pass;
   unsafe provider mechanics must report operation-level `unsupported_gap`
   evidence.
10. OpenCode has the first real no-token `managed_session_e2e` lane. It calls the
   existing provider-live canary to prove server startup, schema, session
   create/get, `prompt_async noReply`, transcript marker recovery, process
   reattach, and abort behavior, then writes canonical Longhouse-style
   event/session/timeline projections.
10. That OpenCode lane now feeds the provider-live raw rows through isolated
   Longhouse SQLite ingest and verifies durable events, session counts, export
   JSONL, query lookup, timeline listing, and preserved provider-session
   binding.
10. Claude `launch_managed_session` now calls the provider-live no-token
   command/channel/PTY contract, requires passing `launch_local` evidence, and
   DB-ingests those rows. Claude send/receive remains an explicit no-token gap.
10. `send_message` action coverage can be proven by any mapped executable
   scenario that carries passing `send_input` operation evidence. This lets
   Claude's channel-control `interrupt_cancel` canary and Antigravity's
   hook/inbox `managed_session_e2e` canary cover the abstract send action while
   `send_receive` still records the no-token response-binding gap.
10. `session_identity` action coverage uses the same "any mapped scenario"
   policy, but requires a provider session id. A passing managed launch or
   managed-session e2e can prove Longhouse captured provider and Longhouse
   session identity even when `resume_reattach` remains an explicit separate
   gap for that provider.
10. Codex `resume_reattach` now calls the existing provider-release canary,
   requires passing reattach evidence, and DB-ingests the resulting rows when
   Runtime Host credentials are present; without credentials it reports the
   typed Runtime Host credentials gap.
10. Claude, Codex, and OpenCode `interrupt_cancel` are executable universal
   control scenarios. Claude calls the provider-control channel canary and
   DB-ingests no-token send/steer/SIGINT evidence. Codex calls the
   managed-live-interrupt canary and returns an explicit Runtime Host
   credentials gap when not configured. OpenCode calls the provider-live
   session.abort canary. All pass lanes DB-ingest their evidence.
11. Claude and Codex `steer_active_turn` are executable universal control
   scenarios. Claude calls the provider-control channel canary, evaluates the
   steer metadata path directly, and DB-ingests the resulting no-token control
   rows. Codex seeds a managed-local Codex session, dispatches through
   `steer_text_to_managed_local_session`, and records the `session.steer_text`
   command/payload/transport. OpenCode and Antigravity still report explicit
   unsupported gaps.
12. Codex and OpenCode `tool_call_result` are executable universal observation
   scenarios. They call their existing real-tool canaries, DB-ingest linked tool
   call/result rows, and expose `universal_tool_call_result` evidence through
   release proof.
13. OpenCode `resume_reattach` is an executable universal session-continuity
   scenario. It calls the provider-live process-restart reattach canary,
   DB-ingests reattach evidence, and exposes `universal_reattach` evidence
   through release proof.
14. Claude `live_token_streaming` is an executable universal one-shot
   live-token scenario. It calls real print, DB-ingests prompt/result marker
   rows, and exposes `universal_live_token_streaming` evidence through release
   proof without claiming managed-session steer.
15. `baseline_compare` is an executable universal release-diff scenario. It
    generates comparable synthetic provider-release-proof envelopes from the
    current action/control artifacts, calls `provider-release-proof-baseline.py
    diff`, and records the baseline/candidate proof plus diff artifact for all
    providers.
15. Codex `live_token_streaming` is an executable universal live-token
   scenario. It calls managed live-send, DB-ingests user/assistant marker rows
   when Runtime Host credentials are present, and exposes live-token credential
   gaps as yellow release-proof evidence.
16. Antigravity `live_token_streaming` is an executable universal live-token
   scenario. It calls real-agy hook-inbox injection, DB-ingests the queued user
   message plus marker response, and exposes `universal_live_token_streaming`
   evidence through release proof.
17. OpenCode `live_token_streaming` is an executable universal live-token
   scenario. It calls real-print `opencode run --format json`, DB-ingests the
   prompt/result marker rows, and exposes `universal_live_token_streaming`
   evidence through release proof.
18. OpenCode `permission_prompt` is an executable universal bridge-transport
   scenario. It writes an OpenCode bridge state file, sends
   `permission-reply` through the real Longhouse bridge command to a held fake
   upstream permission request, and records the forwarded decision/auth/path
   evidence. Claude, Codex, and Antigravity still report the explicit live
   provider-held permission prompt gap.
19. `full_action_suite` runs `managed_session_e2e` and lets any-mapped abstract
   actions count it only when the result carries the operation evidence required
   by that action. Antigravity hook/inbox e2e now covers abstract
   `send_message` and `session_identity` while its response, interrupt, and
   reattach gaps remain visible.
20. Evidence packages are written for pass, fail, and unsupported results.
21. Existing one-off canaries remain compatibility lanes until each behavior is
   migrated and baselined.

Next implementation target: migrate Claude managed live-token send mechanics
and the remaining Codex, OpenCode, and Antigravity control gaps behind the same
runner.
