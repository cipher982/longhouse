# Provider Build Matrix

**Status:** proposed
**Owner:** Longhouse
**Updated:** 2026-07-26
**Extends:** `provider-automation-factory-epic.md`, `provider-automation-factory-completion.md`
**Scope:** How Longhouse proves what it can do, against which provider builds, and
what has to re-run when either side changes.

## Decision

Longhouse and its providers are two independent change streams writing into one
body of evidence. Today only one is wired into CI: our commits are tested against
fake or ambient binaries, while provider releases are tested elsewhere, on a
schedule, by a private operations lane. The two never meet, so we cannot answer the question the
factory exists to answer — *does Longhouse still work against the Codex that
shipped this morning?*

This makes the provider build a first-class input rather than an ambient property
of whichever machine ran the test.

The governing principle, from two independent reviews that otherwise disagreed:
**rigorous identity, dumb scheduling.** Get the evidence key exactly right,
because a wrong key silently invalidates every conclusion drawn from it. Then
schedule with a cron and a committed table, because at four providers nothing
more earns its keep.

## The incident that motivates it

Onboarding Cursor, the backend suite passed locally with 3,633 tests green, then
failed immediately in CI. `cursor-agent` is installed on the author's laptop and
absent from GitHub runners, so every Cursor test silently resolved the real
binary and passed.

`universal_agent_harness.py:_resolve_binary` (~5284) has three tiers: injected
path, environment override, then `shutil.which()`. The third means a test that
forgets to inject a fake does not fail — it uses whatever the machine has.
Fifteen of fifty-nine `HarnessOptions` constructions rely on this.

Two details matter more than the missing fixture:

- The harness records `binary_source: "PATH"` into the evidence artifact at five
  call sites. The proof already declared that it used an ambient binary. Nothing
  asserted on it.
- The smoke runner already records `provider_bin_mode`. The system knows which
  mode it is in and treats that as a label rather than a rule.

The bug is that **forgetting to declare an input is indistinguishable from asking
for a real binary.**

## Evidence identity

An earlier draft of this spec proposed a three-dimensional matrix,
`(longhouse build × provider build × scenario)`. That is wrong, and it is the
most important correction here.

Scenario is not an independent axis. The same scenario accepts different
fixtures, prompts, and baselines (`universal_agent_harness.py:8777`), so two runs
at the same coordinate can legitimately disagree. Platform and architecture
already belong to proof identity (`provider_release_identity.py:248`). The
action-to-scenario mapping is many-to-many, and `run_scenario` already dispatches
by input shape.

Scenario is a **versioned node in a dependency graph**. Evidence is keyed by:

```text
node revision
+ Longhouse component digests
+ provider artifact digest
+ input / fixture / corpus digest
+ platform + architecture
+ coupling profile
+ oracle digest
```

Keep `(Longhouse artifact, provider artifact)` as the two change streams. Schedule
a DAG, not a cube.

This key is cheap — it is data recorded alongside evidence that already exists.
Getting it right is what lets scheduling stay dumb.

## Build identity is an execution closure

A build is not a file. `provider_release_identity.py:95` hashes one resolved
entrypoint and verifies only that file before and after execution. That misses:

- npm launchers whose unchanged shim loads versioned code from `node_modules`;
- postinstall-downloaded platform binaries;
- symlink targets, interpreters, shared libraries, plugins, sibling assets;
- wrappers that select or download a payload at runtime;
- self-updaters that mutate files other than the entrypoint;
- Cursor's installer payload outside the staged directory.

The store addresses a **platform-specific execution closure**: a Merkle manifest
of installed files and symlinks, plus installer or package integrity, OS and
architecture, launcher target, and required runtime. Run the closure read-only
with updates disabled, then verify the whole manifest afterward.

Layout stays boring:

```text
~/.longhouse/provider-builds/<provider>/<version>/     # human-navigable
provider-builds.lock                                   # version → closure digest
```

Content-addressing is for *identity and verification*, not storage layout. At
four providers there is no dedup, shared-closure, or distribution problem, so
resist a content-addressed filesystem. The lockfile carries the rigor; the
directory stays legible.

## Provenance, not a binary coupling

An earlier draft listed `provider_binary` as one of six hermeticity axes. Drop
it. **Execution always receives a pinned build.** There is no "ambient" option to
seal or open, so the axis does not exist.

Real versus fake is **artifact provenance**:

| `artifact_provenance` | Meaning |
| --- | --- |
| `generated_fake` | Fake built by Longhouse, hashed into the store |
| `staged_release` | Real build fetched by declared version from a declared channel |
| `observed_only` | Real build captured as-installed; forward-only, not requestable |

This follows the Nix lesson: purity enforced by layout beats purity asserted in
metadata. There is nothing to assert on `binary_source` because there is no way
to reach a binary that was not handed to you.

The remaining couplings are declared, and enforced only where enforcement is
cheap:

