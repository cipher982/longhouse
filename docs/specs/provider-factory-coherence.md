# Provider factory coherence

**Status:** revision 4 design; implementation underway. Phase 0 shipped
2026-07-28: clifford is deployed at control-plane `07a40bb` / longhouse
`c3065017a0`, Codex qualification profile `codex_tool_call_result_v1`,
verified via a healthy post-deploy tick. The provider census is a CI-checked
generated artifact (`scripts/generate_provider_census.py`,
`docs/generated/provider_census.json`, 189 files). Phase 0 commits, in order:
control-plane `355be4b` (profile flip) + `07a40bb` (bridge-timeout fix caught
by Hatch Sol review — the flip alone would have silently broken on any run
taking 120-180s; deployed). Longhouse `c3065017a` (census, deployed) +
`313d3e31c` (path-component exclusion fix from the same Sol review — doc/CI
tooling only, no runtime effect, not yet redeployed to clifford but will ride
the next Phase 1+ deploy).

Phase 1 shipped 2026-07-28: `server/zerg/qa/provider_factory_model.py`
(longhouse `0c9455e7e`) plus a cross-repo derivation check
(control-plane `0dab206`) — see "Phase 1 model" below for the resolved
vocabulary and `plan_run`'s design. Not deployed to clifford (Phase 1 is
planning-model code with no runtime hookup; nothing about current execution
changed).

Phase 2 in progress, four pieces shipped 2026-07-28, none deployed to
clifford (pure refactors / additive-only right up until the dispatch map,
which is code-complete but deliberately not deployed — see below). Shadow-compare
for `codex_tool_call_result_v1` ran for real against a real Codex binary and a
real API key (result below); deploying to clifford is still an open decision,
not something this result auto-triggers:

- **Step 1** (longhouse `2eeacfc87`): the versioned run-evidence index
  (`server/zerg/qa/run_evidence_index.py`) and a pure oracle extracted from
  every one of the 10 release-lane `_PROFILES`, each verified to preserve its
  current executor's behavior exactly (898-test qa/provider suite unchanged).
- **Step 2, prerequisite** (control-plane `67251ff`, fixed `8ceb4c3`):
  `run_multi_profile_tick` lets one provider carry multiple qualification
  profiles in one tick, purely additive alongside the unchanged single-profile
  `run_tick`/`run_codex_tick`. Hatch Sol review of the first draft caught a
  real cursor-coordination bug (a later profile's success could silently
  advance the shared cursor past a release whose earlier profile's evidence
  was rejected, permanently skipping its retry) — fixed with a new
  `FactoryState.advance_cursor()` and a `cursor_advance_eligible` signal, with
  a regression test for the exact scenario.
- **Step 3, bounded slice** (longhouse `882d91beb`), scoped per Sol's explicit
  recommendation: `run_harness()` now derives `provider_bins` from
  `provider_builds`' own entrypoints when staged (an ambient/PATH binary
  cannot win by construction) and calls `verify_provider_builds()` itself,
  before and after execution, rather than relying on external callers to
  remember. Two tests prove both properties directly.

Bridge/dispatcher design (below) fully landed 2026-07-28, code-complete and
NOT deployed to clifford:

1. Finalizer promotion, harness scenario, and the new
   `scripts/qa/provider-harness-qualification.py` CLI (longhouse `882d91beb`
   and prior Step 3 commits) — `codex_tool_call_result.emit_proof_bundle()`
   and `codex_helm_interrupt.emit_proof_bundle()` are the shared finalizers;
   `provider_harness_qualification.py` builds a real `ProviderBuildRef`,
   drives `run_harness()`, and reuses them. Two real end-to-end bugs were
   caught by testing against a scripted fake codex binary (not mocks): the
   `codex_tool_call_result_strict` scenario payload wasn't nested under
   `strict_oracle` like `interrupt_cancel`'s was, and the bridge's
   `_strict_outcomes()` reader didn't recognize `interrupt_cancel`'s own
   `"blocked"` status. Both fixed; regression tests added.
2. **Control-plane dispatch map** (control-plane `9412d9f`):
   `_qualification_bridge_script(provider, profile)` in `core.py` routes
   `codex_tool_call_result_v1`/`codex_helm_interrupt_v1` to
   `provider-harness-qualification.py` and every other profile to
   `provider-qualification.py` unchanged; rejects a harness profile paired
   with a non-codex provider. Wired into both `_run_v2_bridge`'s argv and
   `binary_factory.py`'s parallel caller. Fixing this also exposed a real
   correctness gap in `_longhouse_identity`: it unconditionally checksummed
   `provider-qualification.py` into the manifest regardless of which script
   the profile actually launches — now it checksums whichever script
   `_qualification_bridge_script` resolves, so the manifest's recorded
   runner provenance matches the runner that actually ran. Tests assert the
   exact launched path for both strict profiles and one ordinary profile
   (`codex_release_identity_v1`), per Sol's explicit ask. 510/510
   control-plane tests pass.

   **This dispatch is unconditional, not env-gated** — the only reason it is
   inert today is that nothing has redeployed control-plane to clifford
   since it landed. Clifford's live `.env` already sets
   `PROVIDER_FACTORY_QUALIFICATION_PROFILE=codex_tool_call_result_v1`
   (Phase 0), so deploying this commit to clifford would flip that lane's
   live script immediately, not gradually. Do not run
   `deploy-provider-factory.sh` for this change until the shadow comparison
   below has actually run — "code-complete but not deployed" is true only
   because deployment is a separate, still-unperformed action, not because
   the code checks anything at runtime.
