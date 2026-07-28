# Provider factory coherence

**Status:** proposed, revision 3. No code has moved.

Revision 2 follows review by three independent agents (Hatch Fable, Hatch Codex
Sol, Hatch OpenRouter Kimi K3). Sol rejected revision 1 and reframed it; several
of revision 1's headline facts were wrong. Those corrections are marked
**[r1 error]** below and are kept visible rather than quietly fixed, because a
document arguing for checkable claims has to be checkable itself.

## What this system is

Longhouse integrates with CLI coding agents it does not own — Claude Code,
Codex, OpenCode, Antigravity, Cursor. Those vendors ship releases on their own
schedule. The provider factory exists to answer two questions without a human
finding out the hard way: does a Longhouse change break a provider integration,
and does a provider release break Longhouse.

Vocabulary, because the eleven documents this replaces each used these
differently:

- **provider** — an upstream CLI agent Longhouse drives.
- **capability** — something Longhouse claims a provider can do (`interrupt`,
  `resume`, `answer_pause`). Declared in `schemas/managed_providers.yml`.
- **assertion** — a named postcondition that, if it holds, is evidence for a
  capability. Example: `interrupt_terminal_cancelled_or_interrupted`.
- **scenario** — an executable procedure that produces assertions. Two disjoint
  families exist today; see below.
- **qualification profile** — the release lane's unit of work: one provider,
  one scenario, a fixed assertion set. Example: `codex_release_identity_v1`.
- **evidence class** — how trustworthy a run is. Schema vocabulary is exactly
  `hermetic`, `live_no_token`, `live_token`.
- **build provenance** — where the executed binary came from: generated fake,
  staged upstream release, or observed local install.
- **column** — one provider run across the full scenario set.
- **the diagonal** — real upstream binary crossed with the full scenario set.
  Empty today. See below for why that is structural, not a setting.

## What it proves today

Verified live on 2026-07-28 against factory container `ab9e0a2` (healthy,
`tick_count` 15) and `main` at `072f14dde2`.

**There are two separate execution stacks. They share no code.** This is the
central fact and revision 1 missed it entirely.

| | Release lane | Commit lane |
|---|---|---|
| Entry | `server/zerg/qa/provider_qualification.py` → `_PROFILES` | `server/zerg/qa/universal_agent_harness.py` |
| Implementation | 10 dedicated per-profile modules | one 4,446-line adapter class |
| Binary | real, downloaded from upstream | generated fake |
| Scenarios | 1 per provider | 4 per push, 22 weekly |
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
through the universal scenario set. Revision 1 called this a parameter change.
It is a merge of two independently built stacks. **[r1 error]**

## Why that is not enough

The factory works. It cannot explain itself, and parts of it are not true.

### Nothing derives from the authority

`schemas/managed_providers.yml` is declared the single authority and its shape
is good — per-capability `required_assertions` carrying `scenario_id`,
`oracle_source`, `acceptable_evidence`, `max_age_seconds`.

Revision 1 claimed one field, Codex platform artifacts, existed in five
hand-written places. That was wrong. **[r1 error]** `release_contract.py:20-34`
*derives* the Codex entry from `core.py`'s `ASSET_MAP`/`PACKAGE_ASSET_MAP` by
comprehension, and there is no `_codex_spec` in `registry.py` at all. The
accurate statement is worse in one way and better in another, and it differs per
provider:

| Provider | Independent hand-written statements |
|---|---|
| codex | 2 — `schemas/managed_providers.yml`, `control-plane/.../core.py` |
| claude, opencode, antigravity | 3 — schema, `release_contract.py`, `registry.py` `_*_spec` |

Two independent authorities is still one too many, and the drift guard only
compares a value derived from the private authority against the generated form
of the public one. It cannot see the third statement for three of five
providers.

`web/src/lib/providers.ts` declares itself "single source of truth for provider
... capability claims" and eight lines later says it "mirrors
`managed_provider_contracts.json`". Revision 1 said this file currently tells
users Cursor cannot launch, interrupt, or resume. That was fixed in `4402f99ea`
and the current file reads `launchAndSend: true, interrupt: true, resume: true`.
**[r1 error]** The falsehood is historical; the hand-mirroring mechanism that
produced it is not.

### The provider census, with its rule stated