| Coupling | Sealed | Enforcement |
| --- | --- | --- |
| `model_tokens` | none spent | declared; verified by absence of provider credentials |
| `network` | no egress, or an allowlist | declared now, enforced later |
| `provider_account` | no credentials present | enforced by environment scrubbing |
| `runtime_host` | fixture | structural, already true |
| `filesystem` | fresh `HOME`, trapped `PATH` | **already enforced** in `native-installer-smoke.sh` |

`network` is an allowlist, not a boolean. `filesystem` replaces the vaguer
"machine state."

## Replacing the 21 proof boundaries

`ALLOWED_PROOF_BOUNDARY` has 21 values, 13 of them compounds joined by `_or_` and
`_plus_`. Mapping them onto couplings alone is **not total** — they encode five
distinct concepts at once: couplings, evidence aggregation (the disjunctions are
alternative runs), acquisition provenance, executor authority, and oracle scope.

So a boundary becomes `runs[]`, where each run declares:

```yaml
runs:
  - couplings: {}                        # hermetic
    artifact_provenance: generated_fake
    input_provenance: fixture
    executor: ci
    oracle_scope: contract_shape
  - couplings: [model_tokens, provider_account, network]
    artifact_provenance: staged_release
    input_provenance: live_capture
    executor: manual
    oracle_scope: transcript_assertion
```

`hermetic_plus_manual_live_token_or_machine_live_token` stops being a coined name
and becomes what it always was: a bundle of two runs with different executors.

## The seam: what a provider release does not invalidate

A control-plane test has nothing to do with a Cursor release. Neither does an iOS
build. A test that parses provider logs into the database is the opposite — it is
the most provider-sensitive thing in the repo.

Measured on this branch:

- iOS has **zero** control-plane names and **zero** behavioral provider branches.
  Provider references are glyphs, brand labels, previews, and picker enumeration.
  It receives `controlOperationsByProvider` from the server at runtime and never
  encodes what a provider can do.
- Web is mostly server-driven but **not literally provider-agnostic**.
  `web/src/lib/sessionWorkspace/interaction.ts:6` hardcodes managed-launch copy
  and commands for Claude and Codex only — Cursor and OpenCode fall through to
  `null`, so the UI never suggests `longhouse cursor` despite Cursor shipping as
  first-tier. Line 45 exports a Codex-specific interaction fact.

That Cursor omission is a live product bug and the seventh hand-maintained
per-provider list found while onboarding one provider.

**Derive invalidation from declared component dependencies, not from
provider-name searches.** A grep for provider names finds branding; it does not
find behavior, and it will not stay true.

## Skip logic is deferred, deliberately

An earlier draft proposed hashing the canonical event stream to skip
provider-neutral work. That does not work today and should not be built yet.

`canonical_event_from_fixture` sets `"provider_event": dict(row)` — it copies the
entire raw provider row unchanged, and `text` carries model output. Live rows
carry provider session and thread IDs, state paths, and canary markers; DB
identities depend on `package.root`. An exact hash would move on every live run.

It fails *safe* — spurious re-runs, not unsafe skips — but delivers none of the
promised savings.

More importantly, **skip logic is the single most dangerous thing in this
design**, because it is the only failure that deletes the detection signal
itself. Everything else produces wrong-but-auditable evidence. Automating it
first would be automating the worst failure mode first.

When it is eventually built:

- restrict it to **replay of identical captured raw evidence**, never as a
  provider-release shortcut;
- define a versioned, presentation-relevant canonical digest that strips
  provenance paths, timestamps, and volatile metrics, and renumbers relational
  IDs deterministically;
- prove stability by normalizing twice in different roots;
- for the live axis, compare **structural fingerprints** with values normalized —
  machinery the epic already specifies and `schema_fingerprints` already
  partially implements.

Until then the full column runs. At four providers it is cheap.

## Sequencing

Four steps. Each is useful if the next never happens.

**1. Fail-closed runner.** Delete tier-3 `shutil.which()` from `_resolve_binary`;
fix the fifteen call sites; the runner requires a build reference or fails.
`which()` survives only inside acquisition, which stages what it finds and records
it. Existing tests keep working via injected fakes. Alone, this permanently kills
the "green suite testing a laptop" bug class.

**2. Build store, fakes only.** Generate fakes into the store with closure
digests recorded in evidence. No network, no credentials, offline-safe. Buys
reproducible hermetic evidence and proves the store abstraction before anything
external depends on it.

**3. Channel declarations and staging for npm-shaped providers.** Declare
`channel` / `coordinate` / `version_discovery` / `verification`. Generalize
the existing Claude staging to Codex and OpenCode. Defers Cursor and every
credential question. At the end of this step at least one provider column has
real-build evidence inside Longhouse CI.

**4. Sparse live scheduling, and only then skip logic.** Requires measurement of
hash stability first.

Cursor enters at step 3 as `observed_only`: snapshot each observed install into
its own versioned archive. After two observations it becomes diffable going
forward, which is how Chrome-for-Testing solved the same problem.

## Failure modes, ranked

1. **Skip logic hides a real break.** Most dangerous — it deletes the signal.
   Guard: a weekly unconditional full-column re-run that ignores skip decisions,
   plus alerts when the skip rate approaches 100% or 0%, since both mean the
   mechanism is broken rather than the world.
