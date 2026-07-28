# Provider factory coherence

**Status:** proposed. No code has moved.

## Why this exists

The provider qualification factory works. It polls upstream every 15 minutes,
downloads real provider binaries, runs them in a sandbox, and has qualified 26
releases. That is not the problem.

The problem is that nobody — human or agent — can find out what it covers
without reading tens of thousands of lines. On 2026-07-28 an agent asked "does
the factory catch a provider release that breaks Longhouse?" needed roughly
twenty tool calls across two repositories to answer, and its first answer was
wrong in both directions: it said release coverage was manual when it is
automatic, and it did not notice that the Codex lane proves only that the
downloaded binary reports the version it claims.

That is the failure this epic fixes. Not "the code is messy" — the code cannot
be read, and what it claims about itself is not reliably true.

## The evidence

Measured on `main` at `072f14dde2`.

### Authority exists but nothing derives from it

`schemas/managed_providers.yml` is declared the single authority for provider
capability in `CLAUDE.md`, and its shape is good: per-capability
`required_assertions` carrying `scenario_id`, `oracle_source`,
`acceptable_evidence`, and `max_age_seconds`. Everything needed to compute what
should run is already declared there.

Almost nothing reads it. One field — Codex platform artifacts — exists in five
places:

| # | Location | Kind |
|---|---|---|
| 1 | `schemas/managed_providers.yml` → `release_channel.platform_artifacts` | authority |
| 2 | `server/zerg/config/managed_provider_contracts.json` | generated from 1, legitimate |
| 3 | `control-plane/provider_factory/core.py` → `ASSET_MAP`, `PACKAGE_ASSET_MAP` | hand-written |
| 4 | `control-plane/provider_factory/release_contract.py` → `EXPECTED_RELEASE_CHANNELS` | hand-written |
| 5 | `control-plane/provider_factory/registry.py` → `_claude_spec`, `_opencode_spec`, `_antigravity_spec` | hand-written |

Copy 4 exists only to be compared against copy 2. It is a drift guard
implemented by restating the contract a second time by hand. It cannot catch
copies 3 or 5, and it cannot catch itself.

`web/src/lib/providers.ts` declares itself "single source of truth for provider
... capability claims" and eight lines later says it "mirrors
`managed_provider_contracts.json`". This is the file that told users Cursor
could not launch, interrupt, or resume while the factory held proof that it
could.

37 non-generated files hold hardcoded multi-provider literals: 17 in
`server/tests_lite/`, 6 in `server/zerg/`, 3 in `engine/src/`, 2 each in
`web/src/`, `scripts/tests/`, `scripts/qa/`, `scripts/ops/`, and one each in
`scripts/ui-fixtures/`, `labs/`, `e2e/tests/`. The test concentration matters
most: a `parametrize` list that names four providers is how a green suite
asserts something false about the fifth.

### The adapter hierarchy carries no behavior

`server/zerg/qa/universal_agent_harness.py` is 9,317 lines.
`UniversalProviderAdapter` spans lines 851–5299 — 4,448 lines in one class. The
five provider adapters below it are empty subclasses containing a docstring:

```python
class ClaudeCodeHarnessAdapter(UniversalProviderAdapter):
    """Claude Code concrete adapter for the universal Longhouse action contract."""
```

All provider variance lives in branches inside the base. That file contains 81
provider-name literals: opencode 35, codex 17, claude 16, antigravity 11,
cursor 2.

The consequence is structural, not cosmetic. There is no location in the
codebase where provider-specific behavior belongs, so it leaks into whatever
file needed it. That is the mechanism behind the standing warning that this
codebase grows hand-maintained per-provider lists, and behind the eight such
lists that one onboarding surfaced.

### What runs is configuration, not derivation

Which scenario a real release is qualified against is
`PROVIDER_FACTORY_QUALIFICATION_PROFILE`, a scalar environment variable in a
compose file on clifford. Which scenarios run in CI is `DEFAULT_SCENARIOS`, a
22-element tuple typed into `scripts/qa/provider-release-proof-universal-smoke.py`.

Neither is derived from the schema, so neither can be checked against it. The
live consequence:

| Lane | Binary | Scenarios | Providers |
|---|---|---|---|
| Release event (private factory) | real, downloaded | 1 per provider | 4 — no Cursor |
| Push (`contract-first-ci.yml`) | generated fake | 4 | 5 |
| Weekly (`provider-release-weekly.yml`) | generated fake | 22 | 5 |

The deployed release-lane assertions, from
`server/zerg/qa/provider_release_semantic_oracles.py` and
`server/zerg/qa/provider_release_identity.py`:

- `claude` — `claude_cli_channel_contract_preserved`, `real_print_marker_returned`
- `opencode` — `serve_session_contract_preserved`, `process_restart_reattach_preserved`
- `antigravity` — `hook_inbox_contract_preserved`, `real_print_injection_observed`
- `codex` — `exact_executable_identity_observed`, `reported_version_matches_expected`