3. Equivalence tests, partial (longhouse `852456782`, `9d424127d`): one test
   runs `codex_tool_call_result.run()` (legacy, inline subprocess) and
   `provider_harness_qualification.run_codex_tool_call_result()` (harness,
   shared `run_codex_real_tool_command()`) against the identical fake codex
   package with `CODEX_MANAGED_PACKAGE_ROOT` set exactly as control-plane
   sets it in production for this profile, and asserts identical outcomes —
   this genuinely exercises two different observation-producing code paths,
   not a tautology, since only the pure oracle and the finalizer are shared.
   Writing this test surfaced a real, previously undetected bug in the
   bridge (not yet deployed anywhere, so no live impact): when bridge
   credentials are missing, `interrupt_cancel`'s Stage 1 falls back to a
   hermetic-only dispatch proof reporting `status="pass"/"fail"` with no
   `strict_oracle` key at all — `_strict_outcomes()`'s status allowlist
   didn't recognize this shape and misclassified it as
   `INFRASTRUCTURE_ERROR` instead of `BLOCKED`, diverging from the legacy
   path's own credentials-missing handling. Fixed by simplifying
   `_strict_outcomes()` to key off `strict_oracle`'s presence alone; a
   regression test reproduces the exact hermetic-fallback payload shape.
   `codex_helm_interrupt_v1`'s equivalence test (longhouse `250667f78`) is
   also landed: its full live-interrupt path needs a managed engine/MCP
   bootstrap that isn't hermetically testable end to end, but the
   credentials-missing case is reachable by both paths without one —
   legacy's `_required_environment()` short-circuits to BLOCKED
   immediately; the harness's `interrupt_cancel` Stage 1 runs a real (not
   mocked) hermetic-only dispatch proof and only reaches BLOCKED via the
   `_strict_outcomes()` fix. The test confirms the fix actually produces
   equivalence, not just the isolated shape the regression test checks.
   **Not yet written:** equivalence at the finalizer/manifest level (both
   paths already share `emit_proof_bundle()`, so this would mostly test
   serialization, but Phase 2's own definition of done still names it), and
   neither equivalence test covers the full live-token success path (both
   profiles' actual live interrupt/tool-call execution against a real
   engine) — only the credentials-missing/hermetic-fallback case for helm,
   and a hermetic fake-binary pass for tool_call_result.

**Explicitly not started**, and multi-session scope per Sol: control-plane
harness process launching, full-column `QualificationSandbox` policy,
port/process lifecycle, intervention production, run-evidence-index
production (the schema exists; nothing writes one yet), oracle invocation
over that index, shadow-compare (the actual comparison run against real
clifford-shaped inputs, now that both sides are wired), and retiring the
duplicate launch-and-collect code + the env-var selector. Revised pacing per
David: this system has zero users, so there is no elapsed-time gate anywhere
in this phase — see "The shadow gate" below — but the remaining work is real
engineering depth, not waiting, and half-wiring it would create a third
execution path instead of convergence.

Two rounds of independent review (Hatch Fable, Hatch Codex Sol, Hatch
OpenRouter Kimi K3) have corrected this document eleven times. Corrections are
listed in the appendix rather than narrated inline. Claims here are checkable
and have been checked; if you find one that is wrong, that is a defect.

## What this system is

Longhouse integrates with CLI coding agents it does not own — Claude Code,
Codex, OpenCode, Antigravity, Cursor. Those vendors ship on their own schedule.
The provider factory answers two questions without a human finding out the hard
way: does a Longhouse change break a provider integration, and does a provider
release break Longhouse.

Vocabulary, because the eleven documents this replaces each used these
differently:

- **provider** — an upstream CLI agent Longhouse drives.
- **capability** — something Longhouse claims a provider can do (`interrupt`,
  `resume`, `answer_pause`). Declared in `schemas/managed_providers.yml`.
- **assertion** — a named postcondition that, if it holds, is evidence for a
  capability. Example: `interrupt_terminal_cancelled_or_interrupted`.
- **scenario** — an executable procedure that produces assertions. Two disjoint
  families exist today.
- **qualification profile** — the release lane's unit of work: one provider,
  one scenario, a fixed assertion set. Example: `codex_release_identity_v1`.
- **evidence class** — how trustworthy a run is. Schema vocabulary is exactly
  `hermetic`, `live_no_token`, `live_token`.
- **build provenance** — where the executed binary came from: generated fake,
  staged upstream release, observed local install.
- **column** — one provider run across the full scenario set.
- **the diagonal** — real upstream binary crossed with the full scenario set.
  Empty today, structurally rather than by configuration.

## What it proves today

Verified 2026-07-28 against factory container `ab9e0a2` (healthy, `tick_count`
15) and `main` at `072f14dde2`.

**There are two execution stacks.** They overlap at one shared driver and
nowhere else.

| | Release lane | Commit lane |
|---|---|---|
| Entry | `provider_qualification.py` → `_PROFILES` | `universal_agent_harness.py` |
| Implementation | 10 dedicated per-profile modules | one 4,446-line adapter class |
| Scenarios | 1 per provider | 36 declared; 22 selected by the weekly smoke |
| Binary | real, downloaded from upstream | generated fake |
| Providers | 4 — no Cursor | 5 |
| Trigger | upstream release, polled every 900s | push and weekly cron |
| Runs by | private factory on clifford | GitHub Actions |

The release lane has qualified 26 releases across 4 providers and works. Its
deployed assertions:

- `claude` → `claude_cli_channel_contract_preserved`, `real_print_marker_returned`
- `opencode` → `serve_session_contract_preserved`, `process_restart_reattach_preserved`
- `antigravity` → `hook_inbox_contract_preserved`, `real_print_injection_observed`
- `codex` → `exact_executable_identity_observed`, `reported_version_matches_expected`

Codex is the most-used provider and its release lane proves only that the
downloaded file is the file that was expected. `codex_tool_call_result_v1` and
`codex_helm_interrupt_v1` are implemented and not deployed.

The diagonal is empty because no code path executes a real upstream binary
through the universal scenario set. That claim, and every row below, is
generated — not hand-typed — from `provider_factory_model.plan_run()` against
the live schema and registry constants:

```
uv run --directory server python scripts/render_provider_factory_status.py
```

<!-- generated by server/scripts/render_provider_factory_status.py — regenerate, do not hand-edit -->

| Provider | Trigger | Build provenance | Status |
|---|---|---|---|
| codex | release_poll | staged_release | runs — 1 scenario |
| codex | push | generated_fake | runs — 1 scenario |
| codex | weekly_cron | generated_fake | runs — 22 scenarios |
| codex | manual | observed_install | runs — 1 scenario |
| claude | release_poll | staged_release | runs — 1 scenario |
| claude | push | generated_fake | runs — 4 scenarios |
| claude | weekly_cron | generated_fake | runs — 22 scenarios |
| claude | manual | observed_install | never runs — remaining capability-proof scenario_ids for claude are orphaned: ['claude_coordination_awareness_create', 'claude_coordination_awareness_post_compaction', 'claude_coordination_directed_input'] |
| opencode | release_poll | staged_release | runs — 1 scenario |
| opencode | push | generated_fake | runs — 4 scenarios |
| opencode | weekly_cron | generated_fake | runs — 22 scenarios |
| opencode | manual | observed_install | never runs — every capability-proof assertion declared for opencode is already satisfiable by an automated trigger |
| antigravity | release_poll | staged_release | runs — 1 scenario |
| antigravity | push | generated_fake | runs — 4 scenarios |
| antigravity | weekly_cron | generated_fake | runs — 22 scenarios |
| antigravity | manual | observed_install | runs — 1 scenario |
| cursor | release_poll | staged_release | never runs — cursor has no registered release lane (no *_lane() in provider_factory/registry.py) |
| cursor | push | generated_fake | runs — 4 scenarios |
| cursor | weekly_cron | generated_fake | runs — 22 scenarios |
| cursor | manual | observed_install | never runs — remaining capability-proof scenario_ids for cursor are orphaned: ['cursor_coordination_awareness_create', 'cursor_coordination_directed_input', 'cursor_steer_rejection'] |

**The diagonal** (`staged_release` binary x `weekly_cron`'s full scenario set) —
one row per provider, all `never runs`, held by
`test_render_provider_factory_status.py::test_diagonal_is_empty_for_every_provider`:

| Provider | The diagonal (real binary x full scenario set) |
|---|---|
| codex | never runs — the weekly matrix only runs against generated fake binaries |
| claude | never runs — the weekly matrix only runs against generated fake binaries |
| opencode | never runs — the weekly matrix only runs against generated fake binaries |
| antigravity | never runs — the weekly matrix only runs against generated fake binaries |
| cursor | never runs — the weekly matrix only runs against generated fake binaries |

This is the first table in this document's history where "is this still true"
is a test run, not a re-read.

## Why that is not enough

The factory works. It cannot explain itself, and parts of it are not true.

### Nothing derives from the authority

`schemas/managed_providers.yml` is declared the single authority and its shape
is good: per-capability `required_assertions` carrying `scenario_id`,
`oracle_source`, `acceptable_evidence` (21 declarations), `max_age_seconds`.

Almost nothing reads it, and the duplication differs per provider:

| Provider | Independent hand-written statements of release artifacts |
|---|---|
| codex | 2 — the schema, and `control-plane/provider_factory/core.py` |
| claude, opencode, antigravity | 3 — schema, `release_contract.py`, `registry.py` `_*_spec` |

`release_contract.py` derives its Codex entry from `core.py` by comprehension
and compares the result to the generated public contract. It cannot see the
third statement for three of five providers, and it cannot see itself.

`web/src/lib/providers.ts` declares itself "single source of truth for provider
capability claims" and then says it mirrors `managed_provider_contracts.json`.
Both claims are in its header. The specific falsehood it once carried about
Cursor was fixed in `4402f99ea`; the hand-mirroring mechanism was not.

### The provider census

Rule, stated because revision 1's undefined grep produced five different
answers on re-check: files tracked by git with extension `.py`, `.ts`, `.tsx`,
`.rs`, excluding `/generated/` and `node_modules`, containing quoted literals
for two or more distinct provider names. **189 files**, about half under
`server/tests_lite/`.

Not all are duplication. Some are intentional policy — a two-provider
experiment, provider-specific historical storage handling, a backfill rule. A
blanket literal ban would replace semantic review with a noisy allowlist, which
is why the enforcement model below is narrower.

### The adapter hierarchy carries no behavior

`UniversalProviderAdapter` spans lines 851–5296 of a 9,317-line file: 4,446
lines. The five provider subclasses at 5299–5316 contain a docstring and
nothing else. `ADAPTER_CLASS_BY_PROVIDER` at 5319 maps five names to those five
identical classes and is consulted twice — at 951 for a conformance report, and
at 7919 in `adapter_registry()`, which instantiates a class per provider on
every harness run. The skeleton is not inert; it dispatches constantly, to
nothing. Line 7919 is also the entry point a plan-driven executor will call, so
it belongs in the convergence work regardless.

The file holds 81 provider-name literals (opencode 35, codex 17, claude 16,
antigravity 11, cursor 2). 48 are inside the base class; the other 33 are in
module-level dispatch and provider projection functions below it.

`AgentHarnessAdapter`, the Protocol at line 724, declares **33 methods**; the
base implements 80. A 33-method Protocol is already at the width where
per-provider decomposition may produce five modules worse than one class.
Whether the seam belongs on provider at all, rather than on scenario, is open.

### The planning vocabulary does not exist

The schema declares 13 distinct `scenario_id` values. `DEFAULT_SCENARIOS` in
the smoke wrapper declares 22. **The intersection is empty.**

```
schema:  claude_real_print, opencode_server_contract, antigravity_hook_inbox,
         codex_coordination_awareness_create, cursor_steer_rejection, ...
harness: adapter_conformance, action_matrix, session_projection,
         timeline_projection, launch_managed_session, ...
```

Different concepts wearing one word. Schema scenarios are semantic proof
procedures bound to capability assertions; harness scenarios are pipeline
stages. There is no mapping to derive a plan from.

The mismatch runs deeper. `acceptable_evidence` is `{hermetic, live_no_token,
live_token}`, which does not align with build provenance `{generated_fake,
staged_release, observed_install}`. `FAKE_VERSION_BY_PROVIDER`, the `_*_spec`
binary-name sets, and the version regexes encode facts the schema does not
represent at all.

### There is no equivalence oracle

`structural_fingerprint_v1` collapses every string to `"string"` unless its key
is one of 13 discriminators, and every int to `"int"`. It cannot see
`complete: true → false`, `returncode: 0 → 1`, or a wrong executable path —
precisely the reversed-boolean failure this epic exists to prevent. It reads
only `.json` and `.jsonl`. `canonical_digest_v1` is not the alternative:
`test_live_value_churn_moves_canonical_digest_but_not_structure` documents that
it moves on ordinary run-to-run churn.

Both are emitted by `EvidencePackage.finalize_measurement()` in the harness.
The release lane never calls it. No pre/post comparator exists in either
repository.

### The documentation is a committee

Eleven provider specs, ~250KB, five presenting as the map, one stale with ~60
references to a retired arrangement. Implementation surface: ~19,100 lines in
`server/zerg/qa/`, ~19,800 in `scripts/qa/`, ~6,400 in
`control-plane/provider_factory/`.

## What this replaces

On completion `docs/specs/` holds one provider factory document. The five
competing maps are deleted; git holds the history. A phase that lands without
deleting its share has not landed.

## Position on cheap compute

Cheap compute is how you buy real binaries instead of fakes and full columns
instead of one scenario. It is not how you buy trust in an oracle, and this
system's recorded failure mode is oracles that lie: 3,633 tests passing against
a binary that happened to be on the laptop, a test asserting a command that does
not exist, and `test_cursor_storage_v2_honesty` asserting the exact defect it
was named after. Volume without a signal plan recreates this document's
complaint one level up, in the alert stream. The signal plan therefore gates
the coverage phase.

## Target architecture

### The convergence: one orchestrator, one oracle layer

The two stacks are not two implementations of one thing. Each is a
half-implementation of two different things, which is why "merge" had no
obvious direction.

| | Execution | Judgment |
|---|---|---|
| Universal harness | orchestration plus its own drivers; also calls `provider_live_canary` | scenario-local assertions and status classification, but no `ProviderCapabilityProofRecord` and no mapping to schema capabilities |
| Release-lane modules | `provider_live_canary.py` for claude/opencode/antigravity; raw `subprocess` in `codex_tool_call_result`, `codex_helm_interrupt`, `provider_release_identity` | typed records carrying `AssertionOutcome` and `EvidenceClass`, mapped to contract assertion IDs |

**The harness becomes the sole execution orchestrator.** Low-level drivers,
including `provider_live_canary.py`, remain execution components the harness
calls. The harness already calls it at five sites — `_run_opencode_managed_session_e2e`
(3490), `_run_opencode_interrupt_cancel` (3878), `_run_opencode_resume_reattach`
(3935), `_run_claude_provider_live_projection` (4011),
`_run_antigravity_launch_managed_session` (4920) — and it is also used by
`provider_live_proof_publish.py` and the shipped `provider live` CLI. It is
shared infrastructure, not release-lane private execution.

That shared call is the best available evidence the seam is cut in the right
place: the harness's OpenCode interrupt and resume scenarios already produce
the same observations the release lane's OpenCode assertions judge, through the
same helper. Convergence is formalizing something the code already does.

**The qualification modules become pure oracles** — mappings from indexed run
evidence to typed capability-proof records. They stop launching binaries.

**The factory stops owning provider-specific execution logic.** It stages
builds and launches the complete harness process through `QualificationSandbox`.

The sandbox boundary is the outer harness process. Do not inject the sandbox
into individual harness subprocess calls: it accepts only the pinned Longhouse
checkout as `cwd`, while provider drivers deliberately use per-run workspaces.

What deletes in this phase is the duplicated launch-and-collect scaffolding in
`provider_release_identity.py`, `codex_tool_call_result.py`, and
`codex_helm_interrupt.py`, once equivalent harness observations exist. What is
**ported rather than deleted** are the intervention behaviors the harness does
not have: marker-prompt driving, restart-reattach choreography, live interrupt
delivery. Those become harness intervention stages. `provider_live_canary.py`
is retained, and its CLI and local-proof consumers stay supported.

The diagonal then fills by construction: staged build → orchestrator → every
oracle required by the resolved plan → proof store.

### The run-evidence index

The seam carries the whole epic, so it is specified rather than named.

Two deployed assertion families are timing-coupled perturbations, not passive
observation: `codex_helm_interrupt` drives an interrupt against a specific
in-flight turn and judges `interrupted_turn_id == sent_turn_id`;
`process_restart_reattach_preserved` kills and restarts a server mid-session.
The judgment is post-hoc in both cases, so the split holds — but only if the
intervention moves into the orchestrator and the index records what it bound to
at runtime. The interrupt target cannot be named in a static plan.

The index must carry: the serialized plan; an intervention log with runtime
bindings (action, monotonic timestamp, the turn id the executor bound to); raw
artifacts with checksums; build provenance; the sandbox receipt; and the
deployed Longhouse/control-plane SHA pair.

It is an index rather than a package because Claude and Antigravity profiles
combine no-token and optional live-token observations that live in different
harness scenario packages.

### One authority, generated consumers

The schema stays authority; all reviewers agreed typed code would relocate
duplication rather than remove it. Two enforcement mechanisms:

1. **Regenerate and diff** — one schema, generated consumers checked in, CI
   regenerates and fails on any diff. Covers every generated surface without a
   scanner.
2. **A narrow derivation check at universal fan-out boundaries only** —
   provider enumeration, capability projection, CI matrices, release-lane
   enablement, generated documentation. Intentional subsets name their policy
   in code rather than sitting in an allowlist.

### A planning model, then a planner

The planner cannot be built until the vocabulary exists. The model must state
the relationships among capability assertion, qualification profile, harness
scenario, build provenance, evidence class, and credential policy — the
relationships the empty intersection proves are undefined.

Then `plan_run` is a pure derivation over schema data: no I/O, no provider
branches, no environment reads. Commit the **generated plan matrix** —
`plan_run` over every provider × provenance × trigger cell, regenerated in CI,
diff-failing on drift. "This cell has never run" is a first-class renderable
state, which is how the diagonal stops being invisible.

### Phase 1 model (resolved 2026-07-28)

Three scenario kinds exist, not two. The spec's original framing — "schema
scenarios are semantic proof procedures, harness scenarios are pipeline
stages" — collapsed two different things on the schema side.

| Kind | Count | Identified by | Producer | Consumes |
|---|---|---|---|---|
| **Capability-proof scenario** | 13 `scenario_id`s in the schema | `(provider, capability)` → `required_assertions[].scenario_id` | 3 release-lane profiles whose own `SCENARIO_ID` equals the schema value (`claude_real_print_v1`, `opencode_server_contract_v1`, `antigravity_hook_inbox_v1`); 1 more (`codex_coordination_awareness_post_compaction`) has a CI-automated producer that runs on every push (`provider_coordination_scenarios.py`, codex-only) | An "observation" dict the oracle function transforms into typed booleans |
| **Identity-proof scenario** | 7 in `_PROFILES` (`provider_qualification.py`) | `(provider, profile)` key; own `SCENARIO_ID` constant that is never a schema value | The release lane itself — each launches its own subprocess (`--version`, or for `codex_tool_call_result`/`codex_helm_interrupt`, a real Codex invocation) | Nothing upstream; self-contained |
| **Harness pipeline scenario** | 36 declared; 22 on weekly cron, 4 on every push | Function name in `SCENARIO_RUNNERS` | CI — push runs 4 (`adapter_conformance`, `action_matrix`, `control_surface`, `old_new_release_diff`, via `validate-provider-cli-canaries`), weekly cron runs the full 22-scenario `DEFAULT_SCENARIOS`; both against fake binaries | Fixture data or a live adapter call |

A first draft of this table collapsed push and weekly-cron into one "commit
lane" trigger and called the coordination-proof producer "manual." Both were
wrong and both were caught by Hatch Sol review, not by re-reading this file:
`validate-provider-cli-canaries` (`Makefile`) runs only 4 harness scenarios on
every push, `provider-release-weekly.yml` runs the full 22 once a week, and
`contract-first-ci.yml`'s "Produce executable provider capability proof
bundle" step runs `provider_coordination_scenarios.py` — codex-only, hermetic
— on every push and PR. The corrected model below treats these as four
separate triggers (`release_poll`, `push`, `weekly_cron`, `manual`), not two.

**9 of the schema's 13 `scenario_id`s have zero producer of any kind** —
verified independently three ways: no non-test importer of
`awareness_create_assertions`, `directed_input_assertions`, or
`unsupported_steer_assertions`; `provider_control_oracles.py` (the declared
`oracle_source` for both `*_steer_rejection` scenarios) has no caller
anywhere, test or production; and there is no dynamic dispatcher anywhere in
the codebase that reads a schema `oracle_source` string and invokes it —
every reference is a hardcoded string used only for manifest generation and
digest-hashing (`provider_semantic_qualification.py:193`,
`managed_provider_contract_manifest.py:487`). These are not merely
undeployed, the way `codex_tool_call_result_v1` was before Phase 0 — nothing
in either repository can produce the observation their oracle functions
require. `codex_coordination_awareness_create`, `codex_coordination_directed_input`,
`claude_coordination_awareness_create`, `claude_coordination_awareness_post_compaction`,
`claude_coordination_directed_input`, `antigravity_steer_rejection`,
`cursor_coordination_awareness_create`, `cursor_coordination_directed_input`,
`cursor_steer_rejection` are declared and unfulfillable as declared. This is
what "never-run cell" means concretely, and the generated plan matrix below
renders exactly these 9 as such rather than asserting anything about them.

**Zero name-level overlap between the 36 harness scenarios and the 13 schema
`scenario_id`s is confirmed** (matches spec's "empty intersection"), but a
non-empty *conceptual* overlap exists — the same capability, observed by
different code, producing different evidence shapes:

| Capability area | Schema scenario_id(s) | Harness scenario(s) (conceptually adjacent, not equal) |
|---|---|---|
| `session.launch.helm` | `claude_real_print`, `opencode_server_contract`, `antigravity_hook_inbox` | `launch_managed_session`, `managed_session_e2e` |
| `session.reattach.helm` (opencode restart) | `opencode_server_contract` (`process_restart_reattach_preserved`) | `resume_reattach` |
| interrupt semantics | none (release lane has `codex_helm_interrupt`, an identity-proof scenario, not a schema `scenario_id`) | `interrupt_cancel` |
| tool-call result semantics | none (release lane has `codex_tool_call_result`, an identity-proof scenario) | `tool_call_result`, `tool_call_result_projection` |
| coordination/directed input | 9 orphaned `scenario_id`s above | `send_receive` (conceptually adjacent; no shared evidence shape) |

This is a proposed mapping for Phase 2/3 to formalize, not a claim that these
pairs are interchangeable today — `codex_tool_call_result`'s oracle requires
exactly one command, exact output, and exact linkage to the final message;
the harness's `tool_call_result` Codex driver does not yet produce that shape
(the spec already states this under Phase 2's profile-parity gate).

**A third, previously undocumented instance of the duplication pattern**: the
string `"codex_release_identity_v1"` is hardcoded as a fallback default in
three independent places — `docker-compose.provider-factory.yml` (fixed in
Phase 0), `provider_factory/registry.py::codex_lane()`'s parameter default,
and `provider_factory/service.py:88`'s `env.get(...)` fallback. Phase 0 fixed
the one that governs clifford's actual runtime behavior; the other two are
inert today (both are always called with an explicit `qualification_profile`
supplied by the env-reading path) but are exactly the kind of fact the
schema does not represent and code duplicates by hand. Left as-is
deliberately — removing the env-var mechanism entirely is Phase 2's job, not
Phase 1's, per Phase 0's "deliberate, documented choice" framing.

**Relationships, made concrete as `plan_run`**: implemented in
`server/zerg/qa/provider_factory_model.py`, split into two steps because a
single I/O-doing `plan_run` is not actually pure. `load_facts()` performs all
I/O once — parses `schemas/managed_providers.yml`, imports
`provider_qualification.py::_PROFILES`, AST-parses `DEFAULT_SCENARIOS` out of
the smoke wrapper's source without executing it (the wrapper's filename is
hyphenated and not import-safe), regex-parses the push-CI scenario override
out of the `Makefile`, and reads `config/provider-release-schedule.yml`'s
`weekly_unconditional` provider set. `plan_run(facts, provider,
build_provenance, trigger) -> PlanCell` then does no I/O at all: it is a
lookup over the resulting `ProviderFactoryFacts` snapshot plus a small number
of hand-verified constant tables the schema cannot express (which evidence
class each known producer actually generates per assertion, which
credentials a release-lane profile's bridge is allowed to receive). Every
`PlanCell` now carries `credential_requirement` and per-assertion
`assertion_status` (`acceptable_evidence` vs. `producible_evidence` vs.
`satisfiable`) — the first draft omitted both, and Phase 1 explicitly
requires evidence class and credential policy as modeled relationships, not
just build provenance and trigger.

That per-assertion status is what makes the `manual` trigger meaningful: it
is not merely "for scenario_ids where the release lane doesn't run
automatically." `codex_coordination_awareness_post_compaction` runs
automatically on every push (hermetic evidence), but the CI producer
hardcodes one of its two assertions to `False` and that assertion's schema
entry only accepts `live_token` evidence anyway — so that specific assertion
is `manual`-only despite its scenario_id having an automated producer.
Symmetrically, `antigravity_hook_inbox_v1` runs automatically on every
`release_poll`, but `real_print_injection_observed` is permanently blocked by
the oracle module itself (no isolated profile/data-root to run a real `agy
--print` safely) — so it is also `manual`-only, forever, unless that
constraint changes. Modeling at the scenario_id level would have missed both.

The generated plan matrix (`docs/generated/provider_factory_plan.json`,
`make validate-provider-factory-plan`) evaluates `plan_run` over every
`(provider, build_provenance, trigger)` cell — 60 cells (5 providers × 3
provenances × 4 triggers), 20 `runs` / 40 `never_run` — and is verified
against real system state as of this phase: it reproduces clifford's actual
deployed profile per provider (confirmed against the live `/health`
payload), push CI's actual 4-scenario override, weekly cron's actual
22-scenario default and 5-provider schedule, and renders all 9 orphaned
capability-proof scenarios and Cursor's absent release lane (`PROVIDER_REGISTRY`
in `control-plane/provider_factory/registry.py` has no `cursor_lane`, and
`registered_lane("cursor")` raises `ValueError`) as `never_run`. Nothing about
current execution changed to produce this; the plan is a read, not a write.

**The cross-repo check does not duplicate the fact it guards.** A first draft
had control-plane assert its `PROVIDER_REGISTRY` against a second
hand-copied dict — passing the review that it recreates exactly the failure
mode the epic exists to kill, since the two hardcoded dicts could silently
drift from each other. `control-plane/tests/test_deployed_profiles_match_plan_model.py`
now reads longhouse's *generated* `docs/generated/provider_factory_plan.json`
from the sibling checkout this workspace already assumes exists
(`deploy-provider-factory.sh`'s `PROVIDER_FACTORY_LONGHOUSE_SOURCE_REPO`
convention) and checks against that artifact instead — a real derivation
check, skipped rather than failed when the sibling checkout is absent.
control-plane's CI does not check out longhouse today, so this only runs
locally; wiring a real cross-repo CI check is Phase 2's "narrow derivation
check at universal fan-out boundaries" work, not Phase 1's.

### Public proposes, private disposes

Public Longhouse owns the declarative desired-proof contract. The private
factory consumes it as pinned versioned data and validates each plan against
**local policy** — which credentials exist, resource caps, allowed providers,
whether live-token scenarios are permitted — then compiles it into private
acquisition steps and records the resolved plan with the contract's hash.

The factory validates policy. It does not restate contract; restating contract
is how `EXPECTED_RELEASE_CHANNELS` happened.

Deploy pinning is already built: `deploy-provider-factory.sh` ships both repos
as bundles, pins them as one deployment, keeps prior images tagged, and
`--rollback TAG` restores the paired Longhouse commit. What is missing is
visibility — record the deployed SHA pair in every evidence artifact and render
its staleness in `/health`.

### Decomposition proved by onboarding

"Zero provider literals in the base" is satisfiable by relocation. The
done-test is the **sixth-provider test**: onboarding a toy provider means
adding one schema entry and one `providers/<name>/` directory, editing zero
existing files, with planner, harness, and derivation checks passing.
Registration by discovery. Publish and cap the `AgentHarnessAdapter` Protocol
width alongside it; it is 33 today.

### Capability and proof freshness are separate axes

Projecting the capability matrix purely from proof freshness means a transient
factory outage silently downgrades working functionality to unsupported — the
same lie in the opposite direction. Generate capability from the contract and
attach proof status separately: `verified`, `stale`, `missing`, `failing`.

## Phases

### Phase 0 — stop the live falsehood, and census

Deploy `codex_tool_call_result_v1` in place of `codex_release_identity_v1` on
clifford. One line in a compose file. The swap is strictly additive: its
assertion set contains both identity assertions it replaces plus
`command_execution_completed_with_exact_output` and
`tool_result_linked_to_final_agent_message`. Using the env-var mechanism this
epic retires is a deliberate, documented choice.

Produce the census as a **generated, CI-checked artifact** with its counting
rule in code. At 189 files a hand-typed list is stale the week it lands.

### Phase 1 — define and validate the planning model

The gate for everything after. Define the relationships among capability
assertion, qualification profile, harness scenario, build provenance, evidence
class, and credential policy. Reconcile the 13 schema scenario ids and the 22
harness scenarios, or state that they are different types and name the mapping.

Emit a serialized plan that reproduces **current execution exactly**, validated
in both repositories. Change nothing about what runs. Constants whose semantics
the model represents become derived; constants it does not represent — version
grammars, binary-name sets, acquisition behavior — stay provider-owned code and
are documented as such.

### Phase 2 — converge, driven by the plan, shadowed first

The structural core, in this order. The first step is in the private
repository and is a hard prerequisite: `ProviderLane` carries one
`qualification_profile` and one `expected_scenario`, `_validated_outcomes()`
accepts exactly one complete bundle for that pair, and baselines are keyed
`(provider, qualification_profile, assertion_id)`. A multi-profile column
cannot be stored or advanced today.

1. Define the versioned run-evidence index and extract pure qualification
   oracles while preserving current executors.
2. Teach factory validation, persistence, baselines, outbox, and delivery to
   consume one serialized plan carrying multiple profile proof bundles;
   shadow-dual-write the existing single-profile representation.
3. Make staged-build manifests authoritative for binary selection, and launch
   the complete harness under `QualificationSandbox`.
4. Invoke the pure oracles over the run-evidence index and emit typed proof
   records.
5. Shadow-compare, then retire duplicate qualification execution and the
   env-var selector.

Staged builds are a partial seam, not a ready one. `HarnessOptions.provider_builds`
records and entrypoint-checks `ProviderBuildRef`, but `provider_bins` still
selects execution, `--use-real-provider-bins` leaves `provider_builds` as
`None`, `verify_provider_builds()` is called by the smoke wrapper rather than
`run_harness()`, and nothing in control-plane constructs a `ProviderBuildRef`.
Phase 2 must make one verified staged-build manifest the source of both
executable selection and recorded provenance.

Assign the smoke wrapper's fate in this phase: it becomes a thin `plan_run`
caller or it deletes. It currently owns `DEFAULT_SCENARIOS`, the
`--use-real-provider-bins` flag, and closure verification, and the convergence
is incomplete while it does.

**The shadow gate.** "One poll cycle" is not a gate — a 900s tick that finds no
release exercises neither path, so it can pass having diffed zero runs.
Revised 2026-07-28: this system has zero users and one operator, and every
deploy already has instant rollback (`deploy-provider-factory.sh --rollback`).
A multi-day wait bought safety margin against a cost this system doesn't have
today — the original framing ("N consecutive agreeing runs... so it completes
in days rather than at release cadence") was sized for a team/user-facing
system, not this one. The gate is instead: replay the last K staged releases
through both paths **synchronously, in one sitting**, and cut over the moment
they agree. If they disagree, fix it and replay again immediately — no
elapsed-time requirement. Diff **typed proof records only** — assertion
identity, outcome, evidence class — never payloads, paths, or timings. The
`canonical_digest_v1` lesson is that value churn makes payload comparison
useless; a payload-diff shadow flake-blocks on day one. Pull exactly that
narrow proof-record comparator forward from Phase 3; the fixture corpus and
full semantic oracle stay there.

**Profile-parity gate**, covering all ten `_PROFILES`: fixed observation
fixtures must produce identical assertion IDs, outcomes, evidence classes, and
blocked-versus-infrastructure distinctions before and after extraction. Known
per-module work, from review:

- The five `*_release_identity` profiles are thin declarations over a shared
  oracle. A generic identity oracle needs pre/post executable hashes, version
  stdout, return code, timeout, expected version, and build identity. **The
  current harness probe does not record pre/post executable hashes.**
- `opencode_server_qualification` is already close to a pure mapping.
- `claude_real_print_qualification` and `antigravity_hook_qualification` need
  two evidence inputs each and an explicit plan outcome for skipped
  credentials. A blocked outcome must never be inferred from a missing file.
- `codex_tool_call_result` **cannot** consume the harness's current Codex
  observation: the harness canary asks for `DONE`, tolerates multiple command
  events, and checks only that a matching one exists, while the oracle requires
  exactly one command with exact output, ordering, and a linked final message.
  Standardize the observation before deleting that executor.
- `codex_helm_interrupt` can consume the managed-interrupt artifact but also
  judges exact turn identity and verified cleanup. Preserve `stop.json` in the
  index and expose cleanup verification explicitly.

**Failure contract**, which does not exist today and must: an executor crash is
attributable as `executor_error` and never as a provider assertion failure,
otherwise a harness bug silently downgrades a working provider. A crash partway
through a column stores completed scenarios' proof and records the rest as
unmet plan items. Add a per-run failure budget — `restart: unless-stopped` plus
a deterministic harness crash is a crash-loop re-running real binaries on every
restart.

**Tick arithmetic**: one narrow scenario fits inside 900s; a full column
against real binaries will not. State the overlapping-tick policy (skip, not
queue) and a per-run timeout, or Phase 4 volume serializes into a permanent
backlog.

**Sandbox policy**: `QualificationSandbox` is built per-lane with per-provider
`writable_roots`. A full column needs one policy spanning all providers, plus a
long-lived `opencode serve` and its port inside bwrap. Acceptance: the full
column runs green under `QualificationSandbox` on clifford, not on a laptop.

Deletes: duplicate execution in the three named modules,
`provider-release-proof.md` (verify inbound links first),
`provider-automation-factory-epic.md`.

### Closing the observation gap — first draft wrong, corrected by review (2026-07-28)

Steps 1-3's bounded slices are done (pure oracles for all 10 profiles, an
additive multi-profile tick, staged builds authoritative inside
`run_harness()`). A first draft of this section claimed the two flagged
profiles (`codex_helm_interrupt`, `codex_tool_call_result`) could consume the
harness's existing observation with only additive changes. Hatch Sol review
found that claim false or overstated on every substantive point, before any
code was written. Kept here, corrected, for the same reason the appendix
keeps revision 1-3's errors: the corrected fact matters more than a clean
draft history.

**`codex_tool_call_result`: the harness runs a different test, not a
differently-parsed version of the same test.** The harness's
`run_real_tool_exec` (`codex_provider_release_canary.py`) has the model run
`printf '{marker}\n'` and reply exactly `DONE`. `codex_tool_call_result_v1`
requires `python -c 'import secrets; print(secrets.token_hex(16))'` and the
final message to equal that command's exact output
(`codex_tool_call_result.py:357-360,467`). These are different scenarios that
happen to share a scenario name. Calling `codex_tool_call_result_command_oracle`
over the harness's `printf`/`DONE` events with the release lane's expected
command would deterministically return `SEMANTIC_FAIL` — command mismatch,
every time, regardless of whether Codex behaves correctly. The raw-JSONL-file
observation was real; the claim that it was *the right observation* for this
oracle was not. Fixing this means either changing what the harness's
`tool_call_result` scenario asks the model to do (which changes what every
other provider's `tool_call_result` scenario is compared against, since the
scenario name is shared and provider-neutral by design), or accepting that
`codex_tool_call_result_v1` tests something genuinely more specific than the
harness's generic `tool_call_result` scenario and the two should not converge
at the scenario-name level. **This is a product decision, not an execution
detail, and is not resolved here.**

**`codex_helm_interrupt`: same underlying function, different execution
contract.** `_run_codex_interrupt_cancel` and `codex_helm_interrupt.py`'s own
executor do both call `run_managed_live_interrupt()` — that part of the first
draft holds. But the release-lane executor first builds an isolated,
fully-replaced environment (`os.environ.clear()` then a `strict_env` with
exactly the allowed variables), prepares inert MCP bootstrap config, and
wraps the call with pre/post engine and provider identity verification
(`codex_helm_interrupt.py:611-649` and surrounding). The harness calls the
same function inside its own ambient process environment with none of that
isolation. "Same function, same call" is true; "therefore the observation is
equally trustworthy" does not follow — the release lane's isolation is part
of what makes its proof meaningful, not incidental plumbing. Reusing the
observation requires the harness driver to adopt the same setup, not just
call the same function.

**The bridge design's "no control-plane changes" claim is also false**, on
two independent grounds:

1. `_run_v2_bridge` (`control-plane/provider_factory/core.py:481-489`)
   hardcodes the launched script path to `scripts/qa/provider-qualification.py`.
   A different entrypoint requires a real dispatch change there, not zero
   changes — unless `provider-qualification.py` itself is taught to dispatch
   to the harness internally, which is a legitimate alternative not explored
   here.
2. Even granting the same `proof-bundle.json` shape, the record `artifact_id`
   is a canonical-JSON sha256 over the full record
   (`_validated_outcomes()`/`worker.py:118-121`), and `_validated_outcomes()`
   additionally requires the exact `provider_version`, pre/post executable
   hash agreement, `provider_build_identity`/`granularity` matching a real
   `ProviderBuildRef`, and (for helm) `longhouse_build_id` matching the
   verified engine binary's hash (`worker.py:64-146`). A bridge script must
   independently reconstruct all of this provenance from what the harness
   produces — it is not free from "same shape." The bundle's own
   `artifact_kind` is `"provider_capability_proof_bundle"`, not
   `"provider_capability_assertion"` as the first draft of this section
   said — that field name belongs to the individual records, not the bundle
   (`worker.py:84-87`; `codex_tool_call_result.py:283-289`).

**Both flagged profiles' parity step is now shipped** (longhouse `ae507c15a`
for `codex_helm_interrupt`, `d6d91b24f` for `codex_tool_call_result`) — per
Sol's stated order (parity first, dispatcher second, dual execution third),
this closes step one for both.

`codex_tool_call_result`'s product-decision blocker was **resolved** by Sol
(asked to decide from first principles, per David's delegation): option (c),
a distinct codex-only harness scenario (`codex_tool_call_result_strict`),
leaving the existing generic `tool_call_result` scenario unchanged for every
provider including codex — the two tests prove different contracts (basic
cross-provider tool execution vs. codex-specific exact-command/exact-output/
exact-linkage), and imposing the stricter one on four unrelated providers'
fake-binary stubs would add real complexity for no coverage gain. Follows the
existing `opencode_lineage_projection` precedent (free function +
not_applicable gating, not a new adapter Protocol method). `codex_tool_call_result.py`
gained `run_codex_real_tool_command()`, the reusable observation-producing
half, alongside its existing pure oracle; the release lane's own `run()` is
untouched. Not in `DEFAULT_HARNESS_SCENARIOS` (opt-in, real-binary +
`CODEX_API_KEY` only, like `live_token_streaming`).

`codex_helm_interrupt` convergence required a second Sol consult mid-build:
the harness's credentials check ran post-hoc (after already calling the
canary), so naively wrapping that call in environment isolation risked
breaking the existing no-credentials fallback test. Fix: `run_isolated_codex_operation`,
extracted from the release lane's `run()`, generalized to accept the actual
canary call as a callable so the release lane and the harness driver share
one isolation implementation instead of two — verified unchanged against the
release lane's 14 existing tests. The harness's `_run_codex_interrupt_cancel`
is now a two-stage preflight: bridge credentials checked before touching
`os.environ` at all (falls back to the existing hermetic dispatch, entirely
outside the isolation path); the full strict-lane input set required second
(missing is `BLOCKED`, not the hermetic fallback, since bridge credentials
already proved present); only then does the isolated interrupt run, feeding
`codex_helm_interrupt_oracle` for a new `strict_oracle` field alongside the
unchanged existing payload.

**Not yet built**: the bridge/dispatcher (wiring control-plane to launch
either strict path instead of the release lane's own executors) and
shadow-compare. `_run_v2_bridge` still hardcodes `scripts/qa/provider-qualification.py`;
nothing in either repository invokes these new harness scenarios from the
release lane yet.

**Finalizer promotion (step 1 of 3) is shipped** (longhouse `130831239`):
both `codex_tool_call_result.py` and `codex_helm_interrupt.py`'s `_emit()`
were already parameterized generically enough (explicit identity/version/
outcomes/execution/observation inputs, no hidden state) to become
`emit_proof_bundle()` — a pure rename, both modules' own `run()` unchanged,
all 35 existing tests pass unchanged. This is what the new bridge script
will call once it has real provenance to hand it.

**The new bridge script (step 2 of 3) is concretely harder than the
request/output CLI contract makes it look, but the hard part is resolved.**
`load_request()` (`provider_release_identity.py:118`) already gives the
bridge everything the *release lane* trusts: `provider_bin`,
`expected_provider_build_identity` (a hash string), and
`expected_provider_build_granularity` — but the release lane's own `run()`
never re-verifies that hash against a live closure; it passes the request's
claim straight through into the record. A harness-backed bridge that did the
same would add nothing. Phase 2 step 3's `run_harness()` work
(`verify_provider_builds()`, now internal to `run_harness()`, called before
and after execution) is exactly the live verification the release lane
lacks, but using it requires a real `ProviderBuildRef`, which needs
`build_root`/`entrypoint_relative` — neither is a request field.

Resolved by reading control-plane's own acquisition code
(`control-plane/provider_factory/core.py:623-643`): both
`codex_tool_call_result_v1` and `codex_helm_interrupt_v1` set
`uses_provider_package = True` unconditionally — there is no `single_asset`
case for either profile, only `full_installed_tree`, staged at
`package_root = artifact_root / "provider-package"` with the entrypoint
always `bin/codex` (`binary = package_root / "bin" / "codex"`). So for these
two profiles specifically: `source_root = provider_bin.parent.parent`,
`entrypoint_relative = "bin/codex"`, always — derive it, then *validate* the
guess by calling `codex_helm_interrupt._package_identity(str(source_root),
provider_bin)`, which already checks the exact `PACKAGE_MEMBERS` set and
raises if the layout doesn't match, rather than trusting the derivation
blind. `provider_build_store.materialize_staged_provider_build()` already
exists and does exactly the rest: copies the closure into a content-addressed
store (idempotent — a second call for the same provider/version/platform
just re-verifies, it doesn't re-copy), computes the real digest, and returns
a `ProviderBuildRef`. Use a run-scoped store under `output_root` for this —
this is a per-run integrity check, not participation in control-plane's
separate, already-working persistent build-store ingestion pipeline
(`ingest_provider_build` / `scripts/qa/provider-build-store.py`); conflating
the two would be scope creep past what step 2 needs.

**One more resolved gap, found while drafting the actual `run()` bodies**: the
two harness scenarios only produce *some* of each profile's `ASSERTIONS`.
`codex_tool_call_result_strict`'s payload carries
`command_execution_completed_with_exact_output` and
`tool_result_linked_to_final_agent_message`, but not
`exact_executable_identity_observed` (the bridge's own preflight already
computes this — pre/post hash comparison, same as the release lane) or
`reported_version_matches_expected` (nothing in the strict scenario probes
`--version` at all). Fix: call `run_harness()` with **two** scenarios —
`("probe_identity", "codex_tool_call_result_strict")` — and read
`probe_identity`'s result `data["version"]` (raw `--version` stdout),
parsed through `identity_bridge._VERSION_LINE` and compared against
`request["expected_provider_version"]`, for the fourth assertion.
`codex_helm_interrupt`'s bridge needs the same combination:
`("probe_identity", "interrupt_cancel")`, reading `interrupt_cancel`'s
already-shipped `engine_identity`/`strict_oracle` fields plus
`probe_identity`'s version for `codex_helm_interrupt.ASSERTIONS`'
`reported_version_matches_expected` (`_required_environment`'s missing-input
BLOCKED path and the bridge credentials preflight both already exist inside
`_run_codex_interrupt_cancel` and will surface as `interrupt_cancel`'s own
`status`/`failure_code`, which the bridge must check before trusting
`strict_oracle` is present at all).

Still to do for step 2, concretely: write the two `run()` functions with this
two-scenario combination, derive+validate+materialize the `ProviderBuildRef`
as above, require exactly one matching `ScenarioResult` per scenario name,
fail closed (not silently skip) if either is missing or `strict_oracle` is
incomplete, recheck post-execution identity, and call `emit_proof_bundle()`
with the assembled four-assertion outcome map. Then tests (unit tests against
a stubbed `run_harness`, not a live Codex run), step 3 (control-plane
dispatch map), and shadow comparison, per the design above. This is genuinely
substantial, correctness-critical implementation — the design is now fully
resolved (nothing left to discover), but writing and testing it carefully is
its own unit of work, deliberately not rushed into the tail of the session
that resolved the design.

### The bridge/dispatcher design (Sol; all three pieces landed 2026-07-28)

Third Sol consult for this section. Three decisions:

1. **A new, narrow public CLI**: `scripts/qa/provider-harness-qualification.py`.
   Same `--request`/`--output-root`/`--json` contract as `provider-qualification.py`.
   Accepts only the two explicit profile-to-scenario mappings (no general
   provider/scenario selection — this is not a second harness CLI). Builds a
   real `ProviderBuildRef` from the staged request, calls `run_harness()` with
   exactly `providers=("codex",)` and one scenario, requires exactly one
   matching `ScenarioResult` (fails closed on missing/multiple/mismatched
   results or an incomplete `strict_oracle` field), preserves the full harness
   observation as raw evidence, rechecks provider identity post-execution
   (plus verified engine identity for helm), and produces the normal proof
   bundle via the finalizer below.
2. **Control-plane dispatch**: a narrow profile-to-script map next to
   `_run_v2_bridge` — `{"codex_tool_call_result_v1", "codex_helm_interrupt_v1"}`
   select the new script; every other profile keeps
   `provider-qualification.py` unchanged. Reject impossible provider/profile
   combinations rather than silently falling back. The manifest records which
   execution boundary was selected; rollback is one mapping edit. Tests must
   assert the exact launched path for both strict profiles and one ordinary
   profile (regression guard against the map silently widening).
3. **Provenance: promote, don't duplicate, don't over-abstract.** Each of the
   two release-lane modules' `_emit()` becomes a small public finalizer (e.g.
   `emit_proof_bundle()`), called by both that module's own `run()` (legacy
   path, still what's deployed today) and the new harness bridge script. This
   reuses the exact `ProviderCapabilityProofRecord` serialization, canonical
   `artifact_id` hash, contract/adapter/oracle digests, and coverage shape
   without building a generic framework across all ten profiles. The bridge
   script stays responsible for obtaining and validating pre/post identity,
   reported version, `ProviderBuildRef`, and (for helm) engine hash before
   calling the finalizer — that acquisition logic is legitimately different
   between staged-release qualification and harness-backed qualification, and
   is not shared. Add equivalence tests: identical outcomes/provenance fed
   through the legacy and harness finalization paths must produce identical
   records, byte-for-byte except explicitly variable evidence/timestamps.

Sequencing followed exactly as specced: finalizer promotion first (pure
refactor of already-tested code, lowest risk, longhouse), then the new CLI
script (new code, testable standalone against constructed fixtures, no
control-plane change, longhouse `882d91beb`), then the control-plane dispatch
map last (the only piece that touches what's actually deployed, control-plane
`9412d9f`). All three are code-complete and tested, and item 3's equivalence
tests (legacy vs. harness output) are written for both bridged profiles
(longhouse `852456782`, `250667f78`) against fake binaries.

**Shadow-compare, real (2026-07-28):** ran both the legacy executor and the
harness bridge for `codex_tool_call_result_v1` against an actually-downloaded
real Codex release (`rust-v0.145.0`, `codex-package-aarch64-unknown-linux-musl.tar.gz`,
sha256 `54f79a05...cb6f54`) with a real `CODEX_API_KEY`, inside a scratch
Linux container (not clifford — zero effect on anything deployed). Both paths
produced byte-identical assertion outcomes:
`exact_executable_identity_observed`/`reported_version_matches_expected`
passed, `command_execution_completed_with_exact_output`/
`tool_result_linked_to_final_agent_message` both `semantic_fail`ed
identically, because the container didn't grant unprivileged user
namespaces and Codex's `bwrap` sandbox couldn't start (`sysctl
kernel.unprivileged_userns_clone`) — a container-permission artifact of the
scratch environment, not a code defect, and both paths correctly classified
it as `semantic_fail` rather than silently mis-reporting it. `compare_scenario_results()`
(Phase 3) confirmed `equivalent: true, mismatches: []`. Not persisted as a
checked-in fixture (needs a live API key to regenerate) or a CI test —
one-shot manual validation, ad hoc driver script in scratch, not committed.

**Still not run:** the same real-binary shadow-compare for
`codex_helm_interrupt_v1` (needs a live managed-session/engine bootstrap,
meaningfully more infrastructure than a binary + API key), and a real
success-path run for `codex_tool_call_result_v1` (this run only proved
equivalence on a real *failure* — real environments with working
`bwrap`/user-namespace support, like clifford's, should be checked too before
fully trusting the live-success path). **Not yet deployed to clifford** —
that decision is still open regardless of this result.

### Phase 3 — equivalence oracle, then split the adapter

**Comparison half shipped 2026-07-28 (longhouse `ef16bccaa`):**
`server/zerg/qa/scenario_equivalence.py`'s `compare_scenario_results()`
judges two `ScenarioResult`-shaped captures for equivalence — explicit
outcomes, commands/arguments, exit status, assertion identities, capability
booleans, and presence/shape (not value equality, since two runs legitimately
differ) of non-JSON-artifact checksums. Value-based, not schema-based: a
release-lane proof-bundle-derived payload and a native harness payload are
both judged by the same function. Fixture corpus seeded with real captures
(not synthetic) from the two profiles Phase 2 proved equivalent —
`server/tests_lite/fixtures/scenario_equivalence/`. Running it against real
data immediately caught a new, previously-unchecked discrepancy:
`run_codex_helm_interrupt()` reported `execution_status="completed"` for a
credentials-missing run where every outcome was BLOCKED and nothing was
actually attempted, diverging from the legacy path's own `"blocked"`
convention — fixed to match.

**Not started:** the fixture corpus only covers the two already-bridged
codex profiles; it doesn't yet run across the other eight `_PROFILES` or any
generic (non-codex) harness scenario. Transcript projections specifically
(the harness's `session_projection`/`timeline_projection` output) aren't
compared yet — the two seeded fixtures don't produce them.

**Adapter split design (Hatch Sol, 2026-07-28):** `AgentHarnessAdapter` is a
33-method `Protocol` (`universal_agent_harness.py:727`). There is one real
implementation, `UniversalProviderAdapter` (`854-5376` before this landed);
the five per-provider classes were cosmetic empty subclasses selected by
`ADAPTER_CLASS_BY_PROVIDER`, inheriting everything unchanged — one god class
with 34 provider conditionals and 27 `_run_<provider>_*` methods, not five
adapters. Target: keep `AgentHarnessAdapter` unchanged, keep
`UniversalProviderAdapter` as the shared base for genuinely provider-neutral
logic (evidence packaging, probing, canonical ingest/projection, baseline
comparison, generic cleanup), give each provider a real subclass overriding
its methods directly with no provider-name branches left in the shared
class. Sequence by risk, smallest first: Antigravity → Cursor → OpenCode →
Claude → Codex (Codex last — much larger, launch-critical). Each provider's
extraction must leave the ~3700-test suite green before the next one starts;
`compare_scenario_results()` guards against silent evidence-shape drift
during each move.

**Antigravity slice shipped 2026-07-28 (longhouse `870493d47`):**
`AntigravityHarnessAdapter` now has real overrides for its five methods
(`launch_managed_session`, `managed_session_e2e`, `external_event_channel`,
`permission_prompt`, `live_token_streaming`); every antigravity branch is
gone from `UniversalProviderAdapter`. Kept in-file rather than moved to a
separate `provider_adapters/` package for this first slice — proving the
extraction pattern without adding package/discovery-loader risk in the same
change, per Sol's own sequencing. Full suite: 3673 passed, 16 skipped, zero
regressions.

**Cursor: no extraction needed.** It has essentially zero provider-specific
dispatch inside `universal_agent_harness.py` — its real control-path
implementation (PTY injection) lives in a separate module entirely, per an
earlier Sol investigation this same session. `CursorHarnessAdapter` staying
an empty pass-through subclass is already correct for this file's scope.

**OpenCode slice shipped 2026-07-28 (longhouse `7d43303f1`):**
`OpenCodeHarnessAdapter` now has real overrides for its six methods
(`permission_prompt`, `managed_session_e2e`, `interrupt_cancel`,
`resume_reattach`, `tool_call_result`, `live_token_streaming`); every
opencode branch is gone from `UniversalProviderAdapter`, including
flattening `managed_session_e2e`'s inverted "if not opencode: ...; else:
opencode" structure into sequential checks. Used AST-derived line boundaries
rather than manual copy-paste for the larger, more complex bodies (one
defines a nested HTTP server class). Full suite: 3673 passed, 16 skipped,
zero regressions, both before and after.

**Claude slice shipped 2026-07-28 (longhouse `42dc90949`):**
`ClaudeCodeHarnessAdapter` now has real overrides for its eight methods
(`launch_managed_session`, `managed_session_e2e`, `external_event_channel`,
`permission_prompt`, `interrupt_cancel`, `steer_active_turn`,
`resume_reattach`, `live_token_streaming`); `_run_claude_provider_live_projection`
stays a private shared helper (three of the overrides call it). Two real
mistakes surfaced by running the full suite, not caught by the mechanical
diff alone: `external_event_channel` was missing from the original
extraction plan entirely (its only body was the claude branch — removing
the branch without adding the override silently made claude fall through to
"unsupported"), and `steer_active_turn`'s body internally called
`self._run_claude_interrupt_cancel(...)`, a second provider-specific method
calling a first, not a shared base-class helper — renaming `interrupt_cancel`
left a dangling `AttributeError` the test suite caught immediately. Full
suite: 3673 passed, 16 skipped, zero regressions — after fixing both.

**Codex slice shipped 2026-07-28 (longhouse `6662b9bca`), completing all
five providers.** `CodexOpenAIHarnessAdapter` now has real overrides for its
seven methods (`permission_prompt`, `steer_active_turn`, `interrupt_cancel`,
`tool_call_result`, `live_token_streaming`, `managed_session_e2e`,
`resume_reattach`); three shared internal helpers stay private, unrenamed
(`_run_codex_interrupt_dispatch_proof`, `_run_codex_managed_session_canary_projection`,
`_run_codex_resume_attach_command_proof`). Mapped the full internal call
graph before moving anything, specifically checking for the exact
cross-reference bug the Claude slice caught — none found, full suite passed
on the first run. **All five providers now resolved**: Antigravity,
OpenCode, Claude, Codex have real per-provider overrides with zero
cross-provider branching left in `UniversalProviderAdapter`; Cursor needs no
extraction (its control path lives outside this file). 3673 tests passed,
16 skipped, zero regressions, at every one of the four extraction commits.

**Not yet done, and the harder-to-assess remainder:** all five provider
classes still live in the same one file as the shared base
(`universal_agent_harness.py`, ~9500 lines) — the `provider_adapters/`
package structure Sol's design calls for (one file per provider under
`server/zerg/qa/provider_adapters/`), the discovery-loader replacing
`ADAPTER_CLASS_BY_PROVIDER`'s hardcoded map, and the sixth-provider test
(add a temporary importable toy adapter, assert discovery constructs it and
existing provider modules need zero edits) are all still open. This matters
because the behavioral decoupling just shipped (no provider branches in
shared code) does not by itself deliver Sol's actual acceptance bar: adding
a real sixth provider today would still require editing this shared file
(a new class + a new map entry), not just adding new files. Splitting each
class across a file boundary means resolving every name each class's moved
code references (imports, module-level helper functions, `EvidencePackage`/
`HarnessOptions` types) per provider — a distinct, nontrivial task from the
extraction just done, not a mechanical follow-on.

Deletes: `executable-provider-capability-contract-epic.md`,
`provider-release-proof-roadmap.md`.

### Phase 4 — signal plan, then coverage

**Correction (2026-07-28): the signal plan already exists and is live.**
This section originally described the plan as unbuilt. It isn't — it was
built separately (control-plane, dated 2026-07-24/25, predating this epic's
Phase 0) and never connected to this document, which is exactly the kind of
map/territory gap the epic exists to close.

- **Alert grouping / Sentry-style fingerprinting**: `provider_factory/policy.py`.
  `_failure_shape()` digests the sorted set of failed assertions into a
  `sha256` fingerprint; `identical_infrastructure_failure` suppresses repeat
  noise only when two runs share the exact same infrastructure-failure
  fingerprint (never suppresses a semantic failure).
- **Baseline comparison / green→red paging**: `assertion_deltas()` classifies
  each assertion as `new`/`unchanged`/`regressed`/`recovered`/`changed`
  against a recorded `assertion_baseline`; `notification_tier()` pages
  (`"alert"`) only on a semantic failure or adverse movement
  (`regressed`/`changed`/a new non-pass), routes first-seen gaps to a quiet
  `"digest"`, and stays `"silent"` on an unchanged known gap — exactly
  "page on a novel failure fingerprint or a green→red transition... everything
  else is dashboard state."
- **Disposition / triage routing**: `qualification_decision()` in the same
  file assigns `quarantined`/`triage_filed`/`baseline_seeded`/
  `rerun_scheduled`/`none_required`, live-called from `worker.py:509`.
  `triage.py`/`triage_agent.py` and `delivery.py` consume these dispositions
  for actual notification delivery (`PROVIDER_FACTORY_NOTIFICATION_TO`,
  confirmed configured on clifford).
- **Flake vs. genuinely stuck**: `RELEASE_STUCK_AFTER_SECONDS = 6 * 60 * 60`
  (`policy.py`) — a release failing qualification for under 6h reads as
  normal publication lag (asset upload racing the tag), not a broken
  factory; past that, `unresolved_release_report()` marks it `"stuck"`.
- **Evidence retention**: `provider_factory/retention.py`,
  `FULL_COMPLETED_RUNS_PER_PROVIDER = 10`, `SLIM_EVIDENCE_DAYS = 30`,
  `MIN_SLIM_AGE_HOURS = 48`; mode (`off`/`report`/`apply`) is
  `PROVIDER_FACTORY_RETENTION_MODE`, confirmed `apply` on clifford.
- **Not present under this name**: an explicit "expected runs per day"
  artifact. The interval (900s) and per-trigger cadence (Phase 1's
  `plan_run`) give this implicitly; nothing currently asserts "codex should
  have produced N runs today and didn't."

So Phase 4's actual remaining work is only the coverage volume, not the
signal plan: turn coverage up — real binaries, full columns, every release.
Cursor enters as `observed_install` snapshots. The diagonal fills. The model
is wpt.fyi — many implementations, one shared suite, and the public matrix
is a projection of the freshest result per cell so nobody reads individual
runs.

**Correction (2026-07-29): "on cube" conflicts with standing policy.** This
section originally named cube as the host for the expanded volume. Verified
against `~/.claude/CLAUDE.md`: "cube (`/cube-llm`): local Tailscale inference
only — not batch/automation." Running the factory's real-binary batch
qualification workload there would violate that. Where the expanded volume
actually runs (clifford at higher capacity, a new dedicated host, or
something else) is an infrastructure decision, not a technical unknown —
flagging it rather than picking a host unilaterally.

**Investigated 2026-07-29: the narrower "just turn on codex's second
profile" path is real but not a quick flip.** Before assuming coverage
volume needs new infrastructure at all, checked whether it could grow inside
clifford's existing single container — `run_multi_profile_tick`
(`provider_factory/worker.py:672`) already lets one provider carry more than
one qualification profile per tick, and both codex harness-bridge profiles
(`codex_tool_call_result_v1`, `codex_helm_interrupt_v1`) are already
equivalence-tested against the legacy path (Phase 2). It is not wired to any
deployed entrypoint, by its own docstring, deliberately — `worker_cli.py`'s
`main()`, the actual container process, still calls `run_tick` with exactly
one profile. Wiring it in is not a config flip: `run_tick`'s and
`run_multi_profile_tick`'s per-provider result shapes are genuinely
incompatible (`_run_lane_tick_locked` returns a flat list of per-release
qualification results per provider; `run_multi_profile_tick`'s equivalent is
a dict nested by profile, `worker.py:763-766`), and `worker_cli.py`'s
downstream code (`provider_build_staleness`, `drain_factory_delivery`,
retention, triage) all consume that shape. Forcing a shape-compatibility
patch into the actual unattended production tick loop — real credentials,
runs every 15 minutes, no one watching — without designing it carefully is
exactly the kind of rushed change worth refusing even under time pressure.
**Wiring shipped 2026-07-29 (control-plane `a8723fc`).** The shapes turned
out closer than the investigation above assumed: both are the same
`{"provider", "profile", "releases"}` dict; `run_multi_profile_tick` just
reaches it one level deeper, keyed by profile, because a provider can carry
more than one. `_flatten_multi_profile_result` unwraps that level — proven
a true no-op for the single-profile case (every provider running today)
by a test that runs both `run_tick` and `run_multi_profile_tick` +
flatten on identical inputs and asserts byte-identical output, and proven
correct for the genuinely-multi-profile case by a second test asserting
merged releases each keep their own `qualification_profile` tag.
`worker_cli.py` now always calls `run_multi_profile_tick` + flatten instead
of `run_tick`, and `--qualification-profile` accepts more than one value.
516 passed, zero regressions.

**Still not live, deliberately — but now only by one decision, not two.**
`service.py`'s side shipped 2026-07-29 (control-plane `ddfcbc8`):
`PROVIDER_FACTORY_QUALIFICATION_PROFILE` now parses as a comma-separated
list (a bare single value is unchanged — every clifford deploy today keeps
running exactly as before), and `run_worker_tick` emits one
`--qualification-profile` flag per entry. Proven with a test that sets the
env var to both codex profiles and asserts both flags land in the actual
subprocess command, plus a duplicate-rejection test; full control-plane
suite 518 passed. This still changes nothing about clifford's live behavior
by itself — the env var's current deployed value is still the single
default. What remains is entirely the second decision, deliberately not
taken here: deploy a changed `PROVIDER_FACTORY_QUALIFICATION_PROFILE` to
clifford, a live production change to an unattended, every-15-minute,
real-credentials process, not yet shadow-verified against a real second
profile running concurrently.

**What "coverage volume" still does not mean, even after this:** the
Definition of Done's actual bar is "the full column runs green under
`QualificationSandbox` on clifford" — every scenario for at least one
provider, not two profiles for one. Codex has two of roughly ten scenarios
harness-bridge-capable today (`codex_tool_call_result_v1`,
`codex_helm_interrupt_v1`). Filling the rest of even one provider's column
means repeating Phase 2's bridge-design-plus-equivalence-test work per
remaining scenario — real, multi-session-scale engineering, not a
config change. This wiring makes that additive once each scenario is
ready; it does not shortcut producing them.

Deletes: `provider-build-matrix.md`, `provider-automation-factory-completion.md`.

### Phase 5 — serving

Capability projection from the contract, proof status attached separately, both
rendered. This document's status tables become generated.

**Data layer shipped and verified live 2026-07-29 (longhouse `ab6e98c2a` →
`ff9cc705a`).** `server/zerg/qa/capability_projection.py`'s
`project_capabilities()` joins the schema's declared capabilities
(`provider_factory_model.load_capability_assertions()`) with real proof
records, served at `GET /api/agents/provider-capabilities`.

**What actually verifying this against the real hosted Runtime Host caught,
that 3685 passing local tests did not:**

1. **A deploy-pipeline gap that had been silently active since the first
   Phase 3 commit.** `deploy-and-verify.yml`'s gate polls
   `contract-first-ci.yml`'s E2E job for the *exact* pushed SHA; its
   concurrency group cancels an in-progress run whenever a newer commit
   lands on `main`. This session's habit of committing code, then
   immediately committing and pushing a spec-doc update right after, meant
   every code commit's own CI run got cancelled by the doc commit's push
   before it could finish — six consecutive `Deploy and Verify` failures
   (`ef16bccaa5` through `ab6e98c2a`), completely invisible unless someone
   actually checked `gh run list` rather than trusting `git push` succeeding.
   David010 stayed pinned to a build from before any of Phase 3 the whole
   time. Fixed operationally, not by changing the pipeline: manually
   dispatched `deploy-and-verify.yml` (`workflow_dispatch`) with nothing
   further pushed to interrupt it.
2. **A real production 500 on the endpoint's first live call.**
   `provider_factory_model.py`'s schema-path resolution
   (`Path(__file__).resolve().parents[3]`) correctly lands on the repo root
   in a local/CI checkout, but the deployed Runtime Host image is not a
   full repo checkout — `docker/runtime.dockerfile` copies only `server/`'s
   *contents* into `/app`, one directory level shallower — so the identical
   arithmetic lands on `/` by accident, and `schemas/` was never part of the
   image at all. This code was written for `load_facts()`, a build-time/CI
   planning tool from Phase 1; nothing had ever called it from a live
   request path before this endpoint did. Fixed in two places that both had
   to be right: the Dockerfile `COPY`, and `.dockerignore` (an explicit
   allowlist that never included `schemas/` — the first fix attempt still
   failed, image build itself refusing with "not found," which is what
   caught the second gap before a broken image could ship).

**"This document's status tables become generated" — first slice shipped
2026-07-29.** `server/scripts/render_provider_factory_status.py` renders the
"What it proves today" status matrix and the diagonal-empty claim straight
from `plan_run()`; both are embedded above and pinned by
`test_render_provider_factory_status.py`, which fails the moment either
becomes stale rather than waiting for someone to notice. This does not yet
cover every table in the document (the release-lane/commit-lane architecture
comparison at the top of "What it proves today" is a structural description,
not schema-derivable data, and stays hand-written) — but the specific claim
this epic was written to stop being trustworthy on faith
("nothing derives from the authority") now literally derives from it.

**Not yet done:** the UI actually rendering this data for a human outside a
markdown file. Real, separate, frontend work this session did not reach.

## Definition of done

### Mechanical

- Generated consumers regenerate byte-identically in CI.
- The derivation check passes at every universal fan-out boundary, and every
  intentional provider subset names its policy in code.
- Every evidence artifact carries its serialized plan, the contract hash, and
  the deployed SHA pair.
- The generated plan matrix regenerates without diff; never-run cells render as
  such.
- The profile-parity gate passes for all ten `_PROFILES`.
- The sixth-provider test passes: one schema entry, one directory, zero edits
  to existing files.
- A mutated staged closure fails both after planning and after execution.
- The full column runs green under `QualificationSandbox` on clifford.
- One provider factory document, with a link check failing on references to
  deleted specs.
- Capability and proof freshness render as separate values everywhere.

### Human

The cold-agent test. A fresh agent, no context, clean worktree, asked two
pinned questions: *what does the factory currently prove about Codex, and what
would catch an upstream Codex release that broke managed interrupt?* Correct
answer in five tool calls or fewer. Grader prompt and scoring live in the
repository with fixed wording. Run at the end of every phase; a phase that does
not improve the result has not delivered its value.

Revision 1 of this document failed its own test.

## Non-goals

- **No evidence caching or skip logic.** Content-hashing the canonical stream
  cannot work and it is the only failure mode that deletes the detection signal
  instead of producing auditable evidence. **Carve-out:** automatic retry of a
  flaky run is not caching, and at Phase 4 volume this distinction must be
  written down or someone will over-apply the non-goal or silently reinvent it.
- **No backward diffing for Cursor.** Permanently `observed_only`, forward-only.
- **No credential broker and no self-hosted runner fleet for live-token
  proofs.** Live-token runs stay manual and deliberate.
- **No Antigravity control-surface investment.** Maintenance tier.

## Risks

**The stacks may not want to converge.** The execution/oracle split is the core
bet and Phase 1 is where it holds or is disproven. If the planning model cannot
reconcile them, the honest outcome is two orchestrators sharing one oracle
layer and one contract, and Phase 1 should be allowed to reach that conclusion.

**Phase 2 converges onto a god class that Phase 3 splits.** This concentrates
risk in the widest-blast-radius component and briefly makes the codebase worse.
Accepted, because splitting first means fixing an interface before the planner
and oracle seam have constrained it. Phase 2's harness changes are additive
seams that do not modify the class internals. Phase 3 must not be deferred once
Phase 2 lands.

**Convergence removes accidental isolation.** Two stacks is waste, but it is
also redundancy: today a harness regression cannot take out the release lane.
Afterward, one code path serves both the 900s tick and CI, and a harness
regression takes out both simultaneously. Shadow mode covers the transition;
only the Phase 3 equivalence corpus covers the steady state.

**Live production during Phase 2.** clifford runs public code from a mounted
read-only checkout under `restart: unless-stopped`. Shadow mode plus the
existing paired-deploy rollback is the mitigation.

**Shared checkout.** Four or five agents work this repository concurrently and
`main` moved 18 commits under one session during the previous epic. Phases run
in separate worktrees.

**Sequencing against launch.** Recorded launch blockers are the fragile steer
demo and red non-blocking CI, and this factory serves a product with no users.
Phase 0 is worth doing today. Phase 1 is cheap and gates knowing whether the
rest is coherent. Phases 3 and 4 should be scheduled explicitly after the
launch blockers.

## Still open

- **Resolved by Phase 1, partially:** the 13 schema scenario_ids and 36
  harness scenarios do not reconcile into one type — there turn out to be
  three types, not two (capability-proof, identity-proof, harness-pipeline;
  see "Phase 1 model" above), with zero name-level overlap and a named
  conceptual mapping for the subset that overlaps in capability. Phase 3 still
  owns making any of these structurally equivalent.
- **Evidence gathered, not resolved:** is the adapter seam correctly placed on
  **provider**? The harness has 27 real per-provider driver functions
  (`_run_<provider>_<scenario>`) across ~11 distinct scenario concepts and 4
  providers (0 for Cursor) — codex alone has 9, including sub-variants no
  other provider has (`_run_codex_interrupt_dispatch_proof`,
  `_run_codex_resume_attach_command_proof`). Variance is not cleanly
  one-dimensional on either axis, which is itself evidence against a simple
  "move the seam to scenario" fix. Phase 3 decides with the equivalence
  oracle in hand, as originally planned.

## Appendix: corrections from review

Kept because they are on-thesis — this document's claim is that claims should
be checkable, and these are what checking produced. Each is the corrected fact,
not the original error.

1. Codex release artifacts have two independent authorities, not five copies;
   `release_contract.py` derives its Codex entry from `core.py`, and no
   `_codex_spec` exists.
2. The provider census is 189 files under a stated rule, not 37 under an
   unstated one.
3. `providers.ts` no longer misstates Cursor's capabilities; that was fixed in
   `4402f99ea`.
4. `UniversalProviderAdapter` is 4,446 lines. 48 of the file's 81 provider
   literals are inside it, not all 81.
5. `ADAPTER_CLASS_BY_PROVIDER` is consulted at 951 **and** 7919; it
   instantiates a class per provider on every run.
6. Structural fingerprints cannot serve as a refactor equivalence oracle, and
   are not emitted on the release path at all.
7. The schema's 13 `scenario_id` values and the harness's 22 scenario names
   have an empty intersection.
8. The harness declares 36 scenarios; 22 is the smoke wrapper's selection.
9. `provider_live_canary.py` is shared infrastructure the harness itself calls
   at five sites, not release-lane private execution. It is not deleted.
10. The harness has five `subprocess` sites, not seven — and the count is
    irrelevant, because sandbox integration happens at the outer process
    boundary.
11. `HarnessOptions.provider_builds` is a partial seam: recorded and
    entrypoint-checked for generated fakes, `None` on the real-binary path,
    with closure verification outside `run_harness()`.
12. **Hatch Sol code review, 2026-07-29** (a review "along the way" of the
    Phase 2/3 work above, distinct from the design-consult reviews already
    cited): checked the four adapter-split extraction commits
    (`870493d47`/`7d43303f1`/`42dc90949`/`6662b9bca`) and the two
    release-discovery fix commits (`7784154`/`09275c7`) against current code,
    not just the commit messages — reading every extracted subclass in full,
    AST-diffing old vs. new class bodies, running the test suite itself, and
    checking the release-tag regex against every page of `openai/codex`'s
    actual live release history via the GitHub API. Cleared the adapter split
    entirely: every moved method's body was preserved, every remaining
    provider-private call resolves inside its own subclass, no orphaned
    dispatch branch was left in the shared base. Found one real defect: the
    release-discovery filter reused `core.py`'s `_release_version()`, which
    accepts bare `X.Y.Z`/`vX.Y.Z` in addition to `rust-vX.Y.Z` — correct for
    that function's own job (parsing a tag already trusted to be real), wrong
    for filtering candidates, since a same-repo vendored release tagged with
    bare semver would still slip through and reproduce the exact
    permanent-retry bug the filter existed to fix. Fixed in `91f8758`
    (control-plane) with a discovery-specific `rust-v...`-only check and a
    regression test for both bare-semver shapes; deployed to clifford.
