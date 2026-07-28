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
changed). Phases 2-5 not started.

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
through the universal scenario set.

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
release exercises neither path, so it can pass having diffed zero runs. The
gate is: each provider reaches N consecutive agreeing qualifying runs, seeded
by replaying the last K staged releases so it completes in days rather than at
release cadence. Diff **typed proof records only** — assertion identity,
outcome, evidence class — never payloads, paths, or timings. The
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

### Phase 3 — equivalence oracle, then split the adapter

Build the fixture corpus and semantic comparison: explicit outcomes, commands
and arguments, transcript projections, capability booleans, exit status,
assertion identities, checksums for non-JSON artifacts. Structural fingerprints
stay schema-drift diagnostics.

Then split the adapter behind `AgentHarnessAdapter`, guarded by that oracle and
the sixth-provider test.

Deletes: `executable-provider-capability-contract-epic.md`,
`provider-release-proof-roadmap.md`.

### Phase 4 — signal plan, then coverage

The signal plan lands before the volume: expected runs per day, evidence
retention, flake retry policy, alert grouping. The model is wpt.fyi — many
implementations, one shared suite, and the public matrix is a projection of the
freshest result per cell so nobody reads individual runs. Pair it with
Sentry-style fingerprint grouping and Chromium sheriff practice: page on a
novel failure fingerprint or a green→red transition on a previously stable
provider × scenario cell; everything else is dashboard state.

Then turn coverage up — real binaries, full columns, every release, on cube.
Cursor enters as `observed_install` snapshots. The diagonal fills.

Deletes: `provider-build-matrix.md`, `provider-automation-factory-completion.md`.

### Phase 5 — serving

Capability projection from the contract, proof status attached separately, both
rendered. This document's status tables become generated.

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