Codex is the most-used provider and its release lane proves only that the
downloaded file is the file that was expected. Richer profiles exist and are
implemented — `codex_tool_call_result_v1`, `codex_helm_interrupt_v1` — they are
simply not what is deployed.

No cell anywhere runs a real provider binary through a full scenario column.

### The documentation is a committee

Eleven provider specs under `docs/specs/`, roughly 250KB. Five present as the
map: `provider-automation-factory-epic.md` (38K),
`provider-release-proof-roadmap.md` (52K),
`executable-provider-capability-contract-epic.md` (24K),
`provider-build-matrix.md` (22K),
`provider-automation-factory-completion.md` (20K). A sixth,
`provider-release-proof.md`, is known stale with ~60 references to the retired
Sauron arrangement. Nothing tells a cold reader which is current.

Total surface: ~19,100 lines in `server/zerg/qa/`, ~19,800 in `scripts/qa/`
provider scripts, ~6,400 in `control-plane/provider_factory/`.

## What this replaces

This spec is the twelfth provider document, which is only defensible because
its completion deletes the other eleven. On completion, `docs/specs/` holds one
provider factory document. The five competing maps are deleted, not archived;
git holds the history. `provider-release-proof-coverage.json` stays only if it
is generated.

Any phase that lands without deleting its share of that list has not landed.

## Position on cheap compute

The framing for this epic is that human attention is the scarce resource and
compute and tokens are not, so the factory should run far more than it does.

That is right about what to buy and wrong about what is binding. Three times in
recent history a green suite here asserted something false: 3,633 backend tests
passed while silently using a binary that happened to be on the laptop;
a frontend test asserted `"longhouse antigravity"`, a command that does not
exist; and `test_cursor_storage_v2_honesty` asserted `can_resume is False`,
which was the exact defect it was named after. Running those continuously
produces continuous false assurance.

Cheap compute is the right way to buy the real thing instead of a proxy — real
binaries rather than generated fakes, full columns rather than one scenario,
every release rather than a weekly sample. That is Phase 3 here, and it is
close to free once the earlier phases exist. It is not a substitute for making
one run mean something.

## Target architecture

### One authority, enforced by absence

`schemas/managed_providers.yml` stays authority. The test of whether something
derives from it is not a guard asserting two copies match — it is that the
value appears exactly once in the repository.

`scripts/qa/check-provider-derivation.py` fails when a provider-name literal or
a provider-derivable constant appears outside the schema, generated files, or a
per-provider module. Legitimate exceptions go in an allowlist that requires a
written reason per entry.

The allowlist is the obvious way this fails: it becomes the ninth
hand-maintained per-provider list. The mitigation is that entries require a
reason and the count is asserted non-increasing, so adding one is visible in
review rather than silent.

### One entry point

A run planner is the single place that answers "what should run":

```
plan_run(longhouse_sha, provider, build, trigger) -> RunPlan
```

`RunPlan` is derived from the schema and holds the ordered scenarios, their
required assertions, acceptable evidence classes, and freshness requirements.
Both loops call it. No scenario tuple or profile name is typed anywhere again.

This is the file a cold reader opens first. Reading it should disclose the rest
of the system: what a provider is, what a scenario is, what counts as proof.

`build.provenance` (`generated_fake`, `staged_release`, `observed_install`) and
`trigger` (`push`, `weekly`, `release_event`, `manual`) become inputs to one
code path rather than three architectures. Scenario eligibility is derived by
matching the build's provenance against the scenario's declared
`acceptable_evidence`, which the schema already carries.

Two consequences follow for free. Real binary crossed with full column — the
empty diagonal — becomes a parameter, not a project. And the plan travels into
the evidence artifact, so an artifact states what it intended to run, what it
ran, and what it proved. A scenario that did not execute appears as an unmet
plan item rather than as absence, which closes by construction the class of gap
found in the previous epic where non-executing scenarios could pass with no
declared build.

### Provider variance gets a home

`server/zerg/qa/providers/<name>/` per provider. The base class keeps only what
is genuinely universal. The done test is mechanical: zero provider-name
literals in the base.

The refactor has an oracle already built. The previous epic added canonical
digests and structural fingerprints to every evidence package, deliberately
unable to authorize a skip. They can authorize a refactor equivalence check:
capture full evidence packages before the split, refactor, and assert the
structural fingerprints are unchanged. This makes the 4,448-line split far less
dangerous than its size suggests.

### Serving projects from proof

The web capability matrix stops mirroring the contract by hand and becomes a
projection of the proof store, gated on `max_age_seconds`. A capability with no
assertion fresher than its declared window does not render as supported. The
system becomes structurally unable to claim what it has not proven.

## Phases

Each phase is independently valuable and independently landable. Each deletes
documentation as well as code.

### Phase 0 — truth census

Enumerate every provider-derivable constant and every document claiming
authority. Produce the kill list that Phase 1 executes against. Done by hand;
this is the spec for everything after.

Output: a list, in this document, of every literal to be deleted and every file
to be deleted.

### Phase 1 — derivation