Revision 1 said 37 files hold hardcoded provider literals, from a same-line
grep it never defined. Reviewers reproducing it got 55, 63, 180, and 233.
**[r1 error]**

Stated rule: files tracked by git with extension `.py`, `.ts`, `.tsx`, `.rs`,
excluding `/generated/` and `node_modules`, containing quoted string literals
for two or more distinct provider names. That yields **189 files** — 93 in
`server/tests_lite/`, 33 in `server/zerg/`, 21 in `engine/src/`, 12 in
`scripts/tests/`, 11 in `web/src/`, 8 in `scripts/qa/`, 3 in `scripts/ops/`,
2 each in `e2e/tests/` and `engine/tests/`.

The test concentration is the dangerous part: a `parametrize` list naming four
providers is how a green suite asserts something false about the fifth.

But not every one is duplication. Sol identified intentional policy subsets —
the Claude/Codex-only experiment in `labs/startup-continuity/install.py`,
provider-specific historical storage handling in
`engine/src/state/source_inventory.rs`, backfill policy in
`scripts/ops/hatch_backfill_llm.py`. A blanket literal ban would replace
semantic review with a noisy allowlist. The enforcement model in this revision
changed accordingly.

### The adapter hierarchy carries no behavior

`UniversalProviderAdapter` spans lines 851–5296 of a 9,317-line file: 4,446
lines, not 4,448. **[r1 error]** The five provider subclasses at 5299–5316
contain a docstring and nothing else. `ADAPTER_CLASS_BY_PROVIDER` at 5319 maps
five names to those five identical classes, consulted at line 951 only so the
conformance scenario can report which class it should have been.

The file holds 81 provider-name literals (opencode 35, codex 17, claude 16,
antigravity 11, cursor 2). Revision 1 implied all of them live inside the base
class. Only 48 do; the other 33 are in module-level dispatch and provider
projection functions below it. **[r1 error]**

`AgentHarnessAdapter`, the Protocol at line 724, declares **33 methods**. The
base implements 80. A 33-method Protocol is already at the width where
per-provider decomposition may produce five 33-method modules that are worse
than one class. Whether the seam belongs on provider at all, rather than on
scenario, is an open question this epic must answer before it splits anything.

### The planning vocabulary does not exist yet

This is the deepest finding and it invalidates revision 1's Phase 1.
**[r1 error]**

The schema declares 13 distinct `scenario_id` values. `DEFAULT_SCENARIOS` in
the universal smoke declares 22. **The intersection is empty.**

```
schema:  claude_real_print, opencode_server_contract, antigravity_hook_inbox,
         codex_coordination_awareness_create, cursor_steer_rejection, ...
harness: adapter_conformance, action_matrix, session_projection,
         timeline_projection, launch_managed_session, ...
```

They are different concepts wearing one word. Schema scenarios are semantic
proof procedures bound to capability assertions. Harness scenarios are pipeline
stages. Revision 1's `plan_run` proposed to "derive the scenario list from the
schema"; there is nothing to derive from because no mapping exists.

The same mismatch runs through the rest of the proposal. `acceptable_evidence`
is exactly `{hermetic, live_no_token, live_token}` — 29 uses across the schema
— which does not align with build provenance `{generated_fake, staged_release,
observed_install}`. And `ProviderLane` supports one profile and one expected
scenario, with `worker._validated_outcomes()` enforcing that singular shape, so
a multi-item plan is an interface migration rather than a parameter.

`FAKE_VERSION_BY_PROVIDER`, the `_*_spec` binary-name sets, and the version
regexes encode facts the schema does not represent at all.

### The proposed safety oracle does not exist on the path it was meant to protect

Revision 1 said structural fingerprints from a previous epic could guard the
adapter split. Three problems, found independently by Sol, Kimi, and me.
**[r1 error]**

`structural_fingerprint_v1` (`provider_evidence_measurement.py:68-92`) collapses
every string to `"string"` unless its key is one of 13 discriminators, every int
to `"int"`, every bool to `"bool"`. It cannot see `complete: true → false`,
`returncode: 0 → 1`, `can_resume: true → false`, or a wrong executable path,
version, or argv. It is blind to precisely the reversed-capability-boolean
failure this epic cites as its motivating example. It also reads only `.json`
and `.jsonl`, ignoring hooks, SQLite, logs, and binaries.