2. **Fake drift makes hermetic proofs comforting lies.** Guard: the disposition
   and evidence-level split already prevents hermetic verdicts carrying live
   weight; add one live canary per provider on the weekly cron, and generate
   fakes from recorded real sessions rather than by hand.
3. **Behavior changes without the version string changing** — including
   `observed_only` re-pulls serving new bits under the same label. Guard: key
   everything by closure digest, treat the version string as display metadata,
   and make the digest↔version map append-only so a moved digest under a known
   version is a loud event.
4. **Version-discovery death.** If enumeration breaks, no new columns ever
   appear, absence-of-evidence never fires, and the system looks green forever.
   Guard: alert when the newest known build for a provider ages past a threshold.
5. **Canonical-hash nondeterminism.** Skip rate collapses to zero, costs rise,
   and operators learn to rubber-stamp. Failure mode is noise rather than
   silence, which is why step 4 is gated on measurement.
6. **Stale accepted baselines.** Guard: bind baseline identity to
   `(provider digest, adapter digest, canonical digest)` so a baseline whose
   coordinates moved is inapplicable by construction rather than merely old.

## Prior art

- **Playwright** vendors exact browser revisions and never touches system Chrome.
  "Works with whatever is installed" is not a supported mode. Make the store the
  only path.
- **Chrome-for-Testing / Puppeteer** handled upstream-serves-only-latest by
  snapshotting every observed build into a versioned archive. That is the Cursor
  `observed_only` answer: the store becomes the version history.
- **Renovate / Dependabot** deliver upstream releases as ordinary PRs through the
  same CI lane rather than a separate watcher. "New column, empty cells" becomes
  a commit.
- **GitHub Actions matrix / tox** declare the axis as data with per-cell
  allow-failure, so one broken upstream version never blocks the whole run.
- **Nix** enforces purity by layout rather than asserting it in metadata.
- **Rust crater** gets old-versus-new diffing as a byproduct of running both
  columns, not as a separate lane.
- **web-platform-tests / Test262**: recorded conformance fixtures are the durable
  asset that outlives any adapter. Invest in fixtures over harness cleverness.

## Do not build

- **No scheduler or disposition engine.** A weekly cron and a committed table
  suffice at four providers.
- **No external sandbox for the remaining couplings.** Trapped `PATH` and fresh
  `HOME` already exist in `native-installer-smoke.sh`. Enforce the pinned build,
  declare the rest, stop.
- **No general version-detection subsystem.** npm registry for Claude, a short
  GitHub releases poll for Codex and OpenCode, observe-and-snapshot for Cursor.
- **No credential broker or self-hosted runners.** Pre-launch, live-token proofs
  run manually from the laptop on a reminder, or not at all.
- **No backward diffing for Cursor.** Forward-only, permanently.
- **No Antigravity staging** until it re-enters the factory.
- **No skip logic yet.** See above.
- **No dashboards.** Evidence artifacts and a generated table.

## Do not rebuild what exists

- **The private lane already stages exact npm versions** of `@anthropic-ai/claude-code`
  into an isolated artifact root and runs proof and differential envelopes. Step
  3 is generalizing something already working.
- **`native-installer-smoke.sh` already implements a real cleanroom** — fresh
  `HOME`, `traps/` fakes, restricted `PATH`, trapped `python`/`uv`/`pip` — so "no
  Python fallback participates" is proven rather than claimed. It is the best
  existing design in the repo and it is confined to one shell script.
- **Release-identity profiles** already pin version and digest per provider; they
  need widening from entrypoint to closure.
- **The managed-provider contract** is already the single authority for provider
  sets and support facts.

## Where the public/private boundary sits

The factory splits along one line: **the public repo defines what a proof means;
a private operations lane decides when to spend money to get one.**

Public — everything a self-hoster needs to run the factory against fakes and get
real answers:

- the managed-provider contract and its dispositions
- the universal harness, scenarios, and oracles
- release-identity profiles and evidence formats
- staging **declarations**: which channel, which coordinate, how to verify

Private — everything that costs money or holds credentials:

- release watching and version discovery
- credential custody and staging execution against real channels
- scheduling and the decision to spend tokens
- the baseline-guard store and the accumulated evidence corpus

Staging declarations are public; staging execution with credentials is not.

Note that *staging* currently sits entirely on the private side, which is why
Longhouse CI cannot test against a real pinned build at all. Moving the
declarations into the contract is what makes staging runnable identically on a
laptop, on a CI runner, and from a scheduled trigger — with only the credentials
and the spend decision remaining private.

## Open questions

1. Can Cursor builds be requested by version, or only observed? Determines
   whether it is ever diffable backwards. Expected answer: observed only.
2. Does the canonical stream normalize stably enough to drive skipping? Gates
   step 4 and must be answered by measurement, not argument.
3. Which closure manifest granularity is right per channel — full installed tree
   for npm, single asset plus interpreter for release binaries?
4. What is the staleness threshold per provider for the version-discovery-death
   alert?