Delete the copies. `ASSET_MAP`, `PACKAGE_ASSET_MAP`, `EXPECTED_RELEASE_CHANNELS`,
`_claude_spec` / `_opencode_spec` / `_antigravity_spec`, `DEFAULT_SCENARIOS`,
`FAKE_VERSION_BY_PROVIDER`, the `providers.ts` capability flags, and the
hardcoded provider tuples across 37 files. Each becomes a read of the generated
contract. Add `check-provider-derivation.py` to CI.

Behaviorally inert. Every existing test must pass unchanged, and the release
lane must keep qualifying releases through the change.

Deletes: `provider-release-proof.md` (stale), and the coverage doc if it can be
generated.

### Phase 2 — run planner and adapter decomposition

The structural core, the expensive phase, and the only one with real risk.
Split `UniversalProviderAdapter` into per-provider modules. Introduce
`plan_run`. Make both loops call it. Retire
`PROVIDER_FACTORY_QUALIFICATION_PROFILE`.

Guarded by structural-fingerprint equivalence against pre-refactor evidence.
Deserves a hard external review at the seam before it lands.

Deletes: `provider-automation-factory-epic.md`,
`executable-provider-capability-contract-epic.md`,
`provider-release-proof-roadmap.md`.

### Phase 3 — coverage

Turn the plan up. Real binaries through full columns, every release, every
provider, on cube. This is where cheap compute goes and it is a change to plan
inputs rather than new code.

Codex moves off `codex_release_identity_v1`. Cursor enters the release lane as
`observed_install` snapshots.

Deletes: `provider-build-matrix.md`,
`provider-automation-factory-completion.md`.

### Phase 4 — serving

Capability matrix projects from the proof store with freshness. This document's
status tables are generated by `outstanding_factory_work()` rather than typed.

## Definition of done

### Mechanical

- Zero provider-name literals outside the schema, generated files, and
  per-provider modules, enforced in CI.
- No scenario tuple, profile name, or provider list in any environment
  variable, compose file, or workflow.
- One provider factory document under `docs/specs/`. A link check fails on any
  reference to a deleted spec.
- Every capability in the schema either carries a passing assertion younger
  than its `max_age_seconds` or an explicit disposition. This is the gate that
  serves the web capability matrix.
- Every evidence artifact contains the run plan it was produced against.

### Human

The cold-agent test. A fresh agent with no context, given a clean worktree, is
asked: *what does the factory currently prove about Codex, and what would catch
an upstream Codex release that broke managed interrupt?* It must answer
correctly in five tool calls or fewer.

This is the property the epic exists to create, measured directly rather than
by proxy. Run it at the end of every phase, not only at the end. A phase that
does not improve the cold-agent result has not delivered its value even if its
mechanical gates pass.

## Non-goals

Settled decisions, restated so they are not re-litigated:

- **No skip logic or evidence caching.** Content-hashing the canonical stream
  cannot work — `canonical_event_from_fixture` copies the raw provider row and
  `text` carries model output, so the hash always moves on live runs. More
  importantly it is the only failure mode that deletes the detection signal
  instead of producing auditable evidence.
- **No backward diffing for Cursor.** Permanently `observed_only`,
  forward-only, snapshot-on-observation.
- **No credential broker and no self-hosted runner fleet for live-token
  proofs.** Live-token runs stay manual and deliberate.
- **No Antigravity control-surface investment.** Maintenance tier. Ingest,
  archive, and transcript projection keep working and keep being tested; its
  native entrypoint stays excluded.

## Risks

**Phase 2 is a 4,448-line refactor of the component every other piece depends
on.** Mitigated by structural-fingerprint equivalence, but it is the phase that
can go wrong.

**The derivation guard becomes the thing it replaced.** Mitigated by requiring
a written reason per allowlist entry and asserting the count does not grow.

**Shared checkout.** Four or five agents work this repository concurrently and
`main` moved 18 commits under a single session during the previous epic. Phases
run in separate worktrees. Phase 1 lands before anything else starts, because
every later phase depends on the derived values existing.

**Sequencing against launch.** The recorded launch blockers are the fragile
steer demo and red non-blocking CI, and this factory serves a product with no
users. Phases 0 and 1 are cheap and end a recurring cost regardless. Phase 2 is
a deliberate spend and should be chosen, not drifted into.

## Open questions

1. The run planner would live in public Longhouse and be consumed by the
   private factory, meaning the public repository decides what the private
   factory executes. Is that the right authority direction, or should the plan
   be a contract the factory validates against rather than imports?

2. Is the YAML schema the right authority, or should authority be typed code
   with the schema generated from it? The schema is already large at 953 lines
   and Phase 4 adds to it.

3. Phase 2 sequencing: split the adapter first and introduce the planner
   against clean modules, or introduce the planner first against the monolith
   and split under a stable interface?

4. Is "zero provider literals in the base class" the right done-test, or does
   it merely relocate variance into a dispatch table that is equally opaque?
   What would distinguish real decomposition from moved strings?