`canonical_digest_v1` is not the alternative:
`test_live_value_churn_moves_canonical_digest_but_not_structure` documents that
it moves on ordinary run-to-run ID and path churn.

Decisively: these measurements are emitted by `EvidencePackage.finalize_measurement()`
in the universal harness. The release lane never calls it. There are no
fingerprints on release evidence at all, and no pre/post comparator exists in
either repository.

### The documentation is a committee

Eleven provider specs, ~250KB, five presenting as the map. A sixth,
`provider-release-proof.md`, is stale with ~60 references to the retired Sauron
arrangement.

Total implementation surface: ~19,100 lines in `server/zerg/qa/`, ~19,800 in
`scripts/qa/`, ~6,400 in `control-plane/provider_factory/`.

## What this replaces

On completion `docs/specs/` holds one provider factory document. The five
competing maps are deleted; git holds the history. A phase that lands without
deleting its share has not landed.

## Position on cheap compute

The premise motivating this epic is that human attention is scarce while
compute and tokens are not, so the factory should run far more than it does.

That is right about what to buy and incomplete about what it costs. Cheap
compute is how you buy real binaries instead of fakes and full columns instead
of one scenario. It is not how you buy trust in an oracle, and this system's
recorded failure mode is oracles that lie: 3,633 tests passing against a binary
that happened to be on the laptop, a test asserting a command that does not
exist, and `test_cursor_storage_v2_honesty` asserting the exact defect it was
named after.

Kimi's sharper version: the document identifies attention as the scarce
resource and then proposes to spend two orders of magnitude more of it, because
nobody designed what the failure signal looks like when one person reads the
output of real, flaky binaries on every release. Volume without a signal plan
recreates the unreadability this epic exists to cure, one level up, in the
alert stream. The signal plan is now a gate on the coverage phase rather than
an afterthought.

## Target architecture

### The convergence: fold each stack in half

Revision 2 said the two stacks merge without saying which half of each
survives. This is that answer, and it changes the framing: they are not two
implementations of one thing. Each is a half-implementation of two different
things, which is why "merge" had no obvious direction.

| | Execution — run a binary, drive it, collect observations | Judgment — did assertion X hold, emit typed proof |
|---|---|---|
| Universal harness | 9,317 lines, 22 stages | **none.** `ProviderCapabilityProofRecord` appears zero times |
| Release-lane modules | `provider_live_canary.py` (2,032 lines) for claude/opencode/antigravity; raw `subprocess` in `codex_tool_call_result`, `codex_helm_interrupt`, `provider_release_identity` | typed records carrying `AssertionOutcome` and `EvidenceClass` |

Execution is implemented three times. Judgment is implemented once, inside the
stack with almost no execution breadth. No release-lane module imports the
harness.

The target is one execution layer and one oracle layer, connected by the
evidence package.

**The harness becomes the sole execution layer.** It is the only one with
pipeline breadth — ingest, projection, timeline, managed-session E2E. It
already accepts real binaries through `--use-real-provider-bins`, and
`HarnessOptions.provider_builds: Mapping[str, ProviderBuildRef]` already accepts
staged build references. That seam was built by the previous epic and nothing
in the factory calls it.

**The qualification modules become the sole oracle layer**, minus their private
execution. `claude_real_print_qualification` stops calling
`run_provider_live_canary`; it receives an evidence package and returns
assertion outcomes. They keep the one thing only they have: typed capability
proof.

**The factory stops executing.** It becomes acquisition and scheduling — stage
the build, hand it to the execution layer, store the resulting proof records.

Deleted: `provider_live_canary.py`'s execution role, the three raw-subprocess
execution paths, and the factory's executor role. Roughly 2,000 lines go and no
new stack is written.

The diagonal then fills by construction rather than as a feature. Once the seam
is real, real binary crossed with full column is the only path that exists:
staged build → execution layer → every oracle → proof store. It is impossible
today because the executor with breadth produces no proof and the producer of
proof has no breadth.

Integration cost is narrow. The harness has seven `subprocess` call sites, so
giving it an injectable runner — needed to execute under clifford's bwrap,
which the release lane already does via `runner=sandbox` — is small. It needs a
typed proof output. The factory swaps its executor for a harness call.

This does not fix the 4,446-line class. Making the harness the *sole* execution
layer raises the stakes on decomposing it, and makes the still-open question of
whether the seam belongs on provider or on scenario more consequential, not
less.

### One authority, generated consumers

`schemas/managed_providers.yml` stays authority. All three reviewers agreed;
typed code would relocate duplication rather than remove it.

Enforcement changes from revision 1. A repository-wide literal ban plus a
non-increasing allowlist was the wrong instrument — it cannot distinguish
duplication from intentional policy, and the allowlist becomes the ninth
hand-maintained list. Instead, two mechanisms:

1. **Regenerate and diff**, the Kubernetes OpenAPI pattern. One schema,
   generated consumers checked in, CI regenerates and fails on any diff. This
   covers every generated surface without a scanner.
2. **A narrow derivation check at universal fan-out boundaries only** —
   provider enumeration, capability projection, CI matrices, release-lane
   enablement, generated documentation. Intentional subsets must name their
   policy in code rather than sit in an allowlist.

### A planning model, then a planner

The planner cannot be built until the vocabulary exists. The model must state
the relationships among capability assertion, qualification profile, harness
scenario, build provenance, evidence class, and credential policy — the
relationships the empty intersection above proves are currently undefined.

Once defined, `plan_run` is a pure derivation over schema data: no I/O, no
provider branches, no environment reads. It emits a serialized `RunPlan` that
travels into the evidence artifact, so an artifact states what it intended to
run, what it ran, and what it proved. A scenario that did not execute appears
as an unmet plan item rather than as absence.

Fable's addition, adopted: commit the **generated plan matrix** — `plan_run`
evaluated over every provider × provenance × trigger cell, regenerated in CI,
diff-failing on drift. That file is the answer to the cold-agent test, makes
plan changes reviewable in a diff, and feeds the generated status table for
free. "This cell has never run" must be a first-class renderable state, which
is how the diagonal stops being invisible.

### Public proposes, private disposes

All three reviewers landed here, with a distinction worth preserving. The
public repository owns the declarative desired-proof contract. The private
factory consumes it as pinned versioned data, and validates each plan against
**local policy** — which credentials exist, resource caps, allowed providers,
whether live-token scenarios are permitted — then compiles it into private
acquisition steps and records the resolved plan with the contract's hash.

The factory validates policy. It does not restate contract. Restating contract
is how `EXPECTED_RELEASE_CHANNELS` happened. The distinction is the whole
lesson.

Kimi's security framing: a push to a public repo silently reprogramming a
private box that holds provider credentials is the codecov bash-uploader shape.
Pinning plus local policy validation is the mitigation.

Delivery is a real gap revision 1 skipped. clifford mounts the public checkout
read-only at `/opt/longhouse` via `LONGHOUSE_PUBLIC_CHECKOUT` and runs that code
live under `restart: unless-stopped`. The contract must ship inside the factory
bundle, the evidence artifact must record its hash, and staleness must be
visible in the factory's health output — otherwise derivation holds only as of
the last deploy and drift returns with better aesthetics.

### Decomposition proved by onboarding, not by string location

All three reviewers rejected "zero provider literals in the base" as the
done-test, and `ADAPTER_CLASS_BY_PROVIDER` proves why: the dispatch skeleton
already exists and dispatches to nothing. A mechanical push of 48 branches into
five subclasses turns the metric green while every method stays a mirror-shard.

The done-test is the **sixth-provider test**: onboarding a toy provider means
adding one schema entry and one `providers/<name>/` directory, editing zero
existing files, with planner, harness, and derivation checks all passing.
Registration by discovery. Alongside it, cap and publish the
`AgentHarnessAdapter` Protocol width — it is 33 today, and if decomposition
needs forty override points the seam is in the wrong place, which is worth
learning before the split rather than after.

### Capability and proof freshness are different axes

Sol's correction to revision 1's Phase 4. Projecting the capability matrix
purely from proof freshness means a transient factory outage silently
downgrades working functionality to unsupported — the same class of lie in the
opposite direction.

Generate the capability projection from the contract, and attach proof status
separately: `verified`, `stale`, `missing`, `failing`. The UI shows both.
Product support and monitoring health never collapse into one boolean.

## Phases

Sequencing follows Sol's model-first reframe, with Fable's immediate fix pulled
ahead and Kimi's shadow-mode and signal-plan gates inserted.

### Phase 0 — stop the live falsehood, and census

Two things, both small, neither blocked by anything.

Deploy `codex_tool_call_result_v1` in place of `codex_release_identity_v1` on
clifford. It is implemented, and it is one line in a compose file. Using the
env-var mechanism this epic will retire is a deliberate, documented choice: the
most-used provider's release lane should not prove nothing for the weeks the
rest of this takes.

Produce the census as a **generated, CI-checked artifact** with its counting
rule in code — not prose in this document. At 189 files a hand-typed list is
stale the week it lands, and this document would become the twelfth committee
member it exists to abolish.

### Phase 1 — define and validate the planning model

The gate for everything after. Define the relationships among capability
assertion, qualification profile, harness scenario, build provenance, evidence
class, and credential policy. Reconcile the 13 schema scenario ids and the 22
harness scenarios, or state explicitly that they are different types and name
the mapping between them.

Emit a serialized plan that reproduces **current execution exactly**, validated
in both repositories. Change nothing about what runs.

Constants whose semantics the model represents become derived. Constants whose
semantics it does not — version grammars, binary-name sets, acquisition
behavior — stay provider-owned code and are documented as such rather than
forced into the schema.

### Phase 2 — converge the stacks, driven by the plan, shadowed first

The structural core. Three moves, in order, with the adapter implementation
left untouched throughout:

1. Give the harness an injectable runner across its seven `subprocess` sites
   and a typed `ProviderCapabilityProofRecord` output.
2. Strip private execution from the qualification modules so each becomes an
   oracle over an evidence package.
3. Make the factory stage a build and call the execution layer, rather than
   executing itself.

Both loops execute from the serialized plan at the end of this.

Shadow mode is the gate, per Kimi: for one full poll cycle the factory emits
derived plans and runs the converged path in parallel with the existing one,
diffing outcomes without acting on them. Only when the diff is empty does the
converged path become authoritative. Pin the factory's Longhouse ref to a SHA
rather than a moving checkout, and keep the env var as a documented override
shim.

Retire `PROVIDER_FACTORY_QUALIFICATION_PROFILE` at the end of this phase, not
the start.

Deletes: `provider_live_canary.py`'s execution role, the three raw-subprocess
paths, `provider-release-proof.md` (verify inbound links first),
`provider-automation-factory-epic.md`.

### Phase 3 — build the equivalence oracle, then split the adapter

The oracle must be built; it does not exist. A deterministic fixture corpus
plus semantic comparison over explicit outcomes, commands and arguments,
transcript projections, capability booleans, exit status, assertion identities,
and checksums for non-JSON artifacts. Structural fingerprints stay what they
are: schema-drift diagnostics, not equivalence.

Then split the adapter behind `AgentHarnessAdapter`, guarded by that oracle and
by the sixth-provider test.

Deletes: `executable-provider-capability-contract-epic.md`,
`provider-release-proof-roadmap.md`.

### Phase 4 — signal plan, then coverage

The signal plan lands **before** the volume, not after: expected runs per day,
evidence retention, flake retry policy, and alert grouping. Prior art is
wpt.fyi — many implementations, one shared suite, and the public matrix is a
projection of the freshest result per cell so that nobody reads individual
runs. Pair it with Sentry-style fingerprint grouping and Chromium sheriff
practice: page on a novel failure fingerprint or a green→red transition on a
previously stable provider × scenario cell; everything else is dashboard state.

Then turn coverage up — real binaries, full columns, every release, on cube.
Cursor enters as `observed_install` snapshots. The diagonal fills.

Deletes: `provider-build-matrix.md`,
`provider-automation-factory-completion.md`.

### Phase 5 — serving

Capability projection from the contract, proof status attached separately, both
rendered. This document's status tables become generated.

## Definition of done

### Mechanical

- Generated consumers regenerate byte-identically in CI; no hand-written
  restatement of any generated surface.
- The derivation check passes at every universal fan-out boundary, and every
  intentional provider subset names its policy in code.
- Every evidence artifact carries the serialized plan it ran against and the
  hash of the contract that produced it.
- The generated plan matrix regenerates without diff, and cells that have never
  run render as such.
- The sixth-provider test passes: one schema entry, one directory, zero edits
  to existing files.
- One provider factory document under `docs/specs/`, with a link check failing
  on references to deleted specs.
- Capability and proof freshness render as separate values everywhere.

### Human

The cold-agent test. A fresh agent, no context, clean worktree, asked the two
pinned questions: *what does the factory currently prove about Codex, and what
would catch an upstream Codex release that broke managed interrupt?* Correct
answer in five tool calls or fewer.

The grader prompt and scoring live in the repository with fixed wording so the
bar cannot soften between phases. Run it at the end of every phase. A phase
that does not improve the result has not delivered its value even if its
mechanical gates pass.

Revision 1 of this document failed its own test — it took an agent roughly
twenty tool calls to reach an answer, and the answer was wrong.

## Non-goals

- **No evidence caching or skip logic.** Content-hashing the canonical stream
  cannot work and it is the only failure mode that deletes the detection signal
  instead of producing auditable evidence. **Carve-out:** automatic retry of a
  flaky run is not caching. At Phase 4 volume this distinction must be written
  down or someone will over-apply the non-goal or silently reinvent it.
- **No backward diffing for Cursor.** Permanently `observed_only`,
  forward-only, snapshot-on-observation.
- **No credential broker and no self-hosted runner fleet for live-token
  proofs.** Live-token runs stay manual and deliberate.
- **No Antigravity control-surface investment.** Maintenance tier.

## Risks

**The two stacks may not want to converge.** The release lane's ten dedicated
modules and the universal harness were built independently for different
purposes. The execution/oracle split is the epic's core bet and Phase 1 is
where it either holds or is disproven. If the planning model cannot reconcile
them, the honest outcome is two executors sharing one oracle layer and one
contract, and Phase 1 should be allowed to reach that conclusion.

**Phase 2 converges onto a god class that Phase 3 then splits.** Making the
4,446-line harness the sole executor before decomposing it concentrates risk in
the component with the widest blast radius, and briefly makes the codebase
worse. The alternative — splitting first — means guessing the interface before
the planner and the oracle seam have told you what it should be, which is the
sequencing error all three reviewers warned against. The order stands, but the
cost is real and Phase 3 should not be deferred once Phase 2 lands.

**Phase 3 is a 4,446-line refactor behind an oracle that must be built first.**
The oracle is now scoped work rather than an assumed reuse.

**Live production during Phase 2.** clifford runs public code from a mounted
read-only checkout under `restart: unless-stopped`. Shadow mode plus a pinned
SHA is the mitigation; the rollback is repinning to the previous pair, which
the deploy script already supports.

**Shared checkout.** Four or five agents work this repository concurrently and
`main` moved 18 commits under a single session during the previous epic. Phases
run in separate worktrees.

**Sequencing against launch.** Recorded launch blockers are the fragile steer
demo and red non-blocking CI, and this factory serves a product with no users.
Phase 0 is worth doing today. Phase 1 is cheap and is the gate on knowing
whether the rest is even coherent. Phases 3 and 4 should be scheduled
explicitly after the launch blockers rather than drifted into.

## Resolved questions

Revision 1's four open questions, answered consistently by all three reviewers.

1. **Authority direction.** Public Longhouse owns the declarative
   desired-proof contract. The private factory consumes it as pinned versioned
   data and validates against local policy — credentials, caps, allowed
   providers — not against a restatement of the contract.
2. **Schema versus typed code.** Keep YAML. Generate typed views. Typed
   authority would relocate duplication and add a codegen step without adding a
   check.
3. **Split or plan first.** Plan first, against the monolith. Combining
   execution-selection changes with a 4,446-line move destroys attribution when
   results differ, and the planner's shape tells you what the modules must
   expose before you cut them.
4. **Is "zero literals in the base" the right done-test.** No. It is
   satisfiable by relocation. The sixth-provider test replaces it, with Protocol
   width as a supporting metric.

## Still open

- Is the adapter seam correctly placed on **provider** at all? A 33-method
  Protocol suggests the variance may be per-scenario. Phase 1's model work
  should answer this before Phase 3 commits to five provider modules.
- Do the 13 schema scenarios and 22 harness scenarios reconcile into one type,
  or are they permanently two types with an explicit mapping? Phase 1 decides.
